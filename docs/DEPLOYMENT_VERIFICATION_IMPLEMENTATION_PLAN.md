# Deployment Verification - Implementation Plan

Last updated: 2026-02-18
Parent spec: `docs/DEPLOYMENT_VERIFICATION_SPEC.md` v0.1
Depends on:
1. `docs/CHURNER_FIX_SPEC.md`
2. `docs/HALTED_PERSISTENCE_FIX_SPEC.md`
Status: **Ready for execution**

## Goal

Verify production deployment of:
1. Churner visibility + capital-adaptive entry (`ef0c95f`)
2. HALTED persistence auto-clear for transient shutdowns (`dcab1a2`)

## Scope

In scope:
1. Pre-deploy validation of code/test baseline.
2. Post-restart runtime-state verification (`RUNNING` vs `HALTED`).
3. Regime-gated churner behavior verification when market reaches `RANGING`.
4. Regression checks and rollback decision criteria.

Out of scope:
1. New feature implementation.
2. Strategy/regime logic redesign.
3. Kraken permission remediation (ledger-query permission remains operationally independent).

## Execution Phases

## Phase 0 - Preconditions

Tasks:
1. Confirm target branch and commits include both fixes:
   - `git log --oneline -n 20`
   - ensure `ef0c95f` and `dcab1a2` are present.
2. Confirm deployment target points at latest `master`.
3. Record current production snapshot of:
   - `/api/status` (`mode`, `pause_reason`, churner block)
   - current HMM regime (`hmm_regime.regime`).

Acceptance:
1. Deployment candidate is exactly the intended commit range.
2. Baseline pre-deploy state is captured for comparison.

## Phase 1 - Pre-Deploy Validation

Tasks:
1. Run target tests:
   - `python -m pytest tests/test_self_healing_slots.py tests/test_hardening_regressions.py -v`
2. If `pytest` is unavailable in runtime environment, run fallback:
   - `python3 -m unittest tests.test_self_healing_slots tests.test_hardening_regressions -q`
3. Optional syntax sanity:
   - `python3 -m py_compile bot.py dashboard.py tests/test_self_healing_slots.py tests/test_hardening_regressions.py`

Acceptance:
1. Test suites pass without new failures.
2. No syntax/import regressions.

## Phase 2 - Deploy + Restart

Tasks:
1. Deploy service with latest artifact.
2. Ensure process restart occurs cleanly.
3. Capture first startup logs for HALTED-clear behavior.

Expected log evidence:
1. `Snapshot restored transient HALTED state (signal 15); clearing to INIT for startup`
2. startup progression to running loop.

Acceptance:
1. Service is reachable.
2. Startup completes without crash loop.

## Phase 3 - Immediate Post-Deploy Verification

Tasks:
1. Query `GET /api/status`.
2. Validate:
   - `mode == "RUNNING"`
   - `pause_reason == ""` (or non-halt operational text only)
3. Confirm churner status endpoint reachable:
   - `GET /api/churner/status`
4. Confirm dashboard renders updated churner reserve label.

Acceptance:
1. Runtime no longer remains latched in `HALTED` after deploy restart.
2. Churner APIs return normally.

## Phase 4 - Regime-Gated Churner Functional Verification

Note:
1. Spawn in `BEARISH`/`BULLISH` is expected to fail with `regime_not_ranging`.
2. This is not a defect; validation must wait for `RANGING`.

Tasks when regime is `RANGING`:
1. Spawn churner from dashboard or `/api/churner/spawn`.
2. Verify HTTP response `200` and success message.
3. Verify UI state bar shows CHURN badge on selected slot.
4. Verify entry-side selection in USD-starved/DOGE-rich conditions:
   - `self_healing.churner.states[].entry_side == "sell"` where applicable.
5. Verify cycles move:
   - `cycles_today > 0` after at least one round-trip.
6. Confirm reserve label text:
   - `Churner Reserve`.

Acceptance:
1. Spawn succeeds in `RANGING`.
2. CHURN visibility and adaptive side behavior are both confirmed.

## Phase 5 - Regression Verification

Tasks:
1. Validate safety halt stickiness still works:
   - induce/observe non-transient halt scenario (test/staging preferred),
   - confirm HALTED is not auto-cleared for invariant-type reasons.
2. Validate capital-side preference logic:
   - preferred side still wins when both sides are funded.
3. Validate non-ranging spawn rejection still returns correct reason.

Acceptance:
1. Only transient shutdown halts auto-clear.
2. No regression in churner guardrails.

## Phase 6 - Rollback Plan

Rollback triggers:
1. Runtime repeatedly re-enters HALTED without transient reason.
2. Spawn succeeds but churner placement behavior regresses materially.
3. Unexpected API/status contract break.

Rollback sequence:
1. `git revert dcab1a2`
2. `git revert ef0c95f`
3. Redeploy.
4. Re-run Phase 3 immediate checks.

## Evidence Checklist

Collect and attach:
1. Startup log excerpt showing transient HALTED clear.
2. `/api/status` JSON excerpt with `mode` and `pause_reason`.
3. Churner spawn request/response during `RANGING`.
4. Screenshot of state bar CHURN badge + reserve label.
5. Post-verification summary including pass/fail per phase.

## Risks

1. Regime timing risk:
   - `RANGING` window may not align with deployment window; keep Phase 4 as delayed verification if needed.
2. Operational confusion risk:
   - spawn failures in non-ranging periods may be misread as defects; always cross-check `hmm_regime.regime`.
3. Environment mismatch risk:
   - local test tooling may differ from deploy runtime (`pytest` availability).

## Completion Criteria

1. Immediate checks pass (`RUNNING`, no persistent transient halt).
2. Ranging-window churner checks pass.
3. Regression checks pass.
4. Verification evidence is recorded with timestamped results.
