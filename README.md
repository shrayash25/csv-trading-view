# Prosperity 4 — Round 1 Trading Algorithm

A production trading algorithm for IMC Prosperity 4, Round 1.  
Trades two products: **Ash-Coated Osmium** (market-making) and **Intarian Pepper Root** (trend-following + skewed market-making).

---

## Files

| File | What it does |
|---|---|
| `datamodel.py` | All the data classes the Prosperity engine uses to talk to your algorithm. You **do not modify** this file. |
| `trader.py` | Your trading algorithm. This is the only file you submit. |
| `README.md` | This file. |

---

## How the Prosperity Engine Works

Every simulation tick (roughly every 100ms of game time), the engine:

1. Builds a `TradingState` object containing the current order book, your position, recent trades, etc.
2. Calls `Trader.run(state)` — your code.
3. Takes the orders you return and matches them against the market.

Your algorithm **must** define a class called `Trader` with a method called `run`. That's it.

---

## Data Classes (datamodel.py)

### `TradingState`
The main object your `run` method receives. Contains everything you need to make a decision.

| Field | Type | What it holds |
|---|---|---|
| `traderData` | `str` | A string you returned last tick. Use it to persist state between ticks (e.g. EMA values). |
| `timestamp` | `int` | Current game time (0, 100, 200, ..., up to 999900 per day). |
| `listings` | `Dict[str, Listing]` | Metadata about tradable products. |
| `order_depths` | `Dict[str, OrderDepth]` | The current order book for each product. **This is your main data source.** |
| `own_trades` | `Dict[str, List[Trade]]` | Trades your algorithm executed since the last tick. |
| `market_trades` | `Dict[str, List[Trade]]` | Trades other participants executed since the last tick. |
| `position` | `Dict[str, int]` | Your current inventory in each product. Positive = long, negative = short. |
| `observations` | `Observation` | Extra market signals (not used in Round 1). |

### `OrderDepth`
The order book for a single product.

| Field | Type | What it holds |
|---|---|---|
| `buy_orders` | `Dict[int, int]` | Price -> volume of resting buy orders. Positive volumes. |
| `sell_orders` | `Dict[int, int]` | Price -> volume of resting sell orders. **Negative** volumes (Prosperity convention). |

To get the best bid: `max(order_depth.buy_orders.keys())`  
To get the best ask: `min(order_depth.sell_orders.keys())`  
Note: sell order volumes are negative — to get the actual size, negate them.

### `Order`
An order you place.

```python
Order(symbol="ASH_COATED_OSMIUM", price=10003, quantity=10)   # Buy 10 at 10003
Order(symbol="ASH_COATED_OSMIUM", price=9997, quantity=-10)   # Sell 10 at 9997
```

| Field | Type | Meaning |
|---|---|---|
| `symbol` | `str` | Which product to trade. |
| `price` | `int` | The price you want. Must be an integer. |
| `quantity` | `int` | Positive = buy, negative = sell. |

### `Trade`
A completed trade (in `own_trades` or `market_trades`).

| Field | Type |
|---|---|
| `symbol` | `str` |
| `price` | `int` |
| `quantity` | `int` |
| `buyer` | `str` |
| `seller` | `str` |
| `timestamp` | `int` |

### `Listing`
Product metadata.

| Field | Meaning |
|---|---|
| `symbol` | The trading symbol (e.g. `"ASH_COATED_OSMIUM"`). |
| `product` | The underlying product name. |
| `denomination` | The currency it's priced in (XIRECS). |

---

## The `Trader.run()` Method

```python
def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
```

**Takes:** A `TradingState` with the current market snapshot.

**Returns a 3-tuple:**
1. `Dict[str, List[Order]]` — A dictionary mapping product symbols to lists of `Order` objects you want to place.
2. `int` — Number of conversions (set to `0` for Round 1, not used).
3. `str` — A string that will be passed back to you as `state.traderData` on the next tick. Use this to persist your algorithm's state (e.g. EMA values, counters).

### Position Limits

Each product has a position limit of **80 units**. If your orders would push your position beyond +80 or below -80 (assuming all orders fill), the engine cancels **all** your orders for that product for that tick.

The algorithm handles this by computing `buy_capacity = 80 - current_position` and `sell_capacity = 80 + current_position` before placing any orders.

---

## Trading Strategies Implemented

### 1. Ash-Coated Osmium — Pure Market Making

**Why it works:** Osmium has zero drift, a tight price range (~40 ticks around 10,000), wide spreads (~16 ticks), and strong mean-reversion (lag-1 ACF of -0.50).

**How the code does it:**

- **Fair value** = EMA of the mid-price, blended 70/30 with the 10,000 anchor. This keeps the estimate stable without ignoring short-term moves.
- **Quoting** = Posts a bid at `fair - 7` and ask at `fair + 7`. This is inside the market's ~16-tick spread (so we get filled) but wide enough to capture edge.
- **Inventory skew** = If we're long 20 units, both bid and ask shift down by `0.15 * 20 = 3 ticks`. This encourages the market to buy from us and discourages further buys.
- **Spread collapse guard** = If the observed spread drops below 6 ticks, we widen our quotes to 10 ticks to avoid getting picked off by an aggressive participant.
- **Aggressive taking** = Before placing passive quotes, we sweep any sell orders below fair value (buy cheap) and any buy orders above fair value (sell dear).

### 2. Intarian Pepper Root — Trend-Following + Skewed Market Making

**Why it works:** Pepper Root rises by exactly +1,000 per day (+0.10 per tick). The trend is mechanical and predictable. At the tick level, it also has a -0.50 ACF (bid-ask bounce), so market-making works too.

**How the code does it:**

- **Fair value** = EMA of mid-price plus the known drift of +0.10/tick. The EMA adapts quickly (alpha=0.15) to track the trending price.
- **Long bias** = The target inventory is +30 units (not 0). The skew formula is based on `position - 30`, so the algorithm always wants to be net long to ride the trend.
- **Asymmetric quotes** = Bid is placed 5 ticks below fair (tight, to get filled on the buy side) and ask is 9 ticks above fair (wide, so we only sell at a premium).
- **Aggressive buying** = Sweeps all sell orders at or below `fair - 1`, aggressively accumulating longs.
- **Conservative selling** = Only sells into bids above `fair + 2`, avoiding premature exits from the uptrend.
- **Inventory skew** = Adjusts quotes based on deviation from the +30 target. If we're at +60 (too long), quotes shift down to encourage selling. If we're at +10 (too low), quotes shift up to encourage buying.

### 3. What is NOT implemented (and why)

**Pairs trading** between the two products is explicitly avoided. The CSV data shows their rolling correlation fluctuates wildly between -0.8 and +0.8 with no stable regime. One product trends, the other doesn't — there is no cointegration.

---

## How to Run / Backtest

### Using the Prosperity 4 Backtester

Install the backtester:

```bash
pip install -U prosperity4btx
```

Run against Round 1 data:

```bash
# All days
prosperity4btx trader.py 1

# Specific day
prosperity4btx trader.py 1-0

# With merged PnL across days
prosperity4btx trader.py 1 --merge-pnl

# Print debug output while running
prosperity4btx trader.py 1 --print
```

### Submitting to Prosperity

1. Go to [prosperity.imc.com](https://prosperity.imc.com).
2. Upload `trader.py` — only this single file is needed. The `datamodel` module is provided by the engine automatically.
3. The engine will call `Trader.run()` once per tick for each simulated day.

---

## Key Parameters (Tuning Guide)

### Osmium Parameters

| Parameter | Default | What it controls |
|---|---|---|
| `OSMIUM_HALF_SPREAD` | 7 | How far from fair value we quote. Wider = safer but fewer fills. |
| `OSMIUM_SPREAD_COLLAPSE_THRESH` | 6 | If the market spread drops below this, we widen our quotes. |
| `OSMIUM_WIDE_HALF_SPREAD` | 10 | The wider quote distance used during spread collapse. |
| `OSMIUM_INV_SKEW_COEFF` | 0.15 | How aggressively we skew quotes to manage inventory. Higher = faster reversion to flat. |
| `OSMIUM_EMA_ALPHA` | 0.05 | EMA responsiveness. Lower = smoother fair value, lags more. |

### Pepper Root Parameters

| Parameter | Default | What it controls |
|---|---|---|
| `PEPPER_BID_OFFSET` | 5 | How far below fair value we place our bid. Tighter to get filled on buys. |
| `PEPPER_ASK_OFFSET` | 9 | How far above fair value we place our ask. Wider to only sell at a premium. |
| `PEPPER_TREND_BASE_POS` | 30 | Target long position to ride the trend. 0 = no directional bias. |
| `PEPPER_INV_SKEW_COEFF` | 0.12 | How aggressively we skew to return to the target position. |
| `PEPPER_EMA_ALPHA` | 0.15 | EMA responsiveness. Higher for trending instruments. |
| `PEPPER_DRIFT_PER_TICK` | 0.10 | Known upward drift added to fair value each tick. |

---

## State Persistence

The algorithm uses `traderData` (a JSON string) to remember values between ticks:

- `osmium_ema` — the EMA of Osmium's mid-price
- `pepper_ema` — the EMA of Pepper Root's mid-price

On the very first tick, when `traderData` is empty, the algorithm initialises:
- Osmium EMA to the anchor of 10,000
- Pepper Root EMA to the first observed mid-price
