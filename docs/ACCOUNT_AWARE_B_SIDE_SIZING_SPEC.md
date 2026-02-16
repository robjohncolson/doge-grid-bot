# Account-Aware B-Side Sizing Spec

**Version:** 0.1.0
**Status:** Implemented (62a5cb2)
**Supersedes:** USD Dust Sweep Spec v0.1.0 (for B-side sizing; dust sweep
infrastructure remains for non-B use)
**Depends on:** CapitalLedger, `_slot_order_size_usd()`, Balance API

---

## 1. Problem Statement

The bot's B-side (buy entry) order sizing used per-slot profit compounding:

```
order_size = ORDER_SIZE_USD + slot.total_profit
```

`total_profit` tracks **net profit after fees** (~$0.015/trade). But each
A-side round trip (sell entry -> buy exit) leaves a larger USD residue in
the Kraken balance (~$0.105/trade) due to three sources:

1. **Gross spread capture** -- the full USD delta `(sell_price - buy_price)
   * volume` stays in balance, but only the fee-adjusted net is booked as
   profit.
2. **Fee settlement asymmetry** -- Kraken deducts sell fees from USD
   received, buy fees from DOGE received. USD accumulates faster than
   `total_profit` tracks.
3. **Volume rounding** -- `compute_order_volume()` truncates to
   `volume_decimals`. Each truncation leaves a sub-cent USD residue.

**Observed:** $118 accumulated idle across 27 slots after sustained
operation. The per-slot profit compounding formula systematically undersized
B-side orders by ~7x the actual USD available per trade, leaving capital
permanently idle.

The original dust sweep mechanism (additive bump on top of profit
compounding) could not fix this because:

- It only fired when slots were in S0/S1a (needing a buy entry).
- When all slots were in S1b/S2 (both sides placed), the dividend was $0.
- Even when active, the bump was capped at 25% of the undersized base --
  too small to close the gap.

---

## 2. Design Goals

| Goal | Priority |
|------|----------|
| Deploy all available USD into productive B-side orders | P0 |
| Zero additional Kraken API calls | P0 |
| No new order flow (fold into existing order sizing) | P0 |
| USD-only -- never include DOGE balance in sizing | P0 |
| Self-correcting: adapts as balance changes | P1 |
| Dashboard visibility | P2 |

### Non-Goals

- Including DOGE balance in sizing (DOGE is held externally; only trade
  with USD the bot manages).
- Per-slot tracking of dust origin.
- Separate sweep orders or market orders.

---

## 3. Architecture: Account-Aware Base

Instead of computing B-side order size from per-slot profit history, derive
it directly from the real-time USD balance:

```
B-side base = max(ORDER_SIZE_USD, available_usd / slot_count)
```

This is the correct formulation because:

- `available_usd` (from CapitalLedger) already accounts for all committed
  buy orders. It represents USD that exists and is not deployed.
- Dividing by `slot_count` distributes capital evenly.
- The `max()` floor prevents sizing below the configured minimum.
- The fund guard (`_try_reserve_loop_funds()`) remains the hard safety net.

### 3.1 Why Not Dust Sweep?

The dust sweep was an additive patch on a broken base. Account-aware sizing
fixes the base itself, making the dust sweep redundant for B-side. The dust
sweep infrastructure is retained for any future non-B use but is skipped
when `trade_id == "B"`.

### 3.2 A-Side Unchanged

A-side (sell entry) sizing continues to use `ORDER_SIZE_USD +
slot.total_profit`. A-side entries are DOGE-funded, not USD-funded, so the
USD balance gap does not apply.

---

## 4. Detailed Design

### 4.1 New Method: `_b_side_base_usd()`

```python
def _b_side_base_usd(self) -> float:
    """Per-slot B-side base: available USD divided evenly across all slots.

    Cached per loop.  Falls back to ORDER_SIZE_USD when balance is
    unavailable.  The fund guard remains the hard safety-net.
    """
    if self._loop_b_side_base is not None:
        return self._loop_b_side_base

    available = 0.0
    if self._loop_available_usd is not None:
        available = max(0.0, float(self._loop_available_usd))
    elif self.ledger._synced:
        available = max(0.0, float(self.ledger.available_usd))
    else:
        self._loop_b_side_base = float(config.ORDER_SIZE_USD)
        return self._loop_b_side_base

    n_slots = max(1, len(self.slots))
    base = available / n_slots
    self._loop_b_side_base = max(float(config.ORDER_SIZE_USD), base)
    return self._loop_b_side_base
```

### 4.2 Modified: `_slot_order_size_usd()`

The base computation now branches on `trade_id`:

```python
base_order = float(config.ORDER_SIZE_USD)
if trade_id == "B":
    # Account-aware: divide available USD evenly across all slots.
    base = self._b_side_base_usd()
elif STICKY_MODE and compound_mode == "fixed":
    base = max(base_order, base_order)
else:
    # Independent compounding per slot (A-side and baseline queries).
    base = max(base_order, base_order + slot.state.total_profit)
```

Dust sweep is skipped for B-side:

```python
if trade_id != "B":
    dust_bump = self._dust_bump_usd(slot, trade_id=trade_id)
    base_with_layers = max(0.0, base_with_layers + dust_bump)
```

### 4.3 New State

```python
self._loop_b_side_base: float | None = None  # per-loop cache
```

Cleared in both `begin_loop()` and `end_loop()`.

### 4.4 Order of Operations (B-side)

1. **Account-aware base** (`available_usd / slot_count`, floored at
   `ORDER_SIZE_USD`)
2. Capital layers (additive)
3. Throughput sizer (multiplicative)
4. ~~Dust bump~~ (skipped for B-side)
5. Rebalancer skew (multiplicative, capped)
6. Fund guard (clamp to available balance)

### 4.5 Order of Operations (A-side, unchanged)

1. Base size (`ORDER_SIZE_USD + slot.total_profit`)
2. Capital layers (additive)
3. Throughput sizer (multiplicative)
4. Dust bump (additive, only if `trade_id == "B"` -- effectively never
   fires for A-side either, kept for symmetry)
5. Rebalancer skew (multiplicative, capped)
6. Fund guard (clamp to available balance)

---

## 5. Dashboard

### 5.1 Status Payload Addition

```python
"b_side_sizing": {
    "base_usd": self._loop_b_side_base,  # None when not yet computed
    "slot_count": len(self.slots),
},
```

The existing `dust_sweep` block is retained for diagnostics but its
`current_dividend_usd` will typically be 0.0 since B-side dust bump is
skipped.

---

## 6. Edge Cases

### 6.1 User Deposits USD

`available_usd` increases, so `_b_side_base_usd()` increases. Each slot's
next B-side entry will be proportionally larger. The fund guard prevents any
single order from exceeding the actual balance. The surplus is absorbed
naturally over the next few B-side entries.

### 6.2 All Slots in S1b/S2

Unlike the dust sweep (which required S0/S1a slots to compute a dividend),
account-aware sizing always computes correctly. When a slot eventually
needs a B-side entry, it gets the right size based on current available
balance.

### 6.3 Balance Below ORDER_SIZE_USD * Slot Count

The `max()` floor means each slot's base never drops below
`ORDER_SIZE_USD`. If there isn't enough USD for all slots, the fund guard
will prevent orders that can't be funded. This is self-correcting: as
trades complete and USD returns, slots resume.

### 6.4 No Balance Data (Ledger Not Synced)

Falls back to `ORDER_SIZE_USD` -- the same behavior as before this change.

### 6.5 Interaction with Throughput Sizer

Throughput sizer receives the account-aware base (potentially larger than
`ORDER_SIZE_USD`). Its multiplier applies on top. This is correct: the
throughput sizer adjusts for fill-rate efficiency, while account-aware
sizing adjusts for capital deployment.

### 6.6 Interaction with Rebalancer Skew

Rebalancer skew is multiplicative on the post-throughput base. With a
larger account-aware base, the rebalancer's absolute effect is
proportionally larger. The fund guard caps the result.

### 6.7 DOGE Balance Excluded

The method only reads `available_usd` (via `_loop_available_usd` or
`ledger.available_usd`). DOGE balance is never referenced. This is by
design: the user holds DOGE externally and only wants to trade with their
USD allocation.

---

## 7. Rate Limit Impact

**Zero.** No new API calls. Uses `_loop_available_usd` which is populated
from the existing `get_balance()` call in `begin_loop()`.

---

## 8. Testing

| Test | Description |
|------|-------------|
| `test_b_side_account_aware_sizing` | available=7.0, 1 slot -> base=7.0 |
| `test_dust_disabled` | B-side still uses account-aware base regardless of dust setting |
| `test_dust_below_threshold` | available=2.3, 1 slot -> base=2.3 (above ORDER_SIZE_USD floor) |
| `test_dust_interacts_with_throughput` | base=10.0 passed to throughput sizer, returns 4.0 |
| `test_dust_fund_guard_clamp` | base=3.0, rebalancer scales to 4.5, fund guard caps at 3.0 |
| `test_dust_dividend_zero_when_no_surplus` | Dust dividend computation still works (0 surplus) |
| `test_dust_dividend_splits_across_slots` | Dust dividend computation still works (split surplus) |
| `test_dust_no_buy_slots` | Dust dividend = 0 when no slots need buy entry |

---

## 9. Migration from Dust Sweep

The dust sweep mechanism (`_compute_dust_dividend`, `_dust_bump_usd`,
config knobs `DUST_SWEEP_ENABLED`, `DUST_MIN_THRESHOLD`,
`DUST_MAX_BUMP_PCT`) remains in the codebase but is effectively dormant
for B-side orders. It can be fully removed in a future cleanup pass once
account-aware sizing is confirmed stable in production.

The `DUST_MAX_BUMP_PCT` default was changed to `0.0` (uncapped) in f134a7e
as an intermediate fix before account-aware sizing was implemented.
