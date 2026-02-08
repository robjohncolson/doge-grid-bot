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

# Telegram Bot API base URL template
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Track the last update_id so we only see new callbacks
_last_update_id = 0


def _telegram_api(method: str, payload: dict) -> dict:
    """
    Call any Telegram Bot API method.

    Args:
        method:  API method name (e.g. "sendMessage", "getUpdates")
        payload: Dict of parameters to send as JSON body

    Returns:
        The parsed JSON response dict, or {} on failure.

    This function NEVER raises -- failures are logged and swallowed.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        logger.debug("Telegram not configured, skipping %s", method)
        return {}

    url = TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DOGEGridBot/1.0",
    }
    req = urllib.request.Request(url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                return result
            logger.warning("Telegram %s returned ok=false: %s", method, result)
            return {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.warning("Telegram %s HTTP %d: %s", method, e.code, body[:200])
        return {}
    except Exception as e:
        logger.warning("Telegram %s failed: %s", method, e)
        return {}


def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    Send a plain message via Telegram Bot API.

    Returns True if sent successfully, False otherwise.
    This function NEVER raises -- failures are logged and swallowed.
    """
    if not config.TELEGRAM_CHAT_ID:
        logger.debug("Telegram chat ID not set, skipping notification")
        return False

    result = _telegram_api("sendMessage", {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    })
    return bool(result)


def send_with_buttons(text: str, buttons: list, parse_mode: str = "HTML") -> int:
    """
    Send a message with inline keyboard buttons.

    Args:
        text:    Message text (HTML)
        buttons: Flat list of dicts (single row) OR list-of-lists (multi-row).
                 Each dict has "text" and "callback_data" keys.

    Returns:
        The message_id (int) of the sent message, or 0 on failure.
    """
    if not config.TELEGRAM_CHAT_ID:
        return 0

    # Detect flat list vs list-of-lists
    if buttons and isinstance(buttons[0], dict):
        # Flat list -- single row (backward compatible)
        keyboard = [[{"text": b["text"], "callback_data": b["callback_data"]} for b in buttons]]
    else:
        # List-of-lists -- multi-row
        keyboard = [
            [{"text": b["text"], "callback_data": b["callback_data"]} for b in row]
            for row in buttons
        ]

    result = _telegram_api("sendMessage", {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard},
    })

    if result:
        return result.get("result", {}).get("message_id", 0)
    return 0


def poll_updates() -> tuple:
    """
    Poll Telegram for new updates (button presses and text commands).

    Uses short polling (timeout=0) so it never blocks.
    Only returns updates from the configured TELEGRAM_CHAT_ID.

    Returns:
        Tuple of (callbacks, commands):
          callbacks: [{"callback_id": str, "data": str, "message_id": int}, ...]
          commands:  [{"text": str, "message_id": int}, ...]
    """
    global _last_update_id

    params = {
        "timeout": 0,
        "allowed_updates": ["callback_query", "message"],
    }
    if _last_update_id > 0:
        params["offset"] = _last_update_id + 1

    result = _telegram_api("getUpdates", params)
    if not result:
        return [], []

    updates = result.get("result", [])
    callbacks = []
    commands = []

    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id > _last_update_id:
            _last_update_id = update_id

        # Handle callback queries (button presses)
        cb = update.get("callback_query")
        if cb:
            msg = cb.get("message", {})
            chat = msg.get("chat", {})
            chat_id = str(chat.get("id", ""))

            if chat_id != str(config.TELEGRAM_CHAT_ID):
                logger.warning("Ignoring callback from unauthorized chat %s", chat_id)
                continue

            callbacks.append({
                "callback_id": cb.get("id", ""),
                "data": cb.get("data", ""),
                "message_id": msg.get("message_id", 0),
            })
            continue

        # Handle text messages (commands)
        msg = update.get("message")
        if not msg:
            continue

        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(config.TELEGRAM_CHAT_ID):
            logger.warning("Ignoring message from unauthorized chat %s", chat_id)
            continue

        text = msg.get("text", "")
        if text.startswith("/"):
            commands.append({
                "text": text,
                "message_id": msg.get("message_id", 0),
            })

    return callbacks, commands


def answer_callback(callback_id: str, text: str = ""):
    """
    Acknowledge a callback query (removes the 'loading' spinner on the button).

    Args:
        callback_id: The callback_query id from poll_callbacks()
        text:        Optional short toast text shown to the user
    """
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    _telegram_api("answerCallbackQuery", payload)


def edit_message_text(message_id: int, text: str, parse_mode: str = "HTML",
                      reply_markup=None):
    """
    Edit an existing message (e.g. to remove buttons after action).

    Args:
        message_id:   The message to edit
        text:         New text content
        reply_markup: Optional inline keyboard dict (same format as send_with_buttons)
    """
    if not config.TELEGRAM_CHAT_ID:
        return
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _telegram_api("editMessageText", payload)


def _prefix() -> str:
    """Add [DRY RUN] prefix when in dry-run mode."""
    return "[DRY RUN] " if config.DRY_RUN else ""


# ---------------------------------------------------------------------------
# Notification methods -- one for each event type
# ---------------------------------------------------------------------------

def notify_startup(current_price: float, pair_display: str = "DOGE/USD"):
    """Send startup notification with bot configuration summary."""
    mode = "DRY RUN (simulated)" if config.DRY_RUN else "LIVE TRADING"
    text = (
        f"🤖 <b>{_prefix()}{pair_display} Grid Bot Started</b>\n\n"
        f"Mode: {mode}\n"
        f"{pair_display} Price: ${current_price:.6f}\n"
        f"Capital: ${config.STARTING_CAPITAL:.2f}\n"
        f"Grid: {config.GRID_LEVELS}×2 levels, {config.GRID_SPACING_PCT}% spacing\n"
        f"Order size: ${config.ORDER_SIZE_USD:.2f}\n"
        f"Stop floor: ${config.STOP_FLOOR:.2f}\n"
        f"Daily loss limit: ${config.DAILY_LOSS_LIMIT:.2f}"
    )
    _send_message(text)


def notify_shutdown(reason: str = "Manual", pair_display: str = ""):
    """Send shutdown notification."""
    label = f"{pair_display} " if pair_display else ""
    text = f"🛑 <b>{_prefix()}{label}Grid Bot Stopped</b>\n\nReason: {reason}"
    _send_message(text)


def notify_round_trip(side: str, price: float, volume: float,
                      profit: float, total_profit: float, trip_count: int,
                      pair_display: str = "DOGE/USD"):
    """
    Notify on a completed round trip (buy->sell or sell->buy cycle).
    This is the "cha-ching" notification -- the bot made money!
    """
    emoji = "💰" if profit > 0 else "📉"
    text = (
        f"{emoji} <b>{_prefix()}{pair_display} Round Trip</b>\n\n"
        f"Sell: {volume:.2f} @ ${price:.6f}\n"
        f"Net profit: ${profit:.4f}\n"
        f"Today total: ${total_profit:.4f}\n"
        f"Trip #{trip_count}"
    )
    _send_message(text)


def notify_grid_built(center_price: float, num_orders: int,
                      pair_display: str = "DOGE/USD"):
    """Notify when a new grid is built or rebuilt."""
    text = (
        f"📊 <b>{_prefix()}{pair_display} Grid Built</b>\n\n"
        f"Center: ${center_price:.6f}\n"
        f"Orders: {num_orders}\n"
        f"Range: ${center_price * (1 - config.GRID_SPACING_PCT * config.GRID_LEVELS / 100):.6f}"
        f" -- ${center_price * (1 + config.GRID_SPACING_PCT * config.GRID_LEVELS / 100):.6f}"
    )
    _send_message(text)


def notify_grid_reset(old_center: float, new_center: float, drift_pct: float,
                      pair_display: str = "DOGE/USD"):
    """Notify when the grid is reset due to price drift."""
    text = (
        f"🔄 <b>{_prefix()}{pair_display} Grid Reset</b>\n\n"
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


def notify_risk_event(event_type: str, details: str, pair_display: str = ""):
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
    label = f"{pair_display} " if pair_display else ""
    text = (
        f"{emoji} <b>{_prefix()}{label}Risk Alert: {event_type.upper()}</b>\n\n"
        f"{details}"
    )
    _send_message(text)


def notify_ai_recommendation(recommendation: dict, current_price: float) -> int:
    """
    Send the AI council recommendation.

    Shows each panelist's vote and the majority verdict.
    For actionable recommendations (not "continue"), sends inline
    Approve/Skip buttons and returns the message_id for tracking.
    For "continue", sends a plain message and returns 0.

    Returns:
        message_id (int) if buttons were sent, 0 otherwise.
    """
    action = recommendation.get("action", "continue")
    reason = recommendation.get("reason", "No reason given")
    panel_votes = recommendation.get("panel_votes", [])
    panel_size = recommendation.get("panel_size", 0)
    winner_count = recommendation.get("winner_count", 0)

    # Build panel votes section
    if panel_votes:
        vote_lines = []
        for v in panel_votes:
            name = v.get("name", "?")
            v_action = v.get("action", "?") or "error"
            v_condition = v.get("condition", "?")
            v_reason = v.get("reason", "")
            marker = ">" if v_action == action else " "
            vote_lines.append(
                f"{marker} <b>{name}</b>: {v_action} ({v_condition})"
            )
            if v_reason:
                vote_lines.append(f"    <i>{v_reason}</i>")
        votes_text = "\n".join(vote_lines)
        verdict = f"{action.upper()} ({winner_count}/{panel_size})"
        header = f"AI Council"
    else:
        # Legacy single-model format
        condition = recommendation.get("condition", "unknown")
        votes_text = f"Market: {condition}\nReason: {reason}"
        verdict = action.upper()
        header = f"AI Advisor"

    if action == "continue":
        text = (
            f"🧠 <b>{_prefix()}{header}</b>\n\n"
            f"Price: ${current_price:.6f}\n\n"
            f"{votes_text}\n\n"
            f"Verdict: <b>{verdict}</b>\n"
            f"<i>No action needed</i>"
        )
        _send_message(text)
        return 0

    # Actionable recommendation -- send with Approve/Skip buttons
    text = (
        f"🧠 <b>{_prefix()}{header}</b>\n\n"
        f"Price: ${current_price:.6f}\n\n"
        f"{votes_text}\n\n"
        f"Verdict: <b>{verdict}</b>\n"
        f"<i>Tap below to approve or skip:</i>"
    )
    buttons = [
        {"text": "Approve", "callback_data": f"approve:{action}"},
        {"text": "Skip", "callback_data": f"skip:{action}"},
    ]
    return send_with_buttons(text, buttons)


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
