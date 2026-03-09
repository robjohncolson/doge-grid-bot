# Polyglot Parity Spec: Catch-Up to Master (rev 4)

**Branch**: `polyglot-refactor`
**Target parity**: master @ `a80ef4e` (STATE_MACHINE.md rev 4)
**Date**: 2026-02-17

## 0. Context

The `polyglot-refactor` branch was cut when master was at rev 2 of `STATE_MACHINE.md`.
Since then, master has added **30+ commits** introducing major subsystems. This spec
maps every gap and prescribes exactly where work goes across the three-language stack.

**Architecture reminder** (unchanged):

```
Haskell  (doge-core-exe)   -- pure reducer: transition() + check_invariants()
Rust     (doge_hmm)         -- HMM regime detection via PyO3
Python   (bot.py + modules) -- runtime orchestration, all advisory overlays
```

**Key principle**: the reducer is the only code that runs in Haskell. Everything else
(throughput sizer, AI advisor, Bayesian stack, self-healing, accumulation, balance
intelligence, manifold score, ops panel, churner) lives in Python as runtime overlays.
The Rust HMM is an optional accelerator for `hmm_regime_detector.py`.

---

## 1. Reducer Parity (Haskell)

These are the **only** changes needed in the Haskell binary. They correspond to the
`state_machine.py` diff between the branch and master.

### 1.1 New Fields

| Type | Field | Haskell Type | Default |
|------|-------|-------------|---------|
| `OrderState` | `regime_at_entry` | `Maybe Int` | `Nothing` |
| `RecoveryOrder` | `regime_at_entry` | `Maybe Int` | `Nothing` |
| `CycleRecord` | `entry_fee` | `Double` | `0.0` |
| `CycleRecord` | `exit_fee` | `Double` | `0.0` |
| `CycleRecord` | `quote_fee` | `Double` | `0.0` |
| `CycleRecord` | `settled_usd` | `Double` | `0.0` |
| `CycleRecord` | `regime_at_entry` | `Maybe Int` | `Nothing` |
| `PairState` | `total_settled_usd` | `Double` | `0.0` |
| `BookCycleAction` | `settled_usd` | `Double` | `0.0` |

### 1.2 Regime ID Normalization

Add `normalizeRegimeId :: Value -> Maybe Int` helper that handles:
- `null` -> `Nothing`
- int `0|1|2` -> `Just n`
- float -> truncate, validate
- string `"BEARISH"|"RANGING"|"BULLISH"` -> `Just 0|1|2`
- string digit `"0"|"1"|"2"` -> `Just n`
- everything else -> `Nothing`

Wire into `FromJSON` for all `regime_at_entry` fields.

### 1.3 Updated `_book_cycle` Logic

The cycle booking function must now compute:

```
entry_fee = order.entry_fee
exit_fee  = fill_fee
fees      = entry_fee + exit_fee
quote_fee = entry_fee  if trade_id == A
          | exit_fee   if trade_id == B
settled_usd = gross - quote_fee
```

And update `total_settled_usd` on the state alongside `total_profit`.

### 1.4 Regime Propagation

- **Entry fill -> exit creation**: copy `regime_at_entry` from filled entry to new exit order
- **Exit orphan -> recovery**: copy `regime_at_entry` from exit to recovery record
- **Recovery fill -> pseudo-order**: copy `regime_at_entry` from recovery to pseudo-order for cycle booking
- **Recovery eviction -> pseudo-order**: copy `regime_at_entry` from recovery when booking eviction loss
- **Cycle record**: carry `regime_at_entry` from the order that closed the cycle

### 1.5 New RPC Methods

Add `apply_order_regime_at_entry` method to the JSON server:

```json
{"method": "apply_order_regime_at_entry", "params": {"state": ..., "local_id": 5, "regime_at_entry": 2}}
```

Returns the patched state. This is a pure state-patch helper (no transition logic).

### 1.6 Deserialization Robustness

The `from_dict` / `FromJSON` path must use safe per-field parsing (matching master's
`_order_from_dict`, `_recovery_from_dict`, `_cycle_from_dict` pattern) instead of
constructor-splat. This prevents crashes on snapshots with extra/missing fields.

Already partially done in Haskell via `withDefault`; verify all new fields use it.

### 1.7 Files to Modify

- `doge-core/src/DogeCore/Types.hs` — all field additions + regime normalization
- `doge-core/src/DogeCore/Transition.hs` — book_cycle settlement math + regime propagation
- `doge-core/app/Main.hs` — new RPC method registration

### 1.8 Acceptance

- All existing 13 cross-language parity fixtures pass with updated fields
- New fixtures added covering:
  - Cycle with regime_at_entry propagation
  - Settlement math (settled_usd, quote_fee) for both A and B trades
  - Regime normalization edge cases (string, int, null, invalid)
  - Recovery eviction with regime tags
- Shadow mode produces zero divergences on the new fields

---

## 2. HMM Parity (Rust)

The Rust `doge_hmm` crate must support features added in master's HMM stack.

### 2.1 Tertiary (1h) Detector

Master adds a third `RegimeDetector` instance for 1-hour candles (strategic context).

**Rust changes**:
- `RegimeDetector` already supports parameterized intervals — no struct changes needed
- Add `TertiaryTransition` tracking struct:
  ```rust
  struct TertiaryTransition {
      from_regime: i32,
      to_regime: i32,
      confirmation_count: i32,
      confirmed: bool,
      changed_at: f64,
      transition_age_sec: f64,
  }
  ```
- PyO3 binding: expose `TertiaryTransition` as a Python class
- `serialize_for_snapshot()` / `restore_from_snapshot()` must handle tertiary state

### 2.2 Training Depth and Quality Tiers

Master tracks per-pipeline training depth and derives quality tiers:

| Tier | Depth Range | Modifier |
|------|-------------|----------|
| `shallow` | < 1000 | 0.70 |
| `baseline` | 1000-2499 | 0.85 |
| `deep` | 2500-3999 | 0.95 |
| `full` | >= 4000 | 1.00 |

**Rust changes**:
- Add `training_depth` field to `RegimeDetector` (set after `fit()`)
- Add `quality_tier(&self) -> &str` method
- Add `confidence_modifier(&self) -> f64` method
- PyO3: expose both methods

### 2.3 Confidence Modifier Pipeline

Master computes `confidence_effective = confidence_raw * confidence_modifier` where
the modifier comes from the active source's training depth.

**Rust changes**:
- Add `confidence_modifier_for_source(source: &str, primary_depth: i32, secondary_depth: i32, tertiary_depth: i32) -> f64` function
- PyO3: expose as module-level function

### 2.4 Updated `compute_grid_bias()`

No functional change needed — the existing implementation already returns spacing
multipliers and size skew. Verify it handles the `regime_at_entry` tagging contract
(this is actually a Python-side responsibility — Rust just provides the signal).

### 2.5 Snapshot Format Update

`serialize_for_snapshot()` and `restore_from_snapshot()` must include:
- Tertiary detector state
- Training depth per pipeline
- Tertiary transition state

### 2.6 Files to Modify

- `doge-hmm/src/regime.rs` — tertiary transition struct, training depth, quality tier
- `doge-hmm/src/lib.rs` — PyO3 bindings for new types/functions
- `doge-hmm/src/hmm.rs` — training depth tracking in `fit()`

### 2.7 Acceptance

- Rust detector matches Python `hmm_regime_detector.py` on label assignment for
  identical training data (tolerance: regime labels agree on >95% of test windows)
- Tertiary transition tracking matches Python semantics
- Quality tier and confidence modifier produce identical values for same depth inputs
- Snapshot round-trip preserves tertiary state

---

## 3. Python Integration — Merge Master Runtime Modules

These modules are **Python-only runtime overlays**. They do not touch the reducer.
They already exist on master and need to be merged onto this branch.

### 3.1 New Modules to Merge (git cherry-pick or merge)

| Module | Master Commit(s) | Purpose |
|--------|------------------|---------|
| `throughput_sizer.py` | `aa9d203`, `30d7e60` | Fill-time throughput sizing (replaces Kelly) |
| `ai_advisor.py` | `88ca979`, `c9429e9`, `395a68b` | Multi-provider AI regime advisor |
| `bayesian_engine.py` | `ac07263` | Belief state, trade-belief actions, manifold |
| `bocpd.py` | `ac07263` | Online Bayesian changepoint detection |
| `survival_model.py` | `ac07263` | Fill-time survival modeling |
| `position_ledger.py` | `3608c73` | Self-healing position/subsidy accounting |
| `kelly_sizer.py` | `aa9d203` | Deprecated but still present on master |

### 3.2 Modified Modules to Merge

| Module | Key Changes |
|--------|-------------|
| `bot.py` | +11k lines — all runtime overlays wired in |
| `config.py` | +413 lines — all new config parameters |
| `dashboard.py` | +2.4k lines — expanded API surface |
| `hmm_regime_detector.py` | Tertiary detector, training depth, confidence modifier |
| `state_machine.py` | Regime vintage, settlement, deserialization (§1 above) |
| `supabase_store.py` | New tables/queries |
| `kraken_client.py` | Minor additions |

### 3.3 New Test Modules to Merge

| Test Module | Coverage |
|-------------|----------|
| `tests/test_throughput_sizer.py` | Throughput sizing model |
| `tests/test_ai_regime_advisor.py` | AI advisor pipeline |
| `tests/test_bayesian_intelligence.py` | Bayesian stack |
| `tests/test_bocpd.py` | BOCPD detector |
| `tests/test_survival_model.py` | Survival model |
| `tests/test_position_ledger.py` | Position ledger |
| `tests/test_self_healing_slots.py` | Self-healing integration |
| `tests/test_kelly_sizer.py` | Kelly sizer (deprecated) |
| `tests/test_balance_intelligence.py` | Balance intelligence |
| `tests/test_hardening_regressions.py` | +3k lines of regression coverage |

### 3.4 Merge Strategy

**Recommended approach**: rebase `polyglot-refactor` onto current master.

Rationale:
- The polyglot branch adds files (Haskell, Rust, adapter) that don't conflict with
  master's changes
- Master's changes are all in Python files that the polyglot branch barely touches
- `state_machine.py` will have merge conflicts — resolve by taking master's version
  and re-verifying Haskell parity
- `doge_core.py` adapter was deleted on master — needs to be re-added after rebase
- `tests/test_cross_language.py` was deleted on master — needs to be re-added

**Conflict hotspots**:
1. `state_machine.py` — master changed fields; branch didn't touch reducer logic, so
   take master's version
2. `bot.py` — master rewrote massively; branch only changed the import line. Take
   master's version, re-apply the `import doge_core as sm` line
3. `doge_core.py` — deleted on master. Re-add as new file post-rebase
4. Test files deleted on master — re-add post-rebase

**Alternative**: cherry-pick all master commits onto branch. More surgical but
higher risk of missing interdependencies between the 30+ commits.

---

## 4. Adapter Updates (`doge_core.py`)

The `doge_core.py` adapter must be updated after the merge to export new symbols
and handle new fields.

### 4.1 New Exports

Master added these to `state_machine.py`:
- `_normalize_regime_id`
- `apply_order_regime_at_entry`
- `_order_from_dict`, `_recovery_from_dict`, `_cycle_from_dict` (internal but used by tests)

The adapter must re-export `apply_order_regime_at_entry` and route it correctly:
- **Haskell backend**: send as RPC call to `doge-core-exe`
- **Python backend**: delegate to `state_machine.apply_order_regime_at_entry`

### 4.2 Shadow Mode Field Comparison

Shadow mode compares Python and Haskell outputs field-by-field. Update the comparison
to include:
- `total_settled_usd` on `PairState`
- `regime_at_entry` on orders, recoveries, cycles
- `entry_fee`, `exit_fee`, `quote_fee`, `settled_usd` on `CycleRecord`
- `settled_usd` on `BookCycleAction`

### 4.3 Serialization Round-Trip

Verify that `to_dict` -> JSON -> Haskell `FromJSON` -> Haskell `ToJSON` -> JSON ->
Python `from_dict` produces identical state for all new fields. Edge cases:
- `regime_at_entry = None` must survive round-trip as `null`/`Nothing`
- `total_settled_usd` defaults to `total_profit` when missing (backward compat)

---

## 5. Cross-Language Test Expansion

### 5.1 New Golden Fixtures

Add fixtures to `tests/test_cross_language.py`:

1. **Regime vintage propagation**: entry fill with `regime_at_entry=2` -> verify exit
   order carries `regime_at_entry=2` -> verify cycle record carries it
2. **Settlement math (A-side)**: A-side cycle close -> verify `quote_fee = entry_fee`,
   `settled_usd = gross - entry_fee`
3. **Settlement math (B-side)**: B-side cycle close -> verify `quote_fee = exit_fee`,
   `settled_usd = gross - exit_fee`
4. **Regime normalization**: state with `regime_at_entry="BULLISH"` -> verify Haskell
   normalizes to `2`
5. **Recovery with regime**: orphan exit with `regime_at_entry=1` -> verify recovery
   carries it -> recovery fill -> verify cycle carries it
6. **Eviction with regime**: recovery eviction with regime tag -> verify booked loss
   cycle carries `regime_at_entry`
7. **Backward compat**: old-format state (no `regime_at_entry`, no `total_settled_usd`)
   -> verify both backends produce same defaults

### 5.2 Updated Fixture Format

All existing fixtures must add the new fields (defaulting to `null`/`0.0`) so that
field-level comparison doesn't flag false divergences.

---

## 6. STATE_MACHINE.md Update

After all code changes, update the branch's `STATE_MACHINE.md` to match master's
rev 4. This is a doc-only change — copy master's version verbatim, since the code
will be at parity by then.

---

## 7. Feature-by-Feature Gap Summary

Legend: HS = Haskell change, RS = Rust change, PY = Python merge, -- = no change needed

| # | Feature | HS | RS | PY | Notes |
|---|---------|----|----|-----|-------|
| 1 | Regime vintage tags (`regime_at_entry`) | **YES** | -- | YES | Reducer field + propagation |
| 2 | Durable profit settlement (`total_settled_usd`, fee split) | **YES** | -- | YES | Reducer math |
| 3 | `apply_order_regime_at_entry()` helper | **YES** | -- | YES | New RPC method |
| 4 | Robust deserialization (`_*_from_dict`) | **YES** | -- | YES | Safety |
| 5 | Throughput sizer | -- | -- | **YES** | Python-only runtime overlay |
| 6 | Account-aware B-side sizing + dust sweep | -- | -- | **YES** | Python-only |
| 7 | Quote-first allocation | -- | -- | **YES** | Python-only |
| 8 | AI regime advisor (multi-provider) | -- | -- | **YES** | Python-only |
| 9 | DCA accumulation engine | -- | -- | **YES** | Python-only |
| 10 | Balance intelligence | -- | -- | **YES** | Python-only |
| 11 | Self-healing slots + position ledger | -- | -- | **YES** | Python-only |
| 12 | Bayesian stack (BOCPD, survival, beliefs) | -- | -- | **YES** | Python-only |
| 13 | Manifold score + ops panel + churner | -- | -- | **YES** | Python-only |
| 14 | Tertiary (1h) HMM detector | -- | **YES** | YES | Rust + Python |
| 15 | HMM training depth + quality tiers | -- | **YES** | YES | Rust + Python |
| 16 | HMM confidence modifier | -- | **YES** | YES | Rust + Python |
| 17 | Tier 2 re-entry cooldown | -- | -- | **YES** | Python-only (runtime) |
| 18 | Recovery orders enabled/disabled gating | -- | -- | **YES** | Python-only (runtime) |
| 19 | Expanded dashboard/API surface | -- | -- | **YES** | Python-only |
| 20 | Expanded Telegram commands | -- | -- | **YES** | Python-only |
| 21 | Startup recovery cleanup | -- | -- | **YES** | Python-only |
| 22 | Position ledger migration | -- | -- | **YES** | Python-only |
| 23 | Expanded snapshot persistence | -- | -- | **YES** | Python-only |

---

## 8. Implementation Order

### Phase A: Rebase & Resolve (1 session)

1. Rebase `polyglot-refactor` onto master
2. Resolve merge conflicts (see §3.4)
3. Re-add `doge_core.py` adapter
4. Re-add `tests/test_cross_language.py` and `tests/test_doge_core_adapter.py`
5. Verify all existing Python tests pass

### Phase B: Haskell Reducer Parity (1-2 sessions)

1. Add new fields to `Types.hs` (§1.1)
2. Add regime normalization helper (§1.2)
3. Update `Transition.hs` book_cycle + regime propagation (§1.3, §1.4)
4. Add `apply_order_regime_at_entry` RPC (§1.5)
5. Build and verify compilation
6. Update cross-language fixtures (§5)
7. Run parity tests until green

### Phase C: Rust HMM Updates (1 session)

1. Add tertiary transition tracking (§2.1)
2. Add training depth + quality tier (§2.2)
3. Add confidence modifier (§2.3)
4. Update snapshot format (§2.5)
5. Build and run Rust tests

### Phase D: Adapter & Shadow Mode (1 session)

1. Update `doge_core.py` exports (§4.1)
2. Update shadow comparison (§4.2)
3. Verify serialization round-trip (§4.3)
4. Run full test suite

### Phase E: Doc Parity (quick)

1. Copy master's `STATE_MACHINE.md` rev 4 to branch
2. Update `ARCHITECTURE.md` with new module list
3. Update `HMM_INTEGRATION.md` with tertiary + training depth

---

## 9. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Rebase conflicts in bot.py | High | Medium | bot.py polyglot changes are small (1 import line); take master version, re-apply |
| Haskell settlement math divergence | Medium | High | Test with explicit numeric fixtures, tolerance 1e-10 |
| Rust HMM label disagreement with hmmlearn | Medium | Medium | Acceptance threshold: 95% agreement on labels; fallback to Python always available |
| Shadow mode false positives from new fields | Low | Low | Update field comparison before enabling shadow |
| New Python modules import errors | Low | Medium | Run full test suite after merge |

---

## 10. Success Criteria

- [ ] `polyglot-refactor` branch is rebased on master HEAD
- [ ] All master Python tests pass on branch
- [ ] Haskell binary handles all new fields (regime vintage, settlement)
- [ ] Cross-language parity: 20+ fixtures, zero divergences
- [ ] Rust HMM supports tertiary detector + training depth
- [ ] Shadow mode runs clean with new field comparison
- [ ] `STATE_MACHINE.md` matches master rev 4
- [ ] `doge_core.py` adapter exports new helpers
- [ ] CI passes all jobs (Haskell build, Python tests, Rust build)
