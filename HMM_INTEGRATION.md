# HMM Integration Contract (Primary + Secondary + Tertiary)

Last updated: 2026-02-18
Primary references: `bot.py`, `hmm_regime_detector.py`, `doge-hmm/src/lib.rs`, `doge-hmm/src/regime.rs`, `config.py`

## 1. Scope and Invariants

The HMM subsystem is an advisory layer in runtime (`bot.py`).

Hard invariants:

1. Reducer semantics remain unchanged (`state_machine.py` / Haskell parity contract).
2. HMM outputs can influence policy knobs, never reducer transition rules directly.
3. Failure degrades to neutral behavior (RANGING/low-confidence), not hard-stop.

## 2. Runtime Topology

Three detector streams can be active:

1. Primary (base interval, default 1m)
2. Secondary (default 15m, gated by `HMM_MULTI_TIMEFRAME_ENABLED`)
3. Tertiary (default 1h, gated by `HMM_TERTIARY_ENABLED`)

`bot.py` maintains per-stream state plus consensus output.

## 3. Training Depth and Quality Tiers

`bot.py` tracks per-stream depth metadata via `_update_hmm_training_depth(...)`:

- `current_candles`
- `target_candles`
- `min_train_samples`
- `quality_tier`
- `confidence_modifier`
- ETA/percent completion

Tier buckets:

1. `shallow` -> `0.70`
2. `baseline` -> `0.85`
3. `deep` -> `0.95`
4. `full` -> `1.00`

These modifiers scale effective confidence before policy decisions.

## 4. Confidence Modifier Pipeline

Runtime confidence flow:

1. Raw confidence from selected source stream (`primary` / `secondary` / `tertiary` / `consensus`)
2. Source-specific modifier selection
3. `confidence_effective = confidence_raw * confidence_modifier`
4. Clamp to `[0, 1]`

In Python runtime (`bot.py`): `_hmm_confidence_modifier_for_source(...)` currently applies:

- primary mode: primary modifier
- consensus mode: `min(primary_modifier, secondary_modifier)`

In Rust module (`doge_hmm`): `confidence_modifier_for_source(...)` supports:

- `primary`
- `secondary` / `15m`
- `tertiary` / `1h`
- `consensus` / `consensus_min`

## 5. Tertiary Transition Tracking

Tertiary transition metadata is tracked in runtime and exposed in status/snapshots:

- `from_regime`
- `to_regime`
- `confirmation_count`
- `confirmed`
- `changed_at`
- `transition_age_sec`

Confirmation behavior:

1. Transition starts when tertiary regime changes.
2. Confirmation count accrues by tertiary candle age.
3. `confirmed` flips once count >= `ACCUM_CONFIRMATION_CANDLES` (default `2`) and regime actually changed.

Rust parity object:

- `TertiaryTransition` PyO3 class with dict round-trip support.

## 6. Backend Surface

### 6.1 Python module (`hmm_regime_detector.py`)

Provides:

- `Regime`, `RegimeState`, `RegimeDetector`
- `compute_blended_idle_target(...)`
- `compute_grid_bias(...)`
- `serialize_for_snapshot(...)`, `restore_from_snapshot(...)`

### 6.2 Rust module (`doge_hmm`)

PyO3 exports:

- `Regime`, `RegimeState`, `RegimeDetector`, `TertiaryTransition`
- `compute_blended_idle_target(...)`
- `compute_grid_bias(...)`
- `serialize_for_snapshot(...)`, `restore_from_snapshot(...)`
- `confidence_modifier_for_source(...)`

Rust `RegimeDetector` additionally exposes:

- `training_depth`
- `quality_tier()`
- `confidence_modifier()`
- `tertiary_transition` getter/setter

## 7. Snapshot Contract

HMM snapshot payloads include base keys:

- `_hmm_regime_state`
- `_hmm_last_train_ts`
- `_hmm_trained`

Extended parity keys (Rust surface):

- `_hmm_training_depth`
- `_hmm_quality_tier`
- `_hmm_confidence_modifier`
- `_hmm_tertiary_transition`

`bot.py` additionally persists runtime-managed HMM fields:

- `hmm_state_secondary`, `hmm_state_tertiary`, `hmm_consensus`
- `hmm_backfill_*` for primary/secondary/tertiary
- `hmm_tertiary_transition`

Backward-compat policy: absent keys default to safe neutral values.

## 8. Data Pipeline

Primary/secondary/tertiary OHLCV streams are synchronized independently and can backfill on startup.

Relevant config groups:

1. Base HMM: `HMM_*`
2. Secondary: `HMM_SECONDARY_*`
3. Tertiary: `HMM_TERTIARY_*`
4. Consensus: `CONSENSUS_*`

Readiness/freshness telemetry is exposed in status endpoints and dashboard payloads.

## 9. Operational Modes

Recommended rollout:

1. Start with `HMM_ENABLED=false` (trend-only path)
2. Enable primary HMM and verify readiness + training depth progression
3. Enable secondary multi-timeframe consensus
4. Enable tertiary and watch transition confirmation behavior
5. Tune confidence gates and consensus weights only after stability

## 10. Validation Targets

Regression coverage should include:

1. HMM train/update snapshot round-trip
2. tertiary transition confirmation logic
3. training-depth tier thresholds/modifiers
4. confidence modifier source routing
5. status payload integrity for primary/secondary/tertiary fields

