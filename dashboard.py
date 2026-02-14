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
      --cmd-bg: #161b22;
      --backdrop: rgba(0,0,0,0.5);
      --toast-bg: #1c2128;
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
    select {
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

    #kbMode {
      letter-spacing: .06em;
    }
    #cmdBar {
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      display: none;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      border-top: 1px solid var(--line);
      background: var(--cmd-bg);
      z-index: 1200;
    }
    #cmdBar.open { display: flex; }
    #cmdPrefix {
      color: var(--accent);
      font-size: 14px;
    }
    #cmdInput {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: #0f141b;
      color: var(--ink);
      font-size: 14px;
      outline: none;
    }
    #cmdInput:focus { border-color: var(--accent); }
    #cmdSuggestions {
      position: fixed;
      left: 14px;
      right: 14px;
      bottom: 54px;
      display: none;
      z-index: 1190;
      max-width: 600px;
    }
    #cmdSuggestions.open { display: block; }
    .cmd-suggestion {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 6px 8px;
      margin-top: 6px;
      background: var(--panel);
      color: var(--muted);
      font-size: 12px;
    }
    .cmd-suggestion.active {
      border-color: var(--accent);
      color: var(--ink);
      background: rgba(88,166,255,.12);
    }
    .overlay {
      position: fixed;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      background: var(--backdrop);
      z-index: 1100;
    }
    .overlay[hidden] { display: none; }
    .modal {
      width: min(720px, calc(100vw - 30px));
      max-height: calc(100vh - 60px);
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      background: var(--panel);
    }
    #confirmDialog .modal {
      width: min(420px, calc(100vw - 30px));
    }
    #confirmActions {
      margin-top: 12px;
      display: flex;
      justify-content: flex-end;
      gap: 8px;
    }
    #toasts {
      position: fixed;
      right: 16px;
      bottom: 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      z-index: 1250;
    }
    .toast {
      min-width: 220px;
      max-width: 360px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      padding: 8px 10px;
      background: var(--toast-bg);
      color: var(--ink);
      font-size: 12px;
      box-shadow: 0 8px 20px rgba(0,0,0,.35);
    }
    .toast.success { border-left-color: var(--good); }
    .toast.error { border-left-color: var(--bad); }
    .toast.info { border-left-color: var(--accent); }
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
        <span id=\"kbMode\" class=\"badge\">KB NORMAL</span>
      </div>
    </div>

    <div class=\"grid\">
      <div class=\"panel\">
        <h3>Controls</h3>
        <div class=\"controls\">
          <button id=\"pauseBtn\">Pause</button>
          <button id=\"resumeBtn\">Resume</button>
          <button id=\"addSlotBtn\">Add Slot</button>
          <button id=\"removeSlotBtn\" style=\"background:#c0392b\">Remove Slot</button>
          <button id=\"softCloseBtn\">Soft Close</button>
          <button id=\"reconcileBtn\">Reconcile Drift</button>
          <button id=\"cancelStaleBtn\">Refresh Recoveries</button>
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

        <h3 style=\"margin-top:14px\">Capital Layers</h3>
        <div class=\"k\">Funding Source</div>
        <div style=\"display:flex;gap:8px\">
          <select id=\"layerSourceSelect\">
            <option value=\"AUTO\">AUTO</option>
            <option value=\"DOGE\">DOGE</option>
            <option value=\"USD\">USD</option>
          </select>
          <button id=\"addLayerBtn\">Add Layer</button>
        </div>
        <div style=\"height:8px\"></div>
        <div style=\"display:flex;gap:8px\">
          <button id=\"removeLayerBtn\" class=\"wide\">Remove Layer</button>
        </div>
        <div class=\"row\"><span class=\"k\">Target Size</span><span id=\"layerTarget\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Funded Now</span><span id=\"layerFunded\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Step Size</span><span id=\"layerStep\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">USD Equiv Now</span><span id=\"layerUsdNow\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Propagation</span><span id=\"layerPropagation\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Funding Gap</span><span id=\"layerGap\" class=\"v\"></span></div>
        <div id=\"layerHint\" class=\"tiny\"></div>

        <div style=\"height:10px\"></div>

        <h3 style=\"margin-top:14px\">Summary</h3>
        <div class=\"row\"><span class=\"k\">Pair</span><span id=\"pair\" class=\"v mono\"></span></div>
        <div class=\"row\"><span class=\"k\">Slots</span><span id=\"slotCount\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Round Trips</span><span id=\"totalTrips\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Realized PnL (USD)</span><span id=\"totalPnl\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Realized PnL (DOGE eq)</span><span id=\"totalPnlDoge\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Unrealized PnL</span><span id=\"totalUnrealized\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Today Loss</span><span id=\"todayLoss\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">P&amp;L Audit</span><span id=\"pnlAudit\" class=\"v\"></span></div>
        <div id=\"pnlAuditDetails\" class=\"tiny\"></div>
        <div class=\"row\"><span class=\"k\">Orphans</span><span id=\"orphans\" class=\"v\"></span></div>

        <h3 style=\"margin-top:14px\">Capacity &amp; Fill Health</h3>
        <div class=\"row\"><span class=\"k\">Status</span><span id=\"cfhBand\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Open Orders</span><span id=\"cfhOpenOrders\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Headroom</span><span id=\"cfhHeadroom\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Slots Runway</span><span id=\"cfhRunway\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Partial Open (1d)</span><span id=\"cfhPartialOpen\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Partial Cancel (1d)</span><span id=\"cfhPartialCancel\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Fill Latency (1d)</span><span id=\"cfhFillLatency\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Auto Soft-Close</span><span id=\"cfhAutoClose\" class=\"v\"></span></div>
        <div id=\"cfhHints\" class=\"tiny\"></div>

        <h3 style=\"margin-top:14px\">Balance Reconciliation</h3>
        <div class=\"row\"><span class=\"k\">Status</span><span id=\"reconStatus\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Account Value</span><span id=\"reconCurrent\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Baseline</span><span id=\"reconBaseline\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Growth</span><span id=\"reconGrowth\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Bot P&amp;L</span><span id=\"reconBotPnl\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Drift</span><span id=\"reconDrift\" class=\"v\"></span></div>
        <div id=\"reconDetails\" class=\"tiny\"></div>

        <h3 style=\"margin-top:14px\">DOGE Bias Scoreboard</h3>
        <div class=\"row\"><span class=\"k\">DOGE Equity</span><span id=\"biasDogeEq\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">1h Change</span><span id=\"biasChange1h\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">24h Change</span><span id=\"biasChange24h\" class=\"v\"></span></div>
        <div id=\"biasSparkline\" style=\"height:24px;margin:4px 0\"></div>
        <div class=\"row\"><span class=\"k\">Idle USD</span><span id=\"biasIdleUsd\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Runway Floor</span><span id=\"biasRunway\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Opp. Cost (B-side)</span><span id=\"biasOppCost\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Open Gap</span><span id=\"biasOpenGap\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Re-entry Lag (med)</span><span id=\"biasLagMed\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Current Wait</span><span id=\"biasLagCurrent\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Trend Score</span><span id=\"trendScore\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Idle Target</span><span id=\"trendIdleTarget\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Governor</span><span id=\"rebalGov\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Size Skew</span><span id=\"rebalSizes\" class=\"v\"></span></div>
        <div id=\"biasDetails\" class=\"tiny\"></div>
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

  <div id=\"cmdSuggestions\" class=\"mono\"></div>
  <div id=\"cmdBar\">
    <span id=\"cmdPrefix\" class=\"mono\">:</span>
    <input id=\"cmdInput\" class=\"mono\" type=\"text\" spellcheck=\"false\" autocomplete=\"off\" />
  </div>

  <div id=\"helpModal\" class=\"overlay\" hidden>
    <div class=\"modal mono\">
<pre>+- Keybindings -------------------------+
|                                       |
|  NAVIGATION        ACTIONS            |
|  1-9   slot #jump  p    pause/resume  |
|  [/]   prev/next   +    add slot      |
|  gg    first slot  -    remove slot   |
|  G     last slot   .    refresh       |
|                    f    factory view  |
|                    s    api/status   |
|                    :    command       |
|                    ?    this help     |
|                    Esc  close         |
|                                       |
|  COMMAND BAR                          |
|  :pause  :resume  :add  :remove N     |
|  :audit  :drift  :stale [d] [n]      |
|  :set entry N  :set profit N          |
|  :layer add [auto|doge|usd]           |
|  :layer remove                        |
|  :jump N (slot #)  :q (factory view)  |
|  Tab=complete  up/down=history  Esc=close |
|                                       |
+-------------------- Esc to close -----+</pre>
    </div>
  </div>

  <div id=\"confirmDialog\" class=\"overlay\" hidden>
    <div class=\"modal\">
      <div id=\"confirmText\"></div>
      <div id=\"confirmActions\">
        <button id=\"confirmCancelBtn\">Cancel</button>
        <button id=\"confirmOkBtn\">Confirm</button>
      </div>
    </div>
  </div>

  <div id=\"toasts\"></div>

  <script>
    let state = null;
    let selectedSlot = 0;
    let pendingRenderState = null;
    let kbMode = 'NORMAL';
    let chordKey = '';
    let chordTimer = null;
    let pendingConfirm = null;
    let lastForcedRefreshMs = 0;
    let currentSuggestions = [];
    let suggestionIndex = -1;
    let historyIndex = 0;
    let lastRefreshError = '';
    const commandHistory = [];
    const COMMAND_COMPLETIONS = [
      'pause', 'resume', 'add', 'remove', 'close', 'audit', 'drift', 'stale',
      'set entry', 'set profit', 'jump', 'layer add', 'layer remove', 'q',
    ];
    const CONTROL_INPUT_IDS = new Set(['entryInput', 'profitInput', 'layerSourceSelect']);

    function fmt(n, d=6) {
      if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
      return Number(n).toFixed(d);
    }

    function showToast(message, type='info') {
      const box = document.getElementById('toasts');
      if (!box) return;
      while (box.children.length >= 3) {
        box.removeChild(box.firstElementChild);
      }
      const item = document.createElement('div');
      item.className = `toast ${type}`;
      item.textContent = String(message || 'ok');
      box.appendChild(item);
      const timeoutMs = type === 'error' ? 8000 : 4000;
      window.setTimeout(() => {
        if (item.parentElement === box) box.removeChild(item);
      }, timeoutMs);
    }

    function updateKbModeBadge() {
      const badge = document.getElementById('kbMode');
      if (!badge) return;
      badge.textContent = `KB ${kbMode}`;
      badge.className = 'badge';
    }

    function clearChordBuffer() {
      chordKey = '';
      if (chordTimer !== null) {
        window.clearTimeout(chordTimer);
        chordTimer = null;
      }
    }

    function setKbMode(nextMode) {
      if (kbMode === nextMode) return;
      kbMode = nextMode;
      clearChordBuffer();
      updateKbModeBadge();
      if (kbMode === 'NORMAL') {
        flushPendingRender();
      }
    }

    function closeCommandBarUi() {
      const bar = document.getElementById('cmdBar');
      const suggestions = document.getElementById('cmdSuggestions');
      bar.classList.remove('open');
      suggestions.classList.remove('open');
      suggestions.innerHTML = '';
      currentSuggestions = [];
      suggestionIndex = -1;
    }

    function closeHelpUi() {
      document.getElementById('helpModal').hidden = true;
    }

    function closeConfirmUi() {
      pendingConfirm = null;
      document.getElementById('confirmDialog').hidden = true;
    }

    function leaveToNormal() {
      closeCommandBarUi();
      closeHelpUi();
      closeConfirmUi();
      setKbMode('NORMAL');
    }

    function getSlots() {
      if (!state || !Array.isArray(state.slots)) return [];
      return state.slots;
    }

    function applySelectedSlotRender() {
      if (!state) return;
      renderSlots(state);
      renderSelected(state);
    }

    function jumpToSlotId(slotId) {
      const slots = getSlots();
      if (!slots.length) return false;
      const slot = slots.find((x) => x.slot_id === slotId);
      if (!slot) return false;
      selectedSlot = slot.slot_id;
      applySelectedSlotRender();
      return true;
    }

    function jumpFirstSlot() {
      const slots = getSlots();
      if (!slots.length) return;
      selectedSlot = slots[0].slot_id;
      applySelectedSlotRender();
    }

    function jumpLastSlot() {
      const slots = getSlots();
      if (!slots.length) return;
      selectedSlot = slots[slots.length - 1].slot_id;
      applySelectedSlotRender();
    }

    function cycleSlot(step) {
      const slots = getSlots();
      if (!slots.length) return;
      let idx = slots.findIndex((slot) => slot.slot_id === selectedSlot);
      if (idx < 0) idx = 0;
      idx = (idx + step + slots.length) % slots.length;
      selectedSlot = slots[idx].slot_id;
      applySelectedSlotRender();
    }

    function isControlInputFocused() {
      const active = document.activeElement;
      return !!(active && CONTROL_INPUT_IDS.has(active.id));
    }

    function normalizeCommandInput(raw) {
      const txt = String(raw || '').trim();
      if (!txt) return '';
      return txt.startsWith(':') ? txt.slice(1).trim() : txt;
    }

    function parseNonNegativeInt(raw) {
      if (!/^[0-9]+$/.test(String(raw || ''))) return null;
      return Number.parseInt(raw, 10);
    }

    function parseCommand(rawInput) {
      const norm = normalizeCommandInput(rawInput);
      if (!norm) return {type: 'noop'};
      const tokens = norm.split(/\\s+/);
      const verb = (tokens[0] || '').toLowerCase();

      if (verb === 'pause') return {type: 'action', action: 'pause', payload: {}};
      if (verb === 'resume') return {type: 'action', action: 'resume', payload: {}};
      if (verb === 'add') return {type: 'action', action: 'add_slot', payload: {}};
      if (verb === 'audit') return {type: 'action', action: 'audit_pnl', payload: {}};
      if (verb === 'drift') return {type: 'action', action: 'reconcile_drift', payload: {}};
      if (verb === 'stale') {
        let minDistancePct = 3.0;
        let maxBatch = 8;
        if (tokens.length >= 2) {
          const dist = Number.parseFloat(tokens[1]);
          if (!Number.isFinite(dist) || dist <= 0) return {error: 'usage: :stale [min_distance_pct] [max_batch]'};
          minDistancePct = dist;
        }
        if (tokens.length >= 3) {
          const batch = Number.parseInt(tokens[2], 10);
          if (!Number.isFinite(batch) || batch < 1 || batch > 20) return {error: 'stale max_batch must be 1..20'};
          maxBatch = batch;
        }
        return {
          type: 'action',
          action: 'cancel_stale_recoveries',
          payload: {min_distance_pct: minDistancePct, max_batch: maxBatch},
        };
      }
      if (verb === 'q') return {type: 'navigate', href: '/factory'};

      if (verb === 'jump') {
        if (tokens.length < 2) return {error: 'usage: :jump <N>'};
        const slotId = parseNonNegativeInt(tokens[1]);
        if (slotId === null) return {error: 'jump target must be a non-negative integer'};
        return {type: 'jump', slotId};
      }

      if (verb === 'layer') {
        if (tokens.length < 2) return {error: 'usage: :layer add [auto|doge|usd] | :layer remove'};
        const op = (tokens[1] || '').toLowerCase();
        if (op === 'remove') return {type: 'layer_remove'};
        if (op !== 'add') return {error: 'usage: :layer add [auto|doge|usd] | :layer remove'};
        const source = (tokens[2] || 'auto').toUpperCase();
        if (!['AUTO', 'DOGE', 'USD'].includes(source)) {
          return {error: 'layer source must be auto, doge, or usd'};
        }
        return {type: 'layer_add', source};
      }

      if (verb === 'set') {
        if (tokens.length < 3) return {error: 'usage: :set entry|profit <value>'};
        const target = (tokens[1] || '').toLowerCase();
        const value = Number.parseFloat(tokens[2]);
        if (!Number.isFinite(value) || value < 0.05 || value > 50.0) {
          return {error: 'set value must be between 0.05 and 50.0'};
        }
        if (target === 'entry') return {type: 'set', metric: 'entry', value};
        if (target === 'profit') return {type: 'set', metric: 'profit', value};
        return {error: `unknown set target: ${target}`};
      }

      if (verb === 'remove') {
        if (tokens.length === 1) return {type: 'remove_slot', count: 1};
        const n = parseNonNegativeInt(tokens[1]);
        if (n === null || n < 1) return {error: 'usage: :remove [N]  (N = number of slots to remove)'};
        return {type: 'remove_slots', count: n};
      }

      if (verb === 'close') {
        if (tokens.length === 1) return {type: 'action', action: 'soft_close_next', payload: {}};
        if (tokens.length < 3) return {error: 'usage: :close <slot> <rid>'};
        const slotId = parseNonNegativeInt(tokens[1]);
        const recoveryId = parseNonNegativeInt(tokens[2]);
        if (slotId === null || recoveryId === null) return {error: 'slot and recovery id must be non-negative integers'};
        return {
          type: 'action',
          action: 'soft_close',
          payload: {slot_id: slotId, recovery_id: recoveryId},
        };
      }

      return {error: `unknown command: ${verb}`};
    }

    function shouldConfirmPctChange(oldValue, newValue) {
      if (!Number.isFinite(oldValue) || oldValue === 0) return true;
      return Math.abs(newValue - oldValue) / Math.abs(oldValue) > 0.5;
    }

    async function api(path, opts={}) {
      const res = await fetch(path, opts);
      const text = await res.text();
      let data = null;
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (_err) {
          data = null;
        }
      }
      if (!res.ok) {
        const msg = data && data.message ? data.message : (text || `request failed (${res.status})`);
        throw new Error(msg);
      }
      if (data !== null) return data;
      throw new Error('invalid server response');
    }

    async function dispatchAction(action, payload={}) {
      try {
        const body = JSON.stringify({action, ...payload});
        const out = await api('/api/action', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body,
        });
        showToast(out.message || 'ok', 'success');
        await refresh();
        return true;
      } catch (err) {
        showToast(err.message || 'request failed', 'error');
        return false;
      }
    }

    function openConfirmDialog(text, onConfirm) {
      closeCommandBarUi();
      closeHelpUi();
      pendingConfirm = {onConfirm};
      document.getElementById('confirmText').textContent = text;
      document.getElementById('confirmDialog').hidden = false;
      setKbMode('CONFIRM');
      document.getElementById('confirmOkBtn').focus();
    }

    async function confirmAccept() {
      if (!pendingConfirm) return;
      const handler = pendingConfirm.onConfirm;
      closeConfirmUi();
      setKbMode('NORMAL');
      if (typeof handler === 'function') {
        await handler();
      }
    }

    function confirmCancel() {
      if (kbMode !== 'CONFIRM') return;
      closeConfirmUi();
      setKbMode('NORMAL');
    }

    function requestPause() {
      openConfirmDialog('Pause bot? Active orders remain open.', async () => {
        await dispatchAction('pause');
      });
    }

    function requestSoftCloseNext() {
      openConfirmDialog('Close oldest recovery?', async () => {
        await dispatchAction('soft_close_next');
      });
    }

    function requestReconcileDrift() {
      openConfirmDialog('Reconcile drift now? This cancels Kraken-only unknown orders for the active pair.', async () => {
        await dispatchAction('reconcile_drift');
      });
    }

    function requestCancelStaleRecoveries(minDistancePct = 3.0, maxBatch = 8) {
      openConfirmDialog(
        `Refresh stale recoveries now? min_distance=${minDistancePct}%, max_batch=${maxBatch}.`,
        async () => {
          await dispatchAction('cancel_stale_recoveries', {
            min_distance_pct: minDistancePct,
            max_batch: maxBatch,
          });
        },
      );
    }

    function requestSoftClose(slotId, recoveryId) {
      openConfirmDialog(`Close recovery #${recoveryId} on slot #${slotId}?`, async () => {
        await dispatchAction('soft_close', {slot_id: slotId, recovery_id: recoveryId});
      });
    }

    function requestRemoveSlot(slotId) {
      const slots = getSlots();
      if (!slots.length) { showToast('no slots to remove', 'error'); return; }
      const target = slotId != null ? slotId : slots[slots.length - 1].slot_id;
      openConfirmDialog(`Remove slot #${target}? This cancels ALL its orders on Kraken.`, async () => {
        await dispatchAction('remove_slot', {slot_id: target});
      });
    }

    function requestRemoveSlots(count) {
      openConfirmDialog(`Remove ${count} highest slot(s)? This cancels ALL their orders.`, async () => {
        await dispatchAction('remove_slots', {count});
      });
    }

    function requestAddLayer(source) {
      const src = String(source || 'AUTO').toUpperCase();
      const layer = state && state.capital_layers ? state.capital_layers : {};
      const usdNowRaw = layer.add_layer_usd_equiv_now;
      const usdNow = usdNowRaw === null || usdNowRaw === undefined ? Number.NaN : Number(usdNowRaw);
      const usdText = Number.isFinite(usdNow) ? `$${fmt(usdNow, 4)}` : 'price unavailable';
      const text = [
        `Commit one layer = +1 DOGE/order across up to 225 orders.`,
        `This commit step is 225 DOGE-equivalent at current price (${usdText}).`,
        `Funding source: ${src}.`,
      ].join(' ');
      openConfirmDialog(text, async () => {
        await dispatchAction('add_layer', {source: src});
      });
    }

    function requestRemoveLayer() {
      openConfirmDialog('Remove one layer (-1 DOGE/order) for newly placed orders?', async () => {
        await dispatchAction('remove_layer');
      });
    }

    function requestSetMetric(metric, value) {
      const oldValue = Number(metric === 'entry' ? state && state.entry_pct : state && state.profit_pct);
      const action = metric === 'entry' ? 'set_entry_pct' : 'set_profit_pct';
      if (shouldConfirmPctChange(oldValue, value)) {
        const oldText = Number.isFinite(oldValue) ? fmt(oldValue, 3) : '0.000';
        const newText = fmt(value, 3);
        openConfirmDialog(`Change ${metric} from ${oldText}% to ${newText}%?`, async () => {
          await dispatchAction(action, {value});
        });
        return;
      }
      void dispatchAction(action, {value});
    }

    function pushCommandHistory(rawInput) {
      const norm = normalizeCommandInput(rawInput);
      if (!norm) return;
      commandHistory.push(`:${norm}`);
      if (commandHistory.length > 20) commandHistory.shift();
      historyIndex = commandHistory.length;
    }

    function commandMatches(rawInput) {
      const pref = normalizeCommandInput(rawInput).toLowerCase();
      if (!pref) return COMMAND_COMPLETIONS.slice(0, 5);
      return COMMAND_COMPLETIONS.filter((cmd) => cmd.startsWith(pref)).slice(0, 5);
    }

    function renderCommandSuggestions() {
      const el = document.getElementById('cmdSuggestions');
      const raw = document.getElementById('cmdInput').value;
      currentSuggestions = commandMatches(raw);
      if (!currentSuggestions.length) {
        el.classList.remove('open');
        el.innerHTML = '';
        suggestionIndex = -1;
        return;
      }
      if (suggestionIndex >= currentSuggestions.length) suggestionIndex = -1;
      el.innerHTML = '';
      for (let i = 0; i < currentSuggestions.length; i += 1) {
        const row = document.createElement('div');
        row.className = 'cmd-suggestion mono' + (i === suggestionIndex ? ' active' : '');
        row.textContent = `:${currentSuggestions[i]}`;
        el.appendChild(row);
      }
      el.classList.add('open');
    }

    function applySuggestion(index) {
      if (!currentSuggestions.length) return;
      const input = document.getElementById('cmdInput');
      input.value = currentSuggestions[index];
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }

    function recallHistory(step) {
      if (!commandHistory.length) return;
      historyIndex = Math.max(0, Math.min(commandHistory.length, historyIndex + step));
      const input = document.getElementById('cmdInput');
      if (historyIndex === commandHistory.length) {
        input.value = '';
      } else {
        input.value = commandHistory[historyIndex].slice(1);
      }
      suggestionIndex = -1;
      renderCommandSuggestions();
    }

    function openCommandBar() {
      closeHelpUi();
      closeConfirmUi();
      const bar = document.getElementById('cmdBar');
      const input = document.getElementById('cmdInput');
      bar.classList.add('open');
      input.value = '';
      historyIndex = commandHistory.length;
      suggestionIndex = -1;
      renderCommandSuggestions();
      setKbMode('COMMAND');
      input.focus();
    }

    function closeCommandBarToNormal() {
      closeCommandBarUi();
      setKbMode('NORMAL');
    }

    function openHelp() {
      closeCommandBarUi();
      closeConfirmUi();
      document.getElementById('helpModal').hidden = false;
      setKbMode('HELP');
    }

    function toggleHelp() {
      if (kbMode === 'HELP') {
        closeHelpUi();
        setKbMode('NORMAL');
        return;
      }
      openHelp();
    }

    async function executeCommand(rawInput) {
      const parsed = parseCommand(rawInput);
      if (parsed.error) {
        showToast(parsed.error, 'error');
        return;
      }
      if (parsed.type === 'noop') {
        showToast('noop', 'info');
        return;
      }
      if (parsed.type === 'navigate') {
        window.location.href = parsed.href;
        return;
      }

      pushCommandHistory(rawInput);

      if (parsed.type === 'jump') {
        if (!jumpToSlotId(parsed.slotId)) {
          showToast(`slot #${parsed.slotId} not found`, 'error');
        } else {
          showToast(`jumped to slot #${parsed.slotId}`, 'info');
        }
        return;
      }

      if (parsed.type === 'set') {
        requestSetMetric(parsed.metric, parsed.value);
        return;
      }

      if (parsed.type === 'layer_add') {
        requestAddLayer(parsed.source);
        return;
      }
      if (parsed.type === 'layer_remove') {
        requestRemoveLayer();
        return;
      }

      if (parsed.action === 'pause') {
        requestPause();
        return;
      }
      if (parsed.action === 'soft_close_next') {
        requestSoftCloseNext();
        return;
      }
      if (parsed.action === 'soft_close') {
        requestSoftClose(parsed.payload.slot_id, parsed.payload.recovery_id);
        return;
      }
      if (parsed.type === 'remove_slot') {
        requestRemoveSlot();
        return;
      }
      if (parsed.type === 'remove_slots') {
        requestRemoveSlots(parsed.count);
        return;
      }
      await dispatchAction(parsed.action, parsed.payload);
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
      const pnlAudit = s.pnl_audit || null;
      const pnlAuditEl = document.getElementById('pnlAudit');
      const pnlAuditDetailsEl = document.getElementById('pnlAuditDetails');
      if (pnlAudit && typeof pnlAudit.ok === 'boolean') {
        pnlAuditEl.textContent = pnlAudit.ok ? 'OK' : 'MISMATCH';
        pnlAuditEl.style.color = pnlAudit.ok ? 'var(--good)' : 'var(--bad)';
        const mismatchCount = Number(pnlAudit.slot_mismatch_count || 0);
        pnlAuditDetailsEl.textContent =
          `drift pnl=${fmt(pnlAudit.profit_drift, 8)} loss=${fmt(pnlAudit.loss_drift, 8)} trips=${pnlAudit.trips_drift || 0}`
          + (mismatchCount > 0 ? ` mismatched_slots=${mismatchCount}` : '');
        pnlAuditDetailsEl.title = String(pnlAudit.message || '');
      } else {
        pnlAuditEl.textContent = '-';
        pnlAuditEl.style.color = '';
        pnlAuditDetailsEl.textContent = '';
        pnlAuditDetailsEl.title = '';
      }
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

      const autoCloseTotal = cfh.auto_soft_close_total || 0;
      const autoCloseLastAt = cfh.auto_soft_close_last_at;
      const autoCloseEl = document.getElementById('cfhAutoClose');
      if (autoCloseTotal === 0) {
        autoCloseEl.textContent = 'idle';
        autoCloseEl.style.color = '';
      } else {
        const ago = autoCloseLastAt ? Math.round((Date.now() / 1000) - autoCloseLastAt) : null;
        const agoText = ago !== null && ago < 300 ? ` (${ago}s ago)` : '';
        autoCloseEl.textContent = `${autoCloseTotal} repriced${agoText}`;
        autoCloseEl.style.color = ago !== null && ago < 120 ? 'var(--warn)' : '';
      }

      const hints = Array.isArray(cfh.blocked_risk_hint) ? cfh.blocked_risk_hint : [];
      document.getElementById('cfhHints').textContent = hints.length ? `Hints: ${hints.join(', ')}` : '';

      const layers = s.capital_layers || {};
      const targetLayers = Number(layers.target_layers || 0);
      const effectiveLayers = Number(layers.effective_layers || 0);
      const dogePerLayer = Number(layers.doge_per_order_per_layer || 0);
      const layerStep = Number(layers.layer_step_doge_eq || 0);
      const usdNowRaw = layers.add_layer_usd_equiv_now;
      const usdNow = usdNowRaw === null || usdNowRaw === undefined ? Number.NaN : Number(usdNowRaw);
      const ordersFunded = Number(layers.orders_at_funded_size || 0);
      const ordersTotal = Number(layers.open_orders_total || 0);
      const gapLayers = Number(layers.gap_layers || 0);
      const gapDoge = Number(layers.gap_doge_now || 0);
      const gapUsd = Number(layers.gap_usd_now || 0);
      const sourceDefault = String(layers.funding_source_default || 'AUTO').toUpperCase();

      document.getElementById('layerTarget').textContent = `+${fmt(targetLayers * dogePerLayer, 3)} DOGE/order`;
      document.getElementById('layerFunded').textContent = `+${fmt(effectiveLayers * dogePerLayer, 3)} DOGE/order`;
      document.getElementById('layerStep').textContent = `${fmt(layerStep, 3)} DOGE-eq`;
      document.getElementById('layerUsdNow').textContent = Number.isFinite(usdNow) ? `$${fmt(usdNow, 4)}` : '-';
      document.getElementById('layerPropagation').textContent = `${ordersFunded}/${ordersTotal}`;
      document.getElementById('layerGap').textContent = `short ${fmt(gapDoge, 3)} DOGE and $${fmt(gapUsd, 4)}`;
      document.getElementById('layerHint').textContent =
        gapLayers > 0
          ? 'Orders resize gradually as they recycle. No mass cancel/replace.'
          : 'Orders resize gradually as they recycle.';

      const layerSourceSelect = document.getElementById('layerSourceSelect');
      if (layerSourceSelect && document.activeElement !== layerSourceSelect) {
        layerSourceSelect.value = sourceDefault;
      }

      // Balance Reconciliation card
      const recon = s.balance_recon;
      const reconStatusEl = document.getElementById('reconStatus');
      const reconCurrentEl = document.getElementById('reconCurrent');
      const reconBaselineEl = document.getElementById('reconBaseline');
      const reconGrowthEl = document.getElementById('reconGrowth');
      const reconBotPnlEl = document.getElementById('reconBotPnl');
      const reconDriftEl = document.getElementById('reconDrift');
      const reconDetailsEl = document.getElementById('reconDetails');
      if (!recon) {
        reconStatusEl.textContent = 'Baseline pending...';
        reconStatusEl.style.color = '';
        reconCurrentEl.textContent = '-';
        reconBaselineEl.textContent = '-';
        reconGrowthEl.textContent = '-';
        reconBotPnlEl.textContent = '-';
        reconDriftEl.textContent = '-';
        reconDetailsEl.textContent = '';
      } else if (recon.status === 'NO_PRICE' || recon.status === 'NO_BALANCE') {
        reconStatusEl.textContent = recon.status;
        reconStatusEl.style.color = '';
        reconCurrentEl.textContent = '-';
        reconBaselineEl.textContent = '-';
        reconGrowthEl.textContent = '-';
        reconBotPnlEl.textContent = '-';
        reconDriftEl.textContent = '-';
        reconDetailsEl.textContent = '';
      } else {
        const sim = recon.simulated ? ' (sim)' : '';
        reconStatusEl.textContent = recon.status + sim;
        reconStatusEl.style.color = recon.status === 'OK' ? 'var(--good)' : 'var(--bad)';
        reconCurrentEl.textContent = `${fmt(recon.current_doge_eq, 1)} DOGE`;
        reconBaselineEl.textContent = `${fmt(recon.baseline_doge_eq, 1)} DOGE`;
        reconGrowthEl.textContent = `${fmt(recon.account_growth_doge, 2)} DOGE`;
        reconBotPnlEl.textContent = `${fmt(recon.bot_pnl_doge, 2)} DOGE`;
        const driftSign = recon.drift_doge >= 0 ? '+' : '';
        reconDriftEl.textContent = `${driftSign}${fmt(recon.drift_doge, 2)} DOGE (${driftSign}${fmt(recon.drift_pct, 2)}%)`;
        reconDriftEl.style.color = recon.status === 'OK' ? '' : 'var(--bad)';
        const ageHrs = recon.baseline_ts ? ((Date.now() / 1000 - recon.baseline_ts) / 3600).toFixed(1) : '?';
        reconDetailsEl.textContent = `baseline age: ${ageHrs}h | threshold: \\u00b1${fmt(recon.threshold_pct, 1)}% | price: $${fmt(recon.price, 5)}`;
      }

      // DOGE Bias Scoreboard card
      const bias = s.doge_bias_scoreboard;
      const bEq = document.getElementById('biasDogeEq');
      const b1h = document.getElementById('biasChange1h');
      const b24h = document.getElementById('biasChange24h');
      const bSpark = document.getElementById('biasSparkline');
      const bIdle = document.getElementById('biasIdleUsd');
      const bRunway = document.getElementById('biasRunway');
      const bOpp = document.getElementById('biasOppCost');
      const bGap = document.getElementById('biasOpenGap');
      const bLagM = document.getElementById('biasLagMed');
      const bLagC = document.getElementById('biasLagCurrent');
      const bDet = document.getElementById('biasDetails');
      const trendScoreEl = document.getElementById('trendScore');
      const trendIdleEl = document.getElementById('trendIdleTarget');
      const govEl = document.getElementById('rebalGov');
      const sizesEl = document.getElementById('rebalSizes');
      const trend = s.trend || null;
      if (!bias) {
        [bEq, b1h, b24h, bIdle, bRunway, bOpp, bGap, bLagM, bLagC].forEach(e => { e.textContent = '-'; e.style.color = ''; });
        bSpark.innerHTML = '';
        bDet.textContent = '';
        govEl.textContent = '-';
        govEl.style.color = '';
        sizesEl.textContent = '-';
        sizesEl.style.color = '';
      } else {
        bEq.textContent = `${fmt(bias.doge_eq, 1)} DOGE`;
        const fmtDelta = (v, el) => {
          if (v == null) { el.textContent = '-'; el.style.color = ''; return; }
          const sign = v >= 0 ? '+' : '';
          el.textContent = `${sign}${fmt(v, 1)} DOGE`;
          el.style.color = v > 0 ? 'var(--good)' : v < 0 ? 'var(--bad)' : '';
        };
        fmtDelta(bias.doge_eq_change_1h, b1h);
        fmtDelta(bias.doge_eq_change_24h, b24h);
        // Sparkline SVG
        const pts = bias.doge_eq_sparkline || [];
        if (pts.length >= 2) {
          const mn = Math.min(...pts), mx = Math.max(...pts);
          const range = mx - mn || 1;
          const w = 280, h = 24;
          const coords = pts.map((v, i) => `${(i / (pts.length - 1) * w).toFixed(1)},${(h - (v - mn) / range * h).toFixed(1)}`).join(' ');
          bSpark.innerHTML = `<svg width=\"${w}\" height=\"${h}\" viewBox=\"0 0 ${w} ${h}\"><polyline points=\"${coords}\" fill=\"none\" stroke=\"var(--accent)\" stroke-width=\"1.5\"/></svg>`;
        } else {
          bSpark.innerHTML = '';
        }
        // Idle USD
        bIdle.textContent = `$${fmt(bias.idle_usd, 2)} (${fmt(bias.idle_usd_pct, 1)}%)`;
        bIdle.style.color = bias.idle_usd_pct > 50 ? 'var(--warn)' : '';
        bRunway.textContent = `$${fmt(bias.usd_runway_floor, 2)}`;
        // Opportunity PnL
        if (bias.gap_count === 0 && bias.open_gap_opportunity_usd == null) {
          bOpp.textContent = '-';
          bOpp.style.color = '';
        } else {
          bOpp.textContent = `$${fmt(bias.total_opportunity_pnl_usd, 2)} (${bias.gap_count} gaps)`;
          bOpp.style.color = bias.total_opportunity_pnl_usd > 0 ? 'var(--warn)' : bias.total_opportunity_pnl_usd < 0 ? 'var(--good)' : '';
        }
        bGap.textContent = bias.open_gap_opportunity_usd != null ? `$${fmt(bias.open_gap_opportunity_usd, 2)}` : '-';
        bGap.style.color = bias.open_gap_opportunity_usd != null && bias.open_gap_opportunity_usd > 0 ? 'var(--warn)' : '';
        // Re-entry Lag
        const fmtSec = (v) => v == null ? '-' : v >= 3600 ? `${(v / 3600).toFixed(1)}h` : v >= 60 ? `${(v / 60).toFixed(1)}m` : `${Math.round(v)}s`;
        bLagM.textContent = fmtSec(bias.median_reentry_lag_sec);
        bLagC.textContent = bias.current_open_lag_sec != null
          ? `${fmtSec(bias.current_open_lag_sec)} (${fmt(bias.current_open_lag_price_pct || 0, 2)}%)`
          : '-';
        bLagC.style.color = bias.current_open_lag_sec != null && bias.current_open_lag_sec > 300 ? 'var(--warn)' : '';
        // Details line
        const parts = [];
        if (bias.worst_missed_usd != null) parts.push(`worst miss: $${fmt(bias.worst_missed_usd, 2)}`);
        if (bias.max_reentry_lag_sec != null) parts.push(`max lag: ${fmtSec(bias.max_reentry_lag_sec)}`);
        if (bias.median_price_distance_pct != null) parts.push(`med dist: ${fmt(bias.median_price_distance_pct, 2)}%`);
        bDet.textContent = parts.join(' | ');

        const rebal = s.rebalancer || null;
        if (!rebal || !rebal.enabled) {
          govEl.textContent = 'Off';
          govEl.style.color = '';
          sizesEl.textContent = 'A ×1.00 | B ×1.00';
          sizesEl.style.color = '';
        } else {
          const dir = String(rebal.skew_direction || 'neutral');
          const damped = Boolean(rebal.damped);
          const skewPct = Number(rebal.skew || 0) * 100;
          if (dir === 'buy_doge') {
            govEl.textContent = `▶ Buy DOGE (${skewPct >= 0 ? '+' : ''}${fmt(skewPct, 1)}%)`;
            govEl.style.color = damped ? 'var(--warn)' : 'var(--good)';
          } else if (dir === 'sell_doge') {
            govEl.textContent = `▶ Sell DOGE (${skewPct >= 0 ? '+' : ''}${fmt(skewPct, 1)}%)`;
            govEl.style.color = damped ? 'var(--warn)' : 'var(--bad)';
          } else {
            govEl.textContent = damped ? 'Neutral (damped)' : 'Neutral';
            govEl.style.color = damped ? 'var(--warn)' : '';
          }
          const aMult = Number(rebal.size_mult_a || 1);
          const bMult = Number(rebal.size_mult_b || 1);
          sizesEl.textContent = `A ×${fmt(aMult, 2)} | B ×${fmt(bMult, 2)}`;
          sizesEl.style.color = damped ? 'var(--warn)' : '';
        }
      }
      if (!trend) {
        trendScoreEl.textContent = '-';
        trendScoreEl.style.color = '';
        trendIdleEl.textContent = '-';
        trendIdleEl.style.color = '';
      } else {
        const score = Number(trend.score || 0);
        const scorePct = score * 100;
        const scoreSign = scorePct >= 0 ? '+' : '';
        trendScoreEl.textContent = `${scoreSign}${fmt(scorePct, 2)}%`;
        if (score > 0.005) trendScoreEl.style.color = 'var(--good)';
        else if (score < -0.005) trendScoreEl.style.color = 'var(--bad)';
        else trendScoreEl.style.color = '';

        const rebal = s.rebalancer || {};
        const dynamicTarget = Number(rebal.target != null ? rebal.target : trend.dynamic_idle_target || 0);
        const baseTarget = Number(rebal.base_target != null ? rebal.base_target : 0);
        trendIdleEl.textContent = `${fmt(dynamicTarget * 100, 1)}% (base ${fmt(baseTarget * 100, 1)}%)`;
        trendIdleEl.style.color = dynamicTarget < baseTarget ? 'var(--good)' : dynamicTarget > baseTarget ? 'var(--warn)' : '';
      }
    }

    function renderSlots(s) {
      const el = document.getElementById('slots');
      el.innerHTML = '';
      for (const slot of s.slots) {
        const b = document.createElement('button');
        b.className = 'slot' + (slot.slot_id === selectedSlot ? ' active' : '');
        const alias = slot.slot_alias || slot.slot_label || `slot-${slot.slot_id}`;
        b.textContent = `${alias} ${slot.phase}`;
        b.title = `slot #${slot.slot_id}`;
        b.onclick = () => {
          selectedSlot = slot.slot_id;
          renderSelected(s);
          renderSlots(s);
        };
        el.appendChild(b);
      }
      if (!s.slots.find((x) => x.slot_id === selectedSlot) && s.slots.length) {
        selectedSlot = s.slots[0].slot_id;
      }
    }

    function renderSelected(s) {
      const slot = s.slots.find((x) => x.slot_id === selectedSlot) || s.slots[0];
      if (!slot) return;

      const sb = document.getElementById('stateBar');
      const alias = slot.slot_alias || slot.slot_label || `slot-${slot.slot_id}`;
      sb.innerHTML = `
        <span class=\"statepill ${slot.phase}\">${slot.phase}</span>
        <span class=\"tiny\">${alias} (#${slot.slot_id})</span>
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
        tr.querySelector('button').onclick = () => requestSoftClose(slot.slot_id, r.recovery_id);
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

      const entryInput = document.getElementById('entryInput');
      const profitInput = document.getElementById('profitInput');
      if (document.activeElement !== entryInput) entryInput.value = fmt(s.entry_pct, 3);
      if (document.activeElement !== profitInput) profitInput.value = fmt(s.profit_pct, 3);
    }

    function renderAll(s) {
      renderTop(s);
      renderSlots(s);
      renderSelected(s);
    }

    function flushPendingRender() {
      if (pendingRenderState) {
        renderAll(pendingRenderState);
        pendingRenderState = null;
        return;
      }
      if (state) renderAll(state);
    }

    async function refresh() {
      try {
        const nextState = await api('/api/status');
        state = nextState;
        lastRefreshError = '';
        if (kbMode === 'NORMAL') {
          renderAll(nextState);
          pendingRenderState = null;
        } else {
          pendingRenderState = nextState;
        }
      } catch (err) {
        const msg = err.message || 'status refresh failed';
        if (msg !== lastRefreshError) {
          showToast(msg, 'error');
          lastRefreshError = msg;
        }
      }
    }

    async function togglePauseResume() {
      if (state && state.mode === 'RUNNING') {
        requestPause();
      } else {
        await dispatchAction('resume');
      }
    }

    function forceRefreshRateLimited() {
      const now = Date.now();
      if (now - lastForcedRefreshMs < 2000) return;
      lastForcedRefreshMs = now;
      void refresh();
    }

    function armChord(key) {
      clearChordBuffer();
      chordKey = key;
      chordTimer = window.setTimeout(() => {
        clearChordBuffer();
      }, 400);
    }

    function handleNormalModeKey(event) {
      const key = event.key;
      if (/^[1-9]$/.test(key)) {
        const slotId = Number(key);
        if (!jumpToSlotId(slotId)) {
          showToast(`slot #${slotId} not found`, 'error');
        }
        clearChordBuffer();
        return true;
      }
      if (key === '[') {
        cycleSlot(-1);
        clearChordBuffer();
        return true;
      }
      if (key === ']') {
        cycleSlot(1);
        clearChordBuffer();
        return true;
      }
      if (key === 'g') {
        if (chordKey === 'g') {
          clearChordBuffer();
          jumpFirstSlot();
        } else {
          armChord('g');
        }
        return true;
      }
      if (key === 'G') {
        clearChordBuffer();
        jumpLastSlot();
        return true;
      }
      if (key === 'p') {
        clearChordBuffer();
        void togglePauseResume();
        return true;
      }
      if (key === '+') {
        clearChordBuffer();
        void dispatchAction('add_slot');
        return true;
      }
      if (key === '-') {
        clearChordBuffer();
        requestRemoveSlot();
        return true;
      }
      if (key === '.') {
        clearChordBuffer();
        forceRefreshRateLimited();
        return true;
      }
      if (key === ':') {
        clearChordBuffer();
        openCommandBar();
        return true;
      }
      if (key === '?') {
        clearChordBuffer();
        toggleHelp();
        return true;
      }
      if (key === 'f') {
        clearChordBuffer();
        window.location.href = '/factory';
        return true;
      }
      if (key === 's') {
        clearChordBuffer();
        window.location.href = '/api/status';
        return true;
      }
      if (key === 'Escape') {
        clearChordBuffer();
        leaveToNormal();
        return true;
      }
      clearChordBuffer();
      return false;
    }

    function onGlobalKeyDown(event) {
      if (event.ctrlKey || event.metaKey || event.altKey) return;

      if (isControlInputFocused()) {
        if (event.key === 'Escape') {
          event.preventDefault();
          document.activeElement.blur();
          leaveToNormal();
        }
        return;
      }

      if (kbMode === 'COMMAND') {
        if (event.target !== document.getElementById('cmdInput')) {
          event.preventDefault();
        }
        return;
      }

      if (kbMode === 'HELP') {
        event.preventDefault();
        if (event.key === 'Escape' || event.key === '?') {
          closeHelpUi();
          setKbMode('NORMAL');
        }
        return;
      }

      if (kbMode === 'CONFIRM') {
        event.preventDefault();
        if (event.key === 'Enter') {
          void confirmAccept();
        } else if (event.key === 'Escape') {
          confirmCancel();
        }
        return;
      }

      if (handleNormalModeKey(event)) {
        event.preventDefault();
      }
    }

    function readAndValidatePctInput(inputId, label) {
      const value = Number(document.getElementById(inputId).value);
      if (!Number.isFinite(value) || value < 0.05 || value > 50.0) {
        showToast(`${label} must be between 0.05 and 50.0`, 'error');
        return null;
      }
      return value;
    }

    document.getElementById('pauseBtn').onclick = () => requestPause();
    document.getElementById('resumeBtn').onclick = () => { void dispatchAction('resume'); };
    document.getElementById('addSlotBtn').onclick = () => { void dispatchAction('add_slot'); };
    document.getElementById('removeSlotBtn').onclick = () => requestRemoveSlot();
    document.getElementById('softCloseBtn').onclick = () => requestSoftCloseNext();
    document.getElementById('reconcileBtn').onclick = () => requestReconcileDrift();
    document.getElementById('cancelStaleBtn').onclick = () => requestCancelStaleRecoveries();
    document.getElementById('addLayerBtn').onclick = () => {
      const source = document.getElementById('layerSourceSelect').value || 'AUTO';
      requestAddLayer(source);
    };
    document.getElementById('removeLayerBtn').onclick = () => requestRemoveLayer();
    document.getElementById('setEntryBtn').onclick = () => {
      const value = readAndValidatePctInput('entryInput', 'entry');
      if (value === null) return;
      requestSetMetric('entry', value);
    };
    document.getElementById('setProfitBtn').onclick = () => {
      const value = readAndValidatePctInput('profitInput', 'profit');
      if (value === null) return;
      requestSetMetric('profit', value);
    };

    document.getElementById('confirmOkBtn').onclick = () => { void confirmAccept(); };
    document.getElementById('confirmCancelBtn').onclick = () => confirmCancel();

    for (const id of CONTROL_INPUT_IDS) {
      const input = document.getElementById(id);
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.target.blur();
          leaveToNormal();
        }
      });
      input.addEventListener('blur', () => {
        setKbMode('NORMAL');
      });
    }

    const cmdInput = document.getElementById('cmdInput');
    cmdInput.addEventListener('input', () => {
      suggestionIndex = -1;
      renderCommandSuggestions();
    });
    cmdInput.addEventListener('keydown', (event) => {
      if (event.key === 'Tab') {
        event.preventDefault();
        if (!currentSuggestions.length) renderCommandSuggestions();
        if (!currentSuggestions.length) return;
        suggestionIndex = (suggestionIndex + 1) % currentSuggestions.length;
        applySuggestion(suggestionIndex);
        renderCommandSuggestions();
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        recallHistory(-1);
        return;
      }
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        recallHistory(1);
        return;
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        const raw = cmdInput.value;
        closeCommandBarToNormal();
        void executeCommand(raw);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        closeCommandBarToNormal();
      }
    });

    document.addEventListener('keydown', onGlobalKeyDown);

    updateKbModeBadge();
    void refresh();
    window.setInterval(refresh, 5000);
  </script>
</body>
</html>
"""
