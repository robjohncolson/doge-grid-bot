# DOGE Bias Scoreboard — Spec v0.1.0

## Context

The bot runs symmetric grid trades (`A=short`, `B=long`). During uptrends, B-side exits (`sell DOGE -> USD`) can complete in clusters, and capital can accumulate in USD while waiting for re-entry.

The user is long-term DOGE-bullish and wants to measure inventory drift before deciding on a policy fix.

This spec adds a read-only telemetry card with 4 metrics agreed during brainstorming.

- Phase 1: measurement only
- Phase 2: policy lever (spec only, not implemented)

Constraints:
- Zero new API calls
- Zero trading behavior changes
- Zero new dependencies

## Locked Decisions

1. DOGE-denominated equity is the primary KPI.
2. Rolling 24h in-memory deque for time-series (same pattern as `_fill_durations_1d`), not persisted.
3. Opportunity PnL sign convention:
   positive = missed appreciation, negative = avoided loss.
4. Re-entry lag measured per-slot from consecutive B-side `CycleRecord`s.
5. Phase 2 blocked until at least 7 days of Phase 1 data.

## Files To Modify

### 1. `bot.py` (~120 lines net)

#### a) `__init__` (after `_partial_open_seen_txids`, around line ~244): add 3 fields

```python
self._doge_eq_snapshots: deque[tuple[float, float]] = deque()   # (ts, doge_eq)
self._doge_eq_snapshot_interval: float = 300.0                   # 5 min
self._doge_eq_last_snapshot_ts: float = 0.0
```

#### b) `_trim_rolling_telemetry()` (around line ~307): trim deque to 24h

```python
while self._doge_eq_snapshots and self._doge_eq_snapshots[0][0] < cutoff:
    self._doge_eq_snapshots.popleft()
```

#### c) New method `_update_doge_eq_snapshot(now)`

Append to deque if 5 minutes elapsed.

- Guard: skip if no balance or `price <= 0`
- Compute: `doge_eq = _doge_balance(bal) + _usd_balance(bal) / price`
- Append `(now, doge_eq)` and update `last_snapshot_ts`

#### d) New method `_extract_b_side_gaps() -> list[dict]`

Shared helper for metrics 3 and 4.

- For each slot, get `slot.state.completed_cycles` filtered to `trade_id == "B"`
- Sort by cycle number and pair consecutive records
- Skip pairs where `entry_time == 0.0` or `exit_time == 0.0` (legacy)
- For each pair, compute:
  - `lag_sec`
  - `opportunity_usd`
  - `price_distance_pct`
- Detect open gap:
  - last B-cycle has exit but no subsequent entry
  - use current price/time
- Return list of gap dicts and optional `open_gap`

#### e) New method `_compute_doge_bias_scoreboard() -> dict | None`

Calls sub-computations and returns assembled scoreboard dict.

Return `None` if no balance or no valid price.

##### Metric 1 — DOGE-Equivalent Equity

- `current_doge_eq` from balance + price (same math as balance reconciliation)
- Lookback matching for now-1h and now-24h from deque (10 minute tolerance)
- `doge_eq_change_1h`, `doge_eq_change_24h`
- `doge_eq_sparkline`: list of float values from deque

##### Metric 2 — Idle USD Above Runway

- `observed_usd` from `_last_balance_snapshot`
- `usd_committed_buy_orders`: sum of `o.volume * o.price` for all buy orders with txids across all slots
- `usd_next_entries_estimate`: sum of `_slot_order_size_usd(slot)` across all slots
- `usd_runway_floor = committed + (next_entries * 1.5)`
- `idle_usd = max(0, observed - runway_floor)`
- `idle_usd_pct = idle / observed * 100`

##### Metric 3 — Opportunity PnL (from `_extract_b_side_gaps`)

- `total_opportunity_pnl_usd`: sum of `(gap_end_price - gap_start_price) * volume`
- `total_opportunity_pnl_doge = total_usd / price`
- `open_gap_opportunity_usd` (or `None`)
- `gap_count`, `avg_opportunity_per_gap_usd`, `worst_missed_usd`

##### Metric 4 — Re-entry Lag (from `_extract_b_side_gaps`)

- `median_reentry_lag_sec`, `avg_reentry_lag_sec`, `max_reentry_lag_sec`
- `current_open_lag_sec`, `current_open_lag_price_pct`
- `lag_count`, `median_price_distance_pct`

#### f) `status_payload()`

After balance reconciliation and before `slots`:

- Call `_update_doge_eq_snapshot(now)` (after `_trim_rolling_telemetry`)
- Add:

```python
"doge_bias_scoreboard": self._compute_doge_bias_scoreboard()
```

### 2. `dashboard.py` (~80 lines HTML + JS)

#### a) HTML

Add a **DOGE Bias Scoreboard** card after balance reconciliation details and before the left panel close.

Card rows:

- `DOGE Equity` | `1h Change` | `24h Change`
- Sparkline SVG (24px tall)
- `Idle USD` | `Runway Floor`
- `Opp. Cost (B-side)` | `Open Gap`
- `Re-entry Lag (med)` | `Current Wait`
- tiny details line

#### b) JS in `renderTop()`

After balance reconciliation rendering:

- Read `s.doge_bias_scoreboard`, guard on null
- DOGE equity row: current + signed 1h/24h changes (green/red)
- Sparkline: inline SVG polyline from `doge_eq_sparkline`
- Idle USD: amount + pct, warning tint when > 50%
- Opportunity PnL: positive as warning (missed gain), negative as green (avoided loss)
- Re-entry lag: median + current wait, warning when > 300s
- Details line: worst miss, max lag, median price distance

## Edge Cases

| Case | Handling |
|---|---|
| No balance/price | Return `None`; card shows `-` |
| No B-side cycles yet | `gap_count=0`; aggregates `null`; card shows `-` |
| `entry_time/exit_time == 0` | Skip legacy gaps |
| Bot restart | Deque empty; sparkline/1h/24h null until rebuilt |
| `from_recovery` cycles | Include (real capital transitions) |
| Single B-cycle, no gaps | `gap_count=0`; may still have `open_gap` |
| Division by zero | Guard on `observed_usd==0`, `price==0`, baseline DOGE eq missing |

## Phase 2 — Inventory Control (Future, spec only)

Do not implement in Phase 1.

Concept:
- If `idle_usd > runway_floor * 1.5` and `detected_trend == "up"`, progressively tighten B-side `entry_pct`.

Future config:

```python
DOGE_BIAS_AGGRESSIVENESS: float = _env("DOGE_BIAS_AGGRESSIVENESS", 0.0, float)  # 0=off
```

Mechanism:
- `b_entry_pct = base_entry_pct * (1.0 - tightening)`
- `tightening` scales with idle ratio and aggressiveness
- floor at `0.05%`

Safety:
- Only when trend is `up`
- Revert immediately when trend flips
- Default OFF
- Max 50% tightening
- B-side only

Hysteresis:
- Enter bias mode when `idle_pct > threshold + 5%`
- Exit when `idle_pct < threshold - 5%`

Review criteria before activation:
- average `idle_usd_pct`
- opportunity PnL sign distribution
- re-entry lag vs price direction correlation
- trend detection frequency

## Verification

1. Start bot: `_update_doge_eq_snapshot` begins populating after first valid balance+price.
2. Dashboard: DOGE Bias Scoreboard card appears below Balance Reconciliation.
3. After B-side round trips: opportunity PnL and lag metrics populate.
4. `/api/status`: `doge_bias_scoreboard` block present.
5. Sparkline fills over ~1 hour (12 points at 5-minute interval).
6. Existing cards unchanged (`balance_recon`, `capacity_fill_health`, `balance_health`).
