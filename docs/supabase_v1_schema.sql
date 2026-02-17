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
  regime_at_entry integer,
  regime_confidence numeric,
  regime_bias_signal numeric,
  against_trend boolean,
  regime_tier integer,
  posterior_1m jsonb,
  posterior_15m jsonb,
  posterior_1h jsonb,
  entropy_at_entry double precision,
  p_switch_at_entry double precision,
  posterior_at_exit_1m jsonb,
  posterior_at_exit_15m jsonb,
  posterior_at_exit_1h jsonb,
  entropy_at_exit double precision,
  p_switch_at_exit double precision,
  created_at timestamptz not null default now()
);

create index if not exists idx_exit_outcomes_pair_time on public.exit_outcomes(pair, time desc);
create index if not exists idx_exit_outcomes_regime_trade on public.exit_outcomes(regime_at_entry, trade);

-- 4) Regime tier transitions (dwell + threshold tuning analytics)
create table if not exists public.regime_tier_transitions (
  id bigserial primary key,
  time double precision not null,
  pair text not null,
  from_tier integer not null,
  to_tier integer not null,
  from_label text not null,
  to_label text not null,
  dwell_sec double precision not null default 0,
  regime text,
  confidence numeric,
  bias_signal numeric,
  abs_bias numeric,
  suppressed_side text,
  favored_side text,
  reason text,
  shadow_enabled boolean not null default false,
  actuation_enabled boolean not null default false,
  hmm_ready boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists idx_regime_tier_transitions_pair_time
  on public.regime_tier_transitions(pair, time desc);
create index if not exists idx_regime_tier_transitions_path
  on public.regime_tier_transitions(from_tier, to_tier, time desc);

-- 5) Price history
create table if not exists public.price_history (
  id bigserial primary key,
  time double precision not null,
  price double precision not null,
  pair text,
  created_at timestamptz not null default now()
);

create index if not exists idx_price_history_pair_time on public.price_history(pair, time desc);

-- 6) OHLCV candles for HMM/trend analytics
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

-- 7) Structured transition/event log (new for v1)
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

-- 8) Self-healing position ledger (open/closed position state)
create table if not exists public.position_ledger (
  position_id bigint primary key,
  slot_id integer not null,
  trade_id text not null,
  slot_mode text not null,
  cycle integer not null default 0,

  entry_price double precision not null default 0,
  entry_cost double precision not null default 0,
  entry_fee double precision not null default 0,
  entry_volume double precision not null default 0,
  entry_time double precision not null default 0,
  entry_regime text,
  entry_volatility double precision not null default 0,

  current_exit_price double precision not null default 0,
  original_exit_price double precision not null default 0,
  target_profit_pct double precision not null default 0,
  exit_txid text,

  exit_price double precision,
  exit_cost double precision,
  exit_fee double precision,
  exit_time double precision,
  exit_regime text,
  net_profit double precision,
  close_reason text,

  status text not null default 'open',
  times_repriced integer not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists idx_position_ledger_slot_status
  on public.position_ledger(slot_id, status);

-- 9) Self-healing append-only journal
create table if not exists public.position_journal (
  journal_id bigint primary key,
  position_id bigint not null references public.position_ledger(position_id),
  timestamp double precision not null,
  event_type text not null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_position_journal_position_time
  on public.position_journal(position_id, timestamp desc);
create index if not exists idx_position_journal_type
  on public.position_journal(event_type);

-- 10) Optional helper view for latest runtime snapshot
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
