"""
NEXUS v5.0 (beta) — OpportunityAgent (Asset Selection Layer)
=============================================================
Background agent that selects the best IQ Option asset every
5 minutes using a composite score:

    composite_score = payout_weight * payout_pct
                    + atr_weight   * normalized_atr

Writes result to Redis key: NEXUS:BEST_ASSET
Pipeline reads from Redis — zero WebSocket calls in the tick path.

Selection criteria:
    1. Payout >= min_payout (hard filter, default 80%)
    2. ATR(14, 1m) > atr_floor (hard filter, ensures movement)
    3. composite_score = max(payout*0.6 + norm_atr*0.4)

Fallback: if Redis is unavailable or no asset passes the filters,
returns None and logs a WARNING. The pipeline handles None gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import pandas as pd
import redis as redis_lib

from nexus.core.execution.base import AbstractExecutionEngine

logger = logging.getLogger("nexus.opportunity")


def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Computes ATR(period) on an OHLCV DataFrame.

    Args:
        df:     DataFrame with columns: high, low, close.
                Must have at least period+1 rows.
        period: ATR lookback period.

    Returns:
        ATR value as a float, or 0.0 if computation fails.
    """
    if df is None or len(df) <= period:
        return 0.0

    try:
        df = df.copy()
        # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        df['prev_close'] = df['close'].shift(1)
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = (df['high'] - df['prev_close']).abs()
        df['tr3'] = (df['low'] - df['prev_close']).abs()
        
        df['true_range'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        # Average True Range
        atr_series = df['true_range'].rolling(window=period).mean()
        
        atr = float(atr_series.iloc[-1])
        if pd.isna(atr):
            return 0.0
        return atr
    except Exception as exc:
        logger.debug(f"_compute_atr failed: {exc}")
        return 0.0


class OpportunityAgent:
    """
    Background agent — selects the best available IQ Option asset
    every `interval_minutes` and caches the result in Redis.
    """

    REDIS_KEY = "NEXUS:BEST_ASSET"
    REDIS_TTL_S = 600  # 10 minutes — if agent stalls, key expires

    def __init__(
        self,
        execution_engine: AbstractExecutionEngine,
        redis_client: Optional[redis_lib.Redis],
        interval_minutes: float = 5.0,
        min_payout: int = 80,
        atr_floor: float = 0.0003,
        payout_weight: float = 0.6,
        atr_weight: float = 0.4,
    ) -> None:
        """
        Args:
            execution_engine: Active venue engine (IQ Option).
            redis_client:     Shared Redis bus. If None, agent logs
                              WARNING and writes to internal cache only.
            interval_minutes: How often to refresh the asset selection.
            min_payout:       Hard filter — assets below this % ignored.
            atr_floor:        Hard filter — assets with ATR(14,1m) below
                              this value ignored (no movement = no edge).
            payout_weight:    Weight of payout in composite score (0-1).
            atr_weight:       Weight of ATR in composite score (0-1).
        """
        self.execution_engine = execution_engine
        self.redis_client = redis_client
        self.interval_minutes = interval_minutes
        self.min_payout = min_payout
        self.atr_floor = atr_floor
        self.payout_weight = payout_weight
        self.atr_weight = atr_weight

        self._task: Optional[asyncio.Task] = None
        self._last_best_asset: Optional[str] = None
        self._last_selection_ts: float = 0.0
        self._running: bool = False
        self._redis_warned: bool = False

    async def start(self) -> None:
        """Launches the background selection loop."""
        if self._running:
            return
        
        logger.info(f"OpportunityAgent started (interval={self.interval_minutes}min)")
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Cancels the background loop cleanly."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("OpportunityAgent stopped.")

    async def get_best_asset(self) -> Optional[str]:
        """
        Returns the current best asset.
        Read order: Redis → internal cache → None.
        Never calls the execution engine directly (read-only accessor).
        """
        # Limpiar activos invalidados expirados
        if hasattr(self, "_invalidated_assets"):
            now = time.time()
            self._invalidated_assets = {
                k: v for k, v in self._invalidated_assets.items() if v > now
            }
            if self._best_asset in self._invalidated_assets:
                self._best_asset = None

        # Try Redis first if available
        if self.redis_client is not None:
            try:
                cached = self.redis_client.get(self.REDIS_KEY)
                if cached:
                    if isinstance(cached, bytes):
                        cached = cached.decode('utf-8')
                    # Skip if invalidated
                    if hasattr(self, "_invalidated_assets") and str(cached) in self._invalidated_assets:
                        return self._last_best_asset
                    return str(cached)
            except Exception as exc:
                logger.debug(f"Redis get failed: {exc}")

        # Fallback to internal cache and log warning if Redis missing
        if self.redis_client is None and self._last_best_asset is not None:
            # We don't spam warning, just fallback
            pass
            
        return self._last_best_asset

    async def invalidate_asset(self, asset: str, ttl_seconds: int = 300) -> None:
        """
        Marca un activo como no disponible temporalmente (p.ej. suspendido).
        El OpportunityAgent ignorará este activo en los próximos 'ttl_seconds'.
        """
        if not hasattr(self, "_invalidated_assets"):
            self._invalidated_assets: dict = {}
        self._invalidated_assets[asset] = time.time() + ttl_seconds
        logger.warning(f"🚫 Activo invalidado temporalmente: {asset} ({ttl_seconds}s)")
        # Si el activo inválido era el best_asset actual, limpiar caché
        if self._last_best_asset == asset:
            self._last_best_asset = None
            if self.redis_client:
                try:
                    self.redis_client.delete(self.REDIS_KEY)
                except Exception:
                    pass

    async def _run_loop(self) -> None:
        """
        Background coroutine. Runs every interval_minutes.
        Calls _select_best_asset() and writes result to Redis.
        Handles all exceptions internally — never propagates to caller.
        """
        await asyncio.sleep(10.0)  # Let execution engine connect fully
        logger.debug("OpportunityAgent: startup delay complete, entering selection loop")
        
        while self._running:
            try:
                asset = await self._select_best_asset()
                if asset:
                    # Append suffix specifically per spec (if missing)
                    if not str(asset).endswith("-op"):
                        asset = f"{asset}-op"
                        
                    self._last_best_asset = asset
                    self._last_selection_ts = time.time()
                    
                    if self.redis_client:
                        try:
                            self.redis_client.set(self.REDIS_KEY, asset, ex=self.REDIS_TTL_S)
                        except Exception as exc:
                            logger.debug(f"Redis set failed: {exc}")
                    else:
                        if not self._redis_warned:
                            logger.warning("OpportunityAgent: Redis unavailable. Using internal cache.")
                            self._redis_warned = True
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"OpportunityAgent loop exception: {exc}")
            
            # Sleep until next interval
            await asyncio.sleep(self.interval_minutes * 60)

    async def _select_best_asset(self) -> Optional[str]:
        """
        Core selection logic.

        Steps:
          1. Call execution_engine.get_all_profit() — get payout map.
          2. Hard filter: payout >= min_payout.
          3. For each candidate: fetch last 30 candles (1m) via
             execution_engine.get_historical_data(asset, "1m", 30).
          4. Compute ATR(14) on those candles.
          5. Hard filter: ATR(14) >= atr_floor.
          6. Compute composite_score = payout_weight * payout_pct
                                     + atr_weight * normalized_atr
             where normalized_atr = atr / atr_floor (ratio to floor,
             capped at 3.0 to prevent extreme outliers dominating).
          7. Return the asset with max composite_score.
             Append '-op' suffix before returning (IQ pipeline standard).

        Returns None if:
          - No assets pass both hard filters.
          - execution_engine is not connected.
          - Any unrecoverable exception occurs.
        """
        if not self.execution_engine or not self.execution_engine.is_connected:
            logger.warning("OpportunityAgent: execution engine not connected")
            return None

        try:
            # 1. Get payout map
            payouts = {}
            if hasattr(self.execution_engine, "get_all_profit"):
                # Fast path if it was added
                res = await getattr(self.execution_engine, "get_all_profit")()
                if isinstance(res, dict):
                    payouts = res
            elif hasattr(self.execution_engine, "_api") and getattr(self.execution_engine, "_api"):
                # Direct access to the JCBV API underneath IQOptionExecutionEngine
                api = getattr(self.execution_engine, "_api")
                if hasattr(api, "get_all_profit"):
                    res = await asyncio.to_thread(api.get_all_profit)
                    if isinstance(res, dict):
                        payouts = res

            if not payouts:
                logger.debug("OpportunityAgent: get_all_profit returned empty/None")

            # 2. Hard filter: payout >= min_payout and Top 10 sort
            valid_candidates = []
            for asset, payout in payouts.items():
                # Handle IQ Option's weird nested structures if any
                if isinstance(payout, dict):
                    val = payout.get("turbo", 0)
                    if 0 < val < 1.0:
                        payout = float(val * 100)
                    elif val > 0:
                        payout = float(val * 100 if val < 10 else val)
                    else:
                        payout = 0.0

                if isinstance(payout, (int, float)) and payout >= self.min_payout:
                    valid_candidates.append((asset, payout))
            
            if not valid_candidates:
                logger.warning(f"⚠️ No asset passed payout+ATR filters. Keeping previous: {self._last_best_asset}")
                return self._last_best_asset

            # Top 10 by payout
            valid_candidates.sort(key=lambda x: x[1], reverse=True)
            top_candidates = valid_candidates[:10]

            best_asset = None
            best_score = -1.0
            best_stats = {}

            # 3. Process top candidates
            candidate_data = []

            # First pass: gather data to compute atr_reference
            for asset, payout in top_candidates:
                try:
                    clean = asset.replace("-op", "").replace("_", "").upper()
                    df = await self.execution_engine.get_historical_data(clean, "1m", 30)
                    if df is None or len(df) < 15:
                        continue
                        
                    atr = _compute_atr(df, period=14)
                    if atr < self.atr_floor:
                        continue
                        
                    closes = df["close"].values
                    volumes = df["volume"].values
                    
                    if len(closes) >= 5:
                        momentum_raw = abs(closes[-1] - closes[-5]) / (closes[-5] + 1e-9)
                        momentum_norm = min(momentum_raw / 0.02, 1.0)
                    else:
                        momentum_norm = 0.0
                        
                    if len(volumes) >= 10:
                        vol_std = volumes[-10:].std()
                        vol_mean = volumes[-10:].mean()
                        liquidity_norm = 1.0 - (vol_std / (vol_mean + 1e-9))
                        liquidity_norm = max(0.0, min(liquidity_norm, 1.0))
                    else:
                        liquidity_norm = 0.5
                        
                    candidate_data.append({
                        "asset": asset,
                        "payout": payout,
                        "atr": atr,
                        "momentum_norm": momentum_norm,
                        "liquidity_norm": liquidity_norm
                    })
                except Exception as cand_exc:
                    logger.debug(f"Eval err for {asset}: {cand_exc}")
                    continue

            if not candidate_data:
                logger.warning(f"⚠️ No asset passed filters. Keeping previous: {self._last_best_asset}")
                return self._last_best_asset

            import numpy as np
            atrs = [d["atr"] for d in candidate_data]
            atr_reference = np.percentile(atrs, 75) if atrs else 1.0
            if atr_reference == 0:
                atr_reference = 1e-9

            # Second pass: Compute scores with multicriteria formula
            for d in candidate_data:
                asset = d["asset"]
                payout = d["payout"]
                atr = d["atr"]
                momentum_norm = d["momentum_norm"]
                liquidity_norm = d["liquidity_norm"]
                
                payout_norm = min(payout / 95.0, 1.0)
                atr_norm = min(atr / atr_reference, 1.0)
                
                score = (payout_norm * 40.0) + (atr_norm * 30.0) + (momentum_norm * 20.0) + (liquidity_norm * 10.0)
                
                logger.debug(f"📊 {asset} score={score:.3f} | payout_n={payout_norm:.2f} atr_n={atr_norm:.2f} momentum_n={momentum_norm:.2f} liquidity_n={liquidity_norm:.2f}")
                
                # Tiebreaker logic: if score is almost identical, pick the one with highest ATR
                update_best = False
                if score > best_score + 0.01:
                    update_best = True
                elif abs(score - best_score) <= 0.01:
                    if best_stats and atr > best_stats.get("atr", 0):
                        update_best = True

                if update_best:
                    best_score = score
                    best_asset = asset
                    best_stats = {"payout": payout, "atr": atr, "score": score}
            for asset, payout in top_candidates:
                try:
                    # Strip IQ option internal suffixes if we need to request properly from get_historical_data
                    clean = asset.replace("-op", "").replace("_", "").upper()
                    
                    df = await self.execution_engine.get_historical_data(clean, "1m", 30)
                    if df is None or len(df) < 15:
                        continue
                        
                    # 4. Compute ATR
                    atr = _compute_atr(df, period=14)
                    
                    # 5. Hard filter ATR
                    if atr < self.atr_floor:
                        continue
                        
                    # 6. Composite Score
                    normalized_atr = min(atr / self.atr_floor, 3.0)
                    score = (self.payout_weight * payout) + (self.atr_weight * normalized_atr)
                    
                    logger.debug(f"{asset} | payout={payout:.1f}% | ATR={atr:.5f} | score={score:.4f}")
                    
                    if score > best_score:
                        best_score = score
                        best_asset = asset
                        best_stats = {"payout": payout, "atr": atr, "score": score}
                        
                except Exception as cand_exc:
                    logger.debug(f"ATR eval err for {asset}: {cand_exc}")
                    continue

            # 7. Final Selection Evaluation
            if best_asset:
                if best_asset == self._last_best_asset:
                    logger.info(f"🔄 Asset confirmed: {best_asset} (same as before)")
                else:
                    logger.info(f"🔎 Best asset: {best_asset} | payout={best_stats['payout']:.1f}% | "
                                f"ATR={best_stats['atr']:.5f} | score={best_stats['score']:.4f}")
                return best_asset
            else:
                logger.warning(f"⚠️ No asset passed payout+ATR filters. Keeping previous: {self._last_best_asset}")
                return self._last_best_asset

        except Exception as exc:
            logger.error(f"OpportunityAgent unexpected _select_best_asset exception: {exc}")
            return self._last_best_asset
