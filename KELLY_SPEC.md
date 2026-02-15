# Kelly Criterion Position Sizer — Spec v1.1

Last updated: 2026-02-15 (rev 2)
Primary code reference: `kelly_sizer.py`
Parent spec: `STATE_MACHINE.md` (§9 Order Sizing, §17 HMM Regime Detector, §18 Directional Regime System)

> **Rev 2 changes**: Added `KELLY_ENABLED` master toggle, clarified HMM-optional operation,
> fixed negative-edge clamp consistency, added `CompletedCycle` migration to integration checklist,
> added recency weighting interpretation caveat, fixed `pair_model.py` filename reference.

## 1. Scope

This document is the implementation contract for the Kelly criterion position sizing module.

- **Master toggle**: `KELLY_ENABLED` (default `False`). When disabled, the module is not instantiated and all sizing passes through unchanged. This allows safe deployment and gradual rollout.
- Purpose: regime-conditional optimal position sizing based on empirical win/loss statistics
- Integration point: `_slot_order_size_usd()` in `bot.py`, downstream of base + layer computation (STATE_MACHINE.md §9)
- Data source: `completed_cycles` from all slots, persisted in `exit_outcomes` Supabase table
- Regime source: HMM regime detector (STATE_MACHINE.md §17) via `regime_id` at entry time

### 1.1 HMM Dependency

Kelly does **not** require HMM to be enabled. The two operating modes:

| HMM State | Kelly Behavior |
|-----------|---------------|
| `HMM_ENABLED=True` | Full regime-conditional Kelly — cycles tagged with `regime_at_entry`, per-regime fractions computed, regime-specific sizing applied |
| `HMM_ENABLED=False` | Aggregate-only Kelly — all cycles land in the `"unknown"` bucket (no regime tag), only aggregate Kelly fraction is computed and applied. Per-regime columns in the dashboard show "no data." |

Aggregate-only mode is fully functional and still provides value: it adjusts sizing based on overall win/loss statistics even without regime awareness. Enabling HMM later automatically unlocks per-regime Kelly as tagged cycles accumulate past `min_samples_per_regime`.

Out of scope for v1:

- Per-slot Kelly (all slots share a single regime-conditional Kelly fraction)
- Kelly-informed `entry_pct` or `profit_pct` adjustment (sizing only)
- Drawdown-conditional Kelly shrinkage (future candidate)
- Multi-pair Kelly allocation (blocked on multi-pair swarm activation)

## 2. Theoretical Foundation

### 2.1 Classic Kelly Formula

Given a repeated wager with binary outcomes:

```
f* = (bp - q) / b
```

Where:

- `f*` = optimal fraction of bankroll to risk per bet
- `b` = payoff ratio (average win / average loss)
- `p` = probability of winning
- `q` = 1 - p (probability of losing)
- `bp - q` = edge (expected value per unit risked)

Properties:

- `f* > 0` implies positive expected value (edge exists)
- `f* <= 0` implies no edge or negative edge (should not bet, or should reduce exposure)
- `f* = 1` implies certainty (all wins, zero losses)
- Maximizes long-run geometric growth rate of bankroll
- Assumes known, stationary probabilities (violated in practice)

### 2.2 Fractional Kelly

Full Kelly is optimal in theory but assumes perfect knowledge of edge. In practice, edge estimates are noisy and overestimation leads to ruin. Fractional Kelly reduces variance at the cost of slower growth:

```
f_applied = f* × fraction
```

Standard practice:

- `fraction = 1.0`: full Kelly (maximum growth, maximum variance)
- `fraction = 0.50`: half Kelly (common in professional gambling)
- `fraction = 0.25`: quarter Kelly (conservative; recommended default for crypto)
- `fraction = 0.10`: tenth Kelly (ultra-conservative, near-linear growth)

Quarter Kelly is the default for this system because:

1. DOGE/USD exhibits fat-tailed returns that violate Kelly's Gaussian assumptions
2. The HMM regime detector introduces regime classification error into `p` estimates
3. Grid bot cycles are not strictly independent (correlated via market regime)
4. Ruin is permanent; slower growth is acceptable

### 2.3 Regime-Conditional Extension

The bot's HMM detector classifies the market into three regimes (STATE_MACHINE.md §17):

- Regime 0: bearish
- Regime 1: ranging
- Regime 2: bullish

Win probability `p` and payoff ratio `b` are not stationary across regimes. A grid bot's A-side (short) has different edge characteristics in a trending bull market vs. a range-bound market. Therefore Kelly should be computed per-regime:

```
f*_regime = (b_regime × p_regime - q_regime) / b_regime
```

This yields separate sizing multipliers per regime, selected at runtime by the current `regime_tier` state.

## 3. Architecture

### 3.1 Module Boundary

```
kelly_sizer.py
├── KellyConfig          (dataclass: all tunables)
├── KellyResult          (dataclass: full computation diagnostics)
├── compute_kelly_fraction()   (pure function: wins/losses → KellyResult)
├── partition_cycles_by_regime()  (pure function: cycles → regime buckets)
└── KellySizer           (stateful runtime class for bot.py integration)
    ├── update()         (recompute from cycle history + current regime)
    ├── size_for_slot()  (apply multiplier to base order size)
    ├── status_payload() (dashboard telemetry)
    ├── snapshot_state() (persistence serialization)
    └── restore_state()  (persistence deserialization)
```

Design constraints (aligned with STATE_MACHINE.md §6 reducer contract):

1. All Kelly computations are pure functions with no network side effects.
2. `KellySizer` holds cached results only; all inputs arrive via `update()`.
3. No modification to the reducer. Kelly sizing is an advisory layer in `bot.py`, parallel to HMM regime detection.
4. Sizing output is a multiplier on the existing `base_with_layers` value — it does not replace or bypass the existing sizing pipeline.

### 3.2 Data Flow

```
completed_cycles (all slots)
        │
        ▼
partition_cycles_by_regime()
        │
        ├── bearish[]
        ├── ranging[]
        ├── bullish[]
        └── aggregate[]
                │
                ▼ (per bucket)
        compute_kelly_fraction()
                │
                ▼
        KellyResult per regime
                │
                ▼
        size_for_slot(base_order_usd, current_regime)
                │
                ▼
        adjusted_order_usd → _slot_order_size_usd()
```

### 3.3 Integration with Existing Sizing Pipeline

Current pipeline (STATE_MACHINE.md §9):

```
base = max(ORDER_SIZE_USD, ORDER_SIZE_USD + slot.total_profit)
layer_usd = effective_layers × CAPITAL_LAYER_DOGE_PER_ORDER × market_price
base_with_layers = max(base, base + layer_usd)
```

With Kelly inserted:

```
base_with_layers = <existing computation>
kelly_usd, kelly_reason = self._kelly.size_for_slot(base_with_layers)
effective = kelly_usd
```

The rebalancer size-skew actuator (§14, currently disabled) would apply after Kelly if re-enabled:

```
base_with_layers → Kelly multiplier → rebalancer skew multiplier → fund guard → final size
```

## 4. Cycle Tagging: `regime_at_entry`

### 4.1 Requirement

Each completed cycle must carry the HMM regime ID that was active when the cycle's entry order filled. This enables partitioning historical cycles by the regime in which they were initiated.

### 4.2 Tagging Point

The tag is stamped in `bot.py` (not in the reducer) to preserve reducer purity.

When a `FillEvent` for an entry triggers a `PlaceOrderAction` for the corresponding exit:

```python
# In bot.py, after reducer returns actions for an entry fill:
new_exit_order.regime_at_entry = self._current_regime_id()
```

The `regime_at_entry` value propagates through the exit order and into the `completed_cycle` record when the exit fills and `_book_cycle()` runs.

### 4.3 Persistence

The `regime_at_entry` field must be:

- Stored in the serialized `PairState` order dict (snapshot persistence)
- Written to the `exit_outcomes` Supabase table as a column
- Written to the `fills` table for auditability

Cycles that predate this feature (no `regime_at_entry` field) are placed in the `"unknown"` bucket and contribute only to the aggregate Kelly computation.

## 5. Kelly Computation

### 5.1 Input Preparation

From the rolling window of most recent `lookback_cycles` completed cycles (default 500), extract:

- **Wins**: cycles where `net_profit > 0` → collect `net_profit` values
- **Losses**: cycles where `net_profit <= 0` → collect `abs(net_profit)` values

### 5.2 Recency Weighting

When `use_recency_weighting = True` (default), exponential decay weights are applied:

```
weight(rank) = exp(-ln(2) / halflife × rank)
```

Where `rank` is the cycle's position when sorted by `exit_time` descending (most recent = rank 0).

Effect: recent cycles contribute more to win rate and average win/loss estimates. This adapts Kelly to non-stationary edge dynamics without requiring explicit change-point detection.

Weighted win rate (recency-weighted proportion):

```
p = sum(weights_wins) / (sum(weights_wins) + sum(weights_losses))
```

> **Interpretation caveat**: This computes a recency-weighted *proportion*, not a classical probability estimate. If recent cycles are clustered (e.g., 5 consecutive wins), their higher weights amplify the contribution to `p` beyond what a uniform sample would yield. This is intentional — we *want* recent performance to dominate — but it means `p` is better understood as "recency-weighted win proportion" than "true probability of winning." The conservative fractional Kelly (0.25×) absorbs the estimation error this introduces.

Weighted average win/loss:

```
avg_win = sum(w_i × win_i) / sum(w_i)
avg_loss = sum(w_j × loss_j) / sum(w_j)
```

### 5.3 Kelly Computation

```
b = avg_win / avg_loss          (payoff ratio)
f* = (b × p - q) / b           (raw Kelly fraction)
edge = b × p - q               (expected value per unit risked)
f_fractional = f* × fraction   (applied Kelly fraction)
multiplier = 1.0 + f_fractional
```

### 5.4 Edge Cases

| Condition | Behavior |
|-----------|----------|
| No cycles (`n = 0`) | Return `multiplier = 1.0`, `reason = "no_data"` |
| All wins (`avg_loss = 0`) | Cap `f* = 1.0`, `multiplier = 1.0 + fraction` |
| No edge (`f* <= 0`) | Return `multiplier = 1.0`, `reason = "no_edge"` |
| Insufficient samples | Return `multiplier = 1.0`, `reason = "insufficient_samples"` |

## 6. Sizing Application

### 6.1 Regime Resolution

When `size_for_slot()` is called:

1. Look up `KellyResult` for current `regime_label`
2. If regime-specific result is unavailable or `sufficient_data = False`, fall back to `"aggregate"` result
3. If aggregate is also insufficient, return `base_order_usd` unchanged (`"kelly_inactive"`)

### 6.2 Multiplier Clamping

```
raw_multiplier = result.multiplier
clamped = clamp(raw_multiplier, kelly_floor_mult, kelly_ceiling_mult)
adjusted_usd = base_order_usd × clamped
```

Defaults:

- `kelly_floor_mult = 0.5` (never size below 50% of base)
- `kelly_ceiling_mult = 2.0` (never size above 200% of base)

### 6.3 Negative Edge Handling

When Kelly detects no edge (`f* <= 0`):

```
raw_mult = negative_edge_mult
clamped = clamp(raw_mult, kelly_floor_mult, kelly_ceiling_mult)
adjusted_usd = base_order_usd × clamped
```

Default `negative_edge_mult = 0.5`. The clamp from §6.2 is applied uniformly — negative edge sizing goes through the same floor/ceiling as positive edge sizing. This prevents configuration conflicts where `negative_edge_mult < kelly_floor_mult` would silently bypass the floor.

Rationale for shrinking rather than halting:

1. Kelly's "no edge" may be a sample artifact during regime transitions
2. The grid still earns spread on mean-reverting moves even with negative aggregate edge
3. Halting would conflict with the bot's bootstrap and degradation model

### 6.4 Interaction with Existing Guardrails

Kelly sizing respects all existing guardrails without modification:

- **Fund guard** (§9): scaling never exceeds available balance (applied after Kelly)
- **Daily loss lock** (§13): aggregate circuit breaker still fires regardless of Kelly sizing
- **Capacity gating** (§12, §23.2): entry scheduler and capacity bands are upstream of Kelly
- **Regime suppression** (§18): tier 2 suppression cancels entries entirely; Kelly only adjusts size of entries that are permitted to exist

## 7. Sample Gating

### 7.1 Minimum Sample Requirements

Kelly fractions computed from small samples are unreliable. Two gates:

| Gate | Default | Effect |
|------|---------|--------|
| `min_samples_total` | 30 | Below this, Kelly is entirely inactive (`multiplier = 1.0`) |
| `min_samples_per_regime` | 15 | Below this per-regime, that regime falls back to aggregate |

### 7.2 Lookback Window

Only the most recent `lookback_cycles` (default 500) cycles are considered. This:

- Prevents ancient cycle data from diluting current edge estimates
- Bounds computation time
- Works with recency weighting to focus on recent market behavior

### 7.3 Activation Sequence

On a fresh bot with no history:

```
Cycles 0–29:   Kelly inactive, all sizing at base
Cycles 30+:    Aggregate Kelly activates
                Per-regime Kelly activates as each bucket reaches 15 samples
```

## 8. Update Cadence

### 8.1 Timing

`KellySizer.update()` is called in the main loop alongside `_update_regime_tier()`, gated by `REGIME_EVAL_INTERVAL_SEC` (default 300s).

Rationale: Kelly fractions change slowly (they depend on hundreds of cycles, not individual ticks), so recomputing every 5 minutes is sufficient. Tighter coupling to regime eval ensures the `_active_regime` label is current when sizing decisions are made.

### 8.2 Cycle Collection

At each update, all `completed_cycles` across all active slots are collected and passed to `update()`. This is a read-only scan of in-memory `PairState` data — no Supabase queries in the hot path.

For historical backfill (e.g., after a restart with empty memory), cycles can be loaded from the `exit_outcomes` Supabase table during bootstrap.

## 9. Configuration

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `kelly_enabled` | `KELLY_ENABLED` | `False` | Master toggle. When `False`, module is not instantiated. |
| `kelly_fraction` | `KELLY_FRACTION` | `0.25` | Fractional Kelly multiplier (0.25 = quarter Kelly) |
| `min_samples_total` | `KELLY_MIN_SAMPLES` | `30` | Minimum total cycles before Kelly activates |
| `min_samples_per_regime` | `KELLY_MIN_REGIME_SAMPLES` | `15` | Minimum cycles per regime bucket |
| `lookback_cycles` | `KELLY_LOOKBACK` | `500` | Rolling window of recent cycles |
| `kelly_floor_mult` | `KELLY_FLOOR_MULT` | `0.5` | Minimum sizing multiplier |
| `kelly_ceiling_mult` | `KELLY_CEILING_MULT` | `2.0` | Maximum sizing multiplier |
| `negative_edge_mult` | `KELLY_NEGATIVE_EDGE_MULT` | `0.5` | Multiplier when no edge detected (clamped by floor/ceiling per §6.3) |
| `use_recency_weighting` | `KELLY_RECENCY_WEIGHTING` | `True` | Enable exponential decay weighting |
| `recency_halflife_cycles` | `KELLY_RECENCY_HALFLIFE` | `100` | Halflife for recency decay |
| `log_kelly_updates` | `KELLY_LOG_UPDATES` | `True` | Log Kelly summary on each update |

### 9.1 Tuning Guidance

**`kelly_fraction`**: Start at 0.25 (quarter Kelly). If drawdowns are acceptable and edge estimates are stable, move to 0.50. Never exceed 0.50 for crypto without extensive backtesting.

**`min_samples_total`**: 30 is a statistical minimum for meaningful win-rate estimates. Increase to 50–100 if cycle frequency is high (many cycles per day) and you want more confidence before activation.

**`lookback_cycles`**: 500 balances recency with sample size. For DOGE/USD with typical grid parameters, this represents roughly 2–4 weeks of cycle history. Decrease if market regime shifts are frequent and you want faster adaptation.

**`kelly_floor_mult`**: 0.5 prevents Kelly from shrinking positions to near-zero during temporary negative-edge periods. Set to 1.0 to disable Kelly downsizing entirely (Kelly can only upsize).

**`kelly_ceiling_mult`**: 2.0 caps aggressive compounding. Critical safety parameter — increasing this amplifies both gains and drawdowns.

## 10. Persistence

### 10.1 Snapshot Fields

Added to the `bot_state` snapshot payload (STATE_MACHINE.md §22):

```
kelly_state:
  active_regime: str          # current regime label
  last_update_n: int          # cycle count at last update
  results: dict               # cached KellyResult per regime (serialized)
```

### 10.2 Backward Compatibility

Snapshots without `kelly_state` default to:

- `active_regime = "ranging"`
- `last_update_n = 0`
- Empty results (Kelly inactive until first `update()` call)

### 10.3 Cold Start

On bot restart, `KellySizer.restore_state()` restores cached regime label only. Actual Kelly results are recomputed on the first `update()` call from in-memory `completed_cycles` (which are restored from the slot snapshot) or from `exit_outcomes` Supabase backfill.

## 11. Dashboard Telemetry

### 11.1 `/api/status` Payload Block

```json
"kelly": {
  "enabled": true,
  "active_regime": "bullish",
  "last_update_n": 347,
  "kelly_fraction": 0.25,
  "aggregate": {
    "f_star": 0.1648,
    "f_fractional": 0.0412,
    "multiplier": 1.0412,
    "win_rate": 0.5602,
    "avg_win": 0.010454,
    "avg_loss": 0.009398,
    "payoff_ratio": 1.1124,
    "n_total": 347,
    "n_wins": 194,
    "n_losses": 153,
    "edge": 0.1833,
    "sufficient_data": true,
    "reason": "ok"
  },
  "bullish": { ... },
  "ranging": { ... },
  "bearish": { ... }
}
```

### 11.2 Diagnostic Interpretation

| Metric | Healthy Range | Concern |
|--------|--------------|---------|
| `win_rate` | 0.50–0.70 | Below 0.45: grid parameters may be too tight |
| `payoff_ratio` | 0.8–2.0 | Below 0.8: profit_pct too low relative to loss magnitude |
| `edge` | > 0 | Negative: no edge in this regime, sizing is floored |
| `f_star` | 0.05–0.50 | Above 0.50: suspiciously high, check for overfitting |
| `multiplier` | 0.5–2.0 | Clamped by floor/ceiling; values near bounds suggest extreme conditions |

### 11.3 Logging

Each `update()` emits one log line per regime bucket:

```
kelly [bullish] f*=0.4904 f_frac=0.1226 mult=1.123 win_rate=69.42% payoff=1.500 edge=0.7356 n=28 (20W/8L)
kelly [ranging] f*=0.1004 f_frac=0.0251 mult=1.025 win_rate=52.65% payoff=1.111 edge=0.1115 n=22 (12W/10L)
kelly [bearish] f*=-0.5549 f_frac=0.0000 mult=1.000 win_rate=39.53% payoff=0.636 edge=-0.3531 n=13 (5W/8L)
kelly [aggregate] f*=0.1648 f_frac=0.0412 mult=1.041 win_rate=56.02% payoff=1.112 edge=0.1833 n=63 (37W/26L)
```

## 12. Integration Checklist

### 12.1 Files Modified

| File | Change |
|------|--------|
| `kelly_sizer.py` | New file (this module) |
| `bot.py` | Import `KellySizer`, construct at init (gated by `KELLY_ENABLED`), call `update()` in regime eval, call `size_for_slot()` in `_slot_order_size_usd()`, add to snapshot save/load, add to `status_payload()` |
| `config.py` | Add `KELLY_ENABLED` + `KELLY_*` env vars |
| `grid_strategy.py` | Add optional `regime_at_entry: int | None` field to `CompletedCycle.__init__()`, `to_dict()`, `from_dict()` (backward-compatible: defaults to `None`, `from_dict()` uses `.get()`) |
| `pair_model.py` | No changes (reducer purity preserved) |
| `supabase_store.py` | Add `regime_at_entry` column to `exit_outcomes` writes (auto-detect pattern: strip if column missing) |
| `dashboard.py` | Add `kelly` block to status display |
| `STATE_MACHINE.md` | Add §X cross-reference to this spec |

### 12.2 Supabase Schema

Add column to `exit_outcomes`:

```sql
ALTER TABLE exit_outcomes ADD COLUMN regime_at_entry INTEGER DEFAULT NULL;
```

No index required (column is read in bulk during backfill, not queried individually).

**Migration ordering**: The schema migration must be applied *before* deploying Kelly-tagged code. The existing Supabase integration auto-detects columns and silently strips unknown ones on write — if the column doesn't exist yet, `regime_at_entry` values will be lost without error. Run the `ALTER TABLE` first.

### 12.3 grid_strategy.py Migration

Add `regime_at_entry` to `CompletedCycle`:

```python
# In __init__:
def __init__(self, ..., regime_at_entry: int | None = None):
    ...
    self.regime_at_entry = regime_at_entry

# In to_dict():
d["regime_at_entry"] = self.regime_at_entry

# In from_dict():
regime_at_entry=d.get("regime_at_entry")
```

This is backward-compatible: existing serialized cycles without the field will deserialize with `regime_at_entry=None` and land in the `"unknown"` bucket for Kelly partitioning.

### 12.4 bot.py Integration Steps

1. **Constructor**: if `config.KELLY_ENABLED`, instantiate `KellySizer(KellyConfig(...))` after config load; otherwise `self._kelly = None`
2. **Bootstrap**: if Kelly enabled, call `restore_state()` from snapshot, optionally backfill from `exit_outcomes`
3. **Entry fill handler**: stamp `regime_at_entry` on new exit orders (stamp regardless of `KELLY_ENABLED` so data accumulates for future activation)
4. **Regime eval interval**: if Kelly enabled, collect cycles, call `update(cycles, regime_label)`
5. **`_slot_order_size_usd()`**: if `self._kelly`, call `size_for_slot(base_with_layers)` after layer computation; otherwise pass through unchanged
6. **Snapshot save**: include `kelly_state` via `snapshot_state()` (omit if Kelly disabled)
7. **Status payload**: include `kelly` block via `status_payload()` (report `{"enabled": false}` if disabled)

## 13. Risk Considerations

### 13.1 Known Limitations

1. **Edge estimation noise**: Kelly assumes `p` and `b` are known. In practice they are estimated from finite samples and subject to regime shift. Fractional Kelly (0.25) mitigates this but does not eliminate it.

2. **Cycle dependence**: Kelly assumes independent bets. Grid bot cycles within the same regime window are correlated (driven by the same price moves). This means true Kelly fraction is lower than computed. The conservative default fraction accounts for this.

3. **Survivorship in cycle data**: Cycles that result in orphaned recoveries (STATE_MACHINE.md §11) are not fully represented in `completed_cycles` until the recovery fills or is evicted. This creates a mild positive bias in win-rate estimates during periods of high orphan accumulation.

4. **Regime classification error**: If the HMM misclassifies the current regime, Kelly will apply the wrong regime's fraction. The fallback-to-aggregate mechanism limits damage, but regime transition periods remain vulnerable.

### 13.2 Safety Boundaries

The following hard limits prevent Kelly from causing catastrophic sizing:

| Boundary | Mechanism | Limit |
|----------|-----------|-------|
| Position size floor | `kelly_floor_mult` | Never below 50% of base |
| Position size ceiling | `kelly_ceiling_mult` | Never above 200% of base |
| Daily loss | `DAILY_LOSS_LIMIT` (§13) | Circuit breaker unchanged |
| Capacity | Entry scheduler + capacity bands | Upstream of Kelly |
| Balance | Fund guard in `_slot_order_size_usd` | Downstream of Kelly |

### 13.3 Degradation Modes

| Condition | Kelly Behavior |
|-----------|---------------|
| No cycle history | Inactive; sizing unchanged |
| Too few samples | Inactive; sizing unchanged |
| Regime-specific data insufficient | Falls back to aggregate |
| All regimes insufficient | Inactive |
| Supabase unavailable | Operates from in-memory cycles only |
| HMM detector unavailable | Uses aggregate Kelly (no regime split) |

## 14. Future Candidates (Not in v1)

1. **Drawdown-conditional shrinkage**: reduce Kelly fraction during drawdowns (e.g., halve fraction when drawdown exceeds 2× daily loss limit)
2. **Per-slot Kelly**: compute separate fractions per slot based on each slot's cycle history
3. **Kelly-informed profit target**: adjust `profit_pct` based on regime-specific payoff ratio
4. **Multi-pair Kelly allocation**: when swarm mode activates, allocate capital across pairs using Kelly
5. **Bayesian Kelly**: use posterior distribution over `p` and `b` rather than point estimates, yielding a more conservative Kelly fraction that accounts for estimation uncertainty
6. **A/B-side split Kelly**: compute separate Kelly for A-leg (short) and B-leg (long) cycles, since edge profile differs by trade direction within the same regime

## 15. Developer Notes

When updating behavior, update these files together:

1. `kelly_sizer.py` for computation and runtime integration
2. `bot.py` for sizing pipeline and telemetry
3. `KELLY_SPEC.md` (this document) for contract parity
4. `STATE_MACHINE.md` §9 for cross-reference

This document is intentionally code-truth first: if this file and code diverge, code wins and doc must be updated in the same change.
