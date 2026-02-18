# Idempotent Skipping Thunder - Implementation Plan

Last updated: 2026-02-17
Parent spec: `docs/MANIFOLD_SCORE_OPS_PANEL_CHURNER_SPEC.md` v0.1 (2026-02-17)
Status: **Plan only (no implementation in this document)**

## Goal

Deliver the full manifold/ops/churner package in deployable increments:
1. Manifold Trading Score (MTS) with deterministic Phase 1 math.
2. Runtime operations toggles without process restart.
3. Dashboard statistical visualizations (MTS + simplex + flow ribbon + age heatmap).
4. AI regime-advisor context enrichment with bounded token usage.
5. Manual churner lifecycle controls (spawn/kill/status/candidates), gated by MTS.

## Scope

In scope:
1. `bayesian_engine.py` MTS scoring module and status serialization.
2. `bot.py` runtime integration, API surface (`/api/ops/*`, `/api/churner/*`), and status payload expansion.
3. `config.py` new `MTS_*` knobs and churner gating knobs.
4. `ai_advisor.py` context schema extension and prompt update.
5. `dashboard.py` ops drawer, MTS panel, simplex/ribbon visuals, and churner controls.
6. Test updates in `tests/test_bayesian_intelligence.py`, `tests/test_hardening_regressions.py`, `tests/test_ai_regime_advisor.py`, and `tests/test_self_healing_slots.py`.

Out of scope:
1. MTS Phase 2 kernel-memory implementation (schema only; no runtime scoring blend yet).
2. MTS Phase 3 GP surface.
3. State-machine reducer redesign.
4. Auto-spawn churner strategy (manual spawn only after this rollout).
5. Persisting ops runtime overrides to disk or `config.py`.

## Current Baseline (Code Audit)

1. MTS does not exist in `bayesian_engine.py`, `bot.py`, `config.py`, or `dashboard.py`.
2. HTTP server exposes `GET /` (dashboard), `GET /factory`, `GET /api/status`, `GET /api/swarm/status`, and `POST /api/action`. No `/api/ops/*` or `/api/churner/*` routes exist. All routes dispatch through `DashboardHandler.do_GET`/`do_POST` (line 14763+).
3. Churner runtime auto-spawns on idle slots: when `state.active` is False, `_run_churner_engine()` (line 6905) autonomously calls `_churner_candidate_parent_position()`, and if a candidate is found and gate checks pass, it immediately activates and begins churning. Phase 4 changes this to require explicit operator spawn.
4. Self-healing payload already includes age-band counts and churner summary, but no manual churner slot controls.
5. AI context flows through two stages: `bot.py:_build_ai_regime_context(now)` (line 8462, raw builder) → `ai_advisor.py:_build_regime_context(payload)` (line 649, normalizer). Neither currently includes manifold/position-age/churner detail.
6. Dashboard has no ops drawer or runtime toggle UX.
7. Existing test suites already cover status payload contracts, dashboard API handler routing, AI context schema, and churner lifecycle behavior.

## Locked Implementation Decisions

1. Keep MTS Phase 1 deterministic and side-effect free in `bayesian_engine.py` first.
2. Add dedicated ops/churner endpoints while preserving existing `/api/action` behavior for backward compatibility.
3. Runtime overrides are in-memory only and intentionally excluded from snapshot persistence.
4. Churner behavior changes from auto-spawn to explicit manual spawn requests per slot.
5. MTS gating is advisory by default; entry throttling remains opt-in via `MTS_ENTRY_THROTTLE_ENABLED`.
6. Dashboard visualizations remain inline SVG (no external chart libraries).
7. Component bootstrap/shutdown for runtime-toggled subsystems (`HMM`, `HMM_TERTIARY`, `TP`, `BOCPD`, `SURVIVAL`, `BELIEF_TRACKER`, `ENRICHED_FEATURES`, `POSITION_LEDGER`) is handled explicitly, not by mutating `config.py`.

## Phase Order

1. Phase 0: Runtime toggle foundation and contracts.
2. Phase 1: MTS engine and configuration.
3. Phase 2: Bot runtime MTS integration and strategy hooks.
4. Phase 3: Ops API + runtime override plumbing.
5. Phase 4: Churner manual spawn API and runtime manualization.
6. Phase 5: AI advisor context enrichment.
7. Phase 6: Dashboard UI delivery (ops drawer + MTS/visuals + churner controls).
8. Phase 7: Verification, staged rollout, and rollback gates.

## Detailed Plan

## Phase 0 - Toggle Foundation

Files:
1. `bot.py`

Changes:
1. Add runtime override store: `self._runtime_overrides: dict[str, bool]`.
2. Add helper methods:
   - `_flag_value(key: str) -> bool` — resolution: `_runtime_overrides.get(key, getattr(config, key, False))`
   - `_set_runtime_override(key: str, value: bool) -> tuple[bool, str]`
   - `_clear_runtime_override(key: str) -> tuple[bool, str]`
   - `_clear_all_runtime_overrides() -> int`
3. Add central toggle registry in runtime (key, group, description, dependencies, runtime-safe side-effect handler).
4. Ensure `_global_snapshot()` (line 3070) and `_load_snapshot()` (line 3436) do not persist or restore overrides. Note: there is no `_restore_global_snapshot()` — the restore path is `_load_snapshot()`.

Acceptance checks:
1. Runtime override takes precedence over `config` constant lookup.
2. Clearing override reverts effective behavior to config defaults.
3. Restart clears overrides by design.

## Phase 1 - MTS Engine

Files:
1. `bayesian_engine.py`
2. `config.py`

Changes:
1. Add MTS dataclasses:
   - `ManifoldScoreComponents`
   - `ManifoldScore`
2. Add computation helpers:
   - `compute_regime_clarity(...)`
   - `compute_regime_stability(...)`
   - `compute_throughput_efficiency(...)`
   - `compute_signal_coherence(...)`
   - `compute_manifold_score(...)`
3. Add band labeling/color mapping utility for score ranges.
4. Add config constants:
   - `MTS_ENABLED`
   - `MTS_CLARITY_WEIGHTS`
   - `MTS_STABILITY_SWITCH_WEIGHTS`
   - `MTS_COHERENCE_WEIGHTS`
   - `MTS_HISTORY_SIZE`
   - `MTS_ENTRY_THROTTLE_ENABLED`
   - `MTS_ENTRY_THROTTLE_FLOOR`
   - `MTS_KERNEL_ENABLED`
   - `MTS_KERNEL_MIN_SAMPLES`
   - `MTS_KERNEL_ALPHA_MAX`
   - `MTS_CHURNER_GATE`
   - `CHURNER_MAX_ACTIVE`
   - `CHURNER_PROFIT_ROUTING`

Acceptance checks:
1. Scores are bounded to `[0, 1]`.
2. Hand-calculated test vectors match function output.
3. Disabled mode returns explicit `enabled: false` payload shape.

## Phase 2 - Bot Runtime MTS Integration

Files:
1. `bot.py`

Changes:
1. Add runtime state:
   - `self._manifold_score`
   - `self._manifold_history` deque (timestamp + score + components)
2. Add `_update_manifold_score(now)` called in loop after belief/BOCPD/throughput updates.
3. Feed available inputs:
   - belief posteriors/entropy/p_switch/direction/confidence
   - BOCPD state (`change_prob`, `run_length_mode`)
   - throughput payload (`active regime multiplier`, age pressure)
   - slot-vintage stuck capital percentage
4. Extend `status_payload()` with `manifold_score` block, compact sparkline list, and `regime_history_30m` (list of `{ts, regime, conf}` entries for regime ribbon visualization).
5. Integrate optional entry throttling into `_compute_entry_adds_loop_cap()` (line 1929). MTS throttle is a secondary gate applied AFTER the existing headroom-based throttle (headroom ≤5→1, ≤10→2, ≤20→3). Both constraints apply independently:
   - if throttle enabled and MTS below floor => cap 0
   - else apply `floor(base_cap * mts)` with safe bounds.

Acceptance checks:
1. Status payload includes `manifold_score` with components/details/history.
2. Entry scheduler cap responds to MTS only when throttle flag is enabled.
3. Loop remains stable when any source signal is missing.

## Phase 3 - Ops API and Runtime Overrides

Files:
1. `bot.py`

Changes:
1. Add `GET /api/ops/toggles`.
2. Add `POST /api/ops/toggle` (`key`, `value`).
3. Add `POST /api/ops/reset` (`key`).
4. Add `POST /api/ops/reset-all`.
5. Add `ops_panel` status block with override metadata.
6. Wire side-effect handlers for toggles that require lifecycle actions (constructor-time init):
   - `HMM_ENABLED` — RegimeDetector(1m) created at line 1029
   - `HMM_TERTIARY_ENABLED` — RegimeDetector(1h) created at line 1034
   - `TP_ENABLED` — ThroughputSizer instance
   - `BOCPD_ENABLED` — BOCPD detector instance
   - `SURVIVAL_MODEL_ENABLED` — survival model instance
   - `BELIEF_TRACKER_ENABLED` — BeliefEngine instance
   - `ENRICHED_FEATURES_ENABLED` — microstructure feature pipeline
   - `POSITION_LEDGER_ENABLED` — position ledger (also gates `_churner_enabled()` at line 6315)

Acceptance checks:
1. Toggle changes apply on next loop without restart.
2. Override metadata accurately reports source (`config_default` vs `runtime_override`).
3. Reset-all clears overrides and reverts effective values.

## Phase 4 - Churner Manual Spawn API

Files:
1. `bot.py`
2. `config.py`

Changes:
1. Add churner API:
   - `GET /api/churner/status`
   - `GET /api/churner/candidates`
   - `POST /api/churner/spawn`
   - `POST /api/churner/kill`
   - `POST /api/churner/config`
2. Change runtime model from auto-idle scanning to explicit manual spawn intent:
   - Remove the auto-discovery path in `_run_churner_engine()` (line 6974: `candidate = self._churner_candidate_parent_position(...)`) so idle slots do nothing unless operator requests spawn.
3. Apply spawn gates (reuse existing `_churner_enabled()` helper at line 6315 plus new gates):
   - `_churner_enabled()` (checks `CHURNER_ENABLED` + `_position_ledger_enabled()`)
   - MTS > `MTS_CHURNER_GATE`
   - `CHURNER_MAX_ACTIVE` limit
   - capacity/headroom and existing safety checks.
4. Add slot-level churner summary payload for dashboard control state.
5. `GET /api/churner/candidates` calls existing `_churner_candidate_parent_position()` per slot to list eligible positions with age bands and subsidy needs.

Acceptance checks:
1. No churner cycle starts unless spawned explicitly.
2. Kill action cancels active churner orders and returns slot to idle.
3. Spawn is rejected with clear reason when gate conditions fail.

## Phase 5 - AI Advisor Enrichment

Files:
1. `bot.py`
2. `ai_advisor.py`
3. `tests/test_ai_regime_advisor.py`

Changes:
1. Extend `bot.py:_build_ai_regime_context(now)` (line 8462, raw context builder) with:
   - manifold block (mts, band, components, trend)
   - positions block (age bands, stuck capital, distance, negative EV count)
   - throughput block (active regime, multiplier, age pressure, median fill time)
   - churner block (enabled, active slots, reserve, subsidy)
2. Extend `ai_advisor.py:_build_regime_context(payload)` (line 649, normalization layer) to extract and normalize the new blocks from the raw payload.
3. Update `_REGIME_SYSTEM_PROMPT` to instruct how to use MTS and throughput/age context.
4. Add schema-focused tests to guard backwards compatibility and token-safe truncation behavior.

Acceptance checks:
1. Prompt context contains new structured blocks with normalized ranges.
2. Existing required fields remain present for old logic.
3. Advisor path still succeeds when manifold/churner data are absent.

## Phase 6 - Dashboard UI

Files:
1. `dashboard.py`

Changes:
1. Add ops drawer UI:
   - top-right trigger
   - grouped toggles
   - override indicators
   - reset-all control.
2. Add MTS card:
   - score + band
   - sparkline (follow existing `renderEquityChart()` SVG polyline pattern at dashboard.py lines 758-823)
   - component bars
   - decomposition tooltip.
3. Add HMM simplex ternary SVG.
4. Add regime flow ribbon SVG from `regime_history_30m` (exposed via `status_payload()` in Phase 2).
5. Replace self-healing age heat text-only presentation with stacked heatmap bar.
6. Add per-slot churner controls (spawn/kill/status/stats) and summary block.
7. Add frontend API bindings for new `/api/ops/*` and `/api/churner/*` routes.

Acceptance checks:
1. Dashboard loads with no JS errors on desktop/mobile widths.
2. Toggle and churner actions round-trip successfully and refresh state.
3. SVG visual components render correctly with sparse and full data.

## Phase 7 - Test and Rollout

Files:
1. `tests/test_bayesian_intelligence.py`
2. `tests/test_hardening_regressions.py`
3. `tests/test_self_healing_slots.py`
4. `tests/test_ai_regime_advisor.py`

Changes:
1. Add MTS computation unit tests and edge-case clamps.
2. Add API routing tests for new `/api/ops/*` and `/api/churner/*` handlers.
3. Update churner tests for manual spawn semantics (old auto-spawn expectations removed).
4. Add status payload assertions for `manifold_score` and `ops_panel`.
5. Add restart-volatility test for runtime overrides.

Rollout stages:
1. Stage 1: Deploy with `MTS_ENABLED=true`, `MTS_ENTRY_THROTTLE_ENABLED=false`, keep churner manual API unused.
2. Stage 2: Enable ops drawer and runtime toggles in one environment; validate no restart required.
3. Stage 3: Enable manual churner controls with conservative `MTS_CHURNER_GATE`.
4. Stage 4: Optionally enable MTS entry throttle after observing score stability.

Rollback:
1. Disable `MTS_ENABLED` and/or `MTS_ENTRY_THROTTLE_ENABLED`.
2. Set `CHURNER_ENABLED=false` or avoid spawning.
3. Ignore runtime overrides via reset-all; restart clears all volatile overrides.

## Risks and Mitigations

1. Risk: Runtime-toggled subsystems with constructor-only initialization can drift.
   Mitigation: explicit hot-start/hot-stop handlers and tests per subsystem.
2. Risk: Manual churner migration breaks assumptions from auto-spawn behavior.
   Mitigation: update self-healing/churner tests first; gate deploy with manual-only canary.
3. Risk: MTS noise causes over-throttling.
   Mitigation: keep throttling opt-in and monitor MTS band distribution before activation.
4. Risk: Dashboard complexity introduces regressions.
   Mitigation: isolate additions into small render helpers and guard against missing payload keys.

## Definition of Done

1. `status_payload()` includes stable `manifold_score` and `ops_panel` contracts.
2. Runtime ops toggles can be changed/reset without restart and do not persist across restart.
3. Churner lifecycle is operator-driven via API/UI (spawn/kill/status/candidates).
4. AI regime context includes manifold/positions/throughput/churner blocks with updated prompt guidance.
5. Dashboard surfaces ops, MTS, simplex/ribbon visuals, and churner controls without JS errors.
6. New/updated tests for math, API routing, status contracts, and churner behavior pass.
