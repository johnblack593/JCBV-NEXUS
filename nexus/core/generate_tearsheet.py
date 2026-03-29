"""
NEXUS v4.0 — Institutional Tear Sheet Generator (FASE 5)
=========================================================
Genera reportes de rendimiento de grado institucional para demostrar
edge cuantitativo (Sharpe, Sortino, Max DD, Win Rate, Expected Value).

Dos modos de operación:
  1. HTML interactivo con gráficos Plotly (default)
  2. PDF estático con matplotlib (para portafolio/curriculum)

Fuentes de datos:
  - QuestDB tabla `trades` (fuente primaria — full history)
  - Archivo JSON de resultados locales (fallback offine)

Dependencias:
  - quantstats (si disponible — reportes avanzados)
  - matplotlib (siempre disponible — reportes básicos)
  - plotly (opcional — gráficos interactivos)

Uso:
    generator = TearSheetGenerator()
    await generator.load_from_questdb(questdb_client)
    generator.generate_html("reports/tearsheet.html")
    generator.generate_pdf("reports/tearsheet.pdf")
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("nexus.tearsheet")

# Lazy imports
try:
    import quantstats as qs
    _HAS_QS = True
except ImportError:
    _HAS_QS = False

try:
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive backend for server/CI
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.backends.backend_pdf import PdfPages
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False


# ══════════════════════════════════════════════════════════════════════
#  Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class PerformanceMetrics:
    """Métricas cuantitativas del portafolio."""
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    total_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration_days: int = 0
    profit_factor: float = 0.0
    expected_value: float = 0.0
    calmar_ratio: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    kelly_optimal: float = 0.0
    venue: str = "UNKNOWN"
    period_start: str = ""
    period_end: str = ""


# ══════════════════════════════════════════════════════════════════════
#  Tear Sheet Generator
# ══════════════════════════════════════════════════════════════════════

class TearSheetGenerator:
    """
    Generador de Tear Sheet institucional.

    Calcula todas las métricas usando NumPy vectorizado (sin loops),
    y exporta en HTML (interactivo) o PDF (estático).
    """

    RISK_FREE_RATE = 0.05  # 5% annualized (US Treasury)
    TRADING_DAYS = 252

    def __init__(self) -> None:
        self.trades_df: Optional[pd.DataFrame] = None
        self.equity_curve: Optional[pd.Series] = None
        self.returns: Optional[pd.Series] = None
        self.metrics: Optional[PerformanceMetrics] = None

    # ══════════════════════════════════════════════════════════════════
    #  Data Loading
    # ══════════════════════════════════════════════════════════════════

    async def load_from_questdb(self, questdb_client, limit: int = 10000) -> bool:
        """Carga trades desde QuestDB tabla `trades`."""
        try:
            df = await questdb_client.query_trades(limit=limit)
            if df is None or df.empty:
                logger.warning("No hay trades en QuestDB")
                return False
            self.trades_df = df
            self._compute_equity_curve()
            self._compute_metrics()
            logger.info(f"Loaded {len(df)} trades from QuestDB")
            return True
        except Exception as exc:
            logger.error(f"Error loading from QuestDB: {exc}")
            return False

    def load_from_results(self, results: List[Dict[str, Any]]) -> bool:
        """Carga trades desde lista de dicts (TradeResult serializado)."""
        if not results:
            return False

        self.trades_df = pd.DataFrame(results)
        self._compute_equity_curve()
        self._compute_metrics()
        return True

    def load_from_json(self, path: str) -> bool:
        """Carga trades desde archivo JSON."""
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return self.load_from_results(data)
        except Exception as exc:
            logger.error(f"Error loading JSON: {exc}")
            return False

    # ══════════════════════════════════════════════════════════════════
    #  Computation (Fully Vectorized)
    # ══════════════════════════════════════════════════════════════════

    def _compute_equity_curve(self) -> None:
        """Construye curva de equity a partir de los trades."""
        df = self.trades_df
        if df is None or df.empty:
            return

        # Calcular P&L por trade
        if "pnl" in df.columns:
            pnl = df["pnl"].values.astype(np.float64)
        elif "payout" in df.columns and "size" in df.columns:
            # IQ Option: payout es % sobre el size
            payout_pct = df["payout"].values.astype(np.float64) / 100.0
            size = df["size"].values.astype(np.float64)
            # Win: size * payout_pct, Loss: -size (assuming binary option)
            # If status == FILLED and payout > 0, it's the payout ratio
            pnl = np.where(payout_pct > 0, size * payout_pct, -size)
        else:
            logger.warning("No PnL column found in trades data")
            return

        # Curva de equity acumulada
        equity = np.cumsum(pnl)
        if "timestamp" in df.columns:
            idx = pd.to_datetime(df["timestamp"])
        else:
            idx = pd.RangeIndex(len(equity))

        self.equity_curve = pd.Series(equity, index=idx, name="equity")

        # Returns como serie
        initial_capital = 20.0  # IQ Option challenge default
        capital = initial_capital + equity
        capital = np.maximum(capital, 1e-8)  # Avoid division by zero
        returns = np.diff(np.insert(capital, 0, initial_capital)) / np.maximum(
            np.insert(capital[:-1], 0, initial_capital), 1e-8
        )
        self.returns = pd.Series(returns, index=idx, name="returns")

    def _compute_metrics(self) -> None:
        """Calcula TODAS las métricas institucionales (vectorizado)."""
        df = self.trades_df
        if df is None or df.empty or self.returns is None:
            self.metrics = PerformanceMetrics()
            return

        returns = self.returns.values.astype(np.float64)
        n = len(returns)

        # ── Basic Stats ──────────────────────────────────────────────
        wins = returns[returns > 0]
        losses = returns[returns < 0]
        n_wins = len(wins)
        n_losses = len(losses)

        win_rate = n_wins / n if n > 0 else 0.0
        avg_return = float(np.mean(returns)) if n > 0 else 0.0
        total_return = float(np.sum(returns))

        avg_win = float(np.mean(wins)) if n_wins > 0 else 0.0
        avg_loss = float(np.mean(np.abs(losses))) if n_losses > 0 else 0.0

        # ── Sharpe Ratio (annualized) ────────────────────────────────
        if n > 1:
            excess = returns - (self.RISK_FREE_RATE / self.TRADING_DAYS)
            mean_excess = float(np.mean(excess))
            std_returns = float(np.std(returns, ddof=1))
            sharpe = (mean_excess / std_returns * np.sqrt(self.TRADING_DAYS)) if std_returns > 0 else 0.0
        else:
            sharpe = 0.0

        # ── Sortino Ratio ────────────────────────────────────────────
        if n > 1:
            downside = returns[returns < 0]
            downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else 1e-8
            excess_mean = float(np.mean(returns)) - (self.RISK_FREE_RATE / self.TRADING_DAYS)
            sortino = (excess_mean / downside_std * np.sqrt(self.TRADING_DAYS)) if downside_std > 0 else 0.0
        else:
            sortino = 0.0

        # ── Max Drawdown (vectorized) ────────────────────────────────
        equity = self.equity_curve.values if self.equity_curve is not None else np.cumsum(returns)
        initial = 20.0
        cum_equity = initial + equity
        running_max = np.maximum.accumulate(cum_equity)
        drawdowns = (running_max - cum_equity) / np.maximum(running_max, 1e-8)
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

        # Max DD duration
        dd_duration = 0
        max_dd_duration = 0
        for dd in drawdowns:
            if dd > 0:
                dd_duration += 1
                max_dd_duration = max(max_dd_duration, dd_duration)
            else:
                dd_duration = 0

        # ── Profit Factor ────────────────────────────────────────────
        gross_profit = float(np.sum(wins)) if n_wins > 0 else 0.0
        gross_loss = float(np.sum(np.abs(losses))) if n_losses > 0 else 1e-8
        profit_factor = gross_profit / gross_loss

        # ── Expected Value ───────────────────────────────────────────
        ev = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        # ── Calmar Ratio ─────────────────────────────────────────────
        annual_return = total_return * (self.TRADING_DAYS / max(n, 1))
        calmar = annual_return / max_dd if max_dd > 0 else 0.0

        # ── Kelly Criterion ──────────────────────────────────────────
        if avg_loss > 0 and win_rate > 0:
            b = avg_win / avg_loss
            kelly = (b * win_rate - (1 - win_rate)) / b
            kelly = max(0.0, min(kelly, 0.25))
        else:
            kelly = 0.0

        # Period
        period_start = ""
        period_end = ""
        if "timestamp" in df.columns:
            period_start = str(df["timestamp"].iloc[0])
            period_end = str(df["timestamp"].iloc[-1])

        venue = df["venue"].iloc[0] if "venue" in df.columns else "UNKNOWN"

        self.metrics = PerformanceMetrics(
            total_trades=n,
            win_count=n_wins,
            loss_count=n_losses,
            win_rate=round(win_rate, 4),
            avg_return=round(avg_return, 6),
            total_return_pct=round(total_return * 100, 2),
            sharpe_ratio=round(sharpe, 4),
            sortino_ratio=round(sortino, 4),
            max_drawdown=round(max_dd, 4),
            max_drawdown_duration_days=max_dd_duration,
            profit_factor=round(profit_factor, 4),
            expected_value=round(ev, 6),
            calmar_ratio=round(calmar, 4),
            best_trade=round(float(np.max(returns)), 6) if n > 0 else 0.0,
            worst_trade=round(float(np.min(returns)), 6) if n > 0 else 0.0,
            avg_win=round(avg_win, 6),
            avg_loss=round(avg_loss, 6),
            kelly_optimal=round(kelly, 4),
            venue=str(venue),
            period_start=period_start,
            period_end=period_end,
        )

        logger.info(
            "📊 Metrics computed: %d trades | WR=%.1f%% | Sharpe=%.2f | "
            "Sortino=%.2f | MaxDD=%.1f%% | PF=%.2f | EV=%.4f",
            n, win_rate * 100, sharpe, sortino, max_dd * 100, profit_factor, ev,
        )

    # ══════════════════════════════════════════════════════════════════
    #  Export: HTML (Interactive)
    # ══════════════════════════════════════════════════════════════════

    def generate_html(self, output_path: str = "reports/tearsheet.html") -> str:
        """
        Genera tear sheet HTML interactivo.
        Si quantstats está disponible, usa su engine de reportes.
        Si no, genera un HTML propio con los datos calculados.
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if _HAS_QS and self.returns is not None and len(self.returns) > 5:
            try:
                qs.reports.html(
                    self.returns,
                    benchmark=None,
                    title="NEXUS v4.0 — Institutional Tear Sheet",
                    output=output_path,
                )
                logger.info(f"✅ QuantStats HTML tear sheet → {output_path}")
                return output_path
            except Exception as exc:
                logger.warning(f"QuantStats HTML failed: {exc}. Using custom HTML.")

        # Custom HTML fallback
        html = self._build_custom_html()
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"✅ Custom HTML tear sheet → {output_path}")
        return output_path

    def _build_custom_html(self) -> str:
        """Genera HTML institucional con métricas y equity curve inline."""
        m = self.metrics or PerformanceMetrics()

        # Equity curve as inline SVG data (base64 or simple plot)
        equity_svg = ""
        if _HAS_MPL and self.equity_curve is not None:
            import io
            import base64

            fig, ax = plt.subplots(figsize=(12, 4))
            ax.plot(self.equity_curve.values, color="#00d4aa", linewidth=1.5)
            ax.fill_between(
                range(len(self.equity_curve)), self.equity_curve.values,
                alpha=0.15, color="#00d4aa"
            )
            ax.set_title("Equity Curve", fontsize=14, color="#e0e0e0")
            ax.set_facecolor("#1a1a2e")
            fig.patch.set_facecolor("#0f0f1a")
            ax.tick_params(colors="#888")
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.spines["bottom"].set_color("#333")
            ax.spines["left"].set_color("#333")
            ax.grid(True, alpha=0.1)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)
            img_b64 = base64.b64encode(buf.read()).decode("utf-8")
            equity_svg = f'<img src="data:image/png;base64,{img_b64}" style="width:100%;max-width:900px;">'

        # Color coding for metrics
        wr_color = "#00d4aa" if m.win_rate >= 0.55 else "#ff4757"
        sharpe_color = "#00d4aa" if m.sharpe_ratio >= 1.0 else ("#ffaa00" if m.sharpe_ratio >= 0.5 else "#ff4757")
        ev_color = "#00d4aa" if m.expected_value > 0 else "#ff4757"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>NEXUS v4.0 — Institutional Tear Sheet</title>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ background:#0f0f1a; color:#e0e0e0; font-family:'Inter','SF Pro',system-ui,sans-serif; padding:40px; }}
        .header {{ text-align:center; margin-bottom:40px; }}
        .header h1 {{ font-size:28px; background:linear-gradient(135deg,#00d4aa,#7c4dff); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }}
        .header p {{ color:#888; font-size:14px; margin-top:8px; }}
        .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-bottom:32px; }}
        .card {{ background:#1a1a2e; border:1px solid #2a2a4e; border-radius:12px; padding:20px; text-align:center; }}
        .card .label {{ font-size:12px; color:#888; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px; }}
        .card .value {{ font-size:28px; font-weight:700; }}
        .green {{ color:#00d4aa; }}
        .red {{ color:#ff4757; }}
        .yellow {{ color:#ffaa00; }}
        .equity {{ text-align:center; margin:32px 0; }}
        .section {{ background:#1a1a2e; border:1px solid #2a2a4e; border-radius:12px; padding:24px; margin-bottom:24px; }}
        .section h2 {{ font-size:18px; margin-bottom:16px; color:#7c4dff; }}
        table {{ width:100%; border-collapse:collapse; }}
        th,td {{ padding:10px 16px; text-align:left; border-bottom:1px solid #2a2a4e; font-size:14px; }}
        th {{ color:#888; font-weight:500; }}
        .footer {{ text-align:center; color:#555; font-size:12px; margin-top:40px; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>NEXUS v4.0 — Institutional Tear Sheet</h1>
        <p>Venue: {m.venue} | Period: {m.period_start[:10] if m.period_start else 'N/A'} → {m.period_end[:10] if m.period_end else 'N/A'} | Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
    </div>

    <div class="grid">
        <div class="card"><div class="label">Total Trades</div><div class="value">{m.total_trades}</div></div>
        <div class="card"><div class="label">Win Rate</div><div class="value" style="color:{wr_color}">{m.win_rate:.1%}</div></div>
        <div class="card"><div class="label">Sharpe Ratio</div><div class="value" style="color:{sharpe_color}">{m.sharpe_ratio:.2f}</div></div>
        <div class="card"><div class="label">Sortino Ratio</div><div class="value">{m.sortino_ratio:.2f}</div></div>
        <div class="card"><div class="label">Max Drawdown</div><div class="value red">{m.max_drawdown:.1%}</div></div>
        <div class="card"><div class="label">Profit Factor</div><div class="value green">{m.profit_factor:.2f}</div></div>
        <div class="card"><div class="label">Expected Value</div><div class="value" style="color:{ev_color}">{m.expected_value:.4f}</div></div>
        <div class="card"><div class="label">Kelly Optimal</div><div class="value">{m.kelly_optimal:.1%}</div></div>
    </div>

    <div class="equity">{equity_svg}</div>

    <div class="section">
        <h2>Detailed Metrics</h2>
        <table>
            <tr><th>Metric</th><th>Value</th><th>Benchmark</th></tr>
            <tr><td>Total Return</td><td>{m.total_return_pct:+.2f}%</td><td>—</td></tr>
            <tr><td>Wins / Losses</td><td>{m.win_count} / {m.loss_count}</td><td>—</td></tr>
            <tr><td>Avg Win</td><td class="green">{m.avg_win:.4f}</td><td>—</td></tr>
            <tr><td>Avg Loss</td><td class="red">{m.avg_loss:.4f}</td><td>—</td></tr>
            <tr><td>Best Trade</td><td class="green">{m.best_trade:.4f}</td><td>—</td></tr>
            <tr><td>Worst Trade</td><td class="red">{m.worst_trade:.4f}</td><td>—</td></tr>
            <tr><td>Max DD Duration</td><td>{m.max_drawdown_duration_days} trades</td><td>—</td></tr>
            <tr><td>Calmar Ratio</td><td>{m.calmar_ratio:.2f}</td><td>&gt; 3.0</td></tr>
            <tr><td>Sharpe Ratio</td><td>{m.sharpe_ratio:.2f}</td><td>&gt; 1.5</td></tr>
            <tr><td>Sortino Ratio</td><td>{m.sortino_ratio:.2f}</td><td>&gt; 2.0</td></tr>
        </table>
    </div>

    <div class="footer">
        <p>NEXUS v4.0 • Algorithmic Trading Platform • Confidential</p>
        <p>This document is generated automatically and does not constitute investment advice.</p>
    </div>
</body>
</html>"""

    # ══════════════════════════════════════════════════════════════════
    #  Export: PDF (Static — for Portfolio/Resume)
    # ══════════════════════════════════════════════════════════════════

    def generate_pdf(self, output_path: str = "reports/tearsheet.pdf") -> str:
        """
        Genera tear sheet PDF estático con matplotlib.
        Incluye: Equity Curve, Drawdown, Distribution, Metrics Table.
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if not _HAS_MPL:
            logger.error("matplotlib no disponible — PDF no generado")
            return ""

        m = self.metrics or PerformanceMetrics()

        with PdfPages(output_path) as pdf:
            # ── Page 1: Summary + Equity Curve ───────────────────────
            fig = plt.figure(figsize=(11, 8.5))
            fig.patch.set_facecolor("#0f0f1a")

            # Title
            fig.text(0.5, 0.95, "NEXUS v4.0 — Institutional Tear Sheet",
                     ha="center", va="top", fontsize=20, color="#00d4aa",
                     fontweight="bold")
            fig.text(0.5, 0.91,
                     f"Venue: {m.venue} | Trades: {m.total_trades} | "
                     f"Period: {m.period_start[:10] if m.period_start else 'N/A'} → "
                     f"{m.period_end[:10] if m.period_end else 'N/A'}",
                     ha="center", va="top", fontsize=10, color="#888")

            # Equity Curve
            ax1 = fig.add_axes([0.08, 0.48, 0.84, 0.38])
            ax1.set_facecolor("#1a1a2e")
            if self.equity_curve is not None and len(self.equity_curve) > 0:
                ax1.plot(self.equity_curve.values, color="#00d4aa", linewidth=1.2)
                ax1.fill_between(range(len(self.equity_curve)),
                                 self.equity_curve.values, alpha=0.1, color="#00d4aa")
            ax1.set_title("Cumulative P&L ($)", color="#e0e0e0", fontsize=12, pad=10)
            ax1.tick_params(colors="#888")
            ax1.grid(True, alpha=0.1)
            for spine in ax1.spines.values():
                spine.set_color("#333")

            # Metrics Table
            ax2 = fig.add_axes([0.08, 0.05, 0.84, 0.35])
            ax2.axis("off")

            table_data = [
                ["Win Rate", f"{m.win_rate:.1%}", "Sharpe", f"{m.sharpe_ratio:.2f}"],
                ["Sortino", f"{m.sortino_ratio:.2f}", "Max DD", f"{m.max_drawdown:.1%}"],
                ["Profit Factor", f"{m.profit_factor:.2f}", "EV", f"{m.expected_value:.4f}"],
                ["Avg Win", f"{m.avg_win:.4f}", "Avg Loss", f"{m.avg_loss:.4f}"],
                ["Kelly Opt", f"{m.kelly_optimal:.1%}", "Calmar", f"{m.calmar_ratio:.2f}"],
                ["Best Trade", f"{m.best_trade:.4f}", "Worst Trade", f"{m.worst_trade:.4f}"],
            ]
            table = ax2.table(
                cellText=table_data,
                colLabels=["Metric", "Value", "Metric", "Value"],
                loc="center",
                cellLoc="center",
            )
            table.auto_set_font_size(False)
            table.set_fontsize(10)
            table.scale(1, 1.8)

            for key, cell in table.get_celld().items():
                cell.set_facecolor("#1a1a2e")
                cell.set_edgecolor("#2a2a4e")
                cell.set_text_props(color="#e0e0e0")
                if key[0] == 0:  # Header row
                    cell.set_facecolor("#2a2a4e")
                    cell.set_text_props(color="#7c4dff", fontweight="bold")

            pdf.savefig(fig, facecolor=fig.get_facecolor())
            plt.close(fig)

            # ── Page 2: Returns Distribution + Drawdown ──────────────
            if self.returns is not None and len(self.returns) > 5:
                fig2, (ax3, ax4) = plt.subplots(2, 1, figsize=(11, 8.5))
                fig2.patch.set_facecolor("#0f0f1a")
                fig2.suptitle("Returns Analysis", color="#e0e0e0", fontsize=16, y=0.96)

                # Returns Distribution
                ax3.set_facecolor("#1a1a2e")
                ax3.hist(self.returns.values, bins=50, color="#7c4dff", alpha=0.7, edgecolor="#0f0f1a")
                ax3.axvline(0, color="#ff4757", linewidth=1, linestyle="--", alpha=0.5)
                ax3.set_title("Returns Distribution", color="#e0e0e0", fontsize=12)
                ax3.tick_params(colors="#888")
                ax3.grid(True, alpha=0.1)
                for spine in ax3.spines.values():
                    spine.set_color("#333")

                # Drawdown
                ax4.set_facecolor("#1a1a2e")
                if self.equity_curve is not None:
                    equity = 20.0 + self.equity_curve.values
                    running_max = np.maximum.accumulate(equity)
                    dd = (running_max - equity) / np.maximum(running_max, 1e-8)
                    ax4.fill_between(range(len(dd)), -dd * 100, color="#ff4757", alpha=0.4)
                    ax4.plot(-dd * 100, color="#ff4757", linewidth=0.8)
                ax4.set_title("Drawdown (%)", color="#e0e0e0", fontsize=12)
                ax4.tick_params(colors="#888")
                ax4.grid(True, alpha=0.1)
                for spine in ax4.spines.values():
                    spine.set_color("#333")

                fig2.tight_layout(rect=[0, 0, 1, 0.94])
                pdf.savefig(fig2, facecolor=fig2.get_facecolor())
                plt.close(fig2)

        logger.info(f"✅ PDF Tear Sheet → {output_path}")
        return output_path

    # ══════════════════════════════════════════════════════════════════
    #  Export: JSON Summary
    # ══════════════════════════════════════════════════════════════════

    def export_metrics_json(self, output_path: str = "reports/metrics.json") -> str:
        """Exporta métricas como JSON para dashboards externos."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if self.metrics is None:
            return ""

        import dataclasses
        data = dataclasses.asdict(self.metrics)
        data["generated_at"] = datetime.now(timezone.utc).isoformat()

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        logger.info(f"✅ Metrics JSON → {output_path}")
        return output_path
