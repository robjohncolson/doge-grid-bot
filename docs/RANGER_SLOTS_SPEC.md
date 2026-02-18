# Ranger Slots Spec

**Version:** 0.1
**Date:** 2026-02-18
**Status:** Draft

---

## Problem

The bot holds 832K DOGE and ~$0 USD. Sticky slots are frozen waiting for exits
that are 15% from market. The churner was supposed to help but it's a
self-healing subsystem that requires a parent position, routes profit to a
subsidy pool, and depends on the position ledger. It fundamentally cannot
cycle independently — no parent = no activity.

What's needed: simple, fast-cycling sell-side slots that convert idle DOGE
into profit during RANGING periods. Accept orphans as a cost of doing business.

## Design

A **ranger** is a standalone micro-cycler. No parent position, no subsidy,
no position ledger, no self-healing. Just the old non-sticky lifecycle:

```
IDLE → sell entry on book → fill → buy exit on book → fill → profit → IDLE
                              ↓                         ↓
                          entry timeout              exit timeout
                              ↓                         ↓
                            IDLE                    orphan → IDLE
```

Rangers use DOGE for sell entries (Trade A pattern: sell high, buy low).
They operate only during RANGING consensus regime. When regime shifts away,
all open ranger orders are cancelled and rangers go idle until RANGING returns.

---

## Lifecycle

### States

| State | Description |
|-------|-------------|
| `idle` | No position. Waiting for next entry opportunity. |
| `entry_open` | Sell limit order on Kraken book. Waiting for fill. |
| `exit_open` | Entry filled. Buy limit exit on book. Waiting for fill or timeout. |
| `cooldown` | Post-cycle delay before next entry. |

### Transitions

**IDLE → ENTRY_OPEN:**
- Regime must be RANGING (consensus)
- DOGE balance ≥ required volume + reserve
- Order headroom ≥ `RANGER_MIN_HEADROOM`
- Place sell limit at `market_price * (1 + RANGER_ENTRY_PCT/100)`
- Volume = `RANGER_ORDER_SIZE_USD / entry_price`, rounded per pair rules
- Volume must be ≥ `min_volume` (13 DOGE) or skip

**ENTRY_OPEN → EXIT_OPEN (entry fill):**
- Sell filled. Record entry price, volume, timestamp.
- Compute exit price: `entry_price * (1 - profit_margin)` where
  `profit_margin = max(RANGER_PROFIT_PCT, ROUND_TRIP_FEE_PCT + 0.20) / 100`
- Place buy limit at exit price

**ENTRY_OPEN → IDLE (entry timeout):**
- Entry hasn't filled within `RANGER_ENTRY_TIMEOUT_SEC`
- Cancel sell order on Kraken
- Return to idle immediately (no cooldown — entry never filled, no risk)

**EXIT_OPEN → COOLDOWN (exit fill):**
- Buy exit filled. Round trip complete.
- Profit = `(entry_price - exit_price) * volume - fees`
- Increment `cycles_today`, add to `profit_today`
- Enter cooldown

**EXIT_OPEN → IDLE (exit timeout / orphan):**
- Exit hasn't filled within `RANGER_EXIT_TIMEOUT_SEC`
- Cancel buy exit on Kraken
- Record orphan: entry_price, volume, timestamp, market_price_at_orphan
- The sold DOGE is gone; USD from the sale remains in balance
- Increment `orphans_today`, add entry cost to `orphan_exposure_usd`
- Enter cooldown

**EXIT_OPEN → IDLE (regime shift):**
- Regime changed away from RANGING
- Cancel buy exit on Kraken
- Record as regime-triggered orphan (same accounting as timeout orphan)
- Go idle (no cooldown — regime gate prevents re-entry anyway)

**COOLDOWN → IDLE:**
- `RANGER_COOLDOWN_SEC` elapsed since last state change
- Return to idle for next cycle

---

## Entry/Exit Mechanics

### Entry (sell side)

```
entry_price = market_price * (1 + RANGER_ENTRY_PCT / 100)
volume = compute_order_volume(entry_price, cfg, RANGER_ORDER_SIZE_USD)
```

- Maker limit sell — earns maker fee rate (0.25%)
- Uses DOGE from free balance (not from sticky slot reserves)
- Entry distance default 0.15% above market (same as current `entry_pct`)

### Exit (buy side)

```
min_margin = (ROUND_TRIP_FEE_PCT + 0.20) / 100   # fees + 0.20% floor
target_margin = RANGER_PROFIT_PCT / 100
margin = max(min_margin, target_margin)
exit_price = entry_price * (1 - margin)
```

- Maker limit buy
- Floor ensures every completed cycle is profitable after fees
- At current fees (0.50% round-trip), minimum exit distance = 0.70%

### Orphan Economics

When an exit is orphaned:
- We sold DOGE at `entry_price`, received USD
- We failed to buy back at `exit_price`
- Net effect: converted DOGE → USD at `entry_price`
- This is not a realized loss — it's a position that didn't round-trip
- The USD stays in balance and becomes available for sticky slot B-side entries
- Over time, orphaning gradually rebalances DOGE-heavy accounts toward USD

At $2/trade, each orphan converts ~20 DOGE to ~$2 USD. With 832K DOGE
available, this is negligible. The profit from completed cycles dwarfs the
opportunity cost of orphaned ones in a ranging market.

---

## Regime Gating

Rangers activate **only** during RANGING consensus regime.

| Event | Action |
|-------|--------|
| Regime → RANGING | Rangers resume from idle (begin placing entries) |
| Regime → BULLISH/BEARISH | Cancel all open ranger orders, go idle |
| Regime oscillating | Rangers naturally pause/resume with regime |

No debounce or hysteresis — just follow the consensus regime. If the regime
flips rapidly, rangers will cycle less (entries get cancelled). This is fine;
it's self-limiting.

---

## Capital Management

### DOGE Reserve

```
RANGER_DOGE_RESERVE = 100.0  # keep at least 100 DOGE untouched
```

Before placing a sell entry, check:
```
available_doge - entry_volume >= RANGER_DOGE_RESERVE
```

With 832K DOGE, this is a trivial guard.

### No USD Required

Rangers only place sell entries (DOGE → USD). They never need USD to start.
This is the entire point: bootstrapping from a DOGE-rich, USD-poor state.

### Interaction with Sticky Slots

- Rangers use free DOGE balance, not slot-reserved capital
- USD generated by ranger sells (including orphans) flows into the general
  balance, making it available for sticky slot B-side buy entries
- Rangers don't compete with sticky slots — they complement them

---

## Configuration

| Config | Default | Description |
|--------|---------|-------------|
| `RANGER_ENABLED` | `false` | Master enable |
| `RANGER_MAX_SLOTS` | `3` | Max concurrent rangers |
| `RANGER_ORDER_SIZE_USD` | `2.00` | USD-equivalent per entry |
| `RANGER_ENTRY_PCT` | `0.15` | Entry distance % above market |
| `RANGER_PROFIT_PCT` | `1.20` | Target profit % per cycle |
| `RANGER_ENTRY_TIMEOUT_SEC` | `300` | Cancel unfilled entry after 5 min |
| `RANGER_EXIT_TIMEOUT_SEC` | `1350` | Orphan unfilled exit after 22.5 min |
| `RANGER_COOLDOWN_SEC` | `30` | Pause between cycles |
| `RANGER_MIN_HEADROOM` | `10` | Min open-order headroom to place |
| `RANGER_DOGE_RESERVE` | `100.0` | Min DOGE to keep untouched |

All configurable via environment variables. Sane defaults — works out of the
box with `RANGER_ENABLED=true`.

---

## State Tracking

Per-ranger state (in memory, not persisted — rangers are ephemeral):

```python
@dataclass
class RangerState:
    ranger_id: int          # 0..MAX_SLOTS-1
    stage: str              # idle | entry_open | exit_open | cooldown
    entry_txid: str         # Kraken order ID for sell entry
    exit_txid: str          # Kraken order ID for buy exit
    entry_price: float      # sell entry price
    entry_volume: float     # DOGE volume
    exit_price: float       # buy exit target price
    entry_filled_at: float  # timestamp of entry fill
    stage_entered_at: float # timestamp of last state change
    last_error: str         # last gate failure reason
```

Aggregate counters (reset daily, not persisted):

```python
cycles_today: int
profit_today: float       # USD
orphans_today: int
orphan_exposure_usd: float  # total USD from orphaned sells
```

No position ledger entries. No Supabase persistence. Rangers are stateless
across restarts — if the bot restarts, any open ranger orders are found via
reconciliation (matching userref prefix) and cancelled. Fresh start.

---

## Reconciliation on Startup

On startup, scan open orders for ranger userref prefix. Cancel any found.
Rangers always start clean from idle. This avoids stale state bugs entirely.

Userref convention: `900_000 + ranger_id` (e.g., 900000, 900001, 900002).

---

## Status Payload

Add to `/api/status` response:

```json
"rangers": {
  "enabled": true,
  "active": 3,
  "regime_ok": true,
  "cycles_today": 14,
  "profit_today": 0.1847,
  "orphans_today": 2,
  "orphan_exposure_usd": 4.02,
  "doge_reserve": 100.0,
  "slots": [
    {
      "ranger_id": 0,
      "stage": "exit_open",
      "entry_price": 0.10146,
      "exit_price": 0.10075,
      "entry_volume": 19.71,
      "age_sec": 342.5,
      "last_error": ""
    }
  ]
}
```

---

## Dashboard

### Ranger Panel (in summary area)

Small card showing:
- **Status**: "3 rangers active" or "paused (BULLISH)"
- **Today**: "14 cycles, +$0.18 profit, 2 orphans"
- **Per-ranger**: stage badge (IDLE / ENTRY / EXIT), age timer

Use `.ranger-badge` CSS class — green for active cycling, gray for idle,
amber for exit_open approaching timeout.

No modal, no spawn button. Rangers auto-activate when `RANGER_ENABLED=true`
and regime is RANGING. Simple.

---

## Implementation Notes

### Where It Lives

- **Config**: `config.py` — add `RANGER_*` variables
- **Engine**: `bot.py` — new `_run_ranger_engine()` method, ~150 lines
- **Main loop**: Call `_run_ranger_engine(loop_now)` after existing
  churner engine call (line ~14612)
- **Dashboard**: `dashboard.py` — ranger panel in summary area, ~50 lines HTML
- **Status**: Add `rangers` block to `_build_status_payload()`

### Order Placement

Reuse existing `_place_order()` and `_cancel_order()` methods. Rangers are
just another caller of the Kraken client.

### Fill Detection

Rangers check their own order status each main loop cycle via the batch
order query (`_cached_order_info`). If entry_txid shows as "closed" →
entry filled. If exit_txid shows as "closed" → exit filled.

### Estimated Code Size

~200 lines in bot.py (engine + status), ~50 lines in dashboard.py,
~20 lines in config.py. Total ~270 lines. No new modules.

---

## What This Replaces

Rangers make the churner unnecessary for the primary use case. The churner
can remain as an opt-in self-healing mechanism (`CHURNER_ENABLED`), but
rangers handle the "I have DOGE and want to cycle during RANGING" case
without any of the churner's complexity:

| Aspect | Churner | Ranger |
|--------|---------|--------|
| Purpose | Heal stuck positions | Generate profit from idle DOGE |
| Requires parent position | Yes | No |
| Requires position ledger | Yes | No |
| Subsidy routing | Yes | No |
| Lines of code | ~900 | ~270 |
| Capital source | USD reserve or DOGE | DOGE only |
| Works with $0 USD | Barely | By design |
| Orphan handling | N/A (no orphans) | Cancel + move on |
| State persistence | Yes (complex) | No (ephemeral) |

---

## Risks

1. **Orphan accumulation in trending market**: If regime detection is slow
   to shift from RANGING → BULLISH/BEARISH, rangers may place sells that
   become orphans as price trends away. Mitigated by short exit timeout
   (22.5 min) and small order size ($2).

2. **DOGE depletion**: Each orphan converts ~20 DOGE to USD permanently.
   At 3 orphans/day, that's 60 DOGE/day — negligible vs 832K balance.
   Would take 38 years to deplete at that rate.

3. **Fee drag on orphans**: Entry sells pay 0.25% maker fee even when
   orphaned. At $2/trade: $0.005 fee per orphan. Negligible.

4. **Order headroom**: Each active ranger uses 1-2 open orders. With
   3 rangers and 183 headroom, this is nothing.

---

## Success Criteria

- `cycles_today > 0` within first RANGING window after deploy
- Profit per completed cycle ≥ 0.70% after fees (at default settings)
- Orphan rate < 30% of total cycles during stable RANGING
- Dashboard shows live ranger activity
- No interaction with or dependency on churner, position ledger, or subsidy
