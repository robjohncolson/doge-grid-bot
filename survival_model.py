"""
survival_model.py

Survival analysis models for fill probability forecasting.
Implements:
  - Stratified Kaplan-Meier baseline
  - Optional Cox proportional hazards model (numpy)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Any

import numpy as np


def _clamp(value: float, lo: float, hi: float) -> float:
    low = min(float(lo), float(hi))
    high = max(float(lo), float(hi))
    return max(low, min(float(value), high))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(out):
        return float(default)
    return out


def _safe_triplet(raw: Any) -> list[float]:
    if isinstance(raw, dict):
        vec = [
            _safe_float(raw.get("bearish", 0.0), 0.0),
            _safe_float(raw.get("ranging", 0.0), 0.0),
            _safe_float(raw.get("bullish", 0.0), 0.0),
        ]
    elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
        vec = [_safe_float(raw[0], 0.0), _safe_float(raw[1], 0.0), _safe_float(raw[2], 0.0)]
    else:
        vec = [0.0, 1.0, 0.0]
    arr = np.asarray(vec, dtype=float)
    arr = np.where(np.isfinite(arr), arr, 0.0)
    arr = np.clip(arr, 0.0, 1.0)
    total = float(arr.sum())
    if total <= 1e-12:
        return [0.0, 1.0, 0.0]
    return (arr / total).tolist()


def _normalize_regime(raw: Any) -> int:
    try:
        regime = int(raw)
    except (TypeError, ValueError):
        return 1
    if regime not in (0, 1, 2):
        return 1
    return regime


def _normalize_side(raw: Any) -> str:
    side = str(raw or "").strip().upper()
    if side not in {"A", "B"}:
        return "A"
    return side


def _stratum_key(regime: int, side: str) -> str:
    regime_name = {0: "bearish", 1: "ranging", 2: "bullish"}.get(int(regime), "ranging")
    return f"{regime_name}_{_normalize_side(side)}"


def parse_horizons(raw: Any) -> list[int]:
    if isinstance(raw, (list, tuple)):
        vals = []
        for item in raw:
            try:
                v = int(item)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
        uniq = sorted(set(vals))
        return uniq if uniq else [1800, 3600, 14400]
    text = str(raw or "").strip()
    if not text:
        return [1800, 3600, 14400]
    vals = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            v = int(token)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    uniq = sorted(set(vals))
    return uniq if uniq else [1800, 3600, 14400]


@dataclass
class FillObservation:
    duration_sec: float
    censored: bool
    regime_at_entry: int
    regime_at_exit: int | None
    side: str
    distance_pct: float
    posterior_1m: list[float]
    posterior_15m: list[float]
    posterior_1h: list[float]
    entropy_at_entry: float
    p_switch_at_entry: float
    fill_imbalance: float
    congestion_ratio: float
    weight: float = 1.0
    synthetic: bool = False

    def normalized(self) -> "FillObservation":
        return FillObservation(
            duration_sec=max(1.0, _safe_float(self.duration_sec, 1.0)),
            censored=bool(self.censored),
            regime_at_entry=_normalize_regime(self.regime_at_entry),
            regime_at_exit=None if self.regime_at_exit is None else _normalize_regime(self.regime_at_exit),
            side=_normalize_side(self.side),
            distance_pct=max(0.0, _safe_float(self.distance_pct, 0.0)),
            posterior_1m=_safe_triplet(self.posterior_1m),
            posterior_15m=_safe_triplet(self.posterior_15m),
            posterior_1h=_safe_triplet(self.posterior_1h),
            entropy_at_entry=_clamp(_safe_float(self.entropy_at_entry, 0.0), 0.0, 1.0),
            p_switch_at_entry=_clamp(_safe_float(self.p_switch_at_entry, 0.0), 0.0, 1.0),
            fill_imbalance=_clamp(_safe_float(self.fill_imbalance, 0.0), -1.0, 1.0),
            congestion_ratio=_clamp(_safe_float(self.congestion_ratio, 0.0), 0.0, 1.0),
            weight=max(1e-6, _safe_float(self.weight, 1.0)),
            synthetic=bool(self.synthetic),
        )


@dataclass
class SurvivalConfig:
    min_observations: int = 50
    min_per_stratum: int = 10
    synthetic_weight: float = 0.3
    horizons: list[int] = field(default_factory=lambda: [1800, 3600, 14400])


@dataclass
class SurvivalPrediction:
    p_fill_30m: float
    p_fill_1h: float
    p_fill_4h: float
    median_remaining: float
    hazard_ratio: float
    model_tier: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "p_fill_30m": round(float(self.p_fill_30m), 6),
            "p_fill_1h": round(float(self.p_fill_1h), 6),
            "p_fill_4h": round(float(self.p_fill_4h), 6),
            "median_remaining": round(float(self.median_remaining), 6),
            "hazard_ratio": round(float(self.hazard_ratio), 6),
            "model_tier": str(self.model_tier),
            "confidence": round(float(self.confidence), 6),
        }


@dataclass
class _KMSurvivalCurve:
    event_times: np.ndarray
    survival: np.ndarray
    n_observations: int
    n_events: int
    n_censored: int
    weighted_observations: float

    def survival_at(self, t: float) -> float:
        tt = max(0.0, float(t))
        if self.event_times.size == 0:
            return 1.0
        idx = np.searchsorted(self.event_times, tt, side="right") - 1
        if idx < 0:
            return 1.0
        return _clamp(float(self.survival[idx]), 0.0, 1.0)

    def median_time(self) -> float:
        if self.event_times.size == 0:
            return float("inf")
        for t, s in zip(self.event_times, self.survival):
            if float(s) <= 0.5:
                return float(t)
        return float("inf")


class _KaplanMeierModel:
    def __init__(self, min_per_stratum: int = 10) -> None:
        self.min_per_stratum = max(1, int(min_per_stratum))
        self.curves: dict[str, _KMSurvivalCurve] = {}
        self.strata_counts: dict[str, int] = {}
        self._aggregate_key = "aggregate"

    def fit(self, observations: list[FillObservation]) -> None:
        self.curves = {}
        self.strata_counts = {}
        grouped: dict[str, list[FillObservation]] = {self._aggregate_key: list(observations)}
        for obs in observations:
            key = _stratum_key(obs.regime_at_entry, obs.side)
            grouped.setdefault(key, []).append(obs)
        for key, rows in grouped.items():
            self.strata_counts[key] = len(rows)
            curve = self._fit_curve(rows)
            if curve is not None:
                self.curves[key] = curve

    @staticmethod
    def _fit_curve(rows: list[FillObservation]) -> _KMSurvivalCurve | None:
        if not rows:
            return None
        durations = np.asarray([max(1.0, float(r.duration_sec)) for r in rows], dtype=float)
        events = np.asarray([0 if bool(r.censored) else 1 for r in rows], dtype=int)
        weights = np.asarray([max(1e-6, float(r.weight)) for r in rows], dtype=float)

        event_times = np.unique(durations[events == 1])
        if event_times.size == 0:
            return _KMSurvivalCurve(
                event_times=np.asarray([], dtype=float),
                survival=np.asarray([], dtype=float),
                n_observations=len(rows),
                n_events=0,
                n_censored=int(np.sum(events == 0)),
                weighted_observations=float(np.sum(weights)),
            )

        s = 1.0
        surv_vals: list[float] = []
        for t in event_times:
            at_risk = float(np.sum(weights[durations >= t]))
            d_i = float(np.sum(weights[(durations == t) & (events == 1)]))
            if at_risk <= 1e-12:
                continue
            s = s * max(0.0, 1.0 - (d_i / at_risk))
            surv_vals.append(_clamp(s, 0.0, 1.0))
        if not surv_vals:
            return None
        return _KMSurvivalCurve(
            event_times=np.asarray(event_times[: len(surv_vals)], dtype=float),
            survival=np.asarray(surv_vals, dtype=float),
            n_observations=len(rows),
            n_events=int(np.sum(events == 1)),
            n_censored=int(np.sum(events == 0)),
            weighted_observations=float(np.sum(weights)),
        )

    def predict(
        self,
        *,
        regime_at_entry: int,
        side: str,
        horizons: list[int],
    ) -> tuple[dict[int, float], float, float]:
        key = _stratum_key(regime_at_entry, side)
        curve = self.curves.get(key) or self.curves.get(self._aggregate_key)
        if curve is None:
            probs = {int(h): 0.5 for h in horizons}
            return probs, float("inf"), 0.0
        probs = {int(h): _clamp(1.0 - curve.survival_at(float(h)), 0.0, 1.0) for h in horizons}
        confidence = _clamp(
            float(curve.weighted_observations) / max(float(self.min_per_stratum), 1.0),
            0.0,
            1.0,
        )
        return probs, curve.median_time(), confidence


class _CoxPHModel:
    """
    Minimal Cox PH implementation with Breslow baseline hazard.
    """

    FEATURE_NAMES = (
        "p_bear_1m",
        "p_range_1m",
        "p_bull_1m",
        "p_bear_15m",
        "p_range_15m",
        "p_bull_15m",
        "p_bear_1h",
        "p_range_1h",
        "p_bull_1h",
        "side_is_B",
        "distance_pct",
        "entropy",
        "p_switch",
        "fill_imbalance",
        "congestion_ratio",
    )

    def __init__(self) -> None:
        self.coef_: np.ndarray | None = None
        self.mean_: np.ndarray | None = None
        self.std_: np.ndarray | None = None
        self.event_times_: np.ndarray | None = None
        self.base_cumhaz_: np.ndarray | None = None
        self.fitted: bool = False

    @classmethod
    def _features_for(cls, obs: FillObservation) -> np.ndarray:
        side_b = 1.0 if _normalize_side(obs.side) == "B" else 0.0
        return np.asarray(
            _safe_triplet(obs.posterior_1m)
            + _safe_triplet(obs.posterior_15m)
            + _safe_triplet(obs.posterior_1h)
            + [
                side_b,
                max(0.0, float(obs.distance_pct)),
                _clamp(float(obs.entropy_at_entry), 0.0, 1.0),
                _clamp(float(obs.p_switch_at_entry), 0.0, 1.0),
                _clamp(float(obs.fill_imbalance), -1.0, 1.0),
                _clamp(float(obs.congestion_ratio), 0.0, 1.0),
            ],
            dtype=float,
        )

    def fit(self, observations: list[FillObservation], *, l2: float = 1e-3, max_iter: int = 25) -> bool:
        if not observations:
            return False
        durations = np.asarray([max(1.0, float(o.duration_sec)) for o in observations], dtype=float)
        events = np.asarray([0 if bool(o.censored) else 1 for o in observations], dtype=int)
        weights = np.asarray([max(1e-6, float(o.weight)) for o in observations], dtype=float)
        x = np.asarray([self._features_for(o) for o in observations], dtype=float)
        if x.ndim != 2 or x.shape[0] < 2:
            return False
        if int(np.sum(events == 1)) < 2:
            return False

        mean = np.mean(x, axis=0)
        std = np.std(x, axis=0)
        std = np.where(std <= 1e-9, 1.0, std)
        z = (x - mean) / std

        beta = np.zeros(z.shape[1], dtype=float)
        event_times = np.unique(durations[events == 1])
        if event_times.size == 0:
            return False

        for _ in range(max(1, int(max_iter))):
            grad = np.zeros_like(beta)
            hess = np.zeros((beta.size, beta.size), dtype=float)
            risk_scores = np.exp(np.clip(z @ beta, -50.0, 50.0))

            for t in event_times:
                event_mask = (durations == t) & (events == 1)
                if not np.any(event_mask):
                    continue
                risk_mask = durations >= t
                w_event = weights[event_mask]
                z_event = z[event_mask]

                w_risk = weights[risk_mask]
                z_risk = z[risk_mask]
                rs_risk = risk_scores[risk_mask]
                weighted_risk = w_risk * rs_risk
                denom = float(np.sum(weighted_risk))
                if denom <= 1e-12:
                    continue

                weighted_sum_z = np.sum(z_risk * weighted_risk[:, None], axis=0)
                mean_risk = weighted_sum_z / denom

                # Gradient contribution.
                grad += np.sum((w_event[:, None] * z_event), axis=0)
                grad -= float(np.sum(w_event)) * mean_risk

                # Hessian contribution.
                weighted_outer = np.zeros_like(hess)
                for zi, wi in zip(z_risk, weighted_risk):
                    weighted_outer += wi * np.outer(zi, zi)
                cov_risk = (weighted_outer / denom) - np.outer(mean_risk, mean_risk)
                hess -= float(np.sum(w_event)) * cov_risk

            # L2 regularization.
            grad -= l2 * beta
            hess -= l2 * np.eye(beta.size)

            try:
                step = np.linalg.solve(hess, grad)
            except np.linalg.LinAlgError:
                step = np.linalg.pinv(hess) @ grad
            if not np.all(np.isfinite(step)):
                return False
            beta_new = beta - step
            if np.linalg.norm(beta_new - beta) < 1e-5:
                beta = beta_new
                break
            beta = beta_new

        # Breslow baseline cumulative hazard.
        risk_scores = np.exp(np.clip(z @ beta, -50.0, 50.0))
        cum = 0.0
        cumhaz = []
        for t in event_times:
            event_mask = (durations == t) & (events == 1)
            risk_mask = durations >= t
            d_i = float(np.sum(weights[event_mask]))
            denom = float(np.sum(weights[risk_mask] * risk_scores[risk_mask]))
            if denom <= 1e-12:
                continue
            cum += d_i / denom
            cumhaz.append(cum)

        if not cumhaz:
            return False
        self.coef_ = beta
        self.mean_ = mean
        self.std_ = std
        self.event_times_ = np.asarray(event_times[: len(cumhaz)], dtype=float)
        self.base_cumhaz_ = np.asarray(cumhaz, dtype=float)
        self.fitted = True
        return True

    def _baseline_cumhaz_at(self, horizon_sec: float) -> float:
        if not self.fitted or self.event_times_ is None or self.base_cumhaz_ is None:
            return 0.0
        idx = np.searchsorted(self.event_times_, float(horizon_sec), side="right") - 1
        if idx < 0:
            return 0.0
        return max(0.0, float(self.base_cumhaz_[idx]))

    def predict_probs(self, obs: FillObservation, horizons: list[int]) -> tuple[dict[int, float], float]:
        if not self.fitted or self.coef_ is None or self.mean_ is None or self.std_ is None:
            return {int(h): 0.5 for h in horizons}, 1.0
        x = self._features_for(obs)
        z = (x - self.mean_) / self.std_
        lin = float(np.dot(z, self.coef_))
        hazard_ratio = float(np.exp(np.clip(lin, -50.0, 50.0)))
        probs: dict[int, float] = {}
        for h in horizons:
            h0 = self._baseline_cumhaz_at(float(h))
            surv = math.exp(-h0 * hazard_ratio)
            probs[int(h)] = _clamp(1.0 - surv, 0.0, 1.0)
        return probs, hazard_ratio

    def median_time(self, obs: FillObservation) -> float:
        if not self.fitted or self.event_times_ is None or self.base_cumhaz_ is None:
            return float("inf")
        x = self._features_for(obs)
        z = (x - self.mean_) / self.std_
        hr = float(np.exp(np.clip(np.dot(z, self.coef_), -50.0, 50.0)))
        surv = np.exp(-self.base_cumhaz_ * hr)
        idx = np.where(surv <= 0.5)[0]
        if idx.size == 0:
            return float("inf")
        return float(self.event_times_[int(idx[0])])

    def coefficients_dict(self) -> dict[str, float]:
        if self.coef_ is None:
            return {}
        return {
            name: float(val)
            for name, val in zip(self.FEATURE_NAMES, self.coef_.tolist())
        }


class SurvivalModel:
    def __init__(self, cfg: SurvivalConfig, model_tier: str = "kaplan_meier") -> None:
        self.cfg = cfg
        self.model_tier = str(model_tier or "kaplan_meier").strip().lower()
        if self.model_tier not in {"kaplan_meier", "cox"}:
            self.model_tier = "kaplan_meier"

        self.km = _KaplanMeierModel(min_per_stratum=max(1, int(cfg.min_per_stratum)))
        self.cox = _CoxPHModel()
        self.last_retrain_ts: float = 0.0
        self.n_observations: int = 0
        self.n_censored: int = 0
        self.synthetic_observations: int = 0
        self.active_tier: str = "kaplan_meier"
        self.fitted: bool = False

    def fit(self, observations: list[FillObservation], synthetic_observations: list[FillObservation] | None = None) -> bool:
        real = [obs.normalized() for obs in (observations or [])]
        synth = [obs.normalized() for obs in (synthetic_observations or [])]
        synth_w = _clamp(float(self.cfg.synthetic_weight), 0.0, 1.0)
        for obs in synth:
            obs.weight = max(1e-6, synth_w)
            obs.synthetic = True

        all_obs = real + synth
        self.n_observations = len(real)
        self.n_censored = int(sum(1 for o in real if o.censored))
        self.synthetic_observations = len(synth)
        self.km.fit(all_obs)
        self.active_tier = "kaplan_meier"
        self.fitted = len(real) >= max(1, int(self.cfg.min_observations))

        use_cox = (
            self.model_tier == "cox"
            and self.fitted
            and len(all_obs) >= max(2, int(self.cfg.min_observations))
        )
        if use_cox:
            if self.cox.fit(all_obs):
                self.active_tier = "cox"
            else:
                self.active_tier = "kaplan_meier"
        self.last_retrain_ts = float(time.time())
        return bool(self.fitted)

    def predict(self, obs: FillObservation) -> SurvivalPrediction:
        normalized = obs.normalized()
        horizons = sorted(parse_horizons(self.cfg.horizons))
        if not horizons:
            horizons = [1800, 3600, 14400]
        if len(horizons) < 3:
            horizons = sorted(set(horizons + [1800, 3600, 14400]))

        if not self.fitted:
            return SurvivalPrediction(
                p_fill_30m=0.5,
                p_fill_1h=0.5,
                p_fill_4h=0.5,
                median_remaining=float("inf"),
                hazard_ratio=1.0,
                model_tier="kaplan_meier",
                confidence=0.0,
            )

        if self.active_tier == "cox" and self.cox.fitted:
            probs, hazard_ratio = self.cox.predict_probs(normalized, horizons)
            p30 = probs.get(1800, probs.get(horizons[0], 0.5))
            p1h = probs.get(3600, probs.get(horizons[min(1, len(horizons) - 1)], p30))
            p4h = probs.get(14400, probs.get(horizons[-1], p1h))
            median = self.cox.median_time(normalized)
            confidence = _clamp(self.n_observations / max(1.0, float(self.cfg.min_observations * 2)), 0.0, 1.0)
            return SurvivalPrediction(
                p_fill_30m=float(p30),
                p_fill_1h=float(p1h),
                p_fill_4h=float(p4h),
                median_remaining=float(median),
                hazard_ratio=float(hazard_ratio),
                model_tier="cox",
                confidence=float(confidence),
            )

        probs, median, confidence = self.km.predict(
            regime_at_entry=normalized.regime_at_entry,
            side=normalized.side,
            horizons=horizons,
        )
        p30 = probs.get(1800, probs.get(horizons[0], 0.5))
        p1h = probs.get(3600, probs.get(horizons[min(1, len(horizons) - 1)], p30))
        p4h = probs.get(14400, probs.get(horizons[-1], p1h))
        return SurvivalPrediction(
            p_fill_30m=float(p30),
            p_fill_1h=float(p1h),
            p_fill_4h=float(p4h),
            median_remaining=float(median),
            hazard_ratio=1.0,
            model_tier="kaplan_meier",
            confidence=float(confidence),
        )

    def status_payload(self, enabled: bool) -> dict[str, Any]:
        return {
            "enabled": bool(enabled),
            "model_tier": str(self.active_tier),
            "n_observations": int(self.n_observations),
            "n_censored": int(self.n_censored),
            "last_retrain_ts": float(self.last_retrain_ts),
            "strata_counts": {
                key: int(val)
                for key, val in sorted(self.km.strata_counts.items())
                if key != "aggregate"
            },
            "synthetic_observations": int(self.synthetic_observations),
            "cox_coefficients": self.cox.coefficients_dict() if self.active_tier == "cox" else {},
        }

    def snapshot_state(self) -> dict[str, Any]:
        km_curves = {}
        for key, curve in self.km.curves.items():
            km_curves[key] = {
                "event_times": [float(x) for x in curve.event_times.tolist()],
                "survival": [float(x) for x in curve.survival.tolist()],
                "n_observations": int(curve.n_observations),
                "n_events": int(curve.n_events),
                "n_censored": int(curve.n_censored),
                "weighted_observations": float(curve.weighted_observations),
            }
        return {
            "cfg": {
                "min_observations": int(self.cfg.min_observations),
                "min_per_stratum": int(self.cfg.min_per_stratum),
                "synthetic_weight": float(self.cfg.synthetic_weight),
                "horizons": [int(x) for x in self.cfg.horizons],
            },
            "model_tier": str(self.model_tier),
            "active_tier": str(self.active_tier),
            "last_retrain_ts": float(self.last_retrain_ts),
            "n_observations": int(self.n_observations),
            "n_censored": int(self.n_censored),
            "synthetic_observations": int(self.synthetic_observations),
            "fitted": bool(self.fitted),
            "strata_counts": {k: int(v) for k, v in self.km.strata_counts.items()},
            "km_curves": km_curves,
            "cox": {
                "fitted": bool(self.cox.fitted),
                "coef": [float(x) for x in (self.cox.coef_.tolist() if self.cox.coef_ is not None else [])],
                "mean": [float(x) for x in (self.cox.mean_.tolist() if self.cox.mean_ is not None else [])],
                "std": [float(x) for x in (self.cox.std_.tolist() if self.cox.std_ is not None else [])],
                "event_times": [
                    float(x) for x in (self.cox.event_times_.tolist() if self.cox.event_times_ is not None else [])
                ],
                "base_cumhaz": [
                    float(x) for x in (self.cox.base_cumhaz_.tolist() if self.cox.base_cumhaz_ is not None else [])
                ],
            },
        }

    def restore_state(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        self.model_tier = str(payload.get("model_tier", self.model_tier) or self.model_tier).strip().lower()
        if self.model_tier not in {"kaplan_meier", "cox"}:
            self.model_tier = "kaplan_meier"
        self.active_tier = str(payload.get("active_tier", self.active_tier) or self.active_tier).strip().lower()
        if self.active_tier not in {"kaplan_meier", "cox"}:
            self.active_tier = "kaplan_meier"
        self.last_retrain_ts = max(0.0, float(payload.get("last_retrain_ts", self.last_retrain_ts) or 0.0))
        self.n_observations = max(0, int(payload.get("n_observations", self.n_observations) or 0))
        self.n_censored = max(0, int(payload.get("n_censored", self.n_censored) or 0))
        self.synthetic_observations = max(
            0, int(payload.get("synthetic_observations", self.synthetic_observations) or 0)
        )
        self.fitted = bool(payload.get("fitted", self.fitted))
        raw_counts = payload.get("strata_counts", {})
        if isinstance(raw_counts, dict):
            self.km.strata_counts = {str(k): max(0, int(v)) for k, v in raw_counts.items()}

        self.km.curves = {}
        raw_curves = payload.get("km_curves", {})
        if isinstance(raw_curves, dict):
            for key, row in raw_curves.items():
                if not isinstance(row, dict):
                    continue
                try:
                    event_times = np.asarray(row.get("event_times", []), dtype=float)
                    survival = np.asarray(row.get("survival", []), dtype=float)
                except Exception:
                    continue
                if event_times.size != survival.size:
                    continue
                self.km.curves[str(key)] = _KMSurvivalCurve(
                    event_times=event_times,
                    survival=survival,
                    n_observations=max(0, int(row.get("n_observations", 0) or 0)),
                    n_events=max(0, int(row.get("n_events", 0) or 0)),
                    n_censored=max(0, int(row.get("n_censored", 0) or 0)),
                    weighted_observations=max(0.0, float(row.get("weighted_observations", 0.0) or 0.0)),
                )

        raw_cox = payload.get("cox", {})
        if isinstance(raw_cox, dict) and bool(raw_cox.get("fitted", False)):
            try:
                self.cox.coef_ = np.asarray(raw_cox.get("coef", []), dtype=float)
                self.cox.mean_ = np.asarray(raw_cox.get("mean", []), dtype=float)
                self.cox.std_ = np.asarray(raw_cox.get("std", []), dtype=float)
                self.cox.event_times_ = np.asarray(raw_cox.get("event_times", []), dtype=float)
                self.cox.base_cumhaz_ = np.asarray(raw_cox.get("base_cumhaz", []), dtype=float)
                n = min(
                    self.cox.coef_.size,
                    self.cox.mean_.size,
                    self.cox.std_.size,
                )
                if n <= 0 or self.cox.event_times_.size == 0 or self.cox.base_cumhaz_.size == 0:
                    self.cox.fitted = False
                else:
                    self.cox.coef_ = self.cox.coef_[:n]
                    self.cox.mean_ = self.cox.mean_[:n]
                    self.cox.std_ = self.cox.std_[:n]
                    self.cox.fitted = True
            except Exception:
                self.cox.fitted = False
        else:
            self.cox.fitted = False

    @staticmethod
    def generate_synthetic_observations(
        *,
        n_paths: int = 5000,
        weight: float = 0.3,
    ) -> list[FillObservation]:
        """
        Lightweight synthetic generator that guarantees all 6 regime x side strata.
        """
        rng = np.random.default_rng(42)
        n_total = max(6, int(n_paths))
        base: list[FillObservation] = []
        strata = [(r, s) for r in (0, 1, 2) for s in ("A", "B")]
        per = max(1, n_total // len(strata))
        for regime, side in strata:
            for _ in range(per):
                if side == "A":
                    mean_dur = 5400.0 if regime == 2 else 3600.0
                else:
                    mean_dur = 2400.0 if regime == 2 else 4200.0
                duration = max(60.0, float(rng.normal(mean_dur, mean_dur * 0.25)))
                censored = bool(rng.uniform(0.0, 1.0) < 0.10)
                p1 = [0.1, 0.2, 0.7] if regime == 2 else [0.6, 0.3, 0.1] if regime == 0 else [0.2, 0.6, 0.2]
                p15 = list(p1)
                p60 = list(p1)
                distance = max(0.01, float(rng.uniform(0.05, 1.2)))
                obs = FillObservation(
                    duration_sec=duration,
                    censored=censored,
                    regime_at_entry=regime,
                    regime_at_exit=None if censored else regime,
                    side=side,
                    distance_pct=distance,
                    posterior_1m=p1,
                    posterior_15m=p15,
                    posterior_1h=p60,
                    entropy_at_entry=0.20 if regime != 1 else 0.75,
                    p_switch_at_entry=0.05 if regime != 1 else 0.12,
                    fill_imbalance=float(rng.uniform(-0.5, 0.5)),
                    congestion_ratio=float(rng.uniform(0.0, 0.6)),
                    weight=max(1e-6, float(weight)),
                    synthetic=True,
                )
                base.append(obs.normalized())
        return base
