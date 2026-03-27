"""
NEXUS Trading System — Telegram Reporter
==========================================
Alertas en tiempo real y reporte semanal automático vía Telegram.

PARTE 1 — Alertas en tiempo real:
  - alerta_trade_ejecutado(trade_data)
  - alerta_circuit_breaker(drawdown, ...)

PARTE 2 — Reporte semanal:
  - Programado con schedule: lunes 7:00 AM GMT-5
  - Métricas completas + mejor/peor trade + régimen de mercado

Usa: python-telegram-bot v20+ (asyncio nativo)
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from dataclasses import dataclass, field
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
    import schedule
    _HAS_SCHEDULE = True
except ImportError:
    _HAS_SCHEDULE = False

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


# ══════════════════════════════════════════════════════════════════════
#  Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class WeeklyMetrics:
    """Métricas de rendimiento semanal."""
    start_date: datetime
    end_date: datetime
    starting_balance: float
    ending_balance: float
    pnl: float
    pnl_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    best_trade: Dict[str, Any]
    worst_trade: Dict[str, Any]
    max_drawdown: float
    sharpe_ratio: float
    market_regime: str = "RANGE"


# ══════════════════════════════════════════════════════════════════════
#  NexusTelegramReporter
# ══════════════════════════════════════════════════════════════════════

class NexusTelegramReporter:
    """
    Reporter de Telegram para el sistema NEXUS.

    Funcionalidades:
    - Alertas en tiempo real (trades, circuit breaker)
    - Reporte semanal automático (lunes 7:00 AM GMT-5)
    """

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._bot: Optional[Bot] = None
        self._initialized = False

        # Historial para reporte semanal
        self._weekly_trades: List[Dict[str, Any]] = []
        self._weekly_equity: List[float] = []
        self._initial_capital: float = 10_000.0

    # ── Lifecycle ─────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Inicializa el bot de Telegram."""
        if not _HAS_TELEGRAM:
            logger.warning("python-telegram-bot no instalado. Reportes deshabilitados.")
            return

        if not self.bot_token or not self.chat_id:
            logger.warning("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados.")
            return

        try:
            self._bot = Bot(token=self.bot_token)
            # Verificar conexión
            me = await self._bot.get_me()
            self._initialized = True
            logger.info("✅ Telegram bot conectado: @%s", me.username)
        except Exception as exc:
            logger.error("Error inicializando Telegram bot: %s", exc)
            self._initialized = False

    # ══════════════════════════════════════════════════════════════
    #  PARTE 1: Alertas en tiempo real
    # ══════════════════════════════════════════════════════════════

    async def alerta_trade_ejecutado(self, trade_data: Dict[str, Any]) -> None:
        """
        Envía alerta de trade ejecutado.

        trade_data esperado:
            symbol:     str   (e.g. "BTCUSDT")
            side:       str   ("BUY" o "SELL")
            entry:      float (precio de entrada)
            stop_loss:  float
            take_profit: float
            size_pct:   float (% del capital)
            confidence: float (0-1)
            capital:    float (capital actual, opcional)
        """
        symbol = trade_data.get("symbol", "???")
        side = trade_data.get("side", "BUY")
        entry = trade_data.get("entry", 0)
        sl = trade_data.get("stop_loss", 0)
        tp = trade_data.get("take_profit", 0)
        size_pct = trade_data.get("size_pct", 0)
        confidence = trade_data.get("confidence", 0)

        # Calcular porcentajes SL/TP
        sl_pct = ((sl - entry) / entry * 100) if entry > 0 else 0
        tp_pct = ((tp - entry) / entry * 100) if entry > 0 else 0

        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        action = "COMPRA" if side == "BUY" else "VENTA"

        msg = (
            f"⚡ *NEXUS | ORDEN DE {action}*\n"
            f"─────────────────\n"
            f"🪙 *Activo:* `{symbol}`\n"
            f"🎯 *Dirección:* {direction}\n"
            f"💵 *Entrada:* `${entry:,.2f}`\n"
            f"🛡️ *Stop Loss:* `${sl:,.2f}` ({sl_pct:+.2f}%)\n"
            f"💰 *Take Profit:* `${tp:,.2f}` ({tp_pct:+.2f}%)\n"
            f"📊 *Exposición:* `{size_pct:.1f}%` del capital\n"
            f"🤖 *Confianza IA:* `{confidence:.0%}`\n"
            f"─────────────────\n"
            f"⏳ NEXUS Core Engine"
        )

        await self._send(msg)

        # Registrar para reporte semanal
        self._weekly_trades.append({
            **trade_data,
            "timestamp": datetime.now(_TZ_GMT5).isoformat(),
        })

        logger.info("Alerta trade enviada: %s %s @ %s", side, symbol, entry)

    async def alerta_circuit_breaker(
        self,
        drawdown: float,
        cooldown_hours: int = 24,
    ) -> None:
        """
        Envía alerta de circuit breaker activado.

        Args:
            drawdown:      Drawdown actual como fracción (e.g. 0.18 = 18%)
            cooldown_hours: Horas de bloqueo (default 24)
        """
        msg = (
            f"🚨 *CIRCUIT BREAKER ACTIVADO*\n"
            f"─────────────────\n"
            f"⚠️ *Atención:* Drawdown Crítico detectado.\n"
            f"⛔ Todas las posiciones han sido *cerradas* de emergencia.\n"
            f"🔒 Sistema de trading *bloqueado* por `{cooldown_hours}` horas.\n"
            f"📉 *Drawdown alcanzado:* `{drawdown:.1%}`\n"
            f"─────────────────\n"
            f"🛡️ NEXUS Risk Management"
        )

        # Usar MarkdownV2 para el escape, pero intentar con Markdown primero
        msg_md = msg

        await self._send(msg_md)
        logger.critical("Alerta circuit breaker enviada: DD=%.1f%%", drawdown * 100)

    async def alerta_error(self, error_msg: str) -> None:
        """Envía alerta de error del sistema."""
        msg = f"⚠️ *NEXUS ERROR*\n\n`{error_msg[:500]}`"
        await self._send(msg)

    # ══════════════════════════════════════════════════════════════
    #  PARTE 2: Reporte semanal
    # ══════════════════════════════════════════════════════════════

    async def enviar_reporte_semanal(
        self,
        trades: Optional[List[Dict[str, Any]]] = None,
        equity_curve: Optional[List[float]] = None,
        capital_actual: Optional[float] = None,
    ) -> None:
        """
        Genera y envía el reporte semanal completo.

        Args:
            trades:        Lista de trades de la semana (None = usar internos)
            equity_curve:  Curva de equity (None = usar interna)
            capital_actual: Capital actual (None = calcular)
        """
        week_trades = trades or self._weekly_trades
        week_equity = equity_curve or self._weekly_equity

        now = datetime.now(_TZ_GMT5)
        start = now - timedelta(days=7)

        # Calcular métricas
        metrics = self._calculate_weekly_metrics(
            trades=week_trades,
            equity_curve=week_equity,
            start_date=start,
            end_date=now,
            capital_actual=capital_actual,
        )

        # Formatear y enviar
        report_text = self._format_weekly_report(metrics)
        await self._send(report_text)

        # Enviar gráfico de equity si hay datos
        if _HAS_MPL and week_equity and len(week_equity) > 1:
            try:
                chart_bytes = self._generate_equity_chart(week_equity)
                await self._send_photo(
                    chart_bytes,
                    caption=f"📈 Equity Curve — Semana {start.strftime('%d/%m')} al {now.strftime('%d/%m')}",
                )
            except Exception as exc:
                logger.error("Error generando gráfico equity: %s", exc)

        # Reset para la próxima semana
        self._weekly_trades = []
        self._weekly_equity = []

        logger.info("Reporte semanal enviado")

    def _calculate_weekly_metrics(
        self,
        trades: List[Dict[str, Any]],
        equity_curve: List[float],
        start_date: datetime,
        end_date: datetime,
        capital_actual: Optional[float] = None,
    ) -> WeeklyMetrics:
        """Calcula las métricas semanales a partir de trades y equity."""

        starting_balance = equity_curve[0] if equity_curve else self._initial_capital
        ending_balance = capital_actual or (equity_curve[-1] if equity_curve else starting_balance)

        pnl = ending_balance - starting_balance
        pnl_pct = (pnl / starting_balance * 100) if starting_balance > 0 else 0

        # Win/Loss desde trades
        winning = [t for t in trades if t.get("pnl", 0) > 0]
        losing = [t for t in trades if t.get("pnl", 0) <= 0]
        total = len(trades)
        win_rate = (len(winning) / total * 100) if total > 0 else 0

        # Profit Factor
        gross_profit = sum(t.get("pnl", 0) for t in winning) if winning else 0
        gross_loss = abs(sum(t.get("pnl", 0) for t in losing)) if losing else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Mejor/Peor trade
        if trades:
            best = max(trades, key=lambda t: t.get("pnl", 0))
            worst = min(trades, key=lambda t: t.get("pnl", 0))
        else:
            best = {"pnl": 0, "symbol": "N/A"}
            worst = {"pnl": 0, "symbol": "N/A"}

        # Max Drawdown desde equity
        max_dd = 0.0
        if equity_curve and len(equity_curve) > 1:
            eq = np.array(equity_curve)
            running_max = np.maximum.accumulate(eq)
            drawdowns = (running_max - eq) / running_max
            max_dd = float(np.max(drawdowns)) * 100

        # Sharpe semanal
        sharpe = 0.0
        if equity_curve and len(equity_curve) > 2:
            eq = np.array(equity_curve)
            returns = np.diff(np.log(eq))
            returns = returns[np.isfinite(returns)]
            if len(returns) > 0 and np.std(returns) > 0:
                sharpe = float(np.mean(returns) / np.std(returns) * math.sqrt(len(returns)))

        # Régimen de mercado
        regime = self._detect_regime(equity_curve)

        return WeeklyMetrics(
            start_date=start_date,
            end_date=end_date,
            starting_balance=round(starting_balance, 2),
            ending_balance=round(ending_balance, 2),
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 2),
            total_trades=total,
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=round(win_rate, 1),
            profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
            best_trade=best,
            worst_trade=worst,
            max_drawdown=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            market_regime=regime,
        )

    def _format_weekly_report(self, m: WeeklyMetrics) -> str:
        """Formatea las métricas en texto para Telegram."""

        pnl_emoji = "📈" if m.pnl >= 0 else "📉"
        regime_emoji = {"BULL": "🐂", "BEAR": "🐻", "RANGE": "↔️"}.get(m.market_regime, "❓")

        best_pnl = m.best_trade.get("pnl", 0)
        best_sym = m.best_trade.get("symbol", "N/A")
        worst_pnl = m.worst_trade.get("pnl", 0)
        worst_sym = m.worst_trade.get("symbol", "N/A")

        report = (
            f"📊 *NEXUS | REPORTE SEMANAL*\n"
            f"🗓️ {m.start_date.strftime('%d/%m/%Y')} al {m.end_date.strftime('%d/%m/%Y')}\n"
            f"─────────────────────────\n"
            f"📈 *P&L Neto:* `{'+' if m.pnl >= 0 else ''}${m.pnl:,.2f}` ({'+' if m.pnl_pct >= 0 else ''}{m.pnl_pct:.2f}%)\n"
            f"💼 *Trades:* `{m.total_trades}` (Win: `{m.winning_trades}` \\| Loss: `{m.losing_trades}`)\n"
            f"🎯 *Win Rate:* `{m.win_rate}%`\n"
            f"⚖️ *Profit Factor:* `{m.profit_factor:.2f}`\n"
            f"📉 *Max Drawdown:* `{m.max_drawdown:.2f}%`\n"
            f"🔪 *Sharpe Ratio:* `{m.sharpe_ratio:.2f}`\n"
            f"─────────────────────────\n"
            f"🏆 *Cúspide:* `+${best_pnl:,.2f}` en `{best_sym}`\n"
            f"💀 *Caída:* `${worst_pnl:,.2f}` en `{worst_sym}`\n"
            f"{regime_emoji} *Régimen del Mercado:* {m.market_regime}\n"
            f"─────────────────────────\n"
            f"💰 *Capital Actual:* `${m.ending_balance:,.2f}`\n"
            f"⚡ NEXUS Analytics"
        )

        # Telegram Markdown tiene problemas con | y otros chars,
        # usar versión limpia como fallback
        report_clean = report.replace('\\|', '|')

        return report_clean

    def _detect_regime(self, equity_curve: List[float]) -> str:
        """Detecta el régimen de mercado: BULL, BEAR o RANGE."""
        if not equity_curve or len(equity_curve) < 10:
            return "RANGE"

        eq = np.array(equity_curve[-50:])  # Últimos 50 puntos
        if len(eq) < 5:
            return "RANGE"

        # Pendiente lineal
        x = np.arange(len(eq))
        slope = np.polyfit(x, eq, 1)[0]

        # Normalizar por valor medio
        normalized_slope = slope / np.mean(eq) * 100

        if normalized_slope > 0.05:
            return "BULL"
        elif normalized_slope < -0.05:
            return "BEAR"
        return "RANGE"

    def _generate_equity_chart(self, equity_curve: List[float]) -> bytes:
        """Genera un gráfico PNG de la curva de equity semanal."""
        fig, ax = plt.subplots(figsize=(10, 4))

        eq = np.array(equity_curve)
        x = range(len(eq))

        color = "#00d4aa" if eq[-1] >= eq[0] else "#ff4757"
        ax.plot(x, eq, color=color, linewidth=1.5)
        ax.fill_between(x, eq, eq[0], alpha=0.15, color=color)
        ax.axhline(y=eq[0], color="#666", linestyle="--", alpha=0.4)

        ax.set_title("Equity Curve Semanal", fontsize=12,
                      fontweight="bold", color="#e0e0e0")
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

    # ══════════════════════════════════════════════════════════════
    #  Scheduling — Lunes 7:00 AM GMT-5
    # ══════════════════════════════════════════════════════════════

    def setup_schedule(self) -> None:
        """
        Configura el schedule para enviar el reporte semanal
        todos los lunes a las 7:00 AM GMT-5.

        Debe llamarse desde main.py al iniciar el sistema.
        Para procesar los trabajos programados, el loop principal
        debe ejecutar `schedule.run_pending()` periódicamente.
        """
        if not _HAS_SCHEDULE:
            logger.warning("Librería 'schedule' no instalada. Reporte semanal deshabilitado.")
            return

        def _weekly_job():
            """Wrapper síncrono para el reporte async."""
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.enviar_reporte_semanal())
            except RuntimeError:
                # No hay loop corriendo, crear uno nuevo
                asyncio.run(self.enviar_reporte_semanal())

        # Programar para lunes 7:00 AM
        # schedule trabaja con hora local, GMT-5 es la zona objetivo
        schedule.every().monday.at("07:00").do(_weekly_job)

        logger.info("📅 Reporte semanal programado: lunes 7:00 AM GMT-5")

    @staticmethod
    def run_pending_schedules() -> None:
        """Ejecuta jobs pendientes de schedule. Llamar desde el loop principal."""
        if _HAS_SCHEDULE:
            schedule.run_pending()

    # ══════════════════════════════════════════════════════════════
    #  Tracking — Se llama desde main.py en cada ciclo
    # ══════════════════════════════════════════════════════════════

    def track_trade(self, trade_data: Dict[str, Any]) -> None:
        """Registra un trade para el reporte semanal."""
        self._weekly_trades.append(trade_data)

    def track_equity(self, value: float) -> None:
        """Registra un punto de equity para el reporte semanal."""
        self._weekly_equity.append(value)

    def set_initial_capital(self, capital: float) -> None:
        """Establece el capital inicial de referencia."""
        self._initial_capital = capital

    # ══════════════════════════════════════════════════════════════
    #  Helpers — Envío
    # ══════════════════════════════════════════════════════════════

    async def _send(self, text: str) -> None:
        """Envía un mensaje de texto al chat configurado."""
        if not self._initialized or not self._bot:
            logger.debug("Bot no inicializado. Mensaje (local): %s", text[:100])
            return

        try:
            await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            # Fallback sin parse_mode si Markdown falla
            try:
                await self._bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                )
            except Exception as exc:
                logger.error("Error enviando mensaje Telegram: %s", exc)

    async def _send_photo(self, photo_bytes: bytes, caption: str = "") -> None:
        """Envía una imagen al chat configurado."""
        if not self._initialized or not self._bot:
            logger.debug("Bot no inicializado. Foto no enviada.")
            return

        try:
            await self._bot.send_photo(
                chat_id=self.chat_id,
                photo=photo_bytes,
                caption=caption,
            )
        except Exception as exc:
            logger.error("Error enviando foto Telegram: %s", exc)

    def __repr__(self) -> str:
        status = "READY" if self._initialized else "NOT_INIT"
        return f"<NexusTelegramReporter status={status} trades={len(self._weekly_trades)}>"


# ══════════════════════════════════════════════════════════════════════
#  Funciones de conveniencia (para llamar desde main.py)
# ══════════════════════════════════════════════════════════════════════

# Instancia singleton (se inicializa en main.py)
_reporter: Optional[NexusTelegramReporter] = None


def get_reporter() -> NexusTelegramReporter:
    """Obtiene la instancia del reporter, creándola si no existe."""
    global _reporter
    if _reporter is None:
        from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        _reporter = NexusTelegramReporter(
            bot_token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID,
        )
    return _reporter


async def alerta_trade_ejecutado(trade_data: Dict[str, Any]) -> None:
    """Atajo para enviar alerta de trade desde cualquier módulo."""
    reporter = get_reporter()
    await reporter.alerta_trade_ejecutado(trade_data)


async def alerta_circuit_breaker(drawdown: float) -> None:
    """Atajo para enviar alerta de circuit breaker."""
    reporter = get_reporter()
    await reporter.alerta_circuit_breaker(drawdown)


# ══════════════════════════════════════════════════════════════════════
#  Test / Validación local
# ══════════════════════════════════════════════════════════════════════

async def _run_validation() -> bool:
    """Envía un paquete masivo de tests en vivo al celular para validar el formato institucional."""
    import sys
    import os

    # Inyectar variables nativas para test móvil real
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    try:
        from nexus.config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    except ImportError:
        print("❌ Error de path: ejecuta python desde el root del proyecto -> python nexus/reporting/weekly_report.py")
        return False

    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    print("\n" + "=" * 60)
    print("  VALIDACION EN VIVO — NEXUS MOBILE UI")
    print("=" * 60)

    if not TELEGRAM_BOT_TOKEN or "tu_token" in TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN no detectado en el .env")
        return False

    reporter = NexusTelegramReporter(bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID)
    await reporter.initialize()

    if not reporter._initialized:
        print("❌ Error de autenticacion con Telegram. Credenciales invalidas.")
        return False
    
    print("✅ Autenticado exitosamente. Iniciando transmisión PUSH...\n")

    # ── Test 1: Formato alerta trade ──────────────────────────────
    print("Enviando [1/3] -> Notificacion de Trade... ", end="", flush=True)
    trade_data = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "entry": 67500.0,
        "stop_loss": 66200.0,
        "take_profit": 69450.0,
        "size_pct": 8.5,
        "confidence": 0.78,
    }
    await reporter.alerta_trade_ejecutado(trade_data)
    print("OK")

    # ── Test 2: Formato circuit breaker ───────────────────────────
    print("Enviando [2/3] -> Alerta Circuit Breaker... ", end="", flush=True)
    await reporter.alerta_circuit_breaker(0.18, 24)
    print("OK")

    # ── Test 3: Reporte semanal ───────────────────────────────────
    print("Enviando [3/3] -> Reporte Financiero + Chart (Grafico)... ", end="", flush=True)

    mock_trades = [
        {"symbol": "BTCUSDT", "pnl": 520.0, "side": "BUY"},
        {"symbol": "ETHUSDT", "pnl": -180.0, "side": "SELL"},
        {"symbol": "BTCUSDT", "pnl": 340.0, "side": "BUY"},
        {"symbol": "BTCUSDT", "pnl": -90.0, "side": "SELL"},
        {"symbol": "SOLUSDT", "pnl": 150.0, "side": "BUY"},
    ]

    # Simular data para grafica
    mock_equity = [10000, 10520, 10340, 10680, 10590, 10740]
    
    await reporter.enviar_reporte_semanal(
        trades=mock_trades,
        equity_curve=mock_equity,
        capital_actual=10740.0
    )
    print("OK")

    # ── Resultado ─────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  SIMULACION FINALIZADA: Revisa tu Telegram ahora.")
    print("=" * 60)

    return True

if __name__ == "__main__":
    import asyncio
    success = asyncio.run(_run_validation())
    import sys
    sys.exit(0 if success else 1)
