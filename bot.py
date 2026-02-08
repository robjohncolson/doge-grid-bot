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
import stats_engine
import telegram_menu
import supabase_store

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
_bot_state: grid_strategy.GridState = None
_bot_healthy = True
_bot_start_time = 0.0

# Pending AI approval -- only one at a time
# Keys: action (str), message_id (int), created_at (float), recommendation (dict)
_pending_approval: dict = None

# How long (seconds) before a pending approval expires
APPROVAL_TIMEOUT = 600  # 10 minutes

# Fixed step for spacing adjustments -- prevents extreme values
SPACING_STEP_PCT = 0.25

# Pending web config changes -- written by HTTP thread, read by main loop
_web_config_pending: dict = {}

# OHLC candle cache for stats engine (fetched every 5 min)
_ohlc_cache: list = []
_ohlc_last_fetch: float = 0.0


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
        if self.path == "/":
            self._send_html(dashboard.DASHBOARD_HTML)
        elif self.path == "/api/status":
            self._handle_api_status()
        elif self.path == "/api/stats":
            self._handle_api_stats()
        elif self.path == "/api/stream":
            self._handle_sse()
        elif self.path.startswith("/api/export/"):
            self._handle_export()
        elif self.path == "/health":
            self._handle_health()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        """Route POST requests."""
        if self.path == "/api/config":
            self._handle_api_config()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_api_status(self):
        """GET /api/status -- full state snapshot for the dashboard."""
        if not _bot_state:
            self._send_json({"error": "bot not initialized"}, 503)
            return

        # Get current price from price history (last recorded)
        if _bot_state.price_history:
            current_price = _bot_state.price_history[-1][1]
        else:
            current_price = _bot_state.center_price

        data = dashboard.serialize_state(_bot_state, current_price)
        self._send_json(data)

    def _handle_api_stats(self):
        """GET /api/stats -- stats engine results only."""
        if not _bot_state:
            self._send_json({"error": "bot not initialized"}, 503)
            return
        self._send_json(_bot_state.stats_results or {})

    def _handle_health(self):
        """GET /health -- legacy Railway health check (backwards compatible)."""
        status = {
            "status": "healthy" if _bot_healthy else "unhealthy",
            "mode": "dry_run" if config.DRY_RUN else "live",
            "uptime_seconds": int(time.time() - _bot_start_time) if _bot_start_time else 0,
            "pair": config.PAIR_DISPLAY,
        }
        if _bot_state:
            status.update({
                "center_price": _bot_state.center_price,
                "total_profit": round(_bot_state.total_profit_usd, 4),
                "total_round_trips": _bot_state.total_round_trips,
                "today_round_trips": _bot_state.round_trips_today,
                "open_orders": len([o for o in _bot_state.grid_orders if o.status == "open"]),
                "is_paused": _bot_state.is_paused,
                "doge_accumulated": round(_bot_state.doge_accumulated, 2),
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
                if not _bot_state:
                    data = json.dumps({"error": "bot not initialized"})
                else:
                    if _bot_state.price_history:
                        current_price = _bot_state.price_history[-1][1]
                    else:
                        current_price = _bot_state.center_price
                    data = json.dumps(dashboard.serialize_state(_bot_state, current_price))

                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(3)
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
        if not _bot_state:
            self._send_json({"error": "bot not initialized"}, 503)
            return

        fills = []
        for f in _bot_state.recent_fills:
            fills.append({
                "time": datetime.fromtimestamp(f.get("time", 0), tz=timezone.utc).isoformat() if f.get("time") else "",
                "side": f["side"],
                "price": round(f["price"], 6),
                "volume": round(f["volume"], 2),
                "profit": round(f.get("profit", 0), 4),
                "fees": round(f.get("fees", 0), 4),
            })

        if fmt == "csv":
            self._send_csv_data(fills, ["time", "side", "price", "volume", "profit", "fees"], "fills.csv")
        else:
            self._send_json(fills)

    def _export_stats(self, fmt):
        """Export stats results as CSV or JSON."""
        if not _bot_state:
            self._send_json({"error": "bot not initialized"}, 503)
            return

        results = _bot_state.stats_results or {}
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

        # Validate interval
        if "interval" in body:
            try:
                val = int(body["interval"])
                if val < 60 or val > 86400:
                    errors.append("interval must be between 60 and 86400")
                else:
                    pending["interval"] = val
                    queued.append("interval")
            except (ValueError, TypeError):
                errors.append("interval must be an integer")

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
            old = config.PAIR_PROFIT_PCT
            config.PAIR_PROFIT_PCT = pending["spacing"]
            logger.info("Web dashboard: profit target %.2f%% -> %.2f%%", old, config.PAIR_PROFIT_PCT)
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

    # Apply AI interval (no rebuild needed)
    if "interval" in pending:
        old = config.AI_ADVISOR_INTERVAL
        config.AI_ADVISOR_INTERVAL = pending["interval"]
        logger.info("Web dashboard: AI interval %ds -> %ds", old, config.AI_ADVISOR_INTERVAL)

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
    if action == "widen_spacing":
        if config.STRATEGY_MODE == "pair":
            old = config.PAIR_PROFIT_PCT
            config.PAIR_PROFIT_PCT = round(old + SPACING_STEP_PCT, 4)
            logger.info("Profit target widened: %.2f%% -> %.2f%%", old, config.PAIR_PROFIT_PCT)
            grid_strategy.cancel_grid(state)
            orders = grid_strategy.build_grid(state, current_price)
            notifier._send_message(
                f"Profit target widened: {old:.2f}% -> {config.PAIR_PROFIT_PCT:.2f}%\n"
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
            old = config.PAIR_PROFIT_PCT
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
            config.PAIR_PROFIT_PCT = new_val
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
        elif command == "/help":
            _cmd_help()
        else:
            notifier._send_message(f"Unknown command: <code>{command}</code>\nSend /help for available commands.")


def _cmd_interval(arg: str):
    """Handle /interval <seconds> -- change AI advisor check interval."""
    try:
        seconds = int(arg)
    except (ValueError, TypeError):
        notifier._send_message("Usage: /interval &lt;seconds&gt;\nExample: /interval 600")
        return

    if seconds < 60 or seconds > 86400:
        notifier._send_message("Interval must be between 60 and 86400 seconds.")
        return

    config.AI_ADVISOR_INTERVAL = seconds
    minutes = seconds / 60
    if minutes == int(minutes):
        label = f"{int(minutes)} min"
    else:
        label = f"{minutes:.1f} min"
    notifier._send_message(f"AI interval set to {seconds}s ({label})")
    logger.info("AI_ADVISOR_INTERVAL changed to %ds via Telegram", seconds)


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
        text, keyboard = telegram_menu.build_settings_screen()
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
            old = config.PAIR_PROFIT_PCT
            config.PAIR_PROFIT_PCT = round(old + SPACING_STEP_PCT, 4)
            notifier.answer_callback(callback_id, f"Profit: {old:.2f}% -> {config.PAIR_PROFIT_PCT:.2f}%")
            logger.info("Menu: profit target widened %.2f%% -> %.2f%%", old, config.PAIR_PROFIT_PCT)
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
            old = config.PAIR_PROFIT_PCT
            new_val = round(old - SPACING_STEP_PCT, 4)
            if new_val < floor:
                notifier.answer_callback(callback_id, f"Can't go below {floor:.2f}%")
                return
            config.PAIR_PROFIT_PCT = new_val
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

    elif action == "ai_check":
        state.last_ai_check = 0
        notifier.answer_callback(callback_id, "AI check queued")
        logger.info("Menu: immediate AI check requested")

    else:
        notifier.answer_callback(callback_id, "Unknown action")
        return

    # Refresh settings screen
    text, keyboard = telegram_menu.build_settings_screen()
    reply_markup = {"inline_keyboard": [
        [{"text": b["text"], "callback_data": b["callback_data"]} for b in row]
        for row in keyboard
    ]}
    notifier.edit_message_text(message_id, text, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Main bot logic
# ---------------------------------------------------------------------------

def run():
    """
    Main bot entry point.  Runs the complete lifecycle:
    init -> build grid -> loop -> shutdown.
    """
    global _bot_state, _bot_healthy, _bot_start_time, _ohlc_cache, _ohlc_last_fetch

    # --- Phase 1: Initialize ---
    setup_logging()
    setup_signal_handlers()
    config.print_banner()

    _bot_start_time = time.time()
    state = grid_strategy.GridState()
    _bot_state = state

    # Restore counters from previous session (if any)
    # Try local state file first, then fall back to Supabase cloud state
    local_loaded = grid_strategy.load_state(state)

    # Start Supabase persistence (loads fills/prices/state if available)
    supabase_store.start_writer_thread()

    if not local_loaded:
        sb_state = supabase_store.load_state()
        if sb_state:
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
            state.trend_ratio = sb_state.get("trend_ratio", 0.5)
            state.trend_ratio_override = sb_state.get("trend_ratio_override", None)
            # Restore runtime config overrides (spacing, AI interval)
            saved_spacing = sb_state.get("grid_spacing_pct")
            if saved_spacing and saved_spacing != config.GRID_SPACING_PCT:
                logger.info(
                    "Restoring spacing from Supabase: %.2f%% -> %.2f%%",
                    config.GRID_SPACING_PCT, saved_spacing,
                )
                config.GRID_SPACING_PCT = saved_spacing
            saved_ai_interval = sb_state.get("ai_advisor_interval")
            if saved_ai_interval and saved_ai_interval != config.AI_ADVISOR_INTERVAL:
                logger.info(
                    "Restoring AI interval from Supabase: %ds -> %ds",
                    config.AI_ADVISOR_INTERVAL, saved_ai_interval,
                )
                config.AI_ADVISOR_INTERVAL = saved_ai_interval
            logger.info(
                "State restored from Supabase: $%.4f profit, %d round trips",
                state.total_profit_usd, state.total_round_trips,
            )

    sb_fills = supabase_store.load_fills()
    if sb_fills:
        state.recent_fills = sb_fills
        logger.info("Restored %d fills from Supabase", len(sb_fills))
    sb_prices = supabase_store.load_price_history(since=time.time() - 86400)
    if sb_prices:
        state.price_history = sb_prices
        logger.info("Restored %d price samples from Supabase", len(sb_prices))

    # Start health check server (Railway expects a listening port)
    health_server = start_health_server()

    # --- Phase 2: Fetch initial price ---
    logger.info("Fetching current DOGE price...")
    try:
        current_price = kraken_client.get_price()
        bid, ask, spread = kraken_client.get_spread()
        logger.info(
            "DOGE price: $%.6f (bid=$%.6f, ask=$%.6f, spread=%.3f%%)",
            current_price, bid, ask, spread,
        )
    except Exception as e:
        logger.error("Failed to fetch initial price: %s", e)
        logger.error("Cannot start bot without a price. Check your network / Kraken status.")
        notifier.notify_error(f"Bot failed to start: cannot fetch DOGE price\n{e}")
        return

    # Record initial price
    grid_strategy.record_price(state, current_price)

    # --- Phase 2b: Validation guardrails ---
    if not grid_strategy.validate_config(current_price):
        logger.critical("Pre-flight validation FAILED -- refusing to start")
        notifier.notify_error("Bot refused to start: validation failed (check logs)")
        return

    # Notify startup
    notifier.notify_startup(current_price)

    # --- Phase 2c: Startup reconciliation ---
    adopted = grid_strategy.reconcile_on_startup(state, current_price)
    if adopted > 0:
        logger.info("Reconciliation adopted %d orders from previous session", adopted)

    # --- Phase 3: Build initial grid ---
    logger.info("Building initial grid...")
    orders = grid_strategy.build_grid(state, current_price)
    logger.info("Grid built: %d orders placed", len(orders))
    notifier.notify_grid_built(current_price, len(orders))

    # Initialize daily tracking
    grid_strategy.check_daily_reset(state)

    # --- Phase 4: Main loop ---
    logger.info("Entering main loop (poll every %ds)...", config.POLL_INTERVAL_SECONDS)
    last_daily_summary_date = ""

    while not _shutdown_requested:
        try:
            loop_start = time.time()

            # --- 4a: Fetch current price ---
            try:
                current_price = kraken_client.get_price()
                grid_strategy.record_price(state, current_price)
                supabase_store.queue_price_point(time.time(), current_price)
                state.consecutive_errors = 0
            except Exception as e:
                state.consecutive_errors += 1
                logger.error(
                    "Price fetch failed (%d/%d): %s",
                    state.consecutive_errors, config.MAX_CONSECUTIVE_ERRORS, e,
                )
                if state.consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                    logger.critical("Too many consecutive errors -- stopping bot")
                    notifier.notify_risk_event("error", f"Max consecutive errors reached: {e}")
                    break
                time.sleep(config.POLL_INTERVAL_SECONDS)
                continue

            # --- 4b: Check daily reset ---
            # Capture yesterday's values BEFORE reset zeroes them
            old_date = state.today_date
            old_profit = state.today_profit_usd
            old_trips = state.round_trips_today
            old_loss = state.today_loss_usd
            old_fees = state.today_fees_usd
            grid_strategy.check_daily_reset(state)

            # Send daily summary at date boundary (using captured values)
            if old_date and old_date != state.today_date and old_date != last_daily_summary_date:
                last_daily_summary_date = old_date
                notifier.notify_daily_summary(
                    date=old_date,
                    trades=old_trips,
                    profit=old_profit,
                    fees=old_fees,
                    doge_accumulated=state.doge_accumulated,
                    total_profit=state.total_profit_usd,
                    total_trips=state.total_round_trips,
                )

            # --- 4c: Check risk limits ---
            should_stop, should_pause, reason = grid_strategy.check_risk_limits(
                state, current_price
            )

            if should_stop:
                logger.critical("STOP triggered: %s", reason)
                notifier.notify_risk_event("stop_floor", reason)
                break

            if should_pause and not state.is_paused:
                state.is_paused = True
                state.pause_reason = reason
                grid_strategy.cancel_grid(state)
                grid_strategy.save_state(state)
                logger.warning("PAUSED: %s -- cancelled open orders", reason)
                notifier.notify_risk_event("daily_limit", reason)

            if state.is_paused:
                # Don't trade, just wait for the day to reset
                logger.debug("Bot paused: %s -- waiting for daily reset", state.pause_reason)
                time.sleep(config.POLL_INTERVAL_SECONDS)
                continue

            # --- 4d: Check grid drift ---
            if grid_strategy.check_grid_drift(state, current_price):
                old_center = state.center_price
                drift_pct = abs(current_price - old_center) / old_center * 100.0

                logger.info("Resetting grid: drift %.2f%% from $%.6f to $%.6f",
                            drift_pct, old_center, current_price)

                grid_strategy.cancel_grid(state)
                orders = grid_strategy.build_grid(state, current_price)

                notifier.notify_grid_reset(old_center, current_price, drift_pct)
                notifier.notify_grid_built(current_price, len(orders))

            # --- 4e: Check for fills ---
            filled = grid_strategy.check_fills(state, current_price)

            if filled:
                logger.info("Detected %d fill(s)", len(filled))
                new_orders = grid_strategy.handle_fills(state, filled, current_price)

                # Update trend ratio based on fill history (grid mode only)
                if config.STRATEGY_MODE != "pair":
                    grid_strategy.update_trend_ratio(state)

                # Ratio-drift rebuild: if ratio shifted enough, rebuild grid (grid mode only)
                if config.STRATEGY_MODE != "pair" and abs(state.trend_ratio - state.last_build_ratio) >= 0.2:
                    logger.info(
                        "Trend ratio drift: %.2f -> %.2f -- rebuilding grid",
                        state.last_build_ratio, state.trend_ratio,
                    )
                    grid_strategy.cancel_grid(state)
                    orders = grid_strategy.build_grid(state, current_price)
                    notifier.notify_grid_built(current_price, len(orders))

                # Send notifications for completed round trips
                for fill_data in state.recent_fills[-len(filled):]:
                    if fill_data.get("profit", 0) != 0:
                        notifier.notify_round_trip(
                            side=fill_data["side"],
                            price=fill_data["price"],
                            volume=fill_data["volume"],
                            profit=fill_data["profit"],
                            total_profit=state.today_profit_usd,
                            trip_count=state.total_round_trips,
                        )

                # Prune completed orders and save state after each fill batch
                grid_strategy.prune_completed_orders(state)
                grid_strategy.save_state(state)

            # --- 4f: Run AI advisor (hourly) ---
            now = time.time()
            if now - state.last_ai_check >= config.AI_ADVISOR_INTERVAL:
                state.last_ai_check = now

                # Gather market context
                price_changes = grid_strategy.get_price_changes(state, current_price)
                _, _, spread_pct = kraken_client.get_spread()

                # Count recent fills (last hour)
                hour_ago = now - 3600
                recent_count = len([
                    f for f in state.recent_fills
                    if f.get("time", 0) > hour_ago
                ])

                market_data = {
                    "price": current_price,
                    "change_1h": price_changes["1h"],
                    "change_4h": price_changes["4h"],
                    "change_24h": price_changes["24h"],
                    "spread_pct": spread_pct,
                    "recent_fills": recent_count,
                    "grid_center": state.center_price,
                }

                stats_context = stats_engine.format_for_ai(state.stats_results)
                recommendation = ai_advisor.get_recommendation(market_data, stats_context)
                state.ai_recommendation = ai_advisor.format_recommendation(recommendation)

                # Send via Telegram (with buttons for actionable recommendations)
                msg_id = notifier.notify_ai_recommendation(recommendation, current_price)

                # If actionable, set up pending approval
                action = recommendation.get("action", "continue")
                if action != "continue" and msg_id:
                    _set_pending_approval(action, msg_id, recommendation)

            # --- 4f2: Poll for approval callbacks & check expiry ---
            _check_approval_expiry()
            _poll_and_handle_callbacks(state, current_price)

            # --- 4f3: Apply web dashboard config changes ---
            _apply_web_config(state, current_price)

            # --- 4f4: Run stats engine (every 60s) ---
            if now - state.stats_last_run >= 60:
                state.stats_last_run = now
                # Refresh OHLC cache every 5 minutes
                if now - _ohlc_last_fetch >= 300:
                    try:
                        _ohlc_cache = kraken_client.get_ohlc(interval=5)
                        _ohlc_last_fetch = now
                    except Exception as e:
                        logger.debug("OHLC fetch failed: %s", e)
                try:
                    state.stats_results = stats_engine.run_all(
                        state, current_price, _ohlc_cache
                    )
                except Exception as e:
                    logger.debug("Stats engine error: %s", e)

            # --- 4g: Check DOGE accumulation ---
            excess = grid_strategy.check_accumulation(state)
            if excess > 0:
                doge_bought = grid_strategy.execute_accumulation(
                    state, excess, current_price
                )
                if doge_bought > 0:
                    notifier.notify_accumulation(
                        excess, doge_bought,
                        state.doge_accumulated, current_price,
                    )

            # --- 4h: Periodic status log + state save ---
            if int(now) % 300 < config.POLL_INTERVAL_SECONDS:
                # Log status roughly every 5 minutes
                logger.info(grid_strategy.get_status_summary(state, current_price))
                grid_strategy.save_state(state)

            # --- 4i: Sleep until next poll ---
            elapsed = time.time() - loop_start
            sleep_time = max(0, config.POLL_INTERVAL_SECONDS - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt caught")
            break

        except Exception as e:
            # Catch-all for unexpected errors in the main loop
            state.consecutive_errors += 1
            logger.error("Main loop error (%d/%d): %s",
                         state.consecutive_errors, config.MAX_CONSECUTIVE_ERRORS, e,
                         exc_info=True)

            if state.consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                logger.critical("Too many errors -- stopping bot")
                notifier.notify_error(f"Bot stopping: too many errors\nLast: {e}")
                break

            # Brief sleep before retry
            time.sleep(5)

    # --- Phase 5: Graceful shutdown ---
    logger.info("Shutting down...")
    _bot_healthy = False

    # Save state before cancelling (preserves open txids for next startup)
    grid_strategy.save_state(state)

    # CRITICAL: Cancel all open orders before exiting
    logger.info("Cancelling all open orders...")
    cancelled = grid_strategy.cancel_grid(state)
    logger.info("Cancelled %d orders", cancelled)

    # Log final state
    logger.info("Final state:")
    logger.info("  Total profit: $%.4f", state.total_profit_usd)
    logger.info("  Total round trips: %d", state.total_round_trips)
    logger.info("  Total fees: $%.4f", state.total_fees_usd)
    logger.info("  DOGE accumulated: %.2f", state.doge_accumulated)

    # Send shutdown notification
    shutdown_reason = "Signal received" if _shutdown_requested else "Error limit reached"
    notifier.notify_shutdown(shutdown_reason)

    # Stop health server
    if health_server:
        health_server.shutdown()

    logger.info("Bot stopped. Goodbye!")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run()
