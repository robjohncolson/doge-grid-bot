# Sticky Slots Spec

Version: v0.2
Date: 2026-02-15
Status: Design draft (brainstorm-stage)
Reviewers: Claude, Codex

---

## 1. Thesis

The current bot spends most of its complexity and most of its realized losses
managing orphans. The orphan system exists because the original design treated
a stuck exit as an emergency. This spec proposes the opposite: **a stuck exit
is normal.** Slots hold their positions patiently. The orphan/recovery system
is deleted. Complexity drops by ~40%. Realized losses from forced cleanup
disappear.

The tradeoff — capital lock-up in stuck slots — is managed by running many
small slots (capacity-safe default: ~80 under current order-cap settings,
target: 100 after cap-policy upgrades) instead of few large ones, and by rare
rule-based slot releases as escape hatches.

---

## 2. Design Principles

1. **Patience over panic.** A $2 exit sitting 5% from market is not a crisis.
   It is a pending trade.
2. **Parallelism over recovery.** Instead of rescuing one stuck slot, run many
   small slots (80 now, 100 after capacity upgrade) so cycling slots cover
   waiting slots.
3. **Momentum over blindness.** Gate entry timing on short-term signals rather
   than placing entries on every tick.
4. **Release as exception.** Slot release is a rare, rule-based decision — not
   a timeout reflex.

---

## 3. What Gets Deleted

| Current system | Disposition |
|----------------|-------------|
| `recovery_orders` (RecoveryOrder dataclass) | Removed. No orphan → no recovery. |
| `_orphan_exit()` / `OrphanOrderAction` | Removed. |
| S2 phase (both exits stuck) | Removed. Two stuck exits = two independent patient slots. |
| Recovery cap / eviction / loss booking | Removed. |
| Auto soft-close governor | Removed. |
| `cancel_stale_recoveries` action | Removed. |
| `soft_close` / `soft_close_next` actions | Replaced by `release_slot` (see §4.2). |
| Daily loss lock (for eviction losses) | Kept but simplified — only needed for genuine trade losses now. |
| S1 orphan timeout (`S1_ORPHAN_AFTER_SEC`) | Removed. |
| S2 orphan timeout (`S2_ORPHAN_AFTER_SEC`) | Removed. |
| Exit repricing / break-glass | Removed. |
| `ORPHAN_PRESSURE_WARN_AT` notifications | Replaced by vintage aging alerts (§9). |

---

## 4. Simplified State Machine

### 4.1 Slot Lifecycle

```
S0 (waiting for entries)
 ├── A entry fills → S1a (A exit placed, B entry stays or re-placed)
 ├── B entry fills → S1b (B exit placed, A entry stays or re-placed)
 └── both fill same cycle → S1a + S1b exits both placed (independent)

S1a (A exit waiting)
 └── A exit fills → book profit, new A entry, back toward S0

S1b (B exit waiting)
 └── B exit fills → book profit, new B entry, back toward S0

Stuck: S1a or S1b where exit has not filled for a long time.
       This is not a failure state. Slot stays in S1. No action taken.
```

There is no S2. If both A and B are in position simultaneously, they are
two independent S1 states on the same slot. Each resolves independently.

### 4.2 Slot Release (escape hatch)

Release is a controlled way to stop waiting on an exit that is unlikely to
fill soon.

**Base eligibility** (conservative gate):
1. **Age**: exit has been open > `RELEASE_MIN_AGE_SEC` (default: 7 days)
2. **Distance**: exit price is > `RELEASE_MIN_DISTANCE_PCT` from current
   market (default: 10%)
3. **Regime**: market is trending (ADX > `RELEASE_ADX_THRESHOLD`, default: 30)

Release paths:
- **Manual** operator action via dashboard (`release_slot`), with confirmation
  dialog and visibility into gate status (age/distance/regime).
- **Auto tier 1 (conservative)**: when `stuck_capital_pct >
  RELEASE_MAX_STUCK_PCT` (default: 50%), release only slots meeting all base
  eligibility gates.
- **Auto tier 2 (panic)**: when `stuck_capital_pct >
  RELEASE_PANIC_STUCK_PCT` (default: 80%), release oldest slots first with an
  age-only gate (`RELEASE_PANIC_MIN_AGE_SEC`, default: 24h) until
  `stuck_capital_pct <= RELEASE_RECOVERY_TARGET_PCT` (default: 60%).

On release:
1. Cancel the exit order on Kraken.
2. Book the close at current mark using the existing synthetic-fill pattern
   (same cancel + synthetic close flow used in auto drain), so PnL and daily
   loss accounting update immediately.
3. Return slot to S0, eligible for new entries on next cycle.
4. Do not place an additional exchange market close by default.

Operational note:
- Synthetic release is guarded by immediate reconciliation.
- After every successful release, run drift reconciliation immediately.
- If balance drift exceeds `BALANCE_RECON_DRIFT_PCT`, pause further releases
  until reconciled.
- Keep this hard-gate enabled by default (`RELEASE_RECON_HARD_GATE_ENABLED`).
- Rollout recommendation: keep `BALANCE_RECON_DRIFT_PCT` in the 1.0-2.0% band
  (target: 1.5%).

### 4.3 Design Decision: Why Not "Never Release"

Pure never-release fails in one scenario: sustained 30%+ directional move
where price never returns. At DOGE's volatility, this is rare at micro-level
but inevitable at macro-level (e.g., bear market). The escape hatch prevents
total capital lock-up in that scenario while keeping releases rare enough
(days, not minutes) that they don't become the orphan system 2.0.

---

## 5. Capacity-Aware Slot Architecture

### 5.1 Order Budget and Safe Cap

```
Kraken hard limit:          225 open orders per pair
Runtime safe cap formula:   floor(limit * OPEN_ORDER_SAFETY_RATIO)
Default safety ratio:       0.75
Default runtime safe cap:   floor(225 * 0.75) = 168

Active slot (S0):           2 orders (A entry + B entry)
Active slot (S1):           1-2 orders (1 exit + 0-1 entry)
Stuck slot:                 1 order (exit only)
```

Implications under current defaults:
- Max all-S0 slots = `floor(168 / 2) = 84`
- Capacity-safe target = **80 slots** (worst-case 160 open orders)
- 100-slot mode is conditional, not default

### 5.2 Slot Count Targets

Current cap-compatible plan:
- **Phase-2 target**: 80 slots
- Typical 80-slot mix: 32 S0 (64) + 24 S1 active (36) + 24 stuck (24) = 124

100-slot target (post-upgrade) default path:
1. Raise `OPEN_ORDER_SAFETY_RATIO` to >= 0.90 (safe cap >= 202).

Optional advanced path:
1. Implement entry-cap policy that preserves capacity for fresh entries while
   keeping a global hard-cap guard on total open orders.
2. Do NOT exempt parked exits from global hard-cap accounting.

### 5.3 Slot Sizing

```
ORDER_SIZE_USD = 2.00 (or ORDER_SIZE_DOGE = 18, see §5.4)
80 slots × $2   = $160 deployed (default rollout)
100 slots × $2  = $200 deployed (post-cap upgrade)
```

At DOGE ~$0.11, each slot holds ~18 DOGE per side. Total max inventory:
~1800 DOGE at 100 slots. Grid exposure remains very small relative to account.

### 5.4 DOGE-Denominated Sizing and Compounding Policy

Since Kraken's minimum is 13 DOGE regardless of USD price, sizing should be
denominated in DOGE to avoid min-size failures.

```
ORDER_SIZE_DOGE = 18     # comfortably above 13 DOGE minimum
```

Volume computation becomes: `volume = ORDER_SIZE_DOGE`.

Compounding policy in sticky mode:
- Production sticky mode keeps profit compounding enabled.
- Capacity math remains order-count based, so compounding does not change open-order limits.
- Fixed sizing remains an emergency fallback/debug mode, not steady-state.

### 5.5 Bootstrap Ramp and Startup Reconciliation Cost

Bootstrap pacing:
- 80 slots × 2 entries = 160 orders → ~40 minutes at 2 orders/loop, 30s loop
- 100 slots × 2 entries = 200 orders → ~50 minutes (post-cap upgrade)

Startup has two costs:
1. Open-order reconciliation
2. Missed-fill replay from trade history

Acceptance criteria:
- Startup completes without HALT under 150-200 tracked orders
- Dashboard remains responsive while bootstrap backlog drains
- Bootstrap priority is deterministic (oldest/most productive slots first)

---

## 6. Momentum-Gated Entries

### 6.1 Motivation

Current entries are "always on" — both A and B entries are placed as soon
as a slot reaches S0, at fixed distance from market. This means roughly
half of entries are immediately fighting the trend.

Momentum gating asks: "is now a good time to enter THIS side?" before
placing the order.

### 6.2 Signal: Short-Term Momentum

Compute on each rebalancer tick (every 5 min) from OHLC data already
collected:

```
momentum_signal = sign(EMA_fast - EMA_slow)

  +1 = bullish (DOGE rising)    → favor B entries (buy)
  -1 = bearish (DOGE falling)   → favor A entries (sell)
   0 = neutral                  → place both sides
```

EMA periods: fast = 5-bar (25 min), slow = 20-bar (100 min) on 5-min OHLC.

### 6.3 Gating Rules

| Momentum | A entry (sell) | B entry (buy) |
|----------|---------------|---------------|
| Bullish  | defer         | place         |
| Bearish  | place         | defer         |
| Neutral  | place         | place         |

"Defer" means: do not place entry. Slot stays in partial-S0 (one entry
or no entries). When momentum flips, deferred entries are placed.

### 6.4 Interaction with Rebalancer

The rebalancer's size skew and the momentum gate are complementary:
- Rebalancer adjusts HOW MUCH to buy/sell (size)
- Momentum gate adjusts WHEN to buy/sell (timing)

Both active simultaneously. During bullish momentum with positive rebalancer
skew, B-side entries are both placed AND oversized. During bullish momentum,
A-side entries are deferred regardless of skew.

### 6.5 Constraints

1. A slot cannot sit in empty S0 (no entries at all) for more than
   `MOMENTUM_MAX_DEFER_SEC` (default: 1 hour). After that, place both
   sides regardless of signal. This prevents slots from going completely
   dark during extended trends.
2. Momentum gating is advisory, not mandatory. If the entry scheduler
   has budget and the signal is neutral, entries go out immediately.

### 6.6 Validation Gate (Required Before Enable)

Momentum gating is a hypothesis and must pass evidence checks before
production enablement:
1. Run in shadow mode first (signal computed, no execution effect).
2. Compare against baseline over at least 7 days of live conditions.
3. Enable only if ALL criteria pass:
   - Expectancy improves by at least 2% versus baseline.
   - p95 stuck-age does not worsen by more than 15%.
   - p95 stuck-capital percent does not worsen by more than 10%.
4. If checks fail, keep momentum gate off and retain symmetric entry logic.

---

## 7. Regime Detection

### 7.1 ADX (Average Directional Index)

ADX measures trend strength regardless of direction.

```
ADX < 20:  choppy / ranging   → tight entry_pct, high fill rate expected
ADX 20-30: mild trend         → normal entry_pct
ADX > 30:  strong trend       → wide entry_pct, expect more stuck slots
```

Computed from 14-period 5-min OHLC (same data source as momentum signal).

### 7.2 Regime-Adaptive Parameters

| Parameter | Choppy (ADX<20) | Normal (20-30) | Trending (ADX>30) |
|-----------|-----------------|----------------|--------------------|
| `entry_pct` | 0.25% | 0.35% | 0.50% |
| New entries/loop | 3 | 2 | 1 |
| Momentum gate | off (place both) | on | on (strict) |
| Release eligible | no | no | yes (tiered policy in §4.2) |

### 7.3 Transition Smoothing

Regime transitions use EMA smoothing (not hard thresholds) to prevent
flapping. ADX is already smooth by construction (14-period), so additional
smoothing may not be needed. Monitor in practice.

---

## 8. Reserve Budget

### 8.1 Purpose

Not all capital should be in grid slots. A reserve budget handles:
- Corrections (drift reconciliation, order repairs)
- Momentum opportunities (manual or future automated swing trades)
- Margin buffer for exchange requirements

### 8.2 Implementation

The existing rebalancer idle target IS the reserve budget. No new
mechanism needed.

```
Idle target 40% (base) = 40% of USD stays undeployed
Dynamic target 15-60% based on trend
```

Rebalancer valuation basis in sticky DOGE sizing:
- Preferred: mark-to-market portfolio basis (DOGE + USD).
- Legacy fallback: USD-only basis (for backward compatibility).

With 80 slots at $2 = $160 deployed (phase-2 baseline), and ~$763 total USD,
idle ratio is already high. At 100 slots ($200 deployed, post-cap upgrade),
idle ratio remains conservative. The rebalancer will gradually deploy more as
slots cycle and profits accumulate.

---

## 9. Telemetry: Slot Vintage

### 9.1 Vintage Distribution

Track how long each stuck exit has been waiting. Expose in status payload:

```json
{
  "slot_vintage": {
    "fresh_0_1h": 35,
    "aging_1_6h": 20,
    "stale_6_24h": 15,
    "old_1_7d": 8,
    "ancient_7d_plus": 2,
    "oldest_exit_age_sec": 518400,
    "min_slot_size_usd": 2.00,
    "median_slot_size_usd": 2.85,
    "max_slot_size_usd": 5.10,
    "stuck_capital_usd": 60.00,
    "stuck_capital_pct": 30.0
  }
}
```

### 9.2 Alerts

- `vintage_warn`: any exit older than 3 days (informational)
- `vintage_critical`: stuck capital > 50% of portfolio (operator action needed)
- `vintage_release_eligible`: N slots meet all three release criteria
- `vintage_size_dispersion`: informational metric (min/median/max slot size) for visibility, not auto-action

### 9.3 Dashboard Display

Replace the current orphan/recovery panel with a simpler vintage bar:

```
Slot Health    [████████░░░░░░░] 65% cycling | 35% waiting
Oldest Exit    2.3 days (slot 47)
Stuck Capital  $42.00 (21%)
```

---

## 10. Migration Path

### Phase 0: Measure (no code changes)

Add vintage tracking to existing bot. Measure how long exits actually
take to fill under current conditions. This data validates the entire
thesis. If median exit fill time is 15 minutes and p99 is 2 hours,
never-orphan is clearly viable. If p99 is 2 weeks, reconsider.

### Phase 1: Soft-Disable Orphaning (Guardrails On)

- Set `S1_ORPHAN_AFTER_SEC` = 86400 (24h)
- Set `S2_ORPHAN_AFTER_SEC` = 172800 (48h)
- Set `MAX_RECOVERY_SLOTS` = 4 (keep bounded emergency valve)
- Keep orphan code in place, but make intraday orphaning effectively absent
- Monitor for 1-2 weeks

### Phase 2: Capacity-Safe Sticky Rollout (to 80 Slots)

- Add slot vintage telemetry and alerts (§9)
- Add `release_slot` API + dashboard action (manual only initially)
- Set `BALANCE_RECON_DRIFT_PCT` to rollout target (recommended: 1.5%)
- Gradually scale slots to 80
- Monitor open-order headroom, fill rates, vintage distribution
- Validate startup reconciliation latency at high order counts
- Switch to DOGE-denominated sizing in canary, then global
- Keep compounding enabled (`STICKY_COMPOUNDING_MODE=legacy_profit`), monitor size dispersion telemetry

### Phase 2b: 100-Slot Upgrade (Optional)

- Default recommendation: raise `OPEN_ORDER_SAFETY_RATIO` to 0.90 for pilot.
- Optional advanced path: implement entry-cap policy upgrade for parked exits
  while keeping global hard-cap accounting intact.
- Scale from 80 to 100 in small batches (5-10/day)
- Roll back if headroom remains low or entry blocking rises

### Phase 3: Add Momentum Gating (Evidence-Gated)

- Implement 5-min EMA momentum signal
- Run in shadow mode first (no execution impact)
- Enable gating only if validation gate (§6.6) passes

### Phase 4: Add Regime Detection + Auto-Release Tiers

- Implement ADX computation
- Adaptive entry behavior based on regime
- Enable auto tier-1 release (>50% stuck, all gates)
- Enable auto tier-2 panic release (>80% stuck, age-only oldest-first)

### Phase 5: Delete Dead Code

- Remove orphan/recovery system entirely
- Remove S2 phase
- Remove soft-close/cancel-stale/manual orphan commands
- Remove auto soft-close, exit repricing, break-glass
- Simplify state machine, dashboard, tests

---

## 11. Risk Analysis

### 11.1 Acceptable Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| 30% of slots stuck for >24h | Medium | Low | Normal. Parallel slots continue cycling. |
| 50% stuck for >3 days | Low | Medium | Tier-1 release eligibility + vintage alerts. |
| Startup slows with 150-200 tracked orders | Medium | Low | Priority bootstrap + staged slot scale-up. |

### 11.2 Unacceptable Risks (escape hatches exist for these)

| Risk | Trigger | Response |
|------|---------|----------|
| Safe-cap breach | open-order headroom <= 5 sustained | Freeze new entries, reduce slots, investigate cap settings |
| All capital locked | stuck_capital_pct > 80% | Tier-2 panic release oldest eligible slots until <=60% |
| Sustained bear market | ADX>30 for >7d + exits >10% away | Tier-1 release eligible, operator notified |
| Synthetic-release drift | balance drift > `BALANCE_RECON_DRIFT_PCT` | Immediate release hard-gate: pause releases, reconcile drift, audit PnL/inventory |
| Exchange order expiry | Kraken cancels old orders | Re-place exit at same price (detect via reconciliation) |

### 11.3 What We Gain

| Metric | Current (orphan model) | Projected (sticky model) |
|--------|----------------------|-------------------------|
| Realized losses from cleanup | $19.15 today | $0 (no forced cleanup) |
| Complexity (sections in STATE_MACHINE.md) | 23 | ~16 |
| Recovery-related code lines | ~800 | 0 |
| Active slots | 10 | 80 default, 100 target |
| Theoretical throughput | 10 cycles/period | 30-60 (80 slots), 40-70 (100 slots) |
| Operator interventions/day | 2-5 (orphan cleanup) | 0-1 (check vintage) |

---

## 12. Open Questions

1. **Panic tier age floor**: Should `RELEASE_PANIC_MIN_AGE_SEC` be 24h or 48h?
2. **Release close mode**: Keep synthetic-close default only, or add optional
   hard-close (market order) mode for strict inventory parity?
3. **Drift hard-gate tuning**: Keep target at 1.5%, or tighten to 1.0% after
   observed release behavior?
4. **Startup SLO**: Confirm acceptable startup reconcile ceiling at
   150-200 tracked orders (target currently <=60s).

---

## 13. Summary

Replace panic with patience. Move from orphan-heavy recovery to sticky slots
with explicit release rules. Roll out to 80 slots under current caps, then
upgrade to 100 only after capacity policy supports it. Gate entries on
momentum only after quantitative validation. Use tiered release logic
(conservative + panic), compounded slot sizing with dispersion visibility,
and post-release drift hard-gates to prevent capital lock-up and accounting
drift.

Expected outcome: simpler code, near-zero cleanup losses, higher throughput from
parallelism, and safer operations with clearer guardrails.
