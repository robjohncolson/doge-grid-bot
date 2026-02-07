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
  - Groq (free tier): Llama 3.3 70B + Llama 3.1 8B
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

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Council panel definitions
# ---------------------------------------------------------------------------

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# Each tuple: (display_name, model_id, is_reasoning_model)
GROQ_PANELISTS = [
    ("Llama-70B", "llama-3.3-70b-versatile", False),
    ("Llama-8B", "llama-3.1-8b-instant", False),
]

NVIDIA_PANELISTS = [
    ("Kimi-K2.5", "moonshotai/kimi-k2.5", True),
]

# Reasoning models need more tokens (chain-of-thought + answer)
_REASONING_MAX_TOKENS = 2048
_INSTRUCT_MAX_TOKENS = 200


def _build_panel() -> list:
    """
    Build the AI council based on available API keys.

    Returns a list of panelist dicts.  Auto-configures:
      - GROQ_API_KEY  -> Llama 3.3 70B + Llama 3.1 8B
      - NVIDIA_API_KEY -> Kimi K2.5
      - Both keys     -> all three (best diversity)
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
    prompt = f"""You are a crypto grid trading advisor. Analyze this DOGE/USD market data and give a brief recommendation.

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

def _call_panelist(prompt: str, panelist: dict) -> str:
    """
    Call a single AI panelist and return the raw response text.

    Supports any OpenAI-compatible endpoint (NVIDIA, Groq, etc.).
    Handles reasoning models (Kimi K2.5) that may put the answer
    in reasoning_content instead of content.

    Returns the assistant's response text, or empty string on failure.
    """
    payload = json.dumps({
        "model": panelist["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a concise crypto market analyst. "
                    "Give structured recommendations for a DOGE/USD grid trading bot. "
                    "Be direct and specific. Never recommend buying or selling -- "
                    "only recommend grid parameter adjustments."
                ),
            },
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
        with urllib.request.urlopen(req, timeout=30) as resp:
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
                return content.strip() if content else ""
            return ""

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.warning(
            "%s HTTP %d: %s", panelist["name"], e.code, error_body[:200],
        )
        return ""

    except urllib.error.URLError as e:
        logger.warning("%s connection error: %s", panelist["name"], e.reason)
        return ""

    except Exception as e:
        logger.warning("%s error: %s", panelist["name"], e)
        return ""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(response: str) -> dict:
    """
    Parse the structured AI response into a dict.

    Expected format:
      CONDITION: ranging
      ACTION: continue
      REASON: Price is oscillating within 2% range, ideal for grid trading.

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

    for line in response.strip().split("\n"):
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

    if has_majority:
        final_action = winner_action
        # Use reason from the first voter who picked the winner
        reason = next(
            v["reason"] for v in valid if v["action"] == final_action
        )
    else:
        final_action = "continue"
        # Summarize the split
        split = ", ".join(
            f"{count}x {act}" for act, count in action_counts.most_common()
        )
        reason = f"Council split ({split}) -- no majority"

    # Majority condition (for display)
    condition_counts = Counter(v["condition"] for v in valid)
    final_condition = condition_counts.most_common(1)[0][0]

    return {
        "condition": final_condition,
        "action": final_action,
        "reason": reason,
        "raw": "",
        "panel_votes": votes,
        "vote_counts": dict(action_counts),
        "consensus": has_majority,
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
    votes = []

    for i, panelist in enumerate(panel):
        try:
            response = _call_panelist(prompt, panelist)
            if response:
                parsed = _parse_response(response)
                parsed["name"] = panelist["name"]
                votes.append(parsed)
                logger.info(
                    "  %s: %s / %s -- %s",
                    panelist["name"], parsed["condition"],
                    parsed["action"], parsed["reason"],
                )
            else:
                votes.append({
                    "name": panelist["name"],
                    "condition": "error",
                    "action": "",
                    "reason": "No response",
                    "raw": "",
                })
                logger.warning("  %s: no response", panelist["name"])

        except Exception as e:
            votes.append({
                "name": panelist["name"],
                "condition": "error",
                "action": "",
                "reason": str(e),
                "raw": "",
            })
            logger.warning("  %s: error -- %s", panelist["name"], e)

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
