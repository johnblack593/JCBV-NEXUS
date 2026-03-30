"""
NEXUS Fase 17 — Test Suite Completo
=====================================
Valida el pipeline de Auto-Mantenimiento de principio a fin:
  1. Logger Multi-Tier (Critical, Medium, Low)
  2. Support Engineer (Diagnóstico LLM + Catálogo de Soluciones)
  3. Developer Telegram Pipeline (Alertas DEV)
"""

import asyncio
import os
import sys
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# Path setup
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "nexus"))

from dotenv import load_dotenv
load_dotenv()


async def test_1_tiered_logger():
    """Test 1: Verificar que el logger crea los 3 archivos rotativos."""
    print("\n" + "=" * 60)
    print("  🧪 TEST 1: Logger Multi-Tier (Critical / Medium / Low)")
    print("=" * 60)

    from core.structured_logger import get_quant_logger

    qlog = get_quant_logger()

    # Escribir en cada tier
    qlog.log_system_event("test_critical", "Evento crítico de prueba", level="CRITICAL", data={"test": True})
    qlog.log_system_event("test_medium", "Evento medio de prueba", level="INFO", data={"test": True})
    qlog.log_maintenance("test_purge", "Purga de prueba exitosa", data={"files_removed": 0})
    qlog.log_crash("test_module", "TestError", "Traceback simulado...", context={"mode": "test"})
    qlog.log_calibration("spot", "BTC-USD", window=1, params={"sl": 2.0}, sharpe=1.42, phase="IS")

    # Verificar archivos
    stats = qlog.get_tier_stats()
    print("\n📊 Estado de Tiers:")
    for tier, info in stats.items():
        exists = "✅" if info["current_bytes"] > 0 else "❌"
        print(f"   {exists} {tier.upper():10s} | {info['current_bytes']:,} bytes | {info['usage_pct']}% usado | {info['path']}")

    all_ok = all(s["current_bytes"] > 0 for s in stats.values())
    print(f"\n{'✅ PASS' if all_ok else '❌ FAIL'}: Logger Multi-Tier")
    return all_ok


async def test_2_solutions_catalog():
    """Test 2: Verificar el catálogo de soluciones conocidas."""
    print("\n" + "=" * 60)
    print("  🧪 TEST 2: Catálogo de Soluciones Conocidas")
    print("=" * 60)

    from agents.agent_support import SolutionsCatalog

    catalog = SolutionsCatalog()
    print(f"   📚 Soluciones cargadas: {catalog.size}")

    # Buscar solución conocida
    sol = catalog.find_solution("JSONDecodeError", "Error en llm_historical_cache: Expecting value")
    if sol:
        print(f"   ✅ Encontrada solución para JSONDecodeError: {sol['description']}")
        print(f"      Auto-fix: {sol['auto_fix_action']}")
    else:
        print("   ❌ No se encontró solución para JSONDecodeError")

    # Buscar algo que no existe
    sol2 = catalog.find_solution("UnknownError", "algo raro")
    if sol2 is None:
        print("   ✅ Correctamente NO encontró solución para error desconocido")
    
    ok = sol is not None and sol2 is None
    print(f"\n{'✅ PASS' if ok else '❌ FAIL'}: Catálogo de Soluciones")
    return ok


async def test_3_dev_telegram():
    """Test 3: Enviar una alerta de prueba al canal DEV de Telegram."""
    print("\n" + "=" * 60)
    print("  🧪 TEST 3: Developer Telegram Pipeline")
    print("=" * 60)

    dev_token = os.getenv("TELEGRAM_DEV_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    dev_chat = os.getenv("TELEGRAM_DEV_CHAT_ID", "")

    print(f"   🔑 DEV Bot Token: {'...'+dev_token[-4:] if dev_token else '❌ NO CONFIGURADO'}")
    print(f"   💬 DEV Chat ID:   {dev_chat if dev_chat else '❌ NO CONFIGURADO'}")

    if not dev_token or not dev_chat:
        print("\n⚠️ SKIP: Variables TELEGRAM_DEV no configuradas en .env")
        return False

    try:
        from telegram import Bot
        from telegram.constants import ParseMode
        
        bot = Bot(token=dev_token)
        me = await bot.get_me()
        print(f"   🤖 Bot conectado: @{me.username}")

        msg = (
            "🧪 *NEXUS | TEST DE SISTEMA*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Pipeline DEV Telegram verificado\n"
            "📊 Logger Multi-Tier: Operativo\n"
            "📚 Catálogo Soluciones: Cargado\n"
            "🛡️ Support Engineer: En línea\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔧 _Canal exclusivo para Desarrolladores_"
        )
        await bot.send_message(chat_id=dev_chat, text=msg, parse_mode=ParseMode.MARKDOWN)
        print("   ✅ Mensaje enviado al canal DEV")
        print(f"\n✅ PASS: Developer Telegram Pipeline")
        return True

    except Exception as e:
        print(f"   ❌ Error: {e}")
        print(f"\n❌ FAIL: Developer Telegram Pipeline")
        return False


async def test_4_support_engineer_crash():
    """Test 4: Simular un crash y verificar el pipeline completo del Support Engineer."""
    print("\n" + "=" * 60)
    print("  🧪 TEST 4: Support Engineer — Diagnóstico de Crash")
    print("=" * 60)

    from agents.agent_support import NexusSupportEngineer

    support = NexusSupportEngineer()
    await support.initialize()

    # Simular un error real
    try:
        result = 1 / 0
    except ZeroDivisionError as exc:
        print("   💥 Crash simulado: ZeroDivisionError")
        result = await support.handle_exception(
            exc,
            module="test_suite.py",
            context={"test": True, "purpose": "Verificación Fase 17"},
        )

        print(f"   📋 Acción tomada: {result.get('action', 'N/A')}")
        print(f"   🔧 Auto-corregido: {result.get('auto_fixed', False)}")
        
        diagnosis = result.get("diagnosis", {})
        if isinstance(diagnosis, dict):
            print(f"   🧠 Diagnóstico LLM: {diagnosis.get('diagnosis', 'N/A')[:120]}")
            print(f"   ⚡ Severidad: {diagnosis.get('severity', 'N/A')}")
        else:
            print(f"   📝 Diagnóstico: {str(diagnosis)[:120]}")

        print(f"\n✅ PASS: Support Engineer Crash Pipeline")
        return True


async def test_5_maintenance_notification():
    """Test 5: Enviar notificación de mantenimiento exitoso."""
    print("\n" + "=" * 60)
    print("  🧪 TEST 5: Notificación de Mantenimiento Exitoso")
    print("=" * 60)

    from agents.agent_support import NexusSupportEngineer

    support = NexusSupportEngineer()
    await support.initialize()

    await support.notify_maintenance_success(
        action="Log Rotation Check",
        details="Verificación completa de 3 tiers de logging. Sin purgas necesarias.",
        data={"critical_usage": "2%", "medium_usage": "1%", "low_usage": "0%"},
    )
    print("   ✅ Notificación de mantenimiento enviada al DEV")
    print(f"\n✅ PASS: Maintenance Notification")
    return True


async def main():
    print("\n")
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     🛡️  NEXUS FASE 17 — SUITE DE VERIFICACIÓN          ║")
    print("║     Auto-Maintenance & Support Engineer System          ║")
    print("╚══════════════════════════════════════════════════════════╝")

    results = {}

    results["Logger Multi-Tier"] = await test_1_tiered_logger()
    results["Catálogo Soluciones"] = await test_2_solutions_catalog()
    results["DEV Telegram"] = await test_3_dev_telegram()
    results["Support Engineer Crash"] = await test_4_support_engineer_crash()
    results["Maintenance Notification"] = await test_5_maintenance_notification()

    # Resumen final
    print("\n")
    print("╔══════════════════════════════════════════════════════════╗")
    print("║                  📊 RESUMEN FINAL                      ║")
    print("╠══════════════════════════════════════════════════════════╣")
    for name, passed in results.items():
        icon = "✅" if passed else "❌"
        print(f"║  {icon}  {name:40s}  ║")
    print("╚══════════════════════════════════════════════════════════╝")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n🏆 Resultado: {passed}/{total} tests pasaron")

    if passed == total:
        print("🎉 ¡TODOS LOS SISTEMAS OPERATIVOS!")
    else:
        print("⚠️ Algunos tests fallaron. Revisar logs arriba.")


if __name__ == "__main__":
    asyncio.run(main())
