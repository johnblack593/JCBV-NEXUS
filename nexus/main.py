# pyre-unsafe
"""
NEXUS Trading System — Entry Point
=====================================
Punto de entrada principal. Integra TODOS los módulos en un loop infinito.

Flujo:
  1. data_handler.get_realtime_klines()
  2. signal_engine.generate_signal()
  3. sentiment_engine.get_score()
  4. agent_bull + agent_bear → build_argument()
  5. agent_arbitro → decide()
  6. risk checks (kelly, circuit breaker, correlation)
  7. execute / paper trade
  → esperar próxima vela (60s)

Modos de operación:
  PAPER  → simulado en memoria por 2 semanas, Sharpe > 1.2 para activar LIVE
  LIVE   → ejecución real en Binance
"""

from __future__ import annotations

# ── CRITICAL: Patch DNS resolver BEFORE any library imports aiohttp ──────
# aiodns + pycares (c-ares) falla en Windows con "Could not contact DNS servers".
# Forzamos ThreadedResolver (usa el DNS del sistema operativo) globalmente.
import aiohttp.resolver  # type: ignore  # noqa: E402
aiohttp.resolver.DefaultResolver = aiohttp.resolver.ThreadedResolver  # type: ignore
# ─────────────────────────────────────────────────────────────────────────

import asyncio
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np  # type: ignore

# ── Ajustar sys.path ────────────────────────────
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

# ── Core ────────────────────────────────────────
from core.data_handler import BinanceDataHandler  # type: ignore
from core.signal_engine import TechnicalSignalEngine  # type: ignore
from core.ml_engine import MLEngine  # type: ignore
from core.regime_classifier import RegimeClassifier  # type: ignore
from core.sentiment_engine import SentimentEngine  # type: ignore
from core.risk_manager import QuantRiskManager  # type: ignore
from core.execution_engine import ExecutionEngine, OrderRequest, OrderSide, OrderType  # type: ignore
from core.evolutionary_agent import EvolutionaryAgent  # type: ignore
from core.structured_logger import get_quant_logger # type: ignore
from scripts.auto_calibrate import WFOAutoCalibrator  # type: ignore

# ── Agents ──────────────────────────────────────
from agents.agent_bull import AgentBull  # type: ignore
from agents.agent_bear import AgentBear  # type: ignore
from agents.agent_arbitro import AgentArbitro  # type: ignore

# ── Reporting ───────────────────────────────────
from reporting.weekly_report import NexusTelegramReporter  # type: ignore

# ── Config ──────────────────────────────────────
from config.settings import (  # type: ignore
    trading_config,
    risk_config,
    ml_config,
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)


# ─────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nexus")

# Timezone GMT-5
_TZ = timezone(timedelta(hours=-5))


# ─────────────────────────────────────────────────
#  Trading Mode
# ─────────────────────────────────────────────────

class TradingMode(Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


# ─────────────────────────────────────────────────
#  Paper Trade Simulator
# ─────────────────────────────────────────────────

class PaperTrader:
    """
    Simula ejecución de órdenes en memoria.
    Reemplaza al ExecutionEngine durante la fase de paper trading.
    """

    def __init__(self, initial_capital: float = 10_000.0) -> None:
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.positions: List[Dict[str, Any]] = []
        self.trade_history: List[Dict[str, Any]] = []
        self.equity_curve: List[float] = [initial_capital]
        self.start_time = datetime.now(_TZ)

    def execute_order(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: float,
        stop_loss: float,
        take_profit: float,
        confidence: float,
    ) -> Dict[str, Any]:
        """Simula una ejecución de orden con slippage y comision."""
        slippage_rate = 0.0002  # 0.02% slippage
        exec_price = price * (1 + slippage_rate) if side == "BUY" else price * (1 - slippage_rate)
        
        commission = exec_price * quantity * 0.0005  # 0.05% comisión (Taker)

        trade = {
            "symbol": symbol,
            "side": side,
            "entry": exec_price,
            "quantity": quantity,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": confidence,
            "commission": commission,
            "timestamp": datetime.now(_TZ).isoformat(),
            "status": "OPEN",
            "pnl": 0.0,
        }

        self.positions.append(trade)
        self.capital -= commission
        self.trade_history.append(trade)

        logger.info(
            "[PAPER] %s %s %.6f @ $%.2f (ejec: $%.2f) | SL=$%.2f TP=$%.2f | Fee=$%.2f",
            side, symbol, quantity, price, exec_price, stop_loss, take_profit, commission
        )
        return trade

    def update_positions(self, symbol: str, current_price: float) -> None:
        """Actualiza posiciones abiertas contra precio actual, aplicando comisiones de salida."""
        closed = []
        for pos in self.positions:
            if pos["symbol"] != symbol or pos["status"] != "OPEN":
                continue

            entry = pos["entry"]
            qty = pos["quantity"]
            sl = pos["stop_loss"]
            tp = pos["take_profit"]
            
            # Simulamos cierre. En SL asumimos peor slippage (orden mercado). En TP mejor o nulo (límite).
            close_price = 0.0
            close_reason = ""

            if pos["side"] == "BUY":
                if current_price <= sl:
                    close_price = sl * (1 - 0.0002) # Extra slippage en SL
                    close_reason = "CLOSED_SL"
                elif current_price >= tp:
                    close_price = tp
                    close_reason = "CLOSED_TP"
                    
            elif pos["side"] == "SELL":
                if current_price >= sl:
                    close_price = sl * (1 + 0.0002) # Extra slippage en SL
                    close_reason = "CLOSED_SL"
                elif current_price <= tp:
                    close_price = tp
                    close_reason = "CLOSED_TP"

            if close_price > 0.0:
                gross_pnl = (close_price - entry) * qty if pos["side"] == "BUY" else (entry - close_price) * qty
                exit_commission = close_price * qty * 0.0005
                net_pnl = gross_pnl - exit_commission
                
                pos["pnl"] = net_pnl
                pos["status"] = close_reason
                self.capital += net_pnl
                closed.append(pos)

        for pos in closed:
            logger.info(
                "[PAPER] Cerrado %s %s: Net PnL=$%.2f (%s)",
                pos["side"], pos["symbol"], pos["pnl"], pos["status"],
            )

        if closed:
            self._save_trade_history()

        # Registrar equity (sin descontar comisión latente por simpleza)
        unrealized = sum(
            ((current_price - p["entry"]) * p["quantity"] if p["side"] == "BUY"
             else (p["entry"] - current_price) * p["quantity"])
            for p in self.positions if p["status"] == "OPEN"
        )
        self.equity_curve.append(self.capital + unrealized)

    def get_open_count(self) -> int:
        return sum(1 for p in self.positions if p["status"] == "OPEN")

    def get_sharpe(self) -> float:
        """Calcula Sharpe anualizado del paper trading."""
        if len(self.equity_curve) < 10:
            return 0.0
        eq = np.array(self.equity_curve, dtype=np.float64)
        returns = np.diff(np.log(eq))
        returns = returns[np.isfinite(returns)]
        if len(returns) == 0 or np.std(returns, ddof=1) == 0:
            return 0.0
        return float(np.mean(returns) / np.std(returns, ddof=1) * math.sqrt(8760))

    def get_elapsed_days(self) -> float:
        return (datetime.now(_TZ) - self.start_time).total_seconds() / 86400

    def get_summary(self) -> Dict[str, Any]:
        closed = [t for t in self.trade_history if t["status"] != "OPEN"]
        wins = [t for t in closed if t["pnl"] > 0]
        return {
            "days": float(f"{self.get_elapsed_days():.1f}"),
            "capital": float(f"{self.capital:.2f}"),
            "total_trades": len(closed),
            "win_rate": float(f"{(len(wins) / len(closed) * 100):.1f}") if closed else 0.0,
            "sharpe": float(f"{self.get_sharpe():.4f}"),
            "return_pct": float(f"{((self.capital - self.initial_capital) / self.initial_capital * 100):.2f}"),
        }

    def close_all_positions(self) -> None:
        """Cierra todas las posiciones abiertas (para circuit breaker)."""
        closed_any = False
        for pos in self.positions:
            if pos["status"] == "OPEN":
                pos["status"] = "CLOSED_CB"
                closed_any = True
        
        if closed_any:
            self._save_trade_history()

    def _save_trade_history(self) -> None:
        """Persiste el historial de trades en logs/paper_trades.json para el Post-Mortem."""
        import os, json
        os.makedirs("logs", exist_ok=True)
        try:
            with open("logs/paper_trades.json", "w", encoding="utf-8") as f:
                json.dump(self.trade_history, f, indent=4)
        except Exception as e:
            logger.error(f"Error guardando paper_trades.json: {e}")


# ─────────────────────────────────────────────────
#  NEXUS System
# ─────────────────────────────────────────────────

class NexusSystem:
    """
    Orquestador principal del sistema de trading NEXUS.

    Modos:
    - PAPER: Simula trades en memoria durante 2 semanas.
             Si Sharpe > 1.2 → cambia a LIVE automáticamente.
    - LIVE:  Ejecuta órdenes reales en Binance.
    """

    PAPER_DURATION_DAYS = 14       # 2 semanas de paper trading
    PAPER_SHARPE_GATE = 1.2        # Sharpe mínimo para activar LIVE
    CYCLE_INTERVAL_SECS = 60       # Intervalo entre ciclos (1 minuto)
    MAX_OPEN_POSITIONS = 3         # Máximo de posiciones simultáneas (se sobreescribe con WFO)
    MIN_CONFIDENCE = 0.65          # Confianza mínima del árbitro (se sobreescribe con WFO)

    def __init__(
        self,
        mode: TradingMode = TradingMode.PAPER,
        initial_capital: float = 10_000.0,
    ) -> None:
        self.mode = mode

        # ── Core Engines ──────────────────────────────
        self.data_handler = BinanceDataHandler(
            symbols=trading_config.symbols,
            timeframes=tuple(trading_config.timeframes),
        )
        self.signal_engine = TechnicalSignalEngine()
        self.ml_engine = MLEngine()
        self.regime_classifier = RegimeClassifier(window=14, clusters=4)
        self.sentiment_engine = SentimentEngine()
        self.risk_manager = QuantRiskManager(log_dir=str(_ROOT / "logs"))
        self.execution_engine = ExecutionEngine(
            api_key=BINANCE_API_KEY,
            api_secret=BINANCE_API_SECRET,
        )

        # ── Agents ────────────────────────────────────
        self.bull = AgentBull()
        self.bear = AgentBear()
        self.arbitro = AgentArbitro()

        # ── Reporting ─────────────────────────────────
        self.telegram = NexusTelegramReporter(
            bot_token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID,
        )

        # ── Evolutionary Agent ────────────────────────
        self.evolutionary = EvolutionaryAgent(
            ml_engine=self.ml_engine,
            telegram_reporter=self.telegram,
            risk_manager=self.risk_manager,
        )

        # ── WFO Auto-Calibrator (Fase 12) ─────────────
        self.calibrator = WFOAutoCalibrator()
        self._wfo_params = self.calibrator.load_active_params()

        # ── Paper Trader ──────────────────────────────
        self.paper_trader = PaperTrader(initial_capital=initial_capital)

        # ── State ─────────────────────────────────────
        self._running = False
        self._cycle_count = 0
        self._initial_capital = initial_capital

    # ══════════════════════════════════════════════════════════════
    #  Lifecycle
    # ══════════════════════════════════════════════════════════════

    async def initialize(self) -> None:
        """Inicializa todos los subsistemas."""
        logger.info("=" * 60)
        logger.info("  NEXUS Trading System v1.0")
        logger.info("  Modo: %s", self.mode.value)
        logger.info("=" * 60)

        # Telegram
        await self.telegram.initialize()

        # Árbitro (LLM o fallback heurístico)
        self.arbitro.initialize()

        # Scheduling
        self.telegram.setup_schedule()
        self.evolutionary.setup_schedule()
        self.calibrator.setup_schedule()
        logger.info("WFO Auto-Calibrator programado (sábados 00:00 GMT-5)")

        # Execution engine solo en modo LIVE
        if self.mode == TradingMode.LIVE:
            try:
                await self.execution_engine.initialize()
                logger.info("Execution engine conectado (LIVE)")
            except Exception as exc:
                logger.error("Error inicializando execution engine: %s", exc)
                logger.warning("Forzando modo PAPER por seguridad")
                self.mode = TradingMode.PAPER

        logger.info("Todos los subsistemas inicializados")

    async def run(self) -> None:
        """Loop principal de trading infinito."""
        self._running = True
        logger.info("Sistema en ejecucion — modo %s", self.mode.value)

        try:
            await self.data_handler.start()

            while self._running:
                self._cycle_count += 1

                # ── Trading Cycle ─────────────────────
                await self._trading_cycle()

                # ── Scheduling (reportes, evolución, calibración) ──
                self.telegram.run_pending_schedules()
                self.evolutionary.run_pending()
                self.calibrator.run_pending()

                # ── Hot-Reload WFO Params (cada 60 ciclos ≈ 1h) ──
                if self._cycle_count % 60 == 0:
                    self._wfo_params = self.calibrator.load_active_params()

                # ── Paper → Live Gate ─────────────────
                if self.mode == TradingMode.PAPER:
                    await self._check_paper_to_live()

                # ── Esperar próxima vela ──────────────
                await asyncio.sleep(self.CYCLE_INTERVAL_SECS)

        except KeyboardInterrupt:
            logger.info("Interrupcion recibida")
        except Exception as exc:
            logger.critical("Error fatal: %s", exc)
            await self.telegram.alerta_error(str(exc))
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Apagado ordenado."""
        logger.info("Apagando NEXUS Trading System...")
        self._running = False

        try:
            await self.data_handler.stop()
        except Exception:
            pass

        # Resumen final si era paper trading
        if self.mode == TradingMode.PAPER:
            summary = self.paper_trader.get_summary()
            logger.info("Paper Trading Summary: %s", summary)

        logger.info("NEXUS apagado correctamente")

    # ══════════════════════════════════════════════════════════════
    #  CICLO PRINCIPAL — 7 pasos
    # ══════════════════════════════════════════════════════════════

    async def _trading_cycle(self) -> None:
        """Ejecuta un ciclo completo de análisis y decisión concurrentemente."""
        async def safe_process(s: str) -> None:
            try:
                await self._process_symbol(s)
            except Exception as exc:
                logger.exception("Error en ciclo %s: %s", s, exc)

        tasks = [safe_process(symbol) for symbol in trading_config.symbols]
        await asyncio.gather(*tasks)

    async def _process_symbol(self, symbol: str) -> None:
        """Procesa un símbolo individual a través del pipeline completo."""

        # ── PASO 1: Datos frescos ─────────────────────────────────
        df = self.data_handler.get_dataframe(symbol)
        if df is None or df.empty:
            return

        current_price = float(df["close"].iloc[-1])

        # Actualizar posiciones paper contra precio actual
        if self.mode == TradingMode.PAPER:
            self.paper_trader.update_positions(symbol, current_price)

        # ── PASO 2: Señal técnica ─────────────────────────────────
        try:
            technical = self.signal_engine.generate_signal(df)
        except Exception as exc:
            logger.debug("Signal engine error: %s", exc)
            return

        # ── PASO 2.5: GATING ALGORÍTMICO (Ahorro LLM Institucional) 
        if technical.get("signal") == "HOLD" and technical.get("confidence", 0.0) < 0.4:
            rsi_ind = technical.get("indicators", {}).get("RSI")
            if rsi_ind and hasattr(rsi_ind, "value") and (40 <= rsi_ind.value <= 60):
                logger.info("Gating [%s]: Mercado en rango (RSI: %.1f, Conf: %.2f) — Bypass LLM (HOLD)", symbol, rsi_ind.value, technical.get("confidence", 0.0))
                return

        # ── PASO 3: Sentimiento / on-chain ────────────────────────
        on_chain: Dict[str, Any] = {}
        try:
            sentiment_result = await self.sentiment_engine.analyze(symbol.replace("USDT", ""))
            on_chain = {
                "overall_score": sentiment_result.overall_score,
                "news_score": sentiment_result.news_score,
                "on_chain_score": sentiment_result.on_chain_score,
                "confidence": sentiment_result.confidence,
            }
        except Exception:
            # Sentimiento no es crítico, continuar sin él
            on_chain = {"overall_score": 0.0, "news_score": 0.0, "on_chain_score": 0.0}

        # ── PASO 3.5: Alt-Data (Microestructura) ──────────────────
        alt_data = self.data_handler.get_alternative_data(symbol)

        # ── PASO 4: Agentes construyen argumentos ─────────────────
        bull_arg = self.bull.build_argument(current_price, technical, on_chain, alt_data)
        bear_arg = self.bear.build_argument(current_price, technical, on_chain, alt_data)

        # ── PASO 5: Árbitro decide ────────────────────────────────
        risk_metrics = {
            "max_drawdown": self._get_current_drawdown(),
            "var_95": 0.0,
            "current_exposure": self._get_current_exposure(),
        }
        
        regime_data = self.regime_classifier.detect_regime(df)

        decision = self.arbitro.deliberate(
            bull_state=self.bull.get_state(),
            bear_state=self.bear.get_state(),
            risk_metrics=risk_metrics,
            market_context={"symbol": symbol, "price": current_price, "regime": regime_data},
        )

        # ── PASO 6: Verificar y ejecutar ──────────────────────────
        confidence = decision.get("confidence", 0)
        action = decision.get("decision", "HOLD")

        # Hot-Reload: MIN_CONFIDENCE desde WFO params
        active_min_conf = self._wfo_params.get("spot", {}).get("min_confidence", self.MIN_CONFIDENCE)
        if action == "HOLD" or confidence < active_min_conf:
            return

        # ── Calcular ATR para sizing y SL/TP ──────────────────────
        atr = self._estimate_atr(df)

        # 6a. Kelly Criterion → tamaño de posición
        win_rate = 0.55  # Estimación base, se actualiza con historial
        avg_win = 1.5
        avg_loss = 1.0
        kelly_size = self.risk_manager.kelly_criterion(win_rate, avg_win, avg_loss)
        position_pct = kelly_size * 100  # Como porcentaje

        # 6b. ATR-Based Sizing (Volatility-Scaled)
        capital = await self._get_current_capital()
        atr_pct = self.risk_manager.atr_position_size(
            capital=capital,
            atr=atr,
            current_price=current_price,
        )
        
        # Combinar Kelly con ATR Sizing (usar el más conservador)
        position_pct = min(position_pct, atr_pct)

        # Usar position_size del árbitro si disponible
        arb_pct = decision.get("position_size_pct", 0)
        if arb_pct > 0:
            position_pct = min(position_pct, arb_pct)

        if position_pct <= 0:
            return

        # 6c. Circuit Breaker check
        current_dd = self._get_current_drawdown()
        if self.risk_manager.circuit_breaker_check(current_dd):
            logger.warning("CIRCUIT BREAKER: Orden bloqueada para %s", symbol)
            if self.mode == TradingMode.PAPER:
                self.paper_trader.close_all_positions()
            await self.telegram.alerta_circuit_breaker(current_dd)
            return

        # 6d. Correlation Penalty check (Nivel Portafolio)
        open_positions = self._get_open_positions_for_corr()
        
        # Recolectar retornos recientes de todos los símbolos operables
        returns_data = {}
        for sym in trading_config.symbols:
            hist_df = self.data_handler.get_dataframe(sym)
            if hist_df is not None and not hist_df.empty:
                closes = hist_df["close"].values[-31:]  # Últimas 30 velas
                if len(closes) > 1:
                    rets = np.diff(closes) / closes[:-1]
                    returns_data[sym] = rets.tolist()

        penalty = self.risk_manager.correlation_penalty(
            new_symbol=symbol,
            existing_positions=open_positions,
            returns_data=returns_data,
        )

        if penalty == 0.0:
            logger.warning("CORRELACION: Orden bloqueada para %s (Penalty 100%%)", symbol)
            return
            
        # Aplicar el factor de penalización al tamaño de posición
        original_pct = position_pct
        position_pct *= penalty
        if penalty < 1.0:
            logger.info("Sizing reducido por correlación: %.1f%% → %.1f%%", original_pct, position_pct)

        # 6e. Max positions check
        n_open = await self._get_open_position_count()
        if n_open >= self.MAX_OPEN_POSITIONS:
            logger.info("Max posiciones (%d) alcanzadas", self.MAX_OPEN_POSITIONS)
            return

        # ── Calcular SL/TP con ATR (Hot-Reload desde WFO) ──────────
        side = "BUY" if action == "BUY" else "SELL"
        sl_mult = self._wfo_params.get("spot", {}).get("sl_atr_mult", 2.0)
        tp_mult = self._wfo_params.get("spot", {}).get("tp_atr_mult", 3.0)

        if side == "BUY":
            stop_loss = current_price - (sl_mult * atr)
            take_profit = current_price + (tp_mult * atr)
        else:
            stop_loss = current_price + (sl_mult * atr)
            take_profit = current_price - (tp_mult * atr)

        # ── Calcular quantity ─────────────────────────────────────
        quantity = (capital * position_pct / 100) / current_price
        quantity = float(f"{quantity:.6f}")

        if quantity <= 0:
            return

        # ── PASO 7: Ejecutar orden ────────────────────────────────
        sl_pct = abs((stop_loss - current_price) / current_price * 100)
        tp_pct = abs((take_profit - current_price) / current_price * 100)

        trade_data = {
            "symbol": symbol,
            "side": side,
            "entry": current_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "size_pct": position_pct,
            "confidence": confidence,
        }

        if self.mode == TradingMode.PAPER:
            # ── Paper Trading ─────────────────────────
            self.paper_trader.execute_order(
                symbol=symbol,
                side=side,
                price=current_price,
                quantity=quantity,
                stop_loss=stop_loss,
                take_profit=take_profit,
                confidence=confidence,
            )
            # Track equity para reporter
            self.telegram.track_equity(self.paper_trader.capital)
            self.telegram.track_trade(trade_data)

            # Phase 6: JSONL Export
            try:
                qlogger = get_quant_logger()
                qlogger.log_trade_execution(
                    symbol=symbol, action=side, size=quantity, price=current_price, trade_id="paper"
                )
            except Exception as e:
                logger.error("Error exportando telemetria trade PAPER: %s", e)

        elif self.mode == TradingMode.LIVE:
            # ── Ejecución Real ────────────────────────
            try:
                order_req = OrderRequest(
                    symbol=symbol,
                    side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                    order_type=OrderType.OCO,
                    quantity=quantity,
                    price=current_price,
                    stop_price=stop_loss,
                    take_profit_price=take_profit,
                )
                result = await self.execution_engine.execute_order(order_req)
                logger.info("LIVE ORDER: %s", result)

                self.telegram.track_trade(trade_data)
                
                # Phase 6: JSONL Export
                try:
                    qlogger = get_quant_logger()
                    qlogger.log_trade_execution(
                        symbol=symbol, action=side, size=quantity, price=current_price, trade_id="live"
                    )
                except Exception as e:
                    logger.error("Error exportando telemetria trade LIVE: %s", e)

            except Exception as exc:
                logger.error("Error ejecutando orden LIVE: %s", exc)
                await self.telegram.alerta_error(f"Error orden {symbol}: {exc}")
                return

        # ── Alerta Telegram ───────────────────────────
        await self.telegram.alerta_trade_ejecutado(trade_data)

        logger.info(
            "[%s] %s %s @ $%.2f | SL=$%.2f (-%.1f%%) TP=$%.2f (+%.1f%%) | Conf=%.0f%%",
            self.mode.value, side, symbol, current_price,
            stop_loss, sl_pct, take_profit, tp_pct, confidence * 100,
        )

    # ══════════════════════════════════════════════════════════════
    #  Paper → Live Gate
    # ══════════════════════════════════════════════════════════════

    async def _check_paper_to_live(self) -> None:
        """
        Verifica si el paper trading ha cumplido 2 semanas
        y si el Sharpe es suficiente para activar modo LIVE.
        """
        elapsed = self.paper_trader.get_elapsed_days()

        # Log de progreso cada 24 horas (cada ~1440 ciclos)
        if self._cycle_count % 1440 == 0 and elapsed > 0:
            summary = self.paper_trader.get_summary()
            logger.info(
                "[PAPER] Dia %.0f/14 | Capital=$%.2f | Trades=%d | "
                "Sharpe=%.4f | Return=%.2f%%",
                elapsed,
                summary["capital"],
                summary["total_trades"],
                summary["sharpe"],
                summary["return_pct"],
            )

        if elapsed < self.PAPER_DURATION_DAYS:
            return  # Aún no cumple 2 semanas

        # ── Evaluación ────────────────────────────────
        sharpe = self.paper_trader.get_sharpe()
        summary = self.paper_trader.get_summary()

        logger.info("=" * 60)
        logger.info("  PAPER TRADING COMPLETADO — %d dias", int(elapsed))
        logger.info("  Sharpe: %.4f (gate: > %.1f)", sharpe, self.PAPER_SHARPE_GATE)
        logger.info("  Trades: %d | Win Rate: %.1f%%", summary["total_trades"], summary["win_rate"])
        logger.info("  Return: %+.2f%%", summary["return_pct"])
        logger.info("=" * 60)

        if sharpe > self.PAPER_SHARPE_GATE:
            # ✅ Activar modo LIVE
            logger.info("ACTIVANDO MODO LIVE — Sharpe %.4f > %.1f", sharpe, self.PAPER_SHARPE_GATE)

            try:
                await self.execution_engine.initialize()
                self.mode = TradingMode.LIVE
                await self.telegram._send(
                    f"🟢 NEXUS MODO LIVE ACTIVADO\n\n"
                    f"Paper trading completado ({int(elapsed)} dias).\n"
                    f"Sharpe: {sharpe:.4f} (gate: {self.PAPER_SHARPE_GATE})\n"
                    f"Trades: {summary['total_trades']} | Win Rate: {summary['win_rate']:.1f}%\n"
                    f"Return: {summary['return_pct']:+.2f}%"
                )
            except Exception as exc:
                logger.error("Error activando LIVE: %s", exc)
                # Reiniciar paper trading
                self.paper_trader = PaperTrader(self._initial_capital)
                await self.telegram._send(
                    f"⚠️ Error activando LIVE: {exc}\n"
                    f"Reiniciando paper trading."
                )
        else:
            # ❌ Sharpe insuficiente — reiniciar paper
            logger.warning(
                "SHARPE INSUFICIENTE (%.4f < %.1f) — reiniciando paper trading",
                sharpe, self.PAPER_SHARPE_GATE,
            )
            await self.telegram._send(
                f"⚠️ NEXUS — Paper trading reiniciado\n\n"
                f"Sharpe: {sharpe:.4f} < {self.PAPER_SHARPE_GATE} (insuficiente)\n"
                f"Trades: {summary['total_trades']} | Win Rate: {summary['win_rate']:.1f}%\n"
                f"Reiniciando 2 semanas de paper trading."
            )
            self.paper_trader = PaperTrader(self._initial_capital)

    # ══════════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════════

    async def _get_current_capital(self) -> float:
        if self.mode == TradingMode.PAPER:
            return self.paper_trader.capital
        try:
            return await self.execution_engine.get_portfolio_value()
        except Exception:
            return self._initial_capital

    def _get_current_drawdown(self) -> float:
        if self.mode == TradingMode.PAPER:
            eq = self.paper_trader.equity_curve
            if not eq:
                return 0.0
            peak = max(eq)
            return (peak - eq[-1]) / peak if peak > 0 else 0.0
        return 0.0

    def _get_current_exposure(self) -> float:
        if self.mode == TradingMode.PAPER:
            total = self.paper_trader.capital
            if total <= 0:
                return 0.0
            pos_value = sum(
                abs(p["entry"] * p["quantity"])
                for p in self.paper_trader.positions
                if p["status"] == "OPEN"
            )
            return pos_value / total
        return 0.0

    async def _get_open_position_count(self) -> int:
        if self.mode == TradingMode.PAPER:
            return self.paper_trader.get_open_count()
        try:
            positions = await self.execution_engine.get_open_positions()
            return len(positions)
        except Exception:
            return 0

    def _get_open_positions_for_corr(self) -> List[Dict[str, Any]]:
        """Obtiene posiciones abiertas formateadas para correlation_penalty."""
        if self.mode == TradingMode.PAPER:
            return [
                {"symbol": p["symbol"]}
                for p in self.paper_trader.positions
                if p["status"] == "OPEN"
            ]
        # LIVE: Por ahora no implementado la extracción vía API
        return []

    def _estimate_atr(self, df, period: int = 14) -> float:
        """Estima el ATR (Average True Range) sin backtrader."""
        try:
            high = df["high"].values[-period:]
            low = df["low"].values[-period:]
            close = df["close"].values[-period - 1:-1]

            tr1 = high - low
            tr2 = np.abs(high - close)
            tr3 = np.abs(low - close)

            true_range = np.maximum(tr1, np.maximum(tr2, tr3))
            return float(np.mean(true_range))
        except Exception:
            # Fallback: 1% del precio
            return float(df["close"].iloc[-1]) * 0.01


# ─────────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────────

def main() -> None:
    """Punto de entrada del sistema."""
    import sys
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore

    # Parsear modo desde argumentos
    mode = TradingMode.PAPER
    if "--live" in sys.argv:
        mode = TradingMode.LIVE
        logger.warning("MODO LIVE activado por argumento --live")
    elif "--paper" in sys.argv:
        mode = TradingMode.PAPER

    system = NexusSystem(mode=mode)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Manejar señales de interrupción
    def _signal_handler(*args):
        system._running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows no soporta add_signal_handler

    try:
        loop.run_until_complete(system.initialize())
        loop.run_until_complete(system.run())
    except KeyboardInterrupt:
        logger.info("Interrupcion detectada")
    finally:
        loop.run_until_complete(system.shutdown())
        loop.close()


if __name__ == "__main__":
    main()
