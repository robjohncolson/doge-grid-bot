# Manual Scaling + Capacity Telemetry Spec v0.1.1

## Summary

Keep slot growth manual (Add Slot button), and implement only read-path telemetry to show:

1. Capacity runway (how many slots you can still add),
2. Early partial-fill pressure,
3. Deterministic stop signals.

No auto-spawn logic, no orphan automation, no trading behavior changes.

## Locked Decisions

1. Metric storage: in-memory rolling 24h windows.
2. open_orders_current source: Kraken-first (pair-filtered), with internal fallback when Kraken data unavailable.
3. Orphan policy: unchanged (lottery tickets, manual soft-close only).

## Scope

### In

1. Status telemetry fields for capacity/fill health.
2. Rolling partial-fill counters.
3. Fill-time stats from existing order timestamps.
4. Dashboard "Capacity & Fill Health" card (read-only).
5. Operator stop-rule guidance.

### Out

1. Auto-spawn governor.
2. Reserve/haircut math.
3. Auto orphan sweep/cull.
4. Vertical sizing automation.
5. Reducer/state-machine behavior changes.

## Data & Runtime Design

### 1) New Runtime Telemetry State (in BotRuntime)

Add in-memory rolling buffers (timestamped events), trimmed to 24h:

1. `partial_fill_open_events`: `deque[float]`
2. `partial_fill_cancel_events`: `deque[float]`
3. `fill_durations_sec`: `deque[tuple[timestamp, duration_sec]]`

Utilities:

1. `_trim_rolling_24h(now)` for all deques.
2. `_count_24h(deque)` helper.
3. `_percentile_24h(fill_durations, p)` for median/p95.

Persistence: None (resets on restart by design).

### 2) Open Order Count Source (Kraken-first)

Status path computes:

1. `kraken_open_orders_current` from latest Kraken open-order snapshot for pair.
2. `internal_open_orders_current` = active_orders + recovery_orders.
3. `open_orders_current` = kraken value if available else internal value.
4. `open_orders_source` = `"kraken"` | `"internal_fallback"`.

Also expose divergence:

1. `open_orders_internal`
2. `open_orders_kraken` (nullable if unavailable)
3. `open_orders_drift` = kraken - internal (nullable)

### 3) Capacity Math

Config:

1. `KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT` default 225 (operator-editable).
2. `OPEN_ORDER_SAFETY_RATIO` default 0.75.

Derived:

1. `open_orders_safe_cap` = floor(limit * ratio)
2. `open_order_headroom` = open_orders_safe_cap - open_orders_current
3. `open_order_utilization_pct` = open_orders_current / open_orders_safe_cap * 100

Runway estimate:

1. `orders_per_slot` = open_orders_current / slot_count (if slot_count > 0 else null)
2. `estimated_slots_remaining` = floor(open_order_headroom / max(orders_per_slot, 1)) when headroom > 0 else 0
3. Expose as advisory estimate only.

### 4) Partial-Fill Detection Hooks

In `_poll_order_status` loop:

1. For `status == "open"`:
   - parse vol_exec, vol
   - if `0 < vol_exec < vol`, append timestamp to `partial_fill_open_events`
   - deduplicate per txid via `_partial_open_seen_txids` set

2. For `status in ("canceled", "expired")`:
   - parse vol_exec
   - if `vol_exec > 0`, append timestamp to `partial_fill_cancel_events`
   - emit warning log marker: `PHANTOM_POSITION_CANARY` with txid, vol_exec, vol

No trade-action change in v0.1.1 (count/alert only).

### 5) Fill-Time Metrics

Use existing `OrderState.placed_at` and close timestamp:

1. On closed tracked order, compute `duration_sec = now - order.placed_at` when `placed_at > 0`.
2. Append `(now, duration_sec)` to rolling fill-duration deque.
3. Expose:
   - `median_fill_seconds_1d`
   - `p95_fill_seconds_1d`

## Public Interface Changes

### /api/status additions

Add top-level `capacity_fill_health` object:

| Field | Type | Description |
|-------|------|-------------|
| `open_orders_current` | int | Best-available open order count |
| `open_orders_source` | string | `"kraken"` or `"internal_fallback"` |
| `open_orders_internal` | int | Bot-tracked active + recovery count |
| `open_orders_kraken` | int\|null | Kraken-reported pair-filtered count |
| `open_orders_drift` | int\|null | kraken - internal (reconciliation health) |
| `open_order_limit_configured` | int | From config |
| `open_orders_safe_cap` | int | floor(limit * safety_ratio) |
| `open_order_headroom` | int | safe_cap - current |
| `open_order_utilization_pct` | float | current / safe_cap * 100 |
| `orders_per_slot_estimate` | float\|null | current / slot_count |
| `estimated_slots_remaining` | int | floor(headroom / orders_per_slot) |
| `partial_fill_open_events_1d` | int | Rolling 24h count |
| `partial_fill_cancel_events_1d` | int | Rolling 24h count (canary) |
| `median_fill_seconds_1d` | float\|null | Median fill time |
| `p95_fill_seconds_1d` | float\|null | 95th percentile fill time |
| `status_band` | string | `"normal"` \| `"caution"` \| `"stop"` |
| `blocked_risk_hint` | string[] | Human-readable risk signals |

Band rules:

1. **normal**: headroom >= 20 and no partial-cancel events.
2. **caution**: headroom 10-19 and no partial-cancel events.
3. **stop**: headroom < 10 OR `partial_fill_cancel_events_1d > 0`.

## Dashboard Changes

Add a read-only "Capacity & Fill Health" card in Summary region:

1. Current orders / safe cap / utilization.
2. Headroom + estimated slots remaining.
3. 1d partial-open and partial-cancel counts.
4. Median/p95 fill time.
5. Band badge (NORMAL/CAUTION/STOP) + short reason text.

No new actions, keybindings, or modal flows.

## Operator Playbook (Deterministic)

1. Add slots freely in **normal** band.
2. In **caution**, add at most 1 slot/day.
3. In **stop**, do not add slots.
4. If `partial_fill_cancel_events_1d > 0`, freeze scaling and prioritize v0.2 partial-cancel handling.
5. Resume scaling only after 72h continuously back in **normal**.

## Testing

### Unit

1. Rolling-window trim logic.
2. Percentile calculations (median/p95).
3. Band classification thresholds.
4. Runway estimate math and null/zero edge cases.
5. Kraken-first + fallback source selection.

### Integration

1. Simulated open partials increment `partial_fill_open_events_1d`.
2. Simulated cancel-after-partial increments `partial_fill_cancel_events_1d` and logs canary.
3. Status payload contains all fields with correct nullability.
4. Kraken unavailable path correctly switches to internal fallback.
5. No changes to order placement/cancel behavior.

### Regression

1. Existing status keys unchanged.
2. Existing dashboard features unaffected.
3. No reducer invariant/test regressions.

## Milestones

| Phase | Scope | Status |
|-------|-------|--------|
| T1 | Backend telemetry core: runtime counters + status fields | **Complete** |
| T2 | Dashboard card: render capacity/fill health panel | **Complete** |
| T3 | Baseline observation (7 days): collect steady-state metrics | Pending |

## Rollout

1. Deploy telemetry only.
2. Run 7 days and establish baseline fill-time metrics.
3. Scale manually to plateau using stop rules.
4. Reassess after plateau:
   - If no partial-cancel events: continue manual steady state.
   - If events occur: prioritize v0.2 partial-fill handling before any vertical sizing experiments.

## Assumptions

1. Account is likely Pro-tier; configured limit defaults to 225 but remains operator-set.
2. Manual slot scaling remains primary control.
3. Orphans are intentional lottery inventory unless operator manually soft-closes.
4. v0.2 is triggered by real partial-cancel signals or sustained stop-band pressure.

## Related Specs

- `docs/DASHBOARD_UX_SPEC.md` — Capacity card is an addition to the Summary panel (section 3.4, panel 1).
- `STATE_MACHINE.md` — No changes required; telemetry is read-only over existing state.

## Known Deferred Issue

The cancel-after-partial path (`bot.py:1050-1065`) counts the phantom event but does **not** handle the stranded position. If `partial_fill_cancel_events_1d > 0` is observed in production, the v0.2 fix must:

1. Read `vol_exec` from the canceled order.
2. Create a recovery order for the executed volume.
3. Place a new entry for only the unfilled remainder (if applicable).

This is deferred because at current order sizes (13 DOGE / ~$1.17), partial fills do not occur.
