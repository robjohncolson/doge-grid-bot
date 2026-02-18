# Per-Slot Sticky Toggle - Implementation Plan

Last updated: 2026-02-18
Parent spec: `docs/PER_SLOT_STICKY_TOGGLE_SPEC.md` v0.1
Status: Draft

## Goal

Replace global sticky behavior with per-slot sticky mode so each regular pair slot can run as either:
1. `sticky=true`: keep waiting exits (current sticky behavior).
2. `sticky=false`: allow orphan flow, cancel stale exit on Kraken, write off in ledger, and continue cycling.

Keep ranger/churner code present but deprecated and hidden when disabled.

## Scope

In scope:
1. `bot.py` per-slot sticky plumbing, orphan write-off execution, snapshot persistence, startup cleanup, and API action routing.
2. `dashboard.py` per-slot mode visibility, toggle action, and panel hide behavior.
3. `config.py` deprecation comments for `RANGER_*` and `CHURNER_*`.
4. New tests for mixed-mode behavior and regressions.

Out of scope:
1. Deleting ranger/churner engines.
2. Reducer redesign in `state_machine.py`.
3. Changing self-healing subsidy logic.

## Code-Truth Constraints (from review)

1. `sm.OrphanOrderAction` has `local_id`, `recovery_id`, `reason` only (no `txid`).
   - Source: `state_machine.py:244`.
2. `_cancel_order()` can fail by returning `False` (not just raising).
   - Source: `bot.py:5113`.
3. Sticky gating is currently global across runtime methods, API gate, and dashboard controls.
   - Sources: `bot.py:14377`, `bot.py:14724`, `bot.py:17397`, `dashboard.py:1263`, `dashboard.py:3651`.
4. Snapshot currently stores slot state + aliases only, no sticky flag map.
   - Sources: `bot.py:3798`, `bot.py:4961`.
5. Startup recovery cleanup only runs when recovery orders are globally disabled.
   - Source: `bot.py:5127`.

## Mandatory Design Corrections

1. Orphan cancel/write-off flow must resolve txid from recovery row (`recovery_id`) or active order (`local_id`), not from action payload.
2. Non-sticky orphan write-off must be conditional on successful cancel (or no txid).
   - If cancel fails, keep recovery tracked; do not write off/unbind yet.
3. Mixed-mode gating must be per slot:
   - Runtime methods (`soft_close`, `cancel_stale_recoveries`, release helpers),
   - `/api/action` pre-validation,
   - Dashboard command parser and button visibility.
4. Snapshot back-compat for missing `slot_sticky` should use prior global sticky value as default, then persist explicit per-slot flags.

## Phase Plan

## Phase 1 - Slot Model and Engine Wiring

Files:
1. `bot.py`

Changes:
1. Add `sticky: bool = True` to `SlotRuntime`.
2. Add helper `_is_slot_sticky(slot_id: int) -> bool`.
3. Keep `_engine_cfg(slot: SlotRuntime)` signature; set `sticky_mode_enabled=slot.sticky`.
4. Update `_slot_mode_for_position` to accept `slot_id` and return `sticky` vs `legacy` per slot (`churner` unchanged).
5. Update call sites that currently rely on global sticky for slot-mode attribution.

Acceptance checks:
1. Engine config for two slots can differ in `sticky_mode_enabled` in the same loop.
2. Position rows opened from different slots carry correct `slot_mode`.

## Phase 2 - Non-Sticky Orphan Execution

Files:
1. `bot.py`

Changes:
1. Replace `OrphanOrderAction` no-op in `_execute_actions()` with slot-aware handling:
   - Sticky slot: retain current behavior.
   - Non-sticky slot: try cancel, then write off only when cancel is confirmed or txid absent.
2. Add `_write_off_orphaned_position(slot_id, action, recovery_row)` helper:
   - Resolve `position_id` via `_find_position_for_exit(slot_id, action.local_id, txid=...)`.
   - Close ledger row with `close_reason="write_off"` and reason context.
   - Unbind exit mapping.
   - Remove matching recovery row from state.
   - Clear `self._self_heal_hold_until_by_position[position_id]` and `self._belief_timer_overrides[position_id]`.
3. Add small helper to locate recovery row by `recovery_id` safely.

Acceptance checks:
1. Non-sticky orphan + cancel success: recovery removed, position closed as write-off, slot continues cycle.
2. Non-sticky orphan + cancel failure: no write-off, recovery remains tracked.
3. Sticky orphan behavior unchanged.

## Phase 3 - Persistence and Startup Reconciliation

Files:
1. `bot.py`

Changes:
1. `_global_snapshot()` add `slot_sticky` map.
2. `_load_snapshot()` restore per-slot sticky with compatibility default:
   - `slot_sticky[sid]` if present,
   - else previous global sticky runtime value.
3. Add startup cleanup for recovery orders belonging to non-sticky slots:
   - Cancel Kraken order if txid present.
   - Remove local recovery rows when cancel confirmed (or txid empty).
   - Keep rows on cancel failure for retry/reconciliation.
4. Keep existing global recovery-disabled cleanup path intact.

Acceptance checks:
1. Snapshot round-trip preserves mixed sticky states.
2. Legacy snapshots (no `slot_sticky`) load without forcing all slots sticky.
3. Startup does not silently drop live recoveries when cancel fails.

## Phase 4 - API and Runtime Action Gating

Files:
1. `bot.py`

Changes:
1. Add `/api/action` `toggle_sticky` action with `slot_id` validation and snapshot save.
2. Move sticky checks for `soft_close`, `soft_close_next`, `cancel_stale_recoveries` to per-slot semantics:
   - `soft_close(slot_id, recovery_id)`: block only if that slot is sticky.
   - `soft_close_next()`: choose oldest recovery among non-sticky slots.
   - `cancel_stale_recoveries(...)`: operate only on non-sticky slots.
3. Update API pre-validation (`DashboardHandler.do_POST`) to avoid global sticky short-circuit.

Acceptance checks:
1. Sticky slot rejects soft-close/cancel-stale actions.
2. Non-sticky slot accepts the same actions in mixed-mode runtime.
3. `toggle_sticky` persists immediately.

## Phase 5 - Dashboard UX and Visibility

Files:
1. `dashboard.py`

Changes:
1. Include per-slot mode indicators in UI:
   - Slot list label/badge,
   - Selected slot state bar badge (`STICKY` or `CYCLE`).
2. Add per-slot toggle control that dispatches `toggle_sticky`.
3. Replace global `isStickyModeEnabled()` usage in command parser and button visibility with selected-slot sticky checks.
4. Hide ranger/churner panels when disabled:
   - Ranger section hidden when `status.rangers.enabled === false`.
   - Churner panel hidden when `status.self_healing.churner.enabled === false` (fallback to existing churner payload as needed).

Acceptance checks:
1. Controls switch correctly when selecting sticky vs non-sticky slots.
2. `:close` and `:stale` command validation is slot-aware.
3. Disabled ranger/churner sections are not rendered.

## Phase 6 - Deprecation Markers

Files:
1. `config.py`

Changes:
1. Add deprecation comments above `CHURNER_*`, `RANGER_*`, and `MTS_CHURNER_GATE` indicating per-slot non-sticky replacement path.

Acceptance checks:
1. No runtime behavior change.
2. Config intent is clear for future cleanup.

## Phase 7 - Tests

Files:
1. `tests/test_nonsticky_slots.py` (new)
2. `tests/test_hardening_regressions.py` (updates)

Required tests:
1. `SlotRuntime` default sticky flag and explicit overrides.
2. `_engine_cfg` uses slot-level sticky.
3. Non-sticky orphan:
   - cancel success -> write-off + cleanup,
   - cancel failure -> no write-off, recovery retained.
4. Sticky orphan unchanged regression.
5. Snapshot round-trip for `slot_sticky`.
6. `toggle_sticky` API action.
7. `_auto_release_sticky_slots` ignores non-sticky slots.
8. API/runtime action gating behaves correctly in mixed mode.
9. Dashboard handler tests for per-slot gating (extend existing API tests).

## Rollout Order

1. Phase 1 and Phase 2 together behind internal review.
2. Phase 3 before exposing toggle UI.
3. Phase 4 and Phase 5 in same PR to avoid API/UI mismatch.
4. Phase 6 and Phase 7 before merge.

## Risks and Mitigations

1. Risk: Losing track of a live Kraken exit if cancel fails during non-sticky write-off.
   - Mitigation: write-off only after confirmed cancel or no txid.
2. Risk: Mixed-mode control confusion from global sticky checks.
   - Mitigation: remove global gating from runtime/API/UI action paths.
3. Risk: Legacy snapshot behavior shift.
   - Mitigation: compatibility default from prior global sticky value.

## Rollback

1. Flip all slots back to sticky via `toggle_sticky` action.
2. Keep ranger/churner disabled by default.
3. Revert single feature branch if needed; schema change is additive (`slot_sticky` optional).
