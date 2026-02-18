# Signal Digest - Implementation Plan

Last updated: 2026-02-18
Parent spec: `docs/SIGNAL_DIGEST_SPEC.md` v0.1
Status: **Ready for implementation (with clarifications below)**

## Goal

Implement a two-layer Signal Digest system that turns the existing high-volume telemetry into:
1. A per-loop diagnostic traffic light (`green`/`amber`/`red`) with ranked concerns.
2. A periodic plain-English market interpretation generated via existing AI provider infrastructure.

## Scope

In scope:
1. Rule-engine module for 12 diagnostic checks.
2. Runtime integration in `bot.py` (evaluation, scheduling, storage, status payload, manual trigger).
3. HMM observation exposure needed by digest rules.
4. LLM interpretation flow (DeepSeek primary, Groq fallback) using existing `ai_advisor.py` patterns.
5. Dashboard digest card + check table + interpretation metadata.
6. Config and environment contract (`DIGEST_*`).
7. Unit/regression tests.

Out of scope:
1. Any order placement/state transition/sizing behavior changes.
2. Replacing existing HMM/manifold/throughput systems.
3. New external dependencies.

## Current Baseline (Code Audit)

1. No digest implementation exists yet (`signal_digest` symbols are absent outside spec docs).
2. `bot.py` has mature scheduling patterns for async AI (`_ai_regime_worker`, `_maybe_schedule_ai_regime`) that can be reused for Layer 2.
3. `bot.py` already publishes all major ingredients in `status_payload()` (HMM, belief, manifold, throughput, self-healing, rangers, capacity), but no unified digest block.
4. `hmm_regime_detector.py` computes MACD/EMA/RSI/volume observations but does not persist a last observation for runtime consumers.
5. `ai_advisor.py` already has provider chain + cooldown handling + parse hardening (`</think>` stripping) for regime calls.
6. `dashboard.py` has no digest card yet, but has a centralized `renderAll()` path and existing card patterns (AI regime advisor, manifold, self-healing) suitable for extension.

## Clarifications / Decisions

1. Observation availability before HMM training:
`RegimeDetector.update()` currently returns early when model is not trained. Digest still needs MACD/EMA/RSI values, so observation capture must happen even in pre-train mode.
Decision: always compute/capture latest observation row before trained/inference gating.

2. Missing-data behavior for rules:
Some signals may be temporarily unavailable (startup, disabled subsystem).
Decision: rules return green/N-A style detail when data is absent unless the missing signal itself is risk-relevant (for example, no headroom telemetry while trading), which returns amber.

3. Top concern selection:
Only amber/red checks compete for top concern; if all checks are green, top concern is a stable neutral string (for example, `"All diagnostic checks nominal"`).

4. Digest persistence:
To avoid empty UI after restart, persist last digest light/checks/interpretation in runtime snapshot, with defensive backward-compatible restore.

## Implementation Phases

## Phase A - Core Module (Original 1 + 2 + 3)

Files:
- `config.py`
- `.env.example`
- `hmm_regime_detector.py`
- `signal_digest.py` (new)

Changes:
1. Add full `DIGEST_*` env/config contract (enable flags + thresholds + interpreter cadence knobs).
2. Expose latest HMM observation values (MACD slope, EMA spread, RSI zone, volume ratio) on every update call, including pre-train mode.
3. Build `signal_digest.py` as a pure rules module:
   - dataclasses (`DiagnosticCheck`, `MarketInterpretation`, digest result wrapper)
   - 12 rule implementations with spec thresholds and trading-aware detail strings
   - deterministic severity + priority sorting
   - reducers for overall light and top concern
4. Keep this phase isolated from `bot.py` wiring so rule behavior can be validated in isolation.

Acceptance checks:
1. Boundary classification tests pass for all 12 rules.
2. Observation snapshot is available even when HMM model is not yet trained.
3. Module is side-effect free and imports cleanly.

## Phase B - Bot + API (Original 4 + 6)

Files:
- `bot.py`

Changes:
1. Add digest runtime state in `Runtime.__init__` (light, checks, top concern, timestamps, interpretation placeholders).
2. Add `_run_signal_digest(now)` and context builder methods using existing runtime state/payload helpers.
3. Invoke digest evaluation in `run_loop_once()` after inputs are fresh (self-healing/rangers/capacity/throughput/manifold already updated).
4. Add `signal_digest` block in `/api/status` payload:
   - `light`, `light_changed_at`, `top_concern`
   - ordered check rows
   - interpretation fields + `age_sec` + `interpretation_stale`
5. Add manual trigger path for interpretation (`POST /api/digest/interpret` or equivalent action route).
6. Persist/restore digest fields in `_global_snapshot()` and `_load_snapshot()` with backward-compatible defaults.

Acceptance checks:
1. Digest works with `DIGEST_ENABLED=true` and interpretation disabled.
2. `/api/status` always contains a safe `signal_digest` object.
3. Restart restores digest light/check context without schema breaks.

## Phase C - Dashboard + Tests (Original 7 + 8)

Files:
- `dashboard.py`
- `tests/test_signal_digest.py` (new)
- `tests/test_hardening_regressions.py`
- `tests/test_ai_regime_advisor.py` (only if digest parsing/provider utilities live there)

Changes:
1. Add Signal Digest panel UI:
   - traffic light + top concern
   - narrative/watch section
   - sorted check rows
   - interpretation age/provider/staleness indicator
2. Add CSS classes for green/amber/red light and stale dimming.
3. Extend render path (`renderTop`/`renderAll`) with null-safe mapping from `signal_digest`.
4. Add test coverage:
   - rule thresholds + ordering + top-concern priority
   - HMM observation exposure regression
   - status payload contract for digest
   - dashboard DOM marker smoke checks
   - scheduling/debounce/stale fallback tests as applicable to enabled layers

Verification run:
1. `python3 -m unittest tests.test_signal_digest`
2. `python3 -m unittest tests.test_hardening_regressions`
3. `python3 -m unittest tests.test_ai_regime_advisor`

Acceptance checks:
1. Dashboard renders digest correctly for full and partial payloads.
2. Test suite is green with no regressions.

## Phase D - LLM Interpreter (Original 5, Deferred)

Files:
- `ai_advisor.py`
- `bot.py`
- tests (same files as Phase C where relevant)

Changes:
1. Add digest-specific interpreter chain (DeepSeek primary, Groq fallback) reusing existing provider cooldown/error patterns.
2. Add digest prompt builder + response parser:
   - structured JSON path first
   - heuristic extraction fallback for plain text
3. Add async digest worker/scheduler in `bot.py`:
   - periodic interval trigger
   - light-change event trigger
   - debounce protection
4. Preserve prior interpretation on failure and expose stale metadata cleanly.

Acceptance checks:
1. Interpreter does not impact Layer 1 operation on failures/timeouts.
2. Triggering behavior matches spec and debounce constraints.
3. Layer 2 can be toggled independently via `DIGEST_INTERPRETATION_ENABLED`.

## Rollout Plan

1. Complete and deploy Phase A + B + C first with `DIGEST_INTERPRETATION_ENABLED=false`.
2. Run a rule-only soak (24-48h) and calibrate thresholds from live telemetry.
3. Enable Phase D in production by setting `DIGEST_INTERPRETATION_ENABLED=true`.
4. Monitor provider errors, debounce behavior, interpretation staleness, and request volume.

## Rollback Plan

1. Set `DIGEST_INTERPRETATION_ENABLED=false` to disable LLM layer only.
2. Set `DIGEST_ENABLED=false` to disable all digest logic while leaving code deployed.
3. If needed, hide dashboard card when payload disabled/missing (already supported by render guards).

## Definition of Done

1. `/api/status` exposes a stable `signal_digest` object with ordered checks and traffic light.
2. Traffic-light transitions are deterministic and reflect rule severities.
3. Interpretation refreshes on schedule and on light changes, with stale fallback on failures.
4. Dashboard shows digest card and staleness clearly.
5. New tests pass and no regressions in existing hardening suite.
