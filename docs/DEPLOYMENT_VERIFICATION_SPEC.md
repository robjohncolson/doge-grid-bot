# Deployment Verification Spec — Churner Fix + Halted Persistence Fix

**Version:** 0.1
**Date:** 2026-02-18
**Status:** Ready to deploy

---

## Changes Deployed

### 1. Churner Fix: Visibility + Capital-Adaptive Entry (commit `ef0c95f`)

**Problem:** Churner spawns but does nothing — invisible in UI, and USD-starved B-side parents force buy entries that always fail.

**Fix:**
- `_churner_gate_check()` (bot.py:7396-7481): Returns 6-tuple with chosen entry side. Loops preferred → opposite side; first side with free capital wins. Reserve backstop preserved on preferred side.
- Churner tick (bot.py:7985-8010): Unpacks `chosen_side` from gate; `state.entry_side` uses it instead of re-deriving from parent.
- Dashboard (dashboard.py:160, 3686-3697): `.churner-badge` CSS + CHURN badge in state bar when churner active.
- Dashboard (dashboard.py:829): Reserve label changed to "Churner Reserve".
- 5 new tests across `test_self_healing_slots.py` and `test_hardening_regressions.py`.

**Parent spec:** `docs/CHURNER_FIX_SPEC.md` v0.1

### 2. Halted Persistence Fix (commit `dcab1a2`)

**Problem:** SIGTERM (deploy restart) writes `mode=HALTED, pause_reason="signal 15"` to snapshot. On next startup, `_load_snapshot()` rehydrates verbatim. `initialize()` checks `mode not in ("PAUSED", "HALTED")` and skips setting RUNNING. Bot stays permanently halted after routine restarts.

**Fix:**
- `_is_transient_halt_reason()` (bot.py:4102-4107): Detects `"signal *"` and `"process exit"` as transient.
- `_load_snapshot()` (bot.py:4118-4128): If HALTED + transient reason → clear to INIT. Safety halts (invariant violations) remain sticky.
- Startup path: INIT → passes line 5129 check → sets RUNNING.
- 2 new tests in `test_hardening_regressions.py`.

**Parent spec:** `docs/HALTED_PERSISTENCE_FIX_SPEC.md` v0.1

---

## Pre-Deploy Checklist

- [ ] `python -m pytest tests/test_self_healing_slots.py tests/test_hardening_regressions.py -v` — all green
- [ ] Both commits on master: `ef0c95f` (churner) + `dcab1a2` (halted fix)

## Post-Deploy Verification

### Immediate (after restart)

| Check | Expected | How |
|-------|----------|-----|
| Mode | `RUNNING` (not HALTED) | `GET /api/status` → `mode` field |
| Pause reason | empty string | `GET /api/status` → `pause_reason` field |
| Log message | "Snapshot restored transient HALTED state (signal 15); clearing to INIT for startup" | Startup logs |

### When Regime Shifts to RANGING

| Check | Expected | How |
|-------|----------|-----|
| Churner spawn | Succeeds (HTTP 200) | Dashboard → Spawn Churner button |
| CHURN badge | Visible in state bar | Navigate to slot with active churner |
| Entry side | `"sell"` (when USD-starved, DOGE-rich) | `GET /api/status` → `self_healing.churner.states[].entry_side` |
| Churner cycles | `cycles_today > 0` after first round-trip | `GET /api/status` → `self_healing.churner.states[].cycles_today` |
| Reserve label | "Churner Reserve" | Dashboard → churner panel |

### Regression Checks

| Check | Expected |
|-------|----------|
| Safety halt (invariant violation) | Still sticks across restart — NOT auto-cleared |
| Churner in BEARISH/BULLISH regime | Correctly rejected with `regime_not_ranging` |
| Preferred side when both have capital | Preferred side wins (buy for B-parent, sell for A-parent) |

---

## Rollback

If issues arise:
1. `git revert dcab1a2` — removes halted fix
2. `git revert ef0c95f` — removes churner fix
3. Redeploy. Both reverts are independent.

---

## Known Side Effects

- `docs/CLAUDE_AUDIOHOOKS_SPEC.md` was accidentally included in commit `dcab1a2`. Harmless — Warcraft Peon sound hooks spec, unrelated to trading logic.
