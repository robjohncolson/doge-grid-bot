# Grid Bot v2 — Clean-Slate Distribution Platform

**Version**: 0.1.0
**Status**: Draft
**Date**: 2026-02-18
**Scope**: Rewrite v1 (51K lines, DOGE-specific, personal data) into a distributable, pair-agnostic grid trading platform.

---

## 0. Executive Summary

v1 is a personal DOGE/USD bot that grew organically from 1 file to 24 files over 255 commits. It works, but it's not distributable: hardcoded DOGE assumptions, personal API keys embedded in examples, 7 deprecated subsystems still in the codebase, two parallel state machines, and an 812KB `bot.py` monolith.

v2 strips it to the structural essence — the parts that are universally useful for *any* grid trader on *any* Kraken pair — and ships it as a clean platform with two slot modes, a strong ledger, factory visualization, and an intelligence layer that governs slot allocation.

### What Ships

| Component | Description |
|-----------|-------------|
| **Slot engine** | Pure state machine with per-slot sticky/cycle toggle |
| **Strong ledger** | Double-entry accounting with audit trail |
| **Factory view** | Canvas visualization + Bauhaus overlay |
| **Slot governor** | Intelligence layer: sticky/cycle ratio + auto-close |
| **Kraken adapter** | Generic pair support, capacity telemetry |
| **Dashboard** | Operational control surface (stripped of DOGE branding) |

### What Dies

| v1 Component | Reason |
|--------------|--------|
| `grid_strategy.py` (5,980 lines) | Legacy state machine — replaced by `state_machine.py` |
| Rangers (bot.py:7600-7750) | Replaced by cycle slots |
| Churners (bot.py:7685-8880) | Replaced by cycle slots |
| Herd mode (bot.py:~6973) | Replaced by per-slot sticky toggle |
| HMM regime detection | Optional plugin, not core |
| Bayesian engine | Optional plugin, not core |
| BOCPD | Optional plugin, not core |
| AI regime advisor | Optional plugin, not core |
| DCA accumulation | Optional plugin, not core |
| Survival model | Optional plugin, not core |
| Stats engine | Optional plugin, not core |
| Signal digest | Optional plugin, not core |
| Throughput sizer | Optional plugin, not core (simple sizing ships by default) |
| Multi-pair swarm | Deferred to v2.1 |
| Pair scanner | Deferred to v2.1 |
| `pair_model.py` (1,943 lines) | Scenario simulator — ship as optional dev tool |
| `state_machine_visual.py` (838 lines) | ASCII visualizer — ship as optional dev tool |

---

## 1. Architecture

### 1.1 Core Philosophy

```
"112 sticky slots should just work."
```

The Kraken Pro tier allows 225 open orders per pair. At 75% safety ratio, that's 168 effective orders. Each sticky slot in S1 holds exactly 2 orders (1 entry + 1 exit). So 168 / 2 = 84 max simultaneous S1 slots. But sticky slots in S0 hold 2 entries, and some will be in S0 at any time. With good churn, 112 sticky-only slots are feasible if the work bank math is right.

Add cycle slots and the math changes: each cycle slot produces orphans (recovery orders that linger on the book), consuming headroom. The governor's job is to maintain the right ratio.

### 1.2 Module Map

```
v2/
├── state_machine.py      # Pure reducer: (state, event) → (state, actions)
├── ledger.py             # Double-entry accounting + audit trail
├── slot_engine.py        # SlotRuntime, per-slot sticky/cycle, work bank
├── governor.py           # Intelligence: ratio control, auto-close
├── capacity.py           # Order headroom tracking, bands
├── kraken_adapter.py     # Kraken REST + order management
├── factory_view.py       # Factory canvas + Bauhaus overlay
├── dashboard.py          # Operational dashboard (HTML embedded)
├── server.py             # HTTP server, API routes, SSE
├── config.py             # All configuration, env-driven, pair-agnostic
├── main.py               # Entry point, main loop orchestration
└── tools/
    ├── pair_model.py     # Optional: scenario simulator
    └── state_viz.py      # Optional: ASCII state visualizer
```

**Target**: ~8,000-12,000 lines total (vs 51K in v1). Zero external dependencies.

### 1.3 Data Flow

```
                    ┌──────────────┐
                    │   Kraken     │
                    │   REST API   │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   Adapter    │  price, fills, order status
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
       ┌──────▼──┐  ┌──────▼──┐  ┌─────▼──────┐
       │Capacity │  │Governor │  │  Main Loop  │
       │Tracker  │  │(ratio + │  │ (30s cycle) │
       │         │  │ auto-   │  │             │
       │headroom │  │ close)  │  │             │
       └────┬────┘  └────┬────┘  └──────┬──────┘
            │            │              │
            └────────────┼──────────────┘
                         │
                  ┌──────▼──────┐
                  │ Slot Engine │  N slots, each with:
                  │             │  - PairState (state_machine.py)
                  │             │  - sticky: bool
                  │             │  - ledger position
                  └──────┬──────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
        ┌─────▼──┐ ┌────▼────┐ ┌──▼───────┐
        │Ledger  │ │Factory  │ │Dashboard │
        │(audit) │ │View     │ │(control) │
        └────────┘ └─────────┘ └──────────┘
```

---

## 2. State Machine (Unchanged Foundation)

v1's `state_machine.py` (1,251 lines) is already clean, pure, and tested. It is the canonical v2 foundation.

### 2.1 What Stays Exactly As-Is

- `PairState`, `OrderState`, `RecoveryOrder`, `CycleRecord` — frozen dataclasses
- `EngineConfig` — all fields, including `sticky_mode_enabled`
- `transition(state, event, cfg, order_size_usd)` — pure reducer
- `derive_phase()` — S0/S1a/S1b/S2 from order set
- `bootstrap_orders()` — initial S0 setup
- `check_invariants()` — 12 invariant checks
- `to_dict()` / `from_dict()` — serialization
- Events: `PriceTick`, `TimerTick`, `FillEvent`, `RecoveryFillEvent`, `RecoveryCancelEvent`
- Actions: `PlaceOrderAction`, `CancelOrderAction`, `OrphanOrderAction`, `BookCycleAction`
- All helper functions: `_exit_price`, `_new_entry_order`, `_orphan_exit`, `_refresh_stale_entries`, `_book_cycle`, `_update_loss_counters`, `entry_backoff_multiplier`, etc.

### 2.2 Sticky vs Cycle — How It Works in the State Machine

The state machine already handles this via `EngineConfig.sticky_mode_enabled`:

```python
# state_machine.py:1026-1031
if cfg.sticky_mode_enabled:
    # Sticky mode: exits wait patiently. Timer ticks do NOT orphan.
    if st.s2_entered_at is not None:
        st = replace(st, s2_entered_at=None)
    return st, actions
```

When `sticky_mode_enabled=True`:
- S1 exits wait indefinitely (no `s1_orphan_after_sec` timeout)
- S2 is never entered from timer ticks
- No recovery orders are ever created by the state machine

When `sticky_mode_enabled=False` (cycle mode):
- S1 exits orphan after `s1_orphan_after_sec` (~22.5 min default)
- S2 triggers after both entries fill, and orphans the worse leg after `s2_orphan_after_sec` (~30 min default)
- Recovery orders accumulate (capped by `max_recovery_slots`)

**v2 change**: The slot engine sets `sticky_mode_enabled` *per slot* based on the slot's `sticky` flag. No state machine changes needed.

### 2.3 Order Budget Per Slot Type

| Slot Type | S0 Orders | S1 Orders | S2 Orders | Recovery | Max Total |
|-----------|-----------|-----------|-----------|----------|-----------|
| Sticky    | 2 (entries) | 2 (1 exit + 1 entry) | 2 (exits) | 0 | 2 |
| Cycle     | 2 (entries) | 2 (1 exit + 1 entry) | 2 (exits) | 0-2 | 4 |

Sticky slots never produce recovery orders → predictable budget.
Cycle slots can produce up to `max_recovery_slots` (default 2) recovery orders per slot → variable budget.

---

## 3. Strong Ledger

### 3.1 Why

v1 tracks profit as a single float (`total_profit`) that gets incremented on cycle completion. There's no audit trail, no reconciliation against Kraken, no way to answer "where did $0.37 go?" The position ledger (`position_ledger.py`, 576 lines) adds tracking but is entangled with the legacy `grid_strategy.py` path.

v2 ships a double-entry ledger that records every capital movement with enough data to reconstruct the entire trading history.

### 3.2 Ledger Entries

Every capital movement is a `LedgerEntry`:

```python
@dataclass(frozen=True)
class LedgerEntry:
    id: int                     # Monotonic sequence
    timestamp: float            # Unix epoch
    slot_id: int                # Which slot
    trade_id: str               # "A" or "B"
    cycle: int                  # Which cycle
    entry_type: str             # See taxonomy below
    debit_usd: float            # USD out (positive)
    credit_usd: float           # USD in (positive)
    debit_base: float           # Base asset out (e.g., DOGE)
    credit_base: float          # Base asset in
    price: float                # Execution price
    fee_usd: float              # Fee in USD-equivalent
    fee_base: float             # Fee in base asset
    txid: str                   # Kraken transaction ID
    order_local_id: int         # State machine local order ID
    balance_usd_after: float    # Running USD balance
    balance_base_after: float   # Running base balance
    note: str                   # Human-readable description
```

### 3.3 Entry Type Taxonomy

| Type | Trigger | USD Flow | Base Flow |
|------|---------|----------|-----------|
| `BUY_ENTRY` | B-side entry fill | Debit (spent USD) | Credit (received base) |
| `SELL_ENTRY` | A-side entry fill | Credit (received USD) | Debit (spent base) |
| `BUY_EXIT` | A-side exit fill (buy back) | Debit (spent USD) | Credit (received base) |
| `SELL_EXIT` | B-side exit fill (sell back) | Credit (received USD) | Debit (spent base) |
| `RECOVERY_FILL` | Recovery order fills | Depends on side | Depends on side |
| `RECOVERY_CANCEL` | Recovery order cancelled/expired | 0 | 0 |
| `WRITE_OFF` | Cycle slot writes off stale exit | 0 | 0 (marks position closed) |
| `FEE` | Trading fee | Debit or 0 | 0 or Debit |
| `CYCLE_PROFIT` | Round-trip completion | Net credit | Net debit/credit |

### 3.4 Slot-Level Accounting

Each slot maintains running totals derived from its ledger entries:

```python
@dataclass
class SlotAccount:
    slot_id: int
    total_profit_usd: float       # Sum of CYCLE_PROFIT credits - debits
    total_fees_usd: float         # Sum of FEE debits
    total_write_offs_usd: float   # Sum of WRITE_OFF losses
    total_recovery_profit_usd: float  # Profit from recovery fills
    open_position_usd: float      # Current unrealized exposure
    round_trips: int              # Completed cycles
    entries: list[LedgerEntry]    # Full history (in-memory, bounded)
```

### 3.5 Reconciliation

Every main loop cycle, the ledger reconciles:

1. **Order count**: Sum of open orders across all slots vs Kraken `OpenOrders` response
2. **Balance drift**: Ledger's computed `balance_usd_after` vs Kraken's actual USD balance
3. **Position check**: Each slot's open positions vs known Kraken order state

Discrepancies are logged as `RECONCILIATION_DRIFT` entries with the delta. The operator sees a "Ledger Drift" indicator in the dashboard — green (exact match), amber (drift < 1%), red (drift >= 1%).

### 3.6 Persistence

- **File**: `logs/ledger.jsonl` — append-only, one JSON object per line
- **Memory**: Last 500 entries per slot in-memory for dashboard queries
- **Optional**: Supabase table `ledger_entries` (if configured)

### 3.7 Dashboard Integration

The ledger feeds:
- **P&L card**: Per-slot and aggregate profit, with fee and write-off breakdown
- **Audit trail**: Scrollable table of recent entries, filterable by slot/type
- **Reconciliation badge**: Green/amber/red drift indicator
- **Factory view**: Profit chest fill level is ledger-derived, not a float accumulator

---

## 4. Slot Engine

### 4.1 SlotRuntime

```python
@dataclass
class SlotRuntime:
    slot_id: int
    state: PairState              # From state_machine.py
    sticky: bool                  # True = sticky, False = cycle
    alias: str                    # Human-readable name
    account: SlotAccount          # Ledger-derived accounting
    created_at: float             # When slot was added
    paused: bool = False          # Individually pausable
```

### 4.2 Work Bank Math

The work bank computes how many orders are available for new slots:

```python
def compute_work_bank(slots, capacity_limit, safety_ratio=0.75):
    effective_cap = int(capacity_limit * safety_ratio)  # 168 for Pro tier

    # Count current open orders (entries + exits + recoveries)
    open_orders = sum(
        len(s.state.orders) + len(s.state.recovery_orders)
        for s in slots.values()
    )

    headroom = effective_cap - open_orders

    # Estimate orders-per-new-slot by type
    sticky_cost = 2   # S0: 2 entries; S1: 1 exit + 1 entry
    cycle_cost = 4     # Same as sticky + up to 2 recovery orders

    # How many more slots can we add?
    max_new_sticky = headroom // sticky_cost
    max_new_cycle = headroom // cycle_cost

    return WorkBank(
        effective_cap=effective_cap,
        open_orders=open_orders,
        headroom=headroom,
        max_new_sticky=max_new_sticky,
        max_new_cycle=max_new_cycle,
    )
```

For 112 sticky-only slots, all in S1: 112 * 2 = 224 orders. That's 224/225 = 99.6% utilization — too tight. At 75% safety: 112 * 2 = 224 > 168. So **84 simultaneous S1 sticky slots** is the real cap.

But slots rotate through S0 (where they also hold 2 orders), so 84 is the steady-state max. To approach 112, you need many slots sitting idle (in a paused or waiting state). The work bank enforces this.

**Realistic capacity table**:

| Config | Sticky Slots | Cycle Slots | Max Open Orders | Headroom |
|--------|-------------|-------------|-----------------|----------|
| All sticky | 84 | 0 | 168 | 0 |
| Heavy sticky | 70 | 7 | 168 | 0 |
| Balanced | 50 | 15 | 160 | 8 |
| Heavy cycle | 20 | 30 | 160 | 8 |
| All cycle | 0 | 42 | 168 | 0 |

### 4.3 Slot Lifecycle

```
ADD_SLOT → S0 (bootstrap: place entries)
         → S1a or S1b (entry fills, exit placed)
         → S0 (exit fills, profit booked, new entries)
         → ... (repeat)

REMOVE_SLOT → Cancel all open orders on Kraken
            → Close all positions in ledger (mark-to-market)
            → Archive slot data
```

### 4.4 Per-Slot Toggle

Toggling `sticky` on a live slot:

- **Sticky → Cycle**: Next timer tick will start evaluating orphan timeouts for any open exits. If an exit is already stale, it'll be repriced/orphaned on the next cycle.
- **Cycle → Sticky**: Immediately stops orphan evaluation. Existing recovery orders are kept (they're already on Kraken's book) but no new ones are created.

The toggle is:
- Dashboard button per slot
- API: `POST /api/action {action: "toggle_sticky", slot_id: N}`
- Persisted in state snapshot

---

## 5. Slot Governor (Intelligence Layer)

### 5.1 Purpose

The governor answers two questions every cycle:
1. **Ratio**: How many slots should be sticky vs cycle?
2. **Auto-close**: Which orphaned exits should be force-closed?

### 5.2 Ratio Governor

The ratio between sticky and cycle slots affects the bot's risk/reward profile:

| More Sticky | More Cycle |
|-------------|------------|
| Lower orphan rate | Higher orphan rate |
| Capital locked longer | Capital recycles faster |
| Higher per-cycle profit (exits wait for full target) | Lower per-cycle profit (exits may be written off) |
| Fewer Kraken orders (predictable budget) | More Kraken orders (recovery orders consume headroom) |
| Better in ranging markets | Better in trending markets |

The governor uses a simple metric: **orphan pressure**.

```python
def compute_orphan_pressure(slots):
    """
    0.0 = no orphans, all exits filling normally
    1.0 = every cycle slot is producing orphans faster than they resolve
    """
    cycle_slots = [s for s in slots.values() if not s.sticky]
    if not cycle_slots:
        return 0.0

    total_recoveries = sum(len(s.state.recovery_orders) for s in cycle_slots)
    max_recoveries = len(cycle_slots) * MAX_RECOVERY_SLOTS

    return total_recoveries / max_recoveries if max_recoveries > 0 else 0.0
```

**Policy**:

| Orphan Pressure | Action |
|-----------------|--------|
| 0.0 - 0.3 | Healthy. Governor may suggest converting some sticky → cycle for faster capital recycling. |
| 0.3 - 0.6 | Moderate. Hold current ratio. |
| 0.6 - 0.8 | Elevated. Governor suggests converting some cycle → sticky. |
| 0.8 - 1.0 | Critical. Governor force-converts the most-orphaned cycle slots to sticky. |

The governor doesn't auto-convert without operator approval unless in "autonomous" mode (configurable). Default: suggestions only, shown in dashboard.

### 5.3 Auto-Close Mechanics

For cycle slots, stale exits follow: **reprice → write-off → fresh entry**.

The state machine already handles S1 orphaning (timeout) and S2 break-glass. But the *write-off* step — where a cycle slot accepts the loss and moves on — needs explicit handling:

```python
def auto_close_stale_exits(slot, ledger, now):
    """
    For cycle slots only. Called when OrphanOrderAction is emitted.

    1. Cancel the exit order on Kraken (state machine already emitted the action)
    2. Record write-off in ledger with mark-to-market loss
    3. State machine already placed fresh entry (OrphanOrderAction flow)
    """
    # The state machine's _orphan_exit() already:
    # - Removed exit from orders
    # - Created recovery order (if not at cap, evicted old ones)
    # - Placed fresh entry
    # - Emitted OrphanOrderAction

    # The slot engine additionally:
    if not slot.sticky:
        # Cancel recovery order on Kraken (don't keep lottery tickets)
        # Record write-off in ledger
        # Recovery order slot freed immediately
        pass
    else:
        # Keep recovery order as lottery ticket (existing behavior)
        pass
```

**Key difference from v1**: In v1, `OrphanOrderAction` is a no-op for sticky slots (the order stays on Kraken as a lottery ticket). In v2:

- **Sticky slots**: Same behavior — recovery orders are kept as lottery tickets.
- **Cycle slots**: Recovery orders are immediately cancelled on Kraken. The position is written off in the ledger. No lingering orders. Clean slot, fresh start.

This is what makes cycle slots order-budget-predictable: they never accumulate recovery orders.

### 5.4 Governor Configuration

```python
# Ratio governor
GOVERNOR_ENABLED = True              # Master switch
GOVERNOR_MODE = "suggest"            # "suggest" | "autonomous"
GOVERNOR_ORPHAN_PRESSURE_HIGH = 0.6  # Trigger cycle→sticky suggestion
GOVERNOR_ORPHAN_PRESSURE_CRIT = 0.8  # Force-convert in autonomous mode

# Auto-close
CYCLE_SLOT_WRITE_OFF = True          # Cancel recovery orders for cycle slots
CYCLE_SLOT_MAX_REPRICES = 3          # Max reprices before write-off
```

### 5.5 Governor Dashboard Widget

```
┌─ Slot Governor ─────────────────────────┐
│ Sticky: 8 slots    Cycle: 4 slots       │
│ Orphan pressure: 0.42 [██████░░░░] 42%  │
│ Headroom: 31 orders (18%)               │
│                                         │
│ Suggestion: (none — pressure normal)    │
│                                         │
│ [+ Add Sticky] [+ Add Cycle]            │
└─────────────────────────────────────────┘
```

### 5.6 Future: Pluggable Intelligence

The governor has a hook for external intelligence plugins:

```python
class GovernorPlugin:
    """Base class for intelligence plugins."""
    def recommend_ratio(self, slots, capacity, market_data) -> RatioRecommendation | None:
        return None

    def recommend_close(self, slot, recovery_order, market_data) -> CloseRecommendation | None:
        return None
```

v1's HMM, Bayesian engine, and AI advisor can be wrapped as plugins in v2.1. They are NOT part of the v2 core.

---

## 6. Factory View + Bauhaus Overlay

### 6.1 What Ships

`factory_viz.py` (5,043 lines) ships as-is with these modifications:

1. **Strip DOGE references** — replace "DOGE" with the configured base asset symbol
2. **Strip hardcoded pair** — read pair from `/api/status` response
3. **Keep both render modes** — Factory (default) and Bauhaus (toggle with `b` key)
4. **Keep all animations** — fill animations, orphan morphs, recovery dissolves, sparkles
5. **Keep keyboard system** — VIM-style navigation, command bar, help overlay

### 6.2 Bauhaus Overlay

The Bauhaus mode is an alternate visualization that renders:

- **Horizontal bars**: Sell (left, orange) and Buy (right, teal) DOGE volume on book
- **Thickness history**: Rolling window (60 polls) showing volume accumulation over time
- **Order dots**: Individual orders as circles, scaled by volume, offset by price distance
- **Orphan sprites**: Recovery orders shown as morphing shapes
- **Profit counter**: Animated USD tally with "gullet" fill animation
- **Sparkles**: Celebration particles on cycle completion

Colors: `void=#FFFFFF`, `frame=#E8881F`, `canvas=#F4C430`, `structure=#000000`, `s1a=#00CED1`, `s1b=#9B59B6`, `s2=#E74C3C`.

### 6.3 Slot Type Badges

Both Factory and Bauhaus views show slot type:

| Badge | Factory Visual | Bauhaus Visual |
|-------|---------------|----------------|
| `[S]` Sticky | Blue border on machine | Blue dot indicator |
| `[C]` Cycle | Orange border on machine | Orange dot indicator |

### 6.4 Governor Integration

The factory view shows governor state:
- **Orphan pressure meter**: Small gauge in the status bar
- **Suggestion toast**: When governor suggests a ratio change
- **Headroom bar**: Capacity utilization (existing, enhanced with sticky/cycle breakdown)

---

## 7. Dashboard

### 7.1 Stripped Dashboard

The existing dashboard (`dashboard.py`, 4,845 lines) is cleaned of:
- DOGE-specific branding and labels
- Rangers panel
- Churners panel
- HMM regime display (moved to plugin)
- AI advisor panel (moved to plugin)
- DCA accumulation panel (moved to plugin)
- Concept animation video modals

### 7.2 Core Dashboard Panels

| Panel | Content |
|-------|---------|
| **Summary** | Price, mode, uptime, aggregate P&L, fee total |
| **Slots** | Table: ID, alias, sticky/cycle badge, phase, profit, cycle count, open orders |
| **Selected Slot** | Detail view: orders, recovery orders, recent cycles, ledger entries |
| **Capacity** | Open orders, headroom, band (normal/caution/stop), work bank |
| **Governor** | Sticky/cycle ratio, orphan pressure, suggestions |
| **Ledger** | Recent entries, reconciliation status, drift indicator |
| **Controls** | Add slot (sticky/cycle), pause/resume, entry%/profit% |

### 7.3 Keyboard Navigation

Ships with the VIM-style keyboard system from the Dashboard UX Spec:
- 4 modes: NORMAL, COMMAND, HELP, CONFIRM
- Slot navigation: `1-9`, `[/]`, `gg`, `G`
- Quick actions: `p` (pause), `+` (add), `-` (close), `.` (refresh), `?` (help), `:` (command)
- Command bar with parser, validation, auto-complete, history

---

## 8. Configuration

### 8.1 Pair-Agnostic Defaults

```python
# === EXCHANGE ===
KRAKEN_API_KEY = ""                    # Required
KRAKEN_API_SECRET = ""                 # Required
PAIR = "XDOGEZUSD"                     # Any Kraken pair
BASE_ASSET = "DOGE"                    # Derived from PAIR or explicit
QUOTE_ASSET = "USD"                    # Derived from PAIR or explicit

# === GRID ===
PAIR_ENTRY_PCT = 0.20                  # Entry distance from market (%)
PAIR_PROFIT_PCT = 1.0                  # Profit target (%)
ORDER_SIZE_USD = 2.0                   # Per-order size in quote currency
MIN_VOLUME = 13.0                      # Exchange minimum volume
PRICE_DECIMALS = 6                     # Price rounding
VOLUME_DECIMALS = 0                    # Volume rounding

# === SLOTS ===
INITIAL_SLOTS = 2                      # Slots on first boot
INITIAL_STICKY_RATIO = 0.75           # 75% sticky, 25% cycle
MAX_SLOTS = 84                         # Hard cap (capacity-derived)
SLOT_PROFIT_THRESHOLD = 5.0           # Auto-slot threshold (USD)
MAX_AUTO_SLOTS = 5                     # Max auto-spawned slots

# === CAPACITY ===
KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT = 225
OPEN_ORDER_SAFETY_RATIO = 0.75        # 168 effective
CAPACITY_CAUTION_HEADROOM = 20
CAPACITY_STOP_HEADROOM = 10

# === CYCLE SLOT BEHAVIOR ===
S1_ORPHAN_AFTER_SEC = 1350.0          # 22.5 min
S2_ORPHAN_AFTER_SEC = 1800.0          # 30 min
MAX_RECOVERY_SLOTS = 2                 # Per slot
CYCLE_SLOT_WRITE_OFF = True           # Cancel recovery immediately

# === STICKY SLOT BEHAVIOR ===
# (No orphan timeouts — exits wait indefinitely)
# Repricing driven by operator or governor plugin

# === GOVERNOR ===
GOVERNOR_ENABLED = True
GOVERNOR_MODE = "suggest"              # "suggest" | "autonomous"
GOVERNOR_ORPHAN_PRESSURE_HIGH = 0.6
GOVERNOR_ORPHAN_PRESSURE_CRIT = 0.8

# === FEES ===
MAKER_FEE_PCT = 0.25                  # Maker fee (%)
MIN_PROFIT_MARGIN_PCT = 0.20          # Floor: fees + 0.2% minimum

# === TIMING ===
POLL_INTERVAL_SECONDS = 30
STALE_PRICE_MAX_AGE_SEC = 60.0
ENTRY_REFRESH_PCT = 1.0               # Refresh entries drifted > 1%

# === PERSISTENCE ===
STATE_FILE = "logs/state.json"
LEDGER_FILE = "logs/ledger.jsonl"
SUPABASE_URL = ""                      # Optional
SUPABASE_KEY = ""                      # Optional

# === DASHBOARD ===
DASHBOARD_PORT = 8080
DASHBOARD_ENABLED = True
FACTORY_ENABLED = True

# === DRY RUN ===
DRY_RUN = False                        # Simulate without placing real orders
```

### 8.2 .env.example

Ships with a clean `.env.example` containing only the above variables, no personal data, no API keys, no DOGE-specific values.

---

## 9. Stripping Personal Data

### 9.1 Files to Sanitize

| File | Action |
|------|--------|
| `.env.example` | Rewrite from scratch, empty values |
| `config.py` | Remove all DOGE-specific defaults, make pair-agnostic |
| `bot.py` | Extract to `main.py` + `slot_engine.py` + `server.py` |
| `dashboard.py` | Strip DOGE branding, remove v1-specific panels |
| `factory_viz.py` | Replace "DOGE" with `BASE_ASSET` template variable |
| `logs/` | `.gitignore` the entire directory |
| `supabase_store.py` | Keep as optional persistence adapter |
| `README.md` | Rewrite for v2 |

### 9.2 Files to Delete

| File | Reason |
|------|--------|
| `grid_strategy.py` | Legacy state machine, replaced |
| `kelly_sizer.py` | Replaced by simple sizing + optional plugin |
| `KELLY_SPEC.md` | Superseded |
| `KELLY_IMPLEMENTATION_PLAN_REVIEW.md` | Superseded |
| `HMM_INTEGRATION.md` | Plugin territory |
| `hmm_regime_detector.py` | Plugin |
| `bayesian_engine.py` | Plugin |
| `bocpd.py` | Plugin |
| `ai_advisor.py` | Plugin |
| `survival_model.py` | Plugin |
| `stats_engine.py` | Plugin |
| `signal_digest.py` | Plugin |
| `throughput_sizer.py` | Plugin |
| `pair_scanner.py` | v2.1 |
| `notifier.py` | v2.1 (Telegram) |
| `telegram_menu.py` | v2.1 |
| `ARCHITECTURE.md` | Rewrite for v2 |
| `VINTAGE_DATA_COLLECTION_PROMPT.md` | Personal |
| `STATE_MACHINE.md` | Rewrite for v2 |
| `evolution-map-app/` | Personal |
| `animations/` | Personal |
| `tools/audio_hooks/` | Personal |
| `tools/render_concept_animation.sh` | Personal |
| `tools/upload_videos.py` | Personal |

### 9.3 Files to Move to `plugins/` (optional distribution)

```
plugins/
├── intelligence/
│   ├── hmm_regime_detector.py
│   ├── bayesian_engine.py
│   ├── bocpd.py
│   ├── ai_advisor.py
│   ├── survival_model.py
│   ├── stats_engine.py
│   ├── signal_digest.py
│   └── throughput_sizer.py
├── notifications/
│   ├── notifier.py
│   └── telegram_menu.py
└── scanning/
    └── pair_scanner.py
```

---

## 10. Implementation Plan

### Phase 1: Core Extract (state_machine + ledger + slot_engine)

**Goal**: Working bot with 2 slots, no dashboard, no visualization.

1. Copy `state_machine.py` as-is (it's already clean)
2. Write `ledger.py` — LedgerEntry, SlotAccount, reconciliation
3. Write `slot_engine.py` — SlotRuntime, work bank, per-slot sticky/cycle toggle
4. Write `config.py` — pair-agnostic, env-driven
5. Write `kraken_adapter.py` — extract from `kraken_client.py` (883 lines, most reusable)
6. Write `main.py` — main loop (price fetch → transitions → fill detection → actions → save)
7. Tests: state machine invariants, ledger double-entry balance, work bank math

**Estimated size**: ~3,000 lines

### Phase 2: Governor

**Goal**: Intelligence layer governing ratio and auto-close.

1. Write `governor.py` — orphan pressure, ratio suggestions, auto-close for cycle slots
2. Write `capacity.py` — order headroom, bands, partial-fill detection
3. Integrate governor into main loop (post-transition hook)
4. Tests: orphan pressure calculation, ratio suggestions, work bank with mixed slots

**Estimated size**: ~800 lines

### Phase 3: Dashboard

**Goal**: Operational control surface.

1. Extract and clean `dashboard.py` — strip DOGE branding, remove v1 panels
2. Write `server.py` — HTTP server, `/api/status`, `/api/action`, SSE
3. Keyboard navigation system (from Dashboard UX Spec)
4. Governor widget, ledger panel, capacity card

**Estimated size**: ~3,000 lines

### Phase 4: Factory + Bauhaus

**Goal**: Visual factory simulation.

1. Clean `factory_viz.py` — replace DOGE references, add slot type badges
2. Add governor integration (orphan pressure meter, suggestion toasts)
3. Route: `GET /factory`

**Estimated size**: ~5,000 lines (mostly existing)

### Phase 5: Polish & Distribution

**Goal**: Shippable package.

1. Rewrite `README.md` — setup, config, usage, screenshots
2. Write `ARCHITECTURE.md` — v2 module map, data flow, state machine reference
3. Clean `.env.example`
4. `.gitignore` for logs, __pycache__, .env
5. Dockerfile (existing is fine, just verify)
6. Optional: `plugins/` directory with intelligence stack

---

## 11. Migration Path

For the author's personal deployment:

1. **Freeze v1**: Tag `v1.0.0`, never touch again
2. **Fresh branch**: `git checkout -b v2` from a clean state
3. **Port state**: Write a one-time `migrate_v1_state.py` that reads v1's `logs/state.json` and converts to v2 format (PairState objects are the same, just need to add ledger entries for existing positions)
4. **Parallel run**: Run v2 in dry-run mode alongside v1 for 48h, compare decisions
5. **Cutover**: Stop v1, start v2 with real orders

---

## 12. Success Criteria

| Criterion | Metric |
|-----------|--------|
| 84 sticky slots run without hitting capacity | headroom >= 0 at steady state |
| Cycle slots produce predictable order budget | max 4 orders per cycle slot |
| Ledger reconciles within 0.1% of Kraken balance | drift < $0.01 per cycle |
| Governor correctly identifies orphan pressure | pressure tracks actual recovery order fill rate |
| Factory viz renders 84 machines without lag | 30fps at 84 slots |
| Dashboard is fully keyboard-navigable | All actions reachable in < 3 keypresses |
| Zero personal data in distribution | No API keys, no DOGE-specific defaults, no personal usernames |
| Clean boot with no state file | First boot creates 2 slots and starts trading |
| Total codebase < 12,000 lines | ~75% reduction from v1 |

---

## 13. Out of Scope (v2.1+)

| Feature | Why Deferred |
|---------|-------------|
| Multi-pair swarm | Complexity; v2 is single-pair |
| Pair scanner | Depends on multi-pair |
| HMM regime detection | Plugin |
| AI regime advisor | Plugin |
| DCA accumulation | Plugin |
| Telegram notifications | Plugin |
| Bayesian belief state | Plugin |
| Throughput sizer | Plugin (simple sizing ships instead) |
| Survival model | Plugin |
| Audio hooks | Personal |
| Concept animations | Personal |
| Evolution map app | Personal |

---

## 14. Open Questions

1. **Auto-slots**: Should v2 ship with auto-slot creation (profit-funded scaling)? Currently planned yes with `SLOT_PROFIT_THRESHOLD`.

2. **Recovery orders for cycle slots**: The spec says "cancel immediately" for cycle slots. Alternative: keep for N minutes as a short lottery window before write-off. Tradeoff: more order headroom usage vs potential profit recovery.

3. **Sticky slot repricing**: v1 has no repricing for sticky slots (they wait forever). Should v2's governor offer optional progressive repricing for sticky slots that have waited beyond a configurable threshold?

4. **Plugin API surface**: How much of the main loop should be hookable? Minimal (just governor) or extensive (pre/post every phase)?
