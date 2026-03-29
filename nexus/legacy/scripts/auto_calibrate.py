"""
NEXUS Trading System — WFO Auto-Calibration Engine
=====================================================
Fase 12: Automatización Completa del Pipeline de Calibración.

Este script coordina la "banda sonora" semanal:
  1. Descarga datos frescos vía Market Vault (Cache-Aware)
  2. Ejecuta Walk-Forward Optimization (Spot + Binary)
  3. Extrae los mejores parámetros de la última ventana OOS
  4. Escribe `config/wfo_active_params.json` (Hot-Reload para main.py)
  5. Reporta vía Telegram el resultado de la calibración

Ejecución manual:
    python auto_calibrate.py --run-now

Programación semanal (dentro de main.py):
    Se lanza automáticamente cada sábado a las 00:00 GMT-5.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import schedule  # type: ignore
    _HAS_SCHEDULE = True
except ImportError:
    _HAS_SCHEDULE = False

logger = logging.getLogger("nexus.auto_calibrate")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

_TZ_GMT5 = timezone(timedelta(hours=-5))
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
_PARAMS_FILE = _CONFIG_DIR / "wfo_active_params.json"
_REPORTS_DIR = _PROJECT_ROOT / "reports"
_VAULT_DIR = _PROJECT_ROOT / "data" / "vault"


# ══════════════════════════════════════════════════════════════════════
#  Parámetros Óptimos por Defecto (Fallback)
# ══════════════════════════════════════════════════════════════════════

DEFAULT_PARAMS: Dict[str, Any] = {
    "version": "1.0.0",
    "last_calibrated": None,
    "calibration_source": "default",
    "spot": {
        "sl_atr_mult": 2.0,
        "tp_atr_mult": 3.0,
        "min_confidence": 0.65,
        "lookback": 60,
    },
    "binary": {
        "expiry_bars": 3,
        "payout_pct": 0.80,
        "stake_pct": 1.0,
        "min_confidence": 0.70,
        "max_positions": 3,
    },
    "risk": {
        "max_drawdown": 0.15,
        "kelly_fraction": 0.25,
        "max_positions": 3,
        "circuit_breaker_dd": 0.12,
    },
}


# ══════════════════════════════════════════════════════════════════════
#  WFO Auto-Calibrator
# ══════════════════════════════════════════════════════════════════════

class WFOAutoCalibrator:
    """
    Orquestador de calibración semanal.
    Diseñado para ejecutarse como cron job autónomo o llamado
    desde el EvolutionaryAgent cada domingo.
    """

    # Configuración de profundidad de datos por modo
    SPOT_SYMBOLS = ["BTC-USD", "ETH-USD"]
    SPOT_TF = "15m"
    SPOT_YEARS = 1      # 15m max in yfinance is 60 days, vault caps it automatically
    SPOT_IS_DAYS = 30.0 # Adaptación mucho más rápida
    SPOT_OOS_DAYS = 7.0

    BINARY_SYMBOLS = ["BTC-USD"]
    BINARY_TF = "5m"
    BINARY_DAYS = 30       # 5m allows ~60 days from yfinance
    BINARY_IS_DAYS = 10.0  # 10 días IS
    BINARY_OOS_DAYS = 3.0  # 3 días OOS

    # Grid de búsqueda (Spot)
    SPOT_GRID: List[Dict[str, Any]] = [
        {"sl_atr_mult": 1.5, "tp_atr_mult": 2.5, "min_confidence": 0.45},
        {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0, "min_confidence": 0.50},
        {"sl_atr_mult": 2.0, "tp_atr_mult": 3.0, "min_confidence": 0.50},
        {"sl_atr_mult": 2.0, "tp_atr_mult": 4.0, "min_confidence": 0.55},
        {"sl_atr_mult": 2.5, "tp_atr_mult": 3.5, "min_confidence": 0.60},
    ]

    # Grid optimizado para BTC 5m Mean-Reversion (Alpha v3: 55.8% WR probado)
    BINARY_GRID: List[Dict[str, Any]] = [
        {"min_confidence": 0.70, "expiry_bars": 3},
        {"min_confidence": 0.70, "expiry_bars": 4},
        {"min_confidence": 0.70, "expiry_bars": 5},
        {"min_confidence": 0.75, "expiry_bars": 5},
        {"min_confidence": 0.85, "expiry_bars": 5},
    ]

    def __init__(self) -> None:
        self._current_params = self._load_params()
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────
    #  Lectura / Escritura de Parámetros
    # ──────────────────────────────────────────────

    @staticmethod
    def _load_params() -> Dict[str, Any]:
        """Lee el archivo de parámetros activos."""
        if _PARAMS_FILE.exists():
            try:
                with open(_PARAMS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return DEFAULT_PARAMS.copy()

    @staticmethod
    def load_active_params() -> Dict[str, Any]:
        """API pública para que main.py lea los parámetros activos."""
        if _PARAMS_FILE.exists():
            try:
                with open(_PARAMS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return DEFAULT_PARAMS.copy()

    def _save_params(self, params: Dict[str, Any]) -> None:
        """Escribe atómicamente el archivo de parámetros."""
        params["last_calibrated"] = datetime.now(_TZ_GMT5).isoformat()
        params["calibration_source"] = "wfo_auto_calibrator"

        tmp_file = _PARAMS_FILE.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(params, f, indent=4)
            # Reemplazo atómico
            if _PARAMS_FILE.exists():
                _PARAMS_FILE.unlink()
            tmp_file.rename(_PARAMS_FILE)
            logger.info("💾 Parámetros calientes actualizados: %s", _PARAMS_FILE)
        except OSError as e:
            logger.error("Error escribiendo parámetros: %s", e)

    # ──────────────────────────────────────────────
    #  Paso 1: Descarga de datos frescos
    # ──────────────────────────────────────────────

    def _refresh_data(self, mode: str = "spot") -> Optional[str]:
        """Usa MarketDataVault para actualizar la bóveda de datos."""
        try:
            from scripts.market_vault import MarketDataVault  # type: ignore
            vault = MarketDataVault()

            if mode == "spot":
                symbol = self.SPOT_SYMBOLS[0]
                tf = self.SPOT_TF
                years = self.SPOT_YEARS
                # Spot: usar caché si existe (datos grandes)
                path = vault.download_timeframe(symbol, tf, years=years, provider="yfinance", force=False)
            else:
                symbol = self.BINARY_SYMBOLS[0]
                tf = self.BINARY_TF
                # Binary 5m: forzar siempre refresh (solo 55 días, rápido)
                # Convertir días a years (MarketDataVault acepta years como float internamente)
                path = vault.download_timeframe(symbol, tf, years=1, provider="yfinance", force=True)

            if path:
                logger.info("✅ Datos listos para calibración: %s", path)
            return path

        except Exception as e:
            logger.error("Error refrescando datos: %s", e)
            return None

    # ──────────────────────────────────────────────
    #  Paso 2: Walk-Forward Optimization
    # ──────────────────────────────────────────────

    def _run_wfo(self, csv_path: str, mode: str = "spot") -> Optional[Dict[str, Any]]:
        """Ejecuta el Walk-Forward Optimizer y retorna resultados."""
        try:
            from backtesting.walk_forward import WalkForwardOptimizer  # type: ignore

            if mode == "spot":
                is_days = self.SPOT_IS_DAYS
                oos_days = self.SPOT_OOS_DAYS
                grid = self.SPOT_GRID
            else:
                is_days = self.BINARY_IS_DAYS
                oos_days = self.BINARY_OOS_DAYS
                grid = self.BINARY_GRID

            wfo = WalkForwardOptimizer(
                csv_path=csv_path,
                is_days=is_days,
                oos_days=oos_days,
                trading_mode=mode if mode == "binary" else "spot",
            )

            results = wfo.execute_wfo(grid)

            # Guardar resultados en reports/
            report_name = f"wfo_calibration_{mode}_{datetime.now(_TZ_GMT5).strftime('%Y%m%d')}.json"
            report_path = _REPORTS_DIR / report_name
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=4)
            logger.info("📊 Reporte WFO guardado: %s", report_path)

            return results

        except Exception as e:
            logger.error("Error en WFO (%s): %s", mode, e)
            return None

    # ──────────────────────────────────────────────
    #  Paso 3: Extracción de Mejores Parámetros
    # ──────────────────────────────────────────────

    def _extract_best_params(self, wfo_results: Dict[str, Any], mode: str = "spot") -> Dict[str, Any]:
        """
        Selecciona los MEJORES parámetros usando scoring agregado en TODAS las ventanas.

        Método institucional:
          - Para cada combinación de params, calcula el OOS Sharpe promedio
            en las ventanas donde fue elegida como ganadora IS.
          - Penaliza ventanas con cero trades (Sharpe=0.0) como -1.0.
          - Solo acepta si al menos 1 ventana tiene OOS Sharpe > 0.
          - Rechaza si el promedio final es negativo.
        """
        import json as _json
        wfo_log = wfo_results.get("wfo_log", [])
        if not wfo_log:
            logger.warning("WFO log vacío. Usando parámetros por defecto.")
            return {}

        # Agregar scores por combinación de params
        # Key: params frozen as JSON string
        param_scores: Dict[str, List[float]] = {}
        param_objects: Dict[str, Dict[str, Any]] = {}

        for w in wfo_log:
            best_p = w.get("best_params", {})
            oos_s = w.get("oos_sharpe", 0)
            oos_trades = w.get("oos_trades", 0)
            key = _json.dumps(best_p, sort_keys=True)

            # Penalizar: si se generaron 0 trades, es un resultado no válido
            if oos_trades == 0:
                effective_sharpe = -1.0
            else:
                effective_sharpe = oos_s

            if key not in param_scores:
                param_scores[key] = []
                param_objects[key] = best_p
            param_scores[key].append(effective_sharpe)

        # Calcular score final = media de todos los OOS Sharpe
        best_score = -9999.0
        best_key = None
        summary_lines = []

        for key, scores in param_scores.items():
            avg = float(sum(scores) / len(scores)) if scores else 0.0
            positive = sum(1 for s in scores if s > 0)
            summary_lines.append(
                f"  Params: {param_objects[key]} | "
                f"Avg OOS Sharpe: {avg:.3f} | "
                f"Ventanas positivas: {positive}/{len(scores)}"
            )
            if avg > best_score:
                best_score = avg
                best_key = key

        logger.info("═" * 60)
        logger.info("  📊 Scoring Agregado de Parámetros WFO (%s):", mode.upper())
        for line in summary_lines:
            logger.info(line)

        if best_key is None:
            logger.warning("⚠️ Sin parámetros válidos. Manteniendo configuración anterior.")
            return {}

        best_params = param_objects[best_key]
        logger.info(
            "  🏆 GANADOR FINAL [%s]: %s | Score: %.3f",
            mode.upper(), best_params, best_score,
        )

        if best_score <= 0:
            logger.warning(
                "  ⚠️ Score promedio negativo (%.3f). Sistema en modo defensivo. "
                "Manteniendo parámetros anteriores para preservar capital.",
                best_score,
            )
            logger.info("═" * 60)
            return {}

        logger.info("  ✅ Parámetros aceptados y aplicados.")
        logger.info("═" * 60)
        return best_params

    # ──────────────────────────────────────────────
    #  Paso 4: Fusión y Escritura
    # ──────────────────────────────────────────────

    def _merge_and_save(
        self,
        spot_params: Dict[str, Any],
        binary_params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Fusiona los parámetros descubiertos con los defaults
        y los escribe al archivo caliente.
        """
        final = self._load_params()

        # Spot
        if spot_params:
            final["spot"]["sl_atr_mult"] = spot_params.get("sl_atr_mult", final["spot"]["sl_atr_mult"])
            final["spot"]["tp_atr_mult"] = spot_params.get("tp_atr_mult", final["spot"]["tp_atr_mult"])
            final["spot"]["min_confidence"] = spot_params.get("min_confidence", final["spot"]["min_confidence"])

        # Binary
        if binary_params:
            final["binary"]["min_confidence"] = binary_params.get("min_confidence", final["binary"]["min_confidence"])
            final["binary"]["expiry_bars"] = binary_params.get("expiry_bars", final["binary"]["expiry_bars"])

        self._save_params(final)
        self._current_params = final
        return final

    # ──────────────────────────────────────────────
    #  Orquestador Principal
    # ──────────────────────────────────────────────

    def run_full_calibration(self, mode: str = "all") -> Dict[str, Any]:
        """
        Ejecuta el pipeline completo de calibración.

        Args:
            mode: "spot", "binary", o "all" (ambos secuencialmente)

        Returns:
            Dict con los parámetros finales aplicados.
        """
        now = datetime.now(_TZ_GMT5)
        logger.info("═" * 70)
        logger.info("  🎼 NEXUS AUTO-CALIBRATION ENGINE — %s", now.strftime("%Y-%m-%d %H:%M"))
        logger.info("  Modo: %s", mode.upper())
        logger.info("═" * 70)

        start_time = time.time()
        spot_best: Dict[str, Any] = {}
        binary_best: Dict[str, Any] = {}

        # ── SPOT ──────────────────────────────────────
        if mode in ("spot", "all"):
            logger.info("\n🎵 Movimiento I: Calibración SPOT/SWING")
            csv_spot = self._refresh_data("spot")
            if csv_spot:
                wfo_spot = self._run_wfo(csv_spot, "spot")
                if wfo_spot:
                    spot_best = self._extract_best_params(wfo_spot, "spot")

        # ── BINARY ────────────────────────────────────
        if mode in ("binary", "all"):
            logger.info("\n🎵 Movimiento II: Calibración BINARY/HFT")
            csv_binary = self._refresh_data("binary")
            if csv_binary:
                wfo_binary = self._run_wfo(csv_binary, "binary")
                if wfo_binary:
                    binary_best = self._extract_best_params(wfo_binary, "binary")

        # ── MERGE & SAVE ──────────────────────────────
        final_params = self._merge_and_save(spot_best, binary_best)

        elapsed = time.time() - start_time
        logger.info("═" * 70)
        logger.info("  🎼 CALIBRACIÓN COMPLETADA en %.1f segundos", elapsed)
        logger.info("  Spot:   SL=%.1fx ATR | TP=%.1fx ATR | Conf≥%.0f%%",
                     final_params["spot"]["sl_atr_mult"],
                     final_params["spot"]["tp_atr_mult"],
                     final_params["spot"]["min_confidence"] * 100)
        logger.info("  Binary: Expiry=%d bars | Conf≥%.0f%%",
                     final_params["binary"]["expiry_bars"],
                     final_params["binary"]["min_confidence"] * 100)
        logger.info("═" * 70)

        return final_params

    # ──────────────────────────────────────────────
    #  Scheduling (Sábado: Spot / Domingo: Binary)
    # ──────────────────────────────────────────────

    def setup_schedule(self) -> None:
        """Programa la calibración automática dividiendo la carga en dos días."""
        if not _HAS_SCHEDULE:
            logger.warning("Librería 'schedule' no instalada. pip install schedule")
            return

        def _run_support_handler(exc: Exception, mode: str) -> None:
            """Activa el Support Engineer para diagnóstico de crashes."""
            try:
                import asyncio
                from agents.agent_support import NexusSupportEngineer  # type: ignore
                support = NexusSupportEngineer()
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(support.initialize())
                loop.run_until_complete(
                    support.handle_exception(
                        exc,
                        module=f"auto_calibrate.py ({mode})",
                        context={"mode": mode, "cron": True},
                    )
                )
                loop.close()
            except Exception as support_exc:
                logger.error("Support Engineer falló: %s", support_exc)

        def _spot_calibration_job():
            logger.info("⏰ Cron job de calibración semanal SPOT activado")
            try:
                result = self.run_full_calibration(mode="spot")
                _notify_calibration_success("spot", result)
            except Exception as e:
                logger.error("Error en calibración SPOT: %s", e)
                _run_support_handler(e, "spot")

        def _binary_calibration_job():
            logger.info("⏰ Cron job de calibración semanal BINARY activado")
            try:
                result = self.run_full_calibration(mode="binary")
                _notify_calibration_success("binary", result)
            except Exception as e:
                logger.error("Error en calibración BINARY: %s", e)
                _run_support_handler(e, "binary")

        def _notify_calibration_success(mode: str, result: Dict[str, Any]) -> None:
            """Envía notificación exitosa al DEV via Support Engineer."""
            try:
                import asyncio
                from agents.agent_support import NexusSupportEngineer  # type: ignore
                support = NexusSupportEngineer()
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(support.initialize())
                elapsed = time.time()
                loop.run_until_complete(
                    support.notify_calibration_complete(mode, result, elapsed)
                )
                loop.close()
            except Exception:
                pass

        schedule.every().saturday.at("01:00").do(_spot_calibration_job)
        schedule.every().sunday.at("01:00").do(_binary_calibration_job)
        
        logger.info("📅 Calendario Auto-Calibración:")
        logger.info("   - Sábados 01:00 GMT-5 -> SPOT/SWING")
        logger.info("   - Domingos 01:00 GMT-5 -> BINARY/HFT")
        logger.info("🛡️ Support Engineer conectado a pipeline de crashes")

    def run_pending(self) -> None:
        """Despacha tareas pendientes del scheduler."""
        if _HAS_SCHEDULE:
            schedule.run_pending()


# ══════════════════════════════════════════════════════════════════════
#  CLI Entry Point
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="NEXUS WFO Auto-Calibration Engine",
    )
    parser.add_argument("--run-now", action="store_true", help="Ejecutar calibración inmediatamente")
    parser.add_argument("--mode", type=str, default="all", choices=["spot", "binary", "all"],
                        help="Modo de calibración")
    parser.add_argument("--daemon", action="store_true",
                        help="Modo daemon: programar y esperar cron (Sábados 00:00)")
    args = parser.parse_args()

    calibrator = WFOAutoCalibrator()

    if args.run_now:
        result = calibrator.run_full_calibration(mode=args.mode)
        print(f"\n✅ Parámetros finales guardados en: {_PARAMS_FILE}")
        print(json.dumps(result, indent=2))
    elif args.daemon:
        calibrator.setup_schedule()
        print("🕐 Auto-Calibrator en modo daemon. Ctrl+C para detener.")
        try:
            while True:
                calibrator.run_pending()
                time.sleep(60)
        except KeyboardInterrupt:
            print("\n👋 Auto-Calibrator detenido.")
    else:
        parser.print_help()
