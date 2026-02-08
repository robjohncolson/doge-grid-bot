"""
supabase_store.py -- Supabase (PostgREST) persistence layer for the DOGE grid bot.

Provides cloud persistence for fills, price history, trades, daily summaries,
and bot state so data survives Railway deploys (ephemeral filesystem).

PATTERN:
  Matches notifier.py / kraken_client.py style:
    - Never raises -- logs warnings on failure
    - Bot works identically without Supabase configured
    - Uses urllib.request only (zero external dependencies)

WRITE PATH:
  All writes go to a collections.deque queue.  A daemon thread flushes
  every 10s, batching by table.  Price history batched every 5 min.
  Main loop never blocks on Supabase I/O.

READ PATH:
  3 sequential HTTP calls on startup only (fills, prices, state).
  10s timeout each = 30s worst case.

SETUP:
  1. Create a Supabase project (free tier)
  2. Run the SQL schema in the Supabase SQL Editor (see README)
  3. Set SUPABASE_URL and SUPABASE_KEY env vars
"""

import json
import logging
import time
import threading
import collections
import urllib.request
import urllib.error
import urllib.parse

import config

logger = logging.getLogger(__name__)
# ---------------------------------------------------------------------------
# Pair column compatibility
# ---------------------------------------------------------------------------

def _note_missing_column(err_body: str):
    """Detect missing columns from Supabase error responses."""
    global _pair_column_supported, _identity_columns_supported
    if "does not exist" in err_body:
        if "pair" in err_body and _pair_column_supported is not False:
            _pair_column_supported = False
        if ("trade_id" in err_body or "cycle" in err_body) and _identity_columns_supported is not False:
            _identity_columns_supported = False


def _strip_unsupported_columns(row: dict) -> dict:
    """Remove columns not supported by the current schema."""
    if _pair_column_supported is False or _identity_columns_supported is False:
        row = dict(row)
    if _pair_column_supported is False:
        row.pop("pair", None)
    if _identity_columns_supported is False:
        row.pop("trade_id", None)
        row.pop("cycle", None)
    return row


# ---------------------------------------------------------------------------
# Queue and buffers
# ---------------------------------------------------------------------------

# Max queued writes before dropping oldest (prevents unbounded memory)
MAX_QUEUE_SIZE = 1000

# Write queue: each item is (table_name, row_dict)
_write_queue: collections.deque = collections.deque(maxlen=MAX_QUEUE_SIZE)

# Price buffer: accumulates (time, price) tuples, flushed every 5 min
_price_buffer: list = []
_price_buffer_lock = threading.Lock()

# Column support flags (older schemas may not have these)
_pair_column_supported = False       # fills table lacks pair column; False skips doomed requests
_identity_columns_supported = None  # trade_id, cycle columns

# Writer thread state
_writer_thread: threading.Thread = None
_writer_stop = threading.Event()

# Cleanup tracking
_last_cleanup: float = 0.0


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    """Return True if Supabase is configured."""
    return bool(config.SUPABASE_URL and config.SUPABASE_KEY)


# ---------------------------------------------------------------------------
# Core HTTP helper
# ---------------------------------------------------------------------------

def _request(method: str, path: str, body: dict = None,
             params: dict = None, timeout: int = 10,
             upsert: bool = False) -> dict:
    """
    Make a PostgREST request to Supabase.  Never raises.

    Args:
        method:  HTTP method (GET, POST, PATCH, DELETE)
        path:    Table path, e.g. "/rest/v1/fills"
        body:    JSON body for POST/PATCH
        params:  Query params dict
        timeout: Request timeout in seconds
        upsert:  If True, add resolution=merge-duplicates to Prefer header

    Returns:
        Parsed JSON response (list or dict), or None on failure.
    """
    if not _enabled():
        return None

    url = config.SUPABASE_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    prefer = "return=minimal"
    if method == "GET":
        prefer = "return=representation"
    elif upsert:
        prefer = "return=minimal, resolution=merge-duplicates"

    headers = {
        "apikey": config.SUPABASE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
        "User-Agent": "DOGEGridBot/1.0",
    }

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8")
            if resp_body:
                return json.loads(resp_body)
            return {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:200]
        _note_missing_column(err_body)
        logger.warning("Supabase %s %s HTTP %d: %s", method, path, e.code, err_body)
        return None
    except Exception as e:
        logger.warning("Supabase %s %s failed: %s", method, path, e)
        return None


# ---------------------------------------------------------------------------
# Write operations (queue-based, non-blocking)
# ---------------------------------------------------------------------------

def save_fill(fill: dict, pair: str = "XDGUSD",
              trade_id: str = None, cycle: int = None):
    """Queue a fill record for persistence."""
    if not _enabled():
        return
    row = {
        "time": fill.get("time", time.time()),
        "side": fill["side"],
        "price": fill["price"],
        "volume": fill["volume"],
        "profit": fill.get("profit", 0),
        "fees": fill.get("fees", 0),
        "pair": pair,
    }
    if trade_id is not None:
        row["trade_id"] = trade_id
        row["cycle"] = cycle or 0
    _write_queue.append(("fills", _strip_unsupported_columns(row)))


def save_trade(order, net_profit: float, fees: float, pair: str = "XDGUSD"):
    """Queue a trade record for persistence (mirrors trades.csv)."""
    if not _enabled():
        return
    from datetime import datetime, timezone
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "side": order.side,
        "price": f"{order.price:.6f}",
        "amount": f"{order.volume:.2f}",
        "fee": f"{fees:.4f}",
        "profit": f"{net_profit:.4f}",
        "grid_level": order.level,
        "pair": pair,
    }
    tid = getattr(order, "trade_id", None)
    if tid is not None:
        row["trade_id"] = tid
        row["cycle"] = getattr(order, "cycle", 0)
    _write_queue.append(("trades", _strip_unsupported_columns(row)))


def save_daily_summary(state, pair: str = "XDGUSD"):
    """Queue a daily summary record (mirrors daily_summary.csv)."""
    if not _enabled():
        return
    row = {
        "date": state.today_date,
        "pair": pair,
        "trades_count": state.round_trips_today,
        "gross_profit": f"{state.today_profit_usd + state.today_fees_usd:.4f}",
        "fees_paid": f"{state.today_fees_usd:.4f}",
        "net_profit": f"{state.today_profit_usd:.4f}",
        "doge_accumulated": f"{state.doge_accumulated:.2f}",
    }
    _write_queue.append(("daily_summaries", _strip_unsupported_columns(row)))


def save_state(snapshot: dict, pair: str = "XDGUSD"):
    """Queue a bot state upsert (keyed by pair name for multi-pair)."""
    if not _enabled():
        return
    _write_queue.append(("bot_state", {
        "key": pair,
        "data": snapshot,
    }))


def queue_price_point(timestamp: float, price: float, pair: str = "XDGUSD"):
    """Buffer a price sample (flushed every 5 min by writer thread)."""
    if not _enabled():
        return
    with _price_buffer_lock:
        row = {"time": timestamp, "price": price, "pair": pair}
        _price_buffer.append(_strip_unsupported_columns(row))


# ---------------------------------------------------------------------------
# Read operations (startup only)
# ---------------------------------------------------------------------------

def load_fills(limit: int = 500, pair: str = "XDGUSD") -> list:
    """
    Load recent fills from Supabase on startup.

    Returns a list of fill dicts matching the in-memory format:
        [{"time": float, "side": str, "price": float, "volume": float,
          "profit": float, "fees": float}, ...]

    Returns [] on failure or if Supabase is not configured.
    """
    if not _enabled():
        return []

    params = {
        "select": "time,side,price,volume,profit,fees",
        "order": "time.desc",
        "limit": str(limit),
    }
    if _pair_column_supported is not False:
        params["pair"] = f"eq.{pair}"

    result = _request("GET", "/rest/v1/fills", params=params)
    if result is None and _pair_column_supported is False:
        # Retry without pair filter for legacy schemas
        params.pop("pair", None)
        result = _request("GET", "/rest/v1/fills", params=params)

    if result is None:
        logger.warning("Supabase: failed to load fills -- starting fresh")
        return []

    if not isinstance(result, list):
        return []

    # Reverse so oldest is first (matches in-memory order)
    fills = list(reversed(result))
    logger.info("Supabase: loaded %d fills", len(fills))
    return fills


def load_price_history(since: float = None, pair: str = "XDGUSD") -> list:
    """
    Load price history from Supabase on startup.

    Args:
        since: Unix timestamp cutoff (loads only prices newer than this)
        pair: Kraken pair name to filter by

    Returns a list of (timestamp, price) tuples, or [] on failure.
    """
    if not _enabled():
        return []

    params = {
        "select": "time,price",
        "order": "time.asc",
        "limit": "3000",
    }
    if _pair_column_supported is not False:
        params["pair"] = f"eq.{pair}"
    if since is not None:
        params["time"] = f"gt.{since}"

    result = _request("GET", "/rest/v1/price_history", params=params)
    if result is None and _pair_column_supported is False:
        params.pop("pair", None)
        result = _request("GET", "/rest/v1/price_history", params=params)

    if result is None:
        logger.warning("Supabase: failed to load price history -- starting fresh")
        return []

    if not isinstance(result, list):
        return []

    prices = [(row["time"], row["price"]) for row in result]
    logger.info("Supabase: loaded %d price samples", len(prices))
    return prices


def load_state(pair: str = "XDGUSD") -> dict:
    """
    Load bot state snapshot from Supabase.

    Returns the state data dict, or {} on failure.
    Tries pair-keyed state first, falls back to legacy "current" key.
    """
    if not _enabled():
        return {}

    result = _request("GET", "/rest/v1/bot_state", params={
        "key": f"eq.{pair}",
        "select": "data",
        "limit": "1",
    })

    # Fallback to legacy "current" key for backward compatibility
    if (result is None or (isinstance(result, list) and len(result) == 0)) and pair == "XDGUSD":
        result = _request("GET", "/rest/v1/bot_state", params={
            "key": "eq.current",
            "select": "data",
            "limit": "1",
        })

    if result is None:
        logger.warning("Supabase: failed to load state -- using local")
        return {}

    if isinstance(result, list) and len(result) > 0:
        data = result[0].get("data", {})
        if data:
            logger.info("Supabase: loaded bot state snapshot")
            return data

    return {}


# ---------------------------------------------------------------------------
# Background writer thread
# ---------------------------------------------------------------------------

def _flush_queue():
    """Flush all pending writes, batching by table."""
    if not _write_queue:
        return

    # Drain the queue into batches by table
    batches: dict = {}
    while _write_queue:
        try:
            table, row = _write_queue.popleft()
        except IndexError:
            break
        if table not in batches:
            batches[table] = []
        batches[table].append(row)

    for table, rows in batches.items():
        if table == "bot_state":
            # Upsert: use the last state snapshot only
            last_row = rows[-1]
            result = _request(
                "POST", "/rest/v1/bot_state",
                body=last_row,
                params={"on_conflict": "key"},
                upsert=True,
            )
            if result is None:
                logger.debug("Supabase: bot_state upsert failed")
        elif table == "daily_summaries":
            # Upsert on (date, pair) to avoid duplicates across pairs
            for row in rows:
                _request(
                    "POST", f"/rest/v1/{table}",
                    body=row,
                    params={"on_conflict": "date,pair"},
                    upsert=True,
                )
        else:
            # Bulk insert
            result = _request("POST", f"/rest/v1/{table}", body=rows)
            if result is None:
                logger.debug("Supabase: %s batch insert failed (%d rows)", table, len(rows))
            else:
                logger.debug("Supabase: inserted %d rows into %s", len(rows), table)


def _flush_price_buffer():
    """Flush the price buffer to Supabase (called every 5 min)."""
    global _price_buffer

    with _price_buffer_lock:
        if not _price_buffer:
            return
        to_flush = _price_buffer[:]
        _price_buffer = []

    result = _request("POST", "/rest/v1/price_history", body=to_flush)
    if result is None:
        logger.debug("Supabase: price_history batch insert failed (%d rows)", len(to_flush))
    else:
        logger.debug("Supabase: inserted %d price samples", len(to_flush))


def _cleanup_old_prices():
    """Delete price_history rows older than 48h.  Called once per hour."""
    cutoff = time.time() - 48 * 3600
    result = _request(
        "DELETE", "/rest/v1/price_history",
        params={"time": f"lt.{cutoff}"},
    )
    if result is not None:
        logger.debug("Supabase: cleaned up price_history older than 48h")


def _writer_loop():
    """Background writer: flush queue every 10s, prices every 5 min, cleanup hourly."""
    global _last_cleanup

    logger.info("Supabase writer thread started")
    last_price_flush = time.time()
    _last_cleanup = time.time()

    while not _writer_stop.is_set():
        try:
            # Flush write queue
            _flush_queue()

            # Flush price buffer every 5 minutes
            now = time.time()
            if now - last_price_flush >= 300:
                _flush_price_buffer()
                last_price_flush = now

            # Cleanup old prices every hour
            if now - _last_cleanup >= 3600:
                _cleanup_old_prices()
                _last_cleanup = now

        except Exception as e:
            logger.warning("Supabase writer error: %s", e)

        # Sleep 10s (interruptible)
        _writer_stop.wait(10)

    # Final flush on shutdown
    try:
        _flush_queue()
        _flush_price_buffer()
    except Exception:
        pass
    logger.info("Supabase writer thread stopped")


def start_writer_thread():
    """Start the background writer daemon thread."""
    global _writer_thread

    if not _enabled():
        logger.info("Supabase not configured -- persistence disabled")
        return

    _writer_stop.clear()
    _writer_thread = threading.Thread(target=_writer_loop, daemon=True, name="supabase-writer")
    _writer_thread.start()
    logger.info("Supabase persistence enabled (URL: %s...)", config.SUPABASE_URL[:40])
