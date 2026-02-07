"""
notifier.py -- Telegram notifications for the DOGE grid trading bot.

Sends alerts via the Telegram Bot API for:
  - Bot startup / shutdown
  - Each completed buy->sell round trip (with profit amount)
  - Daily P&L summary (midnight UTC)
  - Risk events (stop floor approach, daily loss limit, grid reset)
  - AI advisor hourly recommendation
  - Errors that need human attention

SETUP:
  1. Message @BotFather on Telegram to create a bot -> get TELEGRAM_BOT_TOKEN
  2. Message @userinfobot to find your TELEGRAM_CHAT_ID
  3. Set both as environment variables

ZERO DEPENDENCIES:
  Uses urllib.request to POST to https://api.telegram.org/bot{token}/sendMessage
"""

import json
import logging
import urllib.request
import urllib.error

import config

logger = logging.getLogger(__name__)

# Telegram Bot API endpoint template
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    Send a message via Telegram Bot API.

    Args:
        text:       Message text (can include HTML formatting)
        parse_mode: "HTML" or "Markdown" (default: HTML because it's less finicky)

    Returns:
        True if sent successfully, False otherwise.

    This function NEVER raises -- failures are logged and swallowed.
    Notifications must never block trading.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured, skipping notification")
        return False

    url = TELEGRAM_URL.format(token=config.TELEGRAM_BOT_TOKEN)

    payload = json.dumps({
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,  # Don't expand URLs in messages
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DOGEGridBot/1.0",
    }

    req = urllib.request.Request(url, data=payload, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                return True
            else:
                logger.warning("Telegram API returned ok=false: %s", result)
                return False

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.warning("Telegram HTTP %d: %s", e.code, body[:200])
        return False

    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def _prefix() -> str:
    """Add [DRY RUN] prefix when in dry-run mode."""
    return "[DRY RUN] " if config.DRY_RUN else ""


# ---------------------------------------------------------------------------
# Notification methods -- one for each event type
# ---------------------------------------------------------------------------

def notify_startup(current_price: float):
    """Send startup notification with bot configuration summary."""
    mode = "DRY RUN (simulated)" if config.DRY_RUN else "LIVE TRADING"
    text = (
        f"🤖 <b>{_prefix()}DOGE Grid Bot Started</b>\n\n"
        f"Mode: {mode}\n"
        f"DOGE Price: ${current_price:.6f}\n"
        f"Capital: ${config.STARTING_CAPITAL:.2f}\n"
        f"Grid: {config.GRID_LEVELS}×2 levels, {config.GRID_SPACING_PCT}% spacing\n"
        f"Order size: ${config.ORDER_SIZE_USD:.2f}\n"
        f"Stop floor: ${config.STOP_FLOOR:.2f}\n"
        f"Daily loss limit: ${config.DAILY_LOSS_LIMIT:.2f}"
    )
    _send_message(text)


def notify_shutdown(reason: str = "Manual"):
    """Send shutdown notification."""
    text = f"🛑 <b>{_prefix()}DOGE Grid Bot Stopped</b>\n\nReason: {reason}"
    _send_message(text)


def notify_round_trip(side: str, price: float, volume: float,
                      profit: float, total_profit: float, trip_count: int):
    """
    Notify on a completed round trip (buy->sell or sell->buy cycle).
    This is the "cha-ching" notification -- the bot made money!
    """
    emoji = "💰" if profit > 0 else "📉"
    text = (
        f"{emoji} <b>{_prefix()}Round Trip Complete</b>\n\n"
        f"Sell: {volume:.2f} DOGE @ ${price:.6f}\n"
        f"Net profit: ${profit:.4f}\n"
        f"Today total: ${total_profit:.4f}\n"
        f"Trip #{trip_count}"
    )
    _send_message(text)


def notify_grid_built(center_price: float, num_orders: int):
    """Notify when a new grid is built or rebuilt."""
    text = (
        f"📊 <b>{_prefix()}Grid Built</b>\n\n"
        f"Center: ${center_price:.6f}\n"
        f"Orders: {num_orders}\n"
        f"Range: ${center_price * (1 - config.GRID_SPACING_PCT * config.GRID_LEVELS / 100):.6f}"
        f" -- ${center_price * (1 + config.GRID_SPACING_PCT * config.GRID_LEVELS / 100):.6f}"
    )
    _send_message(text)


def notify_grid_reset(old_center: float, new_center: float, drift_pct: float):
    """Notify when the grid is reset due to price drift."""
    text = (
        f"🔄 <b>{_prefix()}Grid Reset (Drift)</b>\n\n"
        f"Old center: ${old_center:.6f}\n"
        f"New center: ${new_center:.6f}\n"
        f"Drift: {drift_pct:.2f}%"
    )
    _send_message(text)


def notify_daily_summary(date: str, trades: int, profit: float,
                         fees: float, doge_accumulated: float,
                         total_profit: float, total_trips: int):
    """Send the daily P&L summary (at midnight UTC)."""
    on_target = profit >= (config.MONTHLY_RESERVE_USD / 30)
    status = "✅ On target" if on_target else "⚠️ Below target"

    daily_target = config.MONTHLY_RESERVE_USD / 30
    text = (
        f"📅 <b>{_prefix()}Daily Summary -- {date}</b>\n\n"
        f"Round trips: {trades}\n"
        f"Net profit: ${profit:.4f}\n"
        f"Fees paid: ${fees:.4f}\n"
        f"Daily target: ${daily_target:.2f} -> {status}\n"
        f"\n<b>Lifetime</b>\n"
        f"Total profit: ${total_profit:.4f}\n"
        f"Total trips: {total_trips}\n"
        f"DOGE accumulated: {doge_accumulated:.2f}"
    )
    _send_message(text)


def notify_risk_event(event_type: str, details: str):
    """
    Send alert for risk events:
      - stop_floor: approaching or hit the stop floor
      - daily_limit: daily loss limit reached
      - error: persistent API errors
    """
    emoji_map = {
        "stop_floor": "🚨",
        "daily_limit": "⚠️",
        "error": "❌",
        "pause": "⏸️",
        "resume": "▶️",
    }
    emoji = emoji_map.get(event_type, "⚠️")
    text = (
        f"{emoji} <b>{_prefix()}Risk Alert: {event_type.upper()}</b>\n\n"
        f"{details}"
    )
    _send_message(text)


def notify_ai_recommendation(recommendation: dict, current_price: float):
    """Send the hourly AI advisor recommendation."""
    condition = recommendation.get("condition", "unknown")
    action = recommendation.get("action", "continue")
    reason = recommendation.get("reason", "No reason given")

    text = (
        f"🧠 <b>{_prefix()}AI Advisor</b>\n\n"
        f"Price: ${current_price:.6f}\n"
        f"Market: {condition}\n"
        f"Recommendation: {action}\n"
        f"Reason: {reason}\n"
        f"\n<i>Advisory only -- not auto-acting</i>"
    )
    _send_message(text)


def notify_accumulation(usd_amount: float, doge_amount: float,
                        total_doge: float, current_price: float):
    """Notify when excess profit is converted to DOGE."""
    text = (
        f"🐕 <b>{_prefix()}DOGE Accumulated!</b>\n\n"
        f"Converted: ${usd_amount:.2f} -> {doge_amount:.2f} DOGE\n"
        f"Price: ${current_price:.6f}\n"
        f"Total accumulated: {total_doge:.2f} DOGE"
    )
    _send_message(text)


def notify_error(error_msg: str):
    """Send an error notification that needs human attention."""
    text = (
        f"❌ <b>{_prefix()}Bot Error</b>\n\n"
        f"{error_msg}\n\n"
        f"<i>Check logs for details</i>"
    )
    _send_message(text)
