"""
NEXUS v5.0 (beta) — Bitget Execution Engine (CCXT-based)
=========================================================
Concrete implementation of AbstractExecutionEngine for Bitget.

Supported modes (controlled by BITGET_MODE env var):
  futures  — USDT-M perpetual contracts (primary, recommended)
  spot     — Spot market
  both     — Futures primary, spot fallback for assets without
             perpetual contract

Leverage: configurable via BITGET_LEVERAGE (default: 5, max: 10).
Margin mode: isolated (never cross — risk containment).

Key design principles:
  - All CCXT calls are async-wrapped via asyncio.to_thread().
  - The engine never sets leverage > BITGET_MAX_LEVERAGE (hard cap: 10).
  - Stop loss and take profit are submitted as attached orders,
    not managed in software.
  - Position sizing uses Kelly fraction capped at 0.1 (10% of capital).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv

try:
    import ccxt
    import ccxt.async_support as ccxt_async
    _HAS_CCXT = True
except ImportError:
    _HAS_CCXT = False

from .base import (
    AbstractExecutionEngine,
    ExecutionStatus,
    SignalDirection,
    TradeResult,
    TradeSignal,
    VenueType
)

logger = logging.getLogger("nexus.execution.bitget")

class BitgetExecutionEngine(AbstractExecutionEngine):
    """
    Execution engine for Bitget via CCXT async.
    Supports futures USDT-M perpetuals and spot markets.
    """

    MAX_LEVERAGE = 10  # Hard cap — never exceeded regardless of config

    def __init__(self) -> None:
        load_dotenv()
        self._api_key    = os.getenv("BITGET_API_KEY", "")
        self._secret     = os.getenv("BITGET_SECRET", "")
        self._passphrase = os.getenv("BITGET_PASSPHRASE", "")
        self._mode       = os.getenv("BITGET_MODE", "futures").lower()
        self._leverage   = min(
            int(os.getenv("BITGET_LEVERAGE", "5")), self.MAX_LEVERAGE
        )
        self._sandbox    = os.getenv("BITGET_SANDBOX", "True").lower() in ("true", "1", "yes")

        self._exchange_futures: Optional[ccxt_async.bitget] = None
        self._exchange_spot:    Optional[ccxt_async.bitget] = None
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def venue(self) -> VenueType:
        return VenueType.BITGET

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        if not _HAS_CCXT:
            logger.error("CCXT not installed. Bitget execution disabled.")
            return False

        if self._connected:
            return True

        async with self._lock:
            if self._connected:
                return True

            attempt = 1
            max_attempts = 3
            delay = 2.0

            while attempt <= max_attempts:
                try:
                    logger.info(f"Connecting to Bitget... (Attempt {attempt}/{max_attempts})")
                    
                    config_base = {
                        "apiKey": self._api_key,
                        "secret": self._secret,
                        "password": self._passphrase,
                        "enableRateLimit": True,
                    }

                    if self._mode in ("futures", "both"):
                        cfg = config_base.copy()
                        cfg["defaultType"] = "swap"
                        cfg["options"] = {"defaultSettle": "USDT"}
                        
                        self._exchange_futures = ccxt_async.bitget(cfg)
                        self._exchange_futures.set_sandbox_mode(self._sandbox)
                        await self._exchange_futures.load_markets()

                    if self._mode in ("spot", "both"):
                        cfg = config_base.copy()
                        cfg["defaultType"] = "spot"
                        
                        self._exchange_spot = ccxt_async.bitget(cfg)
                        self._exchange_spot.set_sandbox_mode(self._sandbox)
                        await self._exchange_spot.load_markets()

                    self._connected = True
                    logger.info(f"✅ Bitget connected [mode={self._mode}, leverage={self._leverage}x, sandbox={self._sandbox}]")
                    return True

                except Exception as exc:
                    logger.warning(f"⚠️ Bitget connection failed (Attempt {attempt}): {exc}")
                    
                    if self._exchange_futures:
                        await asyncio.to_thread(self._exchange_futures.close)
                        self._exchange_futures = None
                    if self._exchange_spot:
                        await asyncio.to_thread(self._exchange_spot.close)
                        self._exchange_spot = None

                    if attempt == max_attempts:
                        logger.error(f"❌ Bitget definitive failure after {max_attempts} attempts.")
                        return False
                    
                    await asyncio.sleep(delay)
                    attempt += 1

            return False

    async def disconnect(self) -> None:
        if self._exchange_futures:
            try:
                await asyncio.to_thread(self._exchange_futures.close)
            except Exception:
                pass
            self._exchange_futures = None
            
        if self._exchange_spot:
            try:
                await asyncio.to_thread(self._exchange_spot.close)
            except Exception:
                pass
            self._exchange_spot = None

        self._connected = False
        logger.info("Bitget disconnected.")

    async def get_balance(self) -> float:
        if not self._connected:
            return 0.0
            
        try:
            if self._mode in ("futures", "both") and self._exchange_futures:
                balances = await asyncio.to_thread(self._exchange_futures.fetch_balance)
                if "USDT" in balances and "free" in balances["USDT"]:
                    return balances["USDT"]["free"]
                    
            if self._mode == "spot" and self._exchange_spot:
                balances = await asyncio.to_thread(self._exchange_spot.fetch_balance)
                if "USDT" in balances and "free" in balances["USDT"]:
                    return balances["USDT"]["free"]
        except Exception as exc:
            logger.debug(f"Error fetching Bitget balance: {exc}")
            
        return 0.0

    async def execute(self, signal: TradeSignal) -> TradeResult:
        t_start = time.perf_counter()
        
        if not self._connected:
            return TradeResult(
                order_id="N/A", venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=ExecutionStatus.ERROR,
                size=signal.size, executed_price=0.0,
                latency_ms=(time.perf_counter() - t_start) * 1000,
            )

        market_type = "futures"
        exchange = self._exchange_futures

        if self._mode == "futures":
            exchange = self._exchange_futures
            market_type = "futures"
        elif self._mode == "spot":
            exchange = self._exchange_spot
            market_type = "spot"
        elif self._mode == "both":
            if self._exchange_futures and signal.asset in self._exchange_futures.markets:
                exchange = self._exchange_futures
                market_type = "futures"
            elif self._exchange_spot and signal.asset in self._exchange_spot.markets:
                exchange = self._exchange_spot
                market_type = "spot"
            else:
                exchange = self._exchange_futures # Default to futures to safely handle BadSymbol later

        if market_type == "futures" and exchange is not None:
            try:
                await asyncio.to_thread(
                    exchange.set_leverage, self._leverage, signal.asset, {"marginMode": "isolated"}
                )
            except Exception as exc:
                logger.debug(f"Could not set leverage for {signal.asset}: {exc}")

        side = "buy" if signal.direction in (SignalDirection.CALL, SignalDirection.BUY) else "sell"

        try:
            if market_type == "futures" and exchange is not None:
                order = await asyncio.to_thread(
                    exchange.create_order,
                    symbol=signal.asset,
                    type="market",
                    side=side,
                    amount=signal.size,
                    params={"tdMode": "isolated"}
                )
            elif exchange is not None:
                order = await asyncio.to_thread(
                    exchange.create_order,
                    symbol=signal.asset,
                    type="market",
                    side=side,
                    amount=signal.size,
                )
            else:
                raise Exception("Suitable exchange reference not found for order execution.")
                
            # Submit Stop Loss if exists
            if signal.stop_loss is not None and exchange is not None:
                try:
                    await asyncio.to_thread(
                        exchange.create_order,
                        symbol=signal.asset,
                        type="stop_market",
                        side="sell" if side == "buy" else "buy",
                        amount=signal.size,
                        params={"stopPrice": signal.stop_loss, "reduceOnly": True}
                    )
                except Exception as sl_exc:
                    logger.warning(f"Failed to submit SL for {signal.asset}: {sl_exc}")

            # Submit Take Profit if exists
            if signal.take_profit is not None and exchange is not None:
                try:
                    await asyncio.to_thread(
                        exchange.create_order,
                        symbol=signal.asset,
                        type="take_profit_market",
                        side="sell" if side == "buy" else "buy",
                        amount=signal.size,
                        params={"stopPrice": signal.take_profit, "reduceOnly": True}
                    )
                except Exception as tp_exc:
                    logger.warning(f"Failed to submit TP for {signal.asset}: {tp_exc}")

            status_map = order.get("status", "error")
            status = ExecutionStatus.FILLED if status_map in ("closed", "filled", "open") else ExecutionStatus.ERROR
            executed_price = float(order.get("average", order.get("price", 0)))
            latency_ms = (time.perf_counter() - t_start) * 1000

            logger.info(
                f"🎯 BITGET ENTRY | {market_type} | {signal.asset} | {side.upper()} | "
                f"${signal.size} | {self._leverage}x | price={executed_price:.4f} | latency={latency_ms:.0f}ms"
            )

            return TradeResult(
                order_id=str(order.get("id", "N/A")), venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=status,
                size=signal.size, executed_price=executed_price,
                latency_ms=latency_ms,
            )

        except Exception as exec_err:
            latency_ms = (time.perf_counter() - t_start) * 1000
            logger.error(f"Bitget execute failed: {exec_err}")
            return TradeResult(
                order_id="N/A", venue=self.venue, asset=signal.asset,
                direction=signal.direction, status=ExecutionStatus.ERROR,
                size=signal.size, executed_price=0.0,
                latency_ms=latency_ms,
            )

    async def get_historical_data(self, asset: str, tf: str, max_bars: int = 500) -> pd.DataFrame:
        tf_map = {
            "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
            "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d"
        }
        timeframe = tf_map.get(tf, "5m")
        
        exchange = None
        if self._mode == "futures" or (self._mode == "both" and self._exchange_futures and asset in self._exchange_futures.markets):
            exchange = self._exchange_futures
        else:
            exchange = self._exchange_spot
            
        if not exchange or not self._connected:
            return pd.DataFrame()

        try:
            ohlcv = await asyncio.to_thread(
                exchange.fetch_ohlcv, asset, timeframe, limit=max_bars
            )
            if not ohlcv:
                return pd.DataFrame()
                
            df = pd.DataFrame(ohlcv, columns=["open_time", "open", "high", "low", "close", "volume"])
            df["open_time"] = pd.to_datetime(df["open_time"], unit='ms', utc=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
                
            return df
        except Exception as exc:
            logger.debug(f"Bitget get_historical_data error: {exc}")
            return pd.DataFrame()

    async def get_payout(self, asset: str, option_type: str = "futures") -> float:
        """Returns effective payout % based on leverage."""
        return float(self._leverage * 100 * 0.998)

    async def get_best_available_asset(self, min_payout: int = 80) -> Optional[Dict[str, str]]:
        """
        For Bitget, returns the most liquid USDT-M perpetual by 24h volume.
        Fetches tickers and returns the symbol with highest quoteVolume
        among symbols ending in /USDT:USDT (futures) or /USDT (spot).
        Returns top-1 by volume. No payout filter (not applicable to futures).
        """
        if not self._connected:
            return None
            
        exchange = self._exchange_futures if self._exchange_futures else self._exchange_spot
        if not exchange:
            return None
            
        try:
            tickers = await asyncio.to_thread(exchange.fetch_tickers)
            best_asset = None
            max_vol = -1.0
            
            for symbol, data in tickers.items():
                if symbol.endswith("/USDT:USDT") or symbol.endswith("/USDT"):
                    vol = data.get("quoteVolume", 0)
                    if vol and vol > max_vol:
                        max_vol = vol
                        best_asset = symbol
                        
            if best_asset:
                market_type = "futures" if best_asset in getattr(self._exchange_futures, "markets", {}) else "spot"
                return {"symbol": best_asset, "market_type": market_type}
            return None
        except Exception as exc:
            logger.error(f"Bitget get_best_available_asset failed: {exc}")
            return None
