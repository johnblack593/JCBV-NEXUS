"""
NEXUS v4.0 — Institutional Telegram Reporter (Singleton Asíncrono)
===================================================================
Canal oficial de observabilidad remota. Todas las alertas críticas
del pipeline se emiten vía Telegram con latencia CERO en el loop
principal (usando asyncio.create_task()).

Eventos Institucionales:
    1. MACRO_SHIFT    — Cambio de régimen macro (GREEN/YELLOW/RED)
    2. CIRCUIT_BREAKER — CB activado por drawdown excesivo
    3. SNIPER_ENTRY    — Trade ejecutado (IQ Option binary / Binance spot)
    4. TRADE_RESULT    — Resultado del trade (P&L + balance actualizado)
    5. WEEKLY_REPORT   — Reporte semanal automático (lunes 7:00 AM)
    6. SYSTEM_ERROR    — Error crítico del sistema

Principio: FIRE AND FORGET.
    Cada alerta se despacha con asyncio.create_task() para que
    NUNCA bloquee el loop de trading. Si Telegram falla, se loguea
    y el pipeline continúa sin interrupción.

Uso:
    from nexus.reporting.telegram_reporter import TelegramReporter

    reporter = TelegramReporter.get_instance()
    await reporter.initialize()

    # Fire-and-forget (non-blocking)
    reporter.fire_macro_shift("GREEN", "RED", "News: Fed rate hike detected")
    reporter.fire_sniper_entry("EURUSD-op", "CALL", 1.0, 0.92, "IQ_OPTION")
    reporter.fire_circuit_breaker(0.18, 24)
    reporter.fire_trade_result("EURUSD-op", "CALL", 1.0, 0.80, 20.80, "WIN")
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np

try:
    from telegram import Bot
    from telegram.constants import ParseMode
    _HAS_TELEGRAM = True
except ImportError:
    _HAS_TELEGRAM = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False

logger = logging.getLogger("nexus.telegram")

# Timezone GMT-5 (Colombia / EST)
_TZ_GMT5 = timezone(timedelta(hours=-5))


class TelegramReporter:
    """
    Singleton Asíncrono — Institutional Telegram Voice.

    Todas las alertas son fire-and-forget vía asyncio.create_task().
    Si Telegram falla, el error se loguea pero NUNCA detiene el pipeline.
    """

    _instance: Optional["TelegramReporter"] = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls) -> "TelegramReporter":
        """Obtiene la instancia singleton del reporter."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._bot: Optional[Any] = None
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._dev_chat_id = os.getenv("TELEGRAM_DEV_CHAT_ID", "")
        self._connected = False

        # Weekly tracking
        self._weekly_trades: List[Dict[str, Any]] = []
        self._weekly_equity: List[float] = []
        self._session_pnl: float = 0.0

        self.__class__._initialized = True

    # ══════════════════════════════════════════════════════════════════
    #  Lifecycle
    # ══════════════════════════════════════════════════════════════════

    async def initialize(self) -> None:
        """Inicializa el bot de Telegram (llamar una vez en pipeline.initialize())."""
        if not _HAS_TELEGRAM:
            logger.warning("python-telegram-bot no instalado. Alertas desactivadas.")
            return

        if not self._bot_token or not self._chat_id:
            logger.warning("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID vacíos. Alertas desactivadas.")
            return

        try:
            self._bot = Bot(token=self._bot_token)
            me = await self._bot.get_me()
            self._connected = True
            logger.info(f"📱 Telegram conectado: @{me.username}")
        except Exception as exc:
            logger.error(f"Telegram init failed: {exc}")
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ══════════════════════════════════════════════════════════════════
    #  EVENT 1: MACRO REGIME SHIFT
    # ══════════════════════════════════════════════════════════════════

    def fire_macro_shift(
        self, old_regime: str, new_regime: str, reason: str = ""
    ) -> None:
        """Fire-and-forget: Cambio de régimen macro."""
        self._fire(self._send_macro_shift(old_regime, new_regime, reason))

    async def _send_macro_shift(
        self, old_regime: str, new_regime: str, reason: str
    ) -> None:
        regime_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(new_regime, "⚪")
        action = {
            "GREEN": "Motor HFT ACTIVO. Trading habilitado.",
            "YELLOW": "Modo precaución. Confianza reducida 20%.",
            "RED": "Motor HFT PAUSADO. Sin nuevas operaciones.",
        }.get(new_regime, "Régimen desconocido.")

        msg = (
            f"🚨 *CAMBIO DE RÉGIMEN*\n"
            f"─────────────────\n"
            f"📊 *Cambio de Régimen:* `{old_regime}` → {regime_emoji} `{new_regime}`\n"
            f"⚡ *Acción:* {action}\n"
        )
        if reason:
            msg += f"📋 *Motivo:* {reason}\n"
        msg += f"─────────────────\n⏱ {self._timestamp()}"

        await self._send(msg)

    # ══════════════════════════════════════════════════════════════════
    #  EVENT 1.5: MARKET BRIEFING
    # ══════════════════════════════════════════════════════════════════

    def fire_market_briefing(
        self, macro_regime: str, best_asset: str, payout: float,
        analysis_mode: str = "Heurístico",
        fear_greed: Optional[int] = None,
    ) -> None:
        """Fire-and-forget: Resumen del mercado inicial / escaneo dinámico."""
        self._fire(self._send_market_briefing(
            macro_regime, best_asset, payout, analysis_mode, fear_greed
        ))

    async def _send_market_briefing(
        self, macro_regime: str, best_asset: str, payout: float,
        analysis_mode: str, fear_greed: Optional[int],
    ) -> None:
        regime_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(macro_regime, "⚪")
        # Fear & Greed label
        if fear_greed is not None:
            fg_labels = [
                (25, "Miedo Extremo"), (45, "Miedo"), (55, "Neutral"),
                (75, "Avaricia"), (101, "Avaricia Extrema"),
            ]
            fg_label = next((lbl for thr, lbl in fg_labels if fear_greed < thr), "Neutral")
            fg_str = f"{fear_greed} — {fg_label}"
        else:
            fg_str = "No disponible"

        msg = (
            f"📰 *NEXUS — Análisis de Mercado*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🌍 *Macro:* {regime_emoji} {macro_regime}\n"
            f"🏆 *Mejor activo:* {best_asset}\n"
            f"💰 *Payout:* {payout:.1f}%\n"
            f"🧠 *Modo de análisis:* {analysis_mode}\n"
            f"📊 *Fear & Greed:* {fg_str}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send(msg)

    # ══════════════════════════════════════════════════════════════════
    #  EVENT 2: CIRCUIT BREAKER
    # ══════════════════════════════════════════════════════════════════

    def fire_circuit_breaker(
        self, drawdown: float, cooldown_hours: int = 24
    ) -> None:
        """Fire-and-forget: Circuit breaker activado."""
        self._fire(self._send_circuit_breaker(drawdown, cooldown_hours))

    async def _send_circuit_breaker(
        self, drawdown: float, cooldown_hours: int
    ) -> None:
        msg = (
            f"⚠️ *CIRCUIT BREAKER ACTIVADO*\n"
            f"─────────────────\n"
            f"⛔ Todas las posiciones *CERRADAS* (emergencia)\n"
            f"🔒 Trading *DETENIDO* por `{cooldown_hours}h`\n"
            f"📉 *Drawdown Máximo:* `{drawdown:.1%}`\n"
            f"─────────────────\n"
            f"🛡️ Gestión de Riesgo NEXUS\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send(msg)

    # ══════════════════════════════════════════════════════════════════
    #  EVENT 3: SNIPER ENTRY (Trade Executed)
    # ══════════════════════════════════════════════════════════════════

    # Feature name mapping for human-readable sniper entry messages
    _FEATURE_NAMES: Dict[str, str] = {
        "log_returns": "Tendencia de precio",
        "rsi": "Momento RSI",
        "atr": "Volatilidad (ATR)",
        "volume": "Volumen",
        "macd": "MACD",
        "bb_width": "Bandas Bollinger",
    }
    _FEATURE_ICONS: Dict[str, str] = {
        "log_returns_CALL": "📈", "log_returns_PUT": "📉",
        "log_returns_BUY": "📈", "log_returns_SELL": "📉",
        "rsi": "🔄", "atr": "📊", "volume": "📦",
        "macd": "🔀", "bb_width": "〰️",
    }

    def fire_sniper_entry(
        self,
        asset: str,
        direction: str,
        size: float,
        confidence: float,
        venue: str,
        regime: str = "GREEN",
        reason: str = "",
        indicators: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Fire-and-forget: Trade ejecutado."""
        self._fire(self._send_sniper_entry(
            asset, direction, size, confidence, venue, regime, reason, indicators
        ))

    async def _send_sniper_entry(
        self,
        asset: str,
        direction: str,
        size: float,
        confidence: float,
        venue: str,
        regime: str,
        reason: str,
        indicators: Optional[Dict[str, Any]] = None,
    ) -> None:
        dir_label = {
            "CALL": "🟢 COMPRA (CALL)", "PUT": "🔴 VENTA (PUT)",
            "BUY": "🟢 COMPRA (LONG)", "SELL": "🔴 VENTA (SHORT)",
        }.get(direction, direction)

        regime_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(regime, "⚪")

        # Confidence level text
        conf_pct = int(confidence * 100) if confidence <= 1.0 else int(confidence)
        if conf_pct >= 75:
            conf_label = "Muy Alta"
        elif conf_pct >= 60:
            conf_label = "Alta"
        else:
            conf_label = "Moderada"

        mode = "[ENTRADA SNIPER]" if venue == "IQ_OPTION" else "[ENTRADA INSTITUCIONAL]"

        msg = (
            f"🎯 *{mode}*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 *Activo:* {asset}\n"
            f"🎯 *Dirección:* {dir_label}\n"
            f"💵 *Monto:* ${size:.2f}\n"
            f"🤖 *Confianza del modelo:* {conf_pct}% — {conf_label}\n"
            f"🌐 *Exchange:* {venue}\n"
            f"📊 *Régimen:* {regime_emoji} {regime}\n"
        )

        # Feature importances section (human-readable)
        importances = None
        if indicators and isinstance(indicators, dict):
            importances = indicators.get("feature_importances")
        if importances and isinstance(importances, dict):
            # Sort by importance, top 3
            sorted_feats = sorted(importances.items(), key=lambda x: -x[1])[:3]
            msg += f"━━━━━━━━━━━━━━━━━━━\n"
            msg += f"📊 *Señales del análisis*\n"
            for feat_name, feat_val in sorted_feats:
                human_name = self._FEATURE_NAMES.get(feat_name, feat_name.upper())
                # Icon lookup with direction awareness
                icon_key = f"{feat_name}_{direction}" if feat_name == "log_returns" else feat_name
                icon = self._FEATURE_ICONS.get(icon_key, "📊")
                pct = int(feat_val * 100)
                msg += f"  {icon} *{human_name}:* {pct}%\n"
        elif reason:
            msg += f"━━━━━━━━━━━━━━━━━━━\n"
            msg += f"📋 *Detalle:* {reason}\n"

        msg += f"━━━━━━━━━━━━━━━━━━━\n⏱ {self._timestamp()}"

        await self._send(msg)

    # ══════════════════════════════════════════════════════════════════
    #  EVENT 4: TRADE RESULT (Win/Loss + Balance)
    # ══════════════════════════════════════════════════════════════════

    def fire_trade_result(
        self,
        asset: str,
        direction: str,
        size: float,
        payout_pct: float,
        new_balance: float,
        outcome: str,  # "WIN" or "LOSS" or "TIE"
        venue: str = "IQ_OPTION",
        profit_net: float = 0.0,
    ) -> None:
        """Fire-and-forget: Resultado del trade."""
        self._fire(self._send_trade_result(
            asset, direction, size, payout_pct, new_balance, outcome, venue, profit_net
        ))

    async def _send_trade_result(
        self,
        asset: str,
        direction: str,
        size: float,
        payout_pct: float,
        new_balance: float,
        outcome: str,
        venue: str,
        profit_net: float = 0.0,
    ) -> None:
        # Direction label in Spanish
        dir_label = {
            "CALL": "🟢 COMPRA", "PUT": "🔴 VENTA",
            "BUY": "🟢 COMPRA", "SELL": "🔴 VENTA",
        }.get(direction, direction)

        if outcome == "WIN":
            if profit_net == 0.0 and payout_pct > 0:
                profit_net = size * (payout_pct / 100.0)
            pnl = profit_net
        elif outcome == "LOSS":
            if profit_net == 0.0:
                profit_net = -size
            pnl = profit_net
        else: # TIE
            pnl = 0.0
            profit_net = 0.0

        self._session_pnl += pnl

        # Track for weekly report
        self._weekly_trades.append({
            "asset": asset, "direction": direction, "size": size,
            "pnl": pnl, "outcome": outcome, "venue": venue,
            "timestamp": datetime.now(_TZ_GMT5).isoformat(),
        })
        self._weekly_equity.append(new_balance)

        if outcome == "WIN":
            msg = (
                f"✅ *OPERACIÓN GANADORA*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *Activo:* {asset} | {dir_label}\n"
                f"💵 *Invertido:* ${size:.2f}\n"
                f"🏆 *Ganancia neta:* +${profit_net:.2f}\n"
                f"📊 *Payout:* {payout_pct:.0f}%\n"
                f"💼 *Balance real:* ${new_balance:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"⏱ {self._timestamp()}"
            )
        elif outcome == "LOSS":
            msg = (
                f"❌ *OPERACIÓN PERDIDA*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *Activo:* {asset} | {dir_label}\n"
                f"💵 *Invertido:* ${size:.2f}\n"
                f"📉 *Pérdida:* -${abs(profit_net):.2f}\n"
                f"💼 *Balance real:* ${new_balance:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"⏱ {self._timestamp()}"
            )
        else:
            msg = (
                f"➖ *OPERACIÓN EMPATADA*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 *Activo:* {asset} | {dir_label}\n"
                f"💵 *Invertido:* ${size:.2f}\n"
                f"💰 *Recuperado:* ${size:.2f}\n"
                f"💼 *Balance real:* ${new_balance:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"⏱ {self._timestamp()}"
            )

        await self._send(msg)

    # ══════════════════════════════════════════════════════════════════
    #  EVENT 5: SYSTEM ERROR
    # ══════════════════════════════════════════════════════════════════

    def fire_system_error(self, error_msg: str, module: str = "pipeline") -> None:
        """Fire-and-forget: Error crítico del sistema."""
        self._fire(self._send_system_error(error_msg, module))

    async def _send_system_error(self, error_msg: str, module: str) -> None:
        msg = (
            f"🚨 *ALERTA DEL SISTEMA*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📦 *Módulo:* {module}\n"
            f"⚠️ *Error:* {error_msg[:400]}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {self._timestamp()}"
        )
        await self._send_dev(msg)

    # ══════════════════════════════════════════════════════════════════
    #  EVENT 6: WEEKLY REPORT
    # ══════════════════════════════════════════════════════════════════

    async def send_weekly_report(self, capital_actual: float = 0.0) -> None:
        """Envía reporte semanal completo con chart de equity."""
        trades = self._weekly_trades
        equity = self._weekly_equity
        now = datetime.now(_TZ_GMT5)
        start = now - timedelta(days=7)

        if not trades:
            msg = (
                f"📊 *NEXUS | REPORTE SEMANAL*\n"
                f"🗓️ {start.strftime('%d/%m/%Y')} → {now.strftime('%d/%m/%Y')}\n"
                f"─────────────────\n"
                f"Sin operaciones esta semana.\n"
                f"💼 *Balance:* `${capital_actual:.2f}`\n"
            )
            await self._send(msg)
            return

        # Calculate metrics (vectorized)
        pnls = np.array([t.get("pnl", 0) for t in trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        total_pnl = float(np.sum(pnls))
        win_rate = len(wins) / len(pnls) * 100 if len(pnls) > 0 else 0
        pf = float(np.sum(wins)) / max(float(np.sum(np.abs(losses))), 1e-8)

        # Max drawdown from equity
        max_dd = 0.0
        if equity and len(equity) > 1:
            eq = np.array(equity)
            running_max = np.maximum.accumulate(eq)
            dd = (running_max - eq) / np.maximum(running_max, 1e-8)
            max_dd = float(np.max(dd)) * 100

        pnl_emoji = "📈" if total_pnl >= 0 else "📉"

        msg = (
            f"📊 *NEXUS | REPORTE SEMANAL*\n"
            f"🗓️ {start.strftime('%d/%m/%Y')} → {now.strftime('%d/%m/%Y')}\n"
            f"─────────────────────────\n"
            f"{pnl_emoji} *G/P:* `{'+'if total_pnl>=0 else ''}${total_pnl:.2f}`\n"
            f"💼 *Operaciones:* `{len(trades)}` (W:`{len(wins)}` | L:`{len(losses)}`)\n"
            f"🎯 *Tasa de Éxito:* `{win_rate:.1f}%`\n"
            f"⚖️ *Factor de Beneficio:* `{pf:.2f}`\n"
            f"📉 *Drawdown Máximo:* `{max_dd:.2f}%`\n"
            f"─────────────────────────\n"
            f"💰 *Balance:* `${capital_actual:.2f}`\n"
            f"⚡ NEXUS Analytics\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send(msg)

        # Send equity chart if possible
        if _HAS_MPL and equity and len(equity) > 1:
            try:
                chart_bytes = self._generate_equity_chart(equity)
                await self._send_photo(
                    chart_bytes,
                    f"📈 Equity — {start.strftime('%d/%m')} → {now.strftime('%d/%m')}"
                )
            except Exception as exc:
                logger.debug(f"Chart generation failed: {exc}")

        # Reset weekly data
        self._weekly_trades = []
        self._weekly_equity = []
        self._session_pnl = 0.0

    # ══════════════════════════════════════════════════════════════════
    #  Startup / Shutdown Broadcasts
    # ══════════════════════════════════════════════════════════════════

    def fire_startup(
        self, venue: str, balance: float,
        dry_run: bool = False,
        infrastructure_report: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Fire-and-forget: Pipeline started with infrastructure diagnostics."""
        self._fire(self._send_startup(venue, balance, dry_run, infrastructure_report))

    async def _send_startup(
        self, venue: str, balance: float,
        dry_run: bool = False,
        infrastructure_report: Optional[Dict[str, Any]] = None,
    ) -> None:
        # FIX 6: Icon matches mode — no duplicate/wrong icon
        if dry_run:
            exec_line = "🟡 *Ejecución:* SIMULACIÓN"
        else:
            exec_line = "🟢 *Ejecución:* REAL"

        msg = (
            f"🚀 *NEXUS v5.0 EN LÍNEA*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 *Exchange:* {venue}\n"
            f"💰 *Balance:* ${balance:.2f}\n"
            f"📊 *Modo:* Sniper (1-3/día)\n"
            f"{exec_line}\n"
        )

        if infrastructure_report is not None:
            ir = infrastructure_report
            msg += f"━━━━━━━━━━━━━━━━━━━\n"
            msg += f"🔧 *Estado de Infraestructura*\n"

            # Redis
            redis_st = ir.get("redis_status", "")
            if redis_st.startswith("OK"):
                redis_detail = redis_st.replace("OK:", "").strip() or "conectado"
                msg += f"  ✅ Redis: {redis_detail}\n"
            else:
                redis_err = redis_st.replace("ERROR:", "").strip() or "no disponible"
                msg += f"  ❌ Redis: {redis_err}\n"

            # QuestDB
            qdb_st = ir.get("questdb_status", "")
            if qdb_st.startswith("OK"):
                msg += f"  ✅ QuestDB: conectado\n"
            else:
                qdb_err = qdb_st.replace("OFFLINE:", "").strip() or "offline"
                msg += f"  ⚠️ QuestDB: OFFLINE — {qdb_err}\n"

            # Telegram (we're sending this, so it's connected)
            msg += f"  ✅ Telegram: conectado\n"

            # Local Journal
            journal_st = ir.get("journal_status", "")
            jnl_trades = ir.get("journal_trades", 0)
            jnl_pnl = ir.get("journal_session_pnl", 0.0)
            if journal_st.startswith("OK"):
                if jnl_trades > 0:
                    msg += f"  ✅ Journal local: {jnl_trades} trades previos | P&L sesión ${jnl_pnl:.2f}\n"
                else:
                    msg += f"  ✅ Journal local: sin trades previos\n"
            elif journal_st:
                jnl_err = journal_st.replace("ERROR:", "").strip() or "error"
                msg += f"  ❌ Journal local: {jnl_err}\n"

            # LLM status
            llm_status = ir.get("llm_status")
            model_discovery = ir.get("model_discovery", {})
            if llm_status and isinstance(llm_status, dict):
                msg += f"━━━━━━━━━━━━━━━━━━━\n"
                msg += f"🤖 *Estado de LLMs*\n"
                any_ok = False
                for provider, info in llm_status.items():
                    if not isinstance(info, dict):
                        continue
                    status = info.get("status", "error")
                    keys_tried = info.get("keys_tried", [])
                    latency = info.get("latency_ms")
                    error_category = info.get("error_category", "")
                    error_type = info.get("error_type", "")
                    retryable = info.get("retryable", True)

                    # Obtener información de model_discovery
                    discovery = model_discovery.get(provider.lower(), {})
                    model_name = discovery.get("selected", "")
                    source = discovery.get("source", "")
                    if source == "api":
                        source_icon = " ✅ "
                        source_label = "(API)"
                    elif source == "preferred_fallback":
                        source_icon = " ⚠️ "
                        source_label = "(fallback)"
                    elif source == "api_first_available":
                        source_icon = " ⚠️ "
                        source_label = "(primer disponible)"
                    else:
                        source_icon = ""
                        source_label = ""
                        
                    model_str = f"\n{source_icon} modelo: {model_name} {source_label}" if model_name else ""

                    if status == "ok":
                        any_ok = True
                        lat_str = f" — {latency:.0f}ms" if latency else ""
                        msg += f"  ✅ {provider}: conectado{lat_str}{model_str}\n"
                    else:
                        diag = self._classify_llm_error(error_type)
                        cat_tag = f" [{error_category}]" if error_category else ""
                        msg += f"  ❌ {provider} ({len(keys_tried)} claves): {diag}{cat_tag}\n"
                if not any_ok:
                    msg += f"  🔄 Modo activo: Heurístico\n"

            # MacroAgent info
            macro_interval = ir.get("macro_interval", "")
            macro_provider = ir.get("macro_provider", "UNKNOWN")
            if macro_interval:
                msg += f"━━━━━━━━━━━━━━━━━━━\n"
                msg += f"🌐 *MacroAgent:* intervalo {macro_interval}h | proveedor: {macro_provider}\n"
        else:
            msg += f"🛡️ *Riesgo:* Circuit Breaker + Redis CB\n"

        msg += f"━━━━━━━━━━━━━━━━━━━\n"
        msg += f"⏱ {self._timestamp()}"
        await self._send(msg)

    def fire_shutdown(self, stats: Dict[str, Any]) -> None:
        """Fire-and-forget: Pipeline shutdown."""
        self._fire(self._send_shutdown(stats))

    async def _send_shutdown(self, stats: Dict[str, Any]) -> None:
        msg = (
            f"🛑 *NEXUS v5.0 FUERA DE LÍNEA*\n"
            f"─────────────────\n"
            f"💼 *Operaciones hoy:* `{stats.get('daily_trades', 0)}`\n"
            f"📈 *G/P de sesión:* `{'+'if self._session_pnl>=0 else ''}${self._session_pnl:.2f}`\n"
            f"🌐 *Exchange:* `{stats.get('venue', 'UNKNOWN')}`\n"
            f"─────────────────\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send(msg)

    # ══════════════════════════════════════════════════════════════════
    #  Internal Helpers
    # ══════════════════════════════════════════════════════════════════

    def _fire(self, coro) -> None:
        """
        Despacha una coroutine como fire-and-forget task.
        NUNCA bloquea el loop de trading.
        """
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(coro)
            task.add_done_callback(self._handle_task_exception)
        except RuntimeError:
            # No running loop — log and skip
            logger.debug("No event loop running. Telegram alert skipped.")

    @staticmethod
    def _handle_task_exception(task: asyncio.Task) -> None:
        """Callback para capturar excepciones silenciosas en tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning(f"Telegram task failed (non-blocking): {exc}")

    async def _send(self, text: str) -> None:
        """Envía un mensaje de texto al chat configurado."""
        if not self._connected or not self._bot:
            logger.debug("Telegram no conectado. Msg: %s", text[:80])
            return

        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            # Fallback sin Markdown
            try:
                clean_text = text.replace("*", "").replace("`", "")
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=clean_text,
                )
            except Exception as exc:
                logger.warning(f"Telegram send failed: {exc}")

    async def _send_dev(self, text: str) -> None:
        """Envía un mensaje técnico al canal de desarrollo."""
        if not self._connected or not self._bot:
            logger.debug("Telegram dev no conectado. Msg: %s", text[:80])
            return
        # Si no hay dev_chat_id configurado, cae al canal principal
        target = self._dev_chat_id if self._dev_chat_id else self._chat_id
        if not target:
            return
        try:
            await self._bot.send_message(
                chat_id=target,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            try:
                clean = text.replace("*", "").replace("`", "")
                await self._bot.send_message(chat_id=target, text=clean)
            except Exception as exc:
                logger.warning(f"Telegram dev send failed: {exc}")

    def fire_llm_fallback(self, provider: str, reason: str) -> None:
        """Fire-and-forget DEV: todos los LLM fallaron (legacy)."""
        self._fire(self._send_dev_llm_fallback(provider, reason))

    async def _send_dev_llm_fallback(self, provider: str, reason: str) -> None:
        # Classify the error for better diagnostics
        error_diag = self._classify_llm_error(reason)
        msg = (
            f"🤖 *LLM — Sin Respuesta*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📦 *Proveedor:* {provider}\n"
            f"⚠️ *Diagnóstico:* {error_diag}\n"
            f"🔄 *Acción tomada:* Análisis macro en modo heurístico\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send_dev(msg)

    def fire_llm_unavailable(
        self,
        providers_tried: Optional[List[Dict[str, Any]]] = None,
        action_taken: str = "Análisis macro en modo heurístico",
    ) -> None:
        """Fire-and-forget: LLM unavailable with detailed provider diagnostics."""
        self._fire(self._send_llm_unavailable(providers_tried, action_taken))

    async def _send_llm_unavailable(
        self,
        providers_tried: Optional[List[Dict[str, Any]]],
        action_taken: str,
    ) -> None:
        msg = (
            f"🤖 *LLM — Sin Respuesta*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
        )

        primary_error_type = ""
        if providers_tried:
            msg += f"📦 *Proveedores intentados:*\n"
            for prov in providers_tried:
                name = prov.get("name", "Desconocido")
                keys_count = prov.get("keys_count", 0)
                error_type = prov.get("error_type", "")
                diag = self._classify_llm_error(error_type)
                msg += f"  ❌ {name} ({keys_count} claves): {diag}\n"
                if not primary_error_type:
                    primary_error_type = error_type
        else:
            msg += f"📦 *Proveedores:* Ninguno configurado\n"

        # Diagnostic recommendations based on error type
        normalized = primary_error_type.lower() if primary_error_type else ""
        if "dns" in normalized or "could not contact dns" in normalized:
            msg += (
                f"⚠️ *Diagnóstico:* Error de red — no se puede contactar los servidores DNS\n"
                f"  → Verificar: conexión a internet del servidor, firewall, VPN/proxy\n"
            )
        elif "timeout" in normalized:
            msg += (
                f"⚠️ *Diagnóstico:* Tiempo de espera agotado\n"
                f"  → Verificar: latencia de red, estado del proveedor\n"
            )
        elif "rate_limit" in normalized or "429" in normalized:
            msg += (
                f"⚠️ *Diagnóstico:* Límite de solicitudes alcanzado\n"
                f"  → Acción sugerida: rotar a las claves de respaldo o esperar renovación\n"
            )
        elif "401" in normalized or "invalid_api_key" in normalized:
            msg += (
                f"⚠️ *Diagnóstico:* Clave API inválida\n"
                f"  → Verificar: claves API configuradas en .env\n"
            )

        msg += (
            f"🔄 *Acción tomada:* {action_taken}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send_dev(msg)

    def fire_key_cooldown(self, provider: str, key_hint: str,
                          cooldown_minutes: int) -> None:
        """Fire-and-forget DEV: clave LLM entró en cooldown por límite."""
        self._fire(self._send_dev_key_cooldown(provider, key_hint,
                                               cooldown_minutes))

    async def _send_dev_key_cooldown(self, provider: str, key_hint: str,
                                     cooldown_minutes: int) -> None:
        msg = (
            f"🔑 *CLAVE LLM EN COOLDOWN*\n"
            f"─────────────────\n"
            f"📦 *Proveedor:* `{provider}`\n"
            f"🔑 *Clave:* `...{key_hint}`\n"
            f"⏳ *Cooldown:* `{cooldown_minutes} min`\n"
            f"🔄 *Acción:* Rotando a siguiente clave disponible.\n"
            f"─────────────────\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send_dev(msg)

    def fire_llm_infra_error(
        self,
        provider: str,
        category: str,
        detail: str,
        key_hash: str = "",
        retryable: bool = True,
        action: str = "",
    ) -> None:
        """Fire-and-forget DEV: error de infraestructura LLM (DNS/TCP/SSL/TIMEOUT).
        La clave NO fue invalidada."""
        self._fire(self._send_dev_llm_infra_error(
            provider, category, detail, key_hash, retryable, action
        ))

    async def _send_dev_llm_infra_error(
        self,
        provider: str,
        category: str,
        detail: str,
        key_hash: str,
        retryable: bool,
        action: str,
    ) -> None:
        retry_str = "Sí" if retryable else "No"
        msg = (
            f"🔌 *LLM — Error de Infraestructura*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📦 *Proveedor:* {provider}\n"
            f"🏷️ *Categoría:* {category}\n"
            f"⚠️ *Detalle:* {detail}\n"
            f"🔑 *Clave:* ...{key_hash} — CONSERVADA ✅\n"
            f"🔄 *Reintentable:* {retry_str}\n"
        )
        if action:
            msg += f"📋 *Acción:* {action}\n"
        msg += (
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send_dev(msg)

    async def _send_photo(self, photo_bytes: bytes, caption: str = "") -> None:
        """Envía una imagen al chat configurado."""
        if not self._connected or not self._bot:
            return
        try:
            await self._bot.send_photo(
                chat_id=self._chat_id, photo=photo_bytes, caption=caption,
            )
        except Exception as exc:
            logger.warning(f"Telegram photo failed: {exc}")

    def _generate_equity_chart(self, equity_curve: List[float]) -> bytes:
        """Genera chart PNG de equity semanal."""
        fig, ax = plt.subplots(figsize=(10, 4))
        eq = np.array(equity_curve)
        color = "#00d4aa" if eq[-1] >= eq[0] else "#ff4757"
        ax.plot(range(len(eq)), eq, color=color, linewidth=1.5)
        ax.fill_between(range(len(eq)), eq, eq[0], alpha=0.15, color=color)
        ax.axhline(y=eq[0], color="#666", linestyle="--", alpha=0.4)
        ax.set_title("Curva de Equity Semanal", fontsize=12, color="#e0e0e0")
        ax.set_facecolor("#1a1a2e")
        fig.set_facecolor("#0d0d1a")
        ax.tick_params(colors="#999")
        ax.grid(True, alpha=0.15)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100,
                    facecolor=fig.get_facecolor(), bbox_inches="tight")
        buf.seek(0)
        plt.close(fig)
        return buf.read()

    @staticmethod
    def _classify_llm_error(error_info: str) -> str:
        """
        Clasifica un error de LLM en una categoría legible para humanos.
        Usado por fire_startup, fire_llm_fallback, y fire_llm_unavailable.
        """
        if not error_info:
            return "Error desconocido"
        lower = error_info.lower()
        if "could not contact dns" in lower or "dns" in lower:
            return "Sin conexión DNS"
        elif "timeout" in lower:
            return "Tiempo de espera agotado"
        elif "rate_limit" in lower or "429" in lower or "quota" in lower or "exhausted" in lower:
            return "Límite de solicitudes alcanzado"
        elif "401" in lower or "invalid_api_key" in lower or "unauthorized" in lower:
            return "Clave API inválida"
        else:
            # Show first 80 chars of raw error
            return error_info[:80]

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(_TZ_GMT5).strftime("%Y-%m-%d %H:%M:%S GMT-5")

    def __repr__(self) -> str:
        status = "CONNECTED" if self._connected else "OFFLINE"
        return f"<TelegramReporter {status} trades={len(self._weekly_trades)}>"
