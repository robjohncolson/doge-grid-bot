"""
state_machine.py

DOGE/USD slot state machine core for v1.

Design goals:
- Pure reducer transitions: (state, event) -> (next_state, actions)
- Strict S0/S1a/S1b/S2 pair semantics
- Simple stale-exit orphaning (no tiered repricing)
- S2 timeout orphaning of worse leg
- Exactly-once friendly identifiers (local order ids, recovery ids)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


Side = Literal["buy", "sell"]
Role = Literal["entry", "exit"]
TradeId = Literal["A", "B"]
PairPhase = Literal["S0", "S1a", "S1b", "S2"]


@dataclass(frozen=True)
class EngineConfig:
    entry_pct: float = 0.2
    profit_pct: float = 1.0
    refresh_pct: float = 1.0
    order_size_usd: float = 2.0
    price_decimals: int = 6
    volume_decimals: int = 0
    min_volume: float = 13.0
    min_cost_usd: float = 0.0
    maker_fee_pct: float = 0.25
    stale_price_max_age_sec: float = 60.0
    s1_orphan_after_sec: float = 600.0
    s2_orphan_after_sec: float = 1800.0
    loss_backoff_start: int = 3
    loss_cooldown_start: int = 5
    loss_cooldown_sec: float = 900.0
    backoff_factor: float = 0.5
    backoff_max_multiplier: float = 5.0
    max_consecutive_refreshes: int = 3
    refresh_cooldown_sec: float = 300.0


@dataclass(frozen=True)
class OrderState:
    local_id: int
    side: Side
    role: Role
    price: float
    volume: float
    trade_id: TradeId
    cycle: int
    txid: str = ""
    placed_at: float = 0.0
    entry_price: float = 0.0
    entry_fee: float = 0.0
    entry_filled_at: float = 0.0


@dataclass(frozen=True)
class RecoveryOrder:
    recovery_id: int
    side: Side
    price: float
    volume: float
    trade_id: TradeId
    cycle: int
    entry_price: float
    orphaned_at: float
    txid: str = ""
    reason: str = "stale"


@dataclass(frozen=True)
class CycleRecord:
    trade_id: TradeId
    cycle: int
    entry_price: float
    exit_price: float
    volume: float
    gross_profit: float
    fees: float
    net_profit: float
    entry_time: float = 0.0
    exit_time: float = 0.0
    from_recovery: bool = False


@dataclass(frozen=True)
class PairState:
    market_price: float
    now: float
    orders: tuple[OrderState, ...] = ()
    recovery_orders: tuple[RecoveryOrder, ...] = ()
    completed_cycles: tuple[CycleRecord, ...] = ()
    cycle_a: int = 1
    cycle_b: int = 1
    next_order_id: int = 1
    next_recovery_id: int = 1
    total_profit: float = 0.0
    total_fees: float = 0.0
    today_realized_loss: float = 0.0
    total_round_trips: int = 0
    s2_entered_at: float | None = None
    last_price_update_at: float | None = None
    consecutive_losses_a: int = 0
    consecutive_losses_b: int = 0
    cooldown_until_a: float = 0.0
    cooldown_until_b: float = 0.0
    long_only: bool = False
    short_only: bool = False
    # Anti-chase entry refresh tracking
    consecutive_refreshes_a: int = 0
    consecutive_refreshes_b: int = 0
    last_refresh_direction_a: str | None = None
    last_refresh_direction_b: str | None = None
    refresh_cooldown_until_a: float = 0.0
    refresh_cooldown_until_b: float = 0.0
    # Runtime-adjusted target used for new exits.
    profit_pct_runtime: float = 1.0


# --------------------------- Events ---------------------------


@dataclass(frozen=True)
class PriceTick:
    price: float
    timestamp: float


@dataclass(frozen=True)
class TimerTick:
    timestamp: float


@dataclass(frozen=True)
class FillEvent:
    order_local_id: int
    txid: str
    side: Side
    price: float
    volume: float
    fee: float
    timestamp: float


@dataclass(frozen=True)
class RecoveryFillEvent:
    recovery_id: int
    txid: str
    side: Side
    price: float
    volume: float
    fee: float
    timestamp: float


@dataclass(frozen=True)
class RecoveryCancelEvent:
    recovery_id: int
    txid: str
    timestamp: float


Event = PriceTick | TimerTick | FillEvent | RecoveryFillEvent | RecoveryCancelEvent


# --------------------------- Actions ---------------------------


@dataclass(frozen=True)
class PlaceOrderAction:
    local_id: int
    side: Side
    role: Role
    price: float
    volume: float
    trade_id: TradeId
    cycle: int
    post_only: bool = True
    reason: str = ""


@dataclass(frozen=True)
class CancelOrderAction:
    local_id: int
    txid: str
    reason: str = ""


@dataclass(frozen=True)
class OrphanOrderAction:
    local_id: int
    recovery_id: int
    reason: str


@dataclass(frozen=True)
class BookCycleAction:
    trade_id: TradeId
    cycle: int
    net_profit: float
    gross_profit: float
    fees: float
    from_recovery: bool = False


Action = PlaceOrderAction | CancelOrderAction | OrphanOrderAction | BookCycleAction


# --------------------------- Helpers ---------------------------


def derive_phase(state: PairState) -> PairPhase:
    has_buy_entry = any(o.side == "buy" and o.role == "entry" for o in state.orders)
    has_sell_entry = any(o.side == "sell" and o.role == "entry" for o in state.orders)
    has_buy_exit = any(o.side == "buy" and o.role == "exit" for o in state.orders)
    has_sell_exit = any(o.side == "sell" and o.role == "exit" for o in state.orders)

    if has_buy_exit and has_sell_exit:
        return "S2"
    if has_buy_exit:
        return "S1a"
    if has_sell_exit:
        return "S1b"
    # Covers normal S0 and long/short-only degraded entry states.
    if has_buy_entry or has_sell_entry:
        return "S0"
    return "S0"


def _clear_s2_flag_if_not_s2(state: PairState) -> PairState:
    """Ensure s2_entered_at is only set while phase is S2."""
    if state.s2_entered_at is None:
        return state
    if derive_phase(state) == "S2":
        return state
    return replace(state, s2_entered_at=None)


def _round_price(price: float, cfg: EngineConfig) -> float:
    return round(price, cfg.price_decimals)


def _entry_prices(market_price: float, entry_pct: float, cfg: EngineConfig) -> tuple[float, float]:
    p = entry_pct / 100.0
    buy = _round_price(market_price * (1 - p), cfg)
    sell = _round_price(market_price * (1 + p), cfg)
    return buy, sell


def _exit_price(entry_fill: float, market_price: float, side: Side, cfg: EngineConfig, profit_pct: float) -> float:
    p = profit_pct / 100.0
    e = cfg.entry_pct / 100.0
    if side == "sell":
        return _round_price(max(entry_fill * (1 + p), market_price * (1 + e)), cfg)
    return _round_price(min(entry_fill * (1 - p), market_price * (1 - e)), cfg)


def compute_order_volume(price: float, cfg: EngineConfig, order_size_usd: float) -> float | None:
    if price <= 0:
        return None
    if order_size_usd <= 0:
        return None
    if cfg.min_cost_usd > 0 and order_size_usd < cfg.min_cost_usd:
        return None

    raw = order_size_usd / price
    if cfg.volume_decimals <= 0:
        vol = float(round(raw))
    else:
        vol = round(raw, cfg.volume_decimals)
    # Locked v1 behavior: do not silently raise size to exchange minimum.
    # If target size doesn't satisfy minimum volume/cost, return None and wait.
    if vol < cfg.min_volume:
        return None
    if cfg.min_cost_usd > 0 and vol * price < cfg.min_cost_usd:
        return None
    return vol


def _find_order(state: PairState, local_id: int) -> OrderState | None:
    for o in state.orders:
        if o.local_id == local_id:
            return o
    return None


def _order_by_role(state: PairState, role: Role, side: Side | None = None) -> list[OrderState]:
    out = [o for o in state.orders if o.role == role]
    if side:
        out = [o for o in out if o.side == side]
    return out


def _remove_order(state: PairState, local_id: int) -> tuple[OrderState, ...]:
    return tuple(o for o in state.orders if o.local_id != local_id)


def _bind_order_txid(state: PairState, local_id: int, txid: str) -> PairState:
    patched = []
    for o in state.orders:
        if o.local_id == local_id:
            patched.append(replace(o, txid=txid))
        else:
            patched.append(o)
    return replace(state, orders=tuple(patched))


def _bind_recovery_txid(state: PairState, recovery_id: int, txid: str) -> PairState:
    patched = []
    for r in state.recovery_orders:
        if r.recovery_id == recovery_id:
            patched.append(replace(r, txid=txid))
        else:
            patched.append(r)
    return replace(state, recovery_orders=tuple(patched))


def entry_backoff_multiplier(loss_count: int, cfg: EngineConfig) -> float:
    if loss_count < cfg.loss_backoff_start:
        return 1.0
    mul = 1.0 + cfg.backoff_factor * (loss_count - cfg.loss_backoff_start + 1)
    return min(cfg.backoff_max_multiplier, mul)


def _new_entry_order(
    state: PairState,
    cfg: EngineConfig,
    side: Side,
    trade_id: TradeId,
    cycle: int,
    order_size_usd: float,
    reason: str,
) -> tuple[PairState, OrderState | None, PlaceOrderAction | None]:
    buy_price, sell_price = _entry_prices(state.market_price, cfg.entry_pct, cfg)
    if side == "buy":
        loss_count = state.consecutive_losses_b if trade_id == "B" else state.consecutive_losses_a
        price = _round_price(state.market_price * (1 - (cfg.entry_pct * entry_backoff_multiplier(loss_count, cfg)) / 100.0), cfg)
    else:
        loss_count = state.consecutive_losses_a if trade_id == "A" else state.consecutive_losses_b
        price = _round_price(state.market_price * (1 + (cfg.entry_pct * entry_backoff_multiplier(loss_count, cfg)) / 100.0), cfg)
    # Fallback to standard rounded side price if backoff rounding produced 0.
    if price <= 0:
        price = buy_price if side == "buy" else sell_price

    vol = compute_order_volume(price, cfg, order_size_usd)
    if vol is None:
        return state, None, None
    local_id = state.next_order_id
    order = OrderState(
        local_id=local_id,
        side=side,
        role="entry",
        price=price,
        volume=vol,
        trade_id=trade_id,
        cycle=cycle,
        placed_at=state.now,
    )
    action = PlaceOrderAction(
        local_id=local_id,
        side=side,
        role="entry",
        price=price,
        volume=vol,
        trade_id=trade_id,
        cycle=cycle,
        reason=reason,
    )
    return replace(state, next_order_id=local_id + 1), order, action


def bootstrap_orders(
    state: PairState,
    cfg: EngineConfig,
    order_size_usd: float,
    allow_long_only: bool = True,
    allow_short_only: bool = True,
) -> tuple[PairState, list[Action], tuple[OrderState, ...]]:
    """
    Build fresh S0-style entries for a slot.

    Runtime is responsible for balance checks and may selectively keep one side.
    """
    actions: list[Action] = []
    orders: list[OrderState] = []
    st = state

    st, buy_order, buy_action = _new_entry_order(
        st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=order_size_usd, reason="bootstrap"
    )
    if buy_order and buy_action:
        orders.append(buy_order)
        actions.append(buy_action)

    st, sell_order, sell_action = _new_entry_order(
        st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=order_size_usd, reason="bootstrap"
    )
    if sell_order and sell_action:
        orders.append(sell_order)
        actions.append(sell_action)

    if allow_long_only and buy_order and not sell_order:
        st = replace(st, long_only=True, short_only=False)
    elif allow_short_only and sell_order and not buy_order:
        st = replace(st, short_only=True, long_only=False)
    else:
        st = replace(st, long_only=False, short_only=False)

    return st, actions, tuple(orders)


def check_invariants(state: PairState) -> list[str]:
    """
    Strict invariant checker for the locked v1 state semantics.
    """
    violations: list[str] = []
    phase = derive_phase(state)

    entries = [o for o in state.orders if o.role == "entry"]
    exits = [o for o in state.orders if o.role == "exit"]
    buy_entries = [o for o in entries if o.side == "buy"]
    sell_entries = [o for o in entries if o.side == "sell"]
    buy_exits = [o for o in exits if o.side == "buy"]
    sell_exits = [o for o in exits if o.side == "sell"]

    ids = [o.local_id for o in state.orders]
    if len(ids) != len(set(ids)):
        violations.append("duplicate order local_id")

    if phase == "S0":
        if state.long_only:
            if len(buy_entries) != 1 or sell_entries or exits:
                violations.append("S0 long_only must be exactly one buy entry")
        elif state.short_only:
            if len(sell_entries) != 1 or buy_entries or exits:
                violations.append("S0 short_only must be exactly one sell entry")
        else:
            if len(buy_entries) != 1 or len(sell_entries) != 1 or exits:
                violations.append("S0 must be exactly A sell entry + B buy entry")
    elif phase == "S1a":
        if state.short_only:
            if len(buy_exits) != 1:
                violations.append("S1a short_only must have one buy exit")
        else:
            if len(buy_exits) != 1 or len(buy_entries) != 1 or sell_entries or sell_exits:
                violations.append("S1a must be one buy exit + one buy entry")
    elif phase == "S1b":
        if state.long_only:
            if len(sell_exits) != 1:
                violations.append("S1b long_only must have one sell exit")
        else:
            if len(sell_exits) != 1 or len(sell_entries) != 1 or buy_entries or buy_exits:
                violations.append("S1b must be one sell exit + one sell entry")
    elif phase == "S2":
        if len(buy_exits) != 1 or len(sell_exits) != 1 or entries:
            violations.append("S2 must be one buy exit + one sell exit only")

    if phase != "S2" and state.s2_entered_at is not None:
        violations.append("s2_entered_at must be null outside S2")

    for o in state.orders:
        if o.cycle < 1:
            violations.append("order cycle must be >= 1")
        if o.role == "exit" and o.entry_price <= 0:
            violations.append("exit must carry entry_price")
        if o.volume <= 0:
            violations.append("order volume must be > 0")

    if state.cycle_a < 1 or state.cycle_b < 1:
        violations.append("cycle counters must be >= 1")

    return violations


def to_dict(state: PairState) -> dict:
    return {
        "market_price": state.market_price,
        "now": state.now,
        "orders": [o.__dict__ for o in state.orders],
        "recovery_orders": [r.__dict__ for r in state.recovery_orders],
        "completed_cycles": [c.__dict__ for c in state.completed_cycles],
        "cycle_a": state.cycle_a,
        "cycle_b": state.cycle_b,
        "next_order_id": state.next_order_id,
        "next_recovery_id": state.next_recovery_id,
        "total_profit": state.total_profit,
        "total_fees": state.total_fees,
        "today_realized_loss": state.today_realized_loss,
        "total_round_trips": state.total_round_trips,
        "s2_entered_at": state.s2_entered_at,
        "last_price_update_at": state.last_price_update_at,
        "consecutive_losses_a": state.consecutive_losses_a,
        "consecutive_losses_b": state.consecutive_losses_b,
        "cooldown_until_a": state.cooldown_until_a,
        "cooldown_until_b": state.cooldown_until_b,
        "long_only": state.long_only,
        "short_only": state.short_only,
        "consecutive_refreshes_a": state.consecutive_refreshes_a,
        "consecutive_refreshes_b": state.consecutive_refreshes_b,
        "last_refresh_direction_a": state.last_refresh_direction_a,
        "last_refresh_direction_b": state.last_refresh_direction_b,
        "refresh_cooldown_until_a": state.refresh_cooldown_until_a,
        "refresh_cooldown_until_b": state.refresh_cooldown_until_b,
        "profit_pct_runtime": state.profit_pct_runtime,
    }


def from_dict(data: dict) -> PairState:
    return PairState(
        market_price=float(data.get("market_price", 0.0)),
        now=float(data.get("now", 0.0)),
        orders=tuple(OrderState(**o) for o in data.get("orders", [])),
        recovery_orders=tuple(RecoveryOrder(**r) for r in data.get("recovery_orders", [])),
        completed_cycles=tuple(CycleRecord(**c) for c in data.get("completed_cycles", [])),
        cycle_a=int(data.get("cycle_a", 1)),
        cycle_b=int(data.get("cycle_b", 1)),
        next_order_id=int(data.get("next_order_id", 1)),
        next_recovery_id=int(data.get("next_recovery_id", 1)),
        total_profit=float(data.get("total_profit", 0.0)),
        total_fees=float(data.get("total_fees", 0.0)),
        today_realized_loss=float(data.get("today_realized_loss", 0.0)),
        total_round_trips=int(data.get("total_round_trips", 0)),
        s2_entered_at=data.get("s2_entered_at"),
        last_price_update_at=data.get("last_price_update_at"),
        consecutive_losses_a=int(data.get("consecutive_losses_a", 0)),
        consecutive_losses_b=int(data.get("consecutive_losses_b", 0)),
        cooldown_until_a=float(data.get("cooldown_until_a", 0.0)),
        cooldown_until_b=float(data.get("cooldown_until_b", 0.0)),
        long_only=bool(data.get("long_only", False)),
        short_only=bool(data.get("short_only", False)),
        consecutive_refreshes_a=int(data.get("consecutive_refreshes_a", 0)),
        consecutive_refreshes_b=int(data.get("consecutive_refreshes_b", 0)),
        last_refresh_direction_a=data.get("last_refresh_direction_a"),
        last_refresh_direction_b=data.get("last_refresh_direction_b"),
        refresh_cooldown_until_a=float(data.get("refresh_cooldown_until_a", 0.0)),
        refresh_cooldown_until_b=float(data.get("refresh_cooldown_until_b", 0.0)),
        profit_pct_runtime=float(data.get("profit_pct_runtime", data.get("profit_pct", 1.0))),
    )


# --------------------------- Transition internals ---------------------------


def _book_cycle(
    state: PairState,
    order: OrderState,
    fill_price: float,
    fill_fee: float,
    timestamp: float,
    from_recovery: bool = False,
) -> tuple[PairState, CycleRecord, BookCycleAction]:
    volume = order.volume
    if order.trade_id == "A":
        gross = (order.entry_price - fill_price) * volume
    else:
        gross = (fill_price - order.entry_price) * volume
    fees = order.entry_fee + fill_fee
    net = gross - fees
    rec = CycleRecord(
        trade_id=order.trade_id,
        cycle=order.cycle,
        entry_price=order.entry_price,
        exit_price=fill_price,
        volume=volume,
        gross_profit=gross,
        fees=fees,
        net_profit=net,
        entry_time=order.entry_filled_at,
        exit_time=timestamp,
        from_recovery=from_recovery,
    )
    total_loss = state.today_realized_loss + (abs(net) if net < 0 else 0.0)
    st = replace(
        state,
        total_profit=state.total_profit + net,
        total_fees=state.total_fees + fill_fee,
        today_realized_loss=total_loss,
        total_round_trips=state.total_round_trips + 1,
        completed_cycles=state.completed_cycles + (rec,),
    )
    act = BookCycleAction(
        trade_id=order.trade_id,
        cycle=order.cycle,
        net_profit=net,
        gross_profit=gross,
        fees=fees,
        from_recovery=from_recovery,
    )
    return st, rec, act


def _update_loss_counters(
    state: PairState,
    trade_id: TradeId,
    net_profit: float,
    cfg: EngineConfig,
) -> PairState:
    la = state.consecutive_losses_a
    lb = state.consecutive_losses_b
    ca = state.cooldown_until_a
    cb = state.cooldown_until_b

    if trade_id == "A":
        la = la + 1 if net_profit < 0 else 0
        if la >= cfg.loss_cooldown_start:
            ca = max(ca, state.now + cfg.loss_cooldown_sec)
    else:
        lb = lb + 1 if net_profit < 0 else 0
        if lb >= cfg.loss_cooldown_start:
            cb = max(cb, state.now + cfg.loss_cooldown_sec)

    return replace(state, consecutive_losses_a=la, consecutive_losses_b=lb, cooldown_until_a=ca, cooldown_until_b=cb)


def _place_followup_entry_after_cycle(
    state: PairState,
    cfg: EngineConfig,
    trade_id: TradeId,
    order_size_usd: float,
    reason: str,
) -> tuple[PairState, list[Action]]:
    actions: list[Action] = []
    st = state

    if trade_id == "A":
        # Refresh A sell entry unless long-only or loss cooldown active.
        if st.long_only or st.now < st.cooldown_until_a:
            return st, actions
        st, order, action = _new_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=order_size_usd, reason=reason)
    else:
        # Refresh B buy entry unless short-only or cooldown active.
        if st.short_only or st.now < st.cooldown_until_b:
            return st, actions
        st, order, action = _new_entry_order(st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=order_size_usd, reason=reason)
    if order and action:
        st = replace(st, orders=st.orders + (order,))
        actions.append(action)
    return st, actions


def _orphan_exit(
    state: PairState,
    cfg: EngineConfig,
    order: OrderState,
    reason: str,
    order_size_usd: float,
) -> tuple[PairState, list[Action]]:
    actions: list[Action] = []
    st = state

    recovery_id = st.next_recovery_id
    recovery = RecoveryOrder(
        recovery_id=recovery_id,
        side=order.side,
        price=order.price,
        volume=order.volume,
        trade_id=order.trade_id,
        cycle=order.cycle,
        entry_price=order.entry_price,
        orphaned_at=st.now,
        txid=order.txid,
        reason=reason,
    )
    st = replace(
        st,
        orders=_remove_order(st, order.local_id),
        recovery_orders=st.recovery_orders + (recovery,),
        next_recovery_id=recovery_id + 1,
    )
    actions.append(OrphanOrderAction(local_id=order.local_id, recovery_id=recovery_id, reason=reason))

    # Advance orphaned trade cycle and re-place entry for that side.
    if order.trade_id == "A":
        st = replace(st, cycle_a=st.cycle_a + 1)
        st, entry_actions = _place_followup_entry_after_cycle(st, cfg, trade_id="A", order_size_usd=order_size_usd, reason="orphan_A")
    else:
        st = replace(st, cycle_b=st.cycle_b + 1)
        st, entry_actions = _place_followup_entry_after_cycle(st, cfg, trade_id="B", order_size_usd=order_size_usd, reason="orphan_B")
    actions.extend(entry_actions)
    return st, actions


def _refresh_stale_entries(state: PairState, cfg: EngineConfig, order_size_usd: float) -> tuple[PairState, list[Action]]:
    actions: list[Action] = []
    st = state
    # Refresh one entry per tick max for stability.
    for o in st.orders:
        if o.role != "entry":
            continue
        drift = abs(o.price - st.market_price) / st.market_price * 100.0 if st.market_price > 0 else 0.0
        if drift <= cfg.refresh_pct:
            continue

        is_a = o.trade_id == "A"
        cooldown_until = st.refresh_cooldown_until_a if is_a else st.refresh_cooldown_until_b
        if st.now < cooldown_until:
            continue

        # If cooldown just expired and counter is still at/above threshold,
        # reset so the next refresh is allowed (counts as 1, not re-trigger).
        prev_count_check = st.consecutive_refreshes_a if is_a else st.consecutive_refreshes_b
        if prev_count_check >= cfg.max_consecutive_refreshes and cooldown_until > 0:
            if is_a:
                st = replace(st, consecutive_refreshes_a=0, refresh_cooldown_until_a=0.0)
            else:
                st = replace(st, consecutive_refreshes_b=0, refresh_cooldown_until_b=0.0)

        if o.side == "buy":
            direction = "down" if st.market_price < o.price else "up"
        else:
            direction = "up" if st.market_price > o.price else "down"

        prev_dir = st.last_refresh_direction_a if is_a else st.last_refresh_direction_b
        prev_count = st.consecutive_refreshes_a if is_a else st.consecutive_refreshes_b
        count = prev_count + 1 if direction == prev_dir else 1

        if count >= cfg.max_consecutive_refreshes:
            if is_a:
                st = replace(
                    st,
                    consecutive_refreshes_a=count,
                    last_refresh_direction_a=direction,
                    refresh_cooldown_until_a=st.now + cfg.refresh_cooldown_sec,
                )
            else:
                st = replace(
                    st,
                    consecutive_refreshes_b=count,
                    last_refresh_direction_b=direction,
                    refresh_cooldown_until_b=st.now + cfg.refresh_cooldown_sec,
                )
            break

        # Replace stale entry.
        st = replace(st, orders=_remove_order(st, o.local_id))
        actions.append(CancelOrderAction(local_id=o.local_id, txid=o.txid, reason="stale_entry"))
        st, new_entry, place_action = _new_entry_order(
            st,
            cfg,
            side=o.side,
            trade_id=o.trade_id,
            cycle=o.cycle,
            order_size_usd=order_size_usd,
            reason="refresh_entry",
        )
        if new_entry and place_action:
            st = replace(st, orders=st.orders + (new_entry,))
            actions.append(place_action)

        if is_a:
            st = replace(st, consecutive_refreshes_a=count, last_refresh_direction_a=direction)
        else:
            st = replace(st, consecutive_refreshes_b=count, last_refresh_direction_b=direction)
        break
    return st, actions


def transition(state: PairState, event: Event, cfg: EngineConfig, order_size_usd: float) -> tuple[PairState, list[Action]]:
    """
    Pure reducer for one event.
    """
    actions: list[Action] = []
    st = state

    if isinstance(event, PriceTick):
        st = replace(st, now=event.timestamp, market_price=event.price, last_price_update_at=event.timestamp)
        st, a = _refresh_stale_entries(st, cfg, order_size_usd=order_size_usd)
        actions.extend(a)
        return st, actions

    if isinstance(event, TimerTick):
        st = replace(st, now=event.timestamp)
        phase = derive_phase(st)
        if phase != "S2" and st.s2_entered_at is not None:
            st = replace(st, s2_entered_at=None)

        # S1 stale exit orphaning after fixed timeout if market moved away.
        if phase in ("S1a", "S1b"):
            exit_orders = [o for o in st.orders if o.role == "exit"]
            if exit_orders:
                ex = exit_orders[0]
                age = st.now - (ex.entry_filled_at or ex.placed_at or st.now)
                moved_away = (ex.side == "sell" and st.market_price < ex.price) or (
                    ex.side == "buy" and st.market_price > ex.price
                )
                if age >= cfg.s1_orphan_after_sec and moved_away:
                    st, a = _orphan_exit(st, cfg, ex, reason="s1_timeout", order_size_usd=order_size_usd)
                    actions.extend(a)
                    return st, actions

        # S2 timeout orphaning of the worse leg.
        if phase == "S2":
            if st.s2_entered_at is None:
                st = replace(st, s2_entered_at=st.now)
                return st, actions
            if st.now - st.s2_entered_at >= cfg.s2_orphan_after_sec:
                exits = [o for o in st.orders if o.role == "exit"]
                buy_exit = next((o for o in exits if o.side == "buy"), None)
                sell_exit = next((o for o in exits if o.side == "sell"), None)
                if buy_exit and sell_exit and st.market_price > 0:
                    a_dist = abs(buy_exit.price - st.market_price) / st.market_price
                    b_dist = abs(sell_exit.price - st.market_price) / st.market_price
                    worse = buy_exit if a_dist > b_dist else sell_exit
                    st, a = _orphan_exit(st, cfg, worse, reason="s2_timeout", order_size_usd=order_size_usd)
                    st = replace(st, s2_entered_at=None)
                    actions.extend(a)
                    return st, actions
        elif st.s2_entered_at is not None:
            st = replace(st, s2_entered_at=None)
        return st, actions

    if isinstance(event, FillEvent):
        st = replace(st, now=event.timestamp)
        order = _find_order(st, event.order_local_id)
        if not order:
            return st, actions

        # Remove the filled order from active set.
        st = replace(st, orders=_remove_order(st, order.local_id))

        if order.role == "entry":
            # Entry fee books immediately.
            st = replace(st, total_fees=st.total_fees + event.fee)
            exit_side: Side = "sell" if order.side == "buy" else "buy"
            exit_local = st.next_order_id
            exit_order = OrderState(
                local_id=exit_local,
                side=exit_side,
                role="exit",
                price=_exit_price(event.price, st.market_price, exit_side, cfg, st.profit_pct_runtime or cfg.profit_pct),
                volume=event.volume,
                trade_id=order.trade_id,
                cycle=order.cycle,
                txid="",
                placed_at=event.timestamp,
                entry_price=event.price,
                entry_fee=event.fee,
                entry_filled_at=event.timestamp,
            )
            st = replace(st, orders=st.orders + (exit_order,), next_order_id=exit_local + 1)
            actions.append(
                PlaceOrderAction(
                    local_id=exit_local,
                    side=exit_side,
                    role="exit",
                    price=exit_order.price,
                    volume=exit_order.volume,
                    trade_id=exit_order.trade_id,
                    cycle=exit_order.cycle,
                    reason="entry_fill_exit",
                )
            )
            st = _clear_s2_flag_if_not_s2(st)
            return st, actions

        # Exit filled -> complete cycle.
        st, cycle_record, book_action = _book_cycle(st, order, event.price, event.fee, event.timestamp, from_recovery=False)
        st = _update_loss_counters(st, order.trade_id, cycle_record.net_profit, cfg)
        actions.append(book_action)

        # Advance this trade cycle and refresh its entry.
        if order.trade_id == "A":
            st = replace(st, cycle_a=max(st.cycle_a, order.cycle + 1))
        else:
            st = replace(st, cycle_b=max(st.cycle_b, order.cycle + 1))
        st, follow_actions = _place_followup_entry_after_cycle(
            st, cfg, trade_id=order.trade_id, order_size_usd=order_size_usd, reason="cycle_complete"
        )
        actions.extend(follow_actions)
        st = _clear_s2_flag_if_not_s2(st)
        return st, actions

    if isinstance(event, RecoveryFillEvent):
        st = replace(st, now=event.timestamp)
        rec = next((r for r in st.recovery_orders if r.recovery_id == event.recovery_id), None)
        if not rec:
            return st, actions
        # Remove recovery row.
        st = replace(st, recovery_orders=tuple(r for r in st.recovery_orders if r.recovery_id != rec.recovery_id))
        pseudo_order = OrderState(
            local_id=-1,
            side=rec.side,
            role="exit",
            price=rec.price,
            volume=rec.volume,
            trade_id=rec.trade_id,
            cycle=rec.cycle,
            entry_price=rec.entry_price,
            entry_fee=0.0,
            entry_filled_at=rec.orphaned_at,
        )
        st, cycle_record, book_action = _book_cycle(st, pseudo_order, event.price, event.fee, event.timestamp, from_recovery=True)
        st = _update_loss_counters(st, rec.trade_id, cycle_record.net_profit, cfg)
        actions.append(book_action)
        st = _clear_s2_flag_if_not_s2(st)
        return st, actions

    if isinstance(event, RecoveryCancelEvent):
        st = replace(st, now=event.timestamp)
        st = replace(st, recovery_orders=tuple(r for r in st.recovery_orders if r.recovery_id != event.recovery_id))
        st = _clear_s2_flag_if_not_s2(st)
        return st, actions

    return st, actions


# --------------------------- Runtime patch helpers ---------------------------


def apply_order_txid(state: PairState, local_id: int, txid: str) -> PairState:
    return _bind_order_txid(state, local_id, txid)


def apply_recovery_txid(state: PairState, recovery_id: int, txid: str) -> PairState:
    return _bind_recovery_txid(state, recovery_id, txid)


def add_entry_order(
    state: PairState,
    cfg: EngineConfig,
    side: Side,
    trade_id: TradeId,
    cycle: int,
    order_size_usd: float,
    reason: str = "manual",
) -> tuple[PairState, PlaceOrderAction | None]:
    """
    Public helper for runtime bootstrap/reseed paths.
    """
    st, order, action = _new_entry_order(
        state,
        cfg,
        side=side,
        trade_id=trade_id,
        cycle=cycle,
        order_size_usd=order_size_usd,
        reason=reason,
    )
    if order and action:
        st = replace(st, orders=st.orders + (order,))
        return st, action
    return st, None


def remove_order(state: PairState, local_id: int) -> PairState:
    return replace(state, orders=_remove_order(state, local_id))


def remove_recovery(state: PairState, recovery_id: int) -> PairState:
    return replace(
        state,
        recovery_orders=tuple(r for r in state.recovery_orders if r.recovery_id != recovery_id),
    )


def find_order(state: PairState, local_id: int) -> OrderState | None:
    return _find_order(state, local_id)
