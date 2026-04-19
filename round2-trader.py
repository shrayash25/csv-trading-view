import json
from typing import Dict, List, Optional, Tuple
from datamodel import Order, OrderDepth, ProsperityEncoder, TradingState

OSMIUM = "ASH_COATED_OSMIUM"
PEPPER = "INTARIAN_PEPPER_ROOT"
POSITION_LIMIT = 80

# ── Osmium parameters ────────────────────────────────────────────────
OSMIUM_EMA_ALPHA = 0.5
OSMIUM_TAKE_MARGIN = 1
OSMIUM_POSITION_REDUCE_THRESHOLD = 50
OSMIUM_PENNY_JUMP = 1
OSMIUM_IMB_SKEW_COEFF = 3.0
OSMIUM_TIGHT_SPREAD_THRESH = 10
OSMIUM_TIGHT_TAKE_MARGIN = 3
OSMIUM_NORMAL_HALF_SPREAD = 7
OSMIUM_WIDE_HALF_SPREAD = 6
OSMIUM_TIGHT_HALF_SPREAD = 10
OSMIUM_WIDE_SPREAD_THRESH = 18
OSMIUM_INV_SKEW_COEFF = 0.12
OSMIUM_LAYER_OFFSETS = (0, 2, 4)
OSMIUM_LAYER_WEIGHTS = (0.50, 0.30, 0.20)
OSMIUM_STRONG_IMB_THRESH = 0.4
OSMIUM_STRONG_TAKE_MARGIN = 4

# ── Pepper parameters (asymmetric skewed MM for +1000/day trend) ─────
PEPPER_DRIFT_PER_TICK = 0.10
PEPPER_EMA_ALPHA = 0.20
PEPPER_TAKE_BUY_MARGIN = 8
PEPPER_TAKE_BUY_MARGIN_CHURN = 3
PEPPER_TAKE_SELL_MARGIN = 5
PEPPER_BID_OFFSET = 3
PEPPER_ASK_OFFSET = 11
PEPPER_CHURN_ASK_OFFSET = 7
PEPPER_TARGET_POS = 68
PEPPER_INV_SKEW_COEFF = 0.06
PEPPER_CHURN_THRESHOLD = 75
PEPPER_CHURN_RESIDUAL_THRESH = 2.0


class Trader:
    """
    Round 2 trading algorithm.

    Osmium  — symmetric market-making (FV-taking, penny-jumping, position-reduction)
    Pepper  — asymmetric skewed MM riding the +1000/day linear trend
    """

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}
        trader_state = self._load_state(state.traderData)

        if OSMIUM in state.order_depths:
            result[OSMIUM] = self._trade_osmium(state, trader_state)

        if PEPPER in state.order_depths:
            result[PEPPER] = self._trade_pepper(state, trader_state)

        return result, 0, json.dumps(trader_state, cls=ProsperityEncoder)

    def _trade_osmium(self, state: TradingState, ts: dict) -> List[Order]:
        orders: List[Order] = []
        od = state.order_depths[OSMIUM]
        position = state.position.get(OSMIUM, 0)

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        # --- Tactic 1: Imbalance-aware FV ---
        imbalance = self._l1_imbalance(od)
        fair = self._compute_osmium_fv(od, ts, imbalance)
        if fair is None:
            return orders

        spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None

        buy_capacity = POSITION_LIMIT - position
        sell_capacity = POSITION_LIMIT + position

        # --- Tactic 2: Tight-spread directional taking ---
        if spread is not None and spread <= OSMIUM_TIGHT_SPREAD_THRESH and imbalance is not None:
            tight_orders, tb, ts_ = self._tight_spread_take(
                od, fair, imbalance, buy_capacity, sell_capacity
            )
            orders += tight_orders
            buy_capacity -= tb
            sell_capacity -= ts_
            position += tb - ts_

        # --- FV-taking ---
        take_orders, buy_taken, sell_taken = self._fv_take(
            od, fair, buy_capacity, sell_capacity
        )
        orders += take_orders
        buy_capacity -= buy_taken
        sell_capacity -= sell_taken
        position += buy_taken - sell_taken

        # --- Position-reduction at FV ---
        reduce_orders, buy_reduced, sell_reduced = self._position_reduce(
            od, fair, position, buy_capacity, sell_capacity
        )
        orders += reduce_orders
        buy_capacity -= buy_reduced
        sell_capacity -= sell_reduced

        # --- Tactic 3: Adaptive-width quoting with penny-jumping ---
        orders += self._adaptive_quotes(
            od, fair, position, best_bid, best_ask, spread,
            buy_capacity, sell_capacity
        )

        return orders

    # ── Intarian Pepper Root: Asymmetric Skewed MM ────────────────────

    def _trade_pepper(self, state: TradingState, ts: dict) -> List[Order]:
        orders: List[Order] = []
        od = state.order_depths[PEPPER]
        position = state.position.get(PEPPER, 0)

        mid = self._micro_price(od)

        prev_ema = ts.get("pepper_ema")
        if mid is not None:
            ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * prev_ema if prev_ema is not None else mid
            fair = ema + PEPPER_DRIFT_PER_TICK
        else:
            fair = prev_ema if prev_ema is not None else 12000.0
            ema = fair
        ts["pepper_ema"] = ema

        # Detrended residual: how far mid is above our smoothed EMA
        residual = (mid - ema) if mid is not None else 0.0

        near_cap = position >= PEPPER_CHURN_THRESHOLD
        # Only churn-sell when price is above trend (positive residual)
        churn_sell_ok = near_cap and residual >= PEPPER_CHURN_RESIDUAL_THRESH

        buy_capacity = POSITION_LIMIT - position
        sell_capacity = POSITION_LIMIT + position

        # --- Layer 1: Aggressive buy-side taking (accumulate longs) ---
        buy_margin = PEPPER_TAKE_BUY_MARGIN_CHURN if near_cap else PEPPER_TAKE_BUY_MARGIN
        buy_take_price = int(round(fair + buy_margin))
        buy_orders = self._take_sells_up_to(od, buy_take_price, buy_capacity, PEPPER)
        orders += buy_orders
        filled_buy = sum(o.quantity for o in buy_orders if o.quantity > 0)
        buy_capacity -= filled_buy

        # --- Layer 2: Sell-side taking (only when NOT near cap) ---
        if not near_cap:
            sell_take_price = int(round(fair + PEPPER_TAKE_SELL_MARGIN))
            sell_take_orders = self._take_buys_down_to(od, sell_take_price, sell_capacity, PEPPER)
            orders += sell_take_orders
            filled_sell = sum(-o.quantity for o in sell_take_orders if o.quantity < 0)
            sell_capacity -= filled_sell

        # --- Layer 3: Passive quotes ---
        skew = PEPPER_INV_SKEW_COEFF * (position + filled_buy - PEPPER_TARGET_POS)

        bid_price = int(round(fair - PEPPER_BID_OFFSET - skew))
        # Use tight churn ask only when residual is positive (sell into noise peaks)
        ask_offset = PEPPER_CHURN_ASK_OFFSET if churn_sell_ok else PEPPER_ASK_OFFSET
        ask_price = int(round(fair + ask_offset - skew))

        if buy_capacity > 0:
            orders.append(Order(PEPPER, bid_price, buy_capacity))
        if sell_capacity > 0:
            orders.append(Order(PEPPER, ask_price, -sell_capacity))

        return orders

    def _take_sells_up_to(
        self, od: OrderDepth, threshold: int, capacity: int, symbol: str
    ) -> List[Order]:
        """Buy against sell orders priced at or below threshold."""
        orders: List[Order] = []
        remaining = capacity
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > threshold or remaining <= 0:
                break
            vol = -od.sell_orders[ask_price]
            fill = min(vol, remaining)
            if fill > 0:
                orders.append(Order(symbol, ask_price, fill))
                remaining -= fill
        return orders

    def _take_buys_down_to(
        self, od: OrderDepth, threshold: int, capacity: int, symbol: str
    ) -> List[Order]:
        """Sell against buy orders priced at or above threshold."""
        orders: List[Order] = []
        remaining = capacity
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < threshold or remaining <= 0:
                break
            vol = od.buy_orders[bid_price]
            fill = min(vol, remaining)
            if fill > 0:
                orders.append(Order(symbol, bid_price, -fill))
                remaining -= fill
        return orders

    # ── Osmium: imbalance-aware FV ─────────────────────────────────────

    def _l1_imbalance(self, od: OrderDepth) -> Optional[float]:
        """Return L1 volume imbalance in [-1, 1]. +1 = heavy bid side (bullish)."""
        if not od.buy_orders or not od.sell_orders:
            return None
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        bid_vol = od.buy_orders[best_bid]
        ask_vol = abs(od.sell_orders[best_ask])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def _compute_osmium_fv(self, od: OrderDepth, ts: dict, imbalance: Optional[float]) -> Optional[float]:
        """Micro-price + imbalance skew + EMA smoothing."""
        raw = self._micro_price(od)
        if raw is not None and imbalance is not None:
            raw += OSMIUM_IMB_SKEW_COEFF * imbalance

        prev_ema = ts.get("ema")
        if raw is not None:
            ema = OSMIUM_EMA_ALPHA * raw + (1 - OSMIUM_EMA_ALPHA) * prev_ema if prev_ema is not None else raw
        else:
            ema = prev_ema

        if ema is not None:
            ts["ema"] = ema
        return ema

    # ── Osmium: tight-spread directional taking ─────────────────────────

    def _tight_spread_take(
        self, od: OrderDepth, fair: float, imbalance: float,
        buy_cap: int, sell_cap: int,
    ) -> Tuple[List[Order], int, int]:
        """When spread is tight and imbalance is strong, take aggressively in the implied direction.
        Uses wider margin when imbalance is very strong (multi-signal confirmation)."""
        orders: List[Order] = []
        bought = 0
        sold = 0
        abs_imb = abs(imbalance)

        margin = OSMIUM_STRONG_TAKE_MARGIN if abs_imb >= OSMIUM_STRONG_IMB_THRESH else OSMIUM_TIGHT_TAKE_MARGIN

        if imbalance > 0.3 and buy_cap > 0:
            threshold = int(round(fair)) + margin
            remaining = buy_cap
            for ask_price in sorted(od.sell_orders.keys()):
                if ask_price > threshold or remaining <= 0:
                    break
                vol = -od.sell_orders[ask_price]
                fill = min(vol, remaining)
                if fill > 0:
                    orders.append(Order(OSMIUM, ask_price, fill))
                    remaining -= fill
                    bought += fill

        elif imbalance < -0.3 and sell_cap > 0:
            threshold = int(round(fair)) - margin
            remaining = sell_cap
            for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                if bid_price < threshold or remaining <= 0:
                    break
                vol = od.buy_orders[bid_price]
                fill = min(vol, remaining)
                if fill > 0:
                    orders.append(Order(OSMIUM, bid_price, -fill))
                    remaining -= fill
                    sold += fill

        return orders, bought, sold

    # ── Osmium: layered adaptive-width quoting ──────────────────────────

    def _adaptive_quotes(
        self, od: OrderDepth, fair: float, position: int,
        best_bid: Optional[int], best_ask: Optional[int],
        spread: Optional[int], buy_cap: int, sell_cap: int,
    ) -> List[Order]:
        """Place layered quotes at L1/L2/L3 with adaptive half-spread + inventory skew."""
        orders: List[Order] = []
        if best_bid is None or best_ask is None:
            return orders

        if spread is not None and spread <= OSMIUM_TIGHT_SPREAD_THRESH:
            base_half = OSMIUM_TIGHT_HALF_SPREAD
        elif spread is not None and spread >= OSMIUM_WIDE_SPREAD_THRESH:
            base_half = OSMIUM_WIDE_HALF_SPREAD
        else:
            base_half = OSMIUM_NORMAL_HALF_SPREAD

        inv_skew = OSMIUM_INV_SKEW_COEFF * position

        jump_bid = best_bid + OSMIUM_PENNY_JUMP
        jump_ask = best_ask - OSMIUM_PENNY_JUMP
        if jump_bid >= jump_ask:
            jump_bid = best_bid
            jump_ask = best_ask

        remaining_buy = buy_cap
        remaining_sell = sell_cap

        for layer_offset, weight in zip(OSMIUM_LAYER_OFFSETS, OSMIUM_LAYER_WEIGHTS):
            half = base_half + layer_offset

            our_bid = int(round(fair - half - inv_skew))
            our_ask = int(round(fair + half - inv_skew))

            bid_price = max(our_bid, jump_bid) if layer_offset == 0 else our_bid
            ask_price = min(our_ask, jump_ask) if layer_offset == 0 else our_ask

            if bid_price >= ask_price:
                bid_price = our_bid
                ask_price = our_ask

            bid_qty = max(1, int(round(buy_cap * weight)))
            ask_qty = max(1, int(round(sell_cap * weight)))
            bid_qty = min(bid_qty, remaining_buy)
            ask_qty = min(ask_qty, remaining_sell)

            if bid_qty > 0:
                orders.append(Order(OSMIUM, bid_price, bid_qty))
                remaining_buy -= bid_qty
            if ask_qty > 0:
                orders.append(Order(OSMIUM, ask_price, -ask_qty))
                remaining_sell -= ask_qty

        if remaining_buy > 0:
            deepest_bid = int(round(fair - base_half - OSMIUM_LAYER_OFFSETS[-1] - inv_skew))
            orders.append(Order(OSMIUM, deepest_bid, remaining_buy))
        if remaining_sell > 0:
            deepest_ask = int(round(fair + base_half + OSMIUM_LAYER_OFFSETS[-1] - inv_skew))
            orders.append(Order(OSMIUM, deepest_ask, -remaining_sell))

        return orders

    def _micro_price(self, od: OrderDepth) -> Optional[float]:
        if not od.buy_orders or not od.sell_orders:
            if od.buy_orders:
                return float(max(od.buy_orders.keys()))
            if od.sell_orders:
                return float(min(od.sell_orders.keys()))
            return None

        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        bid_vol = od.buy_orders[best_bid]
        ask_vol = abs(od.sell_orders[best_ask])
        total = bid_vol + ask_vol
        if total == 0:
            return (best_bid + best_ask) / 2.0
        imb = bid_vol / total
        return best_ask * imb + best_bid * (1 - imb)

    # ── Osmium: FV-taking ──────────────────────────────────────────────

    def _fv_take(
        self, od: OrderDepth, fair: float, buy_cap: int, sell_cap: int
    ) -> Tuple[List[Order], int, int]:
        """Buy asks priced below FV (+ margin) and sell bids priced above FV (- margin)."""
        orders: List[Order] = []
        total_bought = 0
        total_sold = 0

        buy_threshold = int(round(fair)) + OSMIUM_TAKE_MARGIN
        remaining_buy = buy_cap
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > buy_threshold or remaining_buy <= 0:
                break
            volume = -od.sell_orders[ask_price]
            fill = min(volume, remaining_buy)
            if fill > 0:
                orders.append(Order(OSMIUM, ask_price, fill))
                remaining_buy -= fill
                total_bought += fill

        sell_threshold = int(round(fair)) - OSMIUM_TAKE_MARGIN
        remaining_sell = sell_cap
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < sell_threshold or remaining_sell <= 0:
                break
            volume = od.buy_orders[bid_price]
            fill = min(volume, remaining_sell)
            if fill > 0:
                orders.append(Order(OSMIUM, bid_price, -fill))
                remaining_sell -= fill
                total_sold += fill

        return orders, total_bought, total_sold

    # ── Osmium: Position-reduction at FV ────────────────────────────────

    def _position_reduce(
        self,
        od: OrderDepth,
        fair: float,
        position: int,
        buy_cap: int,
        sell_cap: int,
    ) -> Tuple[List[Order], int, int]:
        """Aggressively cross the book at FV to unwind when |position| > threshold."""
        orders: List[Order] = []
        bought = 0
        sold = 0
        fv_int = int(round(fair))

        if position > OSMIUM_POSITION_REDUCE_THRESHOLD:
            qty_to_unwind = min(position - OSMIUM_POSITION_REDUCE_THRESHOLD, sell_cap)
            remaining = qty_to_unwind
            for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                if bid_price < fv_int or remaining <= 0:
                    break
                vol = od.buy_orders[bid_price]
                fill = min(vol, remaining)
                if fill > 0:
                    orders.append(Order(OSMIUM, bid_price, -fill))
                    remaining -= fill
                    sold += fill
            if remaining > 0:
                orders.append(Order(OSMIUM, fv_int, -remaining))
                sold += remaining

        elif position < -OSMIUM_POSITION_REDUCE_THRESHOLD:
            qty_to_unwind = min(abs(position) - OSMIUM_POSITION_REDUCE_THRESHOLD, buy_cap)
            remaining = qty_to_unwind
            for ask_price in sorted(od.sell_orders.keys()):
                if ask_price > fv_int or remaining <= 0:
                    break
                vol = -od.sell_orders[ask_price]
                fill = min(vol, remaining)
                if fill > 0:
                    orders.append(Order(OSMIUM, ask_price, fill))
                    remaining -= fill
                    bought += fill
            if remaining > 0:
                orders.append(Order(OSMIUM, fv_int, remaining))
                bought += remaining

        return orders, bought, sold

    # ── State persistence ──────────────────────────────────────────────

    def _load_state(self, trader_data: str) -> dict:
        if trader_data and trader_data.strip():
            try:
                return json.loads(trader_data)
            except (json.JSONDecodeError, TypeError):
                pass
        return {}
