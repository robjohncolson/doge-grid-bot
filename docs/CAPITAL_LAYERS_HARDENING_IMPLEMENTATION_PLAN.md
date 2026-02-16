# Capital Layers Hardening - Implementation Plan

Last updated: 2026-02-16
Parent spec: `docs/CAPITAL_LAYERS_HARDENING_SPEC.md` v0.1.0
Depends on: `docs/CAPITAL_LAYERS_SLOT_ALIAS_SPEC.md` v1.0
Status: **Ready for implementation**

## Goal

Close the remaining correctness, UX, and test-coverage gaps in Capital Layers without changing core trading semantics (drip resizing only, no mass cancel/replace).

## Scope

In scope:
- Runtime correctness fixes (`add_layer`/`remove_layer` messaging, propagation sizing reference price, max target guard, in-flight guard).
- Performance hardening via per-loop effective-layer caching.
- Dashboard behavior hardening (collapsed zero-target state, gap text, control disable states, source selector persistence).
- Missing runtime + integration tests listed in the hardening spec.
- Spec errata note for effective-layer formula generalization.

Out of scope:
- Any change to base order sizing model unrelated to layers.
- Any Kraken API surface changes.
- Any broad dashboard redesign outside Capital Layers section behavior.

## Current Baseline (Code Audit)

1. `bot.py:768` calls `_recompute_effective_layers()` inside `_slot_order_size_usd()` on every sizing call.
2. `bot.py:669` propagation counter uses `_slot_order_size_usd()` without a mark-price override, so layer USD conversion uses current mark, not each order's placement price.
3. `bot.py:5736` remove-layer success message formats DOGE/order using `target_layers` instead of `target_layers * CAPITAL_LAYER_DOGE_PER_ORDER`.
4. `dashboard.py:1868` gap text always renders "short ...", including `gap_layers == 0`.
5. `dashboard.py:1886` source dropdown is re-synced from backend default each poll after blur, overriding operator choice.
6. `dashboard.py` currently has no explicit disable state wiring for remove-at-zero and add-at-max.
7. `tests/test_hardening_regressions.py` includes some layer tests, but the full hardening list is not implemented yet.

## Implementation Order

1. Runtime correctness + guard rails in `config.py` and `bot.py`.
2. Runtime caching + status payload additions in `bot.py`.
3. Dashboard behavior changes in `dashboard.py`.
4. Test expansion in `tests/test_hardening_regressions.py`.
5. Spec errata update in `docs/CAPITAL_LAYERS_SLOT_ALIAS_SPEC.md` (formula note).

## Phase Plan

## Phase 1 - Runtime Guards and Message Correctness

Files:
- `config.py`
- `bot.py`

Changes:
1. Add `CAPITAL_LAYER_MAX_TARGET_LAYERS: int = 20` in `config.py`.
2. Add runtime flag `self._layer_action_in_flight: bool = False` in `BotRuntime.__init__`.
3. Enforce max target in `add_layer()`:
   - Reject when `target_layers >= CAPITAL_LAYER_MAX_TARGET_LAYERS`.
4. Add in-flight guard in both `add_layer()` and `remove_layer()`:
   - Reject action if a layer action is already in progress.
   - Use `try/finally` to guarantee flag reset on exceptions.
5. Fix remove-layer success message to use:
   - `self.target_layers * config.CAPITAL_LAYER_DOGE_PER_ORDER`.
6. Invalidate per-loop layer cache on successful `add_layer()`/`remove_layer()` so subsequent sizing reads are fresh.

Acceptance checks:
- Add rejects with clear message at cap.
- Double-click add/remove cannot commit two actions concurrently.
- Remove success text reports correct DOGE/order when `CAPITAL_LAYER_DOGE_PER_ORDER != 1.0`.

## Phase 2 - Effective-Layers Caching and Propagation Accuracy

Files:
- `bot.py`

Changes:
1. Add `self._loop_effective_layers: dict | None = None` in runtime state.
2. In `begin_loop()`:
   - Clear `_loop_effective_layers` at loop start.
   - After balance sync, compute once with `_recompute_effective_layers()` and cache result.
3. In `end_loop()`:
   - Clear `_loop_effective_layers` with other loop caches.
4. Add helper `_current_layer_metrics(mark_price: float | None = None)`:
   - Return cached metrics when valid.
   - Recompute and refresh cache when cache is missing/stale.
5. Update `_slot_order_size_usd()`:
   - Stop direct recompute call.
   - Use cached metrics for effective layer count.
   - Add optional `price_override: float | None = None` used only for layer USD conversion when provided.
6. Update `_count_orders_at_funded_size()`:
   - Pass each order's own `price` as `price_override` into `_slot_order_size_usd()` for expected-size calculation.
7. Keep `status_payload()` behavior safe:
   - Use cache when present; recompute if absent.

Acceptance checks:
- One poll cycle performs a single effective-layer recompute for steady-state sizing paths.
- Propagation count no longer drifts solely due to mark-price movement.
- No regression in rebalancer/dust sizing paths.

## Phase 3 - Dashboard Hardening

Files:
- `dashboard.py`
- `bot.py` (status payload fields needed by UI)

Changes:
1. Extend `capital_layers` status payload with:
   - `max_target_layers`
2. Capital Layers UI rendering updates:
   - If `target_layers == 0`, collapse six telemetry rows and show one line: `No layers active`.
   - If `target_layers > 0`, render existing telemetry rows.
3. Gap text rules:
   - `gap_layers == 0`: `fully funded` (green).
   - `gap_layers > 0`: `short {gap_doge} DOGE and ${gap_usd}` (amber).
4. Hint text rules:
   - Hide hint when `target_layers == 0`.
   - Show gradual-resize hint only when active.
5. Button states:
   - `removeLayerBtn.disabled = (targetLayers <= 0)`
   - `addLayerBtn.disabled = (targetLayers >= maxTargetLayers)`
   - Apply disabled style (`opacity: 0.4; cursor: not-allowed`).
6. Funding source persistence:
   - Remove poll-time sync assigning `layerSourceSelect.value = sourceDefault`.
   - Keep select as client-side operator input; initial HTML default remains `AUTO`.
7. Action feedback validation:
   - Confirm existing `dispatchAction()` success/error toasts remain intact after disable-state changes.

Acceptance checks:
- Zero-target view is compact and actionable.
- Remove is disabled at zero; Add is disabled at max.
- Dropdown choice no longer resets after blur.
- Toast feedback still appears for action success/failure.

## Phase 4 - Test Coverage Expansion

Files:
- `tests/test_hardening_regressions.py`

Add tests:
1. `test_add_layer_doge_source_success`
2. `test_add_layer_usd_source_success`
3. `test_add_layer_rejects_underfunded_doge`
4. `test_add_layer_rejects_underfunded_usd`
5. `test_add_layer_rejects_underfunded_auto`
6. `test_effective_layers_never_exceeds_target`
7. `test_alias_fallback_format_after_pool_exhaustion`
8. `test_gap_fields_non_negative_when_underfunded`
9. `test_drip_sizing_existing_orders_unchanged`
10. `test_layer_snapshot_round_trip`
11. `test_zero_slots_effective_layers_safe`
12. `test_add_layer_rejects_at_max_target_layers`
13. `test_layer_action_in_flight_guard_rejects_parallel_action`
14. `test_count_orders_at_funded_size_uses_order_price_override`
15. `test_dashboard_layer_source_not_forced_from_backend_default` (string/render regression for removed sync line)

Test execution:
1. `python3 -m unittest tests.test_hardening_regressions`
2. Run any focused subset while iterating on a phase, then rerun full file before merge.

Acceptance checks:
- All new tests pass.
- Existing hardening regressions remain green.

## Phase 5 - Spec Errata

Files:
- `docs/CAPITAL_LAYERS_SLOT_ALIAS_SPEC.md`

Changes:
1. Add an errata note in the formula section clarifying `DOGE_PER_ORDER` must be included in both DOGE and USD layer-cap terms.
2. Mark as backward-compatible clarification (implementation already follows generalized form).

Acceptance checks:
- Spec and code formulas are aligned.
- No runtime code change required for formula itself.

## Rollout Plan

1. Ship Phase 1 and Phase 2 together (runtime correctness + performance; low UX dependency).
2. Ship Phase 3 (dashboard) after runtime fields are available.
3. Ship Phase 4 tests in same PRs as code changes where possible; no test-only lag.
4. Ship Phase 5 doc errata as a documentation commit or with Phase 2.

## Rollback Plan

1. If caching causes sizing regression, revert `_loop_effective_layers` reads and return to direct recompute path.
2. If UI hardening causes operator friction, keep disable states but temporarily restore expanded telemetry view.
3. Keep max-target and in-flight guards unless they cause demonstrable false rejects; these are safety checks.

## Definition of Done

- All hardening items in `docs/CAPITAL_LAYERS_HARDENING_SPEC.md` Section 8 are implemented or explicitly resolved.
- Runtime performs effective-layer recompute once per loop in steady state.
- Propagation counter uses order-price-consistent sizing expectation.
- Dashboard behavior at `target_layers = 0`, `1`, and `>= max` matches the spec.
- Remove message formatting is semantically correct for non-1.0 DOGE-per-order settings.
- Full `tests.test_hardening_regressions` passes with new coverage included.
