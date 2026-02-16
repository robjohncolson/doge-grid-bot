# Slot Bootstrap Fix - Implementation Plan

Last updated: 2026-02-15
Parent spec: `docs/SLOT_BOOTSTRAP_FIX_SPEC.md` v0.1.0
Status: **Ready for verification and rollout hardening**

## Goal

Ensure new slots always converge to healthy two-sided operation when suppression is inactive, while still respecting active Tier 2 directional suppression and cooldown behavior.

## Scope

- Validate and lock behavior for:
  - conditional auto-repair of `mode_source="regime"` slots
  - scheduler-cap bypass for bootstrap and auto-repair entry placement
- Close remaining regression-test gaps.
- Run staged rollout verification with clear observability and rollback gates.

## Current State Snapshot

Based on review of current code:

- `bot.py` contains conditional suppression logic in `_auto_repair_degraded_slot()` (`bot.py:3812`).
- `bot.py` contains scheduler bypass exemptions in `_execute_actions()` (`bot.py:4074`).
- Regression tests exist for:
  - skip repair while suppression is active (`tests/test_hardening_regressions.py:1046`)
  - allow repair after suppression lapses (`tests/test_hardening_regressions.py:1102`)
- A dedicated regression test for scheduler bypass with `source in ("bootstrap", "bootstrap_regime", "auto_repair")` is not explicit and should be added.

## Execution Plan

## 1) Lock behavior contract in code comments and spec cross-links

- [ ] Confirm `docs/SLOT_BOOTSTRAP_FIX_SPEC.md` is the canonical behavior definition.
- [ ] In `bot.py`, keep/clarify comments at both gates:
  - [ ] repair gate: skip only when suppression/cooldown is still active
  - [ ] scheduler gate: exempt only `bootstrap`, `bootstrap_regime`, `auto_repair`
- [ ] Add a short reference line in the doc header of `bot.py` section comments to the spec path for future maintainers.

Deliverable: behavior contract is clear and discoverable in both spec and runtime code.

## 2) Add missing regression coverage for scheduler bypass

File: `tests/test_hardening_regressions.py`

- [ ] Add `test_execute_actions_bootstrap_bypasses_entry_scheduler_cap`.
  - Setup: `entry_adds_per_loop_cap=1`, two entry actions in one call, `source="bootstrap"`.
  - Assert: both entries placed in same loop, no defer increments.
- [ ] Add `test_execute_actions_auto_repair_bypasses_entry_scheduler_cap`.
  - Setup mirrors above with `source="auto_repair"`.
  - Assert identical bypass behavior.
- [ ] Add negative control test `test_execute_actions_non_bootstrap_respects_scheduler_cap`.
  - Setup with `source="unit_test"` (or similar).
  - Assert one defer occurs and pending queue increments.

Deliverable: bypass behavior is explicitly protected against regression.

## 3) Validate cooldown-sensitive repair behavior end-to-end

Files: `bot.py`, `tests/test_hardening_regressions.py`

- [ ] Add/confirm a test case where:
  - tier drops from 2 to 0,
  - cooldown is still active (`REGIME_TIER2_REENTRY_COOLDOWN_SEC` window),
  - degraded `mode_source="regime"` slot does not repair yet.
- [ ] Add/confirm a follow-on assertion that repair occurs after cooldown expiry.

Deliverable: repair timing is deterministic across tier transition and cooldown boundaries.

## 4) Verification run (local)

- [ ] Run targeted tests:
  - [ ] `python3 -m unittest tests.test_hardening_regressions.BotEventLogTests`
  - [ ] full file run for `tests/test_hardening_regressions.py`
- [ ] Verify no unrelated failures in entry scheduling, bootstrap, and regime-directional suites.
- [ ] Record pass/fail summary in PR or deployment notes.

Deliverable: reproducible green test run before rollout.

## 5) Staged rollout plan

## 5.1 Canary (single runtime)

- [ ] Deploy to canary runtime with normal production config.
- [ ] Observe for at least one full regime cycle (Tier 2 activation and downgrade if possible).
- [ ] Watch telemetry:
  - [ ] `entry_scheduler.deferred_total`
  - [ ] pending entry queue depth
  - [ ] count of one-sided slots (`long_only`/`short_only`) and their `mode_source`
  - [ ] time-to-repair for degraded regime slots after suppression/cooldown lapses

Success gate:
- No persistent one-sided slots outside active suppression/cooldown.
- No unexpected surge in entry order placement rate.

## 5.2 Fleet rollout

- [ ] Roll out to remaining runtimes after canary gate passes.
- [ ] Continue monitoring for 24 hours.
- [ ] Confirm deferred-entry backlog remains stable or improved versus baseline.

Deliverable: fix is production-stable across live load.

## 6) Rollback plan

If rollout regression is observed:

- [ ] Revert `_auto_repair_degraded_slot()` to pre-fix unconditional `mode_source="regime"` skip.
- [ ] Revert `_execute_actions()` scheduler exemption for bootstrap/repair sources.
- [ ] Redeploy previous known-good build.
- [ ] Manually reconcile one-sided slots (remove/re-add if needed) and monitor backlog recovery.

## Acceptance Criteria

- [ ] New slot bootstrap places both sides atomically when not regime-suppressed.
- [ ] Tier 2/cooldown suppression still prevents prohibited side placement.
- [ ] When suppression/cooldown lapses, degraded regime slots auto-repair without restart.
- [ ] Scheduler bypass behavior is covered by explicit regression tests.
- [ ] No increase in order-cap/rate-limit incidents during canary or fleet rollout.
