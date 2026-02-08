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
CHECK_GRID_DRIFT
  │
  ├── drift >= GRID_DRIFT_RESET_PCT ──► CANCEL_GRID ──► BUILD_GRID
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
┌──────────────────────────────┐
│  OFFLINE FILL RECOVERY       │  (pair mode only)
│  get_trades_history(6h)      │
└──────────┬───────────────────┘
           │
     for XDGUSD trades:
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
    │   sell EXIT at    buy EXIT at
    │   buy*(1+PCT)     sell*(1-PCT)
    │
    ▼
 STEP 1: FILTER ALREADY-PROCESSED
    │   skip trades matching recent_fills (price+time)
    │
    ▼
 STEP 2: CLASSIFY EXIT vs ENTRY
    │   match fill price against known positions
    │   in recent_fills (e.g. buy at sell*0.99 = exit)
    │
    ├── fill matches known position exit?
    │      │
    │     YES ──► OFFLINE EXIT + ENTRY
    │              book round trip for exit fill
    │              place exit order for new entry fill
    │      │
    │      NO ──► DUAL-FILL DETECTION
    │              │
    │              ├── 2nd near 1st's profit target?
    │              │     YES ──► OFFLINE ROUND TRIP
    │              │              book PnL, place fresh pair
    │              │     NO  ──► OFFLINE RACE CONDITION
    │              │              book implicit close
    │              │              exit for later fill only
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

## 9. Pair Strategy State Machine (`STRATEGY_MODE=pair`)

In pair mode the bot maintains exactly 2 open orders (1 buy + 1 sell).
Each order has an `order_role`: **entry** (flanking market price) or
**exit** (profit target for a filled entry).

### Config Parameters

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `PAIR_ENTRY_PCT` | 0.2% | Distance from market for entry orders |
| `PAIR_PROFIT_PCT` | 1.0% | Profit target distance from fill price |
| `PAIR_REFRESH_PCT` | 1.0% | Max drift before stale entry is refreshed |

### Initial State (no position)

```
  market price
       |
  SELL entry  = market * (1 + PAIR_ENTRY_PCT)     role=entry
  BUY  entry  = market * (1 - PAIR_ENTRY_PCT)     role=entry
```

### State Transitions

```
INITIAL (2 entries flanking market)
       |
       |--- BUY ENTRY fills -------.
       |                           |
       |    1. Cancel stale sell entry
       |    2. SELL = exit at buy_price * (1 + PAIR_PROFIT_PCT)   role=exit
       |    3. BUY  = entry at market - PAIR_ENTRY_PCT            role=entry
       |                           |
       |    State: 1 buy entry + 1 sell exit
       |                           |
       |    --- SELL EXIT fills (ROUND TRIP) ---
       |        1. Record profit, log trade
       |        2. SELL = entry at market + PAIR_ENTRY_PCT        role=entry
       |        3. Refresh BUY entry if stale (> PAIR_REFRESH_PCT from market)
       |        -> Back to INITIAL
       |
       |--- SELL ENTRY fills ------.
       |                           |
       |    1. Cancel stale buy entry
       |    2. BUY  = exit at sell_price * (1 - PAIR_PROFIT_PCT)  role=exit
       |    3. SELL = entry at market + PAIR_ENTRY_PCT             role=entry
       |                           |
       |    State: 1 sell entry + 1 buy exit
       |                           |
       |    --- BUY EXIT fills (ROUND TRIP) ---
       |        1. Record profit, log trade
       |        2. BUY  = entry at market - PAIR_ENTRY_PCT        role=entry
       |        3. Refresh SELL entry if stale (> PAIR_REFRESH_PCT from market)
       |        -> Back to INITIAL
```

### Key Invariant

Always exactly 2 open orders. Entry orders refresh when they drift
more than `PAIR_REFRESH_PCT` from market. Exit orders never move
(profit target is fixed at the fill price).

### Race Condition: Both Entries Fill (Live)

If both entries fill within the 30s poll gap before the bot can cancel one,
the second entry implicitly closes the position opened by the first.

```
BUY entry fills -> bot places SELL exit
                -> before bot cancels SELL entry, it also fills

Now: orphaned SELL exit exists for an already-closed long position.

_close_orphaned_exit() detects this:
  1. Find orphaned exit (same side as filled entry, role=exit)
  2. Look up cost basis from orphan's matched_buy_price
  3. Book PnL: (sell_entry_price - buy_entry_price) * vol - fees
  4. Cancel orphaned exit
  5. Log as RACE CLOSE
```

Same logic applies in reverse (sell entry fills first, then buy entry).

### Race Condition: Both Entries Fill (Offline)

If both entries fill while the bot is offline (between deploys),
`_reconcile_offline_fills()` detects the dual fill on startup.

```
Offline: BUY entry @ $0.098 fills, then SELL entry @ $0.0984 fills.

On restart, trade history shows both fills.
  1. Sort chronologically (first=buy, second=sell)
  2. Expected exit = $0.098 * 1.01 = $0.09898
  3. Actual second price $0.0984 != $0.09898 -> RACE CONDITION
  4. Book PnL: ($0.0984 - $0.098) * vol - fees  (uses actual prices)
  5. Place buy exit for sell position: $0.0984 * 0.99
  6. Place sell entry companion
```

If the second fill IS near the expected exit, it's a **normal round trip
completed offline**: book the PnL and place a fresh entry pair.

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
| Trend ratio | adjusts buy/sell split | N/A |
| Dashboard UI | Grid Ladder, Trend Ratio, Spacing | Active Orders, Role column, Profit target |
