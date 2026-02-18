# Churner Fix Spec: Visibility + Capital-Adaptive Entry

**Version:** 0.1
**Date:** 2026-02-17
**Status:** Draft

---

## Problem Statement

Two issues prevent the churner from being useful:

### Issue 1: Invisible After Spawning

When a churner is spawned (e.g., on slot 0), the user sees no visual change in the dashboard. The churner panel exists in the slot detail view (below the state bar and beliefs section), but:

- No indicator appears in the slot state bar (the top-level summary with phase pill, alias, price, cycles, and open order count)
- No new slot or row appears when pressing `f`/`b` to cycle slots
- The only evidence is a single line in the Self-Healing summary: "1 active | reserve $5.000"
- The churner panel (Churner Status, Parent, Gate, Reserve) is at the bottom of the slot detail view and easy to miss

**User expectation:** Something visible and obvious should change when a churner is spawned.

### Issue 2: Can't Place Orders (USD Starvation)

The churner determines entry side from the parent trade:

```python
def _churner_entry_side_for_trade(trade_id: str) -> str:
    return "buy" if str(trade_id or "").strip().upper() == "B" else "sell"
```

For B-side parents (the majority of stuck positions), entry side is always `"buy"`, which requires USD. Current capital state:

| Asset | Observed | Committed | Free |
|-------|----------|-----------|------|
| USD   | $39.57   | $39.56    | $0.002 |
| DOGE  | 833,231  | 631       | 832,600 |

The `CHURNER_RESERVE_USD` ($5.00) is an internal accounting counter, not actual Kraken balance. When the gate check "passes" via the reserve backstop, the subsequent `_place_order()` call to Kraken fails because there is no real free USD to execute a buy order.

**Result:** `churner.cycles_today: 0`, `churner.profit_today: 0.0` — the churner spawns but does nothing.

**Key insight:** In a RANGING market (the only regime where the churner operates), buy→sell and sell→buy round trips are symmetrically profitable. The entry direction doesn't matter — what matters is having capital to execute. With 832K free DOGE, sell entries always have capital.

---

## Solution

### Fix 1: CHURN Badge in Slot State Bar

Add a text badge `CHURN` (or `CHURN ENTRY OPEN` / `CHURN EXIT OPEN` when in those stages) to the slot state bar. This makes the churner immediately visible when navigating to a slot.

**Before:**
```
[S1b] bork (#0) | price $0.100238 | A.15 / B.3 | open 1
```

**After:**
```
[S1b] bork (#0) | price $0.100238 | A.15 / B.3 | open 1 | [CHURN]
```

The badge:
- Only appears on slots with an active churner
- Shows the churner stage (IDLE, ENTRY OPEN, EXIT OPEN) when not idle
- Uses existing CSS patterns (`.statepill` base class)
- Data source: `runtimeChurnerState(slot.slot_id)` — already fetched each poll cycle, no new API calls

### Fix 2: Capital-Adaptive Entry Side

Modify `_churner_gate_check()` to try both entry sides before giving up:

1. Compute preferred side from parent trade (existing behavior)
2. Check if free capital exists for preferred side
3. If not, try opposite side (flip buy↔sell)
4. If neither side has capital, fall through to existing USD reserve backstop

The function's return type gains a 6th element: the chosen entry side (`str`). The caller in the churner tick uses this instead of re-deriving it from the parent trade ID.

**Gate check flow (revised):**

```
preferred_side = "buy" if parent is B else "sell"
opposite_side = flip(preferred_side)

for try_side in [preferred_side, opposite_side]:
    compute entry_price, volume for try_side
    check free_usd / free_doge for try_side
    if has_capital → return (ok, "ok", price, vol, usd, try_side)

# Neither side → existing reserve backstop (preferred side only)
... existing reserve logic ...
return (ok, reason, price, vol, usd, preferred_side)
```

**Why this is safe:**
- The churner only operates in RANGING regime (enforced by gate check: `regime_not_ranging` rejection)
- In ranging, buy and sell entries are symmetrically profitable at tight spreads
- The exit side auto-derives from entry side: `"sell" if entry_side == "buy" else "buy"` — no downstream changes needed
- `ChurnerRuntimeState.entry_side` already exists as a field — we're just setting it correctly

### Fix 3: Reserve Label

Change the dashboard label from `"Runtime Reserve (USD)"` to `"Churner Reserve"` since the churner can now use either currency for its entries.

---

## Detailed Changes

### `bot.py`

#### `_churner_gate_check` (lines ~7396-7459)

**Return type:** `tuple[bool, str, float, float, float]` → `tuple[bool, str, float, float, float, str]`

All failure returns: append `""` as 6th element.

**Lines ~7423-7445 (capital check section):** Replace with loop over preferred + opposite sides:

```python
preferred_side = self._churner_entry_side_for_trade(
    str(parent.get("trade_id") or parent_order.trade_id)
)
opposite_side = "sell" if preferred_side == "buy" else "buy"

chosen_side = ""
entry_price = 0.0
volume_chosen = 0.0
required_usd = 0.0

for try_side in (preferred_side, opposite_side):
    try_price = self._churner_entry_target_price(side=try_side, market=market)
    if try_price <= 0:
        continue
    base_order_size_usd = max(
        0.0,
        float(getattr(config, "CHURNER_ORDER_SIZE_USD",
              getattr(config, "ORDER_SIZE_USD", 0.0))),
    )
    order_size_usd = max(base_order_size_usd,
                         base_order_size_usd + float(state.compound_usd))
    cfg_inner = self._engine_cfg(slot)
    try_vol = sm.compute_order_volume(float(try_price), cfg_inner,
                                       float(order_size_usd))
    if try_vol is None:
        continue
    try_vol = float(try_vol)
    try_req = max(0.0, try_vol * try_price)

    free_usd, free_doge = self._available_free_balances(prefer_fresh=False)
    has_capital = (
        (try_side == "buy" and free_usd >= try_req - 1e-12) or
        (try_side == "sell" and free_doge >= try_vol - 1e-12)
    )
    if has_capital:
        chosen_side = try_side
        entry_price = try_price
        volume_chosen = try_vol
        required_usd = try_req
        break

if chosen_side:
    return (True, "ok", float(entry_price), float(volume_chosen),
            float(required_usd), str(chosen_side))

# Neither side had free capital -- fall through to reserve backstop
# (uses preferred side, existing behavior)
entry_side = preferred_side
entry_price = self._churner_entry_target_price(side=entry_side, market=market)
if entry_price <= 0:
    return False, "invalid_entry_price", 0.0, 0.0, 0.0, ""

base_order_size_usd = max(
    0.0,
    float(getattr(config, "CHURNER_ORDER_SIZE_USD",
          getattr(config, "ORDER_SIZE_USD", 0.0))),
)
order_size_usd = max(base_order_size_usd,
                     base_order_size_usd + float(state.compound_usd))
cfg = self._engine_cfg(slot)
volume = sm.compute_order_volume(float(entry_price), cfg,
                                  float(order_size_usd))
if volume is None:
    return False, "below_min_size", 0.0, 0.0, 0.0, ""
volume = float(volume)
required_usd = max(0.0, float(volume) * float(entry_price))
```

Reserve backstop logic (lines ~7447-7459) stays as-is, but all returns add `str(entry_side)` as 6th element.

#### Caller in churner tick (line ~7963)

```python
# Before:
ok, gate_reason, entry_price, volume, _required_usd = self._churner_gate_check(...)

# After:
ok, gate_reason, entry_price, volume, _required_usd, chosen_side = self._churner_gate_check(...)
```

#### Entry side assignment (line ~7984)

```python
# Before:
state.entry_side = self._churner_entry_side_for_trade(state.parent_trade_id)

# After:
state.entry_side = str(chosen_side) if chosen_side else self._churner_entry_side_for_trade(state.parent_trade_id)
```

### `dashboard.py`

#### CSS (after line ~415)

```css
.churner-badge {
  color: var(--accent);
  font-size: 11px;
  background: rgba(0, 255, 200, 0.08);
}
```

#### State bar (lines ~3679-3687)

```javascript
const sb = document.getElementById('stateBar');
const alias = slot.slot_alias || slot.slot_label || `slot-${slot.slot_id}`;
const slotChurner = runtimeChurnerState(slot.slot_id);
const churnerActive = Boolean(slotChurner && slotChurner.active);
const churnerStage = churnerActive ? churnerStageLabel(slotChurner.stage) : '';
sb.innerHTML = `
  <span class="statepill ${slot.phase}">${slot.phase}</span>
  <span class="tiny">${alias} (#${slot.slot_id})</span>
  <span class="tiny">price $${fmt(slot.market_price, 6)}</span>
  <span class="tiny">A.${slot.cycle_a} / B.${slot.cycle_b}</span>
  <span class="tiny">open ${slot.open_orders.length}</span>
  ${churnerActive
    ? `<span class="statepill churner-badge">CHURN${churnerStage !== 'IDLE' ? ' ' + churnerStage : ''}</span>`
    : ''}
`;
```

#### Reserve label (line ~824)

```html
<!-- Before -->
<div class="k">Runtime Reserve (USD)</div>

<!-- After -->
<div class="k">Churner Reserve</div>
```

---

## What Does NOT Change

- `_churner_entry_side_for_trade()` static function — unchanged, still returns the "preferred" side
- `ChurnerRuntimeState` dataclass — no schema change, `entry_side` field already exists
- Exit side derivation — `"sell" if entry_side == "buy" else "buy"` continues to auto-derive
- Reserve backstop accounting — `reserve_allocated_usd`, `_churner_reserve_available_usd` unchanged
- `config.py` — `CHURNER_RESERVE_USD` stays as-is
- API endpoints — no changes to `/api/churner/spawn`, `/api/churner/kill`, `/api/churner/config`

---

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Both sides have capital | Preferred side wins (first in loop) |
| Only opposite side has capital | Opposite side used; logged as `"ok"` (not special-cased) |
| Neither side has capital | Falls to reserve backstop (existing behavior) |
| Reserve backstop succeeds but Kraken rejects | Order placement fails, churner resets to idle with `last_error: "entry_place_failed"` (existing error handling) |
| Regime shifts during entry | Existing regime-shift cancellation (line ~7933-7938) handles this |

---

## Verification

1. **Visual:** Navigate to a slot with an active churner → CHURN badge appears in state bar
2. **Functional:** With ~0 free USD and plenty of DOGE, spawned churner flips to sell entry and completes cycles
3. **Label:** Reserve input shows "Churner Reserve" header
4. **No regression:** Existing tests pass (`python -m pytest tests/test_self_healing_slots.py tests/test_hardening_regressions.py`)
