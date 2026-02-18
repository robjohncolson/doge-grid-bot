# STATE_MACHINE.md Update - Implementation Plan

Last updated: 2026-02-17
Parent spec: `docs/STATE_MACHINE_UPDATE_SPEC.md` v0.1.0
Status: **Ready for implementation (documentation-only)**

## Goal

Update `STATE_MACHINE.md` from rev 3 to rev 4 so it reflects the current runtime in `bot.py`/`config.py` and removes stale Kelly-era and pre-Bayesian assumptions.

## Scope

In scope:
1. Edit `STATE_MACHINE.md` only.
2. Replace stale sections identified in `docs/STATE_MACHINE_UPDATE_SPEC.md`.
3. Add new sections `§27` through `§34` with the existing document style.
4. Reconcile section content against current code paths in `bot.py`, `config.py`, `dashboard.py`, `ai_advisor.py`, `throughput_sizer.py`, `position_ledger.py`, `bayesian_engine.py`, `bocpd.py`, and `survival_model.py`.
5. Run the post-edit verification checklist and fix documentation drift found during verification.

Out of scope:
1. Any runtime code changes.
2. Spec edits in `docs/*_SPEC.md`.
3. `pair_model.py` behavior/documentation changes (explicitly excluded by parent spec).

## Code-Truth Snapshot (From Review)

1. Main loop ordering lives in `bot.py` `run_loop_once()` and currently calls:
   `_sync_ohlcv_candles`, `_update_daily_loss_lock`, `_update_rebalancer`, `_update_micro_features`, `_build_belief_state`, `_maybe_retrain_survival_model`, `_update_regime_tier`, `_update_manifold_score`, `_maybe_schedule_ai_regime`, `_update_accumulation`, `_apply_tier2_suppression`, `_clear_expired_regime_cooldown`, pre/post entry drains, conditional flow polling (`_should_poll_flows`/`_poll_external_flows`), slot ticks, `_poll_order_status`, `_update_trade_beliefs`, `_run_self_healing_reprice`, `_run_churner_engine`, `_auto_drain_recovery_backlog`, `_auto_soft_close_if_capacity_pressure`, `_update_release_recon_gate_locked`, `_auto_release_sticky_slots`, `_update_doge_eq_snapshot`, optional equity flush, and `_save_snapshot`.
2. `/api/status` top-level blocks include both existing and newer surfaces, including:
   `balance_health`, `balance_recon`, `external_flows`, `equity_history`, `sticky_mode`, `slot_vintage`, `self_healing`, `hmm_data_pipeline_tertiary`, `belief_state`, `bocpd`, `survival_model`, `trade_beliefs`, `action_knobs`, `manifold_score`, `ops_panel`, `regime_history_30m`, `throughput_sizer`, `dust_sweep`, `b_side_sizing`, `ai_regime_advisor`, and `accumulation`.
3. Dashboard `/api/action` handlers include both legacy and new controls, including:
   `release_slot`, `release_oldest_eligible`, `soft_close_next`, `ai_regime_override`, `ai_regime_revert`, `ai_regime_dismiss`, `accum_stop`, and self-healing actions (`self_heal_reprice_breakeven`, `self_heal_close_market`, `self_heal_keep_holding`).
4. Telegram command handling in `bot.py` currently does not expose AI/self-healing/accumulation commands; it exposes:
   `/pause`, `/resume`, `/add_slot`, `/status`, `/help`, `/soft_close`, `/cancel_stale`, `/remove_slot`, `/remove_slots`, `/reconcile_drift`, `/audit_pnl`, `/backfill_ohlcv`, `/set_entry_pct`, `/set_profit_pct`.
5. HMM defaults in `config.py` are already `HMM_TRAINING_CANDLES=4000`, `HMM_SECONDARY_TRAINING_CANDLES=1440`, and tertiary defaults are present (`HMM_TERTIARY_*`).
6. Throughput config names in code are `TP_*` but differ from some parent-spec shorthand names; docs must use the real names from `config.py`.
7. Recovery default contract is resolved for documentation:
   `RECOVERY_ORDERS_ENABLED` defaults to `_env("RECOVERY_ORDERS_ENABLED", RECOVERY_ENABLED, bool)` with `RECOVERY_ENABLED=True`, so effective default is True. Documentation should state this and explicitly note: set `RECOVERY_ORDERS_ENABLED=False` for strategic-capital rollout mode.
8. Reducer ownership remains unchanged:
   `state_machine.py` still owns `transition`/`check_invariants`, and `bot.py` continues to call both through `_apply_event`; `pair_model.py` remains out of scope.

## Implementation Phases

## Phase 0 - Working Matrix and Audit Lock

File:
1. `STATE_MACHINE.md`

Tasks:
1. Create a section-by-section matrix mapping old sections to target updates (`§1`, `§2`, `§4`, `§9`, `§11`, `§16`, `§17`, `§22`, `§23`, `§24`, `§26`, plus new `§27-§34`).
2. Capture source-of-truth anchors from code for:
   `run_loop_once`, `status_payload`, `do_POST` action handling, `poll_telegram`, and reducer calls (`sm.transition`/`sm.check_invariants`).
3. Capture current config key names for all new/updated sections to avoid spec-name drift.
4. Freeze section numbering plan before editing to avoid renumber churn.

Deliverable:
1. A completed working checklist used to drive deterministic edits in later phases.

## Phase 1 - Metadata and Stale Section Replacement

File:
1. `STATE_MACHINE.md`

Tasks:
1. Update header metadata:
   - `Last updated: 2026-02-17 (rev 4)`
   - expanded primary code references list.
2. Rewrite `§9` from Kelly-first to throughput-first sizing.
3. Replace stale B-side sizing text with account-aware behavior and dust-sweep interaction.
4. Update `§11` recovery/orphan lifecycle with `RECOVERY_ORDERS_ENABLED` behavior and startup stale-recovery handling.
   Wording requirement: default inherits from `RECOVERY_ENABLED` (effective True), with explicit note to set `RECOVERY_ORDERS_ENABLED=False` for strategic-capital deployment mode.
5. Update `§16`/`§17` with 4000/1440 training defaults, depth tiers, confidence modifier flow, and tertiary context.
6. Expand `§22` persistence model with throughput, AI advisor, accumulation, tertiary HMM, Bayesian, self-healing, and position-ledger snapshot fields.

Deliverable:
1. Existing stale sections are replaced without changing unaffected sections.

## Phase 2 - Add New Sections `§27-§34`

File:
1. `STATE_MACHINE.md`

Tasks:
1. Add `§27 AI Regime Advisor` with architecture/lifecycle/config/degradation.
2. Add `§28 1h HMM (Tertiary Timeframe)` with strategic-only usage and transition confirmation.
3. Add `§29 DCA Accumulation Engine` with state machine, entry/abort rules, and config.
4. Add `§30 Durable Profit Settlement` with dual realized trackers and quote-first implications.
5. Add `§31 Balance Intelligence` with flow detection, equity snapshots, and baseline adjustment semantics.
6. Add `§32 Self-Healing Slots` with subsidy mechanics and position-ledger role.
7. Add `§33 Bayesian Intelligence Stack` with BOCPD/survival/belief integration and phased rollout semantics.
8. Add `§34 Manifold Score/Ops Panel/Churner` documenting implemented vs spec-only boundaries.

Section format requirements:
1. Match current `STATE_MACHINE.md` tone and density.
2. Include config tables for each new section.
3. Include degradation guarantees for any optional subsystem.
4. Reference detailed spec docs for deep implementation detail.

Deliverable:
1. Complete section extension from `§26` to `§34` with coherent numbering.

## Phase 3 - Cross-Cutting Contract Synchronization

File:
1. `STATE_MACHINE.md`

Tasks:
1. Rework `§4 Main Loop` with exact function names/order from `bot.py` rather than parent-spec placeholder names.
   Preserve existing post-tick guardrails and release-flow steps (`_auto_drain_recovery_backlog`, `_auto_soft_close_if_capacity_pressure`, `_update_release_recon_gate_locked`, `_auto_release_sticky_slots`) and mid-loop flow polling.
2. Rework `§23` to document:
   - current `/api/status` block names, including `dust_sweep`, `b_side_sizing`, `balance_health`, `balance_recon`, `sticky_mode`, `slot_vintage`, `ops_panel`, and `regime_history_30m`,
   - current `/api/action` actions, including `release_slot`, `release_oldest_eligible`, `soft_close_next`, AI controls, accumulation controls, and self-healing controls.
3. Rework `§24` to match actual Telegram command support, and explicitly note non-supported new control actions if they are dashboard-only.
4. Keep `§2` and `§6` aligned with reducer architecture (runtime orchestration in `bot.py`, pure transition/invariant engine in `state_machine.py`; `pair_model.py` out of scope).
5. Update `§26 Developer Notes` so "update together" module list includes all newly introduced runtime modules.

Deliverable:
1. Cross-cutting sections align with real API/ops surfaces and avoid aspirational text.

## Phase 4 - Verification, Drift Checks, and Final Pass

Files:
1. `STATE_MACHINE.md`
2. `bot.py` (read-only for verification)
3. `config.py` (read-only for verification)
4. `telegram_menu.py` (read-only for verification)

Checklist execution:
1. Confirm no stale Kelly-first narrative remains (except explicit legacy note if intentionally kept).
2. Confirm every `*_ENABLED` flag in `config.py` is documented in at least one relevant section or explicitly marked out of scope.
3. Confirm every `/api/status` top-level block emitted by `status_payload()` is represented in `§23`.
4. Confirm every `/api/action` branch in `do_POST()` is represented in `§23`.
5. Confirm every Telegram command in `poll_telegram()` is represented in `§24`.
6. Confirm `§4` still includes both new and legacy loop steps (no regressions in post-tick guardrails, release logic, or flow polling).
7. Confirm `§2`/`§6` still accurately describe reducer ownership (`state_machine.py`) and runtime orchestration (`bot.py`).
8. Confirm all root runtime modules are mentioned somewhere in the document (or in an explicit "not part of runtime contract" note).
9. Confirm `pair_model.py` remains out of scope.

Deliverable:
1. Finalized `STATE_MACHINE.md` rev 4 ready for review/commit.

## Verification Commands (Recommended)

1. Root module coverage:
   `find . -maxdepth 1 -name '*.py' -printf '%f\n' | sort`
2. Enabled-flag inventory:
   `rg -n "_ENABLED" config.py`
3. Status payload contract anchor:
   `rg -n "def status_payload\\(|\"[a-z_]+\": " bot.py`
4. Dashboard action contract anchor:
   `rg -n "action ==|action in \\(" bot.py`
5. Telegram command contract anchor:
   `rg -n "head == \"/" bot.py`
6. Reducer contract anchors:
   `rg -n "def transition|def check_invariants" state_machine.py`
   `rg -n "sm\\.transition|sm\\.check_invariants|def _apply_event" bot.py`
7. Kelly drift scan:
   `rg -n "Kelly|kelly|KELLY_" STATE_MACHINE.md`

## Risks and Locked Decisions

1. Recovery default wording drift:
   parent spec text can be read as a code default change; documentation is locked to code-truth wording: default inherits from `RECOVERY_ENABLED` (effective True), and rollout guidance is to set `RECOVERY_ORDERS_ENABLED=False` explicitly.
2. Throughput config naming mismatch:
   parent spec uses simplified names (`TP_LOOKBACK`, `TP_CAPITAL_UTILIZATION_*`), while code uses `TP_LOOKBACK_CYCLES`, `TP_UTIL_*`, and `TP_AGE_PRESSURE_*`; document must prefer code names.
3. Loop-step naming mismatch:
   parent spec references helper names that are not exact current method names; `§4` must be based on `run_loop_once()` as implemented.
4. Telegram parity expectation:
   spec requests new commands "if they exist"; currently those controls are API/dashboard-only, so docs should state that directly.
5. Reducer-module drift:
   keep `state_machine.py` documented as the active pure reducer/invariant module, and avoid conflating it with `pair_model.py` (separate executable model effort).

## Acceptance Criteria

1. `STATE_MACHINE.md` updated to rev 4 on `2026-02-17`.
2. `§9` no longer documents Kelly as active sizing logic.
3. New sections `§27-§34` are present and internally consistent.
4. `§23` and `§24` match current runtime APIs/commands.
5. Verification checklist items pass with no unresolved drift except explicitly documented out-of-scope items.

## Rollback

1. Revert `STATE_MACHINE.md` to previous revision.
2. Re-run the verification checklist to confirm baseline parity is restored.
