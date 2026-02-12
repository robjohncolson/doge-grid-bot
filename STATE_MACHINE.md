# DOGE State-Machine Bot v1

Last updated: 2026-02-12
Primary code references: `bot.py`, `state_machine.py`, `dashboard.py`, `supabase_store.py`

## 1. Scope

This document is the implementation contract for the current runtime.

- Market: Kraken `XDGUSD` (`DOGE/USD`) only
- Strategy: slot-based pair engine (`A` and `B` legs) with independent per-slot compounding
- Persistence: Supabase-first (`bot_state`, `fills`, `price_history`, `bot_events`)
- Execution: fully rule-based reducer; no AI in execution path
- Control plane: dashboard + Telegram commands

Out of scope for v1:

- Multi-pair swarm
- Factory visualization mode
- AI council / approval workflows

## 2. System Overview

Runtime is split into two layers.

- `state_machine.py`: pure reducer (`transition`) and invariant checker (`check_invariants`)
- `bot.py`: exchange I/O, reconciliation, bootstrap, loop budget, APIs, Telegram, persistence

Core data model (`PairState`) is per slot and contains:

- Active orders (`orders`)
- Orphaned exits (`recovery_orders`)
- Completed cycles (`completed_cycles`)
- Cycle counters (`cycle_a`, `cycle_b`)
- Risk counters and cooldown timers
- Mode flags (`long_only`, `short_only`)

## 3. Top-Level Lifecycle

```text
START
  -> INIT (logging, signals, Supabase writer)
  -> LOAD SNAPSHOT (Supabase key __v1__)
  -> FETCH CONSTRAINTS + FEES (Kraken)
  -> FETCH INITIAL PRICE (strict)
  -> RECONCILE TRACKED ORDERS
  -> REPLAY MISSED FILLS (trade history)
  -> ENSURE BOOTSTRAP PER SLOT
  -> RUNNING LOOP
      -> PAUSED (operator or guardrail)
      -> HALTED (invariant breach)
  -> SHUTDOWN (signal or process exit)
```

Runtime modes:

- `RUNNING`: normal operation
- `PAUSED`: no new entry placement; bot remains alive
- `HALTED`: hard stop on invariant violations

## 4. Main Loop (Every `POLL_INTERVAL_SECONDS`)

Per iteration in `bot.py`:

1. `begin_loop()` enables private API budget accounting.
2. `_refresh_price(strict=False)`.
3. If price age exceeds `STALE_PRICE_MAX_AGE_SEC`, call `pause(...)`.
4. Compute volatility-adaptive runtime profit target.
5. For each slot:
   - Apply `PriceTick`
   - Apply `TimerTick`
   - Call `_ensure_slot_bootstrapped(slot_id)`
   - Call `_auto_repair_degraded_slot(slot_id)` to restore missing entry legs when fundable
6. Poll status for all tracked order txids (`_poll_order_status`).
7. Refresh pair open-order telemetry (`_refresh_open_order_telemetry`) when budget allows.
8. Emit orphan-pressure notification at `ORPHAN_PRESSURE_WARN_AT` multiples.
9. Persist snapshot (`save_state` to `bot_state`).
10. Poll Telegram commands.
11. `end_loop()` resets budget/cache.

## 5. Pair Phases (`S0`, `S1a`, `S1b`, `S2`)

Phase is derived from order roles/sides.

- `S0`: entry phase (normal two-sided or degraded one-sided)
- `S1a`: A is in position (`buy exit` exists)
- `S1b`: B is in position (`sell exit` exists)
- `S2`: both exits pending (both positions open)

Trade semantics:

- `A`: sell entry, buy exit
- `B`: buy entry, sell exit

Fallback flags:

- `long_only=True`: only B-side entry flow should continue
- `short_only=True`: only A-side entry flow should continue

## 6. Reducer Contract

`transition(state, event, cfg, order_size_usd) -> (next_state, actions)`

Properties:

- Pure function, no network side effects
- Side effects are represented as actions:
  - `PlaceOrderAction`
  - `CancelOrderAction`
  - `OrphanOrderAction`
  - `BookCycleAction`

All exchange effects happen in runtime after reducer returns.

## 7. Event Transition Rules

### 7.1 `PriceTick`

- Updates `market_price`, `now`, `last_price_update_at`
- Runs stale-entry refresh (`_refresh_stale_entries`)
- Refresh behavior:
  - At most one entry replacement per tick
  - Triggered when entry drift exceeds `refresh_pct`
  - Anti-chase protection:
    - tracks refresh direction/count per trade
    - after `max_consecutive_refreshes`, starts cooldown (`refresh_cooldown_sec`)

### 7.2 `TimerTick`

- `S1` stale exit rule:
  - If exit age >= `S1_ORPHAN_AFTER_SEC`
  - and market moved away from the exit price
  - orphan that exit immediately (`s1_timeout`)
- `S2` timeout rule:
  - Entering `S2` sets `s2_entered_at`
  - If `now - s2_entered_at >= S2_ORPHAN_AFTER_SEC`, orphan the worse exit
  - worse exit = farther from market in percentage distance
- Leaving `S2` clears `s2_entered_at`

### 7.3 `FillEvent` for active orders

If filled order is an entry:

- Remove entry from active set
- Add opposite-side exit with:
  - exact filled volume
  - exit price via `_exit_price(...)`
- Book entry fee immediately into `total_fees`
- Emit `PlaceOrderAction` for new exit

If filled order is an exit:

- Book completed cycle (`_book_cycle`)
- Update realized loss counters and cooldown timers
- Increment cycle counter for that trade (`cycle_a` or `cycle_b`)
- Attempt follow-up entry for same trade unless blocked by:
  - fallback mode (`long_only` / `short_only`)
  - loss cooldown

### 7.4 `RecoveryFillEvent`

- Remove recovery record
- Book cycle as `from_recovery=True`
- Update loss counters

### 7.5 `RecoveryCancelEvent`

- Remove recovery record only

## 8. Bootstrap and Reseed Logic

Implemented in `_ensure_slot_bootstrapped(slot_id)`.

Precondition:

- If slot already has active orders, do nothing.

Balance source:

- Kraken balance is authoritative each check.

Branch order:

1. Both sides available (`doge >= min_volume` and `usd >= min_cost`):
   - place A sell entry + B buy entry
2. USD lacking but DOGE >= 2x minimum:
   - `short_only`, place A sell reseed at 2x min volume equivalent
3. DOGE lacking but USD >= 2x minimum cost:
   - `long_only`, place B buy reseed at 2x min volume equivalent
4. Graceful degradation (one-sided):
   - if DOGE >= minimum, place only A sell (`short_only`)
   - else if USD >= minimum cost, place only B buy (`long_only`)
5. Neither side can bootstrap:
   - `pause("slot X cannot bootstrap: insufficient USD and DOGE")`

Order-size behavior:

- `compute_order_volume(...)` never auto-upsizes.
- If target size is below Kraken minimum constraints, no action is returned and slot waits.

## 9. Invariants and Halt Policy

Primary checker: `state_machine.check_invariants(state)`.

Representative invariants:

- `S0` exact structure (two-sided or valid fallback structure)
- `S1a`/`S1b` exact structure
- `S2` exactly one buy exit + one sell exit
- cycle counters >= 1
- no duplicate local order IDs
- exits must carry `entry_price`

Runtime enforcement (`_validate_slot`):

- Called after every applied event transition.
- Violations normally cause immediate `HALTED` state.

Two explicit runtime bypasses for recoverable startup gaps:

1. Min-size wait state (`_is_min_size_wait_state`)
   - S0 violation allowed when target order size is below Kraken minimum requirements
2. Bootstrap pending state (`_is_bootstrap_pending_state`)
   - S0 violation allowed when there are no exits and <=1 entry (temporary bootstrap gap)

Hotfix behavior (2026-02-11):

- `_normalize_slot_mode(slot_id)` aligns fallback flags with actual one-sided entry state.
- Called after action execution and on placement failure/skip paths.

## 10. Execution Semantics

All order placement goes through `_place_order(...)`:

- `ordertype=limit`
- `post_only=True`
- Kraken pair fixed to runtime pair (`XDGUSD`)

Budget and failure handling:

- Private API calls are capped per loop (`MAX_API_CALLS_PER_LOOP`)
- If budget exhausted, place/cancel/query operations are skipped safely
- Open-order telemetry refresh is also budget-gated and skipped safely when budget is exhausted
- On entry placement exception containing `insufficient funds`:
  - failed sell entry -> switch to `long_only`
  - failed buy entry -> switch to `short_only`

Partial-fill telemetry (read-only diagnostics):

- During order polling, `status="open"` with `0 < vol_exec < vol` records a partial-open event (deduped per txid until terminal status)
- During order polling, `status in {"canceled","expired"}` with `vol_exec > 0` records a partial-cancel canary event and logs `PHANTOM_POSITION_CANARY ...`
- Runtime behavior is intentionally unchanged by these counters; they are exported for operator diagnostics only

Degraded self-heal behavior:

- Runtime continuously attempts to repair one-sided `S0`/`S1` slots when mode is `RUNNING`
- Repair is attempted only when the missing side is currently fundable (`USD` or `DOGE` minimum)
- On successful repair placement, `long_only`/`short_only` flags are cleared for that slot

## 11. Recovery/Orphan Lifecycle

Orphaning converts an active exit to a recovery order while keeping Kraken order alive.

- Recovery orders are stored in `recovery_orders`
- Recovery orders are not auto-cancelled by lifecycle logic
- No hard cap in runtime; pressure warning sent at configured threshold multiples

Manual soft-close:

- Dashboard `soft_close` / `soft_close_next`
- Telegram `/soft_close` interactive picker or direct args
- Soft-close reprices recovery toward market and updates txid

## 12. Reconciliation and Exactly-Once Fill Accounting

Startup reconciliation (`_reconcile_open_orders`):

- Retains tracked orders from snapshot
- Drops unbound local orders with empty txid
- Counts tracked open txids present on Kraken
- Updates pair-filtered Kraken open-order cache used by `status_payload()` capacity telemetry

Missed-fill replay (`_replay_missed_fills`):

- Builds candidate set: tracked txids not open and not already seen
- Queries `TradesHistory` (7-day window)
- Aggregates fills by `ordertxid`
- Emits synthetic fill events once per txid

Exactly-once guard:

- `seen_fill_txids` in memory + persisted snapshot
- Closed-order polling and replay both honor this set

## 13. Persistence Model

Primary snapshot key:

- `bot_state.key = "__v1__"`

Snapshot payload includes:

- runtime mode/pause reason
- global tunables (`entry_pct`, `profit_pct`)
- constraints and fee rates
- `next_slot_id`, `next_event_id`, `seen_fill_txids`
- all per-slot serialized `PairState`

Event log:

- Each transition emits `save_event(...)` row:
  - `event_id`, `timestamp`, `slot_id`, `from_state`, `to_state`, `event_type`, `details`
- Requires Supabase table `bot_events`

If Supabase tables are missing, bot logs warnings and continues running.

## 14. Dashboard and API Contract

HTTP server:

- `GET /` -> dashboard HTML
- `GET /api/status` -> runtime payload
- `GET /api/swarm/status` -> alias to `/api/status` (legacy poller compatibility)
- `POST /api/action` -> control actions

Supported dashboard actions:

- `pause`
- `resume`
- `add_slot`
- `set_entry_pct`
- `set_profit_pct`
- `soft_close`
- `soft_close_next`

### 14.1 `/api/status` Capacity & Fill Health Block

`status_payload()` includes top-level `capacity_fill_health` for manual scaling diagnostics:

- `open_orders_current`
- `open_orders_source` (`kraken` or `internal_fallback`)
- `open_orders_internal`
- `open_orders_kraken` (nullable)
- `open_orders_drift` (nullable)
- `open_order_limit_configured` (from `KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT`)
- `open_orders_safe_cap` (`limit * OPEN_ORDER_SAFETY_RATIO`, clamped to at least 1)
- `open_order_headroom`
- `open_order_utilization_pct`
- `orders_per_slot_estimate` (nullable)
- `estimated_slots_remaining`
- `partial_fill_open_events_1d`
- `partial_fill_cancel_events_1d`
- `median_fill_seconds_1d` (nullable)
- `p95_fill_seconds_1d` (nullable)
- `status_band` (`normal`, `caution`, `stop`)
- `blocked_risk_hint` (string list)

`status_band` thresholds:

- `stop` when `open_order_headroom < 10` or `partial_fill_cancel_events_1d > 0`
- `caution` when `open_order_headroom < 20` (and stop conditions are false)
- `normal` otherwise

Current `blocked_risk_hint` values:

- `kraken_open_orders_unavailable`
- `near_open_order_cap`
- `open_order_caution`
- `partial_fill_open_pressure`
- `partial_fill_cancel_detected`

## 15. Telegram Command Contract

Supported commands:

- `/pause`
- `/resume`
- `/add_slot`
- `/status`
- `/help`
- `/soft_close [slot_id recovery_id]`
- `/set_entry_pct <value>`
- `/set_profit_pct <value>`

Callback format for interactive soft-close:

- `sc:<slot_id>:<recovery_id>`

## 16. Operational Guardrails

Automatic pauses:

- stale price age > `STALE_PRICE_MAX_AGE_SEC`
- consecutive API errors >= `MAX_CONSECUTIVE_ERRORS`
- cannot bootstrap due insufficient DOGE and USD on a slot

Automatic halt:

- invariant violation not covered by explicit bootstrap/min-size bypasses

Capacity telemetry controls (operator diagnostics only):

- `KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT` (default `225`)
- `OPEN_ORDER_SAFETY_RATIO` (default `0.75`)

Signal handling:

- `SIGTERM`, `SIGINT` (and `SIGBREAK` on Windows) trigger graceful shutdown

## 17. Developer Notes

When updating behavior, update these files together:

1. `state_machine.py` for reducer semantics and invariants
2. `bot.py` for runtime side effects / bootstrap / guardrails
3. `tests/test_hardening_regressions.py` for regression coverage
4. `STATE_MACHINE.md` for contract parity

This document is intentionally code-truth first: if this file and code diverge, code wins and doc must be updated in the same change.
