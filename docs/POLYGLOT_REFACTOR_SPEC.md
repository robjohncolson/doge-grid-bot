# Polyglot Refactor Spec — Haskell + Rust + Python

**Version**: 0.1.0
**Status**: DRAFT
**Branch**: `polyglot-refactor`
**Date**: 2026-02-15

## 1. Motivation

The bot has three distinct computational domains that map naturally to three languages:

| Domain | Current | Target | Why |
|--------|---------|--------|-----|
| State machine (S0/S1/S2 transitions, invariants, simulation) | `pair_model.py` (900 lines, pure Python) | **Haskell** | ADTs + exhaustive pattern matching + totality checking. Illegal states become *unrepresentable*. QuickCheck finds edge cases that 6 hand-written scenarios miss. |
| Numerical compute (HMM training/inference, feature extraction, backtesting) | `hmm_regime_detector.py` (570 lines, numpy/hmmlearn) | **Rust** (via PyO3) | 1000x simulation speedup. No GIL. `nalgebra` + `ndarray` for matrix math. `proptest` for fuzzing. |
| Orchestration (Kraken API, dashboard, config, persistence, main loop) | `bot.py`, `dashboard.py`, `grid_strategy.py`, etc. | **Python** (unchanged) | I/O-bound, changes most often, already works. Calls into Haskell and Rust via FFI. |

### Why three languages is viable here

- **AI-assisted development** (Claude Code + Codex) eliminates the human pain of polyglot: build configs, FFI boilerplate, cross-language debugging, context-switching.
- **Clean boundaries**: The three domains barely overlap. Haskell and Rust never talk to each other — both expose Python-callable interfaces.
- **Incremental migration**: Each layer can be ported independently while the Python originals remain as fallback.

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Python Orchestrator                    │
│  bot.py · dashboard.py · grid_strategy.py · config.py   │
│  kraken_client.py · pair_scanner.py · ai_advisor.py     │
│                                                          │
│  Owns: I/O, HTTP, WebSocket, Supabase, logging, config  │
└───────────┬─────────────────────────┬────────────────────┘
            │ cffi / ctypes           │ PyO3 / maturin
            ▼                         ▼
┌───────────────────────┐  ┌──────────────────────────────┐
│   Haskell Core (FFI)  │  │      Rust Compute (PyO3)     │
│                       │  │                              │
│  State machine:       │  │  HMM regime detector:        │
│  · PairState ADT      │  │  · FeatureExtractor          │
│  · Event / Action ADT │  │  · GaussianHMM train/infer   │
│  · transition()       │  │  · Multi-timeframe consensus  │
│  · check_invariants() │  │                              │
│  · simulate()         │  │  Backtesting engine:         │
│  · predict()          │  │  · 1M-step random walk       │
│  · from_state_json()  │  │  · Scenario replay           │
│                       │  │  · Performance stats         │
│  QuickCheck properties│  │                              │
│  Phantom type safety  │  │  proptest fuzzing            │
└───────────────────────┘  └──────────────────────────────┘
```

### 2.1 Communication Protocol

**Haskell ↔ Python**: JSON over C FFI.
- Haskell compiles to a shared library (`.so` / `.dll`) via `foreign export ccall`.
- Python calls via `cffi` (or `ctypes`).
- State and events are serialized as JSON strings across the boundary.
- Why JSON: human-debuggable, matches existing `state.json` format, negligible overhead at 30s cycle times.

**Rust ↔ Python**: Native Python objects via PyO3.
- Rust compiles to a Python extension module (`.pyd` / `.so`) via `maturin`.
- Python imports directly: `from doge_hmm import RegimeDetector, FeatureExtractor`.
- NumPy arrays cross the boundary zero-copy via `numpy` crate.

**Haskell ↔ Rust**: No direct communication. Both are called by Python.

## 3. Haskell State Machine — `doge-core`

### 3.1 Type Design

```haskell
-- 3.1.1 Core ADTs (maps to pair_model.py §2)
data Phase = S0 | S1a | S1b | S2
  deriving (Eq, Show, Generic, ToJSON, FromJSON)

data Side = Buy | Sell
  deriving (Eq, Show, Generic, ToJSON, FromJSON)

data Role = Entry | Exit
  deriving (Eq, Show, Generic, ToJSON, FromJSON)

data TradeId = TradeA | TradeB
  deriving (Eq, Show, Generic, ToJSON, FromJSON)

-- 3.1.2 Order state (frozen — no mutation)
data OrderState = OrderState
  { osSide             :: !Side
  , osRole             :: !Role
  , osPrice            :: !Double
  , osVolume           :: !Double
  , osTradeId          :: !TradeId
  , osCycle            :: !Int
  , osEntryFilledAt    :: !Double
  , osMatchedEntryPrice :: !Double
  } deriving (Eq, Show, Generic, ToJSON, FromJSON)

-- 3.1.3 Recovery order
data RecoveryState = RecoveryState
  { rsSide         :: !Side
  , rsPrice        :: !Double
  , rsVolume       :: !Double
  , rsTradeId      :: !TradeId
  , rsCycle        :: !Int
  , rsEntryPrice   :: !Double
  , rsOrphanedAt   :: !Double
  , rsEntryFilledAt :: !Double
  , rsReason       :: !Text
  } deriving (Eq, Show, Generic, ToJSON, FromJSON)

-- 3.1.4 Completed cycle record
data CycleRecord = CycleRecord
  { crTradeId    :: !TradeId
  , crCycle      :: !Int
  , crEntryPrice :: !Double
  , crExitPrice  :: !Double
  , crVolume     :: !Double
  , crGross      :: !Double
  , crFees       :: !Double
  , crNet        :: !Double
  , crEntryTime  :: !Double
  , crExitTime   :: !Double
  } deriving (Eq, Show, Generic, ToJSON, FromJSON)

-- 3.1.5 Full pair state (the big one — maps to PairState dataclass)
data PairState = PairState
  { psMarketPrice       :: !Double
  , psNow               :: !Double
  , psOrders            :: ![OrderState]
  , psRecoveryOrders    :: ![RecoveryState]
  , psCompletedCycles   :: ![CycleRecord]
  , psCycleA            :: !Int
  , psCycleB            :: !Int
  , psTotalProfit       :: !Double
  , psTotalFees         :: !Double
  , psTotalRoundTrips   :: !Int
  , psTotalRecoveryWins :: !Double
  , psTotalRecoveryLosses :: !Int
  -- Exit lifecycle
  , psS2EnteredAt       :: !(Maybe Double)
  , psS2LastActionAt    :: !(Maybe Double)
  , psLastRepriceA      :: !Double
  , psLastRepriceB      :: !Double
  , psExitRepriceCountA :: !Int
  , psExitRepriceCountB :: !Int
  , psLastPriceUpdateAt :: !(Maybe Double)
  -- Directional
  , psDetectedTrend     :: !(Maybe Text)
  , psTrendDetectedAt   :: !(Maybe Double)
  -- Anti-chase
  , psConsecRefreshesA  :: !Int
  , psConsecRefreshesB  :: !Int
  , psLastRefreshDirA   :: !(Maybe Text)
  , psLastRefreshDirB   :: !(Maybe Text)
  , psRefreshCooldownA  :: !Double
  , psRefreshCooldownB  :: !Double
  -- Timing
  , psMedianCycleDuration :: !(Maybe Double)
  , psMeanNetProfit     :: !(Maybe Double)
  , psMeanDurationSec   :: !(Maybe Double)
  -- Modes
  , psLongOnly          :: !Bool
  -- Backoff
  , psConsecLossesA     :: !Int
  , psConsecLossesB     :: !Int
  , psLastVolatilityAdj :: !Double
  } deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### 3.2 Events & Actions

```haskell
-- Events (input to transition)
data Event
  = EvBuyFill   { bfPrice :: !Double, bfVolume :: !Double }
  | EvSellFill  { sfPrice :: !Double, sfVolume :: !Double }
  | EvPriceTick { ptPrice :: !Double }
  | EvTimeAdv   { taTime  :: !Double }
  | EvRecoveryFill   { rfIndex :: !Int, rfPrice :: !Double }
  | EvRecoveryCancel { rcIndex :: !Int }
  deriving (Eq, Show, Generic, ToJSON, FromJSON)

-- Actions (output from transition)
data Action
  = ActPlaceOrder  !Side !Role !Double !Double !TradeId !Int !Double
  | ActCancelOrder !OrderState !Text
  | ActBookProfit  !TradeId !Int !Double !Double !Double
  | ActOrphanExit  !OrderState !Text
  | ActRepriceExit !OrderState !Double !Int
  | ActDetectTrend !Text
  deriving (Eq, Show, Generic, ToJSON, FromJSON)
```

### 3.3 Core Function

```haskell
-- Pure transition: the heart of the system.
-- Compiler enforces exhaustive pattern matching on ALL event variants.
transition :: PairState -> Event -> ModelConfig -> (PairState, [Action])
transition state event cfg = case event of
  EvBuyFill{}        -> handleBuyFill state event cfg
  EvSellFill{}       -> handleSellFill state event cfg
  EvPriceTick{}      -> handlePriceTick state event cfg
  EvTimeAdv{}        -> handleTimeAdvance state event cfg
  EvRecoveryFill{}   -> handleRecoveryFill state event cfg
  EvRecoveryCancel{} -> handleRecoveryCancel state event cfg
  -- GHC warning: Pattern match(es) are non-exhaustive
  -- ^ This NEVER compiles if we miss a case
```

### 3.4 Invariant Checking

```haskell
-- Returns [] if all 12 invariants hold, or list of violation descriptions.
checkInvariants :: PairState -> ModelConfig -> [Text]

-- QuickCheck property: for ALL possible event sequences, invariants hold.
prop_invariants_preserved :: ModelConfig -> [Event] -> Property
prop_invariants_preserved cfg events =
  let (finalState, _) = foldl' step (initialState, []) events
  in checkInvariants finalState cfg === []
  where
    step (s, _) ev = transition s ev cfg
```

### 3.5 FFI Boundary

```haskell
-- Exported C functions. Python calls these via cffi.
foreign export ccall hs_transition
  :: CString -> CString -> CString -> IO CString
  -- Args: state_json, event_json, config_json
  -- Returns: result_json = {"state": {...}, "actions": [...]}

foreign export ccall hs_check_invariants
  :: CString -> CString -> IO CString
  -- Args: state_json, config_json
  -- Returns: ["violation1", ...] or []

foreign export ccall hs_predict
  :: CString -> CString -> IO CString

foreign export ccall hs_simulate
  :: CString -> CString -> CString -> IO CString
  -- Args: initial_state_json, events_json, config_json
  -- Returns: trace_json
```

### 3.6 Python Wrapper

```python
# doge_core.py — thin Python wrapper around Haskell FFI
import json
from cffi import FFI

ffi = FFI()
ffi.cdef("""
    char* hs_transition(const char* state, const char* event, const char* config);
    char* hs_check_invariants(const char* state, const char* config);
    char* hs_predict(const char* state, const char* config);
    char* hs_simulate(const char* state, const char* events, const char* config);
    void  hs_free(char* ptr);
""")

_lib = ffi.dlopen("./libdoge_core.so")  # or .dll on Windows

def transition(state: dict, event: dict, config: dict) -> tuple[dict, list]:
    result_ptr = _lib.hs_transition(
        json.dumps(state).encode(),
        json.dumps(event).encode(),
        json.dumps(config).encode(),
    )
    result = json.loads(ffi.string(result_ptr))
    _lib.hs_free(result_ptr)
    return result["state"], result["actions"]
```

### 3.7 What This Catches That Python Can't

1. **Missing event handler**: Add `EvNewEventType` to Event → compiler error in `transition` until you handle it.
2. **Impossible state construction**: Phantom types can encode "S2 state MUST have exactly 2 exit orders" at the type level.
3. **Property-based invariant testing**: QuickCheck generates millions of random event sequences, not just 6 hand-written scenarios + 10K random walk.
4. **Refactoring safety**: Change any field in `PairState` → compiler shows every function that needs updating.

## 4. Rust Compute Layer — `doge-hmm`

### 4.1 Crate Structure

```
doge-hmm/
├── Cargo.toml
├── pyproject.toml          # maturin config
├── src/
│   ├── lib.rs              # PyO3 module definition
│   ├── features.rs         # FeatureExtractor (port of §2 in hmm_regime_detector.py)
│   ├── hmm.rs              # GaussianHMM (train + infer, replaces hmmlearn)
│   ├── regime.rs           # RegimeDetector, RegimeState, Regime enum
│   ├── consensus.rs        # Multi-timeframe consensus (port of _compute_hmm_consensus)
│   ├── backtest.rs         # Simulation engine for backtesting
│   └── math/
│       ├── mod.rs
│       ├── ema.rs           # EMA, RSI, MACD implementations
│       └── baum_welch.rs    # Baum-Welch training + forward algorithm
```

### 4.2 Key Types

```rust
use pyo3::prelude::*;
use numpy::{PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};

#[pyclass]
#[derive(Clone, Debug)]
pub enum Regime {
    Bearish = 0,
    Ranging = 1,
    Bullish = 2,
}

#[pyclass]
#[derive(Clone, Debug)]
pub struct RegimeState {
    #[pyo3(get)]
    pub regime: Regime,
    #[pyo3(get)]
    pub probabilities: [f64; 3],
    #[pyo3(get)]
    pub confidence: f64,
    #[pyo3(get)]
    pub bias_signal: f64,
    #[pyo3(get)]
    pub last_update_ts: f64,
    #[pyo3(get)]
    pub observation_count: usize,
}

#[pyclass]
pub struct RegimeDetector {
    model: Option<GaussianHmm>,
    extractor: FeatureExtractor,
    state: RegimeState,
    label_map: [Regime; 3],
    trained: bool,
    last_train_ts: f64,
    obs_mean: Vec<f64>,
    obs_std: Vec<f64>,
}

#[pymethods]
impl RegimeDetector {
    #[new]
    fn new(config: Option<&PyDict>) -> Self { ... }

    /// Train HMM on historical OHLCV data. Returns True on success.
    fn train(&mut self, closes: PyReadonlyArray1<f64>,
             volumes: PyReadonlyArray1<f64>) -> bool { ... }

    /// Run inference on recent data. Returns updated RegimeState.
    fn update(&mut self, closes: PyReadonlyArray1<f64>,
              volumes: PyReadonlyArray1<f64>) -> RegimeState { ... }

    fn needs_retrain(&self) -> bool { ... }
}
```

### 4.3 Custom HMM (Replacing hmmlearn)

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

### 4.4 Backtesting Engine

```rust
#[pyclass]
pub struct BacktestEngine {
    config: BacktestConfig,
}

#[pymethods]
impl BacktestEngine {
    /// Run N-step random walk, return invariant violations + performance stats.
    /// 1M steps in ~200ms (vs ~30s in Python).
    fn random_walk(&self, n_steps: usize, seed: u64) -> BacktestResult { ... }

    /// Replay historical price series through the state machine.
    /// Useful for parameter optimization.
    fn replay(&self, prices: PyReadonlyArray1<f64>,
              timestamps: PyReadonlyArray1<f64>) -> BacktestResult { ... }
}
```

### 4.5 Python Usage (Drop-in Replacement)

```python
# Before (Python + hmmlearn)
from hmm_regime_detector import RegimeDetector
detector = RegimeDetector(config)
detector.train(closes, volumes)
state = detector.update(closes[-100:], volumes[-100:])

# After (Rust + PyO3) — same API
from doge_hmm import RegimeDetector
detector = RegimeDetector(config)
detector.train(closes, volumes)
state = detector.update(closes[-100:], volumes[-100:])
```

## 5. Python Orchestration Layer — What Stays

These files are **unchanged** except for import swaps:

| File | Lines | Changes |
|------|-------|---------|
| `bot.py` | ~2000 | Import `doge_core.transition` instead of `pair_model.transition`. Import `doge_hmm.RegimeDetector` instead of `hmm_regime_detector.RegimeDetector`. |
| `dashboard.py` | ~1900 | None |
| `grid_strategy.py` | ~3500 | Delegates state transitions to Haskell core. Keeps Kraken order execution. |
| `config.py` | ~500 | None |
| `kraken_client.py` | ~800 | None |
| `pair_scanner.py` | ~200 | None |
| `ai_advisor.py` | ~100 | None |

### 5.1 Fallback Strategy

Both compiled modules have Python fallbacks:

```python
# In grid_strategy.py
try:
    from doge_core import transition, check_invariants
    _USING_HASKELL = True
except ImportError:
    from pair_model import transition, check_invariants
    _USING_HASKELL = False

# In bot.py
try:
    from doge_hmm import RegimeDetector
    _USING_RUST_HMM = True
except ImportError:
    from hmm_regime_detector import RegimeDetector
    _USING_RUST_HMM = False
```

This means the bot always works — compiled modules are performance/safety upgrades, not hard requirements.

## 6. Build System

### 6.1 Haskell (`doge-core/`)

```yaml
# doge-core/doge-core.cabal
name:          doge-core
version:       0.1.0
build-type:    Simple

library
  exposed-modules: DogeCore.Types, DogeCore.Transition, DogeCore.Invariants,
                   DogeCore.Simulate, DogeCore.Predict, DogeCore.FFI
  build-depends:   base >= 4.16
                 , aeson >= 2.0
                 , text
                 , vector
                 , QuickCheck >= 2.14
  default-language: GHC2021
  ghc-options:     -O2 -Wall -Werror
  -- Compile to shared library
  ghc-options:     -shared -dynamic -fPIC

test-suite tests
  type:            exitcode-stdio-1.0
  main-is:         Spec.hs
  build-depends:   base, doge-core, QuickCheck, hspec
```

Build: `cd doge-core && cabal build`
Output: `libdoge_core.so` (Linux), `doge_core.dll` (Windows)

### 6.2 Rust (`doge-hmm/`)

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
```

```toml
# doge-hmm/pyproject.toml
[build-system]
requires = ["maturin>=1.0"]
build-backend = "maturin"

[tool.maturin]
features = ["pyo3/extension-module"]
```

Build: `cd doge-hmm && maturin develop --release`
Output: `doge_hmm.pyd` (Windows) / `doge_hmm.so` (Linux) in Python path.

### 6.3 Unified Build Script

```bash
#!/usr/bin/env bash
# build.sh — build all compiled modules
set -euo pipefail

echo "=== Building Haskell core ==="
(cd doge-core && cabal build && cp dist-newstyle/.../libdoge_core.so ..)

echo "=== Building Rust HMM ==="
(cd doge-hmm && maturin develop --release)

echo "=== Running tests ==="
(cd doge-core && cabal test)
(cd doge-hmm && cargo test)
python -m pytest tests/

echo "=== All builds successful ==="
```

## 7. Deployment (Railway)

### 7.1 Dockerfile

```dockerfile
# Multi-stage: Haskell build → Rust build → Python runtime
FROM haskell:9.6 AS haskell-build
WORKDIR /build
COPY doge-core/ ./doge-core/
RUN cd doge-core && cabal update && cabal build \
    && cp $(find dist-newstyle -name 'libdoge_core.so') /build/

FROM rust:1.77 AS rust-build
WORKDIR /build
COPY doge-hmm/ ./doge-hmm/
RUN pip install maturin \
    && cd doge-hmm && maturin build --release \
    && cp target/wheels/*.whl /build/

FROM python:3.12-slim
WORKDIR /app
COPY --from=haskell-build /build/libdoge_core.so /app/
COPY --from=rust-build /build/*.whl /tmp/
RUN pip install /tmp/*.whl && rm /tmp/*.whl
COPY . /app/
# Python fallbacks still work even if compiled modules fail to load
CMD ["python", "bot.py"]
```

### 7.2 Railway Config

```toml
# railway.toml
[build]
dockerfilePath = "Dockerfile"

[deploy]
startCommand = "python bot.py"
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

Image size: ~300MB (GHC runtime + Rust binary + Python). Acceptable for Railway.

## 8. Migration Plan

### Phase 1: Haskell State Machine (Week 1-2)

1. **Port types**: `ModelConfig`, `PairState`, `OrderState`, `RecoveryState`, `CycleRecord` → Haskell ADTs.
2. **Port `transition()`**: All 6 event handlers + sub-machines (stale exits, S2 break-glass, entry refresh, trend expiry).
3. **Port `check_invariants()`**: All 12 invariants.
4. **QuickCheck properties**: `prop_invariants_preserved`, `prop_no_negative_cycles`, `prop_phase_derivation_consistent`.
5. **FFI layer**: JSON-based C exports + Python `doge_core.py` wrapper.
6. **Validation**: Run all 6 scenarios + 10K random walk through BOTH Python and Haskell, assert identical outputs.
7. **Wire in**: `grid_strategy.py` imports from `doge_core` with Python fallback.

### Phase 2: Rust HMM (Week 2-3)

1. **Port `FeatureExtractor`**: EMA, RSI, MACD, volume ratio.
2. **Implement `GaussianHmm`**: Baum-Welch training + forward algorithm (replaces hmmlearn entirely).
3. **Port `RegimeDetector`**: Train/update/state lifecycle.
4. **Port consensus**: Multi-timeframe blending logic from `bot.py._compute_hmm_consensus()`.
5. **PyO3 bindings**: Same API as Python original.
6. **Validation**: Train on same data, assert regime labels match Python ± floating point tolerance.
7. **Wire in**: `bot.py` imports from `doge_hmm` with Python fallback.

### Phase 3: Backtesting Engine (Week 3-4)

1. **Port `simulate()` + `explore_random()`** to Rust.
2. **Add `BacktestEngine.replay()`** for historical price series.
3. **Performance target**: 1M random transitions in <500ms.
4. **Add parameter sweep**: Grid search over `entry_pct`, `profit_pct`, `exit_reprice_mult` etc.

### Phase 4: Advanced Haskell Types (Week 4+)

1. **Phantom types** for phase-indexed state (e.g., `PairState 'S1a` can only hold the right order composition).
2. **Liquid Haskell** annotations for proving invariants at compile time (stretch goal).
3. **Session types** for the FFI boundary (stretch goal).

## 9. Testing Strategy

### 9.1 Cross-Language Validation

The Python originals (`pair_model.py`, `hmm_regime_detector.py`) remain in the repo as reference implementations. A test harness feeds identical inputs to both and asserts matching outputs:

```python
# tests/test_cross_language.py
def test_transition_parity():
    """Haskell transition() matches Python transition() for all scenarios."""
    from pair_model import transition as py_transition
    from doge_core import transition as hs_transition

    for scenario_fn in [scenario_normal_oscillation, scenario_trending_market, ...]:
        name, initial, events, cfg = scenario_fn()
        py_state = initial
        hs_state = initial
        for event in events:
            py_state, py_actions = py_transition(py_state, event, cfg)
            hs_state, hs_actions = hs_transition(hs_state, event, cfg)
            assert py_state == hs_state, f"Divergence in {name} at {event}"
            assert py_actions == hs_actions
```

### 9.2 Per-Language Tests

| Layer | Framework | Focus |
|-------|-----------|-------|
| Haskell | QuickCheck + Hspec | Property tests: invariant preservation across random event sequences. Unit tests: each sub-machine. |
| Rust | `cargo test` + proptest | Numerical accuracy: HMM training convergence, feature extraction matches numpy. Fuzz: random observation sequences. |
| Python | pytest | Integration: end-to-end with compiled modules. Fallback: tests pass with Python-only imports. |

### 9.3 CI Pipeline

```yaml
# .github/workflows/test.yml
jobs:
  haskell:
    runs-on: ubuntu-latest
    steps:
      - uses: haskell-actions/setup@v2
      - run: cd doge-core && cabal test

  rust:
    runs-on: ubuntu-latest
    steps:
      - uses: actions-rust-lang/setup-rust-toolchain@v1
      - run: cd doge-hmm && cargo test

  integration:
    needs: [haskell, rust]
    runs-on: ubuntu-latest
    steps:
      - run: cd doge-core && cabal build
      - run: cd doge-hmm && maturin develop --release
      - run: python -m pytest tests/
```

## 10. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Haskell GHC runtime too large for Railway | Low | Medium | Multi-stage Docker. Static linking. Fallback: ship Haskell as subprocess (JSON stdin/stdout) instead of shared lib. |
| Windows development: Haskell shared lib + GHC on Windows | Medium | Medium | Develop with WSL2 or Docker dev container. Or: Haskell as subprocess on Windows, shared lib on Linux/Railway. |
| JSON serialization overhead on FFI boundary | Low | Low | At 30s cycles, even 10ms of JSON parse is invisible. Profile if needed, switch to MessagePack. |
| Floating-point divergence between Python and Haskell | Medium | Low | Accept ε tolerance in cross-validation. Pin rounding modes. |
| hmmlearn → custom Rust HMM numerical differences | Medium | Medium | Validate against hmmlearn on reference dataset. Accept ε. Log both during shadow period. |

## 11. Success Criteria

- [ ] All 12 invariants pass in Haskell QuickCheck with 100K random event sequences
- [ ] Haskell `transition()` matches Python `transition()` on all 6 scenarios + 10K random walk
- [ ] Rust HMM matches hmmlearn regime labels on reference dataset (±5% confidence tolerance)
- [ ] Bot runs successfully on Railway with compiled modules
- [ ] Fallback mode works: bot runs with Python-only imports if compiled modules missing
- [ ] Backtesting: 1M random transitions complete in <500ms (Rust)
- [ ] No regression in production behavior (shadow-run both codepaths for 48h)

## 12. Open Questions

1. **Haskell FFI on Windows**: Should we use subprocess (JSON over stdin/stdout) for local dev and shared lib only for Linux/Railway?
2. **GHC version**: 9.6 (latest stable) or 9.8 (GHC2024 features)?
3. **Rust HMM**: Port hmmlearn's exact algorithm, or use a cleaner implementation (e.g., `linfa-hmm` crate if it exists)?
4. **Cabal vs Stack**: Cabal is lighter, Stack is more reproducible. Preference?
5. **Should backtesting live in Rust or Haskell?** Currently specced as Rust (for speed), but Haskell could also do it (with the state machine natively available). Trade-off: Rust is faster, Haskell has the types.
