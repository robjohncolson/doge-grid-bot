"""
DOGE Bot v1 dashboard HTML.

The server injects this page at `/`.
Frontend talks to:
- GET  /api/status
- POST /api/action
"""

DASHBOARD_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>DOGE Bot v1</title>
  <style>
    :root {
      --bg: #0d1117;
      --panel: #161b22;
      --ink: #e6edf3;
      --muted: #8b949e;
      --good: #2ea043;
      --bad: #f85149;
      --warn: #d29922;
      --line: #30363d;
      --accent: #58a6ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at 20% -20%, #1f2a44, #0d1117 35%) fixed;
    }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 16px; }
    .top {
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .title { font-weight: 800; letter-spacing: .4px; }
    .badges { display: flex; gap: 8px; flex-wrap: wrap; }
    .badge {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 10px;
      font-size: 12px;
      color: var(--muted);
      background: rgba(255,255,255,.02);
    }
    .badge.ok { color: var(--good); border-color: rgba(46,160,67,.45); }
    .badge.pause { color: var(--warn); border-color: rgba(210,153,34,.45); }
    .badge.halt { color: var(--bad); border-color: rgba(248,81,73,.5); }

    .grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }
    @media (min-width: 960px) {
      .grid { grid-template-columns: 320px 1fr; }
    }
    .panel {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: linear-gradient(180deg, rgba(255,255,255,.02), rgba(255,255,255,.01));
      padding: 12px;
    }
    .panel h3 { margin: 0 0 10px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }
    .row { display: flex; justify-content: space-between; gap: 8px; margin: 6px 0; }
    .k { color: var(--muted); font-size: 13px; }
    .v { font-variant-numeric: tabular-nums; font-size: 13px; }

    .controls { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .controls input {
      width: 100%;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #0f141b;
      color: var(--ink);
      padding: 8px;
    }
    button {
      border: 1px solid var(--line);
      background: #1f2733;
      color: var(--ink);
      border-radius: 8px;
      padding: 8px 10px;
      cursor: pointer;
      font-weight: 600;
    }
    button:hover { border-color: var(--accent); }
    button.wide { width: 100%; }

    .slots { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
    .slot {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      cursor: pointer;
      color: var(--muted);
      font-size: 12px;
    }
    .slot.active { color: var(--ink); border-color: var(--accent); background: rgba(88,166,255,.12); }

    .statebar {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      margin-bottom: 10px;
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .statepill {
      border-radius: 999px;
      padding: 2px 10px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid var(--line);
    }
    .S0 { color: var(--accent); }
    .S1a, .S1b { color: var(--warn); }
    .S2 { color: var(--bad); }

    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid #242c36; padding: 6px 4px; text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .tiny { font-size: 11px; color: var(--muted); }
    .right { text-align: right; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"top\">
      <div class=\"title\">DOGE/USD State-Machine Bot v1</div>
      <div class=\"badges\">
        <span id=\"mode\" class=\"badge\">MODE</span>
        <span id=\"phase\" class=\"badge\">PHASE</span>
        <span id=\"priceAge\" class=\"badge\">PRICE</span>
      </div>
    </div>

    <div class=\"grid\">
      <div class=\"panel\">
        <h3>Controls</h3>
        <div class=\"controls\">
          <button id=\"pauseBtn\">Pause</button>
          <button id=\"resumeBtn\">Resume</button>
          <button id=\"addSlotBtn\">Add Slot</button>
          <button id=\"softCloseBtn\">Soft Close</button>
        </div>

        <div style=\"height:10px\"></div>

        <div class=\"k\">Entry %</div>
        <div style=\"display:flex;gap:8px\">
          <input id=\"entryInput\" type=\"number\" step=\"0.01\" min=\"0.05\" />
          <button id=\"setEntryBtn\">Set</button>
        </div>

        <div style=\"height:8px\"></div>

        <div class=\"k\">Profit %</div>
        <div style=\"display:flex;gap:8px\">
          <input id=\"profitInput\" type=\"number\" step=\"0.01\" min=\"0.05\" />
          <button id=\"setProfitBtn\">Set</button>
        </div>

        <div style=\"height:10px\"></div>
        <div id=\"ctlMsg\" class=\"tiny\"></div>

        <h3 style=\"margin-top:14px\">Summary</h3>
        <div class=\"row\"><span class=\"k\">Pair</span><span id=\"pair\" class=\"v mono\"></span></div>
        <div class=\"row\"><span class=\"k\">Slots</span><span id=\"slotCount\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Round Trips</span><span id=\"totalTrips\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Realized PnL (USD)</span><span id=\"totalPnl\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Realized PnL (DOGE eq)</span><span id=\"totalPnlDoge\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Unrealized PnL</span><span id=\"totalUnrealized\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Today Loss</span><span id=\"todayLoss\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Orphans</span><span id=\"orphans\" class=\"v\"></span></div>

        <h3 style=\"margin-top:14px\">Capacity &amp; Fill Health</h3>
        <div class=\"row\"><span class=\"k\">Status</span><span id=\"cfhBand\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Open Orders</span><span id=\"cfhOpenOrders\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Headroom</span><span id=\"cfhHeadroom\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Slots Runway</span><span id=\"cfhRunway\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Partial Open (1d)</span><span id=\"cfhPartialOpen\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Partial Cancel (1d)</span><span id=\"cfhPartialCancel\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Fill Latency (1d)</span><span id=\"cfhFillLatency\" class=\"v\"></span></div>
        <div id=\"cfhHints\" class=\"tiny\"></div>
      </div>

      <div class=\"panel\">
        <h3>Slots</h3>
        <div id=\"slots\" class=\"slots\"></div>
        <div id=\"stateBar\" class=\"statebar\"></div>

        <div class=\"row\"><span class=\"k\">Order Size USD</span><span id=\"orderUsd\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Runtime Profit %</span><span id=\"runtimeProfit\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Slot Realized</span><span id=\"slotRealized\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Slot Unrealized</span><span id=\"slotUnrealized\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Slot Round Trips</span><span id=\"slotTrips\" class=\"v\"></span></div>

        <h3 style=\"margin-top:12px\">Open Orders</h3>
        <table>
          <thead>
            <tr><th>Type</th><th>Trade</th><th>Cycle</th><th>Volume</th><th>Price</th><th>Txid</th></tr>
          </thead>
          <tbody id=\"ordersBody\"></tbody>
        </table>

        <h3 style=\"margin-top:12px\">Orphaned Exits</h3>
        <table>
          <thead>
            <tr><th>ID</th><th>Trade</th><th>Side</th><th>Age</th><th>Dist%</th><th>Price</th><th></th></tr>
          </thead>
          <tbody id=\"orphansBody\"></tbody>
        </table>

        <h3 style=\"margin-top:12px\">Recent Cycles</h3>
        <table>
          <thead>
            <tr><th>Trade</th><th>Cycle</th><th>Entry</th><th>Exit</th><th>Net</th><th>Rec</th></tr>
          </thead>
          <tbody id=\"cyclesBody\"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    let state = null;
    let selectedSlot = 0;

    function fmt(n, d=6) {
      if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
      return Number(n).toFixed(d);
    }

    async function api(path, opts={}) {
      const res = await fetch(path, opts);
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }

    async function act(action, payload={}) {
      const body = JSON.stringify({action, ...payload});
      const out = await api('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body});
      document.getElementById('ctlMsg').textContent = out.message || 'ok';
      await refresh();
    }

    function renderTop(s) {
      const mode = document.getElementById('mode');
      mode.textContent = s.mode;
      mode.className = 'badge ' + (s.mode === 'RUNNING' ? 'ok' : s.mode === 'PAUSED' ? 'pause' : 'halt');

      const phase = document.getElementById('phase');
      phase.textContent = `PHASE ${s.top_phase}`;
      phase.className = 'badge';

      const age = document.getElementById('priceAge');
      age.textContent = `PRICE AGE ${Math.round(s.price_age_sec)}s`;
      age.className = 'badge ' + (s.price_age_sec <= 60 ? 'ok' : 'halt');

      document.getElementById('pair').textContent = s.pair;
      document.getElementById('slotCount').textContent = s.slot_count;
      document.getElementById('totalTrips').textContent = s.total_round_trips ?? 0;
      document.getElementById('totalPnl').textContent = `$${fmt(s.total_profit, 6)}`;
      document.getElementById('totalPnlDoge').textContent = `${fmt(s.total_profit_doge, 3)} DOGE`;
      document.getElementById('totalUnrealized').textContent =
        `$${fmt(s.total_unrealized_profit, 6)} (${fmt(s.total_unrealized_doge, 3)} DOGE)`;
      document.getElementById('todayLoss').textContent = `$${fmt(s.today_realized_loss, 4)}`;
      document.getElementById('orphans').textContent = s.total_orphans;

      const cfh = s.capacity_fill_health || {};
      const band = String(cfh.status_band || '-').toUpperCase();
      const bandEl = document.getElementById('cfhBand');
      bandEl.textContent = band;
      bandEl.style.color = band === 'STOP' ? 'var(--bad)' : band === 'CAUTION' ? 'var(--warn)' : 'var(--good)';

      const utilPct = cfh.open_order_utilization_pct;
      const utilText = utilPct === null || utilPct === undefined ? '-' : `${fmt(utilPct, 1)}%`;
      const source = cfh.open_orders_source ? ` ${String(cfh.open_orders_source)}` : '';
      document.getElementById('cfhOpenOrders').textContent =
        `${cfh.open_orders_current ?? '-'} / ${cfh.open_orders_safe_cap ?? '-'} (${utilText}${source ? ', ' + source : ''})`;
      document.getElementById('cfhHeadroom').textContent = String(cfh.open_order_headroom ?? '-');
      document.getElementById('cfhRunway').textContent = String(cfh.estimated_slots_remaining ?? '-');
      document.getElementById('cfhPartialOpen').textContent = String(cfh.partial_fill_open_events_1d ?? '-');
      document.getElementById('cfhPartialCancel').textContent = String(cfh.partial_fill_cancel_events_1d ?? '-');

      const med = cfh.median_fill_seconds_1d;
      const p95 = cfh.p95_fill_seconds_1d;
      document.getElementById('cfhFillLatency').textContent =
        (med === null || med === undefined || p95 === null || p95 === undefined)
          ? '-'
          : `${Math.round(Number(med))}s / ${Math.round(Number(p95))}s`;

      const hints = Array.isArray(cfh.blocked_risk_hint) ? cfh.blocked_risk_hint : [];
      document.getElementById('cfhHints').textContent = hints.length ? `Hints: ${hints.join(', ')}` : '';
    }

    function renderSlots(s) {
      const el = document.getElementById('slots');
      el.innerHTML = '';
      for (const slot of s.slots) {
        const b = document.createElement('button');
        b.className = 'slot' + (slot.slot_id === selectedSlot ? ' active' : '');
        b.textContent = `#${slot.slot_id} ${slot.phase}`;
        b.onclick = () => { selectedSlot = slot.slot_id; renderSelected(s); renderSlots(s); };
        el.appendChild(b);
      }
      if (!s.slots.find(x => x.slot_id === selectedSlot) && s.slots.length) {
        selectedSlot = s.slots[0].slot_id;
      }
    }

    function renderSelected(s) {
      const slot = s.slots.find(x => x.slot_id === selectedSlot) || s.slots[0];
      if (!slot) return;

      const sb = document.getElementById('stateBar');
      sb.innerHTML = `
        <span class=\"statepill ${slot.phase}\">${slot.phase}</span>
        <span class=\"tiny\">price $${fmt(slot.market_price, 6)}</span>
        <span class=\"tiny\">A.${slot.cycle_a} / B.${slot.cycle_b}</span>
        <span class=\"tiny\">open ${slot.open_orders.length}</span>
      `;

      document.getElementById('orderUsd').textContent = `$${fmt(slot.order_size_usd, 4)}`;
      document.getElementById('runtimeProfit').textContent = `${fmt(slot.profit_pct_runtime, 3)}%`;
      document.getElementById('slotRealized').textContent =
        `$${fmt(slot.total_profit, 6)} (${fmt(slot.total_profit_doge, 3)} DOGE)`;
      document.getElementById('slotUnrealized').textContent =
        `$${fmt(slot.unrealized_profit, 6)} (${fmt(slot.unrealized_profit_doge, 3)} DOGE)`;
      document.getElementById('slotTrips').textContent = slot.total_round_trips ?? 0;

      const ob = document.getElementById('ordersBody');
      ob.innerHTML = '';
      for (const o of slot.open_orders) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${o.side}/${o.role}</td><td>${o.trade_id}</td><td>${o.cycle}</td><td>${fmt(o.volume, 4)}</td><td>$${fmt(o.price, 6)}</td><td class=\"mono tiny\">${o.txid || '-'}</td>`;
        ob.appendChild(tr);
      }

      const rb = document.getElementById('orphansBody');
      rb.innerHTML = '';
      for (const r of slot.recovery_orders) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${r.recovery_id}</td>
          <td>${r.trade_id}.${r.cycle}</td>
          <td>${r.side}</td>
          <td>${Math.round(r.age_sec)}s</td>
          <td>${fmt(r.distance_pct, 3)}</td>
          <td>$${fmt(r.price, 6)}</td>
          <td><button data-rid=\"${r.recovery_id}\">close</button></td>
        `;
        tr.querySelector('button').onclick = () => act('soft_close', {slot_id: slot.slot_id, recovery_id: r.recovery_id});
        rb.appendChild(tr);
      }

      const cb = document.getElementById('cyclesBody');
      cb.innerHTML = '';
      for (const c of slot.recent_cycles) {
        const tr = document.createElement('tr');
        const clr = c.net_profit >= 0 ? 'var(--good)' : 'var(--bad)';
        tr.innerHTML = `<td>${c.trade_id}</td><td>${c.cycle}</td><td>$${fmt(c.entry_price, 6)}</td><td>$${fmt(c.exit_price, 6)}</td><td style=\"color:${clr}\">$${fmt(c.net_profit, 4)}</td><td>${c.from_recovery ? 'yes' : 'no'}</td>`;
        cb.appendChild(tr);
      }

      document.getElementById('entryInput').value = fmt(s.entry_pct, 3);
      document.getElementById('profitInput').value = fmt(s.profit_pct, 3);
    }

    async function refresh() {
      try {
        state = await api('/api/status');
        renderTop(state);
        renderSlots(state);
        renderSelected(state);
      } catch (e) {
        document.getElementById('ctlMsg').textContent = e.message;
      }
    }

    document.getElementById('pauseBtn').onclick = () => act('pause');
    document.getElementById('resumeBtn').onclick = () => act('resume');
    document.getElementById('addSlotBtn').onclick = () => act('add_slot');
    document.getElementById('softCloseBtn').onclick = () => act('soft_close_next');
    document.getElementById('setEntryBtn').onclick = () => act('set_entry_pct', {value: Number(document.getElementById('entryInput').value)});
    document.getElementById('setProfitBtn').onclick = () => act('set_profit_pct', {value: Number(document.getElementById('profitInput').value)});

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""
