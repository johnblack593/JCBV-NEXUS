"""
NEXUS v4.0 — Crypto Quant Scalp Strategy (Binance Exclusive)
==============================================================
Estrategia de Mean-Reversion estadística para scalping en Crypto.

Arquitectura:
    - Z-Score del precio vs SMA 50 períodos.
    - Señal BUY: Z-Score < -2.5 (precio significativamente por debajo de la media).
    - Señal SELL: Z-Score > +2.5 (precio significativamente por encima de la media).
    - Active Trade Management dinámico basado en ATR:
        • Stop Loss duro:         2x ATR
        • Take Profit:            None (dejamos correr — trailing protect profits)
        • Breakeven Trigger:      1x ATR a favor
        • Trailing Activation:    1.5x ATR a favor
        • Trailing Callback:      0.5%
        • Time Exit:              60 minutos

Diseñada para Binance Spot/Perps scalping (5m candles, ~1h hold).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import numpy as np  # type: ignore
import pandas as pd  # type: ignore

from .base import BaseStrategy

logger = logging.getLogger("nexus.strategies.crypto_quant_scalp")


class CryptoQuantScalpStrategy(BaseStrategy):
    """
    Statistical Mean-Reversion Scalper para Crypto (Binance).

    Principio: Los precios revierten a la media. Cuando el Z-Score
    del precio vs SMA(50) supera ±2.5 desviaciones estándar,
    la probabilidad de reversión estadística es significativa.

    El trade management dinámico (basado en ATR) reemplaza los TP/SL
    rígidos por un sistema de protección adaptativo:
        - El trailing stop protege ganancias sin capear el upside.
        - El breakeven elimina riesgo cuando el trade se mueve a favor.
        - El time exit evita capital muerto en posiciones sin resolución.
    """

    SMA_PERIOD: int = 50        # Período de la media móvil
    ZSCORE_THRESHOLD: float = 2.5  # Umbral Z-Score para señal
    ATR_PERIOD: int = 14        # Período ATR
    SL_ATR_MULT: float = 2.0   # Stop Loss = 2x ATR
    BE_ATR_MULT: float = 1.0   # Breakeven trigger = 1x ATR a favor
    TS_ATR_MULT: float = 1.5   # Trailing stop activation = 1.5x ATR a favor
    TS_CALLBACK_PCT: float = 0.5  # Trailing stop callback %
    TIME_EXIT_MINUTES: int = 60  # Max hold time

    async def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Analiza OHLCV, calcula Z-Score vs SMA(50) y genera señal
        con parámetros de Active Trade Management.

        Returns:
            Dict con signal + stop_loss, breakeven_trigger,
            trailing_stop_activation, trailing_stop_callback_pct,
            time_exit_minutes.
        """
        if df is None or len(df) < self.SMA_PERIOD + 10:
            return self._hold("Datos insuficientes para Z-Score analysis")

        try:
            close = df["close"].astype(float)
            current_price = float(close.iloc[-1])

            # ── SMA & Z-Score ────────────────────────────────────────
            sma = close.rolling(window=self.SMA_PERIOD).mean()
            std = close.rolling(window=self.SMA_PERIOD).std()

            current_sma = float(sma.iloc[-1])
            current_std = float(std.iloc[-1])

            if current_std < 1e-10:
                return self._hold("Desviación estándar ≈ 0 (mercado flat)")

            zscore = (current_price - current_sma) / current_std

            # ── ATR para Trade Management ────────────────────────────
            atr = self._calculate_atr(df)

            if atr is None or atr <= 0:
                return self._hold("ATR no calculable")

            # ── Generación de Señal ──────────────────────────────────
            signal_result = self._evaluate_zscore(
                zscore=zscore,
                current_price=current_price,
                current_sma=current_sma,
                atr=atr,
            )

            return signal_result

        except Exception as exc:
            logger.error("CryptoQuantScalp analyze error: %s", exc, exc_info=True)
            return self._hold(f"Error en análisis: {exc}")

    # ══════════════════════════════════════════════════════════════════
    #  Signal Evaluation
    # ══════════════════════════════════════════════════════════════════

    def _evaluate_zscore(
        self,
        zscore: float,
        current_price: float,
        current_sma: float,
        atr: float,
    ) -> Dict[str, Any]:
        """
        Evalúa el Z-Score y genera señal con trade management dinámico.
        """
        # Confidence: map |zscore| linearly from threshold to ~4.0 → 0.55 to 0.95
        raw_confidence = min(0.95, 0.55 + (abs(zscore) - self.ZSCORE_THRESHOLD) * 0.15)

        indicators = {
            "zscore": round(zscore, 4),
            "sma_50": round(current_sma, 6),
            "current_price": round(current_price, 6),
            "atr": round(atr, 6),
            "deviation_pct": round((current_price - current_sma) / current_sma * 100, 2),
        }

        # ── MEAN-REVERSION BUY: Z-Score muy negativo ─────────────────
        if zscore < -self.ZSCORE_THRESHOLD:
            stop_loss = current_price - (atr * self.SL_ATR_MULT)
            breakeven_trigger = current_price + (atr * self.BE_ATR_MULT)
            trailing_activation = current_price + (atr * self.TS_ATR_MULT)

            return {
                "signal": "BUY",
                "confidence": round(raw_confidence, 4),
                "reason": (
                    f"Z-Score={zscore:.2f} < -{self.ZSCORE_THRESHOLD} | "
                    f"Price={current_price:.4f} << SMA50={current_sma:.4f} | "
                    f"Mean-Reversion BUY | ATR={atr:.6f}"
                ),
                "indicators": indicators,
                # ── Active Trade Management ──────────────────────────
                "stop_loss": round(stop_loss, 6),
                "take_profit": None,  # Dejamos correr — trailing protects
                "breakeven_trigger": round(breakeven_trigger, 6),
                "trailing_stop_activation": round(trailing_activation, 6),
                "trailing_stop_callback_pct": self.TS_CALLBACK_PCT,
                "time_exit_minutes": self.TIME_EXIT_MINUTES,
            }

        # ── MEAN-REVERSION SELL: Z-Score muy positivo ────────────────
        elif zscore > self.ZSCORE_THRESHOLD:
            stop_loss = current_price + (atr * self.SL_ATR_MULT)
            breakeven_trigger = current_price - (atr * self.BE_ATR_MULT)
            trailing_activation = current_price - (atr * self.TS_ATR_MULT)

            return {
                "signal": "SELL",
                "confidence": round(raw_confidence, 4),
                "reason": (
                    f"Z-Score={zscore:.2f} > +{self.ZSCORE_THRESHOLD} | "
                    f"Price={current_price:.4f} >> SMA50={current_sma:.4f} | "
                    f"Mean-Reversion SELL | ATR={atr:.6f}"
                ),
                "indicators": indicators,
                # ── Active Trade Management ──────────────────────────
                "stop_loss": round(stop_loss, 6),
                "take_profit": None,
                "breakeven_trigger": round(breakeven_trigger, 6),
                "trailing_stop_activation": round(trailing_activation, 6),
                "trailing_stop_callback_pct": self.TS_CALLBACK_PCT,
                "time_exit_minutes": self.TIME_EXIT_MINUTES,
            }

        # ── NO SIGNAL ────────────────────────────────────────────────
        else:
            return self._hold(
                f"Z-Score={zscore:.2f} dentro de rango "
                f"[-{self.ZSCORE_THRESHOLD}, +{self.ZSCORE_THRESHOLD}]"
            )

    # ══════════════════════════════════════════════════════════════════
    #  ATR Calculation
    # ══════════════════════════════════════════════════════════════════

    def _calculate_atr(self, df: pd.DataFrame) -> Optional[float]:
        """Calcula ATR(14) sin dependencia de librería 'ta'."""
        if len(df) < self.ATR_PERIOD + 1:
            return None

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        # True Range
        high_low = high - low
        high_prev_close = (high - close.shift(1)).abs()
        low_prev_close = (low - close.shift(1)).abs()
        true_range = pd.concat(
            [high_low, high_prev_close, low_prev_close], axis=1
        ).max(axis=1)

        atr = float(true_range.rolling(window=self.ATR_PERIOD).mean().iloc[-1])

        if np.isnan(atr):
            return None

        return atr

    # ══════════════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════════════

    def _hold(self, reason: str) -> Dict[str, Any]:
        """Retorna señal HOLD estandarizada."""
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": reason,
            "indicators": {},
        }
