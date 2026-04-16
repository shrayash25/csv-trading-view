from typing import Dict, List
from datamodel import OrderDepth, TradingState, Order, Symbol
import math

class Trader:
    # Hard limits set by the exchange
    POSITION_LIMITS = {
        "ASH_COATED_OSMIUM": 80,
        "INTARIAN_PEPPER_ROOT": 80
    }
    
    # Risk Aversion parameter (Gamma). Higher = more aggressive inventory flattening.
    # Needs empirical tuning per asset class.
    GAMMA = {
        "ASH_COATED_OSMIUM": 2.5,     
        "INTARIAN_PEPPER_ROOT": 2.5   
    }

    def run(self, state: TradingState) -> tuple[Dict[Symbol, List[Order]], int, str]:
        result = {}
        conversions = 0
        trader_data = state.traderData if state.traderData else ""

        for product in state.order_depths:
            if product not in self.POSITION_LIMITS:
                continue

            order_depth: OrderDepth = state.order_depths[product]
            orders: List[Order] = []
            
            q = state.position.get(product, 0)
            Q = self.POSITION_LIMITS[product]

            # Require two-sided book to calculate microstructure signals
            if len(order_depth.sell_orders) == 0 or len(order_depth.buy_orders) == 0:
                continue

            # 1. Extract L1 Order Book State
            best_ask = min(order_depth.sell_orders.keys())
            best_bid = max(order_depth.buy_orders.keys())
            
            # Prosperity engine represents sell orders as negative integers
            ask_vol = abs(order_depth.sell_orders[best_ask]) 
            bid_vol = order_depth.buy_orders[best_bid]

            # 2. Calculate Micro-Price (Volume-weighted fair value)
            # If bid_vol is massive, micro_price gets pushed closer to best_ask
            total_vol = bid_vol + ask_vol
            micro_price = ((best_bid * ask_vol) + (best_ask * bid_vol)) / total_vol

            # 3. Calculate Reservation Price (Inventory Skew)
            # If q > 0 (long), we subtract from micro_price to lower our quotes
            skew_penalty = self.GAMMA[product] * (q / Q)
            reservation_price = micro_price - skew_penalty

            # 4. Determine Dynamic Quoting Levels
            # We want to capture the spread, but adjust based on our reservation price
            base_half_spread = (best_ask - best_bid) / 2.0
            
            # Target prices (floats)
            target_bid = reservation_price - base_half_spread
            target_ask = reservation_price + base_half_spread

            # 5. Integer Rounding for Execution Reality
            # Ceil the bid to be more competitive when we want to buy
            # Floor the ask to be more competitive when we want to sell
            my_bid_price = math.ceil(target_bid)
            my_ask_price = math.floor(target_ask)

            # Execution logic constraint: Do not cross the book unless we intend to take liquidity.
            # For pure passive market making, cap our prices at the best prevailing rates.
            my_bid_price = min(my_bid_price, best_ask - 1)
            my_ask_price = max(my_ask_price, best_bid + 1)

            # 6. Order Sizing & Placement
            max_buy_qty = Q - q
            if max_buy_qty > 0:
                # Optional: Scale sizing based on confidence/spread. Here we take max allowed.
                orders.append(Order(product, my_bid_price, max_buy_qty))

            max_sell_qty = -Q - q
            if max_sell_qty < 0:
                orders.append(Order(product, my_ask_price, max_sell_qty))
            
            result[product] = orders

        return result, conversions, trader_data
