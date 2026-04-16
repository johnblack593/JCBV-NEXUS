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
    RANKEA candidatos con un threshold interno bajo (0.50).
    El pipeline aplica el threshold de ejecución real.
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
            
            # ✅ Extracción defensiva multicapa — explicit None checks
            # NUNCA usar `or` chain: 0.0 es falsy en Python y causa fallthrough
            atr = result.get("atr")
            if atr is None or (isinstance(atr, float) and atr <= 0):
                atr = result.get("indicators", {}).get("atr")
            if atr is None or (isinstance(atr, float) and atr <= 0):
                atr = result.get("metrics", {}).get("atr")
            if atr is None or (isinstance(atr, float) and atr <= 0):
                atr = result.get("technical", {}).get("atr")
            if atr is None or (isinstance(atr, float) and atr <= 0):
                atr = 0.001
            atr = float(atr)
            
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

    # ─── Threshold INTERNO del consensus (para rankear candidatos) ──
    # Este umbral es bajo intencionalmente: el consensus RANKEA, no AUTORIZA.
    # El threshold de EJECUCIÓN se aplica en el pipeline, NO aquí.
    INTERNAL_MIN_CONF = 0.50

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
        Evalúa todos los activos en paralelo y retorna el mejor candidato.

        El consensus RANKEA candidatos — NO decide si se ejecuta el trade.
        El threshold de ejecución (min_conf) se usa para logging/diagnóstico.
        La decisión final de ejecución la toma el pipeline.
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
        
        # Diagnostic: Log identical ATRs
        if len(results) >= 2:
            import itertools
            for a, b in itertools.combinations(results, 2):
                if a.atr == b.atr and a.atr > 0.0011: # Ignorar fallbacks default
                    logger.debug(
                        f"[diag] ATR coincidence: {a.symbol} and {b.symbol} "
                        f"share ATR={a.atr:.6f} (may be legitimate for correlated pairs)"
                    )
        
        # Log detallado con el threshold de ejecución del pipeline
        for r in results:
            conf_ok = r.confidence >= min_conf
            payout_ok = r.payout >= min_payout
            signal_ok = r.signal != "HOLD"
            atr_ok = r.atr >= atr_floor
            
            passes = signal_ok and conf_ok and payout_ok and atr_ok
            
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

        # ── Selección del mejor candidato (ranking interno) ──────────
        # Filtrar con INTERNAL_MIN_CONF (bajo): solo para descartar ruido total
        # El pipeline aplica su propio threshold de ejecución después
        candidates = [
            r for r in results
            if r.signal != "HOLD"
            and r.confidence >= self.INTERNAL_MIN_CONF
            and r.atr >= atr_floor
            and r.payout >= min_payout
        ]

        if not candidates:
            # Intentar con HOLD incluido (rf_prediction puede rescatarlos en pipeline)
            candidates = [
                r for r in results
                if r.confidence >= self.INTERNAL_MIN_CONF
                and r.atr >= atr_floor
                and r.payout >= min_payout
            ]

        if candidates:
            best = max(candidates, key=lambda s: s.composite)

            # Log transparente: indicar si el candidato pasa o no el threshold de ejecución
            if best.confidence < min_conf:
                logger.debug(
                    f"[consensus interno] {best.symbol} seleccionado con conf={best.confidence:.3f} "
                    f"(ranking interno, threshold ejecución {min_conf:.2f} se aplica en pipeline)"
                )
            
            logger.info(
                f"✅ Consensus: MEJOR={best.symbol} | "
                f"conf={best.confidence:.1%} | signal={best.signal} | "
                f"composite={best.composite:.3f} | "
                f"candidatos={len(candidates)}/{len(watchlist)}"
            )
            return best
                
        # Sin candidatos viables
        best_overall = max(results, key=lambda s: s.confidence) if results else None
        if best_overall and best_overall.confidence > 0:
            logger.info(
                f"📊 MERCADO SIN SETUP: mejor conf disponible fue {best_overall.confidence:.3f} "
                f"en {best_overall.symbol} — esperando próximo tick"
            )
        else:
            logger.debug(
                f"ConsensusEngine: 0/{len(watchlist)} activos pasaron filtros "
                f"(internal_min={self.INTERNAL_MIN_CONF:.0%}, min_payout={min_payout:.0f}%)"
            )
            
        return None

