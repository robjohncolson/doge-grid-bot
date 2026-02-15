# Kelly Deploy Checklist

Last updated: 2026-02-15

Use this checklist for safe Kelly rollout.

## 1. Pre-Deploy (Schema + Data)

Run migration:

```sql
\i docs/kelly_regime_at_entry_migration.sql
```

Verify column type:

```sql
SELECT data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'exit_outcomes'
  AND column_name = 'regime_at_entry';
```

Expected: `integer` (or bigint/smallint compatible integer family).

Verify no legacy text labels remain:

```sql
SELECT COUNT(*) AS legacy_label_rows
FROM public.exit_outcomes
WHERE regime_at_entry IS NOT NULL
  AND upper(trim(regime_at_entry::text)) IN ('BEARISH', 'RANGING', 'BULLISH');
```

Expected: `0`.

Verify no out-of-range values:

```sql
SELECT COUNT(*) AS invalid_regime_rows
FROM public.exit_outcomes
WHERE regime_at_entry IS NOT NULL
  AND regime_at_entry NOT IN (0, 1, 2);
```

Expected: `0`.

## 2. Deploy Phase A (`KELLY_ENABLED=false`)

Set env:

```bash
KELLY_ENABLED=false
```

Deploy and verify `/api/status`:

```bash
curl -sS http://<host>:<port>/api/status | jq '.kelly'
```

Expected:

```json
{"enabled": false}
```

Verify new `exit_outcomes` rows are being tagged with entry-time regime IDs:

```sql
SELECT
  to_timestamp(time) AS ts,
  trade,
  cycle,
  regime_at_entry,
  regime_tier,
  regime_confidence,
  regime_bias_signal
FROM public.exit_outcomes
ORDER BY time DESC
LIMIT 25;
```

Expected:
- `regime_at_entry` is `0`, `1`, `2`, or `NULL` (when HMM is unavailable/disabled).
- No text values.

## 3. Deploy Phase B (`KELLY_ENABLED=true`)

Enable Kelly:

```bash
KELLY_ENABLED=true
```

Recommended starting defaults:

```bash
KELLY_FRACTION=0.25
KELLY_MIN_SAMPLES=30
KELLY_MIN_REGIME_SAMPLES=15
KELLY_LOOKBACK=500
KELLY_FLOOR_MULT=0.5
KELLY_CEILING_MULT=2.0
KELLY_NEGATIVE_EDGE_MULT=0.5
KELLY_RECENCY_WEIGHTING=true
KELLY_RECENCY_HALFLIFE=100
KELLY_LOG_UPDATES=true
```

Verify runtime payload:

```bash
curl -sS http://<host>:<port>/api/status | jq '.kelly'
```

Health checks:

```bash
curl -sS http://<host>:<port>/api/status | jq '{
  enabled: .kelly.enabled,
  active_regime: .kelly.active_regime,
  last_update_n: .kelly.last_update_n,
  aggregate_ok: .kelly.aggregate.sufficient_data,
  aggregate_reason: .kelly.aggregate.reason,
  aggregate_mult: .kelly.aggregate.multiplier
}'
```

Expected:
- `enabled=true`
- `last_update_n` increases over time as cycles accumulate
- `aggregate.reason` transitions from `insufficient_samples` to `ok` after sample gate
- multiplier stays within floor/ceiling bounds

Optional per-regime readiness check:

```bash
curl -sS http://<host>:<port>/api/status | jq '{
  bullish: {n: .kelly.bullish.n_total, ok: .kelly.bullish.sufficient_data, reason: .kelly.bullish.reason},
  ranging: {n: .kelly.ranging.n_total, ok: .kelly.ranging.sufficient_data, reason: .kelly.ranging.reason},
  bearish: {n: .kelly.bearish.n_total, ok: .kelly.bearish.sufficient_data, reason: .kelly.bearish.reason}
}'
```

## 4. Rollback

If behavior is unexpected, set:

```bash
KELLY_ENABLED=false
```

This immediately returns sizing to pre-Kelly behavior while continuing to collect `regime_at_entry` tags for future re-enable.
