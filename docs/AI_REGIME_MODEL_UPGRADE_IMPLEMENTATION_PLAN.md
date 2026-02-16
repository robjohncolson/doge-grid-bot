# AI Regime Advisor Model Upgrade - Implementation Plan

Last updated: 2026-02-16
Parent spec: `docs/AI_REGIME_MODEL_UPGRADE_SPEC.md` v0.1.0
Status: **Ready for implementation**

## Goal

Upgrade AI regime panel quality and fix conviction semantics so conviction reflects confidence in assessment (including high-confidence Tier 0/ranging calls), while preserving existing fallback resilience and safety rails.

## Scope

- Update regime panel model lineup and ordering.
- Increase instruct-model output token budget.
- Refine system prompt to define conviction semantics and output schema.
- Include consensus probabilities in AI regime context payload.
- Add targeted regression tests for the changes above.

## Current Baseline (Code Audit)

1. `ai_advisor.py` `GROQ_PANELISTS` includes only `Llama-70B` and `Llama-8B`; `GPT-OSS-120B` is not present.
2. `ai_advisor.py` `_ordered_regime_panel()` priorities are currently:
   - prefer_reasoning: `Kimi-K2.5`, `Llama-70B`, `Llama-8B`
   - non-reasoning: `Llama-70B`, `Llama-8B`, `Kimi-K2.5`
3. `ai_advisor.py` `_INSTRUCT_MAX_TOKENS` is `200`.
4. `ai_advisor.py` `_REGIME_SYSTEM_PROMPT` still uses the old wording and does not explicitly define conviction as confidence-in-assessment.
5. `ai_advisor.py` `_build_regime_context()` does not include `consensus_probabilities` in the consensus payload block.
6. `ai_advisor.py` `_sanitize_probabilities()` currently accepts list/tuple only; dict input defaults to `[0.0, 1.0, 0.0]`.
7. `tests/test_ai_regime_advisor.py` exists but does not currently cover model lineup, priority order, token budget constant, prompt semantics, or consensus-probability context wiring.

## Locked Implementation Decisions

1. No new config knobs; constants and panel order remain code-defined in `ai_advisor.py`.
2. Keep Kimi first only when `AI_REGIME_PREFER_REASONING=True`; otherwise use GPT-OSS-120B first.
3. Keep existing fallback behavior and error handling (`_call_panelist_messages`, parse fallback, default opinion).
4. Extend probability sanitization to support both dict and list formats for compatibility with current consensus payload shape.
5. Do not change override mechanics, thresholds, TTL, or dashboard action gating behavior in this workstream.

## Implementation Steps

## 1) Upgrade panel lineup and order logic

Files:
- `ai_advisor.py`

Changes:
1. Update `GROQ_PANELISTS` to:
   - `("GPT-OSS-120B", "openai/gpt-oss-120b", False)`
   - `("Llama-70B", "llama-3.3-70b-versatile", False)`
   - `("Llama-8B", "llama-3.1-8b-instant", False)`
2. Update `_ordered_regime_panel()` priority maps:
   - prefer reasoning: `Kimi-K2.5 -> GPT-OSS-120B -> Llama-70B -> Llama-8B`
   - non-reasoning: `GPT-OSS-120B -> Llama-70B -> Llama-8B -> Kimi-K2.5`
3. Update nearby comments/docstrings in `get_regime_opinion()` fallback order so runtime docs match behavior.

Deliverable:
- GPT-OSS-120B becomes the primary instruct panelist, with unchanged fallback resilience.

## 2) Increase instruct token budget

Files:
- `ai_advisor.py`

Changes:
1. Set `_INSTRUCT_MAX_TOKENS = 400` (from `200`).
2. Keep `_REASONING_MAX_TOKENS = 2048` unchanged.
3. Preserve existing hard cap `min(max_tokens, 512)` in `_call_panelist_messages()`.

Deliverable:
- Instruct models get 2x response budget without changing external knobs or call surface.

## 3) Rewrite regime system prompt for conviction semantics

Files:
- `ai_advisor.py`

Changes:
1. Replace `_REGIME_SYSTEM_PROMPT` with spec-defined wording:
   - explicit conviction definition as confidence in assessment, not urgency to change
   - explicit statement that Tier 0 can still carry high conviction
   - explicit JSON-only schema with fields:
     `recommended_tier`, `recommended_direction`, `conviction`, `rationale`, `watch_for`
2. Keep conservative tier guidance and safety posture intact.

Deliverable:
- Prompt contract directly targets the conviction=0-in-ranging failure mode.

## 4) Add consensus probabilities to context payload

Files:
- `ai_advisor.py`

Changes:
1. In `_build_regime_context()`, include:
   - `hmm.consensus.consensus_probabilities`
2. Update `_sanitize_probabilities()` to accept:
   - dict format: `{"bearish": x, "ranging": y, "bullish": z}`
   - list/tuple format: `[bearish, ranging, bullish]`
   - missing/malformed input fallback: `[0.0, 1.0, 0.0]`
3. Clamp and round values consistently with existing behavior.

Note:
- This step is required because consensus payload currently emits dict-shaped probabilities; without dict support, context silently degrades to default ranging probabilities.

Deliverable:
- LLM receives the blended probability distribution reliably.

## 5) Test coverage expansion

Files:
- `tests/test_ai_regime_advisor.py`

Add tests:
1. `test_groq_panelists_includes_gpt_oss`:
   - GPT-OSS-120B present and first in `GROQ_PANELISTS`.
2. `test_ordered_panel_prefer_reasoning`:
   - order is Kimi-K2.5, GPT-OSS-120B, Llama-70B, Llama-8B.
3. `test_ordered_panel_prefer_instruct`:
   - order is GPT-OSS-120B, Llama-70B, Llama-8B, Kimi-K2.5.
4. `test_instruct_max_tokens_400`:
   - constant is `400`.
5. `test_regime_prompt_conviction_definition`:
   - prompt contains conviction-as-assessment wording and Tier 0 high-conviction guidance.
6. `test_regime_context_has_consensus_probs`:
   - `_build_regime_context()` includes normalized `consensus_probabilities`.
7. `test_consensus_probs_missing_defaults`:
   - missing/malformed consensus probabilities map to `[0.0, 1.0, 0.0]`.
8. `test_consensus_probs_dict_format_supported`:
   - dict input is correctly converted and preserved through context build.

Deliverable:
- Spec behaviors are locked by unit tests before deployment.

## 6) Verification run

1. Run targeted advisor tests:
   - `python3 -m unittest tests.test_ai_regime_advisor`
2. Run full hardening regression file to catch integration side effects:
   - `python3 -m unittest tests.test_hardening_regressions`
3. Confirm no regressions in:
   - JSON parse fallback paths
   - panel fallback behavior
   - default opinion safety values (`tier=0`, `direction=symmetric`, `conviction=0` on error)

Deliverable:
- Green test evidence for both local advisor logic and broader runtime integrations.

## 7) Rollout and operational checks

1. Deploy normally (advisor-only changes; no trade execution path rewrite).
2. Observe next 3-5 advisor cycles in dashboard/logs:
   - panelist is usually `GPT-OSS-120B` when available
   - conviction is non-zero for clear ranging consensus states
   - rationale/watch_for remain parseable and bounded
3. Watch error logs for:
   - `invalid JSON` parse warnings
   - repeated panelist HTTP failures triggering fallback/cooldown

Success criteria:
- Conviction distribution is informative (not pinned at zero in clear Tier 0 states).
- Fallback chain remains stable under model/API hiccups.

## Rollback Plan

If regression is observed:

1. Revert `GROQ_PANELISTS` to prior Llama-first ordering.
2. Restore old `_ordered_regime_panel()` priority maps.
3. Restore `_INSTRUCT_MAX_TOKENS = 200`.
4. Restore previous `_REGIME_SYSTEM_PROMPT`.
5. Keep parsing and safety defaults unchanged so advisor remains non-blocking.

## Acceptance Criteria

- `GPT-OSS-120B` is primary instruct model in panel build and non-reasoning order.
- `_INSTRUCT_MAX_TOKENS` is `400`.
- Prompt explicitly defines conviction semantics and JSON output schema.
- Context includes sanitized `consensus_probabilities`.
- Unit tests covering all items in Step 5 pass.
- No new runtime exceptions or fallback regressions in advisor flow.
