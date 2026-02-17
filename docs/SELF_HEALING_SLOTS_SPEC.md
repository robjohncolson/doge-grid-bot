# Self-Healing Slots Spec

**Version:** v0.2
**Date:** 2026-02-16
**Status:** Draft — ready for implementation planning
**Depends on:** Dust-Proof Ledger Spec v0.1.0, Sticky Slots Spec v0.2, Throughput Sizer Spec v1.0
**Supersedes:** Recovery order system (orphan → recovery → lottery ticket)
**Scope:** Position ledger, per-slot subsidy accounting, regime-gated churner mode, graduated exit repricing
**Files affected:** `grid_strategy.py`, `bot.py`, `config.py`, `dashboard.py`, `state_machine.py` (new: `position_ledger.py`)

---

## 1. Problem

Over time, sticky slots accumulate a bimodal distribution of stale exits:
sells clustered high (from Trade B entries during dips) and buys clustered
low (from Trade A entries during pumps). Each stale exit locks a slot and
its capital. Eventually most slots are occupied by positions whose exits are
far from current price, and fresh capital has nowhere to work.

### 1.1 Why Orphans Failed

The orphan/recovery system tried to solve this by abandoning stale exits
and placing "lottery ticket" recovery orders on Kraken. Problems:

- **Binary**: A position was either fine or abandoned. No middle ground.
- **Chaotic**: Recovery orders were fire-and-forget with no feedback loop.
- **Lossy**: Abandoned positions realized immediate losses.
- **No flywheel**: Freeing one slot didn't help free the next.

### 1.2 What We Want Instead

Each slot should be **self-healing**: when its exit is stale, the slot
begins grinding small, fast cycles near current price. The profit from
those cycles subsidizes repricing the stuck exit closer to market. When
the subsidy is sufficient, the exit is repriced and eventually fills.
The slot unsticks, the subsidy is earned back, and the cycle repeats.

**Key insight**: The question isn't "is this position profitable?" — it's
"is this capital earning its keep?" A $2 exit sitting 8% from market for
3 days is costing more in missed throughput than it would cost to close
at breakeven.

---

## 2. Locked Decisions

1. **Self-healing, not global pool.** Each slot manages its own subsidy.
   No cross-slot allocation decisions. Per-slot with a shared backstop
   reserve for fully-locked slots (§6.5).
2. **Churner mode is regime-gated.** Churners only activate when HMM
   consensus is "ranging." Trending markets kill tight exits. No
   additional volatility gate for v0.2 — timeouts and min order size
   bound the worst case.
3. **Churner target: fees + minimal margin.** Goal is cycle velocity,
   not profit margin. `ROUND_TRIP_FEE_PCT + CHURNER_MARGIN_PCT`.
4. **Graduated pressure, not binary.** Five age bands with escalating
   actions: hold → tighten → breakeven → small loss → close.
5. **Two-table ledger is the foundation.** `position_ledger` stores
   current state (immutable entry context + mutable exit state).
   `position_journal` is append-only event history. Subsidy totals are
   derived from journal entries, not mutable accumulators.
6. **Builds on Dust-Proof Ledger.** Actual Kraken fee/cost data,
   derived profit, rounding residual recycling are prerequisites.
7. **Original exit stays on Kraken.** When a slot enters churner mode,
   its stale exit remains live (free lottery ticket). Churner orders
   are additional, not replacements.
8. **Subsidy repricing is explicit.** Every repricing event is a journal
   entry with clear accounting: old price, new price, subsidy consumed.
9. **Write-off requires operator approval** (or policy threshold).
   The bot never silently closes a position at a loss.
10. **Churner capital is small.** Churner orders use minimum viable size
    to avoid locking more capital while healing.
11. **Pair-specific subsidy.** In swarm mode, each pair's churners fund
    only that pair's stuck positions. No cross-pair transfers.
12. **100% to subsidy until healed, then compound.** While a stuck exit
    needs subsidy, all churner profit goes to the subsidy pool. Once
    healed, churner profit compounds into the churner's order size for
    future resilience.
13. **Tighten reprice uses max(profit_pct, volatility_adjusted).** The
    user's base profit_pct is the floor. Volatility-adjusted target is
    respected when it's wider. Tighten only fires when the exit is
    wider than both.
14. **Slot mode is a per-position label.** `"legacy"`, `"sticky"`, or
    `"churner"` — recorded at position creation for historical analysis.
    Does not change the state machine transitions.

---

## 3. Scope

### In

1. **Position Ledger** — two-table design (`position_ledger` +
   `position_journal`) in new module `position_ledger.py`.
2. **Subsidy Accounting** — per-slot subsidy balance derived from
   journal entries, not mutable accumulators.
3. **Age Bands** — five graduated bands with configurable thresholds and
   escalating actions.
4. **Churner Mode** — regime-gated slot personality that activates when
   an exit ages past threshold and HMM says ranging.
5. **Graduated Repricing** — subsidy-funded exit repricing at each band
   transition.
6. **Dashboard** — position age heatmap, subsidy balances, churner
   activity, cleanup queue.
7. **Config** — `CHURNER_*` and `SUBSIDY_*` env vars.
8. **Supabase persistence** — two new tables, same auto-detect pattern.

### Out

1. Global subsidy pool or cross-slot transfers (each slot self-heals).
2. Changes to the S0/S1/S2 state machine transitions.
3. Automatic write-off / market-close without operator action.
4. Multi-pair swarm changes (churner is per-slot, not per-pair).
5. Changes to HMM regime detection or AI advisor.
6. Cross-pair subsidy transfers in swarm mode.
7. Additional churner volatility gate (deferred — timeouts sufficient).

---

## 4. Position Ledger

The ledger is two tables with strict mutability rules. This is the
foundation that churner, subsidy, and cleanup logic all query against.

### 4.1 Table: position_ledger

Every time an entry fills and an exit gets placed, a record is created.
Entry context is immutable — once written, entry fields never change.

```
position_ledger:
    # Identity
    position_id:        auto-increment         # unique across all positions
    slot_id:            int
    trade_id:           "A" or "B"
    slot_mode:          "legacy" | "sticky" | "churner"
    cycle:              int                     # from cycle_a / cycle_b

    # Entry context (IMMUTABLE after creation)
    entry_price:        float                   # actual fill price
    entry_cost:         float                   # Kraken actual_cost (USD)
    entry_fee:          float                   # Kraken actual_fee (USD)
    entry_volume:       float                   # DOGE
    entry_time:         float                   # unix timestamp
    entry_regime:       str                     # HMM consensus at entry
    entry_volatility:   float                   # realized vol at entry

    # Exit intent (MUTABLE — updated on reprice)
    current_exit_price: float                   # limit price on Kraken now
    original_exit_price: float                  # first placement (never changes)
    target_profit_pct:  float                   # profit_pct at placement time
    exit_txid:          str                     # Kraken txid (updated on reprice)

    # Outcome (written ONCE when position closes)
    exit_price:         float | null
    exit_cost:          float | null            # Kraken actual_cost
    exit_fee:           float | null            # Kraken actual_fee
    exit_time:          float | null
    exit_regime:        str | null
    net_profit:         float | null
    close_reason:       str | null              # see §4.3

    # Status
    status:             "open" | "closed"
    times_repriced:     int                     # incremented on each reprice
```

**Mutability rules:**
- `entry_*` fields: write-once at creation. Never modified.
- `current_exit_price`, `exit_txid`, `times_repriced`: mutable while
  `status = "open"`. Updated on reprice events.
- `original_exit_price`: write-once at creation. Never modified.
- `exit_*`, `net_profit`, `close_reason`: written exactly once when
  `status` transitions from `"open"` to `"closed"`.
- `status`: transitions `"open" → "closed"` once. Never reopened.

### 4.2 Table: position_journal

Append-only. Every action taken on a position gets a row. Never updated
or deleted.

```
position_journal:
    journal_id:         auto-increment
    position_id:        FK → position_ledger
    timestamp:          float                   # unix timestamp
    event_type:         str                     # see §4.4
    details:            json                    # event-specific payload
```

### 4.3 Close Reasons

| close_reason | Meaning |
|---|---|
| `"filled"` | Exit filled normally on Kraken. Happy path. |
| `"written_off"` | Operator or policy closed position at a loss. |
| `"soft_closed"` | Manual slot release. |
| `"cancelled"` | Churner timeout — position never completed. |

Note: repricing does NOT close a position. It updates
`current_exit_price`, `exit_txid`, and `times_repriced` in-place.
The position stays open. The journal records the reprice event.

### 4.4 Journal Event Types

| event_type | When | details payload |
|---|---|---|
| `"created"` | Entry filled, exit placed | `{entry_price, exit_price, regime, slot_mode}` |
| `"repriced"` | Exit moved | `{old_price, new_price, old_txid, new_txid, reason, subsidy_consumed}` where reason is `"tighten"` / `"subsidy"` / `"operator"` |
| `"filled"` | Exit filled on Kraken | `{fill_price, fill_cost, fill_fee, net_profit}` |
| `"written_off"` | Closed at loss | `{close_price, realized_loss, reason}` |
| `"cancelled"` | Churner timeout | `{reason, age_seconds}` |
| `"churner_profit"` | Churner cycle completed | `{net_profit, churner_cycle_id}` — credits subsidy |
| `"over_performance"` | Exit filled above target | `{expected_profit, actual_profit, excess}` — credits subsidy |

### 4.5 Module API

New file: `position_ledger.py`. Three core functions plus queries.

```python
def open_position(slot_id, trade_id, slot_mode, cycle,
                  entry_data, exit_data) -> int:
    """Create a position record when entry fills and exit is placed.
    Returns position_id."""

def journal_event(position_id, event_type, details) -> int:
    """Append a journal entry. Returns journal_id."""

def close_position(position_id, outcome_data) -> None:
    """Fill in outcome fields and set status='closed'.
    Also writes a journal entry for the close event."""
```

Query helpers (derived, not stored):

```python
def get_open_positions(slot_id=None) -> list:
    """All positions where status='open'. Optionally filter by slot."""

def get_subsidy_balance(slot_id) -> float:
    """Derived from journal: sum of churner_profit + over_performance
    minus sum of subsidy_consumed from reprice events."""

def get_position_history(slot_id=None, limit=50) -> list:
    """Closed positions, most recent first."""
```

### 4.6 Call Sites in bot.py

The ledger functions are called from the runtime layer (bot.py), not
from the reducer (state_machine.py). The reducer stays pure — ledger
writes happen after the reducer returns actions.

| Event | Call |
|---|---|
| Entry fill → exit placed | `open_position(...)` + `journal_event("created", ...)` |
| Exit repriced (any reason) | `journal_event("repriced", ...)` + update position fields |
| Exit filled | `close_position(...)` (writes "filled" journal entry internally) |
| Churner cycle completes | `open_position(...)` + `close_position(...)` for the churner round-trip, plus `journal_event("churner_profit", ...)` on the parent stuck position |
| Operator write-off | `close_position(...)` with close_reason="written_off" |

---

## 5. Subsidy Accounting

### 5.1 Derived, Not Accumulated

Subsidy balance is **derived from the journal**, not stored as a mutable
accumulator. This follows the Dust-Proof Ledger principle: derived values
can't drift.

```python
def get_subsidy_balance(slot_id) -> float:
    credits = sum(
        e.details["net_profit"]
        for e in journal
        where e.position_id in open_positions_for(slot_id)
        and e.event_type in ("churner_profit", "over_performance")
    )
    debits = sum(
        e.details["subsidy_consumed"]
        for e in journal
        where e.position_id in all_positions_for(slot_id)
        and e.event_type == "repriced"
        and e.details.get("reason") == "subsidy"
    )
    return credits - debits
```

For performance, a cached value is recomputed once per main loop cycle
(not on every read). The journal is the source of truth; the cache is
a convenience.

### 5.2 Subsidy Sources

1. **Churner profits**: When a churner cycle completes, its net profit
   is journaled as `"churner_profit"` on the stuck position it's healing.
2. **Over-performance**: If a sticky exit fills at a price better than
   target, the excess is journaled as `"over_performance"`.

### 5.3 Subsidy Consumption

When a subsidized reprice occurs:

```
subsidy_consumed = abs(old_exit_price - new_exit_price) × volume
```

This is recorded in the `"repriced"` journal entry's details as
`"subsidy_consumed"`. The subsidy balance decreases by this amount
(derived from journal sum, not a direct debit).

**Invariant**: `get_subsidy_balance(slot_id) >= 0` always. A subsidized
reprice is only attempted if the derived balance covers the consumption.
If insufficient, a partial reprice moves the exit as far as the balance
allows.

### 5.4 Lifetime Aggregates

For dashboard display, aggregate counters are derived from the journal:

```python
subsidy_lifetime_earned = sum(credits from all journal entries for slot)
subsidy_lifetime_consumed = sum(debits from all journal entries for slot)
```

When journal entries are trimmed from local storage (§11.2), their
contributions are captured in watermark counters before removal:

```
GridState:
    subsidy_earned_watermark: float     # sum of trimmed credit entries
    subsidy_consumed_watermark: float   # sum of trimmed debit entries
```

Lifetime total = watermark + sum(active journal entries).

### 5.5 Subsidy Dashboard Summary

```
Subsidy Health
  Pool:  $0.42 available (slot 3: $0.18, slot 7: $0.24)
  Earned:  $2.35 lifetime (churner: $1.89, over-perf: $0.46)
  Spent:   $1.93 on 7 reprices
  Pending: $0.85 needed across 2 stuck positions
  ETA:     ~14h at current churner rate
```

---

## 6. Churner Mode

### 6.1 Activation Criteria

A slot's churner activates when ALL of the following are true:

1. The slot has an open position in age band "aging" or worse.
2. HMM consensus is "ranging" (not trending/bearish/bullish).
3. The slot has available capital for a minimum-size order (own idle
   capital, or global reserve has capacity — see §6.5).
4. Capacity headroom >= `CHURNER_MIN_HEADROOM` open orders (default: 10).
5. The slot is not already in churner mode.

The churner **deactivates** when ANY of the following are true:

1. The stuck exit fills (slot is unstuck — churner's job is done).
2. HMM consensus shifts away from "ranging."
3. Capacity headroom drops below `CHURNER_MIN_HEADROOM`.
4. Subsidy balance is sufficient to fully reprice the stuck exit and
   a reprice has been executed (churner is no longer needed).

### 6.2 Churner Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Entry distance | `CHURNER_ENTRY_PCT` (default: 0.15%) | Half of normal `PAIR_ENTRY_PCT`. Tight to current price for fast fill. |
| Profit target | `CHURNER_PROFIT_PCT` (default: `ROUND_TRIP_FEE_PCT + 0.10`) | ~0.60%. Just enough to cover fees + 0.10% margin. |
| Order size | `CHURNER_ORDER_SIZE_USD` (default: `ORDER_SIZE_USD`) | Same as normal. Uses idle capital from the slot. |
| Timeout | `CHURNER_TIMEOUT_SEC` (default: 300) | 5 minutes. If entry doesn't fill, cancel and re-enter at current price. |
| Exit timeout | `CHURNER_EXIT_TIMEOUT_SEC` (default: 600) | 10 minutes. If exit doesn't fill, cancel and re-enter fresh. No stickiness. |

### 6.3 Churner Lifecycle

```
IDLE ──(exit ages past threshold + regime=ranging)──► ACTIVE
  ▲                                                      │
  │         ┌──────────────────────────────────────┐     │
  │         │  Place entry near current price       │     │
  │         │  Wait up to CHURNER_TIMEOUT_SEC      │     │
  │         │  If entry fills → place tight exit    │     │
  │         │  Wait up to CHURNER_EXIT_TIMEOUT_SEC │     │
  │         │  If exit fills → book profit to       │     │
  │         │    subsidy (via journal), loop        │     │
  │         │  If timeout → cancel, re-enter        │     │
  │         └──────────────────────────────────────┘     │
  │                                                      │
  └──(stuck exit fills OR regime shifts OR repriced)──◄──┘
```

### 6.4 Churner vs Sticky: Same Slot, Two Personalities

A slot in churner mode is doing two things simultaneously:

1. **Holding** its stuck exit on Kraken (passive, costs nothing)
2. **Churning** with a separate entry/exit pair near current price

These are tracked as separate concerns. The churner orders have
`order_role = "churner_entry"` / `"churner_exit"` to distinguish them
from the stuck exit (`order_role = "exit"`).

Churner round-trips get their own `position_ledger` records with
`slot_mode = "churner"`. The stuck position's record stays open with
`slot_mode = "sticky"`. The link between them is the `slot_id` — both
positions belong to the same slot.

The stuck exit and churner orders coexist on Kraken — they're
independent limit orders at different prices.

### 6.5 Capital: Per-Slot with Shared Backstop

When a slot enters churner mode, its capital is split:

- **Locked**: Capital tied up in the stuck position (entry cost).
  Unavailable — it's the position itself.
- **Idle**: Remaining capital in the slot. This funds the churner.
- **Reserve backstop**: If the slot has zero idle capital, it can
  borrow from the global `CHURNER_RESERVE_USD` pool up to one
  `CHURNER_ORDER_SIZE_USD`. The reserve is replenished by churner
  profits that exceed the subsidy needed (surplus after healing).

This is a hybrid approach: per-slot ownership for the common case,
shared reserve for the edge case of fully-locked slots. The reserve
is first-come-first-served with a hard cap per slot (one order size).

A slot with `ORDER_SIZE_USD = $2` and a stuck position locking $2 may
have insufficient idle capital. The reserve covers this. Without the
reserve, the slot simply waits.

### 6.6 Churner Profit Routing

While a stuck exit needs subsidy:
- **100% of churner profit → subsidy pool** (via journal entry on the
  stuck position).

Once the stuck exit is healed (repriced and filled, or subsidy pool
exceeds `subsidy_needed`):
- **100% of churner profit → compounds** into the churner's own order
  size. This builds a larger base for the next time this slot gets stuck.

The routing decision is a simple check each time a churner cycle
completes:

```python
if get_subsidy_balance(slot_id) < subsidy_needed_for_stuck_position:
    journal_event(stuck_position_id, "churner_profit", {net_profit})
else:
    # Compound: increase churner order size for next cycle
    slot.churner_order_size += net_profit
```

### 6.7 Ranging Regime Suitability

Why ranging only:

- **Ranging**: Price oscillates ±0.15% frequently. Tight entries fill
  in minutes. Tight exits fill in minutes. Churner cycle velocity is
  high. Expected: 4-12 cycles/day per churner.
- **Trending (up or down)**: Price moves directionally. Entries on one
  side fill immediately but exits never fill (or vice versa). Churner
  gets stuck — defeating the purpose.
- **Volatile ranging**: Whipsaw periods the HMM may classify as ranging.
  Churner timeouts (5min entry, 10min exit) protect against this — a
  timed-out churner wastes 2 API calls but loses no money. At minimum
  order size ($2), worst case is a $2 stuck position which the sticky
  system already handles. No additional volatility gate for v0.2.

The HMM consensus provides the regime signal. Churners respect it with
no override. If the HMM says ranging, churners grind. If it says
trending, churners pause.

---

## 7. Graduated Repricing

### 7.1 Age Bands

Band assignment uses **both** age and distance. A 4-day-old exit that's
only 1% from market is still "fresh" in effective terms — it might fill
any moment. Conversely, a 2-hour-old exit that's 10% from market after
a flash move is effectively "stuck" immediately.

**Band formula:**

```
effective_age = age_seconds × (1 + distance_pct / AGE_DISTANCE_WEIGHT)
```

This weights distance into the aging calculation. With the default
`AGE_DISTANCE_WEIGHT = 5.0`, an exit 10% away ages 3× faster than
one 0% away.

| Band | effective_age threshold | Action |
|------|------------------------|--------|
| **Fresh** | < 6h (21600s) | None. Actively working. |
| **Aging** | 6h – 24h | Monitor. Churner may activate if ranging. |
| **Stale** | 24h – 72h | Tighten exit toward current profit target if originally wider (§7.2). |
| **Stuck** | 72h – 168h | Reprice toward breakeven+fees using subsidy (§7.3). Churner active. |
| **Write-off** | > 168h | Present to operator as cleanup candidate (§7.5). |

Age bands are **derived** each cycle from `entry_time` and current
market price. They are not stored on the position record.

### 7.2 Tighten Reprice (Stale Band)

This is a "free" reprice — no subsidy needed. It happens when an exit
was placed at a wider profit target than current conditions warrant.

**Target**: `max(profit_pct, _volatility_profit_pct())`. This respects
the user's base `profit_pct` as a floor while adapting to the volatility
system. The tighten only fires when the original placement was wider
than both the user's base target and the current volatility target.

**Trigger condition:**

```
target_profit_pct > tighten_target + 0.1%
```

For example: exit was placed at 1.8% profit during a volatile period.
Volatility has calmed, current volatility-adjusted target is 1.2%, user's
base is 1.0%. Tighten target = max(1.0, 1.2) = 1.2%. Since 1.8 > 1.3
(1.2 + 0.1 hysteresis), the exit is repriced to 1.2%.

**Frequency**: Once per position, on band transition to stale. Not
repeated unless the position is repriced and re-enters the stale band.

### 7.3 Subsidized Reprice (Stuck Band)

Consumes subsidy to bring the exit into fillable range:

```
1. Compute fillable_price:
   Sell exit: max(market × (1 + entry_pct), entry_price × (1 + fee_floor))
   Buy exit:  min(market × (1 - entry_pct), entry_price × (1 - fee_floor))

2. subsidy_needed = abs(current_exit_price - fillable_price) × entry_volume

3. subsidy_available = get_subsidy_balance(slot_id)

4. If subsidy_available >= subsidy_needed:
     → Full reprice to fillable_price
   Else:
     → Partial reprice: move exit as far as subsidy allows
       new_exit = current_exit ∓ (subsidy_available / entry_volume)

5. Journal entry: "repriced" with {old_price, new_price, reason="subsidy",
   subsidy_consumed=abs(old-new)×volume}
```

Partial reprices are fine. A position might get repriced 3-4 times as
subsidy accumulates, each time getting closer to fillable range.

### 7.4 Reprice Cooldown

Repricing has API cost (cancel + place = 2 private calls). Cooldown:

- **Tighten reprices**: Once per position, on band transition.
- **Subsidized reprices**: At most once per `SUBSIDY_REPRICE_INTERVAL_SEC`
  (default: 3600 — hourly). Subsidy accumulates between reprices.

### 7.5 The Write-Off Decision

Positions in the write-off band are presented on the dashboard as a
cleanup queue:

```
Cleanup Queue (2 positions)
  Slot 3 / B.7: sell exit @ $0.0985, entry @ $0.0910
    Age: 9 days | Distance: 12.3% | Subsidy: $0.18 / $0.62 needed
    [Reprice to Breakeven] [Close at Market] [Keep Holding]

  Slot 7 / A.12: buy exit @ $0.0812, entry @ $0.0880
    Age: 11 days | Distance: 8.1% | Subsidy: $0.31 / $0.44 needed
    [Reprice to Breakeven] [Close at Market] [Keep Holding]
```

The operator can:
1. **Reprice to breakeven** — consumes subsidy if available, accepts
   reduced profit for the remainder.
2. **Close at market** — cancel the exit, place a market order to close
   the position. Realized loss is booked. Slot is freed immediately.
3. **Keep holding** — position stays. Resets the write-off timer.

Future: an automated policy can close positions where
`opportunity_cost > subsidy_needed × 2` (i.e., the cost of waiting
exceeds the cost of closing). Deferred — operator approval first.

### 7.6 Opportunity Cost (Derived)

Used for dashboard display and future automated write-off policy:

```
opportunity_cost_usd = (age_seconds / 3600)
    × avg_hourly_throughput
    × (entry_cost / total_capital)
```

Where `avg_hourly_throughput` comes from the throughput sizer's
`profit / time_locked` metric for the current regime.

### 7.7 Subsidy Needed (Derived)

```
For sell exits (Trade B — needs price to go UP to fill):
    fillable_price = max(market × (1 + entry_pct), entry_price × (1 + fee_floor))
    if current_exit_price <= fillable_price:
        subsidy_needed = 0  (already fillable)
    else:
        subsidy_needed = (current_exit_price - fillable_price) × entry_volume

For buy exits (Trade A — needs price to go DOWN to fill):
    fillable_price = min(market × (1 - entry_pct), entry_price × (1 - fee_floor))
    if current_exit_price >= fillable_price:
        subsidy_needed = 0
    else:
        subsidy_needed = (fillable_price - current_exit_price) × entry_volume
```

---

## 8. Flywheel Dynamics

The self-healing system creates a positive feedback loop:

```
Stuck exit detected
  └─► Churner activates (if ranging)
       └─► Small, fast cycles generate profit
            └─► Profit → subsidy (via journal)
                 └─► Subsidy funds reprice
                      └─► Exit moved closer to market
                           └─► Exit fills (or gets closer)
                                └─► Slot freed
                                     └─► Slot returns to sticky mode
                                          └─► More capital working
                                               └─► Higher throughput
```

### 8.1 Estimated Healing Rates

At DOGE ~ $0.09, `ORDER_SIZE_USD = $2`, `CHURNER_PROFIT_PCT = 0.60%`:

| Metric | Value |
|--------|-------|
| Churner profit per cycle | ~$0.012 (after fees) |
| Cycles per day (ranging) | ~8-12 |
| Churner profit per day | ~$0.10-0.14 |
| Typical subsidy needed (stuck 5%) | ~$0.10-0.50 |
| Time to heal one stuck position | ~1-4 days |

Slow but steady and **automatic**. No operator intervention. The slot
grinds its way out. During that time, the original stuck exit is still
live on Kraken — if price bounces back, it fills on its own and the
subsidy is pure bonus.

### 8.2 Bootstrap Problem

If ALL slots are stuck simultaneously (e.g., flash crash), there are
no churners generating profit. Solutions in order of preference:

1. **Reserve capital**: `CHURNER_RESERVE_USD` (default: $5) funds
   churners even when all slot capital is locked.
2. **Operator injection**: Dashboard "Deploy Rescue Capital" button
   allocates fresh USD to the worst-stuck slots' churners.
3. **Accept the wait**: All slots stuck = bot is paused. When price
   enters a ranging period near any stuck exit, those exits fill
   naturally. This is what sticky slots already do.

---

## 9. Persistence

### 9.1 Local: state.json

Only **open** positions are stored in state.json. Closed positions are
removed after being written to Supabase (or after a configurable retention
period if Supabase is unavailable).

```json
{
  "position_ledger": [
    {
      "position_id": 47,
      "slot_id": 3,
      "trade_id": "B",
      "slot_mode": "sticky",
      "cycle": 7,
      "entry_price": 0.0910,
      "entry_cost": 1.82,
      "entry_fee": 0.0047,
      "entry_volume": 20.0,
      "entry_time": 1739650000,
      "entry_regime": "ranging",
      "entry_volatility": 0.42,
      "current_exit_price": 0.0921,
      "original_exit_price": 0.0921,
      "target_profit_pct": 1.20,
      "exit_txid": "OXXXX-XXXXX",
      "status": "open",
      "times_repriced": 0
    }
  ],
  "position_journal_recent": [
    {
      "journal_id": 203,
      "position_id": 47,
      "timestamp": 1739650001,
      "event_type": "created",
      "details": {"entry_price": 0.0910, "exit_price": 0.0921, "regime": "ranging", "slot_mode": "sticky"}
    }
  ],
  "position_id_counter": 48,
  "journal_id_counter": 204,
  "subsidy_earned_watermark": 1.45,
  "subsidy_consumed_watermark": 1.12
}
```

The journal keeps the last `POSITION_JOURNAL_LOCAL_LIMIT` entries
(default: 500). When entries are trimmed:

1. Sum trimmed credit entries → add to `subsidy_earned_watermark`
2. Sum trimmed debit entries → add to `subsidy_consumed_watermark`
3. Remove trimmed entries from local storage

Watermarks ensure lifetime aggregates survive journal trimming.

### 9.2 Supabase

Two new tables, auto-detected (same pattern as existing tables —
if the tables exist, write to them; if not, local-only):

```sql
create table position_ledger (
    position_id         bigserial primary key,
    slot_id             int not null,
    trade_id            text not null,
    slot_mode           text not null,
    cycle               int not null,

    entry_price         double precision not null,
    entry_cost          double precision,
    entry_fee           double precision,
    entry_volume        double precision not null,
    entry_time          double precision not null,
    entry_regime        text,
    entry_volatility    double precision,

    current_exit_price  double precision,
    original_exit_price double precision,
    target_profit_pct   double precision,
    exit_txid           text,

    exit_price          double precision,
    exit_cost           double precision,
    exit_fee            double precision,
    exit_time           double precision,
    exit_regime         text,
    net_profit          double precision,
    close_reason        text,

    status              text not null default 'open',
    times_repriced      int not null default 0,

    created_at          timestamptz default now()
);

create index idx_position_ledger_status on position_ledger(status);
create index idx_position_ledger_slot on position_ledger(slot_id);

create table position_journal (
    journal_id          bigserial primary key,
    position_id         bigint references position_ledger(position_id),
    timestamp           double precision not null,
    event_type          text not null,
    details             jsonb not null default '{}',

    created_at          timestamptz default now()
);

create index idx_position_journal_position on position_journal(position_id);
create index idx_position_journal_type on position_journal(event_type);
```

**Write cadence**: Position opens/closes and journal events are written
to Supabase on the same cadence as `bot_state` upserts (end of each
main loop cycle). The journal is append-only — inserts only, never
updates.

### 9.3 Migration

On first startup after deployment:

1. Scan all open exits across all slots.
2. Create a `position_ledger` record for each, using available data:
   - `entry_price` from `matched_buy_price` / `matched_sell_price`
   - `entry_cost` / `entry_fee` from `matched_entry_cost` /
     `matched_entry_fee` (populated by Dust-Proof Ledger; fall back
     to estimate if unavailable)
   - `entry_volume` from GridOrder.volume
   - `entry_time` from `entry_filled_at`
   - `target_profit_pct` from current `state.profit_pct` (best guess
     for legacy positions without recorded intent)
   - `original_exit_price` = `current_exit_price` = GridOrder.price
   - `slot_mode` = `"legacy"` (pre-ledger positions)
3. Write a `"created"` journal entry for each (with `migration=true`
   in details).
4. Set all counters and watermarks to 0.
5. Log migration summary.

No data loss. Legacy positions start with approximate records that
improve as new events add actual data.

---

## 10. Integration with Existing Systems

### 10.1 Throughput Sizer

The throughput sizer's `profit / time_locked` metric naturally penalizes
stuck slots. Integration:

- Churner cycles are tracked separately (slot_mode="churner") — they
  don't contaminate the sticky slot's fill-time statistics.
- Churner cycles feed into the "ranging" regime bucket specifically.
- Age pressure uses position ledger age bands instead of raw max age.

### 10.2 HMM Regime Detection

No changes to HMM. Churner mode reads the consensus signal:

```python
if hmm_consensus == "ranging" and slot.has_stale_exit():
    slot.activate_churner()
```

### 10.3 Dust-Proof Ledger

The position ledger extends the Dust-Proof Ledger's actual-fee tracking:

- `entry_cost` and `entry_fee` come directly from Kraken's actual
  `cost` and `fee` fields (captured by Dust-Proof).
- Subsidy calculations use actual costs, not estimates.
- Rounding residuals from churner orders are recycled per the existing
  `rounding_residual_a/b` mechanism.

### 10.4 Dashboard

New/modified panels:

1. **Position Age Heatmap**: Visual bar showing distribution of exit
   ages across bands (fresh=green, aging=yellow, stale=orange,
   stuck=red, write_off=purple).
2. **Subsidy Health Card**: Pool balance, lifetime stats, ETA to clear
   queue (see §5.5).
3. **Churner Activity**: Active churners count, cycles today, profit
   today.
4. **Cleanup Queue**: Write-off band positions with action buttons
   (see §7.5).

### 10.5 Capacity Budget

Each churner adds up to 2 open orders (entry + exit) to the Kraken
order book. Capacity check:

```
churner_headroom = total_headroom - CHURNER_MIN_HEADROOM
max_active_churners = churner_headroom / 2
```

If capacity is tight, churners are the first to be paused (sticky
exits have priority since they're already on the book).

---

## 11. Configuration

### 11.1 Churner Config

| Variable | Default | Description |
|----------|---------|-------------|
| `CHURNER_ENABLED` | `false` | Master toggle for churner mode |
| `CHURNER_ENTRY_PCT` | `0.15` | Entry distance from current price (%) |
| `CHURNER_PROFIT_PCT` | `0.60` | Profit target (%). Must exceed `ROUND_TRIP_FEE_PCT`. |
| `CHURNER_ORDER_SIZE_USD` | `ORDER_SIZE_USD` | Order size for churner cycles |
| `CHURNER_TIMEOUT_SEC` | `300` | Entry timeout — cancel if unfilled after 5 min |
| `CHURNER_EXIT_TIMEOUT_SEC` | `600` | Exit timeout — cancel if unfilled after 10 min |
| `CHURNER_MIN_HEADROOM` | `10` | Minimum open-order headroom before churners activate |
| `CHURNER_RESERVE_USD` | `5.00` | Global capital reserved for churner bootstrap |

### 11.2 Subsidy Config

| Variable | Default | Description |
|----------|---------|-------------|
| `SUBSIDY_ENABLED` | `false` | Master toggle for subsidy repricing |
| `SUBSIDY_REPRICE_INTERVAL_SEC` | `3600` | Minimum time between subsidized reprices (per position) |
| `SUBSIDY_AUTO_REPRICE_BAND` | `stuck` | Earliest band at which subsidized reprice is allowed |
| `SUBSIDY_WRITE_OFF_AUTO` | `false` | If true, auto-close write-off positions when opportunity cost > 2x subsidy needed |

### 11.3 Age Band Config

| Variable | Default | Description |
|----------|---------|-------------|
| `AGE_BAND_FRESH_SEC` | `21600` | effective_age threshold: fresh (6h) |
| `AGE_BAND_AGING_SEC` | `86400` | effective_age threshold: aging (24h) |
| `AGE_BAND_STALE_SEC` | `259200` | effective_age threshold: stale (72h) |
| `AGE_BAND_STUCK_SEC` | `604800` | effective_age threshold: stuck (168h) |
| `AGE_DISTANCE_WEIGHT` | `5.0` | Divisor for distance weighting in effective_age formula |

### 11.4 Ledger Config

| Variable | Default | Description |
|----------|---------|-------------|
| `POSITION_LEDGER_ENABLED` | `true` | Master toggle — can deploy ledger without churner/subsidy |
| `POSITION_JOURNAL_LOCAL_LIMIT` | `500` | Max journal entries in state.json (older trimmed to watermark) |

---

## 12. Testing Plan

| Test | Description |
|------|-------------|
| **Ledger core** | |
| `test_open_position_creates_record` | Entry fills, exit placed → position_ledger record with correct fields, status="open". |
| `test_open_position_immutable_entry` | Attempt to modify entry_* fields after creation → raises or is rejected. |
| `test_close_position_writes_outcome` | Exit fills → outcome fields populated, status="closed", journal entry written. |
| `test_close_position_idempotent` | Closing an already-closed position → no-op or error, not double-close. |
| `test_journal_append_only` | Journal entries cannot be modified or deleted after creation. |
| `test_journal_event_types` | Each event type produces correctly structured details payload. |
| **Subsidy accounting** | |
| `test_subsidy_balance_derived` | Subsidy balance = sum(credits) - sum(debits) from journal. |
| `test_subsidy_never_negative` | Reprice request > balance → partial reprice, balance stays >= 0. |
| `test_subsidy_watermark_on_trim` | Trim old journal entries → watermark captures their totals. |
| `test_churner_profit_routes_to_subsidy` | Churner cycle completes while stuck exit exists → journal "churner_profit" entry on stuck position. |
| `test_churner_profit_compounds_after_heal` | Stuck exit healed → subsequent churner profit compounds into order size. |
| **Age bands** | |
| `test_age_band_fresh` | Position < 6h effective age → band = "fresh". |
| `test_age_band_distance_weighting` | Position 2h old but 10% away → effective_age = 2h × 3.0 = 6h → "aging". |
| `test_age_band_close_exit_stays_fresh` | Position 5 days old but 0.5% from market → effective_age low → "fresh". |
| **Churner mode** | |
| `test_churner_activates_on_ranging` | Stale exit + HMM=ranging → churner starts. |
| `test_churner_pauses_on_trending` | Active churner + HMM shifts to bullish → churner stops. |
| `test_churner_respects_capacity` | Headroom < CHURNER_MIN_HEADROOM → churner does not activate. |
| `test_churner_timeout_cancels_entry` | Entry unfilled after CHURNER_TIMEOUT_SEC → cancelled, new entry placed. |
| `test_churner_uses_reserve_when_locked` | Slot has zero idle capital → borrows from CHURNER_RESERVE_USD. |
| `test_churner_position_records` | Churner round-trip creates position_ledger record with slot_mode="churner". |
| **Graduated repricing** | |
| `test_tighten_reprice_free` | Stale exit with wide target → repriced to max(profit_pct, vol_adjusted), no subsidy consumed. |
| `test_tighten_reprice_respects_floor` | profit_pct=1.0, vol_adjusted=0.8 → tighten target is 1.0 (floor). |
| `test_subsidized_reprice_full` | Subsidy >= needed → exit repriced to fillable_price, journal entry with subsidy_consumed. |
| `test_subsidized_reprice_partial` | Subsidy < needed → exit moved as far as subsidy allows. |
| `test_reprice_cooldown` | Reprice attempted within cooldown → skipped. |
| **Integration** | |
| `test_flywheel_integration` | Full scenario: entry → stuck → churner → grind → subsidy reprice → exit fill. |
| `test_migration_creates_records` | Load old state.json without position_ledger → records created from open orders with slot_mode="legacy". |
| `test_supabase_write_on_close` | Position closes → row upserted in Supabase position_ledger table. |
| `test_journal_supabase_insert` | Journal event → row inserted in Supabase position_journal table. |
| `test_write_off_queue` | Position in write_off band → appears in cleanup queue API response. |

---

## 13. Rollout

### Stage A: Ledger Only (observe)

Deploy `position_ledger.py` with the three-function API. Wire into
bot.py at entry fill, reprice, and exit fill call sites. No churners,
no repricing. Dashboard shows position age heatmap derived from ledger.
Observe for 48h to validate records are created correctly and age band
distribution matches intuition.

**Feature flags**: `POSITION_LEDGER_ENABLED=true`, `CHURNER_ENABLED=false`,
`SUBSIDY_ENABLED=false`

### Stage B: Tighten Repricing (low risk)

Enable tighten reprices for stale-band positions. These are "free"
reprices (no subsidy needed, just narrowing a wide exit). Monitor fill
rates for repriced exits vs. un-repriced.

**Feature flags**: `SUBSIDY_ENABLED=true`, `SUBSIDY_AUTO_REPRICE_BAND=stale`,
`CHURNER_ENABLED=false`

### Stage C: Churner Mode (ranging only)

Enable churner mode. Observe churner cycle velocity, profit per cycle,
subsidy accumulation rate. Validate churners deactivate cleanly on
regime shift. Verify churner position records appear in ledger with
correct slot_mode.

**Feature flags**: `CHURNER_ENABLED=true`

### Stage D: Subsidized Repricing (full system)

Enable subsidized repricing for stuck-band positions. Monitor the
flywheel: churner profit → subsidy → reprice → exit fill. Measure
time-to-heal for stuck positions.

**Feature flags**: `SUBSIDY_AUTO_REPRICE_BAND=stuck`

### Stage E: Write-Off Policy (optional)

If comfortable, enable auto write-off for positions where opportunity
cost far exceeds closure cost. Start with `SUBSIDY_WRITE_OFF_AUTO=false`
(manual only via dashboard).

---

## 14. Rate Limit Budget

| Action | Private calls | Frequency |
|--------|---------------|-----------|
| Churner entry place | 1 | Per churner cycle (~8-12/day/slot when ranging) |
| Churner entry cancel (timeout) | 1 | ~50% of attempts (price moved away) |
| Churner exit place | 1 | Per successful entry fill |
| Churner exit cancel (timeout) | 1 | ~20% of exits (conservative) |
| Subsidized reprice (cancel + place) | 2 | Max 1/hour per position |

Worst case with 5 active churners: ~40-60 extra private calls/day.
At 15-counter per Kraken cycle, this adds ~1-2 calls per 30s cycle.
Well within budget.

---

## 15. Relationship to Other Specs

| Spec | Relationship |
|------|-------------|
| **Sticky Slots v0.2** | Self-Healing extends sticky slots with active healing rather than passive patience. Sticky slots remain the default; churner mode is the exception. |
| **Dust-Proof Ledger** | Foundation. Actual fee/cost data feeds position records. Rounding residual recycling applies to churner orders too. |
| **Throughput Sizer** | Provides `profit / time_locked` metric used for opportunity cost. Churner cycles are tracked separately (slot_mode="churner") from sticky cycles. |
| **Recovery Order Deprecation** | Self-Healing replaces recovery orders entirely. Recovery orders were the "chaotic" approach; subsidy repricing is the "deliberate" approach. |
| **Strategic Capital Deployment** | DCA accumulation and churner mode are complementary. Accumulation deploys idle USD into DOGE directionally; churner deploys idle slot capital into fast cycles. Different capital pools, different triggers. |
| **Capacity Telemetry** | Churner orders consume open-order capacity. Telemetry's headroom check gates churner activation. |

---

## 16. Summary

| Component | What It Does |
|-----------|-------------|
| **position_ledger table** | Immutable entry context + mutable exit state + write-once outcome per position |
| **position_journal table** | Append-only event history: created, repriced, filled, written_off, churner_profit |
| **position_ledger.py** | Three-function API: `open_position()`, `journal_event()`, `close_position()` |
| **Age Bands** | Five graduated bands (fresh → write_off) with distance-weighted aging, derived each cycle |
| **Churner Mode** | Regime-gated fast cycling near current price; profit routes to subsidy via journal |
| **Subsidy Accounting** | Derived from journal entries (not mutable accumulators); per-slot with shared reserve backstop |
| **Graduated Repricing** | Tighten (free, stale band) → subsidize (earned, stuck band) → write-off (operator) |
| **Flywheel** | Churner profit → subsidy → reprice → exit fills → slot freed → more throughput |
| **Cleanup Queue** | Dashboard UI for operator-approved write-offs of worst-stuck positions |
| **Supabase** | Two new tables (auto-detected), append-only journal inserts, position upserts |
