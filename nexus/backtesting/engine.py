"""
NEXUS v4.0 — Institutional Backtesting Engine
=============================================
Motor event-driven pero optimizado para ejecución secuencial sobre DataFrame.
Instancia directamente el TechnicalSignalEngine en modo 'binary' para 100% de 
paridad con el entorno productivo.
"""

from typing import Any, Dict
import numpy as np
import pandas as pd
from nexus.core.signal_engine import TechnicalSignalEngine

class VectorizedBacktester:
    """
    Simulador fila-por-fila para validación de estrategias Binary Options HFT.
    Asegura cero lookahead-bias pasando ventanas incrementales (rolling window)
    al motor de señales original.
    """

    def __init__(
        self, 
        initial_capital: float = 10000.0, 
        bet_size: float = 1.0, 
        payout: float = 85.0
    ):
        self.initial_capital = initial_capital
        self.bet_size = bet_size
        self.payout_pct = payout / 100.0
        # CRÍTICO: Usar la misma clase exacta que producción
        self.engine = TechnicalSignalEngine(mode="binary")

    def run(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Ejecuta el backtest stepping through visual temporal.
        
        Args:
            df: DataFrame con OHLCV histórico.

        Returns:
            Diccionario con métricas institucionales (PF, WinRate, etc).
        """
        capital = self.initial_capital
        wins = 0
        losses = 0
        pnl_array = []
        equity_curve = [capital]

        # Necesitamos al menos 30 velas para inicializar los indicadores en frío
        warmup_period = 30
        total_rows = len(df)
        
        if total_rows <= warmup_period:
            return self._empty_metrics()

        # Step by step simulation to guarantee NO look-ahead bias
        for i in range(warmup_period, total_rows - 1):
            # 1. Ventana de datos DISPONIBLES hasta este tic
            window = df.iloc[:i+1]
            
            # 2. Generar señal (Alpha v3 evalúa la última vela de `window`)
            signal_result = self.engine.generate_signal(window)
            signal_val = signal_result.get("signal", "HOLD")
            
            if signal_val in ["BUY", "STRONG_BUY", "SELL", "STRONG_SELL"]:
                # 3. Simulate Execution: Se ejecuta un trade que expira en la SIGUIENTE vela
                next_candle = df.iloc[i+1]
                open_p = float(next_candle["open"])
                close_p = float(next_candle["close"])
                
                # Binary Options Math
                won = False
                if signal_val in ["BUY", "STRONG_BUY"]:
                    # CALL Trade gana si el cierre es superior a la apertura
                    won = (close_p > open_p)
                elif signal_val in ["SELL", "STRONG_SELL"]:
                    # PUT Trade gana si el cierre es inferior a la apertura
                    won = (close_p < open_p)
                    
                # 4. P&L Resolution
                if won:
                    pnl = self.bet_size * self.payout_pct
                    wins += 1
                else:
                    pnl = -self.bet_size
                    losses += 1
                    
                capital += pnl
                pnl_array.append(pnl)
                equity_curve.append(capital)

        # ── Cálculos de Métricas Quant ──
        total_trades = wins + losses
        win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0.0
        
        gross_profit = sum(p for p in pnl_array if p > 0)
        gross_loss = abs(sum(p for p in pnl_array if p < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
        
        # Max Drawdown vectorial
        if len(equity_curve) > 1:
            eq = np.array(equity_curve)
            running_max = np.maximum.accumulate(eq)
            # Prevenir división por cero si capital llega a 0
            safe_max = np.maximum(running_max, 1e-8)
            drawdowns = (running_max - eq) / safe_max
            max_dd = float(np.max(drawdowns)) * 100
        else:
            max_dd = 0.0
            
        ret_total = ((capital - self.initial_capital) / self.initial_capital) * 100

        return {
            "Total Trades": total_trades,
            "Wins": wins,
            "Losses": losses,
            "Win Rate (%)": round(win_rate, 2),
            "Profit Factor": round(pf, 2),
            "Max Drawdown (%)": round(max_dd, 2),
            "Final Capital": round(capital, 2),
            "Total Return (%)": round(ret_total, 2)
        }

    def _empty_metrics(self) -> Dict[str, Any]:
        return {
            "Total Trades": 0, "Wins": 0, "Losses": 0,
            "Win Rate (%)": 0.0, "Profit Factor": 0.0,
            "Max Drawdown (%)": 0.0, "Final Capital": self.initial_capital,
            "Total Return (%)": 0.0
        }
