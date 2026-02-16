# Throughput Sizer - Implementation Plan

Last updated: 2026-02-16
Parent spec: `docs/THROUGHPUT_SIZER_SPEC.md` v1.0
Status: **Ready for implementation**

## Goal

Replace Kelly-based sizing with a fill-time throughput sizer that scales order size by expected capital turnover, while preserving existing trading safeguards and integration behavior.

## Scope

In scope:
1. New module `throughput_sizer.py` implementing the spec contract (`ThroughputConfig`, `BucketStats`, `ThroughputResult`, `ThroughputSizer`).
2. Runtime wiring in `bot.py` (constructor, update cadence, sizing call, status payload, snapshot restore/save).
3. Config migration in `config.py` from active `KELLY_*` usage to `TP_*` usage.
4. Dashboard card replacement in `dashboard.py` ("Kelly Sizing" -> "Throughput Sizer").
5. New and updated tests for sizing logic, runtime integration, status payload, and snapshot behavior.

Out of scope:
1. Any state-machine behavior changes (`S0/S1/S2`, entry/exit pricing logic, repricing policy).
2. Auto-scaling layer-count based on throughput signal.
3. Removal of `kelly_sizer.py` from the repository.

## Current Baseline (Code Audit)

1. Runtime currently imports and instantiates Kelly in `bot.py:33` and `bot.py:403`.
2. Kelly multiplier is applied in `_slot_order_size_usd()` at `bot.py:800`.
3. Kelly updates run via `_update_kelly()` (`bot.py:2112`) on regime-eval cadence (`bot.py:3679`).
4. Kelly snapshot state is persisted/restored at `bot.py:1277` and `bot.py:1547`.
5. `/api/status` emits `kelly` payload at `bot.py:7702`.
6. Config block is Kelly-only in `config.py:539`.
7. Dashboard card and JS renderer are Kelly-only in `dashboard.py:479` and `dashboard.py:1689`.
8. Test coverage includes `tests/test_kelly_sizer.py` and runtime Kelly integration checks in `tests/test_hardening_regressions.py:775` and `tests/test_hardening_regressions.py:931`.
9. `throughput_sizer.py` and `tests/test_throughput_sizer.py` do not exist yet.

## Locked Implementation Decisions

1. `ThroughputSizer` is a drop-in runtime replacement at the same call sites currently used by `KellySizer`.
2. Completed-cycle schema for throughput update includes:
   - `entry_time`, `exit_time`, `regime_at_entry`, `trade_id`, `net_profit`, `volume`.
3. Open-exit schema for censored observations includes:
   - `regime_at_entry`, `trade_id`, `age_sec`, `volume`.
4. Open exits should include both active exit orders and active recovery orders so censored age/utilization sees all locked exits.
5. Insufficient data is always pass-through (`multiplier=1.0`), never an exception path.
6. `kelly_sizer.py` remains in repo; runtime bot imports `throughput_sizer.py` only.

## Implementation Order

1. Build and unit-test `throughput_sizer.py`.
2. Add `TP_*` config and wire runtime integration in `bot.py`.
3. Replace dashboard Kelly card with Throughput card.
4. Update regression tests and status/snapshot assertions.
5. Execute rollout stages behind `TP_ENABLED`.

## Phase Plan

## Phase 1 - Implement `throughput_sizer.py`

Files:
- `throughput_sizer.py` (new)

Changes:
1. Add dataclasses:
   - `ThroughputConfig`
   - `BucketStats`
   - `ThroughputResult`
2. Implement normalization helpers for regime IDs and trade IDs (`A`/`B`).
3. Implement weighted percentile utilities for completed and censored durations.
4. Implement bucket partitioning for:
   - `bearish_A`, `bearish_B`, `ranging_A`, `ranging_B`, `bullish_A`, `bullish_B`, `aggregate`
5. Implement censored merge behavior:
   - include censored observations at `TP_CENSORED_WEIGHT`
   - gate censored rows by age (`> 0.5 * current median`)
6. Implement update pipeline:
   - lookback trim by `TP_LOOKBACK_CYCLES`
   - aggregate and per-bucket stats
   - age pressure computation
   - utilization penalty computation
   - result caching for `size_for_slot()`
7. Implement sizing pipeline:
   - baseline vs bucket median ratio
   - confidence blend toward `1.0`
   - final multiplier clamp using `TP_FLOOR_MULT` and `TP_CEILING_MULT`
   - reason strings (`ok`, `insufficient_data`, `no_bucket`, `tp_disabled`)
8. Implement telemetry and persistence:
   - `status_payload()`
   - `snapshot_state()`
   - `restore_state()`

Acceptance checks:
1. API surface matches spec signature.
2. Disabled or insufficient-data mode always returns base size unchanged.
3. Final multiplier is always bounded and finite.

## Phase 2 - Config Migration to `TP_*`

Files:
- `config.py`

Changes:
1. Add `TP_*` env-backed constants exactly as defined in the parent spec.
2. Remove runtime dependence on `KELLY_*` constants in bot wiring.
3. Keep `KELLY_ENABLED` as inert/dead compatibility config (read nowhere in runtime sizing path).

Acceptance checks:
1. Bot startup succeeds with only `TP_*` settings.
2. Existing deployments with legacy Kelly env vars do not crash startup.

## Phase 3 - Runtime Integration in `bot.py`

Files:
- `bot.py`

Changes:
1. Replace import:
   - `from kelly_sizer import KellyConfig, KellySizer`
   - with `from throughput_sizer import ThroughputConfig, ThroughputSizer`
2. Constructor wiring:
   - replace `self._kelly` with `self._throughput`
   - instantiate when `TP_ENABLED=True`
3. Add/update helper methods:
   - regime label mapping helper for throughput lookup
   - completed-cycle collector with throughput-required fields
   - open-exit collector (orders + recovery orders)
   - free-DOGE resolver for utilization penalty input
4. Replace `_update_kelly()` with `_update_throughput()`:
   - pass completed cycles, open exits, regime label, free DOGE
5. Replace sizing call in `_slot_order_size_usd()`:
   - from `self._kelly.size_for_slot(...)`
   - to `self._throughput.size_for_slot(..., trade_id=trade_id)`
6. Replace snapshot persistence keys:
   - `kelly_state` -> `throughput_sizer_state`
7. Replace status payload key:
   - `kelly` -> `throughput_sizer`
8. Remove remaining `self._kelly` references or convert to throughput equivalents.

Acceptance checks:
1. No import/use of Kelly classes in runtime execution path.
2. Sizing path still honors existing min-volume and fund-guard constraints.
3. Snapshot save/load remains backward-safe when throughput state is absent.

## Phase 4 - Dashboard Card Replacement

Files:
- `dashboard.py`

Changes:
1. Replace "Kelly Sizing" HTML section with "Throughput Sizer".
2. Replace Kelly DOM IDs with throughput IDs:
   - status, active regime, samples, age pressure, utilization, buckets
3. Replace JS rendering block:
   - source object: `s.throughput_sizer`
   - statuses: `OFF`, `WARMING`, `ACTIVE`
   - bucket text format: `name: x1.12 (30m)` or reason/no-data form
4. Ensure disabled display text references throughput, not Kelly.

Acceptance checks:
1. Dashboard renders without missing-element errors.
2. Status card reflects API payload correctly for disabled, warming, and active states.

## Phase 5 - Tests and Regression Updates

Files:
- `tests/test_throughput_sizer.py` (new)
- `tests/test_hardening_regressions.py`

Changes:
1. Add throughput unit tests covering:
   - bucket partitioning (6 buckets + aggregate)
   - median/p75/p95 computation
   - censored weighting behavior
   - faster/slower bucket multiplier directionality
   - confidence blending
   - age pressure trigger/floor
   - utilization penalty trigger/floor
   - final clamp bounds
   - insufficient data pass-through
   - disabled pass-through
   - snapshot/restore shape
2. Replace runtime Kelly integration tests with throughput equivalents:
   - `_slot_order_size_usd()` applies throughput multiplier
   - dust interaction still composes correctly with throughput result
   - status payload exposes `throughput_sizer` key
3. Keep legacy Kelly unit tests as reference-only unless explicitly deprecated in a separate cleanup.

Acceptance checks:
1. Throughput tests pass.
2. Existing hardening regression suite passes with updated assertions.

## Phase 6 - Rollout and Monitoring

Runtime stages:
1. Stage 1: Deploy with `TP_ENABLED=False` and verify no behavioral regression.
2. Stage 2: Enable `TP_ENABLED=True` in one environment and observe 24h.
3. Stage 3: Full rollout with live sizing enabled.
4. Stage 4: Tune `TP_AGE_PRESSURE_*` and `TP_UTIL_*` from observed congestion patterns.

Operational metrics to watch:
1. `throughput_sizer.last_update_n`
2. Distribution of bucket multipliers (especially clamp hits at floor/ceiling)
3. `age_pressure` and oldest open exit age behavior
4. `util_penalty` and utilization ratio behavior
5. Fill turnaround by regime and side after activation

## Rollback Plan

1. Immediate safety rollback: set `TP_ENABLED=False` (pass-through sizing).
2. If dashboard regressions occur: keep API payload, temporarily hide card rendering block.
3. If runtime regressions persist: revert `bot.py` integration changes and leave module/tests isolated.

## Definition of Done

1. `throughput_sizer.py` exists and matches the spec API and math behavior.
2. `bot.py` runtime uses ThroughputSizer at constructor/update/sizing/status/snapshot points.
3. `/api/status` exposes `throughput_sizer` and no longer depends on `kelly`.
4. Dashboard "Throughput Sizer" card is functional and wired to the new payload.
5. Unit and integration tests for throughput behavior are passing.
6. Feature is fully controllable through `TP_ENABLED` for staged rollout.
