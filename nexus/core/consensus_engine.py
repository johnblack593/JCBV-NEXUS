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
    extra: dict = field(default_factory=dict)
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
        real_payouts: dict[str, float] = None
    ) -> Optional[AssetScore]:
        """Evalúa un único activo y devuelve su raw score (sin filtrar)."""
        try:
            df = await get_data_fn(symbol)
            if df is None or df.empty or len(df) < 30:
                return None

            result = await analyze_fn(df)
            signal = result.get("signal", "HOLD")
            confidence = result.get("confidence", 0.0)
            
            # ✅ Extracción defensiva multicapa con fallback seguro
            atr = (
                result.get("atr")
                or result.get("indicators", {}).get("atr")
                or result.get("metrics", {}).get("atr")
                or result.get("technical", {}).get("atr")
                or 0.001
            )
            
            # Payout de real_payouts si existe, sino fallback a result o 80.0
            payout = 80.0
            if real_payouts and symbol in real_payouts:
                payout = real_payouts[symbol]
            else:
                payout = result.get("payout", 80.0)

            logger.debug(
                f"[{symbol}] ATR extraído={atr:.6f} | "
                f"fuente={'real_payouts' if real_payouts and symbol in real_payouts else 'analyze_fn'}"
            )

            # Score compuesto: 60% confianza + 25% payout normalizado + 15% ATR
            norm_payout = min(payout / 100.0, 1.0)
            if atr > 1.0:
                # Activo de precio alto (índices OTC, commodities): escala diferente
                norm_atr = min(atr / 10.0, 1.0)
            else:
                # Activo Forex / cripto de precio bajo: escala original
                norm_atr = min(atr / 0.001, 1.0)
                
            # HOLD recibe composite reducido (no cero) para mantener ranking
            composite = (confidence * 0.60) + (norm_payout * 0.25) + (norm_atr * 0.15)
            if signal == "HOLD":
                composite *= 0.5  # penalización del 50%, pero no eliminación

            score = AssetScore(
                symbol=symbol,
                confidence=confidence,
                signal=signal,
                payout=payout,
                atr=atr,
                composite=composite,
                extra=result.get("indicators", {})
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
        real_payouts: dict[str, float] = None,
        min_conf: float = 0.62,
        min_payout: float = 80.0,
        atr_floor: float = 0.0001,
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
            self._evaluate_single(symbol, get_data_fn, analyze_fn, real_payouts)
            for symbol in watchlist
        ]

        # Evaluación paralela — todos los activos al mismo tiempo
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[AssetScore] = [
            r for r in raw_results
            if isinstance(r, AssetScore) and not r.is_expired
        ]
        
        # Diagnostic Fix 3: Log identical ATRs (Possible state leak)
        if len(results) >= 2:
            import itertools
            for a, b in itertools.combinations(results, 2):
                if a.atr == b.atr and a.atr > 0.0011: # Ignorar fallbacks default
                    logger.warning(
                        f"🚨 ATR LEAK? {a.symbol} and {b.symbol} "
                        f"share identical ATR: {a.atr}. Possible state contamination."
                    )
        
        # Guardar en log detallado con el min_conf principal
        for r in results:
            conf_ok = r.confidence >= min_conf
            payout_ok = r.payout >= min_payout
            signal_ok = r.signal != "HOLD"
            atr_ok = r.atr >= atr_floor
            
            passes = signal_ok and conf_ok and payout_ok and atr_ok
            
            # Log exhaustivo
            logger.debug(
                f"🔬 {r.symbol}: "
                f"conf={r.confidence:.3f}≥{min_conf:.2f}={'✅' if conf_ok else '❌'} | "
                f"payout={r.payout:.1f}%≥{min_payout:.0f}%={'✅' if payout_ok else '❌'}"
            )
            
            motivo = ""
            if not passes:
                if not signal_ok:
                    motivo = " [motivo: HOLD]"
                elif not conf_ok:
                    motivo = " [motivo: conf]"
                elif not payout_ok:
                    motivo = " [motivo: payout]"
                elif not atr_ok:
                    motivo = " [motivo: atr_floor]"

            logger.debug(
                f"🔍 {r.symbol}: conf={r.confidence:.3f} payout={r.payout:.1f}% "
                f"→ {'✅ PASA' if passes else '❌ FILTRADO'}{motivo}"
            )

        # Fallback de confianza
        thresholds = [min_conf, 0.55, 0.50]
        
        for attempt, thresh in enumerate(thresholds):
            # En intentos de fallback (attempt > 0), incluir señales HOLD
            # para no desperdiciar setups válidos por señal neutral
            exclude_hold = (attempt == 0)  # Solo primer intento filtra HOLD
            valid = [
                r for r in results
                if (r.signal != "HOLD" or not exclude_hold)
                and r.confidence >= thresh
                and r.atr >= atr_floor
                and r.payout >= min_payout
            ]
            
            if valid:
                if thresh == 0.50 and min_conf > 0.50:
                    # Solo es diagnóstico si min_conf original era mayor
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
