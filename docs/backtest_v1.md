# v1 Backtesting

`backtest_v1.py` replays historical candles through the current `state_machine.py` reducer.

## What It Does

- Uses v1 pair state transitions (`PriceTick`, `TimerTick`, `FillEvent`, `RecoveryFillEvent`)
- Simulates fills when intrabar path crosses active order prices
- Tracks compounding via slot `total_profit`
- Reports PnL, round trips, win rate, drawdown, orphan counts, and invariant health

## Data Sources

- Kraken OHLC API (default)
- Local CSV (`--csv`)

CSV header must include:

- `time` or `timestamp` (unix ts or ISO8601)
- `open`, `high`, `low`, `close`

## Basic Usage

Run 1-year Kraken replay:

```bash
python3 backtest_v1.py \
  --pair XDGUSD \
  --interval 15 \
  --start 2025-01-01 \
  --end 2026-01-01
```

If bootstrap fails from low order size, enable automatic floor:

```bash
python3 backtest_v1.py \
  --pair XDGUSD \
  --interval 1440 \
  --start 2025-01-01 \
  --end 2026-01-01 \
  --auto-floor
```

Run on local candles:

```bash
python3 backtest_v1.py \
  --csv data/XDGUSD_15m.csv \
  --pair XDGUSD \
  --interval 15 \
  --start 2025-01-01 \
  --end 2026-01-01
```

Write JSON summary:

```bash
python3 backtest_v1.py ... --json-out /tmp/xdgusd_backtest.json
```

## Multi-Slot Replay

You can approximate live multi-slot behavior with:

```bash
python3 backtest_v1.py ... --slots 9
```

This runs 9 independent slot state machines over the same market path.

## Useful Knobs

- `--order-size-usd`
- `--entry-pct`
- `--profit-pct`
- `--refresh-pct`
- `--maker-fee-pct`
- `--auto-floor`

Fallback exchange constraints (for offline CSV environments):

- `--price-decimals`
- `--volume-decimals`
- `--min-volume`
- `--min-cost-usd`

## Notes

- Intrabar path is deterministic: `open -> nearer extreme -> other extreme -> close`.
- This is historical replay, not full order-book microstructure simulation.
- For strategy tuning, compare runs across multiple intervals and date ranges.
- Kraken OHLC retention is interval-limited; for deep history, prefer `--csv`.
