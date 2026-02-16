# Slot Bootstrap Fix Spec v0.1.0

**Status**: IMPLEMENTED
**Date**: 2026-02-15
**Files modified**: `bot.py`, `tests/test_hardening_regressions.py`

---

## Problem

Adding a new slot no longer creates both A & B entry orders. The last slot remains idle/one-sided indefinitely.

**Observed symptoms** (slot 16 "plz"):
- A-sell entry placed successfully (txid present)
- B-buy entry either deferred indefinitely (`txid=""`) or removed entirely
- Slot stuck in `short_only` mode with `mode_source="regime"`
- `entry_scheduler.deferred_total` climbing (597), `pending_entries: 1`
- Slot never auto-repairs despite regime returning to Tier 0

**Prior behavior**: Both A & B entries placed immediately on slot creation.

**Trigger**: Interaction between entry scheduler throttling and regime directional suppression, both added after the original slot bootstrap logic.

---

## Root Causes

### Bug 1: Regime suppression lock-in

**Location**: `bot.py`, `_auto_repair_degraded_slot()`, line ~3821

**Before**:
```python
if str(getattr(st, "mode_source", "none")) == "regime":
    return  # Always skip — permanent lock-in
```

**Sequence**:
1. New slot bootstrapped during Tier 2 regime (e.g., BULLISH suppresses A-side)
2. `_ensure_slot_bootstrapped()` creates only B-buy entry, sets `short_only=True, mode_source="regime"`
3. Regime drops to Tier 0 (no suppression active)
4. `_auto_repair_degraded_slot()` sees `mode_source="regime"` → **unconditionally returns**
5. Missing A-sell entry is never created
6. Slot is permanently one-sided

**Severity**: High — slot can never recover without manual intervention or bot restart.

### Bug 2: Entry scheduler throttles bootstrap

**Location**: `bot.py`, `_execute_actions()`, line ~4074

**Before**:
```python
if action.role == "entry" and self.entry_adds_per_loop_used >= self.entry_adds_per_loop_cap:
    self._defer_entry_due_scheduler(slot_id, action, source)
    continue  # All entries throttled equally
```

**Sequence**:
1. `_ensure_slot_bootstrapped()` creates both A and B entries, passes both to `_execute_actions()`
2. A-sell entry placed → `entry_adds_per_loop_used` increments to 1
3. B-buy entry hits scheduler gate (`used >= cap`) → **deferred**
4. B entry sits with `txid=""`, competing with organic entries from other slots each loop
5. With `cap_per_loop=1` and 17 active slots generating fill-driven entries, drain rarely has budget

**Severity**: Medium — B entry is eventually placed when drain wins a budget slot, but can take minutes to hours.

---

## Fixes

### Fix 1: Conditional regime repair

**Location**: `bot.py`, `_auto_repair_degraded_slot()`

When `mode_source="regime"`, check whether suppression is still actually active before skipping:

```python
if str(getattr(st, "mode_source", "none")) == "regime":
    still_suppressed = False
    if bool(getattr(config, "REGIME_DIRECTIONAL_ENABLED", False)):
        now_ts = _now()
        if int(self._regime_tier) == 2 and self._regime_grace_elapsed(now_ts):
            if self._regime_side_suppressed in ("A", "B"):
                still_suppressed = True
        elif self._regime_tier2_last_downgrade_at > 0:
            cd_side = self._regime_cooldown_suppressed_side
            if cd_side in ("A", "B"):
                cd_sec = float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0))
                if now_ts - self._regime_tier2_last_downgrade_at < cd_sec:
                    still_suppressed = True
    if still_suppressed:
        return
    # Otherwise fall through → repair adds missing side
```

**Behavior**:
| Condition | Result |
|-----------|--------|
| Tier 2 active + grace elapsed + side suppressed | Skip repair (respect regime) |
| Tier 2 downgraded + cooldown active | Skip repair (respect cooldown) |
| Tier 0 or cooldown expired | **Allow repair** (add missing side) |
| `REGIME_DIRECTIONAL_ENABLED=False` | **Allow repair** (regime system off) |

### Fix 2: Bootstrap bypasses entry scheduler cap

**Location**: `bot.py`, `_execute_actions()`

Bootstrap and auto-repair entries skip the scheduler throttle so both sides of a slot are placed atomically:

```python
if action.role == "entry" and self.entry_adds_per_loop_used >= self.entry_adds_per_loop_cap:
    if source not in ("bootstrap", "bootstrap_regime", "auto_repair"):
        self._defer_entry_due_scheduler(slot_id, action, source)
        continue
    # Bootstrap/repair entries bypass cap — both sides placed together
```

**Exempt sources**:
- `"bootstrap"` — normal S0 two-sided bootstrap
- `"bootstrap_regime"` — regime-suppressed one-sided bootstrap
- `"auto_repair"` — adding missing side to degraded slot

**Rate limit safety**: Bootstrap events are rare (slot add or slot drain). At most 2 extra orders per event. No risk of exceeding Kraken rate limits.

---

## Test Changes

**File**: `tests/test_hardening_regressions.py`

| Old test | New tests |
|----------|-----------|
| `test_auto_repair_skips_regime_mode_source` | Split into two: |
| | `test_auto_repair_skips_regime_mode_source_while_suppressed` — Tier 2 active, repair blocked |
| | `test_auto_repair_restores_regime_slot_when_suppression_lapsed` — Tier 0, repair proceeds |

---

## Interaction Matrix

| Scenario | Before fix | After fix |
|----------|-----------|-----------|
| Add slot during Tier 0 | Both entries placed (if cap allows) | Both entries placed (bypass cap) |
| Add slot during Tier 2 | One side placed, other deferred/stuck | One side placed (regime); missing side added when regime drops |
| Add slot, cap=1 | A placed, B deferred for hours | Both placed atomically (bypass) |
| Regime Tier 2 → Tier 0 with degraded slot | Slot stays one-sided forever | Auto-repair adds missing side |
| Regime Tier 2 → cooldown with degraded slot | Slot stays one-sided forever | Waits for cooldown, then repairs |

---

## Config Dependencies

| Config | Default | Role |
|--------|---------|------|
| `REGIME_DIRECTIONAL_ENABLED` | `False` | Must be `True` for suppression checks |
| `REGIME_SUPPRESSION_GRACE_SEC` | `60.0` | Grace period before Tier 2 suppression activates |
| `REGIME_TIER2_REENTRY_COOLDOWN_SEC` | `600.0` | Cooldown after Tier 2 downgrade |
| `MAX_ENTRY_ADDS_PER_LOOP` | `2` | Scheduler cap (bootstrap now exempt) |

---

## Rollback

Revert two changes:
1. `_auto_repair_degraded_slot`: restore unconditional `return` on `mode_source="regime"`
2. `_execute_actions`: remove source exemption from scheduler gate

No state migration needed — degraded slots will simply remain one-sided until manually removed/re-added.
