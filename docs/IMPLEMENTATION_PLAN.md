# DOGE Grid Bot Stabilization - Implementation Plan

## Goal
Stabilize completion rate and stop orphan-loss bleed by combining immediate runtime tuning with code-level safety rails.

## Scope
- Phase 0: runtime tuning (no code deploy)
- Phase 1: code safety implementation (this repo)
- Phase 2: follow-up hardening (optional)

## Phase 0 - Immediate Runtime Changes (Ops)
1. Run `/reconcile_drift` until open-order drift is near zero.
2. Set `S1_ORPHAN_AFTER_SEC=1350` (or `1200-1500` band).
3. Set `REENTRY_BASE_COOLDOWN_SEC=150` (or `120-180` band).
4. Set dashboard `ENTRY_PCT=0.35` (start point).
5. Set dashboard `PROFIT_PCT=0.80` (start point).
6. Keep reduced slot pressure until orphan backlog is under control.
7. Drain old recoveries with soft-close workflow to reduce backlog.

## Phase 1 - Code Changes (Implemented)
1. Daily loss lock (aggregate, UTC day):
   - compute aggregate realized loss for current UTC day from completed cycles
   - trigger pause when `daily_loss >= DAILY_LOSS_LIMIT`
   - block resume while lock is active
   - auto-clear lock at UTC rollover, manual resume required
   - expose lock fields in `/api/status`
2. Recovery cap enforcement (state machine):
   - enforce `MAX_RECOVERY_SLOTS` per slot
   - deterministic priority eviction when cap would be exceeded:
     furthest from market first, then oldest, then id
   - emit cancel actions for evicted recoveries with txids
3. Runtime plumbing:
   - pass `MAX_RECOVERY_SLOTS` into `EngineConfig`
   - persist/restore daily-loss-lock fields in snapshots
   - align dashboard/telegram resume handlers with tuple return `(ok, message)`
   - add throttled auto-drain (`furthest -> oldest`) to reduce recovery backlog
4. Test coverage:
   - recovery-cap eviction behavior
   - daily-loss lock trigger, block, rollover clear, and status fields
   - dashboard resume failure propagation
   - auto-drain priority + P&L booking behavior

## Phase 2 - Optional Follow-Up
1. Add `EXIT_DRIFT_MAX_PCT` logic in `state_machine.py` for distance-aware orphaning.
2. Add dashboard controls for `S1_ORPHAN_AFTER_SEC` and cooldowns.
3. Add operator-facing backlog triage panel (batch soft-close by distance/age).
4. Tune auto-soft-close capacity threshold if pressure remains high.

## Verification Checklist
1. Unit tests pass.
2. Bot remains paused when daily loss lock is active.
3. Recovery count per slot never exceeds configured cap.
4. `/api/status` exposes:
   - `daily_loss_limit`
   - `daily_realized_loss_utc`
   - `daily_loss_lock_active`
   - `daily_loss_lock_utc_day`

## Rollback
1. Revert code changes in `bot.py`, `state_machine.py`, and tests.
2. Restore previous runtime env values.
3. Restart runtime and re-run `/reconcile_drift`.
