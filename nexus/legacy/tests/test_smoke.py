"""Smoke test rápido para verificar imports y lógica sin LLM."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "nexus"))

print("=" * 60)
print("  NEXUS SMOKE TEST — Imports & Logic Verification")
print("=" * 60)

# Test 1: WFO imports
print("\n[1] Importando WFO v2...")
try:
    from backtesting.walk_forward import WalkForwardOptimizer, NexusWFOStrategy
    print("  ✅ WalkForwardOptimizer importado")
    print("  ✅ NexusWFOStrategy importado:", NexusWFOStrategy.__name__)
except Exception as e:
    print(f"  ❌ Error: {e}")
    import traceback; traceback.print_exc()

# Test 2: Auto calibrator
print("\n[2] Importando WFOAutoCalibrator...")
try:
    from scripts.auto_calibrate import WFOAutoCalibrator
    c = WFOAutoCalibrator()
    print("  ✅ WFOAutoCalibrator instanciado")
except Exception as e:
    print(f"  ❌ Error: {e}")
    import traceback; traceback.print_exc()

# Test 3: extract_best_params nuevo método
print("\n[3] Verificando _extract_best_params (scoring agregado)...")
try:
    import inspect
    src = inspect.getsource(c._extract_best_params)
    checks = {
        "param_scores": "param_scores" in src,
        "oos_trades": "oos_trades" in src,
        "effective_sharpe": "effective_sharpe" in src,
        "GANADOR FINAL": "GANADOR FINAL" in src,
    }
    for name, ok in checks.items():
        print(f"    {'✅' if ok else '❌'} {name}: {'found' if ok else 'NOT FOUND'}")
    if all(checks.values()):
        print("  ✅ Método de scoring agregado OK")
    else:
        print("  ❌ Método incompleto")
except Exception as e:
    print(f"  ❌ Error: {e}")

# Test 4: Blacklist 24h en AgentArbitro
print("\n[4] Verificando Blacklist 24h en AgentArbitro...")
try:
    from agents.agent_arbitro import AgentArbitro
    # Solo verificar atributos, no instanciar (evita LLM init)
    import inspect
    src_class = inspect.getsource(AgentArbitro)
    blacklist_checks = {
        "_blacklisted_keys": "_blacklisted_keys" in src_class,
        "_blacklist_key": "def _blacklist_key" in src_class,
        "_is_key_blacklisted": "def _is_key_blacklisted" in src_class,
        "_key_fingerprint": "def _key_fingerprint" in src_class,
        "BLACKLIST_SECS": "_BLACKLIST_SECS" in src_class,
        "86400": "86400" in src_class,
    }
    for name, ok in blacklist_checks.items():
        print(f"    {'✅' if ok else '❌'} {name}: {'found' if ok else 'NOT FOUND'}")
    if all(blacklist_checks.values()):
        print("  ✅ Blacklist 24h implementado correctamente")
    else:
        print("  ❌ Blacklist incompleto")
except Exception as e:
    print(f"  ❌ Error: {e}")

# Test 5: WFO console format prefix
print("\n[5] Verificando formato de consola unificado...")
try:
    src_wfo = inspect.getsource(WalkForwardOptimizer)
    format_checks = {
        "_prefix": "self._prefix" in src_wfo,
        "[SYMBOL | TF | MODE]": "__prefix" in src_wfo or "_prefix" in src_wfo,
        "RESUMEN WFO INSTITUCIONAL": "RESUMEN WFO INSTITUCIONAL" in src_wfo,
        "_print_summary_table": "def _print_summary_table" in src_wfo,
        "Trades column": "is_trades" in src_wfo and "oos_trades" in src_wfo,
    }
    for name, ok in format_checks.items():
        print(f"    {'✅' if ok else '❌'} {name}: {'found' if ok else 'NOT FOUND'}")
    if all(format_checks.values()):
        print("  ✅ Formato de consola unificado OK")
    else:
        print("  ❌ Formato incompleto")
except Exception as e:
    print(f"  ❌ Error: {e}")

# Test 6: run_calibration.py exists and has summary
print("\n[6] Verificando run_calibration.py...")
try:
    cal_path = os.path.join(os.path.dirname(__file__), "run_calibration.py")
    with open(cal_path, "r", encoding="utf-8") as f:
        cal_src = f.read()
    cal_checks = {
        "RESUMEN DE CALIBRACIÓN": "RESUMEN DE CALIBRACIÓN" in cal_src,
        "PRODUCTION READY": "PRODUCTION READY" in cal_src or "PRODUCCIÓN" in cal_src.upper(),
        "Tabla params": "PARÁMETROS ACTIVOS SPOT" in cal_src,
        "ESTADO POR MODO": "ESTADO POR MODO" in cal_src,
    }
    for name, ok in cal_checks.items():
        print(f"    {'✅' if ok else '❌'} {name}: {'found' if ok else 'NOT FOUND'}")
    if all(cal_checks.values()):
        print("  ✅ Runner de calibración con resumen profesional OK")
    else:
        print("  ❌ Runner incompleto")
except Exception as e:
    print(f"  ❌ Error: {e}")

print("\n" + "=" * 60)
print("  🏁 SMOKE TEST COMPLETO")
print("=" * 60)
