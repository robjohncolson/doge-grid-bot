# HMM Deep Training Window Spec v0.1

Version: v0.1
Date: 2026-02-15
Status: Design draft
Depends on: HMM Backfill Fix (implemented), Multi-Timeframe HMM (implemented)

---

## 1. Problem

The HMM trains on 720 candles (12 hours at 1m) because that's the Kraken
per-request API ceiling. But Supabase accumulates candles continuously via
the 60-second sync — after 3 days of uptime, there are ~4,300 1m candles
available; after 14 days (the retention window), ~20,000.

With only 720 candles, the 3-state Gaussian HMM sees perhaps 5-10 regime
transitions during training. This produces:

1. **Shaky transition matrix** — P(bear→bull) estimated from 1-2 observed
   transitions is unreliable.
2. **Thin emission tails** — the model never sees what a strong reversal
   looks like because 12 hours rarely contains one.
3. **Startup amnesia** — every retrain (daily) resets to the same 720-candle
   window, forgetting what it learned about market structure from earlier
   days.

The 15m HMM has a milder version of this problem: 720 × 15m = 7.5 days,
which is more reasonable but still thin for capturing weekly patterns.

**Goal:** Let the HMM train on 3,000–5,000 1m candles (~2–3.5 days) sourced
from Supabase, while gracefully degrading to 720 on cold starts. No new
external data sources, no schema changes.

---

## 2. Locked Decisions

1. **Supabase is the deep source.** No new data providers or aggregation
   pipelines. The existing `ohlcv_candles` table already has the data.
2. **No schema changes.** Same `(pair, interval_min, time)` composite key.
3. **720 remains the cold-start floor.** A fresh bot with empty Supabase
   trains on 720 candles from Kraken and works fine. Deep training is a
   bonus that kicks in once Supabase has accumulated enough data.
4. **Backward compatible.** `HMM_TRAINING_CANDLES=720` continues to work.
   Setting it higher (e.g. 4000) enables deep training when data exists.
5. **Zero new dependencies.** Pure Python.

---

## 3. Scope

### In

1. Raise default `HMM_TRAINING_CANDLES` from 720 to 4000 (1m).
2. Raise default `HMM_SECONDARY_TRAINING_CANDLES` from 720 to 1440 (15m).
3. Modify `_load_recent_ohlcv_rows()` to prefer Supabase depth over Kraken
   single-page fetch when sufficient rows exist.
4. Add warm-up awareness: dashboard shows training completeness as a
   progress indicator (e.g. "HMM: 1,200/4,000 candles (30%)").
5. Add optional exponential decay weighting for non-stationarity mitigation.
6. Adjust readiness cache to reflect deep vs. shallow training quality.

### Out

1. External OHLC providers (CoinGecko, Binance, etc.).
2. Changes to HMM model structure (still 3-state Gaussian, 4 features).
3. Changes to consensus blending or tier gating logic.
4. Backfill pagination (fetching multiple Kraken pages on startup). The
   existing single-page backfill + organic sync is sufficient.
5. Changes to Supabase retention (stays at 14 days).

---

## 4. Design

### 4.1 Training Data Source Priority

Current (`_load_recent_ohlcv_rows`):
```
1. Try Supabase → if rows >= target, done
2. Fall back to Kraken OHLC → max 720 rows
3. Merge by timestamp
```

Proposed (no structural change, just let the target be higher):
```
1. Try Supabase → if rows >= target, done  (target now 4000)
2. If rows < target but rows >= HMM_MIN_TRAIN_SAMPLES (500), train anyway
3. Fall back to Kraken OHLC → max 720 rows (cold start only)
4. Merge by timestamp
```

Key behavior: On day 1 the bot trains on 720 candles. On day 2, ~1440. On
day 3, ~2880. By day 3-4, it reaches the 4000 target and training quality
plateaus. No operator action needed.

### 4.2 New Config Defaults

| Parameter | Old Default | New Default | Notes |
|-----------|-------------|-------------|-------|
| `HMM_TRAINING_CANDLES` | 720 | 4000 | 1m: ~2.8 days |
| `HMM_SECONDARY_TRAINING_CANDLES` | 720 | 1440 | 15m: ~15 days |
| `HMM_MIN_TRAIN_SAMPLES` | 500 | 500 | Unchanged; gate for cold start |
| `HMM_DEEP_DECAY_ENABLED` | — | False | Exponential decay weighting |
| `HMM_DEEP_DECAY_HALFLIFE` | — | 1440 | Half-life in candles (~1 day at 1m) |

### 4.3 Exponential Decay Weighting (Optional)

When `HMM_DEEP_DECAY_ENABLED=True`, observations are weighted during
Baum-Welch training so that recent candles count more than older ones.

```
weight(i) = 2^(-(N - i) / halflife)
```

Where `i` is the candle index (0 = oldest, N-1 = newest) and `halflife`
is `HMM_DEEP_DECAY_HALFLIFE` (default 1440 = ~1 day at 1m).

Effect:
- Candle from 1 day ago: weight = 0.50
- Candle from 2 days ago: weight = 0.25
- Candle from 3 days ago: weight = 0.125

This mitigates non-stationarity: the model learns mostly from recent
structure but still uses older data to define the tails of the emission
distributions.

**Implementation note:** `hmmlearn`'s GaussianHMM does not natively support
sample weights in `fit()`. Two approaches:

**Option A — Resample (simpler):** Duplicate recent observations
proportional to their weight. A candle with weight 0.5 is included once;
a candle with weight 1.0 is included twice. Approximate but simple and
zero-dependency.

**Option B — Weighted sufficient statistics (precise):** Subclass
`GaussianHMM` and override `_accumulate_sufficient_statistics` to multiply
by per-sample weights. More accurate but tighter coupling to hmmlearn
internals.

**Recommendation:** Option A for v0.1. If regime detection quality is
visibly better with decay enabled, consider Option B in a follow-up.

### 4.4 Training Quality Tiers

Introduce a quality classification based on available training data:

| Tier | Candle Count | Label | Confidence Modifier |
|------|-------------|-------|---------------------|
| Shallow | 500–999 | `"shallow"` | ×0.70 |
| Baseline | 1000–2499 | `"baseline"` | ×0.85 |
| Deep | 2500–3999 | `"deep"` | ×0.95 |
| Full | 4000+ | `"full"` | ×1.00 |

The confidence modifier is applied as a multiplier on the HMM's raw
confidence output before it reaches the tier gating logic. This means:

- On cold start (720 candles, Shallow tier): effective confidence is 30%
  lower → harder to reach Tier 1/2 → bot stays symmetric → safe.
- After 3 days (Full tier): no penalty → full regime detection.

This naturally creates conservative startup behavior without adding
special-case code to the tier system.

### 4.5 Warm-Up Progress in Status Payload

Add to `_hmm_status_payload()`:

```python
"training_depth": {
    "current_candles": 1200,
    "target_candles": 4000,
    "quality_tier": "baseline",
    "confidence_modifier": 0.85,
    "pct_complete": 30.0,
    "estimated_full_at": "2026-02-16T14:00Z",  # null if already full
}
```

`estimated_full_at` = now + (target - current) × interval_seconds.

### 4.6 Dashboard Display

In the HMM status card, add a warm-up progress bar:

```
HMM Training: ████████░░░░░░░░ 2,400/4,000 (60%) — Deep
              est. full: ~8h
```

When full:
```
HMM Training: ████████████████ 4,000/4,000 — Full
```

Color coding:
- Shallow: dim/grey
- Baseline: yellow
- Deep: light green
- Full: green

### 4.7 Retrain Behavior Change

Current: retrain every `HMM_RETRAIN_INTERVAL_SEC` (86400s = daily).
Each retrain fetches `HMM_TRAINING_CANDLES` most-recent rows from Supabase.

Proposed change: **no change to retrain interval or trigger.** The only
difference is that the training target is now 4000 instead of 720, so
each daily retrain uses the latest ~2.8 days of 1m data.

The quality tier is recomputed on each retrain and may improve (shallow →
baseline → deep → full) as Supabase accumulates data.

---

## 5. Data Flow

```
Startup:
  _backfill_ohlcv()       → Kraken → up to 720 candles → Supabase
  _train_hmm()            → Supabase query (LIMIT 4000 ORDER BY time DESC)
                          → Got 720 → Shallow tier, conf modifier 0.70
                          → Train anyway (≥500 min samples)

Day 2:
  Organic sync            → +1440 candles in Supabase
  Daily retrain           → Supabase query → got 2160 → Baseline tier (×0.85)

Day 3:
  Organic sync            → +1440 more
  Daily retrain           → Supabase query → got 3600 → Deep tier (×0.95)

Day 4:
  Organic sync            → +1440 more
  Daily retrain           → Supabase query → got 4000+ → Full tier (×1.00)

Steady state:
  Daily retrain always gets 4000 most-recent candles.
  Supabase retention (14d) keeps ~20,000 available; we only use latest 4000.
```

---

## 6. Migration & Compatibility

### Env var behavior

- `HMM_TRAINING_CANDLES=720` → old behavior, Shallow tier on startup,
  eventually Baseline after 720 organic candles (never reaches Deep).
- `HMM_TRAINING_CANDLES=4000` (new default) → Deep/Full tier after 3-4
  days.
- `HMM_TRAINING_CANDLES=10000` → takes ~7 days to reach Full. Operator's
  choice. Deeper training, more non-stationarity risk.

### Rollback

Set `HMM_TRAINING_CANDLES=720` and `HMM_DEEP_DECAY_ENABLED=False`. Behavior
is identical to pre-change.

---

## 7. Files Modified

| File | Change | Est. Lines |
|------|--------|------------|
| `config.py` | New defaults + 2 new env vars (decay) | ~8 |
| `bot.py` | Quality tier computation in `_train_hmm()`, modifier in `_hmm_status_payload()`, progress bar data | ~40 |
| `dashboard.py` | Warm-up progress bar in HMM card | ~20 |

Total: ~68 lines changed/added.

---

## 8. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Non-stationarity degrades regime detection | Medium | Exponential decay (opt-in), quality tier caps confidence |
| Slow startup (training on 4000 candles) | Low | hmmlearn fits 4000×4 in <1s. No perceptible delay. |
| Supabase query latency for 4000 rows | Low | Already fetches up to 720; 4000 is 5.6× more but still <1s for indexed table |
| Operator confusion about "shallow" label | Low | Dashboard progress bar makes it obvious; clears itself after 3 days |

---

## 9. Testing

1. **Unit:** Mock `_load_recent_ohlcv_rows()` to return 500, 1000, 2500,
   4000 candles. Verify quality tier classification and confidence modifier.
2. **Unit:** Verify exponential decay weight vector for known inputs.
3. **Integration:** Start bot with empty Supabase. Verify Shallow tier on
   startup, progression through tiers over simulated time.
4. **Regression:** Confirm `HMM_TRAINING_CANDLES=720` produces identical
   behavior to pre-change.

---

## 10. Future Considerations (Out of Scope)

- **Multi-page backfill:** Fetch >720 candles on startup by paginating
  Kraken's cursor. Would accelerate warm-up from days to minutes. Deferred
  because organic sync already works and Kraken pagination is finicky.
- **Adaptive training window:** Auto-select window size based on observed
  regime transition density. More transitions → shorter window is fine;
  fewer → need longer window. Research-grade feature.
- **Feature expansion:** Add order flow features (fill rate, spread dynamics)
  to the HMM observation vector. Orthogonal to this spec.
