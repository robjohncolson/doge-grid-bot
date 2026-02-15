# HMM Regime Detector — Integration with STATE_MACHINE v1

Last updated: 2026-02-15

## 1. Architectural Fit

The HMM regime detector is a **read-only advisory layer**. It does not
modify the reducer, does not emit actions, and does not touch the event
transition rules. It lives entirely in `bot.py`'s runtime layer, alongside
the existing rebalancer (§14) and trend detector (§15).

```
                    ┌──────────────────────────────────────────┐
                    │              bot.py runtime               │
                    │                                          │
 Kraken OHLCV ──►  │  FeatureExtractor  ──►  RegimeDetector   │
 (ohlcv_candles)    │       │                      │           │
                    │       │              RegimeState          │
                    │       │           (bias_signal,           │
                    │       │            confidence,            │
                    │       │            regime)                │
                    │       ▼                  │                │
                    │  ┌─────────┐             │                │
                    │  │ Trend   │◄────blend───┘                │
                    │  │ §15     │                              │
                    │  └────┬────┘                              │
                    │       │ dynamic_idle_target               │
                    │       ▼                                   │
                    │  ┌──────────┐                             │
                    │  │Rebalancer│ ◄── grid_bias (optional)   │
                    │  │ §14      │                             │
                    │  └────┬─────┘                             │
                    │       │ skew                              │
                    │       ▼                                   │
                    │  _slot_order_size_usd() (§9)             │
                    └──────────────────────────────────────────┘
                                    │
                                    ▼
                    ┌──────────────────────────────┐
                    │  state_machine.py (untouched) │
                    │  transition() pure reducer    │
                    └──────────────────────────────┘
```

The reducer contract (§6) is **not modified**. All HMM influence flows
through existing config parameters that are already tunable at runtime.


## 2. Integration Points in bot.py

### 2.1 Initialization (in `__init__` or startup)

```python
from hmm_regime_detector import RegimeDetector, restore_from_snapshot

self._hmm = RegimeDetector(config={
    "HMM_BLEND_WITH_TREND": 0.5,       # tune this: 0=pure HMM, 1=ignore HMM
    "HMM_CONFIDENCE_THRESHOLD": 0.15,
    "HMM_RETRAIN_INTERVAL_SEC": 86400,
})

# Restore state from snapshot (§19) if available
if "_hmm_regime_state" in snapshot:
    restore_from_snapshot(self._hmm, snapshot)
```

### 2.2 Training (startup, after OHLCV sync/backfill)

```python
# After FETCH INITIAL PRICE in lifecycle (§3)
# Sync OHLCV, optionally backfill, then train from persisted candles
self._sync_ohlcv_candles(_now())
self._maybe_backfill_ohlcv_on_startup()
closes, volumes = self._fetch_training_candles(
    count=int(getattr(config, "HMM_TRAINING_CANDLES", 2000))
)
self._hmm.train(closes, volumes)
```

### 2.3 Per-Tick Inference (in main loop, step 12 — rebalancer update)

```python
# Inside _update_rebalancer(), after computing trend_score
closes_recent, volumes_recent = self._fetch_recent_candles(count=100)
regime_state = self._hmm.update(closes_recent, volumes_recent)

# OPTION A: Blend with existing §15 trend_score
# Replace the raw_target line in _compute_dynamic_idle_target()
from hmm_regime_detector import compute_blended_idle_target

dynamic_target = compute_blended_idle_target(
    trend_score=self._trend_score,
    hmm_bias=regime_state.bias_signal,
    blend_factor=self._hmm.cfg["HMM_BLEND_WITH_TREND"],
    base_target=cfg.REBALANCE_TARGET_IDLE_PCT,
    sensitivity=cfg.TREND_IDLE_SENSITIVITY,
    floor=cfg.TREND_IDLE_FLOOR,
    ceiling=cfg.TREND_IDLE_CEILING,
)

# OPTION B: Additionally adjust grid spacing (more aggressive)
from hmm_regime_detector import compute_grid_bias
grid_bias = compute_grid_bias(regime_state)
# Apply spacing multipliers when computing entry prices
```

### 2.4 Periodic Retrain (in main loop, lightweight check)

```python
# At the end of main loop, check if retrain is due
if self._hmm.needs_retrain():
    closes, volumes = self._fetch_training_candles(
        count=int(getattr(config, "HMM_TRAINING_CANDLES", 2000))
    )
    self._hmm.train(closes, volumes)
```

### 2.5 Persistence (in save_state, §19)

```python
from hmm_regime_detector import serialize_for_snapshot

snapshot_payload = {
    # ... existing fields ...
    **serialize_for_snapshot(self._hmm),
}
```

### 2.6 Dashboard Telemetry (in status_payload, §20)

Add to `/api/status` response:

```python
"hmm_regime": {
    "regime": Regime(self._hmm.state.regime).name,
    "confidence": self._hmm.state.confidence,
    "bias_signal": self._hmm.state.bias_signal,
    "probabilities": {
        "bearish": self._hmm.state.probabilities[0],
        "ranging": self._hmm.state.probabilities[1],
        "bullish": self._hmm.state.probabilities[2],
    },
    "trained": self._hmm._trained,
    "observation_count": self._hmm.state.observation_count,
    "blend_factor": self._hmm.cfg["HMM_BLEND_WITH_TREND"],
},

"hmm_data_pipeline": {
    "interval_min": int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)),
    "sync_interval_sec": float(getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 60.0)),
    "samples": sample_count,
    "coverage_pct": coverage_pct,
    "freshness_sec": freshness_sec,
    "freshness_limit_sec": freshness_limit_sec,  # max(180s, interval*3)
    "freshness_ok": freshness_ok,
    "backfill_last_at": self._hmm_backfill_last_at,
    "backfill_last_rows": self._hmm_backfill_last_rows,
    "backfill_last_message": self._hmm_backfill_last_message,
}
```


## 3. What Changes vs. What Stays

### Untouched (hard guarantees)
- `state_machine.py` — zero modifications
- Reducer contract (§6) — pure function, no HMM awareness
- Event transition rules (§7) — unchanged
- Invariants (§10) — unchanged
- `entry_pct` — sacred, never touched (§14.3 constraint respected)
- No market orders — all influence through limit-order sizing

### Modified (bot.py runtime only)
- `_compute_dynamic_idle_target()` — blended formula
- `_update_hmm()` / `_train_hmm()` — startup + periodic HMM lifecycle
- `_sync_ohlcv_candles()` — incremental OHLCV persistence
- `_maybe_backfill_ohlcv_on_startup()` + `backfill_ohlcv_history()` — one-time warmup
- `/backfill_ohlcv [target_candles] [max_pages]` — operator-triggered backfill
- `_hmm_data_readiness()` — readiness + freshness diagnostics
- `save_state()` / `load_state()` — HMM + backfill snapshot keys
- `status_payload()` / dashboard — `hmm_regime` + `hmm_data_pipeline` telemetry

### New files
- `hmm_regime_detector.py` — self-contained module


## 4. Tuning Strategy

Start conservative, increase influence gradually.

### Phase 1: Shadow Mode (observe only)
```python
HMM_BLEND_WITH_TREND = 1.0    # pure §15, HMM has zero influence
```
Run for 1-2 weeks. Log regime classifications alongside actual outcomes.
Verify that the HMM's state labels make intuitive sense by comparing
to price action in the dashboard.

### Phase 2: Gentle Blend
```python
HMM_BLEND_WITH_TREND = 0.7    # 30% HMM, 70% existing trend
```
The HMM can now nudge the idle target but can't overpower the trend
signal. Monitor for:
- Does inventory balance improve?
- Does realized P&L per cycle change?
- Are orphan rates affected?

### Phase 3: Equal Weight
```python
HMM_BLEND_WITH_TREND = 0.5
```
Now also enable grid_bias spacing adjustments. Monitor aggressively.

### Phase 4: HMM-Primary (if validated)
```python
HMM_BLEND_WITH_TREND = 0.3    # 70% HMM, 30% trend
```
Only move here if Phase 3 data shows clear improvement.

### Key Metrics to Track
- Win rate per regime (did BULLISH regime correlate with profitable B-side cycles?)
- Orphan rate per regime (does RANGING reduce orphans?)
- Inventory skew stability (does blending smooth out the rebalancer?)
- False regime transitions (how often does HMM flip states?)


## 5. Failure Modes and Safeguards

| Failure | Impact | Safeguard |
|---------|--------|-----------|
| hmmlearn not installed | No HMM, pure §15 | Graceful import fallback |
| Training data insufficient | Stays in RANGING | `HMM_MIN_TRAIN_SAMPLES` check |
| Inference exception | Returns last valid state | try/catch in `update()` |
| All states near-equal probability | Neutral bias | Confidence threshold |
| Overfitted model | Misleading signals | Daily retrain + shadow mode |
| Price gap > 2x slow halflife | Stale EMAs in features | Same §15.4 cold-start logic applies |
| Model disagrees with trend_score | Blend factor moderates | Never 100% either signal |

The critical safety property: **if the HMM breaks or returns garbage,
the bot degrades to its current behavior** (RANGING state, zero bias,
trend_score-only idle target). This is guaranteed by:
1. Default RegimeState is RANGING with 0.0 bias
2. blend_factor=1.0 is pure §15
3. All grid_bias spacing multipliers default to 1.0


## 6. Data Pipeline

```
Kraken OHLC endpoint (interval = HMM_OHLCV_INTERVAL_MIN, default 1m)
        │
        ▼
Supabase ohlcv_candles table
        │
        ├── _fetch_training_candles(HMM_TRAINING_CANDLES) → train() on startup + periodic retrain
        │
        └── _fetch_recent_candles(HMM_RECENT_CANDLES) → update() every REBALANCE_INTERVAL_SEC
```

Startup sequence includes:
1. `_sync_ohlcv_candles()` to ingest recent closed bars
2. `_maybe_backfill_ohlcv_on_startup()` (best-effort history warmup)
3. initial HMM train/update

Readiness uses a freshness gate:
- `freshness_limit_sec = max(180, interval_sec * 3)`
- At 1m cadence, stale after ~3 minutes.


## 7. Config Summary

New env vars / config keys:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HMM_ENABLED` | False | Master enable (start disabled) |
| `HMM_OHLCV_ENABLED` | True | Master switch for OHLCV persistence |
| `HMM_OHLCV_INTERVAL_MIN` | 1 | Candle interval in minutes |
| `HMM_OHLCV_SYNC_INTERVAL_SEC` | 60.0 | OHLCV pull cadence |
| `HMM_OHLCV_RETENTION_DAYS` | 14 | Supabase retention period |
| `HMM_OHLCV_BACKFILL_ON_STARTUP` | True | Attempt warmup backfill at startup |
| `HMM_OHLCV_BACKFILL_MAX_PAGES` | 40 | Max Kraken pages per backfill run |
| `HMM_TRAINING_CANDLES` | 2000 | Target training window size |
| `HMM_RECENT_CANDLES` | 100 | Inference fetch window |
| `HMM_READINESS_CACHE_SEC` | 300.0 | Readiness cache TTL |
| `HMM_N_STATES` | 3 | Number of hidden states |
| `HMM_N_ITER` | 100 | Baum-Welch iterations |
| `HMM_COVARIANCE_TYPE` | "diag" | Gaussian covariance structure |
| `HMM_INFERENCE_WINDOW` | 50 | Observations used per inference |
| `HMM_CONFIDENCE_THRESHOLD` | 0.15 | Min confidence for non-zero bias |
| `HMM_RETRAIN_INTERVAL_SEC` | 86400.0 | Retrain period (1 day) |
| `HMM_MIN_TRAIN_SAMPLES` | 500 | Minimum candles for training |
| `HMM_BIAS_GAIN` | 1.0 | Scales bias magnitude |
| `HMM_BLEND_WITH_TREND` | 0.5 | Blend ratio (0=HMM, 1=trend) |


## 8. Bauhaus Overlay Visualization

The regime state is a natural fit for the organism metaphor:

- **Regime → organism mood**: color palette shifts
  - BULLISH: warm tones, upward organic flow
  - BEARISH: cool tones, downward contraction
  - RANGING: neutral, breathing rhythm
- **Confidence → visual certainty**: high confidence = sharp, defined forms;
  low confidence = diffuse, uncertain edges
- **State transition probabilities → fluid morphing**: the probability
  distribution drives smooth blending between visual states, not hard cuts
- **bias_signal → directional energy**: the organism leans or flows in
  the direction of the bias, with intensity proportional to magnitude

This gives you an immediate intuitive read on what the model thinks
without looking at numbers.
