# Durable Profit/Exit Accounting and Zero-Dust Allocation Spec

**Version:** 1.0.0  
**Status:** Draft  
**Scope:** Runtime/spec architecture only (not reducer-code authoritative)

---

## 1. Problem Statement

The bot can realize profitable exits while still accumulating idle USD residue
("dust") that is not reliably reinjected into B-side order sizing. Existing PnL
tracking is useful for analytics but does not by itself provide a durable,
auditable settlement ledger for every completed order lifecycle.

This creates four failure modes:

1. Net-profit accounting does not fully express quote-balance settlement flow.
2. Allocation can be tied to broad slot counts/phases rather than immediate
   buy-entry demand.
3. Follow-up entry sizing can be based on stale pre-fill balance context.
4. Rounding/minimum constraints produce residue with no explicit carry-forward
   contract.

---

## 2. Design Goals

| Goal | Priority |
|------|----------|
| Make persistent spendable USD dust impossible | P0 |
| Track each cycle's settlement flow durably and idempotently | P0 |
| Reinject all deployable quote balance into B-side sizing | P0 |
| Survive restart/replay without accounting drift | P0 |
| Preserve current API-call budget envelope | P1 |
| Improve operator observability | P1 |

### Non-Goals

1. Separate sweep market orders.
2. DOGE-side dust optimization in this phase.
3. Strategy geometry changes (entry/profit percentage policy).

---

## 3. Existing Structures (Runtime/Spec Level)

The current system already provides:

1. Slot-based A/B lifecycle with cycle closure semantics.
2. Capital availability tracking (`available_usd`, committed funds, loop
   reservations).
3. Fill and cycle-outcome persistence.
4. Snapshot + replay behavior for restart continuity.
5. Prior dust/account-aware sizing concepts.

These are the right foundations; the gap is durable settlement accounting plus
strict buy-ready allocation semantics.

---

## 4. Root Cause

The core issue is not one formula; it is a contract gap between **realized
settlement** and **order-sizing allocation**.

1. Settlement truth is not first-class and durable at per-cycle granularity.
2. Allocation logic is not strictly constrained to "buy-ready now" demand.
3. Sizing timing can occur before final post-fill balance state.
4. Residue is not handled as explicit carry state with hard invariants.

---

## 5. Proposed Architecture

### 5.1 Order Lifecycle Registry

Maintain a durable lifecycle record keyed by deterministic identity:

- `slot_id`, `trade_id`, `cycle`, `role`
- local order identity
- exchange txid identity

State transitions:

- `intended -> placed -> partially_filled -> closed|canceled|expired`

### 5.2 Fill Settlement Journal

Store each fill as immutable journal rows with idempotency key:

- exchange fill id where available, else normalized execution tuple

Fields:

- order identity
- side, price, volume
- fee amount + fee currency
- quote delta (USD)
- base delta (DOGE)
- timestamp

### 5.3 Cycle Settlement Record

On cycle closure, persist one cycle-settlement row linked to entry + exit
lifecycle records.

Required outputs:

1. Gross spread capture (USD)
2. Fee split by leg and currency
3. Net PnL (USD)
4. Quote-settled USD delta (balance-impact metric)
5. Closure lineage (normal/recovery/replay source)

### 5.4 Quote-First Allocation Engine

Define per-loop allocation as:

1. `deployable_usd = free_quote_usd - committed_buy_quote - safety_buffer`
2. `buy_ready_slots = slots that need a new B-entry now and do not already have an active buy entry`
3. `allocation_pool = deployable_usd + carry_usd`
4. Split across `buy_ready_slots` only
5. Round down to executable precision/minimums
6. Persist residual to `carry_usd`
7. Reapply `carry_usd` automatically on next eligible buy allocation loop

This makes residue explicit and non-lossy.

---

## 6. Hard Invariants

1. **Fill idempotency:** duplicate fill rows are rejected.
2. **Cycle idempotency:** each cycle closes once.
3. **Conservation:** `allocated_usd + carry_usd == allocation_pool` within epsilon.
4. **No spendable dust persistence:** when `buy_ready_slots > 0`, unallocated
   spendable quote must remain below one executable minimum (plus epsilon).
5. **Replay determinism:** replay/restart reproduces identical lifecycle and
   settlement totals.

---

## 7. Reconciliation Contract

Run continuous three-way reconciliation:

1. Observed quote-balance change
2. Sum of cycle quote-settlement deltas
3. External-flow residual (deposits/withdrawals/manual actions)

Alerting:

1. Warning if drift exceeds soft threshold for N loops.
2. Critical if drift exceeds hard threshold or trends upward across M windows.

---

## 8. Telemetry Additions

Add to runtime status payload:

1. `total_settled_usd`
2. `cycle_settled_usd_24h`
3. `cycle_net_profit_usd_24h`
4. `deployable_usd`
5. `allocated_b_entry_usd_this_loop`
6. `carry_usd`
7. `buy_ready_slots`
8. `unallocated_spendable_usd`
9. `recon_drift_usd`
10. `recon_drift_pct`

---

## 9. Rollout Plan

### Phase 0: Shadow Ledger

Write lifecycle + fill + cycle settlement records without changing sizing
behavior.

### Phase 1: Shadow Allocation

Compute quote-first allocation in parallel and emit divergence metrics.

### Phase 2: Cutover

Make quote-first allocation authoritative for B-entry sizing.

### Phase 3: Cleanup

Retire legacy dust heuristics once stability criteria are met.

---

## 10. Acceptance Criteria

1. Over a 7-day production run, unallocated spendable USD remains below one
   executable minimum except transient single-loop spikes.
2. Replay/restart produces zero duplicate fill/cycle settlement entries.
3. Reconciliation drift remains within configured tolerance bands.
4. B-entry notional reflects realized exits by the next eligible allocation
   cycle.

---

## 11. Primary Risk

The highest-risk failure mode is still timing/order of operations:
if B-entry notional is computed before final post-fill balance state, dust can
reappear even with correct formulas. Allocation must be computed from the
freshest loop balance and current buy-ready slot set.

