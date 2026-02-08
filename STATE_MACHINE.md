# DOGE Grid Bot -- State Machine

## 1. Bot Lifecycle (Top-Level)

```
                          ┌──────────┐
                          │   START  │
                          └────┬─────┘
                               │
                          ┌────▼─────┐
                          │   INIT   │  setup logging, signals, config
                          └────┬─────┘
                               │
                          ┌────▼──────────┐
                          │  LOAD_STATE   │  restore counters from state.json
                          └────┬──────────┘
                               │
                          ┌────▼──────────┐
                          │  FETCH_PRICE  │──── fail ──── NOTIFY + EXIT
                          └────┬──────────┘
                               │ ok
                          ┌────▼──────────┐
                          │   VALIDATE    │──── fail ──── NOTIFY + EXIT
                          └────┬──────────┘
                               │ pass
                          ┌────▼──────────┐
                          │  RECONCILE    │  adopt/cancel stale Kraken orders
                          └────┬──────────┘
                               │
                          ┌────▼──────────┐
                          │  BUILD_GRID   │  place initial orders (skip adopted)
                          └────┬──────────┘
                               │
                   ┌───────────▼───────────┐
              ┌───►│      MAIN LOOP        │◄──────────────────────┐
              │    └───────────┬───────────┘                       │
              │                │                                   │
              │    ┌───────────▼───────────┐                       │
              │    │     FETCH PRICE       │── fail ─► incr errors │
              │    └───────────┬───────────┘     │                 │
              │                │ ok              │ >= MAX ──► SHUTDOWN
              │    ┌───────────▼───────────┐     │
              │    │    DAILY RESET?       │     │
              │    └───────────┬───────────┘     │
              │                │                 │
              │    ┌───────────▼───────────┐     │
              │    │    CHECK RISK         │─────┤
              │    └───────────┬───────────┘     │
              │                │ ok              │ stop_floor ──► SHUTDOWN
              │                │                 │ daily_limit ─► PAUSED
              │    ┌───────────▼───────────┐     │
              │    │    CHECK FILLS        │     │
              │    └───────────┬───────────┘     │
              │                │                 │
              │    ┌───────────▼───────────┐     │
              │    │    CHECK DRIFT        │     │
              │    └───────────┬───────────┘     │
              │                │                 │
              │    ┌───────────▼───────────┐     │
              │    │    AI / CALLBACKS     │     │
              │    └───────────┬───────────┘     │
              │                │                 │
              │    ┌───────────▼───────────┐     │
              │    │    ACCUMULATION       │     │
              │    └───────────┬───────────┘     │
              │                │                 │
              │    ┌───────────▼───────────┐     │
              │    │    SLEEP             │──────┘
              │    └───────────┬───────────┘
              │                │
              └────────────────┘

                          ┌──────────┐
            SIGTERM/INT──►│ SHUTDOWN │  save state, cancel orders, notify, exit
                          └──────────┘
```

## 2. Main Loop Detail (per iteration)

```
FETCH_PRICE
  │
  ├── fail ──► consecutive_errors++
  │              ├── >= MAX_CONSECUTIVE_ERRORS ──► SHUTDOWN
  │              └── < MAX ──► sleep ──► next iteration
  │
  ▼ success (reset consecutive_errors)
DAILY_RESET_CHECK
  │
  ├── date changed ──► capture yesterday's values ──► reset counters ──► send daily summary ──► unpause
  │
  ▼
CHECK_RISK_LIMITS
  │
  ├── stop_floor breached ──────────────────────────► SHUTDOWN
  ├── consecutive_errors >= MAX ────────────────────► SHUTDOWN
  ├── daily_loss >= DAILY_LOSS_LIMIT ──► CANCEL_GRID ──► PAUSED ──► sleep ──► next iteration
  │
  ▼ ok
CHECK_FILLS (runs BEFORE drift check to avoid cancel-before-detect race)
  │
  ├── no fills ──► skip
  │
  ├── fills detected ──► HANDLE_FILLS
  │     │
  │     ├── [grid mode] buy filled ──► place sell (carry matched_buy_price)
  │     ├── [grid mode] sell filled ──► compute profit ──► place buy
  │     │     ├── has matched_buy_price ──► accurate P&L + round trip++
  │     │     └── no match ──► $0 profit + warning (NO round trip increment)
  │     ├── [grid mode] trend_ratio drift >= 0.2 ──► CANCEL_GRID ──► BUILD_GRID
  │     │
  │     ├── [pair mode] handle_pair_fill() ──► see Section 9 state machine
  │     │
  │     └── prune_completed_orders + save_state
  │
  ▼
CHECK_DRIFT
  │
  ├── [grid mode] drift >= GRID_DRIFT_RESET_PCT ──► CANCEL_GRID ──► BUILD_GRID
  ├── [pair mode] stale entries refreshed via refresh_stale_entries() (no full rebuild)
  │
  ▼ no drift
AI_COUNCIL (if interval elapsed)
  │
  ├── query each panelist (Llama-70B, Llama-8B, Kimi-K2.5)
  │     └── 1s pause between calls (rate limit)
  │
  ├── aggregate votes (majority >50% required)
  │     ├── no majority ──► action = "continue" (split suppresses spam)
  │     └── majority ──► winning action
  │
  ├── action == "continue" ──► no-op
  │
  ├── action != "continue" ──► SET_PENDING_APPROVAL
  │     │
  │     ├── user approves ──► EXECUTE_ACTION
  │     │     ├── widen_spacing ──► rebuild grid
  │     │     ├── tighten_spacing ──► rebuild grid
  │     │     ├── pause ──► PAUSED
  │     │     └── reset_grid ──► CANCEL_GRID ──► BUILD_GRID
  │     │
  │     ├── user skips ──► clear pending
  │     └── timeout (10 min) ──► expire + clear
  │
  ▼
WEB_CONFIG_CHECK
  │
  ├── spacing/ratio changed ──► CANCEL_GRID ──► BUILD_GRID
  │
  ▼
STATS_ENGINE (every 60s)
  │
  ▼
ACCUMULATION_CHECK
  │
  ├── excess > $1 AND sweep interval elapsed ──► buy DOGE with profits
  │
  ▼
PERIODIC_SAVE (every ~5 min)
  │
  ▼
SLEEP (remaining poll interval)
```

## 3. GridOrder State Machine

```
                  ┌─────────┐
                  │ pending │  created in memory
                  └────┬────┘
                       │
              place_order() called
                       │
            ┌──────────┼──────────┐
            │ success  │          │ exception
            ▼          │          ▼
       ┌────────┐      │     ┌────────┐
       │  open  │      │     │ failed │
       └───┬────┘      │     └────────┘
           │           │
    ┌──────┴──────┐    │
    │             │    │
    ▼ fill        ▼ cancel
┌────────┐   ┌───────────┐
│ filled │   │ cancelled │
└───┬────┘   └───────────┘
    │
    │ (if buy, and paired sell completes)
    ▼
┌─────────────┐
│ closed_out  │  excluded from unrealized loss calc
└─────────────┘
```

### Fill & Status Handling (live mode only)

**API key requirement:** "Query Closed Orders & Trades" permission is
required for `QueryOrders` to return filled orders. Without it, filled
orders silently disappear from the response.

```
              ┌────────┐
              │  open  │
              └───┬────┘
                  │
     query_orders() from Kraken
                  │
        ┌─────────┼──────────┬──────────────┬──────────────┐
        │         │          │              │              │
   status=closed  │   status=open     status=canceled   txid MISSING
        │         │   + vol_exec>0    or expired        from response
        ▼         │          │              │              │
   ┌────────┐     │    log PARTIAL         ▼         log WARNING
   │ filled │     │    FILL warning   ┌───────────┐  (possible API
   └────────┘     │    (stay open)    │ cancelled │   key permission
   vol = vol_exec │                   └─────┬─────┘   issue)
                  │                         │
           status=open                 place_order()
           + vol_exec=0                (same level/price)
                  │                         │
                  ▼                         ▼
            (no change)              ┌────────┐
                                     │  open  │  (replacement)
                                     └────────┘

  After status loop:
       │
       ▼
  SANITY CHECK: price moved >0.5% past any "open" order?
       │
       ├── no ──► continue
       │
       ├── yes ──► log "STALE OPEN?" warning
       │           │
       │           ▼
       │     TRADE HISTORY FALLBACK
       │     get_trades_history() from Kraken
       │           │
       │     ┌─────┴─────┐
       │     │           │
       │   trade matches  no match
       │   open order     │
       │     │            ▼
       │     ▼         (no action)
       │   mark FILLED
       │   log "FALLBACK" warning
       │
       ▼
  (continue to replacement logic)
```

## 4. Risk / Pause State Machine

```
                  ┌──────────┐
                  │ TRADING  │  normal operation
                  └────┬─────┘
                       │
          ┌────────────┼────────────┐
          │            │            │
   daily_loss >=    stop_floor   consecutive
   DAILY_LIMIT      breached     errors >= MAX
          │            │            │
          ▼            ▼            ▼
   CANCEL_GRID   ┌──────────┐  ┌──────────┐
          │      │ SHUTDOWN │  │ SHUTDOWN │
          ▼      └──────────┘  └──────────┘
     ┌────────┐
     │ PAUSED │  all orders cancelled, no fills possible
     └───┬────┘
         │
    midnight UTC
    (daily reset)
         │
         ▼
    ┌──────────┐
    │ TRADING  │  counters zeroed, unpause, grid rebuilt on drift check
    └──────────┘
```

## 5. Startup Reconciliation Flow

### Grid Mode

```
┌──────────────────────┐
│  get_open_orders()   │  fetch all open orders from Kraken
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  filter to XDGUSD    │  ignore other pairs
└──────────┬───────────┘
           │
     for each order:
           │
    ┌──────┴───────┐
    │              │
    ▼              ▼
 near valid     not near any
 grid level?    grid level
    │              │
    ▼              ▼
 ADOPT          CANCEL (orphan)
 - add to       - cancel_order(txid)
   grid_orders
 - mark level
   as covered
           │
           ▼
┌──────────────────────┐
│  build_grid()        │  only places orders for uncovered levels
└──────────────────────┘
```

### Pair Mode

Pair mode uses a different reconciliation strategy: adopt exits first
(they carry position risk), then restore identity from saved state.

```
┌──────────────────────┐
│  get_open_orders()   │  fetch all open orders from Kraken
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  filter to pair      │  ignore other pairs
└──────────┬───────────┘
           │
     for each order:
           │
    ┌──────┴───────────────┐
    │                      │
    ▼                      ▼
  matches saved         no match in
  txid from state?      saved open_orders
    │                      │
    ▼                      ▼
  ADOPT with           ADOPT with
  saved identity       side convention:
  (trade_id, cycle,    sell → A entry
   order_role)         buy  → B entry
           │
           ▼
┌──────────────────────────────┐
│  OFFLINE FILL RECOVERY       │
│  get_trades_history(6h)      │
└──────────┬───────────────────┘
           │
     for pair trades:
           │
    ┌──────┼────────────────┐
    │      │                │
    ▼      ▼                ▼
  BOTH   buy only,       sell only,
  filled  no sell exit    no buy exit
    │      on book         on book
    │      │               │
    │      ▼               ▼
    │   cancel sell     cancel buy
    │   entry, place    entry, place
    │   sell EXIT via   buy EXIT via
    │   _pair_exit_     _pair_exit_
    │   price()         price()
    │
    ▼
 STEP 1: FILTER ALREADY-PROCESSED
    │   skip trades matching recent_fills (price+time)
    │
    ▼
 STEP 2: CLASSIFY EXIT vs ENTRY
    │   match fill price against known positions
    │   in recent_fills (e.g. buy at sell×(1-π) = exit)
    │   (uses simple formula, not market-guarded)
    │
    ├── fill matches known position exit?
    │      │
    │     YES ──► OFFLINE EXIT + ENTRY
    │              book round trip, create CompletedCycle
    │              place exit order for new entry fill
    │              save_fill with trade_id + cycle
    │      │
    │      NO ──► DUAL-FILL DETECTION
    │              │
    │              ├── 2nd near 1st's profit target?
    │              │     YES ──► OFFLINE ROUND TRIP
    │              │              book PnL, create CompletedCycle
    │              │              place fresh pair
    │              │     NO  ──► OFFLINE RACE CONDITION
    │              │              book implicit close
    │              │              exit for later fill only
           │
           ▼
┌──────────────────────┐
│  build_pair_orders() │  places entries for uncovered sides
└──────────────────────┘
```

## 6. State Persistence Flow

```
                    ┌─────────────┐
                    │  STARTUP    │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
            ┌─yes──│ state.json  │──no──┐
            │      │  exists?    │      │
            ▼      └─────────────┘      ▼
    load_state()                  start fresh
    restore counters              (all zeros)
            │                           │
            └───────────┬───────────────┘
                        │
                   MAIN LOOP
                        │
              ┌─────────┼─────────┐
              │         │         │
         after fills  every 5m  on shutdown
              │         │         │
              └─────────┼─────────┘
                        │
                  save_state()
                  write tmp file
                  atomic rename
                        │
                        ▼
                  logs/state.json
```

### Persisted Fields
| Field | Purpose |
|-------|---------|
| `center_price` | Grid center for reconciliation |
| `total_profit_usd` | Lifetime P&L |
| `today_profit_usd` | Today's running P&L |
| `today_loss_usd` | Today's losses (for daily limit) |
| `today_fees_usd` | Today's fees paid |
| `today_date` | Date string for reset detection |
| `round_trips_today` | Today's completed cycles |
| `total_round_trips` | Lifetime completed cycles |
| `total_fees_usd` | Lifetime fees paid |
| `doge_accumulated` | Total DOGE swept |
| `last_accumulation` | Timestamp of last sweep |
| `trend_ratio` | Current buy/sell asymmetry |
| `trend_ratio_override` | Manual ratio (null = auto) |
| `open_txids` | Kraken order IDs to reconcile |
| `pair_state` | Current state machine state (S0/S1a/S1b/S2) |
| `cycle_a` | Trade A current cycle number |
| `cycle_b` | Trade B current cycle number |
| `completed_cycles` | List of CompletedCycle dicts (max 200) |
| `open_orders` | Order details for identity restoration on restart |

## 7. Fill Pair Cycling (The Profit Engine)

```
Price oscillates around grid center:

        sell L+2 ──────── $0.0918
        sell L+1 ──────── $0.0909
     ── CENTER ─────────── $0.0900 ──
        buy  L-1 ──────── $0.0891
        buy  L-2 ──────── $0.0882

When buy L-1 fills at $0.0891:
  1. Place sell at L0 = $0.0900 (matched_buy_price = $0.0891)

When that sell fills at $0.0900:
  2. Profit = ($0.0900 - $0.0891) * volume - fees
  3. Mark buy L-1 as closed_out
  4. Place buy at L-1 = $0.0891 (cycle repeats)

          BUY fills           SELL fills
            │                    │
            ▼                    ▼
     place SELL 1 up      compute profit
     (carry cost basis)   (from matched_buy_price)
            │                    │
            │              mark buy closed_out
            │                    │
            │              place BUY 1 down
            │                    │
            └────── wait ────────┘
```

## 8. Transition Summary Table

| From | To | Trigger | Action |
|------|----|---------|--------|
| START | INIT | always | setup logging, signals |
| INIT | LOAD_STATE | always | restore state.json |
| LOAD_STATE | FETCH_PRICE | always | get DOGE price |
| FETCH_PRICE | EXIT | price fetch fails | notify error |
| FETCH_PRICE | VALIDATE | price ok | run guardrails |
| VALIDATE | EXIT | critical check fails | notify error |
| VALIDATE | RECONCILE | checks pass | adopt/cancel orders |
| RECONCILE | BUILD_GRID | always | place remaining orders |
| BUILD_GRID | MAIN_LOOP | always | enter polling |
| MAIN_LOOP | SHUTDOWN | SIGTERM/SIGINT | graceful exit |
| MAIN_LOOP | SHUTDOWN | stop_floor breached | emergency exit |
| MAIN_LOOP | SHUTDOWN | MAX errors reached | error exit |
| MAIN_LOOP | PAUSED | daily loss limit hit | cancel orders + skip trading |
| MAIN_LOOP | PAUSED | AI "pause" approved | cancel orders + skip trading |
| PAUSED | MAIN_LOOP | midnight UTC | reset counters, grid rebuilt via drift check |
| MAIN_LOOP | DRIFT_RESET | [grid] price drift >= 5% | cancel + rebuild |
| MAIN_LOOP | ENTRY_REFRESH | [pair] entry drifts >= PAIR_REFRESH_PCT | refresh stale entry only |
| MAIN_LOOP | RATIO_REBUILD | [grid] trend ratio shift >= 0.2 | cancel + rebuild |
| SHUTDOWN | EXIT | always | save, cancel orders, notify |

## 9. Pair Strategy State Machine (`STRATEGY_MODE=pair`)

In pair mode the bot maintains exactly 2 open orders (1 buy + 1 sell),
organized as two independent **trades**:

- **Trade A** (short-side): sell entry → buy exit
- **Trade B** (long-side): buy entry → sell exit

Each order carries identity fields: `trade_id` ("A" or "B"), `cycle`
(generation number), and `order_role` ("entry" or "exit"). Identity
is determined by trade, not side: Trade A entries are sells but Trade A
exits are buys (opposite side). During reconciliation, unmatched orders
fall back to entry-side convention (sell entry → A, buy entry → B).

### Config Parameters

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `PAIR_ENTRY_PCT` (ε) | 0.2% | Distance from market for entry orders |
| `PAIR_PROFIT_PCT` (π) | 1.0% | Profit target distance from entry fill price |
| `PAIR_REFRESH_PCT` | 1.0% | Max drift before stale entry is refreshed |

### Formal States

The pair state machine has 4 states, derived by `_compute_pair_state()`
from which orders are on the book:

| State | Open Orders | Meaning |
|-------|-------------|---------|
| **S0** | sell entry + buy entry | No position, both flanking market |
| **S1a** | buy exit + buy entry | Trade A filled (sell entry → buy exit pending) |
| **S1b** | sell exit + sell entry | Trade B filled (buy entry → sell exit pending) |
| **S2** | buy exit + sell exit | Both entries filled, both exits pending |

```
                   ┌─────────────────────┐
                   │        S0           │
                   │  sell entry (A.n)   │
                   │  buy  entry (B.n)   │
                   └──────┬──────┬───────┘
                          │      │
            sell entry    │      │   buy entry
            fills (A)     │      │   fills (B)
                          │      │
               ┌──────────▼┐    ┌▼──────────┐
               │    S1a    │    │    S1b     │
               │ buy exit  │    │ sell exit  │
               │ buy entry │    │ sell entry │
               └──┬─────┬──┘    └──┬─────┬──┘
                  │     │          │     │
        buy entry │     │ buy exit │     │ sell exit
        fills (B) │     │ fills    │     │ fills
                  │     │  (A ✓)   │     │  (B ✓)
           ┌──────▼─┐   │      ┌──▼──────┐
           │   S2   │◄──┤      │   S2    │
           │buy exit│   │      │buy exit │
           │sel exit│   │      │sel exit │
           └┬─────┬─┘   │      └┬─────┬──┘
            │     │      │       │     │
     buy    │     │sell  │  buy  │     │ sell
     exit   │     │exit  │  exit │     │ exit
     (A ✓)  │     │(B ✓) │  (A ✓)│     │ (B ✓)
            │     │      │       │     │
            ▼     ▼      ▼       ▼     ▼
         back to S0/S1b/S1a (one trade completes,
         new entry placed for completed side)
```

### Exit Price Formula (`_pair_exit_price`)

Exit prices use a min/max market guard to ensure the exit is never
placed inside the current spread:

```
Trade B exit (sell):  max(entry × (1 + π), market × (1 + ε))
Trade A exit (buy):   min(entry × (1 - π), market × (1 - ε))
```

Where π = `PAIR_PROFIT_PCT/100` and ε = `PAIR_ENTRY_PCT/100`.

### Initial State (S0, no position)

```
  SELL entry (A.1)  = market × (1 + ε)     role=entry, trade_id=A
  ── market price ──
  BUY  entry (B.1)  = market × (1 - ε)     role=entry, trade_id=B
```

### State Transitions (handle_pair_fill)

```
S0 (2 entries flanking market)
       │
       │─── BUY ENTRY fills (Trade B) ──────────────────.
       │                                                │
       │    1. Record fee, append to recent_fills       │
       │    2. SELL exit = _pair_exit_price(buy_price,   │
       │       market, "sell", state)  [role=exit, B.n]  │
       │    3. Keep SELL entry (A) unchanged             │
       │    4. State → S1b                               │
       │                                                │
       │    ─── SELL ENTRY fills (Trade A) ──────────   │
       │        1. BUY exit = _pair_exit_price(sell_price,│
       │           market, "buy", state)  [role=exit, A.n]│
       │        2. State → S2                            │
       │                                                │
       │    ─── SELL EXIT fills (Trade B round trip ✓) ──│
       │        1. Profit = (sell - buy) × volume - fees │
       │        2. CompletedCycle(B, n, entry_side=buy)  │
       │        3. cycle_b = n + 1                       │
       │        4. Cancel stale buy entry                │
       │        5. BUY entry = market × (1 - ε)          │
       │           [role=entry, B.(n+1)]                 │
       │        6. State → S0 or S1a                     │
       │                                                │
       │─── SELL ENTRY fills (Trade A) ─────────────────.
       │                                                │
       │    1. Record fee, append to recent_fills       │
       │    2. BUY exit = _pair_exit_price(sell_price,   │
       │       market, "buy", state)  [role=exit, A.n]   │
       │    3. Keep BUY entry (B) unchanged              │
       │    4. State → S1a                               │
       │                                                │
       │    ─── BUY ENTRY fills (Trade B) ──────────    │
       │        1. SELL exit = _pair_exit_price(buy_price,│
       │           market, "sell", state)  [role=exit,B.n]│
       │        2. State → S2                            │
       │                                                │
       │    ─── BUY EXIT fills (Trade A round trip ✓) ───│
       │        1. Profit = (sell - buy) × volume - fees │
       │        2. CompletedCycle(A, n, entry_side=sell)  │
       │        3. cycle_a = n + 1                       │
       │        4. Cancel stale sell entry               │
       │        5. SELL entry = market × (1 + ε)          │
       │           [role=entry, A.(n+1)]                 │
       │        6. State → S0 or S1b                     │
```

### CompletedCycle Tracking

Every round trip (entry → exit) creates a `CompletedCycle` record:

| Field | Type | Description |
|-------|------|-------------|
| `trade_id` | str | "A" or "B" |
| `cycle` | int | Cycle number that completed |
| `entry_side` | str | "sell" (Trade A) or "buy" (Trade B) |
| `entry_price` | float | Entry fill price |
| `exit_price` | float | Exit fill price |
| `volume` | float | DOGE traded |
| `gross_profit` | float | (sell - buy) × volume, before fees |
| `fees` | float | Total fees (entry + exit legs) |
| `net_profit` | float | gross_profit - fees |
| `entry_time` | float | Unix timestamp of entry fill (best-effort*) |
| `exit_time` | float | Unix timestamp of exit fill |

\* `entry_time` is populated by scanning `recent_fills` backwards for a
matching entry (by side and price). It will be 0 if the entry fill was
already pruned from the deque (e.g. after a long-running cycle).

- Max 200 cycles kept in memory (`MAX_COMPLETED_CYCLES`)
- Last 50 exposed to dashboard
- Scalar accumulators (`total_profit_usd`, `total_round_trips`) remain
  authoritative for lifetime stats; `completed_cycles` provides per-trade
  breakdowns

### Key Invariants

1. Exactly 2 open orders under normal operation (may transiently have
   0-1 during pauses, placement failures, or between fill detection
   and replacement order placement)
2. Entry fills keep the opposite-side order and add an exit
3. Exit fills only replace the completed side's entry; the other side is never cancelled
4. If both entries fill before either exit → S2 (both exits on book)
5. Trade identity (A/B) and cycle number propagate through every order
   placement, fill record, state save, Supabase write, AI payload, and
   dashboard display
6. Cycle number increments only on round-trip completion (exit fill), not on entry

### Offline Fill Recovery

If fills happen while the bot is offline (between deploys),
`_reconcile_offline_fills()` detects them on startup via trade history.
Identity is restored by matching saved txids from state.json; unmatched
orders fall back to side convention (sell→A, buy→B).

```
Offline: BUY entry @ $0.098 fills, then SELL entry @ $0.0984 fills.

On restart, trade history shows both fills.
  1. Sort chronologically (first=buy, second=sell)
  2. Expected exit = $0.098 * 1.01 = $0.09898
  3. Actual second price $0.0984 != $0.09898 → DUAL ENTRY
  4. Record both entries (profit = 0, fees tracked)
  5. Assign identity: buy fill → B, sell fill → A
  6. Place sell exit for B: _pair_exit_price($0.098, market, "sell")
  7. Place buy exit for A:  _pair_exit_price($0.0984, market, "buy")
  8. CompletedCycle recorded for each if exit also detected offline
  9. All save_fill calls carry trade_id and cycle
```

Classification formulas in offline reconciliation use the simple
`entry × (1 ± π)` formula (no market guard) since they are detecting
fills that were placed by the prior formula, not placing new orders.

### Entry Refresh Safety

Before cancelling a stale entry, `refresh_stale_entries()` queries
the order status via `query_orders()`. If the order is already
`closed` (filled), the cancel is skipped and the fill is left for
`check_fills_live()` to process on the next cycle. This prevents
the race where drift-cancel runs right after a fill but before
fill detection.

### Pair vs Grid Comparison

| Aspect | Grid Mode | Pair Mode |
|--------|-----------|-----------|
| Open orders | 20-40 | exactly 2 |
| Max exposure | ~$35 (all buys) | ~$3.50 (1 order) |
| Order roles | implicit (level-based) | explicit (entry/exit) |
| Order identity | none | trade_id + cycle |
| Trend ratio | adjusts buy/sell split | N/A |
| State machine | none | S0/S1a/S1b/S2 |
| Round-trip tracking | scalar counters | CompletedCycle history |
| Dashboard UI | Grid Ladder, Trend Ratio | State banner, A/B panels, Cycles table |
