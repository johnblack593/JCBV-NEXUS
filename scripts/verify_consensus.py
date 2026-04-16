import sys, ast, pathlib, asyncio

# 1. Verificar que consensus_engine.py existe y es importable
ce_path = pathlib.Path("nexus/core/consensus_engine.py")
assert ce_path.exists(), "❌ consensus_engine.py no encontrado"
print("✅ consensus_engine.py existe")

# 2. Verificar que NO hay regime_factor en pipeline.py
pipeline_src = pathlib.Path("nexus/core/pipeline.py").read_text(encoding="utf-8")
assert "regime_factor" not in pipeline_src, \
    "❌ regime_factor todavía presente en pipeline.py — eliminar"
print("✅ regime_factor eliminado de pipeline.py")

# 3. Verificar umbral dinámico implementado
assert "THRESHOLDS" in pipeline_src and "YELLOW" in pipeline_src, \
    "❌ Umbral dinámico THRESHOLDS no encontrado en pipeline.py"
print("✅ Umbral dinámico THRESHOLDS presente")

# 4. Verificar cooldown adaptativo
assert "_last_trade_result" in pipeline_src, \
    "❌ Cooldown adaptativo: _last_trade_result no encontrado"
print("✅ Cooldown adaptativo implementado")

# 5. Verificar ConsensusEngine integrado en pipeline
assert "consensus_engine" in pipeline_src and \
       "ConsensusEngine" in pipeline_src, \
    "❌ ConsensusEngine no integrado en pipeline.py"
print("✅ ConsensusEngine integrado en pipeline.py")

# 6. Test unitario rápido del ConsensusEngine (sin API real)
sys.path.insert(0, ".")
from nexus.core.consensus_engine import ConsensusEngine, AssetScore
import time as _time

async def _mock_analyze(df):
    return {"signal": "BUY", "confidence": 0.68,
            "payout": 85.0, "indicators": {"atr": 0.00012}}

async def _mock_data(symbol):
    import pandas as pd, numpy as np
    return pd.DataFrame({
        "open":  np.random.rand(50),
        "high":  np.random.rand(50),
        "low":   np.random.rand(50),
        "close": np.random.rand(50),
        "volume": np.ones(50),
    })

async def _run_test():
    ce = ConsensusEngine()
    best = await ce.evaluate(
        watchlist=["EURUSD-OTC", "GBPUSD-OTC"],
        get_data_fn=_mock_data,
        analyze_fn=_mock_analyze,
        min_conf=0.62,
        min_payout=80.0,
        atr_floor=0.00005,
    )
    assert best is not None, "❌ ConsensusEngine retornó None con señales válidas"
    assert best.composite > 0, "❌ composite score es 0"
    print(f"✅ ConsensusEngine test: MEJOR={best.symbol} composite={best.composite:.3f}")
    
    # Test TTL
    old_score = AssetScore("TEST", 0.7, "BUY", 85.0, 0.0001, 0.65)
    old_score.timestamp = _time.time() - 50  # expirado
    assert old_score.is_expired, "❌ TTL de señal no funciona"
    print("✅ Signal TTL (45s) funciona correctamente")

asyncio.run(_run_test())

print("\n🏆 TODOS LOS CHECKS PASARON — NEXUS ConsensusEngine operativo")
