"""
NEXUS — Full System Calibration Runner v2
==========================================
Ejecuta la calibración completa: Spot + Binary con WFO Heurístico.
Genera parámetros optimizados en config/wfo_active_params.json.
Al finalizar imprime un resumen profesional de todo lo ocurrido.
"""
import os
import sys
import json
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "nexus"))

from dotenv import load_dotenv
load_dotenv()

from scripts.auto_calibrate import WFOAutoCalibrator, _PARAMS_FILE  # type: ignore


def _bar(char: str = "═", width: int = 70) -> str:
    return char * width


def main() -> None:
    print()
    print(_bar())
    print("  🎼  NEXUS FULL SYSTEM CALIBRATION — PRODUCTION READY")
    print("  WFO Heurístico v2 | Sin tokens LLM | Optimización real")
    print(_bar())
    print()

    calibrator = WFOAutoCalibrator()
    results_by_mode: dict = {}

    # ── FASE 1: SPOT ─────────────────────────────────────────────────
    print(_bar("─"))
    print("  📊 FASE 1: Calibración SPOT / SWING (BTC-USD 1H | 2 años)")
    print(_bar("─"))
    t0 = time.time()
    try:
        result_spot = calibrator.run_full_calibration(mode="spot")
        elapsed_spot = time.time() - t0
        results_by_mode["spot"] = {"params": result_spot, "elapsed": elapsed_spot, "status": "OK"}
        print(f"\n  ✅ SPOT calibrado en {elapsed_spot:.1f}s")
    except Exception as e:
        elapsed_spot = time.time() - t0
        results_by_mode["spot"] = {"elapsed": elapsed_spot, "status": "ERROR", "error": str(e)}
        print(f"  ❌ Error SPOT: {e}")
        import traceback
        traceback.print_exc()

    # ── FASE 2: BINARY ───────────────────────────────────────────────
    print()
    print(_bar("─"))
    print("  📊 FASE 2: Calibración BINARY / HFT (BTC-USD 5M | 55 días)")
    print(_bar("─"))
    t1 = time.time()
    try:
        result_binary = calibrator.run_full_calibration(mode="binary")
        elapsed_binary = time.time() - t1
        results_by_mode["binary"] = {"params": result_binary, "elapsed": elapsed_binary, "status": "OK"}
        print(f"\n  ✅ BINARY calibrado en {elapsed_binary:.1f}s")
    except Exception as e:
        elapsed_binary = time.time() - t1
        results_by_mode["binary"] = {"elapsed": elapsed_binary, "status": "ERROR", "error": str(e)}
        print(f"  ❌ Error BINARY: {e}")
        import traceback
        traceback.print_exc()

    total_elapsed = sum(v["elapsed"] for v in results_by_mode.values())

    # ══ RESUMEN PROFESIONAL FINAL ████████████████████████████████████
    print()
    print(_bar())
    print("  📋  RESUMEN DE CALIBRACIÓN — NEXUS TRADING SYSTEM")
    print(_bar())

    if _PARAMS_FILE.exists():
        with open(_PARAMS_FILE, "r") as f:
            final = json.load(f)

        calibration_source = final.get("calibration_source", "?")
        last_cal = final.get("last_calibrated", "?")

        print(f"\n  📁 Archivo params: {_PARAMS_FILE}")
        print(f"  📅 Última calibración: {last_cal}")
        print(f"  🔧 Fuente: {calibration_source}")
        print()

        # Tabla params
        spot = final.get("spot", {})
        binary = final.get("binary", {})
        risk = final.get("risk", {})

        print("  ┌─────────────────────────────────────────────────────────┐")
        print("  │  PARÁMETROS ACTIVOS SPOT                                │")
        print(f"  │    SL ATR Mult:     {spot.get('sl_atr_mult','?'):<10} (Stop Loss multiplicador)  │")
        print(f"  │    TP ATR Mult:     {spot.get('tp_atr_mult','?'):<10} (Take Profit multiplicador)│")
        print(f"  │    Min Confidence:  {spot.get('min_confidence','?'):<10} (Umbral de confianza)     │")
        print(f"  │    Lookback:        {spot.get('lookback','?'):<10} (Barras de historia)      │")
        print("  ├─────────────────────────────────────────────────────────┤")
        print("  │  PARÁMETROS ACTIVOS BINARY                              │")
        print(f"  │    Expiry Bars:     {binary.get('expiry_bars','?'):<10} (Velas a expiración)      │")
        print(f"  │    Payout %:        {binary.get('payout_pct','?'):<10} (Retorno por acierto)     │")
        print(f"  │    Min Confidence:  {binary.get('min_confidence','?'):<10} (Umbral de confianza)     │")
        print(f"  │    Max Positions:   {binary.get('max_positions','?'):<10} (Posiciones simultáneas)  │")
        print("  ├─────────────────────────────────────────────────────────┤")
        print("  │  PARÁMETROS DE RIESGO                                   │")
        print(f"  │    Max Drawdown:    {risk.get('max_drawdown','?'):<10} (Límite de pérdida)       │")
        print(f"  │    Kelly Fraction:  {risk.get('kelly_fraction','?'):<10} (Fracción Kelly)          │")
        print(f"  │    Circuit Breaker: {risk.get('circuit_breaker_dd','?'):<10} (Corte de emergencia)    │")
        print("  └─────────────────────────────────────────────────────────┘")

    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │  ESTADO POR MODO                                        │")
    for mode, info in results_by_mode.items():
        status_icon = "✅" if info["status"] == "OK" else "❌"
        print(f"  │    {mode.upper():<8} {status_icon}  {info['elapsed']:.1f}s   {info.get('error','')[:35]:<36}│")
    print(f"  │    TOTAL    ⏱️  {total_elapsed:.1f}s{' ' * 46}│")
    print("  └─────────────────────────────────────────────────────────┘")
    print()

    if all(v["status"] == "OK" for v in results_by_mode.values()):
        print("  🚀 SISTEMA CALIBRADO Y LISTO PARA PRODUCCIÓN")
        print("     Los parámetros se cargarán automáticamente en el próximo ciclo.")
    else:
        print("  ⚠️  Calibración parcial. Revisar errores arriba.")

    print()
    print(_bar())


if __name__ == "__main__":
    main()
