# Balance Intelligence Spec v0.1.0

## Summary

The current balance reconciliation is a two-point comparison: a baseline snapshot vs. now. It cannot distinguish deposits from organic growth, has no memory between cycles, and throws away every data point except the latest. When the operator deposits $50 of DOGE, the recon shows +$50 "drift" — which is correct math but useless information.

This spec adds three capabilities:

1. **External flow detection** via Kraken's Ledger API — deposits and withdrawals are identified, timestamped, and subtracted from drift so the recon reflects *only* bot activity and market movement.
2. **Persistent equity time-series** — DOGE-equivalent snapshots written to Supabase at 5-minute intervals, enabling historical charts and trend analysis.
3. **Baseline auto-adjustment** — when an external flow is detected, the baseline is adjusted by the flow amount so drift stays clean without manual resets.

## Locked Decisions

1. **Kraken Ledger API is the source of truth** for deposits/withdrawals. No manual annotation, no heuristic detection from balance jumps.
2. **DOGE-equivalent is the unit of account** for all time-series and recon math (consistent with existing `doge_eq` conventions).
3. **Supabase is the persistence layer** for time-series. No new tables — use the existing `bot_state` key-value store with a dedicated key prefix.
4. **No behavioral changes** — this is pure observability. No trading logic, sizing, or entry/exit changes.
5. **Polling frequency**: Ledger API queried once per 5 minutes (12 calls/hour). Rate cost: 1 private call per poll.
6. **In-memory deque + periodic flush** — snapshots accumulate in memory and batch-write to Supabase every 5 minutes (same cadence as snapshot interval).

## Scope

### In

1. `get_ledgers()` method in `kraken_client.py` wrapping `/0/private/Ledgers`.
2. External flow detection and tracking (deposits, withdrawals) in `bot.py`.
3. Baseline auto-adjustment on detected external flows.
4. Persistent DOGE-eq time-series (Supabase + local fallback).
5. Enhanced `balance_recon` payload with flow-adjusted drift.
6. Status API additions: flow history, time-series data, adjusted metrics.
7. Dashboard: equity chart (sparkline → real chart), deposit/withdrawal markers, corrected recon display.

### Out

1. Kraken `TradeBalance` endpoint integration (internal ledger sufficient).
2. Tax reporting or cost-basis tracking.
3. Multi-asset balance tracking (DOGE/USD only).
4. Historical backfill of flows before bot start.
5. Alerting or notifications on drift.
6. Any trading behavior changes.

---

## Data & Runtime Design

### 1) Kraken Ledger API Integration

New method in `kraken_client.py`:

```python
LEDGER_PATH = "/0/private/Ledgers"

def get_ledgers(
    type_: str = "all",
    asset: str = "all",
    start: float | None = None,
    end: float | None = None,
    ofs: int | None = None,
) -> dict:
    """
    Query Kraken ledger entries.
    Returns {"count": int, "ledger": {id: entry, ...}}
    Each entry: {aclass, amount, asset, balance, fee, refid, time, type, subtype}
    Rate cost: 1 private call. Returns 50 entries per page (most recent first).
    """
```

The bot only queries `type_="deposit"` and `type_="withdrawal"` — never `"trade"` (too noisy, and we already track trades internally).

### 2) External Flow Record

```python
@dataclass(frozen=True)
class ExternalFlow:
    ledger_id: str        # Kraken ledger entry ID (dedup key)
    flow_type: str        # "deposit" | "withdrawal"
    asset: str            # "XXDG" | "ZUSD" | etc.
    amount: float         # Signed: positive for deposit, negative for withdrawal
    fee: float            # Kraken fee on the flow
    timestamp: float      # Unix timestamp from Kraken
    doge_eq: float        # DOGE-equivalent at detection price
    price_at_detect: float  # Price used for DOGE-eq conversion
```

### 3) Flow Tracker State (in BotRuntime)

```python
# New fields on BotRuntime.__init__
self._external_flows: list[ExternalFlow] = []        # All detected flows (persisted)
self._flow_ledger_cursor: float = 0.0                # Last-seen ledger timestamp
self._flow_poll_interval: float = 300.0              # 5 minutes
self._flow_last_poll_ts: float = 0.0                 # Last poll timestamp
self._flow_seen_ids: set[str] = set()                # Dedup set (ledger IDs)
self._baseline_adjustments: list[dict] = []          # Audit trail of adjustments
```

### 4) Flow Detection Algorithm

Each main-loop cycle (throttled to once per `FLOW_POLL_INTERVAL_SEC`):

```
1. If now - _flow_last_poll_ts < FLOW_POLL_INTERVAL_SEC: skip
2. Query Kraken: get_ledgers(type_="deposit", start=_flow_ledger_cursor)
3. Query Kraken: get_ledgers(type_="withdrawal", start=_flow_ledger_cursor)
   (2 private API calls, or combine with type_="all" and filter — 1 call)
4. For each entry not in _flow_seen_ids:
   a. Create ExternalFlow record
   b. Compute doge_eq: if asset is USD, doge_eq = amount / price; if DOGE, doge_eq = amount
   c. Adjust baseline: _recon_baseline["doge"] += doge_eq (deposit) or -= abs(doge_eq) (withdrawal)
   d. Log adjustment to _baseline_adjustments audit trail
   e. Add ledger_id to _flow_seen_ids
5. Update _flow_ledger_cursor = max(entry timestamps) + 1
6. Update _flow_last_poll_ts = now
```

**Rate budget**: 1 private call per poll (use `type_="all"`, filter client-side for deposit+withdrawal). At 5-min intervals = 12 calls/hour = 0.2 calls/min. Well within budget.

**Optimization**: Query with `type_="all"` but `asset="ZUSD"` first, then `asset="XXDG"`. Or just query `type_="all"` unfiltered and client-filter. Since we only care about deposit/withdrawal entries, the 50-per-page limit is unlikely to be hit (deposits are rare events).

### 5) Baseline Auto-Adjustment

When an external flow is detected:

```python
# Deposit of X USD at price P
doge_eq_adjustment = amount_usd / current_price
self._recon_baseline["usd"] += amount_usd
# OR equivalently adjust the DOGE-eq baseline:
# baseline_doge_eq += doge_eq_adjustment

# Withdrawal of X DOGE
self._recon_baseline["doge"] -= amount_doge
```

The baseline stores raw `{usd, doge, ts}`. Adjustments modify the raw values so that `baseline_doge_eq` (computed as `baseline.doge + baseline.usd / price`) shifts by exactly the flow amount.

**Audit entry**:
```python
{
    "ts": now,
    "ledger_id": flow.ledger_id,
    "flow_type": flow.flow_type,
    "asset": flow.asset,
    "amount": flow.amount,
    "doge_eq_adjustment": doge_eq,
    "baseline_before": {"usd": old_usd, "doge": old_doge},
    "baseline_after": {"usd": new_usd, "doge": new_doge},
    "price": current_price,
}
```

### 6) Persistent Equity Time-Series

Currently `_doge_eq_snapshots` is an in-memory deque trimmed to 24h. This spec extends it:

**In-memory buffer** (unchanged role, extended retention):
```python
self._doge_eq_snapshots: deque[tuple[float, float]] = deque()  # (ts, doge_eq)
```

**Persistent storage** — Supabase key `__equity_ts__`:
```python
# Written every EQUITY_SNAPSHOT_FLUSH_SEC (300s default)
# Format: list of snapshot records
{
    "snapshots": [
        {"ts": 1771288800.0, "doge_eq": 833616.7, "usd": 94.37, "doge": 832678.56, "price": 0.10058, "bot_pnl_usd": 3.18, "flows_cumulative_doge_eq": 6000.0},
        ...
    ],
    "cursor": 1771288800.0,  # Latest snapshot ts (for resume)
    "version": 1
}
```

**Retention policy**:
- In-memory: 24h rolling (existing behavior, unchanged)
- Supabase: 7 days rolling. On each flush, trim entries older than 7 days.
- Resolution: 5 minutes (288 points/day, ~2016 points for 7 days)

**Flush logic**:
```
1. Every EQUITY_SNAPSHOT_FLUSH_SEC:
2. Collect new snapshots since last flush
3. Load existing from Supabase (or start fresh)
4. Append new, trim > 7 days old
5. Write back to Supabase key "__equity_ts__"
6. Also write to local logs/equity_ts.json as fallback
```

### 7) Enhanced Recon Payload

Current `balance_recon` in status:
```json
{
    "status": "OK",
    "baseline_doge_eq": 827560.47,
    "current_doge_eq": 833616.73,
    "account_growth_doge": 6056.26,
    "bot_pnl_doge": -14.25,
    "drift_doge": 6070.51,
    "drift_pct": 0.73,
    "threshold_pct": 2.0,
    "baseline_ts": 1770996313.1,
    "price": 0.10058,
    "simulated": false
}
```

New fields added:
```json
{
    "...existing fields...",

    "external_flows_doge_eq": 6000.0,
    "external_flow_count": 2,
    "adjusted_drift_doge": 70.51,
    "adjusted_drift_pct": 0.0085,
    "adjusted_status": "OK",
    "baseline_adjustments_count": 2,
    "last_flow_ts": 1771100000.0,
    "last_flow_type": "deposit",
    "last_flow_amount": 5000.0,
    "last_flow_asset": "XXDG",
    "flow_poll_age_sec": 45.2,
    "flow_poll_ok": true
}
```

**Key new metric**: `adjusted_drift_doge` = `drift_doge - external_flows_doge_eq`. This is the *true* unexplained drift after accounting for deposits/withdrawals.

### 8) Status API Additions

New top-level block in status payload:

```json
"equity_history": {
    "enabled": true,
    "snapshots_in_memory": 288,
    "snapshots_persisted": 2016,
    "oldest_persisted_ts": 1770700000.0,
    "newest_persisted_ts": 1771288800.0,
    "span_hours": 163.5,
    "flush_age_sec": 120.3,
    "flush_interval_sec": 300,
    "sparkline_24h": [833100.0, 833200.0, "..."],
    "sparkline_7d": [830000.0, 831000.0, "..."],
    "doge_eq_change_1h": 15.2,
    "doge_eq_change_24h": -200.5,
    "doge_eq_change_7d": 3500.0
},
"external_flows": {
    "enabled": true,
    "poll_interval_sec": 300,
    "last_poll_ts": 1771288700.0,
    "last_poll_age_sec": 100.0,
    "total_deposits_doge_eq": 6000.0,
    "total_withdrawals_doge_eq": 0.0,
    "net_flows_doge_eq": 6000.0,
    "flow_count": 2,
    "recent_flows": [
        {
            "ledger_id": "L4ABC-DEFGH-IJKLMN",
            "type": "deposit",
            "asset": "XXDG",
            "amount": 5000.0,
            "doge_eq": 5000.0,
            "fee": 0.0,
            "ts": 1771100000.0,
            "baseline_adjusted": true
        }
    ]
}
```

`sparkline_7d` is downsampled: take every 6th point from the 7-day series (30-min resolution, ~336 points) to keep the payload manageable.

---

## Config Constants

| Var | Default | Type | Description |
|-----|---------|------|-------------|
| `FLOW_DETECTION_ENABLED` | `True` | bool | Master toggle for Kraken Ledger polling |
| `FLOW_POLL_INTERVAL_SEC` | `300` | float | How often to poll Kraken Ledger API |
| `FLOW_BASELINE_AUTO_ADJUST` | `True` | bool | Auto-adjust baseline on detected flows |
| `EQUITY_TS_ENABLED` | `True` | bool | Enable persistent equity time-series |
| `EQUITY_SNAPSHOT_INTERVAL_SEC` | `300` | float | In-memory snapshot interval (existing, renamed) |
| `EQUITY_SNAPSHOT_FLUSH_SEC` | `300` | float | How often to flush to Supabase |
| `EQUITY_TS_RETENTION_DAYS` | `7` | int | How many days to retain in persistent store |
| `EQUITY_TS_SPARKLINE_7D_STEP` | `6` | int | Downsample factor for 7d sparkline (every Nth point) |

---

## Bot.py Integration Points

### 1. Initialization (`__init__`)

Add flow tracker state fields (section 3 above). Load persisted flows and cursor from snapshot.

### 2. Snapshot Save/Load (`_global_snapshot` / `_load_snapshot`)

Persist and restore:
- `_external_flows` (list of ExternalFlow dicts)
- `_flow_ledger_cursor`
- `_flow_seen_ids`
- `_baseline_adjustments`
- `_recon_baseline` (already persisted — now includes adjustments)

### 3. Main Loop — Flow Poll

After existing balance fetch, before status computation:
```python
if self._should_poll_flows(now):
    self._poll_external_flows(now)
```

New method `_poll_external_flows(now)`:
- Calls `kraken_client.get_ledgers()`
- Processes entries, creates ExternalFlow records
- Adjusts baseline
- Updates cursor and dedup set

### 4. Main Loop — Equity Flush

After existing `_update_doge_eq_snapshot()`:
```python
if self._should_flush_equity_ts(now):
    self._flush_equity_ts(now)
```

New method `_flush_equity_ts(now)`:
- Collects snapshots since last flush
- Enriches with USD, DOGE, price, bot P&L, cumulative flows
- Writes to Supabase key `__equity_ts__`
- Writes to `logs/equity_ts.json` as fallback

### 5. Recon Computation (`_compute_balance_recon`)

After existing drift calculation, add:
```python
total_flow_doge_eq = sum(f.doge_eq for f in self._external_flows)
adjusted_drift = drift - total_flow_doge_eq
adjusted_drift_pct = (adjusted_drift / baseline_doge_eq * 100.0) if baseline_doge_eq > 0 else 0.0
adjusted_status = "OK" if abs(adjusted_drift_pct) <= threshold else "DRIFT"
```

### 6. Status Payload

Add `equity_history` and `external_flows` blocks (section 8 above) to the status dict returned by `_build_status_payload()`.

---

## Dashboard Changes

### 1. Equity Chart (replaces sparkline)

Replace the tiny SVG sparkline in the "DOGE Bias Scoreboard" with a proper time-series chart:

- **24h view** (default): 5-min resolution, from in-memory snapshots
- **7d view** (toggle): 30-min resolution, from persisted data
- **Y-axis**: DOGE-equivalent account value
- **Markers**: Vertical dashed lines at deposit/withdrawal events, with tooltip showing flow type and amount
- **Bot P&L overlay** (optional toggle): Thin line showing cumulative bot P&L in DOGE-eq

Implementation: SVG-based (no external chart library — consistent with zero-deps dashboard). Canvas polyline with axis labels and event markers.

### 2. Balance Recon Card (enhanced)

Current display:
```
Status: OK
Account Value: 833,616 DOGE
Baseline: 827,560 DOGE
Growth: +6,056 DOGE
Bot P&L: -14.25 DOGE
Drift: +6,071 DOGE (+0.73%)
```

New display:
```
Status: OK ✓
Account Value: 833,616 DOGE eq

Growth Breakdown:
  Deposits:     +6,000.0 DOGE eq  (2 flows)
  Bot P&L:         -14.3 DOGE eq  (219 trips)
  Unexplained:     +70.5 DOGE eq  (+0.009%)

Baseline: 827,560 DOGE eq (81.3h ago, 2 adjustments)
Last Flow: deposit 5,000 DOGE — 3d 2h ago
```

The key change: **"Drift" becomes "Unexplained"** and is tiny because deposits are accounted for. The operator can immediately see that the account grew because they deposited, not because the bot printed money.

### 3. Flow History Panel

Small collapsible panel below the recon card:

```
External Flows (2)
  ▸ deposit  5,000 DOGE          Feb 13 14:22
  ▸ deposit  $100.00 → 1,000 eq  Feb 12 09:15
```

Each row: type icon, amount, asset, DOGE-eq, relative time. Click to expand shows ledger ID and baseline adjustment details.

---

## Persistence Schema

### Supabase Keys

| Key | Content | Write Frequency |
|-----|---------|-----------------|
| `__v1__` | Existing global state (now includes flow tracker fields) | Every save cycle (~30s) |
| `__equity_ts__` | Time-series snapshots (7d rolling) | Every 5 minutes |

### Local Files

| File | Content | Write Frequency |
|------|---------|-----------------|
| `logs/state.json` | Existing (now includes flow tracker fields) | Every save cycle |
| `logs/equity_ts.json` | Time-series fallback | Every 5 minutes |

### Snapshot Fields Added to `__v1__`

```python
{
    "...existing fields...",
    "external_flows": [
        {"ledger_id": "...", "flow_type": "deposit", "asset": "XXDG", "amount": 5000.0, "fee": 0.0, "timestamp": 1771100000.0, "doge_eq": 5000.0, "price_at_detect": 0.10058}
    ],
    "flow_ledger_cursor": 1771100001.0,
    "flow_seen_ids": ["L4ABC-DEFGH-IJKLMN"],
    "baseline_adjustments": [
        {"ts": 1771100005.0, "ledger_id": "L4ABC-DEFGH-IJKLMN", "flow_type": "deposit", "doge_eq_adjustment": 5000.0, "baseline_before": {"usd": 100.0, "doge": 827000.0}, "baseline_after": {"usd": 100.0, "doge": 832000.0}, "price": 0.10058}
    ]
}
```

---

## Testing

### Unit

1. `ExternalFlow` creation and DOGE-eq computation (USD deposit, DOGE deposit, withdrawal).
2. Baseline adjustment math: deposit adds, withdrawal subtracts, audit trail recorded.
3. Adjusted drift calculation: `drift - flows = unexplained`.
4. Dedup: same ledger ID processed twice → no double-adjustment.
5. Cursor advancement: only processes entries newer than cursor.
6. Time-series trim: entries older than retention window are dropped.
7. Sparkline downsampling: 7d series at correct step size.

### Integration

1. Flow detection → baseline adjustment → recon shows adjusted drift near zero after deposit.
2. Equity flush → Supabase write → reload on restart → series continuous.
3. Multiple flows in same poll batch → all processed, baseline adjusted for each.
4. Kraken Ledger API failure → graceful skip, retry next interval, no crash.
5. Flow detection disabled → existing behavior unchanged.

### Regression

1. Existing `balance_recon` fields unchanged (backward compatible).
2. `doge_eq_sparkline` still works (in-memory deque unchanged).
3. Rate budget stays within limits (1 extra call per 5 min).
4. Snapshot save/load round-trips correctly with new fields.

---

## Edge Cases

1. **Bot starts after a deposit**: First ledger poll catches it. If baseline was already captured without the deposit, the adjustment corrects it retroactively. If baseline was captured *after* the deposit, the flow is recorded but drift is already zero — adjustment is harmless (adjusting baseline to match what's already there).

2. **Multiple deposits between polls**: All caught in single poll (Kraken returns 50 entries, deposits are rare). Each processed individually.

3. **USD deposit followed by manual DOGE buy**: Ledger shows `deposit` (USD in) and `trade` (USD→DOGE). We only process the `deposit` entry. The trade doesn't change total account value (just rebalances USD↔DOGE), so the baseline adjustment for the deposit alone is correct.

4. **Withdrawal**: Same logic in reverse. Baseline decreases. If operator withdraws DOGE and the recon was showing "growth", the withdrawal is subtracted, showing the true bot-only trajectory.

5. **Price change between flow and detection**: The `doge_eq` is computed at detection price, not flow price. For DOGE flows this doesn't matter (1 DOGE = 1 DOGE). For USD flows, there's a small error proportional to price change over the poll interval (5 min). Acceptable — the flow amount in USD is exact, and the DOGE-eq is an approximation anyway.

6. **Kraken Ledger API unavailable**: Skip poll, retry next interval. Log warning. `flow_poll_ok` flag in status goes false. No baseline adjustment. Recon falls back to existing (unadjusted) behavior.

---

## Rollout

### Stage 1: Ledger Polling + Flow Detection (observe only)

- Enable `FLOW_DETECTION_ENABLED=true`
- Set `FLOW_BASELINE_AUTO_ADJUST=false` (detect but don't adjust)
- Observe `external_flows` in status payload
- Verify detected flows match actual Kraken activity
- Duration: 24-48h

### Stage 2: Baseline Auto-Adjust

- Enable `FLOW_BASELINE_AUTO_ADJUST=true`
- Verify `adjusted_drift_pct` is near zero after known deposits
- Verify audit trail in `baseline_adjustments`
- Duration: 48h

### Stage 3: Persistent Time-Series

- Enable `EQUITY_TS_ENABLED=true`
- Verify Supabase writes under `__equity_ts__`
- Verify 7d retention trim works
- Verify sparkline_7d renders in dashboard
- Duration: 7d (to confirm full retention cycle)

### Stage 4: Dashboard Chart

- Deploy equity chart replacing sparkline
- Verify deposit/withdrawal markers render
- Verify 24h/7d toggle works

---

## Assumptions

1. Kraken Ledger API is available on current API key permissions (requires "Data - Query ledger entries").
2. Deposits/withdrawals are rare events (< 10/day). 50-entry page limit is sufficient.
3. The bot's API key has not been used on other Kraken trading pairs — ledger entries are filtered by asset (ZUSD, XXDG) to avoid noise from unrelated activity.
4. Supabase row size limit accommodates 7 days of 5-min snapshots (~2016 entries, ~200KB JSON).

## Related Specs

- [CAPACITY_TELEMETRY_SPEC.md](CAPACITY_TELEMETRY_SPEC.md) — rate budget accounting
- [STRATEGIC_CAPITAL_DEPLOYMENT_SPEC.md](STRATEGIC_CAPITAL_DEPLOYMENT_SPEC.md) — accumulation engine (generates trades, not external flows)
- [DUST_PROOF_LEDGER_SPEC.md](DUST_PROOF_LEDGER_SPEC.md) — planned actual-fee capture from Kraken orders (complementary)
