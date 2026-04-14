"""
NEXUS v5.0 (beta) — Pipeline Orchestrator (5-LAYER WIRING)
==========================================================
Orquestador institucional que ata las 5 capas del pipeline asíncrono.

    Layer 1: Data Lake (QuestDB — tick/candle/trade ingestion)
    Layer 2: Macro Filter (MacroAgent — LLM regime via Redis)
    Layer 3: Micro Alpha (TechnicalSignalEngine — strategy-driven)
    Layer 4: Risk Management (QuantRiskManager + Circuit Breaker)
    Layer 5: Execution (AbstractExecutionEngine via Factory)

Active Venue: IQ_OPTION — NexusAlpha direct signal,
    flat $1 progressive sizing, Binary Turbo execution.
    (BITGET: scheduled for Phase 3)

Principio: NINGUNA capa bloquea a otra. Todo fluye como eventos asíncronos.

Uso:
    pipeline = NexusPipeline()
    await pipeline.initialize()
    await pipeline.run()   # Main event loop
    await pipeline.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import redis as redis_lib
from dotenv import load_dotenv

from .execution.factory import get_execution_engine
from .execution.base import (
    AbstractExecutionEngine,
    ExecutionStatus,
    SignalDirection,
    TradeResult,
    TradeSignal,
    VenueType,
)
from .opportunity.opportunity_agent import OpportunityAgent
from .macro.macro_agent import MacroAgent, MacroRegime
from .data_lake.questdb_client import QuestDBClient
from .signal_engine import TechnicalSignalEngine
from .risk_manager import QuantRiskManager
from .observability import NexusMetrics
from .strategies.base import BaseStrategy
from .strategies.binary_ml_exotic import BinaryMLExoticStrategy
from .strategies.bitget_trend_scalper import BitgetTrendScalperStrategy
from nexus.reporting.telegram_reporter import TelegramReporter

logger = logging.getLogger("nexus.pipeline")


# ══════════════════════════════════════════════════════════════════════
#  Venue Configuration
# ══════════════════════════════════════════════════════════════════════

# IQ Option: Flat $1 sizing, binary mode alpha
_IQ_OPTION_CONFIG = {
    "signal_mode": "binary",        # NexusAlpha direct signal (Mean-Reversion)
    "base_size": 1.0,               # Flat $1 per trade (Trojan Horse)
    "max_size": 50.0,               # Cap cuando el capital crezca
    "max_daily_trades": 3,          # Sniper Mode: 1-3 trades/día
    "min_confidence": 0.55,         # Umbral mínimo para entrar
    "min_payout": 80,               # Payout mínimo %
    "cooldown_between_trades_s": 300,  # 5 min entre trades
}


class NexusPipeline:
    """
    Orquestador principal de NEXUS v5.0 — 5-Layer Wiring.

    Initialization order (dependency-correct):
        0. Redis           (shared state bus)
        1. QuestDB         (data lake)
        2. MacroAgent      (background cron → Redis MACRO_REGIME)
        3. SignalEngine    (Alpha — strategy-driven)
        4. RiskManager     (Circuit Breaker)
        5. ExecutionEngine (Factory → IQ Option)
    """

    # ── Strategy Factory Registry ──────────────────────────────────
    _STRATEGY_REGISTRY: Dict[str, type] = {
        "BINARY_ML": BinaryMLExoticStrategy,
        "BITGET_TREND_SCALPER": BitgetTrendScalperStrategy,
    }

    def __init__(self) -> None:
        load_dotenv()
        self.venue = os.getenv("EXECUTION_VENUE", "IQ_OPTION").upper()
        self.dry_run_mode = os.getenv("DRY_RUN", "False").lower() in ("true", "1", "yes")

        # IQ Option is the only active venue in v5.0.
        # Bitget config will be introduced in Phase 3 via the engine adapter.
        self._config = dict(_IQ_OPTION_CONFIG)

        # ── Strategy Factory (reads ACTIVE_STRATEGY env) ─────────
        active_strategy_key = os.getenv("ACTIVE_STRATEGY", "BINARY_ML").upper()

        strategy_cls = self._STRATEGY_REGISTRY.get(active_strategy_key)
        if strategy_cls is None:
            raise ValueError(
                f"ACTIVE_STRATEGY='{active_strategy_key}' no reconocido. "
                f"Valores válidos: {list(self._STRATEGY_REGISTRY.keys())}"
            )
        self.strategy: BaseStrategy = strategy_cls()
        logger.info(f"🧠 Strategy Factory → {self.strategy!r} (key={active_strategy_key})")

        # Infrastructure
        self.redis_client = None

        # Layer instances
        self.data_lake: Optional[QuestDBClient] = None
        self.macro_agent: Optional[MacroAgent] = None
        self.signal_engine: Optional[TechnicalSignalEngine] = None
        self.risk_manager: Optional[QuantRiskManager] = None
        self.opportunity_agent: Optional[OpportunityAgent] = None
        self.execution_engine: Optional[AbstractExecutionEngine] = None
        self.metrics: Optional[NexusMetrics] = None

        # Session tracking
        self._running = False
        self._daily_trades = 0
        self._session_start: Optional[datetime] = None
        self._last_trade_time: float = 0.0
        self._trade_results: List[TradeResult] = []
        self._returns_history: List[float] = []

    async def initialize(self) -> None:
        """Inicializa todas las capas en orden de dependencia."""
        logger.info(f"🚀 NEXUS Pipeline v5.0 — Initializing [{self.venue}]")
        self._session_start = datetime.now(timezone.utc)

        # ── Layer 0: Redis (shared state bus) ─────────────────────────
        try:
            redis_host = os.getenv("REDIS_HOST", "localhost")
            redis_port = int(os.getenv("REDIS_PORT", "6379"))
            self.redis_client = redis_lib.Redis(
                host=redis_host, port=redis_port, db=0,
                socket_connect_timeout=5, decode_responses=False,
            )
            self.redis_client.ping()
            logger.info(f"🔴 Redis conectado ({redis_host}:{redis_port})")
        except Exception as exc:
            logger.warning(f"Redis no disponible: {exc}. Operando sin estado compartido.")
            self.redis_client = None

        # ── Layer 1: Data Lake (QuestDB) ──────────────────────────────
        self.data_lake = QuestDBClient()
        dl_connected = await self.data_lake.connect()
        if dl_connected:
            await self.data_lake.create_tables()

        # ── Layer 2: Macro Agent (background cron → Redis) ────────────
        self.macro_agent = MacroAgent(
            interval_hours=1.0,
            redis_client=self.redis_client,
            questdb_client=self.data_lake,
        )
        await self.macro_agent.start()

        # ── Layer 3: Signal Engine (venue-aware) ──────────────────────
        signal_mode = self._config["signal_mode"]
        self.signal_engine = TechnicalSignalEngine(mode=signal_mode)
        logger.info(f"📊 Signal Engine mode={signal_mode}")

        # ── Layer 4: Risk Management ──────────────────────────────────
        self.risk_manager = QuantRiskManager(log_dir="logs", redis_client=self.redis_client)
        logger.info("🛡️ QuantRiskManager inicializado")

        # ── Layer 5: Execution Engine (Factory) ───────────────────────
        self.execution_engine = get_execution_engine()
        connected = await self.execution_engine.connect()
        if connected:
            balance = await self.execution_engine.get_balance()
            self.risk_manager.update_portfolio(balance, [])
            logger.info(f"💰 Balance inicial: ${balance:.2f}")

            # ── Layer 5b: Opportunity Agent (asset selection background loop) ─
            self.opportunity_agent = OpportunityAgent(
                execution_engine=self.execution_engine,
                redis_client=self.redis_client,
                interval_minutes=5.0,
                min_payout=self._config.get("min_payout", 80),
            )
            await self.opportunity_agent.start()
            logger.info("🔎 OpportunityAgent started (5 min interval)")
        else:
            logger.warning("⚠️ Execution engine no conectado. Pipeline en modo dry-run.")

        logger.info(
            f"✅ NEXUS Pipeline v5.0 — All 5 layers initialized [{self.venue}]\n"
            f"   Config: {self._config}"
        )

        # ── Observability: Prometheus Metrics ──────────────────────────
        self.metrics = NexusMetrics(port=9090)
        self.metrics.start_server()

        # ── Telegram: Institutional Voice ─────────────────────────────
        self.telegram = TelegramReporter.get_instance()
        await self.telegram.initialize()
        if connected:
            self.telegram.fire_startup(self.venue, balance)

    # ══════════════════════════════════════════════════════════════════
    #  Main Event Loop
    # ══════════════════════════════════════════════════════════════════

    async def run(self) -> None:
        """
        Main event loop — connects all 5 layers.

        Flow:
            1. Check MACRO_REGIME (Layer 2)
            2. Fetch OHLCV via execution engine
            3. Strategy-driven signal generation (Layer 3)
            4. Circuit breaker check (Layer 4)
            5. Execute CALL/PUT (Layer 5)
            6. Log to QuestDB + Prometheus
        """
        self._running = True
        logger.info("🔄 Pipeline main loop started")

        # ── Briefing Inicial de Mercado ────────────────────────────────
        if self.execution_engine:
            try:
                min_payout = self._config.get("min_payout", 80)
                best_asset = None
                
                if self.opportunity_agent:
                    best_asset = await self.opportunity_agent.get_best_asset()
                    
                if not best_asset and hasattr(self.execution_engine, "get_best_available_asset"):
                    best_asset = await self.execution_engine.get_best_available_asset(min_payout)
                    
                if best_asset:
                    payout = await self.execution_engine.get_payout(best_asset)
                    regime = await self.get_macro_regime()
                    self.telegram.fire_market_briefing(regime.value, best_asset, payout)
            except Exception as exc:
                logger.debug(f"Could not send market briefing: {exc}")

        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pipeline tick error: {e}", exc_info=True)
                self.telegram.fire_system_error(str(e), module="pipeline._tick")
                await asyncio.sleep(5)

            if self.venue == "BITGET":
                tick_interval = 300  # 5 min — aligns with BitgetTrendScalper 5m TF
            else:
                tick_interval = 60   # 1 min — IQ Option sniper mode
            await asyncio.sleep(tick_interval)

    async def _tick(self) -> None:
        """Single pipeline tick — evalúa una oportunidad de trading."""
        t_start = time.perf_counter()

        # ── Step 0: PANIC MODE (Kill Switch from OCP Dashboard) ──────
        if self.redis_client:
            try:
                panic = self.redis_client.get("NEXUS:PANIC_MODE")
                if panic and panic in (b"1", "1"):
                    logger.warning("🚨 PANIC HALT ACTIVE — All trading suspended via OCP.")
                    return
            except Exception:
                pass  # Redis offline — continue without panic check

        # ── Step 0b: Hot-Reload Config from Redis (OCP Overrides) ────
        if self.redis_client:
            try:
                _prefix = "NEXUS:SETTINGS:"
                overrides = {
                    "max_daily_trades": self.redis_client.get(f"{_prefix}max_daily_trades"),
                    "base_size": self.redis_client.get(f"{_prefix}base_size"),
                    "min_confidence": self.redis_client.get(f"{_prefix}min_confidence"),
                    "cooldown_between_trades_s": self.redis_client.get(f"{_prefix}cooldown_between_trades_s"),
                    "min_payout": self.redis_client.get(f"{_prefix}min_payout"),
                }
                for key, val in overrides.items():
                    if val is not None:
                        decoded = val.decode() if isinstance(val, bytes) else val
                        if key in ("max_daily_trades", "cooldown_between_trades_s", "min_payout"):
                            self._config[key] = int(decoded)
                        else:
                            self._config[key] = float(decoded)
            except Exception:
                pass  # Redis offline — use cached config

        # ── Step 1: Check Macro Regime (Layer 2) ─────────────────────
        regime = await self.get_macro_regime()

        if regime == MacroRegime.RED:
            logger.warning("🔴 MACRO REGIME RED — No new trades.")
            return

        # ── Step 2: Check daily trade limit ──────────────────────────
        max_daily = self._config.get("max_daily_trades", 3)
        if self._daily_trades >= max_daily:
            logger.info(
                f"📊 Daily trade limit reached ({self._daily_trades}/{max_daily})"
            )
            return

        # ── Step 3: Cooldown between trades ──────────────────────────
        cooldown = self._config.get("cooldown_between_trades_s", 300)
        elapsed = time.time() - self._last_trade_time
        if elapsed < cooldown and self._last_trade_time > 0:
            return  # Silent return — still in cooldown

        # ── Step 4: Check Circuit Breaker (Layer 4) ──────────────────
        if self.risk_manager and self.risk_manager.is_circuit_breaker_active():
            logger.warning("🚨 Circuit Breaker ACTIVO — No new trades.")
            return

        # ── Step 5: Get Market Data ──────────────────────────────────
        asset, df = await self._get_market_data()
        if df is None or df.empty or len(df) < 30:
            return  # Insuficient data

        # ── Step 6: AI Mode — Dynamic Parameter Injection ─────────────
        # If AI_MODE == 1, read regime-optimized params from Redis
        # and inject them into the signal engine before signal generation.
        if self.redis_client and self.signal_engine:
            try:
                ai_mode = self.redis_client.get("NEXUS:AI_MODE")
                if ai_mode and ai_mode in (b"1", "1"):
                    opt_key = f"NEXUS:OPT:{asset}:{regime.value}"
                    opt_raw = self.redis_client.get(opt_key)
                    if opt_raw:
                        import json as _json
                        opt_decoded = opt_raw.decode() if isinstance(opt_raw, bytes) else opt_raw
                        opt_params = _json.loads(opt_decoded)
                        self.signal_engine.apply_overrides(opt_params)
            except Exception:
                pass  # AI Mode degradation — use current params

        # ── Step 6b: Generate Signal (Strategy Pattern — Layer 3) ─────
        signal_result = await self.strategy.analyze(df)
        signal_dir = signal_result.get("signal", "HOLD")
        confidence = signal_result.get("confidence", 0.0)

        if signal_dir == "HOLD":
            return

        # ── Step 7: Confidence Gate ──────────────────────────────────
        min_conf = self._config.get("min_confidence", 0.55)
        if confidence < min_conf:
            logger.debug(
                f"Signal {signal_dir} conf={confidence:.3f} < {min_conf}. Skipping."
            )
            return

        # ── Step 8: Regime-Aware Adjustments ─────────────────────────
        if regime == MacroRegime.YELLOW:
            confidence *= 0.8  # Reduce confidence 20%
            logger.info("🟡 YELLOW regime — confidence reduced 20%")

        # ── Step 9: Position Sizing (Layer 4) ────────────────────────
        size = await self._calculate_size(confidence, df)
        if size <= 0:
            return

        # ── Step 10: Build TradeSignal (with Active Trade Management) ─
        direction = self._map_signal_to_direction(signal_dir)
        trade_signal = TradeSignal(
            asset=asset,
            direction=direction,
            size=size,
            confidence=confidence,
            regime=regime.value,
            expiration_minutes=1,
            stop_loss=signal_result.get("stop_loss"),
            take_profit=signal_result.get("take_profit"),
            trailing_stop_activation=signal_result.get("trailing_stop_activation"),
            trailing_stop_callback_pct=signal_result.get("trailing_stop_callback_pct"),
            breakeven_trigger=signal_result.get("breakeven_trigger"),
            time_exit_minutes=signal_result.get("time_exit_minutes"),
            metadata={
                "reason": signal_result.get("reason", ""),
                "indicators": signal_result.get("indicators", {}),
            },
        )

        # ── Step 11: Execute (Layer 5) ───────────────────────────────
        if self.dry_run_mode:
            import time as _time
            simulated_price = float(df["close"].iloc[-1])
            logger.info(f"🟢 [DRY RUN] TRADE SIMULADO: {direction.value} {asset} a ${simulated_price:.4f} | Size: ${size:.2f} | Conf: {confidence:.2f}")

            result = TradeResult(
                order_id=f"dry_run_{int(_time.time()*1000)}",
                asset=asset,
                direction=direction,
                size=size,
                status=ExecutionStatus.FILLED,
                executed_price=simulated_price,
                venue=VenueType.IQ_OPTION,
                commission=0.0,
                latency_ms=10.0,
                payout=85.0,
                error_message=None
            )
            latency = 10.0
        else:
            # Telegram: Sniper Entry alert (fire-and-forget)
            self.telegram.fire_sniper_entry(
                asset=asset,
                direction=direction.value,
                size=size,
                confidence=confidence,
                venue=self.venue,
                regime=regime.value,
                reason=signal_result.get("reason", ""),
            )

            try:
                result = await self.execution_engine.execute(trade_signal)
            except Exception as exc:
                logger.error(f"Execution failed: {exc}", exc_info=True)
                self.telegram.fire_system_error(f"Execution failed: {exc}", module="execution_engine")
                return
            latency = (time.perf_counter() - t_start) * 1000

        self._log_execution(result, trade_signal, latency)

        # ── Step 12: Post-Execution ──────────────────────────────────
        if result.status == ExecutionStatus.FILLED:
            self._daily_trades += 1
            self._last_trade_time = time.time()
            self._trade_results.append(result)

            # Log to QuestDB (Layer 1)
            await self._persist_trade(result, trade_signal)

            # Update risk tracking
            if result.payout > 0:
                # IQ Option: log the trade return
                self._returns_history.append(result.payout / 100.0)

            # Telegram: Trade Result alert (fire-and-forget)
            try:
                current_balance = await self.execution_engine.get_balance()
            except Exception:
                current_balance = 0.0
            outcome = "WIN" if result.payout > 0 else "LOSS"
            self.telegram.fire_trade_result(
                asset=result.asset,
                direction=result.direction.value,
                size=result.size,
                payout_pct=result.payout,
                new_balance=current_balance,
                outcome=outcome,
                venue=self.venue,
            )

        # ── Step 13: Observability (Prometheus) ──────────────────────
        if self.metrics:
            self.metrics.record_trade(
                venue=self.venue,
                direction=trade_signal.direction.value,
                status=result.status.value,
                latency_ms=latency,
                confidence=trade_signal.confidence,
                pnl=result.payout if result.status == ExecutionStatus.FILLED else 0.0,
            )
            self.metrics.update_daily_trades(self._daily_trades, self.venue)
            self.metrics.update_circuit_breaker(
                self.risk_manager.is_circuit_breaker_active() if self.risk_manager else False
            )
            self.metrics.update_macro_regime(regime.value)

    # ══════════════════════════════════════════════════════════════════
    #  Market Data Acquisition
    # ══════════════════════════════════════════════════════════════════

    async def _get_market_data(self) -> Tuple[Optional[str], Any]:
        """
        Retrieves the pre-selected best asset from OpportunityAgent
        (Redis cache) and fetches its OHLCV data.

        The asset selection is performed by OpportunityAgent in the
        background — this method never calls get_best_available_asset()
        or get_all_profit() directly.
        """
        import pandas as pd

        # Read pre-selected asset from OpportunityAgent
        asset = None
        if self.opportunity_agent:
            asset = await self.opportunity_agent.get_best_asset()

        if not asset:
            logger.debug("No asset available from OpportunityAgent. Skipping tick.")
            return None, pd.DataFrame()

        # Fetch OHLCV — only network call in the tick path
        try:
            df = await self.execution_engine.get_historical_data(asset, "1m", 500)
            if df is not None and len(df) >= 30:
                return asset, df
        except Exception as exc:
            logger.debug(f"Data fetch failed for {asset}: {exc}")

        return None, pd.DataFrame()

    # ══════════════════════════════════════════════════════════════════
    #  Position Sizing
    # ══════════════════════════════════════════════════════════════════

    async def _calculate_size(self, confidence: float, df: Any) -> float:
        """
        Progressive flat sizing for IQ Option.

        $1 until balance >= $50, then $2 until $200, then $5.
        Capped at max_size from config.
        """
        try:
            balance = await self.execution_engine.get_balance()
        except Exception:
            balance = 20.0

        base = self._config.get("base_size", 1.0)
        max_size = self._config.get("max_size", 50.0)

        # Progressive sizing: $1 until $50, then $2 until $200, etc.
        if balance >= 200:
            size = min(5.0, max_size)
        elif balance >= 50:
            size = min(2.0, max_size)
        else:
            size = base

        return size

    # ══════════════════════════════════════════════════════════════════
    #  Signal Direction Mapping
    # ══════════════════════════════════════════════════════════════════

    def _map_signal_to_direction(self, signal_str: str) -> SignalDirection:
        """Maps signal engine output to IQ Option direction (CALL/PUT)."""
        if signal_str == "BUY":
            return SignalDirection.CALL
        elif signal_str == "SELL":
            return SignalDirection.PUT
        return SignalDirection.CALL  # fallback

    # ══════════════════════════════════════════════════════════════════
    #  State Accessors
    # ══════════════════════════════════════════════════════════════════

    async def get_macro_regime(self) -> MacroRegime:
        """Lee el régimen macro actual (Redis → local fallback)."""
        if self.macro_agent:
            return await self.macro_agent.get_regime_from_redis()
        return MacroRegime.GREEN

    # ══════════════════════════════════════════════════════════════════
    #  Post-Execution: Logging & Persistence
    # ══════════════════════════════════════════════════════════════════

    def _log_execution(
        self, result: TradeResult, signal: TradeSignal, total_latency_ms: float
    ) -> None:
        """Logs trade execution with full details."""
        symbol = f"{'✅' if result.status == ExecutionStatus.FILLED else '❌'}"
        logger.info(
            f"{symbol} TRADE | {result.venue.value} | {result.asset} "
            f"| {result.direction.value} | ${result.size:.2f} "
            f"| Status: {result.status.value} "
            f"| Payout: {result.payout:.1f}% "
            f"| Latency: {total_latency_ms:.0f}ms "
            f"| Regime: {signal.regime} "
            f"| Conf: {signal.confidence:.3f} "
            f"| Daily: {self._daily_trades}/{self._config.get('max_daily_trades', 3)}"
        )

    async def _persist_trade(self, result: TradeResult, signal: TradeSignal) -> None:
        """Persiste trade en QuestDB para tear sheet y análisis."""
        if not self.data_lake:
            return

        try:
            await self.data_lake.ingest_trade({
                "order_id": result.order_id,
                "venue": result.venue.value,
                "asset": result.asset,
                "direction": result.direction.value,
                "size": result.size,
                "price": result.executed_price,
                "payout": result.payout,
                "commission": result.commission,
                "latency_ms": result.latency_ms,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "status": result.status.value,
            })
        except Exception as exc:
            logger.debug(f"QuestDB trade persistence failed: {exc}")

    # ══════════════════════════════════════════════════════════════════
    #  Session Management
    # ══════════════════════════════════════════════════════════════════

    def reset_daily_counters(self) -> None:
        """Resetear contadores diarios (llamar a las 00:00 UTC)."""
        self._daily_trades = 0
        logger.info("📊 Daily counters reset")

    def get_session_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas de la sesión actual."""
        filled = [r for r in self._trade_results if r.status == ExecutionStatus.FILLED]
        return {
            "venue": self.venue,
            "session_start": self._session_start.isoformat() if self._session_start else None,
            "daily_trades": self._daily_trades,
            "max_daily_trades": self._config.get("max_daily_trades", 3),
            "total_trades": len(filled),
            "signal_mode": self._config.get("signal_mode", "unknown"),
            "circuit_breaker_active": (
                self.risk_manager.is_circuit_breaker_active()
                if self.risk_manager else False
            ),
            "macro_regime": self.macro_agent.current_regime.value if self.macro_agent else "GREEN",
            "execution_engine": repr(self.execution_engine) if self.execution_engine else "N/A",
        }

    # ══════════════════════════════════════════════════════════════════
    #  Shutdown
    # ══════════════════════════════════════════════════════════════════

    async def shutdown(self) -> None:
        """Cierra todas las capas limpiamente."""
        self._running = False
        logger.info("🛑 NEXUS Pipeline — Shutting down...")

        # Telegram: Shutdown broadcast (before disconnecting services)
        stats = self.get_session_stats()
        self.telegram.fire_shutdown(stats)
        await asyncio.sleep(1)  # Grace period for Telegram dispatch

        if self.macro_agent:
            await self.macro_agent.stop()
        
        if self.opportunity_agent:
            await self.opportunity_agent.stop()
        if self.execution_engine:
            await self.execution_engine.disconnect()
        if self.data_lake:
            await self.data_lake.disconnect()
        if self.redis_client:
            try:
                self.redis_client.close()
            except Exception:
                pass

        # Final stats
        stats = self.get_session_stats()
        logger.info(
            f"🛑 NEXUS Pipeline — Shutdown complete | "
            f"Trades: {stats['total_trades']} | "
            f"Venue: {stats['venue']}"
        )
