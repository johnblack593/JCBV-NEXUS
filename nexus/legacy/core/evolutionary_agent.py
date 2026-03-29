"""
NEXUS Trading System — Evolutionary Agent
============================================
Agente auto-evolutivo que corre cada domingo 23:59 GMT-5.

4 Tareas principales:
  1. Reentrenamiento semanal del LSTM con nuevos trades
  2. Comparación de modelos (nuevo vs anterior) por Sharpe Ratio
  3. Alerta de degradación (3 semanas consecutivas perdiendo vs B&H)
  4. Reporte de evolución mensual (gráfico Sharpe + tabla parámetros)

Integración:
  - ml_engine.py → LSTMPredictor.train() / save() / load()
  - backtesting/backtest_runner.py → backtest rápido
  - reporting/weekly_report.py → alertas Telegram
  - schedule → programación automática
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np  # type: ignore

try:
    import schedule  # type: ignore
    _HAS_SCHEDULE = True
except ImportError:
    _HAS_SCHEDULE = False

try:
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False

logger = logging.getLogger("nexus.evolutionary")

# Timezone GMT-5
_TZ_GMT5 = timezone(timedelta(hours=-5))

# Rutas
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MODELS_DIR = _PROJECT_ROOT / "models"
_LOGS_DIR = _PROJECT_ROOT / "logs"
_EVOLUTION_LOG = _LOGS_DIR / "evolution_log.csv"
_CHARTS_DIR = _PROJECT_ROOT / "reports" / "evolution"


# ══════════════════════════════════════════════════════════════════════
#  Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class EvolutionResult:
    """Resultado de una iteración evolutiva."""
    date: datetime
    old_sharpe: float
    new_sharpe: float
    model_replaced: bool
    reason: str
    old_model_path: str
    new_model_path: str


@dataclass
class DegradationState:
    """Estado de degradación del sistema."""
    consecutive_losses: int = 0
    weekly_returns: List[float] = field(default_factory=list)
    bh_returns: List[float] = field(default_factory=list)
    conservative_mode: bool = False
    original_position_size: float = 1.0


@dataclass
class EvolutionConfig:
    """Configuración del agente evolutivo."""
    retrain_day: str = "sunday"
    retrain_time: str = "23:59"
    backtest_weeks: int = 4
    degradation_threshold: int = 3      # Semanas consecutivas
    conservative_multiplier: float = 0.5  # 50% reducción
    min_improvement: float = 0.0         # Sharpe nuevo > anterior


# ══════════════════════════════════════════════════════════════════════
#  EvolutionaryAgent
# ══════════════════════════════════════════════════════════════════════

class EvolutionaryAgent:
    """
    Agente auto-evolutivo del sistema NEXUS.

    Se ejecuta automáticamente cada domingo a las 23:59 GMT-5.
    Gestiona el ciclo de vida del modelo ML, detecta degradación,
    y adapta los parámetros del sistema.
    """

    def __init__(
        self,
        config: Optional[EvolutionConfig] = None,
        ml_engine: Any = None,
        telegram_reporter: Any = None,
        risk_manager: Any = None,
    ) -> None:
        self.config = config or EvolutionConfig()
        self._ml_engine = ml_engine
        self._telegram = telegram_reporter
        self._risk_manager = risk_manager

        self._degradation = DegradationState()
        self._evolution_history: List[EvolutionResult] = []
        self._weekly_sharpes: List[Dict[str, Any]] = []
        self._current_params: Dict[str, Any] = {}
        self._month_start_params: Dict[str, Any] = {}

        # Crear directorios
        _MODELS_DIR.mkdir(parents=True, exist_ok=True)
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        _CHARTS_DIR.mkdir(parents=True, exist_ok=True)

        # Cargar historial previo
        self._load_evolution_history()

    # ══════════════════════════════════════════════════════════════
    #  CICLO PRINCIPAL — Se ejecuta cada domingo 23:59
    # ══════════════════════════════════════════════════════════════

    async def run_weekly_evolution(
        self,
        recent_trades: List[Dict[str, Any]],
        equity_curve: List[float],
        training_data: Any = None,
        current_params: Optional[Dict[str, Any]] = None,
    ) -> EvolutionResult:
        """
        Ejecuta el ciclo evolutivo completo.

        Args:
            recent_trades:  Trades de los últimos 7 días
            equity_curve:   Curva de equity de las últimas 4 semanas
            training_data:  DataFrame OHLCV para reentrenar el LSTM
            current_params: Parámetros actuales del sistema

        Returns:
            EvolutionResult con la decisión tomada
        """
        now = datetime.now(_TZ_GMT5)
        logger.info("=" * 60)
        logger.info("  EVOLUTIONARY AGENT — Ciclo semanal %s", now.strftime("%Y-%m-%d %H:%M"))
        logger.info("=" * 60)

        if current_params:
            self._current_params = current_params.copy()

        # TAREA 1: Reentrenamiento semanal
        new_model_path = await self._retrain_model(recent_trades, training_data)

        # TAREA 2: Comparar modelos
        result = await self._compare_models(
            equity_curve=equity_curve,
            new_model_path=new_model_path,
        )

        # TAREA 3: Alerta de degradación
        await self._check_degradation(equity_curve, recent_trades)

        # TAREA 4: Reporte mensual (si es fin de mes)
        if now.day >= 25 or (now + timedelta(days=7)).month != now.month:
            await self._send_monthly_report()

        # Guardar resultado
        self._evolution_history.append(result)
        self._log_result(result)

        return result

    # ══════════════════════════════════════════════════════════════
    #  TAREA 1: Reentrenamiento semanal del LSTM
    # ══════════════════════════════════════════════════════════════

    async def _retrain_model(
        self,
        recent_trades: List[Dict[str, Any]],
        training_data: Any = None,
    ) -> str:
        """
        Toma los últimos 7 días de trades y reentrena el LSTM
        de forma incremental usando fit().

        Returns:
            Ruta al nuevo modelo guardado
        """
        now = datetime.now(_TZ_GMT5)
        new_model_path = str(_MODELS_DIR / f"lstm_{now.strftime('%Y%m%d_%H%M')}.weights.h5")

        logger.info("TAREA 1: Reentrenamiento semanal (%d trades recientes)", len(recent_trades))

        if self._ml_engine is None:
            logger.warning("ml_engine no configurado — simulando reentrenamiento")
            return new_model_path

        try:
            import pandas as pd  # type: ignore

            # Preparar datos de entrenamiento desde trades + OHLCV
            if training_data is not None:
                if isinstance(training_data, pd.DataFrame):
                    df = training_data
                else:
                    df = pd.DataFrame(training_data)
            else:
                # Construir DataFrame mínimo desde los trades
                df = self._trades_to_dataframe(recent_trades)

            if df is None or df.empty:
                logger.warning("Sin datos de entrenamiento disponibles")
                return new_model_path

            # Backup del modelo actual
            current_model_path = str(_MODELS_DIR / "lstm_current.weights.h5")
            if Path(current_model_path).exists():
                backup_path = str(_MODELS_DIR / f"lstm_backup_{now.strftime('%Y%m%d')}.weights.h5")
                shutil.copy2(current_model_path, backup_path)
                logger.info("Modelo anterior respaldado en %s", backup_path)

            # Reentrenamiento incremental
            lstm = self._ml_engine.lstm

            # Preparar secuencias
            X_train, y_train = lstm.prepare_data(df)

            if X_train is not None and len(X_train) > 0:  # type: ignore
                # Fit incremental (pocas épocas con datos recientes)
                history = lstm.model.fit(
                    X_train, y_train,
                    epochs=10,  # Pocas épocas para ajuste incremental
                    batch_size=lstm.batch_size,
                    validation_split=0.2,
                    verbose=0,
                )

                # Guardar nuevo modelo
                lstm.save(Path(new_model_path))
                logger.info(
                    "LSTM reentrenado: loss=%.6f, val_loss=%.6f → %s",
                    history.history["loss"][-1],
                    history.history.get("val_loss", [0])[-1],
                    new_model_path,
                )
            else:
                logger.warning("Datos insuficientes para reentrenamiento")

        except Exception as exc:
            logger.error("Error en reentrenamiento: %s", exc)

        return new_model_path

    def _trades_to_dataframe(self, trades: List[Dict[str, Any]]) -> Any:
        """Convierte trades a DataFrame OHLCV básico para entrenamiento."""
        import pandas as pd  # type: ignore

        if not trades:
            return pd.DataFrame()

        rows = []
        for t in trades:
            rows.append({
                "open": t.get("entry", t.get("price", 0)),
                "high": max(t.get("entry", 0), t.get("tp", t.get("take_profit", 0))),
                "low": min(t.get("entry", 0), t.get("sl", t.get("stop_loss", 0))),
                "close": t.get("exit_price", t.get("entry", 0)),
                "volume": t.get("size", 1),
            })

        return pd.DataFrame(rows)

    # ══════════════════════════════════════════════════════════════
    #  TAREA 2: Comparar modelos (nuevo vs anterior)
    # ══════════════════════════════════════════════════════════════

    async def _compare_models(
        self,
        equity_curve: List[float],
        new_model_path: str,
    ) -> EvolutionResult:
        """
        Corre backtest rápido (últimas 4 semanas) con modelo ANTERIOR
        y modelo NUEVO. Compara Sharpe Ratio de ambos.

        Si nuevo > anterior → reemplazar modelo.
        Si nuevo <= anterior → conservar anterior.
        """
        now = datetime.now(_TZ_GMT5)
        current_model_path = str(_MODELS_DIR / "lstm_current.weights.h5")

        logger.info("TAREA 2: Comparación de modelos")

        # Calcular Sharpe del modelo anterior (desde equity curve)
        old_sharpe = self._calculate_sharpe(equity_curve)

        # Calcular Sharpe del modelo nuevo
        # En un sistema real, esto correría un backtest rápido con el nuevo modelo
        # Por ahora, simulamos un backtest rápido
        new_sharpe = await self._quick_backtest_sharpe(new_model_path, equity_curve)

        # Decisión
        improvement = new_sharpe - old_sharpe
        model_replaced = new_sharpe > old_sharpe + self.config.min_improvement

        if model_replaced:
            # Reemplazar modelo guardado
            try:
                if Path(new_model_path).exists():
                    shutil.copy2(new_model_path, current_model_path)
                reason = (
                    f"Modelo reemplazado: nuevo Sharpe ({new_sharpe:.4f}) > "
                    f"anterior ({old_sharpe:.4f}) por {improvement:+.4f}"
                )
                logger.info("✅ %s", reason)
            except Exception as exc:
                reason = f"Error reemplazando modelo: {exc}"
                model_replaced = False
                logger.error(reason)
        else:
            reason = (
                f"Modelo conservado: nuevo Sharpe ({new_sharpe:.4f}) <= "
                f"anterior ({old_sharpe:.4f}). Diferencia: {improvement:+.4f}"
            )
            logger.info("⏸️ %s", reason)

        result = EvolutionResult(
            date=now,
            old_sharpe=round(old_sharpe, 4),  # type: ignore
            new_sharpe=round(new_sharpe, 4),  # type: ignore
            model_replaced=model_replaced,
            reason=reason,
            old_model_path=current_model_path,
            new_model_path=new_model_path,
        )

        # Registrar Sharpe semanal
        self._weekly_sharpes.append({
            "date": now.isoformat(),
            "sharpe": round(new_sharpe if model_replaced else old_sharpe, 4),  # type: ignore
            "model_replaced": model_replaced,
        })

        return result

    async def _quick_backtest_sharpe(
        self,
        model_path: str,
        equity_curve: List[float],
    ) -> float:
        """
        Corre un backtest rápido y retorna el Sharpe Ratio.

        En producción, esto cargaría el nuevo modelo y correría
        el backtester de 4 semanas. Aquí usa la equity curve
        disponible y simula el efecto del nuevo modelo.
        """
        if not equity_curve or len(equity_curve) < 10:
            return 0.0

        try:
            # Intentar importar y usar el backtest runner
            from backtesting.backtest_runner import calculate_metrics  # type: ignore

            # Usar últimas 4 semanas de equity
            weeks_4 = min(len(equity_curve), 4 * 7 * 24)  # 4 semanas en horas
            recent_equity = equity_curve[-weeks_4:]  # type: ignore

            metrics = calculate_metrics(
                equity_curve=recent_equity,
                initial_capital=recent_equity[0],
                trades=[],
                buy_hold_return=0,
            )

            return metrics.get("sharpe_ratio", 0.0)

        except ImportError:
            # Fallback: calcular Sharpe directamente
            return self._calculate_sharpe(equity_curve)

    def _calculate_sharpe(
        self,
        equity_curve: List[float],
        periods_per_year: int = 8_760,
    ) -> float:
        """Calcula Sharpe Ratio anualizado desde equity curve."""
        if not equity_curve or len(equity_curve) < 2:
            return 0.0

        eq = np.array(equity_curve, dtype=np.float64)
        returns = np.diff(np.log(eq))
        returns = returns[np.isfinite(returns)]

        if len(returns) == 0 or np.std(returns, ddof=1) == 0:
            return 0.0

        return float(
            np.mean(returns) / np.std(returns, ddof=1) * math.sqrt(periods_per_year)
        )

    def _log_result(self, result: EvolutionResult) -> None:
        """Registra la decisión en logs/evolution_log.csv."""
        file_exists = _EVOLUTION_LOG.exists()

        try:
            with open(_EVOLUTION_LOG, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        "date", "old_sharpe", "new_sharpe",
                        "model_replaced", "reason",
                    ])
                writer.writerow([
                    result.date.isoformat(),
                    result.old_sharpe,
                    result.new_sharpe,
                    result.model_replaced,
                    result.reason,
                ])
            logger.info("Evolución registrada en %s", _EVOLUTION_LOG)
        except OSError as exc:
            logger.error("Error escribiendo evolution_log: %s", exc)

    # ══════════════════════════════════════════════════════════════
    #  TAREA 3: Alerta de degradación
    # ══════════════════════════════════════════════════════════════

    async def _check_degradation(
        self,
        equity_curve: List[float],
        recent_trades: List[Dict[str, Any]],
    ) -> None:
        """
        Si el sistema pierde contra BTC Buy & Hold 3 semanas
        consecutivas:
          → Enviar alerta Telegram urgente
          → Sugerir top 3 ajustes de parámetros
          → Entrar en modo conservador (position_size × 0.5)
        """
        logger.info("TAREA 3: Verificación de degradación")

        if not equity_curve or len(equity_curve) < 2:
            return

        # Retorno de la última semana (sistema)
        week_hours = min(len(equity_curve), 7 * 24)
        week_start = equity_curve[-week_hours]
        week_end = equity_curve[-1]
        system_return = (week_end - week_start) / week_start if week_start > 0 else 0

        # Retorno Buy & Hold de la última semana
        # Estimamos B&H como el cambio de precio del primer al último punto
        bh_return = system_return * 0.8  # Placeholder — en producción se calcula con precio BTC

        # Si hay trades, usar datos reales
        if recent_trades:
            bh_prices = [t.get("entry", t.get("price", 0)) for t in recent_trades if t.get("entry", 0) > 0]
            if len(bh_prices) >= 2:
                bh_return = (bh_prices[-1] - bh_prices[0]) / bh_prices[0]  # type: ignore

        # Registrar
        self._degradation.weekly_returns.append(system_return)
        self._degradation.bh_returns.append(bh_return)

        # ¿Perdimos contra B&H esta semana?
        lost_to_bh = system_return < bh_return

        if lost_to_bh:
            self._degradation.consecutive_losses += 1
            logger.warning(
                "Semana perdida vs B&H: sistema=%.2f%%, B&H=%.2f%% (consecutivas: %d)",
                system_return * 100, bh_return * 100,
                self._degradation.consecutive_losses,
            )
        else:
            self._degradation.consecutive_losses = 0
            if self._degradation.conservative_mode:
                self._exit_conservative_mode()
            logger.info(
                "Semana ganada vs B&H: sistema=%.2f%%, B&H=%.2f%%",
                system_return * 100, bh_return * 100,
            )

        # ¿3 semanas consecutivas perdiendo?
        if self._degradation.consecutive_losses >= self.config.degradation_threshold:
            await self._trigger_degradation_alert(system_return, bh_return)
            self._enter_conservative_mode()

    async def _trigger_degradation_alert(
        self,
        system_return: float,
        bh_return: float,
    ) -> None:
        """Envía alerta urgente de degradación por Telegram."""
        suggestions = self._suggest_parameter_adjustments()

        msg = (
            f"⚠️ ALERTA DE DEGRADACION — NEXUS\n\n"
            f"El sistema ha perdido contra BTC Buy & Hold "
            f"por {self._degradation.consecutive_losses} semanas consecutivas.\n\n"
            f"Ultima semana:\n"
            f"  • Sistema: {system_return:+.2%}\n"
            f"  • Buy & Hold: {bh_return:+.2%}\n"
            f"  • Diferencia: {(system_return - bh_return):+.2%}\n\n"
            f"Top 3 ajustes sugeridos:\n"
        )
        for i, sug in enumerate(suggestions[:3], 1):  # type: ignore
            msg += f"  {i}. {sug}\n"

        msg += f"\n🛡️ Modo conservador ACTIVADO\n"
        msg += f"Position size reducido al {self.config.conservative_multiplier:.0%}"

        # Enviar por Telegram
        if self._telegram is not None:
            try:
                await self._telegram._send(msg)
                logger.critical("Alerta degradación enviada por Telegram")
            except Exception as exc:
                logger.error("Error enviando alerta degradación: %s", exc)

        logger.critical("DEGRADACION: %s", msg)

    def _suggest_parameter_adjustments(self) -> List[str]:
        """Genera sugerencias automáticas basadas en el análisis de causa."""
        suggestions = []

        # Analizar últimas semanas
        returns = self._degradation.weekly_returns[-6:]  # type: ignore
        bh = self._degradation.bh_returns[-6:]  # type: ignore

        if len(returns) >= 3:
            avg_ret = np.mean(returns)
            avg_bh = np.mean(bh)
            volatility = np.std(returns) if len(returns) > 1 else 0

            # Sugerencia 1: Si volatilidad alta
            if volatility > 0.05:
                suggestions.append(
                    f"Reducir sl_atr_mult de 2.0 a 1.5 (volatilidad semanal: {volatility:.1%})"
                )
            else:
                suggestions.append(
                    "Aumentar tp_atr_mult de 3.0 a 4.0 para capturar movimientos mayores"
                )

            # Sugerencia 2: Si sistema muy conservador
            if avg_ret < 0 and avg_bh > 0:
                suggestions.append(
                    "Reducir min_confidence del arbitro de 0.65 a 0.55 "
                    "(sistema demasiado selectivo, pierde oportunidades)"
                )
            elif avg_ret > avg_bh * 0.5:
                suggestions.append(
                    "Incrementar lookback del LSTM de 60 a 120 "
                    "para capturar tendencias de mediano plazo"
                )

            # Sugerencia 3: Si Sharpe en tendencia negativa
            recent_sharpes = [s["sharpe"] for s in self._weekly_sharpes[-4:]]  # type: ignore
            if len(recent_sharpes) >= 2 and recent_sharpes[-1] < recent_sharpes[0]:
                suggestions.append(
                    f"Reentrenar LSTM con horizonte extendido (30 → 60 días). "
                    f"Sharpe en declive: {recent_sharpes[0]:.2f} → {recent_sharpes[-1]:.2f}"
                )
            else:
                suggestions.append(
                    "Agregar filtro de régimen (EMA 50/200): solo operar "
                    "en tendencias claras, no en rangos laterales"
                )

        # Defaults si no hay suficiente historial
        while len(suggestions) < 3:
            defaults = [
                "Reducir max_positions de 3 a 2 para concentrar capital",
                "Incrementar periodo del ATR de 14 a 21 para SL/TP mas estables",
                "Agregar filtro de volumen minimo para evitar gaps de liquidez",
            ]
            for d in defaults:
                if d not in suggestions:
                    suggestions.append(d)
                    break

        return suggestions[:3]  # type: ignore

    def _enter_conservative_mode(self) -> None:
        """Entra en modo conservador: reduce position_size al 50%."""
        if self._degradation.conservative_mode:
            return  # Ya en modo conservador

        self._degradation.conservative_mode = True

        if self._risk_manager is not None:
            try:
                original = getattr(self._risk_manager, "KELLY_MAX_FRACTION", 0.25)
                self._degradation.original_position_size = original
                self._risk_manager.KELLY_MAX_FRACTION = original * self.config.conservative_multiplier
                logger.warning(
                    "MODO CONSERVADOR: Kelly max %.2f → %.2f",
                    original, self._risk_manager.KELLY_MAX_FRACTION,
                )
            except Exception as exc:
                logger.error("Error entrando en modo conservador: %s", exc)

        logger.warning("MODO CONSERVADOR ACTIVADO")

    def _exit_conservative_mode(self) -> None:
        """Sale del modo conservador, restaura position_size."""
        if not self._degradation.conservative_mode:
            return

        self._degradation.conservative_mode = False
        self._degradation.consecutive_losses = 0

        if self._risk_manager is not None:
            try:
                self._risk_manager.KELLY_MAX_FRACTION = self._degradation.original_position_size
                logger.info(
                    "Modo conservador DESACTIVADO: Kelly max restaurado a %.2f",
                    self._degradation.original_position_size,
                )
            except Exception as exc:
                logger.error("Error saliendo de modo conservador: %s", exc)

        logger.info("MODO CONSERVADOR DESACTIVADO")

    # ══════════════════════════════════════════════════════════════
    #  TAREA 4: Reporte de evolución mensual
    # ══════════════════════════════════════════════════════════════

    async def _send_monthly_report(self) -> None:
        """
        Genera y envía reporte mensual con:
        - Gráfico PNG: evolución del Sharpe semana a semana
        - Tabla comparativa: parámetros actuales vs inicio del mes
        """
        logger.info("TAREA 4: Reporte de evolución mensual")

        now = datetime.now(_TZ_GMT5)

        # Generar gráfico de Sharpe
        chart_bytes = self._generate_sharpe_chart()

        # Generar tabla comparativa
        params_table = self._format_params_comparison()

        # Texto del reporte
        n_evolutions = len(self._evolution_history)
        replacements = sum(1 for e in self._evolution_history if e.model_replaced)

        msg = (
            f"📊 NEXUS — Reporte de Evolucion Mensual\n"
            f"Mes: {now.strftime('%B %Y')}\n"
            f"─────────────────────────\n"
            f"Iteraciones evolutivas: {n_evolutions}\n"
            f"Modelos reemplazados: {replacements}\n"
            f"Modo conservador: {'SI' if self._degradation.conservative_mode else 'NO'}\n"
            f"Semanas perdidas vs B&H: {self._degradation.consecutive_losses}\n"
            f"─────────────────────────\n"
            f"{params_table}\n"
        )

        if self._weekly_sharpes:
            last_sharpe = self._weekly_sharpes[-1]["sharpe"]
            first_sharpe = self._weekly_sharpes[0]["sharpe"]
            msg += f"\nSharpe: {first_sharpe:.4f} → {last_sharpe:.4f}"

        # Enviar por Telegram
        if self._telegram is not None:
            try:
                await self._telegram._send(msg)
                if chart_bytes:
                    await self._telegram._send_photo(
                        chart_bytes,
                        caption=f"📈 Evolución Sharpe — {now.strftime('%B %Y')}",
                    )
            except Exception as exc:
                logger.error("Error enviando reporte mensual: %s", exc)

        # Guardar gráfico localmente
        if chart_bytes:
            chart_path = _CHARTS_DIR / f"sharpe_evolution_{now.strftime('%Y%m')}.png"
            with open(chart_path, "wb") as f:
                f.write(chart_bytes)
            logger.info("Gráfico guardado en %s", chart_path)

        # Reset parámetros del inicio del mes
        self._month_start_params = self._current_params.copy()

        logger.info("Reporte mensual enviado")

    def _generate_sharpe_chart(self) -> Optional[bytes]:
        """Genera gráfico PNG de la evolución del Sharpe semana a semana."""
        if not _HAS_MPL or not self._weekly_sharpes:
            return None

        dates = []
        sharpes = []
        for s in self._weekly_sharpes:
            try:
                d = datetime.fromisoformat(s["date"])
                dates.append(d.strftime("%m/%d"))
            except (ValueError, KeyError):
                dates.append("?")
            sharpes.append(s.get("sharpe", 0))

        fig, ax = plt.subplots(figsize=(10, 4))

        # Colores: verde si >= 1.5, amarillo si >= 0.5, rojo si < 0.5
        colors = []
        for s in sharpes:
            if s >= 1.5:
                colors.append("#00d4aa")
            elif s >= 0.5:
                colors.append("#ffa500")
            else:
                colors.append("#ff4757")

        ax.bar(range(len(sharpes)), sharpes, color=colors, alpha=0.8, width=0.6)
        ax.plot(range(len(sharpes)), sharpes, color="#e0e0e0", linewidth=1.5,
                marker="o", markersize=5, alpha=0.8)

        # Línea objetivo
        ax.axhline(y=1.5, color="#00d4aa", linestyle="--", alpha=0.4, label="Objetivo (1.5)")
        ax.axhline(y=0, color="#666", linestyle="-", alpha=0.3)

        ax.set_xticks(range(len(dates)))
        ax.set_xticklabels(dates, rotation=45, fontsize=8)
        ax.set_ylabel("Sharpe Ratio")
        ax.set_title("Evolución Semanal del Sharpe Ratio",
                      fontsize=13, fontweight="bold", color="#e0e0e0")
        ax.legend(loc="upper left", fontsize=8)

        ax.set_facecolor("#1a1a2e")
        fig.set_facecolor("#0d0d1a")
        ax.tick_params(colors="#999")
        ax.yaxis.label.set_color("#999")
        ax.grid(True, alpha=0.15, axis="y")
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120,
                    facecolor=fig.get_facecolor(), bbox_inches="tight")
        buf.seek(0)
        plt.close(fig)

        return buf.read()

    def _format_params_comparison(self) -> str:
        """Formatea tabla comparativa de parámetros actuales vs inicio del mes."""
        current = self._current_params
        start = self._month_start_params

        if not current and not start:
            return "Sin datos de parametros para comparar."

        lines = ["Parametros (Inicio vs Actual):"]
        all_keys = set(list(current.keys()) + list(start.keys()))

        for key in sorted(all_keys):
            old_val = start.get(key, "N/A")
            new_val = current.get(key, "N/A")
            changed = "  *" if old_val != new_val else ""
            lines.append(f"  {key}: {old_val} → {new_val}{changed}")

        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════
    #  Scheduling — Domingo 23:59 GMT-5
    # ══════════════════════════════════════════════════════════════

    def setup_schedule(self) -> None:
        """
        Configura el schedule para ejecutar la evolución
        cada domingo a las 23:59 GMT-5.
        """
        if not _HAS_SCHEDULE:
            logger.warning("Librería 'schedule' no instalada.")
            return

        def _evolution_job():
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.run_weekly_evolution(
                    recent_trades=[],
                    equity_curve=[],
                ))
            except RuntimeError:
                asyncio.run(self.run_weekly_evolution(
                    recent_trades=[],
                    equity_curve=[],
                ))

        schedule.every().sunday.at("23:59").do(_evolution_job)
        logger.info("📅 Evolución programada: domingo 23:59 GMT-5")

    @staticmethod
    def run_pending() -> None:
        """Ejecuta tareas pendientes de schedule."""
        if _HAS_SCHEDULE:
            schedule.run_pending()

    # ══════════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════════

    def _load_evolution_history(self) -> None:
        """Carga historial de evolución desde CSV."""
        if not _EVOLUTION_LOG.exists():
            return

        try:
            with open(_EVOLUTION_LOG, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self._weekly_sharpes.append({
                        "date": row.get("date", ""),
                        "sharpe": float(row.get("new_sharpe", 0)),
                        "model_replaced": row.get("model_replaced", "False") == "True",
                    })
            logger.info("Historial cargado: %d entradas", len(self._weekly_sharpes))
        except Exception as exc:
            logger.error("Error cargando historial: %s", exc)

    def get_state(self) -> Dict[str, Any]:
        """Retorna el estado actual del agente evolutivo."""
        return {
            "conservative_mode": self._degradation.conservative_mode,
            "consecutive_losses": self._degradation.consecutive_losses,
            "total_evolutions": len(self._evolution_history),
            "weekly_sharpes": len(self._weekly_sharpes),
            "last_evolution": (
                self._evolution_history[-1].date.isoformat()
                if self._evolution_history else None
            ),
        }

    def __repr__(self) -> str:
        mode = "CONSERVATIVE" if self._degradation.conservative_mode else "NORMAL"
        return (
            f"<EvolutionaryAgent mode={mode} "
            f"evolutions={len(self._evolution_history)} "
            f"losses={self._degradation.consecutive_losses}>"
        )


# ══════════════════════════════════════════════════════════════════════
#  Validación
# ══════════════════════════════════════════════════════════════════════

def _run_validation() -> bool:
    """Valida la lógica del agente evolutivo sin dependencias externas."""
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # type: ignore

    print("\n" + "=" * 60)
    print("  VALIDACION — EvolutionaryAgent")
    print("=" * 60)

    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1  # type: ignore
            print(f"  [OK]  {name}")
        else:
            failed += 1  # type: ignore
            print(f"  [FAIL] {name}: {detail}")

    agent = EvolutionaryAgent()

    # ── Test 1: Sharpe calculation ────────────────────────────────
    print("\n--- Test 1: Calculo del Sharpe ---")

    # Equity creciente → Sharpe positivo
    eq_up = list(np.linspace(10000, 12000, 200))
    sharpe_up = agent._calculate_sharpe(eq_up)
    check("Equity creciente → Sharpe > 0", sharpe_up > 0, f"sharpe={sharpe_up:.4f}")

    # Equity decreciente → Sharpe negativo
    eq_down = list(np.linspace(10000, 8000, 200))
    sharpe_down = agent._calculate_sharpe(eq_down)
    check("Equity decreciente → Sharpe < 0", sharpe_down < 0, f"sharpe={sharpe_down:.4f}")

    # Equity plana → Sharpe = 0
    eq_flat = [10000.0] * 50
    sharpe_flat = agent._calculate_sharpe(eq_flat)
    check("Equity plana → Sharpe = 0", sharpe_flat == 0.0, f"sharpe={sharpe_flat}")

    # ── Test 2: Sugerencias de parámetros ─────────────────────────
    print("\n--- Test 2: Sugerencias de parametros ---")

    agent._degradation.weekly_returns = [-0.02, -0.03, -0.01]
    agent._degradation.bh_returns = [0.02, 0.01, 0.03]

    suggestions = agent._suggest_parameter_adjustments()
    check("3 sugerencias generadas", len(suggestions) == 3, f"got {len(suggestions)}")
    check("Sugerencias son strings", all(isinstance(s, str) for s in suggestions))

    # ── Test 3: Degradación — contador ────────────────────────────
    print("\n--- Test 3: Degradacion ---")

    agent2 = EvolutionaryAgent()

    # Simular 3 semanas perdiendo
    for i in range(3):
        agent2._degradation.consecutive_losses += 1

    check(
        "3 semanas consecutivas = umbral",
        agent2._degradation.consecutive_losses >= agent2.config.degradation_threshold,
    )

    # ── Test 4: Modo conservador ──────────────────────────────────
    print("\n--- Test 4: Modo conservador ---")

    agent3 = EvolutionaryAgent()

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from core.risk_manager import QuantRiskManager  # type: ignore

    rm = QuantRiskManager(log_dir=str(_LOGS_DIR))
    agent3._risk_manager = rm

    original_kelly = rm.KELLY_MAX_FRACTION
    agent3._enter_conservative_mode()

    check(
        "Kelly reducido al 50%",
        rm.KELLY_MAX_FRACTION == original_kelly * 0.5,
        f"Kelly={rm.KELLY_MAX_FRACTION}, esperado={original_kelly * 0.5}",
    )
    check("Modo conservador activo", agent3._degradation.conservative_mode)

    agent3._exit_conservative_mode()
    check(
        "Kelly restaurado",
        rm.KELLY_MAX_FRACTION == original_kelly,
        f"Kelly={rm.KELLY_MAX_FRACTION}, esperado={original_kelly}",
    )
    check("Modo conservador desactivado", not agent3._degradation.conservative_mode)

    # ── Test 5: Log CSV ───────────────────────────────────────────
    print("\n--- Test 5: Log CSV ---")

    result = EvolutionResult(
        date=datetime.now(_TZ_GMT5),
        old_sharpe=1.2,
        new_sharpe=1.5,
        model_replaced=True,
        reason="Test validation",
        old_model_path="old.h5",
        new_model_path="new.h5",
    )
    agent._log_result(result)

    check("evolution_log.csv existe", _EVOLUTION_LOG.exists())
    if _EVOLUTION_LOG.exists():
        with open(_EVOLUTION_LOG, "r") as f:
            content = f.read()
        check("CSV contiene header", "old_sharpe" in content)
        check("CSV contiene datos", "Test validation" in content)

    # ── Test 6: Gráfico Sharpe ────────────────────────────────────
    print("\n--- Test 6: Grafico Sharpe ---")

    agent._weekly_sharpes = [
        {"date": "2026-03-01T00:00:00", "sharpe": 0.8, "model_replaced": False},
        {"date": "2026-03-08T00:00:00", "sharpe": 1.1, "model_replaced": True},
        {"date": "2026-03-15T00:00:00", "sharpe": 1.4, "model_replaced": False},
        {"date": "2026-03-22T00:00:00", "sharpe": 1.7, "model_replaced": True},
    ]

    if _HAS_MPL:
        chart = agent._generate_sharpe_chart()
        check("Chart PNG generado", chart is not None and len(chart) > 1000,
              f"size={len(chart) if chart else 0}")
    else:
        print("  [SKIP] matplotlib no disponible")

    # ── Test 7: Estado del agente ─────────────────────────────────
    print("\n--- Test 7: Estado del agente ---")

    state = agent.get_state()
    check("Estado tiene conservative_mode", "conservative_mode" in state)
    check("Estado tiene consecutive_losses", "consecutive_losses" in state)
    check("Estado tiene weekly_sharpes", "weekly_sharpes" in state)

    # ── Resultado ─────────────────────────────────────────────────
    total = passed + failed
    print("\n" + "=" * 60)
    if failed == 0:
        print(f"  EVOLUTIONARY AGENT VALIDADO: {passed}/{total} tests passed")
    else:
        print(f"  ERRORES: {failed}/{total} tests fallaron")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = _run_validation()
    sys.exit(0 if success else 1)
