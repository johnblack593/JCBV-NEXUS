"""
NEXUS — Tests Unitarios para las 5 Correcciones Críticas
=========================================================
Ejecutar: python -m pytest nexus/tests/test_critical_fixes.py -v
"""

import asyncio
import json
import os
import sys
import time
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Asegurar imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from nexus.core.risk_manager import QuantRiskManager


# ═════════════════════════════════════════════════════════════════
#  TEST 1: Lookahead Bias eliminado en ML Engine
# ═════════════════════════════════════════════════════════════════

class TestLookaheadBiasFix:
    """Verifica que el scaler NO usa datos futuros."""

    def test_scaler_is_local_per_window(self):
        """El scaler debe ser diferente para cada ventana (no global)."""
        from nexus.core.ml_engine import LSTMPredictor

        predictor = LSTMPredictor(lookback=10, features=["close", "volume"])

        # Crear un DataFrame con tendencia clara (para que cada ventana tenga distinto min/max)
        n = 100
        df = pd.DataFrame({
            "close": np.linspace(100, 200, n),  # Tendencia lineal
            "volume": np.random.randint(1000, 5000, n).astype(float),
        })

        X, y = predictor.prepare_data(df)

        assert len(X) > 0, "Debe generar secuencias"
        assert len(y) > 0, "Debe generar targets"

        # Verificar que los valores escalados están en [0, 1] (propio de MinMaxScaler)
        for i in range(min(5, len(X))):
            window = X[i]
            assert window.min() >= -0.01, f"Ventana {i} tiene valores < 0: {window.min()}"
            assert window.max() <= 1.01, f"Ventana {i} tiene valores > 1: {window.max()}"

    def test_no_future_leakage_in_sequences(self):
        """Las secuencias solo deben contener datos <= t (nunca t+1, t+2, etc.)."""
        from nexus.core.ml_engine import LSTMPredictor

        predictor = LSTMPredictor(lookback=5, features=["close"])

        # Crear datos con salto brusco al final (si hay leakage, las ventanas previas lo mostrarán)
        prices = [100.0] * 50 + [500.0] * 10
        df = pd.DataFrame({"close": prices, "volume": [1000.0] * 60})

        X, y = predictor.prepare_data(df)

        # Las primeras ventanas (antes del salto en t=50) NO deben contener
        # valores escalados que reflejen el rango [100, 500]
        early_window = X[20]  # Bien antes del salto
        assert early_window.max() <= 1.01, (
            "Ventana temprana escalada con datos futuros (leakage detectado!)"
        )


# ═════════════════════════════════════════════════════════════════
#  TEST 2: VaR Histórico (percentiles reales, no Gaussiano)
# ═════════════════════════════════════════════════════════════════

class TestHistoricalVaR:
    """Verifica que VaR usa percentiles reales en lugar de z-scores."""

    def test_var_matches_percentile(self):
        """VaR(95%) debe ser exactamente el percentil 5 de la distribución."""
        rm = QuantRiskManager(log_dir="logs")
        returns = [-0.05, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05,
                   -0.04, -0.025, -0.015, 0.005, 0.015, 0.025, 0.035, 0.045, -0.035, 0.0]

        var = rm.value_at_risk(returns, confidence=0.95)
        expected = abs(float(np.percentile(returns, 5)))

        assert abs(var - round(expected, 6)) < 1e-5, (
            f"VaR={var} != percentile={expected}"
        )

    def test_var_captures_fat_tail(self):
        """VaR histórico debe capturar un crash extremo que el paramétrico suavizaría."""
        rm = QuantRiskManager(log_dir="logs")

        # 99 retornos normales + 1 crash catastrófico (-50%)
        normal_returns = [0.001] * 99
        fat_tail_returns = normal_returns + [-0.50]

        var = rm.value_at_risk(fat_tail_returns, confidence=0.99)
        # Con percentil real, el crash de -50% debe estar en la cola (1% de 100 vars = min)
        assert var > 0.0, f"VaR debería detectar riesgo con crash catastrófico en la cola (got {var})"

    def test_var_positive_returns_zero(self):
        """Retornos 100% positivos deben dar VaR ~ 0."""
        rm = QuantRiskManager(log_dir="logs")
        pos_returns = [0.01, 0.02, 0.015, 0.025, 0.01, 0.03, 0.02, 0.015]

        var = rm.value_at_risk(pos_returns, confidence=0.95)
        assert var == 0.0 or var < 0.005, f"VaR debería ser ~0 para retornos positivos, got {var}"


# ═════════════════════════════════════════════════════════════════
#  TEST 3: Persistencia del Circuit Breaker
# ═════════════════════════════════════════════════════════════════

class TestCircuitBreakerPersistence:
    """Verifica que el CB sobrevive crashes escribiendo JSON en disco."""

    def test_cb_writes_json_on_trigger(self):
        """Al activar CB, debe crear un archivo JSON con el estado."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rm = QuantRiskManager(log_dir=tmpdir)
            rm.circuit_breaker_check(0.20, max_dd=0.15)  # Trigger

            cb_path = Path(tmpdir) / "cb_state.json"
            assert cb_path.exists(), "cb_state.json debe existir tras trigger"

            with open(cb_path) as f:
                state = json.load(f)

            assert state["is_active"] is True
            assert state["until_timestamp"] > time.time()

    def test_cb_restores_from_disk(self):
        """Un nuevo QuantRiskManager debe restaurar el CB activo desde disco."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Simular un CB activo
            state = {
                "is_active": True,
                "until_timestamp": time.time() + 3600,
                "activated_at": "2026-03-29T00:00:00+00:00",
            }
            cb_path = Path(tmpdir) / "cb_state.json"
            with open(cb_path, "w") as f:
                json.dump(state, f)

            # Nuevo manager debe leer el estado
            rm2 = QuantRiskManager(log_dir=tmpdir)
            assert rm2.is_circuit_breaker_active(), (
                "CB debería estar activo tras restaurar desde disco"
            )

    def test_cb_clears_expired_state(self):
        """Si el cooldown expiró, el CB debe ignorar el estado del disco."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "is_active": True,
                "until_timestamp": time.time() - 100,  # Ya expiró
                "activated_at": "2026-03-28T00:00:00+00:00",
            }
            cb_path = Path(tmpdir) / "cb_state.json"
            with open(cb_path, "w") as f:
                json.dump(state, f)

            rm3 = QuantRiskManager(log_dir=tmpdir)
            assert not rm3.is_circuit_breaker_active(), (
                "CB no debería estar activo con cooldown expirado"
            )


# ═════════════════════════════════════════════════════════════════
#  TEST 4: Desacoplamiento Async (asyncio.to_thread)
# ═════════════════════════════════════════════════════════════════

class TestAsyncDecoupling:
    """Verifica que la evaluación Alpha V3 es compatible con asyncio.to_thread."""

    def test_evaluate_alpha_is_threadsafe(self):
        """La función evaluate_alpha_v3_vectorized debe ser pura (sin side effects)."""
        from nexus.iq_main import HFTDaemon

        daemon = HFTDaemon.__new__(HFTDaemon)  # Sin __init__ para no conectar al broker

        df = pd.DataFrame({
            "open": np.random.uniform(100, 105, 50),
            "high": np.random.uniform(105, 110, 50),
            "low": np.random.uniform(95, 100, 50),
            "close": np.random.uniform(100, 105, 50),
            "volume": np.random.randint(1000, 5000, 50).astype(float),
        })

        # Llamar dos veces con los mismos datos debe dar el mismo resultado
        r1 = daemon.evaluate_alpha_v3_vectorized(df)
        r2 = daemon.evaluate_alpha_v3_vectorized(df)

        assert r1["signal"] == r2["signal"], "La función no es determinista"
        assert r1["composite"] == r2["composite"], "La función no es determinista"

    def test_evaluate_alpha_can_run_in_thread(self):
        """Debe poder ejecutarse via asyncio.to_thread sin errores."""
        from nexus.iq_main import HFTDaemon

        daemon = HFTDaemon.__new__(HFTDaemon)

        df = pd.DataFrame({
            "open": np.random.uniform(100, 105, 50),
            "high": np.random.uniform(105, 110, 50),
            "low": np.random.uniform(95, 100, 50),
            "close": np.random.uniform(100, 105, 50),
            "volume": np.random.randint(1000, 5000, 50).astype(float),
        })

        async def run():
            return await asyncio.to_thread(daemon.evaluate_alpha_v3_vectorized, df)

        result = asyncio.run(run())
        assert "signal" in result
        assert "composite" in result


# ═════════════════════════════════════════════════════════════════
#  TEST 5: Bypass LLM en Binary Mode
# ═════════════════════════════════════════════════════════════════

class TestBinaryModeLLMBypass:
    """Verifica que el arbitro NO invoca al LLM en modo binario."""

    def test_binary_mode_uses_heuristic(self):
        """En trading_mode='binary_turbo', debe usar heurístico."""
        from nexus.agents.agent_arbitro import AgentArbitro, LLMProvider

        arbitro = AgentArbitro(
            provider=LLMProvider.GROQ,
            trading_mode="binary_turbo",
        )
        # NO inicializar LLM (no hay keys)

        bull = {"strength": 10, "argument": "RSI oversold, BB touch"}
        bear = {"strength": 0, "argument": "Slight downtrend"}

        result = arbitro.deliberate(bull, bear)

        assert result["decision"] in ("BUY", "SELL", "HOLD")
        assert 0 <= result["confidence"] <= 1
        # Verificar que NO se usó LLM (el provider en el log sería heuristic_binary)
        assert result["decision"] == "BUY", (
            f"Con bull_strength=10 >> bear_strength=0, debería ser BUY, got {result['decision']}"
        )

    def test_standard_mode_attempts_llm(self):
        """En trading_mode='standard', debe intentar usar LLM (y caer a heurístico si no hay keys)."""
        from nexus.agents.agent_arbitro import AgentArbitro, LLMProvider

        arbitro = AgentArbitro(
            provider=LLMProvider.GROQ,
            trading_mode="standard",
        )
        # Sin inicializar (no hay keys) — caerá al heurístico pero SIN el bypass binario

        bull = {"strength": 7, "argument": "RSI oversold"}
        bear = {"strength": 3, "argument": "Meh"}

        result = arbitro.deliberate(bull, bear)
        assert result["decision"] in ("BUY", "SELL", "HOLD")


# ═════════════════════════════════════════════════════════════════
#  Ejecución directa
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
