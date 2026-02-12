"""
DOGE Bot v1 runtime.

Ground-up DOGE/USD slot-based pair state machine runtime:
- DOGE-only (Kraken XDGUSD)
- Supabase as single source of truth
- reducer-driven state transitions
- simplified orphaning (S1 timeout + S2 timeout)
- Telegram commands + dashboard controls
"""

from __future__ import annotations

from collections import deque
import json
import logging
import signal
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import ceil, isfinite
from socketserver import ThreadingMixIn
from statistics import median
from typing import Any

import config
import dashboard
import kraken_client
import notifier
import state_machine as sm
import supabase_store


logger = logging.getLogger(__name__)


def setup_logging() -> None:
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )


def _now() -> float:
    return time.time()


def _usd_balance(balance: dict) -> float:
    for key in ("ZUSD", "USD"):
        if key in balance:
            try:
                return float(balance.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _doge_balance(balance: dict) -> float:
    for key in ("XXDG", "XDG", "DOGE"):
        if key in balance:
            try:
                return float(balance.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


@dataclass
class SlotRuntime:
    slot_id: int
    state: sm.PairState


class BotRuntime:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.started_at = _now()
        self.running = True

        self.mode = "INIT"  # INIT | RUNNING | PAUSED | HALTED
        self.pause_reason = ""

        self.pair = config.PAIR
        self.pair_display = config.PAIR_DISPLAY
        self.entry_pct = float(config.PAIR_ENTRY_PCT)
        self.profit_pct = float(config.PAIR_PROFIT_PCT)

        self.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        self.maker_fee_pct = float(config.MAKER_FEE_PCT)
        self.taker_fee_pct = float(config.MAKER_FEE_PCT)

        self.slots: dict[int, SlotRuntime] = {}
        self.next_slot_id = 1

        self.next_event_id = 1
        self.seen_fill_txids: set[str] = set()

        self.price_history: list[tuple[float, float]] = []
        self.last_price = 0.0
        self.last_price_ts = 0.0

        self.consecutive_api_errors = 0
        self.enforce_loop_budget = False
        self.loop_private_calls = 0
        self._loop_balance_cache: dict | None = None

        # Kraken-first capacity telemetry (pair-filtered open orders).
        self._kraken_open_orders_current: int | None = None
        self._kraken_open_orders_ts = 0.0
        self._open_order_drift_over_threshold_since: float | None = None
        self._open_order_drift_last_alert_at = 0.0
        self._open_order_drift_alert_active = False
        self._open_order_drift_alert_active_since: float | None = None

        # Rolling 24h fill/partial telemetry.
        self._partial_fill_open_events: deque[float] = deque()
        self._partial_fill_cancel_events: deque[float] = deque()
        self._fill_durations_1d: deque[tuple[float, float]] = deque()
        self._partial_open_seen_txids: set[str] = set()

    # ------------------ Config/State ------------------

    def _engine_cfg(self, slot: SlotRuntime) -> sm.EngineConfig:
        return sm.EngineConfig(
            entry_pct=self.entry_pct,
            profit_pct=self.profit_pct,
            refresh_pct=config.PAIR_REFRESH_PCT,
            order_size_usd=self._slot_order_size_usd(slot),
            price_decimals=int(self.constraints.get("price_decimals", 6)),
            volume_decimals=int(self.constraints.get("volume_decimals", 0)),
            min_volume=float(self.constraints.get("min_volume", 13.0)),
            min_cost_usd=float(self.constraints.get("min_cost_usd", 0.0)),
            maker_fee_pct=float(self.maker_fee_pct),
            stale_price_max_age_sec=float(config.STALE_PRICE_MAX_AGE_SEC),
            s1_orphan_after_sec=float(config.S1_ORPHAN_AFTER_SEC),
            s2_orphan_after_sec=float(config.S2_ORPHAN_AFTER_SEC),
            loss_backoff_start=int(config.LOSS_BACKOFF_START),
            loss_cooldown_start=int(config.LOSS_COOLDOWN_START),
            loss_cooldown_sec=float(config.LOSS_COOLDOWN_SEC),
            backoff_factor=float(config.ENTRY_BACKOFF_FACTOR),
            backoff_max_multiplier=float(config.ENTRY_BACKOFF_MAX_MULTIPLIER),
        )

    def _slot_order_size_usd(self, slot: SlotRuntime) -> float:
        # Independent compounding per slot.
        return max(float(config.ORDER_SIZE_USD), float(config.ORDER_SIZE_USD) + slot.state.total_profit)

    def _minimum_bootstrap_requirements(self, market_price: float) -> tuple[float, float]:
        min_vol = float(self.constraints.get("min_volume", 13.0))
        min_cost = float(self.constraints.get("min_cost_usd", 0.0))
        if min_cost <= 0 and market_price > 0:
            min_cost = min_vol * market_price
        return min_vol, min_cost

    def _order_matches_runtime_pair(self, row: dict) -> bool:
        # OpenOrders rows typically carry pair under descr.pair.
        descr = row.get("descr", {}) if isinstance(row, dict) else {}
        pair_name = ""
        if isinstance(descr, dict):
            pair_name = str(descr.get("pair") or descr.get("pairname") or "").upper()
        if not pair_name and isinstance(row, dict):
            pair_name = str(row.get("pair") or "").upper()
        if not pair_name:
            # If pair metadata is missing, count conservatively.
            return True

        target = self.pair.upper()
        target_norm = target.replace("/", "")
        pair_norm = pair_name.replace("/", "")
        alt = target.replace("USD", "/USD")
        return pair_name in {target, alt} or pair_norm == target_norm

    def _count_pair_open_orders(self, open_orders: dict) -> int:
        if not isinstance(open_orders, dict):
            return 0
        count = 0
        for row in open_orders.values():
            if not isinstance(row, dict) or self._order_matches_runtime_pair(row):
                count += 1
        return count

    def _trim_rolling_telemetry(self, now: float | None = None) -> None:
        now = now or _now()
        cutoff = now - 86400.0
        while self._partial_fill_open_events and self._partial_fill_open_events[0] < cutoff:
            self._partial_fill_open_events.popleft()
        while self._partial_fill_cancel_events and self._partial_fill_cancel_events[0] < cutoff:
            self._partial_fill_cancel_events.popleft()
        while self._fill_durations_1d and self._fill_durations_1d[0][0] < cutoff:
            self._fill_durations_1d.popleft()

    def _record_partial_fill_open(self, ts: float | None = None) -> None:
        ts = ts or _now()
        self._partial_fill_open_events.append(ts)
        self._trim_rolling_telemetry(ts)

    def _record_partial_fill_cancel(self, ts: float | None = None) -> None:
        ts = ts or _now()
        self._partial_fill_cancel_events.append(ts)
        self._trim_rolling_telemetry(ts)

    def _record_fill_duration(self, duration_sec: float, ts: float | None = None) -> None:
        ts = ts or _now()
        self._fill_durations_1d.append((ts, max(0.0, float(duration_sec))))
        self._trim_rolling_telemetry(ts)

    def _fill_duration_stats_1d(self) -> tuple[float | None, float | None]:
        vals = [d for _, d in self._fill_durations_1d if d >= 0]
        if not vals:
            return None, None
        med = float(median(vals))
        ordered = sorted(vals)
        idx = max(0, min(len(ordered) - 1, ceil(0.95 * len(ordered)) - 1))
        return med, float(ordered[idx])

    def _internal_open_order_count(self) -> int:
        return sum(len(slot.state.orders) + len(slot.state.recovery_orders) for slot in self.slots.values())

    def _open_order_drift_is_persistent(
        self,
        *,
        now: float,
        internal_open_orders_current: int,
        kraken_open_orders_current: int | None,
    ) -> bool:
        if kraken_open_orders_current is None:
            return False
        threshold = max(1, int(config.OPEN_ORDER_DRIFT_ALERT_THRESHOLD))
        persist_sec = max(0.0, float(config.OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC))
        telemetry_max_age = max(60.0, float(config.POLL_INTERVAL_SECONDS) * 3.0)
        if now - self._kraken_open_orders_ts > telemetry_max_age:
            return False
        drift = int(kraken_open_orders_current) - internal_open_orders_current
        if abs(drift) < threshold or self._open_order_drift_over_threshold_since is None:
            return False
        return (now - self._open_order_drift_over_threshold_since) >= persist_sec

    def _maybe_alert_persistent_open_order_drift(self, now: float | None = None) -> None:
        now = now or _now()
        kraken_open_orders_current = self._kraken_open_orders_current
        if kraken_open_orders_current is None:
            return
        telemetry_max_age = max(60.0, float(config.POLL_INTERVAL_SECONDS) * 3.0)
        if now - self._kraken_open_orders_ts > telemetry_max_age:
            self._open_order_drift_over_threshold_since = None
            return

        internal_open_orders_current = self._internal_open_order_count()
        drift = int(kraken_open_orders_current) - internal_open_orders_current
        threshold = max(1, int(config.OPEN_ORDER_DRIFT_ALERT_THRESHOLD))

        if abs(drift) < threshold:
            if self._open_order_drift_alert_active:
                active_since = self._open_order_drift_alert_active_since or now
                active_duration_sec = int(max(0.0, now - active_since))
                notifier._send_message(
                    "<b>Open-order drift recovered</b>\n"
                    f"pair: {self.pair_display}\n"
                    f"kraken_open_orders: {int(kraken_open_orders_current)}\n"
                    f"internal_open_orders: {internal_open_orders_current}\n"
                    f"drift: {drift:+d}\n"
                    f"active_duration: {active_duration_sec}s"
                )
            self._open_order_drift_alert_active = False
            self._open_order_drift_alert_active_since = None
            self._open_order_drift_over_threshold_since = None
            return

        if self._open_order_drift_over_threshold_since is None:
            self._open_order_drift_over_threshold_since = now
            return

        if not self._open_order_drift_is_persistent(
            now=now,
            internal_open_orders_current=internal_open_orders_current,
            kraken_open_orders_current=kraken_open_orders_current,
        ):
            return

        cooldown_sec = max(0.0, float(config.OPEN_ORDER_DRIFT_ALERT_COOLDOWN_SEC))
        if now - self._open_order_drift_last_alert_at < cooldown_sec:
            return

        self._open_order_drift_last_alert_at = now
        if not self._open_order_drift_alert_active:
            self._open_order_drift_alert_active_since = now
        self._open_order_drift_alert_active = True
        persist_sec = int(max(0.0, float(config.OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC)))
        notifier._send_message(
            "<b>Open-order drift persistent</b>\n"
            f"pair: {self.pair_display}\n"
            f"kraken_open_orders: {int(kraken_open_orders_current)}\n"
            f"internal_open_orders: {internal_open_orders_current}\n"
            f"drift: {drift:+d}\n"
            f"persistence: >= {persist_sec}s"
        )

    def _global_snapshot(self) -> dict:
        return {
            "version": "doge-v1",
            "saved_at": _now(),
            "mode": self.mode,
            "pause_reason": self.pause_reason,
            "entry_pct": self.entry_pct,
            "profit_pct": self.profit_pct,
            "pair": self.pair,
            "pair_display": self.pair_display,
            "next_slot_id": self.next_slot_id,
            "next_event_id": self.next_event_id,
            "seen_fill_txids": list(self.seen_fill_txids)[-5000:],
            "last_price": self.last_price,
            "last_price_ts": self.last_price_ts,
            "constraints": self.constraints,
            "maker_fee_pct": self.maker_fee_pct,
            "taker_fee_pct": self.taker_fee_pct,
            "slots": {str(sid): sm.to_dict(slot.state) for sid, slot in self.slots.items()},
        }

    def _save_snapshot(self) -> None:
        supabase_store.save_state(self._global_snapshot(), pair="__v1__")

    def _load_snapshot(self) -> None:
        snap = supabase_store.load_state(pair="__v1__") or {}
        if snap:
            self.mode = snap.get("mode", "INIT")
            self.pause_reason = snap.get("pause_reason", "")
            self.entry_pct = float(snap.get("entry_pct", self.entry_pct))
            self.profit_pct = float(snap.get("profit_pct", self.profit_pct))
            self.next_slot_id = int(snap.get("next_slot_id", 1))
            self.next_event_id = int(snap.get("next_event_id", 1))
            self.seen_fill_txids = set(snap.get("seen_fill_txids", []))
            self.last_price = float(snap.get("last_price", 0.0))
            self.last_price_ts = float(snap.get("last_price_ts", 0.0))

            self.constraints = snap.get("constraints", self.constraints) or self.constraints
            self.maker_fee_pct = float(snap.get("maker_fee_pct", self.maker_fee_pct))
            self.taker_fee_pct = float(snap.get("taker_fee_pct", self.taker_fee_pct))

            self.slots = {}
            for sid_text, raw_state in (snap.get("slots", {}) or {}).items():
                sid = int(sid_text)
                self.slots[sid] = SlotRuntime(slot_id=sid, state=sm.from_dict(raw_state))

        # Startup rebase: if snapshot lagged behind queued event writes before a
        # restart, avoid duplicate-key collisions on bot_events(event_id).
        db_max_event_id = supabase_store.load_max_event_id()
        if db_max_event_id >= self.next_event_id:
            old = self.next_event_id
            self.next_event_id = db_max_event_id + 1
            logger.info("Rebased next_event_id from %d to %d using Supabase max", old, self.next_event_id)

    def _log_event(
        self,
        slot_id: int,
        from_state: str,
        to_state: str,
        event_type: str,
        details: dict,
    ) -> None:
        event_id = self.next_event_id
        self.next_event_id += 1
        row = {
            "event_id": event_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pair": self.pair,
            "slot_id": slot_id,
            "from_state": from_state,
            "to_state": to_state,
            "event_type": event_type,
            "details": details,
        }
        supabase_store.save_event(row)

    # ------------------ Loop API Budget ------------------

    def begin_loop(self) -> None:
        self.enforce_loop_budget = True
        self.loop_private_calls = 0
        self._loop_balance_cache = None

    def end_loop(self) -> None:
        self.enforce_loop_budget = False
        self._loop_balance_cache = None

    def _consume_private_budget(self, units: int, reason: str) -> bool:
        if units <= 0:
            return True
        if not self.enforce_loop_budget:
            return True
        limit = max(1, int(config.MAX_API_CALLS_PER_LOOP))
        if self.loop_private_calls + units > limit:
            logger.warning(
                "Loop private API budget exhausted (%d/%d), skipping %s",
                self.loop_private_calls,
                limit,
                reason,
            )
            return False
        self.loop_private_calls += units
        return True

    def _get_open_orders(self) -> dict:
        if not self._consume_private_budget(1, "get_open_orders"):
            return {}
        out = kraken_client.get_open_orders()
        self._kraken_open_orders_current = self._count_pair_open_orders(out)
        self._kraken_open_orders_ts = _now()
        return out

    def _get_trades_history(self, start: float | None = None) -> dict:
        if not self._consume_private_budget(1, "get_trades_history"):
            return {}
        return kraken_client.get_trades_history(start=start)

    def _query_orders_batched(self, txids: list[str], batch_size: int = 50) -> dict:
        if not txids:
            return {}
        if not self.enforce_loop_budget:
            return kraken_client.query_orders_batched(txids, batch_size=batch_size)

        limit = max(1, int(config.MAX_API_CALLS_PER_LOOP))
        remaining = limit - self.loop_private_calls
        if remaining <= 0:
            logger.warning("Loop private API budget exhausted, skipping query_orders")
            return {}
        max_txids = remaining * batch_size
        bounded = txids[:max_txids]
        units = ceil(len(bounded) / batch_size)
        self.loop_private_calls += units
        return kraken_client.query_orders_batched(bounded, batch_size=batch_size)

    def _place_order(self, *, side: str, volume: float, price: float, userref: int) -> str | None:
        if not self._consume_private_budget(1, "place_order"):
            return None
        return kraken_client.place_order(
            side=side,
            volume=volume,
            price=price,
            pair=self.pair,
            ordertype="limit",
            post_only=True,
            userref=userref,
        )

    def _cancel_order(self, txid: str) -> bool:
        if not txid:
            return False
        if not self._consume_private_budget(1, "cancel_order"):
            return False
        return kraken_client.cancel_order(txid)

    def _refresh_open_order_telemetry(self) -> None:
        try:
            self._get_open_orders()
            self._maybe_alert_persistent_open_order_drift()
        except Exception as e:
            logger.debug("Open-order telemetry refresh failed: %s", e)

    # ------------------ Lifecycle ------------------

    def initialize(self) -> None:
        logger.info("============================================================")
        logger.info("  DOGE STATE-MACHINE BOT v1")
        logger.info("============================================================")

        supabase_store.start_writer_thread()

        # Fetch latest exchange constraints + fees.
        self.constraints = kraken_client.get_pair_constraints(self.pair)
        self.maker_fee_pct, self.taker_fee_pct = kraken_client.get_fee_rates(self.pair)

        # Restore runtime snapshot.
        self._load_snapshot()

        # Ensure at least slot 0 exists.
        if not self.slots:
            ts = _now()
            self.slots[0] = SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.0,
                    now=ts,
                    profit_pct_runtime=self.profit_pct,
                ),
            )
            self.next_slot_id = 1

        # Get initial market price.
        self._refresh_price(strict=True)

        # Push price into all slots.
        for sid, slot in self.slots.items():
            self.slots[sid].state = replace(
                slot.state,
                market_price=self.last_price,
                now=self.last_price_ts,
                last_price_update_at=self.last_price_ts,
                profit_pct_runtime=self.profit_pct,
            )

        # Reconcile + exactly-once replay for missed fills after restart.
        open_orders = self._reconcile_open_orders()
        self._replay_missed_fills(open_orders)

        # Ensure each slot has active entries/exits after reconciliation/replay.
        for sid in sorted(self.slots.keys()):
            self._ensure_slot_bootstrapped(sid)

        if self.mode not in ("PAUSED", "HALTED"):
            self.mode = "RUNNING"

        self._save_snapshot()
        notifier._send_message(
            f"<b>DOGE v1 started</b>\n"
            f"pair: {self.pair_display}\n"
            f"slots: {len(self.slots)}\n"
            f"maker fee: {self.maker_fee_pct:.3f}%\n"
            f"min vol: {self.constraints.get('min_volume')}"
        )

    def shutdown(self, reason: str) -> None:
        with self.lock:
            self.running = False
            self.mode = "HALTED"
            self.pause_reason = reason
            self._save_snapshot()
        notifier._send_message(f"<b>DOGE v1 stopped</b>\nreason: {reason}")

    # ------------------ Pause/Halt ------------------

    def pause(self, reason: str) -> None:
        if self.mode != "PAUSED":
            self.mode = "PAUSED"
            self.pause_reason = reason
            notifier.notify_risk_event("pause", reason, self.pair_display)

    def resume(self) -> None:
        self.mode = "RUNNING"
        self.pause_reason = ""
        self.consecutive_api_errors = 0
        notifier.notify_risk_event("resume", "Resumed by operator", self.pair_display)

    def halt(self, reason: str) -> None:
        self.mode = "HALTED"
        self.pause_reason = reason
        notifier.notify_error(f"HALTED: {reason}")

    # ------------------ Market / Stats ------------------

    def _refresh_price(self, strict: bool = False) -> None:
        try:
            px = float(kraken_client.get_price(pair=self.pair))
            ts = _now()
            self.last_price = px
            self.last_price_ts = ts
            self.price_history.append((ts, px))
            self.price_history = [(t, p) for (t, p) in self.price_history if ts - t <= 86400]
            supabase_store.queue_price_point(ts, px, pair=self.pair)
            self.consecutive_api_errors = 0
        except Exception as e:
            self.consecutive_api_errors += 1
            logger.warning("Price refresh failed (%d): %s", self.consecutive_api_errors, e)
            if strict:
                raise
            if self.consecutive_api_errors >= config.MAX_CONSECUTIVE_ERRORS:
                self.pause(f"{self.consecutive_api_errors} consecutive API errors")

    def _price_age_sec(self) -> float:
        if self.last_price_ts <= 0:
            return 1e9
        return max(0.0, _now() - self.last_price_ts)

    def _volatility_profit_pct(self) -> float:
        # Volatility-aware runtime target from rolling absolute returns.
        samples = [p for _, p in self.price_history[-180:]]
        if len(samples) < 12:
            return self.profit_pct

        ranges = []
        for i in range(1, len(samples)):
            prev = samples[i - 1]
            cur = samples[i]
            if prev > 0:
                ranges.append(abs(cur - prev) / prev * 100.0)
        if not ranges:
            return self.profit_pct

        med_range = median(ranges) * 2.0
        target = med_range * float(config.VOLATILITY_PROFIT_FACTOR)
        target = max(float(config.VOLATILITY_PROFIT_FLOOR), target)
        target = min(float(config.VOLATILITY_PROFIT_CEILING), target)

        # Never below fee floor.
        fee_floor = self.maker_fee_pct * 2.0 + 0.1
        target = max(target, fee_floor)
        return round(target, 4)

    # ------------------ Startup/Reconcile ------------------

    def _reconcile_open_orders(self) -> dict:
        try:
            open_orders = self._get_open_orders()
        except Exception as e:
            logger.warning("Open-order reconciliation failed: %s", e)
            return {}

        known = 0
        dropped = 0
        for sid, slot in self.slots.items():
            st = slot.state
            kept = []
            for o in st.orders:
                if not o.txid:
                    # Unbound pending order from old crash, drop and let bootstrap rebuild.
                    dropped += 1
                    continue
                if o.txid in open_orders:
                    kept.append(o)
                    known += 1
                else:
                    # Keep it for one loop so closed status can be picked by QueryOrders.
                    kept.append(o)
            self.slots[sid].state = replace(st, orders=tuple(kept))

        logger.info("Reconciliation: %d tracked open orders (dropped %d unbound)", known, dropped)
        return open_orders

    def _replay_missed_fills(self, open_orders: dict) -> None:
        """
        Exactly-once restart replay:
        - look for tracked txids no longer open
        - aggregate Kraken trades history by ordertxid
        - emit synthetic fill events once
        """
        tracked: dict[str, tuple[int, str, int, str, str, int]] = {}
        for sid, slot in self.slots.items():
            for o in slot.state.orders:
                if o.txid:
                    tracked[o.txid] = (sid, "order", o.local_id, o.side, o.trade_id, o.cycle)
            for r in slot.state.recovery_orders:
                if r.txid:
                    tracked[r.txid] = (sid, "recovery", r.recovery_id, r.side, r.trade_id, r.cycle)

        candidates = [
            txid for txid in tracked.keys()
            if txid not in open_orders and txid not in self.seen_fill_txids
        ]
        if not candidates:
            return

        # 7-day replay window is enough for crash/redeploy recovery.
        start_ts = _now() - 7 * 86400
        try:
            history = self._get_trades_history(start=start_ts)
        except Exception as e:
            logger.warning("TradesHistory replay failed: %s", e)
            return
        if not history:
            return

        grouped: dict[str, list[dict]] = {}
        for row in history.values():
            order_txid = row.get("ordertxid", "")
            if order_txid not in tracked:
                continue
            pair_name = str(row.get("pair", "")).upper()
            if pair_name and self.pair not in pair_name and self.pair.replace("USD", "/USD") not in pair_name:
                continue
            grouped.setdefault(order_txid, []).append(row)

        replays = 0
        for txid, rows in grouped.items():
            if txid in self.seen_fill_txids:
                continue
            sid, kind, local_id, side, trade_id, cycle = tracked[txid]

            total_vol = 0.0
            total_cost = 0.0
            total_fee = 0.0
            last_time = 0.0
            for r in rows:
                try:
                    vol = float(r.get("vol", 0.0))
                    fee = float(r.get("fee", 0.0))
                    cost = float(r.get("cost", 0.0))
                    t = float(r.get("time", 0.0))
                except (TypeError, ValueError):
                    continue
                total_vol += vol
                total_cost += cost
                total_fee += fee
                if t > last_time:
                    last_time = t
            if total_vol <= 0:
                continue
            avg_price = total_cost / total_vol if total_cost > 0 else 0.0
            if avg_price <= 0:
                continue

            self.seen_fill_txids.add(txid)
            if kind == "order":
                supabase_store.save_fill(
                    {
                        "time": last_time or _now(),
                        "side": side,
                        "price": avg_price,
                        "volume": total_vol,
                        "profit": 0.0,
                        "fees": total_fee,
                    },
                    pair=self.pair,
                    trade_id=trade_id,
                    cycle=cycle,
                )
                ev = sm.FillEvent(
                    order_local_id=local_id,
                    txid=txid,
                    side=side,
                    price=avg_price,
                    volume=total_vol,
                    fee=total_fee,
                    timestamp=last_time or _now(),
                )
                self._apply_event(sid, ev, "fill_replay", {"txid": txid, "price": avg_price, "volume": total_vol})
            else:
                ev = sm.RecoveryFillEvent(
                    recovery_id=local_id,
                    txid=txid,
                    side=side,
                    price=avg_price,
                    volume=total_vol,
                    fee=total_fee,
                    timestamp=last_time or _now(),
                )
                self._apply_event(sid, ev, "recovery_fill_replay", {"txid": txid, "price": avg_price, "volume": total_vol})
            replays += 1

        if replays:
            logger.info("Replayed %d missed fills from trade history", replays)

    def _ensure_slot_bootstrapped(self, slot_id: int) -> None:
        slot = self.slots[slot_id]
        if slot.state.orders:
            return

        balance = self._safe_balance()
        usd = _usd_balance(balance)
        doge = _doge_balance(balance)
        market = self.last_price

        min_vol, min_cost = self._minimum_bootstrap_requirements(market)

        cfg = self._engine_cfg(slot)

        # Normal bootstrap: both sides available.
        if doge >= min_vol and usd >= min_cost:
            st = slot.state
            actions: list[sm.Action] = []
            st, a1 = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=self._slot_order_size_usd(slot), reason="bootstrap_A")
            if a1:
                actions.append(a1)
            st, a2 = sm.add_entry_order(st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=self._slot_order_size_usd(slot), reason="bootstrap_B")
            if a2:
                actions.append(a2)
            self.slots[slot_id].state = replace(st, long_only=False, short_only=False)
            if actions:
                self._execute_actions(slot_id, actions, "bootstrap")
            else:
                logger.info(
                    "slot %s bootstrap waiting: target order size $%.4f below Kraken minimum constraints",
                    slot_id,
                    self._slot_order_size_usd(slot),
                )
            return

        # Symmetric auto-reseed.
        if usd < min_cost and doge >= 2 * min_vol:
            st = replace(slot.state, short_only=True, long_only=False)
            target_usd = market * (2 * min_vol)
            st, a = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=target_usd, reason="reseed_usd")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "bootstrap_reseed_usd")
            else:
                logger.info("slot %s reseed_usd waiting: computed order below minimum", slot_id)
            return

        if doge < min_vol and usd >= 2 * min_cost:
            st = replace(slot.state, long_only=True, short_only=False)
            target_usd = market * (2 * min_vol)
            st, a = sm.add_entry_order(st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=target_usd, reason="reseed_doge")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "bootstrap_reseed_doge")
            else:
                logger.info("slot %s reseed_doge waiting: computed order below minimum", slot_id)
            return

        # Graceful degradation fallback: place whichever side can run.
        if doge >= min_vol:
            st = replace(slot.state, short_only=True, long_only=False)
            st, a = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=market * min_vol, reason="fallback_short_only")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "fallback_short_only")
            else:
                logger.info("slot %s fallback_short_only waiting: computed order below minimum", slot_id)
            return

        if usd >= min_cost:
            st = replace(slot.state, long_only=True, short_only=False)
            st, a = sm.add_entry_order(st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=market * min_vol, reason="fallback_long_only")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "fallback_long_only")
            else:
                logger.info("slot %s fallback_long_only waiting: computed order below minimum", slot_id)
            return

        self.pause(f"slot {slot_id} cannot bootstrap: insufficient USD and DOGE")

    def _auto_repair_degraded_slot(self, slot_id: int) -> None:
        if self.mode != "RUNNING":
            return

        slot = self.slots[slot_id]
        st = slot.state
        if not (st.long_only or st.short_only):
            return

        phase = sm.derive_phase(st)
        entries = [o for o in st.orders if o.role == "entry"]
        exits = [o for o in st.orders if o.role == "exit"]

        market = st.market_price or self.last_price
        if market <= 0:
            return
        min_vol, min_cost = self._minimum_bootstrap_requirements(market)

        balance = self._safe_balance()
        usd = _usd_balance(balance)
        doge = _doge_balance(balance)
        cfg = self._engine_cfg(slot)
        order_size = self._slot_order_size_usd(slot)

        repaired_state = st
        actions: list[sm.PlaceOrderAction] = []

        def _queue_entry(
            side: str,
            trade_id: str,
            cycle: int,
            reason: str,
        ) -> None:
            nonlocal repaired_state
            repaired_state, action = sm.add_entry_order(
                repaired_state,
                cfg,
                side=side,
                trade_id=trade_id,
                cycle=cycle,
                order_size_usd=order_size,
                reason=reason,
            )
            if action:
                actions.append(action)

        if phase == "S0":
            has_buy_entry = any(o.side == "buy" for o in entries)
            has_sell_entry = any(o.side == "sell" for o in entries)
            if st.long_only and has_buy_entry and not has_sell_entry and doge >= min_vol:
                _queue_entry("sell", "A", st.cycle_a, "auto_repair_s0_sell")
            elif st.short_only and has_sell_entry and not has_buy_entry and usd >= min_cost:
                _queue_entry("buy", "B", st.cycle_b, "auto_repair_s0_buy")
        elif phase == "S1a":
            has_buy_exit = any(o.side == "buy" for o in exits)
            has_buy_entry = any(o.side == "buy" for o in entries)
            if st.short_only and has_buy_exit and not has_buy_entry and usd >= min_cost:
                _queue_entry("buy", "B", st.cycle_b, "auto_repair_s1a_buy")
        elif phase == "S1b":
            has_sell_exit = any(o.side == "sell" for o in exits)
            has_sell_entry = any(o.side == "sell" for o in entries)
            if st.long_only and has_sell_exit and not has_sell_entry and doge >= min_vol:
                _queue_entry("sell", "A", st.cycle_a, "auto_repair_s1b_sell")

        if not actions:
            return

        self.slots[slot_id].state = repaired_state
        self._execute_actions(slot_id, list(actions), "auto_repair")

        post = self.slots[slot_id].state
        if any(sm.find_order(post, action.local_id) is not None for action in actions):
            self.slots[slot_id].state = replace(post, long_only=False, short_only=False)
            self._validate_slot(slot_id)
            logger.info("slot %s auto-repaired degraded %s state", slot_id, phase)

    # ------------------ Exchange IO ------------------

    def _safe_balance(self) -> dict:
        if self._loop_balance_cache is not None:
            return dict(self._loop_balance_cache)
        if not self._consume_private_budget(1, "get_balance"):
            return {}
        try:
            bal = kraken_client.get_balance()
            if self.enforce_loop_budget:
                self._loop_balance_cache = dict(bal)
            return bal
        except Exception as e:
            logger.warning("Balance query failed: %s", e)
            return {}

    def _apply_event(self, slot_id: int, event: sm.Event, event_type: str, details: dict) -> None:
        slot = self.slots[slot_id]
        cfg = self._engine_cfg(slot)
        old_phase = sm.derive_phase(slot.state)
        order_size = self._slot_order_size_usd(slot)

        new_state, actions = sm.transition(slot.state, event, cfg, order_size_usd=order_size)
        self.slots[slot_id].state = new_state
        new_phase = sm.derive_phase(new_state)

        self._log_event(
            slot_id=slot_id,
            from_state=old_phase,
            to_state=new_phase,
            event_type=event_type,
            details=details,
        )

        self._execute_actions(slot_id, actions, event_type)
        # Normalize degraded single-sided modes before strict invariant checks.
        self._normalize_slot_mode(slot_id)
        self._validate_slot(slot_id)

    def _execute_actions(self, slot_id: int, actions: list[sm.Action], source: str) -> None:
        if not actions:
            return

        slot = self.slots[slot_id]
        for action in actions:
            if isinstance(action, sm.PlaceOrderAction):
                # Pause/HALT blocks new entry placement; exits still allowed to reduce state risk.
                if self.mode in ("PAUSED", "HALTED") and action.role == "entry":
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    continue
                if self._price_age_sec() > config.STALE_PRICE_MAX_AGE_SEC:
                    self.pause("stale price data > 60s")
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    continue

                try:
                    txid = self._place_order(
                        side=action.side,
                        volume=action.volume,
                        price=action.price,
                        userref=(slot_id * 1_000_000 + action.local_id),
                    )
                    if not txid:
                        slot.state = sm.remove_order(slot.state, action.local_id)
                        self._normalize_slot_mode(slot_id)
                        continue
                    slot.state = sm.apply_order_txid(slot.state, action.local_id, txid)
                except Exception as e:
                    logger.warning("slot %s place failed %s.%s: %s", slot_id, action.trade_id, action.cycle, e)
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    # Graceful degradation: if an entry fails due insufficient funds,
                    # switch slot mode to whichever side can keep running.
                    if action.role == "entry" and "insufficient funds" in str(e).lower():
                        if action.side == "sell":
                            slot.state = replace(slot.state, long_only=True, short_only=False)
                        elif action.side == "buy":
                            slot.state = replace(slot.state, short_only=True, long_only=False)
                    self._normalize_slot_mode(slot_id)
                    self.consecutive_api_errors += 1
                    if self.consecutive_api_errors >= config.MAX_CONSECUTIVE_ERRORS:
                        self.pause(f"{self.consecutive_api_errors} consecutive API errors")

            elif isinstance(action, sm.CancelOrderAction):
                if action.txid:
                    try:
                        self._cancel_order(action.txid)
                    except Exception as e:
                        logger.warning("cancel failed %s: %s", action.txid, e)

            elif isinstance(action, sm.OrphanOrderAction):
                # Orphan keeps order live on Kraken as lottery ticket.
                pass

            elif isinstance(action, sm.BookCycleAction):
                text = (
                    f"<b>{self.pair_display} {action.trade_id}.{action.cycle}</b> "
                    f"net ${action.net_profit:.4f} "
                    f"(gross ${action.gross_profit:.4f}, fees ${action.fees:.4f})"
                    f"{' [recovery]' if action.from_recovery else ''}"
                )
                notifier._send_message(text)

        self._normalize_slot_mode(slot_id)

    def _poll_order_status(self) -> None:
        # Query active + recovery txids once per loop.
        def _to_float(value: Any) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        def _first_positive(row: dict, *keys: str) -> float:
            for k in keys:
                v = _to_float(row.get(k))
                if v > 0:
                    return v
            return 0.0

        tx_map: dict[str, tuple[int, str, int]] = {}
        for sid, slot in self.slots.items():
            for o in slot.state.orders:
                if o.txid:
                    tx_map[o.txid] = (sid, "order", o.local_id)
            for r in slot.state.recovery_orders:
                if r.txid:
                    tx_map[r.txid] = (sid, "recovery", r.recovery_id)

        if not tx_map:
            return

        try:
            info = self._query_orders_batched(list(tx_map.keys()))
            if not info:
                return
            self.consecutive_api_errors = 0
        except Exception as e:
            self.consecutive_api_errors += 1
            logger.warning("query_orders failed (%d): %s", self.consecutive_api_errors, e)
            if self.consecutive_api_errors >= config.MAX_CONSECUTIVE_ERRORS:
                self.pause(f"{self.consecutive_api_errors} consecutive API errors")
            return

        for txid, row in info.items():
            status = row.get("status", "")
            if txid not in tx_map:
                continue
            sid, kind, local_id = tx_map[txid]

            if status == "closed":
                self._partial_open_seen_txids.discard(txid)
                if txid in self.seen_fill_txids:
                    continue

                volume = _first_positive(row, "vol_exec", "vol")
                # Kraken can report limit price as 0 for closed orders; prefer executed/avg prices.
                price = _first_positive(row, "price_exec", "avg_price", "price")
                if price <= 0 and volume > 0:
                    cost = _to_float(row.get("cost"))
                    if cost > 0:
                        price = cost / volume
                fee = _to_float(row.get("fee"))
                if volume <= 0 or price <= 0:
                    logger.warning(
                        "closed order %s missing fill details (status=%s price=%s avg=%s exec=%s vol_exec=%s vol=%s)",
                        txid,
                        status,
                        row.get("price"),
                        row.get("avg_price"),
                        row.get("price_exec"),
                        row.get("vol_exec"),
                        row.get("vol"),
                    )
                    continue

                if kind == "order":
                    o = sm.find_order(self.slots[sid].state, local_id)
                    if not o:
                        logger.warning("closed order %s not found in slot %s local_id=%s", txid, sid, local_id)
                        continue
                    closed_ts = _now()
                    if o.placed_at > 0:
                        self._record_fill_duration(closed_ts - o.placed_at, closed_ts)
                    supabase_store.save_fill(
                        {
                            "time": _now(),
                            "side": o.side,
                            "price": price,
                            "volume": volume,
                            "profit": 0.0,
                            "fees": fee,
                        },
                        pair=self.pair,
                        trade_id=o.trade_id,
                        cycle=o.cycle,
                    )
                    ev = sm.FillEvent(
                        order_local_id=local_id,
                        txid=txid,
                        side=o.side,
                        price=price,
                        volume=volume,
                        fee=fee,
                        timestamp=closed_ts,
                    )
                    self._apply_event(sid, ev, "fill", {"txid": txid, "price": price, "volume": volume})
                    self.seen_fill_txids.add(txid)
                else:
                    r = next((x for x in self.slots[sid].state.recovery_orders if x.recovery_id == local_id), None)
                    if not r:
                        logger.warning("closed recovery %s not found in slot %s recovery_id=%s", txid, sid, local_id)
                        continue
                    ev = sm.RecoveryFillEvent(
                        recovery_id=local_id,
                        txid=txid,
                        side=r.side,
                        price=price,
                        volume=volume,
                        fee=fee,
                        timestamp=_now(),
                    )
                    self._apply_event(sid, ev, "recovery_fill", {"txid": txid, "price": price, "volume": volume})
                    self.seen_fill_txids.add(txid)

            elif status == "open":
                vol_exec = _to_float(row.get("vol_exec"))
                vol = _to_float(row.get("vol"))
                if vol_exec > 0 and vol > 0 and vol_exec < vol and txid not in self._partial_open_seen_txids:
                    self._record_partial_fill_open(_now())
                    self._partial_open_seen_txids.add(txid)

            elif status in ("canceled", "expired"):
                self._partial_open_seen_txids.discard(txid)
                vol_exec = _to_float(row.get("vol_exec"))
                vol = _to_float(row.get("vol"))
                if vol_exec > 0:
                    self._record_partial_fill_cancel(_now())
                    logger.warning(
                        "PHANTOM_POSITION_CANARY txid=%s kind=%s slot=%s local=%s status=%s vol_exec=%.8f vol=%.8f",
                        txid,
                        kind,
                        sid,
                        local_id,
                        status,
                        vol_exec,
                        vol,
                    )
                if kind == "order":
                    st = self.slots[sid].state
                    if sm.find_order(st, local_id):
                        self.slots[sid].state = sm.remove_order(st, local_id)
                else:
                    st = self.slots[sid].state
                    self.slots[sid].state = sm.remove_recovery(st, local_id)

    # ------------------ Invariants ------------------

    def _validate_slot(self, slot_id: int) -> None:
        st = self.slots[slot_id].state
        violations = sm.check_invariants(st)
        if violations:
            # Hotfix: if order size is intentionally below Kraken minimum, slot may
            # legally sit in an empty/incomplete S0 waiting state. Do not hard halt.
            if self._is_min_size_wait_state(slot_id, violations):
                logger.info(
                    "Slot %s in min-size wait state; skipping invariant halt (%s)",
                    slot_id,
                    violations[0],
                )
                return
            if self._is_bootstrap_pending_state(slot_id, violations):
                logger.info(
                    "Slot %s bootstrap pending; skipping invariant halt (%s)",
                    slot_id,
                    violations[0],
                )
                return
            self.halt(f"slot {slot_id} invariant violation: {violations[0]}")
            logger.error("Slot %s invariant violations: %s", slot_id, violations)

    def _is_min_size_wait_state(self, slot_id: int, violations: list[str]) -> bool:
        if not violations:
            return False
        if any(v != "S0 must be exactly A sell entry + B buy entry" for v in violations):
            return False

        slot = self.slots[slot_id]
        st = slot.state
        if sm.derive_phase(st) != "S0":
            return False
        if any(o.role == "exit" for o in st.orders):
            return False

        target_usd = self._slot_order_size_usd(slot)
        market = st.market_price or self.last_price
        if market <= 0:
            return False

        min_vol = float(self.constraints.get("min_volume", 13.0))
        min_cost = float(self.constraints.get("min_cost_usd", 0.0))
        required_usd = max(min_cost, min_vol * market)
        return target_usd < required_usd

    def _is_bootstrap_pending_state(self, slot_id: int, violations: list[str]) -> bool:
        if not violations:
            return False
        allowed = {
            "S0 must be exactly A sell entry + B buy entry",
            "S0 long_only must be exactly one buy entry",
            "S0 short_only must be exactly one sell entry",
        }
        if any(v not in allowed for v in violations):
            return False
        st = self.slots[slot_id].state
        if sm.derive_phase(st) != "S0":
            return False
        entries = [o for o in st.orders if o.role == "entry"]
        exits = [o for o in st.orders if o.role == "exit"]
        if exits or len(entries) > 1:
            return False
        # Recoverable startup/placement gap: allow empty/one-entry S0 briefly.
        if not entries:
            return True
        if st.long_only and entries[0].side != "buy":
            return False
        if st.short_only and entries[0].side != "sell":
            return False
        return True

    def _normalize_slot_mode(self, slot_id: int) -> None:
        st = self.slots[slot_id].state
        entries = [o for o in st.orders if o.role == "entry"]
        exits = [o for o in st.orders if o.role == "exit"]
        if not entries and not exits:
            # Prevent stale snapshot flags from causing false S0 single-sided halts.
            self.slots[slot_id].state = replace(st, long_only=False, short_only=False)
            return
        if exits:
            # Degraded S1 states are legal when only one exit side survives
            # (e.g., loop API budget skipped replacing the missing entry).
            if not entries and len(exits) == 1:
                exit_side = exits[0].side
                if exit_side == "sell":
                    self.slots[slot_id].state = replace(st, long_only=True, short_only=False)
                elif exit_side == "buy":
                    self.slots[slot_id].state = replace(st, long_only=False, short_only=True)
            elif len(exits) == 2:
                self.slots[slot_id].state = replace(st, long_only=False, short_only=False)
            elif len(exits) == 1 and len(entries) == 1 and entries[0].side == exits[0].side:
                # Normal S1 shape (exit + same-side entry) should not keep degraded flags.
                self.slots[slot_id].state = replace(st, long_only=False, short_only=False)
            return
        buy_entries = [o for o in entries if o.side == "buy"]
        sell_entries = [o for o in entries if o.side == "sell"]
        if len(buy_entries) == 1 and len(sell_entries) == 0:
            self.slots[slot_id].state = replace(st, long_only=True, short_only=False)
        elif len(sell_entries) == 1 and len(buy_entries) == 0:
            self.slots[slot_id].state = replace(st, long_only=False, short_only=True)
        elif len(sell_entries) == 1 and len(buy_entries) == 1:
            self.slots[slot_id].state = replace(st, long_only=False, short_only=False)

    # ------------------ Commands ------------------

    def add_slot(self) -> tuple[bool, str]:
        if self.mode == "HALTED":
            return False, "bot halted"
        sid = self.next_slot_id
        self.next_slot_id += 1
        st = sm.PairState(
            market_price=self.last_price,
            now=_now(),
            profit_pct_runtime=self.profit_pct,
        )
        self.slots[sid] = SlotRuntime(slot_id=sid, state=st)
        self._ensure_slot_bootstrapped(sid)
        self._save_snapshot()
        return True, f"slot {sid} added"

    def set_entry_pct(self, value: float) -> tuple[bool, str]:
        if value < 0.05:
            return False, "entry_pct must be >= 0.05"
        self.entry_pct = float(value)
        self._save_snapshot()
        return True, f"entry_pct set to {self.entry_pct:.3f}%"

    def set_profit_pct(self, value: float) -> tuple[bool, str]:
        fee_floor = self.maker_fee_pct * 2.0 + 0.1
        if value < fee_floor:
            return False, f"profit_pct must be >= {fee_floor:.3f}%"
        self.profit_pct = float(value)
        self._save_snapshot()
        return True, f"profit_pct set to {self.profit_pct:.3f}%"

    def soft_close(self, slot_id: int, recovery_id: int) -> tuple[bool, str]:
        slot = self.slots.get(slot_id)
        if not slot:
            return False, f"unknown slot {slot_id}"

        rec = next((r for r in slot.state.recovery_orders if r.recovery_id == recovery_id), None)
        if not rec:
            return False, f"unknown recovery id {recovery_id}"

        # Soft close = cancel old orphan and re-place nearer to market.
        if rec.txid:
            try:
                self._cancel_order(rec.txid)
            except Exception as e:
                logger.warning("soft close cancel failed %s: %s", rec.txid, e)

        side = rec.side
        if side == "sell":
            new_price = round(self.last_price * (1 + self.entry_pct / 100.0), self.constraints["price_decimals"])
        else:
            new_price = round(self.last_price * (1 - self.entry_pct / 100.0), self.constraints["price_decimals"])

        try:
            txid = self._place_order(
                side=side,
                volume=rec.volume,
                price=new_price,
                userref=(slot_id * 1_000_000 + 900_000 + recovery_id),
            )
            if not txid:
                return False, "soft-close skipped: API loop budget exceeded"
        except Exception as e:
            return False, f"soft-close placement failed: {e}"

        patched = []
        for r in slot.state.recovery_orders:
            if r.recovery_id == recovery_id:
                patched.append(replace(r, price=new_price, txid=txid, reason="soft_close"))
            else:
                patched.append(r)
        slot.state = replace(slot.state, recovery_orders=tuple(patched))
        self._save_snapshot()
        return True, f"soft-close repriced recovery {recovery_id}"

    def soft_close_next(self) -> tuple[bool, str]:
        oldest: tuple[int, sm.RecoveryOrder] | None = None
        for sid, slot in self.slots.items():
            for r in slot.state.recovery_orders:
                if oldest is None or r.orphaned_at < oldest[1].orphaned_at:
                    oldest = (sid, r)
        if not oldest:
            return False, "no recovery orders"
        return self.soft_close(oldest[0], oldest[1].recovery_id)

    def status_text(self) -> str:
        lines = [
            f"mode: {self.mode}",
            f"pair: {self.pair_display}",
            f"price: ${self.last_price:.6f}",
            f"price_age: {self._price_age_sec():.1f}s",
            f"entry_pct: {self.entry_pct:.3f}%",
            f"profit_pct: {self.profit_pct:.3f}%",
            f"slots: {len(self.slots)}",
        ]
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            lines.append(
                f"slot {sid}: {sm.derive_phase(st)} A.{st.cycle_a} B.{st.cycle_b} "
                f"orders={len(st.orders)} orphans={len(st.recovery_orders)} pnl=${st.total_profit:.4f}"
            )
        return "\n".join(lines)

    # ------------------ Loop ------------------

    def run_loop_once(self) -> None:
        with self.lock:
            if self.mode == "HALTED":
                self._save_snapshot()
                return

            self._refresh_price(strict=False)
            if self._price_age_sec() > config.STALE_PRICE_MAX_AGE_SEC:
                self.pause("stale price data > 60s")

            runtime_profit = self._volatility_profit_pct()

            # Tick slots with latest price and timer.
            for sid in sorted(self.slots.keys()):
                st = self.slots[sid].state
                self.slots[sid].state = replace(st, profit_pct_runtime=runtime_profit)

                ev_price = sm.PriceTick(price=self.last_price, timestamp=_now())
                self._apply_event(sid, ev_price, "price_tick", {"price": self.last_price})

                ev_timer = sm.TimerTick(timestamp=_now())
                self._apply_event(sid, ev_timer, "timer_tick", {})

                # If a slot drained its active orders, bootstrap it again.
                self._ensure_slot_bootstrapped(sid)
                # When a slot is in one-sided fallback, try to restore normal mode
                # as soon as balances and API budget allow.
                self._auto_repair_degraded_slot(sid)

            self._poll_order_status()
            # Refresh pair open-order telemetry (Kraken source of truth) when budget allows.
            self._refresh_open_order_telemetry()

            # Pressure notice for orphan growth.
            total_orphans = sum(len(s.state.recovery_orders) for s in self.slots.values())
            if total_orphans and total_orphans % int(config.ORPHAN_PRESSURE_WARN_AT) == 0:
                notifier._send_message(f"<b>Orphan pressure</b>\n{total_orphans} recovery orders on book")

            self._save_snapshot()

    # ------------------ Telegram ------------------

    def poll_telegram(self) -> None:
        callbacks, commands = notifier.poll_updates()

        for cb in callbacks:
            data = cb.get("data", "")
            if data.startswith("sc:"):
                # soft-close callback: sc:<slot>:<recovery>
                try:
                    _, s, r = data.split(":", 2)
                    ok, msg = self.soft_close(int(s), int(r))
                except Exception as e:
                    ok, msg = False, f"bad soft-close callback: {e}"
                notifier.answer_callback(cb.get("callback_id", ""), msg)

        for cmd in commands:
            text = (cmd.get("text") or "").strip()
            parts = text.split()
            head = parts[0].lower() if parts else ""

            ok = True
            msg = ""

            if head == "/pause":
                self.pause("paused by operator")
                msg = "paused"
            elif head == "/resume":
                self.resume()
                msg = "running"
            elif head == "/add_slot":
                ok, msg = self.add_slot()
            elif head == "/status":
                msg = self.status_text()
            elif head == "/help":
                msg = (
                    "Commands:\n"
                    "/pause\n/resume\n/add_slot\n/status\n/help\n"
                    "/soft_close [slot_id recovery_id]\n"
                    "/set_entry_pct <value>\n"
                    "/set_profit_pct <value>"
                )
            elif head == "/set_entry_pct":
                if len(parts) < 2:
                    ok, msg = False, "usage: /set_entry_pct <value>"
                else:
                    try:
                        ok, msg = self.set_entry_pct(float(parts[1]))
                    except ValueError:
                        ok, msg = False, "invalid value"
            elif head == "/set_profit_pct":
                if len(parts) < 2:
                    ok, msg = False, "usage: /set_profit_pct <value>"
                else:
                    try:
                        ok, msg = self.set_profit_pct(float(parts[1]))
                    except ValueError:
                        ok, msg = False, "invalid value"
            elif head == "/soft_close":
                if len(parts) == 3:
                    try:
                        ok, msg = self.soft_close(int(parts[1]), int(parts[2]))
                    except ValueError:
                        ok, msg = False, "usage: /soft_close <slot_id> <recovery_id>"
                else:
                    # Interactive list via inline buttons.
                    rows = []
                    for sid in sorted(self.slots.keys()):
                        for r in self.slots[sid].state.recovery_orders[:12]:
                            rows.append([{"text": f"slot {sid} / #{r.recovery_id} {r.side} {r.trade_id}.{r.cycle}", "callback_data": f"sc:{sid}:{r.recovery_id}"}])
                    if not rows:
                        ok, msg = False, "no recovery orders"
                    else:
                        notifier.send_with_buttons("Select recovery to soft-close:", rows)
                        ok, msg = True, "sent picker"
            else:
                ok, msg = False, "unknown command"

            notifier._send_message(("OK: " if ok else "ERR: ") + msg)

    # ------------------ API status ------------------

    def status_payload(self) -> dict:
        def _unrealized_pnl(exit_side: str, entry_price: float, market_price: float, volume: float) -> float:
            if entry_price <= 0 or market_price <= 0 or volume <= 0:
                return 0.0
            # buy exit closes a short (profit as market falls), sell exit closes a long.
            if exit_side == "buy":
                return (entry_price - market_price) * volume
            return (market_price - entry_price) * volume

        with self.lock:
            now = _now()
            self._trim_rolling_telemetry(now)
            slots = []
            total_unrealized_profit = 0.0
            total_active_orders = 0
            for sid in sorted(self.slots.keys()):
                st = self.slots[sid].state
                phase = sm.derive_phase(st)
                slot_unrealized_profit = 0.0
                open_orders = []
                total_active_orders += len(st.orders)
                for o in st.orders:
                    if o.role == "exit":
                        slot_unrealized_profit += _unrealized_pnl(
                            exit_side=o.side,
                            entry_price=o.entry_price,
                            market_price=st.market_price,
                            volume=o.volume,
                        )
                    open_orders.append({
                        "local_id": o.local_id,
                        "side": o.side,
                        "role": o.role,
                        "trade_id": o.trade_id,
                        "cycle": o.cycle,
                        "volume": o.volume,
                        "price": o.price,
                        "txid": o.txid,
                    })
                recs = []
                for r in st.recovery_orders:
                    dist = abs(r.price - st.market_price) / st.market_price * 100.0 if st.market_price > 0 else 0.0
                    slot_unrealized_profit += _unrealized_pnl(
                        exit_side=r.side,
                        entry_price=r.entry_price,
                        market_price=st.market_price,
                        volume=r.volume,
                    )
                    recs.append({
                        "recovery_id": r.recovery_id,
                        "trade_id": r.trade_id,
                        "cycle": r.cycle,
                        "side": r.side,
                        "price": r.price,
                        "volume": r.volume,
                        "txid": r.txid,
                        "reason": r.reason,
                        "age_sec": max(0.0, now - r.orphaned_at),
                        "distance_pct": dist,
                    })
                cycles = list(st.completed_cycles[-20:])
                slot_realized_doge = st.total_profit / st.market_price if st.market_price > 0 else 0.0
                slot_unrealized_doge = slot_unrealized_profit / st.market_price if st.market_price > 0 else 0.0
                slots.append({
                    "slot_id": sid,
                    "phase": phase,
                    "long_only": st.long_only,
                    "short_only": st.short_only,
                    "s2_entered_at": st.s2_entered_at,
                    "market_price": st.market_price,
                    "cycle_a": st.cycle_a,
                    "cycle_b": st.cycle_b,
                    "total_profit": st.total_profit,
                    "total_profit_doge": slot_realized_doge,
                    "unrealized_profit": slot_unrealized_profit,
                    "unrealized_profit_doge": slot_unrealized_doge,
                    "today_realized_loss": st.today_realized_loss,
                    "total_round_trips": st.total_round_trips,
                    "order_size_usd": self._slot_order_size_usd(self.slots[sid]),
                    "profit_pct_runtime": st.profit_pct_runtime,
                    "open_orders": open_orders,
                    "recovery_orders": recs,
                    "recent_cycles": [c.__dict__ for c in reversed(cycles)],
                })
                total_unrealized_profit += slot_unrealized_profit

            total_profit = sum(s.state.total_profit for s in self.slots.values())
            total_loss = sum(s.state.today_realized_loss for s in self.slots.values())
            total_round_trips = sum(s.state.total_round_trips for s in self.slots.values())
            total_orphans = sum(len(s.state.recovery_orders) for s in self.slots.values())
            internal_open_orders_current = total_active_orders + total_orphans
            kraken_open_orders_current = self._kraken_open_orders_current
            if kraken_open_orders_current is None:
                open_orders_current = internal_open_orders_current
                open_orders_source = "internal_fallback"
            else:
                open_orders_current = int(kraken_open_orders_current)
                open_orders_source = "kraken"

            pair_open_order_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
            safety_ratio = float(config.OPEN_ORDER_SAFETY_RATIO)
            safety_ratio = min(1.0, max(0.1, safety_ratio))
            open_orders_safe_cap = max(1, int(pair_open_order_limit * safety_ratio))
            open_order_headroom = open_orders_safe_cap - open_orders_current
            open_order_utilization_pct = (open_orders_current / open_orders_safe_cap * 100.0) if open_orders_safe_cap > 0 else 0.0
            orders_per_slot_estimate = (open_orders_current / len(self.slots)) if self.slots else None
            estimated_slots_remaining = 0
            if orders_per_slot_estimate and orders_per_slot_estimate > 0 and open_order_headroom > 0:
                estimated_slots_remaining = int(open_order_headroom // orders_per_slot_estimate)

            partial_fill_open_events_1d = len(self._partial_fill_open_events)
            partial_fill_cancel_events_1d = len(self._partial_fill_cancel_events)
            median_fill_seconds_1d, p95_fill_seconds_1d = self._fill_duration_stats_1d()

            if partial_fill_cancel_events_1d > 0 or open_order_headroom < 10:
                status_band = "stop"
            elif open_order_headroom < 20:
                status_band = "caution"
            else:
                status_band = "normal"

            drift_persistent = self._open_order_drift_is_persistent(
                now=now,
                internal_open_orders_current=internal_open_orders_current,
                kraken_open_orders_current=kraken_open_orders_current,
            )

            blocked_risk_hint: list[str] = []
            if open_orders_source == "internal_fallback":
                blocked_risk_hint.append("kraken_open_orders_unavailable")
            if drift_persistent:
                blocked_risk_hint.append("open_order_drift_persistent")
            if open_order_headroom < 10:
                blocked_risk_hint.append("near_open_order_cap")
            elif open_order_headroom < 20:
                blocked_risk_hint.append("open_order_caution")
            if partial_fill_open_events_1d > 0:
                blocked_risk_hint.append("partial_fill_open_pressure")
            if partial_fill_cancel_events_1d > 0:
                blocked_risk_hint.append("partial_fill_cancel_detected")

            top_phase = slots[0]["phase"] if slots else "S0"
            pnl_ref_price = self.last_price if self.last_price > 0 else (slots[0]["market_price"] if slots else 0.0)
            total_profit_doge = total_profit / pnl_ref_price if pnl_ref_price > 0 else 0.0
            total_unrealized_doge = total_unrealized_profit / pnl_ref_price if pnl_ref_price > 0 else 0.0

            return {
                "mode": self.mode,
                "pause_reason": self.pause_reason,
                "pair": self.pair_display,
                "entry_pct": self.entry_pct,
                "profit_pct": self.profit_pct,
                "price": self.last_price,
                "price_age_sec": self._price_age_sec(),
                "top_phase": top_phase,
                "slot_count": len(self.slots),
                "total_profit": total_profit,
                "total_profit_doge": total_profit_doge,
                "total_unrealized_profit": total_unrealized_profit,
                "total_unrealized_doge": total_unrealized_doge,
                "today_realized_loss": total_loss,
                "total_round_trips": total_round_trips,
                "total_orphans": total_orphans,
                "pnl_reference_price": pnl_ref_price,
                "s2_orphan_after_sec": float(config.S2_ORPHAN_AFTER_SEC),
                "stale_price_max_age_sec": float(config.STALE_PRICE_MAX_AGE_SEC),
                "capacity_fill_health": {
                    "open_orders_current": open_orders_current,
                    "open_orders_source": open_orders_source,
                    "open_orders_internal": internal_open_orders_current,
                    "open_orders_kraken": kraken_open_orders_current,
                    "open_orders_drift": (
                        None
                        if kraken_open_orders_current is None
                        else int(kraken_open_orders_current) - internal_open_orders_current
                    ),
                    "open_order_limit_configured": pair_open_order_limit,
                    "open_orders_safe_cap": open_orders_safe_cap,
                    "open_order_headroom": open_order_headroom,
                    "open_order_utilization_pct": open_order_utilization_pct,
                    "orders_per_slot_estimate": orders_per_slot_estimate,
                    "estimated_slots_remaining": estimated_slots_remaining,
                    "partial_fill_open_events_1d": partial_fill_open_events_1d,
                    "partial_fill_cancel_events_1d": partial_fill_cancel_events_1d,
                    "median_fill_seconds_1d": median_fill_seconds_1d,
                    "p95_fill_seconds_1d": p95_fill_seconds_1d,
                    "status_band": status_band,
                    "blocked_risk_hint": blocked_risk_hint,
                },
                "slots": slots,
            }


_RUNTIME: BotRuntime | None = None


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        logger.info("HTTP %s - %s", self.address_string(), fmt % args)

    def _send_json(self, data: dict, code: int = 200) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid request body") from exc
        if not isinstance(body, dict):
            raise ValueError("invalid request body")
        return body

    def do_GET(self) -> None:  # noqa: N802
        global _RUNTIME
        if self.path == "/" or self.path.startswith("/?"):
            body = dashboard.DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/factory" or self.path.startswith("/factory?"):
            import factory_viz

            body = factory_viz.FACTORY_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/api/status") or self.path.startswith("/api/swarm/status"):
            if _RUNTIME is None:
                self._send_json({"error": "runtime not ready"}, 503)
                return
            self._send_json(_RUNTIME.status_payload())
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        global _RUNTIME
        try:
            if not self.path.startswith("/api/action"):
                self._send_json({"ok": False, "message": "not found"}, 404)
                return
            if _RUNTIME is None:
                self._send_json({"ok": False, "message": "runtime not ready"}, 503)
                return

            try:
                body = self._read_json()
            except Exception:
                self._send_json({"ok": False, "message": "invalid request body"}, 400)
                return

            action = (body.get("action") or "").strip()
            parsed: dict[str, float | int] = {}

            if action in ("set_entry_pct", "set_profit_pct"):
                try:
                    parsed["value"] = float(body.get("value", 0))
                    if not isfinite(parsed["value"]):
                        raise ValueError("non-finite value")
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid numeric value"}, 400)
                    return
            elif action == "soft_close":
                try:
                    parsed["slot_id"] = int(body.get("slot_id", 0))
                    parsed["recovery_id"] = int(body.get("recovery_id", 0))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid slot/recovery id"}, 400)
                    return
            elif action in ("pause", "resume", "add_slot", "soft_close_next"):
                pass
            else:
                self._send_json({"ok": False, "message": f"unknown action: {action}"}, 400)
                return

            with _RUNTIME.lock:
                ok = True
                msg = "ok"
                if action == "pause":
                    _RUNTIME.pause("paused from dashboard")
                    msg = "paused"
                elif action == "resume":
                    _RUNTIME.resume()
                    msg = "running"
                elif action == "add_slot":
                    ok, msg = _RUNTIME.add_slot()
                elif action == "set_entry_pct":
                    ok, msg = _RUNTIME.set_entry_pct(float(parsed["value"]))
                elif action == "set_profit_pct":
                    ok, msg = _RUNTIME.set_profit_pct(float(parsed["value"]))
                elif action == "soft_close":
                    ok, msg = _RUNTIME.soft_close(int(parsed["slot_id"]), int(parsed["recovery_id"]))
                elif action == "soft_close_next":
                    ok, msg = _RUNTIME.soft_close_next()
                _RUNTIME._save_snapshot()

            self._send_json({"ok": bool(ok), "message": str(msg)}, 200 if ok else 400)
        except Exception:
            logger.exception("Unhandled exception in /api/action")
            self._send_json({"ok": False, "message": "internal server error"}, 500)


def start_http_server() -> ThreadingHTTPServer | None:
    if config.HEALTH_PORT <= 0:
        return None
    server = ThreadingHTTPServer(("0.0.0.0", int(config.HEALTH_PORT)), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dashboard-server")
    thread.start()
    logger.info("Dashboard server started on :%s", config.HEALTH_PORT)
    return server


def run() -> None:
    global _RUNTIME
    setup_logging()

    rt = BotRuntime()
    _RUNTIME = rt

    def _handle_signal(signum, _frame):
        logger.info("Signal %s received", signum)
        rt.shutdown(f"signal {signum}")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    server = None
    try:
        rt.initialize()
        server = start_http_server()

        poll = max(5, int(config.POLL_INTERVAL_SECONDS))
        logger.info("Entering main loop (every %ss)", poll)

        while rt.running:
            loop_start = _now()
            try:
                with rt.lock:
                    rt.begin_loop()
                    rt.run_loop_once()
                    rt.poll_telegram()
                    rt.end_loop()
            except Exception as e:
                logger.exception("Main loop error: %s", e)
                rt.consecutive_api_errors += 1
                with rt.lock:
                    rt.end_loop()
                if rt.consecutive_api_errors >= config.MAX_CONSECUTIVE_ERRORS:
                    rt.pause(f"loop errors: {rt.consecutive_api_errors}")

            elapsed = _now() - loop_start
            sleep_for = max(0.2, poll - elapsed)
            time.sleep(sleep_for)

    finally:
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
        if _RUNTIME is not None:
            _RUNTIME.shutdown("process exit")


if __name__ == "__main__":
    run()
