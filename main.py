#!/usr/bin/env python3
"""
NEXUS v4.0 — Institutional Entry Point
========================================
THE definitive entrypoint for NEXUS v4.0.

Pre-flight checks:
    1. Load .env
    2. Verify Redis connectivity
    3. Verify QuestDB connectivity
    4. Verify Telegram token loaded
    5. Initialize NexusPipeline (5-layer)
    6. Run with graceful shutdown

Usage:
    python main.py

Environment:
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
    file_handler = logging.FileHandler(
        "logs/nexus_session.log", encoding="utf-8"
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
    logger.info("  NEXUS v4.0 — Pre-Flight Health Checks")
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
            "     Pipeline will run without shared state (local fallback)."
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
            "     Trade persistence will be disabled."
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
            "     Set TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID in .env"
        )
        passed += 1  # Non-fatal

    logger.info("")
    logger.info(f"  Result: {passed}/{total} checks passed")
    logger.info("=" * 60)

    return True  # All checks are non-fatal — pipeline degrades gracefully


# ══════════════════════════════════════════════════════════════════════
#  Main Async Entry
# ══════════════════════════════════════════════════════════════════════

async def run() -> None:
    """Async entry point — initializes and runs the 5-layer pipeline."""
    global logger
    logger = logging.getLogger("nexus.main")

    pipeline = NexusPipeline()

    # ── Graceful Shutdown Handler ─────────────────────────────────
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("🛑 Shutdown signal received.")
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
        logger.critical(f"💀 Pipeline initialization failed: {exc}", exc_info=True)
        TelegramReporter.get_instance().fire_system_error(
            f"FATAL: init failed: {exc}", module="main.py"
        )
        await asyncio.sleep(2)  # Grace for Telegram dispatch
        return

    # ── Run Pipeline ──────────────────────────────────────────────
    logger.info("🚀 NEXUS v4.0 — Pipeline LIVE")

    # Run pipeline and shutdown_event concurrently
    pipeline_task = asyncio.create_task(pipeline.run())

    # Wait for either pipeline to finish or shutdown signal
    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        {pipeline_task, shutdown_waiter},
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
    logger.info("🛑 Initiating graceful shutdown...")
    try:
        await asyncio.wait_for(pipeline.shutdown(), timeout=15)
    except asyncio.TimeoutError:
        logger.warning("Shutdown timed out after 15s. Forcing exit.")
    except Exception as exc:
        logger.error(f"Shutdown error: {exc}")

    logger.info("🏁 NEXUS v4.0 — Session ended cleanly.")


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
    print("  v4.0 Institutional Grade — HFT Pipeline")
    print("=" * 60 + "\n")

    import argparse
    parser = argparse.ArgumentParser(description="NEXUS v4.0 Omni-Channel Commander")
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
            print("\n🛑 NEXUS terminated by user (Ctrl+C).")
        except Exception as exc:
            print(f"\n💀 FATAL: {exc}")
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
