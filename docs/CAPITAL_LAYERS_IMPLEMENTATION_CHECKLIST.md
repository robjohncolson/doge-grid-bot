# Capital Layers + Slot Alias Implementation Checklist

Version: v1.0
Date: 2026-02-14
Depends on: `docs/CAPITAL_LAYERS_SLOT_ALIAS_SPEC.md`

---

## 0) Execution Order

1. `config.py` (constants/defaults)
2. `bot.py` (runtime state, sizing logic, status, actions, persistence)
3. `dashboard.py` (controls + rendering + command handling)
4. `tests/test_hardening_regressions.py` (unit/integration coverage)
5. Smoke test run + manual verification pass

---

## 1) `config.py`

### Add new constants

- [ ] Add `CAPITAL_LAYER_DOGE_PER_ORDER = 1.0`
- [ ] Add `CAPITAL_LAYER_ORDER_BUDGET = 225`
- [ ] Add `CAPITAL_LAYER_BALANCE_BUFFER = 1.03`
- [ ] Add `CAPITAL_LAYER_DEFAULT_SOURCE = "AUTO"`
- [ ] Add `SLOT_ALIAS_POOL` as ordered CSV/string list with:
  - `wow,such,much,very,many,so,amaze,plz,coin,moon,hodl,treat,shibe,bork,snoot,floof,smol,boop,wag,zoom,paws,mlem,blep,sniff`

### Validation

- [ ] Validate `CAPITAL_LAYER_DEFAULT_SOURCE` in `{"AUTO","DOGE","USD"}` with safe fallback to `AUTO`
- [ ] Validate parsed alias pool is non-empty, otherwise fall back to hardcoded defaults

---

## 2) `bot.py`

### 2.1 Runtime data model and helpers

- [ ] Extend `SlotRuntime` dataclass with `alias: str = ""`
- [ ] Add runtime fields in `BotRuntime.__init__`:
  - [ ] `self.target_layers`
  - [ ] `self.effective_layers`
  - [ ] `self.layer_last_add_event`
  - [ ] `self.slot_alias_pool`
  - [ ] `self.slot_alias_recycle_queue`
  - [ ] `self.slot_alias_fallback_counter`
- [ ] Add alias helpers:
  - [ ] `_allocate_slot_alias() -> str`
  - [ ] `_release_slot_alias(alias: str) -> None`
  - [ ] `_slot_label(slot: SlotRuntime) -> str`
- [ ] Ensure slot bootstrap paths assign alias (`initialize` and `add_slot`)
- [ ] Ensure slot removal path releases alias (`remove_slot`)

### 2.2 Snapshot persistence

- [ ] Add new fields in `_global_snapshot()`:
  - [ ] `target_layers`
  - [ ] `layer_last_add_event`
  - [ ] `slot_alias_recycle_queue`
  - [ ] `slot_alias_fallback_counter`
  - [ ] slot aliases in slot map (parallel `slot_aliases` map keyed by slot_id)
- [ ] Read those fields in `_load_snapshot()` with backward-compatible defaults
- [ ] If snapshot has old slots with no alias, backfill aliases deterministically

### 2.3 Layer math and balance checks

- [ ] Add helper `LAYER_STEP_DOGE_EQ = CAPITAL_LAYER_DOGE_PER_ORDER * CAPITAL_LAYER_ORDER_BUDGET`
- [ ] Add helper to compute active side counts:
  - [ ] Count active sell orders (`st.orders` + `st.recovery_orders` with `txid`)
  - [ ] Count active buy orders (`st.orders` + `st.recovery_orders` with `txid`)
- [ ] Add helper for free balances:
  - [ ] Prefer loop values (`_loop_available_usd/_doge`) when set
  - [ ] Else use `ledger.available_*` if synced
  - [ ] Else use last observed balance snapshot
- [ ] Add helper for mark price (`self.last_price`, guarded > 0)
- [ ] Implement `effective_layers` computation each status/update cycle:
  - [ ] `max_layers_from_doge = floor(free_doge / (sell_count * buffer))`
  - [ ] `max_layers_from_usd = floor(free_usd / (buy_count * mark * buffer))`
  - [ ] `effective_layers = min(target_layers, max_layers_from_doge, max_layers_from_usd)` clamped >= 0
- [ ] Add gap calculations:
  - [ ] `gap_layers`
  - [ ] `gap_doge_now`
  - [ ] `gap_usd_now`

### 2.4 Add/remove layer actions

- [ ] Add `BotRuntime.add_layer(source: str) -> tuple[bool, str]`
- [ ] Add `BotRuntime.remove_layer() -> tuple[bool, str]`
- [ ] `add_layer` validation:
  - [ ] reject invalid source
  - [ ] reject if mark price unavailable/non-positive
  - [ ] DOGE source: require `free_doge >= 225`
  - [ ] USD source: require `free_usd >= 225 * mark`
  - [ ] AUTO source: require `free_doge + free_usd/mark >= 225`
- [ ] On success:
  - [ ] increment `target_layers`
  - [ ] write `layer_last_add_event` (`timestamp`, `source`, `price_at_commit`, `usd_equiv_at_commit`)
- [ ] On remove:
  - [ ] fail if already zero
  - [ ] decrement `target_layers`

### 2.5 Order sizing integration (drip model)

- [ ] Keep `_slot_order_size_usd()` as central sizing function
- [ ] Integrate layer increment there, without cancel/replace behavior:
  - [ ] Convert per-layer DOGE increment to USD using current slot market price (or runtime mark fallback)
  - [ ] Add `effective_layers * 1 DOGE * mark_price` to base USD sizing
  - [ ] Preserve existing rebalancer logic and guards
- [ ] Ensure layer increment applies only to newly created orders by relying on existing place-order path (no retroactive mutation)

### 2.6 API and status payload

- [ ] In `status_payload()` slot rows:
  - [ ] Add `slot_alias`
  - [ ] Add `slot_label`
- [ ] Add top-level `capital_layers` object with spec fields:
  - [ ] `target_layers`, `effective_layers`, `doge_per_order_per_layer`, `layer_order_budget`, `layer_step_doge_eq`
  - [ ] `add_layer_usd_equiv_now`, `funding_source_default`
  - [ ] `active_sell_orders`, `active_buy_orders`
  - [ ] `orders_at_funded_size`, `open_orders_total`
  - [ ] `gap_layers`, `gap_doge_now`, `gap_usd_now`
  - [ ] `last_add_layer_event`
- [ ] In `DashboardHandler.do_POST()`:
  - [ ] parse `add_layer` with `source`
  - [ ] parse `remove_layer`
  - [ ] route to runtime methods
  - [ ] include new actions in unknown-action allowlist

---

## 3) `dashboard.py`

### 3.1 Controls/UI wiring

- [ ] Add `Capital Layers` section in left panel
- [ ] Add controls:
  - [ ] `Add Layer (+1 DOGE/order)` button
  - [ ] `Remove Layer (-1 DOGE/order)` button
  - [ ] Funding source selector (`AUTO`, `DOGE`, `USD`)
- [ ] Add value rows:
  - [ ] target size
  - [ ] funded now
  - [ ] step size
  - [ ] USD equiv now
  - [ ] propagation X/Y
  - [ ] funding gap

### 3.2 Client actions and confirmations

- [ ] Add handlers:
  - [ ] `requestAddLayer()`
  - [ ] `requestRemoveLayer()`
- [ ] Add confirm text for add action:
  - [ ] `Commit one layer = +1 DOGE/order across up to 225 orders.`
  - [ ] `This commit step is 225 DOGE-equivalent at current price.`
- [ ] Dispatch API actions:
  - [ ] `dispatchAction("add_layer", {source})`
  - [ ] `dispatchAction("remove_layer")`

### 3.3 Keyboard/command bar integration

- [ ] Extend `parseCommand()` with:
  - [ ] `:layer add [auto|doge|usd]`
  - [ ] `:layer remove`
- [ ] Add to command completions list
- [ ] Reuse existing confirm modal for `:layer add` and `:layer remove`

### 3.4 Slot alias display

- [ ] Update slot pills in `renderSlots()`:
  - [ ] replace `#<id>` primary text with alias
  - [ ] keep slot id in secondary text/tooltip for debugging
- [ ] Update selected slot state bar to include alias label
- [ ] Keep all API actions keyed by numeric `slot_id` (no behavior change)

### 3.5 Render path

- [ ] In `renderTop()` (or dedicated render function), consume `s.capital_layers`
- [ ] Format missing values safely (`-`) when backend has not yet provided fields
- [ ] Ensure polling + mode debounce behavior remains unchanged

---

## 4) `tests/test_hardening_regressions.py`

### Add runtime tests

- [ ] Alias assignment on new slot creation
- [ ] Alias recycle queue behavior on remove/add sequences
- [ ] Fallback alias format after pool exhaustion
- [ ] `add_layer` success for DOGE source
- [ ] `add_layer` success for USD source
- [ ] `add_layer` success for AUTO source (mixed balances)
- [ ] `add_layer` reject when underfunded
- [ ] `remove_layer` reject at zero
- [ ] `effective_layers <= target_layers` invariant

### Add status payload tests

- [ ] `status_payload` includes `capital_layers`
- [ ] each slot payload includes `slot_alias` and `slot_label`
- [ ] gap fields are numeric and non-negative in underfunded case

### Add API handler tests

- [ ] `/api/action` accepts `add_layer` with valid source
- [ ] `/api/action` rejects `add_layer` with invalid source (400)
- [ ] `/api/action` accepts `remove_layer`

---

## 5) Manual Verification Script

### Boot and baseline

- [ ] Start bot with `target_layers=0`
- [ ] Confirm `capital_layers` appears in `/api/status`
- [ ] Confirm slot pills show aliases

### Layer flow

- [ ] Add one layer with `AUTO`
- [ ] Verify toast/response includes commit-time price and USD equivalent
- [ ] Confirm `target_layers` increments and `effective_layers` resolves
- [ ] Confirm propagation moves gradually as orders recycle (no mass cancel)

### Funding-source checks

- [ ] Add one layer with `DOGE` when DOGE is sufficient, then insufficient
- [ ] Add one layer with `USD` when USD is sufficient, then insufficient
- [ ] Verify rejections include clear required vs available message

### Underfunded behavior

- [ ] Force `target_layers > effective_layers` by lowering free balances
- [ ] Confirm bot continues trading and does not halt/pause
- [ ] Confirm `gap_layers`, `gap_doge_now`, `gap_usd_now` are visible and sensible

---

## 6) Definition of Done

- [ ] All checklist items complete
- [ ] Existing tests pass
- [ ] New tests pass
- [ ] No regression in pause/resume, slot add/remove, soft-close flows
- [ ] No regression in status polling or keyboard command behavior
