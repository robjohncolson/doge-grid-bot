#!/usr/bin/env python3
"""
backtest_v1.py

Historical replay backtester for the DOGE v1 state machine (`state_machine.py`).

Features:
- Replays OHLC candles from Kraken or CSV
- Simulates entry/exit/recovery fills against intrabar price path
- Runs the same reducer used in production
- Reports PnL, round trips, drawdown, win/loss stats, and invariant health

Examples:
  python3 backtest_v1.py --pair XDGUSD --start 2025-01-01 --end 2026-01-01
  python3 backtest_v1.py --csv data/XDGUSD_1m.csv --interval 1
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable

import config
import kraken_client
import state_machine as sm


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class BacktestStats:
    pair: str
    interval_min: int
    candles: int
    slots: int
    start_ts: int
    end_ts: int
    start_price: float
    end_price: float
    total_profit: float
    total_fees: float
    total_round_trips: int
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: float | None
    max_drawdown: float
    current_drawdown: float
    open_orders: int
    recovery_orders: int
    fills: int
    recovery_fills: int
    actions_place: int
    actions_cancel: int
    actions_orphan: int
    actions_book: int
    invariant_violations: list[str]


def _parse_date_to_ts(value: str | None, *, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    raw = value.strip()
    # Accept unix timestamps directly.
    if raw.isdigit():
        return int(raw)

    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            suffix = "T23:59:59+00:00" if end_of_day else "T00:00:00+00:00"
            dt = datetime.fromisoformat(raw + suffix)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError as e:
        raise ValueError(f"Invalid date/timestamp: {value}") from e


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_ts(raw: str) -> int:
    text = str(raw).strip()
    if text.isdigit():
        return int(text)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def load_candles_csv(path: str, start_ts: int | None = None, end_ts: int | None = None) -> list[Candle]:
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")

        key_map = {k.lower().strip(): k for k in reader.fieldnames}
        ts_key = next((key_map[k] for k in ("time", "timestamp", "ts", "date") if k in key_map), None)
        if not ts_key:
            raise ValueError("CSV must contain one of: time, timestamp, ts, date")

        open_key = next((key_map[k] for k in ("open", "o") if k in key_map), None)
        high_key = next((key_map[k] for k in ("high", "h") if k in key_map), None)
        low_key = next((key_map[k] for k in ("low", "l") if k in key_map), None)
        close_key = next((key_map[k] for k in ("close", "c") if k in key_map), None)
        if not all((open_key, high_key, low_key, close_key)):
            raise ValueError("CSV must contain OHLC columns: open, high, low, close")

        candles: list[Candle] = []
        for row in reader:
            ts = _parse_ts(row[ts_key])
            if start_ts is not None and ts < start_ts:
                continue
            if end_ts is not None and ts > end_ts:
                continue
            o = float(row[open_key])
            h = float(row[high_key])
            l = float(row[low_key])
            c = float(row[close_key])
            if min(o, h, l, c) <= 0:
                continue
            candles.append(Candle(ts=ts, open=o, high=h, low=l, close=c))

    return _dedupe_sort_candles(candles)


def fetch_kraken_candles(
    pair: str,
    interval_min: int,
    start_ts: int,
    end_ts: int | None,
    *,
    max_pages: int = 2000,
    pause_ms: int = 200,
) -> list[Candle]:
    cursor = max(0, int(start_ts))
    seen: set[int] = set()
    out: list[Candle] = []

    for _ in range(max_pages):
        rows, last_cursor = kraken_client.get_ohlc_page(pair=pair, interval=interval_min, since=cursor)
        if not rows:
            break

        added = 0
        for row in rows:
            if len(row) < 5:
                continue
            ts = int(float(row[0]))
            if ts < start_ts:
                continue
            if end_ts is not None and ts > end_ts:
                continue
            if ts in seen:
                continue
            seen.add(ts)

            o = float(row[1])
            h = float(row[2])
            l = float(row[3])
            c = float(row[4])
            if min(o, h, l, c) <= 0:
                continue
            out.append(Candle(ts=ts, open=o, high=h, low=l, close=c))
            added += 1

        if end_ts is not None and out and out[-1].ts >= end_ts:
            break

        if last_cursor is None or last_cursor <= cursor:
            break

        # If Kraken returns no new rows for this cursor, stop to avoid loops.
        if added == 0:
            break

        cursor = int(last_cursor)
        if pause_ms > 0:
            time.sleep(pause_ms / 1000.0)

    return _dedupe_sort_candles(out)


def _dedupe_sort_candles(candles: Iterable[Candle]) -> list[Candle]:
    by_ts: dict[int, Candle] = {}
    for c in candles:
        by_ts[c.ts] = c
    return [by_ts[k] for k in sorted(by_ts.keys())]


def _entry_order_size_usd(base_order_size_usd: float, state: sm.PairState) -> float:
    return max(base_order_size_usd, base_order_size_usd + state.total_profit)


def _maker_fee_usd(price: float, volume: float, maker_fee_pct: float) -> float:
    return price * volume * (maker_fee_pct / 100.0)


def _price_path(candle: Candle, interval_sec: int) -> list[tuple[float, float]]:
    o = candle.open
    h = candle.high
    l = candle.low
    c = candle.close

    # Choose first extreme by proximity to open for a deterministic intrabar path.
    if abs(h - o) < abs(o - l):
        p1, p2 = h, l
    else:
        p1, p2 = l, h

    t0 = float(candle.ts)
    q = max(1.0, float(interval_sec) / 4.0)
    points = [
        (t0, o),
        (t0 + q, p1),
        (t0 + 2.0 * q, p2),
        (t0 + 3.0 * q, c),
    ]

    deduped: list[tuple[float, float]] = []
    last_price = None
    for ts, px in points:
        if last_price is None or px != last_price:
            deduped.append((ts, px))
            last_price = px
    return deduped


def _triggered(side: str, market_price: float, order_price: float) -> bool:
    if side == "buy":
        return market_price <= order_price
    return market_price >= order_price


class BacktestRunner:
    def __init__(
        self,
        candles: list[Candle],
        cfg: sm.EngineConfig,
        *,
        pair: str,
        interval_min: int,
        slots: int,
        base_order_size_usd: float,
        maker_fee_pct: float,
        strict_invariants: bool,
    ) -> None:
        self.candles = candles
        self.cfg = cfg
        self.pair = pair
        self.interval_min = interval_min
        self.interval_sec = max(60, int(interval_min) * 60)
        self.slots = max(1, int(slots))
        self.base_order_size_usd = float(base_order_size_usd)
        self.maker_fee_pct = float(maker_fee_pct)
        self.strict_invariants = strict_invariants

        self.states: dict[int, sm.PairState] = {}
        self.txid_counter = 1

        self.fills = 0
        self.recovery_fills = 0
        self.actions_place = 0
        self.actions_cancel = 0
        self.actions_orphan = 0
        self.actions_book = 0

        self.invariant_violations: list[str] = []
        self.equity_curve: list[tuple[int, float]] = []

    def _next_txid(self) -> str:
        txid = f"BT{self.txid_counter:012d}"
        self.txid_counter += 1
        return txid

    def _apply_actions(self, slot_id: int, state: sm.PairState, actions: list[sm.Action]) -> sm.PairState:
        st = state
        for action in actions:
            if isinstance(action, sm.PlaceOrderAction):
                self.actions_place += 1
                st = sm.apply_order_txid(st, action.local_id, self._next_txid())
            elif isinstance(action, sm.CancelOrderAction):
                self.actions_cancel += 1
            elif isinstance(action, sm.OrphanOrderAction):
                self.actions_orphan += 1
            elif isinstance(action, sm.BookCycleAction):
                self.actions_book += 1
        return st

    def _check_invariants(self, slot_id: int, state: sm.PairState, context: str) -> None:
        violations = sm.check_invariants(state)
        if not violations:
            return
        if self._is_runtime_recoverable_gap(state, violations):
            return
        for v in violations:
            self.invariant_violations.append(f"slot={slot_id} {context}: {v}")
        if self.strict_invariants:
            raise RuntimeError(self.invariant_violations[-1])

    @staticmethod
    def _is_runtime_recoverable_gap(state: sm.PairState, violations: list[str]) -> bool:
        # Match runtime hotfix behavior: tolerate temporary/incomplete S0 during
        # bootstrap/placement gaps instead of treating as fatal.
        if any(v != "S0 must be exactly A sell entry + B buy entry" for v in violations):
            return False
        if sm.derive_phase(state) != "S0":
            return False
        exits = [o for o in state.orders if o.role == "exit"]
        if exits:
            return False
        entries = [o for o in state.orders if o.role == "entry"]
        return len(entries) <= 1

    def _apply_event(self, slot_id: int, event: sm.Event, context: str) -> None:
        st = self.states[slot_id]
        order_size = _entry_order_size_usd(self.base_order_size_usd, st)
        next_state, actions = sm.transition(st, event, self.cfg, order_size_usd=order_size)
        next_state = self._apply_actions(slot_id, next_state, actions)
        self.states[slot_id] = next_state

    def _bootstrap_slot(self, slot_id: int, initial_price: float, initial_ts: int) -> None:
        st = sm.PairState(
            market_price=initial_price,
            now=float(initial_ts),
            profit_pct_runtime=self.cfg.profit_pct,
        )

        actions: list[sm.Action] = []

        order_size = _entry_order_size_usd(self.base_order_size_usd, st)

        st, sell_action = sm.add_entry_order(
            st,
            self.cfg,
            side="sell",
            trade_id="A",
            cycle=st.cycle_a,
            order_size_usd=order_size,
            reason="bt_bootstrap_A",
        )
        if sell_action:
            actions.append(sell_action)

        st, buy_action = sm.add_entry_order(
            st,
            self.cfg,
            side="buy",
            trade_id="B",
            cycle=st.cycle_b,
            order_size_usd=order_size,
            reason="bt_bootstrap_B",
        )
        if buy_action:
            actions.append(buy_action)

        if not actions:
            required = _required_bootstrap_order_size_usd(initial_price, self.cfg)
            raise RuntimeError(
                "Bootstrap produced no orders: "
                f"order_size_usd=${order_size:.4f}, required~${required:.4f} "
                f"(min_volume={self.cfg.min_volume}, min_cost_usd={self.cfg.min_cost_usd}, "
                f"entry_pct={self.cfg.entry_pct}%). "
                "Increase --order-size-usd or use --auto-floor."
            )

        if sell_action and buy_action:
            st = replace(st, long_only=False, short_only=False)
        elif sell_action:
            st = replace(st, long_only=False, short_only=True)
        else:
            st = replace(st, long_only=True, short_only=False)

        st = self._apply_actions(slot_id, st, actions)
        self._check_invariants(slot_id, st, "bootstrap")
        self.states[slot_id] = st

    def _process_fills_at_price(self, slot_id: int, market_price: float, ts: float) -> None:
        # Process one fill at a time to mirror event ordering in production.
        for _ in range(200):
            st = self.states[slot_id]

            next_order = None
            for order in sorted(st.orders, key=lambda x: x.local_id):
                if _triggered(order.side, market_price, order.price):
                    next_order = order
                    break

            if next_order is not None:
                fee = _maker_fee_usd(next_order.price, next_order.volume, self.maker_fee_pct)
                ev = sm.FillEvent(
                    order_local_id=next_order.local_id,
                    txid=next_order.txid or self._next_txid(),
                    side=next_order.side,
                    price=next_order.price,
                    volume=next_order.volume,
                    fee=fee,
                    timestamp=ts,
                )
                self.fills += 1
                self._apply_event(slot_id, ev, "fill")
                continue

            next_recovery = None
            for rec in sorted(st.recovery_orders, key=lambda x: x.recovery_id):
                if _triggered(rec.side, market_price, rec.price):
                    next_recovery = rec
                    break

            if next_recovery is not None:
                fee = _maker_fee_usd(next_recovery.price, next_recovery.volume, self.maker_fee_pct)
                ev = sm.RecoveryFillEvent(
                    recovery_id=next_recovery.recovery_id,
                    txid=next_recovery.txid or self._next_txid(),
                    side=next_recovery.side,
                    price=next_recovery.price,
                    volume=next_recovery.volume,
                    fee=fee,
                    timestamp=ts,
                )
                self.recovery_fills += 1
                self._apply_event(slot_id, ev, "recovery_fill")
                continue

            break

    def run(self) -> BacktestStats:
        first = self.candles[0]
        for slot_id in range(self.slots):
            self._bootstrap_slot(slot_id, initial_price=first.open, initial_ts=first.ts)

        for candle in self.candles:
            for ts, price in _price_path(candle, self.interval_sec):
                for slot_id in range(self.slots):
                    self._apply_event(slot_id, sm.PriceTick(price=price, timestamp=ts), "price_tick")
                    self._process_fills_at_price(slot_id, price, ts + 0.0001)
                    self._apply_event(slot_id, sm.TimerTick(timestamp=ts), "timer_tick")
                    self._check_invariants(slot_id, self.states[slot_id], f"point@{ts:.3f}")

            total_profit = sum(st.total_profit for st in self.states.values())
            self.equity_curve.append((candle.ts, total_profit))

        end_state_list = list(self.states.values())
        total_profit = sum(st.total_profit for st in end_state_list)
        total_fees = sum(st.total_fees for st in end_state_list)
        total_round_trips = sum(st.total_round_trips for st in end_state_list)

        cycles = []
        for st in end_state_list:
            cycles.extend(list(st.completed_cycles))
        wins = sum(1 for c in cycles if c.net_profit > 0)
        losses = sum(1 for c in cycles if c.net_profit < 0)
        win_rate = (wins / len(cycles) * 100.0) if cycles else 0.0

        gross_wins = sum(c.net_profit for c in cycles if c.net_profit > 0)
        gross_losses = abs(sum(c.net_profit for c in cycles if c.net_profit < 0))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else None

        max_dd = 0.0
        cur_dd = 0.0
        peak = -math.inf
        for _, eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd
            cur_dd = dd

        open_orders = sum(len(st.orders) for st in end_state_list)
        recovery_orders = sum(len(st.recovery_orders) for st in end_state_list)

        return BacktestStats(
            pair=self.pair,
            interval_min=self.interval_min,
            candles=len(self.candles),
            slots=self.slots,
            start_ts=self.candles[0].ts,
            end_ts=self.candles[-1].ts,
            start_price=self.candles[0].open,
            end_price=self.candles[-1].close,
            total_profit=total_profit,
            total_fees=total_fees,
            total_round_trips=total_round_trips,
            wins=wins,
            losses=losses,
            win_rate_pct=win_rate,
            profit_factor=profit_factor,
            max_drawdown=max_dd,
            current_drawdown=cur_dd,
            open_orders=open_orders,
            recovery_orders=recovery_orders,
            fills=self.fills,
            recovery_fills=self.recovery_fills,
            actions_place=self.actions_place,
            actions_cancel=self.actions_cancel,
            actions_orphan=self.actions_orphan,
            actions_book=self.actions_book,
            invariant_violations=self.invariant_violations,
        )


def _print_summary(stats: BacktestStats) -> None:
    print("=" * 64)
    print("V1 BACKTEST SUMMARY")
    print("=" * 64)
    print(f"pair:              {stats.pair}")
    print(f"interval:          {stats.interval_min}m")
    print(f"slots:             {stats.slots}")
    print(f"candles:           {stats.candles}")
    print(f"start:             {_iso_utc(stats.start_ts)}")
    print(f"end:               {_iso_utc(stats.end_ts)}")
    print(f"start_price:       ${stats.start_price:.6f}")
    print(f"end_price:         ${stats.end_price:.6f}")
    print("-")
    print(f"total_profit:      ${stats.total_profit:.6f}")
    print(f"total_fees:        ${stats.total_fees:.6f}")
    print(f"round_trips:       {stats.total_round_trips}")
    print(f"wins/losses:       {stats.wins}/{stats.losses}")
    print(f"win_rate:          {stats.win_rate_pct:.2f}%")
    if stats.profit_factor is None:
        print("profit_factor:     n/a")
    else:
        print(f"profit_factor:     {stats.profit_factor:.4f}")
    print(f"max_drawdown:      ${stats.max_drawdown:.6f}")
    print(f"current_drawdown:  ${stats.current_drawdown:.6f}")
    print("-")
    print(f"fills:             {stats.fills}")
    print(f"recovery_fills:    {stats.recovery_fills}")
    print(f"actions/place:     {stats.actions_place}")
    print(f"actions/cancel:    {stats.actions_cancel}")
    print(f"actions/orphan:    {stats.actions_orphan}")
    print(f"actions/book:      {stats.actions_book}")
    print(f"open_orders_end:   {stats.open_orders}")
    print(f"recovery_end:      {stats.recovery_orders}")
    print(f"invariant_issues:  {len(stats.invariant_violations)}")
    if stats.invariant_violations:
        print("first_violation:   " + stats.invariant_violations[0])


def _build_engine_config(args: argparse.Namespace, constraints: dict) -> sm.EngineConfig:
    return sm.EngineConfig(
        entry_pct=float(args.entry_pct),
        profit_pct=float(args.profit_pct),
        refresh_pct=float(args.refresh_pct),
        order_size_usd=float(args.order_size_usd),
        price_decimals=int(constraints.get("price_decimals", 6)),
        volume_decimals=int(constraints.get("volume_decimals", 0)),
        min_volume=float(constraints.get("min_volume", 13.0)),
        min_cost_usd=float(constraints.get("min_cost_usd", 0.0)),
        maker_fee_pct=float(args.maker_fee_pct),
        stale_price_max_age_sec=float(config.STALE_PRICE_MAX_AGE_SEC),
        s1_orphan_after_sec=float(config.S1_ORPHAN_AFTER_SEC),
        s2_orphan_after_sec=float(config.S2_ORPHAN_AFTER_SEC),
        loss_backoff_start=int(config.LOSS_BACKOFF_START),
        loss_cooldown_start=int(config.LOSS_COOLDOWN_START),
        loss_cooldown_sec=float(config.LOSS_COOLDOWN_SEC),
        backoff_factor=float(config.ENTRY_BACKOFF_FACTOR),
        backoff_max_multiplier=float(config.ENTRY_BACKOFF_MAX_MULTIPLIER),
    )


def _default_start_ts() -> int:
    return int(time.time()) - 365 * 24 * 3600


def _required_bootstrap_order_size_usd(market_price: float, cfg: sm.EngineConfig) -> float:
    """
    Estimate the minimum order_size_usd required to place both bootstrap entries.

    We use the stricter side (sell entry price, which is above market) and account
    for round-to-nearest volume precision used by compute_order_volume().
    """
    if market_price <= 0:
        return max(0.0, float(cfg.min_cost_usd))

    def can_bootstrap_two_sided(order_size_usd: float) -> bool:
        st = sm.PairState(market_price=market_price, now=0.0, profit_pct_runtime=cfg.profit_pct)
        st, a = sm.add_entry_order(
            st,
            cfg,
            side="sell",
            trade_id="A",
            cycle=st.cycle_a,
            order_size_usd=order_size_usd,
            reason="probe",
        )
        st, b = sm.add_entry_order(
            st,
            cfg,
            side="buy",
            trade_id="B",
            cycle=st.cycle_b,
            order_size_usd=order_size_usd,
            reason="probe",
        )
        return a is not None and b is not None

    low = 0.0
    high = max(
        float(cfg.min_cost_usd),
        float(cfg.min_volume) * market_price * (1.0 + cfg.entry_pct / 100.0),
        1e-9,
    )

    for _ in range(50):
        if can_bootstrap_two_sided(high):
            break
        high *= 2.0
    else:
        return high

    for _ in range(70):
        mid = (low + high) / 2.0
        if can_bootstrap_two_sided(mid):
            high = mid
        else:
            low = mid

    # Keep a tiny safety margin to avoid floating-point edge ties.
    return round(high * (1.0 + 1e-8), 10)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Historical replay backtest for state_machine v1")
    p.add_argument("--pair", default=config.PAIR, help="Kraken pair, e.g. XDGUSD")
    p.add_argument("--interval", type=int, default=15, help="Candle interval in minutes")
    p.add_argument("--start", default=None, help="Start date/time (YYYY-MM-DD, ISO8601, or unix ts)")
    p.add_argument("--end", default=None, help="End date/time (YYYY-MM-DD, ISO8601, or unix ts)")
    p.add_argument("--csv", default="", help="Optional OHLC CSV path instead of Kraken fetch")
    p.add_argument("--max-pages", type=int, default=2000, help="Max Kraken OHLC pages")
    p.add_argument("--pause-ms", type=int, default=200, help="Pause between Kraken OHLC page requests")

    p.add_argument("--slots", type=int, default=1, help="Independent slot count")
    p.add_argument("--order-size-usd", type=float, default=config.ORDER_SIZE_USD)
    p.add_argument("--entry-pct", type=float, default=config.PAIR_ENTRY_PCT)
    p.add_argument("--profit-pct", type=float, default=config.PAIR_PROFIT_PCT)
    p.add_argument("--refresh-pct", type=float, default=config.PAIR_REFRESH_PCT)
    p.add_argument("--maker-fee-pct", type=float, default=config.MAKER_FEE_PCT)
    p.add_argument("--price-decimals", type=int, default=6, help="Fallback price precision if Kraken constraints unavailable")
    p.add_argument("--volume-decimals", type=int, default=0, help="Fallback volume precision if Kraken constraints unavailable")
    p.add_argument("--min-volume", type=float, default=13.0, help="Fallback Kraken minimum base volume")
    p.add_argument("--min-cost-usd", type=float, default=0.0, help="Fallback Kraken minimum notional")
    p.add_argument("--auto-floor", action="store_true", default=False, help="Auto-raise order size to bootstrap minimum if needed")

    p.add_argument("--strict-invariants", action="store_true", default=False, help="Stop at first invariant violation")
    p.add_argument("--json-out", default="", help="Optional JSON summary output path")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    start_ts = _parse_date_to_ts(args.start, end_of_day=False) or _default_start_ts()
    end_ts = _parse_date_to_ts(args.end, end_of_day=True)

    if end_ts is not None and end_ts <= start_ts:
        raise SystemExit("--end must be after --start")

    constraints = {
        "pair": args.pair,
        "price_decimals": int(args.price_decimals),
        "volume_decimals": int(args.volume_decimals),
        "min_volume": float(args.min_volume),
        "min_cost_usd": float(args.min_cost_usd),
    }
    try:
        remote_constraints = kraken_client.get_pair_constraints(args.pair)
        constraints.update(remote_constraints)
    except Exception as e:
        print(f"warning: could not fetch Kraken constraints for {args.pair}: {e}")
        print("warning: using fallback constraints from CLI/defaults")
    cfg = _build_engine_config(args, constraints)

    if args.csv:
        candles = load_candles_csv(args.csv, start_ts=start_ts, end_ts=end_ts)
    else:
        candles = fetch_kraken_candles(
            pair=args.pair,
            interval_min=max(1, int(args.interval)),
            start_ts=start_ts,
            end_ts=end_ts,
            max_pages=max(1, int(args.max_pages)),
            pause_ms=max(0, int(args.pause_ms)),
        )

    if len(candles) < 2:
        if args.csv:
            raise SystemExit("Need at least 2 candles in CSV for backtest")
        raise SystemExit(
            "Need at least 2 candles for backtest. "
            "Requested range may be outside Kraken OHLC retention for this interval. "
            "Try a more recent --start, a larger --interval, or use --csv."
        )

    effective_order_size_usd = float(args.order_size_usd)
    required_bootstrap_usd = _required_bootstrap_order_size_usd(candles[0].open, cfg)
    if effective_order_size_usd + 1e-12 < required_bootstrap_usd:
        if args.auto_floor:
            print(
                "info: auto-floor enabled; "
                f"order_size_usd raised from ${effective_order_size_usd:.4f} "
                f"to ${required_bootstrap_usd:.4f} "
                f"(first_price=${candles[0].open:.6f})"
            )
            effective_order_size_usd = required_bootstrap_usd
        else:
            raise SystemExit(
                "order-size too small for bootstrap: "
                f"--order-size-usd=${effective_order_size_usd:.4f}, "
                f"required~${required_bootstrap_usd:.4f} "
                f"(first_price=${candles[0].open:.6f}, "
                f"min_volume={cfg.min_volume}, min_cost_usd={cfg.min_cost_usd}). "
                "Increase --order-size-usd or pass --auto-floor."
            )

    runner = BacktestRunner(
        candles=candles,
        cfg=cfg,
        pair=args.pair,
        interval_min=max(1, int(args.interval)),
        slots=max(1, int(args.slots)),
        base_order_size_usd=effective_order_size_usd,
        maker_fee_pct=float(args.maker_fee_pct),
        strict_invariants=bool(args.strict_invariants),
    )

    try:
        stats = runner.run()
    except RuntimeError as e:
        raise SystemExit(str(e)) from e
    _print_summary(stats)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(stats.__dict__, f, indent=2)
        print(f"\nWrote JSON summary: {args.json_out}")


if __name__ == "__main__":
    main()
