"""
NEXUS v4.0 — Operational Control Panel (OCP) Backend
======================================================
Pure async REST API + WebSocket telemetry server.

NO HTML rendering. NO Jinja2. NO static files.
This is a headless API designed to feed a future SPA (React/Svelte).

Architecture:
    Auth:       JWT (HS256) via POST /login
    State:      Redis (MACRO_REGIME, CIRCUIT_BREAKER, PANIC_MODE, AI_MODE)
    Trades:     QuestDB (PG wire on port 8812)
    Control:    Hot-Reload risk params + Kill Switch via Redis writes
    Telemetry:  WebSocket /ws/telemetry (live state broadcast)

Launch:
    uvicorn nexus.dashboard.app:app --host 0.0.0.0 --port 8000

Environment (.env):
    NEXUS_JWT_SECRET     (required — signing key)
    NEXUS_DASH_USER      (default: nexus)
    NEXUS_DASH_PASSWORD   (default: nexus2026)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import jwt
import redis as redis_lib
from dotenv import load_dotenv
from fastapi import (  # type: ignore
    Depends,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware  # type: ignore
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm  # type: ignore
from pydantic import BaseModel  # type: ignore

load_dotenv()

logger = logging.getLogger("nexus.dashboard")


# ══════════════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════════════

JWT_SECRET: str = os.getenv("NEXUS_JWT_SECRET", "nexus-v4-change-me-in-production")
JWT_ALGORITHM: str = "HS256"
JWT_EXPIRE_HOURS: int = 24

DASH_USER: str = os.getenv("NEXUS_DASH_USER", "nexus")
DASH_PASSWORD: str = os.getenv("NEXUS_DASH_PASSWORD", "nexus2026")

REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))

QUESTDB_REST_HOST: str = os.getenv("QUESTDB_REST_HOST", "localhost")
QUESTDB_REST_PORT: int = int(os.getenv("QUESTDB_REST_PORT", "9000"))

# Redis key constants (shared contract with pipeline.py)
_RK_MACRO_REGIME = "NEXUS:MACRO_REGIME"
_RK_CIRCUIT_BREAKER = "NEXUS:CIRCUIT_BREAKER_ACTIVE"
_RK_PANIC_MODE = "NEXUS:PANIC_MODE"
_RK_AI_MODE = "NEXUS:AI_MODE"
_RK_SETTINGS_PREFIX = "NEXUS:SETTINGS:"


# ══════════════════════════════════════════════════════════════════════
#  FastAPI Application
# ══════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="NEXUS v4.0 — Operational Control Panel",
    description="Institutional REST API for the NEXUS HFT Pipeline",
    version="4.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")


# ══════════════════════════════════════════════════════════════════════
#  Redis Connection (Lazy Singleton)
# ══════════════════════════════════════════════════════════════════════

_redis_client: Optional[redis_lib.Redis] = None


def _get_redis() -> redis_lib.Redis:
    """Returns a persistent Redis connection (lazy init)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=0,
            socket_connect_timeout=3,
            decode_responses=True,
        )
    return _redis_client


def _redis_safe_get(key: str, default: str = "") -> str:
    """Read from Redis with graceful fallback if offline."""
    try:
        r = _get_redis()
        val = r.get(key)
        return val if val is not None else default
    except Exception:
        return default


def _redis_safe_set(key: str, value: str) -> bool:
    """Write to Redis with graceful fallback."""
    try:
        r = _get_redis()
        r.set(key, value)
        return True
    except Exception as exc:
        logger.warning(f"Redis write failed for {key}: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════
#  JWT Auth
# ══════════════════════════════════════════════════════════════════════

def _create_token(username: str) -> str:
    """Generate a signed JWT token."""
    payload = {
        "sub": username,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> Dict[str, Any]:
    """Decode and validate a JWT token."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


async def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    """FastAPI dependency — extracts username from valid JWT."""
    payload = _decode_token(token)
    username: Optional[str] = payload.get("sub")
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    return username


# ══════════════════════════════════════════════════════════════════════
#  Pydantic Models
# ══════════════════════════════════════════════════════════════════════

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_hours: int = JWT_EXPIRE_HOURS


class StateResponse(BaseModel):
    macro_regime: str
    circuit_breaker_active: bool
    panic_mode: bool
    ai_mode: bool
    timestamp: str


class RiskSettingsRequest(BaseModel):
    base_size: Optional[float] = None
    max_daily_trades: Optional[int] = None
    min_confidence: Optional[float] = None
    cooldown_between_trades_s: Optional[int] = None
    min_payout: Optional[int] = None


class AIModeRequest(BaseModel):
    enabled: bool


class TradeRecord(BaseModel):
    order_id: str
    venue: str
    asset: str
    direction: str
    size: float
    price: float
    payout: float
    status: str
    confidence: float
    regime: str
    timestamp: str


class TradesResponse(BaseModel):
    trades: List[TradeRecord]
    daily_pnl: float
    trade_count: int
    source: str


# ══════════════════════════════════════════════════════════════════════
#  ENDPOINT: POST /login
# ══════════════════════════════════════════════════════════════════════

@app.post("/login", response_model=TokenResponse, tags=["Auth"])
async def login(form: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    """
    Authenticate and receive a JWT token.
    Use this token as Bearer header for all subsequent requests.
    """
    if form.username != DASH_USER or form.password != DASH_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = _create_token(form.username)
    return TokenResponse(access_token=token)


# ══════════════════════════════════════════════════════════════════════
#  ENDPOINT: GET /state
# ══════════════════════════════════════════════════════════════════════

@app.get("/state", response_model=StateResponse, tags=["Telemetry"])
async def get_state(user: str = Depends(get_current_user)) -> StateResponse:
    """
    Read live pipeline state from Redis.
    Returns MACRO_REGIME, Circuit Breaker, Panic Mode, and AI Mode.
    """
    return StateResponse(
        macro_regime=_redis_safe_get(_RK_MACRO_REGIME, "GREEN"),
        circuit_breaker_active=_redis_safe_get(_RK_CIRCUIT_BREAKER, "0") == "1",
        panic_mode=_redis_safe_get(_RK_PANIC_MODE, "0") == "1",
        ai_mode=_redis_safe_get(_RK_AI_MODE, "0") == "1",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ══════════════════════════════════════════════════════════════════════
#  ENDPOINT: GET /trades
# ══════════════════════════════════════════════════════════════════════

@app.get("/trades", response_model=TradesResponse, tags=["Telemetry"])
async def get_trades(
    limit: int = 20,
    user: str = Depends(get_current_user),
) -> TradesResponse:
    """
    Query last N trades from QuestDB via its REST API (port 9000).
    Returns trades + daily PnL aggregation.
    Degrades gracefully if QuestDB is offline.
    """
    import urllib.request
    import urllib.parse
    import json

    query = (
        f"SELECT order_id, venue, asset, direction, size, price, payout, "
        f"status, confidence, regime, timestamp "
        f"FROM nexus_trades "
        f"ORDER BY timestamp DESC "
        f"LIMIT {min(limit, 100)}"
    )

    try:
        url = (
            f"http://{QUESTDB_REST_HOST}:{QUESTDB_REST_PORT}/exec"
            f"?query={urllib.parse.quote(query)}"
        )
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())

        columns = [col["name"] for col in data.get("columns", [])]
        rows = data.get("dataset", [])

        trades: List[TradeRecord] = []
        daily_pnl: float = 0.0
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for row in rows:
            record = dict(zip(columns, row))
            ts = str(record.get("timestamp", ""))

            trade = TradeRecord(
                order_id=str(record.get("order_id", "")),
                venue=str(record.get("venue", "")),
                asset=str(record.get("asset", "")),
                direction=str(record.get("direction", "")),
                size=float(record.get("size", 0.0)),
                price=float(record.get("price", 0.0)),
                payout=float(record.get("payout", 0.0)),
                status=str(record.get("status", "")),
                confidence=float(record.get("confidence", 0.0)),
                regime=str(record.get("regime", "")),
                timestamp=ts,
            )
            trades.append(trade)

            # Aggregate daily PnL
            if today_str in ts and trade.status == "FILLED":
                daily_pnl += trade.payout

        return TradesResponse(
            trades=trades,
            daily_pnl=round(daily_pnl, 2),
            trade_count=len(trades),
            source="questdb",
        )

    except Exception as exc:
        logger.warning(f"QuestDB offline or query failed: {exc}")
        return TradesResponse(
            trades=[],
            daily_pnl=0.0,
            trade_count=0,
            source="unavailable",
        )


# ══════════════════════════════════════════════════════════════════════
#  ENDPOINT: POST /settings/risk (Hot-Reload)
# ══════════════════════════════════════════════════════════════════════

@app.post("/settings/risk", tags=["Control Panel"])
async def update_risk_settings(
    settings: RiskSettingsRequest,
    user: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Write risk parameters to Redis for hot-reload by the pipeline.
    Only non-null fields are written. Pipeline reads these on every tick.
    """
    updated: Dict[str, Any] = {}

    if settings.base_size is not None:
        _redis_safe_set(f"{_RK_SETTINGS_PREFIX}base_size", str(settings.base_size))
        updated["base_size"] = settings.base_size

    if settings.max_daily_trades is not None:
        _redis_safe_set(f"{_RK_SETTINGS_PREFIX}max_daily_trades", str(settings.max_daily_trades))
        updated["max_daily_trades"] = settings.max_daily_trades

    if settings.min_confidence is not None:
        _redis_safe_set(f"{_RK_SETTINGS_PREFIX}min_confidence", str(settings.min_confidence))
        updated["min_confidence"] = settings.min_confidence

    if settings.cooldown_between_trades_s is not None:
        _redis_safe_set(
            f"{_RK_SETTINGS_PREFIX}cooldown_between_trades_s",
            str(settings.cooldown_between_trades_s),
        )
        updated["cooldown_between_trades_s"] = settings.cooldown_between_trades_s

    if settings.min_payout is not None:
        _redis_safe_set(f"{_RK_SETTINGS_PREFIX}min_payout", str(settings.min_payout))
        updated["min_payout"] = settings.min_payout

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No settings provided. Send at least one field.",
        )

    logger.info(f"Risk settings updated via OCP: {updated}")
    return {
        "status": "updated",
        "settings": updated,
        "user": user,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════
#  ENDPOINT: POST /settings/ai-mode
# ══════════════════════════════════════════════════════════════════════

@app.post("/settings/ai-mode", tags=["Control Panel"])
async def toggle_ai_mode(
    req: AIModeRequest,
    user: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Toggle AI Mode (Dynamic Tuning).
    When enabled, pipeline reads optimized params per regime from Redis.
    """
    value = "1" if req.enabled else "0"
    success = _redis_safe_set(_RK_AI_MODE, value)

    state = "ENABLED" if req.enabled else "DISABLED"
    logger.info(f"AI Mode {state} by user={user}")

    return {
        "status": "ok" if success else "redis_offline",
        "ai_mode": req.enabled,
        "user": user,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════
#  ENDPOINT: POST /panic (THE NUCLEAR BUTTON)
# ══════════════════════════════════════════════════════════════════════

@app.post("/panic", tags=["Control Panel"])
async def panic_halt(user: str = Depends(get_current_user)) -> Dict[str, Any]:
    """
    🔴 PANIC HALT — The Nuclear Button.
    Writes NEXUS:PANIC_MODE=1 to Redis.
    Pipeline reads this at the top of every tick and halts all trading.
    """
    success = _redis_safe_set(_RK_PANIC_MODE, "1")

    logger.critical(f"🚨 PANIC HALT activated by user={user}")

    # Fire Telegram alert (fire-and-forget)
    try:
        from nexus.reporting.telegram_reporter import TelegramReporter
        TelegramReporter.get_instance().fire_system_error(
            f"🚨 PANIC HALT activated via OCP by {user}", module="dashboard.panic"
        )
    except Exception:
        pass

    return {
        "status": "PANIC_ACTIVATED" if success else "REDIS_OFFLINE_PANIC_FAILED",
        "panic_mode": True,
        "user": user,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": "All trading halted. Reset via POST /panic/reset",
    }


@app.post("/panic/reset", tags=["Control Panel"])
async def panic_reset(user: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Reset PANIC_MODE — resume trading."""
    success = _redis_safe_set(_RK_PANIC_MODE, "0")
    logger.info(f"Panic mode RESET by user={user}")
    return {
        "status": "PANIC_CLEARED" if success else "REDIS_OFFLINE",
        "panic_mode": False,
        "user": user,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════
#  WEBSOCKET: /ws/telemetry (Live State Stream)
# ══════════════════════════════════════════════════════════════════════

@app.websocket("/ws/telemetry")
async def telemetry_ws(ws: WebSocket) -> None:
    """
    Live telemetry stream.
    Broadcasts pipeline state from Redis every 2 seconds.
    No auth required on WS (auth is handled by the SPA before connecting).
    """
    await ws.accept()
    logger.info("WebSocket telemetry client connected")

    try:
        while True:
            state = {
                "macro_regime": _redis_safe_get(_RK_MACRO_REGIME, "GREEN"),
                "circuit_breaker": _redis_safe_get(_RK_CIRCUIT_BREAKER, "0") == "1",
                "panic_mode": _redis_safe_get(_RK_PANIC_MODE, "0") == "1",
                "ai_mode": _redis_safe_get(_RK_AI_MODE, "0") == "1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await ws.send_json(state)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("WebSocket telemetry client disconnected")
    except Exception as exc:
        logger.warning(f"WebSocket error: {exc}")


# ══════════════════════════════════════════════════════════════════════
#  Health Check (No Auth)
# ══════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["System"])
async def health() -> Dict[str, Any]:
    """Unauthenticated health check for load balancers / Docker."""
    redis_ok = False
    try:
        _get_redis().ping()
        redis_ok = True
    except Exception:
        pass

    return {
        "status": "ok",
        "redis": "connected" if redis_ok else "offline",
        "version": "4.0.0",
        "uptime_s": round(time.time() - _BOOT_TIME, 1),
    }


_BOOT_TIME = time.time()


# ══════════════════════════════════════════════════════════════════════
#  Local Development Runner
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn  # type: ignore

    uvicorn.run(
        "nexus.dashboard.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
