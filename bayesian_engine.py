"""
bayesian_engine.py

Belief-state math and continuous action knobs for the Bayesian intelligence stack.
All utilities are pure-Python + numpy and safe to call even when inputs are partial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
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


@dataclass
class BeliefState:
    enabled: bool = False
    posterior_1m: list[float] = field(default_factory=lambda: [0.0, 1.0, 0.0])
    posterior_15m: list[float] = field(default_factory=lambda: [0.0, 1.0, 0.0])
    posterior_1h: list[float] = field(default_factory=lambda: [0.0, 1.0, 0.0])
    entropy_1m: float = 0.0
    entropy_15m: float = 0.0
    entropy_1h: float = 0.0
    entropy_consensus: float = 0.0
    confidence_score: float = 1.0
    p_switch_1m: float = 0.0
    p_switch_15m: float = 0.0
    p_switch_1h: float = 0.0
    p_switch_consensus: float = 0.0
    direction_score: float = 0.0
    boundary_risk: str = "low"
    posterior_consensus: list[float] = field(default_factory=lambda: [0.0, 1.0, 0.0])

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "posterior_1m": [round(float(x), 6) for x in self.posterior_1m],
            "posterior_15m": [round(float(x), 6) for x in self.posterior_15m],
            "posterior_1h": [round(float(x), 6) for x in self.posterior_1h],
            "entropy_1m": round(float(self.entropy_1m), 6),
            "entropy_15m": round(float(self.entropy_15m), 6),
            "entropy_1h": round(float(self.entropy_1h), 6),
            "entropy_consensus": round(float(self.entropy_consensus), 6),
            "confidence_score": round(float(self.confidence_score), 6),
            "p_switch_1m": round(float(self.p_switch_1m), 6),
            "p_switch_15m": round(float(self.p_switch_15m), 6),
            "p_switch_1h": round(float(self.p_switch_1h), 6),
            "p_switch_consensus": round(float(self.p_switch_consensus), 6),
            "direction_score": round(float(self.direction_score), 6),
            "boundary_risk": str(self.boundary_risk),
        }


@dataclass
class ActionKnobs:
    enabled: bool = False
    aggression: float = 1.0
    spacing_mult: float = 1.0
    spacing_a: float = 1.0
    spacing_b: float = 1.0
    cadence_mult: float = 1.0
    suppression_strength: float = 0.0
    derived_tier: int = 0
    derived_tier_label: str = "symmetric"

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "aggression": round(float(self.aggression), 6),
            "spacing_mult": round(float(self.spacing_mult), 6),
            "spacing_a": round(float(self.spacing_a), 6),
            "spacing_b": round(float(self.spacing_b), 6),
            "cadence_mult": round(float(self.cadence_mult), 6),
            "suppression_strength": round(float(self.suppression_strength), 6),
            "derived_tier": int(self.derived_tier),
            "derived_tier_label": str(self.derived_tier_label),
        }


@dataclass
class ManifoldScoreComponents:
    regime_clarity: float = 0.0
    regime_stability: float = 0.0
    throughput_efficiency: float = 0.0
    signal_coherence: float = 0.0

    def to_status_dict(self) -> dict[str, float]:
        return {
            "regime_clarity": round(float(self.regime_clarity), 6),
            "regime_stability": round(float(self.regime_stability), 6),
            "throughput_efficiency": round(float(self.throughput_efficiency), 6),
            "signal_coherence": round(float(self.signal_coherence), 6),
        }


@dataclass
class ManifoldScore:
    enabled: bool = False
    mts: float = 0.0
    band: str = "disabled"
    band_color: str = "#6c757d"
    components: ManifoldScoreComponents = field(default_factory=ManifoldScoreComponents)
    component_details: dict[str, float] = field(default_factory=dict)
    fisher_score: float = 0.0
    kernel_enabled: bool = False
    kernel_samples: int = 0
    kernel_score: float | None = None
    kernel_blend_alpha: float = 0.0

    def to_status_dict(self) -> dict[str, Any]:
        if not bool(self.enabled):
            return {
                "enabled": False,
                "mts": 0.0,
                "band": "disabled",
                "band_color": "#6c757d",
                "components": ManifoldScoreComponents().to_status_dict(),
                "component_details": {},
                "kernel_memory": {
                    "enabled": False,
                    "samples": 0,
                    "score": None,
                    "blend_alpha": 0.0,
                },
            }

        return {
            "enabled": True,
            "mts": round(float(self.mts), 6),
            "band": str(self.band),
            "band_color": str(self.band_color),
            "components": self.components.to_status_dict(),
            "component_details": {
                str(k): round(float(v), 6)
                for k, v in self.component_details.items()
            },
            "fisher_score": round(float(self.fisher_score), 6),
            "kernel_memory": {
                "enabled": bool(self.kernel_enabled),
                "samples": int(max(0, self.kernel_samples)),
                "score": (round(float(self.kernel_score), 6) if self.kernel_score is not None else None),
                "blend_alpha": round(float(self.kernel_blend_alpha), 6),
            },
        }


def _safe_weights(raw: Any, default: list[float]) -> list[float]:
    out: list[float] = []
    if isinstance(raw, (list, tuple)):
        for i in range(len(default)):
            if i >= len(raw):
                break
            out.append(max(0.0, _safe_float(raw[i], 0.0)))
    if len(out) != len(default):
        out = [max(0.0, float(v)) for v in default]
    total = sum(out)
    if total <= 1e-12:
        out = [max(0.0, float(v)) for v in default]
        total = sum(out)
    if total <= 1e-12:
        n = max(1, len(default))
        return [1.0 / n for _ in range(n)]
    return [float(v) / float(total) for v in out]


def manifold_score_band(score: float) -> tuple[str, str]:
    mts = _clamp(float(score), 0.0, 1.0)
    if mts >= 0.80:
        return "optimal", "#5cb85c"
    if mts >= 0.60:
        return "favorable", "#20c997"
    if mts >= 0.40:
        return "cautious", "#f0ad4e"
    if mts >= 0.20:
        return "defensive", "#fd7e14"
    return "hostile", "#d9534f"


def compute_regime_clarity(
    *,
    posterior_1m: Any,
    posterior_15m: Any,
    posterior_1h: Any,
    weights: Any = None,
) -> tuple[float, dict[str, float]]:
    def _clarity_from_posterior(raw: Any) -> float:
        p = _safe_triplet(raw)
        kl = 0.0
        uniform = 1.0 / 3.0
        for pi in p:
            if pi <= 0.0:
                continue
            kl += float(pi) * math.log(float(pi) / uniform)
        return _clamp(1.0 - math.exp(-kl), 0.0, 1.0)

    clarity_1m = _clarity_from_posterior(posterior_1m)
    clarity_15m = _clarity_from_posterior(posterior_15m)
    clarity_1h = _clarity_from_posterior(posterior_1h)
    w1, w15, w60 = _safe_weights(weights, [0.2, 0.5, 0.3])
    score = _clamp((w1 * clarity_1m) + (w15 * clarity_15m) + (w60 * clarity_1h), 0.0, 1.0)
    return score, {
        "clarity_1m": float(clarity_1m),
        "clarity_15m": float(clarity_15m),
        "clarity_1h": float(clarity_1h),
    }


def compute_regime_stability(
    *,
    p_switch_1m: float,
    p_switch_15m: float,
    p_switch_1h: float,
    bocpd_change_prob: float,
    switch_weights: Any = None,
) -> tuple[float, dict[str, float]]:
    w1, w15, w60 = _safe_weights(switch_weights, [0.2, 0.5, 0.3])
    ps1 = _clamp(float(p_switch_1m), 0.0, 1.0)
    ps15 = _clamp(float(p_switch_15m), 0.0, 1.0)
    ps60 = _clamp(float(p_switch_1h), 0.0, 1.0)
    switch_risk = _clamp(max(ps1 * w1, ps15 * w15, ps60 * w60), 0.0, 1.0)
    bocpd_risk = _clamp(float(bocpd_change_prob), 0.0, 1.0)
    score = _clamp((1.0 - switch_risk) * (1.0 - bocpd_risk), 0.0, 1.0)
    return score, {
        "p_switch_risk": float(switch_risk),
        "bocpd_risk": float(bocpd_risk),
    }


def compute_throughput_efficiency(
    *,
    throughput_multiplier: float,
    age_pressure: float,
    stuck_capital_pct: float,
) -> tuple[float, dict[str, float]]:
    tp_mult = _clamp(float(throughput_multiplier), 0.0, 2.0)
    age = _clamp(float(age_pressure), 0.0, 1.0)
    stuck = _clamp(float(stuck_capital_pct), 0.0, 100.0)
    age_drag = _clamp(1.0 - (age * stuck / 100.0), 0.0, 1.0)
    score = _clamp(tp_mult * age_drag, 0.0, 1.0)
    return score, {
        "tp_mult": float(tp_mult),
        "age_drag": float(age_drag),
    }


def compute_signal_coherence(
    *,
    entropy_consensus: float,
    direction_score: float,
    bocpd_run_length: float,
    weights: Any = None,
    bocpd_run_norm_window: float = 50.0,
) -> tuple[float, dict[str, float]]:
    agreement = _clamp(1.0 - float(entropy_consensus), 0.0, 1.0)
    directional_clarity = _clamp(abs(float(direction_score)), 0.0, 1.0)
    denom = max(1.0, float(bocpd_run_norm_window))
    bocpd_run_norm = _clamp(float(bocpd_run_length) / denom, 0.0, 1.0)
    wa, wd, wb = _safe_weights(weights, [0.5, 0.25, 0.25])
    score = _clamp(
        (agreement * wa) + (directional_clarity * wd) + (bocpd_run_norm * wb),
        0.0,
        1.0,
    )
    return score, {
        "agreement": float(agreement),
        "directional_clarity": float(directional_clarity),
        "bocpd_run_norm": float(bocpd_run_norm),
    }


def compute_manifold_score(
    *,
    posterior_1m: Any,
    posterior_15m: Any,
    posterior_1h: Any,
    p_switch_1m: float,
    p_switch_15m: float,
    p_switch_1h: float,
    bocpd_change_prob: float,
    bocpd_run_length: float,
    throughput_multiplier: float,
    age_pressure: float,
    stuck_capital_pct: float,
    entropy_consensus: float,
    direction_score: float,
    clarity_weights: Any = None,
    stability_switch_weights: Any = None,
    coherence_weights: Any = None,
    enabled: bool = True,
    kernel_enabled: bool = False,
    kernel_samples: int = 0,
    kernel_score: float | None = None,
    kernel_min_samples: int = 200,
    kernel_alpha_max: float = 0.5,
) -> ManifoldScore:
    if not bool(enabled):
        return ManifoldScore(enabled=False)

    rc, rc_details = compute_regime_clarity(
        posterior_1m=posterior_1m,
        posterior_15m=posterior_15m,
        posterior_1h=posterior_1h,
        weights=clarity_weights,
    )
    rs, rs_details = compute_regime_stability(
        p_switch_1m=p_switch_1m,
        p_switch_15m=p_switch_15m,
        p_switch_1h=p_switch_1h,
        bocpd_change_prob=bocpd_change_prob,
        switch_weights=stability_switch_weights,
    )
    te, te_details = compute_throughput_efficiency(
        throughput_multiplier=throughput_multiplier,
        age_pressure=age_pressure,
        stuck_capital_pct=stuck_capital_pct,
    )
    sc, sc_details = compute_signal_coherence(
        entropy_consensus=entropy_consensus,
        direction_score=direction_score,
        bocpd_run_length=bocpd_run_length,
        weights=coherence_weights,
    )

    components = ManifoldScoreComponents(
        regime_clarity=float(rc),
        regime_stability=float(rs),
        throughput_efficiency=float(te),
        signal_coherence=float(sc),
    )
    product = float(rc) * float(rs) * float(te) * float(sc)
    fisher = _clamp(pow(product, 0.25) if product > 0.0 else 0.0, 0.0, 1.0)

    kernel_score_clean: float | None = None
    kernel_samples_int = max(0, int(kernel_samples))
    kernel_enabled_flag = bool(kernel_enabled)
    alpha_max = _clamp(float(kernel_alpha_max), 0.0, 1.0)
    min_samples = max(1, int(kernel_min_samples))
    blend_alpha = 0.0
    score = fisher
    if kernel_enabled_flag and kernel_samples_int >= min_samples and kernel_score is not None:
        kernel_score_clean = _clamp(float(kernel_score), 0.0, 1.0)
        ramp = _clamp((kernel_samples_int - min_samples) / float(min_samples), 0.0, 1.0)
        blend_alpha = _clamp(ramp * alpha_max, 0.0, alpha_max)
        score = _clamp((1.0 - blend_alpha) * fisher + blend_alpha * kernel_score_clean, 0.0, 1.0)

    band, band_color = manifold_score_band(score)
    component_details: dict[str, float] = {}
    component_details.update(rc_details)
    component_details.update(rs_details)
    component_details.update(te_details)
    component_details.update(sc_details)

    return ManifoldScore(
        enabled=True,
        mts=float(score),
        band=str(band),
        band_color=str(band_color),
        components=components,
        component_details=component_details,
        fisher_score=float(fisher),
        kernel_enabled=kernel_enabled_flag,
        kernel_samples=kernel_samples_int,
        kernel_score=kernel_score_clean,
        kernel_blend_alpha=float(blend_alpha),
    )


@dataclass
class TradeBeliefState:
    position_id: int
    slot_id: int
    trade_id: str
    cycle: int
    entry_regime_posterior: list[float] = field(default_factory=lambda: [0.0] * 9)
    entry_entropy: float = 0.0
    entry_p_switch: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    entry_ts: float = 0.0
    side: str = ""
    current_regime_posterior: list[float] = field(default_factory=lambda: [0.0] * 9)
    current_entropy: float = 0.0
    current_p_switch: float = 0.0
    elapsed_sec: float = 0.0
    distance_from_market_pct: float = 0.0
    p_fill_30m: float = 0.5
    p_fill_1h: float = 0.5
    p_fill_4h: float = 0.5
    median_remaining_sec: float = 0.0
    regime_agreement: float = 1.0
    expected_value: float = 0.0
    ev_trend: str = "stable"
    recommended_action: str = "hold"
    action_confidence: float = 0.0

    def to_badge_dict(self) -> dict[str, Any]:
        return {
            "position_id": int(self.position_id),
            "slot_id": int(self.slot_id),
            "trade_id": str(self.trade_id),
            "cycle": int(self.cycle),
            "p_fill_1h": round(float(self.p_fill_1h), 6),
            "expected_value": round(float(self.expected_value), 8),
            "regime_agreement": round(float(self.regime_agreement), 6),
            "recommended_action": str(self.recommended_action),
            "action_confidence": round(float(self.action_confidence), 6),
            "elapsed_sec": round(float(self.elapsed_sec), 3),
            "distance_from_market_pct": round(float(self.distance_from_market_pct), 6),
        }


def compute_entropy(posterior: Any) -> float:
    """
    Return normalized Shannon entropy in [0, 1].
    """
    p = np.asarray(_safe_triplet(posterior), dtype=float)
    nz = p[p > 0.0]
    if nz.size == 0:
        return 0.0
    h = -float(np.sum(nz * np.log(nz)))
    hmax = math.log(float(len(p))) if len(p) > 1 else 1.0
    if hmax <= 0.0:
        return 0.0
    return _clamp(h / hmax, 0.0, 1.0)


def compute_p_switch(posterior: Any, transmat: Any) -> float:
    """
    p_switch = 1 - Σ π(i) * A[i, i]
    """
    p = np.asarray(_safe_triplet(posterior), dtype=float)
    try:
        a = np.asarray(transmat, dtype=float)
    except Exception:
        return 0.0
    if a.ndim != 2:
        return 0.0
    n = min(len(p), int(a.shape[0]), int(a.shape[1]))
    if n <= 0:
        return 0.0
    diag = np.diag(a[:n, :n])
    diag = np.where(np.isfinite(diag), diag, 0.0)
    diag = np.clip(diag, 0.0, 1.0)
    p_use = p[:n]
    p_use = p_use / max(float(p_use.sum()), 1e-12)
    stay_prob = float(np.dot(p_use, diag))
    return _clamp(1.0 - stay_prob, 0.0, 1.0)


def boundary_risk_label(p_switch_consensus: float) -> str:
    ps = _clamp(float(p_switch_consensus), 0.0, 1.0)
    if ps > 0.15:
        return "high"
    if ps >= 0.08:
        return "medium"
    return "low"


def posterior9_from_timeframes(
    posterior_1m: Any,
    posterior_15m: Any,
    posterior_1h: Any,
) -> list[float]:
    return _safe_triplet(posterior_1m) + _safe_triplet(posterior_15m) + _safe_triplet(posterior_1h)


def cosine_similarity(entry_vec: Any, current_vec: Any) -> float:
    try:
        v1 = np.asarray(entry_vec, dtype=float).reshape(-1)
        v2 = np.asarray(current_vec, dtype=float).reshape(-1)
    except Exception:
        return 0.0
    if v1.size == 0 or v2.size == 0:
        return 0.0
    n = min(v1.size, v2.size)
    v1 = v1[:n]
    v2 = v2[:n]
    n1 = float(np.linalg.norm(v1))
    n2 = float(np.linalg.norm(v2))
    if n1 <= 1e-12 or n2 <= 1e-12:
        return 0.0
    return _clamp(float(np.dot(v1, v2) / (n1 * n2)), -1.0, 1.0)


def expected_value(
    *,
    p_fill: float,
    profit_if_fill: float,
    opportunity_cost_per_hour: float,
    elapsed_sec: float,
) -> float:
    p = _clamp(float(p_fill), 0.0, 1.0)
    profit = float(profit_if_fill)
    elapsed_h = max(0.0, float(elapsed_sec) / 3600.0)
    opp = max(0.0, float(opportunity_cost_per_hour)) * elapsed_h
    return (p * profit) - ((1.0 - p) * opp)


def ev_trend(ev_history: list[float], window: int = 3) -> str:
    n = max(2, int(window))
    if len(ev_history) < n:
        return "stable"
    tail = [float(x) for x in ev_history[-n:]]
    if all(tail[i] < tail[i + 1] - 1e-12 for i in range(len(tail) - 1)):
        return "rising"
    if all(tail[i] > tail[i + 1] + 1e-12 for i in range(len(tail) - 1)):
        return "falling"
    return "stable"


def build_belief_state(
    *,
    posterior_1m: Any,
    posterior_15m: Any,
    posterior_1h: Any,
    transmat_1m: Any = None,
    transmat_15m: Any = None,
    transmat_1h: Any = None,
    weight_1m: float = 0.3,
    weight_15m: float = 0.4,
    weight_1h: float = 0.3,
    enabled: bool = True,
) -> BeliefState:
    p1 = np.asarray(_safe_triplet(posterior_1m), dtype=float)
    p15 = np.asarray(_safe_triplet(posterior_15m), dtype=float)
    p60 = np.asarray(_safe_triplet(posterior_1h), dtype=float)

    w = np.asarray(
        [
            max(0.0, float(weight_1m)),
            max(0.0, float(weight_15m)),
            max(0.0, float(weight_1h)),
        ],
        dtype=float,
    )
    if float(w.sum()) <= 1e-12:
        w = np.asarray([0.3, 0.4, 0.3], dtype=float)
    w = w / float(w.sum())

    consensus = (w[0] * p1) + (w[1] * p15) + (w[2] * p60)
    consensus = consensus / max(float(consensus.sum()), 1e-12)

    e1 = compute_entropy(p1.tolist())
    e15 = compute_entropy(p15.tolist())
    e60 = compute_entropy(p60.tolist())
    e_cons = compute_entropy(consensus.tolist())

    ps1 = compute_p_switch(p1.tolist(), transmat_1m)
    ps15 = compute_p_switch(p15.tolist(), transmat_15m)
    ps60 = compute_p_switch(p60.tolist(), transmat_1h)
    ps_cons = (w[0] * ps1) + (w[1] * ps15) + (w[2] * ps60)

    direction = _clamp(float(consensus[2] - consensus[0]), -1.0, 1.0)
    confidence = _clamp(1.0 - e_cons, 0.0, 1.0)
    boundary = boundary_risk_label(ps_cons)

    return BeliefState(
        enabled=bool(enabled),
        posterior_1m=p1.tolist(),
        posterior_15m=p15.tolist(),
        posterior_1h=p60.tolist(),
        entropy_1m=float(e1),
        entropy_15m=float(e15),
        entropy_1h=float(e60),
        entropy_consensus=float(e_cons),
        confidence_score=float(confidence),
        p_switch_1m=float(ps1),
        p_switch_15m=float(ps15),
        p_switch_1h=float(ps60),
        p_switch_consensus=float(ps_cons),
        direction_score=float(direction),
        boundary_risk=str(boundary),
        posterior_consensus=consensus.tolist(),
    )


def derive_tier_from_knobs(
    suppression_strength: float,
    aggression: float,
) -> tuple[int, str]:
    sup = _clamp(float(suppression_strength), 0.0, 1.0)
    agg = float(aggression)
    if sup > 0.8:
        return 2, "directional"
    if sup > 0.2 or abs(agg - 1.0) > 1e-6:
        return 1, "biased"
    return 0, "symmetric"


def compute_action_knobs(
    *,
    belief_state: BeliefState,
    volatility_score: float,
    congestion_score: float,
    capacity_band: str,
    cfg: dict[str, Any],
    enabled: bool,
) -> ActionKnobs:
    if not bool(enabled):
        return ActionKnobs(enabled=False)

    direction_score = _clamp(float(belief_state.direction_score), -1.0, 1.0)
    confidence_score = _clamp(float(belief_state.confidence_score), 0.0, 1.0)
    boundary_score = _clamp(float(belief_state.p_switch_consensus), 0.0, 1.0)
    entropy = _clamp(float(belief_state.entropy_consensus), 0.0, 1.0)
    vol = max(0.0, float(volatility_score))
    congestion = _clamp(float(congestion_score), 0.0, 1.0)

    direction_boost = _safe_float(cfg.get("KNOB_AGGRESSION_DIRECTION", 0.5), 0.5) * abs(direction_score) * confidence_score
    boundary_damp = 1.0 - (_safe_float(cfg.get("KNOB_AGGRESSION_BOUNDARY", 0.3), 0.3) * boundary_score)
    congestion_damp = 1.0 - (_safe_float(cfg.get("KNOB_AGGRESSION_CONGESTION", 0.5), 0.5) * congestion)
    aggression_raw = (1.0 + direction_boost) * max(0.0, boundary_damp) * max(0.0, congestion_damp)
    aggression = _clamp(
        aggression_raw,
        _safe_float(cfg.get("KNOB_AGGRESSION_FLOOR", 0.5), 0.5),
        _safe_float(cfg.get("KNOB_AGGRESSION_CEILING", 1.5), 1.5),
    )

    vol_stretch = _safe_float(cfg.get("KNOB_SPACING_VOLATILITY", 0.3), 0.3) * max(0.0, vol - 1.0)
    boundary_stretch = _safe_float(cfg.get("KNOB_SPACING_BOUNDARY", 0.2), 0.2) * boundary_score
    spacing_mult = _clamp(
        1.0 + vol_stretch + boundary_stretch,
        _safe_float(cfg.get("KNOB_SPACING_FLOOR", 0.8), 0.8),
        _safe_float(cfg.get("KNOB_SPACING_CEILING", 1.5), 1.5),
    )
    asym = max(0.0, _safe_float(cfg.get("KNOB_ASYMMETRY", 0.3), 0.3))
    if direction_score > 0:
        spacing_a = spacing_mult * (1.0 + asym * abs(direction_score))
        spacing_b = spacing_mult * (1.0 - asym * abs(direction_score))
    elif direction_score < 0:
        spacing_a = spacing_mult * (1.0 - asym * abs(direction_score))
        spacing_b = spacing_mult * (1.0 + asym * abs(direction_score))
    else:
        spacing_a = spacing_mult
        spacing_b = spacing_mult
    spacing_floor = _safe_float(cfg.get("KNOB_SPACING_FLOOR", 0.8), 0.8)
    spacing_ceil = _safe_float(cfg.get("KNOB_SPACING_CEILING", 1.5), 1.5)
    spacing_a = _clamp(spacing_a, spacing_floor, spacing_ceil)
    spacing_b = _clamp(spacing_b, spacing_floor, spacing_ceil)

    cadence = 1.0 - (
        _safe_float(cfg.get("KNOB_CADENCE_BOUNDARY", 0.5), 0.5) * boundary_score
        + _safe_float(cfg.get("KNOB_CADENCE_ENTROPY", 0.3), 0.3) * entropy
    )
    cadence_mult = _clamp(cadence, _safe_float(cfg.get("KNOB_CADENCE_FLOOR", 0.3), 0.3), 1.0)

    suppress_dir_floor = _safe_float(cfg.get("KNOB_SUPPRESS_DIRECTION_FLOOR", 0.3), 0.3)
    suppress_scale = max(1e-9, _safe_float(cfg.get("KNOB_SUPPRESS_SCALE", 0.5), 0.5))
    suppression = ((abs(direction_score) - suppress_dir_floor) * confidence_score) / suppress_scale
    suppression_strength = _clamp(suppression, 0.0, 1.0)
    if str(capacity_band or "").strip().lower() == "stop":
        suppression_strength = 0.0

    derived_tier, derived_tier_label = derive_tier_from_knobs(suppression_strength, aggression)
    return ActionKnobs(
        enabled=True,
        aggression=float(aggression),
        spacing_mult=float(spacing_mult),
        spacing_a=float(spacing_a),
        spacing_b=float(spacing_b),
        cadence_mult=float(cadence_mult),
        suppression_strength=float(suppression_strength),
        derived_tier=int(derived_tier),
        derived_tier_label=str(derived_tier_label),
    )


def recommend_trade_action(
    *,
    regime_agreement: float,
    confidence_score: float,
    p_fill_30m: float,
    p_fill_1h: float,
    p_fill_4h: float,
    expected_value_usd: float,
    ev_trend_label: str,
    is_s2: bool,
    widen_enabled: bool,
    immediate_reprice_agreement: float,
    immediate_reprice_confidence: float,
    tighten_threshold_pfill: float,
    tighten_threshold_ev: float,
) -> tuple[str, float]:
    agreement = _clamp(float(regime_agreement), -1.0, 1.0)
    confidence = _clamp(float(confidence_score), 0.0, 1.0)
    p30 = _clamp(float(p_fill_30m), 0.0, 1.0)
    p1h = _clamp(float(p_fill_1h), 0.0, 1.0)
    p4h = _clamp(float(p_fill_4h), 0.0, 1.0)
    ev = float(expected_value_usd)
    ev_tr = str(ev_trend_label or "stable").strip().lower()

    if agreement < float(immediate_reprice_agreement) and confidence >= float(immediate_reprice_confidence):
        return "reprice_breakeven", max(0.7, confidence)

    if p1h < float(tighten_threshold_pfill) and ev < float(tighten_threshold_ev):
        return "tighten", max(0.6, 1.0 - p1h)

    if p1h < 0.20 and p4h < 0.35 and ev < 0.0:
        return "tighten", max(0.5, 1.0 - p1h)

    if (
        bool(widen_enabled)
        and not bool(is_s2)
        and agreement > 0.85
        and p1h > 0.70
        and p30 > 0.80
        and ev_tr == "rising"
    ):
        return "widen", _clamp(min(p1h, confidence), 0.0, 1.0)

    if agreement > 0.80 and p1h > 0.50:
        return "hold", max(0.4, min(p1h, confidence))

    return "hold", _clamp(max(p1h, 1.0 - abs(agreement)) * 0.5, 0.0, 1.0)
