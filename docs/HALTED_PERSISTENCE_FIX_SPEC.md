# HALTED Persistence Fix Spec

**Version:** 0.1  
**Date:** 2026-02-18  
**Status:** Draft

## Problem

After a normal process shutdown (for example deploy restart / `SIGTERM`), the runtime writes:

- `mode: "HALTED"`
- `pause_reason: "signal 15"` (or `"process exit"`)

On next startup, snapshot restore rehydrates that HALTED state unchanged. Startup then preserves HALTED and never returns to RUNNING.

Result:
1. Bot appears alive (`/api/status` works), but trading loop is effectively disabled.
2. Churner actions can return misleading gate failures while bot is not truly running.
3. Operators get stuck after routine deploy restarts.

## Root Cause

1. `shutdown()` always persists HALTED mode.
2. `_load_snapshot()` restores mode/pause_reason verbatim.
3. `initialize()` only sets `RUNNING` when mode is not `PAUSED`/`HALTED`.

This makes transient shutdown halts sticky across process lifecycles.

## Desired Behavior

1. **Transient halts** from normal shutdown (`signal *`, `process exit`) must auto-clear on startup.
2. **Safety halts** (for example invariant violations) must remain sticky and require operator intervention.

## Proposed Fix

In snapshot restore:
1. Detect `mode == "HALTED"` with transient reason:
   - `pause_reason` starts with `"signal "`
   - or equals `"process exit"`
2. Convert restored mode to `INIT` and clear `pause_reason`.
3. Keep all non-transient HALTED reasons unchanged.

This allows startup to proceed to `RUNNING` while preserving real safety stops.

## Non-Goals

1. No change to hard halt triggers (invariant violations remain HALTED).
2. No change to pause/daily-loss lock behavior.
3. No API contract changes.

## Test Coverage

Add regression tests:
1. Snapshot with `HALTED + signal 15` restores as `INIT` with empty pause reason.
2. Snapshot with `HALTED + invariant violation` remains HALTED.

## Verification

1. Simulate restart from snapshot containing `mode=HALTED, pause_reason=signal 15`.
2. Confirm post-load mode becomes `INIT` and startup transitions to `RUNNING`.
3. Confirm snapshot containing invariant halt still restores HALTED unchanged.
