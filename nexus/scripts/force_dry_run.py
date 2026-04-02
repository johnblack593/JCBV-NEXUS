"""
Script: nexus/scripts/force_dry_run.py

Inyecta un DataFrame DataFrame sintético forzando una señal de extrema sobreventa
(RSI bajo, ruptura de BB inferior, vela de rechazo y volumen alto)
para disparar la señal de STRONG_BUY simulada en el pipeline en modo Dry-Run.
"""

import sys
import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np

# Asegurar importabilidad de nexus
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from nexus.core.pipeline import NexusPipeline

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("force_dry_run")

def create_synthetic_data() -> pd.DataFrame:
    """Genera datos sintéticos para forzar un STRONG_BUY por Mean-Reversion Alpha."""
    periods = 50
    now = datetime.now(timezone.utc)
    
    # Base decreciente para forzar sobreventa fuerte
    prices = np.linspace(100.0, 50.0, periods)
    
    # Datos base
    df = pd.DataFrame({
        "timestamp": [now - timedelta(minutes=periods - i) for i in range(periods)],
        "open": prices + 1.0,  # Tendencia bajista: open > close general
        "high": prices + 2.0,
        "low": prices - 2.0,
        "close": prices,
        "volume": np.random.uniform(100, 200, periods)
    })
    
    # Velas finales manipuladas para cumplir condiciones exactas de NexusAlpha
    # 1. Romper banda inferior de Bollinger bruscamente
    # 2. Causar RSI muy bajo (< 30) (La tendencia ya lo bajó mucho)
    # 3. Vela de rechazo: close > open para cumplir Price Action
    # 4. Volumen alto > 1.0x el promedio (promedio es ~150, ponemos 500)
    
    df.loc[periods - 1, "open"] = 40.0
    df.loc[periods - 1, "low"] = 38.0
    df.loc[periods - 1, "close"] = 42.0  # Cierre alcista de rechazo (C>O)
    df.loc[periods - 1, "high"] = 43.0
    df.loc[periods - 1, "volume"] = 500.0 # Volumen anómalo
    
    # La vela anterior debe ser bajista dura para hundir la banda y el RSI
    df.loc[periods - 2, "open"] = 55.0
    df.loc[periods - 2, "low"] = 43.0
    df.loc[periods - 2, "close"] = 45.0
    df.loc[periods - 2, "high"] = 56.0
    
    return df

async def main():
    # 1. Configurar entorno a STRICT DRY RUN
    os.environ["DRY_RUN"] = "True"
    os.environ["EXECUTION_VENUE"] = "IQ_OPTION"
    
    logger.info("🟢 Iniciando prueba de inyección de fuerza bruta (Dry Run)")
    
    # 2. Crear instancia del pipeline
    pipeline = NexusPipeline()
    await pipeline.initialize()
    
    # 3. Forzamos el salto de chequeos de límites para poder ejecutar la prueba de inmediato
    pipeline._daily_trades = 0
    pipeline._last_trade_time = 0.0
    
    # Generar data sintética
    df_synthetic = create_synthetic_data()
    logger.info("📊 Data sintética generada para forzar señal de STRONG_BUY.")
    
    # 4. Mockear el suministro de datos para inyectar nuestro DataFrame
    async def mock_get_market_data():
        return "SYNTHETIC_BTC", df_synthetic
    
    pipeline._get_market_data = mock_get_market_data
    
    # 5. Ejecutar UN tick del pipeline
    logger.info("💉 Inyectando datos en el Signal Engine a través del Pipeline _tick...")
    await pipeline._tick()
    
    logger.info("✅ Simulación finalizada. Revisa los logs de DRY RUN TRADE SIMULADO arriba.")
    
    await pipeline.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
