"""
NEXUS Trading System — Technical Signal Engine
=================================================
Motor de análisis técnico con 5 indicadores:
RSI, MACD, Bollinger Bands, EMA Cross, Volume Profile.

Emite señal BUY/SELL solo si ≥ 3 de 5 indicadores coinciden.
Usa la librería 'ta' (Technical Analysis) sobre DataFrames OHLCV.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np  # type: ignore
import pandas as pd  # type: ignore
from ta.momentum import RSIIndicator  # type: ignore
from ta.trend import MACD, EMAIndicator  # type: ignore
from ta.volatility import BollingerBands  # type: ignore

logger = logging.getLogger("nexus.signal_engine")


# ══════════════════════════════════════════════════════════════════════
#  Enums & Data Classes
# ══════════════════════════════════════════════════════════════════════

class SignalDirection(Enum):
    """Dirección de la señal técnica."""
    STRONG_BUY = 2
    BUY = 1
    NEUTRAL = 0
    SELL = -1
    STRONG_SELL = -2


@dataclass
class IndicatorResult:
    """Resultado de un indicador individual."""
    name: str
    direction: SignalDirection
    value: float
    detail: str           # Descripción corta del porqué


@dataclass
class SignalOutput:
    """
    Resultado final de generate_signal().

    Attributes:
        signal:     "BUY" | "SELL" | "HOLD"
        confidence: 0.0 – 1.0
        reason:     Indicadores que dispararon la señal
        timestamp:  UTC timestamp de la generación
        indicators: Detalle de cada indicador
    """
    signal: str
    confidence: float
    reason: str
    timestamp: datetime
    indicators: Dict[str, IndicatorResult] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════
#  Calculadores individuales
# ══════════════════════════════════════════════════════════════════════

class RSICalculator:
    """RSI — Relative Strength Index (periodo 14)."""

    OVERBOUGHT = 70
    OVERSOLD = 30

    def __init__(self, period: int = 14) -> None:
        self.period = period

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """Calcula la serie RSI sobre la columna 'close'."""
        indicator = RSIIndicator(close=df["close"], window=self.period)
        return indicator.rsi()

    def evaluate(self, df: pd.DataFrame) -> IndicatorResult:
        """Evalúa la señal RSI sobre la última vela."""
        rsi_series = self.calculate(df)
        current_rsi = rsi_series.iloc[-1]

        if np.isnan(current_rsi):
            return IndicatorResult(
                name="RSI",
                direction=SignalDirection.NEUTRAL,
                value=0.0,
                detail="RSI sin datos suficientes",
            )

        if current_rsi < self.OVERSOLD:
            direction = SignalDirection.BUY
            detail = f"RSI({self.period})={current_rsi:.1f} < {self.OVERSOLD} → sobreventa"
        elif current_rsi > self.OVERBOUGHT:
            direction = SignalDirection.SELL
            detail = f"RSI({self.period})={current_rsi:.1f} > {self.OVERBOUGHT} → sobrecompra"
        else:
            direction = SignalDirection.NEUTRAL
            detail = f"RSI({self.period})={current_rsi:.1f} → zona neutral"

        return IndicatorResult(
            name="RSI",
            direction=direction,
            value=round(float(current_rsi), 2),  # type: ignore
            detail=detail,
        )


class MACDCalculator:
    """MACD — Moving Average Convergence Divergence (12, 26, 9)."""

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> None:
        self.fast = fast
        self.slow = slow
        self.signal = signal

    def calculate(
        self, df: pd.DataFrame
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Retorna (macd_line, signal_line, histogram)."""
        indicator = MACD(
            close=df["close"],
            window_slow=self.slow,
            window_fast=self.fast,
            window_sign=self.signal,
        )
        return indicator.macd(), indicator.macd_signal(), indicator.macd_diff()

    def evaluate(self, df: pd.DataFrame) -> IndicatorResult:
        """Detecta cruce MACD / línea de señal."""
        macd_line, signal_line, histogram = self.calculate(df)

        if len(histogram) < 2 or np.isnan(histogram.iloc[-1]):
            return IndicatorResult(
                name="MACD",
                direction=SignalDirection.NEUTRAL,
                value=0.0,
                detail="MACD sin datos suficientes",
            )

        curr_hist = histogram.iloc[-1]
        prev_hist = histogram.iloc[-2]
        curr_macd = macd_line.iloc[-1]

        # Cruce alcista: histograma pasa de negativo a positivo
        if prev_hist <= 0 < curr_hist:
            direction = SignalDirection.BUY
            detail = f"MACD cruce alcista (hist {prev_hist:.4f} → {curr_hist:.4f})"
        # Cruce bajista: histograma pasa de positivo a negativo
        elif prev_hist >= 0 > curr_hist:
            direction = SignalDirection.SELL
            detail = f"MACD cruce bajista (hist {prev_hist:.4f} → {curr_hist:.4f})"
        # Sin cruce pero con tendencia
        elif curr_hist > 0:
            direction = SignalDirection.NEUTRAL  # tendencia alcista pero sin cruce
            detail = f"MACD positivo ({curr_hist:.4f}), sin cruce reciente"
        elif curr_hist < 0:
            direction = SignalDirection.NEUTRAL
            detail = f"MACD negativo ({curr_hist:.4f}), sin cruce reciente"
        else:
            direction = SignalDirection.NEUTRAL
            detail = "MACD neutral"

        return IndicatorResult(
            name="MACD",
            direction=direction,
            value=round(float(curr_macd), 4),  # type: ignore
            detail=detail,
        )


class BollingerCalculator:
    """Bandas de Bollinger (20 periodos, 2 desviaciones estándar)."""

    def __init__(self, period: int = 20, std_dev: float = 2.0) -> None:
        self.period = period
        self.std_dev = std_dev

    def calculate(
        self, df: pd.DataFrame
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Retorna (upper_band, middle_band, lower_band)."""
        indicator = BollingerBands(
            close=df["close"],
            window=self.period,
            window_dev=self.std_dev,
        )
        return (
            indicator.bollinger_hband(),
            indicator.bollinger_mavg(),
            indicator.bollinger_lband(),
        )

    def evaluate(self, df: pd.DataFrame) -> IndicatorResult:
        """Señal por toque o penetración de banda extrema."""
        upper, middle, lower = self.calculate(df)
        close = df["close"].iloc[-1]

        if np.isnan(upper.iloc[-1]) or np.isnan(lower.iloc[-1]):
            return IndicatorResult(
                name="Bollinger",
                direction=SignalDirection.NEUTRAL,
                value=0.0,
                detail="Bollinger sin datos suficientes",
            )

        upper_val = upper.iloc[-1]
        lower_val = lower.iloc[-1]
        middle_val = middle.iloc[-1]

        # %B: posición relativa del precio dentro de las bandas (0 = lower, 1 = upper)
        band_width = upper_val - lower_val
        pct_b = (close - lower_val) / band_width if band_width > 0 else 0.5

        if close <= lower_val:
            direction = SignalDirection.BUY
            detail = f"Precio ({close:.2f}) tocó banda inferior ({lower_val:.2f}) → rebote potencial"
        elif close >= upper_val:
            direction = SignalDirection.SELL
            detail = f"Precio ({close:.2f}) tocó banda superior ({upper_val:.2f}) → rechazo potencial"
        else:
            direction = SignalDirection.NEUTRAL
            detail = f"Precio dentro de bandas (%B={pct_b:.2f})"

        return IndicatorResult(
            name="Bollinger",
            direction=direction,
            value=round(float(pct_b), 4),  # type: ignore
            detail=detail,
        )


class EMACrossCalculator:
    """Cruce de EMA 50 vs EMA 200 — Golden Cross / Death Cross."""

    def __init__(self, fast_period: int = 50, slow_period: int = 200) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period

    def calculate(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        """Retorna (ema_fast, ema_slow)."""
        ema_fast = EMAIndicator(close=df["close"], window=self.fast_period).ema_indicator()
        ema_slow = EMAIndicator(close=df["close"], window=self.slow_period).ema_indicator()
        return ema_fast, ema_slow

    def evaluate(self, df: pd.DataFrame) -> IndicatorResult:
        """Detecta Golden Cross (BUY) o Death Cross (SELL)."""
        ema_fast, ema_slow = self.calculate(df)

        if len(ema_fast) < 2 or np.isnan(ema_fast.iloc[-1]) or np.isnan(ema_slow.iloc[-1]):
            return IndicatorResult(
                name="EMA_Cross",
                direction=SignalDirection.NEUTRAL,
                value=0.0,
                detail=f"EMA{self.fast_period}/{self.slow_period} sin datos suficientes (requiere {self.slow_period} periodos)",
            )

        curr_fast = ema_fast.iloc[-1]
        curr_slow = ema_slow.iloc[-1]
        prev_fast = ema_fast.iloc[-2]
        prev_slow = ema_slow.iloc[-2]

        # Spread relativo como valor numérico
        spread_pct = ((curr_fast - curr_slow) / curr_slow) * 100 if curr_slow != 0 else 0

        # Golden Cross: EMA 50 cruza por encima de EMA 200
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            direction = SignalDirection.BUY
            detail = f"Golden Cross — EMA{self.fast_period}({curr_fast:.2f}) cruzó EMA{self.slow_period}({curr_slow:.2f})"
        # Death Cross: EMA 50 cruza por debajo de EMA 200
        elif prev_fast >= prev_slow and curr_fast < curr_slow:
            direction = SignalDirection.SELL
            detail = f"Death Cross — EMA{self.fast_period}({curr_fast:.2f}) cruzó EMA{self.slow_period}({curr_slow:.2f})"
        # Ya por encima = tendencia alcista sin cruce reciente
        elif curr_fast > curr_slow:
            direction = SignalDirection.NEUTRAL
            detail = f"Tendencia alcista (EMA{self.fast_period} > EMA{self.slow_period}, spread={spread_pct:+.2f}%)"
        else:
            direction = SignalDirection.NEUTRAL
            detail = f"Tendencia bajista (EMA{self.fast_period} < EMA{self.slow_period}, spread={spread_pct:+.2f}%)"

        return IndicatorResult(
            name="EMA_Cross",
            direction=direction,
            value=round(float(spread_pct), 4),  # type: ignore
            detail=detail,
        )


class VolumeProfileCalculator:
    """Volume Profile — detección de volumen anómalo (> 2× promedio 20 periodos)."""

    def __init__(self, lookback: int = 20, anomaly_factor: float = 2.0) -> None:
        self.lookback = lookback
        self.anomaly_factor = anomaly_factor

    def evaluate(self, df: pd.DataFrame) -> IndicatorResult:
        """Detecta volumen anómalo y lo correlaciona con la dirección del precio."""
        if len(df) < self.lookback + 1:
            return IndicatorResult(
                name="Volume",
                direction=SignalDirection.NEUTRAL,
                value=0.0,
                detail=f"Volume sin datos suficientes (requiere {self.lookback + 1} periodos)",
            )

        current_volume = df["volume"].iloc[-1]
        avg_volume = df["volume"].iloc[-(self.lookback + 1):-1].mean()

        if avg_volume == 0:
            return IndicatorResult(
                name="Volume",
                direction=SignalDirection.NEUTRAL,
                value=0.0,
                detail="Volumen promedio es 0",
            )

        volume_ratio = current_volume / avg_volume

        if volume_ratio >= self.anomaly_factor:
            # Volumen anómalo: la dirección depende de la vela
            price_change = df["close"].iloc[-1] - df["open"].iloc[-1]

            if price_change > 0:
                direction = SignalDirection.BUY
                detail = (
                    f"Volumen anómalo alcista ({volume_ratio:.1f}× promedio) "
                    f"con vela verde (+{price_change:.2f})"
                )
            elif price_change < 0:
                direction = SignalDirection.SELL
                detail = (
                    f"Volumen anómalo bajista ({volume_ratio:.1f}× promedio) "
                    f"con vela roja ({price_change:.2f})"
                )
            else:
                direction = SignalDirection.NEUTRAL
                detail = f"Volumen anómalo ({volume_ratio:.1f}× promedio) con vela doji"
        else:
            direction = SignalDirection.NEUTRAL
            detail = f"Volumen normal ({volume_ratio:.1f}× promedio)"

        return IndicatorResult(
            name="Volume",
            direction=direction,
            value=round(float(volume_ratio), 2),  # type: ignore
            detail=detail,
        )


class NexusAlphaOscillatorCalculator:
    """
    [NEXUS ALPHA v3 — INSTITUTIONAL MEAN-REVERSION COMPOSITE SCORER]

    Pipeline de señal directa para Binary Options HFT (1-5 min).
    NO es un indicador de votación: genera la señal FINAL con su propia confianza.

    Lógica de scoring por capas (0.0 a 1.0):
      Capa 1 (0.40): Bollinger Band (2.0σ) — El precio toca o perfora una banda.
      Capa 2 (0.30): RSI(7) Extremo — RSI < 35 (sobreventa) o RSI > 65 (sobrecompra).
      Capa 3 (0.15): Price Action — Vela de rechazo (cierre contrario al wick extremo).
      Capa 4 (0.15): Volumen — Volumen actual > 1.3x promedio (confirmación de interés).

    Si el score compuesto >= 0.55, emite STRONG_BUY o STRONG_SELL.
    Debajo de ese umbral, NEUTRAL.
    """

    def __init__(self, bb_period: int = 20, bb_dev: float = 2.0, rsi_period: int = 7) -> None:
        self.bb_period = bb_period
        self.bb_dev = bb_dev
        self.rsi_period = rsi_period
        self._cooldown_remaining = 0  # Barras de silencio post-señal
        self._last_signal_dir = None  # Última dirección emitida

    def evaluate(self, df: pd.DataFrame) -> IndicatorResult:
        if len(df) < self.bb_period + 5:
            return IndicatorResult("NexusAlpha", SignalDirection.NEUTRAL, 0.0, "Datos insuficientes")

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return IndicatorResult("NexusAlpha", SignalDirection.NEUTRAL, 0.0,
                                   f"Cooldown ({self._cooldown_remaining})")

        from ta.volatility import BollingerBands
        from ta.momentum import RSIIndicator

        close = df["close"]
        bb = BollingerBands(close=close, window=self.bb_period, window_dev=self.bb_dev)
        rsi = RSIIndicator(close=close, window=self.rsi_period).rsi()

        curr_close = float(close.iloc[-1])
        curr_open = float(df["open"].iloc[-1])
        curr_low = float(df["low"].iloc[-1])
        curr_high = float(df["high"].iloc[-1])
        curr_lower = float(bb.bollinger_lband().iloc[-1])
        curr_upper = float(bb.bollinger_hband().iloc[-1])
        curr_rsi = float(rsi.iloc[-1])

        vol_lookback = min(20, len(df) - 1)
        if vol_lookback > 1:
            avg_vol = float(df["volume"].iloc[-vol_lookback - 1:-1].mean())
            curr_vol = float(df["volume"].iloc[-1])
            vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1.0
        else:
            vol_ratio = 1.0

        # ── BULLISH: Precio tocó/perforó banda inferior + RSI bajo ──
        bull_score = 0.0
        bull_details = []

        if curr_low <= curr_lower:
            bull_score += 0.40
            bull_details.append(f"BB↓")
        elif curr_close <= curr_lower * 1.001:
            bull_score += 0.25
            bull_details.append(f"BB↓Near")

        if curr_rsi < 25:
            bull_score += 0.30
            bull_details.append(f"RSI({curr_rsi:.0f})")
        elif curr_rsi < 35:
            bull_score += 0.20
            bull_details.append(f"RSI({curr_rsi:.0f})")

        if curr_close > curr_open:
            bull_score += 0.15
            bull_details.append("PA↑")

        if vol_ratio >= 1.3:
            bull_score += 0.15
            bull_details.append(f"V({vol_ratio:.1f}x)")

        # ── BEARISH: Precio tocó/perforó banda superior + RSI alto ──
        bear_score = 0.0
        bear_details = []

        if curr_high >= curr_upper:
            bear_score += 0.40
            bear_details.append(f"BB↑")
        elif curr_close >= curr_upper * 0.999:
            bear_score += 0.25
            bear_details.append(f"BB↑Near")

        if curr_rsi > 75:
            bear_score += 0.30
            bear_details.append(f"RSI({curr_rsi:.0f})")
        elif curr_rsi > 65:
            bear_score += 0.20
            bear_details.append(f"RSI({curr_rsi:.0f})")

        if curr_close < curr_open:
            bear_score += 0.15
            bear_details.append("PA↓")

        if vol_ratio >= 1.3:
            bear_score += 0.15
            bear_details.append(f"V({vol_ratio:.1f}x)")

        # ── Señal final ──
        direction = SignalDirection.NEUTRAL
        final_score = 0.0
        detail = f"Bull={bull_score:.2f} Bear={bear_score:.2f}"
        TRIGGER = 0.70  # Óptimo: BB(0.40)+RSI_Panic(0.30)=0.70 → 55.8% WR en BTC 5m

        if bull_score >= TRIGGER and bull_score > bear_score:
            direction = SignalDirection.STRONG_BUY
            final_score = bull_score
            detail = f"BUY[{bull_score:.2f}] {'+'.join(bull_details)}"
            self._cooldown_remaining = 5
        elif bear_score >= TRIGGER and bear_score > bull_score:
            direction = SignalDirection.STRONG_SELL
            final_score = bear_score
            detail = f"SELL[{bear_score:.2f}] {'+'.join(bear_details)}"
            self._cooldown_remaining = 5

        return IndicatorResult(
            name="NexusAlpha",
            direction=direction,
            value=round(final_score, 4),
            detail=detail,
        )

    def generate_direct_signal(self, df: pd.DataFrame) -> dict:
        """
        Genera la señal FINAL completa para Binary Mode, sin pasar
        por el sistema de consenso multi-indicador.
        Retorna el mismo formato dict que TechnicalSignalEngine.generate_signal().
        """
        result = self.evaluate(df)

        if result.direction == SignalDirection.STRONG_BUY:
            signal_str = "BUY"
        elif result.direction == SignalDirection.STRONG_SELL:
            signal_str = "SELL"
        else:
            signal_str = "HOLD"

        return {
            "signal": signal_str,
            "confidence": result.value,  # El score compuesto ES la confianza
            "reason": result.detail,
            "timestamp": datetime.now(timezone.utc),
            "indicators": {
                "NexusAlpha": {
                    "direction": result.direction.name,
                    "value": result.value,
                    "detail": result.detail,
                }
            },
        }


# ══════════════════════════════════════════════════════════════════════
#  TechnicalSignalEngine — Motor principal
# ══════════════════════════════════════════════════════════════════════

# Pesos de cada indicador por defecto
_DEFAULT_WEIGHTS: Dict[str, float] = {
    "RSI": 0.20,
    "MACD": 0.25,
    "Bollinger": 0.20,
    "EMA_Cross": 0.20,
    "Volume": 0.15,
}

_BINARY_WEIGHTS: Dict[str, float] = {
    "NexusAlpha": 0.50,  # Alpha Sniper dicta el 50% de la fuerza
    "Bollinger": 0.20,   # Confirmación perimetral
    "RSI": 0.15,         # Oscilador Crítico
    "Volume": 0.15,      # Anomalías de volumen confirman el bounce
    "MACD": 0.00,        # MACD desactivado (muy lento para 1M Reversion)
}

# Mínimo de indicadores que deben coincidir para emitir señal
_MIN_CONSENSUS = 3


class TechnicalSignalEngine:
    """
    Motor de señales técnicas con consenso de 5 indicadores.

    Solo emite BUY o SELL si ≥ 3 de 5 indicadores coinciden en dirección.
    Caso contrario retorna HOLD.

    Uso:
        engine = TechnicalSignalEngine()
        result = engine.generate_signal(df)
        # result["signal"]     → "BUY" | "SELL" | "HOLD"
        # result["confidence"] → 0.0 – 1.0
        # result["reason"]     → descripción textual
        # result["timestamp"]  → datetime UTC
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        min_consensus: int = _MIN_CONSENSUS,
        mode: str = "spot",
    ) -> None:
        self.mode = mode
        
        if mode == "binary":
            # HFT / BINARY 1-Min — Alpha Composite Scorer (Mean-Reversion)
            # El Alpha v3 incorpora BB, RSI, Volume y Price Action internamente.
            # Los indicadores individuales se mantienen solo para dashboard/diagnóstico.
            self.alpha = NexusAlphaOscillatorCalculator(bb_period=20, bb_dev=2.0, rsi_period=7)
            self.rsi = RSICalculator(period=7)
            self.macd = MACDCalculator(fast=6, slow=13, signal=4)
            self.bollinger = BollingerCalculator(period=20, std_dev=2.0)
            self.volume = VolumeProfileCalculator(lookback=10, anomaly_factor=1.5)
            self._weights = weights or dict(_BINARY_WEIGHTS)
        else:
            # SPOT 15-Min Tuning
            self.rsi = RSICalculator(period=14)
            self.macd = MACDCalculator(fast=12, slow=26, signal=9)
            self.bollinger = BollingerCalculator(period=20, std_dev=2.0)
            self.ema_cross = EMACrossCalculator(fast_period=20, slow_period=50) # 20/50 for intraday
            self.volume = VolumeProfileCalculator(lookback=20, anomaly_factor=2.0)
            self._weights = weights or dict(_DEFAULT_WEIGHTS)

        self._min_consensus = min_consensus

    # ── AI Mode: Dynamic Parameter Injection ──────────────────────────

    def apply_overrides(self, overrides: Dict[str, Any]) -> None:
        """
        Hot-inject optimized parameters from Redis (AI Mode / WFO calibration).
        Called by pipeline._tick() when NEXUS:AI_MODE == 1.

        Supported override keys:
            rsi_period:    int   (default 14 spot / 7 binary)
            bb_period:     int   (default 20)
            bb_dev:        float (default 2.0)
            trigger:       float (NexusAlpha TRIGGER threshold, binary only)
            min_consensus: int   (spot only, 1-5)

        Only provided keys are applied; missing keys keep current values.
        """
        if not overrides:
            return

        changed: List[str] = []

        # RSI period override
        rsi_p = overrides.get("rsi_period")
        if rsi_p is not None:
            rsi_p = int(rsi_p)
            self.rsi = RSICalculator(period=rsi_p)
            changed.append(f"RSI={rsi_p}")

        # Bollinger overrides
        bb_p = overrides.get("bb_period")
        bb_d = overrides.get("bb_dev")
        if bb_p is not None or bb_d is not None:
            new_p = int(bb_p) if bb_p is not None else self.bollinger.period
            new_d = float(bb_d) if bb_d is not None else self.bollinger.std_dev
            self.bollinger = BollingerCalculator(period=new_p, std_dev=new_d)
            changed.append(f"BB({new_p},{new_d})")

        # NexusAlpha (binary mode) overrides
        if self.mode == "binary" and hasattr(self, "alpha"):
            alpha_rsi = overrides.get("rsi_period")
            alpha_bb_p = overrides.get("bb_period")
            alpha_bb_d = overrides.get("bb_dev")
            if any(v is not None for v in [alpha_rsi, alpha_bb_p, alpha_bb_d]):
                new_rsi = int(alpha_rsi) if alpha_rsi is not None else self.alpha.rsi_period
                new_bbp = int(alpha_bb_p) if alpha_bb_p is not None else self.alpha.bb_period
                new_bbd = float(alpha_bb_d) if alpha_bb_d is not None else self.alpha.bb_dev
                self.alpha = NexusAlphaOscillatorCalculator(
                    bb_period=new_bbp, bb_dev=new_bbd, rsi_period=new_rsi
                )
                changed.append(f"Alpha(RSI={new_rsi},BB={new_bbp}/{new_bbd})")

        # Min consensus (spot mode)
        mc = overrides.get("min_consensus")
        if mc is not None and 1 <= int(mc) <= 5:
            self._min_consensus = int(mc)
            changed.append(f"Consensus={mc}")

        if changed:
            logger.info("🧠 AI Mode overrides applied: %s", ", ".join(changed))

    # ── Método principal ──────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Analiza el DataFrame OHLCV y genera la señal de consenso.

        Args:
            df: DataFrame con columnas: open, high, low, close, volume

        Returns:
            dict con keys: signal, confidence, reason, timestamp, indicators
        """
        if df is None or df.empty or len(df) < 2:
            return self._empty_signal("DataFrame vacío o insuficiente")

        # ── BINARY MODE: Pipeline directo del Alpha Composite Scorer ──
        # No usa votación por consenso. El Alpha v3 es un modelo composite
        # que internamente evalúa BB + RSI + Volume + Price Action y
        # genera la señal final con su propia confianza calibrada.
        if self.mode == "binary":
            return self.alpha.generate_direct_signal(df)

        # ── SPOT MODE: Sistema clásico de consenso multi-indicador ──
        results: Dict[str, IndicatorResult] = {
            "RSI": self.rsi.evaluate(df),
            "MACD": self.macd.evaluate(df),
            "Bollinger": self.bollinger.evaluate(df),
            "EMA_Cross": self.ema_cross.evaluate(df),
            "Volume": self.volume.evaluate(df),
        }

        # 2. Contar votos
        buy_votes: List[str] = []
        sell_votes: List[str] = []
        neutral_votes: List[str] = []

        for name, result in results.items():
            if result.direction in (SignalDirection.BUY, SignalDirection.STRONG_BUY):
                buy_votes.append(name)
            elif result.direction in (SignalDirection.SELL, SignalDirection.STRONG_SELL):
                sell_votes.append(name)
            else:
                neutral_votes.append(name)

        # 3. Determinar señal por consenso
        signal, reason_parts, consensus_count = self._resolve_consensus(
            buy_votes, sell_votes, results
        )

        # 4. Calcular confianza ponderada
        confidence = self._calculate_confidence(signal, results, consensus_count)

        # 5. Construir razón textual
        reason = self._build_reason(signal, buy_votes, sell_votes, results)

        output = SignalOutput(
            signal=signal,
            confidence=round(confidence, 4),  # type: ignore
            reason=reason,
            timestamp=datetime.now(timezone.utc),
            indicators=results,
        )

        logger.debug(
            "Señal generada: %s (conf=%.2f, buy=%d, sell=%d, hold=%d) | %s",
            signal,
            confidence,
            len(buy_votes),
            len(sell_votes),
            len(neutral_votes),
            reason,
        )

        return {
            "signal": output.signal,
            "confidence": output.confidence,
            "reason": output.reason,
            "timestamp": output.timestamp,
            "indicators": {
                name: {
                    "direction": r.direction.name,
                    "value": r.value,
                    "detail": r.detail,
                }
                for name, r in output.indicators.items()
            },
        }

    # ── Análisis individual (compatibilidad con main.py) ──────────

    def analyze(self, df: pd.DataFrame) -> SignalOutput:
        """
        Versión que retorna SignalOutput directamente (para uso interno
        de los agentes).
        """
        raw = self.generate_signal(df)
        return SignalOutput(
            signal=raw["signal"],
            confidence=raw["confidence"],
            reason=raw["reason"],
            timestamp=raw["timestamp"],
            indicators={
                name: IndicatorResult(
                    name=name,
                    direction=SignalDirection[info["direction"]],  # type: ignore
                    value=info["value"],
                    detail=info["detail"],
                )
                for name, info in raw["indicators"].items()
            },
        )

    # ── Dashboard ─────────────────────────────────────────────────────

    def get_dashboard(self, df: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
        """Retorna el estado individual de cada indicador sin generar señal."""
        results = {
            "RSI": self.rsi.evaluate(df),
            "MACD": self.macd.evaluate(df),
            "Bollinger": self.bollinger.evaluate(df),
            "EMA_Cross": self.ema_cross.evaluate(df),
            "Volume": self.volume.evaluate(df),
        }
        return {
            name: {
                "direction": r.direction.name,
                "value": r.value,
                "detail": r.detail,
            }
            for name, r in results.items()
        }

    # ── Configuración ─────────────────────────────────────────────────

    def set_weights(self, weights: Dict[str, float]) -> None:
        """Ajusta los pesos relativos de cada indicador."""
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("La suma de pesos debe ser > 0")
        # Normalizar
        self._weights = {k: v / total for k, v in weights.items()}
        logger.info("Pesos actualizados: %s", self._weights)

    def set_min_consensus(self, n: int) -> None:
        """Cambia el mínimo de indicadores para emitir señal."""
        if not (1 <= n <= 5):
            raise ValueError("min_consensus debe estar entre 1 y 5")
        self._min_consensus = n

    # ══════════════════════════════════════════════════════════════════
    #  Métodos privados
    # ══════════════════════════════════════════════════════════════════

    def _resolve_consensus(
        self,
        buy_votes: List[str],
        sell_votes: List[str],
        results: Dict[str, IndicatorResult],
    ) -> Tuple[str, List[str], int]:
        """Determina la señal final basada en votos con mínimo de consenso."""
        n_buy = len(buy_votes)
        n_sell = len(sell_votes)

        if n_buy >= self._min_consensus and n_buy > n_sell:
            return "BUY", buy_votes, n_buy
        elif n_sell >= self._min_consensus and n_sell > n_buy:
            return "SELL", sell_votes, n_sell
        else:
            return "HOLD", [], max(n_buy, n_sell)

    def _calculate_confidence(
        self,
        signal: str,
        results: Dict[str, IndicatorResult],
        consensus_count: int,
    ) -> float:
        """
        Calcula la confianza ponderada de la señal.

        Factores:
        - Proporción de indicadores que votan en la dirección (0 – 1)
        - Pesos de los indicadores votantes
        - Bonus si consensus_count ≥ 4 (alta convicción)
        """
        if signal == "HOLD":
            return 0.0

        target_dir = (
            {SignalDirection.BUY, SignalDirection.STRONG_BUY}
            if signal == "BUY"
            else {SignalDirection.SELL, SignalDirection.STRONG_SELL}
        )

        weighted_sum = 0.0
        total_weight = 0.0

        for name, result in results.items():
            w = self._weights.get(name, 0.2)
            total_weight += w
            if result.direction in target_dir:
                weighted_sum += w  # type: ignore

        base_confidence = weighted_sum / total_weight if total_weight > 0 else 0.0  # type: ignore

        # Bonus por alta convicción (4 o 5 indicadores)
        if consensus_count >= 5:
            base_confidence = min(base_confidence * 1.15, 1.0)
        elif consensus_count >= 4:
            base_confidence = min(base_confidence * 1.05, 1.0)

        return base_confidence

    def _build_reason(
        self,
        signal: str,
        buy_votes: List[str],
        sell_votes: List[str],
        results: Dict[str, IndicatorResult],
    ) -> str:
        """Construye la cadena de razón con los indicadores que dispararon."""
        if signal == "HOLD":
            parts = []
            if buy_votes:
                parts.append(f"BUY({len(buy_votes)}): {', '.join(buy_votes)}")
            if sell_votes:
                parts.append(f"SELL({len(sell_votes)}): {', '.join(sell_votes)}")
            if parts:
                return f"Sin consenso (mín {self._min_consensus}). {' | '.join(parts)}"
            return "Todos los indicadores neutrales"

        active = buy_votes if signal == "BUY" else sell_votes
        details = [results[name].detail for name in active]
        return f"{signal} por {len(active)}/5 indicadores: {' · '.join(details)}"

    def _empty_signal(self, reason: str) -> Dict[str, Any]:
        """Retorna una señal HOLD vacía con la razón indicada."""
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc),
            "indicators": {},
        }

    # ══════════════════════════════════════════════════════════════════
    #  Multi-Timeframe Fractal Analysis (Fase 9)
    # ══════════════════════════════════════════════════════════════════

    def generate_mtf_signal(
        self, tf_dataframes: Dict[str, pd.DataFrame]
    ) -> Dict[str, Any]:
        """
        Análisis Fractal Multi-Timeframe.
        Evalúa indicadores independientes en cada temporalidad y genera
        un contexto LLM unificado con la perspectiva Macro → Micro.

        Args:
            tf_dataframes: dict con {timeframe_label: dataframe}
                Ejemplo: {"1d": df_daily, "4h": df_4h, "1h": df_1h, "5m": df_5m}

        Returns:
            dict con:
              - "signal": señal final ponderada por contexto macro
              - "confidence": confianza ajustada
              - "mtf_context": string descriptivo para el LLM
              - "timeframe_signals": señales individuales por timeframe
              - "macro_bias": "BULLISH" | "BEARISH" | "NEUTRAL"
        """
        if not tf_dataframes:
            return self._empty_signal("No se proporcionaron DataFrames MTF")

        # Orden de prioridad Macro → Micro
        tf_order = ["1d", "4h", "1h", "30m", "15m", "5m", "1m"]
        sorted_tfs = sorted(
            tf_dataframes.keys(),
            key=lambda x: tf_order.index(x) if x in tf_order else 99,
        )

        tf_signals: Dict[str, Dict[str, Any]] = {}
        macro_votes_buy = 0
        macro_votes_sell = 0
        macro_tfs = {"1d", "4h", "1h"}  # Temporalidades consideradas "macro"

        for tf in sorted_tfs:
            df = tf_dataframes[tf]
            if df is None or df.empty or len(df) < 2:
                continue
            sig = self.generate_signal(df)
            tf_signals[tf] = sig

            if tf in macro_tfs:
                if sig["signal"] == "BUY":
                    macro_votes_buy += 1
                elif sig["signal"] == "SELL":
                    macro_votes_sell += 1

        # Determinar sesgo macro
        if macro_votes_buy > macro_votes_sell:
            macro_bias = "BULLISH"
        elif macro_votes_sell > macro_votes_buy:
            macro_bias = "BEARISH"
        else:
            macro_bias = "NEUTRAL"

        # Construir contexto narrativo para el LLM
        context_parts = []
        for tf in sorted_tfs:
            if tf in tf_signals:
                sig = tf_signals[tf]
                emoji = "🟢" if sig["signal"] == "BUY" else "🔴" if sig["signal"] == "SELL" else "⚪"
                context_parts.append(
                    f"{emoji} [{tf.upper()}] {sig['signal']} (conf={sig['confidence']:.2f}) — {sig['reason'][:120]}"
                )

        mtf_context = (
            f"SESGO MACRO: {macro_bias}\n" + "\n".join(context_parts)
        )

        # Señal final: la menor temporalidad que tenga señal, 
        # pero SOLO si está alineada con el sesgo macro
        final_signal = "HOLD"
        final_confidence = 0.0
        final_reason = "Sin alineación fractal"

        # Buscar señal micro (de menor a mayor temporalidad)
        for tf in reversed(sorted_tfs):
            if tf in tf_signals:
                sig = tf_signals[tf]
                if sig["signal"] in ("BUY", "SELL"):
                    # Verificar alineación con el sesgo macro
                    aligned = (
                        (sig["signal"] == "BUY" and macro_bias == "BULLISH")
                        or (sig["signal"] == "SELL" and macro_bias == "BEARISH")
                    )
                    if aligned:
                        final_signal = sig["signal"]
                        # Boost de confianza por alineación fractal
                        final_confidence = min(sig["confidence"] * 1.15, 0.95)
                        final_reason = (
                            f"Fractal {tf}: {sig['signal']} alineado con sesgo macro {macro_bias}"
                        )
                        break

        return {
            "signal": final_signal,
            "confidence": round(final_confidence, 4),
            "reason": final_reason,
            "timestamp": datetime.now(timezone.utc),
            "mtf_context": mtf_context,
            "timeframe_signals": tf_signals,
            "macro_bias": macro_bias,
            "indicators": {},
        }

    def __repr__(self) -> str:
        return (
            f"<TechnicalSignalEngine indicators=5 "
            f"min_consensus={self._min_consensus} "
            f"weights={self._weights}>"
        )

