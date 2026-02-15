-- DOGE Bot v1 Supabase schema additions / baseline
-- Apply in Supabase SQL Editor.

-- 1) State snapshots (single source of truth)
create table if not exists public.bot_state (
  key text primary key,
  data jsonb not null,
  updated_at timestamptz not null default now()
);

create or replace function public.set_bot_state_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_bot_state_updated_at on public.bot_state;
create trigger trg_bot_state_updated_at
before update on public.bot_state
for each row execute function public.set_bot_state_updated_at();

-- 2) Fill ledger (optional analytics; runtime reads/writes)
create table if not exists public.fills (
  id bigserial primary key,
  time double precision not null,
  side text not null,
  price double precision not null,
  volume double precision not null,
  profit double precision not null default 0,
  fees double precision not null default 0,
  pair text,
  trade_id text,
  cycle integer,
  created_at timestamptz not null default now()
);

create index if not exists idx_fills_pair_time on public.fills(pair, time desc);

-- 3) Exit outcomes (vintage data for regime/tier tuning)
create table if not exists public.exit_outcomes (
  id bigserial primary key,
  time double precision not null,
  pair text not null,
  trade text not null,
  cycle integer not null default 0,
  resolution text not null default 'normal',
  from_recovery boolean not null default false,
  entry_time double precision,
  exit_time double precision,
  total_age_sec double precision not null default 0,
  entry_price double precision,
  exit_price double precision,
  volume double precision,
  gross_profit_usd double precision,
  fees_usd double precision,
  net_profit_usd double precision not null,
  regime_at_entry text,
  regime_confidence numeric,
  regime_bias_signal numeric,
  against_trend boolean,
  regime_tier integer,
  created_at timestamptz not null default now()
);

create index if not exists idx_exit_outcomes_pair_time on public.exit_outcomes(pair, time desc);
create index if not exists idx_exit_outcomes_regime_trade on public.exit_outcomes(regime_at_entry, trade);

-- 4) Price history
create table if not exists public.price_history (
  id bigserial primary key,
  time double precision not null,
  price double precision not null,
  pair text,
  created_at timestamptz not null default now()
);

create index if not exists idx_price_history_pair_time on public.price_history(pair, time desc);

-- 5) OHLCV candles for HMM/trend analytics
create table if not exists public.ohlcv_candles (
  id bigserial primary key,
  time double precision not null,
  pair text not null,
  interval_min integer not null default 5,
  open double precision not null,
  high double precision not null,
  low double precision not null,
  close double precision not null,
  volume double precision not null,
  trade_count integer,
  created_at timestamptz not null default now(),
  unique(pair, interval_min, time)
);

create index if not exists idx_ohlcv_pair_interval_time on public.ohlcv_candles(pair, interval_min, time desc);

-- 6) Structured transition/event log (new for v1)
create table if not exists public.bot_events (
  event_id bigint primary key,
  "timestamp" timestamptz not null,
  pair text not null,
  slot_id integer not null,
  from_state text not null,
  to_state text not null,
  event_type text not null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_bot_events_pair_slot_ts
  on public.bot_events(pair, slot_id, "timestamp" desc);

-- 7) Optional helper view for latest runtime snapshot
create or replace view public.v1_runtime as
select
  bs.key,
  bs.updated_at,
  bs.data ->> 'mode' as mode,
  bs.data ->> 'pair' as pair,
  (bs.data ->> 'entry_pct')::double precision as entry_pct,
  (bs.data ->> 'profit_pct')::double precision as profit_pct,
  (bs.data ->> 'last_price')::double precision as last_price
from public.bot_state bs
where bs.key = '__v1__';
