"""
NEXUS v4.0 — IQ Option Execution Engine (Layer 5: Concrete)
============================================================
Implementación concreta del AbstractExecutionEngine para IQ Option.
Modo "Trojan Horse": Sniper Mode, Flat Sizing $1, Binary Turbo 1m.

Powered by: JCBV Modernized API v7.1.1
    - Zero busy-waiting (threading.Event)
    - Strict TLS (verify=True enforced)
    - Thread-safe (no global state)
    - 120-min timeout protection (check_win_v4)
    - Dynamic asset synchronization

Repository: https://github.com/johnblack593/IQOP-API-JOHNBARZOLA
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

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

# JCBV Modernized API — zero busy-wait, TLS strict, thread-safe
try:
    from iqoptionapi.stable_api import IQ_Option
except ImportError:
    IQ_Option = None
    logger.warning("iqoptionapi (JCBV Edition) no instalado. Ejecución IQ desactivada.")


class IQOptionExecutionEngine(AbstractExecutionEngine):
    """
    Motor de ejecución para IQ Option Binary/Turbo Options.

    Powered by JCBV Modernized API v7.1.1:
        - Conexión WebSocket con TLS estricto
        - Ejecución CALL/PUT con validación de payout mínimo
        - Datos históricos OHLCV con paginación profunda
        - Balance PRACTICE/REAL según config
        - P&L extraction via check_win_v4 (async, GIL-safe, timeout-protected)
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
            logger.error("iqoptionapi (JCBV Edition) no instalado.")
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
                    logger.info(f"✅ IQ Option conectado [{balance_mode}] — JCBV API v{IQ_Option.__version__}")
                    return True
                else:
                    if reason == '2FA':
                        logger.critical("2FA ACTIVADO — Enviar código SMS requerido. Abortando reconexión.")
                        return False

                    logger.warning(f"⚠️ Conexión fallida ({reason}). Reintentando en {delay}s...")
                    await asyncio.sleep(delay)

                    delay = min(delay * 2, max_delay)
                    attempt += 1

                    if attempt > 10:
                        logger.error(f"❌ Fallo IQ Option definitivo tras {attempt - 1} intentos: {reason}")
                        return False

    async def disconnect(self) -> None:
        """Cierra la conexión WebSocket limpiamente (JCBV API tiene close())."""
        if self._api:
            try:
                if hasattr(self._api, 'api') and hasattr(self._api.api, 'close'):
                    await asyncio.to_thread(self._api.api.close)
                    logger.info("IQ Option WebSocket cerrado limpiamente.")
            except Exception as e:
                logger.warning(f"Error cerrando WebSocket IQ: {e}")
            finally:
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

    async def get_best_available_asset(self, min_payout: int = 80) -> Optional[Dict[str, str]]:
        """
        Retorna el activo turbo/binary abierto que cumpla el min_payout con la
        mayor liquidez simulada (IQOption internamente no da volumen, pero
        filtramos por mejor payout primariamente).
        """
        if not await self.connect():
            return None

        try:
            payouts = await asyncio.to_thread(self._api.get_all_profit)

            best_asset = None
            best_payout = -1.0

            for asset, data in payouts.items():
                if isinstance(data, dict):
                    val = data.get("turbo", 0)
                    if 0 < val < 1.0:
                        payout_pct = float(val * 100)
                    elif val > 0:
                        payout_pct = float(val * 100 if val < 10 else val)
                    else:
                        payout_pct = 0.0

                    if payout_pct >= min_payout and payout_pct > best_payout:
                        # Añade sufijo -op para estandarización del Nexus pipeline
                        best_asset = f"{asset}-op"
                        best_payout = payout_pct

            if best_asset:
                logger.info(f"🔎 Scanner: Mejor activo detectado -> {best_asset} ({best_payout:.1f}%)")
                return {"symbol": best_asset, "market_type": "binary"}
            return None

        except Exception as exc:
            logger.error(f"Error escaneando mejores activos: {exc}")
            return None

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

    # ── P&L Extraction (JCBV API Feature: check_win_v4) ──────────

    async def check_trade_result(self, order_id: int) -> Tuple[Optional[str], Optional[float]]:
        """
        Extrae el resultado exacto de un trade binario (P&L).

        Usa check_win_v4 de la JCBV API:
            - Async-safe, GIL-safe
            - Timeout inyectado de 120 min contra congelamientos
            - Retorna (resultado, profit_exacto) o (None, None) en timeout

        Args:
            order_id: ID numérico del trade retornado por buy()

        Returns:
            Tuple[str, float]: ("win"|"loose"|"equal", profit_amount)
            Tuple[None, None]: Si timeout o error
        """
        if not self._api:
            logger.error("API no conectada para verificar resultado del trade.")
            return None, None

        try:
            result, profit = await asyncio.to_thread(self._api.check_win_v4, order_id)
            if result == "win":
                logger.info(f"🟢 TRADE WON | Order #{order_id} | Profit: +${profit:.2f}")
            elif result == "loose":
                logger.info(f"🔴 TRADE LOST | Order #{order_id} | Loss: ${profit:.2f}")
            elif result == "equal":
                logger.info(f"⚪ TRADE TIE | Order #{order_id} | Capital devuelto")
            else:
                logger.warning(f"⚠️ TRADE TIMEOUT | Order #{order_id} | Sin resultado")
            return result, profit
        except Exception as e:
            logger.error(f"Error verificando resultado del trade #{order_id}: {e}")
            return None, None

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
