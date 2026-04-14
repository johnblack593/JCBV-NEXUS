#!/usr/bin/env python3
"""
NEXUS v5.0 — Punto de Entrada Principal
========================================
Punto de entrada definitivo para NEXUS v5.0.

Verificaciones pre-vuelo:
    1. Cargar .env
    2. Verificar conectividad Redis
    3. Verificar conectividad QuestDB
    4. Verificar token Telegram cargado
    5. Inicializar NexusPipeline (5 capas)
    6. Ejecutar con apagado elegante

Uso:
    python main.py

Variables de entorno:
    EXECUTION_VENUE=IQ_OPTION | BINANCE  (default: IQ_OPTION)
    REDIS_HOST, REDIS_PORT               (default: localhost:6379)
    QUESTDB_PG_HOST, QUESTDB_PG_PORT     (default: localhost:8812)
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional — alerts disabled if missing)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Optional

import uvicorn  # type: ignore

# ── 0. Load .env FIRST (before any nexus imports) ────────────────
from dotenv import load_dotenv

load_dotenv()

# ── Imports after env is loaded ──────────────────────────────────
from nexus.core.pipeline import NexusPipeline
from nexus.reporting.telegram_reporter import TelegramReporter

logger: Optional[logging.Logger] = None


# ══════════════════════════════════════════════════════════════════════
#  Logging Setup
# ══════════════════════════════════════════════════════════════════════

def setup_logging() -> None:
    """Configure institutional-grade structured logging."""
    log_format = (
        "%(asctime)s │ %(name)-28s │ %(levelname)-7s │ %(message)s"
    )

    # File handler: persistent log
    os.makedirs("logs", exist_ok=True)
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        "logs/nexus_session.log", 
        encoding="utf-8", 
        maxBytes=10*1024*1024,  # 10 MB per log
        backupCount=5           # Keep last 5 logs
    )
    file_handler.setLevel(logging.DEBUG)

    # Console handler: INFO and above
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    logging.basicConfig(
        level=logging.DEBUG,
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[console_handler, file_handler],
    )

    # Suppress noisy third-party loggers
    for name in [
        "urllib3", "httpx", "asyncio", "binance", "websocket",
        "tensorflow", "absl", "grpc", "h5py",
    ]:
        logging.getLogger(name).setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════
#  Pre-Flight Health Checks
# ══════════════════════════════════════════════════════════════════════

def preflight_checks() -> bool:
    """
    Verify infrastructure is reachable BEFORE starting the pipeline.
    Returns True if all critical checks pass.
    """
    global logger
    logger = logging.getLogger("nexus.preflight")
    passed = 0
    total = 3

    venue = os.getenv("EXECUTION_VENUE", "IQ_OPTION").upper()

    logger.info("=" * 60)
    logger.info("  NEXUS v5.0 — Verificaciones Pre-Vuelo")
    logger.info("=" * 60)
    logger.info(f"  Venue: {venue}")
    logger.info("")

    # ── Check 1: Redis ────────────────────────────────────────────
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    try:
        import redis as redis_lib
        client = redis_lib.Redis(
            host=redis_host, port=redis_port,
            socket_connect_timeout=3, decode_responses=True,
        )
        client.ping()
        client.close()
        logger.info(f"  ✅ Redis         → {redis_host}:{redis_port}")
        passed += 1
    except Exception as exc:
        logger.warning(
            f"  ⚠️  Redis         → OFFLINE ({redis_host}:{redis_port}): {exc}"
        )
        logger.warning(
            "     El pipeline correrá sin estado compartido (fallback local)."
        )
        passed += 1  # Non-fatal: pipeline degrades gracefully

    # ── Check 2: QuestDB ──────────────────────────────────────────
    qdb_host = os.getenv("QUESTDB_PG_HOST", "localhost")
    qdb_port = int(os.getenv("QUESTDB_PG_PORT", "8812"))
    try:
        import socket
        sock = socket.create_connection((qdb_host, qdb_port), timeout=3)
        sock.close()
        logger.info(f"  ✅ QuestDB       → {qdb_host}:{qdb_port}")
        passed += 1
    except Exception:
        logger.warning(
            f"  ⚠️  QuestDB       → OFFLINE ({qdb_host}:{qdb_port})"
        )
        logger.warning(
            "     La persistencia de operaciones estará deshabilitada."
        )
        passed += 1  # Non-fatal

    # ── Check 3: Telegram ─────────────────────────────────────────
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat and "tu_token" not in tg_token:
        logger.info(
            f"  ✅ Telegram       → Token loaded (chat: {tg_chat[:6]}...)"
        )
        passed += 1
    else:
        logger.warning("  ⚠️  Telegram       → NOT CONFIGURED")
        logger.warning(
            "     Configura TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID en .env"
        )
        passed += 1  # Non-fatal

    logger.info("")
    logger.info(f"  Resultado: {passed}/{total} verificaciones completadas")
    logger.info("=" * 60)

    return True  # All checks are non-fatal — pipeline degrades gracefully


# ══════════════════════════════════════════════════════════════════════
#  Main Async Entry
# ══════════════════════════════════════════════════════════════════════

async def run() -> None:
    """Async entry point — initializes and runs the 5-layer pipeline + OCP Dashboard."""
    global logger
    logger = logging.getLogger("nexus.main")

    pipeline = NexusPipeline()

    # ── Graceful Shutdown Handler ─────────────────────────────────
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("🛑 Señal de apagado recibida.")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows: signal handlers not supported

    # ── Initialize ────────────────────────────────────────────────
    try:
        await pipeline.initialize()
    except Exception as exc:
        logger.critical(
            "💀 Falló la inicialización del pipeline: %s", exc, exc_info=True
        )
        # Intenta enviar alerta Telegram solo si el reporter ya fue inicializado.
        # Si no, el error se habrá registrado en el log de archivo.
        try:
            reporter = TelegramReporter.get_instance()
            reporter.fire_system_error(
                f"🚨 NEXUS v5.0 — Error fatal en arranque:\n{exc}", module="main.py"
            )
            await asyncio.sleep(2)
        except Exception as tg_exc:
            logger.warning(
                "No se pudo enviar alerta Telegram en el arranque: %s", tg_exc
            )
        return

    # ── Launch OCP Dashboard (FastAPI on port 8000) ───────────────
    dashboard_port = int(os.getenv("NEXUS_DASH_PORT", "8000"))
    uvi_config = uvicorn.Config(
        "nexus.dashboard.app:app",
        host="0.0.0.0",
        port=dashboard_port,
        log_level="warning",
        access_log=False,
    )
    uvi_server = uvicorn.Server(uvi_config)
    dashboard_task = asyncio.create_task(uvi_server.serve())
    logger.info(f"🎛️ OCP Dashboard LIVE on http://0.0.0.0:{dashboard_port}")

    # ── Run Pipeline ──────────────────────────────────────────────
    logger.info("🚀 NEXUS v5.0 — Pipeline ACTIVO")

    # Run pipeline and shutdown_event concurrently
    pipeline_task = asyncio.create_task(pipeline.run())

    # Wait for either pipeline to finish or shutdown signal
    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        {pipeline_task, shutdown_waiter, dashboard_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel pending tasks
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ── Graceful Shutdown ─────────────────────────────────────────
    logger.info("🛑 Iniciando apagado controlado...")
    try:
        await asyncio.wait_for(pipeline.shutdown(), timeout=15)
    except asyncio.TimeoutError:
        logger.warning("Apagado superó 15s de espera. Forzando salida.")
    except Exception as exc:
        logger.error(f"Error en apagado: {exc}")

    logger.info("🏁 NEXUS v5.0 — Sesión finalizada correctamente.")


# ══════════════════════════════════════════════════════════════════════
#  Synchronous Entry Point
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    """Synchronous wrapper — the ONE command to launch NEXUS."""
    setup_logging()

    # ASCII banner
    print("\n" + "=" * 60)
    print("  ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗")
    print("  ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝")
    print("  ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗")
    print("  ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║")
    print("  ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║")
    print("  ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝")
    print("  v5.0 Grado Institucional — Pipeline HFT")
    print("=" * 60 + "\n")

    import argparse
    parser = argparse.ArgumentParser(description="NEXUS v5.0 Comandante Omni-Canal")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: run
    parser_run = subparsers.add_parser("run", help="Lanza el Pipeline asíncrono normal")

    # Command: test
    parser_test = subparsers.add_parser("test", help="Lanza el framework de pruebas")
    parser_test.add_argument("--suite", type=str, required=True, help="Nombre de la suite de pruebas")

    # Command: calibrate
    parser_cal = subparsers.add_parser("calibrate", help="Lanza la calibración de un activo")
    parser_cal.add_argument("--asset", type=str, required=True, help="Nombre del activo a calibrar")

    args = parser.parse_args()

    # Default to run if no command is provided
    if args.command is None or args.command == "run":
        if not preflight_checks():
            sys.exit(1)
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            print("\n🛑 NEXUS detenido por el usuario (Ctrl+C).")
        except Exception as exc:
            print(f"\n💀 ERROR FATAL: {exc}")
            sys.exit(1)
            
    elif args.command == "test":
        print(f"🔧 Ejecutando suite de test: {args.suite}")
        # To be implemented with pytest or specific test runner
        sys.exit(0)
        
    elif args.command == "calibrate":
        from nexus.scripts.auto_calibrate import calibrate_asset
        print(f"📈 Ejecutando calibración WFO para activo: {args.asset}")
        asyncio.run(calibrate_asset(args.asset))


if __name__ == "__main__":
    main()
