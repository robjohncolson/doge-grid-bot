# Strategic Capital Deployment Spec

**Version**: v0.1
**Date**: 2026-02-16
**Status**: Draft — awaiting review
**Scope**: Multi-system revision — 1h HMM, AI advisor upgrade, directional accumulation, recovery deprecation, age pressure fix
**Files affected**: `bot.py`, `config.py`, `ai_advisor.py`, `hmm_regime_detector.py`, `grid_strategy.py`, `dashboard.py`, `throughput_sizer.py`

---

## 1. Problem

Three observations from live telemetry expose capital inefficiency:

1. **Idle capital**: 54.6% of USD sits unused. The grid trades small ranges but holds no mechanism to deploy excess capital directionally when macro conditions favor it.

2. **Age pressure over-throttle**: A single 36-hour-old exit at the grid edge slams `age_pressure` to its floor (0.3×), penalizing all 27 slots. The bot treats structural spread (normal lottery tickets at the edges) identically to operational congestion (exits genuinely stalling).

3. **Stale recovery orders**: 6 phantom open orders on Kraken (internal=54, Kraken=60) are likely recovery orders from the deprecated orphan-style trading. They consume open-order capacity and inflate age pressure.

Additionally:

4. **No macro trend signal**: The 1m HMM captures microstructure, the 15m captures medium swings, but neither detects bear→bull regime transitions that play out over hours to days.

5. **Free-tier AI rate limits**: The multi-provider cascade (SambaNova→Cerebras→Groq→NVIDIA) exhausts free-tier quotas, leaving the AI advisor blind during the periods it matters most.

---

## 2. Locked Decisions

1. **One spec, five changes**: 1h HMM, AI provider swap, accumulation engine, recovery deprecation, age pressure fix. All are interconnected and deploy together.
2. **1h HMM bootstraps from 15m data**: Resample existing 15m OHLCV into 1h candles on day one. No waiting for organic accumulation.
3. **DeepSeek paid API as primary**: DeepSeek-V3 (or R1 for reasoning). One free-tier fallback (Groq). No more 7-provider cascade.
4. **AI advisor gains accumulation signal**: Two-part output — regime opinion (existing) + accumulation recommendation (new).
5. **Accumulation is DCA, not lump**: Small buys spread over time, not a single market order.
6. **Acquired DOGE enters grid inventory passively**: It increases `free_doge`, enabling compounding and sell entries.
7. **Recovery order creation is disabled**: Sticky slots don't need lottery tickets. Existing stale recoveries are cancelled on startup.
8. **Age pressure uses p90 of open exit ages, not max**: One ancient outlier no longer dominates.
9. **All changes are feature-flagged**: Each subsystem has an independent enable toggle.

---

## 3. Scope

### In

1. **1h HMM** — third RegimeDetector instance, 1h OHLC fetch, tertiary consensus integration.
2. **AI advisor provider swap** — DeepSeek-V3 primary, Groq fallback, simplified cascade.
3. **AI accumulation signal** — expanded LLM output schema with `accumulation_signal` + `accumulation_conviction`.
4. **Accumulation engine** — DCA module in bot.py that converts idle USD→DOGE (or DOGE→USD) on confirmed signals.
5. **Recovery order deprecation** — disable creation of new recovery orders, cancel stale ones on startup.
6. **Age pressure fix** — use p90 age instead of max, exclude recovery orders from calculation.
7. **Dashboard updates** — 1h HMM row, accumulation status card, updated AI advisor card.
8. **Config migration** — new `ACCUM_*`, `HMM_TERTIARY_*`, `DEEPSEEK_*` vars; deprecate recovery + cascade vars.

### Out

1. Auto-scaling (throughput sizer already handles sizing signals).
2. Changes to S0/S1/S2 state machine.
3. Changes to entry/exit percent logic or exit repricing.
4. DOGE→USD accumulation (reverse direction — deferred to v0.2 after observing USD→DOGE behavior).
5. Multi-model AI council (simplified to single primary + fallback).
6. Removing `kelly_sizer.py` or old provider code from repo (keep for reference).

---

## 4. System A: 1h HMM (Tertiary Timeframe)

### 4.1 Architecture

A third `RegimeDetector` instance alongside the existing primary (1m) and secondary (15m). Uses the same `hmm_regime_detector.py` class — no changes to the detector itself.

### 4.2 Training Data

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Training window | 500 candles | ~21 days. Each 1h candle carries 60× more market info than 1m. |
| Min train samples | 150 | ~6 days. Enough for 3-state HMM with 4D observations. |
| Inference window | 30 | Last 30 hours of 1h candles for posterior. |
| Quality tiers | Shallow (<150), Baseline (150-299), Deep (300-499), Full (500+) | Proportionally smaller than 1m tiers. |

### 4.3 Bootstrap from 15m Data

On first startup (no 1h candles in Supabase):

1. Query Supabase for 15m OHLCV candles, ordered by time ascending.
2. Group into consecutive blocks of 4 (covering 1 hour each).
3. For each block: `open=first.open`, `high=max(highs)`, `low=min(lows)`, `close=last.close`, `volume=sum(volumes)`.
4. Feed resampled candles to `RegimeDetector.train()`.
5. With 1440 existing 15m candles → 360 synthetic 1h candles → well into Baseline tier on day one.

After bootstrap, organic 1h candles accumulate normally via Kraken OHLC API.

### 4.4 OHLC Fetch Cadence

1h candles only close once per hour. Fetch strategy:

- Sync interval: `HMM_TERTIARY_SYNC_INTERVAL_SEC = 3600` (once per hour).
- Rate limit cost: 1 public API call per hour. Negligible.
- State tracking: same pattern as primary/secondary (`_ohlcv_tertiary_since_cursor`, etc.).

### 4.5 Three-Timeframe Consensus

Extend `_compute_hmm_consensus()` to include the 1h signal:

**Weights** (configurable):
| Timeframe | Config var | Default | Purpose |
|-----------|-----------|---------|---------|
| 1m | `CONSENSUS_1M_WEIGHT` | 0.20 | Microstructure (tactical) |
| 15m | `CONSENSUS_15M_WEIGHT` | 0.50 | Medium swings (operational) |
| 1h | `CONSENSUS_1H_WEIGHT` | 0.30 | Macro trend (strategic) |

**Agreement matrix** (3 timeframes):

| Condition | Agreement label | Confidence treatment |
|-----------|----------------|---------------------|
| All 3 agree | `"unanimous"` | max(conf_1m, conf_15m, conf_1h) |
| 15m + 1h agree, 1m differs | `"strategic_agree"` | max(conf_15m, conf_1h) × 0.9 |
| 1m + 15m agree, 1h differs | `"tactical_agree"` | max(conf_1m, conf_15m) × 0.8 |
| 1m + 1h agree, 15m differs | `"split"` | max(conf_1m, conf_1h) × 0.7 |
| All 3 disagree | `"conflict"` | 0.0 → RANGING |

**Key behavior**: The grid machinery (entry_pct, exit pricing, throughput buckets) continues to use the existing 1m+15m consensus for tactical decisions. The 1h signal feeds into: (a) the new accumulation engine, and (b) the expanded AI advisor context.

### 4.6 Transition Detection

The 1h HMM's primary value is detecting macro regime transitions. New derived signal:

```
transition_signal = {
    "from_regime": previous_1h_regime,
    "to_regime": current_1h_regime,
    "transition_age_sec": time since regime changed,
    "confidence": 1h confidence,
    "confirmed": bool (True if held for > ACCUM_CONFIRMATION_CANDLES consecutive 1h candles)
}
```

A transition is **confirmed** when the 1h regime has been stable for `ACCUM_CONFIRMATION_CANDLES` (default: 2 = 2 hours). This filters noise from single-candle regime flips.

### 4.7 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `HMM_TERTIARY_ENABLED` | `False` | bool | Enable 1h HMM |
| `HMM_TERTIARY_INTERVAL_MIN` | `60` | int | Candle interval (minutes) |
| `HMM_TERTIARY_TRAINING_CANDLES` | `500` | int | Training window |
| `HMM_TERTIARY_MIN_TRAIN_SAMPLES` | `150` | int | Cold-start gate |
| `HMM_TERTIARY_RECENT_CANDLES` | `30` | int | Inference window |
| `HMM_TERTIARY_SYNC_INTERVAL_SEC` | `3600` | float | OHLC fetch interval |
| `CONSENSUS_1H_WEIGHT` | `0.30` | float | Weight in 3-timeframe consensus |
| `ACCUM_CONFIRMATION_CANDLES` | `2` | int | Candles to confirm 1h transition |

---

## 5. System B: AI Advisor Provider Upgrade

### 5.1 Problem with Current Cascade

The current 7-provider fallback (SambaNova DeepSeek-R1 → SambaNova DeepSeek-V3.1 → NVIDIA Kimi-K2.5 → Cerebras Qwen3-235B → Cerebras GPT-OSS-120B → Groq Llama-70B → Groq Llama-8B) has three issues:

1. **Rate exhaustion**: At 12 calls/hour, free tiers burn through daily quotas fast.
2. **Cascade complexity**: 7 providers with individual cooldown tracking and retry logic.
3. **Quality variance**: Llama-8B (last fallback) produces poor regime analysis.

### 5.2 New Provider Architecture

**Primary**: DeepSeek API (paid)
- Model: `deepseek-chat` (DeepSeek-V3) for standard calls, `deepseek-reasoner` (DeepSeek-R1) when reasoning preferred
- URL: `https://api.deepseek.com/chat/completions`
- API key: `DEEPSEEK_API_KEY` env var
- Cost: ~$0.14/M input, $0.28/M output (V3); $0.55/M input, $2.19/M output (R1)
- Estimated daily cost: $0.04-0.15/day (288 calls × ~1K tokens each)
- No rate limit wall at this volume

**Fallback**: Groq (free tier)
- Model: `llama-3.3-70b-versatile`
- Only used if DeepSeek API returns error or times out
- Existing `GROQ_API_KEY` env var

**Removed**: SambaNova, Cerebras, NVIDIA panel entries for regime advisor. (They remain available for other `ai_advisor.py` functions like `analyze_trade()` if desired.)

### 5.3 Implementation

Replace `get_regime_opinion()` internals:

1. Remove panel iteration loop.
2. Call DeepSeek directly (same OpenAI-compatible format).
3. On failure (HTTP error, timeout, parse error): call Groq Llama-70B.
4. On double failure: return default (Tier 0, symmetric, conviction 0).
5. Remove `_panelist_consecutive_fails` and `_panelist_skip_until` tracking (no longer needed with 2 providers).

Token budget unchanged (~800-1300 per call). DeepSeek handles this trivially.

### 5.4 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `DEEPSEEK_API_KEY` | `""` | str | DeepSeek API key (required for paid tier) |
| `DEEPSEEK_MODEL` | `"deepseek-chat"` | str | Model ID (V3 or R1) |
| `DEEPSEEK_TIMEOUT_SEC` | `30` | int | Request timeout |
| `AI_REGIME_PREFER_DEEPSEEK_R1` | `False` | bool | Use `deepseek-reasoner` (R1) instead of V3 |

Existing `AI_REGIME_PREFER_REASONING` config retained but reinterpreted: when True and `AI_REGIME_PREFER_DEEPSEEK_R1` also True, uses DeepSeek-R1 (reasoning model).

---

## 6. System C: Accumulation Signal (AI Output Extension)

### 6.1 Expanded LLM Output Schema

Current output:
```json
{
  "recommended_tier": 0|1|2,
  "recommended_direction": "symmetric"|"long_bias"|"short_bias",
  "conviction": 0-100,
  "rationale": "...",
  "watch_for": "...",
  "suggested_ttl_minutes": 10-60
}
```

New output (additive — existing fields unchanged):
```json
{
  "recommended_tier": 0|1|2,
  "recommended_direction": "symmetric"|"long_bias"|"short_bias",
  "conviction": 0-100,
  "rationale": "...",
  "watch_for": "...",
  "suggested_ttl_minutes": 10-60,
  "accumulation_signal": "accumulate_doge"|"hold"|"accumulate_usd",
  "accumulation_conviction": 0-100
}
```

New fields:
- `accumulation_signal`: What the AI thinks the bot should do with idle capital.
  - `"accumulate_doge"`: Buy DOGE with idle USD (expects price to rise).
  - `"hold"`: Do nothing (uncertain or stable).
  - `"accumulate_usd"`: Sell excess DOGE for USD (expects price to drop). *Deferred in v0.1 — treated as `"hold"`.*
- `accumulation_conviction`: 0-100, independent from regime conviction. How confident the AI is in the accumulation recommendation.

### 6.2 Expanded LLM Context

Add to the user prompt context:

```json
{
  "hmm": {
    "primary_1m": { ... },
    "secondary_15m": { ... },
    "tertiary_1h": {
      "regime": "BEARISH|RANGING|BULLISH",
      "confidence": 0.0-1.0,
      "bias_signal": -1.0 to +1.0,
      "probabilities": [bearish, ranging, bullish],
      "transition": {
        "from_regime": "...",
        "to_regime": "...",
        "transition_age_sec": float,
        "confirmed": bool
      }
    },
    "consensus": { ... },
    "transition_matrix_1m": [[...], ...],
    "transition_matrix_1h": [[...], ...],
    "training_quality": "...",
    "training_quality_1h": "...",
    "confidence_modifier": float
  },
  "capital": {
    "idle_usd": float,
    "idle_usd_pct": float,
    "observed_usd": float,
    "free_doge": float,
    "util_ratio": float,
    "total_profit_usd": float
  },
  "accumulation": {
    "active": bool,
    "direction": "doge"|"usd"|null,
    "spent_usd": float,
    "acquired_doge": float,
    "elapsed_sec": float
  },
  ...existing fields...
}
```

### 6.3 System Prompt Addendum

Append to `_REGIME_SYSTEM_PROMPT`:

```
You also advise on capital deployment. The bot has idle USD not committed to grid orders.
When you detect a macro regime transition (especially bear→ranging or ranging→bullish on the
1h timeframe), recommend "accumulate_doge" to buy DOGE with idle USD. When uncertain or in
established trends with no transition, recommend "hold". Your accumulation_conviction should
reflect how strongly the macro signals support deployment, independent of your tier conviction.
Consider: 1h regime stability, transition confirmation, idle capital ratio, and whether the
1m/15m tactical signals align with the 1h strategic signal.
```

---

## 7. System D: Accumulation Engine

### 7.1 Design

A DCA (Dollar Cost Average) engine that converts idle USD into DOGE when the AI advisor confirms a favorable macro transition. Lives in `bot.py` as a set of methods on the runtime, not a separate module.

### 7.2 Trigger Conditions (ALL must be true)

1. `ACCUM_ENABLED = True`
2. 1h HMM transition detected AND confirmed (held ≥ `ACCUM_CONFIRMATION_CANDLES`)
3. AI advisor `accumulation_signal == "accumulate_doge"` AND `accumulation_conviction >= ACCUM_MIN_CONVICTION`
4. `idle_usd > ACCUM_RESERVE_USD` (safety reserve not breached)
5. No active accumulation session already running
6. `capacity_band != "stop"` (Kraken order capacity available)

### 7.3 Accumulation Session Lifecycle

```
IDLE → ARMED → ACTIVE → COMPLETED
                  ↓
               STOPPED
```

**IDLE**: No accumulation. Default state.

**ARMED**: 1h HMM transition confirmed. Waiting for AI confirmation. If AI says `"hold"` for 3 consecutive polls, disarm.

**ACTIVE**: AI confirmed. DCA buying in progress.
- Places a small market buy every `ACCUM_INTERVAL_SEC` (default: 120 seconds).
- Each buy: `ACCUM_CHUNK_USD` (default: $2.00).
- Total budget: `min(ACCUM_MAX_BUDGET_USD, idle_usd - ACCUM_RESERVE_USD)`.
- Tracks: `spent_usd`, `acquired_doge`, `avg_price`, `n_buys`, `start_ts`.

**COMPLETED**: Budget exhausted or all chunks placed. Session logged. Return to IDLE.

**STOPPED**: Abort conditions met (see §7.4). Partial accumulation kept. Return to IDLE.

### 7.4 Abort Conditions (any triggers STOP)

1. 1h HMM flips back to the pre-transition regime (e.g., ranging→bearish after a bearish→ranging trigger).
2. AI advisor `accumulation_signal` changes to `"hold"` for 2 consecutive polls.
3. Price drops more than `ACCUM_MAX_DRAWDOWN_PCT` (default: 3%) from session start price.
4. `capacity_band` becomes `"stop"`.
5. Manual stop via dashboard button or API.

On STOP: no panic selling. The DOGE already acquired stays in the account and becomes available to the grid as increased `free_doge`.

### 7.5 Execution

Market buys via `kraken_client.add_order()` with `ordertype="market"`, `type="buy"`, `volume=chunk_doge`.

Where `chunk_doge = ACCUM_CHUNK_USD / current_price`.

**Rate limit**: At 1 buy per 2 minutes, this is 0.5 private calls/minute — well within Kraken limits even with grid operations.

### 7.6 Grid Integration

Acquired DOGE is **not** tracked separately. It enters the Kraken account balance, which increases `free_doge` on the next balance query. This passively:
- Increases compounding layer sizing (more DOGE available for sell entries).
- Reduces `util_ratio` in the throughput sizer (more free capital → lower utilization penalty).
- Enables the rebalancer's dynamic idle target to adjust naturally.

No changes to slot state machines, entry/exit logic, or pair fill handlers.

### 7.7 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `ACCUM_ENABLED` | `False` | bool | Master toggle |
| `ACCUM_MIN_CONVICTION` | `60` | int | Minimum AI accumulation_conviction |
| `ACCUM_RESERVE_USD` | `50.0` | float | USD safety reserve (never deploy below this) |
| `ACCUM_MAX_BUDGET_USD` | `50.0` | float | Max USD per accumulation session |
| `ACCUM_CHUNK_USD` | `2.0` | float | USD per DCA buy |
| `ACCUM_INTERVAL_SEC` | `120` | float | Seconds between DCA buys |
| `ACCUM_MAX_DRAWDOWN_PCT` | `3.0` | float | Max price drop before abort (%) |
| `ACCUM_COOLDOWN_SEC` | `3600` | float | Minimum gap between sessions (1h) |

### 7.8 State (persisted in snapshot)

```python
accum_state: str            # "idle" | "armed" | "active" | "completed" | "stopped"
accum_direction: str | None # "doge" (v0.1 only)
accum_trigger_regime: str   # 1h regime that triggered arming
accum_start_ts: float       # session start timestamp
accum_start_price: float    # price at session start
accum_spent_usd: float      # total USD spent this session
accum_acquired_doge: float  # total DOGE acquired this session
accum_n_buys: int           # number of DCA buys placed
accum_last_buy_ts: float    # timestamp of last DCA buy
accum_budget_usd: float     # computed budget for this session
accum_armed_at: float       # when ARMED state entered
accum_hold_streak: int      # consecutive AI "hold" polls (for disarm/stop)
```

---

## 8. System E: Recovery Order Deprecation

### 8.1 Background

Recovery orders were designed for the orphan-style trading strategy where exits could be stranded and needed lottery-ticket recovery orders on Kraken. With sticky slots, this mechanism is unnecessary:

- Sticky slots don't orphan exits — they hold them until filled.
- S2 break-glass handles deadlocks with repricing, not recovery orders.
- Stale recovery orders consume Kraken open-order capacity (6 of 225 slots currently).
- Recovery order ages inflate the throughput sizer's age pressure signal.

### 8.2 Changes

1. **Disable creation**: `_orphan_exit_to_recovery()` becomes a no-op when `RECOVERY_ORDERS_ENABLED=False` (new config, default `False`).
2. **Startup cleanup**: On bot startup, if `RECOVERY_ORDERS_ENABLED=False`, cancel all existing recovery orders on Kraken and clear `state.recovery_orders` for every slot.
3. **Throughput sizer**: `_collect_open_exits()` in bot.py excludes recovery orders when `RECOVERY_ORDERS_ENABLED=False`.
4. **Dashboard**: Recovery panel hidden when no recovery orders exist.
5. **Reconciliation**: `_reconcile_recovery_orders()` becomes a no-op when disabled.

### 8.3 Config

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `RECOVERY_ORDERS_ENABLED` | `False` | bool | Enable/disable recovery order creation |

### 8.4 Migration

On first startup with `RECOVERY_ORDERS_ENABLED=False`:
1. Log all recovery orders being cancelled (txid, side, price, volume, age).
2. Cancel each on Kraken via `cancel_order()`.
3. Clear `state.recovery_orders` list for each slot.
4. Save state.
5. Log summary: "Cancelled N recovery orders, freed N open-order slots."

This is a one-time cleanup. After this, no new recovery orders are created.

---

## 9. System F: Age Pressure Fix

### 9.1 Problem

Current age pressure in `throughput_sizer.py`:

```python
oldest_age = max(exit["age_sec"] for exit in open_exits)
threshold = aggregate.p75_fill_sec * TP_AGE_PRESSURE_TRIGGER
if oldest_age > threshold:
    excess = (oldest_age - threshold) / threshold
    age_pressure = max(floor, 1.0 - excess * sensitivity)
```

Using `max` means one 36-hour exit at the grid edge (normal behavior) drives `age_pressure` to the floor (0.3×) for all 27 slots.

### 9.2 Fix

Replace `max(ages)` with `p90(ages)`:

```python
ages = sorted(exit["age_sec"] for exit in open_exits)
if not ages:
    return 1.0
p90_index = int(len(ages) * 0.9)
reference_age = ages[min(p90_index, len(ages) - 1)]
```

This means:
- If 54 exits are open, the reference is the 49th oldest (ignoring the 5 most ancient).
- A few lottery tickets at the edges don't affect age pressure.
- If 90%+ of exits are old, age pressure correctly fires (genuine congestion).

### 9.3 Recovery Exclusion

When `RECOVERY_ORDERS_ENABLED=False`, recovery orders are already excluded from `_collect_open_exits()` (System E). This further reduces phantom age inflation.

### 9.4 Config

No new config vars. The fix changes the internal algorithm, not the tuning knobs. Existing `TP_AGE_PRESSURE_TRIGGER`, `TP_AGE_PRESSURE_SENSITIVITY`, and `TP_AGE_PRESSURE_FLOOR` remain and work with the new p90 reference.

---

## 10. Dashboard Updates

### 10.1 HMM Card — Third Row

Add `1h` row below existing `1m` and `15m` rows:

```
HMM Regime
  1m:  RANGING  87% conf  [progress: Full]
  15m: BEARISH  99% conf  [progress: Full]
  1h:  RANGING  72% conf  [progress: Baseline (360/500)]
  Consensus: BEARISH (strategic_agree)
```

### 10.2 Accumulation Card (new)

New card in summary panel:

```
Capital Deployment
  Status: ACTIVE (DCA buying DOGE)
  Budget: $12.00 / $50.00 spent
  Acquired: 133.2 DOGE @ avg $0.0901
  Elapsed: 14m (6 buys)
  Trigger: 1h bear→ranging + AI confirm (conv: 72)
  Drawdown: -0.3% (limit: -3%)
  [Stop] button
```

States:
- **IDLE**: "No active deployment"
- **ARMED**: "Awaiting AI confirmation (1h: bear→ranging)"
- **ACTIVE**: Full progress display with Stop button
- **COMPLETED/STOPPED**: "Last session: +133 DOGE ($12.00), stopped: price drawdown"

### 10.3 AI Advisor Card — Provider Badge

Replace panelist display with simpler provider badge:
- "DeepSeek V3" or "DeepSeek R1" (primary)
- "Groq Llama-70B (fallback)" when primary failed
- Show `accumulation_signal` alongside regime opinion

### 10.4 Throughput Sizer Card — Age Pressure Label

When age pressure < 1.0, show the reference metric:
- Before: `Age Pressure: 30%`
- After: `Age Pressure: 30% (p90 age: 2.1h)` or `Age Pressure: 100% (healthy)`

---

## 11. Status Payload Changes

### 11.1 New `accumulation` block

```json
"accumulation": {
  "enabled": true,
  "state": "active",
  "direction": "doge",
  "budget_usd": 50.0,
  "spent_usd": 12.0,
  "acquired_doge": 133.2,
  "avg_price": 0.0901,
  "n_buys": 6,
  "elapsed_sec": 840,
  "start_price": 0.0904,
  "current_drawdown_pct": -0.3,
  "max_drawdown_pct": 3.0,
  "trigger": "1h_bear_to_ranging",
  "ai_accumulation_conviction": 72,
  "armed_at": null,
  "last_session_summary": null
}
```

### 11.2 Extended `hmm_data_pipeline`

Add `tertiary_1h` block alongside existing `primary_1m` and `secondary_15m`:

```json
"hmm_data_pipeline": {
  "primary_1m": { ... },
  "secondary_15m": { ... },
  "tertiary_1h": {
    "enabled": true,
    "interval_min": 60,
    "regime": "RANGING",
    "confidence": 0.72,
    "bias_signal": 0.15,
    "probabilities": [0.12, 0.72, 0.16],
    "trained": true,
    "candle_count": 360,
    "target_candles": 500,
    "quality_tier": "baseline",
    "transition": {
      "from_regime": "BEARISH",
      "to_regime": "RANGING",
      "transition_age_sec": 7200,
      "confirmed": true
    }
  }
}
```

### 11.3 Extended `ai_regime_advisor`

Add to existing opinion block:

```json
"ai_regime_advisor": {
  ...existing fields...,
  "opinion": {
    ...existing fields...,
    "accumulation_signal": "accumulate_doge",
    "accumulation_conviction": 72,
    "provider": "deepseek-chat"
  }
}
```

### 11.4 Updated `throughput_sizer`

```json
"throughput_sizer": {
  ...existing fields...,
  "age_pressure_reference": "p90",
  "age_pressure_ref_age_sec": 7620,
  "age_pressure_excluded_recovery": 6
}
```

---

## 12. Safety Invariants

1. **Accumulation never exceeds budget**: `spent_usd` is checked before each DCA buy. Never exceeds `ACCUM_MAX_BUDGET_USD`.
2. **Accumulation never breaches reserve**: `idle_usd - spent_usd >= ACCUM_RESERVE_USD` checked before each buy.
3. **Drawdown abort**: Session stops if price drops > `ACCUM_MAX_DRAWDOWN_PCT` from start.
4. **No panic selling**: Acquired DOGE is never force-sold. It enters grid inventory.
5. **Feature flags independent**: Each system can be enabled/disabled separately. Bot functions normally with all flags off.
6. **AI failure safe**: If DeepSeek + Groq both fail, accumulation stays in ARMED (never auto-activates without AI confirmation).
7. **Recovery cleanup is idempotent**: Re-running startup cleanup on already-clean state is a no-op.
8. **Age pressure p90 graceful**: With < 10 open exits, p90 ≈ max (degradation toward old behavior, not a cliff).
9. **1h HMM trains independently**: Failure to train 1h HMM doesn't affect 1m/15m consensus or grid operation.
10. **Accumulation session bounded**: Cooldown (`ACCUM_COOLDOWN_SEC`) prevents rapid repeated sessions.

---

## 13. Integration Points

### 13.1 bot.py

| Area | Change |
|------|--------|
| Constructor | Add `_hmm_detector_tertiary`, `_hmm_state_tertiary`, accumulation state fields |
| `_init_hmm_runtime()` | Initialize tertiary detector + state (same pattern as secondary) |
| `_sync_ohlcv_candles()` | Add tertiary 1h fetch (hourly cadence) |
| `_update_hmm()` | Update tertiary detector, populate `_regime_history_1h` |
| `_compute_hmm_consensus()` | 3-timeframe weighted consensus with expanded agreement matrix |
| `_build_ai_regime_context()` | Add tertiary HMM state + capital metrics + accumulation state |
| `_process_ai_regime_pending_result()` | Extract `accumulation_signal` + `accumulation_conviction` |
| New: `_update_accumulation()` | State machine for IDLE→ARMED→ACTIVE→COMPLETED/STOPPED |
| New: `_execute_accum_buy()` | Place DCA market buy via kraken_client |
| Main loop | Call `_update_accumulation()` each cycle |
| Snapshot save/restore | Persist/restore accumulation state |
| Status payload | Add `accumulation` block |
| Startup | Recovery order cleanup when `RECOVERY_ORDERS_ENABLED=False` |

### 13.2 ai_advisor.py

| Area | Change |
|------|--------|
| `get_regime_opinion()` | Replace cascade with DeepSeek primary + Groq fallback |
| `_REGIME_SYSTEM_PROMPT` | Add accumulation guidance paragraph |
| Response parsing | Extract `accumulation_signal` + `accumulation_conviction` (default to `"hold"` / 0 if missing) |
| Remove | `_panelist_consecutive_fails`, `_panelist_skip_until` tracking (unused with 2 providers) |

### 13.3 config.py

| Area | Change |
|------|--------|
| HMM section | Add `HMM_TERTIARY_*` vars (8 new) |
| AI section | Add `DEEPSEEK_*` vars (4 new) |
| New section | Add `ACCUM_*` vars (8 new) |
| Recovery section | Add `RECOVERY_ORDERS_ENABLED` (1 new) |

### 13.4 throughput_sizer.py

| Area | Change |
|------|--------|
| `_compute_age_pressure()` | Replace `max(ages)` with p90 percentile |
| `status_payload()` | Add `age_pressure_reference`, `age_pressure_ref_age_sec` |

### 13.5 grid_strategy.py

| Area | Change |
|------|--------|
| `_orphan_exit_to_recovery()` | Guard with `RECOVERY_ORDERS_ENABLED` check |

### 13.6 dashboard.py

| Area | Change |
|------|--------|
| HMM card HTML | Add 1h row |
| New card HTML | Accumulation status card |
| AI advisor card | Provider badge, accumulation signal display |
| Throughput card | Age pressure reference label |
| JS renderers | Update for new payload fields |

---

## 14. Testing

### Unit Tests

| # | Test | System |
|---|------|--------|
| 1 | 1h HMM trains from bootstrapped 15m data | A |
| 2 | 1h OHLC resample produces correct OHLC from 15m groups | A |
| 3 | 3-timeframe consensus: unanimous agreement | A |
| 4 | 3-timeframe consensus: strategic_agree (15m+1h agree, 1m differs) | A |
| 5 | 3-timeframe consensus: conflict (all disagree) → RANGING | A |
| 6 | 1h transition detection and confirmation after N candles | A |
| 7 | DeepSeek call succeeds, returns valid opinion + accumulation signal | B |
| 8 | DeepSeek fails, Groq fallback succeeds | B |
| 9 | Both fail, returns safe default (hold, conviction 0) | B |
| 10 | LLM output includes accumulation_signal and accumulation_conviction | C |
| 11 | Missing accumulation fields in LLM output default to hold/0 | C |
| 12 | Accumulation session: IDLE → ARMED on 1h transition | D |
| 13 | Accumulation session: ARMED → ACTIVE on AI confirmation | D |
| 14 | Accumulation session: ACTIVE → STOPPED on drawdown breach | D |
| 15 | Accumulation session: ACTIVE → STOPPED on 1h regime revert | D |
| 16 | Accumulation session: ACTIVE → COMPLETED on budget exhaustion | D |
| 17 | Accumulation respects reserve floor | D |
| 18 | Accumulation cooldown prevents rapid re-entry | D |
| 19 | Recovery orders cancelled on startup when disabled | E |
| 20 | Recovery creation is no-op when disabled | E |
| 21 | Age pressure uses p90 instead of max | F |
| 22 | Age pressure p90 with < 10 exits degrades gracefully | F |

### Integration Tests

| # | Test | Systems |
|---|------|---------|
| 23 | Status payload includes `accumulation` block | D |
| 24 | Status payload includes `tertiary_1h` in HMM pipeline | A |
| 25 | Accumulation snapshot round-trip preserves state | D |
| 26 | Full pipeline: 1h transition → AI confirm → DCA buy → budget exhaust | A+C+D |

---

## 15. Rollout

### Stage 1: 1h HMM + Age Pressure Fix (low risk)

Enable:
- `HMM_TERTIARY_ENABLED=True`
- Age pressure p90 fix (no flag — always active after deploy)
- `RECOVERY_ORDERS_ENABLED=False` (cancel stale recoveries)

Observe:
- 1h HMM training progress (expect Baseline within 24h from bootstrap)
- Age pressure values (should increase from 0.3 toward 1.0 after recovery cleanup)
- Open order drift (should resolve to 0 after recovery cleanup)
- 48h observation period

### Stage 2: AI Provider Swap (medium risk)

Enable:
- `DEEPSEEK_API_KEY=<key>`
- AI advisor now calls DeepSeek instead of free cascade

Observe:
- Response quality (rationale coherence)
- Latency (should be faster than free tiers)
- `accumulation_signal` outputs (observe-only, engine still disabled)
- 24h observation period

### Stage 3: Accumulation Engine (higher risk)

Enable:
- `ACCUM_ENABLED=True`
- Conservative settings: `ACCUM_MAX_BUDGET_USD=20`, `ACCUM_CHUNK_USD=1.0`, `ACCUM_MAX_DRAWDOWN_PCT=2.0`

Observe:
- First ARMED event (does 1h transition trigger correctly?)
- First ACTIVE session (does AI confirmation gate work?)
- DCA execution (orders placed at correct intervals and sizes?)
- Abort conditions (does drawdown stop work?)
- 72h observation, then gradually increase budget

### Stage 4: Tuning

- Adjust `ACCUM_CONFIRMATION_CANDLES` based on 1h HMM noise profile
- Adjust `ACCUM_MIN_CONVICTION` based on AI signal quality
- Adjust consensus weights based on observed agreement patterns
- Consider enabling `accumulate_usd` direction (v0.2)

---

## 16. Rollback

Each system rolls back independently:

| System | Rollback |
|--------|----------|
| 1h HMM | `HMM_TERTIARY_ENABLED=False` — tertiary ignored, consensus reverts to 2-timeframe |
| AI provider | Remove `DEEPSEEK_API_KEY` — falls back to Groq only (degrade, not fail) |
| Accumulation | `ACCUM_ENABLED=False` — engine idle, no DCA activity |
| Recovery | `RECOVERY_ORDERS_ENABLED=True` — re-enables creation (cancelled orders stay cancelled) |
| Age pressure | Code revert only — swap p90 back to max in `throughput_sizer.py` |

No state migration needed for any rollback. All changes are additive.
