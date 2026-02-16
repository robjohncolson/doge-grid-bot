"""
hmm_regime_detector.py — HMM-based regime classifier for DOGE/USD grid bot

Sits alongside the existing trend detector (§15) as a higher-order regime
classifier. Feeds regime probabilities into the rebalancer's dynamic idle
target and grid bias logic.

Architecture:
    - Offline training via Baum-Welch on historical 5-min OHLCV from Kraken
    - Online inference via forward algorithm on each rebalancer tick
    - Output: regime probabilities + derived bias signal
    - Integration: modulates existing trend_score sensitivity and idle target

Dependencies:
    pip install hmmlearn numpy pandas

Author: Robert / Claude sketch — 2026-02-14
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from typing import Optional

import numpy as np

try:
    from hmmlearn.hmm import GaussianHMM
except ImportError:
    GaussianHMM = None  # graceful degradation if not installed

logger = logging.getLogger("hmm_regime")


# ---------------------------------------------------------------------------
# 1. Regime definitions
# ---------------------------------------------------------------------------

class Regime(IntEnum):
    """
    Three latent states. Labels are assigned post-training by inspecting
    the learned emission means (see RegimeDetector._label_states).
    """
    BEARISH  = 0
    RANGING  = 1
    BULLISH  = 2


# ---------------------------------------------------------------------------
# 2. Observation feature extraction
# ---------------------------------------------------------------------------

@dataclass
class IndicatorSnapshot:
    """One row of the observation matrix fed to the HMM."""
    macd_hist_slope: float    # rate of change of MACD histogram
    ema_spread_pct: float     # (fast_ema - slow_ema) / slow_ema  (matches §15)
    rsi_zone: float           # normalized: -1 (oversold) to +1 (overbought)
    volume_ratio: float       # current vol / rolling avg vol


class FeatureExtractor:
    """
    Computes the 4-dimensional observation vector from raw OHLCV candles.
    
    Designed to run on each rebalancer tick (default every 300s) using
    the most recent N candles from the price_history table.
    
    All parameters use the same halflife conventions as §15 for consistency.
    """

    def __init__(
        self,
        fast_ema_periods: int = 9,
        slow_ema_periods: int = 21,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        rsi_period: int = 14,
        volume_avg_period: int = 20,
    ):
        self.fast_ema_periods = fast_ema_periods
        self.slow_ema_periods = slow_ema_periods
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.rsi_period = rsi_period
        self.volume_avg_period = volume_avg_period

    @staticmethod
    def _ema(series: np.ndarray, span: int) -> np.ndarray:
        """Exponential moving average via recursive filter."""
        alpha = 2.0 / (span + 1)
        out = np.empty_like(series)
        out[0] = series[0]
        for i in range(1, len(series)):
            out[i] = alpha * series[i] + (1 - alpha) * out[i - 1]
        return out

    @staticmethod
    def _rsi(closes: np.ndarray, period: int) -> np.ndarray:
        """Standard RSI calculation."""
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = np.empty(len(deltas))
        avg_loss = np.empty(len(deltas))
        avg_gain[:period] = np.nan
        avg_loss[:period] = np.nan

        avg_gain[period - 1] = gains[:period].mean()
        avg_loss[period - 1] = losses[:period].mean()

        for i in range(period, len(deltas)):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period

        rs = avg_gain / np.where(avg_loss == 0, 1e-10, avg_loss)
        rsi = 100.0 - 100.0 / (1.0 + rs)

        # prepend NaN for the first element (lost to diff)
        return np.concatenate([[np.nan], rsi])

    def extract(self, closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
        """
        Extract observation matrix from OHLCV arrays.
        
        Args:
            closes: array of close prices, oldest first
            volumes: array of volumes, oldest first
        
        Returns:
            observations: (T, 4) array of [macd_hist_slope, ema_spread_pct,
                          rsi_zone, volume_ratio]
            Only rows where all indicators are valid (no NaN) are returned.
        """
        n = len(closes)
        assert len(volumes) == n, "closes and volumes must be same length"

        # --- EMA spread (matches §15 trend_score) ---
        fast_ema = self._ema(closes, self.fast_ema_periods)
        slow_ema = self._ema(closes, self.slow_ema_periods)
        ema_spread_pct = (fast_ema - slow_ema) / np.where(
            slow_ema == 0, 1e-10, slow_ema
        )

        # --- MACD histogram slope ---
        macd_fast_ema = self._ema(closes, self.macd_fast)
        macd_slow_ema = self._ema(closes, self.macd_slow)
        macd_line = macd_fast_ema - macd_slow_ema
        macd_signal = self._ema(macd_line, self.macd_signal)
        macd_hist = macd_line - macd_signal
        macd_hist_slope = np.concatenate([[0.0], np.diff(macd_hist)])

        # --- RSI zone: map 0-100 to -1..+1 ---
        rsi_raw = self._rsi(closes, self.rsi_period)
        rsi_zone = (rsi_raw - 50.0) / 50.0  # -1 = oversold, +1 = overbought

        # --- Volume ratio ---
        vol_avg = self._ema(volumes, self.volume_avg_period)
        volume_ratio = volumes / np.where(vol_avg == 0, 1e-10, vol_avg)

        # --- Stack and trim NaN rows ---
        obs = np.column_stack([
            macd_hist_slope,
            ema_spread_pct,
            rsi_zone,
            volume_ratio,
        ])

        valid_mask = ~np.any(np.isnan(obs), axis=1)
        return obs[valid_mask]


# ---------------------------------------------------------------------------
# 3. HMM training and inference
# ---------------------------------------------------------------------------

@dataclass
class RegimeState:
    """Serializable state for persistence in bot_state snapshot."""
    regime: int = Regime.RANGING
    probabilities: list[float] = field(
        default_factory=lambda: [0.0, 1.0, 0.0]  # default: 100% ranging
    )
    confidence: float = 0.0       # max(probabilities) - second_max
    bias_signal: float = 0.0      # -1.0 (full bearish) to +1.0 (full bullish)
    last_update_ts: float = 0.0
    observation_count: int = 0    # how many obs in current inference window

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RegimeState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class RegimeDetector:
    """
    HMM-based regime detector.
    
    Lifecycle:
        1. Train offline:  detector.train(closes, volumes)
        2. On each tick:    detector.update(closes, volumes) -> RegimeState
        3. Persist:         detector.state.to_dict() -> snapshot
        4. Restore:         detector.state = RegimeState.from_dict(...)
    
    Integration point: the `bias_signal` output replaces or blends with the
    existing trend_score in §15's dynamic idle target computation.
    """

    # Default config — mirrors the naming style from §15
    DEFAULT_CONFIG = {
        "HMM_N_STATES": 3,
        "HMM_N_ITER": 100,
        "HMM_COVARIANCE_TYPE": "diag",       # "diag" is more stable than "full"
        "HMM_INFERENCE_WINDOW": 50,           # last N observations for inference
        "HMM_CONFIDENCE_THRESHOLD": 0.15,     # min confidence to emit non-zero bias
        "HMM_RETRAIN_INTERVAL_SEC": 86400.0,  # retrain daily
        "HMM_MIN_TRAIN_SAMPLES": 500,         # ~42 hours of 5-min candles
        "HMM_BIAS_GAIN": 1.0,                 # scales bias_signal magnitude
        "HMM_BLEND_WITH_TREND": 0.5,          # 0=pure HMM, 1=pure §15 trend_score
    }

    def __init__(self, config: Optional[dict] = None):
        if GaussianHMM is None:
            raise ImportError(
                "hmmlearn is required: pip install hmmlearn --break-system-packages"
            )

        self.cfg = {**self.DEFAULT_CONFIG, **(config or {})}
        self.model: Optional[GaussianHMM] = None
        self.extractor = FeatureExtractor()
        self.state = RegimeState()
        self._state_label_map: dict[int, Regime] = {}
        self._last_train_ts: float = 0.0
        self._trained = False

    # --- Training -----------------------------------------------------------

    def train(self, closes: np.ndarray, volumes: np.ndarray) -> bool:
        """
        Fit HMM on historical data. Call offline or periodically.
        
        Returns True if training succeeded.
        """
        obs = self.extractor.extract(closes, volumes)

        if len(obs) < self.cfg["HMM_MIN_TRAIN_SAMPLES"]:
            logger.warning(
                "HMM train: only %d samples (need %d), skipping",
                len(obs), self.cfg["HMM_MIN_TRAIN_SAMPLES"],
            )
            return False

        # Standardize features for stable training
        self._obs_mean = obs.mean(axis=0)
        self._obs_std = obs.std(axis=0)
        self._obs_std[self._obs_std == 0] = 1.0
        obs_norm = (obs - self._obs_mean) / self._obs_std

        model = GaussianHMM(
            n_components=self.cfg["HMM_N_STATES"],
            covariance_type=self.cfg["HMM_COVARIANCE_TYPE"],
            n_iter=self.cfg["HMM_N_ITER"],
            random_state=42,
        )

        try:
            model.fit(obs_norm)
        except Exception as e:
            logger.error("HMM training failed: %s", e)
            return False

        self.model = model
        self._label_states(obs_norm)
        self._trained = True
        self._last_train_ts = time.time()

        logger.info(
            "HMM trained on %d samples. State labels: %s. "
            "Transition matrix:\n%s",
            len(obs),
            self._state_label_map,
            np.array2string(model.transmat_, precision=3),
        )
        return True

    def _label_states(self, obs_norm: np.ndarray):
        """
        Assign semantic labels to HMM states by inspecting emission means.
        
        The EMA spread (feature index 1) is the primary discriminator:
            - highest mean  -> BULLISH
            - lowest mean   -> BEARISH
            - middle        -> RANGING
        """
        means = self.model.means_  # shape: (n_states, n_features)
        ema_spread_means = means[:, 1]  # feature 1 = ema_spread_pct

        sorted_indices = np.argsort(ema_spread_means)
        self._state_label_map = {
            sorted_indices[0]: Regime.BEARISH,
            sorted_indices[1]: Regime.RANGING,
            sorted_indices[2]: Regime.BULLISH,
        }

    # --- Inference -----------------------------------------------------------

    def update(self, closes: np.ndarray, volumes: np.ndarray) -> RegimeState:
        """
        Run HMM inference on recent data. Call on each rebalancer tick.
        
        Uses the last HMM_INFERENCE_WINDOW observations for the forward pass.
        Returns updated RegimeState.
        """
        if not self._trained or self.model is None:
            logger.debug("HMM not trained yet, returning default RANGING state")
            return self.state

        obs = self.extractor.extract(closes, volumes)
        if len(obs) == 0:
            return self.state

        # Use tail window for inference
        window = self.cfg["HMM_INFERENCE_WINDOW"]
        obs_tail = obs[-window:]
        obs_norm = (obs_tail - self._obs_mean) / self._obs_std

        try:
            # Forward algorithm → posterior state probabilities for last timestep
            _, posteriors = self.model.score_samples(obs_norm)
            raw_probs = posteriors[-1]  # last timestep's state distribution
        except Exception as e:
            logger.warning("HMM inference failed: %s", e)
            return self.state

        # Remap raw HMM state indices to semantic labels
        labeled_probs = np.zeros(3)
        for raw_idx, label in self._state_label_map.items():
            labeled_probs[label] = raw_probs[raw_idx]

        # Determine regime and confidence
        regime = Regime(int(np.argmax(labeled_probs)))
        sorted_probs = np.sort(labeled_probs)[::-1]
        confidence = sorted_probs[0] - sorted_probs[1]

        # Compute bias signal: weighted sum of probabilities
        # BULLISH contributes +1, BEARISH contributes -1, RANGING contributes 0
        if confidence < self.cfg["HMM_CONFIDENCE_THRESHOLD"]:
            bias_signal = 0.0  # ambiguous → neutral
        else:
            bias_signal = (
                labeled_probs[Regime.BULLISH] - labeled_probs[Regime.BEARISH]
            ) * self.cfg["HMM_BIAS_GAIN"]
            bias_signal = max(-1.0, min(1.0, bias_signal))

        self.state = RegimeState(
            regime=regime,
            probabilities=labeled_probs.tolist(),
            confidence=round(confidence, 4),
            bias_signal=round(bias_signal, 4),
            last_update_ts=time.time(),
            observation_count=len(obs_tail),
        )

        logger.info(
            "HMM regime=%s conf=%.3f bias=%.3f probs=[B:%.2f R:%.2f U:%.2f]",
            regime.name, confidence, bias_signal,
            labeled_probs[0], labeled_probs[1], labeled_probs[2],
        )
        return self.state

    # --- Stale retrain check -------------------------------------------------

    def needs_retrain(self) -> bool:
        """True if model should be retrained (daily by default)."""
        if not self._trained:
            return True
        elapsed = time.time() - self._last_train_ts
        return elapsed >= self.cfg["HMM_RETRAIN_INTERVAL_SEC"]

    @property
    def transmat(self) -> Optional[list[list[float]]]:
        """
        Return the trained transition matrix as a plain list-of-lists.

        Returns None when the model has not been trained yet.
        """
        if not self._trained or self.model is None:
            return None

        matrix = getattr(self.model, "transmat_", None)
        if matrix is None:
            return None

        try:
            arr = np.asarray(matrix, dtype=float)
        except Exception:
            return None

        if arr.ndim != 2 or arr.shape[0] <= 0 or arr.shape[1] <= 0:
            return None
        if not np.isfinite(arr).all():
            return None
        return arr.tolist()


# ---------------------------------------------------------------------------
# 4. Integration helpers: blending with §15 trend system
# ---------------------------------------------------------------------------

def compute_blended_idle_target(
    trend_score: float,
    hmm_bias: float,
    blend_factor: float,
    base_target: float,
    sensitivity: float,
    floor: float,
    ceiling: float,
) -> float:
    """
    Drop-in replacement for §15.2's raw_target computation.
    
    Blends the existing trend_score with the HMM bias_signal.
    
    blend_factor = 0.0  → pure HMM
    blend_factor = 0.5  → equal weight
    blend_factor = 1.0  → pure trend_score (§15 unchanged)
    
    The blended signal replaces `trend_score` in the formula:
        raw_target = base_target - sensitivity * blended_signal
        dynamic_target = clamp(raw_target, floor, ceiling)
    """
    blended = blend_factor * trend_score + (1.0 - blend_factor) * hmm_bias
    raw_target = base_target - sensitivity * blended
    return max(floor, min(ceiling, raw_target))


def compute_grid_bias(
    regime_state: RegimeState,
    confidence_threshold: float = 0.15,
) -> dict:
    """
    Translate regime state into concrete grid-bot actions.
    
    Returns a dict that bot.py can consume to adjust grid behavior:
    
    {
        "mode": "symmetric" | "long_bias" | "short_bias",
        "entry_spacing_mult_a": float,  # multiplier for A-side entry_pct
        "entry_spacing_mult_b": float,  # multiplier for B-side entry_pct
        "size_skew_override": float | None,  # if set, overrides rebalancer skew
    }
    
    This is ADVISORY — bot.py and the reducer still enforce all invariants.
    The rebalancer design constraints (§14.3) are respected:
        - No market orders
        - No new order flow
        - entry_pct is sacred (we only suggest spacing multipliers)
    """
    bias = regime_state.bias_signal
    conf = regime_state.confidence

    # Low confidence → stay symmetric, don't fight noise
    if conf < confidence_threshold:
        return {
            "mode": "symmetric",
            "entry_spacing_mult_a": 1.0,
            "entry_spacing_mult_b": 1.0,
            "size_skew_override": None,
        }

    # Bullish regime: tighten B-side (buy) entries, widen A-side (sell) entries
    # This means we catch more long entries and are pickier about shorts
    if bias > 0:
        return {
            "mode": "long_bias",
            "entry_spacing_mult_a": 1.0 + abs(bias) * 0.5,  # widen short entries
            "entry_spacing_mult_b": max(0.6, 1.0 - abs(bias) * 0.3),  # tighten long entries
            "size_skew_override": min(0.30, abs(bias) * 0.3),  # positive = favor B-side
        }

    # Bearish regime: opposite
    return {
        "mode": "short_bias",
        "entry_spacing_mult_a": max(0.6, 1.0 - abs(bias) * 0.3),  # tighten short entries
        "entry_spacing_mult_b": 1.0 + abs(bias) * 0.5,  # widen long entries
        "size_skew_override": max(-0.30, -abs(bias) * 0.3),  # negative = favor A-side
    }


# ---------------------------------------------------------------------------
# 5. Persistence helpers (for bot_state snapshot)
# ---------------------------------------------------------------------------

def serialize_for_snapshot(detector: RegimeDetector) -> dict:
    """
    Returns dict to merge into the bot_state snapshot payload (§19).
    
    New keys (backward-compatible — absent keys default to safe values):
        _hmm_regime_state: RegimeState as dict
        _hmm_last_train_ts: float
        _hmm_trained: bool
    
    Note: the model itself is NOT serialized here. It's retrained on startup
    from price_history. This avoids pickle/joblib fragility.
    """
    return {
        "_hmm_regime_state": detector.state.to_dict(),
        "_hmm_last_train_ts": detector._last_train_ts,
        "_hmm_trained": detector._trained,
    }


def restore_from_snapshot(detector: RegimeDetector, snapshot: dict):
    """
    Restore regime state from snapshot. Model must be retrained separately.
    """
    if "_hmm_regime_state" in snapshot:
        detector.state = RegimeState.from_dict(snapshot["_hmm_regime_state"])
    detector._last_train_ts = snapshot.get("_hmm_last_train_ts", 0.0)
    # _trained stays False until train() succeeds — this is intentional.
    # The bot runs in RANGING/neutral mode until retrain completes.


# ---------------------------------------------------------------------------
# 6. Example: standalone training + inference demo
# ---------------------------------------------------------------------------

def demo():
    """
    Quick demo with synthetic data. Replace with real Kraken OHLCV.
    """
    np.random.seed(42)

    # Simulate 2000 candles (~7 days of 5-min data)
    n = 2000
    # Three regimes baked in: bear (0-600), range (600-1400), bull (1400-2000)
    price = np.zeros(n)
    price[0] = 0.15  # DOGE starting price
    for i in range(1, n):
        if i < 600:
            drift = -0.0001  # bear
        elif i < 1400:
            drift = 0.0      # range
        else:
            drift = 0.00015  # bull
        price[i] = price[i - 1] * (1 + drift + np.random.randn() * 0.003)

    volume = np.abs(np.random.randn(n) * 1000 + 5000)

    # Train
    detector = RegimeDetector()
    success = detector.train(price, volume)
    print(f"Training succeeded: {success}")

    if success:
        # Inference on last 100 candles
        state = detector.update(price[-100:], volume[-100:])
        print(f"Regime: {Regime(state.regime).name}")
        print(f"Confidence: {state.confidence:.3f}")
        print(f"Bias signal: {state.bias_signal:.3f}")
        print(f"Probabilities: bear={state.probabilities[0]:.3f} "
              f"range={state.probabilities[1]:.3f} "
              f"bull={state.probabilities[2]:.3f}")

        # Grid bias recommendation
        grid = compute_grid_bias(state)
        print(f"\nGrid bias: {grid}")

        # Blended idle target (simulating §15 integration)
        trend_score = 0.002  # example from §15
        target = compute_blended_idle_target(
            trend_score=trend_score,
            hmm_bias=state.bias_signal,
            blend_factor=0.5,
            base_target=0.40,
            sensitivity=5.0,
            floor=0.15,
            ceiling=0.60,
        )
        print(f"Blended idle target: {target:.3f} (pure §15 would be "
              f"{max(0.15, min(0.60, 0.40 - 5.0 * trend_score)):.3f})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo()
