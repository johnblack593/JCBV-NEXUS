"""
NEXUS Trading System — Walk-Forward Optimization Engine
=========================================================
Implementa WFO (Walk-Forward Optimization), dividiendo el histórico 
en ventanas rodantes In-Sample (IS) para ajuste de parámetros y
Out-of-Sample (OOS) para validación estricta y prevención de Curve Fitting.
"""

from __future__ import annotations

import os
import sys
import json
import logging
from datetime import timedelta
from typing import List, Dict, Any, Tuple

import pandas as pd
import numpy as np

try:
    import backtrader as bt
except ImportError:
    bt = None

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Importamos la estrategia desde el runner existente
from backtesting.backtest_runner import NexusSpotStrategy, calculate_metrics

logger = logging.getLogger("nexus.wfo")
if not logger.handlers:
    import config.settings
    # setup basic logger
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")

class WalkForwardOptimizer:
    """Motor de validación institucional rolling-window."""
    
    def __init__(self, csv_path: str, is_days: float = 90.0, oos_days: float = 30.0, trading_mode: str = "spot"):
        """
        Args:
            csv_path: Ruta al archivo CSV procesado de Binance.
            is_days: Longitud de la ventana In-Sample (entrenamiento). Acepta decimales para Binarias.
            oos_days: Longitud de la ventana Out-of-Sample (testeo). Acepta decimales para Binarias.
            trading_mode: "spot" o "binary"
        """
        self.csv_path = csv_path
        self.is_days = float(is_days)
        self.oos_days = float(oos_days)
        self.trading_mode = trading_mode
        
        logger.info("Cargando dataset histórico para WFO: %s", csv_path)
        self.raw_data = pd.read_csv(self.csv_path, parse_dates=['open_time'], index_col='open_time')
        # Limpieza básica
        cols = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}
        self.raw_data = self.raw_data.rename(columns=cols)
        self.raw_data.sort_index(inplace=True)
        
        logger.info("Dataset cargado: %d filas. Intervalo: %s a %s", 
                    len(self.raw_data), self.raw_data.index[0], self.raw_data.index[-1])

    def generate_windows(self) -> List[Dict[str, pd.Timestamp]]:
        """Calcula el rango de cada iteración step-forward."""
        start_date = self.raw_data.index[0]
        end_date = self.raw_data.index[-1]
        
        windows = []
        current_start = start_date
        
        while True:
            is_end = current_start + timedelta(days=self.is_days)
            oos_end = is_end + timedelta(days=self.oos_days)
            
            if oos_end > end_date:
                # Truncamos si no avanza suficiente, o lo rompemos
                break
                
            windows.append({
                "window_idx": len(windows) + 1,
                "is_start": current_start,
                "is_end": is_end,
                "oos_start": is_end,
                "oos_end": oos_end
            })
            
            # Anchored o Rolling? Esto es Rolling (nos movemos oos_days)
            current_start += timedelta(days=self.oos_days)
            
        logger.info("Generadas %d ventanas Walk-Forward (IS: %dd, OOS: %dd)", len(windows), self.is_days, self.oos_days)
        return windows

    def _run_backtrader_slice(self, df_slice: pd.DataFrame, params: Dict[str, Any]) -> Tuple[float, List[float]]:
        """Ejecuta un backtest aislado sobre un segmento de Pandas."""
        if bt is None:
            raise ImportError("backtrader no esta instalado.")
            
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.broker.setcash(10000.0)
        cerebro.broker.setcommission(commission=0.001) # 0.1% Taker Binance
        
        data_feed = bt.feeds.PandasData(
            dataname=df_slice,
            open="open",
            high="high",
            low="low",
            close="close",
            volume="volume"
        )
        cerebro.adddata(data_feed)
        
        if self.trading_mode == "binary":
            from backtesting.binary_strategy import NexusBinaryStrategy
            cerebro.broker.setcommission(commission=0.0) 
            # Inyectar hiperparámetros de Binarias a la Estrategia Nexus
            cerebro.addstrategy(
                NexusBinaryStrategy,
                min_confidence=params.get("min_confidence", 0.70),
                payout_pct=0.85,
                expiry_bars=3
            )
        else:
            from backtesting.backtest_runner import NexusSpotStrategy
            cerebro.broker.setcommission(commission=0.001) # 0.1% Taker Binance
            # Inyectar hiperparámetros de Spot a la Estrategia Nexus
            cerebro.addstrategy(
                NexusSpotStrategy,
                sl_atr_mult=params.get("sl_atr_mult", 2.0),
                tp_atr_mult=params.get("tp_atr_mult", 3.0),
                min_confidence=params.get("min_confidence", 0.65)
            )
        
        # Para capturar la equity curve
        cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='timereturn')
        
        logger.debug("Running cerebo on %d bars params=%s", len(df_slice), params)
        results = cerebro.run()
        
        final_value = cerebro.broker.getvalue()
        strat = results[0]
        rets = strat.analyzers.timereturn.get_analysis()
        
        # Calcular Sharpe básico para ranking
        returns_list = list(rets.values())
        if len(returns_list) > 1 and np.std(returns_list) > 0:
            sharpe = (np.mean(returns_list) / np.std(returns_list, ddof=1)) * np.sqrt(8760) # 1H
        else:
            sharpe = 0.0
            
        return sharpe, returns_list

    def execute_wfo(self, param_grid: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Ejecuta el pipeline WFO entero.
        Itera ventanas -> Busca mejor parametru In-Sample -> Lo aplica en Out-of-Sample -> Acumula retornos.
        """
        windows = self.generate_windows()
        oos_equity_curve_stitched = [10000.0]  # Start with 10k base
        wfo_log = []
        
        for w in windows:
            idx = w["window_idx"]
            logger.info("--- Procesando Ventana %d ---", idx)
            logger.info("  IS:  %s a %s", w["is_start"].date(), w["is_end"].date())
            logger.info("  OOS: %s a %s", w["oos_start"].date(), w["oos_end"].date())
            
            df_is = self.raw_data.loc[w["is_start"] : w["is_end"]]
            df_oos = self.raw_data.loc[w["oos_start"] : w["oos_end"]]
            
            # --- 1. OPTIMIZACION IN-SAMPLE ---
            best_sharpe = -999.0
            best_params = param_grid[0]
            
            for params in param_grid:
                sharpe, _ = self._run_backtrader_slice(df_is, params)
                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = params
                    
            logger.info("  [IN-SAMPLE] Mejor Combinación: %s (Sharpe: %.2f)", best_params, best_sharpe)
            
            # --- 2. TEST OUT-OF-SAMPLE ---
            # Aplicamos SIN TOCAR NADA los `best_params` encontrados
            oos_sharpe, oos_returns = self._run_backtrader_slice(df_oos, best_params)
            
            logger.info("  [OUT-OF-SAMPLE] Sharpe: %.2f | Datos no vistos", oos_sharpe)
            
            # Stitching: Acumular rentabilidad pura OOS continua
            current_capital = oos_equity_curve_stitched[-1]
            for r in oos_returns:
                current_capital *= (1 + r)
                oos_equity_curve_stitched.append(current_capital)
                
            wfo_log.append({
                "window": idx,
                "best_params": best_params,
                "is_sharpe": best_sharpe,
                "oos_sharpe": oos_sharpe,
                "is_bars": len(df_is),
                "oos_bars": len(df_oos)
            })
            
        return {
            "wfo_log": wfo_log,
            "stitched_equity": oos_equity_curve_stitched
        }

if __name__ == "__main__":
    # Motor CLI de Validación WFO Multiple Timeframe
    import argparse
    parser = argparse.ArgumentParser(description="NEXUS Walk-Forward Optimizer")
    parser.add_argument("--csv", type=str, default=str(os.path.join(os.path.dirname(__file__), "..", "data", "historical", "BTCUSDT_1h.csv")), help="Ruta al CSV histórico")
    parser.add_argument("--is-days", type=float, default=90.0, help="Días de In-Sample (acepta decimales para Binarias)")
    parser.add_argument("--oos-days", type=float, default=30.0, help="Días de Out-Of-Sample (acepta decimales para Binarias)")
    parser.add_argument("--mode", type=str, default="spot", choices=["spot", "binary"], help="Modo de Trading: spot o binary")
    args = parser.parse_args()
    
    if os.path.exists(args.csv):
        file_name = os.path.basename(args.csv).replace(".csv", "")
        print(f"\n[NEXUS] Arrancando WFO Engine en {file_name} | Modo: {args.mode.upper()} | IS={args.is_days}d OOS={args.oos_days}d")
        
        wfo = WalkForwardOptimizer(args.csv, is_days=args.is_days, oos_days=args.oos_days, trading_mode=args.mode)
        
        # Grid institucional rápido
        grid = [
            {"sl_atr_mult": 1.5, "tp_atr_mult": 2.5, "min_confidence": 0.60},
            {"sl_atr_mult": 2.0, "tp_atr_mult": 3.0, "min_confidence": 0.65},
        ]
        
        # Ejecutar 
        res = wfo.execute_wfo(grid)
        
        # Reconstruir la serializacion JSON estricta (necesaria para el Dashboard)
        os.makedirs(os.path.join(os.path.dirname(__file__), "..", "reports"), exist_ok=True)
        report_path = os.path.join(os.path.dirname(__file__), "..", "reports", "wfo_results.json")
        
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=4)
            
        print(f"\n--- Walk-Forward Completed & Saved to {report_path} ---")
        for log in res["wfo_log"]:
            print(f"Win {log['window']}: P={log['best_params']} | IS Sharpe={log['is_sharpe']:.2f} | OOS Sharpe={log['oos_sharpe']:.2f}")
    else:
        print(f"[!] Error Crítico: CSV no encontrado en la ruta {args.csv}")
