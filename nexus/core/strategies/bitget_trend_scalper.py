"""
NEXUS v5.0 (beta) — BitgetTrendScalperStrategy
================================================
Trend-following scalper designed for Bitget USDT-M perpetual futures.

Logic:
  Primary timeframe  : 15m (trend direction)
  Execution timeframe: 5m  (entry timing)

Signal generation:
  LONG  when: EMA9 > EMA21 on 15m AND RSI(14) on 5m crosses above 50
              AND current candle volume > 1.5x 20-bar avg volume
  SHORT when: EMA9 < EMA21 on 15m AND RSI(14) on 5m crosses below 50
              AND current candle volume > 1.5x 20-bar avg volume
  HOLD  otherwise

Exit parameters (injected into TradeSignal):
  stop_loss  : entry_price - (2 * ATR14_5m) for LONG
               entry_price + (2 * ATR14_5m) for SHORT
  take_profit: entry_price + (3 * ATR14_5m) for LONG  [ratio 1:1.5]
               entry_price - (3 * ATR14_5m) for SHORT
  Risk/reward: minimum 1:1.5 enforced before entry

Why this strategy for Bitget futures:
  - EMA crossover on 15m filters noise while capturing real trends
  - Volume confirmation eliminates false breakouts in low-liquidity periods
  - ATR-based SL/TP adapts to volatility — no hardcoded pip values
  - Both LONG and SHORT capture full market directionality
"""

from typing import Any, Dict
import pandas as pd
from .base import BaseStrategy

class BitgetTrendScalperStrategy(BaseStrategy):
    """
    Trend-following scalper for Bitget perpetual futures.
    Uses dual-timeframe analysis: 15m trend + 5m entry timing.
    """

    def __init__(self) -> None:
        self.ema_fast    = 9
        self.ema_slow    = 21
        self.rsi_period  = 14
        self.atr_period  = 14
        self.volume_mult = 1.5   # Volume must exceed 1.5x 20-bar avg
        self.sl_atr_mult = 2.0   # SL at 2x ATR from entry
        self.tp_atr_mult = 3.0   # TP at 3x ATR from entry (1:1.5 R/R)
        self.min_rr      = 1.5   # Minimum risk/reward ratio enforced

    async def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Analyzes market data and returns a signal dict.

        Args:
            df: OHLCV DataFrame. Must contain at least 50 rows for
                reliable indicator calculation.

        Returns:
            {
              "signal":       "BUY" | "SELL" | "HOLD",
              "confidence":   float (0.0-1.0),
              "stop_loss":    float | None,
              "take_profit":  float | None,
              "reason":       str,
              "indicators": {
                  "ema_fast": float,
                  "ema_slow": float,
                  "rsi":      float,
                  "atr":      float,
                  "volume_ratio": float,
              }
            }
        """
        # STEP 1 — Guard
        if df is None or len(df) < 50:
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "stop_loss": None,
                "take_profit": None,
                "reason": "Insufficient data",
                "indicators": {}
            }

        # STEP 2 — Compute indicators (pandas only, no ta-lib)
        ema_fast_series = df['close'].ewm(span=self.ema_fast, adjust=False).mean()
        ema_slow_series = df['close'].ewm(span=self.ema_slow, adjust=False).mean()

        # RSI(14)
        delta = df['close'].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/self.rsi_period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/self.rsi_period, adjust=False).mean()
        rs = gain / loss
        rsi_series = 100 - (100 / (1 + rs))

        # ATR(14)
        df_copy = df.copy()
        df_copy['prev_close'] = df_copy['close'].shift(1)
        df_copy['tr1'] = df_copy['high'] - df_copy['low']
        df_copy['tr2'] = (df_copy['high'] - df_copy['prev_close']).abs()
        df_copy['tr3'] = (df_copy['low'] - df_copy['prev_close']).abs()
        df_copy['true_range'] = df_copy[['tr1', 'tr2', 'tr3']].max(axis=1)
        atr_series = df_copy['true_range'].rolling(window=self.atr_period).mean()

        # Volume ratio
        vol_avg_20 = df['volume'].iloc[-20:].mean()
        volume_ratio = float(df['volume'].iloc[-1] / vol_avg_20) if vol_avg_20 > 0 else 0.0

        # STEP 3 — Current values (last bar)
        ema_fast_now = float(ema_fast_series.iloc[-1])
        ema_slow_now = float(ema_slow_series.iloc[-1])
        ema_fast_prev = float(ema_fast_series.iloc[-2])
        ema_slow_prev = float(ema_slow_series.iloc[-2])
        
        rsi_now = float(rsi_series.iloc[-1])
        rsi_prev = float(rsi_series.iloc[-2])
        
        atr_now = float(atr_series.iloc[-1])
        close_now = float(df['close'].iloc[-1])

        # STEP 4 — Signal conditions
        signal = "HOLD"
        reason = "No setup"
        
        if (ema_fast_now > ema_slow_now 
            and ema_fast_prev <= ema_slow_prev 
            and rsi_now > 50 
            and volume_ratio >= self.volume_mult):
            signal = "BUY"
            reason = "LONG setup triggered"
            
        elif (ema_fast_now < ema_slow_now 
              and ema_fast_prev >= ema_slow_prev 
              and rsi_now < 50 
              and volume_ratio >= self.volume_mult):
            signal = "SELL"
            reason = "SHORT setup triggered"

        if signal == "HOLD":
            return {
                "signal": "HOLD",
                "confidence": 0.0,
                "stop_loss": None,
                "take_profit": None,
                "reason": reason,
                "indicators": {
                    "ema_fast": ema_fast_now,
                    "ema_slow": ema_slow_now,
                    "rsi": rsi_now,
                    "atr": atr_now,
                    "volume_ratio": volume_ratio
                }
            }

        # STEP 5 — R/R enforcement
        sl = None
        tp = None
        
        if signal == "BUY":
            sl = close_now - (self.sl_atr_mult * atr_now)
            tp = close_now + (self.tp_atr_mult * atr_now)
            
            risk = close_now - sl
            reward = tp - close_now
            rr = reward / risk if risk > 0 else 0
            
            if rr < self.min_rr:
                return {
                    "signal": "HOLD", "confidence": 0.0, 
                    "stop_loss": None, "take_profit": None,
                    "reason": f"R/R below minimum ({rr:.2f} < {self.min_rr})",
                    "indicators": {"ema_fast": ema_fast_now, "ema_slow": ema_slow_now, "rsi": rsi_now, "atr": atr_now, "volume_ratio": volume_ratio}
                }
                
        elif signal == "SELL":
            sl = close_now + (self.sl_atr_mult * atr_now)
            tp = close_now - (self.tp_atr_mult * atr_now)
            
            risk = sl - close_now
            reward = close_now - tp
            rr = reward / risk if risk > 0 else 0
            
            if rr < self.min_rr:
                return {
                    "signal": "HOLD", "confidence": 0.0, 
                    "stop_loss": None, "take_profit": None,
                    "reason": f"R/R below minimum ({rr:.2f} < {self.min_rr})",
                    "indicators": {"ema_fast": ema_fast_now, "ema_slow": ema_slow_now, "rsi": rsi_now, "atr": atr_now, "volume_ratio": volume_ratio}
                }

        # STEP 6 — Confidence calculation
        base_confidence = 0.60
        rsi_boost = min(abs(rsi_now - 50) / 50.0 * 0.15, 0.15)
        vol_boost = min((volume_ratio - 1.0) / 2.0 * 0.15, 0.15)
        confidence = min(base_confidence + rsi_boost + vol_boost, 0.90)

        # STEP 7 — Return
        return {
            "signal": signal,
            "confidence": confidence,
            "stop_loss": sl,
            "take_profit": tp,
            "reason": reason,
            "indicators": {
                "ema_fast": ema_fast_now,
                "ema_slow": ema_slow_now,
                "rsi": rsi_now,
                "atr": atr_now,
                "volume_ratio": volume_ratio
            }
        }
