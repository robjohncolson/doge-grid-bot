# Polyglot Parity Implementation Plan (for Claude Approval)

**Source spec**: `docs/POLYGLOT_PARITY_SPEC.md` (rev 4, dated 2026-02-17)  
**Target branch**: `polyglot-refactor`  
**Target parity baseline**: master @ `a80ef4e` (`STATE_MACHINE.md` rev 4)

## 1. Objective

Reach functional parity between `polyglot-refactor` and master for:

1. Haskell reducer state/transition contract.
2. Rust HMM detector/runtime contract.
3. Python runtime overlays and adapter integration.
4. Cross-language parity/shadow validation.

## 2. Non-Goals

1. No strategy redesign beyond master parity.
2. No new features outside documented parity gaps.
3. No production cutover in this phase; this plan ends at code/test/doc parity.

## 3. Pre-Implementation Gates

1. Confirm master reference commit is still `a80ef4e`. If master moved, re-baseline before coding.
2. Confirm intended integration strategy: rebase onto master (recommended) vs cherry-pick.
3. Confirm acceptance thresholds:
   1. Cross-language parity: zero divergences on fixtures.
   2. HMM parity: >95% regime-label agreement on shared windows.
4. Confirm CI/build tooling availability:
   1. Python test runner.
   2. Haskell build/test path (`stack`/`cabal` as project-standard).
   3. Rust build/test path (`cargo`/PyO3 module build).

## 4. Execution Plan

### Phase 1: Rebase and Master Runtime Catch-Up

**Goal**: land all master Python runtime overlay changes on `polyglot-refactor`.

1. Rebase `polyglot-refactor` onto master (preferred).
2. Resolve merge conflicts with the approved policy:
   1. `state_machine.py`: prefer master implementation.
   2. `bot.py`: prefer master implementation.
   3. `doge_core.py` and cross-language tests: re-add/update in post-rebase commits.
3. Confirm post-rebase boot contract:
   1. `bot.py` on master imports `state_machine` directly.
   2. `doge_core.py` adapter wiring is restored after rebase as branch-specific follow-up.
4. Ensure presence of spec-listed modules and major updates:
   1. New: `throughput_sizer.py`, `ai_advisor.py`, `bayesian_engine.py`, `bocpd.py`, `survival_model.py`, `position_ledger.py`, `kelly_sizer.py`.
   2. Modified: `bot.py`, `config.py`, `dashboard.py`, `hmm_regime_detector.py`, `state_machine.py`, `supabase_store.py`, `kraken_client.py`.
   3. Tests: all parity-related suites in spec section 3.3.
5. Reconcile import and wiring assumptions in runtime startup flow.

**Output**:

1. Branch includes all master runtime modules relevant to parity.

**Exit criteria**:

1. Python app boots in non-trading mode without import/runtime wiring errors.
2. Master Python tests pass.
3. Cross-language parity tests are not required in this phase.

### Expected Temporary Gap Window (By Design)

Between Phase 1 and Phase 2, cross-language parity tests are expected to fail because
the rebased Python reducer includes new fields/semantics while Haskell has not yet been
updated. This is expected Phase 2 workload, not a regression.

### Phase 2: Haskell Reducer Parity

**Goal**: match `state_machine.py` rev-4 contract in Haskell reducer.

1. Add fields in `doge-core/src/DogeCore/Types.hs`:
   1. `regime_at_entry` on order/recovery/cycle entities.
   2. `entry_fee`, `exit_fee`, `quote_fee`, `settled_usd` on cycle records.
   3. `total_settled_usd` on pair state.
   4. `settled_usd` on `BookCycleAction`.
2. Implement `normalizeRegimeId :: Value -> Maybe Int` and wire to all relevant `FromJSON` paths.
3. Update cycle booking math in `doge-core/src/DogeCore/Transition.hs`:
   1. Compute `entry_fee`, `exit_fee`, `quote_fee`, `settled_usd`.
   2. Update `total_settled_usd`.
4. Implement regime propagation through:
   1. Entry fill -> exit order.
   2. Exit orphan -> recovery.
   3. Recovery fill/eviction -> pseudo-order.
   4. Final cycle record.
5. Add RPC method in `doge-core/app/Main.hs`:
   1. `apply_order_regime_at_entry`.
6. Validate robust deserialization defaults for backward snapshots, including:
   1. missing `total_settled_usd` must default to `total_profit` in Haskell `FromJSON`.

**Output**:

1. Haskell reducer contract includes rev-4 regime and settlement semantics.

**Exit criteria**:

1. Haskell build succeeds.
2. Cross-language parity tests pass for existing and new reducer fields.

### Phase 3: Rust HMM Parity

**Goal**: match master HMM capabilities and snapshot format.

1. Add tertiary detector transition model in `doge-hmm/src/regime.rs` and expose via PyO3.
2. Add training-depth tracking in `doge-hmm/src/hmm.rs` (set during `fit()`).
3. Add quality-tier + confidence-modifier methods in `doge-hmm/src/regime.rs`.
4. Add module-level helper in `doge-hmm/src/lib.rs`:
   1. `confidence_modifier_for_source(...)`.
5. Extend snapshot serialization/restoration for:
   1. Tertiary detector state.
   2. Per-pipeline training depth.
   3. Tertiary transition metadata.
6. Ensure Python detector compatibility in `hmm_regime_detector.py` wiring.

**Output**:

1. Rust module API supports tertiary + depth/quality parity.

**Exit criteria**:

1. Rust tests pass.
2. Python/Rust snapshot round-trip preserves new fields.
3. Label-agreement acceptance threshold met on shared test windows.

### Phase 4: Adapter and Shadow Parity

**Goal**: keep Python/Haskell paths consistent and observable.

1. Update `doge_core.py` export surface:
   1. Re-export/delegate `apply_order_regime_at_entry`.
2. Expand shadow comparisons to include:
   1. `total_settled_usd`.
   2. `regime_at_entry` (orders/recoveries/cycles).
   3. `entry_fee`, `exit_fee`, `quote_fee`, `settled_usd`.
3. Validate serialization compatibility for old and new snapshots.
4. Confirm backward-compat default behavior:
   1. Missing `regime_at_entry` -> `None`.
   2. Missing `total_settled_usd` -> `total_profit` (matching master `from_dict` semantics).

**Output**:

1. Adapter parity across Python backend, Haskell backend, and shadow mode.

**Exit criteria**:

1. Adapter tests pass.
2. Shadow mode reports zero divergences for added parity fixtures.

### Phase 5: Cross-Language Fixture and Test Expansion

**Goal**: lock parity guarantees around new semantics.

1. Add fixture scenarios from spec section 5.1:
   1. Regime vintage propagation.
   2. A-side settlement math.
   3. B-side settlement math.
   4. Regime normalization.
   5. Recovery fill with regime.
   6. Recovery eviction with regime.
   7. Backward-compatible old snapshots.
2. Update existing fixtures with default new fields to avoid false divergences.
3. Extend assertions in:
   1. `tests/test_cross_language.py`
   2. `tests/test_doge_core_adapter.py`

**Output**:

1. Fixture corpus reflects rev-4 contract.

**Exit criteria**:

1. Cross-language test suite passes with zero diffs.

### Phase 6: Documentation and Final Verification

**Goal**: documentation reflects implemented parity state.

1. Sync `STATE_MACHINE.md` to master rev 4 content.
2. Update `ARCHITECTURE.md` module list for new runtime overlays.
3. Update `HMM_INTEGRATION.md` for tertiary detector and training-depth semantics.
4. Run full parity verification matrix (section 5 below).

**Output**:

1. Code and docs aligned to parity baseline.

**Exit criteria**:

1. Final checklist fully green.

## 5. Verification Matrix

1. Python unit/regression:
   1. Phase 1 gate: run master-aligned Python suites; exclude cross-language adapter/parity suites until Phase 2+.
   2. Phase 2+ gate: include cross-language suites as Haskell parity lands.
   3. `tests/test_cross_language.py`
   4. `tests/test_doge_core_adapter.py`
   5. `tests/test_hardening_regressions.py`
   6. Newly merged parity module tests from spec section 3.3
2. Haskell:
   1. Build succeeds.
   2. JSON RPC method coverage includes `apply_order_regime_at_entry`.
   3. Cross-language fixture pass against compiled backend.
3. Rust:
   1. Build and tests succeed.
   2. PyO3 surface exposes new methods/types.
   3. Snapshot round-trip tests cover tertiary/depth fields.
4. Shadow mode:
   1. Added-field comparison active.
   2. No divergence on expanded fixture set.

## 6. Rollback Strategy

1. If branch catch-up destabilizes runtime:
   1. Revert merge/rebase result to pre-catch-up commit.
   2. Re-apply modules in smaller batches with test gates.
2. If Haskell parity drifts:
   1. Keep Python backend authoritative.
   2. Use shadow logs to isolate field-level drift before re-enable.
3. If Rust parity is incomplete:
   1. Keep Python HMM detector as active path.
   2. Continue Rust validation behind fallback gate.

## 7. Claude Approval Checklist

Approve or request changes on:

1. Integration strategy: rebase onto master (recommended).
2. Conflict policy for `state_machine.py` and `bot.py` (prefer master, then re-apply adapter wiring).
3. Acceptance thresholds:
   1. Cross-language: zero divergence.
   2. HMM label agreement: >95%.
4. Phase ordering and gate criteria.
5. Backward-compat policy for missing snapshot fields.
