"""
NEXUS Trading System — Integration Test
==========================================
Test con datos mockeados para verificar el flujo completo:
  BULL → BEAR → ÁRBITRO

Ejecutar: python tests/test_agent_flow.py
"""

import sys
import os

# Fix Windows console encoding
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Añadir raíz del proyecto al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.agent_bull import AgentBull
from agents.agent_bear import AgentBear
from agents.agent_arbitro import AgentArbitro


def separator(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ══════════════════════════════════════════════════════════════════════
#  Datos mockeados
# ══════════════════════════════════════════════════════════════════════

MOCK_PRICE = 67_500.0

MOCK_SIGNAL_BULLISH = {
    "signal": "BUY",
    "confidence": 0.78,
    "reason": "BUY por 3/5 indicadores",
    "indicators": {
        "RSI": {"direction": "BUY", "value": 28.5, "detail": "RSI(14)=28.5 < 30 → sobreventa"},
        "MACD": {"direction": "BUY", "value": 0.0012, "detail": "MACD cruce alcista"},
        "Bollinger": {"direction": "BUY", "value": 0.05, "detail": "Precio tocó banda inferior"},
        "EMA_Cross": {"direction": "NEUTRAL", "value": 1.2, "detail": "EMA50 > EMA200"},
        "Volume": {"direction": "BUY", "value": 2.5, "detail": "Volumen anómalo alcista"},
    },
}

MOCK_SIGNAL_BEARISH = {
    "signal": "SELL",
    "confidence": 0.82,
    "reason": "SELL por 4/5 indicadores",
    "indicators": {
        "RSI": {"direction": "SELL", "value": 78.3, "detail": "RSI(14)=78.3 > 70 → sobrecompra"},
        "MACD": {"direction": "SELL", "value": -0.0023, "detail": "MACD cruce bajista"},
        "Bollinger": {"direction": "SELL", "value": 0.97, "detail": "Precio tocó banda superior"},
        "EMA_Cross": {"direction": "SELL", "value": -2.1, "detail": "Death Cross"},
        "Volume": {"direction": "SELL", "value": 3.1, "detail": "Volumen anómalo bajista"},
    },
}

MOCK_SIGNAL_NEUTRAL = {
    "signal": "HOLD",
    "confidence": 0.0,
    "reason": "Sin consenso",
    "indicators": {
        "RSI": {"direction": "NEUTRAL", "value": 52.0, "detail": "RSI neutral"},
        "MACD": {"direction": "NEUTRAL", "value": 0.0001, "detail": "MACD positivo sin cruce"},
        "Bollinger": {"direction": "NEUTRAL", "value": 0.5, "detail": "Dentro de bandas"},
        "EMA_Cross": {"direction": "NEUTRAL", "value": 0.3, "detail": "Tendencia alcista leve"},
        "Volume": {"direction": "NEUTRAL", "value": 1.1, "detail": "Volumen normal"},
    },
}

MOCK_ONCHAIN_BULLISH = {
    "exchange_inflow": 500,
    "exchange_outflow": 1200,
    "active_addresses": 1_100_000,
    "active_addresses_prev": 1_000_000,
    "fear_greed_index": 22,
    "sentiment_score": 0.5,
    "nvt_ratio": 35,
}

MOCK_ONCHAIN_BEARISH = {
    "exchange_inflow": 1800,
    "exchange_outflow": 600,
    "fear_greed_index": 85,
    "sentiment_score": -0.7,
    "nvt_ratio": 150,
    "hash_rate": 400,
    "hash_rate_prev": 500,
    "whale_alerts": [
        {"amount": 500, "to_exchange": True},
        {"amount": 300, "to_exchange": True},
        {"amount": 150, "to_exchange": False},
    ],
}

MOCK_RISK_LOW = {
    "max_drawdown": 0.03,
    "var_95": 0.02,
    "current_exposure": 0.25,
    "sharpe_ratio": 1.8,
}

MOCK_RISK_HIGH = {
    "max_drawdown": 0.12,
    "var_95": 0.08,
    "current_exposure": 0.85,
}

MOCK_RISK_TIE = {
    "max_drawdown": 0.05,
    "var_95": 0.04,
    "current_exposure": 0.40,
}


# ══════════════════════════════════════════════════════════════════════
#  Tests
# ══════════════════════════════════════════════════════════════════════

def test_bull_build_argument():
    separator("TEST 1: AgentBull.build_argument()")
    bull = AgentBull()
    result = bull.build_argument(MOCK_PRICE, MOCK_SIGNAL_BULLISH, MOCK_ONCHAIN_BULLISH)

    print(f"  stance   = {result['stance']}")
    print(f"  strength = {result['strength']} (type: {type(result['strength']).__name__})")
    print(f"  argument = {result['argument'][:120]}...")

    assert result["stance"] == "BULL", f"Expected BULL, got {result['stance']}"
    assert isinstance(result["strength"], int), f"strength should be int, got {type(result['strength'])}"
    assert 0 <= result["strength"] <= 10, f"strength out of range: {result['strength']}"
    assert isinstance(result["argument"], str) and len(result["argument"]) > 0
    print("  ✅ PASSED")
    return result


def test_bear_build_argument():
    separator("TEST 2: AgentBear.build_argument()")
    bear = AgentBear()
    result = bear.build_argument(MOCK_PRICE, MOCK_SIGNAL_BEARISH, MOCK_ONCHAIN_BEARISH)

    print(f"  stance   = {result['stance']}")
    print(f"  strength = {result['strength']} (type: {type(result['strength']).__name__})")
    print(f"  argument = {result['argument'][:120]}...")

    assert result["stance"] == "BEAR", f"Expected BEAR, got {result['stance']}"
    assert isinstance(result["strength"], int), f"strength should be int, got {type(result['strength'])}"
    assert 0 <= result["strength"] <= 10
    assert isinstance(result["argument"], str)

    # Verificar que whale alerts aparecen en el argumento
    assert "whale" in result["argument"].lower() or "🐋" in result["argument"], \
        "whale alerts should appear in bear argument"
    print("  ✅ PASSED (whale alerts detectadas)")
    return result


def test_bear_rsi_overbought_priority():
    separator("TEST 3: AgentBear RSI sobrecomprado (prioridad)")
    bear = AgentBear()
    # Señal con RSI alto pero neutral → el bear debe detectarlo
    signal = {
        "signal": "HOLD",
        "confidence": 0.0,
        "indicators": {
            "RSI": {"direction": "NEUTRAL", "value": 62, "detail": "RSI neutral zone"},
            "MACD": {"direction": "NEUTRAL", "value": 0, "detail": "neutral"},
            "Bollinger": {"direction": "NEUTRAL", "value": 0.5, "detail": "neutral"},
            "EMA_Cross": {"direction": "NEUTRAL", "value": -0.5, "detail": "neutral"},
            "Volume": {"direction": "NEUTRAL", "value": 1.0, "detail": "normal"},
        },
    }
    result = bear.build_argument(MOCK_PRICE, signal)
    assert "RSI" in result["argument"] or "sobrecompra" in result["argument"].lower(), \
        "RSI overbought should appear in bear argument"
    print(f"  strength = {result['strength']}")
    print("  ✅ PASSED (RSI detectado con valor 62)")


def test_arbitro_both_weak_hold():
    separator("TEST 4: Árbitro — ambos strength ≤ 4 → HOLD")
    arbitro = AgentArbitro()  # Sin LLM = fallback heurístico

    bull_state = {"stance": "BULL", "argument": "Argumento débil", "strength": 3}
    bear_state = {"stance": "BEAR", "argument": "Argumento débil", "strength": 4}

    result = arbitro.deliberate(bull_state, bear_state, MOCK_RISK_LOW)

    print(f"  decision = {result['decision']}")
    print(f"  confidence = {result['confidence']}")

    assert result["decision"] == "HOLD", f"Expected HOLD, got {result['decision']}"
    assert result["confidence"] == 0.0
    assert result["position_size_pct"] == 0.0
    print("  ✅ PASSED")


def test_arbitro_confidence_gate():
    separator("TEST 5: Árbitro — confidence < 0.65 → HOLD")
    arbitro = AgentArbitro()

    # Bull con strength 6, bear con 4 → diff=2 (< 3) → fallback heurístico → HOLD
    bull_state = {"stance": "BULL", "argument": "Argumento moderado", "strength": 6}
    bear_state = {"stance": "BEAR", "argument": "Argumento bajo", "strength": 4}

    result = arbitro.deliberate(bull_state, bear_state, {"var_95": 0.01})

    print(f"  decision  = {result['decision']}")
    print(f"  confidence = {result['confidence']}")
    print(f"  reasoning  = {result['reasoning'][:100]}...")

    # La confianza debería ser baja por empate → confidence gate bloquea
    if result["decision"] in ("BUY", "SELL"):
        assert result["confidence"] >= 0.65, \
            f"Should not execute with conf < 0.65, got {result['confidence']}"
    print("  ✅ PASSED")


def test_arbitro_var_tiebreak():
    separator("TEST 6: Árbitro — desempate por VaR")
    arbitro = AgentArbitro()

    # Strengths cercanos (diff < 3) con alto VaR → debería favorecer bear
    bull_state = {"stance": "BULL", "argument": "Argumento alcista medio", "strength": 6}
    bear_state = {"stance": "BEAR", "argument": "Argumento bajista medio", "strength": 5}

    result = arbitro.deliberate(
        bull_state, bear_state,
        risk_metrics={"var_95": 0.04, "max_drawdown": 0.05, "current_exposure": 0.40},
    )

    print(f"  decision  = {result['decision']}")
    print(f"  confidence = {result['confidence']}")
    print(f"  reasoning  = {result['reasoning'][:120]}...")

    assert "VaR" in result["reasoning"] or "Desempate" in result["reasoning"], \
        "VaR tie-breaking should be mentioned in reasoning"
    print("  ✅ PASSED (VaR mencionado en reasoning)")


def test_arbitro_risk_override():
    separator("TEST 7: Árbitro — risk override (drawdown > 10%)")
    arbitro = AgentArbitro()

    # Bull domina fuertemente pero drawdown > 10% → override a HOLD
    bull_state = {"stance": "BULL", "argument": "Bull fuerte", "strength": 9}
    bear_state = {"stance": "BEAR", "argument": "Bear débil", "strength": 3}

    result = arbitro.deliberate(bull_state, bear_state, MOCK_RISK_HIGH)

    print(f"  decision = {result['decision']}")
    print(f"  reasoning = {result['reasoning'][:100]}...")

    assert result["decision"] == "HOLD", \
        f"Should HOLD with drawdown > 10%, got {result['decision']}"
    assert "Override" in result["reasoning"] or "Drawdown" in result["reasoning"]
    print("  ✅ PASSED")


def test_full_flow_bullish():
    separator("TEST 8: Flujo completo BULL → BEAR → ÁRBITRO (escenario alcista)")
    bull = AgentBull()
    bear = AgentBear()
    arbitro = AgentArbitro()  # Sin LLM

    # 1. Bull analiza con señal alcista + on-chain alcista
    bull_result = bull.build_argument(MOCK_PRICE, MOCK_SIGNAL_BULLISH, MOCK_ONCHAIN_BULLISH)
    print(f"  Bull: stance={bull_result['stance']}, strength={bull_result['strength']}")

    # 2. Bear analiza la misma señal alcista (debería ser débil)
    bear_result = bear.build_argument(MOCK_PRICE, MOCK_SIGNAL_BULLISH, MOCK_ONCHAIN_BULLISH)
    print(f"  Bear: stance={bear_result['stance']}, strength={bear_result['strength']}")

    # 3. Árbitro delibera
    decision = arbitro.deliberate(
        bull.get_state(), bear.get_state(), MOCK_RISK_LOW
    )
    print(f"  Árbitro: decision={decision['decision']}, conf={decision['confidence']}, size={decision['position_size_pct']}%")
    print(f"  Reasoning: {decision['reasoning'][:120]}...")

    assert decision["decision"] in ("BUY", "SELL", "HOLD"), "Invalid decision"
    assert 0 <= decision["confidence"] <= 1
    assert 0 <= decision["position_size_pct"] <= 15
    print("  ✅ PASSED")


def test_full_flow_bearish():
    separator("TEST 9: Flujo completo BULL → BEAR → ÁRBITRO (escenario bajista)")
    bull = AgentBull()
    bear = AgentBear()
    arbitro = AgentArbitro()

    # 1. Bull analiza señal bajista (debería ser débil)
    bull_result = bull.build_argument(MOCK_PRICE, MOCK_SIGNAL_BEARISH, MOCK_ONCHAIN_BEARISH)
    print(f"  Bull: stance={bull_result['stance']}, strength={bull_result['strength']}")

    # 2. Bear analiza señal bajista + on-chain bajista (debería ser fuerte)
    bear_result = bear.build_argument(MOCK_PRICE, MOCK_SIGNAL_BEARISH, MOCK_ONCHAIN_BEARISH)
    print(f"  Bear: stance={bear_result['stance']}, strength={bear_result['strength']}")

    # 3. Árbitro delibera
    decision = arbitro.deliberate(
        bull.get_state(), bear.get_state(), MOCK_RISK_LOW
    )
    print(f"  Árbitro: decision={decision['decision']}, conf={decision['confidence']}, size={decision['position_size_pct']}%")
    print(f"  Reasoning: {decision['reasoning'][:120]}...")

    assert decision["decision"] in ("BUY", "SELL", "HOLD")
    print("  ✅ PASSED")


def test_debate_history():
    separator("TEST 10: Historial de debates")
    arbitro = AgentArbitro()

    for i in range(3):
        bull_state = {"stance": "BULL", "argument": f"arg {i}", "strength": 5 + i}
        bear_state = {"stance": "BEAR", "argument": f"arg {i}", "strength": 4}
        arbitro.deliberate(bull_state, bear_state, MOCK_RISK_LOW)

    history = arbitro.get_debate_history(limit=5)
    print(f"  Debates registrados: {len(history)}")
    for h in history:
        print(f"    {h['decision']} (conf={h['confidence']}) bull={h['bull_strength']} bear={h['bear_strength']}")

    assert len(history) == 3, f"Expected 3 debates, got {len(history)}"
    print("  ✅ PASSED")


# ══════════════════════════════════════════════════════════════════════
#  Runner
# ══════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "🧪" * 30)
    print("  NEXUS Agent Integration Test Suite")
    print("🧪" * 30)

    tests = [
        test_bull_build_argument,
        test_bear_build_argument,
        test_bear_rsi_overbought_priority,
        test_arbitro_both_weak_hold,
        test_arbitro_confidence_gate,
        test_arbitro_var_tiebreak,
        test_arbitro_risk_override,
        test_full_flow_bullish,
        test_full_flow_bearish,
        test_debate_history,
    ]

    passed = 0
    failed = 0
    errors = []

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((test_fn.__name__, str(e)))
            print(f"  ❌ FAILED: {e}")

    separator("RESULTADO FINAL")
    print(f"  ✅ Passed: {passed}/{len(tests)}")
    print(f"  ❌ Failed: {failed}/{len(tests)}")

    if errors:
        print("\n  Errores:")
        for name, err in errors:
            print(f"    - {name}: {err}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
