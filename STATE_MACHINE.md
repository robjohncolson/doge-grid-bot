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
              │    │    CHECK DRIFT        │     │
              │    └───────────┬───────────┘     │
              │                │                 │
              │    ┌───────────▼───────────┐     │
              │    │    CHECK FILLS        │     │
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
CHECK_GRID_DRIFT
  │
  ├── drift >= GRID_DRIFT_RESET_PCT ──► CANCEL_GRID ──► BUILD_GRID
  │
  ▼ no drift
CHECK_FILLS
  │
  ├── no fills ──► skip
  │
  ├── fills detected ──► HANDLE_FILLS
  │     │
  │     ├── buy filled ──► place sell (carry matched_buy_price)
  │     │
  │     ├── sell filled ──► compute profit ──► place buy
  │     │     ├── has matched_buy_price ──► accurate P&L + round trip++
  │     │     └── no match ──► $0 profit + warning (NO round trip increment)
  │     │
  │     ├── trend_ratio drift >= 0.2 ──► CANCEL_GRID ──► BUILD_GRID
  │     │
  │     └── prune_completed_orders + save_state
  │
  ▼
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
```
              ┌────────┐
              │  open  │
              └───┬────┘
                  │
     query_orders() from Kraken
                  │
        ┌─────────┼──────────┬──────────────┐
        │         │          │              │
   status=closed  │   status=open     status=canceled
        │         │   + vol_exec>0    or expired
        ▼         │          │              │
   ┌────────┐     │    log PARTIAL         ▼
   │ filled │     │    FILL warning   ┌───────────┐
   └────────┘     │    (stay open)    │ cancelled │
   vol = vol_exec │                   └─────┬─────┘
                  │                         │
           status=open                 place_order()
           + vol_exec=0                (same level/price)
                  │                         │
                  ▼                         ▼
            (no change)              ┌────────┐
                                     │  open  │  (replacement)
                                     └────────┘
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
| MAIN_LOOP | DRIFT_RESET | price drift >= 5% | cancel + rebuild |
| MAIN_LOOP | RATIO_REBUILD | trend ratio shift >= 0.2 | cancel + rebuild |
| SHUTDOWN | EXIT | always | save, cancel orders, notify |
