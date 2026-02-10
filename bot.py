"""
bot.py -- Main loop for the DOGE grid trading bot.

This is the entry point.  It ties together all the modules:
  config.py        -> settings
  kraken_client.py -> API calls
  grid_strategy.py -> trading logic
  ai_advisor.py    -> hourly market analysis
  notifier.py      -> Telegram alerts

LIFECYCLE:
  1. Load config, print banner
  2. Fetch current DOGE price
  3. Build initial grid around that price
  4. Enter main loop:
     a. Fetch price
     b. Check for fills -> place replacement orders
     c. Check risk limits -> pause/stop if needed
     d. Run AI advisor (hourly)
     e. Check daily reset (midnight UTC)
     f. Check accumulation sweep
     g. Sleep POLL_INTERVAL_SECONDS
  5. On SIGTERM/SIGINT -> cancel all orders -> exit

GRACEFUL SHUTDOWN:
  Railway sends SIGTERM when restarting/redeploying.
  We MUST cancel all open orders before exiting, or they'll
  sit on Kraken's order book with no bot managing them.

HEALTH CHECK:
  A tiny HTTP server runs in a background thread on HEALTH_PORT.
  Railway pings this to know the process is alive.
  Returns JSON with bot status.
"""

import sys
import signal
import time
import logging
import threading
import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
import csv
import io
from datetime import datetime, timezone

import config
import kraken_client
import grid_strategy
import ai_advisor
import notifier
import dashboard
import factory_viz
import stats_engine
import telegram_menu
import supabase_store
import pair_scanner

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging():
    """
    Configure logging with a clear, human-readable format.
    Logs go to both stdout (for Railway's log viewer) and a file.
    """
    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)

    # Format: timestamp [LEVEL] module -- message
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (stdout -- Railway captures this)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(log_level)

    # File handler (local backup)
    os.makedirs(config.LOG_DIR, exist_ok=True)
    file_handler = logging.FileHandler(
        os.path.join(config.LOG_DIR, "bot.log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)

    # Root logger
    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(console)
    root.addHandler(file_handler)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health check HTTP server
# ---------------------------------------------------------------------------

# Global reference so the health handler can read bot state
# Maps pair_name -> GridState (e.g. {"XDGUSD": state, "SOLUSD": state})
_bot_states: dict = {}
_current_prices: dict = {}   # pair_name -> latest price
_bot_healthy = True
_bot_start_time = 0.0

# Pending AI approval -- only one at a time
# Keys: action (str), message_id (int), created_at (float), recommendation (dict)
_pending_approval: dict = None

# How long (seconds) before a pending approval expires
APPROVAL_TIMEOUT = 600  # 10 minutes

# Fixed step for spacing adjustments -- prevents extreme values
SPACING_STEP_PCT = 0.25

# Step for entry distance adjustments (smaller than spacing to fine-tune)
ENTRY_STEP_PCT = 0.1

# Pending web config changes -- written by HTTP thread, read by main loop
_web_config_pending: dict = {}

# OHLC candle cache for stats engine -- per pair
_ohlc_caches: dict = {}       # pair_name -> list
_ohlc_last_fetches: dict = {} # pair_name -> float


# Swarm management queues (thread-safe via CPython GIL atomic assignment)
_swarm_pending_adds: list = []      # list of config.PairConfig
_swarm_pending_removes: list = []   # list of pair_name strings
_swarm_pending_multipliers: list = []  # list of (pair_name, multiplier) tuples

# OHLC round-robin index for throttled fetching
_ohlc_robin_idx = 0


def _first_state():
    """Return the first (or only) pair's state for backward-compatible code paths."""
    if not _bot_states:
        return None
    return next(iter(_bot_states.values()))


def _first_pair_name():
    """Return the first pair name."""
    if not _bot_states:
        return config.PAIR
    return next(iter(_bot_states))


def _get_price(pair_name: str) -> float:
    """Get latest known price for a pair."""
    return _current_prices.get(pair_name, 0.0)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread (for SSE)."""
    daemon_threads = True


class DashboardHandler(BaseHTTPRequestHandler):
    """
    HTTP handler for the web dashboard, JSON API, and legacy health check.
    Replaces the old HealthHandler with full dashboard routes.
    """

    def do_GET(self):
        """Route GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(dashboard.DASHBOARD_HTML)
        elif path == "/api/status":
            self._handle_api_status()
        elif path == "/api/stats":
            self._handle_api_stats()
        elif path == "/api/stream":
            self._handle_sse()
        elif path.startswith("/api/export/"):
            self._handle_export()
        elif path == "/api/swarm/status":
            self._handle_swarm_status()
        elif path == "/api/swarm/available":
            self._handle_swarm_available()
        elif path == "/factory":
            self._send_html(factory_viz.FACTORY_HTML)
        elif path == "/api/factory/status":
            self._handle_factory_status()
        elif path == "/api/factory/stream":
            self._handle_factory_sse()
        elif path == "/health":
            self._handle_health()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        """Route POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/config":
            self._handle_api_config()
        elif path == "/api/swarm/add":
            self._handle_swarm_add()
        elif path == "/api/swarm/remove":
            self._handle_swarm_remove()
        elif path == "/api/swarm/multiplier":
            self._handle_swarm_multiplier()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_api_status(self):
        """GET /api/status -- full state snapshot for the dashboard."""
        if not _bot_states:
            self._send_json({"error": "bot not initialized"}, 503)
            return

        # Support ?pair= query param; default to first pair
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        pair_name = params.get("pair", [_first_pair_name()])[0]
        state = _bot_states.get(pair_name, _first_state())
        current_price = _get_price(state.pair_name)
        if not current_price and state.price_history:
            current_price = state.price_history[-1][1]
        elif not current_price:
            current_price = state.center_price

        data = dashboard.serialize_state(state, current_price)
        # Add list of configured pairs for dashboard pair selector
        data["configured_pairs"] = [
            {"pair": pn, "display": st.pair_display}
            for pn, st in _bot_states.items()
        ]
        self._send_json(data)

    def _handle_api_stats(self):
        """GET /api/stats -- stats engine results only."""
        state = _first_state()
        if not state:
            self._send_json({"error": "bot not initialized"}, 503)
            return
        self._send_json(state.stats_results or {})

    def _handle_health(self):
        """GET /health -- legacy Railway health check (backwards compatible)."""
        status = {
            "status": "healthy" if _bot_healthy else "unhealthy",
            "mode": "dry_run" if config.DRY_RUN else "live",
            "uptime_seconds": int(time.time() - _bot_start_time) if _bot_start_time else 0,
            "pairs": [st.pair_display for st in _bot_states.values()] if _bot_states else [config.PAIR_DISPLAY],
        }
        if _bot_states:
            # Aggregate across all pairs
            total_profit = sum(st.total_profit_usd for st in _bot_states.values())
            total_trips = sum(st.total_round_trips for st in _bot_states.values())
            today_trips = sum(st.round_trips_today for st in _bot_states.values())
            total_open = sum(
                len([o for o in st.grid_orders if o.status == "open"])
                for st in _bot_states.values()
            )
            any_paused = any(st.is_paused for st in _bot_states.values())
            total_doge = sum(st.doge_accumulated for st in _bot_states.values())
            status.update({
                "total_profit": round(total_profit, 4),
                "total_round_trips": total_trips,
                "today_round_trips": today_trips,
                "open_orders": total_open,
                "is_paused": any_paused,
                "doge_accumulated": round(total_doge, 2),
            })
        self._send_json(status)

    def _handle_sse(self):
        """GET /api/stream -- Server-Sent Events stream of bot state."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            while True:
                state = _first_state()
                if not state:
                    data = json.dumps({"error": "bot not initialized"})
                else:
                    current_price = _get_price(state.pair_name)
                    if not current_price and state.price_history:
                        current_price = state.price_history[-1][1]
                    elif not current_price:
                        current_price = state.center_price
                    data = json.dumps(dashboard.serialize_state(state, current_price))

                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(3)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _handle_factory_status(self):
        """GET /api/factory/status -- machine-level state for factory viz."""
        if not _bot_states:
            self._send_json({"error": "bot not initialized"}, 503)
            return
        self._send_json(factory_viz.serialize_factory_state(_bot_states, _current_prices))

    def _handle_factory_sse(self):
        """GET /api/factory/stream -- SSE with diffs (5s interval)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        prev_snapshot = None
        try:
            while True:
                if not _bot_states:
                    data = json.dumps({"error": "bot not initialized"})
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                else:
                    snapshot = factory_viz.serialize_factory_state(
                        _bot_states, _current_prices
                    )
                    if prev_snapshot is None:
                        # First tick: send full state
                        self.wfile.write(
                            f"event: full\ndata: {json.dumps(snapshot)}\n\n"
                            .encode("utf-8")
                        )
                    else:
                        # Subsequent ticks: send only changed machine states
                        diff = factory_viz.compute_diff(prev_snapshot, snapshot)
                        if diff:
                            self.wfile.write(
                                f"event: diff\ndata: {json.dumps(diff)}\n\n"
                                .encode("utf-8")
                            )
                    prev_snapshot = snapshot
                self.wfile.flush()
                time.sleep(5)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _handle_export(self):
        """Route /api/export/* requests."""
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")  # ["api", "export", "<type>"]
        params = parse_qs(parsed.query)
        fmt = params.get("format", ["json"])[0].lower()

        if len(parts) < 3:
            self._send_json({"error": "missing export type"}, 400)
            return

        export_type = parts[2]
        if export_type == "fills":
            self._export_fills(fmt)
        elif export_type == "stats":
            self._export_stats(fmt)
        elif export_type == "trades":
            self._export_trades(fmt)
        else:
            self._send_json({"error": f"unknown export type: {export_type}"}, 404)

    def _export_fills(self, fmt):
        """Export recent fills as CSV or JSON."""
        state = _first_state()
        if not state:
            self._send_json({"error": "bot not initialized"}, 503)
            return

        fills = []
        for f in state.recent_fills:
            fills.append({
                "time": datetime.fromtimestamp(f.get("time", 0), tz=timezone.utc).isoformat() if f.get("time") else "",
                "side": f["side"],
                "price": round(f["price"], 6),
                "volume": round(f["volume"], 2),
                "profit": round(f.get("profit", 0), 4),
                "fees": round(f.get("fees", 0), 4),
                "trade_id": f.get("trade_id", ""),
                "cycle": f.get("cycle", ""),
                "order_role": f.get("order_role", ""),
            })

        if fmt == "csv":
            self._send_csv_data(fills, ["time", "side", "price", "volume", "profit", "fees", "trade_id", "cycle", "order_role"], "fills.csv")
        else:
            self._send_json(fills)

    def _export_stats(self, fmt):
        """Export stats results as CSV or JSON."""
        state = _first_state()
        if not state:
            self._send_json({"error": "bot not initialized"}, 503)
            return

        results = state.stats_results or {}
        if fmt == "csv":
            rows = []
            for name, r in results.items():
                if name == "overall_health":
                    continue
                if isinstance(r, dict):
                    rows.append({
                        "analyzer": name,
                        "verdict": r.get("verdict", ""),
                        "confidence": r.get("confidence", ""),
                        "summary": r.get("summary", ""),
                    })
            self._send_csv_data(rows, ["analyzer", "verdict", "confidence", "summary"], "stats.csv")
        else:
            self._send_json(results)

    def _export_trades(self, fmt):
        """Export trade history from logs/trades.csv."""
        filepath = os.path.join(config.LOG_DIR, "trades.csv")
        if not os.path.exists(filepath):
            if fmt == "csv":
                self._send_csv_raw("", "trades.csv")
            else:
                self._send_json([])
            return

        try:
            with open(filepath, "r", newline="", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
            return

        if fmt == "csv":
            self._send_csv_raw(content, "trades.csv")
        else:
            rows = []
            try:
                reader = csv.DictReader(io.StringIO(content))
                for row in reader:
                    rows.append(row)
            except Exception:
                pass
            self._send_json(rows)

    def _send_csv_data(self, rows, fieldnames, filename):
        """Serialize list of dicts to CSV and send with download header."""
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        self._send_csv_raw(output.getvalue(), filename)

    def _send_csv_raw(self, content, filename):
        """Send raw CSV string with Content-Disposition header."""
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------
    # Swarm API endpoints
    # ---------------------------------------------------------------

    def _handle_swarm_status(self):
        """GET /api/swarm/status -- aggregate stats + per-pair compact summaries."""
        if not _bot_states:
            self._send_json({"error": "bot not initialized"}, 503)
            return

        pairs_list = []
        for pn, st in _bot_states.items():
            cp = _current_prices.get(pn, 0.0)
            open_count = sum(1 for o in st.grid_orders if o.status == "open")
            pairs_list.append({
                "pair": pn,
                "display": st.pair_display,
                "price": round(cp, 8),
                "pair_state": st.pair_state,
                "today_pnl": round(st.today_profit_usd, 4),
                "total_pnl": round(st.total_profit_usd, 4),
                "trips_today": st.round_trips_today,
                "total_trips": st.total_round_trips,
                "open_orders": open_count,
                "entry_pct": st.entry_pct,
                "profit_pct": st.profit_pct,
                "multiplier": st.next_entry_multiplier,
                "long_only": st.long_only,
                "paused": st.is_paused,
            })

        agg = {
            "active_pairs": len(_bot_states),
            "today_profit": round(sum(st.today_profit_usd for st in _bot_states.values()), 4),
            "total_profit": round(sum(st.total_profit_usd for st in _bot_states.values()), 4),
            "trips_today": sum(st.round_trips_today for st in _bot_states.values()),
            "total_trips": sum(st.total_round_trips for st in _bot_states.values()),
        }
        self._send_json({"aggregate": agg, "pairs": pairs_list})

    def _handle_swarm_available(self):
        """GET /api/swarm/available -- pair scanner results (all qualifying)."""
        try:
            ranked = pair_scanner.get_ranked_pairs(min_volume_usd=1000)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
            return

        active_set = set(_bot_states.keys())
        result = []
        for pi in ranked:
            result.append({
                "pair": pi.pair,
                "altname": pi.altname,
                "base": pi.base,
                "display": pair_scanner.get_display_name(pi.pair, pi.altname,
                                                         pi.quote),
                "price": round(pi.price, 8),
                "volatility_pct": round(pi.volatility_pct, 2),
                "volume_24h": round(pi.volume_24h, 0),
                "spread_pct": round(pi.spread_pct, 3),
                "fee_maker": pi.fee_maker,
                "ordermin": pi.ordermin,
                "score": getattr(pi, "score", 0),
                "recovery_mode": getattr(pi, "recovery_mode", "lottery"),
                "active": pi.pair in active_set,
            })
        self._send_json(result)

    def _handle_swarm_add(self):
        """POST /api/swarm/add -- queue a pair for addition."""
        global _swarm_pending_adds
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "invalid JSON"}, 400)
            return

        pair_name = body.get("pair", "")
        if not pair_name:
            self._send_json({"error": "missing 'pair' field"}, 400)
            return

        if pair_name in _bot_states:
            self._send_json({"error": f"{pair_name} already active"}, 400)
            return

        # Look up pair info from scanner cache
        all_pairs = pair_scanner.scan_all_pairs()
        info = all_pairs.get(pair_name)
        if not info:
            self._send_json({"error": f"unknown pair: {pair_name}"}, 404)
            return

        pc = pair_scanner.auto_configure(info)
        _swarm_pending_adds = _swarm_pending_adds + [pc]
        self._send_json({"ok": True, "pair": pair_name, "display": pc.display})

    def _handle_swarm_remove(self):
        """POST /api/swarm/remove -- queue a pair for removal."""
        global _swarm_pending_removes
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "invalid JSON"}, 400)
            return

        pair_name = body.get("pair", "")
        if not pair_name:
            self._send_json({"error": "missing 'pair' field"}, 400)
            return

        if pair_name not in _bot_states:
            self._send_json({"error": f"{pair_name} not active"}, 400)
            return

        _swarm_pending_removes = _swarm_pending_removes + [pair_name]
        self._send_json({"ok": True, "pair": pair_name})

    def _handle_swarm_multiplier(self):
        """POST /api/swarm/multiplier -- set entry size multiplier for a pair."""
        global _swarm_pending_multipliers
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "invalid JSON"}, 400)
            return

        pair_name = body.get("pair", "")
        mult = body.get("multiplier", 1)
        if not pair_name:
            self._send_json({"error": "missing 'pair' field"}, 400)
            return
        try:
            mult = float(mult)
            if mult < 1 or mult > 10:
                self._send_json({"error": "multiplier must be 1-10"}, 400)
                return
        except (ValueError, TypeError):
            self._send_json({"error": "multiplier must be a number"}, 400)
            return

        if pair_name not in _bot_states:
            self._send_json({"error": f"{pair_name} not active"}, 400)
            return

        _swarm_pending_multipliers = _swarm_pending_multipliers + [(pair_name, mult)]
        self._send_json({"ok": True, "pair": pair_name, "multiplier": mult})

    def _handle_api_config(self):
        """POST /api/config -- validate and queue config changes."""
        global _web_config_pending

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "invalid JSON"}, 400)
            return

        errors = []
        queued = []
        pending = {}

        # Validate spacing
        if "spacing" in body:
            val = body["spacing"]
            try:
                val = float(val)
                floor = config.ROUND_TRIP_FEE_PCT + 0.1
                if val < floor:
                    errors.append(f"spacing must be >= {floor:.2f}% (fees + 0.1%)")
                else:
                    pending["spacing"] = round(val, 4)
                    queued.append("spacing")
            except (ValueError, TypeError):
                errors.append("spacing must be a number")

        # Validate ratio
        if "ratio" in body:
            val = body["ratio"]
            if isinstance(val, str) and val.lower() == "auto":
                pending["ratio"] = "auto"
                queued.append("ratio=auto")
            else:
                try:
                    val = float(val)
                    if val < 0.25 or val > 0.75:
                        errors.append("ratio must be between 0.25 and 0.75")
                    else:
                        pending["ratio"] = round(val, 4)
                        queued.append("ratio")
                except (ValueError, TypeError):
                    errors.append("ratio must be a number or 'auto'")

        # Validate entry_pct (pair mode)
        if "entry_pct" in body:
            val = body["entry_pct"]
            try:
                val = float(val)
                if val < 0.05:
                    errors.append("entry_pct must be >= 0.05%")
                else:
                    pending["entry_pct"] = round(val, 4)
                    queued.append("entry_pct")
            except (ValueError, TypeError):
                errors.append("entry_pct must be a number")

        # AI check (manual trigger)
        if "ai_check" in body:
            pending["ai_check"] = True
            queued.append("ai_check")

        if errors:
            self._send_json({"error": "; ".join(errors)}, 400)
            return

        if not queued:
            self._send_json({"error": "no valid fields provided"}, 400)
            return

        # Thread-safe: assign a new dict (atomic in CPython)
        _web_config_pending = pending
        self._send_json({"ok": True, "queued": queued})

    def _send_json(self, data, status=200):
        """Send a JSON response."""
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        """Send an HTML response."""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default HTTP logging (too noisy)."""
        pass


def start_health_server():
    """Start the health check HTTP server in a daemon thread."""
    if config.HEALTH_PORT <= 0:
        logger.info("Health check server disabled")
        return None

    try:
        server = ThreadingHTTPServer(("0.0.0.0", config.HEALTH_PORT), DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info("Health check server running on port %d", config.HEALTH_PORT)
        return server
    except Exception as e:
        logger.warning("Failed to start health server on port %d: %s", config.HEALTH_PORT, e)
        return None


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _signal_handler(signum, frame):
    """
    Handle SIGTERM (Railway) and SIGINT (Ctrl+C).
    Sets a flag that the main loop checks -- we don't exit immediately
    because we need to cancel orders first.
    """
    global _shutdown_requested
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    logger.info("Received %s -- initiating graceful shutdown...", sig_name)
    _shutdown_requested = True


def setup_signal_handlers():
    """Register signal handlers for graceful shutdown."""
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    # SIGBREAK is Windows' equivalent of SIGTERM in some contexts
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)


# ---------------------------------------------------------------------------
# Web config integration
# ---------------------------------------------------------------------------

def _apply_web_config(state: grid_strategy.GridState, current_price: float):
    """
    Check for pending web config changes and apply them.
    Called from the main loop each iteration (main thread only).
    """
    global _web_config_pending

    if not _web_config_pending:
        return

    # Copy + clear (atomic read in CPython)
    pending = _web_config_pending
    _web_config_pending = {}

    needs_rebuild = False

    # Apply spacing
    if "spacing" in pending:
        if config.STRATEGY_MODE == "pair":
            old = state.profit_pct
            state.profit_pct = pending["spacing"]
            logger.info("Web dashboard: profit target %.2f%% -> %.2f%% (applies to next exit)", old, state.profit_pct)
        else:
            old = config.GRID_SPACING_PCT
            config.GRID_SPACING_PCT = pending["spacing"]
            logger.info("Web dashboard: spacing %.2f%% -> %.2f%%", old, config.GRID_SPACING_PCT)
            needs_rebuild = True

    # Apply ratio
    if "ratio" in pending:
        val = pending["ratio"]
        if val == "auto":
            state.trend_ratio_override = None
            grid_strategy.update_trend_ratio(state)
            logger.info("Web dashboard: ratio set to auto (%.2f)", state.trend_ratio)
            needs_rebuild = True
        else:
            state.trend_ratio = val
            state.trend_ratio_override = val
            logger.info("Web dashboard: ratio manually set to %.2f", val)
            needs_rebuild = True

    # Apply entry_pct (pair mode)
    if "entry_pct" in pending:
        old = state.entry_pct
        state.entry_pct = pending["entry_pct"]
        logger.info("Web dashboard: entry distance %.2f%% -> %.2f%%", old, state.entry_pct)
        if config.STRATEGY_MODE == "pair":
            grid_strategy.replace_entries_at_distance(state, current_price)
        else:
            needs_rebuild = True

    # AI check (manual trigger, no rebuild)
    if "ai_check" in pending:
        state.last_ai_check = 0
        logger.info("Web dashboard: AI check requested")

    # Rebuild grid if spacing or ratio changed
    if needs_rebuild:
        grid_strategy.cancel_grid(state)
        orders = grid_strategy.build_grid(state, current_price)
        logger.info("Web dashboard: grid rebuilt -- %d orders around $%.6f", len(orders), current_price)


# ---------------------------------------------------------------------------
# AI approval workflow
# ---------------------------------------------------------------------------

def _set_pending_approval(action: str, message_id: int, recommendation: dict):
    """
    Store a pending approval.  Overwrites any previous pending approval
    (new recommendation supersedes the old one).
    """
    global _pending_approval

    # If there's already a pending approval, expire it
    if _pending_approval:
        old_action = _pending_approval["action"]
        old_msg_id = _pending_approval["message_id"]
        logger.info("New recommendation overwrites pending '%s'", old_action)
        ai_advisor.log_approval_decision(old_action, "expired")
        notifier.edit_message_text(
            old_msg_id,
            f"🧠 <b>AI Council</b> -- <i>{old_action}</i>\n\n"
            f"<s>Expired</s> (superseded by new recommendation)",
        )

    _pending_approval = {
        "action": action,
        "message_id": message_id,
        "created_at": time.time(),
        "recommendation": recommendation,
    }
    logger.info("Pending approval set: %s (msg_id=%d)", action, message_id)


def _check_approval_expiry():
    """Expire the pending approval if it's older than APPROVAL_TIMEOUT."""
    global _pending_approval

    if not _pending_approval:
        return

    age = time.time() - _pending_approval["created_at"]
    if age >= APPROVAL_TIMEOUT:
        action = _pending_approval["action"]
        msg_id = _pending_approval["message_id"]
        logger.info("Approval expired for '%s' after %ds", action, int(age))
        ai_advisor.log_approval_decision(action, "expired")
        notifier.edit_message_text(
            msg_id,
            f"🧠 <b>AI Council</b> -- <i>{action}</i>\n\n"
            f"<s>Expired</s> (no response in {APPROVAL_TIMEOUT // 60} min)",
        )
        _pending_approval = None


def _poll_and_handle_callbacks(state: grid_strategy.GridState, current_price: float):
    """
    Poll Telegram for button presses and text commands, then handle them.
    """
    global _pending_approval

    callbacks, commands = notifier.poll_updates()

    # Handle text commands
    if commands:
        _handle_text_commands(state, current_price, commands)

    if not callbacks:
        return

    for cb in callbacks:
        data = cb["data"]            # e.g. "approve:widen_spacing"
        cb_msg_id = cb["message_id"]
        callback_id = cb["callback_id"]

        # Menu navigation callbacks (m:screen)
        if data.startswith("m:"):
            screen = data[2:]
            _handle_menu_nav(state, current_price, screen, cb_msg_id, callback_id)
            continue

        # Menu action callbacks (ma:action)
        if data.startswith("ma:"):
            action = data[3:]
            _handle_menu_action(state, current_price, action, cb_msg_id, callback_id)
            continue

        # Only process if it matches the pending approval's message
        if not _pending_approval or cb_msg_id != _pending_approval["message_id"]:
            notifier.answer_callback(callback_id, "No pending action for this message")
            continue

        parts = data.split(":", 1)
        if len(parts) != 2:
            notifier.answer_callback(callback_id, "Invalid callback")
            continue

        decision, action = parts  # "approve" or "skip", and the action name

        if decision == "approve":
            notifier.answer_callback(callback_id, f"Approved: {action}")
            logger.info("User APPROVED action: %s", action)
            ai_advisor.log_approval_decision(action, "approved")

            # Execute the action
            _execute_approved_action(state, action, current_price)

            # Update the message to show it was approved
            notifier.edit_message_text(
                cb_msg_id,
                f"🧠 <b>AI Council</b> -- <i>{action}</i>\n\n"
                f"Approved and executed.",
            )
            _pending_approval = None

        elif decision == "skip":
            notifier.answer_callback(callback_id, "Skipped")
            logger.info("User SKIPPED action: %s", action)
            ai_advisor.log_approval_decision(action, "skipped")

            notifier.edit_message_text(
                cb_msg_id,
                f"🧠 <b>AI Council</b> -- <i>{action}</i>\n\n"
                f"<s>Skipped</s> by user.",
            )
            _pending_approval = None

        else:
            notifier.answer_callback(callback_id, "Unknown decision")


def _execute_approved_action(state: grid_strategy.GridState, action: str,
                             current_price: float):
    """
    Execute an approved AI recommendation.

    Actions:
      widen_spacing   -> increase GRID_SPACING_PCT by 0.25%, rebuild grid
      tighten_spacing -> decrease GRID_SPACING_PCT by 0.25% (floor at fees+0.1%), rebuild grid
      pause           -> pause the bot
      reset_grid      -> cancel and rebuild grid at current price
    """
    # Normalize AI prompt aliases to handler names
    ACTION_ALIASES = {"widen_profit": "widen_spacing", "tighten_profit": "tighten_spacing"}
    action = ACTION_ALIASES.get(action, action)

    if action == "widen_spacing":
        if config.STRATEGY_MODE == "pair":
            old = state.profit_pct
            state.profit_pct = round(old + SPACING_STEP_PCT, 4)
            logger.info("Profit target widened: %.2f%% -> %.2f%%", old, state.profit_pct)
            grid_strategy.cancel_grid(state)
            orders = grid_strategy.build_grid(state, current_price)
            notifier._send_message(
                f"Profit target widened: {old:.2f}% -> {state.profit_pct:.2f}%\n"
                f"Pair rebuilt: {len(orders)} orders around ${current_price:.6f}"
            )
        else:
            old = config.GRID_SPACING_PCT
            config.GRID_SPACING_PCT = round(old + SPACING_STEP_PCT, 4)
            logger.info("Spacing widened: %.2f%% -> %.2f%%", old, config.GRID_SPACING_PCT)
            grid_strategy.cancel_grid(state)
            orders = grid_strategy.build_grid(state, current_price)
            notifier._send_message(
                f"Spacing widened: {old:.2f}% -> {config.GRID_SPACING_PCT:.2f}%\n"
                f"Grid rebuilt: {len(orders)} orders around ${current_price:.6f}"
            )

    elif action == "tighten_spacing":
        floor = config.ROUND_TRIP_FEE_PCT + 0.1
        if config.STRATEGY_MODE == "pair":
            old = state.profit_pct
            new_val = round(old - SPACING_STEP_PCT, 4)
            if new_val < floor:
                logger.warning(
                    "Cannot tighten below %.2f%% (fees+0.1%%). Current: %.2f%%",
                    floor, old,
                )
                notifier._send_message(
                    f"Cannot tighten profit target below {floor:.2f}% "
                    f"(round-trip fees + 0.1%%). Current: {old:.2f}%"
                )
                return
            state.profit_pct = new_val
            logger.info("Profit target tightened: %.2f%% -> %.2f%%", old, new_val)
            grid_strategy.cancel_grid(state)
            orders = grid_strategy.build_grid(state, current_price)
            notifier._send_message(
                f"Profit target tightened: {old:.2f}% -> {new_val:.2f}%\n"
                f"Pair rebuilt: {len(orders)} orders around ${current_price:.6f}"
            )
        else:
            old = config.GRID_SPACING_PCT
            new_spacing = round(old - SPACING_STEP_PCT, 4)
            if new_spacing < floor:
                logger.warning(
                    "Cannot tighten below %.2f%% (fees+0.1%%). Current: %.2f%%",
                    floor, old,
                )
                notifier._send_message(
                    f"Cannot tighten spacing below {floor:.2f}% "
                    f"(round-trip fees + 0.1%%). Current: {old:.2f}%"
                )
                return
            config.GRID_SPACING_PCT = new_spacing
            logger.info("Spacing tightened: %.2f%% -> %.2f%%", old, config.GRID_SPACING_PCT)
            grid_strategy.cancel_grid(state)
            orders = grid_strategy.build_grid(state, current_price)
            notifier._send_message(
                f"Spacing tightened: {old:.2f}% -> {config.GRID_SPACING_PCT:.2f}%\n"
                f"Grid rebuilt: {len(orders)} orders around ${current_price:.6f}"
            )

    elif action == "pause":
        state.is_paused = True
        state.pause_reason = "AI advisor (user approved)"
        logger.info("Bot paused by approved AI recommendation")
        notifier.notify_risk_event("pause", "Paused by approved AI recommendation")

    elif action == "widen_entry":
        old = state.entry_pct
        state.entry_pct = round(old + ENTRY_STEP_PCT, 4)
        logger.info("Entry distance widened: %.2f%% -> %.2f%%", old, state.entry_pct)
        grid_strategy.cancel_grid(state)
        orders = grid_strategy.build_grid(state, current_price)
        notifier._send_message(
            f"Entry distance widened: {old:.2f}% -> {state.entry_pct:.2f}%\n"
            f"Pair rebuilt: {len(orders)} orders around ${current_price:.6f}"
        )

    elif action == "tighten_entry":
        old = state.entry_pct
        new_val = round(old - ENTRY_STEP_PCT, 4)
        floor = 0.05
        if new_val < floor:
            logger.warning(
                "Cannot tighten entry below %.2f%%. Current: %.2f%%",
                floor, old,
            )
            notifier._send_message(
                f"Cannot tighten entry below {floor:.2f}%. Current: {old:.2f}%"
            )
            return
        state.entry_pct = new_val
        logger.info("Entry distance tightened: %.2f%% -> %.2f%%", old, new_val)
        grid_strategy.cancel_grid(state)
        orders = grid_strategy.build_grid(state, current_price)
        notifier._send_message(
            f"Entry distance tightened: {old:.2f}% -> {new_val:.2f}%\n"
            f"Pair rebuilt: {len(orders)} orders around ${current_price:.6f}"
        )

    elif action == "reset_grid":
        logger.info("Resetting grid by approved AI recommendation")
        grid_strategy.cancel_grid(state)
        orders = grid_strategy.build_grid(state, current_price)
        notifier.notify_grid_built(current_price, len(orders))

    else:
        logger.warning("Unknown approved action: %s -- ignoring", action)


# ---------------------------------------------------------------------------
# Telegram text commands
# ---------------------------------------------------------------------------

def _handle_text_commands(state: grid_strategy.GridState, current_price: float,
                          commands: list):
    """
    Dispatch Telegram text commands (e.g. /status, /interval 600).
    Each command gets a reply message sent back to Telegram.
    """
    for cmd in commands:
        text = cmd["text"].strip()
        parts = text.split(None, 1)  # split on first whitespace
        command = parts[0].lower()
        # Strip @botname suffix (e.g. /help@MyBot)
        if "@" in command:
            command = command.split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        logger.info("Telegram command: %s (arg=%r)", command, arg)

        if command == "/interval":
            _cmd_interval(arg)
        elif command == "/check":
            _cmd_check(state)
        elif command == "/status":
            _cmd_status(state, current_price)
        elif command == "/spacing":
            _cmd_spacing(state, current_price, arg)
        elif command == "/ratio":
            _cmd_ratio(state, current_price, arg)
        elif command == "/stats":
            _cmd_stats(state)
        elif command == "/menu":
            _cmd_menu()
        elif command == "/resetbackoff":
            _cmd_resetbackoff(state)
        elif command == "/help":
            _cmd_help()
        else:
            notifier._send_message(f"Unknown command: <code>{command}</code>\nSend /help for available commands.")


def _cmd_interval(arg: str):
    """Handle /interval -- inform user AI is now manual-only."""
    notifier._send_message("AI polling is now manual-only. Use /check to trigger.")


def _cmd_check(state: grid_strategy.GridState):
    """Handle /check -- trigger immediate AI advisor check."""
    state.last_ai_check = 0
    notifier._send_message("AI check will run next cycle (~30s)")
    logger.info("Immediate AI check requested via Telegram")


def _cmd_status(state: grid_strategy.GridState, current_price: float):
    """Handle /status -- reply with current bot status."""
    summary = grid_strategy.get_status_summary(state, current_price)
    # Wrap in <pre> for monospace formatting in Telegram
    notifier._send_message(f"<pre>{summary}</pre>")


def _cmd_spacing(state: grid_strategy.GridState, current_price: float, arg: str):
    """Handle /spacing <pct> -- set grid spacing and rebuild grid."""
    try:
        new_spacing = float(arg)
    except (ValueError, TypeError):
        notifier._send_message("Usage: /spacing &lt;percent&gt;\nExample: /spacing 1.5")
        return

    floor = config.ROUND_TRIP_FEE_PCT + 0.1
    if new_spacing < floor:
        notifier._send_message(
            f"Spacing must be >= {floor:.2f}% (fees + 0.1%).\n"
            f"Current: {config.GRID_SPACING_PCT:.2f}%"
        )
        return

    old = config.GRID_SPACING_PCT
    config.GRID_SPACING_PCT = round(new_spacing, 4)
    logger.info("GRID_SPACING_PCT changed %.2f%% -> %.2f%% via Telegram", old, new_spacing)

    grid_strategy.cancel_grid(state)
    orders = grid_strategy.build_grid(state, current_price)

    notifier._send_message(
        f"Spacing: {old:.2f}% -> {config.GRID_SPACING_PCT:.2f}%\n"
        f"Grid rebuilt: {len(orders)} orders around ${current_price:.6f}"
    )


def _cmd_ratio(state: grid_strategy.GridState, current_price: float, arg: str):
    """
    Handle /ratio command.
      /ratio        -- show current ratio and fill counts
      /ratio <0-1>  -- manually set ratio and rebuild
      /ratio auto   -- clear manual override, return to fill-based
    """
    if not arg:
        # Show current ratio info
        now = time.time()
        cutoff = now - grid_strategy.TREND_WINDOW_SECONDS
        buy_count = sum(1 for f in state.recent_fills
                        if f.get("time", 0) > cutoff and f["side"] == "buy")
        sell_count = sum(1 for f in state.recent_fills
                         if f.get("time", 0) > cutoff and f["side"] == "sell")
        total_grid = config.GRID_LEVELS * 2
        n_buys = max(2, min(total_grid - 2, round(total_grid * state.trend_ratio)))
        n_sells = total_grid - n_buys
        src = "manual" if state.trend_ratio_override is not None else "auto"
        notifier._send_message(
            f"<b>Trend Ratio</b> [{src}]\n\n"
            f"Ratio: {state.trend_ratio:.2f} ({state.trend_ratio:.0%} buy / {1-state.trend_ratio:.0%} sell)\n"
            f"Grid split: {n_buys}B + {n_sells}S\n"
            f"12h fills: {buy_count} buys, {sell_count} sells ({buy_count+sell_count} total)"
        )
        return

    if arg.lower() == "auto":
        state.trend_ratio_override = None
        grid_strategy.update_trend_ratio(state)
        logger.info("Trend ratio set to auto (%.2f) via Telegram", state.trend_ratio)
        grid_strategy.cancel_grid(state)
        orders = grid_strategy.build_grid(state, current_price)
        notifier._send_message(
            f"Ratio set to auto ({state.trend_ratio:.2f})\n"
            f"Grid rebuilt: {len(orders)} orders around ${current_price:.6f}"
        )
        return

    try:
        value = float(arg)
    except (ValueError, TypeError):
        notifier._send_message(
            "Usage:\n"
            "/ratio -- show current ratio\n"
            "/ratio &lt;0.25-0.75&gt; -- set manual ratio\n"
            "/ratio auto -- return to fill-based"
        )
        return

    if value < 0.25 or value > 0.75:
        notifier._send_message("Ratio must be between 0.25 and 0.75.")
        return

    state.trend_ratio = value
    state.trend_ratio_override = value
    logger.info("Trend ratio manually set to %.2f via Telegram", value)

    grid_strategy.cancel_grid(state)
    orders = grid_strategy.build_grid(state, current_price)
    notifier._send_message(
        f"Ratio manually set to {value:.2f}\n"
        f"Grid rebuilt: {len(orders)} orders around ${current_price:.6f}"
    )


def _cmd_stats(state: grid_strategy.GridState):
    """Handle /stats -- show statistical analysis results."""
    results = state.stats_results
    if not results:
        notifier._send_message("Stats engine hasn't run yet. Wait ~60s for first analysis.")
        return

    lines = ["<b>Statistical Advisory Board</b>\n"]

    health = results.get("overall_health", {})
    if health:
        lines.append(f"Overall: <b>{health.get('verdict', 'N/A').upper()}</b>")
        lines.append(f"{health.get('summary', '')}\n")

    for name in ("profitability", "fill_asymmetry", "grid_exceedance",
                 "fill_rate", "random_walk"):
        r = results.get(name)
        if not r:
            continue
        label = name.replace("_", " ").title()
        verdict = r.get("verdict", "?").replace("_", " ").upper()
        lines.append(f"<b>{label}</b>: {verdict}")
        lines.append(f"  {r.get('summary', '')}")

    notifier._send_message("\n".join(lines))


def _cmd_resetbackoff(state: grid_strategy.GridState):
    """Handle /resetbackoff -- clear consecutive loss counters."""
    old_a = state.consecutive_losses_a
    old_b = state.consecutive_losses_b
    state.consecutive_losses_a = 0
    state.consecutive_losses_b = 0
    grid_strategy.save_state(state)
    logger.info("Backoff counters reset via Telegram (was A=%d, B=%d)", old_a, old_b)
    notifier._send_message(
        f"Backoff counters reset to 0.\n"
        f"Was: A={old_a} losses, B={old_b} losses\n"
        f"Entry distance returns to base: {state.entry_pct:.2f}%"
    )


def _cmd_help():
    """Handle /help -- list available commands."""
    notifier._send_message(
        "<b>Bot Commands</b>\n\n"
        "/status -- Current bot status\n"
        "/menu -- Interactive button menu\n"
        "/interval &lt;sec&gt; -- Set AI check interval (60-86400)\n"
        "/check -- Trigger AI check next cycle\n"
        "/spacing &lt;pct&gt; -- Set grid spacing % and rebuild\n"
        "/ratio -- Show/set trend ratio (asymmetric grid)\n"
        "/stats -- Statistical analysis results\n"
        "/resetbackoff -- Clear entry backoff counters\n"
        "/help -- This message"
    )


def _cmd_menu():
    """Handle /menu -- send interactive button menu."""
    text, keyboard = telegram_menu.build_main_menu()
    notifier.send_with_buttons(text, keyboard)


def _handle_menu_nav(state: grid_strategy.GridState, current_price: float,
                     screen: str, message_id: int, callback_id: str):
    """Navigate to a menu screen by editing the existing message."""
    if screen == "main":
        text, keyboard = telegram_menu.build_main_menu()
    elif screen == "status":
        text, keyboard = telegram_menu.build_status_screen(state, current_price)
    elif screen == "grid":
        text, keyboard = telegram_menu.build_grid_screen(state, current_price)
    elif screen == "stats":
        text, keyboard = telegram_menu.build_stats_screen(state)
    elif screen == "settings":
        text, keyboard = telegram_menu.build_settings_screen(state)
    else:
        notifier.answer_callback(callback_id, "Unknown screen")
        return

    notifier.answer_callback(callback_id)
    # Build reply_markup in Telegram API format
    reply_markup = {"inline_keyboard": [
        [{"text": b["text"], "callback_data": b["callback_data"]} for b in row]
        for row in keyboard
    ]}
    notifier.edit_message_text(message_id, text, reply_markup=reply_markup)


def _handle_menu_action(state: grid_strategy.GridState, current_price: float,
                        action: str, message_id: int, callback_id: str):
    """Execute a settings action from the menu, then refresh the settings screen."""
    if action == "spacing_up":
        if config.STRATEGY_MODE == "pair":
            old = state.profit_pct
            state.profit_pct = round(old + SPACING_STEP_PCT, 4)
            notifier.answer_callback(callback_id, f"Profit: {old:.2f}% -> {state.profit_pct:.2f}%")
            logger.info("Menu: profit target widened %.2f%% -> %.2f%%", old, state.profit_pct)
        else:
            old = config.GRID_SPACING_PCT
            config.GRID_SPACING_PCT = round(old + SPACING_STEP_PCT, 4)
            notifier.answer_callback(callback_id, f"Spacing: {old:.2f}% -> {config.GRID_SPACING_PCT:.2f}%")
            logger.info("Menu: spacing widened %.2f%% -> %.2f%%", old, config.GRID_SPACING_PCT)
        grid_strategy.cancel_grid(state)
        grid_strategy.build_grid(state, current_price)

    elif action == "spacing_down":
        floor = config.ROUND_TRIP_FEE_PCT + 0.1
        if config.STRATEGY_MODE == "pair":
            old = state.profit_pct
            new_val = round(old - SPACING_STEP_PCT, 4)
            if new_val < floor:
                notifier.answer_callback(callback_id, f"Can't go below {floor:.2f}%")
                return
            state.profit_pct = new_val
            notifier.answer_callback(callback_id, f"Profit: {old:.2f}% -> {new_val:.2f}%")
            logger.info("Menu: profit target tightened %.2f%% -> %.2f%%", old, new_val)
        else:
            old = config.GRID_SPACING_PCT
            new_val = round(old - SPACING_STEP_PCT, 4)
            if new_val < floor:
                notifier.answer_callback(callback_id, f"Can't go below {floor:.2f}%")
                return
            config.GRID_SPACING_PCT = new_val
            notifier.answer_callback(callback_id, f"Spacing: {old:.2f}% -> {new_val:.2f}%")
            logger.info("Menu: spacing tightened %.2f%% -> %.2f%%", old, new_val)
        grid_strategy.cancel_grid(state)
        grid_strategy.build_grid(state, current_price)

    elif action == "ratio_auto":
        state.trend_ratio_override = None
        grid_strategy.update_trend_ratio(state)
        notifier.answer_callback(callback_id, f"Ratio auto: {state.trend_ratio:.2f}")
        logger.info("Menu: ratio set to auto (%.2f)", state.trend_ratio)
        grid_strategy.cancel_grid(state)
        grid_strategy.build_grid(state, current_price)

    elif action == "entry_up":
        old = state.entry_pct
        state.entry_pct = round(old + ENTRY_STEP_PCT, 4)
        notifier.answer_callback(callback_id, f"Entry: {old:.2f}% -> {state.entry_pct:.2f}%")
        logger.info("Menu: entry distance widened %.2f%% -> %.2f%%", old, state.entry_pct)
        grid_strategy.cancel_grid(state)
        grid_strategy.build_grid(state, current_price)

    elif action == "entry_down":
        old = state.entry_pct
        new_val = round(old - ENTRY_STEP_PCT, 4)
        floor = 0.05
        if new_val < floor:
            notifier.answer_callback(callback_id, f"Can't go below {floor:.2f}%")
            return
        state.entry_pct = new_val
        notifier.answer_callback(callback_id, f"Entry: {old:.2f}% -> {new_val:.2f}%")
        logger.info("Menu: entry distance tightened %.2f%% -> %.2f%%", old, new_val)
        grid_strategy.cancel_grid(state)
        grid_strategy.build_grid(state, current_price)

    elif action == "ai_check":
        state.last_ai_check = 0
        notifier.answer_callback(callback_id, "AI check queued")
        logger.info("Menu: immediate AI check requested")

    else:
        notifier.answer_callback(callback_id, "Unknown action")
        return

    # Refresh settings screen
    text, keyboard = telegram_menu.build_settings_screen(state)
    reply_markup = {"inline_keyboard": [
        [{"text": b["text"], "callback_data": b["callback_data"]} for b in row]
        for row in keyboard
    ]}
    notifier.edit_message_text(message_id, text, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Active pairs persistence (Step 6)
# ---------------------------------------------------------------------------

ACTIVE_PAIRS_FILE = os.path.join(config.LOG_DIR, "active_pairs.json")


def _save_active_pairs():
    """Save the active pair list to disk and Supabase."""
    pairs_data = []
    for pair_name, state in _bot_states.items():
        if state.pair_config:
            pairs_data.append(state.pair_config.to_dict())
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        tmp_path = ACTIVE_PAIRS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(pairs_data, f, indent=2)
        if os.path.exists(ACTIVE_PAIRS_FILE):
            os.remove(ACTIVE_PAIRS_FILE)
        os.rename(tmp_path, ACTIVE_PAIRS_FILE)
        logger.debug("Saved %d active pairs to %s", len(pairs_data), ACTIVE_PAIRS_FILE)
    except Exception as e:
        logger.error("Failed to save active pairs: %s", e)
    # Also save to Supabase
    supabase_store.save_state({"active_pairs": pairs_data}, pair="__swarm__")


def _load_active_pairs() -> list:
    """Load active pairs from file, returns list of PairConfig dicts (or empty)."""
    if os.path.exists(ACTIVE_PAIRS_FILE):
        try:
            with open(ACTIVE_PAIRS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                logger.info("Loaded %d active pairs from %s", len(data), ACTIVE_PAIRS_FILE)
                return data
        except Exception as e:
            logger.warning("Failed to load active pairs file: %s", e)

    # Try Supabase
    sb = supabase_store.load_state(pair="__swarm__")
    if sb and "active_pairs" in sb:
        data = sb["active_pairs"]
        if isinstance(data, list) and data:
            logger.info("Loaded %d active pairs from Supabase", len(data))
            return data

    return []


# ---------------------------------------------------------------------------
# Dynamic pair add/remove (Step 3d)
# ---------------------------------------------------------------------------

def _rebalance_capital_budgets():
    """Redistribute capital evenly across active pairs."""
    n = len(_bot_states)
    if n == 0:
        return
    per_pair = config.STARTING_CAPITAL / n
    for state in _bot_states.values():
        if state.pair_config:
            state.pair_config.capital_budget_usd = per_pair
    logger.info("Capital rebalanced: $%.2f per pair across %d pairs", per_pair, n)


def _add_pair(pair_config: config.PairConfig):
    """Add a new trading pair to the live bot."""
    pair_name = pair_config.pair
    if pair_name in _bot_states:
        logger.warning("Pair %s already active, skipping add", pair_name)
        return

    # Floor profit_pct so it always covers actual round-trip fees
    min_profit = round(2 * config.MAKER_FEE_PCT + 0.10, 2)
    if pair_config.profit_pct < min_profit:
        logger.info("Pair %s: profit_pct %.2f%% < fee floor %.2f%%, correcting",
                     pair_config.display, pair_config.profit_pct, min_profit)
        pair_config.profit_pct = min_profit

    state = grid_strategy.GridState(pair_config=pair_config)
    _bot_states[pair_name] = state
    config.PAIRS[pair_name] = pair_config
    _current_prices.setdefault(pair_name, 0.0)
    _ohlc_caches[pair_name] = []
    _ohlc_last_fetches[pair_name] = 0.0

    # Try to load saved state
    local_loaded = grid_strategy.load_state(state)
    if not local_loaded:
        _restore_from_supabase(state)

    # Fetch initial price (use batch cache if available)
    price = _current_prices.get(pair_name, 0.0)
    if price <= 0:
        try:
            price = kraken_client.get_price(pair=pair_name)
            _current_prices[pair_name] = price
        except Exception as e:
            logger.error("Failed to fetch price for new pair %s: %s", pair_name, e)
            # Remove failed pair
            _bot_states.pop(pair_name, None)
            config.PAIRS.pop(pair_name, None)
            return

    grid_strategy.record_price(state, price)

    # Build grid
    adopted = grid_strategy.reconcile_on_startup(state, price)
    if adopted > 0:
        logger.info("[%s] Reconciliation adopted %d orders", pair_name, adopted)
    orders = grid_strategy.build_grid(state, price)
    deduped = grid_strategy.enforce_pair_order_limit(state)
    grid_strategy.check_daily_reset(state)

    logger.info("Added pair %s (%s): %d orders @ $%.6f",
                pair_config.display, pair_name, len(orders), price)
    _rebalance_capital_budgets()
    _save_active_pairs()


def _remove_pair(pair_name: str):
    """Remove a trading pair from the live bot."""
    if pair_name not in _bot_states:
        logger.warning("Pair %s not active, skipping remove", pair_name)
        return

    state = _bot_states[pair_name]

    # Cancel all orders
    cancelled = grid_strategy.cancel_grid(state)
    logger.info("[%s] Cancelled %d orders for removal", pair_name, cancelled)

    # Save final state
    grid_strategy.save_state(state)

    # Clean up
    _bot_states.pop(pair_name, None)
    config.PAIRS.pop(pair_name, None)
    _current_prices.pop(pair_name, None)
    _ohlc_caches.pop(pair_name, None)
    _ohlc_last_fetches.pop(pair_name, None)

    logger.info("Removed pair %s", pair_name)
    _rebalance_capital_budgets()
    _save_active_pairs()


def _process_swarm_queue():
    """Process pending swarm add/remove/multiplier requests (called from main loop)."""
    global _swarm_pending_adds, _swarm_pending_removes, _swarm_pending_multipliers

    # Snapshot and clear queues atomically
    adds = _swarm_pending_adds
    removes = _swarm_pending_removes
    mults = _swarm_pending_multipliers
    _swarm_pending_adds = []
    _swarm_pending_removes = []
    _swarm_pending_multipliers = []

    for pc in adds:
        try:
            _add_pair(pc)
        except Exception as e:
            logger.error("Failed to add pair %s: %s", pc.pair, e)

    for pair_name in removes:
        try:
            _remove_pair(pair_name)
        except Exception as e:
            logger.error("Failed to remove pair %s: %s", pair_name, e)

    for pair_name, mult in mults:
        state = _bot_states.get(pair_name)
        if state:
            old = state.next_entry_multiplier
            state.next_entry_multiplier = mult
            logger.info("[%s] Entry multiplier: %.1fx -> %.1fx", pair_name, old, mult)


# ---------------------------------------------------------------------------
# Main bot logic
# ---------------------------------------------------------------------------

def _restore_from_supabase(state: grid_strategy.GridState):
    """Restore a single pair's state from Supabase cloud."""
    pair_name = state.pair_name
    sb_state = supabase_store.load_state(pair=pair_name)
    if not sb_state:
        return False

    state.center_price = sb_state.get("center_price", 0.0)
    state.total_profit_usd = sb_state.get("total_profit_usd", 0.0)
    state.today_profit_usd = sb_state.get("today_profit_usd", 0.0)
    state.today_loss_usd = sb_state.get("today_loss_usd", 0.0)
    state.today_fees_usd = sb_state.get("today_fees_usd", 0.0)
    state.today_date = sb_state.get("today_date", "")
    state.round_trips_today = sb_state.get("round_trips_today", 0)
    state.total_round_trips = sb_state.get("total_round_trips", 0)
    state.total_fees_usd = sb_state.get("total_fees_usd", 0.0)
    state.doge_accumulated = sb_state.get("doge_accumulated", 0.0)
    state.last_accumulation = sb_state.get("last_accumulation", 0.0)
    if config.STRATEGY_MODE != "pair":
        state.trend_ratio = sb_state.get("trend_ratio", 0.5)
        state.trend_ratio_override = sb_state.get("trend_ratio_override", None)
    # Restore pair mode identity fields
    state.pair_state = sb_state.get("pair_state", "S0")
    state.cycle_a = sb_state.get("cycle_a", 1)
    state.cycle_b = sb_state.get("cycle_b", 1)
    state._saved_open_orders = sb_state.get("open_orders", [])
    # Restore completed cycles
    saved_cycles = sb_state.get("completed_cycles", [])
    state.completed_cycles = [
        grid_strategy.CompletedCycle.from_dict(c) for c in saved_cycles
    ]
    state._pnl_migrated = sb_state.get("pnl_migrated", False)
    # Restore runtime config overrides
    if config.STRATEGY_MODE != "pair":
        saved_spacing = sb_state.get("grid_spacing_pct")
        if saved_spacing and saved_spacing != config.GRID_SPACING_PCT:
            logger.info(
                "[%s] Restoring spacing from Supabase: %.2f%% -> %.2f%%",
                pair_name, config.GRID_SPACING_PCT, saved_spacing,
            )
            config.GRID_SPACING_PCT = saved_spacing
    saved_entry_pct = sb_state.get("pair_entry_pct")
    if saved_entry_pct and saved_entry_pct != state.entry_pct:
        logger.info(
            "[%s] Restoring entry distance from Supabase: %.2f%% -> %.2f%%",
            pair_name, state.entry_pct, saved_entry_pct,
        )
        state.entry_pct = saved_entry_pct
    # AI interval no longer restored (AI is manual-only now)
    logger.info(
        "[%s] State restored from Supabase: $%.4f profit, %d round trips",
        pair_name, state.total_profit_usd, state.total_round_trips,
    )
    return True


def run():
    """
    Main bot entry point.  Runs the complete lifecycle:
    init -> build grid -> loop -> shutdown.

    Supports multiple pairs: iterates sequentially over each configured pair
    per cycle.  Rate limit budget: ~15 API calls per 30s cycle, safe for 3-4 pairs.
    """
    global _bot_states, _current_prices, _bot_healthy, _bot_start_time
    global _ohlc_caches, _ohlc_last_fetches

    # --- Phase 1: Initialize ---
    setup_logging()
    setup_signal_handlers()
    config.print_banner()

    _bot_start_time = time.time()

    # Load active pairs from persistence (overrides PAIRS env var if found)
    saved_pairs = _load_active_pairs()
    if saved_pairs:
        config.PAIRS = {}
        for pd in saved_pairs:
            pc = config.PairConfig.from_dict(pd)
            config.PAIRS[pc.pair] = pc
        logger.info("Restored %d pairs from active_pairs persistence", len(config.PAIRS))

    # Create one GridState per configured pair
    min_profit = round(2 * config.MAKER_FEE_PCT + 0.10, 2)
    for pair_name, pc in config.PAIRS.items():
        # Floor profit_pct so it always covers actual round-trip fees
        if pc.profit_pct < min_profit:
            logger.info("Pair %s: profit_pct %.2f%% < fee floor %.2f%%, correcting",
                         pc.display, pc.profit_pct, min_profit)
            pc.profit_pct = min_profit
        state = grid_strategy.GridState(pair_config=pc)
        _bot_states[pair_name] = state
        _current_prices[pair_name] = 0.0
        _ohlc_caches[pair_name] = []
        _ohlc_last_fetches[pair_name] = 0.0
        logger.info("Initialized state for %s (%s)", pc.display, pair_name)

    # Start Supabase persistence (one writer thread for all pairs)
    supabase_store.start_writer_thread()

    # Restore state per pair (local file first, then Supabase)
    for pair_name, state in _bot_states.items():
        local_loaded = grid_strategy.load_state(state)
        if not local_loaded:
            _restore_from_supabase(state)
        # Restore fills and price history from Supabase
        sb_fills = supabase_store.load_fills(pair=pair_name)
        if sb_fills:
            state.recent_fills = sb_fills
            logger.info("[%s] Restored %d fills from Supabase", pair_name, len(sb_fills))
        sb_prices = supabase_store.load_price_history(since=time.time() - 86400, pair=pair_name)
        if sb_prices:
            state.price_history = sb_prices
            logger.info("[%s] Restored %d price samples from Supabase", pair_name, len(sb_prices))
        # Retroactive P&L reconstruction from fills (once per state)
        if config.STRATEGY_MODE == "pair" and not state._pnl_migrated:
            grid_strategy.migrate_pnl_from_fills(state)
            grid_strategy.save_state(state)

    # Start health check server (Railway expects a listening port)
    health_server = start_health_server()

    # --- Phase 2: Fetch initial prices (batch) ---
    all_pair_names = list(_bot_states.keys())
    logger.info("Fetching initial prices for %d pair(s)...", len(all_pair_names))
    try:
        batch_prices = kraken_client.get_prices(all_pair_names)
        for pair_name, price in batch_prices.items():
            _current_prices[pair_name] = price
            state = _bot_states[pair_name]
            grid_strategy.record_price(state, price)
            logger.info("[%s] Price: $%.6f", state.pair_display, price)
        # Check for pairs that didn't get prices
        missing = [pn for pn in all_pair_names if _current_prices.get(pn, 0) <= 0]
        if missing:
            logger.warning("No price data for %d pair(s): %s -- retrying individually",
                           len(missing), missing[:5])
            for pn in missing:
                try:
                    price = kraken_client.get_price(pair=pn)
                    _current_prices[pn] = price
                    grid_strategy.record_price(_bot_states[pn], price)
                    logger.info("[%s] Price (fallback): $%.6f", _bot_states[pn].pair_display, price)
                except Exception as e:
                    logger.error("[%s] Failed to fetch price: %s", pn, e)
    except Exception as e:
        logger.error("Batch price fetch failed, falling back to individual: %s", e)
        for pair_name, state in _bot_states.items():
            try:
                price = kraken_client.get_price(pair=pair_name)
                _current_prices[pair_name] = price
                grid_strategy.record_price(state, price)
            except Exception as e2:
                logger.error("[%s] Failed to fetch price: %s", pair_name, e2)
                notifier.notify_error(f"Bot failed to start {state.pair_display}: cannot fetch price\n{e2}")
                return

    # --- Phase 2b: Validation guardrails ---
    first_price = _current_prices[_first_pair_name()]
    if not grid_strategy.validate_config(first_price):
        logger.critical("Pre-flight validation FAILED -- refusing to start")
        notifier.notify_error("Bot refused to start: validation failed (check logs)")
        return

    # Log startup (no Telegram notification -- reduce deploy spam)
    pair_labels = ", ".join(st.pair_display for st in _bot_states.values())
    logger.info("Bot started: %s @ $%.6f", pair_labels, first_price)

    # --- Phase 2c: Startup reconciliation + build grid per pair ---
    for pair_name, state in _bot_states.items():
        current_price = _current_prices[pair_name]
        adopted = grid_strategy.reconcile_on_startup(state, current_price)
        if adopted > 0:
            logger.info("[%s] Reconciliation adopted %d orders", pair_name, adopted)

        logger.info("[%s] Building initial grid...", state.pair_display)
        orders = grid_strategy.build_grid(state, current_price)
        logger.info("[%s] Grid built: %d orders placed", state.pair_display, len(orders))

        # Enforce pair invariant: cancel duplicate (side, role) orders
        deduped = grid_strategy.enforce_pair_order_limit(state)
        if deduped:
            logger.info("[%s] Pair dedup: cancelled %d duplicate orders", pair_name, deduped)

        grid_strategy.check_daily_reset(state)

    # --- Phase 3b: One-time reprice thin exits to new profit floor ---
    for pair_name, state in _bot_states.items():
        try:
            ticker = kraken_client.get_ticker(state.pair_name)
            cp = float(ticker.get("c", [0])[0]) if ticker else 0
            if cp > 0:
                n = grid_strategy.reprice_thin_exits(state, cp)
                if n:
                    logger.info("[%s] Repriced %d thin exits to %.2f%% floor",
                                pair_name, n, state.profit_pct)
                    grid_strategy.save_state(state)
        except Exception as e:
            logger.warning("[%s] Exit reprice failed: %s", pair_name, e)

    # --- Phase 3c: Log actual Kraken balances (fidelity check) ---
    try:
        balances = kraken_client.get_balance()
        # Filter to non-zero balances
        held = {k: float(v) for k, v in balances.items() if float(v) > 0.0001}
        logger.info("BALANCE CHECK: %s", held)
    except Exception as e:
        logger.warning("Balance check failed: %s", e)

    # --- Phase 4: Main loop ---
    _rebalance_capital_budgets()  # set per-pair budgets on startup
    logger.info("Entering main loop (poll every %ds, %d pair(s))...",
                config.POLL_INTERVAL_SECONDS, len(_bot_states))
    last_daily_summary_date = ""
    _global_consecutive_errors = 0
    swarm_limit_notified = False

    while not _shutdown_requested:
        try:
            loop_start = time.time()
            all_paused = True
            should_stop_global = False

            # ============================================================
            # Batch operations (before per-pair loop)
            # ============================================================

            # 4a-batch: Fetch all prices in 1-2 public API calls
            active_pairs = list(_bot_states.keys())
            try:
                batch_prices = kraken_client.get_prices(active_pairs)
                for pn, price in batch_prices.items():
                    _current_prices[pn] = price
                    st = _bot_states.get(pn)
                    if st:
                        grid_strategy.record_price(st, price)
                        supabase_store.queue_price_point(time.time(), price, pair=pn)
                        st.consecutive_errors = 0
                _global_consecutive_errors = 0
            except Exception as e:
                _global_consecutive_errors += 1
                logger.error("Batch price fetch failed (%d/%d): %s",
                             _global_consecutive_errors, config.MAX_CONSECUTIVE_ERRORS, e)
                if _global_consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                    logger.critical("Too many consecutive errors -- stopping bot")
                    notifier.notify_risk_event("error", f"Max errors reached: {e}")
                    should_stop_global = True

            # 4a-batch: Collect all open txids and batch query (1-2 private calls)
            if not should_stop_global and not config.DRY_RUN:
                all_txids = {}  # txid -> pair_name
                for pn, st in _bot_states.items():
                    for o in st.grid_orders:
                        if o.status == "open" and o.txid:
                            all_txids[o.txid] = pn
                    # Include recovery order txids for fill detection
                    for r in st.recovery_orders:
                        if r.txid:
                            all_txids[r.txid] = pn
                if all_txids:
                    try:
                        batch_order_info = kraken_client.query_orders_batched(list(all_txids.keys()))
                        # Distribute results to per-pair caches
                        per_pair_info = {}
                        for txid, info in batch_order_info.items():
                            pn = all_txids.get(txid)
                            if pn:
                                per_pair_info.setdefault(pn, {})[txid] = info
                        for pn, info_dict in per_pair_info.items():
                            _bot_states[pn]._cached_order_info = info_dict
                    except Exception as e:
                        logger.error("Batch order query failed: %s", e)

            if should_stop_global:
                break

            # Global swarm daily loss check
            total_today_loss = sum(
                st.today_loss_usd for st in _bot_states.values())
            if total_today_loss >= config.SWARM_DAILY_LOSS_LIMIT:
                for pname, st in list(_bot_states.items()):
                    if pname in config.SWARM_EXEMPT_PAIRS:
                        continue  # DOGE keeps running
                    if not st.is_paused:
                        st.is_paused = True
                        st.pause_reason = (
                            f"Swarm daily loss limit: "
                            f"${total_today_loss:.2f} >= "
                            f"${config.SWARM_DAILY_LOSS_LIMIT:.2f}")
                        grid_strategy.cancel_grid(st)
                        grid_strategy.save_state(st)
                        logger.warning(
                            "[%s] SWARM PAUSED: %s", pname, st.pause_reason)
                if not swarm_limit_notified:
                    notifier.notify_risk_event(
                        "swarm_daily_limit",
                        f"Swarm daily loss ${total_today_loss:.2f} hit limit "
                        f"${config.SWARM_DAILY_LOSS_LIMIT:.2f}. "
                        f"Exempt: {config.SWARM_EXEMPT_PAIRS}")
                    swarm_limit_notified = True

            # ============================================================
            # Per-pair operations
            # ============================================================
            for pair_name, state in _bot_states.items():
                current_price = _current_prices.get(pair_name, 0.0)
                if current_price <= 0:
                    continue  # No price data this cycle

                # --- 4b: Check daily reset ---
                old_date = state.today_date
                old_profit = state.today_profit_usd
                old_trips = state.round_trips_today
                old_fees = state.today_fees_usd
                grid_strategy.check_daily_reset(state)

                if old_date and old_date != state.today_date and old_date != last_daily_summary_date:
                    last_daily_summary_date = old_date
                    swarm_limit_notified = False  # reset for new day
                    # Aggregate daily summary across all pairs
                    total_day_profit = sum(st.today_profit_usd for st in _bot_states.values())
                    total_day_trips = sum(st.round_trips_today for st in _bot_states.values())
                    total_day_fees = sum(st.today_fees_usd for st in _bot_states.values())
                    total_doge = sum(st.doge_accumulated for st in _bot_states.values())
                    total_profit = sum(st.total_profit_usd for st in _bot_states.values())
                    total_trips = sum(st.total_round_trips for st in _bot_states.values())
                    notifier.notify_daily_summary(
                        date=old_date,
                        trades=total_day_trips,
                        profit=total_day_profit,
                        fees=total_day_fees,
                        doge_accumulated=total_doge,
                        total_profit=total_profit,
                        total_trips=total_trips,
                    )

                # --- 4c: Check risk limits (per-pair) ---
                should_stop, should_pause, reason = grid_strategy.check_risk_limits(
                    state, current_price
                )

                if should_stop:
                    logger.critical("[%s] STOP triggered: %s", pair_name, reason)
                    notifier.notify_risk_event("stop_floor", reason, pair_display=state.pair_display)
                    should_stop_global = True
                    break

                if should_pause and not state.is_paused:
                    state.is_paused = True
                    state.pause_reason = reason
                    grid_strategy.cancel_grid(state)
                    grid_strategy.save_state(state)
                    logger.warning("[%s] PAUSED: %s", pair_name, reason)
                    notifier.notify_risk_event("daily_limit", reason, pair_display=state.pair_display)

                if state.is_paused:
                    logger.debug("[%s] Paused: %s", pair_name, state.pause_reason)
                    continue  # Skip trading for this pair

                all_paused = False

                # --- 4d: Check for fills ---
                filled = grid_strategy.check_fills(state, current_price)

                if filled:
                    logger.info("[%s] Detected %d fill(s)", pair_name, len(filled))
                    grid_strategy.handle_fills(state, filled, current_price)

                    if config.STRATEGY_MODE != "pair":
                        grid_strategy.update_trend_ratio(state)

                    if config.STRATEGY_MODE != "pair" and abs(state.trend_ratio - state.last_build_ratio) >= 0.2:
                        logger.info(
                            "[%s] Trend ratio drift: %.2f -> %.2f -- rebuilding",
                            pair_name, state.last_build_ratio, state.trend_ratio,
                        )
                        grid_strategy.cancel_grid(state)
                        orders = grid_strategy.build_grid(state, current_price)
                        notifier.notify_grid_built(current_price, len(orders), pair_display=state.pair_display)

                    for fill_data in state.recent_fills[-len(filled):]:
                        if fill_data.get("profit", 0) != 0:
                            notifier.notify_round_trip(
                                side=fill_data["side"],
                                price=fill_data["price"],
                                volume=fill_data["volume"],
                                profit=fill_data["profit"],
                                total_profit=state.today_profit_usd,
                                trip_count=state.total_round_trips,
                                pair_display=state.pair_display,
                                trade_id=fill_data.get("trade_id"),
                                cycle=fill_data.get("cycle"),
                                entry_price=fill_data.get("entry_price"),
                            )

                    grid_strategy.prune_completed_orders(state)
                    grid_strategy.save_state(state)

                # --- 4d2: Exit lifecycle + recovery (Section 12) ---
                if config.STRATEGY_MODE == "pair" and config.RECOVERY_ENABLED:
                    lifecycle_changed = False
                    # Recompute pair_state from live orders (fixes stale state after restart)
                    fresh_state = grid_strategy._compute_pair_state(state)
                    if fresh_state != state.pair_state:
                        logger.info("[%s] pair_state corrected: %s -> %s",
                                    pair_name, state.pair_state, fresh_state)
                        state.pair_state = fresh_state
                        lifecycle_changed = True
                    # Exit drift check: orphan exits too far from market
                    if grid_strategy.check_exit_drift(state, current_price):
                        lifecycle_changed = True
                    # S1 rebalance: orphan stranded exit to restore S0 balance
                    if grid_strategy.check_s1_rebalance(state, current_price):
                        lifecycle_changed = True
                    # Check for surprise fills on recovery orders
                    cached_info = getattr(state, "_cached_order_info", None) or {}
                    if grid_strategy.check_recovery_fills(state, cached_info):
                        lifecycle_changed = True
                    # S1a/S1b: reprice stale exits (Section 12.2)
                    if grid_strategy.check_stale_exits(state, current_price):
                        lifecycle_changed = True
                    # S2: break-glass protocol (Section 12.3)
                    if grid_strategy.check_s2_break_glass(state, current_price):
                        lifecycle_changed = True
                    # Fallback: orphan exits past orphan threshold
                    if grid_strategy.check_recovery_timeout(state, current_price):
                        lifecycle_changed = True
                    if lifecycle_changed:
                        grid_strategy.save_state(state)

                # --- 4e: Check grid drift ---
                if grid_strategy.check_grid_drift(state, current_price):
                    old_center = state.center_price
                    drift_pct = abs(current_price - old_center) / old_center * 100.0

                    logger.info("[%s] Resetting grid: drift %.2f%% from $%.6f to $%.6f",
                                pair_name, drift_pct, old_center, current_price)

                    grid_strategy.cancel_grid(state)
                    orders = grid_strategy.build_grid(state, current_price)

                    notifier.notify_grid_reset(old_center, current_price, drift_pct, pair_display=state.pair_display)
                    notifier.notify_grid_built(current_price, len(orders), pair_display=state.pair_display)

            # End per-pair loop

            if should_stop_global:
                break

            if all_paused and _bot_states:
                logger.debug("All pairs paused -- waiting for daily reset")
                time.sleep(config.POLL_INTERVAL_SECONDS)
                continue

            # ============================================================
            # Global operations (once per cycle, after all pairs)
            # ============================================================
            now = time.time()

            # --- 4f: Run AI advisor (hourly, using first pair's context) ---
            ai_state = _first_state()
            ai_price = _current_prices.get(_first_pair_name(), 0.0)
            if ai_state and ai_state.last_ai_check == 0:
                ai_state.last_ai_check = now
                ai_pair = _first_pair_name()

                # Force-refresh stats before AI call
                try:
                    ohlc_last = _ohlc_last_fetches.get(ai_pair, 0.0)
                    if now - ohlc_last >= 300:
                        try:
                            _ohlc_caches[ai_pair] = kraken_client.get_ohlc(pair=ai_pair, interval=5)
                            _ohlc_last_fetches[ai_pair] = now
                        except Exception as e:
                            logger.debug("OHLC fetch failed: %s", e)
                    ai_state.stats_results = stats_engine.run_all(
                        ai_state, ai_price, _ohlc_caches.get(ai_pair, []))
                    ai_state.stats_last_run = now
                except Exception as e:
                    logger.debug("Stats refresh before AI failed: %s", e)

                price_changes = grid_strategy.get_price_changes(ai_state, ai_price)
                _, _, spread_pct = kraken_client.get_spread(pair=ai_pair)

                hour_ago = now - 3600
                recent_count = len([
                    f for f in ai_state.recent_fills
                    if f.get("time", 0) > hour_ago
                ])

                if config.STRATEGY_MODE == "pair":
                    def _serialize_trade(trade_id, st):
                        orders = [o for o in st.grid_orders
                                  if getattr(o, 'trade_id', None) == trade_id and o.status == "open"]
                        cyc_attr = f"cycle_{trade_id.lower()}"
                        if not orders:
                            return {"cycle": getattr(st, cyc_attr, 1), "leg": "idle"}
                        o = orders[0]
                        return {
                            "cycle": o.cycle,
                            "leg": o.order_role,
                            "side": o.side,
                            "price": o.price,
                        }

                    upnl = grid_strategy.compute_unrealized_pnl(ai_state, ai_price)
                    ps = getattr(ai_state, "pair_stats", None)
                    market_data = {
                        "market": {
                            "price": ai_price,
                            "pair_display": ai_state.pair_display,
                            "change_1h_pct": price_changes["1h"],
                            "change_4h_pct": price_changes["4h"],
                            "change_24h_pct": price_changes["24h"],
                            "spread_pct": spread_pct,
                            "fills_last_hour": recent_count,
                        },
                        "strategy": {
                            "entry_pct": ai_state.entry_pct,
                            "profit_pct": ai_state.profit_pct,
                            "refresh_pct": ai_state.refresh_pct,
                            "order_size_usd": ai_state.order_size_usd,
                        },
                        "state": {
                            "pair_state": ai_state.pair_state,
                            "trade_a": _serialize_trade("A", ai_state),
                            "trade_b": _serialize_trade("B", ai_state),
                        },
                        "performance": ps.to_dict() if ps else {},
                        "risk": {
                            "capital": config.STARTING_CAPITAL,
                            "stop_floor": ai_state.stop_floor,
                            "daily_loss_limit": ai_state.daily_loss_limit,
                            "today_loss_usd": ai_state.today_loss_usd,
                            "today_profit_usd": ai_state.today_profit_usd,
                            "total_profit_usd": ai_state.total_profit_usd,
                            "unrealized_pnl_usd": upnl["total_unrealized"],
                        },
                    }
                    logger.debug("AI council payload: %s", json.dumps(market_data, default=str))
                else:
                    market_data = {
                        "price": ai_price,
                        "pair_display": ai_state.pair_display,
                        "change_1h": price_changes["1h"],
                        "change_4h": price_changes["4h"],
                        "change_24h": price_changes["24h"],
                        "spread_pct": spread_pct,
                        "recent_fills": recent_count,
                        "grid_center": ai_state.center_price,
                    }

                stats_context = stats_engine.format_for_ai(ai_state.stats_results)
                recommendation = ai_advisor.get_recommendation(market_data, stats_context)
                ai_state.ai_recommendation = ai_advisor.format_recommendation(recommendation)
                ai_state._last_ai_result = recommendation

                action = recommendation.get("action", "continue")
                consensus_type = recommendation.get("consensus_type")

                # Auto-execute safe (conservative) actions without approval
                AI_SAFE_ACTIONS = {"widen_entry", "widen_spacing"}
                if (config.AI_AUTO_EXECUTE
                        and action in AI_SAFE_ACTIONS
                        and consensus_type in ("majority", "condition_fallback")):
                    logger.info(
                        "AI AUTO-EXECUTE: %s (consensus=%s)",
                        action, consensus_type,
                    )
                    _execute_approved_action(ai_state, action, ai_price)
                    notifier._send_message(
                        f"AI AUTO-EXECUTE: {action}\n"
                        f"Consensus: {consensus_type}\n"
                        f"Reason: {recommendation.get('reason', '')}"
                    )
                    ai_advisor.log_approval_decision(action, "auto-executed")
                else:
                    msg_id = notifier.notify_ai_recommendation(recommendation, ai_price)
                    if action != "continue" and msg_id:
                        _set_pending_approval(action, msg_id, recommendation)

            # --- 4f2: Poll for approval callbacks & check expiry ---
            _check_approval_expiry()
            # Use first pair for Telegram callbacks (commands/menus operate on first pair)
            first_st = _first_state()
            first_price = _current_prices.get(_first_pair_name(), 0.0)
            if first_st:
                _poll_and_handle_callbacks(first_st, first_price)

            # --- 4f3: Apply web dashboard config changes ---
            if first_st:
                _apply_web_config(first_st, first_price)

            # --- 4f4: Run stats engine (OHLC throttled: max 3 per cycle) ---
            ohlc_pairs = [pn for pn in _bot_states if now - _ohlc_last_fetches.get(pn, 0) >= 300]
            ohlc_fetched = 0
            MAX_OHLC_PER_CYCLE = 3
            # Round-robin: start from where we left off
            if ohlc_pairs:
                global _ohlc_robin_idx
                for _ in range(len(ohlc_pairs)):
                    if ohlc_fetched >= MAX_OHLC_PER_CYCLE:
                        break
                    idx = _ohlc_robin_idx % len(ohlc_pairs)
                    _ohlc_robin_idx += 1
                    pn = ohlc_pairs[idx]
                    try:
                        _ohlc_caches[pn] = kraken_client.get_ohlc(pair=pn, interval=5)
                        _ohlc_last_fetches[pn] = now
                        ohlc_fetched += 1
                    except Exception as e:
                        logger.debug("[%s] OHLC fetch failed: %s", pn, e)

            for pair_name, state in _bot_states.items():
                if now - state.stats_last_run >= 60:
                    state.stats_last_run = now
                    try:
                        cp = _current_prices.get(pair_name, 0.0)
                        state.stats_results = stats_engine.run_all(
                            state, cp, _ohlc_caches.get(pair_name, [])
                        )
                        # Volatility auto-adjust profit target
                        if config.STRATEGY_MODE == "pair":
                            adjusted = grid_strategy.adjust_profit_from_volatility(
                                state, state.stats_results)
                            if adjusted:
                                notifier._send_message(
                                    f"[{state.pair_display}] Volatility auto-adjust: "
                                    f"profit target -> {state.profit_pct:.2f}%"
                                )
                                grid_strategy.save_state(state)
                    except Exception as e:
                        logger.debug("[%s] Stats engine error: %s", pair_name, e)

            # --- 4g: Check accumulation (aggregate across pairs, execute on first) ---
            if first_st:
                excess = grid_strategy.check_accumulation(first_st)
                if excess > 0:
                    doge_bought = grid_strategy.execute_accumulation(
                        first_st, excess, first_price
                    )
                    if doge_bought > 0:
                        notifier.notify_accumulation(
                            excess, doge_bought,
                            first_st.doge_accumulated, first_price,
                        )

            # --- 4h: Periodic status log + state save ---
            if int(now) % 300 < config.POLL_INTERVAL_SECONDS:
                for pair_name, state in _bot_states.items():
                    cp = _current_prices.get(pair_name, 0.0)
                    logger.info(grid_strategy.get_status_summary(state, cp))
                    grid_strategy.save_state(state)

            # --- 4h2: Process swarm add/remove/multiplier queue ---
            _process_swarm_queue()

            # --- 4i: Sleep until next poll ---
            elapsed = time.time() - loop_start
            sleep_time = max(0, config.POLL_INTERVAL_SECONDS - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt caught")
            break

        except Exception as e:
            _global_consecutive_errors += 1
            logger.error("Main loop error (%d/%d): %s",
                         _global_consecutive_errors, config.MAX_CONSECUTIVE_ERRORS, e,
                         exc_info=True)

            if _global_consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                logger.critical("Too many errors -- stopping bot")
                notifier.notify_error(f"Bot stopping: too many errors\nLast: {e}")
                break

            time.sleep(5)

    # --- Phase 5: Graceful shutdown ---
    logger.info("Shutting down...")
    _bot_healthy = False

    total_profit = 0.0
    total_trips = 0
    total_fees = 0.0
    total_doge = 0.0

    for pair_name, state in _bot_states.items():
        grid_strategy.save_state(state)
        if not _shutdown_requested:
            # Only cancel orders on error-limit shutdown (no restart expected).
            # On SIGTERM (deploy/restart), leave orders on Kraken -- the new
            # instance will re-adopt them via reconciliation.
            logger.info("[%s] Cancelling open orders (error shutdown)...", pair_name)
            cancelled = grid_strategy.cancel_grid(state)
            logger.info("[%s] Cancelled %d orders", pair_name, cancelled)
        else:
            logger.info("[%s] Signal shutdown -- leaving orders on Kraken for re-adopt",
                        pair_name)
        total_profit += state.total_profit_usd
        total_trips += state.total_round_trips
        total_fees += state.total_fees_usd
        total_doge += state.doge_accumulated

    logger.info("Final state (all pairs):")
    logger.info("  Total profit: $%.4f", total_profit)
    logger.info("  Total round trips: %d", total_trips)
    logger.info("  Total fees: $%.4f", total_fees)
    logger.info("  DOGE accumulated: %.2f", total_doge)

    shutdown_reason = "Signal received" if _shutdown_requested else "Error limit reached"
    notifier.notify_shutdown(shutdown_reason)

    if health_server:
        health_server.shutdown()

    logger.info("Bot stopped. Goodbye!")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run()
