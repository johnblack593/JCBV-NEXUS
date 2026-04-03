#!/usr/from/env python3
"""
NEXUS v4.0 — Walk-Forward Optimizer (WFO) Script
=================================================
Simula datos sintéticos y ejecuta un Grid Search hiperparamétrico
sobre el Alpha v3 (Signal Engine).
Busca los parámetros que maximicen el Profit Factor.
"""

import itertools
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# Path wizardry to allow direct execution
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from nexus.backtesting.engine import VectorizedBacktester

def generate_synthetic_data(num_bars: int = 5000) -> pd.DataFrame:
    """Genera datos OHLCV sintéticos altamente ruidosos (mean-reverting)."""
    np.random.seed(42)  # Determinismo para la calibración
    base_price = 1.1000
    
    # Random walk con mean-reversion subyacente
    returns = np.random.normal(0, 0.0005, num_bars)
    prices = base_price * np.exp(np.cumsum(returns))
    
    dates = [datetime.now() - timedelta(minutes=num_bars-i) for i in range(num_bars)]
    
    df = pd.DataFrame({"open_time": dates, "close": prices})
    
    # Generar wicks extremos artificiales para que las BB/RSI actúen
    volatility = abs(np.random.normal(0, 0.0003, num_bars))
    df["open"] = df["close"].shift(1).fillna(base_price)
    
    # Asegurar realismo: high >= max(open, close), low <= min(open, close)
    max_oc = df[["open", "close"]].max(axis=1)
    min_oc = df[["open", "close"]].min(axis=1)
    
    df["high"] = max_oc + volatility
    df["low"] = min_oc - volatility
    df["volume"] = np.random.randint(100, 2000, num_bars)
    
    return df

def run_calibration():
    print("="*75)
    print(" 🧠 NEXUS v4.0 — Auto-Calibration Engine (Grid Search WFO)")
    print("="*75)
    
    # 1. Generar matriz de datos
    df = generate_synthetic_data(2000)
    print(f"[*] Dataset Sintético Generado: {len(df)} velas de 1m (Mean-Reverting).")
    
    # 2. Definir espacio de búsqueda (Grid Search)
    bb_periods = [14, 20, 25]
    rsi_periods = [5, 7, 10]
    bb_devs = [2.0, 2.5]
    
    combinations = list(itertools.product(bb_periods, rsi_periods, bb_devs))
    print(f"[*] Total de combinaciones hiperparamétricas a probar: {len(combinations)}\n")
    
    # 3. Inicializar motor de backtest
    # Apostamos $10 flat, con un payout institucional de IQ Option del 85%
    backtester = VectorizedBacktester(initial_capital=10000.0, bet_size=10.0, payout=85.0)
    results = []
    
    # 4. Loop de Optimización
    import time
    start_time = time.time()
    
    for idx, (bb_p, rsi_p, bb_d) in enumerate(combinations, 1):
        overrides = {
            "bb_period": bb_p,
            "rsi_period": rsi_p,
            "bb_dev": bb_d
        }
        
        # Inyectar Overrides dinámicamente en el Alpha Engine
        backtester.engine.apply_overrides(overrides)
        
        # Ejecutar barrido vectorial
        print(f"  → [{idx:02d}/{len(combinations)}] Simulando BB=({bb_p},{bb_d:.1f}) | RSI={rsi_p:02d} ... ", end="")
        metrics = backtester.run(df)
        
        # Guardar en matriz de resultados
        results.append({
            "bb_period": bb_p,
            "rsi_period": rsi_p,
            "bb_dev": bb_d,
            **metrics
        })
        print(f"PF: {metrics['Profit Factor']} | WR: {metrics['Win Rate (%)']}%")
        
    duration = time.time() - start_time
    
    # 5. Sorting y Evaluación de Resultados Finales
    # Primario: Profit Factor | Secundario: Win Rate
    results.sort(key=lambda x: (x["Profit Factor"], x["Win Rate (%)"]), reverse=True)
    
    print("\n" + "="*75)
    print(f" 🏆 TOP 5 PARAMETER SETS (Optimization Time: {duration:.1f}s)")
    print("="*75)
    
    for i, res in enumerate(results[:5], 1):
        win_rate = res['Win Rate (%)']
        pf = res['Profit Factor']
        trades = res['Total Trades']
        ret = res['Total Return (%)']
        
        print(f" {i}. Alpha Config: BB=(period={res['bb_period']}, dev={res['bb_dev']:.1f}), RSI={res['rsi_period']}")
        print(f"    ↳ Win Rate: {win_rate:05.2f}% | Profit Factor: {pf:04.2f} | Trades: {trades} | Return: {ret}%")
        
    print("="*75)
    
    # Best params isolated
    best = results[0]
    print(f"\n💡 Optimal Redis Injection (AI_MODE):")
    import json
    payload = {
        "bb_period": best['bb_period'],
        "bb_dev": best['bb_dev'],
        "rsi_period": best['rsi_period']
    }
    print(f"redis-cli SET NEXUS:OPT:EURUSD-op:GREEN '{json.dumps(payload)}'")

if __name__ == "__main__":
    run_calibration()
