# Durable Profit/Exit Accounting — Merged Implementation Plan

Last updated: 2026-02-16
Parent specs:
- `docs/DURABLE_PROFIT_EXIT_ACCOUNTING_SPEC.md` (v1.0.0) — architecture contracts
- `docs/DUST_PROOF_LEDGER_SPEC.md` (v0.1.0) — code-level root cause analysis

Status: **Ready for implementation**

---

## Goal

Eliminate persistent spendable USD dust by:

1. Capturing Kraken's actual `fee` and `cost` data (currently discarded)
2. Replacing fragile profit accumulators with self-healing derivation
3. Adding a quote-first allocation engine that conserves every cent
4. Recycling volume rounding residual
5. Establishing three-way reconciliation as a continuous health check

No new API call classes. No new order types.

---

## Spec Alignment

| DURABLE Spec Section | DUST_PROOF Spec Section | Merged Phase |
|---|---|---|
| §5.1 Order Lifecycle Registry | — (not in DUST_PROOF) | Phase 1 (data model only; lifecycle events deferred to Phase 2) |
| §5.2 Fill Settlement Journal | §3 Fill Ledger (GridOrder fields, check_fills_live capture) | Phase 1 (data model) + Phase 2 (shadow capture) |
| §5.3 Cycle Settlement Record | §4.3 CompletedCycle actual fields | Phase 1 (data model) + Phase 2 (shadow capture) |
| §5.4 Quote-First Allocation | — (allocation engine) | Phase 5 (shadow) + Phase 6 (cutover) |
| §6 Hard Invariants | §8 Reconciliation Invariants | Phase 7 |
| §7 Reconciliation Contract | §8.3 Balance Reconciliation | Phase 7 |
| §8 Telemetry | §9 Dashboard Visibility | Phase 8 |
| — | §4 Profit Calculation (actual-first-estimate-fallback) | Phase 3 |
| — | §5 Self-Healing Profit Derivation | Phase 4 |
| — | §6 Rounding Residual Recycling | Phase 5 |
| — | §7 Fee Tier Auto-Detection | Phase 9 |

---

## Locked Decisions (Preconditions)

These resolve the DURABLE spec's §contract-lock requirements using findings
from the DUST_PROOF analysis:

### 1. Idempotency Keys

| Record Type | Key | Source |
|---|---|---|
| Lifecycle event | `(pair, slot_id, trade_id, cycle, role, event_type)` | GridOrder identity + event |
| Fill journal row | `(txid, vol_exec)` — or `(pair, slot_id, trade_id, cycle, role)` when txid unavailable (dry run) | Kraken txid is unique per order; vol_exec disambiguates partial vs full |
| Cycle settlement | `(pair, slot_id, trade_id, cycle)` | One settlement per cycle by construction |

### 2. Fee-Currency Contract

- **Source fields**: `info["fee"]` and `info["cost"]` from Kraken QueryOrders response
- **Currency**: Always USD for USD-quoted pairs (DOGEUSD). Kraken returns fee in quote currency for spot.
- **Fallback**: When `fee` is missing or zero (dry run, old state), estimate as `price × volume × observed_fee_rate_median`. If no observed rate yet, use `MAKER_FEE_PCT`.

### 3. External-Flow Classification

- Deposits, withdrawals, manual trades → not captured in cycle settlement
- They appear as the residual in three-way reconciliation: `external_residual = observed_balance_change - cycle_settlement_sum`
- No attempt to classify individual external flows; the residual is the signal

### 4. Safety Buffer

- `ALLOCATION_SAFETY_BUFFER_USD = 0.50` (fixed)
- Subtracted from deployable pool before allocation
- Covers one worst-case rounding overshoot plus exchange-side slippage

### 5. Feature Toggles

| Toggle | Default | Purpose |
|---|---|---|
| `DURABLE_SETTLEMENT_ENABLED` | `True` | Gates shadow settlement capture (Phase 2) |
| `DURABLE_PROFIT_DERIVATION` | `False` | Gates self-healing profit derivation (Phase 4) |
| `QUOTE_FIRST_ALLOCATION` | `False` | Gates authoritative allocation engine (Phase 6) |
| `ROUNDING_RESIDUAL_ENABLED` | `False` | Gates rounding residual recycling (Phase 5) |
| `FEE_TIER_AUTO_DETECT` | `False` | Gates fee tier auto-detection (Phase 9) |

Rollback: set any toggle to `False` to revert that phase to baseline behavior.

---

## Current Baseline (Code Audit)

These are the exact code locations that change. Line numbers reference the
current `master` branch.

### Fill Detection Path

```
grid_strategy.py:1453-1458  check_fills_live()
  - Reads: vol_exec, status
  - IGNORES: fee, cost  ← ROOT CAUSE
  - Called by: bot.py main loop (every cycle)
```

### Fee Estimation Sites (7 locations in handle_pair_fill)

| # | Location | Code Path | Trade |
|---|---|---|---|
| 1 | grid_strategy.py:2972 | Buy entry fee tracking | B entry |
| 2 | grid_strategy.py:3006-3008 | Sell exit round-trip profit | B exit |
| 3 | grid_strategy.py:3021 | Sell exit fee accumulator | B exit |
| 4 | grid_strategy.py:~3100 (mirror of 2972) | Sell entry fee tracking | A entry |
| 5 | grid_strategy.py:3150-3152 | Buy exit round-trip profit | A exit |
| 6 | grid_strategy.py:3165 | Buy exit fee accumulator | A exit |
| 7 | grid_strategy.py:2618-2620 | Orphan close P&L | either |

All use: `price × volume × config.MAKER_FEE_PCT / 100.0`

### Profit Accumulator

```
grid_strategy.py  total_profit_usd += net_profit
  - 11 call sites (lines 1675, 2623, 3018, 3162, 3480, 3545, 3692, 3989,
    5085, 5134, 5266)
  - Fragile: any single-cycle error permanently contaminates all future values
  - No self-correction mechanism
```

### B-Side Sizing

```
bot.py:858-879  _b_side_base_usd()
  - Divides available_usd by ALL slots (not buy-ready only)
  - Does not account for carry residual
```

### Existing Reconciliation

```
bot.py:8840-8870  _compute_balance_recon()
  - Two-way only (bot P&L vs Kraken balance)
  - No settlement-based decomposition
```

### Data Structures

```
grid_strategy.py:211-249  GridOrder
  - NO actual_fee, actual_cost fields
  - NO matched entry fee/cost propagation

grid_strategy.py:73-103  CompletedCycle
  - Has gross_profit, fees, net_profit — all from ESTIMATES
  - NO actual Kraken data preserved

grid_strategy.py:255-340  GridState
  - total_profit_usd: float accumulator (line 273)
  - NO base_profit_usd watermark
  - NO rounding residual accumulators
  - NO observed fee rate tracking
```

---

## Implementation Phases

## Phase 1: Data Model

**Goal**: Add fields and structures to hold actual Kraken data, settlement
records, and allocation state. No behavioral changes.

**Files**:

| File | Changes |
|---|---|
| `grid_strategy.py` | Add to `GridOrder`: `actual_fee: float|None`, `actual_cost: float|None`, `matched_entry_fee: float|None`, `matched_entry_cost: float|None` |
| `grid_strategy.py` | Add to `CompletedCycle`: `entry_fee_actual`, `exit_fee_actual`, `entry_cost_actual`, `exit_cost_actual` |
| `grid_strategy.py` | Add to `GridState`: `base_profit_usd: float`, `rounding_residual_a: float`, `rounding_residual_b: float`, `observed_fee_rates: list[float]`, `observed_fee_rate_median: float`, `carry_usd: float` |
| `grid_strategy.py` | Update `save_state()` / `load_state()` to persist new fields with backward-compatible defaults (None/0.0) |
| `config.py` | Add toggle constants: `DURABLE_SETTLEMENT_ENABLED`, `DURABLE_PROFIT_DERIVATION`, `QUOTE_FIRST_ALLOCATION`, `ROUNDING_RESIDUAL_ENABLED`, `FEE_TIER_AUTO_DETECT`, `ALLOCATION_SAFETY_BUFFER_USD` |

**Deliverables**:
1. All new fields serialize/deserialize correctly
2. Old state.json files load without error (defaults kick in)
3. Supabase persistence follows existing auto-detect pattern (strip unknown columns)

**Exit criterion**: `load_state()` → `save_state()` round-trip preserves all new fields; loading an old-format state.json produces valid defaults.

---

## Phase 2: Shadow Fill Capture

**Goal**: Capture Kraken's actual `fee` and `cost` on every fill without
changing profit calculation or order sizing.

**Files**:

| File | Changes |
|---|---|
| `grid_strategy.py` `check_fills_live()` (line 1453-1458) | After reading `vol_exec`, also read `info.get("fee")` and `info.get("cost")`. Store on `order.actual_fee` and `order.actual_cost`. |
| `grid_strategy.py` `handle_pair_fill()` | When entry fills and exit is placed, copy entry's `actual_fee`/`actual_cost` to exit order's `matched_entry_fee`/`matched_entry_cost` |
| `grid_strategy.py` cycle closure sites | When CompletedCycle is created, populate `entry_fee_actual`, `exit_fee_actual`, `entry_cost_actual`, `exit_cost_actual` from the orders |

**Behavioral change**: None. Profit calculation still uses estimated fees.
The actual data is captured and persisted but not yet used for sizing.

**Shadow telemetry**: Log per-fill delta between actual and estimated fee:
```
delta = actual_fee - (price × volume × MAKER_FEE_PCT / 100)
```

**Exit criterion**:
1. After 48h, every new fill has `actual_fee` and `actual_cost` populated
2. Shadow delta log shows consistent sign (confirms fee tier mismatch direction)
3. Restart/replay does not create duplicate entries (idempotency via txid)

---

## Phase 3: Profit Calculation Switchover

**Goal**: Replace estimated fees with actual Kraken data in all 7 profit
calculation sites.

**Files**:

| File | Changes |
|---|---|
| `grid_strategy.py` `handle_pair_fill()` (7 sites listed above) | Replace `price × volume × MAKER_FEE_PCT / 100.0` with actual-first-estimate-fallback pattern |

**Pattern** (applied at each site):

```python
# Actual-first, estimate-fallback
exit_fee = filled.actual_fee if filled.actual_fee else (
    filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0)
entry_fee = filled.matched_entry_fee if filled.matched_entry_fee else (
    entry_price * filled.volume * config.MAKER_FEE_PCT / 100.0)
```

For cost-based gross calculation (more accurate than price × volume):

```python
exit_cost = filled.actual_cost if filled.actual_cost else (
    filled.price * filled.volume)
entry_cost = filled.matched_entry_cost if filled.matched_entry_cost else (
    entry_price * filled.volume)
```

**Behavioral change**: Profit numbers change slightly for new fills. Old fills
in completed_cycles retain their estimated values (no retroactive correction).

**Exit criterion**:
1. Balance reconciliation drift decreases (actual fees are more accurate)
2. No fill/cycle processing regressions
3. CompletedCycle records show non-zero actual fields for all new cycles

---

## Phase 4: Self-Healing Profit Derivation

**Goal**: Make `total_profit_usd` derivable from cycle history instead of
relying on a fragile accumulator.

**Files**:

| File | Changes |
|---|---|
| `grid_strategy.py` `GridState` | `base_profit_usd` field (added in Phase 1) |
| `grid_strategy.py` `_trim_completed_cycles()` | Before trimming, sum `net_profit` of removed cycles into `base_profit_usd` |
| `grid_strategy.py` `save_state()` | Derive total: `total_profit_usd = base_profit_usd + sum(c.net_profit for c in completed_cycles)`. Verify against accumulator. Log drift. |
| `grid_strategy.py` `handle_pair_fill()` | Keep `+= net_profit` for in-loop logging accuracy, but it's no longer authoritative |

**Gate**: `DURABLE_PROFIT_DERIVATION = True`

**Migration on first startup**:
1. `base_profit_usd = 0.0` (nothing trimmed yet)
2. `derived = sum(c.net_profit for c in completed_cycles)`
3. If `|total_profit_usd - derived| > 0.01`, log drift warning, adopt derived
4. Going forward, `save_state()` always writes the derived value

**Invariant** (checked every save):
```
|total_profit_usd - (base_profit_usd + sum(c.net_profit for c in completed_cycles))| < 0.0001
```

**Exit criterion**:
1. Derivation invariant holds for 7 days
2. No observable sizing regression (profit feeds slot sizing via compounding)

---

## Phase 5: Rounding Residual Recycling

**Goal**: Recycle volume rounding residual into subsequent orders instead of
leaving it as dust.

**Files**:

| File | Changes |
|---|---|
| `grid_strategy.py` `GridState` | `rounding_residual_a`, `rounding_residual_b` (added in Phase 1) |
| `grid_strategy.py` `compute_order_volume()` or `_place_pair_order()` | Inject residual into effective USD before rounding; update residual with new remainder |

**Logic**:
```
effective_usd = target_usd + rounding_residual[side]
vol = round(effective_usd / price)
actual_usd = vol × price
rounding_residual[side] = clamp(effective_usd - actual_usd, -cap, +cap)
```

Cap = 25% of `ORDER_SIZE_USD` (prevents pathological accumulation).

**Gate**: `ROUNDING_RESIDUAL_ENABLED = True`

**Persistence**: Residuals saved in state.json and survive restart.

**Exit criterion**:
1. Over 100 orders, observed rounding waste < 10% of baseline (pre-recycling)
2. Fund guard (`_try_reserve_loop_funds`) never blocks due to residual overshoot

---

## Phase 6: Quote-First Allocation Engine

**Goal**: Replace all-slots division with buy-ready-only allocation that
conserves every dollar.

**Files**:

| File | Changes |
|---|---|
| `bot.py` `_b_side_base_usd()` | Refactor to quote-first contract (or add parallel path behind toggle) |
| `grid_strategy.py` `GridState` | `carry_usd` field (added in Phase 1) |

**Allocation contract** (per loop):

```
1. free_quote_usd = Kraken USD balance (from existing balance query)
2. committed_buy_quote = sum of all open buy orders' notional value
3. deployable_usd = free_quote_usd - committed_buy_quote - ALLOCATION_SAFETY_BUFFER_USD
4. allocation_pool = deployable_usd + carry_usd
5. buy_ready_slots = slots needing a B-entry NOW with no active buy order
6. per_slot = floor(allocation_pool / buy_ready_slots, precision)
7. carry_usd = allocation_pool - (per_slot × buy_ready_slots)
```

**Conservation invariant** (checked every loop):
```
|per_slot × buy_ready_slots + carry_usd - allocation_pool| < 0.000001
```

**Shadow mode**: First deploy computes the allocation in parallel with existing
`_b_side_base_usd()`. Log divergence. No sizing change.

**Cutover**: `QUOTE_FIRST_ALLOCATION = True` makes the new allocator
authoritative. Old path becomes fallback.

**Gate**: `QUOTE_FIRST_ALLOCATION`

**Exit criterion**:
1. `carry_usd` stays below one executable minimum in steady state
2. No undeployed spendable dust when `buy_ready_slots > 0`
3. No regression in fill rate or cycle completion

---

## Phase 7: Three-Way Reconciliation

**Goal**: Extend existing two-way reconciliation to a three-way settlement
decomposition.

**Files**:

| File | Changes |
|---|---|
| `bot.py` `_compute_balance_recon()` (line 8840-8870) | Add settlement aggregate comparison |
| `grid_strategy.py` | Helper to sum cycle settlement deltas over a time window |
| `config.py` | `RECON_SOFT_THRESHOLD_USD`, `RECON_HARD_THRESHOLD_USD`, `RECON_WINDOW_LOOPS` |

**Three-way check**:
```
A = observed_quote_balance_change (Kraken balance delta since last snapshot)
B = sum(cycle.quote_delta for recent cycles)  (settlement aggregate)
C = A - B  (external-flow residual: deposits, withdrawals, manual trades)

drift = |A - B - C|  (should be zero by construction)
alert if |C| exceeds soft threshold for N consecutive loops
critical if |C| exceeds hard threshold or trends upward
```

**Dashboard**: Expose `recon_drift_usd`, `recon_drift_pct`, `external_residual_usd` in status payload.

**Exit criterion**:
1. Drift decomposition is stable for 7 days
2. External residual correctly reflects known deposits/withdrawals

---

## Phase 8: Dashboard & Telemetry

**Goal**: Surface all new accounting data in the operator dashboard.

**Files**:

| File | Changes |
|---|---|
| `dashboard.py` | Fee Health card (configured vs observed rate, mismatch warning) |
| `dashboard.py` | Rounding residual line (A/B amounts, lifetime recycled) |
| `dashboard.py` | Profit derivation health (base + active = derived, drift) |
| `dashboard.py` | Allocation health (deployable, buy-ready, carry, conservation) |
| `dashboard.py` | Reconciliation drift card (3-way breakdown) |
| `bot.py` status payload | Add fields: `total_settled_usd`, `cycle_settled_usd_24h`, `cycle_net_profit_usd_24h`, `deployable_usd`, `allocated_b_entry_usd_this_loop`, `carry_usd`, `buy_ready_slots`, `unallocated_spendable_usd`, `recon_drift_usd`, `recon_drift_pct` |

**Exit criterion**: Dashboard correctly reflects all new accounting state.

---

## Phase 9: Fee Tier Auto-Detection

**Goal**: Observe Kraken's actual fee rate and use it as the authoritative
fallback instead of hardcoded `MAKER_FEE_PCT`.

**Files**:

| File | Changes |
|---|---|
| `grid_strategy.py` | On each fill with actual data: `observed_rate = actual_fee / actual_cost`. Append to `observed_fee_rates` (rolling window, max 100). Update `observed_fee_rate_median`. |
| `grid_strategy.py` handle_pair_fill fee fallback | When `actual_fee` is None, use `observed_fee_rate_median` instead of `MAKER_FEE_PCT` (if ≥10 observations exist) |
| `grid_strategy.py` `_pair_exit_price()` | Optionally use observed rate for tighter exit pricing |
| `dashboard.py` | Show configured vs observed rate with mismatch warning |
| `config.py` | `FEE_TIER_AUTO_DETECT` toggle, `FEE_OBSERVATION_WINDOW = 100`, `FEE_MISMATCH_THRESHOLD_PCT = 10` |

**Exclusion**: Market orders (DCA accumulation) excluded from the observation
window — they hit taker rate, not maker rate.

**Gate**: `FEE_TIER_AUTO_DETECT = True`

**Exit criterion**:
1. Observed rate converges to actual tier within 100 fills
2. Mismatch detection fires correctly when config differs from reality

---

## Phase 10: Cleanup & Simplification

**Goal**: Remove legacy dust heuristics and estimation-only code paths that
are no longer needed.

**Files**:

| File | Changes |
|---|---|
| `grid_strategy.py` | Remove or simplify `MAKER_FEE_PCT`-only estimation paths (keep as bootstrap default only) |
| `config.py` | Remove `USD_DUST_SWEEP` related config if still present |
| docs | Update `ACCOUNT_AWARE_B_SIDE_SIZING_SPEC.md` status to superseded |
| docs | Update `USD_DUST_SWEEP_SPEC.md` status to superseded |

**Exit criterion**:
1. All profit calculations use actual-first path
2. No dead code paths for heuristic dust absorption
3. Feature toggles for Phases 2-9 can be removed (behavior is default)

---

## Testing Plan

### Unit Tests

| Test | Covers |
|---|---|
| `test_actual_fee_captured_on_fill` | Phase 2: Mock QueryOrders with fee/cost, assert GridOrder fields populated |
| `test_actual_fee_used_in_profit` | Phase 3: Fill with actual_fee=0.004, assert net_profit uses actual |
| `test_fallback_to_estimate` | Phase 3: Fill with actual_fee=None, assert estimated fee used |
| `test_matched_entry_fee_propagation` | Phase 2: Entry fills, exit placed, assert exit carries entry's actual data |
| `test_derived_profit_matches_cycles` | Phase 4: After N cycles, assert total_profit == base + sum(cycles) |
| `test_self_healing_on_drift` | Phase 4: Set wrong total_profit, trigger save, assert derived value persisted |
| `test_base_profit_watermark` | Phase 4: Trim 50 cycles, assert base_profit incremented correctly |
| `test_rounding_residual_accumulates` | Phase 5: 5 orders same price, assert residual flips rounding |
| `test_rounding_residual_capped` | Phase 5: Large residual, assert clamped to ±25% |
| `test_rounding_residual_persists` | Phase 5: Save/load state, assert residuals survive |
| `test_buy_ready_slot_selection` | Phase 6: Mixed S0/S1a/S1b/S2 slots, assert correct buy-ready count |
| `test_allocation_conservation` | Phase 6: Assert `per_slot × slots + carry == pool` |
| `test_carry_forward` | Phase 6: Small remainder persists, reapplied next loop |
| `test_three_way_recon` | Phase 7: Inject known balance change + cycles, assert correct decomposition |
| `test_observed_fee_rate_window` | Phase 9: Feed 20 fills, assert median converges |
| `test_fee_mismatch_detection` | Phase 9: Observed 0.20% vs config 0.25%, assert mismatch flag |
| `test_migration_from_old_state` | Phase 1: Load old state.json, assert graceful defaults |

### Replay/Recovery Tests

1. Restart with partially processed fills → no duplicate lifecycle/fill rows
2. Replay with repeated trade-history rows → idempotency rejects duplicates
3. Snapshot restore preserves carry_usd, rounding residuals, base_profit_usd

### Integration Tests

1. Multi-slot scenario with mixed phases (S0/S1a/S1b/S2) and buy-ready subset
2. External-flow event (manual deposit) and reconciliation residual behavior
3. High-pressure order-cap conditions with deferred entries

### Invariant Tests (continuous, every loop)

1. Fill idempotency: duplicate fill writes rejected
2. Cycle idempotency: each cycle closes exactly once
3. Conservation: `allocated + carry == pool` within epsilon
4. Derivation: `total_profit == base + sum(cycles)` within epsilon
5. No persistent spendable dust when `buy_ready_slots > 0`

---

## Rollout Plan

### Phase A: Data Model + Shadow Capture (Phases 1-2)

1. Deploy new fields (backward compatible)
2. Enable shadow fill capture (`DURABLE_SETTLEMENT_ENABLED = True`)
3. Monitor: actual vs estimated fee delta log, serialization correctness
4. Duration: observe 48 hours minimum

### Phase B: Profit Switchover + Self-Healing (Phases 3-4)

1. Enable actual-fee-first profit calculation
2. Enable self-healing derivation (`DURABLE_PROFIT_DERIVATION = True`)
3. Monitor: balance reconciliation drift (should decrease), derivation invariant
4. Duration: observe 7 days minimum

### Phase C: Allocation + Residual (Phases 5-6)

1. Enable rounding residual recycling (`ROUNDING_RESIDUAL_ENABLED = True`)
2. Deploy allocation engine in shadow mode first (log divergence)
3. Cutover allocation (`QUOTE_FIRST_ALLOCATION = True`) after 7 days shadow
4. Monitor: conservation invariant, dust level, fill rate

### Phase D: Reconciliation + Dashboard + Auto-Detect (Phases 7-9)

1. Enable three-way reconciliation
2. Deploy dashboard cards
3. Enable fee auto-detection (`FEE_TIER_AUTO_DETECT = True`)
4. Monitor: recon stability, dashboard accuracy

### Phase E: Cleanup (Phase 10)

1. Remove legacy dust heuristics
2. Update spec statuses
3. Remove feature toggles (new behavior becomes default)

---

## Rollback Plan

Each phase has an independent toggle. Rollback is granular:

| Situation | Action |
|---|---|
| Shadow capture creates issues | Set `DURABLE_SETTLEMENT_ENABLED = False` |
| Profit derivation drift | Set `DURABLE_PROFIT_DERIVATION = False` (reverts to accumulator) |
| Allocation causes sizing problems | Set `QUOTE_FIRST_ALLOCATION = False` (reverts to all-slots division) |
| Rounding residual causes fund guard blocks | Set `ROUNDING_RESIDUAL_ENABLED = False` |

In all cases:
- Keep shadow data for forensic analysis
- Do not drop journaled settlement records
- Re-enable only after root-cause closure

---

## Acceptance Criteria

1. **7-day run** with `unallocated_spendable_usd` below one executable minimum
   while `buy_ready_slots > 0`
2. **Zero duplicate** fill/cycle settlement rows across restart/replay
3. **Reconciliation drift** within configured soft/hard thresholds
4. **B-entry sizing** reflects realized exits by the next eligible allocation cycle
5. **No increase** in API-call classes versus current runtime envelope
6. **Derivation invariant** holds continuously for 7 days
7. **Rounding waste** reduced by ≥90% versus baseline (pre-recycling)

---

## Risk Summary

| Risk | Severity | Mitigation |
|---|---|---|
| Timing: B-entry sized before post-fill balance settles | High | Quote-first allocation uses freshest loop balance + execution-time revalidation |
| Old state.json without actual fee data | Low | Estimate fallback; gradual improvement as new fills accumulate |
| Rounding residual overshoots fund guard | Low | Capped at 25% of ORDER_SIZE_USD; fund guard is final safety net |
| Fee tier change mid-session | Low | Auto-detection adapts organically over ~100 fills |
| Self-healing adoption of wrong derived value | Medium | Only adopt if `|drift| > 0.01`; log all corrections for human review |
| Allocation engine underdeploys during cutover | Medium | Shadow mode first; divergence telemetry quantifies gap before cutover |
