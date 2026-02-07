"""
config.py -- All tunable parameters for the DOGE grid trading bot.

Every value here is loaded from environment variables so you can configure
the bot via Railway's dashboard (or a local .env file) without touching code.

HOW TO READ THIS FILE:
  Each config value has a comment explaining:
    1. What it controls
    2. What happens if you raise/lower it
    3. The default and why it was chosen
"""

import os
import logging

# ---------------------------------------------------------------------------
# Helper: read an env var with a typed default
# ---------------------------------------------------------------------------

def _env(name, default, cast=str):
    """
    Read an environment variable and cast it to the right type.
    If the var is missing or empty, return *default* (already the right type).
    """
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        # Special handling for booleans -- "true"/"1"/"yes" are all truthy
        if cast is bool:
            return raw.strip().lower() in ("true", "1", "yes")
        return cast(raw)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# API Credentials  (NEVER hard-code these -- always use env vars)
# ---------------------------------------------------------------------------

# Your Kraken API key. Create one at https://www.kraken.com/u/security/api
# Permissions needed: Query Funds, Create & Modify Orders, Cancel/Close Orders, Query Open Orders & Trades
KRAKEN_API_KEY: str = _env("KRAKEN_API_KEY", "")

# Your Kraken private (secret) key -- base64-encoded by Kraken.
KRAKEN_API_SECRET: str = _env("KRAKEN_API_SECRET", "")

# AI Council API keys.  Set one or both for multi-model voting.
# Groq (free tier): Llama 3.3 70B + Llama 3.1 8B
# NVIDIA build.nvidia.com (free tier): Kimi K2.5
GROQ_API_KEY: str = _env("GROQ_API_KEY", "")
NVIDIA_API_KEY: str = _env("NVIDIA_API_KEY", "")

# Legacy fallback -- used only if neither GROQ nor NVIDIA key is set.
AI_API_KEY: str = _env("AI_API_KEY", GROQ_API_KEY or NVIDIA_API_KEY)

# Telegram bot token (from @BotFather) and your chat ID (from @userinfobot).
TELEGRAM_BOT_TOKEN: str = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = _env("TELEGRAM_CHAT_ID", "")

# Supabase (PostgREST) -- cloud persistence for fills, prices, and state.
# Free tier PostgreSQL.  If not set, bot runs fine with local CSV/JSON only.
SUPABASE_URL: str = _env("SUPABASE_URL", "")
SUPABASE_KEY: str = _env("SUPABASE_KEY", "")

# ---------------------------------------------------------------------------
# DRY RUN -- the most important toggle
# ---------------------------------------------------------------------------

# When True (the default!), the bot:
#   - Fetches REAL prices from Kraken's public API
#   - SIMULATES order placement and fills
#   - Logs everything as if real (including fake P&L)
#   - Tags Telegram messages with [DRY RUN]
#
# Set to False ONLY after you've watched dry-run results for several days
# and are confident the strategy works.
DRY_RUN: bool = _env("DRY_RUN", True, bool)

# ---------------------------------------------------------------------------
# Trading pair
# ---------------------------------------------------------------------------

# Kraken's internal ticker for DOGE/USD.
# "XDGUSD" is the pair name used in the REST API.
# The websocket name is "XDG/USD" but we only use REST here.
PAIR: str = "XDGUSD"

# Human-readable name for logs and Telegram messages.
PAIR_DISPLAY: str = "DOGE/USD"

# ---------------------------------------------------------------------------
# Capital & position sizing
# ---------------------------------------------------------------------------

# Total USD you're allocating to grid trading.
# This is NOT your whole DOGE stack -- it's a separate small allocation.
# Raising it: more orders, more profit, more risk.
# Lowering it: fewer orders, less profit, less risk.
STARTING_CAPITAL: float = _env("STARTING_CAPITAL", 120.0, float)

# Dollar value of each individual grid order (initial value).
# Overridden at each grid build by adapt_grid_params() which sizes orders
# near Kraken's 13 DOGE minimum for maximum resolution.
# Set via env var to force a fixed size (adapt will still run but this
# becomes the starting point before first build).
ORDER_SIZE_USD: float = _env("ORDER_SIZE_USD", 3.5, float)

# ---------------------------------------------------------------------------
# Grid geometry
# ---------------------------------------------------------------------------

# Number of grid levels per side (initial value).
# Overridden at each grid build by adapt_grid_params() which maximizes
# levels within capital constraints at the current DOGE price.
# Total orders = GRID_LEVELS * 2, split by trend ratio.
GRID_LEVELS: int = _env("GRID_LEVELS", 10, int)

# Percentage gap between adjacent grid levels.
# At 1.0%: levels are spaced $0.0009 apart when DOGE is $0.09.
# Must be > round-trip fee (0.50%) to be profitable.
# Raising it: more profit per cycle, but fewer fills in calm markets.
# Lowering it: more fills, but less profit each (and fee erosion risk).
GRID_SPACING_PCT: float = _env("GRID_SPACING_PCT", 1.0, float)

# ---------------------------------------------------------------------------
# Fee assumptions
# ---------------------------------------------------------------------------

# Kraken maker fee at the lowest volume tier ($0–$10k/month).
# Maker = limit order that sits on the book (which grid orders do).
# If your 30-day volume crosses $10k, this drops to 0.20%.
MAKER_FEE_PCT: float = 0.25

# Round trip = buy fee + sell fee.
# Used to calculate net profit per grid cycle.
ROUND_TRIP_FEE_PCT: float = MAKER_FEE_PCT * 2  # 0.50%

# ---------------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------------

# STOP FLOOR -- absolute minimum portfolio value (in USD).
# If total value (cash + open order value) drops below this,
# the bot cancels ALL orders and stops trading.
# This is your "circuit breaker" -- it caps max loss at $20 ($120 - $100).
# Raising it: less risk tolerance, bot stops sooner.
# Lowering it: more risk tolerance, bot fights through deeper dips.
STOP_FLOOR: float = _env("STOP_FLOOR", 100.0, float)

# DAILY LOSS LIMIT -- max USD loss in a single calendar day (UTC).
# If cumulative realized losses for the day exceed this, the bot pauses
# until midnight UTC.
# This prevents "death by a thousand cuts" in a sustained crash.
DAILY_LOSS_LIMIT: float = _env("DAILY_LOSS_LIMIT", 3.0, float)

# GRID DRIFT RESET -- percentage price must move from grid center
# before the bot cancels everything and rebuilds the grid.
# At 5%, if DOGE moves from $0.09 to $0.0945 or $0.0855,
# the grid re-centers around the new price.
# Raising it: fewer resets (less fees from cancels), but grid gets stale.
# Lowering it: grid stays tight around price, but more cancel/replace churn.
GRID_DRIFT_RESET_PCT: float = _env("GRID_DRIFT_RESET_PCT", 5.0, float)

# Maximum consecutive API errors before the bot gives up.
# Protects against Kraken outages or network issues draining your rate limit.
MAX_CONSECUTIVE_ERRORS: int = _env("MAX_CONSECUTIVE_ERRORS", 10, int)

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# Main loop poll interval in seconds.
# Every cycle: check fills → place replacement orders → log status.
# 30s is a good balance between responsiveness and rate-limit safety.
# Kraken standard tier: 15 API calls per counter, decays at 1/sec.
# Each cycle uses ~2-4 calls, so 30s keeps us well under the limit.
POLL_INTERVAL_SECONDS: int = _env("POLL_INTERVAL_SECONDS", 30, int)

# How often (in seconds) to run the AI advisor.
# 3600 = once per hour = 24 calls/day, well within Groq free tier.
AI_ADVISOR_INTERVAL: int = _env("AI_ADVISOR_INTERVAL", 3600, int)

# Hour (UTC) to send daily P&L summary via Telegram.
# 0 = midnight UTC.
DAILY_SUMMARY_HOUR_UTC: int = _env("DAILY_SUMMARY_HOUR_UTC", 0, int)

# ---------------------------------------------------------------------------
# DOGE accumulation
# ---------------------------------------------------------------------------

# Monthly USD reserve for hosting costs.
# Profit above this threshold gets converted to DOGE.
MONTHLY_RESERVE_USD: float = _env("MONTHLY_RESERVE_USD", 5.0, float)

# How often (in days) to sweep excess profit into DOGE.
# 7 = weekly sweep.  Keeps the accumulation buys from being too tiny.
ACCUMULATION_SWEEP_DAYS: int = _env("ACCUMULATION_SWEEP_DAYS", 7, int)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Python log level.  DEBUG shows every API call; INFO is normal operations.
LOG_LEVEL: str = _env("LOG_LEVEL", "INFO")

# Directory for CSV trade logs.  Relative to the bot's working directory.
LOG_DIR: str = _env("LOG_DIR", "logs")

# State snapshot file for persistence across restarts.
STATE_FILE: str = os.path.join(LOG_DIR, "state.json")

# ---------------------------------------------------------------------------
# Health-check HTTP server
# ---------------------------------------------------------------------------

# Railway expects a process to bind to a port.
# We run a tiny HTTP server that returns bot status as JSON.
# Set to 0 to disable.
HEALTH_PORT: int = _env("PORT", _env("HEALTH_PORT", 8080, int), int)

# ---------------------------------------------------------------------------
# AI council settings
# ---------------------------------------------------------------------------

# The council queries multiple models and uses majority vote.
# Panel is auto-configured from GROQ_API_KEY / NVIDIA_API_KEY.
# These legacy settings are only used as single-model fallback
# when neither panel key is set.
AI_API_URL: str = _env("AI_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
AI_MODEL: str = _env("AI_MODEL", "meta/llama-3.1-8b-instruct")

# ---------------------------------------------------------------------------
# Startup banner -- printed when the bot launches
# ---------------------------------------------------------------------------

def print_banner():
    """Print a clear summary of all active settings so you know what's running."""
    mode = "DRY RUN (simulated)" if DRY_RUN else "LIVE TRADING (real money!)"
    lines = [
        "",
        "=" * 60,
        "  DOGE GRID TRADING BOT",
        "=" * 60,
        f"  Mode:            {mode}",
        f"  Pair:            {PAIR_DISPLAY}",
        f"  Capital:         ${STARTING_CAPITAL:.2f}",
        f"  Order size:      ${ORDER_SIZE_USD:.2f} (adaptive, min 13 DOGE)",
        f"  Grid levels:     {GRID_LEVELS} per side (adaptive, recalc each build)",
        f"  Grid spacing:    {GRID_SPACING_PCT:.2f}%",
        f"  Net per cycle:   {GRID_SPACING_PCT - ROUND_TRIP_FEE_PCT:.2f}% (after {ROUND_TRIP_FEE_PCT:.2f}% fees)",
        f"  Stop floor:      ${STOP_FLOOR:.2f}",
        f"  Daily loss limit: ${DAILY_LOSS_LIMIT:.2f}",
        f"  Drift reset:     {GRID_DRIFT_RESET_PCT:.1f}%",
        f"  Poll interval:   {POLL_INTERVAL_SECONDS}s",
        f"  AI council:      every {AI_ADVISOR_INTERVAL // 60} min",
        f"  Health port:     {HEALTH_PORT}",
        f"  Log level:       {LOG_LEVEL}",
        f"  Kraken key:      {'configured' if KRAKEN_API_KEY else 'NOT SET'}",
        f"  Groq key:        {'configured' if GROQ_API_KEY else 'NOT SET'}",
        f"  NVIDIA key:      {'configured' if NVIDIA_API_KEY else 'NOT SET'}",
        f"  AI fallback:     {'configured' if AI_API_KEY and not GROQ_API_KEY and not NVIDIA_API_KEY else 'N/A'}",
        f"  Telegram:        {'configured' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else 'NOT SET'}",
        f"  Supabase:        {'configured' if SUPABASE_URL and SUPABASE_KEY else 'NOT SET'}",
        "=" * 60,
        "",
    ]
    print("\n".join(lines))
