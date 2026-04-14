"""
NEXUS v5.0 (beta) — NexusSessionManager
=========================================
Orchestrates 1..N independent pipeline sessions.

Each session is a full NexusPipeline instance running a specific
venue. Sessions share Redis (state bus) but are fully isolated
in terms of execution engine, risk manager, and strategy.

Configuration via environment:
  ACTIVE_SESSIONS=IQ_OPTION,BITGET   → both sessions active
  ACTIVE_SESSIONS=IQ_OPTION          → IQ Option only
  ACTIVE_SESSIONS=BITGET             → Bitget only
  (default: IQ_OPTION)

Usage:
  manager = NexusSessionManager()
  await manager.run()
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List

from nexus.core.pipeline import NexusPipeline

logger = logging.getLogger("nexus.session_manager")

class NexusSessionManager:
    """
    Top-level orchestrator for multi-venue NEXUS deployment.
    """

    def __init__(self) -> None:
        raw = os.getenv("ACTIVE_SESSIONS", "IQ_OPTION")
        self._active_sessions: List[str] = [
            s.strip().upper() for s in raw.split(",") if s.strip()
        ]
        self._pipelines: Dict[str, NexusPipeline] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    _SESSION_CONFIGS = {
        "IQ_OPTION": {
            "venue":    "IQ_OPTION",
            "strategy": "BINARY_ML",
        },
        "BITGET": {
            "venue":    "BITGET",
            "strategy": "BITGET_TREND_SCALPER",
        },
    }

    def _get_session_config(self, name: str) -> dict | None:
        return self._SESSION_CONFIGS.get(name)

    async def run(self) -> None:
        """
        Initializes and launches all active sessions.
        Blocks until all sessions exit or KeyboardInterrupt.
        """
        logger.info(f"🚀 NexusSessionManager — Active sessions: {self._active_sessions}")
        await self._initialize_sessions()
        await self._run_sessions()

    async def _initialize_sessions(self) -> None:
        """Creates and initializes NexusPipeline for each active session."""
        for name in self._active_sessions:
            config = self._get_session_config(name)
            if config is None:
                logger.error(f"Unknown session: {name}. Valid: {list(self._SESSION_CONFIGS)}")
                continue
            
            pipeline = NexusPipeline(
                venue=config["venue"],
                strategy=config["strategy"]
            )
            await pipeline.initialize()
            self._pipelines[name] = pipeline

    async def _run_session(self, name: str, pipeline: NexusPipeline) -> None:
        """
        Runs a single pipeline session with isolation.
        If one session crashes, the other continues running.
        """
        try:
            logger.info(f"Starting session: {name}")
            await pipeline.run()
        except asyncio.CancelledError:
            logger.info(f"Session {name} cancelled.")
        except Exception as exc:
            logger.error(f"Session {name} crashed: {exc}", exc_info=True)
            # Session dies isolated — other sessions unaffected
        finally:
            await pipeline.shutdown()

    async def _run_sessions(self) -> None:
        """Launches all session tasks and awaits completion."""
        tasks = []
        for name, pipeline in self._pipelines.items():
            task = asyncio.create_task(self._run_session(name, pipeline), name=f"session_{name}")
            self._tasks[name] = task
            tasks.append(task)
            
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            logger.warning("No valid sessions started. Exiting loop.")

    async def shutdown(self) -> None:
        """Gracefully shuts down all active sessions."""
        logger.info("NexusSessionManager — Triggering graceful shutdown...")
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                
        for name, pipeline in self._pipelines.items():
            try:
                await pipeline.shutdown()
            except Exception as exc:
                logger.error(f"Error shutting down pipeline {name}: {exc}")
                
        logger.info("NexusSessionManager — All sessions shut down.")

async def run_nexus() -> None:
    """Top-level entry point for NEXUS v5.0."""
    manager = NexusSessionManager()
    try:
        await manager.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — shutting down.")
    finally:
        await manager.shutdown()
