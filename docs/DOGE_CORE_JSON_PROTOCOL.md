# DOGE Core JSON Protocol

## 1. Purpose

This document defines the subprocess JSON contract between Python (`doge_core.py`) and the future Haskell executable (`doge-core-exe`).

The protocol covers only the two Haskell-routed methods:

1. `transition`
2. `check_invariants`

Everything else in the `state_machine` surface remains Python-owned and is re-exported by `doge_core.py`.

## 2. Transport

1. Python launches `doge-core-exe` as a subprocess.
2. Python sends one JSON request via stdin.
3. Haskell returns one JSON response via stdout.
4. Any non-zero process exit code is treated as an error and Python falls back to `state_machine`.

## 3. Method: `transition`

### 3.1 Request Shape

```json
{
  "method": "transition",
  "state": { "...": "PairState as state_machine.to_dict output" },
  "event": { "...": "Event dataclass fields" },
  "config": { "...": "EngineConfig dataclass fields" },
  "order_size_usd": 2.0,
  "order_sizes": {
    "A": 2.0,
    "B": 2.0
  }
}
```

Notes:

1. `state` uses the same schema as `state_machine.to_dict(state)`.
2. `event` uses raw event fields; event variant is inferred by field set:
   1. `{"price","timestamp"}` -> `PriceTick`
   2. `{"timestamp"}` -> `TimerTick`
   3. `{"order_local_id","txid","side","price","volume","fee","timestamp"}` -> `FillEvent`
   4. `{"recovery_id","txid","side","price","volume","fee","timestamp"}` -> `RecoveryFillEvent`
   5. `{"recovery_id","txid","timestamp"}` -> `RecoveryCancelEvent`
3. `config` uses `EngineConfig` field names.
4. `order_sizes` may be `null`.

### 3.2 Response Shape

```json
{
  "state": { "...": "PairState dict for state_machine.from_dict" },
  "actions": [
    { "...": "Action object #1" },
    { "...": "Action object #2" }
  ]
}
```

Action objects are identified by field patterns:

1. `PlaceOrderAction`:
   `local_id, side, role, price, volume, trade_id, cycle` (+ optional `post_only`, `reason`)
2. `CancelOrderAction`:
   `local_id, txid` (+ optional `reason`)
3. `OrphanOrderAction`:
   `local_id, recovery_id` (+ optional `reason`)
4. `BookCycleAction`:
   `trade_id, cycle, net_profit, gross_profit, fees` (+ optional `from_recovery`)

`OrphanOrderAction` constraint:

1. `OrphanOrderAction` payloads MUST include `local_id` and `recovery_id`.
2. They MUST NOT include `trade_id`.
3. They SHOULD NOT include `side`, `role`, `price`, `volume`, or `cycle`.
4. Recommended shape:

```json
{
  "local_id": 1,
  "recovery_id": 7,
  "reason": "s1_timeout"
}
```

Rationale: Python action reconstruction in `doge_core.py` discriminates action types by field patterns, and `trade_id` presence affects classification.

## 4. Method: `check_invariants`

### 4.1 Request Shape

```json
{
  "method": "check_invariants",
  "state": { "...": "PairState as state_machine.to_dict output" }
}
```

### 4.2 Response Shape

```json
{
  "violations": [
    "S0 must be exactly A sell entry + B buy entry"
  ]
}
```

`violations` must always be a JSON list (empty list when valid).

## 5. Error Semantics

1. If the subprocess exits non-zero, Python treats the call as failed.
2. If stdout is empty or invalid JSON, Python treats the call as failed.
3. On failure, `doge_core.py` logs a warning and routes the same call through Python `state_machine`.

## 6. Compatibility Guarantees

1. `state` payloads must round-trip through `state_machine.from_dict`.
2. `actions` payloads must reconstruct Python dataclass action instances used by `isinstance(...)` checks in `bot.py`.
3. Field names and defaults must stay aligned with `state_machine.py`.
