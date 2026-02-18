# Herd Mode Spec: Decoupled Intelligence + Dual-Mode Slots

**Version:** 0.1.0
**Status:** Draft

---

## 1. Problem Statement

The bot accumulated ~30 commits of intelligence systems (HMM, AI
advisor, throughput sizer, rebalancer, BOCPD, belief engine, knob
mode, MTS, churner, ranger, signal digest) that all directly modify
the core entry placement pipeline. **19 distinct gates** can
block/modify/cancel B-side entries. The result: sell entries succeed
(DOGE is abundant), buy entries are systematically blocked (USD is
scarce and gatekept), and USD accumulates with no mechanism to
redeploy it.

The root mistake was not sticky mode itself — sticky slots are
valuable for patient exit-waiting in volatile conditions. The mistake
was **replacing all cycling behavior with sticky wholesale**, and
then layering intelligence that further blocks the entries needed
to keep USD flowing.

---

## 2. Core Insight: Stuck Orders Are Signal

When sticky exits accumulate across slots, they define a **price
range**. If 8 slots have exits spread between $0.170 and $0.195,
the market is ranging within that band. This range is a clue:

- **Inside the range**: Safe to run cycling (orphan-generating) slots.
  The price oscillates, entries fill, exits fill, USD flows.
- **At the edges**: Sticky exits are patient waiters. They'll fill
  when the price revisits.
- **Breakout above/below**: The intelligence detects the breakout
  (HMM/BOCPD) and can reprice or release sticky exits.

The architecture should let both modes coexist, with the intelligence
framework deciding the mix.

---

## 3. Architecture: Three Layers

### Layer 1 — Core Engine (always active, mode-aware)

The mechanical trading loop. Operates each slot in one of two modes:

**Cycling mode** (the pre-sticky behavior):
- Always places bilateral A+B entries
- Stale exits orphaned → recovery order (lottery on Kraken) + fresh entry
- USD flows naturally through both sides
- Creates orphans as a natural byproduct
- `sticky_mode_enabled=False` in engine config

**Sticky mode** (patient waiting):
- Places bilateral A+B entries (same as cycling)
- Exits are **never orphaned by time** — they wait indefinitely
- If an exit fills, the cycle completes normally
- If exit goes stale, the slot stays in S1 state
- `sticky_mode_enabled=True` in engine config

**Key change from current system**: Intelligence systems **never** modify
entries in either mode. No size suppression, no side cancellation, no
spacing asymmetry, no MTS throttle. Both modes always place bilateral
entries with simple symmetric sizing.

### Layer 2 — Advisory Intelligence (always running, never actuates on entries)

All intelligence systems keep computing. Their outputs go to the
status payload and dashboard. They **never**:
- Cancel entry orders
- Set `long_only` / `short_only` / `mode_source`
- Modify entry order sizes
- Set `entry_adds_per_loop_cap` to 0
- Adjust entry spacing asymmetrically

They **do** produce:
- Regime label, confidence, bias (HMM 1m/15m/1h)
- AI opinion, conviction, accumulation signal
- Recommended order sizes (throughput sizer)
- Inventory skew direction and magnitude (rebalancer)
- Change-point probability (BOCPD)
- Direction score, entropy, knob values (belief engine)
- Manifold trading score and band (MTS)
- Diagnostic traffic light (signal digest)

These are displayed prominently on the dashboard.

### Layer 3 — Herd Mode (dashboard toggle)

When `HERD_MODE_ENABLED` is ON, the intelligence layer actively
manages the herd of orders. It has three jobs:

#### 3a. Mode Selection: Which Slots Are Sticky vs Cycling

The intelligence framework decides the per-slot mode based on:

- **Stuck exit range detection**: Examine all sticky exits across
  slots. Their prices define a ranging band. Slots whose exits are
  deep inside this band can be switched to cycling — the price is
  likely to oscillate through them.
- **Volatility regime**: High volatility → more sticky slots (exits
  have a better chance of filling at extreme prices). Low volatility
  / ranging → more cycling slots (keep USD flowing in the tight
  range).
- **HMM/BOCPD signals**: Confirmed trend → don't cycle the
  against-trend side's exits (they'll just orphan immediately). Keep
  those sticky. With-trend side can cycle freely.
- **Capital pressure**: When too much USD is idle (from the ledger),
  bias toward cycling more slots to deploy it.

The per-slot `sticky` flag (already implemented in `bf1971f`) is
the mechanism. Herd mode writes to it; the core engine reads it.

#### 3b. Orphan Herding: Managing Recovery Orders

When cycling slots create orphans, herd mode decides what to do
with them:
- **Reprice exits** closer to market when regime says the exit
  direction is unlikely
- **Soft-close** recovery orders that are far from market and old
- **Let lottery tickets ride** when regime suggests reversal
- **Priority ordering**: Close furthest/oldest first, weighted by
  regime unfavorability

#### 3c. Supplemental Strategies

These existing systems get re-gated behind herd mode:
- **Churner**: Micro-cycles stale exits (already targets orphan positions)
- **Ranger**: Sell-side micro-cycler
- **DCA accumulation**: Market buys during favorable regime transitions

When `HERD_MODE_ENABLED` is OFF:
- All slots run in their current per-slot mode (no automatic switching)
- Orphans are pure lottery tickets
- No exit repricing, no soft-closes
- No churner/ranger/DCA

---

## 4. The 9 Intelligence Gates Being Disconnected from Entries

These are the systems that currently block/modify/cancel entries.
All are being moved to advisory-only (or, for exit-management
systems, to herd mode).

| # | Gate | File:Line | Current effect on entries | New behavior |
|---|------|-----------|--------------------------|--------------|
| 1 | Tier 2 regime suppression | `bot.py:~11953` | Cancels live B-entries, sets `short_only` | Advisory: shows "regime suggests suppress B" on dashboard |
| 2 | Regime bootstrap suppression | `bot.py:~13263` | Bootstraps one-sided | Always bootstraps bilateral |
| 3 | Knob mode suppression | `bot.py:~2451` | Multiplies against-trend entries toward 0 | Advisory: shows recommended suppression value |
| 4 | MTS entry throttle | `bot.py:~2653` | Sets entry cap to 0 | Advisory: shows MTS score and "caution" indicator |
| 5 | Throughput sizer | `bot.py:~2460` | Modifies entry sizes | Advisory: shows recommended size alongside actual |
| 6 | Rebalancer skew | `bot.py:~2491` | Inflates/deflates entry sizes | Advisory: shows skew and recommended multiplier |
| 7 | Sticky mode (wholesale) | `state_machine.py:~1026` | All slots non-orphaning | Per-slot: herd mode decides mix |
| 8 | Deferred entry purging | `bot.py:~2697` | Purges suppressed-side from queue | Never purges entries |
| 9 | Regime entry spacing | `bot.py:~1950` | Asymmetric A/B spacing | Symmetric: both sides use `entry_pct` |

---

## 5. Sizing Simplification

### Current (19-gate pipeline):

```
ORDER_SIZE_USD
  + slot profit compounding
  + capital layers
  + dust sweep
  × knob aggression
  → throughput_sizer.size_for_slot()    ← intelligence
  × knob suppression                    ← intelligence
  → entry floor guard clamp
  × rebalancer skew                     ← intelligence
  = final size (may be below minimum → entry blocked)
```

### New (core engine only):

```
A-side: max(ORDER_SIZE_USD, ORDER_SIZE_USD + slot.total_profit)
B-side: available_usd / buy_ready_slots  (quote-first allocation)
  + capital layers         (manual/config-driven, not intelligence)
  + dust sweep / carry     (recaptures idle USD)
  → entry floor guard      (basic minimum-volume safety only)
  = final size
```

The throughput sizer, rebalancer, and knob values are still computed
and displayed as **"Recommended size: $X.XX"** alongside the actual
size. The operator sees what the intelligence thinks. If they want
to act on it, they adjust `ORDER_SIZE_USD` manually. Later, herd mode
can optionally apply these to exit management.

---

## 6. Stuck Exit Range Detection (New Intelligence)

This is the key new capability that makes the dual-mode system
intelligent. It runs as part of herd mode.

### 6.1 Computing the Range

```python
def _compute_stuck_exit_range(self) -> tuple[float, float] | None:
    """Derive the implied ranging band from sticky exit prices."""
    sticky_exits = []
    for slot in self.slots.values():
        if not getattr(slot, "sticky", False):
            continue
        for o in slot.state.orders:
            if o.role == "exit" and o.txid:
                sticky_exits.append(o.price)
    if len(sticky_exits) < 3:
        return None  # Not enough data for a range
    return (min(sticky_exits), max(sticky_exits))
```

### 6.2 Mode Selection Logic

```python
def _herd_update_slot_modes(self, now: float) -> None:
    range_band = self._compute_stuck_exit_range()
    if range_band is None:
        return  # Not enough sticky exits to establish range

    low, high = range_band
    market = self.last_price

    for sid, slot in sorted(self.slots.items()):
        currently_sticky = getattr(slot, "sticky", True)
        # Slots with exits deep inside the range can cycle
        inside_range = low * 1.02 < market < high * 0.98
        # Capital pressure: too much idle USD → bias toward cycling
        usd_pressure = (self.ledger.available_usd if self.ledger._synced else 0) > self._minimum_b_entry_usd() * 3

        if currently_sticky and inside_range and usd_pressure:
            slot.sticky = False  # Switch to cycling
            logger.info("HERD: slot %d → cycling (market %.6f inside range [%.6f, %.6f])", sid, market, low, high)
        elif not currently_sticky and not inside_range:
            slot.sticky = True  # Switch to sticky (market broke out of range)
            logger.info("HERD: slot %d → sticky (market %.6f outside range [%.6f, %.6f])", sid, market, low, high)
```

### 6.3 Future Enhancement: Intelligence-Driven Mode Selection

The stuck exit range is the simplest signal. Future iterations can
incorporate:
- HMM regime: trending → more sticky, ranging → more cycling
- BOCPD: change-point detected → temporarily all sticky
- MTS band: hostile → all sticky, optimal → mostly cycling
- Throughput data: which slots have best profit/time → keep cycling

This is where the intelligence framework becomes genuinely useful —
not by blocking entries, but by choosing the right mode per slot.

---

## 7. Phased Implementation

### Phase 0 — Toggle Infrastructure (no behavior change)

**Files**: `config.py`, `bot.py`

- Add `HERD_MODE_ENABLED = _env("HERD_MODE_ENABLED", False, bool)`
- Add to `_build_toggle_registry`
- Add `herd_mode` block to status payload
- Add dashboard toggle button

### Phase 1 — Disconnect 9 Gates from Entry Path

**Files**: `bot.py` (primary), `config.py`

**1a. Simplify `_engine_cfg`** (~line 1998):
- Remove `spacing_mult_a/b` asymmetry — set both to `entry_pct`
- Remove knob_cadence multiplication — use config orphan timers directly
- Per-slot sticky from `slot.sticky` attribute (already exists)
- Keep `RECOVERY_ORDERS_ENABLED` semantics for orphan timer gating

**1b. Simplify `_slot_order_size_usd`** (~line 2417):
- Keep: B-side quote-first base, A-side profit compounding, capital layers, dust sweep, entry floor guard
- Remove: throughput sizer call, knob aggression/suppression, rebalancer skew
- Throughput sizer and rebalancer still compute — results go to `advisory` status payload

**1c. Remove `_apply_tier2_suppression` call** from `run_loop_once`

**1d. Remove regime suppression from bootstrap** — always bilateral

**1e. Remove regime suppression from auto_repair** — no `mode_source="regime"` early-return

**1f. Remove entry purging from deferred queue** — never purge entries

**1g. Remove MTS entry throttle** — keep headroom-based throttling only

**1h. Activate dormant fixes**:
- `QUOTE_FIRST_ALLOCATION` default → `True`
- `ROUNDING_RESIDUAL_ENABLED` default → `True`
- `ALLOCATION_SAFETY_BUFFER_USD` default → `0.10`

### Phase 2 — Transition Existing Sticky State

**Files**: `bot.py`

One-time `_transition_to_dual_mode()` in `initialize()`:
- Inventory all slots with old sticky exits
- Instead of orphaning them all, **keep them sticky**
- Set their `slot.sticky = True` explicitly
- New slots added after this point default to `sticky = False` (cycling)
- The herd mode, once enabled, will manage the mix

This is non-destructive. Existing exits stay on Kraken. No orphan
burst. The difference is that NEW slots cycle bilaterally from the
start, and the existing sticky slots continue waiting.

### Phase 3 — Implement Herd Mode Logic

**Files**: `bot.py`, `grid_strategy.py`

New method `_run_herd_mode(self, now)` in `run_loop_once`:

```python
def _run_herd_mode(self, now):
    if not self._flag_value("HERD_MODE_ENABLED"):
        return
    self._herd_update_slot_modes(now)    # stuck exit range → mode selection
    self._herd_reprice_exits(now)        # existing exit repricing, re-gated
    self._herd_manage_recoveries(now)    # soft-close decisions
    self._herd_accumulation(now)         # existing DCA, re-gated
```

Re-gate `_run_churner_engine` and `_run_ranger_engine` behind
`HERD_MODE_ENABLED` (in addition to their own toggles).

Re-gate `check_stale_exits` (exit repricing) and `check_s2_break_glass`
behind herd mode for sticky slots (cycling slots handle their own
orphaning via the core engine).

### Phase 4 — Dashboard Advisory Panel

**Files**: `dashboard.py`

- "Advisory Intelligence" panel: all signals with color-coded indicators
- "Recommended vs Actual" size display per slot
- "Herd Mode" toggle (prominent)
- Mode indicator per slot: STICKY / CYCLING
- Stuck exit range visualization (price band with exit markers)
- When herd OFF: preview "Would switch 3 slots to cycling, reprice 2 exits"

### Phase 5 — Dead Code Cleanup

**Files**: `bot.py`, `config.py`

- Remove `STICKY_MODE_ENABLED` global flag (per-slot `sticky` remains)
- Remove `_auto_release_sticky_slots` and vintage release machinery
- Remove Tier 2 suppression actuation paths (keep computation for advisory)
- Remove `MTS_ENTRY_THROTTLE_ENABLED` gate
- Remove knob-mode entry-sizing branches
- Clean up toggle registry

---

## 8. What Changes Per System

| System | Before | After |
|--------|--------|-------|
| **HMM regime** | Cancels entries, sets mode | Advisory only. Herd mode reads for mode selection. |
| **AI regime advisor** | Overrides tier → triggers cancels | Advisory only. Herd mode reads for exit decisions. |
| **Throughput sizer** | Modifies entry sizes directly | Advisory only. Shows "recommended" alongside actual. |
| **Rebalancer** | Skews entry sizes | Advisory only. Shows skew value. |
| **BOCPD / belief engine** | Modifies cadence, suppresses entries | Advisory only. Herd mode reads for mode selection. |
| **MTS** | Blocks ALL entries when < 0.3 | Advisory only. Shows score/band. |
| **Knob mode** | Suppresses against-trend entries | Advisory only. Shows knob values. |
| **Entry floor guard** | Inflates entries above budget | Simplified: basic min-volume floor only |
| **Sticky mode** | All-or-nothing global flag | Per-slot, managed by herd mode |
| **Exit repricing** | Always active | Herd mode gated (for sticky slots) |
| **S2 break-glass** | Always active | Herd mode gated (for sticky slots) |
| **Recovery soft-close** | Always active | Herd mode gated |
| **Churner** | Own toggle | Herd mode gated + own toggle |
| **Ranger** | Own toggle | Herd mode gated + own toggle |
| **DCA accumulation** | Own toggle | Herd mode gated + own toggle |
| **Signal digest** | Advisory only | No change (already the pattern) |
| **Capital layers** | Modifies sizes (config-driven) | No change (manual, not intelligence) |

---

## 9. Config Changes

| Variable | Old Default | New Default | Notes |
|----------|-------------|-------------|-------|
| `HERD_MODE_ENABLED` | (new) | `False` | Master toggle for Layer 3 |
| `QUOTE_FIRST_ALLOCATION` | `False` | `True` | Better B-side allocation |
| `ROUNDING_RESIDUAL_ENABLED` | `False` | `True` | Recycle sub-cent remainders |
| `ALLOCATION_SAFETY_BUFFER_USD` | `0.50` | `0.10` | Less withheld from allocation |
| `STICKY_MODE_ENABLED` | `False` | (removed) | Replaced by per-slot `sticky` |

---

## 10. Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Existing sticky exits orphaned en masse | Phase 2: keep existing exits sticky, only new slots cycle |
| Order capacity | New cycling slots create ~2 orders each; well within 225 limit |
| USD shock from bilateral entries | Quote-first allocation + fund guard prevents over-commitment |
| Intelligence data loss | No systems disabled — all keep computing |
| State.json backward compat | `slot_sticky` dict already exists; no breaking changes |
| Herd mode bugs | Deploys with `HERD_MODE_ENABLED=false`; manual toggle when ready |

---

## 11. Verification

1. **USD flow**: After Phase 1 deploy, cycling slots should have decreasing `available_usd`. B-entries consuming USD.
2. **Bilateral invariant**: Every cycling S0 slot has exactly 1 sell entry + 1 buy entry. No intelligence-forced `short_only`.
3. **Sticky slots unchanged**: Existing sticky slots still wait patiently. Their exits stay on Kraken.
4. **Advisory visible**: Status payload `advisory` block populated. Dashboard shows intelligence signals.
5. **Herd OFF**: No mode switching, no exit repricing, no churner/ranger.
6. **Herd ON**: Mode selection runs, exits managed, supplemental strategies fire.
7. **Stuck exit range**: When 3+ sticky exits exist, range is computed and displayed on dashboard.
8. **Tests**: `python -m pytest tests/` passes.

---

## 12. Future: Intelligence-Driven Mode Selection

Once the dual-mode system is stable, the herd mode intelligence can
grow to incorporate:

- **HMM regime → mode bias**: Trending markets → more sticky (exits
  need patience). Ranging → more cycling (capture oscillations).
- **BOCPD change-point → temporary all-sticky**: When a structural
  break is detected, pause cycling until the new regime stabilizes.
- **Throughput data → cycling priority**: Slots with best historical
  profit/time in cycling mode get priority to stay cycling.
- **MTS band → cycling budget**: Optimal/favorable → run more cyclers.
  Defensive/hostile → reduce to minimum cyclers.
- **Stuck exit overlap detection**: When two sticky exits are close
  in price, one can be released to cycling (redundant coverage).
- **Directional exit repricing**: In a confirmed trend, sticky exits
  on the against-trend side can be repriced by herd mode (not
  cancelled — repriced closer to market where they might fill).

The intelligence framework has all the data it needs. It just needs
to be pointed at exits and modes instead of entries.

---

## 13. Bauhaus Overlay + Factory Lens Integration

Both overlays live in `factory_viz.py` (5043 lines, served at
`GET /factory`). They consume `/api/status` JSON via `refreshStatus()`.

### 13.1 Current Blind Spots

The overlays read a **narrow slice** of the status payload — core
trading state only (slots, orders, recovery_orders, phases, price,
capacity). They are **completely blind** to the intelligence layer
and ledger, even though that data is already in the payload:

| Data | In Payload? | Read by Overlays? |
|------|:-----------:|:-----------------:|
| `slot.sticky` / `slot.slot_mode` | YES | **NO** |
| `slot.mode_source` | YES | **NO** |
| `hmm_regime` / `hmm_consensus` | YES | **NO** |
| `ai_regime_advisor` | YES | **NO** |
| `belief_state` / `action_knobs` | YES | **NO** |
| `manifold_score` (MTS) | YES | **NO** |
| `signal_digest` | YES | **NO** |
| `throughput_sizer` | YES | **NO** |
| `rebalancer` | YES | **NO** |
| `balance_health.ledger` (CapitalLedger) | YES | **NO** |
| `rangers` / `self_healing` (churner) | YES | **NO** |
| `accumulation` | YES | **NO** |
| `recovery_orders` (per slot) | YES | **YES** (orphan sprites + recycling belt) |
| `capacity_fill_health` | YES | **YES** (status bar + diagnosis) |

The overlays were built before the ledger, intelligence, and herd
mode existed. Every new feature must be wired into both overlays.

### 13.2 New Status Payload Fields

These fields must be added to `status_payload()` in `bot.py`:

```python
"herd_mode": {
    "enabled": self._flag_value("HERD_MODE_ENABLED"),
    "stuck_exit_range": self._compute_stuck_exit_range(),  # (low, high) | None
    "sticky_slot_count": sum(1 for s in self.slots.values() if getattr(s, "sticky", True)),
    "cycling_slot_count": sum(1 for s in self.slots.values() if not getattr(s, "sticky", True)),
    "pending_actions": self._herd_pending_action_preview(),  # list of planned actions
    "repriced_today": self._herd_repriced_today,
    "recoveries_closed_today": self._herd_recoveries_closed_today,
},
"advisory": {
    "throughput_recommended_a": tp_rec_a,  # what throughput sizer would set
    "throughput_recommended_b": tp_rec_b,
    "rebalancer_skew": self._rebalancer_current_skew,
    "rebalancer_recommended_mult": rebal_mult,
    "regime_suggestion": regime_suggestion_text,  # e.g. "suppress B" or "neutral"
},
```

Per-slot (already exists but now actively consumed):
```python
"sticky": bool(getattr(slot, "sticky", True)),
"slot_mode": "sticky" if getattr(slot, "sticky", True) else "cycle",
```

### 13.3 New Diff Events in `computeDiff()`

Add to the `computeDiff(prev, curr)` function in `factory_viz.py`:

| Event | Trigger | Data |
|-------|---------|------|
| `slot_mode_changed` | `prev.slot_mode != curr.slot_mode` | `{slot_id, from_mode, to_mode}` |
| `herd_toggled` | `prev.herd_mode.enabled != curr.herd_mode.enabled` | `{enabled}` |
| `range_changed` | `prev.stuck_exit_range != curr.stuck_exit_range` | `{low, high}` |
| `herd_reprice` | Recovery order price changed by herd mode | `{slot_id, recovery_id, old_price, new_price}` |
| `herd_close` | Recovery order removed by herd mode | `{slot_id, recovery_id}` |

### 13.4 Bauhaus Overlay Additions

#### Slot Mode Indicator

Slots currently render as rectangle outlines with faint yellow fill.
Add a mode distinction:

- **Cycling slots**: Existing solid outline (2px black stroke).
  Add a small rotating gear icon inside (indicates active cycling).
- **Sticky slots**: Dashed outline (`setLineDash([4, 4])`).
  Add a small hourglass or pause icon inside.

Read from `slot.slot_mode` (already in payload, just unused):
```javascript
const mode = slot.slot_mode || 'sticky';
if (mode === 'cycle') {
    ctx.setLineDash([]);  // solid
} else {
    ctx.setLineDash([4, 4]);  // dashed = sticky/waiting
}
```

#### Stuck Exit Range Band

Draw a horizontal band across the gullet at the range boundaries.
The gullet already maps price to vertical position via squared
distance from market. The stuck exit range band shows the
implied ranging zone:

- Two horizontal dashed lines at `range.low` and `range.high`
  Y-positions
- Faint yellow fill between them (`#F4C430` at alpha 0.08)
- Label: "RANGE" in small text at right edge

Only drawn when `herd_mode.stuck_exit_range` is non-null.

```javascript
function drawBauhausStuckRange(layoutView, status, nowMs) {
    const herd = status.herd_mode;
    if (!herd || !herd.stuck_exit_range) return;
    const [low, high] = herd.stuck_exit_range;
    const market = Number(status.price || 0);
    if (market <= 0) return;
    const lowY = priceToY(low, market, layoutView);
    const highY = priceToY(high, market, layoutView);
    // Faint yellow band
    ctx.fillStyle = 'rgba(244, 196, 48, 0.08)';
    ctx.fillRect(layoutView.membraneLeft, Math.min(lowY, highY),
                 layoutView.membraneRight - layoutView.membraneLeft,
                 Math.abs(highY - lowY));
    // Dashed boundary lines
    ctx.strokeStyle = BAUHAUS_COLORS.canvas;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(layoutView.membraneLeft, lowY);
    ctx.lineTo(layoutView.membraneRight, lowY);
    ctx.moveTo(layoutView.membraneLeft, highY);
    ctx.lineTo(layoutView.membraneRight, highY);
    ctx.stroke();
    ctx.setLineDash([]);
}
```

#### Herd Mode Indicator

When herd mode is ON, add a subtle treatment to the membrane:
- Small shepherd's crook icon (or `H` glyph) in the top-left corner
  of the Bauhaus canvas, in `BAUHAUS_COLORS.structure`
- When OFF: no indicator (default state)

#### Mode Transition Animation

When `slot_mode_changed` event fires:
- Slot outline morphs from solid to dashed (or vice versa) over
  600ms
- Brief pulse of yellow (`#F4C430` at alpha 0.4) expanding ring
- Tooltip auto-shows: "Slot N → cycling" or "Slot N → sticky"

#### Orphan Herding Animations

When `herd_reprice` event fires:
- Existing `orphan_repriced` animation already drifts the orphan
  sprite to its new position. This already works. Just ensure herd
  mode reprices emit the same event format.

When `herd_close` event fires:
- Existing `recovery_gone` animation already fades out the orphan
  sprite. Ensure herd mode closures emit this event.

#### Advisory Overlay (Optional, Toggle with `a` Key)

A light overlay mode showing intelligence recommendations:
- Per-slot: thin colored bar inside the slot rectangle showing
  recommended vs actual size (green if close, amber if divergent)
- Regime label text at top of membrane (e.g., "BULLISH 73%")
- MTS band color wash on the membrane edges

### 13.5 Factory Lens Additions

#### Machine Mode Badge

Each machine (slot) already shows phase-colored borders and
degraded state markers (`[LO]`/`[SO]`). Add:

- **Mode badge**: Small text label inside machine: `CYC` (green)
  or `STK` (yellow) for cycling/sticky mode
- Read from `slot.slot_mode`

#### Stuck Exit Range on Power Line

The power line runs horizontally across the top showing price.
Add range markers:
- Two small triangles or markers on the power line at the stuck
  exit range `low` and `high` prices
- Label: "RANGE" between them
- Faint yellow highlight between the markers

#### Herd Mode Status

Add to the existing status bar area:
- `HERD: ON` (green) or `HERD: OFF` (muted gray)
- When ON: show counts: `3 sticky / 9 cycling | range $0.170-$0.195`

#### Recovery Belt Enhancement

The recycling belt already shows colored dots for recovery orders.
Enhance with herd mode awareness:
- When herd mode is ON and targeting a recovery for soft-close,
  add a pulsing amber outline around that belt dot
- When a herd reprice happens, animate the dot sliding to its
  new position

#### CapitalLedger Visibility

Wire the CapitalLedger data into the input chests:
- **USD chest**: Show `available_usd` value inside the chest.
  Fill level proportional to `available_usd / total_usd`.
  Currently only shows full/empty based on `long_only` flag.
- **DOGE chest**: Show `available_doge` value. Fill level from
  `available_doge / total_doge`.

Read from `status.balance_health.ledger` (already in payload):
```javascript
const ledger = (status.balance_health && status.balance_health.ledger) || {};
const availableUsd = Number(ledger.available_usd || 0);
const totalUsd = Number(ledger.total_usd || 0);
```

### 13.6 New Diagnosis Symptoms

Add to the `diagnose(status)` function in `factory_viz.py`:

| Symptom ID | Severity | Condition | Visual Effect |
|------------|----------|-----------|---------------|
| `HERD_ACTIVE` | `info` | `herd_mode.enabled == true` | Subtle green tint on membrane |
| `USD_IDLE` | `warn` | `available_usd > min_b_entry * 3` and no cycling slots | Amber pulse on USD chest |
| `RANGE_TIGHT` | `info` | Stuck exit range < 2% of market price | Range band highlighted brighter |
| `RANGE_BROKEN` | `warn` | Market outside stuck exit range | Range band turns red-dashed |
| `ALL_STICKY_NO_HERD` | `warn` | All slots sticky, herd mode OFF, idle USD growing | Warning diamond on USD chest |

### 13.7 Implementation Phase (fits in Phase 4)

The overlay wiring is part of Phase 4 (Dashboard Advisory Panel).
Both `dashboard.py` and `factory_viz.py` are updated in the same
phase:

1. Add new status payload fields (`herd_mode`, `advisory`)
2. Add `computeDiff` events for mode transitions
3. Add Bauhaus: slot mode indicator, stuck range band, herd indicator
4. Add Factory: machine mode badge, power line range, belt enhancement
5. Wire CapitalLedger into input chests
6. Add diagnosis symptoms
7. Add advisory overlay (Bauhaus `a` key toggle)

---

## 14. Existing Overlay Patterns to Follow

Both overlays use established patterns. New features should follow
them exactly:

**Reading status data**: `const field = (statusData && statusData.x) || default;`

**Adding diagnosis symptoms**: Push to `symptoms[]` in `diagnose()` with
`{symptom_id, severity, priority, summary, affected_slots, visual_effects}`

**Adding animations**: Queue via `animQueue.push({type, ...data, startMs})`,
process in dedicated draw functions with elapsed-time progress curves

**Adding Bauhaus draw layers**: New `drawBauhausXxx()` function, called from
`renderBauhaus()` at the appropriate z-order position

**Adding Factory draw layers**: New `drawXxx()` function, called from
`renderFactory()` at the appropriate position

**Hit testing / tooltips**: Extend `resolveBauhausHoverTarget()` or
`hitTestMachine()` to return new target types

**Colors**: Bauhaus uses `BAUHAUS_COLORS` (void/canvas/structure/alert).
Factory uses `COLORS` (good/bad/warn/accent/ink/muted).
