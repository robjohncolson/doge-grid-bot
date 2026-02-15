# Regime Shadow Runbook (Phase 0)

Use this after:
- `docs/supabase_v1_schema.sql` has been applied
- `REGIME_SHADOW_ENABLED=true`
- `REGIME_DIRECTIONAL_ENABLED=false`

## 0) Quick preflight

```bash
BASE_URL="http://127.0.0.1:${PORT:-8080}"
curl -s "$BASE_URL/api/status" | jq '{
  mode,
  pair,
  hmm_enabled: .hmm_regime.enabled,
  hmm_ready: .regime_directional.hmm_ready,
  regime_directional: .regime_directional
}'
```

Expected:
- `regime_directional.shadow_enabled = true`
- `regime_directional.actuation_enabled = false`

## 1) Step 3 verification: status payload fields

```bash
BASE_URL="http://127.0.0.1:${PORT:-8080}"
curl -s "$BASE_URL/api/status" | jq '{
  regime_directional: .regime_directional,
  slots: [.slots[] | {slot_id, phase, long_only, short_only, mode_source}]
}'
```

Pass conditions:
- `regime_directional` block is present and updating
- `slots[].mode_source` is only `none` or `balance` in shadow mode

## 2) Step 4 verification: smoke test (1-2h)

Run a lightweight monitor:

```bash
BASE_URL="http://127.0.0.1:${PORT:-8080}"
for i in {1..120}; do
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  j=$(curl -s "$BASE_URL/api/status")
  tier=$(echo "$j" | jq -r '.regime_directional.tier')
  regime=$(echo "$j" | jq -r '.regime_directional.regime')
  conf=$(echo "$j" | jq -r '.regime_directional.confidence')
  reason=$(echo "$j" | jq -r '.regime_directional.reason')
  bad=$(echo "$j" | jq -r 'any(.slots[]; .mode_source=="regime")')
  echo "$ts tier=$tier regime=$regime conf=$conf reason=$reason mode_source_regime=$bad"
  sleep 60
done
```

Pass conditions:
- tier values update over time (or remain stable with coherent reason)
- no slot reports `mode_source="regime"`
- no one-sided regime actuation behavior appears

Log marker for tier changes:
- `[REGIME][shadow] tier X -> Y ...`

## 3) Step 5 verification: 14-day vintage SQL

Run in Supabase SQL editor.

### 3.1 Outcome ingestion health

```sql
select
  count(*) as rows,
  min(to_timestamp(time)) as first_row_utc,
  max(to_timestamp(time)) as last_row_utc
from public.exit_outcomes
where pair = 'XDGUSD';
```

### 3.2 Transition ingestion health

```sql
select
  count(*) as rows,
  min(to_timestamp(time)) as first_row_utc,
  max(to_timestamp(time)) as last_row_utc
from public.regime_tier_transitions
where pair = 'XDGUSD';
```

### 3.3 PnL and win rate by regime tier

```sql
select
  coalesce(regime_tier, -1) as tier,
  count(*) as n,
  round(sum(net_profit_usd)::numeric, 4) as total_net_usd,
  round(avg(net_profit_usd)::numeric, 6) as avg_net_usd,
  round(100.0 * avg((net_profit_usd > 0)::int)::numeric, 2) as win_rate_pct,
  round(percentile_cont(0.5) within group (order by total_age_sec)::numeric, 1) as p50_age_sec
from public.exit_outcomes
where pair = 'XDGUSD'
  and time >= extract(epoch from (now() - interval '14 days'))
group by 1
order by 1;
```

### 3.4 By regime + side + against-trend

```sql
select
  regime_at_entry,
  trade,
  against_trend,
  count(*) as n,
  round(sum(net_profit_usd)::numeric, 4) as total_net_usd,
  round(100.0 * avg((net_profit_usd > 0)::int)::numeric, 2) as win_rate_pct
from public.exit_outcomes
where pair = 'XDGUSD'
  and time >= extract(epoch from (now() - interval '14 days'))
group by regime_at_entry, trade, against_trend
order by regime_at_entry, trade, against_trend;
```

### 3.5 Confidence bucket quality

```sql
with b as (
  select
    width_bucket(regime_confidence::numeric, 0, 1, 5) as conf_bucket,
    net_profit_usd
  from public.exit_outcomes
  where pair = 'XDGUSD'
    and regime_confidence is not null
    and time >= extract(epoch from (now() - interval '14 days'))
)
select
  conf_bucket,
  count(*) as n,
  round(avg(net_profit_usd)::numeric, 6) as avg_net_usd,
  round(100.0 * avg((net_profit_usd > 0)::int)::numeric, 2) as win_rate_pct
from b
group by conf_bucket
order by conf_bucket;
```

### 3.6 Dwell distribution by tier path

```sql
select
  from_tier,
  to_tier,
  count(*) as transitions,
  round(avg(dwell_sec)::numeric, 1) as avg_dwell_sec,
  round(percentile_cont(0.5) within group (order by dwell_sec)::numeric, 1) as p50_dwell_sec,
  round(percentile_cont(0.95) within group (order by dwell_sec)::numeric, 1) as p95_dwell_sec
from public.regime_tier_transitions
where pair = 'XDGUSD'
  and time >= extract(epoch from (now() - interval '14 days'))
group by from_tier, to_tier
order by from_tier, to_tier;
```

### 3.7 Tier occupancy estimate from transitions

```sql
with recent as (
  select *
  from public.regime_tier_transitions
  where pair = 'XDGUSD'
    and time >= extract(epoch from (now() - interval '14 days'))
),
by_to as (
  select to_tier as tier, sum(dwell_sec) as dwell_sum
  from recent
  group by to_tier
)
select
  tier,
  round(dwell_sum::numeric, 1) as dwell_sec,
  round(100.0 * dwell_sum / nullif(sum(dwell_sum) over (), 0)::numeric, 2) as dwell_share_pct
from by_to
order by tier;
```

## 4) Ready-for-Phase-1 checklist

Start Tier 1 implementation only if:
- shadow telemetry is stable for at least 14 days
- transition logs are populated and dwell is not oscillating excessively
- tier-conditioned outcomes show directional edge vs symmetric baseline
