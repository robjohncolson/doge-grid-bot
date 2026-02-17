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
import re
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
    global _ohlcv_table_supported, _exit_outcomes_table_supported, _regime_tier_transitions_supported
    global _position_ledger_table_supported, _position_journal_table_supported
    global _exit_outcomes_unsupported_columns
    if "does not exist" in err_body:
        if "pair" in err_body and _pair_column_supported is not False:
            _pair_column_supported = False
        if ("trade_id" in err_body or "cycle" in err_body) and _identity_columns_supported is not False:
            _identity_columns_supported = False
        if "ohlcv_candles" in err_body:
            _ohlcv_table_supported = False
        if "exit_outcomes" in err_body:
            _exit_outcomes_table_supported = False
        if "regime_tier_transitions" in err_body:
            _regime_tier_transitions_supported = False
        if "position_ledger" in err_body:
            _position_ledger_table_supported = False
        if "position_journal" in err_body:
            _position_journal_table_supported = False

    # Column-level autodetection for additive exit_outcomes schema.
    # Example errors:
    # - column "posterior_1m" of relation "exit_outcomes" does not exist
    # - Could not find the 'posterior_1m' column of 'exit_outcomes' in the schema cache
    rel_match = re.search(
        r'column\s+"?([a-zA-Z0-9_]+)"?\s+of\s+relation\s+"?([a-zA-Z0-9_]+)"?\s+does not exist',
        err_body,
        re.IGNORECASE,
    )
    if rel_match:
        col = str(rel_match.group(1) or "").strip()
        rel = str(rel_match.group(2) or "").strip().lower()
        if rel == "exit_outcomes" and col:
            _exit_outcomes_unsupported_columns.add(col)

    cache_match = re.search(
        r"Could not find the '([a-zA-Z0-9_]+)' column of '([a-zA-Z0-9_]+)'",
        err_body,
        re.IGNORECASE,
    )
    if cache_match:
        col = str(cache_match.group(1) or "").strip()
        rel = str(cache_match.group(2) or "").strip().lower()
        if rel == "exit_outcomes" and col:
            _exit_outcomes_unsupported_columns.add(col)


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


def _strip_unsupported_exit_outcomes_columns(row: dict) -> dict:
    """
    Drop exit_outcomes columns that the remote schema does not support.
    """
    if not isinstance(row, dict):
        return {}
    if not _exit_outcomes_unsupported_columns:
        return row
    clean = dict(row)
    for col in list(_exit_outcomes_unsupported_columns):
        clean.pop(col, None)
    return clean


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
_pair_column_supported = None        # auto-detect on first write; None = untested
_identity_columns_supported = None  # trade_id, cycle columns
_ohlcv_table_supported = None       # ohlcv_candles table availability
_exit_outcomes_table_supported = None  # exit_outcomes table availability
_regime_tier_transitions_supported = None  # regime_tier_transitions table availability
_position_ledger_table_supported = None  # position_ledger table availability
_position_journal_table_supported = None  # position_journal table availability
_exit_outcomes_unsupported_columns: set[str] = set()

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


def save_exit_outcome(row: dict):
    """
    Queue a cycle outcome row for vintage analysis.

    Expected table: exit_outcomes.
    """
    if not _enabled() or _exit_outcomes_table_supported is False:
        return
    if not isinstance(row, dict) or not row:
        return
    sanitized = _strip_unsupported_exit_outcomes_columns(dict(row))
    if not sanitized:
        return
    _write_queue.append(("exit_outcomes", sanitized))


def save_regime_tier_transition(row: dict):
    """
    Queue a directional regime tier transition row for dwell analytics.

    Expected table: regime_tier_transitions.
    """
    if not _enabled() or _regime_tier_transitions_supported is False:
        return
    if not isinstance(row, dict) or not row:
        return
    _write_queue.append(("regime_tier_transitions", dict(row)))


def save_position_ledger(row: dict):
    """
    Queue a position_ledger row.

    Expected table: position_ledger (upsert on position_id).
    """
    if not _enabled() or _position_ledger_table_supported is False:
        return
    if not isinstance(row, dict) or not row:
        return
    _write_queue.append(("position_ledger", dict(row)))


def save_position_journal(row: dict):
    """
    Queue a position_journal row (append-only).

    Expected table: position_journal.
    """
    if not _enabled() or _position_journal_table_supported is False:
        return
    if not isinstance(row, dict) or not row:
        return
    _write_queue.append(("position_journal", dict(row)))


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


def save_event(event: dict):
    """
    Queue a structured transition/event log row.
    Expected table: bot_events.
    """
    if not _enabled():
        return
    _write_queue.append(("bot_events", dict(event)))


def load_max_event_id() -> int:
    """
    Return the highest bot_events.event_id currently persisted.
    Returns 0 when unavailable.
    """
    if not _enabled():
        return 0
    result = _request(
        "GET",
        "/rest/v1/bot_events",
        params={"select": "event_id", "order": "event_id.desc", "limit": "1"},
    )
    if isinstance(result, list) and result:
        try:
            return int(result[0].get("event_id") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def save_pairs(pairs_list: list):
    """Queue a batch of scanned pair rows for persistence to the pairs table."""
    if not _enabled():
        return
    _write_queue.append(("pairs", pairs_list))


def queue_price_point(timestamp: float, price: float, pair: str = "XDGUSD"):
    """Buffer a price sample (flushed every 5 min by writer thread)."""
    if not _enabled():
        return
    with _price_buffer_lock:
        row = {"time": timestamp, "price": price, "pair": pair}
        _price_buffer.append(_strip_unsupported_columns(row))


def queue_ohlcv_candles(
    candles: list[dict],
    pair: str = "XDGUSD",
    interval_min: int = 5,
):
    """
    Queue OHLCV candles for upsert into ohlcv_candles.

    Each input row should include:
      time, open, high, low, close, volume, [trade_count]
    """
    if not _enabled() or _ohlcv_table_supported is False:
        return
    if not candles:
        return

    norm_pair = str(pair or "XDGUSD").upper()
    norm_interval = max(1, int(interval_min))
    dedup: dict[tuple[str, int, float], dict] = {}

    for raw in candles:
        if not isinstance(raw, dict):
            continue
        try:
            ts = float(raw.get("time"))
            o = float(raw.get("open"))
            h = float(raw.get("high"))
            l = float(raw.get("low"))
            c = float(raw.get("close"))
            v = float(raw.get("volume"))
        except (TypeError, ValueError):
            continue

        if ts <= 0 or min(o, h, l, c, v) < 0:
            continue

        trade_count = raw.get("trade_count", None)
        if trade_count is not None:
            try:
                trade_count = int(trade_count)
            except (TypeError, ValueError):
                trade_count = None

        key = (norm_pair, norm_interval, ts)
        dedup[key] = {
            "time": ts,
            "pair": norm_pair,
            "interval_min": norm_interval,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
            "trade_count": trade_count,
        }

    if not dedup:
        return

    # Store as one batch payload. _flush_queue flattens and upserts.
    _write_queue.append(("ohlcv_candles", list(dedup.values())))


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


def load_ohlcv_candles(
    limit: int = 2000,
    pair: str = "XDGUSD",
    interval_min: int = 5,
    since: float | None = None,
) -> list[dict]:
    """
    Load recent OHLCV candles from Supabase.

    Paginates automatically to work around PostgREST max_rows server limit
    (default 1000). Returns rows sorted oldest -> newest.
    """
    if not _enabled() or _ohlcv_table_supported is False:
        return []

    target = max(1, int(limit))
    norm_pair = str(pair or "XDGUSD").upper()
    norm_interval = max(1, int(interval_min))
    page_size = 1000  # Safe default under most PostgREST max_rows configs

    collected: list[dict] = []
    max_pages = max(1, (target + page_size - 1) // page_size)

    for page in range(max_pages):
        params = {
            "select": "time,open,high,low,close,volume,trade_count",
            "order": "time.desc",
            "limit": str(min(page_size, target - len(collected))),
            "offset": str(page * page_size),
            "pair": f"eq.{norm_pair}",
            "interval_min": f"eq.{norm_interval}",
        }
        if since is not None:
            try:
                params["time"] = f"gt.{float(since)}"
            except (TypeError, ValueError):
                pass

        result = _request("GET", "/rest/v1/ohlcv_candles", params=params)
        if result is None or not isinstance(result, list):
            break

        collected.extend(result)
        if len(result) < page_size:
            break  # No more rows available

    out: list[dict] = []
    for row in reversed(collected):
        try:
            out.append({
                "time": float(row.get("time")),
                "open": float(row.get("open")),
                "high": float(row.get("high")),
                "low": float(row.get("low")),
                "close": float(row.get("close")),
                "volume": float(row.get("volume")),
                "trade_count": (
                    int(row.get("trade_count"))
                    if row.get("trade_count") is not None
                    else None
                ),
            })
        except (TypeError, ValueError):
            continue
    return out


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
        elif table == "pairs":
            # Bulk upsert scanned pairs (take latest scan only)
            pair_rows = rows[-1]
            result = _request(
                "POST", "/rest/v1/pairs",
                body=pair_rows,
                params={"on_conflict": "pair"},
                upsert=True,
            )
            if result is None:
                logger.debug("Supabase: pairs upsert failed (%d rows)", len(pair_rows))
            else:
                logger.debug("Supabase: upserted %d pairs", len(pair_rows))
        elif table == "ohlcv_candles":
            if _ohlcv_table_supported is False:
                continue

            # Flatten queued chunks and dedupe by (pair, interval_min, time).
            merged: dict[tuple[str, int, float], dict] = {}
            for chunk in rows:
                if isinstance(chunk, list):
                    items = chunk
                elif isinstance(chunk, dict):
                    items = [chunk]
                else:
                    continue
                for row in items:
                    if not isinstance(row, dict):
                        continue
                    try:
                        key = (
                            str(row.get("pair") or "XDGUSD").upper(),
                            int(row.get("interval_min") or 5),
                            float(row.get("time")),
                        )
                    except (TypeError, ValueError):
                        continue
                    merged[key] = row

            payload = list(merged.values())
            if not payload:
                continue

            result = _request(
                "POST",
                "/rest/v1/ohlcv_candles",
                body=payload,
                params={"on_conflict": "pair,interval_min,time"},
                upsert=True,
            )
            if result is None:
                logger.debug("Supabase: ohlcv_candles upsert failed (%d rows)", len(payload))
            else:
                logger.debug("Supabase: upserted %d ohlcv candles", len(payload))
        elif table == "bot_events":
            # Keep event writes resilient across crash/restart windows where the
            # last event_id snapshot may lag the DB by a few rows.
            result = _request(
                "POST",
                "/rest/v1/bot_events",
                body=rows,
                params={"on_conflict": "event_id"},
                upsert=True,
            )
            if result is None:
                logger.debug("Supabase: bot_events upsert failed (%d rows)", len(rows))
            else:
                logger.debug("Supabase: upserted %d rows into bot_events", len(rows))
        elif table == "position_ledger":
            if _position_ledger_table_supported is False:
                continue
            # Keep latest row per position_id for this flush batch.
            by_position: dict[int, dict] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    pid = int(row.get("position_id"))
                except (TypeError, ValueError):
                    continue
                if pid <= 0:
                    continue
                by_position[pid] = row
            payload = list(by_position.values())
            if not payload:
                continue
            result = _request(
                "POST",
                "/rest/v1/position_ledger",
                body=payload,
                params={"on_conflict": "position_id"},
                upsert=True,
            )
            if result is None:
                logger.debug("Supabase: position_ledger upsert failed (%d rows)", len(payload))
            else:
                logger.debug("Supabase: upserted %d rows into position_ledger", len(payload))
        elif table == "position_journal":
            if _position_journal_table_supported is False:
                continue
            payload = [row for row in rows if isinstance(row, dict)]
            if not payload:
                continue
            result = _request("POST", "/rest/v1/position_journal", body=payload)
            if result is None:
                logger.debug("Supabase: position_journal insert failed (%d rows)", len(payload))
            else:
                logger.debug("Supabase: inserted %d rows into position_journal", len(payload))
        elif table == "exit_outcomes":
            if _exit_outcomes_table_supported is False:
                continue
            payload = [
                _strip_unsupported_exit_outcomes_columns(dict(row))
                for row in rows
                if isinstance(row, dict) and row
            ]
            payload = [row for row in payload if row]
            if not payload:
                continue
            result = _request("POST", "/rest/v1/exit_outcomes", body=payload)
            if result is None and _exit_outcomes_unsupported_columns:
                # Retry once after the failed request potentially updated unsupported columns.
                retry_payload = [
                    _strip_unsupported_exit_outcomes_columns(dict(row))
                    for row in payload
                ]
                retry_payload = [row for row in retry_payload if row]
                if retry_payload and retry_payload != payload:
                    result = _request("POST", "/rest/v1/exit_outcomes", body=retry_payload)
            if result is None:
                logger.debug("Supabase: exit_outcomes batch insert failed (%d rows)", len(payload))
            else:
                logger.debug("Supabase: inserted %d rows into exit_outcomes", len(payload))
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


def _cleanup_old_ohlcv():
    """Delete ohlcv_candles rows older than configured retention."""
    if _ohlcv_table_supported is False:
        return

    retention_days = max(1, int(getattr(config, "HMM_OHLCV_RETENTION_DAYS", 14)))
    cutoff = time.time() - retention_days * 86400
    result = _request(
        "DELETE",
        "/rest/v1/ohlcv_candles",
        params={"time": f"lt.{cutoff}"},
    )
    if result is not None:
        logger.debug("Supabase: cleaned up ohlcv_candles older than %d days", retention_days)


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
                _cleanup_old_ohlcv()
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
