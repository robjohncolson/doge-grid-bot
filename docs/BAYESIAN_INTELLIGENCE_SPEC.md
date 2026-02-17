# Bayesian Intelligence Stack Spec

**Version**: v0.1
**Date**: 2026-02-17
**Status**: Draft — awaiting review
**Scope**: Multi-phase upgrade — continuous belief state, per-trade Bayesian management, change-point detection, survival modeling, adaptive action knobs
**Depends on**: HMM Deep Training (implemented), Multi-Timeframe HMM (implemented), Throughput Sizer (implemented), Strategic Capital Deployment (implemented), AI Regime Advisor (implemented)
**Files affected**: `bot.py`, `config.py`, `hmm_regime_detector.py`, `grid_strategy.py`, `dashboard.py`, `throughput_sizer.py`, `bayesian_engine.py` (NEW), `survival_model.py` (NEW)

---

## 1. Problem

Five structural limitations prevent the current regime system from reaching its potential:

1. **Discrete regime labels discard information.** The HMM produces `[0.08, 0.21, 0.71]` but the tier system only sees "BULLISH." The distinction between barely-bullish (0.34, 0.33, 0.33) and screaming-bullish (0.02, 0.03, 0.95) is lost at the argmax boundary. With 3 timeframes × 3 states, the label space is 27 discrete cells — most of which are data-starved.

2. **Regime beliefs are stale between evaluations.** HMM inference runs every `REGIME_EVAL_INTERVAL_SEC` (300s). Between evaluations, the posterior is frozen. The market reprices continuously; our beliefs don't. During a fast pump or dump, 5 minutes of stale beliefs means delayed tier transitions and misaligned exit management.

3. **Trade management is timer-based, not evidence-based.** Once an exit is placed, new information (regime shifts, price drift, fill-rate changes) doesn't flow back into that specific exit's management. Repricing triggers at `median_fill_time * 1.5` regardless of whether the market regime supports or contradicts the trade thesis. A trade that entered in BULLISH and is now in BEARISH gets the same timeout as one where conditions still agree.

4. **No structural break detection.** Regime transitions are detected retroactively when the HMM posterior rotates. The system has no forward-looking "something just changed" signal. The accumulation engine uses a crude candle-count confirmation (`ACCUM_CONFIRMATION_CANDLES`) rather than a probabilistic transition measure.

5. **Action space is bucketed, not continuous.** Tier 0/1/2 maps to fixed action templates. The real action space (grid spacing, exit distance, repricing cadence, sizing aggression) is continuous and should be driven by continuous belief signals rather than 3 discrete modes.

---

## 2. Locked Decisions

1. **Posteriors are the primitive, not labels.** All downstream consumers work with the full probability vector, not the argmax. Labels become display-only convenience.
2. **Pure Python + numpy.** No new dependencies beyond what's already installed (`numpy`, `hmmlearn`). All new models implementable in numpy.
3. **Zero changes to the state machine reducer.** `state_machine.py` is untouched. All intelligence flows through `EngineConfig` parameters and runtime decisions in `bot.py`.
4. **Every layer fails gracefully to current behavior.** Survival model fails → fall back to fixed timers. BOCPD fails → fall back to 300s regime eval. Belief tracker fails → current tier system continues.
5. **Feature-flagged phases.** Each layer has an independent enable toggle. Phases deploy incrementally.
6. **9D posterior vector is the continuous state representation.** The three HMM timeframes produce 3 × 3 = 9 probability values. This 9D vector replaces the 27-cell discrete grid. No cell lookup tables.
7. **Per-trade beliefs replace fixed timer thresholds.** `S1_ORPHAN_AFTER_SEC`, `reprice_after`, `orphan_after` become fallback defaults; the survival model provides evidence-based triggers.
8. **Start logging before building.** Phase 0 (instrumentation) deploys first, collecting training data for all subsequent phases.

---

## 3. Scope

### In

1. **Phase 0: Instrumentation** — Log 9D posteriors, entropy, p_switch with every trade and in status payload. Zero behavior change.
2. **Phase 1: Continuous Belief Signals** — Entropy, p_switch, BOCPD change-point probability. Replace confidence with entropy. Add transition risk signal.
3. **Phase 2: Feature Enrichment** — Private microstructure features (fill imbalance, spread realization, fill-time derivative) added to HMM observation vector.
4. **Phase 3: Survival Model** — Kaplan-Meier and Cox hazard model for per-trade fill probability, trained from historical fills + HMM-synthetic data.
5. **Phase 4: Per-Trade Belief Tracker** — Bayesian belief state per open exit, updated each tick, driving adaptive exit management (tighten/widen/hold).
6. **Phase 5: Action Knobs** — Replace tier 0/1/2 with continuous control parameters derived from belief signals.

### Out

1. Particle filter (deferred — streaming inference every 60s achieves 80% of the value with 10% of the complexity; revisit if staleness proves problematic).
2. HDP-HMM / auto state discovery (deferred — manual 6-state expansion as an intermediate step if needed).
3. Neural network function approximators (deferred — logistic regression first; graduate to NN only if interaction effects proven in residual analysis).
4. Online EM parameter adaptation (deferred to Phase 6 — requires stability analysis and shadow testing).
5. Contextual bandits for action selection (deferred to Phase 7 — requires calibrated outcome tracking).
6. Changes to the state machine reducer (`state_machine.py`).
7. Changes to Kraken API interactions or order types.
8. Student-t HMM emissions (noted as promising for crypto heavy tails; deferred pending performance comparison).

---

## 4. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     LAYER 0: FEATURES                           │
│                                                                 │
│  Existing:  MACD slope, EMA spread, RSI zone, volume ratio      │
│  New:       fill imbalance, spread realization, fill-time deriv  │
│  Private:   only observable from our own execution data          │
│             → feeds ALL downstream models                        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                  LAYER 1: BELIEF STATE                           │
│                                                                 │
│  9D posterior:  [p_bear, p_range, p_bull] × [1m, 15m, 1h]       │
│  Entropy:       H(π) per timeframe (3 scalars)                   │
│  p_switch:      transition risk per timeframe (3 scalars)        │
│  BOCPD:         structural break probability (1 scalar)          │
│                                                                 │
│  Output: ~16D continuous "market belief vector"                   │
│  Updated: every regime eval tick (target: every 60s)             │
└───────────────────────────┬─────────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          │                 │                 │
┌─────────▼──────┐  ┌──────▼───────┐  ┌──────▼──────────────────┐
│  LAYER 2:      │  │  LAYER 3:    │  │  LAYER 3b:              │
│  ACTION KNOBS  │  │  SURVIVAL    │  │  PER-TRADE BELIEFS      │
│                │  │  MODEL       │  │                          │
│  direction     │  │              │  │  For each open exit:     │
│  confidence    │  │  P(fill|T,x) │  │  P(fill), E[value],     │
│  boundary_risk │  │  trained on  │  │  recommended_action      │
│  → spacing     │  │  historical  │  │  → tighten/widen/hold   │
│  → aggression  │  │  + synthetic │  │                          │
│  → cadence     │  │  fills       │  │  Replaces fixed timers   │
└────────────────┘  └──────────────┘  └──────────────────────────┘
          │                 │                 │
          └─────────────────┼─────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│              EXISTING: STATE MACHINE + EXECUTION                 │
│                                                                 │
│  state_machine.py:  UNCHANGED — pure reducer                     │
│  bot.py:            receives smarter EngineConfig parameters     │
│                     + per-trade directives from belief tracker    │
│                                                                 │
│  Fallback:          if any layer fails, current tier 0/1/2       │
│                     logic + fixed timers continue unchanged       │
└─────────────────────────────────────────────────────────────────┘
```

---

## 5. Phase 0: Instrumentation (Zero Behavior Change)

**Goal**: Start collecting the training data that all subsequent phases depend on. Every day without this data is training data lost forever.

### 5.1 New Fields on CompletedCycle

Add to the `CompletedCycle` dataclass (or its dict representation) in `grid_strategy.py`:

| Field | Type | Description |
|-------|------|-------------|
| `posterior_1m` | `list[float]` (3) | `[p_bear, p_range, p_bull]` from primary HMM at entry fill time |
| `posterior_15m` | `list[float]` (3) | From secondary HMM at entry fill time |
| `posterior_1h` | `list[float]` (3) | From tertiary HMM at entry fill time |
| `entropy_at_entry` | `float` | Consensus entropy at entry fill time |
| `p_switch_at_entry` | `float` | Transition risk at entry fill time |
| `confidence_at_entry` | `float` | Raw HMM confidence (existing, but now explicitly logged) |

These are **snapshots** taken at entry fill time and stored with the cycle for later training.

### 5.2 New Fields on exit_outcomes (Supabase)

Add columns to `exit_outcomes` table:

| Column | Type | Default |
|--------|------|---------|
| `posterior_1m` | `jsonb` | `null` |
| `posterior_15m` | `jsonb` | `null` |
| `posterior_1h` | `jsonb` | `null` |
| `entropy_at_entry` | `float` | `null` |
| `p_switch_at_entry` | `float` | `null` |
| `posterior_at_exit_1m` | `jsonb` | `null` |
| `posterior_at_exit_15m` | `jsonb` | `null` |
| `posterior_at_exit_1h` | `jsonb` | `null` |
| `entropy_at_exit` | `float` | `null` |
| `p_switch_at_exit` | `float` | `null` |

Entry AND exit posteriors are logged. The delta between them is the key training signal: "did the regime change during this trade?"

### 5.3 Entropy Computation

```
H(π) = -Σ π(i) * ln(π(i))     for π(i) > 0

H_max = ln(3) ≈ 1.099          for 3 states
normalized_entropy = H(π) / H_max    ∈ [0, 1]
```

- `normalized_entropy = 0.0`: one state has 100% probability (maximum certainty)
- `normalized_entropy = 1.0`: uniform distribution (maximum uncertainty)
- `confidence_score = 1.0 - normalized_entropy`: inverted for intuitive "higher = more confident"

This replaces the current `max(prob) - second_max` confidence with a measure that uses all three probabilities. The existing confidence metric is retained for backward compatibility but entropy becomes the primary uncertainty signal.

### 5.4 Transition Risk (p_switch)

```
p_switch = 1 - Σ π(i) * A[i,i]
```

Where `π` is the current regime posterior and `A` is the trained transition matrix. This computes the probability of leaving the current regime mixture in the next time step.

- `p_switch ≈ 0.02–0.05`: regime is stable (typical for sticky regimes)
- `p_switch > 0.10`: elevated transition risk
- `p_switch > 0.20`: regime boundary — high probability of change

Per-timeframe: `p_switch_1m`, `p_switch_15m`, `p_switch_1h`. The 1h p_switch is the most strategically meaningful.

**Consensus p_switch**: Weighted combination using existing consensus weights:
```
p_switch_consensus = w_1m * p_switch_1m + w_15m * p_switch_15m + w_1h * p_switch_1h
```

### 5.5 Status Payload Additions

Add to `hmm_regime` block in `/api/status`:

```json
{
  "hmm_regime": {
    ...existing fields...,
    "belief_state": {
      "posterior_1m": [0.08, 0.21, 0.71],
      "posterior_15m": [0.28, 0.44, 0.28],
      "posterior_1h": [0.12, 0.16, 0.72],
      "entropy_1m": 0.34,
      "entropy_15m": 0.89,
      "entropy_1h": 0.41,
      "entropy_consensus": 0.52,
      "confidence_score": 0.48,
      "p_switch_1m": 0.04,
      "p_switch_15m": 0.08,
      "p_switch_1h": 0.03,
      "p_switch_consensus": 0.05,
      "direction_score": 0.63,
      "boundary_risk": "low"
    }
  }
}
```

`direction_score = P(bull) - P(bear)` from consensus posterior. Range [-1, +1].

`boundary_risk` derived display label: `"low"` (p_switch < 0.08), `"medium"` (0.08–0.15), `"high"` (> 0.15).

### 5.6 Dashboard: Belief State Card

New card in the HMM section showing the continuous belief state:

```
Belief State
  Direction:  +0.63 ████████████░░░░░░░░ BULL
  Confidence: 0.48  ████████░░░░░░░░░░░░
  Boundary:   0.05  █░░░░░░░░░░░░░░░░░░░ low
  Entropy:    1m: 0.34  15m: 0.89  1h: 0.41
  p_switch:   1m: 0.04  15m: 0.08  1h: 0.03
```

Color coding:
- Direction bar: red (< -0.3), yellow (-0.3 to +0.3), green (> +0.3)
- Confidence: dim when low, bright when high
- Boundary: green (low), yellow (medium), red (high)

### 5.7 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `BELIEF_STATE_LOGGING_ENABLED` | `True` | bool | Log posteriors with every completed cycle |
| `BELIEF_STATE_IN_STATUS` | `True` | bool | Include belief state in `/api/status` |

### 5.8 Implementation

- `hmm_regime_detector.py`: Add `compute_entropy(posterior)` and `compute_p_switch(posterior, transmat)` static methods.
- `bot.py`: Call these in `_update_regime_tier()`, cache results. Stamp onto `CompletedCycle` at fill time.
- `dashboard.py`: New belief state card HTML + JS renderer.
- `grid_strategy.py`: Add posterior/entropy/p_switch fields to cycle completion path.
- Estimated lines: ~60.

---

## 6. Phase 1: Continuous Belief Signals

**Goal**: Replace the discrete confidence threshold with continuous signals. Add BOCPD for structural break detection. Increase regime eval frequency.

### 6.1 Entropy Replaces Confidence in Tier Gating

Current tier gating (from `_update_regime_tier()`):

```
if confidence >= REGIME_TIER2_CONFIDENCE and abs(bias) >= REGIME_TIER2_BIAS_FLOOR:
    tier = 2
elif confidence >= REGIME_TIER1_CONFIDENCE and abs(bias) >= REGIME_TIER1_BIAS_FLOOR:
    tier = 1
else:
    tier = 0
```

New gating uses entropy-derived confidence score:

```
confidence_score = 1.0 - normalized_entropy
# Apply training quality modifier (existing)
effective_confidence = confidence_score * quality_modifier

# Existing threshold logic, just with better input
if effective_confidence >= REGIME_TIER2_CONFIDENCE and ...:
    tier = 2
...
```

The behavior is backward-compatible — entropy-based confidence correlates with the existing `max - second_max` confidence. But it's smoother and more informative at the boundaries.

### 6.2 BOCPD (Bayesian Online Change-Point Detection)

New module: `bocpd.py` (~120 lines, pure numpy).

**Core algorithm**: Maintains a distribution over "run lengths" — how many observations since the last structural break.

```
On each new observation x_t:
    1. Compute predictive probability P(x_t | run_length = r) for each r
    2. Growth probabilities:   P(r_t = r+1) = P(r_{t-1} = r) * P(x_t | r) * (1 - hazard)
    3. Change-point probability: P(r_t = 0) = Σ_r P(r_{t-1} = r) * P(x_t | r) * hazard
    4. Normalize
    5. change_prob = P(r_t = 0)  ← "something just broke"
```

**Hazard function**: Constant hazard `h = 1/expected_run_length`. Default `BOCPD_EXPECTED_RUN_LENGTH = 200` (candles). For 1m candles, this expects a structural break roughly every 3.3 hours.

**Observation model**: Gaussian with online mean/variance estimation (conjugate prior — Normal-Inverse-Gamma). No fitting step needed.

**Input**: The same feature vector fed to the HMM (4D or enriched). BOCPD runs on the primary (1m) feature stream.

**Output**:
- `change_prob`: probability that a structural break occurred at this observation. Range [0, 1].
- `run_length_map`: posterior mass at each run length (for debugging/analysis).
- `max_run_length_prob`: the most likely run length and its probability.

**Integration points**:

| Consumer | How it uses BOCPD |
|----------|-------------------|
| Regime eval | When `change_prob > BOCPD_ALERT_THRESHOLD` (default 0.30), trigger immediate HMM re-inference (don't wait for the 300s timer) |
| Accumulation engine | Replace `ACCUM_CONFIRMATION_CANDLES` with `change_prob` threshold. Arm when `change_prob > 0.50` AND 1h regime has shifted |
| Per-trade beliefs (Phase 4) | `change_prob` is an input feature. High change_prob + regime disagreement with entry → accelerate exit tightening |
| AI advisor context | Add `change_prob` and `run_length` to the LLM prompt. "BOCPD detected structural break 12 candles ago (p=0.72)" |
| Dashboard | Spike indicator on the belief state card |

### 6.3 Increased Eval Frequency

When BOCPD is active, regime evaluation frequency adapts:

```
if change_prob > BOCPD_URGENT_THRESHOLD:      # default 0.50
    eval_interval = REGIME_EVAL_INTERVAL_FAST   # default 60s
elif change_prob > BOCPD_ALERT_THRESHOLD:      # default 0.30
    eval_interval = REGIME_EVAL_INTERVAL_SEC / 2
else:
    eval_interval = REGIME_EVAL_INTERVAL_SEC    # default 300s
```

This directly addresses the staleness problem: beliefs update faster when the market is changing.

### 6.4 BOCPD Data Model

```python
@dataclass
class BOCPDState:
    """Serializable state for persistence."""
    change_prob: float = 0.0
    run_length_mode: int = 0        # most likely run length
    run_length_mode_prob: float = 1.0
    last_update_ts: float = 0.0
    observation_count: int = 0
    alert_active: bool = False      # change_prob > alert threshold
    alert_triggered_at: float = 0.0
```

### 6.5 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `BOCPD_ENABLED` | `False` | bool | Master toggle |
| `BOCPD_EXPECTED_RUN_LENGTH` | `200` | int | Expected observations between change-points |
| `BOCPD_ALERT_THRESHOLD` | `0.30` | float | change_prob above which regime eval accelerates |
| `BOCPD_URGENT_THRESHOLD` | `0.50` | float | change_prob above which eval uses fast interval |
| `BOCPD_MAX_RUN_LENGTH` | `500` | int | Truncation limit for run-length distribution |
| `REGIME_EVAL_INTERVAL_FAST` | `60.0` | float | Accelerated eval interval during transitions |

---

## 7. Phase 2: Feature Enrichment

**Goal**: Add private microstructure signals that are orthogonal to the existing price-derived features. These signals are observable only from our own execution data — they are our informational edge.

### 7.1 New Features

| Feature | Formula | Signal | Update Cadence |
|---------|---------|--------|----------------|
| `fill_imbalance` | `(fills_A_5m - fills_B_5m) / max(1, fills_A_5m + fills_B_5m)` | Micro selling/buying pressure. Range [-1, +1]. Negative = more A-side (sell) fills = buying pressure on DOGE. | Every main loop tick |
| `spread_realization` | `realized_exit_distance / configured_profit_pct` | Effective spread vs. target. > 1.0 = exits filling beyond target (favorable). < 1.0 = exits filling at tighter reprice (unfavorable). | On each exit fill, rolling 20-fill average |
| `fill_time_derivative` | `(median_fill_5m - median_fill_30m) / median_fill_30m` | Is fill speed accelerating (negative = faster) or decelerating (positive = slower)? | Every 5 minutes |
| `congestion_ratio` | `exits_older_than_p75 / total_open_exits` | What fraction of exits are "old" relative to the typical fill time? 0 = healthy, > 0.5 = congested. | Every main loop tick |

### 7.2 Feature Vector Extension

Current HMM observation: 4D `[macd_hist_slope, ema_spread_pct, rsi_zone, volume_ratio]`

Extended observation: 8D `[macd_hist_slope, ema_spread_pct, rsi_zone, volume_ratio, fill_imbalance, spread_realization, fill_time_derivative, congestion_ratio]`

The extended features feed into:
- HMM training (richer emission model — the HMM discovers states where, e.g., high volume_ratio + negative fill_imbalance = genuine buy pressure)
- BOCPD observation model (detects breaks in fill dynamics, not just price dynamics)
- Survival model (Phase 3) and per-trade beliefs (Phase 4) as direct input features

### 7.3 Gating

`ENRICHED_FEATURES_ENABLED` (default `False`). When False, the observation vector remains 4D. When True, extends to 8D. The HMM must be retrained after enabling (first retrain after flag change uses the extended vector).

**Cold-start**: Private features require runtime history (fills, exits). On startup before sufficient fills accumulate:
- `fill_imbalance` = 0.0 (no signal)
- `spread_realization` = 1.0 (assume target)
- `fill_time_derivative` = 0.0 (no signal)
- `congestion_ratio` = 0.0 (assume healthy)

### 7.4 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `ENRICHED_FEATURES_ENABLED` | `False` | bool | Extend HMM observation vector with private features |
| `FILL_IMBALANCE_WINDOW_SEC` | `300` | int | Rolling window for fill imbalance (5 min) |
| `FILL_TIME_DERIVATIVE_SHORT_SEC` | `300` | int | Short window for fill-time derivative |
| `FILL_TIME_DERIVATIVE_LONG_SEC` | `1800` | int | Long window for fill-time derivative |

---

## 8. Phase 3: Survival Model

**Goal**: Build `P(fill within T | features)` for each open exit, trained from historical fills. This is the engine that powers per-trade beliefs.

### 8.1 New Module: `survival_model.py`

~200 lines, pure numpy.

### 8.2 Training Data

Each completed cycle provides one observation:

```python
@dataclass
class FillObservation:
    duration_sec: float         # exit_time - entry_time
    censored: bool              # True for open exits (haven't filled yet)
    regime_at_entry: int        # 0/1/2
    regime_at_exit: int | None  # 0/1/2 or None (if censored)
    side: str                   # "A" or "B"
    distance_pct: float         # exit price distance from market at placement
    posterior_1m: list[float]   # 3-vector at entry time
    posterior_15m: list[float]  # 3-vector at entry time
    posterior_1h: list[float]   # 3-vector at entry time
    entropy_at_entry: float
    p_switch_at_entry: float
    fill_imbalance: float       # at entry time (if enriched features enabled)
    congestion_ratio: float     # at entry time
```

**Censored observations**: Open exits are right-censored — their final fill time is unknown, but current age is a lower bound. Ignoring them causes survivorship bias (only fast fills appear in completed data). The survival model handles censoring natively.

### 8.3 Model Tiers (Progressive Complexity)

#### Tier 1: Stratified Kaplan-Meier (Baseline)

Partition observations into strata by `(regime_at_entry, side)` — 6 buckets (same as throughput sizer). For each stratum, compute the Kaplan-Meier survival curve:

```
S(t) = Π_{t_i ≤ t} (1 - d_i / n_i)
```

Where `d_i` = fills at time `t_i`, `n_i` = at-risk set at time `t_i`.

Lookup: `P(fill by time T) = 1 - S(T)` for the matching stratum.

**Advantages**: Nonparametric, handles censoring natively, well-understood.
**Limitations**: Only conditions on stratum (6 bins). Doesn't use continuous features.

#### Tier 2: Cox Proportional Hazards (Production Target)

```
h(t | x) = h_0(t) * exp(β · x)
```

Where `h_0(t)` is a baseline hazard (from Kaplan-Meier), and `x` is the feature vector:

- 9 posterior probabilities (3 × 3 timeframes)
- side (binary)
- distance_pct (continuous)
- entropy_at_entry (continuous)
- p_switch_at_entry (continuous)
- fill_imbalance (if enriched features enabled)
- congestion_ratio

Total: 13-15 features depending on enrichment.

The coefficients `β` are learned via partial likelihood maximization — standard in survival analysis, ~50 lines of numpy (Newton-Raphson on the partial likelihood).

**What the coefficients tell you**: `β_i > 0` means feature `i` increases the hazard (fills happen faster). For example, if `β_{p_bull_1h}` is large and positive for B-side trades, it means "when the 1h HMM says bullish, B-side exits fill faster" — which is exactly what you'd expect.

**Advantages**: Uses all continuous features. No binning. Naturally handles the 9D posterior without a grid. Interpretable coefficients.

#### Tier 3: Bayesian Logistic Regression (Optional)

For the specific question "will this exit fill within the next T seconds?" (binary outcome), a logistic regression with Bayesian parameter estimation:

```
P(fill within T | x) = sigmoid(w · x + b)
```

With Gaussian prior on `w` and online Laplace approximation updates as trades complete.

**Why this exists alongside Cox**: Cox gives the full survival curve. Logistic regression gives a direct probability for a specific horizon with uncertainty bounds (from the Bayesian posterior on `w`). The uncertainty bounds tell the per-trade belief tracker "I'm confident about this prediction" vs. "I have no idea."

### 8.4 Synthetic Training Data (HMM-Generated)

Some regime combinations are rare (e.g., 1m BEAR + 15m BULL + 1h BEAR). The survival model needs training data in these corners.

**HMM sampling**: `hmmlearn`'s `GaussianHMM.sample()` generates synthetic observation sequences with known hidden states. From these, simulate grid trades:

1. Generate 10,000-candle synthetic price path from each HMM (1m, 15m, 1h).
2. At each candle, compute the "true" regime (known from generation).
3. Simulate grid entries at configured `entry_pct` from synthetic price.
4. Track synthetic fill times (when price crosses exit level).
5. Record: `(duration, regime_at_entry, side, distance, posterior)`.

This produces thousands of synthetic fill observations covering all regime corners, including rare ones. The survival model trains on real data + synthetic data (with synthetic weighted at `SURVIVAL_SYNTHETIC_WEIGHT`, default 0.3).

### 8.5 Update Cadence

The survival model retrains on the same cadence as the HMM: daily (`HMM_RETRAIN_INTERVAL_SEC`). Between retrains, `P(fill | x)` is computed from the fitted model for each open exit on each regime eval tick.

### 8.6 Data Model

```python
@dataclass
class SurvivalConfig:
    min_observations: int = 50       # minimum fills before model activates
    min_per_stratum: int = 10        # minimum per KM stratum
    synthetic_weight: float = 0.3    # weight of HMM-synthetic data
    horizons: list[int] = field(default_factory=lambda: [1800, 3600, 14400])
    # ^ predict P(fill within 30m, 1h, 4h)

@dataclass
class SurvivalPrediction:
    p_fill_30m: float        # P(fill within 30 minutes)
    p_fill_1h: float         # P(fill within 1 hour)
    p_fill_4h: float         # P(fill within 4 hours)
    median_remaining: float  # estimated median time to fill (seconds)
    hazard_ratio: float      # relative to baseline (>1 = faster, <1 = slower)
    model_tier: str          # "kaplan_meier" | "cox" | "logistic"
    confidence: float        # model confidence in this prediction
```

### 8.7 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `SURVIVAL_MODEL_ENABLED` | `False` | bool | Master toggle |
| `SURVIVAL_MODEL_TIER` | `"kaplan_meier"` | str | `"kaplan_meier"` or `"cox"` |
| `SURVIVAL_MIN_OBSERVATIONS` | `50` | int | Minimum completed fills for model activation |
| `SURVIVAL_MIN_PER_STRATUM` | `10` | int | Minimum fills per KM stratum |
| `SURVIVAL_SYNTHETIC_ENABLED` | `False` | bool | Generate synthetic fills from HMM for training |
| `SURVIVAL_SYNTHETIC_WEIGHT` | `0.3` | float | Weight of synthetic vs. real observations |
| `SURVIVAL_SYNTHETIC_PATHS` | `5000` | int | Number of synthetic paths to generate |
| `SURVIVAL_HORIZONS` | `"1800,3600,14400"` | str | Comma-separated prediction horizons (seconds) |
| `SURVIVAL_LOG_PREDICTIONS` | `True` | bool | Log per-trade predictions at update cadence |

---

## 9. Phase 4: Per-Trade Belief Tracker

**Goal**: Give each open position its own evolving belief state. Update every tick. Drive adaptive exit management based on evidence, not timers.

### 9.1 Per-Trade Belief State

For each open exit in S1 or S2, the system maintains:

```python
@dataclass
class TradeBeliefState:
    # Snapshot at entry
    entry_regime_posterior: list[float]  # 9D at entry fill time
    entry_entropy: float
    entry_p_switch: float
    entry_price: float
    exit_price: float
    entry_ts: float
    side: str                           # "A" or "B"

    # Updated each tick
    current_regime_posterior: list[float]  # 9D now
    current_entropy: float
    current_p_switch: float
    elapsed_sec: float
    distance_from_market_pct: float       # how far is exit from current price

    # Survival model predictions
    p_fill_30m: float
    p_fill_1h: float
    p_fill_4h: float
    median_remaining_sec: float

    # Derived signals
    regime_agreement: float    # cosine similarity between entry and current posteriors
    expected_value: float      # E[value] = P(fill) * profit - (1-P) * opportunity_cost
    ev_trend: str              # "rising" | "stable" | "falling" (over last 3 updates)

    # Action recommendation
    recommended_action: str    # "hold" | "tighten" | "widen" | "reprice_breakeven"
    action_confidence: float   # 0-1
```

### 9.2 Regime Agreement Score

Measures how much the market's regime has changed since trade entry:

```
regime_agreement = cosine_similarity(entry_posterior_9d, current_posterior_9d)
```

- `1.0`: Market regime is identical to entry conditions. Trade thesis is intact.
- `0.5–0.8`: Moderate drift. Some timeframes have shifted.
- `< 0.3`: Severe disagreement. The market is in a fundamentally different state.

This is the "new information" signal the Bayesian tweet describes.

### 9.3 Expected Value Computation

```
opportunity_cost_rate = BELIEF_OPPORTUNITY_COST_PER_HOUR  # default $0.001/slot-hour
elapsed_hours = elapsed_sec / 3600

profit_if_fill = (exit_price - entry_price) * volume  # for B-side; inverse for A
p_fill = survival_model.p_fill_1h  # or weighted blend of horizons

expected_fill_profit = p_fill * profit_if_fill
expected_opportunity_cost = (1 - p_fill) * opportunity_cost_rate * elapsed_hours
expected_value = expected_fill_profit - expected_opportunity_cost
```

`expected_value` is the central signal. When it goes negative, the trade is expected to lose money (accounting for opportunity cost of locked capital).

### 9.4 Action Mapping

Replace fixed timer thresholds with belief-driven decisions:

| Condition | Current Behavior | Belief-Driven Action |
|-----------|-----------------|---------------------|
| `regime_agreement > 0.8` AND `p_fill_1h > 0.5` | Fixed hold (timer-based) | **Hold**: conditions support the trade. Optionally widen exit if `ev_trend = "rising"` and `BELIEF_WIDEN_ENABLED` |
| `regime_agreement < 0.3` AND `confidence_score > 0.6` | Wait for `reprice_after` timer | **Reprice to breakeven + fees immediately**: confident the regime flipped against the trade |
| `p_fill_1h < 0.10` AND `expected_value < 0` | Wait for `orphan_after` timer | **Tighten aggressively**: survival model says this exit is unlikely to fill. Reprice closer to market. |
| `p_fill_1h` declining over 3 consecutive updates | No signal | **Graduated tighten**: progressive repricing as probability deteriorates, like a trailing stop on probability |
| `p_fill_30m > 0.80` AND `fill_imbalance` confirms direction | No signal | **Widen slightly** (if enabled): similar trades are filling fast, let this one capture more of the move |
| BOCPD `change_prob > 0.50` | No signal | **Re-evaluate all open exits**: force immediate belief update and action check |

### 9.5 Widen Exit (Optional, Aggressive)

When conditions strongly support a trade and fill probability is high, optionally widen the exit target to capture more of a trend. This implements "let winners run."

```
if (BELIEF_WIDEN_ENABLED
    and regime_agreement > 0.85
    and p_fill_1h > 0.70
    and direction confirms trade side
    and not already widened this cycle):

    new_exit = exit_price * (1 + BELIEF_WIDEN_STEP_PCT)  # e.g., 0.1%
    # Cancel and replace exit on Kraken
```

**Safety**: Maximum widen count per trade (`BELIEF_MAX_WIDEN_COUNT`, default 2). Maximum total widen distance (`BELIEF_MAX_WIDEN_TOTAL_PCT`, default 0.5%). Never widen during S2 (both sides open — too risky).

### 9.6 Interaction with Existing Timer System

The belief tracker does NOT remove existing timers. It layers on top:

- **Belief action fires BEFORE timer**: If the belief tracker recommends "tighten" at t=2h, it fires even though `reprice_after` might be t=4h.
- **Timer fires as backstop**: If the belief tracker has insufficient data (`SURVIVAL_MODEL_ENABLED=False` or model not yet activated), timers run unchanged.
- **Timer never overrides belief hold**: If the belief tracker says "hold" (high p_fill, high agreement), the timer is suppressed (deferred) for up to `BELIEF_TIMER_OVERRIDE_MAX_SEC` (default 3600). After that, the timer fires regardless (hard safety).

This ensures zero regression: disable the belief tracker and you're back to current timer behavior.

### 9.7 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `BELIEF_TRACKER_ENABLED` | `False` | bool | Master toggle for per-trade beliefs |
| `BELIEF_UPDATE_INTERVAL_SEC` | `60.0` | float | How often to update per-trade beliefs |
| `BELIEF_OPPORTUNITY_COST_PER_HOUR` | `0.001` | float | USD opportunity cost per slot-hour locked |
| `BELIEF_TIGHTEN_THRESHOLD_PFILL` | `0.10` | float | P(fill_1h) below which tightening activates |
| `BELIEF_TIGHTEN_THRESHOLD_EV` | `0.0` | float | EV below which tightening activates |
| `BELIEF_IMMEDIATE_REPRICE_AGREEMENT` | `0.30` | float | Regime agreement below which immediate reprice fires |
| `BELIEF_IMMEDIATE_REPRICE_CONFIDENCE` | `0.60` | float | Confidence above which immediate reprice is confident |
| `BELIEF_WIDEN_ENABLED` | `False` | bool | Allow widening exits when conditions are strong |
| `BELIEF_WIDEN_STEP_PCT` | `0.001` | float | Each widen step (0.1%) |
| `BELIEF_MAX_WIDEN_COUNT` | `2` | int | Maximum widens per trade |
| `BELIEF_MAX_WIDEN_TOTAL_PCT` | `0.005` | float | Maximum total widen distance (0.5%) |
| `BELIEF_TIMER_OVERRIDE_MAX_SEC` | `3600` | float | Maximum time belief "hold" can defer a timer |
| `BELIEF_EV_TREND_WINDOW` | `3` | int | Number of updates to compute EV trend |
| `BELIEF_LOG_ACTIONS` | `True` | bool | Log belief-driven actions |

---

## 10. Phase 5: Action Knobs (Replaces Tier Buckets)

**Goal**: Replace the discrete tier 0/1/2 action templates with continuous control parameters derived from belief signals. The tier label becomes a display convenience, not a decision boundary.

### 10.1 Belief-Derived Signals

Five continuous signals computed from the belief state:

| Signal | Formula | Range | Meaning |
|--------|---------|-------|---------|
| `direction_score` | `P(bull)_consensus - P(bear)_consensus` | [-1, +1] | Directional bias |
| `confidence_score` | `1 - H(π_consensus) / H_max` | [0, 1] | Certainty of current regime |
| `boundary_score` | `p_switch_consensus` | [0, 1] | Transition risk |
| `volatility_score` | `volume_ratio_ema` (from features) | [0, +inf] | Market activity |
| `congestion_score` | `congestion_ratio` (from features) | [0, 1] | Exit backlog |

### 10.2 Continuous Knobs

Each knob is derived from the signal vector via a configured mapping function:

#### Aggression (sizing multiplier)

```
base = 1.0
direction_boost = KNOB_AGGRESSION_DIRECTION * |direction_score| * confidence_score
boundary_dampener = 1.0 - KNOB_AGGRESSION_BOUNDARY * boundary_score
congestion_dampener = 1.0 - KNOB_AGGRESSION_CONGESTION * congestion_score

aggression = clamp(base + direction_boost) * boundary_dampener * congestion_dampener
           = clamp(result, KNOB_AGGRESSION_FLOOR, KNOB_AGGRESSION_CEILING)
```

**Effect**: Feeds into throughput sizer as an additional multiplier. Aggressive when confident and directional. Conservative when transitioning or congested.

#### Spacing (entry_pct multiplier)

```
base = 1.0
vol_stretch = KNOB_SPACING_VOLATILITY * max(0, volatility_score - 1.0)
boundary_stretch = KNOB_SPACING_BOUNDARY * boundary_score

spacing_mult = clamp(base + vol_stretch + boundary_stretch,
                     KNOB_SPACING_FLOOR, KNOB_SPACING_CEILING)
```

**Effect**: Replaces the tier 1 asymmetric spacing with a continuous version. High volatility or boundary risk → wider spacing. Applies to `entry_pct_a` / `entry_pct_b` via `_engine_cfg()`.

Per-side asymmetry preserved:
```
if direction_score > 0:  # bullish
    spacing_a = spacing_mult * (1 + KNOB_ASYMMETRY * |direction_score|)  # widen A
    spacing_b = spacing_mult * (1 - KNOB_ASYMMETRY * |direction_score|)  # tighten B
```

#### Reprice Cadence (multiplier on reprice_after threshold)

```
cadence_mult = clamp(
    1.0 - KNOB_CADENCE_BOUNDARY * boundary_score - KNOB_CADENCE_ENTROPY * normalized_entropy,
    KNOB_CADENCE_FLOOR, 1.0
)
effective_reprice_after = base_reprice_after * cadence_mult
```

**Effect**: Faster repricing when beliefs are in flux. Default cadence when stable.

#### Suppression Threshold (replaces tier 2 binary)

Instead of binary suppress/don't-suppress at tier 2 confidence:

```
suppression_strength = clamp(
    (|direction_score| - KNOB_SUPPRESS_DIRECTION_FLOOR) * confidence_score / KNOB_SUPPRESS_SCALE,
    0.0, 1.0
)
```

- `suppression_strength = 0.0`: symmetric, no suppression (current tier 0)
- `suppression_strength = 0.5`: partial suppression — against-trend entries are placed but at reduced size
- `suppression_strength = 1.0`: full suppression (current tier 2) — against-trend entries cancelled

This adds a middle ground that the current system lacks: instead of jumping from "both sides" to "cancel one side entirely," there's a continuous ramp.

### 10.3 Derived Tier Label (Display Only)

For dashboard display and AI advisor context, derive a tier label from the knob state:

```
if suppression_strength > 0.8:
    display_tier = 2  # "directional"
elif suppression_strength > 0.2 or aggression != 1.0:
    display_tier = 1  # "biased"
else:
    display_tier = 0  # "symmetric"
```

The label is informational. The knobs drive behavior.

### 10.4 Backward Compatibility

When `KNOB_MODE_ENABLED=False` (default), the existing tier 0/1/2 system runs unchanged. The knob system is purely additive.

### 10.5 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `KNOB_MODE_ENABLED` | `False` | bool | Replace tier buckets with continuous knobs |
| `KNOB_AGGRESSION_DIRECTION` | `0.5` | float | Direction score contribution to aggression |
| `KNOB_AGGRESSION_BOUNDARY` | `0.3` | float | Boundary risk dampening on aggression |
| `KNOB_AGGRESSION_CONGESTION` | `0.5` | float | Congestion dampening on aggression |
| `KNOB_AGGRESSION_FLOOR` | `0.5` | float | Minimum aggression multiplier |
| `KNOB_AGGRESSION_CEILING` | `1.5` | float | Maximum aggression multiplier |
| `KNOB_SPACING_VOLATILITY` | `0.3` | float | Volatility contribution to spacing stretch |
| `KNOB_SPACING_BOUNDARY` | `0.2` | float | Boundary risk contribution to spacing stretch |
| `KNOB_SPACING_FLOOR` | `0.8` | float | Minimum spacing multiplier |
| `KNOB_SPACING_CEILING` | `1.5` | float | Maximum spacing multiplier |
| `KNOB_ASYMMETRY` | `0.3` | float | Max per-side spacing asymmetry |
| `KNOB_CADENCE_BOUNDARY` | `0.5` | float | Boundary score influence on reprice cadence |
| `KNOB_CADENCE_ENTROPY` | `0.3` | float | Entropy influence on reprice cadence |
| `KNOB_CADENCE_FLOOR` | `0.3` | float | Minimum cadence multiplier (fastest repricing) |
| `KNOB_SUPPRESS_DIRECTION_FLOOR` | `0.3` | float | Minimum |direction| for any suppression |
| `KNOB_SUPPRESS_SCALE` | `0.5` | float | Scaling factor for suppression ramp |

---

## 11. Future Phases (Out of Scope, Noted for Reference)

### Phase 6: Online EM Parameter Adaptation

Replace daily HMM retrain with stochastic approximation:

```
θ_new = θ_old + γ_t * ∇ log P(x_t | θ_old)
```

Where `γ_t` decays as `1/t` (Robbins-Monro condition). The transition matrix and emission parameters drift continuously instead of cliff-resetting daily. Requires stability analysis and a shadow "frozen" baseline for sanity checking.

### Phase 7: Contextual Bandits for Action Selection

Instead of hand-tuned knob formulas (Phase 5), learn the mapping from belief state → optimal action from outcomes. A contextual bandit (LinUCB or Thompson Sampling) explores different action settings for each belief context and converges on the highest-EV actions. Requires calibrated outcome tracking (Brier score, log loss) as a prerequisite.

### Phase 8: Alternative Model Exploration

| Model | What It Solves | Complexity |
|-------|---------------|------------|
| **AR-HMM** (autoregressive emissions) | Current HMM assumes i.i.d. observations. Returns have autocorrelation. AR-HMM captures momentum persistence within regimes. | Medium |
| **Student-t emissions** | Gaussian HMM overreacts to crypto spikes (outliers under Gaussian, expected under Student-t). More stable posteriors during pumps/dumps. | Low (config change in hmmlearn) |
| **HSMM** (hidden semi-Markov) | Explicitly models how long you stay in a regime (duration distribution). Gives "expected time remaining in current regime" — directly useful for exit management. | Medium |
| **Switching Kalman Filter** | Smoother inference than HMM for continuous features. Natural uncertainty quantification. | Medium-High |
| **6-state HMM** (direction × volatility) | Split RANGING into quiet/choppy. Split BULL/BEAR into grinding/spiking. Richer regime taxonomy without HDP-HMM complexity. | Low |
| **HDP-HMM** (auto state discovery) | Discovers the "natural" number of regimes from data. No manual state-count tuning. | High (pyhsmm dependency) |

### Phase 9: Calibration and Monitoring

Once actions depend on probabilities:
- **Reliability curves**: Is P(fill)=0.7 actually filling 70% of the time?
- **Brier score per regime bucket**: Are predictions calibrated?
- **Action regret monitoring**: Did chosen action outperform alternatives?
- **Post-hoc calibration** (Platt scaling / isotonic) if raw probabilities are miscalibrated.

---

## 12. Data Flow (Complete)

### Per Main Loop Tick (30s)

```
1. Price tick, OHLCV candle arrives
2. Feature enrichment (fill_imbalance, spread_realization, etc.)  [Phase 2]
3. BOCPD update → change_prob                                     [Phase 1]
4. If change_prob > urgent OR eval timer expired:
   a. HMM inference (1m, 15m, 1h) → 9D posterior
   b. Compute entropy, p_switch per timeframe
   c. Compute consensus posterior, direction, confidence, boundary
   d. Derive action knobs (aggression, spacing, cadence, suppression) [Phase 5]
5. For each open exit in S1/S2:                                   [Phase 4]
   a. Update TradeBeliefState (current posterior, elapsed, distance)
   b. Survival model → P(fill_30m), P(fill_1h), P(fill_4h)       [Phase 3]
   c. Compute regime_agreement, expected_value, ev_trend
   d. Map to action: hold / tighten / widen / reprice_breakeven
   e. Execute action if changed (cancel/replace on Kraken)
6. Apply action knobs to EngineConfig for new entries              [Phase 5]
7. Existing state machine / slot management (unchanged)
8. On cycle completion:
   a. Log posteriors, entropy, p_switch with CompletedCycle        [Phase 0]
   b. Update survival model training set                           [Phase 3]
```

### Daily

```
1. Retrain HMM (existing cadence)
2. Retrain survival model on accumulated fills + optional synthetic [Phase 3]
3. Refit Cox / logistic regression coefficients                    [Phase 3]
```

---

## 13. Safety Invariants

1. **Belief tracker never bypasses min-volume guards.** `compute_order_volume()` returning None still blocks placement regardless of belief signals.
2. **Timer backstop is never permanently deferred.** `BELIEF_TIMER_OVERRIDE_MAX_SEC` caps how long a "hold" recommendation can suppress the existing timer. After that, the timer fires.
3. **Widen is bounded.** Maximum total widen per trade is `BELIEF_MAX_WIDEN_TOTAL_PCT`. Never widen during S2.
4. **Suppression ramp respects capacity.** If `capacity_band == "stop"`, suppression_strength is forced to 0 (symmetric) regardless of beliefs.
5. **BOCPD cannot cause action without regime eval.** BOCPD triggers re-evaluation; it doesn't directly cause orders. The regime eval → knob computation pipeline must complete.
6. **Survival model returns safe defaults.** When insufficient data: `p_fill = 0.5`, `confidence = 0.0`, `recommended_action = "hold"`. No action taken on low-confidence predictions.
7. **Feature enrichment cold-start is neutral.** Unknown private features default to 0 (no signal), not to values that trigger actions.
8. **All phases fail gracefully to existing behavior.** `BOCPD_ENABLED=False` → no change-point signals. `SURVIVAL_MODEL_ENABLED=False` → fixed timers. `BELIEF_TRACKER_ENABLED=False` → no per-trade management. `KNOB_MODE_ENABLED=False` → tier 0/1/2 unchanged.
9. **No phase has dependencies on a later phase.** Phase 0 is useful alone. Phase 1 is useful without Phase 3. Each phase adds value independently.
10. **State machine reducer is never modified.** All intelligence is in the runtime layer (`bot.py`) and advisory modules. The pure reducer contract is preserved.

---

## 14. Status Payload (Complete)

### New `belief_state` block (Phase 0+)

```json
{
  "belief_state": {
    "enabled": true,
    "posterior_1m": [0.08, 0.21, 0.71],
    "posterior_15m": [0.28, 0.44, 0.28],
    "posterior_1h": [0.12, 0.16, 0.72],
    "entropy_1m": 0.34,
    "entropy_15m": 0.89,
    "entropy_1h": 0.41,
    "entropy_consensus": 0.52,
    "confidence_score": 0.48,
    "p_switch_1m": 0.04,
    "p_switch_15m": 0.08,
    "p_switch_1h": 0.03,
    "p_switch_consensus": 0.05,
    "direction_score": 0.63,
    "boundary_risk": "low"
  }
}
```

### New `bocpd` block (Phase 1)

```json
{
  "bocpd": {
    "enabled": true,
    "change_prob": 0.12,
    "run_length_mode": 87,
    "alert_active": false,
    "last_update_ts": 1739616900,
    "observation_count": 4200
  }
}
```

### New `survival_model` block (Phase 3)

```json
{
  "survival_model": {
    "enabled": true,
    "model_tier": "cox",
    "n_observations": 342,
    "n_censored": 24,
    "last_retrain_ts": 1739616000,
    "strata_counts": {
      "bearish_A": 28, "bearish_B": 31,
      "ranging_A": 82, "ranging_B": 79,
      "bullish_A": 64, "bullish_B": 58
    },
    "synthetic_observations": 1500,
    "cox_coefficients": {
      "p_bull_1h": 0.42,
      "p_bear_1h": -0.38,
      "distance_pct": -1.20,
      "entropy": -0.15,
      "fill_imbalance": 0.31
    }
  }
}
```

### New `trade_beliefs` block (Phase 4)

```json
{
  "trade_beliefs": {
    "enabled": true,
    "tracked_exits": 24,
    "actions_this_session": {
      "hold": 18,
      "tighten": 4,
      "widen": 1,
      "reprice_breakeven": 1
    },
    "avg_regime_agreement": 0.74,
    "avg_expected_value": 0.0012,
    "exits_with_negative_ev": 3,
    "timer_overrides_active": 2,
    "last_update_ts": 1739616900
  }
}
```

### New `action_knobs` block (Phase 5)

```json
{
  "action_knobs": {
    "enabled": true,
    "aggression": 1.15,
    "spacing_mult": 1.08,
    "spacing_a": 1.22,
    "spacing_b": 0.94,
    "cadence_mult": 0.75,
    "suppression_strength": 0.35,
    "derived_tier": 1,
    "derived_tier_label": "biased"
  }
}
```

---

## 15. Dashboard Updates

### Phase 0: Belief State Card

New card in HMM section (see §5.6 for layout).

### Phase 1: BOCPD Indicator

Add to belief state card:
```
  Change Point: 12%  ██░░░░░░░░░░░░░░░░░░
```

When alert active (> threshold):
```
  Change Point: 72%  ██████████████░░░░░░ ⚡ STRUCTURAL BREAK DETECTED
```

### Phase 3: Survival Model Card

New card below throughput sizer:
```
Survival Model (Cox)
  Observations: 342 real + 1,500 synthetic
  Strata: 6/6 active
  Key coefficients: p_bull_1h: +0.42, distance: -1.20
  Retrained: 4h ago
```

### Phase 4: Per-Trade Belief Badges

On each slot in the slot detail view, add a belief badge:

```
Slot "doge" [S1b] — B exit @ $0.0912
  Belief: P(fill 1h)=0.67  EV=+$0.003  Agreement=0.82  → HOLD
```

Color: green (hold/widen), yellow (tighten), red (reprice_breakeven).

When belief tracker recommends tighten:
```
  Belief: P(fill 1h)=0.08  EV=-$0.001  Agreement=0.31  → TIGHTEN ⚡
```

### Phase 5: Knobs Display

Replace tier display with knobs:
```
Action Knobs
  Aggression: 1.15×    Spacing: 1.08× (A: 1.22, B: 0.94)
  Cadence: 0.75×       Suppress: 35%
  [Tier 1 — Biased]    ← derived label
```

---

## 16. Files Modified

### New Files

| File | Purpose | Est. Lines |
|------|---------|-----------|
| `bayesian_engine.py` | Entropy, p_switch, belief vector computation, action knobs | ~300 |
| `bocpd.py` | Bayesian Online Change-Point Detection | ~120 |
| `survival_model.py` | Kaplan-Meier, Cox PH, synthetic generation, prediction | ~400 |

### Modified Files

| File | Phase | Change | Est. Lines |
|------|-------|--------|-----------|
| `config.py` | 0-5 | ~50 new config vars across all phases | ~80 |
| `hmm_regime_detector.py` | 0,1,2 | `compute_entropy()`, `compute_p_switch()` static methods; extended feature extraction if enriched | ~40 |
| `grid_strategy.py` | 0,4 | Posterior fields on CompletedCycle; per-trade belief integration in exit management | ~60 |
| `bot.py` | 0-5 | Belief state computation in `_update_regime_tier()`; BOCPD updates; survival model lifecycle; belief tracker main loop; knob computation; status payload blocks; API endpoint for belief override | ~250 |
| `dashboard.py` | 0-5 | Belief state card, BOCPD indicator, survival card, belief badges, knobs display | ~150 |
| `throughput_sizer.py` | 5 | Accept aggression knob as additional multiplier | ~10 |

### Estimated Totals

| Phase | New Lines | Modified Lines | Total |
|-------|-----------|---------------|-------|
| Phase 0 (Instrumentation) | 0 | ~60 | ~60 |
| Phase 1 (Belief Signals + BOCPD) | ~120 | ~80 | ~200 |
| Phase 2 (Feature Enrichment) | 0 | ~60 | ~60 |
| Phase 3 (Survival Model) | ~400 | ~50 | ~450 |
| Phase 4 (Per-Trade Beliefs) | ~200 | ~100 | ~300 |
| Phase 5 (Action Knobs) | ~100 | ~80 | ~180 |
| **Total** | **~820** | **~430** | **~1,250** |

---

## 17. Testing

### Phase 0

| # | Test |
|---|------|
| 1 | `compute_entropy([1, 0, 0])` returns 0.0 |
| 2 | `compute_entropy([1/3, 1/3, 1/3])` returns H_max (ln(3)) |
| 3 | `compute_p_switch` with identity transition matrix returns 0.0 |
| 4 | `compute_p_switch` with uniform transition matrix returns 2/3 |
| 5 | CompletedCycle includes posterior fields after fill |
| 6 | Status payload includes `belief_state` block |

### Phase 1

| # | Test |
|---|------|
| 7 | BOCPD detects synthetic change-point in mean-shifted series |
| 8 | BOCPD `change_prob` stays low on stationary series |
| 9 | Regime eval interval decreases when BOCPD alert fires |
| 10 | BOCPD state persists across snapshot save/restore |

### Phase 2

| # | Test |
|---|------|
| 11 | `fill_imbalance` = 0 when A fills == B fills |
| 12 | Extended feature vector is 8D when enriched features enabled |
| 13 | Cold-start defaults produce neutral (zero) feature values |
| 14 | HMM trains successfully on 8D observations |

### Phase 3

| # | Test |
|---|------|
| 15 | Kaplan-Meier produces monotonically decreasing survival curve |
| 16 | Censored observations extend survival curve (vs. uncensored-only) |
| 17 | Cox model coefficients have expected signs (distance negative, bull positive for B) |
| 18 | Synthetic fills cover all 6 regime×side strata |
| 19 | `P(fill_1h)` decreases as `distance_pct` increases |
| 20 | Model returns safe defaults when insufficient data |

### Phase 4

| # | Test |
|---|------|
| 21 | `regime_agreement` = 1.0 when entry and current posteriors are identical |
| 22 | `regime_agreement` < 0.3 triggers immediate reprice when confidence high |
| 23 | `expected_value` goes negative for high-elapsed, low-p_fill trades |
| 24 | Timer backstop fires after `BELIEF_TIMER_OVERRIDE_MAX_SEC` even if belief says hold |
| 25 | Widen never exceeds `BELIEF_MAX_WIDEN_TOTAL_PCT` |
| 26 | Widen never fires during S2 |
| 27 | Belief tracker disabled → pure timer behavior (regression) |

### Phase 5

| # | Test |
|---|------|
| 28 | `aggression` clamps to `[floor, ceiling]` |
| 29 | `suppression_strength` = 0 when `|direction_score|` < floor |
| 30 | `suppression_strength` = 1.0 for extreme directional + high confidence |
| 31 | Derived tier label matches expected tier for given knob values |
| 32 | Knobs disabled → existing tier 0/1/2 behavior (regression) |

### Integration

| # | Test |
|---|------|
| 33 | Full pipeline: candle → BOCPD → HMM → belief → survival → trade action |
| 34 | Status payload includes all new blocks with correct shapes |
| 35 | Snapshot round-trip preserves BOCPD, survival, and belief states |
| 36 | All phases disabled → identical behavior to current system |

---

## 18. Rollout Plan

### Stage A: Instrumentation (Phase 0) — Deploy Immediately

Enable:
- `BELIEF_STATE_LOGGING_ENABLED=True`
- `BELIEF_STATE_IN_STATUS=True`

Observe:
- Posteriors, entropy, p_switch accumulating with completed cycles
- Dashboard belief state card showing continuous signals
- No behavior change. Pure observation.

Duration: Indefinite (runs in background while subsequent phases develop).

### Stage B: BOCPD Shadow (Phase 1) — After 3+ days of Phase 0 data

Enable:
- `BOCPD_ENABLED=True`

Observe (shadow only — no action changes):
- `change_prob` spikes correlate with actual regime transitions?
- Alert frequency reasonable (not constant false alarms)?
- Eval interval acceleration responsive but not excessive?

Duration: 48h observation.

### Stage C: Feature Enrichment (Phase 2) — After BOCPD validates

Enable:
- `ENRICHED_FEATURES_ENABLED=True`
- Force HMM retrain after enabling

Observe:
- HMM regime classifications sharper with enriched features?
- Feature cold-start behavior clean?
- No regression in existing regime detection quality

Duration: 48h observation + compare regime quality metrics.

### Stage D: Survival Model Shadow (Phase 3) — After ~200 fills with posteriors logged

Enable:
- `SURVIVAL_MODEL_ENABLED=True`
- `SURVIVAL_SYNTHETIC_ENABLED=True`

Observe (shadow only — predictions logged but not acted on):
- Survival predictions correlate with actual fill times?
- Cox coefficients have expected signs?
- Calibration: when model says P(fill_1h)=0.7, do ~70% actually fill?

Duration: 72h shadow with calibration analysis.

### Stage E: Per-Trade Beliefs (Phase 4) — After survival model validates

Enable:
- `BELIEF_TRACKER_ENABLED=True`
- Start with conservative thresholds (low tighten rate, no widen)

Observe:
- Belief-driven tightens vs. timer-driven tightens: which fires first? Better outcomes?
- Regime agreement score tracking (are belief actions correlated with regime shifts?)
- No increase in unnecessary cancels/replaces (churn)

Duration: 1 week. Gradually tune thresholds.

### Stage F: Action Knobs (Phase 5) — After per-trade beliefs prove out

Enable:
- `KNOB_MODE_ENABLED=True`
- Start with conservative coefficients (small deviations from tier behavior)

Observe:
- Continuous knob values vs. discrete tier decisions: smoother transitions?
- Suppression ramp behavior: does partial suppression outperform binary?
- Overall P&L impact

Duration: Ongoing tuning.

### Stage G: Optional — Widen Exits

Enable:
- `BELIEF_WIDEN_ENABLED=True`
- Very conservative: `BELIEF_MAX_WIDEN_COUNT=1`, `BELIEF_MAX_WIDEN_TOTAL_PCT=0.002`

Observe:
- Do widened exits capture more profit?
- Does widen frequency correlate with trend continuation?
- Any increase in orphan rate?

---

## 19. Rollback

Each phase rolls back independently:

| Phase | Rollback | Effect |
|-------|----------|--------|
| Phase 0 | `BELIEF_STATE_LOGGING_ENABLED=False` | Stop logging posteriors (data already collected remains) |
| Phase 1 | `BOCPD_ENABLED=False` | No change-point signals; eval interval reverts to fixed 300s |
| Phase 2 | `ENRICHED_FEATURES_ENABLED=False` | HMM uses original 4D features (retrain needed) |
| Phase 3 | `SURVIVAL_MODEL_ENABLED=False` | No fill predictions; timers are sole trigger |
| Phase 4 | `BELIEF_TRACKER_ENABLED=False` | No per-trade management; existing timers run unchanged |
| Phase 5 | `KNOB_MODE_ENABLED=False` | Tier 0/1/2 with fixed action templates |

No state migration needed for any rollback. All changes are additive.

---

## 20. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Survival model miscalibrated → bad tighten/widen decisions | Medium | Shadow mode (Stage D) with calibration analysis before live. Timer backstop always runs. |
| BOCPD false alarms → excessive regime eval → API budget | Low | Rate limit on accelerated eval. `REGIME_EVAL_INTERVAL_FAST` has a floor (60s). |
| Enriched features cause HMM instability (more dimensions, same data) | Low | Separate enable flag. Compare regime quality before/after. Rollback to 4D. |
| Belief "widen" causes exits to never fill | Medium | Bounded by `MAX_WIDEN_TOTAL_PCT`. Timer backstop. Disabled by default. |
| Per-trade churn (too many cancel/replace cycles) | Medium | `BELIEF_UPDATE_INTERVAL_SEC` throttles update frequency. Action hysteresis: only act on belief changes, not on steady state. |
| Survival model overfits to synthetic data | Low | Synthetic weight capped at 0.3. Real data always dominates when available. |
| Cox regression numerically unstable with small N | Low | Fall back to Kaplan-Meier when N < `SURVIVAL_MIN_OBSERVATIONS`. |
| Knob parameter tuning overwhelm (50+ config vars) | Medium | Ship with sensible defaults. Each phase has < 15 vars. Document recommended tuning order. |
| Complexity budget: too many interacting systems | Medium | Each phase is independently valuable and independently disableable. No phase requires a later phase. |
