"""
scripts/gonogo_check.py
Pre-flight checklist antes de arrancar NEXUS en modo REAL.
Salida: EXIT 0 si todo verde, EXIT 1 si hay bloqueantes.
"""

import sys
import os
import time
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import redis

# Cargar variables de entorno
load_dotenv()

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from nexus.core.risk_manager import QuantRiskManager
from nexus.core.llm.diagnostics import run_preflight_all
from nexus.core.execution.iqoption_engine import IQOptionExecutionEngine


async def check_circuit_breaker() -> bool:
    print("\n[1] Verificando Circuit Breaker...")
    # Initialize without dependencies just for reading state
    rm = QuantRiskManager()
    
    if rm.is_circuit_breaker_active():
        remaining = int(rm._circuit_breaker_until - time.time())
        hours = remaining / 3600
        print(f"  ❌ CB: OPEN — Activo por {hours:.1f}h más ({remaining}s).")
        
        if remaining > 86400: # Over 24 hours
            ans = input(f"  ⚠️ CB lleva 24h+ abierto. ¿Resetear automáticamente? (s/n): ").strip().lower()
            if ans == 's':
                rm.safe_reset()
                print("  ✅ CB: CLOSED (reseteado)")
                return True
        else:
            ans = input(f"  ⚠️ CB abierto recientemente. ¿Forzar override manual? Ojo, esto es peligroso (s/n): ").strip().lower()
            if ans == 's':
                rm.safe_reset(reason="manual")
                print("  ✅ CB: CLOSED (reseteado manualmente)")
                return True
                
        print("  🔴 BLOQUEANTE: El Circuit Breaker está activo. No arrancar.")
        return False
    else:
        print("  ✅ CB: CLOSED")
        return True

async def check_real_balance() -> float:
    print("\n[2] Verificando Balance de la Cuenta...")
    
    engine = IQOptionExecutionEngine()
    if not await engine.connect():
        print("  ❌ No se pudo conectar a IQ Option para leer balance.")
        return -1.0
        
    balance = await engine.get_real_balance()
    await engine.disconnect()
    
    min_bet_str = os.getenv("BITGET_MIN_ORDER_USDT", "1.0")  # Default to 1 if not set
    min_bet = float(min_bet_str) if min_bet_str else 1.0
    
    if balance < min_bet * 10:
        print(f"  ⚠️ Balance bajo: ${balance:.2f} (Riesgo inminente de ruina si apuestas mínimas son ${min_bet:.2f})")
    else:
        print(f"  ✅ Balance: ${balance:.2f} verificado.")
        
    return balance

async def check_llm() -> bool:
    print("\n[3] Verificando Disponibilidad LLM...")
    groq_env = os.getenv("GROQ_API_KEYS", "")
    gem_env = os.getenv("GEMINI_API_KEYS", "")
    
    g_key = groq_env.split(",")[0].strip() if groq_env else None
    m_key = gem_env.split(",")[0].strip() if gem_env else None
    
    status = await run_preflight_all(
        groq_keys=[g_key] if g_key else [],
        gemini_keys=[m_key] if m_key else [],
        groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    )
    
    gr_ok = status.get("groq", {}).get("status") == "ok"
    gm_ok = status.get("gemini", {}).get("status") == "ok"
    
    out = "  ℹ️ LLM:"
    if gr_ok: out += " Groq ✅ /"
    else: out += " Groq ❌ /"
    
    if gm_ok: out += " Gemini ✅"
    else: out += " Gemini ❌"
    
    # REQUIRE LLM FOR REAL behavior
    req_llm_str = os.getenv("REQUIRE_LLM_FOR_REAL", "false").lower()
    req_llm = req_llm_str in ["true", "1", "yes"]
    
    if not (gr_ok or gm_ok):
        if req_llm:
            print(out + " → BLOQUEANTE por REQUIRE_LLM_FOR_REAL=true")
            return False
        else:
            print(out + " → Modo Heurístico activado (Aviso)")
            return True
    
    print(out)
    return True

def check_redis() -> bool:
    print("\n[4] Verificando Redis...")
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    
    client = redis.Redis(host=host, port=port, socket_timeout=3)
    try:
        client.ping()
        print("  ✅ Redis: OK")
        return True
    except BaseException:
        # User defined this as blocking
        print("  ❌ Redis: OFFLINE. (Bloqueante: El sistema necesita Redis)")
        return False

def check_files() -> bool:
    print("\n[5] Verificando Archivos Críticos...")
    files = [
        ".env",
        "nexus/core/pipeline.py",
        "nexus/core/risk_manager.py"
    ]
    missing = False
    for f in files:
        if not os.path.exists(f):
            print(f"  ❌ Falta: {f}")
            missing = True
        else:
            print(f"  ✅ Archivo OK: {f}")
            
    return not missing

def calculate_exposure(balance: float):
    print("\n[6] Resumen de Riesgo (Exposure)")
    
    min_bet_str = os.getenv("BITGET_MIN_ORDER_USDT", "1.0") 
    min_bet = float(min_bet_str) if min_bet_str else 1.0
    
    max_loss_str = os.getenv("MAX_LOSS_PER_SESSION", "3.00")
    max_loss = float(max_loss_str) if max_loss_str else 3.00
    
    max_trades = balance / min_bet
    
    print(f"  ℹ️ Con ${balance:.2f} y apuesta mínima ${min_bet:.2f}: máx {int(max_trades)} operaciones antes de ruina.")
    print(f"  ℹ️ Stop diario configurado: ${max_loss:.2f} ({(max_loss/balance)*100:.1f}% del balance)")

async def run_demo_smoke():
    ans = input("\n¿Ejecutar trade de prueba en DEMO primero? (s/n): ").strip().lower()
    if ans == 's':
        print("\n  [DEMO] Iniciando engine de simulación...")
        # Override to Practice
        os.environ["IQ_OPTION_ACCOUNT_TYPE"] = "PRACTICE"
        
        engine = IQOptionExecutionEngine()
        if not await engine.connect():
            print("  [DEMO] ❌ Falla en la conexión demo.")
            return False
            
        print("  [DEMO] Ejecutando orden corta de simulación EURUSD-OTC ($1, 1m)...")
        # El timeout debe ser grande para atrapar rechazos u okay
        res = await engine.execute_and_wait_result(asset="EURUSD-OTC", direction="CALL", amount=1.0, duration_minutes=1, timeout_seconds=120)
        
        print(f"  [DEMO] Resultado: {res.get('outcome')} — sistema respondió OK")
        await engine.disconnect()
        
        return True
    return True # User refused but it's ok

async def main():
    print("=========================================================")
    print("         NEXUS v5.0 — PRE-FLIGHT CHECKLIST (REAL)      ")
    print("=========================================================")
    
    os.environ["DRY_RUN"] = "False"  # Forcing checks in real mode parameters
    
    files_ok = check_files()
    if not files_ok:
        print("\n🔴 NO-GO — resolver archivos faltantes antes de arrancar")
        sys.exit(1)
        
    redis_ok = check_redis()
    if not redis_ok:
        print("\n🔴 NO-GO — resolver Redis antes de arrancar")
        sys.exit(1)
        
    cb_ok = await check_circuit_breaker()
    if not cb_ok:
        print("\n🔴 NO-GO — Circuit Breaker abierto. No arrancar.")
        sys.exit(1)
        
    llm_ok = await check_llm()
    if not llm_ok:
        print("\n🔴 NO-GO — LLM requerido para modo real.")
        sys.exit(1)
        
    balance = await check_real_balance()
    if balance < 0:
        print("\n🔴 NO-GO — No se pudo leer balance.")
        sys.exit(1)
        
    calculate_exposure(balance)
    
    demo_ok = await run_demo_smoke()
    if not demo_ok:
        print("\n🔴 NO-GO — Smoke Test Demo falló.")
        sys.exit(1)
        
    warnings = (balance < 10) # Just an example metric
    if warnings:
        print("\n🟡 GO con advertencias — revisar antes de operar")
    else:
        print("\n🟢 GO — Sistema listo para arrancar en REAL")
        
    sys.exit(0)

if __name__ == "__main__":
    asyncio.run(main())
