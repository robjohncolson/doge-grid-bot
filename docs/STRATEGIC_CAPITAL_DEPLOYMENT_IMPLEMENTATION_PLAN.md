# Strategic Capital Deployment - Implementation Plan

Last updated: 2026-02-16
Parent spec: `docs/STRATEGIC_CAPITAL_DEPLOYMENT_SPEC.md` v0.1
Status: **Verified and ready for implementation**

## Goal

Implement strategic idle-capital deployment with a 1h HMM signal, AI advisor/provider simplification, DCA accumulation execution, recovery-order deprecation cleanup, and throughput age-pressure hardening while keeping core slot state-machine trading behavior stable.

## Scope

In scope:
- 1h tertiary HMM data/training/inference wiring.
- DeepSeek primary + Groq fallback for regime advisor.
- AI regime output schema extension with accumulation recommendation fields.
- New accumulation runtime state machine (ARMED/ACTIVE/COMPLETED/STOPPED).
- Recovery-order deprecation controls and one-time startup cleanup.
- Throughput age pressure switch from `max` to `p90`.
- Dashboard/status payload updates for 1h HMM, accumulation, provider badge, and age-pressure reference.
- Test coverage for all new behaviors.

Out of scope:
- Changes to S0/S1/S2 mechanics unrelated to this feature set.
- Entry/exit pricing logic rewrites.
- General AI council behavior for non-regime endpoints.
- Legacy `grid_strategy.py` behavioral overhaul beyond targeted compatibility guards.

## Verification Outcome (Spec vs Code)

The spec was validated against current runtime architecture in `bot.py`, `ai_advisor.py`, `config.py`, `throughput_sizer.py`, `dashboard.py`, and tests.

Key verification findings:
1. Live runtime path is state-machine-driven (`bot.py` + `state_machine.py`), not `grid_strategy.py`. Recovery deprecation must be implemented in runtime/state-machine surfaces, not only legacy grid code.
2. `kraken_client` exposes `place_order(...)` (with `ordertype="market"` support), not `add_order()`.
3. Current `hmm_consensus` feeds directional regime gating and throughput context. Directly inserting 1h into this tactical consensus would change behavior beyond the spec’s “1h is strategic” intent.
4. `ai_advisor.py` currently uses a 7-model panel for regime opinion. Regime-provider swap must not break non-regime advisor paths that still rely on panel helpers.
5. Throughput age pressure currently uses oldest (`max`) exit age and includes recovery orders; this matches the problem statement and requires the planned fix.

## Locked Repo-Specific Decisions

1. Keep existing tactical consensus (`1m+15m`) for mechanical regime actuation; add a separate strategic 1h block/transition signal for accumulation + AI context.
2. Use `kraken_client.place_order(..., ordertype="market")` for accumulation fills.
3. Add a dedicated regime-provider path (DeepSeek->Groq) without removing existing council helpers used by other advisor functions.
4. Introduce `RECOVERY_ORDERS_ENABLED` but keep a compatibility bridge from legacy recovery settings to avoid abrupt non-sticky regressions.
5. Implement startup recovery cleanup as idempotent and safe to re-run.

## Current Baseline (Code Audit)

1. `bot.py` has primary and secondary HMM runtime only (`_hmm_detector`, `_hmm_detector_secondary`).
2. OHLC sync/backfill/readiness is parameterized for `primary` and `secondary`; no tertiary state exists.
3. `bot.py::_compute_hmm_consensus()` currently blends only 1m/15m and drives policy-facing regime outputs.
4. `bot.py::_build_ai_regime_context()` includes 1m/15m + operational data; no tertiary/accumulation block.
5. `bot.py::_process_ai_regime_pending_result()` parses regime-only fields (tier/direction/conviction/rationale/watch/ttl/panelist).
6. `ai_advisor.py::get_regime_opinion()` uses ordered 7-panel fallback chain with cooldown tracking.
7. `throughput_sizer.py::_compute_age_pressure()` uses `max(age_sec)` and stores only `oldest_open_exit_age_sec`.
8. `bot.py::_collect_open_exits()` always includes recovery orders in throughput open-exit set.
9. `dashboard.py` has 1m/15m HMM rows only, no accumulation card, and age-pressure display has no reference-age annotation.
10. Snapshot/status payloads have no accumulation session fields.

## Implementation Order

1. Config contract and compatibility scaffolding.
2. Tertiary 1h HMM data pipeline + transition tracking.
3. AI advisor provider/schema/context upgrade.
4. Accumulation runtime engine.
5. Recovery deprecation + startup cleanup + throughput exclusion.
6. Throughput p90 algorithm update.
7. Dashboard + API/status rendering updates.
8. Tests, rollout checks, and docs finalization.

## Phase Plan

## Phase 1 - Config and Env Contract

Files:
- `config.py`
- `.env.example`

Changes:
1. Add `HMM_TERTIARY_*` and `CONSENSUS_1H_WEIGHT`.
2. Add `DEEPSEEK_*` and `AI_REGIME_PREFER_DEEPSEEK_R1`.
3. Add `ACCUM_*` runtime controls.
4. Add `RECOVERY_ORDERS_ENABLED` with compatibility fallback from legacy recovery toggle.
5. Keep defaults conservative (`HMM_TERTIARY_ENABLED=False`, `ACCUM_ENABLED=False`).

Deliverable:
- Feature flags and env contract exist before runtime wiring.

## Phase 2 - 1h HMM Runtime Integration

Files:
- `bot.py`
- `hmm_regime_detector.py` (no model logic change expected; only if interface gaps are found)

Changes:
1. Add tertiary runtime state:
   - detector/state/training-depth/since-cursor/sync/backfill/readiness/snapshot keys.
2. Extend OHLC sync/backfill/readiness helpers to support `state_key="tertiary"`.
3. Implement 15m->1h bootstrap helper for first-train path:
   - aggregate consecutive 15m groups of 4 into synthetic 1h candles.
   - use for initial tertiary training when no 1h rows exist.
4. Add `_train_hmm_tertiary()` and `_update_hmm_tertiary()` in same pattern as secondary.
5. Add tertiary transition tracking:
   - prior regime, current regime, transition age, confirmation streak.
6. Preserve existing tactical policy source; add new strategic payload block rather than replacing existing policy consensus semantics.
7. Extend snapshot save/load with tertiary fields and backward-compatible defaults.

Deliverable:
- Stable 1h detector lifecycle without changing tactical slot behavior.

## Phase 3 - AI Regime Advisor Upgrade

Files:
- `ai_advisor.py`
- `bot.py`

Changes:
1. Add DeepSeek HTTP call helper (OpenAI-compatible JSON payload).
2. Update regime advisor path to:
   - DeepSeek primary (`deepseek-chat` or `deepseek-reasoner`),
   - Groq Llama-70B fallback,
   - safe default on double failure.
3. Keep non-regime council functions intact (`get_recommendation()`, `analyze_trade()` paths).
4. Extend `_REGIME_SYSTEM_PROMPT` with accumulation guidance.
5. Extend parsing/defaults with:
   - `accumulation_signal` (`accumulate_doge|hold|accumulate_usd`)
   - `accumulation_conviction` (0-100)
6. Extend AI context builder to include:
   - tertiary HMM state + transition block,
   - capital metrics (`idle_usd`, `idle_usd_pct`, `free_doge`, `util_ratio`),
   - current accumulation session block.
7. Update bot pending-result processing and status payload to persist new AI fields.

Deliverable:
- Regime advisor uses paid-primary architecture and emits accumulation directives.

## Phase 4 - Accumulation Engine

Files:
- `bot.py`
- `dashboard.py` (control endpoint wiring and display)

Changes:
1. Add runtime accumulation state fields in `BotRuntime.__init__`.
2. Persist/restore accumulation fields in `_global_snapshot()`/`_load_snapshot()`.
3. Implement accumulation state machine:
   - `IDLE -> ARMED -> ACTIVE -> COMPLETED/STOPPED`.
4. Add trigger gating:
   - flag enabled,
   - confirmed 1h transition,
   - AI signal + conviction threshold,
   - reserve/capacity/cooldown checks.
5. Implement DCA execution method:
   - market buy via `kraken_client.place_order(ordertype="market", type=buy)`.
   - interval and chunk controls.
6. Implement abort logic:
   - transition revert, AI hold streak, drawdown, capacity stop, manual stop.
7. Add dashboard/API stop action for active accumulation session.
8. Add status payload `accumulation` block (live + last-session summary fields).

Deliverable:
- Controlled, bounded DCA deployment that can be observed and stopped safely.

## Phase 5 - Recovery Deprecation and Cleanup

Files:
- `bot.py`
- `state_machine.py` (only if required after runtime gating pass)
- `dashboard.py`

Changes:
1. Add startup cleanup routine:
   - find all tracked recovery orders,
   - cancel Kraken txids,
   - clear `recovery_orders` from every slot,
   - snapshot + summary log.
2. Guard legacy/manual recovery actions when disabled:
   - `soft_close`, `soft_close_next`, `cancel_stale_recoveries`, auto-drain/auto-soft-close loops.
3. Exclude recovery orders from throughput open-exit collection when disabled.
4. Hide or mute recovery-specific UI sections when no recoveries exist.
5. If runtime behavior still emits new recoveries under non-sticky mode, add a strict compatibility guard (documented warning + safe fallback) before enabling default-off in production.

Deliverable:
- Recovery backlog removed and kept from re-inflating in sticky/deprecated flow.

## Phase 6 - Throughput Age Pressure p90

Files:
- `throughput_sizer.py`
- `bot.py`
- `dashboard.py`

Changes:
1. Replace oldest-age reference with p90 age reference in `_compute_age_pressure()`.
2. Track and expose:
   - `age_pressure_reference` (`"p90"`),
   - `age_pressure_ref_age_sec`,
   - legacy `oldest_open_exit_age_sec` retained for compatibility if needed.
3. Update dashboard rendering to show:
   - `Age Pressure: XX% (p90 age: YY)` when enabled.

Deliverable:
- Single outliers no longer pin age pressure to floor.

## Phase 7 - Dashboard and Payload Surface Updates

Files:
- `dashboard.py`
- `bot.py`

Changes:
1. Add 1h HMM display row and training quality line.
2. Add accumulation card with state, budget progress, trigger, drawdown, and stop control.
3. Update AI card:
   - provider badge (`deepseek-chat`, `deepseek-reasoner`, fallback marker),
   - accumulation signal + accumulation conviction.
4. Update throughput label for p90 reference age.
5. Keep all existing cards backward-compatible when flags are off.

Deliverable:
- UI surfaces expose new strategic systems without breaking existing operator flows.

## Phase 8 - Tests and Verification

Files:
- `tests/test_ai_regime_advisor.py`
- `tests/test_throughput_sizer.py`
- `tests/test_hardening_regressions.py`
- `tests/test_strategic_capital_deployment.py` (new, if cleaner than expanding hardening file)

Add/Update tests:
1. 1h bootstrap aggregation from 15m rows (OHLC/volume correctness).
2. Tertiary training/depth/status payload presence.
3. 1h transition confirmation logic.
4. Regime advisor DeepSeek success path.
5. DeepSeek failure -> Groq fallback path.
6. Double-failure safe default (hold/0 accumulation).
7. Parse defaults for missing accumulation fields.
8. Accumulation state transitions (IDLE->ARMED->ACTIVE->COMPLETED/STOPPED).
9. Reserve/cooldown/drawdown/capacity abort guards.
10. Snapshot round-trip for accumulation state.
11. Startup recovery cleanup idempotence.
12. Throughput p90 reference behavior and status payload fields.

Verification commands:
1. `python3 -m unittest tests.test_ai_regime_advisor`
2. `python3 -m unittest tests.test_throughput_sizer`
3. `python3 -m unittest tests.test_hardening_regressions`
4. `python3 -m unittest tests.test_strategic_capital_deployment` (if added)

## Rollout Plan

1. Stage A (low risk):
   - Deploy p90 age-pressure fix + recovery cleanup controls + tertiary pipeline disabled.
2. Stage B (medium risk):
   - Enable tertiary HMM and observe training/transition telemetry.
3. Stage C (medium risk):
   - Enable DeepSeek regime advisor path; accumulation engine still disabled.
4. Stage D (higher risk):
   - Enable accumulation with conservative budget/chunk/drawdown settings.
5. Stage E:
   - Tune thresholds/weights after 48-72h of telemetry.

## Rollback Plan

1. `ACCUM_ENABLED=False` to halt new DCA sessions.
2. Disable tertiary signal consumption while keeping tactical consensus untouched.
3. Remove/disable DeepSeek key to force fallback behavior.
4. Re-enable recovery operations only if needed for non-sticky fallback.
5. Revert p90 code path to `max` only if operationally required.

## Acceptance Criteria

1. All new config/env keys are wired and documented.
2. 1h HMM status/training/transition appears in payload and dashboard when enabled.
3. AI regime advisor returns provider + accumulation fields with safe defaults on errors.
4. Accumulation engine never breaches reserve or budget and honors abort rules.
5. Recovery cleanup runs once and is safe on repeated startups.
6. Age pressure no longer overreacts to a single stale outlier.
7. New and updated tests pass with no regression in existing suites.

