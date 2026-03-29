"""
NEXUS v4.0 — Pipeline Orchestrator (FULL 5-LAYER WIRING)
==========================================================
Orquestador institucional que ata las 5 capas del pipeline asíncrono.

    Layer 1: Data Lake (QuestDB — tick/candle/trade ingestion)
    Layer 2: Macro Filter (MacroAgent — LLM regime via Redis)
    Layer 3: Micro Alpha (TechnicalSignalEngine + MLEngine)
    Layer 4: Risk Management (QuantRiskManager + Circuit Breaker)
    Layer 5: Execution (AbstractExecutionEngine via Factory)

Dual-Mode Operation:
    IQ_OPTION → NexusAlpha direct signal + Flat $1 sizing + Binary Turbo
    BINANCE   → Multi-indicator consensus + Kelly dynamic sizing + Spot/Perps

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
from .macro.macro_agent import MacroAgent, MacroRegime
from .data_lake.questdb_client import QuestDBClient
from .signal_engine import TechnicalSignalEngine
from .risk_manager import QuantRiskManager
from .ml_engine import MLEngine
from .observability import NexusMetrics
from nexus.reporting.telegram_reporter import TelegramReporter

logger = logging.getLogger("nexus.pipeline")


# ══════════════════════════════════════════════════════════════════════
#  Constants per Venue
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

_BINANCE_CONFIG = {
    "signal_mode": "spot",          # Multi-indicator consensus
    "max_daily_trades": 20,         # Market making frequency
    "min_confidence": 0.65,         # Umbral estándar
    "kelly_fraction_cap": 0.25,     # Kelly máximo
    "risk_per_trade": 0.01,         # 1% del portafolio por trade
}


class NexusPipeline:
    """
    Orquestador principal de NEXUS v4.0 — Full 5-Layer Wiring.
    
    Initialization order (dependency-correct):
        0. Redis           (shared state bus)
        1. QuestDB         (data lake)
        2. MacroAgent      (background cron → Redis MACRO_REGIME)
        3. SignalEngine    (Alpha V3 — venue-aware)
        4. RiskManager     (Circuit Breaker + Kelly)
        5. ExecutionEngine (Factory → IQ Option | Binance)
    """

    def __init__(self) -> None:
        load_dotenv()
        self.venue = os.getenv("EXECUTION_VENUE", "IQ_OPTION").upper()

        # Venue-specific config
        if self.venue == "IQ_OPTION":
            self._config = dict(_IQ_OPTION_CONFIG)
        else:
            self._config = dict(_BINANCE_CONFIG)

        # Infrastructure
        self.redis_client = None

        # Layer instances
        self.data_lake: Optional[QuestDBClient] = None
        self.macro_agent: Optional[MacroAgent] = None
        self.signal_engine: Optional[TechnicalSignalEngine] = None
        self.ml_engine: Optional[MLEngine] = None
        self.risk_manager: Optional[QuantRiskManager] = None
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
        logger.info(f"🚀 NEXUS Pipeline v4.0 — Initializing [{self.venue}]")
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

        # ML Engine for validation (Binance mode uses combined confidence)
        self.ml_engine = MLEngine(model_dir="models")

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
        else:
            logger.warning("⚠️ Execution engine no conectado. Pipeline en modo dry-run.")

        logger.info(
            f"✅ NEXUS Pipeline v4.0 — All 5 layers initialized [{self.venue}]\n"
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

        IQ_OPTION flow:
            1. Check MACRO_REGIME (Layer 2)
            2. Fetch OHLCV via IQ Option API
            3. NexusAlpha direct signal (Layer 3)
            4. Circuit breaker check (Layer 4)
            5. Execute CALL/PUT flat $1 (Layer 5)
            6. Log to QuestDB + Prometheus

        BINANCE flow:
            1. Check MACRO_REGIME (Layer 2)
            2. Fetch OHLCV from DataHandler/QuestDB (Layer 1)
            3. Multi-indicator consensus + ML validation (Layer 3)
            4. Kelly sizing + Risk gating + Circuit breaker (Layer 4)
            5. Execute MARKET/LIMIT via DMA (Layer 5)
            6. Log to QuestDB + Prometheus
        """
        self._running = True
        logger.info("🔄 Pipeline main loop started")

        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pipeline tick error: {e}", exc_info=True)
                self.telegram.fire_system_error(str(e), module="pipeline._tick")
                await asyncio.sleep(5)

            # Loop interval: IQ = 60s (sniper), Binance = 10s (higher freq)
            tick_interval = 60 if self.venue == "IQ_OPTION" else 10
            await asyncio.sleep(tick_interval)

    async def _tick(self) -> None:
        """Single pipeline tick — evalúa una oportunidad de trading."""
        t_start = time.perf_counter()

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

        # ── Step 3: Cooldown between trades (IQ Mode) ────────────────
        if self.venue == "IQ_OPTION":
            cooldown = self._config.get("cooldown_between_trades_s", 300)
            elapsed = time.time() - self._last_trade_time
            if elapsed < cooldown and self._last_trade_time > 0:
                return  # Silent return — still in cooldown

        # ── Step 4: Check Circuit Breaker (Layer 4) ──────────────────
        if self.risk_manager and self.risk_manager.is_circuit_breaker_active():
            logger.warning("🚨 Circuit Breaker ACTIVO — No new trades.")
            return

        # ── Step 5: Get Market Data (via IQ/Binance specific) ────────
        asset, df = await self._get_market_data()
        if df is None or df.empty or len(df) < 30:
            return  # Insuficient data

        # ── Step 6: Generate Signal (Layer 3) ────────────────────────
        signal_result = self.signal_engine.generate_signal(df)
        signal_dir = signal_result.get("signal", "HOLD")
        confidence = signal_result.get("confidence", 0.0)

        if signal_dir == "HOLD":
            return

        # ── Step 7: ML Validation (Layer 3b — Binance only) ──────────
        if self.venue == "BINANCE" and self.ml_engine:
            ml_result = self.ml_engine.predict(df)
            ml_direction = ml_result.get("combined_direction", "sideways")
            ml_confidence = ml_result.get("combined_confidence", 0.0)

            # Reject if ML disagrees with signal direction
            if signal_dir == "BUY" and ml_direction == "down":
                logger.info("🤖 ML rejects BUY (predicts down). HOLD.")
                return
            if signal_dir == "SELL" and ml_direction == "up":
                logger.info("🤖 ML rejects SELL (predicts up). HOLD.")
                return

            # Blend confidences
            confidence = (confidence * 0.6 + ml_confidence * 0.4)

        # ── Step 8: Confidence Gate ──────────────────────────────────
        min_conf = self._config.get("min_confidence", 0.55)
        if confidence < min_conf:
            logger.debug(
                f"Signal {signal_dir} conf={confidence:.3f} < {min_conf}. Skipping."
            )
            return

        # ── Step 9: Regime-Aware Adjustments ─────────────────────────
        if regime == MacroRegime.YELLOW:
            confidence *= 0.8  # Reduce confidence 20%
            logger.info("🟡 YELLOW regime — confidence reduced 20%")

        # ── Step 10: Position Sizing (Layer 4) ───────────────────────
        size = await self._calculate_size(confidence, df)
        if size <= 0:
            return

        # ── Step 11: Build TradeSignal ───────────────────────────────
        direction = self._map_signal_to_direction(signal_dir)
        trade_signal = TradeSignal(
            asset=asset,
            direction=direction,
            size=size,
            confidence=confidence,
            regime=regime.value,
            expiration_minutes=1 if self.venue == "IQ_OPTION" else 0,
            metadata={
                "reason": signal_result.get("reason", ""),
                "indicators": signal_result.get("indicators", {}),
            },
        )

        # ── Step 12: Execute (Layer 5) ───────────────────────────────
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

        # ── Step 13: Post-Execution ──────────────────────────────────
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

        # ── Step 14: Observability (Prometheus) ──────────────────────
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
        Gets OHLCV data from the venue-specific source.
        Returns (asset, DataFrame) or (None, empty DataFrame) on failure.
        """
        import pandas as pd

        if self.venue == "IQ_OPTION":
            # IQ-specific assets optimized for Binary (high payout, high volatility)
            assets = ["EURUSD-op", "GBPUSD-op", "EURJPY-op", "USDJPY-op"]

            for asset in assets:
                try:
                    # Use IQ engine's built-in data method
                    engine = self.execution_engine
                    if hasattr(engine, "get_historical_data"):
                        df = await engine.get_historical_data(asset, "1m", 500)
                        if df is not None and len(df) >= 30:
                            # Validate payout before spending compute
                            payout = await engine.get_payout(asset)
                            if payout >= self._config.get("min_payout", 80):
                                return asset, df
                except Exception as exc:
                    logger.debug(f"Data fetch failed for {asset}: {exc}")
                    continue

            return None, pd.DataFrame()

        else:  # BINANCE
            # Primary asset from config
            from nexus.config.settings import trading_config
            asset = trading_config.symbols[0] if trading_config.symbols else "BTCUSDT"

            # Try QuestDB first (Layer 1)
            if self.data_lake and self.data_lake.is_connected:
                df = await self.data_lake.query_candles(asset, "5m", 500)
                if df is not None and len(df) >= 30:
                    return asset, df

            # Fallback to execution engine data
            if hasattr(self.execution_engine, "get_historical_data"):
                df = await self.execution_engine.get_historical_data(asset, "5m", 500)
                if df is not None and len(df) >= 30:
                    return asset, df

            return None, pd.DataFrame()

    # ══════════════════════════════════════════════════════════════════
    #  Position Sizing (Dual-Mode)
    # ══════════════════════════════════════════════════════════════════

    async def _calculate_size(self, confidence: float, df: Any) -> float:
        """
        Venue-aware position sizing.

        IQ_OPTION: Flat $1 (Trojan Horse). When balance > $50, scale to $2.
        BINANCE:   Fractional Kelly * ATR-adjusted * Confidence weighted.
        """
        if self.venue == "IQ_OPTION":
            # Get current balance for scale check
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

        else:  # BINANCE — Full Kelly + ATR sizing
            try:
                balance = await self.execution_engine.get_balance()
            except Exception:
                balance = 10000.0

            if not self.risk_manager or not self._returns_history:
                # Default: 1% of balance
                return balance * self._config.get("risk_per_trade", 0.01)

            # Kelly sizing
            import numpy as np
            returns = np.array(self._returns_history)
            wins = returns[returns > 0]
            losses = returns[returns < 0]

            if len(wins) >= 3 and len(losses) >= 3:
                win_rate = len(wins) / len(returns)
                avg_win = float(np.mean(wins))
                avg_loss = float(np.mean(np.abs(losses)))

                kelly_f = self.risk_manager.kelly_criterion(win_rate, avg_win, avg_loss)

                # Scale kelly by confidence
                sizing_pct = kelly_f * confidence

                # ATR clamp (if available)
                if len(df) >= 20 and "high" in df.columns and "low" in df.columns:
                    atr = float(
                        (df["high"] - df["low"]).rolling(14).mean().iloc[-1]
                    )
                    current_price = float(df["close"].iloc[-1])
                    if atr > 0 and current_price > 0:
                        atr_pct = self.risk_manager.atr_position_size(
                            balance, atr, current_price
                        )
                        sizing_pct = min(sizing_pct, atr_pct / 100.0)

                size = balance * sizing_pct
                # Floor at $10, cap at 15% of balance
                return max(10.0, min(size, balance * 0.15))
            else:
                # Not enough history: conservative 1%
                return balance * 0.01

    # ══════════════════════════════════════════════════════════════════
    #  Signal Direction Mapping
    # ══════════════════════════════════════════════════════════════════

    def _map_signal_to_direction(self, signal_str: str) -> SignalDirection:
        """Maps signal engine output to venue-appropriate direction."""
        if self.venue == "IQ_OPTION":
            if signal_str == "BUY":
                return SignalDirection.CALL
            elif signal_str == "SELL":
                return SignalDirection.PUT
        else:  # BINANCE
            if signal_str == "BUY":
                return SignalDirection.BUY
            elif signal_str == "SELL":
                return SignalDirection.SELL

        return SignalDirection.BUY  # fallback

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
