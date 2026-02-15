# Regime Observability & Stability — Implementation Spec

Version: v0.3
Date: 2026-02-15
Status: Implementation-ready
Parent: TIER2_SIDE_SUPPRESSION_SPEC, DIRECTIONAL_REGIME_SPEC §6–7, MULTI_TIMEFRAME_HMM_SPEC §4

---

## 1. Problem Statement

After an overnight run with `REGIME_DIRECTIONAL_ENABLED=True`, the bot
placed entries on both sides despite the HMM reading BEARISH at 95%
confidence. Three root causes were identified:

1. **Dashboard blind spot**: `regime_directional` payload is in the API
   response (bot.py:6214) but dashboard.py renders zero fields from it.
   The operator has no visibility into tier status, suppressed side,
   grace countdown, or suppressed slot count.

2. **1m HMM flicker**: With `Source: PRIMARY` (1m-only), the regime
   tier oscillates between 0 and 2 when the 1m HMM briefly reads
   RANGING. Each drop clears `mode_source="regime"` on all slots,
   auto-repair restores both sides, and bootstrap places against-trend
   entries. The 300s dwell gate helps but cannot prevent flickers
   lasting >5 minutes.

3. **Consensus not wired**: The 15m HMM (100% BEARISH) was collecting
   data but not influencing policy. Flipping
   `HMM_MULTI_TIMEFRAME_ENABLED=True` +
   `HMM_MULTI_TIMEFRAME_SOURCE=consensus` was the designed fix but was
   never activated because the operator had no dashboard signal showing
   the instability.

---

## 2. What Gets Built

Five changes, each independently testable:

### 2.1 Dashboard: Regime Directional panel

**File:** `dashboard.py`

**Location:** Immediately after the HMM Regime section (after the
`hmmHints` div, before the "Capacity & Fill Health" heading — currently
between lines 372 and 374).

**HTML structure** (follows existing row/key/value pattern):

```html
<h3 style="margin-top:14px">Directional Regime</h3>
<div class="row"><span class="k">Tier</span><span id="regTier" class="v"></span></div>
<div class="row"><span class="k">Suppressed</span><span id="regSuppressed" class="v"></span></div>
<div class="row"><span class="k">Favored</span><span id="regFavored" class="v"></span></div>
<div class="row"><span class="k">Gates</span><span id="regGates" class="v"></span></div>
<div class="row"><span class="k">Grace</span><span id="regGrace" class="v"></span></div>
<div class="row"><span class="k">Cooldown</span><span id="regCooldown" class="v"></span></div>
<div class="row"><span class="k">Suppressed Slots</span><span id="regSuppressedSlots" class="v"></span></div>
<div class="row"><span class="k">Dwell</span><span id="regDwell" class="v"></span></div>
<div class="row"><span class="k">Last Eval</span><span id="regLastEval" class="v"></span></div>
<div id="regHints" class="tiny"></div>
<div id="regTransitions" class="tiny"></div>
```

**JavaScript rendering** (follows the HMM panel pattern at lines
1231–1356):

```javascript
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

// Tier display with color
const tierColors = { 0: '#888', 1: '#f5a623', 2: '#e74c3c' };
const tierBadge = regEnabled
  ? `<span style="color:${tierColors[regTier] || '#888'}">`
    + `${regTier} — ${regLabel}</span>`
  : '<span style="color:#888">OFF</span>';
document.getElementById('regTier').innerHTML = tierBadge;

// Suppressed/favored side
const sideLabel = { A: 'A (short)', B: 'B (long)' };
document.getElementById('regSuppressed').textContent =
  regEnabled && regSuppressed ? sideLabel[regSuppressed] || regSuppressed : '—';
document.getElementById('regFavored').textContent =
  regEnabled && regFavored ? sideLabel[regFavored] || regFavored : '—';

// Directional gates
const gateT1 = regOkT1 ? '✓' : '✗';
const gateT2 = regOkT2 ? '✓' : '✗';
document.getElementById('regGates').innerHTML =
  regEnabled
    ? (`T1:${gateT1} T2:${gateT2}` + (regReady ? '' : ' <span style="color:#e74c3c">(HMM not ready)</span>'))
    : '—';

// Grace countdown
document.getElementById('regGrace').textContent =
  regTier === 2
    ? (regGraceSec > 0 ? fmt(regGraceSec, 0) + 's remaining' : 'elapsed')
    : '—';

// Cooldown countdown (tier-2 re-entry lockout)
document.getElementById('regCooldown').textContent =
  regEnabled && regCooldownSec > 0
    ? `${fmt(regCooldownSec, 0)}s remaining`
      + (regCooldownSuppressed ? ` (${sideLabel[regCooldownSuppressed] || regCooldownSuppressed})` : '')
    : '—';

// Suppressed slot count
document.getElementById('regSuppressedSlots').textContent =
  regEnabled ? String(regSuppressedSlots) : '—';

// Dwell time
document.getElementById('regDwell').textContent =
  regEnabled ? fmtAgeSeconds(regDwellSec) : '—';

// Last eval
document.getElementById('regLastEval').textContent =
  regEnabled && regLastEval > 0
    ? fmt((nowSec - regLastEval), 0) + 's ago'
    : '—';

// Hints
const regHints = [];
if (!regEnabled) regHints.push('actuation:off');
if (!regReady) regHints.push('hmm_not_ready');
if (regTier === 2 && regGraceSec > 0) regHints.push('grace_pending');
if (regCooldownSec > 0) regHints.push(`cooldown_active:${fmt(regCooldownSec, 0)}s`);
if (regTier === 0 && regEnabled && regReady)
  regHints.push('confidence_below_threshold');
if (regReason) regHints.push(regReason);
document.getElementById('regHints').textContent =
  regHints.length ? 'Hints: ' + regHints.join(' | ') : '';
```

**Payload additions** (in `_regime_status_payload()`):

```python
cooldown_sec = max(0.0, float(
    getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)
))
cooldown_remaining = 0.0
cooldown_side = None
if (
    cooldown_sec > 0
    and int(self._regime_tier) < 2
    and self._regime_tier2_last_downgrade_at > 0
):
    elapsed = max(0.0, now_ts - self._regime_tier2_last_downgrade_at)
    if elapsed < cooldown_sec:
        cooldown_remaining = cooldown_sec - elapsed
        if self._regime_cooldown_suppressed_side in ("A", "B"):
            cooldown_side = self._regime_cooldown_suppressed_side

"cooldown_remaining_sec": float(cooldown_remaining),
"cooldown_suppressed_side": cooldown_side,
```

**Display rules:**

| Tier | Color | Label |
|------|-------|-------|
| 0 | gray (#888) | `0 — symmetric` |
| 1 | amber (#f5a623) | `1 — biased` |
| 2 | red (#e74c3c) | `2 — directional` |

When `actuation_enabled` is False, Tier shows "OFF" and all other rows
show `—`.

### 2.2 Flicker guard: tier-down cooldown

**File:** `bot.py`

**Problem:** When the 1m HMM flickers to RANGING for >300s (dwell
gate), the tier drops from 2→0. If it flickers back to BEARISH 30s
later, the tier jumps to 2 again — starting a new 60s grace period.
During that grace window, bootstrap places both sides. This
drop-recover cycle can repeat all night.

**Solution:** Add a cooldown timer that prevents re-promotion to
tier 2 within N seconds of the last downgrade FROM tier 2. This is
distinct from the existing dwell gate (which prevents ANY tier change
within N seconds of the last change).

**New config:**

```python
# config.py — add after REGIME_SUPPRESSION_GRACE_SEC
REGIME_TIER2_REENTRY_COOLDOWN_SEC: float = _env(
    "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0, float
)
```

Default: 600 seconds (10 minutes). After dropping from tier 2, the
bot must wait 10 minutes before tier 2 can activate again, even if
confidence and bias fully satisfy the thresholds.

**New runtime state:**

```python
self._regime_tier2_last_downgrade_at: float = 0.0
self._regime_cooldown_suppressed_side: str | None = None
```

**Implementation** (in `_update_regime_tier()`, after target_tier is
computed but before the `if changed:` block):

```python
# Tier 2 re-entry cooldown: prevent rapid 2→0→2 oscillation
if target_tier == 2 and current_tier < 2:
    cooldown_sec = max(0.0, float(
        getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)
    ))
    if cooldown_sec > 0 and self._regime_tier2_last_downgrade_at > 0:
        since_downgrade = now - self._regime_tier2_last_downgrade_at
        if since_downgrade < cooldown_sec:
            target_tier = min(1, target_tier) if directional_ok_tier1 else 0
```

And in the `if changed:` block, record downgrades:

```python
if changed and current_tier == 2 and int(target_tier) < 2:
    self._regime_tier2_last_downgrade_at = now
```

**Persistence:** Add `regime_tier2_last_downgrade_at` and
`regime_cooldown_suppressed_side` to save/load alongside the other
regime fields.

**Effect:** After a flicker drops the tier, the bot stays at tier 0
or 1 for 10 minutes. During this time, Tier 1 spacing bias still
applies (asymmetric entry_pct). Sections §2.4–§2.5 preserve the last
suppressed side during that window so against-trend bootstrap entries
do not resume immediately.

### 2.3 Dashboard: tier transition log

**File:** `bot.py`, `dashboard.py`

**Problem:** The operator cannot see WHEN tier transitions happened.
The Supabase `regime_tier_transitions` table logs them, but the
dashboard doesn't show recent history.

**Implementation:**

Add a rolling in-memory buffer of recent transitions (last 20):

```python
# bot.py — new runtime state
self._regime_tier_history: list[dict] = []  # max 20 entries

# In _update_regime_tier(), when changed:
if changed:
    self._regime_tier_history.append({
        "time": now,
        "from_tier": current_tier,
        "to_tier": int(target_tier),
        "regime": regime,
        "confidence": round(confidence, 3),
        "bias": round(bias, 3),
        "reason": reason,
    })
    if len(self._regime_tier_history) > 20:
        self._regime_tier_history = self._regime_tier_history[-20:]
```

Add to status payload (in `_regime_status_payload()`):

```python
"tier_history": list(self._regime_tier_history),
```

Dashboard rendering (below the Directional Regime rows):

```javascript
const regHistory = reg.tier_history || [];
const regTransitionsEl = document.getElementById('regTransitions');
if (regHistory.length > 0) {
  const lines = regHistory.slice(-5).reverse().map(h => {
    const ago = fmt(nowSec - h.time, 0);
    return `${h.from_tier}→${h.to_tier} ${ago}s ago `
      + `(${h.regime} ${fmt(h.confidence*100,0)}%)`;
  });
  regTransitionsEl.textContent = 'Transitions: ' + lines.join(' | ');
} else {
  regTransitionsEl.textContent = '';
}
```

This lets the operator see at a glance whether the tier is stable
or oscillating. With cooldown enabled, instability appears as repeated
`2→0` events plus noisy `0↔1` churn. That is a signal to enable
consensus.

### 2.4 Suppress-side persistence across tier flicker

**File:** `bot.py`

**Problem:** When tier drops from 2→0, the downgrade cleanup
(bot.py:2367-2377) clears `mode_source="regime"` on all slots. This
is correct — auto-repair should be able to restore sides when tier
drops. But during the cooldown window after a flicker downgrade, the
cleared slots can get both-side entries before tier 2 is allowed to
re-activate.

**Solution:** During the re-entry cooldown period (§2.2), keep
`mode_source="regime"` on suppressed slots. Only clear regime
ownership when the cooldown expires (i.e., a sustained tier drop).

**Implementation:** Modify the tier-downgrade cleanup block:

```python
# When tier drops from 2 to lower:
if changed and current_tier == 2 and int(target_tier) < 2:
    self._regime_tier2_last_downgrade_at = now
    self._regime_cooldown_suppressed_side = (
        self._regime_side_suppressed if self._regime_side_suppressed in ("A", "B") else None
    )

    cooldown_sec = max(0.0, float(
        getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)
    ))

    if cooldown_sec <= 0:
        # No cooldown — clear immediately (original behavior)
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            if str(getattr(st, "mode_source", "none")) == "regime":
                self.slots[sid].state = replace(st, mode_source="none")
                logger.info(
                    "slot %s: cleared regime suppression (tier %d -> %d)",
                    sid, current_tier, int(target_tier),
                )
        self._regime_cooldown_suppressed_side = None
    else:
        # Cooldown active — defer clearing.
        # mode_source stays "regime", which blocks auto-repair from
        # restoring the suppressed side during the cooldown window.
        # A background task clears after cooldown expires.
        logger.info(
            "tier %d -> %d: deferring regime clear for %.0fs cooldown",
            current_tier, int(target_tier), cooldown_sec,
        )
```

Add a check in the main loop (after `_apply_tier2_suppression`):

```python
# Clear deferred regime ownership after cooldown expires
self._clear_expired_regime_cooldown(loop_now)
```

```python
def _clear_expired_regime_cooldown(self, now: float) -> None:
    if self._regime_tier == 2:
        self._regime_tier2_last_downgrade_at = 0.0
        self._regime_cooldown_suppressed_side = None
        return  # Tier is active again; normal tier-2 logic owns suppression
    if self._regime_tier2_last_downgrade_at <= 0:
        return
    cooldown_sec = max(0.0, float(
        getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)
    ))
    if cooldown_sec <= 0:
        return
    elapsed = now - self._regime_tier2_last_downgrade_at
    if elapsed < cooldown_sec:
        return
    # Cooldown expired without tier returning to 2 — clear regime ownership
    cleared = 0
    for sid in sorted(self.slots.keys()):
        st = self.slots[sid].state
        if str(getattr(st, "mode_source", "none")) == "regime":
            self.slots[sid].state = replace(st, mode_source="none")
            cleared += 1
    if cleared:
        logger.info(
            "cooldown expired (%.0fs): cleared regime ownership on %d slots",
            elapsed, cleared,
        )
    self._regime_tier2_last_downgrade_at = 0.0
    self._regime_cooldown_suppressed_side = None
```

**Effect:** During the cooldown window after a 2→0 downgrade, suppressed
slots stay one-sided the entire time. No new against-trend entries are
placed.

If the drop is sustained (>10 min), the cooldown expires, regime
ownership is cleared, and auto-repair restores both sides — this is
the correct behavior for a genuine regime change.

### 2.5 Bootstrap respects cooldown suppression

**File:** `bot.py`, `_ensure_slot_bootstrapped()`

**Problem:** Even with §2.4 preserving `mode_source="regime"` during
cooldown, auto-repair already checks `mode_source=="regime"` and
skips. However, bootstrap for newly-recycled slots (returning to S0
after round-trip completion) does NOT check mode_source — it only
checks the tier at lines 3455–3462. If tier is 0 during cooldown,
bootstrap places both sides on new S0 slots.

**Solution:** Add cooldown awareness to the bootstrap regime check:

```python
# In _ensure_slot_bootstrapped(), replace existing suppressed check:
now_ts = _now()
suppressed = None
cooldown_suppressed = (
    self._regime_cooldown_suppressed_side
    if self._regime_cooldown_suppressed_side in ("A", "B")
    else None
)
if bool(getattr(config, "REGIME_DIRECTIONAL_ENABLED", False)):
    if int(self._regime_tier) == 2 and self._regime_grace_elapsed(now_ts):
        if self._regime_side_suppressed in ("A", "B"):
            suppressed = self._regime_side_suppressed
    elif (
        self._regime_tier2_last_downgrade_at > 0
        and cooldown_suppressed in ("A", "B")
    ):
        cooldown_sec = max(0.0, float(
            getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)
        ))
        elapsed = now_ts - self._regime_tier2_last_downgrade_at
        if elapsed < cooldown_sec:
            suppressed = cooldown_suppressed
```

**Effect:** During cooldown, bootstrap respects the last known
suppressed side. New slots only place the favored side, matching
the behavior of slots that were already suppressed before the
flicker.

---

## 3. Config Changes Summary

| Parameter | Default | Description |
|-----------|---------|-------------|
| `REGIME_TIER2_REENTRY_COOLDOWN_SEC` | 600.0 | Min seconds before tier 2 can re-activate after a downgrade |

No other config changes. Existing `REGIME_SUPPRESSION_GRACE_SEC`,
`REGIME_MIN_DWELL_SEC`, and `REGIME_HYSTERESIS` remain unchanged.

---

## 4. Persistence Changes

**save_state** — add:

```python
"regime_tier2_last_downgrade_at": float(self._regime_tier2_last_downgrade_at),
"regime_cooldown_suppressed_side": self._regime_cooldown_suppressed_side,
"regime_tier_history": list(self._regime_tier_history[-20:]),
```

**load_state** — add:

```python
self._regime_tier2_last_downgrade_at = float(
    snap.get("regime_tier2_last_downgrade_at", 0.0) or 0.0
)
raw_cooldown_side = snap.get("regime_cooldown_suppressed_side", None)
self._regime_cooldown_suppressed_side = raw_cooldown_side if raw_cooldown_side in ("A", "B", None) else None
raw_history = snap.get("regime_tier_history", [])
if isinstance(raw_history, list):
    self._regime_tier_history = raw_history[-20:]
```

---

## 5. Main Loop Wiring

After the existing suppression call:

```python
self._update_regime_tier(loop_now)
self._apply_tier2_suppression(loop_now)
self._clear_expired_regime_cooldown(loop_now)  # NEW
```

---

## 6. Tests

**Test location:** Add these to `tests/test_hardening_regressions.py`
or create `tests/test_regime_observability_stability.py` if you want
this feature isolated.

### 6.1 Re-entry cooldown blocks rapid re-promotion

```python
def test_tier2_reentry_cooldown_blocks_repromotion():
    # Set tier=2, BEARISH 95%, grace elapsed
    # Transition tier to 0 (HMM flickers to RANGING)
    # Assert: _regime_tier2_last_downgrade_at is set
    # Immediately re-evaluate with BEARISH 95%
    # Assert: tier stays at 0 or 1 (cooldown blocks tier 2)
    # Advance time past cooldown
    # Re-evaluate with BEARISH 95%
    # Assert: tier reaches 2
```

### 6.2 Cooldown preserves regime ownership

```python
def test_cooldown_preserves_mode_source_regime():
    # Set tier=2, slots with mode_source="regime"
    # Transition tier to 0 with cooldown > 0
    # Assert: slots still have mode_source="regime"
    # Advance past cooldown
    # Call _clear_expired_regime_cooldown()
    # Assert: mode_source cleared to "none"
```

### 6.3 Cooldown metadata resets on expiry

```python
def test_cooldown_resets_metadata_on_expiry():
    # Set tier=0, cooldown timestamp in the past, cooldown side set
    # Ensure there are zero slots with mode_source="regime"
    # Call _clear_expired_regime_cooldown()
    # Assert: _regime_tier2_last_downgrade_at reset to 0.0
    # Assert: _regime_cooldown_suppressed_side reset to None
```

### 6.4 Bootstrap respects cooldown suppression

```python
def test_bootstrap_respects_cooldown_suppression():
    # Set tier=0 but within cooldown window
    # _regime_cooldown_suppressed_side="B", _regime_tier2_last_downgrade_at=recent
    # Call _ensure_slot_bootstrapped()
    # Assert: only A-side entry placed (suppressed="B" still honored)
```

### 6.5 Tier history buffer

```python
def test_tier_history_buffer_max_20():
    # Trigger 25 tier transitions
    # Assert: _regime_tier_history has exactly 20 entries
    # Assert: oldest 5 were dropped
```

### 6.6 Dashboard payload includes all regime fields

```python
def test_regime_status_payload_complete():
    # Set up tier=2 state with suppression active
    # Call _regime_status_payload()
    # Assert: all expected keys present
    # Assert: tier_history included
    # Assert: grace_remaining_sec is correct
    # Assert: cooldown_remaining_sec and cooldown_suppressed_side are correct
```

---

## 7. Invariants

1. Re-entry cooldown NEVER overrides a genuine regime change. If the
   HMM stays non-directional past the cooldown, all regime ownership
   is cleared and auto-repair restores both sides.

2. Cooldown only affects tier 2 re-entry. Tier 0↔1 transitions are
   unaffected.

3. `mode_source="regime"` during cooldown is indistinguishable from
   active tier 2 suppression to auto-repair — it won't restore the
   suppressed side in either case.

4. Tier 2 grace is unchanged. After cooldown expires and tier 2 becomes
   eligible again, normal `REGIME_SUPPRESSION_GRACE_SEC` timing applies.

5. The dashboard panel is purely observational. Removing it has zero
   effect on bot behavior.

6. Tier history is in-memory only (plus state.json). It does not
   accumulate unboundedly — capped at 20 entries.

7. Exits are NEVER affected by cooldown logic. Only entries and
   bootstrap are gated.

8. If `REGIME_TIER2_REENTRY_COOLDOWN_SEC=0`, behavior is identical
   to pre-spec (immediate clear on downgrade, no cooldown).

---

## 8. Operator Checklist (Immediate)

After this spec is implemented, the operator should:

1. **Verify dashboard** shows "Directional Regime" panel with tier,
   suppressed side, gates, grace, and suppressed slot count.

2. **Enable multi-timeframe consensus** for maximum stability:
   ```
   HMM_MULTI_TIMEFRAME_ENABLED=True
   HMM_MULTI_TIMEFRAME_SOURCE=consensus
   ```

3. **Monitor tier history** in the dashboard. If transitions are
   still frequent (>3 per hour), increase
   `REGIME_TIER2_REENTRY_COOLDOWN_SEC` to 1800 (30 min).

4. **Verify suppressed slot count** is >0 during Tier 2. If it
   shows 0, the suppression actuator isn't firing — check the
   grace timer and `actuation_enabled` flag.

---

## 9. Files Modified

| File | Changes |
|------|---------|
| `config.py` | `REGIME_TIER2_REENTRY_COOLDOWN_SEC` |
| `bot.py` | `_regime_tier2_last_downgrade_at` + `_regime_cooldown_suppressed_side` state, re-entry cooldown in `_update_regime_tier()`, `_clear_expired_regime_cooldown()`, deferred downgrade cleanup, bootstrap cooldown check, cooldown status payload fields, tier history buffer, persistence |
| `dashboard.py` | Directional Regime HTML panel, JS rendering, cooldown row/hint, tier transition log split into dedicated `regTransitions` line |
| `tests/` | 6 new tests (§6.1–6.6) |
