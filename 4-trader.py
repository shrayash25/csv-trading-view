import json
from typing import Any, Dict, List, Optional, Tuple
from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState

POSITION_LIMIT = 80

OSMIUM = "ASH_COATED_OSMIUM"
PEPPER = "INTARIAN_PEPPER_ROOT"

# ── Osmium: pure market-making on a mean-reverting, zero-drift instrument ─────
OSMIUM_HALF_SPREAD = 4              # [FIX #3] tighter from 7 -> 4 for more fills
OSMIUM_SPREAD_COLLAPSE_THRESH = 5   # widen when market spread drops dangerously low
OSMIUM_WIDE_HALF_SPREAD = 8         # fallback when spread collapses
OSMIUM_INV_SKEW_COEFF = 0.08        # [FIX #7] reduced from 0.15 — let positions grow
OSMIUM_EMA_ALPHA = 0.035            # [FIX #5] slightly lower alpha, no anchor blend
OSMIUM_TAKE_MARGIN = 2              # [FIX #8] buy asks up to fair+2, sell bids down to fair-2

# ── Pepper Root: trend-following + skewed market-making ───────────────────────
PEPPER_DRIFT_PER_TICK = 0.10
PEPPER_BID_OFFSET = 3               # even tighter on buy side to accumulate longs faster
PEPPER_ASK_OFFSET = 11              # wider ask — only sell at a real premium
PEPPER_TREND_BASE_POS = 68          # [FIX #1] target near limit to ride the +1000/day trend
PEPPER_INV_SKEW_COEFF = 0.06        # [FIX #7] reduced from 0.12 — slow to unwind longs
PEPPER_EMA_ALPHA = 0.20             # faster to track the trending price without lagging
PEPPER_TAKE_BUY_MARGIN = 2          # [FIX #1] buy asks up to fair+2 (aggressive accumulation)
PEPPER_TAKE_SELL_MARGIN = 5         # [FIX #2] only sell bids above fair+5 (protect trend)


class Trader:
    """
    Production trading algorithm for IMC Prosperity 4, Round 1 (v2).

    Fixes applied vs v1:
      1. Pepper target position 30 -> 68; aggressive buy-side taking up to fair+2
      2. Pepper sell threshold fair+2 -> fair+5
      3. Osmium quoting spread 7 -> 4
      4. Multi-level quoting (3 layers) on both products
      5. Removed 0.7/0.3 anchor blend on Osmium fair value
      6. Volume-weighted mid-price using all book levels
      7. Reduced inventory skew coefficients on both products
      8. Widened aggressive taking thresholds on Osmium (fair+2 / fair-2)
    """

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}
        conversions = 0

        trader_state = self._load_state(state.traderData)

        if OSMIUM in state.order_depths:
            result[OSMIUM] = self._trade_osmium(state, trader_state)

        if PEPPER in state.order_depths:
            result[PEPPER] = self._trade_pepper(state, trader_state)

        trader_data = json.dumps(trader_state, cls=ProsperityEncoder)
        return result, conversions, trader_data

    # ── Ash-Coated Osmium: Pure Market Making ─────────────────────────────

    def _trade_osmium(self, state: TradingState, ts: dict) -> List[Order]:
        orders: List[Order] = []
        order_depth = state.order_depths[OSMIUM]
        position = state.position.get(OSMIUM, 0)

        mid = self._vwap_mid(order_depth)
        spread = self._calc_spread(order_depth)

        # [FIX #5] Pure EMA — no anchor blend. The EMA tracks the true market.
        prev_ema = ts.get("osmium_ema", 10_000.0)
        if mid is not None:
            ema = OSMIUM_EMA_ALPHA * mid + (1 - OSMIUM_EMA_ALPHA) * prev_ema
            fair = ema
        else:
            fair = prev_ema
            ema = prev_ema
        ts["osmium_ema"] = ema

        if spread is not None and spread < OSMIUM_SPREAD_COLLAPSE_THRESH:
            half_spread = OSMIUM_WIDE_HALF_SPREAD
        else:
            half_spread = OSMIUM_HALF_SPREAD

        skew = OSMIUM_INV_SKEW_COEFF * position

        buy_capacity = POSITION_LIMIT - position
        sell_capacity = POSITION_LIMIT + position

        # Layer 1: Aggressively take mispriced orders
        # [FIX #8] Buy asks up to fair + margin, sell bids down to fair - margin
        buy_take = int(round(fair + OSMIUM_TAKE_MARGIN))
        sell_take = int(round(fair - OSMIUM_TAKE_MARGIN))

        orders += self._take_sells_below(order_depth, buy_take, buy_capacity, OSMIUM)
        filled_buy = sum(o.quantity for o in orders if o.quantity > 0)
        buy_capacity -= filled_buy

        orders += self._take_buys_above(order_depth, sell_take, sell_capacity, OSMIUM)
        filled_sell = sum(-o.quantity for o in orders if o.quantity < 0)
        sell_capacity -= filled_sell

        # [FIX #4] Layer 2: Multi-level passive quotes (3 tiers)
        orders += self._multilevel_quotes(
            OSMIUM, fair, half_spread, skew, buy_capacity, sell_capacity,
            layers=3, layer_step=2,
        )

        return orders

    # ── Intarian Pepper Root: Trend-Following + Skewed MM ─────────────────

    def _trade_pepper(self, state: TradingState, ts: dict) -> List[Order]:
        orders: List[Order] = []
        order_depth = state.order_depths[PEPPER]
        position = state.position.get(PEPPER, 0)

        mid = self._vwap_mid(order_depth)

        prev_ema = ts.get("pepper_ema", None)
        if mid is not None:
            if prev_ema is not None:
                ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * prev_ema
            else:
                ema = mid
            fair = ema + PEPPER_DRIFT_PER_TICK
        else:
            fair = prev_ema if prev_ema is not None else 12000.0
            ema = fair
        ts["pepper_ema"] = ema

        inventory_deviation = position - PEPPER_TREND_BASE_POS
        skew = PEPPER_INV_SKEW_COEFF * inventory_deviation

        buy_capacity = POSITION_LIMIT - position
        sell_capacity = POSITION_LIMIT + position

        # [FIX #1] Aggressive buy-side: take asks up to fair + margin
        buy_take = int(round(fair + PEPPER_TAKE_BUY_MARGIN))
        orders += self._take_sells_below(order_depth, buy_take, buy_capacity, PEPPER)
        filled_buy = sum(o.quantity for o in orders if o.quantity > 0)
        buy_capacity -= filled_buy

        # [FIX #2] Conservative sell-side: only sell bids above fair + margin
        sell_take = int(round(fair + PEPPER_TAKE_SELL_MARGIN))
        orders += self._take_buys_above(order_depth, sell_take, sell_capacity, PEPPER)
        filled_sell = sum(-o.quantity for o in orders if o.quantity < 0)
        sell_capacity -= filled_sell

        # [FIX #4] Multi-level skewed passive quotes (3 tiers)
        orders += self._multilevel_quotes(
            PEPPER, fair, PEPPER_BID_OFFSET, skew, buy_capacity, sell_capacity,
            layers=3, layer_step=2,
            ask_base_offset=PEPPER_ASK_OFFSET,
        )

        return orders

    # ── Order-book helpers ────────────────────────────────────────────────

    def _vwap_mid(self, od: OrderDepth) -> Optional[float]:
        """[FIX #6] Volume-weighted mid-price using all available book levels."""
        if not od.buy_orders or not od.sell_orders:
            if od.buy_orders:
                return float(max(od.buy_orders.keys()))
            if od.sell_orders:
                return float(min(od.sell_orders.keys()))
            return None

        bid_vwap_num = sum(price * vol for price, vol in od.buy_orders.items())
        bid_vwap_den = sum(vol for vol in od.buy_orders.values())

        ask_vwap_num = sum(price * abs(vol) for price, vol in od.sell_orders.items())
        ask_vwap_den = sum(abs(vol) for vol in od.sell_orders.values())

        if bid_vwap_den == 0 or ask_vwap_den == 0:
            best_bid = max(od.buy_orders.keys())
            best_ask = min(od.sell_orders.keys())
            return (best_bid + best_ask) / 2.0

        bid_vwap = bid_vwap_num / bid_vwap_den
        ask_vwap = ask_vwap_num / ask_vwap_den
        return (bid_vwap + ask_vwap) / 2.0

    def _calc_spread(self, od: OrderDepth) -> Optional[float]:
        if od.buy_orders and od.sell_orders:
            return min(od.sell_orders.keys()) - max(od.buy_orders.keys())
        return None

    def _multilevel_quotes(
        self,
        symbol: str,
        fair: float,
        bid_base_offset: float,
        skew: float,
        buy_capacity: int,
        sell_capacity: int,
        layers: int = 3,
        layer_step: int = 2,
        ask_base_offset: Optional[float] = None,
    ) -> List[Order]:
        """
        [FIX #4] Place multiple passive order layers instead of a single level.

        Splits remaining capacity across `layers` price tiers. The innermost
        layer gets the most size (50%), then 30%, then 20% for 3 layers.
        """
        if ask_base_offset is None:
            ask_base_offset = bid_base_offset

        orders: List[Order] = []
        size_weights = [0.50, 0.30, 0.20][:layers]
        total_w = sum(size_weights)
        size_weights = [w / total_w for w in size_weights]

        for i in range(layers):
            offset_extra = i * layer_step
            bid_price = int(round(fair - bid_base_offset - offset_extra - skew))
            ask_price = int(round(fair + ask_base_offset + offset_extra - skew))

            bid_qty = max(1, int(round(buy_capacity * size_weights[i])))
            ask_qty = max(1, int(round(sell_capacity * size_weights[i])))

            if buy_capacity > 0 and bid_qty > 0:
                actual_bid = min(bid_qty, buy_capacity)
                orders.append(Order(symbol, bid_price, actual_bid))
                buy_capacity -= actual_bid

            if sell_capacity > 0 and ask_qty > 0:
                actual_ask = min(ask_qty, sell_capacity)
                orders.append(Order(symbol, ask_price, -actual_ask))
                sell_capacity -= actual_ask

        return orders

    def _take_sells_below(
        self, od: OrderDepth, threshold: int, capacity: int, symbol: str
    ) -> List[Order]:
        """Buy against any sell orders priced at or below threshold."""
        orders: List[Order] = []
        if capacity <= 0:
            return orders
        remaining = capacity
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > threshold or remaining <= 0:
                break
            ask_vol = -od.sell_orders[ask_price]
            fill = min(ask_vol, remaining)
            if fill > 0:
                orders.append(Order(symbol, ask_price, fill))
                remaining -= fill
        return orders

    def _take_buys_above(
        self, od: OrderDepth, threshold: int, capacity: int, symbol: str
    ) -> List[Order]:
        """Sell against any buy orders priced at or above threshold."""
        orders: List[Order] = []
        if capacity <= 0:
            return orders
        remaining = capacity
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < threshold or remaining <= 0:
                break
            bid_vol = od.buy_orders[bid_price]
            fill = min(bid_vol, remaining)
            if fill > 0:
                orders.append(Order(symbol, bid_price, -fill))
                remaining -= fill
        return orders

    # ── State persistence ─────────────────────────────────────────────────

    def _load_state(self, trader_data: str) -> dict:
        if trader_data and trader_data.strip():
            try:
                return json.loads(trader_data)
            except (json.JSONDecodeError, TypeError):
                pass
        return {}
