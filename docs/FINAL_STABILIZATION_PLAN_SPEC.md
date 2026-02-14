# DOGE Runtime Stabilization Final Spec

Version: v1.1  
Date: 2026-02-14  
Status: Implementation-ready  
Owner: Runtime + Dashboard

---

## 1. Objective

Stop orphan-driven loss bleed, add real safety rails, and provide an operator workflow that can tune risk quickly without guessing.

Target outcome:

1. Reduce orphan creation rate materially.
2. Prevent unlimited recovery buildup.
3. Enforce a real daily loss stop in the active runtime.
4. Keep operations simple (dashboard-first where possible).

---

## 2. Baseline Problem Statement

Observed production symptoms:

1. Fill latency median is often above current S1 orphan timeout.
2. S1 exits are orphaned prematurely and recycled into recovery churn.
3. Recovery backlog consumes order capacity and amplifies realized losses.
4. Daily loss protection is not enforced in the active `bot.py` loop.
5. Some config knobs are declared but not wired in current runtime path.

Primary technical mismatch:

1. `S1_ORPHAN_AFTER_SEC=600` (default) is too aggressive for observed fill behavior.

---

## 3. Scope

### In scope

1. Runtime stabilization tuning profile.
2. Daily loss enforcement in active runtime loop.
3. Recovery cap enforcement in active state-machine path.
4. Operator-safe dashboard controls for stabilization actions.

### Out of scope

1. Exchange migration / fee-tier strategy changes.
2. Full strategy redesign or new alpha model.
3. Multi-pair architecture changes.

---

## 4. Final Plan (Phased)

## Phase 0: Immediate Operational Tuning (No Code)

Apply this sequence first, immediately, before any code deployment:

1. Run `reconcile_drift` until `open_orders_drift` is near zero.
2. Drain existing recovery backlog using `cancel_stale_recoveries` in batches until `total_orphans < 30`.
3. Set `S1_ORPHAN_AFTER_SEC` to `1350` (initial target band: `1200-1500`).
4. Keep `S2_ORPHAN_AFTER_SEC` at `1800` (maintain S1 < S2 escalation).
5. Set `REENTRY_BASE_COOLDOWN_SEC` to `150` (target band: `120-180`).
6. Set `PAIR_ENTRY_PCT` to `0.35` (target band: `0.30-0.40`).
7. Set `PAIR_PROFIT_PCT` to `0.80` (target band: `0.75-0.85`).
8. Keep layers at `0` while stabilization is in progress.

Notes:

1. Environment-variable changes are restart/redeploy scoped.
2. Dashboard changes (`entry_pct`, `profit_pct`, slots, soft-close) apply live.
3. If runtime volatility target remains enabled and floor-clamped, `profit_pct` tuning may require wiring/flag cleanup to take full effect.

---

## Phase 1: Mandatory Runtime Safety Fixes (Code)

### 1.1 Enforce Daily Loss Stop in `bot.py`

Requirement:

1. Add active-loop daily realized loss gating in `bot.py` (current active runtime), not legacy-only checks.
2. Scope is aggregate bot-level daily loss (all slots combined), not per-slot.

Behavior:

1. Compute aggregate realized loss for current UTC day from completed cycle records across all slots (`exit_time`, `net_profit < 0`).
2. If daily loss >= `DAILY_LOSS_LIMIT`, transition bot to `PAUSED`.
3. Set clear pause reason: `daily loss limit hit`.
4. Expose values in status payload:
   - `daily_loss_limit`
   - `daily_realized_loss_utc`
   - `daily_loss_lock_active`

Acceptance:

1. Bot pauses deterministically when threshold is crossed.
2. Status clearly explains why trading stopped.

### 1.2 Enforce Recovery Cap in `state_machine.py`

Requirement:

1. Apply `MAX_RECOVERY_SLOTS` in active orphan flow (currently not enforced in this path).
2. Scope is per-slot recovery cap (not global per-bot cap).

Behavior:

1. Before adding a new recovery in `_orphan_exit`, check slot-local recovery count.
2. If cap would be exceeded, apply deterministic oldest-first forced close policy, then add new recovery.
3. Keep policy explicit and deterministic; no silent no-op.

Acceptance:

1. Recovery count never exceeds configured per-slot cap.
2. Bot remains invariant-safe and does not deadlock.
3. Capacity pressure improves as stale recoveries are drained and capped, freeing slots for working entries.

### 1.3 Stabilization Defaults

Requirement:

1. Update default `S1_ORPHAN_AFTER_SEC` from `600` to `1350` in config defaults.

Acceptance:

1. New deployments boot with safer timeout behavior by default.

---

## Phase 2: Dashboard Safe Mode Controls (Code)

Goal:

1. Let operator execute stabilization controls from UI quickly and safely.

### 2.1 New Dashboard Section: `Safe Mode`

Add operator controls:

1. `Run Drift Reconcile` (calls existing runtime action).
2. `Soft-Close Stale Recoveries` with distance input.
3. `Apply Stabilization Preset`:
   - `entry_pct=0.35`
   - `profit_pct=0.80` (or configured target)
   - advisory text for env-backed knobs requiring restart.

### 2.2 Runtime/API

Add/confirm actions:

1. `reconcile_drift` (already exists, expose in UI).
2. `cancel_stale_recoveries` (already exists, expose in UI).
3. `apply_stabilization_preset` (new convenience action, runtime-only parameters).

### 2.3 UX Safety

1. Confirmation modal required for every destructive action.
2. Result toast includes counts (repriced/cancelled/failures).

Acceptance:

1. Operator can run full stabilization playbook from dashboard without Telegram/CLI.

---

## Phase 3: Dead-Knob Cleanup (Optional but Recommended)

Document and either wire or remove misleading knobs in current runtime path:

1. `VOLATILITY_AUTO_PROFIT` (currently declared, but adaptation is unconditional).
2. `EXIT_DRIFT_MAX_PCT` (declared but not used in active orphaning path).
3. `REBALANCE_ON_S1`, `RECOVERY_ENABLED`, `ENTRY_BACKOFF_ENABLED` (declared, verify live usage or deprecate).

Acceptance:

1. Every operator-facing knob is either functional or clearly marked deprecated.

---

## 5. Metrics and Success Criteria

Measure before/after on 24h windows.

Primary KPIs:

1. `orphans_created_per_24h` decreases significantly.
2. `total_orphans` trends down, not up.
3. `today_realized_loss` stays below configured limit.
4. `open_orders_drift` near zero most of the time.
5. `recovery_orders / open_orders` ratio declines.

Secondary KPIs:

1. Completion rate rises (fewer orphaned exits per round trip).
2. Capacity headroom remains healthy.
3. Fill latency distribution remains acceptable under lower churn.

Minimum acceptance thresholds (initial):

1. 50%+ reduction in orphan creation rate within 48h.
2. Zero breaches of enforced daily loss limit after deployment.
3. Recovery count hard-capped with no overflow.

---

## 6. Testing Plan

Unit tests:

1. Daily loss gate triggers at threshold and sets pause reason.
2. Daily loss gate does not trigger below threshold.
3. Recovery cap enforcement never exceeds configured maximum.
4. Recovery cap policy is deterministic (oldest-first).

Integration tests:

1. Simulate fill/orphan sequence with long fill latency and verify reduced premature orphaning under new default.
2. API actions for drift reconcile / stale recovery soft-close return expected messages and status effects.
3. Status payload includes new safety fields.

Manual test script:

1. Apply stabilization preset.
2. Trigger reconcile.
3. Drain stale recoveries and confirm `total_orphans` falls below target.
4. Confirm status fields reflect expected safe state.
5. Confirm pause behavior when synthetic daily loss breach is injected in test harness.

---

## 7. Rollout and Rollback

Rollout order:

1. Execute Phase 0 operational tuning immediately.
2. Deploy Phase 1 safety fixes.
3. Deploy Phase 2 dashboard controls.
4. Run 24h monitored canary.

Rollback:

1. Revert code if invariants regress.
2. Restore prior env values if fill throughput collapses unexpectedly.
3. Keep daily loss gate enabled unless it is proven faulty.

---

## 8. Locked Decisions (v1.1)

1. Daily-loss unlock policy: auto-clear lock at UTC rollover with manual resume required.
2. Recovery overflow policy: deterministic oldest-first forced close.
3. Stabilization preset `profit_pct` default: `0.80` (operator can override).

---

## 9. Final Summary

This plan locks in what all analyses converge on:

1. Fix timeout economics first.
2. Add real safety enforcement (daily loss + recovery cap).
3. Give operator direct, low-friction stabilization controls.

It is intentionally conservative: stop bleed, regain control, then optimize throughput.
