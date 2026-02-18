# Entry Floor Guard Spec

**Version**: 0.1
**Status**: Draft
**Date**: 2026-02-18

## Problem

The throughput sizer's `age_pressure` multiplier (currently **0.3**) crushes
order sizes below the Kraken minimum volume (13 DOGE), creating a death spiral:

```
old exits (p90 = 3 days)
  -> age_pressure = 0.3
    -> B-side order: $2.00 * 0.3 = $0.60
      -> volume: $0.60 / $0.101 = 5.9 DOGE
        -> 5.9 < 13 minimum -> order REJECTED
          -> slot stays short_only
            -> no B-side cycling
              -> exits stay old
```

**Affected slots**: Any slot where `_slot_order_size_usd()` after all
multipliers (throughput, age_pressure, util_penalty, aggression, suppression)
produces a USD amount whose DOGE equivalent is below `min_volume`.

**Current behavior**: `compute_order_volume()` returns `None`, the entry is
silently dropped, `_mark_entry_fallback_for_insufficient_funds` sets the slot
to one-sided mode, and auto-repair retries next loop with the same result.

**Observable symptoms**:
- Cycle-mode slots stuck as `short_only` with `mode_source: "balance"`
- `b_side_sizing.buy_ready_slots: 0`
- Log line: `"B-entry deferred: target $X.XX below exchange minimum at px=Y"`
- Slots that should be dual-sided running one-sided indefinitely

## Root Cause

The throughput sizer's `final_mult` is clamped to `[TP_FLOOR_MULT, TP_CEILING_MULT]`
(default `[0.5, 2.0]`), but this clamp operates on the **multiplier**, not on
the **resulting USD amount**. The multiplier floor of 0.5 is further reduced by
`age_pressure` (floor 0.3) before clamping:

```
final_mult = clamp(throughput_mult * age_pressure * util_penalty, 0.5, 2.0)
```

When `age_pressure = 0.3` and `throughput_mult = 1.0`:
- `raw = 1.0 * 0.3 * 1.0 = 0.3` -> clamped to `0.5`
- `$2.00 * 0.5 = $1.00` -> `$1.00 / $0.101 = 9.9 DOGE` -> still < 13

Even at the multiplier floor, the resulting volume can be sub-minimum depending
on DOGE price. There is **no USD-amount floor** that accounts for the exchange
minimum.

## Proposed Fix

### Change 1: Volume-Aware Floor in `_slot_order_size_usd`

After all multipliers are applied, clamp the final USD to at least
`min_volume * market_price * (1 + fee_buffer)`:

```python
# At the end of _slot_order_size_usd, before return:
if enforce_min_volume:
    market = self._layer_mark_price(slot)
    min_vol = float(self.constraints.get("min_volume", 13.0))
    min_usd = min_vol * market * 1.01  # 1% buffer for rounding/slippage
    base_with_layers = max(base_with_layers, min_usd)
```

**Where**: `bot.py`, `_slot_order_size_usd()`, after line ~2475 (after all
throughput/knobs/suppression math).

**Guard**: Only enforce when the computed size would otherwise be sub-minimum.
This preserves the throughput sizer's signal for all sizes above the floor.

### Change 2: Config Gate

```python
# config.py
ENTRY_FLOOR_ENABLED: bool = _env("ENTRY_FLOOR_ENABLED", True, bool)
```

Default **on**. Can be disabled to restore current behavior if needed.

### Change 3: Telemetry

Add a counter to the status payload so the operator can see when the floor
is being applied:

```python
# In status payload, under throughput_sizer or capacity_fill_health:
"entry_floor": {
    "enabled": true,
    "floor_usd": 1.33,          # current min_volume * price * 1.01
    "floor_applied_count": 4,   # times floor was used this loop
    "floor_applied_sides": {
        "A": 2,
        "B": 2
    }
}
```

This makes the floor **visible** — the operator can see that the throughput
sizer *wanted* to go lower but was clamped.

## Design Decisions

### Why floor in `_slot_order_size_usd` and not in `compute_order_volume`?

`compute_order_volume` is a pure validation function — it should continue to
reject sub-minimum orders. The fix belongs in the sizing layer where the
*intent* is formed, not in the validation layer where the *constraint* is
checked. This keeps the invariant: if `compute_order_volume` returns `None`,
something is genuinely wrong.

### Why not raise the age_pressure floor instead?

The age_pressure floor (0.3) is price-dependent: at $0.05/DOGE the math works
fine, at $0.20/DOGE even a floor of 0.7 wouldn't be enough. A volume-aware
USD floor adapts to any DOGE price automatically.

### Why not exempt auto-repair from throughput sizing?

Auto-repair already bypasses the entry scheduler cap. But bypassing the
throughput sizer entirely would remove a useful safety signal. The floor
approach is narrower: it respects the sizer's intent (reduce size) but prevents
the pathological case (reduce to zero effective).

### Won't this override the throughput sizer's "stop placing orders" signal?

Partially, yes. But the sizer's signal is about *capital efficiency*, not
*safety*. A 13 DOGE order (~$1.31) is the smallest possible position. If the
sizer wants the bot to stop entirely, the operator should use pause/halt — not
rely on a sizing artifact that happens to suppress orders below an exchange
limit.

### Per-slot age pressure (future consideration)

The current age_pressure is **global**: one p90 across all slots. Cycle slots
with 12h exit times are penalized by sticky slots with 3-day exits. A per-slot
or per-mode age pressure would be more fair but is a larger change. The floor
guard fixes the immediate death spiral; per-slot pressure is a v0.2
improvement.

## Implementation

### File Changes

| File | Change |
|------|--------|
| `config.py` | Add `ENTRY_FLOOR_ENABLED` |
| `bot.py` | Floor logic in `_slot_order_size_usd` (~5 lines) |
| `bot.py` | Telemetry counter in status payload (~10 lines) |

### Code Sketch

```python
# bot.py, _slot_order_size_usd, after line ~2475:

base_with_layers *= suppression_mult

# --- Entry floor guard (new) ---
if self._flag_value("ENTRY_FLOOR_ENABLED"):
    market = float(price_override) if price_override else self._layer_mark_price(slot)
    if market > 0:
        min_vol = float(self.constraints.get("min_volume", 13.0))
        floor_usd = min_vol * market * 1.01
        if base_with_layers < floor_usd:
            base_with_layers = floor_usd
            self._entry_floor_applied_count += 1
            side_key = "B" if trade_id == "B" else "A" if trade_id == "A" else "X"
            self._entry_floor_applied_sides[side_key] = (
                self._entry_floor_applied_sides.get(side_key, 0) + 1
            )
# --- end entry floor guard ---

if trade_id is None or not self._flag_value("REBALANCE_ENABLED"):
    return base_with_layers
```

### Initialization

```python
# bot.py, __init__:
self._entry_floor_applied_count = 0
self._entry_floor_applied_sides: dict[str, int] = {}
```

Reset counters each loop start (alongside `entry_adds_per_loop_used`).

## Testing

### Unit Tests

1. **Floor triggers**: Mock throughput sizer to return 0.3x at price $0.10.
   Assert `_slot_order_size_usd` returns >= `13 * 0.10 * 1.01` = $1.313.

2. **Floor doesn't trigger**: Mock throughput sizer to return 1.0x at price
   $0.10. Assert `_slot_order_size_usd` returns the throughput-sized value
   (no clamping).

3. **Telemetry increments**: After a floor-triggering call, assert counter
   incremented.

4. **Config gate**: Set `ENTRY_FLOOR_ENABLED=false`. Assert sub-minimum sizes
   pass through (old behavior).

5. **Price sensitivity**: At price $0.05, floor = $0.66. At price $0.50,
   floor = $6.57. Assert floor adapts.

### Integration Smoke Test

With age_pressure at 0.3 and a cycle-mode slot in S1a/short_only:
- Assert auto-repair successfully places B-side entry (not rejected)
- Assert slot transitions from `short_only` to `long_only=false, short_only=false`
- Assert the placed order volume >= 13 DOGE

## Rollout

1. Deploy with `ENTRY_FLOOR_ENABLED=true` (default)
2. Monitor `entry_floor.floor_applied_count` in status — expect it to be
   non-zero while age_pressure < ~0.65
3. Verify cycle slots (28-30) transition to dual-sided within 1-2 loops
4. Watch for order placement success on Kraken (no rejections)
5. If floor fires excessively (>50% of entries), consider raising
   `ORDER_SIZE_USD` or reducing slot count

## Risk

**Low**. The floor only fires when the alternative is *no order at all*. The
minimum order ($1.31 at current prices) represents negligible capital risk.
The worst case is slightly more orders placed than the throughput sizer
intended, but at minimum size — the capital exposure delta is ~$1.31 per
floored entry.

## Future Work

- **Per-slot age pressure**: Compute p90 from each slot's own exit history
  rather than the global pool. Cycle slots with fast turnover would get
  age_pressure ~1.0 while sticky slots with ancient exits get 0.3.
- **Adaptive floor buffer**: Instead of fixed 1.01 (1%), derive from actual
  fee tier + tick size.
- **Dashboard indicator**: Show which slots are running at floor size (amber
  badge or similar).
