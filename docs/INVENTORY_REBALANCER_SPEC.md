# Inventory Rebalancer Spec v0.3

*v0.3 changelog: Addressed Codex review — fixed baseline bias (#1), volume
drift (#2), deficit starvation (#3), unit mismatch (#4), Kd sign semantics
(#5), risk framing (#6), persistence target (#7), wall-clock interval (#8).*

## Summary

Always-on control loop that skews order **sizing** on the existing grid to
counter inventory drift. When price trends cause one pool (USD or DOGE) to
accumulate excess idle capital, the rebalancer scales up the favored side's
order size to deploy the excess — profitably, through limit orders, never
market orders.

No new order types. No separate grid. No change to entry_pct (0.2% is the
maker-fee boundary and must not be touched). Just one-sided size scaling of
what's already there.

## Mental Model

```
        ┌──────────────────────────────────────┐
        │          MAIN GRID (yin-yang)         │
        │                                       │
        │   USD ──sell entry──▶ DOGE            │
        │    ▲                    │             │
        │    └──buy exit─────────┘             │
        │                                       │
        │   DOGE ──buy entry──▶ USD            │
        │    ▲                    │             │
        │    └──sell exit────────┘             │
        └──────────────────────────────────────┘
                        │
                   fills are
                   asymmetric
                   during trends
                        │
                        ▼
        ┌──────────────────────────────────────┐
        │         INVENTORY GOVERNOR            │
        │                                       │
        │   Measures: pool imbalance + velocity │
        │   Outputs:  skew signal [0, MAX]      │
        │   Actuates: order size on ONE side    │
        │             (the side with excess)    │
        │                                       │
        │   Like a flywheel governor:           │
        │   faster drift → stronger pushback    │
        │   balanced → idle (both sides = base) │
        └──────────────────────────────────────┘
```

The rebalancer is NOT a second machine. It's a governor on the existing machine
that makes the excess side's orders **larger** so each fill deploys more of the
idle pool. The deficit side stays at base size — never shrunk, never starved.

## Locked Decisions

1. **No market orders.** All rebalancing through limit order size skew.
2. **No new order flow.** Only adjusts size of orders the grid would place anyway.
3. **entry_pct is sacred.** 0.2% is the maker-fee boundary. Never touched.
4. **Always-on.** No manual intervention required.
5. **Bounded risk increase.** Scaling up order size increases absolute exposure
   on negative-net cycles (recovery closures, orphan losses). This is accepted
   and bounded by MAX_SIZE_MULT. The rebalancer does NOT guarantee zero loss —
   it guarantees that each trade's profit *margin* is preserved (same spread),
   while absolute exposure per trade increases by at most MAX_SIZE_MULT.
6. **One-sided scaling only.** Excess side scales UP. Deficit side stays at base.
   Never scale down — avoids min-volume violations and slot starvation.
7. **Scaling only on new entry generation.** Auto-repair, reseed, and bootstrap
   paths use base size. Only fresh entries from the normal state machine path
   are scaled.

## Scope

### In

1. Imbalance measurement: target-based idle_usd_pct signal.
2. PD control loop with neutral band, hard clamps, rate limiting.
3. One-sided order size scaling (excess side only).
4. Bootstrap loop prevention (slow EMA, hysteresis, rate limiting).
5. Safety invariants (min volume, runway protection, max scale factor,
   fund-availability pre-check).
6. Dashboard: current skew direction + magnitude indicator.
7. Telemetry: imbalance, velocity, skew logged per update.

### Out

1. **entry_pct adjustment** (maker-fee boundary, do not touch).
2. Exit price skew (exits stay profit-driven, not rebalance-driven).
3. Recovery order adjustment (lottery tickets untouched).
4. Slot count changes (rebalancer doesn't add/remove slots).
5. Any form of market order or taker order.
6. Cross-pair rebalancing (single-pair only for v0.1).
7. Scaling DOWN the deficit side (only scale up, never shrink).
8. Scaling on auto-repair, reseed, or bootstrap paths.

---

## 1. Imbalance Signal

### 1.1 What Creates Imbalance

Price trends cause asymmetric fills:

| Trend   | Effect                                      | Pool drift      |
|---------|---------------------------------------------|-----------------|
| Up      | A-side sells fill fast, B-side buys stall   | USD accumulates  |
| Down    | B-side buys fill fast, A-side sells stall   | DOGE accumulates |
| Flat    | Both sides fill symmetrically               | Balanced         |

The grid is symmetric by design. Fills are not.

### 1.2 Signal: Target-Based idle_usd_pct

**v0.2 used a zero-centered (usd_excess - doge_excess)/portfolio signal.
Codex review identified this is internally inconsistent for a DOGE-native
portfolio — the signal would be permanently negative (excess DOGE always
dominates), causing perpetual A-side scaling. v0.3 replaces this with a
simpler, target-based signal using idle_usd_pct.**

The signal is based on a single, already-computed quantity:

```
idle_usd_pct = idle_usd / observed_usd
```

Where `idle_usd = observed_usd - usd_runway_floor` (already in scoreboard).

**IMPORTANT: Unit convention.** The existing code in `_compute_doge_bias_scoreboard()`
emits `idle_usd_pct` as a percentage (0–100 scale, e.g. 75.0). The controller
operates on ratios (0–1 scale). **Divide by 100 at the boundary:**

```
idle_ratio = scoreboard["idle_usd_pct"] / 100.0    # 0.75, not 75.0
```

All thresholds, gains, and formulas in this spec use ratio scale (0–1).

### 1.3 Error Signal

```
error = idle_ratio - TARGET_IDLE_RATIO
```

**TARGET_IDLE_RATIO = 0.40** (40% of USD should be idle as runway buffer).

- **error > 0** → too much idle USD → scale up B-side (buy DOGE)
- **error < 0** → too little idle USD → scale up A-side (sell DOGE, recover USD)
- **error ≈ 0** → balanced → both sides at base size

This avoids the DOGE-dominance normalization problem entirely. We measure
only the USD working capital pool, which is the one the bot actively manages.
The DOGE pool is 99%+ of portfolio and not meaningfully "idle" — it IS the
portfolio.

### 1.4 Smoothing

Raw error is noisy (single fill can swing idle_usd). Apply EMA:

```
smoothed = α * raw_error + (1 - α) * prev_smoothed
α = 1 - exp(-dt / HALFLIFE)
```

**HALFLIFE = 1800 seconds (30 minutes).** Long enough to ignore individual fills,
short enough to react to sustained trends within 1-2 hours.

### 1.5 Velocity

Rate of change of smoothed error:

```
velocity = (smoothed - prev_smoothed) / dt
```

Also EMA-smoothed with same halflife.

- **Positive velocity** = idle USD growing (error getting more positive)
- **Negative velocity** = idle USD shrinking (error getting more negative)

---

## 2. Control Loop

### 2.1 PD Controller

```
raw_skew = clamp(Kp * smoothed_error + Kd * smoothed_velocity,
                 -MAX_SKEW, +MAX_SKEW)
```

- **Kp** (proportional): Corrects current error. Higher = more aggressive.
- **Kd** (anticipatory): Amplifies correction when error is worsening,
  reduces correction when error is improving. This is a **trend-following**
  term, not a damping term. When idle USD is growing (velocity > 0), the
  controller acts more aggressively. When idle USD is shrinking (velocity < 0),
  the controller eases off — even if error is still positive.
- **MAX_SKEW**: Hard ceiling on output. Default **0.30** (±30% size increase).

No integral term. Steady-state offset is acceptable — the rebalancer is a nudge,
not a precision controller. Avoiding integral windup is worth the tradeoff.

**Sign convention summary:**

| error | velocity | Kp term | Kd term | net skew | meaning                          |
|-------|----------|---------|---------|----------|----------------------------------|
| +     | +        | +       | +       | strong + | excess USD, getting worse → push  |
| +     | -        | +       | -       | weak +   | excess USD, improving → ease off  |
| -     | -        | -       | -       | strong - | USD depleted, getting worse → push|
| -     | +        | -       | +       | weak -   | USD depleted, improving → ease off|

### 2.2 Neutral Band

```
if |smoothed_error| < NEUTRAL_THRESHOLD:
    raw_skew = 0
```

**NEUTRAL_THRESHOLD = 0.05** (5 percentage points of idle_usd_pct).
Within this band, the grid runs symmetric. Prevents micro-oscillation
around the target.

### 2.3 Update Rate

Skew recomputed on a **wall-clock interval**, not cycle count.

**REBALANCE_INTERVAL_SEC = 300** (5 minutes).

```
if now - rebalancer_last_update_ts >= REBALANCE_INTERVAL_SEC:
    _update_rebalancer()
    rebalancer_last_update_ts = now
```

This is independent of main loop poll rate. If loop interval changes,
the rebalancer cadence stays constant.

### 2.4 Skew Rate Limiting

```
skew_delta = new_skew - current_skew
if |skew_delta| > MAX_SKEW_STEP:
    new_skew = current_skew + sign(skew_delta) * MAX_SKEW_STEP
```

**MAX_SKEW_STEP = 0.05** per update. Skew can only change by 5% per update
interval. Full swing from 0 to +0.30 takes at least 6 updates (~30 minutes).
Prevents sudden ramp-ups.

---

## 3. Actuator: One-Sided Size Scaling

### 3.1 Constraint: entry_pct Is Sacred

`entry_pct = 0.2%` is the tightest maker-fee-safe distance from market.
Tightening it risks crossing into taker territory. Widening it reduces fill
probability without deploying capital. **It is not a tunable parameter.**

### 3.2 Size Scaling Mechanism

Current sizing (in `_slot_order_size_usd()`):
```python
base_size = max(ORDER_SIZE_USD, ORDER_SIZE_USD + slot.total_profit)
```

With rebalancer — **only the excess side scales up**:

```
When skew > 0 (excess USD → favor B-side):
    B-side size = base_size * (1 + skew * SIZE_SENSITIVITY)
    A-side size = base_size                                    # unchanged

When skew < 0 (excess DOGE → favor A-side):
    A-side size = base_size * (1 + |skew| * SIZE_SENSITIVITY)
    B-side size = base_size                                    # unchanged

When skew = 0 (neutral band):
    A-side size = base_size
    B-side size = base_size
```

**SIZE_SENSITIVITY = 1.0** (skew of 0.30 → 30% larger orders on excess side).

Hard cap: `effective_size = min(effective_size, base_size * MAX_SIZE_MULT)`

**MAX_SIZE_MULT = 1.5** (50% max increase regardless of controller output).

### 3.3 Where Scaling Is Applied (Volume Integrity)

**v0.2 proposed applying the multiplier at the `_place_pair_order()` call site
(Option B). Codex review identified this causes state/exchange volume drift —
the state machine would track base volume while Kraken sees scaled volume,
corrupting telemetry and reconciliation.**

The scaled USD size must be computed BEFORE entering the state machine path:

**Option A (revised, preferred):** Compute `effective_size` (in USD) upstream.
The state machine then converts this USD amount to DOGE volume via
`compute_order_volume()` as it does today — but starting from the
already-scaled USD input. Since the USD→volume conversion happens once
inside the normal pipeline, `PlaceOrder` actions, `OrderState` records,
and Kraken orders all derive from the same scaled USD value.

Concretely:
1. `_slot_order_size_usd(slot, trade_id)` now accepts `trade_id` parameter
2. Inside, it computes `base_size` (USD) then applies rebalancer multiplier
3. Returns `effective_size` (USD, already scaled)
4. All downstream consumers receive this single USD value → volume conversion
   happens once → no drift between state and exchange

This ensures: **state volume == placed volume == tracked volume**. No drift.

### 3.4 Why One-Sided

| Approach          | Risk                                          |
|-------------------|-----------------------------------------------|
| Scale up excess   | More capital deployed per fill. Safe.          |
| Scale down deficit| Could breach min volume (13 DOGE). Dangerous.  |
| Both              | Deficit side starved + min volume risk.        |

One-sided scaling means: the deficit side keeps earning normally at base size.
The excess side earns the same margin per dollar but churns more dollars per fill.
No slot is ever degraded.

### 3.5 Fund-Availability Pre-Check (Starvation Guard)

**v0.2 claimed "deficit side never starved." Codex review identified that
larger favored-side orders can deplete available funds, triggering long_only /
short_only fallback on the opposite side — effectively starving it.**

Before applying the size multiplier, verify the opposite side still has funds:

```
if skew > 0 (scaling up B-side buys):
    scaled_b_usd = base_size * (1 + skew * SIZE_SENSITIVITY)
    # Check: after this B order, is there still enough USD for next A-side exit?
    remaining_usd = available_usd - scaled_b_usd
    if remaining_usd < base_size:
        # Would starve A-side → clamp B scaling down
        max_safe_b = available_usd - base_size
        scaled_b_usd = min(scaled_b_usd, max(base_size, max_safe_b))

if skew < 0 (scaling up A-side sells):
    scaled_a_doge = (base_size * (1 + |skew| * SIZE_SENSITIVITY)) / price
    remaining_doge = available_doge - scaled_a_doge
    if remaining_doge < (base_size / price):
        # Would starve B-side → clamp A scaling down
        max_safe_a_doge = available_doge - (base_size / price)
        scaled_a_doge = min(scaled_a_doge, max(base_size / price, max_safe_a_doge))
```

This is a per-order pre-check, not a global toggle. The scaling is clamped
only when a specific order would actually cause starvation.

### 3.6 Interaction with Compounding

The rebalancer multiplier stacks on top of the existing compounding formula:

```
compounded_size = max(ORDER_SIZE_USD, ORDER_SIZE_USD + slot.total_profit)
effective_size  = compounded_size * (1 + skew_factor)
effective_size  = min(effective_size, compounded_size * MAX_SIZE_MULT)
```

Where `skew_factor` is 0 for the deficit side and `skew * SIZE_SENSITIVITY`
for the excess side. Compounding and rebalancing are independent multipliers.

### 3.7 What Is NOT Skewed

- **entry_pct**: 0.2%, sacrosanct, maker-fee boundary.
- **Exit prices**: Governed by `_pair_exit_price()`, profit floor sacrosanct.
- **Recovery orders**: Already lottery tickets at fixed prices. Don't touch.
- **Slot count**: Rebalancer has no authority over slot add/remove.
- **profit_pct**: Exit targets stay symmetric.
- **refresh_pct**: Entry refresh distance stays symmetric.
- **Auto-repair / reseed / bootstrap orders**: Use base size only.

---

## 4. Bootstrap Loop Prevention

### 4.1 The Risk

```
Excess USD → scale up B → more USD deployed per B fill → USD shrinks
  → Excess DOGE → scale up A → more DOGE deployed per A fill → DOGE shrinks
    → Excess USD → ... (oscillation paying fees each round trip)
```

Unlike entry_pct skew, size skew doesn't change fill probability — it changes
capital-per-fill. The bootstrap risk is lower because:
- B fills don't happen faster (same entry_pct, same distance from market)
- Each B fill just moves more USD
- The rebalancer can't create fills, only size them

Still, if error oscillates around the neutral band, the rebalancer could
flip sides repeatedly. Each flip doesn't directly cost fees (sizing is applied
at order placement, not mid-flight), but rapid resizing creates noise.

### 4.2 Mitigations (Defense in Depth)

| Layer          | Mechanism                                           | Purpose                        |
|----------------|-----------------------------------------------------|--------------------------------|
| **Target**     | Error measured vs TARGET (0.40), not vs 0           | No permanent bias              |
| **Smoothing**  | 30-min EMA halflife on error                        | Filters fill-driven noise      |
| **Neutral**    | ±5pp dead band around target                        | Prevents micro-oscillation     |
| **Rate limit** | Max ±5% skew change per 5-min interval              | Prevents sudden ramp           |
| **Velocity**   | Kd term eases off when error is improving           | Reduces overshoot              |
| **Max skew**   | Hard cap at ±30%                                    | Bounds maximum size increase   |
| **Slow clock** | 5-min wall-clock update interval                    | Actuator slower than fills     |
| **One-sided**  | Only excess side scaled, deficit untouched           | No starvation risk             |
| **Fund guard** | Per-order starvation pre-check (§3.5)               | Prevents fallback triggers     |

### 4.3 Why Bootstrap Risk Is Lower Than Entry Skew

With entry_pct skew: tighter entries → faster fills → faster pool drain → faster
flip to other side. The actuator directly accelerates the fill rate, creating a
tight feedback loop.

With size skew: same entry_pct → same fill rate → same time between fills. Only
the *amount* per fill changes. The pool drains faster per fill, but fills aren't
more frequent. The feedback loop is looser — bounded by market-driven fill
timing, not by the actuator itself.

### 4.4 Detection

Monitor for oscillation: if skew flips sign more than **3 times per hour**,
log a warning and halve MAX_SKEW for the next hour (auto-damping).

---

## 5. Safety Invariants

Hard constraints that override the controller output:

1. **Min volume**: Effective order size must produce volume >= `min_volume`
   (13 DOGE) on BOTH sides. Since we only scale up (never down), this is
   satisfied by construction — but verify as a runtime assertion anyway.

2. **Max volume**: Effective order size capped at `base_size * MAX_SIZE_MULT`.
   Default **MAX_SIZE_MULT = 1.5** (50% max increase). Even if controller
   outputs MAX_SKEW, the size multiplier is hard-capped.

3. **Runway protection**: If `available_usd < usd_runway_floor`, force
   B-side scaling to 1.0 (base size only — don't deploy more USD).
   If DOGE-side equivalent is breached, force A-side scaling to 1.0.

4. **Fund-availability guard** (§3.5): Per-order pre-check that the scaled
   order doesn't starve the opposite side into long_only/short_only fallback.

5. **Capacity gate**: If `capacity_fill_health.status_band` is "caution" or
   "stop", set `skew = 0`. Uses existing band semantics directly — no new gate.

6. **Skew bounded**: `|skew| <= MAX_SKEW` at all times, enforced by clamp.

7. **Rate bounded**: `|skew_delta| <= MAX_SKEW_STEP` per update.

8. **entry_pct unchanged**: Defensive assertion that entry_pct is never
   modified by rebalancer code path.

9. **Volume integrity**: Assert `state_volume == placed_volume` after every
   order placement. Detect any drift immediately.

---

## 6. State & Persistence

### 6.1 New Fields (bot-level, not per-slot)

```
rebalancer_smoothed_error:     float = 0.0
rebalancer_smoothed_velocity:  float = 0.0
rebalancer_current_skew:       float = 0.0
rebalancer_last_update_ts:     float = 0.0    # wall-clock timestamp
rebalancer_sign_flips_1h:      int   = 0
rebalancer_damped_until:       float = 0.0    # auto-damp timestamp
```

### 6.2 Persistence

Saved to **both** `logs/bot_runtime.json` (local) and Supabase `bot_state` table
(cloud), consistent with existing persistence architecture. On restart,
resume from persisted values (no cold-start spike). If fields are missing,
start at zero (symmetric — all slots at base size).

### 6.3 Per-Slot Application

The skew is computed once at bot level. Each slot reads the same skew value
when sizing its next order. The skew applies uniformly across all slots —
not per-slot tuning. This keeps the model simple and avoids inter-slot
competition.

---

## 7. Integration Points

### 7.1 Where Size Skew Is Applied

In `_slot_order_size_usd()` (bot.py ~line 274). Add `trade_id` parameter:

```python
def _slot_order_size_usd(self, slot: SlotRuntime, trade_id: str = None) -> float:
    base = max(float(config.ORDER_SIZE_USD),
               float(config.ORDER_SIZE_USD) + slot.state.total_profit)

    if trade_id is None or self._rebalancer_current_skew == 0:
        return base

    skew = self._rebalancer_current_skew
    if (skew > 0 and trade_id == "B") or (skew < 0 and trade_id == "A"):
        mult = 1 + abs(skew) * config.REBALANCE_SIZE_SENSITIVITY
        mult = min(mult, config.REBALANCE_MAX_SIZE_MULT)
        return base * mult

    return base
```

All existing callers that pass `trade_id=None` get unchanged behavior.
Only the entry-generation path passes the trade_id to get scaling.

The returned value flows into `compute_order_volume()` → `PlaceOrder` action
→ `OrderState` record → Kraken API. **One number, no drift.**

### 7.2 Where Imbalance Is Measured

In the main loop (bot.py), after balance fetch. Reads `idle_usd_pct` from
the existing `_compute_doge_bias_scoreboard()` result — no new API calls.

**Unit conversion at boundary:**
```python
idle_ratio = scoreboard["idle_usd_pct"] / 100.0
```

### 7.3 Where Skew Is Updated

New method `_update_rebalancer()` called on wall-clock interval:

```python
def _update_rebalancer(self, scoreboard: dict, now: float):
    if now - self._rebalancer_last_update_ts < config.REBALANCE_INTERVAL_SEC:
        return
    # ... PD controller logic ...
    self._rebalancer_last_update_ts = now
```

Called every main-loop iteration but no-ops until interval elapses.

---

## 8. Configuration

| Parameter                    | Default | Unit    | Purpose                              |
|------------------------------|---------|---------|--------------------------------------|
| `REBALANCE_ENABLED`          | True    | bool    | Master switch                        |
| `REBALANCE_TARGET_IDLE_PCT`  | 0.40    | ratio   | Target idle USD fraction (40%)       |
| `REBALANCE_KP`               | 2.0     | —       | Proportional gain                    |
| `REBALANCE_KD`               | 0.5     | —       | Anticipatory gain (trend-following)  |
| `REBALANCE_MAX_SKEW`         | 0.30    | ratio   | Max |skew| output                    |
| `REBALANCE_MAX_SKEW_STEP`    | 0.05    | ratio   | Max skew change per update           |
| `REBALANCE_NEUTRAL_BAND`     | 0.05    | ratio   | Error dead zone (±5pp)               |
| `REBALANCE_EMA_HALFLIFE`     | 1800    | seconds | Smoothing window                     |
| `REBALANCE_INTERVAL_SEC`     | 300     | seconds | Update frequency (wall-clock)        |
| `REBALANCE_SIZE_SENSITIVITY` | 1.0     | ratio   | Skew → size multiplier               |
| `REBALANCE_MAX_SIZE_MULT`    | 1.5     | ratio   | Hard cap: max 50% size increase      |

**Kp and Kd defaults are starting guesses.** Tune via live observation:
- If skew oscillates → increase Kd or decrease Kp
- If error persists too long → increase Kp
- If bootstrap loop detected → decrease both and increase HALFLIFE

---

## 9. Dashboard Integration

### 9.1 Status Payload

Add to existing `/api/status` response:

```json
"rebalancer": {
    "enabled": true,
    "idle_ratio": 0.75,
    "target": 0.40,
    "error": 0.35,
    "smoothed_error": 0.28,
    "velocity": 0.001,
    "skew": 0.15,
    "skew_direction": "buy_doge",
    "size_mult_a": 1.0,
    "size_mult_b": 1.15,
    "damped": false,
    "sign_flips_1h": 0
}
```

### 9.2 Dashboard Display

In the DOGE Bias Scoreboard card, add:

```
Governor: ▶ Buy DOGE (skew +15%)
Size B: ×1.15  |  Size A: ×1.00
Idle USD: 75% → target 40%
```

- Arrow direction indicates which side is scaled up
- Color: green (active), gray (neutral/disabled), yellow (damped)
- Tooltip shows error/velocity/Kp/Kd details

---

## 10. Telemetry & Monitoring

### 10.1 Per-Update Log Line

```
[REBAL] idle=0.75 target=0.40 err=+0.28 vel=+0.001 skew=+0.15 size_a=×1.00 size_b=×1.15
```

### 10.2 Oscillation Alert

If `sign_flips_1h >= 3`:
```
[REBAL] WARNING: oscillation detected (3 flips/hr), auto-damping MAX_SKEW to 0.15
```

### 10.3 Metrics to Watch Post-Launch

- Idle USD % trend (should move toward TARGET_IDLE_RATIO when skew active)
- USD deployed per hour (should increase when B-side scaled up)
- Round-trip profit per side (margin stays constant; absolute profit scales with size)
- Skew sign flip frequency (should be < 3/hr)
- Average order size A vs B (confirms asymmetry is applied)
- long_only / short_only trigger rate (should NOT increase — fund guard working)
- State vs Kraken volume drift (should be zero — volume integrity assertion)

---

## 11. Implementation Phases

### Phase 1: One-Sided Size Scaling

- Target-based error signal from idle_usd_pct
- PD controller with wall-clock interval
- `_slot_order_size_usd(slot, trade_id)` with rebalancer multiplier
- Fund-availability starvation guard
- All safety invariants including volume integrity assertion
- Dashboard indicator
- Telemetry logging

### Phase 2: Tuning

- Live observation of Kp/Kd response
- Adjust halflife, neutral band, max skew based on real behavior
- Calibrate SIZE_SENSITIVITY and MAX_SIZE_MULT
- Validate TARGET_IDLE_RATIO is correct for portfolio dynamics
- Possible addition of slow integral term if steady-state offset is problematic

### Phase 3: Extended Actuators (if needed)

- Exit spread skew on the **excess side only** (take slightly less profit to
  turn capital faster). Only if size skew alone is insufficient.
- Evaluate carefully — changes the profit floor, higher risk.

---

## Appendix A: Why Not Entry Placement Skew?

`entry_pct = 0.2%` is calibrated to the tightest maker-fee-safe distance from
the spread. It cannot be tightened (risks taker fees) or widened (reduces fill
probability without benefit). It is a hard constraint, not a tuning knob.

Size scaling achieves the same rebalancing effect through a different channel:
instead of making one side fill *sooner* (entry_pct), we make each fill
*move more capital* (order size). The pool drains at the same fill rate but
with larger per-fill bites.

## Appendix B: Why Not a Separate Grid?

A second mini-grid for rebalancing would:
1. Compete with the main grid for order slots (capacity conflict)
2. Potentially cross-trade with the main grid (buy from yourself)
3. Need its own state machine, lifecycle, recovery logic
4. Double the complexity for marginal benefit

Skewing the existing grid achieves the same capital flow with zero new
infrastructure. The main grid already places entries on both sides — we're
just making one side's orders bigger when it has more capital to deploy.

## Appendix C: Example Scenario

**Starting state:** Price at $0.0966, idle USD at 75%, target 40%, skew = 0.
Base order size per slot: ~$1.65.

1. Rebalancer measures: `idle_ratio = 0.75`, `error = 0.75 - 0.40 = +0.35`
2. EMA smooths: `smoothed_error ≈ +0.28` (first updates lag raw)
3. Controller: `skew = 2.0 * 0.28 + 0.5 * 0.001 = +0.561`
4. Clamped to MAX_SKEW: `skew = +0.30`
5. Rate limited from 0 → +0.05 (first update)
6. B-side size: `$1.65 × (1 + 0.05 × 1.0) = $1.73`
7. A-side size: `$1.65` (unchanged)
8. Fund guard check: $1.73 B-order leaves enough USD for A-side? ✓
9. Next update (+5 min): skew ramps +0.05 → +0.10
10. B-side size: `$1.65 × 1.10 = $1.82`
11. Over ~30 minutes, skew ramps to steady state
12. Each B-side buy fill deploys more USD → idle_usd_pct decreases
13. As idle_ratio approaches 0.40, error shrinks → skew decreases
14. When error within neutral band (±0.05): skew = 0, both sides at base

**At full skew (+0.30):**

| Side | Base Size | Effective Size | Change     |
|------|-----------|----------------|------------|
| B    | $1.65     | $2.15          | +30%       |
| A    | $1.65     | $1.65          | unchanged  |

Capped by MAX_SIZE_MULT (1.5): `$1.65 × 1.5 = $2.48` max possible.
At skew=0.30, actual is `$1.65 × 1.30 = $2.15` — below cap.

**Worst case:** Trend reverses sharply. B-side entries that filled at larger
size now have larger exits to complete. Exit prices preserve profit margin
(computed from entry price). Absolute exposure per position is up to 30%
higher — so absolute loss on a negative-net cycle is up to 30% higher.
This is the bounded risk increase acknowledged in Locked Decision #5.

## Appendix D: Codex Review Items — Resolution

| # | Issue | Resolution |
|---|-------|------------|
| 1 | Baseline bias for DOGE-native portfolio | Replaced zero-centered signal with target-based `idle_usd_pct - TARGET`. §1.2–1.3 |
| 2 | Volume drift with call-site-only sizing | `_slot_order_size_usd()` now takes `trade_id`, returns scaled value. One number flows through entire pipeline. §3.3 |
| 3 | Deficit side starvation via fund depletion | Per-order fund-availability pre-check clamps scaling when opposite side would be starved. §3.5 |
| 4 | Unit mismatch (ratio vs percent) | Explicit `/100.0` conversion at boundary. All spec formulas use ratio (0–1). §1.2 |
| 5 | Kd sign contradicts "damping" description | Relabeled as "anticipatory/trend-following." Sign convention table added. §2.1 |
| 6 | "Can't cause fund loss" is unsafe | Reframed as "bounded risk increase." MAX_SIZE_MULT caps exposure amplification. Locked Decision #5 |
| 7 | Persistence target inaccurate | Both `logs/bot_runtime.json` and Supabase. §6.2 |
| 8 | Cycle-count cadence is poll-rate dependent | Wall-clock interval (REBALANCE_INTERVAL_SEC). §2.3 |

## Appendix E: Codex Open Questions — Answers

| # | Question | Answer |
|---|----------|--------|
| 1 | Target: 0 or learned baseline? | Configurable target: `TARGET_IDLE_RATIO = 0.40`. Not learned — operator sets it based on desired runway. |
| 2 | Scaling on auto-repair/reseed? | No. Only new entry generation from normal state machine path. Locked Decision #7. |
| 3 | Capacity gate semantics? | Uses existing `capacity_fill_health.status_band` directly. No new gate. Safety Invariant #5. |
