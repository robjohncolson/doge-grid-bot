# Grid Bot v2 — Implementation Plan

**Version**: 0.1.0
**Date**: 2026-02-18
**Companion to**: `V2_MANIFESTO.md` (the build spec), `V2_CLEAN_SLATE_SPEC.md` (technical details)
**Purpose**: Maximize parallel agent execution. Every task card is self-contained and hand-off-ready.

> **Precedence rule**: If `V2_CLEAN_SLATE_SPEC.md` conflicts with `V2_MANIFESTO.md`, the manifesto wins.
> The clean-slate spec remains valid for technical details (ledger schema, work bank math, slot lifecycle)
> but is overridden on all architectural and intelligence-layer decisions (per manifesto §11, final note).

> **Architecture decision**: The main loop is **async** (`asyncio`), matching the manifesto's
> `async def run_loop_once()` (manifesto §4.3, lines 357-395). The Kraken adapter uses
> `asyncio`-native I/O. The Telegram bot uses `python-telegram-bot`'s async mode on the
> same event loop. This is a deliberate v2 decision — v1 was sync, v2 is async.

> **Dependency decision (locked)**: Exactly two external packages: `numpy` (HMM), `python-telegram-bot` (Telegram interface).
> No `aiohttp`, no `httpx`, no `requests`. The Kraken adapter wraps `urllib.request` via `run_in_executor`.
> The HTTP server uses `asyncio.start_server` (stdlib). SSE is manual chunked-transfer over async sockets.
> `sqlite3` is stdlib and does not count.

> **Timeline note**: The manifesto estimates ~24 sequential days (§7). This plan compresses to
> ~11-12 calendar days through parallel agent execution, enabled by the `types.py` interface
> contract that decouples all modules on Day 1.

---

## 0. Dependency Graph

```
Layer 0 (no deps)        Layer 1 (config only)       Layer 2           Layer 3              Layer 4           Layer 5
┌────────────────┐    ┌──────────────────────┐   ┌────────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────┐
│ state_machine  │───▶│ ledger               │──▶│slot_engine │──▶│ governor     │──▶│ server       │──▶│ main   │
│ (copy)         │    │                      │   │            │   │              │   │              │   │        │
└────────────────┘    └──────────────────────┘   └────────────┘   └──────────────┘   └──────────────┘   └────────┘
┌────────────────┐    ┌──────────────────────┐                    ┌──────────────┐   ┌──────────────┐
│ config         │───▶│ kraken_adapter       │───────────────────▶│ simulator    │   │ factory_view │
│ (rewrite)      │    └──────────────────────┘                    └──────────────┘   │ (mock JSON)  │
└────────────────┘    ┌──────────────────────┐                    ┌──────────────┐   └──────────────┘
                      │ capacity             │                    │ diagnostic   │   ┌──────────────┐
                      └──────────────────────┘                    └──────┬───────┘   │ dashboard    │
                      ┌──────────────────────┐                           │            │ (mock JSON)  │
                      │ scanner              │                    ┌──────▼───────┐   └──────────────┘
                      │ (numpy)              │                    │ telegram     │   ┌──────────────┐
                      └──────────────────────┘                    └──────────────┘   │ audio        │
                                                                                    │ (standalone)  │
                                                                                    └──────────────┘
```

**Critical path** (longest sequential chain):
```
config → ledger → slot_engine → governor → server → main
  D0       D1-3      D2-4         D3-6       D5-8    D7-10
```

Everything not on this chain can run in parallel.

---

## 1. Phase 0: Scaffold + Interface Contracts (Day 1)

**Agent count**: 1 (sequential — this defines the contracts everything else builds against)
**Deliverable**: Stub repo with all 16 modules, shared types, test harness

### Task 0.1 — Create repo scaffold

```
Agents: 1 (sequential)
Estimated: 400 lines
```

**Work**:

1. Create `grid-bot-v2/` directory structure per manifesto §4.1:
   ```
   grid-bot-v2/
   ├── state_machine.py      # Copy from v1 UNCHANGED
   ├── types.py              # NEW — shared interfaces (see §1.1 below)
   ├── config.py             # Stub with all env vars from V2_CLEAN_SLATE_SPEC §8
   ├── ledger.py             # Stub
   ├── slot_engine.py        # Stub
   ├── governor.py           # Stub
   ├── scanner.py            # Stub
   ├── capacity.py           # Stub
   ├── kraken_adapter.py     # Stub
   ├── factory_view.py       # Stub
   ├── dashboard.py          # Stub
   ├── server.py             # Stub
   ├── audio.py              # Stub
   ├── simulator.py          # Stub
   ├── diagnostic.py         # Stub
   ├── telegram.py       # Stub
   ├── main.py               # Stub
   ├── tests/
   │   ├── test_state_machine.py
   │   ├── test_ledger.py
   │   ├── test_slot_engine.py
   │   ├── test_governor.py
   │   ├── test_scanner.py
   │   ├── test_capacity.py
   │   ├── test_simulator.py
   │   └── conftest.py       # Shared fixtures
   ├── fixtures/
   │   └── status_payload.json  # Mock status payload for UI agents
   ├── logs/
   │   └── .gitkeep
   ├── plugins/
   │   └── intelligence/
   │       └── .gitkeep
   ├── .env.example
   ├── .gitignore
   ├── requirements.txt      # numpy, python-telegram-bot (optional)
   └── README.md             # Placeholder
   ```

2. Copy `state_machine.py` from v1 unchanged. Verify: `python -c "from state_machine import transition, PairState, EngineConfig"` succeeds.

3. Create `types.py` — the shared interface file (see next section).

4. Create `fixtures/status_payload.json` — the mock status payload (see §1.2).

5. Every stub module has:
   - Module docstring describing purpose (from manifesto)
   - All class/function signatures with `...` bodies
   - Type hints referencing `types.py` or `state_machine.py`
   - `# TODO: implement` markers

6. Create `tests/conftest.py` with shared fixtures:
   - `make_pair_state()` — factory for `PairState` with sane defaults
   - `make_engine_config()` — factory for `EngineConfig`
   - `make_slot_runtime()` — factory for `SlotRuntime`
   - `make_market_character()` — factory for `MarketCharacter`
   - `mock_adapter()` — mock Kraken adapter

**Acceptance**: `python -m pytest tests/ --collect-only` discovers all test files. All imports resolve (stubs exist). State machine transitions pass with default config.

### 1.1 — `types.py` (Shared Interface Contracts)

This file defines every cross-module type. It's the "API boundary" that unlocks parallel work.

```python
"""
Shared types for grid-bot-v2.

Every module imports from here rather than from each other's internals.
This file is the contract — change it and all agents must adapt.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Re-export state_machine types for convenience
from state_machine import (
    PairState, OrderState, RecoveryOrder, CycleRecord, EngineConfig,
    PriceTick, TimerTick, FillEvent, RecoveryFillEvent, RecoveryCancelEvent,
    PlaceOrderAction, CancelOrderAction, OrphanOrderAction, BookCycleAction,
    Side, Role, TradeId, Event, Action,
    transition, bootstrap_orders, derive_phase, check_invariants,
    compute_order_volume, entry_backoff_multiplier,
)


# ── MarketCharacter (scanner → governor) ──────────────────────────

@dataclass(frozen=True)
class MarketCharacter:
    regime: str              # "ranging" | "mild_trend" | "strong_trend"
    trend_direction: str     # "up" | "down" | "none"
    volatility: float        # ATR-based, normalized 0.0-1.0
    confidence: float        # 0.0 - 1.0
    updated_at: float        # Unix timestamp

UNKNOWN_MARKET = MarketCharacter(
    regime="ranging", trend_direction="none",
    volatility=0.5, confidence=0.0, updated_at=0.0,
)


# ── LedgerEntry (ledger → dashboard, diagnostic) ─────────────────

@dataclass(frozen=True)
class LedgerEntry:
    id: int
    timestamp: float
    slot_id: int
    trade_id: str            # "A" or "B"
    cycle: int
    entry_type: str          # BUY_ENTRY, SELL_ENTRY, BUY_EXIT, SELL_EXIT,
                             # RECOVERY_FILL, RECOVERY_CANCEL, WRITE_OFF, FEE, CYCLE_PROFIT
    debit_usd: float
    credit_usd: float
    debit_base: float
    credit_base: float
    price: float
    fee_usd: float
    fee_base: float
    txid: str
    order_local_id: int
    balance_usd_after: float
    balance_base_after: float
    note: str


# ── SlotAccount (ledger → slot_engine, dashboard) ────────────────

@dataclass
class SlotAccount:
    slot_id: int
    total_profit_usd: float = 0.0
    total_fees_usd: float = 0.0
    total_write_offs_usd: float = 0.0
    total_recovery_profit_usd: float = 0.0
    open_position_usd: float = 0.0
    round_trips: int = 0


# ── SlotRuntime (slot_engine → governor, server, diagnostic) ─────

@dataclass
class SlotRuntime:
    slot_id: int
    state: PairState
    sticky: bool = True
    alias: str = ""
    account: SlotAccount = field(default_factory=lambda: SlotAccount(slot_id=0))
    created_at: float = 0.0
    paused: bool = False


# ── WorkBank (capacity → governor, server, dashboard) ────────────

@dataclass(frozen=True)
class WorkBank:
    effective_cap: int       # e.g., 168 (225 * 0.75)
    open_orders: int         # Current count on Kraken
    headroom: int            # effective_cap - open_orders
    max_new_sticky: int      # headroom // 2
    max_new_cycle: int       # headroom // 4


# ── CapacityBand ─────────────────────────────────────────────────

BAND_NORMAL = "normal"       # headroom >= 20
BAND_CAUTION = "caution"     # 10 <= headroom < 20
BAND_STOP = "stop"           # headroom < 10


# ── GovernorActions (governor → main, server, dashboard) ─────────

@dataclass
class GovernorActions:
    orphan_pressure: float = 0.0
    ratio_suggestion: str | None = None       # e.g., "convert 2 cycle → sticky"
    recovery_ttl: float = 300.0               # Dynamic TTL for cycle slot recoveries
    stuck_scores: dict[int, float] = field(default_factory=dict)  # slot_id → score
    write_off_candidates: list[int] = field(default_factory=list)  # slot_ids
    capacity_band: str = BAND_NORMAL


# ── FillTimeStats (telemetry → governor) ─────────────────────────

@dataclass(frozen=True)
class FillTimeStats:
    p50: float = 900.0       # Median fill time (seconds)
    p90: float = 3600.0      # 90th percentile
    p95: float = 7200.0      # 95th percentile
    sample_count: int = 0


# ── FactoryTelemetry (main loop → governor) ──────────────────────

@dataclass
class FactoryTelemetry:
    exit_age_median: float = 0.0
    exit_age_p90: float = 0.0
    exit_age_p95: float = 0.0
    distance_to_market_avg: float = 0.0
    orphan_rate_per_hour: float = 0.0
    write_off_cost_per_hour: float = 0.0
    headroom_utilization: float = 0.0
    fill_time_stats: FillTimeStats = field(default_factory=FillTimeStats)
    fill_time_stats_a: FillTimeStats = field(default_factory=FillTimeStats)
    fill_time_stats_b: FillTimeStats = field(default_factory=FillTimeStats)


# ── Protocols (for dependency inversion) ─────────────────────────

@runtime_checkable
class ExchangeAdapter(Protocol):
    """Interface for exchange communication. Kraken adapter implements this."""
    async def fetch_price(self) -> float: ...
    async def fetch_ohlcv(self, interval: int = 60) -> list: ...
    async def place_order(self, side: str, volume: float, price: float,
                          post_only: bool = True) -> str: ...
    async def cancel_order(self, txid: str) -> bool: ...
    async def query_orders(self, txids: list[str]) -> dict: ...
    async def get_balance(self) -> dict: ...
    async def get_open_orders(self) -> dict: ...
    async def get_open_orders_count(self) -> int: ...


@runtime_checkable
class GovernorPlugin(Protocol):
    """Optional intelligence plugin for the governor."""
    def recommend_ratio(self, slots: dict[int, SlotRuntime],
                        capacity: WorkBank,
                        market: MarketCharacter) -> str | None: ...
    def recommend_close(self, slot: SlotRuntime,
                        market: MarketCharacter) -> list[int] | None: ...


# ── Event taxonomy (for audio + commentator) ─────────────────────

EVENT_CYCLE_COMPLETE = "cycle_complete"
EVENT_ENTRY_FILL = "entry_fill"
EVENT_ORPHAN_CREATED = "orphan_created"
EVENT_S2_ENTERED = "s2_entered"
EVENT_S2_RESOLVED = "s2_resolved"
EVENT_WRITE_OFF = "write_off"
EVENT_RECOVERY_FILL = "recovery_fill"
EVENT_GOVERNOR_RATIO_CHANGE = "governor_ratio_change"
EVENT_SLOT_ADDED = "slot_added"
EVENT_SLOT_REMOVED = "slot_removed"

BOT_EVENTS = [
    EVENT_CYCLE_COMPLETE, EVENT_ENTRY_FILL, EVENT_ORPHAN_CREATED,
    EVENT_S2_ENTERED, EVENT_S2_RESOLVED, EVENT_WRITE_OFF,
    EVENT_RECOVERY_FILL, EVENT_GOVERNOR_RATIO_CHANGE,
    EVENT_SLOT_ADDED, EVENT_SLOT_REMOVED,
]
```

### 1.2 — `fixtures/status_payload.json`

Mock status payload that UI agents (factory_view, dashboard, audio) develop against:

```json
{
  "price": 0.09012,
  "price_age_sec": 2.3,
  "pair": "XDOGEZUSD",
  "base_asset": "DOGE",
  "quote_asset": "USD",
  "mode": "live",
  "uptime_sec": 86400,
  "entry_pct": 0.20,
  "profit_pct": 1.0,
  "total_profit": 12.47,
  "total_fees": 3.21,
  "total_write_offs": 0.83,
  "total_round_trips": 47,
  "total_orphans": 5,
  "slot_count": 12,
  "slots": [
    {
      "slot_id": 1,
      "alias": "Slot 1",
      "sticky": true,
      "paused": false,
      "phase": "S1b",
      "total_profit": 1.23,
      "total_fees": 0.31,
      "total_write_offs": 0.0,
      "round_trips": 5,
      "cycle_a": 3,
      "cycle_b": 5,
      "stuck_score": 0.0,
      "open_orders": [
        {
          "local_id": 1,
          "side": "buy",
          "role": "entry",
          "trade_id": "B",
          "cycle": 6,
          "price": 0.08994,
          "volume": 556,
          "txid": "OXXXX-XXXXX-XXXXXX"
        },
        {
          "local_id": 2,
          "side": "sell",
          "role": "exit",
          "trade_id": "B",
          "cycle": 5,
          "price": 0.09102,
          "volume": 556,
          "txid": "OXXXX-XXXXX-XXXXXY",
          "age_sec": 420,
          "distance_pct": 1.0
        }
      ],
      "recovery_orders": [],
      "recent_cycles": [
        {
          "trade_id": "B",
          "cycle": 4,
          "profit_usd": 0.47,
          "duration_sec": 612,
          "entry_price": 0.08950,
          "exit_price": 0.09040
        }
      ]
    },
    {
      "slot_id": 2,
      "alias": "Slot 2",
      "sticky": false,
      "paused": false,
      "phase": "S1a",
      "total_profit": 0.89,
      "total_fees": 0.22,
      "total_write_offs": 0.27,
      "round_trips": 3,
      "cycle_a": 3,
      "cycle_b": 2,
      "stuck_score": 1.4,
      "open_orders": [
        {
          "local_id": 5,
          "side": "sell",
          "role": "exit",
          "trade_id": "A",
          "cycle": 3,
          "price": 0.09150,
          "volume": 556,
          "txid": "OXXXX-XXXXX-XXXXXZ",
          "age_sec": 1380,
          "distance_pct": 1.5
        },
        {
          "local_id": 6,
          "side": "buy",
          "role": "entry",
          "trade_id": "B",
          "cycle": 2,
          "price": 0.08970,
          "volume": 556,
          "txid": "OXXXX-XXXXX-XXXXXW"
        }
      ],
      "recovery_orders": [
        {
          "recovery_id": 1,
          "trade_id": "B",
          "cycle": 1,
          "side": "sell",
          "price": 0.09200,
          "volume": 500,
          "txid": "OXXXX-XXXXX-XXXXXV",
          "reason": "stale",
          "age_sec": 3600,
          "distance_pct": 2.1,
          "ttl_remaining_sec": 180
        }
      ],
      "recent_cycles": []
    }
  ],
  "capacity": {
    "effective_cap": 168,
    "open_orders": 7,
    "headroom": 161,
    "band": "normal",
    "max_new_sticky": 80,
    "max_new_cycle": 40
  },
  "governor": {
    "orphan_pressure": 0.42,
    "ratio_target": "70/30",
    "ratio_current": "1 sticky / 1 cycle",
    "recovery_ttl_sec": 300,
    "suggestion": null,
    "mode": "suggest"
  },
  "scanner": {
    "regime": "ranging",
    "trend_direction": "none",
    "volatility": 0.35,
    "confidence": 0.78,
    "updated_at": 1739836800,
    "training_candles": 127,
    "training_target": 500,
    "training_tier": "shallow"
  },
  "ledger": {
    "total_entries": 94,
    "reconciliation_drift_pct": 0.02,
    "drift_status": "green",
    "recent_entries": []
  },
  "commentator": {
    "recent_messages": [
      {"timestamp": 1739836802, "text": "Slot #1 entry filled: bought 556 DOGE at $0.08994 (Trade B, cycle 6)"},
      {"timestamp": 1739836802, "text": "Exit placed: sell 556 DOGE at $0.09102 (target: +$0.50 profit)"},
      {"timestamp": 1739836851, "text": "Slot #2 exit getting stale (23 min, median is 15 min) — stuck score: 1.4"}
    ]
  }
}
```

### 1.3 — `fixtures/action_api.md`

Document the action API contract:

```
POST /api/action
Content-Type: application/json

Actions:
  {action: "toggle_sticky", slot_id: int}          → Flip sticky/cycle
  {action: "add_slot", sticky: bool}                → Add new slot
  {action: "remove_slot", slot_id: int}             → Remove + cancel orders
  {action: "pause_slot", slot_id: int}              → Toggle pause
  {action: "write_off", slot_id: int}               → Manual write-off of stale exit
  {action: "soft_close", slot_id: int}              → Cancel entries, let exits fill
  {action: "set_config", key: str, value: any}      → Update runtime config
  {action: "approve_suggestion", suggestion_id: str} → Accept governor suggestion

Response:
  {ok: true, message: str}
  {ok: false, error: str}
```

---

## 2. Parallel Execution Map

### Legend

```
[S] = Sequential (must wait for dependency)
[P] = Parallel (can run simultaneously with others in same wave)
Agent = A self-contained work unit for one coding agent
```

### Wave Diagram

```
         Day 1            Days 2-4           Days 3-6          Days 5-8          Days 7-10
     ┌───────────┐    ┌──────────────┐   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
     │ PHASE 0   │    │  WAVE 1      │   │  WAVE 2      │  │  WAVE 3      │  │  WAVE 4      │
     │ Scaffold  │    │  Foundation  │   │  Core        │  │  UI + Intel  │  │  Features    │
     │           │    │              │   │              │  │              │  │              │
     │ 1 agent   │───▶│ 6 agents [P] │──▶│ 2 agents [P] │─▶│ 5 agents [P] │─▶│ 4 agents [P] │
     │           │    │              │   │              │  │              │  │              │
     └───────────┘    │ A: config    │   │ G: slot_eng  │  │ I: factory   │  │ L: simulator │
                      │ B: capacity  │   │ H: governor  │  │ J: dashboard │  │ M: diagnostic│
                      │ C: ledger    │   │              │  │ K: server    │  │ N: telegram  │
                      │ D: scanner   │   │              │  │ K2: audio    │  │ O: main.py   │
                      │ E: adapter   │   │              │  │ K3: comment. │  │              │
                      │ F: test infra│   │              │  │              │  │              │
                      └──────────────┘   └──────────────┘  └──────────────┘  └──────────────┘

                                                                                    │
                                                                             ┌──────▼───────┐
                                                                             │  WAVE 5      │
                                                                             │  Integration │
                                                                             │  + Migration │
                                                                             │              │
                                                                             │  2 agents    │
                                                                             │  Days 9-12   │
                                                                             └──────────────┘
```

---

## 3. Wave 1 — Foundation (6 Parallel Agents)

All Wave 1 agents depend only on Phase 0 output (scaffold + `types.py` + `state_machine.py`).
**All 6 can run simultaneously.**

---

### Agent A: `config.py`

```
Depends on:  Phase 0 scaffold
Parallel:    B, C, D, E, F
Output:      config.py (~250 lines)
```

**Context to provide agent**:
- V2_CLEAN_SLATE_SPEC §8 (config defaults, .env.example, pair-agnostic structure)
- Manifesto §4.1 (module map — for the v2 file list)
- Manifesto §5.5 (diagnostic + telegram config vars)
- V1 `config.py` for reference (strip all dead config)

**Work**:
1. Pair-agnostic config with env-driven loading
2. All variables from V2_CLEAN_SLATE_SPEC §8 with defaults, plus manifesto §5.5 diagnostic/telegram config
3. Validation: raise on missing `KRAKEN_API_KEY` unless `--simulate`
4. `.env.example` with empty values, documenting each variable
5. `PairConfig` class removed (single-pair only)
6. All `RANGER_*`, `CHURNER_*`, `HERD_*`, `KELLY_*`, `TP_*`, `ACCUM_*` config GONE
7. New config groups: `GOVERNOR_*`, `DIAGNOSTIC_*`, `TELEGRAM_*`, `AUDIO_*`, `SIMULATOR_*`

**Acceptance**: `from config import PAIR, ORDER_SIZE_USD, GOVERNOR_ENABLED` succeeds. Loading from `.env` file works. Validation catches missing API key.

---

### Agent B: `capacity.py`

```
Depends on:  types.py (WorkBank), config.py interface
Parallel:    A, C, D, E, F
Output:      capacity.py (~200 lines), tests/test_capacity.py (~150 lines)
```

**Context to provide agent**:
- V2_CLEAN_SLATE_SPEC §4.2 (work bank math, capacity table)
- Manifesto §3.1 point 4 (capacity question)
- `types.py` for `WorkBank`, band constants
- V1 capacity telemetry (from MEMORY.md notes)

**Work**:
1. `CapacityTracker` class:
   - `update(open_orders_count: int, recovery_count: int) -> WorkBank`
   - `band` property → "normal" / "caution" / "stop"
   - `status_payload() -> dict` (for status JSON)
2. Pure math: `effective_cap = limit * safety_ratio`, `headroom = cap - orders`
3. `compute_work_bank(slots: dict[int, SlotRuntime]) -> WorkBank` standalone function
4. Partial-fill canary counter (rolling 24h, from v1 spec)

**Tests**:
- `test_work_bank_all_sticky` → headroom=0 at 84 slots
- `test_work_bank_mixed` → correct costs (sticky=2, cycle=4)
- `test_band_transitions` → normal ↔ caution ↔ stop
- `test_headroom_never_negative`

**Acceptance**: All tests green. `compute_work_bank()` matches the capacity table in V2_CLEAN_SLATE_SPEC §4.2.

---

### Agent C: `ledger.py`

```
Depends on:  types.py (LedgerEntry, SlotAccount), state_machine.py types
Parallel:    A, B, D, E, F
Output:      ledger.py (~800 lines), tests/test_ledger.py (~400 lines)
```

**Context to provide agent**:
- V2_CLEAN_SLATE_SPEC §3 (full ledger spec — schema, entry types, reconciliation, persistence). *Precedence: manifesto wins on conflicts.*
- `types.py` for `LedgerEntry`, `SlotAccount`
- `state_machine.py` for `BookCycleAction`, `OrphanOrderAction`, `CycleRecord`, `OrderState`, `RecoveryOrder`

**Work**:
1. `Ledger` class with monotonic ID counter:
   - `record_entry_fill(slot_id, trade_id, cycle, side, price, volume, fee, txid)`
   - `record_exit_fill(slot_id, trade_id, cycle, side, price, volume, fee, txid, entry_price)`
   - `record_recovery_fill(slot_id, recovery_order, price, volume, fee, txid)`
   - `record_write_off(slot_id, trade_id, cycle, entry_price, market_price, volume)`
   - `record_fee(slot_id, trade_id, cycle, fee_usd, fee_base)`
   - `book_cycle_profit(slot_id, trade_id, cycle, entry_price, exit_price, volume, fees)`
2. `SlotAccount` maintained as running totals per slot
3. `reconcile(slot_accounts, kraken_balance) -> float` returns drift percentage
4. Persistence: append-only `logs/ledger.jsonl`, one JSON line per entry
5. Load: read JSONL, rebuild `SlotAccount` from entries
6. In-memory: last 500 entries per slot (bounded deque)

**Tests**:
- `test_double_entry_balances` → every entry: sum(debits) == sum(credits) globally
- `test_buy_entry_sell_exit_cycle` → full B-side round-trip, correct profit
- `test_sell_entry_buy_exit_cycle` → full A-side round-trip, correct profit
- `test_write_off_records_loss` → negative P&L recorded
- `test_recovery_fill_records_profit` → recovery profit attributed to correct slot
- `test_reconciliation_drift` → detects $0.05 balance mismatch
- `test_jsonl_persistence_roundtrip` → write → load → compare
- `test_slot_account_running_totals` → incremental updates match full replay

**Acceptance**: All tests green. Double-entry invariant holds for every test. JSONL roundtrip is lossless.

---

### Agent D: `scanner.py`

```
Depends on:  types.py (MarketCharacter), config.py interface
Parallel:    A, B, C, E, F
Output:      scanner.py (~600 lines), tests/test_scanner.py (~300 lines)
```

**Context to provide agent**:
- Manifesto §2.2 (what ships in scanner), §9 (what scanner earns)
- `types.py` for `MarketCharacter`, `UNKNOWN_MARKET`
- V1's `hmm_regime_detector.py` for reference HMM implementation
- Note: must implement 3-state Gaussian HMM from scratch using only numpy

**Work**:
1. **Simple indicators** (no numpy needed):
   - `compute_atr(candles, period=14) -> float` — Average True Range
   - `compute_adx(candles, period=14) -> float` — Average Directional Index (0-100)
   - `compute_directional_efficiency(candles, period=14) -> float` — |net| / sum(|bars|)
   - `compute_bb_width(candles, period=20) -> float` — Bollinger Band width
2. **3-state Gaussian HMM** (numpy):
   - States: ranging (0), mild_trend (1), strong_trend (2)
   - Features: log returns, ATR-normalized
   - `fit(candles)` — Baum-Welch (forward-backward + EM)
   - `predict(candles) -> int` — Viterbi decoding for most likely state
   - Log-sum-exp for numerical stability
3. **Scanner class**:
   - `update(candles) -> MarketCharacter`
   - `should_update(now) -> bool` (every N minutes, configurable)
   - Hysteresis: regime change requires `dwell_candles` (default 2) consecutive agreement
   - Cross-validation: HMM regime must agree with ADX direction (if ADX > 25 says trending, HMM should too)
   - Falls back to `UNKNOWN_MARKET` if insufficient data
4. **SQLite candle store**:
   - `logs/ohlcv.db` with table: `candles(timestamp INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, volume REAL)`
   - Rolling window: keep last 500 candles per interval
   - `store_candles(candles)`, `load_candles(interval, limit) -> list`

**Tests**:
- `test_atr_known_values` — verify against hand-calculated ATR
- `test_adx_trending_data` — ADX > 25 for synthetic trend
- `test_adx_ranging_data` — ADX < 20 for synthetic range
- `test_directional_efficiency_perfect_trend` → ~1.0
- `test_directional_efficiency_random_walk` → ~0.0 to 0.3
- `test_hmm_classifies_ranging` — synthetic ranging candles → "ranging"
- `test_hmm_classifies_trending` — synthetic trending candles → "strong_trend"
- `test_hysteresis_prevents_flipflop` — rapid oscillation doesn't change regime
- `test_cold_start_returns_unknown` — <30 candles → UNKNOWN_MARKET
- `test_sqlite_roundtrip` — store → load candles match

**Acceptance**: All tests green. HMM converges on synthetic data. Scanner produces `MarketCharacter` with correct regime for obviously trending/ranging data.

---

### Agent E: `kraken_adapter.py`

```
Depends on:  config.py interface, types.py (ExchangeAdapter protocol)
Parallel:    A, B, C, D, F
Output:      kraken_adapter.py (~600 lines), tests/test_adapter.py (~200 lines)
```

**Context to provide agent**:
- V1's `kraken_client.py` (884 lines) — the source to port from
- `types.py` for `ExchangeAdapter` protocol
- Manifesto note: pair-agnostic, no DOGE-specific code

**Work**:
1. Port from v1's `kraken_client.py`:
   - HMAC-SHA512 authentication (existing, works)
   - Rate-limit tracking (existing counter logic)
   - `fetch_price(pair) -> float` (public ticker endpoint)
   - `fetch_ohlcv(pair, interval) -> list` (public OHLC endpoint)
   - `place_order(side, volume, price, pair, post_only) -> str` (returns txid)
   - `cancel_order(txid) -> bool`
   - `query_orders(txids) -> dict` (batch, chunk by 50)
   - `get_balance() -> dict`
   - `get_open_orders() -> dict`
   - `get_open_orders_count() -> int`
   - `get_pair_constraints(pair) -> dict` (min volume, price decimals, etc.)
2. Make pair-agnostic:
   - Remove all DOGE/USD hardcoding
   - Accept `pair` parameter (from config) on all methods
   - Handle Kraken's key aliasing (XXDG → DOGE, ZUSD → USD) generically
3. Implement `ExchangeAdapter` protocol from `types.py`
4. Async: all public methods are `async def`. Use `asyncio.get_event_loop().run_in_executor(None, sync_call)` to wrap blocking `urllib.request` calls. This keeps the adapter async-native with zero external HTTP deps. (`aiohttp` is NOT used — see dependency decision below.)
5. DRY_RUN mode: log orders instead of placing them

**Tests** (unit only, no live API):
- `test_auth_signature` — HMAC matches known test vector
- `test_rate_limit_counter` — tracks private call budget
- `test_pair_agnostic_url` — constructs correct URL for any pair
- `test_dry_run_no_api_call` — DRY_RUN=True doesn't hit network
- `test_query_orders_chunking` — 120 txids → 3 chunks of 40

**Acceptance**: `ExchangeAdapter` protocol check passes. All pair references are parameterized. No "DOGE" or "XDOGEZUSD" string literals.

---

### Agent F: Test Infrastructure

```
Depends on:  Phase 0 scaffold, state_machine.py
Parallel:    A, B, C, D, E
Output:      tests/conftest.py (~300 lines), tests/helpers.py (~200 lines)
```

**Context to provide agent**:
- `state_machine.py` — all types and `transition()` signature
- `types.py` — all shared types
- Manifesto §4.3 (main loop shape) for understanding what needs mocking

**Work**:
1. **Shared fixtures** (`conftest.py`):
   - `make_pair_state(**overrides) -> PairState` — sane defaults, customizable
   - `make_engine_config(**overrides) -> EngineConfig` — sane defaults
   - `make_slot(slot_id, sticky, phase) -> SlotRuntime` — creates slot in desired phase
   - `make_market_character(regime, **overrides) -> MarketCharacter`
   - `make_work_bank(**overrides) -> WorkBank`
   - `mock_adapter` fixture — returns `MockExchangeAdapter` with controllable responses
2. **MockExchangeAdapter** (`helpers.py`):
   - Implements `ExchangeAdapter` protocol
   - `set_price(price)`, `set_balance(usd, base)`
   - `queue_fill(txid, price, volume)` — next `query_orders` returns this fill
   - `placed_orders` list — records all `place_order` calls
   - `cancelled_orders` list — records all `cancel_order` calls
3. **Synthetic data generators** (`helpers.py`):
   - `generate_ranging_candles(n, center, spread) -> list`
   - `generate_trending_candles(n, start, end) -> list`
   - `generate_volatile_candles(n, center, volatility) -> list`
4. **State machine helpers** (`helpers.py`):
   - `advance_to_s1a(state, cfg) -> (state, actions)` — bootstrap + fill A entry
   - `advance_to_s1b(state, cfg) -> (state, actions)` — bootstrap + fill B entry
   - `advance_to_s2(state, cfg) -> (state, actions)` — fill both entries
   - `complete_cycle(state, cfg, trade_id) -> (state, actions)` — entry + exit fill

**Acceptance**: All fixture factories produce valid objects. `MockExchangeAdapter` passes `ExchangeAdapter` protocol check. Helper functions produce correct state machine phases.

---

## 4. Wave 2 — Core Runtime (2 Parallel Agents)

Wave 2 needs Wave 1 output. Both agents can run simultaneously.

---

### Agent G: `slot_engine.py`

```
Depends on:  state_machine.py, ledger.py, capacity.py, config.py, types.py
Parallel:    H (governor)
Output:      slot_engine.py (~800 lines), tests/test_slot_engine.py (~500 lines)
```

**Context to provide agent**:
- Manifesto §1 (locked decisions), specifically sticky/cycle per-slot
- V2_CLEAN_SLATE_SPEC §4 (slot engine — SlotRuntime, work bank, lifecycle, per-slot toggle). *Precedence: manifesto wins on conflicts.*
- PER_SLOT_STICKY_TOGGLE_SPEC.md §2-5 (full sticky toggle spec)
- `state_machine.py` — `transition()`, `bootstrap_orders()`, `EngineConfig`, all action types
- `types.py` — `SlotRuntime`, `SlotAccount`, `WorkBank`
- `ledger.py` — `Ledger` class interface
- `capacity.py` — `CapacityTracker` interface

**Work**:
1. **SlotEngine class**:
   - Constructor: initial slots from config (`INITIAL_SLOTS`, `INITIAL_STICKY_RATIO`)
   - `add_slot(sticky: bool) -> SlotRuntime` — check capacity, bootstrap state, allocate ID
   - `remove_slot(slot_id: int)` — cancel all orders (returns actions), archive
   - `toggle_sticky(slot_id: int)` — flip flag, no immediate state change
   - `pause_slot(slot_id: int)` — toggle paused flag
   - `write_off_exit(slot_id: int, market_price: float)` — manual sticky write-off:
     cancel exit on Kraken, record in ledger, state → S0
   - `get_slot(slot_id) -> SlotRuntime`
   - `all_slots() -> dict[int, SlotRuntime]`
2. **`build_engine_config(slot, governor_actions) -> EngineConfig`**:
   - Per-slot `sticky_mode_enabled` from `slot.sticky`
   - Per-slot `s1_orphan_after_sec` from governor TTL
   - Per-slot `entry_pct_a` / `entry_pct_b` from governor entry spacing bias
   - Per-slot `profit_pct` from governor profit target modulation (via `profit_pct_runtime`)
   - Everything else from global config
3. **`process_tick(slot, price, now) -> list[Action]`**:
   - Build config via `build_engine_config()`
   - Call `transition(slot.state, PriceTick(price, now), cfg, order_size)`
   - Call `transition(state, TimerTick(now), cfg, order_size)`
   - Return combined actions
4. **`process_fill(slot, fill_event) -> list[Action]`**:
   - Call `transition(slot.state, fill_event, cfg, order_size)`
   - Classify fill → call appropriate ledger method
   - Handle `BookCycleAction` → `ledger.book_cycle_profit()`
   - Handle `OrphanOrderAction`:
     - Sticky: keep recovery order (lottery ticket)
     - Cycle: cancel recovery on Kraken, `ledger.record_write_off()`
   - Return actions for Kraken execution
5. **`execute_actions(slot, actions, adapter)`**:
   - `PlaceOrderAction` → `adapter.place_order()`, apply txid to state
   - `CancelOrderAction` → `adapter.cancel_order()`
   - `OrphanOrderAction` → per-slot sticky/cycle handling
   - `BookCycleAction` → ledger booking
6. **Recovery TTL enforcement** (for cycle slots):
   - Each tick: check `now - recovery.orphaned_at > governor_actions.recovery_ttl`
   - If expired: cancel on Kraken, feed `RecoveryCancelEvent` through `transition()`
   - This is the plumbing the manifesto's dynamic TTL needs
7. **Auto-slot creation**:
   - Track total profit across all slots
   - When `total_profit >= SLOT_PROFIT_THRESHOLD * (current_count + 1)`: suggest new slot
   - Governor decides sticky vs cycle for new slot
8. **Snapshot persistence**:
   - `save_snapshot() -> dict` — all slot states + sticky flags + accounts
   - `load_snapshot(data: dict)` — restore from dict
   - File: `logs/state.json`

**Tests**:
- `test_add_slot_sticky_default` — new slot is sticky, bootstrapped to S0
- `test_add_slot_cycle` — new slot with sticky=False
- `test_add_slot_exceeds_capacity` — raises or returns None when headroom=0
- `test_toggle_sticky_live_slot` — flip flag, verify EngineConfig changes
- `test_nonsticky_orphan_cancels_recovery` — cycle slot: recovery cancelled, ledger write-off
- `test_sticky_orphan_keeps_recovery` — sticky slot: recovery kept
- `test_recovery_ttl_enforcement` — recovery older than TTL → cancelled
- `test_recovery_ttl_respects_governor` — short TTL in ranging, long in trending
- `test_build_engine_config_per_slot` — sticky/cycle gets correct EngineConfig
- `test_process_tick_returns_actions` — price tick → PlaceOrderAction for fresh entries
- `test_process_fill_books_profit` — exit fill → BookCycleAction → ledger entry
- `test_write_off_exit_manual` — manual write-off for sticky slot works
- `test_snapshot_roundtrip` — save → load → all slots identical
- `test_auto_slot_suggestion` — profit threshold triggers suggestion

**Acceptance**: All tests green. Cycle slots never accumulate recovery orders beyond TTL. Sticky slots never auto-orphan. Snapshot roundtrip is lossless.

---

### Agent H: `governor.py`

```
Depends on:  types.py (all governor types), state_machine.py types, config.py
Parallel:    G (slot_engine)
Output:      governor.py (~800 lines), tests/test_governor.py (~500 lines)
```

**Context to provide agent**:
- Manifesto §3 (full governor spec — ratio, TTL, stuckness, visibility)
- V2_CLEAN_SLATE_SPEC §5 (governor — orphan pressure, auto-close, plugin). *Precedence: manifesto wins on conflicts (especially §3 governor spec).*
- `types.py` for `GovernorActions`, `FactoryTelemetry`, `FillTimeStats`, `MarketCharacter`, `WorkBank`, `SlotRuntime`, `GovernorPlugin`
- Note: governor operates on slot *interfaces* only (typed Protocol), not slot_engine internals

**Work**:
1. **`compute_factory_telemetry(slots) -> FactoryTelemetry`**:
   - Exit age distribution (median, p90, p95) from open exit orders
   - Distance-to-market for all open exits
   - Orphan rate per hour (from ledger write-off count in rolling window)
   - Write-off cost per hour
   - Headroom utilization
   - Fill-time stats from completed cycles (rolling window)
   - Per-side breakdowns (fill_time_stats_a, fill_time_stats_b)
2. **`compute_orphan_pressure(slots) -> float`**:
   - From manifesto §3.2: `recovery_count / (cycle_slot_count * MAX_RECOVERY_SLOTS)`
   - Returns 0.0 if no cycle slots
3. **`compute_stuck_score(slot, fill_time_stats, market_price) -> float`**:
   - From manifesto §3.4: `0.6 * age_score + 0.4 * distance_score`
   - S0 slots → 0.0
   - Capped at 3.0
4. **`compute_recovery_ttl(market_character, fill_time_stats, headroom) -> float`**:
   - From manifesto §3.3: base=p50, ranging=0.5x, mild=1.0x, strong=2.0x
   - Capacity override: headroom < CAUTION → max 300s
   - Clamped to [MIN_RECOVERY_TTL, MAX_RECOVERY_TTL]
5. **Ratio governor (reactive + predictive)**:
   - Reactive: orphan pressure → ratio suggestion per manifesto §3.2 table
   - Predictive: market character → ratio bias per manifesto §3.2 table
   - Predictive is bias only — telemetry is boss, scanner is hint
   - Output: suggestion string (for dashboard toast) or None
6. **Orphan timeout tuning** (for cycle slots):
   - Ranging → shorter `s1_orphan_after_sec` (exits should fill fast or not at all)
   - Mild trend → standard
   - Strong trend → longer (give exits more time)
7. **Entry spacing bias** (uses `entry_pct_a` / `entry_pct_b`):
   - Strong trend up → slightly widen `entry_pct_a` (sell entries, against-trend)
   - Strong trend down → slightly widen `entry_pct_b` (buy entries, against-trend)
   - Ranging → symmetric (both use `entry_pct`)
8. **Profit target modulation** (optional, from ChatGPT's Lever #3):
   - When exits are dragging (p50 > 2x baseline) → nudge `profit_pct` down slightly
   - When exits are filling fast (p50 < 0.5x baseline) → nudge up slightly
   - Range: `[profit_pct * 0.7, profit_pct * 1.3]` — never more than 30% adjustment
9. **`evaluate(slots, telemetry, market_character, capacity) -> GovernorActions`**:
   - Assembles all the above into a single `GovernorActions` struct
10. **`post_tick(slots, capacity)`**:
    - In autonomous mode: auto-convert slots per ratio suggestion
    - In suggest mode: no-op (suggestions shown in dashboard)
11. **`GovernorPlugin` support**:
    - Accept list of plugins
    - Call `plugin.recommend_ratio()` and `plugin.recommend_close()`
    - Merge plugin suggestions with built-in logic (built-in takes priority)

**Tests**:
- `test_orphan_pressure_no_cycle_slots` → 0.0
- `test_orphan_pressure_half_full` → 0.5
- `test_orphan_pressure_all_full` → 1.0
- `test_stuck_score_s0_is_zero` → 0.0
- `test_stuck_score_stale_exit` → positive value
- `test_stuck_score_capped_at_3` → doesn't exceed 3.0
- `test_recovery_ttl_ranging` → short (p50 * 0.5)
- `test_recovery_ttl_strong_trend` → long (p50 * 2.0)
- `test_recovery_ttl_capacity_override` → capped at 300s when headroom tight
- `test_ratio_suggestion_healthy` → may suggest sticky→cycle
- `test_ratio_suggestion_elevated` → suggests cycle→sticky
- `test_ratio_suggestion_critical` → force-convert (autonomous mode)
- `test_predictive_bias_ranging` → favors cycle
- `test_predictive_bias_trending` → favors sticky
- `test_predictive_is_hint_not_command` → low pressure + trending = no force-convert
- `test_entry_spacing_bias_trend_up` → wider entry_pct_a
- `test_entry_spacing_bias_ranging` → symmetric
- `test_profit_modulation_slow_fills` → nudge down
- `test_profit_modulation_fast_fills` → nudge up
- `test_evaluate_assembles_all` → GovernorActions has all fields populated
- `test_plugin_recommendation_merged` → plugin suggestion appears if built-in is None

**Acceptance**: All tests green. Governor never suppresses a side (no test should show entry_pct_a=0 or entry_pct_b=0). Dynamic TTL varies correctly with market character.

---

## 5. Wave 3 — UI + Server (5 Parallel Agents)

Wave 3 needs: types.py + status_payload.json fixture + Wave 1 modules.
UI agents (I, J, K2) can start with mock JSON alone. Server (K) needs Wave 1+2 modules.

---

### Agent I: `factory_view.py`

```
Depends on:  fixtures/status_payload.json, types.py (event taxonomy)
Parallel:    J, K, K2, K3
Output:      factory_view.py (~5,000 lines)
```

**Context to provide agent**:
- V1's `factory_viz.py` (5,043 lines) — the source to port
- Manifesto §5 (features: factory is primary, Bauhaus overlay, art modes)
- FACTORY_LENS_SPEC.md — the original factory design spec
- `fixtures/status_payload.json` — the v2 status payload shape
- Key changes: strip DOGE references, add [S]/[C] badges, add governor integration (orphan pressure gauge, market character hue, stuck score gradients), add commentator ticker area

**Work**:
1. Port Factory render mode from v1, consuming v2 status payload shape
2. Port Bauhaus render mode from v1, same payload adaptation
3. Strip all "DOGE" string literals → use `status.base_asset`
4. Add slot type badges: [S] blue border (sticky), [C] orange border (cycle)
5. Add governor visuals per manifesto §3.5:
   - Orphan pressure gauge in status bar
   - Market character → background hue shift (teal=ranging, warm=trending)
   - Recovery TTL countdown on recovery order sprites
   - Stuck score → machine color gradient (green → amber → red)
   - Ratio suggestion → toast notification
   - Capacity headroom bar with color bands
6. Add commentator ticker area at bottom (receives SSE messages)
7. `b` key toggles Factory ↔ Bauhaus (existing behavior)
8. Remove all v1-specific payload references (rangers, churners, herd, etc.)

**Acceptance**: Factory renders with mock status JSON. Bauhaus toggles. [S]/[C] badges visible. Governor gauge visible. No "DOGE" hardcoded. Commentator area present.

**Note**: This is the hardest porting task. The JS data bindings in v1's factory_viz.py are deeply coupled to v1's status payload shape. Every `status.slots[i].field` reference must be audited and remapped. Budget extra time here.

---

### Agent J: `dashboard.py`

```
Depends on:  fixtures/status_payload.json, fixtures/action_api.md
Parallel:    I, K, K2, K3
Output:      dashboard.py (~1,500 lines)
```

**Context to provide agent**:
- V1's `dashboard.py` (4,845 lines) — strip and rebuild
- Manifesto §1 locked: "Dashboard = settings panel, accessible from factory"
- DASHBOARD_UX_SPEC.md — VIM-style keyboard navigation
- `fixtures/status_payload.json` — v2 payload shape
- `fixtures/action_api.md` — action API contract
- Panels to keep: Summary, Slots table, Selected Slot detail, Capacity, Governor, Ledger, Controls

**Work**:
1. Strip all DOGE branding, rangers panel, churners panel, HMM regime display, AI advisor panel, DCA panel, concept animation modals
2. Core panels per V2_CLEAN_SLATE_SPEC §7.2 (*manifesto wins on conflicts*):
   - Summary: price, mode, uptime, aggregate P&L, fees
   - Slots: table with ID, alias, [S]/[C], phase, profit, cycles, stuck score
   - Selected Slot: orders, recovery orders, recent cycles, ledger entries
   - Capacity: headroom, band, work bank
   - Governor: ratio, orphan pressure, suggestions with approve/dismiss
   - Ledger: recent entries, reconciliation status, drift badge
   - Controls: add slot (sticky/cycle), pause/resume, write-off button, config
3. VIM-style keyboard from DASHBOARD_UX_SPEC:
   - 4 modes: NORMAL, COMMAND, HELP, CONFIRM
   - Slot navigation: `1-9`, `[/]`, `gg`, `G`
   - Actions: `p`, `+`, `-`, `.`, `?`, `:`
   - Command bar with history
4. Sticky slot write-off button:
   - Only visible when sticky slot is in S1 with stale exit
   - Requires CONFIRM mode ("Write off Slot #N exit? y/n")
   - Calls `POST /api/action {action: "write_off", slot_id: N}`
5. Governor suggestion cards:
   - Approve/Dismiss buttons
   - Calls `POST /api/action {action: "approve_suggestion", suggestion_id: ...}`
6. Toggle between dashboard and factory: link/button to `/factory`

**Acceptance**: Dashboard renders with mock status JSON. All panels populated. Keyboard navigation works. Write-off button triggers confirm flow. No DOGE branding. No dead v1 panels.

---

### Agent K: `server.py`

```
Depends on:  All Wave 1+2 modules (for status payload assembly)
Parallel:    I, J (UI agents don't need live server — they use mock JSON)
Output:      server.py (~800 lines), tests/test_server.py (~300 lines)
```

**Context to provide agent**:
- V1's `bot.py` HTTP server section (~lines 16000-17500) for reference
- `fixtures/status_payload.json` — the target shape
- `fixtures/action_api.md` — action routes
- `types.py` for all shared types
- Manifesto §4.3 main loop (server broadcasts after each tick)

**Work**:
1. **HTTP server** (async — stdlib `asyncio` with `asyncio.start_server` + manual HTTP/SSE handling, no `aiohttp`):
   - `GET /` → redirect to `/factory`
   - `GET /factory` → serve `factory_view.py` HTML
   - `GET /dashboard` → serve `dashboard.py` HTML
   - `GET /api/status` → JSON status payload
   - `POST /api/action` → action dispatch (see action_api.md)
   - `GET /api/events` → SSE stream
   - `GET /api/ledger?slot_id=N&limit=50` → ledger entries
2. **`build_status_payload(slots, governor, scanner, capacity, ledger) -> dict`**:
   - Assembles the full status JSON from all modules
   - Shape must match `fixtures/status_payload.json` exactly
3. **SSE stream**:
   - Broadcasts on every main loop tick
   - Events: `status_update` (full payload), `commentator` (text message), `sound` (event type for audio)
   - Client-side: `EventSource('/api/events')` in factory/dashboard JS
4. **Action dispatch**:
   - `toggle_sticky` → `slot_engine.toggle_sticky()`
   - `add_slot` → `slot_engine.add_slot()`
   - `remove_slot` → `slot_engine.remove_slot()`
   - `pause_slot` → `slot_engine.pause_slot()`
   - `write_off` → `slot_engine.write_off_exit()`
   - `soft_close` → cancel entries, let exits fill
   - `set_config` → update runtime config
   - `approve_suggestion` → forward to governor
5. **Commentator engine**:
   - Generates plain-English messages for bot events
   - Template-based: `"Slot #{id} entry filled: bought {vol} {base} at ${price} (Trade {tid}, cycle {c})"`
   - Stores last 50 messages in rolling buffer
   - Served via SSE and in status payload

**Tests**:
- `test_status_payload_shape` — matches fixture schema
- `test_action_toggle_sticky` — POST returns ok, slot flag flipped
- `test_action_add_slot` — creates new slot
- `test_action_write_off` — calls slot_engine.write_off_exit
- `test_sse_broadcasts_on_tick` — connected client receives status
- `test_commentator_formats_messages` — known events → expected text

**Acceptance**: `GET /api/status` returns valid JSON matching fixture shape. Actions work. SSE delivers events. Commentator generates readable messages.

---

### Agent K2: `audio.py`

```
Depends on:  types.py (event taxonomy only)
Parallel:    I, J, K, K3
Output:      audio.py (~200 lines)
```

**Context to provide agent**:
- Manifesto §5.2 (sound design table)
- `types.py` for `BOT_EVENTS` list

**Work**:
1. Pure JavaScript module (embedded in factory_view or loaded as separate file)
2. Web Audio API procedural sound generation:
   - `cycle_complete` → cash register ka-ching (short burst, high freq harmonics)
   - `entry_fill` → soft click (single short tone)
   - `orphan_created` → low thunk (low freq, quick decay)
   - `s2_entered` → warning tone (two-tone, rising)
   - `s2_resolved` → relief chord (major chord, slow release)
   - `write_off` → glass break (noise burst, high freq, fast decay)
   - `recovery_fill` → slot machine jackpot (ascending arpeggio)
   - `governor_ratio_change` → foreman whistle (sine sweep up)
3. Each sound: oscillator + gain envelope, ~10-15 lines each
4. Master volume control, mute toggle
5. Triggered by SSE events: `EventSource` receives `sound` event type, dispatches to synth

**Acceptance**: Each event type produces a distinct, short sound. Master mute works. No audio files (all procedural). Total JS is under 200 lines.

---

### Agent K3: Commentator Templates

```
Depends on:  types.py (event taxonomy), fixtures/status_payload.json
Parallel:    I, J, K, K2
Output:      Commentator message templates (goes into server.py or separate module)
```

**Context to provide agent**:
- Manifesto §5.3 (commentator ticker examples)
- `types.py` for event types and shared types

**Work**:
1. Template function per event type:
   ```python
   def format_event(event_type: str, context: dict) -> str:
       match event_type:
           case "entry_fill":
               return f"Slot #{context['slot_id']} entry filled: {context['side']} {context['volume']} {context['base']} at ${context['price']:.5f} (Trade {context['trade_id']}, cycle {context['cycle']})"
           case "cycle_complete":
               return f"Slot #{context['slot_id']} exit filled! Profit: ${context['profit']:.2f} after fees. Cycle {context['cycle']} complete."
           # ... etc
   ```
2. Governor messages:
   - `"Foreman: \"Market {regime} — extending recovery TTL to {ttl} min\""`
   - `"Foreman: \"Orphan pressure at {pressure:.2f} — suggesting convert {n} cycle → sticky\""`
3. Stale exit warnings:
   - `"Slot #{id} exit getting stale ({age} min, median is {median} min) — stuck score: {score:.1f}"`
4. Write-off messages:
   - `"Slot #{id} exit written off. Loss: ${loss:.2f}. Fresh entry placed."`

**Acceptance**: Every event type has a template. Messages match the examples in manifesto §5.3. All templates tested with sample context dicts.

---

## 6. Wave 4 — Features (4 Parallel Agents)

Wave 4 needs core (Waves 1-2) complete. All 4 agents run in parallel.

---

### Agent L: `simulator.py`

```
Depends on:  state_machine.py, slot_engine.py interface, scanner.py interface, config.py
Parallel:    M, N, O
Output:      simulator.py (~500 lines), tests/test_simulator.py (~300 lines)
```

**Context to provide agent**:
- Manifesto §5.1 (simulation mode spec)
- `types.py` for `ExchangeAdapter` protocol
- `state_machine.py` for transition function

**Work**:
1. **Synthetic price generator**:
   - Geometric Brownian Motion: `price *= exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)`
   - Configurable: `volatility`, `trend` (drift), `seed` (deterministic)
   - Generates 1-second resolution, sampled at configured speed
2. **MockOrderBook**:
   - Holds open limit orders (side, price, volume, txid)
   - On each price tick: check if any order crosses → generate `FillEvent`
   - No partial fills (simplification for v2.0)
   - Synthetic fees at configured `MAKER_FEE_PCT`
3. **SimulatedAdapter** (implements `ExchangeAdapter`):
   - `fetch_price()` → next synthetic price
   - `fetch_ohlcv()` → build candles from synthetic ticks
   - `place_order()` → add to MockOrderBook, return synthetic txid
   - `cancel_order()` → remove from MockOrderBook
   - `get_balance()` → simulated balances (track USD + base)
4. **Speed control**: 1x (real-time), 5x, 10x, 100x — controls tick interval
5. **CLI interface**: `python main.py --simulate --volatility=1.2 --hours=24 --speed=10x --seed=42`
6. **Output**: Full factory renders at chosen speed. Status payload updated each tick. Ledger records simulated trades. Summary printed at end (total profit, cycles, write-offs).

**Tests**:
- `test_price_generator_deterministic` — same seed → same prices
- `test_price_generator_trend` — positive trend → price rises on average
- `test_mock_order_book_fill` — buy at $0.09, price drops to $0.089 → fill
- `test_mock_order_book_no_fill` — buy at $0.09, price stays $0.091 → no fill
- `test_simulated_adapter_protocol` — passes ExchangeAdapter check
- `test_simulated_balance_tracking` — buy order → USD decreases, base increases
- `test_full_simulation_completes` — 1h sim with 2 slots runs without error

**Acceptance**: `python main.py --simulate --hours=1 --speed=100x --seed=42` completes in <5 seconds, prints summary. Deterministic: same seed produces same result.

---

### Agent M: `diagnostic.py`

```
Depends on:  types.py (all types for snapshot), config.py
Parallel:    L, N, O
Output:      diagnostic.py (~500 lines), tests/test_diagnostic.py (~200 lines)
```

**Context to provide agent**:
- Manifesto §5.5 (full AI diagnostic console spec — snapshot, templates, suggest+confirm)
- `types.py` for all shared types
- `fixtures/status_payload.json` for snapshot shape reference

**Work**:
1. **`build_diagnostic_snapshot(slots, governor, scanner, capacity, ledger) -> dict`**:
   - Serializes full system state per manifesto §5.5 snapshot section
   - Structured for LLM consumption (~500 tokens)
2. **Prompt template library**:
   - `daily_digest` → health summary + concerns + suggestions
   - `health_check` → anomaly detection
   - `stuck_analysis` → per-slot stuck exit analysis
   - `ratio_review` → sticky/cycle ratio appropriateness
   - `performance` → profit breakdown, efficiency
   - `whatif` → scenario analysis (user provides scenario text)
   - `explain` → plain-English current state explanation
3. **Each prompt**: system context (~200 tokens) + snapshot (~500 tokens) + question (~100 tokens) + response format instruction
4. **DeepSeek API client**:
   - Plain `urllib.request` (no httpx dependency)
   - `call_deepseek(messages: list[dict], model: str) -> str`
   - Timeout handling, retry once on network error
   - Rate limiting (track calls per hour)
5. **Response parser**:
   - Extract: assessment (text), concerns (list), suggested actions (list)
   - Each action: `{action: str, params: dict, explanation: str}`
   - Map to valid `/api/action` payloads
6. **DiagnosticEngine class**:
   - `analyze(template: str, **kwargs) -> DiagnosticResult`
   - `daily_digest()` → scheduled, returns formatted result
   - Caches snapshot (rebuilt each main loop, not per query)

**Tests**:
- `test_snapshot_shape` — snapshot has all required keys
- `test_snapshot_token_estimate` — serialized snapshot < 1000 tokens
- `test_prompt_templates_all_exist` — every template name maps to a function
- `test_response_parser_valid_json` — parses known DeepSeek response format
- `test_action_mapper_toggle_sticky` — AI suggestion → valid action payload
- `test_rate_limiting` — 11th call in 1 hour → blocked
- `test_degradation_no_api_key` — returns "unavailable" message, no crash

**Acceptance**: All tests green. Snapshot builds from mock data. Prompts are well-formed. Response parser handles both clean and malformed LLM output gracefully. **Non-negotiable #9 verified**: no code path in `diagnostic.py` directly modifies `PairState`, calls `transition()`, or places Kraken orders. All actionable suggestions are expressed as `/api/action` payloads only.

---

### Agent N: `telegram.py`

```
Depends on:  diagnostic.py interface, server.py action API, config.py
Parallel:    L, M, O
Output:      telegram.py (~400 lines), tests/test_telegram.py (~150 lines)
```

**Context to provide agent**:
- Manifesto §5.5 (Telegram section — commands, digest, confirm flow)
- `fixtures/action_api.md` for action API
- Note: use `python-telegram-bot` library (async mode, runs on same `asyncio` event loop as main)
- Single authorized `TELEGRAM_CHAT_ID` only

**Work**:
1. **Bot setup**:
   - `TelegramBot` class with `start()` / `stop()`
   - Authorization: only respond to `TELEGRAM_CHAT_ID`
   - Rate limiting: 10 queries/hour
2. **Command handlers**:
   - `/check` → `diagnostic.health_check()` → formatted message
   - `/stuck` → `diagnostic.stuck_analysis()` → formatted message
   - `/ratio` → `diagnostic.ratio_review()` → formatted message
   - `/perf` → `diagnostic.performance()` → formatted message
   - `/whatif <text>` → `diagnostic.whatif(text)` → formatted message
   - `/explain` → `diagnostic.explain()` → formatted message
3. **Daily digest scheduler**:
   - Configurable hour UTC (`DIAGNOSTIC_DIGEST_HOUR_UTC`)
   - Calls `diagnostic.daily_digest()`, sends to chat
4. **Suggest + confirm flow**:
   - When diagnostic returns actionable suggestions, format as inline buttons
   - `[Approve #1]` `[Approve #2]` `[Dismiss All]`
   - On approve: call `POST http://localhost:{port}/api/action` with mapped payload
   - Confirm result: "Done. Slot 4, 7, 11 converted to sticky."
5. **Message formatting**:
   - Telegram MarkdownV2 or HTML formatting
   - Health/performance summaries with emoji indicators

**Tests** (mock Telegram API, mock diagnostic):
- `test_unauthorized_user_ignored` — different chat_id → no response
- `test_check_command` → calls diagnostic.health_check, sends message
- `test_suggest_confirm_flow` → approve button → action API called
- `test_rate_limiting` → 11th query → "Rate limited" message
- `test_no_api_key_graceful` → TELEGRAM_ENABLED=False → bot doesn't start

**Acceptance**: All tests green with mocked Telegram API. Commands dispatch to correct diagnostic methods. Confirm flow calls correct action API endpoint. **Non-negotiable #9 verified**: no code path in `telegram.py` directly modifies `PairState`, calls `transition()`, or places Kraken orders. All confirmed actions go through `POST /api/action` on the bot's own HTTP server.

---

### Agent O: `main.py`

```
Depends on:  ALL modules (this is the integration point)
Parallel:    L, M, N (main.py can be started while features are in progress —
             wire core first, add features as they complete)
Output:      main.py (~500 lines)
```

**Context to provide agent**:
- Manifesto §4.3 (9-step main loop)
- All module interfaces from `types.py`
- All module stubs from Phase 0

**Work**:
1. **CLI argument parsing**:
   - `--simulate` → simulation mode (no API keys needed)
   - `--volatility=N` → for simulator
   - `--hours=N` → simulation duration
   - `--speed=Nx` → simulation speed
   - `--seed=N` → deterministic simulation
   - `--port=N` → server port override
   - `--dry-run` → connect to Kraken but don't place orders
2. **Initialization sequence**:
   - Load config from env
   - Initialize state_machine (bootstrap or load snapshot)
   - Initialize ledger (load from JSONL)
   - Initialize slot_engine (load snapshot or create initial slots)
   - Initialize capacity tracker
   - Initialize scanner (load candle store)
   - Initialize governor
   - Initialize server (start HTTP + SSE)
   - Initialize diagnostic + telegram (if enabled)
   - If `--simulate`: initialize simulator instead of kraken_adapter
3. **Main loop** (9 steps from manifesto §4.3):
   ```python
   while running:
       # 1. Price + candles
       price = await adapter.fetch_price()
       candles = await adapter.fetch_ohlcv()
       # 2. Scanner update
       if scanner.should_update(now):
           market_character = scanner.update(candles)
       # 3. Capacity check
       capacity = capacity_tracker.update(await adapter.get_open_orders_count(), recovery_count)
       # 4. Governor decisions
       telemetry = governor.compute_factory_telemetry(slots)
       governor_actions = governor.evaluate(slots, telemetry, market_character, capacity)
       # 5. Per-slot transitions
       for slot in slot_engine.all_slots().values():
           actions = slot_engine.process_tick(slot, price, now)
           await slot_engine.execute_actions(slot, actions, adapter)
       # 6. Fill detection + reconciliation
       fill_occurred = await poll_fills(adapter, slot_engine, ledger)
       # Reconcile every 5th cycle or on fill — saves ~2,500 private API calls/day
       if fill_occurred or loop_count % 5 == 0:
           ledger.reconcile(slots, await adapter.get_balance())
       # 7. Governor post-tick
       governor.post_tick(slots, capacity)
       # 8. Persist
       slot_engine.save_snapshot()
       # 9. Broadcast
       server.broadcast(build_status_payload(...))
       await asyncio.sleep(POLL_INTERVAL_SECONDS)
   ```
4. **Graceful shutdown**: SIGINT/SIGTERM → stop loop, save snapshot, cancel open orders optionally
5. **Startup reconciliation**: compare snapshot vs Kraken open orders, cancel strays
6. **Error handling**: per-tick try/except, log errors, continue loop
7. **Boot sequence**: `python main.py` → opens browser to `http://localhost:{port}/factory`

**Acceptance**: `python main.py --simulate --hours=1 --speed=100x` runs complete simulation with all modules wired. `python main.py --dry-run` connects to Kraken, fetches price, shows factory. No crashes on missing optional features (audio off, telegram off, diagnostic off).

---

## 7. Wave 5 — Integration + Migration (2 Agents)

---

### Agent P: Integration Testing

```
Depends on:  All modules complete
Output:      tests/test_integration.py (~500 lines)
```

**Work**:
1. **Full lifecycle test**: Create 2 slots (1 sticky, 1 cycle), simulate 50 ticks with fills, verify:
   - Ledger balances
   - Cycle completion
   - Orphan handling (cycle slot orphans, sticky slot doesn't)
   - Recovery TTL enforcement
   - Governor ratio suggestion
   - Status payload shape
2. **Snapshot roundtrip**: Full state → save → load → compare
3. **Simulation regression**: `--simulate --seed=42 --hours=1` produces deterministic output
4. **Capacity limits**: 84 sticky slots → headroom=0 → add_slot fails
5. **Governor stress**: All cycle slots, orphan pressure → 1.0, governor suggests mass conversion
6. **Simulator → Scanner → Governor pipeline**: Use simulator's synthetic price generator to produce multi-regime candle data (ranging segment → trending segment → ranging). Feed through scanner, verify MarketCharacter transitions. Feed MarketCharacter through governor, verify: dynamic recovery TTL shortens in ranging and lengthens in trending, ratio bias shifts toward cycle in ranging and sticky in trending, entry spacing bias activates in strong trend. This is the end-to-end test for the governor's predictive path — it can't be tested with hand-crafted test candles alone.
7. **Reconciliation throttle**: Verify `ledger.reconcile()` is called only on fill events or every 5th cycle, not every tick.

---

### Agent Q: `migrate_v1_state.py`

```
Depends on:  slot_engine.py, ledger.py (to write v2 format)
Output:      migrate_v1_state.py (~300 lines)
```

**Work**:
1. Read v1's `logs/state.json`
2. Extract `PairState` per slot (same frozen dataclass — direct port)
3. Create `SlotRuntime` for each slot (sticky=True by default for migration)
4. Initialize `SlotAccount` from v1's `total_profit` float (best effort)
5. Create initial ledger entries for existing open positions
6. Write v2 `logs/state.json` + `logs/ledger.jsonl`
7. Verify: load v2 state, check invariants, print summary

---

## 8. Agent Concurrency Summary

| Wave | Agents | Calendar Days | Can Start After |
|------|--------|---------------|-----------------|
| Phase 0: Scaffold | 1 | 1 | — |
| Wave 1: Foundation | 6 (A-F) | 3 | Phase 0 |
| Wave 2: Core | 2 (G, H) | 3 | Wave 1 |
| Wave 3: UI + Server | 5 (I, J, K, K2, K3) | 4 | Wave 1 + status payload schema |
| Wave 4: Features | 4 (L, M, N, O) | 4 | Wave 2 |
| Wave 5: Integration | 2 (P, Q) | 2 | Wave 4 |
| **Total** | **20 agents** | **~17 calendar days** | |

**Peak parallelism**: 6 agents (Wave 1)

**Note**: Waves 2 and 3 can overlap. Wave 3 UI agents (I, J, K2, K3) only need the status payload JSON schema, not actual running modules. So they can start as soon as Phase 0 completes, running in parallel with Wave 1.

**Optimized timeline with overlap**:

```
Day  1:  Phase 0 (scaffold)
Day  2-4:  Wave 1 (6 agents) + Wave 3 UI (agents I, J, K2, K3) ← 10 agents parallel
Day  5-7:  Wave 2 (2 agents) + Wave 3 server (agent K)
Day  6-9:  Wave 4 (4 agents)
Day 10-11: Wave 5 (2 agents)

Optimized total: ~11-12 calendar days
```

---

## 9. Critical Path

The longest sequential dependency chain determines minimum calendar time:

```
Phase 0 → config.py → ledger.py → slot_engine.py → governor.py → server.py → main.py → integration tests
  D1         D2          D2-3         D4-5            D5-6          D6-7        D7-9        D10-11
```

**Critical path: ~11 days** (assuming each step is 1-2 days with overlap).

Everything else (scanner, adapter, factory, dashboard, audio, simulator, diagnostic, telegram) is off the critical path and runs in parallel.

---

## 10. Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Factory JS rewrite scope underestimated | Agent I gets extra context on v1→v2 payload differences; budget 4-5 days instead of 3 |
| HMM from scratch with numpy | Agent D gets v1's `hmm_regime_detector.py` as reference; accept simpler model (fewer iterations) for v2.0 |
| `python-telegram-bot` dependency weight | Locked: `python-telegram-bot` is an approved dependency. It handles polling, async integration, inline keyboards, and retries. Not revisitable. |
| Async event loop complexity | Manifesto locks async (§4.3). Use `asyncio` throughout. Kraken adapter wraps `urllib.request` via `loop.run_in_executor`. HTTP server uses `asyncio.start_server` (stdlib). Telegram bot uses `python-telegram-bot` async mode on same loop. **No `aiohttp`** — three approved deps only: `numpy`, `python-telegram-bot`, `sqlite3` (stdlib). |
| Scanner cold start (21 days for 500 candles) | Agent L's simulator generates synthetic candles for governor testing. Seed candle utility for live cold start. |
| Reconciliation rate-limit cost | Reconcile every 5th cycle (2.5 min) or on fill events only, not every 30s tick |

---

## 11. Handoff Protocol

Each agent receives:
1. **This document** (their specific agent section)
2. **`types.py`** (shared interface contracts)
3. **`state_machine.py`** (unchanged foundation)
4. **`fixtures/status_payload.json`** (for UI agents)
5. **`fixtures/action_api.md`** (for server/dashboard/telegram agents)
6. **Relevant specs** (manifesto sections, clean-slate spec sections, as listed per agent)
7. **V1 source file** (if porting — e.g., Agent E gets `kraken_client.py`, Agent I gets `factory_viz.py`)

Each agent returns:
1. **Implemented module** (complete, linted, type-hinted)
2. **Tests** (all passing)
3. **Integration notes** (any deviations from `types.py` contract, any new types needed)

---

## 12. Definition of Done

v2 is shippable when:

- [ ] `python main.py --simulate --hours=1 --speed=100x` completes with factory rendering
- [ ] `python main.py --dry-run` connects to Kraken, shows factory with real price
- [ ] `python main.py` trades live with 2 slots (1 sticky, 1 cycle)
- [ ] Factory view renders all governor visuals
- [ ] Bauhaus overlay toggles with `b`
- [ ] Dashboard shows all panels, keyboard navigation works
- [ ] Ledger reconciles within 0.1% of Kraken balance
- [ ] 84 sticky slots run without hitting capacity
- [ ] Cycle slots write off within dynamic TTL
- [ ] Governor ratio suggestions appear in dashboard + factory
- [ ] Commentator ticker narrates events
- [ ] Audio plays on events (when enabled)
- [ ] Zero "DOGE" string literals outside config defaults
- [ ] Zero personal data (API keys, usernames)
- [ ] All tests green
- [ ] `migrate_v1_state.py` converts v1 state successfully
