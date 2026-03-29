"""
NEXUS v4.0 — QuestDB Client (Layer 1: Data Lake)
=================================================
Cliente asíncrono completo para QuestDB.

Protocolos:
    - InfluxDB Line Protocol (ILP, port 9009): Escritura de ticks en bulk
      con throughput máximo (~millions rows/sec). Sin overhead SQL.
    - PostgreSQL wire protocol (asyncpg, port 8812): Lectura/Queries
      para backtest, reportes y OHLCV agregados.

Tablas Time-Series:
    - `ticks`:  Tick-level data (asset, bid, ask, volume, timestamp)
    - `candles`: Aggregated OHLCV (asset, tf, open, high, low, close, volume)
    - `trades`: Trade execution log (order_id, venue, asset, direction, ...)

Uso:
    client = QuestDBClient()
    await client.connect()
    await client.create_tables()
    await client.ingest_tick("EURUSD", bid=1.0845, ask=1.0847, volume=100)
    df = await client.query_ohlcv("EURUSD", "1m", bars=500)
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv

logger = logging.getLogger("nexus.datalake")


class QuestDBClient:
    """
    Cliente asíncrono institucional para QuestDB.

    Dual-protocol:
        - ILP socket para ingesta de alta frecuencia (fire-and-forget)
        - asyncpg pool para queries SQL (OHLCV, aggregates, reports)
    """

    def __init__(self) -> None:
        load_dotenv()
        self.pg_host = os.getenv("QUESTDB_PG_HOST", "localhost")
        self.pg_port = int(os.getenv("QUESTDB_PG_PORT", "8812"))
        self.pg_user = os.getenv("QUESTDB_PG_USER", "admin")
        self.pg_password = os.getenv("QUESTDB_PG_PASSWORD", "quest")
        self.ilp_host = os.getenv("QUESTDB_ILP_HOST", self.pg_host)
        self.ilp_port = int(os.getenv("QUESTDB_ILP_PORT", "9009"))

        self._pg_pool = None       # asyncpg connection pool
        self._ilp_socket = None    # Raw TCP socket for ILP
        self._connected = False
        self._ilp_connected = False

        # Ingestion buffer for batch writes
        self._ilp_buffer: List[str] = []
        self._BUFFER_FLUSH_SIZE = 100  # Flush every N lines
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ══════════════════════════════════════════════════════════════════
    #  Connection Lifecycle
    # ══════════════════════════════════════════════════════════════════

    async def connect(self) -> bool:
        """Establece conexiones: asyncpg pool + ILP socket."""
        success = True

        # 1. PostgreSQL wire (asyncpg) — for queries
        try:
            import asyncpg  # type: ignore
            self._pg_pool = await asyncpg.create_pool(
                host=self.pg_host,
                port=self.pg_port,
                user=self.pg_user,
                password=self.pg_password,
                database="qdb",
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            self._connected = True
            logger.info(
                f"🗄️ QuestDB PG pool conectado ({self.pg_host}:{self.pg_port})"
            )
        except ImportError:
            logger.warning(
                "asyncpg no instalado. QuestDB queries desactivados. "
                "Instalar: pip install asyncpg"
            )
            success = False
        except Exception as exc:
            logger.error(f"Error conectando asyncpg a QuestDB: {exc}")
            success = False

        # 2. ILP socket — for high-throughput ingestion
        try:
            self._ilp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._ilp_socket.connect((self.ilp_host, self.ilp_port))
            self._ilp_socket.setblocking(False)
            self._ilp_connected = True
            logger.info(
                f"🗄️ QuestDB ILP socket conectado ({self.ilp_host}:{self.ilp_port})"
            )
        except Exception as exc:
            logger.warning(f"ILP socket no disponible: {exc}. Ticks se descartarán.")
            self._ilp_connected = False

        return success

    async def disconnect(self) -> None:
        """Cierra pool y socket limpiamente."""
        # Flush any remaining buffer
        await self._flush_ilp_buffer()

        if self._pg_pool:
            await self._pg_pool.close()
            self._pg_pool = None
        if self._ilp_socket:
            try:
                self._ilp_socket.close()
            except Exception:
                pass
            self._ilp_socket = None

        self._connected = False
        self._ilp_connected = False
        logger.info("🗄️ QuestDB desconectado.")

    # ══════════════════════════════════════════════════════════════════
    #  Schema Management
    # ══════════════════════════════════════════════════════════════════

    async def create_tables(self) -> None:
        """
        Crea las tablas time-series particionadas si no existen.
        QuestDB usa SQL estándar con extensiones TIMESTAMP y PARTITION BY.
        """
        if not self._pg_pool:
            logger.warning("PG pool no disponible. No se pueden crear tablas.")
            return

        statements = [
            # Tick-level market data (partitioned by DAY for fast pruning)
            """
            CREATE TABLE IF NOT EXISTS ticks (
                asset SYMBOL CAPACITY 256 CACHE,
                bid DOUBLE,
                ask DOUBLE,
                mid DOUBLE,
                spread DOUBLE,
                volume DOUBLE,
                timestamp TIMESTAMP
            ) TIMESTAMP(timestamp) PARTITION BY DAY WAL
            DEDUP UPSERT KEYS(timestamp, asset);
            """,
            # Aggregated OHLCV candles (partitioned by MONTH)
            """
            CREATE TABLE IF NOT EXISTS candles (
                asset SYMBOL CAPACITY 256 CACHE,
                timeframe SYMBOL CAPACITY 16 CACHE,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume DOUBLE,
                timestamp TIMESTAMP
            ) TIMESTAMP(timestamp) PARTITION BY MONTH WAL
            DEDUP UPSERT KEYS(timestamp, asset, timeframe);
            """,
            # Trade execution log (partitioned by MONTH)
            """
            CREATE TABLE IF NOT EXISTS trades (
                order_id SYMBOL CAPACITY 65536 CACHE,
                venue SYMBOL CAPACITY 8 CACHE,
                asset SYMBOL CAPACITY 256 CACHE,
                direction SYMBOL CAPACITY 8 CACHE,
                size DOUBLE,
                price DOUBLE,
                payout DOUBLE,
                commission DOUBLE,
                latency_ms DOUBLE,
                confidence DOUBLE,
                regime SYMBOL CAPACITY 8 CACHE,
                status SYMBOL CAPACITY 16 CACHE,
                timestamp TIMESTAMP
            ) TIMESTAMP(timestamp) PARTITION BY MONTH WAL;
            """,
            # Macro regime history (for analysis)
            """
            CREATE TABLE IF NOT EXISTS macro_regimes (
                regime SYMBOL CAPACITY 8 CACHE,
                provider SYMBOL CAPACITY 16 CACHE,
                reasoning STRING,
                fear_greed_score DOUBLE,
                timestamp TIMESTAMP
            ) TIMESTAMP(timestamp) PARTITION BY MONTH WAL;
            """,
        ]

        async with self._pg_pool.acquire() as conn:
            for stmt in statements:
                try:
                    await conn.execute(stmt.strip())
                except Exception as exc:
                    # QuestDB may not support IF NOT EXISTS on all versions
                    # Safely ignore "table already exists" errors
                    if "already exists" not in str(exc).lower():
                        logger.error(f"Error creando tabla: {exc}")

        logger.info("🗄️ QuestDB schema verificado/creado (4 tablas)")

    # ══════════════════════════════════════════════════════════════════
    #  ILP Ingestion (High-Throughput Write Path)
    # ══════════════════════════════════════════════════════════════════

    def _build_ilp_line(
        self,
        measurement: str,
        tags: Dict[str, str],
        fields: Dict[str, float],
        timestamp_ns: Optional[int] = None,
    ) -> str:
        """
        Construye una línea ILP (InfluxDB Line Protocol).
        Format: measurement,tag1=val1,tag2=val2 field1=val1,field2=val2 timestamp_ns
        """
        tag_str = ",".join(f"{k}={v}" for k, v in tags.items())
        field_str = ",".join(f"{k}={v}" for k, v in fields.items())

        if timestamp_ns is None:
            timestamp_ns = int(time.time() * 1_000_000_000)

        return f"{measurement},{tag_str} {field_str} {timestamp_ns}\n"

    async def _flush_ilp_buffer(self) -> None:
        """Envía el buffer acumulado al socket ILP."""
        if not self._ilp_buffer or not self._ilp_connected:
            return

        async with self._lock:
            if not self._ilp_buffer:
                return

            payload = "".join(self._ilp_buffer)
            self._ilp_buffer.clear()

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, self._ilp_socket.sendall, payload.encode("utf-8")
            )
        except Exception as exc:
            logger.error(f"Error flushing ILP buffer: {exc}")
            self._ilp_connected = False

    async def ingest_tick(
        self,
        asset: str,
        bid: float,
        ask: float,
        volume: float = 0.0,
        timestamp_ns: Optional[int] = None,
    ) -> None:
        """
        Ingesta un tick de mercado vía ILP.
        Fire-and-forget con buffering para máximo throughput.
        """
        if not self._ilp_connected:
            return

        mid = (bid + ask) / 2.0
        spread = ask - bid

        line = self._build_ilp_line(
            measurement="ticks",
            tags={"asset": asset},
            fields={
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread": spread,
                "volume": volume,
            },
            timestamp_ns=timestamp_ns,
        )

        self._ilp_buffer.append(line)

        if len(self._ilp_buffer) >= self._BUFFER_FLUSH_SIZE:
            await self._flush_ilp_buffer()

    async def ingest_candle(
        self,
        asset: str,
        timeframe: str,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        timestamp_ns: Optional[int] = None,
    ) -> None:
        """Ingesta una vela OHLCV vía ILP."""
        if not self._ilp_connected:
            return

        line = self._build_ilp_line(
            measurement="candles",
            tags={"asset": asset, "timeframe": timeframe},
            fields={
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            },
            timestamp_ns=timestamp_ns,
        )

        self._ilp_buffer.append(line)
        if len(self._ilp_buffer) >= self._BUFFER_FLUSH_SIZE:
            await self._flush_ilp_buffer()

    async def ingest_trade(self, trade_data: Dict[str, Any]) -> None:
        """
        Persiste un TradeResult en la tabla trades vía ILP.
        Acepta un dict con los campos de TradeResult.
        """
        if not self._ilp_connected:
            return

        line = self._build_ilp_line(
            measurement="trades",
            tags={
                "order_id": str(trade_data.get("order_id", "N/A")),
                "venue": str(trade_data.get("venue", "UNKNOWN")),
                "asset": str(trade_data.get("asset", "UNKNOWN")),
                "direction": str(trade_data.get("direction", "UNKNOWN")),
                "regime": str(trade_data.get("regime", "GREEN")),
                "status": str(trade_data.get("status", "UNKNOWN")),
            },
            fields={
                "size": float(trade_data.get("size", 0)),
                "price": float(trade_data.get("price", 0)),
                "payout": float(trade_data.get("payout", 0)),
                "commission": float(trade_data.get("commission", 0)),
                "latency_ms": float(trade_data.get("latency_ms", 0)),
                "confidence": float(trade_data.get("confidence", 0)),
            },
        )

        self._ilp_buffer.append(line)
        if len(self._ilp_buffer) >= self._BUFFER_FLUSH_SIZE:
            await self._flush_ilp_buffer()

    # ══════════════════════════════════════════════════════════════════
    #  SQL Query Path (Read via asyncpg)
    # ══════════════════════════════════════════════════════════════════

    async def query_ohlcv(
        self,
        asset: str,
        timeframe: str = "1m",
        bars: int = 500,
    ) -> pd.DataFrame:
        """
        Query OHLCV candles desde QuestDB usando SAMPLE BY.
        Retorna un DataFrame Pandas listo para Alpha V3.
        """
        if not self._pg_pool:
            logger.warning("PG pool no disponible para query.")
            return pd.DataFrame()

        # Map timeframe to QuestDB SAMPLE BY syntax
        sample_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "4h": "4h", "1d": "1d",
        }
        sample_by = sample_map.get(timeframe, "1m")

        query = f"""
        SELECT
            timestamp as open_time,
            first(mid) as open,
            max(mid) as high,
            min(mid) as low,
            last(mid) as close,
            sum(volume) as volume
        FROM ticks
        WHERE asset = $1
        SAMPLE BY {sample_by}
        ORDER BY timestamp DESC
        LIMIT {bars};
        """

        try:
            async with self._pg_pool.acquire() as conn:
                rows = await conn.fetch(query, asset)

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(
                [dict(row) for row in rows],
                columns=["open_time", "open", "high", "low", "close", "volume"],
            )
            # Reverse to chronological order (oldest first)
            df = df.iloc[::-1].reset_index(drop=True)

            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)

            return df

        except Exception as exc:
            logger.error(f"Error querying OHLCV: {exc}")
            return pd.DataFrame()

    async def query_candles(
        self,
        asset: str,
        timeframe: str = "1m",
        bars: int = 500,
    ) -> pd.DataFrame:
        """
        Query pre-aggregated candles from the candles table.
        Use this when candles are ingested directly (not from ticks).
        """
        if not self._pg_pool:
            return pd.DataFrame()

        query = """
        SELECT timestamp as open_time, open, high, low, close, volume
        FROM candles
        WHERE asset = $1 AND timeframe = $2
        ORDER BY timestamp DESC
        LIMIT $3;
        """

        try:
            async with self._pg_pool.acquire() as conn:
                rows = await conn.fetch(query, asset, timeframe, bars)

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(
                [dict(row) for row in rows],
                columns=["open_time", "open", "high", "low", "close", "volume"],
            )
            df = df.iloc[::-1].reset_index(drop=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)

            return df

        except Exception as exc:
            logger.error(f"Error querying candles: {exc}")
            return pd.DataFrame()

    async def query_trades(
        self,
        venue: Optional[str] = None,
        limit: int = 100,
    ) -> pd.DataFrame:
        """Query trade execution history for tear sheet generation."""
        if not self._pg_pool:
            return pd.DataFrame()

        if venue:
            query = """
            SELECT * FROM trades
            WHERE venue = $1
            ORDER BY timestamp DESC
            LIMIT $2;
            """
            params = (venue, limit)
        else:
            query = f"""
            SELECT * FROM trades
            ORDER BY timestamp DESC
            LIMIT {limit};
            """
            params = ()

        try:
            async with self._pg_pool.acquire() as conn:
                if params:
                    rows = await conn.fetch(query, *params)
                else:
                    rows = await conn.fetch(query)

            if not rows:
                return pd.DataFrame()

            return pd.DataFrame([dict(row) for row in rows])

        except Exception as exc:
            logger.error(f"Error querying trades: {exc}")
            return pd.DataFrame()

    async def ingest_macro_regime(
        self,
        regime: str,
        provider: str,
        reasoning: str,
        fear_greed_score: float = 0.0,
    ) -> None:
        """Persiste un cambio de régimen macro para análisis histórico."""
        if not self._ilp_connected:
            return

        line = self._build_ilp_line(
            measurement="macro_regimes",
            tags={"regime": regime, "provider": provider},
            fields={
                "fear_greed_score": fear_greed_score,
            },
        )
        # Regime changes are rare — flush immediately
        self._ilp_buffer.append(line)
        await self._flush_ilp_buffer()

    # ══════════════════════════════════════════════════════════════════
    #  Maintenance
    # ══════════════════════════════════════════════════════════════════

    async def get_table_stats(self) -> Dict[str, int]:
        """Retorna conteo de filas por tabla para monitoreo."""
        if not self._pg_pool:
            return {}

        stats = {}
        for table in ["ticks", "candles", "trades", "macro_regimes"]:
            try:
                async with self._pg_pool.acquire() as conn:
                    row = await conn.fetchrow(f"SELECT count() as cnt FROM {table};")
                    stats[table] = row["cnt"] if row else 0
            except Exception:
                stats[table] = -1  # Table doesn't exist yet

        return stats

    def __repr__(self) -> str:
        pg = "PG:OK" if self._connected else "PG:OFF"
        ilp = "ILP:OK" if self._ilp_connected else "ILP:OFF"
        buf = len(self._ilp_buffer)
        return f"<QuestDBClient {pg} {ilp} buffer={buf}>"
