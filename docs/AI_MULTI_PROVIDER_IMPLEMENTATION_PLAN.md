# AI Regime Advisor Multi-Provider Redundancy - Implementation Plan

Last updated: 2026-02-16
Parent spec: `docs/AI_MULTI_PROVIDER_SPEC.md` v0.1.0
Status: **Ready for implementation**

## Goal

Add SambaNova and Cerebras as first-class AI providers for regime opinion fallback, fix reasoning token clipping, and harden parsing for DeepSeek-style `<think>` output without changing advisor scheduling, override mechanics, or dashboard control flow.

## Scope

- Add provider credentials and wiring for SambaNova + Cerebras.
- Expand panel construction and ranking order for full 7-model fallback chains.
- Fix reasoning-model token cap behavior in `_call_panelist_messages()`.
- Make `_parse_regime_opinion()` resilient to `<think>...</think>` preambles.
- Update docs/comments/startup summary to reflect provider expansion.
- Add targeted tests in `tests/test_ai_regime_advisor.py`.

## Current Baseline (Code Audit)

1. `ai_advisor.py` has only `GROQ_URL` and `NVIDIA_URL`; no SambaNova/Cerebras URLs or panelist tuples.
2. `ai_advisor.py` `_build_panel()` adds Groq and NVIDIA only (plus legacy fallback).
3. `ai_advisor.py` `_ordered_regime_panel()` supports a 4-model chain (Kimi, GPT-OSS-120B, Llama-70B, Llama-8B), not the 7-model chain in the spec.
4. `ai_advisor.py` `_call_panelist_messages()` still hard-caps all outputs at 512 (`max_tokens = min(..., 512)`), clipping reasoning models.
5. `ai_advisor.py` `_parse_regime_opinion()` currently parses from first `{` to last `}`, with no `<think>` stripping.
6. `config.py` defines only `GROQ_API_KEY` and `NVIDIA_API_KEY`; no `SAMBANOVA_API_KEY`/`CEREBRAS_API_KEY`.
7. `config.py` startup banner prints Groq/NVIDIA key status only.
8. `.env.example` has no SambaNova/Cerebras key entries.
9. `tests/test_ai_regime_advisor.py` does not include multi-provider panel wiring, token-cap regression, or think-tag parsing coverage.

## Critical Clarification (Before Coding)

Duplicate model names across providers can collide in cooldown/failure tracking.

- Current skip/fail maps (`_panelist_consecutive_fails`, `_panelist_skip_until`) key by `panelist["name"]`.
- If the same display name exists on two providers (for example GPT-OSS-120B), one provider's failures can suppress the other.
- `get_regime_opinion()` currently does not consult cooldown/skip state at all, so failed endpoints are retried every advisor cycle.

Implementation decision:
- Keep human-facing `name` for logs/UI.
- Add an internal per-panelist key (for example `panelist_id = "{url}|{model}"`) and use it for skip/failure dictionaries.
- Wire skip/cooldown checks into `get_regime_opinion()` in the same pass so regime advisor retries are throttled like other AI call paths.

## Implementation Steps

## 1) Config and environment contract

Files:
- `config.py`
- `.env.example`

Changes:
1. Add new env vars in `config.py`:
   - `SAMBANOVA_API_KEY`
   - `CEREBRAS_API_KEY`
2. Update AI council comments to mention all supported provider keys.
3. Update startup summary banner to include:
   - SambaNova key status
   - Cerebras key status
   - existing Groq/NVIDIA status
4. Add `.env.example` entries for the two new keys under AI provider section.

Deliverable:
- Provider credentials are configurable and visible at startup.

## 2) Provider constants and panelist definitions

Files:
- `ai_advisor.py`

Changes:
1. Add:
   - `SAMBANOVA_URL = "https://api.sambanova.ai/v1/chat/completions"`
   - `CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"`
2. Add panel tuples:
   - `SAMBANOVA_PANELISTS` with `DeepSeek-R1` (reasoning) and `DeepSeek-V3.1` (instruct)
   - `CEREBRAS_PANELISTS` with `Qwen3-235B` and `GPT-OSS-120B` (instruct)
3. Update `GROQ_PANELISTS` to remove GPT-OSS-120B (free-tier failures on Groq):
   - `Llama-70B`
   - `Llama-8B`
4. Keep NVIDIA tuple definitions unchanged.

Deliverable:
- New providers are represented with explicit URL/model metadata.

## 3) Extend `_build_panel()` to include all providers

Files:
- `ai_advisor.py`

Changes:
1. Add SambaNova block when `config.SAMBANOVA_API_KEY` is set.
2. Add Cerebras block when `config.CEREBRAS_API_KEY` is set.
3. Preserve existing Groq/NVIDIA and legacy fallback behavior.
4. Add internal panelist identity field (for example `panelist_id`) in each panel entry to prevent cooldown key collisions.
5. Wire `panelist_id`-based skip/cooldown handling into `get_regime_opinion()` so failed panelists are temporarily skipped there too (matching existing behavior in other advisor paths).
6. Keep `max_tokens` assignment logic based on `reasoning` flag.

Deliverable:
- Panel auto-build supports any subset of configured providers.

## 4) Update ranking logic for 7-model chains

Files:
- `ai_advisor.py`

Changes:
1. Replace priority maps in `_ordered_regime_panel()` with the spec chains:
   - reasoning-preferred chain:
     `DeepSeek-R1 -> Kimi-K2.5 -> DeepSeek-V3.1 -> Qwen3-235B -> GPT-OSS-120B -> Llama-70B -> Llama-8B`
   - instruct-preferred chain:
     `DeepSeek-V3.1 -> Qwen3-235B -> GPT-OSS-120B -> Llama-70B -> Llama-8B -> DeepSeek-R1 -> Kimi-K2.5`
2. Keep stable secondary sort by insertion order for deterministic behavior when priorities tie.

Deliverable:
- Fallback order is strongest-to-weakest across providers.

## 5) Fix reasoning token cap behavior

Files:
- `ai_advisor.py`

Changes:
1. In `_call_panelist_messages()`, apply cap by model type:
   - reasoning: cap at `_REASONING_MAX_TOKENS` (2048)
   - instruct: cap at 512
2. Keep existing panelist-specific `max_tokens` lower bounds via `min(configured, cap)`.

Deliverable:
- Reasoning models are no longer silently clipped to 512.

## 6) Harden JSON extraction for `<think>` outputs

Files:
- `ai_advisor.py`

Changes:
1. Preprocess response in `_parse_regime_opinion()`:
   - if `</think>` exists, parse only content after the last closing think tag.
2. Keep existing fallback behavior:
   - no tags -> parse unchanged
   - invalid/truncated JSON -> `parse_error`, continue fallback chain.
3. Treat this as mandatory for this workstream because DeepSeek-R1 is rank-1 in reasoning mode.

Deliverable:
- DeepSeek reasoning preambles do not poison JSON extraction.

## 7) Update docs/comments in `ai_advisor.py`

Files:
- `ai_advisor.py`

Changes:
1. Update module header `SUPPORTED PROVIDERS` section to list SambaNova, Cerebras, Groq, NVIDIA.
2. Update `_build_panel()` docstring provider summary.
3. Update `get_regime_opinion()` fallback-order docstring to reflect 7-model chain.

Deliverable:
- Runtime docs match actual provider behavior.

## 8) Test Plan (Unit)

Files:
- `tests/test_ai_regime_advisor.py`

Add tests from spec:
1. `test_sambanova_panelists_defined`
2. `test_cerebras_panelists_defined`
3. `test_build_panel_all_providers`
4. `test_build_panel_partial_keys`
   - with only `GROQ_API_KEY`, panel should include only `Llama-70B` and `Llama-8B`
5. `test_ordered_panel_full_reasoning_chain`
6. `test_ordered_panel_full_instruct_chain`
7. `test_reasoning_token_cap_not_clipped`
8. `test_instruct_token_cap_preserved`
9. `test_think_tag_stripping`
10. `test_think_tag_absent`
11. `test_reasoning_content_fallback`

Add one additional safety test:
12. `test_panelist_skip_tracking_uses_unique_identity_key`
    - verify duplicate display names on different providers do not share cooldown/failure counters.
13. `test_groq_panelists_excludes_gpt_oss`
    - verify Groq panel contains only Llama-70B and Llama-8B.
14. `test_get_regime_opinion_honors_panelist_cooldown`
    - verify regime advisor skips panelists with active cooldown and advances to next candidate.

Deliverable:
- Multi-provider behavior and parser hardening are regression-protected.

## 9) Verification Run

1. Run targeted tests:
   - `python3 -m unittest tests.test_ai_regime_advisor`
2. Run integration guard:
   - `python3 -m unittest tests.test_hardening_regressions`
3. Manual smoke checks:
   - startup banner shows SambaNova/Cerebras key status
   - advisor fallback still returns default-safe response on total failure

Deliverable:
- Green test evidence and no regressions in existing hardening suite.

## 10) Rollout Plan

1. Set env vars in deployment:
   - `SAMBANOVA_API_KEY`
   - `CEREBRAS_API_KEY`
2. Deploy advisor changes.
3. Observe first 3-5 advisor cycles:
   - panelist should usually be DeepSeek-R1 or DeepSeek-V3.1 when available
   - failures should cascade naturally to Cerebras/Groq/NVIDIA
4. Monitor logs for `http_429`, `http_4xx`, parse errors, and fallback usage frequency.

Success criteria:
- Advisor remains available under single-provider failures.
- Reasoning responses no longer show frequent truncation-style parse failures.

## Rollback Plan

If regression is observed:

1. Revert SambaNova/Cerebras panel blocks from `_build_panel()`.
2. Revert ranking map to current 4-model chain.
3. Revert token-cap change in `_call_panelist_messages()`.
4. Revert think-tag preprocessing in `_parse_regime_opinion()` if implicated.
5. Keep existing Groq+NVIDIA path as known-good fallback.

## Acceptance Criteria

- New provider keys are supported in `config.py` and `.env.example`.
- Panel can build all 7 models when all keys are configured.
- Ordering matches spec for both reasoning-preferred and instruct-preferred modes.
- Reasoning models can use up to 2048 output tokens.
- `<think>` responses parse correctly without breaking non-think outputs.
- Unit and hardening tests pass.
