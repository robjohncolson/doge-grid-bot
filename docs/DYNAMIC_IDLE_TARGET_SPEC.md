# Dynamic Idle Target Spec

Version: v1.1
Date: 2026-02-14
Status: Implementation-ready
Owner: Runtime (bot.py rebalancer)

---

## 1. Objective

Make the rebalancer's idle-USD target trend-aware so the bot holds minimal USD
during uptrends (deploying capital into DOGE) and holds more USD during
downtrends (preserving dry powder).

Current state: `REBALANCE_TARGET_IDLE_PCT` is a static 0.40. The PD controller
drives toward 40% idle regardless of market direction. During a DOGE rally the
bot accumulates excess USD that sits undeployed.

Target state: The idle target moves dynamically between a floor (0.15) and
ceiling (0.60) based on a trend signal derived from price history. The existing
PD controller, skew output, and size-scaling actuator remain unchanged.

---

## 2. Design Constraints

### Locked (from INVENTORY_REBALANCER_SPEC.md)

1. No market orders. All rebalancing through limit-order size skew.
2. No new order flow. Only adjusts size of orders the grid would place anyway.
3. entry_pct is sacred. Never touched by the rebalancer.
4. One-sided scaling only. Excess side scales up; deficit side stays at base.
5. Bounded risk: MAX_SIZE_MULT caps exposure amplification.

### New constraints (this spec)

6. The trend signal feeds ONLY into the idle target. It does not create new
   actuators, order types, or slot modes.
7. long_only / short_only are repair states, not allocation tools. This spec
   does not use them for trend expression.
8. The dynamic target must degrade gracefully to the static default when
   price history is insufficient (cold start, data gap).

---

## 3. Scope

### In scope

1. Trend score computation from price_history EMA crossover.
2. Dynamic idle target mapping from trend score.
3. Hysteresis and time-hold to prevent target flapping.
4. Config knobs for all tunable parameters.
5. Telemetry exposure in status payload and dashboard.

### Out of scope

1. Entry distance asymmetry (per-side entry_pct).
2. Slot-level mode switching (long_only/short_only for allocation).
3. Market-order accumulation sweeps.
4. Changes to the PD controller gains (KP, KD) or actuator logic.

---

## 4. Unit Convention

**All trend_score values in this spec are ratios, not percentages.**

- trend_score = 0.02 means "fast EMA is 2% above slow EMA"
- trend_score = -0.01 means "fast EMA is 1% below slow EMA"

The `* 100.0` conversion does NOT happen internally. Human-facing display
(dashboard, logs) may format as percentage for readability, but all
computation, config knobs, and persisted state use ratio form.

---

## 5. Trend Score Computation

### Signal: Dual-EMA crossover

Compute a fast and slow exponential moving average from the latest price,
updated on each rebalancer tick.

```
trend_score = (fast_ema - slow_ema) / slow_ema    # ratio (0.02 = 2%)
```

- Positive trend_score: DOGE rising (fast above slow). Deploy USD.
- Negative trend_score: DOGE falling (fast below slow). Hold USD.
- Near zero: ranging / no clear trend.

### EMA update

Reuse the same exponential smoothing formula already in the rebalancer:

```
alpha = 1.0 - exp(-dt / halflife)
ema_new = alpha * price + (1 - alpha) * ema_prev
```

Update both EMAs on each `_update_rebalancer()` call (every
REBALANCE_INTERVAL_SEC = 300s) using the latest price from
`self.last_price`.

### Cold start and restart behavior

Cold start applies when BOTH of these conditions are true:
1. Persisted EMA values are zero (fresh deploy, no prior state).
2. Fewer than `TREND_MIN_SAMPLES` price points exist in `price_history`.

When cold start applies: initialize both EMAs to `self.last_price` and set
`trend_score = 0.0`. The idle target falls back to the static base.

**Restart with persisted state**: If `_trend_fast_ema` and `_trend_slow_ema`
are nonzero from a prior snapshot, use them directly regardless of
`price_history` length. The EMAs carry forward across restarts. This avoids
a false cold-start signal when the process restarts but market state is
continuous.

**Data gap handling**: If `dt` since last update exceeds
`TREND_SLOW_HALFLIFE * 2` (8 hours with defaults), treat as cold start and
reinitialize both EMAs to current price. This prevents stale EMAs from
producing a phantom trend signal after a long outage.

---

## 6. Dynamic Idle Target

### Mapping

```
raw_target = REBALANCE_TARGET_IDLE_PCT - TREND_IDLE_SENSITIVITY * trend_score

dynamic_target = clamp(raw_target, TREND_IDLE_FLOOR, TREND_IDLE_CEILING)
```

### Config Parameters

| Parameter                          | Default  | Type  | Description                                                |
|------------------------------------|----------|-------|------------------------------------------------------------|
| `TREND_FAST_HALFLIFE`              | 1800.0   | float | Fast EMA halflife in seconds (30 min)                      |
| `TREND_SLOW_HALFLIFE`              | 14400.0  | float | Slow EMA halflife in seconds (4 hours)                     |
| `TREND_IDLE_SENSITIVITY`           | 5.0      | float | Idle-target shift per unit trend_score (0.01 score = 0.05 target shift) |
| `TREND_IDLE_FLOOR`                 | 0.15     | float | Minimum idle target (strong uptrend)                       |
| `TREND_IDLE_CEILING`               | 0.60     | float | Maximum idle target (strong downtrend)                     |
| `TREND_MIN_SAMPLES`                | 24       | int   | Minimum price samples for cold-start detection             |
| `TREND_HYSTERESIS_SEC`             | 600.0    | float | Minimum hold time before target can shift (seconds)        |
| `TREND_HYSTERESIS_SMOOTH_HALFLIFE` | 900.0    | float | EMA halflife for smoothing the target output (seconds)     |
| `TREND_DEAD_ZONE`                  | 0.001    | float | trend_score magnitude below which target stays at base (ratio, 0.001 = 0.1%) |

### Worked examples

At base target 0.40, sensitivity 5.0, trend_score in ratio form:

| Market condition     | trend_score | raw_target             | clamped_target |
|----------------------|-------------|------------------------|----------------|
| Strong uptrend       | +0.020      | 0.40 - 5.0×0.020 = 0.30 | 0.30         |
| Moderate uptrend     | +0.010      | 0.40 - 5.0×0.010 = 0.35 | 0.35         |
| Ranging              | +0.0005     | dead zone → 0.40       | 0.40           |
| Moderate downtrend   | -0.010      | 0.40 + 5.0×0.010 = 0.45 | 0.45         |
| Strong downtrend     | -0.030      | 0.40 + 5.0×0.030 = 0.55 | 0.55         |
| Extreme downtrend    | -0.050      | 0.40 + 5.0×0.050 = 0.65 | 0.60 (capped)|

---

## 7. Hysteresis

### Processing order

The hysteresis system has three stages applied in strict order:

1. **Dead zone** (first): If `abs(trend_score) < TREND_DEAD_ZONE`, the
   raw target equals the static base. No further processing.

2. **Time-hold** (second): If `now < _trend_target_locked_until`, the
   **controller output is frozen** at `_trend_dynamic_target`. The
   internal smoothing state (`_trend_smoothed_target`) is NOT updated
   during the hold. This guarantees zero drift during the lock period.

3. **Smoothing** (third, only when hold is not active): Apply EMA to the
   raw clamped target to produce the controller output:

   ```
   alpha = 1.0 - exp(-dt / TREND_HYSTERESIS_SMOOTH_HALFLIFE)
   _trend_smoothed_target = alpha * clamped_target + (1 - alpha) * _trend_smoothed_target
   ```

   If the new smoothed target differs from the previous controller output
   by more than 0.02, set `_trend_target_locked_until = now + TREND_HYSTERESIS_SEC`
   and freeze.

### Guarantees

- During hold: controller sees exactly the same target every tick.
- After hold expires: target glides via EMA toward the new raw value.
- Large jumps (>0.02) trigger a new hold, preventing step discontinuities.

---

## 8. Integration Point

### Where it connects

In `_update_rebalancer()` (bot.py ~line 3186), replace:

```python
target = clamp(config.REBALANCE_TARGET_IDLE_PCT, 0.0, 1.0)
```

with:

```python
target = self._compute_dynamic_idle_target(now)
```

Everything downstream (error computation, PD controller, skew output,
size scaling) remains unchanged. The dynamic target is the ONLY new input.

### New method

```python
def _compute_dynamic_idle_target(self, now: float) -> float:
    """Return trend-adjusted idle target for the rebalancer PD controller."""
    # 1. Check cold start / data gap → reinitialize EMAs if needed
    # 2. Update fast/slow EMAs from self.last_price
    # 3. Compute trend_score = (fast - slow) / slow  [ratio]
    # 4. Dead zone check → return base if |score| < TREND_DEAD_ZONE
    # 5. Compute raw_target, apply floor/ceiling clamp
    # 6. If hysteresis hold active → return frozen target
    # 7. Apply EMA smoothing to target
    # 8. If output jumped > 0.02 → engage new hold
    # 9. Store smoothed target as controller output, return it
```

### New persisted state

| Field                        | Type  | Default | Purpose                              |
|------------------------------|-------|---------|--------------------------------------|
| `_trend_fast_ema`            | float | 0.0     | Fast EMA of price                    |
| `_trend_slow_ema`            | float | 0.0     | Slow EMA of price                    |
| `_trend_score`               | float | 0.0     | Current trend score (ratio)          |
| `_trend_dynamic_target`      | float | 0.40    | Controller output (frozen during hold) |
| `_trend_smoothed_target`     | float | 0.40    | Internal EMA state for smoothing     |
| `_trend_target_locked_until` | float | 0.0     | Hysteresis hold expiry timestamp     |
| `_trend_last_update_ts`      | float | 0.0     | Timestamp of last EMA update         |

All fields saved/restored in `_global_snapshot()` / `_load_snapshot()`
alongside existing rebalancer state. Fields absent from old snapshots
default to the values above (backward compatible).

---

## 9. Telemetry

### Status payload changes

**Update existing field** `rebalancer.target` (bot.py:3577) to emit the
dynamic target instead of the static config value. Add `rebalancer.base_target`
for the static reference:

```json
{
  "rebalancer": {
    "target": 0.32,
    "base_target": 0.40,
    "skew": 0.18,
    "idle_ratio": 0.77
  }
}
```

**Add trend block** as a sibling to the rebalancer block:

```json
{
  "trend": {
    "score": 0.0123,
    "score_display": "+1.23%",
    "fast_ema": 0.10541,
    "slow_ema": 0.10412,
    "dynamic_idle_target": 0.32,
    "hysteresis_active": false,
    "hysteresis_expires_in_sec": 0
  }
}
```

### Dashboard display

Add to the DOGE Bias Scoreboard card:

```
Trend Score     +1.23%  (▲ uptrend)
Idle Target     32.0%   (base 40.0%, trend-adjusted)
```

Color coding:
- score > +0.005: green (uptrend, deploying)
- score < -0.005: red (downtrend, preserving)
- within dead zone: gray (ranging)

---

## 10. Interaction with Existing Systems

### Rebalancer PD controller

No changes to KP, KD, EMA halflife, neutral band, slew rate, or oscillation
damping. The dynamic target is a pre-processing step that feeds into the
existing `raw_error = idle_ratio - target` computation.

### Capacity gating

The existing guard remains: if capacity band is "caution" or "stop", skew
zeroes regardless of trend. This prevents the dynamic target from pushing
orders into a capacity-constrained book.

### Volatility auto-profit

Independent system. The trend score does NOT affect profit targets. Profit
target adaptation continues through the existing volatility and directional
squeeze pathways.

### Entry distance asymmetry (detected_trend)

The `detected_trend` signal and `DIRECTIONAL_ASYMMETRY` config exist in
grid_strategy.py but are NOT active in the current bot.py/state_machine.py
runtime path. If they are wired in the future, they would operate on a
different actuator (entry distances) and would not conflict with this spec's
idle-target adjustment. No coordination needed.

---

## 11. Testing Plan

### Unit tests

1. `_compute_dynamic_idle_target` returns base target when both persisted
   EMAs are zero and price_history has fewer than TREND_MIN_SAMPLES entries.
2. With nonzero persisted EMAs and empty price_history (restart scenario),
   EMAs are used directly — no cold-start reset.
3. Data gap > 2× slow halflife resets EMAs to current price.
4. Positive trend_score reduces idle target (floor-bounded at 0.15).
5. Negative trend_score increases idle target (ceiling-bounded at 0.60).
6. Dead zone: trend_score within ±TREND_DEAD_ZONE returns base target.
7. Hysteresis: target does not change during hold period (frozen output,
   no smoothing drift).
8. Hysteresis: hold engages when output jumps > 0.02.
9. EMA convergence: fast EMA tracks price more closely than slow EMA
   during a monotonic price ramp.

### Snapshot persistence tests

10. New trend fields are saved in `_global_snapshot()` output.
11. `_load_snapshot()` with missing trend fields (old snapshot format)
    initializes to defaults without error.
12. `_load_snapshot()` with present trend fields restores all values
    including EMAs, score, target, and lock timestamp.

### Integration tests

13. Simulate 4h uptrend → verify skew output increases (more B-side bias).
14. Simulate flat market → verify target stays near base.
15. Verify `rebalancer.target` in status payload reflects dynamic value,
    not static config.
16. Verify `rebalancer.base_target` in status payload reflects static config.
17. Verify `trend` block in status payload includes all fields.

---

## 12. Rollout

1. Deploy with `TREND_IDLE_SENSITIVITY=0.0` (disabled — dynamic target
   equals static base). Verify no behavioral change. Verify telemetry
   fields appear in status payload with score=0.0.
2. Set `TREND_IDLE_SENSITIVITY=3.0` (conservative). Monitor for 24h.
   Watch `trend.score` and `rebalancer.target` in dashboard.
3. If stable, raise to `TREND_IDLE_SENSITIVITY=5.0` (default).
4. Tune `TREND_IDLE_FLOOR` and `TREND_IDLE_CEILING` based on observed
   idle ratios and capital deployment.

### Rollback

Set `TREND_IDLE_SENSITIVITY=0.0` via env var. No code revert needed.
The dynamic target becomes the static base and behavior is identical
to pre-feature.

---

## 13. Summary

This spec adds exactly one new input to the rebalancer: a trend-adjusted
idle target. No new actuators, no new order types, no slot mode changes.
The trend signal (dual-EMA crossover) is computed from data already
collected. The integration point is a single line substitution in
`_update_rebalancer()`. Rollback is a single env var.

The expected outcome: during DOGE uptrends, the idle target drops from
40% toward 15%, causing the PD controller to produce larger positive
skew, which scales up B-side (buy) orders, deploying more USD into DOGE.
During downtrends, the target rises toward 60%, reducing buy pressure
and preserving USD.
