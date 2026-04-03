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
            "GREEN": "HFT Engine ACTIVE. Full trading enabled.",
            "YELLOW": "Caution mode. Confidence reduced 20%.",
            "RED": "HFT Engine PAUSED. No new trades.",
        }.get(new_regime, "Unknown regime.")

        msg = (
            f"🚨 *MACRO SHIFT*\n"
            f"─────────────────\n"
            f"📊 *Regime Change:* `{old_regime}` → {regime_emoji} `{new_regime}`\n"
            f"⚡ *Action:* {action}\n"
        )
        if reason:
            msg += f"📋 *Reason:* {reason}\n"
        msg += f"─────────────────\n⏱ {self._timestamp()}"

        await self._send(msg)

    # ══════════════════════════════════════════════════════════════════
    #  EVENT 1.5: MARKET BRIEFING
    # ══════════════════════════════════════════════════════════════════

    def fire_market_briefing(self, macro_regime: str, best_asset: str, payout: float) -> None:
        """Fire-and-forget: Resumen del mercado inicial / escaneo dinámico."""
        self._fire(self._send_market_briefing(macro_regime, best_asset, payout))

    async def _send_market_briefing(self, macro_regime: str, best_asset: str, payout: float) -> None:
        regime_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(macro_regime, "⚪")
        msg = (
            f"📰 *NEXUS Morning Briefing*\n"
            f"─────────────────\n"
            f"📊 *Macro:* {regime_emoji} `{macro_regime}`\n"
            f"🏆 *Top Asset:* `{best_asset}`\n"
            f"💰 *Payout:* `{payout:.1f}%`\n"
            f"─────────────────\n"
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
            f"⚠️ *CIRCUIT BREAKER ENGAGED*\n"
            f"─────────────────\n"
            f"⛔ All positions *CLOSED* (emergency)\n"
            f"🔒 Trading *HALTED* for `{cooldown_hours}h`\n"
            f"📉 *Max Drawdown:* `{drawdown:.1%}`\n"
            f"─────────────────\n"
            f"🛡️ NEXUS Risk Management\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send(msg)

    # ══════════════════════════════════════════════════════════════════
    #  EVENT 3: SNIPER ENTRY (Trade Executed)
    # ══════════════════════════════════════════════════════════════════

    def fire_sniper_entry(
        self,
        asset: str,
        direction: str,
        size: float,
        confidence: float,
        venue: str,
        regime: str = "GREEN",
        reason: str = "",
    ) -> None:
        """Fire-and-forget: Trade ejecutado."""
        self._fire(self._send_sniper_entry(
            asset, direction, size, confidence, venue, regime, reason
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
    ) -> None:
        dir_emoji = {
            "CALL": "🟢 CALL", "PUT": "🔴 PUT",
            "BUY": "🟢 LONG", "SELL": "🔴 SHORT",
        }.get(direction, direction)

        mode = "SNIPER" if venue == "IQ_OPTION" else "INSTITUTIONAL"

        msg = (
            f"🎯 *[{mode} ENTRY]*\n"
            f"─────────────────\n"
            f"🪙 *Asset:* `{asset}`\n"
            f"🎯 *Direction:* {dir_emoji}\n"
            f"💵 *Size:* `${size:.2f}`\n"
            f"🤖 *Confidence:* `{confidence:.0%}`\n"
            f"🌐 *Venue:* `{venue}`\n"
            f"📊 *Regime:* `{regime}`\n"
        )
        if reason:
            # Reemplazamos delimitadores comunes por saltos de línea para mostrar el breakdown en estilo lista
            formatted_reason = reason.replace(' |', '\n  ▫️').replace(', ', '\n  ▫️')
            if not formatted_reason.startswith('  ▫️'):
                formatted_reason = f"  ▫️ {formatted_reason}"
            msg += f"📋 *Breakdown:*\n{formatted_reason}\n"
        msg += f"─────────────────\n⏱ {self._timestamp()}"

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
        outcome: str,  # "WIN" or "LOSS"
        venue: str = "IQ_OPTION",
    ) -> None:
        """Fire-and-forget: Resultado del trade."""
        self._fire(self._send_trade_result(
            asset, direction, size, payout_pct, new_balance, outcome, venue
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
    ) -> None:
        if outcome == "WIN":
            pnl = size * (payout_pct / 100.0)
            emoji = "✅"
            pnl_str = f"+${pnl:.2f}"
        else:
            pnl = -size
            emoji = "❌"
            pnl_str = f"-${size:.2f}"

        self._session_pnl += pnl

        # Track for weekly report
        self._weekly_trades.append({
            "asset": asset, "direction": direction, "size": size,
            "pnl": pnl, "outcome": outcome, "venue": venue,
            "timestamp": datetime.now(_TZ_GMT5).isoformat(),
        })
        self._weekly_equity.append(new_balance)

        msg = (
            f"{emoji} *TRADE RESULT*\n"
            f"─────────────────\n"
            f"🪙 *Asset:* `{asset}` | {direction}\n"
            f"💰 *P&L:* `{pnl_str}`\n"
            f"📊 *Payout:* `{payout_pct:.0f}%`\n"
            f"💼 *Balance:* `${new_balance:.2f}`\n"
            f"📈 *Session P&L:* `{'+'if self._session_pnl>=0 else ''}${self._session_pnl:.2f}`\n"
            f"─────────────────\n"
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
            f"🚨 *SYSTEM ERROR*\n"
            f"─────────────────\n"
            f"📦 *Module:* `{module}`\n"
            f"⚠️ *Error:* `{error_msg[:400]}`\n"
            f"─────────────────\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send(msg)

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
                f"📊 *NEXUS | WEEKLY REPORT*\n"
                f"🗓️ {start.strftime('%d/%m/%Y')} → {now.strftime('%d/%m/%Y')}\n"
                f"─────────────────\n"
                f"No trades executed this week.\n"
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
            f"📊 *NEXUS | WEEKLY REPORT*\n"
            f"🗓️ {start.strftime('%d/%m/%Y')} → {now.strftime('%d/%m/%Y')}\n"
            f"─────────────────────────\n"
            f"{pnl_emoji} *P&L:* `{'+'if total_pnl>=0 else ''}${total_pnl:.2f}`\n"
            f"💼 *Trades:* `{len(trades)}` (W:`{len(wins)}` | L:`{len(losses)}`)\n"
            f"🎯 *Win Rate:* `{win_rate:.1f}%`\n"
            f"⚖️ *Profit Factor:* `{pf:.2f}`\n"
            f"📉 *Max Drawdown:* `{max_dd:.2f}%`\n"
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

    def fire_startup(self, venue: str, balance: float) -> None:
        """Fire-and-forget: Pipeline started."""
        self._fire(self._send_startup(venue, balance))

    async def _send_startup(self, venue: str, balance: float) -> None:
        msg = (
            f"🚀 *NEXUS v4.0 ONLINE*\n"
            f"─────────────────\n"
            f"🌐 *Venue:* `{venue}`\n"
            f"💰 *Balance:* `${balance:.2f}`\n"
            f"📊 *Mode:* `{'Sniper (1-3/day)' if venue == 'IQ_OPTION' else 'Institutional'}`\n"
            f"🛡️ *Risk:* Circuit Breaker + Redis CB\n"
            f"─────────────────\n"
            f"⏱ {self._timestamp()}"
        )
        await self._send(msg)

    def fire_shutdown(self, stats: Dict[str, Any]) -> None:
        """Fire-and-forget: Pipeline shutdown."""
        self._fire(self._send_shutdown(stats))

    async def _send_shutdown(self, stats: Dict[str, Any]) -> None:
        msg = (
            f"🛑 *NEXUS v4.0 OFFLINE*\n"
            f"─────────────────\n"
            f"💼 *Trades today:* `{stats.get('daily_trades', 0)}`\n"
            f"📈 *Session P&L:* `{'+'if self._session_pnl>=0 else ''}${self._session_pnl:.2f}`\n"
            f"🌐 *Venue:* `{stats.get('venue', 'UNKNOWN')}`\n"
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
        ax.set_title("Weekly Equity Curve", fontsize=12, color="#e0e0e0")
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
    def _timestamp() -> str:
        return datetime.now(_TZ_GMT5).strftime("%Y-%m-%d %H:%M:%S GMT-5")

    def __repr__(self) -> str:
        status = "CONNECTED" if self._connected else "OFFLINE"
        return f"<TelegramReporter {status} trades={len(self._weekly_trades)}>"
