# AI Regime Advisor Spec v0.1

Version: v0.1
Date: 2026-02-15
Status: Design draft
Depends on: ai_advisor.py (council pattern), HMM regime detector, Directional
Regime Spec, Multi-Timeframe HMM Spec

---

## 1. Problem

The HMM regime detector produces a regime classification (BEARISH / RANGING /
BULLISH), confidence score, bias signal, and transition matrix. This feeds
into a rigid rule cascade:

```
confidence thresholds → tier gates → fixed blend weights → grid bias
```

The rules work, but leave interpretive value on the table:

1. **No narrative reasoning.** The rule engine can't express "1m just flipped
   bullish while 15m is still ranging but trending up — this looks like an
   early reversal." It just sees `agreement: 1m_cooling` and dampens.

2. **No cross-signal synthesis.** The bot has HMM regimes, directional trend
   signals (Section 12.5), fill-rate patterns, recovery order state, Kelly
   regime-tagged edge estimates, and capacity telemetry. No rule combines
   all of these.

3. **Transition matrix is ignored.** The trained transition matrix reveals
   regime persistence (P(bear→bear) = 0.98 → sticky bear) but no rule
   inspects it. An LLM can naturally reason about "we just entered bearish
   and historically it sticks."

4. **Rigid thresholds.** Tier 1 at 0.20, Tier 2 at 0.50 — these are
   hand-tuned constants. Market structure changes; the thresholds don't.

**Proposal:** Add an LLM-based "second opinion" layer that periodically
interprets the full signal landscape and, when it disagrees with the
mechanical tier conclusion, surfaces its reasoning and a one-click override
button in the dashboard.

---

## 2. Design Principles

1. **Advisory, not autonomous.** The LLM never changes bot behavior on its
   own. It produces a recommendation. The operator (or a future auto-accept
   policy) chooses to apply it. Default: manual approval via dashboard button.

2. **Mechanical tier is always the baseline.** If the LLM is down, slow, or
   produces garbage, the bot continues on its mechanical tier. Zero
   degradation.

3. **Disagreement is the interesting case.** When LLM agrees with the tier
   system, there's nothing to do — log it and move on. When it disagrees,
   that's when the dashboard lights up with reasoning and an override option.

4. **Structured output.** The LLM returns a constrained JSON schema, not
   free-form text. Reasoning is captured in a `rationale` field for display,
   but the actionable fields are enums/numbers.

5. **Reuse existing infrastructure.** Same council pattern as `ai_advisor.py`.
   Same Groq/NVIDIA panelists. Same timeout/skip/cooldown logic. New function,
   not new module.

6. **Rate-conscious.** Runs every 5 minutes (configurable), not every 30s
   cycle. Also triggers on regime transitions. Total: ~12-15 LLM calls/hour
   at most.

---

## 3. Terminology

| Term | Meaning |
|------|---------|
| **Mechanical tier** | The tier (0/1/2) computed by the existing rule engine from HMM confidence |
| **AI opinion** | The LLM's recommended tier + direction + rationale |
| **Agreement** | AI opinion matches mechanical tier and direction |
| **Disagreement** | AI opinion differs on tier, direction, or both |
| **Override** | Operator accepts the AI opinion, temporarily replacing the mechanical tier |
| **Override TTL** | How long an override stays active before reverting to mechanical (default: 30 min) |
| **Conviction** | AI's self-rated confidence in its opinion (0-100) |

---

## 4. Architecture

```
Every AI_REGIME_INTERVAL_SEC (300s) or on tier transition:
                                │
                                ▼
                ┌───────────────────────────────┐
                │     _build_regime_context()    │
                │                               │
                │  HMM state (1m + 15m)         │
                │  Transition matrix            │
                │  Consensus agreement mode     │
                │  Directional trend signal     │
                │  Recent fill rates            │
                │  Recovery order count         │
                │  Kelly edge per regime        │
                │  Capacity headroom            │
                │  Training quality tier        │
                │  Recent regime history (30m)  │
                └───────────┬───────────────────┘
                            │
                            ▼
                ┌───────────────────────────────┐
                │   ai_advisor.get_regime_opinion()  │
                │                               │
                │   Council query (same as      │
                │   get_recommendation pattern)  │
                │                               │
                │   Prompt + structured output   │
                └───────────┬───────────────────┘
                            │
                            ▼
                ┌───────────────────────────────┐
                │    Compare vs mechanical tier  │
                │                               │
                │    agree? → log, display       │
                │    disagree? → surface override │
                └───────────┬───────────────────┘
                            │
                            ▼
                ┌───────────────────────────────┐
                │    Dashboard: AI Regime Card   │
                │                               │
                │    Shows: AI opinion, reason,  │
                │    agreement/disagreement      │
                │    badge, override button      │
                └───────────────────────────────┘
```

Key property: the existing `_update_regime_tier()` flow is unchanged.
The AI opinion sits *beside* it, not inside it. An active override
temporarily replaces the tier value that `_update_regime_tier()` published,
but the mechanical computation still runs (for comparison and auto-revert).

---

## 5. LLM Prompt & Output Schema

### 5.1 System Prompt

```
You are a regime analyst for a DOGE/USD grid trading bot. You receive
technical signals from a Hidden Markov Model (3-state: BEARISH, RANGING,
BULLISH) running on two timeframes (1-minute and 15-minute), plus
operational metrics. Your job is to interpret these signals holistically
and recommend a trading posture.

The bot uses a 3-tier system:
- Tier 0 (Symmetric): Both sides trade equally. Default/safe.
- Tier 1 (Asymmetric): Favor one side with spacing bias.
- Tier 2 (Aggressive): Suppress the against-trend side entirely.

You should recommend a tier and direction based on ALL available signals,
not just the HMM regime label. Consider:
- Whether the 1m and 15m timeframes agree or are converging
- The transition matrix (how sticky is the current regime?)
- Operational signals (fill rates, recovery orders, capacity)
- Whether confidence is rising or falling over recent history

Be conservative. Tier 2 is rarely appropriate. When uncertain, recommend
Tier 0 (symmetric).

Answer with JSON only.
```

### 5.2 User Prompt (constructed by `_build_regime_context()`)

```json
{
  "hmm": {
    "primary_1m": {
      "regime": "BULLISH",
      "confidence": 0.42,
      "bias_signal": 0.35,
      "probabilities": [0.08, 0.21, 0.71]
    },
    "secondary_15m": {
      "regime": "RANGING",
      "confidence": 0.15,
      "bias_signal": 0.05,
      "probabilities": [0.28, 0.44, 0.28]
    },
    "consensus": {
      "agreement": "1m_cooling",
      "effective_regime": "RANGING",
      "effective_confidence": 0.08,
      "effective_bias": 0.03
    },
    "transition_matrix_1m": [
      [0.95, 0.03, 0.02],
      [0.04, 0.91, 0.05],
      [0.02, 0.03, 0.95]
    ],
    "training_quality": "deep",
    "confidence_modifier": 0.95
  },
  "regime_history_30m": [
    {"ts": 1739616000, "regime": "RANGING", "conf": 0.12},
    {"ts": 1739616300, "regime": "RANGING", "conf": 0.18},
    {"ts": 1739616600, "regime": "BULLISH", "conf": 0.35},
    {"ts": 1739616900, "regime": "BULLISH", "conf": 0.42}
  ],
  "mechanical_tier": {
    "current": 0,
    "direction": "symmetric",
    "since": 1739615700
  },
  "operational": {
    "directional_trend": "bullish",
    "trend_detected_at": 1739615000,
    "fill_rate_1h": 12,
    "recovery_order_count": 2,
    "capacity_headroom": 45,
    "capacity_band": "normal",
    "kelly_edge_bullish": 0.032,
    "kelly_edge_bearish": -0.008,
    "kelly_edge_ranging": 0.015
  }
}
```

### 5.3 Required Output Schema

```json
{
  "recommended_tier": 0,
  "recommended_direction": "symmetric",
  "conviction": 65,
  "rationale": "1m HMM is showing early bullish momentum (confidence rising 0.12→0.42 over 30 min) but 15m is still flat. The transition matrix shows bullish states are sticky (P=0.95), suggesting this could develop. However, Kelly edge for bullish is only 3.2% and 15m hasn't confirmed. Wait for 15m convergence before upgrading to Tier 1.",
  "watch_for": "15m confidence crossing 0.20 with bullish bias would confirm the move."
}
```

| Field | Type | Constraints |
|-------|------|-------------|
| `recommended_tier` | int | 0, 1, or 2 |
| `recommended_direction` | string | `"symmetric"`, `"long_bias"`, `"short_bias"` |
| `conviction` | int | 0–100. Higher = more confident in recommendation. |
| `rationale` | string | 1–3 sentences. Human-readable reasoning. |
| `watch_for` | string | 1 sentence. What would change this recommendation. |

### 5.4 Parsing & Validation

Same JSON extraction as `_parse_response()` (find `{...}` in response).
Additional validation:

- `recommended_tier` must be 0, 1, or 2. Default to 0 if missing/invalid.
- `recommended_direction` must be one of the three enum values. Default to
  `"symmetric"`.
- `conviction` clamped to 0–100. Default to 0.
- `rationale` truncated to 500 chars. Default to empty string.
- `watch_for` truncated to 200 chars. Default to empty string.

If JSON parsing fails entirely, the opinion is discarded (logged as
`"parse_error"`). Mechanical tier continues unaffected.

---

## 6. Trigger Logic

### 6.1 Periodic

Run every `AI_REGIME_INTERVAL_SEC` (default: 300 seconds / 5 minutes).

### 6.2 Event-Triggered

Also run immediately (debounced by 60s minimum gap) when:

1. **Mechanical tier changes** — e.g. Tier 0 → Tier 1. The AI should
   evaluate whether it agrees with the transition.
2. **Consensus agreement mode changes** — e.g. `full` → `conflict`. This
   is a significant signal shift.

### 6.3 Rate Limiting

- Minimum 60 seconds between calls (hard debounce).
- Maximum ~15 calls/hour under normal conditions.
- If all panelists are in cooldown (consecutive failures), skip entirely.

---

## 7. Disagreement Detection & Override

### 7.1 Agreement Classification

Compare AI opinion to mechanical tier:

| AI Tier | AI Direction | Mech Tier | Mech Direction | Classification |
|---------|-------------|-----------|----------------|---------------|
| 0 | symmetric | 0 | symmetric | `agree` |
| 1 | long_bias | 1 | long_bias | `agree` |
| 1 | long_bias | 0 | symmetric | `ai_upgrade` |
| 0 | symmetric | 1 | long_bias | `ai_downgrade` |
| 1 | long_bias | 1 | short_bias | `ai_flip` |
| 2 | short_bias | 1 | short_bias | `ai_upgrade` |
| ... | ... | ... | ... | ... |

Simplified:
- Same tier AND same direction → `agree`
- AI tier > mechanical → `ai_upgrade`
- AI tier < mechanical → `ai_downgrade`
- Same tier but different direction → `ai_flip`

### 7.2 Override Lifecycle

```
 [AI disagrees]
      │
      ▼
 Dashboard shows:
   ┌──────────────────────────────────────────────┐
   │  AI Regime Advisor              ⚡ DISAGREES  │
   │                                               │
   │  Mechanical: Tier 0 (Symmetric)               │
   │  AI Opinion: Tier 1 Long Bias (72% conviction)│
   │                                               │
   │  "1m bullish momentum building, 15m starting  │
   │   to turn. Transition matrix shows sticky     │
   │   bull (P=0.95). Kelly edge positive."         │
   │                                               │
   │  Watch: "15m crossing 0.20 confidence"         │
   │                                               │
   │  [ Apply Override (30m) ]  [ Dismiss ]         │
   └──────────────────────────────────────────────┘
```

**Apply Override** button:
1. Sets `ai_override_tier`, `ai_override_direction`, `ai_override_until`
   on the runtime state.
2. `_update_regime_tier()` checks for an active override. If present and
   not expired, uses the override tier/direction instead of the mechanical
   computation.
3. Override expires at `ai_override_until` (now + `AI_OVERRIDE_TTL_SEC`,
   default 1800 = 30 minutes).
4. Dashboard shows countdown timer on active override.
5. Operator can cancel override early via a "Revert to Mechanical" button.

**Dismiss** button:
- Hides the disagreement card until the next AI opinion cycle.
- Logs the dismissal.

### 7.3 Override Safety Rails

| Guard | Rule |
|-------|------|
| **Conviction floor** | Override button only enabled if AI conviction >= `AI_OVERRIDE_MIN_CONVICTION` (default: 50) |
| **No Tier 2 from Tier 0** | AI cannot recommend jumping straight from Tier 0 to Tier 2. Max one tier hop per override. |
| **TTL hard cap** | Override TTL cannot exceed `AI_OVERRIDE_MAX_TTL_SEC` (default: 3600 = 1 hour) |
| **Capacity gate** | If `capacity_band == "stop"`, suppress all upgrades (AI or mechanical) |
| **One at a time** | New override replaces any existing override (no stacking) |

---

## 8. Status Payload

Add to the bot status JSON (`/api/status`):

```json
"ai_regime_advisor": {
  "enabled": true,
  "last_run_ts": 1739616900,
  "last_run_age_sec": 45,
  "next_run_in_sec": 255,

  "opinion": {
    "recommended_tier": 1,
    "recommended_direction": "long_bias",
    "conviction": 72,
    "rationale": "1m bullish momentum building...",
    "watch_for": "15m crossing 0.20 confidence",
    "panelist": "Llama-70B",
    "agreement": "ai_upgrade"
  },

  "override": {
    "active": false,
    "tier": null,
    "direction": null,
    "applied_at": null,
    "expires_at": null,
    "remaining_sec": null,
    "source_conviction": null
  },

  "history": [
    {
      "ts": 1739616900,
      "mechanical_tier": 0,
      "ai_tier": 1,
      "ai_direction": "long_bias",
      "conviction": 72,
      "agreement": "ai_upgrade",
      "action": "pending"
    },
    {
      "ts": 1739616600,
      "mechanical_tier": 0,
      "ai_tier": 0,
      "ai_direction": "symmetric",
      "conviction": 55,
      "agreement": "agree",
      "action": "none"
    }
  ]
}
```

`history` is a rolling window of the last 12 opinions (~1 hour at 5-min
intervals). Useful for seeing how the AI's view has evolved.

---

## 9. Dashboard UI

### 9.1 AI Regime Card (New)

Placed in the HMM section of the detail view, below the existing HMM
status card.

**When agreeing:**
```
┌──────────────────────────────────────────────┐
│  AI Regime Advisor                  ✓ AGREES │
│                                              │
│  Both mechanical and AI: Tier 0 (Symmetric)  │
│  Conviction: 55%  │  Next check: 4m 15s      │
│                                              │
│  "Low confidence across both timeframes.     │
│   No clear directional signal."              │
└──────────────────────────────────────────────┘
```

Color: muted/neutral (same as other info cards).

**When disagreeing:**
```
┌──────────────────────────────────────────────┐
│  AI Regime Advisor              ⚡ DISAGREES  │
│                                              │
│  Mechanical: Tier 0 (Symmetric)              │
│  AI Opinion: Tier 1 Long Bias                │
│  Conviction: 72%  │  Next check: 4m 15s      │
│                                              │
│  "1m bullish momentum building, 15m starting │
│   to turn. Transition matrix shows sticky    │
│   bull (P=0.95). Kelly edge positive."       │
│                                              │
│  Watch: "15m crossing 0.20 confidence"       │
│                                              │
│  [ Apply Override (30m) ]    [ Dismiss ]     │
└──────────────────────────────────────────────┘
```

Color: amber/highlight border to draw attention.

**When override is active:**
```
┌──────────────────────────────────────────────┐
│  AI Regime Advisor          🔶 OVERRIDE ACTIVE│
│                                              │
│  Override: Tier 1 Long Bias (AI, 72%)        │
│  Mechanical would be: Tier 0 Symmetric       │
│  Expires in: 24m 30s                         │
│                                              │
│  [ Revert to Mechanical ]                    │
└──────────────────────────────────────────────┘
```

Color: orange/active border.

### 9.2 Override API Endpoints

| Method | Path | Body | Effect |
|--------|------|------|--------|
| POST | `/api/ai-regime/override` | `{"ttl_sec": 1800}` | Apply current AI opinion as override |
| DELETE | `/api/ai-regime/override` | — | Cancel active override, revert to mechanical |
| POST | `/api/ai-regime/dismiss` | — | Dismiss current disagreement from dashboard |

All endpoints gated by the existing API lock mechanism.

---

## 10. Council Query Details

### 10.1 Which Panelists

Reuse `_build_panel()` from `ai_advisor.py`. Same Groq + NVIDIA roster.
But for regime analysis:

- **Prefer a single panelist** (first available), not full council vote.
  Rationale: the AI regime advisor is an interpretive layer, not a
  democratic vote. One thoughtful response is better than three that need
  aggregation. Council voting makes sense for "what action to take" but
  not for "interpret these signals."

- **Panelist preference order:** Reasoning model first (Kimi K2.5 if
  available, as chain-of-thought is valuable for signal interpretation),
  then Llama-70B, then Llama-8B.

- **Fallback:** If the preferred panelist fails, try the next in order.
  If all fail, skip this cycle (mechanical tier unaffected).

### 10.2 Token Budget

- System prompt: ~250 tokens
- User context: ~400 tokens (the JSON payload)
- Response: ~150 tokens (structured JSON with rationale)
- Reasoning overhead (Kimi K2.5): ~500 tokens additional
- **Total per call: ~800-1300 tokens**

At 12 calls/hour on Groq free tier: ~10,000-16,000 tokens/hour.
Well within Groq's free tier limits (14,400 tokens/min for Llama-70B).

### 10.3 Timeout

- Reasoning models: 30 seconds
- Instruct models: 15 seconds

If timeout is hit, skip this cycle.

---

## 11. Config

| Parameter | Default | Type | Notes |
|-----------|---------|------|-------|
| `AI_REGIME_ADVISOR_ENABLED` | False | bool | Master toggle |
| `AI_REGIME_INTERVAL_SEC` | 300 | float | Periodic check interval |
| `AI_REGIME_DEBOUNCE_SEC` | 60 | float | Minimum gap between calls |
| `AI_OVERRIDE_TTL_SEC` | 1800 | int | Default override duration (30 min) |
| `AI_OVERRIDE_MAX_TTL_SEC` | 3600 | int | Maximum override duration (1 hour) |
| `AI_OVERRIDE_MIN_CONVICTION` | 50 | int | Min conviction to enable override button |
| `AI_REGIME_HISTORY_SIZE` | 12 | int | Rolling opinion history count |
| `AI_REGIME_PREFER_REASONING` | True | bool | Prefer reasoning model (Kimi) when available |

All gated behind `AI_REGIME_ADVISOR_ENABLED`. When False, zero LLM calls
are made for regime analysis. Existing `get_recommendation()` (periodic AI
market analysis) is unaffected and runs on its own schedule.

---

## 12. State Persistence

### In-Memory (resets on restart)

- `_ai_regime_last_run_ts`: float
- `_ai_regime_opinion`: dict (latest parsed opinion)
- `_ai_regime_history`: deque (rolling 12 opinions)
- `_ai_override_tier`: int or None
- `_ai_override_direction`: str or None
- `_ai_override_until`: float or None
- `_ai_override_applied_at`: float or None

### Persisted (state.json / Supabase)

- `ai_override_tier`, `ai_override_direction`, `ai_override_until`:
  persisted so an override survives a restart within its TTL.
- `ai_regime_history`: NOT persisted (recalculated after restart).

On load, if `ai_override_until < now`, the override is expired and cleared.

---

## 13. Logging

```
AI regime advisor: querying Kimi-K2.5...
AI regime advisor: Tier 1 long_bias (conviction 72) — mechanical is Tier 0 symmetric
AI regime advisor: DISAGREES — rationale: "1m bullish momentum building..."
AI regime advisor: override applied — Tier 1 long_bias, expires in 1800s
AI regime advisor: override expired, reverting to mechanical Tier 0 symmetric
AI regime advisor: override cancelled by operator
AI regime advisor: agrees with mechanical Tier 0 symmetric (conviction 55)
AI regime advisor: panelist Kimi-K2.5 failed (timeout), trying Llama-70B...
AI regime advisor: all panelists failed, skipping cycle
```

---

## 14. Files Modified

| File | Change | Est. Lines |
|------|--------|------------|
| `config.py` | 8 new env vars | ~12 |
| `ai_advisor.py` | `get_regime_opinion()`, `_build_regime_context()`, `_parse_regime_opinion()` | ~120 |
| `bot.py` | Call `get_regime_opinion()` on schedule, override state, 3 API endpoints, persist override | ~80 |
| `dashboard.py` | AI Regime Advisor card (agree/disagree/override states), JS for buttons | ~100 |

Total: ~312 lines changed/added.

---

## 15. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| LLM hallucinates "strong signal" from noise | Medium | Conviction floor (50) + max 1-tier hop + TTL expiry |
| Non-determinism (same inputs, different outputs) | High (inherent) | Advisory only; operator confirms. Log all opinions for audit. |
| Groq/NVIDIA outage | Low-Medium | Graceful skip; mechanical tier unaffected |
| Operator trusts AI blindly | Medium | Override TTL forces re-evaluation. Dashboard always shows mechanical baseline. |
| Latency spike delays main loop | Low | Async/threaded call with timeout. Main loop doesn't block on AI. |
| Token cost on paid tier | Low | ~10K tokens/hour. Negligible even on paid Groq. |

---

## 16. Future Considerations (Out of Scope)

1. **Auto-accept policy.** When conviction >= 85 AND agreement with
   directional trend AND training quality = Full, auto-apply the override
   without operator click. Requires confidence in the LLM's calibration.
   Deferred until we have enough logged opinions to evaluate accuracy.

2. **Opinion accuracy tracking.** Log each AI opinion + the actual market
   outcome 30 minutes later. Build a hit-rate metric. Use it to calibrate
   conviction thresholds. Requires completed-cycle outcome attribution.

3. **Multi-model regime council.** Run 2-3 LLMs and require majority
   agreement before surfacing a disagreement. More robust but higher
   latency and token cost. Consider if single-model accuracy is
   insufficient.

4. **Prompt tuning from outcomes.** Feed the LLM its past opinions and
   their outcomes, letting it self-correct. Requires opinion accuracy
   tracking (item 2) as a prerequisite.

5. **Voice/notification alerts.** Push disagreement notifications to
   phone/desktop when the dashboard isn't open. Useful for overnight
   operation.

---

## 17. Implementation Milestones

| Phase | Scope | Depends On |
|-------|-------|------------|
| P0 | Config vars + `get_regime_opinion()` + `_build_regime_context()` in ai_advisor.py | Nothing |
| P1 | Bot integration: schedule, override state, API endpoints | P0 |
| P2 | Dashboard: AI Regime card (agree/disagree/override), JS handlers | P1 |
| P3 | Override persistence (state.json + Supabase) | P1 |
| P4 | Testing + prompt tuning with live data | P2 + P3 |
