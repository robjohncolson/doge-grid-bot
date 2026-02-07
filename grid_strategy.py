"""
grid_strategy.py -- Core grid trading logic.

HOW GRID TRADING WORKS:
  Imagine price is at $0.09.  We place:
    - 4 buy orders below: $0.0891, $0.0882, $0.0873, $0.0864
    - 4 sell orders above: $0.0909, $0.0918, $0.0927, $0.0936

  When price dips to $0.0891 and our buy fills:
    -> Immediately place a sell at $0.0900 (one level up)
    -> That buy->sell captures 1.0% minus fees = 0.50% profit

  When price rises to $0.0909 and our sell fills:
    -> Immediately place a buy at $0.0900 (one level down)
    -> Same cycle in reverse

  The grid "breathes" with price oscillation, capturing profit from volatility.

THIS FILE MANAGES:
  - Grid level calculation and tracking
  - Order placement and fill detection
  - Grid drift detection and rebuild
  - Dry-run simulation of fills
  - Daily P&L tracking
  - DOGE accumulation sweep logic
"""

import time
import logging
import csv
import os
from datetime import datetime, timezone

import config
import kraken_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class GridOrder:
    """
    Represents one order in the grid.

    Attributes:
        level:      Grid level index (-4 to +4, 0 is center, negative = buy, positive = sell)
        side:       "buy" or "sell"
        price:      Limit price in USD
        volume:     DOGE amount
        txid:       Kraken transaction ID (or dry-run fake ID)
        status:     "pending", "open", "filled", "cancelled"
        placed_at:  Unix timestamp when placed
    """
    def __init__(self, level: int, side: str, price: float, volume: float):
        self.level = level
        self.side = side
        self.price = price
        self.volume = volume
        self.txid = None
        self.status = "pending"
        self.placed_at = 0.0

    def __repr__(self):
        return (f"GridOrder(L{self.level:+d} {self.side} "
                f"{self.volume:.2f} DOGE @ ${self.price:.6f} "
                f"[{self.status}] txid={self.txid})")


class GridState:
    """
    Complete state of the grid trading system.
    This is the "brain" -- it tracks everything the bot needs to make decisions.
    """
    def __init__(self):
        # Grid geometry
        self.center_price = 0.0          # Price the grid is built around
        self.grid_orders: list = []      # List of GridOrder objects

        # Tracking
        self.total_profit_usd = 0.0      # Cumulative realized profit
        self.today_profit_usd = 0.0      # Today's realized profit (resets at midnight UTC)
        self.today_loss_usd = 0.0        # Today's realized losses (for daily loss limit)
        self.today_date = ""             # Current date string for reset detection
        self.round_trips_today = 0       # Completed buy->sell cycles today
        self.total_round_trips = 0       # Lifetime round trips
        self.total_fees_usd = 0.0        # Lifetime fees paid
        self.doge_accumulated = 0.0      # DOGE bought with excess profits
        self.last_accumulation = 0.0     # Timestamp of last DOGE sweep

        # Risk state
        self.is_paused = False           # True if daily loss limit hit
        self.pause_reason = ""
        self.consecutive_errors = 0      # Consecutive API failures

        # AI advisor
        self.last_ai_check = 0.0         # Timestamp of last AI analysis
        self.ai_recommendation = ""      # Latest AI recommendation

        # Price tracking for AI context
        self.price_history: list = []    # List of (timestamp, price) tuples
        self.recent_fills: list = []     # Recent fills for AI context

        # Trend ratio (asymmetric grid)
        self.trend_ratio = 0.5           # Buy-side fraction (0.0-1.0), 0.5 = symmetric
        self.last_build_ratio = 0.5      # Ratio used for current grid (for drift detection)
        self.trend_ratio_override = None # Manual override via /ratio command (None = auto)


# ---------------------------------------------------------------------------
# Trend ratio (asymmetric grid)
# ---------------------------------------------------------------------------

TREND_WINDOW_SECONDS = 43200  # 12 hours

def update_trend_ratio(state: GridState):
    """
    Compute trend ratio from recent fill history.

    Sell fills = price going up = want more buys (buy pullbacks).
    Raw ratio = sell_count / total.  Confidence-scaled toward 0.5
    when there are few fills.  Clamped to [0.25, 0.75].

    Skipped if a manual override is active.
    """
    if state.trend_ratio_override is not None:
        return

    now = time.time()
    cutoff = now - TREND_WINDOW_SECONDS

    buy_count = 0
    sell_count = 0
    for f in state.recent_fills:
        if f.get("time", 0) > cutoff:
            if f["side"] == "buy":
                buy_count += 1
            else:
                sell_count += 1

    total = buy_count + sell_count
    if total == 0:
        new_ratio = 0.5
    else:
        raw_ratio = sell_count / total
        confidence = min(1.0, total / 8)
        new_ratio = 0.5 + (raw_ratio - 0.5) * confidence

    new_ratio = max(0.25, min(0.75, new_ratio))

    old_ratio = state.trend_ratio
    state.trend_ratio = new_ratio

    if abs(new_ratio - old_ratio) >= 0.05:
        logger.info(
            "Trend ratio: %.2f -> %.2f (%d buys, %d sells in %dh window)",
            old_ratio, new_ratio, buy_count, sell_count,
            TREND_WINDOW_SECONDS // 3600,
        )


# ---------------------------------------------------------------------------
# Grid calculation
# ---------------------------------------------------------------------------

def calculate_grid_levels(center_price: float,
                          buy_levels: int = None,
                          sell_levels: int = None) -> list:
    """
    Calculate grid price levels around a center price.

    Example with center=$0.09, spacing=1%, levels=4:
      Sell levels: +1=0.0909, +2=0.0918, +3=0.0927, +4=0.0936
      Buy levels:  -1=0.0891, -2=0.0882, -3=0.0873, -4=0.0864

    buy_levels/sell_levels override config.GRID_LEVELS for asymmetric grids.

    Returns a list of (level_index, side, price) tuples, sorted by price ascending.
    """
    if buy_levels is None:
        buy_levels = config.GRID_LEVELS
    if sell_levels is None:
        sell_levels = config.GRID_LEVELS

    spacing_mult = config.GRID_SPACING_PCT / 100.0
    levels = []

    # Buy levels (below center)
    for i in range(1, buy_levels + 1):
        price = center_price * (1.0 - spacing_mult * i)
        levels.append((-i, "buy", price))

    # Sell levels (above center)
    for i in range(1, sell_levels + 1):
        price = center_price * (1.0 + spacing_mult * i)
        levels.append((i, "sell", price))

    # Sort by price (buys first, then sells)
    levels.sort(key=lambda x: x[2])
    return levels


ORDERMIN_DOGE = 13       # Kraken minimum order volume for XDGUSD
COSTMIN_USD = 0.50       # Kraken minimum order cost for USD pairs
CAPITAL_BUDGET_PCT = 0.6 # Fraction of starting capital for worst-case buy exposure
MIN_GRID_LEVELS = 3      # Floor: always at least 6 total orders
MAX_GRID_LEVELS = 20     # Ceiling: 40 total orders (rate-limit safe at 0.5s each)


def adapt_grid_params(state: GridState, current_price: float):
    """
    Recalculate ORDER_SIZE_USD and GRID_LEVELS to maximize grid resolution
    at the current DOGE price while staying within capital constraints.

    Order size hugs Kraken's 13 DOGE minimum (+ 20% buffer).
    Grid levels fill the remaining capital budget.
    Profits grow the effective capital, so the grid expands as you earn.
    """
    # Order size: 20% above Kraken volume minimum, or cost minimum
    min_by_volume = ORDERMIN_DOGE * current_price * 1.2
    order_size = max(min_by_volume, COSTMIN_USD)

    # Effective capital = starting + accumulated profits
    effective_capital = config.STARTING_CAPITAL + max(0, state.total_profit_usd)

    # Grid levels: fit as many as capital allows
    # Worst case at max trend ratio (0.75): 75% of total are buys
    # total = GRID_LEVELS * 2, max_buys = 1.5 * GRID_LEVELS
    budget = effective_capital * CAPITAL_BUDGET_PCT
    levels = int(budget / (1.5 * order_size))
    levels = max(MIN_GRID_LEVELS, min(MAX_GRID_LEVELS, levels))

    old_size = config.ORDER_SIZE_USD
    old_levels = config.GRID_LEVELS
    config.ORDER_SIZE_USD = round(order_size, 2)
    config.GRID_LEVELS = levels

    if abs(config.ORDER_SIZE_USD - old_size) >= 0.01 or config.GRID_LEVELS != old_levels:
        total = levels * 2
        worst_case = int(levels * 1.5) * config.ORDER_SIZE_USD
        logger.info(
            "Adaptive grid: $%.2f/order (%d DOGE), %d levels (%d total), "
            "worst-case $%.0f of $%.0f effective capital ($%.0f start + $%.2f profit)",
            config.ORDER_SIZE_USD,
            max(ORDERMIN_DOGE, int(config.ORDER_SIZE_USD / current_price)),
            levels, total, worst_case, effective_capital,
            config.STARTING_CAPITAL, max(0, state.total_profit_usd),
        )


def calculate_volume_for_price(price: float) -> float:
    """
    Calculate how many DOGE to buy/sell at a given price to match ORDER_SIZE_USD.
    Enforces Kraken's 13 DOGE minimum -- if ORDER_SIZE_USD is too small at
    the current price, volume is floored to ORDERMIN_DOGE (order costs more).

    Example: at $0.09/DOGE with ORDER_SIZE_USD=$5:
      volume = $5 / $0.09 = 55.56 DOGE
    """
    if price <= 0:
        return 0.0
    volume = config.ORDER_SIZE_USD / price
    if volume < ORDERMIN_DOGE:
        volume = float(ORDERMIN_DOGE)
    return volume


# ---------------------------------------------------------------------------
# Grid lifecycle
# ---------------------------------------------------------------------------

def build_grid(state: GridState, current_price: float) -> list:
    """
    Build a fresh grid centered on current_price.

    1. Calculate all level prices
    2. Calculate volume for each level
    3. Create GridOrder objects
    4. Place orders via Kraken (or simulate in dry run)

    Returns the list of placed GridOrder objects.
    """
    # Adapt order size and level count for current price and profits
    adapt_grid_params(state, current_price)

    state.center_price = current_price
    state.grid_orders = []

    # Compute asymmetric level split from trend ratio
    total = config.GRID_LEVELS * 2
    n_buys = max(2, min(total - 2, round(total * state.trend_ratio)))
    n_sells = total - n_buys
    state.last_build_ratio = state.trend_ratio

    levels = calculate_grid_levels(current_price, buy_levels=n_buys, sell_levels=n_sells)
    logger.info(
        "Building grid centered at $%.6f: %d buys + %d sells (ratio=%.2f)",
        current_price, n_buys, n_sells, state.trend_ratio,
    )

    placed_orders = []

    for level_idx, side, price in levels:
        volume = calculate_volume_for_price(price)

        order = GridOrder(level=level_idx, side=side, price=price, volume=volume)

        try:
            txid = kraken_client.place_order(
                side=side,
                volume=volume,
                price=price,
            )
            order.txid = txid
            order.status = "open"
            order.placed_at = time.time()
            placed_orders.append(order)

            logger.info(
                "  Grid L%+d: %s %.2f DOGE @ $%.6f ($%.2f) -> %s",
                level_idx, side.upper(), volume, price, volume * price, txid,
            )

        except Exception as e:
            logger.error("Failed to place grid order L%+d: %s", level_idx, e)
            order.status = "failed"

        # Small delay between orders to be kind to Kraken's rate limiter
        if not config.DRY_RUN:
            time.sleep(0.5)

    state.grid_orders = placed_orders
    return placed_orders


def cancel_grid(state: GridState) -> int:
    """
    Cancel all orders in the current grid.
    Returns the number of orders cancelled.
    """
    cancelled = 0

    if config.DRY_RUN:
        # In dry run, just mark them all cancelled
        for order in state.grid_orders:
            if order.status == "open":
                order.status = "cancelled"
                cancelled += 1
        logger.info("[DRY RUN] Cancelled %d grid orders", cancelled)
    else:
        # In live mode, cancel via API
        count = kraken_client.cancel_all_orders()
        for order in state.grid_orders:
            order.status = "cancelled"
        cancelled = count
        logger.info("Cancelled %d orders via API", count)

    return cancelled


# ---------------------------------------------------------------------------
# Fill detection
# ---------------------------------------------------------------------------

def check_fills_live(state: GridState) -> list:
    """
    [LIVE MODE] Check which grid orders have been filled by querying Kraken.

    Returns a list of GridOrder objects that were filled since last check.
    """
    open_orders = [o for o in state.grid_orders if o.status == "open"]
    if not open_orders:
        return []

    # Query order status from Kraken
    txids = [o.txid for o in open_orders if o.txid]
    if not txids:
        return []

    try:
        order_info = kraken_client.query_orders(txids)
    except Exception as e:
        logger.error("Failed to query orders: %s", e)
        state.consecutive_errors += 1
        return []

    state.consecutive_errors = 0  # Reset on success
    filled = []

    for order in open_orders:
        if order.txid in order_info:
            info = order_info[order.txid]
            status = info.get("status", "")

            if status == "closed":
                # Order fully filled!
                order.status = "filled"
                filled.append(order)
                logger.info(
                    "FILLED: %s %.2f DOGE @ $%.6f (L%+d)",
                    order.side.upper(), order.volume, order.price, order.level,
                )

    return filled


def check_fills_dry_run(state: GridState, current_price: float) -> list:
    """
    [DRY RUN] Simulate fills based on current price.

    Logic:
      - A buy order fills if current_price <= order.price
        (price dipped to or below our buy level)
      - A sell order fills if current_price >= order.price
        (price rose to or above our sell level)

    This is a simplification -- real fills depend on order book depth,
    but it's good enough for strategy validation.
    """
    filled = []

    for order in state.grid_orders:
        if order.status != "open":
            continue

        if order.side == "buy" and current_price <= order.price:
            order.status = "filled"
            filled.append(order)
            logger.info(
                "[DRY RUN] FILLED: BUY %.2f DOGE @ $%.6f (L%+d) -- price=$%.6f",
                order.volume, order.price, order.level, current_price,
            )

        elif order.side == "sell" and current_price >= order.price:
            order.status = "filled"
            filled.append(order)
            logger.info(
                "[DRY RUN] FILLED: SELL %.2f DOGE @ $%.6f (L%+d) -- price=$%.6f",
                order.volume, order.price, order.level, current_price,
            )

    return filled


def check_fills(state: GridState, current_price: float = 0.0) -> list:
    """
    Dispatch to live or dry-run fill detection.
    """
    if config.DRY_RUN:
        return check_fills_dry_run(state, current_price)
    else:
        return check_fills_live(state)


# ---------------------------------------------------------------------------
# Pair cycling -- the money-making core
# ---------------------------------------------------------------------------

def handle_fills(state: GridState, filled_orders: list) -> list:
    """
    For each filled order, place the opposite order one grid level away.

    When a BUY fills at level -2:
      -> Place a SELL at level -1 (one level UP)
      -> This completes the buy->sell pair when the sell eventually fills

    When a SELL fills at level +2:
      -> Place a BUY at level +1 (one level DOWN)
      -> This completes the sell->buy pair when the buy eventually fills

    Each completed pair captures (grid_spacing - round_trip_fees) as profit.

    Returns list of new GridOrder objects placed.
    """
    new_orders = []

    for filled in filled_orders:
        # Calculate the opposite order
        if filled.side == "buy":
            # Buy filled -> place sell one level up
            new_level = filled.level + 1
            new_side = "sell"
            new_price = filled.price * (1.0 + config.GRID_SPACING_PCT / 100.0)

            # Calculate profit when this sell eventually fills
            # Profit = sell_price - buy_price - fees_on_both_sides
            expected_profit = (new_price - filled.price) * filled.volume
            fees = (filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0 +
                    new_price * filled.volume * config.MAKER_FEE_PCT / 100.0)
            net_profit = expected_profit - fees

            logger.info(
                "  Buy filled at $%.6f -> placing sell at $%.6f "
                "(expected net profit: $%.4f)",
                filled.price, new_price, net_profit,
            )

        else:
            # Sell filled -> place buy one level down
            new_level = filled.level - 1
            new_side = "buy"
            new_price = filled.price * (1.0 - config.GRID_SPACING_PCT / 100.0)

            # Record profit from completed sell
            # When a sell fills, we already bought lower -> that's a completed round trip
            gross_profit = filled.volume * filled.price * (config.GRID_SPACING_PCT / 100.0)
            fees = filled.volume * filled.price * config.ROUND_TRIP_FEE_PCT / 100.0
            net_profit = gross_profit - fees

            state.total_profit_usd += net_profit
            state.today_profit_usd += net_profit
            state.total_round_trips += 1
            state.round_trips_today += 1
            state.total_fees_usd += fees

            if net_profit < 0:
                state.today_loss_usd += abs(net_profit)

            logger.info(
                "  ROUND TRIP COMPLETE! Sell at $%.6f -> profit: $%.4f "
                "(fees: $%.4f) | Total: $%.4f (%d trips)",
                filled.price, net_profit, fees,
                state.total_profit_usd, state.total_round_trips,
            )

            # Log to CSV
            _log_trade(filled, net_profit, fees)

            # Track for notifications
            state.recent_fills.append({
                "time": time.time(),
                "side": "sell",
                "price": filled.price,
                "volume": filled.volume,
                "profit": net_profit,
                "fees": fees,
            })

            logger.info(
                "  Sell filled at $%.6f -> placing buy at $%.6f",
                filled.price, new_price,
            )

        # Place the new opposite order
        volume = calculate_volume_for_price(new_price)
        new_order = GridOrder(level=new_level, side=new_side, price=new_price, volume=volume)

        try:
            txid = kraken_client.place_order(
                side=new_side,
                volume=volume,
                price=new_price,
            )
            new_order.txid = txid
            new_order.status = "open"
            new_order.placed_at = time.time()
            new_orders.append(new_order)
            state.grid_orders.append(new_order)

        except Exception as e:
            logger.error("Failed to place replacement order: %s", e)
            new_order.status = "failed"
            state.consecutive_errors += 1

        # Also log the buy fill
        if filled.side == "buy":
            state.recent_fills.append({
                "time": time.time(),
                "side": "buy",
                "price": filled.price,
                "volume": filled.volume,
                "profit": 0,
                "fees": filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0,
            })

    return new_orders


# ---------------------------------------------------------------------------
# Grid drift detection
# ---------------------------------------------------------------------------

def check_grid_drift(state: GridState, current_price: float) -> bool:
    """
    Check if the price has moved too far from the grid center.

    If price moves more than GRID_DRIFT_RESET_PCT from center,
    the grid is "stale" -- most orders are too far from current price
    to fill, and we should rebuild around the new price.

    Returns True if a reset is needed.
    """
    if state.center_price <= 0:
        return False

    drift_pct = abs(current_price - state.center_price) / state.center_price * 100.0

    if drift_pct >= config.GRID_DRIFT_RESET_PCT:
        logger.warning(
            "Grid drift detected! Price $%.6f is %.2f%% from center $%.6f "
            "(threshold: %.1f%%)",
            current_price, drift_pct, state.center_price, config.GRID_DRIFT_RESET_PCT,
        )
        return True

    return False


# ---------------------------------------------------------------------------
# Risk checks
# ---------------------------------------------------------------------------

def check_daily_reset(state: GridState):
    """
    Reset daily counters at midnight UTC.
    Called every loop iteration to detect the day boundary.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.today_date != today:
        if state.today_date:
            # Log previous day's summary
            logger.info(
                "Day ended: %s | Profit: $%.4f | Losses: $%.4f | Round trips: %d",
                state.today_date, state.today_profit_usd,
                state.today_loss_usd, state.round_trips_today,
            )
            _log_daily_summary(state)

        state.today_date = today
        state.today_profit_usd = 0.0
        state.today_loss_usd = 0.0
        state.round_trips_today = 0
        state.is_paused = False
        state.pause_reason = ""
        logger.info("New trading day: %s", today)


def check_risk_limits(state: GridState, current_price: float) -> tuple:
    """
    Check all risk limits.  Returns (should_stop, should_pause, reason).

    should_stop:  True -> cancel everything and shut down (stop floor breached)
    should_pause: True -> pause trading for rest of day (daily loss limit)
    """
    # Check daily loss limit
    if state.today_loss_usd >= config.DAILY_LOSS_LIMIT:
        return (False, True, f"Daily loss limit hit: ${state.today_loss_usd:.2f} >= ${config.DAILY_LOSS_LIMIT:.2f}")

    # Check stop floor
    # Estimate portfolio value: cash + value of any DOGE held
    # In a grid bot, our max exposure is GRID_LEVELS * ORDER_SIZE_USD on each side
    # Simple approximation: starting capital - max possible loss from open buys
    open_buys = [o for o in state.grid_orders if o.side == "buy" and o.status == "open"]
    filled_buys = [o for o in state.grid_orders if o.side == "buy" and o.status == "filled"]

    # Worst case: all filled buys are now worth less
    unrealized_loss = 0.0
    for order in filled_buys:
        unrealized_loss += (order.price - current_price) * order.volume

    estimated_value = config.STARTING_CAPITAL + state.total_profit_usd - max(0, unrealized_loss)

    if estimated_value < config.STOP_FLOOR:
        return (True, False, f"Stop floor breached: estimated value ${estimated_value:.2f} < ${config.STOP_FLOOR:.2f}")

    # Check consecutive errors
    if state.consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
        return (True, False, f"Too many consecutive API errors: {state.consecutive_errors}")

    return (False, False, "")


# ---------------------------------------------------------------------------
# DOGE accumulation
# ---------------------------------------------------------------------------

def check_accumulation(state: GridState) -> float:
    """
    Check if we should sweep excess profits into DOGE.

    Logic:
      1. Calculate days since last sweep
      2. If enough time has passed and there's excess profit, return the USD amount
      3. "Excess" = total profit minus monthly reserve proration

    Returns USD amount to accumulate (0 if not time yet or no excess).
    """
    now = time.time()

    # Don't sweep too frequently
    if state.last_accumulation > 0:
        days_since = (now - state.last_accumulation) / 86400
        if days_since < config.ACCUMULATION_SWEEP_DAYS:
            return 0.0

    # Calculate how much profit to reserve for hosting
    # Prorate: if we've been running for 15 days of a 30-day month,
    # reserve 15/30 * $5 = $2.50
    days_running = max(1, (now - state.last_accumulation) / 86400) if state.last_accumulation > 0 else 1
    daily_reserve = config.MONTHLY_RESERVE_USD / 30.0
    reserved = daily_reserve * min(days_running, config.ACCUMULATION_SWEEP_DAYS)

    excess = state.total_profit_usd - reserved
    if excess < 1.0:
        # Don't bother with tiny amounts (Kraken has minimum order sizes)
        return 0.0

    return excess


def execute_accumulation(state: GridState, usd_amount: float, current_price: float) -> float:
    """
    Buy DOGE with excess profit.

    Returns the DOGE amount purchased (0 if failed or dry run).
    """
    if usd_amount <= 0:
        return 0.0

    doge_amount = usd_amount / current_price

    if config.DRY_RUN:
        logger.info(
            "[DRY RUN] Would accumulate: $%.2f -> %.2f DOGE @ $%.6f",
            usd_amount, doge_amount, current_price,
        )
    else:
        try:
            # Place a market buy for the DOGE
            # NOTE: For market orders, we'd use ordertype="market"
            # but for safety, use a limit at slightly above market
            buy_price = current_price * 1.005  # 0.5% above market to ensure fill
            kraken_client.place_order("buy", doge_amount, buy_price)
            logger.info(
                "DOGE ACCUMULATED: $%.2f -> %.2f DOGE @ $%.6f",
                usd_amount, doge_amount, current_price,
            )
        except Exception as e:
            logger.error("Accumulation buy failed: %s", e)
            return 0.0

    state.doge_accumulated += doge_amount
    state.last_accumulation = time.time()
    state.total_profit_usd -= usd_amount  # Remove from available profit

    logger.info(
        "Total DOGE accumulated: %.2f (lifetime)",
        state.doge_accumulated,
    )

    return doge_amount


# ---------------------------------------------------------------------------
# Price tracking (for AI advisor context)
# ---------------------------------------------------------------------------

def record_price(state: GridState, price: float):
    """
    Record a price sample for trend analysis.
    Keeps last 24 hours of data (at 30s intervals = ~2880 samples).
    """
    now = time.time()
    state.price_history.append((now, price))

    # Trim to last 24 hours
    cutoff = now - 86400
    state.price_history = [(t, p) for t, p in state.price_history if t > cutoff]


def get_price_changes(state: GridState, current_price: float) -> dict:
    """
    Calculate 1h, 4h, and 24h price changes for AI advisor context.
    Returns dict with percentage changes.
    """
    now = time.time()
    changes = {"1h": 0.0, "4h": 0.0, "24h": 0.0}

    for label, seconds in [("1h", 3600), ("4h", 14400), ("24h", 86400)]:
        target_time = now - seconds
        # Find the closest price sample to the target time
        closest = None
        for t, p in state.price_history:
            if closest is None or abs(t - target_time) < abs(closest[0] - target_time):
                closest = (t, p)

        if closest and closest[1] > 0:
            changes[label] = ((current_price - closest[1]) / closest[1]) * 100.0

    return changes


# ---------------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------------

def _ensure_log_dir():
    """Create the log directory if it doesn't exist."""
    os.makedirs(config.LOG_DIR, exist_ok=True)


def _log_trade(order: GridOrder, net_profit: float, fees: float):
    """Append a trade record to trades.csv."""
    _ensure_log_dir()
    filepath = os.path.join(config.LOG_DIR, "trades.csv")
    file_exists = os.path.exists(filepath)

    try:
        with open(filepath, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "side", "price", "amount", "fee",
                    "profit", "grid_level",
                ])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                order.side,
                f"{order.price:.6f}",
                f"{order.volume:.2f}",
                f"{fees:.4f}",
                f"{net_profit:.4f}",
                order.level,
            ])
    except Exception as e:
        logger.error("Failed to write trade log: %s", e)


def _log_daily_summary(state: GridState):
    """Append a daily summary record to daily_summary.csv."""
    _ensure_log_dir()
    filepath = os.path.join(config.LOG_DIR, "daily_summary.csv")
    file_exists = os.path.exists(filepath)

    try:
        with open(filepath, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "date", "trades_count", "gross_profit", "fees_paid",
                    "net_profit", "doge_accumulated",
                ])
            writer.writerow([
                state.today_date,
                state.round_trips_today,
                f"{state.today_profit_usd + state.total_fees_usd:.4f}",
                f"{state.total_fees_usd:.4f}",
                f"{state.today_profit_usd:.4f}",
                f"{state.doge_accumulated:.2f}",
            ])
    except Exception as e:
        logger.error("Failed to write daily summary: %s", e)


def get_status_summary(state: GridState, current_price: float) -> str:
    """
    Generate a human-readable status summary.
    Used for health checks and Telegram status messages.
    """
    open_orders = [o for o in state.grid_orders if o.status == "open"]
    open_buys = len([o for o in open_orders if o.side == "buy"])
    open_sells = len([o for o in open_orders if o.side == "sell"])

    prefix = "[DRY RUN] " if config.DRY_RUN else ""

    # Trend ratio display
    ratio = state.trend_ratio
    total = config.GRID_LEVELS * 2
    n_buys = max(2, min(total - 2, round(total * ratio)))
    n_sells = total - n_buys
    ratio_src = "manual" if state.trend_ratio_override is not None else "auto"

    lines = [
        f"{prefix}DOGE Grid Bot Status",
        f"Price: ${current_price:.6f}",
        f"Grid center: ${state.center_price:.6f}",
        f"Open orders: {open_buys} buys + {open_sells} sells = {len(open_orders)}",
        f"Trend ratio: {ratio:.0%} buy / {1-ratio:.0%} sell (grid: {n_buys}B+{n_sells}S) [{ratio_src}]",
        f"Today: {state.round_trips_today} round trips, ${state.today_profit_usd:.4f} profit",
        f"Lifetime: {state.total_round_trips} round trips, ${state.total_profit_usd:.4f} profit",
        f"Fees paid: ${state.total_fees_usd:.4f}",
        f"DOGE accumulated: {state.doge_accumulated:.2f}",
    ]

    if state.is_paused:
        lines.append(f"PAUSED: {state.pause_reason}")
    if state.ai_recommendation:
        lines.append(f"AI says: {state.ai_recommendation}")

    return "\n".join(lines)
