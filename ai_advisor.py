"""
ai_advisor.py -- Hourly market analysis via Groq free tier API.

HOW THIS WORKS:
  Every hour, the bot:
    1. Gathers market context (price, changes, spread, fill count)
    2. Sends a compact prompt to Groq's API (Llama 3.1 8B model)
    3. Parses the AI's recommendation
    4. Logs it -- does NOT auto-act on it (v1 is advisory only)

  The AI analyzes whether the market is:
    - Ranging (good for grid trading -- do nothing)
    - Trending up (grid might need to shift upward)
    - Trending down (grid might need to shift, or pause if risk is high)

GROQ FREE TIER:
  - ~30 requests/minute, ~14,400/day
  - We use 1 request/hour = 24/day (well within limits)
  - Model: llama-3.1-8b-instant (fast, free)
  - If Groq fails, we log the error and continue trading -- AI is never a blocker

ZERO DEPENDENCIES:
  Uses urllib.request to POST to Groq's OpenAI-compatible API.
"""

import json
import time
import csv
import os
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)

# Groq's OpenAI-compatible endpoint
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def _build_prompt(market_data: dict) -> str:
    """
    Build a compact prompt for the AI advisor.

    We keep it short to minimize token usage and latency.
    The AI gets: current price, recent changes, spread, fill count.
    """
    return f"""You are a crypto grid trading advisor. Analyze this DOGE/USD market data and give a brief recommendation.

Current price: ${market_data.get('price', 0):.6f}
1h change: {market_data.get('change_1h', 0):.2f}%
4h change: {market_data.get('change_4h', 0):.2f}%
24h change: {market_data.get('change_24h', 0):.2f}%
Bid-ask spread: {market_data.get('spread_pct', 0):.3f}%
Grid fills (last hour): {market_data.get('recent_fills', 0)}
Grid center: ${market_data.get('grid_center', 0):.6f}
Grid spacing: {config.GRID_SPACING_PCT}%

Answer in exactly this format (3 lines only):
CONDITION: [ranging/trending_up/trending_down/volatile/low_volume]
ACTION: [continue/pause/widen_spacing/tighten_spacing/reset_grid]
REASON: [One sentence explanation]"""


def _call_groq(prompt: str) -> str:
    """
    Call Groq's API with a chat completion request.

    Uses urllib.request -- no external dependencies.
    Returns the assistant's response text, or empty string on failure.
    """
    if not config.GROQ_API_KEY:
        logger.debug("Groq API key not set, skipping AI advisor")
        return ""

    payload = json.dumps({
        "model": config.GROQ_MODEL,
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
        "temperature": 0.3,       # Low temperature for consistent analysis
        "max_tokens": 150,         # Keep responses short
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "User-Agent": "DOGEGridBot/1.0",
    }

    req = urllib.request.Request(GROQ_API_URL, data=payload, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            # Extract the assistant message from OpenAI-compatible response
            choices = body.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "").strip()
            return ""

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.warning("Groq API HTTP %d: %s", e.code, error_body[:200])
        return ""

    except urllib.error.URLError as e:
        logger.warning("Groq API connection error: %s", e.reason)
        return ""

    except Exception as e:
        logger.warning("Groq API unexpected error: %s", e)
        return ""


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


def _log_recommendation(market_data: dict, parsed: dict):
    """Log the AI recommendation to CSV for later analysis."""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    filepath = os.path.join(config.LOG_DIR, "ai_recommendations.csv")
    file_exists = os.path.exists(filepath)

    try:
        with open(filepath, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "market_condition", "recommendation",
                    "current_price", "was_followed",
                ])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                parsed["condition"],
                parsed["action"],
                f"{market_data.get('price', 0):.6f}",
                "logged_only",  # v1: never auto-followed
            ])
    except Exception as e:
        logger.error("Failed to write AI recommendation log: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_recommendation(market_data: dict) -> dict:
    """
    Run the AI advisor and return its recommendation.

    Args:
        market_data: Dict with keys:
            price, change_1h, change_4h, change_24h,
            spread_pct, recent_fills, grid_center

    Returns:
        Dict with keys: condition, action, reason, raw
        On failure: returns defaults (condition=unknown, action=continue)

    This function NEVER raises -- failures are logged and defaults returned.
    The trading bot must never stop because the AI advisor had a hiccup.
    """
    logger.info("Running AI advisor analysis...")

    try:
        prompt = _build_prompt(market_data)
        response = _call_groq(prompt)

        if not response:
            logger.info("AI advisor: no response (API key missing or call failed)")
            return {"condition": "unknown", "action": "continue", "reason": "No AI response", "raw": ""}

        parsed = _parse_response(response)
        _log_recommendation(market_data, parsed)

        logger.info(
            "AI advisor: condition=%s, action=%s, reason=%s",
            parsed["condition"], parsed["action"], parsed["reason"],
        )

        return parsed

    except Exception as e:
        # This should never happen (we catch everything above),
        # but defense in depth.
        logger.error("AI advisor unexpected error: %s", e)
        return {"condition": "error", "action": "continue", "reason": str(e), "raw": ""}


def format_recommendation(parsed: dict) -> str:
    """Format the AI recommendation for Telegram/logging display."""
    if not parsed or parsed.get("condition") == "unknown":
        return "AI Advisor: unavailable"

    return (
        f"AI Advisor:\n"
        f"  Market: {parsed['condition']}\n"
        f"  Action: {parsed['action']}\n"
        f"  Reason: {parsed['reason']}"
    )
