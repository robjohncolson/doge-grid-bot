"""
factory_viz.py -- Factory Lens interactive canvas view.

Served at GET /factory.
"""

FACTORY_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Factory Lens</title>
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
      --backdrop: rgba(0,0,0,0.55);
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      width: 100%;
      height: 100%;
      background: radial-gradient(circle at 15% -30%, #1f2a44 0%, #0d1117 42%, #0a0d13 100%);
      color: var(--ink);
      overflow: hidden;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    }
    #factory {
      position: fixed;
      inset: 0;
      width: 100vw;
      height: 100vh;
      cursor: grab;
    }
    #factory.dragging { cursor: grabbing; }

    #hudTop {
      position: fixed;
      top: 10px;
      left: 14px;
      z-index: 20;
      display: flex;
      align-items: center;
      gap: 10px;
      pointer-events: none;
    }
    #title {
      font-size: 13px;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    #pairBadge {
      font-size: 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 10px;
      color: var(--ink);
      background: rgba(255,255,255,.03);
    }
    #kbMode {
      font-size: 11px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 10px;
      color: var(--muted);
      background: rgba(255,255,255,.03);
      letter-spacing: .06em;
    }

    #detailPanel {
      position: fixed;
      top: 0;
      right: -420px;
      width: 400px;
      height: 100vh;
      z-index: 30;
      background: rgba(22,27,34,.98);
      border-left: 1px solid var(--line);
      transition: right 180ms ease;
      overflow-y: auto;
      padding: 14px;
    }
    #detailPanel.open { right: 0; }
    #detailPanel h2 {
      margin: 0;
      font-size: 16px;
      color: var(--ink);
    }
    #detailPanel .sub {
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
    }
    #detailPanel .closeBtn {
      border: 1px solid var(--line);
      background: #1f2733;
      color: var(--ink);
      border-radius: 8px;
      padding: 4px 8px;
      cursor: pointer;
      float: right;
    }
    #detailPanel table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      margin-top: 10px;
    }
    #detailPanel th,
    #detailPanel td {
      border-bottom: 1px solid #242c36;
      text-align: left;
      padding: 6px 4px;
      vertical-align: top;
    }
    #detailPanel th { color: var(--muted); font-weight: 600; }
    #detailPanel .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11px;
    }
    #detailPanel .pill {
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      margin-left: 6px;
    }
    #detailPanel .miniBtn {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #1f2733;
      color: var(--ink);
      padding: 2px 6px;
      cursor: pointer;
      font-size: 11px;
      line-height: 1.2;
    }
    #detailPanel .miniBtn:hover {
      border-color: var(--accent);
    }

    #notifStrip {
      position: fixed;
      left: 14px;
      right: 14px;
      bottom: 62px;
      z-index: 25;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(22,27,34,.96);
      color: var(--ink);
      padding: 7px 10px;
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      cursor: pointer;
      user-select: none;
    }
    #notifStrip:focus {
      outline: none;
      border-color: var(--accent);
    }

    #statusBar {
      position: fixed;
      left: 14px;
      right: 14px;
      bottom: 14px;
      z-index: 25;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(22,27,34,.98);
      padding: 8px 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    #statusBar .left {
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
      font-size: 12px;
      color: var(--muted);
    }
    #statusBar .value {
      color: var(--ink);
      font-variant-numeric: tabular-nums;
    }
    #modeDot {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--line);
      margin-right: 6px;
      vertical-align: middle;
    }
    #addBtn {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #1f2733;
      color: var(--ink);
      padding: 6px 10px;
      font-weight: 600;
      cursor: pointer;
    }
    #addBtn:hover { border-color: var(--accent); }

    #cmdBar {
      position: fixed;
      left: 14px;
      right: 14px;
      bottom: 62px;
      z-index: 32;
      display: flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--cmd-bg);
      padding: 8px 10px;
      transform: translateY(22px);
      opacity: 0;
      pointer-events: none;
      transition: transform 140ms ease, opacity 140ms ease;
    }
    #cmdBar.open {
      transform: translateY(0);
      opacity: 1;
      pointer-events: auto;
    }
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
      bottom: 114px;
      z-index: 33;
      display: none;
      max-width: 520px;
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
      z-index: 35;
    }
    .overlay[hidden] { display: none; }
    .modal {
      width: min(760px, calc(100vw - 30px));
      max-height: calc(100vh - 50px);
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      background: var(--panel);
    }
    #helpModal .modal pre {
      margin: 0;
      color: var(--ink);
      font-size: 12px;
      line-height: 1.45;
    }
    #confirmDialog { z-index: 36; }
    #confirmDialog .modal {
      width: min(420px, calc(100vw - 30px));
    }
    #confirmActions {
      margin-top: 12px;
      display: flex;
      justify-content: flex-end;
      gap: 8px;
    }

    #toast {
      position: fixed;
      right: 16px;
      top: 16px;
      z-index: 40;
      display: none;
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      background: rgba(22,27,34,.98);
      padding: 8px 10px;
      color: var(--ink);
      font-size: 12px;
    }
  </style>
</head>
<body>
  <canvas id="factory"></canvas>

  <div id="hudTop">
    <div id="title">Factory Lens F5</div>
    <div id="pairBadge">-</div>
    <span id="kbMode">KB NORMAL</span>
  </div>

  <aside id="detailPanel" aria-hidden="true">
    <button id="detailClose" class="closeBtn">Close</button>
    <h2 id="detailTitle">Slot</h2>
    <div id="detailSub" class="sub"></div>
    <div id="detailContent"></div>
  </aside>

  <div id="notifStrip" tabindex="0" role="button" aria-label="Cycle active symptom">Loading factory status...</div>

  <div id="statusBar">
    <div class="left">
      <span>Capacity <span id="capText" class="value">-</span></span>
      <span><span id="modeDot"></span>Band <span id="bandText" class="value">-</span></span>
      <span>Slots <span id="slotsText" class="value">-</span></span>
      <span>Profit <span id="profitText" class="value">-</span></span>
    </div>
    <button id="addBtn" title="Add slot">+Add</button>
  </div>

  <div id="cmdSuggestions" class="mono"></div>
  <div id="cmdBar">
    <span id="cmdPrefix" class="mono">:</span>
    <input id="cmdInput" class="mono" type="text" spellcheck="false" autocomplete="off" />
  </div>

  <div id="helpModal" class="overlay" hidden>
    <div class="modal mono">
<pre>+- Keybindings -------------------------+
|                                       |
|  NAVIGATION        ACTIONS            |
|  1-9   slot #jump  p    pause/resume  |
|  [/]   prev/next   +    add slot      |
|  gg    first slot  -    close next    |
|  G     last slot   .    refresh       |
|                    b    bauhaus view  |
|                    f    dashboard     |
|                    s    api/status   |
|                    :    command       |
|                    ?    this help     |
|                    Esc  close         |
|                                       |
|  COMMAND BAR                          |
|  :pause  :resume  :add  :close        |
|  :set entry N  :set profit N          |
|  :jump N (slot #)  :q (exit view)     |
|  Tab=complete  up/down=history  Esc=close |
|                                       |
+-------------------- Esc to close -----+</pre>
    </div>
  </div>

  <div id="confirmDialog" class="overlay" hidden>
    <div class="modal">
      <div id="confirmText"></div>
      <div id="confirmActions">
        <button id="confirmCancelBtn">Cancel</button>
        <button id="confirmOkBtn">Confirm</button>
      </div>
    </div>
  </div>

  <div id="toast"></div>

  <script>
    const canvas = document.getElementById('factory');
    const ctx = canvas.getContext('2d');

    const PANEL = document.getElementById('detailPanel');
    const DETAIL_TITLE = document.getElementById('detailTitle');
    const DETAIL_SUB = document.getElementById('detailSub');
    const DETAIL_CONTENT = document.getElementById('detailContent');

    const COLORS = {
      bg: '#0d1117',
      panel: '#161b22',
      line: '#30363d',
      ink: '#e6edf3',
      muted: '#8b949e',
      good: '#2ea043',
      warn: '#d29922',
      bad: '#f85149',
      accent: '#58a6ff'
    };

    const BAUHAUS_COLORS = {
      void: '#FFFFFF',
      frame: '#E8881F',
      canvas: '#F4C430',
      structure: '#000000',
      text: '#2B1B17',
      alert: '#8B0000',
      s1a: '#00CED1',
      s1b: '#9B59B6',
      s2: '#E74C3C'
    };

    const camera = {
      x: 0,
      y: 0,
      zoom: 1,
      targetX: 0,
      targetY: 0,
      targetZoom: 1
    };

    let dpr = 1;
    let viewportW = 0;
    let viewportH = 0;
    let worldW = 1200;
    let worldH = 700;

    let statusData = null;
    let layout = null;
    let hoverSlotId = null;
    let selectedSlotId = null;
    let machineRects = [];
    let recoveryDotPositions = [];
    let tooltipText = '';
    let tooltipX = 0;
    let tooltipY = 0;

    let dragging = false;
    let dragFromEmpty = false;
    let dragStartMs = 0;
    let dragDistance = 0;
    let dragLastX = 0;
    let dragLastY = 0;

    let rafPending = false;
    let lastFrameMs = 0;
    let activeSymptoms = [];
    let activeEffects = new Set();
    let slotEffects = {};
    let animQueue = [];
    let lastMachineRectBySlot = {};
    let notifIndex = 0;

    let kbMode = 'NORMAL';
    let chordKey = '';
    let chordTimer = null;
    let pendingConfirm = null;
    let lastForcedRefreshMs = 0;
    let currentSuggestions = [];
    let suggestionIndex = -1;
    let historyIndex = 0;
    const commandHistory = [];
    const COMMAND_COMPLETIONS = ['pause', 'resume', 'add', 'close', 'set entry', 'set profit', 'jump', 'q'];
    const RENDER_MODE_STORAGE_KEY = 'factory_render_mode';
    const RENDER_MODE_FACTORY = 'factory';
    const RENDER_MODE_BAUHAUS = 'bauhaus';
    const BAUHAUS_THICKNESS_WINDOW_POLLS = 60;
    const BAUHAUS_MAX_SIDE_THICKNESS_PX = 48;
    const BAUHAUS_REPRICE_EPSILON = 1e-9;
    const BAUHAUS_ORDER_SCALE_FACTOR = 25000000;
    const BAUHAUS_ORDER_MAX_OFFSET_RATIO = 0.45;

    function loadRenderMode() {
      try {
        const raw = String(localStorage.getItem(RENDER_MODE_STORAGE_KEY) || '').toLowerCase();
        return raw === RENDER_MODE_BAUHAUS ? RENDER_MODE_BAUHAUS : RENDER_MODE_FACTORY;
      } catch (_err) {
        return RENDER_MODE_FACTORY;
      }
    }

    function saveRenderMode() {
      try {
        localStorage.setItem(RENDER_MODE_STORAGE_KEY, renderMode);
      } catch (_err) {
        // Ignore storage errors (private mode / blocked storage).
      }
    }

    let renderMode = loadRenderMode();

    function setRenderMode(nextMode) {
      const normalized = String(nextMode || '').toLowerCase() === RENDER_MODE_BAUHAUS
        ? RENDER_MODE_BAUHAUS
        : RENDER_MODE_FACTORY;
      if (renderMode === normalized) return;
      renderMode = normalized;
      if (renderMode === RENDER_MODE_BAUHAUS) {
        dragging = false;
        dragFromEmpty = false;
        canvas.classList.remove('dragging');
        selectedSlotId = null;
        clearBauhausTooltipState();
        renderDetailPanel();
      } else {
        bauhausFillAnims = [];
        bauhausOrphanRepriceAnims.clear();
        clearBauhausTooltipState();
        clearBauhausRenderCaches();
      }
      saveRenderMode();
      renderNotifStrip();
      scheduleFrame();
    }

    function toggleRenderMode() {
      setRenderMode(renderMode === RENDER_MODE_FACTORY ? RENDER_MODE_BAUHAUS : RENDER_MODE_FACTORY);
      const label = renderMode === RENDER_MODE_BAUHAUS ? 'Bauhaus overlay on' : 'Factory view on';
      showToast(label, 'info');
    }

    function isBauhausMode() {
      return renderMode === RENDER_MODE_BAUHAUS;
    }

    let bauhausSellDogeHistory = [];
    let bauhausBuyDogeHistory = [];
    let bauhausLatestDogeBySide = {sell: 0, buy: 0};
    let bauhausProfitDisplayed = null;
    let bauhausProfitTarget = 0;
    let bauhausProfitFlights = [];
    let bauhausFillAnims = [];
    let bauhausOrphanRepriceAnims = new Map();
    let bauhausPinnedTooltip = null;
    let bauhausLastOrderPoints = [];
    let bauhausLastOrphanSprites = [];
    let bauhausLastPriceLineRect = null;
    let bauhausLastCounterRect = null;
    let bauhausSlotFlash = null;

    function clearBauhausTooltipState() {
      bauhausPinnedTooltip = null;
      tooltipText = '';
      hoverSlotId = null;
    }

    function clearBauhausRenderCaches() {
      bauhausLastOrderPoints = [];
      bauhausLastOrphanSprites = [];
      bauhausLastPriceLineRect = null;
      bauhausLastCounterRect = null;
      bauhausSlotFlash = null;
    }

    function diagnose(status) {
      const symptoms = [];
      const slots = Array.isArray(status && status.slots) ? status.slots : [];
      const cfh = (status && status.capacity_fill_health) || {};
      const mode = String((status && status.mode) || '');
      const priceAgeSec = Number((status && status.price_age_sec) || 0);
      const partialFillCancel = Number(cfh.partial_fill_cancel_events_1d || 0);
      const statusBand = String(cfh.status_band || '').toLowerCase();
      const totalOrphans = Number((status && status.total_orphans) || 0);
      const slotCount = Number((status && status.slot_count) || slots.length || 0);
      const s2OrphanAfterSecRaw = Number(status && status.s2_orphan_after_sec);
      const s2OrphanAfterSec = (Number.isFinite(s2OrphanAfterSecRaw) && s2OrphanAfterSecRaw > 0) ? s2OrphanAfterSecRaw : 1800;
      const nowSec = Date.now() / 1000;

      if (priceAgeSec > 60 || mode === 'HALTED') {
        symptoms.push({
          symptom_id: 'POWER_BLACKOUT',
          severity: 'crit',
          priority: 1,
          summary: mode === 'HALTED'
            ? 'Bot mode HALTED; factory power is offline'
            : 'Price feed stale for ' + Math.round(priceAgeSec) + 's',
          affected_slots: [],
          visual_effects: ['power_dead', 'red_wash', 'alarm_pulse']
        });
      }

      if (statusBand === 'stop' || partialFillCancel > 0) {
        symptoms.push({
          symptom_id: 'CIRCUIT_TRIP_RISK',
          severity: 'crit',
          priority: 2,
          summary: partialFillCancel > 0
            ? 'Partial-fill cancel canary triggered (' + partialFillCancel + ' in 1d)'
            : 'Capacity status band is STOP',
          affected_slots: [],
          visual_effects: ['circuit_spark', 'hazard_icon']
        });
      }

      if (priceAgeSec > 30 || mode === 'PAUSED') {
        symptoms.push({
          symptom_id: 'POWER_BROWNOUT',
          severity: 'crit',
          priority: 3,
          summary: mode === 'PAUSED'
            ? 'Bot mode PAUSED; throughput reduced'
            : 'Price feed aging (' + Math.round(priceAgeSec) + 's)',
          affected_slots: [],
          visual_effects: ['power_dim', 'amber_wash']
        });
      }

      for (const slot of slots) {
        if (String(slot.phase || '') !== 'S2') continue;

        const entered = Number(slot.s2_entered_at);
        if (Number.isFinite(entered) && entered > 0) {
          const elapsed = Math.max(0, nowSec - entered);
          const ratio = elapsed / s2OrphanAfterSec;
          if (ratio <= 0.5) continue;

          symptoms.push({
            symptom_id: 'BELT_JAM',
            severity: 'warn',
            priority: 4,
            summary: 'Slot #' + slot.slot_id + ' stuck in S2 for '
              + Math.round(elapsed / 60) + 'm (' + Math.round(ratio * 100) + '% of timeout)',
            affected_slots: [slot.slot_id],
            visual_effects: ['conveyor_stop', 'warning_lamp']
          });
          continue;
        }

        symptoms.push({
          symptom_id: 'BELT_JAM',
          severity: 'warn',
          priority: 4,
          summary: 'Slot #' + slot.slot_id + ' in S2 (timing unavailable)',
          affected_slots: [slot.slot_id],
          visual_effects: ['conveyor_stop', 'warning_lamp']
        });
      }

      for (const slot of slots) {
        if (!slot.long_only && !slot.short_only) continue;
        const modeTag = slot.long_only ? '[LO]' : '[SO]';
        symptoms.push({
          symptom_id: 'LANE_STARVATION',
          severity: 'warn',
          priority: 5,
          summary: 'Slot #' + slot.slot_id + ' degraded ' + modeTag,
          affected_slots: [slot.slot_id],
          visual_effects: ['machine_dark', 'input_flash_empty']
        });
      }

      if (slotCount > 0 && totalOrphans > slotCount * 2) {
        symptoms.push({
          symptom_id: 'RECOVERY_BACKLOG',
          severity: 'warn',
          priority: 6,
          summary: 'Recovery backlog: ' + totalOrphans + ' orphans across ' + slotCount + ' slots',
          affected_slots: [],
          visual_effects: ['belt_overflow']
        });
      }

      symptoms.sort((a, b) => a.priority - b.priority);

      if (symptoms.length === 0) {
        const allS0 = slots.every((slot) => String(slot.phase || '') === 'S0');
        const noDegraded = slots.every((slot) => !slot.long_only && !slot.short_only);
        if (allS0 && noDegraded && totalOrphans === 0) {
          symptoms.push({
            symptom_id: 'IDLE_NORMAL',
            severity: 'info',
            priority: 7,
            summary: 'Factory running normally',
            affected_slots: [],
            visual_effects: []
          });
        }
      }

      return symptoms;
    }

    function setActiveSymptoms(symptoms) {
      activeSymptoms = Array.isArray(symptoms) ? symptoms.slice() : [];
      if (notifIndex >= activeSymptoms.length) notifIndex = 0;
      activeEffects = new Set();
      slotEffects = {};

      for (const symptom of activeSymptoms) {
        const effects = Array.isArray(symptom.visual_effects) ? symptom.visual_effects : [];
        for (const effectName of effects) {
          activeEffects.add(effectName);
        }
        const affected = Array.isArray(symptom.affected_slots) ? symptom.affected_slots : [];
        for (const slotId of affected) {
          if (!slotEffects[slotId]) slotEffects[slotId] = new Set();
          for (const effectName of effects) {
            slotEffects[slotId].add(effectName);
          }
        }
      }
    }

    function slotHasEffect(slotId, effectName) {
      const sfx = slotEffects[slotId];
      return !!(sfx && sfx.has(effectName));
    }

    function computeDiff(prev, curr) {
      if (!prev || !curr) return [];
      const events = [];
      const prevSlots = {};
      for (const s of (prev.slots || [])) prevSlots[s.slot_id] = s;

      for (const slot of (curr.slots || [])) {
        const ps = prevSlots[slot.slot_id];
        if (!ps) {
          events.push({type: 'slot_added', slot_id: slot.slot_id});
          continue;
        }

        if (ps.phase !== slot.phase) {
          events.push({type: 'phase_change', slot_id: slot.slot_id, from: ps.phase, to: slot.phase});
        }

        const prevTxids = new Set((ps.open_orders || []).map((o) => o.txid || ('lid:' + o.local_id)));
        for (const o of (slot.open_orders || [])) {
          const key = o.txid || ('lid:' + o.local_id);
          if (!prevTxids.has(key)) {
            events.push({type: 'order_placed', slot_id: slot.slot_id, order: o});
          }
        }

        const currTxids = new Set((slot.open_orders || []).map((o) => o.txid || ('lid:' + o.local_id)));
        for (const o of (ps.open_orders || [])) {
          const key = o.txid || ('lid:' + o.local_id);
          if (!currTxids.has(key)) {
            events.push({type: 'order_gone', slot_id: slot.slot_id, order: o});
          }
        }

        const prevRecByKey = new Map();
        for (const r of (ps.recovery_orders || [])) {
          const recKey = String(slot.slot_id) + ':' + String(r && r.recovery_id);
          prevRecByKey.set(recKey, r);
        }
        for (const r of (slot.recovery_orders || [])) {
          const recKey = String(slot.slot_id) + ':' + String(r && r.recovery_id);
          const prevRec = prevRecByKey.get(recKey);
          if (!prevRec) {
            events.push({type: 'order_orphaned', slot_id: slot.slot_id, recovery: r});
            continue;
          }

          const prevPrice = Number(prevRec && prevRec.price);
          const currPrice = Number(r && r.price);
          if (
            Number.isFinite(prevPrice)
            && Number.isFinite(currPrice)
            && Math.abs(currPrice - prevPrice) > BAUHAUS_REPRICE_EPSILON
          ) {
            events.push({
              type: 'orphan_repriced',
              slot_id: slot.slot_id,
              recovery_id: r && r.recovery_id,
              recovery_key: recKey,
              old_price: prevPrice,
              new_price: currPrice,
              recovery: r
            });
          }
        }

        const prevCycleKeys = new Set((ps.recent_cycles || []).map((c) => c.trade_id + ':' + c.cycle));
        for (const c of (slot.recent_cycles || [])) {
          const key = c.trade_id + ':' + c.cycle;
          if (!prevCycleKeys.has(key)) {
            events.push({type: 'cycle_completed', slot_id: slot.slot_id, cycle: c});
          }
        }
      }

      const currSlotIds = new Set((curr.slots || []).map((s) => s.slot_id));
      for (const ps of (prev.slots || [])) {
        if (!currSlotIds.has(ps.slot_id)) {
          events.push({type: 'slot_removed', slot_id: ps.slot_id});
        }
      }

      return events;
    }

    function diff(prev, curr) {
      return computeDiff(prev, curr);
    }

    function animationDurationMs(evt) {
      if (evt.type === 'slot_added' || evt.type === 'slot_removed') return 500;
      if (evt.type === 'phase_change') return 450;
      if (evt.type === 'order_placed') return 700;
      if (evt.type === 'order_orphaned') return 800;
      if (evt.type === 'cycle_completed') return 1500;
      if (evt.type === 'order_gone') {
        return (evt.order && evt.order.role === 'exit') ? 1000 : 220;
      }
      return 1500;
    }

    function fmt(n, digits = 2) {
      if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
      return Number(n).toFixed(digits);
    }

    function showToast(text, type = 'info') {
      const el = document.getElementById('toast');
      el.textContent = String(text || 'ok');
      if (type === 'success') el.style.borderLeftColor = COLORS.good;
      else if (type === 'error') el.style.borderLeftColor = COLORS.bad;
      else el.style.borderLeftColor = COLORS.accent;
      el.style.display = 'block';
      window.clearTimeout(showToast._t);
      showToast._t = window.setTimeout(() => {
        el.style.display = 'none';
      }, 2200);
    }

    function updateKbModeBadge() {
      const badge = document.getElementById('kbMode');
      if (!badge) return;
      badge.textContent = 'KB ' + kbMode;
    }

    function clearChordBuffer() {
      chordKey = '';
      if (chordTimer !== null) {
        window.clearTimeout(chordTimer);
        chordTimer = null;
      }
    }

    function armChord(key) {
      clearChordBuffer();
      chordKey = key;
      chordTimer = window.setTimeout(() => {
        clearChordBuffer();
      }, 400);
    }

    function setKbMode(nextMode) {
      if (kbMode === nextMode) return;
      kbMode = nextMode;
      clearChordBuffer();
      updateKbModeBadge();
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
      if (!statusData || !Array.isArray(statusData.slots)) return [];
      return statusData.slots;
    }

    function findLayoutNodeBySlotId(slotId) {
      if (!layout || !Array.isArray(layout.positions)) return null;
      for (const node of layout.positions) {
        if (node.slot && node.slot.slot_id === slotId) return node;
      }
      return null;
    }

    function centerCameraOnSlot(slotId) {
      const node = findLayoutNodeBySlotId(slotId);
      if (!node) return;
      camera.targetX = node.x + node.w * 0.5;
      camera.targetY = node.y + node.h * 0.5;
      if (camera.targetZoom < 0.9) camera.targetZoom = 0.9;
    }

    function selectSlot(slotId, recenter = true) {
      selectedSlotId = slotId;
      if (isBauhausMode()) {
        bauhausSlotFlash = {
          slotId,
          startMs: performance.now(),
          durationMs: 500
        };
      } else if (recenter) {
        centerCameraOnSlot(slotId);
      }
      renderDetailPanel();
      scheduleFrame();
    }

    function jumpToSlotId(slotId) {
      const slot = getSlotById(slotId);
      if (!slot) return false;
      selectSlot(slot.slot_id, true);
      return true;
    }

    function jumpToSlotIndex(index) {
      const slots = getSlots();
      if (!slots.length) return false;
      const idx = Number(index);
      if (!Number.isInteger(idx) || idx < 0 || idx >= slots.length) return false;
      selectSlot(slots[idx].slot_id, true);
      return true;
    }

    function jumpFirstSlot() {
      jumpToSlotIndex(0);
    }

    function jumpLastSlot() {
      const slots = getSlots();
      if (!slots.length) return;
      jumpToSlotIndex(slots.length - 1);
    }

    function cycleSlot(step) {
      const slots = getSlots();
      if (!slots.length) return;
      let idx = slots.findIndex((slot) => slot.slot_id === selectedSlotId);
      if (idx < 0) idx = step > 0 ? -1 : 0;
      idx = (idx + step + slots.length) % slots.length;
      selectSlot(slots[idx].slot_id, true);
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
      const tokens = norm.split(/\s+/);
      const verb = (tokens[0] || '').toLowerCase();

      if (verb === 'pause') return {type: 'action', action: 'pause', payload: {}};
      if (verb === 'resume') return {type: 'action', action: 'resume', payload: {}};
      if (verb === 'add') return {type: 'action', action: 'add_slot', payload: {}};
      if (verb === 'q') return {type: 'navigate', href: '/'};

      if (verb === 'jump') {
        if (tokens.length < 2) return {error: 'usage: :jump <N>'};
        const slotId = parseNonNegativeInt(tokens[1]);
        if (slotId === null) return {error: 'jump target must be a non-negative integer'};
        return {type: 'jump', slotId};
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
        return {error: 'unknown set target: ' + target};
      }

      if (verb === 'close') {
        if (tokens.length === 1) return {type: 'action', action: 'soft_close_next', payload: {}};
        if (tokens.length < 3) return {error: 'usage: :close <slot> <rid>'};
        const slotId = parseNonNegativeInt(tokens[1]);
        const recoveryId = parseNonNegativeInt(tokens[2]);
        if (slotId === null || recoveryId === null) {
          return {error: 'slot and recovery id must be non-negative integers'};
        }
        return {
          type: 'action',
          action: 'soft_close',
          payload: {slot_id: slotId, recovery_id: recoveryId}
        };
      }

      return {error: 'unknown command: ' + verb};
    }

    function shouldConfirmPctChange(oldValue, newValue) {
      if (!Number.isFinite(oldValue) || oldValue === 0) return true;
      return Math.abs(newValue - oldValue) / Math.abs(oldValue) > 0.5;
    }

    async function api(path, opts = {}) {
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
        const msg = data && data.message ? data.message : (text || ('request failed (' + res.status + ')'));
        throw new Error(msg);
      }
      if (data !== null) return data;
      throw new Error('invalid server response');
    }

    async function dispatchAction(action, payload = {}) {
      try {
        const body = JSON.stringify({action, ...payload});
        const out = await api('/api/action', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body
        });
        showToast(out.message || 'ok', 'success');
        await refreshStatus();
        return true;
      } catch (err) {
        showToast((err && err.message) ? err.message : 'request failed', 'error');
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

    function requestSoftClose(slotId, recoveryId) {
      openConfirmDialog('Close recovery #' + recoveryId + ' on slot #' + slotId + '?', async () => {
        await dispatchAction('soft_close', {slot_id: slotId, recovery_id: recoveryId});
      });
    }

    function requestSetMetric(metric, value) {
      const oldValue = Number(metric === 'entry' ? statusData && statusData.entry_pct : statusData && statusData.profit_pct);
      const action = metric === 'entry' ? 'set_entry_pct' : 'set_profit_pct';
      if (shouldConfirmPctChange(oldValue, value)) {
        const oldText = Number.isFinite(oldValue) ? fmt(oldValue, 3) : '0.000';
        const newText = fmt(value, 3);
        openConfirmDialog('Change ' + metric + ' from ' + oldText + '% to ' + newText + '%?', async () => {
          await dispatchAction(action, {value});
        });
        return;
      }
      void dispatchAction(action, {value});
    }

    function pushCommandHistory(rawInput) {
      const norm = normalizeCommandInput(rawInput);
      if (!norm) return;
      commandHistory.push(':' + norm);
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
        row.textContent = ':' + currentSuggestions[i];
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
      if (parsed.type === 'noop') return;
      if (parsed.type === 'navigate') {
        if (isBauhausMode()) {
          setRenderMode(RENDER_MODE_FACTORY);
          return;
        }
        window.location.href = parsed.href;
        return;
      }

      pushCommandHistory(rawInput);

      if (parsed.type === 'jump') {
        if (!jumpToSlotId(parsed.slotId)) {
          showToast('slot #' + parsed.slotId + ' not found', 'error');
        } else {
          showToast('jumped to slot #' + parsed.slotId, 'info');
        }
        return;
      }

      if (parsed.type === 'set') {
        requestSetMetric(parsed.metric, parsed.value);
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
      await dispatchAction(parsed.action, parsed.payload);
    }

    function forceRefreshRateLimited() {
      const now = Date.now();
      if (now - lastForcedRefreshMs < 2000) return;
      lastForcedRefreshMs = now;
      void refreshStatus();
    }

    async function togglePauseResume() {
      if (statusData && statusData.mode === 'RUNNING') {
        requestPause();
      } else {
        await dispatchAction('resume');
      }
    }

    function handleNormalModeKey(event) {
      const key = event.key;
      if (/^[1-9]$/.test(key)) {
        const idx = Number(key) - 1;
        if (!jumpToSlotIndex(idx)) {
          showToast('slot index #' + key + ' not found', 'error');
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
      if (key === '+') {
        clearChordBuffer();
        void dispatchAction('add_slot');
        return true;
      }
      if (key === '-') {
        clearChordBuffer();
        requestSoftCloseNext();
        return true;
      }
      if (key === 'p') {
        clearChordBuffer();
        void togglePauseResume();
        return true;
      }
      if (key === '.') {
        clearChordBuffer();
        forceRefreshRateLimited();
        return true;
      }
      if (key === '?') {
        clearChordBuffer();
        toggleHelp();
        return true;
      }
      if (key === ':') {
        clearChordBuffer();
        openCommandBar();
        return true;
      }
      if (key === 'f') {
        clearChordBuffer();
        window.location.href = '/';
        return true;
      }
      if (key === 'b') {
        clearChordBuffer();
        toggleRenderMode();
        return true;
      }
      if (key === 's') {
        clearChordBuffer();
        window.location.href = '/api/status';
        return true;
      }
      if (key === 'Escape') {
        clearChordBuffer();
        if (isBauhausMode()) {
          if (bauhausPinnedTooltip) {
            clearBauhausPinnedTooltip();
            return true;
          }
          setRenderMode(RENDER_MODE_FACTORY);
          return true;
        }
        if (selectedSlotId !== null) {
          selectedSlotId = null;
          renderDetailPanel();
          scheduleFrame();
        } else {
          leaveToNormal();
        }
        return true;
      }
      clearChordBuffer();
      return false;
    }

    function onGlobalKeyDown(event) {
      const cmdInput = document.getElementById('cmdInput');
      if (event.ctrlKey || event.metaKey || event.altKey) return;

      if (kbMode === 'COMMAND') {
        if (event.target !== cmdInput) {
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

      const active = document.activeElement;
      if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) {
        return;
      }

      if (handleNormalModeKey(event)) {
        event.preventDefault();
      }
    }

    function handleKey(event) {
      onGlobalKeyDown(event);
    }

    function roundRect(x, y, w, h, r) {
      const rr = Math.min(r, w * 0.5, h * 0.5);
      ctx.beginPath();
      ctx.moveTo(x + rr, y);
      ctx.arcTo(x + w, y, x + w, y + h, rr);
      ctx.arcTo(x + w, y + h, x, y + h, rr);
      ctx.arcTo(x, y + h, x, y, rr);
      ctx.arcTo(x, y, x + w, y, rr);
      ctx.closePath();
    }

    function drawDiamond(x, y, size, color) {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(x, y - size);
      ctx.lineTo(x + size, y);
      ctx.lineTo(x, y + size);
      ctx.lineTo(x - size, y);
      ctx.closePath();
      ctx.fill();
    }

    function clamp(v, lo, hi) {
      return Math.max(lo, Math.min(hi, v));
    }

    function pointInBox(x, y, box) {
      if (!box) return false;
      return x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h;
    }

    function easeOutBack(t) {
      const c1 = 1.70158;
      const c3 = c1 + 1;
      const p = clamp(t, 0, 1) - 1;
      return 1 + c3 * p * p * p + c1 * p * p;
    }

    function hitTestRecoveryDot(worldX, worldY) {
      for (let i = recoveryDotPositions.length - 1; i >= 0; i -= 1) {
        const dot = recoveryDotPositions[i];
        const dx = worldX - dot.x;
        const dy = worldY - dot.y;
        if (dx * dx + dy * dy <= 36) return dot;
      }
      return null;
    }

    function bauhausCycleCount(status) {
      if (!status) return 0;
      const topLevel = Number(status.total_round_trips);
      if (Number.isFinite(topLevel) && topLevel >= 0) return Math.round(topLevel);
      const slots = Array.isArray(status.slots) ? status.slots : [];
      let sumTrips = 0;
      let foundTrips = false;
      for (const slot of slots) {
        const v = Number(slot && slot.total_round_trips);
        if (Number.isFinite(v) && v >= 0) {
          sumTrips += v;
          foundTrips = true;
        }
      }
      if (foundTrips) return Math.round(sumTrips);
      let recent = 0;
      for (const slot of slots) {
        recent += Array.isArray(slot && slot.recent_cycles) ? slot.recent_cycles.length : 0;
      }
      return recent;
    }

    function getBauhausSlotById(slotId) {
      if (!statusData || !Array.isArray(statusData.slots)) return null;
      for (const slot of statusData.slots) {
        if (slot && slot.slot_id === slotId) return slot;
      }
      return null;
    }

    function resolveBauhausHoverTarget(clientX, clientY) {
      if (!isBauhausMode() || !statusData) return null;
      const px = clientX;
      const py = clientY;

      for (let i = bauhausLastOrderPoints.length - 1; i >= 0; i -= 1) {
        const p = bauhausLastOrderPoints[i];
        const dx = px - p.x;
        const dy = py - p.y;
        if (Math.abs(dx) > 5 || Math.abs(dy) > 5) continue;

        const order = p.order || {};
        const slotId = p.node && p.node.slot ? p.node.slot.slot_id : null;
        const market = Number(statusData.price) > 0
          ? Number(statusData.price)
          : Number(p.node && p.node.slot && p.node.slot.market_price);
        const price = Number(order.price);
        const pct = (Number.isFinite(price) && Number.isFinite(market) && market > 0)
          ? (Math.abs(price - market) / market) * 100
          : null;
        const text = 'Order ' + String(order.side || p.side || '-')
          + '/' + String(order.role || p.role || '-')
          + ' | $' + fmt(price, 6)
          + ' | vol ' + fmt(order.volume, 3)
          + ' | Δ ' + (pct === null ? '-' : fmt(pct, 3) + '%');
        return {
          type: 'order',
          key: 'order:' + bauhausOrderEventKey(slotId, order),
          slot_id: slotId,
          text
        };
      }

      for (let i = bauhausLastOrphanSprites.length - 1; i >= 0; i -= 1) {
        const item = bauhausLastOrphanSprites[i];
        const dx = px - item.x;
        const dy = py - item.y;
        if ((dx * dx + dy * dy) > 49) continue;
        const rec = item.recovery || {};
        const pct = Number(item.pctDistance);
        const text = 'Orphan #' + String(rec.recovery_id)
          + ' | ' + String(rec.side || '-')
          + ' | $' + fmt(rec.price, 6)
          + ' | vol ' + fmt(rec.volume, 3)
          + ' | age ' + Math.round(Number(rec.age_sec || 0)) + 's'
          + ' | Δ ' + (Number.isFinite(pct) ? fmt(pct * 100, 3) + '%' : '-');
        return {
          type: 'orphan',
          key: 'orphan:' + String(item.key),
          slot_id: item.slot_id,
          text
        };
      }

      for (let i = machineRects.length - 1; i >= 0; i -= 1) {
        const rect = machineRects[i];
        if (!pointInBox(px, py, rect)) continue;
        const slot = getBauhausSlotById(rect.slot_id);
        if (!slot) continue;
        const flags = [];
        if (slot.long_only) flags.push('LO');
        if (slot.short_only) flags.push('SO');
        const text = 'Slot #' + slot.slot_id
          + ' | ' + String(slot.phase || 'S0')
          + ' | $' + fmt(slot.total_profit, 4)
          + ' | ' + (Array.isArray(slot.open_orders) ? slot.open_orders.length : 0) + ' orders'
          + (flags.length ? ' | ' + flags.join('/') : '');
        return {
          type: 'slot',
          key: 'slot:' + String(slot.slot_id),
          slot_id: slot.slot_id,
          text
        };
      }

      if (pointInBox(px, py, bauhausLastCounterRect)) {
        const text = 'Profit $' + fmt(statusData.total_profit, 4)
          + ' | cycles ' + String(bauhausCycleCount(statusData));
        return {
          type: 'profit_counter',
          key: 'counter',
          slot_id: null,
          text
        };
      }

      if (pointInBox(px, py, bauhausLastPriceLineRect)) {
        const sides = aggregateBauhausCapitalDoge(statusData);
        const text = 'Price $' + fmt(statusData.price, 6)
          + ' | age ' + Math.round(Number(statusData.price_age_sec || 0)) + 's'
          + ' | DOGE sell ' + fmt(sides.sell, 3)
          + ' / buy ' + fmt(sides.buy, 3);
        return {
          type: 'price_line',
          key: 'price-line',
          slot_id: null,
          text
        };
      }

      return null;
    }

    function clearBauhausPinnedTooltip() {
      if (!bauhausPinnedTooltip && !tooltipText && hoverSlotId === null) return;
      clearBauhausTooltipState();
      scheduleFrame();
    }

    function drawTooltip() {
      if (!tooltipText) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      const padX = 8;
      const padY = 6;
      const w = Math.ceil(ctx.measureText(tooltipText).width) + padX * 2;
      const h = 24;

      let x = tooltipX + 14;
      let y = tooltipY + 16;
      if (x + w + 8 > viewportW) x = tooltipX - w - 14;
      if (y + h + 8 > viewportH) y = tooltipY - h - 14;
      x = clamp(x, 8, viewportW - w - 8);
      y = clamp(y, 8, viewportH - h - 8);

      const bauhaus = isBauhausMode();
      ctx.fillStyle = bauhaus ? 'rgba(0,0,0,0.94)' : 'rgba(13,17,23,0.95)';
      roundRect(x, y, w, h, 7);
      ctx.fill();
      ctx.strokeStyle = bauhaus ? 'rgba(255,255,255,0.35)' : 'rgba(88,166,255,0.45)';
      ctx.lineWidth = 1;
      roundRect(x, y, w, h, 7);
      ctx.stroke();

      ctx.fillStyle = bauhaus ? '#FFFFFF' : COLORS.ink;
      ctx.fillText(tooltipText, x + padX, y + h - padY - 2);
    }

    function updateCanvasSize() {
      viewportW = window.innerWidth;
      viewportH = window.innerHeight;
      dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(viewportW * dpr));
      canvas.height = Math.max(1, Math.floor(viewportH * dpr));
      canvas.style.width = viewportW + 'px';
      canvas.style.height = viewportH + 'px';
      scheduleFrame();
    }

    function worldToScreen(x, y) {
      return {
        x: (x - camera.x) * camera.zoom + viewportW * 0.5,
        y: (y - camera.y) * camera.zoom + viewportH * 0.5
      };
    }

    function screenToWorld(x, y) {
      return {
        x: (x - viewportW * 0.5) / camera.zoom + camera.x,
        y: (y - viewportH * 0.5) / camera.zoom + camera.y
      };
    }

    function getSlotById(slotId) {
      if (!statusData || !Array.isArray(statusData.slots)) return null;
      return statusData.slots.find((s) => s.slot_id === slotId) || null;
    }

    function computeLayout(status) {
      const slots = Array.isArray(status && status.slots) ? status.slots : [];
      const compact = slots.length >= 20;
      const machineW = compact ? 90 : 140;
      const machineH = compact ? 65 : 100;
      const gap = compact ? 20 : 40;
      const maxPerRow = compact ? 10 : 6;
      const rowGap = compact ? 95 : 140;

      const cols = Math.max(1, Math.min(maxPerRow, slots.length || 1));
      const rows = Math.max(1, Math.ceil((slots.length || 1) / maxPerRow));

      const leftPad = compact ? 180 : 230;
      const topPad = compact ? 116 : 140;

      const machineBandW = cols * machineW + (cols - 1) * gap;
      worldW = Math.max(compact ? 900 : 800, leftPad + machineBandW + (compact ? 250 : 330));
      worldH = Math.max(compact ? 520 : 500, topPad + rows * machineH + (rows - 1) * rowGap + (compact ? 200 : 230));

      const positions = [];
      for (let i = 0; i < slots.length; i += 1) {
        const row = Math.floor(i / maxPerRow);
        const col = i % maxPerRow;
        const x = leftPad + col * (machineW + gap);
        const y = topPad + row * (machineH + rowGap);
        positions.push({
          slot: slots[i],
          x,
          y,
          w: machineW,
          h: machineH
        });
      }

      const firstY = topPad;
      const secondY = topPad + rowGap;

      return {
        powerY: 46,
        powerX1: 30,
        powerX2: worldW - 30,
        usdChest: {x: 44, y: firstY, w: 112, h: 82},
        dogeChest: {x: 44, y: secondY, w: 112, h: 82},
        outputChest: {x: worldW - 178, y: firstY + (compact ? 26 : 40), w: 120, h: 92},
        recycle: {x: compact ? 180 : 220, y: worldH - 110, w: Math.max(compact ? 420 : 360, worldW - (compact ? 360 : 440)), h: 36},
        positions,
        compact,
        machineW,
        machineH
      };
    }

    function bauhausPhaseColor(phase) {
      if (phase === 'S1a') return BAUHAUS_COLORS.s1a;
      if (phase === 'S1b') return BAUHAUS_COLORS.s1b;
      if (phase === 'S2') return BAUHAUS_COLORS.s2;
      return null;
    }

    function getBauhausVisualState(status) {
      const mode = String(status && status.mode || '').toUpperCase();
      const halted = mode === 'HALTED' || activeEffects.has('power_dead');
      const paused = mode === 'PAUSED';
      const brownout = paused || activeEffects.has('power_dim') || activeEffects.has('amber_wash');
      const motionFactor = halted ? 0 : (paused ? 0.25 : (brownout ? 0.55 : 1));
      const slotAlpha = halted ? 0.55 : (brownout ? 0.82 : 1);
      const orderAlpha = halted ? 0.42 : (brownout ? 0.72 : 1);
      return {mode, halted, paused, brownout, motionFactor, slotAlpha, orderAlpha};
    }

    function computeBauhausLayout(status) {
      const slots = Array.isArray(status && status.slots) ? status.slots : [];
      const pad = 18;
      const frameStroke = clamp(Math.round(Math.min(viewportW, viewportH) * 0.012), 8, 12);
      const outer = {
        x: pad,
        y: pad,
        w: Math.max(140, viewportW - pad * 2),
        h: Math.max(100, viewportH - pad * 2)
      };
      const inner = {
        x: outer.x + frameStroke,
        y: outer.y + frameStroke,
        w: Math.max(100, outer.w - frameStroke * 2),
        h: Math.max(80, outer.h - frameStroke * 2)
      };
      const priceY = inner.y + inner.h * 0.5;

      const sidePad = Math.max(24, Math.round(inner.w * 0.04));
      const usableW = Math.max(120, inner.w - sidePad * 2);
      let slotW = slots.length <= 10 ? 80 : (slots.length <= 20 ? 50 : 40);
      let slotH = slots.length <= 10 ? 40 : (slots.length <= 20 ? 28 : 24);
      if (slots.length > 0) {
        const maxByWidth = Math.floor((usableW - 8 * Math.max(0, slots.length - 1)) / slots.length);
        slotW = Math.min(slotW, maxByWidth);
      }
      slotW = clamp(slotW, 28, 90);
      slotH = clamp(slotH, 22, 48);

      const totalSlotW = slots.length * slotW;
      let gap = 0;
      if (slots.length > 1) {
        gap = Math.floor((usableW - totalSlotW) / (slots.length - 1));
        if (!Number.isFinite(gap)) gap = 0;
        gap = Math.max(2, gap);
      }
      const usedW = totalSlotW + gap * Math.max(0, slots.length - 1);
      const startX = inner.x + sidePad + Math.max(0, Math.floor((usableW - usedW) * 0.5));
      const slotY = priceY - slotH * 0.5;

      const positions = [];
      for (let i = 0; i < slots.length; i += 1) {
        positions.push({
          slot: slots[i],
          x: startX + i * (slotW + gap),
          y: slotY,
          w: slotW,
          h: slotH
        });
      }

      return {
        outer,
        inner,
        frameStroke,
        frameRadius: 16,
        innerRadius: 12,
        priceY,
        positions
      };
    }

    function aggregateBauhausCapitalDoge(status) {
      const slots = Array.isArray(status && status.slots) ? status.slots : [];
      let marketPrice = Number(status && status.price);
      if (!Number.isFinite(marketPrice) || marketPrice <= 0) marketPrice = 0;

      let sellDoge = 0;
      let buyDogeEq = 0;

      for (const slot of slots) {
        const slotMarket = Number(slot && slot.market_price);
        const refPrice = marketPrice > 0 ? marketPrice : (Number.isFinite(slotMarket) && slotMarket > 0 ? slotMarket : 0);
        const openOrders = Array.isArray(slot && slot.open_orders) ? slot.open_orders : [];
        const recOrders = Array.isArray(slot && slot.recovery_orders) ? slot.recovery_orders : [];

        for (const order of openOrders.concat(recOrders)) {
          const side = String(order && order.side || '').toLowerCase();
          const volume = Number(order && order.volume);
          const price = Number(order && order.price);
          if (!Number.isFinite(volume) || volume <= 0) continue;

          if (side === 'sell') {
            sellDoge += volume;
            continue;
          }

          if (side === 'buy') {
            if (refPrice > 0 && Number.isFinite(price) && price > 0) {
              buyDogeEq += (volume * price) / refPrice;
            } else {
              // Fallback if reference price is unavailable; keeps line responsive.
              buyDogeEq += volume;
            }
          }
        }
      }

      return {
        sell: Math.max(0, sellDoge),
        buy: Math.max(0, buyDogeEq)
      };
    }

    function updateBauhausThicknessWindow(status) {
      const sides = aggregateBauhausCapitalDoge(status);
      bauhausLatestDogeBySide = sides;

      bauhausSellDogeHistory.push(sides.sell);
      bauhausBuyDogeHistory.push(sides.buy);

      if (bauhausSellDogeHistory.length > BAUHAUS_THICKNESS_WINDOW_POLLS) {
        bauhausSellDogeHistory = bauhausSellDogeHistory.slice(-BAUHAUS_THICKNESS_WINDOW_POLLS);
      }
      if (bauhausBuyDogeHistory.length > BAUHAUS_THICKNESS_WINDOW_POLLS) {
        bauhausBuyDogeHistory = bauhausBuyDogeHistory.slice(-BAUHAUS_THICKNESS_WINDOW_POLLS);
      }
    }

    function rollingMax(values) {
      if (!Array.isArray(values) || !values.length) return 0;
      let maxV = 0;
      for (const v of values) {
        const n = Number(v);
        if (Number.isFinite(n) && n > maxV) maxV = n;
      }
      return maxV;
    }

    function computeBauhausSideThicknesses() {
      const sellMax = rollingMax(bauhausSellDogeHistory);
      const buyMax = rollingMax(bauhausBuyDogeHistory);
      const sellNow = Number(bauhausLatestDogeBySide.sell || 0);
      const buyNow = Number(bauhausLatestDogeBySide.buy || 0);

      const sellPx = sellMax > 0
        ? clamp(Math.round((sellNow / sellMax) * BAUHAUS_MAX_SIDE_THICKNESS_PX), 1, BAUHAUS_MAX_SIDE_THICKNESS_PX)
        : 1;
      const buyPx = buyMax > 0
        ? clamp(Math.round((buyNow / buyMax) * BAUHAUS_MAX_SIDE_THICKNESS_PX), 1, BAUHAUS_MAX_SIDE_THICKNESS_PX)
        : 1;

      return {sellPx, buyPx};
    }

    function getBauhausCounterRect(layoutView) {
      const h = clamp(Math.round(layoutView.inner.h * 0.09), 32, 46);
      const w = clamp(Math.round(layoutView.inner.w * 0.23), 150, 220);
      const margin = 16;
      return {
        x: layoutView.inner.x + layoutView.inner.w - w - margin,
        y: layoutView.inner.y + margin,
        w,
        h
      };
    }

    function formatBauhausProfitText(value) {
      const n = Number(value || 0);
      const safe = Number.isFinite(n) ? n : 0;
      const sign = safe < 0 ? '-' : '';
      return sign + Math.abs(safe).toFixed(2);
    }

    function sevenSegActiveSegments(ch) {
      const map = {
        '0': 'abcedf',
        '1': 'bc',
        '2': 'abged',
        '3': 'abgcd',
        '4': 'fgbc',
        '5': 'afgcd',
        '6': 'afgecd',
        '7': 'abc',
        '8': 'abcdefg',
        '9': 'abcfgd',
        '-': 'g',
        'Ð': 'abcedfg'
      };
      return map[ch] || '';
    }

    function drawSevenSegGlyph(ch, x, y, w, h, onColor, offColor) {
      const t = Math.max(2, Math.round(Math.min(w, h) * 0.16));
      const half = h * 0.5;
      const on = sevenSegActiveSegments(ch);

      function seg(name, rx, ry, rw, rh) {
        ctx.fillStyle = on.includes(name) ? onColor : offColor;
        ctx.fillRect(Math.round(rx), Math.round(ry), Math.max(1, Math.round(rw)), Math.max(1, Math.round(rh)));
      }

      if (ch === '.') {
        ctx.fillStyle = onColor;
        ctx.fillRect(Math.round(x + w - t), Math.round(y + h - t), t, t);
        return;
      }

      seg('a', x + t, y, w - t * 2, t);
      seg('d', x + t, y + h - t, w - t * 2, t);
      seg('g', x + t, y + half - t * 0.5, w - t * 2, t);
      seg('f', x, y + t, t, half - t);
      seg('b', x + w - t, y + t, t, half - t);
      seg('e', x, y + half, t, half - t);
      seg('c', x + w - t, y + half, t, half - t);
    }

    function drawBauhausProfitCounter(layoutView) {
      const rect = getBauhausCounterRect(layoutView);
      const shown = formatBauhausProfitText(bauhausProfitDisplayed === null ? bauhausProfitTarget : bauhausProfitDisplayed);
      const text = 'Ð ' + shown;
      const padX = 8;
      const glyphH = rect.h - 10;
      const digitW = Math.round(glyphH * 0.62);
      const prefixW = Math.round(glyphH * 0.68);
      const dotW = Math.max(4, Math.round(digitW * 0.35));
      const gap = 3;

      ctx.fillStyle = BAUHAUS_COLORS.structure;
      roundRect(rect.x, rect.y, rect.w, rect.h, 6);
      ctx.fill();

      let cx = rect.x + padX;
      const onColor = '#f5f5f5';
      const offColor = 'rgba(255,255,255,0.15)';
      for (let i = 0; i < text.length; i += 1) {
        const ch = text[i];
        if (ch === ' ') {
          cx += Math.max(4, gap);
          continue;
        }
        const w = ch === 'Ð' ? prefixW : (ch === '.' ? dotW : digitW);
        drawSevenSegGlyph(ch, cx, rect.y + 5, w, glyphH, onColor, offColor);
        cx += w + gap;
      }
      return rect;
    }

    function queueBauhausProfitFlights(events, status, startMs) {
      const list = Array.isArray(events) ? events : [];
      if (!list.length) return;

      const bauhausLayout = computeBauhausLayout(status);
      const counter = getBauhausCounterRect(bauhausLayout);
      const toX = counter.x + 10;
      const toY = counter.y + counter.h * 0.5;

      const nodeBySlot = {};
      for (const node of bauhausLayout.positions) {
        if (node && node.slot) nodeBySlot[node.slot.slot_id] = node;
      }

      for (const evt of list) {
        if (evt.type !== 'cycle_completed') continue;
        const cycle = evt.cycle || {};
        const delta = Number(cycle.net_profit);
        if (!Number.isFinite(delta)) continue;

        const node = nodeBySlot[evt.slot_id];
        const fromX = node ? node.x + node.w * 0.5 : (bauhausLayout.inner.x + bauhausLayout.inner.w * 0.5);
        const fromY = node ? node.y + node.h * 0.5 : bauhausLayout.priceY;
        const key = String(evt.slot_id) + ':' + String(cycle.trade_id || '') + ':' + String(cycle.cycle || '');

        bauhausProfitFlights.push({
          id: key + ':' + String(startMs),
          slot_id: evt.slot_id,
          fromX,
          fromY,
          toX,
          toY,
          startMs,
          durationMs: 760,
          delta,
          applied: false,
          seed: hashString32(key)
        });
      }

      if (bauhausProfitFlights.length > 80) {
        bauhausProfitFlights = bauhausProfitFlights.slice(-80);
      }
    }

    function drawBauhausProfitFlights(nowMs, status) {
      if (!bauhausProfitFlights.length) return;
      const keep = [];
      const visualState = getBauhausVisualState(status);
      const motionFactor = Math.max(0.05, visualState.motionFactor);

      for (const flight of bauhausProfitFlights) {
        const progress = clamp(((nowMs - flight.startMs) * motionFactor) / flight.durationMs, 0, 1);
        const ease = progress * progress * (3 - 2 * progress);
        const x = flight.fromX + (flight.toX - flight.fromX) * ease;
        const y = flight.fromY + (flight.toY - flight.fromY) * ease;

        const alpha = 1 - progress * 0.65;
        const rgb = flight.delta >= 0 ? '0,0,0' : '80,0,0';
        const count = 8;
        for (let i = 0; i < count; i += 1) {
          const u = seededUnit(flight.seed, i + 30);
          const a = (Math.PI * 2 * i) / count + progress * 6.2;
          const r = 1 + (1 - progress) * (4 + u * 2);
          const px = x + Math.cos(a) * r;
          const py = y + Math.sin(a) * r;
          ctx.fillStyle = 'rgba(' + rgb + ',' + alpha.toFixed(3) + ')';
          ctx.fillRect(Math.round(px), Math.round(py), 2, 2);
        }

        if (progress >= 1 && !flight.applied) {
          flight.applied = true;
          if (bauhausProfitDisplayed === null || !Number.isFinite(bauhausProfitDisplayed)) {
            bauhausProfitDisplayed = 0;
          }
          bauhausProfitDisplayed += flight.delta;
        }

        if (progress < 1) keep.push(flight);
      }

      bauhausProfitFlights = keep;

      if (!bauhausProfitFlights.length && bauhausProfitDisplayed !== null) {
        const diffToTarget = Math.abs(bauhausProfitDisplayed - bauhausProfitTarget);
        if (diffToTarget > 1e-6) {
          bauhausProfitDisplayed = bauhausProfitTarget;
        }
      }
    }

    function drawBauhausFrame(layoutView) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, viewportW, viewportH);

      ctx.fillStyle = BAUHAUS_COLORS.void;
      ctx.fillRect(0, 0, viewportW, viewportH);

      ctx.fillStyle = BAUHAUS_COLORS.canvas;
      roundRect(layoutView.inner.x, layoutView.inner.y, layoutView.inner.w, layoutView.inner.h, layoutView.innerRadius);
      ctx.fill();

      ctx.strokeStyle = BAUHAUS_COLORS.frame;
      ctx.lineWidth = layoutView.frameStroke;
      roundRect(layoutView.outer.x, layoutView.outer.y, layoutView.outer.w, layoutView.outer.h, layoutView.frameRadius);
      ctx.stroke();
    }

    function drawBauhausPriceLine(layoutView, status, nowMs) {
      const inner = layoutView.inner;
      const x = inner.x + 14;
      const w = Math.max(12, inner.w - 28);
      const age = Number(status && status.price_age_sec || 0);
      const visualState = getBauhausVisualState(status);
      const mode = visualState.mode;
      const stale = age > 60;
      const dead = stale || mode === 'HALTED' || visualState.halted;

      const t = computeBauhausSideThicknesses();
      const yTop = Math.round(layoutView.priceY - t.sellPx);
      const h = Math.max(2, t.sellPx + t.buyPx);
      const rect = {
        x: Math.round(x),
        y: yTop,
        w: Math.round(w),
        h
      };

      let lineLightness = 0;
      if (dead) lineLightness = 36;
      else if (age > 10) lineLightness = Math.round(((Math.min(age, 60) - 10) / 50) * 24);
      ctx.fillStyle = 'hsl(0 0% ' + lineLightness + '%)';
      ctx.fillRect(Math.round(x), yTop, Math.round(w), h);

      if (dead) return rect;

      let speedPxPerMs = 0.16;
      if (age >= 10 && age < 60) speedPxPerMs = 0.06;
      speedPxPerMs *= visualState.motionFactor;
      if (speedPxPerMs <= 0) return rect;

      const particleCount = clamp(Math.floor(w / 26), 8, 52);
      const lanes = Math.max(1, h);
      const sparkleColor = age < 10 ? 'rgba(255,255,255,0.95)' : 'rgba(235,235,235,0.72)';

      ctx.save();
      ctx.beginPath();
      ctx.rect(Math.round(x), yTop, Math.round(w), h);
      ctx.clip();

      ctx.fillStyle = sparkleColor;
      for (let i = 0; i < particleCount; i += 1) {
        const phaseOffset = i * (w / particleCount);
        const sparkX = x + w - ((nowMs * speedPxPerMs + phaseOffset) % w);
        const lane = (i * 3) % lanes;
        const sparkY = yTop + lane;
        ctx.fillRect(Math.round(sparkX), Math.round(sparkY), 1, 1);
      }

      ctx.restore();
      return rect;
    }

    function drawBauhausSlots(layoutView, status, nowMs) {
      const positions = Array.isArray(layoutView.positions) ? layoutView.positions : [];
      const visualState = getBauhausVisualState(status);
      const pulse = 0.5 + 0.5 * Math.sin(nowMs / 210);
      machineRects = [];
      for (const node of positions) {
        const slot = node.slot || {};
        const phase = String(slot.phase || 'S0');
        const fill = bauhausPhaseColor(phase);
        const isJammed = slotHasEffect(slot.slot_id, 'conveyor_stop');
        const isStarved = slotHasEffect(slot.slot_id, 'machine_dark') || !!slot.long_only || !!slot.short_only;
        const fillAlpha = clamp(
          visualState.slotAlpha
            * (isJammed && phase === 'S2' ? (0.62 + pulse * 0.38) : 1)
            * (isStarved ? 0.88 : 1),
          0.2,
          1
        );

        if (fill) {
          ctx.save();
          ctx.globalAlpha = fillAlpha;
          ctx.fillStyle = fill;
          roundRect(node.x + 4, node.y + 4, Math.max(8, node.w - 8), Math.max(8, node.h - 8), 7);
          ctx.fill();
          ctx.restore();
        }

        ctx.strokeStyle = BAUHAUS_COLORS.structure;
        ctx.lineWidth = isJammed ? (1.8 + pulse * 1.2) : 2;
        ctx.globalAlpha = clamp(visualState.slotAlpha * (isStarved ? 0.92 : 1), 0.35, 1);
        roundRect(node.x, node.y, node.w, node.h, 8);
        ctx.stroke();
        ctx.globalAlpha = 1;

        const fontPx = node.h >= 34 ? 12 : 10;
        ctx.fillStyle = BAUHAUS_COLORS.structure;
        ctx.globalAlpha = clamp(visualState.slotAlpha * 0.95, 0.35, 1);
        ctx.font = '700 ' + fontPx + 'px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(phase, node.x + node.w * 0.5, node.y + node.h * 0.5 + 0.5);
        ctx.globalAlpha = 1;

        if (slot.long_only || slot.short_only) {
          const label = slot.long_only ? '[LO]' : '[SO]';
          const triCx = node.x + node.w - 9;
          const triCy = node.y + 9;
          const triAlpha = clamp(0.55 + (isStarved ? pulse * 0.35 : 0), 0.45, 0.9);
          ctx.fillStyle = 'rgba(0,0,0,' + triAlpha.toFixed(3) + ')';
          ctx.beginPath();
          ctx.moveTo(triCx, triCy - 5);
          ctx.lineTo(triCx - 5, triCy + 4);
          ctx.lineTo(triCx + 5, triCy + 4);
          ctx.closePath();
          ctx.fill();

          ctx.fillStyle = BAUHAUS_COLORS.canvas;
          ctx.font = '700 7px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
          ctx.fillText('!', triCx, triCy + 1);

          ctx.fillStyle = BAUHAUS_COLORS.structure;
          ctx.font = '700 8px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
          ctx.textAlign = 'right';
          ctx.textBaseline = 'alphabetic';
          ctx.fillText(label, node.x + node.w - 2, node.y + node.h - 2);
        }

        if (bauhausSlotFlash && bauhausSlotFlash.slotId === slot.slot_id) {
          const t = clamp((nowMs - bauhausSlotFlash.startMs) / bauhausSlotFlash.durationMs, 0, 1);
          const alpha = clamp(1 - t, 0, 1);
          if (alpha > 0.01) {
            const grow = 2 + t * 6;
            ctx.strokeStyle = 'rgba(255,255,255,' + (0.95 * alpha).toFixed(3) + ')';
            ctx.lineWidth = 2 + (1 - t) * 1.2;
            roundRect(node.x - grow, node.y - grow, node.w + grow * 2, node.h + grow * 2, 10 + grow);
            ctx.stroke();
          }
        }

        ctx.textAlign = 'left';
        ctx.textBaseline = 'alphabetic';

        machineRects.push({
          slot_id: slot.slot_id,
          x: node.x,
          y: node.y,
          w: node.w,
          h: node.h
        });
      }
      if (bauhausSlotFlash && (nowMs - bauhausSlotFlash.startMs) >= bauhausSlotFlash.durationMs) {
        bauhausSlotFlash = null;
      }
    }

    function computeBauhausOrderPoints(layoutView, status) {
      const positions = Array.isArray(layoutView.positions) ? layoutView.positions : [];
      const points = [];
      const defaultMarket = Number(status && status.price);
      const maxOffset = layoutView.inner.h * 0.5 * BAUHAUS_ORDER_MAX_OFFSET_RATIO;
      const scaleFactor = BAUHAUS_ORDER_SCALE_FACTOR; // pct^2 distance emphasis (v2.2.1 section 3.2)

      for (const node of positions) {
        const slot = node.slot || {};
        const rawOrders = Array.isArray(slot.open_orders) ? slot.open_orders : [];
        if (!rawOrders.length) continue;

        const slotMarket = Number(slot.market_price || 0);
        const marketPrice = (Number.isFinite(defaultMarket) && defaultMarket > 0) ? defaultMarket : slotMarket;
        if (!Number.isFinite(marketPrice) || marketPrice <= 0) continue;

        const perSide = {sell: [], buy: []};
        for (const order of rawOrders) {
          const side = String(order.side || '').toLowerCase();
          if (side !== 'buy' && side !== 'sell') continue;

          const orderPrice = Number(order.price);
          if (!Number.isFinite(orderPrice) || orderPrice <= 0) continue;

          const pctDistance = Math.abs(orderPrice - marketPrice) / marketPrice;
          const rawOffset = pctDistance * pctDistance * scaleFactor;
          const absOffset = Math.min(maxOffset, rawOffset);
          const direction = side === 'sell' ? -1 : 1; // sell above, buy below

          perSide[side].push({
            order,
            side,
            role: String(order.role || '').toLowerCase(),
            rawOffset,
            absOffset,
            pctDistance,
            clamped: rawOffset > maxOffset + 1e-9,
            direction,
            y: layoutView.priceY + direction * absOffset
          });
        }

        for (const sideName of ['sell', 'buy']) {
          const items = perSide[sideName];
          if (!items.length) continue;

          items.sort((a, b) => a.absOffset - b.absOffset);
          let prevY = null;
          const minY = layoutView.priceY - maxOffset;
          const maxY = layoutView.priceY + maxOffset;
          for (const item of items) {
            if (prevY !== null && Math.abs(item.y - prevY) < 8) {
              item.y = prevY + item.direction * 8;
            }
            item.y = clamp(item.y, minY, maxY);
            prevY = item.y;
          }

          const spacing = 10;
          const cx = node.x + node.w * 0.5;
          const totalW = spacing * Math.max(0, items.length - 1);
          for (let i = 0; i < items.length; i += 1) {
            const item = items[i];
            item.x = cx - totalW * 0.5 + i * spacing;
            item.node = node;
            points.push(item);
          }
        }
      }

      return points;
    }

    function drawBauhausOrders(layoutView, status, nowMs) {
      const points = computeBauhausOrderPoints(layoutView, status);
      const visualState = getBauhausVisualState(status);
      const jamPulse = 0.5 + 0.5 * Math.sin(nowMs / 190);

      for (const p of points) {
        const anchorX = p.node.x + p.node.w * 0.5;
        const anchorY = p.side === 'sell' ? p.node.y : (p.node.y + p.node.h);
        const distToSlot = Math.abs(p.y - anchorY);
        const slotData = p.node && p.node.slot ? p.node.slot : {};
        const starvedSell = !!slotData.long_only;
        const starvedBuy = !!slotData.short_only;
        if ((starvedSell && p.side === 'sell') || (starvedBuy && p.side === 'buy')) {
          continue;
        }
        const slotId = p.node && p.node.slot ? p.node.slot.slot_id : null;
        const isJammed = slotId !== null && slotHasEffect(slotId, 'conveyor_stop') && p.role === 'exit';
        const isStarved = slotId !== null && slotHasEffect(slotId, 'machine_dark');
        const lineAlpha = clamp(visualState.orderAlpha * (isStarved ? 0.78 : 1), 0.2, 1);

        if (distToSlot >= 5) {
          ctx.strokeStyle = isJammed ? 'rgba(120,120,120,0.95)' : '#999999';
          ctx.lineWidth = isJammed ? (1.4 + jamPulse * 0.9) : 1;
          ctx.globalAlpha = lineAlpha;
          if (p.clamped) ctx.setLineDash([2, 2]);
          else ctx.setLineDash([]);
          ctx.beginPath();
          ctx.moveTo(Math.round(anchorX) + 0.5, Math.round(anchorY) + 0.5);
          ctx.lineTo(Math.round(p.x) + 0.5, Math.round(p.y) + 0.5);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.globalAlpha = 1;
        }

        const markerAlpha = clamp(visualState.orderAlpha * (isStarved ? 0.84 : 1), 0.22, 1);
        drawBauhausOrderSquare(p.x, p.y, p.role, markerAlpha);
      }
      return points;
    }

    function bauhausOrderEventKey(slotId, order) {
      const prefix = String(slotId);
      if (order && order.txid) {
        return prefix + ':tx:' + String(order.txid);
      }
      if (order && order.local_id !== null && order.local_id !== undefined) {
        return prefix + ':lid:' + String(order.local_id);
      }
      const role = String(order && order.role || '');
      const side = String(order && order.side || '');
      const trade = String(order && order.trade_id || '');
      const cycle = String(order && order.cycle || '');
      const price = Number(order && order.price);
      const volume = Number(order && order.volume);
      const priceTxt = Number.isFinite(price) ? price.toFixed(10) : 'na';
      const volTxt = Number.isFinite(volume) ? volume.toFixed(10) : 'na';
      return prefix + ':fallback:' + [role, side, trade, cycle, priceTxt, volTxt].join(':');
    }

    function buildBauhausOrderPointIndex(statusSnapshot) {
      const out = new Map();
      if (!statusSnapshot || !Array.isArray(statusSnapshot.slots)) return out;

      const layoutView = computeBauhausLayout(statusSnapshot);
      const points = computeBauhausOrderPoints(layoutView, statusSnapshot);
      for (const p of points) {
        if (!p || !p.node || !p.node.slot || !p.order) continue;
        const slotId = p.node.slot.slot_id;
        const key = bauhausOrderEventKey(slotId, p.order);
        out.set(key, {
          x: p.x,
          y: p.y,
          side: p.side,
          role: p.role,
          node: {x: p.node.x, y: p.node.y, w: p.node.w, h: p.node.h}
        });
      }
      return out;
    }

    function findBauhausNodeBySlotId(layoutView, slotId) {
      const positions = Array.isArray(layoutView && layoutView.positions) ? layoutView.positions : [];
      for (const node of positions) {
        if (node && node.slot && node.slot.slot_id === slotId) return node;
      }
      return null;
    }

    function estimateBauhausOrderPoint(layoutView, status, node, order) {
      const defaultMarket = Number(status && status.price);
      const slotMarket = Number(node && node.slot && node.slot.market_price || 0);
      const marketPrice = (Number.isFinite(defaultMarket) && defaultMarket > 0)
        ? defaultMarket
        : slotMarket;

      const orderPrice = Number(order && order.price);
      let side = String(order && order.side || '').toLowerCase();
      if (side !== 'sell' && side !== 'buy') {
        side = (Number.isFinite(orderPrice) && Number.isFinite(marketPrice) && orderPrice >= marketPrice) ? 'sell' : 'buy';
      }
      const direction = side === 'sell' ? -1 : 1;

      let rawOffset = 0;
      if (Number.isFinite(orderPrice) && orderPrice > 0 && Number.isFinite(marketPrice) && marketPrice > 0) {
        const pctDistance = Math.abs(orderPrice - marketPrice) / marketPrice;
        rawOffset = pctDistance * pctDistance * BAUHAUS_ORDER_SCALE_FACTOR;
      }

      const maxOffset = layoutView.inner.h * 0.5 * BAUHAUS_ORDER_MAX_OFFSET_RATIO;
      const absOffset = Math.min(maxOffset, rawOffset);
      const x = node ? node.x + node.w * 0.5 : (layoutView.inner.x + layoutView.inner.w * 0.5);
      const y = layoutView.priceY + direction * absOffset;
      return {
        x,
        y,
        side,
        role: String(order && order.role || '').toLowerCase()
      };
    }

    function queueBauhausFillAnimations(events, prevStatusSnapshot, nextStatus, startMs) {
      const list = Array.isArray(events) ? events : [];
      if (!list.length) return;

      const prevPointByKey = buildBauhausOrderPointIndex(prevStatusSnapshot);
      const nextLayout = computeBauhausLayout(nextStatus || {slots: []});
      let index = 0;

      for (const evt of list) {
        if (evt.type !== 'order_gone' || !evt.order) continue;

        const key = bauhausOrderEventKey(evt.slot_id, evt.order);
        const prevPoint = prevPointByKey.get(key) || null;
        const nextNode = findBauhausNodeBySlotId(nextLayout, evt.slot_id);
        const point = prevPoint || estimateBauhausOrderPoint(nextLayout, nextStatus, nextNode, evt.order);
        const side = String(point.side || '').toLowerCase() === 'sell' ? 'sell' : 'buy';
        const anchorNode = nextNode || (prevPoint ? prevPoint.node : null);
        const anchorX = anchorNode ? anchorNode.x + anchorNode.w * 0.5 : point.x;
        const anchorY = anchorNode
          ? (side === 'sell' ? anchorNode.y : anchorNode.y + anchorNode.h)
          : nextLayout.priceY;

        const dx = point.x - anchorX;
        const dy = point.y - anchorY;
        const distPx = Math.hypot(dx, dy);
        const extendMs = clamp((distPx / 100) * 1000, 160, 760);
        const role = String(point.role || '').toLowerCase();
        const dissolveMs = role === 'exit' ? 360 : 300;
        const totalMs = extendMs + dissolveMs;
        const id = key + ':' + String(startMs) + ':' + String(index);
        index += 1;

        bauhausFillAnims.push({
          id,
          role,
          anchorX,
          anchorY,
          targetX: point.x,
          targetY: point.y,
          startMs,
          extendMs,
          dissolveMs,
          totalMs,
          seed: hashString32(id)
        });
      }

      if (bauhausFillAnims.length > 180) {
        bauhausFillAnims = bauhausFillAnims.slice(-180);
      }
    }

    function drawBauhausOrderSquare(x, y, role, alpha) {
      const size = 6;
      const sx = Math.round(x - size * 0.5);
      const sy = Math.round(y - size * 0.5);
      const kind = String(role || '').toLowerCase();
      const a = clamp(Number(alpha), 0, 1);
      const prevAlpha = ctx.globalAlpha;
      ctx.globalAlpha = Number.isFinite(a) ? a : 1;

      if (kind === 'entry') {
        ctx.fillStyle = '#FFFFFF';
        ctx.fillRect(sx, sy, size, size);
        ctx.strokeStyle = BAUHAUS_COLORS.structure;
        ctx.lineWidth = 1;
        ctx.strokeRect(sx + 0.5, sy + 0.5, size - 1, size - 1);
      } else if (kind === 'exit') {
        ctx.fillStyle = BAUHAUS_COLORS.structure;
        ctx.fillRect(sx, sy, size, size);
      } else {
        ctx.fillStyle = '#777777';
        ctx.fillRect(sx, sy, size, size);
      }

      ctx.globalAlpha = prevAlpha;
    }

    function drawBauhausFillAnimations(nowMs, status) {
      if (!bauhausFillAnims.length) return;
      const keep = [];
      const visualState = getBauhausVisualState(status);
      const motionFactor = Math.max(0.05, visualState.motionFactor);

      for (const anim of bauhausFillAnims) {
        const elapsed = (nowMs - anim.startMs) * motionFactor;
        if (elapsed < 0) {
          keep.push(anim);
          continue;
        }
        if (elapsed > anim.totalMs) continue;

        const extendP = clamp(elapsed / anim.extendMs, 0, 1);
        const dissolveP = clamp((elapsed - anim.extendMs) / anim.dissolveMs, 0, 1);
        const lineP = elapsed < anim.extendMs ? extendP : (1 - dissolveP);
        const lineEndX = anim.anchorX + (anim.targetX - anim.anchorX) * lineP;
        const lineEndY = anim.anchorY + (anim.targetY - anim.anchorY) * lineP;

        ctx.strokeStyle = BAUHAUS_COLORS.structure;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(anim.anchorX) + 0.5, Math.round(anim.anchorY) + 0.5);
        ctx.lineTo(Math.round(lineEndX) + 0.5, Math.round(lineEndY) + 0.5);
        ctx.stroke();

        if (elapsed < anim.extendMs) {
          drawBauhausOrderSquare(anim.targetX, anim.targetY, anim.role, 1);
        } else {
          const shellAlpha = clamp(1 - dissolveP * 1.25, 0, 1);
          if (shellAlpha > 0.02) {
            drawBauhausOrderSquare(anim.targetX, anim.targetY, anim.role, shellAlpha);
          }

          const fragmentCount = anim.role === 'exit' ? 12 : 9;
          const fragmentAlpha = clamp(1 - dissolveP, 0, 1);
          const rgb = anim.role === 'exit'
            ? '0,0,0'
            : (anim.role === 'entry' ? '245,245,245' : '119,119,119');

          for (let i = 0; i < fragmentCount; i += 1) {
            const u = seededUnit(anim.seed, i + 11);
            const a = (Math.PI * 2 * i) / fragmentCount + dissolveP * 4.4;
            const r = 1 + dissolveP * (7 + u * 11);
            const px = anim.targetX + Math.cos(a) * r;
            const py = anim.targetY + Math.sin(a) * r;
            const size = anim.role === 'exit' ? (u > 0.58 ? 2 : 1) : 1;
            ctx.fillStyle = 'rgba(' + rgb + ',' + fragmentAlpha.toFixed(3) + ')';
            ctx.fillRect(Math.round(px), Math.round(py), size, size);
          }

          if (anim.role === 'exit') {
            const sparkleAlpha = clamp((1 - dissolveP) * 0.9, 0, 0.9);
            for (let i = 0; i < 10; i += 1) {
              const u = seededUnit(anim.seed, i + 101);
              const v = seededUnit(anim.seed, i + 151);
              const angle = u * Math.PI * 2 + dissolveP * 5.9;
              const radius = 2 + dissolveP * (8 + v * 10);
              const px = anim.targetX + Math.cos(angle) * radius;
              const py = anim.targetY + Math.sin(angle) * radius;
              ctx.fillStyle = 'rgba(25,25,25,' + sparkleAlpha.toFixed(3) + ')';
              ctx.fillRect(Math.round(px), Math.round(py), 1, 1);
            }
          }
        }

        keep.push(anim);
      }

      bauhausFillAnims = keep;
    }

    function hashString32(input) {
      // FNV-1a 32-bit hash for stable deterministic sprite placement.
      let hash = 0x811c9dc5;
      const txt = String(input || '');
      for (let i = 0; i < txt.length; i += 1) {
        hash ^= txt.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193);
      }
      return hash >>> 0;
    }

    function seededUnit(seed, index) {
      // Stateless deterministic pseudo-random [0,1) from seed/index.
      let x = (seed ^ Math.imul(index + 1, 0x9e3779b1)) >>> 0;
      x ^= x >>> 16;
      x = Math.imul(x, 0x85ebca6b);
      x ^= x >>> 13;
      x = Math.imul(x, 0xc2b2ae35);
      x ^= x >>> 16;
      return (x >>> 0) / 4294967296;
    }

    function orphanGradientColor(rankNorm, centerNorm) {
      const r = clamp(rankNorm, 0, 1);
      const c = clamp(centerNorm, 0, 1);
      const backlogBoost = activeEffects.has('belt_overflow') ? 12 : 0;
      const hue = 275 - r * 265; // violet -> blue -> green -> yellow -> red
      const sat = clamp(40 + c * 50 + backlogBoost, 0, 100);   // center desaturated, edge vivid
      const light = clamp(64 - c * 20 - (backlogBoost > 0 ? 4 : 0), 20, 90);
      return 'hsl(' + hue.toFixed(1) + ' ' + sat.toFixed(1) + '% ' + light.toFixed(1) + '%)';
    }

    function buildBauhausOrphans(layoutView, status) {
      const slots = Array.isArray(status && status.slots) ? status.slots : [];
      const marketPrice = Number((status && status.price) || 0);
      const margin = 12;
      const minBand = 14;
      const maxBand = Math.max(minBand + 1, layoutView.inner.h * 0.45);
      const out = [];

      for (const slot of slots) {
        const recs = Array.isArray(slot && slot.recovery_orders) ? slot.recovery_orders : [];
        const slotMarket = Number(slot && slot.market_price);
        const refPrice = (Number.isFinite(marketPrice) && marketPrice > 0) ? marketPrice : slotMarket;
        if (!Number.isFinite(refPrice) || refPrice <= 0) continue;
        for (const rec of recs) {
          const price = Number(rec && rec.price);
          if (!Number.isFinite(price) || price <= 0) continue;
          const side = String((rec && rec.side) || '').toLowerCase();
          const direction = side === 'sell' ? -1 : (side === 'buy' ? 1 : (price >= refPrice ? -1 : 1));
          const pctDistance = Math.abs(price - refPrice) / refPrice;
          const key = String(slot.slot_id) + ':' + String(rec.recovery_id);
          out.push({
            key,
            slot_id: slot.slot_id,
            recovery: rec,
            pctDistance,
            direction
          });
        }
      }

      out.sort((a, b) => a.pctDistance - b.pctDistance);

      const inner = layoutView.inner;
      const width = Math.max(1, inner.w - margin * 2);
      for (let i = 0; i < out.length; i += 1) {
        const item = out[i];
        const rankNorm = out.length <= 1 ? 0 : i / (out.length - 1);
        const seed = hashString32(item.key);
        const xRand = seededUnit(seed, 0);
        const yRand = seededUnit(seed, 1);
        const blendRand = seededUnit(seed, 2);
        const phaseRand = seededUnit(seed, 3);
        const periodRand = seededUnit(seed, 4);

        const x = inner.x + margin + xRand * width;
        const distNorm = Math.pow(rankNorm, 0.78);
        const targetMag = minBand + distNorm * (maxBand - minBand);
        const seedMag = minBand + yRand * (maxBand - minBand);
        const mag = targetMag * 0.68 + seedMag * 0.32;
        const y = layoutView.priceY + item.direction * mag + (blendRand - 0.5) * 14;
        const yClamped = clamp(y, inner.y + margin, inner.y + inner.h - margin);

        const centerNorm = clamp(Math.abs(yClamped - layoutView.priceY) / maxBand, 0, 1);
        const color = orphanGradientColor(rankNorm, centerNorm);
        const twinklePeriodMs = 2000 + periodRand * 2000;
        const twinklePhase = phaseRand * Math.PI * 2;

        item.rankNorm = rankNorm;
        item.x = x;
        item.y = yClamped;
        item.color = color;
        item.twinklePeriodMs = twinklePeriodMs;
        item.twinklePhase = twinklePhase;
      }

      return out;
    }

    function buildBauhausOrphanIndex(layoutView, status) {
      const index = new Map();
      const items = buildBauhausOrphans(layoutView, status);
      for (const item of items) {
        index.set(item.key, {
          x: item.x,
          y: item.y,
          color: item.color
        });
      }
      return index;
    }

    function queueBauhausOrphanRepriceAnimations(events, prevStatusSnapshot, nextStatus, startMs) {
      const list = Array.isArray(events) ? events : [];
      if (!list.length) return;

      const prevLayout = computeBauhausLayout(prevStatusSnapshot || nextStatus || {slots: []});
      const nextLayout = computeBauhausLayout(nextStatus || {slots: []});
      const prevIndex = buildBauhausOrphanIndex(prevLayout, prevStatusSnapshot || {slots: []});
      const nextIndex = buildBauhausOrphanIndex(nextLayout, nextStatus || {slots: []});

      for (const evt of list) {
        if (evt.type !== 'orphan_repriced') continue;
        const key = String(evt.recovery_key || (String(evt.slot_id) + ':' + String(evt.recovery_id)));
        const to = nextIndex.get(key);
        if (!to) continue;
        const from = prevIndex.get(key) || to;

        bauhausOrphanRepriceAnims.set(key, {
          key,
          startMs,
          durationMs: 900,
          fromX: from.x,
          fromY: from.y,
          toX: to.x,
          toY: to.y
        });
      }

      while (bauhausOrphanRepriceAnims.size > 260) {
        const oldest = bauhausOrphanRepriceAnims.keys().next();
        if (oldest.done) break;
        bauhausOrphanRepriceAnims.delete(oldest.value);
      }
    }

    function drawOrphanPlusSprite(x, y, armColor, alpha) {
      const px = 2;
      const cx = Math.round(x);
      const cy = Math.round(y);
      ctx.globalAlpha = alpha;

      ctx.fillStyle = armColor;
      ctx.fillRect(cx - px, cy, px, px);      // left
      ctx.fillRect(cx + px, cy, px, px);      // right
      ctx.fillRect(cx, cy - px, px, px);      // up
      ctx.fillRect(cx, cy + px, px, px);      // down

      ctx.fillStyle = BAUHAUS_COLORS.structure;
      ctx.fillRect(cx, cy, px, px);           // center

      ctx.globalAlpha = 1;
    }

    function drawBauhausOrphans(layoutView, status, nowMs) {
      const items = buildBauhausOrphans(layoutView, status);
      if (!items.length) {
        bauhausOrphanRepriceAnims.clear();
        return [];
      }
      const visualState = getBauhausVisualState(status);
      const overflow = activeEffects.has('belt_overflow');
      const twinkleBase = overflow ? 0.8 : 0.85;
      const twinkleAmp = overflow ? 0.2 : 0.15;
      const twinkleScale = visualState.halted ? 0 : visualState.motionFactor;
      const rendered = [];

      const currentKeys = new Set(items.map((item) => item.key));
      for (const [key, anim] of bauhausOrphanRepriceAnims) {
        if (!currentKeys.has(key) || (nowMs - anim.startMs) > (anim.durationMs + 1200)) {
          bauhausOrphanRepriceAnims.delete(key);
        }
      }

      ctx.imageSmoothingEnabled = false;
      for (const item of items) {
        const tw = visualState.halted
          ? 0.72
          : twinkleBase + twinkleAmp * Math.sin((nowMs * twinkleScale / item.twinklePeriodMs) * Math.PI * 2 + item.twinklePhase);
        const alpha = clamp(tw, visualState.halted ? 0.55 : 0.7, 1.0);
        const anim = bauhausOrphanRepriceAnims.get(item.key);
        if (!anim) {
          drawOrphanPlusSprite(item.x, item.y, item.color, alpha);
          rendered.push({
            key: item.key,
            slot_id: item.slot_id,
            recovery: item.recovery,
            pctDistance: item.pctDistance,
            x: item.x,
            y: item.y
          });
          continue;
        }

        const t = clamp((nowMs - anim.startMs) / anim.durationMs, 0, 1);
        const ease = t * t * (3 - 2 * t);
        const x = anim.fromX + (anim.toX - anim.fromX) * ease;
        const y = anim.fromY + (anim.toY - anim.fromY) * ease;
        const baseFade = clamp(1 - t * 1.2, 0, 1);
        const greyFade = clamp(0.25 + t * 0.75, 0, 1);

        if (baseFade > 0.03) {
          drawOrphanPlusSprite(x, y, item.color, alpha * baseFade);
        }
        drawOrphanPlusSprite(x, y, '#8A8A8A', alpha * greyFade);
        rendered.push({
          key: item.key,
          slot_id: item.slot_id,
          recovery: item.recovery,
          pctDistance: item.pctDistance,
          x,
          y
        });

        if (t >= 1) {
          bauhausOrphanRepriceAnims.delete(item.key);
        }
      }
      ctx.imageSmoothingEnabled = true;
      return rendered;
    }

    function drawBauhausDiagnosisOverlays(layoutView, status, nowMs) {
      const inner = layoutView.inner;
      const outer = layoutView.outer;
      const visualState = getBauhausVisualState(status);
      const hasCircuitSpark = activeEffects.has('circuit_spark');
      const hasRedWash = activeEffects.has('red_wash') || visualState.halted;

      if (visualState.brownout) {
        ctx.save();
        ctx.globalCompositeOperation = 'saturation';
        ctx.fillStyle = 'rgba(128,128,128,' + (visualState.paused ? '0.82' : '0.45') + ')';
        ctx.fillRect(inner.x, inner.y, inner.w, inner.h);
        ctx.restore();

        ctx.fillStyle = 'rgba(244,236,206,' + (visualState.paused ? '0.24' : '0.12') + ')';
        ctx.fillRect(inner.x, inner.y, inner.w, inner.h);
      }

      if (hasRedWash) {
        const tintAlpha = visualState.halted ? 0.2 : 0.1;
        ctx.fillStyle = 'rgba(82,44,24,' + tintAlpha.toFixed(3) + ')';
        ctx.fillRect(inner.x, inner.y, inner.w, inner.h);
      }

      if (visualState.halted) {
        const cx = inner.x + inner.w * 0.5;
        const cy = inner.y + inner.h * 0.5;
        const grad = ctx.createRadialGradient(cx, cy, Math.min(inner.w, inner.h) * 0.12, cx, cy, Math.max(inner.w, inner.h) * 0.75);
        grad.addColorStop(0, 'rgba(0,0,0,0.00)');
        grad.addColorStop(1, 'rgba(0,0,0,0.60)');
        ctx.fillStyle = grad;
        ctx.fillRect(inner.x, inner.y, inner.w, inner.h);
      }

      if (hasCircuitSpark) {
        const span = Math.max(1, outer.w - 18);
        for (let i = 0; i < 16; i += 1) {
          const phase = (nowMs * 0.12 + i * 63) % span;
          const x = outer.x + 9 + phase;
          const y = i % 2 === 0 ? outer.y + 2 : outer.y + outer.h - 2;
          const alpha = 0.45 + 0.4 * Math.abs(Math.sin(nowMs * 0.01 + i * 0.9));
          ctx.fillStyle = i % 3 === 0
            ? 'rgba(139,0,0,' + alpha.toFixed(3) + ')'
            : 'rgba(232,136,31,' + (alpha * 0.85).toFixed(3) + ')';
          ctx.fillRect(Math.round(x), Math.round(y), 2, 2);
        }
      }
    }

    function slotPhaseColor(phase) {
      if (phase === 'S0') return COLORS.good;
      if (phase === 'S2') return COLORS.bad;
      return COLORS.warn;
    }

    function powerColor(priceAgeSec) {
      const age = Number(priceAgeSec || 0);
      if (age <= 10) return COLORS.good;
      if (age <= 60) return COLORS.warn;
      return COLORS.bad;
    }

    function drawPowerLine(status, nowMs) {
      let color = powerColor(status.price_age_sec);
      if (activeEffects.has('power_dead')) {
        color = 'rgba(84,91,102,0.55)';
      } else if (activeEffects.has('power_dim')) {
        color = 'rgba(210,153,34,0.65)';
      }
      const y = layout.powerY;

      ctx.strokeStyle = color;
      ctx.lineWidth = 6;
      ctx.beginPath();
      ctx.moveTo(layout.powerX1, y);
      ctx.lineTo(layout.powerX2, y);
      ctx.stroke();

      ctx.lineWidth = 1;
      ctx.strokeStyle = 'rgba(255,255,255,0.08)';
      ctx.beginPath();
      ctx.moveTo(layout.powerX1, y + 11);
      ctx.lineTo(layout.powerX2, y + 11);
      ctx.stroke();

      ctx.fillStyle = COLORS.ink;
      ctx.font = '600 14px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillText(
        'POWER  PRICE $' + fmt(status.price, 6) + '   AGE ' + Math.round(Number(status.price_age_sec || 0)) + 's   MODE ' + String(status.mode || '-'),
        layout.powerX1,
        y - 11
      );

      const lineLen = Math.max(1, layout.powerX2 - layout.powerX1);
      let speedPxPerMs = 0;
      if (!activeEffects.has('power_dead')) {
        const age = Number(status.price_age_sec || 0);
        if (age < 10) speedPxPerMs = 0.12;
        else if (age < 60) speedPxPerMs = 0.055;
        else speedPxPerMs = 0.015;
      }
      if (speedPxPerMs <= 0) return;

      const particles = 7;
      ctx.fillStyle = color;
      for (let i = 0; i < particles; i += 1) {
        const offset = i * (lineLen / particles);
        const x = layout.powerX1 + ((nowMs * speedPxPerMs + offset) % lineLen);
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function drawChest(box, title, full, subtext, opts = {}) {
      const flashEmpty = !!opts.flashEmpty;
      const nowMs = Number(opts.nowMs || 0);
      const flashOn = !flashEmpty || (Math.floor(nowMs / 320) % 2 === 0);

      ctx.fillStyle = 'rgba(22,27,34,0.9)';
      roundRect(box.x, box.y, box.w, box.h, 8);
      ctx.fill();

      ctx.lineWidth = 2;
      if (full) {
        ctx.strokeStyle = COLORS.good;
      } else if (flashEmpty) {
        ctx.strokeStyle = flashOn ? COLORS.warn : 'rgba(210,153,34,0.15)';
      } else {
        ctx.strokeStyle = COLORS.line;
      }
      roundRect(box.x, box.y, box.w, box.h, 8);
      ctx.stroke();

      ctx.fillStyle = COLORS.ink;
      ctx.font = '700 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillText(title, box.x + 10, box.y + 20);
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillStyle = COLORS.muted;
      ctx.fillText('IN', box.x + 10, box.y + 38);
      ctx.fillText('chest', box.x + 10, box.y + 53);

      if (full) {
        ctx.fillStyle = 'rgba(46,160,67,0.22)';
      } else if (flashEmpty) {
        ctx.fillStyle = flashOn ? 'rgba(210,153,34,0.22)' : 'rgba(139,148,158,0.10)';
      } else {
        ctx.fillStyle = 'rgba(139,148,158,0.10)';
      }
      roundRect(box.x + 70, box.y + 12, 32, 56, 5);
      ctx.fill();

      if (subtext) {
        ctx.fillStyle = COLORS.muted;
        ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText(subtext, box.x + 8, box.y + box.h + 14);
      }
    }

    function getS2Ratio(slot, status) {
      const entered = Number(slot && slot.s2_entered_at);
      const timeoutSec = Number(status && status.s2_orphan_after_sec);
      if (!Number.isFinite(entered) || entered <= 0) return null;
      if (!Number.isFinite(timeoutSec) || timeoutSec <= 0) return null;
      const age = Date.now() / 1000 - entered;
      return clamp(age / timeoutSec, 0, 10);
    }

    function drawMachine(node, status, nowMs) {
      const slot = node.slot;
      const phase = String(slot.phase || 'S0');
      const border = slotPhaseColor(phase);
      const compact = !!(layout && layout.compact);
      const isHover = hoverSlotId === slot.slot_id;
      const isSelected = selectedSlotId === slot.slot_id;
      const hasWarningLamp = slotHasEffect(slot.slot_id, 'warning_lamp');
      const hasMachineDark = slotHasEffect(slot.slot_id, 'machine_dark');
      const hasConveyorStop = slotHasEffect(slot.slot_id, 'conveyor_stop');
      const r = compact ? 9 : 12;
      const pad = compact ? 5 : 8;
      const headerH = compact ? 18 : 32;
      const innerX = node.x + pad;
      const innerY = node.y + headerH;
      const innerW = node.w - pad * 2;
      const innerH = Math.max(12, node.h - headerH - pad);
      const laneHalfW = Math.max(8, Math.floor(innerW * 0.5));

      ctx.fillStyle = 'rgba(22,27,34,0.94)';
      roundRect(node.x, node.y, node.w, node.h, r);
      ctx.fill();

      if (isSelected) {
        ctx.save();
        ctx.strokeStyle = 'rgba(88,166,255,0.95)';
        ctx.shadowColor = 'rgba(88,166,255,0.45)';
        ctx.shadowBlur = 11;
        ctx.lineWidth = 2;
        roundRect(node.x - 3, node.y - 3, node.w + 6, node.h + 6, r + 2);
        ctx.stroke();
        ctx.restore();
      }

      ctx.strokeStyle = border;
      ctx.lineWidth = isHover ? 3 : 2;
      roundRect(node.x, node.y, node.w, node.h, r);
      ctx.stroke();

      if (phase === 'S1a') {
        ctx.fillStyle = 'rgba(210,153,34,0.18)';
        roundRect(innerX, innerY, laneHalfW, innerH, compact ? 5 : 8);
        ctx.fill();
      } else if (phase === 'S1b') {
        ctx.fillStyle = 'rgba(210,153,34,0.18)';
        roundRect(innerX + innerW - laneHalfW, innerY, laneHalfW, innerH, compact ? 5 : 8);
        ctx.fill();
      } else if (phase === 'S2') {
        ctx.fillStyle = 'rgba(248,81,73,0.16)';
        roundRect(innerX, innerY, innerW, innerH, compact ? 5 : 8);
        ctx.fill();
      }

      if (hasMachineDark && (slot.long_only || slot.short_only)) {
        ctx.fillStyle = 'rgba(0,0,0,0.30)';
        const darkW = Math.floor(innerW * 0.5);
        if (slot.long_only) {
          roundRect(innerX, innerY, darkW, innerH, compact ? 5 : 8);
          ctx.fill();
        }
        if (slot.short_only) {
          roundRect(innerX + innerW - darkW, innerY, darkW, innerH, compact ? 5 : 8);
          ctx.fill();
        }
      }

      ctx.fillStyle = COLORS.ink;
      if (compact) {
        ctx.font = '700 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText('#' + slot.slot_id, node.x + 7, node.y + 13);
        const badgeText = phase === 'S0' ? 'S0' : phase;
        const badgeW = Math.max(16, Math.ceil(ctx.measureText(badgeText).width) + 8);
        ctx.fillStyle = 'rgba(255,255,255,0.08)';
        roundRect(node.x + node.w - badgeW - 6, node.y + 5, badgeW, 12, 5);
        ctx.fill();
        ctx.fillStyle = border;
        ctx.font = '700 9px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText(badgeText, node.x + node.w - badgeW - 2, node.y + 14);
      } else {
        ctx.font = '700 14px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText('#' + slot.slot_id, node.x + 10, node.y + 22);
        ctx.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText(phase === 'S0' ? 'S0 idle' : phase, node.x + 10, node.y + 40);
      }

      let lampColor = COLORS.good;
      if (slot.long_only || slot.short_only) lampColor = COLORS.warn;
      if (hasWarningLamp) {
        lampColor = (Math.floor(nowMs / 250) % 2 === 0) ? COLORS.warn : COLORS.bad;
      }

      const s2Ratio = phase === 'S2' ? getS2Ratio(slot, status) : null;
      if (s2Ratio !== null && s2Ratio > 0.75) {
        lampColor = (Math.floor(nowMs / 500) % 2 === 0) ? COLORS.bad : 'rgba(248,81,73,0.35)';
      }

      drawDiamond(node.x + node.w - (compact ? 10 : 18), node.y + (compact ? 12 : 18), compact ? 5 : 7, lampColor);

      if (!compact && (slot.long_only || slot.short_only)) {
        ctx.fillStyle = COLORS.warn;
        ctx.font = '700 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        const modeTag = slot.long_only ? '[LO]' : '[SO]';
        ctx.fillText(modeTag, node.x + node.w - 45, node.y + 40);
      }

      if (!compact) {
        const recent = Array.isArray(slot.recent_cycles) && slot.recent_cycles.length ? slot.recent_cycles[0] : null;
        if (recent && Number.isFinite(Number(recent.net_profit))) {
          const pnl = Number(recent.net_profit);
          ctx.fillStyle = pnl >= 0 ? COLORS.good : COLORS.bad;
          ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
          ctx.fillText((pnl >= 0 ? '+' : '') + '$' + fmt(pnl, 4), node.x + 10, node.y + node.h + 15);
        }
      }

      if (!compact && s2Ratio !== null && s2Ratio > 0.75) {
        ctx.fillStyle = COLORS.bad;
        ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText('S2 ' + Math.round(s2Ratio * 100) + '%', node.x + node.w - 56, node.y + node.h + 15);
      }

      if (hasConveyorStop) {
        ctx.strokeStyle = COLORS.bad;
        ctx.lineWidth = 1.6;
        ctx.setLineDash([6, 4]);
        roundRect(innerX, node.y + node.h - (compact ? 11 : 18), innerW, compact ? 7 : 12, compact ? 4 : 6);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      let gearSpeed = 0;
      if (phase === 'S2') gearSpeed = 0.003;
      else if (phase === 'S1a' || phase === 'S1b') gearSpeed = 0.001;
      if (activeEffects.has('power_dead') || activeEffects.has('power_dim')) {
        gearSpeed *= 0.25;
      }

      const gearX = node.x + node.w - (compact ? 18 : 38);
      const gearY = node.y + node.h * 0.5 + (compact ? 3 : 10);
      const gearAngle = nowMs * gearSpeed;
      ctx.save();
      ctx.translate(gearX, gearY);
      ctx.rotate(gearAngle);
      ctx.globalAlpha = 0.45;
      ctx.strokeStyle = border;
      ctx.lineWidth = compact ? 1.3 : 1.6;
      ctx.beginPath();
      ctx.arc(0, 0, compact ? 5.5 : 8, 0, Math.PI * 2);
      ctx.stroke();
      for (let i = 0; i < 6; i += 1) {
        const a = (Math.PI * 2 * i) / 6;
        const x1 = Math.cos(a) * (compact ? 5.5 : 8);
        const y1 = Math.sin(a) * (compact ? 5.5 : 8);
        const x2 = Math.cos(a) * (compact ? 8 : 12);
        const y2 = Math.sin(a) * (compact ? 8 : 12);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
      ctx.restore();

      if (hasMachineDark) {
        const bx = node.x + node.w - (compact ? 8 : 12);
        const by = node.y - (compact ? 5 : 7) + Math.sin(nowMs * 0.003) * 3;
        ctx.save();
        ctx.translate(bx, by);
        ctx.strokeStyle = COLORS.warn;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(0, 0, 3.5, 0, Math.PI * 2);
        ctx.stroke();
        for (let i = 0; i < 6; i += 1) {
          const a = (Math.PI * 2 * i) / 6;
          const x1 = Math.cos(a) * 3.5;
          const y1 = Math.sin(a) * 3.5;
          const x2 = Math.cos(a) * 5.4;
          const y2 = Math.sin(a) * 5.4;
          ctx.beginPath();
          ctx.moveTo(x1, y1);
          ctx.lineTo(x2, y2);
          ctx.stroke();
        }
        ctx.beginPath();
        ctx.moveTo(1.8, 1.8);
        ctx.lineTo(7.2, 7.2);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(8.2, 8.2, 1.5, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }

      machineRects.push({
        slot_id: slot.slot_id,
        x: node.x,
        y: node.y,
        w: node.w,
        h: node.h
      });
    }

    function drawConveyors(nodes, nowMs) {
      if (!nodes.length) return;
      const compact = !!(layout && layout.compact);
      const first = nodes[0];
      const last = nodes[nodes.length - 1];

      const yTop = first.y + (compact ? 18 : 32);
      const yBottom = layout.dogeChest.y + (compact ? 30 : 40);

      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(88,166,255,0.45)';
      ctx.beginPath();
      ctx.moveTo(layout.usdChest.x + layout.usdChest.w + 12, yTop);
      ctx.lineTo(first.x - 16, yTop);
      ctx.stroke();

      ctx.strokeStyle = 'rgba(88,166,255,0.28)';
      ctx.beginPath();
      ctx.moveTo(layout.dogeChest.x + layout.dogeChest.w + 12, yBottom);
      ctx.lineTo(first.x - 16, yBottom);
      ctx.stroke();

      ctx.strokeStyle = 'rgba(88,166,255,0.38)';
      for (const node of nodes) {
        const mx = node.x + node.w * 0.5;
        const isStopped = slotHasEffect(node.slot.slot_id, 'conveyor_stop');
        if (isStopped) {
          ctx.strokeStyle = 'rgba(248,81,73,0.95)';
          ctx.setLineDash([6, 4]);
        } else {
          ctx.strokeStyle = 'rgba(88,166,255,0.38)';
          ctx.setLineDash([]);
        }
        ctx.beginPath();
        ctx.moveTo(mx, node.y + node.h + 4);
        ctx.lineTo(mx, layout.recycle.y - 4);
        ctx.stroke();
      }
      ctx.setLineDash([]);

      ctx.strokeStyle = 'rgba(88,166,255,0.50)';
      ctx.beginPath();
      ctx.moveTo(last.x + last.w + 16, yTop + 15);
      ctx.lineTo(layout.outputChest.x - 14, yTop + 15);
      ctx.stroke();

      const itemSpeed = 0.03; // px/ms
      for (const node of nodes) {
        const orders = Array.isArray(node.slot.open_orders) ? node.slot.open_orders : [];
        const hasEntry = orders.some((o) => o.role === 'entry');
        const hasExit = orders.some((o) => o.role === 'exit');
        const stopped = slotHasEffect(node.slot.slot_id, 'conveyor_stop');

        if (hasEntry) {
          const sx = Math.max(layout.usdChest.x + layout.usdChest.w + 8, node.x - 92);
          const ex = node.x - 6;
          const len = Math.max(1, ex - sx);
          const speed = stopped ? 0 : itemSpeed;
          const spacing = 42;
          for (let i = 0; i < 2; i += 1) {
            const x = sx + ((nowMs * speed + i * spacing) % len);
            const y = node.y + (compact ? 19 : 30);
            ctx.fillStyle = 'rgba(88,166,255,0.92)';
            ctx.beginPath();
            ctx.arc(x, y, 3, 0, Math.PI * 2);
            ctx.fill();
          }
        }

        if (hasExit) {
          const sx = node.x + node.w + 6;
          const ex = Math.max(sx + 1, Math.min(layout.outputChest.x - 10, node.x + node.w + 110));
          const len = Math.max(1, ex - sx);
          const speed = stopped ? 0 : itemSpeed;
          const spacing = 38;
          for (let i = 0; i < 2; i += 1) {
            const x = sx + ((nowMs * speed + i * spacing) % len);
            const y = node.y + (compact ? 31 : 46);
            ctx.fillStyle = 'rgba(46,160,67,0.94)';
            ctx.beginPath();
            ctx.arc(x, y, 3, 0, Math.PI * 2);
            ctx.fill();
          }
        }
      }
    }

    function drawOutputChest(status) {
      const box = layout.outputChest;

      ctx.fillStyle = 'rgba(22,27,34,0.9)';
      roundRect(box.x, box.y, box.w, box.h, 8);
      ctx.fill();

      ctx.lineWidth = 2;
      ctx.strokeStyle = COLORS.good;
      roundRect(box.x, box.y, box.w, box.h, 8);
      ctx.stroke();

      ctx.fillStyle = COLORS.ink;
      ctx.font = '700 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillText('PROFIT', box.x + 10, box.y + 20);
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillStyle = COLORS.muted;
      ctx.fillText('OUT chest', box.x + 10, box.y + 38);

      ctx.fillStyle = COLORS.good;
      ctx.font = '700 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillText('$' + fmt(status.total_profit, 4), box.x + 10, box.y + 62);
      ctx.fillStyle = COLORS.muted;
      ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillText(fmt(status.total_profit_doge, 3) + ' DOGE eq', box.x + 10, box.y + 78);
    }

    function drawRecyclingBelt(status, nowMs) {
      const belt = layout.recycle;
      const total = Number(status.total_orphans || 0);
      const overflow = activeEffects.has('belt_overflow');
      const slots = Array.isArray(status && status.slots) ? status.slots : [];
      const recoveries = [];
      for (const slot of slots) {
        for (const rec of (Array.isArray(slot.recovery_orders) ? slot.recovery_orders : [])) {
          recoveries.push({...rec, slot_id: slot.slot_id});
        }
      }
      recoveryDotPositions = [];

      ctx.fillStyle = 'rgba(22,27,34,0.8)';
      roundRect(belt.x, belt.y, belt.w, belt.h, 8);
      ctx.fill();

      ctx.strokeStyle = overflow ? 'rgba(248,81,73,0.88)' : 'rgba(210,153,34,0.70)';
      ctx.lineWidth = 2;
      ctx.setLineDash(overflow ? [6, 4] : [10, 7]);
      roundRect(belt.x, belt.y, belt.w, belt.h, 8);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = COLORS.muted;
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillText('RECYCLING BELT', belt.x + 10, belt.y - 8);

      const maxDots = overflow ? 72 : 40;
      const fallbackDots = Math.min(maxDots, Math.max(0, total));
      const source = recoveries.length ? recoveries : new Array(fallbackDots).fill(null);
      const dots = Math.min(maxDots, source.length);
      const innerX = belt.x + 10;
      const innerW = belt.w - 20;
      const drift = (nowMs * 0.01) % Math.max(1, innerW);
      for (let i = 0; i < dots; i += 1) {
        const srcIdx = source.length <= dots ? i : Math.floor(i * source.length / dots);
        const recovery = source[srcIdx] || null;
        const base = dots <= 1 ? innerW * 0.5 : i * (innerW / dots);
        const x = innerX + ((base + drift) % Math.max(1, innerW));
        const y = belt.y + belt.h * 0.5;
        const ageSec = recovery ? Number(recovery.age_sec || 0) : 0;
        const dotColor = overflow || ageSec > 900 ? COLORS.bad : (total > 12 ? COLORS.bad : COLORS.warn);
        ctx.fillStyle = dotColor;
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
        recoveryDotPositions.push({x, y, recovery});
      }
    }

    function drawModeOverlay(nowMs) {
      const hasRedWash = activeEffects.has('red_wash');
      const hasAmberWash = activeEffects.has('amber_wash') && !hasRedWash;
      if (hasAmberWash) {
        ctx.fillStyle = 'rgba(210,153,34,0.16)';
        ctx.fillRect(0, 0, worldW, worldH);
      }
      if (hasRedWash) {
        ctx.fillStyle = 'rgba(248,81,73,0.18)';
        ctx.fillRect(0, 0, worldW, worldH);
      }
      if (activeEffects.has('alarm_pulse')) {
        const pulse = 0.5 + 0.5 * Math.sin(nowMs / 220);
        const cx = worldW * 0.5;
        const cy = 96;
        const radius = 20 + pulse * 6;
        ctx.strokeStyle = 'rgba(248,81,73,' + (0.45 + pulse * 0.45).toFixed(3) + ')';
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.arc(cx, cy, radius, 0, Math.PI * 2);
        ctx.stroke();
        ctx.fillStyle = COLORS.bad;
        ctx.font = '700 22px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText('ALARM', cx - 42, cy + 7);
      }
    }

    function drawCircuitEffects(nowMs) {
      const hasSpark = activeEffects.has('circuit_spark');
      const hasHazard = activeEffects.has('hazard_icon');
      if (!hasSpark && !hasHazard) return;

      const anchorX = worldW - 220;
      const anchorY = worldH - 34;

      if (hasHazard) {
        ctx.fillStyle = 'rgba(248,81,73,0.90)';
        ctx.beginPath();
        ctx.moveTo(anchorX, anchorY - 20);
        ctx.lineTo(anchorX - 14, anchorY + 6);
        ctx.lineTo(anchorX + 14, anchorY + 6);
        ctx.closePath();
        ctx.fill();
        ctx.fillStyle = '#0d1117';
        ctx.font = '700 14px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText('!', anchorX - 3.5, anchorY + 2);
      }

      if (hasSpark) {
        for (let i = 0; i < 7; i += 1) {
          const phase = nowMs * 0.012 + i * 1.43;
          const px = anchorX - 126 + i * 20;
          const py = anchorY - 12 + Math.sin(phase) * 7;
          const size = 2.4 + Math.abs(Math.sin(phase * 1.7)) * 1.6;
          drawDiamond(px, py, size, i % 2 === 0 ? COLORS.warn : COLORS.bad);
        }
      }
    }

    function getMachineRect(slotId) {
      for (const rect of machineRects) {
        if (rect.slot_id === slotId) return rect;
      }
      return null;
    }

    function drawEventAnimations(nowMs) {
      if (!animQueue.length) return;
      const keep = [];

      for (const anim of animQueue) {
        const progress = clamp((nowMs - anim.startMs) / anim.durationMs, 0, 1);
        const alive = progress < 1;

        if (anim.type === 'order_placed') {
          const rect = getMachineRect(anim.slot_id);
          if (rect) {
            const alpha = Math.min(1, progress * 2);
            ctx.fillStyle = 'rgba(88,166,255,' + alpha.toFixed(3) + ')';
            ctx.beginPath();
            ctx.arc(rect.x + rect.w * 0.5, rect.y + 18, 6, 0, Math.PI * 2);
            ctx.fill();
          }
        } else if (anim.type === 'order_gone') {
          const rect = getMachineRect(anim.slot_id);
          if (anim.order && anim.order.role === 'exit') {
            if (rect && layout && layout.outputChest) {
              const sx = rect.x + rect.w * 0.5;
              const sy = rect.y + rect.h * 0.5;
              const tx = layout.outputChest.x + 18;
              const ty = layout.outputChest.y + 44;
              const trail = [
                {offset: -0.15, radius: 2, alpha: 0.20},
                {offset: -0.10, radius: 3, alpha: 0.28},
                {offset: -0.05, radius: 4, alpha: 0.36}
              ];
              for (const t of trail) {
                const p = clamp(progress + t.offset, 0, 1);
                if (p <= 0) continue;
                const x = sx + (tx - sx) * p;
                const y = sy + (ty - sy) * p;
                ctx.fillStyle = 'rgba(46,160,67,' + (t.alpha * (1 - progress * 0.25)).toFixed(3) + ')';
                ctx.beginPath();
                ctx.arc(x, y, t.radius, 0, Math.PI * 2);
                ctx.fill();
              }
              const x = sx + (tx - sx) * progress;
              const y = sy + (ty - sy) * progress;
              ctx.fillStyle = 'rgba(46,160,67,' + (1 - progress * 0.2).toFixed(3) + ')';
              ctx.beginPath();
              ctx.arc(x, y, 5, 0, Math.PI * 2);
              ctx.fill();
            }
          } else if (rect && anim.order && anim.order.role === 'entry') {
            const alpha = (1 - progress) * 0.58;
            const side = String(anim.order.side || '').toLowerCase();
            const inset = 6;
            const innerX = rect.x + inset;
            const innerY = rect.y + (layout && layout.compact ? 18 : 34);
            const innerW = rect.w - inset * 2;
            const innerH = Math.max(10, rect.h - (layout && layout.compact ? 26 : 42));
            const flashW = innerW * 0.5;
            const flashX = side === 'sell' ? innerX : innerX + innerW - flashW;
            ctx.fillStyle = 'rgba(46,160,67,' + alpha.toFixed(3) + ')';
            roundRect(flashX, innerY, flashW, innerH, 7);
            ctx.fill();
          }
        } else if (anim.type === 'order_orphaned') {
          const rect = getMachineRect(anim.slot_id);
          if (rect && layout && layout.recycle) {
            const sx = rect.x + rect.w * 0.5;
            const sy = rect.y + rect.h;
            const tx = sx;
            const ty = layout.recycle.y + layout.recycle.h * 0.5;
            const x = sx + (tx - sx) * progress;
            const y = sy + (ty - sy) * progress;
            ctx.fillStyle = 'rgba(210,153,34,' + (1 - progress * 0.1).toFixed(3) + ')';
            ctx.beginPath();
            ctx.arc(x, y, 4.5, 0, Math.PI * 2);
            ctx.fill();
          }
        } else if (anim.type === 'cycle_completed') {
          if (layout && layout.outputChest) {
            const cx = layout.outputChest.x + 40;
            const cy = layout.outputChest.y + 46;
            const net = Number(anim.cycle && anim.cycle.net_profit);
            const highProfit = Number.isFinite(net) && net > 0.01;
            const sparkleRgb = highProfit ? '255,215,0' : '46,160,67';
            for (let i = 0; i < 12; i += 1) {
              const waveStart = i < 6 ? 0 : 0.3;
              if (progress < waveStart) continue;
              const localP = clamp((progress - waveStart) / (1 - waveStart), 0, 1);
              const a = (Math.PI * 2 * (i % 6)) / 6 + (i >= 6 ? 0.28 : 0);
              const r = 7 + localP * 22 + (i % 3) * 2;
              const x = cx + Math.cos(a) * r;
              const y = cy + Math.sin(a) * r;
              const size = 2 + (i % 3);
              ctx.fillStyle = 'rgba(' + sparkleRgb + ',' + (1 - localP).toFixed(3) + ')';
              ctx.beginPath();
              ctx.arc(x, y, size, 0, Math.PI * 2);
              ctx.fill();
            }
            const txt = Number.isFinite(net) ? ((net >= 0 ? '+' : '') + '$' + fmt(net, 2)) : '+$';
            ctx.fillStyle = (Number.isFinite(net) && net < 0)
              ? 'rgba(248,81,73,' + (1 - progress).toFixed(3) + ')'
              : (highProfit
                ? 'rgba(255,215,0,' + (1 - progress).toFixed(3) + ')'
                : 'rgba(46,160,67,' + (1 - progress).toFixed(3) + ')');
            ctx.font = '700 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
            ctx.fillText(txt, cx + 28, cy - progress * 28);
          }
        } else if (anim.type === 'phase_change') {
          const rect = getMachineRect(anim.slot_id);
          if (rect) {
            const alpha = 1 - progress;
            const from = String(anim.from || '');
            const to = String(anim.to || '');
            let ring = 'rgba(210,153,34,' + alpha.toFixed(3) + ')';
            if (from === 'S2' && to === 'S0') ring = 'rgba(46,160,67,' + alpha.toFixed(3) + ')';
            else if ((from === 'S1a' || from === 'S1b') && to === 'S2') ring = 'rgba(248,81,73,' + alpha.toFixed(3) + ')';
            ctx.lineWidth = 3;
            ctx.strokeStyle = ring;
            const expand = 3 + progress * 10;
            roundRect(rect.x - expand, rect.y - expand, rect.w + expand * 2, rect.h + expand * 2, 14 + progress * 4);
            ctx.stroke();
            if ((from === 'S1a' || from === 'S1b') && to === 'S2') {
              const flash = alpha * (Math.floor(nowMs / 120) % 2 === 0 ? 1 : 0.35);
              ctx.fillStyle = 'rgba(248,81,73,' + flash.toFixed(3) + ')';
              ctx.beginPath();
              ctx.moveTo(rect.x + rect.w + 8, rect.y + 6);
              ctx.lineTo(rect.x + rect.w - 4, rect.y + 26);
              ctx.lineTo(rect.x + rect.w + 20, rect.y + 26);
              ctx.closePath();
              ctx.fill();
              ctx.fillStyle = 'rgba(13,17,23,' + flash.toFixed(3) + ')';
              ctx.font = '700 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
              ctx.fillText('!', rect.x + rect.w + 5, rect.y + 22);
            } else if (from === 'S2' && to === 'S0') {
              ctx.strokeStyle = 'rgba(46,160,67,' + (alpha * 0.8).toFixed(3) + ')';
              ctx.lineWidth = 2;
              roundRect(rect.x - expand - 6, rect.y - expand - 6, rect.w + (expand + 6) * 2, rect.h + (expand + 6) * 2, 17);
              ctx.stroke();
            }
          }
        } else if (anim.type === 'slot_added') {
          const rect = getMachineRect(anim.slot_id);
          if (rect) {
            const eased = easeOutBack(progress);
            const ghostX = rect.x + (1 - eased) * 200;
            const coverAlpha = clamp(0.95 - progress, 0, 0.95);
            ctx.fillStyle = 'rgba(13,17,23,' + coverAlpha.toFixed(3) + ')';
            roundRect(rect.x - 2, rect.y - 2, rect.w + 4, rect.h + 4, 12);
            ctx.fill();

            const ghostAlpha = clamp(1 - progress * 0.35, 0, 1);
            ctx.fillStyle = 'rgba(22,27,34,' + (0.8 * ghostAlpha).toFixed(3) + ')';
            roundRect(ghostX, rect.y, rect.w, rect.h, 12);
            ctx.fill();
            ctx.strokeStyle = 'rgba(88,166,255,' + ghostAlpha.toFixed(3) + ')';
            ctx.lineWidth = 2;
            roundRect(ghostX, rect.y, rect.w, rect.h, 12);
            ctx.stroke();
            ctx.fillStyle = 'rgba(88,166,255,' + ghostAlpha.toFixed(3) + ')';
            ctx.font = '700 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
            ctx.fillText('#' + anim.slot_id, ghostX + 10, rect.y + 19);
          }
        } else if (anim.type === 'slot_removed') {
          const rect = anim.anchor || getMachineRect(anim.slot_id);
          if (rect) {
            const alpha = 1 - progress;
            const scale = 1 - progress * 0.35;
            const w = rect.w * scale;
            const h = rect.h * scale;
            const x = rect.x + (rect.w - w) * 0.5;
            const y = rect.y + (rect.h - h) * 0.5;
            ctx.fillStyle = 'rgba(22,27,34,' + (0.75 * alpha).toFixed(3) + ')';
            roundRect(x, y, w, h, 12);
            ctx.fill();
            ctx.strokeStyle = 'rgba(248,81,73,' + alpha.toFixed(3) + ')';
            ctx.lineWidth = 2;
            roundRect(x, y, w, h, 12);
            ctx.stroke();
          }
        }

        if (alive) keep.push(anim);
      }

      animQueue = keep;
    }

    function renderStatusBar(status) {
      const cfh = status.capacity_fill_health || {};
      const util = cfh.open_order_utilization_pct;
      const utilTxt = (util === null || util === undefined) ? '-' : fmt(util, 1) + '%';
      document.getElementById('capText').textContent =
        String(cfh.open_orders_current ?? '-') + '/' + String(cfh.open_orders_safe_cap ?? '-') + ' (' + utilTxt + ')';
      const mode = String(status.mode || '').toUpperCase();
      const modeDot = document.getElementById('modeDot');
      modeDot.style.background = mode === 'HALTED' ? COLORS.bad : (mode === 'PAUSED' ? COLORS.warn : COLORS.good);
      const band = String(cfh.status_band || '-').toUpperCase();
      const bandEl = document.getElementById('bandText');
      bandEl.textContent = band;
      bandEl.style.color = band === 'STOP' ? COLORS.bad : (band === 'CAUTION' ? COLORS.warn : COLORS.good);
      document.getElementById('slotsText').textContent = String(status.slot_count ?? 0);
      document.getElementById('profitText').textContent = '$' + fmt(status.total_profit, 4);

      document.getElementById('pairBadge').textContent = String(status.pair || '-');
    }

    function renderNotifStrip() {
      const strip = document.getElementById('notifStrip');
      const bauhaus = isBauhausMode();
      const sevGood = bauhaus ? '#1F5F24' : COLORS.good;
      const sevWarn = bauhaus ? '#8C5A00' : COLORS.warn;
      const sevCrit = bauhaus ? BAUHAUS_COLORS.alert : COLORS.bad;
      strip.style.background = bauhaus ? 'rgba(244,196,48,0.95)' : 'rgba(22,27,34,.96)';
      strip.style.borderColor = bauhaus ? BAUHAUS_COLORS.frame : COLORS.line;
      strip.style.color = bauhaus ? BAUHAUS_COLORS.structure : COLORS.ink;

      if (!activeSymptoms.length) {
        strip.textContent = '... waiting for diagnosis';
        strip.style.color = bauhaus ? BAUHAUS_COLORS.text : COLORS.muted;
        strip.title = '';
        return;
      }

      if (notifIndex >= activeSymptoms.length) notifIndex = 0;
      const top = activeSymptoms[notifIndex];
      if (activeSymptoms.length === 1 && top.symptom_id === 'IDLE_NORMAL') {
        strip.textContent = bauhaus ? '✓ Running' : 'OK Factory running normally';
        strip.style.borderColor = bauhaus ? BAUHAUS_COLORS.frame : COLORS.good;
        strip.style.color = sevGood;
        strip.title = '';
        return;
      }

      const icon = top.severity === 'crit' ? (bauhaus ? 'CRIT' : '!!') : (top.severity === 'warn' ? (bauhaus ? 'WARN' : '!') : 'OK');
      const summary = String(top.summary || '');
      const prefix = activeSymptoms.length > 1 ? '[' + (notifIndex + 1) + '/' + activeSymptoms.length + '] ' : '';
      strip.textContent = prefix + icon + ' ' + top.symptom_id + ': ' + summary;
      const color = top.severity === 'crit'
        ? sevCrit
        : (top.severity === 'warn' ? sevWarn : sevGood);
      strip.style.borderColor = bauhaus ? BAUHAUS_COLORS.frame : color;
      strip.style.color = color;
      strip.title = activeSymptoms.length > 1 ? 'Click or press Enter to cycle symptoms' : '';
    }

    function cycleNotifSymptom() {
      if (activeSymptoms.length <= 1) return;
      notifIndex = (notifIndex + 1) % activeSymptoms.length;
      renderNotifStrip();
    }

    function renderDetailPanel() {
      if (isBauhausMode()) {
        PANEL.classList.remove('open');
        PANEL.setAttribute('aria-hidden', 'true');
        return;
      }
      if (selectedSlotId === null || !statusData) {
        PANEL.classList.remove('open');
        PANEL.setAttribute('aria-hidden', 'true');
        return;
      }

      const slot = getSlotById(selectedSlotId);
      if (!slot) {
        selectedSlotId = null;
        PANEL.classList.remove('open');
        PANEL.setAttribute('aria-hidden', 'true');
        return;
      }

      DETAIL_TITLE.textContent = 'Slot #' + slot.slot_id;
      const flags = [];
      if (slot.long_only) flags.push('LO');
      if (slot.short_only) flags.push('SO');
      DETAIL_SUB.innerHTML =
        'Phase <span class="pill">' + String(slot.phase || 'S0') + '</span>' +
        (flags.length ? ' <span class="pill">' + flags.join('/') + '</span>' : '');

      const openRows = (Array.isArray(slot.open_orders) ? slot.open_orders : []).slice(0, 10)
        .map((o) => '<tr><td>' + o.side + '/' + o.role + '</td><td>' + o.trade_id + '.' + o.cycle + '</td><td>$' + fmt(o.price, 6) + '</td><td>' + fmt(o.volume, 3) + '</td></tr>')
        .join('');

      const recRows = (Array.isArray(slot.recovery_orders) ? slot.recovery_orders : []).slice(0, 8)
        .map((r) => '<tr><td>#' + r.recovery_id + '</td><td>' + r.side + '</td><td>' + Math.round(Number(r.age_sec || 0)) + 's</td><td>$' + fmt(r.price, 6) + '</td><td><button class="miniBtn" data-close-rid="' + r.recovery_id + '">close</button></td></tr>')
        .join('');

      const cycleRows = (Array.isArray(slot.recent_cycles) ? slot.recent_cycles : []).slice(0, 8)
        .map((c) => {
          const pnl = Number(c.net_profit);
          const color = Number.isFinite(pnl) && pnl < 0 ? COLORS.bad : COLORS.good;
          return '<tr><td>' + c.trade_id + '.' + c.cycle + '</td><td>$' + fmt(c.entry_price, 6) + '</td><td>$' + fmt(c.exit_price, 6) + '</td><td style="color:' + color + ';">$' + fmt(c.net_profit, 4) + '</td><td>' + (c.from_recovery ? 'yes' : 'no') + '</td></tr>';
        })
        .join('');

      DETAIL_CONTENT.innerHTML =
        '<table>' +
          '<tr><th>Metric</th><th>Value</th></tr>' +
          '<tr><td>Market Price</td><td>$' + fmt(slot.market_price, 6) + '</td></tr>' +
          '<tr><td>Cycles</td><td>A.' + slot.cycle_a + ' / B.' + slot.cycle_b + '</td></tr>' +
          '<tr><td>Order Size</td><td>$' + fmt(slot.order_size_usd, 4) + '</td></tr>' +
          '<tr><td>Realized</td><td>$' + fmt(slot.total_profit, 6) + '</td></tr>' +
          '<tr><td>Unrealized</td><td>$' + fmt(slot.unrealized_profit, 6) + '</td></tr>' +
          '<tr><td>Round Trips</td><td>' + String(slot.total_round_trips || 0) + '</td></tr>' +
        '</table>' +
        '<table><tr><th colspan="4">Open Orders</th></tr>' +
          '<tr><th>Type</th><th>Trade</th><th>Price</th><th>Vol</th></tr>' +
          (openRows || '<tr><td colspan="4" class="mono">none</td></tr>') +
        '</table>' +
        '<table><tr><th colspan="5">Recovery Orders</th></tr>' +
          '<tr><th>ID</th><th>Side</th><th>Age</th><th>Price</th><th></th></tr>' +
          (recRows || '<tr><td colspan="5" class="mono">none</td></tr>') +
        '</table>' +
        '<table><tr><th colspan="5">Recent Cycles</th></tr>' +
          '<tr><th>Trade</th><th>Entry</th><th>Exit</th><th>Net</th><th>Rec</th></tr>' +
          (cycleRows || '<tr><td colspan="5" class="mono">none</td></tr>') +
        '</table>';

      PANEL.classList.add('open');
      PANEL.setAttribute('aria-hidden', 'false');

      for (const btn of DETAIL_CONTENT.querySelectorAll('button[data-close-rid]')) {
        btn.addEventListener('click', () => {
          const recoveryId = Number.parseInt(btn.getAttribute('data-close-rid'), 10);
          if (!Number.isFinite(recoveryId)) return;
          requestSoftClose(slot.slot_id, recoveryId);
        });
      }
    }

    function hitTestMachine(worldX, worldY) {
      for (const r of machineRects) {
        if (worldX >= r.x && worldX <= r.x + r.w && worldY >= r.y && worldY <= r.y + r.h) {
          return r.slot_id;
        }
      }
      return null;
    }

    function renderFactory(nowMs) {
      if (!statusData || !layout) {
        tooltipText = '';
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, viewportW, viewportH);
        ctx.fillStyle = COLORS.muted;
        ctx.font = '13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText('Loading /api/status ...', 24, 34);
        return;
      }

      camera.x += (camera.targetX - camera.x) * 0.18;
      camera.y += (camera.targetY - camera.y) * 0.18;
      camera.zoom += (camera.targetZoom - camera.zoom) * 0.18;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, viewportW, viewportH);

      ctx.setTransform(
        dpr * camera.zoom,
        0,
        0,
        dpr * camera.zoom,
        dpr * (viewportW * 0.5 - camera.x * camera.zoom),
        dpr * (viewportH * 0.5 - camera.y * camera.zoom)
      );

      ctx.fillStyle = 'rgba(255,255,255,0.01)';
      ctx.fillRect(0, 0, worldW, worldH);

      machineRects = [];
      drawPowerLine(statusData, nowMs);

      const slots = Array.isArray(statusData.slots) ? statusData.slots : [];
      const hasNonLongOnly = slots.length === 0 ? true : slots.some((s) => !s.long_only);
      const hasNonShortOnly = slots.length === 0 ? true : slots.some((s) => !s.short_only);
      const flashInputs = activeEffects.has('input_flash_empty');
      const flashUsd = flashInputs && slots.some((s) => s.short_only);
      const flashDoge = flashInputs && slots.some((s) => s.long_only);

      drawChest(
        layout.usdChest,
        'USD',
        hasNonLongOnly,
        hasNonLongOnly ? 'supply ok' : 'starved',
        {flashEmpty: flashUsd, nowMs}
      );
      drawChest(
        layout.dogeChest,
        'DOGE',
        hasNonShortOnly,
        hasNonShortOnly ? 'supply ok' : 'starved',
        {flashEmpty: flashDoge, nowMs}
      );

      drawConveyors(layout.positions, nowMs);
      for (const node of layout.positions) {
        drawMachine(node, statusData, nowMs);
      }

      drawOutputChest(statusData);
      drawRecyclingBelt(statusData, nowMs);
      drawModeOverlay(nowMs);
      drawCircuitEffects(nowMs);
      drawEventAnimations(nowMs);

      lastMachineRectBySlot = {};
      for (const rect of machineRects) {
        lastMachineRectBySlot[rect.slot_id] = {
          x: rect.x,
          y: rect.y,
          w: rect.w,
          h: rect.h
        };
      }

      drawTooltip();
    }

    function renderBauhaus(nowMs) {
      const bauhausLayout = computeBauhausLayout(statusData || {slots: []});
      drawBauhausFrame(bauhausLayout);

      machineRects = [];

      if (!statusData) {
        clearBauhausRenderCaches();
        ctx.fillStyle = BAUHAUS_COLORS.text;
        ctx.font = '13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText('Loading /api/status ...', bauhausLayout.inner.x + 14, bauhausLayout.inner.y + 26);
        drawTooltip();
        return;
      }

      bauhausLastPriceLineRect = drawBauhausPriceLine(bauhausLayout, statusData, nowMs);
      drawBauhausSlots(bauhausLayout, statusData, nowMs);
      bauhausLastOrderPoints = drawBauhausOrders(bauhausLayout, statusData, nowMs) || [];
      drawBauhausFillAnimations(nowMs, statusData);
      bauhausLastOrphanSprites = drawBauhausOrphans(bauhausLayout, statusData, nowMs) || [];
      drawBauhausProfitFlights(nowMs, statusData);
      bauhausLastCounterRect = drawBauhausProfitCounter(bauhausLayout);
      drawBauhausDiagnosisOverlays(bauhausLayout, statusData, nowMs);

      // Preserve slot removal animation anchoring parity across modes.
      lastMachineRectBySlot = {};
      for (const rect of machineRects) {
        lastMachineRectBySlot[rect.slot_id] = {
          x: rect.x,
          y: rect.y,
          w: rect.w,
          h: rect.h
        };
      }
      drawTooltip();
    }

    function renderCurrentView(nowMs) {
      if (renderMode === RENDER_MODE_BAUHAUS) {
        renderBauhaus(nowMs);
        return;
      }
      renderFactory(nowMs);
    }

    function scheduleFrame() {
      if (rafPending) return;
      rafPending = true;
      requestAnimationFrame((ts) => {
        rafPending = false;
        if (ts - lastFrameMs >= 33) {
          lastFrameMs = ts;
          renderCurrentView(ts);
        }
        scheduleFrame();
      });
    }

    function centerCamera() {
      camera.x = worldW * 0.5;
      camera.y = worldH * 0.5;
      camera.zoom = 1;
      camera.targetX = camera.x;
      camera.targetY = camera.y;
      camera.targetZoom = camera.zoom;
    }

    async function refreshStatus() {
      try {
        const res = await fetch('/api/status', {cache: 'no-store'});
        if (!res.ok) throw new Error('status request failed: ' + res.status);
        const next = await res.json();
        const events = diff(statusData, next);
        const startMs = performance.now();
        for (const evt of events) {
          if (evt.type === 'orphan_repriced') continue;
          const anim = {
            type: evt.type,
            slot_id: evt.slot_id,
            order: evt.order || null,
            recovery: evt.recovery || null,
            cycle: evt.cycle || null,
            from: evt.from || null,
            to: evt.to || null,
            startMs,
            durationMs: animationDurationMs(evt)
          };
          if (evt.type === 'slot_removed' && lastMachineRectBySlot[evt.slot_id]) {
            anim.anchor = lastMachineRectBySlot[evt.slot_id];
          }
          animQueue.push(anim);
        }
        if (animQueue.length > 240) {
          animQueue = animQueue.slice(animQueue.length - 240);
        }

        if (isBauhausMode()) {
          queueBauhausFillAnimations(events, statusData, next, startMs);
          queueBauhausOrphanRepriceAnimations(events, statusData, next, startMs);
        }

        statusData = next;
        layout = computeLayout(next);
        updateBauhausThicknessWindow(next);
        bauhausProfitTarget = Number(next.total_profit || 0);
        if (bauhausProfitDisplayed === null || !Number.isFinite(bauhausProfitDisplayed)) {
          bauhausProfitDisplayed = bauhausProfitTarget;
        }
        queueBauhausProfitFlights(events, next, startMs);
        setActiveSymptoms(diagnose(next));

        if (selectedSlotId !== null && !getSlotById(selectedSlotId)) {
          selectedSlotId = null;
        }

        renderStatusBar(next);
        renderNotifStrip();
        renderDetailPanel();

        if (!refreshStatus._centered) {
          centerCamera();
          refreshStatus._centered = true;
        }
      } catch (err) {
        const strip = document.getElementById('notifStrip');
        strip.textContent = 'Status error: ' + (err && err.message ? err.message : 'unknown error');
        strip.style.color = COLORS.bad;
        strip.style.borderColor = COLORS.bad;
      }
    }

    async function poll() {
      await refreshStatus();
    }

    function handleCanvasContextMenu(ev) {
      if (isBauhausMode()) return;
      const world = screenToWorld(ev.clientX, ev.clientY);
      const hit = hitTestMachine(world.x, world.y);
      if (hit !== null) {
        ev.preventDefault();
        showToast('Remove slot not yet available', 'info');
      }
    }

    function handleCanvasMouseDown(ev) {
      if (isBauhausMode()) {
        if (ev.button !== 0) return;
        tooltipX = ev.clientX;
        tooltipY = ev.clientY;
        const hit = resolveBauhausHoverTarget(ev.clientX, ev.clientY);
        if (!hit) {
          if (bauhausPinnedTooltip) {
            clearBauhausPinnedTooltip();
          }
          return;
        }
        bauhausPinnedTooltip = {
          type: hit.type,
          key: hit.key,
          slot_id: hit.slot_id || null,
          text: hit.text,
          x: ev.clientX,
          y: ev.clientY
        };
        tooltipText = hit.text;
        hoverSlotId = hit.slot_id || null;
        scheduleFrame();
        return;
      }
      if (ev.button !== 0) return;
      tooltipX = ev.clientX;
      tooltipY = ev.clientY;
      const world = screenToWorld(ev.clientX, ev.clientY);
      const hit = hitTestMachine(world.x, world.y);

      if (hit !== null) {
        selectSlot(hit, false);
        return;
      }

      dragging = true;
      dragFromEmpty = true;
      dragStartMs = performance.now();
      dragDistance = 0;
      dragLastX = ev.clientX;
      dragLastY = ev.clientY;
      canvas.classList.add('dragging');
    }

    function handleWindowMouseUp() {
      if (isBauhausMode()) return;
      const wasDragging = dragging;
      const wasFromEmpty = dragFromEmpty;
      const elapsed = performance.now() - dragStartMs;
      const moved = dragDistance;
      dragging = false;
      dragFromEmpty = false;
      canvas.classList.remove('dragging');
      if (wasDragging && wasFromEmpty && elapsed < 200 && moved < 6 && selectedSlotId !== null) {
        selectedSlotId = null;
        renderDetailPanel();
        scheduleFrame();
      }
    }

    function handleCanvasMouseMove(ev) {
      if (isBauhausMode()) {
        if (bauhausPinnedTooltip) {
          const needsUpdate = tooltipText !== bauhausPinnedTooltip.text
            || tooltipX !== bauhausPinnedTooltip.x
            || tooltipY !== bauhausPinnedTooltip.y
            || hoverSlotId !== (bauhausPinnedTooltip.slot_id || null);
          if (needsUpdate) {
            tooltipText = bauhausPinnedTooltip.text;
            tooltipX = bauhausPinnedTooltip.x;
            tooltipY = bauhausPinnedTooltip.y;
            hoverSlotId = bauhausPinnedTooltip.slot_id || null;
            scheduleFrame();
          }
          return;
        }

        tooltipX = ev.clientX;
        tooltipY = ev.clientY;
        const hit = resolveBauhausHoverTarget(ev.clientX, ev.clientY);
        const nextTooltip = hit ? hit.text : '';
        const nextHover = hit && hit.slot_id !== undefined ? (hit.slot_id || null) : null;
        if (nextTooltip !== tooltipText || nextHover !== hoverSlotId) {
          tooltipText = nextTooltip;
          hoverSlotId = nextHover;
          scheduleFrame();
        }
        return;
      }
      tooltipX = ev.clientX;
      tooltipY = ev.clientY;
      const world = screenToWorld(ev.clientX, ev.clientY);
      const hit = hitTestMachine(world.x, world.y);
      if (hit !== hoverSlotId) {
        hoverSlotId = hit;
        scheduleFrame();
      }

      if (dragging) {
        tooltipText = '';
        const dx = ev.clientX - dragLastX;
        const dy = ev.clientY - dragLastY;
        dragDistance += Math.sqrt(dx * dx + dy * dy);
        dragLastX = ev.clientX;
        dragLastY = ev.clientY;
        camera.targetX -= dx / camera.zoom;
        camera.targetY -= dy / camera.zoom;
        scheduleFrame();
        return;
      }

      if (!statusData || !layout) {
        if (tooltipText) {
          tooltipText = '';
          scheduleFrame();
        }
        return;
      }

      let nextTooltip = '';
      if (hit !== null) {
        const slot = getSlotById(hit);
        if (slot) {
          const orders = Array.isArray(slot.open_orders) ? slot.open_orders.length : 0;
          nextTooltip = 'Slot #' + slot.slot_id + ' | ' + String(slot.phase || 'S0')
            + ' | $' + fmt(slot.total_profit, 4) + ' profit | ' + orders + ' orders';
        }
      } else {
        const dot = hitTestRecoveryDot(world.x, world.y);
        if (dot && dot.recovery) {
          const rec = dot.recovery;
          nextTooltip = 'Recovery #' + rec.recovery_id + ' | ' + String(rec.side || '-')
            + ' | $' + fmt(rec.price, 6) + ' | ' + Math.round(Number(rec.age_sec || 0)) + 's';
        } else if (pointInBox(world.x, world.y, layout.usdChest)) {
          const slots = Array.isArray(statusData.slots) ? statusData.slots : [];
          const healthy = slots.length === 0 ? true : slots.some((s) => !s.long_only);
          nextTooltip = healthy ? 'USD side healthy' : 'USD side starved - long_only';
        } else if (pointInBox(world.x, world.y, layout.dogeChest)) {
          const slots = Array.isArray(statusData.slots) ? statusData.slots : [];
          const healthy = slots.length === 0 ? true : slots.some((s) => !s.short_only);
          nextTooltip = healthy ? 'DOGE side healthy' : 'DOGE side starved - short_only';
        } else if (pointInBox(world.x, world.y, layout.outputChest)) {
          nextTooltip = 'Total realized: $' + fmt(statusData.total_profit, 4);
        }
      }

      if (nextTooltip !== tooltipText) {
        tooltipText = nextTooltip;
        scheduleFrame();
      }
    }

    function handleCanvasMouseLeave() {
      if (isBauhausMode()) {
        if (!bauhausPinnedTooltip) {
          hoverSlotId = null;
          tooltipText = '';
          scheduleFrame();
        }
        return;
      }
      hoverSlotId = null;
      tooltipText = '';
      scheduleFrame();
    }

    function handleCanvasWheel(ev) {
      ev.preventDefault();
      if (isBauhausMode()) return;
      const factor = ev.deltaY < 0 ? 1.08 : 0.92;
      const prevZoom = camera.targetZoom;
      camera.targetZoom = clamp(camera.targetZoom * factor, 0.6, 2.2);

      const worldBefore = screenToWorld(ev.clientX, ev.clientY);
      const zoomRatio = camera.targetZoom / prevZoom;
      camera.targetX = worldBefore.x - (worldBefore.x - camera.targetX) / zoomRatio;
      camera.targetY = worldBefore.y - (worldBefore.y - camera.targetY) / zoomRatio;
      scheduleFrame();
    }

    function handleCanvasDoubleClick() {
      if (isBauhausMode()) return;
      centerCamera();
      scheduleFrame();
    }

    function handleDetailCloseClick() {
      selectedSlotId = null;
      renderDetailPanel();
      scheduleFrame();
    }

    function handleAddSlotClick() {
      void dispatchAction('add_slot');
    }

    const cmdInput = document.getElementById('cmdInput');
    const notifStrip = document.getElementById('notifStrip');

    function handleCommandInput() {
      suggestionIndex = -1;
      renderCommandSuggestions();
    }

    function handleCommandKeyDown(event) {
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
    }

    function handleConfirmOkClick() {
      void confirmAccept();
    }

    function handleConfirmCancelClick() {
      confirmCancel();
    }

    function handleNotifStripClick() {
      cycleNotifSymptom();
    }

    function handleNotifStripKeyDown(event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        cycleNotifSymptom();
      }
    }

    function handleWindowResize() {
      updateCanvasSize();
      if (statusData) {
        layout = computeLayout(statusData);
      }
      scheduleFrame();
    }

    function bindMouseHandlers() {
      canvas.addEventListener('contextmenu', handleCanvasContextMenu);
      canvas.addEventListener('mousedown', handleCanvasMouseDown);
      window.addEventListener('mouseup', handleWindowMouseUp);
      canvas.addEventListener('mousemove', handleCanvasMouseMove);
      canvas.addEventListener('mouseleave', handleCanvasMouseLeave);
      canvas.addEventListener('wheel', handleCanvasWheel, {passive: false});
      canvas.addEventListener('dblclick', handleCanvasDoubleClick);
    }

    function bindUiHandlers() {
      document.getElementById('detailClose').addEventListener('click', handleDetailCloseClick);
      document.getElementById('addBtn').addEventListener('click', handleAddSlotClick);
      cmdInput.addEventListener('input', handleCommandInput);
      cmdInput.addEventListener('keydown', handleCommandKeyDown);
      document.getElementById('confirmOkBtn').addEventListener('click', handleConfirmOkClick);
      document.getElementById('confirmCancelBtn').addEventListener('click', handleConfirmCancelClick);
      notifStrip.addEventListener('click', handleNotifStripClick);
      notifStrip.addEventListener('keydown', handleNotifStripKeyDown);
      document.addEventListener('keydown', handleKey);
      window.addEventListener('resize', handleWindowResize);
    }

    function init() {
      bindMouseHandlers();
      bindUiHandlers();
      updateKbModeBadge();
      updateCanvasSize();
      scheduleFrame();
      void poll();
      window.setInterval(() => {
        void poll();
      }, 5000);
    }

    init();
  </script>
</body>
</html>
"""
