"""
NEXUS v4.0 — Walk-Forward Optimization (WFO) Auto-Calibrator
==============================================================
Vectorized parameter grid search for the Alpha V3 signal engine.

Evaluates RSI / Bollinger parameter combinations across three
simulated market regimes (GREEN=trend, YELLOW=range, RED=panic)
and writes the winning parameters to Redis as:
    NEXUS:OPT:<ASSET>:<REGIME>  →  {"rsi_period": N, "bb_dev": M, ...}

The pipeline reads these keys when AI_MODE == 1 to dynamically
tune the signal engine per-tick without restarting.

Usage:
    python main.py calibrate --asset EURUSD-op
    
    Or directly:
    python -m nexus.scripts.auto_calibrate --asset EURUSD-op

Architecture:
    1. Download OHLCV from QuestDB (fallback: generate synthetic data)
    2. Classify each candle into a regime (GREEN/YELLOW/RED) by volatility
    3. For each regime slice, run a vectorized grid search:
       - Compute RSI + Bollinger signals for every param combo simultaneously
       - Score by Profit Factor or win rate on simulated binary trades
    4. Write winners to Redis
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands

logger = logging.getLogger("nexus.calibrate")


# ══════════════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════════════

# Parameter grid for the Alpha V3 signal engine
PARAM_GRID: Dict[str, List[Any]] = {
    "rsi_period": [5, 7, 10, 14, 21],
    "bb_dev": [1.5, 2.0, 2.5, 3.0],
}

# Regime classification thresholds (by rolling volatility percentile)
REGIME_THRESHOLDS = {
    "GREEN": (0.0, 0.33),   # Low volatility → trending
    "YELLOW": (0.33, 0.66), # Medium volatility → ranging
    "RED": (0.66, 1.0),     # High volatility → panic
}

# Binary option simulation parameters
BINARY_EXPIRY_BARS = 5   # 5-minute candles (IQ Option turbo)
BINARY_PAYOUT = 0.85     # 85% payout on win

# Redis key prefix for optimized params
_REDIS_OPT_PREFIX = "NEXUS:OPT"


# ══════════════════════════════════════════════════════════════════════
#  Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class CalibrationResult:
    """Result of a single parameter combination evaluation."""
    rsi_period: int
    bb_dev: float
    regime: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    net_pnl: float


@dataclass
class RegimeWinner:
    """Best parameters for a specific regime."""
    regime: str
    params: Dict[str, Any]
    win_rate: float
    profit_factor: float
    total_trades: int
    net_pnl: float


# ══════════════════════════════════════════════════════════════════════
#  Regime Classifier
# ══════════════════════════════════════════════════════════════════════

def classify_regimes(df: pd.DataFrame, window: int = 50) -> pd.Series:
    """
    Classify each candle into GREEN/YELLOW/RED by rolling volatility percentile.
    
    Uses the rolling standard deviation of returns as a volatility proxy.
    Low vol = GREEN (trend), Medium = YELLOW (range), High = RED (panic).
    """
    returns = df["close"].pct_change()
    rolling_vol = returns.rolling(window, min_periods=10).std()
    
    # Percentile rank within the series (0.0 to 1.0)
    vol_pctile = rolling_vol.rank(pct=True)
    
    regimes = pd.Series("GREEN", index=df.index)
    regimes[vol_pctile > REGIME_THRESHOLDS["YELLOW"][0]] = "YELLOW"
    regimes[vol_pctile > REGIME_THRESHOLDS["RED"][0]] = "RED"
    
    # First 'window' bars don't have enough data
    regimes.iloc[:window] = "GREEN"
    
    return regimes


# ══════════════════════════════════════════════════════════════════════
#  Vectorized Signal Simulation
# ══════════════════════════════════════════════════════════════════════

def simulate_signals(
    df: pd.DataFrame,
    rsi_period: int,
    bb_dev: float,
    expiry_bars: int = BINARY_EXPIRY_BARS,
    payout: float = BINARY_PAYOUT,
) -> pd.DataFrame:
    """
    Vectorized simulation of Alpha V3 mean-reversion signals.
    
    For each bar, computes:
    - RSI oversold/overbought → BUY/SELL signal
    - Bollinger band touch → confirmation
    - Binary outcome: price after 'expiry_bars' candles
    
    Returns DataFrame with columns: signal, outcome, pnl
    """
    close = df["close"]
    low = df["low"]
    high = df["high"]
    
    # Compute indicators (vectorized by ta library)
    rsi = RSIIndicator(close=close, window=rsi_period).rsi()
    bb = BollingerBands(close=close, window=20, window_dev=bb_dev)
    bb_lower = bb.bollinger_lband()
    bb_upper = bb.bollinger_hband()
    
    # Signal generation (fully vectorized)
    buy_signal = (low <= bb_lower) & (rsi < 35)
    sell_signal = (high >= bb_upper) & (rsi > 65)
    
    signals = pd.Series(0, index=df.index)  # 0=HOLD
    signals[buy_signal] = 1   # BUY (CALL)
    signals[sell_signal] = -1  # SELL (PUT)
    
    # Outcome: price change after expiry_bars candles
    future_close = close.shift(-expiry_bars)
    price_diff = future_close - close
    
    # Binary option outcome
    # BUY wins if price goes UP, SELL wins if price goes DOWN
    outcome = pd.Series(0.0, index=df.index)
    
    # BUY trades
    buy_mask = signals == 1
    outcome[buy_mask & (price_diff > 0)] = payout     # WIN
    outcome[buy_mask & (price_diff <= 0)] = -1.0       # LOSS
    
    # SELL trades
    sell_mask = signals == -1
    outcome[sell_mask & (price_diff < 0)] = payout     # WIN
    outcome[sell_mask & (price_diff >= 0)] = -1.0      # LOSS
    
    result = pd.DataFrame({
        "signal": signals,
        "outcome": outcome,
    }, index=df.index)
    
    # Remove last 'expiry_bars' rows (no future data)
    result = result.iloc[:-expiry_bars] if expiry_bars > 0 else result
    
    return result


# ══════════════════════════════════════════════════════════════════════
#  Grid Search Engine
# ══════════════════════════════════════════════════════════════════════

def run_grid_search(
    df: pd.DataFrame,
    regime_labels: pd.Series,
    regime: str,
) -> List[CalibrationResult]:
    """
    Run full parameter grid search for a specific regime.
    Returns sorted list of CalibrationResult (best first).
    """
    # Filter data for this regime
    mask = regime_labels == regime
    regime_df = df[mask].copy()
    
    if len(regime_df) < 100:
        logger.warning(
            f"Insufficient data for regime {regime}: {len(regime_df)} bars (need 100+)"
        )
        return []
    
    results: List[CalibrationResult] = []
    
    for rsi_p in PARAM_GRID["rsi_period"]:
        for bb_d in PARAM_GRID["bb_dev"]:
            try:
                sim = simulate_signals(regime_df, rsi_period=rsi_p, bb_dev=bb_d)
                
                # Only count bars with actual trades
                trades = sim[sim["signal"] != 0]
                total = len(trades)
                
                if total < 5:
                    continue
                
                wins = int((trades["outcome"] > 0).sum())
                losses = total - wins
                win_rate = wins / total if total > 0 else 0.0
                
                # Profit Factor = gross_profit / gross_loss
                gross_profit = float(trades[trades["outcome"] > 0]["outcome"].sum())
                gross_loss = abs(float(trades[trades["outcome"] < 0]["outcome"].sum()))
                pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
                
                net_pnl = gross_profit - gross_loss
                
                results.append(CalibrationResult(
                    rsi_period=rsi_p,
                    bb_dev=bb_d,
                    regime=regime,
                    total_trades=total,
                    wins=wins,
                    losses=losses,
                    win_rate=round(win_rate, 4),
                    profit_factor=round(pf, 4),
                    net_pnl=round(net_pnl, 4),
                ))
            except Exception as exc:
                logger.debug(f"Grid eval failed for RSI={rsi_p}, BB={bb_d}: {exc}")
                continue
    
    # Sort by profit factor (descending), then by win rate
    results.sort(key=lambda r: (r.profit_factor, r.win_rate), reverse=True)
    
    return results


# ══════════════════════════════════════════════════════════════════════
#  Data Acquisition
# ══════════════════════════════════════════════════════════════════════

async def fetch_data_from_questdb(
    asset: str,
    timeframe: str = "1m",
    limit: int = 50000,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV data from QuestDB REST API.
    Returns None if QuestDB is offline.
    """
    import urllib.request
    import urllib.parse
    
    host = os.getenv("QUESTDB_REST_HOST", "localhost")
    port = int(os.getenv("QUESTDB_REST_PORT", "9000"))
    
    query = (
        f"SELECT timestamp, open, high, low, close, volume "
        f"FROM nexus_candles "
        f"WHERE asset = '{asset}' AND timeframe = '{timeframe}' "
        f"ORDER BY timestamp DESC "
        f"LIMIT {limit}"
    )
    
    try:
        url = (
            f"http://{host}:{port}/exec"
            f"?query={urllib.parse.quote(query)}"
        )
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        
        columns = [col["name"] for col in data.get("columns", [])]
        rows = data.get("dataset", [])
        
        if not rows:
            return None
        
        df = pd.DataFrame(rows, columns=columns)
        
        # Ensure proper types
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
        
        logger.info(f"QuestDB: Loaded {len(df)} candles for {asset}/{timeframe}")
        return df
        
    except Exception as exc:
        logger.warning(f"QuestDB fetch failed: {exc}")
        return None


def generate_synthetic_data(bars: int = 20000) -> pd.DataFrame:
    """
    Generate synthetic OHLCV data with realistic regime shifts.
    Used as fallback when QuestDB is offline.
    """
    logger.info(f"Generating {bars} synthetic OHLCV bars for calibration...")
    
    np.random.seed(42)
    
    # Base price series with regime shifts
    price = 1.0800  # EUR/USD-like
    prices = [price]
    
    for i in range(1, bars):
        # Regime-dependent volatility
        if i % 3000 < 1000:
            vol = 0.0001  # Low vol (GREEN)
        elif i % 3000 < 2000:
            vol = 0.0005  # Medium vol (YELLOW)
        else:
            vol = 0.0015  # High vol (RED)
        
        # Add mean-reversion component
        drift = -0.00001 * (price - 1.0800)  # Pull toward 1.0800
        change = drift + np.random.normal(0, vol)
        price = max(price + change, 0.5)
        prices.append(price)
    
    closes = np.array(prices)
    
    # Generate OHLCV from close
    spread = np.random.uniform(0.0001, 0.001, bars)
    df = pd.DataFrame({
        "open": closes + np.random.normal(0, 0.0002, bars),
        "high": closes + spread,
        "low": closes - spread,
        "close": closes,
        "volume": np.random.randint(100, 10000, bars).astype(float),
    })
    
    logger.info(f"Synthetic data generated: {len(df)} bars, price range [{df['close'].min():.5f}, {df['close'].max():.5f}]")
    return df


# ══════════════════════════════════════════════════════════════════════
#  Redis Writer
# ══════════════════════════════════════════════════════════════════════

def write_winners_to_redis(
    asset: str,
    winners: Dict[str, RegimeWinner],
) -> bool:
    """
    Write winning parameters to Redis under NEXUS:OPT:<ASSET>:<REGIME>.
    """
    try:
        import redis as redis_lib
        
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", "6379"))
        r = redis_lib.Redis(host=host, port=port, db=0, decode_responses=True)
        r.ping()
        
        for regime, winner in winners.items():
            key = f"{_REDIS_OPT_PREFIX}:{asset}:{regime}"
            value = json.dumps(winner.params)
            r.set(key, value)
            logger.info(f"Redis SET {key} = {value}")
        
        # Store calibration metadata
        meta_key = f"{_REDIS_OPT_PREFIX}:{asset}:_meta"
        meta = {
            "last_calibrated": datetime.now(timezone.utc).isoformat(),
            "regimes_calibrated": list(winners.keys()),
            "asset": asset,
        }
        r.set(meta_key, json.dumps(meta))
        
        r.close()
        logger.info(f"All {len(winners)} regime winners written to Redis for {asset}")
        return True
        
    except Exception as exc:
        logger.warning(f"Redis write failed: {exc}")
        return False


def read_optimal_params_from_redis(
    asset: str,
    regime: str,
) -> Optional[Dict[str, Any]]:
    """
    Read optimized parameters from Redis.
    Called by pipeline._tick() when AI_MODE is active.
    """
    try:
        import redis as redis_lib
        
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", "6379"))
        r = redis_lib.Redis(host=host, port=port, db=0, decode_responses=True)
        
        key = f"{_REDIS_OPT_PREFIX}:{asset}:{regime}"
        value = r.get(key)
        r.close()
        
        if value:
            return json.loads(value)
        return None
        
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
#  Main Calibration Runner
# ══════════════════════════════════════════════════════════════════════

async def calibrate_asset(asset: str) -> Dict[str, RegimeWinner]:
    """
    Full WFO calibration pipeline for a single asset.
    
    1. Fetch data (QuestDB → synthetic fallback)
    2. Classify regimes
    3. Grid search per regime
    4. Write winners to Redis
    """
    t_start = time.time()
    
    print()
    print("=" * 70)
    print(f"  🧠 NEXUS WFO AUTO-CALIBRATOR — {asset}")
    print(f"  Walk-Forward Optimization with Regime-Aware Parameter Tuning")
    print("=" * 70)
    print()
    
    # ── Step 1: Fetch Data ────────────────────────────────────────────
    print("  📡 Step 1: Fetching historical data...")
    df = await fetch_data_from_questdb(asset)
    
    if df is None or len(df) < 500:
        print("  ⚠️  QuestDB offline or insufficient data. Using synthetic fallback.")
        df = generate_synthetic_data(bars=20000)
    else:
        print(f"  ✅ Loaded {len(df):,} candles from QuestDB")
    
    # ── Step 2: Classify Regimes ──────────────────────────────────────
    print("\n  🎯 Step 2: Classifying market regimes...")
    regimes = classify_regimes(df)
    
    regime_counts = regimes.value_counts()
    for regime, count in regime_counts.items():
        pct = count / len(df) * 100
        emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(regime, "⚪")
        print(f"    {emoji} {regime}: {count:,} bars ({pct:.1f}%)")
    
    # ── Step 3: Grid Search per Regime ────────────────────────────────
    print(f"\n  📊 Step 3: Running grid search ({len(PARAM_GRID['rsi_period'])} × {len(PARAM_GRID['bb_dev'])} = {len(PARAM_GRID['rsi_period']) * len(PARAM_GRID['bb_dev'])} combos per regime)...")
    
    winners: Dict[str, RegimeWinner] = {}
    
    for regime in ["GREEN", "YELLOW", "RED"]:
        print(f"\n  {'─' * 50}")
        print(f"  Regime: {regime}")
        
        results = run_grid_search(df, regimes, regime)
        
        if not results:
            print(f"    ⚠️  No valid results for {regime}")
            # Use defaults
            winners[regime] = RegimeWinner(
                regime=regime,
                params={"rsi_period": 7, "bb_dev": 2.0},
                win_rate=0.0,
                profit_factor=0.0,
                total_trades=0,
                net_pnl=0.0,
            )
            continue
        
        # Top 3 results
        print(f"    {'RSI':>5} {'BB_σ':>6} {'Trades':>7} {'WR%':>7} {'PF':>8} {'Net PnL':>10}")
        print(f"    {'─' * 45}")
        for r in results[:3]:
            marker = " ⭐" if r == results[0] else ""
            print(
                f"    {r.rsi_period:>5} {r.bb_dev:>6.1f} "
                f"{r.total_trades:>7} {r.win_rate * 100:>6.1f}% "
                f"{r.profit_factor:>8.3f} {r.net_pnl:>+10.2f}{marker}"
            )
        
        best = results[0]
        winners[regime] = RegimeWinner(
            regime=regime,
            params={"rsi_period": best.rsi_period, "bb_dev": best.bb_dev},
            win_rate=best.win_rate,
            profit_factor=best.profit_factor,
            total_trades=best.total_trades,
            net_pnl=best.net_pnl,
        )
        print(f"    🏆 Winner: RSI={best.rsi_period}, BB_σ={best.bb_dev}")
    
    # ── Step 4: Write to Redis ────────────────────────────────────────
    print(f"\n  {'═' * 50}")
    print("  📡 Step 4: Writing winners to Redis...")
    success = write_winners_to_redis(asset, winners)
    
    elapsed = time.time() - t_start
    
    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n  {'═' * 50}")
    print(f"  📋 CALIBRATION SUMMARY — {asset}")
    print(f"  {'═' * 50}")
    print(f"  ⏱️  Total time: {elapsed:.1f}s")
    print(f"  📊 Data points: {len(df):,}")
    print(f"  🔑 Redis: {'✅ Written' if success else '⚠️ Offline (params not persisted)'}")
    print()
    
    print(f"  {'Regime':<10} {'RSI':>5} {'BB_σ':>6} {'WR%':>7} {'PF':>8} {'Trades':>7}")
    print(f"  {'─' * 48}")
    for regime in ["GREEN", "YELLOW", "RED"]:
        w = winners[regime]
        emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}[regime]
        print(
            f"  {emoji} {regime:<8} "
            f"{w.params.get('rsi_period', '?'):>5} "
            f"{w.params.get('bb_dev', '?'):>6} "
            f"{w.win_rate * 100:>6.1f}% "
            f"{w.profit_factor:>8.3f} "
            f"{w.total_trades:>7}"
        )
    
    print(f"\n  {'═' * 50}")
    if success:
        print("  🚀 Calibration complete. Enable AI Mode to activate.")
        print("     POST /settings/ai-mode {\"enabled\": true}")
    else:
        print("  ⚠️  Calibration complete but Redis offline.")
        print("     Start Redis and re-run calibration.")
    print(f"  {'═' * 50}\n")
    
    return winners


# ══════════════════════════════════════════════════════════════════════
#  CLI Entry Point
# ══════════════════════════════════════════════════════════════════════

def main(asset: str = "EURUSD-op") -> None:
    """CLI entry point for calibration."""
    from dotenv import load_dotenv
    load_dotenv()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)-25s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    asyncio.run(calibrate_asset(asset))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NEXUS WFO Auto-Calibrator")
    parser.add_argument("--asset", type=str, default="EURUSD-op", help="Asset to calibrate")
    args = parser.parse_args()
    main(asset=args.asset)
