# Balance Intelligence - Implementation Plan

Last updated: 2026-02-17
Parent spec: `docs/BALANCE_INTELLIGENCE_SPEC.md` v0.1.0
Status: **Ready for implementation (with clarifications below)**

## Goal

Add ledger-backed external flow detection, persistent equity time-series, and flow-adjusted reconciliation telemetry so balance drift reflects bot behavior rather than deposits/withdrawals.

## Scope

In scope:
1. Kraken Ledger endpoint wrapper in `kraken_client.py`.
2. Runtime external-flow polling, dedup, cursoring, baseline adjustment, and persistence in `bot.py`.
3. Supabase-backed 7-day equity history under `bot_state` key `__equity_ts__` plus local fallback.
4. Expanded status payload (`balance_recon`, `external_flows`, `equity_history`).
5. Dashboard reconciliation + equity visualization updates in `dashboard.py`.
6. New config/env contract in `config.py` and `.env.example`.
7. Unit/regression test coverage.

Out of scope:
1. Trading logic changes (entry/exit/sizing/state transitions).
2. Tax/cost-basis reporting.
3. New Supabase tables.
4. Historical flow backfill before runtime start.

## Current Baseline (Code Audit)

1. `kraken_client.py` has no `get_ledgers()` wrapper today.
2. `bot.py` currently tracks only `_recon_baseline`; no external flow tracker state.
3. `bot.py` snapshots only persist `recon_baseline`; no flow cursor/ids/adjustment audit.
4. `bot.py` computes recon using baseline vs current balances and bot PnL only (`drift_doge`, `drift_pct`).
5. `bot.py` DOGE-equity snapshots are in-memory only (`_doge_eq_snapshots`, 24h trim), no persistent 7d store.
6. `bot.py` `_update_doge_eq_snapshot()` is called from `status_payload()`, so snapshot cadence currently depends on status polling.
7. `dashboard.py` renders a small sparkline from `doge_eq_sparkline` and shows legacy recon fields (`Drift`, no flow decomposition).
8. `config.py` currently exposes only `BALANCE_RECON_DRIFT_PCT` for recon; no flow/equity-ts toggles.

## Verification Findings (Spec Clarifications)

1. **Double-count risk (baseline auto-adjust + adjusted drift subtraction):**
   If baseline is auto-adjusted for flows, `drift_doge` is already flow-adjusted. Subtracting cumulative flows again would double-count.
   Implementation decision:
   - Keep `drift_doge` semantics unchanged for compatibility.
   - Compute `adjusted_drift_doge` as:
     - `drift_doge` when auto-adjust is enabled
     - `drift_doge - external_flows_doge_eq` when auto-adjust is disabled (observe-only mode).

2. **No-backfill requirement vs initial cursor=0 contradiction:**
   Starting flow cursor at `0` imports historical deposits/withdrawals.
   Implementation decision:
   - On first startup without persisted cursor, seed cursor to `max(recon_baseline.ts, now - FLOW_POLL_INTERVAL_SEC)` and only process newer entries.

3. **Polling budget consistency:**
   Spec alternates between two calls (`deposit` + `withdrawal`) and one call (`type=all`).
   Implementation decision:
   - Use one call per poll (`type_="all"`, client-filter to `deposit`/`withdrawal`, DOGE/USD assets).
   - Keep optional pagination loop only when `count > returned_rows`.

4. **State growth guard:**
   Persisting all historical flows indefinitely in `__v1__` can bloat snapshots.
   Implementation decision:
   - Keep full cumulative totals for metrics.
   - Retain detailed flow objects/audit entries with cap (for example latest 1000) plus `recent_flows` window in status.

## Implementation Phases

## Phase 1 - Config and Data Contracts

Files:
- `config.py`
- `.env.example`
- `bot.py` (state field declarations)

Changes:
1. Add config constants:
   - `FLOW_DETECTION_ENABLED`
   - `FLOW_POLL_INTERVAL_SEC`
   - `FLOW_BASELINE_AUTO_ADJUST`
   - `EQUITY_TS_ENABLED`
   - `EQUITY_SNAPSHOT_INTERVAL_SEC`
   - `EQUITY_SNAPSHOT_FLUSH_SEC`
   - `EQUITY_TS_RETENTION_DAYS`
   - `EQUITY_TS_SPARKLINE_7D_STEP`
2. Replace hardcoded snapshot interval (`300.0`) with config-backed value.
3. Add runtime fields:
   - flow tracker list/cursor/poll timestamps/dedup set/audit trail
   - equity flush timestamps/cursors
   - last flow poll health flags
4. Add `.env.example` entries for new flow/equity settings.

Acceptance checks:
1. Bot boots with no new env vars set (defaults only).
2. Snapshot cadence still works with defaults.

## Phase 2 - Kraken Ledger Integration

Files:
- `kraken_client.py`

Changes:
1. Add constant:
   - `LEDGER_PATH = "/0/private/Ledgers"`
2. Add method:
   - `get_ledgers(type_="all", asset="all", start=None, end=None, ofs=None) -> dict`
3. Use `_private_request` and preserve dry-run safety (`{}` or empty structure).
4. Normalize/forward Kraken parameters as strings only when present.
5. Add minimal defensive parsing helper in runtime path (not in client) for malformed rows.

Acceptance checks:
1. Method works with `start`, `ofs` pagination parameters.
2. Method failure surfaces as existing warning/exception behavior without crashing loop.

## Phase 3 - Flow Polling and Baseline Adjustment

Files:
- `bot.py`

Changes:
1. Add `ExternalFlow` dataclass and serialization helpers.
2. Implement:
   - `_should_poll_flows(now)`
   - `_poll_external_flows(now)`
   - `_process_ledger_entries(rows, now)`
   - `_flow_to_doge_eq(entry, price)`
   - `_apply_flow_baseline_adjustment(flow, price, now)`
3. Integrate polling into `run_loop_once()` after balances/prices are fresh and before snapshot save.
4. Deduplicate by ledger id (`_flow_seen_ids`) and advance cursor monotonically.
5. Filter to allowed flow types/assets and skip unsupported assets safely.
6. Respect loop private-call budget via `_consume_private_budget(1, "get_ledgers")`.
7. Track poll health in status (`flow_poll_ok`, `flow_poll_age_sec`, last error message optional).

Acceptance checks:
1. Duplicate ledger IDs never double-adjust baseline.
2. Deposits increase baseline, withdrawals decrease baseline.
3. Poll failures do not crash loop and recover next interval.

## Phase 4 - Recon Math and Status Payload

Files:
- `bot.py`

Changes:
1. Extend `_compute_balance_recon()` with:
   - `external_flows_doge_eq`
   - `external_flow_count`
   - `adjusted_drift_doge`
   - `adjusted_drift_pct`
   - `adjusted_status`
   - latest flow metadata
2. Preserve existing recon keys for backward compatibility.
3. Add top-level status blocks:
   - `external_flows`
   - `equity_history`
4. Include both 24h sparkline and downsampled 7d sparkline payloads.

Acceptance checks:
1. Existing consumers of `balance_recon.drift_*` continue to work.
2. New adjusted metrics are internally consistent in both auto-adjust and observe-only modes.

## Phase 5 - Persistent Equity Time-Series

Files:
- `bot.py`
- `supabase_store.py` (reuse existing `save_state`/`load_state`, no schema change)

Changes:
1. Keep in-memory `_doge_eq_snapshots` as 24h rolling.
2. Move snapshot update call into loop cadence so it is not dependent on dashboard polling.
3. Implement periodic flush:
   - `_should_flush_equity_ts(now)`
   - `_flush_equity_ts(now)` to key `__equity_ts__`
4. Persist enriched points:
   - `ts`, `doge_eq`, `usd`, `doge`, `price`, `bot_pnl_usd`, `flows_cumulative_doge_eq`
5. Trim persisted points by `EQUITY_TS_RETENTION_DAYS`.
6. Add local fallback file `logs/equity_ts.json` read/write helpers.
7. Restore persisted equity history on startup for continuity in 7d view.

Acceptance checks:
1. Restart preserves 7-day chart continuity.
2. Persisted series is trimmed correctly and does not grow unbounded.

## Phase 6 - Snapshot Persistence and Restore

Files:
- `bot.py`

Changes:
1. Extend `_global_snapshot()` with flow-tracker fields:
   - `external_flows`
   - `flow_ledger_cursor`
   - `flow_seen_ids`
   - `baseline_adjustments`
   - last flow poll metadata
2. Extend `_load_snapshot()` with defensive restore and type sanitation.
3. Keep backward compatibility when keys are missing.
4. Cap loaded historical lists to configured max lengths.

Acceptance checks:
1. Old snapshots load cleanly.
2. New snapshots round-trip without type errors.

## Phase 7 - Dashboard Integration

Files:
- `dashboard.py`

Changes:
1. Update Balance Reconciliation card:
   - show `Unexplained` (adjusted drift) and flow breakdown.
   - preserve fallback display when new fields absent.
2. Replace tiny sparkline area with equity chart panel supporting:
   - 24h and 7d views
   - flow event markers
3. Add collapsible flow-history list using `external_flows.recent_flows`.
4. Keep rendering dependency-free (SVG only), aligned with existing style.

Acceptance checks:
1. Dashboard renders with old and new payloads.
2. 24h/7d toggles and markers behave correctly on mobile and desktop.

## Phase 8 - Test Plan

Files:
- `tests/test_hardening_regressions.py`
- `tests/test_balance_intelligence.py` (new)

Add tests:
1. Ledger entry parsing and DOGE-eq conversion for DOGE/USD deposit/withdrawal.
2. Dedup behavior by `ledger_id`.
3. Cursor advancement and no-backfill bootstrap behavior.
4. Baseline adjustment audit trail entries.
5. Recon adjusted-drift math in both:
   - `FLOW_BASELINE_AUTO_ADJUST=true`
   - `FLOW_BASELINE_AUTO_ADJUST=false`
6. Snapshot save/load round-trip for new flow fields.
7. Equity persistence trim and downsample behavior.
8. Status payload includes `external_flows` and `equity_history` blocks.

Verification run:
1. `python3 -m unittest tests.test_balance_intelligence`
2. `python3 -m unittest tests.test_hardening_regressions`

## Rollout Plan

1. Stage 1 (observe-only):
   - `FLOW_DETECTION_ENABLED=true`
   - `FLOW_BASELINE_AUTO_ADJUST=false`
   - validate detected flows vs Kraken account activity for 24-48h.
2. Stage 2 (auto-adjust on):
   - `FLOW_BASELINE_AUTO_ADJUST=true`
   - verify `adjusted_drift_pct` normalizes after known deposits/withdrawals.
3. Stage 3 (persistent 7d history):
   - `EQUITY_TS_ENABLED=true`
   - validate `__equity_ts__` retention trim and dashboard 7d continuity.
4. Stage 4 (UI rollout):
   - enable chart/flow panels and monitor payload size/render latency.

Operational metrics to watch:
1. `external_flows.last_poll_age_sec`
2. `external_flows.flow_count` and recent flow correctness
3. `balance_recon.adjusted_drift_pct`
4. Supabase write/read warnings for `__equity_ts__`

## Rollback Plan

1. Set `FLOW_DETECTION_ENABLED=false` and `EQUITY_TS_ENABLED=false`.
2. Keep legacy recon fields active (already backward-compatible).
3. Hide/disable dashboard flow/equity widgets if needed while leaving core status endpoint intact.

## Definition of Done

1. `kraken_client.get_ledgers()` exists and is budget-safe.
2. Runtime detects/de-dups external deposits/withdrawals and records audit trail.
3. Recon includes flow-aware adjusted metrics without breaking existing keys.
4. 7-day equity history persists/reloads via `bot_state` key `__equity_ts__`.
5. Dashboard shows corrected recon breakdown plus 24h/7d equity chart with flow markers.
6. New tests pass and hardening regressions remain green.
