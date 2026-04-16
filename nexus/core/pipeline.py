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
from .position_manager import PositionManager, OpenPosition
from .observability import NexusMetrics
from .strategies.base import BaseStrategy
from .strategies.binary_ml_exotic import BinaryMLExoticStrategy
from .strategies.bitget_trend_scalper import BitgetTrendScalperStrategy
from nexus.reporting.telegram_reporter import TelegramReporter
from nexus.storage.local_journal import LocalJournal

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

# Bitget: Dollar-risk sizing, futures mode
_BITGET_CONFIG = {
    "signal_mode": "futures",
    "max_daily_trades": 10,
    "min_confidence": 0.60,
    "min_payout": 0,                # Futures: no payout concept
    "cooldown_between_trades_s": 300,
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

    def __init__(
        self,
        venue: str | None = None,
        strategy: str | None = None,
    ) -> None:
        self._venue_override = venue
        self._strategy_override = strategy
        
        load_dotenv()
        
        self.venue = self._venue_override or os.getenv("EXECUTION_VENUE", "IQ_OPTION").upper()
        self.dry_run_mode = os.getenv("DRY_RUN", "False").lower() in ("true", "1", "yes")

        # ── Venue-specific config ─────────────────────────────────
        if self.venue == "BITGET":
            self._config = dict(_BITGET_CONFIG)
        else:
            self._config = dict(_IQ_OPTION_CONFIG)

        # ── Strategy Factory (reads ACTIVE_STRATEGY env) ─────────
        active_strategy_key = (self._strategy_override or os.getenv("ACTIVE_STRATEGY", "BINARY_ML")).upper()

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
        self._position_manager: Optional[PositionManager] = None
        self.metrics: Optional[NexusMetrics] = None
        self.telegram: Optional[TelegramReporter] = None
        self.journal: Optional[LocalJournal] = None
        
        # ── Validation / Debug Mode (Fix 2) ────────────────────────
        self.validation_mode = os.getenv("NEXUS_VALIDATION_MODE", "false").lower() == "true"
        if self.validation_mode:
            logger.warning(
                "⚠️  MODO VALIDACIÓN ACTIVO: Umbrales reducidos habilitados. "
                "Para producción, set NEXUS_VALIDATION_MODE=false"
            )

        # Session tracking
        self._running = False
        self._daily_trades = 0
        self._session_start: Optional[datetime] = None
        self._last_trade_time: float = 0.0
        self._trade_results: List[TradeResult] = []
        self._returns_history: List[float] = []
        self._last_trade_result: str = "NONE"
        from nexus.core.consensus_engine import ConsensusEngine
        self.consensus_engine = ConsensusEngine()

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
        self.execution_engine = get_execution_engine(force_venue=self.venue)
        logger.info("⏳ Conectando a IQ Option... [puede tomar hasta 30s cargando catálogo de activos]")
        connected = await self.execution_engine.connect()
        if connected:
            logger.info("✅ Login OK (Catálogo verificado o ejecutando en Fallback)")
            logger.info("⏳ Iniciando setup y balance de cuenta...")
            balance = await self.execution_engine.get_balance()
            self.risk_manager.update_portfolio(balance, [])
            logger.info(f"✅ Balance inicial: ${balance:.2f}")

            # Inicializar Asset Intelligence Service
            from nexus.services.asset_intelligence import AssetIntelligenceService

            self.asset_svc = AssetIntelligenceService(
                redis_client=self.redis_client,
                iqoption_api=self.execution_engine._api if hasattr(self.execution_engine, '_api') else None
            )

            # Obtener watchlist inicial basada en horario actual
            macro_regime_str = self.macro_agent.current_regime.value if hasattr(self, 'macro_agent') and self.macro_agent and self.macro_agent.current_regime else "GREEN"
            watchlist = self.asset_svc.get_current_watchlist(macro_regime=macro_regime_str)
            logger.info(f"📋 Watchlist activa: {watchlist}")

            # Iniciar refresh background (no bloquea)
            asyncio.create_task(self.asset_svc.refresh_cache_background())
            logger.info("✅ AssetIntelligenceService activo")

            self.opportunity_agent = OpportunityAgent(
                execution_engine=self.execution_engine,
                redis_client=self.redis_client,
                interval_minutes=5.0,
                min_payout=self._config.get("min_payout", 80),
            )
            await self.opportunity_agent.start()
            logger.info("🔎 AgenteBúsqueda iniciado (intervalo 5 min)")
            
            if self.venue == "BITGET":
                self._position_manager = PositionManager(self.execution_engine)
                synced = await self._position_manager.sync_open_positions()
                logger.info(f"🔄 PositionManager: {synced} open position(s) synced from exchange.")
        else:
            logger.warning("⚠️ Motor de ejecución no conectado. Pipeline en modo simulación.")

        logger.info(
            f"✅ NEXUS Pipeline v5.0 — Las 5 capas inicializadas [{self.venue}]\n"
            f"   Config: {self._config}"
        )

        # ── Observability: Prometheus Metrics ──────────────────────────
        self.metrics = NexusMetrics(port=9090)
        self.metrics.start_server()

        # ── Local Journal: persistencia cuando QuestDB está offline ────
        self.journal = LocalJournal()
        prev_state = self.journal.load_session_state()
        if prev_state.get("trades_today", 0) > 0:
            logger.info(
                "📂 Estado de sesión restaurado: %d trades, P&L $%.2f",
                prev_state["trades_today"], prev_state["session_pnl"]
            )

        # ── Telegram: Institutional Voice ─────────────────────────────
        self.telegram = TelegramReporter.get_instance()
        await self.telegram.initialize()

        # Restaurar estado de sesión en TelegramReporter
        if prev_state.get("trades_today", 0) > 0:
            self.telegram._session_pnl = prev_state.get("session_pnl", 0.0)
            weekly_trades = self.journal.get_weekly_trades()
            weekly_equity = self.journal.get_weekly_equity()
            if weekly_trades:
                self.telegram._weekly_trades = weekly_trades
            if weekly_equity:
                self.telegram._weekly_equity = weekly_equity
            self._daily_trades = prev_state.get("trades_today", 0)

        if connected:
            from nexus.core.llm.model_discovery import ModelDiscoveryService
            from nexus.core.llm.llm_router import LLMRouter

            discovery_svc = ModelDiscoveryService()
            groq_env = os.getenv("GROQ_API_KEYS", os.getenv("GROQ_API_KEY", ""))
            gem_env = os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", ""))
            g_key = groq_env.split(",")[0].strip() if groq_env else None
            m_key = gem_env.split(",")[0].strip() if gem_env else None
            
            # Llamar await service.discover_all
            discovery = await discovery_svc.discover_all(groq_key=g_key, gemini_key=m_key)
            
            # Guardamos para reporter
            self._model_discovery = discovery
            
            # Actualizar el modelo activo (se llama set_model)
            router = LLMRouter.get_instance()
            router.set_model("groq", discovery["groq"]["selected"])
            router.set_model("gemini", discovery["gemini"]["selected"])

            # Build infrastructure report for startup notification
            infra_report = await self._build_infrastructure_report(
                redis_host=os.getenv("REDIS_HOST", "localhost"),
                redis_port=int(os.getenv("REDIS_PORT", "6379")),
                redis_ok=self.redis_client is not None,
                questdb_ok=dl_connected,
            )
            
            # Incluir el resultado de model_discovery
            infra_report["model_discovery"] = {
                "groq": {
                    "selected": discovery["groq"]["selected"],
                    "source": discovery["groq"]["source"]
                },
                "gemini": {
                    "selected": discovery["gemini"]["selected"],
                    "source": discovery["gemini"]["source"]
                }
            }
            
            self.telegram.fire_startup(
                self.venue, balance,
                dry_run=self.dry_run_mode,
                infrastructure_report=infra_report,
            )

    # ══════════════════════════════════════════════════════════════════
    #  Main Event Loop
    # ══════════════════════════════════════════════════════════════════

    async def run(self) -> None:
        """
        Bucle principal — conecta las 5 capas del pipeline.

        Flujo:
            1. Verificar MACRO_REGIME (Capa 2)
            2. Obtener OHLCV del motor de ejecución
            3. Generación de señales por estrategia (Capa 3)
            4. Verificar circuit breaker (Capa 4)
            5. Ejecutar CALL/PUT (Capa 5)
            6. Log en QuestDB + Prometheus
        """
        self._running = True
        logger.info("🔄 Bucle principal del pipeline iniciado")

        # ── Daily Reset Scheduler ─────────────────────────────────────
        asyncio.create_task(self._daily_reset_loop(), name="daily_reset")

        if self.macro_agent:
            logger.info("Agente → Cron de Macro ya iniciado (Capa 2).")
            
        if self._position_manager:
            asyncio.create_task(
                self._position_manager.start(),
                name="position_manager"
            )

        # ── Briefing Inicial de Mercado ────────────────────────────────
        if self.opportunity_agent:
            logger.info("⏳ Esperando primer resultado del agente de oportunidades (máx 20s)...")
            waited = 0
            while waited < 20:
                agent_res = await self.opportunity_agent.get_best_asset()
                if agent_res:
                    logger.info(f"✅ Agente listo: {agent_res}")
                    break
                await asyncio.sleep(1)
                waited += 1
            if waited >= 20:
                logger.warning("⏰ Agente sin resultado tras 20s — usando watchlist base")

        if self.execution_engine:
            try:
                min_payout = self._config.get("min_payout", 80)
                best_asset = None
                
                if self.opportunity_agent:
                    best_asset = await self.opportunity_agent.get_best_asset()
                    
                if not best_asset and hasattr(self.execution_engine, "get_best_available_asset"):
                    best_match = await self.execution_engine.get_best_available_asset(min_payout)
                    if best_match:
                        best_asset = best_match["symbol"]
                        
                # ── ALERTA DE ARRANQUE EN REAL (Cuentas activas con dinero real) ──
                if not self.dry_run_mode:
                    bal = await self.execution_engine.get_balance()
                    max_tr = int(os.getenv("MAX_TRADES_PER_SESSION", "5"))
                    max_loss = float(os.getenv("MAX_LOSS_PER_SESSION", "3.00"))
                    
                    llm_prov = getattr(self.macro_agent, "_llm_provider", "UNKNOWN") if self.macro_agent else "UNKNOWN"
                    msg_llm = f"Groq ✅ / Gemini ⚠️" if "groq" in llm_prov.lower() else f"Modo Heurístico"
                    cb_state = "OPEN ❌" if self.risk_manager and self.risk_manager.is_circuit_breaker_active() else "CLOSED ✅"

                    msg_real = (
                        f"🔴 *NEXUS ARRANCANDO EN CUENTA REAL*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"💰 *Balance inicial:* ${bal:.2f}\n"
                        f"🎯 *Límite sesión:* {max_tr} trades / max pérdida ${max_loss:.2f}\n"
                        f"🤖 *LLM:* {msg_llm}\n"
                        f"⚡ *Circuit Breaker:* {cb_state}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Iniciando operaciones en 30 segundos..."
                    )
                    
                    account_type = os.getenv("IQ_OPTION_ACCOUNT_TYPE", "PRACTICE").upper()
                    if account_type == "PRACTICE":
                        warning_msg = "⚠️  DRY_RUN=False pero cuenta es PRACTICE — operando en demo con órdenes reales"
                        logger.warning(warning_msg)
                        msg_real = msg_real.replace("CUENTA REAL", "CUENTA PRACTICE (REAL MODO)")
                    
                    await self.telegram._send(msg_real)
                    logger.critical(f"Arrancando en MODO {account_type}. Esperando 30 segundos para permitir abortar (Ctrl+C)...")
                    await asyncio.sleep(30)
                    
                if best_asset:
                    payout = await self.execution_engine.get_payout(best_asset)
                    regime = await self.get_macro_regime()
                    # Determine analysis mode and fear & greed for briefing
                    analysis_mode = "Heurístico"
                    fear_greed_score = None
                    if self.macro_agent:
                        if self.macro_agent._llm_provider and self.macro_agent._llm_provider != "UNKNOWN":
                            analysis_mode = f"LLM: {self.macro_agent._llm_provider.capitalize()}"
                        fg = self.macro_agent._last_fear_greed
                        if fg and isinstance(fg, dict) and fg.get("score") is not None:
                            fear_greed_score = fg["score"]
                    self.telegram.fire_market_briefing(
                        regime.value, best_asset, payout,
                        analysis_mode=analysis_mode,
                        fear_greed=fear_greed_score,
                    )
            except Exception as exc:
                logger.debug(f"No se pudo enviar briefing de mercado: {exc}")

        while self._running:
            try:
                await asyncio.wait_for(self._tick(), timeout=45.0)
            except asyncio.TimeoutError:
                logger.warning("⚠️ _tick() timeout_global cancelado (superó 45s). Avanzando al siguiente ciclo.")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pipeline tick error: {e}", exc_info=True)
                self.telegram.fire_system_error(str(e), module="pipeline._tick")
                await asyncio.sleep(5)

            if self.venue == "BITGET":
                tick_interval = 300  # 5 min — aligns with BitgetTrendScalper 5m TF
            else:
                asset_name = self._config.get("asset", "")
                if "OTC" in asset_name.upper():
                    tick_interval = 30
                else:
                    tick_interval = 45
                
            await asyncio.sleep(tick_interval)

    async def _tick(self) -> None:
        """Single pipeline tick — evalúa una oportunidad de trading."""
        t_start = time.perf_counter()

        # ── Step 0: PANIC MODE (Kill Switch from OCP Dashboard) ──────
        if self.redis_client:
            try:
                panic = self.redis_client.get("NEXUS:PANIC_MODE")
                if panic and panic in (b"1", "1"):
                    logger.warning("🚨 MODO PÁNICO ACTIVO — Operaciones suspendidas por OCP.")
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
            logger.warning("🔴 RÉGIMEN MACRO ROJO — Sin nuevas operaciones.")
            return

        # ── Step 2: Check Session Limits (REAL ACCOUNT PROTECTION) ──
        max_session_trades = int(os.getenv("MAX_TRADES_PER_SESSION", "5"))
        max_session_loss = float(os.getenv("MAX_LOSS_PER_SESSION", "3.00"))
        
        if self._daily_trades >= max_session_trades:
            msg = f"⛔ Límite de sesión alcanzado: {self._daily_trades} operaciones ejecutadas. Cerrando."
            logger.warning(msg)
            self.telegram.fire_system_error(msg, module="pipeline._tick")
            self._running = False
            return
            
        current_loss = -self.telegram._session_pnl if self.telegram._session_pnl < 0 else 0.0
        if current_loss >= max_session_loss:
            msg = f"⛔ Stop de pérdida diaria: -${current_loss:.2f} alcanzado. Cerrando."
            logger.warning(msg)
            self.telegram.fire_system_error(msg, module="pipeline._tick")
            self._running = False
            return

        # Legacy check fallback
        max_daily = self._config.get("max_daily_trades", 3)
        if self._daily_trades >= max_daily and max_daily > max_session_trades:
            logger.info(
                f"📊 Límite diario alcanzado ({self._daily_trades}/{max_daily})"
            )
            return

        # ── Step 3: Cooldown Adaptativo por Resultado ─────────────────
        cooldown_base = self._config.get("cooldown_between_trades_s", 300)
        _last_result = getattr(self, "_last_trade_result", "NONE")
        if _last_result == "WIN":
            cooldown = max(90, cooldown_base // 3)    # 90s mínimo tras WIN
        elif _last_result == "LOSS":
            cooldown = min(600, cooldown_base * 2)    # max 600s tras LOSS
        else:
            cooldown = cooldown_base                  # default 300s al inicio

        elapsed = time.time() - self._last_trade_time
        if elapsed < cooldown and self._last_trade_time > 0:
            remaining = int(cooldown - elapsed)
            if remaining % 60 == 0:  # Log cada 60s, no cada tick
                logger.debug(f"⏳ Cooldown {_last_result}: {remaining}s restantes")
            return

        # Actualizar portfolio antes de consultar circuit breaker
        if self.risk_manager and self.execution_engine:
            try:
                current_balance = await self.execution_engine.get_balance()
                self.risk_manager.update_portfolio(current_balance, [])
            except Exception:
                pass  # No bloquear el tick si falla el balance fetch

        # ── Step 4: Check Circuit Breaker (Layer 4) ──────────────────
        if self.risk_manager and self.risk_manager.is_circuit_breaker_active():
            logger.warning("🚨 Circuit Breaker ACTIVO — Sin nuevas operaciones.")
            return

        # ── Step 4b: Verificación de Exposición Máxima (Capa 4) ──────
        if self.risk_manager:
            try:
                _balance_exp = await self._safe_get_balance()
                _prop_pct = (
                    self._config.get("base_size", 1.0) / max(_balance_exp, 1.0) * 100.0
                )
            except Exception:
                _prop_pct = 1.0
            allowed = self.risk_manager.max_exposure_check(
                proposed_size_pct=_prop_pct,
                current_exposure_pct=0.0,
            )
            if allowed == 0.0:
                logger.warning("🛡️ Exposición máxima alcanzada — tick omitido.")
                return

        # ── Step 4c: Consensus Engine — Selección paralela de activos ──
        macro_regime_str = (
            self.macro_agent.current_regime.value
            if hasattr(self, 'macro_agent') and self.macro_agent
            and self.macro_agent.current_regime else "GREEN"
        )
        # Obtener activo del agente si está disponible
        agent_symbol = None
        if self.opportunity_agent:
            agent_symbol = await self.opportunity_agent.get_best_asset()

        # Construir watchlist de evaluación combinando Agente + Inteligencia
        eval_watchlist = []
        if agent_symbol:
            eval_watchlist.append(agent_symbol)
            logger.info(f"🎯 Activo del agente: {agent_symbol} — añadido a evaluación")

        # Añadir watchlist base (sin duplicados)
        for sym in self.asset_svc.get_current_watchlist(macro_regime_str):
            if sym not in eval_watchlist:
                eval_watchlist.append(sym)

        if not eval_watchlist:
            # Fallback safe dict
            eval_watchlist = self._config.get("watchlist", ["EURUSD-OTC", "GBPUSD-OTC"])
            logger.warning("⚠️ eval_watchlist vacía — usando watchlist fallback base")

        logger.info(f"📋 Watchlist de evaluación: {eval_watchlist}")

        tick_count = getattr(self, '_cycle_count', 0) + 1
        self._cycle_count = tick_count
        wl_str = str(eval_watchlist[:3])
        if len(eval_watchlist) > 3:
            wl_str = wl_str[:-1] + ", ...]"
        logger.info(
            f"⏱️  TICK START — ciclo={tick_count} | "
            f"agente={agent_symbol or 'sin agente'} | "
            f"watchlist={wl_str}"
        )

        # Umbrales basados en modo (Fix 2: VALIDATION vs PRODUCTION)
        if self.validation_mode:
            _ce_min_conf = 0.50
            min_payout_target = 75.0
        else:
            _ce_min_conf = float(os.getenv("MIN_CONFIDENCE_THRESHOLD", "0.62"))
            min_payout_target = float(os.getenv("MIN_PAYOUT_THRESHOLD", "80.0"))

        real_payouts = {}
        try:
            # Intentar obtener de API
            if hasattr(self.execution_engine, "get_all_profit"):
                profit_data = await self.execution_engine.get_all_profit()
                for symbol in eval_watchlist:
                    sym_clean = symbol.replace('-op', '').replace('_', '').upper()
                    # A veces IQ retorna list of dicts o dicts
                    val = profit_data.get(sym_clean, profit_data.get(symbol, {}))
                    if isinstance(val, dict):
                        val = val.get('turbo', val.get('binary', 0.80))
                    elif isinstance(val, (int, float)):
                        pass
                    else:
                        val = 0.80
                        
                    payout_val = float(val) if val > 1.0 else float(val) * 100.0
                    real_payouts[symbol] = payout_val
                    
            # Si OpportunityAgent tiene algo fresco, prevalece para el best_asset
            if agent_symbol and hasattr(self.opportunity_agent, "_last_best_asset") and self.opportunity_agent._last_best_asset == agent_symbol:
                if hasattr(self.opportunity_agent, "best_stats") and "payout" in self.opportunity_agent.best_stats:
                    real_payouts[agent_symbol] = self.opportunity_agent.best_stats["payout"]
                
        except Exception as e:
            logger.warning(f"⚠️ No se pudo obtener payouts reales: {e} — usando fallback")

        async def _get_data_for_consensus(symbol: str):
            """Wrapper para obtener datos de mercado por símbolo."""
            try:
                df = await self.execution_engine.get_historical_data(symbol, "1m", 500)
                if df is not None and len(df) >= 30:
                    return df
                return None
            except Exception as e:
                logger.error(f"❌ Error en _tick() Step Descarga Consensus: {e}", exc_info=True)
                return None

        consensus_best = await self.consensus_engine.evaluate(
            watchlist=eval_watchlist,
            get_data_fn=_get_data_for_consensus,
            analyze_fn=self.strategy.analyze,
            real_payouts=real_payouts,
            min_conf=_ce_min_conf,
            min_payout=min_payout_target,
            atr_floor=float(os.getenv("ATR_FLOOR", "0.00005")),
        )

        if consensus_best is None:
            # Ningún activo de la watchlist pasó filtros — tick limpio
            logger.info(
                f"⏭️  TICK END — sin señal "
                f"(ConsensusEngine: no hay activo con señal ejecutable en este momento)"
            )
            return

        # El ConsensusEngine eligió el mejor activo — usarlo en Steps 5+
        self._config["asset"] = consensus_best.symbol

        # ── Step 5: Validar Disponibilidad y Get Market Data ─────────
        from nexus.services.asset_intelligence import AssetStatus
        base_asset = self._config.get("asset", "EURUSD-OTC")
        
        # Verificar disponibilidad del activo antes de operar
        asset_info = await self.asset_svc.get_asset_info(base_asset)

        if asset_info.status == AssetStatus.UNAVAILABLE:
            logger.info(f"⏸️  {base_asset} no disponible ahora — saltando tick")
            return  # No operar, esperar próximo tick

        if asset_info.status == AssetStatus.UNKNOWN:
            # Intentar con el mejor activo disponible de la watchlist
            macro_regime_str = self.macro_agent.current_regime.value if hasattr(self, 'macro_agent') and self.macro_agent and self.macro_agent.current_regime else "GREEN"
            watchlist = self.asset_svc.get_current_watchlist(macro_regime_str)
            best = await self.asset_svc.get_best_available(watchlist)
            if best is None:
                logger.warning("⚠️  Ningún activo disponible — saltando tick")
                return
            active_symbol = best.symbol
            logger.info(f"🔄 Usando activo alternativo: {active_symbol} ({best.profit:.0%} profit)")
        else:
            active_symbol = base_asset
            
        # Forzar el activo configurado para que el resto use el correcto
        self._config["asset"] = active_symbol

        asset, df = await self._get_market_data(active_symbol, eval_watchlist)
        if df is None or df.empty or len(df) < 30:
            logger.info("⏭️  TICK END — sin señal (motivo: datos insuficientes)")
            return  # Datos insuficientes

        logger.info(f"📊 DATOS OK — {len(df)} velas listas → iniciando Steps 1-5")

        # ── Step 6: AI Mode — Dynamic Parameter Injection ─────────────
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
            except Exception as e:
                logger.error(f"❌ Error en _tick() Step AI Options: {e}", exc_info=True)

        logger.debug("▶️  Step 1: Feature Engineering")
        t_s1 = time.perf_counter()
        
        # ── Step 6b: Generate Signal (Strategy Pattern — Layer 3) ─────
        try:
            signal_result = await self.strategy.analyze(df)
            signal_dir = signal_result.get("signal", "HOLD")
            confidence = signal_result.get("confidence", 0.0)
            
            # Simulated Step 1 parsing (Feature Engineering happens inside analyze)
            elapsed_s1 = time.perf_counter() - t_s1
            logger.debug(f"✅ Step 1 completado en {elapsed_s1:.2f}s")
            
            logger.debug("▶️  Step 2: Signal Evaluation")
            t_s2 = time.perf_counter()
            # Simulated Step 2 parsing (Evaluation happens inside analyze)
            elapsed_s2 = time.perf_counter() - t_s2
            logger.debug(f"✅ Step 2 completado en {elapsed_s2:.2f}s")
        except Exception as e:
            logger.error(f"❌ Error en _tick() Step 1/2: {e}", exc_info=True)
            logger.info("⏭️  TICK END — sin señal (motivo: Error interno)")
            return

        if consensus_best.signal == "HOLD" and consensus_best.composite > 0:
            # Extraer convicción latente del modelo RF
            rf_raw = consensus_best.extra.get("rf_prediction", -1)
            if rf_raw == 1:
                signal_dir = "BUY"
                logger.info(f"🔀 HOLD→BUY forzado por rf_prediction | {consensus_best.symbol}")
            elif rf_raw == 0:
                signal_dir = "SELL"
                logger.info(f"🔀 HOLD→SELL forzado por rf_prediction | {consensus_best.symbol}")
            else:
                logger.info("⏭️ TICK END — sin señal (HOLD sin rf_prediction válido)")
                return
        elif signal_dir == "HOLD":
            logger.info("⏭️  TICK END — sin señal (motivo: signal HOLD)")
            return

        logger.debug("▶️  Step 3: Consensus / Confidence")
        t_s3 = time.perf_counter()
        
        # ── Step 7: Umbral Dinámico por Régimen (Institutional Standard) ─
        raw_confidence = confidence
        
        # Umbrales directos — NO se multiplica la confianza
        THRESHOLDS = {
            "GREEN":  float(os.getenv("MIN_CONFIDENCE_GREEN", "0.55")),
            "YELLOW": float(os.getenv("MIN_CONFIDENCE_YELLOW", "0.62")),
            "RED":    float(os.getenv("MIN_CONFIDENCE_RED", "0.68")),
        }
        min_conf = THRESHOLDS.get(regime.value, 0.62)
        
        # Sincronizar pipeline con el umbral dinámico (Fix 2)
        if self.validation_mode and _ce_min_conf < min_conf:
            min_conf = _ce_min_conf
        
        if raw_confidence < min_conf:
            logger.info(
                f"⛔ SEÑAL RECHAZADA | {asset} | "
                f"conf: {raw_confidence:.1%} < umbral {regime.value}: {min_conf:.1%}"
            )
            logger.info(f"⏭️  TICK END — sin señal (motivo: confidence={raw_confidence:.3f} < {min_conf:.3f})")
            return
        
        # La confianza pasa directa, sin penalización
        confidence = raw_confidence
        
        if regime == MacroRegime.YELLOW:
            logger.info(
                f"🟡 YELLOW OK | {asset} | conf: {confidence:.1%} ≥ {min_conf:.1%}"
            )
            
        elapsed_s3 = time.perf_counter() - t_s3
        logger.debug(f"✅ Step 3 completado en {elapsed_s3:.2f}s")
        
        logger.debug("▶️  Step 4: Risk Check")
        t_s4 = time.perf_counter()

        # ── Step 9: Position Sizing (Layer 4) ────────────────────────
        size = await self._calculate_size(confidence, df)
        if size <= 0:
            logger.info("⏭️  TICK END — sin señal (motivo: size 0)")
            return
            
        elapsed_s4 = time.perf_counter() - t_s4
        logger.debug(f"✅ Step 4 completado en {elapsed_s4:.2f}s")
        
        logger.debug("▶️  Step 5: Execution")

        # ── Fix 1: Pre-execution availability guard ───────────────
        is_available = await self.asset_svc.is_available(asset)
        if not is_available:
            logger.warning(f"⏭️ {asset} suspendido — buscando alternativa")
            active_symbol = await self.asset_svc.get_best_available(regime.value)
            if not active_symbol:
                logger.info("⏭️ TICK END — todos los activos suspendidos")
                return
            asset = active_symbol
            self._config["asset"] = asset

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
                "atr": signal_result.get("indicators", {}).get("atr", 0.0),
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
                indicators=signal_result.get("indicators"),
            )

            try:
                result = await self.execution_engine.execute(trade_signal)
            except Exception as exc:
                logger.error(f"Ejecución fallida: {exc}", exc_info=True)
                self.telegram.fire_system_error(
                    f"Ejecución fallida en {asset}: {exc}",
                    module="execution_engine"
                )
                return
            latency = (time.perf_counter() - t_start) * 1000

            # ── Activo suspendido: forzar rotación inmediata ───────────
            if result.status == ExecutionStatus.ERROR:
                err_msg = (result.error_message or "").lower()
                if "suspended" in err_msg or "active is suspended" in err_msg:
                    logger.warning(
                        f"🚫 Activo {asset} SUSPENDIDO por IQ Option — "
                        f"forzando rotación a siguiente activo."
                    )
                    if self.opportunity_agent:
                        await self.opportunity_agent.invalidate_asset(asset)
                    self.telegram.fire_system_error(
                        f"Activo {asset} suspendido por IQ Option.\n"
                        f"🔄 Rotando al siguiente activo disponible.",
                        module="pipeline._tick"
                    )
                    self._last_trade_time = time.time()  # Aplicar cooldown para evitar loop
                return  # Salir del tick — el próximo tick usará el activo rotado

        self._log_execution(result, trade_signal, latency)
        logger.info(f"🎯 TICK END — SEÑAL {direction.value} | conf={confidence:.3f} | {asset}")

        # ── Step 12: Post-Execution ──────────────────────────────────
        if result.status == ExecutionStatus.ERROR:
            # Cooldown corto tras error para no spamear el exchange
            self._last_trade_time = time.time() - (
                self._config.get("cooldown_between_trades_s", 300) - 60
            )
            return

        if result.status == ExecutionStatus.FILLED:
            self._daily_trades += 1
            self._last_trade_time = time.time()
            self._trade_results.append(result)

            # Log to QuestDB (Layer 1)
            await self._persist_trade(result, trade_signal)

            # Update risk tracking
            # Retorno real del trade: ganancia=payout/100, pérdida=-1.0
            if self.venue == "BITGET":
                # Para futuros: payout representa P&L en USDT
                # Retorno = P&L / size (normalizado)
                trade_return = result.payout / result.size if result.size > 0 else 0.0
            else:
                # IQ Option binario: WIN → +payout%, LOSS → -100%
                if result.payout > 0:
                    trade_return = result.payout / 100.0
                else:
                    trade_return = -1.0
            self._returns_history.append(trade_return)

            # ── Active Position Management Delegation ─────────────────────
            if self._position_manager:
                pos = OpenPosition(
                    asset=asset,
                    direction="buy" if direction in (SignalDirection.BUY, SignalDirection.CALL) else "sell",
                    entry_price=result.executed_price,
                    original_size=size,
                    remaining_size=size,
                    stop_loss=trade_signal.stop_loss or 0.0,
                    take_profit=trade_signal.take_profit or 0.0,
                    atr_at_entry=trade_signal.metadata.get("atr", 0.0),
                )
                self._position_manager.register_position(pos)

            # Telegram: Trade Result — esperar liquidación de IQ Option (~60s)
            async def _delayed_result_notification(
                _result=result, _direction=result.direction.value,
                _size=result.size, _payout=result.payout, _venue=self.venue,
                _asset=asset,
            ):
                # Delegar espera de resultado real en vez de usar sleep
                if hasattr(self.execution_engine, "check_trade_result"):
                    try:
                        oid = int(_result.order_id)
                        # Este metodo ya incluye espera internamente, se bloquea localmente a la tarea
                        # Usando check_trade_result con espera explícita si no implementa execute_and_wait_result
                        if hasattr(self.execution_engine, "_wait_for_position_result"):
                            res_dict = await self.execution_engine._wait_for_position_result(oid, timeout=120)
                            _outcome = res_dict["outcome"]
                            _pnl = res_dict["profit_net"]
                            final_balance = res_dict["balance_after"]
                        else:
                            await asyncio.sleep(65)
                            out, profit_net = await self.execution_engine.check_trade_result(oid)
                            if out == "win":
                                _outcome = "WIN"
                            elif out == "lose":
                                _outcome = "LOSS"
                            elif out == "equal":
                                _outcome = "TIE"
                            else:
                                _outcome = "UNKNOWN"
                            _pnl = profit_net if profit_net is not None else 0.0
                            
                            if hasattr(self.execution_engine, "get_real_balance"):
                                final_balance = await self.execution_engine.get_real_balance()
                            else:
                                final_balance = await self.execution_engine.get_balance()
                    except Exception as e:
                        logger.error(f"Error checking trade result: {e}")
                        _outcome = "UNKNOWN"
                        _pnl = 0.0
                        final_balance = 0.0
                else:
                    await asyncio.sleep(65)  # Fallback simulación
                    try:
                        final_balance = await self.execution_engine.get_balance()
                    except Exception:
                        final_balance = 0.0
                    _outcome = "WIN" if _payout > 0 else "LOSS"
                    _pnl = _size * (_payout / 100.0) if _outcome == "WIN" else -_size

                self.telegram.fire_trade_result(
                    asset=_result.asset,
                    direction=_direction,
                    size=_size,
                    payout_pct=_payout,
                    new_balance=final_balance,
                    outcome=_outcome,
                    venue=_venue,
                    profit_net=_pnl
                )

                # Persist to local journal
                self._last_trade_result = "WIN" if _pnl > 0 else "LOSS"
                if self.journal:
                    self.journal.log_trade({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "asset": _asset,
                        "direction": _direction,
                        "size": _size,
                        "payout_pct": _payout,
                        "outcome": _outcome,
                        "profit_net": round(_pnl, 4),
                        "balance_after": round(final_balance, 2),
                        "venue": _venue,
                        "latency_ms": result.latency_ms,
                    })
                    self.journal.log_equity(final_balance)
                    self.journal.save_session_state(
                        session_pnl=self.telegram._session_pnl,
                        trades_today=self._daily_trades,
                        balance=final_balance,
                    )

            asyncio.create_task(_delayed_result_notification(), name="trade_result_notify")

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

    async def _get_market_data(self, requested_asset: str, watchlist: list[str]) -> Tuple[Optional[str], Any]:
        """
        Descarga paralela de velas en N timeframes (1m, 5m, 15m, 30m, 1h).
        Si el activo principal falla en >2 timeframes por timeout, cambia al siguiente.
        """
        import pandas as pd
        
        timeframes = ["1m", "5m", "15m", "30m", "1h"]
        current_asset = requested_asset
        
        for asset_candidate in [current_asset] + [a for a in watchlist if a != current_asset]:
            tasks = [
                self.execution_engine.get_historical_data(asset_candidate, tf, 500)
                for tf in timeframes
            ]
            
            # Descarga PARALELA
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            valid_dfs = {}
            timeout_count = 0
            
            for tf, res in zip(timeframes, results):
                if isinstance(res, Exception) or res is None or len(res) < 30:
                    timeout_count += 1
                else:
                    valid_dfs[tf] = res
                    
            if timeout_count > 2:
                logger.warning(f"⚠️ [{asset_candidate}] timeout en {timeout_count}/5 TFs — switch a NEXT_SYMBOL")
                continue # Probar con el siguiente
                
            # Todo fue exitoso o fallaron <= 2
            # El pipeline maneja los faltantes omitiéndolos lógicamente (se usa '1m' principal)
            primary_df = valid_dfs.get("1m")
            if primary_df is not None:
                if asset_candidate != requested_asset:
                    self._config["asset"] = asset_candidate
                return asset_candidate, primary_df
                
        logger.debug("Ningún activo pudo proporcionar suficientes datos históricos.")
        return None, pd.DataFrame()

    # ══════════════════════════════════════════════════════════════════
    #  Position Sizing
    # ══════════════════════════════════════════════════════════════════

    async def _calculate_size(self, confidence: float, df: Any) -> float:
        """
        Position sizing for NEXUS.

        IQ Option: flat progressive sizing ($1/$2/$5 by balance tier).
        Bitget:    dollar-risk sizing — balance * risk_pct / price * leverage.
                   Returns contract size in base asset units (e.g., BTC).
                   Capped by BITGET_MAX_SIZE_USDT to prevent overleveraging.
        """
        try:
            balance = await self.execution_engine.get_balance()
        except Exception:
            balance = 20.0

        if self.venue == "BITGET":
            return await self._calculate_bitget_size(balance, df)
        else:
            return self._calculate_iq_size(balance)

    def _calculate_iq_size(self, balance: float) -> float:
        """Flat progressive sizing for IQ Option binary trades."""
        base     = self._config.get("base_size", 1.0)
        max_size = self._config.get("max_size", 50.0)
        if balance >= 200:
            return min(5.0, max_size)
        elif balance >= 50:
            return min(2.0, max_size)
        return base

    async def _calculate_bitget_size(
        self, balance: float, df: Any
    ) -> float:
        """
        Dollar-risk sizing for Bitget futures.

        Returns contract size in base asset units.
        The PositionManager will use this as original_size.

        Formula:
          dollars_at_risk = balance * BITGET_RISK_PCT_PER_TRADE
          contract_size   = (dollars_at_risk * leverage) / current_price

        Caps:
          - Minimum notional: BITGET_MIN_ORDER_USDT (default $5)
          - Maximum notional: BITGET_MAX_SIZE_USDT  (default $50)
          - Minimum contracts: enforced by Stream F guard
        """
        risk_pct   = float(os.getenv("BITGET_RISK_PCT_PER_TRADE", "0.01"))
        leverage   = float(os.getenv("BITGET_LEVERAGE",           "5"))
        min_usdt   = float(os.getenv("BITGET_MIN_ORDER_USDT",     "5.0"))
        max_usdt   = float(os.getenv("BITGET_MAX_SIZE_USDT",      "50.0"))

        try:
            current_price = float(df["close"].iloc[-1])
        except Exception:
            logger.warning("Sizing Bitget: no se pudo leer precio actual. Omitiendo.")
            return 0.0

        if current_price <= 0:
            return 0.0

        dollars_at_risk = balance * risk_pct
        # Clamp notional between min and max
        dollars_at_risk = max(min_usdt, min(dollars_at_risk * leverage, max_usdt))
        contract_size   = dollars_at_risk / current_price

        logger.debug(
            f"Bitget sizing | balance=${balance:.2f} | "
            f"risk={risk_pct*100:.1f}% | leverage={leverage}x | "
            f"price={current_price:.4f} | notional=${dollars_at_risk:.2f} | "
            f"contracts={contract_size:.6f}"
        )
        return contract_size

    # ══════════════════════════════════════════════════════════════════
    #  Signal Direction Mapping
    # ══════════════════════════════════════════════════════════════════

    def _map_signal_to_direction(self, signal_str: str) -> SignalDirection:
        """
        Mapea la señal de la estrategia a la dirección de ejecución.

        IQ Option (binarios): CALL / PUT
        Bitget (futuros):     BUY  / SELL

        Razón: los exchanges de futuros no entienden CALL/PUT.
        Si se envía CALL a Bitget, la orden será rechazada.
        """
        if self.venue == "BITGET":
            if signal_str == "BUY":
                return SignalDirection.BUY
            elif signal_str == "SELL":
                return SignalDirection.SELL
            logger.warning(f"Señal desconocida '{signal_str}' para BITGET → fallback BUY")
            return SignalDirection.BUY
        else:
            # IQ Option: binary direction
            if signal_str == "BUY":
                return SignalDirection.CALL
            elif signal_str == "SELL":
                return SignalDirection.PUT
            logger.warning(f"Señal desconocida '{signal_str}' para IQ_OPTION → fallback CALL")
            return SignalDirection.CALL

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
        """Registra la ejecución del trade con detalle completo."""
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
            logger.debug(f"Fallo al persistir trade en QuestDB: {exc}")

    # ══════════════════════════════════════════════════════════════════
    #  Session Management
    # ══════════════════════════════════════════════════════════════════

    def reset_daily_counters(self) -> None:
        """Resetear contadores diarios (llamar a las 00:00 UTC)."""
        self._daily_trades = 0
        logger.info("📊 Contadores diarios reseteados")

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

    async def _daily_reset_loop(self) -> None:
        """Resetea contadores diarios cada 24 horas (alineado a UTC medianoche)."""
        from datetime import timedelta
        while self._running:
            now = datetime.now(timezone.utc)
            # Próxima medianoche UTC (asegurando fecha base en UTC)
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            
            seconds_until_midnight = (next_midnight - now).total_seconds()
            
            # Guarda de seguridad para evitar sleep negativo/inmediato (evita loop infinito)
            if seconds_until_midnight <= 0:
                seconds_until_midnight = 86400.0
                
            await asyncio.sleep(seconds_until_midnight)
            self.reset_daily_counters()
            logger.info("📅 Contadores diarios reseteados a medianoche UTC.")

    async def _safe_get_balance(self) -> float:
        """Balance con fallback seguro para guardianes de riesgo."""
        try:
            return await self.execution_engine.get_balance()
        except Exception:
            return 20.0

    async def _build_infrastructure_report(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_ok: bool = False,
        questdb_ok: bool = False,
    ) -> Dict[str, Any]:
        """
        Builds infrastructure status dict for Telegram startup notification.
        Runs real preflight connectivity checks against LLM providers.
        """
        report: Dict[str, Any] = {}

        # Redis
        if redis_ok:
            report["redis_status"] = f"OK:{redis_host}:{redis_port}"
        else:
            report["redis_status"] = "ERROR:no disponible"

        # QuestDB
        if questdb_ok:
            report["questdb_status"] = "OK"
        else:
            report["questdb_status"] = "OFFLINE:persistencia de operaciones deshabilitada"

        # LLM status — real preflight connectivity check
        llm_status = None
        try:
            from nexus.core.llm.llm_router import LLMRouter
            from nexus.core.llm.diagnostics import run_preflight_all

            router = LLMRouter.get_instance()
            groq_keys = list(router._groq_keys) if router._groq_keys else []
            gemini_keys = list(router._gemini_keys) if router._gemini_keys else []

            if groq_keys or gemini_keys:
                llm_status = await run_preflight_all(
                    groq_keys=groq_keys,
                    gemini_keys=gemini_keys,
                    groq_model=router._groq_model,
                    gemini_model=router._gemini_model,
                )
                # Log preflight results
                for prov, info in (llm_status or {}).items():
                    if info.get("status") == "ok":
                        logger.info(
                            "🤖 LLM Preflight %s: OK (%s ms)",
                            prov, info.get("latency_ms", "?")
                        )
                    else:
                        logger.warning(
                            "🤖 LLM Preflight %s: FALLO [%s] — %s",
                            prov, info.get("error_category", "?"),
                            info.get("error_type", "desconocido")
                        )
        except Exception as exc:
            logger.warning("Error ejecutando preflight LLM: %s", exc)

        report["llm_status"] = llm_status

        # MacroAgent
        if self.macro_agent:
            report["macro_interval"] = self.macro_agent.interval_hours
            report["macro_provider"] = getattr(self.macro_agent, "_llm_provider", "UNKNOWN")
        else:
            report["macro_interval"] = ""
            report["macro_provider"] = "UNKNOWN"

        # Local Journal
        if self.journal:
            report["journal_status"] = self.journal.get_status()
            prev = self.journal.load_session_state()
            report["journal_trades"] = self.journal.get_trade_count()
            report["journal_session_pnl"] = prev.get("session_pnl", 0.0)
        else:
            report["journal_status"] = "ERROR:no inicializado"
            report["journal_trades"] = 0
            report["journal_session_pnl"] = 0.0

        return report

    # ══════════════════════════════════════════════════════════════════
    #  Shutdown
    # ══════════════════════════════════════════════════════════════════

    async def shutdown(self) -> None:
        """Cierra todas las capas limpiamente."""
        self._running = False
        logger.info("🛑 NEXUS Pipeline — Shutting down...")

        # Telegram: Shutdown broadcast (before disconnecting services)
        if self.telegram:
            stats = self.get_session_stats()
            self.telegram.fire_shutdown(stats)
            await asyncio.sleep(1)  # Grace period for Telegram dispatch

        if self.macro_agent:
            await self.macro_agent.stop()
        
        if self.opportunity_agent:
            await self.opportunity_agent.stop()
        if self.execution_engine:
            await self.execution_engine.disconnect()
        
        if self._position_manager:
            await self._position_manager.stop()

        if self.data_lake:
            await self.data_lake.disconnect()
        if self.redis_client:
            try:
                self.redis_client.close()
            except Exception:
                pass

        # resumen de sesión
        stats = self.get_session_stats()
        logger.info(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  📈 resumen de sesión en {stats['venue']}\n"
            f"  Operaciones totales: {stats['total_trades']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        logger.info(
            f"🛑 NEXUS Pipeline — Shutdown complete | "
            f"Trades: {stats['total_trades']} | "
            f"Venue: {stats['venue']}"
        )
