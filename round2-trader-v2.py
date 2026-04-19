import json
from typing import Dict, List, Optional, Tuple
from datamodel import Order, OrderDepth, ProsperityEncoder, TradingState

OSMIUM = "ASH_COATED_OSMIUM"
PEPPER = "INTARIAN_PEPPER_ROOT"
POSITION_LIMIT = 80

# Minimum volume at a price level to qualify as a "wall" (designated-MM quote).
# Osmium walls cluster at 21–30, Pepper walls at 18–25; 15 is a safe floor.
WALL_VOL_THRESHOLD = 15

# ── Osmium parameters ────────────────────────────────────────────────
# NOTE: Anchor-shrinkage on fair value was rolled back after the 323201 run:
# whenever mid traded above 10000, the 0.3 anchor pull + 4·imb skew + a
# STRONG_TAKE_MARGIN of 4 combined into a sell-threshold of ~fair−4, which
# swept 2–3 book levels per adverse imbalance shock (ts=4100, 14100, 59200, …).
# Anchor is retained only as a reference constant; the weight is now zero.
OSMIUM_ANCHOR = 10000
OSMIUM_ANCHOR_WEIGHT = 0.0               # was 0.30 — biased FV down whenever mid > 10000
OSMIUM_EMA_ALPHA = 0.5
OSMIUM_IMB_SKEW_COEFF = 4.0              # kept at 4.0 (safe once anchor pull is removed)
OSMIUM_TAKE_MARGIN = 1
OSMIUM_POSITION_REDUCE_THRESHOLD = 40    # was 20 (too tight) — compromise with v1's 50
OSMIUM_PENNY_JUMP = 1
OSMIUM_TIGHT_SPREAD_THRESH = 10
OSMIUM_TIGHT_TAKE_MARGIN = 3
OSMIUM_NORMAL_HALF_SPREAD = 7
OSMIUM_WIDE_HALF_SPREAD = 6
OSMIUM_TIGHT_HALF_SPREAD = 10
OSMIUM_WIDE_SPREAD_THRESH = 18
OSMIUM_INV_SKEW_COEFF = 0.15             # was 0.25 — 0.25 amplifies adverse fills
OSMIUM_LAYER_OFFSETS = (0, 2, 4)
OSMIUM_LAYER_WEIGHTS = (1.0, 0.0, 0.0)   # snapshot engine: 100% at L1, L2/L3 only as fallback
OSMIUM_STRONG_IMB_THRESH = 0.4
OSMIUM_STRONG_TAKE_MARGIN = 4
OSMIUM_REVERSAL_COEFF = 0.0              # disabled — adds noise without clear empirical gain
OSMIUM_TIGHT_TAKE_DEPTH = 1              # NEW: cap tight-spread taking at N book levels (no sweep)

# ── Pepper parameters (asymmetric skewed MM for +1000/day trend) ─────
PEPPER_DRIFT_PER_TICK = 0.10
PEPPER_EMA_ALPHA = 0.20
PEPPER_IMB_SKEW_COEFF = 4.0              # NEW: empirically |4| ticks next-tick edge per unit imb
PEPPER_TAKE_BUY_MARGIN = 8
PEPPER_TAKE_BUY_MARGIN_CHURN = 3
PEPPER_TAKE_SELL_MARGIN = 5
PEPPER_BID_OFFSET = 3
PEPPER_ASK_OFFSET = 11
PEPPER_NEAR_CAP_ASK_OFFSET = 14          # was 7 (tight churn) — WIDER near cap, hold the drift
PEPPER_TARGET_POS = 80                   # was 68 — drift dominates, no inventory cost at cap
PEPPER_INV_SKEW_COEFF = 0.06
PEPPER_CHURN_THRESHOLD = 75
PEPPER_CHURN_RESIDUAL_THRESH = 3.0       # was 2.0 and vs EMA; now vs trend line
PEPPER_TREND_ANCHOR_BUY_OFFSET = 1       # near-cap: still buy anything at or below trend+1


class Trader:
    """
    Round 2 trading algorithm — post-analysis fixes (two-pass).

    First pass applied all priority-5 edits from the alpha autopsy. The 323201
    submission showed those helped Pepper (+7476 vs v1 +7534, flat within noise)
    but regressed Osmium (−600 vs v1 +784). Forensic replay traced every
    catastrophic drop (ts=4100, 8300, 14100, 26800, 59200, 79300) to the same
    mechanism: anchor-pulled fair + strong imbalance + STRONG_TAKE_MARGIN=4 made
    `_tight_spread_take`'s threshold sit ~4 ticks below true fair, and with no
    depth cap it swept 2–3 book levels per imbalance shock.

    This second pass keeps every Pepper improvement plus wall-mid and
    snapshot-L1 sizing, and rolls back the four Osmium items that failed:

      • Osmium FV no longer shrinks to the 10000 anchor (weight 0).
      • Osmium position-reduction threshold 20 → 40; rest-price reverted to
        int(round(fair)) instead of a static 10000.
      • Osmium inventory skew 0.25 → 0.15.
      • `_tight_spread_take` now capped to OSMIUM_TIGHT_TAKE_DEPTH book levels
        (=1) to prevent further sweeps regardless of FV.
      • Lag-1 reversal coefficient disabled (unclear empirical benefit).

    Retained from first pass:
      • Wall-mid FV for both products (~30% better RMSE in-sample).
      • Snapshot-aware layered quoting: 100% of capacity at L1.
      • Pepper target position = 80; near-cap ask widened (not tightened).
      • Pepper churn-sell gated on a deterministic trend line (mid−trend ≥ 3).
      • Pepper trend-anchored near-cap buy (take any ask ≤ trend + 1).
      • Pepper FV includes +4·imbalance skew.
    """

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}
        trader_state = self._load_state(state.traderData)

        if OSMIUM in state.order_depths:
            result[OSMIUM] = self._trade_osmium(state, trader_state)

        if PEPPER in state.order_depths:
            result[PEPPER] = self._trade_pepper(state, trader_state)

        return result, 0, json.dumps(trader_state, cls=ProsperityEncoder)

    # ── Ash-Coated Osmium: anchored market-making ──────────────────────

    def _trade_osmium(self, state: TradingState, ts: dict) -> List[Order]:
        orders: List[Order] = []
        od = state.order_depths[OSMIUM]
        position = state.position.get(OSMIUM, 0)

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        imbalance = self._l1_imbalance(od)
        fair = self._compute_osmium_fv(od, ts, imbalance)
        if fair is None:
            return orders

        # Lag-1 reversal: expected next-tick mid move is ≈ −0.5·(last_move).
        # Apply only to take-logic fair (fair_take), NEVER to passive quote price.
        raw_mid = self._simple_mid(od)
        last_mid = ts.get("osmium_last_mid")
        if last_mid is not None and raw_mid is not None:
            fair_take = fair - OSMIUM_REVERSAL_COEFF * (raw_mid - last_mid)
        else:
            fair_take = fair
        if raw_mid is not None:
            ts["osmium_last_mid"] = raw_mid

        spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None

        buy_capacity = POSITION_LIMIT - position
        sell_capacity = POSITION_LIMIT + position

        if spread is not None and spread <= OSMIUM_TIGHT_SPREAD_THRESH and imbalance is not None:
            tight_orders, tb, ts_ = self._tight_spread_take(
                od, fair_take, imbalance, buy_capacity, sell_capacity
            )
            orders += tight_orders
            buy_capacity -= tb
            sell_capacity -= ts_
            position += tb - ts_

        take_orders, buy_taken, sell_taken = self._fv_take(
            od, fair_take, buy_capacity, sell_capacity
        )
        orders += take_orders
        buy_capacity -= buy_taken
        sell_capacity -= sell_taken
        position += buy_taken - sell_taken

        # Fair-based unwind (live fair, not a static anchor). See 323201 post-mortem.
        reduce_orders, buy_reduced, sell_reduced = self._position_reduce(
            od, fair, position, buy_capacity, sell_capacity
        )
        orders += reduce_orders
        buy_capacity -= buy_reduced
        sell_capacity -= sell_reduced

        orders += self._adaptive_quotes(
            od, fair, position, best_bid, best_ask, spread,
            buy_capacity, sell_capacity
        )

        return orders

    # ── Intarian Pepper Root: drift-aware asymmetric MM ─────────────────

    def _trade_pepper(self, state: TradingState, ts: dict) -> List[Order]:
        orders: List[Order] = []
        od = state.order_depths[PEPPER]
        position = state.position.get(PEPPER, 0)

        mid = self._wall_mid(od)
        imbalance = self._l1_imbalance(od)

        prev_ema = ts.get("pepper_ema")
        if mid is not None:
            ema = PEPPER_EMA_ALPHA * mid + (1 - PEPPER_EMA_ALPHA) * prev_ema if prev_ema is not None else mid
        else:
            ema = prev_ema if prev_ema is not None else 12000.0
        ts["pepper_ema"] = ema

        # Deterministic trend line: anchored on first observation, advanced by the known drift.
        # This decouples "am I rich vs. trend?" from the lagging EMA.
        if ts.get("pepper_trend") is None and mid is not None:
            ts["pepper_trend"] = mid
        if "pepper_trend" in ts:
            ts["pepper_trend"] = ts["pepper_trend"] + PEPPER_DRIFT_PER_TICK
        trend = ts.get("pepper_trend", ema + PEPPER_DRIFT_PER_TICK)

        # Fair value = EMA + drift-step + imbalance skew (Pepper had no imbalance term in v1).
        imb_skew = PEPPER_IMB_SKEW_COEFF * imbalance if imbalance is not None else 0.0
        fair = ema + PEPPER_DRIFT_PER_TICK + imb_skew

        residual_vs_trend = (mid - trend) if mid is not None else 0.0

        near_cap = position >= PEPPER_CHURN_THRESHOLD
        truly_rich = residual_vs_trend >= PEPPER_CHURN_RESIDUAL_THRESH

        buy_capacity = POSITION_LIMIT - position
        sell_capacity = POSITION_LIMIT + position

        # Layer 1: aggressive buy-side taking.
        # Near cap we stay tight on the EMA-fair path but additionally accept anything
        # priced at or below the trend line + 1 — so a lagging EMA can never lock us out
        # of free drift-carry buys.
        buy_margin = PEPPER_TAKE_BUY_MARGIN_CHURN if near_cap else PEPPER_TAKE_BUY_MARGIN
        ema_based_buy_take = int(round(fair + buy_margin))
        trend_anchor_buy = int(round(trend + PEPPER_TREND_ANCHOR_BUY_OFFSET))
        effective_buy_take = max(ema_based_buy_take, trend_anchor_buy) if near_cap else ema_based_buy_take
        buy_orders = self._take_sells_up_to(od, effective_buy_take, buy_capacity, PEPPER)
        orders += buy_orders
        filled_buy = sum(o.quantity for o in buy_orders if o.quantity > 0)
        buy_capacity -= filled_buy

        # Layer 2: sell-side taking.
        # Enabled whenever not-at-cap, OR when genuinely rich vs. trend (real churn signal).
        if not near_cap or truly_rich:
            sell_take_price = int(round(fair + PEPPER_TAKE_SELL_MARGIN))
            sell_take_orders = self._take_buys_down_to(od, sell_take_price, sell_capacity, PEPPER)
            orders += sell_take_orders
            filled_sell = sum(-o.quantity for o in sell_take_orders if o.quantity < 0)
            sell_capacity -= filled_sell

        # Layer 3: passive quotes.
        # Near cap → wider (not tighter) ask so drift carries us. No more tight-churn ask.
        skew = PEPPER_INV_SKEW_COEFF * (position + filled_buy - PEPPER_TARGET_POS)
        ask_offset = PEPPER_NEAR_CAP_ASK_OFFSET if near_cap else PEPPER_ASK_OFFSET

        bid_price = int(round(fair - PEPPER_BID_OFFSET - skew))
        ask_price = int(round(fair + ask_offset - skew))

        if buy_capacity > 0:
            orders.append(Order(PEPPER, bid_price, buy_capacity))
        if sell_capacity > 0:
            orders.append(Order(PEPPER, ask_price, -sell_capacity))

        return orders

    # ── Generic book-crossing helpers ──────────────────────────────────

    def _take_sells_up_to(
        self, od: OrderDepth, threshold: int, capacity: int, symbol: str
    ) -> List[Order]:
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

    # ── Osmium: imbalance-aware, anchor-shrunk fair value ──────────────

    def _l1_imbalance(self, od: OrderDepth) -> Optional[float]:
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
        """Fair value = (1−w)·wall_mid + w·anchor + k·imb, smoothed by a light EMA.

        Anchor weight is currently 0 (see post-mortem note at top of file): the
        anchor pull + 4·imb compounded into a sell-threshold ~4 ticks below true
        fair, driving `_tight_spread_take` to sweep deep book levels. Anchor is
        kept as a parameter so it can be re-introduced once that feedback is cut.
        """
        wall = self._wall_mid(od)

        prev_ema = ts.get("ema")
        if wall is None:
            return prev_ema

        raw = (1.0 - OSMIUM_ANCHOR_WEIGHT) * wall + OSMIUM_ANCHOR_WEIGHT * OSMIUM_ANCHOR
        if imbalance is not None:
            raw += OSMIUM_IMB_SKEW_COEFF * imbalance

        ema = OSMIUM_EMA_ALPHA * raw + (1 - OSMIUM_EMA_ALPHA) * prev_ema if prev_ema is not None else raw
        ts["ema"] = ema
        return ema

    # ── Osmium: tight-spread directional taking ────────────────────────

    def _tight_spread_take(
        self, od: OrderDepth, fair: float, imbalance: float,
        buy_cap: int, sell_cap: int,
    ) -> Tuple[List[Order], int, int]:
        """Take aggressively on imbalance when spread is tight — but ONLY at the
        top `OSMIUM_TIGHT_TAKE_DEPTH` book levels. Sweeping deep-book levels on a
        short-horizon imbalance signal produces cascading adverse fills (observed
        empirically in the 323201 run: 3+14+25 dump at ts=4100, 14+27 at ts=14100).
        """
        orders: List[Order] = []
        bought = 0
        sold = 0
        abs_imb = abs(imbalance)

        margin = OSMIUM_STRONG_TAKE_MARGIN if abs_imb >= OSMIUM_STRONG_IMB_THRESH else OSMIUM_TIGHT_TAKE_MARGIN

        if imbalance > 0.3 and buy_cap > 0:
            threshold = int(round(fair)) + margin
            remaining = buy_cap
            levels_hit = 0
            for ask_price in sorted(od.sell_orders.keys()):
                if ask_price > threshold or remaining <= 0 or levels_hit >= OSMIUM_TIGHT_TAKE_DEPTH:
                    break
                vol = -od.sell_orders[ask_price]
                fill = min(vol, remaining)
                if fill > 0:
                    orders.append(Order(OSMIUM, ask_price, fill))
                    remaining -= fill
                    bought += fill
                    levels_hit += 1

        elif imbalance < -0.3 and sell_cap > 0:
            threshold = int(round(fair)) - margin
            remaining = sell_cap
            levels_hit = 0
            for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                if bid_price < threshold or remaining <= 0 or levels_hit >= OSMIUM_TIGHT_TAKE_DEPTH:
                    break
                vol = od.buy_orders[bid_price]
                fill = min(vol, remaining)
                if fill > 0:
                    orders.append(Order(OSMIUM, bid_price, -fill))
                    remaining -= fill
                    sold += fill
                    levels_hit += 1

        return orders, bought, sold

    # ── Osmium: layered adaptive-width quoting ──────────────────────────

    def _adaptive_quotes(
        self, od: OrderDepth, fair: float, position: int,
        best_bid: Optional[int], best_ask: Optional[int],
        spread: Optional[int], buy_cap: int, sell_cap: int,
    ) -> List[Order]:
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

            # Snapshot-engine sizing: weight→0 layers stay silent unless L1 under-filled.
            bid_target = int(round(buy_cap * weight))
            ask_target = int(round(sell_cap * weight))
            bid_qty = min(max(bid_target, 0), remaining_buy)
            ask_qty = min(max(ask_target, 0), remaining_sell)

            if bid_qty > 0:
                orders.append(Order(OSMIUM, bid_price, bid_qty))
                remaining_buy -= bid_qty
            if ask_qty > 0:
                orders.append(Order(OSMIUM, ask_price, -ask_qty))
                remaining_sell -= ask_qty

        # Fallback: if any non-L1 layer weights are zero, surface the rest at the deepest rung.
        if remaining_buy > 0:
            deepest_bid = int(round(fair - base_half - OSMIUM_LAYER_OFFSETS[-1] - inv_skew))
            orders.append(Order(OSMIUM, deepest_bid, remaining_buy))
        if remaining_sell > 0:
            deepest_ask = int(round(fair + base_half + OSMIUM_LAYER_OFFSETS[-1] - inv_skew))
            orders.append(Order(OSMIUM, deepest_ask, -remaining_sell))

        return orders

    # ── Fair-value primitives ───────────────────────────────────────────

    def _simple_mid(self, od: OrderDepth) -> Optional[float]:
        if not od.buy_orders or not od.sell_orders:
            return None
        return (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2.0

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

    def _wall_mid(self, od: OrderDepth) -> Optional[float]:
        """Average of the highest-volume bid price and highest-volume ask price.

        Penny-jumpers (size 8–15) distort naive mid; the true price is pinned by
        designated MM walls (size 18–30). Falls back to micro-price if walls are
        absent on either side.
        """
        if not od.buy_orders or not od.sell_orders:
            return self._micro_price(od)

        wall_bid_price, wall_bid_vol = max(od.buy_orders.items(), key=lambda kv: kv[1])
        wall_ask_price, wall_ask_neg_vol = min(od.sell_orders.items(), key=lambda kv: kv[1])
        wall_ask_vol = -wall_ask_neg_vol

        if wall_bid_vol < WALL_VOL_THRESHOLD or wall_ask_vol < WALL_VOL_THRESHOLD:
            return self._micro_price(od)

        return (wall_bid_price + wall_ask_price) / 2.0

    # ── Osmium: FV-taking ──────────────────────────────────────────────

    def _fv_take(
        self, od: OrderDepth, fair: float, buy_cap: int, sell_cap: int
    ) -> Tuple[List[Order], int, int]:
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

    # ── Osmium: anchor-based position-reduction ─────────────────────────

    def _position_reduce(
        self,
        od: OrderDepth,
        fair: float,
        position: int,
        buy_cap: int,
        sell_cap: int,
    ) -> Tuple[List[Order], int, int]:
        """Aggressively cross the book at live fair to unwind when |pos| > threshold.

        The Hedgehogs-style "rest at anchor 10000" fallback was tried in the first
        anchor-centric build and produced systematic under-market fills (mean mid
        in the 323201 session was 10003.95, not 10000). Resting at int(fair)
        keeps the fallback priced at today's live fair instead.
        """
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
