-- Kelly rollout migration for exit_outcomes.regime_at_entry
-- Safe to rerun (idempotent).
-- Run before deploying Kelly-tagged runtime code.

BEGIN;

DO $$
DECLARE
  col_type text;
BEGIN
  SELECT data_type
  INTO col_type
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name = 'exit_outcomes'
    AND column_name = 'regime_at_entry';

  IF col_type IS NULL THEN
    RAISE EXCEPTION 'Missing column public.exit_outcomes.regime_at_entry';
  END IF;

  -- Legacy rows were written as outcome-time text labels in this column.
  -- Clear only those mislabeled values.
  UPDATE public.exit_outcomes
  SET regime_at_entry = NULL
  WHERE regime_at_entry IS NOT NULL
    AND upper(trim(regime_at_entry::text)) IN ('BEARISH', 'RANGING', 'BULLISH');

  -- Canonical schema for new writes: integer regime id (0/1/2) or NULL.
  IF col_type NOT IN ('smallint', 'integer', 'bigint') THEN
    ALTER TABLE public.exit_outcomes
    ALTER COLUMN regime_at_entry TYPE integer
    USING CASE
      WHEN regime_at_entry IS NULL THEN NULL
      WHEN trim(regime_at_entry::text) ~ '^[0-2]$' THEN trim(regime_at_entry::text)::integer
      ELSE NULL
    END;
  END IF;

  -- Clamp any out-of-range values after conversion.
  UPDATE public.exit_outcomes
  SET regime_at_entry = NULL
  WHERE regime_at_entry IS NOT NULL
    AND regime_at_entry NOT IN (0, 1, 2);
END
$$ LANGUAGE plpgsql;

COMMIT;
