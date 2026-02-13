# Bauhaus Overlay — The Charlie Brown Terminal

Version: v2.2.1-impl
Date: 2026-02-13
Status: Implemented (B0–B9 complete)
Scope: Additive 2D canvas overlay on Factory Lens (`/factory`), toggled via `b` key
File: `factory_viz.py` (embedded HTML/JS)
Original spec: v2.2.1 (frozen). This document reflects **as-implemented** behavior.
See Appendix A for deltas between original spec and implementation.

---

## 1. Goal

Render the bot's state machine as a side-view cross-section where **Y-axis = price distance from market**. Bauhaus functionalism meets Peanuts meets pixel art. The overlay is read-heavy, interaction-light — operator work stays in the dashboard; this view is for spatial intuition about where capital sits relative to the current price.

The overlay shares Factory Lens infrastructure: poller, diff engine, diagnosis system, keyboard FSM, command bar, notification strip.

---

## 2. Aesthetic

| Element | Value | Notes |
|---------|-------|-------|
| Canvas background | `#F4C430` (Dogecoin yellow) | Inner canvas fill |
| Frame stroke | `#E8881F` (orange) | Comic-panel border |
| Void | `#FFFFFF` (white) | Outside the frame |
| Structure / text | `#000000` / `#2B1B17` | Dark-on-light, inverted from factory |
| Alert | `#8B0000` (dark red) | Critical severity |
| S1a phase fill | `#00CED1` (dark cyan) | Short-side active |
| S1b phase fill | `#9B59B6` (purple) | Long-side active |
| S2 phase fill | `#E74C3C` (red) | Both exits pending |

All rendering is dark-on-light. No neon, no glow effects (except the slot flash highlight).

---

## 3. Layout

### 3.1 Frame Geometry

```
┌──────────────── white void ────────────────┐
│  ┌──────── orange frame stroke ─────────┐  │
│  │  ┌──── yellow canvas (inner) ─────┐  │  │
│  │  │                                │  │  │
│  │  │   sell orders above            │  │  │
│  │  │   ─── price line (center) ──── │  │  │
│  │  │   buy orders below             │  │  │
│  │  │                                │  │  │
│  │  │   [slots along price line]     │  │  │
│  │  │                                │  │  │
│  │  │              [Ð counter] (TR)  │  │  │
│  │  └────────────────────────────────┘  │  │
│  └──────────────────────────────────────┘  │
└────────────────────────────────────────────┘
```

- **Outer padding**: 18px from viewport edges
- **Frame stroke**: 0.8–1.2% of min(viewportW, viewportH), clamped 8–12px
- **Inner canvas**: outer minus frame stroke on each side
- **Price line Y**: vertical center of inner canvas (`inner.y + inner.h * 0.5`)

### 3.2 Slot Positioning

Slots are rendered as black-outlined rounded rectangles centered on the price line.

- Slot sizing scales with count: ≤10 → 80×40, ≤20 → 50×28, >20 → 40×24
- Width further constrained by `(usableWidth - gaps) / slotCount`
- Clamped: width 28–90px, height 22–48px
- Horizontally centered with 8px gaps
- Phase label centered inside each slot (bold monospace)

### 3.3 Profit Counter (Top-Right)

- Height: 9% of inner height, clamped 32–46px
- Width: 23% of inner width, clamped 150–220px
- Position: top-right of inner canvas, 16px margin from edges
- Black rounded-rect background, white seven-segment LED digits

---

## 4. Y-Axis: Squared-Distance Mapping

Orders are positioned vertically by their price distance from market.

### 4.1 Formula

```
pctDistance = |orderPrice - marketPrice| / marketPrice
yOffset = pctDistance² × 25,000,000
direction = sell → -1 (above price line), buy → +1 (below)
y = priceY + direction × min(yOffset, clampMax)
```

### 4.2 Clamp Behavior

- **Clamp threshold**: 45% of canvas half-height (`inner.h × 0.5 × 0.45`)
- Orders beyond the clamp render at the boundary with **dashed hairlines** (`[2,2]` dash pattern)
- Orders within range get solid hairlines
- The `clamped` flag is carried through to tooltips and fill animations

### 4.3 Scale Factor Rationale

Scale factor 25,000,000 gives:
- 0.10% distance → 25px offset
- 0.20% distance → 100px offset (4× for 2× price distance — squared emphasis)
- Tight grid entries cluster near the price line; distant orphans spread to edges

---

## 5. Price Line Dynamics (B4)

The price line is a horizontal bar centered at `priceY`, spanning the inner canvas width (minus 14px padding each side).

### 5.1 Asymmetric Thickness

The line has independent sell (above) and buy (below) thickness representing DOGE-equivalent capital exposure on each side.

**Capital aggregation** (`aggregateBauhausCapitalDoge`):
- Sell side: direct DOGE volume from sell orders + sell-side recovery orders
- Buy side: USD-denominated orders converted via `(volume × price) / refPrice`
- Sources: `open_orders` + `recovery_orders` from all slots

**Rolling window normalization**:
- 60-poll window (~5 minutes at 5s polling)
- Each side tracks its own history array
- Current thickness = `(currentDoge / rollingMax) × 48px`, clamped 1–48px
- Zero-guard: if rolling max is 0, thickness = 1px (v2.2.1 Patch C)

### 5.2 Stale-Price Behavior

| Age (sec) | Line lightness | Sparkle speed | State |
|-----------|---------------|---------------|-------|
| 0–10 | 0% (black) | 0.16 px/ms | Live |
| 10–60 | 0→24% (greying) | 0.06 px/ms | Aging |
| >60 | 36% (grey) | stopped | Dead |

**Sparkles**: 1px white dots, right-to-left motion, clipped to line rectangle bounds. Speed scaled by `motionFactor` from visual state.

---

## 6. Order Rendering (B2)

### 6.1 Markers

| Role | Visual | Size |
|------|--------|------|
| Entry | White 6×6 square, black outline | 6px |
| Exit | Black 6×6 solid square | 6px |
| Other | Grey 6×6 solid square | 6px |

### 6.2 Hairlines

Each order has a 1px grey (`#999999`) hairline connecting it to its slot's anchor point.

- Sell-side anchor: top edge of slot (`node.y`)
- Buy-side anchor: bottom edge of slot (`node.y + node.h`)
- Hairline hidden when order is within 5px of slot
- 8px horizontal nudge when multiple orders overlap at same slot

### 6.3 Diagnosis Effects on Orders

- **Jammed exits** (BELT_JAM / `conveyor_stop`): Darker hairline (`rgba(120,120,120,0.95)`), pulsing width
- **Starved side** (`long_only` / `short_only`): Orders on the suppressed side are hidden entirely; remaining orders dimmed to 78% alpha
- **Global dimming**: `orderAlpha` from visual state (halted=0.42, brownout=0.72)

---

## 7. Orphan Field (B3)

Recovery orders render as pixel-art plus-sign sprites scattered across the canvas.

### 7.1 Sprite Design

```
    ██          (arm: up)
  ██████        (arm: left, center: black, arm: right)
    ██          (arm: down)
```

- 2px per cell → 6×6 physical pixels
- Center pixel: always black (`BAUHAUS_COLORS.structure`)
- Arm pixels: color from gradient

### 7.2 Color Gradient

Orphans are rank-ordered by price distance from market (re-ranked every poll as price moves).

| Rank position | Hue | Meaning |
|---------------|-----|---------|
| 0.0 (closest) | 275° (violet, `#8A2BE2`) | Near recovery |
| 1.0 (farthest) | 10° (dark red, `#8B0000`) | Deep underwater |

- Saturation: 40–90%
- Lightness: 44–64%
- Center desaturation: orphans near the price line are slightly less saturated

### 7.3 Positioning

- **Composite key**: `${slot_id}:${recovery_id}` (v2.2 Patch 1 — recovery_id not globally unique)
- **FNV-1a hash** of composite key → deterministic seed
- **X**: random within inner canvas (minus margin)
- **Y**: 68% distance-correlated + 32% random, ±7px jitter, clamped to inner bounds
  - `distNorm = rankNorm^0.78` (slight compression of distance mapping)
  - Side direction: sell orphans above price line, buy below
- **Twinkle**: alpha oscillates 0.7–1.0 via `sin()`, period 2–4s (seeded per orphan)

### 7.4 Repriced Orphans (B7)

When the diff engine detects a recovery order's price changed (epsilon > 1e-9):

1. `orphan_repriced` event emitted from `computeDiff()`
2. Orphan animates from old position to new over 900ms (smooth-step easing)
3. During transit: original color fades out, grey (`#8A8A8A`) fades in
4. On completion: animation removed, orphan renders normally at new position with new ranked color

---

## 8. Profit Counter (B5)

### 8.1 Seven-Segment LED Display

- Retro LED aesthetic: black background, white-on-black digits
- Off-segments visible as ghost outlines (`rgba(255,255,255,0.15)`)
- Format: `Ð 15.04` — Dogecoin symbol prefix, 2 decimal places
- Segment thickness: 16% of min(glyph width, glyph height), minimum 2px
- All standard digits (0–9), minus sign, decimal point, and Ð glyph

### 8.2 Two-Stage Profit Trigger (v2.2 Patch 7)

Profit counter updates are **cycle-confirmed**, not order-disappearance-triggered:

1. **Stage 1 — Dissolve**: When an order disappears (`order_gone` event from diff engine), the B6 fill animation plays. This is visual-only and does NOT update the counter.
2. **Stage 2 — Profit flight**: When `recent_cycles` gains a new entry (`cycle_completed` event), fragment particles fly from the slot to the counter. The counter increments only when fragments arrive.

This prevents false animations on cancels, reprices, or expired orders.

### 8.3 Fragment Flights

- Origin: slot center (fallback: canvas center)
- Destination: counter top-left area
- Duration: 760ms, smooth-step easing
- 8 rotating particles per flight, shrinking radius
- Positive profit: black particles, negative: dark red
- Delta applied to `bauhausProfitDisplayed` on arrival (progress ≥ 1)
- Reconciliation: when no flights remain, snap displayed to `total_profit` target (catches float drift)
- 80-flight cap with FIFO trim

---

## 9. Fill Animations (B6)

When an order disappears, a two-phase animation plays:

### 9.1 Phase 1 — Line Extend

- A thin black line extends from the **slot anchor** to the order's last-known position
  - Sell-side: line starts from slot top edge
  - Buy-side: line starts from slot bottom edge
  - Fallback (no slot found): line starts from price line center
  - **Delta from original spec**: Spec said line extends "from the price line." Implementation extends from slot edge, which is more visually coherent since orders are already connected to slots via hairlines.
- Speed: ~100px/s, clamped 160–760ms
- Order square rendered at full opacity during extend

### 9.2 Phase 2 — Dissolve

- Order square fades out (`1 - t × 1.25`, faster than linear)
- Fragment burst: 9 particles for entries, 12 for exits
- Fragment colors match order type: exit=black, entry=white, other=grey
- **Exit-specific dark sparkles**: 10 additional particles at `rgba(25,25,25,...)`, larger radius spread

### 9.3 Position Lookup

- Previous-state order point index built from `computeBauhausOrderPoints()` on old status snapshot
- Key matching via `bauhausOrderEventKey()`: prefers txid, falls back to local_id, then composite fallback
- If order wasn't in previous frame: estimated from order price using same squared-distance formula
- Critical: enqueue happens **before** `statusData = next` in `refreshStatus()` — old snapshot is still available

### 9.4 Constraints

- 180-animation cap with FIFO trim
- Motion scaled by `motionFactor` (halted=0.05 floor, paused=0.25)

---

## 10. Diagnosis Overlays (B8)

### 10.1 Visual State Hierarchy

`getBauhausVisualState()` derives cascading severity:

| State | Condition | motionFactor | slotAlpha | orderAlpha |
|-------|-----------|-------------|-----------|------------|
| **Halted** | `mode=HALTED` or `power_dead` | 0 (floor 0.05) | 0.55 | 0.42 |
| **Paused** | `mode=PAUSED` | 0.25 | 0.82 | 0.72 |
| **Brownout** | paused or `power_dim` or `amber_wash` | 0.55 | 0.82 | 0.72 |
| **Normal** | none of the above | 1.0 | 1.0 | 1.0 |

### 10.2 Global Overlays (draw order: last, over everything)

| Overlay | Trigger | Effect |
|---------|---------|--------|
| **Desaturation** | brownout or paused | `globalCompositeOperation='saturation'` grey fill at 82% (paused) or 45% (brownout), plus warm tint `rgba(244,236,206,...)` |
| **Red wash** | halted or `red_wash` effect | Dark umber tint `rgba(82,44,24,...)` at 10–20% |
| **Vignette** | halted | Radial gradient, transparent center → 60% black edges |
| **Circuit sparks** | `circuit_spark` effect | 16 particles along frame border, alternating dark red / orange, flickering alpha |

### 10.3 Slot-Level Effects

| Effect | Trigger | Visual |
|--------|---------|--------|
| **Belt jam pulse** | `conveyor_stop` + S2 phase | Alpha oscillates 0.62–1.0, stroke width oscillates 1.8–3.0 |
| **Starvation dim** | `machine_dark` or `long_only`/`short_only` | Alpha ×0.88, warning triangle badge with `!` icon and `[LO]`/`[SO]` label |

### 10.4 Notification Strip Styling

In Bauhaus mode, the notification strip adapts:

| Property | Bauhaus | Factory |
|----------|---------|---------|
| Background | `rgba(244,196,48,0.95)` (yellow) | `rgba(22,27,34,0.96)` (dark) |
| Border | orange (`#E8881F`) | varies by severity |
| Text | black | white |
| Severity labels | `CRIT` / `WARN` / `OK` | `!!` / `!` / `OK` |
| Idle message | `✓ Running` | `OK Factory running normally` |

---

## 11. Interactivity (B9)

### 11.1 Hover Tooltips

Hit-testing priority (checked first = highest priority):

| Priority | Target | Hit zone | Tooltip content |
|----------|--------|----------|-----------------|
| 1 | Orders | 10×10px square | side/role, price ($), volume, Δ% from market |
| 2 | Orphans | 7px radius circle | recovery_id, side, price, volume, age (sec), Δ% |
| 3 | Slots | Slot bounding rect | slot_id, phase, profit ($), order count, LO/SO flags |
| 4 | Profit counter | Counter bounding rect | total profit ($), cycle count |
| 5 | Price line | Price line rect | price ($), age (sec), DOGE sell/buy capital |

Small targets (orders, orphans) are checked before containers (slots, counter, price line). Reverse iteration ensures top-rendered elements are tested first.

### 11.2 Tooltip Style

- Bauhaus: black background (`rgba(0,0,0,0.94)`), white border, white monospace text
- Factory: dark background, blue border, standard ink text
- Smart positioning: 14px right / 16px down, flips near edges, clamped to viewport

### 11.3 Click-to-Pin

- **Left-click on target**: Pin tooltip at click position. Content stays locked even as cursor moves.
- **Left-click on empty**: Unpin and clear tooltip.
- **Mouse leave with pin**: Tooltip persists (pin overrides leave-clear).
- Pinned tooltip text is a snapshot — reflects data at click time, not live.

### 11.4 Slot Flash

When a slot is selected (via click or `:jump N`):
- White glow ring (`rgba(255,255,255,0.95)`) expands outward over 500ms
- Growth: 2→8px beyond slot bounds
- Stroke width tapers from 3.2 to 2.0
- Alpha fades from 1.0 to 0.0

### 11.5 Esc Priority Chain (v2.2 Patch 5)

Each press of Esc resolves the highest-priority active state:

```
1. Unpin tooltip (if pinned)
2. Exit Bauhaus → Factory view
3. (Factory-mode Esc behavior follows)
```

### 11.6 Disabled Interactions

In Bauhaus mode, the following factory-mode interactions are suppressed:
- Pan/drag (no `dragging` state initiated)
- Zoom (wheel `preventDefault` fires, then early return)
- Detail panel (not rendered)
- Context menu (no-op)

---

## 12. Render Mode System (B0)

### 12.1 Mode Toggle

- `b` key toggles between Factory and Bauhaus
- `:q` command exits current view (Bauhaus → Factory)
- Mode persisted in `localStorage` key `factory_render_mode`
- Toast notification on toggle: "Bauhaus overlay on" / "Factory view on"

### 12.2 Render Routing

```
renderCurrentView(nowMs)
  ├── RENDER_MODE_BAUHAUS → renderBauhaus(nowMs)
  └── RENDER_MODE_FACTORY → renderFactory(nowMs)
```

### 12.3 Mode Transition Cleanup

**Entering Bauhaus**: Clear factory dragging state, selectedSlotId, tooltip, detail panel.

**Leaving Bauhaus**: Clear all Bauhaus state:
- `bauhausFillAnims`, `bauhausOrphanRepriceAnims`
- `bauhausPinnedTooltip`, `bauhausSlotFlash`
- `bauhausLastOrderPoints`, `bauhausLastOrphanSprites`
- `bauhausLastPriceLineRect`, `bauhausLastCounterRect`
- `tooltipText`, `hoverSlotId`

---

## 13. Bauhaus Render Order

Draw calls in `renderBauhaus()`, bottom to top:

```
1. drawBauhausFrame          — white void, yellow canvas, orange stroke
2. drawBauhausPriceLine      — asymmetric bar + sparkles         → returns rect
3. drawBauhausSlots          — rounded rects + phase labels + flash
4. drawBauhausOrders         — hairlines + square markers         → returns points[]
5. drawBauhausFillAnimations — line extend + dissolve + fragments
6. drawBauhausOrphans        — plus sprites + repriced drift      → returns rendered[]
7. drawBauhausProfitFlights  — particle arcs to counter
8. drawBauhausProfitCounter  — LED seven-segment display          → returns rect
9. drawBauhausDiagnosisOverlays — desat, vignette, sparks (post-process)
10. drawTooltip              — hover/pinned tooltip (screen-space)
```

Functions at steps 2, 4, 6, 8 return geometry used for hover hit-testing.

---

## 14. Diff Engine Extensions

### 14.1 Recovery Mutation Detection (B7)

The `computeDiff()` function was enhanced to detect recovery order repricing:

- Recovery orders matched by composite key `${slot_id}:${recovery_id}`
- Price comparison with epsilon `1e-9` (`BAUHAUS_REPRICE_EPSILON`)
- New recoveries (no prev match) → `order_orphaned` event (unchanged)
- Price change detected → `orphan_repriced` event with `old_price`, `new_price`, `recovery_key`
- `orphan_repriced` events are **excluded** from the factory-mode `animQueue` (Bauhaus-only)

### 14.2 Events Consumed by Bauhaus

| Event | Source | Bauhaus Consumer |
|-------|--------|-----------------|
| `order_gone` | Order disappeared between polls | `queueBauhausFillAnimations()` |
| `cycle_completed` | New entry in `recent_cycles` | `queueBauhausProfitFlights()` |
| `orphan_repriced` | Recovery price changed | `queueBauhausOrphanRepriceAnimations()` |

All enqueue functions are called **before** `statusData = next` to access the previous snapshot for position lookups.

---

## 15. Configuration Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `BAUHAUS_THICKNESS_WINDOW_POLLS` | 60 | Rolling window for price line normalization |
| `BAUHAUS_MAX_SIDE_THICKNESS_PX` | 48 | Max price line half-thickness |
| `BAUHAUS_REPRICE_EPSILON` | 1e-9 | Minimum price change to trigger orphan repriced |
| Y-axis scale factor | 25,000,000 | Squared-distance emphasis for order positioning |
| Clamp threshold | 45% of half-height | Maximum Y offset before dashed rendering |
| Motion factor floor | 0.05 | Prevents permanent animation freeze when halted |
| Fill animation cap | 180 | Max concurrent fill animations |
| Profit flight cap | 80 | Max concurrent profit flights |
| Orphan reprice cap | 260 | Max concurrent reprice animations |
| Slot flash duration | 500ms | White glow highlight on selection |
| Profit flight duration | 760ms | Fragment arc to counter |
| Reprice drift duration | 900ms | Orphan position transition |
| Fill extend speed | ~100px/s | Line extension to vanished order |
| Fill dissolve (exit) | 360ms | Exit order fragment burst |
| Fill dissolve (entry) | 300ms | Entry order fragment burst |

---

## 16. Data Dependencies

All data comes from the existing `/api/status` endpoint. No new endpoints or fields required beyond what Factory Lens v1 already consumes.

Key status fields used:
- `status.price`, `status.price_age_sec` — price line position + stale detection
- `status.mode` — HALTED/PAUSED visual state
- `status.total_profit` — profit counter target
- `status.slots[].phase` — slot phase rendering
- `status.slots[].open_orders[]` — order markers + hairlines
- `status.slots[].recovery_orders[]` — orphan sprites + mutation detection
- `status.slots[].recent_cycles[]` — cycle-confirmed profit trigger
- `status.slots[].long_only`, `status.slots[].short_only` — starvation effects
- `status.slots[].total_profit` — slot tooltip
- `status.slots[].total_round_trips` — cycle count
- `capacity_fill_health.*` — diagnosis engine input

---

## 17. Implementation Milestones (All Complete)

| Milestone | Scope | Status |
|-----------|-------|--------|
| **B0** | Render mode scaffolding, event handler refactor, `renderCurrentView()` router | Done |
| **B1** | Static cross-section: frame, price line, slots, `b` toggle, Esc exit | Done |
| **B2** | Order squares + Y-axis squared-distance mapping + clamp + hairlines | Done |
| **B3** | Orphan field: FNV-1a scatter, ranked color gradient, twinkle, plus sprites | Done |
| **B4** | Price line dynamics: asymmetric thickness, rolling window, sparkles, stale fade | Done |
| **B5** | Profit counter: seven-segment LED, fragment flights, two-stage trigger | Done |
| **B6** | Fill animations: line extend → dissolve → dark sparkles on exits | Done |
| **B7** | Diff engine: recovery mutation detection, repriced-orphan drift animation | Done |
| **B8** | Diagnosis overlays: desaturation, vignette, circuit sparks, motion scaling, notification strip | Done |
| **B9** | Interactivity: hover tooltips, click-to-pin, slot flash, Esc chain | Done |

---

## Appendix A: Deltas from Original Spec (v2.2.1 frozen)

Comparison of the original frozen spec against as-implemented behavior. Items marked **MATCHES** are faithful to spec. Items marked **DELTA** diverge.

### A.1 Faithful Implementations

| Feature | Notes |
|---------|-------|
| Frame geometry (white void / orange / yellow) | Exact match |
| Palette (all 9 BAUHAUS_COLORS) | Exact match |
| Squared-distance Y-axis (formula, scale factor 25M, 45% clamp, dashed hairlines) | Exact match |
| Price line asymmetric thickness + rolling 60-poll window + zero-guard | Exact match |
| Price line sparkles + stale fade + speed scaling | Exact match |
| Order markers (white entry / black exit / grey other, 6×6px) | Exact match |
| Orphan color gradient (violet→red via HSL hue sweep through blue/green/yellow) | Exact match |
| Orphan FNV-1a deterministic scatter + composite key | Exact match |
| Orphan twinkling (0.7–1.0, 2–4s seeded period) | Exact match |
| Repriced orphan animation (grey transition + drift, 900ms) | Exact match |
| Diff engine mutation detection (composite key, 1e-9 epsilon) | Exact match |
| Profit counter LED display (Ð prefix, 2 decimal, seven-segment) | Exact match |
| Two-stage profit trigger (dissolve visual-only, profit on `cycle_completed`) | Exact match |
| Exit-specific dark sparkles on dissolve | Exact match |
| PAUSED desaturation (~80% grayscale) | Exact match |
| HALTED vignette (radial gradient, transparent center → 60% black edges) | Exact match |
| Circuit sparks along frame border | Exact match |
| Notification strip Bauhaus styling (yellow bg, dark text, severity labels) | Exact match |
| Hover tooltips for all 5 target types with correct content | Exact match |
| Click-to-pin / Esc-unpin | Exact match |
| Slot highlight flash (white glow, ~500ms) | Exact match |
| Esc priority chain (unpin → exit Bauhaus) | Exact match |
| Pan/zoom disabled in Bauhaus | Exact match |
| `b` toggle + `:q` exit + localStorage persistence | Exact match |
| `requestAnimationFrame` at 30fps (33ms gate) | Exact match |
| Command bar shared with Factory v1 | Exact match |

### A.2 Deltas

#### D1. Fill animation line origin

- **Spec**: "A thin black line (1px) extends from the price line toward the order square."
- **Impl**: Line extends from the **slot's edge** (top for sell, bottom for buy), not the price line center. Price line is only the fallback when no slot is found.
- **Rationale**: More visually coherent — orders are connected to their parent slot via hairlines, so the fill animation line follows the same parentage relationship.
- **Severity**: Low. Aesthetic improvement.

#### D2. Two-poll grace period for dissolve-to-cycle matching

- **Spec**: "If no matching cycle appears within two consecutive polls (~10 seconds), treat as cancel — fragments dissipate in place, no counter update."
- **Impl**: Dissolve animations (B6) and profit flights (B5) are **completely independent systems**. There is no timeout linking them. Dissolves always play on `order_gone`. Profit flights always play on `cycle_completed`. No "dissipate in place" fallback exists.
- **Impact**: If an order is canceled (not filled), the dissolve animation plays but no profit flight ever occurs — which is the correct visual outcome. The two-poll timeout was meant to handle the case where dissolve and cycle events arrive on different polls, but since they're independent, this is a non-issue in practice. The counter reconciles to `total_profit` whenever no flights are pending.
- **Severity**: Low. The decoupled design achieves the same end result.

#### D3. Dissolve-to-cycle matching by (slot_id, trade_side)

- **Spec**: "Each dissolve event is matched to a confirming `recent_cycles` entry by `(slot_id, trade_side)`. Trade side is inferred from the dissolved exit order's side: buy exit → trade A, sell exit → trade B."
- **Impl**: No matching between dissolve events and cycle events exists. Profit flights trigger on any `cycle_completed` event with a finite `net_profit`, matched only by `slot_id`. Trade side is not checked.
- **Impact**: Same as D2 — the decoupled design makes this matching unnecessary.
- **Severity**: None. Functionally equivalent.

#### D4. Recovery fill special handling (grey fragments, from_recovery gating)

- **Spec**: "Recovery fills produce grey fragments flying to profit counter only when confirmed by `recent_cycles` with `from_recovery=True`."
- **Impl**: All profit flights use the same colors (black for positive, dark red for negative). No `from_recovery` check. Recovery cycle completions are treated identically to normal cycle completions in all animation code.
- **Impact**: Recovery fills look the same as normal fills visually. The counter still updates correctly since `net_profit` from recovery cycles can be negative, which is handled.
- **Severity**: Low. Minor visual distinction lost.

#### D5. Phase change crossfade (200ms)

- **Spec**: "brief 200ms crossfade" for phase transitions.
- **Impl**: Phase colors snap instantly. No crossfade animation in Bauhaus mode. (Factory mode has its own phase animation via `drawEventAnimations`, but that's not called in Bauhaus.)
- **Severity**: Low. Phase changes are infrequent and the snap is clean.

#### D6. Order placed fade-in

- **Spec**: "Order square fades in at calculated Y-position with hairline connecting to slot."
- **Impl**: New orders appear instantly at full opacity on the next render frame after the poll delivers them.
- **Severity**: Low. Cosmetic.

#### D7. Order orphaned transformation animation

- **Spec**: "Order square detaches from slot hairline → transforms into pixel-art plus sign → drifts to Poisson position → begins twinkling."
- **Impl**: No transformation animation. When an order disappears from `open_orders` and a recovery appears in `recovery_orders`, the dissolve animation plays at the old order position (B6), and the orphan plus sign appears at its Poisson position on the next frame. No visual link between the two.
- **Severity**: Medium. This was one of the spec's signature animation sequences. The dissolve + instant orphan appearance is functional but loses the metamorphosis visual.

#### D8. Slot added stroke animation (300ms draw-on)

- **Spec**: "Slot outline draws itself on (stroke animation, ~300ms)."
- **Impl**: New slots appear instantly with full stroke. (Factory mode has a `slot_added` animation via `drawEventAnimations`, but not used in Bauhaus.)
- **Severity**: Low. Slot additions are rare events.

#### D9. Factory v1 pan/zoom state preservation

- **Spec**: "Factory v1's pan/zoom state should be preserved and restored when toggling back with b."
- **Impl**: `setRenderMode()` does not save or restore `camera.x`, `camera.y`, `camera.zoom`. When returning to Factory mode, the camera state is whatever it was before (it's not reset either — it's just left as-is). Since Bauhaus doesn't modify the camera, the camera position survives the round-trip, but `selectedSlotId` is cleared on enter.
- **Impact**: Camera position is effectively preserved (Bauhaus never touches it), but selected slot is lost.
- **Severity**: Low. The camera works by accident. Selected slot loss is minor.

#### D10. Canvas minimum size (800×500)

- **Spec**: "Minimum usable size: 800×500 inner canvas."
- **Impl**: Inner canvas minimum is 100×80px. The layout degrades gracefully to small sizes but doesn't enforce the spec's floor.
- **Severity**: None in practice. The overlay is used on desktop monitors where viewport is always >> 800×500.

### A.3 Additions Beyond Original Spec

Features implemented that weren't in the original spec:

| Addition | Description |
|----------|-------------|
| **Brownout visual state** | Intermediate state (paused or `power_dim` or `amber_wash`) with `motionFactor=0.55`. Original spec had only PAUSED and HALTED. |
| **Motion factor floor (0.05)** | Prevents permanent animation freeze during HALTED. Spec said animations "stop" but implementation ensures they complete at 5% speed. |
| **Red wash overlay** | Dark umber tint on `red_wash` effect or HALTED state. Not explicitly in original spec's overlay table. |
| **Warm tint on desaturation** | Yellowish `rgba(244,236,206,...)` overlay stacked with desaturation. Creates a "powered down but warm" feel not described in spec. |
| **RECOVERY_BACKLOG visual** | Orphan field: `belt_overflow` effect triggers higher twinkle amplitude (0.8±0.2 vs 0.85±0.15). Not explicitly specified. |
| **Halted orphan freeze** | Orphan twinkle stops entirely when halted (`twinkleScale=0`, fixed alpha 0.72). Spec said "all twinkling stops" but didn't specify the frozen alpha. |
| **Interaction geometry caching** | Draw functions return geometry for hover hit-testing. Implementation detail not in spec. |
| **`bauhausCycleCount` three-tier fallback** | `total_round_trips` → sum of slot-level → count of `recent_cycles`. Handles varying API shapes. |

### A.4 Summary

| Category | Count |
|----------|-------|
| Faithful to spec | 26 features |
| Deltas | 10 items (D1–D10) |
| Additions beyond spec | 8 items |

**Critical deltas**: None. All deltas are low-to-medium severity cosmetic/animation differences. The core data model, Y-axis mapping, orphan field, profit counter trigger logic, diagnosis overlays, and interactivity all match the original spec.

**Most notable gap**: D7 (orphan transformation animation) — the spec's signature "order metamorphoses into orphan star" sequence is not implemented. The dissolve and orphan appearance happen independently. This could be added as a polish pass.
