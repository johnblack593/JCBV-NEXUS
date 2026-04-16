"""
ConsensusEngine — NEXUS v5.0
Evaluación paralela de múltiples activos con ranking por score.
No modifica RiskManager, SignalEngine ni OpportunityAgent.
Se invoca desde pipeline._tick() como capa de selección previa.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

import pandas as pd

logger = logging.getLogger("nexus.consensus")


@dataclass
class AssetScore:
    symbol: str
    confidence: float
    signal: str          # "BUY" | "SELL" | "HOLD"
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
    Implementa fallback de cascada 62% -> 55% -> 50%.
    """

    def __init__(self) -> None:
        self._cache: dict[str, AssetScore] = {}

    async def _evaluate_single(
        self,
        symbol: str,
        get_data_fn,
        analyze_fn,
    ) -> Optional[AssetScore]:
        """Evalúa un único activo y devuelve su raw score (sin filtrar)."""
        try:
            df = await get_data_fn(symbol)
            if df is None or df.empty or len(df) < 30:
                return None

            result = await analyze_fn(df)
            signal = result.get("signal", "HOLD")
            confidence = result.get("confidence", 0.0)
            
            atr = result.get("indicators", {}).get("atr", 0.0)
            payout = result.get("payout", 80.0)

            # Score compuesto: 60% confianza + 25% payout normalizado + 15% ATR
            norm_payout = min(payout / 100.0, 1.0)
            norm_atr = min(atr / 0.001, 1.0)  # normalizar ATR forex OTC
            composite = (confidence * 0.60) + (norm_payout * 0.25) + (norm_atr * 0.15) if signal != "HOLD" else 0.0

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
        Evalúa todos los activos en paralelo con threshold en cascada.
        Intento 1: min_conf
        Intento 2: 0.55
        Intento 3: 0.50 (solo para loguear)
        """
        if not watchlist:
            return None

        tasks = [
            self._evaluate_single(symbol, get_data_fn, analyze_fn)
            for symbol in watchlist
        ]

        # Evaluación paralela — todos los activos al mismo tiempo
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[AssetScore] = [
            r for r in raw_results
            if isinstance(r, AssetScore) and not r.is_expired
        ]
        
        # Guardar en log detallado con el min_conf principal
        for r in results:
            passes = (r.signal != "HOLD" and r.confidence >= min_conf and 
                      r.atr >= atr_floor and r.payout >= min_payout)
            logger.debug(
                f"🔍 {r.symbol}: conf={r.confidence:.3f} "
                f"(req={min_conf}) payout={r.payout:.1f}% "
                f"(req={min_payout}%) → {'✅ PASA' if passes else '❌ FILTRADO'}"
            )

        # Fallback de confianza
        thresholds = [min_conf, 0.55, 0.50]
        
        for attempt, thresh in enumerate(thresholds):
            valid = [
                r for r in results
                if r.signal != "HOLD" and r.confidence >= thresh 
                and r.atr >= atr_floor and r.payout >= min_payout
            ]
            
            if valid:
                if thresh == 0.50:
                    # El nivel 50% es solo diagnóstico, NUNCA opera.
                    best = max(valid, key=lambda s: s.confidence)
                    logger.info(
                        f"📊 MERCADO SIN SETUP: mejor conf disponible fue {best.confidence:.3f} "
                        f"en {best.symbol} — esperando próximo tick"
                    )
                    return None
                    
                best = max(valid, key=lambda s: s.composite)
                if attempt > 0:
                    logger.info(f"⚠️ Alerta: Consensus validó {best.symbol} usando fallback de confianza {thresh:.2f}")
                    
                logger.info(
                    f"✅ Consensus: MEJOR={best.symbol} | "
                    f"conf={best.confidence:.1%} | signal={best.signal} | "
                    f"composite={best.composite:.3f} | "
                    f"candidatos={len(valid)}/{len(watchlist)}"
                )
                return best
                
        # Si llegamos aquí ni con 0.50 hubo setup
        best_overall = max(results, key=lambda s: s.confidence) if results else None
        if best_overall and best_overall.confidence > 0:
            logger.info(
                f"📊 MERCADO SIN SETUP: mejor conf disponible fue {best_overall.confidence:.3f} "
                f"en {best_overall.symbol} — esperando próximo tick"
            )
        else:
            logger.debug(
                f"ConsensusEngine: 0/{len(watchlist)} activos pasaron filtros "
                f"(min_conf={min_conf:.0%}, min_payout={min_payout:.0f}%)"
            )
            
        return None
