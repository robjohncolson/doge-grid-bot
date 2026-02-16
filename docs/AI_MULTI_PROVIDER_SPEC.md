# AI Regime Advisor — Multi-Provider Redundancy

**Version:** 0.1.0
**Status:** Draft
**Affects:** `ai_advisor.py`, `config.py`, `.env.example`, `tests/test_ai_regime_advisor.py`

---

## 1. Problem Statement

The AI regime advisor currently relies on two providers:

| Provider | Models | Status |
|----------|--------|--------|
| Groq | GPT-OSS-120B, Llama-70B, Llama-8B | GPT-OSS-120B fails (likely paid-only); Llama-70B works |
| NVIDIA | Kimi-K2.5 | Intermittent failures |

When GPT-OSS-120B and Kimi-K2.5 both fail, the advisor falls back to
Llama-70B (70B params) — the weakest viable model. Meanwhile, free-tier
providers now serve models up to **685B parameters** with OpenAI-compatible
endpoints:

- **SambaNova** (`api.sambanova.ai`): DeepSeek-R1 (685B reasoning),
  DeepSeek-V3.1 (685B instruct), free API key
- **Cerebras** (`api.cerebras.ai`): Qwen3-235B (235B MoE),
  GPT-OSS-120B (120B), free tier 1M tokens/day

Adding these providers gives access to much stronger models AND
cross-provider redundancy so no single outage kills the advisor.

### Pre-Existing Bug: Reasoning Token Cap

`_call_panelist_messages()` (line 589) hard-caps all output tokens at 512:

```python
max_tokens = min(int(panelist.get("max_tokens", _INSTRUCT_MAX_TOKENS)), 512)
```

Reasoning models are configured with `max_tokens=2048` but actually only
get 512. This has been silently affecting Kimi-K2.5 and will be fatal for
DeepSeek-R1, which uses extensive chain-of-thought (~300-500 tokens)
before outputting the JSON answer (~100-150 tokens). At 512 tokens,
the JSON frequently gets truncated.

### DeepSeek-R1 `<think>` Tag Handling

DeepSeek-R1 wraps chain-of-thought in `<think>...</think>` tags. If the
reasoning mentions JSON or curly braces (common when the model is thinking
about what to output), the `_parse_regime_opinion()` JSON extractor
(`find("{")` / `rfind("}")`) could grab text from inside the think block,
causing parse failures.

---

## 2. Design Goals

| Goal | Priority |
|------|----------|
| Rank all models by quality, strongest first | P0 |
| Natural fallback from strongest to weakest across providers | P0 |
| No single provider failure kills the advisor | P0 |
| Fix reasoning model token cap | P0 |
| Handle DeepSeek-R1 `<think>` tags safely | P0 |
| New providers wire in identically to existing Groq/NVIDIA pattern | P1 |
| Zero changes to override mechanics, safety rails, or dashboard | P1 |

### Non-Goals

- Changing the advisor scheduling, worker thread, or override lifecycle.
- Adding provider-selection config knobs (hardcoded ranking is simpler).
- Using preview/evaluation-only models (production-grade only).
- Per-provider rate limit tracking (existing skip/cooldown handles this).

---

## 3. Model Ranking

All models ranked by capability, with provider assignment optimized
for cross-provider distribution:

### 3.1 Complete Ranked Chain

| Rank | Model | Provider | Params | Type | Context |
|------|-------|----------|--------|------|---------|
| 1 | DeepSeek-R1-0528 | SambaNova | 685B (MoE) | Reasoning | 128K |
| 2 | Kimi-K2.5 | NVIDIA | — | Reasoning | 128K |
| 3 | DeepSeek-V3.1 | SambaNova | 685B (MoE) | Instruct | 128K |
| 4 | Qwen3-235B-A22B | Cerebras | 235B (MoE) | Instruct | 8K* |
| 5 | GPT-OSS-120B | Cerebras | 120B | Instruct | 8K* |
| 6 | Llama-70B | Groq | 70B | Instruct | 131K |
| 7 | Llama-8B | Groq | 8B | Instruct | 131K |

*Cerebras free tier limits context to 8K. Our regime payload is ~2.5K
tokens total (system prompt + context JSON), well within this limit.

**Provider distribution:** 4 providers, no adjacent ranks share a provider
(except SambaNova at ranks 1+3, which are reasoning vs instruct).

### 3.2 Fallback Behavior by `PREFER_REASONING` Setting

**`AI_REGIME_PREFER_REASONING=True` (default):**

```
DeepSeek-R1 → Kimi-K2.5 → DeepSeek-V3.1 → Qwen3-235B → GPT-OSS-120B → Llama-70B → Llama-8B
(SambaNova)   (NVIDIA)     (SambaNova)     (Cerebras)    (Cerebras)      (Groq)       (Groq)
```

**`AI_REGIME_PREFER_REASONING=False`:**

```
DeepSeek-V3.1 → Qwen3-235B → GPT-OSS-120B → Llama-70B → Llama-8B → DeepSeek-R1 → Kimi-K2.5
(SambaNova)     (Cerebras)    (Cerebras)      (Groq)       (Groq)     (SambaNova)   (NVIDIA)
```

### 3.3 Failure Scenarios

| Outage | Result |
|--------|--------|
| SambaNova down | Falls to Kimi-K2.5 (reasoning) or Qwen3-235B (instruct) |
| Cerebras down | Falls to Llama-70B (Groq) |
| Groq down | DeepSeek or Qwen3 already served the request |
| NVIDIA down | DeepSeek-R1 already serves reasoning; no impact on instruct |
| SambaNova + Cerebras down | Still have Groq (Llama-70B) + NVIDIA (Kimi) |
| All except Groq down | Llama-70B serves, same as today |

---

## 4. Implementation

### 4.1 New Config Vars (`config.py`)

```python
SAMBANOVA_API_KEY: str = _env("SAMBANOVA_API_KEY", "")
CEREBRAS_API_KEY: str = _env("CEREBRAS_API_KEY", "")
```

### 4.2 New Provider Constants (`ai_advisor.py`)

```python
SAMBANOVA_URL = "https://api.sambanova.ai/v1/chat/completions"
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"

SAMBANOVA_PANELISTS = [
    ("DeepSeek-R1", "DeepSeek-R1-0528", True),
    ("DeepSeek-V3.1", "DeepSeek-V3.1", False),
]

CEREBRAS_PANELISTS = [
    ("Qwen3-235B", "qwen-3-235b-a22b-instruct-2507", False),
    ("GPT-OSS-120B", "gpt-oss-120b", False),
]
```

### 4.3 Updated `_build_panel()` (`ai_advisor.py`)

Add SambaNova and Cerebras blocks following the existing Groq/NVIDIA pattern:

```python
def _build_panel() -> list:
    panel = []

    if config.SAMBANOVA_API_KEY:
        for name, model, reasoning in SAMBANOVA_PANELISTS:
            panel.append({
                "name": name,
                "url": SAMBANOVA_URL,
                "model": model,
                "key": config.SAMBANOVA_API_KEY,
                "reasoning": reasoning,
                "max_tokens": _REASONING_MAX_TOKENS if reasoning else _INSTRUCT_MAX_TOKENS,
            })

    if config.GROQ_API_KEY:
        for name, model, reasoning in GROQ_PANELISTS:
            panel.append(...)  # unchanged

    if config.NVIDIA_API_KEY:
        for name, model, reasoning in NVIDIA_PANELISTS:
            panel.append(...)  # unchanged

    if config.CEREBRAS_API_KEY:
        for name, model, reasoning in CEREBRAS_PANELISTS:
            panel.append({
                "name": name,
                "url": CEREBRAS_URL,
                "model": model,
                "key": config.CEREBRAS_API_KEY,
                "reasoning": reasoning,
                "max_tokens": _REASONING_MAX_TOKENS if reasoning else _INSTRUCT_MAX_TOKENS,
            })

    # Legacy fallback unchanged
    if not panel and config.AI_API_KEY:
        ...
    return panel
```

### 4.4 Updated `_ordered_regime_panel()` Priority Maps

```python
if prefer_reasoning:
    priority = {
        "DeepSeek-R1": 0,
        "Kimi-K2.5": 1,
        "DeepSeek-V3.1": 2,
        "Qwen3-235B": 3,
        "GPT-OSS-120B": 4,
        "Llama-70B": 5,
        "Llama-8B": 6,
    }
else:
    priority = {
        "DeepSeek-V3.1": 0,
        "Qwen3-235B": 1,
        "GPT-OSS-120B": 2,
        "Llama-70B": 3,
        "Llama-8B": 4,
        "DeepSeek-R1": 5,
        "Kimi-K2.5": 6,
    }
```

### 4.5 Fix Reasoning Token Cap (`_call_panelist_messages`, line 589)

**Current (broken):**
```python
max_tokens = min(int(panelist.get("max_tokens", _INSTRUCT_MAX_TOKENS)), 512)
```

**Fixed:**
```python
cap = _REASONING_MAX_TOKENS if panelist.get("reasoning") else 512
max_tokens = min(int(panelist.get("max_tokens", _INSTRUCT_MAX_TOKENS)), cap)
```

This gives:
- Instruct models: capped at 512 (unchanged behavior)
- Reasoning models: capped at 2048 (was silently clipped to 512)

### 4.6 Strip `<think>` Tags Before JSON Parse

DeepSeek-R1 wraps chain-of-thought in `<think>...</think>` tags. If the
reasoning contains curly braces, the JSON extractor could mis-parse.

Add stripping at the top of `_parse_regime_opinion()`:

```python
def _parse_regime_opinion(response: str) -> tuple:
    if not response:
        return ({}, "empty_response")

    stripped = response.strip()

    # Strip <think>...</think> blocks from reasoning model output
    think_end = stripped.rfind("</think>")
    if think_end >= 0:
        stripped = stripped[think_end + len("</think>"):].strip()

    json_start = stripped.find("{")
    json_end = stripped.rfind("}")
    ...  # rest unchanged
```

This takes everything after the last `</think>` tag, which is where
reasoning models place their final answer. If no think tags are present
(instruct models), the string is unchanged.

### 4.7 Increase Reasoning Model Timeout

DeepSeek-R1 with 2048 output tokens at ~200 tps needs ~10 seconds just
for generation. The current 30-second timeout for reasoning models is
adequate but tight with network overhead. Keep it at 30 seconds — if
SambaNova is consistently slower, bump to 45 in a follow-up.

### 4.8 `.env.example` Additions

```env
# --- AI provider API keys ---
# SambaNova (free tier): DeepSeek R1 + V3.1 (strongest models)
SAMBANOVA_API_KEY=
# Cerebras (free tier, 1M tokens/day): Qwen3-235B + GPT-OSS-120B
CEREBRAS_API_KEY=
```

### 4.9 Updated `get_regime_opinion()` Docstring

```python
def get_regime_opinion(context: dict) -> dict:
    """
    Query a single preferred panelist for regime interpretation.

    Fallback order (when PREFER_REASONING=True):
      1) DeepSeek-R1 (SambaNova, reasoning)
      2) Kimi-K2.5 (NVIDIA, reasoning)
      3) DeepSeek-V3.1 (SambaNova, instruct)
      4) Qwen3-235B (Cerebras, instruct)
      5) GPT-OSS-120B (Cerebras, instruct)
      6) Llama-70B (Groq, instruct)
      7) Llama-8B (Groq, instruct)

    Returns a validated dict and never raises.
    """
```

### 4.10 Updated Module Docstring and `_build_panel()` Comment

```
SUPPORTED PROVIDERS:
  - SambaNova (free tier): DeepSeek R1 + DeepSeek V3.1
  - Cerebras (free tier): Qwen3-235B + GPT-OSS-120B
  - Groq (free tier): GPT-OSS-120B + Llama 3.3 70B + Llama 3.1 8B
  - NVIDIA build.nvidia.com (free tier): Kimi K2.5
  - Any OpenAI-compatible endpoint (legacy single-model fallback)

  Set SAMBANOVA_API_KEY, CEREBRAS_API_KEY, GROQ_API_KEY,
  and/or NVIDIA_API_KEY to enable panelists.
  Panel auto-configures based on which keys are available.
```

### 4.11 Updated `config.py` Startup Summary

The startup banner (config.py ~line 911) should report all four provider
keys:

```python
f"  SambaNova key:   {'configured' if SAMBANOVA_API_KEY else 'NOT SET'}",
f"  Cerebras key:    {'configured' if CEREBRAS_API_KEY else 'NOT SET'}",
f"  Groq key:        {'configured' if GROQ_API_KEY else 'NOT SET'}",
f"  NVIDIA key:      {'configured' if NVIDIA_API_KEY else 'NOT SET'}",
```

---

## 5. Edge Cases

### 5.1 Only Some API Keys Configured

`_build_panel()` only adds panelists for providers with non-empty keys.
If the user only has Groq + SambaNova keys, the panel includes those
providers' models. `_ordered_regime_panel()` sorts whatever panel exists
by the priority map. Models from unconfigured providers simply don't
appear — no errors, no empty slots.

### 5.2 DeepSeek-R1 Response Has No `</think>` Tag

If a reasoning model returns content without `<think>` tags (some providers
strip them), `rfind("</think>")` returns -1 and the stripping is skipped.
Response is parsed normally. No behavioral change for non-DeepSeek models.

### 5.3 DeepSeek-R1 Uses All Tokens on Reasoning

With 2048 tokens, the model could spend 1900 tokens reasoning and only
148 on the answer. If the JSON is truncated, `json.loads()` fails,
`_parse_regime_opinion()` returns a parse error, and the fallback chain
tries the next panelist. Self-healing.

### 5.4 Cerebras Free Tier 8K Context Limit

Our regime prompt is ~2.5K tokens (system + context JSON). With 400-512
tokens of output, total is ~3K — well within the 8K limit. If context
ever grows beyond 8K, Cerebras returns an HTTP 400, caught as a panelist
failure, falls through to Groq (131K context).

### 5.5 SambaNova Rate Limits

SambaNova free tier has rate limits (exact numbers TBD from live use).
If rate-limited, the HTTP 429 is caught as `http_429`, triggers the
skip/cooldown mechanism (3 consecutive failures → 1 hour skip). The
chain falls to the next provider seamlessly.

### 5.6 Reasoning Content in Separate Field

Some providers return DeepSeek-R1 reasoning in `reasoning_content` (not
in `content`). Our code at line 612-614 already handles this:
```python
content = msg.get("content")
if not content:
    content = msg.get("reasoning_content")
```

If `content` has the final answer only and `reasoning_content` has the
chain-of-thought, we get clean content without think tags. The `<think>`
stripping is a safety net for providers that concatenate everything
into `content`.

---

## 6. Testing Plan

| Test | Description |
|------|-------------|
| `test_sambanova_panelists_defined` | SAMBANOVA_PANELISTS has DeepSeek-R1 (reasoning) + DeepSeek-V3.1 (instruct) |
| `test_cerebras_panelists_defined` | CEREBRAS_PANELISTS has Qwen3-235B + GPT-OSS-120B (both instruct) |
| `test_build_panel_all_providers` | With all 4 API keys set, panel contains 7 panelists |
| `test_build_panel_partial_keys` | With only GROQ key, panel has 3 Groq panelists only |
| `test_ordered_panel_full_reasoning_chain` | Full 7-model chain in correct order |
| `test_ordered_panel_full_instruct_chain` | Full 7-model instruct-first order |
| `test_reasoning_token_cap_not_clipped` | Reasoning panelist gets max_tokens=2048 (not 512) |
| `test_instruct_token_cap_preserved` | Instruct panelist gets max_tokens capped at 512 |
| `test_think_tag_stripping` | Response with `<think>...{...}...</think>{json}` parses correctly |
| `test_think_tag_absent` | Response without think tags parses normally (no regression) |
| `test_reasoning_content_fallback` | Empty content + populated reasoning_content → uses reasoning_content |

### Integration Verification (Post-Deploy)

1. Check bot startup banner shows all 4 provider keys as "configured"
2. First advisor cycle should show DeepSeek-R1 or DeepSeek-V3.1 as panelist
3. If SambaNova fails, subsequent cycles should show Cerebras or Groq models
4. Monitor logs for `http_429` or `http_4xx` from new providers (rate limits)

---

## 7. Files Changed

| File | Change | Est. Lines |
|------|--------|------------|
| `config.py` | Add `SAMBANOVA_API_KEY`, `CEREBRAS_API_KEY`; update startup banner | ~6 |
| `.env.example` | Add SambaNova + Cerebras key entries | ~5 |
| `ai_advisor.py` | Add provider URLs + panelist tuples | ~12 |
| `ai_advisor.py` | Extend `_build_panel()` with SambaNova + Cerebras blocks | ~20 |
| `ai_advisor.py` | Update `_ordered_regime_panel()` priority maps (7 entries each) | ~16 |
| `ai_advisor.py` | Fix reasoning token cap in `_call_panelist_messages()` | ~2 |
| `ai_advisor.py` | Add `<think>` tag stripping in `_parse_regime_opinion()` | ~4 |
| `ai_advisor.py` | Update module docstring, `_build_panel()` comment, `get_regime_opinion()` docstring | ~15 |
| `tests/test_ai_regime_advisor.py` | Add 11 tests per Section 6 | ~120 |

**Total delta:** ~80 lines in production code + ~120 lines tests.

---

## 8. Rollout

1. **Set env vars** — add `SAMBANOVA_API_KEY` and `CEREBRAS_API_KEY` to
   the deployment environment (Railway dashboard or `.env`).
2. **Deploy** — code changes are advisor-only; no trading logic affected.
3. **Observe first 3-5 cycles:**
   - Panelist should show "DeepSeek-R1" or "DeepSeek-V3.1"
   - Conviction should remain meaningful (non-zero for clear signals)
   - Rationale should be more detailed (685B model advantage)
4. **If SambaNova/Cerebras fail consistently:**
   - Check API keys are valid
   - Check free tier rate limits
   - System auto-degrades to Groq (Llama-70B) — same as today
5. **No rollback needed** — worst case is the same Llama-70B behavior
   the bot already has today.
