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

# Panelist timeout skip tracking
_panelist_consecutive_fails: dict = {}   # name -> int
_panelist_skip_until: dict = {}          # name -> timestamp
SKIP_THRESHOLD = 3        # consecutive failures before skipping
SKIP_COOLDOWN = 3600      # seconds to skip a panelist (1 hour)


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
