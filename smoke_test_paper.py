# smoke_test_paper.py
import asyncio, os, sys, logging, traceback

# Forzar encoding para terminales de Windows
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Forzar modo paper antes de cualquier import del proyecto
os.environ["DRY_RUN"]                 = "True"
os.environ["EXECUTION_VENUE"]         = "IQ_OPTION"
os.environ["ACTIVE_STRATEGY"]         = "BINARY_ML"
os.environ["IQ_OPTION_ACCOUNT_TYPE"]  = "PRACTICE"
os.environ["LOG_LEVEL"]               = "DEBUG"

# Crear carpeta de logs si no existe
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/smoke_test.log", encoding="utf-8"),
    ]
)

RESULTS = {}

async def run_check(name: str, coro):
    try:
        result = await coro
        RESULTS[name] = ("OK", result)
        print(f"  ✅ {name}: OK")
        return result
    except Exception as e:
        RESULTS[name] = ("FAIL", str(e))
        print(f"  ❌ {name}: FAIL")
        print(f"     Error: {e}")
        traceback.print_exc()
        return None

async def main():
    print("\n" + "═"*60)
    print("NEXUS v5.0 — SMOKE TEST PAPER TRADING")
    print("═"*60 + "\n")

    # ── Check 1: Import del pipeline ──────────────────────────────
    print("[ Check 1 ] Import NexusPipeline...")
    try:
        from nexus.core.pipeline import NexusPipeline
        RESULTS["import"] = ("OK", "")
        print("  ✅ import: OK")
    except Exception as e:
        RESULTS["import"] = ("FAIL", str(e))
        print(f"  ❌ Import FAIL: {e}")
        traceback.print_exc()
        print("\n⛔ Import crítico falló. No se puede continuar.")
        print("   Resuelve los errores de import del Paso 2 primero.")
        return

    pipeline = NexusPipeline()

    # ── Check 2: Inicialización de capas ──────────────────────────
    print("\n[ Check 2 ] Inicialización de las 5 capas...")
    init_ok = await run_check("initialize", pipeline.initialize())
    if init_ok is None:
        print("\n⛔ initialize() falló. Muestra el traceback completo.")
        await pipeline.shutdown()
        return

    # ── Check 3: Balance ──────────────────────────────────────────
    print("\n[ Check 3 ] Balance de cuenta PRACTICE...")
    async def check_balance():
        b = await pipeline.execution_engine.get_balance()
        assert b > 0, f"balance={b} — verifica credenciales IQ y que la cuenta sea PRACTICE"
        return b
    balance = await run_check("balance", check_balance())

    # ── Check 4: Régimen Macro ────────────────────────────────────
    print("\n[ Check 4 ] MacroAgent — régimen macro...")
    async def check_regime():
        from nexus.core.macro.macro_agent import MacroRegime
        regime = await pipeline.get_macro_regime()
        assert regime in (MacroRegime.GREEN, MacroRegime.YELLOW, MacroRegime.RED)
        return regime.value
    await run_check("macro_regime", check_regime())

    # ── Check 5: Circuit Breaker state ───────────────────────────
    print("\n[ Check 5 ] Circuit Breaker estado inicial...")
    async def check_cb():
        cb = pipeline.risk_manager.is_circuit_breaker_active()
        if cb:
            import os
            cb_path = "logs/cb_state.json"
            if os.path.exists(cb_path):
                os.remove(cb_path)
                return "CB activo → cb_state.json borrado. Reinicia el test."
            return "CB activo sin cb_state.json — revisar estado"
        return "CB inactivo (correcto)"
    await run_check("circuit_breaker", check_cb())

    # ── Check 6: Tick único en DRY RUN ────────────────────────────
    print("\n[ Check 6 ] Tick único en DRY_RUN=True...")
    async def check_tick():
        await asyncio.wait_for(pipeline._tick(), timeout=30.0)
        return "tick completado"
    await run_check("tick_dry_run", check_tick())

    # ── Check 7: Session stats ────────────────────────────────────
    print("\n[ Check 7 ] Session stats...")
    async def check_stats():
        stats = pipeline.get_session_stats()
        assert stats["venue"] == "IQ_OPTION"
        assert stats["signal_mode"] == "binary"
        return stats
    stats = await run_check("session_stats", check_stats())

    # ── Shutdown ──────────────────────────────────────────────────
    print("\n[ Shutdown ] Cerrando pipeline...")
    await pipeline.shutdown()
    print("  ✅ Shutdown: OK")

    # ── Resumen Final ─────────────────────────────────────────────
    print("\n" + "═"*60)
    print("RESUMEN SMOKE TEST")
    print("═"*60)
    passed  = [k for k, (s, _) in RESULTS.items() if s == "OK"]
    failed  = [k for k, (s, _) in RESULTS.items() if s == "FAIL"]
    for k in passed: print(f"  ✅ {k}")
    for k in failed:
        print(f"  ❌ {k}: {RESULTS[k][1]}")

    print()
    if not failed:
        print("🎯 SMOKE TEST PASSED — NEXUS listo para paper trading")
        print("   Siguiente paso: python main.py (con DRY_RUN=True)")
    else:
        print(f"⛔ SMOKE TEST FAILED — {len(failed)} check(s) fallaron")
        print("   Comparte este output completo para diagnóstico.")

    print("═"*60)
    print(f"Log completo guardado en: logs/smoke_test.log")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()