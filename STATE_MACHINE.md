# DOGE State-Machine Bot v1

Last updated: 2026-02-15 (rev 3)
Primary code references: `bot.py`, `state_machine.py`, `config.py`, `dashboard.py`, `supabase_store.py`, `hmm_regime_detector.py`

## 1. Scope

This document is the implementation contract for the current runtime.

- Market: Kraken `XDGUSD` (`DOGE/USD`) only
- Strategy: slot-based pair engine (`A` and `B` legs) with independent per-slot compounding
- Persistence: Supabase-first (`bot_state`, `fills`, `price_history`, `ohlcv_candles`, `bot_events`, `exit_outcomes`, `regime_tier_transitions`)
- Execution: fully rule-based reducer; HMM regime detector + directional regime system are advisory/actuation layers in `bot.py` runtime (no reducer modifications)
- Control plane: dashboard + Telegram commands

Out of scope for v1:

- Multi-pair swarm (implemented but not active in production)
- Factory visualization mode (specced in `docs/FACTORY_LENS_SPEC.md`, not implemented)

## 2. System Overview

Runtime is split into two layers.

- `state_machine.py`: pure reducer (`transition`) and invariant checker (`check_invariants`)
- `bot.py`: exchange I/O, reconciliation, bootstrap, loop budget, APIs, Telegram, persistence

Core data model (`PairState`) is per slot and contains:

- Active orders (`orders`)
- Orphaned exits (`recovery_orders`)
- Completed cycles (`completed_cycles`)
- Dual realized trackers:
  - `total_profit` (net cycle PnL after both fill fees)
  - `total_settled_usd` (estimated quote-balance USD delta)
- Regime vintage tags on orders/recoveries/cycles (`regime_at_entry`: `0`/`1`/`2` or `None`)
- Cycle counters (`cycle_a`, `cycle_b`)
- Risk counters and cooldown timers
- Mode flags (`long_only`, `short_only`, `mode_source`)

## 3. Top-Level Lifecycle

```text
START
  -> INIT (logging, signals, Supabase writer, HMM runtime)
  -> LOAD SNAPSHOT (Supabase key __v1__, restore HMM state)
  -> FETCH CONSTRAINTS + FEES (Kraken)
  -> FETCH INITIAL PRICE (strict)
  -> SYNC OHLCV + OPTIONAL BACKFILL + PRIME HMM (initial pull, warmup backfill, first train)
  -> RECONCILE TRACKED ORDERS
  -> REPLAY MISSED FILLS (trade history)
  -> ENSURE BOOTSTRAP PER SLOT
  -> RUNNING LOOP
      -> PAUSED (operator, daily loss lock, or guardrail)
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
4. **OHLCV sync** (`_sync_ohlcv_candles`): pull Kraken OHLC candles (default 1m + optional 15m secondary), queue upserts to Supabase (see §16).
5. Compute volatility-adaptive runtime profit target.
6. **Daily loss lock check** (`_update_daily_loss_lock`): auto-pauses if aggregate UTC-day loss exceeds `DAILY_LOSS_LIMIT`.
7. **Rebalancer update** (`_update_rebalancer`): every `REBALANCE_INTERVAL_SEC`. Currently disabled (`REBALANCE_ENABLED=False`) — conflicts with directional regime system (see §18).
8. **Regime tier evaluation** (`_update_regime_tier`): every `REGIME_EVAL_INTERVAL_SEC` (default 300s). Triggers `_update_hmm()` if HMM is stale, then evaluates tier 0/1/2 (see §18).
9. **Tier 2 suppression** (`_apply_tier2_suppression`): runs every loop. Cancels against-trend entries in S0 slots when tier 2 is active and grace has elapsed (see §18.3).
10. **Entry scheduler pre-tick drain**: drain up to `MAX_ENTRY_ADDS_PER_LOOP` deferred entries. Purges suppressed-side deferred entries during tier 2.
11. For each slot:
    - Apply `PriceTick`
    - Apply `TimerTick`
    - Call `_ensure_slot_bootstrapped(slot_id)` — respects regime suppression (see §18.4)
    - Call `_auto_repair_degraded_slot(slot_id)` — skips slots with `mode_source="regime"` (see §18.5)
12. Post-tick entry drain (`_drain_pending_entry_orders`).
13. Poll status for all tracked order txids (`_poll_order_status`).
14. Refresh pair open-order telemetry (`_refresh_open_order_telemetry`) when budget allows.
15. **Auto soft-close** (`_auto_soft_close_if_capacity_pressure`): reprices farthest recoveries when utilization exceeds threshold.
16. **Persistent open-order drift alert** (`_maybe_alert_persistent_open_order_drift`).
17. Emit orphan-pressure notification at `ORPHAN_PRESSURE_WARN_AT` multiples.
18. Persist snapshot (`save_state` to `bot_state`).
19. Poll Telegram commands.
20. `end_loop()` resets budget/cache.

## 5. Pair Phases (`S0`, `S1a`, `S1b`, `S2`)

Phase is derived from order roles/sides.

- `S0`: entry phase (normal two-sided or degraded one-sided)
- `S1a`: A is in position (`buy exit` exists)
- `S1b`: B is in position (`sell exit` exists)
- `S2`: both exits pending (both positions open)

Trade semantics:

- `A`: sell entry, buy exit (short side)
- `B`: buy entry, sell exit (long side)

Fallback flags:

- `long_only=True`: only B-side entry flow should continue
- `short_only=True`: only A-side entry flow should continue

Mode source (`mode_source` on `PairState`):

- `"none"`: default symmetric state
- `"balance"`: one-sided due to insufficient balance for one side
- `"regime"`: one-sided due to directional regime suppression (§18)

## 6. Reducer Contract

`transition(state, event, cfg, order_size_usd) -> (next_state, actions)`

`EngineConfig` fields:

- `entry_pct`: base entry distance from market (sacred — never modified by rebalancer)
- `entry_pct_a`: per-trade override for A-side entry distance (set by regime spacing bias §18.2, or `None` for base)
- `entry_pct_b`: per-trade override for B-side entry distance (set by regime spacing bias §18.2, or `None` for base)
- `profit_pct`, `refresh_pct`, `max_consecutive_refreshes`, `refresh_cooldown_sec`, etc.

Entry distance selection (`_entry_pct_for_trade`): uses `entry_pct_a` for trade A or `entry_pct_b` for trade B when set and >0; otherwise falls back to base `entry_pct`.

Properties:

- Pure function, no network side effects
- Side effects are represented as actions:
  - `PlaceOrderAction`
  - `CancelOrderAction`
  - `OrphanOrderAction`
  - `BookCycleAction`

All exchange effects happen in runtime after reducer returns.

Runtime patch helpers in `state_machine.py` preserve reducer purity while allowing
runtime annotations:

- `apply_order_txid(...)` / `apply_recovery_txid(...)` bind exchange txids
- `apply_order_regime_at_entry(...)` tags an order with regime vintage metadata

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
- **Base reentry cooldown** (`REENTRY_BASE_COOLDOWN_SEC`): applied to `cooldown_until_a`/`cooldown_until_b` after every cycle close or orphan event, independent of P&L.

### 7.3 `FillEvent` for active orders

If filled order is an entry:

- Remove entry from active set
- Add opposite-side exit with:
  - exact filled volume
  - exit price via `_exit_price(...)`
  - `regime_at_entry` copied from the filled entry order
- Book entry fee immediately into `total_fees`
- Emit `PlaceOrderAction` for new exit
- Runtime stamps the newly-created exit order's `regime_at_entry` using current
  policy HMM regime (`_current_regime_id`) before exchange placement.

If filled order is an exit:

- Book completed cycle (`_book_cycle`)
- Booked cycle now includes fee split (`entry_fee`, `exit_fee`), quote-fee estimate
  (`quote_fee`), and quote-settlement estimate (`settled_usd`)
- Booked cycle carries `regime_at_entry` from the filled exit order
- Update realized loss counters, cooldown timers, and cumulative settlement trackers
- Increment cycle counter for that trade (`cycle_a` or `cycle_b`)
- Attempt follow-up entry for same trade unless blocked by:
  - fallback mode (`long_only` / `short_only`)
  - loss cooldown
  - base reentry cooldown

### 7.4 `RecoveryFillEvent`

- Remove recovery record
- Book cycle as `from_recovery=True`
- Booked cycle carries `regime_at_entry` from the recovery record
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
- If target size is below Kraken minimum constraints (`min_volume=13 DOGE`, `min_cost_usd` from Kraken), no action is returned and slot waits.
- Diagnostic log shows `ORDER_SIZE_USD`, `total_profit`, `min_vol`, `min_cost`, and `market` when waiting.

## 9. Order Sizing (`_slot_order_size_usd`)

Per-slot order size computation:

```
base = max(ORDER_SIZE_USD, ORDER_SIZE_USD + slot.total_profit)
layer_usd = effective_layers * CAPITAL_LAYER_DOGE_PER_ORDER * market_price
base_with_layers = max(base, base + layer_usd)
```

B-side sizing behavior:

- Base remains account-aware (`available_usd / slot_count`, floored by `ORDER_SIZE_USD`)
- A dust dividend (when enabled) redistributes free-USD surplus across
  buy-ready slots each loop so realized gains are absorbed instead of idling

Kelly criterion sizing (master toggle `KELLY_ENABLED`) is an advisory layer
applied after `base_with_layers` and before rebalancer skew.

Kelly integration contract:

- Runtime component: `KellySizer` (`kelly_sizer.py`) initialized in `bot.py`
  from `KellyConfig`.
- Update cadence: `_update_kelly()` is called during regime evaluation cadence
  (`_update_regime_tier`) and recomputes Kelly stats from slot
  `completed_cycles`.
- Data used per cycle: `net_profit`, `exit_time`, and `regime_at_entry`.
- Buckets: `aggregate` plus regime-specific buckets (`bearish`, `ranging`,
  `bullish`).
- Sample gates:
  - global gate (`KELLY_MIN_SAMPLES`) for aggregate Kelly activation
  - per-regime gate (`KELLY_MIN_REGIME_SAMPLES`) for regime-specific Kelly activation
- Sizing application (`KellySizer.size_for_slot`):
  - prefer current regime bucket; fallback to aggregate
  - if insufficient data, passthrough base size unchanged
  - if no edge, use `KELLY_NEGATIVE_EDGE_MULT` (clamped by floor/ceiling)
  - otherwise apply Kelly multiplier (clamped by floor/ceiling)

Detailed contracts and rollout guidance: `KELLY_SPEC.md`,
`docs/KELLY_IMPLEMENTATION_PLAN.md`, `docs/KELLY_DEPLOY_CHECKLIST.md`.

If rebalancer is enabled and skew is nonzero, the favored side is scaled up:

```
mult = min(MAX_SIZE_MULT, 1.0 + abs(skew) * REBALANCE_SIZE_SENSITIVITY)
effective = base_with_layers * mult
```

Fund guard: scaling never exceeds available balance for the favored side.

### 9.1 Kelly Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `KELLY_ENABLED` | False | Master toggle for Kelly sizing |
| `KELLY_FRACTION` | 0.25 | Fractional Kelly factor |
| `KELLY_MIN_SAMPLES` | 30 | Minimum aggregate samples before activation |
| `KELLY_MIN_REGIME_SAMPLES` | 15 | Minimum per-regime samples for regime-specific sizing |
| `KELLY_LOOKBACK` | 500 | Rolling cycle lookback window |
| `KELLY_FLOOR_MULT` | 0.5 | Minimum size multiplier clamp |
| `KELLY_CEILING_MULT` | 2.0 | Maximum size multiplier clamp |
| `KELLY_NEGATIVE_EDGE_MULT` | 0.5 | Multiplier when Kelly says no edge |
| `KELLY_RECENCY_WEIGHTING` | True | Enable recency weighting |
| `KELLY_RECENCY_HALFLIFE` | 100 | Recency weighting half-life (cycles) |
| `KELLY_LOG_UPDATES` | True | Emit Kelly summary logs |

## 10. Invariants and Halt Policy

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

Three explicit runtime bypasses for recoverable startup gaps:

1. Min-size wait state (`_is_min_size_wait_state`)
   - S0 violation allowed when target order size is below Kraken minimum requirements
2. Bootstrap pending state (`_is_bootstrap_pending_state`)
   - S0 violation allowed when there are no exits and <=1 entry (temporary bootstrap gap)
3. `_normalize_slot_mode(slot_id)` aligns fallback flags with actual one-sided entry state.
   Called after action execution and on placement failure/skip paths.

## 11. Recovery/Orphan Lifecycle

Orphaning converts an active exit to a recovery order while keeping Kraken order alive.

- Recovery orders are stored in `recovery_orders`
- Recovery orders are not auto-cancelled by lifecycle logic
- **Hard cap**: `MAX_RECOVERY_SLOTS` (default 2) per slot. When cap reached, oldest recovery is evicted with loss booked via `_book_cycle()` pseudo-order. Prevents unbounded orphan accumulation.
- Pressure warning sent at `ORPHAN_PRESSURE_WARN_AT` multiples.

Auto soft-close (capacity governor):

- When open-order utilization exceeds `AUTO_SOFT_CLOSE_CAPACITY_PCT` (default 95%), auto-reprices `AUTO_SOFT_CLOSE_BATCH` (default 2) farthest recoveries to near-market price.
- Runs every main loop cycle.
- Tracks lifetime total (`_auto_soft_close_total`).

Manual soft-close:

- Dashboard `soft_close` / `soft_close_next`
- Dashboard `cancel_stale_recoveries` (bulk operation on distant recoveries)
- Telegram `/soft_close` interactive picker or direct args
- Soft-close reprices recovery toward market and updates txid

## 12. Entry Velocity Scheduler

Rate-limits entry order placement to prevent API budget exhaustion and order-book flooding.

- `MAX_ENTRY_ADDS_PER_LOOP` (default 2): hard cap on entries placed per loop cycle.
- Excess entries are deferred to a pending queue (`_pending_entry_orders`).
- Deferred entries are drained pre-tick and post-tick, up to remaining budget.
- Cap dynamically tightens based on open-order headroom:
  - headroom < 5: cap = 1/loop
  - headroom < 10: cap = 2/loop
  - headroom < 20: cap = 3/loop
  - otherwise: configured default

## 13. Daily Loss Lock

UTC-day-based aggregate realized-loss circuit breaker.

- `_compute_daily_realized_loss_utc()`: sums negative `net_profit` from all slots' `completed_cycles` where `exit_time` falls within the current UTC day.
- `_update_daily_loss_lock()`: called pre-tick and post-tick in main loop.
- If daily loss >= `DAILY_LOSS_LIMIT` (default $3): sets `_daily_loss_lock_active = True` and pauses bot.
- Lock auto-clears on UTC rollover (different day).
- Lock also clears when limit is raised above current loss or disabled (`<= 0`).
- Resume is blocked while lock is active.
- Counts both normal cycle losses and recovery eviction booked losses.

## 14. Inventory Rebalancer (Currently Disabled)

> **Note:** `REBALANCE_ENABLED=False` in production. The rebalancer's size-skew
> actuator conflicts with the directional regime system (§18) — the rebalancer
> may push "sell DOGE" while the regime says BULLISH. The regime system supersedes
> rebalancer for directional adaptation. Full removal is deferred.

PD controller that adjusts entry order sizes to maintain target idle-USD ratio.

### 14.1 Core PD Controller

- **Input**: `idle_ratio = idle_USD / total_portfolio_value`
- **Target**: `_compute_dynamic_idle_target()` (see §15)
- **Error**: `idle_ratio - target`
- **Output**: `skew` in range `[-MAX_SKEW, +MAX_SKEW]` (default ±0.30)
- Update frequency: every `REBALANCE_INTERVAL_SEC` (default 300s)
- EMA smoothing on error and velocity with configurable halflife
- Sign-flip damping: if skew direction changed, increment flip counter; if too many flips in 1h, zero output
- Neutral band: if `|error| < REBALANCE_NEUTRAL_BAND`, skew = 0
- Slew rate limit: max `REBALANCE_MAX_SLEW_RATE` change per update

### 14.2 Size-Skew Actuator

- Positive skew → B-side (buy DOGE) orders scaled up by `1 + |skew| * sensitivity`
- Negative skew → A-side (sell DOGE) orders scaled up
- Max multiplier: `REBALANCE_MAX_SIZE_MULT` (default 1.5)
- One-sided only: excess side scales up; deficit side stays at base
- Fund guard prevents scaling beyond available balance

### 14.3 Design Constraints (locked)

1. No market orders. All rebalancing through limit-order size skew.
2. No new order flow. Only adjusts size of orders the grid would place anyway.
3. `entry_pct` is sacred. Never touched by the rebalancer.
4. Capacity gating: if capacity band is "caution" or "stop", skew zeroes.

### 14.4 Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `REBALANCE_ENABLED` | True | Master enable |
| `REBALANCE_ON_S1` | True | Apply skew during S1 phase |
| `REBALANCE_TARGET_IDLE_PCT` | 0.40 | Static base idle target |
| `REBALANCE_KP` | 0.6 | Proportional gain |
| `REBALANCE_KD` | 0.2 | Derivative gain |
| `REBALANCE_MAX_SKEW` | 0.30 | Output clamp |
| `REBALANCE_SIZE_SENSITIVITY` | 1.0 | Skew-to-multiplier scaling |
| `REBALANCE_MAX_SIZE_MULT` | 1.5 | Max order size multiplier |
| `REBALANCE_NEUTRAL_BAND` | 0.05 | Dead zone around target |
| `REBALANCE_INTERVAL_SEC` | 300.0 | Update period |

## 15. Dynamic Idle Target

Trend-adaptive idle target for the rebalancer PD controller. See `docs/DYNAMIC_IDLE_TARGET_SPEC.md` for full spec.

### 15.1 Trend Score

Dual-EMA crossover signal:

```
trend_score = (fast_ema - slow_ema) / slow_ema    # ratio form
```

- Positive: DOGE rising → deploy USD (lower idle target)
- Negative: DOGE falling → hold USD (raise idle target)
- Updated on each rebalancer tick from `self.last_price`

### 15.2 Target Mapping

When HMM is enabled, trained, and available (see §17), `trend_score` is blended with the HMM `bias_signal`:

```
signal = HMM_BLEND_WITH_TREND * trend_score + (1 - HMM_BLEND_WITH_TREND) * hmm_bias
raw_target = REBALANCE_TARGET_IDLE_PCT - TREND_IDLE_SENSITIVITY * signal
dynamic_target = clamp(raw_target, TREND_IDLE_FLOOR, TREND_IDLE_CEILING)
```

When HMM is disabled or unavailable, `signal = trend_score` (no blend).

### 15.3 Hysteresis

Three stages in strict order:

1. **Dead zone**: if `|trend_score| < TREND_DEAD_ZONE` (0.001), return base target.
2. **Time-hold**: if hold active, freeze output and smoothing state.
3. **Smoothing**: EMA on clamped target; large jumps (>0.02) trigger new hold.

### 15.4 Cold Start and Restart

- Cold start (both EMAs zero + insufficient samples): initialize to current price, score = 0.
- Restart with persisted EMAs: use directly, no reset.
- Data gap > 2x slow halflife: reinitialize EMAs to current price.

### 15.5 Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TREND_FAST_HALFLIFE` | 1800.0 | Fast EMA halflife (30 min) |
| `TREND_SLOW_HALFLIFE` | 14400.0 | Slow EMA halflife (4 hr) |
| `TREND_IDLE_SENSITIVITY` | 5.0 | Target shift per unit score |
| `TREND_IDLE_FLOOR` | 0.15 | Min target (strong uptrend) |
| `TREND_IDLE_CEILING` | 0.60 | Max target (strong downtrend) |
| `TREND_MIN_SAMPLES` | 24 | Cold-start sample threshold |
| `TREND_DEAD_ZONE` | 0.001 | Score magnitude dead zone |
| `TREND_HYSTERESIS_SEC` | 600.0 | Hold duration |
| `TREND_HYSTERESIS_SMOOTH_HALFLIFE` | 900.0 | Smoothing EMA halflife |

## 16. OHLCV Data Pipeline

Persists interval-based OHLCV candles from Kraken into Supabase for HMM training and inference (default interval: 1 minute).

### 16.1 Collection

- `_sync_ohlcv_candles()`: called every main loop iteration, rate-limited to `HMM_OHLCV_SYNC_INTERVAL_SEC` (default 60s).
- Pulls one page from Kraken's OHLC endpoint (`get_ohlc_page`) using `HMM_OHLCV_INTERVAL_MIN` (default 1).
- Normalizes rows, filters out the still-forming candle, and queues upserts to `ohlcv_candles` via `supabase_store.queue_ohlcv_candles()`.
- Cursor (`_ohlcv_since_cursor`) is persisted in snapshot for incremental fetches across restarts.
- Startup warmup: `_maybe_backfill_ohlcv_on_startup()` may call `backfill_ohlcv_history()` to queue additional historical candles when below target window.
- Backfill pagination starts with `since=None` and advances only via Kraken's
  returned `last` cursor.
- Backfill stall breaker: if repeated backfills produce zero new unique candles,
  attempts are blocked after `HMM_BACKFILL_MAX_STALLS` consecutive stalls until
  operator/manual retry.

### 16.2 Storage

Supabase table `ohlcv_candles`:

- Columns: `time`, `pair`, `interval_min`, `open`, `high`, `low`, `close`, `volume`, `trade_count`
- Unique constraint: `(pair, interval_min, time)` — upsert-safe.
- Retention: `_cleanup_old_ohlcv()` deletes rows older than `HMM_OHLCV_RETENTION_DAYS` (default 14 days, ~4032 candles).

### 16.3 Data Access

- `_fetch_training_candles(count)`: loads up to `HMM_TRAINING_CANDLES` (default 720) candles. Prefers Supabase; falls back to Kraken OHLC if Supabase has insufficient data.
- `_fetch_recent_candles(count)`: loads `HMM_RECENT_CANDLES` (default 100) candles for inference.
- Both return `(closes, volumes)` tuple of float lists.

### 16.4 Readiness Check

`_hmm_data_readiness()` returns a cached diagnostic block:

- `samples`: current candle count in Supabase
- `coverage_pct`: samples / training_target * 100
- `freshness_limit_sec`: stale threshold = `max(180, interval_sec * 3)`
- `freshness_ok`: newest candle age <= `freshness_limit_sec`
- `volume_coverage_pct`: % of candles with non-zero volume
- `ready_for_min_train`: samples >= `HMM_MIN_TRAIN_SAMPLES` and fresh
- `ready_for_target_window`: samples >= `HMM_TRAINING_CANDLES` and fresh
- `gaps`: list of actionable gap descriptions

### 16.5 Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HMM_OHLCV_ENABLED` | True | Master switch for candle collection |
| `HMM_OHLCV_INTERVAL_MIN` | 1 | Candle interval in minutes |
| `HMM_OHLCV_SYNC_INTERVAL_SEC` | 60.0 | How often to pull from Kraken |
| `HMM_OHLCV_RETENTION_DAYS` | 14 | Supabase retention period |
| `HMM_OHLCV_BACKFILL_ON_STARTUP` | True | Attempt warmup backfill on startup |
| `HMM_OHLCV_BACKFILL_MAX_PAGES` | 40 | Max Kraken pages in one backfill run |
| `HMM_BACKFILL_MAX_STALLS` | 3 | Consecutive zero-progress backfills before breaker opens |
| `HMM_TRAINING_CANDLES` | 720 | Target training window size |
| `HMM_RECENT_CANDLES` | 100 | Inference window size |
| `HMM_MIN_TRAIN_SAMPLES` | 500 | Minimum candles for training |
| `HMM_READINESS_CACHE_SEC` | 300.0 | Readiness check cache TTL |

## 17. HMM Regime Detector (Advisory Layer)

Read-only regime classifier that sits alongside the trend detector (§15). Does **not** modify the reducer (§6), emit actions, or touch event transition rules. Lives entirely in `bot.py` runtime.

Module: `hmm_regime_detector.py` (requires `numpy`, `hmmlearn`).

### 17.1 Architecture

```
Kraken OHLC → ohlcv_candles (§16) → FeatureExtractor → GaussianHMM → RegimeState
                                                                          │
                                          bias_signal ─── blend ──► §15 idle target
                                                            ↑
                                                       trend_score
```

Three hidden states: `BEARISH` (0), `RANGING` (1), `BULLISH` (2). Labels assigned post-training by inspecting EMA-spread emission means.

Four observation features: MACD histogram slope, EMA spread %, RSI zone, volume ratio.

### 17.2 Lifecycle

1. **Init** (`_init_hmm_runtime`): imports `numpy` + `hmm_regime_detector` inside try/except. If import fails, logs warning and continues with trend-only logic. Also initializes secondary detector if `HMM_MULTI_TIMEFRAME_ENABLED=True`.
2. **Training** (`_train_hmm` / `_train_hmm_secondary`): called on startup and when `needs_retrain()` returns true (daily by default). Uses `_fetch_training_candles()` from §16.
3. **Inference** (`_update_hmm`): called from `_update_regime_tier()` every `REGIME_EVAL_INTERVAL_SEC` (default 300s). Also called from `_update_rebalancer()` when rebalancer is enabled. Uses `_fetch_recent_candles()`. Updates primary detector, then secondary if multi-timeframe enabled, then recomputes consensus.
4. **Blend**: `signal = blend * trend_score + (1 - blend) * hmm_bias` feeds into §15.2 target mapping.
5. **Persistence**: `_snapshot_hmm_state()` / `_restore_hmm_snapshot()` save/restore `RegimeState` and train timestamp. Model itself is **not** serialized — retrained from candle data on startup.

### 17.3 Degradation Guarantees

- If `HMM_ENABLED=false` (default): no init, no inference, zero overhead.
- If `hmmlearn`/`numpy` not installed: graceful fallback to trend-only.
- If training fails or data insufficient: stays in `RANGING` with zero bias.
- `HMM_BLEND_WITH_TREND=1.0`: pure trend_score, HMM has zero influence (shadow mode).
- Default `RegimeState`: `RANGING`, `bias_signal=0.0`, `confidence=0.0`.

### 17.4 Tuning Phases

| Phase | `HMM_BLEND_WITH_TREND` | Effect |
|-------|------------------------|--------|
| Shadow | 1.0 | HMM classifies but has zero influence |
| Gentle | 0.7 | 30% HMM, 70% trend |
| Equal | 0.5 | Equal weight |
| HMM-primary | 0.3 | 70% HMM, 30% trend |

### 17.5 Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HMM_ENABLED` | False | Master enable |
| `HMM_N_STATES` | 3 | Hidden states |
| `HMM_N_ITER` | 100 | Baum-Welch iterations |
| `HMM_COVARIANCE_TYPE` | "diag" | Gaussian covariance structure |
| `HMM_INFERENCE_WINDOW` | 50 | Observations per inference |
| `HMM_CONFIDENCE_THRESHOLD` | 0.15 | Min confidence for non-zero bias |
| `HMM_RETRAIN_INTERVAL_SEC` | 86400.0 | Retrain period (1 day) |
| `HMM_BIAS_GAIN` | 1.0 | Scales bias magnitude |
| `HMM_BLEND_WITH_TREND` | 0.5 | Blend ratio (0=HMM, 1=trend) |

### 17.6 Multi-Timeframe HMM

Dual-detector architecture: a fast primary (default 1m) and a slow secondary (default 15m) HMM run in parallel. A consensus function merges their signals for policy consumption.

**Source selector** (`_policy_hmm_source`): all policy consumers (regime tier, rebalancer, trend blend) read from a single shared source determined by `HMM_MULTI_TIMEFRAME_SOURCE`:

- `"primary"`: only primary HMM drives policy (secondary collects data but is not consumed)
- `"consensus"`: merged consensus signal drives policy (requires `HMM_MULTI_TIMEFRAME_ENABLED=True`)

**Consensus computation** (`_compute_hmm_consensus`):

- If both HMMs agree on direction: full confidence, weighted blend of bias signals (15m weight 0.7, 1m weight 0.3)
- If 1m is cooling (RANGING) but 15m is directional: 15m direction held with dampened confidence
- If 15m is neutral: fall back to primary signal
- If conflict (opposing directions): RANGING with zero bias (conservative)

**Gated confirmation model**: the 15m sets *direction* (BULLISH/BEARISH gate), the 1m sets *timing* (confidence modulation). Agreement boosts confidence; disagreement forces neutral.

**Secondary OHLCV pipeline**: separate `_sync_ohlcv_candles_secondary()` with its own cursor, sync interval, and backfill state. Uses `HMM_SECONDARY_INTERVAL_MIN` (default 15).

**Activation phases** (config flips, no code changes):

| Phase | Config | Effect |
|-------|--------|--------|
| A (Shadow) | `HMM_SECONDARY_OHLCV_ENABLED=True` | Collect 15m data, no policy influence |
| B (Dual inference) | + `HMM_MULTI_TIMEFRAME_ENABLED=True` | Both HMMs run, consensus computed, source still primary |
| C (Consensus policy) | + `HMM_MULTI_TIMEFRAME_SOURCE=consensus` | Consensus drives all policy consumers |

### 17.7 Multi-Timeframe Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HMM_MULTI_TIMEFRAME_ENABLED` | False | Enable secondary HMM detector |
| `HMM_MULTI_TIMEFRAME_SOURCE` | "primary" | Policy source: "primary" or "consensus" |
| `HMM_SECONDARY_INTERVAL_MIN` | 15 | Secondary candle interval |
| `HMM_SECONDARY_OHLCV_ENABLED` | False | Enable secondary OHLCV collection |
| `HMM_SECONDARY_SYNC_INTERVAL_SEC` | 300.0 | Secondary OHLCV pull cadence |
| `HMM_SECONDARY_TRAINING_CANDLES` | 720 | Secondary training window |
| `HMM_SECONDARY_RECENT_CANDLES` | 50 | Secondary inference window |
| `HMM_SECONDARY_MIN_TRAIN_SAMPLES` | 200 | Minimum candles for secondary training |
| `CONSENSUS_1M_WEIGHT` | 0.3 | 1m weight in consensus blend |
| `CONSENSUS_15M_WEIGHT` | 0.7 | 15m weight in consensus blend |
| `CONSENSUS_DAMPEN_FACTOR` | 0.5 | Confidence dampening on partial agreement |

## 18. Directional Regime System

Three-tier graduated response to directional market conditions. Gated by HMM confidence and bias signal magnitude. The reducer (§6) is **not modified** — all influence flows through `EngineConfig` parameters and runtime suppression in `bot.py`.

Specs: `docs/DIRECTIONAL_REGIME_SPEC.md`, `docs/TIER2_SIDE_SUPPRESSION_SPEC.md`, `docs/REGIME_OBSERVABILITY_AND_STABILITY_SPEC.md`

### 18.1 Tier Model

| Tier | Label | Confidence | Bias Floor | Effect |
|------|-------|-----------|------------|--------|
| 0 | symmetric | < 0.20 | — | No directional adaptation |
| 1 | biased | ≥ 0.20 | abs(bias) ≥ 0.10 | Asymmetric entry spacing via `entry_pct_a`/`entry_pct_b` |
| 2 | directional | ≥ 0.50 | abs(bias) ≥ 0.25 | Side suppression: against-trend entries cancelled, bootstrap one-sided |

**Directional gate**: tier 2 requires regime in (`BULLISH`, `BEARISH`) AND abs(bias_signal) ≥ `REGIME_TIER2_BIAS_FLOOR`. RANGING always forces tier 0.

**Evaluation**: `_update_regime_tier()` runs every `REGIME_EVAL_INTERVAL_SEC` (default 300s). Reads signal from `_policy_hmm_signal()` (shared source selector — respects multi-timeframe config). Triggers `_update_hmm()` if HMM data is stale.

**Stability controls**:

- **Hysteresis** (5%): on downgrades only, re-promotes if confidence is within buffer — but NEVER overrides the directional gate. If RANGING caused the downgrade, hysteresis cannot re-promote.
- **Dwell time** (300s): minimum time at current tier before any transition.
- **Tier 2 re-entry cooldown** (default 600s): after a 2→(1/0) downgrade,
  re-entry into tier 2 is blocked for cooldown duration.
- **Manual override**: `REGIME_MANUAL_OVERRIDE=BULLISH|BEARISH` forces regime with `REGIME_MANUAL_CONFIDENCE`.

### 18.2 Tier 1: Asymmetric Entry Spacing

`_regime_entry_spacing_multipliers()` calls `hmm_regime_detector.compute_grid_bias()` to get per-side multipliers:

- **BULLISH**: A-side (sell entry) spacing widens, B-side (buy entry) spacing tightens
- **BEARISH**: opposite — B widens, A tightens

These feed into `_engine_cfg()` as `entry_pct_a` and `entry_pct_b` on `EngineConfig`. The reducer's `_entry_pct_for_trade()` selects the correct distance per trade.

### 18.3 Tier 2: Side Suppression

`_apply_tier2_suppression()` runs every main loop cycle. When tier 2 is active and grace has elapsed:

1. **Entry cancellation**: cancels against-trend entries in S0 slots on Kraken, removes from state, sets `mode_source="regime"` + `long_only`/`short_only`
2. **Regime-flip cleanup**: if suppressed side changes (BULLISH→BEARISH), clears old-side regime ownership before applying new side
3. **Idempotent**: skips slots already in correct regime mode

Suppressed side mapping:

- BULLISH (bias > 0): suppress A (sell entries), favor B (buy entries)
- BEARISH (bias < 0): suppress B (buy entries), favor A (sell entries)

**Grace period**: `REGIME_SUPPRESSION_GRACE_SEC` (default 60s) delay between tier 2 activation and first cancellation. Prevents premature suppression during tier oscillation.

### 18.4 Bootstrap Regime Awareness

`_ensure_slot_bootstrapped()` checks regime state before placing entries:

- If tier 2 active + grace elapsed + suppressed side set: only place favored-side entry with `mode_source="regime"`
- If tier is below 2 but cooldown is active (`regime_tier2_last_downgrade_at`
  still within `REGIME_TIER2_REENTRY_COOLDOWN_SEC`): bootstrap still honors the
  cooldown-suppressed side until cooldown expiry
- If favored side isn't fundable: wait (never fall back to against-trend)
- Priority: balance constraints > regime signal > symmetric

### 18.5 Auto-Repair Guard

`_auto_repair_degraded_slot()` early-returns when `mode_source="regime"`. This prevents auto-repair from undoing regime suppression by restoring the suppressed side.

### 18.6 Tier Downgrade Cleanup

When tier drops from 2 to lower:

- Runtime records downgrade timestamp and prior suppressed side.
- If `REGIME_TIER2_REENTRY_COOLDOWN_SEC <= 0`, `mode_source="regime"` is
  cleared immediately (legacy behavior).
- If cooldown is enabled (`>0`), regime ownership clear is deferred until
  cooldown expiry (`_clear_expired_regime_cooldown`), and bootstrap continues
  honoring the cooldown-suppressed side in the interim.
- After clear, auto-repair may restore missing sides when balance allows.

### 18.7 Deferred Entry Purge

`_drain_pending_entry_orders()` filters out suppressed-side deferred entries (orders with no txid waiting in queue) during tier 2.

### 18.8 Exit Outcomes (Vintage Data)

`exit_outcomes` Supabase table tracks exit resolution with regime context:

- `regime_at_entry`, `regime_confidence`, `against_trend`, `regime_tier`
- Used for future threshold calibration (Phase 3)

### 18.9 Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `REGIME_DIRECTIONAL_ENABLED` | False | Master actuation switch |
| `REGIME_SHADOW_ENABLED` | False | Shadow-only evaluator (logs tiers, no actuations) |
| `REGIME_TIER1_CONFIDENCE` | 0.20 | Tier 1 confidence threshold |
| `REGIME_TIER2_CONFIDENCE` | 0.50 | Tier 2 confidence threshold |
| `REGIME_TIER1_BIAS_FLOOR` | 0.10 | Min abs(bias) for tier 1 |
| `REGIME_TIER2_BIAS_FLOOR` | 0.25 | Min abs(bias) for tier 2 |
| `REGIME_HYSTERESIS` | 0.05 | Downgrade hysteresis buffer |
| `REGIME_MIN_DWELL_SEC` | 300.0 | Min seconds at current tier |
| `REGIME_SUPPRESSION_GRACE_SEC` | 60.0 | Grace before tier 2 cancellations |
| `REGIME_TIER2_REENTRY_COOLDOWN_SEC` | 600.0 | Holdoff before tier-2 re-entry and suppression clear |
| `REGIME_EVAL_INTERVAL_SEC` | 300.0 | Evaluation frequency |
| `REGIME_MANUAL_OVERRIDE` | "" | Force regime (BULLISH/BEARISH or empty) |
| `REGIME_MANUAL_CONFIDENCE` | 0.75 | Confidence for manual override |

## 19. Capital Layers

Manual vertical scaling system for order sizes.

- Each layer adds `CAPITAL_LAYER_DOGE_PER_ORDER` (default 1.0 DOGE) to every slot's order size.
- `_recompute_effective_layers()`: balance-aware; computes max fundable layers from available DOGE and USD.
- `effective_layers = min(target_layers, max_from_doge, max_from_usd)`
- Dashboard exposes layer metrics: target, effective, funding gap, propagation progress.

### 19.1 Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CAPITAL_LAYER_DOGE_PER_ORDER` | 1.0 | DOGE added per order per layer |
| `CAPITAL_LAYER_ORDER_BUDGET` | 225 | Orders per layer step |
| `CAPITAL_LAYER_BALANCE_BUFFER` | 1.5 | Safety margin for balance check |
| `CAPITAL_LAYER_DEFAULT_SOURCE` | "auto" | Funding source (auto/doge/usd) |

## 20. Slot Aliases

Human-friendly names for slots from configurable pool.

- Default pool: `doge`, `shiba`, `floki`, `cheems`, `kabosu`
- On slot removal, alias is recycled to the pool.
- Fallback when pool exhausted: `doge-NN` (incrementing counter).
- Config: `SLOT_ALIAS_POOL` (comma-separated env var).

## 21. Reconciliation and Exactly-Once Fill Accounting

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

## 22. Persistence Model

Primary snapshot key:

- `bot_state.key = "__v1__"`

Snapshot payload includes:

- runtime mode/pause reason
- global tunables (`entry_pct`, `profit_pct`)
- constraints and fee rates
- `next_slot_id`, `next_event_id`, `seen_fill_txids`
- all per-slot serialized `PairState`
- **rebalancer state**: `_rebalancer_current_skew`, `_rebalancer_smoothed_error`, `_rebalancer_smoothed_velocity`, `_rebalancer_last_update_ts`
- **trend state**: `_trend_fast_ema`, `_trend_slow_ema`, `_trend_score`, `_trend_dynamic_target`, `_trend_smoothed_target`, `_trend_target_locked_until`, `_trend_last_update_ts`
- **daily loss lock state**: `_daily_loss_lock_active`, `_daily_loss_lock_utc_day`, `_daily_realized_loss_utc`
- **capital layer state**: `target_layers`, `effective_layers`
- **OHLCV pipeline state**: `ohlcv_since_cursor`, `ohlcv_last_sync_ts`, `ohlcv_last_candle_ts`
- **HMM backfill state**: `hmm_backfill_last_at`, `hmm_backfill_last_rows`, `hmm_backfill_last_message`, `hmm_backfill_stall_count` (primary + secondary)
- **HMM regime state**: `_hmm_regime_state` (RegimeState dict), `_hmm_last_train_ts`, `_hmm_trained`
- **HMM secondary state**: `hmm_state_secondary`, `hmm_consensus` (multi-timeframe)
- **Kelly state**: `kelly_state` (active regime, last sample count, cached results)
- **Directional regime state**: `regime_tier`, `regime_tier_entered_at`, `regime_tier2_grace_start`, `regime_tier2_last_downgrade_at`, `regime_cooldown_suppressed_side`, `regime_tier_history`, `regime_side_suppressed`, `regime_last_eval_ts`, `regime_shadow_state`

Fields absent from old snapshots default to safe values (backward compatible).

Event log:

- Each transition emits `save_event(...)` row:
  - `event_id`, `timestamp`, `slot_id`, `from_state`, `to_state`, `event_type`, `details`
- Requires Supabase table `bot_events`

If Supabase tables are missing, bot logs warnings and continues running.

## 23. Dashboard and API Contract

HTTP server:

- `GET /` -> dashboard HTML
- `GET /factory` -> factory lens HTML (placeholder)
- `GET /api/status` -> runtime payload
- `GET /api/swarm/status` -> alias to `/api/status` (legacy poller compatibility)
- `POST /api/action` -> control actions

Supported dashboard actions:

- `pause`
- `resume`
- `add_slot`
- `remove_slot` (by slot_id)
- `remove_slots` (by count)
- `add_layer` (with optional source: auto/doge/usd)
- `remove_layer`
- `set_entry_pct`
- `set_profit_pct`
- `soft_close` (specific recovery by slot_id + recovery_id)
- `soft_close_next` (oldest recovery)
- `cancel_stale_recoveries` (bulk soft-close distant recoveries)
- `reconcile_drift` (cancel Kraken-only orders not tracked internally)
- `audit_pnl` (recompute P&L from completed cycles)

### 23.1 `/api/status` Payload Blocks

**`capacity_fill_health`**: manual scaling diagnostics:

- `open_orders_current`, `open_orders_source`, `open_orders_internal`, `open_orders_kraken`
- `open_order_limit_configured`, `open_orders_safe_cap`, `open_order_headroom`
- `open_order_utilization_pct`, `orders_per_slot_estimate`, `estimated_slots_remaining`
- `partial_fill_open_events_1d`, `partial_fill_cancel_events_1d`
- `median_fill_seconds_1d`, `p95_fill_seconds_1d`
- `status_band` (`normal`, `caution`, `stop`)
- `blocked_risk_hint` (string list)

**`rebalancer`**: PD controller state:

- `target` (dynamic), `base_target` (static config), `skew`, `idle_ratio`

**`trend`**: trend detector state:

- `score`, `score_display`, `fast_ema`, `slow_ema`
- `dynamic_idle_target`, `hysteresis_active`, `hysteresis_expires_in_sec`

**`daily_loss_limit`**, **`daily_realized_loss_utc`**, **`daily_loss_lock_active`**, **`daily_loss_lock_utc_day`**: loss circuit breaker state.

**`capital_layers`**: layer metrics including target, effective, gap, funding status.

**`entry_scheduler`**: deferred/drained counts and current loop cap.

**`open_order_drift_hint`**: persistent drift alert state.

**`hmm_data_pipeline`**: OHLCV collection readiness (§16):

- `enabled`, `source`, `interval_min`, `training_target`, `min_train_samples`, `samples`, `coverage_pct`
- `ready_for_min_train`, `ready_for_target_window`, `gaps`
- `freshness_sec`, `freshness_limit_sec`, `freshness_ok`, `volume_coverage_pct`, `span_hours`
- `sync_interval_sec`, `last_sync_ts`, `last_sync_rows_queued`, `sync_cursor`
- `backfill_last_at`, `backfill_last_rows`, `backfill_last_message`
- Stall/breaker signals are surfaced via `backfill_last_message` tokens
  (e.g., `stalls=N`, `backfill_circuit_open:...`)

**`hmm_regime`**: HMM regime detector state (§17):

- `enabled`, `available`, `trained`, `regime`, `regime_id`
- `confidence`, `bias_signal`, `blend_factor`
- `probabilities` (`bearish`, `ranging`, `bullish`)
- `observation_count`, `last_update_ts`, `last_train_ts`, `error`
- `source_mode` (`primary` or `consensus`), `multi_timeframe`, `agreement`
- `primary`, `secondary`, `consensus` (nested sub-objects with per-detector state)

**`hmm_consensus`**: multi-timeframe consensus state (§17.6):

- `regime`, `confidence`, `bias_signal`, `agreement` (`full`, `1m_cooling`, `15m_neutral`, `conflict`, `primary_only`)
- `source_mode`, `multi_timeframe`

**`hmm_data_pipeline_secondary`**: secondary OHLCV pipeline (§16, 15m candles):

- Same fields as `hmm_data_pipeline` but for the secondary interval

**`kelly`**: Kelly sizing runtime state (§9):

- `enabled`, `active_regime`, `last_update_n`, `kelly_fraction`
- `aggregate`, `bullish`, `ranging`, `bearish` result blocks:
  - `f_star`, `f_fractional`, `multiplier`, `win_rate`, `avg_win`, `avg_loss`,
    `payoff_ratio`
  - `n_total`, `n_wins`, `n_losses`, `edge`, `sufficient_data`, `reason`

**`regime_directional`**: directional regime system state (§18):

- `enabled`, `shadow_enabled`, `actuation_enabled`
- `tier` (0/1/2), `tier_label` (`symmetric`/`biased`/`directional`)
- `suppressed_side` (`A`/`B`/null), `favored_side` (`A`/`B`/null)
- `regime`, `confidence`, `bias_signal`, `abs_bias`
- `directional_ok_tier1`, `directional_ok_tier2`, `hmm_ready`
- `dwell_sec`, `hysteresis_buffer`, `grace_remaining_sec`, `cooldown_remaining_sec`, `cooldown_suppressed_side`
- `regime_suppressed_slots` (count of slots with `mode_source="regime"`)
- `tier_history` (rolling transition history; capped)
- `last_eval_ts`, `reason`

### 23.2 `status_band` thresholds

- `stop` when `open_order_headroom < 10` or `partial_fill_cancel_events_1d > 0`
- `caution` when `open_order_headroom < 20` (and stop conditions are false)
- `normal` otherwise

## 24. Telegram Command Contract

Supported commands:

- `/pause`
- `/resume`
- `/add_slot`
- `/remove_slot [slot_id]`
- `/remove_slots [count]`
- `/status`
- `/help`
- `/cancel_stale [min_distance_pct]`
- `/reconcile_drift`
- `/audit_pnl`
- `/backfill_ohlcv [target_candles] [max_pages] [interval_min]`
- `/soft_close [slot_id recovery_id]`
- `/set_entry_pct <value>`
- `/set_profit_pct <value>`

Callback format for interactive soft-close:

- `sc:<slot_id>:<recovery_id>`

## 25. Operational Guardrails

Automatic pauses:

- stale price age > `STALE_PRICE_MAX_AGE_SEC`
- consecutive API errors >= `MAX_CONSECUTIVE_ERRORS`
- cannot bootstrap due insufficient DOGE and USD on a slot
- **daily loss lock**: aggregate UTC-day realized loss exceeds `DAILY_LOSS_LIMIT`

Automatic halt:

- invariant violation not covered by explicit bootstrap/min-size bypasses

Capacity telemetry controls (operator diagnostics only):

- `KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT` (default `225`)
- `OPEN_ORDER_SAFETY_RATIO` (default `0.75`)

Open-order drift alert:

- Monitors `(kraken_count - internal_count)` vs 5% threshold.
- If drift persists for 10 min, sends alert notification.
- Auto-clears when drift resolves; sends recovery notification.

Signal handling:

- `SIGTERM`, `SIGINT` (and `SIGBREAK` on Windows) trigger graceful shutdown

## 26. Developer Notes

When updating behavior, update these files together:

1. `state_machine.py` for reducer semantics and invariants
2. `bot.py` for runtime side effects / bootstrap / guardrails
3. `hmm_regime_detector.py` for HMM model / feature extraction / integration helpers
4. `tests/test_hardening_regressions.py` for regression coverage
5. `STATE_MACHINE.md` for contract parity

This document is intentionally code-truth first: if this file and code diverge, code wins and doc must be updated in the same change.
