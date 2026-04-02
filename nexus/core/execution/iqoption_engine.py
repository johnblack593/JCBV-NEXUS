"""
NEXUS v4.0 — IQ Option Execution Engine (Layer 5: Concrete)
============================================================
Implementación concreta del AbstractExecutionEngine para IQ Option.
Modo "Trojan Horse": Sniper Mode, Flat Sizing $1, Binary Turbo 1m.

Hereda de AbstractExecutionEngine y adapta la lógica existente de
IQOptionManager + IQOptionExecutionEngine (v3) al contrato unificado.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd
from dotenv import load_dotenv

from .base import (
    AbstractExecutionEngine,
    ExecutionStatus,
    SignalDirection,
    TradeResult,
    TradeSignal,
    VenueType,
)

logger = logging.getLogger("nexus.execution.iqoption")

# Dependencia comunitaria: The unofficial IQ Option wrapper
try:
    from iqoptionapi.stable_api import IQ_Option
except ImportError:
    IQ_Option = None
    logger.warning("iqoptionapi no instalado. Ejecución IQ desactivada.")


class IQOptionExecutionEngine(AbstractExecutionEngine):
    """
    Motor de ejecución para IQ Option Binary/Turbo Options.
    
    Responsabilidades:
        - Conexión WebSocket vía iqoptionapi
        - Ejecución CALL/PUT con validación de payout mínimo
        - Datos históricos OHLCV con paginación profunda
        - Balance PRACTICE/REAL según config
    """

    def __init__(self) -> None:
        load_dotenv()
        self._email = os.getenv("IQ_OPTION_EMAIL", "")
        self._password = os.getenv("IQ_OPTION_PASSWORD", "")
        self._account_type = os.getenv("IQ_OPTION_ACCOUNT_TYPE", "PRACTICE").upper()
        self._min_payout = int(os.getenv("IQ_MIN_PAYOUT", "80"))

        self._api: Optional[IQ_Option] = None
        self._connected = False
        self._lock = asyncio.Lock()

    @staticmethod
    def _sanitize_asset(asset: str) -> str:
        """Strip pipeline suffixes (-op) and normalize to IQ API format."""
        return asset.replace("-op", "").replace("_", "").upper()

    # ── AbstractExecutionEngine Contract ──────────────────────────

    @property
    def venue(self) -> VenueType:
        return VenueType.IQ_OPTION

    @property
    def is_connected(self) -> bool:
        return self._connected and self._api is not None

    async def connect(self) -> bool:
        if self._connected and self._api and self._api.check_connect():
            return True

        if not IQ_Option:
            logger.error("iqoptionapi no instalado.")
            return False

        async with self._lock:
            # Double-check inside lock
            if self._connected and self._api and self._api.check_connect():
                return True

            attempt = 1
            delay = 1.0
            max_delay = 30.0

            while True:
                logger.info(f"Conectando a IQ Option con {self._email} (Intento {attempt})...")
                self._api = IQ_Option(self._email, self._password)

                check, reason = await asyncio.to_thread(self._api.connect)

                if check:
                    self._connected = True
                    balance_mode = "PRACTICE" if self._account_type == "PRACTICE" else "REAL"
                    await asyncio.to_thread(self._api.change_balance, balance_mode)
                    logger.info(f"✅ IQ Option conectado [{balance_mode}]")
                    return True
                else:
                    if reason == '2FA':
                        logger.critical("2FA ACTIVADO — Enviar código SMS requerido. Abortando reconexión.")
                        return False

                    logger.warning(f"⚠️ Conexión fallida ({reason}). Reintentando en {delay}s...")
                    await asyncio.sleep(delay)
                    
                    delay = min(delay * 2, max_delay)
                    attempt += 1
                    
                    # Stop if we hit 30s backoff and fail repeatedly (limit arbitrary or let it try?)
                    # Requirements say "Exponential Backoff (1s, 2s, 4s... max 30s)". 
                    # If it meant indefinitely capping at 30s, we keep looping. Let's limit attempts to 10.
                    if attempt > 10:
                        logger.error(f"❌ Fallo IQ Option definitivo tras múltiples ataques: {reason}")
                        return False

    async def disconnect(self) -> None:
        if self._api:
            # iqoptionapi no tiene un close() limpio, pero limpiamos estado
            self._connected = False
            self._api = None
            logger.info("IQ Option desconectado.")

    async def get_balance(self) -> float:
        if not await self.connect():
            return 0.0
        return await asyncio.to_thread(self._api.get_balance)

    async def get_payout(self, asset: str, option_type: str = "turbo") -> float:
        """Retorna el payout % activo del activo."""
        if not await self.connect():
            return 0.0

        asset_clean = self._sanitize_asset(asset)
        payouts = await asyncio.to_thread(self._api.get_all_profit)
        try:
            # Try both raw and cleaned keys (IQ API is inconsistent)
            data = payouts.get(asset_clean, payouts.get(asset, {}))
            val = data.get(option_type, 0) if isinstance(data, dict) else 0
            if 0 < val < 1.0:
                return float(val * 100)
            return float(val * 100 if val < 10 else val)
        except Exception:
            return 0.0

    async def execute(self, signal: TradeSignal) -> TradeResult:
        """
        Ejecuta una orden CALL/PUT en IQ Option Turbo.
        Valida payout mínimo antes de disparar.
        """
        t_start = time.perf_counter()

        if not await self.connect():
            return TradeResult(
                order_id="N/A", venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=ExecutionStatus.ERROR,
                size=signal.size, executed_price=0.0,
                latency_ms=(time.perf_counter() - t_start) * 1000,
            )

        # Map direction to IQ action
        action = "call" if signal.direction in (SignalDirection.CALL, SignalDirection.BUY) else "put"

        # Validate payout
        asset_clean = self._sanitize_asset(signal.asset)
        payout = await self.get_payout(signal.asset)
        if payout < self._min_payout:
            logger.warning(
                f"⚠️ Payout {payout}% < {self._min_payout}% para {asset_clean}. REJECTED."
            )
            return TradeResult(
                order_id="N/A", venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=ExecutionStatus.REJECTED,
                size=signal.size, executed_price=0.0, payout=payout,
                latency_ms=(time.perf_counter() - t_start) * 1000,
            )

        # Fire order with auto-reconnect logic
        try:
            check, id_req = await asyncio.to_thread(
                self._api.buy, signal.size, asset_clean, action, signal.expiration_minutes
            )
        except Exception as e:
            logger.warning(f"⚠️ Conexión perdida durante ejecución. Reiniciando motor IQ... Error: {e}")
            self._connected = False
            
            # Intenta reconectar exactamente una vez INMEDIATAMENTE
            if await self.connect():
                try:
                    check, id_req = await asyncio.to_thread(
                        self._api.buy, signal.size, asset_clean, action, signal.expiration_minutes
                    )
                except Exception as e2:
                    logger.error(f"❌ Falla definitiva en ejecución tras reconectar: {e2}")
                    return TradeResult(
                        order_id="N/A", venue=self.venue, asset=signal.asset,
                        direction=signal.direction, status=ExecutionStatus.ERROR,
                        size=signal.size, executed_price=0.0, payout=payout,
                        latency_ms=(time.perf_counter() - t_start) * 1000,
                    )
            else:
                return TradeResult(
                    order_id="N/A", venue=self.venue, asset=signal.asset,
                    direction=signal.direction, status=ExecutionStatus.ERROR,
                    size=signal.size, executed_price=0.0, payout=payout,
                    latency_ms=(time.perf_counter() - t_start) * 1000,
                )

        latency = (time.perf_counter() - t_start) * 1000

        if check:
            logger.info(
                f"🎯 SNIPER ENTRY | IQ_OPTION | {signal.asset} | {action.upper()} "
                f"| ${signal.size} | Payout: {payout}% | Latency: {latency:.1f}ms"
            )
            return TradeResult(
                order_id=str(id_req), venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=ExecutionStatus.FILLED,
                size=signal.size, executed_price=0.0, payout=payout,
                latency_ms=latency,
            )
        else:
            logger.error(f"❌ Orden IQ fallida: {id_req}")
            return TradeResult(
                order_id="N/A", venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=ExecutionStatus.ERROR,
                size=signal.size, executed_price=0.0, payout=payout,
                latency_ms=latency,
            )

    # ── Data Methods (IQ-specific) ────────────────────────────────

    _TF_MAP = {
        "1m": 60, "2m": 120, "3m": 180, "5m": 300,
        "15m": 900, "30m": 1800, "1h": 3600,
    }

    async def get_historical_data(
        self, asset: str, tf: str, max_bars: int = 1000
    ) -> pd.DataFrame:
        """Descarga OHLCV histórico con paginación profunda."""
        if not await self.connect():
            return pd.DataFrame()

        asset_clean = self._sanitize_asset(asset)
        size = self._TF_MAP.get(tf, 60)
        end_from_time = int(time.time())
        all_candles = []
        remaining = max_bars

        while remaining > 0:
            batch_size = min(remaining, 1000)
            candles = await asyncio.to_thread(
                self._api.get_candles, asset_clean, size, batch_size, end_from_time
            )
            if not candles:
                break

            all_candles.extend(reversed(candles))
            oldest_time = candles[0]['from']
            end_from_time = oldest_time - 1
            remaining -= len(candles)

            if len(candles) < (batch_size * 0.5):
                break
            await asyncio.sleep(0.5)

        if not all_candles:
            return pd.DataFrame()

        all_candles.reverse()
        df = pd.DataFrame(all_candles)
        df['open_time'] = pd.to_datetime(df['from'], unit='s', utc=True)
        df['close_time'] = pd.to_datetime(df['to'], unit='s', utc=True)
        df.rename(columns={'min': 'low', 'max': 'high'}, inplace=True)
        df = df.drop_duplicates(subset=['open_time'])
        df = df.sort_values(by="open_time").reset_index(drop=True)
        df = df[['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time']]
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)

        logger.info(f"✅ {len(df)} velas descargadas para {asset} ({tf})")
        return df
