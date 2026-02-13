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


def _asset_balance(balance: dict, aliases: tuple[str, ...]) -> float:
    """
    Read balance for an asset across Kraken naming variants.

    Kraken may expose balances as:
    - legacy keys (e.g. XXDG, ZUSD)
    - plain keys (e.g. DOGE, USD)
    - free-balance suffix keys (e.g. XXDG.F, ZUSD.F)
    """
    if not isinstance(balance, dict):
        return 0.0

    for key in aliases:
        free_key = f"{key}.F"
        if free_key in balance:
            try:
                return float(balance.get(free_key, 0.0))
            except (TypeError, ValueError):
                continue

    for key in aliases:
        if key in balance:
            try:
                return float(balance.get(key, 0.0))
            except (TypeError, ValueError):
                continue

    return 0.0


def _usd_balance(balance: dict) -> float:
    return _asset_balance(balance, ("ZUSD", "USD"))


def _doge_balance(balance: dict) -> float:
    return _asset_balance(balance, ("XXDG", "XDG", "DOGE"))


@dataclass
class SlotRuntime:
    slot_id: int
    state: sm.PairState


class CapitalLedger:
    """Within-loop capital tracker that prevents over-commitment across slots."""

    def __init__(self) -> None:
        self._synced = False
        self._usd_from_free = False
        self._doge_from_free = False
        self._total_usd = 0.0
        self._total_doge = 0.0
        self._committed_usd = 0.0
        self._committed_doge = 0.0
        self._loop_placed_usd = 0.0
        self._loop_placed_doge = 0.0

    @property
    def available_usd(self) -> float:
        # With Kraken free-balance keys (`*.F`), total already means available.
        if self._usd_from_free:
            return max(0.0, self._total_usd - self._loop_placed_usd)
        return max(0.0, self._total_usd - self._committed_usd - self._loop_placed_usd)

    @property
    def available_doge(self) -> float:
        # With Kraken free-balance keys (`*.F`), total already means available.
        if self._doge_from_free:
            return max(0.0, self._total_doge - self._loop_placed_doge)
        return max(0.0, self._total_doge - self._committed_doge - self._loop_placed_doge)

    def sync(self, balance: dict, slots: dict[int, SlotRuntime]) -> None:
        """Recompute from scratch at loop start using fresh Kraken balance."""
        self._usd_from_free = any(k in balance for k in ("ZUSD.F", "USD.F"))
        self._doge_from_free = any(k in balance for k in ("XXDG.F", "XDG.F", "DOGE.F"))
        self._total_usd = _usd_balance(balance)
        self._total_doge = _doge_balance(balance)
        committed_usd = 0.0
        committed_doge = 0.0
        for slot in slots.values():
            st = slot.state
            for o in st.orders:
                if not o.txid:
                    continue
                if o.side == "buy":
                    committed_usd += o.volume * o.price
                elif o.side == "sell":
                    committed_doge += o.volume
            for r in st.recovery_orders:
                if not r.txid:
                    continue
                if r.side == "buy":
                    committed_usd += r.volume * r.price
                elif r.side == "sell":
                    committed_doge += r.volume
        self._committed_usd = committed_usd
        self._committed_doge = committed_doge
        self._loop_placed_usd = 0.0
        self._loop_placed_doge = 0.0
        self._synced = True

    def commit_order(self, side: str, price: float, volume: float) -> None:
        """Deduct capital after a successful order placement within this loop."""
        if side == "buy":
            self._loop_placed_usd += volume * price
        elif side == "sell":
            self._loop_placed_doge += volume

    def clear(self) -> None:
        """Reset loop-placed accumulators at end of loop."""
        self._loop_placed_usd = 0.0
        self._loop_placed_doge = 0.0
        self._synced = False

    def snapshot(self) -> dict:
        return {
            "synced": self._synced,
            "usd_from_free": self._usd_from_free,
            "doge_from_free": self._doge_from_free,
            "total_usd": self._total_usd,
            "total_doge": self._total_doge,
            "committed_usd": self._committed_usd,
            "committed_doge": self._committed_doge,
            "loop_placed_usd": self._loop_placed_usd,
            "loop_placed_doge": self._loop_placed_doge,
            "available_usd": self.available_usd,
            "available_doge": self.available_doge,
        }


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
        self.ledger = CapitalLedger()
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
        self._loop_available_usd: float | None = None
        self._loop_available_doge: float | None = None
        self._last_balance_snapshot: dict | None = None
        self._last_balance_ts = 0.0

        # Kraken-first capacity telemetry (pair-filtered open orders).
        self._kraken_open_orders_current: int | None = None
        self._kraken_open_orders_ts = 0.0
        self._open_order_drift_over_threshold_since: float | None = None
        self._open_order_drift_last_alert_at = 0.0
        self._open_order_drift_alert_active = False
        self._open_order_drift_alert_active_since: float | None = None

        # Auto-soft-close telemetry.
        self._auto_soft_close_total: int = 0
        self._auto_soft_close_last_at: float = 0.0

        # Balance reconciliation baseline {usd, doge, ts}.
        self._recon_baseline: dict | None = None

        # Rolling 24h fill/partial telemetry.
        self._partial_fill_open_events: deque[float] = deque()
        self._partial_fill_cancel_events: deque[float] = deque()
        self._fill_durations_1d: deque[tuple[float, float]] = deque()
        self._partial_open_seen_txids: set[str] = set()

        # Rolling 24h DOGE-equivalent equity snapshots (5-min interval).
        self._doge_eq_snapshots: deque[tuple[float, float]] = deque()  # (ts, doge_eq)
        self._doge_eq_snapshot_interval: float = 300.0  # 5 min
        self._doge_eq_last_snapshot_ts: float = 0.0

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
        while self._doge_eq_snapshots and self._doge_eq_snapshots[0][0] < cutoff:
            self._doge_eq_snapshots.popleft()

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
            "recon_baseline": self._recon_baseline,
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
            self._recon_baseline = snap.get("recon_baseline", None)

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
        self._loop_available_usd = None
        self._loop_available_doge = None
        # Sync capital ledger with fresh Kraken balance
        bal = self._safe_balance()
        if bal:
            self.ledger.sync(bal, self.slots)
            self._loop_available_usd = self.ledger.available_usd
            self._loop_available_doge = self.ledger.available_doge

    def end_loop(self) -> None:
        self.enforce_loop_budget = False
        self._loop_balance_cache = None
        self._loop_available_usd = None
        self._loop_available_doge = None
        self.ledger.clear()

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
        if balance is None:
            logger.warning(
                "slot %s bootstrap deferred: balance unavailable (live+cache unavailable)",
                slot_id,
            )
            return
        usd = self.ledger.available_usd if self.ledger._synced else _usd_balance(balance)
        doge = self.ledger.available_doge if self.ledger._synced else _doge_balance(balance)
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

        logger.warning(
            "slot %s bootstrap blocked: usd=%.8f doge=%.8f min_cost=%.8f min_vol=%.8f market=%.8f keys=%s",
            slot_id,
            usd,
            doge,
            min_cost,
            min_vol,
            market,
            sorted(balance.keys()),
        )
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
        if balance is None:
            logger.warning(
                "slot %s auto-repair deferred: balance unavailable (live+cache unavailable)",
                slot_id,
            )
            return
        usd = self.ledger.available_usd if self.ledger._synced else _usd_balance(balance)
        doge = self.ledger.available_doge if self.ledger._synced else _doge_balance(balance)
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

    def _safe_balance(self) -> dict | None:
        def _fresh_cached_balance(now_ts: float) -> dict | None:
            if not self._last_balance_snapshot:
                return None
            # Cached balance can safely bridge brief API-budget starvation.
            max_age = max(60.0, float(config.POLL_INTERVAL_SECONDS) * 3.0)
            if now_ts - self._last_balance_ts > max_age:
                return None
            return dict(self._last_balance_snapshot)

        if self._loop_balance_cache is not None:
            return dict(self._loop_balance_cache)
        now_ts = _now()
        if not self._consume_private_budget(1, "get_balance"):
            return _fresh_cached_balance(now_ts)
        try:
            bal = kraken_client.get_balance()
            self._last_balance_snapshot = dict(bal)
            self._last_balance_ts = now_ts
            if self.enforce_loop_budget:
                self._loop_balance_cache = dict(bal)
            return bal
        except Exception as e:
            logger.warning("Balance query failed: %s", e)
            return _fresh_cached_balance(now_ts)

    def _seed_loop_available_from_balance(self, balance: dict | None) -> bool:
        if self._loop_available_usd is not None and self._loop_available_doge is not None:
            return True
        if self.ledger._synced:
            self._loop_available_usd = self.ledger.available_usd
            self._loop_available_doge = self.ledger.available_doge
            return True
        if balance is None:
            return False
        self._loop_available_usd = _usd_balance(balance)
        self._loop_available_doge = _doge_balance(balance)
        return True

    def _required_notional(self, side: str, volume: float, price: float) -> float:
        if side == "buy":
            return max(0.0, float(volume) * float(price))
        if side == "sell":
            return max(0.0, float(volume))
        return 0.0

    def _try_reserve_loop_funds(self, *, side: str, volume: float, price: float) -> bool:
        if self._loop_available_usd is None or self._loop_available_doge is None:
            if not self._seed_loop_available_from_balance(self._loop_balance_cache):
                if not self._seed_loop_available_from_balance(self._safe_balance()):
                    return False

        req = self._required_notional(side, volume, price)
        if side == "buy":
            if (self._loop_available_usd or 0.0) + 1e-12 < req:
                return False
            self._loop_available_usd = (self._loop_available_usd or 0.0) - req
            return True
        if side == "sell":
            if (self._loop_available_doge or 0.0) + 1e-12 < req:
                return False
            self._loop_available_doge = (self._loop_available_doge or 0.0) - req
            return True
        return True

    def _release_loop_reservation(self, *, side: str, volume: float, price: float) -> None:
        req = self._required_notional(side, volume, price)
        if side == "buy":
            self._loop_available_usd = (self._loop_available_usd or 0.0) + req
        elif side == "sell":
            self._loop_available_doge = (self._loop_available_doge or 0.0) + req

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
        def _mark_entry_fallback_for_insufficient_funds(action: sm.PlaceOrderAction) -> None:
            # Graceful degradation: if an entry cannot be funded now,
            # switch to the side that can keep running.
            if action.role != "entry":
                return
            if action.side == "sell":
                slot.state = replace(slot.state, long_only=True, short_only=False)
            elif action.side == "buy":
                slot.state = replace(slot.state, short_only=True, long_only=False)

        # Pre-compute order capacity for gating new entries.
        _internal_order_count = self._internal_open_order_count()
        _pair_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
        _safe_ratio = min(1.0, max(0.1, float(config.OPEN_ORDER_SAFETY_RATIO)))
        _order_cap = max(1, int(_pair_limit * _safe_ratio))

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

                # Capacity gate: block new entry orders when at open-order cap.
                # Exit orders are always allowed (they reduce exposure).
                if action.role == "entry" and _internal_order_count >= _order_cap:
                    logger.warning(
                        "slot %s entry blocked: at order capacity (%d/%d)",
                        slot_id, _internal_order_count, _order_cap,
                    )
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    _mark_entry_fallback_for_insufficient_funds(action)
                    self._normalize_slot_mode(slot_id)
                    continue

                reserved_locally = self._try_reserve_loop_funds(
                    side=action.side,
                    volume=action.volume,
                    price=action.price,
                )
                if not reserved_locally:
                    logger.warning(
                        "slot %s local-funds check blocked %s %s [%s.%s] vol=%.8f px=%.8f (avail usd=%.8f doge=%.8f)",
                        slot_id,
                        action.role,
                        action.side,
                        action.trade_id,
                        action.cycle,
                        action.volume,
                        action.price,
                        self._loop_available_usd or 0.0,
                        self._loop_available_doge or 0.0,
                    )
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    _mark_entry_fallback_for_insufficient_funds(action)
                    self._normalize_slot_mode(slot_id)
                    continue

                try:
                    txid = self._place_order(
                        side=action.side,
                        volume=action.volume,
                        price=action.price,
                        userref=(slot_id * 1_000_000 + action.local_id),
                    )
                    if not txid:
                        self._release_loop_reservation(
                            side=action.side,
                            volume=action.volume,
                            price=action.price,
                        )
                        slot.state = sm.remove_order(slot.state, action.local_id)
                        _mark_entry_fallback_for_insufficient_funds(action)
                        self._normalize_slot_mode(slot_id)
                        continue
                    slot.state = sm.apply_order_txid(slot.state, action.local_id, txid)
                    self.ledger.commit_order(action.side, action.price, action.volume)
                    _internal_order_count += 1
                except Exception as e:
                    self._release_loop_reservation(
                        side=action.side,
                        volume=action.volume,
                        price=action.price,
                    )
                    logger.warning("slot %s place failed %s.%s: %s", slot_id, action.trade_id, action.cycle, e)
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    # Graceful degradation: if an entry fails due insufficient funds,
                    # switch slot mode to whichever side can keep running.
                    if "insufficient funds" in str(e).lower():
                        _mark_entry_fallback_for_insufficient_funds(action)
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

    def remove_slot(self, slot_id: int) -> tuple[bool, str]:
        """Remove a slot entirely, cancelling all its open orders on Kraken."""
        slot = self.slots.get(slot_id)
        if not slot:
            return False, f"unknown slot {slot_id}"

        cancelled = 0
        failed = 0

        # Cancel all active orders for this slot.
        for o in slot.state.orders:
            if o.txid:
                try:
                    ok = self._cancel_order(o.txid)
                    if ok:
                        cancelled += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.warning("remove_slot: cancel order %s failed: %s", o.txid, e)
                    failed += 1

        # Cancel all recovery orders for this slot.
        for r in slot.state.recovery_orders:
            if r.txid:
                try:
                    ok = self._cancel_order(r.txid)
                    if ok:
                        cancelled += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.warning("remove_slot: cancel recovery %s failed: %s", r.txid, e)
                    failed += 1

        if failed > 0:
            return False, f"slot {slot_id}: {failed} cancel failures, not removed (retry)"

        del self.slots[slot_id]
        self._save_snapshot()

        msg = f"slot {slot_id} removed, cancelled {cancelled} orders"
        logger.info("remove_slot: %s", msg)
        return True, msg

    def remove_slots(self, count: int = 1) -> tuple[bool, str]:
        """Remove N slots from the top (highest slot IDs first)."""
        if count < 1:
            return False, "count must be >= 1"
        if count > len(self.slots):
            return False, f"only {len(self.slots)} slots exist"

        removed = []
        for sid in sorted(self.slots.keys(), reverse=True)[:count]:
            ok, msg = self.remove_slot(sid)
            if not ok:
                return False, f"stopped after removing {len(removed)}: {msg}"
            removed.append(sid)

        return True, f"removed {len(removed)} slots: {removed}"

    def _auto_soft_close_if_capacity_pressure(self) -> None:
        """Soft-close farthest recovery orders when capacity utilization is high.

        Triggered each main-loop cycle.  Processes a small batch (default 2)
        per cycle to stay within rate limits while steadily draining orphans.
        """
        cap_threshold = float(config.AUTO_SOFT_CLOSE_CAPACITY_PCT)
        batch_size = max(1, min(int(config.AUTO_SOFT_CLOSE_BATCH), 5))

        # Use Kraken-reported count if available, else internal count.
        if self._kraken_open_orders_current is not None:
            current = int(self._kraken_open_orders_current)
        else:
            current = self._internal_open_order_count()

        pair_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
        utilization_pct = current / pair_limit * 100.0

        if utilization_pct < cap_threshold:
            return

        # Collect all recoveries with distance from market, sorted farthest first.
        if self.last_price <= 0:
            return
        candidates: list[tuple[float, int, sm.RecoveryOrder]] = []
        for sid in self.slots:
            for r in self.slots[sid].state.recovery_orders:
                dist = abs(r.price - self.last_price) / self.last_price * 100.0
                candidates.append((dist, sid, r))

        if not candidates:
            return

        candidates.sort(key=lambda t: t[0], reverse=True)
        batch = candidates[:batch_size]

        repriced = 0
        for _dist, sid, rec in batch:
            slot = self.slots[sid]

            # Cancel old order.
            if rec.txid:
                try:
                    ok = self._cancel_order(rec.txid)
                    if not ok:
                        continue
                except Exception:
                    continue

            # Place near market.
            if rec.side == "sell":
                new_price = round(self.last_price * (1 + self.entry_pct / 100.0), self.constraints["price_decimals"])
            else:
                new_price = round(self.last_price * (1 - self.entry_pct / 100.0), self.constraints["price_decimals"])

            try:
                txid = self._place_order(
                    side=rec.side,
                    volume=rec.volume,
                    price=new_price,
                    userref=(sid * 1_000_000 + 900_000 + rec.recovery_id),
                )
            except Exception:
                txid = None

            if not txid:
                # Clear txid so poller won't silently drop on next cycle.
                slot.state = replace(slot.state, recovery_orders=tuple(
                    replace(x, txid="", reason="auto_close_place_failed") if x.recovery_id == rec.recovery_id else x
                    for x in slot.state.recovery_orders
                ))
                continue

            slot.state = replace(slot.state, recovery_orders=tuple(
                replace(x, price=new_price, txid=txid, reason="auto_soft_close") if x.recovery_id == rec.recovery_id else x
                for x in slot.state.recovery_orders
            ))
            repriced += 1

        if repriced > 0:
            self._auto_soft_close_total += repriced
            self._auto_soft_close_last_at = _now()
            logger.info(
                "auto_soft_close: repriced %d/%d farthest recoveries (capacity %.0f%% >= %.0f%% threshold, lifetime %d)",
                repriced, len(batch), utilization_pct, cap_threshold, self._auto_soft_close_total,
            )
            notifier._send_message(
                f"<b>Auto soft-close</b>\nCapacity {utilization_pct:.0f}% — "
                f"repriced {repriced} farthest recoveries near market"
            )

    def cancel_stale_recoveries(self, min_distance_pct: float = 3.0, max_batch: int = 8) -> tuple[bool, str]:
        """Bulk soft-close recovery orders farther than min_distance_pct from market.

        Reprices them to within entry_pct of market so they fill quickly and
        book P&L through the normal recovery-fill path.  Processes up to
        max_batch per call to stay within Kraken rate limits (2 API calls each:
        cancel old + place new).  Call repeatedly until remaining == 0.
        """
        if self.last_price <= 0:
            return False, "no market price"

        max_batch = max(1, min(max_batch, 20))

        # Collect all stale recoveries across slots.
        stale: list[tuple[int, sm.RecoveryOrder]] = []
        for sid in sorted(self.slots.keys()):
            for r in self.slots[sid].state.recovery_orders:
                distance_pct = abs(r.price - self.last_price) / self.last_price * 100.0
                if distance_pct >= min_distance_pct:
                    stale.append((sid, r))

        if not stale:
            return True, "no stale recoveries"

        batch = stale[:max_batch]
        remaining = len(stale) - len(batch)

        repriced = 0
        failed = 0

        # Bypass per-loop budget — this is a user-initiated bulk operation.
        saved_enforce = self.enforce_loop_budget
        self.enforce_loop_budget = False
        try:
            for sid, rec in batch:
                slot = self.slots[sid]

                # Cancel old order on Kraken.
                # _cancel_order returns False on failure (not just exception).
                if rec.txid:
                    try:
                        ok = self._cancel_order(rec.txid)
                        if not ok:
                            logger.warning("cancel_stale: cancel %s returned False", rec.txid)
                            failed += 1
                            continue
                    except Exception as e:
                        logger.warning("cancel_stale: cancel %s failed: %s", rec.txid, e)
                        failed += 1
                        continue

                # Place new order near market.
                if rec.side == "sell":
                    new_price = round(self.last_price * (1 + self.entry_pct / 100.0), self.constraints["price_decimals"])
                else:
                    new_price = round(self.last_price * (1 - self.entry_pct / 100.0), self.constraints["price_decimals"])

                try:
                    txid = self._place_order(
                        side=rec.side,
                        volume=rec.volume,
                        price=new_price,
                        userref=(sid * 1_000_000 + 900_000 + rec.recovery_id),
                    )
                except Exception as e:
                    logger.warning("cancel_stale: place failed after cancel: %s", e)
                    txid = None

                if not txid:
                    # Cancel succeeded but place failed — clear txid so the
                    # poller doesn't see a "cancelled" order and silently drop
                    # the recovery.  It stays in state for retry next call.
                    slot.state = replace(slot.state, recovery_orders=tuple(
                        replace(x, txid="", reason="place_failed") if x.recovery_id == rec.recovery_id else x
                        for x in slot.state.recovery_orders
                    ))
                    failed += 1
                    continue

                # Update recovery in-place with new price/txid.
                slot.state = replace(slot.state, recovery_orders=tuple(
                    replace(x, price=new_price, txid=txid, reason="soft_close") if x.recovery_id == rec.recovery_id else x
                    for x in slot.state.recovery_orders
                ))
                repriced += 1
        finally:
            self.enforce_loop_budget = saved_enforce

        if repriced > 0 or failed > 0:
            self._save_snapshot()
        msg = f"repriced {repriced} stale recoveries to within {self.entry_pct:.1f}% of market"
        if failed:
            msg += f", {failed} failures"
        if remaining > 0:
            msg += f", {remaining} remaining (call again)"
        return True, msg

    def reconcile_drift(self) -> tuple[bool, str]:
        """Cancel Kraken-only orders not tracked internally (drift orders).

        Fetches open orders from Kraken, compares against all known txids
        in slots (orders + recovery_orders), and cancels any pair-matching
        orders that we don't recognize.
        """
        try:
            open_orders = kraken_client.get_open_orders()
        except Exception as e:
            return False, f"failed to fetch open orders: {e}"

        # Build set of all internally tracked txids.
        known_txids: set[str] = set()
        for slot in self.slots.values():
            for o in slot.state.orders:
                if o.txid:
                    known_txids.add(o.txid)
            for r in slot.state.recovery_orders:
                if r.txid:
                    known_txids.add(r.txid)

        # Find pair-matching orders on Kraken that we don't track.
        unknown_txids: list[str] = []
        for txid, row in open_orders.items():
            if not isinstance(row, dict):
                continue
            if not self._order_matches_runtime_pair(row):
                continue
            if txid not in known_txids:
                unknown_txids.append(txid)

        if not unknown_txids:
            return True, f"no drift: {len(open_orders)} kraken orders, {len(known_txids)} tracked"

        cancelled = 0
        failed = 0
        for txid in unknown_txids:
            try:
                kraken_client.cancel_order(txid)
                cancelled += 1
            except Exception as e:
                logger.warning("reconcile_drift: cancel %s failed: %s", txid, e)
                failed += 1

        msg = f"cancelled {cancelled}/{len(unknown_txids)} drift orders"
        if failed:
            msg += f", {failed} failures"
        return True, msg

    def _pnl_audit_summary(self, tolerance: float = 1e-8) -> dict[str, Any]:
        """Recompute realized P&L from completed cycles."""
        total_profit_state = 0.0
        total_profit_cycles = 0.0
        total_loss_state = 0.0
        total_loss_cycles = 0.0
        total_trips_state = 0
        total_trips_cycles = 0
        bad_slots: list[str] = []

        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            cycle_profit = sum(c.net_profit for c in st.completed_cycles)
            cycle_loss = sum(-c.net_profit for c in st.completed_cycles if c.net_profit < 0)
            cycle_trips = len(st.completed_cycles)

            profit_drift = st.total_profit - cycle_profit
            loss_drift = st.today_realized_loss - cycle_loss
            trips_drift = st.total_round_trips - cycle_trips
            if abs(profit_drift) > tolerance or abs(loss_drift) > tolerance or trips_drift != 0:
                bad_slots.append(f"{sid}(pnl={profit_drift:+.6f},loss={loss_drift:+.6f},trips={trips_drift:+d})")

            total_profit_state += st.total_profit
            total_profit_cycles += cycle_profit
            total_loss_state += st.today_realized_loss
            total_loss_cycles += cycle_loss
            total_trips_state += st.total_round_trips
            total_trips_cycles += cycle_trips

        profit_drift = total_profit_state - total_profit_cycles
        loss_drift = total_loss_state - total_loss_cycles
        trips_drift = total_trips_state - total_trips_cycles
        ok = abs(profit_drift) <= tolerance and abs(loss_drift) <= tolerance and trips_drift == 0 and not bad_slots
        preview = bad_slots[:6]
        more = max(0, len(bad_slots) - len(preview))

        return {
            "ok": ok,
            "tolerance": tolerance,
            "slot_count": len(self.slots),
            "slot_mismatch_count": len(bad_slots),
            "slot_mismatches_preview": preview,
            "slot_mismatches_more": more,
            "profit_drift": profit_drift,
            "loss_drift": loss_drift,
            "trips_drift": trips_drift,
            "total_round_trips_state": total_trips_state,
            "total_round_trips_cycles": total_trips_cycles,
            "total_profit_state": total_profit_state,
            "total_profit_cycles": total_profit_cycles,
            "total_loss_state": total_loss_state,
            "total_loss_cycles": total_loss_cycles,
        }

    def _format_pnl_audit_message(self, summary: dict[str, Any]) -> str:
        if bool(summary.get("ok")):
            return (
                "pnl audit OK: "
                f"slots={int(summary.get('slot_count', 0))} "
                f"trips={int(summary.get('total_round_trips_state', 0))} "
                f"profit_drift={float(summary.get('profit_drift', 0.0)):+.8f} "
                f"loss_drift={float(summary.get('loss_drift', 0.0)):+.8f}"
            )

        preview = list(summary.get("slot_mismatches_preview", []))
        details = ", ".join(str(x) for x in preview)
        more = int(summary.get("slot_mismatches_more", 0))
        if more > 0:
            details += f", +{more} more"
        return (
            "pnl audit mismatch: "
            f"profit_drift={float(summary.get('profit_drift', 0.0)):+.8f} "
            f"loss_drift={float(summary.get('loss_drift', 0.0)):+.8f} "
            f"trips_drift={int(summary.get('trips_drift', 0)):+d}; "
            f"slots={details or 'none'}"
        )

    def audit_pnl(self, tolerance: float = 1e-8) -> tuple[bool, str]:
        """Recompute realized P&L from completed cycles and report drift."""
        summary = self._pnl_audit_summary(tolerance=tolerance)
        return bool(summary.get("ok")), self._format_pnl_audit_message(summary)

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

            if self._recon_baseline is None and self.last_price > 0 and self._last_balance_snapshot:
                bal = self._last_balance_snapshot
                self._recon_baseline = {
                    "usd": _usd_balance(bal), "doge": _doge_balance(bal), "ts": _now(),
                }
                logger.info("Balance recon baseline captured: $%.2f + %.1f DOGE",
                            self._recon_baseline["usd"], self._recon_baseline["doge"])

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
            # Auto-soft-close farthest recoveries when nearing order capacity.
            self._auto_soft_close_if_capacity_pressure()

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
                    "/remove_slot [slot_id]\n/remove_slots [N]\n"
                    "/soft_close [slot_id recovery_id]\n"
                    "/cancel_stale [min_distance_pct]\n"
                    "/reconcile_drift\n"
                    "/audit_pnl\n"
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
            elif head == "/cancel_stale":
                dist = 3.0
                if len(parts) >= 2:
                    try:
                        dist = float(parts[1])
                    except ValueError:
                        pass
                ok, msg = self.cancel_stale_recoveries(dist)
            elif head == "/remove_slot":
                if len(parts) >= 2:
                    try:
                        ok, msg = self.remove_slot(int(parts[1]))
                    except ValueError:
                        ok, msg = False, "usage: /remove_slot [slot_id]"
                else:
                    # Remove highest-numbered slot by default.
                    if not self.slots:
                        ok, msg = False, "no slots"
                    else:
                        ok, msg = self.remove_slot(max(self.slots.keys()))
            elif head == "/remove_slots":
                count = 1
                if len(parts) >= 2:
                    try:
                        count = int(parts[1])
                    except ValueError:
                        pass
                ok, msg = self.remove_slots(count)
            elif head == "/reconcile_drift":
                ok, msg = self.reconcile_drift()
            elif head == "/audit_pnl":
                ok, msg = self.audit_pnl()
            else:
                ok, msg = False, "unknown command"

            notifier._send_message(("OK: " if ok else "ERR: ") + msg)

    # ------------------ DOGE bias scoreboard ------------------

    def _update_doge_eq_snapshot(self, now: float) -> None:
        if now - self._doge_eq_last_snapshot_ts < self._doge_eq_snapshot_interval:
            return
        bal = self._last_balance_snapshot
        price = self.last_price
        if not bal or price <= 0:
            return
        doge_eq = _doge_balance(bal) + _usd_balance(bal) / price
        self._doge_eq_snapshots.append((now, doge_eq))
        self._doge_eq_last_snapshot_ts = now

    def _extract_b_side_gaps(self) -> list[dict]:
        """Extract gaps between consecutive B-side cycles for opportunity PnL and re-entry lag."""
        gaps: list[dict] = []
        price = self.last_price
        now = _now()
        for slot in self.slots.values():
            b_cycles = [c for c in slot.state.completed_cycles if c.trade_id == "B"]
            b_cycles.sort(key=lambda c: c.cycle)
            for i in range(len(b_cycles) - 1):
                prev, nxt = b_cycles[i], b_cycles[i + 1]
                if prev.exit_time <= 0 or nxt.entry_time <= 0:
                    continue
                lag_sec = nxt.entry_time - prev.exit_time
                gap_start_price = prev.exit_price
                gap_end_price = nxt.entry_price
                if gap_start_price <= 0:
                    continue
                price_distance_pct = (gap_end_price - gap_start_price) / gap_start_price * 100.0
                opportunity_usd = (gap_end_price - gap_start_price) * prev.volume
                gaps.append({
                    "slot_id": slot.slot_id,
                    "lag_sec": lag_sec,
                    "opportunity_usd": opportunity_usd,
                    "price_distance_pct": price_distance_pct,
                    "volume": prev.volume,
                    "gap_start_price": gap_start_price,
                    "gap_end_price": gap_end_price,
                    "open": False,
                })
            # Detect open gap: last B-cycle exited but no subsequent B entry has filled.
            # A resting (unfilled) B-entry still leaves capital in USD, so we measure
            # the gap from the last exit fill to now regardless of pending orders.
            if b_cycles and b_cycles[-1].exit_time > 0 and price > 0:
                last = b_cycles[-1]
                lag_sec = now - last.exit_time
                gap_start_price = last.exit_price
                if gap_start_price > 0:
                    price_distance_pct = (price - gap_start_price) / gap_start_price * 100.0
                    opportunity_usd = (price - gap_start_price) * last.volume
                    gaps.append({
                        "slot_id": slot.slot_id,
                        "lag_sec": lag_sec,
                        "opportunity_usd": opportunity_usd,
                        "price_distance_pct": price_distance_pct,
                        "volume": last.volume,
                        "gap_start_price": gap_start_price,
                        "gap_end_price": price,
                        "open": True,
                    })
        return gaps

    def _compute_doge_bias_scoreboard(self) -> dict | None:
        bal = self._last_balance_snapshot
        price = self.last_price
        if not bal or price <= 0:
            return None
        now = _now()

        # --- Metric 1: DOGE-Equivalent Equity ---
        current_doge_eq = _doge_balance(bal) + _usd_balance(bal) / price
        doge_eq_change_1h = None
        doge_eq_change_24h = None
        sparkline = [v for _, v in self._doge_eq_snapshots]

        for target_ago, attr_name in [(3600, "1h"), (86400, "24h")]:
            target_ts = now - target_ago
            best_snap = None
            best_dist = float("inf")
            for ts, val in self._doge_eq_snapshots:
                dist = abs(ts - target_ts)
                if dist < best_dist and dist < 600:  # 10 min tolerance
                    best_dist = dist
                    best_snap = val
            if best_snap is not None:
                delta = current_doge_eq - best_snap
                if attr_name == "1h":
                    doge_eq_change_1h = delta
                else:
                    doge_eq_change_24h = delta

        # --- Metric 2: Idle USD Above Runway ---
        observed_usd = _usd_balance(bal)
        usd_committed_buy_orders = 0.0
        usd_next_entries_estimate = 0.0
        for slot in self.slots.values():
            usd_next_entries_estimate += self._slot_order_size_usd(slot)
            for o in slot.state.orders:
                if o.txid and o.side == "buy":
                    usd_committed_buy_orders += o.volume * o.price
            for r in slot.state.recovery_orders:
                if r.txid and r.side == "buy":
                    usd_committed_buy_orders += r.volume * r.price
        usd_runway_floor = usd_committed_buy_orders + (usd_next_entries_estimate * 1.5)
        idle_usd = max(0.0, observed_usd - usd_runway_floor)
        idle_usd_pct = (idle_usd / observed_usd * 100.0) if observed_usd > 0 else 0.0

        # --- Metrics 3 & 4: Opportunity PnL + Re-entry Lag ---
        gaps = self._extract_b_side_gaps()
        closed_gaps = [g for g in gaps if not g["open"]]
        open_gaps = [g for g in gaps if g["open"]]

        # Metric 3: Opportunity PnL
        total_opportunity_pnl_usd = sum(g["opportunity_usd"] for g in closed_gaps)
        total_opportunity_pnl_doge = total_opportunity_pnl_usd / price if price > 0 else 0.0
        open_gap_opportunity_usd = sum(g["opportunity_usd"] for g in open_gaps) if open_gaps else None
        gap_count = len(closed_gaps)
        avg_opportunity_per_gap_usd = (total_opportunity_pnl_usd / gap_count) if gap_count > 0 else None
        worst_missed_usd = max((g["opportunity_usd"] for g in closed_gaps), default=None)

        # Metric 4: Re-entry Lag
        closed_lags = [g["lag_sec"] for g in closed_gaps]
        median_reentry_lag_sec = float(median(closed_lags)) if closed_lags else None
        avg_reentry_lag_sec = (sum(closed_lags) / len(closed_lags)) if closed_lags else None
        max_reentry_lag_sec = max(closed_lags, default=None)
        current_open_lag_sec = max((g["lag_sec"] for g in open_gaps), default=None)
        current_open_lag_price_pct = max(
            (g["price_distance_pct"] for g in open_gaps), default=None
        )
        lag_count = len(closed_lags)
        closed_price_dists = [g["price_distance_pct"] for g in closed_gaps]
        median_price_distance_pct = float(median(closed_price_dists)) if closed_price_dists else None

        return {
            "doge_eq": current_doge_eq,
            "doge_eq_change_1h": doge_eq_change_1h,
            "doge_eq_change_24h": doge_eq_change_24h,
            "doge_eq_sparkline": sparkline,
            "idle_usd": idle_usd,
            "idle_usd_pct": idle_usd_pct,
            "usd_runway_floor": usd_runway_floor,
            "observed_usd": observed_usd,
            "total_opportunity_pnl_usd": total_opportunity_pnl_usd,
            "total_opportunity_pnl_doge": total_opportunity_pnl_doge,
            "open_gap_opportunity_usd": open_gap_opportunity_usd,
            "gap_count": gap_count,
            "avg_opportunity_per_gap_usd": avg_opportunity_per_gap_usd,
            "worst_missed_usd": worst_missed_usd,
            "median_reentry_lag_sec": median_reentry_lag_sec,
            "avg_reentry_lag_sec": avg_reentry_lag_sec,
            "max_reentry_lag_sec": max_reentry_lag_sec,
            "current_open_lag_sec": current_open_lag_sec,
            "current_open_lag_price_pct": current_open_lag_price_pct,
            "lag_count": lag_count,
            "median_price_distance_pct": median_price_distance_pct,
        }

    # ------------------ Balance reconciliation ------------------

    def _compute_balance_recon(self, total_profit: float, total_unrealized: float) -> dict | None:
        if self._recon_baseline is None:
            return None
        price = self.last_price
        if price <= 0:
            return {"status": "NO_PRICE"}
        bal = self._last_balance_snapshot
        if not bal:
            return {"status": "NO_BALANCE"}

        baseline = self._recon_baseline
        baseline_doge_eq = baseline["doge"] + baseline["usd"] / price
        current_usd = _usd_balance(bal)
        current_doge = _doge_balance(bal)
        current_doge_eq = current_doge + current_usd / price

        account_growth = current_doge_eq - baseline_doge_eq
        bot_pnl_doge = (total_profit + total_unrealized) / price if price > 0 else 0.0
        drift = account_growth - bot_pnl_doge
        drift_pct = (drift / baseline_doge_eq * 100.0) if baseline_doge_eq > 0 else 0.0
        threshold = float(config.BALANCE_RECON_DRIFT_PCT)
        status = "OK" if abs(drift_pct) <= threshold else "DRIFT"

        return {
            "status": status,
            "baseline_doge_eq": baseline_doge_eq,
            "current_doge_eq": current_doge_eq,
            "account_growth_doge": account_growth,
            "bot_pnl_doge": bot_pnl_doge,
            "drift_doge": drift,
            "drift_pct": drift_pct,
            "threshold_pct": threshold,
            "baseline_ts": baseline["ts"],
            "price": price,
            "simulated": config.DRY_RUN,
        }

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
            self._update_doge_eq_snapshot(now)
            slots = []
            total_unrealized_profit = 0.0
            total_active_orders = 0
            committed_usd_internal = 0.0
            committed_doge_internal = 0.0
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
                    if o.txid:
                        if o.side == "buy":
                            committed_usd_internal += o.volume * o.price
                        elif o.side == "sell":
                            committed_doge_internal += o.volume
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
                    if r.txid:
                        if r.side == "buy":
                            committed_usd_internal += r.volume * r.price
                        elif r.side == "sell":
                            committed_doge_internal += r.volume
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
            last_balance = dict(self._last_balance_snapshot) if self._last_balance_snapshot else {}
            observed_usd_balance = _usd_balance(last_balance) if last_balance else None
            observed_doge_balance = _doge_balance(last_balance) if last_balance else None
            balance_age_sec = (now - self._last_balance_ts) if last_balance else None
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
            pnl_audit = self._pnl_audit_summary()

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
                "pnl_audit": {
                    "ok": bool(pnl_audit.get("ok")),
                    "message": self._format_pnl_audit_message(pnl_audit),
                    "tolerance": float(pnl_audit.get("tolerance", 0.0)),
                    "profit_drift": float(pnl_audit.get("profit_drift", 0.0)),
                    "loss_drift": float(pnl_audit.get("loss_drift", 0.0)),
                    "trips_drift": int(pnl_audit.get("trips_drift", 0)),
                    "slot_mismatch_count": int(pnl_audit.get("slot_mismatch_count", 0)),
                    "slot_mismatches_preview": list(pnl_audit.get("slot_mismatches_preview", [])),
                    "slot_mismatches_more": int(pnl_audit.get("slot_mismatches_more", 0)),
                },
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
                    "auto_soft_close_total": self._auto_soft_close_total,
                    "auto_soft_close_last_at": self._auto_soft_close_last_at or None,
                    "auto_soft_close_threshold_pct": float(config.AUTO_SOFT_CLOSE_CAPACITY_PCT),
                },
                "balance_health": {
                    "usd_observed": observed_usd_balance,
                    "doge_observed": observed_doge_balance,
                    "balance_age_sec": balance_age_sec,
                    "usd_committed_internal": committed_usd_internal,
                    "doge_committed_internal": committed_doge_internal,
                    "loop_available_usd": self._loop_available_usd,
                    "loop_available_doge": self._loop_available_doge,
                    "ledger": self.ledger.snapshot(),
                },
                "balance_recon": self._compute_balance_recon(total_profit, total_unrealized_profit),
                "doge_bias_scoreboard": self._compute_doge_bias_scoreboard(),
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
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
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
            elif action == "cancel_stale_recoveries":
                try:
                    parsed["min_distance_pct"] = float(body.get("min_distance_pct", 3.0))
                except (TypeError, ValueError):
                    parsed["min_distance_pct"] = 3.0
                try:
                    parsed["max_batch"] = int(body.get("max_batch", 8))
                except (TypeError, ValueError):
                    parsed["max_batch"] = 8
            elif action == "remove_slot":
                try:
                    parsed["slot_id"] = int(body.get("slot_id", -1))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
                    return
            elif action == "remove_slots":
                try:
                    parsed["count"] = int(body.get("count", 1))
                except (TypeError, ValueError):
                    parsed["count"] = 1
            elif action in ("pause", "resume", "add_slot", "soft_close_next", "reconcile_drift", "audit_pnl"):
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
                elif action == "cancel_stale_recoveries":
                    ok, msg = _RUNTIME.cancel_stale_recoveries(
                        float(parsed.get("min_distance_pct", 3.0)),
                        int(parsed.get("max_batch", 8)),
                    )
                elif action == "remove_slot":
                    ok, msg = _RUNTIME.remove_slot(int(parsed["slot_id"]))
                elif action == "remove_slots":
                    ok, msg = _RUNTIME.remove_slots(int(parsed.get("count", 1)))
                elif action == "reconcile_drift":
                    ok, msg = _RUNTIME.reconcile_drift()
                elif action == "audit_pnl":
                    ok, msg = _RUNTIME.audit_pnl()
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
