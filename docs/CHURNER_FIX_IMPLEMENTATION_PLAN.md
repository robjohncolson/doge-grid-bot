# Churner Fix - Implementation Plan

Last updated: 2026-02-18
Parent spec: `docs/CHURNER_FIX_SPEC.md` v0.1
Status: **Ready for implementation**

## Goal

Fix churner usability and execution by:
1. Making active churners obvious in the slot state bar.
2. Letting churner entry side adapt to available capital (buy/sell fallback).
3. Clarifying reserve labeling in dashboard UI.

## Scope

In scope:
1. `bot.py` churner gate and entry-side wiring changes.
2. `dashboard.py` state bar badge + reserve label update.
3. Targeted test additions for churner gate behavior and dashboard markup.

Out of scope:
1. API contract changes (`/api/churner/*` remains unchanged).
2. New config keys.
3. Churner lifecycle redesign outside entry-side selection.

## Baseline Review (Code-Truth)

1. `_churner_gate_check()` in `bot.py:7396` currently returns a 5-tuple and checks only one side (derived from parent trade).
2. The churner engine caller in `bot.py:7963` re-derives side from parent trade and ignores any adaptive choice opportunity.
3. Slot state bar rendering in `dashboard.py:3679` does not display churner activity.
4. The reserve label in `dashboard.py:824` is currently `"Runtime Reserve (USD)"`.
5. `runtimeChurnerState(slotId)` and `churnerStageLabel(stage)` already exist and can support the badge without API additions.

## Locked Decisions

1. Keep `_churner_entry_side_for_trade()` unchanged as the preferred-side rule.
2. Add side fallback only inside `_churner_gate_check()` to centralize capital gating logic.
3. Preserve existing reserve backstop behavior; only append chosen side to the gate return payload.
4. Keep churner UI visibility in state bar only (no slot list row proliferation in this change).

## Implementation Phases

## Phase 1 - Backend: Capital-Adaptive Gate Side

Files:
1. `bot.py`

Changes:
1. Update `_churner_gate_check()` return type:
   - from `tuple[bool, str, float, float, float]`
   - to `tuple[bool, str, float, float, float, str]`
2. Ensure all early-return failures append `""` as the 6th element.
3. Replace single-side capital check with preferred/opposite loop:
   - derive `preferred_side` from parent trade (`B -> buy`, else `sell`)
   - derive `opposite_side`
   - attempt capital check for preferred first, then opposite
   - return first side with sufficient free capital and reason `"ok"`
4. If neither side has free capital:
   - keep existing reserve-backstop path
   - reserve path still prices/sizes by preferred side
   - append preferred side to success/failure returns as appropriate

Acceptance checks:
1. Gate returns `"sell"` when parent-preferred `"buy"` is USD-starved but DOGE is available.
2. Gate still returns preferred side when both sides are capitalized.
3. Existing reserve exhaustion and below-min-size failures preserve reasons and now return 6 values.

## Phase 2 - Backend: Churner Tick Side Wiring

Files:
1. `bot.py`

Changes:
1. Update `_run_churner_engine()` gate unpacking at the call site (`bot.py:7963`) to include `chosen_side`.
2. Set `state.entry_side` from `chosen_side` when present; fallback to existing derived preferred side only when empty.
3. Keep downstream behavior unchanged:
   - `_try_reserve_loop_funds(side=state.entry_side, ...)`
   - `_place_order(side=state.entry_side, ...)`
   - exit side derivation on fill remains automatic (`buy <-> sell`)

Acceptance checks:
1. Entry order side passed to `_place_order()` matches adaptive side chosen by gate.
2. No changes to stage transitions (`idle -> entry_open -> exit_open -> idle`).

## Phase 3 - Dashboard Visibility + Label Update

Files:
1. `dashboard.py`

Changes:
1. Add `.churner-badge` CSS style near existing state pill styles.
2. Update `renderSelected()` state bar template (`dashboard.py:3679`) to append:
   - `CHURN` when active + idle
   - `CHURN ENTRY OPEN` or `CHURN EXIT OPEN` when active in those stages
3. Use existing helpers only:
   - `runtimeChurnerState(slot.slot_id)`
   - `churnerStageLabel(stage)`
4. Change reserve header text:
   - from `"Runtime Reserve (USD)"`
   - to `"Churner Reserve"`

Acceptance checks:
1. Active churner is visible immediately in state bar.
2. Badge text reflects stage when not idle.
3. Reserve field label reflects side-agnostic execution behavior.

## Phase 4 - Tests and Regression Coverage

Files:
1. `tests/test_self_healing_slots.py`
2. `tests/test_hardening_regressions.py`

Add tests:
1. `test_churner_gate_check_uses_opposite_side_when_preferred_lacks_capital`
   - setup parent trade `B` (preferred `buy`)
   - free USD insufficient, free DOGE sufficient
   - assert gate success with `chosen_side == "sell"`
2. `test_churner_gate_check_prefers_parent_side_when_both_sides_have_capital`
   - both capital pools sufficient
   - assert `chosen_side` remains preferred side
3. `test_run_churner_engine_places_entry_with_gate_chosen_side`
   - churner active/idle candidate path
   - assert `_place_order` uses chosen adaptive side
4. `test_dashboard_churner_badge_markup_present`
   - assert `dashboard.DASHBOARD_HTML` contains `churner-badge` and `CHURN`
5. `test_dashboard_churner_reserve_label_updated`
   - assert `dashboard.DASHBOARD_HTML` contains `Churner Reserve`

Regression test pass set:
1. `python -m pytest tests/test_self_healing_slots.py`
2. `python -m pytest tests/test_hardening_regressions.py`

## Verification Checklist

1. Spawn churner on a slot and confirm state bar immediately shows `CHURN`.
2. In a USD-starved / DOGE-rich state, confirm gate reason is `ok` and entry side is `sell`.
3. Confirm cycle counters and profit fields move after at least one churner round trip.
4. Confirm dashboard reserve card reads `Churner Reserve`.
5. Confirm no regressions in churner API routes and existing hardening tests.

## Risks

1. Tuple-shape change risk:
   - any missed `_churner_gate_check()` unpacking will raise runtime errors.
2. Capital-check drift risk:
   - buy/sell branch precision tolerance (`1e-12`) must remain consistent with existing checks.
3. UI rendering risk:
   - state bar string template changes can regress if `slot.open_orders` or churner state is unexpectedly null-like.

## Rollback Plan

1. Revert `bot.py` gate-return and caller-side changes.
2. Revert `dashboard.py` state bar + reserve label edits.
3. Remove newly added churner-fix tests.
4. Re-run `tests/test_self_healing_slots.py` and `tests/test_hardening_regressions.py` to validate baseline restoration.
