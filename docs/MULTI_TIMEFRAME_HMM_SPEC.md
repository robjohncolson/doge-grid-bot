# Multi-Timeframe HMM Consensus Spec

Version: v0.2
Date: 2026-02-15
Status: Revised design draft
Depends on: HMM regime detector, Directional Regime Spec (`docs/DIRECTIONAL_REGIME_SPEC.md`)

---

## 1. Problem

A single-timeframe HMM is either noisy (1m) or slow (15m). The 1m HMM
catches regime shifts early but whipsaws on intraday noise. The 15m HMM
is more stable but lags real transitions by 30-60 minutes. Neither alone
produces the right confidence signal for the tier system.

Running both in parallel and requiring consensus solves both problems:
the 15m provides directional conviction, the 1m provides timing precision.
When they agree, confidence is high. When they disagree, the bot stays
cautious.

---

## 2. Design Principles

1. **15m sets direction, 1m sets timing.** The 15m HMM is the commitment
   signal. The 1m HMM cannot override the 15m's direction — it can only
   modulate intensity within the 15m's regime, or dampen to neutral.
2. **Disagreement → caution.** When timeframes conflict, effective
   confidence drops. The bot doesn't pick a winner; it goes symmetric.
3. **Independent training.** Each HMM trains on its own candle stream.
   Feature extractor parameters are shared (same EMA/MACD/RSI periods)
   but the different candle granularity naturally produces different
   feature distributions.
4. **Existing pipeline, no new tables.** Both timeframes use the same
   `ohlcv_candles` table (keyed by `interval_min`). Both use the same
   `RegimeDetector` class. The new logic is consensus computation and
   a second instance in bot.py.

---

## 3. Architecture

```
Kraken OHLC 1m ──► ohlcv_candles (interval_min=1)
                        │
                        ▼
                   RegimeDetector (1m)
                        │
                        ├── regime_1m, confidence_1m, bias_1m
                        │
Kraken OHLC 15m ─► ohlcv_candles (interval_min=15)
                        │
                        ▼
                   RegimeDetector (15m)
                        │
                        ├── regime_15m, confidence_15m, bias_15m
                        │
                        ▼
                ┌───────────────────┐
                │  Consensus Engine │
                │                   │
                │  effective_regime │
                │  effective_conf   │
                │  effective_bias   │
                └───────┬───────────┘
                        │
                        ▼
              _update_regime_tier()
              (unchanged — consumes single
               regime/confidence/bias signal)
```

Key property: `_update_regime_tier()` does NOT change. It still consumes
a single regime + confidence + bias triple. The consensus engine produces
that triple from the two HMMs.

Important integration rule: all policy consumers must read that triple
through a single source selector helper (not directly from `_hmm_state`).
At minimum this includes `_update_regime_tier()`,
`_regime_entry_spacing_multipliers()`, dynamic idle target blending, and
regime-tagged telemetry/event rows.

---

## 4. Consensus Model: Gated Confirmation

### 4.1 Direction gate (15m controls)

The 15m HMM sets the **allowed direction**. The effective regime can
never be more directional than what the 15m says:

| 15m regime | Allowed effective regimes |
|------------|--------------------------|
| BULLISH    | BULLISH or RANGING       |
| BEARISH    | BEARISH or RANGING       |
| RANGING    | RANGING only             |

If 15m is RANGING, effective regime is always RANGING regardless of 1m.
If 15m is BULLISH but 1m is BEARISH, effective regime is RANGING
(disagreement → neutral).

### 4.2 Confidence computation

Effective confidence depends on agreement:

```
if regime_1m == regime_15m:
    # Full agreement: boost confidence
    effective_confidence = max(confidence_1m, confidence_15m)
    agreement = "full"

elif regime_15m == "RANGING":
    # 15m sees no direction: clamp to zero regardless of 1m
    effective_confidence = 0.0
    agreement = "15m_neutral"

elif regime_1m == "RANGING":
    # 15m has direction but 1m is cooling off: dampen 15m confidence
    effective_confidence = confidence_15m * CONSENSUS_DAMPEN_FACTOR
    agreement = "1m_cooling"

else:
    # Opposite directions (BULLISH vs BEARISH): zero confidence
    effective_confidence = 0.0
    agreement = "conflict"
```

### 4.3 Bias signal computation

```
if agreement == "full":
    # Blend: 1m for timing detail, 15m for conviction
    effective_bias = CONSENSUS_1M_WEIGHT * bias_1m
                   + CONSENSUS_15M_WEIGHT * bias_15m

elif agreement == "1m_cooling":
    # 15m direction holds but reduced intensity
    effective_bias = bias_15m * CONSENSUS_DAMPEN_FACTOR

else:
    # neutral or conflict
    effective_bias = 0.0
```

### 4.4 Effective regime

```
if effective_confidence < REGIME_TIER1_CONFIDENCE:
    effective_regime = "RANGING"
elif effective_bias > 0:
    effective_regime = "BULLISH"
elif effective_bias < 0:
    effective_regime = "BEARISH"
else:
    effective_regime = "RANGING"
```

---

## 5. Transition Scenarios

### 5.1 Market turns bearish

```
Time 0:  15m=BULLISH  1m=BULLISH  → full agreement, BULLISH, high conf
Time 1:  15m=BULLISH  1m=RANGING  → 1m cooling, BULLISH dampened
Time 2:  15m=BULLISH  1m=BEARISH  → conflict, RANGING, zero conf
Time 3:  15m=RANGING  1m=BEARISH  → 15m neutral, RANGING
Time 4:  15m=BEARISH  1m=BEARISH  → full agreement, BEARISH, high conf
```

The 1m flip at Time 1 acts as a natural **grace period** — reducing
commitment before the 15m catches up. By Time 2, full disagreement
forces symmetric. By Time 4, both confirm the new direction.

### 5.2 Brief dip in uptrend

```
Time 0:  15m=BULLISH  1m=BULLISH  → BULLISH, high conf
Time 1:  15m=BULLISH  1m=RANGING  → BULLISH dampened (grace)
Time 2:  15m=BULLISH  1m=BULLISH  → BULLISH restored
```

The 15m doesn't react to the brief dip. The 1m wobble only dampens
intensity temporarily. No tier whipsaw.

### 5.3 Choppy / ranging market

```
Time 0:  15m=RANGING  1m=BULLISH  → RANGING (15m gate)
Time 1:  15m=RANGING  1m=BEARISH  → RANGING (15m gate)
Time 2:  15m=RANGING  1m=RANGING  → RANGING
```

The 15m gate prevents the noisy 1m from triggering any directional
response during true ranging conditions.

---

## 6. Config

```python
# Multi-timeframe HMM
HMM_MULTI_TIMEFRAME_ENABLED: bool = False      # master switch
HMM_MULTI_TIMEFRAME_SOURCE: str = "primary"    # "primary" or "consensus"
HMM_SECONDARY_INTERVAL_MIN: int = 15           # second timeframe
HMM_SECONDARY_OHLCV_ENABLED: bool = False      # collect 15m candles even if consensus off

# Consensus weights (must sum to 1.0)
CONSENSUS_1M_WEIGHT: float = 0.3               # 1m contribution to blended bias
CONSENSUS_15M_WEIGHT: float = 0.7              # 15m contribution to blended bias

# Dampening when 1m is neutral but 15m has direction
CONSENSUS_DAMPEN_FACTOR: float = 0.5           # multiply 15m confidence/bias by this

# OHLCV sync interval for secondary timeframe
HMM_SECONDARY_SYNC_INTERVAL_SEC: float = 300.0 # 5 min (15m candles close less often)

# Training candle targets per timeframe
HMM_SECONDARY_TRAINING_CANDLES: int = 1000     # ~10.4 days of 15m candles
HMM_SECONDARY_RECENT_CANDLES: int = 50         # inference window for 15m
HMM_SECONDARY_MIN_TRAIN_SAMPLES: int = 200     # ~50 hours of 15m candles
```

When `HMM_MULTI_TIMEFRAME_ENABLED=False`, the bot uses the single HMM
(whatever `HMM_OHLCV_INTERVAL_MIN` is set to) exactly as today.

---

## 7. Implementation

### 7.1 Bot runtime state

```python
# Existing (primary HMM — currently 1m):
self._hmm_detector        # RegimeDetector instance
self._hmm_state           # dict with regime/confidence/bias

# New (secondary HMM — 15m):
self._hmm_detector_secondary        # RegimeDetector instance (or None)
self._hmm_state_secondary           # dict with regime/confidence/bias
self._hmm_secondary_last_sync_ts    # OHLCV sync tracking
self._hmm_secondary_since_cursor    # Kraken pagination cursor

# Consensus output (fed to _update_regime_tier):
self._hmm_consensus       # dict with effective regime/confidence/bias + agreement
```

### 7.2 Initialization

```python
def _init_hmm_runtime(self):
    # Existing primary init
    primary_cfg = self._hmm_runtime_config(
        min_train_samples=HMM_MIN_TRAIN_SAMPLES
    )
    self._hmm_detector = RegimeDetector(config=primary_cfg)

    if HMM_MULTI_TIMEFRAME_ENABLED and self._hmm_module:
        secondary_cfg = self._hmm_runtime_config(
            min_train_samples=HMM_SECONDARY_MIN_TRAIN_SAMPLES
        )
        self._hmm_detector_secondary = RegimeDetector(
            config=secondary_cfg
        )
        # Same model family/feature extractor, different interval + min samples
```

### 7.3 OHLCV sync

Add a parameterized interval sync path. Do not duplicate sync logic:

```python
def _sync_ohlcv_candles_for_interval(
    self,
    now: float,
    *,
    interval_min: int,
    sync_interval_sec: float,
    state_key: str,  # e.g. "primary" | "secondary"
):
    # Shared OHLCV fetch/upsert pipeline with per-interval cursor + last_sync state
```

Phase A collection requires secondary interval sync or backfill to work even
when `HMM_MULTI_TIMEFRAME_ENABLED=False`:

```python
if HMM_SECONDARY_OHLCV_ENABLED:
    _sync_ohlcv_candles_for_interval(
        now,
        interval_min=HMM_SECONDARY_INTERVAL_MIN,
        sync_interval_sec=HMM_SECONDARY_SYNC_INTERVAL_SEC,
        state_key="secondary",
    )
```

Backfill path must also accept explicit `interval_min`.

### 7.4 Training and inference

```python
def _update_hmm(self, now: float):
    # ... existing primary HMM update ...

    if not HMM_MULTI_TIMEFRAME_ENABLED:
        self._hmm_consensus = self._hmm_state  # passthrough
        return

    # Train/infer secondary
    self._update_hmm_secondary(now)

    # If secondary is unavailable/untrained, keep primary as effective signal
    if not self._hmm_state_secondary.get("trained", False):
        self._hmm_consensus = self._hmm_state
        return

    # Compute consensus
    self._hmm_consensus = self._compute_hmm_consensus()

def _update_hmm_secondary(self, now: float):
    # Mirror of _update_hmm() but using:
    # - self._hmm_detector_secondary
    # - HMM_SECONDARY_INTERVAL_MIN for candle fetches
    # - HMM_SECONDARY_TRAINING_CANDLES / HMM_SECONDARY_RECENT_CANDLES
```

### 7.5 Consensus computation

```python
def _compute_hmm_consensus(self) -> dict:
    primary = self._hmm_state
    secondary = self._hmm_state_secondary

    regime_1m = primary.get("regime", "RANGING")
    regime_15m = secondary.get("regime", "RANGING")
    conf_1m = primary.get("confidence", 0.0)
    conf_15m = secondary.get("confidence", 0.0)
    bias_1m = primary.get("bias_signal", 0.0)
    bias_15m = secondary.get("bias_signal", 0.0)
    dampen = clamp(CONSENSUS_DAMPEN_FACTOR, 0.0, 1.0)
    w1, w15 = _normalize_consensus_weights(
        CONSENSUS_1M_WEIGHT, CONSENSUS_15M_WEIGHT
    )

    # Apply gate + consensus logic from §4
    # ... (see §4.1 through §4.4)

    return {
        "regime": effective_regime,
        "confidence": effective_confidence,
        "bias_signal": effective_bias,
        "agreement": agreement,
        # Preserve individual signals for dashboard
        "primary_regime": regime_1m,
        "primary_confidence": conf_1m,
        "primary_bias": bias_1m,
        "secondary_regime": regime_15m,
        "secondary_confidence": conf_15m,
        "secondary_bias": bias_15m,
    }

def _normalize_consensus_weights(w1_raw: float, w15_raw: float) -> tuple[float, float]:
    w1 = max(0.0, float(w1_raw))
    w15 = max(0.0, float(w15_raw))
    total = w1 + w15
    if total <= 1e-9:
        # Defensive fallback to defaults when both are zero/invalid
        return 0.3, 0.7
    return w1 / total, w15 / total
```

### 7.6 Integration with _update_regime_tier()

Use a shared signal-source helper so all policy consumers stay consistent:

```python
def _policy_hmm_source(self) -> dict:
    # Single place that decides whether policy uses primary or consensus.
    if not HMM_MULTI_TIMEFRAME_ENABLED:
        return self._hmm_state
    mode = str(HMM_MULTI_TIMEFRAME_SOURCE).strip().lower()
    if mode == "consensus":
        return self._hmm_consensus or self._hmm_state
    return self._hmm_state
```

Required call sites (same source helper):
- `_update_regime_tier()`
- `_regime_entry_spacing_multipliers()`
- dynamic idle blending (`_compute_dynamic_idle_target()`)
- regime-tagged outcome telemetry (`_record_exit_outcome()`)
- dashboard "effective regime" strip

---

## 8. Dashboard

### 8.1 Status payload

```python
"hmm_consensus": {
    "multi_timeframe": bool,
    "source_mode": "primary" | "consensus",
    "agreement": "full" | "1m_cooling" | "15m_neutral" | "conflict",
    "effective_regime": str,
    "effective_confidence": float,
    "effective_bias": float,
    "primary": {
        "interval_min": 1,
        "regime": str,
        "confidence": float,
        "bias_signal": float,
        "trained": bool,
        "observation_count": int,
    },
    "secondary": {
        "interval_min": 15,
        "regime": str,
        "confidence": float,
        "bias_signal": float,
        "trained": bool,
        "observation_count": int,
    },
}
```

### 8.2 Visual

```
HMM Consensus    BULLISH (full agreement)
  1m  ▲ BULLISH  conf=0.45  bias=+0.38
  15m ▲ BULLISH  conf=0.62  bias=+0.51
  eff            conf=0.62  bias=+0.47   → Tier 1 biased
```

When disagreeing:
```
HMM Consensus    RANGING (1m cooling)
  1m  ● RANGING  conf=0.12  bias=+0.02
  15m ▲ BULLISH  conf=0.55  bias=+0.44
  eff            conf=0.28  bias=+0.22   → Tier 1 biased (dampened)
```

---

## 9. OHLCV Budget

The secondary HMM adds one more public API call per sync cycle:

| Call | Interval | Cost |
|------|----------|------|
| Primary OHLCV sync (1m) | every 60s | 1 public (free) |
| Secondary OHLCV sync (15m) | every 300s | 1 public (free) |
| Primary inference | every rebalancer tick | CPU only (no API) |
| Secondary inference | every rebalancer tick | CPU only (no API) |

Total additional cost: ~0.2 public calls/cycle. Negligible.

---

## 10. Training Readiness

| Timeframe | Min samples | Time to collect | Backfill available |
|-----------|-------------|-----------------|-------------------|
| 1m | `HMM_MIN_TRAIN_SAMPLES` (default 500) | ~8 hours | Yes (Kraken OHLC) |
| 15m | `HMM_SECONDARY_MIN_TRAIN_SAMPLES` (default 200) | ~50 hours | Yes (Kraken OHLC) |

On first enable, the 15m HMM won't have enough data to train. During
this window:
- `_hmm_detector_secondary._trained = False`
- Consensus falls back to primary-only (same as single-timeframe mode)
- As 15m candles accumulate (or backfill runs), secondary trains and
  consensus activates

This is automatic — no operator intervention needed.

Important: readiness checks must be interval-specific. Primary and secondary
readiness should be reported independently (different interval, targets,
freshness windows, and min-train thresholds).

---

## 11. Rollout

### Phase A: Parallel collection (no consensus)

- `HMM_MULTI_TIMEFRAME_ENABLED=False`
- `HMM_MULTI_TIMEFRAME_SOURCE="primary"`
- `HMM_OHLCV_INTERVAL_MIN=1` (primary stays 1m)
- `HMM_SECONDARY_OHLCV_ENABLED=True` to collect 15m candles in parallel
- Or: run one-time backfill with `interval_min=HMM_SECONDARY_INTERVAL_MIN`
- Verify 15m candles appear in ohlcv_candles table
- Duration: until 200+ 15m candles collected

### Phase B: Shadow consensus

- `HMM_MULTI_TIMEFRAME_ENABLED=True`
- `HMM_MULTI_TIMEFRAME_SOURCE="primary"` (consensus computed but non-actuating)
- Both HMMs run, consensus computed and logged
- `REGIME_DIRECTIONAL_ENABLED` can remain as-is; policy still reads primary
  because source mode is pinned to `"primary"`
- Monitor: does consensus agree with primary? Does it reduce false
  regime transitions?
- Duration: 1 week

### Phase C: Consensus drives tier system

- Set `HMM_MULTI_TIMEFRAME_SOURCE="consensus"`
- Spacing bias now driven by multi-timeframe consensus
- Monitor: smoother tier transitions, fewer whipsaws, and no source drift
  between tiering/rebalancer/telemetry

---

## 12. Invariants

1. When `HMM_MULTI_TIMEFRAME_ENABLED=False`, behavior is identical to
   single-timeframe mode. No consensus computation occurs.
2. Secondary HMM untrained → consensus falls back to primary-only.
3. Primary HMM untrained → consensus falls back to RANGING/0.0/0.0
   (existing behavior).
4. Both HMMs untrained → RANGING, tier 0, symmetric.
5. 15m regime is RANGING → effective regime is always RANGING regardless
   of 1m signal.
6. 1m and 15m in opposite directions → effective confidence is 0.0.
7. Consensus weights are normalized at runtime after clamping negatives to 0.
   If both weights are non-positive, fallback defaults `(0.3, 0.7)` are used.
8. Secondary OHLCV sync does not interfere with primary sync (separate
   cursors, separate intervals).
9. Policy consumers use a shared source selector helper; when source mode is
   `"consensus"`, tiering/rebalancer/telemetry must not read raw primary
   `_hmm_state` directly.

---

## 13. Future Extensions

- **Tertiary timeframe** (1h or 4h): same pattern, add another gate
  layer. Diminishing returns but possible.
- **Adaptive weights**: adjust CONSENSUS_1M_WEIGHT / 15M_WEIGHT based
  on historical accuracy per timeframe.
- **Per-timeframe feature tuning**: different EMA/RSI periods optimized
  for each candle interval.
- **Regime transition prediction**: use 1m regime shift as a leading
  indicator for 15m transition probability.

---

## 14. Files Modified

| File | Changes |
|------|---------|
| `config.py` | New `HMM_MULTI_TIMEFRAME_*` and `CONSENSUS_*` knobs |
| `bot.py` | Second HMM instance, parallel OHLCV sync, `_compute_hmm_consensus()`, consensus payload |
| `hmm_regime_detector.py` | No changes (RegimeDetector already reusable) |
| `dashboard.py` | Consensus display panel |
| `HMM_INTEGRATION.md` | Document multi-timeframe architecture |
| `docs/DIRECTIONAL_REGIME_SPEC.md` | Reference consensus as signal source for tier system |
