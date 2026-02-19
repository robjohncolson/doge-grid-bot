# Per-Slot Sticky Toggle + Ranger/Churner Deprecation

**Version**: 0.1
**Status**: Draft
**Replaces**: Ranger slots (bot.py:7024-7668), Churner slots (bot.py:7685-8880)

---

## 1. Problem

### 1.1 Ranger slots don't work

Rangers are standalone sell-side micro-cyclers that operate only during `RANGING` consensus. In practice:

- **Regime oscillation**: When HMM flips `RANGING -> BULLISH -> RANGING` (which happens frequently), rangers cancel all open exits and re-place entries on every flip. This litters the Kraken order book with useless cancel/place churn.
- **Fixed spreads**: Entry at 0.15% above market regardless of actual volatility. In low-vol ranging, 5-minute entry timeout kills the order before it can fill. In high-vol, it fills but the 1.2% exit target is unreachable.
- **Net result**: Lots of API calls, very few completed cycles, wasted rate-limit budget.

### 1.2 Churner slots don't work

Churners are a self-healing mechanism for stuck positions. They spawn tight cycles to generate micro-profits that subsidize repricing the parent's stale exit. In practice:

- **Same regime thrash**: Also gated to RANGING, same cancel/respawn oscillation.
- **Capital starvation**: Most stuck positions are B-side (buy entry, sell exit). Churner derives entry_side="buy" which requires USD. But the account typically has DOGE surplus / USD deficit. The capital-adaptive flip exists but the tight regime gating still causes thrash.

### 1.3 Both are invisible in visualizations

Rangers and churners are **separate execution engines** with their own status payloads:

```
status.slots[]              -> regular pair slots (factory/bauhaus renders these)
status.rangers{}            -> separate payload, own panel only
status.self_healing.churner -> separate payload, own panel only
```

Factory viz (`factory_viz.py`) and Bauhaus viz only iterate `status.slots`. Ranger and churner activity is completely invisible in the main visualization layers. They have small dedicated dashboard panels, but debugging requires navigating to those specific panels.

### 1.4 Root cause

Rangers and churners are separate engines bolted onto the main pair system to work around sticky slot limitations. The real fix is to let some regular pair slots operate in non-sticky mode, cycling freely through stale exits instead of holding them.

---

## 2. Solution: Per-Slot Sticky Toggle

Instead of two parallel engines, add a per-slot `sticky` boolean to the existing slot system:

- **Sticky slots** (`sticky=true`, default): Current behavior. Exits wait patiently. Position ledger tracks age bands. Self-healing repricing when subsidized.
- **Non-sticky slots** (`sticky=false`): Old-school cycling. Stale exits get repriced, then written off. Slot immediately places fresh entry and keeps cycling.

### 2.1 Why this is better

| Aspect | Ranger/Churner | Per-slot sticky toggle |
|--------|---------------|----------------------|
| Visualization | Invisible in factory/bauhaus | Regular slots, fully visible |
| State machine | Separate engines (~1300 lines) | Same S0/S1/S2 state machine |
| Both sides | Ranger: sell-only. Churner: opposite-of-parent only | Full A/B bidirectional cycling |
| Regime gating | Hard-gated to RANGING | No regime gating needed |
| Fill routing | Separate detection + handlers | Standard pair fill path |
| Persistence | Ranger: transient. Churner: snapshot | Same snapshot as all slots |
| Config | 20+ config vars across two systems | 1 boolean per slot |
| Dashboard | Separate panels, separate APIs | Same slot detail view |

---

## 3. Non-Sticky Exit Lifecycle

When a non-sticky slot's exit gets stale, it follows: **reprice -> write-off -> fresh entry**.

### 3.1 Happy path (same for both modes)

1. Entry fills (buy or sell)
2. Exit placed at profit target
3. Exit fills -> book profit, increment cycle counter
4. Place new entry on same side
5. Slot back in S0, cycling

### 3.2 Stale exit path (non-sticky only)

1. Exit doesn't fill after `EXIT_REPRICE_MULTIPLIER x median_duration` (~1.5x)
2. **Reprice**: Tighten exit progressively toward market (existing logic in `check_stale_exits()`, grid_strategy.py:4681-4800)
3. Still stuck after `EXIT_ORPHAN_MULTIPLIER x median_duration` (~5.0x)
4. **Write-off**:
   - Cancel exit order on Kraken (do NOT leave lottery tickets)
   - Close position in ledger with `close_reason="written_off"` (position_ledger.py:243)
   - State machine places fresh entry on same side (existing `OrphanOrderAction` flow)
5. Slot is back in S0, cycling again

### 3.3 Stale exit path (sticky, unchanged)

1. Exit waits patiently, no orphan timeout
2. Age band system classifies: Fresh -> Aging -> Stale -> Stuck
3. Self-healing repricing when subsidy available
4. Manual release via dashboard as escape hatch

---

## 4. Configuration

### 4.1 Per-slot toggle

Each slot has a `sticky: bool` field on `SlotRuntime` (default `True` for backward compatibility).

Toggled via:
- Dashboard button (per-slot toggle in slot detail view)
- POST `/api/slot/{id}/action` with `action=toggle_sticky`

### 4.2 Existing config reuse

Non-sticky behavior is controlled by **existing config knobs** — no new config needed:

| Config | Default | Used by non-sticky |
|--------|---------|-------------------|
| `EXIT_REPRICE_MULTIPLIER` | 1.5 | Reprice threshold = median x 1.5 |
| `EXIT_ORPHAN_MULTIPLIER` | 5.0 | Write-off threshold = median x 5.0 |
| `REPRICE_COOLDOWN_SEC` | 120 | Minimum time between reprices |
| `MIN_CYCLES_FOR_TIMING` | 3 | Min cycles to compute median duration |

### 4.3 Deprecated config

All `RANGER_*` and `CHURNER_*` config variables are deprecated. Defaults already `False` for both `RANGER_ENABLED` and `CHURNER_ENABLED`.

| Deprecated | Count | Status |
|-----------|-------|--------|
| `RANGER_*` | 10 vars (config.py:501-528) | Default disabled |
| `CHURNER_*` | 10 vars (config.py:471-498) | Default disabled |
| `MTS_CHURNER_GATE` | 1 var (config.py:831) | Dead when churner disabled |

---

## 5. Implementation

### 5.1 Phase 1: SlotRuntime.sticky + EngineConfig per-slot

**bot.py**:
- Add `sticky: bool = True` to `SlotRuntime` dataclass (~line 978)
- `_engine_cfg()` (~line 2007): accept `slot_id`, look up `self.slots[slot_id].sticky` instead of global `STICKY_MODE_ENABLED`
- Update all `_engine_cfg()` call sites to pass `slot_id`
- `_slot_mode_for_position()` (~line 5633): accept `slot_id`, return `"sticky"` or `"legacy"` per-slot
- Add `_is_slot_sticky(slot_id)` helper

**state_machine.py**: No changes. `EngineConfig.sticky_mode_enabled` (line 80) already controls orphan behavior in `transition()` (line 1026).

### 5.2 Phase 2: Non-sticky write-off path

**bot.py `_execute_actions()` (~line 13756)**:

Currently `OrphanOrderAction` is a no-op (`pass` — keeps order on Kraken as lottery ticket). For non-sticky slots:

- Cancel the exit on Kraken
- Find position via `_position_for_order` lookup
- Close position in ledger with `close_reason="written_off"` and negative P&L
- Unbind position-to-exit mapping
- Clean up `state.recovery_orders` if the state machine added one

New method: `_write_off_orphaned_position(slot_id, action)` encapsulates this.

### 5.3 Phase 3: Persistence + toggle API

**Snapshot**:
- `_global_snapshot()`: add `"slot_sticky": {sid: slot.sticky ...}`
- `_load_snapshot()`: restore from `slot_sticky` dict (default `True` for missing keys)

**Toggle API**:
- POST action `toggle_sticky` on existing slot action endpoint
- Flips `slot.sticky`, saves snapshot
- Toggling sticky -> non-sticky while exit is stale: next timer tick orphans naturally

**Dashboard badge**:
- Slot state bar: `[STICKY]` or `[CYCLE]` badge with distinct colors
- Toggle button in slot detail view

### 5.4 Phase 4: Ranger/Churner deprecation

**config.py**: Add `# DEPRECATED` comments to all RANGER_* and CHURNER_* blocks.

**bot.py**: Engine calls already gated. No code deletion.

**dashboard.py**: Hide ranger/churner panels when `enabled === false`.

### 5.5 Phase 5: Edge cases

- **Startup reconciliation**: Non-sticky slots with leftover recovery orders -> cancel on Kraken
- **S2 toggle safety**: S2 break-glass already handles orphaning worse leg
- **`_auto_release_sticky_slots()`**: Skip non-sticky slots
- **Action gate** (~line 17397): `soft_close` blocked in sticky mode -> allow for non-sticky slots (check per-slot)

---

## 6. Files Modified

| File | Scope |
|------|-------|
| `bot.py` | SlotRuntime.sticky, _engine_cfg per-slot, _is_slot_sticky(), _write_off_orphaned_position(), OrphanOrderAction handler, snapshot save/load, toggle API, startup reconciliation, _auto_release filter, action gate per-slot |
| `config.py` | Deprecation comments on RANGER_*/CHURNER_* |
| `dashboard.py` | Sticky/cycle badge, toggle button, hide ranger/churner panels when disabled |
| `state_machine.py` | No changes |
| `position_ledger.py` | No changes (written_off path exists at line 243) |

---

## 7. Verification

### 7.1 Unit tests (new `tests/test_nonsticky_slots.py`)

- `test_slot_runtime_sticky_default_true` — backward compat
- `test_engine_cfg_uses_per_slot_sticky` — per-slot, not global
- `test_nonsticky_orphan_cancels_on_kraken` — exit cancelled
- `test_nonsticky_orphan_closes_position_written_off` — ledger write-off
- `test_nonsticky_orphan_places_fresh_entry` — slot back to cycling
- `test_sticky_orphan_keeps_lottery_ticket` — regression guard
- `test_snapshot_roundtrip_preserves_sticky` — persistence
- `test_toggle_sticky_action` — API flips flag
- `test_auto_release_skips_nonsticky` — release filter

### 7.2 Integration test

Simulated non-sticky lifecycle:
1. Create slot with `sticky=False`
2. Place entry -> fill -> place exit -> timer tick past orphan threshold
3. Verify: exit cancelled, position written off, fresh entry placed, slot cycling again

### 7.3 Manual

Run bot, toggle a slot to non-sticky via dashboard, observe it cycles through stale exits instead of holding them. Verify the slot appears in factory/bauhaus viz normally.

---

## 8. Rollback

Set all slots back to `sticky=True` via dashboard or snapshot edit. Behavior reverts to current sticky mode. Ranger/churner code is still present (just disabled by default) and can be re-enabled if needed.

---

## 9. Future

- Remove ranger/churner code entirely once non-sticky has proven stable (~2 weeks)
- Consider auto-selecting sticky vs non-sticky based on capital utilization or regime
- Throughput sizer could weight non-sticky cycles differently from sticky (if needed)
