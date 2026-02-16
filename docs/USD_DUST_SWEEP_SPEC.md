# USD Dust Sweep Spec

**Version:** 0.1.0
**Status:** Draft
**Depends on:** CapitalLedger, `_slot_order_size_usd()`, Balance API

---

## 1. Problem Statement

Each A-side round trip (sell entry -> buy exit) leaves a small USD residue
in the Kraken balance. Over many cycles this "dust" accumulates and sits idle
because individual crumbs are too small to meet Kraken's minimum order cost.

**Observed:** $9.57 accumulated over 91 round trips (~$0.105/trip average)
while tracked realized PnL was only $1.21. The gap comes from three sources:

1. **Gross spread capture** -- the full USD delta (sell_price - buy_price) *
   volume stays in balance, but only the fee-adjusted net is booked as profit.
2. **Fee settlement asymmetry** -- Kraken deducts sell fees from USD received,
   buy fees from DOGE received. USD accumulates slightly more than the profit
   tracker expects.
3. **Volume rounding** -- `compute_order_volume()` truncates to
   `volume_decimals`. Each truncation leaves a sub-cent USD residue that
   cannot be spent on a whole-unit order.

The bot's order sizing (`_slot_order_size_usd`) uses `ORDER_SIZE_USD +
total_profit` for compounding, but `total_profit` only captures the
*tracked net* -- not the full settlement residue. The untracked portion
sits in the Kraken USD balance doing nothing.

---

## 2. Design Goals

| Goal | Priority |
|------|----------|
| Recapture idle USD into productive B-side orders | P0 |
| Zero additional Kraken API calls (piggyback on existing balance query) | P0 |
| No new order flow (fold dust into existing order sizing, not separate sweeps) | P0 |
| Accurate tracking that self-corrects over time | P1 |
| Dashboard visibility into dust accumulation | P2 |

### Non-Goals

- Sweeping dust into DOGE via separate market orders (taker fee, extra API).
- Tracking dust per-slot (dust is account-level, not slot-level).
- Handling DOGE-side dust (DOGE volume rounding creates the USD dust;
  the DOGE side is whole by construction).

---

## 3. Architecture: Piggyback on CapitalLedger

The existing `CapitalLedger` already queries `get_balance()` once per loop
and computes `available_usd = total_usd - committed_usd - loop_placed_usd`.
This `available_usd` already contains the dust -- it's the USD that exists
in the account but isn't committed to any open buy order.

The insight: **we don't need a separate dust tracker**. The dust is already
visible as the gap between `available_usd` and the capital the bot *thinks*
it has (based on `ORDER_SIZE_USD + total_profit` per slot).

### 3.1 Computing Dust

```
expected_deployable_usd = sum(
    _slot_order_size_usd(slot) for slot in active_slots
    if slot needs a B-side entry
)

dust_usd = ledger.available_usd - expected_deployable_usd
```

But this is fragile -- it depends on how many slots need entries at any given
moment. A simpler and more robust approach:

```
# USD the bot "knows about" through its profit tracking:
tracked_usd = ORDER_SIZE_USD * num_active_slots + sum(slot.total_profit for slot in slots)

# USD that actually exists and is undeployed:
actual_available_usd = ledger.available_usd

# Dust = the gap
dust_usd = max(0, actual_available_usd - tracked_usd)
```

This also over-counts if the user deposited extra USD. To handle that, we
anchor to a **baseline** captured at startup (same pattern as `_recon_baseline`).

### 3.2 Chosen Approach: Balance-Aware Sizing Bump

Rather than tracking dust analytically (which drifts), use the CapitalLedger's
real-time `available_usd` to compute a per-slot dust dividend each loop:

```python
@property
def dust_dividend_usd(self) -> float:
    """USD available beyond what active slots expect to use."""
    if not self.ledger._synced:
        return 0.0
    available = self.ledger.available_usd
    # What slots need for their next B-side entries:
    reserved = sum(
        self._slot_order_size_usd(slot)
        for slot in self.slots.values()
        if self._slot_wants_buy_entry(slot)
    )
    surplus = available - reserved
    if surplus < self._min_dust_threshold_usd:
        return 0.0
    # Divide evenly across slots that are about to place B-side entries.
    buy_slots = sum(1 for s in self.slots.values() if self._slot_wants_buy_entry(s))
    if buy_slots <= 0:
        return 0.0
    return surplus / buy_slots
```

This dividend is added to `_slot_order_size_usd()` for B-side entries only,
slightly increasing the buy volume to absorb the idle USD.

---

## 4. Detailed Design

### 4.1 New State (BotRuntime)

```python
# Dust sweep configuration
self._dust_sweep_enabled: bool = config.DUST_SWEEP_ENABLED      # default True
self._dust_min_threshold_usd: float = config.DUST_MIN_THRESHOLD  # default 0.50
self._dust_max_bump_pct: float = config.DUST_MAX_BUMP_PCT        # default 25.0
self._dust_last_absorbed_usd: float = 0.0   # telemetry: lifetime absorbed
self._dust_last_dividend_usd: float = 0.0   # telemetry: last computed dividend
```

### 4.2 Config Knobs (config.py)

| Variable | Default | Description |
|----------|---------|-------------|
| `DUST_SWEEP_ENABLED` | `True` | Master switch |
| `DUST_MIN_THRESHOLD` | `0.50` | Ignore dust below this (noise filter) |
| `DUST_MAX_BUMP_PCT` | `25.0` | Max % increase to a single order's USD size. Prevents a large dust pool from creating an outsized position. |

### 4.3 Integration Point: `_slot_order_size_usd()`

The dust dividend is applied as a **bounded additive bump** to B-side entries:

```python
def _slot_order_size_usd(self, slot, trade_id=None) -> float:
    base_with_layers = ...  # existing logic unchanged

    # --- Dust sweep: fold idle USD into B-side entries ---
    if (trade_id == "B"
            and self._dust_sweep_enabled
            and self.ledger._synced):
        dividend = self._compute_dust_dividend()
        if dividend > 0:
            max_bump = base_with_layers * (self._dust_max_bump_pct / 100.0)
            bump = min(dividend, max_bump)
            base_with_layers += bump

    # ... existing rebalancer skew logic continues ...
```

### 4.4 `_compute_dust_dividend()` Logic

Called once per loop (cached for the loop duration like other ledger values):

```python
def _compute_dust_dividend(self) -> float:
    """Per-slot USD dust available for B-side absorption."""
    if self._loop_dust_dividend is not None:
        return self._loop_dust_dividend

    available = self.ledger.available_usd
    if available <= 0:
        self._loop_dust_dividend = 0.0
        return 0.0

    # How much USD do active slots expect to need for B-side entries?
    reserved = 0.0
    buy_count = 0
    for slot in self.slots.values():
        base_size = self._slot_order_size_usd(slot, trade_id=None)  # without dust
        # A slot "wants" a buy entry if it has no open buy order in entry role.
        if self._slot_wants_buy_entry(slot):
            reserved += base_size
            buy_count += 1
        # Also reserve for B-side exits (buy exits for A-trades):
        for o in slot.state.orders:
            if o.side == "buy" and o.role == "exit":
                reserved += o.volume * o.price

    surplus = available - reserved
    if surplus < self._dust_min_threshold_usd or buy_count <= 0:
        self._loop_dust_dividend = 0.0
        return 0.0

    dividend = surplus / buy_count
    self._loop_dust_dividend = dividend
    self._dust_last_dividend_usd = dividend
    return dividend
```

### 4.5 `_slot_wants_buy_entry()` Helper

```python
def _slot_wants_buy_entry(self, slot: SlotRuntime) -> bool:
    """True if this slot's next action will be placing a B-side (buy) entry."""
    st = slot.state
    # Has no open buy entry order
    for o in st.orders:
        if o.side == "buy" and o.role == "entry" and o.txid:
            return False
    # Is in a phase where it needs one (S0 or S1a)
    return st.phase in ("S0", "S1a")
```

### 4.6 Loop Integration

At the start of each main loop iteration, after `ledger.sync()`:

```python
# Reset per-loop dust cache
self._loop_dust_dividend = None
```

No additional API calls needed. The dust computation piggybacks entirely
on the balance data that `CapitalLedger.sync()` already fetches.

### 4.7 Telemetry

After a B-side entry is placed with a dust bump:

```python
if dust_bump > 0:
    self._dust_last_absorbed_usd += dust_bump
    logger.info(
        "DUST ABSORBED: $%.4f into slot %d B-entry (lifetime: $%.4f)",
        dust_bump, slot_id, self._dust_last_absorbed_usd,
    )
```

---

## 5. Dashboard

### 5.1 Status Payload Addition

Add to the status JSON (in the summary section):

```python
"dust_sweep": {
    "enabled": self._dust_sweep_enabled,
    "current_dividend_usd": self._dust_last_dividend_usd,
    "lifetime_absorbed_usd": self._dust_last_absorbed_usd,
    "available_usd": self.ledger.available_usd if self.ledger._synced else None,
}
```

### 5.2 Dashboard Display

Small line in the existing summary panel, only shown when dust > threshold:

```
Dust sweep: $0.12/slot available | $3.41 lifetime absorbed
```

No separate card needed -- this is a minor telemetry line, not a primary metric.

---

## 6. Edge Cases

### 6.1 User Deposits USD

If the user deposits extra USD, `available_usd` jumps. The dust dividend would
spike, but `DUST_MAX_BUMP_PCT` (25%) caps any single order's increase. The
surplus persists and gets absorbed gradually across many B-side entries.

To prevent this from being perceived as a bug, the dashboard shows the
dividend amount so the user can see what's happening.

### 6.2 All Slots in S1b/S2 (No B-Side Entries Needed)

`buy_count = 0` -> dividend = 0. Dust sits idle until a slot transitions
back to S0/S1a. This is correct behavior -- no point absorbing dust into
non-existent orders.

### 6.3 Very Large Dust Accumulation

If the bot runs for weeks without B-side entries (extreme trending market),
dust could grow large. The `DUST_MAX_BUMP_PCT` cap prevents any single
order from becoming dangerously oversized. The dust is absorbed gradually
as normal cycling resumes.

### 6.4 Rounding Creates Sub-Minimum Volume Bump

After computing the bumped `order_size_usd`, `compute_order_volume()` rounds
to `volume_decimals`. If the bump is so small it rounds away, it has no
effect -- the dust stays in the balance and is tried again next cycle. This
is self-correcting: dust accumulates until the bump is large enough to
survive rounding.

### 6.5 Interaction with Kelly Sizing

Kelly sizing runs before the dust bump. The dust bump is additive on top
of the Kelly-adjusted size. This is correct because Kelly controls risk
exposure from the bot's perspective, while dust recovery is recapturing
capital that's already in the account and idle.

### 6.6 Interaction with Rebalancer Skew

The rebalancer's size skew also adjusts B-side entries. Order of operations:

1. Base size (ORDER_SIZE_USD + slot profit)
2. Capital layers
3. Kelly sizing
4. **Dust bump** (new -- additive, capped by DUST_MAX_BUMP_PCT)
5. Rebalancer skew (multiplicative, capped by REBALANCE_MAX_SIZE_MULT)
6. Fund guard (clamp to available balance)

The fund guard at step 6 is the final safety net -- even if steps 1-5
combine to produce a large number, the order can't exceed what's actually
in the account.

---

## 7. Rate Limit Impact

**Zero.** No new API calls. The dust computation uses `ledger.available_usd`
which is populated from the existing `get_balance()` call that already
happens once per loop.

---

## 8. Rollout

1. **Deploy with `DUST_SWEEP_ENABLED=False`** -- code ships but inactive.
   Observe `dust_sweep.current_dividend_usd` in the status payload to
   verify the computation makes sense.
2. **Enable with conservative cap** -- `DUST_SWEEP_ENABLED=True`,
   `DUST_MAX_BUMP_PCT=10`. Monitor for a few days.
3. **Raise cap** -- `DUST_MAX_BUMP_PCT=25` once comfortable.

---

## 9. Testing Plan

| Test | Description |
|------|-------------|
| `test_dust_dividend_zero_when_no_surplus` | available_usd = committed -> dividend = 0 |
| `test_dust_dividend_splits_across_slots` | $2 surplus, 4 buy slots -> $0.50 each |
| `test_dust_bump_capped` | dividend $5, base $3, cap 25% -> bump = $0.75 |
| `test_dust_disabled` | DUST_SWEEP_ENABLED=False -> no bump regardless |
| `test_dust_below_threshold` | surplus $0.30 < $0.50 threshold -> no bump |
| `test_dust_no_buy_slots` | all slots in S1b -> dividend = 0 |
| `test_dust_interacts_with_kelly` | Kelly runs first, dust bump additive on top |
| `test_dust_fund_guard_clamp` | bump would exceed available -> fund guard limits |
