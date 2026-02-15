# Tier 2 Side Suppression — Implementation Spec

Version: v0.1
Date: 2026-02-15
Status: Implementation-ready
Parent: Directional Regime Spec §6.3, §6.4, §7.1, §7.4, §7.5

---

## 1. Scope

This spec covers the **actuation code** for Tier 2 of the directional
regime system. When the HMM (or multi-timeframe consensus) reaches high
confidence in a directional regime, the bot stops placing against-trend
entries entirely. Existing exits are untouched (sticky patience).

Everything described here is gated by `REGIME_DIRECTIONAL_ENABLED=True`
AND `_regime_tier == 2`. When either condition is false, zero behavioral
change occurs.

---

## 2. What Gets Built

Six concrete code changes, each independently testable:

### 2.1 Grace period tracking

**File:** `bot.py`

**Problem:** `REGIME_SUPPRESSION_GRACE_SEC` is defined (default 60s) but
never tracked. Tier 2 should not immediately cancel entries — it should
wait for the grace period to confirm the regime is stable.

**Implementation:**

```python
# New runtime state:
self._regime_tier2_grace_start: float = 0.0  # when tier first hit 2

# In _update_regime_tier(), when tier transitions TO 2:
if changed and target_tier == 2:
    self._regime_tier2_grace_start = now

# Grace elapsed check:
def _regime_grace_elapsed(self, now: float) -> bool:
    if self._regime_tier != 2:
        return False
    grace_sec = max(0.0, float(config.REGIME_SUPPRESSION_GRACE_SEC))
    if grace_sec <= 0:
        return True
    return (now - self._regime_tier2_grace_start) >= grace_sec
```

Suppression actions (§2.2–2.4) only fire when `_regime_grace_elapsed()`
returns True.

### 2.2 Entry cancellation for suppressed side

**File:** `bot.py`

**Problem:** When Tier 2 activates (after grace), against-trend entries
sitting in S0 should be cancelled. Currently nothing cancels them.

**Implementation:** New method `_apply_tier2_suppression()`, called from
main loop after `_update_regime_tier()`:

```python
def _apply_tier2_suppression(self, now: float) -> None:
    if not bool(config.REGIME_DIRECTIONAL_ENABLED):
        return
    if self._regime_tier != 2:
        return
    if not self._regime_grace_elapsed(now):
        return

    suppressed = self._regime_side_suppressed  # "A" or "B"
    if not suppressed:
        return

    suppressed_side = "sell" if suppressed == "A" else "buy"

    for sid in sorted(self.slots.keys()):
        slot = self.slots[sid]
        st = slot.state

        # Skip slots already in the correct mode
        if suppressed == "A" and st.long_only and st.mode_source == "regime":
            continue
        if suppressed == "B" and st.short_only and st.mode_source == "regime":
            continue

        phase = sm.derive_phase(st)

        # Only suppress entries in S0 (entry phase)
        if phase != "S0":
            continue

        # Find suppressed-side entry order
        target_order = None
        for o in st.orders:
            if o.role == "entry" and o.side == suppressed_side:
                target_order = o
                break

        if not target_order:
            continue

        # Cancel the against-trend entry
        if target_order.txid:
            try:
                kraken_client.cancel_order(target_order.txid)
            except Exception as e:
                logger.warning(
                    "slot %s tier2 cancel %s failed: %s",
                    sid, target_order.txid, e,
                )
                continue

        # Remove from state and set mode flags
        new_st = sm.remove_order(st, target_order.local_id)
        if suppressed == "A":
            new_st = replace(new_st, long_only=True, short_only=False,
                           mode_source="regime")
        else:
            new_st = replace(new_st, short_only=True, long_only=False,
                           mode_source="regime")

        self.slots[sid].state = new_st
        logger.info(
            "slot %s: tier2 suppressed %s entry (regime=%s, conf=%.3f)",
            sid, suppressed, self._regime_side_suppressed,
            self._hmm_consensus.get("confidence", 0.0),
        )
```

**Invariants:**
- Only cancels entries, never exits
- Only acts on S0 slots
- Sets `mode_source="regime"` to distinguish from balance degradation
- Idempotent — skips slots already in correct regime mode

### 2.3 Bootstrap respects suppressed side

**File:** `bot.py`, `_ensure_slot_bootstrapped()`

**Problem:** Bootstrap currently places both A and B entries (or degrades
based on balance). It ignores `_regime_side_suppressed`. During Tier 2,
bootstrap should only place the favored side.

**Implementation:** Add regime check at the top of the normal bootstrap
path (line ~3310, before the `if doge >= min_vol and usd >= min_cost`
block):

```python
# After balance checks, before placing entries:
suppressed = self._regime_side_suppressed if (
    bool(config.REGIME_DIRECTIONAL_ENABLED)
    and self._regime_tier == 2
    and self._regime_grace_elapsed(_now())
) else None

if suppressed:
    # Only place favored side
    if suppressed == "A":
        # Suppress sell entry, only place buy
        if usd >= min_cost:
            st = replace(slot.state, long_only=True, short_only=False,
                        mode_source="regime")
            st, a = sm.add_entry_order(st, cfg, side="buy", trade_id="B",
                                       cycle=st.cycle_b,
                                       order_size_usd=self._slot_order_size_usd(slot),
                                       reason="bootstrap_regime_long_only")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "bootstrap_regime")
        else:
            logger.info("slot %s bootstrap waiting: regime suppresses A, "
                       "insufficient USD for B", slot_id)
        return

    elif suppressed == "B":
        # Suppress buy entry, only place sell
        if doge >= min_vol:
            st = replace(slot.state, short_only=True, long_only=False,
                        mode_source="regime")
            st, a = sm.add_entry_order(st, cfg, side="sell", trade_id="A",
                                       cycle=st.cycle_a,
                                       order_size_usd=self._slot_order_size_usd(slot),
                                       reason="bootstrap_regime_short_only")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "bootstrap_regime")
        else:
            logger.info("slot %s bootstrap waiting: regime suppresses B, "
                       "insufficient DOGE for A", slot_id)
        return
```

**Priority order:** balance constraints > regime signal > symmetric.
If favored side isn't fundable, wait (don't fall back to against-trend).

### 2.4 Auto-repair skips regime-suppressed slots

**File:** `bot.py`, `_auto_repair_degraded_slot()`

**Problem:** Auto-repair currently sees `long_only=True` or
`short_only=True` and tries to restore the missing side when funds are
available. It doesn't distinguish regime-driven suppression from balance-
driven degradation.

**Implementation:** Add early return at top of `_auto_repair_degraded_slot()`
(after line 3403):

```python
def _auto_repair_degraded_slot(self, slot_id: int) -> None:
    if self.mode != "RUNNING":
        return

    slot = self.slots[slot_id]
    st = slot.state
    if not (st.long_only or st.short_only):
        return

    # NEW: Don't auto-repair regime-driven suppression.
    # The regime tier system controls when suppression lifts.
    if str(getattr(st, "mode_source", "none")) == "regime":
        return

    # ... rest of existing auto-repair logic unchanged ...
```

This is the simplest and most critical change. Without it, auto-repair
would immediately undo the regime suppression on the next loop cycle.

### 2.5 Deferred entry purge

**File:** `bot.py`, `_drain_pending_entry_orders()`

**Problem:** When Tier 2 activates, there may be deferred entries
(orders with no txid, waiting to drain) for the suppressed side. These
would eventually be placed, defeating suppression.

**Implementation:** Filter suppressed-side entries in
`_drain_pending_entry_orders()`:

```python
# In _drain_pending_entry_orders(), after collecting pending list:

# Purge suppressed-side deferred entries during Tier 2
if (bool(config.REGIME_DIRECTIONAL_ENABLED)
    and self._regime_tier == 2
    and self._regime_grace_elapsed(_now())):
    suppressed = self._regime_side_suppressed
    if suppressed:
        suppressed_side = "sell" if suppressed == "A" else "buy"
        purged = []
        kept = []
        for sid, o in pending:
            if o.side == suppressed_side:
                # Remove from slot state
                self.slots[sid].state = sm.remove_order(
                    self.slots[sid].state, o.local_id
                )
                purged.append((sid, o))
            else:
                kept.append((sid, o))
        if purged:
            logger.info(
                "entry_scheduler: purged %d suppressed-side (%s) "
                "deferred entries", len(purged), suppressed,
            )
        pending = kept
```

### 2.6 Tier downgrade: clear regime ownership

**File:** `bot.py`, `_update_regime_tier()`

**Problem:** When tier drops from 2 to 1 or 0, regime-suppressed slots
need `mode_source` cleared so auto-repair can restore them.

**Implementation:** Add to `_update_regime_tier()`, after the tier change
is applied (after line ~2316):

```python
# When tier drops from 2 to lower: clear regime ownership on all slots
if changed and current_tier == 2 and target_tier < 2:
    for sid in sorted(self.slots.keys()):
        st = self.slots[sid].state
        if str(getattr(st, "mode_source", "none")) == "regime":
            self.slots[sid].state = replace(
                st, mode_source="none"
            )
            logger.info(
                "slot %s: cleared regime suppression (tier %d → %d)",
                sid, current_tier, target_tier,
            )
    # Reset grace tracking
    self._regime_tier2_grace_start = 0.0
```

After clearing `mode_source="none"`, the existing `_auto_repair_degraded_slot()`
will see `long_only=True` with `mode_source="none"`, check balance, and
restore the missing side on the next loop cycle. No additional code needed.

**Regime flip** (e.g., BULLISH → BEARISH while at Tier 2):

This is handled naturally. `_update_regime_tier()` already updates
`_regime_side_suppressed` based on bias direction. On the next
`_apply_tier2_suppression()` call:
1. Slots with old suppression (`mode_source="regime"`, wrong side) will
   have `mode_source` cleared by §2.6 (tier briefly transitions)
2. OR if tier stays at 2 but side flips: `_apply_tier2_suppression()`
   needs to also clear old-side regime slots and suppress new side.

Add to `_apply_tier2_suppression()`:

```python
# Clear regime ownership on slots suppressing the WRONG side
# (handles regime flip while staying at Tier 2)
for sid in sorted(self.slots.keys()):
    st = self.slots[sid].state
    if str(getattr(st, "mode_source", "none")) != "regime":
        continue
    if suppressed == "A" and st.short_only:
        # Was suppressing B, now should suppress A — clear old
        self.slots[sid].state = replace(st, mode_source="none")
    elif suppressed == "B" and st.long_only:
        # Was suppressing A, now should suppress B — clear old
        self.slots[sid].state = replace(st, mode_source="none")
```

Auto-repair picks up the cleared slots and restores the newly-favored
side on the next cycle.

---

## 3. Main Loop Integration

Add `_apply_tier2_suppression()` call in the main loop, right after
`_update_regime_tier()`:

```python
# In main loop (after existing _update_regime_tier call):
self._update_regime_tier(now)
self._apply_tier2_suppression(now)  # NEW
```

Order matters: tier must be evaluated before suppression is applied.

---

## 4. Status Payload

The existing `regime_directional` status block already has
`grace_remaining_sec: 0.0` as a placeholder. Populate it:

```python
"grace_remaining_sec": max(
    0.0,
    float(config.REGIME_SUPPRESSION_GRACE_SEC)
    - (now - self._regime_tier2_grace_start)
) if self._regime_tier == 2 else 0.0,
```

Also add count of regime-suppressed slots:

```python
"regime_suppressed_slots": sum(
    1 for s in self.slots.values()
    if str(getattr(s.state, "mode_source", "none")) == "regime"
),
```

---

## 5. Tests

### 5.1 Auto-repair skips regime slots

```python
def test_auto_repair_skips_regime_mode_source():
    # Set slot to long_only with mode_source="regime"
    # Call _auto_repair_degraded_slot()
    # Assert: no new orders placed, slot unchanged
```

### 5.2 Auto-repair restores after regime cleared

```python
def test_auto_repair_restores_when_mode_source_cleared():
    # Set slot to long_only with mode_source="none", sufficient balance
    # Call _auto_repair_degraded_slot()
    # Assert: missing sell entry is placed
```

### 5.3 Grace period delays suppression

```python
def test_tier2_grace_period_delays_cancellation():
    # Set tier=2 with grace_start=now (grace not elapsed)
    # Call _apply_tier2_suppression()
    # Assert: no entries cancelled
    # Advance time past REGIME_SUPPRESSION_GRACE_SEC
    # Call _apply_tier2_suppression()
    # Assert: suppressed entry cancelled
```

### 5.4 Bootstrap respects suppression

```python
def test_bootstrap_only_places_favored_side_during_tier2():
    # Set tier=2, suppressed="A", grace elapsed
    # Call _ensure_slot_bootstrapped()
    # Assert: only B entry placed, mode_source="regime"
```

### 5.5 Deferred entry purge

```python
def test_deferred_entries_purged_for_suppressed_side():
    # Create slot with unbound sell entry (no txid) in deferred queue
    # Set tier=2, suppressed="A"
    # Call _drain_pending_entry_orders()
    # Assert: sell entry removed from state
```

### 5.6 Tier downgrade clears regime ownership

```python
def test_tier_downgrade_clears_mode_source_regime():
    # Set tier=2, several slots with mode_source="regime"
    # Transition tier to 1
    # Assert: all slots have mode_source="none"
```

### 5.7 Regime flip rotates suppressed side

```python
def test_regime_flip_clears_old_suppression():
    # Set tier=2, suppressed="A" (BULLISH), slots regime-suppressed
    # Flip to BEARISH (suppressed="B")
    # Call _apply_tier2_suppression()
    # Assert: old A-suppressed slots cleared, B entries now suppressed
```

---

## 6. Invariants

1. Exits are NEVER cancelled by Tier 2 logic.
2. Suppression only acts on S0 slots (entry phase).
3. `mode_source="regime"` is only set when `REGIME_DIRECTIONAL_ENABLED`
   is True AND `_regime_tier == 2`.
4. Grace period must elapse before any cancellation occurs.
5. Auto-repair never restores a side while `mode_source="regime"`.
6. Tier downgrade always clears `mode_source="regime"` on all slots.
7. Balance constraints still win — if favored side isn't fundable,
   bootstrap waits (doesn't fall back to against-trend).
8. `_apply_tier2_suppression()` is idempotent — calling it repeatedly
   on an already-suppressed slot is a no-op.

---

## 7. Files Modified

| File | Changes |
|------|---------|
| `bot.py` | `_regime_tier2_grace_start` state, `_regime_grace_elapsed()`, `_apply_tier2_suppression()`, bootstrap regime check, auto-repair early return, deferred entry purge, tier downgrade cleanup, status payload |
| `state_machine.py` | No changes (mode_source already exists) |
| `config.py` | No changes (REGIME_SUPPRESSION_GRACE_SEC already defined) |
| `dashboard.py` | Populate grace_remaining_sec, show regime_suppressed_slots count |
| `tests/` | 7 new tests (§5.1–5.7) |
