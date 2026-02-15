# Kelly Criterion — Implementation Plan

Last updated: 2026-02-15
Parent spec: `KELLY_SPEC.md` v1.1 (rev 2)
Status: **Approved for implementation**

## Pre-Implementation Decision

**`regime_at_entry` canonical format: numeric ID (0/1/2).**

All new writes use `int`. Legacy text data in Supabase (`"BULLISH"`, `"RANGING"`, `"BEARISH"`) is handled by a normalizer during backfill and `from_dict()` deserialization:

```python
_REGIME_TEXT_TO_ID = {"BULLISH": 2, "RANGING": 1, "BEARISH": 0}
```

Legacy `exit_outcomes` rows where `regime_at_entry` contains outcome-time text should be treated as `NULL` (not normalized to int), because the original values were mislabeled — they captured regime at *exit-book time*, not entry time. Using them as entry-time proxies introduces systematic bias for long-lived cycles. Nulled rows land in the `"unknown"` aggregate bucket, which is the honest representation.

## Current Gaps

| # | Gap | Location | Severity |
|---|-----|----------|----------|
| 1 | Kelly not wired into runtime sizing | `bot.py:641`, `bot.py:2286`, `bot.py:6030` | Blocking |
| 2 | `regime_at_entry` written as outcome-time regime, not entry-time | `bot.py:4093` (comment on :4092 confirms: "Phase 0: best-effort regime snapshot at outcome time") | Data integrity bug |
| 3 | State model does not carry `regime_at_entry` through order/recovery/cycle lifecycle | `state_machine.py:88` (CycleRecord), `:985` (exit OrderState creation), `:1044` (recovery cancel) | Structural blocker |
| 4 | `CompletedCycle` in `grid_strategy.py` also lacks `regime_at_entry` field | `grid_strategy.py:50-112` | Structural blocker |
| 5 | Kelly env config/toggle missing | `config.py:482` onward (HMM/regime config but no `KELLY_*`) | Blocking |
| 6 | Dashboard has no Kelly block | `dashboard.py` status UI/JS sections | Cosmetic |
| 7 | `kelly_sizer.py` has spec drift on negative-edge clamp | `kelly_sizer.py:415-418` (`negative_edge_mult` bypasses floor/ceiling clamp) | Bug |

## Implementation Steps

### Step 1: Lock contract and compatibility rules

**Files**: `KELLY_SPEC.md`, `STATE_MACHINE.md`

- Confirm `regime_at_entry` canonical format as `int | None` (0=bearish, 1=ranging, 2=bullish, None=unknown)
- Define backward-compat normalization: `from_dict()` on all dataclasses accepts `None`, `int`, or legacy text (via `_REGIME_TEXT_TO_ID` lookup, unknown strings map to `None`)
- Add cross-reference section in `STATE_MACHINE.md` pointing to `KELLY_SPEC.md`

**Output**: Updated spec files. No runtime changes.

---

### Step 2: Database/schema readiness

**Files**: `docs/supabase_v1_schema.sql`, migration SQL

**Must complete before any code deploy.** The existing Supabase integration auto-detects columns and silently strips unknown ones on write — if `regime_at_entry` column doesn't exist with the right type, int values will be lost without error.

Actions:
1. Verify `exit_outcomes.regime_at_entry` column exists (it does — but currently holds text values)
2. Alter column type to accept both text and int, or migrate to int-only with NULL backfill:
   ```sql
   -- Null out mislabeled outcome-time values
   UPDATE exit_outcomes SET regime_at_entry = NULL
     WHERE regime_at_entry IS NOT NULL;
   -- If column type needs changing:
   ALTER TABLE exit_outcomes ALTER COLUMN regime_at_entry TYPE INTEGER USING NULL;
   ```
3. Document deployment order: schema migration runs first, then code deploy

**Output**: Migration applied. Column ready for int writes.

---

### Step 3: Add regime tag propagation in state machine models

**Files**: `state_machine.py` (pair_model.py), `grid_strategy.py`

This step is **purely structural** — add fields and copy-forward logic. The reducer never *computes* `regime_at_entry`; it treats it as opaque cargo that `bot.py` stamps and the reducer preserves through transitions.

#### 3a: `state_machine.py` dataclasses

Add `regime_at_entry: int | None = None` to:

| Dataclass | Line | Purpose |
|-----------|------|---------|
| `OrderState` | ~:60 | Carries tag from entry fill through to exit order |
| `RecoveryOrder` | ~:78 | Preserves tag when exit is orphaned to recovery |
| `CycleRecord` | :88 | Final resting place; consumed by Kelly partitioner |

Propagation points in the reducer:
- **Entry fill → exit order creation** (~:985): copy `order.regime_at_entry` to new exit `OrderState`
- **Exit orphan → recovery** (~:1020s): copy from orphaned exit order to `RecoveryOrder`
- **Recovery fill → cycle record**: copy from recovery to `CycleRecord`
- **Normal exit fill → cycle record**: copy from exit order to `CycleRecord`

All `from_dict()` methods: use `.get("regime_at_entry")` with `None` default. Apply `_REGIME_TEXT_TO_ID` normalization for legacy text values.

#### 3b: `grid_strategy.py` CompletedCycle

Add `regime_at_entry: int | None = None` to `CompletedCycle`:

```python
# In __init__ (add as last parameter):
def __init__(self, ..., regime_at_entry: int | None = None):
    ...
    self.regime_at_entry = regime_at_entry

# In to_dict():
d["regime_at_entry"] = self.regime_at_entry

# In from_dict():
regime_at_entry=d.get("regime_at_entry")
```

Backward-compatible: existing serialized cycles without the field deserialize with `None`.

**Output**: All dataclasses carry and propagate `regime_at_entry`. No runtime behavior change.

**Dependency**: Step 1 (contract locked). Blocks steps 4, 5.

---

### Step 4: Stamp regime at entry in runtime

**Files**: `bot.py`

Stamp `regime_at_entry` at entry-fill time, **regardless of `KELLY_ENABLED`**, so data accumulates for future activation.

#### 4a: Entry fill handler

When a `FillEvent` for an entry triggers exit order creation:

```python
# After reducer returns actions for an entry fill:
new_exit_order.regime_at_entry = self._current_regime_id()  # int or None
```

Where `_current_regime_id()` returns the HMM regime ID (0/1/2) if HMM is enabled, or `None` if HMM is disabled.

#### 4b: Fix mislabeled Supabase write

At `bot.py:4093`, the exit outcome row currently calls `_policy_hmm_signal()` to get outcome-time regime. Change to read the persisted `regime_at_entry` from the cycle record:

```python
# Before (outcome-time, mislabeled):
"regime_at_entry": regime_name,

# After (entry-time, from cycle record):
"regime_at_entry": cycle_record.regime_at_entry,
```

The remaining outcome-time fields (`regime_confidence`, `regime_bias_signal`, `against_trend`, `regime_tier`) stay as-is — they describe the market state at exit-book time, which is still useful context.

**Output**: All new cycles carry true entry-time regime tag. Supabase writes are correctly labeled.

**Dependency**: Step 3 (fields exist on dataclasses). Blocks step 6.

---

### Step 5: Bring kelly_sizer.py to spec parity

**Files**: `kelly_sizer.py`

#### 5a: Fix negative-edge clamp (spec §6.3)

Current code at line 415-418:
```python
if result.reason == "no_edge":
    adjusted = base_order_usd * cfg.negative_edge_mult
    return max(adjusted, 0.0), f"kelly_no_edge({source})"
```

Change to route through floor/ceiling clamp:
```python
if result.reason == "no_edge":
    mult = max(cfg.kelly_floor_mult, min(cfg.negative_edge_mult, cfg.kelly_ceiling_mult))
    adjusted = base_order_usd * mult
    return adjusted, f"kelly_no_edge({source},m={mult:.3f})"
```

#### 5b: Add legacy regime tag normalizer

Add normalization in `partition_cycles_by_regime()` for mixed historical data:

```python
_REGIME_TEXT_TO_ID = {"BULLISH": 2, "RANGING": 1, "BEARISH": 0}

def _normalize_regime_id(raw) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        return _REGIME_TEXT_TO_ID.get(raw.upper())
    return None
```

Apply in the partitioner before bucket assignment.

#### 5c: Verify status reasons/fields match spec §11.1

Ensure `status_payload()` output structure matches the spec's JSON example. Confirm `reason` field values: `"ok"`, `"no_data"`, `"no_edge"`, `"all_wins"`, `"insufficient_samples"`.

**Output**: `kelly_sizer.py` matches `KELLY_SPEC.md` v1.1.

**Dependency**: Step 1 (contract locked). Blocks step 6.

---

### Step 6: Integrate Kelly into runtime with master toggle

**Files**: `config.py`, `bot.py`

#### 6a: Add config parameters

In `config.py`, after the HMM config block (~line 482+):

```python
# Kelly criterion position sizer (advisory sizing layer; see KELLY_SPEC.md).
KELLY_ENABLED: bool = _env("KELLY_ENABLED", False, bool)
KELLY_FRACTION: float = _env("KELLY_FRACTION", 0.25, float)
KELLY_MIN_SAMPLES: int = _env("KELLY_MIN_SAMPLES", 30, int)
KELLY_MIN_REGIME_SAMPLES: int = _env("KELLY_MIN_REGIME_SAMPLES", 15, int)
KELLY_LOOKBACK: int = _env("KELLY_LOOKBACK", 500, int)
KELLY_FLOOR_MULT: float = _env("KELLY_FLOOR_MULT", 0.5, float)
KELLY_CEILING_MULT: float = _env("KELLY_CEILING_MULT", 2.0, float)
KELLY_NEGATIVE_EDGE_MULT: float = _env("KELLY_NEGATIVE_EDGE_MULT", 0.5, float)
KELLY_RECENCY_WEIGHTING: bool = _env("KELLY_RECENCY_WEIGHTING", True, bool)
KELLY_RECENCY_HALFLIFE: int = _env("KELLY_RECENCY_HALFLIFE", 100, int)
KELLY_LOG_UPDATES: bool = _env("KELLY_LOG_UPDATES", True, bool)
```

#### 6b: Instantiate in bot.py constructor

```python
if config.KELLY_ENABLED:
    from kelly_sizer import KellySizer, KellyConfig
    kelly_cfg = KellyConfig(
        kelly_fraction=config.KELLY_FRACTION,
        min_samples_total=config.KELLY_MIN_SAMPLES,
        # ... all config fields
    )
    self._kelly = KellySizer(kelly_cfg)
else:
    self._kelly = None
```

#### 6c: Update on regime eval cadence

In `_update_regime_tier()` (~line 2286), after regime eval completes:

```python
if self._kelly is not None:
    all_cycles = []
    for sid, slot in self.slots.items():
        for c in slot.state.completed_cycles:
            all_cycles.append({
                "net_profit": c.net_profit,
                "regime_at_entry": c.regime_at_entry,
                "exit_time": c.exit_time,
            })
    regime_label = self._kelly.cfg.regime_labels.get(
        self._regime_tier_state.get("regime_id"), "ranging"
    )
    self._kelly.update(all_cycles, regime_label=regime_label)
```

#### 6d: Apply in sizing pipeline

In `_slot_order_size_usd()` (~line 641), after `base_with_layers` computation and before rebalancer skew:

```python
if self._kelly is not None:
    kelly_usd, kelly_reason = self._kelly.size_for_slot(base_with_layers)
    base_with_layers = kelly_usd
    # kelly_reason available for debug logging
```

Pipeline order: `base + layers → Kelly multiplier → rebalancer skew → fund guard → final size`

**Output**: Kelly sizing active when `KELLY_ENABLED=True`. Inactive by default.

**Dependency**: Steps 4, 5.

---

### Step 7: Add Kelly persistence and API telemetry

**Files**: `bot.py`

#### 7a: Snapshot save/restore

In `_save_snapshot()` / `_save_local_runtime_snapshot()`:

```python
if self._kelly is not None:
    snapshot["kelly_state"] = self._kelly.snapshot_state()
```

In `_load_snapshot()`:

```python
if self._kelly is not None:
    self._kelly.restore_state(snapshot.get("kelly_state", {}))
```

#### 7b: Status payload

In the status payload builder (~line 6030):

```python
if self._kelly is not None:
    payload["kelly"] = self._kelly.status_payload()
else:
    payload["kelly"] = {"enabled": False}
```

**Output**: Kelly state survives restarts. Dashboard API includes Kelly telemetry.

**Dependency**: Step 6.

---

### Step 8: Add dashboard visibility

**Files**: `dashboard.py`

Render Kelly status block in the dashboard UI. Display:
- Enabled/disabled badge
- Active regime label
- Per-regime table: win rate, payoff ratio, edge, f*, multiplier, sample count
- Aggregate row
- Fallback reason and insufficient-data states
- `"kelly_inactive"` / `"no_edge"` / `"insufficient_samples"` shown as muted status text

Low risk, purely additive to existing dashboard HTML/JS.

**Output**: Kelly telemetry visible in browser.

**Dependency**: Step 7.

---

### Step 9: Testing and rollout gating

**Files**: `tests/test_kelly_sizer.py` (new), `tests/test_hardening_regressions.py`

#### 9a: Unit tests for `kelly_sizer.py`

- `compute_kelly_fraction()`: verify math for known inputs, edge cases (no data, all wins, no edge)
- Negative-edge clamp: verify floor/ceiling is applied (the bug that was fixed in step 5)
- `partition_cycles_by_regime()`: verify bucketing with int IDs, text legacy, None, unknown values
- Recency weighting: verify decay math and win-rate computation
- Sample gating: verify inactive below `min_samples_total`, fallback below `min_samples_per_regime`
- `KellySizer.size_for_slot()`: verify regime resolution fallback cascade

#### 9b: Integration tests

- Regime tagging: entry fill → exit order → cycle record carries `regime_at_entry`
- Sizing path: `_slot_order_size_usd()` returns Kelly-adjusted value when enabled, unchanged when disabled
- Snapshot round-trip: `snapshot_state()` → `restore_state()` preserves active regime and update count
- Status payload: verify JSON structure matches spec §11.1
- Legacy data: verify old text-valued cycles handled gracefully

#### 9c: Rollout sequence

1. Deploy with `KELLY_ENABLED=False` (default)
2. Verify `regime_at_entry` tagging is working in Supabase (step 4 runs regardless of toggle)
3. Verify `kelly` block appears in `/api/status` as `{"enabled": false}`
4. Set `KELLY_ENABLED=True` on staging/test
5. Monitor Kelly telemetry for reasonable values (spec §11.2 healthy ranges)
6. Enable in production

**Output**: Confidence that Kelly sizing is correct before it touches real money.

---

## Dependency Graph

```
Step 1 (lock contract)
  ├── Step 2 (schema migration)  ← must complete before any code deploy
  ├── Step 3 (dataclass fields)
  │     └── Step 4 (runtime stamping)
  │           └── Step 6 (runtime integration)
  │                 └── Step 7 (persistence + API)
  │                       └── Step 8 (dashboard)
  └── Step 5 (kelly_sizer.py fixes)
        └── Step 6

Step 9 (testing) runs in parallel with steps 3–8
```

## Files Modified Summary

| File | Steps | Nature |
|------|-------|--------|
| `KELLY_SPEC.md` | 1 | Contract (already at v1.1) |
| `STATE_MACHINE.md` | 1 | Cross-reference addition |
| `docs/supabase_v1_schema.sql` | 2 | Schema migration |
| `state_machine.py` (pair_model.py) | 3 | Add `regime_at_entry` to 3 dataclasses + propagation |
| `grid_strategy.py` | 3 | Add `regime_at_entry` to `CompletedCycle` |
| `kelly_sizer.py` | 5 | Fix clamp bug, add normalizer |
| `config.py` | 6 | Add `KELLY_ENABLED` + `KELLY_*` env vars |
| `bot.py` | 4, 6, 7 | Stamp regime, instantiate Kelly, sizing pipeline, persistence, telemetry |
| `dashboard.py` | 8 | Kelly status UI block |
| `tests/test_kelly_sizer.py` | 9 | New test file |
| `tests/test_hardening_regressions.py` | 9 | Additional integration tests |
