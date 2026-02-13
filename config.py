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
import json as _json
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
# Permissions needed: Query Funds, Create & Modify Orders, Cancel/Close Orders,
#   Query Open Orders & Trades, Query Closed Orders & Trades
# IMPORTANT: "Query Closed Orders & Trades" is required for fill detection!
# Without it, QueryOrders won't return filled orders and the bot misses fills.
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
DRY_RUN: bool = _env("DRY_RUN", False, bool)

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

# Capital budget mode: controls how per-pair budgets are managed.
# "off"    = no per-pair budgets (default -- small order sizes are the safety net)
# "auto"   = auto-rebalance across slots (legacy behavior)
# "manual" = per-pair budgets from PAIRS JSON only, no auto-rebalance
CAPITAL_BUDGET_MODE: str = _env("CAPITAL_BUDGET_MODE", "off", str)

# Dollar value of each individual grid order (initial value).
# Base order size in USD.  Defaults to Kraken's $0.50 cost minimum so every
# pair trades at its Kraken minimum volume.  calculate_volume_for_price()
# floors to each pair's min_volume, so this just needs to be <= the cheapest
# pair's minimum cost.  Profits fund additional slots instead of compounding.
ORDER_SIZE_USD: float = _env("ORDER_SIZE_USD", 2.00, float)

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
# Strategy mode
# ---------------------------------------------------------------------------

# "pair" = single-pair market making (2 orders: 1 buy + 1 sell)
# "grid" = full grid ladder (default legacy mode)
# Pair mode caps max exposure at ~1 order size instead of GRID_LEVELS * ORDER_SIZE_USD.
STRATEGY_MODE: str = _env("STRATEGY_MODE", "pair")

# Pair strategy: how far from market to place entry orders (%)
# At 0.2% with DOGE at $0.25, entries are $0.0005 from market.
PAIR_ENTRY_PCT: float = _env("PAIR_ENTRY_PCT", 0.2, float)

# Pair strategy: profit target distance from entry fill price (%)
# Must be > ROUND_TRIP_FEE_PCT (0.50%) or every trade loses money.
# At 1.0%: net ~0.50% per round trip after fees.
PAIR_PROFIT_PCT: float = _env("PAIR_PROFIT_PCT", 1.0, float)

# Pair strategy: refresh entry order when it drifts this far from market (%)
# Exit orders (profit targets) are NEVER refreshed.
PAIR_REFRESH_PCT: float = _env("PAIR_REFRESH_PCT", 1.0, float)

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
MAX_CONSECUTIVE_ERRORS: int = _env("MAX_CONSECUTIVE_ERRORS", 5, int)

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
# DOGE v1 state-machine runtime controls
# ---------------------------------------------------------------------------

# Maximum accepted staleness of market data; new order placement is blocked
# and trading is paused if the latest price is older than this.
STALE_PRICE_MAX_AGE_SEC: int = _env("STALE_PRICE_MAX_AGE_SEC", 60, int)

# Simplified exit lifecycle:
# - S1 stale exit: orphan after this age when market has moved away.
S1_ORPHAN_AFTER_SEC: int = _env("S1_ORPHAN_AFTER_SEC", 600, int)   # 10 min
# - S2 deadlock: orphan worse exit after this age.
S2_ORPHAN_AFTER_SEC: int = _env("S2_ORPHAN_AFTER_SEC", 1800, int)  # 30 min

# Entry anti-loss behavior:
# - At N consecutive losses, widen entry distance (backoff).
# - At M consecutive losses, stop placing new entries for cooldown seconds.
LOSS_BACKOFF_START: int = _env("LOSS_BACKOFF_START", 3, int)
LOSS_COOLDOWN_START: int = _env("LOSS_COOLDOWN_START", 5, int)
LOSS_COOLDOWN_SEC: int = _env("LOSS_COOLDOWN_SEC", 900, int)       # 15 min

# Soft operational limits.
MAX_API_CALLS_PER_LOOP: int = _env("MAX_API_CALLS_PER_LOOP", 10, int)
ORPHAN_PRESSURE_WARN_AT: int = _env("ORPHAN_PRESSURE_WARN_AT", 100, int)
# Capacity telemetry controls for manual scaling.
KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT: int = _env("KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT", 225, int)
OPEN_ORDER_SAFETY_RATIO: float = _env("OPEN_ORDER_SAFETY_RATIO", 0.75, float)
# Alert when Kraken/internal open-order counts diverge for too long.
# This is a persistence canary, not a one-sample spike alarm.
# Auto-soft-close: when capacity utilization >= this %, soft-close farthest
# recovery orders each cycle to prevent hitting the hard order limit.
AUTO_SOFT_CLOSE_CAPACITY_PCT: float = _env("AUTO_SOFT_CLOSE_CAPACITY_PCT", 80.0, float)
AUTO_SOFT_CLOSE_BATCH: int = _env("AUTO_SOFT_CLOSE_BATCH", 2, int)
OPEN_ORDER_DRIFT_ALERT_THRESHOLD: int = _env("OPEN_ORDER_DRIFT_ALERT_THRESHOLD", 10, int)
OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC: int = _env("OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC", 600, int)
OPEN_ORDER_DRIFT_ALERT_COOLDOWN_SEC: int = _env("OPEN_ORDER_DRIFT_ALERT_COOLDOWN_SEC", 1800, int)

# Balance reconciliation: max acceptable drift between account growth and bot P&L.
BALANCE_RECON_DRIFT_PCT: float = _env("BALANCE_RECON_DRIFT_PCT", 2.0, float)

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
# Exit lifecycle management (Section 12: repricing, S2 break-glass, recovery)
# ---------------------------------------------------------------------------

# Enable/disable the exit lifecycle system. When disabled, exits stay as-is.
RECOVERY_ENABLED: bool = _env("RECOVERY_ENABLED", True, bool)

# Max orphaned exits kept on Kraken book per pair.
MAX_RECOVERY_SLOTS: int = _env("MAX_RECOVERY_SLOTS", 2, int)

# Reprice exit after this × median_duration_sec.
EXIT_REPRICE_MULTIPLIER: float = _env("EXIT_REPRICE_MULTIPLIER", 1.5, float)

# Orphan exit after this × median_duration_sec.
EXIT_ORPHAN_MULTIPLIER: float = _env("EXIT_ORPHAN_MULTIPLIER", 5.0, float)

# Max tolerable spread between exits in S2 (%) before break-glass triggers.
S2_MAX_SPREAD_PCT: float = _env("S2_MAX_SPREAD_PCT", 3.0, float)

# Min seconds between reprices of the same exit.
REPRICE_COOLDOWN_SEC: float = _env("REPRICE_COOLDOWN_SEC", 120.0, float)

# Min completed cycles before timing-based logic activates.
MIN_CYCLES_FOR_TIMING: int = _env("MIN_CYCLES_FOR_TIMING", 5, int)

# Entry distance multiplier for with-trend side (0.3-0.8).
# Closer to 0 = more aggressive trend-following.
DIRECTIONAL_ASYMMETRY: float = _env("DIRECTIONAL_ASYMMETRY", 0.5, float)

# Fallback timeout (seconds) when fewer than MIN_CYCLES_FOR_TIMING cycles.
RECOVERY_FALLBACK_TIMEOUT_SEC: float = _env("RECOVERY_FALLBACK_TIMEOUT_SEC", 7200.0, float)

# S2 fallback timeout (seconds) when no PairStats yet.
S2_FALLBACK_TIMEOUT_SEC: float = _env("S2_FALLBACK_TIMEOUT_SEC", 600.0, float)

# Cooldown (seconds) after S2 break-glass fires before it can re-trigger.
S2_COOLDOWN_SEC: float = _env("S2_COOLDOWN_SEC", 300.0, float)

# Max distance (%) an exit can drift from market price before being orphaned.
# If an exit is farther than this from current price, cancel it and re-enter.
# This prevents capital from sitting dead in exits that will never fill.
# At 2.5%: exit at $0.092 with market at $0.096 (4.2% away) gets orphaned.
EXIT_DRIFT_MAX_PCT: float = _env("EXIT_DRIFT_MAX_PCT", 2.5, float)

# Immediately rebalance when entering S1a/S1b (both orders on same side).
# S1a = both buys (no upside capture), S1b = both sells (no downside capture).
# When True: orphan the stranded exit and re-enter, forcing back to S0 (balanced).
# The orphaned exit stays on Kraken as a lottery ticket.
REBALANCE_ON_S1: bool = _env("REBALANCE_ON_S1", True, bool)

# ---------------------------------------------------------------------------
# Entry backoff after consecutive losses
# ---------------------------------------------------------------------------

# Widen entry distance after consecutive losing cycles on a trade leg.
# Formula: effective_entry = entry_pct * min(1 + FACTOR * losses, MAX_MULTIPLIER)
# 0 losses -> 1.0x, 1 loss -> 1.5x, 2 -> 2.0x, ... capped at MAX_MULTIPLIER.
ENTRY_BACKOFF_ENABLED: bool = _env("ENTRY_BACKOFF_ENABLED", True, bool)
ENTRY_BACKOFF_FACTOR: float = _env("ENTRY_BACKOFF_FACTOR", 0.5, float)
ENTRY_BACKOFF_MAX_MULTIPLIER: float = _env("ENTRY_BACKOFF_MAX_MULTIPLIER", 5.0, float)

# ---------------------------------------------------------------------------
# Volatility-aware profit targets
# ---------------------------------------------------------------------------

# Auto-adjust profit_pct based on OHLC volatility so exit targets are reachable.
VOLATILITY_AUTO_PROFIT: bool = _env("VOLATILITY_AUTO_PROFIT", True, bool)
VOLATILITY_PROFIT_FACTOR: float = _env("VOLATILITY_PROFIT_FACTOR", 0.8, float)
VOLATILITY_PROFIT_FLOOR: float = _env("VOLATILITY_PROFIT_FLOOR", 1.0, float)  # DOGE-like margins
VOLATILITY_PROFIT_CEILING: float = _env("VOLATILITY_PROFIT_CEILING", 3.0, float)
VOLATILITY_PROFIT_MIN_CHANGE: float = _env("VOLATILITY_PROFIT_MIN_CHANGE", 0.05, float)

# Squeeze profit target when market is directional (trend_ratio deviates from 0.5).
# 0.0 = disabled (no squeeze), 1.0 = full squeeze (halves target at max directionality).
DIRECTIONAL_SQUEEZE: float = _env("DIRECTIONAL_SQUEEZE", 0.5, float)

# ---------------------------------------------------------------------------
# AI auto-execute
# ---------------------------------------------------------------------------

# Allow the AI council to auto-execute safe (conservative) actions without
# Telegram approval.  Only widen_entry and widen_spacing auto-execute.
AI_AUTO_EXECUTE: bool = _env("AI_AUTO_EXECUTE", True, bool)

# ---------------------------------------------------------------------------
# Swarm scanner settings
# ---------------------------------------------------------------------------

# Quote currencies considered safe (lottery recovery on orphan exit).
SWARM_SAFE_QUOTES: str = _env("SWARM_SAFE_QUOTES", "USD,JPY", str)

# Quote currencies acceptable but riskier (force-liquidate on orphan exit).
SWARM_ACCEPTABLE_QUOTES: str = _env("SWARM_ACCEPTABLE_QUOTES", "EUR,GBP,USDT,USDC,DOGE", str)

# Minimum 24h USD volume for a pair to appear in scanner results.
SWARM_MIN_VOLUME_USD: float = _env("SWARM_MIN_VOLUME_USD", 100000, float)

# Maximum spread (%) for a pair to be eligible.
SWARM_MAX_SPREAD_PCT: float = _env("SWARM_MAX_SPREAD_PCT", 0.30, float)

# Diversity cap: max pairs sharing the same base asset in top-N selection.
SWARM_MAX_PER_BASE: int = _env("SWARM_MAX_PER_BASE", 2, int)

# Global daily loss limit across all swarm pairs (USD).
# If aggregate daily losses exceed this, non-exempt pairs are paused.
SWARM_DAILY_LOSS_LIMIT: float = _env("SWARM_DAILY_LOSS_LIMIT", 10.0, float)

# Pairs exempt from swarm-level pausing (comma-separated Kraken pair names).
SWARM_EXEMPT_PAIRS: list = _env("SWARM_EXEMPT_PAIRS", "XDGUSD", str).split(",")

# ---------------------------------------------------------------------------
# Auto-slot scaling (profits fund additional A/B slots)
# ---------------------------------------------------------------------------

# Net profit (USD) needed per additional slot.  At $5: 1 slot at $0, 2 at $5, 3 at $10.
SLOT_PROFIT_THRESHOLD: float = _env("SLOT_PROFIT_THRESHOLD", 5.0, float)

# Maximum auto-slots per base pair (including the original).
MAX_AUTO_SLOTS: int = _env("MAX_AUTO_SLOTS", 5, int)

# ---------------------------------------------------------------------------
# Multi-pair configuration
# ---------------------------------------------------------------------------

class PairConfig:
    """Per-pair configuration for multi-pair mode.

    Each instance holds the trading parameters for one Kraken pair.
    When PAIRS env var is absent, a single PairConfig is auto-built
    from the existing single-pair env vars (backward compatible).
    """
    def __init__(self, pair: str, display: str, entry_pct: float,
                 profit_pct: float, refresh_pct: float = 1.0,
                 order_size_usd: float = 0.50, daily_loss_limit: float = 3.0,
                 stop_floor: float = 100.0, min_volume: float = 13,
                 price_decimals: int = 6, volume_decimals: int = 0,
                 filter_strings: list = None, recovery_mode: str = "lottery",
                 capital_budget_usd: float = 0.0):
        self.pair = pair
        self.display = display
        self.entry_pct = entry_pct
        self.profit_pct = profit_pct
        self.refresh_pct = refresh_pct
        self.order_size_usd = order_size_usd
        self.daily_loss_limit = daily_loss_limit
        self.stop_floor = stop_floor
        self.min_volume = min_volume
        self.price_decimals = price_decimals
        self.volume_decimals = volume_decimals
        self.filter_strings = filter_strings or [pair[:3].upper()]
        self.recovery_mode = recovery_mode  # "lottery" or "liquidate"
        self.capital_budget_usd = capital_budget_usd  # 0 = unlimited (backward compat)
        self.slot_count = 1  # number of independent A/B trade slots (1 = single)
        self.peak_slot_count = 1  # high-water mark: auto-grow but never auto-shrink

        # Validate critical parameters
        if self.entry_pct <= 0:
            raise ValueError(f"entry_pct must be positive, got {self.entry_pct}")
        if self.profit_pct <= 0:
            raise ValueError(f"profit_pct must be positive, got {self.profit_pct}")
        if self.order_size_usd <= 0:
            raise ValueError(f"order_size_usd must be positive, got {self.order_size_usd}")

    def to_dict(self) -> dict:
        """Serialize for persistence."""
        return {
            "pair": self.pair,
            "display": self.display,
            "entry_pct": self.entry_pct,
            "profit_pct": self.profit_pct,
            "refresh_pct": self.refresh_pct,
            "order_size_usd": self.order_size_usd,
            "daily_loss_limit": self.daily_loss_limit,
            "stop_floor": self.stop_floor,
            "min_volume": self.min_volume,
            "price_decimals": self.price_decimals,
            "volume_decimals": self.volume_decimals,
            "filter_strings": self.filter_strings,
            "recovery_mode": self.recovery_mode,
            "capital_budget_usd": self.capital_budget_usd,
            "slot_count": self.slot_count,
            "peak_slot_count": self.peak_slot_count,
        }

    @staticmethod
    def from_dict(d: dict) -> "PairConfig":
        _logger = logging.getLogger("config")
        try:
            entry_pct = d.get("entry_pct", PAIR_ENTRY_PCT)
            profit_pct = d.get("profit_pct", PAIR_PROFIT_PCT)
            order_size = d.get("order_size_usd", ORDER_SIZE_USD)
            refresh_pct = d.get("refresh_pct", PAIR_REFRESH_PCT)
            recovery_mode = d.get("recovery_mode", "lottery")
            capital_budget = d.get("capital_budget_usd", 0.0)
            slot_count = d.get("slot_count", 1)
            # Clamp bad persisted values
            if entry_pct <= 0:
                _logger.warning("Clamping bad entry_pct=%.4f to 0.01 for %s", entry_pct, d.get("pair"))
                entry_pct = max(0.01, entry_pct)
            if profit_pct <= 0:
                _logger.warning("Clamping bad profit_pct=%.4f to 0.01 for %s", profit_pct, d.get("pair"))
                profit_pct = max(0.01, profit_pct)
            if order_size <= 0:
                _logger.warning("Clamping bad order_size_usd=%.4f to 0.50 for %s", order_size, d.get("pair"))
                order_size = max(0.50, order_size)
            if refresh_pct <= 0:
                _logger.warning("Clamping bad refresh_pct=%.4f to 0.10 for %s", refresh_pct, d.get("pair"))
                refresh_pct = max(0.10, refresh_pct)
            if recovery_mode not in ("lottery", "liquidate"):
                _logger.warning("Unknown recovery_mode=%r for %s; defaulting to lottery", recovery_mode, d.get("pair"))
                recovery_mode = "lottery"
            try:
                capital_budget = float(capital_budget)
            except (ValueError, TypeError):
                _logger.warning("Invalid capital_budget_usd=%r for %s; defaulting to 0", capital_budget, d.get("pair"))
                capital_budget = 0.0
            if capital_budget < 0:
                _logger.warning("Clamping negative capital_budget_usd=%.4f to 0 for %s", capital_budget, d.get("pair"))
                capital_budget = 0.0
            try:
                slot_count = int(slot_count)
            except (ValueError, TypeError):
                _logger.warning("Invalid slot_count=%r for %s; defaulting to 1", slot_count, d.get("pair"))
                slot_count = 1
            if slot_count < 1:
                _logger.warning("Clamping slot_count=%d to 1 for %s", slot_count, d.get("pair"))
                slot_count = 1
            pc = PairConfig(
                pair=d["pair"],
                display=d.get("display", d["pair"]),
                entry_pct=entry_pct,
                profit_pct=profit_pct,
                refresh_pct=refresh_pct,
                order_size_usd=order_size,
                daily_loss_limit=d.get("daily_loss_limit", DAILY_LOSS_LIMIT),
                stop_floor=d.get("stop_floor", STOP_FLOOR),
                min_volume=d.get("min_volume", 13),
                price_decimals=d.get("price_decimals", 6),
                volume_decimals=d.get("volume_decimals", 0),
                filter_strings=d.get("filter_strings"),
                recovery_mode=recovery_mode,
                capital_budget_usd=capital_budget,
            )
            pc.slot_count = slot_count
            pc.peak_slot_count = max(slot_count, d.get("peak_slot_count", 1))
            return pc
        except (KeyError, TypeError, ValueError) as e:
            _logger.error("PairConfig.from_dict failed for %s: %s -- using defaults", d.get("pair", "?"), e)
            return PairConfig(
                pair=d.get("pair", "UNKNOWN"),
                display=d.get("display", d.get("pair", "UNKNOWN")),
                entry_pct=PAIR_ENTRY_PCT,
                profit_pct=PAIR_PROFIT_PCT,
            )


def _build_pairs() -> dict:
    """
    Parse PAIRS env var (JSON array) into a dict of pair_name -> PairConfig.
    If PAIRS env var is absent, auto-build a single entry from existing globals.
    """
    raw = os.environ.get("PAIRS", "")
    if raw:
        try:
            items = _json.loads(raw)
            pairs = {}
            for item in items:
                pc = PairConfig(
                    pair=item["pair"],
                    display=item.get("display", item["pair"]),
                    entry_pct=item.get("entry_pct", PAIR_ENTRY_PCT),
                    profit_pct=item.get("profit_pct", PAIR_PROFIT_PCT),
                    refresh_pct=item.get("refresh_pct", PAIR_REFRESH_PCT),
                    order_size_usd=item.get("order_size_usd", ORDER_SIZE_USD),
                    daily_loss_limit=item.get("daily_loss_limit", DAILY_LOSS_LIMIT),
                    stop_floor=item.get("stop_floor", STOP_FLOOR),
                    min_volume=item.get("min_volume", 13),
                    price_decimals=item.get("price_decimals", 6),
                    volume_decimals=item.get("volume_decimals", 0),
                    filter_strings=item.get("filter_strings"),
                    recovery_mode=item.get("recovery_mode", "lottery"),
                    capital_budget_usd=item.get("capital_budget_usd", 0.0),
                )
                try:
                    pc.slot_count = max(1, int(item.get("slot_count", 1)))
                except (ValueError, TypeError):
                    pc.slot_count = 1
                pairs[pc.pair] = pc
            if pairs:
                return pairs
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to parse PAIRS env var: %s -- falling back to single pair", e)

    # Fallback: build single entry from existing globals
    return {
        PAIR: PairConfig(
            pair=PAIR,
            display=PAIR_DISPLAY,
            entry_pct=PAIR_ENTRY_PCT,
            profit_pct=PAIR_PROFIT_PCT,
            refresh_pct=PAIR_REFRESH_PCT,
            order_size_usd=ORDER_SIZE_USD,
            daily_loss_limit=DAILY_LOSS_LIMIT,
            stop_floor=STOP_FLOOR,
            min_volume=13,
            price_decimals=6,
            volume_decimals=0,
            filter_strings=["XDG", "DOGE"],
        ),
    }


PAIRS: dict = _build_pairs()


# ---------------------------------------------------------------------------
# Startup banner -- printed when the bot launches
# ---------------------------------------------------------------------------

def print_banner():
    """Print a clear summary of all active settings so you know what's running."""
    mode = "DRY RUN (simulated)" if DRY_RUN else "LIVE TRADING (real money!)"
    strategy = "PAIR (2 orders)" if STRATEGY_MODE == "pair" else "GRID (full ladder)"
    lines = [
        "",
        "=" * 60,
        "  GRID TRADING BOT",
        "=" * 60,
        f"  Mode:            {mode}",
        f"  Strategy:        {strategy}",
        f"  Pairs:           {', '.join(pc.display for pc in PAIRS.values())}",
        f"  Capital:         ${STARTING_CAPITAL:.2f}",
        f"  Order size:      ${ORDER_SIZE_USD:.2f} base (floors to Kraken min per pair)",
    ]
    if STRATEGY_MODE == "pair":
        net_pair = PAIR_PROFIT_PCT - ROUND_TRIP_FEE_PCT
        lines += [
            f"  Entry distance:  {PAIR_ENTRY_PCT:.2f}%",
            f"  Profit target:   {PAIR_PROFIT_PCT:.2f}%",
            f"  Net per cycle:   {net_pair:.2f}% (after {ROUND_TRIP_FEE_PCT:.2f}% fees)",
            f"  Refresh drift:   {PAIR_REFRESH_PCT:.2f}%",
            f"  Max exposure:    ~${ORDER_SIZE_USD:.2f} (1 buy order)",
        ]
    else:
        lines += [
            f"  Grid levels:     {GRID_LEVELS} per side (adaptive, recalc each build)",
            f"  Grid spacing:    {GRID_SPACING_PCT:.2f}%",
            f"  Net per cycle:   {GRID_SPACING_PCT - ROUND_TRIP_FEE_PCT:.2f}% (after {ROUND_TRIP_FEE_PCT:.2f}% fees)",
            f"  Drift reset:     {GRID_DRIFT_RESET_PCT:.1f}%",
        ]
    lines += [
        f"  Stop floor:      ${STOP_FLOOR:.2f}",
        f"  Daily loss limit: ${DAILY_LOSS_LIMIT:.2f}",
        f"  Poll interval:   {POLL_INTERVAL_SECONDS}s",
        f"  AI council:      manual (/check or dashboard)",
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
