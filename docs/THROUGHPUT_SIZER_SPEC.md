# Throughput Sizer Spec

Version: v1.0
Date: 2026-02-16
Status: Implementation-ready
Scope: Replace Kelly Criterion sizer with fill-time-based throughput sizer
Target: `throughput_sizer.py` (NEW), `bot.py`, `config.py`, `dashboard.py`, `/api/status`

---

## 1. Summary

Replace the Kelly Criterion position sizer (`kelly_sizer.py`) with a throughput-based sizer that optimizes capital turnover rate rather than win/loss edge.

**Why**: With sticky slots, every completed round-trip is structurally profitable. Kelly degenerates to `f*=1.0` (100% win rate), producing a constant `1.25×` multiplier at quarter-Kelly. It provides zero useful signal.

**What instead**: Size on `profit / time_locked` per regime (renewal reward theory). Fast-filling regimes get larger orders. Slow-filling regimes get smaller orders. Live congestion signals throttle further when exits are stalling.

The new module (`throughput_sizer.py`) is a drop-in replacement with the same integration surface: constructor, `update()`, `size_for_slot()`, `status_payload()`, `snapshot_state()`/`restore_state()`.

---

## 2. Locked Decisions

1. Fill-time (entry→exit round-trip duration) is the core sizing signal.
2. Regime-conditional: 6 buckets (bearish/ranging/bullish × trade A/trade B).
3. Two live throttles: age pressure (stalling exits) and capital utilization.
4. Drop-in replacement for `kelly_sizer.py` at the same 4 bot.py integration points.
5. Config vars change from `KELLY_*` to `TP_*` (throughput prefix).
6. `kelly_sizer.py` is retained but no longer imported; `KELLY_ENABLED` becomes dead config.
7. Multiplier is always clamped between floor and ceiling; never bypasses min-volume guards.
8. Insufficient data → multiplier 1.0 (pass-through), not an error.

---

## 3. Scope

### In

1. New module `throughput_sizer.py` with `ThroughputConfig`, `ThroughputResult`, `ThroughputSizer`.
2. Config constants (`TP_*` env vars) in `config.py`.
3. Bot.py integration: swap `KellySizer` → `ThroughputSizer` at constructor, update, sizing, status payload.
4. Dashboard: replace "Kelly Sizing" card with "Throughput Sizer" card.
5. Status payload: replace `kelly` key with `throughput_sizer` key.
6. Unit and integration tests.

### Out

1. Auto-scaling layers based on throughput signal (future option).
2. Changes to the S0/S1/S2 state machine.
3. Changes to entry/exit percent logic or exit repricing.
4. Removing `kelly_sizer.py` from the repo (keep for reference; just stop importing).

---

## 4. Core Model

### 4.1 Data Source: Completed Cycles

Each `CompletedCycle` already carries:
- `entry_time`: Unix timestamp of entry fill
- `exit_time`: Unix timestamp of exit fill
- `regime_at_entry`: HMM regime ID (0/1/2) when entry was placed
- `trade_id`: "A" or "B"
- `net_profit`: USD profit of the round-trip
- `volume`: DOGE traded

**Round-trip duration**: `duration = exit_time - entry_time` (seconds).

### 4.2 Regime × Side Buckets

Partition completed cycles into 6 buckets:

| Bucket key | Regime | Side |
|---|---|---|
| `bearish_A` | 0 (bearish) | Trade A (sell entry) |
| `bearish_B` | 0 (bearish) | Trade B (buy entry) |
| `ranging_A` | 1 (ranging) | Trade A |
| `ranging_B` | 1 (ranging) | Trade B |
| `bullish_A` | 2 (bullish) | Trade A |
| `bullish_B` | 2 (bullish) | Trade B |

Plus an `aggregate` bucket (all cycles regardless of regime/side) for fallback.

### 4.3 Fill-Time Statistics Per Bucket

For each bucket with ≥ `TP_MIN_SAMPLES` cycles:

1. `median_fill_sec`: Median of `duration` values.
2. `p75_fill_sec`: 75th percentile.
3. `p95_fill_sec`: 95th percentile.
4. `mean_profit_per_sec`: `sum(net_profit) / sum(duration)` (renewal reward rate).
5. `n_completed`: Number of completed cycles in bucket.
6. `n_censored`: Number of open exits currently contributing censored observations.

### 4.4 Censored Observations (Kaplan-Meier Lite)

Open exits are right-censored: their final fill time is unknown, but their *current* age is a lower bound. Ignoring them biases fill-time estimates downward (survivorship bias — only fast fills are in completed data).

Handling:
1. Collect all open exit orders across all slots.
2. For each open exit: `censored_duration = now - entry_filled_at`.
3. Include in the bucket's duration list with weight `TP_CENSORED_WEIGHT` (default 0.5).
4. Only include if `censored_duration > median_fill_sec * 0.5` (filter noise from freshly placed exits).

Implementation: weighted median/percentile using sorted merge of completed (weight 1.0) and censored (weight 0.5) observations.

### 4.5 Throughput Multiplier

For each new order placed in regime `R` for side `S`:

```
baseline_fill = aggregate.median_fill_sec
bucket_fill   = bucket[R_S].median_fill_sec   (or aggregate if bucket insufficient)

raw_mult = baseline_fill / bucket_fill
```

- `raw_mult > 1.0`: This regime×side fills faster than average → increase size.
- `raw_mult < 1.0`: This regime×side fills slower than average → decrease size.
- `raw_mult = 1.0`: Average fill speed → no change.

Clamped: `throughput_mult = clamp(raw_mult, TP_FLOOR_MULT, TP_CEILING_MULT)`.

**Confidence blend**: When a regime×side bucket has data but fewer than `TP_FULL_CONFIDENCE_SAMPLES`, blend toward 1.0:

```
confidence = min(1.0, n_completed / TP_FULL_CONFIDENCE_SAMPLES)
blended_mult = 1.0 + confidence * (throughput_mult - 1.0)
```

### 4.6 Age Pressure (Live Congestion Throttle)

Detects when current open exits are stalling and throttles new entry sizes.

1. Find `oldest_open_exit_age = max(now - entry_filled_at)` across all slots' open exit orders.
2. Compute threshold: `age_threshold = aggregate.p75_fill_sec * TP_AGE_PRESSURE_TRIGGER`.
3. If `oldest_open_exit_age > age_threshold`:

```
excess_ratio = (oldest_open_exit_age - age_threshold) / age_threshold
age_pressure = max(TP_AGE_PRESSURE_FLOOR, 1.0 - excess_ratio * TP_AGE_PRESSURE_SENSITIVITY)
```

4. If no exits are stalling: `age_pressure = 1.0`.

### 4.7 Capital Utilization Penalty

Prevents over-committing when too much capital is locked in open positions.

1. `locked_doge = sum(volume for all open orders across all slots)`
2. `total_doge = free_doge + locked_doge` (approximate total available)
3. `util_ratio = locked_doge / total_doge` (0.0 to 1.0)
4. If `util_ratio > TP_UTIL_THRESHOLD`:

```
excess = (util_ratio - TP_UTIL_THRESHOLD) / (1.0 - TP_UTIL_THRESHOLD)
util_penalty = max(TP_UTIL_FLOOR, 1.0 - excess * TP_UTIL_SENSITIVITY)
```

5. If `util_ratio <= TP_UTIL_THRESHOLD`: `util_penalty = 1.0`.

### 4.8 Final Sizing Formula

```
size = base_with_layers * throughput_mult * age_pressure * util_penalty
```

Where `base_with_layers` is the existing compounding + layer logic output (same as current Kelly input).

Clamped: `final_size = max(base_with_layers * TP_FLOOR_MULT, min(size, base_with_layers * TP_CEILING_MULT))`.

---

## 5. Config Constants

Replace `KELLY_*` block in `config.py` with:

| Var | Default | Type | Description |
|---|---|---|---|
| `TP_ENABLED` | `False` | bool | Master toggle |
| `TP_LOOKBACK_CYCLES` | `500` | int | Rolling window of recent cycles |
| `TP_MIN_SAMPLES` | `20` | int | Minimum cycles before activation |
| `TP_MIN_SAMPLES_PER_BUCKET` | `10` | int | Per-bucket minimum for regime-specific sizing |
| `TP_FULL_CONFIDENCE_SAMPLES` | `50` | int | Bucket size for full confidence (no blend) |
| `TP_FLOOR_MULT` | `0.5` | float | Minimum sizing multiplier |
| `TP_CEILING_MULT` | `2.0` | float | Maximum sizing multiplier |
| `TP_CENSORED_WEIGHT` | `0.5` | float | Weight for open-exit censored observations |
| `TP_AGE_PRESSURE_TRIGGER` | `1.5` | float | Multiplier on p75 fill time to start throttling |
| `TP_AGE_PRESSURE_SENSITIVITY` | `0.5` | float | How aggressively age pressure reduces size |
| `TP_AGE_PRESSURE_FLOOR` | `0.3` | float | Minimum age pressure multiplier |
| `TP_UTIL_THRESHOLD` | `0.7` | float | Utilization ratio above which penalty applies |
| `TP_UTIL_SENSITIVITY` | `0.8` | float | How aggressively utilization reduces size |
| `TP_UTIL_FLOOR` | `0.4` | float | Minimum utilization penalty multiplier |
| `TP_RECENCY_HALFLIFE` | `100` | int | Exponential decay halflife (cycles) for recency weighting |
| `TP_LOG_UPDATES` | `True` | bool | Emit throughput sizer logs at update cadence |

---

## 6. Runtime Data Model

### 6.1 ThroughputConfig

Dataclass mirroring the `TP_*` config vars above. Constructed from config at startup.

### 6.2 BucketStats

```python
@dataclass
class BucketStats:
    median_fill_sec: float
    p75_fill_sec: float
    p95_fill_sec: float
    mean_profit_per_sec: float
    n_completed: int
    n_censored: int
```

### 6.3 ThroughputResult

```python
@dataclass
class ThroughputResult:
    throughput_mult: float      # regime x side multiplier
    age_pressure: float         # live congestion throttle (0.3-1.0)
    util_penalty: float         # capital utilization penalty (0.4-1.0)
    final_mult: float           # product of all three, clamped
    bucket_key: str             # which bucket was used ("ranging_A", "aggregate", etc.)
    reason: str                 # "ok", "insufficient_data", "no_bucket", etc.
    sufficient_data: bool
```

### 6.4 ThroughputSizer

```python
class ThroughputSizer:
    def __init__(self, cfg: ThroughputConfig | None = None)
    def update(self, completed_cycles: list[dict], open_exits: list[dict],
               regime_label: str | None = None, free_doge: float = 0.0) -> dict[str, BucketStats]
    def size_for_slot(self, base_order_usd: float, regime_label: str | None = None,
                      trade_id: str | None = None) -> tuple[float, str]
    def status_payload(self) -> dict
    def snapshot_state(self) -> dict
    def restore_state(self, data: dict) -> None
```

Key differences from KellySizer:
- `update()` accepts `open_exits` (for censored observations) and `free_doge` (for utilization).
- `size_for_slot()` accepts `trade_id` (for side-specific bucket lookup).

---

## 7. Update Flow

Called at regime eval interval (same cadence as `_update_kelly()` today).

1. Collect completed cycles from all slots (same as `_collect_kelly_cycles()`, but include `duration`, `trade_id`, `volume`).
2. Collect open exits: for each slot, for each order with `role == "exit"` and `entry_filled_at > 0`, emit `{regime_at_entry, trade_id, age: now - entry_filled_at}`.
3. Trim to `TP_LOOKBACK_CYCLES` most recent.
4. Partition into 6 regime×side buckets + aggregate.
5. For each bucket >= `TP_MIN_SAMPLES_PER_BUCKET`: compute `BucketStats` with censored observations merged.
6. Compute aggregate `BucketStats` (>= `TP_MIN_SAMPLES`).
7. Compute age pressure from open exits.
8. Compute utilization penalty from `free_doge` and total locked.
9. Cache all results for `size_for_slot()` calls until next update.

---

## 8. Status Payload

Replace `"kelly"` key in `/api/status` with `"throughput_sizer"`:

```json
{
  "enabled": true,
  "active_regime": "ranging",
  "last_update_n": 142,
  "age_pressure": 1.0,
  "util_penalty": 0.92,
  "oldest_open_exit_age_sec": 847,
  "util_ratio": 0.43,
  "aggregate": {
    "median_fill_sec": 1823,
    "p75_fill_sec": 3200,
    "p95_fill_sec": 8100,
    "mean_profit_per_sec": 0.000012,
    "n_completed": 142,
    "n_censored": 8,
    "multiplier": 1.0
  },
  "ranging_A": { "..." : "..." },
  "ranging_B": { "..." : "..." },
  "bullish_A": { "..." : "..." },
  "bullish_B": { "..." : "..." },
  "bearish_A": { "..." : "..." },
  "bearish_B": { "..." : "..." }
}
```

---

## 9. Dashboard

Replace "Kelly Sizing" card (HTML at dashboard.py:478-483, JS at 1689-1729) with:

**Title**: `Throughput Sizer`

**Rows**:
1. `Status`: OFF / WARMING / ACTIVE (same pattern as Kelly)
2. `Active Regime`: current HMM regime label
3. `Samples`: total completed cycles in window
4. `Age Pressure`: percentage (100% = no throttle, <100% = throttling)
5. `Utilization`: percentage of capital locked
6. `Buckets`: compact inline display of per-bucket multiplier + median fill time

**Bucket display format**: `ranging_A: x1.12 (30m) | ranging_B: x0.88 (45m) | ...`

---

## 10. Safety Invariants

1. Final multiplier is always clamped to `[TP_FLOOR_MULT, TP_CEILING_MULT]`.
2. Throughput sizer never bypasses existing min-volume guards (`compute_order_volume()` returning None).
3. Insufficient data → `multiplier = 1.0` (pass-through), never an error.
4. Age pressure floor prevents sizing from going to zero.
5. Utilization penalty floor prevents sizing from going to zero.
6. Censored observations only included when meaningfully aged (> 0.5x median).
7. `TP_ENABLED=False` → sizer returns `(base_order_usd, "tp_disabled")`, zero overhead.
8. Existing pause/resume, soft-close, and capacity telemetry unchanged.

---

## 11. Bot.py Integration Points (4 swaps)

### 11.1 Constructor (lines 403-418)
Replace `KellySizer` construction with `ThroughputSizer` construction.

### 11.2 Update (lines 2110-2117)
Replace `_update_kelly()` with `_update_throughput()`. Add open-exit collection and `free_doge` pass-through.

### 11.3 Sizing (lines 798-803)
Replace `self._kelly.size_for_slot(base_with_layers)` with `self._throughput.size_for_slot(base_with_layers, trade_id=trade_id)`.

### 11.4 Status (line 7700)
Replace `self._kelly.status_payload()` with `self._throughput.status_payload()`.

---

## 12. Testing

### Unit

1. **Bucket partitioning**: Cycles correctly split into 6 regime x side buckets.
2. **Fill-time stats**: Median, p75, p95 computed correctly from duration values.
3. **Censored observations**: Open exits merge into stats at reduced weight.
4. **Throughput multiplier**: Faster-than-average bucket → mult > 1.0; slower → mult < 1.0.
5. **Confidence blend**: Low-sample bucket blends toward 1.0.
6. **Age pressure**: Old exit triggers throttle; no old exits → 1.0.
7. **Utilization penalty**: High util → penalty < 1.0; low util → 1.0.
8. **Floor/ceiling clamp**: Final mult never outside `[TP_FLOOR_MULT, TP_CEILING_MULT]`.
9. **Insufficient data**: Returns multiplier 1.0 with `sufficient_data=False`.
10. **Disabled**: `TP_ENABLED=False` → pass-through.

### Integration

11. **Status payload**: `GET /api/status` returns `throughput_sizer` key with correct shape.
12. **Sizing pipeline**: `_slot_order_size_usd()` applies throughput multiplier after layers.
13. **Snapshot round-trip**: `snapshot_state()` → `restore_state()` preserves state.

---

## 13. Rollout

1. **Stage 1**: Deploy with `TP_ENABLED=False`. Kelly remains active if previously enabled. Verify no regressions.
2. **Stage 2**: Enable throughput sizer (`TP_ENABLED=True`, `KELLY_ENABLED=False`). Observe shadow telemetry in dashboard for 24h.
3. **Stage 3**: Live sizing. Monitor multiplier distribution and fill-time responsiveness across regime transitions.
4. **Stage 4**: Tune `TP_AGE_PRESSURE_*` and `TP_UTIL_*` based on observed stall patterns.
