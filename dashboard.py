"""
dashboard.py -- Web dashboard for the DOGE grid trading bot.

Serves a single-page dark-theme dashboard via the existing health server.
No external dependencies -- the HTML/CSS/JS is a Python string constant.

Three public symbols:
  DASHBOARD_HTML   -- the full HTML page (served on GET /)
  serialize_state  -- converts GridState + price into a JSON-ready dict
  (stats_engine)   -- imported lazily to avoid circular imports
"""

import time
import config
import grid_strategy
import stats_engine


# ---------------------------------------------------------------------------
# State serializer
# ---------------------------------------------------------------------------

def serialize_state(state: grid_strategy.GridState, current_price: float) -> dict:
    """
    Build a complete snapshot of bot state for the dashboard.
    Called on every GET /api/status request.
    """
    now = time.time()

    # -- Price info --
    drift_pct = 0.0
    if state.center_price > 0:
        drift_pct = (current_price - state.center_price) / state.center_price * 100.0

    # -- Grid orders --
    orders = []
    for o in state.grid_orders:
        order_data = {
            "level": o.level,
            "side": o.side,
            "price": round(o.price, 6),
            "volume": round(o.volume, 2),
            "status": o.status,
        }
        if config.STRATEGY_MODE == "pair":
            order_data["role"] = getattr(o, "order_role", "")
            order_data["trade_id"] = getattr(o, "trade_id", None)
            order_data["cycle"] = getattr(o, "cycle", 0)
            order_data["matched_buy_price"] = getattr(o, "matched_buy_price", None)
            order_data["matched_sell_price"] = getattr(o, "matched_sell_price", None)
            order_data["entry_filled_at"] = getattr(o, "entry_filled_at", 0.0)
        orders.append(order_data)
    orders.sort(key=lambda x: x["price"], reverse=True)

    # -- Trend ratio --
    cutoff = now - grid_strategy.TREND_WINDOW_SECONDS
    buy_12h = sum(1 for f in state.recent_fills
                  if f.get("time", 0) > cutoff and f["side"] == "buy")
    sell_12h = sum(1 for f in state.recent_fills
                   if f.get("time", 0) > cutoff and f["side"] == "sell")
    is_pair_mode = config.STRATEGY_MODE == "pair"
    if is_pair_mode:
        n_buys = 1
        n_sells = 1
    else:
        total_grid = config.GRID_LEVELS * 2
        n_buys = max(2, min(total_grid - 2, round(total_grid * state.trend_ratio)))
        n_sells = total_grid - n_buys
    ratio_source = "manual" if state.trend_ratio_override is not None else "auto"

    # -- Effective capital --
    effective_capital = config.STARTING_CAPITAL + max(0, state.total_profit_usd)

    # -- Recent fills (last 50, newest first) --
    recent = []
    for f in state.recent_fills[-50:]:
        recent.append({
            "time": f.get("time", 0),
            "side": f["side"],
            "price": round(f["price"], 6),
            "volume": round(f["volume"], 2),
            "profit": round(f.get("profit", 0), 4),
            "fees": round(f.get("fees", 0), 4),
            "trade_id": f.get("trade_id"),
            "cycle": f.get("cycle"),
            "order_role": f.get("order_role"),
        })
    recent.reverse()

    # -- Uptime --
    from bot import _bot_start_time
    uptime = int(now - _bot_start_time) if _bot_start_time else 0

    # -- Chart data: price sparkline (downsample to ~200 points) --
    price_chart = []
    if state.price_history:
        step = max(1, len(state.price_history) // 200)
        price_chart = [
            [round(t, 1), round(p, 6)]
            for i, (t, p) in enumerate(state.price_history) if i % step == 0
        ]

    # -- Chart data: fill rate per hour (last 24h) --
    fill_rate_chart = [0] * 24
    for f in state.recent_fills:
        ft = f.get("time", 0)
        if ft > 0:
            hours_ago = (now - ft) / 3600
            if 0 <= hours_ago < 24:
                bucket = 23 - int(hours_ago)
                if 0 <= bucket < 24:
                    fill_rate_chart[bucket] += 1

    # -- Chart data: profit scatter --
    profit_chart = [
        [round(f.get("time", 0), 1), round(f.get("profit", 0), 4)]
        for f in state.recent_fills if f.get("profit", 0) != 0
    ]

    result = {
        "server_time": round(now, 1),
        "price": {
            "current": round(current_price, 6),
            "center": round(state.center_price, 6),
            "drift_pct": round(drift_pct, 2),
        },
        "profit": {
            "today": round(state.today_profit_usd, 4),
            "total": round(state.total_profit_usd, 4),
            "fees": round(state.total_fees_usd, 4),
            "round_trips": state.total_round_trips,
            "round_trips_today": state.round_trips_today,
            "doge_accumulated": round(state.doge_accumulated, 2),
        },
        "grid": orders,
        "trend": None if is_pair_mode else {
            "ratio": round(state.trend_ratio, 2),
            "source": ratio_source,
            "buy_pct": round(state.trend_ratio * 100),
            "sell_pct": round((1 - state.trend_ratio) * 100),
            "buy_12h": buy_12h,
            "sell_12h": sell_12h,
            "grid_buys": n_buys,
            "grid_sells": n_sells,
        },
        "config": {
            "strategy_mode": config.STRATEGY_MODE,
            "order_size": state.order_size_usd,
            "grid_levels": config.GRID_LEVELS,
            "spacing_pct": state.profit_pct if is_pair_mode else config.GRID_SPACING_PCT,
            "effective_capital": round(effective_capital, 2),
            "starting_capital": config.STARTING_CAPITAL,
            "ai_interval": config.AI_ADVISOR_INTERVAL,
            "round_trip_fee_pct": config.ROUND_TRIP_FEE_PCT,
            "min_spacing": round(config.ROUND_TRIP_FEE_PCT + 0.1, 2),
            "pair_entry_pct": state.entry_pct,
            "pair_profit_pct": state.profit_pct,
            "pair_refresh_pct": state.refresh_pct,
            "pair_name": state.pair_name,
            "pair_display": state.pair_display,
        },
        "recent_fills": recent,
        "ai_recommendation": state.ai_recommendation or "No recommendation yet",
        "uptime": uptime,
        "mode": "dry_run" if config.DRY_RUN else "live",
        "paused": state.is_paused,
        "pause_reason": state.pause_reason,
        # Pair mode state machine
        "pair_state": getattr(state, "pair_state", "S0"),
        "long_only": getattr(state, "long_only", False),
        "cycle_a": getattr(state, "cycle_a", 1),
        "cycle_b": getattr(state, "cycle_b", 1),
        # Completed cycles (most recent 50 for dashboard)
        "completed_cycles": [
            c.to_dict() for c in getattr(state, "completed_cycles", [])[-50:]
        ],
        # Unrealized P&L (pair mode)
        "unrealized_pnl": grid_strategy.compute_unrealized_pnl(state, current_price)
            if is_pair_mode else None,
        # AI council result
        "ai_council": getattr(state, "_last_ai_result", None),
        # Pair stats
        "pair_stats": state.pair_stats.to_dict()
            if is_pair_mode and getattr(state, "pair_stats", None) else None,
        # New: chart data
        "charts": {
            "price": price_chart,
            "fill_rate": fill_rate_chart,
            "profits": profit_chart,
        },
        # New: stats results
        "stats": state.stats_results if state.stats_results else {},
        # Recovery orders
        "recovery_orders": [
            {
                "txid": r.txid,
                "side": r.side,
                "price": round(r.price, 6),
                "volume": round(r.volume, 2),
                "trade_id": r.trade_id,
                "cycle": r.cycle,
                "entry_price": round(r.entry_price, 6),
                "age_minutes": round((time.time() - r.orphaned_at) / 60, 1)
                    if r.orphaned_at > 0 else 0,
                "unrealized_pnl": round(r.unrealized_pnl(current_price), 4),
            }
            for r in getattr(state, "recovery_orders", [])
        ] if is_pair_mode else [],
        "recovery_stats": {
            "active_count": len(getattr(state, "recovery_orders", [])),
            "max_slots": config.MAX_RECOVERY_SLOTS,
            "total_losses": getattr(state, "total_recovery_losses", 0),
            "total_wins": round(getattr(state, "total_recovery_wins", 0.0), 4),
        } if is_pair_mode else None,
        # Exit lifecycle (Section 12)
        "detected_trend": getattr(state, "detected_trend", None),
        "s2_entered_at": getattr(state, "s2_entered_at", None),
        "s2_age_minutes": round((time.time() - state.s2_entered_at) / 60, 1)
            if is_pair_mode and getattr(state, "s2_entered_at", None) else None,
        "exit_thresholds": None,
    }
    # Compute exit thresholds for dashboard display
    if is_pair_mode and getattr(state, "pair_stats", None):
        th = grid_strategy.compute_exit_thresholds(state.pair_stats)
        if th:
            result["exit_thresholds"] = {
                "reprice_min": round(th["reprice_after"] / 60, 1),
                "orphan_min": round(th["orphan_after"] / 60, 1),
            }
    return result


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Grid Bot</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:'Cascadia Mono','Fira Code',monospace;font-size:14px;padding:16px}
a{color:#58a6ff}
.header{display:flex;align-items:center;gap:16px;margin-bottom:20px;flex-wrap:wrap}
.header h1{font-size:20px;color:#f0f6fc}
.badge{padding:4px 10px;border-radius:4px;font-size:12px;font-weight:700;text-transform:uppercase}
.badge-dry{background:#f0883e;color:#0d1117}
.badge-live{background:#f85149;color:#fff}
.badge-paused{background:#8b949e;color:#0d1117}
.uptime{color:#8b949e;font-size:12px;margin-left:auto}

.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}
.card .label{font-size:11px;color:#8b949e;text-transform:uppercase;margin-bottom:4px}
.card .value{font-size:20px;font-weight:700;color:#f0f6fc}
.card .sub{font-size:11px;color:#8b949e;margin-top:2px}

.sections{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
@media(max-width:900px){.sections{grid-template-columns:1fr}}
.section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.section h2{font-size:14px;color:#f0f6fc;margin-bottom:12px;border-bottom:1px solid #30363d;padding-bottom:8px}

.ladder{max-height:400px;overflow-y:auto}
.ladder table{width:100%;border-collapse:collapse}
.ladder th,.ladder td{padding:4px 8px;text-align:right;font-size:12px}
.ladder th{color:#8b949e;position:sticky;top:0;background:#161b22}
.ladder .buy{color:#3fb950}
.ladder .sell{color:#f85149}
.ladder .marker{background:#1c2128;font-weight:700}
.ladder .marker td{color:#e3b341}

.trend-bar-wrap{margin-bottom:12px}
.trend-bar{display:flex;height:24px;border-radius:4px;overflow:hidden;margin-top:4px}
.trend-bar .buy-bar{background:#238636}
.trend-bar .sell-bar{background:#da3633}
.trend-bar span{display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#fff}
.trend-meta{display:flex;justify-content:space-between;font-size:11px;color:#8b949e;margin-top:4px}

.params{width:100%;border-collapse:collapse}
.params td{padding:4px 0;font-size:12px}
.params td:first-child{color:#8b949e;width:45%}
.params td:last-child{color:#f0f6fc;text-align:right}

.controls{display:flex;flex-direction:column;gap:10px}
.ctrl-row{display:flex;align-items:center;gap:8px}
.ctrl-row label{font-size:12px;color:#8b949e;width:70px}
.ctrl-row input{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;padding:4px 8px;font-family:inherit;font-size:12px;width:80px}
.ctrl-row button,.btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;padding:4px 12px;font-family:inherit;font-size:12px;cursor:pointer}
.ctrl-row button:hover,.btn:hover{background:#30363d;border-color:#8b949e}
.btn-auto{background:#1f3a1f;border-color:#238636;color:#3fb950}
.btn-auto:hover{background:#238636;color:#fff}
.ctrl-msg{font-size:11px;margin-top:4px}
.ctrl-msg.ok{color:#3fb950}
.ctrl-msg.err{color:#f85149}

.fills{max-height:350px;overflow-y:auto}
.fills table{width:100%;border-collapse:collapse}
.fills th,.fills td{padding:4px 8px;text-align:right;font-size:12px}
.fills th{color:#8b949e;position:sticky;top:0;background:#161b22}
.fills .buy{color:#3fb950}
.fills .sell{color:#f85149}
.fills .profit-pos{color:#3fb950}
.fills .profit-neg{color:#f85149}

.ai-rec{font-size:12px;color:#8b949e;white-space:pre-wrap;line-height:1.5}

/* Pair state machine banner */
.pair-state-bar{display:none;background:#161b22;border:2px solid #30363d;border-radius:8px;padding:14px 20px;margin-bottom:20px;text-align:center}
.pair-state-bar .ps-label{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}
.pair-state-bar .ps-state{font-size:22px;font-weight:700;color:#e3b341;margin:4px 0}
.pair-state-bar .ps-desc{font-size:12px;color:#8b949e}
.pair-state-bar.ps-S0{border-color:#238636} .pair-state-bar.ps-S0 .ps-state{color:#3fb950}
.pair-state-bar.ps-S1a{border-color:#9e6a03} .pair-state-bar.ps-S1a .ps-state{color:#e3b341}
.pair-state-bar.ps-S1b{border-color:#9e6a03} .pair-state-bar.ps-S1b .ps-state{color:#e3b341}
.pair-state-bar.ps-S2{border-color:#da3633} .pair-state-bar.ps-S2 .ps-state{color:#f85149}
.ps-trend{font-size:12px;margin-top:4px;font-weight:600}
.ps-s2-timer{font-size:11px;color:#8b949e;margin-top:4px}
.exit-age-badge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:3px;margin-left:6px;font-weight:600}
.exit-age-badge.ok{background:#238636;color:#3fb950}
.exit-age-badge.warn{background:#9e6a03;color:#e3b341}
.exit-age-badge.danger{background:#da3633;color:#f85149}
.recovery-panel{background:#161b22;border:2px solid #9e6a03;border-radius:8px;padding:14px 20px;margin-bottom:20px}
.recovery-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.recovery-title{font-size:14px;font-weight:700;color:#e3b341}
.recovery-stats{font-size:11px;color:#8b949e}
.recovery-table{width:100%;border-collapse:collapse;font-size:12px}
.recovery-table th{color:#8b949e;text-align:left;padding:4px 8px;border-bottom:1px solid #30363d;font-weight:400;text-transform:uppercase;font-size:10px;letter-spacing:1px}
.recovery-table td{padding:4px 8px;border-bottom:1px solid #21262d;color:#c9d1d9}

/* Trade A/B panels */
.pair-panels{display:none;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px}
@media(max-width:600px){.pair-panels{grid-template-columns:1fr}}
.pair-panel{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;border-top:3px solid #30363d}
.pair-panel.trade-a{border-top-color:#f85149}
.pair-panel.trade-b{border-top-color:#3fb950}
.pair-panel .pp-title{font-size:13px;font-weight:700;margin-bottom:8px}
.pair-panel.trade-a .pp-title{color:#f85149}
.pair-panel.trade-b .pp-title{color:#3fb950}
.pair-panel .pp-row{display:flex;justify-content:space-between;font-size:12px;padding:3px 0}
.pair-panel .pp-row .pp-label{color:#8b949e}
.pair-panel .pp-row .pp-val{color:#f0f6fc;font-weight:600}

/* Completed cycles table */
.cycles{max-height:300px;overflow-y:auto}
.cycles table{width:100%;border-collapse:collapse}
.cycles th,.cycles td{padding:4px 8px;text-align:right;font-size:12px}
.cycles th{color:#8b949e;position:sticky;top:0;background:#161b22}
.cycles .trade-a-tag{color:#f85149;font-weight:700}
.cycles .trade-b-tag{color:#3fb950;font-weight:700}

/* Unrealized P&L card */
.card-unreal .value{font-size:16px}
.card-unreal .sub-a{color:#f85149;font-size:11px}
.card-unreal .sub-b{color:#3fb950;font-size:11px}

/* Pair stats panel */
.pair-stats-panel{display:none;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:20px}
.pair-stats-panel h2{font-size:14px;color:#f0f6fc;margin-bottom:12px;border-bottom:1px solid #30363d;padding-bottom:8px}
.ps-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0}
@media(max-width:700px){.ps-grid{grid-template-columns:1fr}}
.ps-col{padding:0 12px;border-right:1px solid #30363d}
.ps-col:last-child{border-right:none}
.ps-col-head{font-size:12px;font-weight:700;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid #21262d}
.ps-col-head.col-a{color:#f85149}
.ps-col-head.col-b{color:#3fb950}
.ps-col-head.col-c{color:#58a6ff}
.ps-row{display:flex;justify-content:space-between;font-size:11px;padding:3px 0}
.ps-row .ps-k{color:#8b949e}
.ps-row .ps-v{color:#f0f6fc;font-weight:600}

/* AI council panel */
.ai-council-panel{display:none;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:20px}
.ai-council-panel h2{font-size:14px;color:#f0f6fc;margin-bottom:12px;border-bottom:1px solid #30363d;padding-bottom:8px}
.ai-verdict-row{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.ai-verdict-label{font-size:18px;font-weight:700}
.ai-verdict-label.v-hold{color:#e3b341}
.ai-verdict-label.v-adjust{color:#58a6ff}
.ai-verdict-label.v-pause{color:#f85149}
.ai-verdict-label.v-resume{color:#3fb950}
.ai-next{font-size:11px;color:#484f58}
.ai-panelists{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin-top:8px}
.ai-panelist{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px;border-left:3px solid #30363d}
.ai-panelist.ap-hold{border-left-color:#e3b341}
.ai-panelist.ap-adjust{border-left-color:#58a6ff}
.ai-panelist.ap-pause{border-left-color:#f85149}
.ai-panelist.ap-skip{border-left-color:#484f58}
.ai-panelist .ap-name{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px}
.ai-panelist .ap-vote{font-size:12px;font-weight:700;margin:2px 0}
.ai-panelist .ap-reason{font-size:11px;color:#8b949e;line-height:1.4}

/* Charts row */
.charts-row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}
@media(max-width:900px){.charts-row{grid-template-columns:1fr}}
.chart-box{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}
.chart-box h3{font-size:12px;color:#8b949e;margin-bottom:8px}
.chart-box canvas{width:100%;height:120px;display:block}
.chart-empty{color:#30363d;font-size:11px;text-align:center;padding:40px 0}

/* Strategy health banner */
.health-banner{background:#161b22;border:2px solid #30363d;border-radius:8px;padding:16px;margin-bottom:20px;text-align:center}
.health-banner .hb-verdict{font-size:18px;font-weight:700;margin-bottom:4px}
.health-banner .hb-summary{font-size:13px;color:#8b949e}
.hb-green{border-color:#238636}.hb-green .hb-verdict{color:#3fb950}
.hb-yellow{border-color:#9e6a03}.hb-yellow .hb-verdict{color:#e3b341}
.hb-red{border-color:#da3633}.hb-red .hb-verdict{color:#f85149}

/* Advisory board */
.advisory{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-bottom:20px}
.acard{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;border-left:4px solid #30363d}
.acard.ac-green{border-left-color:#238636}
.acard.ac-yellow{border-left-color:#9e6a03}
.acard.ac-red{border-left-color:#da3633}
.acard .ac-name{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px}
.acard .ac-verdict{font-size:13px;font-weight:700;margin:4px 0}
.acard .ac-verdict.v-green{color:#3fb950}
.acard .ac-verdict.v-yellow{color:#e3b341}
.acard .ac-verdict.v-red{color:#f85149}
.acard .ac-summary{font-size:12px;color:#c9d1d9;line-height:1.4}
.acard .ac-conf{font-size:10px;color:#484f58;margin-top:6px}
.audio-btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;padding:4px 10px;font-size:16px;cursor:pointer;margin-left:8px}
.audio-btn:hover{background:#30363d;border-color:#8b949e}
.audio-btn.active{background:#1f3a1f;border-color:#238636;color:#3fb950}

/* Modal */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.75);z-index:1000;justify-content:center;align-items:flex-start;padding:40px 16px;overflow-y:auto}
.modal-overlay.active{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:12px;max-width:740px;width:100%;padding:28px 32px;position:relative;margin-bottom:40px}
.modal-close{position:absolute;top:12px;right:16px;background:none;border:none;color:#8b949e;font-size:28px;cursor:pointer;line-height:1}
.modal-close:hover{color:#f0f6fc}
.modal h3{font-size:17px;color:#f0f6fc;margin-bottom:16px;padding-bottom:10px;border-bottom:2px solid #30363d}
.modal h4{font-size:12px;color:#58a6ff;margin:18px 0 8px;text-transform:uppercase;letter-spacing:0.8px}
.modal p,.modal li{font-size:13px;color:#c9d1d9;line-height:1.75;margin-bottom:8px}
.modal ul{margin:8px 0 12px 20px}
.modal li{margin-bottom:4px}
.modal b{color:#f0f6fc}
.modal .formula{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:14px 18px;margin:12px 0;font-size:15px;color:#e3b341;text-align:center;font-family:'Cascadia Mono','Fira Code',monospace;line-height:1.9}
.modal .formula .explain{font-size:11px;color:#8b949e;text-align:left;margin-top:10px;line-height:1.6;color:#8b949e}
.modal .hyp{background:#0d1117;border-left:3px solid #58a6ff;padding:10px 14px;margin:10px 0;font-size:13px;line-height:1.7}
.modal .hyp b{color:#58a6ff}
.modal .vtable{width:100%;border-collapse:collapse;margin:12px 0}
.modal .vtable th,.modal .vtable td{padding:7px 10px;font-size:12px;text-align:left;border-bottom:1px solid #21262d}
.modal .vtable th{color:#8b949e;font-weight:600}
.modal .vg{color:#3fb950}.modal .vy{color:#e3b341}.modal .vr{color:#f85149}
.modal .timeline{border-left:3px solid #30363d;padding-left:20px;margin:16px 0}
.modal .tl-item{margin-bottom:16px;position:relative}
.modal .tl-item::before{content:'';position:absolute;left:-26px;top:5px;width:12px;height:12px;border-radius:50%;background:#30363d;border:2px solid #161b22}
.modal .tl-time{font-size:11px;color:#58a6ff;font-weight:700;text-transform:uppercase}
.modal .tl-text{font-size:12px;color:#c9d1d9;margin-top:3px;line-height:1.6}
.modal .note{background:#1c2128;border:1px solid #30363d;border-radius:6px;padding:10px 14px;margin:12px 0;font-size:12px;color:#8b949e;line-height:1.6}
.acard{cursor:pointer;transition:border-color 0.15s,background 0.15s}
.acard:hover{border-color:#58a6ff;background:#1c2128}
.acard .ac-info{float:right;font-size:10px;color:#484f58;letter-spacing:0.3px}
.acard:hover .ac-info{color:#58a6ff}
.health-banner{cursor:pointer;transition:border-color 0.15s}
.health-banner:hover{border-color:#58a6ff !important}
.health-banner .hb-hint{font-size:10px;color:#484f58;margin-top:4px}
.health-banner:hover .hb-hint{color:#58a6ff}

/* Swarm view */
.swarm-view{display:none}
.swarm-header{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.swarm-header h2{font-size:16px;color:#f0f6fc;flex:1}
.swarm-agg{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:16px}
.swarm-agg .card .value{font-size:18px}
.swarm-table{width:100%;border-collapse:collapse;font-size:12px}
.swarm-table th{text-align:left;color:#8b949e;padding:6px 8px;border-bottom:1px solid #30363d;position:sticky;top:0;background:#0d1117;font-size:11px;text-transform:uppercase}
.swarm-table td{padding:4px 8px;border-bottom:1px solid #21262d}
.swarm-table tr:hover{background:#161b22}
.swarm-table tr{cursor:pointer}
.swarm-table .pnl-pos{color:#3fb950}
.swarm-table .pnl-neg{color:#f85149}
.swarm-table .pnl-zero{color:#8b949e}
.swarm-table select{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:3px;padding:2px 4px;font-size:11px;font-family:inherit}
.swarm-table .btn-remove{background:#21262d;border:1px solid #30363d;color:#f85149;border-radius:3px;padding:2px 8px;font-size:11px;cursor:pointer}
.swarm-table .btn-remove:hover{background:#da3633;color:#fff}
.btn-browse{background:#238636;border:1px solid #2ea043;color:#fff;border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer;font-family:inherit}
.btn-browse:hover{background:#2ea043}
.btn-back{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer;font-family:inherit;margin-bottom:16px}
.btn-back:hover{background:#30363d}
.detail-view{display:none}

/* Scanner modal */
.scanner-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.8);z-index:2000;justify-content:center;align-items:flex-start;padding:30px 16px;overflow-y:auto}
.scanner-overlay.active{display:flex}
.scanner-panel{background:#0d1117;border:1px solid #30363d;border-radius:12px;max-width:900px;width:100%;max-height:85vh;display:flex;flex-direction:column}
.scanner-head{padding:16px 20px;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:12px}
.scanner-head h3{font-size:16px;color:#f0f6fc;flex:1}
.scanner-head input{background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px 12px;font-size:13px;font-family:inherit;width:200px}
.scanner-head .scanner-close{background:none;border:none;color:#8b949e;font-size:24px;cursor:pointer}
.scanner-head .scanner-close:hover{color:#f0f6fc}
.scanner-body{overflow-y:auto;flex:1;padding:0}
.scanner-table{width:100%;border-collapse:collapse;font-size:12px}
.scanner-table th{text-align:left;color:#8b949e;padding:6px 10px;border-bottom:1px solid #30363d;position:sticky;top:0;background:#0d1117;font-size:11px}
.scanner-table td{padding:5px 10px;border-bottom:1px solid #21262d}
.scanner-table tr:hover{background:#161b22}
.scanner-table .btn-add{background:#238636;border:1px solid #2ea043;color:#fff;border-radius:3px;padding:2px 10px;font-size:11px;cursor:pointer}
.scanner-table .btn-add:hover{background:#2ea043}
.scanner-table .badge-active{background:#238636;color:#fff;border-radius:3px;padding:2px 8px;font-size:10px;font-weight:700}
.scanner-table th.sortable{cursor:pointer;user-select:none}
.scanner-table th.sortable:hover{color:#c9d1d9}
.scanner-table th.sort-active{color:#58a6ff}
.sort-arrow{font-size:9px;margin-left:3px}
.scanner-info{padding:16px 20px;color:#8b949e;font-size:12px;text-align:center}
</style>
</head>
<body>

<!-- Swarm View (shown when >1 pair active) -->
<div class="swarm-view" id="swarm-view">
  <div class="swarm-header">
    <h2>Swarm Manager</h2>
    <button class="btn-browse" onclick="openScanner()">Browse Pairs</button>
  </div>
  <div class="swarm-agg" id="swarm-agg">
    <div class="card"><div class="label">Active Pairs</div><div class="value" id="sw-pairs">--</div></div>
    <div class="card"><div class="label">Today P&amp;L</div><div class="value" id="sw-today">--</div></div>
    <div class="card"><div class="label">Total P&amp;L</div><div class="value" id="sw-total">--</div></div>
    <div class="card"><div class="label">Trips Today</div><div class="value" id="sw-trips">--</div></div>
    <div class="card"><div class="label">Total Trips</div><div class="value" id="sw-total-trips">--</div></div>
  </div>
  <div style="overflow-x:auto">
    <table class="swarm-table">
      <thead><tr>
        <th>Pair</th><th>Price</th><th>State</th><th>Today</th><th>Total</th><th>Trips</th><th>Entry%</th><th>Multi</th><th></th>
      </tr></thead>
      <tbody id="swarm-body"></tbody>
    </table>
  </div>
</div>

<!-- Scanner Modal -->
<div class="scanner-overlay" id="scanner-overlay" onclick="if(event.target===this)closeScanner()">
  <div class="scanner-panel">
    <div class="scanner-head">
      <h3>Available Pairs</h3>
      <input type="text" id="scanner-search" placeholder="Search pairs..." oninput="filterScanner()">
      <button class="scanner-close" onclick="closeScanner()">&times;</button>
    </div>
    <div class="scanner-body" id="scanner-body">
      <div class="scanner-info">Loading pairs...</div>
    </div>
  </div>
</div>

<!-- Detail View wrapper (wraps existing single-pair dashboard) -->
<div class="detail-view" id="detail-view">
<button class="btn-back" id="btn-back" onclick="showSwarmView()" style="display:none">&#8592; Back to Swarm</button>

<div class="header">
  <h1 id="bot-title">Grid Bot</h1>
  <span id="badge" class="badge badge-dry">---</span>
  <button class="btn-browse" onclick="openScanner()" title="Browse &amp; add trading pairs">+ Pairs</button>
  <button class="audio-btn" id="audio-btn" onclick="toggleAudio()" title="Toggle audio alerts">&#x1f507;</button>
  <span class="uptime" id="uptime">--</span>
</div>

<div class="cards">
  <div class="card"><div class="label">Price</div><div class="value" id="c-price">--</div><div class="sub" id="c-price-sub">--</div></div>
  <div class="card"><div class="label">Center</div><div class="value" id="c-center">--</div><div class="sub" id="c-center-sub">--</div></div>
  <div class="card"><div class="label">Today P&amp;L</div><div class="value" id="c-today">--</div><div class="sub" id="c-today-sub">--</div></div>
  <div class="card"><div class="label">Total P&amp;L</div><div class="value" id="c-total">--</div><div class="sub" id="c-total-sub">--</div></div>
  <div class="card"><div class="label">Round Trips</div><div class="value" id="c-trips">--</div><div class="sub" id="c-trips-sub">--</div></div>
  <div class="card"><div class="label" id="c-doge-label">DOGE Accumulated</div><div class="value" id="c-doge">--</div><div class="sub" id="c-doge-sub">&nbsp;</div></div>
  <div class="card card-unreal" id="c-unreal-card" style="display:none"><div class="label">Unrealized P&amp;L</div><div class="value" id="c-unreal">--</div><div class="sub-a" id="c-unreal-a">&nbsp;</div><div class="sub-b" id="c-unreal-b">&nbsp;</div></div>
</div>

<!-- Pair State Machine (pair mode only) -->
<div class="pair-state-bar" id="pair-state-bar">
  <div class="ps-label">State Machine</div>
  <div class="ps-state" id="ps-state">S0</div>
  <div class="ps-desc" id="ps-desc">Both entries open</div>
  <div class="ps-trend" id="ps-trend" style="display:none"></div>
  <div class="ps-s2-timer" id="ps-s2-timer" style="display:none"></div>
</div>

<!-- Recovery Orders Panel (pair mode, conditional) -->
<div class="recovery-panel" id="recovery-panel" style="display:none">
  <div class="recovery-header">
    <span class="recovery-title" id="recovery-title">Recovery Orders (0/2 slots)</span>
    <span class="recovery-stats" id="recovery-stats"></span>
  </div>
  <table class="recovery-table">
    <thead><tr><th>Trade</th><th>Side</th><th>Exit Price</th><th>Entry Price</th><th>Age</th><th>Unrealized P&amp;L</th></tr></thead>
    <tbody id="recovery-body"></tbody>
  </table>
</div>

<!-- Trade A/B Panels (pair mode only) -->
<div class="pair-panels" id="pair-panels">
  <div class="pair-panel trade-a">
    <div class="pp-title">Trade A (Short)</div>
    <div class="pp-row"><span class="pp-label">Cycle</span><span class="pp-val" id="pp-a-cycle">--</span></div>
    <div class="pp-row"><span class="pp-label">Leg</span><span class="pp-val" id="pp-a-leg">--</span></div>
    <div class="pp-row"><span class="pp-label">Entry price</span><span class="pp-val" id="pp-a-entry">--</span></div>
    <div class="pp-row"><span class="pp-label">Target</span><span class="pp-val" id="pp-a-target">--</span></div>
    <div class="pp-row"><span class="pp-label">Distance</span><span class="pp-val" id="pp-a-dist">--</span></div>
    <div class="pp-row"><span class="pp-label">Est P&amp;L</span><span class="pp-val" id="pp-a-est">--</span></div>
    <div class="pp-row"><span class="pp-label">Completed</span><span class="pp-val" id="pp-a-completed">--</span></div>
    <div class="pp-row"><span class="pp-label">Net P&amp;L</span><span class="pp-val" id="pp-a-pnl">--</span></div>
    <div class="pp-row"><span class="pp-label">Avg profit</span><span class="pp-val" id="pp-a-avg">--</span></div>
  </div>
  <div class="pair-panel trade-b">
    <div class="pp-title">Trade B (Long)</div>
    <div class="pp-row"><span class="pp-label">Cycle</span><span class="pp-val" id="pp-b-cycle">--</span></div>
    <div class="pp-row"><span class="pp-label">Leg</span><span class="pp-val" id="pp-b-leg">--</span></div>
    <div class="pp-row"><span class="pp-label">Entry price</span><span class="pp-val" id="pp-b-entry">--</span></div>
    <div class="pp-row"><span class="pp-label">Target</span><span class="pp-val" id="pp-b-target">--</span></div>
    <div class="pp-row"><span class="pp-label">Distance</span><span class="pp-val" id="pp-b-dist">--</span></div>
    <div class="pp-row"><span class="pp-label">Est P&amp;L</span><span class="pp-val" id="pp-b-est">--</span></div>
    <div class="pp-row"><span class="pp-label">Completed</span><span class="pp-val" id="pp-b-completed">--</span></div>
    <div class="pp-row"><span class="pp-label">Net P&amp;L</span><span class="pp-val" id="pp-b-pnl">--</span></div>
    <div class="pp-row"><span class="pp-label">Avg profit</span><span class="pp-val" id="pp-b-avg">--</span></div>
  </div>
</div>

<!-- Pair Stats Panel (pair mode only) -->
<div class="pair-stats-panel" id="pair-stats-panel">
  <h2>Pair Statistics</h2>
  <div class="ps-grid">
    <div class="ps-col" id="ps-col-a">
      <div class="ps-col-head col-a">Trade A (Short)</div>
    </div>
    <div class="ps-col" id="ps-col-b">
      <div class="ps-col-head col-b">Trade B (Long)</div>
    </div>
    <div class="ps-col" id="ps-col-c">
      <div class="ps-col-head col-c">Combined</div>
    </div>
  </div>
</div>

<!-- AI Council Panel (pair mode only) -->
<div class="ai-council-panel" id="ai-council-panel">
  <h2>AI Council</h2>
  <div class="ai-verdict-row">
    <div class="ai-verdict-label" id="ai-verdict">--</div>
    <div class="ai-next" id="ai-next"></div>
  </div>
  <div class="ai-panelists" id="ai-panelists"></div>
</div>

<!-- Charts -->
<div class="charts-row">
  <div class="chart-box"><h3>Price (24h)</h3><canvas id="chart-price"></canvas></div>
  <div class="chart-box"><h3>Fill Rate (hourly, 24h)</h3><canvas id="chart-fills"></canvas></div>
  <div class="chart-box"><h3>Round Trip Profits</h3><canvas id="chart-profits"></canvas></div>
</div>

<!-- Strategy Health Banner -->
<div class="health-banner" id="health-banner" onclick="openModal('timeline')" title="Click for timeline &amp; methodology">
  <div class="hb-verdict" id="hb-verdict">Collecting data...</div>
  <div class="hb-summary" id="hb-summary">Statistical analyzers need more fills to produce results</div>
  <div class="hb-hint">Click for data sufficiency timeline &amp; methodology</div>
</div>

<!-- Statistical Advisory Board -->
<div class="advisory" id="advisory"></div>

<div class="sections">
  <!-- Grid ladder -->
  <div class="section">
    <h2 id="ladder-title">Grid Ladder</h2>
    <div class="ladder" id="ladder-wrap">
      <table><thead><tr id="ladder-head"><th>Lvl</th><th>Side</th><th>Price</th><th>Volume</th><th>Status</th></tr></thead>
      <tbody id="ladder-body"></tbody></table>
    </div>
  </div>

  <!-- Right column -->
  <div class="section">
    <div id="trend-section">
    <h2>Trend Ratio</h2>
    <div class="trend-bar-wrap">
      <div class="trend-bar">
        <span class="buy-bar" id="trend-buy" style="width:50%">50% Buy</span>
        <span class="sell-bar" id="trend-sell" style="width:50%">50% Sell</span>
      </div>
      <div class="trend-meta">
        <span id="trend-source">auto</span>
        <span id="trend-fills">0 buys / 0 sells (12h)</span>
      </div>
    </div>
    </div>

    <h2 style="margin-top:16px">Adaptive Parameters</h2>
    <table class="params">
      <tr><td>Order size</td><td id="p-size">--</td></tr>
      <tr id="p-levels-row"><td>Grid levels</td><td id="p-levels">--</td></tr>
      <tr><td id="p-spacing-label">Spacing</td><td id="p-spacing">--</td></tr>
      <tr id="p-entry-row" style="display:none"><td>Entry distance</td><td id="p-entry">--</td></tr>
      <tr id="p-refresh-row" style="display:none"><td>Refresh drift</td><td id="p-refresh">--</td></tr>
      <tr><td>Effective capital</td><td id="p-capital">--</td></tr>
      <tr><td>Fees (round trip)</td><td id="p-fees">--</td></tr>
      <tr><td>AI mode</td><td id="p-ai">--</td></tr>
      <tr><td>AI recommendation</td><td id="p-airec" class="ai-rec">--</td></tr>
    </table>

    <h2 style="margin-top:16px">Controls</h2>
    <div class="controls">
      <div class="ctrl-row">
        <label id="ctrl-spacing-label">Spacing %</label>
        <input type="number" id="in-spacing" step="0.1" min="0.6">
        <button onclick="applySpacing()">Apply</button>
      </div>
      <div class="ctrl-row" id="ctrl-entry-row" style="display:none">
        <label>Entry %</label>
        <input type="number" id="in-entry" step="0.05" min="0.05">
        <button onclick="applyEntry()">Apply</button>
      </div>
      <div class="ctrl-row" id="ctrl-ratio-row">
        <label>Ratio</label>
        <input type="number" id="in-ratio" step="0.05" min="0.25" max="0.75">
        <button onclick="applyRatio()">Apply</button>
        <button class="btn-auto" onclick="applyRatioAuto()">Auto</button>
      </div>
      <div class="ctrl-row">
        <button onclick="triggerAICheck()" id="btn-ai-check">Probe AI Council</button>
      </div>
      <div id="ctrl-msg" class="ctrl-msg">&nbsp;</div>
      <div id="ctrl-help" class="ctrl-msg" style="color:#8b949e;display:none">Entry %: replaces open entries immediately. Profit %: applies to next exit.</div>
    </div>
  </div>
</div>

<!-- Completed Cycles (pair mode only) -->
<div class="sections" id="cycles-section" style="display:none">
  <div class="section" style="grid-column:1/-1">
    <h2>Completed Cycles (recent)</h2>
    <div class="cycles" id="cycles-wrap">
      <table><thead><tr><th>Time</th><th>Trade</th><th>Cycle</th><th>Entry</th><th>Exit</th><th>Volume</th><th>Duration</th><th>Net P&amp;L</th></tr></thead>
      <tbody id="cycles-body"></tbody></table>
    </div>
  </div>
</div>

<div class="sections">
  <div class="section" style="grid-column:1/-1">
    <h2>Recent Fills (last 20)</h2>
    <div class="fills" id="fills-wrap">
      <table><thead><tr><th>Time</th><th>Leg</th><th>Price</th><th>Volume</th><th>Profit</th></tr></thead>
      <tbody id="fills-body"></tbody></table>
    </div>
  </div>
</div>

<div class="sections">
  <div class="section" style="grid-column:1/-1">
    <h2>Export Data</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <a class="btn" href="/api/export/fills?format=csv" download>Fills CSV</a>
      <a class="btn" href="/api/export/fills?format=json" target="_blank">Fills JSON</a>
      <a class="btn" href="/api/export/stats?format=csv" download>Stats CSV</a>
      <a class="btn" href="/api/export/stats?format=json" target="_blank">Stats JSON</a>
      <a class="btn" href="/api/export/trades?format=csv" download>Trades CSV</a>
      <a class="btn" href="/api/export/trades?format=json" target="_blank">Trades JSON</a>
    </div>
  </div>
</div>

<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <div id="modal-content"></div>
  </div>
</div>

</div><!-- end detail-view -->

<script>
const API = '/api/status';
const CONFIG = '/api/config';

function fmt(n, d) { return n != null ? n.toFixed(d) : '--'; }
function fmtUSD(n) { return n != null ? '$' + n.toFixed(4) : '--'; }
function fmtTime(ts) {
  if (!ts) return '--';
  return new Date(ts * 1000).toLocaleTimeString();
}
function fmtUptime(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h + 'h ' + m + 'm ' + sec + 's';
}
function fmtDuration(sec) {
  if (sec == null || sec <= 0) return '\u2014';
  if (sec < 60) return Math.round(sec) + 's';
  if (sec < 3600) return Math.floor(sec/60) + 'm ' + Math.round(sec%60) + 's';
  return Math.floor(sec/3600) + 'h ' + Math.floor((sec%3600)/60) + 'm';
}
function fmtPct(n) { return n != null ? n.toFixed(1) + '%' : '\u2014'; }
function fmtStatUSD(n) { return n != null ? fmtUSD(n) : '\u2014'; }
function fmtStatNum(n, d) { return n != null ? n.toFixed(d) : '\u2014'; }
function _exitAgeBadge(order, data) {
  if (!order || !order.entry_filled_at || order.entry_filled_at === 0) return null;
  const role = order.role || '';
  if (role !== 'exit') return null;
  const ageSec = (data.server_time || Date.now()/1000) - order.entry_filled_at;
  if (ageSec <= 0) return null;
  const ageMin = ageSec / 60;
  const th = data.exit_thresholds;
  let cls = 'ok';
  if (th) {
    if (ageMin >= th.orphan_min) cls = 'danger';
    else if (ageMin >= th.reprice_min) cls = 'warn';
  } else if (ageMin >= 120) cls = 'danger';
  else if (ageMin >= 60) cls = 'warn';
  const txt = ageMin < 60 ? fmt(ageMin,0) + 'm' : fmt(ageMin/60,1) + 'h';
  const badge = document.createElement('span');
  badge.className = 'exit-age-badge ' + cls;
  badge.textContent = txt;
  return badge;
}
// Local uptime ticker -- set base once, tick locally, resync only on bot restart
let _lastServerUptime = 0;
let _lastServerUptimeAt = 0;
setInterval(function() {
  if (!_lastServerUptimeAt) return;
  const elapsed = (Date.now() - _lastServerUptimeAt) / 1000;
  const current = _lastServerUptime + elapsed;
  document.getElementById('uptime').textContent = 'Up ' + fmtUptime(current);
}, 1000);

// === Chart rendering ===

function setupCanvas(id) {
  const canvas = document.getElementById(id);
  if (!canvas) return null;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  return { ctx, w: rect.width, h: rect.height };
}

function drawSparkline(id, data, color) {
  const c = setupCanvas(id);
  if (!c || !data || data.length < 2) return;
  const { ctx, w, h } = c;
  ctx.clearRect(0, 0, w, h);
  const vals = data.map(d => d[1]);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const range = mx - mn || 1;
  // axis labels
  ctx.fillStyle = '#484f58';
  ctx.font = '10px monospace';
  ctx.fillText('$' + mx.toFixed(4), 2, 10);
  ctx.fillText('$' + mn.toFixed(4), 2, h - 2);
  // line
  const pad = 55;
  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  for (let i = 0; i < data.length; i++) {
    const x = pad + (i / (data.length - 1)) * (w - pad - 4);
    const y = 4 + (1 - (vals[i] - mn) / range) * (h - 12);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();
}

function drawBars(id, data, colorFn) {
  const c = setupCanvas(id);
  if (!c || !data || data.length === 0) return;
  const { ctx, w, h } = c;
  ctx.clearRect(0, 0, w, h);
  const mx = Math.max(...data, 1);
  const bw = (w - 4) / data.length;
  for (let i = 0; i < data.length; i++) {
    const bh = (data[i] / mx) * (h - 16);
    ctx.fillStyle = colorFn ? colorFn(data[i]) : '#58a6ff';
    ctx.fillRect(2 + i * bw + 1, h - bh - 12, bw - 2, bh);
  }
  // x-axis labels
  ctx.fillStyle = '#484f58';
  ctx.font = '9px monospace';
  ctx.fillText('-24h', 2, h - 1);
  ctx.fillText('now', w - 22, h - 1);
  // max label
  ctx.fillText('max:' + mx, 2, 10);
}

function drawProfitDots(id, data) {
  const c = setupCanvas(id);
  if (!c || !data || data.length === 0) return;
  const { ctx, w, h } = c;
  ctx.clearRect(0, 0, w, h);
  const vals = data.map(d => d[1]);
  const mn = Math.min(...vals, 0), mx = Math.max(...vals, 0);
  const range = mx - mn || 1;
  // zero line
  const zeroY = 4 + (1 - (0 - mn) / range) * (h - 12);
  ctx.strokeStyle = '#30363d';
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(0, zeroY);
  ctx.lineTo(w, zeroY);
  ctx.stroke();
  ctx.setLineDash([]);
  // dots
  for (let i = 0; i < data.length; i++) {
    const x = 4 + (i / Math.max(data.length - 1, 1)) * (w - 8);
    const y = 4 + (1 - (vals[i] - mn) / range) * (h - 12);
    ctx.fillStyle = vals[i] >= 0 ? '#3fb950' : '#f85149';
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
  }
  // labels
  ctx.fillStyle = '#484f58';
  ctx.font = '10px monospace';
  if (mx > 0) ctx.fillText('+$' + mx.toFixed(4), 2, 10);
  if (mn < 0) ctx.fillText('-$' + Math.abs(mn).toFixed(4), 2, h - 2);
}

// === Advisory board rendering ===

const ANALYZER_LABELS = {
  profitability: 'Profitability Test',
  fill_asymmetry: 'Fill Asymmetry',
  grid_exceedance: 'Hidden Risk',
  volatility_targets: 'Volatility vs Targets',
  fill_rate: 'Volatility Regime',
  random_walk: 'Market Type',
  round_trip_duration: 'Round Trip Duration',
};

// === Modal System ===

const MODAL = {};

MODAL.profitability = `
<h3>Profitability Test</h3>
<h4>Method: One-Sample <i>t</i>-Test</h4>
<p>Tests whether the bot's mean profit per round trip is statistically different from zero. This is the same one-sample <i>t</i>-test from AP Statistics -- applied to trading data instead of textbook examples.</p>

<div class="hyp">
<b>H<sub>0</sub>:</b> &#956; = 0 &emsp;(mean profit per round trip is zero -- the strategy has no edge)<br>
<b>H<sub>a</sub>:</b> &#956; &#8800; 0 &emsp;(mean profit is significantly different from zero)
</div>

<h4>Conditions for Inference</h4>
<ul>
<li><b>Independence:</b> Each round trip is an independent trial. The bot buys at one grid level and sells at the next level up; each cycle's profit depends on that specific price movement, not previous cycles. (In practice, serial correlation is minimal because grid fills are event-driven, not time-driven.)</li>
<li><b>Normality:</b> For small <i>n</i>, we need the population of profits to be approximately normal. Profits are bounded below (can't lose more than the order size) and above (capped by grid spacing), making the distribution roughly symmetric. For <i>n</i> &#8805; 30, CLT handles non-normality.</li>
<li><b>Random sample:</b> Each round trip is a sample from the "process" of grid trading at current market conditions.</li>
</ul>

<h4>Test Statistic</h4>
<div class="formula">
<i>t</i> = <i>x&#772;</i> / (<i>s</i> / &#8730;<i>n</i>)
<div class="explain">
where:<br>
&emsp;<i>x&#772;</i> = sample mean of round-trip profits (&#8721;<i>x<sub>i</sub></i> / <i>n</i>)<br>
&emsp;<i>s</i> = sample standard deviation = &#8730;[&#8721;(<i>x<sub>i</sub></i> - <i>x&#772;</i>)&#178; / (<i>n</i> - 1)]<br>
&emsp;<i>n</i> = number of completed round trips<br>
&emsp;df = <i>n</i> - 1
</div>
</div>

<p><b>Distribution under H<sub>0</sub>:</b> The test statistic follows <b>Student's <i>t</i>-distribution</b> with <i>n</i> - 1 degrees of freedom. The implementation uses the regularized incomplete beta function to compute the CDF (no lookup tables needed).</p>

<p><b>Decision rule:</b> Reject H<sub>0</sub> if <i>p</i>-value &lt; &#945; = 0.05</p>

<h4>95% Confidence Interval</h4>
<div class="formula">
<i>x&#772;</i> &#177; <i>t</i>*<sub>df, 0.025</sub> &#183; (<i>s</i> / &#8730;<i>n</i>)
<div class="explain">
The critical value <i>t</i>* is looked up from a table for df = <i>n</i> - 1. For df &#8805; 120, <i>t</i>* &#8776; 1.96 (approaching the standard normal <i>z</i>*). This interval gives us the range of plausible values for the true mean profit per trip.
</div>
</div>

<h4>Interpretation in Context</h4>
<p>Each round trip works like this: the bot buys DOGE at price level <i>k</i>, then sells at level <i>k</i> + 1. The gross profit = (sell_price - buy_price) &#215; volume. After subtracting Kraken's maker fees on both legs (0.25% each), the net profit depends on whether the grid spacing exceeds the round-trip fee cost.</p>
<p>The <i>t</i>-test determines whether the observed mean profit is <b>statistically distinguishable from zero</b>, or whether apparent profits could be due to random variation. A "significant profit" means you can confidently say the strategy has a positive expected value -- the profits are real, not sampling noise.</p>

<h4>Power & Minimum Sample Size</h4>
<p><b>Mathematical minimum:</b> <i>n</i> &#8805; 3 (need df &#8805; 2 for a non-degenerate variance estimate).</p>
<p><b>Practical reality:</b> With small <i>n</i>, the standard error <i>s</i>/&#8730;<i>n</i> is large, and the confidence interval is wide. The test usually reports "not significant" until <i>n</i> &#8776; 10-20, depending on the <b>effect size</b> (<i>x&#772;</i>/<i>s</i>).</p>
<p>The bot estimates the minimum <i>n</i> needed for significance at the current effect size:</p>
<div class="formula">
<i>n</i><sub>min</sub> = &#8968;(<i>t</i>* &#183; <i>s</i> / |<i>x&#772;</i>|)&#178;&#8969;
<div class="explain">
This uses the current effect size to project how many more trips are needed. Wider grid spacing (larger profit per trip relative to variance) reaches significance faster. This is analogous to a power analysis: given the observed effect, how large a sample do we need?
</div>
</div>

<h4>Verdict Mapping</h4>
<table class="vtable">
<tr><th>Verdict</th><th>Condition</th><th>Meaning</th></tr>
<tr><td class="vg">SIGNIFICANT PROFIT</td><td><i>p</i> &lt; 0.05 and <i>x&#772;</i> &gt; 0</td><td>Reject H<sub>0</sub>. Profits are statistically real.</td></tr>
<tr><td class="vr">SIGNIFICANT LOSS</td><td><i>p</i> &lt; 0.05 and <i>x&#772;</i> &#8804; 0</td><td>Reject H<sub>0</sub>. Strategy is reliably losing money.</td></tr>
<tr><td class="vy">NOT SIGNIFICANT</td><td><i>p</i> &#8805; 0.05</td><td>Fail to reject H<sub>0</sub>. Need more data.</td></tr>
<tr><td class="vy">INSUFFICIENT DATA</td><td><i>n</i> &lt; 3</td><td>Cannot run the test yet.</td></tr>
</table>
`;

MODAL.fill_asymmetry = `
<h3>Fill Asymmetry</h3>
<h4>Method: Exact Binomial Test</h4>
<p>Tests whether buy fills and sell fills occur with equal probability in a 12-hour sliding window. This uses the exact binomial distribution rather than a normal approximation, so it remains valid even at small sample sizes.</p>

<div class="hyp">
<b>H<sub>0</sub>:</b> <i>p</i> = 0.5 &emsp;(buy and sell fills are equally likely -- range-bound market)<br>
<b>H<sub>a</sub>:</b> <i>p</i> &#8800; 0.5 &emsp;(one side fills significantly more often -- trending market)
</div>

<h4>Setup</h4>
<p>Let <i>X</i> = number of buy fills out of <i>n</i> total fills in the 12-hour window. Under H<sub>0</sub>:</p>
<div class="formula">
<i>X</i> ~ Binomial(<i>n</i>, 0.5)
<div class="explain">
Each fill is classified as "buy" (price dipped to trigger a buy order) or "sell" (price rose to trigger a sell order). Under a symmetric, range-bound market, these should be equally likely.
</div>
</div>

<h4>Conditions</h4>
<ul>
<li><b>Independence:</b> Each fill is triggered by a separate price crossing event. While prices are serially correlated, the fill <i>events</i> (crossing a specific level) are approximately independent.</li>
<li><b>Fixed <i>n</i>:</b> We condition on the total number of fills observed in the window.</li>
<li><b>Two categories:</b> Each fill is either buy or sell (binary outcome).</li>
</ul>

<h4>Test Statistic & <i>p</i>-Value</h4>
<p>Let <i>k</i> = max(buys, sells). The two-tailed exact binomial <i>p</i>-value is:</p>
<div class="formula">
<i>p</i>-value = 2 &#183; P(<i>X</i> &#8805; <i>k</i> | <i>n</i>, <i>p</i> = 0.5)
<div class="explain">
Computed via the regularized incomplete beta function:<br><br>
P(<i>X</i> &#8805; <i>k</i>) = 1 - I<sub>0.5</sub>(<i>n</i> - <i>k</i> + 1, <i>k</i>)<br><br>
where I<sub><i>x</i></sub>(<i>a</i>, <i>b</i>) is the regularized incomplete beta function. This is mathematically equivalent to summing the binomial PMF from <i>k</i> to <i>n</i>, but numerically stable. The factor of 2 makes it two-tailed.
</div>
</div>

<div class="note">
<b>Why exact instead of normal approximation?</b> The familiar z-test for proportions (z = (<i>p&#770;</i> - 0.5) / &#8730;(0.25/<i>n</i>)) requires <i>np</i> &#8805; 10 and <i>n</i>(1-<i>p</i>) &#8805; 10. At small sample sizes (n = 5-15), the exact binomial is more appropriate. The implementation uses the incomplete beta function, which gives exact results at any <i>n</i>.
</div>

<h4>Confidence Interval: Wilson Score</h4>
<div class="formula">
<i>p&#771;</i> &#177; <i>z</i> &#183; &#8730;(<i>p&#770;</i>(1-<i>p&#770;</i>)/<i>n</i> + <i>z</i>&#178;/4<i>n</i>&#178;) / (1 + <i>z</i>&#178;/<i>n</i>)
<div class="explain">
where <i>p&#771;</i> = (<i>p&#770;</i> + <i>z</i>&#178;/2<i>n</i>) / (1 + <i>z</i>&#178;/<i>n</i>), <i>z</i> = 1.96 for 95% confidence, and <i>p&#770;</i> = buys/<i>n</i>.<br><br>
Wilson score is preferred over the Wald interval (<i>p&#770;</i> &#177; <i>z</i>&#8730;(<i>p&#770;</i><i>q&#770;</i>/<i>n</i>)) because it has better coverage probability for small <i>n</i> and proportions near 0 or 1.
</div>
</div>

<h4>Interpretation</h4>
<p>In a range-bound market, price oscillates and fills are balanced:</p>
<ul>
<li><b>Buys dominate</b> (p&#770; &gt; 0.5) &#8594; price is falling. The bot keeps buying dips, but sell orders above aren't triggering. This suggests a bearish trend.</li>
<li><b>Sells dominate</b> (p&#770; &lt; 0.5) &#8594; price is rising. The bot keeps selling rallies, but buy orders below aren't triggering. This suggests a bullish trend.</li>
</ul>
<p>Grid bots profit from oscillation, not direction. Persistent asymmetry means the grid should be re-centered, or the buy/sell ratio adjusted to match the trend.</p>

<h4>Sample Size Considerations</h4>
<p><b>Minimum:</b> <i>n</i> &#8805; 5 fills in the 12h window. At <i>n</i> = 5, only extreme splits (5/0) yield <i>p</i> &lt; 0.05. To detect moderate asymmetry (e.g. 65/35 split), you need <i>n</i> &#8776; 40-50 fills.</p>

<h4>Verdict Mapping</h4>
<table class="vtable">
<tr><th>Verdict</th><th>Condition</th><th>Meaning</th></tr>
<tr><td class="vr">TREND DETECTED</td><td><i>p</i> &lt; 0.05</td><td>Significant directional bias in fills</td></tr>
<tr><td class="vg">SYMMETRIC</td><td><i>p</i> &#8805; 0.05</td><td>No evidence of trend -- market appears range-bound</td></tr>
<tr><td class="vy">INSUFFICIENT DATA</td><td><i>n</i> &lt; 5</td><td>Not enough fills in window</td></tr>
</table>
`;

MODAL.grid_exceedance = `
<h3>Hidden Risk (Grid Exceedance)</h3>
<h4>Method: OHLC Boundary Proportion Analysis</h4>
<p>Measures how often price escapes the grid's range between the bot's 30-second polling intervals. This is a <b>descriptive risk metric</b> rather than a formal hypothesis test -- it quantifies risk the bot cannot directly observe.</p>

<h4>Setup</h4>
<p>The grid defines a price corridor [grid<sub>low</sub>, grid<sub>high</sub>] bounded by the outermost buy and sell orders. Kraken provides 5-minute OHLC (Open-High-Low-Close) candles that capture intra-period price extremes the bot's 30-second snapshots miss.</p>

<p>A candle <b>exceeds</b> the grid if:</p>
<div class="formula">
High &gt; grid<sub>high</sub> &emsp; OR &emsp; Low &lt; grid<sub>low</sub>
</div>

<h4>Metrics Computed</h4>
<div class="formula">
exceedance% = (candles with breach / total candles) &#215; 100
<div class="explain">
Additional metrics tracked:<br>
&emsp;&#8226; worst_breach% = max over all candles of (breach distance / grid boundary) &#215; 100<br>
&emsp;&#8226; above_count = candles where High exceeded grid_high<br>
&emsp;&#8226; below_count = candles where Low went below grid_low<br>
&emsp;&#8226; Directional skew: if above &#8811; below, price is testing the upside
</div>
</div>

<h4>Why This Matters</h4>
<p>The bot polls every 30 seconds, but cryptocurrency prices can spike dramatically within seconds and revert. These <b>hidden excursions</b> have three implications:</p>
<ul>
<li><b>Missed opportunities:</b> Price moved beyond the grid where no orders existed, and potential profit was left on the table.</li>
<li><b>Underestimated risk:</b> The bot's state shows everything "contained" but price actually breached the boundary, meaning realized volatility exceeds what the grid is capturing.</li>
<li><b>Breakout precursor:</b> Increasing exceedance frequency often precedes a sustained move beyond the grid, which can cause one-sided fill accumulation.</li>
</ul>

<div class="note">
<b>Statistical note:</b> This is deliberately not a hypothesis test. A formal test (e.g. testing if exceedance proportion &gt; 5%) would add complexity without value. The thresholds below are calibrated from empirical observation: at &gt; 20% exceedance, the grid is clearly too narrow for the observed volatility, regardless of p-values.
</div>

<h4>Decision Thresholds</h4>
<table class="vtable">
<tr><th>Verdict</th><th>Condition</th><th>Action</th></tr>
<tr><td class="vg">CONTAINED</td><td>&#8804; 5% of candles exceed</td><td>Grid covers the price action well</td></tr>
<tr><td class="vy">MODERATE RISK</td><td>5-20% of candles exceed</td><td>Consider widening grid or adding levels</td></tr>
<tr><td class="vr">HIGH RISK</td><td>&gt; 20% of candles exceed</td><td>Price regularly escaping -- widen immediately</td></tr>
</table>

<h4>Data Requirements</h4>
<p><b>No fills needed.</b> This analyzer uses OHLC candle data fetched every 5 minutes from Kraken's public API. It produces results as soon as candle data is available (~60 seconds after bot start). It examines up to 720 candles (60 hours of 5-minute data), giving it the earliest and most reliable signal of all five analyzers.</p>
`;

MODAL.fill_rate = `
<h3>Volatility Regime (Fill Rate)</h3>
<h4>Method: Poisson <i>z</i>-Test</h4>
<p>Compares the current hour's fill rate against the historical baseline to detect volatility regime changes. Uses a Poisson process model and the normal approximation for large &#955;.</p>

<div class="hyp">
<b>H<sub>0</sub>:</b> &#955;<sub>current</sub> = &#955;<sub>baseline</sub> &emsp;(current fill rate equals historical average)<br>
<b>H<sub>a</sub>:</b> &#955;<sub>current</sub> &#8800; &#955;<sub>baseline</sub> &emsp;(a regime change has occurred)
</div>

<h4>Poisson Process Model</h4>
<p>Fill arrivals are modeled as a <b>Poisson process</b> -- events (grid order fills) occur independently at a constant average rate. Under stable volatility, the number of fills per hour follows:</p>
<div class="formula">
<i>X</i> ~ Poisson(&#955;)
<div class="explain">
where &#955; = expected fills per hour. The Poisson distribution is the natural model for count data where events occur independently at a constant rate -- the same model used for radioactive decay, customer arrivals, and website hits.
</div>
</div>

<h4>Conditions</h4>
<ul>
<li><b>Independence:</b> Fill events are approximately independent. While prices exhibit serial correlation, the <i>events</i> of crossing specific grid levels are approximately independent over hour-scale windows.</li>
<li><b>Constant rate (under H<sub>0</sub>):</b> The null hypothesis assumes volatility hasn't changed, so the fill rate should be stable.</li>
<li><b>Sufficient &#955;:</b> For the normal approximation to hold, we need &#955;<sub>baseline</sub> to be reasonably large (&#8805; 5). The Poisson CLT gives good results even for moderate &#955;.</li>
</ul>

<h4>Baseline & Current Rate</h4>
<div class="formula">
&#955;<sub>baseline</sub> = (fills before last hour) / (hours of earlier history)<br><br>
observed = fills in the most recent 60 minutes
</div>

<h4>Test Statistic</h4>
<div class="formula">
<i>z</i> = (observed - &#955;<sub>baseline</sub>) / &#8730;&#955;<sub>baseline</sub>
<div class="explain">
By the Poisson CLT: for sufficiently large &#955;, the Poisson distribution is approximately Normal(&#955;, &#955;). Since Var(Poisson) = &#955;, the standard deviation is &#8730;&#955;. The <i>z</i>-score measures how many standard deviations the current rate deviates from the expected baseline.
</div>
</div>

<h4>Why Sigma Thresholds?</h4>
<p>This analyzer uses |<i>z</i>| (sigma) thresholds rather than a fixed &#945; because it functions as a <b>continuous monitoring tool</b>, not a one-time test. The sigma scale maps directly to intuitive probability:</p>

<table class="vtable">
<tr><th>|<i>z</i>| Range</th><th>Verdict</th><th>P(this extreme | H<sub>0</sub>)</th><th>Meaning</th></tr>
<tr><td>&#8804; 2</td><td class="vg">NORMAL</td><td>~95.4% of the time</td><td>Fill rate consistent with baseline</td></tr>
<tr><td>2 - 3</td><td class="vy">ELEVATED / REDUCED</td><td>~4.3% of the time</td><td>Moderate regime shift detected</td></tr>
<tr><td>&gt; 3</td><td class="vr">HIGH VOL / LOW VOL</td><td>&lt; 0.27% of the time</td><td>Strong regime change -- rare under H<sub>0</sub></td></tr>
</table>

<h4>Interpretation for Grid Trading</h4>
<ul>
<li><b>HIGH VOL:</b> Fill rate spiked. Could mean a breakout -- more fills &#8800; more profit if the move is one-directional (triggers buys without matching sells, or vice versa). Consider widening spacing.</li>
<li><b>LOW VOL:</b> Fill rate dropped. Grid is idle, capital deployed but not earning. Consider tightening spacing to capture smaller oscillations.</li>
<li><b>NORMAL:</b> Current volatility matches historical. Grid parameters are appropriately sized.</li>
</ul>

<h4>Data Requirements</h4>
<p><b>Minimum:</b> 5 total fills AND bot running &gt; 1 hour. The test requires a "before" period (history excluding the last hour) to establish &#955;<sub>baseline</sub>. With less than 1 hour of uptime, there is no pre-period to compare against.</p>
`;

MODAL.random_walk = `
<h3>Market Type (Random Walk)</h3>
<h4>Method: Chi-Squared Goodness-of-Fit Test</h4>
<p>Tests whether the distribution of fills across grid levels matches the theoretical pattern of a random walk, or reveals mean-reverting or momentum market structure. This is the most important test for grid trading strategy validation.</p>

<div class="hyp">
<b>H<sub>0</sub>:</b> Fill distribution follows a random walk pattern (harmonic distribution across grid levels)<br>
<b>H<sub>a</sub>:</b> Fill distribution departs from random walk (mean-reverting or momentum structure)
</div>

<h4>Theoretical Background: The Harmonic Distribution</h4>
<p>Under a <b>symmetric random walk</b>, the probability that a particle (price) reaches distance <i>k</i> steps from the origin before returning follows the <b>harmonic distribution</b>:</p>
<div class="formula">
P(fill at level <i>k</i>) = (1/<i>k</i>) / <i>H<sub>m</sub></i>
<div class="explain">
where <i>H<sub>m</sub></i> = &#8721;<sub><i>i</i>=1</sub><sup><i>m</i></sup> (1/<i>i</i>) is the <i>m</i>-th harmonic number, and <i>m</i> = number of grid levels.<br><br>
<b>Intuition:</b> A random walk visits nearby levels much more frequently than distant ones. Level 1 (closest to center) is visited ~2x as often as level 2, ~3x as often as level 3, etc. This 1/<i>k</i> decay is a fundamental property of symmetric random walks.
</div>
</div>

<h4>Expected Frequencies</h4>
<div class="formula">
<i>E<sub>k</sub></i> = <i>n</i> &#183; (1/<i>k</i>) / <i>H<sub>m</sub></i>
<div class="explain">
where <i>n</i> = total observed fills. Each fill is classified by its grid level distance:<br>
<i>k</i> = round(|fill_price - center_price| / grid_spacing), clamped to [1, max_levels]
</div>
</div>

<h4>Test Statistic</h4>
<div class="formula">
&#967;&#178; = &#8721;<sub><i>k</i></sub> (<i>O<sub>k</sub></i> - <i>E<sub>k</sub></i>)&#178; / <i>E<sub>k</sub></i>
<div class="explain">
where <i>O<sub>k</sub></i> = observed fills at level <i>k</i>, <i>E<sub>k</sub></i> = expected fills under H<sub>0</sub>.<br><br>
<b>Cochran's rule:</b> Adjacent bins are merged when <i>E<sub>k</sub></i> &lt; 5 to ensure the &#967;&#178; approximation is valid. This is standard practice -- the chi-squared distribution is a poor approximation when expected counts are very small.<br><br>
df = (number of bins after merging) - 1
</div>
</div>

<p><b>Distribution under H<sub>0</sub>:</b> &#967;&#178; follows the <b>chi-squared distribution</b> with df degrees of freedom. The <i>p</i>-value = P(&#967;&#178;<sub>df</sub> &gt; observed &#967;&#178;), computed via the regularized lower incomplete gamma function:</p>
<div class="formula">
<i>p</i> = 1 - P(<i>a</i>, <i>x</i>) &emsp; where &emsp; <i>a</i> = df/2, &ensp; <i>x</i> = &#967;&#178;/2
<div class="explain">
P(<i>a</i>, <i>x</i>) = &#947;(<i>a</i>, <i>x</i>) / &#915;(<i>a</i>) is the lower regularized incomplete gamma function, evaluated using series expansion (for <i>x</i> &lt; <i>a</i> + 1) or continued fraction (otherwise).
</div>
</div>

<h4>Direction Indicator: Inner Excess</h4>
<p>When H<sub>0</sub> is rejected, we determine <i>which way</i> the distribution departs from random walk by computing <b>inner excess</b>:</p>
<div class="formula">
inner_excess = &#8721;<sub><i>k</i> &#8804; <i>m</i>/3</sub> (<i>O<sub>k</sub></i> - <i>E<sub>k</sub></i>)
<div class="explain">
Sums the deviations from expected for the innermost third of grid levels. Positive inner excess means inner levels are over-represented relative to the random walk prediction.
</div>
</div>

<table class="vtable">
<tr><th>Result</th><th>inner_excess</th><th>Market Interpretation</th></tr>
<tr><td class="vg">MEAN REVERTING</td><td>&gt; 0 (inner levels over-represented)</td><td>Price tends to return to center after small excursions. Fills cluster near the grid center where round trips complete quickly. <b>Grid strategy has a natural statistical edge.</b></td></tr>
<tr><td class="vr">MOMENTUM</td><td>&lt; 0 (outer levels over-represented)</td><td>Price tends to continue moving away from center. Fills happen at the extremes, leaving inner orders stranded. <b>Grid strategy is structurally disadvantaged.</b></td></tr>
<tr><td class="vy">RANDOM WALK</td><td><i>p</i> &#8805; 0.05</td><td>Fail to reject H<sub>0</sub>. Fill distribution is consistent with a random walk. No detectable edge or disadvantage for the grid.</td></tr>
</table>

<h4>Why This Is the Most Important Test</h4>
<p>This answers the <b>fundamental question</b> for grid trading: does the market mean-revert or trend?</p>
<p>Grid bots are inherently <b>mean-reversion strategies</b>. They profit when price oscillates through buy and sell levels, completing round trips. A mean-reverting market is ideal (fills cluster near center, trips complete quickly). A momentum market is the worst case (price moves directionally, accumulating losing positions on one side without completing the offsetting trade).</p>
<p>This test directly measures the fill-level distribution shape from the bot's own trade data -- the most granular evidence of market microstructure available without external data feeds.</p>

<h4>Data Requirements</h4>
<p><b>Minimum:</b> &#8805; 10 fills across &#8805; 3 distinct grid levels. The level requirement ensures enough distributional structure to test the shape. With fewer than 3 levels, the &#967;&#178; test has 0 degrees of freedom and is undefined.</p>
<p>After Cochran's bin merging, at least 2 bins must remain. This is the <b>hardest analyzer to satisfy</b> because it needs both <i>volume</i> (&#8805; 10 fills) and <i>diversity</i> (fills at multiple price levels, requiring price to oscillate across the grid).</p>
`;

MODAL.volatility_targets = `
<h3>Volatility vs Targets</h3>
<h4>Method: OHLC Candle Range Analysis</h4>
<p>Pair mode replacement for Grid Exceedance. Instead of asking "did price escape the grid?", this asks: <b>"Is current volatility right for my entry and exit targets?"</b></p>

<h4>Setup</h4>
<p>Using Kraken's 5-minute OHLC candles, the bot measures each candle's price range:</p>
<div class="formula">
candle_range% = (high - low) / low &times; 100
<div class="explain">
This captures the intra-candle price swing as a percentage. The <b>median</b> candle range is compared to the pair strategy's two key parameters:<br><br>
&emsp;&bull; <b>PAIR_ENTRY_PCT</b> -- how far from market the entry order sits<br>
&emsp;&bull; <b>PAIR_PROFIT_PCT</b> -- the profit target distance from entry fill
</div>
</div>

<h4>Reachability Metrics</h4>
<div class="formula">
entry_reachable% = (candles with range &ge; PAIR_ENTRY_PCT) / total &times; 100<br><br>
exit_reachable% = (candles with range &ge; PAIR_PROFIT_PCT) / total &times; 100
<div class="explain">
Entry reachability tells you what fraction of 5-minute windows have enough price movement to potentially fill your entry order. Exit reachability shows how often price swings are large enough to complete a full round trip within a single candle.
</div>
</div>

<h4>Verdict Logic</h4>
<table class="vtable">
<tr><th>Verdict</th><th>Condition</th><th>Meaning</th></tr>
<tr><td class="vr">LOW VOLATILITY</td><td>median range &lt; PAIR_ENTRY_PCT</td><td>Most candles don't swing enough to reach your entry orders. Entries will fill rarely. Consider tightening entry distance.</td></tr>
<tr><td class="vg">WELL TUNED</td><td>PAIR_ENTRY_PCT &le; median &le; PAIR_PROFIT_PCT</td><td>Volatility sits between your entry and exit distances. Entries fill regularly; exits require accumulation of several candle swings -- ideal for pair mode.</td></tr>
<tr><td class="vr">HIGH VOLATILITY</td><td>median range &gt; PAIR_PROFIT_PCT</td><td>Typical candle swings exceed your profit target. Price may fill your entry and then blow past your exit in the wrong direction before it triggers. Consider widening profit target or pausing.</td></tr>
</table>

<h4>Why Not a Hypothesis Test?</h4>
<p>Like Grid Exceedance in grid mode, this is a <b>descriptive metric</b> rather than a formal hypothesis test. The candle range directly measures the market characteristic we care about (volatility relative to our targets), and the thresholds are the actual strategy parameters -- no statistical abstraction needed.</p>

<h4>Data Requirements</h4>
<p><b>No fills needed.</b> Uses OHLC candle data from Kraken's public API. Produces results within ~60 seconds of bot start. Examines up to 720 candles (60 hours of 5-minute data).</p>
`;

MODAL.round_trip_duration = `
<h3>Round Trip Duration</h3>
<h4>Method: Duration Descriptive Statistics</h4>
<p>Pair mode replacement for the Random Walk chi-squared test. Measures the time between an entry fill and its corresponding exit fill for completed round trips.</p>

<h4>Setup</h4>
<p>In pair mode, each round trip consists of:</p>
<ul>
<li><b>Entry fill</b> (profit = 0): a buy or sell entry order fills, establishing a position</li>
<li><b>Exit fill</b> (profit &ne; 0): the corresponding profit-target order fills, completing the cycle</li>
</ul>

<div class="formula">
duration = exit_fill_time - entry_fill_time
<div class="explain">
The bot walks through fills chronologically, pairing each entry (profit = 0) with the next exit (profit &ne; 0) to measure the round-trip duration. This reflects the actual time capital is locked in a position.
</div>
</div>

<h4>Statistics Reported</h4>
<div class="formula">
median, mean, min, max duration (in minutes)
<div class="explain">
The <b>median</b> is used for the verdict because it's robust to outliers. A single slow round trip (e.g., price moved away and took hours to return) doesn't skew the assessment of typical behavior.
</div>
</div>

<h4>Verdict Logic</h4>
<table class="vtable">
<tr><th>Verdict</th><th>Condition</th><th>Meaning</th></tr>
<tr><td class="vg">FAST</td><td>median &lt; 5 min</td><td>Round trips completing quickly. High fill activity, capital turning over rapidly -- ideal for pair mode.</td></tr>
<tr><td class="vy">NORMAL</td><td>5 min &le; median &le; 60 min</td><td>Expected pace for pair mode. Entries and exits filling at a reasonable rate.</td></tr>
<tr><td class="vr">SLOW</td><td>median &gt; 60 min</td><td>Positions are staying open too long. Consider tightening entry distance (more fills) or reducing profit target (faster exits).</td></tr>
</table>

<h4>Why Replace Random Walk?</h4>
<p>The chi-squared random walk test bins fills by grid level distance. In pair mode, there are only 1-2 price levels (entry and exit), so the test always returns "insufficient data" (needs &ge; 3 distinct levels). Round trip duration directly measures what pair mode cares about: <b>how fast is capital cycling?</b></p>

<h4>Data Requirements</h4>
<p><b>Minimum:</b> 3 completed round trips. This is the same threshold as the profitability t-test. With fewer than 3, the median is unreliable and the analyzer reports "insufficient data".</p>
`;

MODAL.timeline = `
<h3>Data Sufficiency Timeline & Methodology</h3>
<p>The Statistical Advisory Board runs 5 independent analyzers, each using a different inferential technique from your AP Statistics toolkit. Each activates when it has enough data to produce meaningful results.</p>

<h4>Expected Timeline</h4>
<p>With 30-second polling and typical DOGE/USD volatility:</p>

<div class="timeline">
<div class="tl-item">
<div class="tl-time">~0 min (immediate)</div>
<div class="tl-text"><b>Grid Exceedance (Hidden Risk)</b> -- Proportion analysis of OHLC candle data from Kraken's public API. No fills needed. Begins analyzing within 60 seconds of bot start. Examines up to 720 five-minute candles (60 hours of data).</div>
</div>
<div class="tl-item">
<div class="tl-time">~15-30 min</div>
<div class="tl-text"><b>Fill Asymmetry</b> -- Exact binomial test. Needs &#8805; 5 fills in the 12h window. In dry run, simulated fills occur each time price crosses a grid level. One oscillation through the grid can generate several fills, but significance at <i>n</i> = 5 requires an extreme split (5/0).</div>
</div>
<div class="tl-item">
<div class="tl-time">~15-30 min</div>
<div class="tl-text"><b>Profitability</b> -- One-sample <i>t</i>-test. Needs &#8805; 3 completed round trips (buy-then-sell cycles). Will likely show "not significant" initially because the confidence interval is wide when <i>n</i> is small and <i>s</i>/&#8730;<i>n</i> is large.</div>
</div>
<div class="tl-item">
<div class="tl-time">~1-2 hours</div>
<div class="tl-text"><b>Fill Rate (Volatility Regime)</b> -- Poisson <i>z</i>-test. Needs &#8805; 5 fills AND &gt; 1 hour of history. Requires a baseline period (all fills before the last hour) to compute &#955;<sub>baseline</sub>, so the bot must accumulate enough pre-history.</div>
</div>
<div class="tl-item">
<div class="tl-time">~2-4 hours</div>
<div class="tl-text"><b>Random Walk (Market Type)</b> -- &#967;&#178; goodness-of-fit test. Needs &#8805; 10 fills across &#8805; 3 grid levels with expected bins &#8805; 5 after merging. This is the hardest to satisfy because it requires price to oscillate widely enough to generate fills at multiple distinct levels.</div>
</div>
<div class="tl-item">
<div class="tl-time">~4-8 hours</div>
<div class="tl-text"><b>Profitability reaches significance.</b> With <i>n</i> &#8776; 10-20 round trips, the standard error <i>s</i>/&#8730;<i>n</i> shrinks enough that the 95% CI typically excludes zero (given grid spacing &gt; round-trip fees). The exact timing depends on the effect size |<i>x&#772;</i>|/<i>s</i>.</div>
</div>
<div class="tl-item">
<div class="tl-time">~12-24 hours</div>
<div class="tl-text"><b>High-confidence results across all analyzers.</b> Fill asymmetry and fill rate have stable baselines. Random walk test has enough fills for reliable &#967;&#178; approximation. The overall health verdict becomes trustworthy.</div>
</div>
</div>

<h4>The Five Tests at a Glance</h4>
<table class="vtable">
<tr><th>Analyzer</th><th>Statistical Method</th><th>AP Stats Topic</th><th>Minimum Data</th></tr>
<tr><td>Profitability</td><td>One-sample <i>t</i>-test</td><td>Inference for means</td><td>3 round trips</td></tr>
<tr><td>Fill Asymmetry</td><td>Exact binomial test</td><td>Inference for proportions</td><td>5 fills in 12h</td></tr>
<tr><td>Grid: Hidden Risk<br>Pair: Volatility vs Targets</td><td>Grid: OHLC boundary proportion<br>Pair: candle range vs targets</td><td>Descriptive statistics</td><td>OHLC candles (no fills)</td></tr>
<tr><td>Fill Rate</td><td>Poisson <i>z</i>-test</td><td>Sampling distributions</td><td>5 fills + 1hr history</td></tr>
<tr><td>Grid: Market Type<br>Pair: Round Trip Duration</td><td>Grid: &#967;&#178; goodness-of-fit<br>Pair: duration descriptives</td><td>Grid: Chi-squared tests<br>Pair: Descriptive stats</td><td>Grid: 10 fills across 3+ levels<br>Pair: 3 round trips</td></tr>
</table>

<h4>Overall Health Verdict Logic</h4>
<p>The banner verdict is derived from all 5 analyzers with this priority ordering (red conditions override green ones). In <b>pair mode</b>, the grid-specific analyzers are swapped for pair-specific ones:</p>
<table class="vtable">
<tr><th>Verdict</th><th>Color</th><th>Grid Mode Trigger</th><th>Pair Mode Trigger</th></tr>
<tr><td class="vr">DANGEROUS</td><td>Red</td><td>High vol + trending</td><td>High vol + trending</td></tr>
<tr><td class="vr">UNFAVORABLE</td><td>Red</td><td>Random walk: momentum</td><td>Round trips slow OR low volatility</td></tr>
<tr><td class="vr">EXPOSED</td><td>Red</td><td>Grid exceedance &gt; 20%</td><td>Volatility &gt; profit target</td></tr>
<tr><td class="vg">FAVORABLE</td><td>Green</td><td>Random walk: mean-reverting</td><td>Fast round trips OR well-tuned volatility</td></tr>
<tr><td class="vg">PROFITABLE</td><td>Green</td><td colspan="2">Profitability <i>t</i>-test is statistically significant</td></tr>
<tr><td class="vy">CALIBRATING</td><td>Yellow</td><td colspan="2">All analyzers still have insufficient data</td></tr>
<tr><td class="vy">NEUTRAL</td><td>Yellow</td><td colspan="2">Mixed or inconclusive signals</td></tr>
</table>
<p>Red takes priority over green: even if the <i>t</i>-test confirms significant profits, a structural risk detection overrides because it warns of conditions that past profits may not predict.</p>

<h4>Implementation Notes</h4>
<ul>
<li>All distribution functions (Student's <i>t</i>, &#967;&#178;, binomial via incomplete beta) are implemented in pure Python with no external dependencies. The normal CDF uses the Abramowitz &amp; Stegun rational approximation (~10<sup>-7</sup> accuracy). The incomplete beta uses Lentz's continued fraction. The incomplete gamma uses series expansion or continued fraction depending on the regime.</li>
<li>The stats engine runs every 60 seconds in the main bot loop. OHLC candle data is fetched from Kraken every 5 minutes.</li>
<li>All tests use &#945; = 0.05 (two-tailed) as the significance threshold, consistent with AP Statistics conventions.</li>
</ul>
`;

function openModal(name) {
  const content = MODAL[name];
  if (!content) return;
  document.getElementById('modal-content').innerHTML = content;
  document.getElementById('modal-overlay').classList.add('active');
  document.body.style.overflow = 'hidden';
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
  document.body.style.overflow = '';
}

document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeModal(); });

// === Advisory board rendering ===

function renderAdvisory(stats) {
  const board = document.getElementById('advisory');
  if (!stats || Object.keys(stats).length === 0) {
    board.innerHTML = '<div class="acard" onclick="openModal(\'timeline\')"><div class="ac-info">&#9432; details</div><div class="ac-name">STATS ENGINE</div><div class="ac-summary">Collecting data -- analyzers will appear as fills accumulate. Click any card for methodology.</div></div>';
    return;
  }
  // Health banner
  const health = stats.overall_health || {};
  const hb = document.getElementById('health-banner');
  hb.className = 'health-banner hb-' + (health.color || 'yellow');
  document.getElementById('hb-verdict').textContent = (health.verdict || 'calibrating').toUpperCase().replace(/_/g, ' ');
  document.getElementById('hb-summary').textContent = health.summary || '';

  // Cards (clickable with info hint)
  let html = '';
  const isPairMode = stats.volatility_targets || stats.round_trip_duration;
  const order = isPairMode
    ? ['profitability', 'fill_asymmetry', 'volatility_targets', 'fill_rate', 'round_trip_duration']
    : ['profitability', 'fill_asymmetry', 'grid_exceedance', 'fill_rate', 'random_walk'];
  for (const name of order) {
    const r = stats[name];
    if (!r) continue;
    const color = r.color || 'yellow';
    html += '<div class="acard ac-' + color + '" onclick="openModal(\'' + name + '\')">';
    html += '<div class="ac-info">&#9432; details</div>';
    html += '<div class="ac-name">' + (ANALYZER_LABELS[name] || name) + '</div>';
    html += '<div class="ac-verdict v-' + color + '">' + (r.verdict || '').replace(/_/g, ' ').toUpperCase() + '</div>';
    html += '<div class="ac-summary">' + (r.summary || '') + '</div>';
    html += '<div class="ac-conf">Confidence: ' + (r.confidence || 'none') + '</div>';
    html += '</div>';
  }
  board.innerHTML = html;
}

// === Main update ===

function update(data) {
  // Badge
  const badge = document.getElementById('badge');
  if (data.paused) {
    badge.textContent = 'PAUSED'; badge.className = 'badge badge-paused';
  } else if (data.mode === 'dry_run') {
    badge.textContent = 'DRY RUN'; badge.className = 'badge badge-dry';
  } else {
    badge.textContent = 'LIVE'; badge.className = 'badge badge-live';
  }
  // Only set base once, or resync if bot restarted (uptime dropped)
  if (!_lastServerUptimeAt || data.uptime < _lastServerUptime) {
    _lastServerUptime = data.uptime;
    _lastServerUptimeAt = Date.now();
    document.getElementById('uptime').textContent = 'Up ' + fmtUptime(data.uptime);
  }

  // Pair mode detection
  const cfg = data.config;
  const isPair = cfg.strategy_mode === 'pair';

  // Title + section headers (dynamic from pair config)
  const base = (cfg.pair_display || 'DOGE/USD').split('/')[0];
  document.getElementById('bot-title').textContent = isPair ? base + ' Pair Bot' : base + ' Grid Bot';
  document.title = isPair ? base + ' Pair Bot' : base + ' Grid Bot';
  document.getElementById('ladder-title').textContent = isPair ? 'Active Orders' : 'Grid Ladder';

  // Cards
  const p = data.price;
  document.getElementById('c-price').textContent = '$' + fmt(p.current, 6);
  document.getElementById('c-price-sub').textContent = 'drift ' + fmt(p.drift_pct, 2) + '%';
  document.getElementById('c-center').textContent = '$' + fmt(p.center, 6);
  document.getElementById('c-center-sub').textContent = isPair ? 'center' : 'grid center';
  const pr = data.profit;
  document.getElementById('c-today').textContent = fmtUSD(pr.today);
  document.getElementById('c-today-sub').textContent = pr.round_trips_today + ' trips today';
  document.getElementById('c-total').textContent = fmtUSD(pr.total);
  document.getElementById('c-total-sub').textContent = 'fees: ' + fmtUSD(pr.fees);
  document.getElementById('c-trips').textContent = pr.round_trips;
  document.getElementById('c-trips-sub').textContent = 'lifetime';
  document.getElementById('c-doge').textContent = fmt(pr.doge_accumulated, 2);
  document.getElementById('c-doge-label').textContent = base + ' Accumulated';

  // Charts
  if (data.charts) {
    drawSparkline('chart-price', data.charts.price, '#58a6ff');
    drawBars('chart-fills', data.charts.fill_rate, function(v) { return v > 0 ? '#58a6ff' : '#21262d'; });
    drawProfitDots('chart-profits', data.charts.profits);
  }

  // Pair state machine + Trade A/B panels
  const psBar = document.getElementById('pair-state-bar');
  const ppDiv = document.getElementById('pair-panels');
  const cycSec = document.getElementById('cycles-section');
  if (isPair) {
    // State machine banner
    const ps = data.pair_state || 'S0';
    const psDesc = {S0:'Both entries open',S1a:'Trade A entered (exit pending)',S1b:'Trade B entered (exit pending)',S2:'Both entered (both exits pending)'}[ps] || ps;
    psBar.style.display = 'block';
    psBar.className = 'pair-state-bar ps-' + ps;
    document.getElementById('ps-state').textContent = ps;
    document.getElementById('ps-desc').textContent = psDesc + (data.long_only ? ' (LONG ONLY)' : '');

    // Trend indicator
    const trendEl = document.getElementById('ps-trend');
    const trend = data.detected_trend;
    if (trend) {
      trendEl.style.display = '';
      const arrow = trend === 'up' ? '\u2191' : '\u2193';
      trendEl.textContent = arrow + ' ' + trend.toUpperCase() + ' trend detected';
      trendEl.style.color = trend === 'up' ? '#3fb950' : '#f85149';
    } else {
      trendEl.style.display = 'none';
    }

    // S2 timer
    const s2Timer = document.getElementById('ps-s2-timer');
    if (ps === 'S2' && data.s2_age_minutes != null) {
      s2Timer.style.display = '';
      const ageM = data.s2_age_minutes;
      const th = data.exit_thresholds;
      const ageTxt = ageM < 60 ? fmt(ageM, 0) + 'm' : fmt(ageM / 60, 1) + 'h';
      let timerTxt = 'S2 for ' + ageTxt;
      if (th && th.orphan_min) {
        const remain = th.orphan_min - ageM;
        if (remain > 0) timerTxt += ' \u2014 break-glass in ' + fmt(remain, 0) + 'm';
        else timerTxt += ' \u2014 break-glass active';
      }
      s2Timer.textContent = timerTxt;
    } else {
      s2Timer.style.display = 'none';
    }

    // Trade A/B panels
    ppDiv.style.display = 'grid';
    const cycles = data.completed_cycles || [];
    const aCyc = cycles.filter(c => c.trade_id === 'A');
    const bCyc = cycles.filter(c => c.trade_id === 'B');
    const aNet = aCyc.reduce((s,c) => s + c.net_profit, 0);
    const bNet = bCyc.reduce((s,c) => s + c.net_profit, 0);
    const aAvg = aCyc.length > 0 ? aNet / aCyc.length : 0;
    const bAvg = bCyc.length > 0 ? bNet / bCyc.length : 0;
    // Derive per-trade details from open orders
    const openOrders = data.grid.filter(o => o.status === 'open');
    const aOrders = openOrders.filter(o => o.trade_id === 'A');
    const bOrders = openOrders.filter(o => o.trade_id === 'B');
    // Trade A panel: current leg, entry price, target, distance, est P&L
    const aOrder = aOrders.length > 0 ? aOrders[0] : null;
    const aLeg = aOrder ? (aOrder.role || aOrder.side) : 'idle';
    const aEntry = aOrder && aOrder.matched_sell_price ? aOrder.matched_sell_price : null;
    const aTarget = aOrder ? aOrder.price : null;
    const aDist = (aTarget && p.current > 0) ? ((aTarget - p.current) / p.current * 100) : null;
    const aEst = (aEntry && aTarget && aOrder.volume) ? (aEntry - aTarget) * aOrder.volume : null;

    document.getElementById('pp-a-cycle').textContent = '#' + (data.cycle_a || 1);
    document.getElementById('pp-a-leg').textContent = aLeg;
    // Exit age badge for Trade A
    const aLegEl = document.getElementById('pp-a-leg');
    const aExitAge = _exitAgeBadge(aOrder, data);
    const oldBadgeA = aLegEl.parentElement.querySelector('.exit-age-badge');
    if (oldBadgeA) oldBadgeA.remove();
    if (aExitAge) aLegEl.parentElement.appendChild(aExitAge);
    document.getElementById('pp-a-entry').textContent = aEntry ? '$' + fmt(aEntry, 6) : '--';
    document.getElementById('pp-a-target').textContent = aTarget ? '$' + fmt(aTarget, 6) : '--';
    document.getElementById('pp-a-dist').textContent = aDist != null ? fmt(aDist, 2) + '%' : '--';
    document.getElementById('pp-a-est').textContent = aEst != null ? fmtUSD(aEst) : '--';
    document.getElementById('pp-a-completed').textContent = aCyc.length + ' cycles';
    document.getElementById('pp-a-pnl').textContent = fmtUSD(aNet);
    document.getElementById('pp-a-pnl').style.color = aNet >= 0 ? '#3fb950' : '#f85149';
    document.getElementById('pp-a-avg').textContent = fmtUSD(aAvg);

    // Trade B panel
    const bOrder = bOrders.length > 0 ? bOrders[0] : null;
    const bLeg = bOrder ? (bOrder.role || bOrder.side) : 'idle';
    const bEntry = bOrder && bOrder.matched_buy_price ? bOrder.matched_buy_price : null;
    const bTarget = bOrder ? bOrder.price : null;
    const bDist = (bTarget && p.current > 0) ? ((bTarget - p.current) / p.current * 100) : null;
    const bEst = (bEntry && bTarget && bOrder.volume) ? (bTarget - bEntry) * bOrder.volume : null;

    document.getElementById('pp-b-cycle').textContent = '#' + (data.cycle_b || 1);
    document.getElementById('pp-b-leg').textContent = bLeg;
    // Exit age badge for Trade B
    const bLegEl = document.getElementById('pp-b-leg');
    const bExitAge = _exitAgeBadge(bOrder, data);
    const oldBadgeB = bLegEl.parentElement.querySelector('.exit-age-badge');
    if (oldBadgeB) oldBadgeB.remove();
    if (bExitAge) bLegEl.parentElement.appendChild(bExitAge);
    document.getElementById('pp-b-entry').textContent = bEntry ? '$' + fmt(bEntry, 6) : '--';
    document.getElementById('pp-b-target').textContent = bTarget ? '$' + fmt(bTarget, 6) : '--';
    document.getElementById('pp-b-dist').textContent = bDist != null ? fmt(bDist, 2) + '%' : '--';
    document.getElementById('pp-b-est').textContent = bEst != null ? fmtUSD(bEst) : '--';
    document.getElementById('pp-b-completed').textContent = bCyc.length + ' cycles';
    document.getElementById('pp-b-pnl').textContent = fmtUSD(bNet);
    document.getElementById('pp-b-pnl').style.color = bNet >= 0 ? '#3fb950' : '#f85149';
    document.getElementById('pp-b-avg').textContent = fmtUSD(bAvg);

    // Recovery orders panel
    const recPanel = document.getElementById('recovery-panel');
    const recOrders = data.recovery_orders || [];
    const recStats = data.recovery_stats || {};
    if (recOrders.length > 0) {
      recPanel.style.display = 'block';
      document.getElementById('recovery-title').textContent =
        'Recovery Orders (' + recStats.active_count + '/' + recStats.max_slots + ' slots)';
      const statsText = (recStats.total_wins ? 'Recovered: ' + fmtUSD(recStats.total_wins) + '  ' : '') +
                         (recStats.total_losses ? 'Evicted: ' + recStats.total_losses : '');
      document.getElementById('recovery-stats').textContent = statsText;
      let rrows = '';
      for (const r of recOrders) {
        const pnlColor = r.unrealized_pnl >= 0 ? '#3fb950' : '#f85149';
        const ageTxt = r.age_minutes < 60 ? fmt(r.age_minutes,0) + 'm' : fmt(r.age_minutes/60,1) + 'h';
        rrows += '<tr>';
        rrows += '<td>' + r.trade_id + '.' + r.cycle + '</td>';
        rrows += '<td>' + r.side.toUpperCase() + '</td>';
        rrows += '<td>$' + fmt(r.price, 6) + '</td>';
        rrows += '<td>$' + fmt(r.entry_price, 6) + '</td>';
        rrows += '<td>' + ageTxt + '</td>';
        rrows += '<td style="color:' + pnlColor + '">' + fmtUSD(r.unrealized_pnl) + '</td>';
        rrows += '</tr>';
      }
      document.getElementById('recovery-body').innerHTML = rrows;
    } else {
      recPanel.style.display = 'none';
    }

    // Unrealized P&L card
    const uCard = document.getElementById('c-unreal-card');
    const upnl = data.unrealized_pnl;
    if (upnl) {
      uCard.style.display = '';
      const tot = upnl.total_unrealized || 0;
      document.getElementById('c-unreal').textContent = fmtUSD(tot);
      document.getElementById('c-unreal').style.color = tot >= 0 ? '#3fb950' : '#f85149';
      document.getElementById('c-unreal-a').textContent = 'A: ' + fmtUSD(upnl.a_unrealized || 0);
      document.getElementById('c-unreal-b').textContent = 'B: ' + fmtUSD(upnl.b_unrealized || 0);
    } else {
      uCard.style.display = 'none';
    }

    // Pair stats panel
    const psPanel = document.getElementById('pair-stats-panel');
    const pst = data.pair_stats;
    if (pst) {
      psPanel.style.display = '';
      function psRows(colId, stats) {
        const col = document.getElementById(colId);
        // Keep header, replace rows
        const head = col.querySelector('.ps-col-head').outerHTML;
        let h = head;
        const rows = [
          ['Cycles', (stats.n != null ? stats.n : (stats.n_total != null ? stats.n_total : '\u2014'))],
          ['Mean P&L', fmtStatUSD(stats.mean_net)],
          ['Win rate', fmtPct(stats.win_rate)],
          ['Profit factor', fmtStatNum(stats.profit_factor, 2)],
          ['Max drawdown', fmtStatUSD(stats.max_drawdown)],
          ['95% CI', (stats.ci_95_lower != null && stats.ci_95_upper != null) ? fmtUSD(stats.ci_95_lower) + ' .. ' + fmtUSD(stats.ci_95_upper) : '\u2014'],
          ['Avg duration', fmtDuration(stats.mean_duration_sec)],
          ['Fill rate', fmtPct(stats.fill_rate)],
        ];
        for (const [k,v] of rows) {
          h += '<div class="ps-row"><span class="ps-k">' + k + '</span><span class="ps-v">' + v + '</span></div>';
        }
        col.innerHTML = h;
      }
      // Build per-trade stats from completed cycles
      const aStats = {n: aCyc.length, mean_net: aAvg, win_rate: aCyc.length > 0 ? aCyc.filter(c=>c.net_profit>0).length/aCyc.length*100 : null,
        profit_factor: null, max_drawdown: null, ci_95_lower: null, ci_95_upper: null, mean_duration_sec: null, fill_rate: null};
      const bStats = {n: bCyc.length, mean_net: bAvg, win_rate: bCyc.length > 0 ? bCyc.filter(c=>c.net_profit>0).length/bCyc.length*100 : null,
        profit_factor: null, max_drawdown: null, ci_95_lower: null, ci_95_upper: null, mean_duration_sec: null, fill_rate: null};
      // Compute per-trade durations
      if (aCyc.length > 0) {
        const aDurs = aCyc.filter(c=>c.entry_time&&c.exit_time).map(c=>c.exit_time-c.entry_time);
        if (aDurs.length > 0) aStats.mean_duration_sec = aDurs.reduce((a,b)=>a+b,0)/aDurs.length;
      }
      if (bCyc.length > 0) {
        const bDurs = bCyc.filter(c=>c.entry_time&&c.exit_time).map(c=>c.exit_time-c.entry_time);
        if (bDurs.length > 0) bStats.mean_duration_sec = bDurs.reduce((a,b)=>a+b,0)/bDurs.length;
      }
      psRows('ps-col-a', aStats);
      psRows('ps-col-b', bStats);
      // Combined stats: normalize win_rate from 0-1 to 0-100 for display
      const pstDisplay = Object.assign({}, pst, {n: pst.n_total, win_rate: pst.win_rate != null ? pst.win_rate * 100 : null});
      psRows('ps-col-c', pstDisplay);
    } else {
      psPanel.style.display = 'none';
    }

    // AI council panel
    const aiPanel = document.getElementById('ai-council-panel');
    const aic = data.ai_council;
    if (aic && aic.action) {
      aiPanel.style.display = '';
      const vLabel = document.getElementById('ai-verdict');
      vLabel.textContent = (aic.action || '').toUpperCase();
      const aCls = (aic.action || '').toLowerCase().replace(/_.*/, '');
      vLabel.className = 'ai-verdict-label v-' + aCls;
      // Panelists
      const pDiv = document.getElementById('ai-panelists');
      let phtml = '';
      const votes = aic.panel_votes || aic.votes || [];
      for (const v of votes) {
        const cond = v.condition || '';
        const isSkip = cond === 'skipped' || cond === 'error' || cond === 'timeout';
        const vCls = isSkip ? 'ap-skip' : 'ap-' + (v.action || 'hold').toLowerCase().replace(/_.*/, '');
        phtml += '<div class="ai-panelist ' + vCls + '">';
        phtml += '<div class="ap-name">' + (v.name || v.model || 'panelist') + '</div>';
        phtml += '<div class="ap-vote">' + (isSkip ? cond.toUpperCase() : (v.action || '--').toUpperCase()) + '</div>';
        phtml += '<div class="ap-reason">' + (v.reason || '') + '</div>';
        phtml += '</div>';
      }
      pDiv.innerHTML = phtml;
    } else {
      aiPanel.style.display = 'none';
    }

    // Completed cycles table (last 20, with duration, color-coded P&L)
    cycSec.style.display = '';
    const cb = document.getElementById('cycles-body');
    let crows = '';
    const sortedCycles = cycles.slice().reverse();
    for (const c of sortedCycles.slice(0, 20)) {
      const tag = c.trade_id === 'A' ? 'trade-a-tag' : 'trade-b-tag';
      const pcls = c.net_profit > 0 ? 'profit-pos' : (c.net_profit < 0 ? 'profit-neg' : '');
      const dur = (c.entry_time && c.exit_time) ? fmtDuration(c.exit_time - c.entry_time) : '--';
      crows += '<tr><td>' + fmtTime(c.exit_time) + '</td>'
             + '<td class="' + tag + '">' + c.trade_id + '</td>'
             + '<td>' + c.cycle + '</td>'
             + '<td>$' + fmt(c.entry_price, 6) + '</td>'
             + '<td>$' + fmt(c.exit_price, 6) + '</td>'
             + '<td>' + fmt(c.volume, 2) + '</td>'
             + '<td>' + dur + '</td>'
             + '<td class="' + pcls + '">' + fmtUSD(c.net_profit) + '</td></tr>';
    }
    cb.innerHTML = crows || '<tr><td colspan="8" style="text-align:center;color:#8b949e">No completed cycles yet</td></tr>';
  } else {
    psBar.style.display = 'none';
    ppDiv.style.display = 'none';
    cycSec.style.display = 'none';
    document.getElementById('c-unreal-card').style.display = 'none';
    document.getElementById('pair-stats-panel').style.display = 'none';
    document.getElementById('ai-council-panel').style.display = 'none';
  }

  // Stats advisory board
  renderAdvisory(data.stats);

  // Grid ladder
  const lhead = document.getElementById('ladder-head');
  lhead.innerHTML = isPair
    ? '<th>Role</th><th>Side</th><th>Price</th><th>Volume</th><th>Status</th>'
    : '<th>Lvl</th><th>Side</th><th>Price</th><th>Volume</th><th>Status</th>';
  const tbody = document.getElementById('ladder-body');
  let rows = '';
  const cp = p.current;
  let markerPlaced = false;
  for (const o of data.grid) {
    if (!markerPlaced && o.price < cp) {
      rows += '<tr class="marker"><td></td><td></td><td>$' + fmt(cp, 6) + '</td><td>PRICE</td><td></td></tr>';
      markerPlaced = true;
    }
    const cls = o.side === 'buy' ? 'buy' : 'sell';
    let col1;
    if (isPair) {
      const tid = o.trade_id ? (o.trade_id + '.' + (o.cycle || 0)) : '';
      col1 = (o.role || '--') + (tid ? ' ' + tid : '');
    } else {
      col1 = (o.level > 0 ? '+' : '') + o.level;
    }
    rows += '<tr><td>' + col1 + '</td>'
          + '<td class="' + cls + '">' + o.side.toUpperCase() + '</td>'
          + '<td>$' + fmt(o.price, 6) + '</td>'
          + '<td>' + fmt(o.volume, 2) + '</td>'
          + '<td>' + o.status + '</td></tr>';
  }
  if (!markerPlaced && data.grid.length > 0) {
    rows += '<tr class="marker"><td></td><td></td><td>$' + fmt(cp, 6) + '</td><td>PRICE</td><td></td></tr>';
  }
  tbody.innerHTML = rows;

  // Trend bar (hidden in pair mode)
  const t = data.trend;
  document.getElementById('trend-section').style.display = isPair ? 'none' : '';
  if (!isPair) {
    const buyBar = document.getElementById('trend-buy');
    const sellBar = document.getElementById('trend-sell');
    buyBar.style.width = t.buy_pct + '%';
    buyBar.textContent = t.buy_pct + '% Buy (' + t.grid_buys + ')';
    sellBar.style.width = t.sell_pct + '%';
    sellBar.textContent = t.sell_pct + '% Sell (' + t.grid_sells + ')';
    document.getElementById('trend-source').textContent = 'Source: ' + t.source;
    document.getElementById('trend-fills').textContent = t.buy_12h + ' buys / ' + t.sell_12h + ' sells (12h)';
  }

  // Params
  document.getElementById('p-size').textContent = '$' + fmt(cfg.order_size, 2);
  document.getElementById('p-levels-row').style.display = isPair ? 'none' : '';
  if (!isPair) {
    document.getElementById('p-levels').textContent = cfg.grid_levels + ' per side (' + (cfg.grid_levels * 2) + ' total)';
  }
  document.getElementById('p-spacing-label').textContent = isPair ? 'Profit target' : 'Spacing';
  document.getElementById('p-spacing').textContent = fmt(cfg.spacing_pct, 2) + '%';
  document.getElementById('p-entry-row').style.display = isPair ? '' : 'none';
  document.getElementById('p-refresh-row').style.display = isPair ? '' : 'none';
  if (isPair) {
    document.getElementById('p-entry').textContent = fmt(cfg.pair_entry_pct, 2) + '%';
    document.getElementById('p-refresh').textContent = fmt(cfg.pair_refresh_pct, 2) + '%';
  }
  document.getElementById('p-capital').textContent = '$' + fmt(cfg.effective_capital, 2);
  document.getElementById('p-fees').textContent = fmt(cfg.round_trip_fee_pct, 2) + '%';
  document.getElementById('p-ai').textContent = 'manual (button or /check)';
  document.getElementById('p-airec').textContent = data.ai_recommendation;

  // Controls (pair mode adjustments)
  document.getElementById('ctrl-spacing-label').textContent = isPair ? 'Profit %' : 'Spacing %';
  document.getElementById('ctrl-ratio-row').style.display = isPair ? 'none' : '';
  document.getElementById('ctrl-entry-row').style.display = isPair ? '' : 'none';
  document.getElementById('ctrl-help').style.display = isPair ? '' : 'none';

  // Populate input placeholders
  const inS = document.getElementById('in-spacing');
  const inR = document.getElementById('in-ratio');
  const inE = document.getElementById('in-entry');
  if (!inS.value) inS.placeholder = fmt(cfg.spacing_pct, 2);
  if (!isPair && !inR.value) inR.placeholder = fmt(t.ratio, 2);
  if (isPair && !inE.value) inE.placeholder = fmt(cfg.pair_entry_pct, 2);
  inS.min = cfg.min_spacing;

  // Recent fills (last 20)
  const fb = document.getElementById('fills-body');
  let frows = '';
  const fills = data.recent_fills.slice(0, 20);
  for (const f of fills) {
    const cls = f.side === 'buy' ? 'buy' : 'sell';
    const pcls = f.profit > 0 ? 'profit-pos' : (f.profit < 0 ? 'profit-neg' : '');
    const leg = f.trade_id ? f.trade_id + '.' + f.cycle + ' ' + (f.order_role || '') : f.side.toUpperCase();
    frows += '<tr><td>' + fmtTime(f.time) + '</td>'
           + '<td class="' + cls + '">' + leg + '</td>'
           + '<td>$' + fmt(f.price, 6) + '</td>'
           + '<td>' + fmt(f.volume, 2) + '</td>'
           + '<td class="' + pcls + '">' + (f.profit ? fmtUSD(f.profit) : '--') + '</td></tr>';
  }
  fb.innerHTML = frows || '<tr><td colspan="5" style="text-align:center;color:#8b949e">No fills yet</td></tr>';

  // Check for audio alerts
  checkAlerts(data);
}

// === Controls ===

function showMsg(text, ok) {
  const el = document.getElementById('ctrl-msg');
  el.textContent = text;
  el.className = 'ctrl-msg ' + (ok ? 'ok' : 'err');
  setTimeout(() => { el.innerHTML = '&nbsp;'; el.className = 'ctrl-msg'; }, 5000);
}

async function postConfig(body) {
  try {
    const r = await fetch(CONFIG, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    if (r.ok) {
      showMsg('Queued: ' + (d.queued || []).join(', '), true);
      document.getElementById('in-spacing').value = '';
      document.getElementById('in-ratio').value = '';
    } else { showMsg(d.error || 'Error', false); }
  } catch(e) { showMsg('Network error', false); }
}

function applySpacing() { const v = parseFloat(document.getElementById('in-spacing').value); if (isNaN(v)) return showMsg('Enter a spacing value', false); postConfig({spacing: v}); }
function applyEntry() { const v = parseFloat(document.getElementById('in-entry').value); if (isNaN(v)) return showMsg('Enter an entry % value', false); postConfig({entry_pct: v}); document.getElementById('in-entry').value = ''; }
function applyRatio() { const v = parseFloat(document.getElementById('in-ratio').value); if (isNaN(v)) return showMsg('Enter a ratio value', false); postConfig({ratio: v}); }
function applyRatioAuto() { postConfig({ratio: 'auto'}); }
function triggerAICheck() { postConfig({ai_check: true}); }

// === Audio Alerts ===
let audioEnabled = localStorage.getItem('audioEnabled') === 'true';
let audioCtx = null;
let prevState = null;

function initAudioBtn() {
  const btn = document.getElementById('audio-btn');
  if (audioEnabled) { btn.innerHTML = '&#x1f50a;'; btn.classList.add('active'); }
  else { btn.innerHTML = '&#x1f507;'; btn.classList.remove('active'); }
}
initAudioBtn();

function toggleAudio() {
  audioEnabled = !audioEnabled;
  localStorage.setItem('audioEnabled', audioEnabled);
  if (audioEnabled && !audioCtx) {
    try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch(e) {}
  }
  initAudioBtn();
}

function beep(freq, duration, volume) {
  if (!audioEnabled || !audioCtx) return;
  try {
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.frequency.value = freq;
    gain.gain.value = volume || 0.3;
    osc.start();
    osc.stop(audioCtx.currentTime + (duration || 0.15));
  } catch(e) {}
}

function speak(text) {
  if (!audioEnabled || !window.speechSynthesis) return;
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.1;
    window.speechSynthesis.speak(u);
  } catch(e) {}
}

function checkAlerts(data) {
  if (!prevState) { prevState = data; return; }
  // New fill
  if (data.profit && prevState.profit && data.profit.round_trips > prevState.profit.round_trips) {
    beep(880, 0.15, 0.3);
    const fills = data.recent_fills;
    if (fills && fills.length > 0) {
      const f = fills[0];
      speak('Fill: ' + f.side + ' at ' + f.price.toFixed(4));
    }
  }
  // Verdict change
  if (data.stats && data.stats.overall_health && prevState.stats && prevState.stats.overall_health) {
    const nv = data.stats.overall_health.verdict || '';
    const ov = prevState.stats.overall_health.verdict || '';
    if (nv && nv !== ov) {
      beep(660, 0.2, 0.25);
      speak('Verdict changed to ' + nv.replace(/_/g, ' '));
    }
  }
  // Bot paused
  if (data.paused && !prevState.paused) {
    beep(220, 0.3, 0.4);
    setTimeout(function(){ beep(220, 0.3, 0.4); }, 400);
    speak('Warning: bot paused');
  }
  prevState = data;
}

// === SSE Live Feed ===
let evtSource = null;
function startSSE() {
  if (evtSource) { evtSource.close(); evtSource = null; }
  evtSource = new EventSource('/api/stream');
  evtSource.onmessage = function(e) {
    try { const data = JSON.parse(e.data); if (!data.error) update(data); } catch(ex) {}
  };
  evtSource.onerror = function() {
    evtSource.close(); evtSource = null;
    // fallback to polling
    setInterval(poll, 5000);
  };
}
async function poll() { try { const r = await fetch(API); if (r.ok) update(await r.json()); } catch(e) {} }
// === Swarm View ===
let swarmMode = false;
let swarmData = null;
let scannerData = null;
let scannerCacheTime = 0;
let scannerSortKey = 'score';
let scannerSortAsc = false;
let detailPair = null;

function isMultiPair() {
  // Determined from swarm status or configured_pairs
  return swarmData && swarmData.aggregate && swarmData.aggregate.active_pairs > 1;
}

function showSwarmView() {
  swarmMode = true;
  detailPair = null;
  document.getElementById('swarm-view').style.display = 'block';
  document.getElementById('detail-view').style.display = 'none';
  pollSwarm();
}

function showDetailView(pair) {
  swarmMode = false;
  detailPair = pair;
  document.getElementById('swarm-view').style.display = 'none';
  document.getElementById('detail-view').style.display = 'block';
  document.getElementById('btn-back').style.display = isMultiPair() ? 'inline-block' : 'none';
  // SSE only streams the first pair -- close it and poll the selected pair immediately
  if (evtSource) { evtSource.close(); evtSource = null; }
  poll();
}

function renderSwarm(data) {
  swarmData = data;
  const agg = data.aggregate;
  document.getElementById('sw-pairs').textContent = agg.active_pairs;
  const todayEl = document.getElementById('sw-today');
  todayEl.textContent = fmtUSD(agg.today_profit);
  todayEl.style.color = agg.today_profit >= 0 ? '#3fb950' : '#f85149';
  const totalEl = document.getElementById('sw-total');
  totalEl.textContent = fmtUSD(agg.total_profit);
  totalEl.style.color = agg.total_profit >= 0 ? '#3fb950' : '#f85149';
  document.getElementById('sw-trips').textContent = agg.trips_today;
  document.getElementById('sw-total-trips').textContent = agg.total_trips;

  const tbody = document.getElementById('swarm-body');
  let rows = '';
  const pairs = data.pairs || [];
  pairs.sort(function(a,b){ return b.today_pnl - a.today_pnl; });
  for (const p of pairs) {
    const pnlCls = p.today_pnl > 0 ? 'pnl-pos' : (p.today_pnl < 0 ? 'pnl-neg' : 'pnl-zero');
    const totCls = p.total_pnl > 0 ? 'pnl-pos' : (p.total_pnl < 0 ? 'pnl-neg' : 'pnl-zero');
    const multVal = p.multiplier || 1;
    rows += '<tr onclick="showDetailView(\'' + p.pair + '\')">';
    rows += '<td><b>' + p.display + '</b></td>';
    rows += '<td>$' + (p.price < 1 ? p.price.toFixed(6) : p.price.toFixed(2)) + '</td>';
    rows += '<td>' + p.pair_state + (p.long_only ? ' <span style="background:#9e6a03;color:#e3b341;font-size:10px;padding:1px 4px;border-radius:3px;font-weight:600">L</span>' : '') + '</td>';
    rows += '<td class="' + pnlCls + '">' + fmtUSD(p.today_pnl) + '</td>';
    rows += '<td class="' + totCls + '">' + fmtUSD(p.total_pnl) + '</td>';
    rows += '<td>' + p.trips_today + '/' + p.total_trips + '</td>';
    rows += '<td>' + fmt(p.entry_pct, 2) + '%</td>';
    rows += '<td><select onchange="setMultiplier(\'' + p.pair + '\',this.value);event.stopPropagation()">'
          + '<option' + (multVal===1 ? ' selected' : '') + '>1x</option>'
          + '<option' + (multVal===2 ? ' selected' : '') + '>2x</option>'
          + '<option' + (multVal===3 ? ' selected' : '') + '>3x</option>'
          + '<option' + (multVal===5 ? ' selected' : '') + '>5x</option>'
          + '</select></td>';
    rows += '<td><button class="btn-remove" onclick="removePair(\'' + p.pair + '\');event.stopPropagation()">X</button></td>';
    rows += '</tr>';
  }
  tbody.innerHTML = rows || '<tr><td colspan="9" style="text-align:center;color:#8b949e">No pairs active</td></tr>';
}

async function pollSwarm() {
  try {
    const r = await fetch('/api/swarm/status');
    if (r.ok) {
      const data = await r.json();
      renderSwarm(data);
    }
  } catch(e) {}
}

async function setMultiplier(pair, val) {
  const mult = parseInt(val);
  try {
    await fetch('/api/swarm/multiplier', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pair: pair, multiplier: mult})
    });
  } catch(e) {}
}

async function removePair(pair) {
  if (!confirm('Remove ' + pair + '? Open orders will be cancelled.')) return;
  try {
    await fetch('/api/swarm/remove', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pair: pair})
    });
    setTimeout(async function() {
      await pollSwarm();
      if (swarmData && swarmData.aggregate && swarmData.aggregate.active_pairs <= 1) {
        const pairs = swarmData.pairs || [];
        if (pairs.length === 1) showDetailView(pairs[0].pair);
      }
    }, 1000);
  } catch(e) {}
}

async function addPair(pair) {
  try {
    const r = await fetch('/api/swarm/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pair: pair})
    });
    const d = await r.json();
    if (r.ok) {
      // Refresh scanner and swarm, auto-switch to swarm view
      scannerCacheTime = 0;
      loadScanner();
      setTimeout(async function() {
        await pollSwarm();
        if (swarmData && swarmData.aggregate && swarmData.aggregate.active_pairs > 1) {
          closeScanner();
          showSwarmView();
        }
      }, 2000);
    } else {
      alert(d.error || 'Failed to add pair');
    }
  } catch(e) { alert('Network error'); }
}

// === Scanner ===
function openScanner() {
  document.getElementById('scanner-overlay').classList.add('active');
  loadScanner();
}

function closeScanner() {
  document.getElementById('scanner-overlay').classList.remove('active');
}

async function loadScanner() {
  const now = Date.now();
  if (scannerData && (now - scannerCacheTime) < 60000) {
    renderScanner(scannerData);
    return;
  }
  document.getElementById('scanner-body').innerHTML = '<div class="scanner-info">Loading pairs from Kraken...</div>';
  try {
    const r = await fetch('/api/swarm/available');
    if (r.ok) {
      scannerData = await r.json();
      scannerCacheTime = now;
      renderScanner(scannerData);
    }
  } catch(e) {
    document.getElementById('scanner-body').innerHTML = '<div class="scanner-info">Failed to load pairs</div>';
  }
}

function renderScanner(data) {
  const search = (document.getElementById('scanner-search').value || '').toUpperCase();
  let filtered = data;
  if (search) {
    filtered = data.filter(function(p) {
      return p.pair.toUpperCase().indexOf(search) >= 0
          || p.display.toUpperCase().indexOf(search) >= 0
          || p.base.toUpperCase().indexOf(search) >= 0;
    });
  }
  const stringKeys = new Set(['display']);
  filtered = filtered.slice().sort(function(a, b) {
    const av = a[scannerSortKey], bv = b[scannerSortKey];
    let cmp;
    if (stringKeys.has(scannerSortKey)) {
      cmp = String(av).localeCompare(String(bv));
    } else {
      cmp = (av || 0) - (bv || 0);
    }
    return scannerSortAsc ? cmp : -cmp;
  });
  const cols = [
    {key:'display',label:'Pair'}, {key:'score',label:'Score'},
    {key:'price',label:'Price'},
    {key:'volatility_pct',label:'Volatility'}, {key:'volume_24h',label:'24h Vol'},
    {key:'spread_pct',label:'Spread'}, {key:'fee_maker',label:'Fee'}
  ];
  let hdr = '';
  for (const c of cols) {
    const active = scannerSortKey === c.key;
    const arrow = active ? (scannerSortAsc ? ' <span class="sort-arrow">&#9650;</span>' : ' <span class="sort-arrow">&#9660;</span>') : '';
    hdr += '<th class="sortable' + (active ? ' sort-active' : '') + '" onclick="sortScanner(\'' + c.key + '\')">' + c.label + arrow + '</th>';
  }
  hdr += '<th></th>';
  let html = '<table class="scanner-table"><thead><tr>' + hdr + '</tr></thead><tbody>';
  for (const p of filtered) {
    const rmBadge = p.recovery_mode === 'liquidate'
      ? ' <span style="background:#9e6a03;color:#e3b341;font-size:9px;padding:1px 4px;border-radius:3px;font-weight:600" title="Force liquidate on orphan">F</span>'
      : '';
    html += '<tr>';
    html += '<td><b>' + p.display + '</b>' + rmBadge + '</td>';
    html += '<td>' + (p.score || 0).toFixed(1) + '</td>';
    html += '<td>$' + (p.price < 1 ? p.price.toFixed(6) : p.price.toFixed(2)) + '</td>';
    html += '<td>' + p.volatility_pct.toFixed(1) + '%</td>';
    html += '<td>$' + (p.volume_24h > 1e6 ? (p.volume_24h/1e6).toFixed(1)+'M' : Math.round(p.volume_24h).toLocaleString()) + '</td>';
    html += '<td>' + p.spread_pct.toFixed(3) + '%</td>';
    html += '<td>' + p.fee_maker.toFixed(2) + '%</td>';
    if (p.active) {
      html += '<td><span class="badge-active">Active</span></td>';
    } else {
      html += '<td><button class="btn-add" onclick="addPair(\'' + p.pair + '\')">Add</button></td>';
    }
    html += '</tr>';
  }
  html += '</tbody></table>';
  if (filtered.length === 0) {
    html = '<div class="scanner-info">No pairs match your search</div>';
  }
  document.getElementById('scanner-body').innerHTML = html;
}

function sortScanner(key) {
  const stringKeys = new Set(['display']);
  if (scannerSortKey === key) {
    scannerSortAsc = !scannerSortAsc;
  } else {
    scannerSortKey = key;
    scannerSortAsc = stringKeys.has(key);
  }
  if (scannerData) renderScanner(scannerData);
}

function filterScanner() {
  if (scannerData) renderScanner(scannerData);
}

// === View routing ===
// On initial load, check pair count to decide view
async function initView() {
  try {
    const r = await fetch('/api/swarm/status');
    if (r.ok) {
      const data = await r.json();
      swarmData = data;
      if (data.aggregate && data.aggregate.active_pairs > 1) {
        showSwarmView();
        renderSwarm(data);
      } else {
        showDetailView(null);
      }
    } else {
      showDetailView(null);
    }
  } catch(e) {
    showDetailView(null);
  }
}

// Override the API url for detail view when a specific pair is selected
const origPoll = poll;
async function poll() {
  if (swarmMode) {
    pollSwarm();
    return;
  }
  try {
    let url = API;
    if (detailPair) url = API + '?pair=' + detailPair;
    const r = await fetch(url);
    if (r.ok) update(await r.json());
  } catch(e) {}
}

// Auto-refresh: swarm view via pollSwarm, detail view via poll (when SSE is closed)
setInterval(function() {
  if (swarmMode) pollSwarm();
  else if (!evtSource) poll();
}, 5000);

// Initial view setup
initView();

// Initial detail fetch + SSE
poll();
startSSE();
</script>
</body>
</html>
"""
