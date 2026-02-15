# Sticky Slots Implementation Checklist

Version: v0.2
Date: 2026-02-15
Depends on: `docs/STICKY_SLOTS_SPEC.md` (v0.2)

---

## 0) Execution Order

1. `config.py` (new knobs + safe defaults)
2. `state_machine.py` (sticky behavior behind feature flag)
3. `bot.py` (runtime wiring, release action, telemetry, status payload)
4. `dashboard.py` (new controls + remove orphan-centric UX paths)
5. `tests/test_hardening_regressions.py` (coverage for all new behavior)
6. Canary rollout + manual verification

---

## 1) `config.py`

### 1.1 Add sticky-mode controls

- [ ] Add `STICKY_MODE_ENABLED` (default: `False` for safe rollout)
- [ ] Add `STICKY_TARGET_SLOTS` (default: `80`)
- [ ] Add `STICKY_MAX_TARGET_SLOTS` (default: `100`)

### 1.2 Add release controls (spec §4.2)

- [ ] Add `RELEASE_MIN_AGE_SEC` (default: `604800`)
- [ ] Add `RELEASE_MIN_DISTANCE_PCT` (default: `10.0`)
- [ ] Add `RELEASE_ADX_THRESHOLD` (default: `30.0`)
- [ ] Add `RELEASE_MAX_STUCK_PCT` (default: `50.0`)
- [ ] Add `RELEASE_PANIC_STUCK_PCT` (default: `80.0`)
- [ ] Add `RELEASE_PANIC_MIN_AGE_SEC` (default: `86400`)
- [ ] Add `RELEASE_RECOVERY_TARGET_PCT` (default: `60.0`)
- [ ] Add `RELEASE_AUTO_ENABLED` (default: `False` initially)

### 1.3 Add momentum/regime controls (spec §6-§7)

- [ ] Add `MOMENTUM_ENABLED` (default: `False`)
- [ ] Add `MOMENTUM_MAX_DEFER_SEC` (default: `3600`)
- [ ] Add `MOMENTUM_EMA_FAST_BARS` (default: `5`)
- [ ] Add `MOMENTUM_EMA_SLOW_BARS` (default: `20`)
- [ ] Add `REGIME_ADX_ENABLED` (default: `False`)
- [ ] Add `REGIME_ADX_PERIOD` (default: `14`)
- [ ] Add `REGIME_ADX_CHOPPY_MAX` (default: `20.0`)
- [ ] Add `REGIME_ADX_TRENDING_MIN` (default: `30.0`)

### 1.4 Capacity and migration defaults

- [ ] Set phase-1 migration defaults:
  - [ ] `S1_ORPHAN_AFTER_SEC = 86400`
  - [ ] `S2_ORPHAN_AFTER_SEC = 172800`
  - [ ] `MAX_RECOVERY_SLOTS = 4`
- [ ] Keep `OPEN_ORDER_SAFETY_RATIO = 0.75` as default unless 100-slot path explicitly enabled

### 1.5 Add sizing and compounding controls (spec §5.3/§5.4)

- [ ] Add `ORDER_SIZE_MODE` enum (`USD`, `DOGE`) with safe fallback to `USD`
- [ ] Add `ORDER_SIZE_DOGE` (default: `18.0`)
- [ ] Add `STICKY_COMPOUNDING_MODE` enum (`legacy_profit`, `fixed`) with default `legacy_profit`
- [ ] Define sticky-mode rule: when `STICKY_MODE_ENABLED=true`, use `legacy_profit` in production rollout
- [ ] Keep `fixed` as emergency fallback/debug mode only

### 1.6 Release reconciliation hard-gate controls

- [ ] Keep `BALANCE_RECON_DRIFT_PCT` as release hard-gate threshold
- [ ] Set rollout recommendation: `BALANCE_RECON_DRIFT_PCT` in `1.0-2.0` band (target `1.5`)
- [ ] Add `RELEASE_RECON_HARD_GATE_ENABLED` (default: `True` when sticky mode enabled)

### 1.7 Validation and startup logging

- [ ] Clamp/validate release thresholds (`panic > conservative`, age >= 0)
- [ ] Clamp/validate EMA/ADX periods (`>= 2`)
- [ ] Clamp/validate sizing mode + compounding mode enums
- [ ] Emit sticky configuration summary in startup config log

---

## 2) `state_machine.py`

### 2.1 Introduce sticky behavior behind feature flag

- [ ] Extend `EngineConfig` with sticky/release/momentum flags needed by reducer
- [ ] In `transition(... TimerTick ...)`, short-circuit orphan transitions when `sticky_mode_enabled` is true
- [ ] Keep legacy orphan path intact when `sticky_mode_enabled` is false

### 2.2 Slot lifecycle simplification path (phase-gated)

- [ ] Remove S2-only logic from sticky path
- [ ] Ensure both-leg-in-position case behaves as independent exit waits in sticky path
- [ ] Preserve current behavior in non-sticky path

### 2.3 Release integration contract

- [ ] Define release state mutation helper for sticky path (remove waiting exit, advance cycle, re-arm entry)
- [ ] Keep `_book_cycle()` as canonical realized PnL accounting
- [ ] Ensure release bookkeeping updates consecutive-loss and cooldown counters consistently

### 2.4 Deletion targets (phase 5 only)

- [ ] Remove `RecoveryOrder` data model
- [ ] Remove `OrphanOrderAction` and orphan action union usage
- [ ] Remove `_orphan_exit()` path and recovery-cap eviction logic
- [ ] Remove `s2_entered_at` and S2-dependent transitions

### 2.5 Sizing path updates (USD and DOGE modes)

- [ ] Extend `EngineConfig` with sizing mode inputs required by reducer
- [ ] Keep existing USD path (`compute_order_volume(... order_size_usd)`) for compatibility
- [ ] Add DOGE-native volume path for sticky mode:
  - [ ] if mode is `DOGE`, set `volume = ORDER_SIZE_DOGE` (precision-clamped)
  - [ ] bypass USD/price division path for volume sizing
- [ ] Preserve Kraken min-volume/min-cost guards for both sizing modes

---

## 3) `bot.py`

### 3.1 Engine config plumbing

- [ ] Pass new sticky/release/momentum/regime settings through `_engine_cfg(...)`
- [ ] Keep legacy fields wired for non-sticky mode until phase 5 deletion

### 3.2 Release action (manual)

- [ ] Add runtime method `release_slot(slot_id, trade_id|exit_id)` (exact selector to be finalized)
- [ ] Implement conservative gate checks:
  - [ ] age gate
  - [ ] distance gate
  - [ ] regime gate
- [ ] Execute release using existing cancel + synthetic close pattern (auto-drain style)
- [ ] Immediately run drift reconciliation check after each successful release
- [ ] If drift exceeds `BALANCE_RECON_DRIFT_PCT`, block further releases until reconciled
- [ ] Return structured message including which gates passed/failed

### 3.3 Release action (automatic tiers)

- [ ] Add periodic evaluator in main loop:
  - [ ] tier-1 trigger: `stuck_capital_pct > RELEASE_MAX_STUCK_PCT` + all gates
  - [ ] tier-2 trigger: `stuck_capital_pct > RELEASE_PANIC_STUCK_PCT` + age-only oldest-first
- [ ] Drain until `stuck_capital_pct <= RELEASE_RECOVERY_TARGET_PCT` for tier-2
- [ ] Guard with `RELEASE_AUTO_ENABLED`
- [ ] Auto-disable release tiers when release recon hard-gate is tripped

### 3.4 Replace orphan operations in runtime UX/API

- [ ] Add/remove API actions in `DashboardHandler.do_POST()`:
  - [ ] add `release_slot`
  - [ ] remove/deprecate `soft_close`, `soft_close_next`, `cancel_stale_recoveries` (phase-gated)
- [ ] Add telemetry for release counts and last release timestamp

### 3.5 Slot vintage telemetry (spec §9)

- [ ] Add helper to compute vintage buckets for waiting exits
- [ ] Compute and expose:
  - [ ] `fresh_0_1h`, `aging_1_6h`, `stale_6_24h`, `old_1_7d`, `ancient_7d_plus`
  - [ ] `oldest_exit_age_sec`
  - [ ] `min_slot_size_usd`, `median_slot_size_usd`, `max_slot_size_usd`
  - [ ] `stuck_capital_usd`
  - [ ] `stuck_capital_pct`
- [ ] Add alert flags:
  - [ ] `vintage_warn`
  - [ ] `vintage_critical`
  - [ ] `vintage_release_eligible`

### 3.6 Capacity-safe rollout enforcement

- [ ] Add status fields for sticky slot targets and headroom safety
- [ ] Block/advise slot adds when projected all-S0 orders exceed safe cap
- [ ] Keep startup/bootstrap order pacing with deterministic prioritization
- [ ] Add startup/reconcile latency metrics:
  - [ ] `startup_reconcile_ms`
  - [ ] `startup_replay_ms`
  - [ ] `startup_bootstrap_ms`
  - [ ] `startup_total_ms`

### 3.7 Momentum + regime runtime wiring

- [ ] Compute momentum signal from existing 5-min trend data (or add minimal OHLC cache if needed)
- [ ] Apply entry deferral policy per side in sticky mode
- [ ] Enforce `MOMENTUM_MAX_DEFER_SEC` override
- [ ] Compute ADX regime and expose current band (`choppy`, `normal`, `trending`)
- [ ] Gate tier-1 release on trending regime

### 3.8 Rebalancer valuation basis (DOGE sizing interaction)

- [ ] Define/implement rebalancer idle-basis policy for sticky DOGE sizing:
  - [ ] preferred: mark-to-market portfolio basis (DOGE + USD)
  - [ ] legacy fallback: USD-only basis
- [ ] Expose active basis in status payload for operator visibility
- [ ] Ensure idle-ratio signal is consistent with DOGE-denominated deployment

### 3.9 Snapshot persistence

- [ ] Persist sticky-mode state and release telemetry in `_global_snapshot()`
- [ ] Load with backward-compatible defaults in `_load_snapshot()`
- [ ] Persist vintage-related counters/metadata required for restart continuity

### 3.10 Compounding behavior in sticky mode

- [ ] Make compounding decision explicit in `_slot_order_size_usd(...)`:
  - [ ] `legacy_profit`: preserve existing `ORDER_SIZE_USD + total_profit` behavior (sticky default)
  - [ ] `fixed`: ignore `slot.state.total_profit` in per-order notional (fallback mode)
- [ ] Ensure DOGE sizing mode is not silently coupled to legacy USD compounding
- [ ] Expose active sizing + compounding mode in `status_payload()`
- [ ] Assert sticky production profile uses `legacy_profit`

### 3.11 Phase-5 cleanup in runtime

- [ ] Remove orphan-pressure notification code (`ORPHAN_PRESSURE_WARN_AT`)
- [ ] Remove auto-soft-close/autodrain recovery paths
- [ ] Remove orphan commands from Telegram runtime command parser/help text

---

## 4) `dashboard.py`

### 4.1 Actions and command parsing

- [ ] Add command parser entries:
  - [ ] `:release <slot> <id>`
  - [ ] optional `:release_next` (if implemented runtime-side)
- [ ] Route to `/api/action {action: "release_slot", ...}`
- [ ] Remove/deprecate command suggestions for:
  - [ ] `:close`
  - [ ] `:stale`

### 4.2 Control panel updates

- [ ] Replace orphan controls with release controls:
  - [ ] manual release button(s)
  - [ ] release confirmation modal with gate summary
- [ ] Add sticky summary panel:
  - [ ] cycling vs waiting ratio
  - [ ] oldest exit age
  - [ ] stuck capital USD and %
  - [ ] tier-1/tier-2 release eligibility indicator

### 4.3 Table and labels

- [ ] Replace "Orphans" top metric with "Waiting/Stuck"
- [ ] Replace recovery table with waiting-exit vintage table
- [ ] Keep slot selection and action routing by numeric `slot_id`

### 4.4 Backward compatibility during rollout

- [ ] If backend does not yet return `slot_vintage`, render placeholders safely
- [ ] If legacy recovery fields still present, hide orphan-only actions when sticky mode is active

---

## 5) `tests/test_hardening_regressions.py`

### 5.1 Config + engine plumbing tests

- [ ] Sticky config fields load and clamp correctly
- [ ] `_engine_cfg()` includes sticky/release/momentum fields
- [ ] `ORDER_SIZE_MODE` and `STICKY_COMPOUNDING_MODE` invalid values clamp to safe defaults
- [ ] `RELEASE_RECON_HARD_GATE_ENABLED` defaults correctly under sticky mode

### 5.2 State machine tests

- [ ] Sticky mode `TimerTick` does not orphan S1 exits
- [ ] Sticky mode ignores S2 timeout orphaning path
- [ ] Non-sticky mode preserves current orphan behavior
- [ ] Release state mutation produces expected cycle and cooldown updates
- [ ] DOGE sizing mode creates orders with fixed DOGE volume independent of price

### 5.3 Runtime release tests

- [ ] `release_slot` rejects when age gate fails
- [ ] `release_slot` rejects when distance gate fails
- [ ] `release_slot` rejects when regime gate fails (tier-1)
- [ ] `release_slot` succeeds when all gates pass
- [ ] Tier-2 panic release bypasses distance/regime but enforces panic age floor
- [ ] Tier-2 drains to `RELEASE_RECOVERY_TARGET_PCT`
- [ ] Post-release reconcile runs immediately
- [ ] Releases pause when drift exceeds `BALANCE_RECON_DRIFT_PCT`

### 5.4 Telemetry/status tests

- [ ] `status_payload` includes `slot_vintage` block
- [ ] Bucket counts and percentages are consistent
- [ ] `vintage_release_eligible` reflects gate evaluation correctly

### 5.5 API/dashboard handler tests

- [ ] `/api/action` accepts `release_slot` with valid payload
- [ ] `/api/action` rejects invalid release payload (400 JSON)
- [ ] Legacy `soft_close*`/`cancel_stale_recoveries` actions are disabled in sticky mode

### 5.6 Capacity safety tests

- [ ] Safe-cap warning/guard triggers near `open_order_headroom <= 5`
- [ ] Slot scale-up to 80 stays under configured safe cap in worst-case projection
- [ ] 100-slot path requires explicit cap-policy override

### 5.7 Sizing/compounding tests

- [ ] Sticky `legacy_profit` mode preserves current behavior and compounds with slot profit
- [ ] Sticky `fixed` mode keeps per-slot notional stable as profits change
- [ ] DOGE sizing mode prevents min-size failures seen in USD/price-division path
- [ ] Sticky production profile defaults to `legacy_profit`

### 5.8 Rebalancer basis tests

- [ ] Mark-to-market basis changes idle ratio when DOGE price moves with constant balances
- [ ] USD-only basis behavior remains unchanged when fallback selected

### 5.9 Startup SLO tests

- [ ] Status payload includes startup timing metrics after initialize
- [ ] Startup timing metrics are non-negative and monotonic

---

## 6) Rollout Checklist (Ops + Verification)

### Phase 1 (guardrailed non-sticky)

- [ ] Deploy config-only timeout/cap changes (`24h/48h`, cap `4`)
- [ ] Run for 7-14 days and collect vintage baseline metrics

### Phase 2 (sticky manual release, 80 slots)

- [ ] Enable `STICKY_MODE_ENABLED=true`
- [ ] Keep `RELEASE_AUTO_ENABLED=false` initially
- [ ] Switch to `ORDER_SIZE_MODE=DOGE` with `ORDER_SIZE_DOGE=18`
- [ ] Use `STICKY_COMPOUNDING_MODE=legacy_profit` by default
- [ ] Set `BALANCE_RECON_DRIFT_PCT` to rollout target (recommend `1.5`)
- [ ] Scale slots gradually to 80
- [ ] Verify startup/reconcile stability at 150-200 tracked orders

### Phase 2b (optional 100-slot)

- [ ] Default recommendation: raise `OPEN_ORDER_SAFETY_RATIO` to `0.90` for pilot, monitor headroom/entry blocking, and roll back if needed
- [ ] Optional advanced path: implement entry-cap policy upgrade:
  - [ ] maintain global hard-cap guard on total open orders
  - [ ] add entry-only scheduling guard so parked exits do not over-throttle fresh entries
- [ ] Do NOT exempt parked exits from global hard-cap accounting
- [ ] Scale from 80 to 100 in batches and monitor headroom/entry blocking

### Phase 3-4 (momentum + auto release)

- [ ] Run momentum in shadow mode for >=7 days
- [ ] Enable momentum only if acceptance criteria pass
- [ ] Enable release tier-1 auto
- [ ] Enable release tier-2 auto after tier-1 stability

### Phase 5 (deletion)

- [ ] Remove orphan/recovery code paths
- [ ] Remove orphan-centric dashboard sections
- [ ] Remove orphan-related runtime/API commands

---

## 7) Acceptance Gates (Must Pass Before Phase Progression)

### Gate A: Sticky at 80 slots

- [ ] No invariant failures or HALTs attributable to sticky mode
- [ ] Safe-cap headroom remains healthy (no sustained `<=5`)
- [ ] Operator can execute manual `release_slot` with clear gate feedback

### Gate B: Momentum enablement

- [ ] Shadow-mode expectancy improves by at least 2% vs baseline over 7-day window
- [ ] p95 stuck age does not worsen by more than 15%
- [ ] p95 stuck capital % does not worsen by more than 10%

### Gate C: 100-slot rollout

- [ ] Capacity policy changed and documented
- [ ] Entry blocking does not materially increase after 80->100 scale-up
- [ ] Startup/reconcile latency remains within agreed SLO (target <= 60s at 150-200 tracked orders)
