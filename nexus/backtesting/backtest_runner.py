"""
NEXUS Trading System — Backtest Runner
========================================
Motor de backtesting con backtrader que integra:
- TechnicalSignalEngine para señales
- QuantRiskManager para gestión de riesgo
- Agentes BULL/BEAR/ARBITRO para decisiones

Pasos:
1. Descarga datos históricos BTCUSDT 1h → CSV
2. Estrategia backtrader con reglas NEXUS
3. Métricas: Sharpe, Sortino, Max DD, Win Rate, Return vs B&H
4. Reporte HTML con gráficos

Uso:
    python backtesting/backtest_runner.py
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np  # type: ignore

# ── Lazy imports para backtrader + matplotlib ─────────────────────
try:
    import backtrader as bt  # type: ignore
    _HAS_BT = True
except ImportError:
    _HAS_BT = False

try:
    import matplotlib  # type: ignore
    matplotlib.use("Agg")  # Backend sin GUI para generar PNGs
    import matplotlib.pyplot as plt  # type: ignore
    import matplotlib.dates as mdates  # type: ignore
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False

# ── Imports del proyecto ──────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.signal_engine import TechnicalSignalEngine  # type: ignore
from core.risk_manager import QuantRiskManager  # type: ignore
from agents.agent_bull import AgentBull  # type: ignore
from agents.agent_bear import AgentBear  # type: ignore
from agents.agent_arbitro import AgentArbitro  # type: ignore

logger = logging.getLogger("nexus.backtest")

# Rutas
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data" / "historical"
_REPORT_DIR = _PROJECT_ROOT / "reports"
_CSV_PATH = _DATA_DIR / "BTCUSDT_1h.csv"


# ══════════════════════════════════════════════════════════════════════
#  PASO 1: Descarga de datos históricos
# ══════════════════════════════════════════════════════════════════════

async def download_historical_data(
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    start_date: str = "2022-01-01",
    output_path: Optional[Path] = None,
) -> Path:
    """
    Descarga datos históricos usando BinanceDataHandler y los guarda como CSV.

    Args:
        symbol:     Par de trading (default: BTCUSDT)
        interval:   Timeframe (default: 1h)
        start_date: Fecha de inicio (YYYY-MM-DD)
        output_path: Ruta de salida (default: data/historical/BTCUSDT_1h.csv)

    Returns:
        Path al archivo CSV generado
    """
    from core.data_handler import BinanceDataHandler  # type: ignore

    out = output_path or _CSV_PATH
    out.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Descargando datos %s %s desde %s...", symbol, interval, start_date)

    handler = BinanceDataHandler(symbols=[symbol], timeframes=(interval,))
    await handler.start()

    try:
        df = await handler.get_historical_data(
            symbol=symbol,
            interval=interval,
            start_date=start_date,
        )

        if df is None or df.empty:  # type: ignore
            raise RuntimeError(f"No se obtuvieron datos para {symbol} {interval}")

        # Guardar como CSV
        df.to_csv(out, index=False)  # type: ignore
        logger.info("Datos guardados en %s (%d filas)", out, len(df))  # type: ignore

    finally:
        await handler.stop()

    return out


def load_csv_data(csv_path: Optional[Path] = None) -> "bt.feeds.PandasData":
    """Carga CSV histórico como feed de backtrader."""
    import pandas as pd  # type: ignore

    path = csv_path or _CSV_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"CSV no encontrado: {path}\n"
            "Ejecutar primero: download_historical_data()"
        )

    df = pd.read_csv(
        path,
        parse_dates=["open_time"],
        index_col="open_time",
    )

    # Renombrar columnas al formato backtrader
    col_map = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    df = df.rename(columns=col_map)

    # Asegurar que las columnas necesarias existen
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            raise ValueError(f"Columna '{col}' no encontrada en CSV")

    data = bt.feeds.PandasData(
        dataname=df,
        datetime=None,  # Usa el index
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        openinterest=-1,
    )

    return data


# ══════════════════════════════════════════════════════════════════════
#  PASO 2: Estrategia Backtrader — NexusStrategy
# ══════════════════════════════════════════════════════════════════════

class NexusSpotStrategy(bt.Strategy):
    """
    Estrategia NEXUS para SPOT / FUTUROS (Margin Trading).

    Reglas:
    - Entrar solo si arbitro confidence ≥ 0.65
    - Stop Loss:   2 × ATR(14)
    - Take Profit:  3 × ATR(14)  (ratio R:B = 1:1.5)
    - Máximo 3 posiciones abiertas simultáneas
    - No operar si circuit_breaker está activado
    """

    params = dict(  # type: ignore
        atr_period=14,
        sl_atr_mult=2.0,      # Stop Loss = 2 × ATR  # type: ignore
        tp_atr_mult=3.0,      # Take Profit = 3 × ATR  # type: ignore
        max_positions=3,
        min_confidence=0.65,  # type: ignore
        lookback=200,         # Mínimo de barras para indicadores
    )

    def __init__(self):
        # Indicador ATR nativo de backtrader
        self.atr = bt.indicators.ATR(self.data, period=self.p.atr_period)

        # NEXUS engines
        self.signal_engine = TechnicalSignalEngine()
        self.risk_manager = QuantRiskManager(log_dir=str(_PROJECT_ROOT / "logs"))
        self.bull = AgentBull()
        self.bear = AgentBear()
        self.arbitro = AgentArbitro()
        self.arbitro.initialize()  # Encendemos la maquina de LLM para la arquitectura Event-Driven

        # Estado
        self._open_orders: Dict[str, Any] = {}
        self._trade_log: List[Dict[str, Any]] = []
        self._equity_curve: List[float] = []
        self._dates: List[datetime] = []

    def next(self):
        # Registrar equity
        self._equity_curve.append(self.broker.getvalue())
        self._dates.append(self.data.datetime.datetime(0))

        # No operar si no hay suficientes barras para los indicadores
        if len(self.data) < self.p.lookback:
            return

        # No operar si circuit breaker activo
        if self.risk_manager.is_circuit_breaker_active():
            return

        # Construir DataFrame OHLCV de las últimas N barras
        df = self._build_ohlcv_df()
        if df is None or df.empty:
            return

        # Generar señal técnica
        try:
            technical = self.signal_engine.generate_signal(df)
        except Exception:
            return

        current_price = self.data.close[0]

        # Agentes construyen argumentos
        self.bull.build_argument(current_price, technical)
        self.bear.build_argument(current_price, technical)
        
        bull_state = self.bull.get_state()
        bear_state = self.bear.get_state()
        
        # ── EVENT-DRIVEN GATING (Fase 8) ─────────────────────────
        # 1. Si ya estamos posicionados, NO consultamos al Árbitro. 
        # La gestión de salida es matemática pura mediante ATR Trailing Stops (más abajo en _manage_active_trade).
        if self.position:
            decision = {"decision": "HOLD", "confidence": 0.0, "reasoning": "Gate: Trailing Stop gestionando salida."}
        else:
            bull_strength = bull_state.get("strength", 0.0)
            bear_strength = bear_state.get("strength", 0.0)
            max_strength = max(bull_strength, bear_strength)
            
            # 2. Asymmetric Noise Filter: Si no hay anomalía direccional clara, silenciamos al LLM.
            if max_strength < 6.5:
                decision = {"decision": "HOLD", "confidence": 0.0, "reasoning": "Gate: Ruido de mercado detectado. LLM en reposo."}
            else:
                # 3. Apertura del Ojo Cuántico: Anomalía detectada, invocamos deliberación
                risk_metrics = {
                    "max_drawdown": self._current_drawdown(),
                    "var_95": 0.0,
                    "current_exposure": self._current_exposure(),
                }
                
                # Inyectamos el Timestamp puro para el Global Caching
                dt_str = self.data.datetime.datetime(0).isoformat()
                market_ctx = {"symbol": "BTCUSDT", "price": current_price, "timestamp": dt_str}
                
                decision = self.arbitro.deliberate(
                    bull_state=bull_state,
                    bear_state=bear_state,
                    risk_metrics=risk_metrics,
                    market_context=market_ctx
                )

        # ── Ejecutar decisión ─────────────────────────────────────
        confidence = decision.get("confidence", 0)
        action = decision.get("decision", "HOLD")
        atr_val = self.atr[0]

        if confidence < self.p.min_confidence:
            return

        n_open = len([o for o in self.broker.getorders() if o.alive()])
        open_positions = len([p for p in self.broker.positions.values() if p.size != 0])

        if action == "BUY" and open_positions < self.p.max_positions:
            size = self._calc_size(decision)
            if size > 0:
                sl_price = current_price - (self.p.sl_atr_mult * atr_val)
                tp_price = current_price + (self.p.tp_atr_mult * atr_val)

                # Orden principal + bracket (SL + TP)
                self.buy_bracket(
                    size=size,
                    stopprice=sl_price,
                    limitprice=tp_price,
                )

                self._trade_log.append({
                    "datetime": self.data.datetime.datetime(0),
                    "action": "BUY",
                    "price": current_price,
                    "size": size,
                    "sl": sl_price,
                    "tp": tp_price,
                    "confidence": confidence,
                    "atr": atr_val,
                })

        elif action == "SELL" and open_positions < self.p.max_positions:
            size = self._calc_size(decision)
            if size > 0:
                sl_price = current_price + (self.p.sl_atr_mult * atr_val)
                tp_price = current_price - (self.p.tp_atr_mult * atr_val)

                self.sell_bracket(
                    size=size,
                    stopprice=sl_price,
                    limitprice=tp_price,
                )

                self._trade_log.append({
                    "datetime": self.data.datetime.datetime(0),
                    "action": "SELL",
                    "price": current_price,
                    "size": size,
                    "sl": sl_price,
                    "tp": tp_price,
                    "confidence": confidence,
                    "atr": atr_val,
                })

    def _build_ohlcv_df(self):
        """Construye un DataFrame OHLCV desde los buffers de backtrader."""
        import pandas as pd  # type: ignore

        n = min(len(self.data), self.p.lookback)
        data = {
            "open": [self.data.open[-i] for i in range(n - 1, -1, -1)],
            "high": [self.data.high[-i] for i in range(n - 1, -1, -1)],
            "low": [self.data.low[-i] for i in range(n - 1, -1, -1)],
            "close": [self.data.close[-i] for i in range(n - 1, -1, -1)],
            "volume": [self.data.volume[-i] for i in range(n - 1, -1, -1)],
        }
        return pd.DataFrame(data)

    def _calc_size(self, decision: Dict[str, Any]) -> float:
        """Calcula el tamaño de la posición basado en la decisión."""
        pct = decision.get("position_size_pct", 5.0) / 100.0
        capital = self.broker.getvalue()
        price = self.data.close[0]
        if price <= 0:
            return 0
        # Tamaño en unidades del activo
        size = (capital * pct) / price
        return round(size, 6)

    def _current_drawdown(self) -> float:
        """Calcula el drawdown actual del portafolio."""
        if not self._equity_curve:
            return 0.0
        peak = max(self._equity_curve)
        current = self.broker.getvalue()
        if peak <= 0:
            return 0.0
        return (peak - current) / peak

    def _current_exposure(self) -> float:
        """Calcula la exposición actual como fracción del capital."""
        total_value = self.broker.getvalue()
        if total_value <= 0:
            return 0.0
        position_value = sum(
            abs(p.size * p.price)
            for p in self.broker.positions.values()
            if p.size != 0
        )
        return position_value / total_value


# ══════════════════════════════════════════════════════════════════════
#  PASO 3: Cálculo de métricas
# ══════════════════════════════════════════════════════════════════════

def calculate_metrics(
    equity_curve: List[float],
    initial_capital: float,
    trades: List[Dict[str, Any]],
    buy_hold_return: float,
    risk_free_rate: float = 0.0,
) -> Dict[str, Any]:
    """
    Calcula las métricas principales del backtest.

    Returns:
        Dict con: sharpe_ratio, sortino_ratio, max_drawdown,
        win_rate, total_return, buy_hold_return, etc.
    """
    equity = np.array(equity_curve, dtype=np.float64)

    if len(equity) < 2:
        return {"error": "Equity curve demasiado corta"}

    # Retornos diarios (log returns)
    returns = np.diff(np.log(equity))
    returns = returns[np.isfinite(returns)]

    if len(returns) == 0:
        return {"error": "Sin retornos calculables"}

    # ── Total Return ──────────────────────────────────────────────
    total_return = (equity[-1] - initial_capital) / initial_capital

    # ── Max Drawdown ──────────────────────────────────────────────
    running_max = np.maximum.accumulate(equity)
    drawdowns = (running_max - equity) / running_max
    max_drawdown = float(np.max(drawdowns))

    # ── Sharpe Ratio (anualizado, asumiendo 24/7 = 8760h, datos 1h) ──
    periods_per_year = 8_760  # Horas en un año
    mean_ret = float(np.mean(returns))
    std_ret = float(np.std(returns, ddof=1))
    sharpe = (
        (mean_ret - risk_free_rate / periods_per_year)
        / std_ret * math.sqrt(periods_per_year)
        if std_ret > 0 else 0.0
    )

    # ── Sortino Ratio ─────────────────────────────────────────────
    downside_returns = returns[returns < 0]
    downside_std = float(np.std(downside_returns, ddof=1)) if len(downside_returns) > 1 else 0.0
    sortino = (
        (mean_ret - risk_free_rate / periods_per_year)
        / downside_std * math.sqrt(periods_per_year)
        if downside_std > 0 else 0.0
    )

    # ── Win Rate ──────────────────────────────────────────────────
    # Calcular desde la equity curve: numero de períodos positivos
    positive_periods = int(np.sum(returns > 0))
    total_periods = len(returns)
    win_rate = positive_periods / total_periods if total_periods > 0 else 0.0

    # ── Profit Factor ─────────────────────────────────────────────
    gross_profit = float(np.sum(returns[returns > 0]))
    gross_loss = abs(float(np.sum(returns[returns < 0])))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # ── Institutional Metrics (Fase 5) ────────────────────────────
    # 1. Calmar Ratio: Annual Return / Max Drawdown
    annualized_return = (total_return + 1) ** (periods_per_year / len(equity)) - 1
    calmar_ratio = annualized_return / max_drawdown if max_drawdown > 0 else float("inf")

    # 2. Tail Ratio: 95th percentile return / Abs(5th percentile return)
    # Medida del "Right Tail" (ganancias extremas) vs "Left Tail" (perdidas extremas)
    p95 = np.percentile(returns, 95)
    p5 = np.percentile(returns, 5)
    tail_ratio = abs(p95 / p5) if p5 != 0 else 0.0

    # 3. Information Ratio (Aproximación contra B&H)
    # Suponiendo `buy_hold_return` se distribuye linealmente (simplificación)
    bh_annualized = (buy_hold_return + 1) ** (periods_per_year / len(equity)) - 1 if len(equity) > 0 else 0
    active_return = annualized_return - bh_annualized
    tracking_error = std_ret * math.sqrt(periods_per_year) # Usando stdev absoluta como proxy temporal
    information_ratio = active_return / tracking_error if tracking_error > 0 else 0.0

    return {
        "total_return": round(total_return * 100, 2),
        "buy_hold_return": round(buy_hold_return * 100, 2),  # type: ignore
        "sharpe_ratio": round(sharpe, 4),  # type: ignore
        "sortino_ratio": round(sortino, 4),  # type: ignore
        "max_drawdown": round(max_drawdown * 100, 2),  # type: ignore
        "win_rate": round(win_rate * 100, 2),  # type: ignore
        "profit_factor": round(profit_factor, 4),  # type: ignore
        "calmar_ratio": round(float(calmar_ratio), 4) if calmar_ratio != float("inf") else float("inf"),  # type: ignore
        "tail_ratio": round(float(tail_ratio), 4),  # type: ignore
        "information_ratio": round(float(information_ratio), 4),  # type: ignore
        "total_trades": len(trades),
        "initial_capital": initial_capital,
        "final_capital": round(float(equity[-1]), 2),  # type: ignore
        "periods": len(equity),
    }


def print_metrics(metrics: Dict[str, Any]) -> None:
    """Imprime las métricas en consola con formato legible."""
    print("\n" + "=" * 60)
    print("  NEXUS BACKTEST — RESULTADOS")
    print("=" * 60)
    print(f"  Capital Inicial:  ${metrics.get('initial_capital', 0):,.2f}")
    print(f"  Capital Final:    ${metrics.get('final_capital', 0):,.2f}")
    print(f"  Total Return:     {metrics.get('total_return', 0):+.2f}%")
    print(f"  Buy & Hold:       {metrics.get('buy_hold_return', 0):+.2f}%")
    print(f"  Total Trades:     {metrics.get('total_trades', 0)}")
    print("-" * 60)
    print(f"  Sharpe Ratio:     {metrics.get('sharpe_ratio', 0):.4f}  (objetivo > 1.5)")
    print(f"  Sortino Ratio:    {metrics.get('sortino_ratio', 0):.4f}  (objetivo > 2.0)")
    print(f"  Max Drawdown:     {metrics.get('max_drawdown', 0):.2f}%  (limite < 20%)")
    print(f"  Win Rate:         {metrics.get('win_rate', 0):.2f}%  (objetivo > 55%)")
    print(f"  Profit Factor:    {metrics.get('profit_factor', 0):.4f}")
    print("-" * 60)
    print("  Métricas Institucionales:")
    print(f"  Calmar Ratio:     {metrics.get('calmar_ratio', 0):.4f}")
    print(f"  Tail Ratio:       {metrics.get('tail_ratio', 0):.4f}  (> 1 indica asimetría positiva)")
    print(f"  Information Ratio:{metrics.get('information_ratio', 0):.4f}  (vs Buy&Hold)")
    print("=" * 60)

    # Advertencias de optimización
    sharpe = metrics.get("sharpe_ratio", 0)
    if sharpe < 1.5:
        print("\n  OPTIMIZACION REQUERIDA")
        print("  Top 3 parametros a ajustar:")
        suggestions = []
        if metrics.get("max_drawdown", 0) > 20:
            suggestions.append("1. Reducir sl_atr_mult (SL mas ceñido) o reducir max_positions")
        if metrics.get("win_rate", 0) < 55:
            suggestions.append("2. Aumentar min_confidence del arbitro (> 0.70)")
        if sharpe < 1.0:
            suggestions.append("3. Reducir tamaño de posicion (position_size_pct)")

        if not suggestions:
            suggestions = [
                "1. Ajustar sl_atr_mult / tp_atr_mult ratio",
                "2. Incrementar min_confidence del arbitro",
                "3. Agregar filtro de volatilidad (no operar con VIX alto)",
            ]

        for s in suggestions:
            print(f"    {s}")


# ══════════════════════════════════════════════════════════════════════
#  PASO 4: Reporte HTML con gráficos
# ══════════════════════════════════════════════════════════════════════

def generate_html_report(
    equity_curve: List[float],
    dates: List[datetime],
    metrics: Dict[str, Any],
    trades: List[Dict[str, Any]],
    output_path: Optional[Path] = None,
) -> Path:
    """
    Genera reporte HTML con 3 gráficos:
    1. Equity Curve
    2. Drawdown Timeline
    3. Monthly Returns Heatmap
    """
    if not _HAS_MPL:
        raise ImportError("matplotlib necesario para generar gráficos")

    out = output_path or (_REPORT_DIR / "backtest_result.html")
    out.parent.mkdir(parents=True, exist_ok=True)

    equity = np.array(equity_curve, dtype=np.float64)

    # ── 1. Equity Curve ───────────────────────────────────────────
    fig1, ax1 = plt.subplots(figsize=(14, 5))
    ax1.plot(dates, equity, color="#00d4aa", linewidth=1.2, label="NEXUS Strategy")
    ax1.fill_between(dates, equity, equity[0], alpha=0.1, color="#00d4aa")
    ax1.axhline(y=equity[0], color="#666", linestyle="--", alpha=0.5, label="Capital Inicial")
    ax1.set_title("Equity Curve", fontsize=14, fontweight="bold", color="#e0e0e0")
    ax1.set_xlabel("Fecha")
    ax1.set_ylabel("Capital ($)")
    ax1.legend()
    ax1.grid(True, alpha=0.2)
    ax1.set_facecolor("#1a1a2e")
    fig1.set_facecolor("#0d0d1a")
    ax1.tick_params(colors="#999")
    ax1.xaxis.label.set_color("#999")
    ax1.yaxis.label.set_color("#999")
    fig1.tight_layout()

    equity_img = _fig_to_base64(fig1)
    plt.close(fig1)

    # ── 2. Drawdown Timeline ──────────────────────────────────────
    running_max = np.maximum.accumulate(equity)
    drawdowns = (running_max - equity) / running_max * 100

    fig2, ax2 = plt.subplots(figsize=(14, 4))
    ax2.fill_between(dates, 0, -drawdowns, color="#ff4757", alpha=0.6)
    ax2.set_title("Drawdown Timeline", fontsize=14, fontweight="bold", color="#e0e0e0")
    ax2.set_xlabel("Fecha")
    ax2.set_ylabel("Drawdown (%)")
    ax2.grid(True, alpha=0.2)
    ax2.set_facecolor("#1a1a2e")
    fig2.set_facecolor("#0d0d1a")
    ax2.tick_params(colors="#999")
    ax2.xaxis.label.set_color("#999")
    ax2.yaxis.label.set_color("#999")
    fig2.tight_layout()

    dd_img = _fig_to_base64(fig2)
    plt.close(fig2)

    # ── 3. Monthly Returns Heatmap ────────────────────────────────
    import pandas as pd  # type: ignore

    eq_series = pd.Series(equity, index=pd.DatetimeIndex(dates))
    monthly_returns = eq_series.resample("ME").last().pct_change().dropna()

    if len(monthly_returns) > 0:
        # Pivotear por año/mes
        mr_df = pd.DataFrame({
            "year": monthly_returns.index.year,
            "month": monthly_returns.index.month,
            "return": (monthly_returns.values * 100),
        })
        heatmap_data = mr_df.pivot_table(
            index="year", columns="month", values="return", aggfunc="mean"
        )

        month_names = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
                       "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        heatmap_data.columns = [month_names[m - 1] for m in heatmap_data.columns]

        fig3, ax3 = plt.subplots(figsize=(14, 4))
        im = ax3.imshow(
            heatmap_data.values, aspect="auto",
            cmap="RdYlGn", interpolation="nearest",
        )
        ax3.set_xticks(range(len(heatmap_data.columns)))
        ax3.set_xticklabels(heatmap_data.columns)
        ax3.set_yticks(range(len(heatmap_data.index)))
        ax3.set_yticklabels(heatmap_data.index)

        # Anotar valores
        for i in range(len(heatmap_data.index)):
            for j in range(len(heatmap_data.columns)):
                val = heatmap_data.iloc[i, j]
                if not np.isnan(val):
                    ax3.text(j, i, f"{val:.1f}%", ha="center", va="center",
                             fontsize=8, color="black", fontweight="bold")

        ax3.set_title("Monthly Returns Heatmap (%)", fontsize=14,
                       fontweight="bold", color="#e0e0e0")
        fig3.colorbar(im, ax=ax3, label="Return %")
        fig3.set_facecolor("#0d0d1a")
        ax3.tick_params(colors="#999")
        fig3.tight_layout()

        heatmap_img = _fig_to_base64(fig3)
        plt.close(fig3)
    else:
        heatmap_img = ""

    # ── Generar HTML ──────────────────────────────────────────────
    html = _build_html(metrics, equity_img, dd_img, heatmap_img, trades)
    out.write_text(html, encoding="utf-8")
    logger.info("Reporte HTML generado: %s", out)

    return out


def _fig_to_base64(fig) -> str:
    """Convierte una figura matplotlib a base64 para embeber en HTML."""
    import base64

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("utf-8")
    buf.close()
    return f"data:image/png;base64,{encoded}"


def _build_html(
    metrics: Dict[str, Any],
    equity_img: str,
    dd_img: str,
    heatmap_img: str,
    trades: List[Dict[str, Any]],
) -> str:
    """Genera el HTML completo del reporte con diseño Glassmorphism Institucional."""

    sharpe_color = "#00d4aa" if metrics.get("sharpe_ratio", 0) >= 1.5 else "#ff4757"
    sortino_color = "#00d4aa" if metrics.get("sortino_ratio", 0) >= 2.0 else "#ff4757"
    dd_color = "#00d4aa" if metrics.get("max_drawdown", 100) < 20 else "#ff4757"
    wr_color = "#00d4aa" if metrics.get("win_rate", 0) > 55 else "#ff4757"
    tr_color = "#00d4aa" if metrics.get("total_return", 0) > 0 else "#ff4757"
    tail_color = "#00d4aa" if metrics.get("tail_ratio", 0) > 1 else "#ff4757"

    # Trades table rows
    trade_rows = ""
    for t in trades[:50]:  # type: ignore
        action = t.get('action', '')
        action_class = 'buy' if action == 'BUY' else 'sell'
        trade_rows += f"""
        <tr>
            <td>{t.get('datetime', '')}</td>
            <td><span class="badge {action_class}">{action}</span></td>
            <td style="font-weight:600;">${t.get('price', 0):,.2f}</td>
            <td>{t.get('size', 0):.6f}</td>
            <td style="color:var(--accent-purple); font-weight:600;">{t.get('confidence', 0):.2f}</td>
            <td style="color:#ff4757;">${t.get('sl', 0):,.2f}</td>
            <td style="color:#00d4aa;">${t.get('tp', 0):,.2f}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NEXUS Backtest Report</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        :root {{
            --bg-main: #07070e;
            --bg-panel: rgba(20, 20, 30, 0.6);
            --accent-cyan: #00d4aa;
            --accent-purple: #7c3aed;
            --text-main: #e0e0f8;
            --text-dim: #8888a0;
            --glass-border: rgba(255, 255, 255, 0.05);
            --danger: #ff4757;
        }}
        @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(-10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        @keyframes slideUp {{ from {{ opacity: 0; transform: translateY(20px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        body {{
            background: var(--bg-main);
            color: var(--text-main);
            font-family: 'Inter', system-ui, -apple-system, sans-serif;
            padding: 2rem 3rem;
            min-height: 100vh;
        }}
        .glass-panel {{
            background: var(--bg-panel);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--glass-border);
            border-radius: 16px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
            padding: 1.5rem;
        }}
        .header {{ text-align: center; margin-bottom: 2.5rem; animation: fadeIn 0.8s ease-out; }}
        .header h1 {{
            font-size: 2.4rem;
            font-weight: 800;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-purple));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
            letter-spacing: 1px;
            text-transform: uppercase;
        }}
        .header .subtitle {{ color: var(--text-dim); font-size: 0.95rem; font-weight: 500; letter-spacing: 0.5px; }}
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1.5rem;
            margin-bottom: 3rem;
        }}
        .metric-card {{
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            transition: transform 0.2s, box-shadow 0.2s, border-color 0.2s;
            animation: slideUp 0.6s ease-out backwards;
            text-align: center;
        }}
        .metric-card:nth-child(1) {{ animation-delay: 0.05s; }}
        .metric-card:nth-child(2) {{ animation-delay: 0.1s; }}
        .metric-card:nth-child(3) {{ animation-delay: 0.15s; }}
        .metric-card:nth-child(4) {{ animation-delay: 0.2s; }}
        .metric-card:nth-child(5) {{ animation-delay: 0.25s; }}
        .metric-card:nth-child(6) {{ animation-delay: 0.3s; }}
        .metric-card:nth-child(7) {{ animation-delay: 0.35s; }}
        .metric-card:nth-child(8) {{ animation-delay: 0.4s; }}
        
        .metric-card:hover {{
            transform: translateY(-4px);
            box-shadow: 0 8px 24px rgba(0, 212, 170, 0.12);
            border-color: rgba(0, 212, 170, 0.3);
        }}
        .metric-card .icon-title {{ display: flex; align-items: center; justify-content: center; gap: 8px; color: var(--text-dim); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }}
        .metric-card .value {{ font-size: 1.8rem; font-weight: 800; text-shadow: 0 0 15px rgba(255,255,255,0.05); }}
        .section-title {{
            font-size: 1.25rem; font-weight: 700; color: var(--text-main);
            margin: 3rem 0 1.2rem; display: flex; align-items: center; gap: 10px;
            text-transform: uppercase; letter-spacing: 1px;
            animation: fadeIn 0.8s ease-out;
        }}
        .section-title::before {{ content: ""; display: block; width: 4px; height: 18px; background: var(--accent-cyan); border-radius: 2px; box-shadow: 0 0 8px var(--accent-cyan); }}
        .chart-container {{ text-align: center; margin-bottom: 2.5rem; animation: slideUp 0.8s ease-out backwards; animation-delay: 0.2s; }}
        .chart-container img {{ width: 100%; max-width: 1200px; border-radius: 8px; display: block; margin: 0 auto; }}
        .table-wrapper {{ overflow-x: auto; animation: slideUp 0.8s ease-out backwards; animation-delay: 0.4s; padding: 0; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; text-align: left; }}
        th {{ background: rgba(0,0,0,0.5); padding: 1.2rem 1rem; color: var(--text-dim); font-weight: 700; text-transform: uppercase; letter-spacing: 1px; position: sticky; top: 0; backdrop-filter: blur(10px); border-bottom: 1px solid rgba(0,212,170,0.2); }}
        td {{ padding: 1rem; border-bottom: 1px solid var(--glass-border); color: var(--text-main); transition: background 0.2s; }}
        tr:hover td {{ background: rgba(0, 212, 170, 0.05); }}
        tr:last-child td {{ border-bottom: none; }}
        .badge {{ padding: 4px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.5px; }}
        .badge.buy {{ background: rgba(0, 212, 170, 0.15); color: #00d4aa; border: 1px solid rgba(0, 212, 170, 0.3); }}
        .badge.sell {{ background: rgba(255, 71, 87, 0.15); color: #ff4757; border: 1px solid rgba(255, 71, 87, 0.3); }}
    </style>
</head>
<body>
    <div class="header">
        <h1>NEXUS Institutional Analytics</h1>
        <p class="subtitle">AUTOMATED BACKTESTING ENGINE | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>

    <div class="metrics-grid">
        <div class="glass-panel metric-card">
            <div class="icon-title">📈 Total Return</div>
            <div class="value" style="color:{tr_color}">{metrics.get('total_return',0):+.2f}%</div>
        </div>
        <div class="glass-panel metric-card">
            <div class="icon-title">💎 Buy &amp; Hold</div>
            <div class="value" style="color:var(--accent-purple)">{metrics.get('buy_hold_return',0):+.2f}%</div>
        </div>
        <div class="glass-panel metric-card">
            <div class="icon-title">⚖️ Sharpe Ratio</div>
            <div class="value" style="color:{sharpe_color}">{metrics.get('sharpe_ratio',0):.4f}</div>
        </div>
        <div class="glass-panel metric-card">
            <div class="icon-title">🛡️ Max Drawdown</div>
            <div class="value" style="color:{dd_color}">{metrics.get('max_drawdown',0):.2f}%</div>
        </div>
        <div class="glass-panel metric-card">
            <div class="icon-title">🎯 Win Rate</div>
            <div class="value" style="color:{wr_color}">{metrics.get('win_rate',0):.2f}%</div>
        </div>
        <div class="glass-panel metric-card">
            <div class="icon-title">💰 Profit Factor</div>
            <div class="value" style="color:var(--text-main)">{metrics.get('profit_factor',0):.2f}</div>
        </div>
        <div class="glass-panel metric-card">
            <div class="icon-title">⚡ Tail Ratio</div>
            <div class="value" style="color:{tail_color}">{metrics.get('tail_ratio',0):.2f}</div>
        </div>
        <div class="glass-panel metric-card">
            <div class="icon-title">🔄 Total Trades</div>
            <div class="value" style="color:var(--text-main)">{metrics.get('total_trades',0)}</div>
        </div>
    </div>

    <h2 class="section-title">Equity Curve</h2>
    <div class="glass-panel chart-container"><img src="{equity_img}" alt="Equity Curve"></div>

    <h2 class="section-title">Drawdown Timeline</h2>
    <div class="glass-panel chart-container"><img src="{dd_img}" alt="Drawdown Timeline"></div>

    {"<h2 class='section-title'>Monthly Returns Heatmap</h2><div class='glass-panel chart-container'><img src='" + heatmap_img + "' alt='Monthly Heatmap'></div>" if heatmap_img else ""}

    <h2 class="section-title">Trade Execution Log</h2>
    <div class="glass-panel table-wrapper">
        <table>
            <thead>
                <tr>
                    <th>Timestamp</th>
                    <th>Action</th>
                    <th>Fill Price</th>
                    <th>Size</th>
                    <th>AI Confidence</th>
                    <th>Stop Loss</th>
                    <th>Take Profit</th>
                </tr>
            </thead>
            <tbody>
                {trade_rows if trade_rows else '<tr><td colspan="7" style="text-align:center;color:var(--text-dim);padding:2rem;">No trade executions registered in this backtest window.</td></tr>'}
            </tbody>
        </table>
    </div>

    <p style="text-align:center; color:var(--text-dim); margin-top:3rem; font-size:0.75rem; font-weight:600; letter-spacing:1px; text-transform:uppercase;">
        NEXUS COMMAND TERMINAL &copy; {datetime.now().year}
    </p>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════
#  Runner principal
# ══════════════════════════════════════════════════════════════════════

def run_backtest(
    csv_path: Optional[Path] = None,
    initial_capital: float = 10_000.0,
    commission: float = 0.001,
    open_report: bool = True,
    trading_mode: str = "spot",
) -> Dict[str, Any]:
    """
    Ejecuta el backtest completo: carga datos, corre estrategia,
    calcula métricas, genera reporte HTML.

    Args:
        csv_path:        Ruta al CSV de datos históricos
        initial_capital: Capital inicial en USD
        commission:      Comisión por operación (0.001 = 0.1%)
        open_report:     Abrir reporte HTML en navegador al terminar
        trading_mode:    "spot" (SL/TP Margin) o "binary" (HFT Fixed Yield)

    Returns:
        Dict con las métricas del backtest
    """
    if not _HAS_BT:
        raise ImportError(
            "backtrader no instalado. Instalar: pip install backtrader2"
        )

    # 1. Cargar datos
    data_feed = load_csv_data(csv_path)

    # 2. Configurar Cerebro
    cerebro = bt.Cerebro()
    
    if trading_mode == "binary":
        try:
            from nexus.backtesting.binary_strategy import NexusBinaryStrategy  # type: ignore
        except ImportError:
            from binary_strategy import NexusBinaryStrategy  # type: ignore
        cerebro.addstrategy(NexusBinaryStrategy)
        cerebro.broker.setcommission(commission=0.0)  # Cero comisiones en binarias (el spread es implícito en el payout 85%)
    else:
        cerebro.addstrategy(NexusSpotStrategy)
        cerebro.broker.setcommission(commission=commission)
        
    cerebro.adddata(data_feed)
    cerebro.broker.setcash(initial_capital)

    # 3. Ejecutar
    print(f"\n  Iniciando backtest con ${initial_capital:,.2f}...")
    results = cerebro.run()
    strategy = results[0]

    final_value = cerebro.broker.getvalue()
    print(f"  Capital final: ${final_value:,.2f}")

    # 4. Buy & Hold return
    if len(strategy._equity_curve) > 0:
        first_close = strategy.data.close[-(len(strategy._equity_curve) - 1)]
        last_close = strategy.data.close[0]
        buy_hold = (last_close - first_close) / first_close if first_close > 0 else 0
    else:
        buy_hold = 0

    # 5. Calcular métricas
    metrics = calculate_metrics(
        equity_curve=strategy._equity_curve,
        initial_capital=initial_capital,
        trades=strategy._trade_log,
        buy_hold_return=buy_hold,
    )

    # 6. Imprimir en consola
    print_metrics(metrics)

    # 7. Generar reporte HTML
    if _HAS_MPL and strategy._equity_curve and strategy._dates:
        try:
            report_path = generate_html_report(
                equity_curve=strategy._equity_curve,
                dates=strategy._dates,
                metrics=metrics,
                trades=strategy._trade_log,
            )
            print(f"\n  Reporte HTML: {report_path}")

            if open_report:
                webbrowser.open(f"file:///{report_path}")
        except Exception as exc:
            logger.error("Error generando reporte HTML: %s", exc)
    else:
        print("\n  (matplotlib no disponible — reporte HTML omitido)")

    return metrics


# ══════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Si el CSV no existe, intentar descargar
    if not _CSV_PATH.exists():
        print("CSV no encontrado. Intentando descargar datos historicos...")
        print("(Requiere API keys de Binance en config/settings.py)")
        try:
            asyncio.run(download_historical_data())
        except Exception as exc:
            print(f"Error descargando datos: {exc}")
            print(f"Coloca un CSV manualmente en: {_CSV_PATH}")
            print("Formato: open_time,open,high,low,close,volume")
            sys.exit(1)

    # Ejecutar backtest
    try:
        metrics = run_backtest(open_report=True)
    except Exception as exc:
        print(f"Error en backtest: {exc}")
        sys.exit(1)
