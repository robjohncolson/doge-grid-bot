# HMM OHLCV Backfill Fix Spec v0.1

## Problem Statement

The 1m HMM data window is stuck at 1000/2000 (50%). The backfill mechanism
fetches 720 candles every attempt but adds zero new rows (`new=0`). The HMM
consensus confidence is pinned at 50% because the under-filled 1m timeframe
chronically disagrees with the 15m timeframe.

### Root Causes

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 1 | Kraken OHLC returns max 720 most-recent candles; no deep history available | Critical | Kraken API limitation |
| 2 | Backfill cursor built in Unix seconds; Kraken `since` expects nanosecond-scale opaque cursor | High | `bot.py:2965` |
| 3 | No stall detection: backfill retries identically on every startup, always gets `new=0` | Medium | `bot.py:3029` |

**Consequence:** The only path to 2000 1m candles is organic runtime sync at
1 candle/min = **33.3 hours** of continuous uptime. Backfill provides zero
acceleration.

## Locked Decisions

1. **No external data source.** We will not introduce a third-party OHLC
   provider or Kraken Trades-to-OHLC aggregation pipeline. The fix must work
   within the existing Kraken OHLC + Supabase architecture.
2. **No schema changes.** The `ohlcv_candles` Supabase table keeps its current
   `(pair, interval_min, time)` composite key.
3. **Backward compatible.** Existing env vars continue to work; new behavior
   is opt-in or strictly better defaults.
4. **Zero new dependencies.** Pure Python, nothing added.

## Scope

### In

1. Fix the nanosecond cursor mismatch in backfill.
2. Respect Kraken's 720-candle ceiling: lower default training target.
3. Add backfill stall detection and circuit-breaker.
4. Accelerate warm-up with a Supabase-side gap-fill strategy.
5. Dashboard hint improvements for backfill health.

### Out

1. Kraken Trades endpoint aggregation (deferred; high complexity, rate-limit cost).
2. Changes to HMM model parameters, training logic, or consensus blending.
3. Auto-retry of failed backfills from the main loop (startup + manual only).
4. Changes to the 15m secondary pipeline (benefits automatically from fixes 1-3).

---

## Fix 1: Cursor Unit Correction

### Current (broken)

`bot.py:2965`:
```python
cursor = max(0, int(oldest_ts - (missing + 24) * interval_sec))
# oldest_ts ~ 1.74e9 (seconds)  →  Kraken treats as ~1.74 nanoseconds (epoch)
```

Kraken receives a near-zero `since` value, ignores it, and returns the most
recent 720 candles — all of which already exist in Supabase.

### Fix

**Do not fabricate cursors from timestamps.** The backfill should call
`get_ohlc_page` **without** `since` on the first page (to get the most recent
720), and use Kraken's returned `last` cursor for subsequent pages.

But this alone does not solve the 720-ceiling problem (Fix 2 addresses that).
The cursor fix is still required so that the pagination loop terminates
correctly and the `new_unique` accounting is accurate.

#### Changes — `bot.py:backfill_ohlcv_history`

Replace lines 2962-2967:

```python
# --- BEFORE ---
missing = max(0, target - existing_count)
oldest_ts = min(existing_ts) if existing_ts else 0.0
if oldest_ts > 0:
    cursor = max(0, int(oldest_ts - (missing + 24) * interval_sec))
else:
    cursor = 0
```

With:

```python
# --- AFTER ---
# Do not fabricate a cursor from timestamps — Kraken's OHLC `since` is an
# opaque nanosecond-scale cursor, not a Unix-seconds timestamp.  Calling
# without `since` returns the most recent 720 candles; Kraken's `last`
# cursor is used for any subsequent pages.
cursor = 0
```

No other changes needed in the pagination loop (lines 2970-3000): `cursor=0`
causes `since=None` on the first call (line 2975: `since=cursor if cursor > 0
else None`), and `last_cursor` from Kraken drives subsequent pages correctly.

---

## Fix 2: Training Target Aligned to API Ceiling

### Problem

`HMM_TRAINING_CANDLES` defaults to 2000, but Kraken can only serve 720 candles
per OHLC request and **does not provide deep history** beyond the most recent
720 entries (regardless of `since` value). The 2000 target is unreachable via
backfill and requires ~33h of organic sync.

### Fix

Lower the default `HMM_TRAINING_CANDLES` from 2000 to **720**.

The HMM already trains successfully with `HMM_MIN_TRAIN_SAMPLES=500`.
720 candles at 1m = 12 hours of price action — sufficient for a 3-state
Gaussian HMM to detect regime structure.

#### Changes — `config.py`

```python
# --- BEFORE ---
HMM_TRAINING_CANDLES: int = _env("HMM_TRAINING_CANDLES", 2000, int)

# --- AFTER ---
HMM_TRAINING_CANDLES: int = _env("HMM_TRAINING_CANDLES", 720, int)
```

Operators who already set `HMM_TRAINING_CANDLES=2000` in env will keep their
override. The change only affects the default.

#### Secondary pipeline

`HMM_SECONDARY_TRAINING_CANDLES` defaults to 1000. At 15m intervals, 720
candles = 7.5 days — Kraken can serve that in one page. However, the same
720-ceiling applies. Lower the default to **720** as well:

```python
# --- BEFORE ---
HMM_SECONDARY_TRAINING_CANDLES: int = _env("HMM_SECONDARY_TRAINING_CANDLES", 1000, int)

# --- AFTER ---
HMM_SECONDARY_TRAINING_CANDLES: int = _env("HMM_SECONDARY_TRAINING_CANDLES", 720, int)
```

#### Impact on organic warm-up

| Scenario | Old (2000) | New (720) |
|----------|-----------|-----------|
| 1m warm-up from cold start | ~33.3h | **Instant** (single backfill) |
| 1m warm-up from 500 existing | ~25h organic | **Instant** (single backfill) |
| 15m warm-up from cold start | ~10.4d | **Instant** (single backfill) |

---

## Fix 3: Backfill Stall Detection & Circuit-Breaker

### Problem

The startup backfill runs every restart, always fetches 720 duplicate rows,
always logs `new=0`, and wastes a Kraken API call. There is no logic to
recognize the stall and skip future attempts.

### Fix

Add a **backfill stall counter** and **circuit-breaker** that stops retrying
after N consecutive zero-new-data attempts.

#### New state fields — `GridState.__init__`

```python
self._hmm_backfill_stall_count: int = 0
self._hmm_backfill_stall_count_secondary: int = 0
```

#### Persist in save/load snapshot

Add to status snapshot dict and restore on load (same pattern as existing
`_hmm_backfill_last_*` fields).

#### New config constant — `config.py`

```python
# Max consecutive backfill attempts with new=0 before circuit-breaker trips.
HMM_BACKFILL_MAX_STALLS: int = _env("HMM_BACKFILL_MAX_STALLS", 3, int)
```

#### Logic — `bot.py:backfill_ohlcv_history`

**At entry** (after existing_count check, before fetch loop):

```python
stall_limit = max(1, int(getattr(config, "HMM_BACKFILL_MAX_STALLS", 3)))
stall_count = (
    self._hmm_backfill_stall_count_secondary
    if state_key == "secondary"
    else self._hmm_backfill_stall_count
)
if stall_count >= stall_limit:
    msg = f"backfill_circuit_open:stalls={stall_count}/{stall_limit}"
    if state_key == "secondary":
        self._hmm_backfill_last_message_secondary = msg
    else:
        self._hmm_backfill_last_message = msg
    return False, f"Backfill circuit-breaker open ({stall_count} consecutive stalls)"
```

**After new_unique is computed** (line ~3012):

```python
if new_unique == 0:
    if state_key == "secondary":
        self._hmm_backfill_stall_count_secondary += 1
    else:
        self._hmm_backfill_stall_count += 1
else:
    # Reset on any progress
    if state_key == "secondary":
        self._hmm_backfill_stall_count_secondary = 0
    else:
        self._hmm_backfill_stall_count = 0
```

#### Circuit-breaker reset

The circuit-breaker resets when:

1. **New candles arrive via runtime sync** — the readiness cache invalidation
   already signals fresh data; no extra logic needed since backfill only runs
   on startup.
2. **Manual Telegram `/backfill_ohlcv`** — reset stall counter before calling
   `backfill_ohlcv_history` so operators can force-retry.
3. **Bot restart** — stall counter persists in snapshot, so the breaker
   survives restarts. This is intentional: if the breaker tripped before
   shutdown, it stays tripped. Manual `/backfill_ohlcv` is the explicit reset.

---

## Fix 4: Supabase Gap-Fill on Startup

### Problem

After a restart, the runtime sync cursor (`_ohlcv_since_cursor`) reloads from
the snapshot. If the bot was down for hours, there is a gap between the last
stored candle and now. The sync cursor correctly picks up from where it left
off, fetching at most 720 candles forward — which usually covers the gap.

But if the gap exceeds 720 minutes (12 hours at 1m), candles in the middle
are permanently lost. The existing backfill (even with Fix 1) cannot retrieve
them because Kraken only serves the most recent 720.

### Fix

**No code change needed.** With Fix 2 (target=720), the training window only
needs the most recent 12 hours. Any gap older than that falls outside the
training window and is irrelevant. A restart after >12h downtime simply
rebuilds the window from the single backfill call (720 fresh candles).

This is a "fix by design" — lowering the target to match the API ceiling
eliminates the gap-fill problem entirely.

---

## Fix 5: Dashboard Hint Improvements

### Problem

The current hints show `backfill:queued=720 new=0 est_total=1000/2000` but do
not surface the stall count or circuit-breaker state. The operator has no
visibility into why the window is not filling.

### Fix

#### Backfill message enrichment — `bot.py:backfill_ohlcv_history`

Include the stall count in the backfill message string:

```python
# --- BEFORE ---
backfill_msg = f"queued={queued_rows} new={new_unique} est_total={est_total}/{target}"

# --- AFTER ---
current_stalls = (
    self._hmm_backfill_stall_count_secondary
    if state_key == "secondary"
    else self._hmm_backfill_stall_count
)
backfill_msg = (
    f"queued={queued_rows} new={new_unique} est_total={est_total}/{target}"
    + (f" stalls={current_stalls}" if current_stalls > 0 else "")
)
```

#### Dashboard rendering — `dashboard.py`

No structural changes. The existing hint rendering already displays
`backfill:{message}` verbatim. The stall count will appear naturally:

```
backfill:queued=720 new=720 est_total=720/720
```

Or, if stalled (legacy env override with target>720):

```
backfill:queued=720 new=0 est_total=500/2000 stalls=3
```

Or, if circuit-breaker is open:

```
backfill:backfill_circuit_open:stalls=3/3
```

---

## Summary of Changes

| File | Change | Lines Affected |
|------|--------|----------------|
| `config.py` | `HMM_TRAINING_CANDLES` default 2000 → 720 | ~476 |
| `config.py` | `HMM_SECONDARY_TRAINING_CANDLES` default 1000 → 720 | ~498 |
| `config.py` | Add `HMM_BACKFILL_MAX_STALLS` (default 3) | new line |
| `bot.py` | Remove fabricated cursor; use `cursor=0` | ~2962-2967 |
| `bot.py` | Add stall counter fields to `__init__` | ~322 |
| `bot.py` | Add stall counter to save/load snapshot | ~1105, ~1243 |
| `bot.py` | Circuit-breaker check at backfill entry | ~2968 (new block) |
| `bot.py` | Stall counter increment/reset after backfill | ~3012 (new block) |
| `bot.py` | Enrich backfill message with stall count | ~3021 |
| `bot.py` | Reset stall counter in Telegram `/backfill_ohlcv` handler | ~5676 |

**Total: ~40 lines changed/added across 2 files. No new files.**

---

## Test Plan

### Unit

1. **Cursor fix**: Verify `backfill_ohlcv_history` calls `get_ohlc_page` with
   `since=None` on the first page (mock Kraken response with 720 rows and a
   nanosecond `last` cursor).
2. **Stall counter**: Call `backfill_ohlcv_history` with a mock that returns
   720 all-duplicate rows. Verify stall counter increments. Call again past
   limit — verify circuit-breaker returns early without API call.
3. **Stall reset**: After a stall, call with a mock returning 1+ new rows.
   Verify counter resets to 0.
4. **Config defaults**: Verify `HMM_TRAINING_CANDLES` defaults to 720,
   `HMM_SECONDARY_TRAINING_CANDLES` defaults to 720.
5. **Backward compat**: Set `HMM_TRAINING_CANDLES=2000` in env. Verify the
   override is respected and backfill targets 2000.

### Integration (dry-run)

1. Cold start with empty Supabase OHLCV table. Verify:
   - Startup backfill fetches 720 candles, `new=720`, `est_total=720/720`.
   - `ready_for_target_window` = True immediately.
   - HMM trains on first `_update_hmm` call.
2. Restart with existing 720+ candles. Verify:
   - Startup backfill skips (`already_ready`).
   - No wasted API call.
3. Restart with 500 existing candles. Verify:
   - Backfill fetches 720, ~220 are new, rest are duplicates.
   - `est_total >= 720` → ready.

### Production validation

After deploy, check dashboard:

1. `Data Window (1m)` should show `N/720` (not `/2000`).
2. After one backfill cycle: `720/720 (100%)`.
3. Confidence should no longer be pinned at 50% (will vary with regime).
4. Hint should show `backfill:queued=720 new=N est_total=720/720`.

---

## Rollback

Set `HMM_TRAINING_CANDLES=2000` in env to restore old target. The cursor fix
and stall detection have no env-gated rollback but are strictly
non-destructive (cursor=0 simply means "fetch most recent" which is what
Kraken was doing anyway due to the bug).

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| 720 candles insufficient for HMM accuracy | Low | Medium | Already trains with 500 (MIN_TRAIN_SAMPLES); 720 > 500. Monitor regime stability post-deploy. |
| Operator has `HMM_TRAINING_CANDLES=2000` hardcoded | Low | None | Backfill still fetches 720; organic sync fills the rest. Circuit-breaker prevents wasted API calls. |
| Stall counter persists across restarts | Intended | None | Manual `/backfill_ohlcv` resets it. |
