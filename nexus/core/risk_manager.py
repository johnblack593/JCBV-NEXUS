"""
NEXUS Trading System — Quant Risk Manager
============================================
Gestión cuantitativa de riesgo con verificación matemática inline.

5 métodos principales:
  1. kelly_criterion()          — Dimensionamiento óptimo (Kelly fraccionado)
  2. monte_carlo_simulation()   — Trayectorias de capital aleatorias
  3. value_at_risk()            — VaR paramétrico (μ - z·σ)
  4. circuit_breaker_check()    — Corte de emergencia por drawdown
  5. correlation_check()        — Bloqueo por correlación excesiva

Cada método incluye un bloque de validación assert al final del módulo.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np  # type: ignore

try:
    from scipy import stats as scipy_stats  # type: ignore
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

logger = logging.getLogger("nexus.risk_manager")


# ══════════════════════════════════════════════════════════════════════
#  Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class PositionSize:
    """Resultado del cálculo de tamaño de posición."""
    optimal_size: float
    kelly_size: float
    adjusted_size: float
    max_allowed: float
    rationale: str


@dataclass
class RiskMetrics:
    """Métricas de riesgo del portafolio."""
    var_95: float
    var_99: float
    cvar_95: float
    max_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float
    current_exposure: float


@dataclass
class MonteCarloResult:
    """Resultado de la simulación Monte Carlo."""
    p5: float               # Percentil 5
    p50: float              # Mediana
    p95: float              # Percentil 95
    max_dd: float           # Drawdown máximo promedio
    simulations: int
    paths: Optional[np.ndarray] = None


# ══════════════════════════════════════════════════════════════════════
#  QuantRiskManager
# ══════════════════════════════════════════════════════════════════════

class QuantRiskManager:
    """
    Gestor cuantitativo de riesgo principal.

    Integra Kelly Criterion, Monte Carlo, VaR paramétrico,
    circuit breaker y control de correlación.
    """

    # Constantes
    KELLY_MAX_FRACTION = 0.25           # Cap del Kelly fraccionado
    KELLY_MIN_FLOOR = 0.01   # Si f* < 1%, no vale la pena operar
    Z_SCORES = {0.95: 1.645, 0.99: 2.326}
    CIRCUIT_BREAKER_COOLDOWN = 86_400   # 24 horas en segundos
    CB_STATE_FILE = "cb_state.json"     # Persistencia del circuit breaker

    def __init__(
        self,
        log_dir: str = "logs",
        execution_engine: Any = None,
        redis_client: Any = None,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        self._execution_engine = execution_engine
        self._redis = redis_client  # v4.0: Redis for atomic CB state
        self._portfolio_value: float = 0.0
        self._open_positions: List[Dict[str, Any]] = []
        self._circuit_breaker_active: bool = False
        self._circuit_breaker_until: float = 0.0  # timestamp

        # Restaurar estado del CB: Redis (cluster-wide) → disco (local fallback)
        self._load_cb_state()

    # ══════════════════════════════════════════════════════════════════
    #  MÉTODO 1: Kelly Criterion
    # ══════════════════════════════════════════════════════════════════

    def kelly_criterion(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
    ) -> float:
        """
        Calcula la fracción óptima de Kelly.

        Fórmula: f* = (b·p − q) / b
        donde:
            b = avg_win / avg_loss  (ratio ganancia/pérdida)
            p = win_rate            (probabilidad de ganar)
            q = 1 − win_rate        (probabilidad de perder)

        Reglas:
            - Si f* > 0.25 → retornar 0.25 (Kelly fraccionado)
            - Si f* < 0    → retornar 0.0  (no operar)

        Args:
            win_rate: Probabilidad de trade ganador (0-1)
            avg_win:  Ganancia promedio por trade ganador (valor absoluto)
            avg_loss: Pérdida promedio por trade perdedor (valor absoluto)

        Returns:
            Fracción óptima del capital a apostar (0.0 – 0.25)
        """
        if avg_loss <= 0:
            raise ValueError(f"avg_loss debe ser > 0, recibido: {avg_loss}")
        if not (0 <= win_rate <= 1):
            raise ValueError(f"win_rate debe estar entre 0 y 1, recibido: {win_rate}")

        b = avg_win / avg_loss          # Win/Loss ratio
        p = win_rate                    # Prob. de ganar
        q = 1.0 - win_rate              # Prob. de perder

        f_star = (b * p - q) / b        # Kelly puro

        # Regla: si negativo → no operar
        if f_star < 0:
            logger.info("Kelly f*=%.4f < 0 → no operar", f_star)
            return 0.0

        # Regla: cap a 0.25 (Kelly fraccionado)
        # Piso mínimo operacional: Kelly < 1% no justifica abrir posición
        if f_star < self.KELLY_MIN_FLOOR:
            logger.info("Kelly f*=%.4f < MIN_FLOOR %.2f → no operar", f_star, self.KELLY_MIN_FLOOR)
            return 0.0

        result = min(f_star, self.KELLY_MAX_FRACTION)

        logger.info(
            "Kelly: p=%.2f, b=%.2f → f*=%.4f → cappado=%.4f",
            p, b, f_star, result,
        )
        return round(result, 6)  # type: ignore

    # ══════════════════════════════════════════════════════════════════
    #  MÉTODO 2: Monte Carlo Simulation
    # ══════════════════════════════════════════════════════════════════

    def monte_carlo_simulation(
        self,
        returns_list: List[float],
        n: int = 10_000,
        initial_capital: float = 10_000.0,
        horizon: Optional[int] = None,
    ) -> Dict[str, float]:
        """
        Genera n trayectorias de capital aleatorias usando remuestreo
        con reemplazo de retornos históricos.

        Args:
            returns_list: Lista de retornos porcentuales (e.g. 0.01 = +1%)
            n:            Número de simulaciones
            initial_capital: Capital inicial
            horizon:      Número de pasos por trayectoria (default = len(returns_list))

        Returns:
            {"p5": float, "p50": float, "p95": float, "max_dd": float}
        """
        if not returns_list:
            raise ValueError("returns_list no puede estar vacío")

        returns_arr = np.array(returns_list, dtype=np.float64)
        h = horizon or len(returns_arr)

        # Remuestreo con reemplazo: (n, h) samples
        rng = np.random.default_rng(seed=42)
        sampled = rng.choice(returns_arr, size=(n, h), replace=True)

        # Trayectorias de capital acumuladas
        # capital[i, t] = capital_0 * prod(1 + r[j] para j=0..t)
        cumulative = initial_capital * np.cumprod(1.0 + sampled, axis=1)

        # Capital final de cada trayectoria
        final_capitals = cumulative[:, -1]  # type: ignore

        # Percentiles del capital final
        p5 = float(np.percentile(final_capitals, 5))
        p50 = float(np.percentile(final_capitals, 50))
        p95 = float(np.percentile(final_capitals, 95))

        # Drawdown máximo promedio
        # Para cada trayectoria, calcular max drawdown
        running_max = np.maximum.accumulate(cumulative, axis=1)
        drawdowns = (running_max - cumulative) / running_max
        max_dds = np.max(drawdowns, axis=1)
        avg_max_dd = float(np.mean(max_dds))

        result = {
            "p5": round(p5, 2),  # type: ignore
            "p50": round(p50, 2),  # type: ignore
            "p95": round(p95, 2),  # type: ignore
            "max_dd": round(avg_max_dd, 6),  # type: ignore
        }

        logger.info(
            "Monte Carlo (%d sims, h=%d): p5=%.2f, p50=%.2f, p95=%.2f, max_dd=%.4f",
            n, h, p5, p50, p95, avg_max_dd,
        )
        return result

    # ══════════════════════════════════════════════════════════════════
    #  MÉTODO 3: Value at Risk (VaR HISTÓRICO — No asume normalidad)
    # ══════════════════════════════════════════════════════════════════

    def value_at_risk(
        self,
        returns_list: List[float],
        confidence: float = 0.95,
    ) -> float:
        """
        VaR HISTÓRICO (basado en percentiles reales).

        A diferencia del VaR paramétrico (que asume distribución normal),
        este método usa el percentil real de la cola izquierda de los
        retornos, capturando correctamente fat tails y distribuciones
        asimétricas típicas de mercados financieros.

        Fórmula: VaR = -percentile(returns, alpha)
        donde alpha = 1 - confidence (e.g. 5% para VaR95)

        También calcula CVaR (Expected Shortfall) como el promedio
        de todos los retornos peores que el VaR.

        Args:
            returns_list: Lista de retornos porcentuales
            confidence:   Nivel de confianza (0.95 o 0.99)

        Returns:
            VaR como porcentaje del capital en riesgo (valor positivo = pérdida)
        """
        if not returns_list:
            raise ValueError("returns_list no puede estar vacío")
        if len(returns_list) < 30:
            logger.warning("VaR histórico: solo %d muestras (mínimo recomendado: 30)", len(returns_list))

        returns_arr = np.array(returns_list, dtype=np.float64)

        # Detectar retornos en formato incorrecto (>10 = probablemente porcentaje entero)
        max_abs = float(np.max(np.abs(returns_arr)))
        if max_abs > 10.0:
            logger.warning(
                "VaR: retornos parecen estar en formato porcentual entero "
                "(max_abs=%.2f). Se esperan valores como 0.01 para 1%%. "
                "Normalizar dividiendo por 100.",
                max_abs
            )
            returns_arr = returns_arr / 100.0

        alpha = (1.0 - confidence) * 100  # e.g. 5.0 para 95%

        # Percentil real de la cola izquierda
        var_threshold = float(np.percentile(returns_arr, alpha))

        # CVaR (Expected Shortfall): promedio de pérdidas peores que VaR
        tail_losses = returns_arr[returns_arr <= var_threshold]
        cvar = float(np.mean(tail_losses)) if len(tail_losses) > 0 else var_threshold

        # Retornar como valor positivo (porcentaje de capital en riesgo)
        var_pct = abs(var_threshold) if var_threshold < 0 else 0.0

        logger.info(
            "VaR_Hist(%.0f%%): threshold=%.6f, CVaR=%.6f, n=%d → VaR=%.4f%%",
            confidence * 100, var_threshold, cvar, len(returns_arr), var_pct * 100,
        )
        return round(var_pct, 6)  # type: ignore

    # ══════════════════════════════════════════════════════════════════
    #  MÉTODO 4: Circuit Breaker
    # ══════════════════════════════════════════════════════════════════

    def circuit_breaker_check(
        self,
        current_drawdown: float,
        max_dd: float = 0.15,
    ) -> bool:
        """
        Verifica si el drawdown actual excede el límite y activa
        el circuit breaker si es necesario.

        Si current_drawdown >= max_dd:
            1. Llama execution_engine.close_all_positions()
            2. Escribe en logs/circuit_breaker.log con timestamp
            3. Bloquea nuevas órdenes durante 86400 segundos (24h)
            4. Retorna True (activado)

        Args:
            current_drawdown: Drawdown actual como fracción (e.g. 0.18 = 18%)
            max_dd:           Límite máximo de drawdown (default: 0.15 = 15%)

        Returns:
            True si circuit breaker fue activado, False si no
        """
        if current_drawdown < max_dd:
            # Verificar si estamos en cooldown activo
            if self._circuit_breaker_active:
                remaining = self._circuit_breaker_until - time.time()
                if remaining > 0:
                    logger.warning(
                        "Circuit breaker activo (%.0f seg restantes)", remaining
                    )
                    return True
                else:
                    # Cooldown expirado
                    self._circuit_breaker_active = False
                    logger.info("Circuit breaker desactivado (cooldown expirado)")
            return False

        # ── Activar circuit breaker ───────────────────────────────────

        now = datetime.now(timezone.utc)
        self._circuit_breaker_active = True
        self._circuit_breaker_until = time.time() + self.CIRCUIT_BREAKER_COOLDOWN

        # 1. Cerrar todas las posiciones
        if self._execution_engine is not None:
            try:
                self._execution_engine.close_all_positions()
                logger.critical(
                    "CIRCUIT BREAKER: Todas las posiciones cerradas (DD=%.1f%% >= %.1f%%)",
                    current_drawdown * 100, max_dd * 100,
                )
            except Exception as exc:
                logger.error("Error cerrando posiciones: %s", exc)
        else:
            logger.warning("execution_engine no configurado — no se cerraron posiciones")

        # 2. Escribir en log
        log_path = self._log_dir / "circuit_breaker.log"
        log_entry = (
            f"[{now.isoformat()}] CIRCUIT BREAKER ACTIVADO | "
            f"drawdown={current_drawdown:.4f} ({current_drawdown:.1%}) | "
            f"max_dd={max_dd:.4f} ({max_dd:.1%}) | "
            f"cooldown={self.CIRCUIT_BREAKER_COOLDOWN}s\n"
        )
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(log_entry)
            logger.info("Circuit breaker log escrito en %s", log_path)
        except OSError as exc:
            logger.error("Error escribiendo circuit breaker log: %s", exc)

        # 3. Persistir estado en disco (sobrevive crashes)
        self._save_cb_state()

        logger.critical(
            "CIRCUIT BREAKER: Nuevas órdenes bloqueadas por %d segundos (estado persistido)",
            self.CIRCUIT_BREAKER_COOLDOWN,
        )

        # Telegram: Circuit Breaker alert (fire-and-forget)
        try:
            from nexus.reporting.telegram_reporter import TelegramReporter
            TelegramReporter.get_instance().fire_circuit_breaker(
                current_drawdown, self.CIRCUIT_BREAKER_COOLDOWN // 3600
            )
        except Exception:
            pass  # Telegram failure never blocks risk management

        return True

    def is_circuit_breaker_active(self) -> bool:
        """Retorna True si el circuit breaker está activo y en cooldown."""
        if not self._circuit_breaker_active:
            return False
        if time.time() >= self._circuit_breaker_until:
            self._circuit_breaker_active = False
            self._clear_cb_state()
            logger.info("Circuit breaker desactivado (cooldown expirado, estado limpiado)")
            return False
        return True

    # ── Persistencia Dual del Circuit Breaker (Redis + Disco) ──────────

    _REDIS_CB_KEY = "NEXUS:CIRCUIT_BREAKER"
    _REDIS_CB_UNTIL_KEY = "NEXUS:CIRCUIT_BREAKER_UNTIL"

    def _get_cb_state_path(self) -> Path:
        return self._log_dir / self.CB_STATE_FILE

    def _save_cb_state(self) -> None:
        """Persiste el estado del CB en Redis (atómico) + JSON (fallback local)."""
        state = {
            "is_active": self._circuit_breaker_active,
            "until_timestamp": self._circuit_breaker_until,
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }

        # 1. Redis (atómico, compartido entre instancias)
        if self._redis:
            try:
                ttl = max(1, int(self._circuit_breaker_until - time.time()))
                pipe = self._redis.pipeline()
                pipe.set(self._REDIS_CB_KEY, "1", ex=ttl)
                pipe.set(self._REDIS_CB_UNTIL_KEY, str(self._circuit_breaker_until), ex=ttl)
                pipe.execute()
                logger.info("CB state persistido en Redis (TTL=%ds)", ttl)
            except Exception as exc:
                logger.warning("Redis CB write failed: %s (falling back to disk)", exc)

        # 2. Disco (local fallback — sobrevive Redis outages)
        try:
            with open(self._get_cb_state_path(), "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except OSError as exc:
            logger.error("Error persistiendo CB state en disco: %s", exc)

    def _load_cb_state(self) -> None:
        """Restaura el estado del CB: Redis (cluster-wide) → disco (local fallback)."""
        loaded = False

        # 1. Intentar Redis primero (fuente de verdad para múltiples instancias)
        if self._redis:
            try:
                raw_until = self._redis.get(self._REDIS_CB_UNTIL_KEY)
                if raw_until:
                    until = float(raw_until.decode("utf-8") if isinstance(raw_until, bytes) else raw_until)
                    if time.time() < until:
                        self._circuit_breaker_active = True
                        self._circuit_breaker_until = until
                        remaining = int(until - time.time())
                        logger.warning(
                            "CB restaurado desde Redis: activo por %d seg más", remaining
                        )
                        loaded = True
                    else:
                        self._redis.delete(self._REDIS_CB_KEY, self._REDIS_CB_UNTIL_KEY)
            except Exception as exc:
                logger.debug("Redis CB read failed: %s", exc)

        if loaded:
            return

        # 2. Fallback a disco
        path = self._get_cb_state_path()
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            until = state.get("until_timestamp", 0)
            if time.time() < until:
                self._circuit_breaker_active = True
                self._circuit_breaker_until = until
                remaining = int(until - time.time())
                logger.warning(
                    "CB restaurado desde disco: activo por %d seg más", remaining
                )
            else:
                self._clear_cb_state()
        except Exception as exc:
            logger.error("Error cargando CB state: %s", exc)

    def _clear_cb_state(self) -> None:
        """Elimina el estado del CB de Redis y disco."""
        # Redis
        if self._redis:
            try:
                self._redis.delete(self._REDIS_CB_KEY, self._REDIS_CB_UNTIL_KEY)
            except Exception:
                pass
        # Disco
        path = self._get_cb_state_path()
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    # (Método correlation_check eliminado en favor de correlation_penalty)

    # ══════════════════════════════════════════════════════════════════
    #  Métodos auxiliares para el sistema principal
    # ══════════════════════════════════════════════════════════════════

    def update_portfolio(
        self, portfolio_value: float, positions: List[Dict[str, Any]]
    ) -> None:
        """Actualiza el estado del portafolio."""
        self._portfolio_value = portfolio_value
        self._open_positions = positions

    def get_risk_report(self) -> Dict[str, Any]:
        """Genera un reporte resumido de riesgo para el árbitro."""
        return {
            "portfolio_value": self._portfolio_value,
            "num_positions": len(self._open_positions),
            "circuit_breaker_active": self.is_circuit_breaker_active(),
        }

    # ══════════════════════════════════════════════════════════════════
    #  MÉTODO 6: Correlation Penalty (Factor de Penalización)
    # ══════════════════════════════════════════════════════════════════

    def correlation_penalty(
        self,
        new_symbol: str,
        existing_positions: List[Dict[str, Any]],
        returns_data: Dict[str, List[float]],
        threshold: float = 0.70,
    ) -> float:
        """
        Calcula un factor de penalización (0.0 – 1.0) para el sizing de una
        nueva posición basándose en la correlación con las posiciones existentes.

        En lugar de bloquear binariamente (como correlation_check), este método
        reduce proporcionalmente el tamaño de la posición según el grado de
        correlación con el portafolio actual.

        Fórmula:
            penalty = max(0, 1 - avg_correlation_above_threshold)

        Ejemplo:
            - Sin posiciones existentes → penalty = 1.0 (sin penalización)
            - Corr promedio = 0.90 → penalty = 0.10 (solo 10% del sizing)
            - Corr promedio = 0.50 → penalty = 1.0 (bajo umbral, sin penalización)

        Args:
            new_symbol: Símbolo de la nueva posición a abrir
            existing_positions: Lista de posiciones abiertas con {"symbol": str}
            returns_data: Dict {symbol: [returns_list]} con retornos recientes
            threshold: Umbral de correlación por debajo del cual no se penaliza

        Returns:
            Factor de penalización (0.0 = bloquear, 1.0 = sin penalización)
        """
        if not _HAS_SCIPY:
            return 1.0

        if not existing_positions:
            return 1.0  # Sin posiciones abiertas → sin penalización

        new_returns = returns_data.get(new_symbol, [])
        if len(new_returns) < 5:
            return 1.0  # Datos insuficientes

        new_arr = np.array(new_returns, dtype=np.float64)
        correlations_above: List[float] = []

        for pos in existing_positions:
            sym = pos.get("symbol", "")
            pos_returns = returns_data.get(sym, [])
            if len(pos_returns) < 5 or sym == new_symbol:
                continue

            pos_arr = np.array(pos_returns, dtype=np.float64)
            min_len = min(len(new_arr), len(pos_arr))
            if min_len < 5:
                continue

            corr, _ = scipy_stats.pearsonr(
                new_arr[:min_len], pos_arr[:min_len]
            )
            abs_corr = abs(float(corr))  # type: ignore

            if abs_corr > threshold:
                correlations_above.append(abs_corr)

                logger.info(
                    "📊 Correlation %s↔%s: r=%.3f (above threshold %.2f)",
                    new_symbol, sym, abs_corr, threshold,
                )

        if not correlations_above:
            return 1.0  # Todas bajo el umbral

        avg_corr = float(np.mean(correlations_above))
        penalty = max(0.0, 1.0 - avg_corr)

        logger.info(
            "📊 Correlation Penalty [%s]: avg_corr=%.3f → sizing factor=%.2f",
            new_symbol, avg_corr, penalty,
        )
        return round(penalty, 4)  # type: ignore

    # ══════════════════════════════════════════════════════════════════
    #  MÉTODO 7: ATR-Based Position Sizing (Volatility-Scaled)
    # ══════════════════════════════════════════════════════════════════

    def atr_position_size(
        self,
        capital: float,
        atr: float,
        current_price: float,
        risk_per_trade: float = 0.01,
        atr_multiplier: float = 2.0,
    ) -> float:
        """
        Calcula el tamaño de posición basado en la volatilidad actual (ATR),
        siguiendo la metodología estándar de mesas institucionales.

        Fórmula:
            risk_amount = capital × risk_per_trade
            dollar_risk_per_unit = atr × atr_multiplier
            units = risk_amount / dollar_risk_per_unit
            position_pct = (units × current_price / capital) × 100

        Efecto:
            - Alta volatilidad (ATR alto) → posición más pequeña
            - Baja volatilidad (ATR bajo) → posición más grande

        Args:
            capital: Capital total disponible
            atr: Average True Range actual del activo
            current_price: Precio actual del activo
            risk_per_trade: Porcentaje de capital a arriesgar por trade (default 1%)
            atr_multiplier: Multiplicador del ATR para definir el stop-loss distance

        Returns:
            Tamaño de posición como porcentaje del capital (0.0 – 15.0)
        """
        if atr <= 0 or current_price <= 0 or capital <= 0:
            logger.error(
                "ATR sizing: parámetros inválidos (atr=%.4f, price=%.2f, capital=%.2f) "
                "→ retornando 0.0 (fail-safe). Verificar data handler.",
                atr, current_price, capital
            )
            return 0.0  # Fail-safe: datos inválidos = no abrir posición

        risk_amount = capital * risk_per_trade
        dollar_risk_per_unit = atr * atr_multiplier
        units = risk_amount / dollar_risk_per_unit
        position_pct = (units * current_price / capital) * 100

        # Clamp entre 1% y 15% del portafolio
        position_pct = max(1.0, min(position_pct, 15.0))

        logger.info(
            "📐 ATR Sizing: ATR=$%.2f, risk=$%.2f, units=%.6f → %.1f%% del capital",
            atr, risk_amount, units, position_pct,
        )
        return round(position_pct, 2)  # type: ignore

    def max_exposure_check(
        self,
        proposed_size_pct: float,
        current_exposure_pct: float,
        max_portfolio_exposure: float = 0.80,
    ) -> float:
        """
        Verifica que la exposición total del portafolio no exceda el límite.

        Limita el tamaño propuesto si sumarlo a la exposición actual
        superaría max_portfolio_exposure (default: 80% del portafolio).

        Args:
            proposed_size_pct:      Tamaño propuesto como % del capital (0-100)
            current_exposure_pct:   Exposición actual total como % del capital (0-100)
            max_portfolio_exposure: Límite máximo de exposición total (0.0-1.0)

        Returns:
            Tamaño ajustado como % del capital (puede ser 0.0 si no hay espacio)
        """
        max_pct = max_portfolio_exposure * 100.0
        available_pct = max(0.0, max_pct - current_exposure_pct)

        if available_pct <= 0:
            logger.warning(
                "max_exposure_check: exposición actual %.1f%% >= límite %.1f%% → bloqueando",
                current_exposure_pct, max_pct
            )
            return 0.0

        adjusted = min(proposed_size_pct, available_pct)

        if adjusted < proposed_size_pct:
            logger.info(
                "max_exposure_check: tamaño reducido %.1f%% → %.1f%% (límite de exposición)",
                proposed_size_pct, adjusted
            )

        return round(adjusted, 2)


# ══════════════════════════════════════════════════════════════════════
#  BLOQUE DE VALIDACIÓN — Asserts con valores de prueba conocidos
# ══════════════════════════════════════════════════════════════════════

def _run_validation() -> bool:
    """
    Ejecuta todos los asserts de verificación matemática.
    Retorna True si todos pasan, False si alguno falla.
    """
    import sys
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # type: ignore

    rm = QuantRiskManager(log_dir="logs")
    passed = 0
    failed = 0
    total = 0

    def check(name: str, condition: bool, detail: str = ""):
        nonlocal passed, failed, total
        total += 1  # type: ignore
        if condition:
            passed += 1  # type: ignore
            print(f"  [OK]  {name}")
        else:
            failed += 1  # type: ignore
            print(f"  [FAIL] {name}: {detail}")

    print("\n" + "=" * 60)
    print("  VALIDACION MATEMATICA — QuantRiskManager")
    print("=" * 60)

    # ── KELLY CRITERION ───────────────────────────────────────────

    print("\n--- Kelly Criterion ---")

    # Test 1: kelly(0.55, 2.0, 1.0) → f* = (2*0.55 - 0.45)/2 = 0.325 → cap a 0.25
    k1 = rm.kelly_criterion(0.55, 2.0, 1.0)
    f_star_1 = (2.0 * 0.55 - 0.45) / 2.0  # = 0.325
    check(
        "kelly(0.55, 2.0, 1.0) = 0.25 (cappado)",
        k1 == 0.25,
        f"Esperado 0.25 (f*={f_star_1:.4f}), obtenido {k1}",
    )

    # Test 2: kelly(0.50, 1.0, 1.0) → f* = (1*0.5 - 0.5)/1 = 0.0
    k2 = rm.kelly_criterion(0.50, 1.0, 1.0)
    check(
        "kelly(0.50, 1.0, 1.0) = 0.0 (breakeven)",
        k2 == 0.0,
        f"Esperado 0.0, obtenido {k2}",
    )

    # Test 3: kelly(0.40, 1.0, 1.0) → f* = (1*0.4 - 0.6)/1 = -0.2 → 0.0
    k3 = rm.kelly_criterion(0.40, 1.0, 1.0)
    check(
        "kelly(0.40, 1.0, 1.0) = 0.0 (negativo)",
        k3 == 0.0,
        f"Esperado 0.0, obtenido {k3}",
    )

    # Test 4: kelly(0.60, 1.5, 1.0) → f* = (1.5*0.6 - 0.4)/1.5 = 0.3333 → cap 0.25
    k4 = rm.kelly_criterion(0.60, 1.5, 1.0)
    check(
        "kelly(0.60, 1.5, 1.0) = 0.25 (cappado)",
        k4 == 0.25,
        f"Esperado 0.25, obtenido {k4}",
    )

    # Test 5: kelly(0.55, 1.2, 1.0) → f* = (1.2*0.55 - 0.45)/1.2 = 0.175
    k5 = rm.kelly_criterion(0.55, 1.2, 1.0)
    expected_k5 = round((1.2 * 0.55 - 0.45) / 1.2, 6)  # type: ignore
    check(
        f"kelly(0.55, 1.2, 1.0) = {expected_k5} (sin cap)",
        k5 == expected_k5,
        f"Esperado {expected_k5}, obtenido {k5}",
    )

    # ── MONTE CARLO ───────────────────────────────────────────────

    print("\n--- Monte Carlo Simulation ---")

    # Test 1: retornos constantes de +1% → p50 debe ser positivo
    constant_returns = [0.01] * 30
    mc1 = rm.monte_carlo_simulation(constant_returns, n=1000)
    check(
        "MC retornos +1% constante → p50 > capital inicial",
        mc1["p50"] > 10_000.0,
        f"p50={mc1['p50']}, esperado > 10000",
    )
    check(
        "MC retornos +1% → p5 > capital inicial",
        mc1["p5"] > 10_000.0,
        f"p5={mc1['p5']}",
    )
    check(
        "MC retornos +1% → max_dd = 0 (sin drawdowns)",
        mc1["max_dd"] == 0.0,
        f"max_dd={mc1['max_dd']}",
    )

    # Test 2: retornos mixtos → p5 < p50 < p95
    mixed_returns = [0.02, -0.01, 0.015, -0.005, 0.01, -0.02, 0.03, -0.01]
    mc2 = rm.monte_carlo_simulation(mixed_returns, n=5000)
    check(
        "MC retornos mixtos → p5 < p50 < p95",
        mc2["p5"] < mc2["p50"] < mc2["p95"],
        f"p5={mc2['p5']}, p50={mc2['p50']}, p95={mc2['p95']}",
    )
    check(
        "MC retornos mixtos → max_dd > 0",
        mc2["max_dd"] > 0,
        f"max_dd={mc2['max_dd']}",
    )

    # Test 3: retornos negativos constantes → p50 < capital inicial
    neg_returns = [-0.02] * 20
    mc3 = rm.monte_carlo_simulation(neg_returns, n=1000)
    check(
        "MC retornos -2% constante → p50 < capital inicial",
        mc3["p50"] < 10_000.0,
        f"p50={mc3['p50']}",
    )

    # ── VALUE AT RISK ─────────────────────────────────────────────

    print("\n--- Value at Risk (VaR HISTÓRICO) ---")

    # Test 1: retornos conocidos → verificar percentil real
    var_returns = [0.01, 0.02, -0.01, 0.005, -0.015, 0.03, -0.005, 0.01, -0.02, 0.015]
    expected_var_hist = abs(float(np.percentile(var_returns, 5)))
    var1 = rm.value_at_risk(var_returns, confidence=0.95)
    check(
        "VaR_Hist(95%) = |percentile(returns, 5%)|",
        abs(var1 - round(expected_var_hist, 6)) < 1e-5,
        f"Esperado {expected_var_hist:.6f}, obtenido {var1}",
    )

    # Test 2: retornos todos positivos → VaR debería ser 0 o muy bajo
    pos_returns = [0.01, 0.02, 0.015, 0.025, 0.01]
    var2 = rm.value_at_risk(pos_returns, confidence=0.95)
    check(
        "VaR_Hist con retornos positivos = 0",
        var2 == 0.0 or var2 < 0.01,
        f"VaR={var2}",
    )

    # ── CIRCUIT BREAKER ───────────────────────────────────────────

    print("\n--- Circuit Breaker ---")

    # Test 1: DD bajo → no activar
    rm_cb = QuantRiskManager(log_dir="logs")
    cb1 = rm_cb.circuit_breaker_check(0.05, max_dd=0.15)
    check(
        "CB drawdown 5% < 15% → False (no activar)",
        cb1 is False,
        f"Obtenido {cb1}",
    )

    # Test 2: DD alto → activar
    cb2 = rm_cb.circuit_breaker_check(0.18, max_dd=0.15)
    check(
        "CB drawdown 18% >= 15% → True (activar)",
        cb2 is True,
        f"Obtenido {cb2}",
    )

    # Test 3: Verificar que circuit breaker quedó activo
    check(
        "CB activo después de activación",
        rm_cb.is_circuit_breaker_active() is True,
        f"Obtenido {rm_cb.is_circuit_breaker_active()}",
    )

    # Test 4: DD bajo pero CB activo → sigue activo
    cb3 = rm_cb.circuit_breaker_check(0.02, max_dd=0.15)
    check(
        "CB activo: DD bajo pero en cooldown → True",
        cb3 is True,
        f"Obtenido {cb3}",
    )

    # ── ATR POSITION SIZING ───────────────────────────────────────

    print("\n--- ATR Position Sizing ---")

    # Test 1: Capital 10k, ATR $100, Price $2000, risk 1%, multiplier 2
    # risk_amount = 10000 * 0.01 = 100
    # dollar_risk_per_unit = 100 * 2 = 200
    # units = 100 / 200 = 0.5
    # pct = (0.5 * 2000 / 10000) * 100 = 10.0%
    atr_pct1 = rm.atr_position_size(10000.0, 100.0, 2000.0, 0.01, 2.0)
    check(
        "ATR Sizing normal → 10.0%",
        atr_pct1 == 10.0,
        f"Esperado 10.0, obtenido {atr_pct1}",
    )

    # Test 2: Altísima volatilidad → se recorta por min/max cap (ej min 1.0%)
    atr_invalid = rm.atr_position_size(0.0, 2000.0, 10000.0, 0.01, 2.0)
    check(
        "ATR parámetros inválidos (atr=0) → 0.0 (fail-safe)",
        atr_invalid == 0.0,
        f"Esperado 0.0, obtenido {atr_invalid}",
    )
    atr_pct2 = rm.atr_position_size(10000.0, 10000.0, 2000.0, 0.01, 2.0)
    check(
        "ATR Volatilidad extrema → clamp a 1.0%",
        atr_pct2 == 1.0,
        f"Esperado 1.0, obtenido {atr_pct2}",
    )

    # Test 3: Baja volatilidad → se recorta max cap (15.0%)
    atr_pct3 = rm.atr_position_size(10000.0, 1.0, 2000.0, 0.01, 2.0)
    check(
        "ATR Baja volatilidad → clamp a 15.0%",
        atr_pct3 == 15.0,
        f"Esperado 15.0, obtenido {atr_pct3}",
    )

    # ── CORRELATION PENALTY ───────────────────────────────────────

    print("\n--- Correlation Penalty ---")

    if _HAS_SCIPY:
        # Test 1: Posiciones idénticas → correlación 1.0 → penalty = 0.0
        pos_identical = [{"symbol": "BTC"}]
        ret_data_1 = {
            "ETH": [0.01, 0.02, -0.01, 0.015, -0.005],
            "BTC": [0.01, 0.02, -0.01, 0.015, -0.005],
        }
        cp1 = rm.correlation_penalty("ETH", pos_identical, ret_data_1, threshold=0.70)
        check(
            "Correlación idéntica (r=1.0) → Penalty 0.0",
            cp1 == 0.0,
            f"Obtenido {cp1}",
        )

        # Test 2: Correlación moderada (bajo umbral) → Penalty 1.0
        pos_uncorr = [{"symbol": "GOLD"}]
        ret_data_2 = {
            "BTC":  [0.01, -0.02, 0.03, -0.01, 0.02, -0.005, 0.015],
            "GOLD": [0.005, 0.01, -0.005, 0.02, -0.01, 0.015, -0.002],
        }
        cp2 = rm.correlation_penalty("BTC", pos_uncorr, ret_data_2, threshold=0.90)
        check(
            "Correlación bajo threshold=0.90 → Penalty 1.0 (sin castigo)",
            cp2 == 1.0,
            f"Obtenido {cp2}",
        )
    else:
        print("  [SKIP] scipy no instalado — correlation checks omitidos")

    # ── RESULTADO FINAL ───────────────────────────────────────────

    print("\n--- Max Exposure Check ---")

    rm_exp = QuantRiskManager(log_dir="logs")

    # Test 1: sin espacio → retorna 0.0
    exp1 = rm_exp.max_exposure_check(10.0, 82.0, 0.80)
    check(
        "max_exposure: exposición 82% >= 80% → retorna 0.0",
        exp1 == 0.0,
        f"Obtenido {exp1}",
    )

    # Test 2: espacio parcial → tamaño reducido
    exp2 = rm_exp.max_exposure_check(15.0, 70.0, 0.80)
    check(
        "max_exposure: propuesto 15%, disponible 10% → retorna 10.0",
        exp2 == 10.0,
        f"Obtenido {exp2}",
    )

    # Test 3: hay espacio suficiente → tamaño sin cambios
    exp3 = rm_exp.max_exposure_check(5.0, 30.0, 0.80)
    check(
        "max_exposure: propuesto 5%, disponible 50% → retorna 5.0",
        exp3 == 5.0,
        f"Obtenido {exp3}",
    )

    # Test 4: kelly floor activo
    k_floor = rm_exp.kelly_criterion(0.501, 1.0, 1.0)
    check(
        "Kelly f* < MIN_FLOOR → retorna 0.0",
        k_floor == 0.0,
        f"Obtenido {k_floor}",
    )

    print("\n" + "=" * 60)
    if failed == 0:
        print(f"  RISK MANAGER VALIDADO: {passed}/{total} tests passed")
    else:
        print(f"  ERRORES ENCONTRADOS: {failed}/{total} tests fallaron")
    print("=" * 60)

    return failed == 0


# Ejecutar validación cuando se corre el archivo directamente
if __name__ == "__main__":
    import sys
    success = _run_validation()
    sys.exit(0 if success else 1)
