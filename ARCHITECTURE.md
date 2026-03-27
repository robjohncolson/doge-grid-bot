# DOGE State-Machine Bot Architecture (Polyglot Runtime)

Last updated: 2026-02-18
Primary references: `bot.py`, `state_machine.py`, `doge_core.py`, `doge-core/app/Main.hs`, `doge-core/src/DogeCore/*.hs`, `doge-hmm/src/*.rs`, `tests/test_cross_language.py`

## 1. System Overview

The runtime is now polyglot and split into three layers:

1. Control/runtime layer in Python (`bot.py` + runtime overlays)
2. Reducer contract layer (`state_machine.py` contract, optional Haskell backend via `doge_core.py`)
3. Regime/HMM layer (Python detector module plus Rust PyO3 backend artifacts)

`bot.py` remains the orchestrator for exchange I/O, persistence, dashboard/Telegram control, and all policy overlays.

## 2. Reducer Architecture

### 2.1 Contract surface

Canonical reducer contract:

- `transition(state, event, cfg, order_size_usd, order_sizes=None) -> (state, actions)`
- `check_invariants(state) -> list[str]`
- patch helpers used by runtime:
  - `apply_order_txid(...)`
  - `apply_order_regime_at_entry(...)`

Core state/action schema is implemented in `state_machine.py` and mirrored in Haskell types.

### 2.2 Python reducer

`state_machine.py` is still the authoritative contract definition and includes:

- Pair phases (`S0`, `S1a`, `S1b`, `S2`)
- regime vintage fields (`regime_at_entry`) on order/recovery/cycle records
- settlement split fields (`entry_fee`, `exit_fee`, `quote_fee`, `settled_usd`)
- cumulative settlement tracker (`total_settled_usd`)
- backward-compat default: missing `total_settled_usd` falls back to `total_profit` in `from_dict`

### 2.3 Haskell reducer backend

Haskell implementation lives under `doge-core/`:

- `doge-core/src/DogeCore/Types.hs`
- `doge-core/src/DogeCore/Transition.hs`
- `doge-core/src/DogeCore/Invariants.hs`
- RPC/entrypoint: `doge-core/app/Main.hs`

JSON RPC methods exposed by the executable:

1. `transition`
2. `check_invariants`
3. `apply_order_regime_at_entry`

The RPC schema accepts one-shot stdin/stdout calls and a line-delimited `--server` mode.

### 2.4 Adapter and shadow mode

`doge_core.py` is the compatibility adapter between Python callers and the selected reducer backend.

Backend controls:

- `DOGE_CORE_BACKEND`: `python` | `haskell` | `auto`
- `DOGE_CORE_EXE`: path to reducer executable
- `DOGE_CORE_PERSISTENT`: enable persistent `--server` transport
- `DOGE_CORE_TIMEOUT_SEC`: subprocess timeout
- `DOGE_CORE_SHADOW`: run Python + Haskell and compare

Shadow mode compares full state/actions and focused parity fields (`regime_at_entry`, settlement fields, `total_settled_usd`) and records divergence metrics.

Important current boot contract:

- `bot.py` imports `state_machine` directly.
- `doge_core.py` is used for backend switching/parity testing and can be adopted by runtime wiring as needed.

## 3. HMM / Regime Architecture

### 3.1 Runtime role

Regime logic is advisory-only. It influences runtime policy knobs but does not modify reducer transition semantics.

`bot.py` runs:

- primary HMM stream (base interval)
- optional secondary stream (15m-style)
- optional tertiary stream (1h-style)
- consensus and confidence gating over those streams

### 3.2 Python runtime integration

`bot.py` currently initializes HMM detectors from `hmm_regime_detector` and manages:

- periodic OHLCV sync/backfill for primary/secondary/tertiary intervals
- train/retrain cadence per stream
- consensus blending and regime agreement handling
- tertiary transition tracking with confirmation window (`ACCUM_CONFIRMATION_CANDLES`)
- training-depth-derived confidence modifiers exposed in status

### 3.3 Rust backend artifacts

Rust/PyO3 HMM implementation is in `doge-hmm/` and exports:

- `RegimeDetector`, `RegimeState`, `TertiaryTransition`
- `serialize_for_snapshot(...)`, `restore_from_snapshot(...)`
- `confidence_modifier_for_source(...)`
- grid/target helpers (`compute_grid_bias`, `compute_blended_idle_target`)

This backend is parity-tested and available for staged runtime adoption.

## 4. Persistence and Observability

Snapshot state (`bot_state` + local fallback) includes:

- slot reducer states (`PairState` per slot)
- HMM primary/secondary/tertiary state and consensus
- tertiary transition metadata
- training depth summaries and confidence modifiers
- runtime overlays (Bayesian, survival, throughput, self-healing, ledger state)

Dashboard/API status publishes reducer, risk, HMM, and pipeline diagnostics including:

- `hmm_regime`
- `hmm_secondary`
- `hmm_tertiary`
- `hmm_data_pipeline*` readiness/freshness snapshots

## 5. Parity and Validation Harness

Cross-language parity is enforced by fixtures and adapter tests:

- `tests/test_cross_language.py`
- `tests/test_doge_core_adapter.py`
- fixture corpus in `tests/fixtures/cross_language/`

Coverage includes:

- regime propagation paths
- settlement math (A/B side)
- backward-compat deserialization defaults
- shadow divergence accounting

Current expanded fixture corpus: 20 scenarios.

## 6. Build Targets

Reducer (Haskell):

- `stack build` in `doge-core/`

HMM backend (Rust):

- `cargo test` in `doge-hmm/`
- PyO3 module via `maturin develop --release` (when needed)

Python runtime/parity tests:

- `python3 -m unittest tests.test_cross_language tests.test_doge_core_adapter`

