"""
NEXUS — Verificación de módulos
Corre este archivo para confirmar que todo el sistema está operativo.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

resultados = []

# ── TEST 1: Imports generales ──────────────────────────────
print("\n🔍 TEST 1: Verificando imports...")
try:
    from config.settings import (
        BINANCE_API_KEY, BINANCE_API_SECRET,
        trading_config, risk_config, ml_config
    )
    assert BINANCE_API_KEY != "", "API Key vacía"
    print("  ✅ config.settings → OK")
    resultados.append(True)
except Exception as e:
    print(f"  ❌ config.settings → {e}")
    resultados.append(False)

# ── TEST 2: Data Handler ───────────────────────────────────
print("\n🔍 TEST 2: Verificando Data Handler...")
try:
    from core.data_handler import BinanceDataHandler
    handler = BinanceDataHandler()
    print("  ✅ BinanceDataHandler → instanciado OK")
    resultados.append(True)
except Exception as e:
    print(f"  ❌ BinanceDataHandler → {e}")
    resultados.append(False)

# ── TEST 3: Signal Engine ──────────────────────────────────
print("\n🔍 TEST 3: Verificando Signal Engine...")
try:
    from core.signal_engine import TechnicalSignalEngine
    engine = TechnicalSignalEngine()
    print("  ✅ TechnicalSignalEngine → instanciado OK")
    resultados.append(True)
except Exception as e:
    print(f"  ❌ TechnicalSignalEngine → {e}")
    resultados.append(False)

# ── TEST 4: Risk Manager ───────────────────────────────────
print("\n🔍 TEST 4: Verificando Risk Manager...")
try:
    from core.risk_manager import QuantRiskManager
    rm = QuantRiskManager()
    kelly = rm.kelly_criterion(0.55, 2.0, 1.0)
    assert kelly <= 0.25, f"Kelly no cappea en 0.25: {kelly}"
    print(f"  ✅ QuantRiskManager → Kelly test OK ({kelly:.4f})")
    resultados.append(True)
except Exception as e:
    print(f"  ❌ QuantRiskManager → {e}")
    resultados.append(False)

# ── TEST 5: Agentes ────────────────────────────────────────
print("\n🔍 TEST 5: Verificando Agentes...")
try:
    from agents.agent_bull import AgentBull
    from agents.agent_bear import AgentBear
    from agents.agent_arbitro import AgentArbitro
    print("  ✅ AgentBull / AgentBear / AgentArbitro → importados OK")
    resultados.append(True)
except Exception as e:
    print(f"  ❌ Agentes → {e}")
    resultados.append(False)

# ── TEST 6: Telegram ───────────────────────────────────────
print("\n🔍 TEST 6: Verificando Telegram config...")
try:
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if TELEGRAM_TOKEN:
        print("  ✅ Telegram token → configurado")
    else:
        print("  ⚠️  Telegram token → vacío (configúralo más adelante)")
    resultados.append(True)
except Exception as e:
    print(f"  ❌ Telegram → {e}")
    resultados.append(False)

# ── RESUMEN ────────────────────────────────────────────────
ok = sum(resultados)
total = len(resultados)
print("\n" + "─"*45)
print(f"  NEXUS Verificación: ✅ {ok}/{total} módulos OK")
if ok == total:
    print("  🚀 Sistema listo para backtesting")
else:
    print(f"  ⚠️  {total - ok} módulo(s) requieren atención")
print("─"*45 + "\n")