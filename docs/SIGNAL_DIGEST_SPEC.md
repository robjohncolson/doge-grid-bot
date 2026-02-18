# Signal Digest Spec

**Version:** 0.1
**Date:** 2026-02-18
**Status:** Draft

---

## Problem

The bot computes 200+ signals every cycle — HMM posteriors, MACD histogram
slope, RSI zone, EMA spread, survival probabilities, belief states, manifold
score, throughput efficiency, capacity metrics, regime tiers, action knobs.
These are dumped raw into the status payload. A human operator has to mentally
synthesize them to answer: "Is my configuration working right now?"

What's needed: a single traffic light (green/amber/red) with a pointer to
the most concerning signal, plus periodic plain-English interpretation of
what the technical indicators mean together — written in trading language,
not system internals.

---

## Design

Two layers:

### Layer 1: Rule Engine (every cycle, <10ms)

12 diagnostic rules. Each checks one dimension of system health against
thresholds derived from the bot's own data. Each returns:

```python
@dataclass
class DiagnosticCheck:
    signal: str        # e.g. "rsi_zone", "age_skew"
    severity: str      # "green" | "amber" | "red"
    title: str         # short label for dashboard
    detail: str        # trading-aware interpretation (1-2 sentences)
    value: float       # raw metric value
    threshold: str     # what triggered the severity
```

Overall light = worst severity across all checks.
Top concern = the red/amber check with the highest priority.

### Layer 2: Market Interpreter (periodic, LLM-powered)

Every `DIGEST_INTERPRETATION_INTERVAL_SEC` (default 600s = 10 min), or when
the traffic light changes color, fire an LLM call that receives the full
signal snapshot and returns a plain-English market interpretation.

Uses the existing AI advisor infrastructure (DeepSeek primary, Groq fallback).
Separate from the regime advisor — different prompt, different purpose.

---

## Layer 1: Diagnostic Rules

### Rule 1: EMA Trend Alignment

**Signal**: `ema_trend`
**Source**: `trend.score` (fast_ema vs slow_ema spread)
**What it means**: EMA crossover direction and strength

| Condition | Severity | Detail |
|-----------|----------|--------|
| `|trend_score| < 0.003` | green | "EMAs converged — price in equilibrium. Ideal for grid cycling." |
| `0.003 <= |score| < 0.01` | amber | "EMAs diverging {X}% — developing {bullish/bearish} lean. Grid entries on {unfavored} side may orphan more." |
| `|score| >= 0.01` | red | "Strong EMA divergence ({X}%) — trending market. Grid cycling against the trend will produce orphans." |

### Rule 2: RSI Zone

**Signal**: `rsi_zone`
**Source**: HMM observation vector, `rsi_zone` field (normalized -1 to +1)
**What it means**: Momentum position on the oversold-overbought spectrum

| Condition | Severity | Detail |
|-----------|----------|--------|
| `-0.2 <= rsi <= 0.2` | green | "RSI neutral ({raw}) — no momentum extreme. Grid entries balanced." |
| `0.2 < rsi <= 0.4` or `-0.4 <= rsi < -0.2` | amber | "RSI {overbought/oversold} territory ({raw}) — {sell/buy} exits more likely to fill, but trend reversal risk rising." |
| `rsi > 0.4` or `rsi < -0.4` | red | "RSI extreme ({raw}) — strongly {overbought/oversold}. Mean reversion likely soon. {Sell/Buy} entries risky." |

### Rule 3: MACD Momentum

**Signal**: `macd_momentum`
**Source**: HMM observation vector, `macd_hist_slope` field
**What it means**: Whether momentum is accelerating or fading

| Condition | Severity | Detail |
|-----------|----------|--------|
| `|slope| < 1e-6` | green | "MACD flat — no momentum change. Stable range conditions." |
| `slope negative AND regime RANGING` | amber | "MACD declining in RANGING — bearish momentum building. Watch for regime shift." |
| `slope positive AND regime RANGING` | amber | "MACD rising in RANGING — bullish momentum building. Watch for regime shift." |
| `slope aligns with non-RANGING regime` | green | "MACD confirms {regime} — momentum and regime agree." |
| `slope opposes regime` | red | "MACD diverges from regime — momentum says {X} but HMM says {Y}. Signal conflict." |

### Rule 4: Regime Confidence

**Signal**: `regime_confidence`
**Source**: `hmm_regime.confidence_effective`
**What it means**: How certain the HMM is about the current regime

| Condition | Severity | Detail |
|-----------|----------|--------|
| `confidence >= 0.80` | green | "Regime confidence {X}% — HMM strongly believes {REGIME}. Grid parameters well-tuned to current conditions." |
| `0.50 <= confidence < 0.80` | amber | "Regime confidence only {X}% — HMM uncertain. May be in transition. Entry timing less reliable." |
| `confidence < 0.50` | red | "Regime confidence low ({X}%) — effectively guessing. Consider pausing entries or reducing slot count." |

### Rule 5: Timeframe Agreement

**Signal**: `timeframe_agreement`
**Source**: `hmm_regime.agreement` + individual timeframe regimes
**What it means**: Whether 1m, 15m, and 1h HMMs agree on regime

| Condition | Severity | Detail |
|-----------|----------|--------|
| all three match | green | "Full timeframe agreement — 1m/15m/1h all say {REGIME}. Strong conviction." |
| 2 of 3 match | amber | "{tf} says {X} while others say {Y} — short-term divergence. {1m dissent = noise likely, 1h dissent = watch for trend change}." |
| all three differ | red | "No timeframe agreement — market in transition. Grid will produce orphans on both sides." |

### Rule 6: Boundary Risk

**Signal**: `boundary_risk`
**Source**: `belief_state.boundary_risk` + `p_switch_consensus`
**What it means**: Probability of imminent regime change

| Condition | Severity | Detail |
|-----------|----------|--------|
| `boundary_risk == "low"` | green | "Low regime switch probability ({p_switch:.1%}) — stable conditions for grid cycling." |
| `boundary_risk == "medium"` | amber | "Moderate switch probability ({p_switch:.1%}) — regime boundary nearby. Rangers/entries may get caught in transition." |
| `boundary_risk == "high"` | red | "High switch probability ({p_switch:.1%}) — regime change likely soon. Open entries risk immediate orphaning." |

### Rule 7: Position Age Distribution

**Signal**: `age_skew`
**Source**: `self_healing.age_bands` (fresh/aging/stale/stuck/write_off)
**What it means**: How many positions are stuck far from market

| Condition | Severity | Detail |
|-----------|----------|--------|
| stuck+write_off < 30% | green | "Healthy age distribution — most exits within reach of current price." |
| 30% <= stuck+write_off < 60% | amber | "{N} of {total} positions ({pct}%) in stuck/write-off bands — significant capital locked in distant exits. Consider if regime has fundamentally shifted." |
| stuck+write_off >= 60% | red | "{N} of {total} positions ({pct}%) stuck/write-off — majority of grid capital frozen. These exits likely need a major price move to fill." |

### Rule 8: Exit Distance from Market

**Signal**: `exit_distance`
**Source**: `trade_beliefs.positions[].distance_from_market_pct` (median of worst side)
**What it means**: How far open exit orders are from current price

| Condition | Severity | Detail |
|-----------|----------|--------|
| median < 3% | green | "Exits close to market (median {X}%) — normal grid operation." |
| 3% <= median < 8% | amber | "Exits moderately distant (median {X}%) — entered at a different price level. Will fill when price mean-reverts." |
| median >= 8% | red | "Exits far from market (median {X}%) — {N} positions need {X}%+ price move to fill. This capital is effectively frozen until a major reversal." |

### Rule 9: Order Headroom

**Signal**: `headroom`
**Source**: `capacity_fill_health.open_order_headroom`
**What it means**: How many more orders can be placed on Kraken

| Condition | Severity | Detail |
|-----------|----------|--------|
| headroom >= 50 | green | "Plenty of order headroom ({N} slots available)." |
| 20 <= headroom < 50 | amber | "Order headroom narrowing ({N} remaining) — approaching Kraken limit. New slots will squeeze capacity." |
| headroom < 20 | red | "Critical headroom ({N} remaining) — near Kraken's open order limit. No new entries should be placed." |

### Rule 10: Ranger Health

**Signal**: `ranger_health`
**Source**: `rangers.*`
**What it means**: Whether the sell-side micro-cyclers are productive

| Condition | Severity | Detail |
|-----------|----------|--------|
| not enabled | green | "Rangers disabled — not applicable." |
| enabled AND regime_ok AND cycles > 0 | green | "Rangers cycling — {cycles} cycles today, +${profit:.4f} profit, {orphans} orphans." |
| enabled AND regime_ok AND cycles == 0 AND entries placed | amber | "Rangers have entries on book but no fills yet — price hasn't reached entry level. Normal during stable range." |
| enabled AND NOT regime_ok | amber | "Rangers paused — regime is {regime}, not RANGING. Will resume when regime returns to RANGING." |
| enabled AND last_error != "" | red | "Ranger error: {last_error}. Check order placement or balance." |
| enabled AND orphans > cycles * 2 | red | "Ranger orphan rate critical ({orphans} orphans vs {cycles} cycles) — selling DOGE but not buying back. Consider pausing." |

### Rule 11: Capital Efficiency

**Signal**: `capital_efficiency`
**Source**: `throughput_sizer.util_ratio`, `slot_vintage.stuck_capital_pct`
**What it means**: Whether deployed capital is producing returns

| Condition | Severity | Detail |
|-----------|----------|--------|
| util_ratio < 0.50 | green | "Capital utilization {pct}% — room to deploy more." |
| 0.50 <= util < 0.70 | amber | "Capital utilization {pct}% — moderately loaded. Throughput sizer may start throttling." |
| util >= 0.70 | red | "Capital utilization {pct}% — heavily loaded. Throughput sizer is throttling entries. Reduce slots or wait for exits." |

### Rule 12: Manifold Score Trend

**Signal**: `mts_trend`
**Source**: `manifold_score.mts`, `manifold_score.trend`, `manifold_score.mts_30m_ago`
**What it means**: Overall system favorability trajectory

| Condition | Severity | Detail |
|-----------|----------|--------|
| mts >= 0.60 AND trend != "falling" | green | "Manifold score {mts:.3f} ({band}) — conditions favorable for grid cycling." |
| 0.40 <= mts < 0.60 OR trend == "falling" | amber | "Manifold score {mts:.3f} ({band}), {trend} — conditions degrading. Entry quality may suffer." |
| mts < 0.40 | red | "Manifold score {mts:.3f} ({band}) — hostile conditions for grid cycling. Consider reducing exposure." |

---

## Layer 2: Market Interpreter

### Architecture

Reuses the AI advisor's LLM infrastructure (provider chain, threading model,
rate limiting). Runs as a separate daemon thread from the regime advisor.

### Trigger Schedule

| Trigger | Condition |
|---------|-----------|
| Periodic | Every `DIGEST_INTERPRETATION_INTERVAL_SEC` (default 600s) |
| Event | Traffic light changes color (green→amber, amber→red, etc.) |
| Debounce | Min `DIGEST_INTERPRETATION_DEBOUNCE_SEC` (default 120s) between calls |
| Manual | POST `/api/digest/interpret` forces immediate LLM call |

### LLM Prompt

```
You are a trading systems analyst monitoring a DOGE/USD grid trading bot.
Analyze the current signal state and provide a concise interpretation.

INSTRUCTIONS:
- Explain what the technical indicators mean TOGETHER, not individually
- Use trading language (e.g., "momentum fading", "mean reversion setup")
- Identify the dominant market narrative from the signals
- Flag any signal conflicts or divergences
- Assess whether current grid configuration suits the market conditions
- Be specific about what to watch for next
- Keep it under 150 words

CURRENT STATE:
- Price: ${price}
- Regime: ${regime} (confidence: ${confidence}%)
- Timeframe agreement: ${agreement}
- MACD histogram slope: ${macd_slope} (${macd_direction})
- RSI zone: ${rsi_zone} (raw RSI: ${rsi_raw})
- EMA spread: ${ema_spread}% (trend score: ${trend_score})
- Volume ratio: ${volume_ratio}x average
- Boundary risk: ${boundary_risk} (p_switch: ${p_switch}%)
- BOCPD run length: ${run_length} candles (change prob: ${change_prob}%)
- Direction score: ${direction} (${direction_label})
- Manifold score: ${mts} (${mts_band}, ${mts_trend})

OPERATIONAL STATE:
- Slots: ${slot_count}, open orders: ${open_orders}/${limit}
- Age bands: ${fresh} fresh, ${aging} aging, ${stale} stale, ${stuck} stuck, ${write_off} write-off
- B-side exits: ${b_side_count} open, median ${b_distance}% from market
- Rangers: ${ranger_status}
- Profit today: $${profit_today}
- Fill velocity: median ${median_fill} seconds

DIAGNOSTIC TRAFFIC LIGHT: ${light} — ${top_concern}
```

### LLM Response Schema

```python
@dataclass
class MarketInterpretation:
    narrative: str        # 1-3 sentence market story
    key_insight: str      # single most important takeaway
    watch_for: str        # what could change the picture
    config_assessment: str  # "well-suited" | "mismatched" | "borderline"
    config_suggestion: str  # optional: what to adjust
    panelist: str         # which LLM provider answered
    ts: float             # timestamp
```

The response is parsed from the LLM's text output. If the LLM returns
structured JSON, use it directly. Otherwise, extract fields from natural
language with simple heuristics (first sentence = narrative, etc.).

### Fallback

If LLM call fails or times out, keep the previous interpretation.
Display staleness: "Interpretation from 12 min ago" with dimmed styling.
Rule-based Layer 1 always runs regardless of LLM availability.

---

## Data Flow

```
Every cycle (~30s):
  ┌──────────────────────────────────────────────────────────────┐
  │  _run_signal_digest(now_ts)                                  │
  │                                                              │
  │  1. Read current signals from bot state:                     │
  │     - HMM observations (MACD, RSI, EMA, volume)             │
  │     - Regime state (regime, confidence, agreement)           │
  │     - Belief state (entropy, p_switch, direction)            │
  │     - Manifold score (mts, trend, components)                │
  │     - Age bands, exit distances, headroom                    │
  │     - Ranger state                                           │
  │     - Throughput sizer state                                 │
  │                                                              │
  │  2. Run 12 diagnostic rules → list[DiagnosticCheck]          │
  │                                                              │
  │  3. Sort by severity (red > amber > green)                   │
  │     Set _digest_light = worst severity                       │
  │     Set _digest_top_concern = highest priority red/amber     │
  │                                                              │
  │  4. If light changed OR periodic timer elapsed:              │
  │     Fire LLM interpretation in daemon thread                 │
  │                                                              │
  │  5. Store results in _digest_* fields                        │
  └──────────────────────────────────────────────────────────────┘
```

---

## Accessing HMM Observations

The diagnostic rules need MACD, RSI, and EMA values. These are computed
inside `RegimeDetector.update()` in `hmm_regime_detector.py` but not
currently exposed after computation.

**Solution**: After `FeatureExtractor.extract()` returns the observation
matrix, store the latest row's raw values on the detector:

```python
# In RegimeDetector.update(), after feature extraction:
self.last_observation = Observation(
    macd_hist_slope=observations[-1, 0],
    ema_spread_pct=observations[-1, 1],
    rsi_zone=observations[-1, 2],
    volume_ratio=observations[-1, 3],
)
```

The bot already holds references to the regime detectors. The digest
reads `self._hmm_primary.last_observation` to get the latest indicator
values. No new computation needed.

---

## Status Payload

Add to `/api/status`:

```json
"signal_digest": {
  "light": "amber",
  "light_changed_at": 1771391200.0,
  "top_concern": "59% of positions in stuck/write-off bands",
  "checks": [
    {
      "signal": "age_skew",
      "severity": "amber",
      "title": "Position Age",
      "detail": "25 of 42 positions (59%) in stuck/write-off bands — significant capital locked in distant exits.",
      "value": 0.595,
      "threshold": ">= 30%"
    },
    {
      "signal": "exit_distance",
      "severity": "amber",
      "title": "Exit Distance",
      "detail": "B-side exits 15% from market on average — need major price reversal to fill.",
      "value": 15.2,
      "threshold": ">= 8%"
    },
    {
      "signal": "ema_trend",
      "severity": "green",
      "title": "EMA Trend",
      "detail": "EMAs converged — price in equilibrium. Ideal for grid cycling.",
      "value": 0.0082,
      "threshold": "< 0.3%"
    }
  ],
  "interpretation": {
    "narrative": "Market is ranging with high confidence across all timeframes. MACD flat and RSI neutral — classic mean-reversion environment. However, 59% of existing positions are from a prior bullish move and are stuck 15%+ above current price. New grid cycles via rangers are well-suited to current conditions, but legacy B-side exits are frozen.",
    "key_insight": "Current range is profitable for new entries. Legacy stuck positions are a sunk cost for now.",
    "watch_for": "RSI dropping below 35 or MACD histogram going negative — would signal bearish momentum building and potential regime shift.",
    "config_assessment": "well-suited",
    "config_suggestion": "",
    "panelist": "DeepSeek-Chat",
    "ts": 1771391200.0,
    "age_sec": 42.5
  },
  "interpretation_stale": false
}
```

---

## Dashboard Panel

### Signal Digest Card (in summary area)

```
┌─────────────────────────────────────────────────┐
│  ● Signal Digest                          AMBER │
│                                                 │
│  ⚠ 59% of positions in stuck/write-off bands   │
│                                                 │
│  Market Ranging with high conviction across all │
│  timeframes. MACD flat, RSI neutral — ideal for │
│  new grid entries. Legacy B-side exits remain   │
│  frozen 15%+ above market.                      │
│                                                 │
│  Watch: RSI < 35 or MACD going negative         │
│                                                 │
│  ┌─────────┬────────┬────────────────────────┐  │
│  │ Signal  │ Status │ Detail                 │  │
│  ├─────────┼────────┼────────────────────────┤  │
│  │ Age     │ ⚠ AMB  │ 25/42 stuck/write-off  │  │
│  │ B-Exit  │ ⚠ AMB  │ median 15% from mkt    │  │
│  │ EMA     │ ✓ GRN  │ converged, equilibrium  │  │
│  │ RSI     │ ✓ GRN  │ neutral (51)            │  │
│  │ MACD    │ ✓ GRN  │ flat, no momentum       │  │
│  │ Regime  │ ✓ GRN  │ RANGING 95% conf        │  │
│  │ Agree   │ ✓ GRN  │ 1m/15m/1h all RANGING   │  │
│  │ Switch  │ ✓ GRN  │ p_switch 4%, low risk   │  │
│  │ Head    │ ✓ GRN  │ 179 headroom            │  │
│  │ Ranger  │ ✓ GRN  │ 3 entries on book       │  │
│  │ Capital │ ✓ GRN  │ util 0.1%               │  │
│  │ MTS     │ ✓ GRN  │ 0.715, favorable        │  │
│  └─────────┴────────┴────────────────────────┘  │
│                                                 │
│  Interpretation: 42s ago via DeepSeek-Chat      │
└─────────────────────────────────────────────────┘
```

### CSS

```css
.digest-light-green { color: #5cb85c; }
.digest-light-amber { color: #f0ad4e; }
.digest-light-red   { color: #d9534f; }
.digest-check-row   { font-size: 0.85em; padding: 2px 6px; }
.digest-narrative   { font-style: italic; color: #b0b8c0; margin: 8px 0; }
.digest-stale       { opacity: 0.5; }
```

### Sorting

Checks are sorted: red first, then amber, then green.
Within same severity, sort by priority (rule number).

---

## Configuration

| Config | Default | Description |
|--------|---------|-------------|
| `DIGEST_ENABLED` | `true` | Master enable for signal digest |
| `DIGEST_INTERPRETATION_ENABLED` | `true` | Enable LLM interpretation layer |
| `DIGEST_INTERPRETATION_INTERVAL_SEC` | `600` | Periodic LLM call interval |
| `DIGEST_INTERPRETATION_DEBOUNCE_SEC` | `120` | Min time between LLM calls |
| `DIGEST_EMA_AMBER_THRESHOLD` | `0.003` | EMA spread % for amber |
| `DIGEST_EMA_RED_THRESHOLD` | `0.01` | EMA spread % for red |
| `DIGEST_RSI_AMBER_ZONE` | `0.2` | RSI zone threshold for amber |
| `DIGEST_RSI_RED_ZONE` | `0.4` | RSI zone threshold for red |
| `DIGEST_AGE_AMBER_PCT` | `30.0` | Stuck+write_off % for amber |
| `DIGEST_AGE_RED_PCT` | `60.0` | Stuck+write_off % for red |
| `DIGEST_EXIT_AMBER_PCT` | `3.0` | Median exit distance % for amber |
| `DIGEST_EXIT_RED_PCT` | `8.0` | Median exit distance % for red |
| `DIGEST_HEADROOM_AMBER` | `50` | Open order headroom for amber |
| `DIGEST_HEADROOM_RED` | `20` | Open order headroom for red |

All configurable via environment variables.

---

## Implementation Notes

### Where It Lives

- **Module**: `signal_digest.py` (~300 lines) — pure functions for rules + dataclasses
- **Bot integration**: `bot.py` — `_run_signal_digest()` method, ~100 lines
- **HMM exposure**: `hmm_regime_detector.py` — store `last_observation` on update
- **Dashboard**: `dashboard.py` — digest panel, ~80 lines HTML/JS
- **Config**: `config.py` — `DIGEST_*` variables, ~20 lines

### Observation Access

The key change to existing code is exposing HMM observations:

```python
# hmm_regime_detector.py, in RegimeDetector.update():
if len(observations) > 0:
    self.last_macd_hist_slope = float(observations[-1, 0])
    self.last_ema_spread_pct = float(observations[-1, 1])
    self.last_rsi_zone = float(observations[-1, 2])
    self.last_volume_ratio = float(observations[-1, 3])
```

These are already computed — we're just keeping a reference to the latest
values instead of discarding them after HMM inference.

### LLM Call

Reuses the existing `_call_ai_provider()` infrastructure from the AI regime
advisor. Same provider chain (DeepSeek → Groq fallback), same threading
model (daemon thread, result queue), same rate limiting.

The digest prompt is shorter than the regime advisor prompt (~500 tokens
input vs ~800), so cost per call is lower. At one call per 10 minutes:
~144 calls/day × ~$0.0002/call = ~$0.03/day.

### Estimated Code Size

| File | Lines |
|------|-------|
| `signal_digest.py` | ~300 |
| `bot.py` additions | ~120 |
| `hmm_regime_detector.py` change | ~10 |
| `dashboard.py` additions | ~80 |
| `config.py` additions | ~25 |
| **Total** | **~535** |

---

## Priority Order of Rules

When multiple rules fire amber/red, the "top concern" should reflect the
most actionable issue. Priority (highest first):

1. `headroom` (red) — can't trade if you hit the limit
2. `regime_confidence` (red) — everything else is unreliable if this is low
3. `boundary_risk` (red) — imminent regime change affects all positions
4. `timeframe_agreement` (red) — signals conflicting
5. `macd_momentum` (red) — momentum diverging from regime
6. `rsi_zone` (red) — extreme territory
7. `ema_trend` (red) — strong trend vs grid
8. `ranger_health` (red) — active subsystem failing
9. `exit_distance` — frozen capital (informational)
10. `age_skew` — frozen capital (informational)
11. `capital_efficiency` — utilization pressure
12. `mts_trend` — overall trajectory

---

## Interaction with Existing Systems

| System | Relationship |
|--------|-------------|
| Manifold Score | Digest reads MTS as one of 12 inputs. MTS is a component, not replaced. |
| AI Regime Advisor | Separate LLM call, different purpose. Advisor says "what regime?" Digest says "what does it all mean?" |
| Action Knobs | Digest reads knob state but doesn't modify it. Digest is read-only. |
| Trade Beliefs | Digest reads belief badges for exit distance stats. |
| Throughput Sizer | Digest reads util_ratio and age_pressure. |
| Rangers | Digest monitors ranger cycling health. |

The digest is **strictly read-only**. It never modifies bot state, places
orders, or changes configuration. It is a pure diagnostic/interpretive layer.

---

## What the User Sees

### Scenario 1: Stable Range (current situation)

```
● GREEN
All signals aligned. RANGING at 95% across all timeframes.
MACD flat, RSI neutral, EMAs converged. Grid cycling conditions
are ideal. Rangers fishing for fills.

⚠ AMBER: 25/42 positions stuck from prior move (legacy issue,
not current market).
```

### Scenario 2: Regime Transition

```
● RED
MACD turning negative while HMM still says RANGING — bearish
momentum building before regime has officially shifted. 1m HMM
shows 60% BEARISH but 15m/1h still RANGING. Expect regime
downgrade within 2-3 candles. Ranger entries placed now may
orphan immediately.

Watch: If 15m flips BEARISH, full regime shift confirmed.
```

### Scenario 3: Oversold Bounce Setup

```
● AMBER
RSI at 28 (oversold) with MACD histogram bottoming. Classic
mean-reversion setup — buy exits below market likely to fill
soon. EMA spread -1.2% suggests downtrend has been strong,
but momentum is exhausting. Grid B-side entries placed now
have higher fill probability than normal.

Watch: RSI crossing back above 30 confirms reversal.
```

---

## Success Criteria

- Traffic light visible on dashboard within 1 second of page load
- Green/amber/red correctly reflects system state (manual spot-check)
- LLM interpretation updates every 10 min and reads as genuine trading analysis
- Top concern correctly identifies the most actionable issue
- No new external dependencies (uses existing LLM infra)
- Digest adds < 5ms to main loop cycle time (rule evaluation only)
- Total implementation < 600 lines
