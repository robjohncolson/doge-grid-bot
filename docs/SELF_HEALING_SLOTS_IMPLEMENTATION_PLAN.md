# Self-Healing Slots - Implementation Plan

Last updated: 2026-02-17
Parent spec: `docs/SELF_HEALING_SLOTS_SPEC.md` v0.2
Depends on: `docs/DUST_PROOF_LEDGER_SPEC.md`, `docs/STICKY_SLOTS_SPEC.md`, `docs/THROUGHPUT_SIZER_SPEC.md`
Status: **Ready for implementation**

## Goal

Implement per-slot self-healing so stale sticky exits are healed by a ledger-driven subsidy flywheel:
churner profit -> subsidy credits -> graduated repricing -> exit fill -> slot reuse.

## Scope

In scope:
1. New position ledger/journal module with immutable entry context, mutable exit intent, and append-only events.
2. Runtime integration for position open/close/reprice/churner-profit journaling.
3. Distance-weighted age bands and derived subsidy/opportunity-cost calculations.
4. Regime-gated churner mode with per-slot capital and shared reserve backstop.
5. Dashboard/API additions for subsidy health, churner activity, and write-off cleanup queue.
6. Supabase write/read integration for `position_ledger` and `position_journal`.
7. Migration of existing open exits into `slot_mode="legacy"` position records.

Out of scope:
1. S0/S1/S2 reducer transition redesign.
2. HMM detector model changes.
3. Cross-pair subsidy transfers.
4. Silent automatic write-offs without operator approval (unless explicitly enabled by policy flag).

## Current Baseline (Code Audit)

1. Sticky mode already disables timer orphaning in reducer path (`state_machine.py:1026`), but there is no position ledger, subsidy, churner state, or age-band engine.
2. Runtime currently supports manual/auto sticky release gates (`bot.py:8095`, `bot.py:8290`, `bot.py:8344`) and vintage metrics (`bot.py:8227`), but these are age-only buckets and not distance-weighted effective age.
3. Runtime fill handling is centralized in `_poll_order_status()` (`bot.py:7238`) and `_apply_event()` (`bot.py:6955`), which is the right insertion point for ledger open/close/reprice journaling.
4. Supabase persistence already uses queue-based batched writes with schema auto-detection (`supabase_store.py:574`) but has no position-ledger table support functions.
5. Dashboard already has sticky/release controls and slot-vintage cards (`dashboard.py:1436`) plus release actions (`dashboard.py:987`), so self-healing UI can extend existing sticky UX rather than replace it wholesale.

## Locked Implementation Decisions

1. Keep reducer pure and keep self-healing orchestration in runtime (`bot.py`) plus new helper module(s).
2. Implement `position_ledger.py` as local-first source of truth with optional Supabase replication.
3. Track churner orders outside reducer-managed `PairState.orders` to avoid changing S0/S1/S2 semantics.
4. Maintain explicit runtime index from active exit identity -> `position_id` for deterministic close/reprice journaling.
5. Use journal-derived subsidy balances only; never maintain mutable subsidy accumulators.

## Implementation Order

1. Build ledger core (`position_ledger.py`) with deterministic invariants and unit tests.
2. Wire runtime call sites for open/close/reprice journaling and snapshot persistence.
3. Add age-band/subsidy derivation helpers and reprice engine (tighten + subsidized partial/full).
4. Add churner runtime loop and reserve-capital flow.
5. Add Supabase table support and startup migration.
6. Add dashboard/API surfaces and cleanup queue actions.
7. Execute staged rollout behind feature flags.

## Phase 1 - Ledger Foundation (`position_ledger.py`)

Files:
- `position_ledger.py` (new)
- `tests/test_position_ledger.py` (new)

Changes:
1. Add data models for `PositionRecord` and `JournalRecord` with immutable/mutable field guardrails.
2. Implement required API:
   - `open_position(...)`
   - `journal_event(...)`
   - `close_position(...)`
3. Implement derived query helpers:
   - `get_open_positions(slot_id=None)`
   - `get_subsidy_balance(slot_id)`
   - `get_position_history(slot_id=None, limit=50)`
4. Add serialization/deserialization helpers for runtime snapshot persistence.
5. Add invariant checks:
   - immutable `entry_*` and `original_exit_price`
   - one-way `status` transition (`open` -> `closed`)
   - single-write outcome fields.

Acceptance checks:
1. Ledger open/close/journal flows are deterministic and idempotent where required.
2. Invalid mutation attempts are rejected with explicit errors.
3. Derived subsidy balance matches journal credits/debits exactly.

## Phase 2 - Runtime Integration (`bot.py`)

Files:
- `bot.py`

Changes:
1. Runtime state:
   - add `self._position_ledger`
   - add counters (`position_id_counter`, `journal_id_counter`) and watermarks
   - add index maps (`(slot_id, local_id)` and txid-based lookup -> `position_id`)
2. Entry fill -> exit placement integration:
   - in closed-order path (`bot.py:7238`), when a reducer entry fill creates a new exit, call `open_position(...)` and `journal_event("created", ...)`.
3. Exit fill integration:
   - on filled exits, call `close_position(...)` with actual fee/cost/regime fields and `close_reason="filled"`.
   - **over-performance detection**: compare actual fill price to `current_exit_price`. If the fill is more favorable (sell filled higher than target, or buy filled lower), compute `excess = abs(actual - target) × volume` and write `journal_event("over_performance", {expected_profit, actual_profit, excess})` on the position. This credits the slot's subsidy balance (spec §4.4, §5.2).
4. Reprice integration:
   - on every tighten/subsidized/operator reprice, write `journal_event("repriced", {...})`, update exit identity mapping, and increment `times_repriced`.
5. Operator close/write-off integration:
   - route manual cleanup actions to `close_position(... close_reason="written_off" | "soft_closed")`.
6. Churner profit integration:
   - journal `churner_profit` (or compound after heal) on parent stuck position.

Acceptance checks:
1. Every open reducer exit maps to exactly one open ledger position.
2. No duplicate closes for the same `position_id`.
3. Reprices keep position open and correctly rotate exit identity mapping.
4. Over-performance detection fires when exit fills at a better-than-target price and correctly journals the excess as a subsidy credit.

## Phase 3 - Age Bands, Subsidy, and Opportunity Cost

Files:
- `bot.py`
- `config.py`
- `tests/test_hardening_regressions.py`
- `tests/test_self_healing_slots.py` (new)

Changes:
1. Add distance-weighted effective-age helper:
   - `effective_age = age_seconds * (1 + distance_pct / AGE_DISTANCE_WEIGHT)`
2. Add derived age-band classification (`fresh`, `aging`, `stale`, `stuck`, `write_off`) per open position.
3. Add derived subsidy-needed calculation for both exit sides using fillable-price logic in spec.
4. Add derived opportunity-cost calculation using throughput signal plus position capital share.
5. Cache ledger-derived subsidy and age-band snapshots once per loop for runtime/dashboard reads.
6. **Throughput sizer segregation**: Churner positions (`slot_mode="churner"`) must be excluded from the throughput sizer's sticky-slot fill-time statistics. Churner cycles feed only the "ranging" regime bucket. This prevents fast churner cycles from artificially inflating the throughput signal for sticky slots (spec §10.1).

Acceptance checks:
1. Band boundaries and distance weighting match spec with deterministic thresholds.
2. Subsidy-needed and opportunity-cost values are non-negative and stable.
3. Existing sticky/release metrics continue to populate without regression.

## Phase 4 - Graduated Repricing Engine

Files:
- `bot.py`
- `config.py`
- `tests/test_hardening_regressions.py`
- `tests/test_self_healing_slots.py`

Changes:
1. Tighten reprice (stale band):
   - one-time transition action
   - target `max(profit_pct, volatility_adjusted_target)`
   - hysteresis (`+0.1%`) and no subsidy debit.
2. Subsidized reprice (stuck band):
   - enforce per-position cooldown (`SUBSIDY_REPRICE_INTERVAL_SEC`)
   - full or partial move based on derived subsidy balance
   - journal `subsidy_consumed` debit in `repriced` event.
3. Add explicit skip reasons (cooldown, insufficient balance, no fillable delta) for observability.
4. Ensure reprice flow is API-safe (cancel old -> place new -> remap `position_id` binding).

Acceptance checks:
1. Tighten reprices never consume subsidy.
2. Subsidy reprices never drive derived subsidy balance below zero.
3. Partial reprices advance toward fillable price exactly by affordable amount.

## Phase 5 - Churner Runtime Engine

Files:
- `bot.py`
- `config.py`
- `tests/test_hardening_regressions.py`
- `tests/test_self_healing_slots.py`

Changes:
1. Add per-slot churner runtime state:
   - active flag, parent stuck `position_id`, current churner txids, timers, order size.
2. Activation/deactivation gates:
   - age band >= aging
   - HMM consensus = ranging
   - order-cap headroom >= `CHURNER_MIN_HEADROOM`
   - capital available (slot idle or reserve backstop).
3. Churner lifecycle loop:
   - place churner entry near market
   - on fill place tight churner exit
   - enforce entry/exit timeouts with cancel/reseed
   - on cycle close, route profit to subsidy until healed, then compound.
4. Add shared reserve accounting:
   - `CHURNER_RESERVE_USD` pool with per-slot cap (one order size).
5. Track churner round-trips as `slot_mode="churner"` positions in ledger.

Acceptance checks:
1. Churner never activates when regime/headroom/capital gates fail.
2. Timeout loops do not strand unmanaged orders.
3. Profit routing switches from subsidy-credit to compounding only after heal criteria are met.
4. **Rate limit budget**: Worst-case churner API calls stay within budget per spec §14 — with 5 active churners, overhead is ~1-2 extra private calls per 30s cycle (40-60 calls/day total), well within the 15-counter rate limit.

## Phase 6 - Persistence and Supabase Integration

Files:
- `supabase_store.py`
- `bot.py`
- `docs/supabase_v1_schema.sql`
- `tests/test_hardening_regressions.py`

Changes:
1. Add Supabase support flags for new tables (`position_ledger`, `position_journal`) using existing auto-detect pattern.
2. Add queue helpers:
   - `save_position_ledger(row)` (upsert on `position_id`)
   - `save_position_journal(row)` (append-only insert)
3. Add snapshot persistence fields:
   - open ledger positions
   - recent journal window
   - counters and watermarks.
4. Add journal trim logic (`POSITION_JOURNAL_LOCAL_LIMIT`) with watermark carry-forward.
5. Write cadence:
   - queue ledger/journal rows on same loop cadence as current `bot_state` persistence.

Acceptance checks:
1. Local-only mode runs fully when Supabase tables are absent.
2. Supabase-enabled mode inserts journal rows and upserts position rows without blocking loop.
3. Trimmed journal contributions are preserved in watermark totals.

## Phase 7 - Startup Migration

Files:
- `bot.py`
- `position_ledger.py`
- `tests/test_hardening_regressions.py`

Changes:
1. On first startup with ledger enabled and no existing positions:
   - scan current open exits in all slots
   - create `slot_mode="legacy"` open positions
   - create matching `created` journal entries with `migration=true`.
2. Derive best-effort entry context from existing order fields (and available actual fee/cost data).
3. Initialize counters/watermarks and log migration summary.
4. Add idempotent migration sentinel to prevent duplicate import.

Acceptance checks:
1. Migration does not alter live order behavior.
2. Re-running startup does not duplicate migrated positions.
3. All active exits become visible in ledger immediately after migration.

## Phase 8 - API and Dashboard

Files:
- `bot.py`
- `dashboard.py`
- `tests/test_hardening_regressions.py`

Changes:
1. Extend `/api/status` payload:
   - subsidy summary (pool, lifetime earned/spent, pending need, ETA)
   - churner activity (active count, cycles/profit today, paused reason)
   - cleanup queue rows (write-off band).
2. Add operator actions:
   - reprice to breakeven (operator reason)
   - close at market (write-off)
   - keep holding/reset timer.
3. Dashboard updates:
   - age heatmap from effective-age bands
   - subsidy health card
   - churner activity card
   - cleanup queue controls.
4. Preserve backward-safe rendering when new payload fields are absent.

Acceptance checks:
1. Dashboard renders cleanly with and without self-healing flags enabled.
2. New actions route through API and produce ledger journal trails.
3. Existing sticky/release controls remain functional during phased rollout.

## Phase 9 - Configuration and Validation

**Timing note**: Config variable definitions are added to `config.py` progressively as each phase needs them (Phase 1 adds `POSITION_LEDGER_*`, Phase 3 adds `AGE_BAND_*`, Phase 4 adds `SUBSIDY_*`, Phase 5 adds `CHURNER_*`). This phase consolidates validation, dependency enforcement, and startup summary logging. All validation runs at bot startup before any ledger/churner/subsidy logic executes.

Files:
- `config.py`
- `.env.example` (if present)
- `README.md` (if config docs are maintained there)

Changes:
1. Add and validate config families:
   - `POSITION_LEDGER_*`
   - `CHURNER_*`
   - `SUBSIDY_*`
   - `AGE_BAND_*` and `AGE_DISTANCE_WEIGHT`
2. Clamp invalid values to safe defaults and emit startup summaries.
3. Enforce dependencies:
   - `CHURNER_ENABLED` implies `POSITION_LEDGER_ENABLED`
   - subsidy auto-reprice requires ledger + subsidy master toggle.

Acceptance checks:
1. Invalid envs do not crash startup.
2. Startup logs clearly show active self-healing profile.

## Test Plan

New tests:
1. `tests/test_position_ledger.py`
   - ledger open/close immutability
   - journal append-only guarantees
   - subsidy derivation + watermark behavior.
2. `tests/test_self_healing_slots.py`
   - age-band classification and distance weighting
   - tighten/subsidized full/partial reprices
   - churner activation/deactivation/timeouts/profit routing
   - over-performance detection and subsidy credit journaling
   - throughput sizer excludes churner cycles from sticky fill-time stats.

Updated regression tests:
1. `tests/test_hardening_regressions.py`
   - runtime fill-to-ledger open/close wiring
   - startup migration idempotence
   - API action routing for cleanup queue controls
   - status payload fields for subsidy/churner/cleanup.

Local verification commands:
1. `python3 -m unittest tests.test_position_ledger`
2. `python3 -m unittest tests.test_self_healing_slots`
3. `python3 -m unittest tests.test_hardening_regressions`

## Rollout Plan

1. Stage A (Ledger only, 48h)
   - `POSITION_LEDGER_ENABLED=true`
   - `CHURNER_ENABLED=false`
   - `SUBSIDY_ENABLED=false`
2. Stage B (Tighten repricing only)
   - `SUBSIDY_ENABLED=true`
   - `SUBSIDY_AUTO_REPRICE_BAND=stale`
   - `CHURNER_ENABLED=false`
3. Stage C (Churner on, subsidy crediting)
   - `CHURNER_ENABLED=true`
   - keep subsidized repricing at stale or stuck based on observed behavior.
4. Stage D (Full flywheel)
   - `SUBSIDY_AUTO_REPRICE_BAND=stuck`
   - monitor heal time, API call budget, open-order headroom.
5. Stage E (Optional policy automation)
   - `SUBSIDY_WRITE_OFF_AUTO` only after manual queue operation is stable.

## Rollback Plan

1. Immediate safety rollback:
   - `CHURNER_ENABLED=false`
   - `SUBSIDY_ENABLED=false`
   - retain `POSITION_LEDGER_ENABLED=true` for forensic continuity.
2. If repricing regressions persist:
   - disable auto-reprice band and keep manual cleanup only.
3. If persistence issues occur:
   - run local-only ledger mode; keep Supabase writes disabled until schema/API parity is restored.

## Definition of Done

1. All active exits are represented as open ledger positions with journal history.
2. Subsidy balances are derived solely from journal credits/debits and remain non-negative.
3. Churner mode runs only under ranging/headroom/capital gates and cleanly pauses on gate failure.
4. Graduated repricing executes with cooldowns, partial reprices, and auditable journal entries.
5. Dashboard/API expose subsidy health, churner activity, and write-off cleanup queue actions.
6. Migration, unit tests, and regression suites pass under staged feature flags.

