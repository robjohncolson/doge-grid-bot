"""
factory_viz.py -- Factorio-style factory visualization dashboard.

Renders the bot's data pipeline as an interactive Canvas factory:
  - Price fetching → fill detection → state machine → exit lifecycle → AI council → profit booking
  - Each subsystem is a visible "machine" with input/output ports
  - Data flows as animated items on conveyor belts
  - Three zoom levels: World Map (all pairs), Pair Factory (single pair), Machine Detail

Three public symbols:
  FACTORY_HTML           -- the full HTML page (served on GET /factory)
  serialize_factory_state -- converts all GridStates into machine-level JSON
"""

import time
import config
import grid_strategy


# ---------------------------------------------------------------------------
# Factory state serializer
# ---------------------------------------------------------------------------

def serialize_factory_state(bot_states: dict, current_prices: dict) -> dict:
    """
    Build a machine-level snapshot of every pair for the factory viz.
    Called on every GET /api/factory/status and /api/factory/stream tick.

    Args:
        bot_states: dict of pair_name -> GridState
        current_prices: dict of pair_name -> float

    Returns:
        { "pairs": [ { pair_name, price, machines: {...}, orders, ... } ] }
    """
    now = time.time()
    pairs = []

    for pair_name, state in bot_states.items():
        # Use base pair for price lookup (slots share prices with primary)
        base = pair_name.split("#")[0] if "#" in pair_name else pair_name
        price = current_prices.get(base, 0.0)
        if not price and state.price_history:
            price = state.price_history[-1][1]
        if not price:
            price = state.center_price

        # Open orders breakdown
        open_orders = [o for o in state.grid_orders if o.status == "open"]
        entry_orders = [o for o in open_orders if getattr(o, "order_role", "") == "entry"]
        exit_orders = [o for o in open_orders if getattr(o, "order_role", "") == "exit"]

        # Recent fill count (last 5 min)
        recent_fill_cutoff = now - 300
        recent_fills = sum(1 for f in state.recent_fills if f.get("time", 0) > recent_fill_cutoff)

        # Exit age for each open exit
        exit_ages = []
        for o in exit_orders:
            entry_at = getattr(o, "entry_filled_at", 0.0)
            if entry_at > 0:
                exit_ages.append(round(now - entry_at))

        # Thresholds
        thresholds = {}
        if getattr(state, "pair_stats", None):
            th = grid_strategy.compute_exit_thresholds(state.pair_stats)
            if th:
                thresholds = {
                    "reprice_sec": round(th["reprice_after"]),
                    "orphan_sec": round(th["orphan_after"]),
                }

        # Backoff multipliers
        backoff_a = grid_strategy.get_backoff_entry_pct(state.entry_pct, state.consecutive_losses_a)
        backoff_b = grid_strategy.get_backoff_entry_pct(state.entry_pct, state.consecutive_losses_b)

        # AI council last result
        ai_result = getattr(state, "_last_ai_result", None)
        ai_machine = {"state": "idle", "verdict": None, "panelists": []}
        if ai_result:
            ai_machine = {
                "state": "active" if ai_result.get("action", "continue") != "continue" else "idle",
                "verdict": ai_result.get("action"),
                "condition": ai_result.get("condition"),
                "reason": ai_result.get("reason", ""),
                "consensus": ai_result.get("consensus", False),
                "panelists": [
                    {
                        "name": v.get("name", "?"),
                        "action": v.get("action", "?"),
                        "condition": v.get("condition", "?"),
                    }
                    for v in ai_result.get("panel_votes", [])
                ],
            }

        # Recovery orders
        recovery_list = []
        for r in getattr(state, "recovery_orders", []):
            recovery_list.append({
                "txid": r.txid,
                "side": r.side,
                "price": round(r.price, 6),
                "volume": round(r.volume, 2),
                "trade_id": r.trade_id,
                "cycle": r.cycle,
                "age_sec": round(now - r.orphaned_at) if r.orphaned_at > 0 else 0,
                "unrealized_pnl": round(r.unrealized_pnl(price), 4),
            })

        # Unrealized PnL
        unrealized = grid_strategy.compute_unrealized_pnl(state, price)

        machines = {
            "price_fetcher": {
                "state": "active" if price > 0 else "blocked",
                "price": round(price, 6),
                "pair_display": state.pair_display,
            },
            "order_scanner": {
                "state": "active",
                "open_count": len(open_orders),
                "entry_count": len(entry_orders),
                "exit_count": len(exit_orders),
            },
            "daily_reset": {
                "state": "idle",
                "today_date": state.today_date,
            },
            "risk_gate": {
                "state": "blocked" if state.is_paused else "active",
                "paused": state.is_paused,
                "pause_reason": state.pause_reason,
                "today_loss": round(state.today_loss_usd, 4),
                "daily_limit": state.daily_loss_limit,
            },
            "fill_detector": {
                "state": "active" if recent_fills > 0 else "idle",
                "recent_fills_5m": recent_fills,
                "total_entries_placed": state.total_entries_placed,
                "total_entries_filled": state.total_entries_filled,
            },
            "state_machine": {
                "state": "active",
                "pair_state": state.pair_state,
                "cycle_a": state.cycle_a,
                "cycle_b": state.cycle_b,
                "long_only": state.long_only,
            },
            "exit_pricer": {
                "state": "active" if exit_orders else "idle",
                "profit_pct": state.profit_pct,
                "exit_count": len(exit_orders),
                "exit_ages_sec": exit_ages,
            },
            "entry_placer": {
                "state": "active" if entry_orders else "idle",
                "entry_pct": state.entry_pct,
                "entry_count": len(entry_orders),
            },
            "order_placer": {
                "state": "active" if open_orders else "idle",
                "total_open": len(open_orders),
            },
            "profit_chest": {
                "state": "active",
                "today_profit": round(state.today_profit_usd, 4),
                "total_profit": round(state.total_profit_usd, 4),
                "round_trips_today": state.round_trips_today,
                "total_round_trips": state.total_round_trips,
                "total_fees": round(state.total_fees_usd, 4),
                "doge_accumulated": round(state.doge_accumulated, 2),
                "unrealized": unrealized,
            },
            "reprice_station": {
                "state": "active" if state.pair_state in ("S1a", "S1b") else "idle",
                "last_reprice_a": state.last_reprice_a,
                "last_reprice_b": state.last_reprice_b,
                "reprice_count_a": state.exit_reprice_count_a,
                "reprice_count_b": state.exit_reprice_count_b,
            },
            "orphan_station": {
                "state": "active" if recovery_list else "idle",
                "recovery_count": len(recovery_list),
                "max_slots": config.MAX_RECOVERY_SLOTS,
                "total_losses": getattr(state, "total_recovery_losses", 0),
                "total_wins": round(getattr(state, "total_recovery_wins", 0.0), 4),
            },
            "s2_break_glass": {
                "state": "active" if state.pair_state == "S2" else "idle",
                "s2_entered_at": state.s2_entered_at,
                "s2_age_sec": round(now - state.s2_entered_at) if state.s2_entered_at else None,
                "max_spread_pct": config.S2_MAX_SPREAD_PCT,
            },
            "recovery_monitor": {
                "state": "active" if recovery_list else "idle",
                "orders": recovery_list,
            },
            "stats_engine": {
                "state": "active" if state.stats_results else "idle",
                "last_run": state.stats_last_run,
                "age_sec": round(now - state.stats_last_run) if state.stats_last_run else None,
            },
            "volatility_adjust": {
                "state": "active" if state.last_volatility_adjust > 0 else "idle",
                "last_adjust": state.last_volatility_adjust,
                "current_profit_pct": state.profit_pct,
                "floor": config.VOLATILITY_PROFIT_FLOOR,
                "ceiling": config.VOLATILITY_PROFIT_CEILING,
            },
            "entry_backoff": {
                "state": "active" if (state.consecutive_losses_a > 0 or state.consecutive_losses_b > 0) else "idle",
                "losses_a": state.consecutive_losses_a,
                "losses_b": state.consecutive_losses_b,
                "effective_entry_a": round(backoff_a, 4),
                "effective_entry_b": round(backoff_b, 4),
                "base_entry": state.entry_pct,
            },
            "ai_council": ai_machine,
            "auto_execute": {
                "state": "active" if config.AI_AUTO_EXECUTE else "idle",
                "enabled": config.AI_AUTO_EXECUTE,
            },
            "state_persister": {
                "state": "active",
            },
        }

        pairs.append({
            "pair_name": pair_name,
            "pair_display": state.pair_display,
            "price": round(price, 6),
            "machines": machines,
            "orders": [
                {
                    "side": o.side,
                    "price": round(o.price, 6),
                    "role": getattr(o, "order_role", ""),
                    "trade_id": getattr(o, "trade_id", None),
                }
                for o in open_orders
            ],
            "trend": {
                "direction": getattr(state, "detected_trend", None),
                "age_sec": round(now - state.trend_detected_at) if getattr(state, "trend_detected_at", None) else None,
            },
            "thresholds": thresholds,
        })

    return {"pairs": pairs, "server_time": round(now, 1)}


# ---------------------------------------------------------------------------
# Diff computation (minimize SSE bandwidth)
# ---------------------------------------------------------------------------

def compute_diff(prev: dict, curr: dict) -> dict:
    """
    Compare two factory snapshots and return only changed machine states.
    Returns a compact diff or None if nothing changed.
    """
    if not prev or not curr:
        return curr

    diff_pairs = []
    prev_map = {p["pair_name"]: p for p in prev.get("pairs", [])}

    for cp in curr.get("pairs", []):
        pn = cp["pair_name"]
        pp = prev_map.get(pn)
        if not pp:
            # New pair -- send full
            diff_pairs.append(cp)
            continue

        changed_machines = {}
        for mid, mdata in cp["machines"].items():
            prev_mdata = pp["machines"].get(mid)
            if mdata != prev_mdata:
                changed_machines[mid] = mdata

        if changed_machines or cp["price"] != pp["price"]:
            diff_pairs.append({
                "pair_name": pn,
                "pair_display": cp["pair_display"],
                "price": cp["price"],
                "machines": changed_machines,
                "orders": cp["orders"],
                "trend": cp["trend"],
                "thresholds": cp["thresholds"],
            })

    if not diff_pairs:
        return None

    return {"pairs": diff_pairs, "server_time": curr["server_time"], "is_diff": True}


# ---------------------------------------------------------------------------
# Factory HTML (Canvas visualization)
# ---------------------------------------------------------------------------

FACTORY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Factory Viz</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a2e;color:#f0f6fc;font-family:'Cascadia Mono','Fira Code','Courier New',monospace;overflow:hidden;height:100vh;width:100vw}
#canvas{position:absolute;top:0;left:0;cursor:grab}
#canvas.dragging{cursor:grabbing}

/* Detail panel */
#detail{position:fixed;right:-420px;top:0;width:400px;height:100vh;background:#161b22;border-left:2px solid #30363d;
  transition:right 0.3s ease;z-index:100;overflow-y:auto;padding:20px;font-size:13px}
#detail.open{right:0}
#detail .close-btn{position:absolute;top:12px;right:12px;background:none;border:none;color:#8b949e;font-size:20px;cursor:pointer}
#detail .close-btn:hover{color:#f0f6fc}
#detail h2{font-size:16px;color:#e3b341;margin-bottom:4px}
#detail .subtitle{color:#8b949e;font-size:11px;margin-bottom:16px}
#detail .source-ref{color:#58a6ff;font-size:11px;margin-bottom:16px;display:block}
#detail .section{margin-bottom:16px}
#detail .section h3{font-size:12px;color:#8b949e;text-transform:uppercase;margin-bottom:8px;border-bottom:1px solid #30363d;padding-bottom:4px}
#detail table{width:100%;border-collapse:collapse}
#detail td{padding:4px 8px;border-bottom:1px solid #21262d}
#detail td:first-child{color:#8b949e;width:45%}
#detail td:last-child{color:#f0f6fc;text-align:right}
#detail .status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
#detail .status-active{background:#3fb950}
#detail .status-idle{background:#8b949e}
#detail .status-blocked{background:#f85149}

/* HUD overlay */
#hud{position:fixed;top:12px;left:12px;z-index:50;pointer-events:none}
#hud .title{font-size:18px;font-weight:700;color:#e3b341;text-shadow:0 0 10px rgba(227,179,65,0.3)}
#hud .info{font-size:11px;color:#8b949e;margin-top:4px}
#zoom-label{position:fixed;top:12px;right:430px;z-index:50;font-size:11px;color:#8b949e;pointer-events:none;transition:right 0.3s ease}

/* Minimap */
#minimap{position:fixed;bottom:12px;left:12px;width:160px;height:100px;background:#161b22;border:1px solid #30363d;border-radius:6px;z-index:50;overflow:hidden}
#minimap canvas{width:100%;height:100%}

/* Nav buttons */
#nav{position:fixed;bottom:12px;right:12px;z-index:50;display:flex;gap:8px}
#nav button{background:#21262d;border:1px solid #30363d;color:#f0f6fc;padding:6px 14px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px}
#nav button:hover{background:#30363d;border-color:#58a6ff}
#nav button.active{background:#1f6feb;border-color:#58a6ff}

/* Back to dashboard link */
#back-link{position:fixed;top:12px;right:12px;z-index:50;pointer-events:auto}
#back-link a{color:#58a6ff;text-decoration:none;font-size:12px;background:#21262d;padding:6px 12px;border-radius:4px;border:1px solid #30363d}
#back-link a:hover{background:#30363d}
</style>
</head>
<body>
<canvas id="canvas"></canvas>

<div id="hud">
  <div class="title">FACTORY VIZ</div>
  <div class="info" id="hud-info">Loading...</div>
</div>
<div id="zoom-label"></div>

<div id="back-link"><a href="/">Dashboard</a></div>

<div id="nav">
  <button id="btn-world" class="active">World Map</button>
  <button id="btn-fit">Fit View</button>
  <button id="btn-reset">Reset Zoom</button>
</div>

<div id="detail">
  <button class="close-btn" id="detail-close">&times;</button>
  <h2 id="detail-title">Machine</h2>
  <div class="subtitle" id="detail-subtitle"></div>
  <a class="source-ref" id="detail-source" href="#"></a>
  <div id="detail-body"></div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════
// FACTORY VIZ -- Factorio-style Canvas visualization
// ═══════════════════════════════════════════════════════════════════

(function(){
"use strict";

// ─── Color palette ─────────────────────────────────────────────
const C = {
  bg:         "#1a1a2e",
  grid:       "#2d2d44",
  beltTrack:  "#3d3d3d",
  beltDash:   "#5a5a5a",
  machineBody:"#2d2d44",
  machineBodyLit: "#363650",
  active:     "#e3b341",
  idle:       "#30363d",
  blocked:    "#f85149",
  hover:      "#58a6ff",
  itemPrice:  "#b87333",
  itemFill:   "#3fb950",
  itemOrder:  "#58a6ff",
  itemProfit: "#e3b341",
  itemRisk:   "#f85149",
  itemRecov:  "#f0883e",
  itemAI:     "#bc8cff",
  label:      "#f0f6fc",
  sublabel:   "#8b949e",
  wire:       "#3fb950",
  wireAlert:  "#f85149",
  s0:         "#3fb950",
  s1:         "#e3b341",
  s2:         "#f85149",
};

// ─── Machine registry ──────────────────────────────────────────
const MACHINE_REG = {
  price_fetcher:    { label: "Price Fetcher",     sub: "kraken_client.get_prices()",       file: "bot.py",            desc: "Fetches live bid/ask prices from Kraken REST API every poll cycle." },
  order_scanner:    { label: "Order Scanner",     sub: "query_orders_batched()",           file: "bot.py",            desc: "Queries Kraken for status of all open order TXIDs in batch." },
  daily_reset:      { label: "Daily Reset",       sub: "check_daily_reset()",              file: "grid_strategy.py",  desc: "Resets daily P&L counters at midnight UTC." },
  risk_gate:        { label: "Risk Gate",         sub: "check_risk_limits()",              file: "grid_strategy.py",  desc: "Circuit breaker: pauses trading if daily loss limit or stop floor is breached." },
  fill_detector:    { label: "Fill Detector",     sub: "check_fills_live()",               file: "grid_strategy.py",  desc: "Detects order fills by comparing order status snapshots. Triggers state transitions." },
  state_machine:    { label: "STATE MACHINE",     sub: "_compute_pair_state()",            file: "grid_strategy.py",  desc: "Central S0/S1a/S1b/S2 state diamond. Derives pair state from open orders on book." },
  exit_pricer:      { label: "Exit Pricer",       sub: "_pair_exit_price()",               file: "grid_strategy.py",  desc: "Computes exit order price from entry fill price + profit target percentage." },
  entry_placer:     { label: "Entry Placer",      sub: "_place_pair_order()",              file: "grid_strategy.py",  desc: "Places entry limit orders at computed distance from market price." },
  order_placer:     { label: "Order Placer",      sub: "place_order()",                    file: "kraken_client.py",  desc: "Sends limit order to Kraken REST API. Handles nonce, signing, retries." },
  profit_chest:     { label: "Profit Chest",      sub: "P&L accumulators",                 file: "grid_strategy.py",  desc: "Accumulates realized profit, fees, round trip counts. Saved to state." },
  reprice_station:  { label: "Reprice Station",   sub: "check_stale_exits()",              file: "grid_strategy.py",  desc: "Tightens stale exit orders in S1a/S1b when they exceed reprice threshold." },
  orphan_station:   { label: "Orphan Station",    sub: "_orphan_exit()",                   file: "grid_strategy.py",  desc: "Moves stranded exits to recovery slots and places fresh entries." },
  s2_break_glass:   { label: "S2 Break-Glass",    sub: "check_s2_break_glass()",           file: "grid_strategy.py",  desc: "Emergency protocol when both exits are open. Evaluates spread and orphans worse trade." },
  recovery_monitor: { label: "Recovery Monitor",  sub: "check_recovery_fills()",           file: "grid_strategy.py",  desc: "Monitors orphaned recovery orders for surprise fills or external cancellations." },
  stats_engine:     { label: "Stats Engine",      sub: "stats_engine.run_all()",           file: "stats_engine.py",   desc: "Runs 5 statistical analyzers: fill rate, timing, volatility, trend, momentum." },
  volatility_adjust:{ label: "Volatility Adjust", sub: "adjust_profit_from_volatility()",  file: "grid_strategy.py",  desc: "Auto-tunes profit_pct from OHLC median range. Rate limited to once per 5 min." },
  entry_backoff:    { label: "Entry Backoff",     sub: "get_backoff_entry_pct()",           file: "grid_strategy.py",  desc: "Widens entry distance after consecutive losing cycles on a trade leg." },
  ai_council:       { label: "AI Council",        sub: "get_recommendation()",             file: "ai_advisor.py",     desc: "Queries 3 AI models (Llama-70B, Llama-8B, Kimi-K2.5), aggregates by majority vote." },
  auto_execute:     { label: "Auto-Execute Gate", sub: "AI_SAFE_ACTIONS check",            file: "bot.py",            desc: "Allows safe AI actions (widen_entry, widen_spacing) to bypass Telegram approval." },
  state_persister:  { label: "State Persister",   sub: "save_state()",                     file: "grid_strategy.py",  desc: "Atomic JSON snapshot to disk + Supabase cloud backup. Survives restarts." },
};

// ─── Layout constants ──────────────────────────────────────────
const MW = 170, MH = 80;  // machine size
const BELT_GAP = 70;      // gap between machines (belt length)
const ROW_GAP = 120;      // gap between rows
const MARGIN = 100;       // layout margin

// ─── Factory layout definition (x,y in grid units) ────────────
// Row 1 (main production line) - left to right
const LAYOUT = {
  // Row 0 (exit lifecycle) -- y=0
  reprice_station:  { gx: 3.5, gy: 0, row: 0 },
  orphan_station:   { gx: 5,   gy: 0, row: 0 },
  s2_break_glass:   { gx: 6.5, gy: 0, row: 0 },
  recovery_monitor: { gx: 8,   gy: 0, row: 0 },

  // Row 1 (main line) -- y=1
  price_fetcher:    { gx: 0,   gy: 1, row: 1 },
  order_scanner:    { gx: 1.5, gy: 1, row: 1 },
  daily_reset:      { gx: 3,   gy: 1, row: 1 },
  risk_gate:        { gx: 4.5, gy: 1, row: 1 },
  fill_detector:    { gx: 6,   gy: 1, row: 1 },
  state_machine:    { gx: 7.5, gy: 1, row: 1 },
  exit_pricer:      { gx: 9,   gy: 1, row: 1 },
  entry_placer:     { gx: 9,   gy: 1.7, row: 1 },
  order_placer:     { gx: 10.5,gy: 1, row: 1 },
  profit_chest:     { gx: 12,  gy: 1, row: 1 },
  state_persister:  { gx: 13.5,gy: 1, row: 1 },

  // Row 2 (analytics) -- y=2
  stats_engine:     { gx: 3.5, gy: 2.4, row: 2 },
  volatility_adjust:{ gx: 5,   gy: 2.4, row: 2 },
  entry_backoff:    { gx: 6.5, gy: 2.4, row: 2 },
  ai_council:       { gx: 8,   gy: 2.4, row: 2 },
  auto_execute:     { gx: 9.5, gy: 2.4, row: 2 },
};

// Belt connections: [from, to, itemType]
const BELTS = [
  // Main line
  ["price_fetcher",   "order_scanner",   "price"],
  ["order_scanner",   "daily_reset",     "order"],
  ["daily_reset",     "risk_gate",       "order"],
  ["risk_gate",       "fill_detector",   "order"],
  ["fill_detector",   "state_machine",   "fill"],
  ["state_machine",   "exit_pricer",     "order"],
  ["state_machine",   "entry_placer",    "order"],
  ["exit_pricer",     "order_placer",    "order"],
  ["entry_placer",    "order_placer",    "order"],
  ["order_placer",    "profit_chest",    "profit"],
  ["profit_chest",    "state_persister", "profit"],
  // Exit lifecycle row
  ["reprice_station", "orphan_station",  "recovery"],
  ["orphan_station",  "s2_break_glass",  "recovery"],
  ["s2_break_glass",  "recovery_monitor","recovery"],
  // Cross-row connections
  ["state_machine",   "reprice_station", "order"],
  ["recovery_monitor","state_machine",   "recovery"],
  // Analytics row
  ["stats_engine",    "volatility_adjust","ai"],
  ["volatility_adjust","entry_backoff",  "ai"],
  ["entry_backoff",   "ai_council",      "ai"],
  ["ai_council",      "auto_execute",    "ai"],
  ["fill_detector",   "stats_engine",    "fill"],
  ["auto_execute",    "state_machine",   "ai"],
];

// ─── Canvas setup ──────────────────────────────────────────────
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
let W, H, DPR;

function resize() {
  DPR = window.devicePixelRatio || 1;
  W = window.innerWidth;
  H = window.innerHeight;
  canvas.width = W * DPR;
  canvas.height = H * DPR;
  canvas.style.width = W + "px";
  canvas.style.height = H + "px";
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
}
window.addEventListener("resize", resize);
resize();

// ─── Camera ────────────────────────────────────────────────────
let cam = { x: 0, y: 0, zoom: 0.22 };
let targetCam = null;  // for smooth transitions

function screenToWorld(sx, sy) {
  return { x: (sx - W/2) / cam.zoom + cam.x, y: (sy - H/2) / cam.zoom + cam.y };
}

function worldToScreen(wx, wy) {
  return { x: (wx - cam.x) * cam.zoom + W/2, y: (wy - cam.y) * cam.zoom + H/2 };
}

// ─── Scene graph ───────────────────────────────────────────────
let nodes = [];       // machine nodes
let beltPaths = [];   // belt connection paths
let pairLayouts = []; // one layout per pair (world map)
let factoryData = null;
let selectedNode = null;
let hoveredNode = null;
let selectedPair = null;  // for world map drill-down
let animTime = 0;

// Item types for belt animation
const ITEM_COLORS = {
  price:    C.itemPrice,
  fill:     C.itemFill,
  order:    C.itemOrder,
  profit:   C.itemProfit,
  risk:     C.itemRisk,
  recovery: C.itemRecov,
  ai:       C.itemAI,
};

// ─── Build scene from data ─────────────────────────────────────
function buildScene(data) {
  factoryData = data;
  if (!data || !data.pairs || data.pairs.length === 0) return;

  nodes = [];
  beltPaths = [];
  pairLayouts = [];

  const pairCount = data.pairs.length;
  const cols = Math.ceil(Math.sqrt(pairCount));

  // World map: space pair factories on a grid
  const FACTORY_W = (14.5 * (MW + BELT_GAP)) + MARGIN * 2;
  const FACTORY_H = (3 * (MH + ROW_GAP)) + MARGIN * 2;
  const PAIR_SPACING_X = FACTORY_W + 200;
  const PAIR_SPACING_Y = FACTORY_H + 200;

  data.pairs.forEach((pair, pi) => {
    const col = pi % cols;
    const row = Math.floor(pi / cols);
    const ox = col * PAIR_SPACING_X;
    const oy = row * PAIR_SPACING_Y;

    pairLayouts.push({
      pair_name: pair.pair_name,
      pair_display: pair.pair_display,
      ox, oy,
      w: FACTORY_W,
      h: FACTORY_H,
      price: pair.price,
      machines: pair.machines,
    });

    // Create machine nodes for this pair
    const pairNodes = {};
    for (const [mid, pos] of Object.entries(LAYOUT)) {
      const reg = MACHINE_REG[mid] || { label: mid, sub: "", file: "", desc: "" };
      const x = ox + MARGIN + pos.gx * (MW + BELT_GAP);
      const y = oy + MARGIN + pos.gy * (MH + ROW_GAP);
      const machineData = pair.machines[mid] || { state: "idle" };

      const node = {
        id: pair.pair_name + ":" + mid,
        mid: mid,
        pair: pair.pair_name,
        pairDisplay: pair.pair_display,
        x, y, w: MW, h: MH,
        label: reg.label,
        sublabel: reg.sub,
        sourceRef: reg.file,
        desc: reg.desc,
        state: machineData.state || "idle",
        data: machineData,
        isDiamond: mid === "state_machine",
        isChest: mid === "profit_chest",
        ports: { in: { x: x, y: y + MH/2 }, out: { x: x + MW, y: y + MH/2 } },
      };
      nodes.push(node);
      pairNodes[mid] = node;
    }

    // Create belt paths
    BELTS.forEach(([from, to, itemType]) => {
      const nf = pairNodes[from];
      const nt = pairNodes[to];
      if (!nf || !nt) return;
      beltPaths.push({
        from: nf, to: nt,
        pair: pair.pair_name,
        itemType: itemType,
        // Path from output port to input port
        x1: nf.x + nf.w, y1: nf.y + nf.h / 2,
        x2: nt.x,         y2: nt.y + nt.h / 2,
      });
    });
  });

  // Center camera on world if first load
  if (pairLayouts.length > 0 && !selectedPair) {
    const all = pairLayouts;
    const cx = all.reduce((s, p) => s + p.ox + p.w/2, 0) / all.length;
    const cy = all.reduce((s, p) => s + p.oy + p.h/2, 0) / all.length;
    cam.x = cx;
    cam.y = cy;
    if (pairLayouts.length === 1) {
      cam.zoom = Math.min(W / (pairLayouts[0].w + 100), H / (pairLayouts[0].h + 100)) * 0.85;
    }
  }
}

function updateSceneData(data) {
  if (!data || !data.pairs) return;
  factoryData = data;
  data.pairs.forEach(pair => {
    for (const [mid, machineData] of Object.entries(pair.machines)) {
      const nodeId = pair.pair_name + ":" + mid;
      const node = nodes.find(n => n.id === nodeId);
      if (node) {
        node.state = machineData.state || "idle";
        node.data = machineData;
      }
    }
    // Update pair layout data
    const pl = pairLayouts.find(p => p.pair_name === pair.pair_name);
    if (pl) {
      pl.price = pair.price;
      pl.machines = pair.machines;
    }
  });
}

// ─── Drawing helpers ───────────────────────────────────────────
function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.arcTo(x + w, y, x + w, y + r, r);
  ctx.lineTo(x + w, y + h - r);
  ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
  ctx.lineTo(x + r, y + h);
  ctx.arcTo(x, y + h, x, y + h - r, r);
  ctx.lineTo(x, y + r);
  ctx.arcTo(x, y, x + r, y, r);
  ctx.closePath();
}

function diamond(cx, cy, w, h) {
  ctx.beginPath();
  ctx.moveTo(cx, cy - h/2);
  ctx.lineTo(cx + w/2, cy);
  ctx.lineTo(cx, cy + h/2);
  ctx.lineTo(cx - w/2, cy);
  ctx.closePath();
}

function stateColor(state) {
  if (state === "active") return C.active;
  if (state === "blocked") return C.blocked;
  return C.idle;
}

function pairStateColor(ps) {
  if (ps === "S0") return C.s0;
  if (ps === "S2") return C.s2;
  return C.s1; // S1a, S1b
}

// ─── Draw belt ─────────────────────────────────────────────────
function drawBelt(b, t) {
  const {x1, y1, x2, y2, itemType} = b;
  const dx = x2 - x1, dy = y2 - y1;
  const len = Math.sqrt(dx*dx + dy*dy);
  if (len < 1) return;

  // Belt track (double line)
  ctx.strokeStyle = C.beltTrack;
  ctx.lineWidth = 8;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  // Use a curve for non-horizontal belts
  if (Math.abs(dy) > 10) {
    const mx = (x1 + x2) / 2;
    const my = (y1 + y2) / 2;
    ctx.quadraticCurveTo(mx, y1, x2, y2);
  } else {
    ctx.lineTo(x2, y2);
  }
  ctx.stroke();

  // Animated dashes (moving hash marks)
  ctx.strokeStyle = C.beltDash;
  ctx.lineWidth = 6;
  ctx.setLineDash([6, 10]);
  ctx.lineDashOffset = -(t * 40) % 16;
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  if (Math.abs(dy) > 10) {
    const mx = (x1 + x2) / 2;
    ctx.quadraticCurveTo(mx, y1, x2, y2);
  } else {
    ctx.lineTo(x2, y2);
  }
  ctx.stroke();
  ctx.setLineDash([]);

  // Items flowing along belt
  const itemColor = ITEM_COLORS[itemType] || C.itemOrder;
  const fromActive = b.from.state === "active";
  const itemCount = fromActive ? 3 : 1;
  for (let i = 0; i < itemCount; i++) {
    const phase = ((t * 0.5 + i / itemCount) % 1);
    let ix, iy;
    if (Math.abs(dy) > 10) {
      // Quadratic bezier point
      const p0x = x1, p0y = y1;
      const p1x = (x1+x2)/2, p1y = y1;
      const p2x = x2, p2y = y2;
      ix = (1-phase)*(1-phase)*p0x + 2*(1-phase)*phase*p1x + phase*phase*p2x;
      iy = (1-phase)*(1-phase)*p0y + 2*(1-phase)*phase*p1y + phase*phase*p2y;
    } else {
      ix = x1 + dx * phase;
      iy = y1 + dy * phase;
    }
    ctx.beginPath();
    ctx.arc(ix, iy, 4, 0, Math.PI * 2);
    ctx.fillStyle = itemColor;
    ctx.globalAlpha = fromActive ? 0.9 : 0.4;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
}

// ─── Draw machine ──────────────────────────────────────────────
function drawMachine(node, t) {
  const {x, y, w, h, label, state, isDiamond, isChest, data} = node;
  const isHover = hoveredNode === node;
  const isSelected = selectedNode === node;

  // Glow for active/blocked
  if (state === "active" || state === "blocked") {
    ctx.shadowColor = stateColor(state);
    ctx.shadowBlur = isHover ? 20 : 10;
  }

  if (isDiamond) {
    // State machine diamond
    const ps = data.pair_state || "S0";
    const cx = x + w/2, cy = y + h/2;
    const dw = w * 0.9, dh = h * 1.3;
    diamond(cx, cy, dw, dh);
    const grad = ctx.createLinearGradient(cx - dw/2, cy - dh/2, cx + dw/2, cy + dh/2);
    grad.addColorStop(0, pairStateColor(ps));
    grad.addColorStop(1, C.machineBody);
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.strokeStyle = isHover ? C.hover : pairStateColor(ps);
    ctx.lineWidth = isSelected ? 3 : 2;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // State label
    ctx.fillStyle = C.label;
    ctx.font = "bold 20px 'Cascadia Mono','Fira Code',monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(ps, cx, cy);

    // Cycle labels
    ctx.font = "11px 'Cascadia Mono','Fira Code',monospace";
    ctx.fillStyle = C.sublabel;
    ctx.fillText("A:" + (data.cycle_a||1), cx - 20, cy + dh/2 + 12);
    ctx.fillText("B:" + (data.cycle_b||1), cx + 20, cy + dh/2 + 12);
  } else {
    // Standard machine box
    roundRect(x, y, w, h, 6);

    const grad = ctx.createLinearGradient(x, y, x, y + h);
    grad.addColorStop(0, isHover ? C.machineBodyLit : C.machineBody);
    grad.addColorStop(1, C.bg);
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.strokeStyle = isHover ? C.hover : (isSelected ? C.hover : stateColor(state));
    ctx.lineWidth = isSelected ? 3 : (state === "active" ? 2 : 1);
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Indicator light (top-right)
    const lx = x + w - 12, ly = y + 8;
    ctx.beginPath();
    ctx.arc(lx, ly, 4, 0, Math.PI * 2);
    const pulseAlpha = state === "active" ? 0.6 + 0.4 * Math.sin(t * 3) : 1;
    ctx.globalAlpha = pulseAlpha;
    ctx.fillStyle = stateColor(state);
    ctx.fill();
    ctx.globalAlpha = 1;

    // Input port (left)
    ctx.beginPath();
    ctx.arc(x, y + h/2, 4, 0, Math.PI * 2);
    ctx.fillStyle = C.beltTrack;
    ctx.fill();
    ctx.strokeStyle = C.idle;
    ctx.lineWidth = 1;
    ctx.stroke();

    // Output port (right)
    ctx.beginPath();
    ctx.arc(x + w, y + h/2, 4, 0, Math.PI * 2);
    ctx.fillStyle = C.beltTrack;
    ctx.fill();
    ctx.strokeStyle = C.idle;
    ctx.lineWidth = 1;
    ctx.stroke();

    // Label
    ctx.fillStyle = C.label;
    ctx.font = "bold 14px 'Cascadia Mono','Fira Code',monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const displayLabel = isChest ? "Profit Chest" : label;
    ctx.fillText(displayLabel, x + w/2, y + h/2 - 10);

    // Sublabel (value or source ref)
    ctx.font = "12px 'Cascadia Mono','Fira Code',monospace";
    ctx.fillStyle = C.sublabel;
    let subText = _machineSubtext(node);
    ctx.fillText(subText, x + w/2, y + h/2 + 10);

    // Chest: show profit value below
    if (isChest && data) {
      ctx.font = "bold 13px 'Cascadia Mono','Fira Code',monospace";
      ctx.fillStyle = (data.total_profit||0) >= 0 ? C.itemFill : C.blocked;
      ctx.fillText("$" + (data.total_profit||0).toFixed(2), x + w/2, y + h + 12);
    }
  }
}

function _machineSubtext(node) {
  const d = node.data;
  if (!d) return node.sublabel;
  switch (node.mid) {
    case "price_fetcher":   return d.price ? "$" + d.price.toFixed(6) : "...";
    case "order_scanner":   return d.open_count + " open";
    case "risk_gate":       return d.paused ? "PAUSED" : "OK";
    case "fill_detector":   return d.recent_fills_5m + " fills/5m";
    case "state_machine":   return d.pair_state || "S0";
    case "exit_pricer":     return d.profit_pct ? d.profit_pct.toFixed(2)+"%" : "";
    case "entry_placer":    return d.entry_pct ? d.entry_pct.toFixed(2)+"%" : "";
    case "entry_backoff":
      if (d.losses_a > 0 || d.losses_b > 0) return "A:"+d.losses_a+" B:"+d.losses_b;
      return "0 losses";
    case "ai_council":      return d.verdict || "idle";
    case "profit_chest":    return (d.round_trips_today||0) + " trips today";
    case "orphan_station":  return d.recovery_count + "/" + d.max_slots;
    case "reprice_station": return "reprices: " + ((d.reprice_count_a||0) + (d.reprice_count_b||0));
    case "s2_break_glass":  return d.s2_age_sec ? Math.round(d.s2_age_sec/60)+"m" : "idle";
    case "recovery_monitor":return (d.orders||[]).length + " tracking";
    case "volatility_adjust":return d.current_profit_pct ? d.current_profit_pct.toFixed(2)+"%" : "";
    case "auto_execute":    return d.enabled ? "enabled" : "disabled";
    default: return node.sublabel;
  }
}

// ─── Draw power wires from risk gate ───────────────────────────
function drawWires(t) {
  if (!factoryData || !factoryData.pairs) return;
  factoryData.pairs.forEach(pair => {
    const riskNode = nodes.find(n => n.pair === pair.pair_name && n.mid === "risk_gate");
    if (!riskNode) return;
    const isPaused = pair.machines.risk_gate && pair.machines.risk_gate.paused;
    nodes.forEach(n => {
      if (n.pair !== pair.pair_name || n.mid === "risk_gate") return;
      // Draw thin wire from risk gate to each machine
      ctx.strokeStyle = isPaused ? C.wireAlert : C.wire;
      ctx.lineWidth = 1;
      ctx.globalAlpha = isPaused ? (0.4 + 0.4 * Math.sin(t * 5)) : 0.15;
      ctx.setLineDash([2, 6]);
      ctx.beginPath();
      ctx.moveTo(riskNode.x + riskNode.w/2, riskNode.y + riskNode.h);
      ctx.lineTo(n.x + n.w/2, n.y + n.h/2);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
    });
  });
}

// ─── Draw world map outpost ────────────────────────────────────
function drawOutpost(pl, t) {
  const {ox, oy, w, h, pair_display, price, machines} = pl;
  const isHover = hoveredPairLayout === pl;
  const ps = machines.state_machine ? machines.state_machine.pair_state : "S0";

  // Outpost background
  roundRect(ox, oy, w, h, 12);
  ctx.fillStyle = isHover ? "#1e1e3a" : "#161b28";
  ctx.fill();
  ctx.strokeStyle = isHover ? C.hover : pairStateColor(ps);
  ctx.lineWidth = isHover ? 3 : 2;
  ctx.stroke();

  // Big pair label
  ctx.fillStyle = C.label;
  ctx.font = "bold 48px 'Cascadia Mono','Fira Code',monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(pair_display, ox + w/2, oy + h * 0.35);

  // Price
  ctx.font = "32px 'Cascadia Mono','Fira Code',monospace";
  ctx.fillStyle = C.itemPrice;
  ctx.fillText("$" + (price||0).toFixed(6), ox + w/2, oy + h * 0.5);

  // State diamond
  const dSize = 60;
  diamond(ox + w/2, oy + h * 0.65, dSize, dSize);
  ctx.fillStyle = pairStateColor(ps);
  ctx.fill();
  ctx.fillStyle = "#000";
  ctx.font = "bold 20px monospace";
  ctx.fillText(ps, ox + w/2, oy + h * 0.65);

  // Profit
  const profit = machines.profit_chest ? machines.profit_chest.total_profit : 0;
  ctx.fillStyle = profit >= 0 ? C.itemFill : C.blocked;
  ctx.font = "28px 'Cascadia Mono','Fira Code',monospace";
  ctx.fillText("$" + profit.toFixed(2), ox + w/2, oy + h * 0.8);

  // Status lights row
  const lightY = oy + h * 0.9;
  const machineKeys = Object.keys(machines);
  const lightSpacing = Math.min(30, (w - 100) / machineKeys.length);
  const startX = ox + w/2 - (machineKeys.length * lightSpacing) / 2;
  machineKeys.forEach((mk, i) => {
    const ms = machines[mk].state || "idle";
    ctx.beginPath();
    ctx.arc(startX + i * lightSpacing, lightY, 5, 0, Math.PI * 2);
    ctx.fillStyle = stateColor(ms);
    ctx.globalAlpha = ms === "active" ? (0.6 + 0.4 * Math.sin(t * 3 + i)) : 0.5;
    ctx.fill();
    ctx.globalAlpha = 1;
  });
}

// ─── Draw grid background ──────────────────────────────────────
function drawGrid() {
  const step = 100;
  const tl = screenToWorld(0, 0);
  const br = screenToWorld(W, H);
  const startX = Math.floor(tl.x / step) * step;
  const startY = Math.floor(tl.y / step) * step;

  ctx.strokeStyle = C.grid;
  ctx.lineWidth = 0.5;
  ctx.globalAlpha = 0.3;
  for (let x = startX; x < br.x; x += step) {
    const s = worldToScreen(x, 0);
    ctx.beginPath();
    ctx.moveTo(s.x, 0);
    ctx.lineTo(s.x, H);
    ctx.stroke();
  }
  for (let y = startY; y < br.y; y += step) {
    const s = worldToScreen(0, y);
    ctx.beginPath();
    ctx.moveTo(0, s.y);
    ctx.lineTo(W, s.y);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

// ─── Recovery sushi belt loop ──────────────────────────────────
function drawRecoverySushiBelt(t) {
  if (!factoryData || !factoryData.pairs) return;
  factoryData.pairs.forEach(pair => {
    const recovNode = nodes.find(n => n.pair === pair.pair_name && n.mid === "recovery_monitor");
    if (!recovNode || !pair.machines.recovery_monitor) return;
    const orders = pair.machines.recovery_monitor.orders || [];
    if (orders.length === 0) return;

    // Draw a small looping belt around the recovery monitor
    const cx = recovNode.x + recovNode.w / 2;
    const cy = recovNode.y + recovNode.h / 2;
    const rx = recovNode.w * 0.7;
    const ry = recovNode.h * 0.8;

    ctx.strokeStyle = C.beltTrack;
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
    ctx.stroke();

    // Animated dash
    ctx.strokeStyle = C.beltDash;
    ctx.lineWidth = 4;
    ctx.setLineDash([4, 8]);
    ctx.lineDashOffset = -(t * 30) % 12;
    ctx.beginPath();
    ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);

    // Items on the loop
    orders.forEach((order, i) => {
      const angle = (t * 0.3 + (i / orders.length) * Math.PI * 2) % (Math.PI * 2);
      const ix = cx + rx * Math.cos(angle);
      const iy = cy + ry * Math.sin(angle);
      ctx.beginPath();
      ctx.arc(ix, iy, 5, 0, Math.PI * 2);
      ctx.fillStyle = C.itemRecov;
      ctx.fill();
      // Tiny label
      if (cam.zoom > 0.6) {
        ctx.font = "7px monospace";
        ctx.fillStyle = C.sublabel;
        ctx.textAlign = "center";
        ctx.fillText(order.trade_id + ":" + order.cycle, ix, iy - 8);
      }
    });
  });
}

// ─── AI combinator indicators ──────────────────────────────────
function drawAICombinators(t) {
  if (!factoryData || !factoryData.pairs) return;
  factoryData.pairs.forEach(pair => {
    const aiNode = nodes.find(n => n.pair === pair.pair_name && n.mid === "ai_council");
    if (!aiNode || !pair.machines.ai_council) return;
    const panelists = pair.machines.ai_council.panelists || [];
    if (panelists.length === 0) return;

    // Draw small combinator boxes above the AI council machine
    const startX = aiNode.x;
    const startY = aiNode.y - 30;
    const bw = 36, bh = 20, gap = 6;

    panelists.forEach((p, i) => {
      const bx = startX + i * (bw + gap);
      const by = startY;
      roundRect(bx, by, bw, bh, 3);
      ctx.fillStyle = "#1a1a2e";
      ctx.fill();
      const voteColor = p.action === "continue" ? C.idle : (p.action ? C.active : C.sublabel);
      ctx.strokeStyle = voteColor;
      ctx.lineWidth = 1.5;
      ctx.stroke();

      // Panelist name
      ctx.font = "7px monospace";
      ctx.fillStyle = C.sublabel;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const shortName = (p.name || "?").substring(0, 5);
      ctx.fillText(shortName, bx + bw/2, by + bh/2);

      // Wire from combinator to AI machine
      ctx.strokeStyle = voteColor;
      ctx.lineWidth = 1;
      ctx.globalAlpha = 0.6;
      ctx.beginPath();
      ctx.moveTo(bx + bw/2, by + bh);
      ctx.lineTo(aiNode.x + aiNode.w/2, aiNode.y);
      ctx.stroke();
      ctx.globalAlpha = 1;
    });
  });
}

// ─── Detail panel ──────────────────────────────────────────────
const detailEl = document.getElementById("detail");
const detailTitle = document.getElementById("detail-title");
const detailSubtitle = document.getElementById("detail-subtitle");
const detailSource = document.getElementById("detail-source");
const detailBody = document.getElementById("detail-body");

function openDetail(node) {
  selectedNode = node;
  detailEl.classList.add("open");
  updateDetail();
}

function closeDetail() {
  selectedNode = null;
  detailEl.classList.remove("open");
}

function updateDetail() {
  if (!selectedNode) return;
  const node = selectedNode;
  const reg = MACHINE_REG[node.mid] || {};

  detailTitle.textContent = node.label;
  detailSubtitle.textContent = node.pairDisplay + " \u2014 " + (node.state || "idle");
  detailSource.textContent = (reg.file || "") + " \u2192 " + (reg.sub || "");

  let html = '<div class="section"><h3>Description</h3><p style="color:#c9d1d9;font-size:12px;line-height:1.5">' + (reg.desc || "No description") + '</p></div>';

  // Parameter table
  const d = node.data || {};
  const entries = Object.entries(d).filter(([k]) => k !== "state");
  if (entries.length > 0) {
    html += '<div class="section"><h3>Live Parameters</h3><table>';
    entries.forEach(([k, v]) => {
      let display;
      if (v === null || v === undefined) display = '<span style="color:#8b949e">null</span>';
      else if (typeof v === "boolean") display = v ? '<span style="color:#3fb950">true</span>' : '<span style="color:#f85149">false</span>';
      else if (typeof v === "number") display = '<span style="color:#e3b341">' + v + '</span>';
      else if (Array.isArray(v)) display = '<span style="color:#58a6ff">[' + v.length + ' items]</span>';
      else if (typeof v === "object") display = '<span style="color:#58a6ff">{...}</span>';
      else display = String(v);
      html += '<tr><td>' + k.replace(/_/g, " ") + '</td><td>' + display + '</td></tr>';
    });
    html += '</table></div>';
  }

  // Machine-specific extra sections
  if (node.mid === "ai_council" && d.panelists && d.panelists.length > 0) {
    html += '<div class="section"><h3>Panelist Votes</h3><table>';
    d.panelists.forEach(p => {
      const dotColor = p.action === "continue" ? "idle" : "active";
      html += '<tr><td><span class="status-dot status-' + dotColor + '"></span>' + p.name + '</td><td>' + (p.action||"?") + '</td></tr>';
    });
    html += '</table></div>';
  }

  if (node.mid === "recovery_monitor" && d.orders && d.orders.length > 0) {
    html += '<div class="section"><h3>Recovery Orders</h3><table>';
    html += '<tr><td style="color:#8b949e">ID</td><td style="color:#8b949e">Age</td></tr>';
    d.orders.forEach(o => {
      const ageMin = Math.round((o.age_sec||0)/60);
      html += '<tr><td>' + o.trade_id + ':' + o.cycle + '</td><td>' + ageMin + 'm ($' + (o.unrealized_pnl||0).toFixed(4) + ')</td></tr>';
    });
    html += '</table></div>';
  }

  if (node.mid === "profit_chest" && d.unrealized) {
    html += '<div class="section"><h3>Unrealized P&L</h3><table>';
    for (const [k, v] of Object.entries(d.unrealized)) {
      const color = v >= 0 ? "#3fb950" : "#f85149";
      html += '<tr><td>' + k + '</td><td style="color:' + color + '">$' + (typeof v === "number" ? v.toFixed(4) : v) + '</td></tr>';
    }
    html += '</table></div>';
  }

  detailBody.innerHTML = html;
}

document.getElementById("detail-close").addEventListener("click", closeDetail);

// ─── Hit testing ───────────────────────────────────────────────
function hitTest(sx, sy) {
  const w = screenToWorld(sx, sy);

  // Check machine nodes (reverse order = top-most first)
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    if (n.isDiamond) {
      // Diamond hit test
      const cx = n.x + n.w/2, cy = n.y + n.h/2;
      const dw = n.w * 0.9, dh = n.h * 1.3;
      const dx = Math.abs(w.x - cx) / (dw/2);
      const dy = Math.abs(w.y - cy) / (dh/2);
      if (dx + dy <= 1) return n;
    } else {
      if (w.x >= n.x && w.x <= n.x + n.w && w.y >= n.y && w.y <= n.y + n.h) return n;
    }
  }
  return null;
}

let hoveredPairLayout = null;

function hitTestPairLayout(sx, sy) {
  const w = screenToWorld(sx, sy);
  for (let i = pairLayouts.length - 1; i >= 0; i--) {
    const pl = pairLayouts[i];
    if (w.x >= pl.ox && w.x <= pl.ox + pl.w && w.y >= pl.oy && w.y <= pl.oy + pl.h) return pl;
  }
  return null;
}

// ─── Pan/Zoom ──────────────────────────────────────────────────
let isDragging = false;
let dragStart = { x: 0, y: 0 };
let camStart = { x: 0, y: 0 };

canvas.addEventListener("mousedown", (e) => {
  isDragging = true;
  dragStart = { x: e.clientX, y: e.clientY };
  camStart = { x: cam.x, y: cam.y };
  canvas.classList.add("dragging");
  targetCam = null; // cancel any animation
});

window.addEventListener("mousemove", (e) => {
  if (isDragging) {
    const dx = (e.clientX - dragStart.x) / cam.zoom;
    const dy = (e.clientY - dragStart.y) / cam.zoom;
    cam.x = camStart.x - dx;
    cam.y = camStart.y - dy;
  }
  // Hover
  const n = hitTest(e.clientX, e.clientY);
  hoveredNode = n;
  hoveredPairLayout = !n ? hitTestPairLayout(e.clientX, e.clientY) : null;
  canvas.style.cursor = (n || hoveredPairLayout) ? "pointer" : (isDragging ? "grabbing" : "grab");
});

window.addEventListener("mouseup", () => {
  isDragging = false;
  canvas.classList.remove("dragging");
});

canvas.addEventListener("click", (e) => {
  if (Math.abs(e.clientX - dragStart.x) > 5 || Math.abs(e.clientY - dragStart.y) > 5) return; // was drag

  const n = hitTest(e.clientX, e.clientY);
  if (n) {
    openDetail(n);
    return;
  }

  // World map: click outpost to zoom in
  if (cam.zoom < 0.4) {
    const pl = hitTestPairLayout(e.clientX, e.clientY);
    if (pl) {
      zoomToPair(pl);
      return;
    }
  }

  closeDetail();
});

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  targetCam = null;
  const factor = e.deltaY > 0 ? 0.88 : 1.14;
  const newZoom = Math.max(0.05, Math.min(3, cam.zoom * factor));

  // Zoom toward cursor
  const wx = (e.clientX - W/2) / cam.zoom + cam.x;
  const wy = (e.clientY - H/2) / cam.zoom + cam.y;
  cam.zoom = newZoom;
  cam.x = wx - (e.clientX - W/2) / newZoom;
  cam.y = wy - (e.clientY - H/2) / newZoom;
}, { passive: false });

// ─── Navigation ────────────────────────────────────────────────
function zoomToPair(pl) {
  selectedPair = pl.pair_name;
  targetCam = {
    x: pl.ox + pl.w / 2,
    y: pl.oy + pl.h / 2,
    zoom: Math.min(W / (pl.w + 80), H / (pl.h + 80)) * 0.85,
  };
  document.getElementById("btn-world").classList.remove("active");
}

function zoomToWorld() {
  selectedPair = null;
  if (pairLayouts.length === 0) return;
  const all = pairLayouts;
  const minX = Math.min(...all.map(p => p.ox));
  const maxX = Math.max(...all.map(p => p.ox + p.w));
  const minY = Math.min(...all.map(p => p.oy));
  const maxY = Math.max(...all.map(p => p.oy + p.h));
  const cw = maxX - minX + 200;
  const ch = maxY - minY + 200;
  targetCam = {
    x: (minX + maxX) / 2,
    y: (minY + maxY) / 2,
    zoom: Math.min(W / cw, H / ch) * 0.9,
  };
  document.getElementById("btn-world").classList.add("active");
}

function fitView() {
  if (pairLayouts.length === 1) {
    zoomToPair(pairLayouts[0]);
  } else {
    zoomToWorld();
  }
}

document.getElementById("btn-world").addEventListener("click", zoomToWorld);
document.getElementById("btn-fit").addEventListener("click", fitView);
document.getElementById("btn-reset").addEventListener("click", () => {
  targetCam = { x: cam.x, y: cam.y, zoom: 0.7 };
});

// ─── Smooth camera transitions ─────────────────────────────────
function updateCamera(dt) {
  if (!targetCam) return;
  const lerp = 1 - Math.pow(0.01, dt);
  cam.x += (targetCam.x - cam.x) * lerp;
  cam.y += (targetCam.y - cam.y) * lerp;
  cam.zoom += (targetCam.zoom - cam.zoom) * lerp;
  if (Math.abs(cam.x - targetCam.x) < 0.5 &&
      Math.abs(cam.y - targetCam.y) < 0.5 &&
      Math.abs(cam.zoom - targetCam.zoom) < 0.001) {
    cam.x = targetCam.x;
    cam.y = targetCam.y;
    cam.zoom = targetCam.zoom;
    targetCam = null;
  }
}

// ─── Zoom level label ──────────────────────────────────────────
function getZoomLevelName() {
  if (cam.zoom < 0.3) return "World Map";
  if (cam.zoom < 1.5) return "Pair Factory";
  return "Machine Detail";
}

// ─── Main render loop ──────────────────────────────────────────
let lastTime = 0;

function render(timestamp) {
  const dt = Math.min((timestamp - lastTime) / 1000, 0.1);
  lastTime = timestamp;
  animTime += dt;

  updateCamera(dt);

  // Clear
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, W, H);

  // Apply camera
  ctx.save();
  ctx.translate(W/2, H/2);
  ctx.scale(cam.zoom, cam.zoom);
  ctx.translate(-cam.x, -cam.y);

  // Grid background
  drawGrid();

  const zoomLevel = cam.zoom;
  const isWorldView = zoomLevel < 0.3;

  if (isWorldView && pairLayouts.length > 1) {
    // World map: draw outposts
    pairLayouts.forEach(pl => drawOutpost(pl, animTime));

    // Train lines between outposts
    ctx.strokeStyle = C.beltTrack;
    ctx.lineWidth = 4;
    ctx.setLineDash([8, 12]);
    for (let i = 0; i < pairLayouts.length - 1; i++) {
      const a = pairLayouts[i], b = pairLayouts[i+1];
      ctx.beginPath();
      ctx.moveTo(a.ox + a.w, a.oy + a.h/2);
      ctx.lineTo(b.ox, b.oy + b.h/2);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  } else {
    // Pair factory view: draw belts, machines, overlays

    // Factory outpost borders (subtle)
    pairLayouts.forEach(pl => {
      roundRect(pl.ox - 20, pl.oy - 20, pl.w + 40, pl.h + 40, 16);
      ctx.strokeStyle = C.grid;
      ctx.lineWidth = 1;
      ctx.stroke();
      // Pair label top-left
      ctx.font = "bold 16px 'Cascadia Mono','Fira Code',monospace";
      ctx.fillStyle = C.sublabel;
      ctx.textAlign = "left";
      ctx.fillText(pl.pair_display + "  $" + (pl.price||0).toFixed(6), pl.ox, pl.oy - 30);
    });

    // Belts (behind machines)
    beltPaths.forEach(b => drawBelt(b, animTime));

    // Power wires
    if (zoomLevel > 0.4) drawWires(animTime);

    // Machines
    nodes.forEach(n => drawMachine(n, animTime));

    // Recovery sushi belt
    if (zoomLevel > 0.5) drawRecoverySushiBelt(animTime);

    // AI combinators
    if (zoomLevel > 0.5) drawAICombinators(animTime);
  }

  ctx.restore();

  // HUD
  updateHUD();

  // Zoom label
  document.getElementById("zoom-label").textContent = getZoomLevelName() + " (" + (cam.zoom * 100).toFixed(0) + "%)";

  // Update detail panel if open
  if (selectedNode && detailEl.classList.contains("open")) {
    updateDetail();
  }

  requestAnimationFrame(render);
}

function updateHUD() {
  if (!factoryData || !factoryData.pairs) {
    document.getElementById("hud-info").textContent = "Connecting...";
    return;
  }
  const pairs = factoryData.pairs;
  const totalProfit = pairs.reduce((s, p) => s + (p.machines.profit_chest?.total_profit || 0), 0);
  const totalTrips = pairs.reduce((s, p) => s + (p.machines.profit_chest?.total_round_trips || 0), 0);
  const pairCount = pairs.length;
  document.getElementById("hud-info").textContent =
    pairCount + " pair" + (pairCount !== 1 ? "s" : "") +
    " | $" + totalProfit.toFixed(2) + " total" +
    " | " + totalTrips + " trips";
}

// ─── SSE data connection ───────────────────────────────────────
let evtSource = null;

function handleFullState(data) {
  if (nodes.length === 0) {
    buildScene(data);
    if (pairLayouts.length === 1) zoomToPair(pairLayouts[0]);
    else if (pairLayouts.length > 1) zoomToWorld();
  } else {
    updateSceneData(data);
  }
}

function handleDiff(data) {
  // Diff only contains changed machines -- merge into existing factoryData
  if (!factoryData || nodes.length === 0) { handleFullState(data); return; }
  updateSceneData(data);  // updateSceneData already handles partial machine updates
}

function connectSSE() {
  if (evtSource) { try { evtSource.close(); } catch(e) {} }
  evtSource = new EventSource("/api/factory/stream");

  // Named events: "full" = complete state, "diff" = only changes
  evtSource.addEventListener("full", function(e) {
    try {
      const data = JSON.parse(e.data);
      if (!data.error) handleFullState(data);
    } catch(err) { console.error("SSE full parse error:", err); }
  });

  evtSource.addEventListener("diff", function(e) {
    try {
      const data = JSON.parse(e.data);
      if (!data.error) handleDiff(data);
    } catch(err) { console.error("SSE diff parse error:", err); }
  });

  // Fallback for unnamed events (backwards compat)
  evtSource.onmessage = function(e) {
    try {
      const data = JSON.parse(e.data);
      if (!data.error) handleFullState(data);
    } catch(err) {}
  };

  evtSource.onerror = function() {
    setTimeout(connectSSE, 5000);
  };
}

// Also fetch initial data via REST (faster than waiting for first SSE tick)
fetch("/api/factory/status")
  .then(r => r.json())
  .then(data => {
    if (!data.error) {
      buildScene(data);
      if (pairLayouts.length === 1) {
        zoomToPair(pairLayouts[0]);
      } else if (pairLayouts.length > 1) {
        zoomToWorld();
      }
    }
  })
  .catch(() => {});

connectSSE();

// ─── Keyboard shortcuts ────────────────────────────────────────
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDetail();
  if (e.key === "w" || e.key === "W") zoomToWorld();
  if (e.key === "f" || e.key === "F") fitView();
});

// ─── Start ─────────────────────────────────────────────────────
requestAnimationFrame(render);

})();
</script>
</body>
</html>"""
