# AI-Suggested Override TTL — Implementation Plan

**Spec:** `docs/AI_SUGGESTED_TTL_SPEC.md` v0.1.0
**Date:** 2026-02-16

---

## Steps

### Step 1: Update system prompt

**File:** `ai_advisor.py:89-113`

Add 6th field to the JSON schema in `_REGIME_SYSTEM_PROMPT`:

```python
'- "suggested_ttl_minutes": 10-60, how long you expect this regime to persist. '
'Consider: timeframe agreement (both align = longer), transition matrix stickiness '
'(high self-transition = longer), conviction trend (rising = longer), and signal '
'noise. 15 means "short-lived or uncertain signal", 45+ means "strong convergent trend".'
```

### Step 2: Update `_parse_regime_opinion()`

**File:** `ai_advisor.py:701-734`

After building the `opinion` dict (line 727-733), add:

```python
"suggested_ttl_minutes": _safe_int(parsed.get("suggested_ttl_minutes"), 0, 0, 60),
```

### Step 3: Update `_default_regime_opinion()`

**File:** `ai_advisor.py:350-359`

Add `"suggested_ttl_minutes": 0` to the returned dict.

### Step 4: Add `AI_OVERRIDE_MIN_TTL_SEC` config

**File:** `config.py:~253` (AI override section)

```python
AI_OVERRIDE_MIN_TTL_SEC: int = _env("AI_OVERRIDE_MIN_TTL_SEC", 300, int)  # 5 min floor
```

### Step 5: Thread `suggested_ttl_minutes` through opinion processing

**File:** `bot.py:3184-3236` (`_process_ai_regime_pending_result`)

After extracting `watch_for` (line 3198), add:

```python
suggested_ttl_minutes = max(0, min(60, int(opinion.get("suggested_ttl_minutes", 0) or 0)))
```

Add `"suggested_ttl_minutes": suggested_ttl_minutes` to the `self._ai_regime_opinion` dict at line 3222.

### Step 6: Compute `suggested_ttl_sec` in status payload

**File:** `bot.py:3864-3877` (`_ai_regime_status_payload`)

After `default_ttl_sec` (line 3868), compute:

```python
ai_ttl_min = int(opinion_payload.get("suggested_ttl_minutes", 0) or 0)
min_ttl = max(1, int(getattr(config, "AI_OVERRIDE_MIN_TTL_SEC", 300)))
if ai_ttl_min > 0:
    suggested_ttl = max(min_ttl, min(max_ttl, ai_ttl_min * 60))
else:
    suggested_ttl = default_ttl
```

Add `"suggested_ttl_sec": suggested_ttl` to the returned dict.

### Step 7: Update TTL floor in `apply_ai_regime_override()`

**File:** `bot.py:3049-3058`

Change `max(1, min(use_ttl, max_ttl))` to:

```python
min_ttl = max(1, int(getattr(config, "AI_OVERRIDE_MIN_TTL_SEC", 300)))
use_ttl = max(min_ttl, min(use_ttl, max_ttl))
```

### Step 8: Update dashboard Apply button text

**File:** `dashboard.py:1678`

```js
// Before:
aiApplyBtn.textContent = `Apply Override (${fmtAgeSeconds(defaultTtlSec)})`;

// After:
const suggestedTtl = ai.suggested_ttl_sec || defaultTtlSec;
aiApplyBtn.textContent = `Apply Override (${fmtAgeSeconds(suggestedTtl)})`;
```

### Step 9: Update dashboard Apply click handler

**File:** `dashboard.py:2378-2381`

```js
// Before:
const ttl = Number(ai.default_ttl_sec || 1800);

// After:
const ttl = Number(ai.suggested_ttl_sec || ai.default_ttl_sec || 1800);
```

### Step 10: Add tests

**File:** `tests/test_ai_regime_advisor.py`

4 new tests:

1. `test_parse_regime_opinion_suggested_ttl_present` — value 25 → parsed as 25
2. `test_parse_regime_opinion_suggested_ttl_missing` — omitted → 0
3. `test_parse_regime_opinion_suggested_ttl_clamped` — 120 → 60, -5 → 0
4. `test_default_regime_opinion_has_suggested_ttl` — default includes `suggested_ttl_minutes: 0`

### Step 11: Update `.env.example`

Add entry:

```
AI_OVERRIDE_MIN_TTL_SEC=300    # Minimum override duration (seconds, default 5 min)
```

## Verification

1. `python -m pytest tests/test_ai_regime_advisor.py -v` — all pass
2. `python -m pytest tests/ -v` — no regressions
3. Deploy → dashboard Apply button shows AI-suggested duration
4. Click Apply → log line shows matching TTL
5. AI omits field → button falls back to "30m"
