"""
NEXUS v4.0 — Operational Control Panel (OCP) Backend
======================================================
Pure async REST API + WebSocket telemetry server.

NO HTML rendering. NO Jinja2. NO static files.
This is a headless API designed to feed the NEXUS Commander SPA.

Architecture:
    Auth:       JWT (HS256) via POST /login
    State:      Redis (MACRO_REGIME, CIRCUIT_BREAKER, PANIC_MODE, AI_MODE)
    Trades:     QuestDB (PG wire on port 8812)
    Control:    Hot-Reload risk params + Kill Switch via Redis writes
    Telemetry:  WebSocket /ws/telemetry   (live state broadcast)
    Prices:     WebSocket /ws/prices      (OHLCV tick stream)
    Logs:       WebSocket /ws/logs        (live log tail)

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
import math
import os
import random
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
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
_RK_EXECUTION_VENUE = "NEXUS:EXECUTION_VENUE"
_RK_ACCOUNT_TYPE = "NEXUS:ACCOUNT_TYPE"

# Log file path (shared with main.py)
_LOG_FILE = Path("logs/nexus_session.log")


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
    execution_venue: str
    account_type: str
    timestamp: str


class RiskSettingsRequest(BaseModel):
    base_size: Optional[float] = None
    max_daily_trades: Optional[int] = None
    min_confidence: Optional[float] = None
    cooldown_between_trades_s: Optional[int] = None
    min_payout: Optional[int] = None


class AIModeRequest(BaseModel):
    enabled: bool


class VenueRequest(BaseModel):
    venue: str  # "IQ_OPTION" | "BINANCE"


class AccountTypeRequest(BaseModel):
    account_type: str  # "PRACTICE" | "REAL"
    confirm: bool = False  # Double-confirm guard for REAL


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
#  ENDPOINT: GET /
# ══════════════════════════════════════════════════════════════════════

@app.get("/", tags=["System"])
async def read_root() -> Dict[str, Any]:
    """Root endpoint providing basic API information."""
    return {
        "app": "NEXUS v4.0 Operational Control Panel (OCP)",
        "status": "online",
        "message": "Headless API. Connect via NEXUS Commander SPA or visit /docs.",
        "docs_url": "/docs",
        "health_check": "/health",
    }


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
    Returns MACRO_REGIME, Circuit Breaker, Panic Mode, AI Mode, Venue, Account.
    """
    return StateResponse(
        macro_regime=_redis_safe_get(_RK_MACRO_REGIME, "GREEN"),
        circuit_breaker_active=_redis_safe_get(_RK_CIRCUIT_BREAKER, "0") == "1",
        panic_mode=_redis_safe_get(_RK_PANIC_MODE, "0") == "1",
        ai_mode=_redis_safe_get(_RK_AI_MODE, "0") == "1",
        execution_venue=_redis_safe_get(_RK_EXECUTION_VENUE, os.getenv("EXECUTION_VENUE", "IQ_OPTION")),
        account_type=_redis_safe_get(_RK_ACCOUNT_TYPE, os.getenv("IQ_OPTION_ACCOUNT_TYPE", "PRACTICE")),
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

    except (ConnectionRefusedError, OSError) as exc:
        logger.debug(f"QuestDB not ready (startup race): {exc}")
        return TradesResponse(
            trades=[],
            daily_pnl=0.0,
            trade_count=0,
            source="unavailable",
        )
    except Exception as exc:
        logger.debug(f"QuestDB offline or query failed: {exc}")
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
#  ENDPOINT: POST /settings/venue
# ══════════════════════════════════════════════════════════════════════

@app.post("/settings/venue", tags=["Control Panel"])
async def switch_venue(
    req: VenueRequest,
    user: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Switch execution venue between IQ_OPTION and BINANCE.
    Pipeline reads NEXUS:EXECUTION_VENUE from Redis on every tick.
    """
    venue = req.venue.upper().strip()
    if venue not in ("IQ_OPTION", "BINANCE"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid venue '{venue}'. Must be 'IQ_OPTION' or 'BINANCE'.",
        )

    success = _redis_safe_set(_RK_EXECUTION_VENUE, venue)
    logger.info(f"Execution venue changed to {venue} by user={user}")

    return {
        "status": "ok" if success else "redis_offline",
        "execution_venue": venue,
        "user": user,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════
#  ENDPOINT: POST /settings/account-type
# ══════════════════════════════════════════════════════════════════════

@app.post("/settings/account-type", tags=["Control Panel"])
async def switch_account_type(
    req: AccountTypeRequest,
    user: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Switch account type between PRACTICE (demo) and REAL.

    ⚠️ CRITICAL: Switching to REAL requires `confirm: true` in the request body.
    This is a safety guard — real money is at stake.
    """
    acct = req.account_type.upper().strip()
    if acct not in ("PRACTICE", "REAL"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid account type '{acct}'. Must be 'PRACTICE' or 'REAL'.",
        )

    # Double-confirm guard for REAL trading
    if acct == "REAL" and not req.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Switching to REAL requires 'confirm: true'. This will use real money.",
        )

    success = _redis_safe_set(_RK_ACCOUNT_TYPE, acct)
    logger.info(f"Account type changed to {acct} by user={user}")

    # Telegram alert for REAL mode activation
    if acct == "REAL":
        try:
            from nexus.reporting.telegram_reporter import TelegramReporter
            TelegramReporter.get_instance().fire_system_error(
                f"⚠️ REAL MONEY trading activated via OCP by {user}",
                module="dashboard.account",
            )
        except Exception:
            pass

    return {
        "status": "ok" if success else "redis_offline",
        "account_type": acct,
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
                "type": "telemetry",
                "macro_regime": _redis_safe_get(_RK_MACRO_REGIME, "GREEN"),
                "circuit_breaker": _redis_safe_get(_RK_CIRCUIT_BREAKER, "0") == "1",
                "panic_mode": _redis_safe_get(_RK_PANIC_MODE, "0") == "1",
                "ai_mode": _redis_safe_get(_RK_AI_MODE, "0") == "1",
                "execution_venue": _redis_safe_get(
                    _RK_EXECUTION_VENUE, os.getenv("EXECUTION_VENUE", "IQ_OPTION")
                ),
                "account_type": _redis_safe_get(
                    _RK_ACCOUNT_TYPE, os.getenv("IQ_OPTION_ACCOUNT_TYPE", "PRACTICE")
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await ws.send_json(state)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("WebSocket telemetry client disconnected")
    except Exception as exc:
        logger.warning(f"WebSocket error: {exc}")


# ══════════════════════════════════════════════════════════════════════
#  WEBSOCKET: /ws/prices (OHLCV Tick Stream)
# ══════════════════════════════════════════════════════════════════════

# In-memory price simulator for dev/demo mode.
# In production, pipeline publishes real ticks to Redis channel NEXUS:TICKS.
_PRICE_STATE: Dict[str, float] = {
    "price": 1.08500,  # EUR/USD starting price
    "volume": 0.0,
}

# Monotonically increasing time counter for lightweight-charts
# (each candle MUST have a strictly increasing `time` value)
_TICK_COUNTER: Dict[str, int] = {"last_time": 0}


def _generate_tick() -> Dict[str, Any]:
    """Generate a realistic OHLCV tick for demo/dev mode."""
    p = _PRICE_STATE["price"]
    # Brownian-motion step with slight mean reversion
    delta = random.gauss(0, 0.00015) - (p - 1.08500) * 0.001
    new_price = round(p + delta, 5)
    _PRICE_STATE["price"] = new_price

    # Simulate OHLCV candle
    high = round(new_price + abs(random.gauss(0, 0.00008)), 5)
    low = round(new_price - abs(random.gauss(0, 0.00008)), 5)
    vol = round(random.uniform(50, 500), 2)

    # Ensure strictly increasing time (lightweight-charts requirement)
    now_s = int(time.time())
    if now_s <= _TICK_COUNTER["last_time"]:
        now_s = _TICK_COUNTER["last_time"] + 1
    _TICK_COUNTER["last_time"] = now_s

    return {
        "type": "price",
        "asset": "EURUSD",
        "open": p,
        "high": high,
        "low": low,
        "close": new_price,
        "volume": vol,
        "time": now_s,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _try_redis_pubsub() -> Optional[Any]:
    """Try to subscribe to real-time ticks from the pipeline via Redis Pub/Sub."""
    try:
        r = _get_redis()
        r.ping()
        ps = r.pubsub()
        ps.subscribe("NEXUS:TICKS")
        logger.info("📡 /ws/prices connected to Redis Pub/Sub channel NEXUS:TICKS")
        return ps
    except Exception:
        logger.debug("/ws/prices falling back to simulator (Redis Pub/Sub unavailable)")
        return None


@app.websocket("/ws/prices")
async def prices_ws(ws: WebSocket) -> None:
    """
    Live OHLCV price stream.
    Priority: Redis Pub/Sub (real ticks from pipeline) → Brownian simulator.
    Emits a tick every 1s for lightweight-charts integration.
    """
    await ws.accept()
    logger.info("WebSocket prices client connected")

    # Attempt Redis Pub/Sub for real data
    pubsub = await asyncio.to_thread(_try_redis_pubsub)

    try:
        while True:
            tick = None

            # Try real data from Redis first
            if pubsub:
                try:
                    msg = await asyncio.to_thread(pubsub.get_message, True, 0.1)
                    if msg and msg["type"] == "message":
                        import json as _json
                        raw = msg["data"]
                        data = _json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                        # Ensure required fields + monotonic time
                        now_s = int(time.time())
                        if now_s <= _TICK_COUNTER["last_time"]:
                            now_s = _TICK_COUNTER["last_time"] + 1
                        _TICK_COUNTER["last_time"] = now_s
                        tick = {
                            "type": "price",
                            "asset": data.get("asset", "EURUSD"),
                            "open": float(data.get("open", 0)),
                            "high": float(data.get("high", 0)),
                            "low": float(data.get("low", 0)),
                            "close": float(data.get("close", 0)),
                            "volume": float(data.get("volume", 0)),
                            "time": now_s,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                except Exception:
                    pass  # Degrade to simulator

            # Fallback: simulator
            if tick is None:
                tick = _generate_tick()

            await ws.send_json(tick)
            logger.debug(
                f"TICK → {tick['asset']} close={tick['close']} time={tick['time']}"
            )
            await asyncio.sleep(1)  # 1 tick/second

    except WebSocketDisconnect:
        logger.info("WebSocket prices client disconnected")
    except Exception as exc:
        logger.warning(f"WebSocket prices error: {exc}")
    finally:
        if pubsub:
            try:
                await asyncio.to_thread(pubsub.unsubscribe)
                await asyncio.to_thread(pubsub.close)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════
#  WEBSOCKET: /ws/logs (Live Log Tail)
# ══════════════════════════════════════════════════════════════════════

@app.websocket("/ws/logs")
async def logs_ws(ws: WebSocket) -> None:
    """
    Live log tail stream.
    Reads the last N lines of nexus_session.log and streams new lines.
    Similar to `tail -f` but over WebSocket.
    """
    await ws.accept()
    logger.info("WebSocket logs client connected")

    last_pos: int = 0

    try:
        # Send initial burst: last 50 lines
        if _LOG_FILE.exists():
            with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                initial_lines = lines[-50:] if len(lines) > 50 else lines
                for line in initial_lines:
                    await ws.send_json({
                        "type": "log",
                        "line": line.rstrip("\n"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                last_pos = f.tell()

        # Continuous tail
        while True:
            if _LOG_FILE.exists():
                with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_pos)
                    new_lines = f.readlines()
                    last_pos = f.tell()

                    for line in new_lines:
                        stripped = line.rstrip("\n")
                        if stripped:
                            await ws.send_json({
                                "type": "log",
                                "line": stripped,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })

            await asyncio.sleep(1)  # Poll every 1s
    except WebSocketDisconnect:
        logger.info("WebSocket logs client disconnected")
    except Exception as exc:
        logger.warning(f"WebSocket logs error: {exc}")


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
