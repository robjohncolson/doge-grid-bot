# Bauhaus Overlay -- As Implemented

Version: v2.3.1-impl
Date: 2026-02-13
Status: Implemented in `factory_viz.py`
Scope: Bauhaus rendering mode on `/factory` (toggled with `b`)

---

## 1. Authority and Scope

This document describes the current implementation in `factory_viz.py`.

- Frozen v2.2.1 spec remains authoritative for: poller contract, diff semantics,
  keyboard command grammar, and API payload shape.
- This document is authoritative for: Bauhaus visual rendering and Bauhaus-mode
  UI presentation behavior.

---

## 2. Mode and Shell Behavior

Render modes:
- `factory`
- `bauhaus`

Mode controls:
- `b` toggles mode.
- `:q` exits Bauhaus to Factory.
- `Esc` in Bauhaus: unpin tooltip first, otherwise exits to Factory.

Persistence:
- Mode stored in localStorage key `factory_render_mode`.

Bauhaus shell hiding:
- Entering Bauhaus hides these HTML elements with `display: none`:
  - `hudTop`
  - `detailPanel`
  - `notifStrip`
  - `statusBar`
  - `cmdBar`
  - `cmdSuggestions`
- Exiting Bauhaus restores them.

Interactive overlays in Bauhaus:
- Command/help/confirm remain HTML overlays (not canvas-drawn).
- When opened in Bauhaus, they are restyled to Bauhaus palette:
  yellow background, black text/borders, monospace input.
- When closed or on exit from Bauhaus, styles are reverted.

---

## 3. Canvas Layer Order (Bauhaus)

Per `renderBauhaus()` draw order:

1. `drawBauhausFrame` -- clear + white void
2. `drawBauhausPriceLine` -- orchestrates:
   - black gullet path
   - yellow membrane
   - stationary sparkles clipped to gullet
3. `drawBauhausSlots`
4. `drawBauhausOrders`
5. `drawBauhausFillAnimations`
6. `drawBauhausRecoveryDissolveAnimations`
7. `drawBauhausOrphans`
8. `drawBauhausProfitFlights`
9. `drawBauhausProfitCounter`
10. `drawBauhausCanvasNotifStrip`
11. `drawTooltip`
12. `drawBauhausDiagnosisOverlays`
13. Browser HTML overlays above canvas (command/help/confirm when open)

---

## 4. Layout Model

`computeBauhausLayout(status)` defines:

- Membrane rectangle inset from viewport edges: horizontal `clamp(viewportW * 0.04, 50, 80)` px, vertical 24px.
- `centerY = viewportH * 0.5`.
- `membraneLeft = membrane.x`
- `membraneRight = membrane.x + membrane.w`
- `canvasW = viewportW`, `canvasH = viewportH`

Legacy compatibility aliases retained:
- `inner` and `outer` both point to membrane rect.
- `priceY` points to `centerY`.

Slot sizing:
- Base sizes by slot count:
  - <= 10: 80x40
  - <= 20: 50x28
  - > 20: 40x24
- Width clamped by available horizontal room.
- Final clamp: width 28-90, height 22-48.

---

## 5. Gullet Geometry and Thickness

`buildGulletPath(layoutView, channelHalfTop, channelHalfBot)` builds a full
canvas `Path2D` with cubic-bezier funnels:

- Narrow center channel through membrane.
- Funnels to full canvas height at left/right edges.
- Top and bottom halves are mirrored with independent top/bottom half-heights.

Half-heights come from asymmetric capital thickness:
- `computeBauhausSideThicknesses()`
- Rolling-window normalization using:
  - `BAUHAUS_THICKNESS_WINDOW_POLLS = 60`
  - `BAUHAUS_MAX_SIDE_THICKNESS_PX = 48`
- Capital basis: DOGE-equivalent exposure from `open_orders + recovery_orders`
  via `aggregateBauhausCapitalDoge()`.

Staleness color:
- Gullet fill is grayscale via lightness 0..36.
- `staleLevel = 0` fresh black.
- `staleLevel = 1` fully stale/dim.
- Dead/stale condition when `price_age_sec > 60` or halted state.

---

## 6. Membrane Rendering

`drawBauhausMembrane()` draws the yellow membrane (`#F4C430`) as two wobbled
filled paths (top and bottom), leaving the channel opening between them.

Wobble:
- `membraneWobble(position, seed)` seeded static 1D noise.
- Approx range: +/-3 px.
- Sample spacing: 12 px.

Channel boundaries (where membrane meets gullet opening) are straight for a
clean notch transition.

---

## 7. Sparkles (Stationary)

State:
- `bauhausSparkles[]` entries contain:
  - `x, y`
  - `freq, phase`
  - `opacity`
  - `fadingOut, fadeStartMs, birthMs`
- `bauhausSparklesLastPollMs` throttles population updates.

Population update (`updateBauhausSparkles(priceAgeSec, nowMs)`):
- Recomputed no more than once per ~4s (time-gated).
- Target count uses estimated area:
  - `estimatedArea = centralStripArea * 2.5`
  - density approx `1 sparkle / 400 px^2`
  - freshness multiplier `max(0, 1 - price_age_sec / 60)`
- Excess sparkles are marked `fadingOut` (not popped).

Placement:
- Rejection sampling against full gullet path using
  `ctx.isPointInPath(bauhausGulletPath, x*dpr, y*dpr)`.
- Fallback to gullet bounds if sampling fails.

Rendering (`drawBauhausSparkles(nowMs)`):
- No directional flow.
- Opacity twinkle via sinusoid + fade-in + fade-out.
- Clipped to gullet path.

---

## 8. Slots

Visual style:
- Slightly opaque yellow slot interior (`#F4C430`) with low alpha.
- Phase tint overlay:
  - S1a, S1b, S2 use alpha baseline 0.15, clamped [0.02, 0.25] (modulated by state effects).
  - Jammed/starved multipliers apply to tint layer only.
- Outline: `strokeRect`, black, 2px base.
- Phase text centered in monospace (10-12px based on slot height).

Degraded indicators:
- No `[LO]` / `[SO]` text in slot body.
- Small black triangle outside slot:
  - `long_only`: downward marker below slot
  - `short_only`: upward marker above slot

Slot flash:
- White expanding stroke ring for selected slot, ~500ms.

---

## 9. Orders and Hairlines

Y positioning:
- Squared distance mapping with scale factor `25,000,000`.
- Clamp at `45%` of half membrane height (`BAUHAUS_ORDER_MAX_OFFSET_RATIO=0.45`).

Markers (`drawBauhausOrderSquare`):
- Size: 7x7.
- Entry: white fill + black 1px stroke.
- Exit: solid black.
- Other: gray.

Hairlines:
- Base: `rgba(100,100,100,0.4)`, 1px.
- Jammed exits: darker pulsing line.
- Dashed only when clamped: `[3,3]`.
- Hidden when order is within 6px of anchor.

---

## 10. Fill Animations

Triggered by `order_gone`:
- Phase 1: line extends from slot anchor to prior/estimated point.
- Phase 2: square dissolves with fragments.
- Exit orders get extra dark sparkle fragments.

Caps and timing:
- Max queue: 180.
- Extend duration: distance-based, clamped 160-760ms.
- Dissolve duration: exit 360ms, entry/other 300ms.

---

## 11. Orphans

Placement:
- Derived from `recovery_orders`.
- Deterministic scatter by hashed key `slot_id:recovery_id`.
- Side-respecting placement (sell above center, buy below).

Color:
- Ranked by distance from market.
- Hue gradient 270 -> 0.
- Saturation base 70% with center-line desaturation factor.
- Lightness base 50% (reduced during backlog effect).

Sprite:
- Pixel-plus motif at 3x scale (`px = 3`), including inner and outer arms.
- Center is black.
- Rendered with `imageSmoothingEnabled = false`.

Twinkle:
- Base 0.5, amplitude 0.4 (0.45 on backlog), clamped to `[0.1, 0.9]`
  in normal operation.
- Halted freezes twinkle factor.

Orphan animations retained:
- Reprice drift (`orphan_repriced`, 900ms).
- Morph from order to orphan (`order_orphaned`).
- Recovery dissolve (`recovery_gone`).

---

## 12. Profit Counter and Flights

Counter:
- Top-right within membrane.
- Seven-segment text `Ð <value>`.
- No background box.
- Lit segments: black.
- Unlit ghost segments: `rgba(0,0,0,0.06)`.

Flights:
- Trigger on `cycle_completed` only.
- 8 particles per flight, 760ms.
- Colors:
  - from recovery: gray
  - positive: dark brown `rgb(43,27,23)`
  - negative: dark red `rgb(139,0,0)`
- Particle size: 3x3.
- Counter value updates on arrival, then reconciles to `total_profit`.

---

## 13. Notification Strip (Canvas)

Bauhaus notification is rendered on canvas (`drawBauhausCanvasNotifStrip`):
- Sits at bottom of membrane region.
- 20px tall strip area.
- Severity dot radius 4:
  - info/idle: dark green
  - warn: dark goldenrod
  - crit: dark red
- Text in 11px monospace.
- Idle label: `Running`.

Note:
- Legacy HTML `notifStrip` still exists for Factory mode and remains updated,
  but is hidden during Bauhaus.

---

## 14. Tooltips and Hit Testing

Bauhaus hover priority:
1. Orders
2. Orphans
3. Slots
4. Profit counter
5. Gullet path (price channel)

Price channel hover uses full path hit-test:
- `ctx.isPointInPath(bauhausGulletPath, px*dpr, py*dpr)`
- fallback to last rectangle when path unavailable

Price channel tooltip includes:
- price
- price age
- DOGE sell/buy exposure
- capacity (`open_orders_current/open_orders_safe_cap`)
- status band (`status_band`)

---

## 15. Diagnosis Overlays

`drawBauhausDiagnosisOverlays` applies:

- Paused: gray overlay `rgba(128,128,128,0.6)` on membrane.
- Brownout: lighter gray overlay `rgba(128,128,128,0.35)`.
- Red wash tint for red-wash states.
- Halted vignette radial darkening.
- Circuit sparks moving along wobbled membrane perimeter
  (top/right/bottom/left traversal).

---

## 16. Diff and Event Integration

Diff events consumed by Bauhaus queues:
- `order_gone` -> fill animations
- `orphan_repriced` -> orphan drift
- `order_orphaned` -> morph animation
- `recovery_gone` -> orphan dissolve
- `cycle_completed` -> profit flights

Enqueue order in refresh pipeline:
- Queues are fed before `statusData = next` so prior snapshot lookups remain
  available.

---

## 17. Known Implementation Notes

- Bauhaus command/help/confirm are HTML overlays with Bauhaus styling, not
  canvas-native widgets.
- Hidden Factory DOM elements are still updated while in Bauhaus (safe and
  intentional to preserve Factory continuity on return).
- Sparkle population updates are time-gated (~4s) rather than hard-coupled to
  poll callback boundaries.

---

## 18. Verification Checklist

- `b` toggles mode and hides/restores shell.
- Gullet is funnel-shaped and clipped sparkles stay inside it.
- Slots are square outlines with slightly opaque yellow interior and faint phase tint.
- Order markers are 7x7; clamped lines are dashed `[3,3]`.
- Orphans render as 3x pixel-plus sprites with ranked color/twinkle.
- Profit counter has no box and uses ghosted seven-segment digits.
- Canvas notification strip shows severity dot + text.
- Gullet hover tooltip includes capacity and band.
- Circuit sparks run along membrane edge.
- Overlay order is: notif -> tooltip -> diagnosis.
