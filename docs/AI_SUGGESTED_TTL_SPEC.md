# AI-Suggested Override TTL — Spec

**Version:** 0.1.0
**Date:** 2026-02-16
**Status:** Draft

---

## 1. Problem

When the AI regime advisor disagrees with the mechanical HMM tier, the user can click "Apply Override" to adopt the AI's recommendation. Currently the override always uses a fixed 30-minute TTL (`AI_OVERRIDE_TTL_SEC=1800`). This is suboptimal:

- A high-conviction opinion backed by converging timeframes (both 1m and 15m agree, high self-transition stickiness) should persist longer — it's likely to remain valid for 45-60 minutes.
- A borderline call during choppy, divergent signals should expire sooner — the regime may flip within 10-15 minutes.
- The AI already receives all the information needed to estimate duration (timeframe agreement, transition matrices, conviction trends, operational metrics) but has no way to communicate it.

## 2. Solution

Add a `suggested_ttl_minutes` field to the AI's JSON response schema. The AI estimates how long it expects the recommended regime to hold based on the signal dynamics it observes. The value is displayed on the dashboard Apply button and sent to the backend when the user clicks it.

### Non-goals

- **No auto-apply.** The user still decides whether to apply the override. This feature only changes the *duration* shown on the button, not whether the override is applied automatically.
- **No dynamic re-evaluation.** Once applied, the override TTL is fixed. It doesn't shorten or extend based on subsequent signals (that's a separate future feature).
- **No per-direction TTL.** The suggested TTL applies to the entire override, not separately to tier vs. direction.

## 3. AI Prompt Changes

### 3.1 New field in JSON schema

Add to `_REGIME_SYSTEM_PROMPT` (`ai_advisor.py:104-112`):

```
- "suggested_ttl_minutes": 10-60, how long you expect this regime to persist.
  Consider: timeframe agreement (both align = longer), transition matrix
  stickiness (high self-transition probability = longer), conviction trend
  (rising = longer), and signal noise/divergence (more noise = shorter).
  15 means "short-lived or uncertain signal."
  45+ means "strong convergent trend with high stickiness."
```

### 3.2 Guidance heuristics (embedded in prompt)

The AI should weigh:

| Factor | Effect on TTL |
|--------|--------------|
| Both timeframes agree on regime | +10-15 min |
| High self-transition probability (>0.8) | +10 min |
| Conviction rising over recent history | +5-10 min |
| Single timeframe signal, other ambiguous | -10 min |
| Transition matrix shows regime instability | -10-15 min |
| Conviction declining or volatile | -5-10 min |

These are guidelines for the AI, not hard rules. The AI produces a single integer.

## 4. Data Flow

```
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐     ┌───────────┐
│  AI Panelist     │────▶│ Parse        │────▶│ Bot opinion  │────▶│ Status    │
│  JSON response   │     │ _parse_      │     │ storage      │     │ payload   │
│  {               │     │ regime_      │     │ _ai_regime_  │     │ suggested │
│   ...,           │     │ opinion()    │     │ opinion{}    │     │ _ttl_sec  │
│   suggested_ttl_ │     │ extracts &   │     │ stores raw   │     │ clamped   │
│   minutes: 25    │     │ clamps 0-60  │     │ minutes      │     │ to config │
│  }               │     │              │     │              │     │ bounds    │
└─────────────────┘     └──────────────┘     └──────────────┘     └─────┬─────┘
                                                                        │
                                                                        ▼
                                                                  ┌───────────┐
                                                                  │ Dashboard │
                                                                  │ "Apply    │
                                                                  │ Override  │
                                                                  │ (25m)"    │
                                                                  └─────┬─────┘
                                                                        │ click
                                                                        ▼
                                                                  ┌───────────┐
                                                                  │ POST /api │
                                                                  │ ttl_sec=  │
                                                                  │ 1500      │
                                                                  └───────────┘
```

## 5. Clamping & Fallback

| Parameter | Config key | Default | Purpose |
|-----------|-----------|---------|---------|
| Floor | `AI_OVERRIDE_MIN_TTL_SEC` | 300 (5 min) | Prevent trivially short overrides |
| Ceiling | `AI_OVERRIDE_MAX_TTL_SEC` | 3600 (60 min) | Existing; prevent runaway overrides |
| Default | `AI_OVERRIDE_TTL_SEC` | 1800 (30 min) | Fallback when AI omits field or returns 0 |

**Clamping logic** (in status payload computation):

```
ai_minutes = opinion.suggested_ttl_minutes  (0-60 integer)

if ai_minutes > 0:
    suggested_sec = clamp(ai_minutes * 60, MIN_TTL, MAX_TTL)
else:
    suggested_sec = AI_OVERRIDE_TTL_SEC   # config default (30 min)
```

A value of `0` means "AI didn't provide a suggestion" — use the config default. This ensures backward compatibility with older panelist responses or parse failures.

## 6. Affected Components

### 6.1 `ai_advisor.py`

| Location | Change |
|----------|--------|
| `_REGIME_SYSTEM_PROMPT` (L89-113) | Add `suggested_ttl_minutes` to JSON schema |
| `_parse_regime_opinion()` (L701-734) | Extract & clamp `suggested_ttl_minutes` (0-60) |
| `_default_regime_opinion()` (L350-359) | Add `"suggested_ttl_minutes": 0` |

### 6.2 `config.py`

| Location | Change |
|----------|--------|
| AI override section (L253-260) | Add `AI_OVERRIDE_MIN_TTL_SEC` (default 300) |

### 6.3 `bot.py`

| Location | Change |
|----------|--------|
| `_process_ai_regime_pending_result()` (L3184-3236) | Extract `suggested_ttl_minutes`, store in `_ai_regime_opinion` |
| `_ai_regime_status_payload()` (L3864-3877) | Compute `suggested_ttl_sec` from minutes, add to payload |
| `apply_ai_regime_override()` (L3049-3058) | Use `AI_OVERRIDE_MIN_TTL_SEC` as floor instead of 1 |

### 6.4 `dashboard.py`

| Location | Change |
|----------|--------|
| Apply button text (L1678) | Show `suggested_ttl_sec` instead of `default_ttl_sec` |
| Apply click handler (L2378-2381) | Send `suggested_ttl_sec` instead of `default_ttl_sec` |

### 6.5 `.env.example`

| Change |
|--------|
| Add `AI_OVERRIDE_MIN_TTL_SEC=300` with comment |

## 7. Dashboard UX

**Before:**
```
[Apply Override (30m)]  [Dismiss]
```

**After (AI suggests 25 min):**
```
[Apply Override (25m)]  [Dismiss]
```

**After (AI omits field / returns 0):**
```
[Apply Override (30m)]  [Dismiss]
```
Falls back to config default — visually identical to current behavior.

## 8. Backward Compatibility

- If the AI response doesn't include `suggested_ttl_minutes` (older model, parse error, etc.), the field defaults to `0` and the system falls back to `AI_OVERRIDE_TTL_SEC` — identical to current behavior.
- No API contract changes for the override endpoint. The `ttl_sec` parameter already exists and accepts any integer.
- No state format changes. The field is transient in `_ai_regime_opinion` (not persisted to state.json).

## 9. Testing

4 new unit tests in `tests/test_ai_regime_advisor.py`:

1. **`test_parse_regime_opinion_suggested_ttl_present`** — Response includes `"suggested_ttl_minutes": 25` → parsed as 25
2. **`test_parse_regime_opinion_suggested_ttl_missing`** — Field omitted → defaults to 0
3. **`test_parse_regime_opinion_suggested_ttl_clamped`** — Value 120 clamped to 60; value -5 clamped to 0
4. **`test_default_regime_opinion_has_suggested_ttl`** — Default opinion includes `suggested_ttl_minutes: 0`

## 10. Future Extensions (Out of Scope)

- **Dynamic TTL adjustment:** Re-evaluate override TTL mid-flight if new signals arrive
- **TTL confidence interval:** AI returns a range (optimistic/pessimistic) instead of point estimate
- **Auto-apply with AI-suggested TTL:** Skip user click when conviction exceeds threshold
- **TTL history analytics:** Track suggested vs. actual regime duration for model calibration
