"""
NEXUS Trading System — Bitget Data Handler
==========================================
Fuente de datos primaria: Bitget USDT-M Futures (LIVE y DEMO)

Arquitectura:
  BitgetDataHandler       → Handler principal, lifecycle, API
  ├── DataStore           → Persistencia SQLite (reutilizado de v1)
  ├── DataCleaner         → Normalización OHLCV (mejorado de v1)
  ├── _ws_kline_loop()    → WebSocket Bitget con reconexión
  └── _alt_data_loop()    → Polling REST funding + orderbook

Modo demo:
  BITGET_DEMO_MODE=true  → productType=SUSDT-FUTURES
  BITGET_DEMO_MODE=false → productType=USDT-FUTURES
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import defaultdict, deque

import numpy as np  # type: ignore
import pandas as pd  # type: ignore
from sqlalchemy import (  # type: ignore
    Column,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker  # type: ignore
from sqlalchemy.dialects.sqlite import insert as sqlite_insert # type: ignore

import websockets  # type: ignore
import aiohttp     # type: ignore
import json

from nexus.config.settings import (  # type: ignore
    BITGET_API_KEY,
    BITGET_API_SECRET,
    BITGET_API_PASSPHRASE,
    BITGET_DEMO_MODE,
    trading_config,
)

logger = logging.getLogger("nexus.data_handler")

# ──────────────────────────────────────────────────────────────────────
#  Constantes
# ──────────────────────────────────────────────────────────────────────

BITGET_WS_PUBLIC = "wss://ws.bitget.com/v2/ws/public"
BITGET_REST_BASE = "https://api.bitget.com"

PRODUCT_TYPE_LIVE = "USDT-FUTURES"
PRODUCT_TYPE_DEMO = "SUSDT-FUTURES"

# Mapa de timeframes NEXUS → Bitget granularity
_TF_MAP: Dict[str, str] = {
    "1m":  "1m",
    "3m":  "3m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1H",
    "2h":  "2H",
    "4h":  "4H",
    "6h":  "6H",
    "12h": "12H",
    "1d":  "1Dutc",
    "1w":  "1Wutc",
}

# ──────────────────────────────────────────────────────────────────────
#  DataStore — SQLite persistence (SQLAlchemy Model)
# ──────────────────────────────────────────────────────────────────────

Base = declarative_base()

class KlineRecord(Base):
    """Modelo SQLAlchemy para almacenar velas OHLCV (Bitget)."""

    __tablename__ = "klines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    interval = Column(String(5), nullable=False, index=True)
    open_time = Column(DateTime, nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    close_time = Column(DateTime, nullable=False)
    quote_volume = Column(Float, nullable=False)
    num_trades = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("symbol", "interval", "open_time", name="uq_kline"),
    )

class DataStore:
    """Almacenamiento persistente de datos de mercado con SQLAlchemy + SQLite."""

    def __init__(self, db_path: str = "nexus_data.db") -> None:
        self.db_path = db_path
        self._engine = None
        self._SessionFactory = None

    def initialize(self) -> None:
        """Crea engine, session factory y tablas si no existen."""
        self._engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self._engine)
        self._SessionFactory = sessionmaker(bind=self._engine)
        logger.info("DataStore inicializado → %s", self.db_path)

    def _session(self) -> Session:
        return self._SessionFactory()  # type: ignore

    def save_klines(self, symbol: str, interval: str, df: pd.DataFrame) -> int:
        """Persiste un DataFrame de velas usando INSERT OR IGNORE."""
        if df.empty:
            return 0

        session = self._session()
        inserted = 0
        try:
            for _, row in df.iterrows():
                row_dict = {
                    "symbol": symbol,
                    "interval": interval,
                    "open_time": row["open_time"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "close_time": row["close_time"],
                    "quote_volume": float(row.get("quote_volume", 0)),
                    "num_trades": int(row.get("num_trades", 0))
                }
                stmt = sqlite_insert(KlineRecord).values(row_dict)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["symbol", "interval", "open_time"]
                )
                session.execute(stmt)
                inserted += 1
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        logger.debug("Guardadas %d velas [%s %s]", inserted, symbol, interval)
        return inserted

    def load_klines(
        self,
        symbol: str,
        interval: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Carga velas históricas filtradas por rango de fechas."""
        session = self._session()
        try:
            query = session.query(KlineRecord).filter_by(
                symbol=symbol, interval=interval
            )
            if start:
                query = query.filter(KlineRecord.open_time >= start)
            if end:
                query = query.filter(KlineRecord.open_time <= end)
            query = query.order_by(KlineRecord.open_time)

            records = query.all()
            if not records:
                return pd.DataFrame()

            data = [
                {
                    "open_time": r.open_time,
                    "open": r.open,
                    "high": r.high,
                    "low": r.low,
                    "close": r.close,
                    "volume": r.volume,
                    "close_time": r.close_time,
                    "quote_volume": r.quote_volume,
                    "num_trades": r.num_trades,
                }
                for r in records
            ]
            df = pd.DataFrame(data)
            if not df.empty:
                df["open_time"] = pd.to_datetime(df["open_time"]).dt.tz_localize("UTC")
                df["close_time"] = pd.to_datetime(df["close_time"]).dt.tz_localize("UTC")
            return df
        finally:
            session.close()

    def get_latest_timestamp(self, symbol: str, interval: str) -> Optional[datetime]:
        """Retorna el open_time más reciente almacenado para un par/intervalo."""
        session = self._session()
        try:
            record = (
                session.query(KlineRecord)
                .filter_by(symbol=symbol, interval=interval)
                .order_by(KlineRecord.open_time.desc())
                .first()
            )
            return record.open_time if record else None
        finally:
            session.close()

# ──────────────────────────────────────────────────────────────────────
#  DataCleaner — limpieza y normalización
# ──────────────────────────────────────────────────────────────────────

class DataCleaner:
    """Limpieza, normalización y detección de outliers para datos OHLCV."""

    @staticmethod
    def clean_kline_row(raw: List[Any]) -> Dict[str, Any]:
        """
        Calcula y limpia una vela cruda tal como llega de Bitget.
        Formato de lista Bitget: [ts(str), open, high, low, close, baseVolume, quoteVolume]
        """
        ts = int(raw[0])
        return {
            "open_time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
            "open": float(raw[1]),
            "high": float(raw[2]),
            "low": float(raw[3]),
            "close": float(raw[4]),
            "volume": float(raw[5]),
            "close_time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc), # aproximado en Bitget
            "quote_volume": float(raw[6]) if len(raw) > 6 else 0.0,
            "num_trades": 0,
            "ts_ms": ts, # Usado internamente para trackear cambios
        }

    @staticmethod
    def detect_outliers(
        df: pd.DataFrame,
        column: str = "close",
        z_threshold: float = 3.0,
    ) -> pd.DataFrame:
        """Marca outliers basándose en z-score rolling."""
        if df.empty or column not in df.columns:
            return df

        df = df.copy()
        
        # FIX: Rolling z-score evita penalizar el precio de mercado con tendencia
        df["rolling_mean"] = df[column].rolling(window=100, min_periods=20).mean()
        df["rolling_std"] = df[column].rolling(window=100, min_periods=20).std()
        
        # Evitar divisiones por cero o NaNs
        df["rolling_std"] = df["rolling_std"].replace(0, np.nan)
        
        df["z_score"] = (df[column] - df["rolling_mean"]) / df["rolling_std"]
        df["is_outlier"] = df["z_score"].abs() > z_threshold
        df = df.drop(columns=["rolling_mean", "rolling_std"])
        
        outlier_count = df["is_outlier"].sum()
        if outlier_count > 0:
            logger.warning(
                "Detectados %d outliers in '%s'",
                int(outlier_count),
                column
            )
        return df

    @staticmethod
    def fill_gaps(df: pd.DataFrame, freq: str = "1min", max_fill_periods: int = 10) -> pd.DataFrame:
        """Rellena huecos temporales con forward-fill hasta un límite de periodos."""
        if df.empty:
            return df
        df = df.copy()
        df = df.set_index("open_time")
        df = df.asfreq(freq)
        
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].ffill(limit=max_fill_periods)
        df["volume"] = df["volume"].fillna(0)
        df = df.reset_index()
        
        if df[["close"]].isna().sum().sum() > 0:
            logger.critical("Gap mayor a %d periodos detectado en data", max_fill_periods)
            
        return df

    @staticmethod
    def clean_data(df: pd.DataFrame) -> pd.DataFrame:
        """Pipeline completo de limpieza."""
        if df.empty:
            return df

        df = df.copy()

        numeric_cols = ["open", "high", "low", "close", "volume"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        before = len(df)
        df = df.dropna(subset=numeric_cols)
        dropped = before - len(df)
        if dropped:
            logger.info("Eliminadas %d filas con NaN", dropped)

        mask_invalid = (df["high"] < df["low"]) | (df["volume"] < 0)
        invalid_count = mask_invalid.sum()
        if invalid_count:
            logger.warning("Eliminadas %d filas con OHLC incoherente", invalid_count)
            df = df[~mask_invalid]

        df = DataCleaner.detect_outliers(df, column="close")

        if "open_time" in df.columns:
            df = df.sort_values("open_time").reset_index(drop=True)

        return df

# ──────────────────────────────────────────────────────────────────────
#  BitgetDataHandler
# ──────────────────────────────────────────────────────────────────────

class BitgetDataHandler:
    """
    Handler principal de datos de Bitget Futures.
    
    Fuente de datos primaria del sistema NEXUS v5.0.
    Soporta modo LIVE (USDT-FUTURES) y DEMO (SUSDT-FUTURES).
    
    IMPORTANTE sobre modo demo:
    - En SUSDT-FUTURES los símbolos tienen prefijo S: SBTCUSDT
    - Los datos son reales de mercado, solo el balance es simulado
    - Para datos históricos REST, el productType cambia en la URL
    
    Uso:
        handler = BitgetDataHandler()
        await handler.start()
        df = handler.get_realtime_klines("BTCUSDT", "1m")
        df = await handler.get_historical_data("BTCUSDT", "1h")
        await handler.stop()
    """

    WS_URL = BITGET_WS_PUBLIC
    MAX_RECONNECT_ATTEMPTS = 10
    RECONNECT_BASE_DELAY = 2  # segundos, backoff exponencial
    BUFFER_MAXLEN = 1500      # velas por par/timeframe en memoria

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        db_path: str = "nexus_data.db",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        demo_mode: Optional[bool] = None,
    ) -> None:
        self.symbols = symbols or ["BTCUSDT"]
        self.timeframes = timeframes or ["1m", "5m", "1h", "4h"]
        self.api_key = api_key or BITGET_API_KEY
        self.api_secret = api_secret or BITGET_API_SECRET
        self.api_passphrase = api_passphrase or BITGET_API_PASSPHRASE
        
        self.demo_mode = demo_mode if demo_mode is not None else BITGET_DEMO_MODE
        self.product_type = PRODUCT_TYPE_DEMO if self.demo_mode else PRODUCT_TYPE_LIVE

        self.store = DataStore(db_path=db_path)
        self.cleaner = DataCleaner()

        self._realtime_buffers: Dict[Tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=self.BUFFER_MAXLEN))
        self._orderbook_data: Dict[str, Dict[str, Any]] = defaultdict(dict)
        self._funding_rates: Dict[str, float] = defaultdict(float)

        self._ws_tasks: List[asyncio.Task] = []
        self._running = False
        self._subscribers: List[Callable] = []
        self._session: Optional[aiohttp.ClientSession] = None
        
        num_symbols = len(self.symbols)
        self._alt_data_interval = max(3.0, num_symbols * 0.15)

    def _resolve_symbol(self, symbol: str) -> str:
        """
        Resuelve el símbolo correcto según el modo de operación.
        LIVE: BTCUSDT  →  BTCUSDT
        DEMO: BTCUSDT  →  SBTCUSDT (prefijo S requerido por Bitget)
        """
        symbol_upper = symbol.upper()
        if self.demo_mode and not symbol_upper.startswith("S"):
            return f"S{symbol_upper}"
        return symbol_upper

    async def start(self) -> None:
        """Inicializa DB, crea session aiohttp, lanza WebSocket tasks."""
        logger.info("Iniciando BitgetDataHandler (product_type=%s)", self.product_type)
        self.store.initialize()
        self._session = aiohttp.ClientSession()
        self._running = True

        for base_sym in self.symbols:
            sym = self._resolve_symbol(base_sym)
            for tf in self.timeframes:
                task = asyncio.create_task(
                    self._ws_kline_loop(sym, tf),
                    name=f"ws_{sym}_{tf}"
                )
                self._ws_tasks.append(task)

        alt_data_task = asyncio.create_task(
            self._alt_data_loop(),
            name="alt_data_polling"
        )
        self._ws_tasks.append(alt_data_task)

        logger.info(
            "WebSocket activo para %d streams (%s × %s)",
            len(self._ws_tasks) - 1,
            self.symbols,
            self.timeframes,
        )

    async def stop(self) -> None:
        """Cancela tasks, cierra aiohttp session, cierra DB engine."""
        logger.info("Deteniendo BitgetDataHandler...")
        self._running = False

        for task in self._ws_tasks:
            task.cancel()
        await asyncio.gather(*self._ws_tasks, return_exceptions=True)
        self._ws_tasks.clear()

        if self._session:
            await self._session.close()
            self._session = None

        logger.info("BitgetDataHandler detenido.")

    async def _ws_kline_loop(self, symbol: str, interval: str) -> None:
        """
        Loop de reconexión para stream de klines via WebSocket Bitget.
        
        REGLA CRÍTICA anti-lookahead:
        Bitget envía updates de la vela abierta con cada tick.
        La vela se considera CERRADA cuando llega un nuevo timestamp
        (el ts_ms cambia respecto a la última vela del buffer).
        NUNCA usar la última vela del buffer en signal_engine —
        siempre usar df.iloc[:-1] para excluir la vela abierta.
        """
        attempt = 0
        bg_interval = _TF_MAP.get(interval, "1m")
        channel = f"candle{bg_interval}"

        subscribe_msg = {
            "op": "subscribe",
            "args": [{
                "instType": self.product_type,
                "channel": channel,
                "instId": symbol
            }]
        }

        while self._running:
            try:
                attempt += 1
                async with websockets.connect(self.WS_URL, ping_interval=30) as ws:
                    attempt = 0
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info("✅ WebSocket conectado: %s %s", symbol, interval)
                    
                    while self._running:
                        msg_str = await asyncio.wait_for(ws.recv(), timeout=65)
                        if msg_str == "pong":
                            continue
                        
                        msg = json.loads(msg_str)
                        if msg.get("event") == "error":
                            logger.error("Error en stream: %s", msg)
                            break
                            
                        if "action" in msg and msg["action"] in ["snapshot", "update"]:
                            await self._handle_ws_message(msg, symbol, interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.error("Error en WebSocket %s %s: %s — reconectando...", symbol, interval, e)
                if attempt > self.MAX_RECONNECT_ATTEMPTS:
                    logger.critical("❌ Máximo de reconexiones alcanzado para %s", symbol)
                    break
                delay = min(self.RECONNECT_BASE_DELAY ** attempt, 300)
                await asyncio.sleep(delay)

    async def _handle_ws_message(self, msg: dict, symbol: str, interval: str) -> None:
        """
        Procesa mensaje WS, actualiza buffer, persiste velas cerradas y 
        dispara suscripciones en base al avance de timestamps.
        """
        data = msg.get("data", [])
        if not data:
            return

        key = (symbol, interval)
        buf = self._realtime_buffers[key]

        for raw_kline in data:
            cleaned = self.cleaner.clean_kline_row(raw_kline)
            ts_ms = cleaned.pop("ts_ms")
            
            # Anti-lookahead & close state detection
            if buf:
                last_kline = buf[-1]
                last_ts = int(last_kline["open_time"].timestamp() * 1000)
                
                if ts_ms > last_ts:
                    # New candle arrived, previous is closed
                    closed_kline = last_kline
                    new_row_df = pd.DataFrame([closed_kline])
                    self.store.save_klines(symbol, interval, new_row_df)
                    
                    for cb in self._subscribers:
                        try:
                            cb(symbol, interval, closed_kline)
                        except Exception as e:
                            logger.error("Error en subscriber: %s", e)
                            
            if buf and int(buf[-1]["open_time"].timestamp() * 1000) == ts_ms:
                buf[-1] = cleaned
            else:
                buf.append(cleaned)

    async def _alt_data_loop(self) -> None:
        """
        Polling REST de datos alternativos (Order book + Funding rates).
        """
        while self._running:
            try:
                for base_sym in self.symbols:
                    sym = self._resolve_symbol(base_sym)
                    if not self._session:
                        break

                    # 1. Funding rate
                    url_fund = f"{BITGET_REST_BASE}/api/v2/mix/market/current-fund-rate?symbol={sym}&productType={self.product_type}"
                    async with self._session.get(url_fund) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("code") == "00000" and data.get("data"):
                                f_rate = float(data["data"][0].get("fundingRate", 0.0))
                                self._funding_rates[sym] = f_rate

                    # 2. Order book merge depth
                    url_depth = f"{BITGET_REST_BASE}/api/v2/mix/market/merge-depth?symbol={sym}&productType={self.product_type}&limit=1"
                    async with self._session.get(url_depth) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("code") == "00000" and data.get("data"):
                                d = data["data"][0]
                                bid_qty = float(d["bids"][0][1]) if d.get("bids") else 0.0
                                bid_px = float(d["bids"][0][0]) if d.get("bids") else 0.0
                                ask_qty = float(d["asks"][0][1]) if d.get("asks") else 0.0
                                ask_px = float(d["asks"][0][0]) if d.get("asks") else 0.0
                                total_qty = bid_qty + ask_qty
                                imb = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0
                                self._orderbook_data[sym] = {
                                    "bidPrice": bid_px,
                                    "bidQty": bid_qty,
                                    "askPrice": ask_px,
                                    "askQty": ask_qty,
                                    "imbalance": round(imb, 4)
                                }
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Error polleando Alt-Data: %s", exc)

            await asyncio.sleep(self._alt_data_interval)

    async def get_historical_data(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit_per_request: int = 200,
    ) -> pd.DataFrame:
        """
        Descarga klines históricas via REST paginado desde Bitget v2.
        PAGINACIÓN móvil y resiliencia ante rate limits.
        """
        if self._session is None:
            self._session = aiohttp.ClientSession()

        sym = self._resolve_symbol(symbol)
        granularity = _TF_MAP.get(interval, "1H")

        end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        if start_date:
            try:
                dt = pd.to_datetime(start_date, utc=True)
                start_ts = int(dt.timestamp() * 1000)
            except Exception:
                start_ts = end_ts - (365 * 24 * 60 * 60 * 1000)
        else:
            start_ts = end_ts - (365 * 24 * 60 * 60 * 1000)

        current_end_ts = end_ts
        url = f"{BITGET_REST_BASE}/api/v2/mix/market/history-candles"
        
        all_klines = []
        logger.info("Descargando históricos %s %s...", sym, interval)

        while current_end_ts > start_ts:
            params = {
                "symbol": sym,
                "granularity": granularity,
                "productType": self.product_type,
                "limit": str(limit_per_request),
                "startTime": str(start_ts),
                "endTime": str(current_end_ts)
            }
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("code") == "00000" and data.get("data"):
                            page_klines = data["data"]
                            if not page_klines:
                                break
                            all_klines.extend(page_klines)
                            # Descendente: page_klines[-1] es el más viejo recibido
                            earliest_ts = int(page_klines[-1][0])
                            next_end = earliest_ts - 1
                            if next_end >= current_end_ts:
                                # Previene loop infinito
                                break
                            current_end_ts = next_end
                        else:
                            break
                    elif resp.status == 429:
                        await asyncio.sleep(2.0)
                        continue
                    else:
                        break
            except Exception as e:
                logger.error("Error fetching historical history: %s", e)
                break
                
            await asyncio.sleep(0.1)

        if not all_klines:
            return pd.DataFrame()

        # Invertir para ascendente
        all_klines = list(reversed(all_klines))

        df_data = []
        for k in all_klines:
            d = self.cleaner.clean_kline_row(k)
            d.pop("ts_ms", None)
            df_data.append(d)

        df = pd.DataFrame(df_data)
        df = self.cleaner.clean_data(df)
        
        saved = self.store.save_klines(sym, interval, df)
        logger.info(
            "Descargados %d klines (%d guardados) [%s %s]",
            len(df),
            saved,
            sym,
            interval,
        )
        return df

    def get_realtime_klines(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1m",
        exclude_open_candle: bool = True,
    ) -> pd.DataFrame:
        """
        Retorna DataFrame OHLCV del buffer en memoria.
        
        PARÁMETRO CRÍTICO exclude_open_candle=True (default):
        Si True → retorna df.iloc[:-1] (excluye la última vela, que
        puede estar abierta y tiene close provisional).
        
        Documentar explícitamente: llamar con exclude_open_candle=False
        solo si se necesita el precio actual del tick (para dashboard
        de monitoreo), NUNCA para cálculo de señales o indicadores.
        """
        sym = self._resolve_symbol(symbol)
        key = (sym, interval)
        buf = self._realtime_buffers.get(key)

        if not buf:
            return pd.DataFrame(
                columns=[
                    "open_time", "open", "high", "low", "close",
                    "volume", "close_time", "quote_volume", "num_trades",
                ]
            )

        df = pd.DataFrame(list(buf))
        
        if "ts_ms" in df.columns:
            df = df.drop(columns=["ts_ms"])
            
        df = self.cleaner.clean_data(df)
        
        if exclude_open_candle and not df.empty and len(df) > 1:
            df = df.iloc[:-1]
            
        return df

    def get_alternative_data(self, symbol: str) -> Dict[str, Any]:
        """Retorna funding rate e imbalance actuales."""
        sym = self._resolve_symbol(symbol)
        return {
            "orderbook": self._orderbook_data.get(sym, {}),
            "funding_rate": self._funding_rates.get(sym, 0.0)
        }

    def subscribe(self, callback: Callable) -> None:
        """Sistema de suscripción para velas cerradas."""
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_streams(self) -> int:
        return len(self._ws_tasks)

    def buffer_sizes(self) -> Dict[str, int]:
        return {f"{k[0]}_{k[1]}": len(v) for k, v in self._realtime_buffers.items()}

    def __repr__(self) -> str:
        return (
            f"<BitgetDataHandler(demo={self.demo_mode}, "
            f"product={self.product_type}, active_streams={self.active_streams})>"
        )

# ──────────────────────────────────────────────────────────────────────
#  DataHandlerFactory
# ──────────────────────────────────────────────────────────────────────

def create_data_handler(
    venue: str = "bitget",
    **kwargs
) -> BitgetDataHandler:
    """
    Factory para crear el handler de datos correcto según venue.
    
    Por ahora solo soporta Bitget. Estructura preparada para
    añadir ForexDataHandler (OANDA) en fase futura sin romper
    nada en pipeline.py.
    
    Args:
        venue: "bitget" (único soportado actualmente)
        **kwargs: parámetros para BitgetDataHandler
    
    Returns:
        Handler configurado para el venue solicitado
    
    Raises:
        ValueError: si venue no está soportado
    """
    if venue == "bitget":
        return BitgetDataHandler(**kwargs)
    raise ValueError(
        f"Venue '{venue}' no soportado. Venues disponibles: ['bitget']"
    )
