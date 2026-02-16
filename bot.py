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
import os
import signal
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import ceil, exp, floor, isfinite
from socketserver import ThreadingMixIn
from statistics import median
from types import SimpleNamespace
from typing import Any

import config
import dashboard
import kraken_client
from kelly_sizer import KellyConfig, KellySizer
import notifier
import ai_advisor
import state_machine as sm
import supabase_store


logger = logging.getLogger(__name__)
_BOT_RUNTIME_STATE_FILE = os.path.join(config.LOG_DIR, "bot_runtime.json")


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
    alias: str = ""


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
        self.slot_alias_pool: tuple[str, ...] = tuple(config.SLOT_ALIAS_POOL)
        self.slot_alias_recycle_queue: deque[str] = deque()
        self.slot_alias_fallback_counter = 1

        self.target_layers = 0
        self.effective_layers = 0
        self.layer_last_add_event: dict | None = None

        self.next_event_id = 1
        self.seen_fill_txids: set[str] = set()

        self.price_history: list[tuple[float, float]] = []
        self.last_price = 0.0
        self.last_price_ts = 0.0

        self.consecutive_api_errors = 0
        self.enforce_loop_budget = False
        self.loop_private_calls = 0
        self.entry_adds_per_loop_cap = max(1, int(config.MAX_ENTRY_ADDS_PER_LOOP))
        self.entry_adds_per_loop_used = 0
        self._entry_adds_deferred_total = 0
        self._entry_adds_drained_total = 0
        self._entry_adds_last_deferred_at = 0.0
        self._entry_adds_last_drained_at = 0.0
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
        self._auto_recovery_drain_total: int = 0
        self._auto_recovery_drain_last_at: float = 0.0

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

        # Inventory rebalancer state.
        self._rebalancer_idle_ratio: float = 0.0
        self._rebalancer_smoothed_error: float = 0.0
        self._rebalancer_smoothed_velocity: float = 0.0
        self._rebalancer_current_skew: float = 0.0
        self._rebalancer_last_update_ts: float = 0.0
        self._rebalancer_last_raw_error: float = 0.0
        self._rebalancer_sign_flip_history: deque[float] = deque()
        self._rebalancer_damped_until: float = 0.0
        self._rebalancer_last_capacity_band: str = "normal"
        base_target = max(0.0, min(1.0, float(config.REBALANCE_TARGET_IDLE_PCT)))
        self._trend_fast_ema: float = 0.0
        self._trend_slow_ema: float = 0.0
        self._trend_score: float = 0.0
        self._trend_dynamic_target: float = base_target
        self._trend_smoothed_target: float = base_target
        self._trend_target_locked_until: float = 0.0
        self._trend_last_update_ts: float = 0.0
        self._ohlcv_since_cursor: int | None = None
        self._ohlcv_last_sync_ts: float = 0.0
        self._ohlcv_last_candle_ts: float = 0.0
        self._ohlcv_last_rows_queued: int = 0
        self._ohlcv_secondary_since_cursor: int | None = None
        self._ohlcv_secondary_last_sync_ts: float = 0.0
        self._ohlcv_secondary_last_candle_ts: float = 0.0
        self._ohlcv_secondary_last_rows_queued: int = 0
        self._hmm_readiness_cache: dict[str, dict[str, Any]] = {}
        self._hmm_readiness_last_ts: dict[str, float] = {}
        self._hmm_detector: Any = None
        self._hmm_detector_secondary: Any = None
        self._hmm_module: Any = None
        self._hmm_numpy: Any = None
        self._regime_history_30m: deque[dict[str, Any]] = deque()
        self._regime_history_window_sec: float = 1800.0
        self._hmm_state: dict[str, Any] = self._hmm_default_state()
        self._hmm_state_secondary: dict[str, Any] = self._hmm_default_state(
            enabled=bool(getattr(config, "HMM_ENABLED", False))
            and bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)),
            interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
        )
        self._hmm_consensus: dict[str, Any] = dict(self._hmm_state)
        self._hmm_consensus.update({
            "agreement": "primary_only",
            "source_mode": "primary",
            "multi_timeframe": False,
        })
        self._hmm_training_depth: dict[str, Any] = self._hmm_training_depth_default(
            state_key="primary"
        )
        self._hmm_training_depth_secondary: dict[str, Any] = self._hmm_training_depth_default(
            state_key="secondary"
        )
        self._hmm_last_train_attempt_ts: float = 0.0
        self._hmm_last_train_attempt_ts_secondary: float = 0.0
        self._hmm_backfill_last_at: float = 0.0
        self._hmm_backfill_last_rows: int = 0
        self._hmm_backfill_last_message: str = ""
        self._hmm_backfill_stall_count: int = 0
        self._hmm_backfill_last_at_secondary: float = 0.0
        self._hmm_backfill_last_rows_secondary: int = 0
        self._hmm_backfill_last_message_secondary: str = ""
        self._hmm_backfill_stall_count_secondary: int = 0
        self._regime_tier: int = 0
        self._regime_tier_entered_at: float = 0.0
        self._regime_tier2_grace_start: float = 0.0
        self._regime_side_suppressed: str | None = None
        self._regime_last_eval_ts: float = 0.0
        self._regime_tier2_last_downgrade_at: float = 0.0
        self._regime_cooldown_suppressed_side: str | None = None
        self._regime_tier_history: list[dict[str, Any]] = []
        self._regime_mechanical_tier: int = 0
        self._regime_mechanical_direction: str = "symmetric"
        self._regime_mechanical_since: float = 0.0
        self._regime_mechanical_tier_entered_at: float = 0.0
        self._regime_mechanical_tier2_last_downgrade_at: float = 0.0
        self._ai_regime_last_run_ts: float = 0.0
        self._ai_regime_opinion: dict[str, Any] = {}
        self._ai_regime_history: deque[dict[str, Any]] = deque()
        self._ai_regime_dismissed: bool = False
        self._ai_regime_thread_alive: bool = False
        self._ai_regime_pending_result: dict[str, Any] | None = None
        self._ai_regime_last_mechanical_tier: int = 0
        self._ai_regime_last_mechanical_direction: str = "symmetric"
        self._ai_regime_last_consensus_agreement: str = "primary_only"
        self._ai_regime_last_trigger_reason: str = ""
        self._ai_override_tier: int | None = None
        self._ai_override_direction: str | None = None
        self._ai_override_until: float | None = None
        self._ai_override_applied_at: float | None = None
        self._ai_override_source_conviction: int | None = None
        self._regime_shadow_state: dict[str, Any] = {
            "enabled": False,
            "shadow_enabled": False,
            "actuation_enabled": False,
            "tier": 0,
            "regime": "RANGING",
            "confidence": 0.0,  # effective confidence used for tier gating
            "confidence_raw": 0.0,  # raw HMM confidence from detector/consensus
            "confidence_effective": 0.0,
            "confidence_modifier": 1.0,
            "confidence_modifier_source": "none",
            "bias_signal": 0.0,
            "abs_bias": 0.0,
            "suppressed_side": None,
            "favored_side": None,
            "directional_ok_tier1": False,
            "directional_ok_tier2": False,
            "hmm_ready": False,
            "last_eval_ts": 0.0,
            "reason": "init",
            "mechanical_tier": 0,
            "mechanical_direction": "symmetric",
            "override_active": False,
        }

        # Daily loss lock (aggregate bot-level, UTC day).
        self._daily_loss_lock_active: bool = False
        self._daily_loss_lock_utc_day: str = ""
        self._daily_realized_loss_utc: float = 0.0
        self._sticky_release_total: int = 0
        self._sticky_release_last_at: float = 0.0
        self._release_recon_blocked: bool = False
        self._release_recon_blocked_reason: str = ""
        self._kelly: KellySizer | None = None
        if bool(getattr(config, "KELLY_ENABLED", False)):
            self._kelly = KellySizer(
                KellyConfig(
                    kelly_fraction=float(getattr(config, "KELLY_FRACTION", 0.25)),
                    min_samples_total=int(getattr(config, "KELLY_MIN_SAMPLES", 30)),
                    min_samples_per_regime=int(getattr(config, "KELLY_MIN_REGIME_SAMPLES", 15)),
                    lookback_cycles=int(getattr(config, "KELLY_LOOKBACK", 500)),
                    kelly_floor_mult=float(getattr(config, "KELLY_FLOOR_MULT", 0.5)),
                    kelly_ceiling_mult=float(getattr(config, "KELLY_CEILING_MULT", 2.0)),
                    negative_edge_mult=float(getattr(config, "KELLY_NEGATIVE_EDGE_MULT", 0.5)),
                    use_recency_weighting=bool(getattr(config, "KELLY_RECENCY_WEIGHTING", True)),
                    recency_halflife_cycles=int(getattr(config, "KELLY_RECENCY_HALFLIFE", 100)),
                    log_kelly_updates=bool(getattr(config, "KELLY_LOG_UPDATES", True)),
                )
            )
        self._init_hmm_runtime()

    # ------------------ Config/State ------------------

    def _regime_entry_spacing_multipliers(self) -> tuple[float, float]:
        """
        Runtime policy hook for Tier-1 asymmetric spacing.

        Returns A/B entry spacing multipliers. Shadow mode must remain non-actuating.
        """
        if not bool(getattr(config, "REGIME_DIRECTIONAL_ENABLED", False)):
            return 1.0, 1.0
        if int(self._regime_tier) < 1:
            return 1.0, 1.0

        state = dict(self._regime_shadow_state or {})
        policy_regime, policy_confidence, policy_bias_signal, _, _ = self._policy_hmm_signal()
        regime = str(state.get("regime", policy_regime)).upper()
        if regime not in {"BULLISH", "BEARISH"}:
            return 1.0, 1.0

        hmm_mod = self._hmm_module
        if not hmm_mod or not hasattr(hmm_mod, "compute_grid_bias"):
            return 1.0, 1.0

        confidence = float(state.get("confidence", policy_confidence))
        bias_signal = float(state.get("bias_signal", policy_bias_signal))
        regime_stub = SimpleNamespace(confidence=confidence, bias_signal=bias_signal)
        try:
            bias = hmm_mod.compute_grid_bias(regime_stub)
            mult_a = float(bias.get("entry_spacing_mult_a", 1.0) or 1.0)
            mult_b = float(bias.get("entry_spacing_mult_b", 1.0) or 1.0)
        except Exception as e:
            logger.debug("Regime spacing bias unavailable: %s", e)
            return 1.0, 1.0

        if not isfinite(mult_a) or mult_a <= 0:
            mult_a = 1.0
        if not isfinite(mult_b) or mult_b <= 0:
            mult_b = 1.0

        return max(0.10, min(3.0, mult_a)), max(0.10, min(3.0, mult_b))

    def _engine_cfg(self, slot: SlotRuntime) -> sm.EngineConfig:
        spacing_mult_a, spacing_mult_b = self._regime_entry_spacing_multipliers()
        base_entry_pct = float(self.entry_pct)
        return sm.EngineConfig(
            entry_pct=base_entry_pct,
            entry_pct_a=base_entry_pct * spacing_mult_a,
            entry_pct_b=base_entry_pct * spacing_mult_b,
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
            reentry_base_cooldown_sec=float(config.REENTRY_BASE_COOLDOWN_SEC),
            backoff_factor=float(config.ENTRY_BACKOFF_FACTOR),
            backoff_max_multiplier=float(config.ENTRY_BACKOFF_MAX_MULTIPLIER),
            max_recovery_slots=max(1, int(config.MAX_RECOVERY_SLOTS)),
            sticky_mode_enabled=bool(config.STICKY_MODE_ENABLED),
        )

    def _allocate_slot_alias(self, used_aliases: set[str] | None = None) -> str:
        used = set(used_aliases or set())
        if used_aliases is None:
            for slot in self.slots.values():
                alias = str(slot.alias or "").strip().lower()
                if alias:
                    used.add(alias)

        pool = [str(a).strip().lower() for a in self.slot_alias_pool if str(a).strip()]
        if not pool:
            pool = ["wow"]

        recycled_set = {str(a).strip().lower() for a in self.slot_alias_recycle_queue if str(a).strip()}
        for alias in pool:
            if alias not in used and alias not in recycled_set:
                return alias

        while self.slot_alias_recycle_queue:
            alias = str(self.slot_alias_recycle_queue.popleft()).strip().lower()
            if not alias or alias in used:
                continue
            return alias

        while True:
            alias = f"doge-{self.slot_alias_fallback_counter:02d}"
            self.slot_alias_fallback_counter += 1
            if alias not in used:
                return alias

    def _release_slot_alias(self, alias: str) -> None:
        norm = str(alias or "").strip().lower()
        if not norm:
            return
        if norm not in self.slot_alias_pool:
            return
        if norm in self.slot_alias_recycle_queue:
            return
        self.slot_alias_recycle_queue.append(norm)

    def _slot_label(self, slot: SlotRuntime) -> str:
        alias = str(slot.alias or "").strip().lower()
        if alias:
            return alias
        return f"slot-{slot.slot_id}"

    def _sanitize_slot_alias_state(self) -> None:
        pool = [str(a).strip().lower() for a in self.slot_alias_pool if str(a).strip()]
        if not pool:
            pool = ["wow"]
        self.slot_alias_pool = tuple(pool)

        cleaned_queue: deque[str] = deque()
        seen_queue: set[str] = set()
        for raw in list(self.slot_alias_recycle_queue):
            alias = str(raw).strip().lower()
            if not alias or alias not in self.slot_alias_pool or alias in seen_queue:
                continue
            cleaned_queue.append(alias)
            seen_queue.add(alias)
        self.slot_alias_recycle_queue = cleaned_queue

        used: set[str] = set()
        for sid in sorted(self.slots.keys()):
            slot = self.slots[sid]
            alias = str(slot.alias or "").strip().lower()
            if alias and alias not in used:
                slot.alias = alias
                used.add(alias)
                continue
            slot.alias = self._allocate_slot_alias(used_aliases=used)
            used.add(slot.alias)

        self.slot_alias_recycle_queue = deque(a for a in self.slot_alias_recycle_queue if a not in used)

    def _capital_layer_step_doge_eq(self) -> float:
        return max(0.0, float(config.CAPITAL_LAYER_DOGE_PER_ORDER)) * max(1, int(config.CAPITAL_LAYER_ORDER_BUDGET))

    def _layer_mark_price(self, slot: SlotRuntime | None = None) -> float:
        if slot is not None:
            px = float(slot.state.market_price or 0.0)
            if px > 0:
                return px
        if self.last_price > 0:
            return float(self.last_price)
        return 0.0

    def _available_free_balances(self, *, prefer_fresh: bool = False) -> tuple[float, float]:
        if prefer_fresh:
            bal = self._safe_balance()
            if bal is not None:
                return max(0.0, _usd_balance(bal)), max(0.0, _doge_balance(bal))

        if self._loop_available_usd is not None and self._loop_available_doge is not None:
            return max(0.0, float(self._loop_available_usd)), max(0.0, float(self._loop_available_doge))

        if self.ledger._synced:
            return max(0.0, float(self.ledger.available_usd)), max(0.0, float(self.ledger.available_doge))

        if self._last_balance_snapshot:
            return (
                max(0.0, _usd_balance(self._last_balance_snapshot)),
                max(0.0, _doge_balance(self._last_balance_snapshot)),
            )
        return 0.0, 0.0

    def _active_order_side_counts(self) -> tuple[int, int, int]:
        sells = 0
        buys = 0
        total = 0
        for slot in self.slots.values():
            for o in slot.state.orders:
                if not o.txid:
                    continue
                total += 1
                if o.side == "sell":
                    sells += 1
                elif o.side == "buy":
                    buys += 1
            for r in slot.state.recovery_orders:
                if not r.txid:
                    continue
                total += 1
                if r.side == "sell":
                    sells += 1
                elif r.side == "buy":
                    buys += 1
        return sells, buys, total

    def _recompute_effective_layers(self, mark_price: float | None = None) -> dict[str, float | int | None]:
        doge_per_order = max(0.0, float(config.CAPITAL_LAYER_DOGE_PER_ORDER))
        layer_order_budget = max(1, int(config.CAPITAL_LAYER_ORDER_BUDGET))
        layer_step_doge_eq = doge_per_order * float(layer_order_budget)

        price = float(mark_price or 0.0)
        if price <= 0:
            price = self._layer_mark_price()

        free_usd, free_doge = self._available_free_balances(prefer_fresh=False)
        active_sell_orders, active_buy_orders, open_orders_total = self._active_order_side_counts()
        sell_den = max(1, active_sell_orders)
        buy_den = max(1, active_buy_orders)
        buffer = max(1.0, float(config.CAPITAL_LAYER_BALANCE_BUFFER))

        if doge_per_order <= 0:
            max_layers_from_doge = 0
            max_layers_from_usd = 0
        else:
            max_layers_from_doge = int(floor(free_doge / (sell_den * doge_per_order * buffer)))
            if price > 0:
                max_layers_from_usd = int(floor(free_usd / (buy_den * doge_per_order * price * buffer)))
            else:
                max_layers_from_usd = 0

        target_layers = max(0, int(self.target_layers))
        effective_layers = max(0, min(target_layers, max_layers_from_doge, max_layers_from_usd))
        self.effective_layers = int(effective_layers)

        gap_layers = max(0, target_layers - effective_layers)
        gap_doge_now = max(0.0, (target_layers - max_layers_from_doge) * sell_den * doge_per_order)
        gap_usd_now = max(0.0, (target_layers - max_layers_from_usd) * buy_den * doge_per_order * max(price, 0.0))

        return {
            "target_layers": target_layers,
            "effective_layers": effective_layers,
            "doge_per_order_per_layer": doge_per_order,
            "layer_order_budget": layer_order_budget,
            "layer_step_doge_eq": layer_step_doge_eq,
            "mark_price": price if price > 0 else None,
            "add_layer_usd_equiv_now": (layer_step_doge_eq * price) if price > 0 else None,
            "active_sell_orders": active_sell_orders,
            "active_buy_orders": active_buy_orders,
            "open_orders_total": open_orders_total,
            "max_layers_from_doge": max_layers_from_doge,
            "max_layers_from_usd": max_layers_from_usd,
            "gap_layers": gap_layers,
            "gap_doge_now": gap_doge_now,
            "gap_usd_now": gap_usd_now,
            "free_usd": free_usd,
            "free_doge": free_doge,
        }

    def _count_orders_at_funded_size(self) -> int:
        matched = 0
        for slot in self.slots.values():
            cfg = self._engine_cfg(slot)
            vol_decimals = int(cfg.volume_decimals)
            if vol_decimals <= 0:
                tol = 0.5
            else:
                tol = 0.5 * (10 ** (-vol_decimals))

            for o in slot.state.orders:
                if not o.txid:
                    continue
                trade = o.trade_id if o.trade_id in ("A", "B") else None
                target_usd = self._slot_order_size_usd(slot, trade_id=trade)
                expected_vol = sm.compute_order_volume(float(o.price), cfg, float(target_usd))
                if expected_vol is None:
                    continue
                if abs(float(o.volume) - float(expected_vol)) <= tol + 1e-12:
                    matched += 1

            for r in slot.state.recovery_orders:
                if not r.txid:
                    continue
                trade = r.trade_id if r.trade_id in ("A", "B") else None
                target_usd = self._slot_order_size_usd(slot, trade_id=trade)
                expected_vol = sm.compute_order_volume(float(r.price), cfg, float(target_usd))
                if expected_vol is None:
                    continue
                if abs(float(r.volume) - float(expected_vol)) <= tol + 1e-12:
                    matched += 1
        return matched

    def _slot_order_size_usd(self, slot: SlotRuntime, trade_id: str | None = None) -> float:
        base_order = float(config.ORDER_SIZE_USD)
        compound_mode = str(getattr(config, "STICKY_COMPOUNDING_MODE", "legacy_profit")).strip().lower()
        if bool(config.STICKY_MODE_ENABLED) and compound_mode == "fixed":
            base = max(base_order, base_order)
        else:
            # Independent compounding per slot.
            base = max(base_order, base_order + slot.state.total_profit)
        layer_metrics = self._recompute_effective_layers(mark_price=self._layer_mark_price(slot))
        effective_layers = int(layer_metrics.get("effective_layers", 0))
        layer_usd = 0.0
        layer_price = self._layer_mark_price(slot)
        if layer_price > 0:
            layer_usd = effective_layers * max(0.0, float(config.CAPITAL_LAYER_DOGE_PER_ORDER)) * layer_price
        base_with_layers = max(base, base + layer_usd)
        if self._kelly is not None:
            kelly_usd, _ = self._kelly.size_for_slot(
                base_with_layers,
                regime_label=self._kelly_regime_label(self._current_regime_id()),
            )
            base_with_layers = max(0.0, float(kelly_usd))
        if trade_id is None or not bool(config.REBALANCE_ENABLED):
            return base_with_layers

        skew = float(self._rebalancer_current_skew)
        if abs(skew) <= 1e-12:
            return base_with_layers

        favored = (skew > 0 and trade_id == "B") or (skew < 0 and trade_id == "A")
        if not favored:
            return base_with_layers

        sensitivity = max(0.0, float(config.REBALANCE_SIZE_SENSITIVITY))
        max_mult = max(1.0, float(config.REBALANCE_MAX_SIZE_MULT))
        mult = min(max_mult, 1.0 + abs(skew) * sensitivity)
        effective = base_with_layers * mult

        # Fund guard: scaling should not make an already-viable side non-viable.
        if skew > 0 and trade_id == "B":
            available_usd: float | None = None
            if self._loop_available_usd is not None:
                available_usd = float(self._loop_available_usd)
            elif self.ledger._synced:
                available_usd = float(self.ledger.available_usd)
            if available_usd is not None:
                max_safe = max(base_with_layers, available_usd - base_with_layers)
                effective = min(effective, max_safe)
        elif skew < 0 and trade_id == "A":
            price = float(slot.state.market_price or self.last_price)
            if price > 0:
                available_doge: float | None = None
                if self._loop_available_doge is not None:
                    available_doge = float(self._loop_available_doge)
                elif self.ledger._synced:
                    available_doge = float(self.ledger.available_doge)
                if available_doge is not None:
                    base_doge = base_with_layers / price
                    max_safe_doge = max(base_doge, available_doge - base_doge)
                    effective = min(effective, max_safe_doge * price)

        return max(base_with_layers, effective)

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

    def _compute_capacity_health(self, now: float | None = None) -> dict:
        now = now or _now()
        self._trim_rolling_telemetry(now)

        internal_open_orders_current = self._internal_open_order_count()
        kraken_open_orders_current = self._kraken_open_orders_current
        if kraken_open_orders_current is None:
            open_orders_current = internal_open_orders_current
            open_orders_source = "internal_fallback"
        else:
            open_orders_current = int(kraken_open_orders_current)
            open_orders_source = "kraken"

        pair_open_order_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
        safety_ratio = min(1.0, max(0.1, float(config.OPEN_ORDER_SAFETY_RATIO)))
        open_orders_safe_cap = max(1, int(pair_open_order_limit * safety_ratio))
        open_order_headroom = open_orders_safe_cap - open_orders_current
        open_order_utilization_pct = (
            open_orders_current / open_orders_safe_cap * 100.0 if open_orders_safe_cap > 0 else 0.0
        )
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

        return {
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
        }

    def _pending_entry_orders(self) -> list[tuple[int, sm.OrderState]]:
        pending: list[tuple[int, sm.OrderState]] = []
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            for o in st.orders:
                if o.role == "entry" and not o.txid:
                    pending.append((sid, o))
        pending.sort(key=lambda row: (float(row[1].placed_at or 0.0), int(row[0]), int(row[1].local_id)))
        return pending

    def _compute_entry_adds_loop_cap(self) -> int:
        base_cap = max(1, int(config.MAX_ENTRY_ADDS_PER_LOOP))
        try:
            capacity = self._compute_capacity_health()
            headroom = int(capacity.get("open_order_headroom") or 0)
        except Exception:
            return base_cap

        # Tighten entry velocity as we approach order-cap pressure.
        if headroom <= 5:
            return 1
        if headroom <= 10:
            return min(base_cap, 2)
        if headroom <= 20:
            return min(base_cap, 3)
        return base_cap

    def _defer_entry_due_scheduler(self, slot_id: int, action: sm.PlaceOrderAction, source: str) -> None:
        self._entry_adds_deferred_total += 1
        self._entry_adds_last_deferred_at = _now()
        logger.info(
            "entry_scheduler: deferred %s %s [%s.%s] slot=%s local=%s (cap %d/loop reached via %s)",
            action.role,
            action.side,
            action.trade_id,
            action.cycle,
            slot_id,
            action.local_id,
            self.entry_adds_per_loop_cap,
            source,
        )

    def _drain_pending_entry_orders(self, source: str, *, skip_stale: bool = False) -> None:
        if self.mode in ("PAUSED", "HALTED"):
            return
        if self.entry_adds_per_loop_used >= self.entry_adds_per_loop_cap:
            return
        if self._price_age_sec() > config.STALE_PRICE_MAX_AGE_SEC:
            return

        max_drift_pct = max(0.05, float(config.PAIR_REFRESH_PCT))
        pending = self._pending_entry_orders()
        if not pending:
            return

        # Purge suppressed-side deferred entries during Tier 2 after grace.
        if bool(getattr(config, "REGIME_DIRECTIONAL_ENABLED", False)) and self._regime_grace_elapsed(_now()):
            suppressed = self._regime_side_suppressed
            if suppressed in ("A", "B"):
                suppressed_side = "sell" if suppressed == "A" else "buy"
                purged = 0
                kept: list[tuple[int, sm.OrderState]] = []
                for sid, order in pending:
                    if order.side == suppressed_side:
                        slot = self.slots.get(sid)
                        if slot is not None:
                            current = sm.find_order(slot.state, order.local_id)
                            if current is not None and current.role == "entry" and not current.txid:
                                slot.state = sm.remove_order(slot.state, order.local_id)
                        purged += 1
                    else:
                        kept.append((sid, order))
                if purged > 0:
                    logger.info(
                        "entry_scheduler: purged %d suppressed-side (%s) deferred entries",
                        purged,
                        suppressed,
                    )
                pending = kept
                if not pending:
                    return

        drained = 0
        for sid, order in pending:
            if self.entry_adds_per_loop_used >= self.entry_adds_per_loop_cap:
                break
            slot = self.slots.get(sid)
            if slot is None:
                continue
            current = sm.find_order(slot.state, order.local_id)
            if current is None or current.role != "entry" or current.txid:
                continue

            if skip_stale and self.last_price > 0:
                drift = abs(current.price - self.last_price) / self.last_price * 100.0
                if drift > max_drift_pct:
                    continue

            action = sm.PlaceOrderAction(
                local_id=current.local_id,
                side=current.side,
                role="entry",
                price=current.price,
                volume=current.volume,
                trade_id=current.trade_id,
                cycle=current.cycle,
                reason="entry_scheduler_drain",
            )
            before = self.entry_adds_per_loop_used
            self._execute_actions(sid, [action], source)
            if self.entry_adds_per_loop_used > before:
                drained += 1

        if drained > 0:
            self._entry_adds_drained_total += drained
            self._entry_adds_last_drained_at = _now()
            logger.info(
                "entry_scheduler: drained %d pending entries via %s (used %d/%d this loop)",
                drained,
                source,
                self.entry_adds_per_loop_used,
                self.entry_adds_per_loop_cap,
            )

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
        snap = {
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
            "target_layers": int(self.target_layers),
            "effective_layers": int(self.effective_layers),
            "layer_last_add_event": self.layer_last_add_event,
            "slot_alias_recycle_queue": list(self.slot_alias_recycle_queue),
            "slot_alias_fallback_counter": int(self.slot_alias_fallback_counter),
            "slots": {str(sid): sm.to_dict(slot.state) for sid, slot in self.slots.items()},
            "slot_aliases": {str(sid): str(slot.alias or "").strip().lower() for sid, slot in self.slots.items()},
            "recon_baseline": self._recon_baseline,
            "rebalancer_idle_ratio": self._rebalancer_idle_ratio,
            "rebalancer_smoothed_error": self._rebalancer_smoothed_error,
            "rebalancer_smoothed_velocity": self._rebalancer_smoothed_velocity,
            "rebalancer_current_skew": self._rebalancer_current_skew,
            "rebalancer_last_update_ts": self._rebalancer_last_update_ts,
            "rebalancer_last_raw_error": self._rebalancer_last_raw_error,
            "rebalancer_sign_flip_history": list(self._rebalancer_sign_flip_history)[-20:],
            "rebalancer_damped_until": self._rebalancer_damped_until,
            "trend_fast_ema": self._trend_fast_ema,
            "trend_slow_ema": self._trend_slow_ema,
            "trend_score": self._trend_score,
            "trend_dynamic_target": self._trend_dynamic_target,
            "trend_smoothed_target": self._trend_smoothed_target,
            "trend_target_locked_until": self._trend_target_locked_until,
            "trend_last_update_ts": self._trend_last_update_ts,
            "ohlcv_since_cursor": self._ohlcv_since_cursor,
            "ohlcv_last_sync_ts": self._ohlcv_last_sync_ts,
            "ohlcv_last_candle_ts": self._ohlcv_last_candle_ts,
            "ohlcv_secondary_since_cursor": self._ohlcv_secondary_since_cursor,
            "ohlcv_secondary_last_sync_ts": self._ohlcv_secondary_last_sync_ts,
            "ohlcv_secondary_last_candle_ts": self._ohlcv_secondary_last_candle_ts,
            "ohlcv_secondary_last_rows_queued": self._ohlcv_secondary_last_rows_queued,
            "hmm_state_secondary": dict(self._hmm_state_secondary or {}),
            "hmm_consensus": dict(self._hmm_consensus or {}),
            "hmm_backfill_last_at": self._hmm_backfill_last_at,
            "hmm_backfill_last_rows": self._hmm_backfill_last_rows,
            "hmm_backfill_last_message": self._hmm_backfill_last_message,
            "hmm_backfill_stall_count": self._hmm_backfill_stall_count,
            "hmm_backfill_last_at_secondary": self._hmm_backfill_last_at_secondary,
            "hmm_backfill_last_rows_secondary": self._hmm_backfill_last_rows_secondary,
            "hmm_backfill_last_message_secondary": self._hmm_backfill_last_message_secondary,
            "hmm_backfill_stall_count_secondary": self._hmm_backfill_stall_count_secondary,
            "regime_tier": int(self._regime_tier),
            "regime_tier_entered_at": float(self._regime_tier_entered_at),
            "regime_tier2_grace_start": float(self._regime_tier2_grace_start),
            "regime_side_suppressed": self._regime_side_suppressed,
            "regime_last_eval_ts": float(self._regime_last_eval_ts),
            "regime_tier2_last_downgrade_at": float(self._regime_tier2_last_downgrade_at),
            "regime_cooldown_suppressed_side": self._regime_cooldown_suppressed_side,
            "regime_tier_history": list(self._regime_tier_history[-20:]),
            "regime_shadow_state": dict(self._regime_shadow_state or {}),
            "regime_mechanical_tier": int(self._regime_mechanical_tier),
            "regime_mechanical_direction": str(self._regime_mechanical_direction),
            "regime_mechanical_since": float(self._regime_mechanical_since),
            "regime_mechanical_tier_entered_at": float(self._regime_mechanical_tier_entered_at),
            "regime_mechanical_tier2_last_downgrade_at": float(
                self._regime_mechanical_tier2_last_downgrade_at
            ),
            "ai_override_tier": self._ai_override_tier,
            "ai_override_direction": self._ai_override_direction,
            "ai_override_until": self._ai_override_until,
            "ai_override_applied_at": self._ai_override_applied_at,
            "ai_override_source_conviction": self._ai_override_source_conviction,
            "entry_adds_deferred_total": self._entry_adds_deferred_total,
            "entry_adds_drained_total": self._entry_adds_drained_total,
            "entry_adds_last_deferred_at": self._entry_adds_last_deferred_at,
            "entry_adds_last_drained_at": self._entry_adds_last_drained_at,
            "daily_loss_lock_active": bool(self._daily_loss_lock_active),
            "daily_loss_lock_utc_day": str(self._daily_loss_lock_utc_day or ""),
            "daily_realized_loss_utc": float(self._daily_realized_loss_utc),
            "sticky_release_total": int(self._sticky_release_total),
            "sticky_release_last_at": float(self._sticky_release_last_at),
            "release_recon_blocked": bool(self._release_recon_blocked),
            "release_recon_blocked_reason": str(self._release_recon_blocked_reason or ""),
        }
        if self._kelly is not None:
            snap["kelly_state"] = self._kelly.snapshot_state()
        snap.update(self._snapshot_hmm_state())
        return snap

    def _save_local_runtime_snapshot(self, snapshot: dict) -> None:
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            tmp_path = _BOT_RUNTIME_STATE_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=True, separators=(",", ":"))
            os.replace(tmp_path, _BOT_RUNTIME_STATE_FILE)
        except Exception as e:
            logger.warning("Local runtime snapshot write failed: %s", e)

    def _load_local_runtime_snapshot(self) -> dict:
        try:
            with open(_BOT_RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save_snapshot(self) -> None:
        snap = self._global_snapshot()
        supabase_store.save_state(snap, pair="__v1__")
        self._save_local_runtime_snapshot(snap)

    def _load_snapshot(self) -> None:
        try:
            snap = supabase_store.load_state(pair="__v1__") or {}
        except Exception as e:
            logger.warning("Supabase snapshot load failed: %s", e)
            snap = {}
        if not snap:
            snap = self._load_local_runtime_snapshot()
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
            self.target_layers = max(0, int(snap.get("target_layers", self.target_layers)))
            self.effective_layers = max(0, int(snap.get("effective_layers", self.effective_layers)))
            raw_last_add = snap.get("layer_last_add_event", self.layer_last_add_event)
            self.layer_last_add_event = raw_last_add if isinstance(raw_last_add, dict) else None
            self.slot_alias_fallback_counter = max(
                1,
                int(snap.get("slot_alias_fallback_counter", self.slot_alias_fallback_counter)),
            )
            raw_alias_queue = snap.get("slot_alias_recycle_queue", list(self.slot_alias_recycle_queue))
            self.slot_alias_recycle_queue = deque()
            if isinstance(raw_alias_queue, list):
                for alias in raw_alias_queue:
                    norm = str(alias).strip().lower()
                    if norm:
                        self.slot_alias_recycle_queue.append(norm)
            self._recon_baseline = snap.get("recon_baseline", None)
            self._rebalancer_idle_ratio = float(snap.get("rebalancer_idle_ratio", self._rebalancer_idle_ratio))
            self._rebalancer_smoothed_error = float(
                snap.get("rebalancer_smoothed_error", self._rebalancer_smoothed_error)
            )
            self._rebalancer_smoothed_velocity = float(
                snap.get("rebalancer_smoothed_velocity", self._rebalancer_smoothed_velocity)
            )
            self._rebalancer_current_skew = float(snap.get("rebalancer_current_skew", self._rebalancer_current_skew))
            self._rebalancer_last_update_ts = float(
                snap.get("rebalancer_last_update_ts", self._rebalancer_last_update_ts)
            )
            self._rebalancer_last_raw_error = float(
                snap.get("rebalancer_last_raw_error", self._rebalancer_last_raw_error)
            )
            self._rebalancer_damped_until = float(snap.get("rebalancer_damped_until", self._rebalancer_damped_until))
            self._trend_fast_ema = float(snap.get("trend_fast_ema", self._trend_fast_ema))
            self._trend_slow_ema = float(snap.get("trend_slow_ema", self._trend_slow_ema))
            self._trend_score = float(snap.get("trend_score", self._trend_score))
            self._trend_dynamic_target = float(snap.get("trend_dynamic_target", self._trend_dynamic_target))
            self._trend_smoothed_target = float(snap.get("trend_smoothed_target", self._trend_smoothed_target))
            self._trend_target_locked_until = float(
                snap.get("trend_target_locked_until", self._trend_target_locked_until)
            )
            self._trend_last_update_ts = float(snap.get("trend_last_update_ts", self._trend_last_update_ts))
            raw_cursor = snap.get("ohlcv_since_cursor", self._ohlcv_since_cursor)
            try:
                self._ohlcv_since_cursor = int(raw_cursor) if raw_cursor is not None else None
            except (TypeError, ValueError):
                self._ohlcv_since_cursor = None
            raw_secondary_cursor = snap.get("ohlcv_secondary_since_cursor", self._ohlcv_secondary_since_cursor)
            try:
                self._ohlcv_secondary_since_cursor = int(raw_secondary_cursor) if raw_secondary_cursor is not None else None
            except (TypeError, ValueError):
                self._ohlcv_secondary_since_cursor = None
            self._ohlcv_last_sync_ts = float(snap.get("ohlcv_last_sync_ts", self._ohlcv_last_sync_ts))
            self._ohlcv_last_candle_ts = float(snap.get("ohlcv_last_candle_ts", self._ohlcv_last_candle_ts))
            self._ohlcv_secondary_last_sync_ts = float(
                snap.get("ohlcv_secondary_last_sync_ts", self._ohlcv_secondary_last_sync_ts)
            )
            self._ohlcv_secondary_last_candle_ts = float(
                snap.get("ohlcv_secondary_last_candle_ts", self._ohlcv_secondary_last_candle_ts)
            )
            self._ohlcv_secondary_last_rows_queued = int(
                snap.get("ohlcv_secondary_last_rows_queued", self._ohlcv_secondary_last_rows_queued)
            )
            self._hmm_backfill_last_at = float(snap.get("hmm_backfill_last_at", self._hmm_backfill_last_at))
            self._hmm_backfill_last_rows = int(snap.get("hmm_backfill_last_rows", self._hmm_backfill_last_rows))
            self._hmm_backfill_last_message = str(
                snap.get("hmm_backfill_last_message", self._hmm_backfill_last_message) or ""
            )
            self._hmm_backfill_stall_count = int(
                snap.get("hmm_backfill_stall_count", self._hmm_backfill_stall_count)
            )
            self._hmm_backfill_last_at_secondary = float(
                snap.get("hmm_backfill_last_at_secondary", self._hmm_backfill_last_at_secondary)
            )
            self._hmm_backfill_last_rows_secondary = int(
                snap.get("hmm_backfill_last_rows_secondary", self._hmm_backfill_last_rows_secondary)
            )
            self._hmm_backfill_last_message_secondary = str(
                snap.get(
                    "hmm_backfill_last_message_secondary",
                    self._hmm_backfill_last_message_secondary,
                )
                or ""
            )
            self._hmm_backfill_stall_count_secondary = int(
                snap.get(
                    "hmm_backfill_stall_count_secondary",
                    self._hmm_backfill_stall_count_secondary,
                )
            )
            raw_hmm_state_secondary = snap.get("hmm_state_secondary", self._hmm_state_secondary)
            if isinstance(raw_hmm_state_secondary, dict):
                self._hmm_state_secondary = dict(raw_hmm_state_secondary)
            raw_hmm_consensus = snap.get("hmm_consensus", self._hmm_consensus)
            if isinstance(raw_hmm_consensus, dict):
                self._hmm_consensus = dict(raw_hmm_consensus)
            self._regime_tier = int(snap.get("regime_tier", self._regime_tier))
            self._regime_tier = max(0, min(2, self._regime_tier))
            self._regime_tier_entered_at = float(snap.get("regime_tier_entered_at", self._regime_tier_entered_at))
            raw_grace_start = snap.get("regime_tier2_grace_start", self._regime_tier2_grace_start)
            self._regime_tier2_grace_start = float(raw_grace_start or 0.0)
            if self._regime_tier == 2 and self._regime_tier2_grace_start <= 0.0:
                self._regime_tier2_grace_start = float(self._regime_tier_entered_at)
            if self._regime_tier != 2:
                self._regime_tier2_grace_start = 0.0
            raw_suppressed = snap.get("regime_side_suppressed", self._regime_side_suppressed)
            self._regime_side_suppressed = raw_suppressed if raw_suppressed in ("A", "B", None) else None
            self._regime_last_eval_ts = float(snap.get("regime_last_eval_ts", self._regime_last_eval_ts))
            self._regime_tier2_last_downgrade_at = float(
                snap.get("regime_tier2_last_downgrade_at", self._regime_tier2_last_downgrade_at) or 0.0
            )
            raw_cooldown_side = snap.get("regime_cooldown_suppressed_side", self._regime_cooldown_suppressed_side)
            self._regime_cooldown_suppressed_side = (
                raw_cooldown_side if raw_cooldown_side in ("A", "B", None) else None
            )
            raw_tier_history = snap.get("regime_tier_history", self._regime_tier_history)
            if isinstance(raw_tier_history, list):
                self._regime_tier_history = list(raw_tier_history[-20:])
            raw_regime_shadow_state = snap.get("regime_shadow_state", self._regime_shadow_state)
            if isinstance(raw_regime_shadow_state, dict):
                self._regime_shadow_state = dict(raw_regime_shadow_state)
            self._regime_mechanical_tier = max(
                0,
                min(2, int(snap.get("regime_mechanical_tier", self._regime_mechanical_tier))),
            )
            raw_mech_dir = str(
                snap.get("regime_mechanical_direction", self._regime_mechanical_direction) or "symmetric"
            ).strip().lower()
            if raw_mech_dir not in {"symmetric", "long_bias", "short_bias"}:
                raw_mech_dir = "symmetric"
            self._regime_mechanical_direction = raw_mech_dir
            self._regime_mechanical_since = float(
                snap.get("regime_mechanical_since", self._regime_mechanical_since) or 0.0
            )
            self._regime_mechanical_tier_entered_at = float(
                snap.get(
                    "regime_mechanical_tier_entered_at",
                    self._regime_mechanical_tier_entered_at,
                )
                or 0.0
            )
            self._regime_mechanical_tier2_last_downgrade_at = float(
                snap.get(
                    "regime_mechanical_tier2_last_downgrade_at",
                    self._regime_mechanical_tier2_last_downgrade_at,
                )
                or 0.0
            )
            raw_override_tier = snap.get("ai_override_tier", self._ai_override_tier)
            try:
                self._ai_override_tier = (
                    max(0, min(2, int(raw_override_tier)))
                    if raw_override_tier is not None
                    else None
                )
            except (TypeError, ValueError):
                self._ai_override_tier = None
            raw_override_dir = snap.get("ai_override_direction", self._ai_override_direction)
            raw_override_dir = str(raw_override_dir).strip().lower() if raw_override_dir is not None else ""
            if raw_override_dir not in {"symmetric", "long_bias", "short_bias"}:
                self._ai_override_direction = None
            else:
                self._ai_override_direction = raw_override_dir
            raw_override_until = snap.get("ai_override_until", self._ai_override_until)
            try:
                self._ai_override_until = float(raw_override_until) if raw_override_until is not None else None
            except (TypeError, ValueError):
                self._ai_override_until = None
            raw_override_applied_at = snap.get("ai_override_applied_at", self._ai_override_applied_at)
            try:
                self._ai_override_applied_at = (
                    float(raw_override_applied_at) if raw_override_applied_at is not None else None
                )
            except (TypeError, ValueError):
                self._ai_override_applied_at = None
            raw_source_conv = snap.get("ai_override_source_conviction", self._ai_override_source_conviction)
            try:
                self._ai_override_source_conviction = (
                    max(0, min(100, int(raw_source_conv)))
                    if raw_source_conv is not None
                    else None
                )
            except (TypeError, ValueError):
                self._ai_override_source_conviction = None
            if self._ai_override_until is not None and float(self._ai_override_until) <= _now():
                self._clear_ai_override()
            self._entry_adds_deferred_total = int(snap.get("entry_adds_deferred_total", self._entry_adds_deferred_total))
            self._entry_adds_drained_total = int(snap.get("entry_adds_drained_total", self._entry_adds_drained_total))
            self._entry_adds_last_deferred_at = float(
                snap.get("entry_adds_last_deferred_at", self._entry_adds_last_deferred_at)
            )
            self._entry_adds_last_drained_at = float(
                snap.get("entry_adds_last_drained_at", self._entry_adds_last_drained_at)
            )
            self._daily_loss_lock_active = bool(snap.get("daily_loss_lock_active", self._daily_loss_lock_active))
            self._daily_loss_lock_utc_day = str(snap.get("daily_loss_lock_utc_day", self._daily_loss_lock_utc_day) or "")
            self._daily_realized_loss_utc = float(snap.get("daily_realized_loss_utc", self._daily_realized_loss_utc))
            self._sticky_release_total = int(snap.get("sticky_release_total", self._sticky_release_total))
            self._sticky_release_last_at = float(snap.get("sticky_release_last_at", self._sticky_release_last_at))
            self._release_recon_blocked = bool(snap.get("release_recon_blocked", self._release_recon_blocked))
            self._release_recon_blocked_reason = str(
                snap.get("release_recon_blocked_reason", self._release_recon_blocked_reason) or ""
            )
            hist = snap.get("rebalancer_sign_flip_history", [])
            cleaned_hist: list[float] = []
            if isinstance(hist, list):
                for row in hist:
                    try:
                        cleaned_hist.append(float(row))
                    except Exception:
                        continue
            self._rebalancer_sign_flip_history = deque(sorted(cleaned_hist)[-20:])
            self._restore_hmm_snapshot(snap)
            self._hmm_consensus = self._compute_hmm_consensus()
            if self._kelly is not None:
                self._kelly.restore_state(snap.get("kelly_state", {}))

            self.slots = {}
            slot_aliases = snap.get("slot_aliases", {})
            slot_aliases = slot_aliases if isinstance(slot_aliases, dict) else {}
            for sid_text, raw_state in (snap.get("slots", {}) or {}).items():
                sid = int(sid_text)
                alias = str(slot_aliases.get(str(sid)) or slot_aliases.get(sid) or "").strip().lower()
                self.slots[sid] = SlotRuntime(slot_id=sid, state=sm.from_dict(raw_state), alias=alias)

            self._sanitize_slot_alias_state()
            self._recompute_effective_layers()

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
        self.entry_adds_per_loop_used = 0
        self.entry_adds_per_loop_cap = self._compute_entry_adds_loop_cap()
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
        self.entry_adds_per_loop_used = 0
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
        self._sanitize_slot_alias_state()

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
                alias=self._allocate_slot_alias(),
            )
            self.next_slot_id = 1

        # Get initial market price.
        self._refresh_price(strict=True)
        # Prime OHLCV + optional one-time backfill + HMM state before entering the loop.
        self._sync_ohlcv_candles(_now())
        self._maybe_backfill_ohlcv_on_startup()
        self._hmm_readiness_cache = {}
        self._hmm_readiness_last_ts = {}
        startup_now = _now()
        self._update_hmm(startup_now)
        self._update_regime_tier(startup_now)

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

        self._recompute_effective_layers()

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

    def resume(self) -> tuple[bool, str]:
        self._update_daily_loss_lock(_now())
        if self._daily_loss_lock_active:
            msg = (
                "daily loss lock active "
                f"(UTC {self._daily_loss_lock_utc_day or self._utc_day_key()}); manual resume available after rollover"
            )
            self.pause_reason = msg
            return False, msg
        if self.mode == "HALTED":
            return False, "bot halted"
        self.mode = "RUNNING"
        self.pause_reason = ""
        self.consecutive_api_errors = 0
        notifier.notify_risk_event("resume", "Resumed by operator", self.pair_display)
        return True, "running"

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

    @staticmethod
    def _hmm_quality_tier(
        current_candles: int,
        target_candles: int,
        min_train_samples: int,
    ) -> tuple[str, float]:
        current = max(0, int(current_candles))
        target = max(1, int(target_candles))
        min_train = max(1, int(min_train_samples))

        if current >= target:
            return "full", 1.00

        baseline_threshold = max(min_train, int(round(target * 0.25)))
        deep_threshold = max(min_train, int(round(target * 0.625)))
        if deep_threshold <= baseline_threshold:
            deep_threshold = baseline_threshold + 1

        if current >= deep_threshold:
            return "deep", 0.95
        if current >= baseline_threshold:
            return "baseline", 0.85
        return "shallow", 0.70

    def _hmm_training_depth_default(self, *, state_key: str = "primary") -> dict[str, Any]:
        use_secondary = str(state_key).lower() == "secondary"
        if use_secondary:
            target_candles = max(1, int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 1440)))
            min_train_samples = max(1, int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)))
            interval_min = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
            key = "secondary"
        else:
            target_candles = max(1, int(getattr(config, "HMM_TRAINING_CANDLES", 4000)))
            min_train_samples = max(1, int(getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)))
            interval_min = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
            key = "primary"

        quality_tier, modifier = self._hmm_quality_tier(
            0,
            target_candles,
            min_train_samples,
        )
        return {
            "state_key": key,
            "current_candles": 0,
            "target_candles": target_candles,
            "min_train_samples": min_train_samples,
            "quality_tier": quality_tier,
            "confidence_modifier": modifier,
            "pct_complete": 0.0,
            "interval_min": interval_min,
            "estimated_full_at": None,
            "updated_at": 0.0,
        }

    def _update_hmm_training_depth(
        self,
        *,
        current_candles: int,
        secondary: bool = False,
        target_candles: int | None = None,
        min_train_samples: int | None = None,
        interval_min: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now_ts = float(now if now is not None else _now())
        if secondary:
            target = max(
                1,
                int(
                    target_candles
                    if target_candles is not None
                    else getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 1440)
                ),
            )
            min_train = max(
                1,
                int(
                    min_train_samples
                    if min_train_samples is not None
                    else getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)
                ),
            )
            interval = max(
                1,
                int(
                    interval_min
                    if interval_min is not None
                    else getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)
                ),
            )
            state_key = "secondary"
        else:
            target = max(
                1,
                int(
                    target_candles
                    if target_candles is not None
                    else getattr(config, "HMM_TRAINING_CANDLES", 4000)
                ),
            )
            min_train = max(
                1,
                int(
                    min_train_samples
                    if min_train_samples is not None
                    else getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)
                ),
            )
            interval = max(
                1,
                int(
                    interval_min
                    if interval_min is not None
                    else getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)
                ),
            )
            state_key = "primary"

        current = max(0, int(current_candles))
        quality_tier, modifier = self._hmm_quality_tier(current, target, min_train)
        pct_complete = min(100.0, (current / target * 100.0)) if target > 0 else 100.0

        estimated_full_at: str | None = None
        if current < target:
            remaining = target - current
            eta_ts = now_ts + float(remaining * interval * 60)
            estimated_full_at = datetime.fromtimestamp(eta_ts, timezone.utc).isoformat()

        out = {
            "state_key": state_key,
            "current_candles": current,
            "target_candles": target,
            "min_train_samples": min_train,
            "quality_tier": quality_tier,
            "confidence_modifier": float(modifier),
            "pct_complete": round(float(pct_complete), 2),
            "interval_min": interval,
            "estimated_full_at": estimated_full_at,
            "updated_at": float(now_ts),
        }
        if secondary:
            self._hmm_training_depth_secondary = out
        else:
            self._hmm_training_depth = out
        return out

    def _hmm_confidence_modifier_for_source(self, source: dict[str, Any] | None) -> tuple[float, str]:
        if not isinstance(source, dict):
            return 1.0, "default"
        source_mode = str(source.get("source_mode", "") or "").strip().lower()
        multi_enabled = bool(source.get("multi_timeframe", False))
        if source_mode == "consensus" and multi_enabled:
            primary_mod = float(self._hmm_training_depth.get("confidence_modifier", 1.0) or 1.0)
            secondary_mod = float(
                self._hmm_training_depth_secondary.get("confidence_modifier", 1.0) or 1.0
            )
            return max(0.0, min(1.0, min(primary_mod, secondary_mod))), "consensus_min"
        primary_mod = float(self._hmm_training_depth.get("confidence_modifier", 1.0) or 1.0)
        return max(0.0, min(1.0, primary_mod)), "primary"

    def _record_regime_history_sample(self, now: float | None = None) -> None:
        now_ts = float(now if now is not None else _now())
        source = dict(self._policy_hmm_source() or {})
        regime = str(source.get("regime", "RANGING") or "RANGING").upper()
        confidence = max(0.0, min(1.0, float(source.get("confidence", 0.0) or 0.0)))
        bias = max(-1.0, min(1.0, float(source.get("bias_signal", 0.0) or 0.0)))

        self._regime_history_30m.append(
            {
                "ts": now_ts,
                "regime": regime,
                "conf": round(confidence, 4),
                "bias": round(bias, 4),
            }
        )

        cutoff = now_ts - float(self._regime_history_window_sec)
        while self._regime_history_30m and float(self._regime_history_30m[0].get("ts", 0.0)) < cutoff:
            self._regime_history_30m.popleft()
        if len(self._regime_history_30m) > 512:
            while len(self._regime_history_30m) > 512:
                self._regime_history_30m.popleft()

    def _hmm_default_state(
        self,
        *,
        enabled: bool | None = None,
        interval_min: int | None = None,
    ) -> dict[str, Any]:
        blend = max(0.0, min(1.0, float(getattr(config, "HMM_BLEND_WITH_TREND", 0.5))))
        use_enabled = bool(getattr(config, "HMM_ENABLED", False)) if enabled is None else bool(enabled)
        use_interval = max(
            1,
            int(
                getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)
                if interval_min is None
                else interval_min
            ),
        )
        return {
            "enabled": use_enabled,
            "available": False,
            "trained": False,
            "interval_min": use_interval,
            "regime": "RANGING",
            "regime_id": 1,
            "confidence": 0.0,
            "bias_signal": 0.0,
            "probabilities": {
                "bearish": 0.0,
                "ranging": 1.0,
                "bullish": 0.0,
            },
            "observation_count": 0,
            "blend_factor": blend,
            "last_update_ts": 0.0,
            "last_train_ts": 0.0,
            "agreement": "single",
            "source_mode": "primary",
            "multi_timeframe": False,
            "error": "",
        }

    def _hmm_source_mode(self) -> str:
        raw = str(getattr(config, "HMM_MULTI_TIMEFRAME_SOURCE", "primary") or "primary").strip().lower()
        mode = "consensus" if raw == "consensus" else "primary"
        if mode == "consensus" and not bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)):
            return "primary"
        return mode

    def _policy_hmm_source(self) -> dict[str, Any]:
        primary = self._hmm_state if isinstance(self._hmm_state, dict) else self._hmm_default_state()
        if not bool(getattr(config, "HMM_ENABLED", False)):
            return primary
        if not bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)):
            return primary
        if self._hmm_source_mode() != "consensus":
            return primary
        if isinstance(self._hmm_consensus, dict) and self._hmm_consensus:
            return self._hmm_consensus
        return primary

    def _policy_hmm_signal(self) -> tuple[str, float, float, bool, dict[str, Any]]:
        source = dict(self._policy_hmm_source() or {})
        regime = str(source.get("regime", "RANGING") or "RANGING").upper()
        confidence = max(0.0, min(1.0, float(source.get("confidence", 0.0) or 0.0)))
        bias = float(source.get("bias_signal", 0.0) or 0.0)
        ready = bool(
            bool(getattr(config, "HMM_ENABLED", False))
            and bool(source.get("available"))
            and bool(source.get("trained"))
        )
        return regime, confidence, bias, ready, source

    def _current_regime_id(self) -> int | None:
        if not bool(getattr(config, "HMM_ENABLED", False)):
            return None
        source = dict(self._policy_hmm_source() or {})
        if not (bool(source.get("available")) and bool(source.get("trained"))):
            return None
        try:
            regime_id = int(source.get("regime_id", 1))
        except (TypeError, ValueError):
            return None
        return regime_id if regime_id in (0, 1, 2) else None

    @staticmethod
    def _kelly_regime_label(regime_id: int | None) -> str:
        return {0: "bearish", 1: "ranging", 2: "bullish"}.get(regime_id, "ranging")

    def _collect_kelly_cycles(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for slot in self.slots.values():
            for c in slot.state.completed_cycles:
                rows.append(
                    {
                        "net_profit": float(c.net_profit),
                        "regime_at_entry": c.regime_at_entry,
                        "exit_time": float(c.exit_time or 0.0),
                    }
                )
        return rows

    def _update_kelly(self) -> None:
        if self._kelly is None:
            return
        rows = self._collect_kelly_cycles()
        self._kelly.update(
            rows,
            regime_label=self._kelly_regime_label(self._current_regime_id()),
        )

    def _hmm_runtime_config(self, *, min_train_samples: int | None = None) -> dict[str, Any]:
        resolved_min_samples = max(
            50,
            int(
                getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)
                if min_train_samples is None
                else min_train_samples
            ),
        )
        return {
            "HMM_N_STATES": max(2, int(getattr(config, "HMM_N_STATES", 3))),
            "HMM_N_ITER": max(10, int(getattr(config, "HMM_N_ITER", 100))),
            "HMM_COVARIANCE_TYPE": str(getattr(config, "HMM_COVARIANCE_TYPE", "diag") or "diag"),
            "HMM_INFERENCE_WINDOW": max(5, int(getattr(config, "HMM_INFERENCE_WINDOW", 50))),
            "HMM_CONFIDENCE_THRESHOLD": max(
                0.0, float(getattr(config, "HMM_CONFIDENCE_THRESHOLD", 0.15))
            ),
            "HMM_RETRAIN_INTERVAL_SEC": max(
                300.0, float(getattr(config, "HMM_RETRAIN_INTERVAL_SEC", 86400.0))
            ),
            "HMM_MIN_TRAIN_SAMPLES": resolved_min_samples,
            "HMM_BIAS_GAIN": max(0.0, float(getattr(config, "HMM_BIAS_GAIN", 1.0))),
            "HMM_BLEND_WITH_TREND": max(
                0.0, min(1.0, float(getattr(config, "HMM_BLEND_WITH_TREND", 0.5)))
            ),
        }

    def _refresh_hmm_state_from_detector(self, *, secondary: bool = False) -> None:
        detector = self._hmm_detector_secondary if secondary else self._hmm_detector
        if secondary:
            if not isinstance(self._hmm_state_secondary, dict):
                self._hmm_state_secondary = self._hmm_default_state(
                    enabled=bool(getattr(config, "HMM_ENABLED", False))
                    and bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)),
                    interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
                )
            state = self._hmm_state_secondary
            enabled_flag = bool(getattr(config, "HMM_ENABLED", False)) and bool(
                getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)
            )
            interval_min = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        else:
            if not isinstance(self._hmm_state, dict):
                self._hmm_state = self._hmm_default_state(
                    enabled=bool(getattr(config, "HMM_ENABLED", False)),
                    interval_min=max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
                )
            state = self._hmm_state
            enabled_flag = bool(getattr(config, "HMM_ENABLED", False))
            interval_min = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))

        if not detector:
            state["enabled"] = enabled_flag
            state["available"] = False
            state["trained"] = False
            state["interval_min"] = interval_min
            state["blend_factor"] = max(
                0.0, min(1.0, float(getattr(config, "HMM_BLEND_WITH_TREND", 0.5)))
            )
            return

        st = getattr(detector, "state", None)
        probs = [0.0, 1.0, 0.0]
        if st is not None:
            raw_probs = list(getattr(st, "probabilities", []) or [])
            if len(raw_probs) >= 3:
                try:
                    probs = [float(raw_probs[0]), float(raw_probs[1]), float(raw_probs[2])]
                except (TypeError, ValueError):
                    probs = [0.0, 1.0, 0.0]

        regime_id = 1
        if st is not None:
            try:
                regime_id = int(getattr(st, "regime", 1))
            except (TypeError, ValueError):
                regime_id = 1
        regime_name = "RANGING"
        try:
            if self._hmm_module and hasattr(self._hmm_module, "Regime"):
                regime_name = str(self._hmm_module.Regime(regime_id).name)
            else:
                regime_name = {0: "BEARISH", 1: "RANGING", 2: "BULLISH"}.get(regime_id, "RANGING")
        except Exception:
            regime_name = "RANGING"

        state.update({
            "enabled": enabled_flag,
            "available": True,
            "trained": bool(getattr(detector, "_trained", False)),
            "interval_min": interval_min,
            "regime": regime_name,
            "regime_id": regime_id,
            "confidence": float(getattr(st, "confidence", 0.0) if st is not None else 0.0),
            "bias_signal": float(getattr(st, "bias_signal", 0.0) if st is not None else 0.0),
            "probabilities": {
                "bearish": probs[0],
                "ranging": probs[1],
                "bullish": probs[2],
            },
            "observation_count": int(getattr(st, "observation_count", 0) if st is not None else 0),
            "blend_factor": max(
                0.0, min(1.0, float(getattr(config, "HMM_BLEND_WITH_TREND", 0.5)))
            ),
            "last_update_ts": float(getattr(st, "last_update_ts", 0.0) if st is not None else 0.0),
            "last_train_ts": float(getattr(detector, "_last_train_ts", 0.0) or 0.0),
        })

    def _init_hmm_runtime(self) -> None:
        primary_interval = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
        secondary_interval = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        hmm_enabled = bool(getattr(config, "HMM_ENABLED", False))
        multi_enabled = bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False))

        self._hmm_state = self._hmm_default_state(enabled=hmm_enabled, interval_min=primary_interval)
        self._hmm_state_secondary = self._hmm_default_state(
            enabled=bool(hmm_enabled and multi_enabled),
            interval_min=secondary_interval,
        )
        self._hmm_consensus = dict(self._hmm_state)
        self._hmm_consensus.update({
            "agreement": "primary_only",
            "source_mode": self._hmm_source_mode(),
            "multi_timeframe": bool(multi_enabled),
        })
        self._hmm_detector = None
        self._hmm_detector_secondary = None
        self._hmm_module = None
        self._hmm_numpy = None

        if not hmm_enabled:
            self._hmm_consensus = self._compute_hmm_consensus()
            return

        try:
            import numpy as np  # type: ignore
            import hmm_regime_detector as hmm_mod  # type: ignore

            self._hmm_numpy = np
            self._hmm_module = hmm_mod
            self._hmm_detector = hmm_mod.RegimeDetector(
                config=self._hmm_runtime_config(
                    min_train_samples=max(1, int(getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500))),
                )
            )
            self._hmm_state["available"] = True
            self._hmm_state["error"] = ""
            self._refresh_hmm_state_from_detector()
            if multi_enabled:
                try:
                    self._hmm_detector_secondary = hmm_mod.RegimeDetector(
                        config=self._hmm_runtime_config(
                            min_train_samples=max(
                                1,
                                int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)),
                            ),
                        )
                    )
                    self._hmm_state_secondary["available"] = True
                    self._hmm_state_secondary["error"] = ""
                    self._refresh_hmm_state_from_detector(secondary=True)
                except Exception as e:
                    self._hmm_state_secondary["available"] = False
                    self._hmm_state_secondary["trained"] = False
                    self._hmm_state_secondary["error"] = str(e)
                    logger.warning(
                        "Secondary HMM runtime unavailable, continuing with primary HMM only: %s",
                        e,
                    )
            logger.info("HMM runtime initialized (advisory mode enabled)")
        except Exception as e:
            self._hmm_state["available"] = False
            self._hmm_state["trained"] = False
            self._hmm_state["error"] = str(e)
            self._hmm_state_secondary["available"] = False
            self._hmm_state_secondary["trained"] = False
            logger.warning("HMM runtime unavailable, continuing with trend-only logic: %s", e)
        self._hmm_consensus = self._compute_hmm_consensus()

    def _snapshot_hmm_state(self) -> dict[str, Any]:
        if not self._hmm_module:
            return {}
        out: dict[str, Any] = {}
        try:
            if self._hmm_detector and hasattr(self._hmm_module, "serialize_for_snapshot"):
                snap = self._hmm_module.serialize_for_snapshot(self._hmm_detector)
                if isinstance(snap, dict):
                    out.update(dict(snap))
            if self._hmm_detector_secondary and hasattr(self._hmm_module, "serialize_for_snapshot"):
                sec_snap = self._hmm_module.serialize_for_snapshot(self._hmm_detector_secondary)
                if isinstance(sec_snap, dict):
                    out["_hmm_secondary_regime_state"] = dict(
                        sec_snap.get("_hmm_regime_state", {}) or {}
                    )
                    out["_hmm_secondary_last_train_ts"] = float(
                        sec_snap.get("_hmm_last_train_ts", 0.0) or 0.0
                    )
                    out["_hmm_secondary_trained"] = bool(
                        sec_snap.get("_hmm_trained", False)
                    )
        except Exception as e:
            logger.warning("HMM snapshot serialization failed: %s", e)
        return out

    def _restore_hmm_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            return
        if not self._hmm_module:
            return
        try:
            if self._hmm_detector and "_hmm_regime_state" in snapshot and hasattr(self._hmm_module, "restore_from_snapshot"):
                self._hmm_module.restore_from_snapshot(self._hmm_detector, snapshot)
            if (
                self._hmm_detector_secondary
                and "_hmm_secondary_regime_state" in snapshot
                and hasattr(self._hmm_module, "restore_from_snapshot")
            ):
                sec_snap = {
                    "_hmm_regime_state": snapshot.get("_hmm_secondary_regime_state", {}),
                    "_hmm_last_train_ts": snapshot.get("_hmm_secondary_last_train_ts", 0.0),
                    "_hmm_trained": snapshot.get("_hmm_secondary_trained", False),
                }
                self._hmm_module.restore_from_snapshot(self._hmm_detector_secondary, sec_snap)
        except Exception as e:
            logger.warning("HMM snapshot restore failed: %s", e)
        finally:
            self._refresh_hmm_state_from_detector()
            self._refresh_hmm_state_from_detector(secondary=True)

    def _train_hmm(self, *, now: float | None = None, reason: str = "scheduled") -> bool:
        if not self._hmm_detector or self._hmm_numpy is None:
            return False

        now_ts = float(now if now is not None else _now())
        retry_sec = max(60.0, float(getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0)))
        is_trained = bool(getattr(self._hmm_detector, "_trained", False))
        if (not is_trained) and (now_ts - self._hmm_last_train_attempt_ts) < retry_sec and reason != "startup":
            return False

        self._hmm_last_train_attempt_ts = now_ts
        interval_min = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
        target_candles = max(1, int(getattr(config, "HMM_TRAINING_CANDLES", 4000)))
        min_train_samples = max(1, int(getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)))
        closes, volumes = self._fetch_training_candles(
            count=target_candles,
            interval_min=interval_min,
        )
        self._update_hmm_training_depth(
            current_candles=min(len(closes), len(volumes)),
            secondary=False,
            target_candles=target_candles,
            min_train_samples=min_train_samples,
            interval_min=interval_min,
            now=now_ts,
        )
        if not closes or not volumes:
            self._hmm_state["error"] = "no_training_candles"
            self._refresh_hmm_state_from_detector()
            return False

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            ok = bool(self._hmm_detector.train(closes_arr, volumes_arr))
        except Exception as e:
            logger.warning("HMM train failed (%s): %s", reason, e)
            self._hmm_state["error"] = f"train_failed:{e}"
            self._refresh_hmm_state_from_detector()
            return False

        if ok:
            self._hmm_state["error"] = ""
            logger.info("HMM trained (%s) with %d candles", reason, len(closes))
        else:
            self._hmm_state["error"] = "train_skipped_or_failed"
        self._refresh_hmm_state_from_detector()
        return ok

    def _train_hmm_secondary(self, *, now: float | None = None, reason: str = "scheduled") -> bool:
        if not self._hmm_detector_secondary or self._hmm_numpy is None:
            return False

        now_ts = float(now if now is not None else _now())
        retry_sec = max(
            60.0,
            float(
                getattr(
                    config,
                    "HMM_SECONDARY_SYNC_INTERVAL_SEC",
                    getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                )
            ),
        )
        is_trained = bool(getattr(self._hmm_detector_secondary, "_trained", False))
        if (
            (not is_trained)
            and (now_ts - self._hmm_last_train_attempt_ts_secondary) < retry_sec
            and reason != "startup"
        ):
            return False

        self._hmm_last_train_attempt_ts_secondary = now_ts
        interval_min = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        target_candles = max(1, int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 1440)))
        min_train_samples = max(1, int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)))
        closes, volumes = self._fetch_training_candles(
            count=target_candles,
            interval_min=interval_min,
        )
        self._update_hmm_training_depth(
            current_candles=min(len(closes), len(volumes)),
            secondary=True,
            target_candles=target_candles,
            min_train_samples=min_train_samples,
            interval_min=interval_min,
            now=now_ts,
        )
        if not closes or not volumes:
            self._hmm_state_secondary["error"] = "no_training_candles"
            self._refresh_hmm_state_from_detector(secondary=True)
            return False

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            ok = bool(self._hmm_detector_secondary.train(closes_arr, volumes_arr))
        except Exception as e:
            logger.warning("Secondary HMM train failed (%s): %s", reason, e)
            self._hmm_state_secondary["error"] = f"train_failed:{e}"
            self._refresh_hmm_state_from_detector(secondary=True)
            return False

        if ok:
            self._hmm_state_secondary["error"] = ""
            logger.info("Secondary HMM trained (%s) with %d candles", reason, len(closes))
        else:
            self._hmm_state_secondary["error"] = "train_skipped_or_failed"
        self._refresh_hmm_state_from_detector(secondary=True)
        return ok

    def _update_hmm_secondary(self, now: float) -> None:
        if not bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)):
            return
        if not self._hmm_detector_secondary or self._hmm_numpy is None:
            self._refresh_hmm_state_from_detector(secondary=True)
            return

        trained = bool(getattr(self._hmm_detector_secondary, "_trained", False))
        if not trained:
            self._train_hmm_secondary(now=now, reason="startup")
            trained = bool(getattr(self._hmm_detector_secondary, "_trained", False))
        else:
            try:
                if bool(self._hmm_detector_secondary.needs_retrain()):
                    self._train_hmm_secondary(now=now, reason="periodic")
            except Exception as e:
                logger.debug("Secondary HMM retrain check failed: %s", e)

        if not trained:
            self._refresh_hmm_state_from_detector(secondary=True)
            return

        interval_min = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        closes, volumes = self._fetch_recent_candles(
            count=int(getattr(config, "HMM_SECONDARY_RECENT_CANDLES", 50)),
            interval_min=interval_min,
        )
        if not closes or not volumes:
            self._refresh_hmm_state_from_detector(secondary=True)
            return

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            self._hmm_detector_secondary.update(closes_arr, volumes_arr)
            self._hmm_state_secondary["error"] = ""
        except Exception as e:
            logger.warning("Secondary HMM inference failed: %s", e)
            self._hmm_state_secondary["error"] = f"inference_failed:{e}"
        finally:
            self._refresh_hmm_state_from_detector(secondary=True)

    @staticmethod
    def _normalize_consensus_weights(w1_raw: Any, w15_raw: Any) -> tuple[float, float]:
        try:
            w1 = float(w1_raw)
        except (TypeError, ValueError):
            w1 = 0.0
        try:
            w15 = float(w15_raw)
        except (TypeError, ValueError):
            w15 = 0.0
        if not isfinite(w1):
            w1 = 0.0
        if not isfinite(w15):
            w15 = 0.0
        w1 = max(0.0, w1)
        w15 = max(0.0, w15)
        total = w1 + w15
        if total <= 1e-9:
            return 0.3, 0.7
        return w1 / total, w15 / total

    def _compute_hmm_consensus(self) -> dict[str, Any]:
        primary = dict(self._hmm_state or self._hmm_default_state())
        secondary = dict(
            self._hmm_state_secondary
            or self._hmm_default_state(
                enabled=bool(getattr(config, "HMM_ENABLED", False))
                and bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)),
                interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
            )
        )
        multi_enabled = bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False))
        source_mode = self._hmm_source_mode()
        primary_ready = bool(primary.get("available")) and bool(primary.get("trained"))

        if not primary_ready:
            return {
                "enabled": bool(getattr(config, "HMM_ENABLED", False)),
                "available": bool(primary.get("available")),
                "trained": False,
                "interval_min": int(primary.get("interval_min", getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
                "regime": "RANGING",
                "regime_id": 1,
                "confidence": 0.0,
                "bias_signal": 0.0,
                "effective_regime": "RANGING",
                "effective_confidence": 0.0,
                "effective_bias": 0.0,
                "agreement": "primary_untrained",
                "source_mode": source_mode,
                "multi_timeframe": bool(multi_enabled),
                "primary": primary,
                "secondary": secondary,
                "last_update_ts": float(primary.get("last_update_ts", 0.0) or 0.0),
                "last_train_ts": float(primary.get("last_train_ts", 0.0) or 0.0),
                "blend_factor": float(primary.get("blend_factor", getattr(config, "HMM_BLEND_WITH_TREND", 0.5))),
                "error": str(primary.get("error", "")),
            }

        if not multi_enabled:
            out = dict(primary)
            out.update({
                "agreement": "primary_only",
                "source_mode": source_mode,
                "multi_timeframe": False,
                "primary": primary,
                "secondary": secondary,
                "effective_regime": str(out.get("regime", "RANGING") or "RANGING"),
                "effective_confidence": float(out.get("confidence", 0.0) or 0.0),
                "effective_bias": float(out.get("bias_signal", 0.0) or 0.0),
            })
            return out

        if not bool(secondary.get("available")) or not bool(secondary.get("trained")):
            out = dict(primary)
            out.update({
                "agreement": "primary_only",
                "source_mode": source_mode,
                "multi_timeframe": True,
                "primary": primary,
                "secondary": secondary,
                "effective_regime": str(out.get("regime", "RANGING") or "RANGING"),
                "effective_confidence": float(out.get("confidence", 0.0) or 0.0),
                "effective_bias": float(out.get("bias_signal", 0.0) or 0.0),
            })
            return out

        regime_1m = str(primary.get("regime", "RANGING") or "RANGING").upper()
        regime_15m = str(secondary.get("regime", "RANGING") or "RANGING").upper()
        valid_regimes = {"BULLISH", "BEARISH", "RANGING"}
        if regime_1m not in valid_regimes:
            regime_1m = "RANGING"
        if regime_15m not in valid_regimes:
            regime_15m = "RANGING"

        conf_1m = max(0.0, min(1.0, float(primary.get("confidence", 0.0) or 0.0)))
        conf_15m = max(0.0, min(1.0, float(secondary.get("confidence", 0.0) or 0.0)))
        bias_1m = float(primary.get("bias_signal", 0.0) or 0.0)
        bias_15m = float(secondary.get("bias_signal", 0.0) or 0.0)
        dampen = max(0.0, min(1.0, float(getattr(config, "CONSENSUS_DAMPEN_FACTOR", 0.5))))
        w1, w15 = self._normalize_consensus_weights(
            getattr(config, "CONSENSUS_1M_WEIGHT", 0.3),
            getattr(config, "CONSENSUS_15M_WEIGHT", 0.7),
        )

        agreement = "conflict"
        if regime_1m == regime_15m:
            effective_confidence = max(conf_1m, conf_15m)
            agreement = "full"
        elif regime_15m == "RANGING":
            effective_confidence = 0.0
            agreement = "15m_neutral"
        elif regime_1m == "RANGING":
            effective_confidence = conf_15m * dampen
            agreement = "1m_cooling"
        else:
            effective_confidence = 0.0
            agreement = "conflict"

        if agreement == "full":
            effective_bias = w1 * bias_1m + w15 * bias_15m
        elif agreement == "1m_cooling":
            effective_bias = bias_15m * dampen
        else:
            effective_bias = 0.0

        effective_confidence = max(0.0, min(1.0, float(effective_confidence)))
        effective_bias = max(-1.0, min(1.0, float(effective_bias)))
        tier1_conf = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER1_CONFIDENCE", 0.20))))
        if effective_confidence < tier1_conf:
            effective_regime = "RANGING"
        elif effective_bias > 0:
            effective_regime = "BULLISH"
        elif effective_bias < 0:
            effective_regime = "BEARISH"
        else:
            effective_regime = "RANGING"

        return {
            "enabled": bool(getattr(config, "HMM_ENABLED", False)),
            "available": bool(primary.get("available")) and bool(secondary.get("available")),
            "trained": bool(primary.get("trained")) and bool(secondary.get("trained")),
            "interval_min": int(primary.get("interval_min", getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
            "regime": effective_regime,
            "regime_id": {"BEARISH": 0, "RANGING": 1, "BULLISH": 2}.get(effective_regime, 1),
            "confidence": effective_confidence,
            "bias_signal": effective_bias,
            "effective_regime": effective_regime,
            "effective_confidence": effective_confidence,
            "effective_bias": effective_bias,
            "agreement": agreement,
            "weights": {"w1m": w1, "w15m": w15},
            "source_mode": source_mode,
            "multi_timeframe": True,
            "primary": primary,
            "secondary": secondary,
            "last_update_ts": max(
                float(primary.get("last_update_ts", 0.0) or 0.0),
                float(secondary.get("last_update_ts", 0.0) or 0.0),
            ),
            "last_train_ts": max(
                float(primary.get("last_train_ts", 0.0) or 0.0),
                float(secondary.get("last_train_ts", 0.0) or 0.0),
            ),
            "blend_factor": float(primary.get("blend_factor", getattr(config, "HMM_BLEND_WITH_TREND", 0.5))),
            "error": "",
        }

    def _update_hmm(self, now: float) -> None:
        if not bool(getattr(config, "HMM_ENABLED", False)):
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)
            return
        if not self._hmm_detector or self._hmm_numpy is None:
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)
            return

        trained = bool(getattr(self._hmm_detector, "_trained", False))
        if not trained:
            self._train_hmm(now=now, reason="startup")
            trained = bool(getattr(self._hmm_detector, "_trained", False))
        else:
            try:
                if bool(self._hmm_detector.needs_retrain()):
                    self._train_hmm(now=now, reason="periodic")
            except Exception as e:
                logger.debug("HMM retrain check failed: %s", e)

        if not trained:
            self._refresh_hmm_state_from_detector()
            if bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)):
                self._update_hmm_secondary(now)
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)
            return

        interval_min = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
        closes, volumes = self._fetch_recent_candles(
            count=int(getattr(config, "HMM_RECENT_CANDLES", 100)),
            interval_min=interval_min,
        )
        if not closes or not volumes:
            self._refresh_hmm_state_from_detector()
            if bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)):
                self._update_hmm_secondary(now)
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)
            return

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            self._hmm_detector.update(closes_arr, volumes_arr)
            self._hmm_state["error"] = ""
        except Exception as e:
            logger.warning("HMM inference failed: %s", e)
            self._hmm_state["error"] = f"inference_failed:{e}"
        finally:
            self._refresh_hmm_state_from_detector()
            if bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)):
                self._update_hmm_secondary(now)
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)

    def _hmm_status_payload(self) -> dict[str, Any]:
        primary = dict(
            self._hmm_state
            or self._hmm_default_state(
                enabled=bool(getattr(config, "HMM_ENABLED", False)),
                interval_min=max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
            )
        )
        secondary = dict(
            self._hmm_state_secondary
            or self._hmm_default_state(
                enabled=bool(getattr(config, "HMM_ENABLED", False))
                and bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)),
                interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
            )
        )
        training_depth_primary = dict(
            self._hmm_training_depth
            or self._hmm_training_depth_default(state_key="primary")
        )
        training_depth_secondary = dict(
            self._hmm_training_depth_secondary
            or self._hmm_training_depth_default(state_key="secondary")
        )
        consensus = dict(self._hmm_consensus or self._compute_hmm_consensus())
        source_mode = self._hmm_source_mode()
        source = dict(self._policy_hmm_source() or primary)
        source_interval = int(source.get("interval_min", primary.get("interval_min", 1)) or 1)
        secondary_interval = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        if source_interval == secondary_interval and bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)):
            training_depth = training_depth_secondary
        else:
            training_depth = training_depth_primary
        raw_probs = source.get("probabilities", primary.get("probabilities"))
        probs = (
            dict(raw_probs)
            if isinstance(raw_probs, dict)
            else {"bearish": 0.0, "ranging": 1.0, "bullish": 0.0}
        )
        confidence_raw = max(0.0, min(1.0, float(source.get("confidence", 0.0) or 0.0)))
        confidence_modifier, confidence_modifier_source = self._hmm_confidence_modifier_for_source(source)
        confidence_effective = max(0.0, min(1.0, confidence_raw * confidence_modifier))
        out = {
            "enabled": bool(source.get("enabled", False)),
            "available": bool(source.get("available", False)),
            "trained": bool(source.get("trained", False)),
            "interval_min": int(source.get("interval_min", primary.get("interval_min", 1))),
            "regime": str(source.get("regime", "RANGING")),
            "regime_id": int(source.get("regime_id", 1)),
            "confidence": confidence_raw,
            "confidence_raw": confidence_raw,
            "confidence_effective": confidence_effective,
            "confidence_modifier": float(confidence_modifier),
            "confidence_modifier_source": str(confidence_modifier_source),
            "bias_signal": float(source.get("bias_signal", 0.0)),
            "probabilities": probs,
            "observation_count": int(source.get("observation_count", 0)),
            "blend_factor": float(primary.get("blend_factor", getattr(config, "HMM_BLEND_WITH_TREND", 0.5))),
            "last_update_ts": float(source.get("last_update_ts", 0.0)),
            "last_train_ts": float(source.get("last_train_ts", 0.0)),
            "error": str(source.get("error", "")),
            "source_mode": source_mode,
            "multi_timeframe": bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)),
            "agreement": str(consensus.get("agreement", "primary_only")),
            "primary": primary,
            "secondary": secondary,
            "consensus": consensus,
            "training_depth": training_depth,
            "training_depth_primary": training_depth_primary,
            "training_depth_secondary": training_depth_secondary,
            "regime_history_30m": list(self._regime_history_30m),
        }
        return out

    def _manual_regime_override(self) -> tuple[str | None, float]:
        raw = str(getattr(config, "REGIME_MANUAL_OVERRIDE", "") or "").strip().upper()
        if raw in {"BULLISH", "BEARISH"}:
            conf = max(0.0, min(1.0, float(getattr(config, "REGIME_MANUAL_CONFIDENCE", 0.75))))
            return raw, conf
        return None, 0.0

    @staticmethod
    def _tier_direction(
        tier: int,
        regime: str,
        bias: float,
        suppressed_side: str | None = None,
    ) -> str:
        use_tier = max(0, min(2, int(tier)))
        if use_tier <= 0:
            return "symmetric"
        if use_tier >= 2:
            if suppressed_side == "A":
                return "long_bias"
            if suppressed_side == "B":
                return "short_bias"
        reg = str(regime or "RANGING").upper()
        if reg == "BULLISH" or float(bias) > 0.0:
            return "long_bias"
        if reg == "BEARISH" or float(bias) < 0.0:
            return "short_bias"
        return "symmetric"

    @staticmethod
    def _classify_ai_regime_agreement(
        ai_tier: int,
        ai_direction: str,
        mechanical_tier: int,
        mechanical_direction: str,
    ) -> str:
        ai_t = max(0, min(2, int(ai_tier)))
        mech_t = max(0, min(2, int(mechanical_tier)))
        ai_dir = str(ai_direction or "symmetric").strip().lower()
        mech_dir = str(mechanical_direction or "symmetric").strip().lower()
        if ai_t == mech_t and ai_dir == mech_dir:
            return "agree"
        if ai_t > mech_t:
            return "ai_upgrade"
        if ai_t < mech_t:
            return "ai_downgrade"
        return "ai_flip"

    def _ai_regime_history_limit(self) -> int:
        return max(1, int(getattr(config, "AI_REGIME_HISTORY_SIZE", 12)))

    @staticmethod
    def _hmm_prob_triplet(source: dict[str, Any]) -> list[float]:
        raw = source.get("probabilities", {})
        if isinstance(raw, dict):
            try:
                return [
                    float(raw.get("bearish", 0.0) or 0.0),
                    float(raw.get("ranging", 1.0) or 0.0),
                    float(raw.get("bullish", 0.0) or 0.0),
                ]
            except (TypeError, ValueError):
                return [0.0, 1.0, 0.0]
        if isinstance(raw, (list, tuple)) and len(raw) >= 3:
            try:
                return [float(raw[0]), float(raw[1]), float(raw[2])]
            except (TypeError, ValueError):
                return [0.0, 1.0, 0.0]
        return [0.0, 1.0, 0.0]

    def _fill_rate_1h(self, now: float) -> int:
        cutoff = float(now) - 3600.0
        count = 0
        for slot in self.slots.values():
            for cyc in slot.state.completed_cycles:
                try:
                    exit_time = float(getattr(cyc, "exit_time", 0.0) or 0.0)
                except (TypeError, ValueError):
                    exit_time = 0.0
                if exit_time >= cutoff:
                    count += 1
        return int(count)

    def _build_ai_regime_context(self, now: float) -> dict[str, Any]:
        primary = dict(self._hmm_state or self._hmm_default_state())
        secondary = dict(
            self._hmm_state_secondary
            or self._hmm_default_state(
                enabled=bool(getattr(config, "HMM_ENABLED", False))
                and bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)),
                interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
            )
        )
        consensus = dict(self._hmm_consensus or self._compute_hmm_consensus())
        source = dict(self._policy_hmm_source() or primary)
        source_interval = int(source.get("interval_min", primary.get("interval_min", 1)) or 1)
        secondary_interval = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        if source_interval == secondary_interval and bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)):
            depth = dict(self._hmm_training_depth_secondary or self._hmm_training_depth_default(state_key="secondary"))
        else:
            depth = dict(self._hmm_training_depth or self._hmm_training_depth_default(state_key="primary"))
        confidence_modifier, _mod_source = self._hmm_confidence_modifier_for_source(source)

        transmat = None
        try:
            if self._hmm_detector is not None:
                transmat = getattr(self._hmm_detector, "transmat", None)
        except Exception:
            transmat = None

        capacity = self._compute_capacity_health(now)
        safe_cap = max(1, int(capacity.get("open_orders_safe_cap") or 1))
        headroom_count = int(capacity.get("open_order_headroom") or 0)
        headroom_pct = max(0.0, min(100.0, (float(headroom_count) / float(safe_cap)) * 100.0))

        trend_score = float(self._trend_score)
        dead_zone = abs(float(getattr(config, "TREND_DEAD_ZONE", 0.001)))
        if trend_score > dead_zone:
            directional_trend = "bullish"
        elif trend_score < -dead_zone:
            directional_trend = "bearish"
        else:
            directional_trend = "neutral"

        recovery_order_count = sum(len(slot.state.recovery_orders) for slot in self.slots.values())
        kelly_payload = self._kelly.status_payload() if self._kelly is not None else {}
        bull_edge = 0.0
        bear_edge = 0.0
        range_edge = 0.0
        if isinstance(kelly_payload, dict):
            try:
                bull_edge = float((kelly_payload.get("bullish") or {}).get("edge", 0.0) or 0.0)
            except Exception:
                bull_edge = 0.0
            try:
                bear_edge = float((kelly_payload.get("bearish") or {}).get("edge", 0.0) or 0.0)
            except Exception:
                bear_edge = 0.0
            try:
                range_edge = float((kelly_payload.get("ranging") or {}).get("edge", 0.0) or 0.0)
            except Exception:
                range_edge = 0.0

        return {
            "hmm_primary": {
                "regime": str(primary.get("regime", "RANGING")),
                "confidence": float(primary.get("confidence", 0.0) or 0.0),
                "bias_signal": float(primary.get("bias_signal", 0.0) or 0.0),
                "probabilities": self._hmm_prob_triplet(primary),
            },
            "hmm_secondary": {
                "regime": str(secondary.get("regime", "RANGING")),
                "confidence": float(secondary.get("confidence", 0.0) or 0.0),
                "bias_signal": float(secondary.get("bias_signal", 0.0) or 0.0),
                "probabilities": self._hmm_prob_triplet(secondary),
            },
            "hmm_consensus": {
                "agreement": str(consensus.get("agreement", "primary_only")),
                "effective_regime": str(consensus.get("effective_regime", consensus.get("regime", "RANGING"))),
                "effective_confidence": float(consensus.get("effective_confidence", consensus.get("confidence", 0.0)) or 0.0),
                "effective_bias": float(consensus.get("effective_bias", consensus.get("bias_signal", 0.0)) or 0.0),
            },
            "transition_matrix_1m": transmat,
            "training_quality": str(depth.get("quality_tier", "shallow")),
            "confidence_modifier": float(confidence_modifier),
            "regime_history_30m": list(self._regime_history_30m),
            "mechanical_tier": {
                "current": int(self._regime_mechanical_tier),
                "direction": str(self._regime_mechanical_direction),
                "since": int(float(self._regime_mechanical_since or 0.0)),
            },
            "operational": {
                "directional_trend": directional_trend,
                "trend_detected_at": int(float(self._trend_last_update_ts or 0.0)),
                "fill_rate_1h": int(self._fill_rate_1h(now)),
                "recovery_order_count": int(recovery_order_count),
                "capacity_headroom": float(headroom_pct),
                "capacity_band": str(capacity.get("status_band") or "normal"),
                "kelly_edge_bullish": float(bull_edge),
                "kelly_edge_bearish": float(bear_edge),
                "kelly_edge_ranging": float(range_edge),
            },
        }

    def _clear_ai_override(self) -> None:
        self._ai_override_tier = None
        self._ai_override_direction = None
        self._ai_override_until = None
        self._ai_override_applied_at = None
        self._ai_override_source_conviction = None

    def _mark_ai_history_latest(self, action: str) -> None:
        if not self._ai_regime_history:
            return
        tail = dict(self._ai_regime_history[-1] or {})
        tail["action"] = str(action)
        self._ai_regime_history[-1] = tail

    def apply_ai_regime_override(self, ttl_sec: int | None = None) -> tuple[bool, str]:
        if not bool(getattr(config, "AI_REGIME_ADVISOR_ENABLED", False)):
            return False, "ai regime advisor disabled"

        opinion = dict(self._ai_regime_opinion or {})
        if not opinion:
            return False, "no ai opinion available"
        if str(opinion.get("error", "") or "").strip():
            return False, "ai opinion unavailable"

        agreement = str(opinion.get("agreement", "unknown") or "unknown").strip().lower()
        if agreement not in {"ai_upgrade", "ai_downgrade", "ai_flip"}:
            return False, "ai opinion already agrees with mechanical"

        try:
            recommended_tier = int(opinion.get("recommended_tier", 0) or 0)
        except (TypeError, ValueError):
            recommended_tier = 0
        recommended_tier = max(0, min(2, recommended_tier))
        recommended_direction = str(opinion.get("recommended_direction", "symmetric") or "symmetric").strip().lower()
        if recommended_direction not in {"symmetric", "long_bias", "short_bias"}:
            recommended_direction = "symmetric"

        try:
            conviction = int(opinion.get("conviction", 0) or 0)
        except (TypeError, ValueError):
            conviction = 0
        conviction = max(0, min(100, conviction))
        min_conviction = max(0, min(100, int(getattr(config, "AI_OVERRIDE_MIN_CONVICTION", 50))))
        if conviction < min_conviction:
            return False, f"conviction {conviction} below minimum {min_conviction}"

        mechanical_tier = max(0, min(2, int(self._regime_mechanical_tier)))
        low = max(0, mechanical_tier - 1)
        high = min(2, mechanical_tier + 1)
        applied_tier = max(low, min(high, recommended_tier))
        applied_direction = recommended_direction if applied_tier > 0 else "symmetric"

        capacity_band = str(self._compute_capacity_health(_now()).get("status_band") or "normal").strip().lower()
        if capacity_band == "stop" and applied_tier > mechanical_tier:
            return False, "capacity stop gate blocks upgrade override"

        default_ttl = max(1, int(getattr(config, "AI_OVERRIDE_TTL_SEC", 1800)))
        max_ttl = max(1, int(getattr(config, "AI_OVERRIDE_MAX_TTL_SEC", 3600)))
        if ttl_sec is None:
            use_ttl = default_ttl
        else:
            try:
                use_ttl = int(ttl_sec)
            except (TypeError, ValueError):
                use_ttl = default_ttl
        use_ttl = max(1, min(use_ttl, max_ttl))

        now_ts = _now()
        self._ai_override_tier = int(applied_tier)
        self._ai_override_direction = str(applied_direction)
        self._ai_override_applied_at = float(now_ts)
        self._ai_override_until = float(now_ts + use_ttl)
        self._ai_override_source_conviction = int(conviction)
        self._ai_regime_dismissed = False
        self._mark_ai_history_latest("applied")
        logger.info(
            "AI regime advisor: override applied tier=%d direction=%s ttl=%ds conviction=%d",
            int(applied_tier),
            str(applied_direction),
            int(use_ttl),
            int(conviction),
        )
        return True, (
            f"AI override applied: Tier {int(applied_tier)} {str(applied_direction)} "
            f"for {int(use_ttl)}s"
        )

    def revert_ai_regime_override(self) -> tuple[bool, str]:
        payload = self._ai_override_payload()
        was_active = bool(payload.get("active"))
        if (
            self._ai_override_tier is None
            and self._ai_override_direction is None
            and self._ai_override_until is None
        ):
            return False, "no ai override active"
        self._clear_ai_override()
        self._mark_ai_history_latest("reverted")
        logger.info("AI regime advisor: override cancelled by operator")
        if was_active:
            return True, "override cancelled; reverted to mechanical"
        return True, "override state cleared"

    def dismiss_ai_regime_opinion(self) -> tuple[bool, str]:
        if not bool(getattr(config, "AI_REGIME_ADVISOR_ENABLED", False)):
            return False, "ai regime advisor disabled"
        opinion = dict(self._ai_regime_opinion or {})
        if not opinion:
            return False, "no ai opinion available"
        agreement = str(opinion.get("agreement", "unknown") or "unknown").strip().lower()
        if agreement not in {"ai_upgrade", "ai_downgrade", "ai_flip"}:
            return False, "nothing to dismiss (no active disagreement)"
        self._ai_regime_dismissed = True
        self._mark_ai_history_latest("dismissed")
        return True, "ai disagreement dismissed"

    def _ai_override_payload(self, now: float | None = None) -> dict[str, Any]:
        now_ts = float(now if now is not None else _now())
        expires = float(self._ai_override_until or 0.0)
        active = bool(self._ai_override_tier is not None and self._ai_override_direction and expires > now_ts)
        remaining = max(0.0, expires - now_ts) if active else None
        return {
            "active": bool(active),
            "tier": (int(self._ai_override_tier) if self._ai_override_tier is not None else None),
            "direction": (str(self._ai_override_direction) if self._ai_override_direction else None),
            "applied_at": (float(self._ai_override_applied_at) if self._ai_override_applied_at else None),
            "expires_at": (float(expires) if active else None),
            "remaining_sec": (int(remaining) if remaining is not None else None),
            "source_conviction": (
                int(self._ai_override_source_conviction)
                if self._ai_override_source_conviction is not None
                else None
            ),
        }

    def _ai_regime_worker(self, context: dict[str, Any], trigger: str, requested_at: float) -> None:
        try:
            opinion = ai_advisor.get_regime_opinion(context)
            pending = {
                "opinion": dict(opinion or {}),
                "trigger": str(trigger),
                "requested_at": float(requested_at),
                "completed_at": float(_now()),
                "mechanical_at_request": dict(context.get("mechanical_tier", {})),
                "consensus_at_request": str((context.get("hmm_consensus") or {}).get("agreement", "")),
            }
            self._ai_regime_pending_result = pending
        except Exception as e:
            self._ai_regime_pending_result = {
                "opinion": {
                    "recommended_tier": 0,
                    "recommended_direction": "symmetric",
                    "conviction": 0,
                    "rationale": "",
                    "watch_for": "",
                    "panelist": "",
                    "error": str(e),
                },
                "trigger": str(trigger),
                "requested_at": float(requested_at),
                "completed_at": float(_now()),
                "mechanical_at_request": dict(context.get("mechanical_tier", {})),
                "consensus_at_request": str((context.get("hmm_consensus") or {}).get("agreement", "")),
            }
        finally:
            self._ai_regime_thread_alive = False

    def _start_ai_regime_run(self, now: float, trigger: str) -> None:
        if not bool(getattr(config, "AI_REGIME_ADVISOR_ENABLED", False)):
            return
        if self._ai_regime_thread_alive:
            return
        context = self._build_ai_regime_context(now)
        self._ai_regime_last_run_ts = float(now)
        self._ai_regime_last_trigger_reason = str(trigger)
        self._ai_regime_last_mechanical_tier = int(self._regime_mechanical_tier)
        self._ai_regime_last_mechanical_direction = str(self._regime_mechanical_direction)
        self._ai_regime_last_consensus_agreement = str((self._hmm_consensus or {}).get("agreement", "primary_only"))
        self._ai_regime_thread_alive = True
        thread = threading.Thread(
            target=self._ai_regime_worker,
            args=(context, str(trigger), float(now)),
            daemon=True,
            name="ai-regime-advisor",
        )
        try:
            thread.start()
        except Exception:
            self._ai_regime_thread_alive = False
            raise

    def _process_ai_regime_pending_result(self, now: float) -> None:
        pending = self._ai_regime_pending_result
        if not isinstance(pending, dict):
            return
        self._ai_regime_pending_result = None

        raw_opinion = pending.get("opinion", {})
        opinion = dict(raw_opinion) if isinstance(raw_opinion, dict) else {}
        recommended_tier = max(0, min(2, int(opinion.get("recommended_tier", 0) or 0)))
        recommended_direction = str(opinion.get("recommended_direction", "symmetric") or "symmetric").strip().lower()
        if recommended_direction not in {"symmetric", "long_bias", "short_bias"}:
            recommended_direction = "symmetric"
        conviction = max(0, min(100, int(opinion.get("conviction", 0) or 0)))
        rationale = str(opinion.get("rationale", "") or "")[:500]
        watch_for = str(opinion.get("watch_for", "") or "")[:200]
        panelist = str(opinion.get("panelist", "") or "")
        error = str(opinion.get("error", "") or "")

        mechanical_ref = pending.get("mechanical_at_request", {})
        if not isinstance(mechanical_ref, dict):
            mechanical_ref = {}
        mechanical_tier = max(0, min(2, int(mechanical_ref.get("current", self._regime_mechanical_tier) or 0)))
        mechanical_direction = str(
            mechanical_ref.get("direction", self._regime_mechanical_direction) or "symmetric"
        ).strip().lower()
        if mechanical_direction not in {"symmetric", "long_bias", "short_bias"}:
            mechanical_direction = "symmetric"

        if error:
            agreement = "error"
        else:
            agreement = self._classify_ai_regime_agreement(
                recommended_tier,
                recommended_direction,
                mechanical_tier,
                mechanical_direction,
            )

        self._ai_regime_opinion = {
            "recommended_tier": int(recommended_tier),
            "recommended_direction": str(recommended_direction),
            "conviction": int(conviction),
            "rationale": rationale,
            "watch_for": watch_for,
            "panelist": panelist,
            "error": error,
            "agreement": agreement,
            "trigger": str(pending.get("trigger", "")),
            "ts": float(pending.get("completed_at", now) or now),
            "requested_at": float(pending.get("requested_at", now) or now),
            "mechanical_tier": int(mechanical_tier),
            "mechanical_direction": str(mechanical_direction),
        }
        self._ai_regime_dismissed = False

        action = "none"
        if agreement in {"ai_upgrade", "ai_downgrade", "ai_flip"} and not error:
            action = "pending"
        self._ai_regime_history.append(
            {
                "ts": float(pending.get("completed_at", now) or now),
                "mechanical_tier": int(mechanical_tier),
                "mechanical_direction": str(mechanical_direction),
                "ai_tier": int(recommended_tier),
                "ai_direction": str(recommended_direction),
                "conviction": int(conviction),
                "agreement": str(agreement),
                "action": action,
            }
        )
        while len(self._ai_regime_history) > self._ai_regime_history_limit():
            self._ai_regime_history.popleft()

        if error:
            logger.info("AI regime advisor: %s", error)
            return
        if agreement == "agree":
            logger.info(
                "AI regime advisor: agrees with mechanical Tier %d %s (conviction %d)",
                mechanical_tier,
                mechanical_direction,
                conviction,
            )
        else:
            logger.info(
                "AI regime advisor: Tier %d %s (conviction %d) -- mechanical Tier %d %s",
                recommended_tier,
                recommended_direction,
                conviction,
                mechanical_tier,
                mechanical_direction,
            )

    def _maybe_schedule_ai_regime(self, now: float) -> None:
        if not bool(getattr(config, "AI_REGIME_ADVISOR_ENABLED", False)):
            return
        if self._ai_regime_thread_alive:
            return

        self._process_ai_regime_pending_result(now)

        last_run = float(self._ai_regime_last_run_ts or 0.0)
        elapsed = now - last_run if last_run > 0.0 else float("inf")
        debounce_sec = max(1.0, float(getattr(config, "AI_REGIME_DEBOUNCE_SEC", 60.0)))
        interval_sec = max(1.0, float(getattr(config, "AI_REGIME_INTERVAL_SEC", 300.0)))
        if elapsed < debounce_sec:
            return

        periodic_due = elapsed >= interval_sec
        agreement_now = str((self._hmm_consensus or {}).get("agreement", "primary_only"))
        mech_changed = (
            int(self._regime_mechanical_tier) != int(self._ai_regime_last_mechanical_tier)
            or str(self._regime_mechanical_direction) != str(self._ai_regime_last_mechanical_direction)
        )
        consensus_changed = agreement_now != str(self._ai_regime_last_consensus_agreement)
        event_due = bool(mech_changed or consensus_changed)
        if not (periodic_due or event_due):
            return

        if periodic_due:
            trigger = "periodic"
        elif mech_changed and consensus_changed:
            trigger = "mechanical_and_consensus_change"
        elif mech_changed:
            trigger = "mechanical_tier_change"
        else:
            trigger = "consensus_mode_change"
        self._start_ai_regime_run(now, trigger)

    def _regime_grace_elapsed(self, now: float) -> bool:
        if int(self._regime_tier) != 2:
            return False
        grace_sec = max(0.0, float(getattr(config, "REGIME_SUPPRESSION_GRACE_SEC", 0.0)))
        if grace_sec <= 0.0:
            return True
        started_at = float(self._regime_tier2_grace_start or self._regime_tier_entered_at or now)
        return (float(now) - started_at) >= grace_sec

    def _update_regime_tier(self, now: float) -> None:
        interval_sec = max(1.0, float(getattr(config, "REGIME_EVAL_INTERVAL_SEC", 300.0)))
        if self._regime_last_eval_ts > 0 and (now - self._regime_last_eval_ts) < interval_sec:
            return
        self._regime_last_eval_ts = now

        actuation_enabled = bool(getattr(config, "REGIME_DIRECTIONAL_ENABLED", False))
        shadow_enabled = bool(getattr(config, "REGIME_SHADOW_ENABLED", False))
        enabled = bool(actuation_enabled or shadow_enabled)

        # Backward-compatible bootstrap for tests/snapshots that only seed
        # effective regime fields.
        if self._regime_mechanical_tier_entered_at <= 0.0 and self._regime_tier_entered_at > 0.0:
            self._regime_mechanical_tier = max(0, min(2, int(self._regime_tier)))
            self._regime_mechanical_tier_entered_at = float(self._regime_tier_entered_at)
            if self._regime_mechanical_since <= 0.0:
                self._regime_mechanical_since = float(self._regime_tier_entered_at)
            reg = str((self._regime_shadow_state or {}).get("regime", "RANGING"))
            reg_bias = float((self._regime_shadow_state or {}).get("bias_signal", 0.0) or 0.0)
            self._regime_mechanical_direction = self._tier_direction(
                int(self._regime_mechanical_tier),
                reg,
                reg_bias,
                self._regime_side_suppressed,
            )
        if self._regime_mechanical_tier2_last_downgrade_at <= 0.0 and self._regime_tier2_last_downgrade_at > 0.0:
            self._regime_mechanical_tier2_last_downgrade_at = float(self._regime_tier2_last_downgrade_at)

        if enabled:
            _, _, _, _, pre_source = self._policy_hmm_signal()
            last_hmm_update_ts = float(pre_source.get("last_update_ts", 0.0) or 0.0)
            if (now - last_hmm_update_ts) >= interval_sec:
                self._update_hmm(now)

        regime, confidence_raw, bias, hmm_ready, policy_source = self._policy_hmm_signal()
        confidence_modifier, confidence_modifier_source = self._hmm_confidence_modifier_for_source(
            policy_source
        )
        confidence_effective = max(
            0.0,
            min(1.0, float(confidence_raw) * float(confidence_modifier)),
        )
        reason = "disabled"

        override, override_conf = self._manual_regime_override()
        if override is not None:
            regime = override
            confidence_raw = override_conf
            confidence_effective = override_conf
            confidence_modifier = 1.0
            confidence_modifier_source = "manual_override"
            bias = 1.0 if override == "BULLISH" else -1.0
            hmm_ready = True
            reason = "manual_override"

        current_effective_tier = max(0, min(2, int(self._regime_tier)))
        current_mechanical_tier = max(0, min(2, int(self._regime_mechanical_tier)))
        mechanical_target_tier = 0
        target_tier = 0
        suppressed_side: str | None = None
        directional_ok_tier1 = False
        directional_ok_tier2 = False
        effective_regime = str(regime)
        effective_bias = float(bias)
        effective_confidence = float(confidence_effective)
        effective_direction = "symmetric"
        mechanical_reason = str(reason)

        if enabled and hmm_ready:
            tier1_conf = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER1_CONFIDENCE", 0.20))))
            tier2_conf = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER2_CONFIDENCE", 0.50))))
            tier1_bias_floor = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER1_BIAS_FLOOR", 0.10))))
            tier2_bias_floor = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER2_BIAS_FLOOR", 0.25))))
            hysteresis = max(0.0, min(1.0, float(getattr(config, "REGIME_HYSTERESIS", 0.05))))
            min_dwell_sec = max(0.0, float(getattr(config, "REGIME_MIN_DWELL_SEC", 300.0)))
            entered_at = float(self._regime_mechanical_tier_entered_at)
            dwell_elapsed = max(0.0, now - entered_at) if entered_at > 0 else min_dwell_sec
            abs_bias = abs(float(bias))
            directional = regime in ("BULLISH", "BEARISH")
            directional_ok_tier1 = bool(directional and abs_bias >= tier1_bias_floor)
            directional_ok_tier2 = bool(directional and abs_bias >= tier2_bias_floor)

            if confidence_effective >= tier2_conf:
                mechanical_target_tier = 2
            elif confidence_effective >= tier1_conf:
                mechanical_target_tier = 1
            else:
                mechanical_target_tier = 0

            if mechanical_target_tier == 2 and not directional_ok_tier2:
                mechanical_target_tier = 1 if directional_ok_tier1 else 0
            elif mechanical_target_tier == 1 and not directional_ok_tier1:
                mechanical_target_tier = 0

            # Hysteresis on downgrades only — but never override the
            # directional gate.  If the downgrade was caused by missing
            # directional evidence (RANGING or weak bias), hysteresis
            # must not re-promote back to the gated tier.
            if mechanical_target_tier < current_mechanical_tier:
                # Only apply hysteresis if the current tier's directional
                # gate is still satisfied.
                gate_ok_for_current = (
                    (current_mechanical_tier == 1 and directional_ok_tier1) or
                    (current_mechanical_tier == 2 and directional_ok_tier2) or
                    current_mechanical_tier == 0
                )
                if gate_ok_for_current:
                    threshold = [0.0, tier1_conf, tier2_conf][current_mechanical_tier]
                    if confidence_effective > (threshold - hysteresis):
                        mechanical_target_tier = current_mechanical_tier

            # Minimum dwell between transitions.
            if mechanical_target_tier != current_mechanical_tier and dwell_elapsed < min_dwell_sec:
                mechanical_target_tier = current_mechanical_tier

            # Tier 2 re-entry cooldown: prevent rapid 2->0->2 oscillation.
            if mechanical_target_tier == 2 and current_mechanical_tier < 2:
                cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
                if cooldown_sec > 0 and self._regime_mechanical_tier2_last_downgrade_at > 0:
                    since_downgrade = now - self._regime_mechanical_tier2_last_downgrade_at
                    if since_downgrade < cooldown_sec:
                        mechanical_target_tier = 1 if directional_ok_tier1 else 0

            mechanical_reason = "hmm_eval"
        elif enabled:
            mechanical_reason = "hmm_not_ready"

        if int(mechanical_target_tier) != current_mechanical_tier:
            if current_mechanical_tier == 2 and int(mechanical_target_tier) < 2:
                self._regime_mechanical_tier2_last_downgrade_at = float(now)
            elif int(mechanical_target_tier) == 2:
                self._regime_mechanical_tier2_last_downgrade_at = 0.0
            self._regime_mechanical_tier = int(mechanical_target_tier)
            self._regime_mechanical_since = float(now)
            self._regime_mechanical_tier_entered_at = float(now)
        elif self._regime_mechanical_since <= 0.0:
            self._regime_mechanical_since = float(now)
        self._regime_mechanical_direction = self._tier_direction(
            int(mechanical_target_tier),
            str(regime),
            float(bias),
        )

        target_tier = int(mechanical_target_tier)
        reason = str(mechanical_reason)
        effective_direction = str(self._regime_mechanical_direction)

        # AI override lifecycle (manual apply/revert endpoint is added in P2).
        if override is None:
            ttl_max = max(1.0, float(getattr(config, "AI_OVERRIDE_MAX_TTL_SEC", 3600)))
            applied_at = float(self._ai_override_applied_at or 0.0)
            if applied_at > 0 and self._ai_override_until is not None:
                capped_until = min(float(self._ai_override_until), applied_at + ttl_max)
                self._ai_override_until = float(capped_until)

            expires_at = float(self._ai_override_until or 0.0)
            if expires_at > 0.0 and expires_at <= float(now):
                logger.info("AI regime advisor: override expired, reverting to mechanical")
                self._clear_ai_override()
                expires_at = 0.0

            if (
                self._ai_override_tier is not None
                and self._ai_override_direction in {"symmetric", "long_bias", "short_bias"}
                and expires_at > float(now)
            ):
                source_conv = int(self._ai_override_source_conviction or 0)
                min_conv = max(0, min(100, int(getattr(config, "AI_OVERRIDE_MIN_CONVICTION", 50))))
                if source_conv >= min_conv:
                    requested_tier = max(0, min(2, int(self._ai_override_tier)))
                    requested_direction = str(self._ai_override_direction)
                    low = max(0, int(mechanical_target_tier) - 1)
                    high = min(2, int(mechanical_target_tier) + 1)
                    applied_tier = max(low, min(high, requested_tier))
                    applied_direction = requested_direction
                    if applied_tier == 0:
                        applied_direction = "symmetric"

                    capacity_blocked = False
                    capacity_band = str(self._compute_capacity_health(now).get("status_band") or "normal")
                    if capacity_band == "stop" and applied_tier > int(mechanical_target_tier):
                        applied_tier = int(mechanical_target_tier)
                        applied_direction = str(self._regime_mechanical_direction)
                        capacity_blocked = True

                    target_tier = int(applied_tier)
                    effective_direction = str(applied_direction)
                    if (
                        capacity_blocked
                        and int(target_tier) == int(mechanical_target_tier)
                        and str(effective_direction) == str(self._regime_mechanical_direction)
                    ):
                        reason = "ai_override_capacity_blocked"
                    else:
                        reason = "ai_override"
                    effective_confidence = max(effective_confidence, max(0.0, min(1.0, source_conv / 100.0)))
                    if effective_direction == "long_bias":
                        effective_regime = "BULLISH"
                        effective_bias = max(0.25, abs(float(bias)))
                    elif effective_direction == "short_bias":
                        effective_regime = "BEARISH"
                        effective_bias = -max(0.25, abs(float(bias)))
                    else:
                        effective_regime = "RANGING"
                        effective_bias = 0.0
                else:
                    reason = "ai_override_rejected_conviction"

        current_tier = int(current_effective_tier)
        current_tier = max(0, min(2, current_tier))

        changed = target_tier != current_tier
        prev_entered_at = float(self._regime_tier_entered_at)
        prev_dwell_sec = max(0.0, now - prev_entered_at) if prev_entered_at > 0 else 0.0
        if changed:
            self._regime_tier = int(target_tier)
            self._regime_tier_entered_at = float(now)
            if int(target_tier) == 2:
                self._regime_tier2_grace_start = float(now)
                self._regime_tier2_last_downgrade_at = 0.0
                self._regime_cooldown_suppressed_side = None
            else:
                self._regime_tier2_grace_start = 0.0

        # Tier downgrade: clear regime ownership so balance-driven repair can restore both sides.
        # During cooldown, defer clearing to avoid rapid suppression churn.
        if changed and current_tier == 2 and int(target_tier) < 2:
            self._regime_tier2_last_downgrade_at = float(now)
            self._regime_cooldown_suppressed_side = (
                self._regime_side_suppressed if self._regime_side_suppressed in ("A", "B") else None
            )
            cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
            if cooldown_sec <= 0:
                for sid in sorted(self.slots.keys()):
                    st = self.slots[sid].state
                    if str(getattr(st, "mode_source", "none")) == "regime":
                        self.slots[sid].state = replace(st, mode_source="none")
                        logger.info(
                            "slot %s: cleared regime suppression (tier %d -> %d)",
                            sid,
                            int(current_tier),
                            int(target_tier),
                        )
                self._regime_cooldown_suppressed_side = None
            else:
                logger.info(
                    "tier %d -> %d: deferring regime clear for %.0fs cooldown",
                    int(current_tier),
                    int(target_tier),
                    cooldown_sec,
                )

        if int(target_tier) == 2:
            if effective_direction == "long_bias":
                suppressed_side = "A"
            elif effective_direction == "short_bias":
                suppressed_side = "B"
            elif str(effective_regime) in ("BULLISH", "BEARISH"):
                suppressed_side = "A" if float(effective_bias) > 0 else "B"
        self._regime_side_suppressed = suppressed_side

        if changed:
            self._regime_tier_history.append({
                "time": float(now),
                "from_tier": int(current_tier),
                "to_tier": int(target_tier),
                "regime": str(effective_regime),
                "confidence": round(float(effective_confidence), 3),
                "confidence_raw": round(float(confidence_raw), 3),
                "confidence_modifier": round(float(confidence_modifier), 3),
                "bias": round(float(effective_bias), 3),
                "mechanical_tier": int(mechanical_target_tier),
                "mechanical_direction": str(self._regime_mechanical_direction),
                "reason": str(reason),
            })
            if len(self._regime_tier_history) > 20:
                self._regime_tier_history = self._regime_tier_history[-20:]

        if changed:
            tier_labels = {0: "symmetric", 1: "biased", 2: "directional"}
            supabase_store.save_regime_tier_transition({
                "time": float(now),
                "pair": str(self.pair),
                "from_tier": int(current_tier),
                "to_tier": int(target_tier),
                "from_label": str(tier_labels.get(int(current_tier), "symmetric")),
                "to_label": str(tier_labels.get(int(target_tier), "symmetric")),
                "dwell_sec": float(prev_dwell_sec),
                "regime": str(effective_regime),
                "confidence": float(effective_confidence),
                "bias_signal": float(effective_bias),
                "abs_bias": abs(float(effective_bias)),
                "suppressed_side": suppressed_side,
                "favored_side": ("B" if suppressed_side == "A" else "A" if suppressed_side == "B" else None),
                "reason": str(reason),
                "shadow_enabled": bool(shadow_enabled),
                "actuation_enabled": bool(actuation_enabled),
                "hmm_ready": bool(hmm_ready),
            })

        if changed:
            logger.info(
                "[REGIME][shadow] tier %d -> %d regime=%s conf=%.3f bias=%.3f suppressed=%s",
                current_tier,
                int(target_tier),
                effective_regime,
                effective_confidence,
                effective_bias,
                suppressed_side or "-",
            )

        self._regime_shadow_state = {
            "enabled": enabled,
            "shadow_enabled": shadow_enabled,
            "actuation_enabled": actuation_enabled,
            "tier": int(target_tier),
            "regime": str(effective_regime),
            "confidence": float(effective_confidence),
            "confidence_raw": float(confidence_raw),
            "confidence_effective": float(confidence_effective),
            "confidence_modifier": float(confidence_modifier),
            "confidence_modifier_source": str(confidence_modifier_source),
            "bias_signal": float(effective_bias),
            "abs_bias": abs(float(effective_bias)),
            "suppressed_side": suppressed_side,
            "favored_side": ("B" if suppressed_side == "A" else "A" if suppressed_side == "B" else None),
            "directional_ok_tier1": bool(directional_ok_tier1),
            "directional_ok_tier2": bool(directional_ok_tier2),
            "hmm_ready": bool(hmm_ready),
            "last_eval_ts": float(now),
            "reason": reason,
            "mechanical_tier": int(mechanical_target_tier),
            "mechanical_direction": str(self._regime_mechanical_direction),
            "override_active": bool(reason == "ai_override"),
        }
        self._update_kelly()

    def _apply_tier2_suppression(self, now: float) -> None:
        if not bool(getattr(config, "REGIME_DIRECTIONAL_ENABLED", False)):
            return
        if int(self._regime_tier) != 2:
            return
        if not self._regime_grace_elapsed(now):
            return
        suppressed = self._regime_side_suppressed
        if suppressed not in ("A", "B"):
            return
        suppressed_side = "sell" if suppressed == "A" else "buy"

        # Regime can flip while still in Tier 2; release old-side regime ownership.
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            if str(getattr(st, "mode_source", "none")) != "regime":
                continue
            if suppressed == "A" and st.short_only:
                self.slots[sid].state = replace(st, mode_source="none")
            elif suppressed == "B" and st.long_only:
                self.slots[sid].state = replace(st, mode_source="none")

        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state

            if suppressed == "A" and st.long_only and str(getattr(st, "mode_source", "none")) == "regime":
                continue
            if suppressed == "B" and st.short_only and str(getattr(st, "mode_source", "none")) == "regime":
                continue

            if sm.derive_phase(st) != "S0":
                continue

            target_order = next(
                (
                    o
                    for o in st.orders
                    if o.role == "entry" and o.side == suppressed_side
                ),
                None,
            )

            # Preserve favored one-sided slots by tagging regime ownership.
            if target_order is None:
                if suppressed == "A" and st.long_only and not st.short_only and str(getattr(st, "mode_source", "none")) != "regime":
                    self.slots[sid].state = replace(st, mode_source="regime")
                elif suppressed == "B" and st.short_only and not st.long_only and str(getattr(st, "mode_source", "none")) != "regime":
                    self.slots[sid].state = replace(st, mode_source="regime")
                continue

            if target_order.txid:
                try:
                    if not self._cancel_order(target_order.txid):
                        logger.warning("slot %s tier2 cancel %s failed", sid, target_order.txid)
                        continue
                except Exception as e:
                    logger.warning("slot %s tier2 cancel %s failed: %s", sid, target_order.txid, e)
                    continue

            new_st = sm.remove_order(st, target_order.local_id)
            if suppressed == "A":
                new_st = replace(new_st, long_only=True, short_only=False, mode_source="regime")
            else:
                new_st = replace(new_st, short_only=True, long_only=False, mode_source="regime")
            self.slots[sid].state = new_st
            logger.info(
                "slot %s: tier2 suppressed %s entry (regime=%s, conf=%.3f)",
                sid,
                suppressed,
                self._hmm_consensus.get("regime", ""),
                float(self._hmm_consensus.get("confidence", 0.0)),
            )

    def _clear_expired_regime_cooldown(self, now: float) -> None:
        if int(self._regime_tier) == 2:
            self._regime_tier2_last_downgrade_at = 0.0
            self._regime_cooldown_suppressed_side = None
            return
        if self._regime_tier2_last_downgrade_at <= 0:
            return
        cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
        if cooldown_sec <= 0:
            return
        elapsed = now - self._regime_tier2_last_downgrade_at
        if elapsed < cooldown_sec:
            return
        cleared = 0
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            if str(getattr(st, "mode_source", "none")) == "regime":
                self.slots[sid].state = replace(st, mode_source="none")
                cleared += 1
        if cleared:
            logger.info(
                "cooldown expired (%.0fs): cleared regime ownership on %d slots",
                elapsed,
                cleared,
            )
        self._regime_tier2_last_downgrade_at = 0.0
        self._regime_cooldown_suppressed_side = None

    def _regime_status_payload(self, now: float | None = None) -> dict[str, Any]:
        now_ts = float(now if now is not None else _now())
        state = dict(self._regime_shadow_state or {})
        regime_default, confidence_default, bias_default, _, _ = self._policy_hmm_signal()
        tier = int(state.get("tier", self._regime_tier))
        tier = max(0, min(2, tier))
        suppressed = state.get("suppressed_side", self._regime_side_suppressed)
        suppressed = suppressed if suppressed in ("A", "B") else None
        if suppressed == "A":
            favored = "B"
        elif suppressed == "B":
            favored = "A"
        else:
            favored = None
        tier_label = {0: "symmetric", 1: "biased", 2: "directional"}[tier]
        dwell_sec = max(0.0, now_ts - float(self._regime_tier_entered_at or now_ts))
        grace_sec = max(0.0, float(getattr(config, "REGIME_SUPPRESSION_GRACE_SEC", 0.0)))
        grace_start = float(self._regime_tier2_grace_start or self._regime_tier_entered_at or now_ts)
        grace_remaining = max(0.0, grace_sec - max(0.0, now_ts - grace_start)) if tier == 2 else 0.0
        cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
        cooldown_remaining = 0.0
        cooldown_side = None
        if (
            cooldown_sec > 0
            and int(self._regime_tier) < 2
            and self._regime_tier2_last_downgrade_at > 0
        ):
            elapsed = max(0.0, now_ts - self._regime_tier2_last_downgrade_at)
            if elapsed < cooldown_sec:
                cooldown_remaining = cooldown_sec - elapsed
                if self._regime_cooldown_suppressed_side in ("A", "B"):
                    cooldown_side = self._regime_cooldown_suppressed_side
        return {
            "enabled": bool(state.get("enabled", False)),
            "shadow_enabled": bool(state.get("shadow_enabled", False)),
            "actuation_enabled": bool(state.get("actuation_enabled", False)),
            "tier": tier,
            "tier_label": tier_label,
            "suppressed_side": suppressed,
            "favored_side": favored,
            "regime": str(state.get("regime", regime_default)),
            "confidence": float(state.get("confidence", confidence_default)),
            "confidence_raw": float(state.get("confidence_raw", confidence_default)),
            "confidence_effective": float(state.get("confidence_effective", confidence_default)),
            "confidence_modifier": float(state.get("confidence_modifier", 1.0)),
            "confidence_modifier_source": str(state.get("confidence_modifier_source", "none")),
            "bias_signal": float(state.get("bias_signal", bias_default)),
            "abs_bias": float(state.get("abs_bias", abs(float(bias_default)))),
            "directional_ok_tier1": bool(state.get("directional_ok_tier1", False)),
            "directional_ok_tier2": bool(state.get("directional_ok_tier2", False)),
            "hmm_ready": bool(state.get("hmm_ready", False)),
            "dwell_sec": float(dwell_sec),
            "hysteresis_buffer": float(getattr(config, "REGIME_HYSTERESIS", 0.05)),
            "grace_remaining_sec": float(grace_remaining),
            "cooldown_remaining_sec": float(cooldown_remaining),
            "cooldown_suppressed_side": cooldown_side,
            "regime_suppressed_slots": sum(
                1
                for slot in self.slots.values()
                if str(getattr(slot.state, "mode_source", "none")) == "regime"
            ),
            "tier_history": list(self._regime_tier_history[-20:]),
            "last_eval_ts": float(state.get("last_eval_ts", self._regime_last_eval_ts)),
            "reason": str(state.get("reason", "")),
        }

    def _ai_regime_status_payload(self, now: float | None = None) -> dict[str, Any]:
        now_ts = float(now if now is not None else _now())
        enabled = bool(getattr(config, "AI_REGIME_ADVISOR_ENABLED", False))
        last_run_ts = float(self._ai_regime_last_run_ts or 0.0)
        last_run_age = (now_ts - last_run_ts) if last_run_ts > 0.0 else None
        interval_sec = max(1.0, float(getattr(config, "AI_REGIME_INTERVAL_SEC", 300.0)))
        if not enabled:
            next_run_in = None
        elif last_run_ts <= 0.0:
            next_run_in = 0
        else:
            next_run_in = int(max(0.0, interval_sec - max(0.0, now_ts - last_run_ts)))

        opinion = dict(self._ai_regime_opinion or {})
        recommended_tier = max(0, min(2, int(opinion.get("recommended_tier", 0) or 0)))
        recommended_direction = str(opinion.get("recommended_direction", "symmetric") or "symmetric").strip().lower()
        if recommended_direction not in {"symmetric", "long_bias", "short_bias"}:
            recommended_direction = "symmetric"
        agreement = str(opinion.get("agreement", "unknown") or "unknown")
        conviction = max(0, min(100, int(opinion.get("conviction", 0) or 0)))

        opinion_payload = {
            "recommended_tier": int(recommended_tier),
            "recommended_direction": str(recommended_direction),
            "conviction": int(conviction),
            "rationale": str(opinion.get("rationale", "") or ""),
            "watch_for": str(opinion.get("watch_for", "") or ""),
            "panelist": str(opinion.get("panelist", "") or ""),
            "agreement": agreement,
            "error": str(opinion.get("error", "") or ""),
            "trigger": str(opinion.get("trigger", "") or ""),
            "ts": float(opinion.get("ts", 0.0) or 0.0),
            "mechanical_tier": int(opinion.get("mechanical_tier", self._regime_mechanical_tier) or 0),
            "mechanical_direction": str(
                opinion.get("mechanical_direction", self._regime_mechanical_direction) or "symmetric"
            ),
        }

        return {
            "enabled": enabled,
            "thread_alive": bool(self._ai_regime_thread_alive),
            "dismissed": bool(self._ai_regime_dismissed),
            "default_ttl_sec": int(max(1, int(getattr(config, "AI_OVERRIDE_TTL_SEC", 1800)))),
            "max_ttl_sec": int(max(1, int(getattr(config, "AI_OVERRIDE_MAX_TTL_SEC", 3600)))),
            "min_conviction": int(max(0, min(100, int(getattr(config, "AI_OVERRIDE_MIN_CONVICTION", 50))))),
            "last_run_ts": (last_run_ts if last_run_ts > 0.0 else None),
            "last_run_age_sec": (float(last_run_age) if last_run_age is not None else None),
            "next_run_in_sec": next_run_in,
            "opinion": opinion_payload,
            "override": self._ai_override_payload(now_ts),
            "history": list(self._ai_regime_history),
        }

    def _normalize_kraken_ohlcv_rows(
        self,
        rows: list,
        *,
        interval_min: int,
        now_ts: float | None = None,
    ) -> list[dict[str, float | int | None]]:
        """
        Parse Kraken OHLC rows into normalized candle dicts sorted by time.
        """
        interval_sec = max(60, int(interval_min) * 60)
        now_ref = float(now_ts if now_ts is not None else _now())
        out: dict[float, dict[str, float | int | None]] = {}

        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            try:
                ts = float(row[0])
                o = float(row[1])
                h = float(row[2])
                l = float(row[3])
                c = float(row[4])
                v = float(row[6])
                tc = int(float(row[7])) if len(row) > 7 else None
            except (TypeError, ValueError):
                continue

            if ts <= 0 or min(o, h, l, c) <= 0 or v < 0:
                continue

            # Skip the still-forming bar.
            if (ts + interval_sec) > now_ref + 1e-9:
                continue

            out[ts] = {
                "time": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "trade_count": tc,
            }

        return [out[k] for k in sorted(out.keys())]

    @staticmethod
    def _extract_close_volume(
        candles: list[dict[str, float | int | None]],
    ) -> tuple[list[float], list[float]]:
        closes: list[float] = []
        volumes: list[float] = []
        for row in candles:
            try:
                c = float(row.get("close", 0.0))
                v = float(row.get("volume", 0.0))
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            closes.append(c)
            volumes.append(max(0.0, v))
        return closes, volumes

    def _sync_ohlcv_candles_for_interval(
        self,
        now: float,
        *,
        interval_min: int,
        sync_interval_sec: float,
        state_key: str,
    ) -> None:
        """
        Pull recent Kraken OHLCV for one interval and queue upserts to Supabase.
        """
        if not bool(getattr(config, "HMM_OHLCV_ENABLED", True)):
            return

        interval = max(1, int(interval_min))
        sync_interval = max(30.0, float(sync_interval_sec))
        if state_key == "secondary":
            last_sync_ts = float(self._ohlcv_secondary_last_sync_ts)
            since_cursor = int(self._ohlcv_secondary_since_cursor) if self._ohlcv_secondary_since_cursor else None
        else:
            last_sync_ts = float(self._ohlcv_last_sync_ts)
            since_cursor = int(self._ohlcv_since_cursor) if self._ohlcv_since_cursor else None

        if last_sync_ts > 0 and (now - last_sync_ts) < sync_interval:
            return

        if state_key == "secondary":
            self._ohlcv_secondary_last_sync_ts = now
        else:
            self._ohlcv_last_sync_ts = now

        try:
            rows, last_cursor = kraken_client.get_ohlc_page(
                pair=self.pair,
                interval=interval,
                since=since_cursor,
            )
            candles = self._normalize_kraken_ohlcv_rows(
                rows,
                interval_min=interval,
                now_ts=now,
            )
            if candles:
                supabase_store.queue_ohlcv_candles(
                    candles,
                    pair=self.pair,
                    interval_min=interval,
                )
                self._hmm_readiness_cache.pop(state_key, None)
                self._hmm_readiness_last_ts.pop(state_key, None)
                if state_key == "secondary":
                    self._ohlcv_secondary_last_rows_queued = len(candles)
                    self._ohlcv_secondary_last_candle_ts = float(candles[-1]["time"])
                else:
                    self._ohlcv_last_rows_queued = len(candles)
                    self._ohlcv_last_candle_ts = float(candles[-1]["time"])
            else:
                if state_key == "secondary":
                    self._ohlcv_secondary_last_rows_queued = 0
                else:
                    self._ohlcv_last_rows_queued = 0

            if last_cursor is not None:
                try:
                    next_cursor = int(last_cursor)
                    if state_key == "secondary":
                        self._ohlcv_secondary_since_cursor = next_cursor
                    else:
                        self._ohlcv_since_cursor = next_cursor
                except (TypeError, ValueError):
                    pass
            elif candles:
                if state_key == "secondary":
                    self._ohlcv_secondary_since_cursor = int(float(candles[-1]["time"]))
                else:
                    self._ohlcv_since_cursor = int(float(candles[-1]["time"]))
        except Exception as e:
            logger.warning(
                "OHLCV sync failed (%s interval=%dm): %s",
                state_key,
                interval,
                e,
            )

    def _sync_ohlcv_candles(self, now: float | None = None) -> None:
        """
        Pull recent Kraken OHLCV and queue upserts to Supabase for active intervals.
        """
        now_ts = float(now if now is not None else _now())
        self._sync_ohlcv_candles_for_interval(
            now_ts,
            interval_min=max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5))),
            sync_interval_sec=max(30.0, float(getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0))),
            state_key="primary",
        )

        secondary_collect_enabled = bool(getattr(config, "HMM_SECONDARY_OHLCV_ENABLED", False)) or bool(
            getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)
        )
        if secondary_collect_enabled:
            self._sync_ohlcv_candles_for_interval(
                now_ts,
                interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
                sync_interval_sec=max(
                    30.0,
                    float(
                        getattr(
                            config,
                            "HMM_SECONDARY_SYNC_INTERVAL_SEC",
                            getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                        )
                    ),
                ),
                state_key="secondary",
            )


    def backfill_ohlcv_history(
        self,
        target_candles: int | None = None,
        max_pages: int | None = None,
        *,
        interval_min: int | None = None,
        state_key: str = "primary",
    ) -> tuple[bool, str]:
        """
        Best-effort historical OHLCV backfill for faster HMM warm-up.
        Queues rows into Supabase writer; ingestion is asynchronous.
        """
        if not bool(getattr(config, "HMM_OHLCV_ENABLED", True)):
            msg = "ohlcv pipeline disabled"
            if state_key == "secondary":
                self._hmm_backfill_last_message_secondary = msg
            else:
                self._hmm_backfill_last_message = msg
            return False, msg

        default_target = (
            int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720))
            if state_key == "secondary"
            else int(getattr(config, "HMM_TRAINING_CANDLES", 720))
        )
        target = max(1, int(target_candles if target_candles is not None else default_target))
        pages = max(1, int(max_pages if max_pages is not None else getattr(config, "HMM_OHLCV_BACKFILL_MAX_PAGES", 40)))
        if interval_min is None:
            interval = max(
                1,
                int(
                    getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)
                    if state_key == "secondary"
                    else getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5)
                ),
            )
        else:
            interval = max(1, int(interval_min))
        existing = supabase_store.load_ohlcv_candles(
            limit=target,
            pair=self.pair,
            interval_min=interval,
        )
        existing_ts: set[float] = set()
        for row in existing:
            try:
                existing_ts.add(float(row.get("time")))
            except Exception:
                continue
        existing_count = len(existing_ts)
        if existing_count >= target:
            if state_key == "secondary":
                self._hmm_backfill_last_at_secondary = _now()
                self._hmm_backfill_last_rows_secondary = 0
                self._hmm_backfill_last_message_secondary = f"already_ready:{existing_count}/{target}"
            else:
                self._hmm_backfill_last_at = _now()
                self._hmm_backfill_last_rows = 0
                self._hmm_backfill_last_message = f"already_ready:{existing_count}/{target}"
            return True, f"OHLCV already sufficient: {existing_count}/{target}"

        stall_limit = max(1, int(getattr(config, "HMM_BACKFILL_MAX_STALLS", 3)))
        stall_count = (
            self._hmm_backfill_stall_count_secondary
            if state_key == "secondary"
            else self._hmm_backfill_stall_count
        )
        if stall_count >= stall_limit:
            msg = f"backfill_circuit_open:stalls={stall_count}/{stall_limit}"
            if state_key == "secondary":
                self._hmm_backfill_last_at_secondary = _now()
                self._hmm_backfill_last_rows_secondary = 0
                self._hmm_backfill_last_message_secondary = msg
            else:
                self._hmm_backfill_last_at = _now()
                self._hmm_backfill_last_rows = 0
                self._hmm_backfill_last_message = msg
            return False, f"Backfill circuit-breaker open ({stall_count} consecutive stalls)"

        # Kraken OHLC uses an opaque cursor; start without `since` and paginate
        # only with Kraken's returned `last` value.
        cursor = 0

        fetched: dict[float, dict[str, float | int | None]] = {}
        for _ in range(pages):
            try:
                rows, last_cursor = kraken_client.get_ohlc_page(
                    pair=self.pair,
                    interval=interval,
                    since=cursor if cursor > 0 else None,
                )
            except Exception as e:
                if state_key == "secondary":
                    self._hmm_backfill_last_message_secondary = f"fetch_failed:{e}"
                else:
                    self._hmm_backfill_last_message = f"fetch_failed:{e}"
                break

            parsed = self._normalize_kraken_ohlcv_rows(
                rows,
                interval_min=interval,
                now_ts=_now(),
            )
            for row in parsed:
                fetched[float(row["time"])] = row

            if last_cursor is None:
                break
            try:
                next_cursor = int(last_cursor)
            except (TypeError, ValueError):
                break
            if next_cursor <= cursor:
                break
            cursor = next_cursor

        queued_rows = 0
        new_unique = 0
        if fetched:
            payload = [fetched[k] for k in sorted(fetched.keys())]
            supabase_store.queue_ohlcv_candles(
                payload,
                pair=self.pair,
                interval_min=interval,
            )
            queued_rows = len(payload)
            new_unique = sum(1 for ts in fetched.keys() if ts not in existing_ts)

        if new_unique == 0:
            if state_key == "secondary":
                self._hmm_backfill_stall_count_secondary += 1
            else:
                self._hmm_backfill_stall_count += 1
        else:
            if state_key == "secondary":
                self._hmm_backfill_stall_count_secondary = 0
            else:
                self._hmm_backfill_stall_count = 0

        if state_key == "secondary":
            self._hmm_backfill_last_at_secondary = _now()
            self._hmm_backfill_last_rows_secondary = int(queued_rows)
            current_stalls = self._hmm_backfill_stall_count_secondary
        else:
            self._hmm_backfill_last_at = _now()
            self._hmm_backfill_last_rows = int(queued_rows)
            current_stalls = self._hmm_backfill_stall_count
        est_total = existing_count + new_unique
        backfill_msg = f"queued={queued_rows} new={new_unique} est_total={est_total}/{target}"
        if current_stalls > 0:
            backfill_msg += f" stalls={current_stalls}"
        if state_key == "secondary":
            self._hmm_backfill_last_message_secondary = backfill_msg
        else:
            self._hmm_backfill_last_message = backfill_msg
        self._hmm_readiness_cache.pop(state_key, None)
        self._hmm_readiness_last_ts.pop(state_key, None)

        if queued_rows <= 0:
            return False, (
                "OHLCV backfill queued no rows "
                f"({state_key}, interval={interval}m); "
                f"existing={existing_count}/{target}, max_pages={pages}"
            )

        return True, (
            f"OHLCV backfill queued {queued_rows} rows ({state_key}, interval={interval}m) "
            f"({new_unique} new, est {est_total}/{target})"
        )

    def _maybe_backfill_ohlcv_on_startup(self) -> None:
        if not bool(getattr(config, "HMM_OHLCV_BACKFILL_ON_STARTUP", True)):
            return
        now_ts = _now()
        readiness = self._hmm_data_readiness(now_ts)
        if bool(readiness.get("ready_for_target_window", False)):
            logger.info("OHLCV startup backfill skipped (primary already ready)")
        else:
            ok, msg = self.backfill_ohlcv_history(
                target_candles=int(getattr(config, "HMM_TRAINING_CANDLES", 720)),
                max_pages=int(getattr(config, "HMM_OHLCV_BACKFILL_MAX_PAGES", 40)),
                interval_min=max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
                state_key="primary",
            )
            if ok:
                logger.info("OHLCV startup backfill: %s", msg)
            else:
                logger.warning("OHLCV startup backfill: %s", msg)

        secondary_collect_enabled = bool(getattr(config, "HMM_SECONDARY_OHLCV_ENABLED", False)) or bool(
            getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)
        )
        if not secondary_collect_enabled:
            return

        secondary_readiness = self._hmm_data_readiness(
            now_ts,
            interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
            training_target=max(1, int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720))),
            min_samples=max(1, int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200))),
            sync_interval_sec=max(
                30.0,
                float(
                    getattr(
                        config,
                        "HMM_SECONDARY_SYNC_INTERVAL_SEC",
                        getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                    )
                ),
            ),
            state_key="secondary",
        )
        if bool(secondary_readiness.get("ready_for_target_window", False)):
            logger.info("OHLCV startup backfill skipped (secondary already ready)")
            return

        ok, msg = self.backfill_ohlcv_history(
            target_candles=int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720)),
            max_pages=int(getattr(config, "HMM_OHLCV_BACKFILL_MAX_PAGES", 40)),
            interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
            state_key="secondary",
        )
        if ok:
            logger.info("OHLCV startup backfill (secondary): %s", msg)
        else:
            logger.warning("OHLCV startup backfill (secondary): %s", msg)

    def _load_recent_ohlcv_rows(
        self,
        count: int,
        *,
        interval_min: int,
    ) -> list[dict[str, float | int | None]]:
        """
        Load recent OHLCV candles, preferring Supabase and falling back to Kraken.
        """
        limit = max(1, int(count))
        interval = max(1, int(interval_min))
        supa_rows = supabase_store.load_ohlcv_candles(
            limit=limit,
            pair=self.pair,
            interval_min=interval,
        )

        merged: dict[float, dict[str, float | int | None]] = {}
        for row in supa_rows:
            try:
                ts = float(row.get("time"))
            except (TypeError, ValueError):
                continue
            merged[ts] = row

        need_fallback = len(merged) < limit
        if need_fallback:
            try:
                kr_rows = kraken_client.get_ohlc(pair=self.pair, interval=interval)
                parsed = self._normalize_kraken_ohlcv_rows(
                    kr_rows,
                    interval_min=interval,
                    now_ts=_now(),
                )
                for row in parsed:
                    merged[float(row["time"])] = row
            except Exception as e:
                logger.debug("OHLCV fallback fetch failed: %s", e)

        out = [merged[k] for k in sorted(merged.keys())]
        if len(out) > limit:
            out = out[-limit:]
        return out

    def _fetch_training_candles(
        self,
        count: int | None = None,
        *,
        interval_min: int | None = None,
    ) -> tuple[list[float], list[float]]:
        interval = max(
            1,
            int(
                getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5)
                if interval_min is None
                else interval_min
            ),
        )
        target = max(1, int(count if count is not None else getattr(config, "HMM_TRAINING_CANDLES", 720)))
        rows = self._load_recent_ohlcv_rows(target, interval_min=interval)
        return self._extract_close_volume(rows)

    def _fetch_recent_candles(
        self,
        count: int | None = None,
        *,
        interval_min: int | None = None,
    ) -> tuple[list[float], list[float]]:
        interval = max(
            1,
            int(
                getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5)
                if interval_min is None
                else interval_min
            ),
        )
        target = max(1, int(count if count is not None else getattr(config, "HMM_RECENT_CANDLES", 100)))
        rows = self._load_recent_ohlcv_rows(target, interval_min=interval)
        return self._extract_close_volume(rows)

    def _hmm_data_readiness(
        self,
        now: float | None = None,
        *,
        interval_min: int | None = None,
        training_target: int | None = None,
        min_samples: int | None = None,
        sync_interval_sec: float | None = None,
        state_key: str = "primary",
    ) -> dict[str, Any]:
        """
        Runtime readiness summary for HMM training data.
        """
        now_ts = float(now if now is not None else _now())
        ttl = max(5.0, float(getattr(config, "HMM_READINESS_CACHE_SEC", 300.0)))
        use_state_key = "secondary" if str(state_key).lower() == "secondary" else "primary"
        if interval_min is None:
            interval = max(
                1,
                int(
                    getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)
                    if use_state_key == "secondary"
                    else getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5)
                ),
            )
        else:
            interval = max(1, int(interval_min))
        if training_target is None:
            target = max(
                1,
                int(
                    getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720)
                    if use_state_key == "secondary"
                    else getattr(config, "HMM_TRAINING_CANDLES", 720)
                ),
            )
        else:
            target = max(1, int(training_target))
        if min_samples is None:
            min_train = max(
                1,
                int(
                    getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)
                    if use_state_key == "secondary"
                    else getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)
                ),
            )
        else:
            min_train = max(1, int(min_samples))
        if sync_interval_sec is None:
            sync_interval = (
                float(getattr(config, "HMM_SECONDARY_SYNC_INTERVAL_SEC", 300.0))
                if use_state_key == "secondary"
                else float(getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0))
            )
        else:
            sync_interval = float(sync_interval_sec)

        cached = self._hmm_readiness_cache.get(use_state_key)
        last_cache_ts = float(self._hmm_readiness_last_ts.get(use_state_key, 0.0))
        if cached and (now_ts - last_cache_ts) < ttl:
            cached_interval = int(cached.get("interval_min", 0) or 0)
            cached_target = int(cached.get("training_target", 0) or 0)
            cached_min = int(cached.get("min_train_samples", 0) or 0)
            if cached_interval == interval and cached_target == target and cached_min == min_train:
                return dict(cached)

        try:
            secondary_collect_enabled = bool(getattr(config, "HMM_SECONDARY_OHLCV_ENABLED", False)) or bool(
                getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)
            )
            enabled = bool(getattr(config, "HMM_OHLCV_ENABLED", True))
            if use_state_key == "secondary":
                enabled = bool(enabled and secondary_collect_enabled)
            rows = supabase_store.load_ohlcv_candles(
                limit=target,
                pair=self.pair,
                interval_min=interval,
            )
            source = "supabase" if rows else "none"

            closes, volumes = self._extract_close_volume(rows)
            sample_count = min(len(closes), len(volumes))
            span_sec = 0.0
            last_candle_ts = None
            if rows:
                try:
                    first_ts = float(rows[0].get("time", 0.0))
                    last_candle_ts = float(rows[-1].get("time", 0.0))
                    span_sec = max(0.0, last_candle_ts - first_ts)
                except (TypeError, ValueError):
                    last_candle_ts = None
                    span_sec = 0.0

            freshness_sec = (now_ts - last_candle_ts) if last_candle_ts is not None else None
            interval_sec = interval * 60.0
            # Keep short-interval feeds honest (e.g., 1m should not tolerate 15m stale data).
            freshness_limit_sec = max(180.0, interval_sec * 3.0)
            freshness_ok = bool(freshness_sec is not None and freshness_sec <= freshness_limit_sec)
            volume_nonzero_count = sum(1 for v in volumes if v > 0)
            volume_coverage_pct = (volume_nonzero_count / sample_count * 100.0) if sample_count else 0.0
            coverage_pct = sample_count / target * 100.0 if target > 0 else 0.0

            gaps: list[str] = []
            if not enabled:
                gaps.append("pipeline_disabled")
            if sample_count < min_train:
                gaps.append(f"insufficient_samples:{sample_count}/{min_train}")
            if sample_count < target:
                gaps.append(f"below_target_window:{sample_count}/{target}")
            if not freshness_ok:
                gaps.append("stale_candles")
            if volume_coverage_pct < 95.0:
                gaps.append(f"low_volume_coverage:{volume_coverage_pct:.1f}%")
            if source != "supabase":
                gaps.append("no_supabase_ohlcv")

            if use_state_key == "secondary":
                last_sync_ts = float(self._ohlcv_secondary_last_sync_ts)
                last_sync_rows_queued = int(self._ohlcv_secondary_last_rows_queued)
                sync_cursor = self._ohlcv_secondary_since_cursor
                backfill_last_at = float(self._hmm_backfill_last_at_secondary)
                backfill_last_rows = int(self._hmm_backfill_last_rows_secondary)
                backfill_last_message = str(self._hmm_backfill_last_message_secondary or "")
            else:
                last_sync_ts = float(self._ohlcv_last_sync_ts)
                last_sync_rows_queued = int(self._ohlcv_last_rows_queued)
                sync_cursor = self._ohlcv_since_cursor
                backfill_last_at = float(self._hmm_backfill_last_at)
                backfill_last_rows = int(self._hmm_backfill_last_rows)
                backfill_last_message = str(self._hmm_backfill_last_message or "")

            out = {
                "enabled": bool(enabled),
                "state_key": use_state_key,
                "source": source,
                "interval_min": interval,
                "training_target": target,
                "min_train_samples": min_train,
                "samples": sample_count,
                "coverage_pct": round(coverage_pct, 2),
                "span_hours": round(span_sec / 3600.0, 2),
                "last_candle_ts": last_candle_ts,
                "freshness_sec": freshness_sec,
                "freshness_limit_sec": freshness_limit_sec,
                "freshness_ok": freshness_ok,
                "volume_coverage_pct": round(volume_coverage_pct, 2),
                "ready_for_min_train": bool(enabled and sample_count >= min_train and freshness_ok),
                "ready_for_target_window": bool(enabled and sample_count >= target and freshness_ok),
                "gaps": gaps,
                "sync_interval_sec": float(sync_interval),
                "last_sync_ts": last_sync_ts,
                "last_sync_rows_queued": last_sync_rows_queued,
                "sync_cursor": sync_cursor,
                "backfill_last_at": backfill_last_at,
                "backfill_last_rows": backfill_last_rows,
                "backfill_last_message": backfill_last_message,
            }
        except Exception as e:
            out = {
                "enabled": bool(getattr(config, "HMM_OHLCV_ENABLED", True)),
                "state_key": use_state_key,
                "error": str(e),
                "ready_for_min_train": False,
                "ready_for_target_window": False,
                "gaps": ["readiness_check_failed"],
                "backfill_last_at": (
                    float(self._hmm_backfill_last_at_secondary)
                    if use_state_key == "secondary"
                    else float(self._hmm_backfill_last_at)
                ),
                "backfill_last_rows": (
                    int(self._hmm_backfill_last_rows_secondary)
                    if use_state_key == "secondary"
                    else int(self._hmm_backfill_last_rows)
                ),
                "backfill_last_message": (
                    str(self._hmm_backfill_last_message_secondary or "")
                    if use_state_key == "secondary"
                    else str(self._hmm_backfill_last_message or "")
                ),
            }

        self._hmm_readiness_cache[use_state_key] = dict(out)
        self._hmm_readiness_last_ts[use_state_key] = now_ts
        return out

    def _price_age_sec(self) -> float:
        if self.last_price_ts <= 0:
            return 1e9
        return max(0.0, _now() - self.last_price_ts)

    def _volatility_profit_pct(self) -> float:
        # Volatility-aware runtime target: user's profit_pct is the base,
        # volatility applies a bounded multiplier (same pattern as entry_pct + HMM).
        base = self.profit_pct
        if base <= 0:
            base = float(config.VOLATILITY_PROFIT_FLOOR)

        samples = [p for _, p in self.price_history[-180:]]
        if len(samples) < 12:
            return base

        ranges = []
        for i in range(1, len(samples)):
            prev = samples[i - 1]
            cur = samples[i]
            if prev > 0:
                ranges.append(abs(cur - prev) / prev * 100.0)
        if not ranges:
            return base

        vol_suggested = median(ranges) * 2.0 * float(config.VOLATILITY_PROFIT_FACTOR)

        # Express as multiplier on user's base, clamped to configured bounds.
        raw_mult = vol_suggested / base if base > 0 else 1.0
        mult = max(float(config.VOLATILITY_PROFIT_MULT_FLOOR),
                   min(float(config.VOLATILITY_PROFIT_MULT_CEILING), raw_mult))
        target = base * mult

        # Absolute ceiling still applies.
        target = min(float(config.VOLATILITY_PROFIT_CEILING), target)

        # Never below fee floor.
        fee_floor = self.maker_fee_pct * 2.0 + 0.1
        target = max(target, fee_floor)
        return round(target, 4)

    def _utc_day_key(self, ts: float | None = None) -> str:
        dt = datetime.fromtimestamp(ts if ts is not None else _now(), timezone.utc)
        return dt.strftime("%Y-%m-%d")

    def _compute_daily_realized_loss_utc(self, now_ts: float | None = None) -> float:
        now_ts = float(now_ts if now_ts is not None else _now())
        now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
        day_start_dt = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc)
        day_start = day_start_dt.timestamp()
        day_end = day_start + 86400.0

        loss_total = 0.0
        for slot in self.slots.values():
            for cycle in slot.state.completed_cycles:
                exit_ts = float(getattr(cycle, "exit_time", 0.0) or 0.0)
                if exit_ts <= 0.0 or exit_ts < day_start or exit_ts >= day_end:
                    continue
                net = float(getattr(cycle, "net_profit", 0.0) or 0.0)
                if net < 0.0:
                    loss_total += -net
        return loss_total

    def _update_daily_loss_lock(self, now_ts: float | None = None) -> float:
        now_ts = float(now_ts if now_ts is not None else _now())
        utc_day = self._utc_day_key(now_ts)

        # Auto-clear at UTC rollover, but require manual operator resume.
        if self._daily_loss_lock_active and self._daily_loss_lock_utc_day and self._daily_loss_lock_utc_day != utc_day:
            self._daily_loss_lock_active = False
            self._daily_loss_lock_utc_day = ""
            if self.mode == "PAUSED" and str(self.pause_reason).startswith("daily loss limit hit"):
                self.pause_reason = "daily loss lock cleared at UTC rollover; manual resume required"

        daily_loss = self._compute_daily_realized_loss_utc(now_ts)
        self._daily_realized_loss_utc = float(daily_loss)

        limit = max(0.0, float(config.DAILY_LOSS_LIMIT))

        # If limit is disabled (0) or raised above current loss, clear any
        # existing lock on the same day so resume() is not blocked.
        if self._daily_loss_lock_active and (limit <= 0.0 or daily_loss + 1e-12 < limit):
            logger.info(
                "daily loss lock cleared: loss $%.4f < limit $%.4f (or limit disabled)",
                daily_loss, limit,
            )
            self._daily_loss_lock_active = False
            self._daily_loss_lock_utc_day = ""

        if limit <= 0.0:
            return daily_loss

        if daily_loss + 1e-12 < limit:
            return daily_loss

        if not self._daily_loss_lock_active or self._daily_loss_lock_utc_day != utc_day:
            self._daily_loss_lock_active = True
            self._daily_loss_lock_utc_day = utc_day
            reason = f"daily loss limit hit: ${daily_loss:.4f} >= ${limit:.4f} (UTC {utc_day})"
            self.pause(reason)
        return daily_loss

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

        now_ts = _now()
        suppressed = None
        cooldown_suppressed = (
            self._regime_cooldown_suppressed_side
            if self._regime_cooldown_suppressed_side in ("A", "B")
            else None
        )
        if bool(getattr(config, "REGIME_DIRECTIONAL_ENABLED", False)):
            if int(self._regime_tier) == 2 and self._regime_grace_elapsed(now_ts):
                if self._regime_side_suppressed in ("A", "B"):
                    suppressed = self._regime_side_suppressed
            elif (
                self._regime_tier2_last_downgrade_at > 0
                and cooldown_suppressed in ("A", "B")
            ):
                cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
                elapsed = now_ts - self._regime_tier2_last_downgrade_at
                if elapsed < cooldown_sec:
                    suppressed = cooldown_suppressed

        if suppressed == "A":
            if usd >= min_cost:
                st = replace(slot.state, long_only=True, short_only=False, mode_source="regime")
                st, action = sm.add_entry_order(
                    st,
                    cfg,
                    side="buy",
                    trade_id="B",
                    cycle=st.cycle_b,
                    order_size_usd=self._slot_order_size_usd(slot),
                    reason="bootstrap_regime_long_only",
                )
                self.slots[slot_id].state = st
                if action:
                    self._execute_actions(slot_id, [action], "bootstrap_regime")
                else:
                    logger.info("slot %s bootstrap_regime waiting: buy entry below minimum", slot_id)
            else:
                logger.info(
                    "slot %s bootstrap waiting: regime suppresses A, insufficient USD for B",
                    slot_id,
                )
            return

        if suppressed == "B":
            if doge >= min_vol:
                st = replace(slot.state, short_only=True, long_only=False, mode_source="regime")
                st, action = sm.add_entry_order(
                    st,
                    cfg,
                    side="sell",
                    trade_id="A",
                    cycle=st.cycle_a,
                    order_size_usd=self._slot_order_size_usd(slot),
                    reason="bootstrap_regime_short_only",
                )
                self.slots[slot_id].state = st
                if action:
                    self._execute_actions(slot_id, [action], "bootstrap_regime")
                else:
                    logger.info("slot %s bootstrap_regime waiting: sell entry below minimum", slot_id)
            else:
                logger.info(
                    "slot %s bootstrap waiting: regime suppresses B, insufficient DOGE for A",
                    slot_id,
                )
            return

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
            self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")
            if actions:
                self._execute_actions(slot_id, actions, "bootstrap")
            else:
                target_usd = self._slot_order_size_usd(slot)
                min_vol = float(self.constraints.get("min_volume", 13.0))
                min_cost = float(self.constraints.get("min_cost_usd", 0.0))
                required_usd = max(min_cost, min_vol * market)
                logger.info(
                    "slot %s bootstrap waiting: target $%.4f < required $%.4f "
                    "(ORDER_SIZE_USD=$%.4f, total_profit=$%.4f, min_vol=%.1f, "
                    "min_cost=$%.4f, market=$%.6f)",
                    slot_id, target_usd, required_usd,
                    float(config.ORDER_SIZE_USD), slot.state.total_profit,
                    min_vol, min_cost, market,
                )
            return

        # Symmetric auto-reseed.
        if usd < min_cost and doge >= 2 * min_vol:
            st = replace(slot.state, short_only=True, long_only=False, mode_source="balance")
            target_usd = market * (2 * min_vol)
            st, a = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=target_usd, reason="reseed_usd")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "bootstrap_reseed_usd")
            else:
                logger.info("slot %s reseed_usd waiting: computed order below minimum", slot_id)
            return

        if doge < min_vol and usd >= 2 * min_cost:
            st = replace(slot.state, long_only=True, short_only=False, mode_source="balance")
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
            st = replace(slot.state, short_only=True, long_only=False, mode_source="balance")
            st, a = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=market * min_vol, reason="fallback_short_only")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "fallback_short_only")
            else:
                logger.info("slot %s fallback_short_only waiting: computed order below minimum", slot_id)
            return

        if usd >= min_cost:
            st = replace(slot.state, long_only=True, short_only=False, mode_source="balance")
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

        if str(getattr(st, "mode_source", "none")) == "regime":
            # Check if the regime still requires suppression.
            # If suppression has lapsed (tier dropped, cooldown expired),
            # clear mode_source and fall through to attempt normal repair.
            still_suppressed = False
            if bool(getattr(config, "REGIME_DIRECTIONAL_ENABLED", False)):
                now_ts = _now()
                if int(self._regime_tier) == 2 and self._regime_grace_elapsed(now_ts):
                    if self._regime_side_suppressed in ("A", "B"):
                        still_suppressed = True
                elif self._regime_tier2_last_downgrade_at > 0:
                    cd_side = (
                        self._regime_cooldown_suppressed_side
                        if self._regime_cooldown_suppressed_side in ("A", "B")
                        else None
                    )
                    if cd_side:
                        cd_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
                        if now_ts - self._regime_tier2_last_downgrade_at < cd_sec:
                            still_suppressed = True
            if still_suppressed:
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
            self.slots[slot_id].state = replace(post, long_only=False, short_only=False, mode_source="none")
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
        order_sizes = {
            "A": self._slot_order_size_usd(slot, trade_id="A"),
            "B": self._slot_order_size_usd(slot, trade_id="B"),
        }
        order_size = self._slot_order_size_usd(slot)

        new_state, actions = sm.transition(
            slot.state,
            event,
            cfg,
            order_size_usd=order_size,
            order_sizes=order_sizes,
        )
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
                slot.state = replace(slot.state, long_only=True, short_only=False, mode_source="balance")
            elif action.side == "buy":
                slot.state = replace(slot.state, short_only=True, long_only=False, mode_source="balance")

        # Pre-compute order capacity for gating new entries.
        _internal_order_count = self._internal_open_order_count()
        _pair_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
        _safe_ratio = min(1.0, max(0.1, float(config.OPEN_ORDER_SAFETY_RATIO)))
        _order_cap = max(1, int(_pair_limit * _safe_ratio))

        for action in actions:
            if isinstance(action, sm.PlaceOrderAction):
                if action.role == "exit" and action.reason == "entry_fill_exit":
                    slot.state = sm.apply_order_regime_at_entry(
                        slot.state,
                        action.local_id,
                        self._current_regime_id(),
                    )
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

                if action.role == "entry" and self.entry_adds_per_loop_used >= self.entry_adds_per_loop_cap:
                    # Bootstrap and auto-repair entries bypass the scheduler cap so
                    # both sides of a slot are placed atomically.
                    if source not in ("bootstrap", "bootstrap_regime", "auto_repair"):
                        self._defer_entry_due_scheduler(slot_id, action, source)
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
                    state_order = sm.find_order(slot.state, action.local_id)
                    if state_order and abs(float(state_order.volume) - float(action.volume)) > 1e-8:
                        logger.error(
                            "[REBAL] VOLUME DRIFT slot=%s local_id=%s state_vol=%.10f action_vol=%.10f",
                            slot_id,
                            action.local_id,
                            state_order.volume,
                            action.volume,
                        )
                    self.ledger.commit_order(action.side, action.price, action.volume)
                    if action.role == "entry":
                        self.entry_adds_per_loop_used += 1
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
                self._record_exit_outcome(slot_id, action)

        self._normalize_slot_mode(slot_id)

    def _record_exit_outcome(self, slot_id: int, action: sm.BookCycleAction) -> None:
        slot = self.slots.get(slot_id)
        if slot is None:
            return

        cycle_record = next(
            (
                c
                for c in reversed(slot.state.completed_cycles)
                if c.trade_id == action.trade_id and int(c.cycle) == int(action.cycle)
            ),
            None,
        )
        if cycle_record is None:
            logger.debug(
                "exit_outcomes: cycle record missing for slot=%s trade=%s cycle=%s",
                slot_id,
                action.trade_id,
                action.cycle,
            )
            return

        regime_name, regime_confidence, regime_bias, _, _ = self._policy_hmm_signal()

        against_trend = False
        if regime_name == "BULLISH":
            against_trend = action.trade_id == "A"
        elif regime_name == "BEARISH":
            against_trend = action.trade_id == "B"

        entry_time = float(cycle_record.entry_time or 0.0)
        exit_time = float(cycle_record.exit_time or 0.0)
        if entry_time > 0 and exit_time > 0:
            total_age_sec = max(0.0, exit_time - entry_time)
        else:
            total_age_sec = 0.0

        row = {
            "time": float(exit_time if exit_time > 0 else _now()),
            "pair": str(self.pair),
            "trade": str(action.trade_id),
            "cycle": int(action.cycle),
            "resolution": "recovery" if bool(action.from_recovery) else "normal",
            "from_recovery": bool(action.from_recovery),
            "entry_time": entry_time if entry_time > 0 else None,
            "exit_time": exit_time if exit_time > 0 else None,
            "total_age_sec": float(total_age_sec),
            "entry_price": float(cycle_record.entry_price),
            "exit_price": float(cycle_record.exit_price),
            "volume": float(cycle_record.volume),
            "gross_profit_usd": float(action.gross_profit),
            "fees_usd": float(action.fees),
            "net_profit_usd": float(action.net_profit),
            "regime_at_entry": cycle_record.regime_at_entry,
            "regime_confidence": float(regime_confidence),
            "regime_bias_signal": float(regime_bias),
            "against_trend": bool(against_trend),
            "regime_tier": int(self._regime_tier),
        }
        supabase_store.save_exit_outcome(row)

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
        one_sided_source = "regime" if str(getattr(st, "mode_source", "none")) == "regime" else "balance"
        if not entries and not exits:
            # Prevent stale snapshot flags from causing false S0 single-sided halts.
            self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")
            return
        if exits:
            # Degraded S1 states are legal when only one exit side survives
            # (e.g., loop API budget skipped replacing the missing entry).
            if not entries and len(exits) == 1:
                exit_side = exits[0].side
                if exit_side == "sell":
                    self.slots[slot_id].state = replace(
                        st, long_only=True, short_only=False, mode_source=one_sided_source
                    )
                elif exit_side == "buy":
                    self.slots[slot_id].state = replace(
                        st, long_only=False, short_only=True, mode_source=one_sided_source
                    )
            elif len(exits) == 2:
                self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")
            elif len(exits) == 1 and len(entries) == 1 and entries[0].side == exits[0].side:
                # Normal S1 shape (exit + same-side entry) should not keep degraded flags.
                self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")
            return
        buy_entries = [o for o in entries if o.side == "buy"]
        sell_entries = [o for o in entries if o.side == "sell"]
        if len(buy_entries) == 1 and len(sell_entries) == 0:
            self.slots[slot_id].state = replace(st, long_only=True, short_only=False, mode_source=one_sided_source)
        elif len(sell_entries) == 1 and len(buy_entries) == 0:
            self.slots[slot_id].state = replace(st, long_only=False, short_only=True, mode_source=one_sided_source)
        elif len(sell_entries) == 1 and len(buy_entries) == 1:
            self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")

    # ------------------ Commands ------------------

    def add_slot(self) -> tuple[bool, str]:
        if self.mode == "HALTED":
            return False, "bot halted"
        sid = self.next_slot_id
        self.next_slot_id += 1
        alias = self._allocate_slot_alias()
        st = sm.PairState(
            market_price=self.last_price,
            now=_now(),
            profit_pct_runtime=self.profit_pct,
        )
        self.slots[sid] = SlotRuntime(slot_id=sid, state=st, alias=alias)
        self._ensure_slot_bootstrapped(sid)
        self._save_snapshot()
        return True, f"slot {sid} ({alias}) added"

    def add_layer(self, source: str | None = None) -> tuple[bool, str]:
        src = str(source or config.CAPITAL_LAYER_DEFAULT_SOURCE).strip().upper()
        if src not in {"AUTO", "DOGE", "USD"}:
            return False, f"invalid layer funding source: {src}"

        step_doge_eq = self._capital_layer_step_doge_eq()
        if step_doge_eq <= 0:
            return False, "layer add rejected: invalid layer step"

        price = self._layer_mark_price()
        if price <= 0:
            return False, "layer add rejected: market price unavailable"

        free_usd, free_doge = self._available_free_balances(prefer_fresh=True)
        required_usd = step_doge_eq * price
        available_doge_eq = free_doge + (free_usd / price)

        ok = False
        if src == "DOGE":
            ok = free_doge + 1e-12 >= step_doge_eq
        elif src == "USD":
            ok = free_usd + 1e-12 >= required_usd
        else:
            ok = available_doge_eq + 1e-12 >= step_doge_eq

        if not ok:
            if src == "DOGE":
                return False, f"layer add rejected: need {step_doge_eq:.0f} DOGE, available {free_doge:.4f} DOGE"
            if src == "USD":
                return False, f"layer add rejected: need ${required_usd:.4f}, available ${free_usd:.4f}"
            return (
                False,
                f"layer add rejected: need {step_doge_eq:.0f} DOGE-eq, available {available_doge_eq:.4f} DOGE-eq",
            )

        self.target_layers = max(0, int(self.target_layers)) + 1
        self.layer_last_add_event = {
            "timestamp": _now(),
            "source": src,
            "price_at_commit": float(price),
            "usd_equiv_at_commit": float(required_usd),
        }
        self._recompute_effective_layers(mark_price=price)
        self._save_snapshot()
        return (
            True,
            f"layer added: target={self.target_layers} (+{config.CAPITAL_LAYER_DOGE_PER_ORDER:.0f} DOGE/order), "
            f"commit step {step_doge_eq:.0f} DOGE-eq @ ${price:.4f}",
        )

    def remove_layer(self) -> tuple[bool, str]:
        if int(self.target_layers) <= 0:
            return False, "layer remove rejected: target already zero"
        self.target_layers = int(self.target_layers) - 1
        self._recompute_effective_layers()
        self._save_snapshot()
        return True, f"layer removed: target={self.target_layers} (+{self.target_layers:.0f} DOGE/order)"

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
        if bool(config.STICKY_MODE_ENABLED):
            return False, "soft_close disabled in sticky mode; use release_slot"
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
        if bool(config.STICKY_MODE_ENABLED):
            return False, "soft_close_next disabled in sticky mode; use release_slot"
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

        self._release_slot_alias(slot.alias)
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

    def _auto_drain_recovery_backlog(self) -> None:
        """Force-close a small number of recoveries to reduce persistent backlog.

        Priority is deterministic: furthest-from-market first, then oldest.
        """
        if not bool(config.AUTO_RECOVERY_DRAIN_ENABLED):
            return
        if self.last_price <= 0:
            return

        max_per_loop = max(1, min(int(config.AUTO_RECOVERY_DRAIN_MAX_PER_LOOP), 5))
        if max_per_loop <= 0:
            return

        total_recoveries = sum(len(slot.state.recovery_orders) for slot in self.slots.values())
        if total_recoveries <= 0:
            return

        slot_count = max(1, len(self.slots))
        target_total = max(0, int(config.MAX_RECOVERY_SLOTS)) * slot_count
        excess = max(0, total_recoveries - target_total)

        if self._kraken_open_orders_current is not None:
            open_current = int(self._kraken_open_orders_current)
        else:
            open_current = self._internal_open_order_count()
        pair_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
        utilization_pct = open_current / pair_limit * 100.0
        pressure_threshold = float(config.AUTO_RECOVERY_DRAIN_CAPACITY_PCT)
        pressure = utilization_pct >= pressure_threshold

        if excess <= 0 and not pressure:
            return

        drain_target = min(max_per_loop, excess if excess > 0 else total_recoveries)
        if drain_target <= 0:
            return

        candidates: list[tuple[float, float, int, int, sm.RecoveryOrder]] = []
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            for rec in st.recovery_orders:
                dist = abs(float(rec.price) - float(self.last_price)) / float(self.last_price)
                candidates.append((-dist, float(rec.orphaned_at), int(rec.recovery_id), sid, rec))
        if not candidates:
            return
        candidates.sort()

        drained = 0
        now_ts = _now()
        for _neg_dist, _orphaned_at, _rid, sid, rec in candidates:
            if drained >= drain_target:
                break
            slot = self.slots.get(sid)
            if not slot:
                continue
            live = next((r for r in slot.state.recovery_orders if r.recovery_id == rec.recovery_id), None)
            if not live:
                continue

            if live.txid:
                try:
                    ok = self._cancel_order(live.txid)
                    if not ok:
                        continue
                except Exception as e:
                    logger.warning("auto_drain: cancel recovery %s failed: %s", live.txid, e)
                    continue

            fill_price = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
            if fill_price <= 0:
                fill_price = float(live.price if live.price > 0 else live.entry_price)
            if fill_price <= 0:
                continue
            fill_fee = max(0.0, fill_price * float(live.volume) * (float(self.maker_fee_pct) / 100.0))
            ev = sm.RecoveryFillEvent(
                recovery_id=int(live.recovery_id),
                txid=str(live.txid or ""),
                side=live.side,
                price=fill_price,
                volume=float(live.volume),
                fee=fill_fee,
                timestamp=now_ts,
            )
            self._apply_event(
                sid,
                ev,
                "recovery_auto_drain",
                {
                    "recovery_id": int(live.recovery_id),
                    "fill_price": fill_price,
                    "fill_fee": fill_fee,
                    "reason": "auto_recovery_drain",
                },
            )
            drained += 1

        if drained > 0:
            self._auto_recovery_drain_total += drained
            self._auto_recovery_drain_last_at = now_ts
            logger.info(
                "auto_recovery_drain: drained %d recoveries (excess=%d pressure=%s util=%.1f%% target_total=%d lifetime=%d)",
                drained,
                excess,
                pressure,
                utilization_pct,
                target_total,
                self._auto_recovery_drain_total,
            )

    def cancel_stale_recoveries(self, min_distance_pct: float = 3.0, max_batch: int = 8) -> tuple[bool, str]:
        """Bulk soft-close recovery orders farther than min_distance_pct from market.

        Reprices them to within entry_pct of market so they fill quickly and
        book P&L through the normal recovery-fill path.  Processes up to
        max_batch per call to stay within Kraken rate limits (2 API calls each:
        cancel old + place new).  Call repeatedly until remaining == 0.
        """
        if bool(config.STICKY_MODE_ENABLED):
            return False, "cancel_stale_recoveries disabled in sticky mode; use release_slot"
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

    def _trend_strength_proxy_adx(self, period: int = 14) -> float:
        """
        Lightweight trend-strength proxy mapped to ADX-like 0-100 scale.

        Uses close-only directionality (net move / total path) over recent
        samples. This is intentionally conservative for release gating until
        full OHLC ADX is introduced.
        """
        p = max(2, int(period))
        closes = [float(px) for _, px in self.price_history[-(p + 1):] if float(px) > 0]
        if len(closes) < p + 1:
            return 0.0

        path = 0.0
        for i in range(1, len(closes)):
            path += abs(closes[i] - closes[i - 1])
        if path <= 1e-12:
            return 0.0
        net = abs(closes[-1] - closes[0])
        strength = (net / path) * 100.0
        return max(0.0, min(100.0, strength))

    def _slot_unrealized_profit(self, st: sm.PairState) -> float:
        market = float(st.market_price if st.market_price > 0 else self.last_price)
        if market <= 0:
            return 0.0

        total = 0.0
        for o in st.orders:
            if o.role != "exit":
                continue
            if o.entry_price <= 0 or o.volume <= 0:
                continue
            if o.side == "buy":
                total += (o.entry_price - market) * o.volume
            else:
                total += (market - o.entry_price) * o.volume
        for r in st.recovery_orders:
            if r.entry_price <= 0 or r.volume <= 0:
                continue
            if r.side == "buy":
                total += (r.entry_price - market) * r.volume
            else:
                total += (market - r.entry_price) * r.volume
        return total

    def _total_unrealized_profit_locked(self) -> float:
        return sum(self._slot_unrealized_profit(slot.state) for slot in self.slots.values())

    def _balance_recon_locked(self) -> dict | None:
        total_profit = sum(slot.state.total_profit for slot in self.slots.values())
        total_unrealized = self._total_unrealized_profit_locked()
        return self._compute_balance_recon(total_profit, total_unrealized)

    def _update_release_recon_gate_locked(self) -> tuple[bool, str]:
        if not bool(config.RELEASE_RECON_HARD_GATE_ENABLED):
            self._release_recon_blocked = False
            self._release_recon_blocked_reason = ""
            return True, "release recon hard-gate disabled"

        recon = self._balance_recon_locked()
        if not isinstance(recon, dict):
            # No baseline / no balance -> don't hard-block operator action.
            self._release_recon_blocked = False
            self._release_recon_blocked_reason = ""
            return True, "release recon unavailable"

        status = str(recon.get("status") or "")
        drift_pct = float(recon.get("drift_pct", 0.0) or 0.0)
        threshold = float(recon.get("threshold_pct", float(config.BALANCE_RECON_DRIFT_PCT)) or 0.0)
        if status == "DRIFT" and abs(drift_pct) > threshold + 1e-12:
            self._release_recon_blocked = True
            self._release_recon_blocked_reason = (
                f"release blocked by balance recon drift: {drift_pct:+.4f}% > {threshold:.4f}%"
            )
            return False, self._release_recon_blocked_reason

        self._release_recon_blocked = False
        self._release_recon_blocked_reason = ""
        return True, "release recon gate clear"

    def _release_gate_flags(
        self,
        slot: SlotRuntime,
        order: sm.OrderState,
        *,
        now_ts: float,
    ) -> dict[str, float | bool]:
        market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        if market <= 0:
            market = float(order.price if order.price > 0 else 0.0)
        age_sec = max(0.0, now_ts - float(order.entry_filled_at or order.placed_at or now_ts))
        distance_pct = abs(float(order.price) - market) / market * 100.0 if market > 0 else 0.0
        regime_strength = self._trend_strength_proxy_adx(period=14)

        age_ok = age_sec >= float(config.RELEASE_MIN_AGE_SEC)
        distance_ok = distance_pct >= float(config.RELEASE_MIN_DISTANCE_PCT)
        regime_ok = regime_strength >= float(config.RELEASE_ADX_THRESHOLD)
        return {
            "age_sec": age_sec,
            "distance_pct": distance_pct,
            "regime_strength": regime_strength,
            "age_ok": age_ok,
            "distance_ok": distance_ok,
            "regime_ok": regime_ok,
        }

    def _pick_release_exit(
        self,
        slot: SlotRuntime,
        *,
        local_id: int | None = None,
        trade_id: str | None = None,
    ) -> sm.OrderState | None:
        exits = [o for o in slot.state.orders if o.role == "exit"]
        if not exits:
            return None
        if local_id is not None:
            return next((o for o in exits if int(o.local_id) == int(local_id)), None)
        if trade_id in ("A", "B"):
            candidates = [o for o in exits if o.trade_id == trade_id]
            if candidates:
                candidates.sort(key=lambda o: float(o.entry_filled_at or o.placed_at or 0.0))
                return candidates[0]
        exits.sort(key=lambda o: float(o.entry_filled_at or o.placed_at or 0.0))
        return exits[0]

    def _release_exit_locked(
        self,
        slot_id: int,
        order: sm.OrderState,
        *,
        reason: str,
        now_ts: float,
    ) -> tuple[bool, str]:
        slot = self.slots.get(slot_id)
        if not slot:
            return False, f"unknown slot {slot_id}"

        live = next((o for o in slot.state.orders if o.local_id == order.local_id and o.role == "exit"), None)
        if not live:
            return False, f"slot {slot_id} exit {order.local_id} no longer active"

        if live.txid:
            try:
                ok = self._cancel_order(live.txid)
                if not ok:
                    return False, f"release cancel failed for {live.txid}"
            except Exception as e:
                return False, f"release cancel failed for {live.txid}: {e}"

        fill_price = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        if fill_price <= 0:
            fill_price = float(live.price if live.price > 0 else live.entry_price)
        if fill_price <= 0:
            return False, "release failed: no valid mark price"

        fill_fee = max(0.0, fill_price * float(live.volume) * (float(self.maker_fee_pct) / 100.0))
        ev = sm.FillEvent(
            order_local_id=int(live.local_id),
            txid=str(live.txid or ""),
            side=live.side,
            price=fill_price,
            volume=float(live.volume),
            fee=fill_fee,
            timestamp=now_ts,
        )
        self._apply_event(
            slot_id,
            ev,
            "sticky_release",
            {
                "order_local_id": int(live.local_id),
                "trade_id": live.trade_id,
                "fill_price": fill_price,
                "fill_fee": fill_fee,
                "reason": reason,
            },
        )
        self._sticky_release_total += 1
        self._sticky_release_last_at = now_ts
        gate_ok, gate_msg = self._update_release_recon_gate_locked()
        if not gate_ok:
            return True, f"released exit {live.local_id} on slot {slot_id}; {gate_msg}"
        return True, f"released exit {live.local_id} on slot {slot_id} @ ${fill_price:.6f}"

    def _slot_vintage_metrics_locked(self, now_ts: float | None = None) -> dict[str, float | int]:
        now_ts = float(now_ts if now_ts is not None else _now())
        buckets = {
            "fresh_0_1h": 0,
            "aging_1_6h": 0,
            "stale_6_24h": 0,
            "old_1_7d": 0,
            "ancient_7d_plus": 0,
        }
        oldest_age = 0.0
        stuck_capital_usd = 0.0
        release_eligible = 0
        period = 14

        for sid in sorted(self.slots.keys()):
            slot = self.slots[sid]
            for o in slot.state.orders:
                if o.role != "exit":
                    continue
                age_sec = max(0.0, now_ts - float(o.entry_filled_at or o.placed_at or now_ts))
                if age_sec < 3600:
                    buckets["fresh_0_1h"] += 1
                elif age_sec < 6 * 3600:
                    buckets["aging_1_6h"] += 1
                elif age_sec < 24 * 3600:
                    buckets["stale_6_24h"] += 1
                elif age_sec < 7 * 86400:
                    buckets["old_1_7d"] += 1
                else:
                    buckets["ancient_7d_plus"] += 1
                oldest_age = max(oldest_age, age_sec)
                mark = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
                if mark > 0:
                    stuck_capital_usd += abs(float(o.volume)) * mark
                flags = self._release_gate_flags(slot, o, now_ts=now_ts)
                if bool(flags.get("age_ok")) and bool(flags.get("distance_ok")) and bool(flags.get("regime_ok")):
                    release_eligible += 1

        bal = self._last_balance_snapshot
        mark = float(self.last_price)
        portfolio_usd = 0.0
        if bal and mark > 0:
            portfolio_usd = _usd_balance(bal) + _doge_balance(bal) * mark
        stuck_capital_pct = (stuck_capital_usd / portfolio_usd * 100.0) if portfolio_usd > 0 else 0.0

        sizes = [self._slot_order_size_usd(self.slots[sid]) for sid in sorted(self.slots.keys())]
        min_size = min(sizes) if sizes else 0.0
        max_size = max(sizes) if sizes else 0.0
        med_size = float(median(sizes)) if sizes else 0.0

        out: dict[str, float | int] = {
            **buckets,
            "oldest_exit_age_sec": float(oldest_age),
            "min_slot_size_usd": float(min_size),
            "median_slot_size_usd": float(med_size),
            "max_slot_size_usd": float(max_size),
            "stuck_capital_usd": float(stuck_capital_usd),
            "stuck_capital_pct": float(stuck_capital_pct),
            "vintage_release_eligible": int(release_eligible),
            "regime_strength_adx_proxy": float(self._trend_strength_proxy_adx(period=period)),
        }
        return out

    def release_slot(
        self,
        slot_id: int,
        local_id: int | None = None,
        trade_id: str | None = None,
    ) -> tuple[bool, str]:
        slot = self.slots.get(int(slot_id))
        if not slot:
            return False, f"unknown slot {slot_id}"

        gate_ok, gate_msg = self._update_release_recon_gate_locked()
        if not gate_ok:
            return False, gate_msg

        order = self._pick_release_exit(slot, local_id=local_id, trade_id=trade_id)
        if not order:
            return False, f"slot {slot_id}: no matching active exit"

        now_ts = _now()
        flags = self._release_gate_flags(slot, order, now_ts=now_ts)
        if not (bool(flags["age_ok"]) and bool(flags["distance_ok"]) and bool(flags["regime_ok"])):
            return (
                False,
                "release blocked by gates: "
                f"age={flags['age_sec']:.0f}s ({'ok' if flags['age_ok'] else 'no'}) "
                f"distance={flags['distance_pct']:.2f}% ({'ok' if flags['distance_ok'] else 'no'}) "
                f"regime={flags['regime_strength']:.2f} ({'ok' if flags['regime_ok'] else 'no'})",
            )
        return self._release_exit_locked(int(slot_id), order, reason="manual_release", now_ts=now_ts)

    def release_oldest_eligible(self, slot_id: int) -> tuple[bool, str]:
        slot = self.slots.get(int(slot_id))
        if not slot:
            return False, f"unknown slot {slot_id}"

        gate_ok, gate_msg = self._update_release_recon_gate_locked()
        if not gate_ok:
            return False, gate_msg

        now_ts = _now()
        exits = [o for o in slot.state.orders if o.role == "exit"]
        exits.sort(key=lambda o: float(o.entry_filled_at or o.placed_at or now_ts))
        for order in exits:
            flags = self._release_gate_flags(slot, order, now_ts=now_ts)
            if bool(flags["age_ok"]) and bool(flags["distance_ok"]) and bool(flags["regime_ok"]):
                return self._release_exit_locked(
                    int(slot_id),
                    order,
                    reason="manual_release_oldest_eligible",
                    now_ts=now_ts,
                )

        return False, f"slot {slot_id}: no release-eligible exits (age/distance/regime)"

    def _auto_release_sticky_slots(self) -> None:
        if not bool(config.STICKY_MODE_ENABLED):
            return
        if not bool(config.RELEASE_AUTO_ENABLED):
            return
        gate_ok, _gate_msg = self._update_release_recon_gate_locked()
        if not gate_ok:
            return

        vintage = self._slot_vintage_metrics_locked(_now())
        stuck_pct = float(vintage.get("stuck_capital_pct", 0.0) or 0.0)
        tier1_threshold = float(config.RELEASE_MAX_STUCK_PCT)
        tier2_threshold = float(config.RELEASE_PANIC_STUCK_PCT)
        if stuck_pct <= tier1_threshold:
            return

        now_ts = _now()
        tier2 = stuck_pct > tier2_threshold
        batch_max = max(1, min(int(config.AUTO_RECOVERY_DRAIN_MAX_PER_LOOP), 5))
        target_pct = float(config.RELEASE_RECOVERY_TARGET_PCT)
        panic_age = float(config.RELEASE_PANIC_MIN_AGE_SEC)

        candidates: list[tuple[float, int, sm.OrderState]] = []
        for sid in sorted(self.slots.keys()):
            slot = self.slots[sid]
            for o in slot.state.orders:
                if o.role != "exit":
                    continue
                flags = self._release_gate_flags(slot, o, now_ts=now_ts)
                age_sec = float(flags["age_sec"])
                if tier2:
                    if age_sec >= panic_age:
                        candidates.append((-age_sec, sid, o))
                else:
                    if bool(flags["age_ok"]) and bool(flags["distance_ok"]) and bool(flags["regime_ok"]):
                        candidates.append((-age_sec, sid, o))
        if not candidates:
            return
        candidates.sort()

        released = 0
        for _neg_age, sid, order in candidates:
            if released >= batch_max:
                break
            ok, _msg = self._release_exit_locked(
                sid,
                order,
                reason=("auto_release_tier2" if tier2 else "auto_release_tier1"),
                now_ts=now_ts,
            )
            if not ok:
                continue
            released += 1
            if self._release_recon_blocked:
                break
            if tier2:
                stuck_pct = float(self._slot_vintage_metrics_locked(now_ts).get("stuck_capital_pct", 0.0) or 0.0)
                if stuck_pct <= target_pct:
                    break

        if released > 0:
            logger.info(
                "auto_release: released %d exits (%s trigger, stuck_capital_pct=%.2f%%)",
                released,
                "tier2" if tier2 else "tier1",
                float(vintage.get("stuck_capital_pct", 0.0) or 0.0),
            )

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
            self._update_release_recon_gate_locked()
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
        self._update_release_recon_gate_locked()
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
            loop_now = _now()
            self._sync_ohlcv_candles(loop_now)
            self._update_daily_loss_lock(loop_now)
            self._update_rebalancer(loop_now)
            self._update_regime_tier(loop_now)
            self._maybe_schedule_ai_regime(loop_now)
            self._apply_tier2_suppression(loop_now)
            self._clear_expired_regime_cooldown(loop_now)
            # Prioritize older deferred entries each loop, while avoiding stale placements
            # that the upcoming price tick is likely to refresh anyway.
            self._drain_pending_entry_orders("entry_scheduler_pre_tick", skip_stale=True)

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

            # After all slot transitions/actions, use remaining entry quota.
            self._drain_pending_entry_orders("entry_scheduler_post_tick", skip_stale=False)

            self._poll_order_status()
            self._update_daily_loss_lock(_now())
            # Refresh pair open-order telemetry (Kraken source of truth) when budget allows.
            self._refresh_open_order_telemetry()
            # Force-drain a small number of recoveries when backlog/pressure is high.
            self._auto_drain_recovery_backlog()
            # Auto-soft-close farthest recoveries when nearing order capacity.
            self._auto_soft_close_if_capacity_pressure()
            # Keep release hard-gate status fresh and run sticky auto-release tiers.
            self._update_release_recon_gate_locked()
            self._auto_release_sticky_slots()

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
                ok, msg = self.resume()
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
                    "/backfill_ohlcv [target_candles] [max_pages] [interval_min]\n"
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
            elif head == "/backfill_ohlcv":
                target = None
                pages = None
                interval_min = None
                if len(parts) >= 2:
                    try:
                        target = int(parts[1])
                    except ValueError:
                        ok, msg = False, "usage: /backfill_ohlcv [target_candles] [max_pages] [interval_min]"
                if ok and len(parts) >= 3:
                    try:
                        pages = int(parts[2])
                    except ValueError:
                        ok, msg = False, "usage: /backfill_ohlcv [target_candles] [max_pages] [interval_min]"
                if ok and len(parts) >= 4:
                    try:
                        interval_min = int(parts[3])
                    except ValueError:
                        ok, msg = False, "usage: /backfill_ohlcv [target_candles] [max_pages] [interval_min]"
                if ok:
                    interval = (
                        max(1, int(interval_min))
                        if interval_min is not None
                        else max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
                    )
                    secondary_interval = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
                    state_key = "secondary" if interval == secondary_interval else "primary"
                    if state_key == "secondary":
                        self._hmm_backfill_stall_count_secondary = 0
                    else:
                        self._hmm_backfill_stall_count = 0
                    ok, msg = self.backfill_ohlcv_history(
                        target_candles=target,
                        max_pages=pages,
                        interval_min=interval,
                        state_key=state_key,
                    )
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

    def _compute_dynamic_idle_target(self, now: float) -> float:
        base_target = max(0.0, min(1.0, float(config.REBALANCE_TARGET_IDLE_PCT)))
        floor_target = max(0.0, min(1.0, float(config.TREND_IDLE_FLOOR)))
        ceil_target = max(0.0, min(1.0, float(config.TREND_IDLE_CEILING)))
        if floor_target > ceil_target:
            floor_target, ceil_target = ceil_target, floor_target
        sensitivity = max(0.0, float(config.TREND_IDLE_SENSITIVITY))
        dead_zone = max(0.0, float(config.TREND_DEAD_ZONE))
        hold_sec = max(0.0, float(config.TREND_HYSTERESIS_SEC))
        min_samples = max(1, int(config.TREND_MIN_SAMPLES))
        fast_halflife = max(1.0, float(config.TREND_FAST_HALFLIFE))
        slow_halflife = max(fast_halflife, float(config.TREND_SLOW_HALFLIFE))
        smooth_halflife = max(1.0, float(config.TREND_HYSTERESIS_SMOOTH_HALFLIFE))
        price = float(self.last_price)
        if price <= 0:
            self._trend_score = 0.0
            self._trend_dynamic_target = base_target
            self._trend_smoothed_target = base_target
            self._trend_target_locked_until = 0.0
            return base_target

        last_update_ts = float(self._trend_last_update_ts)
        interval_sec = max(1.0, float(config.REBALANCE_INTERVAL_SEC))
        dt = max(1.0, (now - last_update_ts) if last_update_ts > 0 else interval_sec)

        # Long update gaps can leave stale EMA state; restart from current price.
        if last_update_ts > 0 and (now - last_update_ts) > (slow_halflife * 2.0):
            self._trend_fast_ema = price
            self._trend_slow_ema = price

        has_persisted_ema = self._trend_fast_ema > 0 and self._trend_slow_ema > 0
        if not has_persisted_ema:
            if len(self.price_history) < min_samples:
                self._trend_fast_ema = price
                self._trend_slow_ema = price
                self._trend_score = 0.0
                self._trend_dynamic_target = base_target
                self._trend_smoothed_target = base_target
                self._trend_target_locked_until = 0.0
                self._trend_last_update_ts = now
                return base_target
            self._trend_fast_ema = price
            self._trend_slow_ema = price

        fast_alpha = 1.0 - exp(-dt / fast_halflife)
        slow_alpha = 1.0 - exp(-dt / slow_halflife)
        self._trend_fast_ema = fast_alpha * price + (1.0 - fast_alpha) * float(self._trend_fast_ema)
        self._trend_slow_ema = slow_alpha * price + (1.0 - slow_alpha) * float(self._trend_slow_ema)

        slow = float(self._trend_slow_ema)
        if slow <= 0:
            self._trend_score = 0.0
        else:
            self._trend_score = (float(self._trend_fast_ema) - slow) / slow

        signal_for_target = float(self._trend_score)
        hmm_enabled = bool(getattr(config, "HMM_ENABLED", False))
        _, _, policy_bias, hmm_ready, _ = self._policy_hmm_signal()
        if hmm_enabled and hmm_ready:
            blend_factor = max(
                0.0,
                min(1.0, float(self._hmm_state.get("blend_factor", getattr(config, "HMM_BLEND_WITH_TREND", 0.5)))),
            )
            hmm_bias = float(policy_bias)
            signal_for_target = blend_factor * float(self._trend_score) + (1.0 - blend_factor) * hmm_bias
            self._hmm_state["blend_factor"] = blend_factor
        self._hmm_state["blended_signal"] = float(signal_for_target)

        # Dead zone first: collapse to base target and skip hold/smoothing stages.
        if abs(float(signal_for_target)) < dead_zone:
            self._trend_dynamic_target = base_target
            self._trend_smoothed_target = base_target
            self._trend_target_locked_until = 0.0
            self._trend_last_update_ts = now
            return base_target

        raw_target = base_target - sensitivity * float(signal_for_target)
        clamped_target = max(floor_target, min(ceil_target, raw_target))

        # Hold second: freeze output and smoothing state.
        if now < float(self._trend_target_locked_until):
            self._trend_last_update_ts = now
            return max(0.0, min(1.0, float(self._trend_dynamic_target)))

        # Smooth third: only when hold is not active.
        prev_output = max(0.0, min(1.0, float(self._trend_dynamic_target)))
        prev_smoothed = float(self._trend_smoothed_target)
        if not isfinite(prev_smoothed):
            prev_smoothed = prev_output
        target_alpha = 1.0 - exp(-dt / smooth_halflife)
        smoothed_target = target_alpha * clamped_target + (1.0 - target_alpha) * prev_smoothed
        smoothed_target = max(floor_target, min(ceil_target, smoothed_target))
        self._trend_smoothed_target = smoothed_target
        self._trend_dynamic_target = smoothed_target

        if abs(smoothed_target - prev_output) > 0.02 + 1e-12:
            self._trend_target_locked_until = now + hold_sec
        else:
            self._trend_target_locked_until = 0.0

        self._trend_last_update_ts = now
        return max(0.0, min(1.0, smoothed_target))

    def _update_rebalancer(self, now: float) -> None:
        if not bool(config.REBALANCE_ENABLED):
            self._rebalancer_current_skew = 0.0
            return

        interval_sec = max(1.0, float(config.REBALANCE_INTERVAL_SEC))
        last_ts = float(self._rebalancer_last_update_ts)
        if last_ts > 0 and (now - last_ts) < interval_sec:
            return

        self._update_hmm(now)
        target = self._compute_dynamic_idle_target(now)

        capacity = self._compute_capacity_health(now)
        band = str(capacity.get("status_band") or "normal")
        self._rebalancer_last_capacity_band = band
        if band in ("caution", "stop"):
            self._rebalancer_current_skew = 0.0
            self._rebalancer_last_update_ts = now
            logger.info("[REBAL] paused: capacity band=%s", band)
            return

        scoreboard = self._compute_doge_bias_scoreboard()
        if not scoreboard:
            self._rebalancer_current_skew = 0.0
            self._rebalancer_last_update_ts = now
            return

        idle_ratio = max(0.0, min(1.0, float(scoreboard.get("idle_usd_pct", 0.0)) / 100.0))
        raw_error = idle_ratio - target

        dt = max(1.0, (now - last_ts) if last_ts > 0 else interval_sec)
        halflife = max(1.0, float(config.REBALANCE_EMA_HALFLIFE))
        alpha = 1.0 - exp(-dt / halflife)

        prev_error = float(self._rebalancer_smoothed_error)
        smoothed_error = alpha * raw_error + (1.0 - alpha) * prev_error
        raw_velocity = (smoothed_error - prev_error) / dt
        prev_velocity = float(self._rebalancer_smoothed_velocity)
        smoothed_velocity = alpha * raw_velocity + (1.0 - alpha) * prev_velocity

        max_skew = max(0.0, float(config.REBALANCE_MAX_SKEW))
        if now < float(self._rebalancer_damped_until):
            max_skew *= 0.5

        neutral_band = max(0.0, float(config.REBALANCE_NEUTRAL_BAND))
        if abs(smoothed_error) < neutral_band:
            raw_skew = 0.0
        else:
            raw_skew = float(config.REBALANCE_KP) * smoothed_error + float(config.REBALANCE_KD) * smoothed_velocity
            raw_skew = max(-max_skew, min(max_skew, raw_skew))

        current_skew = float(self._rebalancer_current_skew)
        max_step = max(0.0, float(config.REBALANCE_MAX_SKEW_STEP))
        delta = raw_skew - current_skew
        if abs(delta) > max_step:
            new_skew = current_skew + (max_step if delta > 0 else -max_step)
        else:
            new_skew = raw_skew
        new_skew = max(-max_skew, min(max_skew, new_skew))

        # 1h sign-flip tracking for oscillation damping.
        if abs(current_skew) > 1e-12 and abs(new_skew) > 1e-12 and current_skew * new_skew < 0:
            self._rebalancer_sign_flip_history.append(now)
        cutoff = now - 3600.0
        while self._rebalancer_sign_flip_history and self._rebalancer_sign_flip_history[0] < cutoff:
            self._rebalancer_sign_flip_history.popleft()
        sign_flips_1h = len(self._rebalancer_sign_flip_history)
        if sign_flips_1h >= 3 and now >= float(self._rebalancer_damped_until):
            self._rebalancer_damped_until = now + 3600.0
            logger.warning(
                "[REBAL] WARNING: oscillation detected (%d flips/hr), auto-damping active",
                sign_flips_1h,
            )

        self._rebalancer_idle_ratio = idle_ratio
        self._rebalancer_last_raw_error = raw_error
        self._rebalancer_smoothed_error = smoothed_error
        self._rebalancer_smoothed_velocity = smoothed_velocity
        self._rebalancer_current_skew = new_skew
        self._rebalancer_last_update_ts = now

        logger.info(
            "[REBAL] idle=%.3f target=%.3f err=%+.4f vel=%+.6f skew=%+.4f band=%s flips1h=%d",
            idle_ratio,
            target,
            smoothed_error,
            smoothed_velocity,
            new_skew,
            band,
            sign_flips_1h,
        )

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
            self._update_release_recon_gate_locked()
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
                    "slot_alias": self._slot_label(self.slots[sid]),
                    "slot_label": self._slot_label(self.slots[sid]),
                    "phase": phase,
                    "long_only": st.long_only,
                    "short_only": st.short_only,
                    "mode_source": str(getattr(st, "mode_source", "none") or "none"),
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
            daily_realized_loss_utc = self._compute_daily_realized_loss_utc(now)
            self._daily_realized_loss_utc = float(daily_realized_loss_utc)
            daily_loss_lock_active = bool(self._daily_loss_lock_active)
            daily_loss_lock_day = str(self._daily_loss_lock_utc_day or "")
            capacity = self._compute_capacity_health(now)
            pending_entries = len(self._pending_entry_orders())
            try:
                private_api = kraken_client.rate_limit_telemetry()
            except Exception:
                private_api = {}
            internal_open_orders_current = int(capacity.get("open_orders_internal") or 0)
            last_balance = dict(self._last_balance_snapshot) if self._last_balance_snapshot else {}
            observed_usd_balance = _usd_balance(last_balance) if last_balance else None
            observed_doge_balance = _doge_balance(last_balance) if last_balance else None
            balance_age_sec = (now - self._last_balance_ts) if last_balance else None
            kraken_open_orders_current = capacity.get("open_orders_kraken")
            open_orders_current = int(capacity.get("open_orders_current") or 0)
            open_orders_source = str(capacity.get("open_orders_source") or "internal_fallback")
            open_order_headroom = int(capacity.get("open_order_headroom") or 0)
            partial_fill_open_events_1d = int(capacity.get("partial_fill_open_events_1d") or 0)
            partial_fill_cancel_events_1d = int(capacity.get("partial_fill_cancel_events_1d") or 0)
            status_band = str(capacity.get("status_band") or "normal")

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

            cutoff_flips = now - 3600.0
            while self._rebalancer_sign_flip_history and self._rebalancer_sign_flip_history[0] < cutoff_flips:
                self._rebalancer_sign_flip_history.popleft()

            top_phase = slots[0]["phase"] if slots else "S0"
            pnl_ref_price = self.last_price if self.last_price > 0 else (slots[0]["market_price"] if slots else 0.0)
            total_profit_doge = total_profit / pnl_ref_price if pnl_ref_price > 0 else 0.0
            total_unrealized_doge = total_unrealized_profit / pnl_ref_price if pnl_ref_price > 0 else 0.0
            pnl_audit = self._pnl_audit_summary()
            layer_metrics = self._recompute_effective_layers(mark_price=pnl_ref_price)
            orders_at_funded_size = self._count_orders_at_funded_size()
            slot_vintage = self._slot_vintage_metrics_locked(now)
            hmm_data_pipeline = self._hmm_data_readiness(now)
            secondary_collect_enabled = bool(getattr(config, "HMM_SECONDARY_OHLCV_ENABLED", False)) or bool(
                getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False)
            )
            if secondary_collect_enabled:
                hmm_data_pipeline_secondary = self._hmm_data_readiness(
                    now,
                    interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
                    training_target=max(1, int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720))),
                    min_samples=max(1, int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200))),
                    sync_interval_sec=max(
                        30.0,
                        float(
                            getattr(
                                config,
                                "HMM_SECONDARY_SYNC_INTERVAL_SEC",
                                getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                            )
                        ),
                    ),
                    state_key="secondary",
                )
            else:
                hmm_data_pipeline_secondary = {
                    "enabled": False,
                    "state_key": "secondary",
                    "ready_for_min_train": False,
                    "ready_for_target_window": False,
                    "gaps": ["pipeline_disabled"],
                }
            hmm_regime = self._hmm_status_payload()
            hmm_consensus = dict(self._hmm_consensus or self._compute_hmm_consensus())
            hmm_consensus["source_mode"] = self._hmm_source_mode()
            hmm_consensus["multi_timeframe"] = bool(getattr(config, "HMM_MULTI_TIMEFRAME_ENABLED", False))
            regime_directional = self._regime_status_payload(now)
            ai_regime_advisor = self._ai_regime_status_payload(now)
            oldest_exit_age_sec = float(slot_vintage.get("oldest_exit_age_sec", 0.0) or 0.0)
            stuck_capital_pct = float(slot_vintage.get("stuck_capital_pct", 0.0) or 0.0)
            slot_vintage["vintage_warn"] = oldest_exit_age_sec >= 3.0 * 86400.0
            slot_vintage["vintage_critical"] = stuck_capital_pct > float(config.RELEASE_MAX_STUCK_PCT)
            slot_vintage["release_recon_gate_blocked"] = bool(self._release_recon_blocked)
            slot_vintage["release_recon_gate_reason"] = str(self._release_recon_blocked_reason or "")

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
                "daily_loss_limit": float(config.DAILY_LOSS_LIMIT),
                "daily_realized_loss_utc": float(daily_realized_loss_utc),
                "daily_loss_lock_active": daily_loss_lock_active,
                "daily_loss_lock_utc_day": daily_loss_lock_day,
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
                "reentry_base_cooldown_sec": float(config.REENTRY_BASE_COOLDOWN_SEC),
                "capacity_fill_health": {
                    "open_orders_current": open_orders_current,
                    "open_orders_source": open_orders_source,
                    "open_orders_internal": int(capacity.get("open_orders_internal") or internal_open_orders_current),
                    "open_orders_kraken": kraken_open_orders_current,
                    "open_orders_drift": capacity.get("open_orders_drift"),
                    "open_order_limit_configured": int(capacity.get("open_order_limit_configured") or 0),
                    "open_orders_safe_cap": int(capacity.get("open_orders_safe_cap") or 0),
                    "open_order_headroom": open_order_headroom,
                    "open_order_utilization_pct": float(capacity.get("open_order_utilization_pct") or 0.0),
                    "orders_per_slot_estimate": capacity.get("orders_per_slot_estimate"),
                    "estimated_slots_remaining": int(capacity.get("estimated_slots_remaining") or 0),
                    "partial_fill_open_events_1d": partial_fill_open_events_1d,
                    "partial_fill_cancel_events_1d": partial_fill_cancel_events_1d,
                    "median_fill_seconds_1d": capacity.get("median_fill_seconds_1d"),
                    "p95_fill_seconds_1d": capacity.get("p95_fill_seconds_1d"),
                    "status_band": status_band,
                    "blocked_risk_hint": blocked_risk_hint,
                    "auto_soft_close_total": self._auto_soft_close_total,
                    "auto_soft_close_last_at": self._auto_soft_close_last_at or None,
                    "auto_soft_close_threshold_pct": float(config.AUTO_SOFT_CLOSE_CAPACITY_PCT),
                    "auto_recovery_drain_total": self._auto_recovery_drain_total,
                    "auto_recovery_drain_last_at": self._auto_recovery_drain_last_at or None,
                    "auto_recovery_drain_threshold_pct": float(config.AUTO_RECOVERY_DRAIN_CAPACITY_PCT),
                    "private_api_metronome": {
                        "enabled": bool(private_api.get("enabled", False)),
                        "wave_calls": int(private_api.get("wave_calls", 0) or 0),
                        "wave_seconds": float(private_api.get("wave_seconds", 0.0) or 0.0),
                        "wave_calls_used": int(private_api.get("wave_calls_used", 0) or 0),
                        "wave_window_remaining_sec": float(private_api.get("wave_window_remaining_sec", 0.0) or 0.0),
                        "wait_events": int(private_api.get("wait_events", 0) or 0),
                        "wait_total_sec": float(private_api.get("wait_total_sec", 0.0) or 0.0),
                        "last_wait_sec": float(private_api.get("last_wait_sec", 0.0) or 0.0),
                        "calls_last_60s": int(private_api.get("calls_last_60s", 0) or 0),
                        "effective_calls_per_sec": float(private_api.get("effective_calls_per_sec", 0.0) or 0.0),
                        "budget_available": float(private_api.get("budget_available", 0.0) or 0.0),
                        "consecutive_rate_errors": int(private_api.get("consecutive_rate_errors", 0) or 0),
                    },
                    "entry_scheduler": {
                        "cap_per_loop": int(self.entry_adds_per_loop_cap),
                        "used_this_loop": int(self.entry_adds_per_loop_used),
                        "pending_entries": int(pending_entries),
                        "deferred_total": int(self._entry_adds_deferred_total),
                        "drained_total": int(self._entry_adds_drained_total),
                        "last_deferred_at": self._entry_adds_last_deferred_at or None,
                        "last_drained_at": self._entry_adds_last_drained_at or None,
                    },
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
                "sticky_mode": {
                    "enabled": bool(config.STICKY_MODE_ENABLED),
                    "target_slots": int(config.STICKY_TARGET_SLOTS),
                    "max_target_slots": int(config.STICKY_MAX_TARGET_SLOTS),
                    "compounding_mode": str(getattr(config, "STICKY_COMPOUNDING_MODE", "legacy_profit")),
                    "auto_release_enabled": bool(config.RELEASE_AUTO_ENABLED),
                },
                "slot_vintage": slot_vintage,
                "hmm_data_pipeline": hmm_data_pipeline,
                "hmm_data_pipeline_secondary": hmm_data_pipeline_secondary,
                "hmm_regime": hmm_regime,
                "hmm_consensus": hmm_consensus,
                "regime_history_30m": list(self._regime_history_30m),
                "kelly": (self._kelly.status_payload() if self._kelly is not None else {"enabled": False}),
                "regime_directional": regime_directional,
                "ai_regime_advisor": ai_regime_advisor,
                "release_health": {
                    "sticky_release_total": int(self._sticky_release_total),
                    "sticky_release_last_at": self._sticky_release_last_at or None,
                    "recon_hard_gate_enabled": bool(config.RELEASE_RECON_HARD_GATE_ENABLED),
                    "recon_hard_gate_blocked": bool(self._release_recon_blocked),
                    "recon_hard_gate_reason": str(self._release_recon_blocked_reason or ""),
                },
                "doge_bias_scoreboard": self._compute_doge_bias_scoreboard(),
                "rebalancer": {
                    "enabled": bool(config.REBALANCE_ENABLED),
                    "idle_ratio": float(self._rebalancer_idle_ratio),
                    "target": float(max(0.0, min(1.0, self._trend_dynamic_target))),
                    "base_target": float(max(0.0, min(1.0, float(config.REBALANCE_TARGET_IDLE_PCT)))),
                    "error": float(self._rebalancer_last_raw_error),
                    "smoothed_error": float(self._rebalancer_smoothed_error),
                    "velocity": float(self._rebalancer_smoothed_velocity),
                    "skew": float(self._rebalancer_current_skew),
                    "skew_direction": (
                        "buy_doge"
                        if self._rebalancer_current_skew > 1e-12
                        else "sell_doge"
                        if self._rebalancer_current_skew < -1e-12
                        else "neutral"
                    ),
                    "size_mult_a": (
                        min(
                            float(config.REBALANCE_MAX_SIZE_MULT),
                            1.0 + abs(float(self._rebalancer_current_skew)) * float(config.REBALANCE_SIZE_SENSITIVITY),
                        )
                        if self._rebalancer_current_skew < 0
                        else 1.0
                    ),
                    "size_mult_b": (
                        min(
                            float(config.REBALANCE_MAX_SIZE_MULT),
                            1.0 + abs(float(self._rebalancer_current_skew)) * float(config.REBALANCE_SIZE_SENSITIVITY),
                        )
                        if self._rebalancer_current_skew > 0
                        else 1.0
                    ),
                    "damped": bool(now < float(self._rebalancer_damped_until)),
                    "sign_flips_1h": len(self._rebalancer_sign_flip_history),
                    "capacity_band": self._rebalancer_last_capacity_band,
                },
                "trend": {
                    "score": float(self._trend_score),
                    "score_display": (
                        f"+{(float(self._trend_score) * 100.0):.2f}%"
                        if float(self._trend_score) > 0
                        else f"{(float(self._trend_score) * 100.0):.2f}%"
                    ),
                    "fast_ema": float(self._trend_fast_ema),
                    "slow_ema": float(self._trend_slow_ema),
                    "dynamic_idle_target": float(max(0.0, min(1.0, self._trend_dynamic_target))),
                    "hysteresis_active": bool(now < float(self._trend_target_locked_until)),
                    "hysteresis_expires_in_sec": int(
                        max(0.0, float(self._trend_target_locked_until) - now)
                    ),
                },
                "capital_layers": {
                    "target_layers": int(layer_metrics.get("target_layers", 0) or 0),
                    "effective_layers": int(layer_metrics.get("effective_layers", 0) or 0),
                    "doge_per_order_per_layer": float(layer_metrics.get("doge_per_order_per_layer", 0.0) or 0.0),
                    "layer_order_budget": int(layer_metrics.get("layer_order_budget", 0) or 0),
                    "layer_step_doge_eq": float(layer_metrics.get("layer_step_doge_eq", 0.0) or 0.0),
                    "add_layer_usd_equiv_now": layer_metrics.get("add_layer_usd_equiv_now"),
                    "funding_source_default": str(config.CAPITAL_LAYER_DEFAULT_SOURCE),
                    "active_sell_orders": int(layer_metrics.get("active_sell_orders", 0) or 0),
                    "active_buy_orders": int(layer_metrics.get("active_buy_orders", 0) or 0),
                    "orders_at_funded_size": int(orders_at_funded_size),
                    "open_orders_total": int(layer_metrics.get("open_orders_total", 0) or 0),
                    "gap_layers": int(layer_metrics.get("gap_layers", 0) or 0),
                    "gap_doge_now": float(layer_metrics.get("gap_doge_now", 0.0) or 0.0),
                    "gap_usd_now": float(layer_metrics.get("gap_usd_now", 0.0) or 0.0),
                    "last_add_layer_event": self.layer_last_add_event,
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
            parsed: dict[str, float | int | str] = {}
            sticky_mode_enabled = bool(config.STICKY_MODE_ENABLED)
            if sticky_mode_enabled and action in ("soft_close", "soft_close_next", "cancel_stale_recoveries"):
                self._send_json(
                    {"ok": False, "message": f"{action} disabled in sticky mode; use release_slot"},
                    400,
                )
                return

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
            elif action == "release_slot":
                try:
                    parsed["slot_id"] = int(body.get("slot_id", 0))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
                    return
                local_id_raw = body.get("local_id", body.get("exit_local_id"))
                if local_id_raw not in (None, ""):
                    try:
                        parsed["local_id"] = int(local_id_raw)
                    except (TypeError, ValueError):
                        self._send_json({"ok": False, "message": "invalid local_id"}, 400)
                        return
                trade_id_raw = body.get("trade_id", "")
                trade_id = str(trade_id_raw).strip().upper()
                if trade_id:
                    if trade_id not in {"A", "B"}:
                        self._send_json({"ok": False, "message": "invalid trade_id (expected A or B)"}, 400)
                        return
                    parsed["trade_id"] = trade_id
            elif action == "release_oldest_eligible":
                try:
                    parsed["slot_id"] = int(body.get("slot_id", 0))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
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
            elif action == "add_layer":
                source = str(body.get("source", config.CAPITAL_LAYER_DEFAULT_SOURCE)).strip().upper()
                if source not in {"AUTO", "DOGE", "USD"}:
                    self._send_json({"ok": False, "message": "invalid layer source"}, 400)
                    return
                parsed["source"] = source
            elif action == "remove_layer":
                pass
            elif action == "ai_regime_override":
                ttl_raw = body.get("ttl_sec", None)
                if ttl_raw in (None, ""):
                    parsed["ttl_sec"] = int(getattr(config, "AI_OVERRIDE_TTL_SEC", 1800))
                else:
                    try:
                        parsed["ttl_sec"] = int(ttl_raw)
                    except (TypeError, ValueError):
                        self._send_json({"ok": False, "message": "invalid ttl_sec"}, 400)
                        return
            elif action in ("ai_regime_revert", "ai_regime_dismiss"):
                pass
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
                    ok, msg = _RUNTIME.resume()
                elif action == "add_slot":
                    ok, msg = _RUNTIME.add_slot()
                elif action == "add_layer":
                    ok, msg = _RUNTIME.add_layer(str(parsed.get("source", config.CAPITAL_LAYER_DEFAULT_SOURCE)))
                elif action == "remove_layer":
                    ok, msg = _RUNTIME.remove_layer()
                elif action == "set_entry_pct":
                    ok, msg = _RUNTIME.set_entry_pct(float(parsed["value"]))
                elif action == "set_profit_pct":
                    ok, msg = _RUNTIME.set_profit_pct(float(parsed["value"]))
                elif action == "soft_close":
                    ok, msg = _RUNTIME.soft_close(int(parsed["slot_id"]), int(parsed["recovery_id"]))
                elif action == "release_slot":
                    local_id = int(parsed["local_id"]) if "local_id" in parsed else None
                    trade_id = str(parsed["trade_id"]) if "trade_id" in parsed else None
                    ok, msg = _RUNTIME.release_slot(int(parsed["slot_id"]), local_id=local_id, trade_id=trade_id)
                elif action == "release_oldest_eligible":
                    ok, msg = _RUNTIME.release_oldest_eligible(int(parsed["slot_id"]))
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
                elif action == "ai_regime_override":
                    ok, msg = _RUNTIME.apply_ai_regime_override(int(parsed.get("ttl_sec", 0)))
                elif action == "ai_regime_revert":
                    ok, msg = _RUNTIME.revert_ai_regime_override()
                elif action == "ai_regime_dismiss":
                    ok, msg = _RUNTIME.dismiss_ai_regime_opinion()
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
