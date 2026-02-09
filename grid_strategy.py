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
import json
from datetime import datetime, timezone

import config
import kraken_client
import supabase_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class CompletedCycle:
    """
    Record of one completed round-trip (entry + exit).

    Attributes:
        trade_id:       "A" (short-side) or "B" (long-side)
        cycle:          Cycle number that completed
        entry_side:     "sell" (Trade A) or "buy" (Trade B)
        entry_price:    Entry fill price
        exit_price:     Exit fill price
        volume:         DOGE traded
        gross_profit:   (sell - buy) * volume, before fees
        fees:           Total fees (entry + exit legs)
        net_profit:     gross_profit - fees
        entry_time:     Unix timestamp of entry fill (0 if unknown)
        exit_time:      Unix timestamp of exit fill
    """
    def __init__(self, trade_id: str, cycle: int, entry_side: str,
                 entry_price: float, exit_price: float, volume: float,
                 gross_profit: float, fees: float, net_profit: float,
                 entry_time: float = 0.0, exit_time: float = 0.0):
        self.trade_id = trade_id
        self.cycle = cycle
        self.entry_side = entry_side
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.volume = volume
        self.gross_profit = gross_profit
        self.fees = fees
        self.net_profit = net_profit
        self.entry_time = entry_time
        self.exit_time = exit_time

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "cycle": self.cycle,
            "entry_side": self.entry_side,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "volume": self.volume,
            "gross_profit": round(self.gross_profit, 6),
            "fees": round(self.fees, 6),
            "net_profit": round(self.net_profit, 6),
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
        }

    @staticmethod
    def from_dict(d: dict) -> "CompletedCycle":
        return CompletedCycle(
            trade_id=d.get("trade_id", "?"),
            cycle=d.get("cycle", 0),
            entry_side=d.get("entry_side", ""),
            entry_price=d.get("entry_price", 0.0),
            exit_price=d.get("exit_price", 0.0),
            volume=d.get("volume", 0.0),
            gross_profit=d.get("gross_profit", 0.0),
            fees=d.get("fees", 0.0),
            net_profit=d.get("net_profit", 0.0),
            entry_time=d.get("entry_time", 0.0),
            exit_time=d.get("exit_time", 0.0),
        )

    def __repr__(self):
        return (f"CompletedCycle({self.trade_id}.{self.cycle} "
                f"{self.entry_side} ${self.entry_price:.6f} -> "
                f"${self.exit_price:.6f} net=${self.net_profit:.4f})")


class GridOrder:
    """
    Represents one order in the grid.

    Attributes:
        level:              Grid level index (-4 to +4, 0 is center, negative = buy, positive = sell)
        side:               "buy" or "sell"
        price:              Limit price in USD
        volume:             DOGE amount
        txid:               Kraken transaction ID (or dry-run fake ID)
        status:             "pending", "open", "filled", "cancelled"
        placed_at:          Unix timestamp when placed
        matched_buy_price:  For sell orders: the buy price this sell is paired with (cost basis)
        closed_out:         True when a buy's paired sell has completed (round trip done)
        trade_id:           "A" (short-side) or "B" (long-side) -- pair mode identity
        cycle:              Cycle/generation number, increments on round-trip completion
        order_role:         "entry" (flank market) or "exit" (profit target)
        matched_sell_price: For buy exits: the sell price this buy is paired with
    """
    def __init__(self, level: int, side: str, price: float, volume: float):
        self.level = level
        self.side = side
        self.price = price
        self.volume = volume
        self.txid = None
        self.status = "pending"
        self.placed_at = 0.0
        self.matched_buy_price = None
        self.closed_out = False
        # Pair mode fields
        self.order_role = "entry"          # "entry" (flank market) or "exit" (profit target)
        self.matched_sell_price = None     # For buy exits: the sell price this buy is paired with
        self.trade_id = None               # "A" (short/sell-entry) or "B" (long/buy-entry)
        self.cycle = 0                     # Increments each completed round trip

    def __repr__(self):
        tid = f" {self.trade_id}.{self.cycle}" if self.trade_id else ""
        role = f" {self.order_role}" if self.order_role else ""
        return (f"GridOrder({self.side}{role}{tid} "
                f"{self.volume:.2f} DOGE @ ${self.price:.6f} "
                f"[{self.status}] txid={self.txid})")


class GridState:
    """
    Complete state of the grid trading system.
    This is the "brain" -- it tracks everything the bot needs to make decisions.

    An optional pair_config (config.PairConfig) provides per-pair overrides.
    If set, properties like entry_pct, profit_pct, etc. read from pair_config;
    otherwise they fall back to global config values.
    """
    def __init__(self, pair_config=None):
        # Per-pair config (None = use global config, backward compatible)
        self.pair_config = pair_config

        # Grid geometry
        self.center_price = 0.0          # Price the grid is built around
        self.grid_orders: list = []      # List of GridOrder objects

        # Tracking
        self.total_profit_usd = 0.0      # Cumulative realized profit
        self.today_profit_usd = 0.0      # Today's realized profit (resets at midnight UTC)
        self.today_loss_usd = 0.0        # Today's realized losses (for daily loss limit)
        self.today_fees_usd = 0.0       # Today's fees paid (resets at midnight UTC)
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

        # AI advisor (manual-only: init to now so it doesn't auto-fire)
        self.last_ai_check = time.time()
        self.ai_recommendation = ""      # Latest AI recommendation
        self._last_ai_result = None      # Full AI council result dict for dashboard

        # Price tracking for AI context
        self.price_history: list = []    # List of (timestamp, price) tuples
        self.recent_fills: list = []     # Recent fills for AI context

        # Trend ratio (asymmetric grid)
        self.trend_ratio = 0.5           # Buy-side fraction (0.0-1.0), 0.5 = symmetric
        self.last_build_ratio = 0.5      # Ratio used for current grid (for drift detection)
        self.trend_ratio_override = None # Manual override via /ratio command (None = auto)

        # Statistical analysis
        self.stats_results = {}          # Latest stats_engine.run_all() output
        self.stats_last_run = 0.0        # Timestamp of last stats run
        self.pair_stats = None           # Latest PairStats object (pair mode)

        # Pair mode: explicit state machine and cycle counters
        # States: "S0" (both entries), "S1a" (A exit + B entry),
        #         "S1b" (A entry + B exit), "S2" (both exits)
        self.pair_state = "S0"
        self.cycle_a = 1                 # Trade A current cycle number
        self.cycle_b = 1                 # Trade B current cycle number

        # Completed round-trip records (most recent N kept for dashboard/stats)
        self.completed_cycles: list = []  # List of CompletedCycle objects

        # P&L migration flag (set True after retroactive reconstruction)
        self._pnl_migrated = False

        # Entry placement/fill counters (for fill rate stat)
        self.total_entries_placed = 0
        self.total_entries_filled = 0

        # Anti-chase: prevent rapid same-direction refreshes during trends
        self.consecutive_refreshes_a = 0
        self.consecutive_refreshes_b = 0
        self.last_refresh_direction_a = None  # "up" or "down"
        self.last_refresh_direction_b = None
        self.refresh_cooldown_until_a = 0.0
        self.refresh_cooldown_until_b = 0.0

    # -- Per-pair property accessors (fall back to global config) --

    @property
    def pair_name(self) -> str:
        if self.pair_config:
            return self.pair_config.pair
        return config.PAIR

    @property
    def pair_display(self) -> str:
        if self.pair_config:
            return self.pair_config.display
        return config.PAIR_DISPLAY

    @property
    def entry_pct(self) -> float:
        if self.pair_config:
            return self.pair_config.entry_pct
        return config.PAIR_ENTRY_PCT

    @entry_pct.setter
    def entry_pct(self, val):
        if self.pair_config:
            self.pair_config.entry_pct = val
        else:
            config.PAIR_ENTRY_PCT = val

    @property
    def profit_pct(self) -> float:
        if self.pair_config:
            return self.pair_config.profit_pct
        return config.PAIR_PROFIT_PCT

    @profit_pct.setter
    def profit_pct(self, val):
        if self.pair_config:
            self.pair_config.profit_pct = val
        else:
            config.PAIR_PROFIT_PCT = val

    @property
    def refresh_pct(self) -> float:
        if self.pair_config:
            return self.pair_config.refresh_pct
        return config.PAIR_REFRESH_PCT

    @property
    def min_volume(self) -> float:
        if self.pair_config:
            return self.pair_config.min_volume
        return ORDERMIN_DOGE

    @property
    def price_decimals(self) -> int:
        if self.pair_config:
            return self.pair_config.price_decimals
        return 6

    @property
    def volume_decimals(self) -> int:
        if self.pair_config:
            return self.pair_config.volume_decimals
        return 0

    @property
    def order_size_usd(self) -> float:
        if self.pair_config:
            return self.pair_config.order_size_usd
        return config.ORDER_SIZE_USD

    @order_size_usd.setter
    def order_size_usd(self, val):
        if self.pair_config:
            self.pair_config.order_size_usd = val
        else:
            config.ORDER_SIZE_USD = val

    @property
    def daily_loss_limit(self) -> float:
        if self.pair_config:
            return self.pair_config.daily_loss_limit
        return config.DAILY_LOSS_LIMIT

    @property
    def stop_floor(self) -> float:
        if self.pair_config:
            return self.pair_config.stop_floor
        return config.STOP_FLOOR


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _state_file_path(state: GridState) -> str:
    """Return the state file path for this pair (or the global default)."""
    if state.pair_config and len(config.PAIRS) > 1:
        return os.path.join(config.LOG_DIR, f"state_{state.pair_name}.json")
    return config.STATE_FILE


def save_state(state: GridState):
    """
    Save a minimal state snapshot to disk for crash recovery.
    Written atomically (write tmp then rename) to avoid corruption.
    """
    _ensure_log_dir()
    # Build per-order identity snapshots for pair mode (survives restarts)
    open_order_details = []
    for o in state.grid_orders:
        if o.status == "open" and o.txid:
            open_order_details.append({
                "txid": o.txid,
                "side": o.side,
                "price": o.price,
                "volume": o.volume,
                "order_role": o.order_role,
                "trade_id": getattr(o, "trade_id", None),
                "cycle": getattr(o, "cycle", 0),
                "matched_buy_price": o.matched_buy_price,
                "matched_sell_price": getattr(o, "matched_sell_price", None),
            })

    snapshot = {
        "center_price": state.center_price,
        "total_profit_usd": state.total_profit_usd,
        "today_profit_usd": state.today_profit_usd,
        "today_loss_usd": state.today_loss_usd,
        "today_fees_usd": state.today_fees_usd,
        "today_date": state.today_date,
        "round_trips_today": state.round_trips_today,
        "total_round_trips": state.total_round_trips,
        "total_fees_usd": state.total_fees_usd,
        "doge_accumulated": state.doge_accumulated,
        "last_accumulation": state.last_accumulation,
        **({"trend_ratio": state.trend_ratio,
            "trend_ratio_override": state.trend_ratio_override}
           if config.STRATEGY_MODE != "pair" else {}),
        "open_txids": [o.txid for o in state.grid_orders
                       if o.status == "open" and o.txid],
        "open_orders": open_order_details,
        "pair_state": state.pair_state,
        "cycle_a": state.cycle_a,
        "cycle_b": state.cycle_b,
        "completed_cycles": [c.to_dict() for c in state.completed_cycles],
        "pnl_migrated": state._pnl_migrated,
        "total_entries_placed": state.total_entries_placed,
        "total_entries_filled": state.total_entries_filled,
        "consecutive_refreshes_a": state.consecutive_refreshes_a,
        "consecutive_refreshes_b": state.consecutive_refreshes_b,
        "last_refresh_direction_a": state.last_refresh_direction_a,
        "last_refresh_direction_b": state.last_refresh_direction_b,
        "refresh_cooldown_until_a": state.refresh_cooldown_until_a,
        "refresh_cooldown_until_b": state.refresh_cooldown_until_b,
        "saved_at": time.time(),
        "strategy_mode": config.STRATEGY_MODE,
        # Runtime config overrides (survive deploys via Supabase)
        "grid_spacing_pct": config.GRID_SPACING_PCT,
        "pair_profit_pct": state.profit_pct,
        "pair_entry_pct": state.entry_pct,
        "ai_advisor_interval": config.AI_ADVISOR_INTERVAL,
    }
    state_path = _state_file_path(state)
    tmp_path = state_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        # Atomic rename (works on Windows if dest doesn't exist, so remove first)
        if os.path.exists(state_path):
            os.remove(state_path)
        os.rename(tmp_path, state_path)
        logger.debug("State saved to %s", state_path)
    except Exception as e:
        logger.error("Failed to save state: %s", e)

    supabase_store.save_state(snapshot, pair=state.pair_name)


def load_state(state: GridState) -> bool:
    """
    Restore counters from a previous state snapshot.
    Returns True if a snapshot was loaded, False otherwise.
    Does NOT restore grid_orders -- that's done by reconcile_on_startup().
    """
    state_path = _state_file_path(state)
    if not os.path.exists(state_path):
        logger.info("No state file found at %s -- starting fresh", state_path)
        return False

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except Exception as e:
        logger.warning("Failed to read state file: %s -- starting fresh", e)
        return False

    state.center_price = snapshot.get("center_price", 0.0)
    state.total_profit_usd = snapshot.get("total_profit_usd", 0.0)
    state.today_profit_usd = snapshot.get("today_profit_usd", 0.0)
    state.today_loss_usd = snapshot.get("today_loss_usd", 0.0)
    state.today_fees_usd = snapshot.get("today_fees_usd", 0.0)
    state.today_date = snapshot.get("today_date", "")
    state.round_trips_today = snapshot.get("round_trips_today", 0)
    state.total_round_trips = snapshot.get("total_round_trips", 0)
    state.total_fees_usd = snapshot.get("total_fees_usd", 0.0)
    state.doge_accumulated = snapshot.get("doge_accumulated", 0.0)
    state.last_accumulation = snapshot.get("last_accumulation", 0.0)
    if config.STRATEGY_MODE != "pair":
        state.trend_ratio = snapshot.get("trend_ratio", 0.5)
        state.trend_ratio_override = snapshot.get("trend_ratio_override", None)

    # Restore pair mode identity fields
    state.pair_state = snapshot.get("pair_state", "S0")
    state.cycle_a = snapshot.get("cycle_a", 1)
    state.cycle_b = snapshot.get("cycle_b", 1)
    # Store open_orders detail for reconciliation to restore identity
    state._saved_open_orders = snapshot.get("open_orders", [])

    # Restore completed cycles history
    saved_cycles = snapshot.get("completed_cycles", [])
    state.completed_cycles = [CompletedCycle.from_dict(c) for c in saved_cycles]
    state._pnl_migrated = snapshot.get("pnl_migrated", False)
    state.total_entries_placed = snapshot.get("total_entries_placed", 0)
    state.total_entries_filled = snapshot.get("total_entries_filled", 0)

    # Restore anti-chase state
    state.consecutive_refreshes_a = snapshot.get("consecutive_refreshes_a", 0)
    state.consecutive_refreshes_b = snapshot.get("consecutive_refreshes_b", 0)
    state.last_refresh_direction_a = snapshot.get("last_refresh_direction_a", None)
    state.last_refresh_direction_b = snapshot.get("last_refresh_direction_b", None)
    state.refresh_cooldown_until_a = snapshot.get("refresh_cooldown_until_a", 0.0)
    state.refresh_cooldown_until_b = snapshot.get("refresh_cooldown_until_b", 0.0)

    # Restore pair_entry_pct runtime override
    saved_entry_pct = snapshot.get("pair_entry_pct")
    if saved_entry_pct and saved_entry_pct != state.entry_pct:
        logger.info(
            "Restoring entry distance from state: %.2f%% -> %.2f%%",
            state.entry_pct, saved_entry_pct,
        )
        state.entry_pct = saved_entry_pct

    saved_at = snapshot.get("saved_at", 0)
    age_min = (time.time() - saved_at) / 60 if saved_at else 0
    txid_count = len(snapshot.get("open_txids", []))

    logger.info(
        "State restored from %s (%.0f min old): "
        "$%.4f profit, %d round trips, %d open txids, "
        "pair_state=%s cycle_a=%d cycle_b=%d",
        state_path, age_min,
        state.total_profit_usd, state.total_round_trips, txid_count,
        state.pair_state, state.cycle_a, state.cycle_b,
    )
    return True


def migrate_pnl_from_fills(state: GridState):
    """
    Retroactively reconstruct CompletedCycle records from recent_fills.

    Pairs fills by price matching (not by stored profit field, which may
    have been sanitized to zero by old code):
      - Trade A: sell (entry) followed by buy (exit) at sell × (1 - π)
      - Trade B: buy (entry) followed by sell (exit) at buy × (1 + π)

    P&L is always computed as (sell_price - buy_price) × volume - fees.
    Only runs once (sets _pnl_migrated flag).
    """
    if getattr(state, "_pnl_migrated", False):
        return

    fills = sorted(
        [f for f in state.recent_fills if f.get("time", 0) > 0],
        key=lambda f: f["time"],
    )

    if len(fills) < 2:
        state._pnl_migrated = True
        return

    # Separate into sells and buys (preserving original list indices)
    sells = [(i, f) for i, f in enumerate(fills) if f.get("side") == "sell"]
    buys = [(i, f) for i, f in enumerate(fills) if f.get("side") == "buy"]

    completed = []
    used_sells = set()
    used_buys = set()

    profit_pct = state.profit_pct / 100.0  # e.g. 0.01
    tolerance = 0.005  # 0.5% price matching tolerance

    # Trade A pairs: sell (entry) followed by buy (exit)
    # Expected exit price ≈ sell_price × (1 - π)
    for si, (s_idx, s) in enumerate(sells):
        if s_idx in used_sells:
            continue
        expected_exit = s["price"] * (1 - profit_pct)
        for bi, (b_idx, b) in enumerate(buys):
            if b_idx in used_buys:
                continue
            if b.get("time", 0) <= s.get("time", 0):
                continue  # exit must come after entry
            if expected_exit > 0 and abs(b["price"] - expected_exit) / expected_exit < tolerance:
                vol = min(s.get("volume", 0), b.get("volume", 0))
                gross = (s["price"] - b["price"]) * vol
                fee_entry = s.get("fees", vol * s["price"] * 0.0026)
                fee_exit = b.get("fees", vol * b["price"] * 0.0026)
                net = gross - fee_entry - fee_exit
                cycle_num = len([c for c in completed if c.trade_id == "A"]) + 1
                completed.append(CompletedCycle(
                    trade_id="A", cycle=cycle_num,
                    entry_side="sell",
                    entry_price=s["price"], exit_price=b["price"],
                    volume=vol, gross_profit=gross,
                    fees=fee_entry + fee_exit, net_profit=net,
                    entry_time=s.get("time", 0), exit_time=b.get("time", 0),
                ))
                used_sells.add(s_idx)
                used_buys.add(b_idx)
                break

    # Trade B pairs: buy (entry) followed by sell (exit)
    # Expected exit price ≈ buy_price × (1 + π)
    for bi, (b_idx, b) in enumerate(buys):
        if b_idx in used_buys:
            continue
        expected_exit = b["price"] * (1 + profit_pct)
        for si, (s_idx, s) in enumerate(sells):
            if s_idx in used_sells:
                continue
            if s.get("time", 0) <= b.get("time", 0):
                continue  # exit must come after entry
            if expected_exit > 0 and abs(s["price"] - expected_exit) / expected_exit < tolerance:
                vol = min(b.get("volume", 0), s.get("volume", 0))
                gross = (s["price"] - b["price"]) * vol
                fee_entry = b.get("fees", vol * b["price"] * 0.0026)
                fee_exit = s.get("fees", vol * s["price"] * 0.0026)
                net = gross - fee_entry - fee_exit
                cycle_num = len([c for c in completed if c.trade_id == "B"]) + 1
                completed.append(CompletedCycle(
                    trade_id="B", cycle=cycle_num,
                    entry_side="buy",
                    entry_price=b["price"], exit_price=s["price"],
                    volume=vol, gross_profit=gross,
                    fees=fee_entry + fee_exit, net_profit=net,
                    entry_time=b.get("time", 0), exit_time=s.get("time", 0),
                ))
                used_sells.add(s_idx)
                used_buys.add(b_idx)
                break

    if completed:
        completed.sort(key=lambda c: c.exit_time)
        if not state.completed_cycles:
            state.completed_cycles = completed
        else:
            # Merge: add only cycles not already present (by exit_time)
            existing_times = {c.exit_time for c in state.completed_cycles}
            for rc in completed:
                if rc.exit_time not in existing_times:
                    state.completed_cycles.append(rc)

        _trim_completed_cycles(state)
        # Update accumulators from reconstructed data
        state.total_profit_usd = sum(c.net_profit for c in state.completed_cycles)
        state.total_round_trips = len(state.completed_cycles)
        logger.info(
            "P&L migration: reconstructed %d cycles, total_profit=$%.4f "
            "(A: %d, B: %d)",
            len(completed), state.total_profit_usd,
            sum(1 for c in completed if c.trade_id == "A"),
            sum(1 for c in completed if c.trade_id == "B"),
        )

    state._pnl_migrated = True


# ---------------------------------------------------------------------------
# Trend ratio (asymmetric grid)
# ---------------------------------------------------------------------------

TREND_WINDOW_SECONDS = 43200  # 12 hours

# Anti-chase constants
MAX_CONSECUTIVE_REFRESHES = 3   # refreshes in same direction before cooldown
REFRESH_COOLDOWN_SEC = 300      # seconds to pause refreshes after chase detected

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
# Pre-flight validation
# ---------------------------------------------------------------------------

def validate_config(current_price: float) -> bool:
    """
    Run pre-flight checks before the first grid build.
    Returns True if safe to proceed, False if a critical check fails.
    Logs warnings for non-critical issues.
    """
    if config.STRATEGY_MODE == "pair":
        return validate_pair_config(current_price)

    ok = True
    warnings = []

    # Critical: spacing must exceed round-trip fees or every trade loses money
    if config.GRID_SPACING_PCT <= config.ROUND_TRIP_FEE_PCT:
        msg = (
            "GRID_SPACING_PCT (%.2f%%) <= ROUND_TRIP_FEE_PCT (%.2f%%) -- "
            "every trade is a guaranteed loss" % (
                config.GRID_SPACING_PCT, config.ROUND_TRIP_FEE_PCT))
        logger.critical(msg)
        warnings.append(msg)
        ok = False

    # Critical: capital check -- worst-case buy exposure must not exceed 90% of capital
    total = config.GRID_LEVELS * 2
    max_buys = int(total * 0.75)  # Worst case at max trend ratio
    max_exposure = config.ORDER_SIZE_USD * max_buys
    if max_exposure > config.STARTING_CAPITAL * 0.9:
        msg = (
            "Over-leveraged: worst-case buy exposure $%.2f > 90%% of capital $%.2f "
            "(order_size=$%.2f x %d max buys)" % (
                max_exposure, config.STARTING_CAPITAL * 0.9,
                config.ORDER_SIZE_USD, max_buys))
        logger.critical(msg)
        warnings.append(msg)
        ok = False

    # Warning: order size below Kraken minimum DOGE volume
    doge_per_order = config.ORDER_SIZE_USD / current_price if current_price > 0 else 0
    if doge_per_order < ORDERMIN_DOGE:
        msg = (
            "ORDER_SIZE_USD $%.2f = %.1f DOGE at $%.6f -- below Kraken min %d DOGE "
            "(adapt_grid_params will fix this, but order cost will exceed ORDER_SIZE_USD)" % (
                config.ORDER_SIZE_USD, doge_per_order, current_price, ORDERMIN_DOGE))
        logger.warning(msg)
        warnings.append(msg)
        # Not critical -- adapt_grid_params floors volume to ORDERMIN_DOGE

    if warnings:
        try:
            import notifier
            notifier.notify_error("Validation:\n" + "\n".join(warnings))
        except Exception:
            pass

    if ok:
        logger.info("Pre-flight validation passed")
    return ok


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


def calculate_volume_for_price(price: float, state: GridState = None) -> float:
    """
    Calculate volume at a given price to match the per-pair order size.
    Enforces Kraken's minimum volume -- if order size is too small at
    the current price, volume is floored to the minimum (order costs more).

    Args:
        price: Limit price in USD.
        state: Optional GridState; if set, uses per-pair order_size_usd and min_volume.

    Example: at $0.09/DOGE with order_size_usd=$5:
      volume = $5 / $0.09 = 55.56 DOGE
    """
    if price <= 0:
        return 0.0
    size_usd = state.order_size_usd if state else config.ORDER_SIZE_USD
    volume = size_usd / price
    min_vol = state.min_volume if state and state.pair_config else ORDERMIN_DOGE
    if volume < min_vol:
        volume = float(min_vol)
    vol_dec = state.volume_decimals if state else 0
    if vol_dec == 0:
        volume = float(int(volume))
    else:
        volume = round(volume, vol_dec)
    return volume


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------

def reconcile_on_startup(state: GridState, current_price: float) -> int:
    """
    Adopt open orders on Kraken that belong to this bot, cancel orphans.

    Called between price fetch and grid build on startup.
    1. Fetch all open orders from Kraken, filter to XDGUSD
    2. For each, check if it's near a valid grid level
    3. If yes, adopt it into state.grid_orders
    4. If no, cancel it (orphan from a previous crashed session)

    Returns the number of adopted orders.
    """
    if config.STRATEGY_MODE == "pair":
        return reconcile_pair_on_startup(state, current_price)

    if config.DRY_RUN:
        logger.info("[DRY RUN] Skipping startup reconciliation")
        return 0

    try:
        open_orders = kraken_client.get_open_orders()
    except Exception as e:
        logger.error("Reconciliation: failed to fetch open orders: %s", e)
        return 0

    if not open_orders:
        logger.info("Reconciliation: no open orders found on Kraken")
        return 0

    # Build the grid levels we'd expect for the current price
    adapt_grid_params(state, current_price)
    total = config.GRID_LEVELS * 2
    n_buys = max(2, min(total - 2, round(total * state.trend_ratio)))
    n_sells = total - n_buys
    expected_levels = calculate_grid_levels(current_price, buy_levels=n_buys, sell_levels=n_sells)
    # Build a set of (side, price) for quick matching
    level_map = {}  # price_key -> (level_idx, side, price)
    for level_idx, side, price in expected_levels:
        level_map[(side, round(price, 6))] = (level_idx, side, price)

    adopted = 0
    orphans = 0
    spacing_tolerance = config.GRID_SPACING_PCT / 100.0 * current_price * 0.3  # 30% of one spacing

    # Determine pair filter strings
    if state.pair_config:
        filter_strings = state.pair_config.filter_strings
    else:
        filter_strings = ["XDG", "DOGE"]

    for txid, info in open_orders.items():
        descr = info.get("descr", {})
        pair = descr.get("pair", "")
        # Kraken may use various pair formats
        if not any(s in pair.upper() for s in filter_strings):
            continue  # Not our pair, skip

        side = descr.get("type", "")
        order_price = float(descr.get("price", 0))
        order_vol = float(info.get("vol", 0))

        if not side or order_price <= 0:
            continue

        # Try to match to a grid level
        matched_level = None
        for (lside, lprice_key), (level_idx, _, lprice) in level_map.items():
            if lside == side and abs(order_price - lprice) <= spacing_tolerance:
                matched_level = (level_idx, lside, lprice)
                break

        if matched_level:
            level_idx, _, _ = matched_level
            order = GridOrder(level=level_idx, side=side, price=order_price, volume=order_vol)
            order.txid = txid
            order.status = "open"
            order.placed_at = time.time()
            state.grid_orders.append(order)
            adopted += 1
            # Remove from level_map so build_grid doesn't duplicate
            for key, val in list(level_map.items()):
                if val[0] == level_idx:
                    del level_map[key]
                    break
            logger.info(
                "Reconcile: adopted %s L%+d %.2f DOGE @ $%.6f -> %s",
                side.upper(), level_idx, order_vol, order_price, txid,
            )
        else:
            # Orphan -- cancel it
            logger.warning(
                "Reconcile: cancelling orphan %s %.2f DOGE @ $%.6f -> %s",
                side.upper(), order_vol, order_price, txid,
            )
            kraken_client.cancel_order(txid)
            orphans += 1

    logger.info(
        "Reconciliation complete: %d adopted, %d orphans cancelled",
        adopted, orphans,
    )
    return adopted


# ---------------------------------------------------------------------------
# Grid lifecycle
# ---------------------------------------------------------------------------

def build_grid(state: GridState, current_price: float) -> list:
    """
    Build a fresh grid centered on current_price.

    1. Calculate all level prices
    2. Calculate volume for each level
    3. Create GridOrder objects (skip levels already covered by adopted orders)
    4. Place orders via Kraken (or simulate in dry run)

    Returns the list of placed GridOrder objects.
    """
    if config.STRATEGY_MODE == "pair":
        return build_pair(state, current_price)

    # Adapt order size and level count for current price and profits
    adapt_grid_params(state, current_price)

    state.center_price = current_price

    # Identify levels already covered by adopted orders (from reconciliation)
    adopted_levels = {o.level for o in state.grid_orders if o.status == "open"}

    # Compute asymmetric level split from trend ratio
    total = config.GRID_LEVELS * 2
    n_buys = max(2, min(total - 2, round(total * state.trend_ratio)))
    n_sells = total - n_buys
    state.last_build_ratio = state.trend_ratio

    levels = calculate_grid_levels(current_price, buy_levels=n_buys, sell_levels=n_sells)
    skipped = len([l for l in levels if l[0] in adopted_levels])
    logger.info(
        "Building grid centered at $%.6f: %d buys + %d sells (ratio=%.2f, %d adopted)",
        current_price, n_buys, n_sells, state.trend_ratio, skipped,
    )

    placed_orders = [o for o in state.grid_orders if o.status == "open"]  # Keep adopted orders only

    for level_idx, side, price in levels:
        if level_idx in adopted_levels:
            logger.debug("  Grid L%+d: skipped (already adopted)", level_idx)
            continue

        volume = calculate_volume_for_price(price, state)

        order = GridOrder(level=level_idx, side=side, price=price, volume=volume)

        try:
            txid = kraken_client.place_order(
                side=side,
                volume=volume,
                price=price,
                pair=state.pair_name,
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
    Cancel all open orders in the current grid by txid.
    Only cancels orders the bot placed (not unrelated orders on the account).
    Returns the number of orders cancelled.
    """
    cancelled = 0
    open_orders = [o for o in state.grid_orders if o.status == "open" and o.txid]

    if config.DRY_RUN:
        for order in open_orders:
            order.status = "cancelled"
            cancelled += 1
        logger.info("[DRY RUN] Cancelled %d grid orders by txid", cancelled)
    else:
        for i, order in enumerate(open_orders):
            ok = kraken_client.cancel_order(order.txid)
            if ok:
                order.status = "cancelled"
                cancelled += 1
            else:
                logger.warning("Failed to cancel order %s (L%+d)", order.txid, order.level)
            # Rate-limit guard: sleep every 10 cancels
            if (i + 1) % 10 == 0 and i + 1 < len(open_orders):
                time.sleep(2)
        logger.info("Cancelled %d/%d grid orders by txid", cancelled, len(open_orders))

    return cancelled


# ---------------------------------------------------------------------------
# Fill detection
# ---------------------------------------------------------------------------

def _check_trade_history_fallback(state: GridState, open_orders: list,
                                  filled: list):
    """
    Cross-check open orders against Kraken trade history.
    If a trade matches an order we think is "open", mark it as filled.
    Called when the sanity check detects price has moved past an open order.
    """
    try:
        # Look back 1 hour of trade history
        trades = kraken_client.get_trades_history(start=time.time() - 3600)
    except Exception as e:
        logger.debug("Trade history fallback failed: %s", e)
        return

    if not trades:
        return

    # Build a set of open txids for fast lookup
    open_txids = {o.txid for o in open_orders if o.status == "open" and o.txid}

    for trade_txid, trade_info in trades.items():
        order_txid = trade_info.get("ordertxid", "")
        if order_txid not in open_txids:
            continue

        # Found a trade matching an order we think is open
        for order in open_orders:
            if order.txid != order_txid or order.status != "open":
                continue

            vol_exec = float(trade_info.get("vol", order.volume))
            if vol_exec > 0:
                order.volume = vol_exec
            order.status = "filled"
            filled.append(order)
            logger.warning(
                "TRADE HISTORY FALLBACK: %s %.2f DOGE @ $%.6f was FILLED "
                "(trade=%s, ordertxid=%s) -- QueryOrders missed it!",
                order.side.upper(), order.volume, order.price,
                trade_txid, order_txid,
            )
            break


def check_fills_live(state: GridState, current_price: float = 0.0) -> list:
    """
    [LIVE MODE] Check which grid orders have been filled by querying Kraken.

    Uses actual vol_exec from Kraken for filled volume (not our estimate).
    Logs a warning for partial fills but waits for full fill before acting.

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

    # Diagnostic: log query result counts
    if len(order_info) != len(txids):
        logger.warning(
            "QueryOrders mismatch: sent %d txids, got %d back -- missing: %s",
            len(txids), len(order_info),
            [t for t in txids if t not in order_info],
        )

    # Summarize statuses returned
    status_counts = {}
    for txid, info in order_info.items():
        s = info.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1
    status_summary = ", ".join(f"{c} {s}" for s, c in sorted(status_counts.items()))
    logger.debug(
        "QueryOrders: %d queried, %d returned (%s)",
        len(txids), len(order_info), status_summary,
    )

    filled = []

    for order in open_orders:
        if order.txid not in order_info:
            logger.warning(
                "Order %s %s @ $%.6f (txid=%s) not in QueryOrders response",
                order.side.upper(), order.order_role or "?",
                order.price, order.txid,
            )
            continue

        info = order_info[order.txid]
        status = info.get("status", "")

        if status == "closed":
            # Order fully filled -- use actual executed volume from Kraken
            vol_exec = float(info.get("vol_exec", order.volume))
            if vol_exec > 0:
                order.volume = vol_exec
            order.status = "filled"
            filled.append(order)
            logger.info(
                "FILLED: %s %.2f DOGE @ $%.6f (L%+d) [vol_exec=%.2f]",
                order.side.upper(), order.volume, order.price,
                order.level, vol_exec,
            )

        elif status in ("canceled", "expired"):
            # Kraken canceled or expired this order -- mark it so we can replace
            order.status = "cancelled"
            logger.warning(
                "ORDER %s: %s L%+d %.2f DOGE @ $%.6f -- will replace",
                status.upper(), order.side.upper(), order.level,
                order.volume, order.price,
            )

        elif status == "open":
            # Check for partial fill (still open but some volume executed)
            vol_exec = float(info.get("vol_exec", 0))
            if vol_exec > 0 and not getattr(order, '_partial_warned', False):
                logger.warning(
                    "PARTIAL FILL: %s L%+d %.2f/%.2f DOGE @ $%.6f -- waiting for full fill",
                    order.side.upper(), order.level, vol_exec, order.volume, order.price,
                )
                order._partial_warned = True

    # Sanity check: warn if price has clearly moved past an "open" order
    stale_detected = False
    if current_price > 0:
        for order in open_orders:
            if order.status != "open":
                continue
            if order.side == "buy" and current_price < order.price * 0.995:
                logger.warning(
                    "STALE OPEN? BUY @ $%.6f still 'open' but price $%.6f is "
                    "%.2f%% below -- possible missed fill (txid=%s)",
                    order.price, current_price,
                    (order.price - current_price) / order.price * 100,
                    order.txid,
                )
                stale_detected = True
            elif order.side == "sell" and current_price > order.price * 1.005:
                logger.warning(
                    "STALE OPEN? SELL @ $%.6f still 'open' but price $%.6f is "
                    "%.2f%% above -- possible missed fill (txid=%s)",
                    order.price, current_price,
                    (current_price - order.price) / order.price * 100,
                    order.txid,
                )
                stale_detected = True

    # Trade history fallback: if sanity check flagged stale orders, cross-check
    if stale_detected:
        _check_trade_history_fallback(state, open_orders, filled)

    # Replace canceled/expired orders immediately to fill grid holes
    cancelled_orders = [o for o in open_orders if o.status == "cancelled"]
    for order in cancelled_orders:
        volume = calculate_volume_for_price(order.price)
        replacement = GridOrder(
            level=order.level, side=order.side,
            price=order.price, volume=volume,
        )
        # Carry over matched_buy_price for sell replacements
        if order.matched_buy_price is not None:
            replacement.matched_buy_price = order.matched_buy_price
        try:
            txid = kraken_client.place_order(
                side=order.side, volume=volume, price=order.price,
            )
            replacement.txid = txid
            replacement.status = "open"
            replacement.placed_at = time.time()
            state.grid_orders.append(replacement)
            logger.info(
                "REPLACED %s L%+d %.2f DOGE @ $%.6f -> %s",
                order.side.upper(), order.level, volume, order.price, txid,
            )
        except Exception as e:
            logger.error(
                "Failed to replace cancelled order L%+d: %s", order.level, e,
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
        return check_fills_live(state, current_price)


# ---------------------------------------------------------------------------
# Pair cycling -- the money-making core
# ---------------------------------------------------------------------------

def handle_fills(state: GridState, filled_orders: list, current_price: float = 0.0) -> list:
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
    if config.STRATEGY_MODE == "pair":
        return handle_pair_fill(state, filled_orders, current_price)

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

            # Record profit from completed sell using actual cost basis
            if filled.matched_buy_price is not None:
                buy_price = filled.matched_buy_price
                gross_profit = (filled.price - buy_price) * filled.volume
                fees = (buy_price * filled.volume * config.MAKER_FEE_PCT / 100.0 +
                        filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0)
                net_profit = gross_profit - fees
                state.total_round_trips += 1
                state.round_trips_today += 1
            else:
                # No cost basis -- initial grid sell or orphan. Book $0 with warning.
                # NOT a completed round trip -- don't increment counters.
                logger.warning(
                    "  Sell at $%.6f has no matched_buy_price -- booking $0 profit",
                    filled.price,
                )
                net_profit = 0.0
                fees = filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0

            state.total_profit_usd += net_profit
            state.today_profit_usd += net_profit
            state.total_fees_usd += fees
            state.today_fees_usd += fees

            if net_profit < 0:
                state.today_loss_usd += abs(net_profit)

            # Mark the matched buy as closed out (Fix 3)
            if filled.matched_buy_price is not None:
                for o in state.grid_orders:
                    if (o.side == "buy" and o.status == "filled"
                            and not o.closed_out
                            and abs(o.price - filled.matched_buy_price) < 1e-9):
                        o.closed_out = True
                        break

            logger.info(
                "  ROUND TRIP COMPLETE! Sell at $%.6f (bought at $%.6f) "
                "-> profit: $%.4f (fees: $%.4f) | Total: $%.4f (%d trips)",
                filled.price,
                filled.matched_buy_price if filled.matched_buy_price is not None else 0.0,
                net_profit, fees,
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
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name)

            logger.info(
                "  Sell filled at $%.6f -> placing buy at $%.6f",
                filled.price, new_price,
            )

        # Place the new opposite order
        volume = calculate_volume_for_price(new_price)
        new_order = GridOrder(level=new_level, side=new_side, price=new_price, volume=volume)

        # Carry cost basis: if a buy filled, the replacement sell knows the buy price
        if filled.side == "buy":
            new_order.matched_buy_price = filled.price

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

        # Also log the buy fill and track its fee
        if filled.side == "buy":
            buy_fee = filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0
            state.total_fees_usd += buy_fee
            state.today_fees_usd += buy_fee
            state.recent_fills.append({
                "time": time.time(),
                "side": "buy",
                "price": filled.price,
                "volume": filled.volume,
                "profit": 0,
                "fees": buy_fee,
            })
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name)

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
    if config.STRATEGY_MODE == "pair":
        # Pair mode refreshes entries in-place (no full rebuild needed).
        # Always return False so bot.py doesn't cancel_grid + build_grid.
        refresh_stale_entries(state, current_price)
        return False

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
        state.today_fees_usd = 0.0
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
    # Check daily loss limit (per-pair if configured)
    if state.today_loss_usd >= state.daily_loss_limit:
        return (False, True, f"Daily loss limit hit: ${state.today_loss_usd:.2f} >= ${state.daily_loss_limit:.2f}")

    # Check stop floor
    if config.STRATEGY_MODE == "pair":
        # Pair mode: use signed unrealized P&L (positive = in the money)
        upnl = compute_unrealized_pnl(state, current_price)
        estimated_value = config.STARTING_CAPITAL + state.total_profit_usd + upnl["total_unrealized"]
    else:
        # Grid mode: estimate from filled-but-unclosed buys
        unrealized_loss = 0.0
        for order in state.grid_orders:
            if order.side == "buy" and order.status == "filled" and not order.closed_out:
                unrealized_loss += max(0, (order.price - current_price) * order.volume)
        estimated_value = config.STARTING_CAPITAL + state.total_profit_usd - unrealized_loss

    if estimated_value < state.stop_floor:
        return (True, False, f"Stop floor breached: estimated value ${estimated_value:.2f} < ${state.stop_floor:.2f}")

    # Check consecutive errors
    if state.consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
        return (True, False, f"Too many consecutive API errors: {state.consecutive_errors}")

    return (False, False, "")


def prune_completed_orders(state: GridState):
    """
    Remove stale orders from grid_orders to prevent unbounded memory growth.
    Keeps: all open orders, un-closed filled buys, recent filled sells.
    Prunes: closed-out buys, cancelled orders older than 1 hour.
    """
    now = time.time()
    kept = []
    pruned = 0
    for o in state.grid_orders:
        if o.status == "open":
            kept.append(o)
        elif o.status == "filled" and o.side == "buy" and o.closed_out:
            pruned += 1  # Round trip complete, safe to drop
        elif o.status == "filled" and o.side == "sell" and now - o.placed_at > 3600:
            pruned += 1  # Processed sell, safe to drop
        elif o.status in ("cancelled", "failed") and now - o.placed_at > 3600:
            pruned += 1  # Stale cancelled/failed
        else:
            kept.append(o)

    if pruned > 0:
        state.grid_orders = kept
        logger.debug("Pruned %d completed/stale orders (%d remaining)", pruned, len(kept))


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
    """Append a trade record to trades.csv and Supabase."""
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

    supabase_store.save_trade(order, net_profit, fees)


def _log_daily_summary(state: GridState):
    """Append a daily summary record to daily_summary.csv and Supabase."""
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
                f"{state.today_profit_usd + state.today_fees_usd:.4f}",
                f"{state.today_fees_usd:.4f}",
                f"{state.today_profit_usd:.4f}",
                f"{state.doge_accumulated:.2f}",
            ])
    except Exception as e:
        logger.error("Failed to write daily summary: %s", e)

    supabase_store.save_daily_summary(state, pair=state.pair_name)


# ---------------------------------------------------------------------------
# Single-pair market making strategy
# ---------------------------------------------------------------------------
# Two orders on the book at all times: 1 buy + 1 sell.
# Each is either an "entry" (flanking market) or an "exit" (profit target).
# Maximum exposure = 1 order size (~$3.50) instead of the full grid's ~$35.
# ---------------------------------------------------------------------------


def validate_pair_config(current_price: float) -> bool:
    """
    Pre-flight checks for pair mode.
    Returns True if safe to proceed, False if a critical check fails.
    """
    ok = True
    warnings = []

    # Critical: profit target must exceed round-trip fees
    if config.PAIR_PROFIT_PCT <= config.ROUND_TRIP_FEE_PCT:
        msg = (
            "PAIR_PROFIT_PCT (%.2f%%) <= ROUND_TRIP_FEE_PCT (%.2f%%) -- "
            "every trade is a guaranteed loss" % (
                config.PAIR_PROFIT_PCT, config.ROUND_TRIP_FEE_PCT))
        logger.critical(msg)
        warnings.append(msg)
        ok = False

    # Warning: order size below Kraken minimum DOGE volume
    doge_per_order = config.ORDER_SIZE_USD / current_price if current_price > 0 else 0
    if doge_per_order < ORDERMIN_DOGE:
        msg = (
            "ORDER_SIZE_USD $%.2f = %.1f DOGE at $%.6f -- below Kraken min %d DOGE "
            "(will be floored at build time)" % (
                config.ORDER_SIZE_USD, doge_per_order, current_price, ORDERMIN_DOGE))
        logger.warning(msg)
        warnings.append(msg)

    if warnings:
        try:
            import notifier
            notifier.notify_error("Pair validation:\n" + "\n".join(warnings))
        except Exception:
            pass

    if ok:
        logger.info("Pair mode pre-flight validation passed")
    return ok


def build_pair(state: GridState, current_price: float) -> list:
    """
    Build pair orders appropriate to current position state.

    If recent_fills shows an open position (last fill was an entry),
    ensure only the exit order is on the book. Otherwise place fresh
    entries flanking market.

    Returns the list of open GridOrder objects.
    """
    # Pair mode: keep user's order_size_usd but floor at Kraken minimums.
    # No grid-level capital budgeting (only 2 orders, not 40).
    # Use state property (per-pair) instead of mutating global config.
    min_vol = state.min_volume if state.pair_config else ORDERMIN_DOGE
    min_by_volume = min_vol * current_price * 1.2
    state.order_size_usd = max(state.order_size_usd, min_by_volume, COSTMIN_USD)

    state.center_price = current_price

    # If both exits are already on the book, keep them (dual-position state).
    open_orders = [o for o in state.grid_orders if o.status == "open"]
    has_buy_exit = any(o.side == "buy" and o.order_role == "exit" for o in open_orders)
    has_sell_exit = any(o.side == "sell" and o.order_role == "exit" for o in open_orders)
    if has_buy_exit and has_sell_exit:
        logger.info("Pair build: dual exits on book; skipping entry placement")
        state.grid_orders = open_orders
        return open_orders

    # --- Position recovery: if last fill was an entry, we have a position ---
    if state.recent_fills:
        last_fill = state.recent_fills[-1]
        if last_fill.get("profit", 0) == 0 and last_fill.get("side") in ("buy", "sell"):
            return _build_pair_with_position(state, last_fill, current_price)

    # --- No position: place fresh entries flanking market ---
    # Identify any adopted orders (from reconciliation) to skip
    adopted_sides = set()
    for o in state.grid_orders:
        if o.status == "open":
            adopted_sides.add(o.side)

    entry_pct = state.entry_pct
    decimals = state.price_decimals
    buy_price = round(current_price * (1 - entry_pct / 100.0), decimals)
    sell_price = round(current_price * (1 + entry_pct / 100.0), decimals)

    logger.info(
        "Building pair at $%.6f: buy entry $%.6f, sell entry $%.6f",
        current_price, buy_price, sell_price,
    )

    placed = [o for o in state.grid_orders if o.status == "open"]  # Keep adopted

    # Trade A = short-side (sell entry), Trade B = long-side (buy entry)
    side_identity = {"sell": ("A", state.cycle_a), "buy": ("B", state.cycle_b)}

    for side, price, level in [("buy", buy_price, -1), ("sell", sell_price, +1)]:
        if side in adopted_sides:
            logger.debug("  Pair %s: skipped (already adopted)", side.upper())
            continue

        tid, cyc = side_identity[side]
        volume = calculate_volume_for_price(price, state)
        order = GridOrder(level=level, side=side, price=price, volume=volume)
        order.order_role = "entry"
        order.trade_id = tid
        order.cycle = cyc

        try:
            txid = kraken_client.place_order(
                side=side, volume=volume, price=price, pair=state.pair_name)
            order.txid = txid
            order.status = "open"
            order.placed_at = time.time()
            placed.append(order)
            logger.info(
                "  Pair %s entry [%s.%d]: %.2f DOGE @ $%.6f ($%.2f) -> %s",
                side.upper(), tid, cyc, volume, price, volume * price, txid,
            )
        except Exception as e:
            logger.error("Failed to place pair %s entry [%s.%d]: %s",
                         side, tid, cyc, e)
            order.status = "failed"

        if not config.DRY_RUN:
            time.sleep(0.5)

    state.pair_state = "S0"
    state.grid_orders = placed
    return placed


def _build_pair_with_position(state: GridState, last_fill: dict,
                              current_price: float) -> list:
    """
    Called by build_pair when recent_fills shows an open position.
    Ensures exit order is on the book. Keeps opposite-side entry if present.
    Relabels adopted exits that reconciliation misclassified.
    Cancels same-side entries (shouldn't exist).
    """
    fill_side = last_fill["side"]
    fill_price = last_fill["price"]
    decimals = state.price_decimals
    profit_pct = state.profit_pct

    # Determine trade identity from fill side
    # buy fill = Trade B entered, sell fill = Trade A entered
    if fill_side == "buy":
        exit_side = "sell"
        exit_tid, exit_cyc = "B", state.cycle_b
        entry_tid, entry_cyc = "A", state.cycle_a
    else:
        exit_side = "buy"
        exit_tid, exit_cyc = "A", state.cycle_a
        entry_tid, entry_cyc = "B", state.cycle_b
    exit_price = _pair_exit_price(fill_price, current_price, exit_side, state)

    # Look for an adopted order at the exit price (may be mislabeled as entry)
    tol = exit_price * 0.005  # 0.5% tolerance
    exit_found = False
    for o in state.grid_orders:
        if (o.status == "open" and o.side == exit_side
                and abs(o.price - exit_price) < tol):
            if o.order_role != "exit":
                logger.info(
                    "Position recovery: relabeled %s @ $%.6f as exit (was %s)",
                    exit_side.upper(), o.price, o.order_role)
                o.order_role = "exit"
            if fill_side == "buy":
                o.matched_buy_price = fill_price
            else:
                o.matched_sell_price = fill_price
            o.trade_id = exit_tid
            o.cycle = exit_cyc
            exit_found = True
            break

    # Cancel same-side entries (e.g. stale buy entry after buy fill)
    _cancel_open_by_role(state, fill_side, "entry")

    if not exit_found:
        logger.warning(
            "Position recovery: %s entry $%.6f -- placing %s exit [%s.%d] @ $%.6f",
            fill_side.upper(), fill_price, exit_side.upper(),
            exit_tid, exit_cyc, exit_price)
        _place_pair_order(
            state, exit_side, exit_price, "exit",
            matched_buy=fill_price if fill_side == "buy" else None,
            matched_sell=fill_price if fill_side == "sell" else None,
            trade_id=exit_tid, cycle=exit_cyc)
    else:
        logger.info(
            "Position recovery: %s entry $%.6f -- %s exit [%s.%d] @ $%.6f on book",
            fill_side.upper(), fill_price, exit_side.upper(),
            exit_tid, exit_cyc, exit_price)

    # Ensure opposite-side entry exists (e.g. sell entry after buy fill)
    has_opposite_entry = any(
        o.status == "open" and o.side == exit_side and o.order_role == "entry"
        for o in state.grid_orders)
    if not has_opposite_entry:
        entry_pct = state.entry_pct
        if exit_side == "sell":
            entry_price = round(
                current_price * (1 + entry_pct / 100.0), decimals)
        else:
            entry_price = round(
                current_price * (1 - entry_pct / 100.0), decimals)
        logger.info(
            "Position recovery: placing %s entry [%s.%d] @ $%.6f",
            exit_side.upper(), entry_tid, entry_cyc, entry_price)
        _place_pair_order(state, exit_side, entry_price, "entry",
                          trade_id=entry_tid, cycle=entry_cyc)

    # Set pair state based on what's on the book
    state.pair_state = _compute_pair_state(state)

    return [o for o in state.grid_orders if o.status == "open"]


def _place_pair_order(state, side, price, role, matched_buy=None,
                      matched_sell=None, trade_id=None, cycle=None):
    """
    Place a single pair order and add it to state.grid_orders.
    Returns the GridOrder on success, None on failure.

    trade_id: "A" or "B" -- inherited from caller context.
    cycle:    int -- current cycle number for this trade.
    """
    volume = calculate_volume_for_price(price, state)
    level = -1 if side == "buy" else +1
    order = GridOrder(level=level, side=side, price=price, volume=volume)
    order.order_role = role
    if matched_buy is not None:
        order.matched_buy_price = matched_buy
    if matched_sell is not None:
        order.matched_sell_price = matched_sell
    # Assign trade identity
    if trade_id is not None:
        order.trade_id = trade_id
    if cycle is not None:
        order.cycle = cycle
    try:
        txid = kraken_client.place_order(
            side=side, volume=volume, price=price, pair=state.pair_name)
        order.txid = txid
        order.status = "open"
        order.placed_at = time.time()
        state.grid_orders.append(order)
        if role == "entry":
            state.total_entries_placed += 1
        logger.info(
            "  Pair %s %s [%s.%d]: %.2f DOGE @ $%.6f -> %s",
            side.upper(), role, trade_id or "?", cycle or 0,
            volume, price, txid,
        )
        return order
    except Exception as e:
        logger.error("Failed to place pair %s %s [%s.%d]: %s",
                     side, role, trade_id or "?", cycle or 0, e)
        order.status = "failed"
        state.consecutive_errors += 1
        return None


def _close_orphaned_exit(state, filled_entry):
    """
    Race condition: both entries filled before the bot could cancel one.
    The second entry implicitly closes the position opened by the first.

    Example: buy entry fills, places sell exit. Before bot cancels the sell
    entry, it also fills. Now there's an orphaned sell exit for a long
    position that was effectively closed by the sell entry fill.

    This function finds the orphan, books the implicit round trip PnL,
    marks the original entry closed out, logs the trade, and cancels
    the now-unnecessary exit order.

    The orphaned exit is always on the SAME side as the entry that just
    filled (sell entry -> orphaned sell exit, buy entry -> orphaned buy exit).
    """
    # Look for an orphaned exit on the same side as this entry
    orphan = None
    for o in state.grid_orders:
        if (o.status == "open" and o.side == filled_entry.side
                and o.order_role == "exit"):
            orphan = o
            break

    if orphan is None:
        return  # No race condition -- normal case

    # Determine cost basis from the orphaned exit's pairing info
    if filled_entry.side == "sell":
        # Sell entry closed a long. Orphan (sell exit) has matched_buy_price.
        cost_basis = orphan.matched_buy_price
        original_entry_side = "buy"
    else:
        # Buy entry closed a short. Orphan (buy exit) has matched_sell_price.
        cost_basis = orphan.matched_sell_price
        original_entry_side = "sell"

    if cost_basis is None:
        logger.warning(
            "Orphaned %s exit @ $%.6f has no cost basis -- cancelling unbooked",
            filled_entry.side.upper(), orphan.price,
        )
        _cancel_open_by_role(state, filled_entry.side, "exit")
        return

    # Find original entry fill volume from recent_fills
    close_vol = None
    for rf in reversed(state.recent_fills):
        if (rf["side"] == original_entry_side
                and rf.get("profit", 0) == 0
                and abs(rf["price"] - cost_basis) < 1e-6):
            close_vol = rf["volume"]
            break
    if close_vol is None:
        close_vol = min(filled_entry.volume, orphan.volume)

    # Compute PnL (same formula as normal round trip)
    if filled_entry.side == "sell":
        # Long closed: bought at cost_basis, sold at fill price
        buy_p, sell_p = cost_basis, filled_entry.price
    else:
        # Short closed: sold at cost_basis, bought at fill price
        buy_p, sell_p = filled_entry.price, cost_basis

    gross = (sell_p - buy_p) * close_vol
    fees = (buy_p * close_vol + sell_p * close_vol) * config.MAKER_FEE_PCT / 100.0
    net_profit = gross - fees

    # Book the round trip
    state.total_profit_usd += net_profit
    state.today_profit_usd += net_profit
    state.total_round_trips += 1
    state.round_trips_today += 1
    # Don't add to fee totals -- both entry fills already tracked their leg fees
    if net_profit < 0:
        state.today_loss_usd += abs(net_profit)

    # Mark the original entry as closed out
    for o2 in state.grid_orders:
        if (o2.side == original_entry_side and o2.status == "filled"
                and not o2.closed_out
                and abs(o2.price - cost_basis) < 1e-9):
            o2.closed_out = True
            break

    logger.warning(
        "RACE CLOSE: %s entry $%.6f implicitly closed %s "
        "(cost $%.6f, %.2f DOGE) -> net $%.4f "
        "(intended exit $%.6f cancelled)",
        filled_entry.side.upper(), filled_entry.price,
        "long" if filled_entry.side == "sell" else "short",
        cost_basis, close_vol, net_profit, orphan.price,
    )

    # Log trade and fill record for the implicit close
    _log_trade(filled_entry, net_profit, fees)
    state.recent_fills.append({
        "time": time.time(),
        "side": filled_entry.side,
        "price": filled_entry.price,
        "volume": close_vol,
        "profit": net_profit,
        "fees": fees,
        "trade_id": filled_entry.trade_id,
        "cycle": filled_entry.cycle,
        "order_role": "exit",
    })
    supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name)

    # Cancel the orphaned exit order
    _cancel_open_by_role(state, filled_entry.side, "exit")


def _cancel_open_by_role(state, side, role):
    """Cancel all open orders matching side+role. Returns count cancelled."""
    cancelled = 0
    for o in state.grid_orders:
        if o.status == "open" and o.side == side and o.order_role == role:
            if config.DRY_RUN:
                o.status = "cancelled"
            else:
                ok = kraken_client.cancel_order(o.txid)
                if ok:
                    o.status = "cancelled"
                else:
                    logger.warning("Failed to cancel %s %s %s", side, role, o.txid)
                    continue
            cancelled += 1
            logger.info(
                "  Cancelled stale %s %s @ $%.6f (%s)",
                side.upper(), role, o.price, o.txid,
            )
    return cancelled


def _pair_exit_price(entry_fill: float, current_price: float,
                     exit_side: str, state: GridState) -> float:
    """
    Compute the exit price for a pair trade using the spec formula:
      Sell exit (Trade B): max(entry × (1 + π), market × (1 + ε))
      Buy exit  (Trade A): min(entry × (1 - π), market × (1 - ε))

    The min/max ensures the exit is never placed inside the current spread.
    """
    profit_pct = state.profit_pct / 100.0
    entry_pct = state.entry_pct / 100.0
    decimals = state.price_decimals

    if exit_side == "sell":
        # Trade B exit: sell high
        from_entry = entry_fill * (1 + profit_pct)
        from_market = current_price * (1 + entry_pct)
        return round(max(from_entry, from_market), decimals)
    else:
        # Trade A exit: buy low
        from_entry = entry_fill * (1 - profit_pct)
        from_market = current_price * (1 - entry_pct)
        return round(min(from_entry, from_market), decimals)


def compute_unrealized_pnl(state: GridState, current_price: float) -> dict:
    """
    Compute mark-to-market unrealized P&L for open exit orders.

    For each open exit order, calculate what profit would be if closed
    at the current market price instead of the limit exit price:
      Trade A (buy exit): unrealized = (matched_sell_price - current_price) * volume
      Trade B (sell exit): unrealized = (current_price - matched_buy_price) * volume

    Returns dict: {a_unrealized, b_unrealized, total_unrealized}
    """
    a_unreal = 0.0
    b_unreal = 0.0

    for o in state.grid_orders:
        if o.status != "open" or o.order_role != "exit":
            continue

        if o.side == "buy" and o.matched_sell_price is not None:
            # Trade A exit (buy-back): we sold at matched_sell_price
            a_unreal += (o.matched_sell_price - current_price) * o.volume
        elif o.side == "sell" and o.matched_buy_price is not None:
            # Trade B exit (sell): we bought at matched_buy_price
            b_unreal += (current_price - o.matched_buy_price) * o.volume

    return {
        "a_unrealized": round(a_unreal, 6),
        "b_unrealized": round(b_unreal, 6),
        "total_unrealized": round(a_unreal + b_unreal, 6),
    }


def replace_entries_at_distance(state: GridState, current_price: float):
    """User-initiated entry replacement at new entry_pct. Bypasses anti-chase."""
    entry_pct = state.entry_pct
    decimals = state.price_decimals
    replaced = 0
    for o in list(state.grid_orders):
        if o.status != "open" or o.order_role != "entry":
            continue
        tid, cyc = o.trade_id, o.cycle
        if config.DRY_RUN:
            o.status = "cancelled"
        else:
            ok = kraken_client.cancel_order(o.txid)
            if not ok:
                logger.warning("Failed to cancel entry %s for replacement", o.txid)
                continue
            o.status = "cancelled"
        new_price = round(current_price * (1 - entry_pct / 100.0) if o.side == "buy"
                          else current_price * (1 + entry_pct / 100.0), decimals)
        _place_pair_order(state, o.side, new_price, "entry", trade_id=tid, cycle=cyc)
        replaced += 1
    if replaced:
        state.center_price = current_price
        logger.info("User entry_pct change: replaced %d entries at %.2f%% from $%.6f",
                    replaced, entry_pct, current_price)


MAX_COMPLETED_CYCLES = 200  # Keep most recent N completed cycles


def _trim_completed_cycles(state: GridState):
    """Trim completed_cycles list to most recent MAX_COMPLETED_CYCLES."""
    if len(state.completed_cycles) > MAX_COMPLETED_CYCLES:
        state.completed_cycles = state.completed_cycles[-MAX_COMPLETED_CYCLES:]


def _compute_pair_state(state: GridState) -> str:
    """Derive S0/S1a/S1b/S2 from open orders on the book."""
    open_orders = [o for o in state.grid_orders if o.status == "open"]
    has_a_exit = any(o.side == "buy" and o.order_role == "exit" for o in open_orders)
    has_b_exit = any(o.side == "sell" and o.order_role == "exit" for o in open_orders)
    if has_a_exit and has_b_exit:
        return "S2"
    if has_a_exit:
        return "S1a"
    if has_b_exit:
        return "S1b"
    return "S0"


def handle_pair_fill(state: GridState, filled_orders: list,
                     current_price: float) -> list:
    """
    Core pair (A/B) state machine. Entry fills -> place exit for that side.
    Exit fills -> book profit and place a new entry for that same side.
    Opposite-side entries are independent and should not be cancelled.

    Returns list of new GridOrder objects placed.
    """
    new_orders = []
    decimals = state.price_decimals
    profit_pct = state.profit_pct
    entry_pct = state.entry_pct

    old_state = state.pair_state

    for filled in filled_orders:
        is_entry = filled.order_role == "entry"
        is_exit = filled.order_role == "exit"
        tid = getattr(filled, "trade_id", None)
        cyc = getattr(filled, "cycle", 0)

        # ---------------------------------------------------------------
        # BUY ENTRY fills -> place sell exit, keep sell entry on book
        # Trade B entry completes -> transition toward S1b or S2
        # ---------------------------------------------------------------
        if filled.side == "buy" and is_entry:
            tid = tid or "B"
            state.total_entries_filled += 1
            logger.info(
                "PAIR [%s.%d]: Buy entry filled @ $%.6f (%.2f DOGE)",
                tid, cyc, filled.price, filled.volume,
            )

            # Track buy fill fee
            buy_fee = filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0
            state.total_fees_usd += buy_fee
            state.today_fees_usd += buy_fee
            state.recent_fills.append({
                "time": time.time(), "side": "buy",
                "price": filled.price, "volume": filled.volume,
                "profit": 0, "fees": buy_fee,
                "trade_id": tid, "cycle": cyc, "order_role": "entry",
            })
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                     trade_id=tid, cycle=cyc)

            # Place sell exit at profit target (with market floor)
            exit_price = _pair_exit_price(
                filled.price, current_price, "sell", state)
            o = _place_pair_order(
                state, "sell", exit_price, "exit",
                matched_buy=filled.price,
                trade_id=tid, cycle=cyc)
            if o:
                new_orders.append(o)

        # ---------------------------------------------------------------
        # SELL EXIT fills -> Trade B round trip complete!
        # Book profit, place new B.entry (BUY), keep A order untouched
        # ---------------------------------------------------------------
        elif filled.side == "sell" and is_exit:
            tid = tid or "B"
            buy_price = filled.matched_buy_price
            gross = None
            if buy_price is not None:
                gross = (filled.price - buy_price) * filled.volume
                fees = (buy_price * filled.volume * config.MAKER_FEE_PCT / 100.0 +
                        filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0)
                net_profit = gross - fees
                state.total_round_trips += 1
                state.round_trips_today += 1
            else:
                logger.warning(
                    "  [%s.%d] Sell exit at $%.6f has no matched_buy_price -- booking $0",
                    tid, cyc, filled.price)
                net_profit = 0.0
                fees = filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0

            state.total_profit_usd += net_profit
            state.today_profit_usd += net_profit
            # Only track sell leg fee here (buy leg was tracked when buy entry filled)
            sell_leg_fee = filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0
            state.total_fees_usd += sell_leg_fee
            state.today_fees_usd += sell_leg_fee
            if net_profit < 0:
                state.today_loss_usd += abs(net_profit)

            # Mark matched buy as closed out
            if buy_price is not None:
                for o in state.grid_orders:
                    if (o.side == "buy" and o.status == "filled"
                            and not o.closed_out
                            and abs(o.price - buy_price) < 1e-9):
                        o.closed_out = True
                        break

            logger.info(
                "  PAIR ROUND TRIP [%s.%d]! Sell exit $%.6f (bought $%.6f) "
                "-> profit: $%.4f (fees: $%.4f) | Total: $%.4f (%d trips)",
                tid, cyc, filled.price, buy_price or 0, net_profit, fees,
                state.total_profit_usd, state.total_round_trips,
            )

            _log_trade(filled, net_profit, fees)
            state.recent_fills.append({
                "time": time.time(), "side": "sell",
                "price": filled.price, "volume": filled.volume,
                "profit": net_profit, "fees": fees,
                "trade_id": tid, "cycle": cyc, "order_role": "exit",
                "entry_price": buy_price,
            })
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                     trade_id=tid, cycle=cyc)

            # Record completed cycle
            # Find entry fill time from recent_fills
            entry_t = 0.0
            if buy_price is not None:
                for rf in reversed(state.recent_fills):
                    if rf["side"] == "buy" and abs(rf["price"] - buy_price) < 1e-9:
                        entry_t = rf.get("time", 0.0)
                        break
            state.completed_cycles.append(CompletedCycle(
                trade_id=tid, cycle=cyc, entry_side="buy",
                entry_price=buy_price or 0, exit_price=filled.price,
                volume=filled.volume,
                gross_profit=gross or 0, fees=fees, net_profit=net_profit,
                entry_time=entry_t, exit_time=time.time(),
            ))
            _trim_completed_cycles(state)

            # Increment Trade B cycle
            new_cyc = cyc + 1
            if tid == "B":
                state.cycle_b = new_cyc

            # Round trip complete -- reopen buy entry for this side only
            _cancel_open_by_role(state, "buy", "entry")
            buy_entry_price = round(
                current_price * (1 - entry_pct / 100.0), decimals)
            o = _place_pair_order(state, "buy", buy_entry_price, "entry",
                                  trade_id=tid, cycle=new_cyc)
            if o:
                new_orders.append(o)

        # ---------------------------------------------------------------
        # SELL ENTRY fills -> place buy exit, keep buy entry on book
        # Trade A entry completes -> transition toward S1a or S2
        # ---------------------------------------------------------------
        elif filled.side == "sell" and is_entry:
            tid = tid or "A"
            state.total_entries_filled += 1
            logger.info(
                "PAIR [%s.%d]: Sell entry filled @ $%.6f (%.2f DOGE)",
                tid, cyc, filled.price, filled.volume,
            )

            # Track sell fill fee
            sell_fee = filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0
            state.total_fees_usd += sell_fee
            state.today_fees_usd += sell_fee
            state.recent_fills.append({
                "time": time.time(), "side": "sell",
                "price": filled.price, "volume": filled.volume,
                "profit": 0, "fees": sell_fee,
                "trade_id": tid, "cycle": cyc, "order_role": "entry",
            })
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                     trade_id=tid, cycle=cyc)

            # Place buy exit at profit target (with market floor)
            exit_price = _pair_exit_price(
                filled.price, current_price, "buy", state)
            o = _place_pair_order(
                state, "buy", exit_price, "exit",
                matched_sell=filled.price,
                trade_id=tid, cycle=cyc)
            if o:
                new_orders.append(o)

        # ---------------------------------------------------------------
        # BUY EXIT fills -> Trade A round trip complete!
        # Book profit, place new A.entry (SELL), keep B order untouched
        # ---------------------------------------------------------------
        elif filled.side == "buy" and is_exit:
            tid = tid or "A"
            sell_price = filled.matched_sell_price
            gross = None
            if sell_price is not None:
                gross = (sell_price - filled.price) * filled.volume
                fees = (filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0 +
                        sell_price * filled.volume * config.MAKER_FEE_PCT / 100.0)
                net_profit = gross - fees
                state.total_round_trips += 1
                state.round_trips_today += 1
            else:
                logger.warning(
                    "  [%s.%d] Buy exit at $%.6f has no matched_sell_price -- booking $0",
                    tid, cyc, filled.price)
                net_profit = 0.0
                fees = filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0

            state.total_profit_usd += net_profit
            state.today_profit_usd += net_profit
            # Only track buy leg fee here (sell leg was tracked when sell entry filled)
            buy_leg_fee = filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0
            state.total_fees_usd += buy_leg_fee
            state.today_fees_usd += buy_leg_fee
            if net_profit < 0:
                state.today_loss_usd += abs(net_profit)

            # Mark matched sell as closed out
            if sell_price is not None:
                for o in state.grid_orders:
                    if (o.side == "sell" and o.status == "filled"
                            and not o.closed_out
                            and abs(o.price - sell_price) < 1e-9):
                        o.closed_out = True
                        break

            logger.info(
                "  PAIR ROUND TRIP [%s.%d]! Buy exit $%.6f (sold $%.6f) "
                "-> profit: $%.4f (fees: $%.4f) | Total: $%.4f (%d trips)",
                tid, cyc, filled.price, sell_price or 0, net_profit, fees,
                state.total_profit_usd, state.total_round_trips,
            )

            _log_trade(filled, net_profit, fees)
            state.recent_fills.append({
                "time": time.time(), "side": "buy",
                "price": filled.price, "volume": filled.volume,
                "profit": net_profit, "fees": fees,
                "trade_id": tid, "cycle": cyc, "order_role": "exit",
                "entry_price": sell_price,
            })
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                     trade_id=tid, cycle=cyc)

            # Record completed cycle
            # Find entry fill time from recent_fills
            entry_t = 0.0
            if sell_price is not None:
                for rf in reversed(state.recent_fills):
                    if rf["side"] == "sell" and abs(rf["price"] - sell_price) < 1e-9:
                        entry_t = rf.get("time", 0.0)
                        break
            state.completed_cycles.append(CompletedCycle(
                trade_id=tid, cycle=cyc, entry_side="sell",
                entry_price=sell_price or 0, exit_price=filled.price,
                volume=filled.volume,
                gross_profit=gross or 0, fees=fees, net_profit=net_profit,
                entry_time=entry_t, exit_time=time.time(),
            ))
            _trim_completed_cycles(state)

            # Increment Trade A cycle
            new_cyc = cyc + 1
            if tid == "A":
                state.cycle_a = new_cyc

            # Round trip complete -- reopen sell entry for this side only
            _cancel_open_by_role(state, "sell", "entry")
            sell_entry_price = round(
                current_price * (1 + entry_pct / 100.0), decimals)
            o = _place_pair_order(state, "sell", sell_entry_price, "entry",
                                  trade_id=tid, cycle=new_cyc)
            if o:
                new_orders.append(o)

    # Recompute and log state transitions
    new_state = _compute_pair_state(state)
    if new_state != old_state:
        # Include trigger info from last processed fill
        if filled_orders:
            last = filled_orders[-1]
            trigger_tid = getattr(last, "trade_id", "?")
            trigger_cyc = getattr(last, "cycle", 0)
            logger.info(
                "PAIR STATE: %s -> %s (trigger: %s %s [%s.%d] filled)",
                old_state, new_state,
                last.side, last.order_role, trigger_tid, trigger_cyc,
            )
        else:
            logger.info("PAIR STATE: %s -> %s", old_state, new_state)
    state.pair_state = new_state

    return new_orders


def _refresh_entry_if_stale(state, side, current_price):
    """
    If the entry order on the given side is too far from market, cancel and replace.
    Exit orders are never touched.
    """
    entry_pct = state.entry_pct
    refresh_pct = state.refresh_pct
    decimals = state.price_decimals

    for o in state.grid_orders:
        if o.status != "open" or o.side != side or o.order_role != "entry":
            continue
        distance_pct = abs(o.price - current_price) / current_price * 100.0
        if distance_pct > refresh_pct:
            # Check if this order already filled before we cancel it
            if not config.DRY_RUN:
                try:
                    info = kraken_client.query_orders([o.txid])
                    if info.get(o.txid, {}).get("status") == "closed":
                        logger.info(
                            "  Entry %s @ $%.6f already filled -- skipping "
                            "cancel (txid=%s)", side.upper(), o.price, o.txid,
                        )
                        return False  # Let check_fills_live handle it
                except Exception:
                    pass  # If query fails, proceed with cancel

            # Preserve trade identity from the order being replaced
            stale_tid = getattr(o, "trade_id", None)
            stale_cyc = getattr(o, "cycle", 0)
            logger.info(
                "  Refreshing stale %s entry [%s.%d]: $%.6f is %.2f%% from market $%.6f",
                side.upper(), stale_tid or "?", stale_cyc,
                o.price, distance_pct, current_price,
            )
            if config.DRY_RUN:
                o.status = "cancelled"
            else:
                kraken_client.cancel_order(o.txid)
                o.status = "cancelled"

            if side == "buy":
                new_price = round(
                    current_price * (1 - entry_pct / 100.0), decimals)
            else:
                new_price = round(
                    current_price * (1 + entry_pct / 100.0), decimals)
            _place_pair_order(state, side, new_price, "entry",
                              trade_id=stale_tid, cycle=stale_cyc)
            return True
    return False


def refresh_stale_entries(state: GridState, current_price: float) -> bool:
    """
    Replaces check_grid_drift() for pair mode.
    For each open entry order, check if it has drifted too far from market.
    Exit orders are NEVER refreshed (fixed profit target).

    Anti-chase: tracks consecutive same-direction refreshes per trade.
    After MAX_CONSECUTIVE_REFRESHES in the same direction, enters a
    cooldown period (REFRESH_COOLDOWN_SEC) to avoid chasing trends.

    Returns True if any entries were refreshed (triggers rebuild notification).
    """
    refresh_pct = state.refresh_pct
    entry_pct = state.entry_pct
    decimals = state.price_decimals
    now = time.time()

    refreshed = False
    for o in list(state.grid_orders):
        if o.status != "open" or o.order_role != "entry":
            continue
        distance_pct = abs(o.price - current_price) / current_price * 100.0
        if distance_pct > refresh_pct:
            # Determine which trade this is for anti-chase tracking
            tid = getattr(o, "trade_id", None)
            is_a = (tid == "A")

            # Check anti-chase cooldown
            cooldown_until = state.refresh_cooldown_until_a if is_a else state.refresh_cooldown_until_b
            consec = state.consecutive_refreshes_a if is_a else state.consecutive_refreshes_b
            if now < cooldown_until:
                remaining = int(cooldown_until - now)
                logger.info(
                    "Anti-chase: %s entry [%s] refresh blocked (cooldown, %ds remaining)",
                    o.side.upper(), tid or "?", remaining,
                )
                continue
            # If cooldown just expired and counter is still at/above threshold,
            # reset so the next refresh is allowed (counts as 1, not re-trigger)
            if consec >= MAX_CONSECUTIVE_REFRESHES and cooldown_until > 0:
                consec = 0  # Will become 1 after direction check below
                if is_a:
                    state.consecutive_refreshes_a = 0
                    state.refresh_cooldown_until_a = 0.0
                else:
                    state.consecutive_refreshes_b = 0
                    state.refresh_cooldown_until_b = 0.0

            # Check if this order already filled before we cancel it
            if not config.DRY_RUN:
                try:
                    info = kraken_client.query_orders([o.txid])
                    if info.get(o.txid, {}).get("status") == "closed":
                        logger.info(
                            "Entry %s @ $%.6f already filled -- skipping cancel "
                            "(txid=%s)", o.side.upper(), o.price, o.txid,
                        )
                        continue  # Let check_fills_live handle it
                except Exception:
                    pass  # If query fails, proceed with cancel

            # Determine refresh direction (price moved up or down since entry)
            if o.side == "buy":
                direction = "down" if current_price < o.price else "up"
            else:
                direction = "up" if current_price > o.price else "down"

            # Anti-chase: track consecutive same-direction refreshes
            last_dir = state.last_refresh_direction_a if is_a else state.last_refresh_direction_b
            consec = state.consecutive_refreshes_a if is_a else state.consecutive_refreshes_b

            if direction == last_dir:
                consec += 1
            else:
                consec = 1  # New direction, reset

            # Update tracking
            if is_a:
                state.consecutive_refreshes_a = consec
                state.last_refresh_direction_a = direction
            else:
                state.consecutive_refreshes_b = consec
                state.last_refresh_direction_b = direction

            # Check if chase threshold exceeded
            if consec >= MAX_CONSECUTIVE_REFRESHES:
                if is_a:
                    state.refresh_cooldown_until_a = now + REFRESH_COOLDOWN_SEC
                else:
                    state.refresh_cooldown_until_b = now + REFRESH_COOLDOWN_SEC
                logger.warning(
                    "Anti-chase: %s entry [%s] hit %d consecutive %s refreshes "
                    "-- cooldown %ds",
                    o.side.upper(), tid or "?", consec, direction,
                    REFRESH_COOLDOWN_SEC,
                )
                continue

            # Preserve trade identity from the order being replaced
            stale_tid = tid
            stale_cyc = getattr(o, "cycle", 0)
            logger.info(
                "Pair drift: %s entry [%s.%d] $%.6f is %.2f%% from market $%.6f "
                "(threshold: %.1f%%, chase: %d/%d %s)",
                o.side.upper(), stale_tid or "?", stale_cyc,
                o.price, distance_pct,
                current_price, state.refresh_pct,
                consec, MAX_CONSECUTIVE_REFRESHES, direction,
            )
            if config.DRY_RUN:
                o.status = "cancelled"
            else:
                kraken_client.cancel_order(o.txid)
                o.status = "cancelled"

            if o.side == "buy":
                new_price = round(
                    current_price * (1 - entry_pct / 100.0), decimals)
            else:
                new_price = round(
                    current_price * (1 + entry_pct / 100.0), decimals)
            _place_pair_order(state, o.side, new_price, "entry",
                              trade_id=stale_tid, cycle=stale_cyc)
            refreshed = True

    if refreshed:
        state.center_price = current_price
    return refreshed


def _identify_order_3tier(order_info: dict, state: GridState,
                          saved_by_txid: dict, current_price: float) -> dict:
    """
    3-tier identity resolution for reconciliation.

    Returns dict with keys: trade_id, cycle, order_role,
    matched_buy_price, matched_sell_price, method.

    Tier 1 (saved_txid):  Match against _saved_open_orders by txid.
    Tier 2 (price_match): Match price against recent_fills to detect exits.
    Tier 3 (side_convention): Deterministic fallback from side.
    """
    txid = order_info["txid"]
    side = order_info["side"]
    price = order_info["price"]
    profit_pct = state.profit_pct

    # --- Tier 1: Saved txid match (most reliable) ---
    saved = saved_by_txid.get(txid)
    if saved and saved.get("trade_id"):
        return {
            "trade_id": saved["trade_id"],
            "cycle": saved.get("cycle", 0),
            "order_role": saved.get("order_role", "entry"),
            "matched_buy_price": saved.get("matched_buy_price"),
            "matched_sell_price": saved.get("matched_sell_price"),
            "method": "saved_txid",
        }

    # --- Tier 2: Price matching against recent_fills ---
    # If a buy order's price matches a recent sell fill × (1 - profit_pct/100)
    # within 0.5% tolerance, it's Trade A's exit (buy-back after sell entry).
    # Vice versa for sell orders matching buy fills.
    tol = 0.005  # 0.5% tolerance
    if side == "buy":
        # Could be Trade A exit: buy exit price ≈ sell_entry × (1 - profit_pct/100)
        for rf in reversed(state.recent_fills):
            if rf["side"] == "sell" and rf.get("profit", 0) == 0:
                expected_exit = rf["price"] * (1 - profit_pct / 100.0)
                if expected_exit > 0 and abs(price - expected_exit) / expected_exit < tol:
                    return {
                        "trade_id": "A",
                        "cycle": state.cycle_a,
                        "order_role": "exit",
                        "matched_buy_price": None,
                        "matched_sell_price": rf["price"],
                        "method": "price_match",
                    }
    elif side == "sell":
        # Could be Trade B exit: sell exit price ≈ buy_entry × (1 + profit_pct/100)
        for rf in reversed(state.recent_fills):
            if rf["side"] == "buy" and rf.get("profit", 0) == 0:
                expected_exit = rf["price"] * (1 + profit_pct / 100.0)
                if expected_exit > 0 and abs(price - expected_exit) / expected_exit < tol:
                    return {
                        "trade_id": "B",
                        "cycle": state.cycle_b,
                        "order_role": "exit",
                        "matched_buy_price": rf["price"],
                        "matched_sell_price": None,
                        "method": "price_match",
                    }

    # --- Tier 3: Side convention fallback ---
    # sell -> A entry, buy -> B entry
    if side == "sell":
        return {
            "trade_id": "A",
            "cycle": state.cycle_a,
            "order_role": "entry",
            "matched_buy_price": None,
            "matched_sell_price": None,
            "method": "side_convention",
        }
    else:
        return {
            "trade_id": "B",
            "cycle": state.cycle_b,
            "order_role": "entry",
            "matched_buy_price": None,
            "matched_sell_price": None,
            "method": "side_convention",
        }


def reconcile_pair_on_startup(state: GridState, current_price: float) -> int:
    """
    Simplified reconciliation for pair mode.
    Adopt up to 2 open orders (exit-first, then entry). Cancel extras.
    Uses 3-tier identity resolution (saved_txid > price_match > side_convention).
    """
    if config.DRY_RUN:
        logger.info("[DRY RUN] Skipping pair reconciliation")
        return 0

    try:
        open_orders = kraken_client.get_open_orders()
    except Exception as e:
        logger.error("Pair reconciliation: failed to fetch open orders: %s", e)
        return 0

    if not open_orders:
        logger.info("Pair reconciliation: no open orders found on Kraken")
        return 0

    # Determine pair filter strings
    if state.pair_config:
        filter_strings = state.pair_config.filter_strings
    else:
        filter_strings = ["XDG", "DOGE"]

    # Build saved-state lookup for Tier 1
    saved_by_txid = {}
    for so in getattr(state, "_saved_open_orders", []):
        if so.get("txid"):
            saved_by_txid[so["txid"]] = so

    # Collect orders (without pre-assigning role)
    our_orders = []

    for txid, info in open_orders.items():
        descr = info.get("descr", {})
        pair = descr.get("pair", "")
        if not any(s in pair.upper() for s in filter_strings):
            continue

        side = descr.get("type", "")
        order_price = float(descr.get("price", 0))
        order_vol = float(info.get("vol", 0))

        if not side or order_price <= 0:
            continue

        order_info = {
            "txid": txid,
            "side": side,
            "price": order_price,
            "volume": order_vol,
        }

        # Resolve identity via 3-tier function
        identity = _identify_order_3tier(
            order_info, state, saved_by_txid, current_price)
        order_info["role"] = identity["order_role"]
        order_info["identity"] = identity
        our_orders.append(order_info)

    if not our_orders:
        logger.info("Pair reconciliation: no matching orders for %s", state.pair_display)
        return 0

    def _dist(o):
        return abs(o["price"] - current_price)

    exits = [o for o in our_orders if o["role"] == "exit"]
    entries = [o for o in our_orders if o["role"] == "entry"]
    keep = []

    # Prefer exits: keep up to one buy exit and one sell exit (closest per side)
    for side in ("buy", "sell"):
        candidates = [o for o in exits if o["side"] == side]
        if candidates:
            candidates.sort(key=_dist)
            keep.append(candidates[0])

    # If only one exit, keep an entry on the SAME side if present
    if len(keep) == 1:
        exit_side = keep[0]["side"]
        same_side_entries = [o for o in entries if o["side"] == exit_side]
        if same_side_entries:
            same_side_entries.sort(key=_dist)
            keep.append(same_side_entries[0])

    # If no exits, keep closest entry per side (buy+sell if possible)
    if not keep:
        for side in ("buy", "sell"):
            candidates = [o for o in entries if o["side"] == side]
            if candidates:
                candidates.sort(key=_dist)
                keep.append(candidates[0])
        if not keep and entries:
            entries.sort(key=_dist)
            keep.append(entries[0])

    # Cap to 2 orders total
    keep = keep[:2]

    adopted = 0
    orphans = 0
    keep_txids = {o["txid"] for o in keep}

    for o in keep:
        identity = o["identity"]
        order = GridOrder(level=-1 if o["side"] == "buy" else +1,
                          side=o["side"], price=o["price"], volume=o["volume"])
        order.txid = o["txid"]
        order.status = "open"
        order.placed_at = time.time()
        order.order_role = identity["order_role"]
        order.trade_id = identity["trade_id"]
        order.cycle = identity["cycle"]
        order.matched_buy_price = identity["matched_buy_price"]
        order.matched_sell_price = identity["matched_sell_price"]

        state.grid_orders.append(order)
        adopted += 1
        logger.info(
            "Pair reconcile: adopted %s %s [%s.%d] %.2f DOGE @ $%.6f -> %s (id: %s)",
            o["side"].upper(), order.order_role, order.trade_id, order.cycle,
            o["volume"], o["price"], o["txid"], identity["method"],
        )

    for o in our_orders:
        if o["txid"] in keep_txids:
            continue
        logger.warning(
            "Pair reconcile: cancelling extra %s %s [orphan] @ $%.6f -> %s",
            o["side"].upper(), o["role"], o["price"], o["txid"],
        )
        kraken_client.cancel_order(o["txid"])
        orphans += 1

    logger.info(
        "Pair reconciliation: %d adopted, %d orphans cancelled",
        adopted, orphans,
    )

    # Check trade history for fills that happened while bot was offline
    _reconcile_offline_fills(state, current_price)

    return adopted


def _reconcile_offline_fills(state: GridState, current_price: float):
    """
    Check recent trade history for fills that happened while the bot was
    offline.  If a buy filled but no sell exit exists on the book, place one.
    Same for sell fills with no buy exit.

    This closes the gap where a fill between deploys was invisible because
    startup reconciliation only looks at open orders.
    """
    profit_pct = state.profit_pct
    entry_pct = state.entry_pct
    decimals = state.price_decimals

    try:
        # Look back 6 hours of trade history
        trades = kraken_client.get_trades_history(
            start=time.time() - 21600)
    except Exception as e:
        logger.warning("Offline fill check: trade history failed: %s", e)
        return

    if not trades:
        logger.info("Offline fill check: no recent trades")
        return

    # Determine pair filter strings
    if state.pair_config:
        filter_strings = state.pair_config.filter_strings
    else:
        filter_strings = ["XDG", "DOGE"]

    # Filter to our pair's trades only
    doge_trades = []
    for trade_txid, info in trades.items():
        pair = info.get("pair", "")
        if not any(s in pair.upper() for s in filter_strings):
            continue
        doge_trades.append({
            "txid": trade_txid,
            "ordertxid": info.get("ordertxid", ""),
            "side": info.get("type", ""),
            "price": float(info.get("price", 0)),
            "volume": float(info.get("vol", 0)),
            "time": float(info.get("time", 0)),
        })

    if not doge_trades:
        logger.info("Offline fill check: no XDGUSD trades in last 6h")
        return

    # Sort by time (oldest first)
    doge_trades.sort(key=lambda t: t["time"])
    logger.info(
        "Offline fill check: found %d XDGUSD trades in last 6h",
        len(doge_trades),
    )

    # --- Step 1: Filter out trades already recorded in recent_fills ---
    new_trades = []
    for t in doge_trades:
        already = False
        for rf in state.recent_fills:
            if (rf["side"] == t["side"]
                    and abs(rf["price"] - t["price"]) < 1e-5
                    and abs(rf.get("time", 0) - t["time"]) < 300):
                already = True
                break
        if not already:
            new_trades.append(t)

    if not new_trades:
        logger.info("Offline fill check: all %d trades already processed",
                     len(doge_trades))
        return

    logger.info(
        "Offline fill check: %d new (unprocessed) trades after filtering",
        len(new_trades),
    )

    # Check what's currently on the book
    open_orders = [o for o in state.grid_orders if o.status == "open"]
    has_sell_exit = any(
        o.side == "sell" and o.order_role == "exit" for o in open_orders)
    has_buy_exit = any(
        o.side == "buy" and o.order_role == "exit" for o in open_orders)

    # Find the most recent buy and sell from NEW (unprocessed) trades only
    last_buy = None
    last_sell = None
    for t in new_trades:
        if t["side"] == "buy":
            last_buy = t
        elif t["side"] == "sell":
            last_sell = t

    placed = 0
    tol = profit_pct / 200.0  # half profit pct as tolerance

    # --- Step 2: Dual fill -- classify each as EXIT vs ENTRY ---
    # Kraken trade history has no role info, so we match each fill's price
    # against known positions in recent_fills to determine if it's an exit.
    if last_buy and last_sell:

        # Is last_buy an exit for a sell position in recent_fills?
        buy_is_exit = False
        buy_cost_basis = None
        for rf in reversed(state.recent_fills):
            if rf["side"] == "sell":
                expected = rf["price"] * (1 - profit_pct / 100.0)
                if (expected > 0
                        and abs(last_buy["price"] - expected) / expected < tol):
                    buy_is_exit = True
                    buy_cost_basis = rf["price"]
                    break

        # Is last_sell an exit for a buy position in recent_fills?
        sell_is_exit = False
        sell_cost_basis = None
        for rf in reversed(state.recent_fills):
            if rf["side"] == "buy":
                expected = rf["price"] * (1 + profit_pct / 100.0)
                if (expected > 0
                        and abs(last_sell["price"] - expected) / expected < tol):
                    sell_is_exit = True
                    sell_cost_basis = rf["price"]
                    break

        if buy_is_exit or sell_is_exit:
            # --- At least one fill is an exit for a known position ---
            # Book exit round trip(s)
            if buy_is_exit:
                sell_p = buy_cost_basis
                buy_p = last_buy["price"]
                close_vol = last_buy["volume"]
                gross = (sell_p - buy_p) * close_vol
                fee_buy = buy_p * close_vol * config.MAKER_FEE_PCT / 100.0
                fee_sell = sell_p * close_vol * config.MAKER_FEE_PCT / 100.0
                net_profit = gross - fee_buy - fee_sell
                state.total_profit_usd += net_profit
                state.today_profit_usd += net_profit
                state.total_round_trips += 1
                state.round_trips_today += 1
                # Only track exit leg fee (entry leg tracked previously)
                state.total_fees_usd += fee_buy
                state.today_fees_usd += fee_buy
                if net_profit < 0:
                    state.today_loss_usd += abs(net_profit)
                logger.warning(
                    "OFFLINE EXIT: Buy $%.6f closes short "
                    "(sell entry $%.6f, %.2f DOGE) -> net $%.4f",
                    buy_p, sell_p, close_vol, net_profit,
                )
                state.recent_fills.append({
                    "time": last_buy["time"], "side": "buy",
                    "price": buy_p, "volume": close_vol,
                    "profit": net_profit, "fees": fee_buy,
                    "trade_id": "A", "cycle": state.cycle_a, "order_role": "exit",
                })
                supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                         trade_id="A", cycle=state.cycle_a)
                # Record completed cycle (Trade A: sell entry -> buy exit)
                state.completed_cycles.append(CompletedCycle(
                    trade_id="A", cycle=state.cycle_a, entry_side="sell",
                    entry_price=sell_p, exit_price=buy_p,
                    volume=close_vol,
                    gross_profit=gross, fees=fee_buy + fee_sell,
                    net_profit=net_profit,
                    entry_time=0, exit_time=last_buy["time"],
                ))
                _trim_completed_cycles(state)
                # Reopen sell entry (Trade A) for this side
                _cancel_open_by_role(state, "sell", "entry")
                sell_entry = round(
                    current_price * (1 + entry_pct / 100.0), decimals)
                o = _place_pair_order(state, "sell", sell_entry, "entry",
                                      trade_id="A", cycle=state.cycle_a)
                if o:
                    placed += 1

            if sell_is_exit:
                buy_p = sell_cost_basis
                sell_p = last_sell["price"]
                close_vol = last_sell["volume"]
                gross = (sell_p - buy_p) * close_vol
                fee_buy = buy_p * close_vol * config.MAKER_FEE_PCT / 100.0
                fee_sell = sell_p * close_vol * config.MAKER_FEE_PCT / 100.0
                net_profit = gross - fee_buy - fee_sell
                state.total_profit_usd += net_profit
                state.today_profit_usd += net_profit
                state.total_round_trips += 1
                state.round_trips_today += 1
                state.total_fees_usd += fee_sell
                state.today_fees_usd += fee_sell
                if net_profit < 0:
                    state.today_loss_usd += abs(net_profit)
                logger.warning(
                    "OFFLINE EXIT: Sell $%.6f closes long "
                    "(buy entry $%.6f, %.2f DOGE) -> net $%.4f",
                    sell_p, buy_p, close_vol, net_profit,
                )
                state.recent_fills.append({
                    "time": last_sell["time"], "side": "sell",
                    "price": sell_p, "volume": close_vol,
                    "profit": net_profit, "fees": fee_sell,
                    "trade_id": "B", "cycle": state.cycle_b, "order_role": "exit",
                })
                supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                         trade_id="B", cycle=state.cycle_b)
                # Record completed cycle (Trade B: buy entry -> sell exit)
                state.completed_cycles.append(CompletedCycle(
                    trade_id="B", cycle=state.cycle_b, entry_side="buy",
                    entry_price=buy_p, exit_price=sell_p,
                    volume=close_vol,
                    gross_profit=gross, fees=fee_buy + fee_sell,
                    net_profit=net_profit,
                    entry_time=0, exit_time=last_sell["time"],
                ))
                _trim_completed_cycles(state)
                # Reopen buy entry (Trade B) for this side
                _cancel_open_by_role(state, "buy", "entry")
                buy_entry = round(
                    current_price * (1 - entry_pct / 100.0), decimals)
                o = _place_pair_order(state, "buy", buy_entry, "entry",
                                      trade_id="B", cycle=state.cycle_b)
                if o:
                    placed += 1

            # Handle remaining entry fill(s) -- place exit only
            if buy_is_exit and not sell_is_exit:
                # Sell is a new entry -> place buy exit only
                sp = last_sell["price"]
                exit_price = _pair_exit_price(sp, current_price, "buy", state)
                if not has_buy_exit:
                    o = _place_pair_order(
                        state, "buy", exit_price, "exit", matched_sell=sp,
                        trade_id="A", cycle=state.cycle_a)
                    if o:
                        placed += 1
                sell_fee = sp * last_sell["volume"] * config.MAKER_FEE_PCT / 100.0
                state.total_fees_usd += sell_fee
                state.today_fees_usd += sell_fee
                state.recent_fills.append({
                    "time": last_sell["time"], "side": "sell",
                    "price": sp, "volume": last_sell["volume"],
                    "profit": 0, "fees": sell_fee,
                    "trade_id": "A", "cycle": state.cycle_a, "order_role": "entry",
                })
                supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                         trade_id="A", cycle=state.cycle_a)
                logger.info(
                    "Offline exit+entry: buy exit booked, "
                    "placed %d exit order for sell entry position", placed)

            elif sell_is_exit and not buy_is_exit:
                # Buy is a new entry -> place sell exit only
                bp = last_buy["price"]
                exit_price = _pair_exit_price(bp, current_price, "sell", state)
                if not has_sell_exit:
                    o = _place_pair_order(
                        state, "sell", exit_price, "exit", matched_buy=bp,
                        trade_id="B", cycle=state.cycle_b)
                    if o:
                        placed += 1
                buy_fee = bp * last_buy["volume"] * config.MAKER_FEE_PCT / 100.0
                state.total_fees_usd += buy_fee
                state.today_fees_usd += buy_fee
                state.recent_fills.append({
                    "time": last_buy["time"], "side": "buy",
                    "price": bp, "volume": last_buy["volume"],
                    "profit": 0, "fees": buy_fee,
                    "trade_id": "B", "cycle": state.cycle_b, "order_role": "entry",
                })
                supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                         trade_id="B", cycle=state.cycle_b)
                logger.info(
                    "Offline exit+entry: sell exit booked, "
                    "placed %d exit order for buy entry position", placed)

            else:
                logger.info(
                    "Offline double exit: booked both, reopened entries (placed=%d)",
                    placed)

            return

        # --- Neither fill matches a known position exit ---
        # Check if the two fills form a round trip between each other
        # (entry + its exit) or a race condition (both entries).
        if last_buy["time"] <= last_sell["time"]:
            first, second = last_buy, last_sell
        else:
            first, second = last_sell, last_buy

        if first["side"] == "buy":
            expected_exit = first["price"] * (1 + profit_pct / 100.0)
        else:
            expected_exit = first["price"] * (1 - profit_pct / 100.0)

        is_round_trip = (
            expected_exit > 0
            and abs(second["price"] - expected_exit) / expected_exit < tol
        )

        if is_round_trip:
            # PnL computation (entry + its exit)
            if first["side"] == "buy":
                buy_p, sell_p = first["price"], second["price"]
            else:
                buy_p, sell_p = second["price"], first["price"]

            close_vol = first["volume"]
            gross = (sell_p - buy_p) * close_vol
            fee_buy = buy_p * close_vol * config.MAKER_FEE_PCT / 100.0
            fee_sell = sell_p * close_vol * config.MAKER_FEE_PCT / 100.0
            net_profit = gross - fee_buy - fee_sell

            # Book the round trip
            state.total_profit_usd += net_profit
            state.today_profit_usd += net_profit
            state.total_round_trips += 1
            state.round_trips_today += 1
            state.total_fees_usd += fee_buy + fee_sell
            state.today_fees_usd += fee_buy + fee_sell
            if net_profit < 0:
                state.today_loss_usd += abs(net_profit)

            # Record both fills
            entry_side = first["side"]
            rt_tid = "B" if entry_side == "buy" else "A"
            rt_cyc = state.cycle_b if rt_tid == "B" else state.cycle_a
            state.recent_fills.append({
                "time": first["time"], "side": first["side"],
                "price": first["price"], "volume": first["volume"],
                "profit": 0,
                "fees": fee_buy if first["side"] == "buy" else fee_sell,
                "trade_id": rt_tid, "cycle": rt_cyc, "order_role": "entry",
            })
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                     trade_id=rt_tid, cycle=rt_cyc)
            state.recent_fills.append({
                "time": second["time"], "side": second["side"],
                "price": second["price"], "volume": close_vol,
                "profit": net_profit,
                "fees": fee_sell if second["side"] == "sell" else fee_buy,
                "trade_id": rt_tid, "cycle": rt_cyc, "order_role": "exit",
            })
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                     trade_id=rt_tid, cycle=rt_cyc)

            # Record completed cycle
            state.completed_cycles.append(CompletedCycle(
                trade_id=rt_tid, cycle=rt_cyc, entry_side=entry_side,
                entry_price=first["price"], exit_price=second["price"],
                volume=close_vol,
                gross_profit=gross, fees=fee_buy + fee_sell,
                net_profit=net_profit,
                entry_time=first["time"], exit_time=second["time"],
            ))
            _trim_completed_cycles(state)

            logger.warning(
                "OFFLINE ROUND TRIP: %s entry $%.6f -> %s exit $%.6f "
                "(%.2f DOGE) -> net $%.4f",
                first["side"].upper(), first["price"],
                second["side"].upper(), second["price"],
                close_vol, net_profit,
            )

            # Reopen entry for the completed side only
            if first["side"] == "buy":
                _cancel_open_by_role(state, "buy", "entry")
                buy_entry = round(
                    current_price * (1 - entry_pct / 100.0), decimals)
                o = _place_pair_order(state, "buy", buy_entry, "entry",
                                      trade_id="B", cycle=state.cycle_b)
                if o:
                    placed += 1
            else:
                _cancel_open_by_role(state, "sell", "entry")
                sell_entry = round(
                    current_price * (1 + entry_pct / 100.0), decimals)
                o = _place_pair_order(state, "sell", sell_entry, "entry",
                                      trade_id="A", cycle=state.cycle_a)
                if o:
                    placed += 1
        else:
            # Dual entries: keep both trades, place exits, no PnL booked
            for t in (first, second):
                fee = t["price"] * t["volume"] * config.MAKER_FEE_PCT / 100.0
                state.total_fees_usd += fee
                state.today_fees_usd += fee
                t_tid = "B" if t["side"] == "buy" else "A"
                t_cyc = state.cycle_b if t_tid == "B" else state.cycle_a
                state.recent_fills.append({
                    "time": t["time"], "side": t["side"],
                    "price": t["price"], "volume": t["volume"],
                    "profit": 0, "fees": fee,
                    "trade_id": t_tid, "cycle": t_cyc, "order_role": "entry",
                })
                supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                         trade_id=t_tid, cycle=t_cyc)

            # Cancel stale entries (both sides filled)
            _cancel_open_by_role(state, "buy", "entry")
            _cancel_open_by_role(state, "sell", "entry")

            logger.warning(
                "OFFLINE DUAL ENTRY: %s entry $%.6f + %s entry $%.6f "
                "(%.2f DOGE) -- placing exits (no implicit close)",
                first["side"].upper(), first["price"],
                second["side"].upper(), second["price"],
                first["volume"],
            )

            if first["side"] == "buy" and not has_sell_exit:
                exit_price = _pair_exit_price(first["price"], current_price, "sell", state)
                o = _place_pair_order(
                    state, "sell", exit_price, "exit", matched_buy=first["price"],
                    trade_id="B", cycle=state.cycle_b)
                if o:
                    placed += 1
            if first["side"] == "sell" and not has_buy_exit:
                exit_price = _pair_exit_price(first["price"], current_price, "buy", state)
                o = _place_pair_order(
                    state, "buy", exit_price, "exit", matched_sell=first["price"],
                    trade_id="A", cycle=state.cycle_a)
                if o:
                    placed += 1

            if second["side"] == "buy" and not has_sell_exit:
                exit_price = _pair_exit_price(second["price"], current_price, "sell", state)
                o = _place_pair_order(
                    state, "sell", exit_price, "exit", matched_buy=second["price"],
                    trade_id="B", cycle=state.cycle_b)
                if o:
                    placed += 1
            if second["side"] == "sell" and not has_buy_exit:
                exit_price = _pair_exit_price(second["price"], current_price, "buy", state)
                o = _place_pair_order(
                    state, "buy", exit_price, "exit", matched_sell=second["price"],
                    trade_id="A", cycle=state.cycle_a)
                if o:
                    placed += 1

        return  # Dual-fill handled

    # --- Single offline fill (only buy OR only sell) ---

    # If there's a recent buy fill but no sell exit on the book, place one
    if last_buy and not has_sell_exit:
        buy_price = last_buy["price"]
        exit_price = _pair_exit_price(buy_price, current_price, "sell", state)
        logger.warning(
            "OFFLINE FILL RECOVERY: Buy filled @ $%.6f (%.2f DOGE) at %s "
            "-- placing sell exit @ $%.6f",
            buy_price, last_buy["volume"],
            datetime.fromtimestamp(last_buy["time"], timezone.utc)
                .strftime("%Y-%m-%d %H:%M UTC"),
            exit_price,
        )
        _cancel_open_by_role(state, "sell", "entry")
        o = _place_pair_order(
            state, "sell", exit_price, "exit", matched_buy=buy_price,
            trade_id="B", cycle=state.cycle_b)
        if o:
            placed += 1
            buy_fee = buy_price * last_buy["volume"] * config.MAKER_FEE_PCT / 100.0
            state.total_fees_usd += buy_fee
            state.today_fees_usd += buy_fee
            state.recent_fills.append({
                "time": last_buy["time"], "side": "buy",
                "price": buy_price, "volume": last_buy["volume"],
                "profit": 0, "fees": buy_fee,
                "trade_id": "B", "cycle": state.cycle_b, "order_role": "entry",
            })
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                     trade_id="B", cycle=state.cycle_b)

    # If there's a recent sell fill but no buy exit on the book, place one
    if last_sell and not has_buy_exit:
        sell_price = last_sell["price"]
        exit_price = _pair_exit_price(sell_price, current_price, "buy", state)
        logger.warning(
            "OFFLINE FILL RECOVERY: Sell filled @ $%.6f (%.2f DOGE) at %s "
            "-- placing buy exit @ $%.6f",
            sell_price, last_sell["volume"],
            datetime.fromtimestamp(last_sell["time"], timezone.utc)
                .strftime("%Y-%m-%d %H:%M UTC"),
            exit_price,
        )
        _cancel_open_by_role(state, "buy", "entry")
        o = _place_pair_order(
            state, "buy", exit_price, "exit", matched_sell=sell_price,
            trade_id="A", cycle=state.cycle_a)
        if o:
            placed += 1
            sell_fee = sell_price * last_sell["volume"] * config.MAKER_FEE_PCT / 100.0
            state.total_fees_usd += sell_fee
            state.today_fees_usd += sell_fee
            state.recent_fills.append({
                "time": last_sell["time"], "side": "sell",
                "price": sell_price, "volume": last_sell["volume"],
                "profit": 0, "fees": sell_fee,
                "trade_id": "A", "cycle": state.cycle_a, "order_role": "entry",
            })
            supabase_store.save_fill(state.recent_fills[-1], pair=state.pair_name,
                                     trade_id="A", cycle=state.cycle_a)

    if placed:
        logger.info("Offline fill recovery: placed %d orders", placed)
    else:
        logger.info("Offline fill check: no unhandled fills found")


def get_position_state(state: GridState) -> str:
    """
    Determine the current position state for pair mode.
    Returns the formal pair state (S0/S1a/S1b/S2) and a human-readable
    position label: "long", "short", "both", or "flat".
    """
    # Recompute from open orders (authoritative)
    state.pair_state = _compute_pair_state(state)
    ps = state.pair_state
    if ps == "S0":
        return "flat"
    if ps == "S1a":
        return "short"   # Trade A has entered (sold DOGE), waiting for buy-back
    if ps == "S1b":
        return "long"    # Trade B has entered (bought DOGE), waiting for sell
    if ps == "S2":
        return "both"    # Both trades have entered, waiting for both exits
    return "flat"


def get_status_summary(state: GridState, current_price: float) -> str:
    """
    Generate a human-readable status summary.
    Used for health checks and Telegram status messages.
    """
    open_orders = [o for o in state.grid_orders if o.status == "open"]
    open_buys = len([o for o in open_orders if o.side == "buy"])
    open_sells = len([o for o in open_orders if o.side == "sell"])

    prefix = "[DRY RUN] " if config.DRY_RUN else ""

    if config.STRATEGY_MODE == "pair":
        # Show roles and trade identity for pair mode
        entry_orders = [o for o in open_orders if o.order_role == "entry"]
        exit_orders = [o for o in open_orders if o.order_role == "exit"]
        order_details = ", ".join(
            f"{o.trade_id or '?'}.{o.cycle}={o.side} {o.order_role}"
            for o in open_orders
        )
        # Per-trade cycle stats from completed_cycles
        a_cycles = [c for c in state.completed_cycles if c.trade_id == "A"]
        b_cycles = [c for c in state.completed_cycles if c.trade_id == "B"]
        a_net = sum(c.net_profit for c in a_cycles)
        b_net = sum(c.net_profit for c in b_cycles)

        lines = [
            f"{prefix}{state.pair_display} Pair Bot Status",
            f"Price: ${current_price:.6f}",
            f"State: {state.pair_state} | A.cycle={state.cycle_a} B.cycle={state.cycle_b}",
            f"Open: {order_details or 'none'}",
            f"Entry dist: {state.entry_pct:.2f}% | Profit tgt: {state.profit_pct:.2f}%",
            f"Today: {state.round_trips_today} round trips, ${state.today_profit_usd:.4f} profit",
            f"Lifetime: {state.total_round_trips} round trips, ${state.total_profit_usd:.4f} profit",
            f"  Trade A: {len(a_cycles)} cycles, ${a_net:.4f} net",
            f"  Trade B: {len(b_cycles)} cycles, ${b_net:.4f} net",
            f"Fees paid: ${state.total_fees_usd:.4f}",
            f"DOGE accumulated: {state.doge_accumulated:.2f}",
        ]
        # Add unrealized P&L
        upnl = compute_unrealized_pnl(state, current_price)
        if upnl["total_unrealized"] != 0:
            lines.append(
                f"Unrealized: ${upnl['total_unrealized']:.4f} "
                f"(A: ${upnl['a_unrealized']:.4f}, B: ${upnl['b_unrealized']:.4f})"
            )
    else:
        # Trend ratio display
        ratio = state.trend_ratio
        total = config.GRID_LEVELS * 2
        n_buys = max(2, min(total - 2, round(total * ratio)))
        n_sells = total - n_buys
        ratio_src = "manual" if state.trend_ratio_override is not None else "auto"

        lines = [
            f"{prefix}{state.pair_display} Grid Bot Status",
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
