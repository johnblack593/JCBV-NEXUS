"""
NEXUS v4.0 — Binance Execution Engine (Layer 5: Concrete)
==========================================================
Implementación concreta del AbstractExecutionEngine para Binance
Spot / Perpetuals. Modo Institucional: DMA, Market Making,
Arbitraje Estadístico.

Hereda de AbstractExecutionEngine y adapta la lógica existente de
BinanceConnector + ExecutionEngine (v3) al contrato unificado.

Dependencias: python-binance (AsyncClient) o ccxt.pro (futuro).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

logger = logging.getLogger("nexus.execution.binance")


class BinanceExecutionEngine(AbstractExecutionEngine):
    """
    Motor de ejecución DMA para Binance Futures/Spot.
    
    Responsabilidades:
        - Conexión AsyncClient vía python-binance
        - Ejecución Market/Limit con OCO (SL+TP)
        - TWAP para órdenes institucionales grandes
        - Portfolio tracking y balance USDT
    """

    def __init__(self) -> None:
        load_dotenv()
        self._api_key = os.getenv("BINANCE_API_KEY", "")
        self._api_secret = os.getenv("BINANCE_API_SECRET", "")
        self._testnet = os.getenv("BINANCE_TESTNET", "True").lower() in ("true", "1", "yes")

        self._client = None
        self._connected = False
        self._order_history: List[TradeResult] = []

    # ── AbstractExecutionEngine Contract ──────────────────────────

    @property
    def venue(self) -> VenueType:
        return VenueType.BINANCE

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    async def connect(self) -> bool:
        if self._connected and self._client:
            return True

        try:
            from binance import AsyncClient  # type: ignore

            self._client = await AsyncClient.create(
                api_key=self._api_key,
                api_secret=self._api_secret,
                testnet=self._testnet,
            )
            self._connected = True
            mode = "TESTNET" if self._testnet else "LIVE"
            logger.info(f"✅ Binance Futures conectado [{mode}]")
            return True
        except ImportError:
            logger.error("python-binance no instalado. Binance engine desactivado.")
            return False
        except Exception as exc:
            logger.error(f"❌ Error conectando a Binance: {exc}")
            return False

    async def disconnect(self) -> None:
        if self._client:
            await self._client.close_connection()
            self._connected = False
            self._client = None
            logger.info("Binance desconectado.")

    async def get_balance(self) -> float:
        if not await self.connect():
            return 0.0
        try:
            account = await self._client.futures_account()
            for asset in account.get("assets", []):
                if asset["asset"] == "USDT":
                    return float(asset.get("walletBalance", 0))
            return 0.0
        except Exception as exc:
            logger.error(f"Error obteniendo balance: {exc}")
            return 0.0

    async def get_payout(self, asset: str) -> float:
        """
        Para Binance retorna el spread en bps como proxy de 'cost'.
        Implementación completa vendrá en Fase 3 con TCA.
        """
        if not await self.connect():
            return 0.0
        try:
            ticker = await self._client.futures_orderbook_ticker(symbol=asset)
            bid = float(ticker.get("bidPrice", 0))
            ask = float(ticker.get("askPrice", 0))
            if bid > 0:
                spread_bps = ((ask - bid) / bid) * 10000
                return round(spread_bps, 2)
            return 0.0
        except Exception as exc:
            logger.error(f"Error obteniendo spread de {asset}: {exc}")
            return 0.0

    async def execute(self, signal: TradeSignal) -> TradeResult:
        """
        Ejecuta una orden en Binance Futures.
        Soporta Market y Limit con SL/TP automáticos (OCO simulado).
        """
        t_start = time.perf_counter()

        if not await self.connect():
            return TradeResult(
                order_id="N/A", venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=ExecutionStatus.ERROR,
                size=signal.size, executed_price=0.0,
                latency_ms=(time.perf_counter() - t_start) * 1000,
            )

        # Map direction
        side = "BUY" if signal.direction in (SignalDirection.BUY, SignalDirection.CALL) else "SELL"

        try:
            if signal.order_type == "LIMIT" and signal.limit_price:
                raw = await self._client.futures_create_order(
                    symbol=signal.asset, side=side, type="LIMIT",
                    quantity=signal.size, price=signal.limit_price,
                    timeInForce="GTC",
                )
            else:
                raw = await self._client.futures_create_order(
                    symbol=signal.asset, side=side, type="MARKET",
                    quantity=signal.size,
                )

            fill_price = float(raw.get("avgPrice", 0))
            fill_qty = float(raw.get("executedQty", signal.size))
            commission = fill_qty * fill_price * 0.0004  # Maker fee

            # Place SL if provided
            if signal.stop_loss:
                close_side = "SELL" if side == "BUY" else "BUY"
                await self._client.futures_create_order(
                    symbol=signal.asset, side=close_side, type="STOP_MARKET",
                    quantity=signal.size, stopPrice=signal.stop_loss,
                )

            # Place TP if provided
            if signal.take_profit:
                close_side = "SELL" if side == "BUY" else "BUY"
                await self._client.futures_create_order(
                    symbol=signal.asset, side=close_side, type="TAKE_PROFIT_MARKET",
                    quantity=signal.size, stopPrice=signal.take_profit,
                )

            latency = (time.perf_counter() - t_start) * 1000

            # Map Binance status
            status_map = {
                "FILLED": ExecutionStatus.FILLED,
                "PARTIALLY_FILLED": ExecutionStatus.PARTIALLY_FILLED,
                "NEW": ExecutionStatus.SUBMITTED,
                "REJECTED": ExecutionStatus.REJECTED,
            }
            status = status_map.get(raw.get("status", ""), ExecutionStatus.SUBMITTED)

            result = TradeResult(
                order_id=str(raw.get("orderId", uuid.uuid4().hex[:12])),
                venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=status,
                size=fill_qty, executed_price=fill_price,
                commission=commission, latency_ms=latency,
                raw_response=raw,
            )

            self._order_history.append(result)
            logger.info(
                f"🎯 ENTRY | BINANCE | {signal.asset} | {side} "
                f"| qty={fill_qty} @ ${fill_price:.2f} | Latency: {latency:.1f}ms"
            )
            return result

        except Exception as exc:
            latency = (time.perf_counter() - t_start) * 1000
            logger.error(f"❌ Orden Binance fallida: {exc}")
            return TradeResult(
                order_id="N/A", venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=ExecutionStatus.ERROR,
                size=signal.size, executed_price=0.0,
                latency_ms=latency,
            )

    # ── Binance-specific methods ──────────────────────────────────

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """Retorna posiciones abiertas en Futures."""
        if not self.is_connected:
            return []
        try:
            account = await self._client.futures_account()
            positions = []
            for pos in account.get("positions", []):
                amt = float(pos.get("positionAmt", 0))
                if amt != 0:
                    positions.append({
                        "symbol": pos["symbol"],
                        "side": "LONG" if amt > 0 else "SHORT",
                        "quantity": abs(amt),
                        "entry_price": float(pos.get("entryPrice", 0)),
                        "unrealized_pnl": float(pos.get("unrealizedProfit", 0)),
                    })
            return positions
        except Exception as exc:
            logger.error(f"Error obteniendo posiciones: {exc}")
            return []

    def get_order_history(self) -> pd.DataFrame:
        """Retorna historial de órdenes como DataFrame."""
        if not self._order_history:
            return pd.DataFrame()
        return pd.DataFrame([
            {
                "order_id": r.order_id, "asset": r.asset,
                "direction": r.direction.value, "status": r.status.value,
                "size": r.size, "price": r.executed_price,
                "commission": r.commission, "latency_ms": r.latency_ms,
                "timestamp": r.timestamp,
            }
            for r in self._order_history
        ])
