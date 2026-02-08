"""
telegram_menu.py -- Inline keyboard menu screens for the Telegram bot.

Each function returns (text, keyboard_rows) where:
  text:           HTML-formatted message text
  keyboard_rows:  list-of-lists for send_with_buttons() multi-row format
"""

import time
import config
import grid_strategy


def build_main_menu():
    """Main menu -- 4 buttons in a 2x2 grid + pair selector if multiple pairs."""
    pair_count = len(config.PAIRS)
    if pair_count > 1:
        pair_names = ", ".join(pc.display for pc in config.PAIRS.values())
        text = f"<b>Grid Bot Menu</b> ({pair_names})\nSelect an option:"
    else:
        text = "<b>Grid Bot Menu</b>\nSelect an option:"
    keyboard = [
        [
            {"text": "Status", "callback_data": "m:status"},
            {"text": "Grid", "callback_data": "m:grid"},
        ],
        [
            {"text": "Stats", "callback_data": "m:stats"},
            {"text": "Settings", "callback_data": "m:settings"},
        ],
    ]
    return text, keyboard


def build_status_screen(state: grid_strategy.GridState, current_price: float):
    """Bot status summary with Back button."""
    summary = grid_strategy.get_status_summary(state, current_price)
    text = f"<pre>{summary}</pre>"
    keyboard = [[{"text": "<< Back", "callback_data": "m:main"}]]
    return text, keyboard


def build_grid_screen(state: grid_strategy.GridState, current_price: float):
    """Compact grid info with Back button."""
    open_orders = [o for o in state.grid_orders if o.status == "open"]
    open_buys = [o for o in open_orders if o.side == "buy"]
    open_sells = [o for o in open_orders if o.side == "sell"]

    # Price range
    buy_prices = [o.price for o in open_buys]
    sell_prices = [o.price for o in open_sells]
    low = min(buy_prices) if buy_prices else 0
    high = max(sell_prices) if sell_prices else 0

    drift_pct = 0.0
    if state.center_price > 0:
        drift_pct = (current_price - state.center_price) / state.center_price * 100

    if config.STRATEGY_MODE == "pair":
        entry_orders = [o for o in open_orders if o.order_role == "entry"]
        exit_orders = [o for o in open_orders if o.order_role == "exit"]
        text = (
            f"<b>Pair Info</b>\n\n"
            f"Center: ${state.center_price:.6f}\n"
            f"Price: ${current_price:.6f} (drift {drift_pct:+.2f}%)\n"
            f"Orders: {len(open_buys)}B + {len(open_sells)}S ({len(entry_orders)} entry, {len(exit_orders)} exit)\n"
            f"Range: ${low:.6f} -- ${high:.6f}\n"
            f"Entry: {state.entry_pct:.2f}% | Profit: {state.profit_pct:.2f}%\n"
            f"Order size: ${state.order_size_usd:.2f}"
        )
    else:
        text = (
            f"<b>Grid Info</b>\n\n"
            f"Center: ${state.center_price:.6f}\n"
            f"Price: ${current_price:.6f} (drift {drift_pct:+.2f}%)\n"
            f"Orders: {len(open_buys)}B + {len(open_sells)}S = {len(open_orders)}\n"
            f"Range: ${low:.6f} -- ${high:.6f}\n"
            f"Spacing: {config.GRID_SPACING_PCT:.2f}%\n"
            f"Order size: ${config.ORDER_SIZE_USD:.2f}"
        )
    keyboard = [[{"text": "<< Back", "callback_data": "m:main"}]]
    return text, keyboard


def build_stats_screen(state: grid_strategy.GridState):
    """Stats engine verdicts with Back button."""
    results = state.stats_results
    if not results:
        text = "<b>Statistical Advisory Board</b>\n\nNo data yet. Wait ~60s for first analysis."
        keyboard = [[{"text": "<< Back", "callback_data": "m:main"}]]
        return text, keyboard

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

    text = "\n".join(lines)
    keyboard = [[{"text": "<< Back", "callback_data": "m:main"}]]
    return text, keyboard


def build_settings_screen(state: grid_strategy.GridState = None):
    """Current params + action buttons + Back."""
    floor = config.ROUND_TRIP_FEE_PCT + 0.1

    if config.STRATEGY_MODE == "pair":
        entry_pct = state.entry_pct if state else config.PAIR_ENTRY_PCT
        profit_pct = state.profit_pct if state else config.PAIR_PROFIT_PCT
        refresh_pct = state.refresh_pct if state else config.PAIR_REFRESH_PCT
        order_size = state.order_size_usd if state else config.ORDER_SIZE_USD
        text = (
            f"<b>Settings</b> [pair mode]\n\n"
            f"Profit target: {profit_pct:.2f}% (min {floor:.2f}%)\n"
            f"Entry distance: {entry_pct:.2f}% (min 0.05%)\n"
            f"Refresh drift: {refresh_pct:.2f}%\n"
            f"Order size: ${order_size:.2f}\n"
            f"AI interval: {config.AI_ADVISOR_INTERVAL}s ({config.AI_ADVISOR_INTERVAL // 60} min)\n"
            f"Mode: {'DRY RUN' if config.DRY_RUN else 'LIVE'}"
        )
        keyboard = [
            [
                {"text": "Profit +", "callback_data": "ma:spacing_up"},
                {"text": "Profit -", "callback_data": "ma:spacing_down"},
            ],
            [
                {"text": "Entry +", "callback_data": "ma:entry_up"},
                {"text": "Entry -", "callback_data": "ma:entry_down"},
            ],
            [
                {"text": "AI Check Now", "callback_data": "ma:ai_check"},
            ],
            [{"text": "<< Back", "callback_data": "m:main"}],
        ]
    else:
        text = (
            f"<b>Settings</b> [grid mode]\n\n"
            f"Spacing: {config.GRID_SPACING_PCT:.2f}% (min {floor:.2f}%)\n"
            f"Grid levels: {config.GRID_LEVELS} per side\n"
            f"Order size: ${config.ORDER_SIZE_USD:.2f}\n"
            f"AI interval: {config.AI_ADVISOR_INTERVAL}s ({config.AI_ADVISOR_INTERVAL // 60} min)\n"
            f"Mode: {'DRY RUN' if config.DRY_RUN else 'LIVE'}"
        )
        keyboard = [
            [
                {"text": "Spacing +", "callback_data": "ma:spacing_up"},
                {"text": "Spacing -", "callback_data": "ma:spacing_down"},
            ],
            [
                {"text": "Ratio Auto", "callback_data": "ma:ratio_auto"},
                {"text": "AI Check Now", "callback_data": "ma:ai_check"},
            ],
            [{"text": "<< Back", "callback_data": "m:main"}],
        ]
    return text, keyboard
