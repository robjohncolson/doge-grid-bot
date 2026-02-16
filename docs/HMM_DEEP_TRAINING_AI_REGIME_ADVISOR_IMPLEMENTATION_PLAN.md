# HMM Deep Training + AI Regime Advisor - Implementation Plan

Last updated: 2026-02-16
Parent specs:
- `docs/HMM_DEEP_TRAINING_SPEC.md` (Spec A, v0.1)
- `docs/AI_REGIME_ADVISOR_SPEC.md` (Spec B, v0.1)
Status: **IMPLEMENTATION COMPLETE** — all phases shipped, ready for staged rollout

## Goal

Implement deep HMM warm-up/training quality awareness first, then add an AI regime advisor layer that can recommend temporary overrides without blocking the main trading loop or weakening mechanical safety rails.

## Current Baseline (Code Audit)

1. `config.py` still defaults `HMM_TRAINING_CANDLES=720` and `HMM_SECONDARY_TRAINING_CANDLES=720`; deep-decay knobs do not exist.
2. `bot.py` exposes `hmm_data_pipeline` from `status_payload()` via `_hmm_data_readiness()`; this is separate from `_hmm_status_payload()`.
3. `bot.py` has no `training_depth`/quality-tier payload in `_hmm_status_payload()` and no training-quality confidence modifier in tier gating.
4. `_load_recent_ohlcv_rows()` already implements Supabase-first, Kraken-fallback, timestamp-deduped merge semantics.
5. `hmm_regime_detector.py` has no public transition-matrix getter and no built-in recency-decay policy.
6. `ai_advisor.py` currently supports council recommendations for generic market actions only; no regime-opinion API exists.
7. `bot.py` HTTP server uses `POST /api/action` action dispatch; no AI regime override/dismiss actions exist yet.
8. `dashboard.py` has HMM/Kelly/regime cards but no AI regime advisor card or deep-training progress bar.

## Locked Implementation Decisions

1. `training_depth` lives in `_hmm_status_payload()` (model quality), while `hmm_data_pipeline` remains a separate pipeline-health block from `status_payload()`.
2. API control path for AI regime actions will follow existing `POST /api/action` dispatch (`ai_regime_override`, `ai_regime_revert`, `ai_regime_dismiss`) instead of introducing a second endpoint style in v1.
3. AI regime LLM calls will run in a dedicated background worker thread with a bounded queue; main loop will only enqueue work and consume completed results.
4. Deep-decay Option A (resample) will be implemented in `bot.py` before `detector.train()` so recency policy stays in runtime orchestration, not inside model internals.
5. `RegimeDetector` will expose transition matrix via a read-only getter for context-building.
6. A rolling `regime_history_30m` buffer (input history) will be implemented in Workstream A so Workstream B can consume it directly.

## Implementation Order

1. Workstream A (Spec A) first.
2. Deploy/observe Workstream A for 48-72 hours.
3. Workstream B (Spec B) next, using Workstream A outputs.

## Workstream A - HMM Deep Training Window

### A0 - Config + Contracts + Loader Validation

Files:
- `config.py`
- `.env.example`

Changes:
1. Update defaults:
   - `HMM_TRAINING_CANDLES`: `720 -> 4000`
   - `HMM_SECONDARY_TRAINING_CANDLES`: `720 -> 1440`
2. Add:
   - `HMM_DEEP_DECAY_ENABLED` (default `False`)
   - `HMM_DEEP_DECAY_HALFLIFE` (default `1440`)
3. Add quality-tier contract:
   - tiers: `shallow`, `baseline`, `deep`, `full`
   - modifiers: `0.70`, `0.85`, `0.95`, `1.00`
4. Validation-only step:
   - confirm `_load_recent_ohlcv_rows()` behavior remains Supabase-first and no structural rewrite is required.

### A1 - Training Depth State + Confidence Modifier

Files:
- `bot.py`

Changes:
1. Add helpers to classify training depth from `(current_candles, target_candles)`.
2. Track training depth for primary and secondary HMM:
   - `current_candles`
   - `target_candles`
   - `quality_tier`
   - `confidence_modifier`
   - `pct_complete`
   - `estimated_full_at` (`null` when full)
3. Inject modifier at precise gating point in `_update_regime_tier()`:
   - after `_policy_hmm_signal()` returns raw confidence
   - compute `effective_confidence = raw_confidence * confidence_modifier`
   - use `effective_confidence` for tier threshold comparisons only
4. Preserve raw confidence for display/logging:
   - raw value stays in HMM state payload
   - effective value is exposed separately (diagnostic only)

### A2 - Warm-Up Payload + Dashboard + Regime Input History Buffer

Files:
- `bot.py`
- `dashboard.py`

Changes:
1. Add `training_depth` to `_hmm_status_payload()` (not to `hmm_data_pipeline`).
2. Dashboard HMM card:
   - training progress bar (`current/target`, percent, tier, ETA)
   - tier color coding per spec
3. Add `regime_history_30m` runtime buffer in `bot.py`:
   - rolling deque of `{ts, regime, conf}` samples
   - refreshed on each HMM update/eval cycle
   - exposed in status payload for observability and reused by AI context builder.

### A3 - Optional Deep Decay (Deferred by Default)

Files:
- `bot.py`

Changes:
1. Implement opt-in Option A resampling in training-candle preparation path:
   - decay formula per spec (`halflife` in candles)
   - duplicate newer observations more often than older observations
2. Keep default `HMM_DEEP_DECAY_ENABLED=false`.
3. Log both:
   - raw sample count
   - effective post-resample count

Note:
- This phase is intentionally deferred until post-A0/A1/A2 live observations show non-stationarity pain.

### A4 - Tests for Spec A

Files:
- `tests/test_hardening_regressions.py`
- new `tests/test_hmm_deep_training.py` (recommended)

Add coverage for:
1. Updated default training targets (4000/1440).
2. Quality-tier thresholds and modifiers.
3. Modifier injection behavior in `_update_regime_tier()` (raw vs effective confidence split).
4. `training_depth` payload schema and ETA null/full behavior.
5. `regime_history_30m` buffer population and 30-minute trimming.
6. Decay off-by-default and decay-on resample behavior.

## Workstream B - AI Regime Advisor (P0-P4)

### P0 - Opinion Engine + Context Builder + Transition Matrix Access

Files:
- `config.py`
- `.env.example`
- `ai_advisor.py`
- `hmm_regime_detector.py`
- `bot.py`

Changes:
1. Add config:
   - `AI_REGIME_ADVISOR_ENABLED`
   - `AI_REGIME_INTERVAL_SEC`
   - `AI_REGIME_DEBOUNCE_SEC`
   - `AI_OVERRIDE_TTL_SEC`
   - `AI_OVERRIDE_MAX_TTL_SEC`
   - `AI_OVERRIDE_MIN_CONVICTION`
   - `AI_REGIME_HISTORY_SIZE`
   - `AI_REGIME_PREFER_REASONING`
2. Add `RegimeDetector` getter for transition matrix:
   - returns 3x3 matrix (or `None` when unavailable/untrained)
   - read-only serialization-safe structure for prompt context
3. Add `get_regime_opinion()` in `ai_advisor.py`:
   - single-panelist path with preference order (Kimi -> Llama-70B -> Llama-8B)
   - strict JSON schema parse and clamped defaults
4. Build regime context payload including:
   - HMM state (1m/15m), consensus, transition matrix
   - training quality tier/modifier
   - `regime_history_30m` from Workstream A
   - directional trend, fills, recovery count, Kelly edges, capacity band/headroom

### P1 - Runtime Integration + Non-Blocking Advisor Worker + Disagreement Engine

Files:
- `bot.py`

Changes:
1. Add runtime state:
   - advisor timing (`last_run`, `last_trigger`)
   - latest opinion
   - rolling opinion history deque
   - dismiss state
   - override state (`tier`, `direction`, `until`, `applied_at`, `source_conviction`)
2. Add dedicated AI advisor worker thread:
   - bounded input queue (size 1, latest context wins)
   - output queue for completed opinions
   - main loop enqueues and returns immediately (no blocking network call)
3. Scheduler:
   - periodic trigger (`AI_REGIME_INTERVAL_SEC`)
   - event trigger on mechanical tier and consensus-agreement changes
   - hard debounce (`AI_REGIME_DEBOUNCE_SEC`)
4. Disagreement classification:
   - `agree`, `ai_upgrade`, `ai_downgrade`, `ai_flip`
5. Override application rules:
   - mechanical tier still computed every cycle
   - max one-tier hop
   - conviction floor
   - capacity `stop` gate blocks upgrades
   - TTL expiry auto-revert

### P2 - API + Dashboard UX

Files:
- `bot.py`
- `dashboard.py`

Changes:
1. Extend `POST /api/action` actions:
   - `ai_regime_override` (apply current opinion as override)
   - `ai_regime_revert` (cancel override)
   - `ai_regime_dismiss` (dismiss current disagreement)
2. Extend `/api/status` with `ai_regime_advisor` block:
   - run timing
   - current opinion
   - agreement type
   - override status/countdown
   - rolling opinion history
3. Dashboard AI Regime card states:
   - agree (neutral)
   - disagree (amber)
   - override active (orange)
4. Buttons:
   - Apply Override (gated by conviction threshold)
   - Dismiss
   - Revert to Mechanical

### P3 - Persistence + Restart Semantics

Files:
- `bot.py`

Changes:
1. Persist override fields in global snapshot.
2. Restore override on startup if TTL is still active.
3. Clear expired override on load and runtime checks.
4. Keep opinion history in-memory only.

### P4 - Tests + Prompt Tuning

Files:
- `tests/test_hardening_regressions.py`
- new `tests/test_ai_regime_advisor.py` (recommended)

Add coverage for:
1. `get_regime_opinion()` parse/validation failures.
2. Worker-thread non-blocking behavior and queue semantics.
3. Trigger/debounce logic.
4. Agreement classification matrix.
5. Conviction floor + one-tier-hop + capacity stop gates.
6. Override TTL expiry and manual revert.
7. `POST /api/action` AI regime actions.
8. `ai_regime_advisor` status payload schema.

## Cross-Cutting Acceptance Criteria

1. Cold start remains safe with Kraken-only initial training window.
2. Training quality climbs automatically from shallow to full as Supabase fills.
3. Regime-tier actuation uses effective confidence while preserving raw confidence reporting.
4. AI advisor never auto-applies an override.
5. Main loop remains non-blocking during AI calls.
6. Mechanical tier remains active and visible during overrides.
7. Override is always bounded by TTL and can be cancelled immediately.

## Commit History

1. **Commit 1: A0 + A1** — config defaults (4000/1440), quality tiers, confidence modifier injection. ✅ DONE
2. **Commit 2: A2** — training_depth payload, dashboard progress bar, regime_history_30m buffer. Code nits fixed. ✅ DONE
3. **Commit 3: P0** — get_regime_opinion(), _build_regime_context(), transmat getter, 8 config vars, tests. ✅ DONE
4. **Commit 4: P1** — threaded worker, scheduler (periodic + event triggers + debounce), override application with all 5 safety rails, TTL-on-load fix, status payload. ✅ DONE
5. **Commit 5: P2 + P3** — /api/action dispatch (override/revert/dismiss), dashboard AI Regime card (agree/disagree/override states), persistence, confirmation dialogs. Test message assertions relaxed. ✅ DONE
6. **A3 (decay resampling)** — config knobs in place, implementation deferred until live data shows need.
7. **P4 (prompt tuning)** — ongoing, post-rollout.

## Rollout Plan

### Stage 1 - Deep Training Only

1. Deploy A0/A1/A2 with `HMM_DEEP_DECAY_ENABLED=false`.
2. Observe `training_depth`, tier stability, and startup behavior for 48-72 hours.

### Stage 2 - Optional Decay

1. If needed, canary-enable `HMM_DEEP_DECAY_ENABLED=true` on one runtime.
2. Compare regime stability before broader rollout.

### Stage 3 - AI Advisor Observe-Only

1. Deploy P0/P1/P2/P3 with `AI_REGIME_ADVISOR_ENABLED=true`.
2. Keep overrides unused initially; audit agreement/disagreement quality.

### Stage 4 - Manual Override Operations

1. Begin operator use of Apply Override.
2. Monitor disagreement-to-override ratio, expiry, and revert behavior.
3. Adjust conviction floor and interval only from observed data.

## Rollback

1. Disable AI advisor:
   - `AI_REGIME_ADVISOR_ENABLED=false`
2. Disable deep decay:
   - `HMM_DEEP_DECAY_ENABLED=false`
3. Revert to shallow targets if needed:
   - `HMM_TRAINING_CANDLES=720`
   - `HMM_SECONDARY_TRAINING_CANDLES=720`
4. If code rollback is required, revert:
   - `config.py`
   - `.env.example`
   - `hmm_regime_detector.py`
   - `ai_advisor.py`
   - `bot.py`
   - `dashboard.py`
   - related tests

## Estimated Change Footprint

| File | Workstream | Estimated Delta |
|---|---|---|
| `config.py` | A + B | ~35-55 lines |
| `.env.example` | A + B | ~20-35 lines |
| `hmm_regime_detector.py` | B (matrix getter) | ~15-35 lines |
| `bot.py` | A + B | ~260-390 lines |
| `ai_advisor.py` | B | ~150-240 lines |
| `dashboard.py` | A + B | ~170-260 lines |
| `tests/*` | A + B | ~260-420 lines |

Total expected delta: approximately 910-1,435 lines including tests and UI.
