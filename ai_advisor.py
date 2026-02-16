"""
ai_advisor.py -- Multi-model AI council for grid trading decisions.

HOW THIS WORKS:
  Every AI_ADVISOR_INTERVAL seconds, the bot:
    1. Gathers market context (price, changes, spread, fill count)
    2. Queries multiple AI models (the "council") with the same prompt
    3. Each model votes on condition and action
    4. Majority vote determines the final recommendation
    5. Only surfaces actionable recommendations when the council agrees

  The council approach eliminates single-model bias (the "broken record"
  problem where one model fixates on the same recommendation).

SUPPORTED PROVIDERS:
  - Groq (free tier): GPT-OSS-120B + Llama 3.3 70B + Llama 3.1 8B
  - NVIDIA build.nvidia.com (free tier): Kimi K2.5
  - Any OpenAI-compatible endpoint (legacy single-model fallback)

  Set GROQ_API_KEY and/or NVIDIA_API_KEY to enable panelists.
  Panel auto-configures based on which keys are available.

ZERO DEPENDENCIES:
  Uses urllib.request to POST to OpenAI-compatible endpoints.
"""

import json
import time
import csv
import os
import logging
import urllib.request
import urllib.error
from collections import Counter
from datetime import datetime, timezone
from math import isfinite

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Council panel definitions
# ---------------------------------------------------------------------------

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# Each tuple: (display_name, model_id, is_reasoning_model)
GROQ_PANELISTS = [
    ("GPT-OSS-120B", "openai/gpt-oss-120b", False),
    ("Llama-70B", "llama-3.3-70b-versatile", False),
    ("Llama-8B", "llama-3.1-8b-instant", False),
]

NVIDIA_PANELISTS = [
    ("Kimi-K2.5", "moonshotai/kimi-k2.5", True),
]

# Reasoning models need more tokens (chain-of-thought + answer)
_REASONING_MAX_TOKENS = 2048
_INSTRUCT_MAX_TOKENS = 400

# Panelist timeout skip tracking
_panelist_consecutive_fails: dict = {}   # name -> int
_panelist_skip_until: dict = {}          # name -> timestamp
SKIP_THRESHOLD = 3        # consecutive failures before skipping
SKIP_COOLDOWN = 3600      # seconds to skip a panelist (1 hour)

_REGIME_DIRECTIONS = {"symmetric", "long_bias", "short_bias"}
_REGIME_LABELS = {"BEARISH", "RANGING", "BULLISH"}
_REGIME_QUALITY_TIERS = {"shallow", "baseline", "deep", "full"}

_REGIME_SYSTEM_PROMPT = (
    "You are a regime analyst for a DOGE/USD grid trading bot. You receive "
    "technical signals from a Hidden Markov Model (3-state: BEARISH, "
    "RANGING, BULLISH) running on two timeframes (1-minute and 15-minute), "
    "plus operational metrics. Your job is to interpret these signals "
    "holistically and recommend a trading posture.\n\n"
    "The bot uses a 3-tier system:\n"
    "- Tier 0 (Symmetric): Both sides trade equally. Default/safe.\n"
    "- Tier 1 (Asymmetric): Favor one side with spacing bias.\n"
    "- Tier 2 (Aggressive): Suppress the against-trend side entirely.\n\n"
    "Recommend a tier and direction using ALL signals. Consider timeframe "
    "agreement/convergence, transition matrix stickiness, consensus "
    "probabilities, operational signals, and whether confidence is "
    "rising/falling over recent history. Be conservative. Tier 2 is rare. "
    "When uncertain, recommend Tier 0 (symmetric).\n\n"
    "Return ONLY a JSON object with these fields:\n"
    '- "recommended_tier": 0, 1, or 2\n'
    '- "recommended_direction": "symmetric", "long_bias", or "short_bias"\n'
    '- "conviction": 0-100, your confidence in the ASSESSMENT (not urgency '
    'to change). 80 means "I\'m quite sure this is the right posture." '
    "Even Tier 0 can have high conviction when signals clearly confirm "
    'ranging. 0 means "I cannot read these signals at all."\n'
    '- "rationale": brief explanation (1-2 sentences)\n'
    '- "watch_for": what would change your mind (1 sentence)'
)


def _build_panel() -> list:
    """
    Build the AI council based on available API keys.

    Returns a list of panelist dicts.  Auto-configures:
      - GROQ_API_KEY  -> GPT-OSS-120B + Llama 3.3 70B + Llama 3.1 8B
      - NVIDIA_API_KEY -> Kimi K2.5
      - Both keys     -> all four (best diversity)
      - Neither       -> legacy fallback to AI_API_KEY
    """
    panel = []

    if config.GROQ_API_KEY:
        for name, model, reasoning in GROQ_PANELISTS:
            panel.append({
                "name": name,
                "url": GROQ_URL,
                "model": model,
                "key": config.GROQ_API_KEY,
                "reasoning": reasoning,
                "max_tokens": _REASONING_MAX_TOKENS if reasoning else _INSTRUCT_MAX_TOKENS,
            })

    if config.NVIDIA_API_KEY:
        for name, model, reasoning in NVIDIA_PANELISTS:
            panel.append({
                "name": name,
                "url": NVIDIA_URL,
                "model": model,
                "key": config.NVIDIA_API_KEY,
                "reasoning": reasoning,
                "max_tokens": _REASONING_MAX_TOKENS if reasoning else _INSTRUCT_MAX_TOKENS,
            })

    # Legacy fallback: single model from AI_API_KEY + AI_API_URL
    if not panel and config.AI_API_KEY:
        panel.append({
            "name": config.AI_MODEL.split("/")[-1],
            "url": config.AI_API_URL,
            "model": config.AI_MODEL,
            "key": config.AI_API_KEY,
            "reasoning": False,
            "max_tokens": _INSTRUCT_MAX_TOKENS,
        })

    return panel


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_prompt(market_data: dict, stats_context: str = "") -> str:
    """
    Build a compact prompt for each council panelist.

    We keep it short to minimize token usage and latency.
    The AI gets: current price, recent changes, spread, fill count,
    and optionally the output of the statistical analyzers.
    """
    if config.STRATEGY_MODE == "pair":
        import json as _json
        pair_display = market_data.get("market", {}).get("pair_display", "DOGE/USD")
        prompt = f"""You are a crypto pair-trading advisor analyzing a {pair_display} bot.

DATA:
{_json.dumps(market_data, indent=2, default=str)}

The bot runs two trades flanking market price:
- Trade A (short): sell entry above market -> buy exit below
- Trade B (long): buy entry below market -> sell exit above

AVAILABLE ACTIONS (pick exactly one):
- continue: no changes needed
- tighten_entry: reduce entry distance (more fills, more whipsaw risk)
- widen_entry: increase entry distance (fewer fills, better entries)
- tighten_profit: reduce profit target (faster exits, less profit per cycle)
- widen_profit: increase profit target (slower exits, more profit per cycle)
- pause: stop trading temporarily (high risk detected)
"""
        if stats_context:
            prompt += f"\n{stats_context}\n"
        prompt += """
Answer with JSON only: {"regime": "<ranging|trending|volatile>", "action": "<action>", "reason": "<1-2 sentences>"}"""
    else:
        pair_display = market_data.get('pair_display', 'DOGE/USD')
        prompt = f"""You are a crypto grid trading advisor. Analyze this {pair_display} market data and give a brief recommendation.

Current price: ${market_data.get('price', 0):.6f}
1h change: {market_data.get('change_1h', 0):.2f}%
4h change: {market_data.get('change_4h', 0):.2f}%
24h change: {market_data.get('change_24h', 0):.2f}%
Bid-ask spread: {market_data.get('spread_pct', 0):.3f}%
Grid fills (last hour): {market_data.get('recent_fills', 0)}
Grid center: ${market_data.get('grid_center', 0):.6f}
Grid spacing: {config.GRID_SPACING_PCT}%"""
        if stats_context:
            prompt += f"\n\n{stats_context}"
        prompt += """

Answer in exactly this format (3 lines only):
CONDITION: [ranging/trending_up/trending_down/volatile/low_volume]
ACTION: [continue/pause/widen_spacing/tighten_spacing/reset_grid]
REASON: [One sentence explanation]"""

    return prompt


# ---------------------------------------------------------------------------
# API call (parameterized for each panelist)
# ---------------------------------------------------------------------------

def _call_panelist(prompt: str, panelist: dict, pair_display: str = "DOGE/USD") -> tuple:
    """
    Call a single AI panelist and return the raw response text.

    Supports any OpenAI-compatible endpoint (NVIDIA, Groq, etc.).
    Handles reasoning models (Kimi K2.5) that may put the answer
    in reasoning_content instead of content.

    Returns (response_text, error_string).  On success error is "".
    On failure response_text is "" and error describes what went wrong.
    """
    # Reasoning models (chain-of-thought) need longer timeouts
    timeout = 60 if panelist.get("reasoning") else 30

    if config.STRATEGY_MODE == "pair":
        system_content = (
            "You are a concise crypto market analyst advising a pair trading "
            f"bot on {pair_display}. The bot runs two concurrent trades: "
            "Trade A (short-side: sell entry, buy exit) and "
            "Trade B (long-side: buy entry, sell exit). "
            "State machine: S0=both entries open, S1a=A entered (exit pending), "
            "S1b=B entered (exit pending), S2=both entered. "
            "Entry distance controls how far entries sit from market -- "
            "wider means fewer fills but less risk. Profit target controls "
            "the round trip size -- wider means more profit per trip but "
            "slower fills. Both parameters apply symmetrically to A and B. "
            "Be direct and specific. Never recommend buying or selling -- "
            "only recommend parameter adjustments."
        )
    else:
        system_content = (
            "You are a concise crypto market analyst. "
            f"Give structured recommendations for a {pair_display} grid trading bot. "
            "Be direct and specific. Never recommend buying or selling -- "
            "only recommend grid parameter adjustments."
        )

    payload = json.dumps({
        "model": panelist["model"],
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": panelist["max_tokens"],
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {panelist['key']}",
        "User-Agent": "DOGEGridBot/1.0",
    }

    req = urllib.request.Request(panelist["url"], data=payload, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                # Reasoning models (Kimi K2.5) may put the answer in
                # "content" but spend tokens on "reasoning_content".
                # If content is null, fall back to reasoning_content.
                content = msg.get("content")
                if not content:
                    content = msg.get("reasoning_content")
                return (content.strip(), "") if content else ("", "Empty response body")
            return ("", "No choices in response")

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.warning(
            "%s HTTP %d: %s", panelist["name"], e.code, error_body[:200],
        )
        return ("", f"HTTP {e.code}")

    except urllib.error.URLError as e:
        logger.warning("%s connection error: %s", panelist["name"], e.reason)
        return ("", f"Connection error: {e.reason}")

    except Exception as e:
        logger.warning("%s error: %s", panelist["name"], e)
        return ("", str(e))


# ---------------------------------------------------------------------------
# Regime advisor helpers
# ---------------------------------------------------------------------------

def _default_regime_opinion(error: str = "") -> dict:
    return {
        "recommended_tier": 0,
        "recommended_direction": "symmetric",
        "conviction": 0,
        "rationale": "",
        "watch_for": "",
        "panelist": "",
        "error": _clip_text(error, 200),
    }


def _safe_float(value, default: float = 0.0, minimum=None, maximum=None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = float(default)
    if not isfinite(out):
        out = float(default)
    if minimum is not None and out < minimum:
        out = float(minimum)
    if maximum is not None and out > maximum:
        out = float(maximum)
    return out


def _safe_int(value, default: int = 0, minimum=None, maximum=None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        try:
            out = int(float(value))
        except (TypeError, ValueError):
            out = int(default)
    if minimum is not None and out < minimum:
        out = int(minimum)
    if maximum is not None and out > maximum:
        out = int(maximum)
    return out


def _clip_text(value, max_len: int) -> str:
    text = str(value or "").strip()
    if max_len > 0 and len(text) > max_len:
        return text[:max_len]
    return text


def _normalize_regime(value) -> str:
    regime = str(value or "RANGING").strip().upper()
    if regime not in _REGIME_LABELS:
        return "RANGING"
    return regime


def _normalize_direction(value) -> str:
    direction = str(value or "symmetric").strip().lower()
    if direction not in _REGIME_DIRECTIONS:
        return "symmetric"
    return direction


def _sanitize_probabilities(values) -> list:
    if isinstance(values, dict):
        values = [
            values.get("bearish"),
            values.get("ranging"),
            values.get("bullish"),
        ]
    if not isinstance(values, (list, tuple)):
        return [0.0, 1.0, 0.0]
    probs = []
    for raw in list(values)[:3]:
        probs.append(round(_safe_float(raw, 0.0, 0.0, 1.0), 4))
    while len(probs) < 3:
        probs.append(0.0)
    if sum(probs) <= 0.0:
        return [0.0, 1.0, 0.0]
    return probs


def _sanitize_transition_matrix(values) -> list:
    if not isinstance(values, (list, tuple)):
        return []
    matrix = []
    for row in list(values)[:3]:
        if not isinstance(row, (list, tuple)):
            return []
        sanitized = []
        for raw in list(row)[:3]:
            sanitized.append(round(_safe_float(raw, 0.0, 0.0, 1.0), 4))
        while len(sanitized) < 3:
            sanitized.append(0.0)
        matrix.append(sanitized)
    while len(matrix) < 3 and matrix:
        matrix.append([0.0, 0.0, 0.0])
    return matrix


def _sanitize_hmm_state(payload: dict) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    return {
        "regime": _normalize_regime(payload.get("regime")),
        "confidence": round(_safe_float(payload.get("confidence"), 0.0, 0.0, 1.0), 4),
        "bias_signal": round(_safe_float(payload.get("bias_signal"), 0.0, -1.0, 1.0), 4),
        "probabilities": _sanitize_probabilities(payload.get("probabilities")),
    }


def _build_regime_context(payload: dict) -> dict:
    """
    Build the structured regime context payload for the LLM prompt.

    The caller supplies one dict with all source fields; this function
    normalizes schema/limits before JSON serialization.
    """
    if not isinstance(payload, dict):
        payload = {}

    hmm = payload.get("hmm")
    if not isinstance(hmm, dict):
        hmm = {}

    primary = hmm.get("primary_1m")
    if not isinstance(primary, dict):
        primary = payload.get("hmm_primary")
    if not isinstance(primary, dict):
        primary = payload.get("primary_1m")
    if not isinstance(primary, dict):
        primary = {}

    secondary = hmm.get("secondary_15m")
    if not isinstance(secondary, dict):
        secondary = payload.get("hmm_secondary")
    if not isinstance(secondary, dict):
        secondary = payload.get("secondary_15m")
    if not isinstance(secondary, dict):
        secondary = {}

    consensus = hmm.get("consensus")
    if not isinstance(consensus, dict):
        consensus = payload.get("hmm_consensus")
    if not isinstance(consensus, dict):
        consensus = payload.get("consensus")
    if not isinstance(consensus, dict):
        consensus = {}

    transition_matrix = hmm.get("transition_matrix_1m")
    if transition_matrix is None:
        transition_matrix = payload.get("transition_matrix_1m")

    training_quality = str(
        hmm.get("training_quality", payload.get("training_quality", "shallow"))
    ).strip().lower()
    if training_quality not in _REGIME_QUALITY_TIERS:
        training_quality = "shallow"

    confidence_modifier = round(
        _safe_float(
            hmm.get("confidence_modifier", payload.get("confidence_modifier", 1.0)),
            1.0,
            0.5,
            1.0,
        ),
        4,
    )

    history_raw = payload.get("regime_history_30m")
    if not isinstance(history_raw, list):
        history_raw = []
    history = []
    for item in history_raw[-60:]:
        if not isinstance(item, dict):
            continue
        history.append({
            "ts": int(_safe_float(item.get("ts"), 0.0, 0.0)),
            "regime": _normalize_regime(item.get("regime")),
            "conf": round(
                _safe_float(item.get("conf", item.get("confidence")), 0.0, 0.0, 1.0),
                4,
            ),
        })

    mechanical = payload.get("mechanical_tier")
    if not isinstance(mechanical, dict):
        mechanical = {}

    operational = payload.get("operational")
    if not isinstance(operational, dict):
        operational = {}

    directional_trend = _clip_text(
        str(operational.get("directional_trend", "unknown")).strip().lower(),
        24,
    )
    if not directional_trend:
        directional_trend = "unknown"

    capacity_band = _clip_text(
        str(operational.get("capacity_band", "normal")).strip().lower(),
        24,
    )
    if not capacity_band:
        capacity_band = "normal"

    return {
        "hmm": {
            "primary_1m": _sanitize_hmm_state(primary),
            "secondary_15m": _sanitize_hmm_state(secondary),
            "consensus": {
                "agreement": _clip_text(consensus.get("agreement", "unknown"), 40),
                "effective_regime": _normalize_regime(
                    consensus.get("effective_regime", consensus.get("regime")),
                ),
                "effective_confidence": round(
                    _safe_float(consensus.get("effective_confidence"), 0.0, 0.0, 1.0),
                    4,
                ),
                "effective_bias": round(
                    _safe_float(consensus.get("effective_bias"), 0.0, -1.0, 1.0),
                    4,
                ),
                "consensus_probabilities": _sanitize_probabilities(
                    consensus.get("consensus_probabilities"),
                ),
            },
            "transition_matrix_1m": _sanitize_transition_matrix(transition_matrix),
            "training_quality": training_quality,
            "confidence_modifier": confidence_modifier,
        },
        "regime_history_30m": history,
        "mechanical_tier": {
            "current": _safe_int(mechanical.get("current"), 0, 0, 2),
            "direction": _normalize_direction(mechanical.get("direction", "symmetric")),
            "since": int(_safe_float(mechanical.get("since"), 0.0, 0.0)),
        },
        "operational": {
            "directional_trend": directional_trend,
            "trend_detected_at": int(_safe_float(operational.get("trend_detected_at"), 0.0, 0.0)),
            "fill_rate_1h": _safe_int(operational.get("fill_rate_1h"), 0, 0),
            "recovery_order_count": _safe_int(operational.get("recovery_order_count"), 0, 0),
            "capacity_headroom": round(
                _safe_float(operational.get("capacity_headroom"), 0.0, 0.0, 100.0),
                2,
            ),
            "capacity_band": capacity_band,
            "kelly_edge_bullish": round(
                _safe_float(operational.get("kelly_edge_bullish"), 0.0, -1.0, 1.0),
                6,
            ),
            "kelly_edge_bearish": round(
                _safe_float(operational.get("kelly_edge_bearish"), 0.0, -1.0, 1.0),
                6,
            ),
            "kelly_edge_ranging": round(
                _safe_float(operational.get("kelly_edge_ranging"), 0.0, -1.0, 1.0),
                6,
            ),
        },
    }


def _ordered_regime_panel(panel: list) -> list:
    if not panel:
        return []

    prefer_reasoning = bool(getattr(config, "AI_REGIME_PREFER_REASONING", True))
    if prefer_reasoning:
        priority = {"Kimi-K2.5": 0, "GPT-OSS-120B": 1, "Llama-70B": 2, "Llama-8B": 3}
    else:
        priority = {"GPT-OSS-120B": 0, "Llama-70B": 1, "Llama-8B": 2, "Kimi-K2.5": 3}

    with_index = list(enumerate(panel))
    with_index.sort(
        key=lambda item: (
            priority.get(str(item[1].get("name", "")).strip(), 50),
            item[0],
        )
    )
    return [p for _, p in with_index]


def _call_panelist_messages(messages: list, panelist: dict) -> tuple:
    timeout = 30 if panelist.get("reasoning") else 15
    max_tokens = min(int(panelist.get("max_tokens", _INSTRUCT_MAX_TOKENS)), 512)

    payload = json.dumps({
        "model": panelist["model"],
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {panelist['key']}",
        "User-Agent": "DOGEGridBot/1.0",
    }

    req = urllib.request.Request(panelist["url"], data=payload, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                content = msg.get("content")
                if not content:
                    content = msg.get("reasoning_content")
                return (content.strip(), "") if content else ("", "empty_response")
            return ("", "no_choices")
    except urllib.error.HTTPError as e:
        return ("", f"http_{e.code}")
    except urllib.error.URLError as e:
        return ("", f"connection_error:{e.reason}")
    except Exception as e:
        return ("", str(e))


def _parse_regime_opinion(response: str) -> tuple:
    if not response:
        return ({}, "empty_response")

    stripped = response.strip()
    json_start = stripped.find("{")
    json_end = stripped.rfind("}")
    if json_start < 0 or json_end <= json_start:
        return ({}, "parse_error")

    try:
        parsed = json.loads(stripped[json_start:json_end + 1])
    except Exception:
        return ({}, "parse_error")

    if not isinstance(parsed, dict):
        return ({}, "parse_error")

    tier_raw = parsed.get("recommended_tier")
    tier = _safe_int(tier_raw, 0)
    if tier not in (0, 1, 2):
        tier = 0

    opinion = {
        "recommended_tier": tier,
        "recommended_direction": _normalize_direction(parsed.get("recommended_direction")),
        "conviction": _safe_int(parsed.get("conviction"), 0, 0, 100),
        "rationale": _clip_text(parsed.get("rationale"), 500),
        "watch_for": _clip_text(parsed.get("watch_for"), 200),
    }
    return (opinion, "")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(response: str) -> dict:
    """
    Parse the structured AI response into a dict.

    Supports two formats:
      1. Line-based: CONDITION: / ACTION: / REASON: (grid mode)
      2. JSON: {"regime": ..., "action": ..., "reason": ...} (pair mode)

    Returns dict with keys: condition, action, reason, raw.
    """
    result = {
        "condition": "unknown",
        "action": "continue",
        "reason": "",
        "raw": response,
    }

    if not response:
        return result

    # Try JSON parse first (pair mode format)
    stripped = response.strip()
    # Find JSON object in response (may be wrapped in markdown code block)
    json_start = stripped.find("{")
    json_end = stripped.rfind("}")
    if json_start >= 0 and json_end > json_start:
        try:
            import json as _json
            parsed = _json.loads(stripped[json_start:json_end + 1])
            if "action" in parsed:
                result["action"] = str(parsed["action"]).strip().lower()
                result["condition"] = str(parsed.get("regime", parsed.get("condition", "unknown"))).strip().lower()
                result["reason"] = str(parsed.get("reason", "")).strip()
                return result
        except (ValueError, KeyError):
            pass  # Fall through to line-based parsing

    for line in stripped.split("\n"):
        line = line.strip()
        if line.upper().startswith("CONDITION:"):
            result["condition"] = line.split(":", 1)[1].strip().lower()
        elif line.upper().startswith("ACTION:"):
            result["action"] = line.split(":", 1)[1].strip().lower()
        elif line.upper().startswith("REASON:"):
            result["reason"] = line.split(":", 1)[1].strip()

    return result


# ---------------------------------------------------------------------------
# Vote aggregation
# ---------------------------------------------------------------------------

def _aggregate_votes(votes: list) -> dict:
    """
    Aggregate individual panelist votes into a council recommendation.

    Majority vote (>50%) determines the action.
    If no majority or all votes failed, default to "continue".
    This naturally suppresses spam: disagreement -> no action.
    """
    valid = [v for v in votes if v.get("action") not in ("", "unknown")]

    if not valid:
        return {
            "condition": "unknown",
            "action": "continue",
            "reason": "No valid votes from council",
            "raw": "",
            "panel_votes": votes,
            "consensus": False,
            "panel_size": 0,
            "winner_count": 0,
        }

    # Count action votes
    action_counts = Counter(v["action"] for v in valid)
    winner_action, winner_count = action_counts.most_common(1)[0]

    # Need strict majority (>50%)
    has_majority = winner_count > len(valid) / 2

    # Majority condition (for display and fallback)
    condition_counts = Counter(v["condition"] for v in valid)
    final_condition = condition_counts.most_common(1)[0][0]
    condition_winner_count = condition_counts.most_common(1)[0][1]

    if has_majority:
        final_action = winner_action
        consensus_type = "majority"
        # Use reason from the first voter who picked the winner
        reason = next(
            v["reason"] for v in valid if v["action"] == final_action
        )
    else:
        # No action majority -- try condition-based fallback
        # If 2/3+ agree on the market condition, map to a safe default action
        CONDITION_DEFAULT_ACTIONS = {
            "trending_down": "widen_entry",
            "trending_up": "tighten_entry",
            "volatile": "widen_spacing",
            "ranging": "tighten_spacing",
            "low_volume": "continue",
        }
        condition_has_supermajority = condition_winner_count >= len(valid) * 2 / 3
        if condition_has_supermajority and final_condition in CONDITION_DEFAULT_ACTIONS:
            final_action = CONDITION_DEFAULT_ACTIONS[final_condition]
            consensus_type = "condition_fallback"
            split = ", ".join(
                f"{count}x {act}" for act, count in action_counts.most_common()
            )
            reason = (
                f"Council deadlock broken by condition consensus: "
                f"{condition_winner_count}/{len(valid)} agree on '{final_condition}' "
                f"-> {final_action} (action split: {split})"
            )
            logger.info(reason)
        else:
            final_action = "continue"
            consensus_type = None
            split = ", ".join(
                f"{count}x {act}" for act, count in action_counts.most_common()
            )
            reason = f"Council split ({split}) -- no majority"

    return {
        "condition": final_condition,
        "action": final_action,
        "reason": reason,
        "raw": "",
        "panel_votes": votes,
        "vote_counts": dict(action_counts),
        "consensus": has_majority,
        "consensus_type": consensus_type,
        "panel_size": len(valid),
        "winner_count": winner_count if has_majority else 0,
    }


# ---------------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------------

def _log_recommendation(market_data: dict, parsed: dict):
    """Log the council recommendation to CSV for later analysis."""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    filepath = os.path.join(config.LOG_DIR, "ai_recommendations.csv")
    file_exists = os.path.exists(filepath)

    # Summarize panel votes for CSV
    panel_votes = parsed.get("panel_votes", [])
    panel_summary = "; ".join(
        f"{v.get('name', '?')}={v.get('action', '?')}"
        for v in panel_votes
    ) if panel_votes else ""

    try:
        with open(filepath, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "market_condition", "recommendation",
                    "current_price", "was_followed", "panel_votes",
                ])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                parsed["condition"],
                parsed["action"],
                f"{market_data.get('price', 0):.6f}",
                "pending",
                panel_summary,
            ])
    except Exception as e:
        logger.error("Failed to write AI recommendation log: %s", e)


def log_approval_decision(action: str, decision: str):
    """
    Log the user's approval decision (approve/skip/expired) to the CSV.

    Args:
        action:   The AI-recommended action (e.g. "widen_spacing")
        decision: One of "approved", "skipped", "expired"
    """
    os.makedirs(config.LOG_DIR, exist_ok=True)
    filepath = os.path.join(config.LOG_DIR, "ai_recommendations.csv")
    file_exists = os.path.exists(filepath)

    try:
        with open(filepath, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "market_condition", "recommendation",
                    "current_price", "was_followed", "panel_votes",
                ])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                "",
                action,
                "",
                decision,
                "",
            ])
    except Exception as e:
        logger.error("Failed to log approval decision: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_regime_opinion(context: dict) -> dict:
    """
    Query a single preferred panelist for regime interpretation.

    Fallback order:
      1) Kimi-K2.5 (reasoning), when enabled and preferred
      2) GPT-OSS-120B
      3) Llama-70B
      4) Llama-8B

    Returns a validated dict and never raises.
    """
    if not bool(getattr(config, "AI_REGIME_ADVISOR_ENABLED", False)):
        return _default_regime_opinion("disabled")

    try:
        panel = _ordered_regime_panel(_build_panel())
        if not panel:
            return _default_regime_opinion("no_panelists")

        prompt_context = _build_regime_context(context)
        messages = [
            {"role": "system", "content": _REGIME_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(prompt_context, default=str)},
        ]

        last_error = "all_panelists_failed"
        for panelist in panel:
            name = str(panelist.get("name", "")).strip() or "unknown"
            logger.info("AI regime advisor: querying %s...", name)

            response, err = _call_panelist_messages(messages, panelist)
            if not response:
                reason = _clip_text(err or "empty_response", 120)
                last_error = f"{name}:{reason}"
                logger.warning(
                    "AI regime advisor: panelist %s failed (%s), trying next",
                    name,
                    reason,
                )
                continue

            parsed, parse_err = _parse_regime_opinion(response)
            if parse_err:
                last_error = f"{name}:{parse_err}"
                logger.warning(
                    "AI regime advisor: panelist %s returned invalid JSON (%s), trying next",
                    name,
                    parse_err,
                )
                continue

            result = _default_regime_opinion("")
            result.update(parsed)
            result["panelist"] = name
            logger.info(
                "AI regime advisor: %s -> tier %d %s (%d%% conviction)",
                name,
                result["recommended_tier"],
                result["recommended_direction"],
                result["conviction"],
            )
            return result

        return _default_regime_opinion(last_error)

    except Exception as e:
        logger.exception("AI regime advisor failed")
        return _default_regime_opinion(str(e))


def get_recommendation(market_data: dict, stats_context: str = "") -> dict:
    """
    Run the AI council and return the aggregated recommendation.

    Queries all configured panelists with the same prompt, then
    aggregates by majority vote.  Disagreement defaults to "continue"
    (no action), which naturally suppresses the "broken record" problem.

    Args:
        market_data: Dict with keys:
            price, change_1h, change_4h, change_24h,
            spread_pct, recent_fills, grid_center
        stats_context: Optional string of statistical analysis results

    Returns:
        Dict with keys: condition, action, reason, raw,
                        panel_votes, consensus, panel_size, winner_count

    This function NEVER raises -- failures are logged and defaults returned.
    """
    panel = _build_panel()
    if not panel:
        logger.info("AI council: no API keys configured")
        return {
            "condition": "unknown", "action": "continue",
            "reason": "No AI keys configured", "raw": "",
            "panel_votes": [], "consensus": False,
            "panel_size": 0, "winner_count": 0,
        }

    logger.info("Running AI council (%d panelists)...", len(panel))

    prompt = _build_prompt(market_data, stats_context)
    if config.STRATEGY_MODE == "pair":
        pair_display = market_data.get("market", {}).get("pair_display", "DOGE/USD")
    else:
        pair_display = market_data.get("pair_display", "DOGE/USD")
    votes = []

    now = time.time()

    for i, panelist in enumerate(panel):
        name = panelist["name"]

        # Check if this panelist is in cooldown from consecutive failures
        skip_until = _panelist_skip_until.get(name, 0)
        if now < skip_until:
            remaining = int(skip_until - now)
            votes.append({
                "name": name,
                "condition": "skipped",
                "action": "",
                "reason": f"Skipped (cooldown, {remaining}s remaining)",
                "raw": "",
            })
            logger.info("  %s: skipped (cooldown, %ds remaining)", name, remaining)
            continue

        try:
            response, err = _call_panelist(prompt, panelist, pair_display=pair_display)
            if response:
                parsed = _parse_response(response)
                parsed["name"] = name
                votes.append(parsed)
                logger.info(
                    "  %s: %s / %s -- %s",
                    name, parsed["condition"],
                    parsed["action"], parsed["reason"],
                )
                # Reset consecutive fail counter on success
                _panelist_consecutive_fails[name] = 0
            else:
                error_reason = err or "No response"
                votes.append({
                    "name": name,
                    "condition": "error",
                    "action": "",
                    "reason": error_reason,
                    "raw": "",
                })
                logger.warning("  %s: %s", name, error_reason)
                # Track consecutive failure
                fails = _panelist_consecutive_fails.get(name, 0) + 1
                _panelist_consecutive_fails[name] = fails
                if fails >= SKIP_THRESHOLD:
                    _panelist_skip_until[name] = now + SKIP_COOLDOWN
                    logger.warning(
                        "  %s: %d consecutive failures -- skipping for %ds",
                        name, fails, SKIP_COOLDOWN,
                    )

        except Exception as e:
            votes.append({
                "name": name,
                "condition": "error",
                "action": "",
                "reason": str(e),
                "raw": "",
            })
            logger.warning("  %s: error -- %s", name, e)
            fails = _panelist_consecutive_fails.get(name, 0) + 1
            _panelist_consecutive_fails[name] = fails
            if fails >= SKIP_THRESHOLD:
                _panelist_skip_until[name] = now + SKIP_COOLDOWN

        # Brief pause between panelists to respect rate limits
        if i < len(panel) - 1:
            time.sleep(1)

    result = _aggregate_votes(votes)
    _log_recommendation(market_data, result)

    logger.info(
        "AI council verdict: %s (%d/%d) -- %s",
        result["action"].upper(),
        result.get("winner_count", 0),
        result.get("panel_size", len(panel)),
        result["reason"],
    )

    return result


def format_recommendation(parsed: dict) -> str:
    """
    Format the AI council recommendation for status display / logging.
    Shows each panelist's vote and the majority verdict.
    """
    panel_votes = parsed.get("panel_votes", [])

    if not panel_votes:
        if parsed.get("condition") == "unknown":
            return "AI Council: unavailable"
        return (
            f"AI Advisor:\n"
            f"  Market: {parsed['condition']}\n"
            f"  Action: {parsed['action']}\n"
            f"  Reason: {parsed['reason']}"
        )

    lines = ["AI Council:"]
    for v in panel_votes:
        name = v.get("name", "?")
        action = v.get("action", "?") or "error"
        condition = v.get("condition", "?")
        lines.append(f"  {name}: {action} ({condition})")

    action = parsed.get("action", "continue")
    panel_size = parsed.get("panel_size", len(panel_votes))
    winner_count = parsed.get("winner_count", 0)
    lines.append(f"  Verdict: {action.upper()} ({winner_count}/{panel_size})")

    return "\n".join(lines)


def analyze_trade(cycle_data: dict) -> dict:
    """
    Ask the first available AI panelist why a trade lost money.

    Args:
        cycle_data: Dict with trade_id, cycle, entry_side, entry_price,
                    exit_price, volume, net_profit, fees, duration_sec.

    Returns:
        {"analysis": "...", "panelist": "..."} on success,
        {"analysis": "No AI keys configured", "panelist": ""} on failure.
    """
    panel = _build_panel()
    if not panel:
        return {"analysis": "No AI keys configured", "panelist": ""}

    prompt = (
        f"A pair trade just closed at a loss. Analyze why and suggest what "
        f"the bot could do differently.\n\n"
        f"Trade: {cycle_data.get('trade_id', '?')} cycle {cycle_data.get('cycle', 0)}\n"
        f"Side: {cycle_data.get('entry_side', '?')} entry\n"
        f"Entry price: ${cycle_data.get('entry_price', 0):.6f}\n"
        f"Exit price: ${cycle_data.get('exit_price', 0):.6f}\n"
        f"Volume: {cycle_data.get('volume', 0):.2f}\n"
        f"Net P&L: ${cycle_data.get('net_profit', 0):.4f}\n"
        f"Fees: ${cycle_data.get('fees', 0):.4f}\n"
        f"Duration: {cycle_data.get('duration_sec', 0):.0f}s\n\n"
        f"Give a concise 2-3 sentence analysis of why this trade lost money "
        f"and one actionable suggestion. Be specific about the numbers."
    )

    # Try panelists in order until one succeeds
    for panelist in panel:
        now = time.time()
        skip_until = _panelist_skip_until.get(panelist["name"], 0)
        if now < skip_until:
            continue

        response, err = _call_panelist(prompt, panelist)
        if response:
            return {"analysis": response.strip(), "panelist": panelist["name"]}

    return {"analysis": "All AI panelists unavailable", "panelist": ""}
