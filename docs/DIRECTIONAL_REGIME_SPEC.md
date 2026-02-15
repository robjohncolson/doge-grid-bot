# Directional Regime Awareness Spec

Version: v0.1
Date: 2026-02-15
Status: Design draft
Depends on: HMM regime detector, vintage data pipeline, sticky slots

---

## 1. Problem

The A/B pair model was designed for ranging markets. Both sides run
simultaneously: A (sell high → buy back low) and B (buy low → sell high).
In a directional move, one side fills immediately and the other side's exit
stalls indefinitely. The bot currently treats this as a patience problem
(sticky slots) or an orphan problem (legacy). Neither addresses the root
cause: **placing against-trend entries is capital-destructive during
sustained directional moves.**

The HMM regime detector already classifies the market into BEARISH /
RANGING / BULLISH with a confidence score and bias signal. The vintage
data pipeline collects empirical evidence of exit behavior per regime.
This spec wires those signals into the entry-placement decision so the
bot stops fighting the trend.

---

## 2. Design Principles

1. **Neutral by default.** The bot has no permanent directional opinion.
   All bias comes from the HMM signal, which can change every tick.
2. **Graduated response.** Low confidence → symmetric. Medium → spacing
   bias. High → suppress one side entirely. No binary flip.
3. **Policy stays in runtime.** Directional decisions live in `bot.py`.
   The reducer may accept side-specific entry-distance inputs, but it
   remains deterministic and policy-agnostic.
4. **Reversible.** When regime confidence drops or flips, the suppressed
   side re-enables via the existing `_auto_repair_degraded_slot()` path.
   No manual intervention needed.
5. **Vintage-validated.** Confidence thresholds for tier transitions are
   initially conservative, then tuned from vintage outcome data.

---

## 3. Terminology

| Term | Meaning |
|------|---------|
| **Regime** | HMM-classified market state: BEARISH (0), RANGING (1), BULLISH (2) |
| **Confidence** | max(regime_probs) - second_max. Range 0.0–1.0 |
| **Bias signal** | P(BULLISH) - P(BEARISH), scaled by gain. Range -1.0 to +1.0 |
| **Directional evidence** | abs(bias_signal), plus regime in {BULLISH, BEARISH} |
| **Tier** | Graduated response level (0–2) based on confidence |
| **Suppressed side** | The A or B leg that is NOT placed during directional mode |
| **Favored side** | The A or B leg that IS placed (with-trend) |

---

## 4. Three-Tier Response Model

### Tier 0: Symmetric (default)

**Condition:** any of:
- HMM unavailable/untrained/disabled
- confidence < `REGIME_TIER1_CONFIDENCE` (default: 0.20)
- directional evidence below Tier-1 gate (default:
  `abs(bias_signal) < REGIME_TIER1_BIAS_FLOOR`)
- regime is `RANGING`

**Behavior:** Both A and B entries placed. Equal spacing. Current behavior.
No change to `long_only` / `short_only` flags.

This is the safe fallback. If HMM is untrained, disabled, or uncertain,
the bot operates exactly as it does today.

### Tier 1: Asymmetric Spacing

**Condition:** confidence >= `REGIME_TIER1_CONFIDENCE` AND
confidence < `REGIME_TIER2_CONFIDENCE` (default: 0.50) AND
directional evidence >= `REGIME_TIER1_BIAS_FLOOR`

**Behavior:** Both sides still active, but entry distances are skewed.

- BULLISH regime: B entries (buy/long) placed closer to market, A entries
  (sell/short) placed farther from market
- BEARISH regime: A entries placed closer, B entries placed farther

Spacing multipliers come from `compute_grid_bias()` in
`hmm_regime_detector.py` (already implemented, not yet consumed).

Implementation: multiply the base `entry_pct` by the spacing multiplier
when computing entry prices in `_place_entry_for_slot()` or equivalent.

**Flags:** `long_only=False`, `short_only=False` (both sides active)

### Tier 2: Side Suppression

**Condition:** confidence >= `REGIME_TIER2_CONFIDENCE` AND
directional evidence >= `REGIME_TIER2_BIAS_FLOOR` AND
regime in {`BULLISH`, `BEARISH`}

**Behavior:** Against-trend side is suppressed entirely.

- BULLISH regime → `short_only=False`, `long_only=True` (only B entries)
- BEARISH regime → `long_only=False`, `short_only=True` (only A entries)

The suppressed side's existing **entries** are cancelled. Existing
**exits** are NOT touched — they stay open (sticky patience still
applies). Only new entry placement is suppressed.

**Flags:** `long_only` or `short_only` set to True, with new field
`mode_source="regime"` to distinguish from balance-driven degradation.

---

## 5. New State and Config

### 5.1 PairState additions

```python
# In state_machine.py PairState dataclass:
mode_source: str = "none"          # "none" | "balance" | "regime"
# Optional migration helper (can be removed once mode_source is fully adopted):
regime_directional: bool = False   # derived convenience bool (mode_source == "regime")
```

This lets `_auto_repair_degraded_slot()` distinguish between:
- Balance-driven degradation: repair when funds available
- Regime-driven suppression: repair when regime changes, NOT when funds
  appear

Serialized in snapshot, restored on load.

### 5.2 Bot runtime state

```python
# In bot.py GridStrategy:
self._regime_tier: int = 0                    # current tier (0/1/2)
self._regime_tier_entered_at: float = 0.0     # timestamp of last tier change
self._regime_side_suppressed: str | None = None  # "A" or "B" or None
```

### 5.3 Config knobs

```python
# Tier thresholds
REGIME_TIER1_CONFIDENCE: float = 0.20    # asymmetric spacing kicks in
REGIME_TIER2_CONFIDENCE: float = 0.50    # side suppression kicks in

# Directional evidence gates (abs(bias_signal))
REGIME_TIER1_BIAS_FLOOR: float = 0.10    # must exceed for spacing skew
REGIME_TIER2_BIAS_FLOOR: float = 0.25    # must exceed for side suppression

# Hysteresis: require confidence to drop below threshold minus buffer
# before downgrading tier. Prevents rapid tier oscillation.
REGIME_HYSTERESIS: float = 0.05

# Minimum dwell time: stay in a tier for at least this long before
# allowing tier change. Prevents whipsaw on noisy signals.
REGIME_MIN_DWELL_SEC: float = 300.0      # 5 minutes

# Grace period: after entering Tier 2, wait this long before actually
# cancelling against-trend entries. Gives the regime time to confirm.
REGIME_SUPPRESSION_GRACE_SEC: float = 60.0

# Master enable (separate from HMM_ENABLED so you can run HMM in
# shadow/logging mode without directional actuation)
REGIME_DIRECTIONAL_ENABLED: bool = False

# Evaluate regime tier each loop regardless of rebalancer enable.
REGIME_EVAL_INTERVAL_SEC: float = 5.0
```

---

## 6. Runtime Logic

### 6.1 Tier evaluation (in dedicated `_update_regime_tier()`)

Called once per main loop cycle, after `_update_hmm()`. This should NOT be
gated by `REBALANCE_ENABLED`.

Recommended loop order:
1. `_update_hmm(now)`
2. `_update_regime_tier(now)`
3. `_update_rebalancer(now)` (if enabled)

```
if not REGIME_DIRECTIONAL_ENABLED:
    target_tier = 0
    suppressed_side = None
    return

hmm_ready = HMM_ENABLED and hmm_state.available and hmm_state.trained
if not hmm_ready:
    target_tier = 0
    suppressed_side = None
    return

confidence = hmm_state.confidence
bias = hmm_state.bias_signal
regime = hmm_state.regime
current_tier = self._regime_tier
now = _now()
dwell_elapsed = now - self._regime_tier_entered_at

# Determine raw target tier
if confidence >= REGIME_TIER2_CONFIDENCE:
    target_tier = 2
elif confidence >= REGIME_TIER1_CONFIDENCE:
    target_tier = 1
else:
    target_tier = 0

# Directional evidence gates (prevent Tier 2 on high-confidence RANGING)
directional = regime in ("BULLISH", "BEARISH")
abs_bias = abs(bias)
allow_tier1 = directional and abs_bias >= REGIME_TIER1_BIAS_FLOOR
allow_tier2 = directional and abs_bias >= REGIME_TIER2_BIAS_FLOOR

if target_tier == 2 and not allow_tier2:
    target_tier = 1 if allow_tier1 else 0
elif target_tier == 1 and not allow_tier1:
    target_tier = 0

# Apply hysteresis on tier downgrades only
if target_tier < current_tier:
    threshold = [0, REGIME_TIER1_CONFIDENCE, REGIME_TIER2_CONFIDENCE][current_tier]
    if confidence > threshold - REGIME_HYSTERESIS:
        target_tier = current_tier  # hold current tier

# Apply minimum dwell
if target_tier != current_tier and dwell_elapsed < REGIME_MIN_DWELL_SEC:
    target_tier = current_tier  # hold current tier

# Apply tier
if target_tier != current_tier:
    self._regime_tier = target_tier
    self._regime_tier_entered_at = now
    log tier transition

# Determine suppressed side from bias direction
if target_tier == 2:
    if bias > 0:  # BULLISH
        self._regime_side_suppressed = "A"  # suppress shorts
    else:         # BEARISH
        self._regime_side_suppressed = "B"  # suppress longs
elif target_tier < 2 and self._regime_side_suppressed is not None:
    self._regime_side_suppressed = None
    # trigger re-enable of suppressed side
```

### 6.2 Applying Tier 1 (asymmetric spacing)

In `_ensure_slot_bootstrapped()`, stale-entry refresh, and cycle-complete
re-entry paths, compute side-specific entry distances from market:

```
if self._regime_tier >= 1:
    grid_bias = compute_grid_bias(hmm_regime_state)
    a_entry_pct = base_entry_pct * grid_bias["entry_spacing_mult_a"]
    b_entry_pct = base_entry_pct * grid_bias["entry_spacing_mult_b"]
else:
    a_entry_pct = base_entry_pct
    b_entry_pct = base_entry_pct
```

Implementation contract (required):
- Reducer currently computes entry prices from a single `cfg.entry_pct`.
- Add side-specific multipliers (or side-specific entry_pct fields) to
  `EngineConfig` so reducer helpers can stay deterministic while runtime
  controls policy.
- Runtime sets those values before emitting/refreshing entry actions.

### 6.3 Applying Tier 2 (side suppression)

When `_regime_tier == 2` and grace period has elapsed:

**For each slot in S0 (entry phase):**
1. If suppressed side is "A" and slot has an A entry order pending:
   cancel it. Set `long_only=True, mode_source="regime"`.
2. If suppressed side is "B" and slot has a B entry order pending:
   cancel it. Set `short_only=True, mode_source="regime"`.

Also remove suppressed-side entry intents from the deferred-entry scheduler
queue (orders with no txid that would otherwise drain later).

**For each slot NOT in S0:**
- Do NOT cancel existing exits. Sticky patience applies.
- Do NOT change mode flags while exits are pending. Only suppress
  new entry placement after the current cycle completes.

**Slots that complete a cycle while Tier 2 is active:**
- On cycle completion (exit fill), only re-enter the favored side.
- The suppressed side's entry is simply not placed.

### 6.4 Tier downgrade / regime reversal

When tier drops from 2 to 1 or 0:

1. Clear regime ownership on all affected slots (`mode_source="none"`).
2. `_auto_repair_degraded_slot()` sees `long_only` or `short_only` is
   True but mode is no longer regime-owned. Since the balance-check
   conditions still pass (balance-driven repair), it re-adds the missing
   entry, restoring both sides.
3. Normal symmetric operation resumes.

When regime **flips** (e.g., BULLISH → BEARISH while at Tier 2):

1. Suppressed side changes from A to B (or vice versa).
2. Previously-suppressed side gets re-enabled first (entry placed).
3. Newly-suppressed side gets cancelled after grace period.
4. Net effect: the bot rotates which side it's running within one cycle.

---

## 7. Interaction with Existing Systems

### 7.1 Bootstrap

`_ensure_slot_bootstrapped()` currently has its own balance-driven
degradation (short_only reseed, long_only reseed). Regime-driven
suppression overrides balance-driven degradation:

- If regime suppresses A, place only B when B is fundable.
- If regime suppresses B, place only A when A is fundable.
- If favored side is NOT fundable, place nothing and wait. Do not place
  against-trend fallback entries solely because they are fundable.
- Absolute balance constraints still win (can't place an unfundable order).

Priority: **balance constraints > regime signal > symmetric default**.

### 7.2 Sticky slots

Fully compatible. Tier 2 suppression only affects entry placement.
Existing exits stay patient. Vintage data collection continues
regardless of regime tier. Release gates are unaffected.

### 7.3 Rebalancer

The rebalancer's idle target is already blended with HMM bias (line 4221
in bot.py). Directional regime adds a second actuator (entry suppression)
alongside the existing actuator (idle target modulation). They reinforce
each other:

- BULLISH: idle target favors holding DOGE (rebalancer) + only B entries
  placed (regime). Both push the same direction.
- BEARISH: idle target favors holding USD + only A entries placed.

### 7.4 Auto-repair

`_auto_repair_degraded_slot()` must check mode ownership:

```
# Current: repair if funds available
# New: repair if funds available AND regime isn't suppressing this side

if st.mode_source == "regime":
    # Don't auto-repair — regime is intentionally suppressing a side
    return
```

### 7.5 Entry scheduler / anti-chase

Entry scheduler remains the timing gate, but Tier 2 must actively purge
suppressed-side deferred entries and skip scheduling new ones for that side.

---

## 8. Dashboard

### 8.1 Status payload additions

```python
"regime_directional": {
    "enabled": bool,
    "tier": 0 | 1 | 2,
    "tier_label": "symmetric" | "biased" | "directional",
    "suppressed_side": null | "A" | "B",
    "favored_side": null | "A" | "B",
    "regime": "BEARISH" | "RANGING" | "BULLISH",
    "confidence": float,
    "abs_bias": float,
    "directional_ok_tier1": bool,
    "directional_ok_tier2": bool,
    "dwell_sec": float,           # time in current tier
    "hysteresis_buffer": float,
    "grace_remaining_sec": float, # 0 if grace elapsed
}
```

### 8.2 Visual indicator

In the summary panel, next to the existing HMM regime display:

```
Regime    BULLISH (0.62)  Tier 2 ▲ long-only
```

- Tier 0: no indicator (or "symmetric")
- Tier 1: "biased" + arrow showing favored direction
- Tier 2: "▲ long-only" or "▼ short-only" with color coding
- Always distinguish regime-driven suppression from balance-driven starvation
  (`[REG]` vs `[BAL]`) in slot badges/tooltips.

Per-slot, in the slot detail view, the phase badge already shows
S0/S1a/S1b. Add a small suffix if regime-suppressed:

```
S0 ▲     (long-only, regime-driven)
S0 ▼     (short-only, regime-driven)
S0       (symmetric, normal)
```

### 8.3 Command bar

```
:regime           Show current tier, confidence, suppressed side
:regime override  Enter manual regime override mode (see §10)
:regime auto      Return to HMM-driven regime (clear override)
```

---

## 9. Vintage Data Integration

The vintage data pipeline answers the question: "at what confidence
threshold does side suppression actually improve outcomes?"

### 9.1 Per-regime outcome analysis

After 2+ weeks of vintage collection, query:

```sql
-- Exit outcomes bucketed by regime at time of entry
-- (requires adding regime_at_entry to exit_outcomes table)
SELECT
    regime_at_entry,
    trade,
    resolution,
    COUNT(*) as n,
    AVG(total_age_sec) / 3600 as avg_hours,
    AVG(net_profit_usd) as avg_profit,
    SUM(CASE WHEN net_profit_usd < 0 THEN 1 ELSE 0 END)::float / COUNT(*) as loss_rate
FROM exit_outcomes
GROUP BY regime_at_entry, trade, resolution
ORDER BY regime_at_entry, trade;
```

Expected findings:
- A exits placed during BULLISH regime have longer fill times and higher
  loss rates (market moving away from buy-back target)
- B exits placed during BEARISH regime show the same pattern in reverse
- RANGING regime shows roughly equal A/B performance

### 9.2 Threshold calibration

Use vintage data to find the optimal `REGIME_TIER2_CONFIDENCE`:

```sql
-- For each confidence bucket, what's the loss rate of against-trend entries?
-- The threshold should be where against-trend loss rate exceeds N%.
WITH bucketed AS (
    SELECT
        CASE
            WHEN confidence < 0.2 THEN '0.0-0.2'
            WHEN confidence < 0.3 THEN '0.2-0.3'
            WHEN confidence < 0.4 THEN '0.3-0.4'
            WHEN confidence < 0.5 THEN '0.4-0.5'
            ELSE '0.5+'
        END as conf_bucket,
        net_profit_usd,
        against_trend  -- bool: was this trade against the detected regime?
    FROM exit_outcomes
    WHERE against_trend = true
)
SELECT conf_bucket, COUNT(*), AVG(net_profit_usd), ...
```

### 9.3 Schema addition

`exit_outcomes` is not in the current baseline schema (`docs/supabase_v1_schema.sql`),
so rollout includes adding it plus the write path.

Add table/columns:

```sql
CREATE TABLE IF NOT EXISTS exit_outcomes (
    id BIGSERIAL PRIMARY KEY,
    time DOUBLE PRECISION NOT NULL,
    pair TEXT NOT NULL,
    trade TEXT NOT NULL,
    resolution TEXT NOT NULL,
    total_age_sec DOUBLE PRECISION NOT NULL,
    net_profit_usd DOUBLE PRECISION NOT NULL
);

ALTER TABLE exit_outcomes ADD COLUMN regime_at_entry TEXT;       -- BEARISH/RANGING/BULLISH
ALTER TABLE exit_outcomes ADD COLUMN regime_confidence NUMERIC;  -- 0.0-1.0
ALTER TABLE exit_outcomes ADD COLUMN against_trend BOOLEAN;      -- was this against regime?
ALTER TABLE exit_outcomes ADD COLUMN regime_tier INTEGER;        -- 0/1/2 at time of entry
```

These columns are nullable (backward-compatible). Populated when
`REGIME_DIRECTIONAL_ENABLED=True`.

Implementation notes:
- Add migration SQL to `docs/supabase_v1_schema.sql`.
- Add queue/write helper in `supabase_store.py`.
- Emit row on cycle close/release paths in `bot.py` with regime snapshot at
  entry time.

---

## 10. Manual Override

For situations where the operator has conviction the HMM is wrong:

```python
REGIME_MANUAL_OVERRIDE: str | None = None  # "BULLISH", "BEARISH", or None
REGIME_MANUAL_CONFIDENCE: float = 0.75     # confidence to apply to override
```

When set, the manual override replaces HMM output for tier evaluation.
The HMM continues running and logging (shadow mode) so its accuracy can
be compared against the operator's calls.

Dashboard command: `:regime override bullish` / `:regime override bearish`
/ `:regime auto`

API endpoint: `POST /api/regime` with `{"override": "BULLISH"}` or
`{"override": null}`.

---

## 11. Rollout Plan

### Phase 0: Shadow logging (no behavioral change)

- `HMM_ENABLED=True`, `REGIME_DIRECTIONAL_ENABLED=False`
- HMM runs, classifies, logs to status payload
- `exit_outcomes` schema + writes are live; vintage data collects with
  `regime_at_entry` populated
- Duration: 2+ weeks
- Exit criteria: vintage data shows clear regime-correlated performance
  differences

### Phase 1: Tier 1 only (spacing bias)

- `REGIME_DIRECTIONAL_ENABLED=True`, `REGIME_TIER2_CONFIDENCE=1.0`
  (effectively disabling Tier 2)
- Entry spacing skewed by regime but both sides still active
- Monitor: does spacing bias reduce orphan rate without reducing fill
  rate?
- Duration: 1 week

### Phase 2: Tier 2 enabled (side suppression)

- `REGIME_TIER2_CONFIDENCE=0.50` (or calibrated from vintage data)
- Full directional mode active
- Monitor: win rate, capital efficiency, stuck capital %
- Duration: ongoing

### Phase 3: Threshold tuning

- Use vintage outcome data to optimize tier thresholds
- Tighten or loosen based on empirical loss rates
- Consider per-regime asymmetric thresholds (e.g., BEARISH suppression
  at lower confidence than BULLISH, reflecting user's DOGE-holding bias)

---

## 12. Invariants

1. `mode_source` is always `"none"` when `REGIME_DIRECTIONAL_ENABLED`
   is False (`regime_directional=False` if compatibility bool is retained).
2. `long_only` and `short_only` are never both True simultaneously
   (existing invariant, unchanged).
3. Existing exits are NEVER cancelled by regime logic. Only entries.
4. Balance constraints always override regime. You can't buy DOGE without
   USD regardless of what the HMM says.
5. Tier transitions require both confidence threshold AND minimum dwell.
   No single-tick flips.
6. When HMM is untrained or unavailable, tier is always 0 (symmetric).
7. Manual override, when set, takes precedence over HMM but not over
   balance constraints.
8. Tier 2 is unreachable when regime is `RANGING` or
   `abs(bias_signal) < REGIME_TIER2_BIAS_FLOOR`.
9. If `mode_source == "regime"`, auto-repair does not re-add suppressed
   entries until tier downgrades/reverses.

---

## 13. What This Spec Does NOT Cover

- **Position sizing by regime** — adjusting ORDER_SIZE_USD based on
  confidence. Possible future enhancement, orthogonal to this spec.
- **Exit price adjustment by regime** — tightening/widening profit
  targets during directional moves. The existing directional squeeze
  (`DIRECTIONAL_SQUEEZE`) partially does this; could be regime-gated
  in the future.
- **Multi-pair regime** — different pairs may be in different regimes.
  This spec is single-pair (XDGUSD). Multi-pair would need per-pair
  HMM instances.
- **Regime-aware release gates** — releasing stuck exits faster when
  regime confirms they're against-trend. Natural extension but separate
  spec.

---

## 14. Files Modified

| File | Changes |
|------|---------|
| `config.py` | New `REGIME_*` knobs (§5.3) |
| `state_machine.py` | Add `mode_source` (and optional compatibility bool), serialize/deserialize |
| `bot.py` | `_update_regime_tier()`, wire into main loop (decoupled from rebalancer), modify bootstrap/auto-repair/scheduler purge |
| `hmm_regime_detector.py` | No changes (compute_grid_bias already exists) |
| `dashboard.py` | Regime tier indicator, command bar verbs |
| `supabase_store.py` | Add `exit_outcomes` write path |
| `docs/supabase_v1_schema.sql` | Add `exit_outcomes` table and regime columns |
| `STATE_MACHINE.md` | Document `mode_source` semantics and Tier 2 behavior |
