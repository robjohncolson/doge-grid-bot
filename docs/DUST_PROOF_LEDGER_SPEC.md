# Dust-Proof Ledger Spec

**Version:** 0.1.0
**Status:** Draft
**Supersedes:** USD Dust Sweep Spec v0.1.0 (root cause fix; sweep was symptom treatment)
**Depends on:** `check_fills_live()`, `handle_pair_fill()`, `GridOrder`, `CompletedCycle`, Kraken QueryOrders API

---

## 1. Problem Statement

The bot systematically leaks value into untradeable "dust." After the
Account-Aware B-Side Sizing fix (62a5cb2), idle USD is deployed more
aggressively — but dust still forms because the underlying tracking is
broken.

### 1.1 Observed Symptoms

- `total_profit_usd` consistently understates actual Kraken balance growth
- Balance reconciliation shows persistent positive drift (more USD in
  account than the bot thinks it earned)
- Small USD amounts accumulate that never flow back into order sizing

### 1.2 Quantified Sources

Consider a typical $2.00 order at DOGE = $0.18:

| Source | Per-Trade Loss | Mechanism |
|--------|---------------|-----------|
| **Fee estimation error** | $0.001–0.005 | `MAKER_FEE_PCT` hardcoded at 0.25%; actual Kraken tier may be 0.20% or lower. Over-estimated fees → under-reported profit. |
| **Float arithmetic drift** | $0.0001–0.001 | `gross = (sell_price - buy_price) × volume` differs from Kraken's pre-computed `cost` field by sub-cent amounts due to IEEE 754 rounding. |
| **Volume rounding residual** | $0.00–0.05 | `round($2.00 / $0.18)` = 11 DOGE → actual cost $1.98. The $0.02 residual stays idle in the Kraken balance with no mechanism to recapture it. |
| **Accumulator fragility** | compounds | `total_profit_usd += net` on every cycle. Any single-cycle error permanently contaminates all future values. No self-correction. |

Over 1000 round trips, fee estimation alone leaks $1–5. Volume rounding
leaks $0–50 (price-dependent). The accumulator error never corrects, so
it grows monotonically.

### 1.3 The Smoking Gun

Kraken's QueryOrders API returns `cost` (actual USD transacted) and `fee`
(actual USD fee charged) on every closed order. These fields are
**documented in our own codebase** (`kraken_client.py:720-721`). But:

1. `check_fills_live()` reads only `vol_exec` and `status` — **ignores
   `cost` and `fee`** (grid_strategy.py:1453-1458)
2. `GridOrder` has no fields for actual fee or cost — **nowhere to store
   the data** (grid_strategy.py:211-249)
3. `handle_pair_fill()` **estimates all fees** using a hardcoded rate:
   `price × volume × config.MAKER_FEE_PCT / 100.0` (grid_strategy.py:
   2972, 3006-3007, 3021, 3150-3151, 3165, 2619)

The actual Kraken data flows through the system and is discarded.
The profit calculation uses a fictional number instead.

---

## 2. Design Goals

| Goal | Priority |
|------|----------|
| Use Kraken's actual `fee` and `cost` for ALL profit calculations | P0 |
| Make `total_profit_usd` self-healing (derivable, not accumulated) | P0 |
| Recycle volume rounding residual into next order | P0 |
| Zero additional Kraken API calls | P0 |
| Backward compatible with existing state.json / Supabase schema | P1 |
| Auto-detect actual fee tier from observed Kraken fees | P1 |
| Dashboard visibility into fee accuracy and rounding waste | P2 |

### Non-Goals

- Changing Kraken fee tier (user manages their own tier)
- Fixing DOGE-side dust (DOGE volume is whole-unit by construction)
- Adding new order types or API calls
- Modifying the S0/S1/S2 state machine transitions

---

## 3. Fill Ledger: Capture Actual Kraken Data

### 3.1 New Fields on GridOrder

```
GridOrder:
    ...existing fields...
    actual_fee: float | None     # Kraken's reported fee (USD), None = not yet filled
    actual_cost: float | None    # Kraken's reported cost (USD), None = not yet filled
```

Default `None` for unfilled orders. Populated at fill detection time.
These are write-once fields — set when the order transitions to "filled"
and never modified after.

### 3.2 Capture in check_fills_live()

When an order's status is "closed" in the QueryOrders response, read
the actual fee and cost:

```
Current (grid_strategy.py:1453-1458):
    if status == "closed":
        vol_exec = float(info.get("vol_exec", order.volume))
        if vol_exec > 0:
            order.volume = vol_exec
        order.status = "filled"

Proposed:
    if status == "closed":
        vol_exec = float(info.get("vol_exec", order.volume))
        if vol_exec > 0:
            order.volume = vol_exec
        order.actual_fee = float(info.get("fee", 0.0))
        order.actual_cost = float(info.get("cost", 0.0))
        order.status = "filled"
```

No new API calls. The data is already in the response — we just read two
more fields.

### 3.3 Propagation to Exit Orders

When an entry fills and we place an exit, carry the entry's actual data
forward so it's available at round-trip completion:

```
GridOrder (exit):
    matched_buy_fee: float | None      # Entry leg's actual Kraken fee
    matched_buy_cost: float | None     # Entry leg's actual Kraken cost
    (or matched_sell_fee / matched_sell_cost for Trade A exits)
```

This mirrors the existing `matched_buy_price` / `matched_sell_price`
pattern already on GridOrder.

---

## 4. Profit Calculation: Actual, Not Estimated

### 4.1 handle_pair_fill() — Use Actual Fees

Replace every fee estimation with actual Kraken data. The pattern
repeats in 6 locations in handle_pair_fill():

**Current (example, Trade B exit — grid_strategy.py:3006-3008):**
```python
gross = (filled.price - buy_price) * filled.volume
fees = (buy_price * filled.volume * config.MAKER_FEE_PCT / 100.0 +
        filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0)
net_profit = gross - fees
```

**Proposed:**
```python
# Use actual Kraken cost when available, fall back to price × volume
exit_cost = filled.actual_cost if filled.actual_cost else filled.price * filled.volume
entry_cost = filled.matched_buy_cost if filled.matched_buy_cost else buy_price * filled.volume

# Use actual Kraken fees when available, fall back to estimate
exit_fee = filled.actual_fee if filled.actual_fee else filled.price * filled.volume * config.MAKER_FEE_PCT / 100.0
entry_fee = filled.matched_buy_fee if filled.matched_buy_fee else buy_price * filled.volume * config.MAKER_FEE_PCT / 100.0

gross = exit_cost - entry_cost    # for Trade B (buy entry, sell exit)
fees = entry_fee + exit_fee
net_profit = gross - fees
```

The fallback ensures backward compatibility: old fills without actual
data still work with the estimated formula.

### 4.2 All 6 Fee Estimation Sites

| Location | Code Path | Trade | Lines |
|----------|-----------|-------|-------|
| Buy entry fill | Entry fee tracking | B entry | 2972 |
| Sell exit fill | Round-trip profit | B exit | 3006-3008 |
| Sell exit fee | Fee accumulator | B exit | 3021 |
| Sell entry fill | Entry fee tracking | A entry | (mirror) |
| Buy exit fill | Round-trip profit | A exit | 3150-3152 |
| Buy exit fee | Fee accumulator | A exit | 3165 |
| Orphan close | Orphan P&L | either | 2618-2620 |

Each site gets the same actual-first-estimate-fallback pattern.

### 4.3 CompletedCycle — Record Actual Data

Extend CompletedCycle to preserve the actual Kraken values:

```
CompletedCycle:
    ...existing fields (gross_profit, fees, net_profit)...
    entry_fee_actual: float       # Kraken's actual entry fee (0 if unavailable)
    exit_fee_actual: float        # Kraken's actual exit fee (0 if unavailable)
    entry_cost_actual: float      # Kraken's actual entry cost (0 if unavailable)
    exit_cost_actual: float       # Kraken's actual exit cost (0 if unavailable)
```

The existing `fees` and `gross_profit` fields continue to hold the
VALUES USED (actual when available, estimate when not). The new
`_actual` fields are the raw Kraken data for audit purposes.

---

## 5. Self-Healing Profit Derivation

### 5.1 The Problem with Accumulators

`total_profit_usd += net_profit` on every cycle means:
- Float drift compounds over thousands of additions
- A single wrong `net_profit` permanently pollutes the total
- State.json round-trip (serialize → deserialize) can introduce error
- No way to detect or correct drift without external audit

### 5.2 Derived Profit

Instead of accumulating, derive `total_profit_usd` from the cycle
ledger:

```
total_profit_usd = sum(c.net_profit for c in state.completed_cycles)
```

This is re-computed on every read (or cached per-loop). Any historical
error in a single cycle's `net_profit` is isolated to that cycle. Adding
a new correct cycle doesn't inherit old errors.

### 5.3 Migration

On first startup after deployment:

1. Compute `derived = sum(c.net_profit for c in completed_cycles)`
2. Compare to `state.total_profit_usd`
3. If they differ, log the drift and adopt the derived value
4. Going forward, `total_profit_usd` is always derived, never
   incremented

The incremental `+= net_profit` lines remain in handle_pair_fill for
logging (so the "Total: $X.XX" log line is correct mid-loop), but the
**persisted** value in save_state() always uses the derived sum.

### 5.4 Completed Cycles Integrity

`completed_cycles` is the source of truth. It must not be trimmed or
modified. The existing `_trim_completed_cycles(state)` function limits
the in-memory list to prevent unbounded growth. This creates a problem:
if cycles are trimmed, the derived sum is incomplete.

**Solution**: Maintain a `base_profit_usd` watermark that represents the
sum of all trimmed cycles. The derived formula becomes:

```
total_profit_usd = base_profit_usd + sum(c.net_profit for c in completed_cycles)
```

When `_trim_completed_cycles` removes N cycles from the front:
```
trimmed_profit = sum(c.net_profit for c in removed_cycles)
state.base_profit_usd += trimmed_profit
```

This is still self-healing because:
- `base_profit_usd` is a one-time computed sum of old cycles (not an
  accumulator that drifts per-cycle)
- The active `completed_cycles` window is always re-derivable
- The watermark only changes on trim events (rare, not per-cycle)

---

## 6. Rounding Residual Recycling

### 6.1 The Rounding Problem

`compute_order_volume()` in grid_strategy.py (line 1092) or
state_machine.py:

```python
raw = order_size_usd / price
vol = round(raw)    # volume_decimals = 0 for DOGE → whole units
```

`round()` uses banker's rounding (round-half-to-even). The residual per
order is:

```
residual_usd = order_size_usd - (vol × price)
```

This can be positive (rounded down, undeployed USD) or negative (rounded
up, over-deployed USD). It averages toward zero but doesn't cancel
perfectly — small amounts accumulate.

### 6.2 Per-Side Residual Accumulator

Add to GridState:

```
rounding_residual_a: float = 0.0    # A-side (sell entries)
rounding_residual_b: float = 0.0    # B-side (buy entries)
```

Separate accumulators because A-side entries draw from DOGE inventory
while B-side entries draw from USD balance. They shouldn't cross-
subsidize.

### 6.3 Recycling Mechanism

When computing volume for a new entry order:

```
1. effective_usd = target_usd + rounding_residual[side]
2. vol = round(effective_usd / price)
3. actual_usd = vol × price
4. new_residual = effective_usd - actual_usd
5. Clamp: new_residual = clamp(new_residual, -cap, +cap)
6. rounding_residual[side] = new_residual
```

Cap = 25% of `ORDER_SIZE_USD`. This prevents pathological accumulation
if price moves dramatically between orders.

### 6.4 Example

At DOGE = $0.18, ORDER_SIZE_USD = $2.00:

| Order | Target | + Residual | Effective | Volume | Actual | New Residual |
|-------|--------|-----------|-----------|--------|--------|-------------|
| 1 | $2.00 | $0.00 | $2.00 | 11 | $1.98 | +$0.02 |
| 2 | $2.00 | +$0.02 | $2.02 | 11 | $1.98 | +$0.04 |
| 3 | $2.00 | +$0.04 | $2.04 | 11 | $1.98 | +$0.06 |
| 4 | $2.00 | +$0.06 | $2.06 | 11 | $1.98 | +$0.08 |
| 5 | $2.00 | +$0.08 | $2.08 | 12 | $2.16 | -$0.08 |
| 6 | $2.00 | -$0.08 | $1.92 | 11 | $1.98 | -$0.06 |

The residual oscillates. Without the accumulator, orders 1-4 each waste
$0.02 = $0.08 total dust. With the accumulator, the dust builds until it
flips the rounding on order 5, deploying the accumulated residual.

### 6.5 Persistence

`rounding_residual_a` and `rounding_residual_b` are persisted in
state.json. On cold start, residuals survive and continue recycling.

---

## 7. Fee Tier Auto-Detection

### 7.1 Observed vs Configured Fee Rate

Rather than relying on `MAKER_FEE_PCT` being correct, compute the
actual observed fee rate from Kraken's reported data:

```
observed_rate = actual_fee / actual_cost    (for each filled order)
```

Maintain a rolling window of the last N observed rates:

```
GridState:
    observed_fee_rates: list[float]    # Rolling window (max 100)
    observed_fee_rate_median: float    # Cached median for quick access
```

### 7.2 Usage

The observed rate is used for:

1. **Fallback fee estimation** — when `actual_fee` is unavailable (e.g.,
   old fills loaded from state.json without actual data), use
   `observed_fee_rate_median` instead of `MAKER_FEE_PCT`

2. **Exit pricing** — `_pair_exit_price()` uses `ROUND_TRIP_FEE_PCT` to
   set minimum profit margins. If the observed rate is lower than
   configured, the margin target is tighter than necessary (leaves money
   on the table). Auto-detected rate improves this.

3. **Dashboard alert** — if `observed_fee_rate_median` differs from
   `MAKER_FEE_PCT` by more than 10%, show a config mismatch warning

### 7.3 Bootstrap

On first run or after a config change, there are no observed rates yet.
Fall back to `MAKER_FEE_PCT` until ≥10 fills accumulate.

---

## 8. Reconciliation Invariants

### 8.1 Per-Cycle Invariant

After every round-trip completion, verify:

```
|net_profit - (gross_profit - fees)| < 0.000001
```

This catches any arithmetic error in the profit calculation before it
contaminates the total.

### 8.2 Derivation Invariant

On every save_state() call:

```
derived = base_profit_usd + sum(c.net_profit for c in completed_cycles)
|total_profit_usd - derived| < 0.0001
```

If this fails, log a warning and adopt the derived value. This is the
self-healing mechanism.

### 8.3 Balance Reconciliation (existing)

The existing `_compute_balance_recon()` compares bot P&L to actual
Kraken balance changes. With actual fee/cost data, the drift should
converge toward zero. A persistent drift after this fix indicates a
different problem (deposits, withdrawals, external trades).

### 8.4 Fee Cross-Check

After every fill with actual data:

```
estimated_fee = price × volume × MAKER_FEE_PCT / 100.0
actual_fee = filled.actual_fee
delta = actual_fee - estimated_fee
```

Track `delta` in a rolling window. If the median delta is consistently
non-zero, the configured `MAKER_FEE_PCT` is wrong. This feeds the
auto-detection in §7.

---

## 9. Dashboard Visibility

### 9.1 Fee Health Card

Small card in the summary panel:

```
Fee Tracking
  Configured: 0.25% maker | Observed: 0.20% maker
  ⚠ Config mismatch: you may be on a lower fee tier
  Last 100 fills: avg fee $0.0041 (expected $0.0050)
```

Only shown when there's a mismatch or when observed data is available.

### 9.2 Rounding Residual

Line in the existing dust sweep section:

```
Rounding residual: A=$0.04 B=-$0.02 | Lifetime recycled: $1.23
```

### 9.3 Profit Derivation Health

In the PnL audit section:

```
Profit derivation: ✓ OK (drift: $0.0000)
  base_profit_usd: $12.34 (from 847 trimmed cycles)
  active_cycles: 153 cycles, $2.56
  derived_total: $14.90
```

---

## 10. State Persistence & Migration

### 10.1 New Fields in state.json

```json
{
  "rounding_residual_a": 0.04,
  "rounding_residual_b": -0.02,
  "base_profit_usd": 12.34,
  "observed_fee_rates": [0.0020, 0.0020, ...],
  "observed_fee_rate_median": 0.0020
}
```

### 10.2 GridOrder Serialization

When GridOrders are serialized (save_state), include the new fields:

```json
{
  "side": "buy",
  "price": 0.18,
  "volume": 11,
  "actual_fee": 0.00495,
  "actual_cost": 1.98,
  ...
}
```

Old state.json files without these fields load fine — the fields default
to `None` / `0.0`, triggering the estimation fallback.

### 10.3 Supabase

If Supabase schema supports the fields, include them. If not, strip
them (same pattern as existing trade_id/cycle auto-detection).

### 10.4 First-Run Migration

On first startup after deployment:

1. Load state.json (old format, no actual fee data)
2. All existing CompletedCycles have estimated fees — that's fine
3. Compute `base_profit_usd = 0.0` (nothing trimmed yet)
4. Compute `derived = sum(c.net_profit for c in completed_cycles)`
5. If `total_profit_usd ≠ derived`, log drift and adopt derived
6. Set `rounding_residual_a = 0.0`, `rounding_residual_b = 0.0`
7. Going forward, new fills get actual data; old fills keep estimates

No data loss. No schema migration. Gradual improvement as new actual-
fee fills replace old estimated-fee history.

---

## 11. Edge Cases

### 11.1 Partial Fills

Kraken reports `fee` and `cost` for the executed portion of a partial
fill. The bot currently waits for full fill before acting
(grid_strategy.py:1477-1487). When the fill completes, `fee` and `cost`
reflect the TOTAL for all partial executions. No special handling needed.

### 11.2 Market Orders (DCA Accumulation)

Market orders hit the `taker_fee` tier, not `maker_fee`. If the DCA
accumulation engine places market orders, their actual fees will be
captured correctly (Kraken reports the real fee regardless of order
type). The observed_fee_rate window should exclude market orders to
avoid contaminating the maker fee estimate.

### 11.3 Dry Run Mode

In dry run, there's no Kraken response. `actual_fee` and `actual_cost`
stay `None`. The estimation fallback kicks in. No change to dry-run
behavior.

### 11.4 Very Old State Files

State files from before this change have no actual fee data anywhere.
The entire system runs on estimates until new fills accumulate. This is
identical to current behavior — no regression.

### 11.5 Fee Tier Changes

If the user's 30-day volume crosses a Kraken tier boundary (e.g., 0.25%
→ 0.20%), the observed_fee_rate_median will shift organically over the
next ~100 fills. The auto-detection tracks reality; no manual config
change needed. `MAKER_FEE_PCT` becomes a bootstrap default, not the
source of truth.

### 11.6 Rounding Residual and Fund Guard

The rounding residual can make `effective_usd` slightly larger than
`target_usd`. The existing fund guard (`_try_reserve_loop_funds`) is
the final safety net — it prevents placing an order that exceeds
available balance. If the fund guard rejects the bumped order, the
residual persists and is tried next cycle.

---

## 12. Rate Limit Impact

**Zero.** All data (`fee`, `cost`) comes from the existing QueryOrders
response that `check_fills_live()` already fetches. No new API calls.

---

## 13. Testing Plan

| Test | Description |
|------|-------------|
| `test_actual_fee_captured_on_fill` | Mock QueryOrders response with `fee` and `cost`. Assert GridOrder.actual_fee and actual_cost are populated after check_fills_live. |
| `test_actual_fee_used_in_profit` | Fill with actual_fee=0.004 vs estimated 0.005. Assert net_profit uses actual. |
| `test_fallback_to_estimate` | Fill with actual_fee=None (old order). Assert estimated fee used. |
| `test_derived_profit_matches_cycles` | After 10 cycles, assert total_profit_usd == base_profit_usd + sum(cycles). |
| `test_self_healing_on_drift` | Set total_profit_usd to wrong value, trigger save_state. Assert persisted value is derived. |
| `test_rounding_residual_accumulates` | Place 5 orders at same price. Assert residual flips rounding on the 5th. |
| `test_rounding_residual_capped` | Inject large residual. Assert clamped to ±25% of ORDER_SIZE_USD. |
| `test_rounding_residual_persists` | Save/load state. Assert residuals survive round-trip. |
| `test_observed_fee_rate_window` | Feed 20 fills with actual fees. Assert median converges to actual rate. |
| `test_fee_mismatch_detection` | Observed median 0.20% vs config 0.25%. Assert mismatch flag set. |
| `test_base_profit_watermark` | Trim 50 cycles. Assert base_profit_usd incremented by sum of trimmed. |
| `test_migration_from_old_state` | Load state.json with no actual fee fields. Assert graceful fallback. |

---

## 14. Rollout

### Stage 1: Capture Only (observe)

Deploy with actual fee/cost capture enabled but **profit calculation
unchanged**. Log the delta between actual and estimated fees per fill.
Observe for 48 hours to validate the data quality and quantify the
real fee tier.

### Stage 2: Switch Profit Calculation

Enable actual-fee-first profit calculation. Enable self-healing
derivation. Monitor balance reconciliation drift — it should converge
toward zero.

### Stage 3: Enable Rounding Residual

Turn on rounding residual recycling. Monitor order sizes for
correctness. The fund guard is the safety net.

### Stage 4: Auto-Detection

Enable fee tier auto-detection and dashboard alerts. At this point
`MAKER_FEE_PCT` becomes advisory — the system adapts to reality.

---

## 15. Summary of Changes

| Component | What Changes |
|-----------|-------------|
| `GridOrder` | Add `actual_fee`, `actual_cost`, `matched_buy_fee`, `matched_buy_cost` (and sell equivalents) |
| `check_fills_live()` | Read `fee` and `cost` from QueryOrders response (2 lines) |
| `handle_pair_fill()` | Use actual fee/cost with estimate fallback (6 sites) |
| `CompletedCycle` | Add `entry_fee_actual`, `exit_fee_actual`, `entry_cost_actual`, `exit_cost_actual` |
| `GridState` | Add `rounding_residual_a/b`, `base_profit_usd`, `observed_fee_rates`, `observed_fee_rate_median` |
| `save_state()` / `load_state()` | Persist new fields (backward-compatible defaults) |
| `total_profit_usd` | Derived from `base_profit_usd + sum(cycles)` on save, not accumulated |
| Order sizing | `effective_usd = target_usd + rounding_residual[side]` |
| Dashboard | Fee health card, rounding residual line, derivation health |
| Config | `MAKER_FEE_PCT` becomes bootstrap default; observed rate takes precedence |
