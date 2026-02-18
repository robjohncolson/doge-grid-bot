# DOGE State-Machine Bot v1

Last updated: 2026-02-17 (rev 4)
Primary code references: `bot.py`, `state_machine.py`, `config.py`, `dashboard.py`, `supabase_store.py`, `hmm_regime_detector.py`, `throughput_sizer.py`, `ai_advisor.py`, `bayesian_engine.py`, `bocpd.py`, `survival_model.py`, `position_ledger.py`

## 1. Scope

This document is the implementation contract for the current runtime.

- Market: Kraken `XDGUSD` (`DOGE/USD`) only
- Strategy: slot-based pair engine (`A` and `B` legs) with A-side per-slot compounding, account-aware B-side quote allocation, and optional dust/quote-first allocation overlays
- Persistence: Supabase-first (`bot_state`, `fills`, `price_history`, `ohlcv_candles`, `bot_events`, `exit_outcomes`, `regime_tier_transitions`)
- Execution: fully rule-based reducer; HMM/directional/AI/Bayesian/self-healing/throughput layers are runtime overlays in `bot.py` (no reducer modifications)
- Control plane: dashboard + Telegram commands

Out of scope for v1:

- Multi-pair swarm (implemented but not active in production)
- Factory visualization mode (specced in `docs/FACTORY_LENS_SPEC.md`, not implemented)
- `pair_model.py` executable model effort (separate track)

## 2. System Overview

Runtime is split into two layers.

- `state_machine.py`: pure reducer (`transition`) and invariant checker (`check_invariants`)
- `bot.py`: exchange I/O, reconciliation, bootstrap, loop budget, APIs, Telegram, persistence, and all advisory/actuation overlays

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

Runtime helper modules (non-reducer):

- `throughput_sizer.py`: fill-time throughput sizing
- `ai_advisor.py`: AI regime advisor opinion pipeline + provider fallback
- `bayesian_engine.py`: continuous belief state, trade-belief actions, manifold score
- `bocpd.py`: online changepoint detector state
- `survival_model.py`: fill-time survival modeling
- `position_ledger.py`: self-healing ledger and subsidy accounting

## 3. Top-Level Lifecycle

```text
START
  -> INIT (logging, signals, Supabase writer, HMM runtime, Bayesian runtime, throughput/AI/ledger/churner runtime state)
  -> LOAD SNAPSHOT (Supabase key __v1__, restore HMM/Bayesian/throughput/accumulation/self-healing state)
  -> FETCH CONSTRAINTS + FEES (Kraken)
  -> FETCH INITIAL PRICE (strict)
  -> SYNC OHLCV + OPTIONAL BACKFILL + PRIME HMM (primary/secondary/tertiary as enabled)
  -> STARTUP RECOVERY CLEANUP (when `RECOVERY_ORDERS_ENABLED=false`, cancel/clear stale recoveries)
  -> RECONCILE TRACKED ORDERS
  -> REPLAY MISSED FILLS (trade history)
  -> POSITION LEDGER MIGRATION (if enabled and required)
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

Per outer loop iteration in `run()`:

1. `begin_loop()` enables private API budget accounting and resets loop-local caches.
2. `run_loop_once()` executes the trading lifecycle.
3. `poll_telegram()` handles operator commands/callbacks.
4. `end_loop()` resets budget counters and loop-local accumulators.

Inside `run_loop_once()` in current order:

1. `_refresh_price(strict=False)`.
2. If price age exceeds `STALE_PRICE_MAX_AGE_SEC`, call `pause(...)`.
3. `_sync_ohlcv_candles(loop_now)` (primary + optional secondary + optional tertiary OHLCV sync).
4. `_update_daily_loss_lock(loop_now)`.
5. `_update_rebalancer(loop_now)`.
6. `_update_micro_features(loop_now)`.
7. `_build_belief_state(loop_now)`.
8. `_maybe_retrain_survival_model(loop_now)`.
9. `_update_regime_tier(loop_now)` (includes HMM refresh path and tier evaluation).
10. `_update_manifold_score(loop_now)`.
11. `_maybe_schedule_ai_regime(loop_now)` (process pending AI result + debounce/schedule worker).
12. `_update_accumulation(loop_now)`.
13. `_apply_tier2_suppression(loop_now)`.
14. `_clear_expired_regime_cooldown(loop_now)`.
15. Pre-tick deferred entry drain (`_drain_pending_entry_orders(..., skip_stale=True)`).
16. Capture initial balance baseline when absent.
17. Conditional balance-intelligence flow polling (`_should_poll_flows` -> `_poll_external_flows`).
18. Compute runtime profit target (`_volatility_profit_pct`).
19. For each slot: apply `PriceTick`, apply `TimerTick`, `_ensure_slot_bootstrapped`, `_auto_repair_degraded_slot`.
20. Post-tick deferred entry drain (`_drain_pending_entry_orders(..., skip_stale=False)`).
21. `_poll_order_status()`.
22. `_update_micro_features(_now())`.
23. `_update_trade_beliefs(_now())`.
24. `_run_self_healing_reprice(loop_now)`.
25. `_run_churner_engine(loop_now)`.
26. `_update_daily_loss_lock(_now())` (post-fill recalculation).
27. `_refresh_open_order_telemetry()`.
28. `_auto_drain_recovery_backlog()`.
29. `_auto_soft_close_if_capacity_pressure()`.
30. `_update_release_recon_gate_locked()` and `_auto_release_sticky_slots()`.
31. Orphan pressure notification when count hits `ORPHAN_PRESSURE_WARN_AT` multiples.
32. `_update_doge_eq_snapshot(loop_now)`.
33. Optional equity series flush (`_should_flush_equity_ts` -> `_flush_equity_ts`).
34. `_save_snapshot()`.

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

Sizing is composed in runtime layers (no reducer changes):

1. **Base leg sizing**
   - Trade `A`: `max(ORDER_SIZE_USD, ORDER_SIZE_USD + slot.total_profit)` (or fixed `ORDER_SIZE_USD` when sticky fixed compounding is enabled).
   - Trade `B`: `_b_side_base_usd()` (account-aware quote sizing).
2. **Capital layers overlay**
   - `layer_usd = effective_layers * CAPITAL_LAYER_DOGE_PER_ORDER * mark_price`
   - `base_with_layers = max(base, base + layer_usd)`
3. **Dust sweep overlay (B-side only)**
   - When `DUST_SWEEP_ENABLED=True` and `QUOTE_FIRST_ALLOCATION=False`, free-USD surplus is split across buy-ready slots and added as a bounded bump.
4. **Throughput overlay**
   - Applied by `ThroughputSizer.size_for_slot(...)`.
5. **Belief/action-knob overlay**
   - Aggression and against-trend suppression multipliers.
6. **Rebalancer favored-side overlay**
   - Size skew on favored side with fund guards.

B-side account-aware behavior:

- Legacy quote split path (`QUOTE_FIRST_ALLOCATION=False`):
  `base_b = max(ORDER_SIZE_USD, available_usd / slot_count)`.
- Quote-first path (`QUOTE_FIRST_ALLOCATION=True`):
  allocation uses only buy-ready slots, subtracts committed buy quote, applies safety buffer, and carries forward unallocated cents (`_quote_first_carry_usd`).

Throughput sizing is the active advisory sizing model (`throughput_sizer.py`):

- Core objective: maximize realized `profit / time_locked` by regime and side.
- Buckets: `aggregate`, plus 6 regime-side buckets (`bearish_A`, `bearish_B`, `ranging_A`, `ranging_B`, `bullish_A`, `bullish_B`).
- Right-censored exits: open exits participate with `TP_CENSORED_WEIGHT`.
- Bucket selection: prefer current `regime x trade` bucket when sufficient; fallback to aggregate.
- Age pressure: uses open-exit age distribution (p90 reference) with penalty trigger against aggregate fill-time baseline.
- Capital utilization penalty: throttles sizing when locked-capital ratio exceeds `TP_UTIL_THRESHOLD` (default `0.7`).
- Final multiplier: `throughput_mult * age_pressure * util_penalty`, clamped to configured floor/ceiling.
- Update cadence: `_update_throughput()` runs from regime evaluation cadence (`_update_regime_tier`).
- Status surfacing: `/api/status -> throughput_sizer`.

See `docs/THROUGHPUT_SIZER_SPEC.md` for full model details.

### 9.1 Throughput + Allocation Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TP_ENABLED` | False | Master throughput sizing toggle |
| `TP_LOOKBACK_CYCLES` | 500 | Completed-cycle lookback window |
| `TP_MIN_SAMPLES` | 20 | Aggregate sample gate |
| `TP_MIN_SAMPLES_PER_BUCKET` | 10 | Regime-side bucket sample gate |
| `TP_FULL_CONFIDENCE_SAMPLES` | 50 | Sample count for full bucket confidence |
| `TP_FLOOR_MULT` | 0.5 | Minimum throughput multiplier clamp |
| `TP_CEILING_MULT` | 2.0 | Maximum throughput multiplier clamp |
| `TP_CENSORED_WEIGHT` | 0.5 | Weight for open/censored exits |
| `TP_AGE_PRESSURE_TRIGGER` | 1.5 | Age pressure trigger vs aggregate fill-time baseline |
| `TP_AGE_PRESSURE_SENSITIVITY` | 0.5 | Age pressure slope |
| `TP_AGE_PRESSURE_FLOOR` | 0.3 | Minimum age-pressure multiplier |
| `TP_UTIL_THRESHOLD` | 0.7 | Capital utilization throttle threshold |
| `TP_UTIL_SENSITIVITY` | 0.8 | Utilization penalty slope |
| `TP_UTIL_FLOOR` | 0.4 | Minimum utilization multiplier |
| `TP_RECENCY_HALFLIFE` | 100 | Recency weighting half-life |
| `TP_LOG_UPDATES` | True | Emit throughput update logs |
| `DUST_SWEEP_ENABLED` | True | Enable B-side free-USD dust redistribution |
| `QUOTE_FIRST_ALLOCATION` | False | Buy-ready-slot-only quote allocation path |

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

Recovery/orphan behavior is runtime-gated by `RECOVERY_ORDERS_ENABLED`.

Default contract:

- `RECOVERY_ORDERS_ENABLED` inherits `RECOVERY_ENABLED` (effective default `True` in current config defaults).
- Strategic-capital rollout mode explicitly sets `RECOVERY_ORDERS_ENABLED=False`.

When recovery orders are enabled:

- Orphaning converts an active exit to a `recovery_order` while keeping Kraken order alive.
- Recovery orders remain on book until filled, manually soft-closed, or auto-soft-closed under capacity pressure.
- Hard cap: `MAX_RECOVERY_SLOTS` per slot.
- Pressure warning emitted at `ORPHAN_PRESSURE_WARN_AT` multiples.
- Auto governors:
  - `_auto_drain_recovery_backlog()` (small forced drains when pressure is high),
  - `_auto_soft_close_if_capacity_pressure()` (near-market repricing under capacity stress).

When recovery orders are disabled (`RECOVERY_ORDERS_ENABLED=False`):

- Runtime passes `s1_orphan_after_sec=inf` and `s2_orphan_after_sec=inf` into reducer config, so timer orphaning is effectively disabled.
- Startup cleanup (`_cleanup_recovery_orders_on_startup`) cancels tracked recovery txids and clears `recovery_orders` state.
- Dashboard actions `soft_close`, `soft_close_next`, and `cancel_stale_recoveries` are rejected.
- Throughput/recovery accounting paths treat recoveries as disabled.

Manual lifecycle controls (when enabled):

- Dashboard `/api/action`: `soft_close`, `soft_close_next`, `cancel_stale_recoveries`
- Telegram: `/soft_close` (picker or explicit args)

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
- Entry anti-loss widening remains separately gated by `ENTRY_BACKOFF_ENABLED`.

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

Persists interval OHLCV candles from Kraken into Supabase for HMM training/inference across primary (1m), secondary (15m), and tertiary (1h) pipelines.

### 16.1 Collection

- `_sync_ohlcv_candles()` is called each loop and dispatches to `_sync_ohlcv_candles_for_interval(...)` for enabled pipelines.
- Primary pipeline uses `HMM_OHLCV_INTERVAL_MIN` (default `1`) and `HMM_OHLCV_SYNC_INTERVAL_SEC` (default `60s`).
- Secondary pipeline uses `HMM_SECONDARY_INTERVAL_MIN` (default `15`) and `HMM_SECONDARY_SYNC_INTERVAL_SEC` (default `300s`) when `HMM_SECONDARY_OHLCV_ENABLED` or `HMM_MULTI_TIMEFRAME_ENABLED`.
- Tertiary pipeline uses `HMM_TERTIARY_INTERVAL_MIN` (default `60`) and `HMM_TERTIARY_SYNC_INTERVAL_SEC` (default `3600s`) when `HMM_TERTIARY_ENABLED`.
- Rows are normalized, still-forming candles are dropped, and upserts are queued via `supabase_store.queue_ohlcv_candles(...)`.
- Per-pipeline cursors are snapshot-persisted (`ohlcv_since_cursor`, `ohlcv_secondary_since_cursor`, `ohlcv_tertiary_since_cursor`).
- Startup warmup (`_maybe_backfill_ohlcv_on_startup`) can backfill all enabled pipelines; stall breaker blocks repeated zero-progress backfills after `HMM_BACKFILL_MAX_STALLS`.

### 16.2 Storage

Supabase table `ohlcv_candles`:

- Columns: `time`, `pair`, `interval_min`, `open`, `high`, `low`, `close`, `volume`, `trade_count`
- Unique constraint: `(pair, interval_min, time)` — upsert-safe.
- Retention: `_cleanup_old_ohlcv()` deletes rows older than `HMM_OHLCV_RETENTION_DAYS` (default 14 days, ~4032 candles).

### 16.3 Data Access

- `_fetch_training_candles(count, interval_min=...)` loads per-interval training windows.
- `_fetch_recent_candles(count, interval_min=...)` loads per-interval inference windows.
- Defaults in current config:
  - primary training: `HMM_TRAINING_CANDLES=4000`
  - secondary training: `HMM_SECONDARY_TRAINING_CANDLES=1440`
  - tertiary training: `HMM_TERTIARY_TRAINING_CANDLES=500`
- Both helpers return `(closes, volumes)` float lists.

### 16.4 Readiness Check

`_hmm_data_readiness(..., state_key=primary|secondary|tertiary)` returns a cached diagnostic block:

- `samples`: current candle count in Supabase
- `coverage_pct`: samples / training_target * 100
- `freshness_limit_sec`: stale threshold = `max(180, interval_sec * 3)`
- `freshness_ok`: newest candle age <= `freshness_limit_sec`
- `volume_coverage_pct`: % of candles with non-zero volume
- `ready_for_min_train`: samples >= `HMM_MIN_TRAIN_SAMPLES` and fresh
- `ready_for_target_window`: samples >= `HMM_TRAINING_CANDLES` and fresh
- `gaps`: list of actionable gap descriptions

Status surfaces:

- `hmm_data_pipeline` (primary)
- `hmm_data_pipeline_secondary`
- `hmm_data_pipeline_tertiary`

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
| `HMM_TRAINING_CANDLES` | 4000 | Primary training window size |
| `HMM_RECENT_CANDLES` | 100 | Inference window size |
| `HMM_MIN_TRAIN_SAMPLES` | 500 | Minimum candles for training |
| `HMM_SECONDARY_OHLCV_ENABLED` | False | Enable 15m collection |
| `HMM_SECONDARY_INTERVAL_MIN` | 15 | Secondary candle interval |
| `HMM_SECONDARY_SYNC_INTERVAL_SEC` | 300.0 | Secondary pull cadence |
| `HMM_SECONDARY_TRAINING_CANDLES` | 1440 | Secondary training window |
| `HMM_SECONDARY_MIN_TRAIN_SAMPLES` | 200 | Secondary min-train threshold |
| `HMM_TERTIARY_ENABLED` | False | Enable 1h collection/detector |
| `HMM_TERTIARY_INTERVAL_MIN` | 60 | Tertiary candle interval |
| `HMM_TERTIARY_SYNC_INTERVAL_SEC` | 3600.0 | Tertiary pull cadence |
| `HMM_TERTIARY_TRAINING_CANDLES` | 500 | Tertiary training window |
| `HMM_TERTIARY_MIN_TRAIN_SAMPLES` | 150 | Tertiary min-train threshold |
| `HMM_READINESS_CACHE_SEC` | 300.0 | Readiness check cache TTL |

## 17. HMM Regime Detector (Advisory Layer)

Read-only regime classifier stack that sits alongside trend logic (§15). It does **not** modify the reducer (§6), emit reducer actions, or alter event transition rules directly.

Module: `hmm_regime_detector.py` (requires `numpy`, `hmmlearn`).

### 17.1 Architecture

```
OHLCV (§16) -> RegimeDetector(1m) ----\
OHLCV (§16) -> RegimeDetector(15m) ----> tactical policy source (primary or consensus)
OHLCV (§16) -> RegimeDetector(1h) ----/  strategic context (AI advisor + accumulation)

tactical source -> directional tiers (§18), throughput regime bucket (§9), trend blend (§15)
tertiary source -> transition confirmation + accumulation gating (§29), AI context (§27)
```

All detectors use three hidden states: `BEARISH` (0), `RANGING` (1), `BULLISH` (2).

Four observation features: MACD histogram slope, EMA spread %, RSI zone, volume ratio.

### 17.2 Lifecycle

1. **Init** (`_init_hmm_runtime`): initializes primary detector; conditionally initializes secondary and tertiary detectors.
2. **Train paths**:
   - primary: `_train_hmm(...)`
   - secondary: `_train_hmm_secondary(...)`
   - tertiary: `_train_hmm_tertiary(...)`
3. **Inference path** (`_update_hmm`):
   - updates enabled detectors,
   - recomputes tactical consensus (1m+15m),
   - updates tertiary transition tracking (`_update_hmm_tertiary_transition`).
4. **Policy read path**:
   - `_policy_hmm_source()` returns primary or tactical consensus only.
   - tertiary is not part of tactical consensus; it is consumed by strategic layers.
5. **Persistence**:
   - detector state snapshots + training depth/consensus/transition metadata are persisted and restored.

### 17.3 Degradation Guarantees

- If `HMM_ENABLED=False`, all detectors stay inert.
- If `numpy`/`hmmlearn` import fails, runtime degrades gracefully.
- If a detector is unavailable/untrained, policy reads degrade to available lower-tier sources (or neutral defaults).
- Tactical policy source remains primary-only unless `HMM_MULTI_TIMEFRAME_ENABLED=True` and source mode is `consensus`.

### 17.4 Tuning Phases

| Phase | `HMM_BLEND_WITH_TREND` | Effect |
|-------|------------------------|--------|
| Shadow | 1.0 | HMM classifies but has zero influence |
| Gentle | 0.7 | 30% HMM, 70% trend |
| Equal | 0.5 | Equal weight |
| HMM-primary | 0.3 | 70% HMM, 30% trend |

### 17.5 Tactical Multi-Timeframe (1m + 15m)

- Tactical consensus combines primary and secondary only.
- Policy source selector (`HMM_MULTI_TIMEFRAME_SOURCE`) accepts:
  - `primary`
  - `consensus`
- Consensus agreement classes: `full`, `1m_cooling`, `15m_neutral`, `conflict`, `primary_only`.

### 17.6 Tertiary 1h Detector (Strategic)

- Tertiary detector is enabled by `HMM_TERTIARY_ENABLED`.
- Default windows:
  - `HMM_TERTIARY_TRAINING_CANDLES=500`
  - `HMM_TERTIARY_MIN_TRAIN_SAMPLES=150`
  - `HMM_TERTIARY_RECENT_CANDLES=30`
- Transition state tracked in `hmm_tertiary_transition`:
  `from_regime`, `to_regime`, `confirmation_count`, `confirmed`, `changed_at`, `transition_age_sec`.
- Confirmation gate for strategic accumulation uses `ACCUM_CONFIRMATION_CANDLES` (default `2`).

### 17.7 Training Depth and Confidence Modifier

- Runtime tracks per-pipeline training depth (`training_depth_primary`, `training_depth_secondary`, `training_depth_tertiary`).
- Quality tier output: `shallow`, `baseline`, `deep`, `full`.
- Primary (4000-candle target) tier thresholds:
  - `shallow`: `<1000`, modifier `0.70`
  - `baseline`: `1000-2499`, modifier `0.85`
  - `deep`: `2500-3999`, modifier `0.95`
  - `full`: `>=4000`, modifier `1.00`
- Effective confidence path in tier evaluation:
  1. `_policy_hmm_signal()` produces `confidence_raw`
  2. `_hmm_confidence_modifier_for_source(...)` selects modifier from active source depth
  3. `_update_regime_tier()` computes `confidence_effective = confidence_raw * confidence_modifier`
- Dashboard renders depth progress (`pct_complete`), quality tier color, modifier, and estimated full-window timestamp.

### 17.8 Config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HMM_ENABLED` | False | Master HMM enable |
| `HMM_N_STATES` | 3 | Hidden states |
| `HMM_N_ITER` | 100 | Baum-Welch iterations |
| `HMM_COVARIANCE_TYPE` | "diag" | Gaussian covariance type |
| `HMM_INFERENCE_WINDOW` | 50 | Primary inference window |
| `HMM_CONFIDENCE_THRESHOLD` | 0.15 | Min confidence gate |
| `HMM_RETRAIN_INTERVAL_SEC` | 86400.0 | Retrain cadence |
| `HMM_BIAS_GAIN` | 1.0 | Bias scaling |
| `HMM_BLEND_WITH_TREND` | 0.5 | Trend/HMM blend |
| `HMM_DEEP_DECAY_ENABLED` | False | Enable deep-window decay behavior |
| `HMM_MULTI_TIMEFRAME_ENABLED` | False | Enable 15m tactical detector |
| `HMM_MULTI_TIMEFRAME_SOURCE` | "primary" | Tactical policy source |
| `HMM_SECONDARY_TRAINING_CANDLES` | 1440 | 15m training window |
| `HMM_SECONDARY_RECENT_CANDLES` | 50 | 15m inference window |
| `HMM_TERTIARY_ENABLED` | False | Enable 1h strategic detector |
| `HMM_TERTIARY_TRAINING_CANDLES` | 500 | 1h training window |
| `HMM_TERTIARY_RECENT_CANDLES` | 30 | 1h inference window |
| `CONSENSUS_1M_WEIGHT` | 0.3 | Tactical 1m weight |
| `CONSENSUS_15M_WEIGHT` | 0.7 | Tactical 15m weight |
| `CONSENSUS_1H_WEIGHT` | 0.30 | Strategic context weight input |
| `CONSENSUS_DAMPEN_FACTOR` | 0.5 | Tactical disagreement dampener |

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
- **OHLCV pipeline state**: primary + secondary + tertiary cursors/sync timestamps/last-rows fields
- **HMM backfill state**: primary + secondary + tertiary backfill progress/stall counters
- **HMM regime state**: primary snapshot plus `hmm_state_secondary`, `hmm_state_tertiary`, `hmm_consensus`, `hmm_tertiary_transition`
- **HMM training depth**: primary/secondary/tertiary depth + quality metadata
- **throughput state**: `throughput_sizer_state` snapshot when throughput is enabled
- **Directional regime state**: `regime_tier`, `regime_tier_entered_at`, `regime_tier2_grace_start`, `regime_tier2_last_downgrade_at`, `regime_cooldown_suppressed_side`, `regime_tier_history`, `regime_side_suppressed`, `regime_last_eval_ts`, `regime_shadow_state`
- **AI override state**: `ai_override_tier`, `ai_override_direction`, `ai_override_until`, `ai_override_applied_at`, `ai_override_source_conviction`
- **accumulation state**: `accum_*` fields (`state`, trigger regime pair, budget/spend/acquired, timers, hold/cooldown/session summary)
- **balance intelligence state**: `recon_baseline`, `flow_*`, `external_flows`, `baseline_adjustments`, equity-snapshot metadata
- **Bayesian state**: `belief_state`, `belief_cycle_metadata`, `action_knobs`, micro-feature windows, `bocpd_state`, `bocpd_snapshot`, survival snapshot, trade-belief state
- **self-healing state**: `self_heal_*` counters/summaries/hold-overrides
- **position-ledger state**: `position_ledger_state`, exit-to-position maps, migration markers
- **churner state**: reserve/day/cycle/profit counters and per-slot churner runtime state

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
- `GET /api/ops/toggles` -> runtime toggle panel payload
- `GET /api/churner/status` -> churner runtime status
- `GET /api/churner/candidates` -> churner candidate list
- `POST /api/action` -> operator actions
- `POST /api/ops/toggle|reset|reset-all` -> runtime toggle control
- `POST /api/churner/spawn|kill|config` -> churner runtime control

Supported `/api/action` actions:

- `pause`
- `resume`
- `add_slot`
- `set_entry_pct`
- `set_profit_pct`
- `soft_close` (specific recovery by slot_id + recovery_id)
- `soft_close_next` (oldest recovery)
- `release_slot` (sticky release by slot/exit selector)
- `release_oldest_eligible` (sticky release helper)
- `cancel_stale_recoveries` (bulk soft-close distant recoveries)
- `remove_slot` (by slot_id)
- `remove_slots` (by count)
- `add_layer` (optional source: auto/doge/usd)
- `remove_layer`
- `reconcile_drift` (cancel Kraken-only orders not tracked internally)
- `audit_pnl` (recompute P&L from completed cycles)
- `ai_regime_override` (accept AI recommendation with TTL)
- `ai_regime_revert` (cancel active AI override)
- `ai_regime_dismiss` (dismiss disagreement recommendation)
- `accum_stop` (manual stop for active accumulation session)
- `self_heal_reprice_breakeven`
- `self_heal_close_market`
- `self_heal_keep_holding`

Action gates:

- In sticky mode, `soft_close`, `soft_close_next`, and `cancel_stale_recoveries` are blocked.
- When `RECOVERY_ORDERS_ENABLED=false`, those same recovery actions are blocked.

### 23.1 `/api/status` Payload Blocks

Top-level status includes bot-wide fields plus these contract blocks:

- `capacity_fill_health`
- `balance_health`
- `balance_recon`
- `external_flows`
- `equity_history`
- `sticky_mode`
- `self_healing`
- `slot_vintage`
- `hmm_data_pipeline`
- `hmm_data_pipeline_secondary`
- `hmm_data_pipeline_tertiary`
- `hmm_regime`
- `hmm_consensus`
- `belief_state`
- `bocpd`
- `survival_model`
- `trade_beliefs`
- `action_knobs`
- `manifold_score`
- `ops_panel`
- `regime_history_30m`
- `throughput_sizer`
- `dust_sweep`
- `b_side_sizing`
- `regime_directional`
- `ai_regime_advisor`
- `accumulation`
- `release_health`
- `doge_bias_scoreboard`
- `rebalancer`
- `trend`
- `capital_layers`

Operational fields retained at top level:

- `pause_reason`
- `top_phase`
- `daily_loss_limit`
- `daily_realized_loss_utc`
- `daily_loss_lock_active`
- `daily_loss_lock_utc_day`
- `price_age_sec`
- `stale_price_max_age_sec`
- `reentry_base_cooldown_sec`
- `total_round_trips`
- `total_orphans`
- `total_profit_doge`
- `total_unrealized_profit`
- `total_unrealized_doge`
- `today_realized_loss`
- `pnl_audit`
- `pnl_reference_price`
- `recovery_orders_enabled`
- `slots` (per-slot state list)

Notable newer blocks:

- `throughput_sizer`: aggregate + regime-side throughput stats and multipliers.
- `ai_regime_advisor`: last opinion, conviction, suggested TTL, override state, history, scheduler timings.
- `accumulation`: state machine status (`IDLE|ARMED|ACTIVE|COMPLETED|STOPPED`), spend/budget/drawdown/session summary.
- `self_healing`: subsidy accounting, cleanup queue, operator actions state, ledger-backed slot healing telemetry.
- `bocpd`/`belief_state`/`survival_model`/`trade_beliefs`/`action_knobs`: Bayesian stack telemetry.
- `hmm_data_pipeline_tertiary` + `hmm_regime.tertiary` + `hmm_regime.tertiary_transition`: 1h HMM observability.
- `dust_sweep` and `b_side_sizing`: account-aware quote allocation observability.

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

Current Telegram surface is intentionally smaller than dashboard/API.
AI override actions, accumulation stop, self-healing operator actions, ops toggles,
and churner runtime controls are dashboard/API-only in current runtime.

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

Related guardrail toggles:

- `AUTO_RECOVERY_DRAIN_ENABLED` (backlog pressure drain)
- `PRIVATE_API_METRONOME_ENABLED` (private API pacing telemetry)

## 26. Developer Notes

When updating behavior, update these files together:

1. `state_machine.py` for reducer semantics and invariants
2. `bot.py` for runtime side effects / bootstrap / guardrails / API contract
3. `config.py` for runtime flag and default contract changes
4. `hmm_regime_detector.py` for HMM model / feature extraction / integration helpers
5. `throughput_sizer.py` for fill-time sizing model contract
6. `ai_advisor.py` for AI regime advisor behavior contract
7. `bayesian_engine.py`, `bocpd.py`, `survival_model.py` for Bayesian stack contract
8. `position_ledger.py` for self-healing position/subsidy accounting contract
9. `dashboard.py` for status/action rendering contract
10. `tests/test_hardening_regressions.py` (+ subsystem tests) for regression coverage
11. `STATE_MACHINE.md` for contract parity

Auxiliary repository modules (outside core state-machine contract but present in root):

- `kraken_client.py` (exchange client)
- `notifier.py` and `telegram_menu.py` (Telegram transport/menu helpers)
- `grid_strategy.py`, `stats_engine.py`, `pair_scanner.py`, `backtest_v1.py`, `state_machine_visual.py` (legacy/auxiliary tooling)
- `kelly_sizer.py` (deprecated — replaced by `throughput_sizer.py`)
- `factory_viz.py` (factory view HTML endpoint helper)

This document is intentionally code-truth first: if this file and code diverge, code wins and doc must be updated in the same change.

## 27. AI Regime Advisor

`ai_advisor.py` provides a second-opinion layer on top of mechanical regime tiering.

Runtime model:

- `bot.py` builds AI context from 1m/15m/1h HMM state, consensus agreement, transition matrices, training depth, capacity state, and recent regime history.
- `_maybe_schedule_ai_regime(...)` enforces periodic scheduling + debounce + event-triggered scheduling.
- AI call runs on a daemon thread (`_ai_regime_worker`) and writes `_ai_regime_pending_result`.
- Main loop ingests pending result on next cycle (`_process_ai_regime_pending_result`).
- Dashboard actions can apply/revert/dismiss AI recommendations.

Provider behavior:

- Multi-provider panel is built from configured keys (`DEEPSEEK_API_KEY`, `SAMBANOVA_API_KEY`, `CEREBRAS_API_KEY`, `GROQ_API_KEY`, `NVIDIA_API_KEY`).
- AI output includes `recommended_tier`, `recommended_direction`, `conviction`, `accumulation_signal`, `accumulation_conviction`, and `suggested_ttl_minutes`.
- Suggested TTL is clamped to config min/max bounds before use.

Config:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `AI_REGIME_ADVISOR_ENABLED` | False | Master AI advisor toggle |
| `AI_REGIME_INTERVAL_SEC` | 300.0 | Periodic run cadence |
| `AI_REGIME_DEBOUNCE_SEC` | 60.0 | Event-trigger debounce |
| `AI_OVERRIDE_TTL_SEC` | 1800 | Default override TTL |
| `AI_OVERRIDE_MIN_TTL_SEC` | 300 | Minimum allowed TTL |
| `AI_OVERRIDE_MAX_TTL_SEC` | 3600 | Maximum allowed TTL |
| `AI_OVERRIDE_MIN_CONVICTION` | 50 | Min conviction for override application |
| `AI_REGIME_HISTORY_SIZE` | 12 | Opinion history buffer size |
| `AI_REGIME_PREFER_REASONING` | True | Prefer reasoning-capable panel path |
| `AI_REGIME_PREFER_DEEPSEEK_R1` | False | DeepSeek reasoning preference flag |

Degradation guarantees:

- If disabled or providers unavailable, advisor remains inactive and mechanical regime stays authoritative.
- Failed/invalid AI responses do not halt loop; status reports error state.

See `docs/AI_REGIME_ADVISOR_SPEC.md`, `docs/AI_MULTI_PROVIDER_SPEC.md`, and `docs/AI_SUGGESTED_TTL_SPEC.md` for full spec.

## 28. 1h HMM (Tertiary Timeframe)

The tertiary detector is a strategic 1h classifier, separate from tactical 1m/15m consensus.

Behavior:

- Enabled by `HMM_TERTIARY_ENABLED`.
- Trained/inferred by `_train_hmm_tertiary` and `_update_hmm_tertiary`.
- Bootstrap path can resample lower-interval candles (`_fetch_bootstrap_tertiary_candles`) when native 1h depth is limited.
- Transition tracker (`_hmm_tertiary_transition`) records:
  `from_regime`, `to_regime`, `confirmation_count`, `confirmed`, `changed_at`, `transition_age_sec`.

Contract:

- Tertiary regime is exposed in `hmm_regime.tertiary` and `hmm_regime.tertiary_transition`.
- Tactical policy source remains primary/consensus (`_policy_hmm_source`); tertiary is consumed by strategic layers (AI context + accumulation gating).

Config:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HMM_TERTIARY_ENABLED` | False | Enable tertiary detector |
| `HMM_TERTIARY_INTERVAL_MIN` | 60 | Tertiary candle interval |
| `HMM_TERTIARY_TRAINING_CANDLES` | 500 | Tertiary training window |
| `HMM_TERTIARY_MIN_TRAIN_SAMPLES` | 150 | Minimum training samples |
| `HMM_TERTIARY_RECENT_CANDLES` | 30 | Inference window |
| `HMM_TERTIARY_SYNC_INTERVAL_SEC` | 3600.0 | Sync cadence |

Degradation guarantees:

- If unavailable/untrained, transition output is neutral and strategic consumers degrade safely.

See `docs/STRATEGIC_CAPITAL_DEPLOYMENT_SPEC.md` for full spec.

## 29. DCA Accumulation Engine

Strategic DOGE accumulation state machine driven by tertiary transition confirmation + AI conviction.

Runtime states:

- `IDLE`
- `ARMED`
- `ACTIVE`
- `COMPLETED`
- `STOPPED`

Core flow:

1. Arm on confirmed tertiary transition and available idle budget.
2. Activate when AI signal is `accumulate_doge` and conviction meets threshold.
3. Execute periodic market buys (`ordertype="market"`) in fixed USD chunks.
4. Finalize on stop conditions (manual stop, transition invalidation/revert, hold streak, drawdown breach, budget depletion, buy failure, cooldown).

Status/API:

- `/api/status -> accumulation` includes state, trigger, budget/spend, buy counts, drawdown, and last session summary.
- `/api/action -> accum_stop` performs controlled stop/finalize.

Config:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ACCUM_ENABLED` | False | Master accumulation toggle |
| `ACCUM_MIN_CONVICTION` | 60 | AI conviction floor |
| `ACCUM_RESERVE_USD` | 50.0 | Idle USD reserve floor |
| `ACCUM_MAX_BUDGET_USD` | 50.0 | Max per-session budget |
| `ACCUM_CHUNK_USD` | 2.0 | Per-buy chunk size |
| `ACCUM_INTERVAL_SEC` | 120.0 | Buy cadence |
| `ACCUM_MAX_DRAWDOWN_PCT` | 3.0 | Stop-loss drawdown guard |
| `ACCUM_COOLDOWN_SEC` | 3600.0 | Post-session cooldown |
| `ACCUM_CONFIRMATION_CANDLES` | 2 | Tertiary transition confirmation count |

Degradation guarantees:

- If disabled or prerequisites fail, state remains `IDLE`.
- No accumulation order is placed without API budget and min-volume validation.

See `docs/STRATEGIC_CAPITAL_DEPLOYMENT_SPEC.md` for full spec.

## 30. Durable Profit Settlement

Realized tracking uses dual fields on `PairState`:

- `total_profit`: net cycle PnL estimate after fees
- `total_settled_usd`: quote-balance settlement-oriented tracker

Cycle accounting includes fee/settlement split fields in booked cycles:

- `entry_fee`
- `exit_fee`
- `quote_fee`
- `settled_usd`

Sizing implications:

- B-side quote allocation and quote-first logic consume quote-side availability and settlement-aware accounting.
- This reduces drift between long-run PnL estimates and quote balance changes.

Config:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DURABLE_SETTLEMENT_ENABLED` | True | Enable durable quote-settlement accounting path |
| `ROUNDING_RESIDUAL_ENABLED` | False | Enable residual rounding carry controls |

See `docs/DURABLE_PROFIT_EXIT_ACCOUNTING_SPEC.md` for full spec.

## 31. Balance Intelligence

Balance intelligence is observability-first and does not directly change reducer semantics.

Capabilities:

1. External flow detection from Kraken ledger (`deposit` / `withdrawal`).
2. Baseline auto-adjustment for detected external flows.
3. Persistent DOGE-equivalent equity snapshots and sparkline history.

Status surfaces:

- `balance_recon`
- `external_flows`
- `equity_history`
- `doge_bias_scoreboard` (derived performance/idle/runway/opportunity metrics)

Config:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `FLOW_DETECTION_ENABLED` | True | Enable external-flow polling |
| `FLOW_POLL_INTERVAL_SEC` | 300.0 | Flow poll cadence |
| `FLOW_BASELINE_AUTO_ADJUST` | True | Auto-adjust recon baseline on external flows |
| `EQUITY_TS_ENABLED` | True | Enable persistent equity series |
| `EQUITY_SNAPSHOT_INTERVAL_SEC` | 300.0 | Snapshot cadence |
| `EQUITY_SNAPSHOT_FLUSH_SEC` | 300.0 | Flush cadence |
| `BALANCE_RECON_DRIFT_PCT` | 2.0 | Drift alert threshold |

Degradation guarantees:

- If flow/equity paths fail, trading loop continues and status reports degraded fields.

See `docs/BALANCE_INTELLIGENCE_SPEC.md` for full spec.

## 32. Self-Healing Slots

Self-healing replaces timer-driven recovery churn for sticky exits with ledger-driven subsidy management.

Core components:

- `position_ledger.py` tracks immutable entry context, mutable exit intent, and append-only journal events.
- Runtime derives age bands, subsidy needs, and opportunity-cost estimates per open position.
- Repricing engine performs tighten and subsidy-backed repricing paths.
- Operator controls exposed through `/api/action` self-heal actions.

Churner integration:

- Optional churner cycles can generate subsidy credits.
- Churner runtime has dedicated endpoints and status payloads.

Config:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `POSITION_LEDGER_ENABLED` | True | Enable ledger runtime |
| `STICKY_MODE_ENABLED` | False | Enable sticky-slot lifecycle mode |
| `SUBSIDY_ENABLED` | False | Enable subsidy repricing path |
| `SUBSIDY_REPRICE_INTERVAL_SEC` | 3600 | Min interval between subsidy reprices |
| `SUBSIDY_AUTO_REPRICE_BAND` | "stuck" | Minimum band for auto subsidy repricing |
| `SUBSIDY_WRITE_OFF_AUTO` | False | Allow automatic write-off behavior |
| `CHURNER_ENABLED` | False | Enable churner helper engine |
| `CHURNER_MIN_HEADROOM` | 10 | Open-order headroom gate |
| `CHURNER_RESERVE_USD` | 5.0 | Shared reserve pool |
| `RELEASE_AUTO_ENABLED` | False | Enable sticky auto-release |
| `RELEASE_RECON_HARD_GATE_ENABLED` | True | Require recon gate for release actions |

Degradation guarantees:

- If ledger/subsidy/churner features are disabled, bot falls back to non-self-healing paths without reducer changes.

See `docs/SELF_HEALING_SLOTS_SPEC.md` for full spec.

## 33. Bayesian Intelligence Stack

Bayesian stack introduces continuous belief signals while preserving reducer purity.

Modules:

- `bocpd.py`: online changepoint detection (`change_prob`, run-length posterior).
- `survival_model.py`: fill-time survival predictions with censoring support.
- `bayesian_engine.py`: belief/posterior synthesis, trade-belief actions, action knobs, manifold components.

Runtime outputs:

- `belief_state`
- `bocpd`
- `survival_model`
- `trade_beliefs`
- `action_knobs`

Integration:

- Throughput/manifold/AI/self-healing consume Bayesian-derived telemetry.
- Existing timer and safety guards remain active as backstops.

Config (selected):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `BOCPD_ENABLED` | False | Enable BOCPD updates |
| `ENRICHED_FEATURES_ENABLED` | False | Enable enriched private microstructure feature vector |
| `SURVIVAL_MODEL_ENABLED` | False | Enable survival model |
| `SURVIVAL_SYNTHETIC_ENABLED` | False | Enable synthetic survival observations |
| `BELIEF_TRACKER_ENABLED` | False | Enable per-trade belief actions |
| `BELIEF_WIDEN_ENABLED` | False | Enable widen branch in belief action mapping |
| `KNOB_MODE_ENABLED` | False | Enable continuous action knobs |
| `BELIEF_STATE_LOGGING_ENABLED` | True | Belief instrumentation logging |

Degradation guarantees:

- Any disabled or unavailable Bayesian subsystem degrades to neutral defaults without halting loop.

See `docs/BAYESIAN_INTELLIGENCE_SPEC.md` for full spec.

## 34. Manifold Score, Ops Panel, and Churner

This section documents current runtime wiring and scope boundaries.

Manifold score (`manifold_score`):

- Computed in `_update_manifold_score()`.
- Uses regime clarity/stability, throughput efficiency, signal coherence, and BOCPD-related components.
- Exposed in `/api/status` as score, components, history sparkline, and trend.

Ops panel (`ops_panel`):

- Runtime override surfaces exposed via `/api/ops/*`.
- Supports feature-flag style runtime toggles with snapshot persistence.

Churner:

- Runtime status/candidate/read-write endpoints exist (`/api/churner/*`).
- Integrated with self-healing and reserve controls.
- Operates as optional auxiliary engine, not reducer core logic.

Config (selected):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MTS_ENABLED` | True | Enable manifold score computation |
| `MTS_ENTRY_THROTTLE_ENABLED` | False | Enable manifold-driven entry throttle |
| `MTS_KERNEL_ENABLED` | False | Enable kernel-based MTS adaptation path |

Status note:

- Full concept surface (score orchestration + operations UX + churner strategy) is broader than currently active runtime behavior.
- Document implemented contract here; detailed forward-looking design remains in spec docs.

See `docs/MANIFOLD_SCORE_OPS_PANEL_CHURNER_SPEC.md` for full spec.
