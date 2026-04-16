"""
ConsensusEngine — NEXUS v5.0
Evaluación paralela de múltiples activos con ranking por score.
No modifica RiskManager, SignalEngine ni OpportunityAgent.
Se invoca desde pipeline._tick() como capa de selección previa.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

logger = logging.getLogger("nexus.consensus")


@dataclass
class AssetScore:
    symbol: str
    confidence: float
    signal: str          # "BUY" | "SELL"
    payout: float
    atr: float
    composite: float     # score final para ranking
    timestamp: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        """Señal válida solo por 45 segundos (Signal Freshness TTL)."""
        return (time.time() - self.timestamp) > 45.0


class ConsensusEngine:
    """
    Evalúa en paralelo todos los activos de la watchlist.
    Devuelve el mejor AssetScore o None si ninguno pasa los filtros.

    Filtros en cascada (ninguno modifica los agentes existentes):
      1. Señal no es HOLD
      2. Confianza >= umbral dinámico del régimen actual
      3. ATR > atr_floor (volatilidad mínima)
      4. Payout >= min_payout
      5. Señal fresca (< 45s desde cálculo)

    Uso desde pipeline._tick():
      best = await self.consensus_engine.evaluate(
          watchlist, regime, get_market_data_fn, analyze_fn,
          min_conf, min_payout, atr_floor
      )
      if best is None: return
      # usar best.symbol, best.signal, best.confidence
    """

    def __init__(self) -> None:
        self._cache: dict[str, AssetScore] = {}

    async def _evaluate_single(
        self,
        symbol: str,
        get_data_fn,
        analyze_fn,
        min_conf: float,
        min_payout: float,
        atr_floor: float,
    ) -> Optional[AssetScore]:
        """Evalúa un único activo. Retorna None si no pasa filtros."""
        try:
            df: Optional[pd.DataFrame] = await get_data_fn(symbol)
            if df is None or df.empty or len(df) < 30:
                return None

            result = await analyze_fn(df)
            signal = result.get("signal", "HOLD")
            confidence = result.get("confidence", 0.0)

            if signal == "HOLD" or confidence < min_conf:
                return None

            atr = result.get("indicators", {}).get("atr", 0.0)
            if atr < atr_floor:
                logger.debug(f"[{symbol}] ATR {atr:.5f} < floor {atr_floor:.5f} — skip")
                return None

            # Payout desde cache OpportunityAgent o fallback 80%
            payout = result.get("payout", 80.0)
            if payout < min_payout:
                return None

            # Score compuesto: 60% confianza + 25% payout normalizado + 15% ATR
            norm_payout = min(payout / 100.0, 1.0)
            norm_atr = min(atr / 0.001, 1.0)  # normalizar ATR forex OTC
            composite = (confidence * 0.60) + (norm_payout * 0.25) + (norm_atr * 0.15)

            score = AssetScore(
                symbol=symbol,
                confidence=confidence,
                signal=signal,
                payout=payout,
                atr=atr,
                composite=composite,
            )
            self._cache[symbol] = score
            return score

        except Exception as e:
            logger.debug(f"[{symbol}] ConsensusEngine error: {e}")
            return None

    async def evaluate(
        self,
        watchlist: list[str],
        get_data_fn,
        analyze_fn,
        min_conf: float = 0.62,
        min_payout: float = 80.0,
        atr_floor: float = 0.00005,
    ) -> Optional[AssetScore]:
        """
        Evalúa todos los activos en paralelo.
        Retorna el de mayor score compuesto, o None si ninguno pasa.
        """
        if not watchlist:
            return None

        tasks = [
            self._evaluate_single(
                symbol, get_data_fn, analyze_fn,
                min_conf, min_payout, atr_floor
            )
            for symbol in watchlist
        ]

        # Evaluación paralela — todos los activos al mismo tiempo
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid: list[AssetScore] = [
            r for r in results
            if isinstance(r, AssetScore) and not r.is_expired
        ]

        if not valid:
            logger.debug(
                f"ConsensusEngine: 0/{len(watchlist)} activos pasaron filtros "
                f"(min_conf={min_conf:.0%}, min_payout={min_payout:.0f}%)"
            )
            return None

        best = max(valid, key=lambda s: s.composite)
        logger.info(
            f"✅ Consensus: MEJOR={best.symbol} | "
            f"conf={best.confidence:.1%} | signal={best.signal} | "
            f"composite={best.composite:.3f} | "
            f"candidatos={len(valid)}/{len(watchlist)}"
        )
        return best
