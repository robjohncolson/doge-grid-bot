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
        orders.append({
            "level": o.level,
            "side": o.side,
            "price": round(o.price, 6),
            "volume": round(o.volume, 2),
            "status": o.status,
        })
    orders.sort(key=lambda x: x["price"], reverse=True)

    # -- Trend ratio --
    cutoff = now - grid_strategy.TREND_WINDOW_SECONDS
    buy_12h = sum(1 for f in state.recent_fills
                  if f.get("time", 0) > cutoff and f["side"] == "buy")
    sell_12h = sum(1 for f in state.recent_fills
                   if f.get("time", 0) > cutoff and f["side"] == "sell")
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

    return {
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
        "trend": {
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
            "order_size": config.ORDER_SIZE_USD,
            "grid_levels": config.GRID_LEVELS,
            "spacing_pct": config.GRID_SPACING_PCT,
            "effective_capital": round(effective_capital, 2),
            "starting_capital": config.STARTING_CAPITAL,
            "ai_interval": config.AI_ADVISOR_INTERVAL,
            "round_trip_fee_pct": config.ROUND_TRIP_FEE_PCT,
            "min_spacing": round(config.ROUND_TRIP_FEE_PCT + 0.1, 2),
        },
        "recent_fills": recent,
        "ai_recommendation": state.ai_recommendation or "No recommendation yet",
        "uptime": uptime,
        "mode": "dry_run" if config.DRY_RUN else "live",
        "paused": state.is_paused,
        "pause_reason": state.pause_reason,
        # New: chart data
        "charts": {
            "price": price_chart,
            "fill_rate": fill_rate_chart,
            "profits": profit_chart,
        },
        # New: stats results
        "stats": state.stats_results if state.stats_results else {},
    }


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DOGE Grid Bot</title>
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
</style>
</head>
<body>

<div class="header">
  <h1>DOGE Grid Bot</h1>
  <span id="badge" class="badge badge-dry">---</span>
  <button class="audio-btn" id="audio-btn" onclick="toggleAudio()" title="Toggle audio alerts">&#x1f507;</button>
  <span class="uptime" id="uptime">--</span>
</div>

<div class="cards">
  <div class="card"><div class="label">Price</div><div class="value" id="c-price">--</div><div class="sub" id="c-price-sub">--</div></div>
  <div class="card"><div class="label">Center</div><div class="value" id="c-center">--</div><div class="sub" id="c-center-sub">--</div></div>
  <div class="card"><div class="label">Today P&amp;L</div><div class="value" id="c-today">--</div><div class="sub" id="c-today-sub">--</div></div>
  <div class="card"><div class="label">Total P&amp;L</div><div class="value" id="c-total">--</div><div class="sub" id="c-total-sub">--</div></div>
  <div class="card"><div class="label">Round Trips</div><div class="value" id="c-trips">--</div><div class="sub" id="c-trips-sub">--</div></div>
  <div class="card"><div class="label">DOGE Accumulated</div><div class="value" id="c-doge">--</div><div class="sub" id="c-doge-sub">&nbsp;</div></div>
</div>

<!-- Charts -->
<div class="charts-row">
  <div class="chart-box"><h3>Price (24h)</h3><canvas id="chart-price"></canvas></div>
  <div class="chart-box"><h3>Fill Rate (hourly, 24h)</h3><canvas id="chart-fills"></canvas></div>
  <div class="chart-box"><h3>Round Trip Profits</h3><canvas id="chart-profits"></canvas></div>
</div>

<!-- Strategy Health Banner -->
<div class="health-banner" id="health-banner">
  <div class="hb-verdict" id="hb-verdict">Collecting data...</div>
  <div class="hb-summary" id="hb-summary">Statistical analyzers need more fills to produce results</div>
</div>

<!-- Statistical Advisory Board -->
<div class="advisory" id="advisory"></div>

<div class="sections">
  <!-- Grid ladder -->
  <div class="section">
    <h2>Grid Ladder</h2>
    <div class="ladder" id="ladder-wrap">
      <table><thead><tr><th>Lvl</th><th>Side</th><th>Price</th><th>Volume</th><th>Status</th></tr></thead>
      <tbody id="ladder-body"></tbody></table>
    </div>
  </div>

  <!-- Right column -->
  <div class="section">
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

    <h2 style="margin-top:16px">Adaptive Parameters</h2>
    <table class="params">
      <tr><td>Order size</td><td id="p-size">--</td></tr>
      <tr><td>Grid levels</td><td id="p-levels">--</td></tr>
      <tr><td>Spacing</td><td id="p-spacing">--</td></tr>
      <tr><td>Effective capital</td><td id="p-capital">--</td></tr>
      <tr><td>Fees (round trip)</td><td id="p-fees">--</td></tr>
      <tr><td>AI interval</td><td id="p-ai">--</td></tr>
      <tr><td>AI recommendation</td><td id="p-airec" class="ai-rec">--</td></tr>
    </table>

    <h2 style="margin-top:16px">Controls</h2>
    <div class="controls">
      <div class="ctrl-row">
        <label>Spacing %</label>
        <input type="number" id="in-spacing" step="0.1" min="0.6">
        <button onclick="applySpacing()">Apply</button>
      </div>
      <div class="ctrl-row">
        <label>Ratio</label>
        <input type="number" id="in-ratio" step="0.05" min="0.25" max="0.75">
        <button onclick="applyRatio()">Apply</button>
        <button class="btn-auto" onclick="applyRatioAuto()">Auto</button>
      </div>
      <div class="ctrl-row">
        <label>AI sec</label>
        <input type="number" id="in-interval" step="60" min="60" max="86400">
        <button onclick="applyInterval()">Apply</button>
      </div>
      <div id="ctrl-msg" class="ctrl-msg">&nbsp;</div>
    </div>
  </div>
</div>

<div class="sections">
  <div class="section" style="grid-column:1/-1">
    <h2>Recent Fills (last 20)</h2>
    <div class="fills" id="fills-wrap">
      <table><thead><tr><th>Time</th><th>Side</th><th>Price</th><th>Volume</th><th>Profit</th></tr></thead>
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
  return h + 'h ' + m + 'm';
}

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
  fill_rate: 'Volatility Regime',
  random_walk: 'Market Type',
};

function renderAdvisory(stats) {
  const board = document.getElementById('advisory');
  if (!stats || Object.keys(stats).length === 0) {
    board.innerHTML = '<div class="acard"><div class="ac-name">STATS ENGINE</div><div class="ac-summary">Collecting data -- analyzers will appear as fills accumulate</div></div>';
    return;
  }
  // Health banner
  const health = stats.overall_health || {};
  const hb = document.getElementById('health-banner');
  hb.className = 'health-banner hb-' + (health.color || 'yellow');
  document.getElementById('hb-verdict').textContent = (health.verdict || 'calibrating').toUpperCase().replace(/_/g, ' ');
  document.getElementById('hb-summary').textContent = health.summary || '';

  // Cards
  let html = '';
  const order = ['profitability', 'fill_asymmetry', 'grid_exceedance', 'fill_rate', 'random_walk'];
  for (const name of order) {
    const r = stats[name];
    if (!r) continue;
    const color = r.color || 'yellow';
    html += '<div class="acard ac-' + color + '">';
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
  document.getElementById('uptime').textContent = 'Up ' + fmtUptime(data.uptime);

  // Cards
  const p = data.price;
  document.getElementById('c-price').textContent = '$' + fmt(p.current, 6);
  document.getElementById('c-price-sub').textContent = 'drift ' + fmt(p.drift_pct, 2) + '%';
  document.getElementById('c-center').textContent = '$' + fmt(p.center, 6);
  document.getElementById('c-center-sub').textContent = 'grid center';
  const pr = data.profit;
  document.getElementById('c-today').textContent = fmtUSD(pr.today);
  document.getElementById('c-today-sub').textContent = pr.round_trips_today + ' trips today';
  document.getElementById('c-total').textContent = fmtUSD(pr.total);
  document.getElementById('c-total-sub').textContent = 'fees: ' + fmtUSD(pr.fees);
  document.getElementById('c-trips').textContent = pr.round_trips;
  document.getElementById('c-trips-sub').textContent = 'lifetime';
  document.getElementById('c-doge').textContent = fmt(pr.doge_accumulated, 2);

  // Charts
  if (data.charts) {
    drawSparkline('chart-price', data.charts.price, '#58a6ff');
    drawBars('chart-fills', data.charts.fill_rate, function(v) { return v > 0 ? '#58a6ff' : '#21262d'; });
    drawProfitDots('chart-profits', data.charts.profits);
  }

  // Stats advisory board
  renderAdvisory(data.stats);

  // Grid ladder
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
    rows += '<tr><td>' + (o.level > 0 ? '+' : '') + o.level + '</td>'
          + '<td class="' + cls + '">' + o.side.toUpperCase() + '</td>'
          + '<td>$' + fmt(o.price, 6) + '</td>'
          + '<td>' + fmt(o.volume, 2) + '</td>'
          + '<td>' + o.status + '</td></tr>';
  }
  if (!markerPlaced && data.grid.length > 0) {
    rows += '<tr class="marker"><td></td><td></td><td>$' + fmt(cp, 6) + '</td><td>PRICE</td><td></td></tr>';
  }
  tbody.innerHTML = rows;

  // Trend bar
  const t = data.trend;
  const buyBar = document.getElementById('trend-buy');
  const sellBar = document.getElementById('trend-sell');
  buyBar.style.width = t.buy_pct + '%';
  buyBar.textContent = t.buy_pct + '% Buy (' + t.grid_buys + ')';
  sellBar.style.width = t.sell_pct + '%';
  sellBar.textContent = t.sell_pct + '% Sell (' + t.grid_sells + ')';
  document.getElementById('trend-source').textContent = 'Source: ' + t.source;
  document.getElementById('trend-fills').textContent = t.buy_12h + ' buys / ' + t.sell_12h + ' sells (12h)';

  // Params
  const cfg = data.config;
  document.getElementById('p-size').textContent = '$' + fmt(cfg.order_size, 2);
  document.getElementById('p-levels').textContent = cfg.grid_levels + ' per side (' + (cfg.grid_levels * 2) + ' total)';
  document.getElementById('p-spacing').textContent = fmt(cfg.spacing_pct, 2) + '%';
  document.getElementById('p-capital').textContent = '$' + fmt(cfg.effective_capital, 2);
  document.getElementById('p-fees').textContent = fmt(cfg.round_trip_fee_pct, 2) + '%';
  document.getElementById('p-ai').textContent = cfg.ai_interval + 's (' + Math.round(cfg.ai_interval / 60) + ' min)';
  document.getElementById('p-airec').textContent = data.ai_recommendation;

  // Populate input placeholders
  const inS = document.getElementById('in-spacing');
  const inR = document.getElementById('in-ratio');
  const inI = document.getElementById('in-interval');
  if (!inS.value) inS.placeholder = fmt(cfg.spacing_pct, 2);
  if (!inR.value) inR.placeholder = fmt(t.ratio, 2);
  if (!inI.value) inI.placeholder = cfg.ai_interval;
  inS.min = cfg.min_spacing;

  // Recent fills (last 20)
  const fb = document.getElementById('fills-body');
  let frows = '';
  const fills = data.recent_fills.slice(0, 20);
  for (const f of fills) {
    const cls = f.side === 'buy' ? 'buy' : 'sell';
    const pcls = f.profit > 0 ? 'profit-pos' : (f.profit < 0 ? 'profit-neg' : '');
    frows += '<tr><td>' + fmtTime(f.time) + '</td>'
           + '<td class="' + cls + '">' + f.side.toUpperCase() + '</td>'
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
      document.getElementById('in-interval').value = '';
    } else { showMsg(d.error || 'Error', false); }
  } catch(e) { showMsg('Network error', false); }
}

function applySpacing() { const v = parseFloat(document.getElementById('in-spacing').value); if (isNaN(v)) return showMsg('Enter a spacing value', false); postConfig({spacing: v}); }
function applyRatio() { const v = parseFloat(document.getElementById('in-ratio').value); if (isNaN(v)) return showMsg('Enter a ratio value', false); postConfig({ratio: v}); }
function applyRatioAuto() { postConfig({ratio: 'auto'}); }
function applyInterval() { const v = parseInt(document.getElementById('in-interval').value); if (isNaN(v)) return showMsg('Enter an interval value', false); postConfig({interval: v}); }

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
// Initial fetch for immediate render, then start SSE
poll();
startSSE();
</script>
</body>
</html>
"""
