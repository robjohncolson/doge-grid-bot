# Bayesian Intelligence - Implementation Plan

Last updated: 2026-02-17
Parent spec: `docs/BAYESIAN_INTELLIGENCE_SPEC.md` v0.1 (2026-02-17)
Status: **Plan only (no implementation in this document)**

## Goal

Ship the Bayesian Intelligence stack incrementally so each phase is independently useful, independently toggleable, and safe to roll back without touching `state_machine.py`.

## Scope

In scope:
1. Phase 0 instrumentation for posterior snapshots, entropy, and transition risk logging.
2. Phase 1 continuous belief signals and BOCPD-based structural break detection.
3. Phase 2 feature enrichment with private microstructure signals.
4. Phase 3 survival modeling for per-trade fill probabilities.
5. Phase 4 per-trade belief tracking and evidence-driven exit actions.
6. Phase 5 continuous action knobs replacing discrete tier templates.
7. Status payload and dashboard surfacing for all enabled phases.
8. Test coverage, staged rollout, and rollback paths.

Out of scope:
1. Changes to `state_machine.py` reducer behavior.
2. Kraken API/order-type redesign.
3. Particle filters, HDP-HMM, neural approximators, online EM, contextual bandits (future phases).

## Current Baseline Snapshot

1. Existing files present: `bot.py`, `config.py`, `hmm_regime_detector.py`, `grid_strategy.py`, `dashboard.py`, `throughput_sizer.py`, `state_machine.py`.
2. Planned new files do not yet exist: `bayesian_engine.py`, `bocpd.py`, `survival_model.py`.
3. Existing test suite contains related infrastructure tests, including `tests/test_hardening_regressions.py`.

## Non-Negotiable Guardrails

1. `state_machine.py` remains unchanged.
2. Every phase is feature-flagged and fails gracefully to current behavior.
3. Timer backstops remain active even when belief-driven actions are enabled.
4. No new non-standard dependencies; implementation stays in Python + `numpy`/existing stack.
5. Phase order is respected so data collection starts before model-dependent behavior.
6. Belief tracker and action knobs never bypass existing min-volume guards (`compute_order_volume()` returning `None` still blocks placement).
7. BOCPD triggers regime re-evaluation only — it never directly causes order placement, cancellation, or modification.
8. When `capacity_band == "stop"`, action knob suppression strength is forced to 0 (symmetric) regardless of belief signals.

## Implementation Sequence

## Phase 0 - Instrumentation (Zero Behavior Change)

Objective:
1. Begin collecting high-value training data immediately, with no runtime trading behavior changes.

Files:
1. `grid_strategy.py`
2. `bot.py`
3. `hmm_regime_detector.py`
4. `dashboard.py`
5. `config.py`

Work items:
1. Extend completed-cycle payloads with entry-time posterior snapshots (`1m/15m/1h`), `entropy_at_entry`, `p_switch_at_entry`, and `confidence_at_entry` (see spec §5.1 for field list).
2. Add entry and exit posterior/entropy/`p_switch` columns to the Supabase `exit_outcomes` table (10 new nullable columns — see spec §5.2 for full schema). Guard with column auto-detection so missing columns are silently skipped (same pattern as existing `trade_id`/`cycle` detection).
3. Add `compute_entropy(posterior)` and `compute_p_switch(posterior, transmat)` as static methods on `RegimeDetector` in `hmm_regime_detector.py` (see spec §5.3–5.4 for formulas).
4. Add `belief_state` block to status payload (see spec §5.5 for full field list: 16 fields including per-timeframe entropy/p_switch, consensus values, direction_score, boundary_risk).
5. Add dashboard belief-state card rendering (see spec §5.6 for layout and color coding).
6. Add config toggles:
   - `BELIEF_STATE_LOGGING_ENABLED`
   - `BELIEF_STATE_IN_STATUS`

Acceptance checks:
1. Completed cycles include new belief-state fields.
2. Status includes `belief_state` when enabled.
3. Disabling both toggles reverts to pre-phase payload behavior.
4. Trading decisions are unchanged when only Phase 0 is enabled.

## Phase 1 - Continuous Belief Signals + BOCPD

Objective:
1. Replace threshold input confidence with entropy-derived confidence and add structural break probability for adaptive regime evaluation.

Files:
1. `bocpd.py` (new)
2. `bot.py`
3. `hmm_regime_detector.py`
4. `config.py`
5. `dashboard.py`

Work items:
1. Introduce `BOCPDState` dataclass (spec §6.4: `change_prob`, `run_length_mode`, `run_length_mode_prob`, `last_update_ts`, `observation_count`, `alert_active`, `alert_triggered_at`) and online update loop in `bocpd.py`. Use Normal-Inverse-Gamma conjugate prior for the observation model (spec §6.2).
2. Integrate BOCPD updates on feature stream updates.
3. Adapt regime evaluation cadence based on `change_prob` thresholds.
4. Wire entropy-derived confidence into tier gating path (backward-compatible thresholds).
5. Add status payload `bocpd` block and dashboard BOCPD indicator.
6. Add config:
   - `BOCPD_ENABLED`
   - `BOCPD_EXPECTED_RUN_LENGTH`
   - `BOCPD_ALERT_THRESHOLD`
   - `BOCPD_URGENT_THRESHOLD`
   - `BOCPD_MAX_RUN_LENGTH`
   - `REGIME_EVAL_INTERVAL_FAST`

Acceptance checks:
1. Stationary data keeps `change_prob` low.
2. Synthetic mean-shift data triggers BOCPD spikes.
3. Regime eval interval accelerates only while alerts are active.
4. BOCPD disable flag fully restores fixed-interval behavior.
5. BOCPD state round-trips through snapshot save/restore without loss.
6. BOCPD never directly causes order placement or cancellation (only triggers regime re-evaluation).

## Phase 2 - Feature Enrichment

Objective:
1. Add private microstructure features to improve belief quality and downstream modeling.

Files:
1. `bot.py`
2. `hmm_regime_detector.py`
3. `config.py`

Work items:
1. Compute and maintain runtime metrics for:
   - `fill_imbalance`
   - `spread_realization`
   - `fill_time_derivative`
   - `congestion_ratio`
2. Extend HMM observation vector from 4D to 8D when feature flag is on.
3. Preserve neutral cold-start defaults until enough runtime history exists.
4. Ensure retrain path handles enriched vectors.
5. Add config:
   - `ENRICHED_FEATURES_ENABLED`
   - `FILL_IMBALANCE_WINDOW_SEC`
   - `FILL_TIME_DERIVATIVE_SHORT_SEC`
   - `FILL_TIME_DERIVATIVE_LONG_SEC`

Acceptance checks:
1. Enriched mode outputs 8D vectors; disabled mode remains 4D.
2. Cold-start values are neutral and do not force action changes.
3. Retraining succeeds after toggling enriched features on.

## Phase 3 - Survival Model

Objective:
1. Predict `P(fill within horizon | features)` with censoring support to replace timer-only assumptions.

Files:
1. `survival_model.py` (new)
2. `bot.py`
3. `config.py`
4. `dashboard.py`

Work items:
1. Define `FillObservation` dataclass (spec §8.2: `duration_sec`, `censored`, `regime_at_entry`, `regime_at_exit`, `side`, `distance_pct`, `posterior_1m/15m/1h`, `entropy_at_entry`, `p_switch_at_entry`, `fill_imbalance`, `congestion_ratio`).
2. Define `SurvivalConfig` dataclass (spec §8.6: `min_observations`, `min_per_stratum`, `synthetic_weight`, `horizons`).
3. Define `SurvivalPrediction` dataclass (spec §8.6: `p_fill_30m`, `p_fill_1h`, `p_fill_4h`, `median_remaining`, `hazard_ratio`, `model_tier`, `confidence`).
4. Implement Tier 1 Kaplan-Meier stratified baseline (6 strata: regime_at_entry × side). Fall back to Kaplan-Meier when Cox has insufficient data (N < `SURVIVAL_MIN_OBSERVATIONS`).
5. Implement Tier 2 Cox PH model with partial likelihood maximization and prediction interface.
6. Add optional synthetic HMM-sampled observations via `hmmlearn`'s `sample()`. Weight synthetic data at `SURVIVAL_SYNTHETIC_WEIGHT` (default 0.3). Verify synthetic fills cover all 6 regime × side strata.
7. Retrain survival model on daily cadence aligned to HMM retraining cadence.
8. Surface model health, strata counts, and Cox coefficients in `survival_model` status payload block (spec §14) and dashboard card (spec §15).
7. Add config:
   - `SURVIVAL_MODEL_ENABLED`
   - `SURVIVAL_MODEL_TIER`
   - `SURVIVAL_MIN_OBSERVATIONS`
   - `SURVIVAL_MIN_PER_STRATUM`
   - `SURVIVAL_SYNTHETIC_ENABLED`
   - `SURVIVAL_SYNTHETIC_WEIGHT`
   - `SURVIVAL_SYNTHETIC_PATHS`
   - `SURVIVAL_HORIZONS`
   - `SURVIVAL_LOG_PREDICTIONS`

Acceptance checks:
1. Survival curves are monotonic.
2. Censoring changes estimates as expected versus uncensored-only runs.
3. Predictions degrade to safe defaults below minimum observation thresholds. Cox falls back to Kaplan-Meier when N is insufficient for stable regression.
4. `distance_pct` sensitivity is directionally correct (larger distance -> lower near-term fill probability).
5. Synthetic fills cover all 6 regime × side strata.
6. `SurvivalPrediction` fields match spec §8.6 contract.

## Phase 4 - Per-Trade Belief Tracker

Objective:
1. Maintain per-exit belief state and choose evidence-based actions (`hold/tighten/widen/reprice_breakeven`).

Files:
1. `bayesian_engine.py` (new)
2. `bot.py`
3. `grid_strategy.py`
4. `config.py`
5. `dashboard.py`

Work items:
1. Define `TradeBeliefState` contract with entry snapshot, current belief, survival predictions, and derived metrics.
2. Compute regime-agreement score from entry vs current 9D posterior.
3. Compute expected value including configurable opportunity cost.
4. Implement action-mapping logic with hysteresis and confidence gates.
5. Integrate with existing timer system as layered override, not replacement.
6. Enforce widen safeguards (count cap, total widen cap, S2 exclusion).
7. Add tracker telemetry in status payload and slot-level dashboard badges.
8. Add config:
   - `BELIEF_TRACKER_ENABLED`
   - `BELIEF_UPDATE_INTERVAL_SEC`
   - `BELIEF_OPPORTUNITY_COST_PER_HOUR`
   - `BELIEF_TIGHTEN_THRESHOLD_PFILL`
   - `BELIEF_TIGHTEN_THRESHOLD_EV`
   - `BELIEF_IMMEDIATE_REPRICE_AGREEMENT`
   - `BELIEF_IMMEDIATE_REPRICE_CONFIDENCE`
   - `BELIEF_WIDEN_ENABLED`
   - `BELIEF_WIDEN_STEP_PCT`
   - `BELIEF_MAX_WIDEN_COUNT`
   - `BELIEF_MAX_WIDEN_TOTAL_PCT`
   - `BELIEF_TIMER_OVERRIDE_MAX_SEC`
   - `BELIEF_EV_TREND_WINDOW`
   - `BELIEF_LOG_ACTIONS`

Acceptance checks:
1. Tracker-disabled mode exactly matches timer-only behavior.
2. Timer backstop always fires by `BELIEF_TIMER_OVERRIDE_MAX_SEC`.
3. Immediate-reprice path triggers only under intended disagreement + confidence conditions.
4. Widen path never runs in S2 and never exceeds configured limits.
5. Belief tracker never bypasses min-volume guards (`compute_order_volume()` returning `None` still blocks placement).
6. `suppression_strength` = 1.0 for extreme directional + high confidence inputs (ceiling test).
7. Derived tier label maps correctly: suppression > 0.8 → tier 2, > 0.2 → tier 1, else → tier 0.

## Phase 5 - Continuous Action Knobs

Objective:
1. Replace binary tier templates with continuous knobs while preserving a derived display tier.

Files:
1. `bayesian_engine.py`
2. `bot.py`
3. `throughput_sizer.py`
4. `dashboard.py`
5. `config.py`

Work items:
1. Compute continuous signals: direction, confidence, boundary, volatility, congestion (see spec §10.1 for formulas).
2. Map signals to knob outputs (see spec §10.2 for all formulas):
   - aggression multiplier
   - spacing multiplier (+ per-side asymmetry based on direction_score)
   - reprice cadence multiplier
   - suppression strength ramp (continuous 0–1, not binary)
3. Feed aggression into throughput sizing path.
4. Feed spacing/cadence/suppression into runtime engine config generation.
5. Derive informational tier label from knob state for dashboard compatibility (spec §10.3).
6. Add `action_knobs` block to `/api/status` payload (spec §14: `aggression`, `spacing_mult`, `spacing_a`, `spacing_b`, `cadence_mult`, `suppression_strength`, `derived_tier`, `derived_tier_label`).
7. When `capacity_band == "stop"`, force `suppression_strength = 0` regardless of belief signals (spec safety invariant #4).
6. Add config:
   - `KNOB_MODE_ENABLED`
   - `KNOB_AGGRESSION_DIRECTION`
   - `KNOB_AGGRESSION_BOUNDARY`
   - `KNOB_AGGRESSION_CONGESTION`
   - `KNOB_AGGRESSION_FLOOR`
   - `KNOB_AGGRESSION_CEILING`
   - `KNOB_SPACING_VOLATILITY`
   - `KNOB_SPACING_BOUNDARY`
   - `KNOB_SPACING_FLOOR`
   - `KNOB_SPACING_CEILING`
   - `KNOB_ASYMMETRY`
   - `KNOB_CADENCE_BOUNDARY`
   - `KNOB_CADENCE_ENTROPY`
   - `KNOB_CADENCE_FLOOR`
   - `KNOB_SUPPRESS_DIRECTION_FLOOR`
   - `KNOB_SUPPRESS_SCALE`

Acceptance checks:
1. Knobs clamp to configured floors/ceilings.
2. Suppression strength ramps continuously instead of jumping.
3. `suppression_strength` = 0 when `|direction_score|` < `KNOB_SUPPRESS_DIRECTION_FLOOR`.
4. `suppression_strength` = 1.0 for extreme directional + high confidence.
5. `capacity_band == "stop"` forces `suppression_strength = 0` regardless of signals.
6. `KNOB_MODE_ENABLED=False` preserves existing tier 0/1/2 runtime behavior.
7. `/api/status` includes `action_knobs` block with expected fields.
8. Dashboard displays knob values and derived tier coherently.

## Cross-Phase Dependencies

1. Phase 0 must ship before Phase 3/4 for high-quality training data.
2. Phase 1 and Phase 2 can progress independently after Phase 0.
3. Phase 3 requires sufficient logged fills from Phase 0 data collection.
4. Phase 4 depends on Phase 3 predictions for full functionality, but must degrade to timer-safe defaults.
5. Phase 5 can start after Phase 1 belief vector outputs exist; it should not require Phase 4.

## File-Level Delivery Plan

New files:
1. `bayesian_engine.py`
2. `bocpd.py`
3. `survival_model.py`
4. `tests/test_bayesian_intelligence.py` (recommended)
5. `tests/test_survival_model.py` (recommended)
6. `tests/test_bocpd.py` (recommended)

Modified files:
1. `config.py`
2. `bot.py`
3. `hmm_regime_detector.py`
4. `grid_strategy.py`
5. `dashboard.py`
6. `throughput_sizer.py`

## Test Plan

Unit tests:
1. Entropy and `p_switch` correctness at known probability/transition-matrix edges (spec tests #1–4).
2. BOCPD stability on stationary series and sensitivity on abrupt shifts (spec tests #7–8).
3. Enriched-feature calculations, bounds, and cold-start defaults (spec tests #11–13).
4. Kaplan-Meier monotonicity and censoring behavior (spec tests #15–16).
5. Cox fit/predict sanity and safe fallback to KM when N insufficient (spec tests #17, #20).
6. Synthetic fills cover all 6 regime × side strata (spec test #18).
7. Knob mapping clamp and monotonicity: `suppression_strength` = 0 below floor, = 1.0 at extreme inputs (spec tests #28–30).
8. Derived tier label maps correctly for given knob values (spec test #31).
9. Regime-agreement (cosine similarity) and EV calculations (spec tests #21, #23).

Regression tests:
1. All new phase toggles disabled -> behavior and payloads remain backward compatible.
2. Timer backstop behavior unchanged under tracker-disable and low-confidence conditions.
3. Existing throughput sizing and slot lifecycle tests continue to pass.

Integration tests:
1. End-to-end path from candle/feature update -> belief state -> action recommendation.
2. Snapshot save/load round-trip for BOCPD/survival/trade-belief state.
3. Status payload schema includes all enabled blocks with expected shapes.

Suggested test execution order:
1. `python3 -m unittest tests.test_bocpd`
2. `python3 -m unittest tests.test_survival_model`
3. `python3 -m unittest tests.test_bayesian_intelligence`
4. `python3 -m unittest tests.test_hardening_regressions`

## Rollout Plan

Stage A (Phase 0 live, observe-only):
1. Enable belief-state logging and status payload.
2. Run until enough posterior-linked completed cycles accumulate.
3. Duration: indefinite (runs in background while subsequent stages develop). Minimum 3 days before advancing to Stage B.

Stage B (Phase 1 shadow):
1. Prerequisite: 3+ days of Phase 0 data accumulated.
2. Enable BOCPD.
3. Monitor change-probability quality and evaluation cadence impacts.
4. Duration: minimum 48h observation. Validate `change_prob` spikes correlate with actual regime transitions.

Stage C (Phase 2 guarded):
1. Enable enriched features.
2. Force HMM retrain after enabling.
3. Compare regime quality and runtime stability.
4. Duration: minimum 48h observation with before/after regime quality comparison.

Stage D (Phase 3 shadow):
1. Prerequisite: ~200+ completed fills with posterior data logged (from Phase 0).
2. Enable survival model and optional synthetic augmentation.
3. Log predictions, calibration, and coefficient sanity without acting on them.
4. Duration: minimum 72h shadow with calibration analysis (when model says P(fill_1h)=0.7, do ~70% actually fill?).

Stage E (Phase 4 conservative live):
1. Enable belief tracker with conservative tighten thresholds.
2. Keep widen disabled initially.
3. Duration: minimum 1 week. Gradually tune thresholds based on observed belief-vs-timer comparison.

Stage F (Phase 5 conservative live):
1. Enable knob mode with coefficients close to legacy behavior (small deviations from tier templates).
2. Tune suppression/aggression/spacing gradually.
3. Duration: ongoing tuning.

Stage G (optional widen):
1. Enable widen with strict initial caps: `BELIEF_MAX_WIDEN_COUNT=1`, `BELIEF_MAX_WIDEN_TOTAL_PCT=0.002` (0.2%).
2. Monitor orphan/churn impacts before relaxing limits.

## Rollback Plan

1. Disable each phase via its master toggle, independently.
2. Phase 2 rollback requires reverting to 4D feature mode and retraining HMM.
3. No migration-required rollback is expected because all structures are additive.

## Risks and Mitigations

1. **Miscalibrated survival predictions causing poor actions.**
Mitigation: shadow stage first (Stage D, 72h), confidence gating, timer backstop always active.

2. **BOCPD false positives increasing evaluation/API pressure.**
Mitigation: thresholds, fast-interval floor (60s), alert hysteresis.

3. **Feature enrichment destabilizing HMM training.**
Mitigation: separate flag, cold-start neutral defaults, immediate rollback path.

4. **Belief tracker churn from overreactive action switching.**
Mitigation: update interval controls, action hysteresis, conservative initial thresholds.

5. **Overly aggressive knob tuning causing behavior drift.**
Mitigation: start near legacy values, clamp all knobs, monitor derived-tier and action-frequency metrics. Document recommended tuning order: aggression first, then spacing, then cadence, then suppression.

6. **Belief widen causing exits to never fill.**
Mitigation: bounded by `BELIEF_MAX_WIDEN_TOTAL_PCT` (default 0.5%). Maximum widen count per trade (`BELIEF_MAX_WIDEN_COUNT`, default 2). Never widen during S2. Timer backstop always runs. Disabled by default; Stage G starts with `MAX_WIDEN_TOTAL_PCT=0.002`.

7. **Survival model overfits to synthetic data.**
Mitigation: synthetic weight capped at `SURVIVAL_SYNTHETIC_WEIGHT` (default 0.3). Real data always dominates when available. Monitor calibration (Brier score) separately for real-only vs. blended predictions.

8. **Cox regression numerically unstable with small N.**
Mitigation: automatic fallback to Kaplan-Meier when N < `SURVIVAL_MIN_OBSERVATIONS`. Cox activation requires minimum observation threshold per stratum. Log warnings when fitting is unstable.

9. **Complexity budget — too many interacting systems.**
Mitigation: each phase is independently valuable and independently disableable via feature flag. No phase requires a later phase. Clear dependency ordering in Cross-Phase Dependencies section. Rollback of any single phase has zero impact on others.

## Definition of Done

1. All five phases implemented behind flags with safe defaults off (except Phase 0 logging, if approved for always-on).
2. Unit, regression, and integration test suites pass.
3. Shadow-stage metrics validate BOCPD and survival calibration before live actions.
4. Dashboard/status provide operational visibility for each enabled component.
5. Verified rollback by disabling each phase toggle without runtime errors.
