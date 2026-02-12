"""
factory_viz.py -- Factory Lens F1 static canvas view.

Served at GET /factory.

F1 scope:
- Static factory render from GET /api/status
- Power line, input chests, slot machines, output chest, recycling belt
- Status bar + notification strip
- Read-only detail panel for selected slot

Out of scope for F1:
- Diagnosis engine
- Interactive control actions
- Animation beyond lightweight visual pulse/blink cues
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
      pointer-events: none;
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
    <div id="title">Factory Lens F1</div>
    <div id="pairBadge">-</div>
  </div>

  <aside id="detailPanel" aria-hidden="true">
    <button id="detailClose" class="closeBtn">Close</button>
    <h2 id="detailTitle">Slot</h2>
    <div id="detailSub" class="sub"></div>
    <div id="detailContent"></div>
  </aside>

  <div id="notifStrip">Loading factory status...</div>

  <div id="statusBar">
    <div class="left">
      <span>Capacity <span id="capText" class="value">-</span></span>
      <span>Band <span id="bandText" class="value">-</span></span>
      <span>Slots <span id="slotsText" class="value">-</span></span>
      <span>Profit <span id="profitText" class="value">-</span></span>
    </div>
    <button id="addBtn" title="F1 static view">+Add</button>
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

    let dragging = false;
    let dragLastX = 0;
    let dragLastY = 0;

    let rafPending = false;
    let lastFrameMs = 0;
    let activeSymptoms = [];
    let activeEffects = new Set();
    let slotEffects = {};

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

    function fmt(n, digits = 2) {
      if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
      return Number(n).toFixed(digits);
    }

    function showToast(text) {
      const el = document.getElementById('toast');
      el.textContent = String(text || 'ok');
      el.style.display = 'block';
      window.clearTimeout(showToast._t);
      showToast._t = window.setTimeout(() => {
        el.style.display = 'none';
      }, 2200);
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
      const machineW = 140;
      const machineH = 100;
      const gap = 40;
      const maxPerRow = 6;
      const rowGap = 140;

      const cols = Math.max(1, Math.min(maxPerRow, slots.length || 1));
      const rows = Math.max(1, Math.ceil((slots.length || 1) / maxPerRow));

      const leftPad = 230;
      const topPad = 140;

      const machineBandW = cols * machineW + (cols - 1) * gap;
      worldW = Math.max(800, leftPad + machineBandW + 330);
      worldH = Math.max(500, topPad + rows * machineH + (rows - 1) * rowGap + 230);

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
        outputChest: {x: worldW - 178, y: firstY + 40, w: 120, h: 92},
        recycle: {x: 220, y: worldH - 110, w: Math.max(360, worldW - 440), h: 36},
        positions
      };
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

    function drawPowerLine(status) {
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

    function drawMachine(node, status, nowSec) {
      const slot = node.slot;
      const phase = String(slot.phase || 'S0');
      const border = slotPhaseColor(phase);
      const isHover = hoverSlotId === slot.slot_id;
      const isSelected = selectedSlotId === slot.slot_id;
      const hasWarningLamp = slotHasEffect(slot.slot_id, 'warning_lamp');
      const hasMachineDark = slotHasEffect(slot.slot_id, 'machine_dark');
      const hasConveyorStop = slotHasEffect(slot.slot_id, 'conveyor_stop');

      ctx.fillStyle = 'rgba(22,27,34,0.94)';
      roundRect(node.x, node.y, node.w, node.h, 12);
      ctx.fill();

      ctx.strokeStyle = border;
      ctx.lineWidth = isSelected ? 4 : isHover ? 3 : 2;
      roundRect(node.x, node.y, node.w, node.h, 12);
      ctx.stroke();

      if (phase === 'S1a') {
        ctx.fillStyle = 'rgba(210,153,34,0.18)';
        roundRect(node.x + 8, node.y + 34, 56, 56, 8);
        ctx.fill();
      } else if (phase === 'S1b') {
        ctx.fillStyle = 'rgba(210,153,34,0.18)';
        roundRect(node.x + node.w - 64, node.y + 34, 56, 56, 8);
        ctx.fill();
      } else if (phase === 'S2') {
        ctx.fillStyle = 'rgba(248,81,73,0.16)';
        roundRect(node.x + 8, node.y + 34, node.w - 16, 56, 8);
        ctx.fill();
      }

      if (hasMachineDark && (slot.long_only || slot.short_only)) {
        ctx.fillStyle = 'rgba(0,0,0,0.30)';
        const innerX = node.x + 8;
        const innerY = node.y + 34;
        const innerW = node.w - 16;
        const darkW = Math.floor(innerW * 0.5);
        if (slot.long_only) {
          roundRect(innerX, innerY, darkW, 56, 8);
          ctx.fill();
        }
        if (slot.short_only) {
          roundRect(innerX + innerW - darkW, innerY, darkW, 56, 8);
          ctx.fill();
        }
      }

      ctx.fillStyle = COLORS.ink;
      ctx.font = '700 14px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillText('#' + slot.slot_id, node.x + 10, node.y + 22);
      ctx.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.fillText(phase === 'S0' ? 'S0 idle' : phase, node.x + 10, node.y + 40);

      let lampColor = COLORS.good;
      if (slot.long_only || slot.short_only) lampColor = COLORS.warn;
      if (hasWarningLamp) {
        lampColor = (Math.floor(nowSec * 4) % 2 === 0) ? COLORS.warn : COLORS.bad;
      }

      const s2Ratio = phase === 'S2' ? getS2Ratio(slot, status) : null;
      if (s2Ratio !== null && s2Ratio > 0.75) {
        lampColor = (Math.floor(nowSec * 2) % 2 === 0) ? COLORS.bad : 'rgba(248,81,73,0.35)';
      }

      drawDiamond(node.x + node.w - 18, node.y + 18, 7, lampColor);

      if (slot.long_only || slot.short_only) {
        ctx.fillStyle = COLORS.warn;
        ctx.font = '700 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        const modeTag = slot.long_only ? '[LO]' : '[SO]';
        ctx.fillText(modeTag, node.x + node.w - 45, node.y + 40);
      }

      const recent = Array.isArray(slot.recent_cycles) && slot.recent_cycles.length ? slot.recent_cycles[0] : null;
      if (recent && Number.isFinite(Number(recent.net_profit))) {
        const pnl = Number(recent.net_profit);
        ctx.fillStyle = pnl >= 0 ? COLORS.good : COLORS.bad;
        ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText((pnl >= 0 ? '+' : '') + '$' + fmt(pnl, 4), node.x + 10, node.y + node.h + 15);
      }

      if (s2Ratio !== null && s2Ratio > 0.75) {
        ctx.fillStyle = COLORS.bad;
        ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        ctx.fillText('S2 ' + Math.round(s2Ratio * 100) + '%', node.x + node.w - 56, node.y + node.h + 15);
      }

      if (hasConveyorStop) {
        ctx.strokeStyle = COLORS.bad;
        ctx.lineWidth = 1.6;
        ctx.setLineDash([6, 4]);
        roundRect(node.x + 8, node.y + node.h - 18, node.w - 16, 12, 6);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      machineRects.push({
        slot_id: slot.slot_id,
        x: node.x,
        y: node.y,
        w: node.w,
        h: node.h
      });
    }

    function drawConveyors(nodes) {
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];

      const yTop = first.y + 32;
      const yBottom = layout.dogeChest.y + 40;

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

    function drawRecyclingBelt(status) {
      const belt = layout.recycle;
      const total = Number(status.total_orphans || 0);
      const overflow = activeEffects.has('belt_overflow');

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

      const dots = overflow
        ? Math.min(72, Math.max(0, total * 2 + 6))
        : Math.min(40, Math.max(0, total));
      const innerX = belt.x + 10;
      const innerW = belt.w - 20;
      for (let i = 0; i < dots; i += 1) {
        const t = dots <= 1 ? 0.5 : i / (dots - 1);
        const x = innerX + innerW * t;
        const y = belt.y + belt.h * 0.5;
        ctx.fillStyle = overflow ? COLORS.bad : (total > 12 ? COLORS.bad : COLORS.warn);
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
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

    function renderStatusBar(status) {
      const cfh = status.capacity_fill_health || {};
      const util = cfh.open_order_utilization_pct;
      const utilTxt = (util === null || util === undefined) ? '-' : fmt(util, 1) + '%';
      document.getElementById('capText').textContent =
        String(cfh.open_orders_current ?? '-') + '/' + String(cfh.open_orders_safe_cap ?? '-') + ' (' + utilTxt + ')';
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
      if (!activeSymptoms.length) {
        strip.textContent = '... waiting for diagnosis';
        strip.style.borderColor = COLORS.line;
        strip.style.color = COLORS.muted;
        return;
      }

      const top = activeSymptoms[0];
      if (activeSymptoms.length === 1 && top.symptom_id === 'IDLE_NORMAL') {
        strip.textContent = 'OK Factory running normally';
        strip.style.borderColor = COLORS.good;
        strip.style.color = COLORS.good;
        return;
      }

      const icon = top.severity === 'crit' ? '!!' : (top.severity === 'warn' ? '!' : 'OK');
      const summary = String(top.summary || '');
      strip.textContent = icon + ' ' + top.symptom_id + ': ' + summary;
      const color = top.severity === 'crit'
        ? COLORS.bad
        : (top.severity === 'warn' ? COLORS.warn : COLORS.good);
      strip.style.borderColor = color;
      strip.style.color = color;
    }

    function renderDetailPanel() {
      if (!selectedSlotId || !statusData) {
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
        .map((r) => '<tr><td>#' + r.recovery_id + '</td><td>' + r.side + '</td><td>' + Math.round(Number(r.age_sec || 0)) + 's</td><td>$' + fmt(r.price, 6) + '</td></tr>')
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
        '<table><tr><th colspan="4">Recovery Orders</th></tr>' +
          '<tr><th>ID</th><th>Side</th><th>Age</th><th>Price</th></tr>' +
          (recRows || '<tr><td colspan="4" class="mono">none</td></tr>') +
        '</table>';

      PANEL.classList.add('open');
      PANEL.setAttribute('aria-hidden', 'false');
    }

    function hitTestMachine(worldX, worldY) {
      for (const r of machineRects) {
        if (worldX >= r.x && worldX <= r.x + r.w && worldY >= r.y && worldY <= r.y + r.h) {
          return r.slot_id;
        }
      }
      return null;
    }

    function drawScene(nowMs) {
      if (!statusData || !layout) {
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
      drawPowerLine(statusData);

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

      drawConveyors(layout.positions);
      for (const node of layout.positions) {
        drawMachine(node, statusData, nowMs / 1000);
      }

      drawOutputChest(statusData);
      drawRecyclingBelt(statusData);
      drawModeOverlay(nowMs);
      drawCircuitEffects(nowMs);
    }

    function scheduleFrame() {
      if (rafPending) return;
      rafPending = true;
      requestAnimationFrame((ts) => {
        rafPending = false;
        if (ts - lastFrameMs >= 33) {
          lastFrameMs = ts;
          drawScene(ts);
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
        statusData = next;
        layout = computeLayout(next);
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

    canvas.addEventListener('mousedown', (ev) => {
      const world = screenToWorld(ev.clientX, ev.clientY);
      const hit = hitTestMachine(world.x, world.y);

      if (hit !== null) {
        selectedSlotId = hit;
        renderDetailPanel();
        scheduleFrame();
        return;
      }

      dragging = true;
      dragLastX = ev.clientX;
      dragLastY = ev.clientY;
      canvas.classList.add('dragging');
    });

    window.addEventListener('mouseup', () => {
      dragging = false;
      canvas.classList.remove('dragging');
    });

    canvas.addEventListener('mousemove', (ev) => {
      const world = screenToWorld(ev.clientX, ev.clientY);
      const hit = hitTestMachine(world.x, world.y);
      if (hit !== hoverSlotId) {
        hoverSlotId = hit;
        scheduleFrame();
      }

      if (!dragging) return;
      const dx = ev.clientX - dragLastX;
      const dy = ev.clientY - dragLastY;
      dragLastX = ev.clientX;
      dragLastY = ev.clientY;
      camera.targetX -= dx / camera.zoom;
      camera.targetY -= dy / camera.zoom;
      scheduleFrame();
    });

    canvas.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      const factor = ev.deltaY < 0 ? 1.08 : 0.92;
      const prevZoom = camera.targetZoom;
      camera.targetZoom = clamp(camera.targetZoom * factor, 0.6, 2.2);

      const worldBefore = screenToWorld(ev.clientX, ev.clientY);
      const zoomRatio = camera.targetZoom / prevZoom;
      camera.targetX = worldBefore.x - (worldBefore.x - camera.targetX) / zoomRatio;
      camera.targetY = worldBefore.y - (worldBefore.y - camera.targetY) / zoomRatio;
      scheduleFrame();
    }, {passive: false});

    canvas.addEventListener('dblclick', () => {
      centerCamera();
      scheduleFrame();
    });

    document.getElementById('detailClose').addEventListener('click', () => {
      selectedSlotId = null;
      renderDetailPanel();
      scheduleFrame();
    });

    document.getElementById('addBtn').addEventListener('click', () => {
      showToast('F1 static view: +Add is not wired yet');
    });

    window.addEventListener('resize', () => {
      updateCanvasSize();
      if (statusData) {
        layout = computeLayout(statusData);
      }
      scheduleFrame();
    });

    updateCanvasSize();
    scheduleFrame();
    refreshStatus();
    window.setInterval(refreshStatus, 5000);
  </script>
</body>
</html>
"""
