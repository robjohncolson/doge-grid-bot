# Dashboard Fixes Spec — 3 Issues

## Context

- **Project**: doge-grid-bot (Python, zero external deps)
- **Dashboard**: Single-page HTML embedded as a string constant in `dashboard.py`
- **API server**: HTTP handler in `bot.py` (stdlib `http.server`)
- **State**: `grid_strategy.py` contains `GridState` and all trading logic

The bot runs a "swarm" of 12+ crypto pairs. Each pair can have multiple
"slots" (independent A/B trade state machines on the same pair). The
dashboard has two views: **swarm view** (table of all pairs) and
**detail view** (single pair deep-dive, reached by clicking a pair row).

---

## Fix 1: AI Analyze Button Does Nothing

### Problem

The "?" button on losing completed cycles has broken HTML escaping.
Clicking it does nothing — no network request, no error in console.

**File**: `dashboard.py` line 1837

```javascript
// Current (broken):
+ (c.net_profit < 0
  ? ' <button class="btn-analyze" onclick="analyzeTrade(this,'
    + JSON.stringify(JSON.stringify(c))
    + ')" title="AI analysis">?</button>'
  : '')
```

`JSON.stringify(JSON.stringify(c))` produces a string wrapped in literal
`"` characters. When inserted into the `onclick="..."` HTML attribute,
the inner `"` terminates the attribute prematurely. The browser sees:

```html
onclick="analyzeTrade(this,"     ← attribute ends here
{"trade_id":"b",...}")"          ← parsed as garbage HTML attributes
```

### Fix

Replace the inline JSON approach with a `data-` attribute + event
listener pattern, or HTML-encode the quotes.

**Simplest approach** — HTML-encode the double-stringified JSON:

```javascript
// Line 1837 — replace the button generation:
+ (c.net_profit < 0
  ? ' <button class="btn-analyze" onclick="analyzeTrade(this,'
    + JSON.stringify(JSON.stringify(c)).replace(/"/g, '&quot;')
    + ')" title="AI analysis">?</button>'
  : '')
```

This produces:
```html
onclick="analyzeTrade(this,&quot;{...}&quot;)"
```
The browser decodes `&quot;` back to `"` before executing the JS,
so `analyzeTrade(this, "{...}")` works correctly. The `analyzeTrade`
function already calls `JSON.parse(cycleJson)` on line 2188 to
deserialize the string.

**Alternative (cleaner)**: Use `data-cycle` attribute + delegated listener:
```javascript
// Button:  <button class="btn-analyze" data-cycle='${singleStringifiedJSON}'>?</button>
// Listener: document.addEventListener('click', e => { if (e.target.matches('.btn-analyze')) analyzeTrade(e.target, e.target.dataset.cycle); })
```

### Files to Change

| File | Lines | Change |
|------|-------|--------|
| `dashboard.py` | 1837 | Fix quote escaping in button onclick |

### Verification

1. Open dashboard, navigate to a pair with losing cycles
2. Click the "?" button on a red (negative P&L) completed cycle row
3. Button should show "..." while loading, then a new row with AI analysis
   should appear below the cycle row
4. Check browser console — no errors
5. Check network tab — POST to `/api/ai/analyze-trade` should fire with
   correct JSON body

---

## Fix 2: No Way to View Individual Slots

### Problem

When a pair has multiple slots (e.g., DOGE/USD with 2 slots), the detail
view shows "Slots: 2" with +/− buttons, but there is no way to see or
switch between individual slot states. Slot #1's orders, cycles, P&L,
and state machine are invisible.

### Current Architecture

- Each slot is a separate `GridState` in `_bot_states` (bot.py), keyed
  as `XDGUSD` (slot 0) and `XDGUSD#1` (slot 1)
- `/api/status?pair=XDGUSD` returns slot 0's full state
- `/api/swarm/status` groups slots by base pair and includes a `slots`
  array with per-slot summaries (lines 509-530 in bot.py)
- The detail view always requests `?pair={basePair}` — it never
  requests slot #1

### Swarm Status Slots Array

Each entry in the `slots` array (from `/api/swarm/status`) contains:

```python
# bot.py lines 480-507
slots_detail.append({
    "slot_id": st.slot_id,
    "pair_state": getattr(st, '_cached_pair_state', 'S0'),
    "today_pnl": round(st.today_profit_usd, 4),
    "total_pnl": round(st.total_profit_usd, 4),
    "trips_today": st.round_trips_today,
    "total_trips": st.total_round_trips,
    "open_orders": len([o for o in st.grid_orders if o.status == "open"]),
    "winding_down": st.winding_down,
    "seed_cost": round(st.seed_cost_usd, 4),
})
```

### Fix — Add Slot Tabs to Detail View

Add a tab bar / pill selector in the detail view (inside or next to the
`ps-slots` div) that lets the user switch between slots. Each tab loads
that slot's full state via `/api/status?pair=XDGUSD` (slot 0) or
`/api/status?pair=XDGUSD%231` (slot 1).

**Approach**:

1. **Slot tab bar** (dashboard.py HTML): Add a row of clickable tabs/pills
   below the slot count. Only shown when `slot_count > 1`. Each tab shows
   the slot ID and its pair state (e.g., "Slot 0 (S2)" / "Slot 1 (S0)").

2. **State variable** (dashboard.py JS): Add `detailSlot = 0` alongside
   existing `detailPair`. When a slot tab is clicked, set `detailSlot`
   and re-fetch state.

3. **API query** (dashboard.py JS): Change the status fetch from:
   ```javascript
   fetch('/api/status?pair=' + detailPair)
   ```
   to:
   ```javascript
   fetch('/api/status?pair=' + detailPair + (detailSlot > 0 ? '%23' + detailSlot : ''))
   ```
   (`%23` = URL-encoded `#`, so `XDGUSD#1`)

4. **Tab rendering** (dashboard.py JS): In the slot info population
   block (lines 1598-1614), when `slot_count > 1`, render tabs from
   the swarm data `pInfo.slots` array:
   ```javascript
   let tabsHtml = '';
   for (const s of pInfo.slots) {
     const active = s.slot_id === detailSlot ? ' slot-tab-active' : '';
     tabsHtml += '<button class="slot-tab' + active + '" onclick="switchSlot('
       + s.slot_id + ')">#' + s.slot_id + ' (' + s.pair_state + ')</button>';
   }
   ```

5. **`switchSlot(id)` function** (dashboard.py JS): Sets `detailSlot = id`
   and calls the existing `pollStatus()` to re-render.

6. **Styling**: Small pill buttons, similar to `.slot-btn` style.
   Active tab gets a highlight border/background.

### Files to Change

| File | Lines | Change |
|------|-------|--------|
| `dashboard.py` | 610-616 | Add slot tab container HTML element |
| `dashboard.py` | 1598-1614 | Render slot tabs from `pInfo.slots` when count > 1 |
| `dashboard.py` | ~1700 (status fetch) | Append slot suffix to `?pair=` query |
| `dashboard.py` | 2213+ | Add `switchSlot()` function |
| `dashboard.py` | CSS section | Add `.slot-tab` / `.slot-tab-active` styles |

### Verification

1. Have a pair with 2+ slots (use +/− buttons or wait for auto-slot)
2. Slot tabs should appear below the slot count
3. Clicking a tab should reload the detail view with that slot's state,
   orders, cycles, and pair state
4. The active tab should be visually highlighted
5. Switching back to slot 0 should show the original data

---

## Fix 3: Order Size Display Shows Static/Stale Value

### Problem

The params panel shows `"$5.00 base × 2 slots"` — the `$5.00` comes from
`state.order_size_usd` which was an inflated value from old configs. After
the recent fix (commit `0361e9b`), `build_pair()` resets `order_size_usd`
to the global base `$0.50`, but this is also misleading because the actual
order cost varies per pair based on Kraken's minimum volume:

| Pair | Min Volume | Price | Actual Order Cost |
|------|-----------|-------|-------------------|
| DOGE | 13 | $0.093 | $1.21 |
| ETH | ~0.001 | $2013 | ~$2.01 |
| BCH | 0.01 | $523 | $5.23 |
| LTC | ~0.05 | $53 | ~$2.66 |
| ADA | ~10 | $0.26 | ~$2.61 |

The dashboard should show the **actual per-order cost** for the current
pair, not the static `order_size_usd` base value.

### Current Code

**Serialization** (`dashboard.py` line 148):
```python
"order_size": state.order_size_usd,
```

**Display** (`dashboard.py` lines 1900-1907):
```javascript
document.getElementById('p-size').textContent =
  '$' + fmt(cfg.order_size, 2) + ' base × ' + sc + ' slot' + (sc > 1 ? 's' : '') + ' | Profit: ' + fmtUSD(np);
```

### Fix — Compute and Display Actual Order Cost

**Option A: Compute server-side** (recommended — keeps logic in Python)

In `serialize_state()` (dashboard.py line 148), add the actual order cost:

```python
from grid_strategy import calculate_volume_for_price

actual_volume = calculate_volume_for_price(current_price, state)
actual_cost = round(actual_volume * current_price, 4)

# In the config dict:
"order_size": state.order_size_usd,       # keep for backwards compat
"actual_order_cost": actual_cost,          # new field
"actual_order_volume": actual_volume,      # new field
```

Then update the JS display (line ~1906):

```javascript
// Show actual cost per order, not the base
const actualCost = cfg.actual_order_cost || cfg.order_size;
document.getElementById('p-size').textContent =
  '$' + fmt(actualCost, 2) + '/order × ' + sc + ' slot' + (sc > 1 ? 's' : '') + ' | Profit: ' + fmtUSD(np);
```

**Option B: Compute client-side** (less ideal — duplicates min_volume logic)

Pass `min_volume` and `current_price` to the dashboard and let JS compute
`Math.max(order_size / price, min_volume) * price`.

### Files to Change

| File | Lines | Change |
|------|-------|--------|
| `dashboard.py` | 148 | Add `actual_order_cost` and `actual_order_volume` to serialized config |
| `dashboard.py` | 1900-1907 | Display `actual_order_cost` instead of `order_size` |

### Verification

1. Open dashboard, navigate to any pair's detail view
2. Params panel should show actual cost (e.g., "$1.21/order × 1 slot")
   not "$0.50 base × 1 slot"
3. Different pairs should show different costs matching their Kraken
   minimum volumes
4. Value should update when price changes

---

## File Reference

| File | Role | Key Lines |
|------|------|-----------|
| `dashboard.py` | HTML + JS + CSS (embedded string) + `serialize_state()` | All UI |
| `bot.py` | HTTP API handlers | `/api/status`, `/api/swarm/status`, `/api/ai/analyze-trade`, `/api/swarm/multiplier` |
| `grid_strategy.py` | `GridState`, `calculate_volume_for_price()`, `build_pair()` | State + logic |
| `config.py` | `PairConfig`, `ORDER_SIZE_USD` | Configuration |
| `ai_advisor.py` | `analyze_trade()` | AI analysis endpoint logic |

## Testing Notes

- The bot runs in a Docker container on Railway; the dashboard is served
  on port 8080
- No build step — dashboard is a Python string constant, changes require
  container redeploy
- Browser dev tools (Console + Network tabs) are essential for debugging
  the JS fixes
- The `/api/swarm/status` endpoint can be hit directly to inspect slot
  data structure
