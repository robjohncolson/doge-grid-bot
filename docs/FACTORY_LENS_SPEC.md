# Factory Lens — Visual Factory Simulation

Version: v1.0
Date: 2026-02-12
Status: Implementation-ready
Scope: 2D Canvas factory visualization of bot state machine, served at `/factory`

---

## 1. Goal

Render the bot's state machine as a living Factorio-style factory. Every visual element maps 1:1 to a runtime concept. The factory must accurately represent the state machine at all times — if something breaks, you see it break. If a slot is jammed, the machine stops. If price goes stale, the lights flicker out.

The factory is also bidirectional: deleting a machine removes the slot, adding a machine creates one.

Minimal backend additions: 3 new fields on the existing `/api/status` payload (see section 10.1). No new endpoints, no new telemetry counters. Sends existing `/api/action` commands.

---

## 2. Concept Mapping

| Visual Element | Runtime Concept | Data Source |
|---|---|---|
| **Power line** | Price feed from Kraken | `status.price`, `status.price_age_sec` |
| **Power meter** | API health / capacity utilization | `capacity_fill_health.open_order_utilization_pct` |
| **Input chest (USD)** | Buy-side health (symbolic) | Chest full when `!long_only`, empty outline when `long_only` (can't buy) |
| **Input chest (DOGE)** | Sell-side health (symbolic) | Chest full when `!short_only`, empty outline when `short_only` (can't sell) |
| **Machine** | Slot | `status.slots[n]` |
| **Machine phase indicator** | S0 / S1a / S1b / S2 | `slot.phase` |
| **Entry conveyor** (into machine) | Entry orders pending on Kraken | `slot.open_orders` where `role=entry` |
| **Exit conveyor** (out of machine) | Exit orders pending on Kraken | `slot.open_orders` where `role=exit` |
| **Recycling belt** | Recovery orders (orphaned exits) | `slot.recovery_orders` |
| **Output chest** | Realized profit | `slot.total_profit` |
| **Item on belt** | Individual order | order objects in `open_orders` / `recovery_orders` |
| **Status band indicator** | Capacity health | `capacity_fill_health.status_band` |
| **Circuit network wire** | Guardrails (pause/halt state) | `status.mode` |
| **Logistic bot** (floating repair icon) | Auto-repair in progress | `slot.long_only` or `slot.short_only` (degraded) |

---

## 3. Visual States per Machine

Each machine (slot) renders differently based on its phase and health.

### 3.1 Phase Rendering

| Phase | Machine Visual | Animation |
|-------|---------------|-----------|
| **S0** (entry) | Machine idle, doors open, waiting for input | Slow pulse glow, gears stationary |
| **S1a** (short in position) | Machine active, A-side lit, B-side dim | Gears turning, A-side conveyor moving |
| **S1b** (long in position) | Machine active, B-side lit, A-side dim | Gears turning, B-side conveyor moving |
| **S2** (both exits) | Machine fully active, both sides lit, warning lamp | Fast gears, both conveyors moving |

### 3.2 Health Overlays

| Condition | Visual | Source |
|-----------|--------|--------|
| Normal two-sided S0 | Green status light | `!long_only && !short_only` |
| Degraded one-sided | Yellow warning lamp + `[SO]`/`[LO]` label | `long_only \|\| short_only` |
| S2 timeout approaching | Red blinking lamp (>75% of orphan timeout) | `slot.s2_entered_at` + `status.s2_orphan_after_sec` (see 10.1) |
| Slot producing profit | Green sparkle on output | `recent_cycles` with positive `net_profit` |
| Slot taking losses | Red flash on output | `recent_cycles` with negative `net_profit` |

### 3.3 Power System

| State | Visual | Source |
|-------|--------|--------|
| Fresh price (<10s) | Bright power line, electricity particles flowing | `price_age_sec < 10` |
| Aging price (10-60s) | Dimming power line, fewer particles | `10 < price_age_sec < 60` |
| Stale price (>60s) | Power line dark, red warning, machines dim | `price_age_sec > 60` |
| Bot PAUSED | All machines idle, amber overlay | `mode == "PAUSED"` |
| Bot HALTED | All machines stopped, red overlay, alarm icon | `mode == "HALTED"` |

---

## 4. Layout

Top-down 2D factory floor. Auto-arranged, no manual placement needed.

```
┌─────────────────────────────────────────────────────────┐
│  ⚡ POWER LINE ════════════════════════════════════════  │
│  [PRICE: $0.0912]  [AGE: 2s]  [MODE: RUNNING]          │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─USD─┐    ┌─────┐   ┌─────┐   ┌─────┐    ┌─PROFIT─┐│
│  │ IN  │═►══│ #1  │   │ #2  │   │ #3  │══►═│  OUT   ││
│  │chest│    │ S1a │   │ S0  │   │ S1b │    │ chest  ││
│  └─────┘    └──┬──┘   └──┬──┘   └──┬──┘    └────────┘│
│  ┌DOGE─┐       │         │         │                   │
│  │ IN  │═►══   │         │         │                   │
│  │chest│    ┌──┴─────────┴─────────┴──┐                │
│  └─────┘    │    RECYCLING BELT       │                │
│             │  ○ ○ ○  (recoveries)    │                │
│             └─────────────────────────┘                │
│                                                         │
│  [Capacity: 117/168 (70%)] [Band: NORMAL] [+Add]       │
└─────────────────────────────────────────────────────────┘
```

### 4.1 Auto-Layout Rules

1. Machines arranged in a horizontal row. If >6 machines, wrap to second row.
2. Power line runs across the top, branching down to each machine.
3. Input chests (USD left, DOGE left-below) feed into entry conveyors.
4. Output chest (right) receives completed cycle items.
5. Recycling belt runs along the bottom, collecting recovery orders from all machines.
6. Status bar at bottom shows capacity, band, and controls.
7. Layout reflows on slot add/remove without page reload.

### 4.2 Scaling

- Canvas resizes to fill viewport. Minimum 800x500.
- Machine size and spacing scale proportionally.
- At 20+ machines, machines shrink to icon size with tooltip on hover.

---

## 5. Animation

### 5.1 Continuous Animations

| Element | Animation | Speed |
|---------|-----------|-------|
| Power line particles | Small dots flowing left-to-right | Proportional to price freshness |
| Active conveyor items | Items sliding toward machine (entry) or away (exit) | ~30px/sec |
| Machine gears | Rotating cog icon inside machine box | Phase-dependent (S2 fastest) |
| Output sparkle | Brief green particle burst on cycle completion | On new `recent_cycles` entry |
| Recovery belt items | Slow drift along bottom belt | ~10px/sec |
| Logistic bot | Small icon floating near degraded machine | Gentle bob animation |

### 5.2 Event-Triggered Animations

| Event | Animation |
|-------|-----------|
| New order placed | Item appears on entry conveyor, slides toward machine |
| Entry fill | Item absorbed into machine, machine briefly glows |
| Exit fill | Item emitted from machine onto exit conveyor, slides to output |
| Cycle completed | Item reaches output chest, sparkle + profit number float-up |
| Order orphaned | Item ejected from machine onto recycling belt |
| Slot added | Machine slides in from right with build animation |
| Slot removed | Machine shrinks and fades out |
| Price goes stale | Power line dims, all machines slow down |
| Bot paused | Amber wash over entire factory |
| Bot halted | Red wash, alarm icon pulses |

### 5.3 Animation Source of Truth

Animations are driven by **diffing consecutive `/api/status` polls**. On each poll:

1. Compare `slot.open_orders` arrays — new orders trigger "placed" animation, missing orders trigger "filled" or "canceled."
2. Compare `slot.recovery_orders` — new entries trigger "orphaned" animation.
3. Compare `slot.recent_cycles` — new entries trigger "cycle completed" animation.
4. Compare `slot.phase` — phase change triggers machine state transition.
5. Compare `status.mode` — mode change triggers power/overlay transition.

Track previous poll state client-side. First poll renders static; subsequent polls animate deltas.

---

## 6. Interactivity

### 6.1 Mouse

| Action | Effect |
|--------|--------|
| Click machine | Select slot (highlights, shows detail tooltip) |
| Hover machine | Show tooltip: slot ID, phase, profit, order count |
| Hover belt item | Show tooltip: order side, price, volume, age |
| Click `[+Add]` button | `POST /api/action {action: "add_slot"}` → machine appears |
| Right-click machine | Show "Remove slot not yet available" toast (no API exists) |
| Click recovery item | Confirm dialog → `POST /api/action {action: "soft_close", ...}` |
| Hover input chest | Show symbolic status ("USD side healthy" or "USD side starved — long_only") |
| Hover output chest | Show total realized PnL |

### 6.2 Keyboard

Full parity with the dashboard keyboard spec. Same mode FSM (NORMAL/COMMAND/HELP/CONFIRM), same `preventDefault()` behavior, same command bar with parser/validation/auto-complete/history.

| Key | Action |
|-----|--------|
| `1`-`9` | Select machine by index |
| `[`/`]` | Cycle machine selection |
| `g g` | Jump to first machine (chord, 400ms timeout) |
| `G` | Jump to last machine |
| `+` | Add machine (slot) |
| `-` | Soft close next recovery |
| `p` | Toggle pause/resume (with confirm) |
| `.` | Force refresh (rate-limited: 2s cooldown) |
| `?` | Toggle help overlay |
| `:` | Open command bar (same parser/commands as dashboard) |
| `Esc` | Deselect / close overlay / return to NORMAL |

Command bar, help overlay, confirm dialog, and toast system should be shared JS extracted from the dashboard implementation or duplicated identically.

### 6.3 Bidirectional Requirement

Visual actions MUST map to real API calls. The factory is not decorative — it's a control surface.

- Adding a machine = `add_slot` API call
- Removing a machine = future (not currently in API — show "not yet available" toast)
- Clicking recovery item = `soft_close` API call
- Pause/resume = same as dashboard

---

## 7. Detail Panel

When a machine is selected (clicked or keyboard), a slide-out panel appears on the right showing:

1. Slot ID, phase, mode flags (`[SO]`/`[LO]`)
2. Market price
3. Open orders (same table as main dashboard)
4. Recovery orders with close buttons
5. Recent cycles with profit coloring
6. Cycle counters (A.N / B.N)
7. Slot-level realized + unrealized PnL

Panel closes with `Esc` or clicking elsewhere. Reuses the same data from `/api/status` — no additional API calls.

---

## 8. Technical Approach

### 8.1 Rendering

- **2D Canvas** with `requestAnimationFrame` loop.
- Render at 30fps (sufficient for smooth belt animation, saves CPU).
- Canvas element fills the page. Dark background matching `--bg: #0d1117`.
- All drawing uses canvas 2D context (fillRect, strokeRect, arc, drawImage for icons).
- No external dependencies. No Three.js, no Pixi.js.

### 8.2 Architecture

```
┌──────────────────────────────────────────┐
│  factory_view.py                         │
│  FACTORY_HTML = """..."""                 │
│                                          │
│  <canvas id="factory">                   │
│  <script>                                │
│    1. State poller (GET /api/status)      │
│    2. Diff engine (prev vs current)      │
│    3. Layout engine (slot positions)     │
│    4. Renderer (canvas draw calls)       │
│    5. Animation queue (tweens)           │
│    6. Input handler (mouse + keyboard)   │
│    7. Detail panel (HTML overlay)        │
│    8. Command bar (reused from dash)     │
│  </script>                               │
└──────────────────────────────────────────┘
```

### 8.3 File Organization

- **New file: `factory_view.py`** — contains `FACTORY_HTML` string, same pattern as `dashboard.py`.
- **`bot.py`** — add route: `GET /factory` serves `FACTORY_HTML`. No other backend changes.
- The factory page polls the same `GET /api/status` and `POST /api/action` endpoints.

### 8.4 Drawing Primitives

Keep the visual style simple and readable:

- **Machines**: Rounded rectangles with phase-colored border. Internal gear icon (drawn with arcs).
- **Conveyors**: Horizontal lines with chevron pattern (animated via offset). Items are small colored circles.
- **Power line**: Thick line across top with flowing dot particles.
- **Chests**: Rectangles with fill-level indicator.
- **Recovery belt**: Dashed line along bottom with circulating items.
- **Text**: Canvas `fillText` for labels, slot IDs, prices. Monospace font.
- **Colors**: Reuse dashboard CSS token values (`#2ea043` good, `#f85149` bad, `#d29922` warn, `#58a6ff` accent).

---

## 9. Diagnosis Engine

A pure client-side JS function that reads the `/api/status` payload and outputs an array of active symptoms. The renderer maps symptoms to visual effects on the factory floor — you don't read a symptom card, you *see* the problem.

### 9.1 Engine Contract

```js
// Pure function, no side effects, no network calls.
// Input: status payload from GET /api/status
// Output: array of symptom objects, sorted by priority (1 = highest)
function diagnose(status) -> Symptom[]
```

Runs client-side on every poll. No backend endpoint needed.

### 9.2 Symptom Taxonomy

| ID | Severity | Trigger Rule | Visual Effect |
|---|---|---|---|
| `IDLE_NORMAL` | `info` | All slots S0, no degradation, no recoveries | Machines calm, green lights, peaceful factory |
| `BELT_JAM` | `warn` | Any slot in S2 for >50% of orphan timeout (from `slot.s2_entered_at` + `status.s2_orphan_after_sec`, see 10.1). Falls back to "phase is S2" if fields absent. | Affected machine's output conveyor stops, items pile up, warning lamp blinks |
| `POWER_BROWNOUT` | `crit` | `price_age_sec > 30` or `mode == "PAUSED"` | Power line flickers/dims, all machines slow, amber particles |
| `LANE_STARVATION` | `warn` | Any slot degraded (`long_only` or `short_only`) | Affected machine's starved side goes dark, input chest for that resource flashes empty |
| `RECOVERY_BACKLOG` | `warn` | `total_orphans > slot_count * 2` | Recycling belt overflows, items pile up at edges, belt turns yellow |
| `CIRCUIT_TRIP_RISK` | `crit` | `capacity_fill_health.status_band == "stop"` or `partial_fill_cancel_events_1d > 0` | Circuit network wire sparks red, hazard icon near capacity meter |
| `POWER_BLACKOUT` | `crit` | `price_age_sec > 60` or `mode == "HALTED"` | Power line dead, all machines dark, red overlay, alarm icon |

### 9.3 Symptom Object

```js
{
  symptom_id: "BELT_JAM",
  severity: "warn",         // info | warn | crit
  priority: 2,              // 1 = highest
  summary: "Slot #7 stuck in S2 for 22m (73% of timeout)",
  affected_slots: [7],
  visual_effects: ["conveyor_stop", "warning_lamp"]
}
```

### 9.4 Visual Mapping

The renderer reads active symptoms and applies effects:

| Visual Effect | What it does |
|---|---|
| `conveyor_stop` | Belt animation stops on affected machine, items freeze in place |
| `warning_lamp` | Yellow/red blinking lamp icon above affected machine |
| `power_dim` | Power line particle speed drops, brightness fades |
| `power_dead` | Power line goes black, no particles |
| `input_flash_empty` | Input chest for USD or DOGE blinks empty (outline only) |
| `belt_overflow` | Recovery belt items pile up, belt color shifts to yellow/red |
| `circuit_spark` | Small spark particles along circuit network wire |
| `hazard_icon` | Triangle hazard icon near capacity meter |
| `machine_dark` | One side of machine goes dark (starved lane) |
| `alarm_pulse` | Large alarm icon in center of factory, pulsing red |
| `amber_wash` | Semi-transparent amber overlay on entire factory |
| `red_wash` | Semi-transparent red overlay on entire factory |

### 9.5 Notification Strip

A thin strip at the bottom of the canvas (above the status bar) shows the **top symptom** as text:

```
⚠ BELT_JAM: Slot #7 stuck in S2 for 22m (73% of timeout)
```

- Color-coded: `info` = blue, `warn` = yellow, `crit` = red.
- Shows only the highest-priority symptom. Click/`Enter` to cycle through all active symptoms.
- When `IDLE_NORMAL` is the only symptom, the strip shows `✓ Factory running normally` in green.

### 9.6 Priority Rules

When multiple symptoms are active, priority determines:
1. Which symptom shows in the notification strip
2. Which visual effects take precedence (e.g., `red_wash` overrides `amber_wash`)

Priority order (1 = highest):
1. `POWER_BLACKOUT`
2. `CIRCUIT_TRIP_RISK`
3. `POWER_BROWNOUT`
4. `BELT_JAM`
5. `LANE_STARVATION`
6. `RECOVERY_BACKLOG`
7. `IDLE_NORMAL`

All active symptoms' visual effects are applied simultaneously — they stack, with higher priority winning any conflicts.

---

## 10. Data Requirements

### 10.1 Backend Additions (3 fields)

Add to `status_payload()` in `bot.py`. All three values already exist on runtime objects — this is pure serialization, no new computation.

| Field | Location | Type | Source |
|-------|----------|------|--------|
| `s2_orphan_after_sec` | top-level | `float` | `config.S2_ORPHAN_AFTER_SEC` |
| `s2_entered_at` | per-slot | `float\|null` | `slot_state.s2_entered_at` (already on `PairState` line 109) |
| `stale_price_max_age_sec` | top-level | `float` | `config.STALE_PRICE_MAX_AGE_SEC` |

These are read-only additions. No trading behavior change. Needed for `BELT_JAM` duration calculation and S2 blink threshold.

Also update `STATE_MACHINE.md` section 14 to include `GET /factory` route.

### 10.2 Existing Fields Used

| Visual Need | Source Field |
|-------------|-------------|
| Machine count + layout | `status.slots` array length |
| Machine phase | `slot.phase` |
| Machine health | `slot.long_only`, `slot.short_only` |
| Orders on belts | `slot.open_orders` (side, role, price, volume) |
| Recovery items | `slot.recovery_orders` (side, price, age_sec) |
| Cycle completions | `slot.recent_cycles` (diff to detect new) |
| Power state | `status.price_age_sec`, `status.mode` |
| Capacity | `capacity_fill_health.open_order_utilization_pct` |
| Profit output | `slot.total_profit`, `status.total_profit` |
| Band status | `capacity_fill_health.status_band` |
| Partial fill canary | `capacity_fill_health.partial_fill_cancel_events_1d` |
| Total orphans | `status.total_orphans` |
| Slot count | `status.slot_count` |
| S2 duration | `slot.s2_entered_at` + `status.s2_orphan_after_sec` **(new, 10.1)** |

### 10.3 Graceful Degradation

All new fields (10.1) must be treated as optional by the client. If absent:
- `BELT_JAM` falls back to "phase is S2" without duration percentage.
- S2 blink threshold uses hardcoded 1800s default.
- Stale threshold uses hardcoded 60s default.

---

## 11. Testing Criteria

1. Factory renders all slots from `/api/status` as machines with correct phases.
2. Adding a slot via `+` key or button causes a new machine to appear on next poll.
3. Phase change (S0→S1a) visually transitions the machine (side lights up, gears start).
4. Price going stale (>60s) dims the power line and all machines.
5. Bot PAUSED shows amber overlay. Bot HALTED shows red overlay with alarm.
6. Recovery orders appear as items on the recycling belt.
7. Clicking a recovery item opens confirmation → soft_close API call.
8. Selecting a machine shows the detail panel with correct slot data.
9. Canvas resizes on window resize without breaking layout.
10. Keyboard shortcuts (`1-9`, `[]`, `+`, `-`, `p`, `:`, `?`) work identically to main dashboard.
11. Degraded slot (`long_only`/`short_only`) shows warning lamp + starved side goes dark.
12. S2 slot shows belt jam visual when duration exceeds 50% of timeout.
13. `status_band == "stop"` triggers circuit spark and hazard icon.
14. Notification strip shows highest-priority symptom with correct color.
15. `IDLE_NORMAL` state shows green "Factory running normally" in strip.

---

## 12. Milestones

| Phase | Scope |
|-------|-------|
| F1 | Static factory: machines + power line + chests + status bar + notification strip. No animation. Route `/factory` served. |
| F2 | Diagnosis engine: `diagnose()` function + visual effect mapping + symptom-driven rendering. |
| F3 | Animation: belt items moving, gear rotation, power particles, phase-based machine states. |
| F4 | Interactivity: click to select, detail panel, keyboard nav, command bar, add/remove/soft-close. |
| F5 | Polish: diff-driven event animations (fills, orphans, cycles), sparkles, transitions, tooltips. |

Each milestone is independently shippable. F1 alone is a useful static factory view. F2 makes it diagnostic.

---

## 13. Out of Scope

- 3D rendering (Three.js)
- Drag-and-drop machine placement
- Custom factory layouts / saved arrangements
- Sound effects
- New backend telemetry counters or API endpoints
- Multi-pair factory views
- Prompt launchers for Codex/Claude (may layer on later)
