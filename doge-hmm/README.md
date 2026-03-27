# doge-hmm (Rust HMM backend)

This directory contains the initial Rust/PyO3 scaffold for the HMM regime detector
tracked in `docs/POLYGLOT_REFACTOR_SPEC.md` Phase 4.

Current status:
- Python API surface scaffolded (`Regime`, `RegimeState`, `FeatureExtractor`, `RegimeDetector`)
- Snapshot helper functions exposed (`serialize_for_snapshot`, `restore_from_snapshot`)
- Grid-bias and blended-target helper functions exposed
- Diagonal-Gaussian HMM core implemented with Baum-Welch training and forward/backward inference
- State-label remapping by EMA-spread means (`bearish/ranging/bullish`) is implemented

Remaining:
- Numerical parity validation against `hmm_regime_detector.py` / `hmmlearn`
- Real-world performance tuning and convergence validation

Build locally (once Rust toolchain is installed):

```bash
cd doge-hmm
maturin develop --release
```

Then in Python:

```python
from doge_hmm import RegimeDetector
```

This does not replace `hmm_regime_detector.py` yet; the bot keeps fallback behavior.
