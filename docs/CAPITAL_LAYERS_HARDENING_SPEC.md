# Capital Layers Hardening Spec

Version: v0.1.0
Date: 2026-02-16
Status: Review
Depends on: `docs/CAPITAL_LAYERS_SLOT_ALIAS_SPEC.md` v1.0
Scope: Close behavioral gaps between v1.0 spec and current implementation

---

## 0. Motivation

The Capital Layers feature is substantially implemented and faithful to the v1.0
spec. This hardening pass addresses:

1. **Formula drift** — the effective-layers formula in code includes an extra
   `doge_per_order` factor the spec omits. Numerically equivalent today
   (`DOGE_PER_ORDER = 1.0`) but silently wrong if that constant ever changes.
2. **Propagation accuracy** — current counter re-derives "expected volume" per
   order per poll, which calls `_recompute_effective_layers()` on every order
   (expensive, side-effecting). Propagation should be a lightweight snapshot.
3. **Gap text when fully funded** — dashboard shows "short 0.000 DOGE and
   $0.0000" even when `gap_layers == 0`. Spec intent is to show nothing or a
   positive confirmation.
4. **No visual distinction between target 0 and target > 0** — every telemetry
   row renders even when the operator has never added a layer.
5. **remove_layer success message is wrong** — says
   `+{self.target_layers} DOGE/order` instead of using `DOGE_PER_ORDER *
   target_layers`.
6. **Missing test coverage** — the implementation checklist (Section 4) lists
   15 test cases; only 7 exist.
7. **Dashboard-side edge cases** — no disable state on Remove button when
   target is 0; no loading/error feedback after action dispatch.
8. **Effective layers recomputed on every `_slot_order_size_usd()` call** —
   this means one sizing call mutates `self.effective_layers` as a side effect,
   and N sizing calls per cycle each do N full scans of all orders. Should
   compute once per cycle and cache.

---

## 1. Bugs to Fix

### 1.1 `remove_layer` success message

**Current** (bot.py:5742):
```python
f"layer removed: target={self.target_layers} (+{self.target_layers:.0f} DOGE/order)"
```

The `+N DOGE/order` text uses raw `target_layers` instead of
`target_layers * DOGE_PER_ORDER`. With `DOGE_PER_ORDER = 1.0` they happen to
match, but the message semantics are wrong if the constant changes.

**Fix**: Use `target_layers * config.CAPITAL_LAYER_DOGE_PER_ORDER`.

### 1.2 Gap text when fully funded

**Current** (dashboard.py:1878): Always renders
`short 0.000 DOGE and $0.0000` even at `gap_layers == 0`.

**Fix**: When `gap_layers == 0`, display `"fully funded"` instead.

### 1.3 Hint text always visible

**Current** (dashboard.py:1879-1882): Shows "Orders resize gradually..."
even when `target_layers == 0` and no layers exist. Clutters the UI.

**Fix**: When `target_layers == 0`, hide the hint text entirely.

---

## 2. Effective Layers Caching

### Problem

`_slot_order_size_usd()` calls `_recompute_effective_layers()` on every
invocation. During `_count_orders_at_funded_size()`, this is called once per
live order (could be 100+ times per poll). Each call:

- Queries `_available_free_balances()` (may hit Kraken API if `prefer_fresh`)
- Scans all orders via `_active_order_side_counts()`
- Mutates `self.effective_layers` as side effect

### Fix

1. Add a cycle-level cache: `_loop_effective_layers: dict | None = None`.
2. At the start of each main loop iteration (after balance refresh),
   call `_recompute_effective_layers()` once, store result in cache.
3. `_slot_order_size_usd()` reads `effective_layers` from cache instead
   of recomputing.
4. `_count_orders_at_funded_size()` also reads from cache.
5. Cache is cleared at the end of each loop iteration (same pattern as
   `_loop_available_usd/_doge`).

### Safety

- `add_layer()` and `remove_layer()` invalidate the cache (set to `None`)
  so the next read triggers a fresh computation.
- Status endpoint still calls `_recompute_effective_layers()` if cache is
  stale (same as balance fields).

---

## 3. Effective Layers Formula Alignment

### Spec (Section 6.2)

```
max_layers_from_doge = floor(free_doge / (S * LAYER_BALANCE_BUFFER))
max_layers_from_usd  = floor(free_usd / (B * P_now * LAYER_BALANCE_BUFFER))
```

### Code (bot.py:635-637)

```python
max_layers_from_doge = floor(free_doge / (sell_den * doge_per_order * buffer))
max_layers_from_usd  = floor(free_usd / (buy_den * doge_per_order * price * buffer))
```

The code includes `doge_per_order` which the spec omits. The code is actually
**more correct** — the spec implicitly assumes `DOGE_PER_ORDER = 1.0`.

### Resolution

**Update the spec**, not the code. The code's formula generalizes correctly.
Add a note to v1.0 spec errata documenting this.

---

## 4. Dashboard Hardening

### 4.1 Collapsed state when target_layers == 0

When no layers have been added, the six telemetry rows all show zero/empty
values. This wastes space and confuses operators who haven't used the feature.

**Fix**: When `target_layers == 0`:
- Show controls (Add/Remove/Source) normally — operator needs them to add.
- Collapse telemetry rows into a single line: `"No layers active"`.
- Remove Layer button disabled (greyed out, `pointer-events: none`).

When `target_layers > 0`:
- Expand all six telemetry rows as currently rendered.
- Remove Layer button enabled.

### 4.2 Remove button disable state

**Fix**: Set `removeLayerBtn.disabled = (targetLayers <= 0)` in the render
path. Already rendered each poll cycle, so no extra wiring needed. Style
disabled buttons with `opacity: 0.4; cursor: not-allowed`.

### 4.3 Gap text improvements

| `gap_layers` | Display |
|---|---|
| `0` | `"fully funded"` (green text) |
| `> 0` | `"short {gap_doge} DOGE and ${gap_usd}"` (amber text) |

### 4.4 Action feedback

After `dispatchAction('add_layer', ...)` or `dispatchAction('remove_layer')`:

- On success: existing toast system shows the returned message (already works
  if `dispatchAction` handles it).
- On failure: toast shows rejection reason in amber.

**Verify**: Confirm that `dispatchAction()` already surfaces the response
message in a toast. If not, wire it.

### 4.5 Funding source persistence

**Current**: `layerSourceSelect` syncs to `funding_source_default` from status
payload each poll, but only if the select is not focused. This means the
operator's choice resets after blur.

**Fix**: The operator's dropdown choice should be the source sent with
`add_layer`. The dropdown is purely client-side input — stop syncing it to
the backend default. Remove the `layerSourceSelect.value = sourceDefault`
line. The initial value is `AUTO` (the HTML default), which matches the config
default. If the operator changes it, their choice persists until page reload.

---

## 5. Missing Test Coverage

The v1.0 implementation checklist (Section 4) specifies 15 test cases.
Currently 7 exist. Add the remaining 8:

### 5.1 Runtime tests (to add)

| # | Test | What it verifies |
|---|---|---|
| 1 | `test_add_layer_doge_source_success` | DOGE-only funding with sufficient balance |
| 2 | `test_add_layer_usd_source_success` | USD-only funding with sufficient balance |
| 3 | `test_add_layer_rejects_underfunded_doge` | DOGE source with insufficient free DOGE |
| 4 | `test_add_layer_rejects_underfunded_usd` | USD source with insufficient free USD |
| 5 | `test_add_layer_rejects_underfunded_auto` | AUTO source with insufficient combined |
| 6 | `test_effective_layers_never_exceeds_target` | Invariant across various balance/price combos |
| 7 | `test_alias_fallback_format_after_pool_exhaustion` | After 24 pool aliases + recycled used, format is `doge-NN` |
| 8 | `test_gap_fields_non_negative_when_underfunded` | `gap_layers >= 0`, `gap_doge_now >= 0`, `gap_usd_now >= 0` |

### 5.2 Integration tests (to add)

| # | Test | What it verifies |
|---|---|---|
| 9 | `test_drip_sizing_existing_orders_unchanged` | Adding a layer doesn't mutate volumes of already-placed orders |
| 10 | `test_layer_snapshot_round_trip` | `target_layers`, `effective_layers`, `layer_last_add_event` survive save/load |

---

## 6. Propagation Counter Refinement

### Problem

`_count_orders_at_funded_size()` calls `_slot_order_size_usd()` for every live
order. Each of those calls currently recomputes effective layers (see Section
2). Even after the caching fix, this counter has a subtle accuracy issue:

It computes "expected volume" at the current mark price, but the order was
placed at a different price. If price has moved, the expected volume at
current price differs from what was correct at placement time, causing the
counter to under-report propagation even for correctly-sized orders.

### Fix

Use the **order's own price** (already available as `o.price`) as the price
input to `compute_order_volume()` instead of the current mark price. The
order was sized using its own price at placement time. The current code
already passes `float(o.price)` to `compute_order_volume()`, so this is
actually correct. However, `_slot_order_size_usd()` uses `_layer_mark_price()`
internally for the layer USD conversion. An order placed at $0.09 with 1 layer
got `+$0.09` of layer USD. But at current mark $0.10, the counter expects
`+$0.10` of layer USD, producing a different expected volume.

**Resolution**: For propagation counting only, pass the order's own price
as the mark price override to `_slot_order_size_usd()`. Add an optional
`price_override` parameter.

---

## 7. Edge Cases to Guard

### 7.1 Zero-slot state

When all slots are removed, `_active_order_side_counts()` returns `(0, 0, 0)`.
The `max(1, ...)` denominators in `_recompute_effective_layers()` handle
division-by-zero correctly. **No fix needed**, but add a test.

### 7.2 Price unavailable at startup

Before the first OHLC fetch, `last_price` and slot market prices may be 0.
`_layer_mark_price()` returns 0, and `_recompute_effective_layers()` sets
`max_layers_from_usd = 0`. This means `effective_layers = 0` until price
arrives. This is correct behavior — can't size USD orders without a price.
**No fix needed**, document as expected.

### 7.3 Very large target_layers

No upper bound on `target_layers`. An operator could accidentally click
Add Layer many times. The balance check gates each addition, but rapid
clicking could queue multiple API calls before the first one's balance
deduction is reflected.

**Fix**: Add `MAX_TARGET_LAYERS = 20` config constant. `add_layer()` rejects
if `target_layers >= MAX_TARGET_LAYERS`. Dashboard: disable Add button when at
max. This prevents runaway layer commits. The value 20 allows up to
+20 DOGE/order (at $0.10 = $2.00 increment = ~$450 per layer commitment),
which is well beyond any reasonable manual scaling range.

### 7.4 Concurrent add_layer calls

Two rapid Add Layer clicks could both pass the balance check before either
increments `target_layers`. Both succeed, committing 2 layers on funds
sufficient for only 1. The `effective_layers` computation will immediately
cap down to 1, and the gap telemetry will show the shortfall, so there's
no order-placement risk. But the operator sees `target=2` when they only
validated 1.

**Fix**: Add a simple guard — `add_layer()` checks a
`_layer_action_in_flight` boolean (set True at entry, False at exit). If
True, reject with "layer action already in progress". Since the HTTP handler
is single-threaded (Python GIL + HTTPServer), this is mostly a no-op for
the current architecture but protects against future async refactors.

---

## 8. Implementation Checklist

### 8.1 Bug fixes

- [ ] Fix `remove_layer` success message to use `target_layers * DOGE_PER_ORDER`
- [ ] Fix gap text: show "fully funded" when `gap_layers == 0`
- [ ] Hide hint text when `target_layers == 0`

### 8.2 Performance

- [ ] Add `_loop_effective_layers` cycle cache in `BotRuntime`
- [ ] Populate cache once at start of main loop (after balance refresh)
- [ ] Read cache in `_slot_order_size_usd()` instead of recomputing
- [ ] Clear cache at end of loop iteration
- [ ] Invalidate cache in `add_layer()` and `remove_layer()`

### 8.3 Dashboard

- [ ] Collapse telemetry rows to "No layers active" when `target_layers == 0`
- [ ] Disable Remove button when `target_layers <= 0`
- [ ] Color gap text: green for fully funded, amber for short
- [ ] Stop syncing `layerSourceSelect` to backend default (remove sync line)
- [ ] Disable Add button when `target_layers >= MAX_TARGET_LAYERS`

### 8.4 Guards

- [ ] Add `MAX_TARGET_LAYERS = 20` to config.py
- [ ] Enforce in `add_layer()`
- [ ] Add `_layer_action_in_flight` guard in `add_layer()`/`remove_layer()`

### 8.5 Propagation accuracy

- [ ] Add `price_override` parameter to `_slot_order_size_usd()`
- [ ] Use order's own price when computing expected volume in `_count_orders_at_funded_size()`

### 8.6 Tests

- [ ] `test_add_layer_doge_source_success`
- [ ] `test_add_layer_usd_source_success`
- [ ] `test_add_layer_rejects_underfunded_doge`
- [ ] `test_add_layer_rejects_underfunded_usd`
- [ ] `test_add_layer_rejects_underfunded_auto`
- [ ] `test_effective_layers_never_exceeds_target`
- [ ] `test_alias_fallback_format_after_pool_exhaustion`
- [ ] `test_gap_fields_non_negative_when_underfunded`
- [ ] `test_drip_sizing_existing_orders_unchanged`
- [ ] `test_layer_snapshot_round_trip`
- [ ] `test_zero_slots_effective_layers_safe`

### 8.7 Spec errata

- [ ] Add errata note to v1.0 spec: effective layers formula should include
      `DOGE_PER_ORDER` factor (code is correct, spec simplified)

---

## 9. Rollout

1. Deploy bug fixes + dashboard hardening (no behavior change to sizing).
2. Deploy caching (performance only, no behavior change).
3. Deploy propagation accuracy fix.
4. Deploy new tests.
5. Add MAX_TARGET_LAYERS guard.

---

## 10. Definition of Done

- [ ] All Section 8 checklist items complete
- [ ] All existing tests pass
- [ ] New tests pass
- [ ] No regression in order sizing, pause/resume, slot add/remove
- [ ] Dashboard renders correctly at target_layers = 0, 1, and > 1
- [ ] Gap text shows "fully funded" when effective == target
- [ ] Remove button disabled at target = 0
- [ ] Propagation counter matches manual order volume inspection
