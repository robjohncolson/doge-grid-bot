# AI Regime Advisor — Model Upgrade & Conviction Fix

**Version:** 0.1.0
**Status:** Draft
**Affects:** `ai_advisor.py` (panelist config, system prompt, token limits, context builder)

---

## 1. Problem Statement

### 1.1 Conviction Always 0%

The AI regime advisor returns `conviction: 0` in production even when the signal
landscape is clear (both HMMs agree RANGING at 98%+). The dashboard shows:

```
Status AGREES, Mechanical Tier 0 symmetric, AI Opinion Tier 0 symmetric,
Conviction 0% (Llama-70B), Next Check ~2.6m
```

**Root cause:** The system prompt tells the LLM to "recommend a tier and
direction" and "when uncertain, recommend Tier 0 (symmetric)." The LLM
interprets conviction as *"how much should the bot deviate from default"*
rather than *"how confident am I in my assessment."* When the correct answer
IS the default (tier 0 symmetric in a ranging market), the model says
conviction = 0 — meaning "no need to change anything."

This makes the conviction signal useless: it's always 0 when the market is
ranging (the most common state), and operators can't distinguish "confident
it's ranging" from "I have no idea."

### 1.2 Weak Model

The current primary Groq panelist is `llama-3.3-70b-versatile` (70B params,
280 tokens/sec). Groq now serves **GPT-OSS-120B** — a 120B-parameter model
at 500 tokens/sec. This is nearly 2x the parameters and actually faster.

### 1.3 Tight Token Budget

`_INSTRUCT_MAX_TOKENS = 200` (ai_advisor.py:62) limits non-reasoning models
to 200 output tokens. A well-formed regime opinion JSON with rationale is
~80-120 tokens. This leaves minimal room for the model to reason before
outputting JSON, increasing the risk of shallow or truncated responses.

---

## 2. Design Goals

| Goal | Priority |
|------|----------|
| Conviction reflects confidence in the assessment, not urgency to change | P0 |
| Upgrade to strongest available Groq production model | P0 |
| Give models enough output tokens for quality responses | P0 |
| Pass consensus probabilities to LLM for better context | P1 |
| Maintain fallback chain resilience | P1 |
| Zero new config knobs | P1 |
| No changes to override mechanics, TTL, or safety rails | P0 |

### Non-Goals

- Changing the AI regime advisor architecture (worker thread, scheduler, etc.)
- Adding new providers or API endpoints.
- Modifying override application rules or conviction floor.
- Making model selection configurable via env vars (premature — hardcode first).

---

## 3. Changes

### 3.1 Upgrade `GROQ_PANELISTS` (ai_advisor.py:51-54)

**Current:**
```python
GROQ_PANELISTS = [
    ("Llama-70B", "llama-3.3-70b-versatile", False),
    ("Llama-8B", "llama-3.1-8b-instant", False),
]
```

**New:**
```python
GROQ_PANELISTS = [
    ("GPT-OSS-120B", "openai/gpt-oss-120b", False),
    ("Llama-70B", "llama-3.3-70b-versatile", False),
    ("Llama-8B", "llama-3.1-8b-instant", False),
]
```

GPT-OSS-120B becomes the primary panelist. Llama-70B demotes to first
fallback. Llama-8B remains last resort.

**Groq model comparison:**

| Model | Params | Speed | Context | Status |
|-------|--------|-------|---------|--------|
| `openai/gpt-oss-120b` | 120B | 500 tps | 131K | Production |
| `llama-3.3-70b-versatile` | 70B | 280 tps | 131K | Production |
| `openai/gpt-oss-20b` | 20B | 1000 tps | 131K | Production |
| `llama-3.1-8b-instant` | 8B | 560 tps | 131K | Production |

Preview models (Llama 4 Maverick, Qwen3-32B, Kimi K2) are excluded — Groq
marks them "evaluation purposes only."

### 3.2 Update `_ordered_regime_panel()` Priority (ai_advisor.py:548-565)

**Current priority dicts:**
```python
if prefer_reasoning:
    priority = {"Kimi-K2.5": 0, "Llama-70B": 1, "Llama-8B": 2}
else:
    priority = {"Llama-70B": 0, "Llama-8B": 1, "Kimi-K2.5": 2}
```

**New:**
```python
if prefer_reasoning:
    priority = {"Kimi-K2.5": 0, "GPT-OSS-120B": 1, "Llama-70B": 2, "Llama-8B": 3}
else:
    priority = {"GPT-OSS-120B": 0, "Llama-70B": 1, "Llama-8B": 2, "Kimi-K2.5": 3}
```

When `AI_REGIME_PREFER_REASONING=True` (default), Kimi-K2.5 (NVIDIA,
reasoning model) still gets first shot. GPT-OSS-120B is the preferred
instruct model. When reasoning is not preferred, GPT-OSS-120B leads.

### 3.3 Raise `_INSTRUCT_MAX_TOKENS` (ai_advisor.py:62)

**Current:**
```python
_INSTRUCT_MAX_TOKENS = 200
```

**New:**
```python
_INSTRUCT_MAX_TOKENS = 400
```

The hard cap in `_call_panelist_messages()` (line 570) is already
`min(max_tokens, 512)`, so 400 stays under the cap. This gives instruct
models ~2x the output budget for reasoning + JSON.

Reasoning models keep `_REASONING_MAX_TOKENS = 2048` — unchanged.

### 3.4 Fix Conviction Semantics in System Prompt (ai_advisor.py:74-89)

**Current `_REGIME_SYSTEM_PROMPT`:**
```
You are a regime analyst for a DOGE/USD grid trading bot. You receive
technical signals from a Hidden Markov Model (3-state: BEARISH,
RANGING, BULLISH) running on two timeframes (1-minute and 15-minute),
plus operational metrics. Your job is to interpret these signals
holistically and recommend a trading posture.

The bot uses a 3-tier system:
- Tier 0 (Symmetric): Both sides trade equally. Default/safe.
- Tier 1 (Asymmetric): Favor one side with spacing bias.
- Tier 2 (Aggressive): Suppress the against-trend side entirely.

Recommend a tier and direction using ALL signals. Consider timeframe
agreement/convergence, transition matrix stickiness, operational
signals, and whether confidence is rising/falling over recent history.
Be conservative. Tier 2 is rare. When uncertain, recommend Tier 0
(symmetric). Return JSON only.
```

**New `_REGIME_SYSTEM_PROMPT`:**
```
You are a regime analyst for a DOGE/USD grid trading bot. You receive
technical signals from a Hidden Markov Model (3-state: BEARISH,
RANGING, BULLISH) running on two timeframes (1-minute and 15-minute),
plus operational metrics. Your job is to interpret these signals
holistically and recommend a trading posture.

The bot uses a 3-tier system:
- Tier 0 (Symmetric): Both sides trade equally. Default/safe.
- Tier 1 (Asymmetric): Favor one side with spacing bias.
- Tier 2 (Aggressive): Suppress the against-trend side entirely.

Recommend a tier and direction using ALL signals. Consider timeframe
agreement/convergence, transition matrix stickiness, consensus
probabilities, operational signals, and whether confidence is
rising/falling over recent history. Be conservative. Tier 2 is rare.
When uncertain, recommend Tier 0 (symmetric).

Return ONLY a JSON object with these fields:
- "recommended_tier": 0, 1, or 2
- "recommended_direction": "symmetric", "long_bias", or "short_bias"
- "conviction": 0-100, your confidence in the ASSESSMENT (not urgency
  to change). 80 means "I'm quite sure this is the right posture."
  Even Tier 0 can have high conviction when signals clearly confirm
  ranging. 0 means "I cannot read these signals at all."
- "rationale": brief explanation (1-2 sentences)
- "watch_for": what would change your mind (1 sentence)
```

**Key changes:**
1. Explicit conviction definition: "confidence in the ASSESSMENT, not
   urgency to change"
2. Example that Tier 0 can have high conviction
3. Defines what 0 means ("cannot read signals") vs what 80 means
4. Mentions consensus probabilities as a signal to consider
5. Explicit JSON schema so the model knows what fields to return

### 3.5 Pass Consensus Probabilities to LLM Context (ai_advisor.py:498-511)

The `_build_regime_context()` sanitized consensus block currently includes:
- `agreement`
- `effective_regime`
- `effective_confidence`
- `effective_bias`

**Add `consensus_probabilities`:**

```python
"consensus": {
    "agreement": ...,
    "effective_regime": ...,
    "effective_confidence": ...,
    "effective_bias": ...,
    "consensus_probabilities": _sanitize_probabilities(
        consensus.get("consensus_probabilities")
    ),
},
```

This gives the LLM the blended probability distribution (e.g.,
`[0.013, 0.984, 0.003]`) which is much more informative than a single
regime label. The `_sanitize_probabilities()` helper already exists at
line 356 and handles dict/list/missing formats.

---

## 4. Behavioral Analysis

### 4.1 Conviction Change

| Scenario | Current | After fix |
|----------|---------|-----------|
| Both RANGING 98%, tier 0 recommended | conviction: 0 ("nothing to change") | conviction: 70-90 ("confident it's ranging") |
| Clear BULLISH, tier 1 recommended | conviction: 50-70 | conviction: 60-80 (similar, slightly higher with better prompt) |
| Mixed signals, tier 0 recommended | conviction: 0-20 | conviction: 20-40 ("somewhat sure, hedging") |
| Total disagreement, can't read signals | conviction: 0-10 | conviction: 0-10 (genuinely uncertain) |

The key shift: conviction now has a meaningful range in the RANGING state
(the most common state), making the dashboard signal useful to operators.

### 4.2 Model Upgrade Impact

GPT-OSS-120B (120B params) should produce:
- More nuanced rationale text
- Better interpretation of transition matrix stickiness
- More calibrated conviction scores
- Lower hallucination risk on the structured JSON schema

Fallback behavior is unchanged — if GPT-OSS-120B fails (rate limit, timeout),
the system falls to Llama-70B, then Llama-8B. The skip/cooldown logic
(lines 64-68) applies per-panelist.

### 4.3 Token Budget Impact

With `_INSTRUCT_MAX_TOKENS = 400`:
- Typical response: ~100-200 tokens (JSON + rationale)
- Budget headroom: ~200 tokens for model reasoning before JSON output
- No cost impact (Groq free tier has no per-token billing)
- Slightly longer response time: negligible at 500 tps (0.4s for 200 extra tokens)

### 4.4 Override Mechanics

**Unchanged.** The conviction value feeds into the existing override
application rules:
- `AI_OVERRIDE_MIN_CONVICTION = 50` (config.py:251)
- Override can only be applied manually via dashboard button
- Button is gated by conviction >= min_conviction

With the prompt fix, a confident RANGING assessment (conviction ~80) now
*could* theoretically be overridden — but since the mechanical tier already
agrees (tier 0), the dashboard shows "AGREES" and the override button is
moot. The conviction is purely informational in the agreement case.

---

## 5. Edge Cases

### 5.1 GPT-OSS-120B Not Available on Free Tier

If Groq restricts GPT-OSS-120B to paid tiers, `_call_panelist_messages()`
returns an HTTP 4xx error. The fallback chain catches this and tries
Llama-70B. The skip/cooldown mechanism (3 consecutive failures → 1 hour
cooldown) prevents hammering a broken endpoint.

### 5.2 Model Returns Old-Style Conviction

If GPT-OSS-120B ignores the prompt refinement and still returns
conviction: 0 for RANGING, the behavior is no worse than today. The prompt
change is a best-effort improvement, not a hard contract.

### 5.3 Consensus Probabilities Missing (Old State)

If the bot is running an older version where `consensus_probabilities` isn't
in the status payload, `consensus.get("consensus_probabilities")` returns
`None`, and `_sanitize_probabilities(None)` returns `[0.0, 1.0, 0.0]`
(default RANGING). Graceful degradation.

### 5.4 Very Long Rationale Eats Token Budget

With 400 tokens, a verbose model could write a long rationale and push
`watch_for` past the limit. `_parse_regime_opinion()` clips `rationale` to
500 chars and `watch_for` to 200 chars, so downstream storage is bounded.
The model might truncate its own output, but `rfind("}")` in the parser
handles partial JSON gracefully — it finds the last valid closing brace.

---

## 6. Testing Plan

| Test | Description |
|------|-------------|
| `test_groq_panelists_includes_gpt_oss` | Verify GPT-OSS-120B is first in GROQ_PANELISTS |
| `test_ordered_panel_prefer_reasoning` | Kimi-K2.5 first, then GPT-OSS-120B, then Llama-70B |
| `test_ordered_panel_prefer_instruct` | GPT-OSS-120B first when prefer_reasoning=False |
| `test_instruct_max_tokens_400` | Verify _INSTRUCT_MAX_TOKENS == 400 |
| `test_regime_prompt_conviction_definition` | Prompt contains "confidence in the ASSESSMENT" |
| `test_regime_context_has_consensus_probs` | `_build_regime_context()` output includes consensus_probabilities |
| `test_consensus_probs_missing_defaults` | Missing consensus_probabilities → [0.0, 1.0, 0.0] |

### Integration Verification (Post-Deploy)

After enabling, observe 3-5 advisor cycles in the dashboard:
1. Conviction should be non-zero when signals are clear (RANGING at 98%+)
2. Panelist should show "GPT-OSS-120B" (not "Llama-70B")
3. Rationale text should reference consensus probabilities
4. No increase in parse failures (check logs for "invalid JSON" warnings)

---

## 7. Files Changed

| File | Change | Lines |
|------|--------|-------|
| `ai_advisor.py` | Add GPT-OSS-120B to GROQ_PANELISTS | ~2 |
| `ai_advisor.py` | Update priority dicts in `_ordered_regime_panel()` | ~4 |
| `ai_advisor.py` | Change `_INSTRUCT_MAX_TOKENS` from 200 to 400 | ~1 |
| `ai_advisor.py` | Rewrite `_REGIME_SYSTEM_PROMPT` with conviction definition | ~15 |
| `ai_advisor.py` | Add `consensus_probabilities` to `_build_regime_context()` | ~3 |
| `tests/test_ai_regime_advisor.py` | Add 7 tests per Section 6 | ~60 |

**Total delta:** ~25 lines in ai_advisor.py + ~60 lines tests.

---

## 8. Rollout

This is a non-breaking improvement. No staged rollout needed.

1. **Deploy** — all changes are to the advisor layer only; no trading logic
   affected.
2. **Observe** — watch the next 3-5 advisor cycles in the dashboard:
   - Panelist should show "GPT-OSS-120B"
   - Conviction should be non-zero for clear signals
   - If GPT-OSS-120B fails, fallback to Llama-70B should be seamless
3. **If GPT-OSS-120B consistently fails** — remove it from GROQ_PANELISTS
   and revert to Llama-70B as primary. No other rollback needed.
