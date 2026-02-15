# Kelly Implementation Plan Review (Opus)

Saved on: 2026-02-15  
Context: Review of the Kelly implementation plan for `KELLY_SPEC.md` rollout.

## Original Review (Preserved)

### Gap Analysis: Accurate

All 6 gaps are real and correctly identified. The line references check out. Particularly sharp catch on gap #2: the existing `regime_at_entry` field at `bot.py:4093` is mislabeled.

The comment on `bot.py:4092` says "Phase 0: best-effort regime snapshot at outcome time" while the field is named `regime_at_entry`. This was correctly flagged.

### Step-by-Step Assessment

#### Step 1: Lock contract and compatibility rules
Good to do first.

Decision:
- Keep `regime_at_entry` canonical as numeric ID (`0/1/2`), with tolerant parsing for legacy text data.

Agreement:
- Existing Supabase rows contain text values like `"BULLISH"`, `"BEARISH"`, `"RANGING"` from outcome-time stamping.
- Kelly partitioning already uses `regime_labels: dict[int, str]`.
- Normalizer should handle:
  - `"BULLISH"` -> `2`
  - `"RANGING"` -> `1`
  - `"BEARISH"` -> `0`
  - `None` -> unknown

#### Step 2: Database/schema readiness first
Correct ordering.

Note:
- The spec's migration warning applies: Supabase auto-detect can silently strip unknown columns.
- Migration must be applied before code deploy.

#### Step 3: Add regime tag propagation in state machine models
This is the most sensitive step.

Concern raised:
- Adding `regime_at_entry` fields to dataclasses may look like reducer impurity.

Resolution:
- Acceptable if reducer does not compute the field and only carries it as opaque passthrough data.

Required implementation constraints:
- Reducer never computes `regime_at_entry`; only copies existing values forward.
- Deserialization is backward-compatible (`.get(..., None)` behavior).
- Field type is `Optional[int]` default `None`.

#### Step 4: Stamp regime at entry in runtime
Correct.

Notes:
- Must stamp regardless of `KELLY_ENABLED` (for future data accumulation).
- Fixes the mislabeling bug at `bot.py:4093`: exit outcome writer should read persisted entry-time tag from cycle record, not call `_policy_hmm_signal()` at book time.

#### Step 5: Bring `kelly_sizer.py` to spec parity
Confirmed needed.

Issues:
- Negative-edge clamp bug at `kelly_sizer.py` no-edge path bypasses floor/ceiling clamp.
- Legacy text-to-int normalization needed for old cycle data.

#### Step 6: Integrate Kelly into runtime with master toggle
Correct sequencing; depends on steps 3-5.

#### Step 7: Add Kelly persistence and API telemetry
Straightforward.

Spec note:
- Disabled payload should include `{"enabled": false}`.

#### Step 8: Dashboard visibility
Low risk, additive, appropriate late step.

#### Step 9: Testing and rollout gating
Good strategy.

Rollout approach:
- Deploy with `KELLY_ENABLED=false`, validate telemetry, then enable gradually.

## Missing Items Called Out

1. Legacy mislabeled Supabase data strategy was not explicit.

Options listed:
- (a) Backfill map text -> int (lossy: still wrong semantic source)
- (b) Set legacy rows `regime_at_entry = NULL` (honest handling)
- (c) Keep text and normalize on read

Recommendation in review:
- Prefer (b): null legacy rows instead of preserving outcome-time proxy as entry-time signal.

2. `grid_strategy.py` `CompletedCycle` was missing in plan detail.

Note:
- Spec explicitly calls this migration out and expects backward-compatible field add.

3. Step dependency clarity between Step 3 and Step 4.

Clarification needed:
- Step 3 = structural plumbing (fields + propagation).
- Step 4 = runtime stamping and persistence wiring.

## Pre-Implementation Decision Endorsed

- Canonical storage: numeric `regime_at_entry` (`0/1/2`).
- Backward compatibility: tolerant normalization for legacy text values.

Suggested mapping:

```python
_REGIME_TEXT_TO_ID = {
    "BULLISH": 2,
    "RANGING": 1,
    "BEARISH": 0,
}
```

Use cases:
- Legacy data parse/normalization
- Snapshot/state restoration with mixed historical formats

## Bottom Line from Review

Plan quality: ready to execute with three amendments:
1. Add `grid_strategy.py:CompletedCycle` migration detail.
2. Decide and document legacy Supabase data treatment (recommended: null legacy mislabeled values).
3. Clarify Step 3 -> Step 4 dependency as structural first, runtime wiring second.

