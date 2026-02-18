# Ranger Slots - Implementation Plan

Last updated: 2026-02-18
Parent spec: `docs/RANGER_SLOTS_SPEC.md` v0.1
Status: **Implementation in progress (Phases 1, 3, 4, and 5 complete)**

## Goal

Implement standalone ranger micro-cyclers that:
1. Sell DOGE above market and buy back lower during `RANGING` consensus.
2. Run without position-ledger, subsidy, or churner dependencies.
3. Gracefully accept orphaned exits as intended inventory conversion.

## Scope

In scope:
1. `config.py` additions for `RANGER_*` knobs.
2. `bot.py` ranger runtime state + lifecycle engine + startup cleanup + `/api/status` payload.
3. `dashboard.py` summary panel and per-ranger stage rendering.
4. Targeted tests for gating, state transitions, orphan accounting, and status/dashboard surfacing.

Out of scope:
1. Reducer (`state_machine.py`) lifecycle changes.
2. Position-ledger/Supabase persistence for ranger state.
3. New REST endpoints (reuse `/api/status` only).
4. Churner redesign or removal in this change.

## Execution Status (Current)

1. Phase 1 complete.
2. Phase 2 folded into Phase 3 by first-run cleanup inside `_run_ranger_engine()` (completed).
3. Phase 3 complete.
4. Phase 4 complete.
5. Phase 5 complete.
6. Phase 6 partial:
   - Added ranger status/dashboard regression checks in `tests/test_hardening_regressions.py`.
   - Dedicated `tests/test_ranger_slots.py` is still pending.

## Baseline Review (Code-Truth)

1. Churner runtime already has an isolated state machine in `bot.py` (`ChurnerRuntimeState`, `_run_churner_engine`, `_churner_*` helpers), which is the best architectural template for rangers.
2. Fill/cancel handling is centralized in `_poll_order_status()` (`bot.py`) and already supports custom order kinds (`churner_entry`, `churner_exit`) via txid tagging.
3. Startup cleanup exists for recovery orders only (`_cleanup_recovery_orders_on_startup()` in `bot.py`); no cleanup currently targets standalone subsystem orders by `userref`.
4. Main loop hook point is already available (`self._run_churner_engine(loop_now)` in `bot.py`), so ranger execution can be inserted adjacent to existing helper engines.
5. `/api/status` is built by `status_payload()` in `bot.py` and already drives dashboard summary rendering in `dashboard.py`.

## Locked Decisions

1. Keep ranger runtime fully in-memory and ephemeral; no snapshot persistence fields are added.
2. Implement ranger pool as `ranger_id: 0..RANGER_MAX_SLOTS-1` (global pool, not tied to slot IDs).
3. Use a dedicated ranger `userref` namespace that does not collide with existing ranges:
   - Existing: `900_000+` (recovery), `960_000+` (churner), `970_000+` (self-heal close/recover).
   - Ranger plan: `980_000 + ranger_id`.
4. Follow spec intent for timeout behavior:
   - Entry timeout: immediate `idle` (no cooldown).
   - Exit timeout orphan: account orphan, then `cooldown`.
   - Regime-shift orphan: account orphan, then `idle` (no cooldown).
5. Ranger observability is exposed as a top-level `rangers` block in `/api/status`, then rendered in the main dashboard summary area.

## Implementation Phases

## Phase 1 - Config and Runtime Scaffolding

Files:
1. `config.py`
2. `bot.py`
3. `.env.example`

Changes:
1. Add `RANGER_*` config keys with guardrails:
   - `RANGER_ENABLED`
   - `RANGER_MAX_SLOTS`
   - `RANGER_ORDER_SIZE_USD`
   - `RANGER_ENTRY_PCT`
   - `RANGER_PROFIT_PCT`
   - `RANGER_ENTRY_TIMEOUT_SEC`
   - `RANGER_EXIT_TIMEOUT_SEC`
   - `RANGER_COOLDOWN_SEC`
   - `RANGER_MIN_HEADROOM`
   - `RANGER_DOGE_RESERVE`
2. Add `RangerRuntimeState` dataclass in `bot.py`:
   - `ranger_id`, `stage`, `entry_txid`, `exit_txid`, `entry_price`, `entry_volume`, `exit_price`, `entry_filled_at`, `stage_entered_at`, `last_error`.
3. Add runtime members in `BotRuntime.__init__`:
   - `self._rangers: dict[int, RangerRuntimeState]`
   - `self._ranger_day_key`
   - `self._ranger_cycles_today`, `self._ranger_profit_today`
   - `self._ranger_orphans_today`, `self._ranger_orphan_exposure_usd`
4. Add helper methods:
   - `_ranger_enabled()`
   - `_ensure_ranger_state(ranger_id)`
   - `_reconcile_ranger_state()`
   - `_ranger_userref(ranger_id)`

Acceptance checks:
1. Bot initializes with ranger fields when enabled/disabled.
2. Ranger runtime state remains absent from snapshot save/load paths.
3. Config defaults are safe with no behavior change until `RANGER_ENABLED=true`.

## Phase 2 - Startup Reconciliation and Cleanup

Files:
1. `bot.py`

Changes:
1. Add `_cleanup_ranger_orders_on_startup()`:
   - Query open orders once.
   - Filter to runtime pair.
   - Cancel orders in ranger `userref` namespace.
   - Log cancelled/failed counts.
2. Call cleanup during `initialize()` before normal open-order reconciliation.
3. Keep cleanup unconditional (runs even when `RANGER_ENABLED=false`) to prevent stale-order carryover.

Acceptance checks:
1. Bot startup removes stale ranger orders left by prior process.
2. Non-ranger orders are unaffected.
3. Startup continues successfully on partial cancel failures (warn + proceed).

## Phase 3 - Ranger Lifecycle Engine

Files:
1. `bot.py`

Changes:
1. Add pricing/sizing helpers:
   - Entry price = `market * (1 + RANGER_ENTRY_PCT/100)`.
   - Volume via `sm.compute_order_volume(entry_price, cfg, RANGER_ORDER_SIZE_USD)`.
   - Exit margin = `max(RANGER_PROFIT_PCT, ROUND_TRIP_FEE_PCT + 0.20)`.
   - Exit price = `entry_fill_price * (1 - margin/100)`.
2. Add gate checks per ranger:
   - Consensus regime from `_policy_hmm_signal()` must be `RANGING`.
   - Open-order headroom from `_compute_capacity_health()` must be `>= RANGER_MIN_HEADROOM`.
   - DOGE free balance after reservation must respect `RANGER_DOGE_RESERVE`.
3. Add lifecycle handlers:
   - `_run_ranger_engine(now_ts)`
   - `_ranger_timeout_tick(...)`
   - `_ranger_mark_orphan(...)`
   - `_ranger_reset_state(...)`
4. Integrate loop reservation + commit discipline:
   - Reserve before placement with `_try_reserve_loop_funds`.
   - Release reservation on failed placement.
   - Commit successful placements with `self.ledger.commit_order(...)`.
5. Add main-loop hook after churner call:
   - `self._run_ranger_engine(loop_now)`.
6. Daily counter rollover on UTC day change.

Acceptance checks:
1. Rangers place sell entries only in `RANGING`.
2. Entry timeout cancels and returns to `idle` without cooldown.
3. Exit timeout/regime-shift properly records orphan metrics.
4. Completed cycles increment `cycles_today` and `profit_today`.
5. Disabled or non-ranging gate cancels ranger orders and idles all rangers.

## Phase 4 - Fill/Cancel Wiring in `_poll_order_status()`

Files:
1. `bot.py`

Changes:
1. Extend txid map in `_poll_order_status()` with:
   - `("ranger_entry", ranger_id)`
   - `("ranger_exit", ranger_id)`
2. Add handlers:
   - `_ranger_on_entry_fill(...)`:
     - Store fill data.
     - Place buy exit.
     - Transition `entry_open -> exit_open`.
   - `_ranger_on_exit_fill(...)`:
     - Compute net profit after both fees.
     - Transition to `cooldown`.
   - `_ranger_on_order_canceled(...)`:
     - Keep state machine coherent for manual/remote cancels.
3. Keep using existing batched order query path (`_query_orders_batched`) to avoid extra API calls.

Acceptance checks:
1. Entry fill immediately creates exit order.
2. Exit fill completes one ranger cycle and updates counters.
3. Canceled/expired statuses do not leave stranded ranger txids/state.

## Phase 5 - Status Payload and Dashboard

Files:
1. `bot.py`
2. `dashboard.py`

Changes:
1. Add top-level `rangers` block to `status_payload()` with:
   - `enabled`, `active`, `regime_ok`
   - `cycles_today`, `profit_today`
   - `orphans_today`, `orphan_exposure_usd`
   - `doge_reserve`
   - `slots[]` rows (`ranger_id`, `stage`, `entry_price`, `exit_price`, `entry_volume`, `age_sec`, `last_error`)
2. Dashboard summary card:
   - Status line: active/paused with regime reason.
   - Today line: cycles, profit, orphans.
   - Per-ranger compact stage/age list.
3. Add `.ranger-badge` styles:
   - green active, gray idle, amber near exit-timeout.
4. Keep UI passive:
   - no spawn/kill buttons,
   - no new endpoints.

Acceptance checks:
1. `/api/status` always includes `rangers` block (safe defaults when disabled).
2. Dashboard renders ranger card without errors when fields are missing/zeroed.
3. Ranger stage and age update live as orders progress.

## Phase 6 - Tests and Regression Coverage

Files:
1. `tests/test_ranger_slots.py` (new)
2. `tests/test_hardening_regressions.py`

Add tests:
1. `test_ranger_skips_when_regime_not_ranging`
2. `test_ranger_respects_doge_reserve_gate`
3. `test_ranger_places_sell_entry_when_gates_pass`
4. `test_ranger_entry_timeout_cancels_to_idle`
5. `test_ranger_entry_fill_places_exit_and_transitions`
6. `test_ranger_exit_fill_updates_profit_and_counters`
7. `test_ranger_exit_timeout_records_orphan_and_cooldown`
8. `test_ranger_regime_shift_cancels_and_idles`
9. `test_startup_cleanup_cancels_stale_ranger_userref_orders`
10. `test_status_payload_exposes_rangers_block`
11. `test_dashboard_contains_ranger_panel_markup`

Regression commands:
1. `python -m pytest tests/test_ranger_slots.py -v`
2. `python -m pytest tests/test_hardening_regressions.py -k ranger -v`
3. `python -m pytest tests/test_self_healing_slots.py -k churner -v`

## Verification Checklist

1. Dry-run with `RANGER_ENABLED=true`, `RANGER_MAX_SLOTS=1`, `RANGER_ORDER_SIZE_USD=2.0`.
2. Confirm first `RANGING` window produces at least one ranger entry.
3. Confirm completed cycle produces positive net profit after fees.
4. Force exit timeout and confirm orphan counters increment.
5. Flip regime away from `RANGING` and confirm open ranger orders are cancelled.
6. Restart bot with synthetic stale ranger order and confirm startup cleanup cancels it.

## Rollout Plan

1. Stage 0 (dark launch):
   - Deploy code with `RANGER_ENABLED=false`.
   - Validate startup cleanup and status compatibility.
2. Stage 1 (canary):
   - Enable `RANGER_ENABLED=true`, `RANGER_MAX_SLOTS=1`.
   - Observe API budget, orphan rate, and order headroom for 24h.
3. Stage 2 (full):
   - Increase to `RANGER_MAX_SLOTS=3` if canary stable.
   - Monitor success criteria from spec:
     - `cycles_today > 0` in ranging windows.
     - orphan rate under target in stable ranging.

## Risks

1. `userref` namespace collision:
   - mitigated by dedicated `980_000+` ranger range.
2. Private API budget pressure:
   - mitigated by reuse of existing batched order polling and headroom gating.
3. Regime oscillation churn:
   - mitigated by automatic order cancel/idle behavior and small order size.
4. DOGE depletion via orphan drift:
   - mitigated by reserve floor and explicit orphan metrics in status/dashboard.

## Rollback Plan

1. Set `RANGER_ENABLED=false` and restart.
2. Run startup cleanup path (or manual drift reconciliation) to cancel remaining ranger orders.
3. Revert ranger-specific code sections in:
   - `config.py`
   - `bot.py`
   - `dashboard.py`
   - `tests/test_ranger_slots.py`
4. Re-run targeted regressions to confirm baseline behavior.
