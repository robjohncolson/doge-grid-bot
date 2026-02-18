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
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        # Special handling for booleans -- "true"/"1"/"yes" are all truthy
        if cast is bool:
            return raw.strip().lower() in ("true", "1", "yes")
        return cast(raw)
    except (ValueError, TypeError):
        return default


def _env_float_list(name: str, default: list[float], expected_len: int) -> list[float]:
    """
    Read a float list from env. Accepts either JSON list or comma-separated text.
    Falls back to default when parsing fails or length is incorrect.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        vals = list(default)
    else:
        parsed: list[float] = []
        used_json = False
        if raw.startswith("[") and raw.endswith("]"):
            try:
                loaded = _json.loads(raw)
            except Exception:
                loaded = None
            if isinstance(loaded, list):
                used_json = True
                for item in loaded:
                    try:
                        parsed.append(float(item))
                    except (TypeError, ValueError):
                        parsed = []
                        break
        if not used_json:
            for tok in raw.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    parsed.append(float(tok))
                except (TypeError, ValueError):
                    parsed = []
                    break
        vals = parsed

    clean = [max(0.0, float(v)) for v in vals if isinstance(v, (int, float))]
    if len(clean) != int(expected_len):
        return list(default)
    if sum(clean) <= 1e-12:
        return list(default)
    return clean


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

# AI Council API keys. Set any subset for multi-provider fallback.
# SambaNova (free tier): DeepSeek R1 + DeepSeek V3.1
# Cerebras (free tier): Qwen3-235B + GPT-OSS-120B
# Groq (free tier): Llama 3.3 70B + Llama 3.1 8B
# NVIDIA build.nvidia.com (free tier): Kimi K2.5
SAMBANOVA_API_KEY: str = _env("SAMBANOVA_API_KEY", "")
CEREBRAS_API_KEY: str = _env("CEREBRAS_API_KEY", "")
GROQ_API_KEY: str = _env("GROQ_API_KEY", "")
NVIDIA_API_KEY: str = _env("NVIDIA_API_KEY", "")

# Legacy fallback -- used only if no provider key is set.
AI_API_KEY: str = _env(
    "AI_API_KEY",
    SAMBANOVA_API_KEY or CEREBRAS_API_KEY or GROQ_API_KEY or NVIDIA_API_KEY,
)

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

# Capital layers (manual vertical sizing).
# Each layer adds this much DOGE to every new order's target size.
CAPITAL_LAYER_DOGE_PER_ORDER: float = _env("CAPITAL_LAYER_DOGE_PER_ORDER", 1.0, float)

# Commitment unit denominator for layer preview/validation.
# 225 matches configured per-pair maximum open orders.
CAPITAL_LAYER_ORDER_BUDGET: int = _env("CAPITAL_LAYER_ORDER_BUDGET", 225, int)

# Safety haircut for layer affordability checks.
CAPITAL_LAYER_BALANCE_BUFFER: float = _env("CAPITAL_LAYER_BALANCE_BUFFER", 1.03, float)

# Hard upper bound for manual layer target increments.
CAPITAL_LAYER_MAX_TARGET_LAYERS: int = _env("CAPITAL_LAYER_MAX_TARGET_LAYERS", 20, int)

# Default funding source for add-layer action.
_CAPITAL_LAYER_DEFAULT_SOURCE_RAW: str = _env("CAPITAL_LAYER_DEFAULT_SOURCE", "AUTO", str)
CAPITAL_LAYER_DEFAULT_SOURCE: str = str(_CAPITAL_LAYER_DEFAULT_SOURCE_RAW).strip().upper()
if CAPITAL_LAYER_DEFAULT_SOURCE not in {"AUTO", "DOGE", "USD"}:
    CAPITAL_LAYER_DEFAULT_SOURCE = "AUTO"

# Doge-themed slot aliases shown in dashboard/log display.
_SLOT_ALIAS_POOL_DEFAULT = [
    "wow", "such", "much", "very", "many", "so", "amaze", "plz",
    "coin", "moon", "hodl", "treat", "shibe", "bork", "snoot", "floof",
    "smol", "boop", "wag", "zoom", "paws", "mlem", "blep", "sniff",
]
_SLOT_ALIAS_POOL_RAW: str = _env("SLOT_ALIAS_POOL", ",".join(_SLOT_ALIAS_POOL_DEFAULT), str)
SLOT_ALIAS_POOL: list[str] = [s.strip().lower() for s in _SLOT_ALIAS_POOL_RAW.split(",") if s.strip()]
if not SLOT_ALIAS_POOL:
    SLOT_ALIAS_POOL = list(_SLOT_ALIAS_POOL_DEFAULT)

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
# Durable profit/exit accounting toggles
# ---------------------------------------------------------------------------

# Capture/propagate settlement metadata (actual Kraken fee/cost).
# Safe default ON: this augments state, it does not force PnL cutover alone.
DURABLE_SETTLEMENT_ENABLED: bool = _env("DURABLE_SETTLEMENT_ENABLED", True, bool)

# Derive persisted total_profit_usd from cycle ledger + base watermark.
# Default OFF for staged rollout.
DURABLE_PROFIT_DERIVATION: bool = _env("DURABLE_PROFIT_DERIVATION", False, bool)

# Quote-first B-side allocation cutover (bot.py allocator path).
# Default OFF until shadow comparisons are stable.
QUOTE_FIRST_ALLOCATION: bool = _env("QUOTE_FIRST_ALLOCATION", False, bool)

# Recycle per-side rounding residual into subsequent entry sizing.
# Default OFF for staged rollout.
ROUNDING_RESIDUAL_ENABLED: bool = _env("ROUNDING_RESIDUAL_ENABLED", False, bool)

# Auto-detect effective maker fee tier from observed (actual_fee / actual_cost).
# Default OFF to keep fallback strictly config-driven unless enabled.
FEE_TIER_AUTO_DETECT: bool = _env("FEE_TIER_AUTO_DETECT", False, bool)

# Reserve this much USD from deployable quote to avoid overshoot on rounding/slippage.
ALLOCATION_SAFETY_BUFFER_USD: float = _env("ALLOCATION_SAFETY_BUFFER_USD", 0.50, float)

# Rolling window for observed fee-rate samples when auto-detect is enabled.
FEE_OBSERVATION_WINDOW: int = _env("FEE_OBSERVATION_WINDOW", 100, int)

# Threshold for operator warnings when configured and observed fee rates diverge.
FEE_MISMATCH_THRESHOLD_PCT: float = _env("FEE_MISMATCH_THRESHOLD_PCT", 10.0, float)

# Residual clamp as a fraction of ORDER_SIZE_USD per side (e.g. 0.25 = 25% cap).
ROUNDING_RESIDUAL_CAP_PCT: float = _env("ROUNDING_RESIDUAL_CAP_PCT", 0.25, float)

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

# AI Regime Advisor controls (HMM second-opinion layer).
AI_REGIME_ADVISOR_ENABLED: bool = _env("AI_REGIME_ADVISOR_ENABLED", False, bool)
AI_REGIME_INTERVAL_SEC: float = _env("AI_REGIME_INTERVAL_SEC", 300.0, float)
AI_REGIME_DEBOUNCE_SEC: float = _env("AI_REGIME_DEBOUNCE_SEC", 60.0, float)
AI_OVERRIDE_TTL_SEC: int = _env("AI_OVERRIDE_TTL_SEC", 1800, int)
AI_OVERRIDE_MIN_TTL_SEC: int = _env("AI_OVERRIDE_MIN_TTL_SEC", 300, int)  # 5 min floor
AI_OVERRIDE_MAX_TTL_SEC: int = _env("AI_OVERRIDE_MAX_TTL_SEC", 3600, int)
AI_OVERRIDE_MIN_CONVICTION: int = _env("AI_OVERRIDE_MIN_CONVICTION", 50, int)
AI_REGIME_HISTORY_SIZE: int = _env("AI_REGIME_HISTORY_SIZE", 12, int)
AI_REGIME_PREFER_REASONING: bool = _env("AI_REGIME_PREFER_REASONING", True, bool)

# DeepSeek regime-advisor provider settings (strategic capital deployment).
DEEPSEEK_API_KEY: str = _env("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL: str = _env("DEEPSEEK_MODEL", "deepseek-chat", str)
DEEPSEEK_TIMEOUT_SEC: int = _env("DEEPSEEK_TIMEOUT_SEC", 30, int)
AI_REGIME_PREFER_DEEPSEEK_R1: bool = _env("AI_REGIME_PREFER_DEEPSEEK_R1", False, bool)

# Strategic accumulation engine (AI + 1h HMM gated).
ACCUM_ENABLED: bool = _env("ACCUM_ENABLED", False, bool)
ACCUM_MIN_CONVICTION: int = _env("ACCUM_MIN_CONVICTION", 60, int)
ACCUM_RESERVE_USD: float = _env("ACCUM_RESERVE_USD", 50.0, float)
ACCUM_MAX_BUDGET_USD: float = _env("ACCUM_MAX_BUDGET_USD", 50.0, float)
ACCUM_CHUNK_USD: float = _env("ACCUM_CHUNK_USD", 2.0, float)
ACCUM_INTERVAL_SEC: float = _env("ACCUM_INTERVAL_SEC", 120.0, float)
ACCUM_MAX_DRAWDOWN_PCT: float = _env("ACCUM_MAX_DRAWDOWN_PCT", 3.0, float)
ACCUM_COOLDOWN_SEC: float = _env("ACCUM_COOLDOWN_SEC", 3600.0, float)
ACCUM_CONFIRMATION_CANDLES: int = _env("ACCUM_CONFIRMATION_CANDLES", 2, int)

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
S1_ORPHAN_AFTER_SEC: int = _env("S1_ORPHAN_AFTER_SEC", 1350, int)  # 22.5 min
# - S2 deadlock: orphan worse exit after this age.
S2_ORPHAN_AFTER_SEC: int = _env("S2_ORPHAN_AFTER_SEC", 1800, int)  # 30 min

# Sticky slots mode: keep exits waiting (no timer orphaning in reducer path).
STICKY_MODE_ENABLED: bool = _env("STICKY_MODE_ENABLED", False, bool)
STICKY_TARGET_SLOTS: int = _env("STICKY_TARGET_SLOTS", 80, int)
STICKY_MAX_TARGET_SLOTS: int = _env("STICKY_MAX_TARGET_SLOTS", 100, int)

# Self-healing position ledger (local-first, optional Supabase replication).
POSITION_LEDGER_ENABLED: bool = _env("POSITION_LEDGER_ENABLED", True, bool)
POSITION_JOURNAL_LOCAL_LIMIT: int = _env("POSITION_JOURNAL_LOCAL_LIMIT", 500, int)
if POSITION_JOURNAL_LOCAL_LIMIT < 50:
    POSITION_JOURNAL_LOCAL_LIMIT = 50

# Distance-weighted age bands for self-healing exits.
AGE_BAND_FRESH_SEC: int = _env("AGE_BAND_FRESH_SEC", 21600, int)     # 6h
AGE_BAND_AGING_SEC: int = _env("AGE_BAND_AGING_SEC", 86400, int)     # 24h
AGE_BAND_STALE_SEC: int = _env("AGE_BAND_STALE_SEC", 259200, int)    # 72h
AGE_BAND_STUCK_SEC: int = _env("AGE_BAND_STUCK_SEC", 604800, int)    # 168h
if AGE_BAND_FRESH_SEC < 60:
    AGE_BAND_FRESH_SEC = 60
if AGE_BAND_AGING_SEC <= AGE_BAND_FRESH_SEC:
    AGE_BAND_AGING_SEC = AGE_BAND_FRESH_SEC + 60
if AGE_BAND_STALE_SEC <= AGE_BAND_AGING_SEC:
    AGE_BAND_STALE_SEC = AGE_BAND_AGING_SEC + 60
if AGE_BAND_STUCK_SEC <= AGE_BAND_STALE_SEC:
    AGE_BAND_STUCK_SEC = AGE_BAND_STALE_SEC + 60
AGE_DISTANCE_WEIGHT: float = _env("AGE_DISTANCE_WEIGHT", 5.0, float)
if AGE_DISTANCE_WEIGHT <= 0:
    AGE_DISTANCE_WEIGHT = 5.0

# Subsidy repricing controls.
SUBSIDY_ENABLED: bool = _env("SUBSIDY_ENABLED", False, bool)
SUBSIDY_REPRICE_INTERVAL_SEC: int = _env("SUBSIDY_REPRICE_INTERVAL_SEC", 3600, int)
if SUBSIDY_REPRICE_INTERVAL_SEC < 60:
    SUBSIDY_REPRICE_INTERVAL_SEC = 60
_SUBSIDY_AUTO_REPRICE_BAND_RAW: str = _env("SUBSIDY_AUTO_REPRICE_BAND", "stuck", str)
SUBSIDY_AUTO_REPRICE_BAND: str = str(_SUBSIDY_AUTO_REPRICE_BAND_RAW).strip().lower()
if SUBSIDY_AUTO_REPRICE_BAND not in {"stale", "stuck", "write_off"}:
    SUBSIDY_AUTO_REPRICE_BAND = "stuck"
SUBSIDY_WRITE_OFF_AUTO: bool = _env("SUBSIDY_WRITE_OFF_AUTO", False, bool)

# Churner mode controls (regime-gated, self-healing helper cycles).
CHURNER_ENABLED: bool = _env("CHURNER_ENABLED", False, bool)
CHURNER_ENTRY_PCT: float = _env("CHURNER_ENTRY_PCT", 0.15, float)
if CHURNER_ENTRY_PCT <= 0:
    CHURNER_ENTRY_PCT = 0.15
CHURNER_PROFIT_PCT: float = _env("CHURNER_PROFIT_PCT", ROUND_TRIP_FEE_PCT + 0.10, float)
if CHURNER_PROFIT_PCT <= ROUND_TRIP_FEE_PCT:
    CHURNER_PROFIT_PCT = ROUND_TRIP_FEE_PCT + 0.10
CHURNER_ORDER_SIZE_USD: float = _env("CHURNER_ORDER_SIZE_USD", ORDER_SIZE_USD, float)
if CHURNER_ORDER_SIZE_USD <= 0:
    CHURNER_ORDER_SIZE_USD = ORDER_SIZE_USD
CHURNER_TIMEOUT_SEC: int = _env("CHURNER_TIMEOUT_SEC", 300, int)
if CHURNER_TIMEOUT_SEC < 60:
    CHURNER_TIMEOUT_SEC = 60
CHURNER_EXIT_TIMEOUT_SEC: int = _env("CHURNER_EXIT_TIMEOUT_SEC", 600, int)
if CHURNER_EXIT_TIMEOUT_SEC < CHURNER_TIMEOUT_SEC:
    CHURNER_EXIT_TIMEOUT_SEC = CHURNER_TIMEOUT_SEC
CHURNER_MIN_HEADROOM: int = _env("CHURNER_MIN_HEADROOM", 10, int)
if CHURNER_MIN_HEADROOM < 0:
    CHURNER_MIN_HEADROOM = 0
CHURNER_RESERVE_USD: float = _env("CHURNER_RESERVE_USD", 5.0, float)
if CHURNER_RESERVE_USD < 0:
    CHURNER_RESERVE_USD = 0.0
CHURNER_MAX_ACTIVE: int = _env("CHURNER_MAX_ACTIVE", 5, int)
if CHURNER_MAX_ACTIVE < 1:
    CHURNER_MAX_ACTIVE = 1
CHURNER_PROFIT_ROUTING: str = _env("CHURNER_PROFIT_ROUTING", "subsidy", str).strip().lower()
if CHURNER_PROFIT_ROUTING not in {"subsidy", "slot"}:
    CHURNER_PROFIT_ROUTING = "subsidy"

# Dependency enforcement: churner/subsidy rely on ledger primitives.
if CHURNER_ENABLED and not POSITION_LEDGER_ENABLED:
    POSITION_LEDGER_ENABLED = True
if SUBSIDY_ENABLED and not POSITION_LEDGER_ENABLED:
    POSITION_LEDGER_ENABLED = True

# Sticky release gate controls.
RELEASE_MIN_AGE_SEC: int = _env("RELEASE_MIN_AGE_SEC", 7 * 86400, int)
RELEASE_MIN_DISTANCE_PCT: float = _env("RELEASE_MIN_DISTANCE_PCT", 10.0, float)
RELEASE_ADX_THRESHOLD: float = _env("RELEASE_ADX_THRESHOLD", 30.0, float)
RELEASE_MAX_STUCK_PCT: float = _env("RELEASE_MAX_STUCK_PCT", 50.0, float)
RELEASE_PANIC_STUCK_PCT: float = _env("RELEASE_PANIC_STUCK_PCT", 80.0, float)
RELEASE_PANIC_MIN_AGE_SEC: int = _env("RELEASE_PANIC_MIN_AGE_SEC", 86400, int)
RELEASE_RECOVERY_TARGET_PCT: float = _env("RELEASE_RECOVERY_TARGET_PCT", 60.0, float)
RELEASE_AUTO_ENABLED: bool = _env("RELEASE_AUTO_ENABLED", False, bool)
RELEASE_RECON_HARD_GATE_ENABLED: bool = _env("RELEASE_RECON_HARD_GATE_ENABLED", True, bool)

# Sticky compounding mode:
# - legacy_profit: ORDER_SIZE_USD + slot.total_profit (current behavior)
# - fixed: ORDER_SIZE_USD only
_STICKY_COMPOUNDING_MODE_RAW: str = _env("STICKY_COMPOUNDING_MODE", "legacy_profit", str)
STICKY_COMPOUNDING_MODE: str = str(_STICKY_COMPOUNDING_MODE_RAW).strip().lower()
if STICKY_COMPOUNDING_MODE not in {"legacy_profit", "fixed"}:
    STICKY_COMPOUNDING_MODE = "legacy_profit"

# Entry anti-loss behavior:
# - At N consecutive losses, widen entry distance (backoff).
# - At M consecutive losses, stop placing new entries for cooldown seconds.
LOSS_BACKOFF_START: int = _env("LOSS_BACKOFF_START", 3, int)
LOSS_COOLDOWN_START: int = _env("LOSS_COOLDOWN_START", 5, int)
LOSS_COOLDOWN_SEC: int = _env("LOSS_COOLDOWN_SEC", 900, int)       # 15 min
# Base re-entry cooldown after cycle close/orphan (seconds), regardless of PnL.
# This slows immediate re-entries and reduces order-velocity bursts across slots.
REENTRY_BASE_COOLDOWN_SEC: float = _env("REENTRY_BASE_COOLDOWN_SEC", 60.0, float)

# Soft operational limits.
MAX_API_CALLS_PER_LOOP: int = _env("MAX_API_CALLS_PER_LOOP", 10, int)
# Max new entry placements per main loop across all slots.
# Exits/cancels are not capped here; this only throttles fresh entry Adds.
MAX_ENTRY_ADDS_PER_LOOP: int = _env("MAX_ENTRY_ADDS_PER_LOOP", 2, int)
# Private API metronome: smooths bursts into paced call waves.
# Example default = 2 calls per 1.5s (~1.33 calls/sec sustained).
PRIVATE_API_METRONOME_ENABLED: bool = _env("PRIVATE_API_METRONOME_ENABLED", True, bool)
PRIVATE_API_METRONOME_WAVE_CALLS: int = _env("PRIVATE_API_METRONOME_WAVE_CALLS", 2, int)
PRIVATE_API_METRONOME_WAVE_SECONDS: float = _env("PRIVATE_API_METRONOME_WAVE_SECONDS", 1.5, float)
ORPHAN_PRESSURE_WARN_AT: int = _env("ORPHAN_PRESSURE_WARN_AT", 100, int)
# Capacity telemetry controls for manual scaling.
KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT: int = _env("KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT", 225, int)
OPEN_ORDER_SAFETY_RATIO: float = _env("OPEN_ORDER_SAFETY_RATIO", 0.75, float)
# Alert when Kraken/internal open-order counts diverge for too long.
# This is a persistence canary, not a one-sample spike alarm.
# Auto-soft-close: when capacity utilization >= this %, soft-close farthest
# recovery orders each cycle to prevent hitting the hard order limit.
AUTO_SOFT_CLOSE_CAPACITY_PCT: float = _env("AUTO_SOFT_CLOSE_CAPACITY_PCT", 95.0, float)
AUTO_SOFT_CLOSE_BATCH: int = _env("AUTO_SOFT_CLOSE_BATCH", 2, int)

# Auto recovery drain: force-close a small number of recoveries each loop
# to reduce backlog when above per-slot cap or under capacity pressure.
AUTO_RECOVERY_DRAIN_ENABLED: bool = _env("AUTO_RECOVERY_DRAIN_ENABLED", True, bool)
AUTO_RECOVERY_DRAIN_MAX_PER_LOOP: int = _env("AUTO_RECOVERY_DRAIN_MAX_PER_LOOP", 1, int)
AUTO_RECOVERY_DRAIN_CAPACITY_PCT: float = _env("AUTO_RECOVERY_DRAIN_CAPACITY_PCT", 80.0, float)

OPEN_ORDER_DRIFT_ALERT_THRESHOLD: int = _env("OPEN_ORDER_DRIFT_ALERT_THRESHOLD", 10, int)
OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC: int = _env("OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC", 600, int)
OPEN_ORDER_DRIFT_ALERT_COOLDOWN_SEC: int = _env("OPEN_ORDER_DRIFT_ALERT_COOLDOWN_SEC", 1800, int)

# Balance reconciliation: max acceptable drift between account growth and bot P&L.
BALANCE_RECON_DRIFT_PCT: float = _env("BALANCE_RECON_DRIFT_PCT", 2.0, float)

# Balance intelligence: external flow detection + persistent equity history.
FLOW_DETECTION_ENABLED: bool = _env("FLOW_DETECTION_ENABLED", True, bool)
FLOW_POLL_INTERVAL_SEC: float = _env("FLOW_POLL_INTERVAL_SEC", 300.0, float)
FLOW_BASELINE_AUTO_ADJUST: bool = _env("FLOW_BASELINE_AUTO_ADJUST", True, bool)

EQUITY_TS_ENABLED: bool = _env("EQUITY_TS_ENABLED", True, bool)
EQUITY_SNAPSHOT_INTERVAL_SEC: float = _env("EQUITY_SNAPSHOT_INTERVAL_SEC", 300.0, float)
EQUITY_SNAPSHOT_FLUSH_SEC: float = _env("EQUITY_SNAPSHOT_FLUSH_SEC", 300.0, float)
EQUITY_TS_RETENTION_DAYS: int = _env("EQUITY_TS_RETENTION_DAYS", 7, int)
EQUITY_TS_SPARKLINE_7D_STEP: int = _env("EQUITY_TS_SPARKLINE_7D_STEP", 6, int)

# ---------------------------------------------------------------------------
# AI council settings
# ---------------------------------------------------------------------------

# The council queries multiple models and uses majority vote.
# Panel is auto-configured from SAMBANOVA_API_KEY / CEREBRAS_API_KEY /
# GROQ_API_KEY / NVIDIA_API_KEY.
# These legacy settings are only used as single-model fallback
# when neither panel key is set.
AI_API_URL: str = _env("AI_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
AI_MODEL: str = _env("AI_MODEL", "meta/llama-3.1-8b-instruct")

# ---------------------------------------------------------------------------
# Exit lifecycle management (Section 12: repricing, S2 break-glass, recovery)
# ---------------------------------------------------------------------------

# Enable/disable the exit lifecycle system. When disabled, exits stay as-is.
RECOVERY_ENABLED: bool = _env("RECOVERY_ENABLED", True, bool)
# Recovery-order creation/management toggle for strategic-capital rollout.
# Compatibility: if unset, it inherits RECOVERY_ENABLED.
RECOVERY_ORDERS_ENABLED: bool = _env("RECOVERY_ORDERS_ENABLED", RECOVERY_ENABLED, bool)

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
# Inventory rebalancer (size-skew governor)
# ---------------------------------------------------------------------------

# Master switch for inventory rebalancer.
REBALANCE_ENABLED: bool = _env("REBALANCE_ENABLED", True, bool)

# Target idle USD ratio (0.40 = keep ~40% USD idle runway).
REBALANCE_TARGET_IDLE_PCT: float = _env("REBALANCE_TARGET_IDLE_PCT", 0.40, float)

# Dynamic idle target (trend-aware target adjustment for rebalancer input only).
TREND_FAST_HALFLIFE: float = _env("TREND_FAST_HALFLIFE", 1800.0, float)
TREND_SLOW_HALFLIFE: float = _env("TREND_SLOW_HALFLIFE", 14400.0, float)
TREND_IDLE_SENSITIVITY: float = _env("TREND_IDLE_SENSITIVITY", 5.0, float)
TREND_IDLE_FLOOR: float = _env("TREND_IDLE_FLOOR", 0.15, float)
TREND_IDLE_CEILING: float = _env("TREND_IDLE_CEILING", 0.60, float)
TREND_MIN_SAMPLES: int = _env("TREND_MIN_SAMPLES", 24, int)
TREND_HYSTERESIS_SEC: float = _env("TREND_HYSTERESIS_SEC", 600.0, float)
TREND_HYSTERESIS_SMOOTH_HALFLIFE: float = _env("TREND_HYSTERESIS_SMOOTH_HALFLIFE", 900.0, float)
TREND_DEAD_ZONE: float = _env("TREND_DEAD_ZONE", 0.001, float)

# PD gains for skew controller.
REBALANCE_KP: float = _env("REBALANCE_KP", 2.0, float)
REBALANCE_KD: float = _env("REBALANCE_KD", 0.5, float)

# Controller output and slew limits.
REBALANCE_MAX_SKEW: float = _env("REBALANCE_MAX_SKEW", 0.30, float)
REBALANCE_MAX_SKEW_STEP: float = _env("REBALANCE_MAX_SKEW_STEP", 0.05, float)
REBALANCE_NEUTRAL_BAND: float = _env("REBALANCE_NEUTRAL_BAND", 0.05, float)

# EMA smoothing and update cadence.
REBALANCE_EMA_HALFLIFE: float = _env("REBALANCE_EMA_HALFLIFE", 1800.0, float)
REBALANCE_INTERVAL_SEC: float = _env("REBALANCE_INTERVAL_SEC", 300.0, float)

# ---------------------------------------------------------------------------
# HMM data pipeline (OHLCV collection/readiness)
# ---------------------------------------------------------------------------

# Persist OHLCV candles for HMM training/readiness checks.
HMM_OHLCV_ENABLED: bool = _env("HMM_OHLCV_ENABLED", True, bool)
HMM_OHLCV_INTERVAL_MIN: int = _env("HMM_OHLCV_INTERVAL_MIN", 1, int)
# How often runtime pulls Kraken OHLC and upserts into Supabase.
HMM_OHLCV_SYNC_INTERVAL_SEC: float = _env("HMM_OHLCV_SYNC_INTERVAL_SEC", 60.0, float)
# Retention for persisted candles (days). 14 days ~= 4032 5m candles.
HMM_OHLCV_RETENTION_DAYS: int = _env("HMM_OHLCV_RETENTION_DAYS", 14, int)
# One-time startup backfill to accelerate first HMM training window.
HMM_OHLCV_BACKFILL_ON_STARTUP: bool = _env("HMM_OHLCV_BACKFILL_ON_STARTUP", True, bool)
HMM_OHLCV_BACKFILL_MAX_PAGES: int = _env("HMM_OHLCV_BACKFILL_MAX_PAGES", 40, int)
# Max consecutive startup/manual backfills that can return zero new candles
# before the backfill circuit-breaker skips further attempts.
HMM_BACKFILL_MAX_STALLS: int = _env("HMM_BACKFILL_MAX_STALLS", 3, int)

# Readiness targets for HMM training/inference windows.
HMM_TRAINING_CANDLES: int = _env("HMM_TRAINING_CANDLES", 4000, int)
HMM_RECENT_CANDLES: int = _env("HMM_RECENT_CANDLES", 100, int)
HMM_MIN_TRAIN_SAMPLES: int = _env("HMM_MIN_TRAIN_SAMPLES", 500, int)
HMM_READINESS_CACHE_SEC: float = _env("HMM_READINESS_CACHE_SEC", 300.0, float)
# Optional deep-window recency decay (applied as resampling in runtime).
HMM_DEEP_DECAY_ENABLED: bool = _env("HMM_DEEP_DECAY_ENABLED", False, bool)
# Half-life in candles for deep-window decay weighting.
HMM_DEEP_DECAY_HALFLIFE: int = _env("HMM_DEEP_DECAY_HALFLIFE", 1440, int)

# HMM detector runtime knobs (advisory-only layer; reducer remains unchanged).
HMM_ENABLED: bool = _env("HMM_ENABLED", False, bool)
HMM_N_STATES: int = _env("HMM_N_STATES", 3, int)
HMM_N_ITER: int = _env("HMM_N_ITER", 100, int)
HMM_COVARIANCE_TYPE: str = _env("HMM_COVARIANCE_TYPE", "diag", str)
HMM_INFERENCE_WINDOW: int = _env("HMM_INFERENCE_WINDOW", 50, int)
HMM_CONFIDENCE_THRESHOLD: float = _env("HMM_CONFIDENCE_THRESHOLD", 0.15, float)
HMM_RETRAIN_INTERVAL_SEC: float = _env("HMM_RETRAIN_INTERVAL_SEC", 86400.0, float)
HMM_BIAS_GAIN: float = _env("HMM_BIAS_GAIN", 1.0, float)
HMM_BLEND_WITH_TREND: float = _env("HMM_BLEND_WITH_TREND", 0.5, float)

# Multi-timeframe HMM (primary + secondary + consensus selector).
HMM_MULTI_TIMEFRAME_ENABLED: bool = _env("HMM_MULTI_TIMEFRAME_ENABLED", False, bool)
HMM_MULTI_TIMEFRAME_SOURCE: str = _env("HMM_MULTI_TIMEFRAME_SOURCE", "primary", str)
HMM_SECONDARY_INTERVAL_MIN: int = _env("HMM_SECONDARY_INTERVAL_MIN", 15, int)
HMM_SECONDARY_OHLCV_ENABLED: bool = _env("HMM_SECONDARY_OHLCV_ENABLED", False, bool)
HMM_SECONDARY_SYNC_INTERVAL_SEC: float = _env("HMM_SECONDARY_SYNC_INTERVAL_SEC", 300.0, float)
HMM_SECONDARY_TRAINING_CANDLES: int = _env("HMM_SECONDARY_TRAINING_CANDLES", 1440, int)
HMM_SECONDARY_RECENT_CANDLES: int = _env("HMM_SECONDARY_RECENT_CANDLES", 50, int)
HMM_SECONDARY_MIN_TRAIN_SAMPLES: int = _env("HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200, int)
# Tertiary (1h) HMM for strategic transition sensing.
HMM_TERTIARY_ENABLED: bool = _env("HMM_TERTIARY_ENABLED", False, bool)
HMM_TERTIARY_INTERVAL_MIN: int = _env("HMM_TERTIARY_INTERVAL_MIN", 60, int)
HMM_TERTIARY_TRAINING_CANDLES: int = _env("HMM_TERTIARY_TRAINING_CANDLES", 500, int)
HMM_TERTIARY_RECENT_CANDLES: int = _env("HMM_TERTIARY_RECENT_CANDLES", 30, int)
HMM_TERTIARY_MIN_TRAIN_SAMPLES: int = _env("HMM_TERTIARY_MIN_TRAIN_SAMPLES", 150, int)
HMM_TERTIARY_SYNC_INTERVAL_SEC: float = _env("HMM_TERTIARY_SYNC_INTERVAL_SEC", 3600.0, float)
CONSENSUS_1M_WEIGHT: float = _env("CONSENSUS_1M_WEIGHT", 0.3, float)
CONSENSUS_15M_WEIGHT: float = _env("CONSENSUS_15M_WEIGHT", 0.7, float)
CONSENSUS_1H_WEIGHT: float = _env("CONSENSUS_1H_WEIGHT", 0.30, float)
CONSENSUS_DAMPEN_FACTOR: float = _env("CONSENSUS_DAMPEN_FACTOR", 0.5, float)

# ---------------------------------------------------------------------------
# Throughput sizer (fill-time-based advisory sizing layer)
# ---------------------------------------------------------------------------

# Master toggle: when disabled, runtime sizing is unchanged.
TP_ENABLED: bool = _env("TP_ENABLED", False, bool)
# Rolling window of completed cycles used to compute throughput statistics.
TP_LOOKBACK_CYCLES: int = _env("TP_LOOKBACK_CYCLES", 500, int)
# Global sample gate before throughput sizer activates.
TP_MIN_SAMPLES: int = _env("TP_MIN_SAMPLES", 20, int)
# Per regime x side sample gate before bucket-specific sizing is used.
TP_MIN_SAMPLES_PER_BUCKET: int = _env("TP_MIN_SAMPLES_PER_BUCKET", 10, int)
# Bucket sample count where confidence blending reaches 1.0.
TP_FULL_CONFIDENCE_SAMPLES: int = _env("TP_FULL_CONFIDENCE_SAMPLES", 50, int)
# Lower/upper multiplier bounds for throughput sizing.
TP_FLOOR_MULT: float = _env("TP_FLOOR_MULT", 0.5, float)
TP_CEILING_MULT: float = _env("TP_CEILING_MULT", 2.0, float)
# Weight for right-censored open exits in fill-time distribution.
TP_CENSORED_WEIGHT: float = _env("TP_CENSORED_WEIGHT", 0.5, float)
# Age-pressure throttle controls.
TP_AGE_PRESSURE_TRIGGER: float = _env("TP_AGE_PRESSURE_TRIGGER", 1.5, float)
TP_AGE_PRESSURE_SENSITIVITY: float = _env("TP_AGE_PRESSURE_SENSITIVITY", 0.5, float)
TP_AGE_PRESSURE_FLOOR: float = _env("TP_AGE_PRESSURE_FLOOR", 0.3, float)
# Capital utilization penalty controls.
TP_UTIL_THRESHOLD: float = _env("TP_UTIL_THRESHOLD", 0.7, float)
TP_UTIL_SENSITIVITY: float = _env("TP_UTIL_SENSITIVITY", 0.8, float)
TP_UTIL_FLOOR: float = _env("TP_UTIL_FLOOR", 0.4, float)
# Optional recency weighting on completed cycle stats.
TP_RECENCY_HALFLIFE: int = _env("TP_RECENCY_HALFLIFE", 100, int)
# Emit throughput summary logs at update cadence.
TP_LOG_UPDATES: bool = _env("TP_LOG_UPDATES", True, bool)

# ---------------------------------------------------------------------------
# Manifold Trading Score (MTS)
# ---------------------------------------------------------------------------

MTS_ENABLED: bool = _env("MTS_ENABLED", True, bool)
MTS_CLARITY_WEIGHTS: list[float] = _env_float_list("MTS_CLARITY_WEIGHTS", [0.2, 0.5, 0.3], 3)
MTS_STABILITY_SWITCH_WEIGHTS: list[float] = _env_float_list(
    "MTS_STABILITY_SWITCH_WEIGHTS",
    [0.2, 0.5, 0.3],
    3,
)
MTS_COHERENCE_WEIGHTS: list[float] = _env_float_list("MTS_COHERENCE_WEIGHTS", [0.5, 0.25, 0.25], 3)
MTS_HISTORY_SIZE: int = _env("MTS_HISTORY_SIZE", 360, int)
if MTS_HISTORY_SIZE < 1:
    MTS_HISTORY_SIZE = 1
MTS_ENTRY_THROTTLE_ENABLED: bool = _env("MTS_ENTRY_THROTTLE_ENABLED", False, bool)
MTS_ENTRY_THROTTLE_FLOOR: float = _env("MTS_ENTRY_THROTTLE_FLOOR", 0.3, float)
if MTS_ENTRY_THROTTLE_FLOOR < 0.0:
    MTS_ENTRY_THROTTLE_FLOOR = 0.0
if MTS_ENTRY_THROTTLE_FLOOR > 1.0:
    MTS_ENTRY_THROTTLE_FLOOR = 1.0
MTS_KERNEL_ENABLED: bool = _env("MTS_KERNEL_ENABLED", False, bool)
MTS_KERNEL_MIN_SAMPLES: int = _env("MTS_KERNEL_MIN_SAMPLES", 200, int)
if MTS_KERNEL_MIN_SAMPLES < 1:
    MTS_KERNEL_MIN_SAMPLES = 1
MTS_KERNEL_ALPHA_MAX: float = _env("MTS_KERNEL_ALPHA_MAX", 0.5, float)
if MTS_KERNEL_ALPHA_MAX < 0.0:
    MTS_KERNEL_ALPHA_MAX = 0.0
if MTS_KERNEL_ALPHA_MAX > 1.0:
    MTS_KERNEL_ALPHA_MAX = 1.0
MTS_CHURNER_GATE: float = _env("MTS_CHURNER_GATE", 0.3, float)
if MTS_CHURNER_GATE < 0.0:
    MTS_CHURNER_GATE = 0.0
if MTS_CHURNER_GATE > 1.0:
    MTS_CHURNER_GATE = 1.0

# Legacy Kelly toggle retained for backward compatibility only (dead config).
KELLY_ENABLED: bool = _env("KELLY_ENABLED", False, bool)

# ---------------------------------------------------------------------------
# Directional regime controls (Phase 0 shadow mode defaults)
# ---------------------------------------------------------------------------

# Master actuation switch. Keep disabled during Phase 0.
REGIME_DIRECTIONAL_ENABLED: bool = _env("REGIME_DIRECTIONAL_ENABLED", False, bool)
# Shadow-only evaluator (computes/logs tiers, no order-flow changes).
REGIME_SHADOW_ENABLED: bool = _env("REGIME_SHADOW_ENABLED", False, bool)

# Tier confidence thresholds.
REGIME_TIER1_CONFIDENCE: float = _env("REGIME_TIER1_CONFIDENCE", 0.20, float)
REGIME_TIER2_CONFIDENCE: float = _env("REGIME_TIER2_CONFIDENCE", 0.50, float)

# Directional evidence floors using abs(HMM bias_signal).
REGIME_TIER1_BIAS_FLOOR: float = _env("REGIME_TIER1_BIAS_FLOOR", 0.10, float)
REGIME_TIER2_BIAS_FLOOR: float = _env("REGIME_TIER2_BIAS_FLOOR", 0.25, float)

# Stability controls.
REGIME_HYSTERESIS: float = _env("REGIME_HYSTERESIS", 0.05, float)
REGIME_MIN_DWELL_SEC: float = _env("REGIME_MIN_DWELL_SEC", 300.0, float)
REGIME_SUPPRESSION_GRACE_SEC: float = _env("REGIME_SUPPRESSION_GRACE_SEC", 60.0, float)
REGIME_TIER2_REENTRY_COOLDOWN_SEC: float = _env("REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0, float)
REGIME_EVAL_INTERVAL_SEC: float = _env("REGIME_EVAL_INTERVAL_SEC", 300.0, float)
# Accelerated eval interval used during BOCPD alerts.
REGIME_EVAL_INTERVAL_FAST: float = _env("REGIME_EVAL_INTERVAL_FAST", 60.0, float)

# Optional manual override (empty string = auto/HMM-driven).
REGIME_MANUAL_OVERRIDE: str = _env("REGIME_MANUAL_OVERRIDE", "", str)
REGIME_MANUAL_CONFIDENCE: float = _env("REGIME_MANUAL_CONFIDENCE", 0.75, float)

# Mapping from skew signal -> size multiplier.
REBALANCE_SIZE_SENSITIVITY: float = _env("REBALANCE_SIZE_SENSITIVITY", 1.0, float)
REBALANCE_MAX_SIZE_MULT: float = _env("REBALANCE_MAX_SIZE_MULT", 1.5, float)

# ---------------------------------------------------------------------------
# Bayesian intelligence stack (phased rollout)
# ---------------------------------------------------------------------------

# Phase 0: instrumentation only (no behavior change).
BELIEF_STATE_LOGGING_ENABLED: bool = _env("BELIEF_STATE_LOGGING_ENABLED", True, bool)
BELIEF_STATE_IN_STATUS: bool = _env("BELIEF_STATE_IN_STATUS", True, bool)

# Phase 1: BOCPD structural break detector.
BOCPD_ENABLED: bool = _env("BOCPD_ENABLED", False, bool)
BOCPD_EXPECTED_RUN_LENGTH: int = _env("BOCPD_EXPECTED_RUN_LENGTH", 200, int)
BOCPD_ALERT_THRESHOLD: float = _env("BOCPD_ALERT_THRESHOLD", 0.30, float)
BOCPD_URGENT_THRESHOLD: float = _env("BOCPD_URGENT_THRESHOLD", 0.50, float)
BOCPD_MAX_RUN_LENGTH: int = _env("BOCPD_MAX_RUN_LENGTH", 500, int)

# Phase 2: enriched private microstructure features for HMM/BOCPD/survival.
ENRICHED_FEATURES_ENABLED: bool = _env("ENRICHED_FEATURES_ENABLED", False, bool)
FILL_IMBALANCE_WINDOW_SEC: int = _env("FILL_IMBALANCE_WINDOW_SEC", 300, int)
FILL_TIME_DERIVATIVE_SHORT_SEC: int = _env("FILL_TIME_DERIVATIVE_SHORT_SEC", 300, int)
FILL_TIME_DERIVATIVE_LONG_SEC: int = _env("FILL_TIME_DERIVATIVE_LONG_SEC", 1800, int)

# Phase 3: survival model.
SURVIVAL_MODEL_ENABLED: bool = _env("SURVIVAL_MODEL_ENABLED", False, bool)
SURVIVAL_MODEL_TIER: str = _env("SURVIVAL_MODEL_TIER", "kaplan_meier", str)
if SURVIVAL_MODEL_TIER not in {"kaplan_meier", "cox"}:
    SURVIVAL_MODEL_TIER = "kaplan_meier"
# Survival retrain cadence (spec default: 6 hours).
SURVIVAL_RETRAIN_INTERVAL_SEC: float = _env("SURVIVAL_RETRAIN_INTERVAL_SEC", 21600.0, float)
SURVIVAL_MIN_OBSERVATIONS: int = _env("SURVIVAL_MIN_OBSERVATIONS", 50, int)
SURVIVAL_MIN_PER_STRATUM: int = _env("SURVIVAL_MIN_PER_STRATUM", 10, int)
SURVIVAL_SYNTHETIC_ENABLED: bool = _env("SURVIVAL_SYNTHETIC_ENABLED", False, bool)
SURVIVAL_SYNTHETIC_WEIGHT: float = _env("SURVIVAL_SYNTHETIC_WEIGHT", 0.30, float)
SURVIVAL_SYNTHETIC_PATHS: int = _env("SURVIVAL_SYNTHETIC_PATHS", 5000, int)
_SURVIVAL_HORIZONS_RAW: str = _env("SURVIVAL_HORIZONS", "1800,3600,14400", str)
SURVIVAL_HORIZONS: list[int] = []
for _tok in _SURVIVAL_HORIZONS_RAW.split(","):
    _tok = _tok.strip()
    if not _tok:
        continue
    try:
        _v = int(_tok)
    except (TypeError, ValueError):
        continue
    if _v > 0:
        SURVIVAL_HORIZONS.append(_v)
if not SURVIVAL_HORIZONS:
    SURVIVAL_HORIZONS = [1800, 3600, 14400]
SURVIVAL_HORIZONS = sorted(set(SURVIVAL_HORIZONS))
SURVIVAL_LOG_PREDICTIONS: bool = _env("SURVIVAL_LOG_PREDICTIONS", True, bool)

# Phase 4: per-trade belief tracker.
BELIEF_TRACKER_ENABLED: bool = _env("BELIEF_TRACKER_ENABLED", False, bool)
BELIEF_UPDATE_INTERVAL_SEC: float = _env("BELIEF_UPDATE_INTERVAL_SEC", 60.0, float)
BELIEF_OPPORTUNITY_COST_PER_HOUR: float = _env("BELIEF_OPPORTUNITY_COST_PER_HOUR", 0.001, float)
BELIEF_TIGHTEN_THRESHOLD_PFILL: float = _env("BELIEF_TIGHTEN_THRESHOLD_PFILL", 0.10, float)
BELIEF_TIGHTEN_THRESHOLD_EV: float = _env("BELIEF_TIGHTEN_THRESHOLD_EV", 0.0, float)
BELIEF_IMMEDIATE_REPRICE_AGREEMENT: float = _env("BELIEF_IMMEDIATE_REPRICE_AGREEMENT", 0.30, float)
BELIEF_IMMEDIATE_REPRICE_CONFIDENCE: float = _env("BELIEF_IMMEDIATE_REPRICE_CONFIDENCE", 0.60, float)
BELIEF_WIDEN_ENABLED: bool = _env("BELIEF_WIDEN_ENABLED", False, bool)
BELIEF_WIDEN_STEP_PCT: float = _env("BELIEF_WIDEN_STEP_PCT", 0.001, float)
BELIEF_MAX_WIDEN_COUNT: int = _env("BELIEF_MAX_WIDEN_COUNT", 2, int)
BELIEF_MAX_WIDEN_TOTAL_PCT: float = _env("BELIEF_MAX_WIDEN_TOTAL_PCT", 0.005, float)
BELIEF_TIMER_OVERRIDE_MAX_SEC: float = _env("BELIEF_TIMER_OVERRIDE_MAX_SEC", 3600.0, float)
BELIEF_EV_TREND_WINDOW: int = _env("BELIEF_EV_TREND_WINDOW", 3, int)
BELIEF_LOG_ACTIONS: bool = _env("BELIEF_LOG_ACTIONS", True, bool)

# Phase 5: continuous action knobs.
KNOB_MODE_ENABLED: bool = _env("KNOB_MODE_ENABLED", False, bool)
KNOB_AGGRESSION_DIRECTION: float = _env("KNOB_AGGRESSION_DIRECTION", 0.5, float)
KNOB_AGGRESSION_BOUNDARY: float = _env("KNOB_AGGRESSION_BOUNDARY", 0.3, float)
KNOB_AGGRESSION_CONGESTION: float = _env("KNOB_AGGRESSION_CONGESTION", 0.5, float)
KNOB_AGGRESSION_FLOOR: float = _env("KNOB_AGGRESSION_FLOOR", 0.5, float)
KNOB_AGGRESSION_CEILING: float = _env("KNOB_AGGRESSION_CEILING", 1.5, float)
KNOB_SPACING_VOLATILITY: float = _env("KNOB_SPACING_VOLATILITY", 0.3, float)
KNOB_SPACING_BOUNDARY: float = _env("KNOB_SPACING_BOUNDARY", 0.2, float)
KNOB_SPACING_FLOOR: float = _env("KNOB_SPACING_FLOOR", 0.8, float)
KNOB_SPACING_CEILING: float = _env("KNOB_SPACING_CEILING", 1.5, float)
KNOB_ASYMMETRY: float = _env("KNOB_ASYMMETRY", 0.3, float)
KNOB_CADENCE_BOUNDARY: float = _env("KNOB_CADENCE_BOUNDARY", 0.5, float)
KNOB_CADENCE_ENTROPY: float = _env("KNOB_CADENCE_ENTROPY", 0.3, float)
KNOB_CADENCE_FLOOR: float = _env("KNOB_CADENCE_FLOOR", 0.3, float)
KNOB_SUPPRESS_DIRECTION_FLOOR: float = _env("KNOB_SUPPRESS_DIRECTION_FLOOR", 0.3, float)
KNOB_SUPPRESS_SCALE: float = _env("KNOB_SUPPRESS_SCALE", 0.5, float)

# ---------------------------------------------------------------------------
# USD dust sweep (balance-aware B-side sizing bump)
# ---------------------------------------------------------------------------

# Master switch for folding idle USD residue into B-side entries.
DUST_SWEEP_ENABLED: bool = _env("DUST_SWEEP_ENABLED", True, bool)
# Ignore tiny balance noise below this threshold (USD).
DUST_MIN_THRESHOLD: float = _env("DUST_MIN_THRESHOLD", 0.50, float)
# Cap additive bump per order as % of base B-side size (0 = uncapped, fund guard is safety net).
DUST_MAX_BUMP_PCT: float = _env("DUST_MAX_BUMP_PCT", 0.0, float)

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
# Multiplier bounds: how far volatility can adjust profit_pct from the user's setting.
# 0.5 = can tighten to 50% of user's base; 2.0 = can widen to 200%.
VOLATILITY_PROFIT_MULT_FLOOR: float = _env("VOLATILITY_PROFIT_MULT_FLOOR", 0.5, float)
VOLATILITY_PROFIT_MULT_CEILING: float = _env("VOLATILITY_PROFIT_MULT_CEILING", 2.0, float)

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
        f"  Re-entry delay:  {max(0.0, float(REENTRY_BASE_COOLDOWN_SEC)):.0f}s base",
        f"  Entry add cap:   {max(1, int(MAX_ENTRY_ADDS_PER_LOOP))} per loop",
        (
            "  API metronome:  "
            f"{'on' if PRIVATE_API_METRONOME_ENABLED else 'off'} "
            f"({max(1, int(PRIVATE_API_METRONOME_WAVE_CALLS))} calls/"
            f"{max(0.05, float(PRIVATE_API_METRONOME_WAVE_SECONDS)):.2f}s)"
        ),
        f"  AI council:      manual (/check or dashboard)",
        f"  Health port:     {HEALTH_PORT}",
        f"  Log level:       {LOG_LEVEL}",
        f"  Kraken key:      {'configured' if KRAKEN_API_KEY else 'NOT SET'}",
        f"  DeepSeek key:    {'configured' if DEEPSEEK_API_KEY else 'NOT SET'}",
        f"  SambaNova key:   {'configured' if SAMBANOVA_API_KEY else 'NOT SET'}",
        f"  Cerebras key:    {'configured' if CEREBRAS_API_KEY else 'NOT SET'}",
        f"  Groq key:        {'configured' if GROQ_API_KEY else 'NOT SET'}",
        f"  NVIDIA key:      {'configured' if NVIDIA_API_KEY else 'NOT SET'}",
        (
            "  AI fallback:     "
            f"{'configured' if AI_API_KEY and not SAMBANOVA_API_KEY and not CEREBRAS_API_KEY and not GROQ_API_KEY and not NVIDIA_API_KEY else 'N/A'}"
        ),
        f"  Telegram:        {'configured' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else 'NOT SET'}",
        f"  Supabase:        {'configured' if SUPABASE_URL and SUPABASE_KEY else 'NOT SET'}",
        "=" * 60,
        "",
    ]
    print("\n".join(lines))
