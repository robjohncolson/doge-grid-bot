# Polyglot Refactor Spec — Haskell + Rust + Python (Implementation Tracker)

**Version**: 0.4.2
**Status**: In progress — Haskell complete, Phase 3 shadow observability wired, Rust HMM core implementation underway
**Branch**: `polyglot-refactor`
**Date**: 2026-02-16

## 1. Summary

This is the implementation tracker for the staged polyglot refactor.
Primary goals are:

1. Preserve current production behavior.
2. Improve safety around state transitions.
3. Improve compute performance where it matters.

Migration remains incremental with Python fallbacks where appropriate.

### 1.1 Current Snapshot (2026-02-16)

Completed on this branch:

1. `doge_core.py` adapter exists and is wired into `bot.py` and `backtest_v1.py`.
2. Haskell state-machine implementation in `doge-core/` is fully native for all 5 events.
3. Haskell binary transition path is strict/native (no Python delegation in `doge-core-exe`).
4. Persistent backend mode is implemented (`doge-core-exe --server` + Python persistent client), with one-shot fallback retained.
5. Cross-language parity harness exists with 13 fixtures and passes.
6. CI exists at `.github/workflows/ci.yml` with Haskell build + Python test/parity jobs.
7. Rust HMM module exists under `doge-hmm/` with PyO3 API surface and initial Baum-Welch/forward-backward core implementation.
8. `doge_core.py` supports opt-in shadow comparison mode (`DOGE_CORE_SHADOW=1`) with Python-authoritative results and divergence logging.
9. Shadow telemetry is exposed in `/api/status` (`state_machine_shadow`) and rendered in dashboard UI.
10. State-machine shadow operations runbook exists at `docs/STATE_MACHINE_SHADOW_RUNBOOK.md`.

Remaining major scope:

1. Rust HMM parity implementation and integration (`doge-hmm`).
2. Final production rollout/shadow operations hardening.

### 1.2 Why Three Languages

The bot has three distinct computational domains that map naturally to three languages:

| Domain | Current | Target | Why |
|--------|---------|--------|-----|
| State machine (S0→S1→S2 transitions, invariants) | `state_machine.py` (1,125 lines) | **Haskell** | ADTs + exhaustive pattern matching + totality checking. Illegal states become *unrepresentable*. QuickCheck finds edge cases that hand-written scenarios miss. |
| Numerical compute (HMM training/inference, feature extraction) | `hmm_regime_detector.py` (numpy/hmmlearn) | **Rust** (via PyO3) | No GIL. `ndarray` for matrix math. `proptest` for fuzzing. Replaces `hmmlearn` with purpose-built 3-state diagonal-covariance HMM. |
| Orchestration (Kraken API, dashboard, config, persistence, main loop) | `bot.py`, `grid_strategy.py`, etc. | **Python** (unchanged) | I/O-bound, changes most often, already works. Calls into Haskell and Rust via FFI. |

This is viable because:

- **AI-assisted development** eliminates the human pain of polyglot: build configs, FFI boilerplate, cross-language debugging.
- **Clean boundaries**: The three domains barely overlap. Haskell and Rust never talk to each other — both expose Python-callable interfaces.
- **Incremental migration**: Each layer can be ported independently while the Python originals remain as fallback.

## 2. Current Baseline (Validated 2026-02-15)

Codebase snapshot:

1. 16 root Python modules plus tests.
2. Total root Python LOC: **28,762**.
3. Largest files:
   1. `bot.py`: **6,576** lines
   2. `grid_strategy.py`: **5,546** lines
   3. `pair_model.py`: **1,943** lines
   4. `state_machine.py`: **1,125** lines
4. Tests: one large regression suite, `tests/test_hardening_regressions.py` (**3,064** lines).
5. Deployment currently works with Python-only Docker on Railway.
6. Haskell implementation exists under `doge-core/`; Rust scaffold exists under `doge-hmm/` and is not parity-complete yet.
7. CI pipeline exists (`.github/workflows/ci.yml`).

Important architectural facts:

1. Runtime/backtesting/tests depend on `state_machine.py` (not `pair_model.py`) as the active reducer contract.
2. `bot.py` imports `doge_core as sm` — uses **25 unique symbols** including `sm.transition()`, type constructors, `isinstance()` dispatch on action types, and helpers (`find_order`, `remove_order`, `add_entry_order`, `to_dict`, `from_dict`, etc.). The adapter re-exports the full `state_machine` API surface.
3. `backtest_v1.py` imports `doge_core as sm` — uses **16 unique symbols** including `sm.transition()` and `isinstance()` dispatch on all 4 action types.
4. `tests/test_hardening_regressions.py` imports `state_machine as sm` (line 10) — uses **19 unique symbols**, constructing all dataclass types directly.
5. `grid_strategy.py` does **NOT** import `state_machine` — it is not in the migration dependency graph.
6. HMM graceful degradation exists in `bot.py` via module import ladder: prefer `doge_hmm` (Rust), fallback to `hmm_regime_detector` (Python).
7. Serialization uses module-level `sm.to_dict(state)` / `sm.from_dict(data)` — **not** instance methods. No dataclass has a `.to_dict()` method.

## 3. Scope and Boundaries

### 3.1 In Scope

1. Haskell port of the active state machine contract (`state_machine.py`) with Python fallback.
2. Rust implementation of HMM regime detection (`hmm_regime_detector.py`) with Python fallback.
3. Parity harness and shadow mode for safe cutover.
4. CI, build, and deployment updates required to support mixed-language runtime.

### 3.2 Out of Scope

1. Strategy changes during migration (no behavior redesign).
2. New trading features during migration phases.
3. Replacing Python orchestration (`bot.py`, API, persistence, dashboards).
4. Mandatory advanced type-level Haskell work in initial rollout.

## 4. Architecture Targets

Target language ownership:

1. **Python**: orchestration, I/O, exchange integration, persistence, controls.
2. **Haskell**: pure transition/invariant logic currently represented by `state_machine.py`.
3. **Rust**: HMM training/inference and feature extraction currently represented by `hmm_regime_detector.py`.

Boundary rules:

1. Haskell and Rust do not directly call each other.
2. Python remains the coordinator and fallback owner.
3. Every compiled module path has a Python fallback path that can be enabled instantly.

### 4.1 System Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    Python Orchestrator                    │
│  bot.py · grid_strategy.py · pair_model.py · config.py  │
│  kraken_client.py · pair_scanner.py · ai_advisor.py     │
│                                                          │
│  Owns: I/O, HTTP, WebSocket, Supabase, logging, config  │
└───────────┬─────────────────────────┬────────────────────┘
            │ subprocess JSON         │ PyO3 / maturin
            │ (→ shared lib later)    │
            ▼                         ▼
┌───────────────────────┐  ┌──────────────────────────────┐
│   Haskell Core        │  │      Rust Compute (PyO3)     │
│   (doge-core)         │  │      (doge-hmm)              │
│                       │  │                              │
│  State machine:       │  │  HMM regime detector:        │
│  · PairState ADT      │  │  · FeatureExtractor          │
│  · Event / Action ADT │  │  · GaussianHMM train/infer   │
│  · transition()       │  │  · RegimeDetector            │
│  · check_invariants() │  │  · compute_grid_bias()       │
│  · derive_phase()     │  │                              │
│  · bootstrap_orders() │  │  Future:                     │
│                       │  │  · Backtesting acceleration   │
│  QuickCheck properties│  │  · proptest fuzzing           │
└───────────────────────┘  └──────────────────────────────┘
```

### 4.2 Communication Protocols

**Haskell ↔ Python**: JSON over stdin/stdout.

- Haskell compiles to a standalone executable.
- Default runtime path is a **persistent** subprocess (`doge-core-exe --server`) with JSON-lines request/response.
- One-shot subprocess mode is retained as fallback.
- State and events are serialized as JSON strings across the boundary.
- Why JSON: human-debuggable, matches existing `state.json` format, stable contract surface.
- Why subprocess: avoids GHC shared-library pain on Windows; persistent mode removes per-call process spawn overhead.

**Rust ↔ Python**: Native Python objects via PyO3.

- Rust compiles to a Python extension module (`.pyd` / `.so`) via `maturin`.
- Python imports directly: `from doge_hmm import RegimeDetector`.
- NumPy arrays cross the boundary zero-copy via `numpy` crate.

**Haskell ↔ Rust**: No direct communication. Both are called by Python.

## 5. Locked Technical Decisions

### 5.1 Haskell Integration

1. Windows local development: persistent subprocess JSON (`--server`) with one-shot fallback.
2. Linux/Railway: persistent subprocess JSON; evaluate shared library only after parity and operational stability.

### 5.2 Toolchain

1. GHC version: **9.10.3** (current branch/tooling baseline).
2. Build system: **Cabal**.
3. Rust build: **PyO3 + maturin**.

### 5.3 HMM Direction

1. Replace `hmmlearn` with a direct Rust implementation for this 3-state diagonal-covariance use case.
2. Keep output contract API-compatible with current Python detector.

### 5.4 Backtesting Placement

1. Initial backtesting remains on current state machine contract path (Python/Haskell parity path).
2. Rust backtesting acceleration is a later optimization track, not part of core cutover.

## 6. Prerequisite: Python Freeze Gate

Refactor begins only after these are true:

1. No state-machine semantic changes for 2–4 weeks.
2. No Sev-1/Sev-2 incidents tied to order lifecycle or invariants in that window.
3. Regression suite is consistently green in CI/deploy.
4. State snapshot schema is frozen or explicitly versioned.
5. Baseline performance telemetry is captured and retained.

Required baseline metrics:

1. Loop duration distribution (p50, p95, p99).
2. Fill-to-action latency.
3. CPU and memory usage under normal load.
4. Invariant violation rate.
5. HMM training/inference time and cadence.

## 7. Migration Plan (Status-Tracked)

### Phase 0: Spec Lock and Contracts

Status:

1. **Completed** (branch implementation complete).

Deliverables:

1. Final migration contract document for `state_machine.py` API surface (25 symbols — see Appendix C.0):
   1. state shape (`PairState` — 30 fields, `EngineConfig` — 24 fields, `OrderState` — 12 fields)
   2. event schemas (5 types: `PriceTick`, `TimerTick`, `FillEvent`, `RecoveryFillEvent`, `RecoveryCancelEvent`)
   3. action schemas (4 types: `PlaceOrderAction`, `CancelOrderAction`, `OrphanOrderAction`, `BookCycleAction`)
   4. `transition()` signature: `(PairState, Event, EngineConfig, float, dict|None) → (PairState, list[Action])`
   5. full helper function surface used by `bot.py`, `backtest_v1.py`, and tests (12 functions)
   6. `isinstance()` dispatch sites in action execution (`bot.py`)
2. Final migration contract document for `hmm_regime_detector.py` API surface.
3. Frozen fixture corpus extracted from current behavior.
4. Go/no-go checklist signed off.

Exit criteria:

1. Contracts and tolerance thresholds agreed.
2. No unresolved architecture-blocking questions.

### Phase 1: State Machine Parity Harness

Status:

1. **Completed** (cross-language parity harness active with 13 fixtures).

Deliverables:

1. Golden fixtures for transition paths and invariants.
2. Shadow test runner comparing Python reducer outputs against candidate Haskell outputs.
3. Divergence reporting format with reproducible replay inputs.

Exit criteria:

1. Harness can replay critical scenarios and randomized cases.
2. Divergence reports are actionable and deterministic.

### Phase 2: Haskell State Machine MVP

Status:

1. **Completed** (all 5 event handlers native; strict transition path in Haskell binary).

Deliverables:

1. Haskell reducer implementing active `state_machine.py` semantics (see Appendix A for types).
2. Python adapter preserving existing call contracts (`bot.py`, `backtest_v1.py`).
3. Windows and Linux subprocess execution path.
4. Persistent subprocess mode (`doge-core-exe --server`) with one-shot fallback.
5. Feature flags for backend selection and graceful Python fallback at adapter layer.

Exit criteria:

1. Deterministic parity on agreed scenario corpus.
2. Invariants preserved at agreed tolerance.
3. No orchestration-level API break in `bot.py` integration path.
4. Haskell binary no longer delegates transition logic to Python.

### Phase 3: Production Shadow Mode (State Machine)

Status:

1. **In progress** (adapter-level shadow compare path implemented, telemetry exposed via status/dashboard; operational rollout still pending).

Deliverables:

1. Dual-run mode in non-actuating path (Python authoritative, Haskell shadow).
2. Divergence logging with per-event context.
3. Operational dashboards for divergence frequency/severity.

Exit criteria:

1. Stable shadow period (target: 48h+) with no high-severity divergences.
2. Clear rollback procedure validated.

### Phase 4: Rust HMM MVP

Status:

1. **In progress** (core algorithm implementation underway; numerical parity and rollout validation pending).

Deliverables:

1. Rust `RegimeDetector` with API-compatible Python import shape.
2. Feature extraction parity checks against current implementation.
3. Training/inference parity metrics versus `hmmlearn` reference behavior.
4. Runtime feature flag and hard fallback to Python HMM.

Exit criteria:

1. Label/confidence behavior within agreed thresholds.
2. No increase in runtime errors in HMM-dependent paths.

### Phase 5: Integrated Cutover

Status:

1. **Pending** (depends on Rust HMM completion + rollout gates).

Deliverables:

1. Controlled enablement plan:
   1. Haskell state machine path
   2. Rust HMM path
2. CI matrix for Python + Haskell + Rust artifacts.
3. Deployment runbook, rollback runbook, and alert thresholds.

Exit criteria:

1. Production stability holds during staged rollout.
2. No material behavior regressions versus baseline.

### Phase 6: Optimization and Advanced Types (Optional)

Status:

1. **Pending/optional**.

Optional only after all prior phases are stable:

1. Shared-library Haskell boundary evaluation.
2. Rust backtesting acceleration.
3. Advanced Haskell type-level constraints (phantom types, Liquid Haskell) as R&D, not critical path.

## 8. Validation Strategy

Validation tiers:

1. Unit parity for deterministic helper behavior.
2. Transition parity for event-by-event outputs.
3. Invariant parity across scenario and randomized streams.
4. Shadow parity under production-like event flow.

Tolerance policy:

1. State machine outputs target exact structural parity where deterministic.
2. Numerical outputs may allow explicit epsilon where floating-point differences are expected.
3. HMM outputs use predefined statistical thresholds, not exact bitwise parity.

### 8.1 Cross-Language Test Harness

```python
# tests/test_cross_language.py
def test_transition_parity():
    """Haskell transition() matches Python transition() for all scenarios."""
    from state_machine import transition as py_transition
    from doge_core import transition as hs_transition

    for scenario in load_golden_fixtures():
        state, events, cfg, order_size_usd = scenario
        py_state = state
        hs_state = state
        for event in events:
            py_state, py_actions = py_transition(py_state, event, cfg, order_size_usd)
            hs_state, hs_actions = hs_transition(hs_state, event, cfg, order_size_usd)
            assert py_state == hs_state, f"Divergence at {event}"
            assert py_actions == hs_actions
```

### 8.2 Per-Language Test Strategy

| Layer | Framework | Focus |
|-------|-----------|-------|
| Haskell | QuickCheck + Hspec | Property tests: invariant preservation across random event sequences. Unit tests: each handler. |
| Rust | `cargo test` + proptest | Numerical accuracy: HMM training convergence, feature extraction matches numpy. Fuzz: random observation sequences. |
| Python | pytest | Integration: end-to-end with compiled modules. Fallback: tests pass with Python-only imports. |

## 9. Build and Deployment

### 9.1 Principles

1. Keep runtime image minimal; avoid shipping compilers in final image.
2. Use multi-stage builds and artifact-only runtime layers.
3. Preserve boot path if compiled modules are unavailable.

### 9.2 Dockerfile

```dockerfile
# Stage 1: Haskell build
FROM haskell:9.6-slim AS haskell-build
WORKDIR /build
COPY doge-core/ ./doge-core/
RUN cd doge-core && cabal update && cabal build \
    && cp $(cabal list-bin doge-core-exe) /build/doge-core-exe

# Stage 2: Rust build
FROM rust:1.77-slim AS rust-build
RUN apt-get update && apt-get install -y python3-dev python3-pip python3-venv && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --break-system-packages maturin
WORKDIR /build
COPY doge-hmm/ ./doge-hmm/
RUN cd doge-hmm && maturin build --release \
    && cp target/wheels/*.whl /build/

# Stage 3: Runtime
FROM python:3.12-slim
WORKDIR /app
COPY --from=haskell-build /build/doge-core-exe /app/
COPY --from=rust-build /build/*.whl /tmp/
RUN pip install /tmp/*.whl && rm /tmp/*.whl
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app/
# Python fallbacks still work even if compiled modules fail to load
CMD ["python", "bot.py"]
```

Practical expectation:

1. Image size will likely exceed initial optimistic estimates (~300MB+ with GHC runtime + Rust binary).
2. CI duration will increase; caching strategy is required.

### 9.3 CI Pipeline

CI exists at `.github/workflows/ci.yml`.

Current pipeline:

1. `haskell-build` job:
   1. Setup GHC/Cabal/Stack (`ghc 9.10.3`, `cabal 3.12.1.0`, `stack 3.7.1`).
   2. Cache Stack artifacts.
   3. `stack build` in `doge-core/`.
2. `python-tests` job:
   1. Setup Python and install `requirements.txt` + `pytest`.
   2. Setup Haskell toolchain and build `doge-core`.
   3. Run full Python suite.
   4. Run explicit cross-language parity tests with `DOGE_CORE_BACKEND=haskell`.

Reference workflow (abridged):

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]

jobs:
  haskell-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: haskell-actions/setup@v2
        with:
          ghc-version: "9.10.3"
          stack-version: "3.7.1"
      - run: cd doge-core && stack build

  python-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt pytest
      - uses: haskell-actions/setup@v2
        with:
          ghc-version: "9.10.3"
          stack-version: "3.7.1"
      - run: cd doge-core && stack build
      - run: python -m pytest tests -x
      - run: DOGE_CORE_BACKEND=haskell python -m pytest tests/test_cross_language.py -x -v
```

### 9.4 Build Script

```bash
#!/usr/bin/env bash
# build.sh — build all compiled modules
set -euo pipefail

echo "=== Building Haskell core ==="
(cd doge-core && cabal update && cabal build)

echo "=== Building Rust HMM ==="
(cd doge-hmm && maturin develop --release)

echo "=== Running per-language tests ==="
(cd doge-core && cabal test)
(cd doge-hmm && cargo test)

echo "=== Running Python integration tests ==="
python -m pytest tests/

echo "=== All builds successful ==="
```

### 9.5 Railway Config

```toml
# railway.toml
[build]
dockerfilePath = "Dockerfile"

[deploy]
startCommand = "python bot.py"
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

## 10. Risk Register

1. **Wrong migration target risk**
   Mitigation: Anchor state-machine migration to `state_machine.py` contract, not `pair_model.py`. Verified: `bot.py` and `backtest_v1.py` route reducer calls through `doge_core` which preserves the `state_machine` contract.

2. **Behavior drift during shadow rollout**
   Mitigation: deterministic fixtures, divergence replay tooling, explicit rollback switch.

3. **Windows Haskell tooling friction**
   Mitigation: subprocess boundary first; keep shared library as later optimization.

4. **Build complexity and CI slowdown**
   Mitigation: stage-wise pipeline, caching, separate artifact jobs, final-image minimization.

5. **HMM numeric divergence vs `hmmlearn`**
   Mitigation: threshold-based validation, shadow telemetry, controlled rollout.

6. **Overreach into advanced type work too early**
   Mitigation: treat advanced Haskell features as post-stability optional track.

7. **Stale dependency graph assumptions**
   Mitigation: Verified that `grid_strategy.py` does NOT import `state_machine`. Runtime callers are `bot.py` and `backtest_v1.py`. Tests (`tests/test_hardening_regressions.py`) are also heavy direct callers (constructing all dataclass types, calling `sm.transition`, `sm.add_entry_order`, `sm.find_order`, etc.) and must pass unmodified under both Python and Haskell paths.

## 11. Success Criteria

1. State-machine migration introduces no high-severity behavioral regressions.
2. Fallback mode remains operable and tested throughout rollout.
3. HMM Rust path meets agreed parity thresholds and improves compute profile.
4. Production stability metrics are at least as good as Python baseline.
5. Rollback path is validated in drills before full enablement.
6. All invariants pass in Haskell QuickCheck with 100K+ random event sequences.
7. Cross-language parity harness passes on golden fixtures and randomized corpus.

## 12. Open Items Before Rust/HMM Start

1. Finalize Rust HMM API contract to mirror `hmm_regime_detector.py` behaviors and fallback semantics.
2. Decide whether Rust HMM will use persistent subprocess JSON or PyO3 extension first in this branch.
3. Define HMM parity thresholds (labels/confidence drift) and fixture corpus.
4. Define production shadow-period duration and abort criteria for full polyglot rollout.
5. Set CI runtime budget and caching thresholds once Rust job is added.

---

## Appendix A: Haskell Type Definitions

These types map directly to `state_machine.py` dataclasses (validated against source 2026-02-15).

### A.1 Core ADTs

```haskell
-- Literal types → Haskell sum types
data Phase = S0 | S1a | S1b | S2
  deriving (Eq, Show, Generic, ToJSON, FromJSON)

data Side = Buy | Sell
  deriving (Eq, Show, Generic, ToJSON, FromJSON)

data Role = Entry | Exit
  deriving (Eq, Show, Generic, ToJSON, FromJSON)

data TradeId = TradeA | TradeB
  deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### A.2 EngineConfig

```haskell
-- Maps to state_machine.EngineConfig (frozen dataclass, 24 fields)
data EngineConfig = EngineConfig
  { cfgEntryPct              :: !Double           -- 0.2
  , cfgEntryPctA             :: !(Maybe Double)   -- None
  , cfgEntryPctB             :: !(Maybe Double)   -- None
  , cfgProfitPct             :: !Double           -- 1.0
  , cfgRefreshPct            :: !Double           -- 1.0
  , cfgOrderSizeUsd          :: !Double           -- 2.0
  , cfgPriceDecimals         :: !Int              -- 6
  , cfgVolumeDecimals        :: !Int              -- 0
  , cfgMinVolume             :: !Double           -- 13.0
  , cfgMinCostUsd            :: !Double           -- 0.0
  , cfgMakerFeePct           :: !Double           -- 0.25
  , cfgStalePriceMaxAgeSec   :: !Double           -- 60.0
  , cfgS1OrphanAfterSec      :: !Double           -- 1350.0
  , cfgS2OrphanAfterSec      :: !Double           -- 1800.0
  , cfgLossBackoffStart      :: !Int              -- 3
  , cfgLossCooldownStart     :: !Int              -- 5
  , cfgLossCooldownSec       :: !Double           -- 900.0
  , cfgReentryBaseCooldownSec :: !Double          -- 0.0
  , cfgBackoffFactor         :: !Double           -- 0.5
  , cfgBackoffMaxMultiplier  :: !Double           -- 5.0
  , cfgMaxConsecutiveRefreshes :: !Int            -- 3
  , cfgRefreshCooldownSec    :: !Double           -- 300.0
  , cfgMaxRecoverySlots      :: !Int              -- 2
  , cfgStickyModeEnabled     :: !Bool             -- False
  } deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### A.3 OrderState

```haskell
-- Maps to state_machine.OrderState (frozen dataclass, 12 fields)
data OrderState = OrderState
  { osLocalId        :: !Int
  , osSide           :: !Side
  , osRole           :: !Role
  , osPrice          :: !Double
  , osVolume         :: !Double
  , osTradeId        :: !TradeId
  , osCycle          :: !Int
  , osTxid           :: !Text             -- ""
  , osPlacedAt       :: !Double           -- 0.0
  , osEntryPrice     :: !Double           -- 0.0
  , osEntryFee       :: !Double           -- 0.0
  , osEntryFilledAt  :: !Double           -- 0.0
  } deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### A.4 RecoveryOrder

```haskell
-- Maps to state_machine.RecoveryOrder (frozen dataclass, 12 fields)
data RecoveryOrder = RecoveryOrder
  { roRecoveryId     :: !Int
  , roSide           :: !Side
  , roPrice          :: !Double
  , roVolume         :: !Double
  , roTradeId        :: !TradeId
  , roCycle          :: !Int
  , roEntryPrice     :: !Double
  , roOrphanedAt     :: !Double
  , roEntryFee       :: !Double           -- 0.0
  , roEntryFilledAt  :: !Double           -- 0.0
  , roTxid           :: !Text             -- ""
  , roReason         :: !Text             -- "stale"
  } deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### A.5 CycleRecord

```haskell
-- Maps to state_machine.CycleRecord (frozen dataclass, 11 fields)
data CycleRecord = CycleRecord
  { crTradeId      :: !TradeId
  , crCycle        :: !Int
  , crEntryPrice   :: !Double
  , crExitPrice    :: !Double
  , crVolume       :: !Double
  , crGrossProfit  :: !Double
  , crFees         :: !Double
  , crNetProfit    :: !Double
  , crEntryTime    :: !Double             -- 0.0
  , crExitTime     :: !Double             -- 0.0
  , crFromRecovery :: !Bool               -- False
  } deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### A.6 PairState

```haskell
-- Maps to state_machine.PairState (frozen dataclass, 30 fields)
data PairState = PairState
  { psMarketPrice           :: !Double
  , psNow                   :: !Double
  , psOrders                :: ![OrderState]         -- ()
  , psRecoveryOrders        :: ![RecoveryOrder]      -- ()
  , psCompletedCycles       :: ![CycleRecord]        -- ()
  , psCycleA                :: !Int                   -- 1
  , psCycleB                :: !Int                   -- 1
  , psNextOrderId           :: !Int                   -- 1
  , psNextRecoveryId        :: !Int                   -- 1
  , psTotalProfit           :: !Double                -- 0.0
  , psTotalFees             :: !Double                -- 0.0
  , psTodayRealizedLoss     :: !Double                -- 0.0
  , psTotalRoundTrips       :: !Int                   -- 0
  , psS2EnteredAt           :: !(Maybe Double)        -- None
  , psLastPriceUpdateAt     :: !(Maybe Double)        -- None
  , psConsecutiveLossesA    :: !Int                   -- 0
  , psConsecutiveLossesB    :: !Int                   -- 0
  , psCooldownUntilA        :: !Double                -- 0.0
  , psCooldownUntilB        :: !Double                -- 0.0
  , psLongOnly              :: !Bool                  -- False
  , psShortOnly             :: !Bool                  -- False
  , psModeSource            :: !Text                  -- "none"
  , psConsecutiveRefreshesA :: !Int                   -- 0
  , psConsecutiveRefreshesB :: !Int                   -- 0
  , psLastRefreshDirectionA :: !(Maybe Text)          -- None
  , psLastRefreshDirectionB :: !(Maybe Text)          -- None
  , psRefreshCooldownUntilA :: !Double                -- 0.0
  , psRefreshCooldownUntilB :: !Double                -- 0.0
  , psProfitPctRuntime      :: !Double                -- 1.0
  } deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### A.7 Events (5 types)

```haskell
-- Maps to state_machine Event union (5 frozen dataclasses)
data Event
  = EvPriceTick
      { ptPrice     :: !Double
      , ptTimestamp  :: !Double
      }
  | EvTimerTick
      { ttTimestamp  :: !Double
      }
  | EvFill
      { flOrderLocalId :: !Int
      , flTxid         :: !Text
      , flSide         :: !Side
      , flPrice        :: !Double
      , flVolume       :: !Double
      , flFee          :: !Double
      , flTimestamp    :: !Double
      }
  | EvRecoveryFill
      { rfRecoveryId :: !Int
      , rfTxid       :: !Text
      , rfSide       :: !Side
      , rfPrice      :: !Double
      , rfVolume     :: !Double
      , rfFee        :: !Double
      , rfTimestamp   :: !Double
      }
  | EvRecoveryCancel
      { rcRecoveryId :: !Int
      , rcTxid       :: !Text
      , rcTimestamp   :: !Double
      }
  deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### A.8 Actions (4 types)

```haskell
-- Maps to state_machine Action union (4 frozen dataclasses)
data Action
  = ActPlaceOrder
      { poLocalId  :: !Int
      , poSide     :: !Side
      , poRole     :: !Role
      , poPrice    :: !Double
      , poVolume   :: !Double
      , poTradeId  :: !TradeId
      , poCycle    :: !Int
      , poPostOnly :: !Bool             -- True
      , poReason   :: !Text             -- ""
      }
  | ActCancelOrder
      { coLocalId :: !Int
      , coTxid    :: !Text
      , coReason  :: !Text              -- ""
      }
  | ActOrphanOrder
      { ooLocalId    :: !Int
      , ooRecoveryId :: !Int
      , ooReason     :: !Text
      }
  | ActBookCycle
      { bcTradeId      :: !TradeId
      , bcCycle        :: !Int
      , bcNetProfit    :: !Double
      , bcGrossProfit  :: !Double
      , bcFees         :: !Double
      , bcFromRecovery :: !Bool          -- False
      }
  deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### A.9 Core Function

```haskell
-- Pure transition: the heart of the system.
-- Compiler enforces exhaustive pattern matching on ALL 5 event variants.
-- Extra params: order_size_usd (always required), order_sizes (optional override dict).
transition
  :: PairState
  -> Event
  -> EngineConfig
  -> Double                      -- order_size_usd
  -> Maybe (Map Text Double)     -- order_sizes (optional)
  -> (PairState, [Action])
transition state event cfg orderSizeUsd orderSizes = case event of
  EvPriceTick{}      -> handlePriceTick state event cfg orderSizeUsd orderSizes
  EvTimerTick{}      -> handleTimerTick state event cfg orderSizeUsd orderSizes
  EvFill{}           -> handleFill state event cfg orderSizeUsd orderSizes
  EvRecoveryFill{}   -> handleRecoveryFill state event cfg orderSizeUsd orderSizes
  EvRecoveryCancel{} -> handleRecoveryCancel state event cfg orderSizeUsd orderSizes
  -- GHC warning: Pattern match(es) are non-exhaustive
  -- ^ This NEVER compiles if we miss a case

-- Helper functions also ported:
derivePhase :: PairState -> Phase
checkInvariants :: PairState -> [Text]
computeOrderVolume :: Double -> EngineConfig -> Double -> Maybe Double
bootstrapOrders :: PairState -> EngineConfig -> Double -> Bool -> Bool
                -> (PairState, [Action], [OrderState])
```

### A.10 QuickCheck Properties

```haskell
-- For ALL possible event sequences, invariants hold.
prop_invariants_preserved :: EngineConfig -> Double -> [Event] -> Property
prop_invariants_preserved cfg orderSizeUsd events =
  let (finalState, _) = foldl' step (initialState, []) events
  in checkInvariants finalState === []
  where
    step (s, _) ev = transition s ev cfg orderSizeUsd Nothing
```

## Appendix B: Rust Type Definitions

These types map directly to `hmm_regime_detector.py` classes (validated against source 2026-02-15).

### B.1 Crate Structure

```
doge-hmm/
├── Cargo.toml
├── pyproject.toml          # maturin config
├── src/
│   ├── lib.rs              # PyO3 module definition
│   ├── features.rs         # FeatureExtractor
│   ├── hmm.rs              # GaussianHMM (replaces hmmlearn)
│   ├── regime.rs           # RegimeDetector, RegimeState, Regime enum
│   └── math/
│       ├── mod.rs
│       ├── ema.rs           # EMA, RSI, MACD implementations
│       └── baum_welch.rs    # Baum-Welch training + forward algorithm
```

### B.2 Regime Enum

```rust
use pyo3::prelude::*;

/// Maps to hmm_regime_detector.Regime (IntEnum)
#[pyclass(eq, eq_int)]
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Regime {
    Bearish = 0,
    Ranging = 1,
    Bullish = 2,
}
```

### B.3 RegimeState

```rust
/// Maps to hmm_regime_detector.RegimeState (dataclass)
#[pyclass(get_all)]
#[derive(Clone, Debug)]
pub struct RegimeState {
    pub regime: i32,                 // Regime.RANGING = 1
    pub probabilities: Vec<f64>,     // [0.0, 1.0, 0.0]
    pub confidence: f64,             // 0.0
    pub bias_signal: f64,            // 0.0
    pub last_update_ts: f64,         // 0.0
    pub observation_count: usize,    // 0
}

#[pymethods]
impl RegimeState {
    fn to_dict(&self, py: Python) -> PyResult<PyObject> { ... }

    #[classmethod]
    fn from_dict(cls: &PyType, d: &PyDict) -> PyResult<Self> { ... }
}
```

### B.4 IndicatorSnapshot

```rust
/// Maps to hmm_regime_detector.IndicatorSnapshot (dataclass)
#[derive(Clone, Debug)]
pub struct IndicatorSnapshot {
    pub macd_hist_slope: f64,
    pub ema_spread_pct: f64,
    pub rsi_zone: f64,
    pub volume_ratio: f64,
}
```

### B.5 FeatureExtractor

```rust
/// Maps to hmm_regime_detector.FeatureExtractor
/// Configurable EMA/MACD/RSI/volume parameters
#[pyclass]
pub struct FeatureExtractor {
    fast_ema_periods: usize,       // 9
    slow_ema_periods: usize,       // 21
    macd_fast: usize,              // 12
    macd_slow: usize,              // 26
    macd_signal: usize,            // 9
    rsi_period: usize,             // 14
    volume_avg_period: usize,      // 20
}

#[pymethods]
impl FeatureExtractor {
    #[new]
    #[pyo3(signature = (
        fast_ema_periods=9, slow_ema_periods=21,
        macd_fast=12, macd_slow=26, macd_signal=9,
        rsi_period=14, volume_avg_period=20
    ))]
    fn new(
        fast_ema_periods: usize, slow_ema_periods: usize,
        macd_fast: usize, macd_slow: usize, macd_signal: usize,
        rsi_period: usize, volume_avg_period: usize,
    ) -> Self { ... }

    /// Extract feature matrix from close prices and volumes.
    /// Returns (n_samples, 4) ndarray: [macd_hist_slope, ema_spread_pct, rsi_zone, volume_ratio]
    fn extract<'py>(
        &self,
        py: Python<'py>,
        closes: PyReadonlyArray1<f64>,
        volumes: PyReadonlyArray1<f64>,
    ) -> PyResult<&'py PyArray2<f64>> { ... }
}
```

### B.6 RegimeDetector

```rust
/// Maps to hmm_regime_detector.RegimeDetector
/// Main public API: train(), update(), needs_retrain()
#[pyclass]
pub struct RegimeDetector {
    cfg: HmmConfig,
    model: Option<GaussianHmm>,
    extractor: FeatureExtractor,
    state: RegimeState,
    label_map: [Regime; 3],
    trained: bool,
    last_train_ts: f64,
    obs_mean: Vec<f64>,
    obs_std: Vec<f64>,
}

/// Configuration — maps to RegimeDetector.DEFAULT_CONFIG dict
pub struct HmmConfig {
    pub n_states: usize,                    // 3
    pub n_iter: usize,                      // 100
    pub covariance_type: CovarianceType,    // Diag
    pub inference_window: usize,            // 50
    pub confidence_threshold: f64,          // 0.15
    pub retrain_interval_sec: f64,          // 86400.0
    pub min_train_samples: usize,           // 500
    pub bias_gain: f64,                     // 1.0
    pub blend_with_trend: f64,              // 0.5
}

#[pymethods]
impl RegimeDetector {
    #[new]
    fn new(config: Option<&PyDict>) -> Self { ... }

    /// Train HMM on historical close/volume data. Returns true on success.
    fn train(&mut self, closes: PyReadonlyArray1<f64>,
             volumes: PyReadonlyArray1<f64>) -> bool { ... }

    /// Run inference on recent data. Returns updated RegimeState.
    fn update(&mut self, closes: PyReadonlyArray1<f64>,
              volumes: PyReadonlyArray1<f64>) -> RegimeState { ... }

    /// Check if retrain interval has elapsed.
    fn needs_retrain(&self) -> bool { ... }
}
```

### B.7 Custom GaussianHMM

```rust
/// 3-state Gaussian HMM with diagonal covariance.
/// Replaces hmmlearn dependency entirely.
pub struct GaussianHmm {
    n_states: usize,
    n_features: usize,
    transition_matrix: Array2<f64>,  // (n_states, n_states)
    means: Array2<f64>,              // (n_states, n_features)
    covars: Array2<f64>,             // (n_states, n_features) — diagonal
    initial_probs: Array1<f64>,      // (n_states,)
}

impl GaussianHmm {
    /// Baum-Welch (EM) training.
    pub fn fit(&mut self, observations: &Array2<f64>, n_iter: usize) -> Result<()> { ... }

    /// Forward algorithm → posterior probabilities.
    pub fn predict_proba(&self, observations: &Array2<f64>) -> Array2<f64> { ... }

    /// Viterbi decoding (optional, for debugging).
    pub fn decode(&self, observations: &Array2<f64>) -> Vec<usize> { ... }
}
```

### B.8 Module-Level Functions

```rust
/// Maps to hmm_regime_detector.compute_blended_idle_target()
#[pyfunction]
fn compute_blended_idle_target(
    trend_score: f64,
    hmm_bias: f64,
    blend_factor: f64,
    base_target: f64,
    sensitivity: f64,
    floor: f64,
    ceiling: f64,
) -> f64 { ... }

/// Maps to hmm_regime_detector.compute_grid_bias()
/// Returns dict with: mode, entry_spacing_mult_a, entry_spacing_mult_b, size_skew_override
#[pyfunction]
#[pyo3(signature = (regime_state, confidence_threshold=0.15))]
fn compute_grid_bias(
    py: Python,
    regime_state: &RegimeState,
    confidence_threshold: f64,
) -> PyResult<PyObject> { ... }

/// Maps to hmm_regime_detector.serialize_for_snapshot()
#[pyfunction]
fn serialize_for_snapshot(detector: &RegimeDetector) -> PyResult<PyObject> { ... }

/// Maps to hmm_regime_detector.restore_from_snapshot()
#[pyfunction]
fn restore_from_snapshot(detector: &mut RegimeDetector, snapshot: &PyDict) -> PyResult<()> { ... }
```

## Appendix C: Python Wrapper Sketches

### C.0 Full `state_machine` API Surface

`bot.py`, `backtest_v1.py`, and `tests/test_hardening_regressions.py` collectively use **25 unique symbols** from `state_machine`. The wrapper must either implement or re-export all of them:

**Dataclass types** (used as constructors, type annotations, and `isinstance()` targets):
`PairState`, `EngineConfig`, `OrderState`, `RecoveryOrder`, `CycleRecord`,
`PlaceOrderAction`, `CancelOrderAction`, `OrphanOrderAction`, `BookCycleAction`

**Event constructors**:
`PriceTick`, `TimerTick`, `FillEvent`, `RecoveryFillEvent`, `RecoveryCancelEvent`

**Type aliases** (annotations only):
`Action`, `Event`

**Functions**:
`transition`, `check_invariants`, `derive_phase`, `compute_order_volume`,
`add_entry_order`, `find_order`, `remove_order`, `remove_recovery`,
`apply_order_txid`, `bootstrap_orders`, `to_dict`, `from_dict`

Critical constraint: `bot.py` dispatches action execution using `isinstance(action, sm.PlaceOrderAction)` etc. Actions **must** be returned as proper dataclass instances, not raw dicts.

### C.1 Integration Strategy

Haskell only replaces the **pure transition kernel** — the function `transition()` and its invariant checker. All dataclass types, event constructors, and state-manipulation helpers (`find_order`, `remove_order`, `add_entry_order`, etc.) remain in Python. The wrapper module re-exports the full Python API surface with only `transition` and `check_invariants` routed through Haskell.

```python
# doge_core.py — Haskell-accelerated state machine with full API re-export
#
# Design: Only transition() and check_invariants() cross the Haskell boundary.
# All types, constructors, and helpers are re-exported from state_machine.py
# so that `import doge_core as sm` is a drop-in replacement for
# `import state_machine as sm`.

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

# Re-export the FULL Python API surface — types, constructors, helpers.
# bot.py uses isinstance() checks on these (e.g. isinstance(action, sm.PlaceOrderAction)),
# so they MUST be the real dataclass types.
from state_machine import (  # noqa: F401 — re-exports
    PairState, EngineConfig, OrderState, RecoveryOrder, CycleRecord,
    PlaceOrderAction, CancelOrderAction, OrphanOrderAction, BookCycleAction,
    PriceTick, TimerTick, FillEvent, RecoveryFillEvent, RecoveryCancelEvent,
    Action, Event,
    derive_phase, compute_order_volume, add_entry_order,
    find_order, remove_order, remove_recovery, apply_order_txid,
    bootstrap_orders, to_dict, from_dict,
)
import state_machine as _sm

_HASKELL_EXE = Path(__file__).parent / (
    "doge-core-exe.exe" if sys.platform == "win32" else "doge-core-exe"
)

def _call_haskell(method: str, payload: dict) -> dict:
    """Send JSON request to Haskell subprocess, return JSON response."""
    request = json.dumps({"method": method, **payload})
    result = subprocess.run(
        [str(_HASKELL_EXE)],
        input=request,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Haskell process failed: {result.stderr}")
    return json.loads(result.stdout)

def _state_to_json(state: PairState) -> dict:
    """Serialize PairState using the module-level to_dict (not instance method)."""
    return _sm.to_dict(state)

def _actions_from_json(raw_actions: list[dict]) -> list[Action]:
    """Reconstruct proper dataclass instances from Haskell JSON output.

    Critical: bot.py uses isinstance() dispatch on action types,
    so we MUST return real PlaceOrderAction / CancelOrderAction / etc. instances.
    """
    action_map = {
        "PlaceOrderAction": PlaceOrderAction,
        "CancelOrderAction": CancelOrderAction,
        "OrphanOrderAction": OrphanOrderAction,
        "BookCycleAction": BookCycleAction,
    }
    result = []
    for raw in raw_actions:
        action_type = raw.pop("_type")
        cls = action_map[action_type]
        result.append(cls(**raw))
    return result

def transition(state, event, cfg, order_size_usd, order_sizes=None):
    """Drop-in replacement for state_machine.transition().

    Sends state/event/config as JSON to Haskell subprocess.
    Returns (PairState, list[Action]) with proper dataclass instances.
    """
    resp = _call_haskell("transition", {
        "state": _state_to_json(state),
        "event": asdict(event),
        "config": asdict(cfg),
        "order_size_usd": order_size_usd,
        "order_sizes": order_sizes,
    })
    new_state = _sm.from_dict(resp["state"])
    actions = _actions_from_json(resp["actions"])
    return new_state, actions

def check_invariants(state):
    """Drop-in replacement for state_machine.check_invariants()."""
    resp = _call_haskell("check_invariants", {
        "state": _state_to_json(state),
    })
    return resp["violations"]
```

### C.2 Fallback Wiring in bot.py

The existing HMM graceful degradation pattern in `bot.py` proves the fallback approach. The state machine follows the same pattern. Because `doge_core` re-exports all 25 symbols, the import swap is a single line:

```python
# In bot.py — state machine import with fallback.
# doge_core re-exports all types/helpers, so `sm.PairState`, `sm.find_order`,
# `sm.PlaceOrderAction` etc. all resolve correctly under either path.
try:
    import doge_core as sm      # Haskell-accelerated transition()
    _USING_HASKELL = True
except (ImportError, OSError):
    import state_machine as sm  # Pure Python fallback
    _USING_HASKELL = False

# In bot.py — HMM import with fallback
try:
    from doge_hmm import RegimeDetector
    _USING_RUST_HMM = True
except ImportError:
    from hmm_regime_detector import RegimeDetector
    _USING_RUST_HMM = False
```

### C.3 Rust HMM Usage (Drop-in Replacement)

```python
# Before (Python + hmmlearn):
from hmm_regime_detector import RegimeDetector
detector = RegimeDetector(config)
detector.train(closes, volumes)
state = detector.update(closes[-100:], volumes[-100:])

# After (Rust + PyO3) — identical API:
from doge_hmm import RegimeDetector
detector = RegimeDetector(config)
detector.train(closes, volumes)
state = detector.update(closes[-100:], volumes[-100:])
```

## Appendix D: Build Artifacts

### D.1 Haskell Cabal File

```yaml
-- doge-core/doge-core.cabal
cabal-version: 3.0
name:          doge-core
version:       0.1.0
build-type:    Simple

library
  exposed-modules: DogeCore.Types
                 , DogeCore.Transition
                 , DogeCore.Invariants
                 , DogeCore.Helpers
                 , DogeCore.Json
  build-depends:   base >= 4.16 && < 5
                 , aeson >= 2.0
                 , text
                 , containers
  default-language: GHC2021
  ghc-options:     -O2 -Wall -Werror

executable doge-core-exe
  main-is:       Main.hs
  hs-source-dirs: app
  build-depends:  base, doge-core, aeson, bytestring, text
  default-language: GHC2021
  ghc-options:    -O2 -threaded

test-suite tests
  type:            exitcode-stdio-1.0
  main-is:         Spec.hs
  hs-source-dirs:  test
  build-depends:   base, doge-core, QuickCheck >= 2.14, hspec
  default-language: GHC2021
```

### D.2 Rust Cargo.toml

```toml
# doge-hmm/Cargo.toml
[package]
name = "doge-hmm"
version = "0.1.0"
edition = "2021"

[lib]
name = "doge_hmm"
crate-type = ["cdylib"]

[dependencies]
pyo3 = { version = "0.22", features = ["extension-module"] }
numpy = "0.22"
ndarray = "0.16"
rand = "0.8"
rand_distr = "0.4"

[dev-dependencies]
proptest = "1.4"
approx = "0.5"
```

### D.3 Rust pyproject.toml

```toml
# doge-hmm/pyproject.toml
[build-system]
requires = ["maturin>=1.0"]
build-backend = "maturin"

[tool.maturin]
features = ["pyo3/extension-module"]
```
