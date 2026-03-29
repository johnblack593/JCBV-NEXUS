"""
NEXUS Trading System — Execution Engine
=========================================
Ejecucion de ordenes en Binance Futures: ordenes Market, Limit,
OCO (One-Cancels-Other), TWAP (Time-Weighted Average Price)
y gestion del ciclo de vida completo de ordenes.

Usa python-binance AsyncClient para no bloquear el hilo principal.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np  # type: ignore
import pandas as pd  # type: ignore

logger = logging.getLogger("nexus.execution")


# ══════════════════════════════════════════════════════════════════════
#  Enums y Data Classes
# ══════════════════════════════════════════════════════════════════════

class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    OCO = "OCO"
    TWAP = "TWAP"


class OrderStatus(Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class OrderRequest:
    """Solicitud de orden para enviar al exchange."""
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    time_in_force: str = "GTC"


@dataclass
class OrderResult:
    """Resultado de una orden ejecutada."""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    executed_qty: float
    executed_price: float
    commission: float
    timestamp: datetime
    raw_response: Optional[Dict[str, Any]] = None


@dataclass
class TWAPConfig:
    """Configuracion para ejecucion TWAP."""
    total_quantity: float
    num_slices: int = 10
    interval_seconds: int = 60
    max_deviation_pct: float = 0.005


# ══════════════════════════════════════════════════════════════════════
#  Binance Connector
# ══════════════════════════════════════════════════════════════════════

class BinanceConnector:
    """
    Wrapper sobre python-binance AsyncClient para operaciones de
    cuenta, ordenes y posiciones en Binance Futures.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self._client = None
        self._connected = False

    async def connect(self) -> None:
        """Inicializa el AsyncClient de Binance."""
        try:
            from binance import AsyncClient  # type: ignore

            self._client = await AsyncClient.create(
                api_key=self.api_key,
                api_secret=self.api_secret,
                testnet=self.testnet,
            )
            self._connected = True
            mode = "TESTNET" if self.testnet else "LIVE"
            logger.info("Conectado a Binance Futures (%s)", mode)
        except Exception as exc:
            logger.error("Error conectando a Binance: %s", exc)
            self._connected = False
            raise

    async def disconnect(self) -> None:
        """Cierra la conexion con Binance."""
        if self._client:
            await self._client.close_connection()  # type: ignore
            self._connected = False
            logger.info("Desconectado de Binance")

    def _ensure_connected(self) -> None:
        if not self._connected or self._client is None:
            raise ConnectionError(
                "BinanceConnector no esta conectado. Llamar connect() primero."
            )

    async def get_account_balance(self) -> Dict[str, float]:
        """Retorna los balances de la cuenta futures."""
        self._ensure_connected()
        try:
            account = await self._client.futures_account()  # type: ignore
            balances = {}
            for asset in account.get("assets", []):
                balance = float(asset.get("walletBalance", 0))
                if balance > 0:
                    balances[asset["asset"]] = balance
            return balances
        except Exception as exc:
            logger.error("Error obteniendo balances: %s", exc)
            return {}

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retorna las ordenes abiertas en futures."""
        self._ensure_connected()
        try:
            if symbol:
                return await self._client.futures_get_open_orders(symbol=symbol)  # type: ignore
            return await self._client.futures_get_open_orders()  # type: ignore
        except Exception as exc:
            logger.error("Error obteniendo ordenes abiertas: %s", exc)
            return []

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """Retorna las posiciones abiertas en futures."""
        self._ensure_connected()
        try:
            account = await self._client.futures_account()  # type: ignore
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
                        "leverage": int(pos.get("leverage", 1)),
                    })
            return positions
        except Exception as exc:
            logger.error("Error obteniendo posiciones: %s", exc)
            return []

    async def place_market_order(
        self, symbol: str, side: str, quantity: float
    ) -> Dict[str, Any]:
        """Envia una orden de mercado a Binance Futures."""
        self._ensure_connected()
        try:
            result = await self._client.futures_create_order(  # type: ignore
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=quantity,
            )
            logger.info(
                "Orden MARKET %s %s qty=%.6f | orderId=%s",
                side, symbol, quantity, result.get("orderId"),
            )
            return result
        except Exception as exc:
            logger.error("Error en orden MARKET: %s", exc)
            raise

    async def place_limit_order(
        self, symbol: str, side: str, quantity: float, price: float
    ) -> Dict[str, Any]:
        """Envia una orden limite a Binance Futures."""
        self._ensure_connected()
        try:
            result = await self._client.futures_create_order(  # type: ignore
                symbol=symbol,
                side=side,
                type="LIMIT",
                quantity=quantity,
                price=price,
                timeInForce="GTC",
            )
            logger.info(
                "Orden LIMIT %s %s qty=%.6f @ $%.2f | orderId=%s",
                side, symbol, quantity, price, result.get("orderId"),
            )
            return result
        except Exception as exc:
            logger.error("Error en orden LIMIT: %s", exc)
            raise

    async def place_stop_market(
        self, symbol: str, side: str, quantity: float, stop_price: float
    ) -> Dict[str, Any]:
        """Envia una orden STOP_MARKET (para stop loss)."""
        self._ensure_connected()
        try:
            result = await self._client.futures_create_order(  # type: ignore
                symbol=symbol,
                side=side,
                type="STOP_MARKET",
                quantity=quantity,
                stopPrice=stop_price,
            )
            logger.info(
                "Orden STOP_MARKET %s %s qty=%.6f trigger=$%.2f | orderId=%s",
                side, symbol, quantity, stop_price, result.get("orderId"),
            )
            return result
        except Exception as exc:
            logger.error("Error en orden STOP_MARKET: %s", exc)
            raise

    async def place_take_profit_market(
        self, symbol: str, side: str, quantity: float, stop_price: float
    ) -> Dict[str, Any]:
        """Envia una orden TAKE_PROFIT_MARKET."""
        self._ensure_connected()
        try:
            result = await self._client.futures_create_order(  # type: ignore
                symbol=symbol,
                side=side,
                type="TAKE_PROFIT_MARKET",
                quantity=quantity,
                stopPrice=stop_price,
            )
            logger.info(
                "Orden TP_MARKET %s %s qty=%.6f trigger=$%.2f | orderId=%s",
                side, symbol, quantity, stop_price, result.get("orderId"),
            )
            return result
        except Exception as exc:
            logger.error("Error en orden TP_MARKET: %s", exc)
            raise

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Cancela una orden existente."""
        self._ensure_connected()
        try:
            result = await self._client.futures_cancel_order(  # type: ignore
                symbol=symbol, orderId=order_id,
            )
            logger.info("Orden %s cancelada para %s", order_id, symbol)
            return result
        except Exception as exc:
            logger.error("Error cancelando orden %s: %s", order_id, exc)
            raise

    async def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        """Cancela todas las ordenes abiertas para un simbolo."""
        self._ensure_connected()
        try:
            result = await self._client.futures_cancel_all_open_orders(symbol=symbol)  # type: ignore
            logger.info("Todas las ordenes canceladas para %s", symbol)
            return result
        except Exception as exc:
            logger.error("Error cancelando ordenes de %s: %s", symbol, exc)
            raise

    async def get_ticker_price(self, symbol: str) -> float:
        """Obtiene el precio actual del simbolo."""
        self._ensure_connected()
        try:
            ticker = await self._client.futures_symbol_ticker(symbol=symbol)  # type: ignore
            return float(ticker["price"])
        except Exception as exc:
            logger.error("Error obteniendo precio de %s: %s", symbol, exc)
            return 0.0


# ══════════════════════════════════════════════════════════════════════
#  TWAP Executor
# ══════════════════════════════════════════════════════════════════════

class TWAPExecutor:
    """
    Ejecutor de ordenes TWAP (Time-Weighted Average Price).
    Divide una orden grande en slices mas pequenos ejecutados
    a intervalos regulares para minimizar el impacto de mercado.
    """

    def __init__(self, connector: BinanceConnector) -> None:
        self.connector = connector
        self._active_twaps: Dict[str, Dict[str, Any]] = {}
        self._cancelled: set = set()

    async def execute(
        self,
        symbol: str,
        side: OrderSide,
        config: TWAPConfig,
    ) -> OrderResult:
        """
        Ejecuta una orden TWAP fragmentada en el tiempo.

        Args:
            symbol: Par de trading
            side:   BUY o SELL
            config: Configuracion TWAP (slices, intervalo, etc.)

        Returns:
            OrderResult consolidado con precio promedio ponderado.
        """
        twap_id = str(uuid.uuid4())[:8]  # type: ignore
        slice_qty = round(config.total_quantity / config.num_slices, 6)  # type: ignore
        total_filled = 0.0
        total_cost = 0.0
        fills: List[Dict[str, Any]] = []

        self._active_twaps[twap_id] = {
            "symbol": symbol,
            "side": side.value,
            "status": "EXECUTING",
            "slices_done": 0,
            "slices_total": config.num_slices,
            "filled_qty": 0.0,
        }

        logger.info(
            "TWAP iniciado [%s]: %s %s %.6f en %d slices (intervalo %ds)",
            twap_id, side.value, symbol, config.total_quantity,
            config.num_slices, config.interval_seconds,
        )

        try:
            for i in range(config.num_slices):  # type: ignore
                if twap_id in self._cancelled:  # type: ignore
                    logger.info("TWAP [%s] cancelado en slice %d/%d", twap_id, i + 1, config.num_slices)  # type: ignore
                    break

                # Verificar desviacion de precio
                current_price = await self.connector.get_ticker_price(symbol)  # type: ignore
                if i > 0 and fills:
                    avg_price = total_cost / total_filled if total_filled > 0 else current_price  # type: ignore
                    deviation = abs(current_price - avg_price) / avg_price
                    if deviation > config.max_deviation_pct:  # type: ignore
                        logger.warning(
                            "TWAP [%s] desviacion %.2f%% > %.2f%% — pausando",
                            twap_id, deviation * 100, config.max_deviation_pct * 100,  # type: ignore
                        )
                        await asyncio.sleep(config.interval_seconds)  # type: ignore
                        continue

                # Ejecutar slice
                remaining = config.total_quantity - total_filled  # type: ignore
                qty = min(slice_qty, remaining)
                if qty <= 0:
                    break

                result = await self.connector.place_market_order(symbol, side.value, qty)  # type: ignore
                fill_price = float(result.get("avgPrice", current_price))
                fill_qty = float(result.get("executedQty", qty))  # type: ignore

                total_filled += fill_qty  # type: ignore
                total_cost += fill_qty * fill_price  # type: ignore
                fills.append({"price": fill_price, "qty": fill_qty})

                self._active_twaps[twap_id]["slices_done"] = i + 1  # type: ignore
                self._active_twaps[twap_id]["filled_qty"] = total_filled  # type: ignore

                logger.info(
                    "TWAP [%s] slice %d/%d: %.6f @ $%.2f (total: %.6f)",
                    twap_id, i + 1, config.num_slices, fill_qty, fill_price, total_filled,
                )

                if i < config.num_slices - 1:
                    await asyncio.sleep(config.interval_seconds)

        except Exception as exc:
            logger.error("Error en TWAP [%s]: %s", twap_id, exc)
            self._active_twaps[twap_id]["status"] = "ERROR"

        # Resultado consolidado
        avg_price = total_cost / total_filled if total_filled > 0 else 0.0
        commission = total_cost * 0.0004  # 0.04% Binance maker fee

        self._active_twaps[twap_id]["status"] = "COMPLETED"

        return OrderResult(
            order_id=f"TWAP-{twap_id}",
            symbol=symbol,
            side=side,
            order_type=OrderType.TWAP,
            status=OrderStatus.FILLED if total_filled >= config.total_quantity * 0.95 else OrderStatus.PARTIALLY_FILLED,
            executed_qty=round(total_filled, 6),  # type: ignore
            executed_price=round(avg_price, 2),  # type: ignore
            commission=round(commission, 4),  # type: ignore
            timestamp=datetime.now(timezone.utc),
        )

    async def cancel(self, twap_id: str) -> None:
        """Cancela una ejecucion TWAP en progreso."""
        self._cancelled.add(twap_id)
        if twap_id in self._active_twaps:
            self._active_twaps[twap_id]["status"] = "CANCELLED"
        logger.info("TWAP [%s] marcado para cancelacion", twap_id)

    def get_status(self, twap_id: str) -> Dict[str, Any]:
        """Retorna el estado de una ejecucion TWAP."""
        return self._active_twaps.get(twap_id, {"status": "NOT_FOUND"})


# ══════════════════════════════════════════════════════════════════════
#  Execution Engine
# ══════════════════════════════════════════════════════════════════════

class ExecutionEngine:
    """
    Motor de ejecucion principal para NEXUS.
    Gestiona el ciclo de vida completo de ordenes:
    - Market, Limit, OCO (SL+TP) y TWAP
    - Historial de ordenes
    - Portfolio value tracking
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
    ) -> None:
        self.connector = BinanceConnector(api_key, api_secret, testnet)
        self.twap = TWAPExecutor(self.connector)
        self._order_history: List[OrderResult] = []
        self._initialized = False

    async def initialize(self) -> None:
        """Inicializa la conexion con el exchange."""
        if not self.connector.api_key or not self.connector.api_secret:
            logger.warning(
                "API keys no configuradas. ExecutionEngine en modo desconectado."
            )
            return

        try:
            await self.connector.connect()
            self._initialized = True
            logger.info("ExecutionEngine inicializado correctamente")
        except Exception as exc:
            logger.error("Error inicializando ExecutionEngine: %s", exc)

    async def shutdown(self) -> None:
        """Cierra la conexion con el exchange."""
        if self._initialized:
            await self.connector.disconnect()
            self._initialized = False

    async def execute_order(self, request: OrderRequest) -> OrderResult:
        """
        Ejecuta una orden segun su tipo.

        Para ordenes OCO: coloca la orden principal + stop loss + take profit.
        """
        if not self._initialized:
            raise ConnectionError("ExecutionEngine no inicializado")

        if request.order_type == OrderType.MARKET:
            return await self._execute_market(request)
        elif request.order_type == OrderType.LIMIT:
            return await self._execute_limit(request)
        elif request.order_type == OrderType.OCO:
            return await self._execute_oco(request)
        elif request.order_type == OrderType.TWAP:
            config = TWAPConfig(total_quantity=request.quantity)
            return await self.twap.execute(
                request.symbol, request.side, config
            )
        else:
            raise ValueError(f"Tipo de orden no soportado: {request.order_type}")

    async def _execute_market(self, request: OrderRequest) -> OrderResult:
        """Ejecuta una orden de mercado."""
        raw = await self.connector.place_market_order(
            symbol=request.symbol,
            side=request.side.value,  # type: ignore
            quantity=request.quantity,
        )
        return self._parse_binance_response(raw, request)

    async def _execute_limit(self, request: OrderRequest) -> OrderResult:
        """Ejecuta una orden limite."""
        if request.price is None:
            raise ValueError("Precio requerido para orden LIMIT")

        raw = await self.connector.place_limit_order(
            symbol=request.symbol,
            side=request.side.value,  # type: ignore
            quantity=request.quantity,
            price=request.price,  # type: ignore
        )
        return self._parse_binance_response(raw, request)

    async def _execute_oco(self, request: OrderRequest) -> OrderResult:
        """
        Ejecuta una orden OCO: coloca orden principal + SL + TP.

        En Binance Futures no hay OCO nativo, asi que simulamos con:
        1. Orden MARKET para entrar
        2. Orden STOP_MARKET como stop loss
        3. Orden TAKE_PROFIT_MARKET como take profit
        """
        # 1. Orden principal
        main_result = await self.connector.place_market_order(
            symbol=request.symbol,
            side=request.side.value,  # type: ignore
            quantity=request.quantity,
        )

        entry_price = float(main_result.get("avgPrice", 0))
        close_side = "SELL" if request.side == OrderSide.BUY else "BUY"

        # 2. Stop Loss
        if request.stop_price is not None:
            try:
                await self.connector.place_stop_market(
                    symbol=request.symbol,
                    side=close_side,
                    quantity=request.quantity,
                    stop_price=request.stop_price,  # type: ignore
                )
                logger.info(
                    "SL colocado: %s %s @ $%.2f",
                    close_side, request.symbol, request.stop_price,
                )
            except Exception as exc:
                logger.error("Error colocando SL: %s", exc)

        # 3. Take Profit
        if request.take_profit_price is not None:
            try:
                await self.connector.place_take_profit_market(
                    symbol=request.symbol,
                    side=close_side,
                    quantity=request.quantity,
                    stop_price=request.take_profit_price,  # type: ignore
                )
                logger.info(
                    "TP colocado: %s %s @ $%.2f",
                    close_side, request.symbol, request.take_profit_price,
                )
            except Exception as exc:
                logger.error("Error colocando TP: %s", exc)

        return self._parse_binance_response(main_result, request)

    def _parse_binance_response(
        self, raw: Dict[str, Any], request: OrderRequest
    ) -> OrderResult:
        """Parsea la respuesta de Binance a un OrderResult."""
        status_map = {
            "NEW": OrderStatus.SUBMITTED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
        }

        result = OrderResult(
            order_id=str(raw.get("orderId", uuid.uuid4().hex[:12])),  # type: ignore
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            status=status_map.get(raw.get("status", ""), OrderStatus.SUBMITTED),
            executed_qty=float(raw.get("executedQty", 0)),
            executed_price=float(raw.get("avgPrice", raw.get("price", 0))),  # type: ignore
            commission=float(raw.get("executedQty", 0)) * float(raw.get("avgPrice", 0)) * 0.0004,
            timestamp=datetime.now(timezone.utc),
            raw_response=raw,
        )

        self._order_history.append(result)
        logger.info(
            "Orden ejecutada: %s %s %s qty=%.6f @ $%.2f [%s]",
            result.order_type.value,
            result.side.value,
            result.symbol,
            result.executed_qty,
            result.executed_price,
            result.status.value,
        )
        return result

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> List[str]:
        """Cancela todas las ordenes abiertas."""
        if not self._initialized:
            return []

        cancelled: List[str] = []
        symbols = [symbol] if symbol else ["BTCUSDT", "ETHUSDT"]

        for sym in symbols:
            try:
                await self.connector.cancel_all_orders(sym)
                cancelled.append(sym)
            except Exception as exc:
                logger.warning("Error cancelando ordenes de %s: %s", sym, exc)

        return cancelled

    async def get_portfolio_value(self) -> float:
        """Calcula el valor total del portafolio en USDT."""
        if not self._initialized:
            return 0.0

        balances = await self.connector.get_account_balance()
        return balances.get("USDT", 0.0)

    def get_order_history(self) -> pd.DataFrame:
        """Retorna el historial de ordenes como DataFrame."""
        if not self._order_history:
            return pd.DataFrame()

        records = [
            {
                "order_id": o.order_id,
                "symbol": o.symbol,
                "side": o.side.value,
                "type": o.order_type.value,
                "status": o.status.value,
                "qty": o.executed_qty,
                "price": o.executed_price,
                "commission": o.commission,
                "timestamp": o.timestamp,
            }
            for o in self._order_history
        ]
        return pd.DataFrame(records)

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """Retorna las posiciones abiertas actuales."""
        if not self._initialized:
            return []
        return await self.connector.get_open_positions()

    def __repr__(self) -> str:
        status = "CONNECTED" if self._initialized else "DISCONNECTED"
        mode = "TESTNET" if self.connector.testnet else "LIVE"
        return f"<ExecutionEngine status={status} mode={mode} orders={len(self._order_history)}>"
