# HMM Consensus Regime Label Fix

**Version:** 0.1.0
**Status:** Draft
**Affects:** `bot.py` `_compute_hmm_consensus()`, Kelly bucketing, `regime_at_entry` tagging

---

## 1. Problem Statement

The HMM consensus function determines the regime label (BEARISH/RANGING/BULLISH)
using the **sign of the weighted bias signal** rather than the actual probability
distributions from the sub-models. This causes incorrect regime labeling when
both models agree on RANGING but have asymmetric residual probabilities.

### Observed in Production

Both sub-models agree on RANGING with >96% probability:

```
Primary  (1m):  RANGING  probs=[bear:0.03% rang:98.84% bull:1.13%]  bias=+0.011
Secondary(15m): RANGING  probs=[bear:1.93% rang:98.07% bull:0.00%]  bias=-0.019
```

Agreement detection correctly identifies `agreement="full"` (both RANGING).

But the weighted bias is:
```
effective_bias = 0.3 × 0.011 + 0.7 × (-0.019) = -0.01014
```

The regime determination code (bot.py:2610-2618):
```python
if effective_confidence < tier1_conf:
    effective_regime = "RANGING"
elif effective_bias > 0:           # ← sign-based, ignores probabilities
    effective_regime = "BULLISH"
elif effective_bias < 0:           # ← this fires: -0.01 < 0
    effective_regime = "BEARISH"
```

**Result:** Consensus says BEARISH despite both models agreeing on RANGING at 98%+.

### Root Cause

The bias signal was designed for **directional strength measurement**
(how bullish vs bearish within a directional regime), not for **regime
classification**. Using its sign for classification creates a category error:

- In a RANGING distribution, P(BULLISH) and P(BEARISH) are both tiny (<2%).
  Their difference is noise, not signal.
- The individual models correctly use `argmax(probabilities)` for their own
  regime labels (hmm_regime_detector.py:350). The consensus should follow
  the same principle.

---

## 2. Impact

### Downstream Consumers of Consensus Regime Label

| Consumer | How it uses regime | Impact of mislabel |
|----------|-------------------|-------------------|
| `_current_regime_id()` | Returns `regime_id` for Kelly bucketing | Cycles tagged to wrong regime bucket |
| `regime_at_entry` | Stamped on CompletedCycle records | Historical regime data polluted |
| `_eval_regime_directional()` | Determines directional tier (0/1/2) | Currently gated by `abs_bias >= tier1_bias_floor (0.10)`, so bias of 0.01 fails the gate → tier stays at 0 (symmetric). **No active trading impact.** |
| `_build_regime_context()` (AI advisor) | Formats regime for LLM analysis | LLM gets contradictory signal |
| Dashboard / status payload | Displays regime to user | Confusing UX |

**Current severity: Low** — the directional tier gate (`abs_bias >= 0.10`)
prevents the mislabel from affecting order placement. But Kelly bucketing is
silently accumulating mistagged cycles, which will skew sizing once per-regime
buckets reach the 15-sample threshold.

---

## 3. Design Goals

| Goal | Priority |
|------|----------|
| Consensus regime label matches the probability-weighted evidence | P0 |
| Preserve existing bias signal computation (used for directional strength) | P0 |
| No behavioral change when both models genuinely agree on a directional regime | P0 |
| Carry blended probabilities through consensus for telemetry | P1 |
| Zero new config knobs | P1 |

### Non-Goals

- Changing individual model regime detection (already correct via argmax).
- Modifying how `bias_signal` is computed or used for directional tiers.
- Changing agreement detection logic (works correctly).

---

## 4. Fix Design

### 4.1 Core Principle

The agreement detection (lines 2587-2599) already compares regime labels
from both sub-models. When both agree, the consensus regime should **be**
the agreed regime — not something re-derived from a noisy scalar.

### 4.2 Probability Blending

For telemetry and robustness, blend the raw probability distributions:

```python
# Extract sub-model probabilities
probs_1m = _extract_probs(primary)    # [bear, rang, bull]
probs_15m = _extract_probs(secondary)  # [bear, rang, bull]

# Weighted blend
consensus_probs = [
    w1 * probs_1m[i] + w15 * probs_15m[i]
    for i in range(3)
]
```

### 4.3 Regime Label Determination (Replaces lines 2610-2618)

```python
# --- Determine effective regime label ---
tier1_conf = max(0.0, min(1.0, float(
    getattr(config, "REGIME_TIER1_CONFIDENCE", 0.20)
)))

if effective_confidence < tier1_conf:
    # Low confidence → default to RANGING regardless
    effective_regime = "RANGING"
elif agreement == "full":
    # Both models agree → use their agreed label directly
    effective_regime = regime_1m  # == regime_15m by definition
elif agreement == "1m_cooling":
    # 1m is RANGING, 15m has a directional signal → use 15m's label
    effective_regime = regime_15m
else:
    # "conflict" or "15m_neutral" → already handled by confidence < tier1
    # (effective_confidence is 0.0 for these cases)
    effective_regime = "RANGING"
```

**Behavioral analysis by agreement type:**

| Agreement | Current behavior | New behavior | Change? |
|-----------|-----------------|--------------|---------|
| `"full"` + both RANGING | Follows bias sign (BUG) | Uses agreed label: RANGING | YES — fix |
| `"full"` + both BULLISH | Follows bias sign (+) → BULLISH | Uses agreed label: BULLISH | No change |
| `"full"` + both BEARISH | Follows bias sign (-) → BEARISH | Uses agreed label: BEARISH | No change |
| `"1m_cooling"` | Follows dampened 15m bias sign | Uses 15m regime label | Equivalent* |
| `"15m_neutral"` | conf=0 < tier1 → RANGING | Same path → RANGING | No change |
| `"conflict"` | conf=0 < tier1 → RANGING | Same path → RANGING | No change |

*Equivalent because: if 15m says BEARISH, its bias is negative, so sign-based
and label-based both yield BEARISH. Same for BULLISH.

### 4.4 Helper: Extract Probabilities

```python
def _extract_probs(state: dict) -> list[float]:
    """Extract [bearish, ranging, bullish] from an HMM state dict."""
    p = state.get("probabilities", {})
    if isinstance(p, dict):
        return [
            float(p.get("bearish", 0.0) or 0.0),
            float(p.get("ranging", 1.0) or 0.0),
            float(p.get("bullish", 0.0) or 0.0),
        ]
    if isinstance(p, (list, tuple)) and len(p) >= 3:
        return [float(p[0]), float(p[1]), float(p[2])]
    return [0.0, 1.0, 0.0]
```

### 4.5 Consensus Probabilities in Output

Add to the returned dict (line ~2620):

```python
"consensus_probabilities": {
    "bearish": consensus_probs[0],
    "ranging": consensus_probs[1],
    "bullish": consensus_probs[2],
},
```

This provides visibility into what the blended distribution looks like,
independent of the regime label. Useful for debugging and dashboard.

---

## 5. Integration Points

### 5.1 `_compute_hmm_consensus()` (bot.py ~line 2503)

**Changes:**
1. Add `_extract_probs()` helper (static method or module-level function)
2. Compute `consensus_probs` after weight normalization
3. Replace regime determination block (lines 2610-2618) with label-based logic
4. Add `consensus_probabilities` to returned dict

### 5.2 Status Payload (bot.py ~line 7500)

No changes needed — the consensus dict is already serialized into the payload.
The new `consensus_probabilities` field appears automatically.

### 5.3 Dashboard (dashboard.py)

Optional: display consensus probabilities in the HMM panel. Low priority
since the primary fix is the label, not the display.

---

## 6. Edge Cases

### 6.1 One Model Has Empty Probabilities

If a model's state dict lacks `probabilities` or has malformed data,
`_extract_probs()` returns `[0.0, 1.0, 0.0]` (default RANGING). The
consensus blend degrades gracefully to the other model's probabilities.

### 6.2 Both Models RANGING but Consensus Confidence High

This is the exact scenario that triggered the bug. With the fix:
- `agreement = "full"`, both RANGING
- `effective_confidence = 0.9771` (high — both are confident RANGING)
- `effective_regime = "RANGING"` (from agreed label)
- `effective_bias = -0.01` (still computed, still available for directional)

The high confidence + RANGING regime correctly signals "the market is
confidently ranging" rather than the misleading "confidently bearish".

### 6.3 Bias Signal Still Negative in RANGING Regime

The bias signal remains unchanged — it's still the weighted directional
lean. Downstream consumers that use `bias_signal` for directional tier
evaluation continue to work identically:
- `abs_bias (0.01) < tier1_bias_floor (0.10)` → directional gate fails
- `mechanical_target_tier = 0` (symmetric)
- No entry suppression

This is correct. A -0.01 bias in a 98% RANGING distribution has no
directional significance.

### 6.4 Agreement="full" but Probabilities Disagree on Magnitude

Example: Primary says BULLISH at 60%, Secondary says BULLISH at 95%.
Both agree on regime → `effective_regime = "BULLISH"`. The blended
probabilities show the composite view. Confidence = max(conf_1m, conf_15m).

This is fine — the agreed label is correct, and the confidence captures
how decisive the call is.

---

## 7. Testing Plan

| Test | Description |
|------|-------------|
| `test_consensus_ranging_with_negative_bias` | Both RANGING, bias < 0 → regime = RANGING (not BEARISH) |
| `test_consensus_ranging_with_positive_bias` | Both RANGING, bias > 0 → regime = RANGING (not BULLISH) |
| `test_consensus_full_agreement_bullish` | Both BULLISH → regime = BULLISH (unchanged) |
| `test_consensus_full_agreement_bearish` | Both BEARISH → regime = BEARISH (unchanged) |
| `test_consensus_1m_cooling_uses_15m_label` | 1m=RANGING, 15m=BEARISH → regime = BEARISH |
| `test_consensus_conflict_gives_ranging` | 1m=BULLISH, 15m=BEARISH → regime = RANGING |
| `test_consensus_low_confidence_gives_ranging` | Both BULLISH but conf < tier1 → regime = RANGING |
| `test_consensus_probabilities_blended` | Verify consensus_probabilities = w1*p1 + w15*p15 |
| `test_extract_probs_dict_format` | Probabilities as `{bearish:, ranging:, bullish:}` dict |
| `test_extract_probs_list_format` | Probabilities as `[bear, rang, bull]` list |
| `test_extract_probs_missing` | No probabilities key → defaults to [0, 1, 0] |

### Regression Coverage

The existing `test_hardening_regressions.py` has HMM-related tests. The new
tests should verify the specific "full agreement RANGING + non-zero bias" case
that was previously broken.

---

## 8. Rollout

This is a bug fix, not a feature toggle. No staged rollout needed.

1. **Implement** the fix in `_compute_hmm_consensus()`
2. **Add tests** per Section 7
3. **Deploy** — the fix is strictly corrective; no new behavior to observe
4. **Verify** in next status payload that `regime` matches the sub-model
   agreement when both say RANGING

---

## 9. Summary of Changes

| File | Change |
|------|--------|
| `bot.py` | Replace 8-line regime determination block with label-based logic. Add `_extract_probs()` helper. Add `consensus_probabilities` to output dict. |
| `tests/test_hardening_regressions.py` | Add 11 new tests per Section 7. |

**Lines changed:** ~30 net (replace 8 + add 22).
**Risk:** Low — only affects the label derivation in one function. Bias signal,
confidence, agreement detection all unchanged.
