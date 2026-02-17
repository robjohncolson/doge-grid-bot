"""
bocpd.py

Bayesian Online Change-Point Detection (BOCPD) with a Normal-Inverse-Gamma
observation model. Pure numpy, no external dependencies.
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


def _safe_scalar_observation(observation: Any) -> float:
    """
    Accept scalar or vector observations. Vectors are reduced by mean.
    """
    try:
        arr = np.asarray(observation, dtype=float).reshape(-1)
    except Exception:
        return 0.0
    if arr.size == 0:
        return 0.0
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return 0.0
    return float(np.mean(arr))


@dataclass
class BOCPDState:
    change_prob: float = 0.0
    run_length_mode: int = 0
    run_length_mode_prob: float = 1.0
    last_update_ts: float = 0.0
    observation_count: int = 0
    alert_active: bool = False
    alert_triggered_at: float = 0.0
    run_length_map: dict[int, float] = field(default_factory=dict)

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "change_prob": round(float(self.change_prob), 6),
            "run_length_mode": int(self.run_length_mode),
            "run_length_mode_prob": round(float(self.run_length_mode_prob), 6),
            "last_update_ts": float(self.last_update_ts),
            "observation_count": int(self.observation_count),
            "alert_active": bool(self.alert_active),
            "alert_triggered_at": float(self.alert_triggered_at),
            "run_length_map": {int(k): round(float(v), 6) for k, v in self.run_length_map.items()},
        }


class BOCPD:
    """
    Online BOCPD detector with truncated run-length distribution.
    """

    def __init__(
        self,
        *,
        expected_run_length: int = 200,
        max_run_length: int = 500,
        alert_threshold: float = 0.30,
        urgent_threshold: float = 0.50,
        prior_mu: float = 0.0,
        prior_kappa: float = 1.0,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
    ) -> None:
        self.expected_run_length = max(2, int(expected_run_length))
        self.max_run_length = max(10, int(max_run_length))
        self.alert_threshold = _clamp(float(alert_threshold), 0.0, 1.0)
        self.urgent_threshold = _clamp(float(urgent_threshold), self.alert_threshold, 1.0)
        self.hazard = 1.0 / float(self.expected_run_length)
        self._prior = (
            float(prior_mu),
            max(1e-9, float(prior_kappa)),
            max(1e-9, float(prior_alpha)),
            max(1e-9, float(prior_beta)),
        )

        # Run-length posterior and NIG parameters per run length.
        self._run_probs = np.asarray([1.0], dtype=float)
        self._mu = np.asarray([self._prior[0]], dtype=float)
        self._kappa = np.asarray([self._prior[1]], dtype=float)
        self._alpha = np.asarray([self._prior[2]], dtype=float)
        self._beta = np.asarray([self._prior[3]], dtype=float)
        self.state = BOCPDState()

    @staticmethod
    def _student_t_logpdf(x: float, mu: float, kappa: float, alpha: float, beta: float) -> float:
        # Predictive Student-t under NIG posterior.
        dof = max(1e-9, 2.0 * float(alpha))
        scale2 = (float(beta) * (float(kappa) + 1.0)) / max(1e-9, float(alpha) * float(kappa))
        scale2 = max(1e-12, float(scale2))
        z = ((float(x) - float(mu)) ** 2) / (dof * scale2)
        return (
            math.lgamma((dof + 1.0) / 2.0)
            - math.lgamma(dof / 2.0)
            - 0.5 * (math.log(dof) + math.log(math.pi) + math.log(scale2))
            - ((dof + 1.0) / 2.0) * math.log1p(z)
        )

    @staticmethod
    def _nig_update(
        x: float,
        mu: np.ndarray,
        kappa: np.ndarray,
        alpha: np.ndarray,
        beta: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        x_arr = np.full_like(mu, float(x), dtype=float)
        kappa_new = kappa + 1.0
        mu_new = (kappa * mu + x_arr) / np.maximum(kappa_new, 1e-12)
        alpha_new = alpha + 0.5
        beta_new = beta + (kappa * (x_arr - mu) ** 2) / np.maximum(2.0 * kappa_new, 1e-12)
        return mu_new, kappa_new, alpha_new, beta_new

    def update(self, observation: Any, now_ts: float | None = None) -> BOCPDState:
        x = _safe_scalar_observation(observation)
        now = float(now_ts if now_ts is not None else time.time())

        # Predictive likelihood for each run length.
        log_pred = np.zeros_like(self._run_probs)
        for idx in range(self._run_probs.size):
            log_pred[idx] = self._student_t_logpdf(
                x,
                float(self._mu[idx]),
                float(self._kappa[idx]),
                float(self._alpha[idx]),
                float(self._beta[idx]),
            )

        # Numerically stable joint terms in log-space.
        run_log = np.log(np.maximum(self._run_probs, 1e-300))
        log_h = math.log(max(1e-12, min(1.0 - 1e-12, self.hazard)))
        log_1mh = math.log(max(1e-12, 1.0 - self.hazard))

        # Growth: r -> r+1, Change-point: r -> 0.
        log_growth = run_log + log_pred + log_1mh
        log_cp_terms = run_log + log_pred + log_h
        max_cp = float(np.max(log_cp_terms))
        cp_mass = float(np.exp(log_cp_terms - max_cp).sum()) * math.exp(max_cp)

        # Truncate to configured max run length.
        new_len = min(self.max_run_length + 1, self._run_probs.size + 1)
        new_joint = np.full(new_len, 0.0, dtype=float)
        new_joint[0] = cp_mass
        growth_mass = np.exp(log_growth - float(np.max(log_growth)))
        growth_mass = growth_mass * math.exp(float(np.max(log_growth)))
        growth_keep = min(new_len - 1, growth_mass.size)
        if growth_keep > 0:
            new_joint[1 : 1 + growth_keep] = growth_mass[:growth_keep]

        total = float(new_joint.sum())
        if total <= 1e-300:
            new_joint = np.asarray([1.0], dtype=float)
            total = 1.0
        new_probs = new_joint / total

        # Posterior parameters for next step.
        old_mu = self._mu
        old_kappa = self._kappa
        old_alpha = self._alpha
        old_beta = self._beta
        upd_mu, upd_kappa, upd_alpha, upd_beta = self._nig_update(x, old_mu, old_kappa, old_alpha, old_beta)

        new_mu = np.full(new_probs.size, self._prior[0], dtype=float)
        new_kappa = np.full(new_probs.size, self._prior[1], dtype=float)
        new_alpha = np.full(new_probs.size, self._prior[2], dtype=float)
        new_beta = np.full(new_probs.size, self._prior[3], dtype=float)
        carry = min(new_probs.size - 1, upd_mu.size)
        if carry > 0:
            new_mu[1 : 1 + carry] = upd_mu[:carry]
            new_kappa[1 : 1 + carry] = upd_kappa[:carry]
            new_alpha[1 : 1 + carry] = upd_alpha[:carry]
            new_beta[1 : 1 + carry] = upd_beta[:carry]

        self._run_probs = new_probs
        self._mu = new_mu
        self._kappa = new_kappa
        self._alpha = new_alpha
        self._beta = new_beta

        mode = int(np.argmax(self._run_probs))
        mode_prob = float(self._run_probs[mode]) if self._run_probs.size else 1.0
        # Mass on "young" run lengths captures "a change happened recently"
        # rather than just P(r=0) which converges to the hazard rate.
        young_window = max(3, self.expected_run_length // 20)
        young_end = min(young_window, self._run_probs.size)
        change_prob = float(self._run_probs[:young_end].sum()) if young_end > 0 else 0.0
        obs_count = int(self.state.observation_count) + 1
        alert_active = bool(change_prob >= self.alert_threshold)
        alert_ts = float(self.state.alert_triggered_at)
        if alert_active and alert_ts <= 0.0:
            alert_ts = now
        if not alert_active:
            alert_ts = 0.0

        run_map = {
            int(i): float(p)
            for i, p in enumerate(self._run_probs[: min(32, self._run_probs.size)])
            if float(p) > 1e-9
        }

        self.state = BOCPDState(
            change_prob=_clamp(change_prob, 0.0, 1.0),
            run_length_mode=mode,
            run_length_mode_prob=_clamp(mode_prob, 0.0, 1.0),
            last_update_ts=float(now),
            observation_count=obs_count,
            alert_active=alert_active,
            alert_triggered_at=float(alert_ts),
            run_length_map=run_map,
        )
        return self.state

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "expected_run_length": int(self.expected_run_length),
            "max_run_length": int(self.max_run_length),
            "alert_threshold": float(self.alert_threshold),
            "urgent_threshold": float(self.urgent_threshold),
            "hazard": float(self.hazard),
            "prior": {
                "mu": float(self._prior[0]),
                "kappa": float(self._prior[1]),
                "alpha": float(self._prior[2]),
                "beta": float(self._prior[3]),
            },
            "run_probs": [float(x) for x in self._run_probs.tolist()],
            "mu": [float(x) for x in self._mu.tolist()],
            "kappa": [float(x) for x in self._kappa.tolist()],
            "alpha": [float(x) for x in self._alpha.tolist()],
            "beta": [float(x) for x in self._beta.tolist()],
            "state": self.state.to_status_dict(),
        }

    def restore_state(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        state_raw = payload.get("state", {})
        if isinstance(state_raw, dict):
            self.state = BOCPDState(
                change_prob=_clamp(float(state_raw.get("change_prob", 0.0) or 0.0), 0.0, 1.0),
                run_length_mode=max(0, int(state_raw.get("run_length_mode", 0) or 0)),
                run_length_mode_prob=_clamp(
                    float(state_raw.get("run_length_mode_prob", 1.0) or 1.0), 0.0, 1.0
                ),
                last_update_ts=max(0.0, float(state_raw.get("last_update_ts", 0.0) or 0.0)),
                observation_count=max(0, int(state_raw.get("observation_count", 0) or 0)),
                alert_active=bool(state_raw.get("alert_active", False)),
                alert_triggered_at=max(0.0, float(state_raw.get("alert_triggered_at", 0.0) or 0.0)),
                run_length_map={
                    int(k): float(v)
                    for k, v in (state_raw.get("run_length_map", {}) or {}).items()
                    if float(v) > 0.0
                },
            )

        def _to_arr(name: str, default: np.ndarray) -> np.ndarray:
            raw = payload.get(name)
            if not isinstance(raw, list) or not raw:
                return default
            try:
                arr = np.asarray(raw, dtype=float)
            except Exception:
                return default
            arr = arr[np.isfinite(arr)]
            if arr.size == 0:
                return default
            return arr

        self._run_probs = _to_arr("run_probs", np.asarray([1.0], dtype=float))
        self._run_probs = self._run_probs / max(float(self._run_probs.sum()), 1e-12)
        n = self._run_probs.size

        self._mu = _to_arr("mu", np.full(n, self._prior[0], dtype=float))
        self._kappa = _to_arr("kappa", np.full(n, self._prior[1], dtype=float))
        self._alpha = _to_arr("alpha", np.full(n, self._prior[2], dtype=float))
        self._beta = _to_arr("beta", np.full(n, self._prior[3], dtype=float))

        # Length parity safety.
        n = min(self._run_probs.size, self._mu.size, self._kappa.size, self._alpha.size, self._beta.size)
        self._run_probs = self._run_probs[:n]
        self._mu = self._mu[:n]
        self._kappa = self._kappa[:n]
        self._alpha = self._alpha[:n]
        self._beta = self._beta[:n]
        if n == 0:
            self._run_probs = np.asarray([1.0], dtype=float)
            self._mu = np.asarray([self._prior[0]], dtype=float)
            self._kappa = np.asarray([self._prior[1]], dtype=float)
            self._alpha = np.asarray([self._prior[2]], dtype=float)
            self._beta = np.asarray([self._prior[3]], dtype=float)

