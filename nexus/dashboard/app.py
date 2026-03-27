"""
NEXUS Trading System — Dashboard Backend (FastAPI)
==================================================
Servidor REST asíncrono estilo institucional para exponer telemétria 
y estado general de operaciones en tiempo real.
"""

import os
import json
import asyncio
import random
import time
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- ESTADO GLOBAL (Simulando Memoria del Engine en Frontend) ---
SYSTEM_STATE: Dict[str, Any] = {
    "mode": "PAPER",
    "panic": False,
    "pause": False,
    "uptime_start": float(time.time()),
}

# Precios base hardcodeados para la demostración institucional
BASE_PRICES = {
    "cripto": {"BTC/USDT": 69420.50, "ETH/USDT": 3810.25, "BNB/USDT": 590.10, "SOL/USDT": 145.30, "LTC/USDT": 85.40, "LINK/USDT": 18.20},
    "inversors": {"DXY": 104.25, "SP500": 5150.30, "NDX NASDAQ": 18200.40, "XAUUSD": 2340.50, "USOIL": 82.50, "UKOIL": 86.30, "NATGAS": 1.85},
    "binary": {"USD/EUR": 0.92, "USD/CAD": 1.35, "USD/JPY": 151.20, "RUB/USD": 0.010, "EUR/USD": 1.08, "CAD/USD": 0.74, "JPY/USD": 0.0066}
}

app = FastAPI(
    title="NEXUS Institutional Dashboard",
    description="Real-time Quantitative Trading Telemetry",
    version="1.0.0"
)

# CORS para permitir conexiones externas si es necesario
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuración de Rutas
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
LOGS_FILE = os.path.join(BASE_DIR, "..", "logs", "telemetry.jsonl")

# Sirviendo el Frontend Vanilla
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Sirviendo los Reportes HTML (Backtesting)
REPORTS_DIR = os.path.join(BASE_DIR, "..", "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)
app.mount("/reports", StaticFiles(directory=REPORTS_DIR), name="reports")

@app.get("/", response_class=HTMLResponse)
async def read_index():
    """Sirve el dashboard principal."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return "<h1>Dashboard UI not found. Please build frontend.</h1>"
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/v1/status")
async def get_system_status() -> Dict[str, Any]:
    """Retorna el estado general sincronizado de NEXUS."""
    uptime_hrs = (time.time() - float(SYSTEM_STATE["uptime_start"])) / 3600.0
    return {
        "status": "ONLINE" if not SYSTEM_STATE["pause"] else "PAUSED",
        "mode": str(SYSTEM_STATE["mode"]),
        "panic_active": bool(SYSTEM_STATE["panic"]),
        "uptime_hrs": round(float(uptime_hrs), 4),  # type: ignore
        "capital": 10000.00 if SYSTEM_STATE["mode"] == "PAPER" else 250000.00,
        "open_positions": 0 if SYSTEM_STATE["panic"] else 3,
        "exposure_pct": 0.0 if SYSTEM_STATE["panic"] else 18.5,
        "drawdown_pct": 1.2
    }


@app.get("/api/v1/logs")
async def get_telemetry_logs(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Lee las últimas 'limit' líneas del archivo JSONL de telemetría,
    exponiendo el reasoning de los agentes y trades ejecutados.
    """
    if not os.path.exists(LOGS_FILE):
        return []

    logs = []
    try:
        # Leemos el archivo y sacamos las ultimas N lineas
        with open(LOGS_FILE, "r", encoding="utf-8") as f:
            lines: List[str] = f.readlines()
            recent_lines = lines[-limit:]  # type: ignore
            for line in recent_lines:
                if line.strip():
                    logs.append(json.loads(line))
        return logs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/fattah")
async def get_fattah_status() -> Dict[str, Any]:
    """
    Retorna el estado dinámico del Modo Fattah (Macro-Hedge)
    """
    state_file = os.path.join(BASE_DIR, "..", "data", "fattah_state.json")
    if not os.path.exists(state_file):
        return {
            "is_active": False,
            "fear_index": {"score": "--", "regime": "STANDBY", "sentiment_reason": "Inicia el motor macro..."},
            "optimal_allocation_pct": {"USD_Stable": "--", "Gold_GLD": "--", "BTC_USD": "--", "CHF_JPY": "--", "Commodities": "--"}
        }
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
            state["is_active"] = True
            return state
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/calibration")
async def get_calibration_params() -> Dict[str, Any]:
    """
    Retorna los parámetros activos de calibración WFO.
    Leídos desde config/wfo_active_params.json.
    """
    params_file = os.path.join(BASE_DIR, "..", "config", "wfo_active_params.json")
    if not os.path.exists(params_file):
        return {
            "version": "1.0.0",
            "last_calibrated": None,
            "calibration_source": "default",
            "spot": {"sl_atr_mult": 2.0, "tp_atr_mult": 3.0, "min_confidence": 0.65},
            "binary": {"expiry_bars": 3, "payout_pct": 0.85, "min_confidence": 0.70},
            "risk": {"max_drawdown": 0.15, "kelly_fraction": 0.25, "max_positions": 3},
        }
    try:
        with open(params_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/wfo-report")
async def get_wfo_report(mode: str = "binary") -> Dict[str, Any]:
    """
    Retorna el último reporte WFO para el modo especificado.
    Busca el archivo más reciente en reports/wfo_calibration_{mode}_*.json
    """
    import glob
    reports_dir = os.path.join(BASE_DIR, "..", "reports")
    pattern = os.path.join(reports_dir, f"wfo_calibration_{mode}_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    
    if not files:
        raise HTTPException(status_code=404, detail=f"No WFO report found for mode: {mode}")
    
    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ModeRequest(BaseModel):
    mode: str

@app.post("/api/v1/control/mode")
async def toggle_mode(req: ModeRequest) -> Dict[str, Any]:
    """Alterna el modo global de ejecución (PAPER/LIVE)."""
    new_mode = req.mode.upper()
    if new_mode in ["PAPER", "LIVE"]:
        SYSTEM_STATE["mode"] = new_mode
    return SYSTEM_STATE

@app.post("/api/v1/control/panic")
async def toggle_panic() -> Dict[str, Any]:
    """Activa/Desactiva pánico general (Liquidación de emergencia)."""
    SYSTEM_STATE["panic"] = not SYSTEM_STATE["panic"]
    if SYSTEM_STATE["panic"]:
        SYSTEM_STATE["pause"] = True # Panic automatica la pausa
    return SYSTEM_STATE

@app.post("/api/v1/control/pause")
async def toggle_pause() -> Dict[str, Any]:
    """Pausa/Reanuda el trading algorítmico."""
    SYSTEM_STATE["pause"] = not SYSTEM_STATE["pause"]
    return SYSTEM_STATE

@app.get("/api/v1/market-data")
async def get_market_data() -> Dict[str, Any]:
    """Retorna precios simulados con ligeras variaciones aleatorias para el Screener."""
    data = {"cripto": [], "inversors": [], "binary": []}  # type: ignore
    for cat, assets in BASE_PRICES.items():
        for sym, base_px in assets.items():
            # Simular fluctuación estocástica suave
            var_pct = random.uniform(-2.5, 3.2)
            current_px = base_px * (1 + (var_pct / 100))
            
            # Formateo de decimales según el activo
            if current_px < 5:
                px_str = f"{current_px:.4f}"
            else:
                px_str = f"{current_px:,.2f}"
                
            data[cat].append({  # type: ignore
                "symbol": sym,
                "price": px_str,
                "change_pct": round(float(var_pct), 2)  # type: ignore
            })
    return data

if __name__ == "__main__":
    import uvicorn  # type: ignore
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)

