"""
NEXUS v4.0 — Pipeline Entry Point
===================================
Lanza el pipeline completo de 5 capas.

Uso:
    python -m nexus.core.run
    
O desde Docker:
    docker-compose up nexus-core
"""

import asyncio
import logging
import signal
import sys

from nexus.core.pipeline import NexusPipeline


def setup_logging() -> None:
    """Configura logging institucional con formato consistente."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)-25s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Suppress noisy libraries
    for name in ["urllib3", "httpx", "asyncio", "binance", "websocket"]:
        logging.getLogger(name).setLevel(logging.WARNING)


async def main() -> None:
    """Entry point asíncrono del pipeline."""
    setup_logging()
    logger = logging.getLogger("nexus.main")

    pipeline = NexusPipeline()

    # Graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(pipeline.shutdown()))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await pipeline.initialize()
        await pipeline.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as exc:
        logger.critical(f"Fatal pipeline error: {exc}", exc_info=True)
    finally:
        await pipeline.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
