"""
NEXUS v4.0 — Prometheus Observability Layer (FASE 5)
=====================================================
Métricas de grado institucional exportadas a Prometheus/Grafana.

Métricas expuestas:
  - nexus_trades_total (Counter)          — Total de trades ejecutados
  - nexus_trades_won (Counter)            — Trades ganadores
  - nexus_trades_lost (Counter)           — Trades perdedores
  - nexus_trade_latency_ms (Histogram)    — Latencia signal→fill
  - nexus_signal_confidence (Histogram)   — Distribución de confianza
  - nexus_drawdown_current (Gauge)        — Drawdown actual en tiempo real
  - nexus_balance_usd (Gauge)             — Balance USD actual
  - nexus_pnl_cumulative (Gauge)          — P&L acumulado
  - nexus_circuit_breaker_active (Gauge)  — 1 si CB activo, 0 si no
  - nexus_macro_regime (Gauge)            — 0=GREEN, 1=YELLOW, 2=RED
  - nexus_win_rate (Gauge)                — Win rate running
  - nexus_daily_trades (Gauge)            — Trades del día actual

Uso:
    from nexus.core.observability import NexusMetrics

    metrics = NexusMetrics(port=9090)
    metrics.start_server()           # Lanza HTTP server para Prometheus
    metrics.record_trade(result)     # Registra cada trade
    metrics.update_balance(1234.56)  # Actualiza balance
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger("nexus.observability")

# Lazy import — prometheus_client puede no estar instalado
try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        Info,
        start_http_server,
    )
    _HAS_PROM = True
except ImportError:
    _HAS_PROM = False
    logger.info("prometheus_client no instalado. Métricas desactivadas.")


class NexusMetrics:
    """
    Capa de observabilidad que exporta métricas a Prometheus.

    Si prometheus_client no está instalado, todas las operaciones
    son no-ops silenciosos (graceful degradation).
    """

    def __init__(self, port: int = 9090) -> None:
        self._port = port
        self._server_started = False

        if not _HAS_PROM:
            self._enabled = False
            return

        self._enabled = True

        # ── Counters ─────────────────────────────────────────────────
        self.trades_total = Counter(
            "nexus_trades_total",
            "Total number of trades executed",
            ["venue", "direction", "status"],
        )
        self.trades_won = Counter(
            "nexus_trades_won",
            "Total number of winning trades",
            ["venue"],
        )
        self.trades_lost = Counter(
            "nexus_trades_lost",
            "Total number of losing trades",
            ["venue"],
        )

        # ── Histograms ───────────────────────────────────────────────
        self.trade_latency = Histogram(
            "nexus_trade_latency_ms",
            "Trade execution latency in milliseconds (signal → fill)",
            ["venue"],
            buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
        )
        self.signal_confidence = Histogram(
            "nexus_signal_confidence",
            "Distribution of signal confidence scores",
            ["venue", "direction"],
            buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
        )

        # ── Gauges ───────────────────────────────────────────────────
        self.balance_usd = Gauge(
            "nexus_balance_usd",
            "Current account balance in USD",
            ["venue"],
        )
        self.pnl_cumulative = Gauge(
            "nexus_pnl_cumulative",
            "Cumulative P&L in USD",
            ["venue"],
        )
        self.drawdown_current = Gauge(
            "nexus_drawdown_current",
            "Current portfolio drawdown (0.0 - 1.0)",
            ["venue"],
        )
        self.circuit_breaker = Gauge(
            "nexus_circuit_breaker_active",
            "1 if circuit breaker is active, 0 otherwise",
        )
        self.macro_regime = Gauge(
            "nexus_macro_regime",
            "Macro regime: 0=GREEN, 1=YELLOW, 2=RED",
        )
        self.win_rate = Gauge(
            "nexus_win_rate",
            "Running win rate (0.0 - 1.0)",
            ["venue"],
        )
        self.daily_trades = Gauge(
            "nexus_daily_trades",
            "Number of trades executed today",
            ["venue"],
        )

        # ── Info ─────────────────────────────────────────────────────
        self.system_info = Info(
            "nexus_system",
            "NEXUS system information",
        )
        self.system_info.info({
            "version": "4.0",
            "architecture": "5-layer-async-pipeline",
        })

        # Internal tracking
        self._total_wins = 0
        self._total_trades = 0

    # ══════════════════════════════════════════════════════════════════
    #  Server Lifecycle
    # ══════════════════════════════════════════════════════════════════

    def start_server(self) -> None:
        """Inicia el HTTP server de Prometheus en un thread daemon."""
        if not self._enabled or self._server_started:
            return

        try:
            start_http_server(self._port)
            self._server_started = True
            logger.info(f"📊 Prometheus metrics server → http://0.0.0.0:{self._port}/metrics")
        except Exception as exc:
            logger.warning(f"Failed to start Prometheus server: {exc}")

    # ══════════════════════════════════════════════════════════════════
    #  Recording Methods
    # ══════════════════════════════════════════════════════════════════

    def record_trade(
        self,
        venue: str,
        direction: str,
        status: str,
        latency_ms: float,
        confidence: float,
        pnl: float = 0.0,
    ) -> None:
        """Registra un trade ejecutado en todas las métricas relevantes."""
        if not self._enabled:
            return

        # Counter
        self.trades_total.labels(
            venue=venue, direction=direction, status=status
        ).inc()

        # Win/Loss tracking
        if status == "FILLED":
            self._total_trades += 1
            if pnl > 0:
                self._total_wins += 1
                self.trades_won.labels(venue=venue).inc()
            elif pnl < 0:
                self.trades_lost.labels(venue=venue).inc()

            # Running win rate
            wr = self._total_wins / max(self._total_trades, 1)
            self.win_rate.labels(venue=venue).set(wr)

        # Histogram: latency
        self.trade_latency.labels(venue=venue).observe(latency_ms)

        # Histogram: confidence
        self.signal_confidence.labels(venue=venue, direction=direction).observe(confidence)

    def update_balance(self, balance: float, venue: str = "IQ_OPTION") -> None:
        """Actualiza el gauge de balance."""
        if not self._enabled:
            return
        self.balance_usd.labels(venue=venue).set(balance)

    def update_pnl(self, cumulative_pnl: float, venue: str = "IQ_OPTION") -> None:
        """Actualiza el gauge de P&L acumulado."""
        if not self._enabled:
            return
        self.pnl_cumulative.labels(venue=venue).set(cumulative_pnl)

    def update_drawdown(self, drawdown: float, venue: str = "IQ_OPTION") -> None:
        """Actualiza el gauge de drawdown."""
        if not self._enabled:
            return
        self.drawdown_current.labels(venue=venue).set(drawdown)

    def update_circuit_breaker(self, is_active: bool) -> None:
        """Actualiza el estado del circuit breaker."""
        if not self._enabled:
            return
        self.circuit_breaker.set(1.0 if is_active else 0.0)

    def update_macro_regime(self, regime: str) -> None:
        """Actualiza el régimen macro (GREEN=0, YELLOW=1, RED=2)."""
        if not self._enabled:
            return
        regime_map = {"GREEN": 0, "YELLOW": 1, "RED": 2}
        self.macro_regime.set(regime_map.get(regime.upper(), 0))

    def update_daily_trades(self, count: int, venue: str = "IQ_OPTION") -> None:
        """Actualiza el contador de trades diarios."""
        if not self._enabled:
            return
        self.daily_trades.labels(venue=venue).set(count)
