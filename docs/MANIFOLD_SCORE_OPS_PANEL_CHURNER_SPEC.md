# Manifold Trading Score, Operations Panel & Churner Spawning Spec

**Version**: v0.1
**Date**: 2026-02-17
**Status**: Draft
**Scope**: Five interconnected systems — MTS score, operations panel, statistical visualizations, AI advisor enrichment, churner slot spawning
**Files affected**: `bayesian_engine.py`, `bot.py`, `config.py`, `ai_advisor.py`, `dashboard.py`, `grid_strategy.py`

---

## Context

The bot produces ~20 statistical signals (HMM posteriors across 3 timeframes, entropy, BOCPD, throughput sizer, age pressure, belief engine, action knobs, trade beliefs) but no unified measure of "should I be trading aggressively or conservatively right now?" Each subsystem speaks its own language. The action knobs attempt synthesis but are ad-hoc weighted sums without geometric justification.

Additionally:
- **No runtime feature toggles**: 35+ boolean flags in config.py require restart to change.
- **No statistical visualizations**: Raw numbers in JSON, no visual interpretation.
- **AI advisor is context-starved**: Sees HMM + capital but not age distribution, throughput, beliefs, or the manifold score.
- **Churner slots can't be spawned manually**: The engine exists but has no API/UI, so the "orphan factory" ranging strategy is inaccessible.

---

## 1. Locked Decisions

1. **MTS is a geometric mean**, not arithmetic — multiplicative composition is natural on probability manifolds.
2. **Phase 1 (Fisher Score) ships first** — deterministic formula, no training data needed, immediately useful.
3. **Phase 2 (Kernel Memory) deferred** until ~500+ state snapshots accumulated with outcome labels.
4. **Phase 3 (GP) is aspirational** — noted in spec but not designed in detail.
5. **Feature toggles are runtime-only** — they don't persist to config.py. On restart, config.py defaults resume. This is intentional (safe default).
6. **Ops panel is a slide-down drawer**, not a modal — stays accessible without blocking the view.
7. **SVG only** for visualizations — no canvas, no external chart libs.
8. **Churner spawn is manual** — user picks slot + clicks Spawn. No auto-churner spawning in this spec.
9. **AI context stays under 2000 tokens** — MTS provides a compressed summary, not raw signal dump.
10. **numpy is allowed** — bayesian_engine.py already uses it; MTS lives there too.

---

## 2. Scope

### In

1. **Manifold Trading Score (MTS)** — single ∈ [0,1] score computed every main loop cycle.
2. **Operations Control Panel** — drawer UI with grouped feature toggles, runtime API.
3. **Statistical Visualizations** — MTS sparkline, component bars, ternary simplex plot, regime flow.
4. **AI Advisor Enrichment** — expanded context payload with MTS, age bands, throughput buckets.
5. **Churner Slot Spawning** — API endpoints + dashboard UI for manual churner lifecycle.

### Out

1. GP Profit Surface (Phase 3) — noted, not designed.
2. Kernel Memory implementation (Phase 2) — schema defined, implementation deferred.
3. Changing the action knobs system — MTS runs alongside it, doesn't replace it.
4. Auto-churner spawning — manual only in this spec.
5. Persisting runtime toggle state to config.py or disk.

---

## 3. System A: Manifold Trading Score (MTS)

### 3.1 Concept

The MTS answers: **"How favorable is the current market state for active grid trading?"**

Score ∈ [0, 1]:
- **0.0**: Maximally unfavorable (regime unknown, capital stuck, no throughput)
- **0.5**: Neutral (some uncertainty, moderate throughput)
- **1.0**: Maximally favorable (clear regime, fast fills, low age pressure)

The score drives: entry cadence, spacing multiplier, slot allocation decisions. It does NOT replace the directional system (which answers "which side?") — MTS answers "how much?"

### 3.2 State Vector

Extracted every main loop cycle from existing subsystems (no new data collection):

| # | Signal | Source | Range | Update freq |
|---|--------|--------|-------|-------------|
| 1-3 | posterior_1m [bear, range, bull] | BeliefState | [0,1]³ simplex | every cycle |
| 4-6 | posterior_15m | BeliefState | [0,1]³ simplex | every cycle |
| 7-9 | posterior_1h | BeliefState | [0,1]³ simplex | every cycle |
| 10 | entropy_consensus | BeliefState | [0, 1] | every cycle |
| 11 | p_switch_consensus | BeliefState | [0, 1] | every cycle |
| 12 | bocpd_change_prob | BOCPD | [0, 1] | every cycle |
| 13 | bocpd_run_length | BOCPD | [0, ∞) | every cycle |
| 14 | age_pressure | ThroughputSizer | [0, 1] | every cycle |
| 15 | throughput_multiplier | ThroughputSizer | [0, 2] | every cycle |
| 16 | direction_score | BeliefState | [-1, 1] | every cycle |
| 17 | stuck_capital_pct | SlotVintage | [0, 100] | every cycle |
| 18 | confidence_score | BeliefState | [0, 1] | every cycle |

Total: 18-dimensional state vector.

### 3.3 Component Scores (Phase 1: Fisher Score)

Four components, each ∈ [0, 1], combined via geometric mean.

#### 3.3.1 Regime Clarity (RC)

Measures how concentrated posteriors are vs uniform — KL divergence on the probability simplex.

```
KL(p || uniform) = Σ p_i × ln(p_i / (1/3))
```

For a 3-state simplex, KL ranges from 0 (uniform) to ln(3) ≈ 1.099 (degenerate).

Per-timeframe clarity:
```
clarity_tf = 1 - exp(-KL(posterior_tf || uniform))
```

Weighted average across timeframes (matching consensus weights):
```
RC = 0.2 × clarity_1m + 0.5 × clarity_15m + 0.3 × clarity_1h
```

**Interpretation**: RC near 0 = the HMMs can't distinguish regimes = fly blind. RC near 1 = posteriors are sharp = regime is identifiable.

**Current snapshot**: clarity_1m ≈ 0.33 (near-uniform), clarity_15m ≈ 0.67 (strong bullish), clarity_1h ≈ 0.45 (moderate ranging). RC ≈ 0.50.

#### 3.3.2 Regime Stability (RS)

Measures likelihood the current regime persists. Uses p_switch (transition probability) and BOCPD change probability.

```
switch_risk = max(p_switch_1m × 0.2, p_switch_15m × 0.5, p_switch_1h × 0.3)
bocpd_risk = bocpd_change_prob
RS = (1 - switch_risk) × (1 - bocpd_risk)
```

The `max()` ensures any one timeframe flashing "imminent switch" drags stability down.

**Current snapshot**: switch_risk ≈ max(0.105, 0.036, 0.015) = 0.105, bocpd_risk = 0.006. RS ≈ 0.89.

#### 3.3.3 Throughput Efficiency (TE)

Measures how well capital is converting to profit in the current regime.

```
tp_mult = throughput_multiplier for active regime (from sizer)
age_drag = 1 - (age_pressure × stuck_capital_pct / 100)
TE = clamp(tp_mult × age_drag, 0, 1)
```

When throughput multiplier is high AND age pressure is low, TE is high. When ancient exits dominate and the sizer is throttled, TE collapses.

**Current snapshot**: tp_mult ≈ 0.67 (ranging_A), age_drag ≈ 1 - (0.425 × 0.124) = 0.947. TE ≈ 0.63.

#### 3.3.4 Signal Coherence (SC)

Measures agreement between subsystems — do the HMM, BOCPD, and directional signals tell a consistent story?

```
timeframe_agreement = 1 - entropy_consensus  # 0 = total conflict, 1 = agreement
directional_clarity = abs(direction_score)    # 0 = no direction, 1 = clear trend
bocpd_stability = min(bocpd_run_length / 50, 1.0)  # Normalized run length

SC = (timeframe_agreement × 0.5) + (directional_clarity × 0.25) + (bocpd_stability × 0.25)
```

**Current snapshot**: agreement ≈ 0.12, directional_clarity ≈ 0.38, bocpd_stability ≈ 1.0. SC ≈ 0.41.

### 3.4 Final Score

```
MTS = (RC × RS × TE × SC) ^ (1/4)
```

Geometric mean ensures any single collapsed component drags the whole score down — you can't trade aggressively with high throughput if you have zero regime clarity.

**Current snapshot**: MTS = (0.50 × 0.89 × 0.63 × 0.41)^(1/4) ≈ **0.58**

Interpretation: "Cautious — regime unclear, signals conflicting, but capital is working and no imminent changepoint."

### 3.5 Score Bands

| MTS Range | Label | Color | Strategy Implication |
|-----------|-------|-------|---------------------|
| 0.80 - 1.00 | Optimal | Green | Full cadence, tight spacing, deploy all slots |
| 0.60 - 0.79 | Favorable | Teal | Normal cadence, standard spacing |
| 0.40 - 0.59 | Cautious | Amber | Reduced cadence, wider spacing, defer new entries |
| 0.20 - 0.39 | Defensive | Orange | Minimum cadence, wide spacing, consider pausing adds |
| 0.00 - 0.19 | Hostile | Red | Hold only, no new entries, consider soft-close |

### 3.6 MTS History

Rolling deque of (timestamp, mts, rc, rs, te, sc) tuples. Max 360 entries (6 hours at 60s cycles). Used for:
- Dashboard sparkline
- AI advisor context (trend + current)
- Detecting MTS momentum (rising vs falling)

### 3.7 Strategy Mapping

MTS feeds into existing systems as a multiplier, NOT a replacement:

| System | How MTS Integrates |
|--------|-------------------|
| Entry scheduler `cap_per_loop` | `floor(base_cap × MTS)` — throttle entries when MTS low |
| Action knobs `cadence_mult` | `cadence × MTS` — secondary damping |
| Throughput sizer | MTS included in status payload for operator awareness |
| AI advisor | MTS + components in context — AI can recommend overrides |
| Churner spawn gate | Churner only spawns if MTS > 0.3 (not hostile) |

### 3.8 Phase 2: Kernel Memory Score (Schema Only)

Deferred implementation. Design for future:

**State bank**: List of (state_vector_18d, outcome_label, profit_per_sec) tuples.
- Outcome labels: "fast_fill" (filled <1h, profit>0), "slow_fill" (filled 1-24h), "stuck" (>24h), "loss" (realized loss).
- Populated from completed cycles — each round trip completion logs the state_vector at entry time + the outcome.

**MMD computation**:
```
RBF kernel: K(x, y) = exp(-||x - y||² / 2σ²)
σ = median pairwise distance (adaptive bandwidth)
score = mean(K(current, good)) - mean(K(current, bad))
```

**Blending**: When kernel memory has ≥ 200 samples, blend with Fisher:
```
MTS = (1 - α) × fisher_score + α × kernel_score
α starts at 0, ramps to 0.5 as sample count grows
```

### 3.9 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `MTS_ENABLED` | `True` | bool | Master switch |
| `MTS_CLARITY_WEIGHTS` | `[0.2, 0.5, 0.3]` | list | 1m/15m/1h weights for RC |
| `MTS_STABILITY_SWITCH_WEIGHTS` | `[0.2, 0.5, 0.3]` | list | p_switch weighting per TF |
| `MTS_COHERENCE_WEIGHTS` | `[0.5, 0.25, 0.25]` | list | agreement/direction/bocpd for SC |
| `MTS_HISTORY_SIZE` | `360` | int | Rolling history entries |
| `MTS_ENTRY_THROTTLE_ENABLED` | `False` | bool | Use MTS to throttle entries |
| `MTS_ENTRY_THROTTLE_FLOOR` | `0.3` | float | Below this MTS, no new entries |
| `MTS_KERNEL_ENABLED` | `False` | bool | Phase 2 kernel blending |
| `MTS_KERNEL_MIN_SAMPLES` | `200` | int | Min samples before kernel activates |
| `MTS_KERNEL_ALPHA_MAX` | `0.5` | float | Max blend weight for kernel |

---

## 4. System B: Operations Control Panel

### 4.1 Concept

A slide-down drawer (triggered by a gear icon or "Ops" button in the dashboard header) that exposes all boolean feature flags as toggle switches, organized by category. Changes take effect immediately (next main loop cycle) without restart.

### 4.2 Architecture

**Runtime override dict** in BotRuntime:
```python
self._runtime_overrides: dict[str, bool] = {}
```

**Resolution order** (checked every cycle):
1. `_runtime_overrides[key]` if present → use override
2. `config.py` value → use default

**API**:
- `GET /api/ops/toggles` → returns all toggleable flags with current effective value + override status
- `POST /api/ops/toggle` → body: `{"key": "HMM_TERTIARY_ENABLED", "value": true}` → sets runtime override
- `POST /api/ops/reset` → body: `{"key": "HMM_TERTIARY_ENABLED"}` → clears override (reverts to config.py)
- `POST /api/ops/reset-all` → clears all overrides

**Safety**: Overrides are volatile (lost on restart). This is a feature — prevents "I toggled something weird and now it won't start."

### 4.3 Toggle Groups

Organized in the drawer UI as collapsible sections:

**Group 1: Regime Detection**
| Toggle | Config Key | Description |
|--------|-----------|-------------|
| HMM (1m) | `HMM_ENABLED` | Primary 1-minute regime detector |
| HMM Multi-TF | `HMM_MULTI_TIMEFRAME_ENABLED` | 1m+15m consensus |
| HMM 15m OHLCV | `HMM_SECONDARY_OHLCV_ENABLED` | 15m candle collection |
| HMM 1h Strategic | `HMM_TERTIARY_ENABLED` | 1h macro transitions |
| HMM Decay | `HMM_DEEP_DECAY_ENABLED` | Recency decay on training window |

**Group 2: Intelligence**
| Toggle | Config Key | Description |
|--------|-----------|-------------|
| AI Advisor | `AI_REGIME_ADVISOR_ENABLED` | LLM regime second opinion |
| AI Auto-Execute | `AI_AUTO_EXECUTE` | Auto-apply conservative AI actions |
| Belief Tracker | `BELIEF_TRACKER_ENABLED` | Per-trade belief states |
| Belief Widen | `BELIEF_WIDEN_ENABLED` | Allow belief-driven exit widening |
| BOCPD | `BOCPD_ENABLED` | Bayesian changepoint detection |
| Enriched Features | `ENRICHED_FEATURES_ENABLED` | Microstructure features |
| Survival Model | `SURVIVAL_MODEL_ENABLED` | Kaplan-Meier / Cox fill model |
| Action Knobs | `KNOB_MODE_ENABLED` | Continuous action knob mode |

**Group 3: Capital & Sizing**
| Toggle | Config Key | Description |
|--------|-----------|-------------|
| Throughput Sizer | `TP_ENABLED` | Fill-time advisory sizing |
| Directional Regime | `REGIME_DIRECTIONAL_ENABLED` | Directional actuation |
| Directional Shadow | `REGIME_SHADOW_ENABLED` | Shadow-only directional eval |
| Dust Sweep | `DUST_SWEEP_ENABLED` | Fold idle USD into B-side |
| Rebalancer | `REBALANCE_ENABLED` | Inventory size-skew governor |
| Entry Backoff | `ENTRY_BACKOFF_ENABLED` | Widen entry after losses |
| Vol Auto-Profit | `VOLATILITY_AUTO_PROFIT` | Auto-adjust profit targets |

**Group 4: Position Management**
| Toggle | Config Key | Description |
|--------|-----------|-------------|
| Sticky Mode | `STICKY_MODE_ENABLED` | Keep exits waiting indefinitely |
| Recovery Orders | `RECOVERY_ORDERS_ENABLED` | Recovery order creation |
| Subsidy Repricing | `SUBSIDY_ENABLED` | Subsidy-funded exit repricing |
| Churner | `CHURNER_ENABLED` | Regime-gated helper cycles |
| Position Ledger | `POSITION_LEDGER_ENABLED` | Local-first position tracking |
| Auto-Release | `RELEASE_AUTO_ENABLED` | Auto-release eligible exits |

**Group 5: Strategic**
| Toggle | Config Key | Description |
|--------|-----------|-------------|
| Accumulation | `ACCUM_ENABLED` | DCA accumulation engine |
| Manifold Score | `MTS_ENABLED` | Manifold trading score |
| MTS Entry Throttle | `MTS_ENTRY_THROTTLE_ENABLED` | Use MTS to gate entries |
| Kernel Memory | `MTS_KERNEL_ENABLED` | Phase 2 kernel blending |

### 4.4 UI Design

**Trigger**: Gear icon in dashboard header bar, right-aligned.

**Drawer**: Slides down from top, ~400px max height, scrollable. Dark background matching dashboard theme.

**Each toggle**: CSS toggle switch (no checkbox). Label left, switch right. Override indicator: small dot next to label when override active (different from config default). Amber dot = overridden to ON, gray dot = overridden to OFF.

**Group headers**: Clickable to collapse/expand. Show count of enabled/total in that group.

**Footer**: "Reset All to Config Defaults" button. Red, with confirmation.

**Dependency hints**: When toggling a parent OFF that has children (e.g., turning off HMM_ENABLED should warn that Multi-TF depends on it), show a brief warning toast. Don't prevent the action.

### 4.5 Status Payload Addition

```json
"ops_panel": {
  "overrides_active": 3,
  "overrides": {
    "CHURNER_ENABLED": {"effective": true, "config_default": false, "source": "runtime_override"},
    "HMM_TERTIARY_ENABLED": {"effective": true, "config_default": false, "source": "runtime_override"}
  }
}
```

---

## 5. System C: Statistical Visualizations

### 5.1 MTS Score Panel

**Location**: New card in left column summary panel, between "Action Knobs" and "AI Regime Advisor".

**Contents**:
- **Score display**: Large number (e.g., "0.58") with band label ("Cautious") and color background.
- **Sparkline**: SVG polyline showing MTS over last 6 hours (from MTS history deque). Same pattern as equity sparkline.
- **Component bars**: Four horizontal bars (RC, RS, TE, SC) each showing their [0,1] value. Color-coded: green >0.7, amber 0.4-0.7, red <0.4. Labels: "Clarity", "Stability", "Throughput", "Coherence".
- **Interpretation line**: One-sentence auto-generated explanation. Template:
  - "Regime is {clarity_adj}, {stability_adj}, throughput is {te_adj}, signals are {sc_adj}."
  - Where adj from {"sharp"/"murky"/"blind"} x {"stable"/"flickering"/"volatile"} x {"healthy"/"sluggish"/"stalled"} x {"aligned"/"mixed"/"conflicting"}

### 5.2 Ternary Simplex Plot

**Location**: Inside the HMM Regime card (expandable section).

**What it shows**: A triangle (equilateral) where each vertex represents Bear/Range/Bull. Three dots plotted:
- 1m posterior (small, labeled "1m")
- 15m posterior (medium, labeled "15m")
- 1h posterior (large, labeled "1h")

Each dot positioned via barycentric coordinates:
```
x = 0.5 * (2*bull + range) / (bear + range + bull)
y = (sqrt(3)/2) * range / (bear + range + bull)
```

**SVG implementation**: ~60 lines. Triangle outline with vertex labels. Three colored circles. Optional: faint trail showing last 10 positions of consensus posterior (shows regime drift).

**Size**: 160x140px. Inline SVG.

### 5.3 Regime Flow Ribbon

**Location**: Below the MTS sparkline (inside MTS panel, collapsible).

**What it shows**: Horizontal ribbon (6 hours wide) divided into colored segments:
- Green = BULLISH consensus
- Gray = RANGING
- Red = BEARISH
- Height: 20px

Each segment corresponds to one regime_history_30m entry. Drawn as SVG rectangles. Shows regime persistence at a glance.

### 5.4 Component Decomposition Tooltip

**On hover** over the MTS score number, show a tooltip/popover with:
```
RC = 0.50  [1m: 0.33, 15m: 0.67, 1h: 0.45]
RS = 0.89  [p_sw: 0.105, bocpd: 0.006]
TE = 0.63  [tp: 0.67, age: 0.947]
SC = 0.41  [agree: 0.12, dir: 0.38, bocpd_rl: 1.00]
MTS = (0.50 * 0.89 * 0.63 * 0.41)^(1/4) = 0.58
```

This makes the score fully transparent and debuggable.

### 5.5 Age Heatmap Bar

**Location**: Inside self-healing card.

**What it shows**: Single horizontal stacked bar showing position age distribution:
- Fresh: bright green
- Aging: green
- Stale: amber
- Stuck: orange
- Write-off: red

Width proportional to count. Labels show count inside each segment if wide enough.

Already partially in status payload (`age_heatmap`). This just visualizes it.

---

## 6. System D: AI Advisor Context Enrichment

### 6.1 Extended Context

Add to `_build_regime_context()` output:

```python
"manifold": {
    "mts": 0.58,
    "band": "cautious",
    "components": {"clarity": 0.50, "stability": 0.89, "throughput": 0.63, "coherence": 0.41},
    "trend": "falling",        # MTS rising/falling/stable over last 30min
    "mts_30m_ago": 0.64,       # For context on trajectory
},
"positions": {
    "total_open": 42,
    "age_bands": {"fresh": 2, "aging": 2, "stale": 24, "stuck": 5, "write_off": 9},
    "stuck_capital_pct": 0.12,
    "avg_distance_pct": 5.8,   # Average exit distance from market
    "negative_ev_count": 29,   # Positions with negative expected value
},
"throughput": {
    "active_regime": "ranging",
    "multiplier": 0.67,
    "age_pressure": 0.43,
    "median_fill_sec": 7377,
    "sufficient_data_regimes": ["bearish_A", "ranging_A", "ranging_B"],
},
"churner": {
    "enabled": false,
    "active_slots": 0,
    "reserve_usd": 5.0,
    "subsidy_balance": 0.0,
    "subsidy_needed": 0.46,
}
```

### 6.2 Updated System Prompt Addition

Append to `_REGIME_SYSTEM_PROMPT`:

```
You also receive a Manifold Trading Score (MTS) — a geometric composite of regime
clarity, stability, throughput efficiency, and signal coherence. MTS 0.0-0.19 is
hostile, 0.20-0.39 defensive, 0.40-0.59 cautious, 0.60-0.79 favorable, 0.80-1.00
optimal. Factor MTS into your conviction — low MTS means high uncertainty,
recommend conservative postures even if one timeframe shows strong signal.

Position age distribution shows how many exits are fresh vs stuck. High write-off
counts suggest a regime shift has stranded positions. Throughput data shows which
regime/side combinations are actually profitable. Use this to validate or challenge
the HMM signal — if HMM says bullish but throughput shows bearish_A is the only
profitable bucket, something is off.
```

### 6.3 Token Budget

Current context: ~800 tokens. New additions: ~400 tokens. Total: ~1200 tokens. Well within 2000 token budget.

### 6.4 Feedback Signal

When AI advisor makes a recommendation, log the MTS at that moment. Over time this builds a dataset of (MTS_at_recommendation, ai_recommendation, outcome) that can validate whether the AI performs better at certain MTS ranges.

---

## 7. System E: Churner Slot Spawning

### 7.1 Concept

"Churner slots" are the old-school strategy: tight entries, quick exits, accept orphans as cost of doing business. In ranging markets they kept capital cycling — lots of small wins, some stranded exits left as lottery tickets. The modern bot evolved away from this but the user wants to be able to manually engage this mode on specific slots.

### 7.2 What Exists vs What's Missing

**Exists** (in bot.py):
- `ChurnerRuntimeState` dataclass with full lifecycle
- `_run_churner_engine()` main loop
- `_churner_candidate_parent_position()` — picks eligible position
- `_churner_on_entry_fill()`, `_churner_on_exit_fill()` — state transitions
- `_churner_gate_check()` — capacity/reserve validation
- `_churner_timeout_tick()` — order timeouts
- Reserve budget tracking, daily counters, profit routing

**Missing**:
- API endpoints for manual spawn/kill
- Dashboard UI for churner management
- Per-slot churner visibility (which slot is churning what)
- Ability to select target position for churning
- Per-churner lifecycle event log

### 7.3 New API Endpoints

| Method | Path | Body | Action |
|--------|------|------|--------|
| `GET` | `/api/churner/status` | — | Full churner state: active slots, reserve, daily stats |
| `GET` | `/api/churner/candidates` | — | List positions eligible for churning (from position_ledger) |
| `POST` | `/api/churner/spawn` | `{"slot_id": 5}` | Activate churner on slot. Uses auto-selected parent position. |
| `POST` | `/api/churner/spawn` | `{"slot_id": 5, "position_id": 9}` | Activate churner on slot targeting specific parent position. |
| `POST` | `/api/churner/kill` | `{"slot_id": 5}` | Deactivate churner on slot. Cancels any open churner order. |
| `POST` | `/api/churner/config` | `{"reserve_usd": 10.0}` | Adjust runtime churner reserve (volatile). |

### 7.4 Spawn Lifecycle

1. **User clicks "Spawn Churner"** on a slot in the dashboard.
2. API validates: slot exists, no active churner on that slot, CHURNER_ENABLED is true (or runtime-overridden to true), headroom check passes.
3. BotRuntime sets `_churner_by_slot[slot_id].active = True`.
4. Next main loop cycle, `_run_churner_engine()` picks up the slot and begins its existing lifecycle: find candidate parent → place entry order → wait for fill → place exit → wait for fill → route profit → repeat.
5. **User clicks "Kill Churner"** → sets `active = False`, cancels any open order, resets to idle.
6. Churner on that slot remains dormant until user spawns again.

### 7.5 Dashboard UI

**Location**: Inside the detail view for each slot, below the existing controls.

**Churner control bar** (visible only when CHURNER_ENABLED or runtime-overridden):
- **State indicator**: Pill showing "IDLE" / "ENTRY OPEN" / "EXIT OPEN" with color
- **Spawn/Kill button**: Green "Spawn Churner" when idle, Red "Kill" when active
- **Stats line**: "Today: 3 cycles, $0.02 profit | Lifetime: 47 cycles, $0.31"
- **Reserve**: "Reserve: $5.00 available"
- **Active churner info** (when running): "Churning position #9 (slot 5, B2) | Entry at $0.1007 | Age: 45s"

**Churner summary** (in left column summary cards):
- Aggregate: "Churners: 2 active / 31 slots | Today: $0.04 | Reserve: $4.92"
- Clickable to expand list of active churners with per-slot detail

### 7.6 Churner <-> MTS Integration

- Churner spawn is gated: MTS must be > `MTS_CHURNER_GATE` (default 0.3) to spawn.
- Dashboard shows MTS next to spawn button with color — if MTS is below gate, button is grayed out with tooltip "MTS too low (0.22 < 0.30)".
- Churner profit routes to subsidy pool, which enables self-healing repricing of stuck positions. This creates a virtuous cycle: churner generates subsidy → subsidy reprices write-offs → freed capital enables more entries → MTS improves.

### 7.7 Churner Config Additions

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `MTS_CHURNER_GATE` | `0.3` | float | Min MTS for churner spawn |
| `CHURNER_MAX_ACTIVE` | `5` | int | Max simultaneous churner slots |
| `CHURNER_PROFIT_ROUTING` | `"subsidy"` | str | Where profit goes: "subsidy" or "slot" |

---

## 8. Implementation Order

| Phase | System | Effort | Dependencies |
|-------|--------|--------|--------------|
| **Phase 1** | MTS Fisher Score | bayesian_engine.py + bot.py | None — uses existing signals |
| **Phase 2** | Ops Panel API | bot.py (runtime_overrides + 4 endpoints) | None |
| **Phase 3** | Ops Panel UI | dashboard.py (drawer + toggle switches) | Phase 2 API |
| **Phase 4** | MTS Dashboard Card | dashboard.py (score, sparkline, bars) | Phase 1 MTS |
| **Phase 5** | Ternary Simplex + Regime Ribbon | dashboard.py (SVG components) | Phase 1 |
| **Phase 6** | AI Context Enrichment | ai_advisor.py (extended context + prompt) | Phase 1 MTS |
| **Phase 7** | Churner API | bot.py (spawn/kill/status endpoints) | Phase 2 Ops |
| **Phase 8** | Churner Dashboard UI | dashboard.py (controls + status) | Phase 7 |
| **Phase 9** | Age Heatmap Bar | dashboard.py (stacked SVG bar) | None |

Each phase is independently deployable. Phase 1-3 are the highest priority.

---

## 9. Status Payload Addition

New top-level key in `status_payload()`:

```json
"manifold_score": {
    "enabled": true,
    "mts": 0.58,
    "band": "cautious",
    "band_color": "#f0ad4e",
    "components": {
        "regime_clarity": 0.50,
        "regime_stability": 0.89,
        "throughput_efficiency": 0.63,
        "signal_coherence": 0.41
    },
    "component_details": {
        "clarity_1m": 0.33,
        "clarity_15m": 0.67,
        "clarity_1h": 0.45,
        "p_switch_risk": 0.105,
        "bocpd_risk": 0.006,
        "tp_mult": 0.67,
        "age_drag": 0.947,
        "agreement": 0.12,
        "directional_clarity": 0.38,
        "bocpd_run_norm": 1.0
    },
    "history_sparkline": [0.64, 0.62, 0.60, 0.58],
    "trend": "falling",
    "mts_30m_ago": 0.64,
    "kernel_memory": {
        "enabled": false,
        "samples": 0,
        "score": null,
        "blend_alpha": 0.0
    }
}
```

---

## 10. Verification

1. **MTS correctness**: Add unit tests in `test_bayesian_engine.py` with known posteriors → verify component scores and final MTS match hand calculations.
2. **Ops panel round-trip**: Toggle a flag via API → verify status payload reflects override → restart bot → verify override cleared.
3. **SVG rendering**: Open dashboard → verify simplex plot positions match posterior values → verify sparkline updates each cycle.
4. **AI context**: Enable AI advisor → check logs for expanded context → verify token count < 2000.
5. **Churner spawn**: Enable churner via ops panel → click Spawn on a slot → verify entry order placed → verify profit routes to subsidy after fill.
6. **MTS → strategy**: Set `MTS_ENTRY_THROTTLE_ENABLED=true` → observe that entries are deferred when MTS < floor.
