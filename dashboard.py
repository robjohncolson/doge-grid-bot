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
    .status-chip {
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 1px 8px;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .04em;
      margin-right: 6px;
      color: var(--muted);
      background: rgba(255,255,255,.02);
      vertical-align: middle;
    }
    .status-chip.warn {
      color: var(--warn);
      border-color: rgba(210,153,34,.45);
      background: rgba(210,153,34,.1);
    }

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
    button:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    button:disabled:hover { border-color: var(--line); }
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
    .progress-track {
      width: 100%;
      height: 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.03);
      overflow: hidden;
      margin: 6px 0 4px;
    }
    .progress-fill {
      height: 100%;
      width: 0%;
      transition: width .25s ease;
      background: var(--line);
    }
    .progress-fill.shallow { background: #6e7681; }
    .progress-fill.baseline { background: var(--warn); }
    .progress-fill.deep { background: #3fb950; }
    .progress-fill.full { background: var(--good); }
    .ai-regime-card {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      margin-top: 8px;
      background: rgba(255,255,255,.02);
    }
    .ai-regime-card.agree {
      border-color: var(--line);
      background: rgba(255,255,255,.02);
    }
    .ai-regime-card.disagree {
      border-color: rgba(210,153,34,.55);
      background: rgba(210,153,34,.09);
    }
    .ai-regime-card.override {
      border-color: rgba(240,136,62,.65);
      background: rgba(240,136,62,.10);
    }
    .ai-regime-card.disabled {
      opacity: .72;
    }
    .ai-regime-actions {
      display: flex;
      gap: 8px;
      margin-top: 8px;
      flex-wrap: wrap;
    }
    .ai-regime-actions button {
      padding: 6px 10px;
      font-size: 12px;
    }
    .ai-rationale {
      margin-top: 6px;
    }
    .ai-watch {
      margin-top: 4px;
    }

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
    .vintage-bar {
      display: flex;
      height: 10px;
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid var(--line);
      background: #0f141b;
      margin-top: 4px;
    }
    .vintage-seg { height: 100%; }
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
          <button id=\"releaseEligibleBtn\" style=\"display:none\">Release Oldest Eligible</button>
          <button id=\"releaseBtn\" style=\"display:none\">Release Exit</button>
          <button id=\"accumStopBtn\" style=\"display:none;background:#8f3a2f\">Stop Accumulation</button>
          <button id=\"softCloseBtn\">Close Oldest Waiting</button>
          <button id=\"reconcileBtn\">Reconcile Drift</button>
          <button id=\"cancelStaleBtn\">Refresh Waiting</button>
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
        <div id=\"layerTelemetryRows\">
          <div class=\"row\"><span class=\"k\">Target Size</span><span id=\"layerTarget\" class=\"v\"></span></div>
          <div class=\"row\"><span class=\"k\">Funded Now</span><span id=\"layerFunded\" class=\"v\"></span></div>
          <div class=\"row\"><span class=\"k\">Step Size</span><span id=\"layerStep\" class=\"v\"></span></div>
          <div class=\"row\"><span class=\"k\">USD Equiv Now</span><span id=\"layerUsdNow\" class=\"v\"></span></div>
          <div class=\"row\"><span class=\"k\">Propagation</span><span id=\"layerPropagation\" class=\"v\"></span></div>
          <div class=\"row\"><span class=\"k\">Funding Gap</span><span id=\"layerGap\" class=\"v\"></span></div>
        </div>
        <div id=\"layerNoLayers\" class=\"tiny\" style=\"display:none\">No layers active</div>
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
        <div class=\"row\"><span id=\"orphansLabel\" class=\"k\">Waiting Exits</span><span id=\"orphans\" class=\"v\"></span></div>
        <div id=\"dustSweepRow\" class=\"row\" style=\"display:none\"><span class=\"k\">Dust Sweep</span><span id=\"dustSweep\" class=\"v\"></span></div>
        <div id=\"dustSweepDetails\" class=\"tiny\" style=\"display:none\"></div>

        <h3 style=\"margin-top:14px\">Sticky Vintage</h3>
        <div class=\"row\"><span class=\"k\">Sticky Mode</span><span id=\"stickyModeStatus\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Waiting Exits</span><span id=\"vintageWaiting\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Oldest Exit</span><span id=\"vintageOldest\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Stuck Capital</span><span id=\"vintageStuck\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Release Eligible</span><span id=\"vintageEligible\" class=\"v\"></span></div>
        <div id=\"vintageBar\" class=\"vintage-bar\"></div>
        <div id=\"vintageLegend\" class=\"tiny\"></div>
        <div class=\"row\"><span class=\"k\">Release Gate</span><span id=\"releaseGateStatus\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Releases</span><span id=\"releaseTotals\" class=\"v\"></span></div>
        <div id=\"releaseGateReason\" class=\"tiny\"></div>

        <h3 style=\"margin-top:14px\">HMM Regime</h3>
        <div class=\"row\"><span class=\"k\">Status</span><span id=\"hmmStatus\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Source</span><span id=\"hmmSource\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Regime</span><span id=\"hmmRegime\" class=\"v\"></span></div>
        <div class=\"row\" id=\"hmmRegime1mRow\" style=\"display:none\"><span class=\"k\">&nbsp;&nbsp;1m</span><span id=\"hmmRegime1m\" class=\"v\"></span></div>
        <div class=\"row\" id=\"hmmRegime15mRow\" style=\"display:none\"><span class=\"k\">&nbsp;&nbsp;15m</span><span id=\"hmmRegime15m\" class=\"v\"></span></div>
        <div class=\"row\" id=\"hmmRegime1hRow\" style=\"display:none\"><span class=\"k\">&nbsp;&nbsp;1h</span><span id=\"hmmRegime1h\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Confidence</span><span id=\"hmmConfidence\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Bias Signal</span><span id=\"hmmBias\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Blend</span><span id=\"hmmBlend\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Data Window (1m)</span><span id=\"hmmWindow\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Data Window (15m)</span><span id=\"hmmWindowSecondary\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Data Window (1h)</span><span id=\"hmmWindowTertiary\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Training</span><span id=\"hmmTrainingSummary\" class=\"v\"></span></div>
        <div class=\"progress-track\"><div id=\"hmmTrainingBar\" class=\"progress-fill\"></div></div>
        <div class=\"row\"><span class=\"k\">Training ETA</span><span id=\"hmmTrainingEta\" class=\"v\"></span></div>
        <div id=\"hmmHints\" class=\"tiny\"></div>

        <h3 style=\"margin-top:14px\">AI Regime Advisor</h3>
        <div id=\"aiRegimeCard\" class=\"ai-regime-card disabled\">
          <div class=\"row\"><span class=\"k\">Status</span><span id=\"aiRegimeStatus\" class=\"v\">OFF</span></div>
          <div class=\"row\"><span class=\"k\">Mechanical</span><span id=\"aiRegimeMechanical\" class=\"v\">-</span></div>
          <div class=\"row\"><span class=\"k\">AI Opinion</span><span id=\"aiRegimeOpinion\" class=\"v\">-</span></div>
          <div class=\"row\"><span class=\"k\">Provider</span><span id=\"aiRegimeProvider\" class=\"v\">-</span></div>
          <div class=\"row\"><span class=\"k\">Accum Signal</span><span id=\"aiRegimeAccumSignal\" class=\"v\">-</span></div>
          <div class=\"row\"><span class=\"k\">Conviction</span><span id=\"aiRegimeConviction\" class=\"v\">-</span></div>
          <div class=\"row\"><span class=\"k\">Next Check</span><span id=\"aiRegimeNextRun\" class=\"v\">-</span></div>
          <div id=\"aiRegimeRationale\" class=\"tiny ai-rationale\"></div>
          <div id=\"aiRegimeWatch\" class=\"tiny ai-watch\"></div>
          <div id=\"aiRegimeActions\" class=\"ai-regime-actions\">
            <button id=\"aiApplyOverrideBtn\" type=\"button\">Apply Override (30m)</button>
            <button id=\"aiDismissBtn\" type=\"button\">Dismiss</button>
            <button id=\"aiRevertBtn\" type=\"button\">Revert to Mechanical</button>
          </div>
        </div>

        <h3 style=\"margin-top:14px\">Strategic Accumulation</h3>
        <div class=\"row\"><span class=\"k\">State</span><span id=\"accumState\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Trigger</span><span id=\"accumTrigger\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Budget</span><span id=\"accumBudget\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Spent</span><span id=\"accumSpent\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Remaining</span><span id=\"accumRemaining\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Drawdown</span><span id=\"accumDrawdown\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">AI Signal</span><span id=\"accumAiSignal\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Buys</span><span id=\"accumBuys\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Avg Price</span><span id=\"accumAvgPrice\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Cooldown</span><span id=\"accumCooldown\" class=\"v\"></span></div>
        <div id=\"accumLastSession\" class=\"tiny\"></div>

        <h3 style=\"margin-top:14px\">Throughput Sizer</h3>
        <div class=\"row\"><span class=\"k\">Status</span><span id=\"throughputStatus\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Active Regime</span><span id=\"throughputActive\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Samples</span><span id=\"throughputSamples\" class=\"v\"></span></div>
        <div class=\"row\"><span id=\"throughputAgeLabel\" class=\"k\">Age Pressure (p90)</span><span id=\"throughputAgePressure\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Utilization</span><span id=\"throughputUtilization\" class=\"v\"></span></div>
        <div id=\"throughputBuckets\" class=\"tiny\"></div>

        <h3 style=\"margin-top:14px\">Directional Regime</h3>
        <div class=\"row\"><span class=\"k\">Tier</span><span id=\"regTier\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Suppressed</span><span id=\"regSuppressed\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Favored</span><span id=\"regFavored\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Gates</span><span id=\"regGates\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Grace</span><span id=\"regGrace\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Cooldown</span><span id=\"regCooldown\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Suppressed Slots</span><span id=\"regSuppressedSlots\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Dwell</span><span id=\"regDwell\" class=\"v\"></span></div>
        <div class=\"row\"><span class=\"k\">Last Eval</span><span id=\"regLastEval\" class=\"v\"></span></div>
        <div id=\"regHints\" class=\"tiny\"></div>
        <div id=\"regTransitions\" class=\"tiny\"></div>

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
            <tr><th>Type</th><th>Trade</th><th>Cycle</th><th>Volume</th><th>Price</th><th>Txid</th><th>Action</th></tr>
          </thead>
          <tbody id=\"ordersBody\"></tbody>
        </table>

        <h3 id=\"orphansTitle\" style=\"margin-top:12px\">Waiting Exits</h3>
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
|  :release <slot> [local_id|A|B]      |
|  :release_eligible [slot]            |
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
      'pause', 'resume', 'add', 'remove', 'close', 'release', 'release_eligible', 'audit', 'drift', 'stale',
      'set entry', 'set profit', 'jump', 'layer add', 'layer remove', 'q',
    ];
    const CONTROL_INPUT_IDS = new Set(['entryInput', 'profitInput', 'layerSourceSelect']);

    function fmt(n, d=6) {
      if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
      return Number(n).toFixed(d);
    }

    function fmtAgeSeconds(rawSeconds) {
      const seconds = Number(rawSeconds || 0);
      if (!Number.isFinite(seconds) || seconds <= 0) return '0s';
      if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)}d`;
      if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}h`;
      if (seconds >= 60) return `${(seconds / 60).toFixed(1)}m`;
      return `${Math.round(seconds)}s`;
    }

    function isStickyModeEnabled() {
      return Boolean(state && state.sticky_mode && state.sticky_mode.enabled);
    }

    function isRecoveryOrdersEnabled() {
      if (!state) return true;
      return state.recovery_orders_enabled !== false;
    }

    function commandCompletions() {
      if (!isStickyModeEnabled() && isRecoveryOrdersEnabled()) return COMMAND_COMPLETIONS;
      return COMMAND_COMPLETIONS.filter((cmd) => cmd !== 'close' && cmd !== 'stale');
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
        if (!isRecoveryOrdersEnabled()) return {error: 'stale disabled when recovery orders are disabled'};
        if (isStickyModeEnabled()) return {error: 'stale disabled in sticky mode; use :release'};
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
        if (!isRecoveryOrdersEnabled()) return {error: 'close disabled when recovery orders are disabled'};
        if (isStickyModeEnabled()) return {error: 'close disabled in sticky mode; use :release'};
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

      if (verb === 'release') {
        if (tokens.length < 2) return {error: 'usage: :release <slot> [local_id|A|B]'};
        if (tokens.length > 3) return {error: 'usage: :release <slot> [local_id|A|B]'};
        const slotId = parseNonNegativeInt(tokens[1]);
        if (slotId === null) return {error: 'slot id must be a non-negative integer'};
        const payload = {slot_id: slotId};
        if (tokens.length === 3) {
          const selector = String(tokens[2] || '').trim();
          const selectorInt = parseNonNegativeInt(selector);
          if (selectorInt !== null) {
            payload.local_id = selectorInt;
          } else {
            const tradeId = selector.toUpperCase();
            if (tradeId !== 'A' && tradeId !== 'B') return {error: 'selector must be local_id or trade A/B'};
            payload.trade_id = tradeId;
          }
        }
        return {type: 'action', action: 'release_slot', payload};
      }

      if (verb === 'release_eligible') {
        if (!isStickyModeEnabled()) return {error: 'release_eligible is for sticky mode'};
        if (tokens.length > 2) return {error: 'usage: :release_eligible [slot]'};
        let slotId = selectedSlot;
        if (tokens.length === 2) {
          const parsed = parseNonNegativeInt(tokens[1]);
          if (parsed === null) return {error: 'slot id must be a non-negative integer'};
          slotId = parsed;
        }
        if (!slotId || slotId < 0) return {error: 'no selected slot; use :release_eligible <slot>'};
        return {type: 'action', action: 'release_oldest_eligible', payload: {slot_id: slotId}};
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
      if (!isRecoveryOrdersEnabled()) {
        showToast('close disabled: recovery orders are disabled', 'error');
        return;
      }
      openConfirmDialog('Close oldest waiting exit?', async () => {
        await dispatchAction('soft_close_next');
      });
    }

    function requestReconcileDrift() {
      openConfirmDialog('Reconcile drift now? This cancels Kraken-only unknown orders for the active pair.', async () => {
        await dispatchAction('reconcile_drift');
      });
    }

    function requestCancelStaleRecoveries(minDistancePct = 3.0, maxBatch = 8) {
      if (!isRecoveryOrdersEnabled()) {
        showToast('refresh waiting disabled: recovery orders are disabled', 'error');
        return;
      }
      openConfirmDialog(
        `Refresh stale waiting exits now? min_distance=${minDistancePct}%, max_batch=${maxBatch}.`,
        async () => {
          await dispatchAction('cancel_stale_recoveries', {
            min_distance_pct: minDistancePct,
            max_batch: maxBatch,
          });
        },
      );
    }

    function requestSoftClose(slotId, recoveryId) {
      if (!isRecoveryOrdersEnabled()) {
        showToast('close disabled: recovery orders are disabled', 'error');
        return;
      }
      openConfirmDialog(`Close waiting exit #${recoveryId} on slot #${slotId}?`, async () => {
        await dispatchAction('soft_close', {slot_id: slotId, recovery_id: recoveryId});
      });
    }

    function requestRelease(slotId, payload = {}) {
      const localId = payload && payload.local_id !== undefined ? payload.local_id : null;
      const tradeId = payload && payload.trade_id ? payload.trade_id : '';
      let selector = 'oldest exit';
      if (localId !== null) selector = `exit #${localId}`;
      if (tradeId) selector = `trade ${tradeId}`;
      openConfirmDialog(`Release ${selector} on slot #${slotId}?`, async () => {
        await dispatchAction('release_slot', payload);
      });
    }

    function requestReleaseOldestEligible(slotId) {
      openConfirmDialog(`Release oldest eligible exit on slot #${slotId}?`, async () => {
        await dispatchAction('release_oldest_eligible', {slot_id: slotId});
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

    function requestAiRegimeOverride(ttlSec = 1800) {
      const ttl = Number.isFinite(Number(ttlSec)) ? Math.max(1, Math.round(Number(ttlSec))) : 1800;
      openConfirmDialog(`Apply AI regime override for ${fmtAgeSeconds(ttl)}?`, async () => {
        await dispatchAction('ai_regime_override', {ttl_sec: ttl});
      });
    }

    function requestAiRegimeDismiss() {
      openConfirmDialog('Dismiss current AI disagreement?', async () => {
        await dispatchAction('ai_regime_dismiss');
      });
    }

    function requestAiRegimeRevert() {
      openConfirmDialog('Revert to mechanical regime now?', async () => {
        await dispatchAction('ai_regime_revert');
      });
    }

    function requestAccumStop() {
      openConfirmDialog('Stop active accumulation session now?', async () => {
        await dispatchAction('accum_stop');
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
      const choices = commandCompletions();
      if (!pref) return choices.slice(0, 5);
      return choices.filter((cmd) => cmd.startsWith(pref)).slice(0, 5);
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
      if (parsed.action === 'release_slot') {
        requestRelease(parsed.payload.slot_id, parsed.payload);
        return;
      }
      if (parsed.action === 'release_oldest_eligible') {
        requestReleaseOldestEligible(parsed.payload.slot_id);
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
      const nowSec = Date.now() / 1000;
      const mode = document.getElementById('mode');
      mode.textContent = s.mode;
      mode.className = 'badge ' + (s.mode === 'RUNNING' ? 'ok' : s.mode === 'PAUSED' ? 'pause' : 'halt');

      const phase = document.getElementById('phase');
      const phaseSymbols = {S0: '\u25CF', S1a: '\u25BC', S1b: '\u25B2', S2: '\u25A0'};
      const sym = phaseSymbols[s.top_phase] || '';
      phase.textContent = sym ? `${sym} ${s.top_phase}` : `PHASE ${s.top_phase}`;
      phase.className = 'badge ' + (s.top_phase || '');

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

      const sticky = s.sticky_mode || {};
      const stickyEnabled = Boolean(sticky.enabled);
      const recoveryOrdersEnabled = s.recovery_orders_enabled !== false;
      const vintage = s.slot_vintage || {};
      const waitingFresh = Number(vintage.fresh_0_1h || 0);
      const waitingAging = Number(vintage.aging_1_6h || 0);
      const waitingStale = Number(vintage.stale_6_24h || 0);
      const waitingOld = Number(vintage.old_1_7d || 0);
      const waitingAncient = Number(vintage.ancient_7d_plus || 0);
      const waitingTotal = waitingFresh + waitingAging + waitingStale + waitingOld + waitingAncient;

      document.getElementById('orphansLabel').textContent =
        recoveryOrdersEnabled ? 'Waiting Exits' : 'Waiting Exits (disabled)';
      if (recoveryOrdersEnabled) {
        document.getElementById('orphans').textContent = stickyEnabled ? waitingTotal : (waitingTotal || s.total_orphans);
      } else {
        document.getElementById('orphans').textContent = 0;
      }
      const dust = s.dust_sweep || {};
      const dustRowEl = document.getElementById('dustSweepRow');
      const dustValueEl = document.getElementById('dustSweep');
      const dustDetailsEl = document.getElementById('dustSweepDetails');
      const dustDividend = Number(dust.current_dividend_usd || 0);
      const dustLifetime = Number(dust.lifetime_absorbed_usd || 0);
      if (dustDividend > 0) {
        dustRowEl.style.display = '';
        dustDetailsEl.style.display = '';
        dustValueEl.textContent = `$${fmt(dustDividend, 2)}/slot available`;
        dustDetailsEl.textContent = `$${fmt(dustLifetime, 2)} lifetime absorbed`;
      } else {
        dustRowEl.style.display = 'none';
        dustDetailsEl.style.display = 'none';
        dustValueEl.textContent = '';
        dustDetailsEl.textContent = '';
      }
      document.getElementById('stickyModeStatus').textContent = stickyEnabled
        ? `ON (${String(sticky.compounding_mode || 'legacy_profit')})`
        : 'OFF';
      document.getElementById('stickyModeStatus').style.color = stickyEnabled ? 'var(--good)' : '';
      document.getElementById('vintageWaiting').textContent = String(waitingTotal);
      document.getElementById('vintageOldest').textContent = fmtAgeSeconds(Number(vintage.oldest_exit_age_sec || 0));
      document.getElementById('vintageStuck').textContent =
        `$${fmt(vintage.stuck_capital_usd, 2)} (${fmt(vintage.stuck_capital_pct, 1)}%)`;
      document.getElementById('vintageEligible').textContent =
        `${Number(vintage.vintage_release_eligible || 0)} (regime ${fmt(vintage.regime_strength_adx_proxy, 1)})`;

      const vintageBar = document.getElementById('vintageBar');
      const vintageLegend = document.getElementById('vintageLegend');
      vintageBar.innerHTML = '';
      const bucketRows = [
        {key: 'fresh', count: waitingFresh, color: '#2ea043', label: '0-1h'},
        {key: 'aging', count: waitingAging, color: '#58a6ff', label: '1-6h'},
        {key: 'stale', count: waitingStale, color: '#d29922', label: '6-24h'},
        {key: 'old', count: waitingOld, color: '#f0883e', label: '1-7d'},
        {key: 'ancient', count: waitingAncient, color: '#f85149', label: '7d+'},
      ];
      if (waitingTotal <= 0) {
        const seg = document.createElement('div');
        seg.className = 'vintage-seg';
        seg.style.width = '100%';
        seg.style.background = '#30363d';
        vintageBar.appendChild(seg);
        vintageLegend.textContent = 'No waiting exits';
      } else {
        for (const row of bucketRows) {
          if (row.count <= 0) continue;
          const seg = document.createElement('div');
          seg.className = 'vintage-seg';
          seg.style.width = `${Math.max(2, (row.count / waitingTotal) * 100)}%`;
          seg.style.background = row.color;
          seg.title = `${row.label}: ${row.count}`;
          vintageBar.appendChild(seg);
        }
        vintageLegend.textContent =
          `0-1h:${waitingFresh} | 1-6h:${waitingAging} | 6-24h:${waitingStale} | 1-7d:${waitingOld} | 7d+:${waitingAncient}`;
      }

      const release = s.release_health || {};
      const releaseGateStatusEl = document.getElementById('releaseGateStatus');
      const releaseGateBlocked = Boolean(release.recon_hard_gate_blocked);
      releaseGateStatusEl.textContent = releaseGateBlocked ? 'BLOCKED' : 'CLEAR';
      releaseGateStatusEl.style.color = releaseGateBlocked ? 'var(--bad)' : 'var(--good)';
      const releaseLastAt = release.sticky_release_last_at;
      const releaseAgo = releaseLastAt ? fmtAgeSeconds((Date.now() / 1000) - Number(releaseLastAt)) : null;
      document.getElementById('releaseTotals').textContent =
        releaseLastAt
          ? `${Number(release.sticky_release_total || 0)} total (${releaseAgo} ago)`
          : `${Number(release.sticky_release_total || 0)} total`;
      document.getElementById('releaseGateReason').textContent =
        releaseGateBlocked ? String(release.recon_hard_gate_reason || '') : '';

      const hmm = s.hmm_regime || {};
      const hmmConsensus = s.hmm_consensus || {};
      const hmmPipe = s.hmm_data_pipeline || {};
      const hmmPipeSecondary = s.hmm_data_pipeline_secondary || {};
      const hmmPipeTertiary = s.hmm_data_pipeline_tertiary || {};
      const hmmStatusEl = document.getElementById('hmmStatus');
      const hmmEnabled = Boolean(hmm.enabled);
      const hmmAvailable = Boolean(hmm.available);
      const hmmTrained = Boolean(hmm.trained);
      let hmmStatus = 'OFF';
      let hmmStatusColor = '';
      if (hmmEnabled && hmmAvailable && hmmTrained) {
        hmmStatus = 'ACTIVE';
        hmmStatusColor = 'var(--good)';
      } else if (hmmEnabled && hmmAvailable && !hmmTrained) {
        hmmStatus = 'WARMING';
        hmmStatusColor = 'var(--warn)';
      } else if (hmmEnabled && !hmmAvailable) {
        hmmStatus = 'UNAVAILABLE';
        hmmStatusColor = 'var(--warn)';
      }
      hmmStatusEl.textContent = hmmStatus;
      hmmStatusEl.style.color = hmmStatusColor;

      const sourceModeRaw = hmmConsensus.source_mode || hmm.source_mode || 'primary';
      const sourceMode = String(sourceModeRaw || 'primary').toLowerCase();
      const agreement = hmmConsensus.agreement || hmm.agreement || (sourceMode === 'consensus' ? '-' : 'primary_only');
      document.getElementById('hmmSource').textContent =
        `${sourceMode.toUpperCase()}${agreement ? ` (${String(agreement)})` : ''}`;

      const regimeLabel = hmm.regime ? String(hmm.regime) : '-';
      const regimeId = hmm.regime_id;
      document.getElementById('hmmRegime').textContent =
        regimeId === null || regimeId === undefined ? regimeLabel : `${regimeLabel} (id ${regimeId})`;

      const hmmPrimary = hmm.primary || {};
      const hmmSecondary = hmm.secondary || {};
      const hmmTertiary = hmm.tertiary || {};
      const isMultiTF = Boolean(hmm.multi_timeframe) && hmmPrimary.regime && hmmSecondary.regime;
      const hasTertiary = Boolean(hmmTertiary.regime) && (Boolean(hmmTertiary.enabled) || Boolean(hmmPipeTertiary.enabled));
      const row1m = document.getElementById('hmmRegime1mRow');
      const row15m = document.getElementById('hmmRegime15mRow');
      const row1h = document.getElementById('hmmRegime1hRow');
      if (isMultiTF) {
        row1m.style.display = '';
        row15m.style.display = '';
        const conf1m = hmmPrimary.confidence;
        const conf15m = hmmSecondary.confidence;
        const confStr1m = conf1m === null || conf1m === undefined ? '' : ` (${fmt(Number(conf1m) * 100, 0)}%)`;
        const confStr15m = conf15m === null || conf15m === undefined ? '' : ` (${fmt(Number(conf15m) * 100, 0)}%)`;
        document.getElementById('hmmRegime1m').textContent = `${String(hmmPrimary.regime)}${confStr1m}`;
        document.getElementById('hmmRegime15m').textContent = `${String(hmmSecondary.regime)}${confStr15m}`;
      } else {
        row1m.style.display = 'none';
        row15m.style.display = 'none';
      }
      if (hasTertiary) {
        row1h.style.display = '';
        const conf1h = hmmTertiary.confidence;
        const confStr1h = conf1h === null || conf1h === undefined ? '' : ` (${fmt(Number(conf1h) * 100, 0)}%)`;
        document.getElementById('hmmRegime1h').textContent = `${String(hmmTertiary.regime)}${confStr1h}`;
      } else {
        row1h.style.display = 'none';
      }

      const confidence = hmm.confidence;
      document.getElementById('hmmConfidence').textContent =
        confidence === null || confidence === undefined ? '-' : `${fmt(Number(confidence) * 100, 1)}%`;

      const biasSignal = hmm.bias_signal;
      document.getElementById('hmmBias').textContent =
        biasSignal === null || biasSignal === undefined
          ? '-'
          : `${Number(biasSignal) >= 0 ? '+' : ''}${fmt(Number(biasSignal) * 100, 2)}%`;

      const blendFactor = hmm.blend_factor;
      document.getElementById('hmmBlend').textContent =
        blendFactor === null || blendFactor === undefined ? '-' : fmt(Number(blendFactor), 2);

      const samples = Number(hmmPipe.samples || 0);
      const target = Number(hmmPipe.training_target || 0);
      const coverage = hmmPipe.coverage_pct;
      const spanHours = hmmPipe.span_hours;
      const freshnessSec = hmmPipe.freshness_sec;
      let windowText = '-';
      if (target > 0 || samples > 0) {
        const covText = coverage === null || coverage === undefined ? '' : ` (${fmt(Number(coverage), 1)}%)`;
        const spanText = spanHours === null || spanHours === undefined ? '' : `, ${fmt(Number(spanHours), 1)}h`;
        windowText = `${samples}/${target || '-'}${covText}${spanText}`;
      }
      document.getElementById('hmmWindow').textContent = windowText;

      const samples2 = Number(hmmPipeSecondary.samples || 0);
      const target2 = Number(hmmPipeSecondary.training_target || 0);
      const coverage2 = hmmPipeSecondary.coverage_pct;
      const spanHours2 = hmmPipeSecondary.span_hours;
      let windowText2 = '-';
      if (target2 > 0 || samples2 > 0) {
        const covText2 = coverage2 === null || coverage2 === undefined ? '' : ` (${fmt(Number(coverage2), 1)}%)`;
        const spanText2 = spanHours2 === null || spanHours2 === undefined ? '' : `, ${fmt(Number(spanHours2), 1)}h`;
        windowText2 = `${samples2}/${target2 || '-'}${covText2}${spanText2}`;
      }
      document.getElementById('hmmWindowSecondary').textContent = windowText2;

      const samples3 = Number(hmmPipeTertiary.samples || 0);
      const target3 = Number(hmmPipeTertiary.training_target || 0);
      const coverage3 = hmmPipeTertiary.coverage_pct;
      const spanHours3 = hmmPipeTertiary.span_hours;
      let windowText3 = '-';
      if (target3 > 0 || samples3 > 0) {
        const covText3 = coverage3 === null || coverage3 === undefined ? '' : ` (${fmt(Number(coverage3), 1)}%)`;
        const spanText3 = spanHours3 === null || spanHours3 === undefined ? '' : `, ${fmt(Number(spanHours3), 1)}h`;
        windowText3 = `${samples3}/${target3 || '-'}${covText3}${spanText3}`;
      }
      document.getElementById('hmmWindowTertiary').textContent = windowText3;

      const depth = (hmm && typeof hmm.training_depth === 'object') ? hmm.training_depth : {};
      const depthCurrent = Number(depth.current_candles || 0);
      const depthTarget = Number(depth.target_candles || 0);
      const depthPctRaw = Number(depth.pct_complete || (depthTarget > 0 ? (depthCurrent / depthTarget) * 100 : 0));
      const depthPct = Math.max(0, Math.min(100, Number.isFinite(depthPctRaw) ? depthPctRaw : 0));
      const depthTier = String(depth.quality_tier || 'shallow').toLowerCase();
      const depthMod = Number(depth.confidence_modifier || 1.0);
      const depthSummaryEl = document.getElementById('hmmTrainingSummary');
      const depthEtaEl = document.getElementById('hmmTrainingEta');
      const depthBarEl = document.getElementById('hmmTrainingBar');
      const tierLabelMap = { shallow: 'Shallow', baseline: 'Baseline', deep: 'Deep', full: 'Full' };
      const tierColorMap = {
        shallow: 'var(--muted)',
        baseline: 'var(--warn)',
        deep: '#3fb950',
        full: 'var(--good)',
      };
      const currentStr = Number.isFinite(depthCurrent) ? Math.round(depthCurrent).toLocaleString() : '-';
      const targetStr = Number.isFinite(depthTarget) && depthTarget > 0 ? Math.round(depthTarget).toLocaleString() : '-';
      const tierLabel = tierLabelMap[depthTier] || 'Shallow';
      depthSummaryEl.textContent = `${currentStr}/${targetStr} (${fmt(depthPct, 1)}%) - ${tierLabel} (x${fmt(depthMod, 2)})`;
      depthSummaryEl.style.color = tierColorMap[depthTier] || 'var(--muted)';
      depthBarEl.style.width = `${depthPct}%`;
      depthBarEl.className = `progress-fill ${depthTier in tierLabelMap ? depthTier : 'shallow'}`;

      const etaIso = depth.estimated_full_at;
      if (etaIso) {
        const etaTs = Date.parse(String(etaIso));
        if (Number.isFinite(etaTs)) {
          const etaSec = Math.max(0, etaTs / 1000 - nowSec);
          depthEtaEl.textContent = etaSec > 0 ? `~${fmtAgeSeconds(etaSec)}` : 'full';
        } else {
          depthEtaEl.textContent = '-';
        }
      } else {
        depthEtaEl.textContent = depthTarget > 0 && depthCurrent >= depthTarget ? 'full' : '-';
      }

      const hmmHints = [];
      if (Array.isArray(hmmPipe.gaps) && hmmPipe.gaps.length) {
        hmmHints.push(...hmmPipe.gaps.map(x => String(x)));
      }
      if (Array.isArray(hmmPipeSecondary.gaps) && hmmPipeSecondary.gaps.length) {
        hmmHints.push(...hmmPipeSecondary.gaps.map(x => `secondary:${String(x)}`));
      }
      if (Array.isArray(hmmPipeTertiary.gaps) && hmmPipeTertiary.gaps.length) {
        hmmHints.push(...hmmPipeTertiary.gaps.map(x => `tertiary:${String(x)}`));
      }
      if (hmm.error) {
        hmmHints.push(`error:${String(hmm.error)}`);
      }
      if (hmmPipe.backfill_last_message) {
        hmmHints.push(`backfill:${String(hmmPipe.backfill_last_message)}`);
      }
      if (hmmPipeSecondary.backfill_last_message) {
        hmmHints.push(`backfill_secondary:${String(hmmPipeSecondary.backfill_last_message)}`);
      }
      if (hmmPipeTertiary.backfill_last_message) {
        hmmHints.push(`backfill_tertiary:${String(hmmPipeTertiary.backfill_last_message)}`);
      }
      if (hmmPipe.freshness_ok === false && freshnessSec !== null && freshnessSec !== undefined) {
        hmmHints.push(`stale:${Math.round(Number(freshnessSec))}s`);
      }
      if (hmmPipeSecondary.freshness_ok === false && hmmPipeSecondary.freshness_sec !== null && hmmPipeSecondary.freshness_sec !== undefined) {
        hmmHints.push(`stale_secondary:${Math.round(Number(hmmPipeSecondary.freshness_sec))}s`);
      }
      if (hmmPipeTertiary.freshness_ok === false && hmmPipeTertiary.freshness_sec !== null && hmmPipeTertiary.freshness_sec !== undefined) {
        hmmHints.push(`stale_tertiary:${Math.round(Number(hmmPipeTertiary.freshness_sec))}s`);
      }
      if (hmmPipe.backfill_last_at) {
        const agoSec = (Date.now() / 1000) - Number(hmmPipe.backfill_last_at);
        const rows = Number(hmmPipe.backfill_last_rows || 0);
        hmmHints.push(`last_backfill:${rows} rows, ${fmtAgeSeconds(agoSec)} ago`);
      }
      if (hmmPipeSecondary.backfill_last_at) {
        const agoSec2 = (Date.now() / 1000) - Number(hmmPipeSecondary.backfill_last_at);
        const rows2 = Number(hmmPipeSecondary.backfill_last_rows || 0);
        hmmHints.push(`last_backfill_secondary:${rows2} rows, ${fmtAgeSeconds(agoSec2)} ago`);
      }
      if (hmmPipeTertiary.backfill_last_at) {
        const agoSec3 = (Date.now() / 1000) - Number(hmmPipeTertiary.backfill_last_at);
        const rows3 = Number(hmmPipeTertiary.backfill_last_rows || 0);
        hmmHints.push(`last_backfill_tertiary:${rows3} rows, ${fmtAgeSeconds(agoSec3)} ago`);
      }
      document.getElementById('hmmHints').textContent =
        hmmHints.length ? `Hints: ${hmmHints.join(' | ')}` : '';

      // --- AI Regime Advisor ---
      const ai = s.ai_regime_advisor || {};
      const aiOpinion = (ai && typeof ai.opinion === 'object') ? ai.opinion : {};
      const aiOverride = (ai && typeof ai.override === 'object') ? ai.override : {};
      const aiEnabled = Boolean(ai.enabled);
      const aiDismissed = Boolean(ai.dismissed);
      const aiAgreement = String(aiOpinion.agreement || 'unknown');
      const aiError = String(aiOpinion.error || '');
      const aiConviction = Number(aiOpinion.conviction || 0);
      const aiPanelist = String(aiOpinion.panelist || '');
      const aiProvider = String(aiOpinion.provider || '').trim();
      const aiModel = String(aiOpinion.model || '').trim();
      const aiAccumulationSignal = String(aiOpinion.accumulation_signal || 'hold').trim().toLowerCase();
      const aiAccumulationConviction = Number(aiOpinion.accumulation_conviction || 0);
      const minConviction = Number(ai.min_conviction || 50);
      const defaultTtlSec = Number(ai.default_ttl_sec || 1800);
      const capacityBand = String((s.capacity_fill_health || {}).status_band || '').toLowerCase();

      const aiCardEl = document.getElementById('aiRegimeCard');
      const aiStatusEl = document.getElementById('aiRegimeStatus');
      const aiMechanicalEl = document.getElementById('aiRegimeMechanical');
      const aiOpinionEl = document.getElementById('aiRegimeOpinion');
      const aiProviderEl = document.getElementById('aiRegimeProvider');
      const aiAccumSignalEl = document.getElementById('aiRegimeAccumSignal');
      const aiConvictionEl = document.getElementById('aiRegimeConviction');
      const aiNextRunEl = document.getElementById('aiRegimeNextRun');
      const aiRationaleEl = document.getElementById('aiRegimeRationale');
      const aiWatchEl = document.getElementById('aiRegimeWatch');
      const aiApplyBtn = document.getElementById('aiApplyOverrideBtn');
      const aiDismissBtn = document.getElementById('aiDismissBtn');
      const aiRevertBtn = document.getElementById('aiRevertBtn');

      const fmtTierDir = (tierRaw, dirRaw) => {
        const tier = Math.max(0, Math.min(2, Number(tierRaw || 0)));
        const dir = String(dirRaw || 'symmetric').toLowerCase();
        if (tier <= 0) return 'Tier 0 symmetric';
        if (dir === 'long_bias') return `Tier ${tier} long_bias`;
        if (dir === 'short_bias') return `Tier ${tier} short_bias`;
        return `Tier ${tier} symmetric`;
      };

      const mechTier = Number(
        aiOpinion.mechanical_tier != null
          ? aiOpinion.mechanical_tier
          : ((s.regime_directional || {}).mechanical_tier != null
              ? (s.regime_directional || {}).mechanical_tier
              : ((s.regime_directional || {}).tier || 0)),
      );
      const mechDir = String(
        aiOpinion.mechanical_direction
          || (s.regime_directional || {}).mechanical_direction
          || 'symmetric',
      );
      const opTier = Number(aiOpinion.recommended_tier || 0);
      const opDir = String(aiOpinion.recommended_direction || 'symmetric');
      const disagreement = ['ai_upgrade', 'ai_downgrade', 'ai_flip'].includes(aiAgreement) && !aiDismissed && !aiError;
      const overrideActive = Boolean(aiOverride.active);

      aiCardEl.className = 'ai-regime-card';
      aiStatusEl.style.color = '';
      aiMechanicalEl.textContent = fmtTierDir(mechTier, mechDir);
      aiOpinionEl.textContent = fmtTierDir(opTier, opDir);
      const providerSummary = aiProvider
        ? (aiModel ? `${aiProvider}/${aiModel}` : aiProvider)
        : (aiPanelist || '-');
      aiProviderEl.textContent = providerSummary || '-';
      const accumSignalNorm = ['accumulate_doge', 'hold', 'accumulate_usd'].includes(aiAccumulationSignal)
        ? aiAccumulationSignal
        : 'hold';
      const accumSignalConv = Number.isFinite(aiAccumulationConviction)
        ? `${Math.max(0, Math.min(100, Math.round(aiAccumulationConviction)))}%`
        : '0%';
      aiAccumSignalEl.textContent = `${accumSignalNorm} (${accumSignalConv})`;
      aiConvictionEl.textContent = Number.isFinite(aiConviction)
        ? `${Math.max(0, Math.min(100, Math.round(aiConviction)))}%${aiPanelist ? ` (${aiPanelist})` : ''}`
        : '-';
      aiNextRunEl.textContent = ai.next_run_in_sec == null ? '-' : `~${fmtAgeSeconds(Number(ai.next_run_in_sec || 0))}`;
      aiRationaleEl.textContent = aiOpinion.rationale ? `"${String(aiOpinion.rationale)}"` : '';
      aiWatchEl.textContent = aiOpinion.watch_for ? `Watch: ${String(aiOpinion.watch_for)}` : '';

      aiApplyBtn.style.display = 'none';
      aiDismissBtn.style.display = 'none';
      aiRevertBtn.style.display = 'none';
      aiApplyBtn.disabled = true;

      if (!aiEnabled) {
        aiCardEl.classList.add('disabled');
        aiStatusEl.textContent = 'OFF';
        aiStatusEl.style.color = 'var(--muted)';
        aiRationaleEl.textContent = 'AI regime advisor is disabled.';
        aiWatchEl.textContent = '';
      } else if (overrideActive) {
        aiCardEl.classList.add('override');
        aiStatusEl.textContent = 'OVERRIDE ACTIVE';
        aiStatusEl.style.color = '#f0883e';
        aiOpinionEl.textContent = fmtTierDir(aiOverride.tier, aiOverride.direction);
        aiMechanicalEl.textContent = `Mechanical would be ${fmtTierDir(mechTier, mechDir)}`;
        aiConvictionEl.textContent = aiOverride.source_conviction == null
          ? '-'
          : `${Math.max(0, Math.min(100, Math.round(Number(aiOverride.source_conviction))))}%`;
        aiNextRunEl.textContent = aiOverride.remaining_sec == null
          ? '-'
          : `${fmtAgeSeconds(Number(aiOverride.remaining_sec || 0))} remaining`;
        aiRationaleEl.textContent = 'Mechanical tier continues running underneath this temporary override.';
        aiWatchEl.textContent = '';
        aiRevertBtn.style.display = '';
      } else if (disagreement) {
        aiCardEl.classList.add('disagree');
        aiStatusEl.textContent = 'DISAGREES';
        aiStatusEl.style.color = 'var(--warn)';
        aiApplyBtn.style.display = '';
        aiDismissBtn.style.display = '';
        const isUpgrade = aiAgreement === 'ai_upgrade';
        const blockedByCapacity = isUpgrade && capacityBand === 'stop';
        const convictionOk = Number.isFinite(aiConviction) && aiConviction >= minConviction;
        aiApplyBtn.disabled = !(convictionOk && !blockedByCapacity);
        if (!convictionOk) {
          aiWatchEl.textContent = `Override disabled: conviction ${Math.round(aiConviction)}% below floor ${Math.round(minConviction)}%.`;
        } else if (blockedByCapacity) {
          aiWatchEl.textContent = 'Override disabled: capacity stop gate blocks upgrades.';
        }
      } else {
        aiCardEl.classList.add('agree');
        if (aiError) {
          aiStatusEl.textContent = 'UNAVAILABLE';
          aiStatusEl.style.color = 'var(--warn)';
          aiRationaleEl.textContent = `AI advisor error: ${aiError}`;
          aiWatchEl.textContent = '';
        } else if (aiDismissed) {
          aiStatusEl.textContent = 'DISMISSED';
          aiStatusEl.style.color = 'var(--muted)';
          aiRationaleEl.textContent = 'Current disagreement dismissed until the next opinion cycle.';
        } else if (aiAgreement === 'agree') {
          aiStatusEl.textContent = 'AGREES';
          aiStatusEl.style.color = 'var(--good)';
        } else {
          aiStatusEl.textContent = 'WAITING';
          aiStatusEl.style.color = 'var(--muted)';
          if (!aiOpinion.ts) {
            aiRationaleEl.textContent = 'No AI opinion yet.';
          }
        }
      }

      const suggestedTtl = ai.suggested_ttl_sec || defaultTtlSec;
      aiApplyBtn.textContent = `Apply Override (${fmtAgeSeconds(suggestedTtl)})`;

      // --- Strategic Accumulation ---
      const accumInfo = s.accumulation || {};
      const accumEnabled = Boolean(accumInfo.enabled);
      const accumMode = String(accumInfo.state || 'IDLE').toUpperCase();
      const accumDirection = accumInfo.direction ? String(accumInfo.direction).toUpperCase() : '';
      let accumStateText = accumEnabled ? accumMode : 'OFF';
      if (accumEnabled && accumDirection && accumMode !== 'IDLE') {
        accumStateText = `${accumMode} (${accumDirection})`;
      }
      const accumStateEl = document.getElementById('accumState');
      accumStateEl.textContent = accumStateText;
      accumStateEl.style.color = '';
      if (!accumEnabled) {
        accumStateEl.style.color = 'var(--muted)';
      } else if (accumMode === 'ACTIVE') {
        accumStateEl.style.color = 'var(--good)';
      } else if (accumMode === 'ARMED') {
        accumStateEl.style.color = 'var(--warn)';
      } else if (accumMode === 'STOPPED') {
        accumStateEl.style.color = 'var(--bad)';
      }
      document.getElementById('accumTrigger').textContent = String(accumInfo.trigger || '-');
      const budgetUsd = Number(accumInfo.budget_usd || 0.0);
      const spentUsd = Number(accumInfo.spent_usd || 0.0);
      const remainingUsd = Number(accumInfo.budget_remaining_usd || 0.0);
      document.getElementById('accumBudget').textContent = accumEnabled ? `$${fmt(budgetUsd, 2)}` : '-';
      document.getElementById('accumSpent').textContent = accumEnabled ? `$${fmt(spentUsd, 2)}` : '-';
      document.getElementById('accumRemaining').textContent = accumEnabled ? `$${fmt(remainingUsd, 2)}` : '-';
      const drawdownNow = Number(accumInfo.current_drawdown_pct);
      const drawdownMax = Number(accumInfo.max_drawdown_pct);
      document.getElementById('accumDrawdown').textContent =
        accumEnabled && Number.isFinite(drawdownNow) && Number.isFinite(drawdownMax)
          ? `${fmt(drawdownNow, 2)}% / max ${fmt(drawdownMax, 2)}%`
          : '-';
      const accumAiSignal = String(accumInfo.ai_accumulation_signal || 'hold').toLowerCase();
      const accumAiConv = Math.max(0, Math.min(100, Number(accumInfo.ai_accumulation_conviction || 0)));
      document.getElementById('accumAiSignal').textContent =
        accumEnabled ? `${accumAiSignal} (${fmt(accumAiConv, 0)}%)` : '-';
      document.getElementById('accumBuys').textContent =
        accumEnabled ? String(Number(accumInfo.n_buys || 0)) : '-';
      const avgPrice = Number(accumInfo.avg_price);
      document.getElementById('accumAvgPrice').textContent =
        accumEnabled && Number.isFinite(avgPrice) && avgPrice > 0 ? `$${fmt(avgPrice, 6)}` : '-';
      const cooldownSec = Number(accumInfo.cooldown_remaining_sec || 0);
      document.getElementById('accumCooldown').textContent =
        accumEnabled ? (cooldownSec > 0 ? fmtAgeSeconds(cooldownSec) : 'ready') : '-';
      const accumLastSessionEl = document.getElementById('accumLastSession');
      const lastSession = (accumInfo && typeof accumInfo.last_session_summary === 'object')
        ? accumInfo.last_session_summary
        : null;
      if (lastSession && Object.keys(lastSession).length > 0) {
        const lsState = String(lastSession.state || '-').toUpperCase();
        const lsReason = String(lastSession.reason || '');
        const lsSpent = Number(lastSession.spent_usd || 0.0);
        const lsDoge = Number(lastSession.acquired_doge || 0.0);
        const lsBuys = Number(lastSession.n_buys || 0);
        accumLastSessionEl.textContent =
          `Last: ${lsState} (${lsReason || 'n/a'}) spent $${fmt(lsSpent, 2)} acquired ${fmt(lsDoge, 4)} DOGE in ${lsBuys} buys`;
      } else {
        accumLastSessionEl.textContent = '';
      }

      const throughput = s.throughput_sizer || { enabled: false };
      const throughputStatusEl = document.getElementById('throughputStatus');
      const throughputEnabled = Boolean(throughput.enabled);
      const throughputAggregate = (throughput && typeof throughput.aggregate === 'object') ? throughput.aggregate : null;
      const throughputActive = Boolean(throughputAggregate && throughputAggregate.sufficient_data);
      if (!throughputEnabled) {
        throughputStatusEl.textContent = 'OFF';
        throughputStatusEl.style.color = '';
      } else if (throughputActive) {
        throughputStatusEl.textContent = 'ACTIVE';
        throughputStatusEl.style.color = 'var(--good)';
      } else {
        throughputStatusEl.textContent = 'WARMING';
        throughputStatusEl.style.color = 'var(--warn)';
      }
      document.getElementById('throughputActive').textContent = String(throughput.active_regime || '-');
      document.getElementById('throughputSamples').textContent =
        throughputEnabled ? String(Number(throughput.last_update_n || 0)) : '-';
      const agePressureReference = String(throughput.age_pressure_reference || 'p90').toLowerCase();
      document.getElementById('throughputAgeLabel').textContent = `Age Pressure (${agePressureReference})`;
      const agePressureRaw = Number(throughput.age_pressure);
      const agePressure = Number.isFinite(agePressureRaw) ? agePressureRaw : 1.0;
      const ageRefRaw = Number(throughput.age_pressure_ref_age_sec);
      const ageRefSec = Number.isFinite(ageRefRaw) ? Math.max(0, ageRefRaw) : 0.0;
      let agePressureDetails = ' (healthy)';
      if (agePressure < 0.999) {
        const refText = ageRefSec > 0 ? fmtAgeSeconds(ageRefSec) : '-';
        agePressureDetails = ` (${agePressureReference} age: ${refText})`;
      }
      document.getElementById('throughputAgePressure').textContent =
        throughputEnabled ? `${fmt(agePressure * 100, 1)}%${agePressureDetails}` : '-';
      const utilRatio = Number(throughput.util_ratio || 0.0);
      document.getElementById('throughputUtilization').textContent =
        throughputEnabled ? `${fmt(utilRatio * 100, 1)}%` : '-';

      const throughputBucketNames = [
        'aggregate',
        'bearish_A',
        'bearish_B',
        'ranging_A',
        'ranging_B',
        'bullish_A',
        'bullish_B',
      ];
      const throughputBits = [];
      for (const name of throughputBucketNames) {
        const row = (throughput && typeof throughput[name] === 'object') ? throughput[name] : null;
        if (!row) {
          throughputBits.push(`${name}: no_data`);
          continue;
        }
        const n = Number(row.n_completed || 0);
        if (Boolean(row.sufficient_data)) {
          const mult = Number(row.multiplier || 1.0);
          const medianFillSec = Number(row.median_fill_sec || 0.0);
          const fillText = medianFillSec > 0 ? fmtAgeSeconds(medianFillSec) : '-';
          throughputBits.push(`${name}: x${fmt(mult, 2)} (${fillText})`);
        } else {
          throughputBits.push(`${name}: ${String(row.reason || 'insufficient_samples')} n=${n}`);
        }
      }
      document.getElementById('throughputBuckets').textContent =
        throughputEnabled ? throughputBits.join(' | ') : 'Throughput disabled';

      // --- Directional Regime ---
      const reg = s.regime_directional || {};
      const regEnabled = Boolean(reg.actuation_enabled);
      const regTier = Number(reg.tier || 0);
      const regLabel = String(reg.tier_label || 'symmetric');
      const regSuppressed = reg.suppressed_side || null;
      const regFavored = reg.favored_side || null;
      const regGraceSec = Number(reg.grace_remaining_sec || 0);
      const regCooldownSec = Number(reg.cooldown_remaining_sec || 0);
      const regCooldownSuppressed = reg.cooldown_suppressed_side || null;
      const regSuppressedSlots = Number(reg.regime_suppressed_slots || 0);
      const regDwellSec = Number(reg.dwell_sec || 0);
      const regReady = Boolean(reg.hmm_ready);
      const regOkT1 = Boolean(reg.directional_ok_tier1);
      const regOkT2 = Boolean(reg.directional_ok_tier2);
      const regReason = String(reg.reason || '');
      const regLastEval = Number(reg.last_eval_ts || 0);

      const tierColors = { 0: '#888', 1: '#f5a623', 2: '#e74c3c' };
      const tierColor = regEnabled
        ? (regCooldownSec > 0 ? '#f5a623' : (tierColors[regTier] || '#888'))
        : '#888';
      const tierBadge = regEnabled
        ? `<span style="color:${tierColor}">${regTier} - ${regLabel}</span>`
        : '<span style="color:#888">OFF</span>';
      document.getElementById('regTier').innerHTML = tierBadge;

      const regSideLabel = { A: 'A (short)', B: 'B (long)' };
      document.getElementById('regSuppressed').textContent =
        regEnabled && regSuppressed ? regSideLabel[regSuppressed] || regSuppressed : '-';
      document.getElementById('regFavored').textContent =
        regEnabled && regFavored ? regSideLabel[regFavored] || regFavored : '-';

      const gateT1 = regOkT1 ? '✓' : '✗';
      const gateT2 = regOkT2 ? '✓' : '✗';
      document.getElementById('regGates').innerHTML =
        regEnabled
          ? (`T1:${gateT1} T2:${gateT2}` + (regReady ? '' : ' <span style="color:#e74c3c">(HMM not ready)</span>'))
          : '-';

      document.getElementById('regGrace').textContent =
        regTier === 2
          ? (regGraceSec > 0 ? fmt(regGraceSec, 0) + 's remaining' : 'elapsed')
          : '-';

      const regCooldownEl = document.getElementById('regCooldown');
      if (!regEnabled) {
        regCooldownEl.textContent = '-';
      } else if (regCooldownSec > 0) {
        const cooldownDetail = `${fmt(regCooldownSec, 0)}s remaining`
          + (regCooldownSuppressed ? ` (${regSideLabel[regCooldownSuppressed] || regCooldownSuppressed})` : '');
        regCooldownEl.innerHTML = `<span class="status-chip warn">ACTIVE</span>${cooldownDetail}`;
      } else {
        regCooldownEl.innerHTML = '<span class="status-chip">IDLE</span>';
      }

      document.getElementById('regSuppressedSlots').textContent =
        regEnabled ? String(regSuppressedSlots) : '-';

      document.getElementById('regDwell').textContent =
        regEnabled ? fmtAgeSeconds(regDwellSec) : '-';

      document.getElementById('regLastEval').textContent =
        regEnabled && regLastEval > 0
          ? fmt((nowSec - regLastEval), 0) + 's ago'
          : '-';

      const regHints = [];
      if (!regEnabled) regHints.push('actuation:off');
      if (!regReady) regHints.push('hmm_not_ready');
      if (regTier === 2 && regGraceSec > 0) regHints.push('grace_pending');
      if (regCooldownSec > 0) regHints.push(`cooldown_active:${fmt(regCooldownSec, 0)}s`);
      if (regTier === 0 && regEnabled && regReady) regHints.push('confidence_below_threshold');
      if (regReason) regHints.push(regReason);
      document.getElementById('regHints').textContent =
        regHints.length ? `Hints: ${regHints.join(' | ')}` : '';

      const regHistory = reg.tier_history || [];
      const regTransitionsEl = document.getElementById('regTransitions');
      if (regHistory.length > 0) {
        const lines = regHistory.slice(-5).reverse().map(h => {
          const ago = fmt(nowSec - Number(h.time || 0), 0);
          return `${h.from_tier}→${h.to_tier} ${ago}s ago (${String(h.regime || '-')} ${fmt(Number(h.confidence || 0) * 100, 0)}%)`;
        });
        regTransitionsEl.textContent = 'Transitions: ' + lines.join(' | ');
      } else {
        regTransitionsEl.textContent = '';
      }

      const softCloseBtn = document.getElementById('softCloseBtn');
      const cancelStaleBtn = document.getElementById('cancelStaleBtn');
      const releaseBtn = document.getElementById('releaseBtn');
      const releaseEligibleBtn = document.getElementById('releaseEligibleBtn');
      const accumStopBtn = document.getElementById('accumStopBtn');
      softCloseBtn.style.display = stickyEnabled || !recoveryOrdersEnabled ? 'none' : '';
      cancelStaleBtn.style.display = stickyEnabled || !recoveryOrdersEnabled ? 'none' : '';
      releaseBtn.style.display = stickyEnabled ? '' : 'none';
      releaseEligibleBtn.style.display = stickyEnabled ? '' : 'none';
      const accumState = String(((s.accumulation || {}).state) || 'IDLE').toUpperCase();
      const accumCanStop = accumState === 'ARMED' || accumState === 'ACTIVE';
      accumStopBtn.style.display = accumCanStop ? '' : 'none';
      accumStopBtn.disabled = !accumCanStop;

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
      const maxTargetLayers = Math.max(1, Number(layers.max_target_layers || 20));
      const hasActiveLayers = targetLayers > 0;

      document.getElementById('layerTarget').textContent = `+${fmt(targetLayers * dogePerLayer, 3)} DOGE/order`;
      document.getElementById('layerFunded').textContent = `+${fmt(effectiveLayers * dogePerLayer, 3)} DOGE/order`;
      document.getElementById('layerStep').textContent = `${fmt(layerStep, 3)} DOGE-eq`;
      document.getElementById('layerUsdNow').textContent = Number.isFinite(usdNow) ? `$${fmt(usdNow, 4)}` : '-';
      document.getElementById('layerPropagation').textContent = `${ordersFunded}/${ordersTotal}`;
      const layerGapEl = document.getElementById('layerGap');
      if (gapLayers <= 0) {
        layerGapEl.textContent = 'fully funded';
        layerGapEl.style.color = 'var(--good)';
      } else {
        layerGapEl.textContent = `short ${fmt(gapDoge, 3)} DOGE and $${fmt(gapUsd, 4)}`;
        layerGapEl.style.color = 'var(--warn)';
      }

      const layerTelemetryRows = document.getElementById('layerTelemetryRows');
      const layerNoLayers = document.getElementById('layerNoLayers');
      if (layerTelemetryRows) layerTelemetryRows.style.display = hasActiveLayers ? '' : 'none';
      if (layerNoLayers) {
        layerNoLayers.style.display = hasActiveLayers ? 'none' : '';
        layerNoLayers.textContent = hasActiveLayers ? '' : 'No layers active';
      }

      const layerHintEl = document.getElementById('layerHint');
      if (hasActiveLayers) {
        layerHintEl.style.display = '';
        layerHintEl.textContent = gapLayers > 0
          ? 'Orders resize gradually as they recycle. No mass cancel/replace.'
          : 'Orders resize gradually as they recycle.';
      } else {
        layerHintEl.textContent = '';
        layerHintEl.style.display = 'none';
      }

      const addLayerBtn = document.getElementById('addLayerBtn');
      const removeLayerBtn = document.getElementById('removeLayerBtn');
      if (addLayerBtn) addLayerBtn.disabled = (targetLayers >= maxTargetLayers);
      if (removeLayerBtn) removeLayerBtn.disabled = (targetLayers <= 0);

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
      const stickyEnabled = isStickyModeEnabled();
      const recoveryOrdersEnabled = s.recovery_orders_enabled !== false;
      const orphansTitle = document.getElementById('orphansTitle');
      orphansTitle.textContent = recoveryOrdersEnabled ? 'Waiting Exits' : 'Waiting Exits (disabled)';

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
        const canRelease = stickyEnabled && o.role === 'exit';
        const actionHtml = canRelease ? `<button data-local-id=\"${o.local_id}\">release</button>` : '-';
        tr.innerHTML = `<td>${o.side}/${o.role}</td><td>${o.trade_id}</td><td>${o.cycle}</td><td>${fmt(o.volume, 4)}</td><td>$${fmt(o.price, 6)}</td><td class=\"mono tiny\">${o.txid || '-'}</td><td>${actionHtml}</td>`;
        if (canRelease) {
          tr.querySelector('button').onclick = () => requestRelease(slot.slot_id, {
            slot_id: slot.slot_id,
            local_id: Number(o.local_id),
          });
        }
        ob.appendChild(tr);
      }

      const rb = document.getElementById('orphansBody');
      rb.innerHTML = '';
      for (const r of slot.recovery_orders) {
        const tr = document.createElement('tr');
        const canCloseRecovery = !stickyEnabled && recoveryOrdersEnabled;
        const actionHtml = canCloseRecovery ? `<button data-rid=\"${r.recovery_id}\">close</button>` : '-';
        tr.innerHTML = `
          <td>${r.recovery_id}</td>
          <td>${r.trade_id}.${r.cycle}</td>
          <td>${r.side}</td>
          <td>${Math.round(r.age_sec)}s</td>
          <td>${fmt(r.distance_pct, 3)}</td>
          <td>$${fmt(r.price, 6)}</td>
          <td>${actionHtml}</td>
        `;
        if (canCloseRecovery) {
          tr.querySelector('button').onclick = () => requestSoftClose(slot.slot_id, r.recovery_id);
        }
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
    document.getElementById('releaseEligibleBtn').onclick = () => {
      const slots = getSlots();
      if (!slots.length) { showToast('no slots available', 'error'); return; }
      const slotId = selectedSlot || slots[0].slot_id;
      requestReleaseOldestEligible(slotId);
    };
    document.getElementById('releaseBtn').onclick = () => {
      const slots = getSlots();
      if (!slots.length) { showToast('no slots available', 'error'); return; }
      const slotId = selectedSlot || slots[0].slot_id;
      requestRelease(slotId, {slot_id: slotId});
    };
    document.getElementById('softCloseBtn').onclick = () => requestSoftCloseNext();
    document.getElementById('reconcileBtn').onclick = () => requestReconcileDrift();
    document.getElementById('cancelStaleBtn').onclick = () => requestCancelStaleRecoveries();
    document.getElementById('accumStopBtn').onclick = () => requestAccumStop();
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
    document.getElementById('aiApplyOverrideBtn').onclick = () => {
      const ai = state && state.ai_regime_advisor ? state.ai_regime_advisor : {};
      const ttl = Number(ai.suggested_ttl_sec || ai.default_ttl_sec || 1800);
      requestAiRegimeOverride(ttl);
    };
    document.getElementById('aiDismissBtn').onclick = () => requestAiRegimeDismiss();
    document.getElementById('aiRevertBtn').onclick = () => requestAiRegimeRevert();

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
