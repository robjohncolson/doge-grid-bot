"""
Executable state machine model for the pair trading system.

Pure Python, zero external dependencies. Models the S0/S1a/S1b/S2 pair state
machine from grid_strategy.py as frozen dataclasses + pure transition functions.

    python pair_model.py          # runs all scenarios + 10K random walk
    python pair_model.py predict  # loads logs/state.json + prints predictions

Every function corresponds to a production function in grid_strategy.py.
See the mapping table at the bottom of this docstring:

    derive_phase        -> _compute_pair_state
    _exit_price         -> _pair_exit_price
    _repriced_exit      -> _repriced_exit
    _compute_thresholds -> compute_exit_thresholds
    _entry_distances    -> compute_entry_distances
    _handle_buy_fill    -> handle_pair_fill (buy cases)
    _handle_sell_fill   -> handle_pair_fill (sell cases)
    _check_stale_exits  -> check_stale_exits
    _check_s2_break     -> check_s2_break_glass
    check_invariants    -> STATE_MACHINE.md invariants
"""

from __future__ import annotations
from dataclasses import dataclass, replace, field
from enum import Enum
from typing import Union
import json
import os
import random
import sys

# ──────────────────────────────────────────────────────────────────────
# 1. Config
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelConfig:
    """Immutable parameter set mirroring PairConfig + global constants."""
    entry_pct: float = 0.2           # Distance from market for entries (%)
    profit_pct: float = 1.0          # Profit target from entry (%)
    refresh_pct: float = 1.0         # Max drift before entry refresh (%)
    order_size_usd: float = 3.5      # Dollar value per order
    price_decimals: int = 6          # Decimal places for price rounding
    volume_decimals: int = 0         # Decimal places for volume rounding
    min_volume: float = 13.0         # Kraken minimum order volume
    maker_fee_pct: float = 0.25      # Maker fee per side (%)
    max_recovery_slots: int = 2      # Max orphaned exits per pair
    exit_reprice_mult: float = 1.5   # Reprice after median * this
    exit_orphan_mult: float = 5.0    # Orphan after median * this
    s2_max_spread_pct: float = 3.0   # Max exit spread before S2 break-glass
    reprice_cooldown_sec: float = 120.0
    min_cycles_for_timing: int = 5   # Min completed cycles for timing logic
    directional_asymmetry: float = 0.5
    recovery_fallback_sec: float = 7200.0  # Fallback orphan timeout
    s2_fallback_sec: float = 600.0   # Fallback S2 timeout
    max_consecutive_refreshes: int = 3
    refresh_cooldown_sec: float = 300.0
    fee_margin: float = 0.003        # 0.3% breakeven margin for repricing
    next_entry_multiplier: float = 1.0
    # Entry backoff after consecutive losses
    entry_backoff_enabled: bool = True
    entry_backoff_factor: float = 0.5
    entry_backoff_max_multiplier: float = 5.0
    # S2 break-glass hardening
    s2_cooldown_sec: float = 300.0    # Cooldown after break-glass fires
    price_staleness_limit: float = 90.0  # Max seconds before price considered stale


def default_config(**overrides) -> ModelConfig:
    return ModelConfig(**overrides)


# ──────────────────────────────────────────────────────────────────────
# 2. State data structures
# ──────────────────────────────────────────────────────────────────────

class Phase(Enum):
    S0 = "S0"    # Both entries on book
    S1a = "S1a"  # Trade A exit + Trade B entry
    S1b = "S1b"  # Trade A entry + Trade B exit
    S2 = "S2"    # Both exits on book


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class Role(Enum):
    ENTRY = "entry"
    EXIT = "exit"


@dataclass(frozen=True)
class OrderState:
    """One order on the book."""
    side: Side
    role: Role
    price: float
    volume: float
    trade_id: str          # "A" or "B"
    cycle: int
    entry_filled_at: float = 0.0      # When entry fill created this exit
    matched_entry_price: float = 0.0  # Cost basis for exit orders


@dataclass(frozen=True)
class RecoveryState:
    """One recovery (orphaned exit) order."""
    side: Side
    price: float
    volume: float
    trade_id: str
    cycle: int
    entry_price: float
    orphaned_at: float = 0.0
    entry_filled_at: float = 0.0
    reason: str = "timeout"


@dataclass(frozen=True)
class CycleRecord:
    """One completed round-trip."""
    trade_id: str
    cycle: int
    entry_price: float
    exit_price: float
    volume: float
    gross_profit: float
    fees: float
    net_profit: float
    entry_time: float = 0.0
    exit_time: float = 0.0


@dataclass(frozen=True)
class PairState:
    """Complete snapshot of pair trading state."""
    market_price: float
    now: float                    # Current timestamp

    orders: tuple[OrderState, ...] = ()
    recovery_orders: tuple[RecoveryState, ...] = ()
    completed_cycles: tuple[CycleRecord, ...] = ()

    cycle_a: int = 1
    cycle_b: int = 1
    total_profit: float = 0.0
    total_fees: float = 0.0
    total_round_trips: int = 0
    total_recovery_wins: float = 0.0
    total_recovery_losses: int = 0

    # Exit lifecycle
    s2_entered_at: float | None = None
    s2_last_action_at: float | None = None  # Cooldown anchor after break-glass
    last_reprice_a: float = 0.0
    last_reprice_b: float = 0.0
    exit_reprice_count_a: int = 0
    exit_reprice_count_b: int = 0
    last_price_update_at: float | None = None  # When price was last fetched

    # Directional signal
    detected_trend: str | None = None  # "up", "down", or None
    trend_detected_at: float | None = None

    # Anti-chase
    consecutive_refreshes_a: int = 0
    consecutive_refreshes_b: int = 0
    last_refresh_direction_a: str | None = None
    last_refresh_direction_b: str | None = None
    refresh_cooldown_until_a: float = 0.0
    refresh_cooldown_until_b: float = 0.0

    # Timing stats (median duration of completed cycles, in seconds)
    median_cycle_duration: float | None = None
    mean_net_profit: float | None = None
    mean_duration_sec: float | None = None

    # Entry multiplier
    next_entry_multiplier: float = 1.0

    # Long-only mode (no sell entries -- spot pairs without inventory)
    long_only: bool = False

    # Anti-loss-spiral: consecutive losing cycles per trade leg
    consecutive_losses_a: int = 0
    consecutive_losses_b: int = 0

    # Volatility auto-adjust: last time profit target was adjusted
    last_volatility_adjust: float = 0.0


def derive_phase(state: PairState) -> Phase:
    """Derive phase from open orders. Maps to _compute_pair_state()."""
    has_a_exit = any(o.side == Side.BUY and o.role == Role.EXIT for o in state.orders)
    has_b_exit = any(o.side == Side.SELL and o.role == Role.EXIT for o in state.orders)
    if has_a_exit and has_b_exit:
        return Phase.S2
    if has_a_exit:
        return Phase.S1a
    if has_b_exit:
        return Phase.S1b
    return Phase.S0


def _compute_volume(price: float, cfg: ModelConfig, multiplier: float = 1.0) -> float:
    """Compute order volume from USD size and price."""
    raw = cfg.order_size_usd / price * multiplier
    if cfg.volume_decimals == 0:
        vol = max(round(raw), cfg.min_volume)
    else:
        vol = round(raw, cfg.volume_decimals)
        if vol < cfg.min_volume:
            vol = cfg.min_volume
    return vol


def make_initial_state(market_price: float, now: float, cfg: ModelConfig,
                       long_only: bool = False) -> PairState:
    """Create S0 state with entry orders flanking the market.
    In long-only mode, only the buy entry is placed (no sell entry)."""
    a_pct, b_pct = cfg.entry_pct, cfg.entry_pct
    buy_price = round(market_price * (1 - b_pct / 100), cfg.price_decimals)
    buy_vol = _compute_volume(buy_price, cfg, cfg.next_entry_multiplier)
    orders = [OrderState(Side.BUY, Role.ENTRY, buy_price, buy_vol, "B", 1)]
    if not long_only:
        sell_price = round(market_price * (1 + a_pct / 100), cfg.price_decimals)
        sell_vol = _compute_volume(sell_price, cfg, cfg.next_entry_multiplier)
        orders.insert(0, OrderState(Side.SELL, Role.ENTRY, sell_price, sell_vol, "A", 1))
    return PairState(
        market_price=market_price,
        now=now,
        orders=tuple(orders),
        next_entry_multiplier=cfg.next_entry_multiplier,
        long_only=long_only,
    )


# ──────────────────────────────────────────────────────────────────────
# 3. Events
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BuyFill:
    """A buy order filled."""
    price: float
    volume: float


@dataclass(frozen=True)
class SellFill:
    """A sell order filled."""
    price: float
    volume: float


@dataclass(frozen=True)
class PriceTick:
    """Market price update."""
    price: float


@dataclass(frozen=True)
class TimeAdvance:
    """Clock moves forward."""
    new_time: float


@dataclass(frozen=True)
class RecoveryFill:
    """A recovery order filled (surprise!)."""
    index: int         # Index into recovery_orders
    fill_price: float


@dataclass(frozen=True)
class RecoveryCancel:
    """A recovery order was cancelled externally."""
    index: int


Event = Union[BuyFill, SellFill, PriceTick, TimeAdvance, RecoveryFill, RecoveryCancel]


# ──────────────────────────────────────────────────────────────────────
# 4. Actions (output — what the bot *would* do)
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlaceOrder:
    side: Side
    role: Role
    price: float
    volume: float
    trade_id: str
    cycle: int
    matched_entry_price: float = 0.0


@dataclass(frozen=True)
class CancelOrder:
    order: OrderState
    reason: str = ""


@dataclass(frozen=True)
class BookProfit:
    trade_id: str
    cycle: int
    net_profit: float
    gross_profit: float
    fees: float


@dataclass(frozen=True)
class OrphanExit:
    order: OrderState
    reason: str


@dataclass(frozen=True)
class RepriceExit:
    old_order: OrderState
    new_price: float
    reprice_count: int


@dataclass(frozen=True)
class DetectTrend:
    direction: str  # "up" or "down"


Action = Union[PlaceOrder, CancelOrder, BookProfit, OrphanExit, RepriceExit, DetectTrend]


# ──────────────────────────────────────────────────────────────────────
# 5. Price helpers
# ──────────────────────────────────────────────────────────────────────

def _exit_price(entry_fill: float, market: float, exit_side: Side,
                cfg: ModelConfig) -> float:
    """Compute exit limit price. Maps to _pair_exit_price()."""
    profit = cfg.profit_pct / 100.0
    entry = cfg.entry_pct / 100.0
    if exit_side == Side.SELL:
        from_entry = entry_fill * (1 + profit)
        from_market = market * (1 + entry)
        return round(max(from_entry, from_market), cfg.price_decimals)
    else:
        from_entry = entry_fill * (1 - profit)
        from_market = market * (1 - entry)
        return round(min(from_entry, from_market), cfg.price_decimals)


def _repriced_exit_price(entry_price: float, market: float, exit_side: Side,
                         cfg: ModelConfig, reprice_count: int) -> float:
    """Compute repriced exit price. Maps to _repriced_exit()."""
    minimum = _exit_price(entry_price, market, exit_side, cfg)
    profit = cfg.profit_pct / 100.0
    margin = cfg.fee_margin

    if exit_side == Side.SELL:
        original = entry_price * (1 + profit)
        breakeven_plus = entry_price * (1 + margin)
        target = (original + breakeven_plus) / 2 if reprice_count == 0 else breakeven_plus
        return round(max(target, minimum), cfg.price_decimals)
    else:
        original = entry_price * (1 - profit)
        breakeven_plus = entry_price * (1 - margin)
        target = (original + breakeven_plus) / 2 if reprice_count == 0 else breakeven_plus
        return round(min(target, minimum), cfg.price_decimals)


def _compute_thresholds(state: PairState, cfg: ModelConfig) -> dict | None:
    """Compute reprice/orphan thresholds. Maps to compute_exit_thresholds()."""
    n_cycles = len(state.completed_cycles)
    if n_cycles < cfg.min_cycles_for_timing:
        return None
    median = state.median_cycle_duration
    if not median or median <= 0:
        return None
    return {
        "reprice_after": median * cfg.exit_reprice_mult,
        "orphan_after": median * cfg.exit_orphan_mult,
    }


def _entry_distances(state: PairState, cfg: ModelConfig) -> tuple[float, float]:
    """Compute asymmetric entry distances. Maps to compute_entry_distances().
    Returns (a_entry_pct, b_entry_pct)."""
    base = cfg.entry_pct
    trend = state.detected_trend
    if not trend:
        return (base, base)
    asym = cfg.directional_asymmetry
    if trend == "down":
        return (base * asym, base * (2 - asym))
    else:  # "up"
        return (base * (2 - asym), base * asym)


def _trend_expiry(state: PairState, cfg: ModelConfig) -> float:
    """How long before a trend signal expires."""
    if state.median_cycle_duration and state.median_cycle_duration > 0:
        return state.median_cycle_duration * cfg.exit_orphan_mult
    return cfg.recovery_fallback_sec


def _backoff_entry_pct(base_pct: float, consecutive_losses: int,
                       cfg: ModelConfig) -> float:
    """Widen entry distance after consecutive losses. Maps to get_backoff_entry_pct()."""
    if not cfg.entry_backoff_enabled or consecutive_losses <= 0:
        return base_pct
    multiplier = min(
        1 + cfg.entry_backoff_factor * consecutive_losses,
        cfg.entry_backoff_max_multiplier,
    )
    return base_pct * multiplier


# ──────────────────────────────────────────────────────────────────────
# 6. Fill handlers
# ──────────────────────────────────────────────────────────────────────

def _find_order(state: PairState, side: Side, role: Role) -> OrderState | None:
    """Find an order by side+role."""
    for o in state.orders:
        if o.side == side and o.role == role:
            return o
    return None


def _remove_order(orders: tuple[OrderState, ...],
                  target: OrderState) -> tuple[OrderState, ...]:
    """Remove an order from the tuple."""
    return tuple(o for o in orders if o is not target)


def _handle_buy_fill(state: PairState, event: BuyFill,
                     cfg: ModelConfig) -> tuple[PairState, list[Action]]:
    """Handle a buy order filling. Could be B entry or A exit."""
    actions: list[Action] = []

    # Check for buy exit (Trade A round-trip completion)
    buy_exit = _find_order(state, Side.BUY, Role.EXIT)
    if buy_exit and abs(buy_exit.price - event.price) < 1e-8:
        return _complete_round_trip_a(state, buy_exit, event, cfg)

    # Must be buy entry (Trade B entry fill)
    buy_entry = _find_order(state, Side.BUY, Role.ENTRY)
    if not buy_entry:
        return state, actions  # No matching order

    # Entry fill: place sell exit
    exit_price = _exit_price(event.price, state.market_price, Side.SELL, cfg)
    exit_vol = buy_entry.volume
    fee = event.price * event.volume * cfg.maker_fee_pct / 100.0

    new_exit = OrderState(
        Side.SELL, Role.EXIT, exit_price, exit_vol,
        "B", state.cycle_b,
        entry_filled_at=state.now,
        matched_entry_price=event.price,
    )

    # Reset multiplier if > 1
    new_mult = 1.0 if state.next_entry_multiplier > 1.0 else state.next_entry_multiplier

    orders = _remove_order(state.orders, buy_entry) + (new_exit,)
    actions.append(PlaceOrder(Side.SELL, Role.EXIT, exit_price, exit_vol,
                              "B", state.cycle_b, event.price))

    new_state = replace(state,
                        orders=orders,
                        total_fees=state.total_fees + fee,
                        next_entry_multiplier=new_mult)
    return new_state, actions


def _handle_sell_fill(state: PairState, event: SellFill,
                      cfg: ModelConfig) -> tuple[PairState, list[Action]]:
    """Handle a sell order filling. Could be A entry or B exit."""
    actions: list[Action] = []

    # Check for sell exit (Trade B round-trip completion)
    sell_exit = _find_order(state, Side.SELL, Role.EXIT)
    if sell_exit and abs(sell_exit.price - event.price) < 1e-8:
        return _complete_round_trip_b(state, sell_exit, event, cfg)

    # Must be sell entry (Trade A entry fill)
    sell_entry = _find_order(state, Side.SELL, Role.ENTRY)
    if not sell_entry:
        return state, actions

    # Entry fill: place buy exit
    exit_price = _exit_price(event.price, state.market_price, Side.BUY, cfg)
    exit_vol = sell_entry.volume
    fee = event.price * event.volume * cfg.maker_fee_pct / 100.0

    new_exit = OrderState(
        Side.BUY, Role.EXIT, exit_price, exit_vol,
        "A", state.cycle_a,
        entry_filled_at=state.now,
        matched_entry_price=event.price,
    )

    new_mult = 1.0 if state.next_entry_multiplier > 1.0 else state.next_entry_multiplier

    orders = _remove_order(state.orders, sell_entry) + (new_exit,)
    actions.append(PlaceOrder(Side.BUY, Role.EXIT, exit_price, exit_vol,
                              "A", state.cycle_a, event.price))

    new_state = replace(state,
                        orders=orders,
                        total_fees=state.total_fees + fee,
                        next_entry_multiplier=new_mult)
    return new_state, actions


def _complete_round_trip_a(state: PairState, buy_exit: OrderState,
                           event: BuyFill,
                           cfg: ModelConfig) -> tuple[PairState, list[Action]]:
    """Trade A exit fills (buy exit). Sell entry -> buy exit complete."""
    actions: list[Action] = []
    sell_price = buy_exit.matched_entry_price
    buy_price = event.price
    volume = buy_exit.volume

    gross = (sell_price - buy_price) * volume
    fees = (buy_price * volume + sell_price * volume) * cfg.maker_fee_pct / 100.0
    net = gross - fees

    actions.append(BookProfit("A", buy_exit.cycle, net, gross, fees))

    cycle_rec = CycleRecord(
        "A", buy_exit.cycle, sell_price, buy_price, volume,
        gross, fees, net,
        entry_time=buy_exit.entry_filled_at, exit_time=state.now,
    )

    # Clear trend if expired
    new_trend = state.detected_trend
    new_trend_at = state.trend_detected_at
    if new_trend and new_trend_at:
        expiry = _trend_expiry(state, cfg)
        if state.now - new_trend_at > expiry:
            new_trend = None
            new_trend_at = None

    # Cancel existing sell entry, place fresh one (skip in long-only mode)
    sell_entry = _find_order(state, Side.SELL, Role.ENTRY)
    orders = _remove_order(state.orders, buy_exit)
    if sell_entry:
        orders = _remove_order(orders, sell_entry)
        actions.append(CancelOrder(sell_entry, "round-trip complete, refresh entry"))

    new_cycle_a = buy_exit.cycle + 1

    # Backoff: track consecutive losses for Trade A
    tid = buy_exit.trade_id
    new_losses_a = state.consecutive_losses_a
    new_losses_b = state.consecutive_losses_b
    if tid == "A":
        new_losses_a = new_losses_a + 1 if net < 0 else 0
    elif tid == "B":
        new_losses_b = new_losses_b + 1 if net < 0 else 0

    if not state.long_only:
        a_pct, _ = _entry_distances(
            replace(state, detected_trend=new_trend, trend_detected_at=new_trend_at), cfg)
        # Apply backoff on top of asymmetry for Trade A re-entry
        if tid == "A":
            a_pct = _backoff_entry_pct(a_pct, new_losses_a, cfg)
        new_sell_price = round(state.market_price * (1 + a_pct / 100), cfg.price_decimals)
        new_sell_vol = _compute_volume(new_sell_price, cfg, state.next_entry_multiplier)

        new_entry = OrderState(Side.SELL, Role.ENTRY, new_sell_price, new_sell_vol,
                               "A", new_cycle_a)
        orders = orders + (new_entry,)
        actions.append(PlaceOrder(Side.SELL, Role.ENTRY, new_sell_price, new_sell_vol,
                                  "A", new_cycle_a))

    completed = state.completed_cycles + (cycle_rec,)
    # Update median
    new_median = _compute_median_duration(completed)

    new_state = replace(state,
                        orders=orders,
                        cycle_a=new_cycle_a,
                        total_profit=state.total_profit + net,
                        total_fees=state.total_fees + fees,
                        total_round_trips=state.total_round_trips + 1,
                        completed_cycles=completed,
                        exit_reprice_count_a=0,
                        s2_entered_at=None,
                        detected_trend=new_trend,
                        trend_detected_at=new_trend_at,
                        median_cycle_duration=new_median,
                        consecutive_losses_a=new_losses_a,
                        consecutive_losses_b=new_losses_b)
    return new_state, actions


def _complete_round_trip_b(state: PairState, sell_exit: OrderState,
                           event: SellFill,
                           cfg: ModelConfig) -> tuple[PairState, list[Action]]:
    """Trade B exit fills (sell exit). Buy entry -> sell exit complete."""
    actions: list[Action] = []
    buy_price = sell_exit.matched_entry_price
    sell_price = event.price
    volume = sell_exit.volume

    gross = (sell_price - buy_price) * volume
    fees = (buy_price * volume + sell_price * volume) * cfg.maker_fee_pct / 100.0
    net = gross - fees

    actions.append(BookProfit("B", sell_exit.cycle, net, gross, fees))

    cycle_rec = CycleRecord(
        "B", sell_exit.cycle, buy_price, sell_price, volume,
        gross, fees, net,
        entry_time=sell_exit.entry_filled_at, exit_time=state.now,
    )

    # Clear trend if expired
    new_trend = state.detected_trend
    new_trend_at = state.trend_detected_at
    if new_trend and new_trend_at:
        expiry = _trend_expiry(state, cfg)
        if state.now - new_trend_at > expiry:
            new_trend = None
            new_trend_at = None

    # Cancel existing buy entry, place fresh one
    buy_entry = _find_order(state, Side.BUY, Role.ENTRY)
    orders = _remove_order(state.orders, sell_exit)
    if buy_entry:
        orders = _remove_order(orders, buy_entry)
        actions.append(CancelOrder(buy_entry, "round-trip complete, refresh entry"))

    # Backoff: track consecutive losses for Trade B
    tid = sell_exit.trade_id
    new_losses_a = state.consecutive_losses_a
    new_losses_b = state.consecutive_losses_b
    if tid == "B":
        new_losses_b = new_losses_b + 1 if net < 0 else 0
    elif tid == "A":
        new_losses_a = new_losses_a + 1 if net < 0 else 0

    _, b_pct = _entry_distances(
        replace(state, detected_trend=new_trend, trend_detected_at=new_trend_at), cfg)
    # Apply backoff on top of asymmetry for Trade B re-entry
    if tid == "B":
        b_pct = _backoff_entry_pct(b_pct, new_losses_b, cfg)
    new_buy_price = round(state.market_price * (1 - b_pct / 100), cfg.price_decimals)
    new_buy_vol = _compute_volume(new_buy_price, cfg, state.next_entry_multiplier)
    new_cycle_b = sell_exit.cycle + 1

    new_entry = OrderState(Side.BUY, Role.ENTRY, new_buy_price, new_buy_vol,
                           "B", new_cycle_b)
    orders = orders + (new_entry,)
    actions.append(PlaceOrder(Side.BUY, Role.ENTRY, new_buy_price, new_buy_vol,
                              "B", new_cycle_b))

    completed = state.completed_cycles + (cycle_rec,)
    new_median = _compute_median_duration(completed)

    new_state = replace(state,
                        orders=orders,
                        cycle_b=new_cycle_b,
                        total_profit=state.total_profit + net,
                        total_fees=state.total_fees + fees,
                        total_round_trips=state.total_round_trips + 1,
                        completed_cycles=completed,
                        exit_reprice_count_b=0,
                        s2_entered_at=None,
                        detected_trend=new_trend,
                        trend_detected_at=new_trend_at,
                        median_cycle_duration=new_median,
                        consecutive_losses_a=new_losses_a,
                        consecutive_losses_b=new_losses_b)
    return new_state, actions


def _compute_median_duration(cycles: tuple[CycleRecord, ...]) -> float | None:
    """Compute median cycle duration from completed cycles."""
    durations = [c.exit_time - c.entry_time for c in cycles
                 if c.entry_time > 0 and c.exit_time > c.entry_time]
    if not durations:
        return None
    durations.sort()
    n = len(durations)
    if n % 2 == 1:
        return durations[n // 2]
    return (durations[n // 2 - 1] + durations[n // 2]) / 2


# ──────────────────────────────────────────────────────────────────────
# 7. Exit lifecycle sub-machines
# ──────────────────────────────────────────────────────────────────────

def _orphan_exit(state: PairState, order: OrderState, cfg: ModelConfig,
                 reason: str) -> tuple[PairState, list[Action]]:
    """Move exit to recovery, place fresh entry. Maps to _orphan_exit()."""
    actions: list[Action] = []
    actions.append(OrphanExit(order, reason))

    # Track backoff counters (may be incremented by eviction)
    new_losses_a = state.consecutive_losses_a
    new_losses_b = state.consecutive_losses_b

    # Evict oldest recovery if at capacity
    recovery = list(state.recovery_orders)
    evict_actions: list[Action] = []
    if len(recovery) >= cfg.max_recovery_slots:
        evicted = recovery.pop(0)
        evict_actions.append(CancelOrder(
            OrderState(evicted.side, Role.EXIT, evicted.price, evicted.volume,
                       evicted.trade_id, evicted.cycle),
            "evict oldest recovery"))
        # Increment backoff counter for evicted trade leg
        if evicted.trade_id == "A":
            new_losses_a += 1
        elif evicted.trade_id == "B":
            new_losses_b += 1

    # Create recovery order
    rec = RecoveryState(
        side=order.side, price=order.price, volume=order.volume,
        trade_id=order.trade_id, cycle=order.cycle,
        entry_price=order.matched_entry_price,
        orphaned_at=state.now,
        entry_filled_at=order.entry_filled_at,
        reason=reason,
    )
    recovery.append(rec)

    # Detect trend
    if order.side == Side.SELL:
        new_trend = "down"  # sell exit stranded = price going down
    else:
        new_trend = "up"    # buy exit stranded = price going up
    actions.append(DetectTrend(new_trend))

    # Place fresh entry
    temp_state = replace(state, detected_trend=new_trend, trend_detected_at=state.now)
    a_pct, b_pct = _entry_distances(temp_state, cfg)
    # Apply backoff on top of asymmetry
    a_pct = _backoff_entry_pct(a_pct, new_losses_a, cfg)
    b_pct = _backoff_entry_pct(b_pct, new_losses_b, cfg)

    orders = _remove_order(state.orders, order)

    if order.trade_id == "B":
        # Trade B sell exit orphaned -> place buy entry
        new_cycle = state.cycle_b + 1
        price = round(state.market_price * (1 - b_pct / 100), cfg.price_decimals)
        vol = _compute_volume(price, cfg, state.next_entry_multiplier)
        new_entry = OrderState(Side.BUY, Role.ENTRY, price, vol, "B", new_cycle)
        orders = orders + (new_entry,)
        actions.append(PlaceOrder(Side.BUY, Role.ENTRY, price, vol, "B", new_cycle))
        new_state = replace(state,
                            orders=orders,
                            recovery_orders=tuple(recovery),
                            cycle_b=new_cycle,
                            exit_reprice_count_b=0,
                            detected_trend=new_trend,
                            trend_detected_at=state.now,
                            consecutive_refreshes_b=0,
                            refresh_cooldown_until_b=0.0,
                            consecutive_losses_a=new_losses_a,
                            consecutive_losses_b=new_losses_b)
    else:
        # Trade A buy exit orphaned -> place sell entry (skip in long-only)
        new_cycle = state.cycle_a + 1
        if not state.long_only:
            price = round(state.market_price * (1 + a_pct / 100), cfg.price_decimals)
            vol = _compute_volume(price, cfg, state.next_entry_multiplier)
            new_entry = OrderState(Side.SELL, Role.ENTRY, price, vol, "A", new_cycle)
            orders = orders + (new_entry,)
            actions.append(PlaceOrder(Side.SELL, Role.ENTRY, price, vol, "A", new_cycle))
        new_state = replace(state,
                            orders=orders,
                            recovery_orders=tuple(recovery),
                            cycle_a=new_cycle,
                            exit_reprice_count_a=0,
                            detected_trend=new_trend,
                            trend_detected_at=state.now,
                            consecutive_refreshes_a=0,
                            refresh_cooldown_until_a=0.0,
                            consecutive_losses_a=new_losses_a,
                            consecutive_losses_b=new_losses_b)

    return new_state, evict_actions + actions


def _check_stale_exits(state: PairState,
                       cfg: ModelConfig) -> tuple[PairState, list[Action]]:
    """Check for stale exits in S1a/S1b. Maps to check_stale_exits()."""
    actions: list[Action] = []
    phase = derive_phase(state)
    if phase not in (Phase.S1a, Phase.S1b):
        return state, actions

    thresholds = _compute_thresholds(state, cfg)
    if thresholds is None:
        # Fallback timeout for recovery
        timeout = cfg.recovery_fallback_sec
        for o in state.orders:
            if o.role == Role.EXIT and o.entry_filled_at > 0:
                exit_age = state.now - o.entry_filled_at
                if exit_age >= timeout:
                    state, orphan_actions = _orphan_exit(state, o, cfg, "timeout")
                    actions.extend(orphan_actions)
                    break  # Only one exit in S1
        return state, actions

    for o in state.orders:
        if o.role != Role.EXIT or o.entry_filled_at <= 0:
            continue

        exit_age = state.now - o.entry_filled_at

        # Orphan check (highest priority)
        if exit_age >= thresholds["orphan_after"]:
            state, orphan_actions = _orphan_exit(state, o, cfg, "timeout")
            actions.extend(orphan_actions)
            break  # Only one exit in S1

        # Reprice eligibility
        if exit_age < thresholds["reprice_after"]:
            continue

        # Cooldown check
        last_reprice = (state.last_reprice_a if o.trade_id == "A"
                        else state.last_reprice_b)
        if state.now - last_reprice < cfg.reprice_cooldown_sec:
            continue

        # Compute new price
        reprice_count = (state.exit_reprice_count_a if o.trade_id == "A"
                         else state.exit_reprice_count_b)
        new_price = _repriced_exit_price(
            o.matched_entry_price, state.market_price, o.side, cfg, reprice_count)

        # Safety checks (one-way ratchet)
        if o.side == Side.SELL and new_price >= o.price:
            continue
        if o.side == Side.BUY and new_price <= o.price:
            continue

        # Must still be profitable
        est_fee = o.matched_entry_price * o.volume * cfg.maker_fee_pct / 100 * 2
        if o.side == Side.SELL:
            if new_price * o.volume <= o.matched_entry_price * o.volume + est_fee:
                continue
        else:
            if o.matched_entry_price * o.volume <= new_price * o.volume + est_fee:
                continue

        # Must be meaningful (> 0.1% change)
        if abs(new_price - o.price) / o.price < 0.001:
            continue

        # Execute reprice
        actions.append(RepriceExit(o, new_price, reprice_count + 1))
        actions.append(DetectTrend("down" if o.side == Side.SELL else "up"))

        new_order = replace(o, price=new_price)
        orders = _remove_order(state.orders, o) + (new_order,)

        if o.trade_id == "A":
            state = replace(state,
                            orders=orders,
                            last_reprice_a=state.now,
                            exit_reprice_count_a=reprice_count + 1,
                            detected_trend="up",
                            trend_detected_at=state.now)
        else:
            state = replace(state,
                            orders=orders,
                            last_reprice_b=state.now,
                            exit_reprice_count_b=reprice_count + 1,
                            detected_trend="down",
                            trend_detected_at=state.now)
        break  # Only one exit in S1

    return state, actions


def _check_s2_break_glass(state: PairState,
                          cfg: ModelConfig) -> tuple[PairState, list[Action]]:
    """S2 break-glass protocol. Maps to check_s2_break_glass()."""
    actions: list[Action] = []
    phase = derive_phase(state)

    if phase != Phase.S2:
        if state.s2_entered_at is not None:
            state = replace(state, s2_entered_at=None)
        return state, actions

    # Stale price guard
    if (state.last_price_update_at is not None
            and (state.now - state.last_price_update_at) > cfg.price_staleness_limit):
        return state, actions

    # Cooldown after last break-glass action
    if (state.s2_last_action_at is not None
            and (state.now - state.s2_last_action_at) < cfg.s2_cooldown_sec):
        return state, actions

    # Phase 1: Record entry time
    if state.s2_entered_at is None:
        return replace(state, s2_entered_at=state.now), actions

    s2_age = state.now - state.s2_entered_at

    # Compute timeout
    thresholds = _compute_thresholds(state, cfg)
    if thresholds:
        reprice_threshold = thresholds["reprice_after"]
    else:
        reprice_threshold = cfg.s2_fallback_sec

    if s2_age < reprice_threshold:
        return state, actions

    # Phase 2: Evaluate spread
    buy_exit = _find_order(state, Side.BUY, Role.EXIT)
    sell_exit = _find_order(state, Side.SELL, Role.EXIT)
    if not buy_exit or not sell_exit:
        return state, actions

    spread_pct = (sell_exit.price - buy_exit.price) / state.market_price * 100
    if spread_pct < cfg.s2_max_spread_pct:
        # Spread tolerable -- reset timer so it measures continuous bad-spread duration
        state = replace(state, s2_entered_at=state.now)
        return state, actions

    # Phase 3: Identify worse trade (larger distance from market)
    a_dist = abs(buy_exit.price - state.market_price) / state.market_price
    b_dist = abs(sell_exit.price - state.market_price) / state.market_price
    worse = buy_exit if a_dist > b_dist else sell_exit

    # Phase 4: Opportunity cost check (with entry price guard)
    do_close = False
    if not worse.matched_entry_price or worse.matched_entry_price <= 0:
        pass  # Skip opp cost — no entry price, fall through to reprice
    elif (state.mean_net_profit is not None and state.mean_duration_sec
            and state.mean_duration_sec > 0):
        profit_per_sec = state.mean_net_profit / state.mean_duration_sec
        foregone = profit_per_sec * s2_age
        if worse.side == Side.SELL:
            loss = (worse.matched_entry_price - state.market_price) * worse.volume
        else:
            loss = (state.market_price - worse.matched_entry_price) * worse.volume
        est_fee = state.market_price * worse.volume * cfg.maker_fee_pct / 100 * 2
        loss_total = abs(loss) + est_fee
        if foregone > loss_total:
            do_close = True

    # Phase 5: Reprice attempt
    if not do_close:
        last_reprice = (state.last_reprice_a if worse.trade_id == "A"
                        else state.last_reprice_b)
        if state.now - last_reprice >= cfg.reprice_cooldown_sec:
            reprice_count = (state.exit_reprice_count_a if worse.trade_id == "A"
                             else state.exit_reprice_count_b)
            new_price = _repriced_exit_price(
                worse.matched_entry_price, state.market_price, worse.side,
                cfg, reprice_count)

            meaningful = abs(new_price - worse.price) / worse.price >= 0.001
            closer = ((worse.side == Side.SELL and new_price < worse.price) or
                      (worse.side == Side.BUY and new_price > worse.price))

            if meaningful and closer:
                actions.append(RepriceExit(worse, new_price, reprice_count + 1))
                new_order = replace(worse, price=new_price)
                orders = _remove_order(state.orders, worse) + (new_order,)

                # Check if new spread resolves
                if worse.side == Side.SELL:
                    new_spread = (new_price - buy_exit.price) / state.market_price * 100
                else:
                    new_spread = (sell_exit.price - new_price) / state.market_price * 100

                if worse.trade_id == "A":
                    state = replace(state, orders=orders,
                                    last_reprice_a=state.now,
                                    exit_reprice_count_a=reprice_count + 1)
                else:
                    state = replace(state, orders=orders,
                                    last_reprice_b=state.now,
                                    exit_reprice_count_b=reprice_count + 1)

                if new_spread < cfg.s2_max_spread_pct:
                    state = replace(state, s2_last_action_at=state.now)
                    return state, actions
                do_close = True
            else:
                do_close = True  # Can't reprice meaningfully, fall through

    # Phase 6: Close worse trade
    if do_close:
        state, orphan_actions = _orphan_exit(state, worse, cfg, "s2_break")
        actions.extend(orphan_actions)
        state = replace(state, s2_entered_at=None, s2_last_action_at=state.now)

    return state, actions


def _check_trend_expiry(state: PairState,
                        cfg: ModelConfig) -> PairState:
    """Clear expired trend signal."""
    if not state.detected_trend or not state.trend_detected_at:
        return state
    expiry = _trend_expiry(state, cfg)
    if state.now - state.trend_detected_at > expiry:
        return replace(state, detected_trend=None, trend_detected_at=None)
    return state


def _check_entry_refresh(state: PairState,
                         cfg: ModelConfig) -> tuple[PairState, list[Action]]:
    """Check for stale entries needing refresh. Maps to refresh_stale_entries()."""
    actions: list[Action] = []

    for o in state.orders:
        if o.role != Role.ENTRY:
            continue

        distance_pct = abs(o.price - state.market_price) / state.market_price * 100
        if distance_pct <= cfg.refresh_pct:
            continue

        # Check cooldown
        is_a = o.trade_id == "A"
        cooldown_until = (state.refresh_cooldown_until_a if is_a
                          else state.refresh_cooldown_until_b)
        consec = (state.consecutive_refreshes_a if is_a
                  else state.consecutive_refreshes_b)
        last_dir = (state.last_refresh_direction_a if is_a
                    else state.last_refresh_direction_b)

        if state.now < cooldown_until:
            # Check if cooldown expired and we should reset
            continue

        # If was in cooldown and it's now expired, reset
        if consec >= cfg.max_consecutive_refreshes and cooldown_until > 0:
            if state.now >= cooldown_until:
                consec = 0

        # Determine direction
        if o.side == Side.BUY:
            direction = "down" if state.market_price < o.price else "up"
        else:
            direction = "up" if state.market_price > o.price else "down"

        # Track consecutive
        if direction == last_dir:
            consec += 1
        else:
            consec = 1

        # Chase threshold
        if consec >= cfg.max_consecutive_refreshes:
            new_cooldown = state.now + cfg.refresh_cooldown_sec
            if is_a:
                state = replace(state,
                                consecutive_refreshes_a=consec,
                                last_refresh_direction_a=direction,
                                refresh_cooldown_until_a=new_cooldown)
            else:
                state = replace(state,
                                consecutive_refreshes_b=consec,
                                last_refresh_direction_b=direction,
                                refresh_cooldown_until_b=new_cooldown)
            continue

        # Execute refresh
        a_pct, b_pct = _entry_distances(state, cfg)
        if o.side == Side.BUY:
            new_price = round(state.market_price * (1 - b_pct / 100), cfg.price_decimals)
        else:
            new_price = round(state.market_price * (1 + a_pct / 100), cfg.price_decimals)

        new_vol = _compute_volume(new_price, cfg, state.next_entry_multiplier)
        actions.append(CancelOrder(o, "stale entry refresh"))
        new_entry = OrderState(o.side, Role.ENTRY, new_price, new_vol,
                               o.trade_id, o.cycle)
        actions.append(PlaceOrder(o.side, Role.ENTRY, new_price, new_vol,
                                  o.trade_id, o.cycle))
        orders = _remove_order(state.orders, o) + (new_entry,)

        if is_a:
            state = replace(state,
                            orders=orders,
                            consecutive_refreshes_a=consec,
                            last_refresh_direction_a=direction)
        else:
            state = replace(state,
                            orders=orders,
                            consecutive_refreshes_b=consec,
                            last_refresh_direction_b=direction)

    return state, actions


# ──────────────────────────────────────────────────────────────────────
# 8. Main transition function
# ──────────────────────────────────────────────────────────────────────

def transition(state: PairState, event: Event,
               cfg: ModelConfig) -> tuple[PairState, list[Action]]:
    """
    Pure transition function. Takes frozen state + event, returns new state + actions.
    This is the core of the model.
    """
    actions: list[Action] = []

    match event:
        case BuyFill():
            state, actions = _handle_buy_fill(state, event, cfg)

        case SellFill():
            state, actions = _handle_sell_fill(state, event, cfg)

        case PriceTick(price=price):
            state = replace(state, market_price=price, last_price_update_at=state.now)
            state, refresh_actions = _check_entry_refresh(state, cfg)
            actions.extend(refresh_actions)

        case TimeAdvance(new_time=t):
            state = replace(state, now=t)
            state = _check_trend_expiry(state, cfg)
            state, stale_actions = _check_stale_exits(state, cfg)
            actions.extend(stale_actions)
            state, s2_actions = _check_s2_break_glass(state, cfg)
            actions.extend(s2_actions)

        case RecoveryFill(index=idx, fill_price=fill_price):
            if idx < len(state.recovery_orders):
                rec = state.recovery_orders[idx]
                if rec.side == Side.SELL:
                    gross = (fill_price - rec.entry_price) * rec.volume
                else:
                    gross = (rec.entry_price - fill_price) * rec.volume
                fees = ((rec.entry_price * rec.volume + fill_price * rec.volume)
                        * cfg.maker_fee_pct / 100.0)
                net = gross - fees
                actions.append(BookProfit(rec.trade_id, rec.cycle, net, gross, fees))
                recovery = list(state.recovery_orders)
                recovery.pop(idx)
                # Reset backoff counter on profitable recovery
                new_losses_a = state.consecutive_losses_a
                new_losses_b = state.consecutive_losses_b
                if net >= 0:
                    if rec.trade_id == "A":
                        new_losses_a = 0
                    elif rec.trade_id == "B":
                        new_losses_b = 0
                state = replace(state,
                                recovery_orders=tuple(recovery),
                                total_profit=state.total_profit + net,
                                total_fees=state.total_fees + fees,
                                total_round_trips=state.total_round_trips + 1,
                                total_recovery_wins=state.total_recovery_wins + net,
                                consecutive_losses_a=new_losses_a,
                                consecutive_losses_b=new_losses_b)

        case RecoveryCancel(index=idx):
            if idx < len(state.recovery_orders):
                recovery = list(state.recovery_orders)
                recovery.pop(idx)
                state = replace(state,
                                recovery_orders=tuple(recovery),
                                total_recovery_losses=state.total_recovery_losses + 1)

    return state, actions


# ──────────────────────────────────────────────────────────────────────
# 9. Invariant checker
# ──────────────────────────────────────────────────────────────────────

def check_invariants(state: PairState, cfg: ModelConfig) -> list[str]:
    """
    Verify 12 invariants after every transition.
    Returns list of violation descriptions (empty = all good).
    """
    violations = []
    phase = derive_phase(state)

    # 1. Phase matches derived phase
    #    (We derive phase, so this is always true by construction)

    # 2. At most 2 active orders (long-only: at most 1)
    max_orders = 1 if state.long_only else 2
    if len(state.orders) > max_orders:
        violations.append(
            f"INV2: {len(state.orders)} orders on book (max {max_orders})")

    # 3. No duplicate (side, role) combinations
    seen = set()
    for o in state.orders:
        key = (o.side, o.role)
        if key in seen:
            violations.append(
                f"INV3: duplicate ({o.side.value}, {o.role.value})")
        seen.add(key)

    # 4. Recovery orders <= MAX_RECOVERY_SLOTS
    if len(state.recovery_orders) > cfg.max_recovery_slots:
        violations.append(
            f"INV4: {len(state.recovery_orders)} recovery orders "
            f"(max {cfg.max_recovery_slots})")

    # 5-8. Phase-specific order composition
    entries = [o for o in state.orders if o.role == Role.ENTRY]
    exits = [o for o in state.orders if o.role == Role.EXIT]
    buy_entries = [o for o in entries if o.side == Side.BUY]
    sell_entries = [o for o in entries if o.side == Side.SELL]
    buy_exits = [o for o in exits if o.side == Side.BUY]
    sell_exits = [o for o in exits if o.side == Side.SELL]

    if phase == Phase.S0:
        if state.long_only:
            # Long-only: S0 has just 1 buy entry, no sell entry
            if len(buy_entries) != 1:
                violations.append(
                    f"INV5: S0 (long-only) should have 1 buy entry, "
                    f"got {len(buy_entries)}")
            if sell_entries:
                violations.append(
                    f"INV5: S0 (long-only) should have 0 sell entries, "
                    f"got {len(sell_entries)}")
        else:
            if len(buy_entries) != 1 or len(sell_entries) != 1:
                violations.append(
                    f"INV5: S0 should have 1 buy entry + 1 sell entry, "
                    f"got {len(buy_entries)} buy + {len(sell_entries)} sell entries")
        if exits:
            violations.append(
                f"INV5: S0 should have 0 exits, got {len(exits)}")

    elif phase == Phase.S1a:
        if len(buy_exits) != 1:
            violations.append(
                f"INV6: S1a should have 1 buy exit, got {len(buy_exits)}")
        if len(buy_entries) != 1 and len(sell_entries) != 1:
            # S1a: buy exit (Trade A) + one entry (could be buy or sell)
            # Typically: buy exit + buy entry (Trade B entry)
            pass  # Relaxed: just need an exit
        if sell_exits:
            violations.append(
                f"INV6: S1a should have 0 sell exits, got {len(sell_exits)}")

    elif phase == Phase.S1b:
        if len(sell_exits) != 1:
            violations.append(
                f"INV7: S1b should have 1 sell exit, got {len(sell_exits)}")
        if buy_exits:
            violations.append(
                f"INV7: S1b should have 0 buy exits, got {len(buy_exits)}")
        # Long-only: S1b has sell exit only (no sell entry companion)
        # Normal: S1b has sell exit + sell entry (or buy entry)

    elif phase == Phase.S2:
        if len(buy_exits) != 1 or len(sell_exits) != 1:
            violations.append(
                f"INV8: S2 should have 1 buy exit + 1 sell exit, "
                f"got {len(buy_exits)} buy + {len(sell_exits)} sell exits")
        if entries:
            violations.append(
                f"INV8: S2 should have 0 entries, got {len(entries)}")

    # 9. Exit prices on correct side of entry price
    for o in exits:
        if o.matched_entry_price > 0:
            if o.side == Side.SELL and o.price < o.matched_entry_price * 0.995:
                violations.append(
                    f"INV9: sell exit ${o.price} below entry "
                    f"${o.matched_entry_price} (Trade {o.trade_id})")
            if o.side == Side.BUY and o.price > o.matched_entry_price * 1.005:
                violations.append(
                    f"INV9: buy exit ${o.price} above entry "
                    f"${o.matched_entry_price} (Trade {o.trade_id})")

    # 10. Cycle numbers >= 1
    if state.cycle_a < 1:
        violations.append(f"INV10: cycle_a={state.cycle_a} < 1")
    if state.cycle_b < 1:
        violations.append(f"INV10: cycle_b={state.cycle_b} < 1")
    for o in state.orders:
        if o.cycle < 1:
            violations.append(
                f"INV10: order {o.trade_id}.{o.cycle} has cycle < 1")

    # 11. S2 timer set iff phase == S2
    if phase == Phase.S2 and state.s2_entered_at is None:
        # Timer might not be set yet on first entry to S2 (set by break-glass check)
        pass
    if phase != Phase.S2 and state.s2_entered_at is not None:
        violations.append(
            f"INV11: s2_entered_at set ({state.s2_entered_at}) but phase={phase.value}")

    # 12. Recovery order reasons are valid
    valid_reasons = {"timeout", "s2_break", "repriced_out"}
    for r in state.recovery_orders:
        if r.reason not in valid_reasons:
            violations.append(
                f"INV12: recovery order {r.trade_id}.{r.cycle} has "
                f"invalid reason '{r.reason}'")

    return violations


# ──────────────────────────────────────────────────────────────────────
# 10. Predict function
# ──────────────────────────────────────────────────────────────────────

def predict(state: PairState, cfg: ModelConfig) -> dict:
    """
    Human-readable predictions from current state.
    Maps to what the dashboard would show + what-if analysis.
    """
    phase = derive_phase(state)
    result = {
        "phase": phase.value,
        "market_price": state.market_price,
        "long_only": state.long_only,
        "orders": [],
        "recovery_slots": f"{len(state.recovery_orders)}/{cfg.max_recovery_slots}",
        "trend": state.detected_trend,
        "total_profit": round(state.total_profit, 4),
        "total_round_trips": state.total_round_trips,
        "backoff_a": state.consecutive_losses_a,
        "backoff_b": state.consecutive_losses_b,
    }

    # Show effective entry distances with backoff applied
    if state.consecutive_losses_a > 0 or state.consecutive_losses_b > 0:
        a_pct, b_pct = _entry_distances(state, cfg)
        eff_a = _backoff_entry_pct(a_pct, state.consecutive_losses_a, cfg)
        eff_b = _backoff_entry_pct(b_pct, state.consecutive_losses_b, cfg)
        result["effective_entry_a_pct"] = round(eff_a, 4)
        result["effective_entry_b_pct"] = round(eff_b, 4)

    for o in state.orders:
        dist = abs(o.price - state.market_price) / state.market_price * 100
        info = {
            "side": o.side.value,
            "role": o.role.value,
            "trade": o.trade_id,
            "cycle": o.cycle,
            "price": o.price,
            "distance_pct": round(dist, 3),
        }
        if o.role == Role.EXIT and o.entry_filled_at > 0:
            age = state.now - o.entry_filled_at
            info["exit_age_sec"] = round(age, 1)
            thresholds = _compute_thresholds(state, cfg)
            if thresholds:
                info["reprice_in_sec"] = round(
                    max(0, thresholds["reprice_after"] - age), 1)
                info["orphan_in_sec"] = round(
                    max(0, thresholds["orphan_after"] - age), 1)
        result["orders"].append(info)

    if phase == Phase.S2 and state.s2_entered_at:
        s2_age = state.now - state.s2_entered_at
        result["s2_age_sec"] = round(s2_age, 1)
        thresholds = _compute_thresholds(state, cfg)
        timeout = thresholds["reprice_after"] if thresholds else cfg.s2_fallback_sec
        result["s2_break_glass_in_sec"] = round(max(0, timeout - s2_age), 1)

    for r in state.recovery_orders:
        if r.side == Side.SELL:
            unreal = (state.market_price - r.entry_price) * r.volume
        else:
            unreal = (r.entry_price - state.market_price) * r.volume
        result.setdefault("recovery", []).append({
            "trade": r.trade_id,
            "cycle": r.cycle,
            "side": r.side.value,
            "price": r.price,
            "entry_price": r.entry_price,
            "unrealized": round(unreal, 4),
            "reason": r.reason,
        })

    return result


# ──────────────────────────────────────────────────────────────────────
# 11. Simulator
# ──────────────────────────────────────────────────────────────────────

@dataclass
class SimStep:
    """One step of a simulation trace."""
    step: int
    event: Event
    phase_before: str
    phase_after: str
    actions: list[Action]
    violations: list[str]
    state_after: PairState


def generate_fills(state: PairState, new_price: float) -> list[Event]:
    """Generate fill events when price crosses orders."""
    fills = []
    for o in state.orders:
        if o.role == Role.ENTRY and o.side == Side.BUY:
            if new_price <= o.price:
                fills.append(BuyFill(o.price, o.volume))
        elif o.role == Role.ENTRY and o.side == Side.SELL:
            if new_price >= o.price:
                fills.append(SellFill(o.price, o.volume))
        elif o.role == Role.EXIT and o.side == Side.BUY:
            if new_price <= o.price:
                fills.append(BuyFill(o.price, o.volume))
        elif o.role == Role.EXIT and o.side == Side.SELL:
            if new_price >= o.price:
                fills.append(SellFill(o.price, o.volume))
    return fills


def simulate(initial: PairState, events: list[Event],
             cfg: ModelConfig) -> list[SimStep]:
    """Feed events through transition(), collect trace."""
    trace = []
    state = initial
    step_num = 0

    for event in events:
        phase_before = derive_phase(state).value

        # PriceTick auto-generates fills
        if isinstance(event, PriceTick):
            fills = generate_fills(state, event.price)
            # Process fills first, then the tick
            for fill in fills:
                step_num += 1
                pb = derive_phase(state).value
                state, actions = transition(state, fill, cfg)
                pa = derive_phase(state).value
                violations = check_invariants(state, cfg)
                trace.append(SimStep(step_num, fill, pb, pa, actions, violations, state))

        step_num += 1
        state, actions = transition(state, event, cfg)
        phase_after = derive_phase(state).value
        violations = check_invariants(state, cfg)
        trace.append(SimStep(step_num, event, phase_before, phase_after,
                             actions, violations, state))

    return trace


def print_trace(trace: list[SimStep], verbose: bool = False):
    """Pretty-print a simulation trace."""
    for step in trace:
        event_name = type(step.event).__name__
        transition_str = ""
        if step.phase_before != step.phase_after:
            transition_str = f"  {step.phase_before} -> {step.phase_after}"

        action_str = ""
        if step.actions:
            names = [type(a).__name__ for a in step.actions]
            action_str = f"  [{', '.join(names)}]"

        violation_str = ""
        if step.violations:
            violation_str = f"  !! {step.violations}"

        if verbose or transition_str or action_str or step.violations:
            price_info = ""
            if hasattr(step.event, 'price'):
                price_info = f" @ ${step.event.price:.6f}"
            elif hasattr(step.event, 'new_time'):
                price_info = f" t={step.event.new_time:.0f}"

            print(f"  [{step.step:4d}] {event_name}{price_info}"
                  f"{transition_str}{action_str}{violation_str}")


# ──────────────────────────────────────────────────────────────────────
# 12. Bridge: from_state_json()
# ──────────────────────────────────────────────────────────────────────

def from_state_json(path: str = "logs/state.json") -> tuple[PairState, ModelConfig]:
    """
    Load production state.json and convert to PairState + ModelConfig.
    Lets you run predict() on your actual live state.
    """
    with open(path, "r", encoding="utf-8") as f:
        snap = json.load(f)

    # Build config from snapshot
    cfg = ModelConfig(
        entry_pct=snap.get("pair_entry_pct", 0.2),
        profit_pct=snap.get("pair_profit_pct", 1.0),
    )

    # Convert open orders
    orders = []
    for od in snap.get("open_orders", []):
        side = Side.BUY if od["side"] == "buy" else Side.SELL
        role = Role.ENTRY if od.get("order_role") == "entry" else Role.EXIT
        entry_price = 0.0
        if role == Role.EXIT:
            if side == Side.BUY:
                entry_price = od.get("matched_sell_price", 0.0) or 0.0
            else:
                entry_price = od.get("matched_buy_price", 0.0) or 0.0
        orders.append(OrderState(
            side=side, role=role,
            price=od.get("price", 0.0),
            volume=od.get("volume", 0.0),
            trade_id=od.get("trade_id", "?"),
            cycle=od.get("cycle", 0),
            entry_filled_at=od.get("entry_filled_at", 0.0),
            matched_entry_price=entry_price,
        ))

    # Convert recovery orders
    recovery = []
    for rd in snap.get("recovery_orders", []):
        recovery.append(RecoveryState(
            side=Side.BUY if rd["side"] == "buy" else Side.SELL,
            price=rd.get("price", 0.0),
            volume=rd.get("volume", 0.0),
            trade_id=rd.get("trade_id", "?"),
            cycle=rd.get("cycle", 0),
            entry_price=rd.get("entry_price", 0.0),
            orphaned_at=rd.get("orphaned_at", 0.0),
            entry_filled_at=rd.get("entry_filled_at", 0.0),
            reason=rd.get("reason", "timeout"),
        ))

    # Convert completed cycles
    cycles = []
    for cd in snap.get("completed_cycles", []):
        cycles.append(CycleRecord(
            trade_id=cd.get("trade_id", "?"),
            cycle=cd.get("cycle", 0),
            entry_price=cd.get("entry_price", 0.0),
            exit_price=cd.get("exit_price", 0.0),
            volume=cd.get("volume", 0.0),
            gross_profit=cd.get("gross_profit", 0.0),
            fees=cd.get("fees", 0.0),
            net_profit=cd.get("net_profit", 0.0),
            entry_time=cd.get("entry_time", 0.0),
            exit_time=cd.get("exit_time", 0.0),
        ))

    # Estimate market price from order midpoint
    if orders:
        prices = [o.price for o in orders]
        market_price = sum(prices) / len(prices)
    else:
        market_price = snap.get("center_price", 0.0)

    import time as _time
    now = snap.get("saved_at", _time.time())
    median = _compute_median_duration(tuple(cycles))

    state = PairState(
        market_price=market_price,
        now=now,
        orders=tuple(orders),
        recovery_orders=tuple(recovery),
        completed_cycles=tuple(cycles),
        cycle_a=snap.get("cycle_a", 1),
        cycle_b=snap.get("cycle_b", 1),
        total_profit=snap.get("total_profit_usd", 0.0),
        total_fees=snap.get("total_fees_usd", 0.0),
        total_round_trips=snap.get("total_round_trips", 0),
        total_recovery_wins=snap.get("total_recovery_wins", 0.0),
        total_recovery_losses=snap.get("total_recovery_losses", 0),
        s2_entered_at=snap.get("s2_entered_at"),
        s2_last_action_at=snap.get("s2_last_action_at"),
        last_reprice_a=snap.get("last_reprice_a", 0.0),
        last_reprice_b=snap.get("last_reprice_b", 0.0),
        last_price_update_at=snap.get("last_price_update_at"),
        exit_reprice_count_a=snap.get("exit_reprice_count_a", 0),
        exit_reprice_count_b=snap.get("exit_reprice_count_b", 0),
        detected_trend=snap.get("detected_trend"),
        trend_detected_at=snap.get("trend_detected_at"),
        consecutive_refreshes_a=snap.get("consecutive_refreshes_a", 0),
        consecutive_refreshes_b=snap.get("consecutive_refreshes_b", 0),
        last_refresh_direction_a=snap.get("last_refresh_direction_a"),
        last_refresh_direction_b=snap.get("last_refresh_direction_b"),
        refresh_cooldown_until_a=snap.get("refresh_cooldown_until_a", 0.0),
        refresh_cooldown_until_b=snap.get("refresh_cooldown_until_b", 0.0),
        median_cycle_duration=median,
        next_entry_multiplier=snap.get("next_entry_multiplier", 1.0),
        long_only=snap.get("long_only", False),
        consecutive_losses_a=snap.get("consecutive_losses_a", 0),
        consecutive_losses_b=snap.get("consecutive_losses_b", 0),
        last_volatility_adjust=snap.get("last_volatility_adjust", 0.0),
    )

    return state, cfg


# ──────────────────────────────────────────────────────────────────────
# 13. Random explorer
# ──────────────────────────────────────────────────────────────────────

def explore_random(n_steps: int = 10000, seed: int = 42) -> list[str]:
    """Run n random transitions, return all invariant violations found."""
    rng = random.Random(seed)
    cfg = default_config()
    market = 0.10  # $0.10 starting price
    t = 1000000.0
    state = make_initial_state(market, t, cfg)
    all_violations = []

    for i in range(n_steps):
        # Random event
        r = rng.random()
        if r < 0.4:
            # Price tick: random walk
            delta = rng.gauss(0, market * 0.005)
            market = max(market * 0.5, min(market * 2.0, market + delta))
            event = PriceTick(round(market, 6))
        elif r < 0.7:
            # Time advance
            t += rng.uniform(5, 120)
            event = TimeAdvance(t)
        elif r < 0.85:
            # Random buy fill (if there's a buy order)
            buy_orders = [o for o in state.orders if o.side == Side.BUY]
            if buy_orders:
                o = rng.choice(buy_orders)
                event = BuyFill(o.price, o.volume)
            else:
                t += 1
                event = TimeAdvance(t)
        elif r < 0.95:
            # Random sell fill
            sell_orders = [o for o in state.orders if o.side == Side.SELL]
            if sell_orders:
                o = rng.choice(sell_orders)
                event = SellFill(o.price, o.volume)
            else:
                t += 1
                event = TimeAdvance(t)
        else:
            # Recovery event
            if state.recovery_orders:
                idx = rng.randrange(len(state.recovery_orders))
                if rng.random() < 0.7:
                    event = RecoveryFill(idx, state.recovery_orders[idx].price)
                else:
                    event = RecoveryCancel(idx)
            else:
                t += 1
                event = TimeAdvance(t)

        state, actions = transition(state, event, cfg)
        violations = check_invariants(state, cfg)
        if violations:
            for v in violations:
                msg = f"Step {i}: {v} (event={type(event).__name__})"
                all_violations.append(msg)

    return all_violations


# ──────────────────────────────────────────────────────────────────────
# 14. Built-in scenarios
# ──────────────────────────────────────────────────────────────────────

def scenario_normal_oscillation() -> tuple[str, PairState, list[Event], ModelConfig]:
    """Price bounces, both sides complete round trips S0 -> S1 -> S0."""
    cfg = default_config(entry_pct=0.5, profit_pct=1.0)
    market = 0.10
    t = 1000000.0
    state = make_initial_state(market, t, cfg)

    # Use direct fill events to avoid PriceTick auto-fill cross-contamination
    buy_entry_price = round(market * (1 - 0.5 / 100), cfg.price_decimals)
    sell_entry_price = round(market * (1 + 0.5 / 100), cfg.price_decimals)
    buy_vol = _compute_volume(buy_entry_price, cfg)
    sell_vol = _compute_volume(sell_entry_price, cfg)

    events = []
    # Trade B: buy entry fills -> S1b
    events.append(BuyFill(buy_entry_price, buy_vol))
    events.append(TimeAdvance(t + 30))
    # Trade B: sell exit fills -> S0 (round trip complete)
    sell_exit_price = _exit_price(buy_entry_price, market, Side.SELL, cfg)
    events.append(SellFill(sell_exit_price, buy_vol))
    events.append(TimeAdvance(t + 60))
    # Trade A: sell entry fills -> S1a
    events.append(SellFill(sell_entry_price, sell_vol))
    events.append(TimeAdvance(t + 90))
    # Trade A: buy exit fills -> S0 (round trip complete)
    buy_exit_price = _exit_price(sell_entry_price, market, Side.BUY, cfg)
    events.append(BuyFill(buy_exit_price, buy_vol))
    events.append(TimeAdvance(t + 120))

    return "Normal Oscillation", state, events, cfg


def scenario_trending_market() -> tuple[str, PairState, list[Event], ModelConfig]:
    """Price drops persistently. S1b -> repricing -> orphaning -> recovery."""
    cfg = default_config(entry_pct=0.5, profit_pct=1.0, min_cycles_for_timing=2)
    market = 0.10
    t = 1000000.0
    state = make_initial_state(market, t, cfg)

    # Pre-seed with completed cycles for timing logic
    completed = []
    for i in range(5):
        completed.append(CycleRecord(
            "B", i + 1, 0.099, 0.101, 35, 0.07, 0.002, 0.068,
            entry_time=t - 1000 + i * 120, exit_time=t - 880 + i * 120))
    state = replace(state, completed_cycles=tuple(completed),
                    median_cycle_duration=120.0)

    events = []
    # Buy entry fills (price drops)
    events.append(PriceTick(0.0995))
    events.append(TimeAdvance(t + 30))
    # Price keeps dropping -- sell exit gets stale
    for i in range(10):
        t_step = t + 60 + i * 30
        events.append(PriceTick(round(0.098 - i * 0.001, 6)))
        events.append(TimeAdvance(t_step))

    # Advance time enough for reprice threshold (120 * 1.5 = 180s)
    events.append(TimeAdvance(t + 250))
    # More time for orphan (120 * 5 = 600s)
    events.append(TimeAdvance(t + 700))

    return "Trending Market", state, events, cfg


def scenario_s2_break_glass() -> tuple[str, PairState, list[Event], ModelConfig]:
    """Both entries fill, S2 deadlock, break-glass resolves."""
    cfg = default_config(entry_pct=0.5, profit_pct=1.0, min_cycles_for_timing=2,
                         s2_fallback_sec=120.0, s2_max_spread_pct=0.5)
    market = 0.10
    t = 1000000.0
    state = make_initial_state(market, t, cfg)

    # Manually construct S2: both entries filled, two exits on book
    # Trade B: bought at 0.0995, sell exit at 0.1005
    # Trade A: sold at 0.1005, buy exit at 0.0995
    sell_exit = OrderState(Side.SELL, Role.EXIT, 0.1005, 35.0, "B", 1,
                           entry_filled_at=t, matched_entry_price=0.0995)
    buy_exit = OrderState(Side.BUY, Role.EXIT, 0.0995, 35.0, "A", 1,
                          entry_filled_at=t, matched_entry_price=0.1005)
    state = replace(state,
                    orders=(sell_exit, buy_exit),
                    market_price=0.10)

    events = [
        TimeAdvance(t + 10),   # First call: records s2_entered_at
        TimeAdvance(t + 50),   # Still within timeout
        TimeAdvance(t + 200),  # Past fallback (120s), trigger break-glass
    ]

    return "S2 Break Glass", state, events, cfg


def scenario_recovery_fill() -> tuple[str, PairState, list[Event], ModelConfig]:
    """Orphaned exit fills on price reversal."""
    cfg = default_config(entry_pct=0.5, profit_pct=1.0)
    market = 0.10
    t = 1000000.0
    state = make_initial_state(market, t, cfg)

    # Manually create a state with a recovery order
    recovery = RecoveryState(
        side=Side.SELL, price=0.101, volume=35.0,
        trade_id="B", cycle=1, entry_price=0.099,
        orphaned_at=t - 100, entry_filled_at=t - 200,
        reason="timeout",
    )
    state = replace(state, recovery_orders=(recovery,))

    events = [
        RecoveryFill(0, 0.101),  # Recovery fills at its limit price
    ]

    return "Recovery Fill", state, events, cfg


def scenario_anti_chase() -> tuple[str, PairState, list[Event], ModelConfig]:
    """3 same-direction refreshes trigger cooldown."""
    cfg = default_config(entry_pct=0.2, refresh_pct=0.5)
    market = 0.10
    t = 1000000.0
    state = make_initial_state(market, t, cfg)

    events = []
    # Price drifts down repeatedly, triggering buy entry refreshes
    for i in range(5):
        price = round(0.098 - i * 0.003, 6)
        events.append(PriceTick(price))
        events.append(TimeAdvance(t + 30 * (i + 1)))

    return "Anti-Chase", state, events, cfg


def scenario_long_only() -> tuple[str, PairState, list[Event], ModelConfig]:
    """Long-only mode: only buy entries, sell entries skipped. Full B cycle."""
    cfg = default_config(entry_pct=0.5, profit_pct=1.0)
    market = 0.10
    t = 1000000.0
    state = make_initial_state(market, t, cfg, long_only=True)

    buy_entry_price = round(market * (1 - 0.5 / 100), cfg.price_decimals)
    buy_vol = _compute_volume(buy_entry_price, cfg)

    events = []
    # Trade B: buy entry fills -> S1b (no sell entry on book)
    events.append(BuyFill(buy_entry_price, buy_vol))
    events.append(TimeAdvance(t + 30))
    # Trade B: sell exit fills -> round trip complete, back to S0 (buy entry only)
    sell_exit_price = _exit_price(buy_entry_price, market, Side.SELL, cfg)
    events.append(SellFill(sell_exit_price, buy_vol))
    events.append(TimeAdvance(t + 60))
    # Second cycle: buy entry fills again
    new_buy_price = round(market * (1 - 0.5 / 100), cfg.price_decimals)
    new_buy_vol = _compute_volume(new_buy_price, cfg)
    events.append(BuyFill(new_buy_price, new_buy_vol))
    events.append(TimeAdvance(t + 90))

    return "Long Only", state, events, cfg


def scenario_random_walk() -> tuple[str, list[str]]:
    """10K random events, check all invariants."""
    violations = explore_random(10000)
    return "Random Walk (10K steps)", violations


# ──────────────────────────────────────────────────────────────────────
# 15. __main__
# ──────────────────────────────────────────────────────────────────────

def _run_scenario(name, initial, events, cfg):
    """Run one scenario and print results."""
    print(f"\n{'='*60}")
    print(f"  Scenario: {name}")
    print(f"{'='*60}")

    phase_before = derive_phase(initial).value
    print(f"  Initial: {phase_before}, market=${initial.market_price:.6f}, "
          f"{len(initial.orders)} orders")

    trace = simulate(initial, events, cfg)
    print_trace(trace)

    final = trace[-1].state_after if trace else initial
    phase_after = derive_phase(final).value
    total_violations = sum(len(s.violations) for s in trace)
    total_actions = sum(len(s.actions) for s in trace)

    print(f"  Final: {phase_after}, profit=${final.total_profit:.4f}, "
          f"round-trips={final.total_round_trips}, "
          f"recovery={len(final.recovery_orders)}")
    print(f"  Steps: {len(trace)}, Actions: {total_actions}, "
          f"Violations: {total_violations}")

    if total_violations > 0:
        print("  ** VIOLATIONS FOUND **")
        for s in trace:
            for v in s.violations:
                print(f"    {v}")

    return total_violations


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "predict":
        # Load production state and predict
        path = sys.argv[2] if len(sys.argv) > 2 else "logs/state.json"
        if not os.path.exists(path):
            print(f"State file not found: {path}")
            sys.exit(1)
        state, cfg = from_state_json(path)
        result = predict(state, cfg)
        print(json.dumps(result, indent=2, default=str))
        violations = check_invariants(state, cfg)
        if violations:
            print("\nInvariant violations:")
            for v in violations:
                print(f"  {v}")
        else:
            print("\nAll invariants OK")
        return

    total_violations = 0

    # Deterministic scenarios
    scenarios = [
        scenario_normal_oscillation,
        scenario_trending_market,
        scenario_s2_break_glass,
        scenario_recovery_fill,
        scenario_anti_chase,
        scenario_long_only,
    ]

    for scenario_fn in scenarios:
        name, initial, events, cfg = scenario_fn()
        total_violations += _run_scenario(name, initial, events, cfg)

    # Random walk
    print(f"\n{'='*60}")
    print(f"  Scenario: Random Walk (10K steps)")
    print(f"{'='*60}")
    name, violations = scenario_random_walk()
    if violations:
        print(f"  {len(violations)} violations found:")
        for v in violations[:20]:
            print(f"    {v}")
        total_violations += len(violations)
    else:
        print("  0 violations across 10,000 random transitions")

    # Summary
    print(f"\n{'='*60}")
    if total_violations == 0:
        print("  ALL SCENARIOS PASSED -- 0 invariant violations")
    else:
        print(f"  FAILURES: {total_violations} total invariant violations")
    print(f"{'='*60}")

    sys.exit(1 if total_violations > 0 else 0)


if __name__ == "__main__":
    main()
