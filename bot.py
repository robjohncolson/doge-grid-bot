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
from datetime import datetime, timezone

import config
import kraken_client
import grid_strategy
import ai_advisor
import notifier

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


class HealthHandler(BaseHTTPRequestHandler):
    """
    Minimal HTTP handler that returns bot status as JSON.
    Railway pings this to verify the process is alive.
    """

    def do_GET(self):
        """Handle GET requests -- return bot health status."""
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

        body = json.dumps(status, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
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
        server = HTTPServer(("0.0.0.0", config.HEALTH_PORT), HealthHandler)
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
# Main bot logic
# ---------------------------------------------------------------------------

def run():
    """
    Main bot entry point.  Runs the complete lifecycle:
    init -> build grid -> loop -> shutdown.
    """
    global _bot_state, _bot_healthy, _bot_start_time

    # --- Phase 1: Initialize ---
    setup_logging()
    setup_signal_handlers()
    config.print_banner()

    _bot_start_time = time.time()
    state = grid_strategy.GridState()
    _bot_state = state

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

    # Notify startup
    notifier.notify_startup(current_price)

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
            old_date = state.today_date
            grid_strategy.check_daily_reset(state)

            # Send daily summary at date boundary
            if old_date and old_date != state.today_date and old_date != last_daily_summary_date:
                last_daily_summary_date = old_date
                notifier.notify_daily_summary(
                    date=old_date,
                    trades=state.round_trips_today,
                    profit=state.today_profit_usd,
                    fees=state.total_fees_usd,
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
                logger.warning("PAUSED: %s", reason)
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
                new_orders = grid_strategy.handle_fills(state, filled)

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

                recommendation = ai_advisor.get_recommendation(market_data)
                state.ai_recommendation = ai_advisor.format_recommendation(recommendation)

                # Notify via Telegram
                notifier.notify_ai_recommendation(recommendation, current_price)

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

            # --- 4h: Periodic status log ---
            if int(now) % 300 < config.POLL_INTERVAL_SECONDS:
                # Log status roughly every 5 minutes
                logger.info(grid_strategy.get_status_summary(state, current_price))

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
