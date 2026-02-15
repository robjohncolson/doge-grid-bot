# Task: Implement Vintage Data Collection for Sticky Slots

## Context

We just deployed "Sticky Slots" — a major architectural change to the DOGE/USD grid bot on Kraken (`XDGUSD`). The core idea: exits that don't fill quickly are no longer orphaned and force-closed. They sit patiently ("sticky") until they fill naturally or are explicitly released. The old orphan/recovery system is being phased out.

Before we can enable advanced features (momentum gating, regime-adaptive parameters, auto-release tiers), we need **2+ weeks of vintage data** — a historical record of how long each exit sits before filling, what market conditions existed during that time, and whether the exit eventually filled profitably or was released at a loss.

This data validates the entire sticky thesis and informs future tuning decisions.

## Codebase Orientation

Primary files (read these first for full context):

- `state_machine.py` — pure reducer, `transition()` function, `PairState` data model
- `bot.py` — runtime: exchange I/O, main loop, reconciliation, persistence, APIs
- `config.py` — all configuration knobs
- `dashboard.py` — HTTP server + dashboard HTML
- `supabase_store.py` — Supabase persistence layer
- `docs/STATE_MACHINE.md` — implementation contract (read §5 for phases, §7 for event rules, §19 for persistence)
- `docs/STICKY_SLOTS_SPEC.md` — the sticky slots design (read §9 for vintage telemetry spec, §4.2 for release gates)

Key data model concepts:

- **Slot**: independent trading unit with A-side (short) and B-side (long) legs
- **Phases**: S0 (entry), S1a (A exit waiting), S1b (B exit waiting)
- **PairState**: per-slot state including `orders`, `completed_cycles`, cycle counters
- **Each order** has: `local_id`, `txid`, `side`, `order_type`, `price`, `volume`, `created_at`
- **Each completed cycle** has: `entry_price`, `exit_price`, `entry_time`, `exit_time`, `net_profit`, `trade` (A or B), `from_recovery`

Persistence:

- Bot state snapshots go to Supabase table `bot_state` (key `__v1__`)
- Fill events go to Supabase table `fills`
- Bot events go to Supabase table `bot_events`
- Price history goes to Supabase table `price_history`

## What to Build

### 1. New Supabase Table: `exit_vintage_log`

Create a time-series log table that records a snapshot of every waiting exit on each main loop tick (or at a configurable interval, default every 5 minutes to avoid write spam).

Schema:

```sql
CREATE TABLE exit_vintage_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    slot_id     INTEGER NOT NULL,
    trade       TEXT NOT NULL,           -- 'A' or 'B'
    exit_txid   TEXT,                    -- Kraken order txid
    exit_price  NUMERIC NOT NULL,
    entry_price NUMERIC NOT NULL,
    market_price NUMERIC NOT NULL,
    age_sec     NUMERIC NOT NULL,        -- seconds since exit was placed
    distance_pct NUMERIC NOT NULL,       -- abs(exit_price - market_price) / market_price * 100
    direction   TEXT NOT NULL,           -- 'favorable' or 'adverse' (is market moving toward or away from exit)
    slot_profit_total NUMERIC,           -- slot's cumulative total_profit at snapshot time
    created_at  TIMESTAMPTZ NOT NULL     -- when the exit order was originally placed
);

-- Index for time-range queries
CREATE INDEX idx_exit_vintage_ts ON exit_vintage_log (ts);
-- Index for per-slot queries
CREATE INDEX idx_exit_vintage_slot ON exit_vintage_log (slot_id, trade, ts);
```

### 2. New Supabase Table: `exit_outcomes`

When an exit resolves (either fills naturally or is released), log the final outcome. This is the table we'll query to validate the sticky thesis.

Schema:

```sql
CREATE TABLE exit_outcomes (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    slot_id         INTEGER NOT NULL,
    trade           TEXT NOT NULL,
    exit_txid       TEXT,
    entry_price     NUMERIC NOT NULL,
    exit_price      NUMERIC NOT NULL,
    resolution      TEXT NOT NULL,        -- 'filled' or 'released'
    total_age_sec   NUMERIC NOT NULL,     -- total time exit was open
    net_profit_usd  NUMERIC NOT NULL,     -- profit (positive) or loss (negative)
    market_at_entry NUMERIC,              -- market price when entry filled (exit was placed)
    market_at_resolution NUMERIC,         -- market price when exit filled or was released
    max_adverse_pct NUMERIC,              -- worst-case distance from exit during lifetime (peak pain)
    max_favorable_pct NUMERIC             -- best-case distance (how close did market get to filling)
);

CREATE INDEX idx_exit_outcomes_ts ON exit_outcomes (ts);
CREATE INDEX idx_exit_outcomes_resolution ON exit_outcomes (resolution, ts);
```

### 3. Runtime: Vintage Snapshot Writer (in `bot.py`)

Add a periodic writer in the main loop that:

1. Runs every `VINTAGE_LOG_INTERVAL_SEC` (default: 300 seconds / 5 minutes)
2. Iterates all slots, finds exits in S1a/S1b phase
3. For each waiting exit, computes age, distance, direction
4. Writes a batch row to `exit_vintage_log` via `supabase_store`

Integration point: this runs in the main loop after the rebalancer update (step 12 in the lifecycle), using the same `self.last_price` that's already available. It should be lightweight — just a batch insert, no queries.

Add config:

```python
VINTAGE_LOG_ENABLED = True        # master switch
VINTAGE_LOG_INTERVAL_SEC = 300    # how often to snapshot
```

### 4. Runtime: Outcome Logger (in `bot.py` or `state_machine.py` event handling)

When an exit fill event is processed (in the `FillEvent` handler for exits):

1. Compute `total_age_sec` from exit order's `created_at` to now
2. Compute `net_profit_usd` (already available from `_book_cycle`)
3. Compute `max_adverse_pct` and `max_favorable_pct` — these require tracking per-exit extremes (see item 5 below)
4. Write to `exit_outcomes` with `resolution = 'filled'`

When a release action occurs (in the `release_slot` handler):

1. Same computation but `resolution = 'released'`
2. `net_profit_usd` is the synthetic close P&L

### 5. Per-Exit Extreme Tracking (in `PairState`)

To compute `max_adverse_pct` and `max_favorable_pct` at resolution time, we need to track the worst and best distances seen during the exit's lifetime.

Add two fields per active exit order in the slot state:

```python
peak_adverse_distance_pct: float = 0.0
peak_favorable_distance_pct: float = 0.0
```

Update these on each `PriceTick` event in the reducer:

```python
# For each active exit order:
distance = abs(exit_price - market_price) / market_price * 100
if market is moving away from exit:
    peak_adverse = max(peak_adverse, distance)
if market is moving toward exit:
    peak_favorable = max(peak_favorable, distance)  # closest approach
```

These fields must be persisted in the snapshot (add to PairState serialization).

Note: "favorable" means market moved closer to the exit price (toward filling). For a buy exit (A-side), favorable = market price dropping toward exit. For a sell exit (B-side), favorable = market price rising toward exit.

Actually, simpler: track `min_distance_pct` (closest the market got to filling) and `max_distance_pct` (farthest the market got from filling). These are more directly useful.

### 6. Real-Time Vintage Telemetry (in `/api/status` payload)

Add a `slot_vintage` block to the status payload (this is specified in STICKY_SLOTS_SPEC.md §9 but needs implementation):

```python
"slot_vintage": {
    "total_waiting_exits": N,
    "fresh_0_1h": N,
    "aging_1_6h": N,
    "stale_6_24h": N,
    "old_1_7d": N,
    "ancient_7d_plus": N,
    "oldest_exit_age_sec": N,
    "stuck_capital_usd": N.NN,
    "stuck_capital_pct": N.N,
    "mean_distance_pct": N.N,       -- average distance of all waiting exits from market
    "vintage_warn": bool,           -- any exit older than 3 days
    "vintage_critical": bool,       -- stuck_capital_pct > 50
    "vintage_release_eligible": N   -- count meeting all release gate criteria
}
```

This is computed live on each status request from current slot state — no database query needed.

### 7. Dashboard Display

Replace the orphan/recovery panel in the dashboard with the vintage health bar from §9.3 of the spec:

```
Slot Health    [████████░░░░░░░] 65% cycling | 35% waiting
Oldest Exit    2.3 days (slot 47)
Stuck Capital  $42.00 (21%)
```

Keep it simple. The vintage bucket breakdown can go in a tooltip or expandable section.

## What NOT to Build

- Do NOT modify the state machine reducer logic — vintage collection is read-only observation
- Do NOT implement release gates yet — that's a separate task
- Do NOT implement momentum gating or regime detection — those depend on vintage data analysis
- Do NOT add any new dashboard *actions* — this is pure telemetry/logging
- Do NOT add any Telegram commands for this — dashboard + API is sufficient

## Testing

- Verify `exit_vintage_log` rows are written at the configured interval
- Verify `exit_outcomes` rows are written when exits fill (check `completed_cycles` event flow)
- Verify `slot_vintage` block appears in `/api/status` response
- Verify peak distance tracking survives a snapshot save/load cycle
- Verify the vintage writer doesn't blow up when there are zero waiting exits
- Verify the vintage writer handles slots in S0 (no exits) gracefully
- Verify the log interval is respected (not writing every loop tick)

## Success Criteria

After deploying this, within 24 hours we should be able to query:

```sql
-- Distribution of exit ages for currently waiting exits
SELECT slot_id, trade, age_sec, distance_pct, direction
FROM exit_vintage_log
WHERE ts = (SELECT MAX(ts) FROM exit_vintage_log)
ORDER BY age_sec DESC;

-- Outcome distribution: how long do exits take to fill?
SELECT
    resolution,
    COUNT(*) as count,
    AVG(total_age_sec) as avg_age_sec,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_age_sec) as median_age_sec,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY total_age_sec) as p95_age_sec,
    AVG(net_profit_usd) as avg_profit
FROM exit_outcomes
GROUP BY resolution;

-- How often does market come close to filling but not quite?
SELECT
    resolution,
    AVG(min_distance_pct) as avg_closest_approach,
    AVG(max_distance_pct) as avg_peak_distance
FROM exit_outcomes
GROUP BY resolution;
```

This data directly answers: "Is patience profitable? How patient do we need to be? Where's the point of diminishing returns?"
