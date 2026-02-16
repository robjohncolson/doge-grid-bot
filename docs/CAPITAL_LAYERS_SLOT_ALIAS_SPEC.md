# Capital Layers + Slot Alias Spec

Version: v1.0
Date: 2026-02-14
Status: Implementation-ready
Scope: Manual capital layers (225-order increment model) + doge-themed slot aliases
Target: `bot.py`, `dashboard.py`, `config.py`, `/api/status`, `/api/action`

---

## 1. Summary

Add two operator-facing upgrades:

1. Doge-themed slot aliases for UI/log readability.
2. Manual "Capital Layers" where each layer increases target order size by `+1 DOGE/order`, using a fixed commitment unit of `225 DOGE-equivalent` per layer (matching max order budget).

Sizing uses a drip model: only newly placed orders use the latest layer value. Existing live orders are not mass-canceled.

---

## 2. Locked Decisions

1. Layer increment is fixed at `+1 DOGE/order`.
2. Commitment step is fixed at `225 DOGE-equivalent` per layer.
3. Layer controls are manual only (`Add Layer`, `Remove Layer`), no auto-scaling.
4. Add-layer flow supports funding source choice: `DOGE`, `USD`, or `AUTO`.
5. USD equivalent is quoted at commit-time mark price.
6. Runtime remains balance-based (no forced market conversion and no separate ledger account).
7. Underfunded states do not fail the bot; runtime uses `effective_layers <= target_layers`.
8. Slot IDs remain numeric internally; aliases are display/log labels only.

---

## 3. Scope

### In

1. Slot alias pool and assignment/reuse policy.
2. Capital layer config and runtime sizing rules.
3. Add/remove layer API actions and validations.
4. Dashboard controls, funding preview, and propagation telemetry.
5. Status payload additions for layers and aliases.

### Out

1. Automatic asset conversion trades to satisfy layers.
2. Auto-layering based on PnL or volatility.
3. Changes to state-machine recovery logic.
4. Changes to entry/profit percent semantics.
5. Non-doge naming themes (future option, not v1).

---

## 4. Slot Alias Specification

### 4.1 Alias Pool

Ordered default pool:

`wow, such, much, very, many, so, amaze, plz, coin, moon, hodl, treat, shibe, bork, snoot, floof, smol, boop, wag, zoom, paws, mlem, blep, sniff`

### 4.2 Assignment Rules

1. Every slot keeps immutable internal `slot_id` (int).
2. UI and logs display alias by default; internal numeric ID is still present in API payloads.
3. New slot gets the next unused alias from pool order.
4. Removed slot alias goes into a recycle queue.
5. Recycle queue is used only after all never-used pool aliases are exhausted.
6. Fallback format after pool exhaustion: `doge-01`, `doge-02`, etc.

### 4.3 API Additions (Slot Object)

Add to each slot in `/api/status`:

1. `slot_alias: string`
2. `slot_label: string` (formatted display label, default alias)

---

## 5. Capital Layer Model

### 5.1 Constants

1. `LAYER_DOGE_PER_ORDER = 1.0`
2. `LAYER_ORDER_BUDGET = 225`
3. `LAYER_STEP_DOGE_EQ = LAYER_DOGE_PER_ORDER * LAYER_ORDER_BUDGET = 225`
4. `LAYER_BALANCE_BUFFER = 1.03` (3% runtime balance buffer)

### 5.2 Operator Controls

1. `Add Layer (+1 DOGE/order)`
2. `Remove Layer (-1 DOGE/order)`
3. Funding source selector for add action:
   - `DOGE`
   - `USD`
   - `AUTO` (recommended; DOGE + USD equivalent)

### 5.3 Add-Layer Validation

For add action, snapshot mark price `P_add` (30s median/EMA source used elsewhere by runtime).

Validation by source:

1. `DOGE`: require `free_doge >= 225`
2. `USD`: require `free_usd >= 225 * P_add`
3. `AUTO`: require `free_doge + (free_usd / P_add) >= 225`

If check passes:

1. `target_layers += 1`
2. Record add event metadata:
   - timestamp
   - source
   - `price_at_commit = P_add`
   - `usd_equiv_at_commit = 225 * P_add`

If check fails:

1. Reject action with required vs available breakdown.

### 5.4 Remove-Layer Validation

1. Require `target_layers > 0`.
2. On success: `target_layers -= 1`.
3. No cancel/replace of existing orders.

---

## 6. Runtime Sizing and Underfunded Behavior

### 6.1 Drip Propagation

Only newly created orders use the current layer value. Existing orders keep prior size until naturally filled/canceled/replaced by normal flow.

### 6.2 Effective Layers

Runtime computes an `effective_layers` value from live free balances and active side counts:

Definitions:

1. `S = max(1, active_sell_orders)`
2. `B = max(1, active_buy_orders)`
3. `P_now = current mark price`

Capacity bounds:

1. `max_layers_from_doge = floor(free_doge / (S * LAYER_BALANCE_BUFFER))`
2. `max_layers_from_usd = floor(free_usd / (B * P_now * LAYER_BALANCE_BUFFER))`
3. `effective_layers = max(0, min(target_layers, max_layers_from_doge, max_layers_from_usd))`

Errata (2026-02-16): generalized form includes `DOGE_PER_ORDER` in both denominators (`S * DOGE_PER_ORDER * LAYER_BALANCE_BUFFER` and `B * DOGE_PER_ORDER * P_now * LAYER_BALANCE_BUFFER`); formulas above are the `DOGE_PER_ORDER = 1.0` simplification.

### 6.3 Order Size Rule

For each new order:

1. Compute existing base size from current runtime logic.
2. Convert base to DOGE quantity as currently done.
3. Add layer increment:
   - `extra_doge = effective_layers * 1.0`
4. Final quantity:
   - `qty_doge = base_qty_doge + extra_doge`

### 6.4 Underfunded Handling

1. `target_layers` is the operator intent.
2. `effective_layers` is what balances currently support.
3. If underfunded, bot keeps trading with `effective_layers` (no hard error, no emergency pause).
4. Re-evaluate every poll/action cycle and auto-promote when balances allow.
5. No mass cancel/replace to force immediate resizing.

Funding gap telemetry:

1. `gap_layers = target_layers - effective_layers`
2. `gap_doge_now = max(0, (target_layers - max_layers_from_doge) * S)`
3. `gap_usd_now = max(0, (target_layers - max_layers_from_usd) * B * P_now)`

---

## 7. Dashboard UX Copy

Section title: `Capital Layers`

Controls and labels:

1. `Add Layer (+1 DOGE/order)`
2. `Remove Layer (-1 DOGE/order)`
3. `Funding Source: AUTO | DOGE | USD`
4. `Target size: +N DOGE/order`
5. `Funded now: +M DOGE/order`
6. `Step size: 225 DOGE-equivalent`
7. `USD equiv right now: $X` (for one layer, at current mark)
8. `Propagation: X/Y orders at funded size`
9. `Funding gap: short A DOGE and B USD`
10. `Orders resize gradually as they recycle. No mass cancel/replace.`

Add-layer confirmation text:

1. `Commit one layer = +1 DOGE/order across up to 225 orders.`
2. `This commit step is 225 DOGE-equivalent at current price.`

---

## 8. API Changes

### 8.1 POST `/api/action`

Add actions:

1. `add_layer`
   - payload: `{ "action": "add_layer", "source": "AUTO|DOGE|USD" }`
2. `remove_layer`
   - payload: `{ "action": "remove_layer" }`

Response message examples:

1. `layer added: target=3 (+1 DOGE/order), commit step 225 DOGE-eq @ $0.0942`
2. `layer add rejected: need 225 DOGE-eq, available 141.7 DOGE-eq`
3. `layer removed: target=2 (+2 DOGE/order)`

### 8.2 GET `/api/status`

Add top-level object `capital_layers`:

1. `target_layers: int`
2. `effective_layers: int`
3. `doge_per_order_per_layer: float` (1.0)
4. `layer_order_budget: int` (225)
5. `layer_step_doge_eq: float` (225.0)
6. `add_layer_usd_equiv_now: float`
7. `funding_source_default: "AUTO"|"DOGE"|"USD"`
8. `active_sell_orders: int`
9. `active_buy_orders: int`
10. `orders_at_funded_size: int`
11. `open_orders_total: int`
12. `gap_layers: int`
13. `gap_doge_now: float`
14. `gap_usd_now: float`
15. `last_add_layer_event: object|null`

---

## 9. Safety Invariants

1. Layer logic must never bypass existing min-volume and precision guards.
2. `effective_layers` must never exceed `target_layers`.
3. Invalid/unknown funding source must reject with HTTP 400.
4. Layer actions must be lock-safe and pre-validated before mutating runtime state.
5. Existing pause/resume, soft-close, and capacity telemetry behavior remains unchanged.

---

## 10. Testing

### Unit

1. Add-layer validation for `DOGE`, `USD`, `AUTO`.
2. Effective layer math across balance/price/side-count combinations.
3. Underfunded gap math (`gap_layers`, `gap_doge_now`, `gap_usd_now`).
4. Alias assignment, recycle-queue behavior, fallback naming.

### Integration

1. `POST /api/action add_layer/remove_layer` success/failure paths.
2. `GET /api/status` returns full `capital_layers` and slot alias fields.
3. Drip behavior: existing orders unchanged, newly placed orders include layer increment.
4. Underfunded runtime continues safely with reduced `effective_layers`.

### Regression

1. Existing dashboard controls and keyboard shortcuts remain functional.
2. No behavior regressions in recovery order handling.
3. No changes to current order lifecycle outside sizing increment.

---

## 11. Rollout

1. Deploy with `target_layers = 0` default.
2. Enable alias display first (pure presentation).
3. Enable layer controls and status telemetry.
4. First live test: add exactly one layer with `AUTO`, observe:
   - `target_layers=1`
   - `effective_layers` response
   - propagation trend over normal recycle window
5. Expand to additional layers only after 24h stable observation.
