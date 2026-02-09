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
  ├── [pair mode] stale entries refreshed via refresh_stale_entries() (anti-chase protected)
  │
  ▼ no drift
AI_COUNCIL (manual trigger only: /check or dashboard button)
  │
  ├── last_ai_check == 0? (flag set by /check or web ai_check)
  │     NO  ──► skip (AI is manual-only, no timer)
  │     YES ──► run council, set last_ai_check = now
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
  ├── [grid mode] spacing/ratio changed ──► CANCEL_GRID ──► BUILD_GRID
  ├── [pair mode] spacing (profit %) changed ──► no rebuild (applies to next exit)
  ├── [pair mode] entry_pct changed ──► replace_entries_at_distance() (exits preserved)
  ├── ai_check ──► set last_ai_check = 0 (triggers council next cycle)
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
(they carry position risk), then restore identity via 3-tier resolution.

#### 3-Tier Identity Resolution (`_identify_order_3tier`)

Each Kraken order is identified using the first matching tier:

| Tier | Method | Reliability | How |
|------|--------|-------------|-----|
| **1** | `saved_txid` | Highest | Match txid against `_saved_open_orders` from state.json |
| **2** | `price_match` | Medium | Match price against `recent_fills`: buy exit ≈ sell_entry × (1 - π) within 0.5% tolerance |
| **3** | `side_convention` | Fallback | sell → A entry, buy → B entry |

Tier 2 catches orders placed as exits where the bot restarted before
saving txid to state (e.g. crash between placement and save).

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
           ▼
  ┌──────────────────────────────────┐
  │  _identify_order_3tier()         │
  │                                  │
  │  Tier 1: saved txid match?      │
  │    YES → use saved identity     │
  │    NO  ↓                        │
  │  Tier 2: price matches fill?    │
  │    YES → infer exit identity    │
  │    NO  ↓                        │
  │  Tier 3: side convention        │
  │    sell → A entry               │
  │    buy  → B entry               │
  └──────────┬───────────────────────┘
             │
             ▼
  ADOPT with resolved identity
  (trade_id, cycle, order_role, method)
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
| `trend_ratio` | Current buy/sell asymmetry (excluded in pair mode — saved as 0.5) |
| `trend_ratio_override` | Manual ratio (null = auto, excluded in pair mode — saved as null) |
| `open_txids` | Kraken order IDs to reconcile |
| `pair_state` | Current state machine state (S0/S1a/S1b/S2) |
| `cycle_a` | Trade A current cycle number |
| `cycle_b` | Trade B current cycle number |
| `completed_cycles` | List of CompletedCycle dicts (max 200) |
| `open_orders` | Order details for identity restoration on restart |
| `pnl_migrated` | Flag: historical P&L reconstruction complete (prevents re-run) |
| `consecutive_refreshes_a` | Anti-chase: same-direction refresh count for Trade A |
| `consecutive_refreshes_b` | Anti-chase: same-direction refresh count for Trade B |
| `last_refresh_direction_a` | Anti-chase: last refresh direction ("up"/"down") for Trade A |
| `last_refresh_direction_b` | Anti-chase: last refresh direction ("up"/"down") for Trade B |
| `refresh_cooldown_until_a` | Anti-chase: cooldown expiry timestamp for Trade A |
| `refresh_cooldown_until_b` | Anti-chase: cooldown expiry timestamp for Trade B |
| `total_entries_placed` | Lifetime entry orders placed (for fill rate) |
| `total_entries_filled` | Lifetime entry orders filled (for fill rate) |

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
| MAIN_LOOP | ENTRY_REPLACE | [pair] user changes entry_pct via dashboard | replace entries at new distance (exits preserved) |
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
| `volume_decimals` | 0 | Decimal places for volume rounding (DOGE=0, SOL=4) |

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

### P&L Migration (`migrate_pnl_from_fills`)

On first startup after the refactor, `migrate_pnl_from_fills()` reconstructs
`CompletedCycle` records from `recent_fills`. It does NOT trust the stored
`profit` field (which may have been sanitized by old code). Instead it:

1. Separates fills into buy/sell lists
2. Matches exits to entries by expected price: `sell × (1 - π) ≈ buy` (Trade A)
   or `buy × (1 + π) ≈ sell` (Trade B), within 0.5% tolerance
3. Computes P&L as `(sell_price - buy_price) × volume - fees`
4. Sets `pnl_migrated = True` to prevent re-running

### Key Invariants

1. Exactly 2 open orders under normal operation (may transiently have
   0-1 during pauses, placement failures, or between fill detection
   and replacement order placement)
2. Entry fills keep the opposite-side order and add an exit
3. Exit fills only replace the completed side's entry; the other side is never cancelled
4. If both entries fill before either exit → S2 (both exits on book)
5. Trade identity (A/B) and cycle number propagate through every order
   placement, fill record (`recent_fills` carry `trade_id`, `cycle`,
   `order_role`), state save, Supabase write, AI payload, Telegram
   notification, CSV export, and dashboard display
6. Cycle number increments only on round-trip completion (exit fill), not on entry
7. Exit fill records also carry `entry_price` for Telegram notification formatting

### Offline Fill Recovery

If fills happen while the bot is offline (between deploys),
`_reconcile_offline_fills()` detects them on startup via trade history.
Identity is restored by `_identify_order_3tier()` (see Section 5):
saved txid → price match against fills → side convention fallback.

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

### Anti-Chase Mechanism

During sustained trends, the bot may repeatedly refresh entries in
the same direction (always chasing price up or down). The anti-chase
mechanism prevents this:

```
refresh_stale_entries() called
        │
        ├── trade in cooldown? ──── YES → skip refresh, log warning
        │
        ▼ NO
  determine refresh direction (up/down) from price movement
        │
        ├── same direction as last refresh?
        │       YES → increment consecutive count
        │       NO  → reset count to 1
        │
        ├── count >= MAX_CONSECUTIVE_REFRESHES (3)?
        │       YES → enter cooldown (REFRESH_COOLDOWN_SEC = 300s)
        │              log warning, skip refresh
        │       NO  → proceed with normal refresh
        │
  On next call, if cooldown expired AND count >= threshold:
        │       → reset count to 0, clear cooldown, allow refresh
        │
        ▼
  cancel stale entry, place new entry at current market distance
```

Per-trade tracking (A/B independent): `consecutive_refreshes_a/b`,
`last_refresh_direction_a/b`, `refresh_cooldown_until_a/b`.

### Unrealized P&L

`compute_unrealized_pnl(state, current_price)` calculates mark-to-market
P&L for open exit orders:

```
Trade A (buy exit): unrealized = (matched_sell_price - current_price) × volume
Trade B (sell exit): unrealized = (current_price - matched_buy_price) × volume
```

Returns `{a_unrealized, b_unrealized, total_unrealized}`.

Returns signed values (positive = in profit, negative = in loss).

Used in:
- `check_risk_limits()`: stop floor uses signed unrealized in pair mode:
  `estimated_value = STARTING_CAPITAL + total_profit_usd + total_unrealized`
- `get_status_summary()`: displays unrealized P&L line
- Dashboard: unrealized P&L card in top metrics

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
| Anti-chase | N/A | 3 consecutive same-direction → 5min cooldown |
| Unrealized P&L | N/A | mark-to-market from open exits |
| Statistics | stats_engine analyzers | PairStats + stats_engine |
| AI trigger | timer (AI_ADVISOR_INTERVAL) | manual only (/check or dashboard button) |
| Entry hot-reload | full rebuild | replace entries only (exits preserved) |
| Profit hot-reload | full rebuild | deferred (applies to next exit placement) |
| Notifications | startup + grid built + round trip | round trip only (with trade identity) |
| Dashboard UI | Grid Ladder, Trend Ratio | State banner, A/B panels, Stats, AI council, Cycles |

## 10. PairStats (Pair Mode Statistical Engine)

`compute_pair_stats()` in `stats_engine.py` produces aggregate statistics
from `CompletedCycle` records. Pure Python, zero external dependencies.

### Schema

| Field | Type | Description |
|-------|------|-------------|
| `n_total` | int | Total completed cycles |
| `n_trade_a` | int | Trade A cycles |
| `n_trade_b` | int | Trade B cycles |
| `total_net` | float | Sum of net profits |
| `mean_net` | float | Mean net profit per cycle |
| `stdev_net` | float | Sample standard deviation |
| `median_net` | float | Median net profit |
| `win_count` | int | Cycles with net > 0 |
| `loss_count` | int | Cycles with net ≤ 0 |
| `win_rate` | float | win_count / n_total (0-1) |
| `profit_factor` | float | sum(wins) / abs(sum(losses)) |
| `mean_duration_sec` | float | Average cycle duration in seconds |
| `median_duration_sec` | float | Median cycle duration |
| `max_drawdown` | float | Peak-to-trough of cumulative P&L series |
| `current_drawdown` | float | Current distance from cumulative peak |
| `ci_95_lower` | float | 95% CI lower bound for mean |
| `ci_95_upper` | float | 95% CI upper bound for mean |
| `entries_placed` | int | Entries placed (for fill rate) |
| `entries_filled` | int | Entries that filled |
| `fill_rate` | float | entries_filled / entries_placed |

### Computation

- **95% CI**: `mean ± t*(df, 0.025) × stdev / √n` using `_t_critical()`
  lookup table (same as stats_engine profitability test). Requires n ≥ 3.
- **Max drawdown**: Walk cumulative P&L series, track running peak and
  largest peak-to-current difference
- **Profit factor**: Sum of winning trades / abs(sum of losing trades);
  None if no losses, 0 if no wins
- **Fill rate**: `total_entries_filled / total_entries_placed` from GridState
  counters (incremented in `_place_pair_order` and `handle_pair_fill`)

### None Handling

Computed stats are `None` (not 0.0) when meaningless:
- `win_rate`, `mean_net`, `median_net` → None when n = 0
- `stdev_net` → None when n < 2
- `ci_95_lower/upper` → None when n < 3
- `profit_factor` → None when no losses
- `fill_rate` → None when no entries placed

Dashboard renders None as "—" (em dash).

### Integration

- Computed in `stats_engine.run_all()` when `STRATEGY_MODE == "pair"`
- Stored on `state.pair_stats` (PairStats object)
- Exposed to dashboard via `serialize_state()` → `pair_stats` dict
- Exposed to AI council via `market_data.performance.pair_stats`
- Per-trade breakdown (A/B columns) computed client-side in dashboard JS

## 11. AI Council Payload & Quorum

### Quorum Rules

Three panelists vote independently. Majority (>50%) required for action.

```
3/3 agree  →  action passes (unanimous)
2/3 agree  →  action passes (majority)
1/3 agree  →  "continue" (no action, suppresses spam)
0/3 agree  →  "continue" (all error/timeout)
```

`_aggregate_votes()` filters out error/timeout votes (`action="" or "unknown"`)
before counting. With 2 responding and agreeing, majority passes (2 > 1.0).

### Timeout Skip Mechanism

Per-panelist consecutive failure tracking prevents slow models from
blocking the council:

```
panelist call
    │
    ├── success → reset consecutive_fails to 0
    │
    ├── error/timeout → increment consecutive_fails
    │       │
    │       ├── < SKIP_THRESHOLD (3) → normal retry next cycle
    │       │
    │       └── >= SKIP_THRESHOLD → set skip_until = now + SKIP_COOLDOWN (3600s)
    │                                panelist skipped until cooldown expires
    │                                vote recorded as condition="skipped"
    │
    ▼ (next council cycle)
    ├── skip_until > now? → skip panelist, vote as "skipped"
    └── skip_until expired → try panelist again
```

### Payload Schema (`market_data`)

```json
{
  "market": {
    "price": 0.09,
    "center_price": 0.09,
    "drift_pct": 0.5
  },
  "strategy": {
    "mode": "pair",
    "pair_profit_pct": 1.0,
    "pair_entry_pct": 0.2,
    "pair_refresh_pct": 1.0
  },
  "state": {
    "pair_state": "S0",
    "cycle_a": 5,
    "cycle_b": 4,
    "trade_a_order": {"side": "sell", "price": 0.0902, "role": "entry", "volume": 35},
    "trade_b_order": {"side": "buy", "price": 0.0898, "role": "entry", "volume": 35}
  },
  "performance": {
    "total_profit": 0.15,
    "today_profit": 0.02,
    "total_round_trips": 8,
    "pair_stats": { ... PairStats.to_dict() ... }
  },
  "risk": {
    "daily_loss": 0.01,
    "daily_loss_limit": 2.0,
    "stop_floor": -10.0
  }
}

## 12. Exit Lifecycle Management (Stale Exits, S2 Break-Glass, Recovery)

The pair strategy assumes price oscillates near market. When price trends,
exits strand and the engine stalls. This section defines a graduated
response system that detects stalls, reprices exits, breaks deadlocks,
and exploits directional signals.

### New Config Parameters

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `EXIT_REPRICE_MULTIPLIER` | 1.5 | Reprice exit after this × `median_duration_sec` |
| `EXIT_ORPHAN_MULTIPLIER` | 5.0 | Orphan exit after this × `median_duration_sec` |
| `MAX_RECOVERY_SLOTS` | 2 | Max orphaned exits kept on Kraken book |
| `S2_MAX_SPREAD_PCT` | 3.0 | Max tolerable gap between exits in S2 (%) |
| `REPRICE_COOLDOWN_SEC` | 120 | Min seconds between reprices of same exit |
| `MIN_CYCLES_FOR_TIMING` | 5 | Don't use timing-based logic until N cycles complete |
| `DIRECTIONAL_ASYMMETRY` | 0.5 | Entry distance multiplier for with-trend side (0.3–0.8) |

### New Persisted Fields (state.json additions)

| Field | Type | Purpose |
|-------|------|-------------|
| `recovery_orders` | list[RecoveryOrder] | Orphaned exits still on Kraken book |
| `s2_entered_at` | float\|null | Unix timestamp when S2 was entered |
| `last_reprice_a` | float | Timestamp of last Trade A exit reprice |
| `last_reprice_b` | float | Timestamp of last Trade B exit reprice |
| `exit_reprice_count_a` | int | Times Trade A exit has been repriced this cycle |
| `exit_reprice_count_b` | int | Times Trade B exit has been repriced this cycle |
| `detected_trend` | str\|null | "up", "down", or null |
| `trend_detected_at` | float\|null | When trend was detected |
| `stdev_duration_sec` | float\|null | Added to PairStats |

### RecoveryOrder Schema

| Field | Type | Description |
|-------|------|-------------|
| `txid` | str | Kraken order ID (still live on book) |
| `trade_id` | str | Original trade identity ("A" or "B") |
| `cycle` | int | Original cycle number |
| `side` | str | "buy" or "sell" |
| `price` | float | Order price |
| `volume` | float | Order volume |
| `entry_price` | float | Original entry fill price (for P&L calc) |
| `orphaned_at` | float | Unix timestamp when orphaned |
| `reason` | str | "timeout", "s2_break", "repriced_out" |

### 12.1 Timing Thresholds

All timing-based decisions require `MIN_CYCLES_FOR_TIMING` completed
cycles (default 5). Until then, the bot operates without repricing
or orphaning (original behavior).

```
compute_exit_thresholds(pair_stats):
    │
    ├── pair_stats.n_total < MIN_CYCLES_FOR_TIMING?
    │     YES → return None (disable timing logic)
    │
    ├── reprice_after = median_duration_sec × EXIT_REPRICE_MULTIPLIER
    │                   (default: median × 1.5)
    │
    ├── orphan_after  = median_duration_sec × EXIT_ORPHAN_MULTIPLIER
    │                   (default: median × 5.0)
    │
    └── return { reprice_after, orphan_after }
```

**New PairStats field**: `stdev_duration_sec` — sample standard deviation
of cycle durations. Computed alongside `mean_duration_sec` from
`CompletedCycle.exit_time - CompletedCycle.entry_time`. None when n < 2.

### 12.2 Single-Exit Repricing (S1a / S1b)

Runs in the main loop as a new step `CHECK_STALE_EXITS` after
`CHECK_DRIFT`, before `AI_COUNCIL`. Only active in S1a or S1b
(one exit + one entry).

```
CHECK_STALE_EXITS (S1a or S1b only)
    │
    ├── thresholds = compute_exit_thresholds(pair_stats)
    │     NULL → skip (not enough data)
    │
    ├── identify the open exit order
    │     S1a: Trade A buy exit (matched_sell_price known)
    │     S1b: Trade B sell exit (matched_buy_price known)
    │
    ├── exit_age = now - exit_placed_time
    │
    ├── exit_age < thresholds.reprice_after?
    │     YES → skip (still in normal range)
    │
    ├── exit_age >= thresholds.orphan_after?
    │     YES → jump to §12.4 ORPHAN LOGIC
    │
    ├── last_reprice < REPRICE_COOLDOWN_SEC ago?
    │     YES → skip (cooldown active)
    │
    ▼ REPRICE ELIGIBLE
    │
    ├── Compute new exit price:
    │     new_price = _pair_exit_price(entry_fill_price, market, side, state)
    │     (reuses existing function — market guard ensures minimum profit)
    │
    ├── SAFETY: Is new price CLOSER to market than current?
    │     NO  → skip (one-way ratchet: only tighten, never loosen)
    │
    ├── SAFETY: Would this exit still be profitable?
    │     Trade B: new_price > matched_buy_price + estimated_fees?
    │     Trade A: new_price < matched_sell_price - estimated_fees?
    │     NO  → skip (don't reprice into a loss)
    │
    ├── SAFETY: Price improvement meaningful? (> 0.1% closer)
    │     NO  → skip (avoid churn)
    │
    ▼ EXECUTE REPRICE
    │
    ├── cancel old exit order on Kraken
    ├── place new exit order at new_price
    │     (preserve trade_id, cycle, role=exit)
    ├── increment exit_reprice_count
    ├── set last_reprice timestamp
    ├── log: "EXIT REPRICED: B sell $0.09950 → $0.09838 (age: 45m, profit: 0.6%→0.3%)"
    └── save_state()
```

#### Reprice Tiering (Progressive Tightening)

Rather than jumping to minimum-profit on the first reprice, the bot
tightens gradually based on how many times this exit has been repriced:

```
reprice_count == 0 (first reprice):
    target = midpoint(original_exit, market_guard_minimum)
    (accept ~half the original profit target)

reprice_count == 1:
    target = market_guard_minimum
    (accept whatever profit the market guard allows)

reprice_count >= 2:
    target = market_guard_minimum (same as count 1)
    (if still stranded after 2 reprices → heading toward orphan threshold)
```

### 12.3 S2 Break-Glass Protocol

S2 means both entries filled and both exits are on the book. The engine
is fully stalled — no entries, no fills, no profit. This is the most
urgent condition.

```
CHECK_S2_BREAK_GLASS (runs only when pair_state == S2)
    │
    ├── Record s2_entered_at (if not already set)
    │
    ├── thresholds = compute_exit_thresholds(pair_stats)
    │     NULL → use fallback: S2_FALLBACK_TIMEOUT = 600s (10 min)
    │
    ├── s2_age = now - s2_entered_at
    │
    ├── PHASE 1: NATURAL RESOLUTION WINDOW
    │     s2_age < thresholds.reprice_after?
    │     YES → skip (give exits time to fill naturally)
    │
    ├── PHASE 2: EVALUATE THE SPREAD
    │     sell_exit_price = Trade B exit price
    │     buy_exit_price  = Trade A exit price
    │     spread_pct = (sell_exit_price - buy_exit_price) / market × 100
    │
    │     spread_pct < S2_MAX_SPREAD_PCT?
    │     YES → skip (spread is tolerable, wait longer)
    │
    ├── PHASE 3: IDENTIFY THE WORSE TRADE
    │     │
    │     ├── a_distance = abs(buy_exit_price - market) / market
    │     ├── b_distance = abs(sell_exit_price - market) / market
    │     │
    │     ├── worse_trade = trade with LARGER distance from market
    │     │   (this is the one less likely to fill)
    │     │
    │     ├── better_trade = the other one
    │     │   (closer to market, more likely to fill on its own)
    │
    ├── PHASE 4: OPPORTUNITY COST CHECK
    │     │
    │     ├── mean_profit_per_sec = pair_stats.mean_net / pair_stats.mean_duration_sec
    │     │   (expected earnings rate when engine is running)
    │     │
    │     ├── foregone_profit = mean_profit_per_sec × s2_age
    │     │
    │     ├── loss_if_close = compute loss from closing worse trade at market
    │     │     Trade B (sell exit stranded high):
    │     │       loss = (matched_buy_price - market) × volume + est_fees
    │     │       (bought high, selling at current lower price)
    │     │     Trade A (buy exit stranded low):
    │     │       loss = (market - matched_sell_price) × volume + est_fees
    │     │       (sold low, buying back at current higher price)
    │     │
    │     ├── foregone_profit > abs(loss_if_close)?
    │     │     NO  → try REPRICE first (Phase 5)
    │     │     YES → CLOSE the worse trade (Phase 6)
    │
    ├── PHASE 5: S2 REPRICE (attempt before closing)
    │     │
    │     ├── Reprice BOTH exits using tiered repricing (§12.2)
    │     ├── If new spread < S2_MAX_SPREAD_PCT → done, wait for fills
    │     ├── If still too wide → proceed to Phase 6
    │     └── Reprice cooldown applies (won't re-enter Phase 5 for N sec)
    │
    ├── PHASE 6: CLOSE WORSE TRADE
    │     │
    │     ├── OPTION A: ORPHAN (recovery slot available)
    │     │     len(recovery_orders) < MAX_RECOVERY_SLOTS?
    │     │     YES →
    │     │       1. Move worse exit to recovery_orders[]
    │     │          (order stays on Kraken book as a recovery ticket)
    │     │       2. Place new entry for that side at market distance
    │     │       3. Increment cycle number for that trade
    │     │       4. State transitions: S2 → S1a or S1b
    │     │       5. Log
    │     │       6. Set detected_trend (see §12.5)
    │     │
    │     ├── OPTION B: CLOSE AT LOSS (no recovery slots)
    │     │     len(recovery_orders) >= MAX_RECOVERY_SLOTS?
    │     │     YES →
    │     │       1. Cancel worse exit on Kraken
    │     │       2. Book the realized loss in total_profit_usd
    │     │       3. Create CompletedCycle with negative net_profit
    │     │       4. Place new entry for that side at market distance
    │     │       5. Increment cycle number
    │     │       6. State transitions: S2 → S1a or S1b
    │     │       7. Telegram notification (loss event)
    │
    └── Reset s2_entered_at = null (no longer in S2)
```

### 12.4 Orphan → Recovery Pipeline

When an exit is orphaned (removed from the active pair state machine
but left on the Kraken book), it becomes a recovery order.

```
ORPHAN EXIT
    │
    ├── Remove order from active pair tracking
    │
    ├── Append to recovery_orders[]:
    │     RecoveryOrder {
    │       txid, trade_id, cycle, side, price, volume,
    │       entry_price, orphaned_at, reason
    │     }
    │
    ├── Place new entry for that side (engine resumes)
    │
    └── State recalculated by _compute_pair_state()
```

#### Recovery Order Monitoring (per main loop iteration)

```
CHECK_RECOVERY_ORDERS (runs every cycle, after CHECK_STALE_EXITS)
    │
    for each recovery_order in recovery_orders[]:
    │
    ├── STATUS: filled → RECOVERY SUCCESS
    │     Book the original round trip P&L
    │     Create CompletedCycle, remove from recovery_orders[]
    │
    ├── STATUS: cancelled/expired → Book loss, remove
    │
    ├── STATUS: open
    │     └── recovery_age > MAX_RECOVERY_AGE (24h)?
    │           YES → Cancel, book loss, remove
    │           NO  → keep (free lottery ticket)
    │
    └── continue
```

### 12.5 Directional Signal Detection

Which side stalls reveals trend direction. This signal feeds back into
entry placement for the next cycle.

```
DETECT_TREND (called during orphan/reprice events)
    │
    ├── B sell exit orphaned/repriced → price trending DOWN
    │     set detected_trend = "down"
    │
    ├── A buy exit orphaned/repriced → price trending UP
    │     set detected_trend = "up"
    │
    ├── Round trip completes normally → trend weakening
    │     if trend_age > 5 × median_duration_sec:
    │       set detected_trend = null (expired)
    │
    └── Both sides cycling normally for 3+ cycles → clear trend
```

#### Asymmetric Entry Placement

When a trend is detected, adjust entry distances:

```
detected_trend == "down":
    a_entry_pct = base_entry_pct × DIRECTIONAL_ASYMMETRY  (closer sell entries)
    b_entry_pct = base_entry_pct × (2 - DIRECTIONAL_ASYMMETRY)  (wider buy entries)

detected_trend == "up":
    a_entry_pct = base_entry_pct × (2 - DIRECTIONAL_ASYMMETRY)  (wider sell entries)
    b_entry_pct = base_entry_pct × DIRECTIONAL_ASYMMETRY  (closer buy entries)
```

### 12.6 Main Loop Integration

Updated main loop order:

```
CHECK_FILLS → CHECK_DRIFT → ★CHECK_STALE_EXITS → ★CHECK_RECOVERY_ORDERS → AI_COUNCIL → ...
```

### 12.7 Updated Transition Summary

| From | To | Trigger | Action |
|------|----|---------|--------|
| S1a/S1b | S1a/S1b | exit age > reprice threshold | reprice exit closer to market |
| S2 | S1a/S1b | S2 break-glass: orphan worse exit | move to recovery, restart entry |
| S2 | S1a/S1b | S2 break-glass: close worse exit (slots full) | close at market, book loss, restart entry |
| S1a/S1b | S0 | recovery order fills | book delayed round-trip profit |
| S2 | S1a/S1b | S2 reprice tightens spread below threshold | repriced exits, wait for natural fill |

### 12.8 Dashboard Additions

| Element | Location | Content |
|---------|----------|---------|
| Exit age badge | A/B panels | "Exit open 45m (median: 20m)" with color: green < 1×, yellow 1-3×, red > 3× |
| Recovery orders card | Below A/B panels | List of orphaned exits with price, age, unrealized P&L |
| Trend indicator | Top metrics | "↓ DOWN" / "↑ UP" / "—" with timestamp |
| S2 timer | State banner | "S2 for 47m — break-glass at 90m" with countdown |
| Opportunity cost | S2 state banner | "Foregone: ~$0.05 \| Close cost: $0.09" |

### 12.9 Pair Mode Order Count (Updated)

Original invariant: "exactly 2 open orders under normal operation."

New invariant: **2 active orders + 0–N recovery orders** (where N ≤ `MAX_RECOVERY_SLOTS`).

Total open on Kraken: 2 + len(recovery_orders), typical 2-4, max 4.

Reconciliation on startup must scan for recovery orders
(by matching against `state.recovery_orders[].txid`).

### 12.10 Risk Integration

Recovery orders carry position risk. Update risk calculations:

```
compute_total_exposure(state, current_price):
    active_exposure = existing pair unrealized P&L
    recovery_exposure = sum of recovery order mark-to-market
    total_exposure = active_exposure + recovery_exposure
    estimated_value = STARTING_CAPITAL + total_profit_usd + total_exposure
```

### 12.11 Edge Cases

| Scenario | Handling |
|----------|----------|
| Bot restarts with recovery orders | Reconcile: match txids against Kraken. Filled → book profit. Cancelled → book loss. |
| Recovery order partially fills | Treat as filled (Kraken doesn't partial-fill limits at this size) |
| S2 entered but no PairStats yet | Use fallback timeout (600s) |
| Both exits reprice to same price | Impossible — A exits are buys (below market), B exits are sells (above market) |
| Trend flips during recovery | `detected_trend` updates on next event. Old recovery orders stay (benefit from reversal) |
| Recovery order fills WHILE in S2 | Book profit, free slot. Proceed with break-glass as normal |
| Price flash-crashes through all exits | Both exits fill → normal resolution. Recovery orders also fill. Best case. |
| `MAX_RECOVERY_SLOTS = 0` | Disables orphaning. S2 break-glass goes straight to close-at-market. |
