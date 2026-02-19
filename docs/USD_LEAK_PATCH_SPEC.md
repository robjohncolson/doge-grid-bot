# USD Leak Patch Spec

**Version:** 0.1.0
**Status:** Draft
**Supersedes:** Partially addresses gaps left by Account-Aware B-Side Sizing (62a5cb2), Dust-Proof Ledger Spec (draft), USD Dust Sweep Spec (v0.1.0)
**Depends on:** CapitalLedger, state_machine.py, bot.py, grid_strategy.py

---

## 1. Problem Statement

USD accumulates in the Kraken account beyond what is tied up in open
buy orders. The only legitimate USD existence should be committed to
working buy-side orders. Instead, idle USD grows monotonically over
time.

### 1.1 The Smoking Gun: Asymmetric Placement Success

The user's observation is correct: **sell entries (A-side) consistently
succeed while buy entries (B-side) are disproportionately
blocked/cancelled**.

The bot operates a symmetric A/B grid. A-side sells DOGE for USD;
B-side buys DOGE with USD. When A-side fills but B-side fails to
place, a one-way USD pump operates:

```
Sell DOGE entry fills → USD arrives
Buy DOGE entry blocked → USD stays idle
A-side exit (buy DOGE) fills → cycle completes
Next A-side entry fires again → more USD
Still no B-side entry → USD grows
```

### 1.2 Why B-Side Entries Fail More Than A-Side

| Gate | A-side (sell) | B-side (buy) | Asymmetry |
|------|---------------|--------------|-----------|
| **Fund guard** (`_try_reserve_loop_funds`) | Checks DOGE balance (abundant) | Checks USD balance (scarce, shared across all slots) | B blocked when USD is fragmented across too many slots |
| **Entry floor guard** | Same minimum, but DOGE supply is ample | 13 DOGE × price × 1.01 = ~$2.36 minimum; if throughput sizer shrinks below this, blocked | B more likely to hit floor |
| **Capacity gate** | Entries processed sequentially; A-entry placed first | B-entry second; if at cap, B is the one blocked | B loses tie-breaker |
| **Mode degradation** | If sell fails → `long_only` (B runs) | If buy fails → `short_only` (A runs, accumulating USD) | `short_only` creates a positive feedback loop: more A cycles → more idle USD → still can't fund B |
| **Throughput sizer** | May shrink A, but DOGE buffer absorbs it | Shrinks B below minimum → blocked | B hit harder in bearish regimes |

### 1.3 Quantified Leak Sources

Ranked by contribution to idle USD:

| # | Source | Per-Cycle USD Leak | Mechanism | Status |
|---|--------|-------------------|-----------|--------|
| **L1** | Asymmetric placement success | ~$2.00+ per blocked B-entry | Entire order notional stays idle | **Active, primary cause** |
| **L2** | Ranger profits invisible to allocation | ~$0.01–0.05/cycle | Profit settles on Kraken, no slot tracks it | Active |
| **L3** | Churner profits routed to subsidy/compound | ~$0.01–0.05/cycle | Goes to `compound_usd` or position_ledger journal, not slot `total_profit` | Active |
| **L4** | `settled_usd` > `total_profit` gap | ~$0.005/cycle | DOGE-side fees don't reduce USD; `total_profit` only subtracts net | Structural, always present |
| **L5** | Fee estimation overshoot | ~$0.001–0.005/cycle | `MAKER_FEE_PCT` configured at 0.25% but actual tier may be 0.20% | Active if fee tier is wrong |
| **L6** | Rounding residual | ~$0.02/order | `compute_order_volume` rounds, residual sits idle | Code exists, OFF by default |
| **L7** | Legacy B-side sizing (non-quote-first) | Variable | Divides by ALL slots, not buy-ready ones | `QUOTE_FIRST_ALLOCATION` is OFF |
| **L8** | Entry floor inflation | ~$0.10–0.50/blocked entry | Floor bumps order above what budget allows | Active since `dcd8259` |

**L1 is dominant.** When a single B-entry is blocked, ~$2+ of USD goes
idle for the duration. When this happens across multiple slots
simultaneously (e.g., during a sell cascade), $20–50+ of USD can
accumulate instantly.

---

## 2. Design Goals

| Goal | Priority |
|------|----------|
| Eliminate B-side placement asymmetry as primary leak source | P0 |
| Ensure `short_only` degradation is always temporary | P0 |
| Activate existing unshipped fixes (rounding residual, quote-first) | P0 |
| Track ranger/churner profits in the allocation budget | P1 |
| Dashboard visibility into B-side placement failures | P1 |
| Zero additional Kraken API calls | P1 |

### Non-Goals

- New order types or trading strategies.
- DOGE-side dust optimization.
- Changing the S0/S1/S2 state machine transitions.
- Auto-spawning new slots (that's a separate scaling concern).

---

## 3. Patch A: B-Side Placement Recovery Loop

**Problem:** When `_mark_entry_fallback_for_insufficient_funds` sets
`short_only=True, mode_source="balance"`, the slot enters a death
spiral where it only runs A-side, generating more USD that still can't
fund a B-entry because all slots are competing for the same pool.

**Fix:** Add a periodic B-side recovery sweep that attempts to restore
bilateral operation for balance-degraded slots.

### 3.1 Recovery Logic

Run once per main loop, after `begin_loop()` syncs the ledger:

```python
def _recover_balance_degraded_slots(self) -> None:
    """Try to restore B-side for slots stuck in short_only due to
    fund-guard failures.  Run once per loop after ledger sync."""
    if not self.ledger._synced:
        return

    available = self.ledger.available_usd
    if available <= 0:
        return

    min_b_entry_usd = self._minimum_b_entry_usd()
    if available < min_b_entry_usd:
        return

    for sid in sorted(self.slots.keys()):
        st = self.slots[sid].state
        # Only touch balance-degraded short_only slots in S0
        if not st.short_only:
            continue
        if str(getattr(st, "mode_source", "none")) != "balance":
            continue
        if sm.derive_phase(st) != "S0":
            continue
        # Check if we can fund a B-entry now
        if available < min_b_entry_usd:
            break  # No point trying further slots
        # Clear degradation and let normal bootstrap place both entries
        self.slots[sid].state = replace(
            st, short_only=False, long_only=False, mode_source="none"
        )
        logger.info(
            "slot %s: cleared balance degradation (available $%.2f >= min $%.2f)",
            sid, available, min_b_entry_usd,
        )
        available -= min_b_entry_usd  # Optimistic reservation
```

### 3.2 Minimum B-Entry USD Helper

```python
def _minimum_b_entry_usd(self) -> float:
    """Smallest possible B-side entry in USD."""
    market = self.last_price
    if market <= 0:
        return float(config.ORDER_SIZE_USD)
    min_vol = float(self.constraints.get("min_volume", 13.0))
    return max(float(config.ORDER_SIZE_USD), min_vol * market * 1.01)
```

### 3.3 Placement Priority: B-First

Currently `_execute_actions` processes actions in list order.
`sm.transition` emits A-entry before B-entry (since A is processed
first in the state machine).

**Change:** Sort actions so B-side entries are placed **before** A-side
entries when both are pending. This ensures the scarce resource (USD)
is committed to B before the abundant resource (DOGE) is committed to
A. If B fails, the slot still degrades — but at least B got first shot
at the funds.

```python
def _execute_actions(self, slot_id, actions, source):
    # Prioritize buy entries (B-side) over sell entries (A-side)
    # so scarce USD is allocated before abundant DOGE.
    def _entry_priority(action):
        if isinstance(action, sm.PlaceOrderAction) and action.role == "entry":
            return 0 if action.side == "buy" else 1
        return 2  # exits and other actions keep original order
    actions = sorted(actions, key=_entry_priority)
    ...
```

### 3.4 Cooldown on Degradation

To prevent rapid flip-flopping between short_only and bilateral:

```python
self._balance_degrade_cooldown: dict[int, float] = {}  # slot_id -> timestamp
BALANCE_DEGRADE_COOLDOWN_SEC = 120  # 4 main loops at 30s
```

A slot that was just restored from degradation can't be re-degraded
for `BALANCE_DEGRADE_COOLDOWN_SEC` seconds. During cooldown, if a
B-entry fails, the order is simply not placed (slot stays bilateral
with a missing B-entry) rather than flipping to `short_only`.

---

## 4. Patch B: Activate Existing Unshipped Fixes

Three mechanisms are already coded but defaulted OFF. These should be
activated as part of this patch.

### 4.1 QUOTE_FIRST_ALLOCATION → ON

The quote-first path in `_b_side_base_usd()` (bot.py:2378-2409):
- Divides deployable USD only across buy-ready slots
- Subtracts committed buy-side quote
- Maintains carry for rounding residual
- Has safety buffer

This is strictly better than the legacy path for preventing idle USD.
The code has been in production (dormant) since the capital layers
work.

**Change:** `QUOTE_FIRST_ALLOCATION` default from `False` to `True`.

### 4.2 ROUNDING_RESIDUAL_ENABLED → ON

The rounding residual recycler (grid_strategy.py:2760-2780) accumulates
sub-cent remainders and folds them into the next order. Code exists,
tested, persisted in state.json.

**Change:** `ROUNDING_RESIDUAL_ENABLED` default from `False` to `True`.

### 4.3 Reduce ALLOCATION_SAFETY_BUFFER_USD

The quote-first path subtracts a safety buffer from deployable USD.
Default is $0.50, which at $2/order means 25% of one slot's allocation
is permanently withheld.

**Change:** `ALLOCATION_SAFETY_BUFFER_USD` default from `0.50` to
`0.10`.

---

## 5. Patch C: Ranger/Churner Profit Visibility

### 5.1 Problem

Ranger and churner profits settle as USD on Kraken but neither system
updates any slot's `total_profit`. The account-aware B-side sizing
sees the USD (it's in `available_usd`), but:
- A-side compounding uses `total_profit`, which is unaware of
  ranger/churner contributions.
- Throughput sizer's capital utilization penalty may under-count
  deployed capital.

### 5.2 Fix: Aggregate Ancillary Profit Counter

Add a bot-level counter that sums ranger + churner profit. Feed this
into the B-side allocation pool as bonus capital:

```python
self._ancillary_profit_usd: float = 0.0  # ranger + churner lifetime profit
```

Updated in `_ranger_on_exit_fill` and `_churner_route_profit`:
```python
self._ancillary_profit_usd += max(0.0, float(net_profit))
```

In `_b_side_base_usd()` quote-first path, add ancillary profit to the
allocation pool:
```python
allocation_pool = deployable_usd + carry_in + self._ancillary_profit_usd_share()
```

Where `_ancillary_profit_usd_share` returns the portion not already
visible in `available_usd` (to avoid double-counting — the profit is
already in the Kraken balance, which feeds `available_usd`; this
counter is for telemetry and the non-quote-first path's awareness).

Actually, since the quote-first path already uses `available_usd` which
includes ranger/churner USD, the real fix is simpler: **no formula
change needed for quote-first**. The issue is only in the legacy path
and in A-side compounding (which uses `total_profit`).

For A-side, the fix is to add an `ancillary_profit_share` to
`_slot_order_size_usd` when `trade_id == "A"`:

```python
ancillary_share = self._ancillary_profit_usd / max(1, len(self.slots))
base = max(base_order, base_order + slot.state.total_profit + ancillary_share)
```

This ensures A-side entries grow proportionally to real system-wide
earnings, not just per-slot tracked earnings.

### 5.3 Status Payload

```python
"ancillary_profit": {
    "ranger_usd": self._ranger_profit_today,
    "churner_usd": self._churner_profit_total,
    "total_lifetime_usd": self._ancillary_profit_usd,
}
```

---

## 6. Patch D: B-Side Failure Telemetry

### 6.1 Problem

B-side entry failures are logged but not surfaced to the dashboard.
The operator has no visibility into how often B-entries fail or why.

### 6.2 Fix: Rolling Failure Counters

```python
self._b_entry_failures_24h: dict[str, int] = {
    "fund_guard": 0,
    "entry_floor": 0,
    "capacity_gate": 0,
    "throughput_below_min": 0,
    "mode_degraded": 0,
    "scheduler_deferred": 0,
}
self._b_entry_failure_window_start: float = _now()
```

Increment the appropriate counter at each B-entry failure site in
`_execute_actions`:

- `_try_reserve_loop_funds` returns False → `fund_guard`
- `refreshed_vol is None` → `throughput_below_min`
- `_internal_order_count >= _order_cap` → `capacity_gate`
- `_mark_entry_fallback_for_insufficient_funds` → `mode_degraded`

Roll the window daily.

### 6.3 Status Payload

```python
"b_side_health": {
    "failures_24h": self._b_entry_failures_24h,
    "balance_degraded_slots": sum(
        1 for s in self.slots.values()
        if s.state.short_only and str(getattr(s.state, "mode_source", "")) == "balance"
    ),
    "recoveries_24h": self._b_recovery_count_24h,
}
```

### 6.4 Dashboard Card

Small card in summary panel:

```
B-Side Health
  Fund guard blocks: 14 | Floor blocks: 3 | Degraded slots: 2/12
  Last recovery: 4m ago
```

Colored amber when `balance_degraded_slots > 0`, red when > 50% of
slots are degraded.

---

## 7. Patch E: Entry Floor Guard Softening

### 7.1 Problem

The entry floor guard (bot.py:2478-2490) was added in `dcd8259` to
break a throughput sizer death spiral. But it inflates B-side orders
above what some slots can afford, causing fund-guard rejections.

### 7.2 Fix: Floor Applies Only to A-Side

The death spiral it fixed was about the throughput sizer shrinking
entries below exchange minimum, causing no fills, causing the sizer
to shrink further. This primarily affects A-side because A-side sizing
is profit-compounded (can shrink).

B-side sizing is already account-aware (balance-divided), so the
floor is redundant — `compute_order_volume` already returns `None`
when volume is below exchange minimum, which is handled gracefully.

```python
if self._flag_value("ENTRY_FLOOR_ENABLED") and trade_id != "B":
    # Floor guard only for A-side; B-side is account-aware and
    # handles below-minimum via compute_order_volume -> None.
    ...
```

Alternatively, use a softer floor for B-side that clamps to the
actual available share rather than inflating:

```python
if self._flag_value("ENTRY_FLOOR_ENABLED"):
    if trade_id == "B":
        # B-side: don't inflate above what the allocation provides.
        # Just ensure the compute_order_volume minimum is met.
        pass
    else:
        # A-side: apply floor to break throughput sizer death spiral.
        ...
```

---

## 8. Patch F: Short-Only Cascade Breaker

### 8.1 Problem

When multiple slots enter `short_only` simultaneously (e.g., after a
sell cascade fills many A-entries at once), the available USD is
fragmented across many competing B-entries. None can be funded, so all
slots degrade to `short_only`. This is a cascade.

### 8.2 Fix: Priority Ordering for B-Side Restoration

When recovering balance-degraded slots (Patch A), restore them in
order of **most recently degraded first** (LIFO). This concentrates
USD into fewer slots rather than spreading it thin.

Additionally, cap the number of bilateral slots to what the current
USD balance can actually support:

```python
def _max_bilateral_slots(self) -> int:
    """How many slots can run bilateral (A+B) given current USD."""
    available = self.ledger.available_usd if self.ledger._synced else 0.0
    min_b = self._minimum_b_entry_usd()
    if min_b <= 0:
        return len(self.slots)
    return max(1, int(available / min_b))
```

Slots beyond this count stay `short_only` until USD frees up from
B-side exit fills.

---

## 9. Implementation Order

### Phase 1: Stop the Bleeding (Deploy First)

1. **Patch A** — B-side recovery loop + B-first placement priority
2. **Patch E** — Entry floor guard B-side exemption
3. **Patch F** — Short-only cascade breaker

These directly address L1 (the dominant leak) and can be deployed
independently.

### Phase 2: Activate Dormant Fixes

4. **Patch B** — Flip `QUOTE_FIRST_ALLOCATION=True`,
   `ROUNDING_RESIDUAL_ENABLED=True`, reduce safety buffer

These are pre-existing, tested code paths. Just config changes.

### Phase 3: Visibility & Attribution

5. **Patch C** — Ranger/churner ancillary profit counter
6. **Patch D** — B-side failure telemetry + dashboard card

These provide observability but don't directly fix leaks.

---

## 10. Config Changes

| Variable | Old Default | New Default | Phase |
|----------|-------------|-------------|-------|
| `QUOTE_FIRST_ALLOCATION` | `False` | `True` | 2 |
| `ROUNDING_RESIDUAL_ENABLED` | `False` | `True` | 2 |
| `ALLOCATION_SAFETY_BUFFER_USD` | `0.50` | `0.10` | 2 |
| `BALANCE_DEGRADE_COOLDOWN_SEC` | (new) | `120` | 1 |
| `B_SIDE_RECOVERY_ENABLED` | (new) | `True` | 1 |
| `ENTRY_FLOOR_B_SIDE_EXEMPT` | (new) | `True` | 1 |

---

## 11. New State

### 11.1 BotRuntime

```python
self._balance_degrade_cooldown: dict[int, float] = {}  # slot_id -> last_degrade_ts
self._b_recovery_count_24h: int = 0
self._b_entry_failures_24h: dict[str, int] = {}
self._b_entry_failure_window_start: float = _now()
self._ancillary_profit_usd: float = 0.0
```

### 11.2 No New Persistent State

All new fields are runtime-only. No state.json or Supabase changes.
The recovery loop derives everything from existing `short_only` +
`mode_source` + `ledger.available_usd`.

---

## 12. Edge Cases

### 12.1 All USD Consumed by DCA Accumulation

If the DCA engine is actively buying DOGE, `available_usd` drops.
The recovery loop will not restore B-entries it can't fund. This is
correct — DCA has priority over grid B-entries.

### 12.2 User Deposits USD

Extra USD increases `available_usd`, allowing more B-entries to be
restored. The system self-corrects within one loop cycle.

### 12.3 Rapid Price Movement

Price moves can cause A-side sells to fill in bursts (sell cascade).
Multiple B-entries are needed simultaneously. The cascade breaker
(Patch F) limits concurrent restorations to what USD can support.

### 12.4 B-First Priority and A-Side Starvation

Placing B-entries before A-entries in `_execute_actions` could
theoretically starve A-entries if both need the same scarce resource.
But A-entries use DOGE (not USD), so there is no competition. The
only shared resource is the capacity gate (open order count), and
entries of either type consume equally.

### 12.5 Recovery Loop vs. Bootstrap

`_recover_balance_degraded_slots` only clears flags; it doesn't place
orders. The actual B-entry placement happens in the normal bootstrap
path that runs for S0 slots. This avoids duplicating placement logic.

---

## 13. Rate Limit Impact

**Zero.** All patches use existing data from `get_balance()` and
`QueryOrders`. No new API calls.

---

## 14. Testing Plan

| Test | Description |
|------|-------------|
| `test_b_entry_blocked_sets_short_only` | Fund guard blocks B-entry → slot enters short_only mode_source=balance |
| `test_recovery_clears_balance_degradation` | After USD becomes available, recovery loop clears short_only |
| `test_recovery_respects_cooldown` | Slot degraded < 120s ago → not recovered yet |
| `test_recovery_caps_at_available_usd` | Only restores N slots where N = available_usd / min_b_entry |
| `test_b_first_placement_priority` | In _execute_actions, B-entry processed before A-entry |
| `test_entry_floor_exempt_b_side` | B-side order not inflated by entry floor guard |
| `test_cascade_breaker_limits_restoration` | 10 degraded slots, USD for 3 → only 3 restored |
| `test_quote_first_allocation_on` | Verify B-side sizing uses buy-ready count, not total slots |
| `test_rounding_residual_active` | Verify residual accumulates and recycles into next order |
| `test_ancillary_profit_tracked` | Ranger exit updates _ancillary_profit_usd |
| `test_b_failure_counters` | Fund guard rejection increments failures_24h["fund_guard"] |

---

## 15. Observability Improvements

After this patch, the status payload should expose:

```json
{
  "b_side_health": {
    "failures_24h": {"fund_guard": 14, "entry_floor": 3, ...},
    "balance_degraded_slots": 2,
    "max_bilateral_slots": 10,
    "recoveries_24h": 7,
    "ancillary_profit_usd": 0.45
  },
  "quote_first_allocation": {
    "enabled": true,
    "buy_ready_slots": 8,
    "deployable_usd": 18.50,
    "carry_usd": 0.03,
    "allocated_usd": 18.40,
    "unallocated_spendable_usd": 0.10
  }
}
```

---

## 16. Summary of Root Cause

The USD leak is **not a single bug** but a systemic asymmetry:

1. **A-side entries always succeed** (DOGE is abundant)
2. **B-side entries frequently fail** (USD is scarce and shared)
3. **Failure causes `short_only` degradation** (positive feedback loop)
4. **A-side-only operation generates more idle USD** (amplifies the problem)
5. **No recovery mechanism exists** to restore B-side operation
6. **Ancillary profit sources** (rangers, churners) add more untracked USD

Patches A through F break this cycle at multiple points:
- **A**: Periodic recovery restores B-side when USD is available
- **B**: Better allocation math (quote-first, rounding recycling)
- **C**: Rangers/churners profit tracked in allocation budget
- **D**: Visibility into failure rates
- **E**: Entry floor doesn't inflate B-side
- **F**: Cascade breaker prevents mass degradation
