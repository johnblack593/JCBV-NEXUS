import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict

from dotenv import load_dotenv

from .execution.bitget_engine import BitgetExecutionEngine

logger = logging.getLogger("nexus.position_manager")


@dataclass
class OpenPosition:
    """Represents a single open futures position being managed."""
    asset:          str
    direction:      str          # "buy" | "sell"
    entry_price:    float
    original_size:  float        # Size at entry, never changes
    remaining_size: float        # Decreases with partial closes
    stop_loss:      float
    take_profit:    float
    atr_at_entry:   float        # ATR when position was opened
    opened_at:      float = field(default_factory=time.time)

    # State flags — all False at open, set to True when each fires
    breakeven_active: bool = False
    partial_tier_1:   bool = False
    partial_tier_2:   bool = False
    is_closed:        bool = False

    # For trailing stop — tracks peak price in position direction
    peak_price:      float = 0.0


class PositionManager:
    """
    Active position management for Bitget futures.
    Runs as a background asyncio task alongside NexusPipeline.

    Manages:
      - Breakeven trigger (SL to entry after initial gain)
      - Adaptive ATR trailing stop (tracks peak, tightens on reversal)
      - Partial close ladder (40% -> 35% -> 25% runner)
      - Multi-TF trend filter (optional 4h filter, default off)
    """

    def __init__(self, engine: BitgetExecutionEngine) -> None:
        load_dotenv()
        self._engine = engine
        self._positions: Dict[str, OpenPosition] = {}  # key: asset
        self._lock = asyncio.Lock()
        self._running = False

        # Config from env
        self._breakeven_pct    = float(os.getenv("BITGET_BREAKEVEN_PCT",    "1.0"))
        self._trail_atr_mult   = float(os.getenv("BITGET_TRAIL_ATR_MULT",   "1.5"))
        self._partial_t1_atr   = float(os.getenv("BITGET_PARTIAL_T1_ATR",   "2.0"))
        self._partial_t2_atr   = float(os.getenv("BITGET_PARTIAL_T2_ATR",   "4.0"))
        self._htf_filter       = os.getenv("BITGET_HTF_FILTER", "False").lower() in ("true","1")
        self._tick_interval    = int(os.getenv("BITGET_POSITION_TICK",       "15"))

    def register_position(self, position: OpenPosition) -> None:
        """Called by the pipeline immediately after a successful entry."""
        position.peak_price = position.entry_price
        self._positions[position.asset] = position
        logger.info(
            f"📋 POSITION REGISTERED — {position.asset} | "
            f"{position.direction.upper()} | entry={position.entry_price:.4f} | "
            f"size={position.original_size} | SL={position.stop_loss:.4f} | "
            f"TP={position.take_profit:.4f}"
        )

    async def start(self) -> None:
        """Starts the background management loop."""
        self._running = True
        logger.info("🔁 PositionManager started.")
        await self._management_loop()

    async def stop(self) -> None:
        """Gracefully stops the management loop."""
        self._running = False
        logger.info("PositionManager stopped.")

    async def _management_loop(self) -> None:
        """Main management loop — ticks every BITGET_POSITION_TICK seconds."""
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"PositionManager tick error: {exc}", exc_info=True)
            await asyncio.sleep(self._tick_interval)

    async def _tick(self) -> None:
        """Process all open positions in one tick."""
        if not self._positions:
            return

        async with self._lock:
            closed_assets = []

            for asset, pos in self._positions.items():
                if pos.is_closed:
                    closed_assets.append(asset)
                    continue

                try:
                    await self._process_position(asset, pos)
                except Exception as exc:
                    logger.error(f"Error processing position {asset}: {exc}")

            for asset in closed_assets:
                del self._positions[asset]

    async def _process_position(self, asset: str, pos: OpenPosition) -> None:
        """Apply all exit mechanics to a single position."""
        current_price = await self._get_current_price(asset)
        if current_price <= 0:
            return

        df = await self._engine.get_historical_data(asset, "5m", max_bars=50)
        if df.empty or len(df) < 20:
            return

        df = df.copy()
        df['prev_close'] = df['close'].shift(1)
        df['tr'] = df[['high', 'low', 'prev_close']].apply(
            lambda r: max(r['high']-r['low'],
                          abs(r['high']-r['prev_close']),
                          abs(r['low']-r['prev_close'])), axis=1
        )
        current_atr = float(df['tr'].rolling(14).mean().iloc[-1])

        if pos.direction == "buy":
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
        else:
            pnl_pct = (pos.entry_price - current_price) / pos.entry_price * 100

        if pos.direction == "buy":
            pos.peak_price = max(pos.peak_price, current_price)
        else:
            pos.peak_price = min(pos.peak_price, current_price)

        if self._htf_filter:
            await self._check_htf_filter(asset, pos, current_atr)

        if not pos.breakeven_active and pnl_pct >= self._breakeven_pct:
            fee_buffer = pos.entry_price * 0.001
            new_sl = pos.entry_price + fee_buffer if pos.direction == "buy" else \
                     pos.entry_price - fee_buffer
            if self._is_sl_improvement(pos, new_sl):
                pos.stop_loss = new_sl
                pos.breakeven_active = True
                await self._update_sl_on_exchange(asset, pos, new_sl)
                logger.info(
                    f"🔒 BREAKEVEN triggered — {asset} | "
                    f"SL → {new_sl:.4f} | pnl={pnl_pct:.2f}%"
                )

        gain_in_atr = pnl_pct / (pos.atr_at_entry / pos.entry_price * 100) \
                      if pos.atr_at_entry > 0 else 0

        if not pos.partial_tier_1 and gain_in_atr >= self._partial_t1_atr:
            close_size = pos.original_size * 0.40
            await self._partial_close(asset, pos, close_size, tier=1)

        if pos.partial_tier_1 and not pos.partial_tier_2 \
                and gain_in_atr >= self._partial_t2_atr:
            close_size = pos.original_size * 0.35
            await self._partial_close(asset, pos, close_size, tier=2)

        if pos.breakeven_active:
            trail_dist  = self._trail_atr_mult * current_atr
            if pos.direction == "buy":
                trail_level = pos.peak_price - trail_dist
                if trail_level > pos.stop_loss:
                    pos.stop_loss = trail_level
                    await self._update_sl_on_exchange(asset, pos, trail_level)
                    logger.info(f"📈 TRAIL — {asset} | SL → {trail_level:.4f}")
            else:
                trail_level = pos.peak_price + trail_dist
                if trail_level < pos.stop_loss:
                    pos.stop_loss = trail_level
                    await self._update_sl_on_exchange(asset, pos, trail_level)
                    logger.info(f"📈 TRAIL — {asset} | SL → {trail_level:.4f}")

    async def _get_current_price(self, asset: str) -> float:
        """Fetches last close price from the most recent 5m candle."""
        try:
            df = await self._engine.get_historical_data(asset, "5m", max_bars=2)
            return float(df['close'].iloc[-1]) if not df.empty else 0.0
        except Exception:
            return 0.0

    def _is_sl_improvement(self, pos: OpenPosition, new_sl: float) -> bool:
        """Returns True if new_sl is strictly better than current stop."""
        if pos.direction == "buy":
            return new_sl > pos.stop_loss
        return new_sl < pos.stop_loss

    async def _partial_close(
        self, asset: str, pos: OpenPosition, size: float, tier: int
    ) -> None:
        """
        Submits a reduce-only market order to close `size` of the position.
        Updates remaining_size on success.
        """
        try:
            exchange = self._engine._exchange_futures or self._engine._exchange_spot
            if not exchange:
                return

            close_side = "sell" if pos.direction == "buy" else "buy"
            order = await asyncio.to_thread(
                exchange.create_order,
                symbol=asset,
                type="market",
                side=close_side,
                amount=size,
                params={"reduceOnly": True}
            )

            if order.get("status") in ("closed", "filled", "open"):
                pos.remaining_size -= size
                if tier == 1:
                    pos.partial_tier_1 = True
                elif tier == 2:
                    pos.partial_tier_2 = True

                price = float(order.get("average", order.get("price", 0)))
                logger.info(
                    f"💰 PARTIAL CLOSE T{tier} — {asset} | "
                    f"closed {size:.4f} ({size/pos.original_size*100:.0f}%) "
                    f"at {price:.4f} | remaining={pos.remaining_size:.4f}"
                )
        except Exception as exc:
            logger.error(f"Partial close T{tier} failed for {asset}: {exc}")

    async def _update_sl_on_exchange(
        self, asset: str, pos: OpenPosition, new_sl: float
    ) -> None:
        """
        Cancels existing SL order and submits a new one at new_sl.
        """
        try:
            exchange = self._engine._exchange_futures or self._engine._exchange_spot
            if not exchange:
                return

            try:
                open_orders = await asyncio.to_thread(
                    exchange.fetch_open_orders, asset
                )
                for order in open_orders:
                    if order.get("type") in ("stop_market", "stop"):
                        await asyncio.to_thread(
                            exchange.cancel_order, order["id"], asset
                        )
            except Exception as cancel_exc:
                logger.warning(f"Could not cancel existing SL for {asset}: {cancel_exc}")

            close_side = "sell" if pos.direction == "buy" else "buy"
            await asyncio.to_thread(
                exchange.create_order,
                symbol=asset,
                type="stop_market",
                side=close_side,
                amount=pos.remaining_size,
                params={"stopPrice": new_sl, "reduceOnly": True}
            )
        except Exception as exc:
            logger.warning(f"_update_sl_on_exchange failed for {asset}: {exc}")

    async def _check_htf_filter(
        self, asset: str, pos: OpenPosition, current_atr: float
    ) -> None:
        """
        Optional 4h trend filter.
        """
        try:
            df_4h = await self._engine.get_historical_data(asset, "4h", max_bars=30)
            if df_4h.empty or len(df_4h) < 22:
                return

            ema_fast = float(df_4h['close'].ewm(span=9, adjust=False).mean().iloc[-1])
            ema_slow = float(df_4h['close'].ewm(span=21, adjust=False).mean().iloc[-1])

            trend_conflict = (
                (pos.direction == "buy"  and ema_fast < ema_slow) or
                (pos.direction == "sell" and ema_fast > ema_slow)
            )

            if trend_conflict and not pos.partial_tier_1:
                logger.info(
                    f"⚠️  HTF CONFLICT — {asset} | 4h trend contradicts position. "
                    f"Accelerating T1 partial close."
                )
                close_size = pos.original_size * 0.40
                await self._partial_close(asset, pos, close_size, tier=1)

        except Exception as exc:
            logger.debug(f"HTF filter check failed for {asset}: {exc}")
