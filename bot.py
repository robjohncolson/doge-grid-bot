"""
DOGE Bot v1 runtime.

Ground-up DOGE/USD slot-based pair state machine runtime:
- DOGE-only (Kraken XDGUSD)
- Supabase as single source of truth
- reducer-driven state transitions
- simplified orphaning (S1 timeout + S2 timeout)
- Telegram commands + dashboard controls
"""

from __future__ import annotations

from collections import deque
import json
import logging
import os
import signal
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import ceil, exp, floor, isfinite, log
from socketserver import ThreadingMixIn
from statistics import median
from types import SimpleNamespace
from typing import Any

import config
import dashboard
import kraken_client
from position_ledger import PositionLedger
from throughput_sizer import ThroughputConfig, ThroughputSizer
import notifier
import ai_advisor
import state_machine as sm
import supabase_store

try:
    import bayesian_engine as _bayesian_engine
except Exception as exc:  # pragma: no cover - environment dependent
    _bayesian_engine = None
    _bayesian_import_error = exc
else:
    _bayesian_import_error = None

try:
    import bocpd as _bocpd
except Exception as exc:  # pragma: no cover - environment dependent
    _bocpd = None
    _bocpd_import_error = exc
else:
    _bocpd_import_error = None

try:
    import survival_model as _survival_model
except Exception as exc:  # pragma: no cover - environment dependent
    _survival_model = None
    _survival_import_error = exc
else:
    _survival_import_error = None


logger = logging.getLogger(__name__)


if _bayesian_engine is None:  # pragma: no cover - fallback path for minimal runtimes
    @dataclass
    class _FallbackBeliefState:
        enabled: bool = False
        posterior_1m: list[float] = None
        posterior_15m: list[float] = None
        posterior_1h: list[float] = None
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
        posterior_consensus: list[float] = None

        def __post_init__(self) -> None:
            if self.posterior_1m is None:
                self.posterior_1m = [0.0, 1.0, 0.0]
            if self.posterior_15m is None:
                self.posterior_15m = [0.0, 1.0, 0.0]
            if self.posterior_1h is None:
                self.posterior_1h = [0.0, 1.0, 0.0]
            if self.posterior_consensus is None:
                self.posterior_consensus = [0.0, 1.0, 0.0]

        def to_status_dict(self) -> dict[str, Any]:
            return {
                "enabled": bool(self.enabled),
                "posterior_1m": [float(x) for x in self.posterior_1m],
                "posterior_15m": [float(x) for x in self.posterior_15m],
                "posterior_1h": [float(x) for x in self.posterior_1h],
                "entropy_1m": float(self.entropy_1m),
                "entropy_15m": float(self.entropy_15m),
                "entropy_1h": float(self.entropy_1h),
                "entropy_consensus": float(self.entropy_consensus),
                "confidence_score": float(self.confidence_score),
                "p_switch_1m": float(self.p_switch_1m),
                "p_switch_15m": float(self.p_switch_15m),
                "p_switch_1h": float(self.p_switch_1h),
                "p_switch_consensus": float(self.p_switch_consensus),
                "direction_score": float(self.direction_score),
                "boundary_risk": str(self.boundary_risk),
            }

    @dataclass
    class _FallbackActionKnobs:
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
                "aggression": float(self.aggression),
                "spacing_mult": float(self.spacing_mult),
                "spacing_a": float(self.spacing_a),
                "spacing_b": float(self.spacing_b),
                "cadence_mult": float(self.cadence_mult),
                "suppression_strength": float(self.suppression_strength),
                "derived_tier": int(self.derived_tier),
                "derived_tier_label": str(self.derived_tier_label),
            }

    @dataclass
    class _FallbackManifoldScoreComponents:
        regime_clarity: float = 0.0
        regime_stability: float = 0.0
        throughput_efficiency: float = 0.0
        signal_coherence: float = 0.0

        def to_status_dict(self) -> dict[str, float]:
            return {
                "regime_clarity": float(self.regime_clarity),
                "regime_stability": float(self.regime_stability),
                "throughput_efficiency": float(self.throughput_efficiency),
                "signal_coherence": float(self.signal_coherence),
            }

    @dataclass
    class _FallbackManifoldScore:
        enabled: bool = False
        mts: float = 0.0
        band: str = "disabled"
        band_color: str = "#6c757d"
        components: _FallbackManifoldScoreComponents = None
        component_details: dict[str, float] = None
        fisher_score: float = 0.0
        kernel_enabled: bool = False
        kernel_samples: int = 0
        kernel_score: float | None = None
        kernel_blend_alpha: float = 0.0

        def __post_init__(self) -> None:
            if self.components is None:
                self.components = _FallbackManifoldScoreComponents()
            if self.component_details is None:
                self.component_details = {}

        def to_status_dict(self) -> dict[str, Any]:
            if not bool(self.enabled):
                return {
                    "enabled": False,
                    "mts": 0.0,
                    "band": "disabled",
                    "band_color": "#6c757d",
                    "components": _FallbackManifoldScoreComponents().to_status_dict(),
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
                "mts": float(self.mts),
                "band": str(self.band),
                "band_color": str(self.band_color),
                "components": self.components.to_status_dict(),
                "component_details": {
                    str(k): float(v)
                    for k, v in (self.component_details or {}).items()
                },
                "fisher_score": float(self.fisher_score),
                "kernel_memory": {
                    "enabled": bool(self.kernel_enabled),
                    "samples": int(max(0, self.kernel_samples)),
                    "score": (float(self.kernel_score) if self.kernel_score is not None else None),
                    "blend_alpha": float(self.kernel_blend_alpha),
                },
            }

    @dataclass
    class _FallbackTradeBeliefState:
        position_id: int
        slot_id: int
        trade_id: str
        cycle: int
        entry_regime_posterior: list[float] = None
        entry_entropy: float = 0.0
        entry_p_switch: float = 0.0
        entry_price: float = 0.0
        exit_price: float = 0.0
        entry_ts: float = 0.0
        side: str = ""
        current_regime_posterior: list[float] = None
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

        def __post_init__(self) -> None:
            if self.entry_regime_posterior is None:
                self.entry_regime_posterior = [0.0] * 9
            if self.current_regime_posterior is None:
                self.current_regime_posterior = [0.0] * 9

        def to_badge_dict(self) -> dict[str, Any]:
            return {
                "position_id": int(self.position_id),
                "slot_id": int(self.slot_id),
                "trade_id": str(self.trade_id),
                "cycle": int(self.cycle),
                "p_fill_1h": float(self.p_fill_1h),
                "expected_value": float(self.expected_value),
                "regime_agreement": float(self.regime_agreement),
                "recommended_action": str(self.recommended_action),
                "action_confidence": float(self.action_confidence),
                "elapsed_sec": float(self.elapsed_sec),
                "distance_from_market_pct": float(self.distance_from_market_pct),
            }

    def _fallback_triplet(raw: Any) -> list[float]:
        if isinstance(raw, dict):
            values = [
                float(raw.get("bearish", 0.0) or 0.0),
                float(raw.get("ranging", 0.0) or 0.0),
                float(raw.get("bullish", 0.0) or 0.0),
            ]
        elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
            values = [float(raw[0] or 0.0), float(raw[1] or 0.0), float(raw[2] or 0.0)]
        else:
            values = [0.0, 1.0, 0.0]
        values = [max(0.0, min(1.0, v)) for v in values]
        total = sum(values)
        if total <= 1e-12:
            return [0.0, 1.0, 0.0]
        return [v / total for v in values]

    def _fallback_entropy(p: list[float]) -> float:
        nz = [x for x in p if x > 0.0]
        if not nz:
            return 0.0
        h = -sum(x * log(x) for x in nz)
        hmax = log(3.0)
        return max(0.0, min(1.0, h / hmax))

    def _fallback_clamp(value: float, lo: float, hi: float) -> float:
        low = min(float(lo), float(hi))
        high = max(float(lo), float(hi))
        return max(low, min(float(value), high))

    def _fallback_safe_float(value: Any, default: float = 0.0) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not isfinite(out):
            return float(default)
        return out

    def _fallback_weights(raw: Any, default: list[float]) -> list[float]:
        out: list[float] = []
        if isinstance(raw, (list, tuple)):
            for i in range(len(default)):
                if i >= len(raw):
                    break
                out.append(max(0.0, _fallback_safe_float(raw[i], 0.0)))
        if len(out) != len(default):
            out = [max(0.0, float(v)) for v in default]
        total = float(sum(out))
        if total <= 1e-12:
            out = [max(0.0, float(v)) for v in default]
            total = float(sum(out))
        if total <= 1e-12:
            n = max(1, len(default))
            return [1.0 / float(n) for _ in range(n)]
        return [float(v) / total for v in out]

    def _fallback_manifold_score_band(score: float) -> tuple[str, str]:
        mts = _fallback_clamp(float(score), 0.0, 1.0)
        if mts >= 0.80:
            return "optimal", "#5cb85c"
        if mts >= 0.60:
            return "favorable", "#20c997"
        if mts >= 0.40:
            return "cautious", "#f0ad4e"
        if mts >= 0.20:
            return "defensive", "#fd7e14"
        return "hostile", "#d9534f"

    class _FallbackBayesianModule:
        BeliefState = _FallbackBeliefState
        ActionKnobs = _FallbackActionKnobs
        ManifoldScoreComponents = _FallbackManifoldScoreComponents
        ManifoldScore = _FallbackManifoldScore
        TradeBeliefState = _FallbackTradeBeliefState

        @staticmethod
        def posterior9_from_timeframes(p1: Any, p15: Any, p60: Any) -> list[float]:
            return _fallback_triplet(p1) + _fallback_triplet(p15) + _fallback_triplet(p60)

        @staticmethod
        def cosine_similarity(entry_vec: Any, current_vec: Any) -> float:
            try:
                v1 = [float(x) for x in list(entry_vec or [])]
                v2 = [float(x) for x in list(current_vec or [])]
            except Exception:
                return 0.0
            n = min(len(v1), len(v2))
            if n <= 0:
                return 0.0
            v1 = v1[:n]
            v2 = v2[:n]
            dot = sum(a * b for a, b in zip(v1, v2))
            n1 = sum(a * a for a in v1) ** 0.5
            n2 = sum(b * b for b in v2) ** 0.5
            if n1 <= 1e-12 or n2 <= 1e-12:
                return 0.0
            value = dot / (n1 * n2)
            return max(-1.0, min(1.0, value))

        @staticmethod
        def expected_value(
            *,
            p_fill: float,
            profit_if_fill: float,
            opportunity_cost_per_hour: float,
            elapsed_sec: float,
        ) -> float:
            p = max(0.0, min(1.0, float(p_fill)))
            elapsed_h = max(0.0, float(elapsed_sec) / 3600.0)
            opp = max(0.0, float(opportunity_cost_per_hour)) * elapsed_h
            return (p * float(profit_if_fill)) - ((1.0 - p) * opp)

        @staticmethod
        def ev_trend(ev_history: list[float], window: int = 3) -> str:
            n = max(2, int(window))
            if len(ev_history) < n:
                return "stable"
            tail = [float(x) for x in ev_history[-n:]]
            if all(tail[i] < tail[i + 1] for i in range(len(tail) - 1)):
                return "rising"
            if all(tail[i] > tail[i + 1] for i in range(len(tail) - 1)):
                return "falling"
            return "stable"

        @staticmethod
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
        ) -> _FallbackBeliefState:
            p1 = _fallback_triplet(posterior_1m)
            p15 = _fallback_triplet(posterior_15m)
            p60 = _fallback_triplet(posterior_1h)
            w1 = max(0.0, float(weight_1m))
            w15 = max(0.0, float(weight_15m))
            w60 = max(0.0, float(weight_1h))
            ws = w1 + w15 + w60
            if ws <= 1e-12:
                w1, w15, w60 = 0.3, 0.4, 0.3
                ws = 1.0
            w1 /= ws
            w15 /= ws
            w60 /= ws
            consensus = [
                w1 * p1[0] + w15 * p15[0] + w60 * p60[0],
                w1 * p1[1] + w15 * p15[1] + w60 * p60[1],
                w1 * p1[2] + w15 * p15[2] + w60 * p60[2],
            ]
            direction = max(-1.0, min(1.0, consensus[2] - consensus[0]))
            entropy_cons = _fallback_entropy(consensus)
            confidence = max(0.0, min(1.0, 1.0 - entropy_cons))
            return _FallbackBeliefState(
                enabled=bool(enabled),
                posterior_1m=p1,
                posterior_15m=p15,
                posterior_1h=p60,
                entropy_1m=_fallback_entropy(p1),
                entropy_15m=_fallback_entropy(p15),
                entropy_1h=_fallback_entropy(p60),
                entropy_consensus=entropy_cons,
                confidence_score=confidence,
                p_switch_1m=0.0,
                p_switch_15m=0.0,
                p_switch_1h=0.0,
                p_switch_consensus=0.0,
                direction_score=direction,
                boundary_risk="low",
                posterior_consensus=consensus,
            )

        @staticmethod
        def compute_action_knobs(
            *,
            belief_state: _FallbackBeliefState,
            volatility_score: float,
            congestion_score: float,
            capacity_band: str,
            cfg: dict[str, Any],
            enabled: bool,
        ) -> _FallbackActionKnobs:
            if not bool(enabled):
                return _FallbackActionKnobs(enabled=False)
            suppression = 0.0 if str(capacity_band or "").strip().lower() == "stop" else 0.0
            return _FallbackActionKnobs(
                enabled=True,
                aggression=1.0,
                spacing_mult=1.0,
                spacing_a=1.0,
                spacing_b=1.0,
                cadence_mult=1.0,
                suppression_strength=suppression,
                derived_tier=0,
                derived_tier_label="symmetric",
            )

        @staticmethod
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
            if (
                float(regime_agreement) < float(immediate_reprice_agreement)
                and float(confidence_score) >= float(immediate_reprice_confidence)
            ):
                return "reprice_breakeven", max(0.7, float(confidence_score))
            if float(p_fill_1h) < float(tighten_threshold_pfill) and float(expected_value_usd) < float(
                tighten_threshold_ev
            ):
                return "tighten", 0.6
            return "hold", 0.5

        @staticmethod
        def manifold_score_band(score: float) -> tuple[str, str]:
            return _fallback_manifold_score_band(score)

        @staticmethod
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
        ) -> _FallbackManifoldScore:
            if not bool(enabled):
                return _FallbackManifoldScore(enabled=False)

            def _clarity(raw: Any) -> float:
                p = _fallback_triplet(raw)
                kl = 0.0
                uniform = 1.0 / 3.0
                for pi in p:
                    if pi <= 0.0:
                        continue
                    kl += float(pi) * log(float(pi) / uniform)
                return _fallback_clamp(1.0 - exp(-kl), 0.0, 1.0)

            clarity_1m = _clarity(posterior_1m)
            clarity_15m = _clarity(posterior_15m)
            clarity_1h = _clarity(posterior_1h)
            cw1, cw15, cw60 = _fallback_weights(clarity_weights, [0.2, 0.5, 0.3])
            rc = _fallback_clamp(
                (cw1 * clarity_1m) + (cw15 * clarity_15m) + (cw60 * clarity_1h),
                0.0,
                1.0,
            )

            sw1, sw15, sw60 = _fallback_weights(stability_switch_weights, [0.2, 0.5, 0.3])
            ps1 = _fallback_clamp(float(p_switch_1m), 0.0, 1.0)
            ps15 = _fallback_clamp(float(p_switch_15m), 0.0, 1.0)
            ps60 = _fallback_clamp(float(p_switch_1h), 0.0, 1.0)
            switch_risk = _fallback_clamp(max(ps1 * sw1, ps15 * sw15, ps60 * sw60), 0.0, 1.0)
            bocpd_risk = _fallback_clamp(float(bocpd_change_prob), 0.0, 1.0)
            rs = _fallback_clamp((1.0 - switch_risk) * (1.0 - bocpd_risk), 0.0, 1.0)

            tp_mult = _fallback_clamp(float(throughput_multiplier), 0.0, 2.0)
            age = _fallback_clamp(float(age_pressure), 0.0, 1.0)
            stuck = _fallback_clamp(float(stuck_capital_pct), 0.0, 100.0)
            age_drag = _fallback_clamp(1.0 - (age * stuck / 100.0), 0.0, 1.0)
            te = _fallback_clamp(tp_mult * age_drag, 0.0, 1.0)

            agreement = _fallback_clamp(1.0 - float(entropy_consensus), 0.0, 1.0)
            directional_clarity = _fallback_clamp(abs(float(direction_score)), 0.0, 1.0)
            bocpd_run_norm = _fallback_clamp(float(bocpd_run_length) / 50.0, 0.0, 1.0)
            wa, wd, wb = _fallback_weights(coherence_weights, [0.5, 0.25, 0.25])
            sc = _fallback_clamp(
                (agreement * wa) + (directional_clarity * wd) + (bocpd_run_norm * wb),
                0.0,
                1.0,
            )

            components = _FallbackManifoldScoreComponents(
                regime_clarity=rc,
                regime_stability=rs,
                throughput_efficiency=te,
                signal_coherence=sc,
            )
            product = float(rc) * float(rs) * float(te) * float(sc)
            fisher = _fallback_clamp(pow(product, 0.25) if product > 0.0 else 0.0, 0.0, 1.0)

            kernel_score_clean: float | None = None
            kernel_samples_int = max(0, int(kernel_samples))
            kernel_enabled_flag = bool(kernel_enabled)
            alpha_max = _fallback_clamp(float(kernel_alpha_max), 0.0, 1.0)
            min_samples = max(1, int(kernel_min_samples))
            blend_alpha = 0.0
            score = fisher
            if kernel_enabled_flag and kernel_samples_int >= min_samples and kernel_score is not None:
                kernel_score_clean = _fallback_clamp(float(kernel_score), 0.0, 1.0)
                ramp = _fallback_clamp((kernel_samples_int - min_samples) / float(min_samples), 0.0, 1.0)
                blend_alpha = _fallback_clamp(ramp * alpha_max, 0.0, alpha_max)
                score = _fallback_clamp(
                    (1.0 - blend_alpha) * fisher + blend_alpha * kernel_score_clean,
                    0.0,
                    1.0,
                )

            band, band_color = _fallback_manifold_score_band(score)
            details = {
                "clarity_1m": float(clarity_1m),
                "clarity_15m": float(clarity_15m),
                "clarity_1h": float(clarity_1h),
                "p_switch_risk": float(switch_risk),
                "bocpd_risk": float(bocpd_risk),
                "tp_mult": float(tp_mult),
                "age_drag": float(age_drag),
                "agreement": float(agreement),
                "directional_clarity": float(directional_clarity),
                "bocpd_run_norm": float(bocpd_run_norm),
            }
            return _FallbackManifoldScore(
                enabled=True,
                mts=float(score),
                band=str(band),
                band_color=str(band_color),
                components=components,
                component_details=details,
                fisher_score=float(fisher),
                kernel_enabled=kernel_enabled_flag,
                kernel_samples=kernel_samples_int,
                kernel_score=kernel_score_clean,
                kernel_blend_alpha=float(blend_alpha),
            )

    bayesian_engine = _FallbackBayesianModule()
else:
    bayesian_engine = _bayesian_engine

if _bocpd is None:  # pragma: no cover - fallback path for minimal runtimes
    @dataclass
    class _FallbackBOCPDState:
        change_prob: float = 0.0
        run_length_mode: int = 0
        run_length_mode_prob: float = 1.0
        last_update_ts: float = 0.0
        observation_count: int = 0
        alert_active: bool = False
        alert_triggered_at: float = 0.0
        run_length_map: dict[int, float] = None

        def __post_init__(self) -> None:
            if self.run_length_map is None:
                self.run_length_map = {}

        def to_status_dict(self) -> dict[str, Any]:
            return {
                "change_prob": float(self.change_prob),
                "run_length_mode": int(self.run_length_mode),
                "run_length_mode_prob": float(self.run_length_mode_prob),
                "last_update_ts": float(self.last_update_ts),
                "observation_count": int(self.observation_count),
                "alert_active": bool(self.alert_active),
                "alert_triggered_at": float(self.alert_triggered_at),
                "run_length_map": {int(k): float(v) for k, v in (self.run_length_map or {}).items()},
            }

    class _FallbackBOCPD:
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
            self.expected_run_length = int(expected_run_length)
            self.max_run_length = int(max_run_length)
            self.alert_threshold = float(alert_threshold)
            self.urgent_threshold = float(urgent_threshold)
            self.state = _FallbackBOCPDState()

        def update(self, observation: Any, now_ts: float | None = None) -> _FallbackBOCPDState:
            ts = float(now_ts if now_ts is not None else _now())
            self.state.last_update_ts = ts
            self.state.observation_count = int(self.state.observation_count) + 1
            self.state.change_prob = 0.0
            self.state.alert_active = False
            self.state.alert_triggered_at = 0.0
            self.state.run_length_mode = min(self.state.observation_count, self.max_run_length)
            self.state.run_length_mode_prob = 1.0
            self.state.run_length_map = {int(self.state.run_length_mode): 1.0}
            return self.state

        def snapshot_state(self) -> dict[str, Any]:
            return {"state": self.state.to_status_dict()}

        def restore_state(self, payload: dict[str, Any]) -> None:
            if not isinstance(payload, dict):
                return
            raw = payload.get("state", payload)
            if not isinstance(raw, dict):
                return
            self.state = _FallbackBOCPDState(
                change_prob=float(raw.get("change_prob", 0.0) or 0.0),
                run_length_mode=max(0, int(raw.get("run_length_mode", 0) or 0)),
                run_length_mode_prob=float(raw.get("run_length_mode_prob", 1.0) or 1.0),
                last_update_ts=float(raw.get("last_update_ts", 0.0) or 0.0),
                observation_count=max(0, int(raw.get("observation_count", 0) or 0)),
                alert_active=bool(raw.get("alert_active", False)),
                alert_triggered_at=float(raw.get("alert_triggered_at", 0.0) or 0.0),
                run_length_map={
                    int(k): float(v)
                    for k, v in (raw.get("run_length_map", {}) or {}).items()
                    if float(v) > 0.0
                },
            )

    class _FallbackBOCPDModule:
        BOCPD = _FallbackBOCPD
        BOCPDState = _FallbackBOCPDState

    bocpd = _FallbackBOCPDModule()
else:
    bocpd = _bocpd

if _survival_model is None:  # pragma: no cover - fallback path for minimal runtimes
    @dataclass
    class _FallbackFillObservation:
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

        def normalized(self) -> "_FallbackFillObservation":
            return self

    @dataclass
    class _FallbackSurvivalConfig:
        min_observations: int = 50
        min_per_stratum: int = 10
        synthetic_weight: float = 0.3
        horizons: list[int] = None

        def __post_init__(self) -> None:
            if self.horizons is None:
                self.horizons = [1800, 3600, 14400]

    @dataclass
    class _FallbackSurvivalPrediction:
        p_fill_30m: float
        p_fill_1h: float
        p_fill_4h: float
        median_remaining: float
        hazard_ratio: float
        model_tier: str
        confidence: float

        def to_dict(self) -> dict[str, Any]:
            return {
                "p_fill_30m": float(self.p_fill_30m),
                "p_fill_1h": float(self.p_fill_1h),
                "p_fill_4h": float(self.p_fill_4h),
                "median_remaining": float(self.median_remaining),
                "hazard_ratio": float(self.hazard_ratio),
                "model_tier": str(self.model_tier),
                "confidence": float(self.confidence),
            }

    class _FallbackSurvivalModel:
        def __init__(self, cfg: _FallbackSurvivalConfig, model_tier: str = "kaplan_meier") -> None:
            self.cfg = cfg
            self.model_tier = str(model_tier or "kaplan_meier")
            self.active_tier = "kaplan_meier"
            self.last_retrain_ts: float = 0.0
            self.n_observations: int = 0
            self.n_censored: int = 0
            self.synthetic_observations: int = 0
            self.fitted: bool = False

        def fit(
            self,
            observations: list[_FallbackFillObservation],
            synthetic_observations: list[_FallbackFillObservation] | None = None,
        ) -> bool:
            real = list(observations or [])
            synth = list(synthetic_observations or [])
            self.n_observations = len(real)
            self.n_censored = int(sum(1 for row in real if bool(row.censored)))
            self.synthetic_observations = len(synth)
            self.last_retrain_ts = _now()
            self.fitted = len(real) >= max(1, int(self.cfg.min_observations))
            return bool(self.fitted)

        def predict(self, obs: _FallbackFillObservation) -> _FallbackSurvivalPrediction:
            if not self.fitted:
                return _FallbackSurvivalPrediction(
                    p_fill_30m=0.5,
                    p_fill_1h=0.5,
                    p_fill_4h=0.5,
                    median_remaining=float("inf"),
                    hazard_ratio=1.0,
                    model_tier="kaplan_meier",
                    confidence=0.0,
                )
            dist = max(0.0, float(getattr(obs, "distance_pct", 0.0) or 0.0))
            p1h = max(0.05, min(0.95, exp(-dist)))
            p30 = max(0.02, min(0.98, p1h * 0.8))
            p4h = max(0.05, min(0.995, min(1.0, p1h + 0.2)))
            confidence = max(0.0, min(1.0, self.n_observations / max(1.0, float(self.cfg.min_observations * 2))))
            return _FallbackSurvivalPrediction(
                p_fill_30m=p30,
                p_fill_1h=p1h,
                p_fill_4h=p4h,
                median_remaining=3600.0,
                hazard_ratio=1.0,
                model_tier="kaplan_meier",
                confidence=confidence,
            )

        def status_payload(self, enabled: bool) -> dict[str, Any]:
            return {
                "enabled": bool(enabled),
                "model_tier": str(self.active_tier),
                "n_observations": int(self.n_observations),
                "n_censored": int(self.n_censored),
                "last_retrain_ts": float(self.last_retrain_ts),
                "strata_counts": {},
                "synthetic_observations": int(self.synthetic_observations),
                "cox_coefficients": {},
            }

        def snapshot_state(self) -> dict[str, Any]:
            return {
                "model_tier": str(self.model_tier),
                "active_tier": str(self.active_tier),
                "last_retrain_ts": float(self.last_retrain_ts),
                "n_observations": int(self.n_observations),
                "n_censored": int(self.n_censored),
                "synthetic_observations": int(self.synthetic_observations),
                "fitted": bool(self.fitted),
            }

        def restore_state(self, payload: dict[str, Any]) -> None:
            if not isinstance(payload, dict):
                return
            self.model_tier = str(payload.get("model_tier", self.model_tier) or self.model_tier)
            self.active_tier = str(payload.get("active_tier", self.active_tier) or self.active_tier)
            self.last_retrain_ts = float(payload.get("last_retrain_ts", self.last_retrain_ts) or 0.0)
            self.n_observations = max(0, int(payload.get("n_observations", self.n_observations) or 0))
            self.n_censored = max(0, int(payload.get("n_censored", self.n_censored) or 0))
            self.synthetic_observations = max(
                0, int(payload.get("synthetic_observations", self.synthetic_observations) or 0)
            )
            self.fitted = bool(payload.get("fitted", self.fitted))

        @staticmethod
        def generate_synthetic_observations(
            *,
            n_paths: int = 5000,
            weight: float = 0.3,
        ) -> list[_FallbackFillObservation]:
            rows: list[_FallbackFillObservation] = []
            strata = [(0, "A"), (0, "B"), (1, "A"), (1, "B"), (2, "A"), (2, "B")]
            per = max(1, int(n_paths) // len(strata))
            for regime, side in strata:
                for _ in range(per):
                    rows.append(
                        _FallbackFillObservation(
                            duration_sec=3600.0,
                            censored=False,
                            regime_at_entry=regime,
                            regime_at_exit=regime,
                            side=side,
                            distance_pct=0.3,
                            posterior_1m=[0.0, 1.0, 0.0],
                            posterior_15m=[0.0, 1.0, 0.0],
                            posterior_1h=[0.0, 1.0, 0.0],
                            entropy_at_entry=0.5,
                            p_switch_at_entry=0.05,
                            fill_imbalance=0.0,
                            congestion_ratio=0.0,
                            weight=max(1e-6, float(weight)),
                            synthetic=True,
                        )
                    )
            return rows

    class _FallbackSurvivalModule:
        FillObservation = _FallbackFillObservation
        SurvivalConfig = _FallbackSurvivalConfig
        SurvivalPrediction = _FallbackSurvivalPrediction
        SurvivalModel = _FallbackSurvivalModel

    survival_model = _FallbackSurvivalModule()
else:
    survival_model = _survival_model

if _bayesian_import_error is not None:
    logger.warning("Bayesian module unavailable; using neutral fallback: %s", _bayesian_import_error)
if _bocpd_import_error is not None:
    logger.warning("BOCPD module unavailable; using neutral fallback: %s", _bocpd_import_error)
if _survival_import_error is not None:
    logger.warning("Survival module unavailable; using neutral fallback: %s", _survival_import_error)

_BOT_RUNTIME_STATE_FILE = os.path.join(config.LOG_DIR, "bot_runtime.json")
_EQUITY_TS_FILE = os.path.join(config.LOG_DIR, "equity_ts.json")


def _recovery_orders_enabled_flag() -> bool:
    return bool(
        getattr(
            config,
            "RECOVERY_ORDERS_ENABLED",
            getattr(config, "RECOVERY_ENABLED", True),
        )
    )


def _recovery_disabled_message(action: str) -> str:
    return f"{action} disabled when RECOVERY_ORDERS_ENABLED=false"


def setup_logging() -> None:
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )


def _now() -> float:
    return time.time()


def _asset_balance(balance: dict, aliases: tuple[str, ...]) -> float:
    """
    Read balance for an asset across Kraken naming variants.

    Kraken may expose balances as:
    - legacy keys (e.g. XXDG, ZUSD)
    - plain keys (e.g. DOGE, USD)
    - free-balance suffix keys (e.g. XXDG.F, ZUSD.F)
    """
    if not isinstance(balance, dict):
        return 0.0

    for key in aliases:
        free_key = f"{key}.F"
        if free_key in balance:
            try:
                return float(balance.get(free_key, 0.0))
            except (TypeError, ValueError):
                continue

    for key in aliases:
        if key in balance:
            try:
                return float(balance.get(key, 0.0))
            except (TypeError, ValueError):
                continue

    return 0.0


def _usd_balance(balance: dict) -> float:
    return _asset_balance(balance, ("ZUSD", "USD"))


def _doge_balance(balance: dict) -> float:
    return _asset_balance(balance, ("XXDG", "XDG", "DOGE"))


@dataclass
class SlotRuntime:
    slot_id: int
    state: sm.PairState
    alias: str = ""


@dataclass
class ChurnerRuntimeState:
    active: bool = False
    stage: str = "idle"  # idle | entry_open | exit_open
    parent_position_id: int = 0
    parent_trade_id: str = ""
    cycle_id: int = 0
    order_size_usd: float = 0.0
    compound_usd: float = 0.0
    reserve_allocated_usd: float = 0.0
    entry_side: str = ""
    entry_txid: str = ""
    entry_price: float = 0.0
    entry_volume: float = 0.0
    entry_placed_at: float = 0.0
    entry_fill_price: float = 0.0
    entry_fill_fee: float = 0.0
    entry_fill_time: float = 0.0
    exit_txid: str = ""
    exit_price: float = 0.0
    exit_placed_at: float = 0.0
    churner_position_id: int = 0
    last_error: str = ""
    last_state_change_at: float = 0.0


@dataclass(frozen=True)
class ExternalFlow:
    ledger_id: str
    flow_type: str
    asset: str
    amount: float
    fee: float
    timestamp: float
    doge_eq: float
    price_at_detect: float


@dataclass(frozen=True)
class RuntimeToggleSpec:
    key: str
    group: str
    description: str
    dependencies: tuple[str, ...] = ()
    side_effect: str | None = None


class CapitalLedger:
    """Within-loop capital tracker that prevents over-commitment across slots."""

    def __init__(self) -> None:
        self._synced = False
        self._usd_from_free = False
        self._doge_from_free = False
        self._total_usd = 0.0
        self._total_doge = 0.0
        self._committed_usd = 0.0
        self._committed_doge = 0.0
        self._loop_placed_usd = 0.0
        self._loop_placed_doge = 0.0

    @property
    def available_usd(self) -> float:
        # With Kraken free-balance keys (`*.F`), total already means available.
        if self._usd_from_free:
            return max(0.0, self._total_usd - self._loop_placed_usd)
        return max(0.0, self._total_usd - self._committed_usd - self._loop_placed_usd)

    @property
    def available_doge(self) -> float:
        # With Kraken free-balance keys (`*.F`), total already means available.
        if self._doge_from_free:
            return max(0.0, self._total_doge - self._loop_placed_doge)
        return max(0.0, self._total_doge - self._committed_doge - self._loop_placed_doge)

    def sync(self, balance: dict, slots: dict[int, SlotRuntime]) -> None:
        """Recompute from scratch at loop start using fresh Kraken balance."""
        self._usd_from_free = any(k in balance for k in ("ZUSD.F", "USD.F"))
        self._doge_from_free = any(k in balance for k in ("XXDG.F", "XDG.F", "DOGE.F"))
        self._total_usd = _usd_balance(balance)
        self._total_doge = _doge_balance(balance)
        committed_usd = 0.0
        committed_doge = 0.0
        for slot in slots.values():
            st = slot.state
            for o in st.orders:
                if not o.txid:
                    continue
                if o.side == "buy":
                    committed_usd += o.volume * o.price
                elif o.side == "sell":
                    committed_doge += o.volume
            for r in st.recovery_orders:
                if not r.txid:
                    continue
                if r.side == "buy":
                    committed_usd += r.volume * r.price
                elif r.side == "sell":
                    committed_doge += r.volume
        self._committed_usd = committed_usd
        self._committed_doge = committed_doge
        self._loop_placed_usd = 0.0
        self._loop_placed_doge = 0.0
        self._synced = True

    def commit_order(self, side: str, price: float, volume: float) -> None:
        """Deduct capital after a successful order placement within this loop."""
        if side == "buy":
            self._loop_placed_usd += volume * price
        elif side == "sell":
            self._loop_placed_doge += volume

    def clear(self) -> None:
        """Reset loop-placed accumulators at end of loop."""
        self._loop_placed_usd = 0.0
        self._loop_placed_doge = 0.0
        self._synced = False

    def snapshot(self) -> dict:
        return {
            "synced": self._synced,
            "usd_from_free": self._usd_from_free,
            "doge_from_free": self._doge_from_free,
            "total_usd": self._total_usd,
            "total_doge": self._total_doge,
            "committed_usd": self._committed_usd,
            "committed_doge": self._committed_doge,
            "loop_placed_usd": self._loop_placed_usd,
            "loop_placed_doge": self._loop_placed_doge,
            "available_usd": self.available_usd,
            "available_doge": self.available_doge,
        }


class BotRuntime:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.started_at = _now()
        self.running = True
        self._runtime_overrides: dict[str, bool] = {}
        self._toggle_registry: dict[str, RuntimeToggleSpec] = self._build_toggle_registry()

        self.mode = "INIT"  # INIT | RUNNING | PAUSED | HALTED
        self.pause_reason = ""

        self.pair = config.PAIR
        self.pair_display = config.PAIR_DISPLAY
        self.entry_pct = float(config.PAIR_ENTRY_PCT)
        self.profit_pct = float(config.PAIR_PROFIT_PCT)

        self.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        self.maker_fee_pct = float(config.MAKER_FEE_PCT)
        self.taker_fee_pct = float(config.MAKER_FEE_PCT)

        self.slots: dict[int, SlotRuntime] = {}
        self.ledger = CapitalLedger()
        self.next_slot_id = 1
        self.slot_alias_pool: tuple[str, ...] = tuple(config.SLOT_ALIAS_POOL)
        self.slot_alias_recycle_queue: deque[str] = deque()
        self.slot_alias_fallback_counter = 1

        self.target_layers = 0
        self.effective_layers = 0
        self.layer_last_add_event: dict | None = None
        self._layer_action_in_flight: bool = False

        self.next_event_id = 1
        self.seen_fill_txids: set[str] = set()

        self.price_history: list[tuple[float, float]] = []
        self.last_price = 0.0
        self.last_price_ts = 0.0

        self.consecutive_api_errors = 0
        self.enforce_loop_budget = False
        self.loop_private_calls = 0
        self.entry_adds_per_loop_cap = max(1, int(config.MAX_ENTRY_ADDS_PER_LOOP))
        self.entry_adds_per_loop_used = 0
        self._entry_adds_deferred_total = 0
        self._entry_adds_drained_total = 0
        self._entry_adds_last_deferred_at = 0.0
        self._entry_adds_last_drained_at = 0.0
        self._loop_balance_cache: dict | None = None
        self._loop_available_usd: float | None = None
        self._loop_available_doge: float | None = None
        self._loop_dust_dividend: float | None = None
        self._loop_b_side_base: float | None = None
        self._loop_effective_layers: dict[str, float | int | None] | None = None
        self._dust_sweep_enabled: bool = self._flag_value("DUST_SWEEP_ENABLED")
        self._dust_min_threshold_usd: float = max(0.0, float(getattr(config, "DUST_MIN_THRESHOLD", 0.50)))
        self._dust_max_bump_pct: float = max(0.0, float(getattr(config, "DUST_MAX_BUMP_PCT", 25.0)))
        self._dust_last_absorbed_usd: float = 0.0
        self._dust_last_dividend_usd: float = 0.0
        self._quote_first_carry_usd: float = 0.0
        self._loop_quote_first_meta: dict | None = None
        self._last_balance_snapshot: dict | None = None
        self._last_balance_ts = 0.0

        # Kraken-first capacity telemetry (pair-filtered open orders).
        self._kraken_open_orders_current: int | None = None
        self._kraken_open_orders_ts = 0.0
        self._open_order_drift_over_threshold_since: float | None = None
        self._open_order_drift_last_alert_at = 0.0
        self._open_order_drift_alert_active = False
        self._open_order_drift_alert_active_since: float | None = None

        # Auto-soft-close telemetry.
        self._auto_soft_close_total: int = 0
        self._auto_soft_close_last_at: float = 0.0
        self._auto_recovery_drain_total: int = 0
        self._auto_recovery_drain_last_at: float = 0.0

        # Balance reconciliation baseline {usd, doge, ts}.
        self._recon_baseline: dict | None = None
        self._flow_detection_active: bool = bool(getattr(config, "FLOW_DETECTION_ENABLED", True))
        self._flow_poll_interval: float = max(30.0, float(getattr(config, "FLOW_POLL_INTERVAL_SEC", 300.0)))
        self._flow_last_poll_ts: float = 0.0
        self._flow_ledger_cursor: float = 0.0
        self._flow_seen_ids: set[str] = set()
        self._external_flows: list[ExternalFlow] = []
        self._baseline_adjustments: list[dict] = []
        self._flow_total_deposits_doge_eq: float = 0.0
        self._flow_total_withdrawals_doge_eq: float = 0.0
        self._flow_total_count: int = 0
        self._flow_last_error: str = ""
        self._flow_last_ok: bool = True
        self._flow_disabled_reason: str = ""
        self._flow_history_cap: int = 1000
        self._baseline_adjustments_cap: int = 1000
        self._flow_recent_status_limit: int = 25

        # Rolling 24h fill/partial telemetry.
        self._partial_fill_open_events: deque[float] = deque()
        self._partial_fill_cancel_events: deque[float] = deque()
        self._fill_durations_1d: deque[tuple[float, float]] = deque()
        self._partial_open_seen_txids: set[str] = set()

        # Rolling 24h DOGE-equivalent equity snapshots (5-min interval).
        self._doge_eq_snapshots: deque[tuple[float, float]] = deque()  # (ts, doge_eq)
        self._doge_eq_snapshot_interval: float = max(
            30.0,
            float(getattr(config, "EQUITY_SNAPSHOT_INTERVAL_SEC", 300.0)),
        )
        self._doge_eq_last_snapshot_ts: float = 0.0
        self._equity_ts_enabled: bool = bool(getattr(config, "EQUITY_TS_ENABLED", True))
        self._equity_ts_flush_interval: float = max(
            30.0,
            float(getattr(config, "EQUITY_SNAPSHOT_FLUSH_SEC", 300.0)),
        )
        self._equity_ts_retention_days: int = max(1, int(getattr(config, "EQUITY_TS_RETENTION_DAYS", 7)))
        self._equity_ts_sparkline_7d_step: int = max(1, int(getattr(config, "EQUITY_TS_SPARKLINE_7D_STEP", 6)))
        self._equity_ts_records: list[dict] = []
        self._equity_ts_last_flush_ts: float = 0.0
        self._equity_ts_last_flush_ok: bool = True
        self._equity_ts_last_flush_error: str = ""
        self._equity_ts_dirty: bool = False

        # Inventory rebalancer state.
        self._rebalancer_idle_ratio: float = 0.0
        self._rebalancer_smoothed_error: float = 0.0
        self._rebalancer_smoothed_velocity: float = 0.0
        self._rebalancer_current_skew: float = 0.0
        self._rebalancer_last_update_ts: float = 0.0
        self._rebalancer_last_raw_error: float = 0.0
        self._rebalancer_sign_flip_history: deque[float] = deque()
        self._rebalancer_damped_until: float = 0.0
        self._rebalancer_last_capacity_band: str = "normal"
        base_target = max(0.0, min(1.0, float(config.REBALANCE_TARGET_IDLE_PCT)))
        self._trend_fast_ema: float = 0.0
        self._trend_slow_ema: float = 0.0
        self._trend_score: float = 0.0
        self._trend_dynamic_target: float = base_target
        self._trend_smoothed_target: float = base_target
        self._trend_target_locked_until: float = 0.0
        self._trend_last_update_ts: float = 0.0
        self._ohlcv_since_cursor: int | None = None
        self._ohlcv_last_sync_ts: float = 0.0
        self._ohlcv_last_candle_ts: float = 0.0
        self._ohlcv_last_rows_queued: int = 0
        self._ohlcv_secondary_since_cursor: int | None = None
        self._ohlcv_secondary_last_sync_ts: float = 0.0
        self._ohlcv_secondary_last_candle_ts: float = 0.0
        self._ohlcv_secondary_last_rows_queued: int = 0
        self._ohlcv_tertiary_since_cursor: int | None = None
        self._ohlcv_tertiary_last_sync_ts: float = 0.0
        self._ohlcv_tertiary_last_candle_ts: float = 0.0
        self._ohlcv_tertiary_last_rows_queued: int = 0
        self._hmm_readiness_cache: dict[str, dict[str, Any]] = {}
        self._hmm_readiness_last_ts: dict[str, float] = {}
        self._hmm_detector: Any = None
        self._hmm_detector_secondary: Any = None
        self._hmm_detector_tertiary: Any = None
        self._hmm_module: Any = None
        self._hmm_numpy: Any = None
        self._regime_history_30m: deque[dict[str, Any]] = deque()
        self._regime_history_window_sec: float = 1800.0
        self._hmm_state: dict[str, Any] = self._hmm_default_state()
        self._hmm_state_secondary: dict[str, Any] = self._hmm_default_state(
            enabled=self._flag_value("HMM_ENABLED")
            and self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"),
            interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
        )
        self._hmm_state_tertiary: dict[str, Any] = self._hmm_default_state(
            enabled=self._flag_value("HMM_ENABLED")
            and self._flag_value("HMM_TERTIARY_ENABLED"),
            interval_min=max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60))),
        )
        self._hmm_consensus: dict[str, Any] = dict(self._hmm_state)
        self._hmm_consensus.update({
            "agreement": "primary_only",
            "source_mode": "primary",
            "multi_timeframe": False,
        })
        self._hmm_training_depth: dict[str, Any] = self._hmm_training_depth_default(
            state_key="primary"
        )
        self._hmm_training_depth_secondary: dict[str, Any] = self._hmm_training_depth_default(
            state_key="secondary"
        )
        self._hmm_training_depth_tertiary: dict[str, Any] = self._hmm_training_depth_default(
            state_key="tertiary"
        )
        self._hmm_last_train_attempt_ts: float = 0.0
        self._hmm_last_train_attempt_ts_secondary: float = 0.0
        self._hmm_last_train_attempt_ts_tertiary: float = 0.0
        self._hmm_backfill_last_at: float = 0.0
        self._hmm_backfill_last_rows: int = 0
        self._hmm_backfill_last_message: str = ""
        self._hmm_backfill_stall_count: int = 0
        self._hmm_backfill_last_at_secondary: float = 0.0
        self._hmm_backfill_last_rows_secondary: int = 0
        self._hmm_backfill_last_message_secondary: str = ""
        self._hmm_backfill_stall_count_secondary: int = 0
        self._hmm_backfill_last_at_tertiary: float = 0.0
        self._hmm_backfill_last_rows_tertiary: int = 0
        self._hmm_backfill_last_message_tertiary: str = ""
        self._hmm_backfill_stall_count_tertiary: int = 0
        self._hmm_tertiary_transition: dict[str, Any] = {
            "from_regime": "RANGING",
            "to_regime": "RANGING",
            "transition_age_sec": 0.0,
            "confidence": 0.0,
            "confirmed": False,
            "confirmation_count": 0,
            "changed_at": 0.0,
        }
        self._belief_state: bayesian_engine.BeliefState = bayesian_engine.BeliefState(enabled=False)
        self._belief_state_last_ts: float = 0.0
        self._belief_cycle_metadata: dict[tuple[int, str, int], dict[str, Any]] = {}
        self._action_knobs: bayesian_engine.ActionKnobs = bayesian_engine.ActionKnobs(enabled=False)
        self._micro_features: dict[str, float] = {
            "fill_imbalance": 0.0,
            "spread_realization": 1.0,
            "fill_time_derivative": 0.0,
            "congestion_ratio": 0.0,
        }
        self._fill_events_recent: deque[tuple[float, str]] = deque()
        self._fill_duration_events: deque[tuple[float, float]] = deque()
        self._spread_realization_events: deque[tuple[float, float]] = deque()
        self._bocpd: bocpd.BOCPD | None = None
        self._bocpd_state: bocpd.BOCPDState = bocpd.BOCPDState()
        self._bocpd_last_price: float = 0.0
        self._manifold_score: bayesian_engine.ManifoldScore = bayesian_engine.ManifoldScore(enabled=False)
        self._manifold_history: deque[tuple[float, float, float, float, float, float]] = deque(
            maxlen=max(1, int(getattr(config, "MTS_HISTORY_SIZE", 360)))
        )
        if self._flag_value("BOCPD_ENABLED"):
            self._bocpd = self._new_bocpd_instance()
        self._survival_model: survival_model.SurvivalModel | None = None
        self._survival_last_retrain_ts: float = 0.0
        if self._flag_value("SURVIVAL_MODEL_ENABLED"):
            self._survival_model = self._new_survival_model_instance()
        self._trade_beliefs: dict[int, bayesian_engine.TradeBeliefState] = {}
        self._trade_belief_ev_history: dict[int, list[float]] = {}
        self._trade_belief_widen_count: dict[int, int] = {}
        self._trade_belief_widen_total_pct: dict[int, float] = {}
        self._trade_belief_action_counts: dict[str, int] = {
            "hold": 0,
            "tighten": 0,
            "widen": 0,
            "reprice_breakeven": 0,
        }
        self._trade_belief_last_update_ts: float = 0.0
        self._belief_timer_overrides: dict[int, float] = {}
        self._belief_slot_override_until: dict[int, float] = {}
        self._regime_tier: int = 0
        self._regime_tier_entered_at: float = 0.0
        self._regime_tier2_grace_start: float = 0.0
        self._regime_side_suppressed: str | None = None
        self._regime_last_eval_ts: float = 0.0
        self._regime_tier2_last_downgrade_at: float = 0.0
        self._regime_cooldown_suppressed_side: str | None = None
        self._regime_tier_history: list[dict[str, Any]] = []
        self._regime_mechanical_tier: int = 0
        self._regime_mechanical_direction: str = "symmetric"
        self._regime_mechanical_since: float = 0.0
        self._regime_mechanical_tier_entered_at: float = 0.0
        self._regime_mechanical_tier2_last_downgrade_at: float = 0.0
        self._ai_regime_last_run_ts: float = 0.0
        self._ai_regime_opinion: dict[str, Any] = {}
        self._ai_regime_history: deque[dict[str, Any]] = deque()
        self._ai_regime_dismissed: bool = False
        self._ai_regime_thread_alive: bool = False
        self._ai_regime_pending_result: dict[str, Any] | None = None
        self._ai_regime_last_mechanical_tier: int = 0
        self._ai_regime_last_mechanical_direction: str = "symmetric"
        self._ai_regime_last_consensus_agreement: str = "primary_only"
        self._ai_regime_last_trigger_reason: str = ""
        self._ai_override_tier: int | None = None
        self._ai_override_direction: str | None = None
        self._ai_override_until: float | None = None
        self._ai_override_applied_at: float | None = None
        self._ai_override_source_conviction: int | None = None
        # Strategic accumulation engine runtime state.
        self._accum_state: str = "IDLE"  # IDLE | ARMED | ACTIVE | COMPLETED | STOPPED
        self._accum_direction: str | None = None
        self._accum_trigger_from_regime: str = "RANGING"
        self._accum_trigger_to_regime: str = "RANGING"
        self._accum_start_ts: float = 0.0
        self._accum_start_price: float = 0.0
        self._accum_spent_usd: float = 0.0
        self._accum_acquired_doge: float = 0.0
        self._accum_n_buys: int = 0
        self._accum_last_buy_ts: float = 0.0
        self._accum_budget_usd: float = 0.0
        self._accum_armed_at: float = 0.0
        self._accum_hold_streak: int = 0
        self._accum_last_session_end_ts: float = 0.0
        self._accum_last_session_summary: dict[str, Any] = {}
        self._accum_manual_stop_requested: bool = False
        self._accum_cooldown_remaining_sec: int = 0
        self._regime_shadow_state: dict[str, Any] = {
            "enabled": False,
            "shadow_enabled": False,
            "actuation_enabled": False,
            "tier": 0,
            "regime": "RANGING",
            "confidence": 0.0,  # effective confidence used for tier gating
            "confidence_raw": 0.0,  # raw HMM confidence from detector/consensus
            "confidence_effective": 0.0,
            "confidence_modifier": 1.0,
            "confidence_modifier_source": "none",
            "bias_signal": 0.0,
            "abs_bias": 0.0,
            "suppressed_side": None,
            "favored_side": None,
            "directional_ok_tier1": False,
            "directional_ok_tier2": False,
            "hmm_ready": False,
            "last_eval_ts": 0.0,
            "reason": "init",
            "mechanical_tier": 0,
            "mechanical_direction": "symmetric",
            "override_active": False,
        }

        # Daily loss lock (aggregate bot-level, UTC day).
        self._daily_loss_lock_active: bool = False
        self._daily_loss_lock_utc_day: str = ""
        self._daily_realized_loss_utc: float = 0.0
        self._sticky_release_total: int = 0
        self._sticky_release_last_at: float = 0.0
        self._release_recon_blocked: bool = False
        self._release_recon_blocked_reason: str = ""
        self._self_heal_reprice_total: int = 0
        self._self_heal_reprice_last_at: float = 0.0
        self._self_heal_reprice_last_summary: dict[str, Any] = {}
        self._self_heal_hold_until_by_position: dict[int, float] = {}
        self._position_ledger_migration_done: bool = False
        self._position_ledger_migration_last_at: float = 0.0
        self._position_ledger_migration_last_created: int = 0
        self._position_ledger_migration_last_scanned: int = 0
        self._position_ledger = PositionLedger(
            enabled=self._flag_value("POSITION_LEDGER_ENABLED"),
            journal_local_limit=max(50, int(getattr(config, "POSITION_JOURNAL_LOCAL_LIMIT", 500))),
        )
        # Active exit identity -> position mappings (runtime convenience index).
        self._position_by_exit_local: dict[tuple[int, int], int] = {}
        self._position_by_exit_txid: dict[str, int] = {}
        # Optional cycle classification hook for throughput exclusion.
        self._cycle_slot_mode: dict[tuple[int, str, int], str] = {}
        self._churner_by_slot: dict[int, ChurnerRuntimeState] = {}
        self._churner_next_cycle_id: int = 1
        self._churner_reserve_available_usd: float = max(
            0.0, float(getattr(config, "CHURNER_RESERVE_USD", 0.0))
        )
        self._churner_day_key: str = self._utc_day_key()
        self._churner_cycles_today: int = 0
        self._churner_profit_today: float = 0.0
        self._churner_cycles_total: int = 0
        self._churner_profit_total: float = 0.0
        self._throughput: ThroughputSizer | None = None
        if self._flag_value("TP_ENABLED"):
            self._throughput = self._new_throughput_sizer()
        self._init_hmm_runtime()

    # ------------------ Config/State ------------------

    @staticmethod
    def _build_toggle_registry() -> dict[str, RuntimeToggleSpec]:
        specs = (
            RuntimeToggleSpec(
                key="HMM_ENABLED",
                group="Regime Detection",
                description="Primary 1-minute regime detector",
                side_effect="_toggle_side_effect_hmm_runtime",
            ),
            RuntimeToggleSpec(
                key="HMM_MULTI_TIMEFRAME_ENABLED",
                group="Regime Detection",
                description="1m + 15m consensus mode",
                dependencies=("HMM_ENABLED",),
                side_effect="_toggle_side_effect_hmm_runtime",
            ),
            RuntimeToggleSpec(
                key="HMM_SECONDARY_OHLCV_ENABLED",
                group="Regime Detection",
                description="15m candle collection",
            ),
            RuntimeToggleSpec(
                key="HMM_TERTIARY_ENABLED",
                group="Regime Detection",
                description="1h strategic transitions",
                dependencies=("HMM_ENABLED",),
                side_effect="_toggle_side_effect_hmm_runtime",
            ),
            RuntimeToggleSpec(
                key="HMM_DEEP_DECAY_ENABLED",
                group="Regime Detection",
                description="Recency decay on training window",
            ),
            RuntimeToggleSpec(
                key="AI_REGIME_ADVISOR_ENABLED",
                group="Intelligence",
                description="LLM regime second opinion",
            ),
            RuntimeToggleSpec(
                key="AI_AUTO_EXECUTE",
                group="Intelligence",
                description="Auto-apply conservative AI actions",
            ),
            RuntimeToggleSpec(
                key="BELIEF_TRACKER_ENABLED",
                group="Intelligence",
                description="Per-trade belief tracker",
                dependencies=("POSITION_LEDGER_ENABLED",),
                side_effect="_toggle_side_effect_belief_tracker_runtime",
            ),
            RuntimeToggleSpec(
                key="BELIEF_WIDEN_ENABLED",
                group="Intelligence",
                description="Allow belief-driven widening",
                dependencies=("BELIEF_TRACKER_ENABLED",),
            ),
            RuntimeToggleSpec(
                key="BOCPD_ENABLED",
                group="Intelligence",
                description="Bayesian changepoint detector",
                side_effect="_toggle_side_effect_bocpd_runtime",
            ),
            RuntimeToggleSpec(
                key="ENRICHED_FEATURES_ENABLED",
                group="Intelligence",
                description="Private microstructure features",
                side_effect="_toggle_side_effect_enriched_features_runtime",
            ),
            RuntimeToggleSpec(
                key="SURVIVAL_MODEL_ENABLED",
                group="Intelligence",
                description="Survival model predictions",
                side_effect="_toggle_side_effect_survival_runtime",
            ),
            RuntimeToggleSpec(
                key="KNOB_MODE_ENABLED",
                group="Intelligence",
                description="Continuous action knob mode",
            ),
            RuntimeToggleSpec(
                key="TP_ENABLED",
                group="Capital & Sizing",
                description="Throughput advisory sizing",
                side_effect="_toggle_side_effect_throughput_runtime",
            ),
            RuntimeToggleSpec(
                key="REGIME_DIRECTIONAL_ENABLED",
                group="Capital & Sizing",
                description="Directional regime actuation",
            ),
            RuntimeToggleSpec(
                key="REGIME_SHADOW_ENABLED",
                group="Capital & Sizing",
                description="Directional shadow evaluation",
            ),
            RuntimeToggleSpec(
                key="DUST_SWEEP_ENABLED",
                group="Capital & Sizing",
                description="Fold idle USD into B-side entries",
                side_effect="_toggle_side_effect_dust_sweep_runtime",
            ),
            RuntimeToggleSpec(
                key="REBALANCE_ENABLED",
                group="Capital & Sizing",
                description="Inventory skew governor",
            ),
            RuntimeToggleSpec(
                key="ACCUM_ENABLED",
                group="Capital & Sizing",
                description="Strategic accumulation engine",
            ),
            RuntimeToggleSpec(
                key="MTS_ENABLED",
                group="Capital & Sizing",
                description="Manifold Trading Score master switch",
            ),
            RuntimeToggleSpec(
                key="MTS_ENTRY_THROTTLE_ENABLED",
                group="Capital & Sizing",
                description="Apply MTS entry throttling",
                dependencies=("MTS_ENABLED",),
            ),
            RuntimeToggleSpec(
                key="MTS_KERNEL_ENABLED",
                group="Capital & Sizing",
                description="Enable kernel-memory blend",
                dependencies=("MTS_ENABLED",),
            ),
            RuntimeToggleSpec(
                key="STICKY_MODE_ENABLED",
                group="Position Management",
                description="Keep exits waiting indefinitely",
            ),
            RuntimeToggleSpec(
                key="RECOVERY_ORDERS_ENABLED",
                group="Position Management",
                description="Recovery order management",
            ),
            RuntimeToggleSpec(
                key="SUBSIDY_ENABLED",
                group="Position Management",
                description="Subsidy-funded repricing",
                dependencies=("POSITION_LEDGER_ENABLED",),
            ),
            RuntimeToggleSpec(
                key="CHURNER_ENABLED",
                group="Position Management",
                description="Regime-gated churner helper cycles",
                dependencies=("POSITION_LEDGER_ENABLED",),
            ),
            RuntimeToggleSpec(
                key="POSITION_LEDGER_ENABLED",
                group="Position Management",
                description="Position ledger subsystem",
                side_effect="_toggle_side_effect_position_ledger_runtime",
            ),
            RuntimeToggleSpec(
                key="RELEASE_AUTO_ENABLED",
                group="Position Management",
                description="Auto-release eligible exits",
            ),
        )
        return {spec.key: spec for spec in specs}

    def _flag_value(self, key: str) -> bool:
        norm_key = str(key or "").strip().upper()
        if not norm_key:
            return False
        if norm_key in self._runtime_overrides:
            return bool(self._runtime_overrides[norm_key])
        return bool(getattr(config, norm_key, False))

    def _new_throughput_sizer(self) -> ThroughputSizer:
        return ThroughputSizer(
            ThroughputConfig(
                enabled=self._flag_value("TP_ENABLED"),
                lookback_cycles=int(getattr(config, "TP_LOOKBACK_CYCLES", 500)),
                min_samples=int(getattr(config, "TP_MIN_SAMPLES", 20)),
                min_samples_per_bucket=int(getattr(config, "TP_MIN_SAMPLES_PER_BUCKET", 10)),
                full_confidence_samples=int(getattr(config, "TP_FULL_CONFIDENCE_SAMPLES", 50)),
                floor_mult=float(getattr(config, "TP_FLOOR_MULT", 0.5)),
                ceiling_mult=float(getattr(config, "TP_CEILING_MULT", 2.0)),
                censored_weight=float(getattr(config, "TP_CENSORED_WEIGHT", 0.5)),
                age_pressure_trigger=float(getattr(config, "TP_AGE_PRESSURE_TRIGGER", 1.5)),
                age_pressure_sensitivity=float(getattr(config, "TP_AGE_PRESSURE_SENSITIVITY", 0.5)),
                age_pressure_floor=float(getattr(config, "TP_AGE_PRESSURE_FLOOR", 0.3)),
                util_threshold=float(getattr(config, "TP_UTIL_THRESHOLD", 0.7)),
                util_sensitivity=float(getattr(config, "TP_UTIL_SENSITIVITY", 0.8)),
                util_floor=float(getattr(config, "TP_UTIL_FLOOR", 0.4)),
                recency_halflife=int(getattr(config, "TP_RECENCY_HALFLIFE", 100)),
                log_updates=bool(getattr(config, "TP_LOG_UPDATES", True)),
            )
        )

    @staticmethod
    def _new_bocpd_instance() -> bocpd.BOCPD:
        return bocpd.BOCPD(
            expected_run_length=max(2, int(getattr(config, "BOCPD_EXPECTED_RUN_LENGTH", 200))),
            max_run_length=max(10, int(getattr(config, "BOCPD_MAX_RUN_LENGTH", 500))),
            alert_threshold=float(getattr(config, "BOCPD_ALERT_THRESHOLD", 0.30)),
            urgent_threshold=float(getattr(config, "BOCPD_URGENT_THRESHOLD", 0.50)),
        )

    @staticmethod
    def _new_survival_model_instance() -> survival_model.SurvivalModel:
        return survival_model.SurvivalModel(
            survival_model.SurvivalConfig(
                min_observations=max(1, int(getattr(config, "SURVIVAL_MIN_OBSERVATIONS", 50))),
                min_per_stratum=max(1, int(getattr(config, "SURVIVAL_MIN_PER_STRATUM", 10))),
                synthetic_weight=max(0.0, min(1.0, float(getattr(config, "SURVIVAL_SYNTHETIC_WEIGHT", 0.30)))),
                horizons=list(getattr(config, "SURVIVAL_HORIZONS", [1800, 3600, 14400])),
            ),
            model_tier=str(getattr(config, "SURVIVAL_MODEL_TIER", "kaplan_meier")),
        )

    def _apply_toggle_side_effect(self, key: str) -> tuple[bool, str]:
        spec = self._toggle_registry.get(str(key or "").strip().upper())
        if spec is None:
            return False, "unknown toggle"
        if not spec.side_effect:
            return True, ""
        handler = getattr(self, str(spec.side_effect), None)
        if not callable(handler):
            return False, f"missing side-effect handler: {spec.side_effect}"
        try:
            handler()
        except Exception as exc:
            return False, f"side-effect failed: {exc}"
        return True, ""

    def _set_runtime_override(self, key: str, value: bool) -> tuple[bool, str]:
        norm_key = str(key or "").strip().upper()
        if not norm_key:
            return False, "toggle key required"
        spec = self._toggle_registry.get(norm_key)
        if spec is None:
            return False, f"unknown toggle: {norm_key}"

        old_present = norm_key in self._runtime_overrides
        old_value = self._runtime_overrides.get(norm_key)
        old_effective = self._flag_value(norm_key)
        self._runtime_overrides[norm_key] = bool(value)
        new_effective = self._flag_value(norm_key)

        if bool(value):
            missing = [dep for dep in spec.dependencies if dep != norm_key and not self._flag_value(dep)]
            if missing:
                if old_present:
                    self._runtime_overrides[norm_key] = bool(old_value)
                else:
                    self._runtime_overrides.pop(norm_key, None)
                return False, f"dependency blocked: requires {', '.join(sorted(missing))}"

        if new_effective != old_effective:
            ok, msg = self._apply_toggle_side_effect(norm_key)
            if not ok:
                if old_present:
                    self._runtime_overrides[norm_key] = bool(old_value)
                else:
                    self._runtime_overrides.pop(norm_key, None)
                logger.warning("runtime override failed key=%s value=%s: %s", norm_key, bool(value), msg)
                return False, msg

        return True, f"{norm_key} override set to {bool(value)}"

    def _clear_runtime_override(self, key: str) -> tuple[bool, str]:
        norm_key = str(key or "").strip().upper()
        if not norm_key:
            return False, "toggle key required"
        if norm_key not in self._toggle_registry:
            return False, f"unknown toggle: {norm_key}"
        if norm_key not in self._runtime_overrides:
            return True, f"{norm_key} already using config default"

        old_value = bool(self._runtime_overrides.get(norm_key))
        old_effective = self._flag_value(norm_key)
        self._runtime_overrides.pop(norm_key, None)
        new_effective = self._flag_value(norm_key)
        if old_effective != new_effective:
            ok, msg = self._apply_toggle_side_effect(norm_key)
            if not ok:
                self._runtime_overrides[norm_key] = old_value
                logger.warning("runtime reset failed key=%s: %s", norm_key, msg)
                return False, msg
        return True, f"{norm_key} override cleared"

    def _clear_all_runtime_overrides(self) -> int:
        keys = list(self._runtime_overrides.keys())
        if not keys:
            return 0
        old_effective = {key: self._flag_value(key) for key in keys}
        self._runtime_overrides.clear()
        for key in keys:
            if old_effective.get(key) == self._flag_value(key):
                continue
            ok, msg = self._apply_toggle_side_effect(key)
            if not ok:
                logger.warning("runtime reset-all side-effect failed key=%s: %s", key, msg)
        return len(keys)

    def _ops_panel_status_payload(self) -> dict[str, Any]:
        overrides: dict[str, dict[str, Any]] = {}
        for key in sorted(self._runtime_overrides.keys()):
            overrides[str(key)] = {
                "effective": bool(self._flag_value(key)),
                "config_default": bool(getattr(config, str(key), False)),
                "source": "runtime_override",
            }
        return {
            "overrides_active": int(len(overrides)),
            "overrides": overrides,
        }

    def _ops_toggles_payload(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        group_summary: dict[str, dict[str, Any]] = {}
        ordered = sorted(self._toggle_registry.items(), key=lambda row: (row[1].group.lower(), row[0]))
        for key, spec in ordered:
            override_active = key in self._runtime_overrides
            effective = bool(self._flag_value(key))
            config_default = bool(getattr(config, key, False))
            group = str(spec.group)
            row = {
                "key": str(key),
                "group": group,
                "description": str(spec.description),
                "dependencies": [str(dep) for dep in spec.dependencies],
                "effective": effective,
                "config_default": config_default,
                "override_active": bool(override_active),
                "source": ("runtime_override" if override_active else "config_default"),
            }
            rows.append(row)
            agg = group_summary.setdefault(
                group,
                {
                    "group": group,
                    "enabled": 0,
                    "overridden": 0,
                    "total": 0,
                },
            )
            agg["total"] = int(agg["total"]) + 1
            if effective:
                agg["enabled"] = int(agg["enabled"]) + 1
            if override_active:
                agg["overridden"] = int(agg["overridden"]) + 1

        groups = [group_summary[name] for name in sorted(group_summary.keys(), key=str.lower)]
        return {
            "overrides_active": int(len(self._runtime_overrides)),
            "groups": groups,
            "toggles": rows,
        }

    def _toggle_side_effect_hmm_runtime(self) -> None:
        self._init_hmm_runtime()

    def _toggle_side_effect_throughput_runtime(self) -> None:
        if self._flag_value("TP_ENABLED"):
            self._throughput = self._new_throughput_sizer()
            return
        self._throughput = None

    def _toggle_side_effect_bocpd_runtime(self) -> None:
        if self._flag_value("BOCPD_ENABLED"):
            self._bocpd = self._new_bocpd_instance()
        else:
            self._bocpd = None
        self._bocpd_state = bocpd.BOCPDState()
        self._bocpd_last_price = 0.0

    def _toggle_side_effect_survival_runtime(self) -> None:
        if self._flag_value("SURVIVAL_MODEL_ENABLED"):
            self._survival_model = self._new_survival_model_instance()
            return
        self._survival_model = None

    def _toggle_side_effect_belief_tracker_runtime(self) -> None:
        if self._flag_value("BELIEF_TRACKER_ENABLED"):
            return
        self._trade_beliefs.clear()
        self._trade_belief_ev_history.clear()
        self._trade_belief_widen_count.clear()
        self._trade_belief_widen_total_pct.clear()
        self._trade_belief_action_counts = {
            "hold": 0,
            "tighten": 0,
            "widen": 0,
            "reprice_breakeven": 0,
        }
        self._belief_timer_overrides.clear()
        self._belief_slot_override_until.clear()

    def _toggle_side_effect_enriched_features_runtime(self) -> None:
        if self._flag_value("ENRICHED_FEATURES_ENABLED"):
            return
        self._micro_features = {
            "fill_imbalance": 0.0,
            "spread_realization": 1.0,
            "fill_time_derivative": 0.0,
            "congestion_ratio": 0.0,
        }
        self._fill_events_recent.clear()
        self._fill_duration_events.clear()
        self._spread_realization_events.clear()

    def _toggle_side_effect_position_ledger_runtime(self) -> None:
        enabled = self._flag_value("POSITION_LEDGER_ENABLED")
        self._position_ledger.enabled = bool(enabled)
        if enabled:
            return
        now_ts = _now()
        for state in list(self._churner_by_slot.values()):
            self._churner_reset_state(state, now_ts=now_ts, reason="position_ledger_disabled")
        self._reconcile_churner_state()

    def _toggle_side_effect_dust_sweep_runtime(self) -> None:
        self._dust_sweep_enabled = self._flag_value("DUST_SWEEP_ENABLED")

    def _regime_entry_spacing_multipliers(self) -> tuple[float, float]:
        """
        Runtime policy hook for Tier-1 asymmetric spacing.

        Returns A/B entry spacing multipliers. Shadow mode must remain non-actuating.
        """
        if self._flag_value("KNOB_MODE_ENABLED") and bool(getattr(self._action_knobs, "enabled", False)):
            return (
                max(0.10, min(3.0, float(self._action_knobs.spacing_a))),
                max(0.10, min(3.0, float(self._action_knobs.spacing_b))),
            )

        if not self._flag_value("REGIME_DIRECTIONAL_ENABLED"):
            return 1.0, 1.0
        if int(self._regime_tier) < 1:
            return 1.0, 1.0

        state = dict(self._regime_shadow_state or {})
        policy_regime, policy_confidence, policy_bias_signal, _, _ = self._policy_hmm_signal()
        regime = str(state.get("regime", policy_regime)).upper()
        if regime not in {"BULLISH", "BEARISH"}:
            return 1.0, 1.0

        hmm_mod = self._hmm_module
        if not hmm_mod or not hasattr(hmm_mod, "compute_grid_bias"):
            return 1.0, 1.0

        confidence = float(state.get("confidence", policy_confidence))
        bias_signal = float(state.get("bias_signal", policy_bias_signal))
        regime_stub = SimpleNamespace(confidence=confidence, bias_signal=bias_signal)
        try:
            bias = hmm_mod.compute_grid_bias(regime_stub)
            mult_a = float(bias.get("entry_spacing_mult_a", 1.0) or 1.0)
            mult_b = float(bias.get("entry_spacing_mult_b", 1.0) or 1.0)
        except Exception as e:
            logger.debug("Regime spacing bias unavailable: %s", e)
            return 1.0, 1.0

        if not isfinite(mult_a) or mult_a <= 0:
            mult_a = 1.0
        if not isfinite(mult_b) or mult_b <= 0:
            mult_b = 1.0

        return max(0.10, min(3.0, mult_a)), max(0.10, min(3.0, mult_b))

    def _recovery_orders_enabled(self) -> bool:
        return self._flag_value("RECOVERY_ORDERS_ENABLED")

    def _engine_cfg(self, slot: SlotRuntime) -> sm.EngineConfig:
        spacing_mult_a, spacing_mult_b = self._regime_entry_spacing_multipliers()
        base_entry_pct = float(self.entry_pct)
        recovery_orders_enabled = self._recovery_orders_enabled()
        knob_cadence = 1.0
        if self._flag_value("KNOB_MODE_ENABLED") and bool(getattr(self._action_knobs, "enabled", False)):
            knob_cadence = max(0.05, float(getattr(self._action_knobs, "cadence_mult", 1.0) or 1.0))
        s1_orphan_after_sec = (
            float(config.S1_ORPHAN_AFTER_SEC) * knob_cadence
            if recovery_orders_enabled
            else float("inf")
        )
        s2_orphan_after_sec = (
            float(config.S2_ORPHAN_AFTER_SEC) * knob_cadence
            if recovery_orders_enabled
            else float("inf")
        )
        if self._slot_has_active_belief_override(int(slot.slot_id), now=_now()):
            s1_orphan_after_sec = float("inf")
            s2_orphan_after_sec = float("inf")
        return sm.EngineConfig(
            entry_pct=base_entry_pct,
            entry_pct_a=base_entry_pct * spacing_mult_a,
            entry_pct_b=base_entry_pct * spacing_mult_b,
            profit_pct=self.profit_pct,
            refresh_pct=config.PAIR_REFRESH_PCT,
            order_size_usd=self._slot_order_size_usd(slot),
            price_decimals=int(self.constraints.get("price_decimals", 6)),
            volume_decimals=int(self.constraints.get("volume_decimals", 0)),
            min_volume=float(self.constraints.get("min_volume", 13.0)),
            min_cost_usd=float(self.constraints.get("min_cost_usd", 0.0)),
            maker_fee_pct=float(self.maker_fee_pct),
            stale_price_max_age_sec=float(config.STALE_PRICE_MAX_AGE_SEC),
            s1_orphan_after_sec=s1_orphan_after_sec,
            s2_orphan_after_sec=s2_orphan_after_sec,
            loss_backoff_start=int(config.LOSS_BACKOFF_START),
            loss_cooldown_start=int(config.LOSS_COOLDOWN_START),
            loss_cooldown_sec=float(config.LOSS_COOLDOWN_SEC),
            reentry_base_cooldown_sec=float(config.REENTRY_BASE_COOLDOWN_SEC),
            backoff_factor=float(config.ENTRY_BACKOFF_FACTOR),
            backoff_max_multiplier=float(config.ENTRY_BACKOFF_MAX_MULTIPLIER),
            max_recovery_slots=max(1, int(config.MAX_RECOVERY_SLOTS)),
            sticky_mode_enabled=self._flag_value("STICKY_MODE_ENABLED"),
        )

    def _allocate_slot_alias(self, used_aliases: set[str] | None = None) -> str:
        used = set(used_aliases or set())
        if used_aliases is None:
            for slot in self.slots.values():
                alias = str(slot.alias or "").strip().lower()
                if alias:
                    used.add(alias)

        pool = [str(a).strip().lower() for a in self.slot_alias_pool if str(a).strip()]
        if not pool:
            pool = ["wow"]

        recycled_set = {str(a).strip().lower() for a in self.slot_alias_recycle_queue if str(a).strip()}
        for alias in pool:
            if alias not in used and alias not in recycled_set:
                return alias

        while self.slot_alias_recycle_queue:
            alias = str(self.slot_alias_recycle_queue.popleft()).strip().lower()
            if not alias or alias in used:
                continue
            return alias

        while True:
            alias = f"doge-{self.slot_alias_fallback_counter:02d}"
            self.slot_alias_fallback_counter += 1
            if alias not in used:
                return alias

    def _release_slot_alias(self, alias: str) -> None:
        norm = str(alias or "").strip().lower()
        if not norm:
            return
        if norm not in self.slot_alias_pool:
            return
        if norm in self.slot_alias_recycle_queue:
            return
        self.slot_alias_recycle_queue.append(norm)

    def _slot_label(self, slot: SlotRuntime) -> str:
        alias = str(slot.alias or "").strip().lower()
        if alias:
            return alias
        return f"slot-{slot.slot_id}"

    def _sanitize_slot_alias_state(self) -> None:
        pool = [str(a).strip().lower() for a in self.slot_alias_pool if str(a).strip()]
        if not pool:
            pool = ["wow"]
        self.slot_alias_pool = tuple(pool)

        cleaned_queue: deque[str] = deque()
        seen_queue: set[str] = set()
        for raw in list(self.slot_alias_recycle_queue):
            alias = str(raw).strip().lower()
            if not alias or alias not in self.slot_alias_pool or alias in seen_queue:
                continue
            cleaned_queue.append(alias)
            seen_queue.add(alias)
        self.slot_alias_recycle_queue = cleaned_queue

        used: set[str] = set()
        for sid in sorted(self.slots.keys()):
            slot = self.slots[sid]
            alias = str(slot.alias or "").strip().lower()
            if alias and alias not in used:
                slot.alias = alias
                used.add(alias)
                continue
            slot.alias = self._allocate_slot_alias(used_aliases=used)
            used.add(slot.alias)

        self.slot_alias_recycle_queue = deque(a for a in self.slot_alias_recycle_queue if a not in used)

    def _capital_layer_step_doge_eq(self) -> float:
        return max(0.0, float(config.CAPITAL_LAYER_DOGE_PER_ORDER)) * max(1, int(config.CAPITAL_LAYER_ORDER_BUDGET))

    def _layer_mark_price(self, slot: SlotRuntime | None = None) -> float:
        if slot is not None:
            px = float(slot.state.market_price or 0.0)
            if px > 0:
                return px
        if self.last_price > 0:
            return float(self.last_price)
        return 0.0

    def _available_free_balances(self, *, prefer_fresh: bool = False) -> tuple[float, float]:
        if prefer_fresh:
            bal = self._safe_balance()
            if bal is not None:
                return max(0.0, _usd_balance(bal)), max(0.0, _doge_balance(bal))

        if self._loop_available_usd is not None and self._loop_available_doge is not None:
            return max(0.0, float(self._loop_available_usd)), max(0.0, float(self._loop_available_doge))

        if self.ledger._synced:
            return max(0.0, float(self.ledger.available_usd)), max(0.0, float(self.ledger.available_doge))

        if self._last_balance_snapshot:
            return (
                max(0.0, _usd_balance(self._last_balance_snapshot)),
                max(0.0, _doge_balance(self._last_balance_snapshot)),
            )
        return 0.0, 0.0

    def _active_order_side_counts(self) -> tuple[int, int, int]:
        sells = 0
        buys = 0
        total = 0
        for slot in self.slots.values():
            for o in slot.state.orders:
                if not o.txid:
                    continue
                total += 1
                if o.side == "sell":
                    sells += 1
                elif o.side == "buy":
                    buys += 1
            for r in slot.state.recovery_orders:
                if not r.txid:
                    continue
                total += 1
                if r.side == "sell":
                    sells += 1
                elif r.side == "buy":
                    buys += 1
        return sells, buys, total

    def _recompute_effective_layers(self, mark_price: float | None = None) -> dict[str, float | int | None]:
        doge_per_order = max(0.0, float(config.CAPITAL_LAYER_DOGE_PER_ORDER))
        layer_order_budget = max(1, int(config.CAPITAL_LAYER_ORDER_BUDGET))
        layer_step_doge_eq = doge_per_order * float(layer_order_budget)

        price = float(mark_price or 0.0)
        if price <= 0:
            price = self._layer_mark_price()

        free_usd, free_doge = self._available_free_balances(prefer_fresh=False)
        active_sell_orders, active_buy_orders, open_orders_total = self._active_order_side_counts()
        sell_den = max(1, active_sell_orders)
        buy_den = max(1, active_buy_orders)
        buffer = max(1.0, float(config.CAPITAL_LAYER_BALANCE_BUFFER))

        if doge_per_order <= 0:
            max_layers_from_doge = 0
            max_layers_from_usd = 0
        else:
            max_layers_from_doge = int(floor(free_doge / (sell_den * doge_per_order * buffer)))
            if price > 0:
                max_layers_from_usd = int(floor(free_usd / (buy_den * doge_per_order * price * buffer)))
            else:
                max_layers_from_usd = 0

        target_layers = max(0, int(self.target_layers))
        effective_layers = max(0, min(target_layers, max_layers_from_doge, max_layers_from_usd))
        self.effective_layers = int(effective_layers)

        gap_layers = max(0, target_layers - effective_layers)
        gap_doge_now = max(0.0, (target_layers - max_layers_from_doge) * sell_den * doge_per_order)
        gap_usd_now = max(0.0, (target_layers - max_layers_from_usd) * buy_den * doge_per_order * max(price, 0.0))

        return {
            "target_layers": target_layers,
            "effective_layers": effective_layers,
            "doge_per_order_per_layer": doge_per_order,
            "layer_order_budget": layer_order_budget,
            "layer_step_doge_eq": layer_step_doge_eq,
            "mark_price": price if price > 0 else None,
            "add_layer_usd_equiv_now": (layer_step_doge_eq * price) if price > 0 else None,
            "active_sell_orders": active_sell_orders,
            "active_buy_orders": active_buy_orders,
            "open_orders_total": open_orders_total,
            "max_layers_from_doge": max_layers_from_doge,
            "max_layers_from_usd": max_layers_from_usd,
            "gap_layers": gap_layers,
            "gap_doge_now": gap_doge_now,
            "gap_usd_now": gap_usd_now,
            "free_usd": free_usd,
            "free_doge": free_doge,
        }

    def _current_layer_metrics(self, mark_price: float | None = None) -> dict[str, float | int | None]:
        if self._loop_effective_layers is not None:
            return self._loop_effective_layers
        self._loop_effective_layers = self._recompute_effective_layers(mark_price=mark_price)
        return self._loop_effective_layers

    def _count_orders_at_funded_size(self) -> int:
        matched = 0
        for slot in self.slots.values():
            cfg = self._engine_cfg(slot)
            vol_decimals = int(cfg.volume_decimals)
            if vol_decimals <= 0:
                tol = 0.5
            else:
                tol = 0.5 * (10 ** (-vol_decimals))

            for o in slot.state.orders:
                if not o.txid:
                    continue
                trade = o.trade_id if o.trade_id in ("A", "B") else None
                target_usd = self._slot_order_size_usd(slot, trade_id=trade, price_override=float(o.price))
                expected_vol = sm.compute_order_volume(float(o.price), cfg, float(target_usd))
                if expected_vol is None:
                    continue
                if abs(float(o.volume) - float(expected_vol)) <= tol + 1e-12:
                    matched += 1

            for r in slot.state.recovery_orders:
                if not r.txid:
                    continue
                trade = r.trade_id if r.trade_id in ("A", "B") else None
                target_usd = self._slot_order_size_usd(slot, trade_id=trade, price_override=float(r.price))
                expected_vol = sm.compute_order_volume(float(r.price), cfg, float(target_usd))
                if expected_vol is None:
                    continue
                if abs(float(r.volume) - float(expected_vol)) <= tol + 1e-12:
                    matched += 1
        return matched

    def _slot_wants_buy_entry(self, slot: SlotRuntime) -> bool:
        """True when slot can and should place a new B-side buy entry now."""
        st = slot.state
        if bool(st.short_only):
            return False
        # A txid-bound buy entry is already working on Kraken.
        # Unbound local entries still count as "wants buy" until placed.
        if any(o.role == "entry" and o.side == "buy" and bool(o.txid) for o in st.orders):
            return False
        phase = sm.derive_phase(st)
        return phase in ("S0", "S1a")

    def _committed_buy_quote_usd(self) -> float:
        """USD currently committed by live buy-side orders across all slots."""
        committed = 0.0
        for slot in self.slots.values():
            st = slot.state
            for o in st.orders:
                if o.side == "buy" and bool(o.txid):
                    committed += max(0.0, float(o.volume) * float(o.price))
            for r in st.recovery_orders:
                if r.side == "buy" and bool(r.txid):
                    committed += max(0.0, float(r.volume) * float(r.price))
        return committed

    def _compute_dust_dividend(self) -> float:
        """Per-slot USD surplus that can be folded into B-side sizing."""
        if bool(getattr(config, "QUOTE_FIRST_ALLOCATION", False)):
            self._loop_dust_dividend = 0.0
            self._dust_last_dividend_usd = 0.0
            return 0.0
        if self._loop_dust_dividend is not None:
            return max(0.0, float(self._loop_dust_dividend))

        available = 0.0
        if self._loop_available_usd is not None:
            available = max(0.0, float(self._loop_available_usd))
        elif self.ledger._synced:
            available = max(0.0, float(self.ledger.available_usd))
        else:
            self._loop_dust_dividend = 0.0
            self._dust_last_dividend_usd = 0.0
            return 0.0

        if available <= 0.0:
            self._loop_dust_dividend = 0.0
            self._dust_last_dividend_usd = 0.0
            return 0.0

        reserved = 0.0
        buy_count = 0
        for slot in self.slots.values():
            if not self._slot_wants_buy_entry(slot):
                continue
            buy_count += 1
        if buy_count > 0:
            # Reserve buy-ready slots at their account-aware B-side baseline.
            # This captures the slot-count split and leaves only true surplus as dividend.
            reserved = max(0.0, float(self._b_side_base_usd())) * buy_count

        surplus = available - reserved
        if buy_count <= 0 or surplus < float(self._dust_min_threshold_usd):
            self._loop_dust_dividend = 0.0
            self._dust_last_dividend_usd = 0.0
            return 0.0

        dividend = max(0.0, surplus / buy_count)
        if not isfinite(dividend):
            dividend = 0.0
        self._loop_dust_dividend = dividend
        self._dust_last_dividend_usd = dividend
        return dividend

    def _dust_bump_usd(self, slot: SlotRuntime, trade_id: str | None) -> float:
        if trade_id != "B" or not self._dust_sweep_enabled:
            return 0.0
        if not self._slot_wants_buy_entry(slot):
            return 0.0
        if not self.ledger._synced and self._loop_available_usd is None:
            return 0.0

        dividend = max(0.0, float(self._compute_dust_dividend()))
        if dividend <= 0.0:
            return 0.0

        # DUST_MAX_BUMP_PCT <= 0 means uncapped (fund guard is the safety net).
        cap_pct = max(0.0, float(self._dust_max_bump_pct))
        if cap_pct > 0.0:
            base_no_dust = max(0.0, float(self._b_side_base_usd()))
            if base_no_dust <= 0.0:
                return 0.0
            max_bump = base_no_dust * (cap_pct / 100.0)
            return min(dividend, max_bump)

        return dividend

    def _b_side_base_usd(self) -> float:
        """Per-slot B-side base sizing.

        Legacy path: divide available USD across all slots.
        Quote-first path (QUOTE_FIRST_ALLOCATION): allocate only across buy-ready
        slots with committed-buy subtraction and carry recycling.
        """
        if self._loop_b_side_base is not None:
            return self._loop_b_side_base

        available = 0.0
        if self._loop_available_usd is not None:
            available = max(0.0, float(self._loop_available_usd))
        elif self.ledger._synced:
            available = max(0.0, float(self.ledger.available_usd))
        else:
            self._loop_b_side_base = float(config.ORDER_SIZE_USD)
            return self._loop_b_side_base

        if bool(getattr(config, "QUOTE_FIRST_ALLOCATION", False)):
            buy_ready_slots = sum(1 for slot in self.slots.values() if self._slot_wants_buy_entry(slot))
            committed_buy_quote = self._committed_buy_quote_usd()
            safety_buffer = max(0.0, float(getattr(config, "ALLOCATION_SAFETY_BUFFER_USD", 0.50)))
            deployable_usd = max(0.0, available - committed_buy_quote - safety_buffer)
            carry_in = max(0.0, float(self._quote_first_carry_usd))
            allocation_pool = deployable_usd + carry_in

            if buy_ready_slots > 0 and allocation_pool > 0.0:
                per_slot = floor((allocation_pool * 100.0) / buy_ready_slots) / 100.0
                if not isfinite(per_slot) or per_slot < 0.0:
                    per_slot = 0.0
                allocated = per_slot * buy_ready_slots
                carry_out = max(0.0, allocation_pool - allocated)
            else:
                per_slot = 0.0
                allocated = 0.0
                carry_out = max(0.0, allocation_pool)

            self._quote_first_carry_usd = carry_out
            self._loop_b_side_base = per_slot
            self._loop_quote_first_meta = {
                "enabled": True,
                "buy_ready_slots": int(buy_ready_slots),
                "committed_buy_quote_usd": float(committed_buy_quote),
                "deployable_usd": float(deployable_usd),
                "allocation_pool_usd": float(allocation_pool),
                "allocated_usd": float(allocated),
                "carry_usd": float(carry_out),
                "unallocated_spendable_usd": float(max(0.0, deployable_usd - allocated)),
            }
            return self._loop_b_side_base

        n_slots = max(1, len(self.slots))
        base = available / n_slots
        self._loop_quote_first_meta = None
        self._loop_b_side_base = max(float(config.ORDER_SIZE_USD), base)
        return self._loop_b_side_base

    def _slot_order_size_usd(
        self,
        slot: SlotRuntime,
        trade_id: str | None = None,
        price_override: float | None = None,
        include_dust: bool = True,
    ) -> float:
        base_order = float(config.ORDER_SIZE_USD)
        if trade_id == "B":
            # Account-aware: divide available USD evenly across all slots.
            base = self._b_side_base_usd()
        elif self._flag_value("STICKY_MODE_ENABLED") and str(getattr(config, "STICKY_COMPOUNDING_MODE", "legacy_profit")).strip().lower() == "fixed":
            base = max(base_order, base_order)
        else:
            # Independent compounding per slot (A-side and baseline queries).
            base = max(base_order, base_order + slot.state.total_profit)
        layer_metrics = self._current_layer_metrics(mark_price=self._layer_mark_price(slot))
        effective_layers = int(layer_metrics.get("effective_layers", 0))
        layer_usd = 0.0
        layer_price = float(price_override) if price_override is not None else self._layer_mark_price(slot)
        if layer_price <= 0:
            layer_price = self._layer_mark_price(slot)
        if layer_price > 0:
            layer_usd = effective_layers * max(0.0, float(config.CAPITAL_LAYER_DOGE_PER_ORDER)) * layer_price
        base_with_layers = max(base, base + layer_usd)
        # Dust sweep for B-side: allocate current free-USD surplus across slots
        # that are actively waiting for a fresh buy entry this loop.
        if include_dust and trade_id == "B" and not bool(getattr(config, "QUOTE_FIRST_ALLOCATION", False)):
            dust_bump = self._dust_bump_usd(slot, trade_id=trade_id)
            base_with_layers = max(0.0, base_with_layers + dust_bump)
        knobs_active = self._flag_value("KNOB_MODE_ENABLED") and bool(
            getattr(self._action_knobs, "enabled", False)
        )
        aggression_mult = 1.0
        suppression_mult = 1.0
        if knobs_active:
            aggression_mult = max(0.0, float(getattr(self._action_knobs, "aggression", 1.0) or 1.0))
            direction = float(getattr(self._belief_state, "direction_score", 0.0) or 0.0)
            suppress = max(0.0, min(1.0, float(getattr(self._action_knobs, "suppression_strength", 0.0) or 0.0)))
            t = str(trade_id or "").strip().upper()
            against_trend = (direction > 1e-9 and t == "A") or (direction < -1e-9 and t == "B")
            if against_trend:
                suppression_mult = max(0.0, 1.0 - suppress)
        if self._throughput is not None:
            if knobs_active:
                throughput_usd, _ = self._throughput.size_for_slot(
                    base_with_layers,
                    regime_label=self._regime_label(self._current_regime_id()),
                    trade_id=trade_id,
                    aggression_mult=aggression_mult,
                )
            else:
                throughput_usd, _ = self._throughput.size_for_slot(
                    base_with_layers,
                    regime_label=self._regime_label(self._current_regime_id()),
                    trade_id=trade_id,
                )
            base_with_layers = max(0.0, float(throughput_usd))
        elif knobs_active:
            base_with_layers *= aggression_mult
        base_with_layers *= suppression_mult
        if trade_id is None or not self._flag_value("REBALANCE_ENABLED"):
            return base_with_layers

        skew = float(self._rebalancer_current_skew)
        if abs(skew) <= 1e-12:
            return base_with_layers

        favored = (skew > 0 and trade_id == "B") or (skew < 0 and trade_id == "A")
        if not favored:
            return base_with_layers

        sensitivity = max(0.0, float(config.REBALANCE_SIZE_SENSITIVITY))
        max_mult = max(1.0, float(config.REBALANCE_MAX_SIZE_MULT))
        mult = min(max_mult, 1.0 + abs(skew) * sensitivity)
        effective = base_with_layers * mult

        # Fund guard: scaling should not make an already-viable side non-viable.
        if skew > 0 and trade_id == "B":
            available_usd: float | None = None
            if self._loop_available_usd is not None:
                available_usd = float(self._loop_available_usd)
            elif self.ledger._synced:
                available_usd = float(self.ledger.available_usd)
            if available_usd is not None:
                max_safe = max(base_with_layers, available_usd - base_with_layers)
                effective = min(effective, max_safe)
        elif skew < 0 and trade_id == "A":
            price = float(slot.state.market_price or self.last_price)
            if price > 0:
                available_doge: float | None = None
                if self._loop_available_doge is not None:
                    available_doge = float(self._loop_available_doge)
                elif self.ledger._synced:
                    available_doge = float(self.ledger.available_doge)
                if available_doge is not None:
                    base_doge = base_with_layers / price
                    max_safe_doge = max(base_doge, available_doge - base_doge)
                    effective = min(effective, max_safe_doge * price)

        return max(base_with_layers, effective)

    def _minimum_bootstrap_requirements(self, market_price: float) -> tuple[float, float]:
        min_vol = float(self.constraints.get("min_volume", 13.0))
        min_cost = float(self.constraints.get("min_cost_usd", 0.0))
        if min_cost <= 0 and market_price > 0:
            min_cost = min_vol * market_price
        return min_vol, min_cost

    def _order_matches_runtime_pair(self, row: dict) -> bool:
        # OpenOrders rows typically carry pair under descr.pair.
        descr = row.get("descr", {}) if isinstance(row, dict) else {}
        pair_name = ""
        if isinstance(descr, dict):
            pair_name = str(descr.get("pair") or descr.get("pairname") or "").upper()
        if not pair_name and isinstance(row, dict):
            pair_name = str(row.get("pair") or "").upper()
        if not pair_name:
            # If pair metadata is missing, count conservatively.
            return True

        target = self.pair.upper()
        target_norm = target.replace("/", "")
        pair_norm = pair_name.replace("/", "")
        alt = target.replace("USD", "/USD")
        return pair_name in {target, alt} or pair_norm == target_norm

    def _count_pair_open_orders(self, open_orders: dict) -> int:
        if not isinstance(open_orders, dict):
            return 0
        count = 0
        for row in open_orders.values():
            if not isinstance(row, dict) or self._order_matches_runtime_pair(row):
                count += 1
        return count

    def _compute_capacity_health(self, now: float | None = None) -> dict:
        now = now or _now()
        self._trim_rolling_telemetry(now)

        internal_open_orders_current = self._internal_open_order_count()
        kraken_open_orders_current = self._kraken_open_orders_current
        if kraken_open_orders_current is None:
            open_orders_current = internal_open_orders_current
            open_orders_source = "internal_fallback"
        else:
            open_orders_current = int(kraken_open_orders_current)
            open_orders_source = "kraken"

        pair_open_order_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
        safety_ratio = min(1.0, max(0.1, float(config.OPEN_ORDER_SAFETY_RATIO)))
        open_orders_safe_cap = max(1, int(pair_open_order_limit * safety_ratio))
        open_order_headroom = open_orders_safe_cap - open_orders_current
        open_order_utilization_pct = (
            open_orders_current / open_orders_safe_cap * 100.0 if open_orders_safe_cap > 0 else 0.0
        )
        orders_per_slot_estimate = (open_orders_current / len(self.slots)) if self.slots else None
        estimated_slots_remaining = 0
        if orders_per_slot_estimate and orders_per_slot_estimate > 0 and open_order_headroom > 0:
            estimated_slots_remaining = int(open_order_headroom // orders_per_slot_estimate)

        partial_fill_open_events_1d = len(self._partial_fill_open_events)
        partial_fill_cancel_events_1d = len(self._partial_fill_cancel_events)
        median_fill_seconds_1d, p95_fill_seconds_1d = self._fill_duration_stats_1d()

        if partial_fill_cancel_events_1d > 0 or open_order_headroom < 10:
            status_band = "stop"
        elif open_order_headroom < 20:
            status_band = "caution"
        else:
            status_band = "normal"

        return {
            "open_orders_current": open_orders_current,
            "open_orders_source": open_orders_source,
            "open_orders_internal": internal_open_orders_current,
            "open_orders_kraken": kraken_open_orders_current,
            "open_orders_drift": (
                None
                if kraken_open_orders_current is None
                else int(kraken_open_orders_current) - internal_open_orders_current
            ),
            "open_order_limit_configured": pair_open_order_limit,
            "open_orders_safe_cap": open_orders_safe_cap,
            "open_order_headroom": open_order_headroom,
            "open_order_utilization_pct": open_order_utilization_pct,
            "orders_per_slot_estimate": orders_per_slot_estimate,
            "estimated_slots_remaining": estimated_slots_remaining,
            "partial_fill_open_events_1d": partial_fill_open_events_1d,
            "partial_fill_cancel_events_1d": partial_fill_cancel_events_1d,
            "median_fill_seconds_1d": median_fill_seconds_1d,
            "p95_fill_seconds_1d": p95_fill_seconds_1d,
            "status_band": status_band,
        }

    def _pending_entry_orders(self) -> list[tuple[int, sm.OrderState]]:
        pending: list[tuple[int, sm.OrderState]] = []
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            for o in st.orders:
                if o.role == "entry" and not o.txid:
                    pending.append((sid, o))
        pending.sort(key=lambda row: (float(row[1].placed_at or 0.0), int(row[0]), int(row[1].local_id)))
        return pending

    def _compute_entry_adds_loop_cap(self) -> int:
        base_cap = max(1, int(config.MAX_ENTRY_ADDS_PER_LOOP))
        try:
            capacity = self._compute_capacity_health()
            headroom = int(capacity.get("open_order_headroom") or 0)
        except Exception:
            return base_cap

        # Tighten entry velocity as we approach order-cap pressure.
        if headroom <= 5:
            cap = 1
        elif headroom <= 10:
            cap = min(base_cap, 2)
        elif headroom <= 20:
            cap = min(base_cap, 3)
        else:
            cap = base_cap

        if not (
            self._flag_value("MTS_ENABLED")
            and self._flag_value("MTS_ENTRY_THROTTLE_ENABLED")
        ):
            return int(cap)

        if not bool(getattr(self._manifold_score, "enabled", False)):
            return int(cap)

        mts_floor = max(0.0, min(1.0, float(getattr(config, "MTS_ENTRY_THROTTLE_FLOOR", 0.3))))
        mts_value = max(0.0, min(1.0, float(getattr(self._manifold_score, "mts", 0.0) or 0.0)))
        if mts_value < mts_floor:
            return 0
        scaled = int(floor(float(cap) * mts_value))
        return max(1, min(int(cap), scaled))

    def _defer_entry_due_scheduler(self, slot_id: int, action: sm.PlaceOrderAction, source: str) -> None:
        self._entry_adds_deferred_total += 1
        self._entry_adds_last_deferred_at = _now()
        logger.info(
            "entry_scheduler: deferred %s %s [%s.%s] slot=%s local=%s (cap %d/loop reached via %s)",
            action.role,
            action.side,
            action.trade_id,
            action.cycle,
            slot_id,
            action.local_id,
            self.entry_adds_per_loop_cap,
            source,
        )

    def _drain_pending_entry_orders(self, source: str, *, skip_stale: bool = False) -> None:
        if self.mode in ("PAUSED", "HALTED"):
            return
        if self.entry_adds_per_loop_used >= self.entry_adds_per_loop_cap:
            return
        if self._price_age_sec() > config.STALE_PRICE_MAX_AGE_SEC:
            return

        max_drift_pct = max(0.05, float(config.PAIR_REFRESH_PCT))
        pending = self._pending_entry_orders()
        if not pending:
            return

        # Purge suppressed-side deferred entries during Tier 2 after grace.
        if self._flag_value("REGIME_DIRECTIONAL_ENABLED") and self._regime_grace_elapsed(_now()):
            suppressed = self._regime_side_suppressed
            if suppressed in ("A", "B"):
                suppressed_side = "sell" if suppressed == "A" else "buy"
                purged = 0
                kept: list[tuple[int, sm.OrderState]] = []
                for sid, order in pending:
                    if order.side == suppressed_side:
                        slot = self.slots.get(sid)
                        if slot is not None:
                            current = sm.find_order(slot.state, order.local_id)
                            if current is not None and current.role == "entry" and not current.txid:
                                slot.state = sm.remove_order(slot.state, order.local_id)
                        purged += 1
                    else:
                        kept.append((sid, order))
                if purged > 0:
                    logger.info(
                        "entry_scheduler: purged %d suppressed-side (%s) deferred entries",
                        purged,
                        suppressed,
                    )
                pending = kept
                if not pending:
                    return

        drained = 0
        for sid, order in pending:
            if self.entry_adds_per_loop_used >= self.entry_adds_per_loop_cap:
                break
            slot = self.slots.get(sid)
            if slot is None:
                continue
            current = sm.find_order(slot.state, order.local_id)
            if current is None or current.role != "entry" or current.txid:
                continue

            if skip_stale and self.last_price > 0:
                drift = abs(current.price - self.last_price) / self.last_price * 100.0
                if drift > max_drift_pct:
                    continue

            action = sm.PlaceOrderAction(
                local_id=current.local_id,
                side=current.side,
                role="entry",
                price=current.price,
                volume=current.volume,
                trade_id=current.trade_id,
                cycle=current.cycle,
                reason="entry_scheduler_drain",
            )
            before = self.entry_adds_per_loop_used
            self._execute_actions(sid, [action], source)
            if self.entry_adds_per_loop_used > before:
                drained += 1

        if drained > 0:
            self._entry_adds_drained_total += drained
            self._entry_adds_last_drained_at = _now()
            logger.info(
                "entry_scheduler: drained %d pending entries via %s (used %d/%d this loop)",
                drained,
                source,
                self.entry_adds_per_loop_used,
                self.entry_adds_per_loop_cap,
            )

    def _trim_rolling_telemetry(self, now: float | None = None) -> None:
        now = now or _now()
        cutoff = now - 86400.0
        while self._partial_fill_open_events and self._partial_fill_open_events[0] < cutoff:
            self._partial_fill_open_events.popleft()
        while self._partial_fill_cancel_events and self._partial_fill_cancel_events[0] < cutoff:
            self._partial_fill_cancel_events.popleft()
        while self._fill_durations_1d and self._fill_durations_1d[0][0] < cutoff:
            self._fill_durations_1d.popleft()
        fill_window = max(30.0, float(getattr(config, "FILL_IMBALANCE_WINDOW_SEC", 300)))
        fill_cutoff = now - fill_window
        while self._fill_events_recent and self._fill_events_recent[0][0] < fill_cutoff:
            self._fill_events_recent.popleft()
        deriv_long = max(
            60.0,
            float(
                getattr(
                    config,
                    "FILL_TIME_DERIVATIVE_LONG_SEC",
                    1800,
                )
            ),
        )
        deriv_cutoff = now - deriv_long
        while self._fill_duration_events and self._fill_duration_events[0][0] < deriv_cutoff:
            self._fill_duration_events.popleft()
        spread_cutoff = now - 86400.0
        while self._spread_realization_events and self._spread_realization_events[0][0] < spread_cutoff:
            self._spread_realization_events.popleft()
        while len(self._spread_realization_events) > 200:
            self._spread_realization_events.popleft()
        while self._doge_eq_snapshots and self._doge_eq_snapshots[0][0] < cutoff:
            self._doge_eq_snapshots.popleft()

    def _record_partial_fill_open(self, ts: float | None = None) -> None:
        ts = ts or _now()
        self._partial_fill_open_events.append(ts)
        self._trim_rolling_telemetry(ts)

    def _record_partial_fill_cancel(self, ts: float | None = None) -> None:
        ts = ts or _now()
        self._partial_fill_cancel_events.append(ts)
        self._trim_rolling_telemetry(ts)

    def _record_fill_duration(self, duration_sec: float, ts: float | None = None) -> None:
        ts = ts or _now()
        dur = max(0.0, float(duration_sec))
        self._fill_durations_1d.append((ts, dur))
        self._fill_duration_events.append((ts, dur))
        self._trim_rolling_telemetry(ts)

    def _fill_duration_stats_1d(self) -> tuple[float | None, float | None]:
        vals = [d for _, d in self._fill_durations_1d if d >= 0]
        if not vals:
            return None, None
        med = float(median(vals))
        ordered = sorted(vals)
        idx = max(0, min(len(ordered) - 1, ceil(0.95 * len(ordered)) - 1))
        return med, float(ordered[idx])

    @staticmethod
    def _normalize_prob_triplet(raw: Any) -> list[float]:
        vals: list[Any]
        if isinstance(raw, dict):
            vals = [
                raw.get("bearish", 0.0),
                raw.get("ranging", 0.0),
                raw.get("bullish", 0.0),
            ]
        elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
            vals = [raw[0], raw[1], raw[2]]
        else:
            vals = [0.0, 1.0, 0.0]
        clean: list[float] = []
        for value in vals:
            try:
                vv = float(value or 0.0)
            except (TypeError, ValueError):
                vv = 0.0
            if not isfinite(vv):
                vv = 0.0
            clean.append(max(0.0, vv))
        total = float(sum(clean))
        if total <= 1e-12:
            return [0.0, 1.0, 0.0]
        return [clean[0] / total, clean[1] / total, clean[2] / total]

    def _record_fill_event(self, trade_id: str | None, ts: float | None = None) -> None:
        side = str(trade_id or "").strip().upper()
        if side not in {"A", "B"}:
            return
        when = float(ts if ts is not None else _now())
        self._fill_events_recent.append((when, side))
        self._trim_rolling_telemetry(when)

    def _record_spread_realization(self, value: float, ts: float | None = None) -> None:
        when = float(ts if ts is not None else _now())
        val = float(value)
        if not isfinite(val) or val <= 0.0:
            return
        self._spread_realization_events.append((when, val))
        self._trim_rolling_telemetry(when)

    def _update_micro_features(self, now: float | None = None) -> dict[str, float]:
        now_ts = float(now if now is not None else _now())
        self._trim_rolling_telemetry(now_ts)

        # fill_imbalance in [-1, 1]
        a_fills = 0
        b_fills = 0
        for _ts, side in self._fill_events_recent:
            if side == "A":
                a_fills += 1
            elif side == "B":
                b_fills += 1
        denom = max(1, a_fills + b_fills)
        fill_imbalance = max(-1.0, min(1.0, float(a_fills - b_fills) / float(denom)))

        # spread_realization as rolling mean of recent cycle realizations.
        if self._spread_realization_events:
            vals = [float(v) for _, v in list(self._spread_realization_events)[-20:] if float(v) > 0.0]
            spread_realization = float(sum(vals) / len(vals)) if vals else 1.0
        else:
            spread_realization = 1.0
        spread_realization = max(0.0, spread_realization)

        # fill_time_derivative compares short-window vs long-window median fill time.
        short_sec = max(60.0, float(getattr(config, "FILL_TIME_DERIVATIVE_SHORT_SEC", 300)))
        long_sec = max(
            short_sec,
            float(getattr(config, "FILL_TIME_DERIVATIVE_LONG_SEC", 1800)),
        )
        short_vals = [d for ts, d in self._fill_duration_events if (now_ts - ts) <= short_sec and d >= 0.0]
        long_vals = [d for ts, d in self._fill_duration_events if (now_ts - ts) <= long_sec and d >= 0.0]
        fill_time_derivative = 0.0
        if short_vals and long_vals:
            med_short = float(median(short_vals))
            med_long = float(median(long_vals))
            if med_long > 1e-9:
                fill_time_derivative = (med_short - med_long) / med_long
        fill_time_derivative = max(-1.0, min(1.0, float(fill_time_derivative)))

        # congestion_ratio = fraction of open exits older than p75 fill duration.
        open_exits = self._collect_open_exits(now_ts=now_ts)
        ages = [max(0.0, float(row.get("age_sec", 0.0) or 0.0)) for row in open_exits]
        congestion_ratio = 0.0
        if ages:
            hist = [d for _, d in self._fill_durations_1d if d >= 0.0]
            if hist:
                ordered = sorted(hist)
                idx = max(0, min(len(ordered) - 1, ceil(0.75 * len(ordered)) - 1))
                p75 = max(1.0, float(ordered[idx]))
            else:
                p75 = max(1.0, float(median(ages)))
            old_count = sum(1 for age in ages if age >= p75)
            congestion_ratio = float(old_count) / float(max(1, len(ages)))
        congestion_ratio = max(0.0, min(1.0, float(congestion_ratio)))

        self._micro_features = {
            "fill_imbalance": float(fill_imbalance),
            "spread_realization": float(spread_realization),
            "fill_time_derivative": float(fill_time_derivative),
            "congestion_ratio": float(congestion_ratio),
        }
        self._push_private_features_to_hmm_detectors()
        return dict(self._micro_features)

    def _push_private_features_to_hmm_detectors(self) -> None:
        metrics = dict(self._micro_features or {})
        for detector in (self._hmm_detector, self._hmm_detector_secondary, self._hmm_detector_tertiary):
            if detector is None:
                continue
            if hasattr(detector, "set_private_features"):
                try:
                    detector.set_private_features(metrics)
                except Exception:
                    continue

    @staticmethod
    def _detector_transmat(detector: Any) -> Any:
        if detector is None:
            return None
        model = getattr(detector, "model", None)
        if model is None:
            return None
        matrix = getattr(model, "transmat_", None)
        if matrix is None:
            return None
        return matrix

    def _build_belief_state(self, now: float | None = None) -> bayesian_engine.BeliefState:
        should_compute = bool(
            bool(getattr(config, "BELIEF_STATE_LOGGING_ENABLED", True))
            or bool(getattr(config, "BELIEF_STATE_IN_STATUS", True))
            or self._flag_value("BELIEF_TRACKER_ENABLED")
            or self._flag_value("KNOB_MODE_ENABLED")
        )
        if not should_compute:
            self._belief_state = bayesian_engine.BeliefState(enabled=False)
            return self._belief_state

        primary = dict(self._hmm_state or {})
        secondary = dict(self._hmm_state_secondary or {})
        tertiary = dict(self._hmm_state_tertiary or {})
        primary_ready = bool(primary.get("available")) and bool(primary.get("trained"))
        if not primary_ready:
            self._belief_state = bayesian_engine.BeliefState(enabled=False)
            return self._belief_state

        p1 = self._normalize_prob_triplet(self._hmm_prob_triplet(primary))
        secondary_ready = bool(secondary.get("available")) and bool(secondary.get("trained"))
        tertiary_ready = bool(tertiary.get("available")) and bool(tertiary.get("trained"))
        p15 = self._normalize_prob_triplet(self._hmm_prob_triplet(secondary if secondary_ready else primary))
        p1h = self._normalize_prob_triplet(self._hmm_prob_triplet(tertiary if tertiary_ready else secondary if secondary_ready else primary))

        if secondary_ready:
            w1, w15 = self._normalize_consensus_weights(
                getattr(config, "CONSENSUS_1M_WEIGHT", 0.3),
                getattr(config, "CONSENSUS_15M_WEIGHT", 0.7),
            )
        else:
            w1, w15 = 1.0, 0.0
        w1h = 0.0
        if tertiary_ready:
            w1h = max(0.0, 1.0 - (w1 + w15))
            if w1h <= 1e-9:
                w1 *= 0.7
                w15 *= 0.7
                w1h = 0.3

        belief = bayesian_engine.build_belief_state(
            posterior_1m=p1,
            posterior_15m=p15,
            posterior_1h=p1h,
            transmat_1m=self._detector_transmat(self._hmm_detector),
            transmat_15m=self._detector_transmat(self._hmm_detector_secondary),
            transmat_1h=self._detector_transmat(self._hmm_detector_tertiary),
            weight_1m=float(w1),
            weight_15m=float(w15),
            weight_1h=float(w1h),
            enabled=True,
        )
        self._belief_state = belief
        self._belief_state_last_ts = float(now if now is not None else _now())
        return belief

    def _update_bocpd_state(self, now: float | None = None) -> bocpd.BOCPDState:
        now_ts = float(now if now is not None else _now())
        if self._bocpd is None:
            self._bocpd_state = bocpd.BOCPDState()
            return self._bocpd_state

        price_now = float(self.last_price if self.last_price > 0 else 0.0)
        ret = 0.0
        if price_now > 0.0 and self._bocpd_last_price > 0.0:
            ret = (price_now - float(self._bocpd_last_price)) / max(1e-12, float(self._bocpd_last_price))
        if price_now > 0.0:
            self._bocpd_last_price = float(price_now)

        obs = [
            float(ret),
            float((self._micro_features or {}).get("fill_imbalance", 0.0)),
            float((self._micro_features or {}).get("spread_realization", 1.0)) - 1.0,
            float((self._micro_features or {}).get("fill_time_derivative", 0.0)),
            float((self._micro_features or {}).get("congestion_ratio", 0.0)),
            float(getattr(self._belief_state, "direction_score", 0.0)),
            float(getattr(self._belief_state, "entropy_consensus", 0.0)),
        ]
        try:
            self._bocpd_state = self._bocpd.update(obs, now_ts=now_ts)
        except Exception as e:
            logger.debug("BOCPD update failed: %s", e)
        return self._bocpd_state

    def _effective_regime_eval_interval(self, base_interval_sec: float) -> float:
        interval = max(1.0, float(base_interval_sec))
        if self._bocpd is None:
            return interval
        change_prob = max(0.0, min(1.0, float(self._bocpd_state.change_prob)))
        alert = max(0.0, min(1.0, float(getattr(config, "BOCPD_ALERT_THRESHOLD", 0.30))))
        urgent = max(alert, min(1.0, float(getattr(config, "BOCPD_URGENT_THRESHOLD", 0.50))))
        fast = max(1.0, float(getattr(config, "REGIME_EVAL_INTERVAL_FAST", 60.0)))
        if change_prob > urgent:
            return fast
        if change_prob > alert:
            return max(fast, interval * 0.5)
        return interval

    def _estimate_volatility_score(self) -> float:
        interval_min = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
        closes, volumes = self._fetch_recent_candles(count=40, interval_min=interval_min)
        if not closes or not volumes:
            return 1.0
        vols = [max(0.0, float(v)) for v in volumes]
        if not vols:
            return 1.0
        alpha = 2.0 / (20.0 + 1.0)
        ema = vols[0]
        for value in vols[1:]:
            ema = alpha * value + (1.0 - alpha) * ema
        if ema <= 1e-12:
            return 1.0
        return max(0.0, float(vols[-1]) / float(ema))

    @staticmethod
    def _cycle_meta_key(slot_id: int, trade_id: str, cycle: int) -> tuple[int, str, int]:
        return int(slot_id), str(trade_id or "").strip().upper(), int(cycle)

    def _prune_belief_timer_overrides(self, now: float | None = None) -> None:
        now_ts = float(now if now is not None else _now())
        for pid, until_ts in list(self._belief_timer_overrides.items()):
            if float(until_ts) <= now_ts:
                self._belief_timer_overrides.pop(int(pid), None)

    def _refresh_belief_slot_overrides(self) -> None:
        slot_until: dict[int, float] = {}
        for pid, until_ts in self._belief_timer_overrides.items():
            pos = self._position_ledger.get_position(int(pid))
            if not isinstance(pos, dict):
                continue
            if str(pos.get("status") or "") != "open":
                continue
            try:
                sid = int(pos.get("slot_id", -1))
            except (TypeError, ValueError):
                continue
            if sid < 0:
                continue
            cur = float(slot_until.get(sid, 0.0) or 0.0)
            if float(until_ts) > cur:
                slot_until[sid] = float(until_ts)
        self._belief_slot_override_until = slot_until

    def _slot_has_active_belief_override(self, slot_id: int, now: float | None = None) -> bool:
        now_ts = float(now if now is not None else _now())
        self._prune_belief_timer_overrides(now_ts)
        self._refresh_belief_slot_overrides()
        return float(self._belief_slot_override_until.get(int(slot_id), 0.0) or 0.0) > now_ts

    def _belief_knob_cfg(self) -> dict[str, Any]:
        return {
            "KNOB_AGGRESSION_DIRECTION": float(getattr(config, "KNOB_AGGRESSION_DIRECTION", 0.5)),
            "KNOB_AGGRESSION_BOUNDARY": float(getattr(config, "KNOB_AGGRESSION_BOUNDARY", 0.3)),
            "KNOB_AGGRESSION_CONGESTION": float(getattr(config, "KNOB_AGGRESSION_CONGESTION", 0.5)),
            "KNOB_AGGRESSION_FLOOR": float(getattr(config, "KNOB_AGGRESSION_FLOOR", 0.5)),
            "KNOB_AGGRESSION_CEILING": float(getattr(config, "KNOB_AGGRESSION_CEILING", 1.5)),
            "KNOB_SPACING_VOLATILITY": float(getattr(config, "KNOB_SPACING_VOLATILITY", 0.3)),
            "KNOB_SPACING_BOUNDARY": float(getattr(config, "KNOB_SPACING_BOUNDARY", 0.2)),
            "KNOB_SPACING_FLOOR": float(getattr(config, "KNOB_SPACING_FLOOR", 0.8)),
            "KNOB_SPACING_CEILING": float(getattr(config, "KNOB_SPACING_CEILING", 1.5)),
            "KNOB_ASYMMETRY": float(getattr(config, "KNOB_ASYMMETRY", 0.3)),
            "KNOB_CADENCE_BOUNDARY": float(getattr(config, "KNOB_CADENCE_BOUNDARY", 0.5)),
            "KNOB_CADENCE_ENTROPY": float(getattr(config, "KNOB_CADENCE_ENTROPY", 0.3)),
            "KNOB_CADENCE_FLOOR": float(getattr(config, "KNOB_CADENCE_FLOOR", 0.3)),
            "KNOB_SUPPRESS_DIRECTION_FLOOR": float(getattr(config, "KNOB_SUPPRESS_DIRECTION_FLOOR", 0.3)),
            "KNOB_SUPPRESS_SCALE": float(getattr(config, "KNOB_SUPPRESS_SCALE", 0.5)),
        }

    @staticmethod
    def _survival_regime_to_id(raw: Any) -> int:
        if isinstance(raw, int) and raw in (0, 1, 2):
            return int(raw)
        text = str(raw or "").strip().upper()
        return {"BEARISH": 0, "RANGING": 1, "BULLISH": 2}.get(text, 1)

    def _stamp_belief_entry_metadata(
        self,
        *,
        slot_id: int,
        trade_id: str,
        cycle: int,
        entry_price: float,
        exit_price: float,
        entry_ts: float,
    ) -> None:
        belief = self._build_belief_state(entry_ts)
        key = self._cycle_meta_key(slot_id, trade_id, cycle)
        self._belief_cycle_metadata[key] = {
            "posterior_1m": list(belief.posterior_1m),
            "posterior_15m": list(belief.posterior_15m),
            "posterior_1h": list(belief.posterior_1h),
            "entropy_at_entry": float(belief.entropy_consensus),
            "p_switch_at_entry": float(belief.p_switch_consensus),
            "confidence_at_entry": float(belief.confidence_score),
            "fill_imbalance": float((self._micro_features or {}).get("fill_imbalance", 0.0)),
            "spread_realization": float((self._micro_features or {}).get("spread_realization", 1.0)),
            "fill_time_derivative": float((self._micro_features or {}).get("fill_time_derivative", 0.0)),
            "congestion_ratio": float((self._micro_features or {}).get("congestion_ratio", 0.0)),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "entry_ts": float(entry_ts),
        }
        while len(self._belief_cycle_metadata) > 5000:
            oldest = next(iter(self._belief_cycle_metadata.keys()), None)
            if oldest is None:
                break
            self._belief_cycle_metadata.pop(oldest, None)

    def _apply_cycle_belief_snapshot(
        self,
        *,
        slot_id: int,
        cycle_record: Any,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else _now())
        key = self._cycle_meta_key(slot_id, str(cycle_record.trade_id), int(cycle_record.cycle))
        meta = dict(self._belief_cycle_metadata.get(key) or {})
        belief_exit = self._build_belief_state(now)

        entry_p1 = self._normalize_prob_triplet(
            meta.get("posterior_1m", getattr(cycle_record, "posterior_1m", None))
        )
        entry_p15 = self._normalize_prob_triplet(
            meta.get("posterior_15m", getattr(cycle_record, "posterior_15m", None))
        )
        entry_p1h = self._normalize_prob_triplet(
            meta.get("posterior_1h", getattr(cycle_record, "posterior_1h", None))
        )
        entry_entropy = float(
            meta.get("entropy_at_entry", getattr(cycle_record, "entropy_at_entry", 0.0) or 0.0)
        )
        entry_p_switch = float(
            meta.get("p_switch_at_entry", getattr(cycle_record, "p_switch_at_entry", 0.0) or 0.0)
        )
        entry_confidence = float(
            meta.get("confidence_at_entry", getattr(cycle_record, "confidence_at_entry", 0.0) or 0.0)
        )
        realized_pct = 0.0
        entry_price = float(getattr(cycle_record, "entry_price", 0.0) or 0.0)
        exit_price = float(getattr(cycle_record, "exit_price", 0.0) or 0.0)
        if entry_price > 0.0:
            realized_pct = abs(exit_price - entry_price) / entry_price * 100.0
        target_pct = max(1e-9, float(self.profit_pct))
        spread_realization = max(0.0, realized_pct / target_pct)
        snapshot = {
            "posterior_1m": list(entry_p1),
            "posterior_15m": list(entry_p15),
            "posterior_1h": list(entry_p1h),
            "entropy_at_entry": float(entry_entropy),
            "p_switch_at_entry": float(entry_p_switch),
            "confidence_at_entry": float(entry_confidence),
            "posterior_at_exit_1m": list(belief_exit.posterior_1m),
            "posterior_at_exit_15m": list(belief_exit.posterior_15m),
            "posterior_at_exit_1h": list(belief_exit.posterior_1h),
            "entropy_at_exit": float(belief_exit.entropy_consensus),
            "p_switch_at_exit": float(belief_exit.p_switch_consensus),
            "fill_imbalance": float((self._micro_features or {}).get("fill_imbalance", 0.0)),
            "fill_time_derivative": float((self._micro_features or {}).get("fill_time_derivative", 0.0)),
            "congestion_ratio": float((self._micro_features or {}).get("congestion_ratio", 0.0)),
            "spread_realization": float(spread_realization),
        }
        for field_name, field_value in snapshot.items():
            try:
                setattr(cycle_record, field_name, field_value)
            except Exception:
                continue

        self._record_spread_realization(
            spread_realization,
            ts=float(getattr(cycle_record, "exit_time", 0.0) or now),
        )
        self._belief_cycle_metadata.pop(key, None)
        return snapshot

    def _build_survival_observations(
        self,
        *,
        now_ts: float | None = None,
        include_censored: bool = True,
    ) -> list[survival_model.FillObservation]:
        now = float(now_ts if now_ts is not None else _now())
        rows: list[survival_model.FillObservation] = []

        for slot in self.slots.values():
            for cycle in slot.state.completed_cycles:
                entry_time = float(cycle.entry_time or 0.0)
                exit_time = float(cycle.exit_time or 0.0)
                if entry_time <= 0.0 or exit_time <= 0.0:
                    continue
                duration = max(1.0, exit_time - entry_time)
                entry_price = float(cycle.entry_price or 0.0)
                exit_price = float(cycle.exit_price or 0.0)
                if entry_price > 0.0 and exit_price > 0.0:
                    distance_pct = abs(exit_price - entry_price) / entry_price * 100.0
                else:
                    distance_pct = 0.0
                p1 = self._normalize_prob_triplet(getattr(cycle, "posterior_1m", None))
                p15 = self._normalize_prob_triplet(getattr(cycle, "posterior_15m", None))
                p1h = self._normalize_prob_triplet(getattr(cycle, "posterior_1h", None))
                rows.append(
                    survival_model.FillObservation(
                        duration_sec=float(duration),
                        censored=False,
                        regime_at_entry=self._survival_regime_to_id(getattr(cycle, "regime_at_entry", 1)),
                        regime_at_exit=self._survival_regime_to_id(self._current_regime_id()),
                        side=str(getattr(cycle, "trade_id", "A") or "A"),
                        distance_pct=float(max(0.0, distance_pct)),
                        posterior_1m=list(p1),
                        posterior_15m=list(p15),
                        posterior_1h=list(p1h),
                        entropy_at_entry=float(getattr(cycle, "entropy_at_entry", 0.0) or 0.0),
                        p_switch_at_entry=float(getattr(cycle, "p_switch_at_entry", 0.0) or 0.0),
                        fill_imbalance=float(getattr(cycle, "fill_imbalance", 0.0) or 0.0),
                        congestion_ratio=float(getattr(cycle, "congestion_ratio", 0.0) or 0.0),
                    ).normalized()
                )

        if include_censored and self._position_ledger_enabled():
            for pos in self._position_ledger.get_open_positions():
                try:
                    pid = int(pos.get("position_id", 0) or 0)
                    sid = int(pos.get("slot_id", -1))
                    cycle = int(pos.get("cycle", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if pid <= 0 or sid < 0:
                    continue
                slot = self.slots.get(sid)
                live = self._find_live_exit_for_position(pos)
                if slot is None or live is None:
                    continue
                _local_id, order = live
                entry_time = float(pos.get("entry_time", 0.0) or 0.0)
                if entry_time <= 0.0:
                    continue
                duration = max(1.0, now - entry_time)
                market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
                exit_price = float(pos.get("current_exit_price", order.price) or order.price)
                if market > 0.0 and exit_price > 0.0:
                    distance_pct = abs(exit_price - market) / market * 100.0
                else:
                    entry_price = float(pos.get("entry_price", 0.0) or 0.0)
                    distance_pct = abs(exit_price - entry_price) / max(entry_price, 1e-12) * 100.0

                key = self._cycle_meta_key(sid, str(pos.get("trade_id") or ""), cycle)
                meta = dict(self._belief_cycle_metadata.get(key) or {})
                rows.append(
                    survival_model.FillObservation(
                        duration_sec=float(duration),
                        censored=True,
                        regime_at_entry=self._survival_regime_to_id(pos.get("entry_regime")),
                        regime_at_exit=None,
                        side=str(pos.get("trade_id") or "A"),
                        distance_pct=float(max(0.0, distance_pct)),
                        posterior_1m=self._normalize_prob_triplet(meta.get("posterior_1m")),
                        posterior_15m=self._normalize_prob_triplet(meta.get("posterior_15m")),
                        posterior_1h=self._normalize_prob_triplet(meta.get("posterior_1h")),
                        entropy_at_entry=float(meta.get("entropy_at_entry", 0.0) or 0.0),
                        p_switch_at_entry=float(meta.get("p_switch_at_entry", 0.0) or 0.0),
                        fill_imbalance=float(meta.get("fill_imbalance", (self._micro_features or {}).get("fill_imbalance", 0.0)) or 0.0),
                        congestion_ratio=float(meta.get("congestion_ratio", (self._micro_features or {}).get("congestion_ratio", 0.0)) or 0.0),
                    ).normalized()
                )

        return rows

    def _maybe_retrain_survival_model(self, now: float | None = None, *, force: bool = False) -> None:
        now_ts = float(now if now is not None else _now())
        if self._survival_model is None:
            return
        if not self._flag_value("SURVIVAL_MODEL_ENABLED"):
            return

        interval = max(300.0, float(getattr(config, "SURVIVAL_RETRAIN_INTERVAL_SEC", 21600.0)))
        if (not force) and self._survival_last_retrain_ts > 0.0 and (now_ts - self._survival_last_retrain_ts) < interval:
            return

        observations = self._build_survival_observations(now_ts=now_ts, include_censored=True)
        synthetic_rows: list[survival_model.FillObservation] = []
        if bool(getattr(config, "SURVIVAL_SYNTHETIC_ENABLED", False)):
            n_paths = max(6, int(getattr(config, "SURVIVAL_SYNTHETIC_PATHS", 5000)))
            synth_w = max(0.0, min(1.0, float(getattr(config, "SURVIVAL_SYNTHETIC_WEIGHT", 0.30))))
            synthetic_rows = self._survival_model.generate_synthetic_observations(
                n_paths=n_paths,
                weight=synth_w,
            )

        try:
            self._survival_model.fit(observations, synthetic_observations=synthetic_rows)
            self._survival_last_retrain_ts = float(now_ts)
        except Exception as e:
            logger.debug("Survival retrain failed: %s", e)
            self._survival_last_retrain_ts = float(now_ts)

    def _predict_survival_for_position(
        self,
        position: dict[str, Any],
        order: sm.OrderState,
        *,
        now_ts: float | None = None,
    ) -> survival_model.SurvivalPrediction:
        now = float(now_ts if now_ts is not None else _now())
        if self._survival_model is None or not self._flag_value("SURVIVAL_MODEL_ENABLED"):
            return survival_model.SurvivalPrediction(
                p_fill_30m=0.5,
                p_fill_1h=0.5,
                p_fill_4h=0.5,
                median_remaining=float("inf"),
                hazard_ratio=1.0,
                model_tier="kaplan_meier",
                confidence=0.0,
            )

        try:
            sid = int(position.get("slot_id", -1))
            cycle = int(position.get("cycle", 0) or 0)
        except (TypeError, ValueError):
            sid = -1
            cycle = 0
        slot = self.slots.get(sid)
        market = float(slot.state.market_price if slot and slot.state.market_price > 0 else self.last_price)
        current_exit = float(position.get("current_exit_price", order.price) or order.price)
        if market > 0.0 and current_exit > 0.0:
            distance_pct = abs(current_exit - market) / market * 100.0
        else:
            entry_price = float(position.get("entry_price", 0.0) or 0.0)
            distance_pct = abs(current_exit - entry_price) / max(entry_price, 1e-12) * 100.0

        key = self._cycle_meta_key(sid, str(position.get("trade_id") or ""), cycle)
        meta = dict(self._belief_cycle_metadata.get(key) or {})
        obs = survival_model.FillObservation(
            duration_sec=max(1.0, now - float(position.get("entry_time", now) or now)),
            censored=True,
            regime_at_entry=self._survival_regime_to_id(position.get("entry_regime")),
            regime_at_exit=None,
            side=str(position.get("trade_id") or "A"),
            distance_pct=float(max(0.0, distance_pct)),
            posterior_1m=self._normalize_prob_triplet(meta.get("posterior_1m")),
            posterior_15m=self._normalize_prob_triplet(meta.get("posterior_15m")),
            posterior_1h=self._normalize_prob_triplet(meta.get("posterior_1h")),
            entropy_at_entry=float(meta.get("entropy_at_entry", 0.0) or 0.0),
            p_switch_at_entry=float(meta.get("p_switch_at_entry", 0.0) or 0.0),
            fill_imbalance=float(meta.get("fill_imbalance", (self._micro_features or {}).get("fill_imbalance", 0.0)) or 0.0),
            congestion_ratio=float(meta.get("congestion_ratio", (self._micro_features or {}).get("congestion_ratio", 0.0)) or 0.0),
        ).normalized()
        try:
            return self._survival_model.predict(obs)
        except Exception as e:
            logger.debug("Survival predict failed for position=%s: %s", position.get("position_id"), e)
            return survival_model.SurvivalPrediction(
                p_fill_30m=0.5,
                p_fill_1h=0.5,
                p_fill_4h=0.5,
                median_remaining=float("inf"),
                hazard_ratio=1.0,
                model_tier="kaplan_meier",
                confidence=0.0,
            )

    def _belief_tighten_position(self, position_id: int, *, now_ts: float | None = None) -> tuple[bool, str]:
        now = float(now_ts if now_ts is not None else _now())
        position = self._position_ledger.get_position(int(position_id))
        if not isinstance(position, dict) or str(position.get("status") or "") != "open":
            return False, "position_not_open"
        slot_id = int(position.get("slot_id", -1))
        slot = self.slots.get(slot_id)
        if slot is None:
            return False, "slot_missing"
        live = self._find_live_exit_for_position(position)
        if live is None:
            return False, "live_exit_missing"
        _local_id, order = live
        market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        if market <= 0.0:
            return False, "market_unavailable"
        new_price, _tighten_pct = self._self_heal_tighten_target_price(
            position=position,
            slot=slot,
            side=str(order.side),
            market=float(market),
        )
        current_exit = float(position.get("current_exit_price", order.price) or order.price)
        if str(order.side) == "sell" and new_price >= current_exit:
            return False, "no_tighten_delta"
        if str(order.side) == "buy" and new_price <= current_exit:
            return False, "no_tighten_delta"
        return self._execute_self_heal_reprice(
            position_id=int(position_id),
            slot_id=int(slot_id),
            order=order,
            new_price=float(new_price),
            reason="tighten",
            subsidy_consumed=0.0,
            now_ts=float(now),
        )

    def _belief_widen_position(self, position_id: int, *, now_ts: float | None = None) -> tuple[bool, str]:
        now = float(now_ts if now_ts is not None else _now())
        position = self._position_ledger.get_position(int(position_id))
        if not isinstance(position, dict) or str(position.get("status") or "") != "open":
            return False, "position_not_open"
        slot_id = int(position.get("slot_id", -1))
        slot = self.slots.get(slot_id)
        if slot is None:
            return False, "slot_missing"
        if sm.derive_phase(slot.state) == "S2":
            return False, "widen_blocked_s2"
        live = self._find_live_exit_for_position(position)
        if live is None:
            return False, "live_exit_missing"
        _local_id, order = live

        step_pct = max(0.0, float(getattr(config, "BELIEF_WIDEN_STEP_PCT", 0.001)))
        max_count = max(0, int(getattr(config, "BELIEF_MAX_WIDEN_COUNT", 2)))
        max_total = max(0.0, float(getattr(config, "BELIEF_MAX_WIDEN_TOTAL_PCT", 0.005)))
        current_count = int(self._trade_belief_widen_count.get(int(position_id), 0) or 0)
        current_total = float(self._trade_belief_widen_total_pct.get(int(position_id), 0.0) or 0.0)
        if step_pct <= 0.0:
            return False, "widen_disabled"
        if current_count >= max_count:
            return False, "widen_count_limit"
        if (current_total + step_pct) > (max_total + 1e-12):
            return False, "widen_total_limit"

        decimals = max(0, int(self.constraints.get("price_decimals", 6)))
        current_exit = float(position.get("current_exit_price", order.price) or order.price)
        if str(order.side) == "sell":
            new_price = float(round(current_exit * (1.0 + step_pct), decimals))
            if new_price <= current_exit:
                return False, "no_widen_delta"
        else:
            new_price = float(round(current_exit * (1.0 - step_pct), decimals))
            if new_price >= current_exit or new_price <= 0.0:
                return False, "no_widen_delta"

        ok, reason = self._execute_self_heal_reprice(
            position_id=int(position_id),
            slot_id=int(slot_id),
            order=order,
            new_price=float(new_price),
            reason="operator",
            subsidy_consumed=0.0,
            now_ts=float(now),
        )
        if ok:
            self._trade_belief_widen_count[int(position_id)] = current_count + 1
            self._trade_belief_widen_total_pct[int(position_id)] = current_total + step_pct
        return ok, reason

    def _update_trade_beliefs(self, now: float | None = None) -> None:
        now_ts = float(now if now is not None else _now())
        self._prune_belief_timer_overrides(now_ts)

        if not self._flag_value("BELIEF_TRACKER_ENABLED") or not self._position_ledger_enabled():
            self._trade_beliefs = {}
            self._refresh_belief_slot_overrides()
            return

        interval = max(5.0, float(getattr(config, "BELIEF_UPDATE_INTERVAL_SEC", 60.0)))
        if self._trade_belief_last_update_ts > 0.0 and (now_ts - self._trade_belief_last_update_ts) < interval:
            self._refresh_belief_slot_overrides()
            return
        self._trade_belief_last_update_ts = now_ts
        self._build_belief_state(now_ts)

        tracked: dict[int, bayesian_engine.TradeBeliefState] = {}
        open_positions = self._position_ledger.get_open_positions()
        current_vec9 = bayesian_engine.posterior9_from_timeframes(
            self._belief_state.posterior_1m,
            self._belief_state.posterior_15m,
            self._belief_state.posterior_1h,
        )

        for pos in open_positions:
            try:
                pid = int(pos.get("position_id", 0) or 0)
                sid = int(pos.get("slot_id", -1))
                cycle = int(pos.get("cycle", 0) or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or sid < 0:
                continue
            slot = self.slots.get(sid)
            if slot is None:
                continue
            live = self._find_live_exit_for_position(pos)
            if live is None:
                continue
            _local_id, order = live

            trade_id = str(pos.get("trade_id") or "")
            key = self._cycle_meta_key(sid, trade_id, cycle)
            meta = dict(self._belief_cycle_metadata.get(key) or {})
            entry_p1 = self._normalize_prob_triplet(meta.get("posterior_1m"))
            entry_p15 = self._normalize_prob_triplet(meta.get("posterior_15m"))
            entry_p1h = self._normalize_prob_triplet(meta.get("posterior_1h"))
            entry_vec9 = bayesian_engine.posterior9_from_timeframes(entry_p1, entry_p15, entry_p1h)

            entry_time = float(pos.get("entry_time", 0.0) or 0.0)
            elapsed_sec = max(0.0, now_ts - entry_time) if entry_time > 0.0 else 0.0
            market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
            exit_price = float(pos.get("current_exit_price", order.price) or order.price)
            if market > 0.0 and exit_price > 0.0:
                distance_pct = abs(exit_price - market) / market * 100.0
            else:
                entry_price_fallback = float(pos.get("entry_price", 0.0) or 0.0)
                distance_pct = abs(exit_price - entry_price_fallback) / max(1e-12, entry_price_fallback) * 100.0

            prediction = self._predict_survival_for_position(pos, order, now_ts=now_ts)
            regime_agreement = bayesian_engine.cosine_similarity(entry_vec9, current_vec9)
            volume = max(0.0, float(pos.get("entry_volume", 0.0) or 0.0))
            entry_price = float(pos.get("entry_price", 0.0) or 0.0)
            if trade_id == "B":
                profit_if_fill = (exit_price - entry_price) * volume
            else:
                profit_if_fill = (entry_price - exit_price) * volume
            expected_value = bayesian_engine.expected_value(
                p_fill=float(prediction.p_fill_1h),
                profit_if_fill=float(profit_if_fill),
                opportunity_cost_per_hour=float(getattr(config, "BELIEF_OPPORTUNITY_COST_PER_HOUR", 0.001)),
                elapsed_sec=float(elapsed_sec),
            )
            ev_hist = list(self._trade_belief_ev_history.get(pid, []))
            ev_hist.append(float(expected_value))
            ev_hist = ev_hist[-20:]
            self._trade_belief_ev_history[pid] = ev_hist
            trend = bayesian_engine.ev_trend(ev_hist, window=max(2, int(getattr(config, "BELIEF_EV_TREND_WINDOW", 3))))

            is_s2 = sm.derive_phase(slot.state) == "S2"
            rec_action, rec_conf = bayesian_engine.recommend_trade_action(
                regime_agreement=float(regime_agreement),
                confidence_score=float(self._belief_state.confidence_score),
                p_fill_30m=float(prediction.p_fill_30m),
                p_fill_1h=float(prediction.p_fill_1h),
                p_fill_4h=float(prediction.p_fill_4h),
                expected_value_usd=float(expected_value),
                ev_trend_label=str(trend),
                is_s2=bool(is_s2),
                widen_enabled=self._flag_value("BELIEF_WIDEN_ENABLED"),
                immediate_reprice_agreement=float(getattr(config, "BELIEF_IMMEDIATE_REPRICE_AGREEMENT", 0.30)),
                immediate_reprice_confidence=float(getattr(config, "BELIEF_IMMEDIATE_REPRICE_CONFIDENCE", 0.60)),
                tighten_threshold_pfill=float(getattr(config, "BELIEF_TIGHTEN_THRESHOLD_PFILL", 0.10)),
                tighten_threshold_ev=float(getattr(config, "BELIEF_TIGHTEN_THRESHOLD_EV", 0.0)),
            )
            if float(prediction.confidence) <= 0.0 and rec_action != "hold":
                rec_action = "hold"
                rec_conf = min(float(rec_conf), 0.2)

            belief = bayesian_engine.TradeBeliefState(
                position_id=int(pid),
                slot_id=int(sid),
                trade_id=str(trade_id),
                cycle=int(cycle),
                entry_regime_posterior=list(entry_vec9),
                entry_entropy=float(meta.get("entropy_at_entry", 0.0) or 0.0),
                entry_p_switch=float(meta.get("p_switch_at_entry", 0.0) or 0.0),
                entry_price=float(entry_price),
                exit_price=float(exit_price),
                entry_ts=float(entry_time),
                side=str(order.side),
                current_regime_posterior=list(current_vec9),
                current_entropy=float(self._belief_state.entropy_consensus),
                current_p_switch=float(self._belief_state.p_switch_consensus),
                elapsed_sec=float(elapsed_sec),
                distance_from_market_pct=float(distance_pct),
                p_fill_30m=float(prediction.p_fill_30m),
                p_fill_1h=float(prediction.p_fill_1h),
                p_fill_4h=float(prediction.p_fill_4h),
                median_remaining_sec=float(prediction.median_remaining),
                regime_agreement=float(regime_agreement),
                expected_value=float(expected_value),
                ev_trend=str(trend),
                recommended_action=str(rec_action),
                action_confidence=float(rec_conf),
            )
            tracked[pid] = belief

            previous = self._trade_beliefs.get(pid)
            prev_action = str(previous.recommended_action) if previous is not None else ""
            action_changed = prev_action != str(rec_action)
            if action_changed:
                self._trade_belief_action_counts[str(rec_action)] = int(
                    self._trade_belief_action_counts.get(str(rec_action), 0) or 0
                ) + 1

            if rec_action == "hold":
                max_override = max(60.0, float(getattr(config, "BELIEF_TIMER_OVERRIDE_MAX_SEC", 3600.0)))
                self._belief_timer_overrides.setdefault(pid, now_ts + max_override)
                continue

            self._belief_timer_overrides.pop(pid, None)
            if not action_changed:
                continue

            ok = False
            reason = "skipped"
            if rec_action == "reprice_breakeven":
                ok, reason = self.self_heal_reprice_breakeven(pid, operator_reason="belief_tracker")
            elif rec_action == "tighten":
                ok, reason = self._belief_tighten_position(pid, now_ts=now_ts)
            elif rec_action == "widen":
                ok, reason = self._belief_widen_position(pid, now_ts=now_ts)
            if bool(getattr(config, "BELIEF_LOG_ACTIONS", True)):
                logger.info(
                    "belief_tracker action position=%s slot=%s action=%s ok=%s reason=%s p_fill_1h=%.3f ev=%.6f agree=%.3f",
                    pid,
                    sid,
                    rec_action,
                    ok,
                    reason,
                    float(prediction.p_fill_1h),
                    float(expected_value),
                    float(regime_agreement),
                )

        tracked_ids = set(tracked.keys())
        for pid in list(self._trade_beliefs.keys()):
            if pid not in tracked_ids:
                self._trade_beliefs.pop(pid, None)
                self._trade_belief_ev_history.pop(pid, None)
                self._trade_belief_widen_count.pop(pid, None)
                self._trade_belief_widen_total_pct.pop(pid, None)
                self._belief_timer_overrides.pop(pid, None)
        self._trade_beliefs = tracked
        self._prune_belief_timer_overrides(now_ts)
        self._refresh_belief_slot_overrides()

    def _trade_beliefs_status_payload(self) -> dict[str, Any]:
        enabled = self._flag_value("BELIEF_TRACKER_ENABLED") and self._position_ledger_enabled()
        beliefs = list(self._trade_beliefs.values())
        tracked = len(beliefs)
        avg_agree = (sum(float(b.regime_agreement) for b in beliefs) / tracked) if tracked else 0.0
        avg_ev = (sum(float(b.expected_value) for b in beliefs) / tracked) if tracked else 0.0
        neg_ev = sum(1 for b in beliefs if float(b.expected_value) < 0.0)
        timer_overrides = sum(1 for until in self._belief_timer_overrides.values() if float(until) > _now())
        badges = [b.to_badge_dict() for b in sorted(beliefs, key=lambda row: (row.slot_id, row.position_id))]
        return {
            "enabled": bool(enabled),
            "tracked_exits": int(tracked),
            "actions_this_session": dict(self._trade_belief_action_counts or {}),
            "avg_regime_agreement": float(avg_agree),
            "avg_expected_value": float(avg_ev),
            "exits_with_negative_ev": int(neg_ev),
            "timer_overrides_active": int(timer_overrides),
            "last_update_ts": float(self._trade_belief_last_update_ts),
            "positions": badges,
        }

    def _internal_open_order_count(self) -> int:
        count = sum(len(slot.state.orders) + len(slot.state.recovery_orders) for slot in self.slots.values())
        for state in self._churner_by_slot.values():
            if str(state.entry_txid or "").strip():
                count += 1
            if str(state.exit_txid or "").strip():
                count += 1
        return count

    def _open_order_drift_is_persistent(
        self,
        *,
        now: float,
        internal_open_orders_current: int,
        kraken_open_orders_current: int | None,
    ) -> bool:
        if kraken_open_orders_current is None:
            return False
        threshold = max(1, int(config.OPEN_ORDER_DRIFT_ALERT_THRESHOLD))
        persist_sec = max(0.0, float(config.OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC))
        telemetry_max_age = max(60.0, float(config.POLL_INTERVAL_SECONDS) * 3.0)
        if now - self._kraken_open_orders_ts > telemetry_max_age:
            return False
        drift = int(kraken_open_orders_current) - internal_open_orders_current
        if abs(drift) < threshold or self._open_order_drift_over_threshold_since is None:
            return False
        return (now - self._open_order_drift_over_threshold_since) >= persist_sec

    def _maybe_alert_persistent_open_order_drift(self, now: float | None = None) -> None:
        now = now or _now()
        kraken_open_orders_current = self._kraken_open_orders_current
        if kraken_open_orders_current is None:
            return
        telemetry_max_age = max(60.0, float(config.POLL_INTERVAL_SECONDS) * 3.0)
        if now - self._kraken_open_orders_ts > telemetry_max_age:
            self._open_order_drift_over_threshold_since = None
            return

        internal_open_orders_current = self._internal_open_order_count()
        drift = int(kraken_open_orders_current) - internal_open_orders_current
        threshold = max(1, int(config.OPEN_ORDER_DRIFT_ALERT_THRESHOLD))

        if abs(drift) < threshold:
            if self._open_order_drift_alert_active:
                active_since = self._open_order_drift_alert_active_since or now
                active_duration_sec = int(max(0.0, now - active_since))
                notifier._send_message(
                    "<b>Open-order drift recovered</b>\n"
                    f"pair: {self.pair_display}\n"
                    f"kraken_open_orders: {int(kraken_open_orders_current)}\n"
                    f"internal_open_orders: {internal_open_orders_current}\n"
                    f"drift: {drift:+d}\n"
                    f"active_duration: {active_duration_sec}s"
                )
            self._open_order_drift_alert_active = False
            self._open_order_drift_alert_active_since = None
            self._open_order_drift_over_threshold_since = None
            return

        if self._open_order_drift_over_threshold_since is None:
            self._open_order_drift_over_threshold_since = now
            return

        if not self._open_order_drift_is_persistent(
            now=now,
            internal_open_orders_current=internal_open_orders_current,
            kraken_open_orders_current=kraken_open_orders_current,
        ):
            return

        cooldown_sec = max(0.0, float(config.OPEN_ORDER_DRIFT_ALERT_COOLDOWN_SEC))
        if now - self._open_order_drift_last_alert_at < cooldown_sec:
            return

        self._open_order_drift_last_alert_at = now
        if not self._open_order_drift_alert_active:
            self._open_order_drift_alert_active_since = now
        self._open_order_drift_alert_active = True
        persist_sec = int(max(0.0, float(config.OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC)))
        notifier._send_message(
            "<b>Open-order drift persistent</b>\n"
            f"pair: {self.pair_display}\n"
            f"kraken_open_orders: {int(kraken_open_orders_current)}\n"
            f"internal_open_orders: {internal_open_orders_current}\n"
            f"drift: {drift:+d}\n"
            f"persistence: >= {persist_sec}s"
        )

    def _global_snapshot(self) -> dict:
        snap = {
            "version": "doge-v1",
            "saved_at": _now(),
            "mode": self.mode,
            "pause_reason": self.pause_reason,
            "entry_pct": self.entry_pct,
            "profit_pct": self.profit_pct,
            "pair": self.pair,
            "pair_display": self.pair_display,
            "next_slot_id": self.next_slot_id,
            "next_event_id": self.next_event_id,
            "seen_fill_txids": list(self.seen_fill_txids)[-5000:],
            "last_price": self.last_price,
            "last_price_ts": self.last_price_ts,
            "constraints": self.constraints,
            "maker_fee_pct": self.maker_fee_pct,
            "taker_fee_pct": self.taker_fee_pct,
            "target_layers": int(self.target_layers),
            "effective_layers": int(self.effective_layers),
            "layer_last_add_event": self.layer_last_add_event,
            "slot_alias_recycle_queue": list(self.slot_alias_recycle_queue),
            "slot_alias_fallback_counter": int(self.slot_alias_fallback_counter),
            "slots": {str(sid): sm.to_dict(slot.state) for sid, slot in self.slots.items()},
            "slot_aliases": {str(sid): str(slot.alias or "").strip().lower() for sid, slot in self.slots.items()},
            "recon_baseline": self._recon_baseline,
            "flow_detection_active": bool(self._flow_detection_active),
            "flow_last_poll_ts": float(self._flow_last_poll_ts),
            "flow_ledger_cursor": float(self._flow_ledger_cursor),
            "flow_seen_ids": list(self._flow_seen_ids),
            "external_flows": [asdict(flow) for flow in self._external_flows[-self._flow_history_cap:]],
            "baseline_adjustments": list(self._baseline_adjustments[-self._baseline_adjustments_cap:]),
            "flow_total_deposits_doge_eq": float(self._flow_total_deposits_doge_eq),
            "flow_total_withdrawals_doge_eq": float(self._flow_total_withdrawals_doge_eq),
            "flow_total_count": int(self._flow_total_count),
            "flow_last_error": str(self._flow_last_error or ""),
            "flow_last_ok": bool(self._flow_last_ok),
            "flow_disabled_reason": str(self._flow_disabled_reason or ""),
            "rebalancer_idle_ratio": self._rebalancer_idle_ratio,
            "rebalancer_smoothed_error": self._rebalancer_smoothed_error,
            "rebalancer_smoothed_velocity": self._rebalancer_smoothed_velocity,
            "rebalancer_current_skew": self._rebalancer_current_skew,
            "rebalancer_last_update_ts": self._rebalancer_last_update_ts,
            "rebalancer_last_raw_error": self._rebalancer_last_raw_error,
            "rebalancer_sign_flip_history": list(self._rebalancer_sign_flip_history)[-20:],
            "rebalancer_damped_until": self._rebalancer_damped_until,
            "trend_fast_ema": self._trend_fast_ema,
            "trend_slow_ema": self._trend_slow_ema,
            "trend_score": self._trend_score,
            "trend_dynamic_target": self._trend_dynamic_target,
            "trend_smoothed_target": self._trend_smoothed_target,
            "trend_target_locked_until": self._trend_target_locked_until,
            "trend_last_update_ts": self._trend_last_update_ts,
            "ohlcv_since_cursor": self._ohlcv_since_cursor,
            "ohlcv_last_sync_ts": self._ohlcv_last_sync_ts,
            "ohlcv_last_candle_ts": self._ohlcv_last_candle_ts,
            "ohlcv_secondary_since_cursor": self._ohlcv_secondary_since_cursor,
            "ohlcv_secondary_last_sync_ts": self._ohlcv_secondary_last_sync_ts,
            "ohlcv_secondary_last_candle_ts": self._ohlcv_secondary_last_candle_ts,
            "ohlcv_secondary_last_rows_queued": self._ohlcv_secondary_last_rows_queued,
            "ohlcv_tertiary_since_cursor": self._ohlcv_tertiary_since_cursor,
            "ohlcv_tertiary_last_sync_ts": self._ohlcv_tertiary_last_sync_ts,
            "ohlcv_tertiary_last_candle_ts": self._ohlcv_tertiary_last_candle_ts,
            "ohlcv_tertiary_last_rows_queued": self._ohlcv_tertiary_last_rows_queued,
            "hmm_state_secondary": dict(self._hmm_state_secondary or {}),
            "hmm_state_tertiary": dict(self._hmm_state_tertiary or {}),
            "hmm_consensus": dict(self._hmm_consensus or {}),
            "hmm_backfill_last_at": self._hmm_backfill_last_at,
            "hmm_backfill_last_rows": self._hmm_backfill_last_rows,
            "hmm_backfill_last_message": self._hmm_backfill_last_message,
            "hmm_backfill_stall_count": self._hmm_backfill_stall_count,
            "hmm_backfill_last_at_secondary": self._hmm_backfill_last_at_secondary,
            "hmm_backfill_last_rows_secondary": self._hmm_backfill_last_rows_secondary,
            "hmm_backfill_last_message_secondary": self._hmm_backfill_last_message_secondary,
            "hmm_backfill_stall_count_secondary": self._hmm_backfill_stall_count_secondary,
            "hmm_backfill_last_at_tertiary": self._hmm_backfill_last_at_tertiary,
            "hmm_backfill_last_rows_tertiary": self._hmm_backfill_last_rows_tertiary,
            "hmm_backfill_last_message_tertiary": self._hmm_backfill_last_message_tertiary,
            "hmm_backfill_stall_count_tertiary": self._hmm_backfill_stall_count_tertiary,
            "hmm_tertiary_transition": dict(self._hmm_tertiary_transition or {}),
            "belief_state": self._belief_state.to_status_dict(),
            "belief_state_last_ts": float(self._belief_state_last_ts),
            "belief_cycle_metadata": [
                {
                    "slot_id": int(slot_id),
                    "trade_id": str(trade_id),
                    "cycle": int(cycle),
                    "data": dict(data),
                }
                for (slot_id, trade_id, cycle), data in self._belief_cycle_metadata.items()
            ],
            "action_knobs": self._action_knobs.to_status_dict(),
            "micro_features": dict(self._micro_features or {}),
            "fill_events_recent": [
                [float(ts), str(side)]
                for ts, side in list(self._fill_events_recent)[-2000:]
            ],
            "fill_duration_events": [
                [float(ts), float(duration)]
                for ts, duration in list(self._fill_duration_events)[-2000:]
            ],
            "spread_realization_events": [
                [float(ts), float(value)]
                for ts, value in list(self._spread_realization_events)[-2000:]
            ],
            "bocpd_state": self._bocpd_state.to_status_dict(),
            "bocpd_snapshot": self._bocpd.snapshot_state() if self._bocpd is not None else {},
            "bocpd_last_price": float(self._bocpd_last_price),
            "survival_last_retrain_ts": float(self._survival_last_retrain_ts),
            "survival_snapshot": (
                self._survival_model.snapshot_state()
                if self._survival_model is not None
                else {}
            ),
            "trade_beliefs": [
                asdict(belief)
                for belief in self._trade_beliefs.values()
            ],
            "trade_belief_action_counts": dict(self._trade_belief_action_counts or {}),
            "trade_belief_last_update_ts": float(self._trade_belief_last_update_ts),
            "trade_belief_ev_history": {
                str(position_id): [float(v) for v in values[-20:]]
                for position_id, values in self._trade_belief_ev_history.items()
            },
            "trade_belief_widen_count": {
                str(position_id): int(count)
                for position_id, count in self._trade_belief_widen_count.items()
            },
            "trade_belief_widen_total_pct": {
                str(position_id): float(total_pct)
                for position_id, total_pct in self._trade_belief_widen_total_pct.items()
            },
            "belief_timer_overrides": {
                str(position_id): float(until_ts)
                for position_id, until_ts in self._belief_timer_overrides.items()
            },
            "regime_tier": int(self._regime_tier),
            "regime_tier_entered_at": float(self._regime_tier_entered_at),
            "regime_tier2_grace_start": float(self._regime_tier2_grace_start),
            "regime_side_suppressed": self._regime_side_suppressed,
            "regime_last_eval_ts": float(self._regime_last_eval_ts),
            "regime_tier2_last_downgrade_at": float(self._regime_tier2_last_downgrade_at),
            "regime_cooldown_suppressed_side": self._regime_cooldown_suppressed_side,
            "regime_tier_history": list(self._regime_tier_history[-20:]),
            "regime_shadow_state": dict(self._regime_shadow_state or {}),
            "regime_mechanical_tier": int(self._regime_mechanical_tier),
            "regime_mechanical_direction": str(self._regime_mechanical_direction),
            "regime_mechanical_since": float(self._regime_mechanical_since),
            "regime_mechanical_tier_entered_at": float(self._regime_mechanical_tier_entered_at),
            "regime_mechanical_tier2_last_downgrade_at": float(
                self._regime_mechanical_tier2_last_downgrade_at
            ),
            "ai_override_tier": self._ai_override_tier,
            "ai_override_direction": self._ai_override_direction,
            "ai_override_until": self._ai_override_until,
            "ai_override_applied_at": self._ai_override_applied_at,
            "ai_override_source_conviction": self._ai_override_source_conviction,
            "accum_state": str(self._accum_state),
            "accum_direction": self._accum_direction,
            "accum_trigger_from_regime": str(self._accum_trigger_from_regime),
            "accum_trigger_to_regime": str(self._accum_trigger_to_regime),
            "accum_start_ts": float(self._accum_start_ts),
            "accum_start_price": float(self._accum_start_price),
            "accum_spent_usd": float(self._accum_spent_usd),
            "accum_acquired_doge": float(self._accum_acquired_doge),
            "accum_n_buys": int(self._accum_n_buys),
            "accum_last_buy_ts": float(self._accum_last_buy_ts),
            "accum_budget_usd": float(self._accum_budget_usd),
            "accum_armed_at": float(self._accum_armed_at),
            "accum_hold_streak": int(self._accum_hold_streak),
            "accum_last_session_end_ts": float(self._accum_last_session_end_ts),
            "accum_last_session_summary": dict(self._accum_last_session_summary or {}),
            "accum_manual_stop_requested": bool(self._accum_manual_stop_requested),
            "accum_cooldown_remaining_sec": int(self._accum_cooldown_remaining_sec),
            "entry_adds_deferred_total": self._entry_adds_deferred_total,
            "entry_adds_drained_total": self._entry_adds_drained_total,
            "entry_adds_last_deferred_at": self._entry_adds_last_deferred_at,
            "entry_adds_last_drained_at": self._entry_adds_last_drained_at,
            "daily_loss_lock_active": bool(self._daily_loss_lock_active),
            "daily_loss_lock_utc_day": str(self._daily_loss_lock_utc_day or ""),
            "daily_realized_loss_utc": float(self._daily_realized_loss_utc),
            "sticky_release_total": int(self._sticky_release_total),
            "sticky_release_last_at": float(self._sticky_release_last_at),
            "release_recon_blocked": bool(self._release_recon_blocked),
            "release_recon_blocked_reason": str(self._release_recon_blocked_reason or ""),
            "self_heal_reprice_total": int(self._self_heal_reprice_total),
            "self_heal_reprice_last_at": float(self._self_heal_reprice_last_at),
            "self_heal_reprice_last_summary": dict(self._self_heal_reprice_last_summary or {}),
            "self_heal_hold_until_by_position": {
                str(position_id): float(until_ts)
                for position_id, until_ts in self._self_heal_hold_until_by_position.items()
                if int(position_id) > 0 and float(until_ts) > 0.0
            },
            "position_ledger_migration_done": bool(self._position_ledger_migration_done),
            "position_ledger_migration_last_at": float(self._position_ledger_migration_last_at),
            "position_ledger_migration_last_created": int(self._position_ledger_migration_last_created),
            "position_ledger_migration_last_scanned": int(self._position_ledger_migration_last_scanned),
            "dust_last_absorbed_usd": float(self._dust_last_absorbed_usd),
            "dust_last_dividend_usd": float(self._dust_last_dividend_usd),
            "quote_first_carry_usd": float(self._quote_first_carry_usd),
            "position_ledger_state": self._position_ledger.snapshot_state(),
            "position_by_exit_local": [
                {"slot_id": int(slot_id), "local_id": int(local_id), "position_id": int(position_id)}
                for (slot_id, local_id), position_id in self._position_by_exit_local.items()
            ],
            "position_by_exit_txid": {str(k): int(v) for k, v in self._position_by_exit_txid.items()},
            "cycle_slot_mode": [
                {
                    "slot_id": int(slot_id),
                    "trade_id": str(trade_id),
                    "cycle": int(cycle),
                    "slot_mode": str(slot_mode),
                }
                for (slot_id, trade_id, cycle), slot_mode in self._cycle_slot_mode.items()
            ],
            "churner_next_cycle_id": int(self._churner_next_cycle_id),
            "churner_reserve_available_usd": float(self._churner_reserve_available_usd),
            "churner_day_key": str(self._churner_day_key or ""),
            "churner_cycles_today": int(self._churner_cycles_today),
            "churner_profit_today": float(self._churner_profit_today),
            "churner_cycles_total": int(self._churner_cycles_total),
            "churner_profit_total": float(self._churner_profit_total),
            "churner_by_slot": {
                str(sid): asdict(state)
                for sid, state in self._churner_by_slot.items()
            },
        }
        if self._throughput is not None:
            snap["throughput_sizer_state"] = self._throughput.snapshot_state()
        snap.update(self._snapshot_hmm_state())
        return snap

    def _save_local_runtime_snapshot(self, snapshot: dict) -> None:
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            tmp_path = _BOT_RUNTIME_STATE_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=True, separators=(",", ":"))
            os.replace(tmp_path, _BOT_RUNTIME_STATE_FILE)
        except Exception as e:
            logger.warning("Local runtime snapshot write failed: %s", e)

    def _load_local_runtime_snapshot(self) -> dict:
        try:
            with open(_BOT_RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save_local_equity_ts(self, payload: dict) -> None:
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            tmp_path = _EQUITY_TS_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True, separators=(",", ":"))
            os.replace(tmp_path, _EQUITY_TS_FILE)
        except Exception as e:
            logger.warning("Local equity_ts write failed: %s", e)

    def _load_local_equity_ts(self) -> dict:
        try:
            with open(_EQUITY_TS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _deserialize_external_flows(self, raw_flows: Any) -> list[ExternalFlow]:
        out: list[ExternalFlow] = []
        if not isinstance(raw_flows, list):
            return out
        for row in raw_flows:
            if not isinstance(row, dict):
                continue
            try:
                flow = ExternalFlow(
                    ledger_id=str(row.get("ledger_id") or ""),
                    flow_type=str(row.get("flow_type") or ""),
                    asset=str(row.get("asset") or ""),
                    amount=float(row.get("amount") or 0.0),
                    fee=float(row.get("fee") or 0.0),
                    timestamp=float(row.get("timestamp") or 0.0),
                    doge_eq=float(row.get("doge_eq") or 0.0),
                    price_at_detect=float(row.get("price_at_detect") or 0.0),
                )
            except (TypeError, ValueError):
                continue
            if not flow.ledger_id:
                continue
            out.append(flow)
        return out

    def _trim_flow_buffers(self) -> None:
        if len(self._external_flows) > self._flow_history_cap:
            self._external_flows = self._external_flows[-self._flow_history_cap:]
        if len(self._baseline_adjustments) > self._baseline_adjustments_cap:
            self._baseline_adjustments = self._baseline_adjustments[-self._baseline_adjustments_cap:]
        max_seen = self._flow_history_cap * 2
        if len(self._flow_seen_ids) > max_seen:
            self._flow_seen_ids = {flow.ledger_id for flow in self._external_flows}

    def _trim_equity_ts_records(self, now: float | None = None) -> None:
        if not self._equity_ts_records:
            return
        now_ts = float(now if now is not None else _now())
        cutoff = now_ts - float(self._equity_ts_retention_days) * 86400.0
        self._equity_ts_records = [
            row for row in self._equity_ts_records
            if isinstance(row, dict) and float(row.get("ts", 0.0) or 0.0) >= cutoff
        ]

    def _load_equity_ts_history(self) -> None:
        if not self._equity_ts_enabled:
            self._equity_ts_records = []
            self._equity_ts_dirty = False
            return

        payload: dict = {}
        try:
            payload = supabase_store.load_state(pair="__equity_ts__") or {}
        except Exception as e:
            logger.warning("Supabase equity_ts load failed: %s", e)
            payload = {}
        if not payload:
            payload = self._load_local_equity_ts()

        rows = payload.get("snapshots", []) if isinstance(payload, dict) else []
        records: list[dict] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    ts = float(row.get("ts") or 0.0)
                    doge_eq = float(row.get("doge_eq") or 0.0)
                except (TypeError, ValueError):
                    continue
                if ts <= 0:
                    continue
                def _f(value: Any) -> float:
                    try:
                        return float(value or 0.0)
                    except (TypeError, ValueError):
                        return 0.0
                records.append({
                    "ts": ts,
                    "doge_eq": doge_eq,
                    "usd": _f(row.get("usd")),
                    "doge": _f(row.get("doge")),
                    "price": _f(row.get("price")),
                    "bot_pnl_usd": _f(row.get("bot_pnl_usd")),
                    "flows_cumulative_doge_eq": _f(row.get("flows_cumulative_doge_eq")),
                })
        records.sort(key=lambda x: float(x.get("ts", 0.0)))
        self._equity_ts_records = records
        self._trim_equity_ts_records()
        newest_ts = float(self._equity_ts_records[-1].get("ts", 0.0)) if self._equity_ts_records else 0.0
        self._equity_ts_last_flush_ts = max(float(payload.get("cursor", 0.0) or 0.0), newest_ts)
        self._equity_ts_dirty = False

    def _save_snapshot(self) -> None:
        snap = self._global_snapshot()
        supabase_store.save_state(snap, pair="__v1__")
        self._save_local_runtime_snapshot(snap)

    def _load_snapshot(self) -> None:
        try:
            snap = supabase_store.load_state(pair="__v1__") or {}
        except Exception as e:
            logger.warning("Supabase snapshot load failed: %s", e)
            snap = {}
        if not snap:
            snap = self._load_local_runtime_snapshot()
        if snap:
            self.mode = snap.get("mode", "INIT")
            self.pause_reason = snap.get("pause_reason", "")
            self.entry_pct = float(snap.get("entry_pct", self.entry_pct))
            self.profit_pct = float(snap.get("profit_pct", self.profit_pct))
            self.next_slot_id = int(snap.get("next_slot_id", 1))
            self.next_event_id = int(snap.get("next_event_id", 1))
            self.seen_fill_txids = set(snap.get("seen_fill_txids", []))
            self.last_price = float(snap.get("last_price", 0.0))
            self.last_price_ts = float(snap.get("last_price_ts", 0.0))

            self.constraints = snap.get("constraints", self.constraints) or self.constraints
            self.maker_fee_pct = float(snap.get("maker_fee_pct", self.maker_fee_pct))
            self.taker_fee_pct = float(snap.get("taker_fee_pct", self.taker_fee_pct))
            self.target_layers = max(0, int(snap.get("target_layers", self.target_layers)))
            self.effective_layers = max(0, int(snap.get("effective_layers", self.effective_layers)))
            raw_last_add = snap.get("layer_last_add_event", self.layer_last_add_event)
            self.layer_last_add_event = raw_last_add if isinstance(raw_last_add, dict) else None
            self.slot_alias_fallback_counter = max(
                1,
                int(snap.get("slot_alias_fallback_counter", self.slot_alias_fallback_counter)),
            )
            raw_alias_queue = snap.get("slot_alias_recycle_queue", list(self.slot_alias_recycle_queue))
            self.slot_alias_recycle_queue = deque()
            if isinstance(raw_alias_queue, list):
                for alias in raw_alias_queue:
                    norm = str(alias).strip().lower()
                    if norm:
                        self.slot_alias_recycle_queue.append(norm)
            self._recon_baseline = snap.get("recon_baseline", None)
            self._flow_detection_active = bool(
                snap.get("flow_detection_active", self._flow_detection_active)
            ) and bool(getattr(config, "FLOW_DETECTION_ENABLED", True))
            self._flow_last_poll_ts = float(snap.get("flow_last_poll_ts", self._flow_last_poll_ts))
            self._flow_ledger_cursor = float(snap.get("flow_ledger_cursor", self._flow_ledger_cursor))
            raw_seen_ids = snap.get("flow_seen_ids", [])
            seen_ids: set[str] = set()
            if isinstance(raw_seen_ids, list):
                for row in raw_seen_ids:
                    val = str(row or "").strip()
                    if val:
                        seen_ids.add(val)
            self._external_flows = self._deserialize_external_flows(snap.get("external_flows", []))
            if not seen_ids and self._external_flows:
                seen_ids = {flow.ledger_id for flow in self._external_flows}
            self._flow_seen_ids = seen_ids
            raw_adjustments = snap.get("baseline_adjustments", [])
            adjustments: list[dict] = []
            if isinstance(raw_adjustments, list):
                for row in raw_adjustments:
                    if isinstance(row, dict):
                        adjustments.append(dict(row))
            self._baseline_adjustments = adjustments
            self._flow_last_error = str(snap.get("flow_last_error", self._flow_last_error) or "")
            self._flow_last_ok = bool(snap.get("flow_last_ok", self._flow_last_ok))
            self._flow_disabled_reason = str(snap.get("flow_disabled_reason", self._flow_disabled_reason) or "")
            self._flow_total_deposits_doge_eq = float(
                snap.get("flow_total_deposits_doge_eq", self._flow_total_deposits_doge_eq)
            )
            self._flow_total_withdrawals_doge_eq = float(
                snap.get("flow_total_withdrawals_doge_eq", self._flow_total_withdrawals_doge_eq)
            )
            self._flow_total_count = int(snap.get("flow_total_count", self._flow_total_count))
            if self._flow_total_count <= 0 and self._external_flows:
                self._flow_total_count = len(self._external_flows)
                self._flow_total_deposits_doge_eq = sum(
                    max(0.0, float(flow.doge_eq)) for flow in self._external_flows
                )
                self._flow_total_withdrawals_doge_eq = sum(
                    min(0.0, float(flow.doge_eq)) for flow in self._external_flows
                )
            self._trim_flow_buffers()
            self._rebalancer_idle_ratio = float(snap.get("rebalancer_idle_ratio", self._rebalancer_idle_ratio))
            self._rebalancer_smoothed_error = float(
                snap.get("rebalancer_smoothed_error", self._rebalancer_smoothed_error)
            )
            self._rebalancer_smoothed_velocity = float(
                snap.get("rebalancer_smoothed_velocity", self._rebalancer_smoothed_velocity)
            )
            self._rebalancer_current_skew = float(snap.get("rebalancer_current_skew", self._rebalancer_current_skew))
            self._rebalancer_last_update_ts = float(
                snap.get("rebalancer_last_update_ts", self._rebalancer_last_update_ts)
            )
            self._rebalancer_last_raw_error = float(
                snap.get("rebalancer_last_raw_error", self._rebalancer_last_raw_error)
            )
            self._rebalancer_damped_until = float(snap.get("rebalancer_damped_until", self._rebalancer_damped_until))
            self._trend_fast_ema = float(snap.get("trend_fast_ema", self._trend_fast_ema))
            self._trend_slow_ema = float(snap.get("trend_slow_ema", self._trend_slow_ema))
            self._trend_score = float(snap.get("trend_score", self._trend_score))
            self._trend_dynamic_target = float(snap.get("trend_dynamic_target", self._trend_dynamic_target))
            self._trend_smoothed_target = float(snap.get("trend_smoothed_target", self._trend_smoothed_target))
            self._trend_target_locked_until = float(
                snap.get("trend_target_locked_until", self._trend_target_locked_until)
            )
            self._trend_last_update_ts = float(snap.get("trend_last_update_ts", self._trend_last_update_ts))
            raw_cursor = snap.get("ohlcv_since_cursor", self._ohlcv_since_cursor)
            try:
                self._ohlcv_since_cursor = int(raw_cursor) if raw_cursor is not None else None
            except (TypeError, ValueError):
                self._ohlcv_since_cursor = None
            raw_secondary_cursor = snap.get("ohlcv_secondary_since_cursor", self._ohlcv_secondary_since_cursor)
            try:
                self._ohlcv_secondary_since_cursor = int(raw_secondary_cursor) if raw_secondary_cursor is not None else None
            except (TypeError, ValueError):
                self._ohlcv_secondary_since_cursor = None
            raw_tertiary_cursor = snap.get("ohlcv_tertiary_since_cursor", self._ohlcv_tertiary_since_cursor)
            try:
                self._ohlcv_tertiary_since_cursor = int(raw_tertiary_cursor) if raw_tertiary_cursor is not None else None
            except (TypeError, ValueError):
                self._ohlcv_tertiary_since_cursor = None
            self._ohlcv_last_sync_ts = float(snap.get("ohlcv_last_sync_ts", self._ohlcv_last_sync_ts))
            self._ohlcv_last_candle_ts = float(snap.get("ohlcv_last_candle_ts", self._ohlcv_last_candle_ts))
            self._ohlcv_secondary_last_sync_ts = float(
                snap.get("ohlcv_secondary_last_sync_ts", self._ohlcv_secondary_last_sync_ts)
            )
            self._ohlcv_secondary_last_candle_ts = float(
                snap.get("ohlcv_secondary_last_candle_ts", self._ohlcv_secondary_last_candle_ts)
            )
            self._ohlcv_secondary_last_rows_queued = int(
                snap.get("ohlcv_secondary_last_rows_queued", self._ohlcv_secondary_last_rows_queued)
            )
            self._ohlcv_tertiary_last_sync_ts = float(
                snap.get("ohlcv_tertiary_last_sync_ts", self._ohlcv_tertiary_last_sync_ts)
            )
            self._ohlcv_tertiary_last_candle_ts = float(
                snap.get("ohlcv_tertiary_last_candle_ts", self._ohlcv_tertiary_last_candle_ts)
            )
            self._ohlcv_tertiary_last_rows_queued = int(
                snap.get("ohlcv_tertiary_last_rows_queued", self._ohlcv_tertiary_last_rows_queued)
            )
            self._hmm_backfill_last_at = float(snap.get("hmm_backfill_last_at", self._hmm_backfill_last_at))
            self._hmm_backfill_last_rows = int(snap.get("hmm_backfill_last_rows", self._hmm_backfill_last_rows))
            self._hmm_backfill_last_message = str(
                snap.get("hmm_backfill_last_message", self._hmm_backfill_last_message) or ""
            )
            self._hmm_backfill_stall_count = int(
                snap.get("hmm_backfill_stall_count", self._hmm_backfill_stall_count)
            )
            self._hmm_backfill_last_at_secondary = float(
                snap.get("hmm_backfill_last_at_secondary", self._hmm_backfill_last_at_secondary)
            )
            self._hmm_backfill_last_rows_secondary = int(
                snap.get("hmm_backfill_last_rows_secondary", self._hmm_backfill_last_rows_secondary)
            )
            self._hmm_backfill_last_message_secondary = str(
                snap.get(
                    "hmm_backfill_last_message_secondary",
                    self._hmm_backfill_last_message_secondary,
                )
                or ""
            )
            self._hmm_backfill_stall_count_secondary = int(
                snap.get(
                    "hmm_backfill_stall_count_secondary",
                    self._hmm_backfill_stall_count_secondary,
                )
            )
            self._hmm_backfill_last_at_tertiary = float(
                snap.get("hmm_backfill_last_at_tertiary", self._hmm_backfill_last_at_tertiary)
            )
            self._hmm_backfill_last_rows_tertiary = int(
                snap.get("hmm_backfill_last_rows_tertiary", self._hmm_backfill_last_rows_tertiary)
            )
            self._hmm_backfill_last_message_tertiary = str(
                snap.get(
                    "hmm_backfill_last_message_tertiary",
                    self._hmm_backfill_last_message_tertiary,
                )
                or ""
            )
            self._hmm_backfill_stall_count_tertiary = int(
                snap.get(
                    "hmm_backfill_stall_count_tertiary",
                    self._hmm_backfill_stall_count_tertiary,
                )
            )
            raw_hmm_state_secondary = snap.get("hmm_state_secondary", self._hmm_state_secondary)
            if isinstance(raw_hmm_state_secondary, dict):
                self._hmm_state_secondary = dict(raw_hmm_state_secondary)
            raw_hmm_state_tertiary = snap.get("hmm_state_tertiary", self._hmm_state_tertiary)
            if isinstance(raw_hmm_state_tertiary, dict):
                self._hmm_state_tertiary = dict(raw_hmm_state_tertiary)
            raw_hmm_consensus = snap.get("hmm_consensus", self._hmm_consensus)
            if isinstance(raw_hmm_consensus, dict):
                self._hmm_consensus = dict(raw_hmm_consensus)
            raw_tertiary_transition = snap.get("hmm_tertiary_transition", self._hmm_tertiary_transition)
            if isinstance(raw_tertiary_transition, dict):
                self._hmm_tertiary_transition = dict(raw_tertiary_transition)
            raw_belief_state = snap.get("belief_state", {})
            if isinstance(raw_belief_state, dict):
                self._belief_state = bayesian_engine.BeliefState(
                    enabled=bool(raw_belief_state.get("enabled", False)),
                    posterior_1m=list(raw_belief_state.get("posterior_1m", [0.0, 1.0, 0.0])),
                    posterior_15m=list(raw_belief_state.get("posterior_15m", [0.0, 1.0, 0.0])),
                    posterior_1h=list(raw_belief_state.get("posterior_1h", [0.0, 1.0, 0.0])),
                    entropy_1m=float(raw_belief_state.get("entropy_1m", 0.0) or 0.0),
                    entropy_15m=float(raw_belief_state.get("entropy_15m", 0.0) or 0.0),
                    entropy_1h=float(raw_belief_state.get("entropy_1h", 0.0) or 0.0),
                    entropy_consensus=float(raw_belief_state.get("entropy_consensus", 0.0) or 0.0),
                    confidence_score=float(raw_belief_state.get("confidence_score", 1.0) or 1.0),
                    p_switch_1m=float(raw_belief_state.get("p_switch_1m", 0.0) or 0.0),
                    p_switch_15m=float(raw_belief_state.get("p_switch_15m", 0.0) or 0.0),
                    p_switch_1h=float(raw_belief_state.get("p_switch_1h", 0.0) or 0.0),
                    p_switch_consensus=float(raw_belief_state.get("p_switch_consensus", 0.0) or 0.0),
                    direction_score=float(raw_belief_state.get("direction_score", 0.0) or 0.0),
                    boundary_risk=str(raw_belief_state.get("boundary_risk", "low") or "low"),
                    posterior_consensus=list(
                        raw_belief_state.get("posterior_consensus", [0.0, 1.0, 0.0])
                    ),
                )
            self._belief_state_last_ts = float(snap.get("belief_state_last_ts", self._belief_state_last_ts) or 0.0)
            self._belief_cycle_metadata = {}
            raw_cycle_meta = snap.get("belief_cycle_metadata", [])
            if isinstance(raw_cycle_meta, list):
                for row in raw_cycle_meta:
                    if not isinstance(row, dict):
                        continue
                    try:
                        key = (
                            int(row.get("slot_id")),
                            str(row.get("trade_id") or ""),
                            int(row.get("cycle")),
                        )
                    except (TypeError, ValueError):
                        continue
                    if not key[1]:
                        continue
                    self._belief_cycle_metadata[key] = dict(row.get("data") or {})
            raw_knobs = snap.get("action_knobs", {})
            if isinstance(raw_knobs, dict):
                self._action_knobs = bayesian_engine.ActionKnobs(
                    enabled=bool(raw_knobs.get("enabled", False)),
                    aggression=float(raw_knobs.get("aggression", 1.0) or 1.0),
                    spacing_mult=float(raw_knobs.get("spacing_mult", 1.0) or 1.0),
                    spacing_a=float(raw_knobs.get("spacing_a", 1.0) or 1.0),
                    spacing_b=float(raw_knobs.get("spacing_b", 1.0) or 1.0),
                    cadence_mult=float(raw_knobs.get("cadence_mult", 1.0) or 1.0),
                    suppression_strength=float(raw_knobs.get("suppression_strength", 0.0) or 0.0),
                    derived_tier=max(0, min(2, int(raw_knobs.get("derived_tier", 0) or 0))),
                    derived_tier_label=str(raw_knobs.get("derived_tier_label", "symmetric") or "symmetric"),
                )
            raw_micro = snap.get("micro_features", {})
            if isinstance(raw_micro, dict):
                self._micro_features = {
                    "fill_imbalance": float(raw_micro.get("fill_imbalance", 0.0) or 0.0),
                    "spread_realization": float(raw_micro.get("spread_realization", 1.0) or 1.0),
                    "fill_time_derivative": float(raw_micro.get("fill_time_derivative", 0.0) or 0.0),
                    "congestion_ratio": float(raw_micro.get("congestion_ratio", 0.0) or 0.0),
                }
            self._fill_events_recent = deque()
            for row in list(snap.get("fill_events_recent", []) or []):
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    try:
                        self._fill_events_recent.append((float(row[0]), str(row[1])))
                    except (TypeError, ValueError):
                        continue
            self._fill_duration_events = deque()
            for row in list(snap.get("fill_duration_events", []) or []):
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    try:
                        self._fill_duration_events.append((float(row[0]), float(row[1])))
                    except (TypeError, ValueError):
                        continue
            self._spread_realization_events = deque()
            for row in list(snap.get("spread_realization_events", []) or []):
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    try:
                        self._spread_realization_events.append((float(row[0]), float(row[1])))
                    except (TypeError, ValueError):
                        continue
            raw_bocpd_state = snap.get("bocpd_state", {})
            if isinstance(raw_bocpd_state, dict):
                self._bocpd_state = bocpd.BOCPDState(
                    change_prob=float(raw_bocpd_state.get("change_prob", 0.0) or 0.0),
                    run_length_mode=max(0, int(raw_bocpd_state.get("run_length_mode", 0) or 0)),
                    run_length_mode_prob=float(raw_bocpd_state.get("run_length_mode_prob", 1.0) or 1.0),
                    last_update_ts=float(raw_bocpd_state.get("last_update_ts", 0.0) or 0.0),
                    observation_count=max(0, int(raw_bocpd_state.get("observation_count", 0) or 0)),
                    alert_active=bool(raw_bocpd_state.get("alert_active", False)),
                    alert_triggered_at=float(raw_bocpd_state.get("alert_triggered_at", 0.0) or 0.0),
                    run_length_map={
                        int(k): float(v)
                        for k, v in (raw_bocpd_state.get("run_length_map", {}) or {}).items()
                    },
                )
            if self._bocpd is not None:
                raw_bocpd_snapshot = snap.get("bocpd_snapshot", {})
                if isinstance(raw_bocpd_snapshot, dict):
                    self._bocpd.restore_state(raw_bocpd_snapshot)
                    self._bocpd_state = self._bocpd.state
            self._bocpd_last_price = float(snap.get("bocpd_last_price", self._bocpd_last_price) or 0.0)
            self._survival_last_retrain_ts = float(
                snap.get("survival_last_retrain_ts", self._survival_last_retrain_ts) or 0.0
            )
            if self._survival_model is not None:
                raw_survival_snapshot = snap.get("survival_snapshot", {})
                if isinstance(raw_survival_snapshot, dict):
                    self._survival_model.restore_state(raw_survival_snapshot)
            self._trade_beliefs = {}
            raw_trade_beliefs = snap.get("trade_beliefs", [])
            if isinstance(raw_trade_beliefs, list):
                for row in raw_trade_beliefs:
                    if not isinstance(row, dict):
                        continue
                    try:
                        pid = int(row.get("position_id", 0) or 0)
                        sid = int(row.get("slot_id", -1) or -1)
                        cycle = int(row.get("cycle", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if pid <= 0 or sid < 0:
                        continue
                    self._trade_beliefs[pid] = bayesian_engine.TradeBeliefState(
                        position_id=pid,
                        slot_id=sid,
                        trade_id=str(row.get("trade_id") or ""),
                        cycle=cycle,
                        entry_regime_posterior=list(row.get("entry_regime_posterior", []) or []),
                        entry_entropy=float(row.get("entry_entropy", 0.0) or 0.0),
                        entry_p_switch=float(row.get("entry_p_switch", 0.0) or 0.0),
                        entry_price=float(row.get("entry_price", 0.0) or 0.0),
                        exit_price=float(row.get("exit_price", 0.0) or 0.0),
                        entry_ts=float(row.get("entry_ts", 0.0) or 0.0),
                        side=str(row.get("side") or ""),
                        current_regime_posterior=list(row.get("current_regime_posterior", []) or []),
                        current_entropy=float(row.get("current_entropy", 0.0) or 0.0),
                        current_p_switch=float(row.get("current_p_switch", 0.0) or 0.0),
                        elapsed_sec=float(row.get("elapsed_sec", 0.0) or 0.0),
                        distance_from_market_pct=float(row.get("distance_from_market_pct", 0.0) or 0.0),
                        p_fill_30m=float(row.get("p_fill_30m", 0.5) or 0.5),
                        p_fill_1h=float(row.get("p_fill_1h", 0.5) or 0.5),
                        p_fill_4h=float(row.get("p_fill_4h", 0.5) or 0.5),
                        median_remaining_sec=float(row.get("median_remaining_sec", 0.0) or 0.0),
                        regime_agreement=float(row.get("regime_agreement", 1.0) or 1.0),
                        expected_value=float(row.get("expected_value", 0.0) or 0.0),
                        ev_trend=str(row.get("ev_trend", "stable") or "stable"),
                        recommended_action=str(row.get("recommended_action", "hold") or "hold"),
                        action_confidence=float(row.get("action_confidence", 0.0) or 0.0),
                    )
            raw_trade_counts = snap.get("trade_belief_action_counts", {})
            if isinstance(raw_trade_counts, dict):
                for key in ("hold", "tighten", "widen", "reprice_breakeven"):
                    self._trade_belief_action_counts[key] = max(0, int(raw_trade_counts.get(key, 0) or 0))
            self._trade_belief_last_update_ts = float(
                snap.get("trade_belief_last_update_ts", self._trade_belief_last_update_ts) or 0.0
            )
            self._trade_belief_ev_history = {}
            raw_ev_hist = snap.get("trade_belief_ev_history", {})
            if isinstance(raw_ev_hist, dict):
                for pid_txt, vals in raw_ev_hist.items():
                    try:
                        pid = int(pid_txt)
                    except (TypeError, ValueError):
                        continue
                    if pid <= 0 or not isinstance(vals, list):
                        continue
                    clean_vals = []
                    for v in vals:
                        try:
                            clean_vals.append(float(v))
                        except (TypeError, ValueError):
                            continue
                    if clean_vals:
                        self._trade_belief_ev_history[pid] = clean_vals[-20:]
            self._trade_belief_widen_count = {}
            raw_widen_count = snap.get("trade_belief_widen_count", {})
            if isinstance(raw_widen_count, dict):
                for pid_txt, count in raw_widen_count.items():
                    try:
                        pid = int(pid_txt)
                        c = int(count)
                    except (TypeError, ValueError):
                        continue
                    if pid > 0 and c > 0:
                        self._trade_belief_widen_count[pid] = c
            self._trade_belief_widen_total_pct = {}
            raw_widen_total = snap.get("trade_belief_widen_total_pct", {})
            if isinstance(raw_widen_total, dict):
                for pid_txt, total_pct in raw_widen_total.items():
                    try:
                        pid = int(pid_txt)
                        pct = float(total_pct)
                    except (TypeError, ValueError):
                        continue
                    if pid > 0 and pct > 0.0:
                        self._trade_belief_widen_total_pct[pid] = pct
            self._belief_timer_overrides = {}
            raw_timer_overrides = snap.get("belief_timer_overrides", {})
            if isinstance(raw_timer_overrides, dict):
                for pid_txt, until_ts in raw_timer_overrides.items():
                    try:
                        pid = int(pid_txt)
                        until = float(until_ts)
                    except (TypeError, ValueError):
                        continue
                    if pid > 0 and until > 0.0:
                        self._belief_timer_overrides[pid] = until
            self._refresh_belief_slot_overrides()
            self._regime_tier = int(snap.get("regime_tier", self._regime_tier))
            self._regime_tier = max(0, min(2, self._regime_tier))
            self._regime_tier_entered_at = float(snap.get("regime_tier_entered_at", self._regime_tier_entered_at))
            raw_grace_start = snap.get("regime_tier2_grace_start", self._regime_tier2_grace_start)
            self._regime_tier2_grace_start = float(raw_grace_start or 0.0)
            if self._regime_tier == 2 and self._regime_tier2_grace_start <= 0.0:
                self._regime_tier2_grace_start = float(self._regime_tier_entered_at)
            if self._regime_tier != 2:
                self._regime_tier2_grace_start = 0.0
            raw_suppressed = snap.get("regime_side_suppressed", self._regime_side_suppressed)
            self._regime_side_suppressed = raw_suppressed if raw_suppressed in ("A", "B", None) else None
            self._regime_last_eval_ts = float(snap.get("regime_last_eval_ts", self._regime_last_eval_ts))
            self._regime_tier2_last_downgrade_at = float(
                snap.get("regime_tier2_last_downgrade_at", self._regime_tier2_last_downgrade_at) or 0.0
            )
            raw_cooldown_side = snap.get("regime_cooldown_suppressed_side", self._regime_cooldown_suppressed_side)
            self._regime_cooldown_suppressed_side = (
                raw_cooldown_side if raw_cooldown_side in ("A", "B", None) else None
            )
            raw_tier_history = snap.get("regime_tier_history", self._regime_tier_history)
            if isinstance(raw_tier_history, list):
                self._regime_tier_history = list(raw_tier_history[-20:])
            raw_regime_shadow_state = snap.get("regime_shadow_state", self._regime_shadow_state)
            if isinstance(raw_regime_shadow_state, dict):
                self._regime_shadow_state = dict(raw_regime_shadow_state)
            self._regime_mechanical_tier = max(
                0,
                min(2, int(snap.get("regime_mechanical_tier", self._regime_mechanical_tier))),
            )
            raw_mech_dir = str(
                snap.get("regime_mechanical_direction", self._regime_mechanical_direction) or "symmetric"
            ).strip().lower()
            if raw_mech_dir not in {"symmetric", "long_bias", "short_bias"}:
                raw_mech_dir = "symmetric"
            self._regime_mechanical_direction = raw_mech_dir
            self._regime_mechanical_since = float(
                snap.get("regime_mechanical_since", self._regime_mechanical_since) or 0.0
            )
            self._regime_mechanical_tier_entered_at = float(
                snap.get(
                    "regime_mechanical_tier_entered_at",
                    self._regime_mechanical_tier_entered_at,
                )
                or 0.0
            )
            self._regime_mechanical_tier2_last_downgrade_at = float(
                snap.get(
                    "regime_mechanical_tier2_last_downgrade_at",
                    self._regime_mechanical_tier2_last_downgrade_at,
                )
                or 0.0
            )
            raw_override_tier = snap.get("ai_override_tier", self._ai_override_tier)
            try:
                self._ai_override_tier = (
                    max(0, min(2, int(raw_override_tier)))
                    if raw_override_tier is not None
                    else None
                )
            except (TypeError, ValueError):
                self._ai_override_tier = None
            raw_override_dir = snap.get("ai_override_direction", self._ai_override_direction)
            raw_override_dir = str(raw_override_dir).strip().lower() if raw_override_dir is not None else ""
            if raw_override_dir not in {"symmetric", "long_bias", "short_bias"}:
                self._ai_override_direction = None
            else:
                self._ai_override_direction = raw_override_dir
            raw_override_until = snap.get("ai_override_until", self._ai_override_until)
            try:
                self._ai_override_until = float(raw_override_until) if raw_override_until is not None else None
            except (TypeError, ValueError):
                self._ai_override_until = None
            raw_override_applied_at = snap.get("ai_override_applied_at", self._ai_override_applied_at)
            try:
                self._ai_override_applied_at = (
                    float(raw_override_applied_at) if raw_override_applied_at is not None else None
                )
            except (TypeError, ValueError):
                self._ai_override_applied_at = None
            raw_source_conv = snap.get("ai_override_source_conviction", self._ai_override_source_conviction)
            try:
                self._ai_override_source_conviction = (
                    max(0, min(100, int(raw_source_conv)))
                    if raw_source_conv is not None
                    else None
                )
            except (TypeError, ValueError):
                self._ai_override_source_conviction = None
            if self._ai_override_until is not None and float(self._ai_override_until) <= _now():
                self._clear_ai_override()
            raw_accum_state = str(snap.get("accum_state", self._accum_state) or "IDLE").strip().upper()
            if raw_accum_state not in {"IDLE", "ARMED", "ACTIVE", "COMPLETED", "STOPPED"}:
                raw_accum_state = "IDLE"
            self._accum_state = raw_accum_state
            raw_accum_dir = str(snap.get("accum_direction", self._accum_direction) or "").strip().lower()
            self._accum_direction = raw_accum_dir if raw_accum_dir in {"doge", "usd"} else None
            raw_trigger_from = str(
                snap.get("accum_trigger_from_regime", self._accum_trigger_from_regime) or "RANGING"
            ).strip().upper()
            raw_trigger_to = str(
                snap.get("accum_trigger_to_regime", self._accum_trigger_to_regime) or "RANGING"
            ).strip().upper()
            if raw_trigger_from not in {"BEARISH", "RANGING", "BULLISH"}:
                raw_trigger_from = "RANGING"
            if raw_trigger_to not in {"BEARISH", "RANGING", "BULLISH"}:
                raw_trigger_to = "RANGING"
            self._accum_trigger_from_regime = raw_trigger_from
            self._accum_trigger_to_regime = raw_trigger_to
            self._accum_start_ts = max(0.0, float(snap.get("accum_start_ts", self._accum_start_ts) or 0.0))
            self._accum_start_price = max(0.0, float(snap.get("accum_start_price", self._accum_start_price) or 0.0))
            self._accum_spent_usd = max(0.0, float(snap.get("accum_spent_usd", self._accum_spent_usd) or 0.0))
            self._accum_acquired_doge = max(
                0.0,
                float(snap.get("accum_acquired_doge", self._accum_acquired_doge) or 0.0),
            )
            self._accum_n_buys = max(0, int(snap.get("accum_n_buys", self._accum_n_buys) or 0))
            self._accum_last_buy_ts = max(0.0, float(snap.get("accum_last_buy_ts", self._accum_last_buy_ts) or 0.0))
            self._accum_budget_usd = max(0.0, float(snap.get("accum_budget_usd", self._accum_budget_usd) or 0.0))
            self._accum_armed_at = max(0.0, float(snap.get("accum_armed_at", self._accum_armed_at) or 0.0))
            self._accum_hold_streak = max(0, int(snap.get("accum_hold_streak", self._accum_hold_streak) or 0))
            self._accum_last_session_end_ts = max(
                0.0,
                float(snap.get("accum_last_session_end_ts", self._accum_last_session_end_ts) or 0.0),
            )
            raw_last_session_summary = snap.get("accum_last_session_summary", self._accum_last_session_summary)
            self._accum_last_session_summary = (
                dict(raw_last_session_summary) if isinstance(raw_last_session_summary, dict) else {}
            )
            self._accum_manual_stop_requested = bool(
                snap.get("accum_manual_stop_requested", self._accum_manual_stop_requested)
            )
            self._accum_cooldown_remaining_sec = max(
                0,
                int(snap.get("accum_cooldown_remaining_sec", self._accum_cooldown_remaining_sec) or 0),
            )
            self._entry_adds_deferred_total = int(snap.get("entry_adds_deferred_total", self._entry_adds_deferred_total))
            self._entry_adds_drained_total = int(snap.get("entry_adds_drained_total", self._entry_adds_drained_total))
            self._entry_adds_last_deferred_at = float(
                snap.get("entry_adds_last_deferred_at", self._entry_adds_last_deferred_at)
            )
            self._entry_adds_last_drained_at = float(
                snap.get("entry_adds_last_drained_at", self._entry_adds_last_drained_at)
            )
            self._daily_loss_lock_active = bool(snap.get("daily_loss_lock_active", self._daily_loss_lock_active))
            self._daily_loss_lock_utc_day = str(snap.get("daily_loss_lock_utc_day", self._daily_loss_lock_utc_day) or "")
            self._daily_realized_loss_utc = float(snap.get("daily_realized_loss_utc", self._daily_realized_loss_utc))
            self._sticky_release_total = int(snap.get("sticky_release_total", self._sticky_release_total))
            self._sticky_release_last_at = float(snap.get("sticky_release_last_at", self._sticky_release_last_at))
            self._release_recon_blocked = bool(snap.get("release_recon_blocked", self._release_recon_blocked))
            self._release_recon_blocked_reason = str(
                snap.get("release_recon_blocked_reason", self._release_recon_blocked_reason) or ""
            )
            self._self_heal_reprice_total = int(
                snap.get("self_heal_reprice_total", self._self_heal_reprice_total)
            )
            self._self_heal_reprice_last_at = float(
                snap.get("self_heal_reprice_last_at", self._self_heal_reprice_last_at)
            )
            raw_self_heal_summary = snap.get("self_heal_reprice_last_summary", self._self_heal_reprice_last_summary)
            self._self_heal_reprice_last_summary = (
                dict(raw_self_heal_summary) if isinstance(raw_self_heal_summary, dict) else {}
            )
            self._self_heal_hold_until_by_position = {}
            raw_hold_until = snap.get("self_heal_hold_until_by_position", {})
            if isinstance(raw_hold_until, dict):
                for position_id_raw, until_raw in raw_hold_until.items():
                    try:
                        position_id = int(position_id_raw)
                        until_ts = float(until_raw)
                    except (TypeError, ValueError):
                        continue
                    if position_id > 0 and until_ts > 0.0:
                        self._self_heal_hold_until_by_position[position_id] = until_ts
            self._position_ledger_migration_done = bool(
                snap.get("position_ledger_migration_done", self._position_ledger_migration_done)
            )
            self._position_ledger_migration_last_at = float(
                snap.get("position_ledger_migration_last_at", self._position_ledger_migration_last_at) or 0.0
            )
            self._position_ledger_migration_last_created = max(
                0,
                int(
                    snap.get(
                        "position_ledger_migration_last_created",
                        self._position_ledger_migration_last_created,
                    )
                    or 0
                ),
            )
            self._position_ledger_migration_last_scanned = max(
                0,
                int(
                    snap.get(
                        "position_ledger_migration_last_scanned",
                        self._position_ledger_migration_last_scanned,
                    )
                    or 0
                ),
            )
            self._dust_last_absorbed_usd = max(
                0.0,
                float(snap.get("dust_last_absorbed_usd", self._dust_last_absorbed_usd)),
            )
            self._dust_last_dividend_usd = max(
                0.0,
                float(snap.get("dust_last_dividend_usd", self._dust_last_dividend_usd)),
            )
            self._quote_first_carry_usd = max(
                0.0,
                float(snap.get("quote_first_carry_usd", self._quote_first_carry_usd)),
            )
            hist = snap.get("rebalancer_sign_flip_history", [])
            cleaned_hist: list[float] = []
            if isinstance(hist, list):
                for row in hist:
                    try:
                        cleaned_hist.append(float(row))
                    except Exception:
                        continue
            self._rebalancer_sign_flip_history = deque(sorted(cleaned_hist)[-20:])
            self._restore_hmm_snapshot(snap)
            self._hmm_consensus = self._compute_hmm_consensus()
            if self._throughput is not None:
                self._throughput.restore_state(snap.get("throughput_sizer_state", {}))
            self._position_ledger.restore_state(snap.get("position_ledger_state", {}))
            self._position_by_exit_local = {}
            raw_pos_local = snap.get("position_by_exit_local", [])
            if isinstance(raw_pos_local, list):
                for row in raw_pos_local:
                    if not isinstance(row, dict):
                        continue
                    try:
                        key = (int(row.get("slot_id")), int(row.get("local_id")))
                        self._position_by_exit_local[key] = int(row.get("position_id"))
                    except (TypeError, ValueError):
                        continue
            self._position_by_exit_txid = {}
            raw_pos_txid = snap.get("position_by_exit_txid", {})
            if isinstance(raw_pos_txid, dict):
                for txid, pid in raw_pos_txid.items():
                    try:
                        pid_int = int(pid)
                    except (TypeError, ValueError):
                        continue
                    txid_norm = str(txid or "").strip()
                    if txid_norm and pid_int > 0:
                        self._position_by_exit_txid[txid_norm] = pid_int
            self._cycle_slot_mode = {}
            raw_cycle_modes = snap.get("cycle_slot_mode", [])
            if isinstance(raw_cycle_modes, list):
                for row in raw_cycle_modes:
                    if not isinstance(row, dict):
                        continue
                    try:
                        key = (
                            int(row.get("slot_id")),
                            str(row.get("trade_id") or ""),
                            int(row.get("cycle")),
                        )
                        self._cycle_slot_mode[key] = str(row.get("slot_mode") or "legacy")
                    except (TypeError, ValueError):
                        continue
            self._churner_next_cycle_id = max(1, int(snap.get("churner_next_cycle_id", self._churner_next_cycle_id)))
            self._churner_reserve_available_usd = max(
                0.0,
                float(snap.get("churner_reserve_available_usd", self._churner_reserve_available_usd) or 0.0),
            )
            self._churner_day_key = str(snap.get("churner_day_key", self._churner_day_key) or self._utc_day_key())
            self._churner_cycles_today = max(0, int(snap.get("churner_cycles_today", self._churner_cycles_today) or 0))
            self._churner_profit_today = float(snap.get("churner_profit_today", self._churner_profit_today) or 0.0)
            self._churner_cycles_total = max(0, int(snap.get("churner_cycles_total", self._churner_cycles_total) or 0))
            self._churner_profit_total = float(snap.get("churner_profit_total", self._churner_profit_total) or 0.0)
            self._churner_by_slot = {}
            raw_churner_by_slot = snap.get("churner_by_slot", {})
            if isinstance(raw_churner_by_slot, dict):
                for sid_txt, row in raw_churner_by_slot.items():
                    if not isinstance(row, dict):
                        continue
                    try:
                        sid = int(sid_txt)
                    except (TypeError, ValueError):
                        continue
                    try:
                        state = ChurnerRuntimeState(
                            active=bool(row.get("active", False)),
                            stage=str(row.get("stage") or "idle"),
                            parent_position_id=max(0, int(row.get("parent_position_id", 0) or 0)),
                            parent_trade_id=str(row.get("parent_trade_id") or ""),
                            cycle_id=max(0, int(row.get("cycle_id", 0) or 0)),
                            order_size_usd=max(0.0, float(row.get("order_size_usd", 0.0) or 0.0)),
                            compound_usd=max(0.0, float(row.get("compound_usd", 0.0) or 0.0)),
                            reserve_allocated_usd=max(
                                0.0, float(row.get("reserve_allocated_usd", 0.0) or 0.0)
                            ),
                            entry_side=str(row.get("entry_side") or ""),
                            entry_txid=str(row.get("entry_txid") or ""),
                            entry_price=max(0.0, float(row.get("entry_price", 0.0) or 0.0)),
                            entry_volume=max(0.0, float(row.get("entry_volume", 0.0) or 0.0)),
                            entry_placed_at=max(0.0, float(row.get("entry_placed_at", 0.0) or 0.0)),
                            entry_fill_price=max(0.0, float(row.get("entry_fill_price", 0.0) or 0.0)),
                            entry_fill_fee=max(0.0, float(row.get("entry_fill_fee", 0.0) or 0.0)),
                            entry_fill_time=max(0.0, float(row.get("entry_fill_time", 0.0) or 0.0)),
                            exit_txid=str(row.get("exit_txid") or ""),
                            exit_price=max(0.0, float(row.get("exit_price", 0.0) or 0.0)),
                            exit_placed_at=max(0.0, float(row.get("exit_placed_at", 0.0) or 0.0)),
                            churner_position_id=max(0, int(row.get("churner_position_id", 0) or 0)),
                            last_error=str(row.get("last_error") or ""),
                            last_state_change_at=max(
                                0.0, float(row.get("last_state_change_at", 0.0) or 0.0)
                            ),
                        )
                    except (TypeError, ValueError):
                        continue
                    if state.stage not in {"idle", "entry_open", "exit_open"}:
                        state.stage = "idle"
                    self._churner_by_slot[sid] = state

            self.slots = {}
            slot_aliases = snap.get("slot_aliases", {})
            slot_aliases = slot_aliases if isinstance(slot_aliases, dict) else {}
            for sid_text, raw_state in (snap.get("slots", {}) or {}).items():
                sid = int(sid_text)
                alias = str(slot_aliases.get(str(sid)) or slot_aliases.get(sid) or "").strip().lower()
                self.slots[sid] = SlotRuntime(slot_id=sid, state=sm.from_dict(raw_state), alias=alias)

            self._sanitize_slot_alias_state()
            self._reconcile_churner_state()
            self._rebuild_position_bindings_from_open_orders()
            self._recompute_effective_layers()

        # Startup rebase: if snapshot lagged behind queued event writes before a
        # restart, avoid duplicate-key collisions on bot_events(event_id).
        db_max_event_id = supabase_store.load_max_event_id()
        if db_max_event_id >= self.next_event_id:
            old = self.next_event_id
            self.next_event_id = db_max_event_id + 1
            logger.info("Rebased next_event_id from %d to %d using Supabase max", old, self.next_event_id)

    def _log_event(
        self,
        slot_id: int,
        from_state: str,
        to_state: str,
        event_type: str,
        details: dict,
    ) -> None:
        event_id = self.next_event_id
        self.next_event_id += 1
        row = {
            "event_id": event_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pair": self.pair,
            "slot_id": slot_id,
            "from_state": from_state,
            "to_state": to_state,
            "event_type": event_type,
            "details": details,
        }
        supabase_store.save_event(row)

    # ------------------ Loop API Budget ------------------

    def begin_loop(self) -> None:
        self.enforce_loop_budget = True
        self.loop_private_calls = 0
        self.entry_adds_per_loop_used = 0
        self.entry_adds_per_loop_cap = self._compute_entry_adds_loop_cap()
        self._loop_balance_cache = None
        self._loop_available_usd = None
        self._loop_available_doge = None
        self._loop_dust_dividend = None
        self._loop_b_side_base = None
        self._loop_quote_first_meta = None
        self._loop_effective_layers = None
        # Sync capital ledger with fresh Kraken balance
        bal = self._safe_balance()
        if bal:
            self.ledger.sync(bal, self.slots)
            self._loop_available_usd = self.ledger.available_usd
            self._loop_available_doge = self.ledger.available_doge
        # Prime the effective-layer snapshot once per loop.
        self._loop_effective_layers = self._recompute_effective_layers()

    def end_loop(self) -> None:
        self.enforce_loop_budget = False
        self.entry_adds_per_loop_used = 0
        self._loop_balance_cache = None
        self._loop_available_usd = None
        self._loop_available_doge = None
        self._loop_dust_dividend = None
        self._loop_b_side_base = None
        self._loop_quote_first_meta = None
        self._loop_effective_layers = None
        self.ledger.clear()

    def _consume_private_budget(self, units: int, reason: str) -> bool:
        if units <= 0:
            return True
        if not self.enforce_loop_budget:
            return True
        limit = max(1, int(config.MAX_API_CALLS_PER_LOOP))
        if self.loop_private_calls + units > limit:
            logger.warning(
                "Loop private API budget exhausted (%d/%d), skipping %s",
                self.loop_private_calls,
                limit,
                reason,
            )
            return False
        self.loop_private_calls += units
        return True

    def _get_open_orders(self) -> dict:
        if not self._consume_private_budget(1, "get_open_orders"):
            return {}
        out = kraken_client.get_open_orders()
        self._kraken_open_orders_current = self._count_pair_open_orders(out)
        self._kraken_open_orders_ts = _now()
        return out

    def _get_trades_history(self, start: float | None = None) -> dict:
        if not self._consume_private_budget(1, "get_trades_history"):
            return {}
        return kraken_client.get_trades_history(start=start)

    def _query_orders_batched(self, txids: list[str], batch_size: int = 50) -> dict:
        if not txids:
            return {}
        if not self.enforce_loop_budget:
            return kraken_client.query_orders_batched(txids, batch_size=batch_size)

        limit = max(1, int(config.MAX_API_CALLS_PER_LOOP))
        remaining = limit - self.loop_private_calls
        if remaining <= 0:
            logger.warning("Loop private API budget exhausted, skipping query_orders")
            return {}
        max_txids = remaining * batch_size
        bounded = txids[:max_txids]
        units = ceil(len(bounded) / batch_size)
        self.loop_private_calls += units
        return kraken_client.query_orders_batched(bounded, batch_size=batch_size)

    def _place_order(self, *, side: str, volume: float, price: float, userref: int) -> str | None:
        if not self._consume_private_budget(1, "place_order"):
            return None
        return kraken_client.place_order(
            side=side,
            volume=volume,
            price=price,
            pair=self.pair,
            ordertype="limit",
            post_only=True,
            userref=userref,
        )

    def _place_market_order(self, *, side: str, volume: float, userref: int) -> str | None:
        if not self._consume_private_budget(1, "place_market_order"):
            return None
        mark = float(self.last_price if self.last_price > 0 else 0.0)
        return kraken_client.place_order(
            side=side,
            volume=volume,
            price=mark,
            pair=self.pair,
            ordertype="market",
            post_only=False,
            userref=userref,
        )

    def _cancel_order(self, txid: str) -> bool:
        if not txid:
            return False
        if not self._consume_private_budget(1, "cancel_order"):
            return False
        return kraken_client.cancel_order(txid)

    def _refresh_open_order_telemetry(self) -> None:
        try:
            self._get_open_orders()
            self._maybe_alert_persistent_open_order_drift()
        except Exception as e:
            logger.debug("Open-order telemetry refresh failed: %s", e)

    def _cleanup_recovery_orders_on_startup(self) -> tuple[int, int, int]:
        if self._recovery_orders_enabled():
            return 0, 0, 0

        cleared = 0
        cancelled = 0
        failed = 0
        seen_txids: set[str] = set()

        for sid in sorted(self.slots.keys()):
            slot = self.slots[sid]
            recoveries = tuple(slot.state.recovery_orders)
            if not recoveries:
                continue

            for rec in recoveries:
                txid = str(rec.txid or "").strip()
                if not txid or txid in seen_txids:
                    continue
                seen_txids.add(txid)
                try:
                    ok = self._cancel_order(txid)
                except Exception as e:
                    logger.warning(
                        "startup recovery cleanup: cancel failed slot=%s recovery_id=%s txid=%s: %s",
                        sid,
                        int(rec.recovery_id),
                        txid,
                        e,
                    )
                    failed += 1
                    continue
                if ok:
                    cancelled += 1
                else:
                    failed += 1

            cleared += len(recoveries)
            slot.state = replace(slot.state, recovery_orders=tuple())

        if cleared > 0:
            logger.info(
                "startup recovery cleanup: cleared=%d cancelled=%d failed=%d",
                cleared,
                cancelled,
                failed,
            )
        return cleared, cancelled, failed

    # ------------------ Lifecycle ------------------

    def initialize(self) -> None:
        logger.info("============================================================")
        logger.info("  DOGE STATE-MACHINE BOT v1")
        logger.info("============================================================")

        supabase_store.start_writer_thread()

        # Fetch latest exchange constraints + fees.
        self.constraints = kraken_client.get_pair_constraints(self.pair)
        self.maker_fee_pct, self.taker_fee_pct = kraken_client.get_fee_rates(self.pair)

        # Restore runtime snapshot.
        self._load_snapshot()
        self._load_equity_ts_history()
        self._sanitize_slot_alias_state()
        self._cleanup_recovery_orders_on_startup()

        # Ensure at least slot 0 exists.
        if not self.slots:
            ts = _now()
            self.slots[0] = SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.0,
                    now=ts,
                    profit_pct_runtime=self.profit_pct,
                ),
                alias=self._allocate_slot_alias(),
            )
            self.next_slot_id = 1

        # Get initial market price.
        self._refresh_price(strict=True)
        # Prime OHLCV + optional one-time backfill + HMM state before entering the loop.
        self._sync_ohlcv_candles(_now())
        self._maybe_backfill_ohlcv_on_startup()
        self._hmm_readiness_cache = {}
        self._hmm_readiness_last_ts = {}
        startup_now = _now()
        self._update_micro_features(startup_now)
        self._update_hmm(startup_now)
        self._build_belief_state(startup_now)
        self._maybe_retrain_survival_model(startup_now, force=True)
        self._update_regime_tier(startup_now)
        self._update_manifold_score(startup_now)

        # Push price into all slots.
        for sid, slot in self.slots.items():
            self.slots[sid].state = replace(
                slot.state,
                market_price=self.last_price,
                now=self.last_price_ts,
                last_price_update_at=self.last_price_ts,
                profit_pct_runtime=self.profit_pct,
            )

        # Reconcile + exactly-once replay for missed fills after restart.
        open_orders = self._reconcile_open_orders()
        self._replay_missed_fills(open_orders)

        # Ensure each slot has active entries/exits after reconciliation/replay.
        for sid in sorted(self.slots.keys()):
            self._ensure_slot_bootstrapped(sid)

        # Initialize/reconcile self-healing position ledger bindings.
        self._migrate_open_exits_to_position_ledger()
        self._update_trade_beliefs(_now())

        self._recompute_effective_layers()

        if self.mode not in ("PAUSED", "HALTED"):
            self.mode = "RUNNING"

        self._save_snapshot()
        notifier._send_message(
            f"<b>DOGE v1 started</b>\n"
            f"pair: {self.pair_display}\n"
            f"slots: {len(self.slots)}\n"
            f"maker fee: {self.maker_fee_pct:.3f}%\n"
            f"min vol: {self.constraints.get('min_volume')}"
        )

    def shutdown(self, reason: str) -> None:
        with self.lock:
            self.running = False
            self.mode = "HALTED"
            self.pause_reason = reason
            now = _now()
            self._update_doge_eq_snapshot(now)
            if self._equity_ts_enabled and self._equity_ts_dirty:
                self._flush_equity_ts(now)
            self._save_snapshot()
        notifier._send_message(f"<b>DOGE v1 stopped</b>\nreason: {reason}")

    # ------------------ Pause/Halt ------------------

    def pause(self, reason: str) -> None:
        if self.mode != "PAUSED":
            self.mode = "PAUSED"
            self.pause_reason = reason
            notifier.notify_risk_event("pause", reason, self.pair_display)

    def resume(self) -> tuple[bool, str]:
        self._update_daily_loss_lock(_now())
        if self._daily_loss_lock_active:
            msg = (
                "daily loss lock active "
                f"(UTC {self._daily_loss_lock_utc_day or self._utc_day_key()}); manual resume available after rollover"
            )
            self.pause_reason = msg
            return False, msg
        if self.mode == "HALTED":
            return False, "bot halted"
        self.mode = "RUNNING"
        self.pause_reason = ""
        self.consecutive_api_errors = 0
        notifier.notify_risk_event("resume", "Resumed by operator", self.pair_display)
        return True, "running"

    def halt(self, reason: str) -> None:
        self.mode = "HALTED"
        self.pause_reason = reason
        notifier.notify_error(f"HALTED: {reason}")

    # ------------------ Market / Stats ------------------

    def _refresh_price(self, strict: bool = False) -> None:
        try:
            px = float(kraken_client.get_price(pair=self.pair))
            ts = _now()
            self.last_price = px
            self.last_price_ts = ts
            self.price_history.append((ts, px))
            self.price_history = [(t, p) for (t, p) in self.price_history if ts - t <= 86400]
            supabase_store.queue_price_point(ts, px, pair=self.pair)
            self.consecutive_api_errors = 0
        except Exception as e:
            self.consecutive_api_errors += 1
            logger.warning("Price refresh failed (%d): %s", self.consecutive_api_errors, e)
            if strict:
                raise
            if self.consecutive_api_errors >= config.MAX_CONSECUTIVE_ERRORS:
                self.pause(f"{self.consecutive_api_errors} consecutive API errors")

    @staticmethod
    def _hmm_quality_tier(
        current_candles: int,
        target_candles: int,
        min_train_samples: int,
    ) -> tuple[str, float]:
        current = max(0, int(current_candles))
        target = max(1, int(target_candles))
        min_train = max(1, int(min_train_samples))

        if current >= target:
            return "full", 1.00

        baseline_threshold = max(min_train, int(round(target * 0.25)))
        deep_threshold = max(min_train, int(round(target * 0.625)))
        if deep_threshold <= baseline_threshold:
            deep_threshold = baseline_threshold + 1

        if current >= deep_threshold:
            return "deep", 0.95
        if current >= baseline_threshold:
            return "baseline", 0.85
        return "shallow", 0.70

    def _hmm_training_depth_default(self, *, state_key: str = "primary") -> dict[str, Any]:
        use_secondary = str(state_key).lower() == "secondary"
        use_tertiary = str(state_key).lower() == "tertiary"
        if use_secondary:
            target_candles = max(1, int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 1440)))
            min_train_samples = max(1, int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)))
            interval_min = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
            key = "secondary"
        elif use_tertiary:
            target_candles = max(1, int(getattr(config, "HMM_TERTIARY_TRAINING_CANDLES", 500)))
            min_train_samples = max(1, int(getattr(config, "HMM_TERTIARY_MIN_TRAIN_SAMPLES", 150)))
            interval_min = max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)))
            key = "tertiary"
        else:
            target_candles = max(1, int(getattr(config, "HMM_TRAINING_CANDLES", 4000)))
            min_train_samples = max(1, int(getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)))
            interval_min = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
            key = "primary"

        quality_tier, modifier = self._hmm_quality_tier(
            0,
            target_candles,
            min_train_samples,
        )
        return {
            "state_key": key,
            "current_candles": 0,
            "target_candles": target_candles,
            "min_train_samples": min_train_samples,
            "quality_tier": quality_tier,
            "confidence_modifier": modifier,
            "pct_complete": 0.0,
            "interval_min": interval_min,
            "estimated_full_at": None,
            "updated_at": 0.0,
        }

    def _update_hmm_training_depth(
        self,
        *,
        current_candles: int,
        secondary: bool = False,
        tertiary: bool = False,
        target_candles: int | None = None,
        min_train_samples: int | None = None,
        interval_min: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now_ts = float(now if now is not None else _now())
        if secondary:
            target = max(
                1,
                int(
                    target_candles
                    if target_candles is not None
                    else getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 1440)
                ),
            )
            min_train = max(
                1,
                int(
                    min_train_samples
                    if min_train_samples is not None
                    else getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)
                ),
            )
            interval = max(
                1,
                int(
                    interval_min
                    if interval_min is not None
                    else getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)
                ),
            )
            state_key = "secondary"
        elif tertiary:
            target = max(
                1,
                int(
                    target_candles
                    if target_candles is not None
                    else getattr(config, "HMM_TERTIARY_TRAINING_CANDLES", 500)
                ),
            )
            min_train = max(
                1,
                int(
                    min_train_samples
                    if min_train_samples is not None
                    else getattr(config, "HMM_TERTIARY_MIN_TRAIN_SAMPLES", 150)
                ),
            )
            interval = max(
                1,
                int(
                    interval_min
                    if interval_min is not None
                    else getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)
                ),
            )
            state_key = "tertiary"
        else:
            target = max(
                1,
                int(
                    target_candles
                    if target_candles is not None
                    else getattr(config, "HMM_TRAINING_CANDLES", 4000)
                ),
            )
            min_train = max(
                1,
                int(
                    min_train_samples
                    if min_train_samples is not None
                    else getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)
                ),
            )
            interval = max(
                1,
                int(
                    interval_min
                    if interval_min is not None
                    else getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)
                ),
            )
            state_key = "primary"

        current = max(0, int(current_candles))
        quality_tier, modifier = self._hmm_quality_tier(current, target, min_train)
        pct_complete = min(100.0, (current / target * 100.0)) if target > 0 else 100.0

        estimated_full_at: str | None = None
        if current < target:
            remaining = target - current
            eta_ts = now_ts + float(remaining * interval * 60)
            estimated_full_at = datetime.fromtimestamp(eta_ts, timezone.utc).isoformat()

        out = {
            "state_key": state_key,
            "current_candles": current,
            "target_candles": target,
            "min_train_samples": min_train,
            "quality_tier": quality_tier,
            "confidence_modifier": float(modifier),
            "pct_complete": round(float(pct_complete), 2),
            "interval_min": interval,
            "estimated_full_at": estimated_full_at,
            "updated_at": float(now_ts),
        }
        if secondary:
            self._hmm_training_depth_secondary = out
        elif tertiary:
            self._hmm_training_depth_tertiary = out
        else:
            self._hmm_training_depth = out
        return out

    def _hmm_confidence_modifier_for_source(self, source: dict[str, Any] | None) -> tuple[float, str]:
        if not isinstance(source, dict):
            return 1.0, "default"
        source_mode = str(source.get("source_mode", "") or "").strip().lower()
        multi_enabled = bool(source.get("multi_timeframe", False))
        if source_mode == "consensus" and multi_enabled:
            primary_mod = float(self._hmm_training_depth.get("confidence_modifier", 1.0) or 1.0)
            secondary_mod = float(
                self._hmm_training_depth_secondary.get("confidence_modifier", 1.0) or 1.0
            )
            return max(0.0, min(1.0, min(primary_mod, secondary_mod))), "consensus_min"
        primary_mod = float(self._hmm_training_depth.get("confidence_modifier", 1.0) or 1.0)
        return max(0.0, min(1.0, primary_mod)), "primary"

    def _record_regime_history_sample(self, now: float | None = None) -> None:
        now_ts = float(now if now is not None else _now())
        source = dict(self._policy_hmm_source() or {})
        regime = str(source.get("regime", "RANGING") or "RANGING").upper()
        confidence = max(0.0, min(1.0, float(source.get("confidence", 0.0) or 0.0)))
        bias = max(-1.0, min(1.0, float(source.get("bias_signal", 0.0) or 0.0)))

        self._regime_history_30m.append(
            {
                "ts": now_ts,
                "regime": regime,
                "conf": round(confidence, 4),
                "bias": round(bias, 4),
            }
        )

        cutoff = now_ts - float(self._regime_history_window_sec)
        while self._regime_history_30m and float(self._regime_history_30m[0].get("ts", 0.0)) < cutoff:
            self._regime_history_30m.popleft()
        if len(self._regime_history_30m) > 512:
            while len(self._regime_history_30m) > 512:
                self._regime_history_30m.popleft()

    def _hmm_default_state(
        self,
        *,
        enabled: bool | None = None,
        interval_min: int | None = None,
    ) -> dict[str, Any]:
        blend = max(0.0, min(1.0, float(getattr(config, "HMM_BLEND_WITH_TREND", 0.5))))
        use_enabled = self._flag_value("HMM_ENABLED") if enabled is None else bool(enabled)
        use_interval = max(
            1,
            int(
                getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)
                if interval_min is None
                else interval_min
            ),
        )
        return {
            "enabled": use_enabled,
            "available": False,
            "trained": False,
            "interval_min": use_interval,
            "regime": "RANGING",
            "regime_id": 1,
            "confidence": 0.0,
            "bias_signal": 0.0,
            "probabilities": {
                "bearish": 0.0,
                "ranging": 1.0,
                "bullish": 0.0,
            },
            "observation_count": 0,
            "blend_factor": blend,
            "last_update_ts": 0.0,
            "last_train_ts": 0.0,
            "agreement": "single",
            "source_mode": "primary",
            "multi_timeframe": False,
            "error": "",
        }

    def _hmm_source_mode(self) -> str:
        raw = str(getattr(config, "HMM_MULTI_TIMEFRAME_SOURCE", "primary") or "primary").strip().lower()
        mode = "consensus" if raw == "consensus" else "primary"
        if mode == "consensus" and not self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"):
            return "primary"
        return mode

    def _policy_hmm_source(self) -> dict[str, Any]:
        primary = self._hmm_state if isinstance(self._hmm_state, dict) else self._hmm_default_state()
        if not self._flag_value("HMM_ENABLED"):
            return primary
        if not self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"):
            return primary
        if self._hmm_source_mode() != "consensus":
            return primary
        if isinstance(self._hmm_consensus, dict) and self._hmm_consensus:
            return self._hmm_consensus
        return primary

    def _policy_hmm_signal(self) -> tuple[str, float, float, bool, dict[str, Any]]:
        source = dict(self._policy_hmm_source() or {})
        regime = str(source.get("regime", "RANGING") or "RANGING").upper()
        confidence = max(0.0, min(1.0, float(source.get("confidence", 0.0) or 0.0)))
        bias = float(source.get("bias_signal", 0.0) or 0.0)
        ready = bool(
            self._flag_value("HMM_ENABLED")
            and bool(source.get("available"))
            and bool(source.get("trained"))
        )
        return regime, confidence, bias, ready, source

    def _current_regime_id(self) -> int | None:
        if not self._flag_value("HMM_ENABLED"):
            return None
        source = dict(self._policy_hmm_source() or {})
        if not (bool(source.get("available")) and bool(source.get("trained"))):
            return None
        try:
            regime_id = int(source.get("regime_id", 1))
        except (TypeError, ValueError):
            return None
        return regime_id if regime_id in (0, 1, 2) else None

    @staticmethod
    def _regime_label(regime_id: int | None) -> str:
        return {0: "bearish", 1: "ranging", 2: "bullish"}.get(regime_id, "ranging")

    def _position_ledger_enabled(self) -> bool:
        return self._flag_value("POSITION_LEDGER_ENABLED") and bool(
            self._position_ledger.enabled
        )

    def _slot_mode_for_position(self, *, churner: bool = False) -> str:
        if churner:
            return "churner"
        if self._flag_value("STICKY_MODE_ENABLED"):
            return "sticky"
        return "legacy"

    def _bind_position_for_exit(self, slot_id: int, order: sm.OrderState, position_id: int) -> None:
        key = (int(slot_id), int(order.local_id))
        self._position_by_exit_local[key] = int(position_id)
        txid = str(order.txid or "").strip()
        if txid:
            self._position_by_exit_txid[txid] = int(position_id)
            self._position_ledger.bind_exit_txid(int(position_id), txid)

    def _bind_position_txid_for_exit(self, slot_id: int, local_id: int, txid: str) -> None:
        key = (int(slot_id), int(local_id))
        position_id = self._position_by_exit_local.get(key)
        if not position_id:
            return
        txid_norm = str(txid or "").strip()
        if not txid_norm:
            return
        self._position_by_exit_txid[txid_norm] = int(position_id)
        self._position_ledger.bind_exit_txid(int(position_id), txid_norm)
        self._persist_position_ledger_row(int(position_id))

    def _unbind_position_for_exit(self, slot_id: int, local_id: int, txid: str = "") -> int | None:
        key = (int(slot_id), int(local_id))
        position_id = self._position_by_exit_local.pop(key, None)
        txid_norm = str(txid or "").strip()
        if txid_norm:
            self._position_by_exit_txid.pop(txid_norm, None)
        if position_id is None:
            return None
        if not txid_norm:
            for k, v in list(self._position_by_exit_txid.items()):
                if int(v) == int(position_id):
                    self._position_by_exit_txid.pop(k, None)
        return int(position_id)

    def _find_position_for_exit(
        self,
        slot_id: int,
        local_id: int,
        *,
        txid: str | None = None,
    ) -> int | None:
        pid = self._position_by_exit_local.get((int(slot_id), int(local_id)))
        if pid is not None:
            return int(pid)
        txid_norm = str(txid or "").strip()
        if txid_norm and txid_norm in self._position_by_exit_txid:
            return int(self._position_by_exit_txid[txid_norm])
        return None

    def _rebuild_position_bindings_from_open_orders(self) -> int:
        self._position_by_exit_local = {}
        self._position_by_exit_txid = {}
        if not self._position_ledger_enabled():
            return 0

        bound = 0
        for pos in self._position_ledger.get_open_positions():
            sid = int(pos.get("slot_id", -1))
            slot = self.slots.get(sid)
            if slot is None:
                continue
            trade_id = str(pos.get("trade_id", ""))
            cycle = int(pos.get("cycle", 0))
            entry_time = float(pos.get("entry_time", 0.0) or 0.0)
            candidates = [
                o
                for o in slot.state.orders
                if o.role == "exit"
                and str(o.trade_id) == trade_id
                and int(o.cycle) == cycle
            ]
            if not candidates:
                continue
            if len(candidates) == 1:
                chosen = candidates[0]
            else:
                chosen = min(
                    candidates,
                    key=lambda o: abs(float(o.entry_filled_at or o.placed_at or 0.0) - entry_time),
                )
            position_id = int(pos.get("position_id", 0) or 0)
            if position_id <= 0:
                continue
            self._bind_position_for_exit(sid, chosen, position_id)
            bound += 1
        return bound

    def _record_position_open_for_entry_fill(
        self,
        *,
        slot_id: int,
        entry_order: sm.OrderState,
        fill_price: float,
        fill_volume: float,
        fill_fee: float,
        fill_cost: float,
        fill_timestamp: float,
    ) -> None:
        if not self._position_ledger_enabled():
            return
        slot = self.slots.get(int(slot_id))
        if slot is None:
            return

        candidates = [
            o
            for o in slot.state.orders
            if o.role == "exit"
            and str(o.trade_id) == str(entry_order.trade_id)
            and int(o.cycle) == int(entry_order.cycle)
        ]
        if not candidates:
            return

        expected_entry_ts = float(fill_timestamp)
        chosen = min(
            candidates,
            key=lambda o: abs(float(o.entry_filled_at or o.placed_at or 0.0) - expected_entry_ts),
        )

        try:
            regime_label = self._regime_label(chosen.regime_at_entry)
            position_id = self._position_ledger.open_position(
                slot_id=int(slot_id),
                trade_id=str(entry_order.trade_id),
                slot_mode=self._slot_mode_for_position(churner=False),
                cycle=int(entry_order.cycle),
                entry_data={
                    "entry_price": float(fill_price),
                    "entry_cost": float(fill_cost if fill_cost > 0 else fill_price * fill_volume),
                    "entry_fee": float(fill_fee),
                    "entry_volume": float(fill_volume),
                    "entry_time": float(fill_timestamp),
                    "entry_regime": regime_label,
                    "entry_volatility": 0.0,
                },
                exit_data={
                    "current_exit_price": float(chosen.price),
                    "original_exit_price": float(chosen.price),
                    "target_profit_pct": float(self.profit_pct),
                    "exit_txid": str(chosen.txid or ""),
                },
            )
            self._position_ledger.journal_event(
                position_id,
                "created",
                {
                    "entry_price": float(fill_price),
                    "exit_price": float(chosen.price),
                    "regime": regime_label,
                    "slot_mode": self._slot_mode_for_position(churner=False),
                },
                timestamp=float(fill_timestamp),
            )
            self._bind_position_for_exit(int(slot_id), chosen, int(position_id))
            self._persist_position_ledger_row(int(position_id))
            self._persist_position_journal_tail(int(position_id), count=1)
            self._stamp_belief_entry_metadata(
                slot_id=int(slot_id),
                trade_id=str(entry_order.trade_id),
                cycle=int(entry_order.cycle),
                entry_price=float(fill_price),
                exit_price=float(chosen.price),
                entry_ts=float(fill_timestamp),
            )
        except Exception as e:
            logger.warning(
                "position_ledger open failed slot=%s trade=%s cycle=%s: %s",
                int(slot_id),
                str(entry_order.trade_id),
                int(entry_order.cycle),
                e,
            )

    def _record_position_close_for_exit_fill(
        self,
        *,
        slot_id: int,
        exit_order: sm.OrderState,
        fill_price: float,
        fill_fee: float,
        fill_cost: float,
        fill_timestamp: float,
        txid: str,
    ) -> None:
        if not self._position_ledger_enabled():
            return

        position_id = self._find_position_for_exit(
            int(slot_id),
            int(exit_order.local_id),
            txid=str(txid or ""),
        )
        if position_id is None:
            return

        pos = self._position_ledger.get_position(int(position_id))
        if not isinstance(pos, dict):
            self._unbind_position_for_exit(int(slot_id), int(exit_order.local_id), str(txid or ""))
            return

        slot = self.slots.get(int(slot_id))
        cycle_row = None
        if slot is not None:
            cycle_row = next(
                (
                    c
                    for c in reversed(slot.state.completed_cycles)
                    if str(c.trade_id) == str(exit_order.trade_id)
                    and int(c.cycle) == int(exit_order.cycle)
                ),
                None,
            )

        if cycle_row is not None:
            net_profit = float(cycle_row.net_profit)
        else:
            entry_price = float(pos.get("entry_price", 0.0) or 0.0)
            volume = float(exit_order.volume or 0.0)
            gross = (
                (fill_price - entry_price) * volume
                if exit_order.side == "sell"
                else (entry_price - fill_price) * volume
            )
            net_profit = gross - (float(pos.get("entry_fee", 0.0) or 0.0) + float(fill_fee))

        exit_regime = self._regime_label(self._current_regime_id())
        try:
            self._position_ledger.close_position(
                int(position_id),
                {
                    "exit_price": float(fill_price),
                    "exit_cost": float(fill_cost if fill_cost > 0 else fill_price * float(exit_order.volume)),
                    "exit_fee": float(fill_fee),
                    "exit_time": float(fill_timestamp),
                    "exit_regime": str(exit_regime),
                    "net_profit": float(net_profit),
                    "close_reason": "filled",
                },
            )
            self._persist_position_ledger_row(int(position_id))
            self._persist_position_journal_tail(int(position_id), count=1)
        except Exception as e:
            logger.warning(
                "position_ledger close failed slot=%s local=%s pos=%s: %s",
                int(slot_id),
                int(exit_order.local_id),
                int(position_id),
                e,
            )
            self._unbind_position_for_exit(int(slot_id), int(exit_order.local_id), str(txid or ""))
            return

        # Track cycle mode so throughput can explicitly ignore churner cycles.
        self._cycle_slot_mode[(int(slot_id), str(exit_order.trade_id), int(exit_order.cycle))] = str(
            pos.get("slot_mode") or "legacy"
        )

        # Over-performance credit: favorable fill vs target exit price.
        try:
            target_exit = float(pos.get("current_exit_price", 0.0) or 0.0)
            volume = float(exit_order.volume or 0.0)
            favorable = (exit_order.side == "sell" and fill_price > target_exit) or (
                exit_order.side == "buy" and fill_price < target_exit
            )
            if target_exit > 0 and volume > 0 and favorable:
                excess = abs(float(fill_price) - target_exit) * volume
                entry_price = float(pos.get("entry_price", 0.0) or 0.0)
                entry_fee = float(pos.get("entry_fee", 0.0) or 0.0)
                expected_gross = (
                    (target_exit - entry_price) * volume
                    if exit_order.side == "sell"
                    else (entry_price - target_exit) * volume
                )
                actual_gross = (
                    (fill_price - entry_price) * volume
                    if exit_order.side == "sell"
                    else (entry_price - fill_price) * volume
                )
                expected_profit = expected_gross - (entry_fee + float(fill_fee))
                actual_profit = actual_gross - (entry_fee + float(fill_fee))
                self._position_ledger.journal_event(
                    int(position_id),
                    "over_performance",
                    {
                        "expected_profit": float(expected_profit),
                        "actual_profit": float(actual_profit),
                        "excess": max(0.0, float(excess)),
                    },
                    timestamp=float(fill_timestamp),
                )
                self._persist_position_journal_tail(int(position_id), count=1)
        except Exception as e:
            logger.warning(
                "position_ledger over_performance failed slot=%s local=%s pos=%s: %s",
                int(slot_id),
                int(exit_order.local_id),
                int(position_id),
                e,
            )

        self._unbind_position_for_exit(int(slot_id), int(exit_order.local_id), str(txid or ""))
        self._self_heal_hold_until_by_position.pop(int(position_id), None)
        self._belief_timer_overrides.pop(int(position_id), None)

    def _migrate_open_exits_to_position_ledger(self) -> None:
        if not self._position_ledger_enabled():
            return

        now_ts = _now()
        scanned = 0
        for sid in sorted(self.slots.keys()):
            slot = self.slots[sid]
            scanned += sum(1 for o in slot.state.orders if o.role == "exit")
        self._position_ledger_migration_last_scanned = int(scanned)

        open_positions = self._position_ledger.get_open_positions()
        if bool(self._position_ledger_migration_done):
            if open_positions or int(scanned) == 0:
                bound = self._rebuild_position_bindings_from_open_orders()
                logger.info(
                    "position_ledger startup migration: sentinel complete (scanned=%d bound=%d)",
                    int(scanned),
                    int(bound),
                )
                return
            logger.warning(
                "position_ledger startup migration: sentinel set but no ledger positions with %d live exits; rerunning import",
                int(scanned),
            )
            self._position_ledger_migration_done = False

        if open_positions:
            bound = self._rebuild_position_bindings_from_open_orders()
            self._position_ledger_migration_done = True
            self._position_ledger_migration_last_at = float(now_ts)
            self._position_ledger_migration_last_created = 0
            logger.info(
                "position_ledger startup migration: restored %d open positions (bound %d active exits; scanned=%d)",
                len(open_positions),
                bound,
                int(scanned),
            )
            return

        created = 0
        for sid in sorted(self.slots.keys()):
            slot = self.slots[sid]
            for o in slot.state.orders:
                if o.role != "exit":
                    continue
                entry_ts = float(o.entry_filled_at or o.placed_at or _now())
                try:
                    regime_label = self._regime_label(o.regime_at_entry)
                    position_id = self._position_ledger.open_position(
                        slot_id=int(sid),
                        trade_id=str(o.trade_id),
                        slot_mode="legacy",
                        cycle=int(o.cycle),
                        entry_data={
                            "entry_price": float(o.entry_price),
                            "entry_cost": float(o.entry_price) * float(o.volume),
                            "entry_fee": float(o.entry_fee),
                            "entry_volume": float(o.volume),
                            "entry_time": entry_ts,
                            "entry_regime": regime_label,
                            "entry_volatility": 0.0,
                        },
                        exit_data={
                            "current_exit_price": float(o.price),
                            "original_exit_price": float(o.price),
                            "target_profit_pct": float(self.profit_pct),
                            "exit_txid": str(o.txid or ""),
                        },
                    )
                    self._position_ledger.journal_event(
                        int(position_id),
                        "created",
                        {
                            "entry_price": float(o.entry_price),
                            "exit_price": float(o.price),
                            "regime": regime_label,
                            "slot_mode": "legacy",
                            "migration": True,
                        },
                        timestamp=entry_ts,
                    )
                    self._bind_position_for_exit(int(sid), o, int(position_id))
                    self._persist_position_ledger_row(int(position_id))
                    self._persist_position_journal_tail(int(position_id), count=1)
                    created += 1
                except Exception as e:
                    logger.warning(
                        "position_ledger migration failed slot=%s local=%s trade=%s.%s: %s",
                        int(sid),
                        int(o.local_id),
                        str(o.trade_id),
                        int(o.cycle),
                        e,
                    )
        self._position_ledger_migration_done = True
        self._position_ledger_migration_last_at = float(now_ts)
        self._position_ledger_migration_last_created = int(created)
        if created > 0:
            logger.info(
                "position_ledger startup migration: created %d legacy open positions (scanned=%d)",
                int(created),
                int(scanned),
            )
        else:
            logger.info(
                "position_ledger startup migration: no open exits to import (scanned=%d)",
                int(scanned),
            )

    def _effective_age_seconds(self, age_sec: float, distance_pct: float) -> float:
        weight = max(1e-9, float(getattr(config, "AGE_DISTANCE_WEIGHT", 5.0)))
        return max(0.0, float(age_sec)) * (1.0 + max(0.0, float(distance_pct)) / weight)

    def _age_band_for_effective_age(self, effective_age_sec: float) -> str:
        age = max(0.0, float(effective_age_sec))
        fresh = float(getattr(config, "AGE_BAND_FRESH_SEC", 21600))
        aging = float(getattr(config, "AGE_BAND_AGING_SEC", 86400))
        stale = float(getattr(config, "AGE_BAND_STALE_SEC", 259200))
        stuck = float(getattr(config, "AGE_BAND_STUCK_SEC", 604800))
        if age < fresh:
            return "fresh"
        if age < aging:
            return "aging"
        if age < stale:
            return "stale"
        if age < stuck:
            return "stuck"
        return "write_off"

    def _persist_position_ledger_row(self, position_id: int) -> None:
        row = self._position_ledger.get_position(int(position_id))
        if isinstance(row, dict) and row:
            supabase_store.save_position_ledger(row)

    def _persist_position_journal_tail(self, position_id: int, count: int = 1) -> None:
        if count <= 0:
            return
        rows = self._position_ledger.get_journal(int(position_id))
        if not rows:
            return
        for row in rows[-int(count):]:
            if isinstance(row, dict) and row:
                supabase_store.save_position_journal(row)

    @staticmethod
    def _self_heal_band_rank(band: str) -> int:
        return {
            "fresh": 0,
            "aging": 1,
            "stale": 2,
            "stuck": 3,
            "write_off": 4,
        }.get(str(band or "").strip().lower(), 0)

    def _self_heal_auto_reprice_min_rank(self) -> int:
        raw = str(getattr(config, "SUBSIDY_AUTO_REPRICE_BAND", "stuck") or "stuck").strip().lower()
        if raw not in {"stale", "stuck", "write_off"}:
            raw = "stuck"
        return self._self_heal_band_rank(raw)

    def _entry_pct_for_trade_runtime(self, slot: SlotRuntime, trade_id: str) -> float:
        cfg = self._engine_cfg(slot)
        t = str(trade_id or "").strip().upper()
        if t == "A" and cfg.entry_pct_a is not None and float(cfg.entry_pct_a) > 0:
            return float(cfg.entry_pct_a)
        if t == "B" and cfg.entry_pct_b is not None and float(cfg.entry_pct_b) > 0:
            return float(cfg.entry_pct_b)
        return max(0.0, float(cfg.entry_pct))

    def _compute_fillable_exit_price(
        self,
        *,
        slot: SlotRuntime,
        trade_id: str,
        side: str,
        market: float,
        entry_price: float,
    ) -> float:
        if market <= 0 or entry_price <= 0:
            return 0.0
        entry_pct = self._entry_pct_for_trade_runtime(slot, trade_id) / 100.0
        fee_floor = max(0.0, float(config.ROUND_TRIP_FEE_PCT)) / 100.0
        if str(side or "").lower() == "sell":
            return max(market * (1.0 + entry_pct), entry_price * (1.0 + fee_floor))
        return min(market * (1.0 - entry_pct), entry_price * (1.0 - fee_floor))

    def _subsidy_needed_for_position(
        self,
        position: dict[str, Any],
        *,
        slot: SlotRuntime,
        side: str,
        market: float,
        volume_override: float | None = None,
    ) -> tuple[float, float]:
        current_exit = float(position.get("current_exit_price", 0.0) or 0.0)
        entry_price = float(position.get("entry_price", 0.0) or 0.0)
        volume = max(
            0.0,
            float(
                volume_override
                if volume_override is not None
                else position.get("entry_volume", 0.0) or 0.0
            ),
        )
        if current_exit <= 0 or entry_price <= 0 or volume <= 0 or market <= 0:
            return 0.0, 0.0
        fillable = self._compute_fillable_exit_price(
            slot=slot,
            trade_id=str(position.get("trade_id") or ""),
            side=str(side or ""),
            market=float(market),
            entry_price=float(entry_price),
        )
        if fillable <= 0:
            return 0.0, 0.0

        if str(side or "").lower() == "sell":
            if current_exit <= fillable:
                return 0.0, float(fillable)
            return max(0.0, (current_exit - fillable) * volume), float(fillable)

        if current_exit >= fillable:
            return 0.0, float(fillable)
        return max(0.0, (fillable - current_exit) * volume), float(fillable)

    def _find_live_exit_for_position(
        self,
        position: dict[str, Any],
    ) -> tuple[int, sm.OrderState] | None:
        try:
            slot_id = int(position.get("slot_id", -1))
            position_id = int(position.get("position_id", 0))
        except (TypeError, ValueError):
            return None
        if slot_id < 0 or position_id <= 0:
            return None
        slot = self.slots.get(slot_id)
        if slot is None:
            return None

        for (sid, local_id), pid in self._position_by_exit_local.items():
            if int(sid) != int(slot_id) or int(pid) != int(position_id):
                continue
            order = sm.find_order(slot.state, int(local_id))
            if order and order.role == "exit":
                return int(local_id), order

        txid_norm = str(position.get("exit_txid") or "").strip()
        if txid_norm:
            for order in slot.state.orders:
                if order.role != "exit":
                    continue
                if str(order.txid or "").strip() == txid_norm:
                    self._position_by_exit_local[(int(slot_id), int(order.local_id))] = int(position_id)
                    self._position_by_exit_txid[txid_norm] = int(position_id)
                    return int(order.local_id), order

        trade_id = str(position.get("trade_id") or "")
        cycle = int(position.get("cycle", 0) or 0)
        candidates = [
            o
            for o in slot.state.orders
            if o.role == "exit" and str(o.trade_id) == trade_id and int(o.cycle) == cycle
        ]
        if len(candidates) == 1:
            chosen = candidates[0]
            self._position_by_exit_local[(int(slot_id), int(chosen.local_id))] = int(position_id)
            txid = str(chosen.txid or "").strip()
            if txid:
                self._position_by_exit_txid[txid] = int(position_id)
            return int(chosen.local_id), chosen
        return None

    def _last_position_reprice_ts(self, position_id: int, *, reason: str | None = None) -> float:
        rows = self._position_ledger.get_journal(int(position_id))
        wanted = str(reason or "").strip().lower()
        for row in reversed(rows):
            if str(row.get("event_type") or "") != "repriced":
                continue
            details = row.get("details") if isinstance(row.get("details"), dict) else {}
            if wanted and str(details.get("reason") or "").strip().lower() != wanted:
                continue
            try:
                return float(row.get("timestamp") or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _has_position_reprice_reason(self, position_id: int, reason: str) -> bool:
        return self._last_position_reprice_ts(int(position_id), reason=reason) > 0.0

    def _patch_live_exit_order(
        self,
        *,
        slot_id: int,
        local_id: int,
        price: float,
        txid: str,
        placed_at: float,
    ) -> None:
        slot = self.slots.get(int(slot_id))
        if slot is None:
            return
        patched: list[sm.OrderState] = []
        for order in slot.state.orders:
            if int(order.local_id) == int(local_id) and order.role == "exit":
                patched.append(
                    replace(
                        order,
                        price=float(price),
                        txid=str(txid or ""),
                        placed_at=float(placed_at),
                    )
                )
            else:
                patched.append(order)
        slot.state = replace(slot.state, orders=tuple(patched))

    def _execute_self_heal_reprice(
        self,
        *,
        position_id: int,
        slot_id: int,
        order: sm.OrderState,
        new_price: float,
        reason: str,
        subsidy_consumed: float,
        now_ts: float,
    ) -> tuple[bool, str]:
        pid = int(position_id)
        sid = int(slot_id)
        local_id = int(order.local_id)
        old_price = float(order.price)
        old_txid = str(order.txid or "").strip()
        target_price = max(0.0, float(new_price))
        if old_txid == "":
            return False, "missing_txid"
        if target_price <= 0:
            return False, "invalid_target"
        if abs(target_price - old_price) <= 1e-12:
            return False, "no_fillable_delta"

        try:
            cancelled = self._cancel_order(old_txid)
        except Exception:
            cancelled = False
        if not cancelled:
            return False, "cancel_failed"

        replacement_txid: str | None = None
        try:
            replacement_txid = self._place_order(
                side=order.side,
                volume=float(order.volume),
                price=float(target_price),
                userref=(sid * 1_000_000 + local_id),
            )
        except Exception:
            replacement_txid = None

        if not replacement_txid:
            # Best-effort restore at old price if replacement at new price fails.
            restore_txid: str | None = None
            try:
                restore_txid = self._place_order(
                    side=order.side,
                    volume=float(order.volume),
                    price=float(old_price),
                    userref=(sid * 1_000_000 + local_id),
                )
            except Exception:
                restore_txid = None
            self._position_by_exit_txid.pop(old_txid, None)
            if restore_txid:
                self._patch_live_exit_order(
                    slot_id=sid,
                    local_id=local_id,
                    price=float(old_price),
                    txid=str(restore_txid),
                    placed_at=float(now_ts),
                )
                self._position_by_exit_local[(sid, local_id)] = pid
                self._position_by_exit_txid[str(restore_txid)] = pid
                self._position_ledger.bind_exit_txid(pid, str(restore_txid))
                self._persist_position_ledger_row(pid)
                return False, "place_failed_restored"

            self._patch_live_exit_order(
                slot_id=sid,
                local_id=local_id,
                price=float(old_price),
                txid="",
                placed_at=float(now_ts),
            )
            self.pause(f"self-heal reprice failed: slot={sid} local={local_id} replacement placement failed")
            return False, "place_failed"

        new_txid = str(replacement_txid).strip()
        self._patch_live_exit_order(
            slot_id=sid,
            local_id=local_id,
            price=float(target_price),
            txid=new_txid,
            placed_at=float(now_ts),
        )
        self._position_by_exit_local[(sid, local_id)] = pid
        self._position_by_exit_txid.pop(old_txid, None)
        if new_txid:
            self._position_by_exit_txid[new_txid] = pid

        try:
            self._position_ledger.reprice_position(
                pid,
                new_exit_price=float(target_price),
                new_exit_txid=new_txid,
                reason=str(reason),
                subsidy_consumed=max(0.0, float(subsidy_consumed)),
                timestamp=float(now_ts),
                old_txid_override=old_txid,
            )
            self._persist_position_ledger_row(pid)
            self._persist_position_journal_tail(pid, count=1)
        except Exception as e:
            logger.warning(
                "position_ledger reprice failed slot=%s local=%s position=%s: %s",
                sid,
                local_id,
                pid,
                e,
            )
            return False, "ledger_reprice_failed"
        return True, "repriced"

    def _self_heal_tighten_target_price(
        self,
        *,
        position: dict[str, Any],
        slot: SlotRuntime,
        side: str,
        market: float,
    ) -> tuple[float, float]:
        entry_price = float(position.get("entry_price", 0.0) or 0.0)
        if entry_price <= 0 or market <= 0:
            return 0.0, 0.0
        tighten_profit_pct = max(float(self.profit_pct), float(self._volatility_profit_pct()))
        entry_pct = self._entry_pct_for_trade_runtime(slot, str(position.get("trade_id") or "")) / 100.0
        p = tighten_profit_pct / 100.0
        if str(side or "").lower() == "sell":
            raw = max(entry_price * (1.0 + p), market * (1.0 + entry_pct))
        else:
            raw = min(entry_price * (1.0 - p), market * (1.0 - entry_pct))
        decimals = max(0, int(self.constraints.get("price_decimals", 6)))
        return float(round(raw, decimals)), float(tighten_profit_pct)

    def _self_heal_cleanup_hold_window_sec(self) -> float:
        stale = max(60.0, float(getattr(config, "AGE_BAND_STALE_SEC", 259200)))
        stuck = max(stale + 60.0, float(getattr(config, "AGE_BAND_STUCK_SEC", 604800)))
        return max(3600.0, stuck - stale)

    def _prune_self_heal_hold_overrides(self, now_ts: float | None = None) -> None:
        now = float(now_ts if now_ts is not None else _now())
        for position_id, hold_until in list(self._self_heal_hold_until_by_position.items()):
            if float(hold_until) <= now:
                self._self_heal_hold_until_by_position.pop(int(position_id), None)

    def _self_heal_throughput_hourly_profit_estimate(self) -> float:
        if self._throughput is None:
            return 0.0
        try:
            payload = self._throughput.status_payload()
        except Exception:
            return 0.0
        aggregate = payload.get("aggregate") if isinstance(payload, dict) else {}
        if not isinstance(aggregate, dict):
            return 0.0
        try:
            mean_profit_per_sec = float(aggregate.get("mean_profit_per_sec", 0.0) or 0.0)
        except (TypeError, ValueError):
            mean_profit_per_sec = 0.0
        return max(0.0, mean_profit_per_sec * 3600.0)

    def _self_heal_total_capital_usd(self) -> float:
        mark = float(self.last_price if self.last_price > 0 else 0.0)
        if mark <= 0.0 and self.slots:
            mark = max(0.0, max(float(slot.state.market_price or 0.0) for slot in self.slots.values()))
        if self._last_balance_snapshot and mark > 0.0:
            return max(
                0.0,
                _usd_balance(self._last_balance_snapshot) + _doge_balance(self._last_balance_snapshot) * mark,
            )
        open_positions = self._position_ledger.get_open_positions()
        total_entry_cost = sum(max(0.0, float(pos.get("entry_cost", 0.0) or 0.0)) for pos in open_positions)
        if total_entry_cost > 0.0:
            return float(total_entry_cost)
        return max(0.0, float(getattr(config, "ORDER_SIZE_USD", 0.0)) * max(1, len(self.slots)))

    def _self_heal_opportunity_cost_usd(
        self,
        *,
        age_sec: float,
        entry_cost: float,
        total_capital_usd: float,
        hourly_throughput_usd: float,
    ) -> float:
        if age_sec <= 0.0 or entry_cost <= 0.0 or total_capital_usd <= 0.0 or hourly_throughput_usd <= 0.0:
            return 0.0
        return max(
            0.0,
            (float(age_sec) / 3600.0)
            * float(hourly_throughput_usd)
            * (float(entry_cost) / float(total_capital_usd)),
        )

    def _self_heal_cleanup_queue_rows(
        self,
        now_ts: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        now = float(now_ts if now_ts is not None else _now())
        self._prune_self_heal_hold_overrides(now)

        rows: list[dict[str, Any]] = []
        hidden_by_hold = 0
        open_positions = self._position_ledger.get_open_positions()
        total_capital_usd = self._self_heal_total_capital_usd()
        hourly_throughput_usd = self._self_heal_throughput_hourly_profit_estimate()

        for position in open_positions:
            try:
                position_id = int(position.get("position_id", 0) or 0)
                slot_id = int(position.get("slot_id", -1))
            except (TypeError, ValueError):
                continue
            if position_id <= 0 or slot_id < 0:
                continue
            slot = self.slots.get(slot_id)
            if slot is None:
                continue
            live = self._find_live_exit_for_position(position)
            if live is None:
                continue
            _local_id, order = live

            market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
            if market <= 0.0:
                continue

            exit_px = float(position.get("current_exit_price", order.price) or order.price)
            entry_px = float(position.get("entry_price", 0.0) or 0.0)
            entry_cost = max(0.0, float(position.get("entry_cost", 0.0) or 0.0))
            age_sec = max(0.0, now - float(position.get("entry_time", now) or now))
            distance_pct = abs(exit_px - market) / market * 100.0 if market > 0 else 0.0
            effective_age_sec = self._effective_age_seconds(age_sec, distance_pct)
            band = self._age_band_for_effective_age(effective_age_sec)
            if band != "write_off":
                continue

            hold_until = float(self._self_heal_hold_until_by_position.get(position_id, 0.0) or 0.0)
            hold_remaining_sec = max(0.0, hold_until - now)
            if hold_remaining_sec > 0.0:
                hidden_by_hold += 1
                continue

            subsidy_balance = max(0.0, float(self._position_ledger.get_subsidy_balance(slot_id)))
            subsidy_needed, fillable_price = self._subsidy_needed_for_position(
                position,
                slot=slot,
                side=order.side,
                market=market,
                volume_override=float(order.volume),
            )
            opportunity_cost_usd = self._self_heal_opportunity_cost_usd(
                age_sec=age_sec,
                entry_cost=entry_cost,
                total_capital_usd=total_capital_usd,
                hourly_throughput_usd=hourly_throughput_usd,
            )
            rows.append(
                {
                    "position_id": position_id,
                    "slot_id": slot_id,
                    "trade_id": str(position.get("trade_id") or ""),
                    "cycle": int(position.get("cycle", 0) or 0),
                    "side": str(order.side or ""),
                    "entry_time": float(position.get("entry_time", 0.0) or 0.0),
                    "entry_price": float(entry_px),
                    "entry_volume": max(0.0, float(order.volume)),
                    "entry_cost": float(entry_cost),
                    "current_exit_price": float(exit_px),
                    "market_price": float(market),
                    "age_sec": float(age_sec),
                    "distance_pct": float(distance_pct),
                    "effective_age_sec": float(effective_age_sec),
                    "age_band": str(band),
                    "fillable_price": float(fillable_price),
                    "subsidy_balance": float(subsidy_balance),
                    "subsidy_needed": float(max(0.0, subsidy_needed)),
                    "opportunity_cost_usd": float(opportunity_cost_usd),
                    "hold_remaining_sec": float(hold_remaining_sec),
                    "target_profit_pct": float(position.get("target_profit_pct", self.profit_pct) or self.profit_pct),
                }
            )

        rows.sort(
            key=lambda row: (
                -float(row.get("effective_age_sec", 0.0) or 0.0),
                -float(row.get("age_sec", 0.0) or 0.0),
                -int(row.get("position_id", 0) or 0),
            )
        )
        return rows, int(hidden_by_hold)

    def _self_heal_operator_journal(
        self,
        *,
        position_id: int,
        event_type: str,
        details: dict[str, Any],
        now_ts: float | None = None,
    ) -> None:
        pid = int(position_id)
        if pid <= 0:
            return
        try:
            self._position_ledger.journal_event(
                pid,
                str(event_type or "operator_action"),
                dict(details or {}),
                timestamp=float(now_ts if now_ts is not None else _now()),
            )
            self._persist_position_journal_tail(pid, count=1)
        except Exception as e:
            logger.warning("self-heal operator journal failed position=%s event=%s: %s", pid, event_type, e)

    def self_heal_reprice_breakeven(
        self,
        position_id: int,
        *,
        operator_reason: str = "",
    ) -> tuple[bool, str]:
        if not self._position_ledger_enabled():
            return False, "self-healing ledger is disabled"

        pid = int(position_id)
        if pid <= 0:
            return False, "invalid position_id"

        position = self._position_ledger.get_position(pid)
        if not isinstance(position, dict) or str(position.get("status") or "") != "open":
            return False, f"position {pid} is not open"

        slot_id = int(position.get("slot_id", -1))
        slot = self.slots.get(slot_id)
        if slot is None:
            return False, f"slot {slot_id} not found"

        live = self._find_live_exit_for_position(position)
        if live is None:
            return False, f"position {pid} has no live exit order"
        _local_id, order = live

        entry_price = float(position.get("entry_price", 0.0) or 0.0)
        volume = max(0.0, float(order.volume))
        current_exit = float(position.get("current_exit_price", order.price) or order.price)
        if entry_price <= 0.0 or volume <= 0.0 or current_exit <= 0.0:
            return False, "position has invalid price/volume context"

        fee_floor = max(0.0, float(getattr(config, "ROUND_TRIP_FEE_PCT", 0.0))) / 100.0
        breakeven_price = (
            entry_price * (1.0 + fee_floor)
            if str(order.side or "").lower() == "sell"
            else entry_price * (1.0 - fee_floor)
        )
        decimals = max(0, int(self.constraints.get("price_decimals", 6)))
        price_scale = float(10 ** decimals) if decimals >= 0 else 1.0
        tick = 1.0 / price_scale if price_scale > 0 else 0.0

        if order.side == "sell" and current_exit <= (breakeven_price + tick):
            return False, "exit is already at or below breakeven"
        if order.side == "buy" and current_exit >= (breakeven_price - tick):
            return False, "exit is already at or above breakeven"

        subsidy_balance = max(0.0, float(self._position_ledger.get_subsidy_balance(slot_id)))
        if subsidy_balance <= 1e-12:
            return False, "no subsidy balance available"

        subsidy_needed = abs(current_exit - breakeven_price) * volume
        affordable = min(subsidy_needed, subsidy_balance)
        if affordable <= 1e-12:
            return False, "no subsidy balance available"

        delta_px = affordable / volume
        partial = affordable + 1e-12 < subsidy_needed
        if order.side == "sell":
            raw_target = max(breakeven_price, current_exit - delta_px)
            if partial:
                raw_target = max(breakeven_price, ceil(raw_target * price_scale) / price_scale)
        else:
            raw_target = min(breakeven_price, current_exit + delta_px)
            if partial:
                raw_target = min(breakeven_price, floor(raw_target * price_scale) / price_scale)

        target_price = float(round(raw_target, decimals))
        if order.side == "sell":
            target_price = max(breakeven_price, target_price)
        else:
            target_price = min(breakeven_price, target_price)

        subsidy_consumed = max(0.0, abs(current_exit - target_price) * volume)
        if subsidy_consumed <= 1e-12:
            return False, "no reprice delta"
        if subsidy_consumed > subsidy_balance + 1e-9:
            return False, "insufficient subsidy balance"

        now = _now()
        ok, reason = self._execute_self_heal_reprice(
            position_id=pid,
            slot_id=slot_id,
            order=order,
            new_price=target_price,
            reason="operator",
            subsidy_consumed=subsidy_consumed,
            now_ts=now,
        )
        if not ok:
            return False, f"reprice failed: {reason}"

        self._self_heal_hold_until_by_position.pop(pid, None)
        self._self_heal_operator_journal(
            position_id=pid,
            event_type="operator_action",
            details={
                "action": "reprice_breakeven",
                "operator_reason": str(operator_reason or "manual"),
                "breakeven_price": float(round(breakeven_price, decimals)),
                "new_exit_price": float(target_price),
                "subsidy_consumed": float(subsidy_consumed),
                "partial": bool(partial),
            },
            now_ts=now,
        )

        if partial:
            return (
                True,
                (
                    f"position {pid}: partial breakeven reprice to ${target_price:.6f} "
                    f"(used ${subsidy_consumed:.4f}/${subsidy_needed:.4f})"
                ),
            )
        return True, f"position {pid}: repriced to breakeven @ ${target_price:.6f}"

    def self_heal_keep_holding(
        self,
        position_id: int,
        *,
        operator_reason: str = "",
        hold_sec: float | None = None,
    ) -> tuple[bool, str]:
        if not self._position_ledger_enabled():
            return False, "self-healing ledger is disabled"

        pid = int(position_id)
        if pid <= 0:
            return False, "invalid position_id"

        position = self._position_ledger.get_position(pid)
        if not isinstance(position, dict) or str(position.get("status") or "") != "open":
            return False, f"position {pid} is not open"

        default_hold = self._self_heal_cleanup_hold_window_sec()
        hold_window_sec = float(hold_sec if hold_sec is not None else default_hold)
        hold_window_sec = max(60.0, min(30.0 * 86400.0, hold_window_sec))
        now = _now()
        hold_until = now + hold_window_sec
        self._self_heal_hold_until_by_position[pid] = float(hold_until)
        self._self_heal_operator_journal(
            position_id=pid,
            event_type="keep_holding",
            details={
                "action": "keep_holding",
                "operator_reason": str(operator_reason or "manual"),
                "hold_sec": float(hold_window_sec),
                "hold_until": float(hold_until),
            },
            now_ts=now,
        )
        return True, f"position {pid}: cleanup timer reset for {hold_window_sec / 3600.0:.1f}h"

    def self_heal_close_at_market(
        self,
        position_id: int,
        *,
        operator_reason: str = "",
    ) -> tuple[bool, str]:
        if not self._position_ledger_enabled():
            return False, "self-healing ledger is disabled"

        pid = int(position_id)
        if pid <= 0:
            return False, "invalid position_id"

        position = self._position_ledger.get_position(pid)
        if not isinstance(position, dict) or str(position.get("status") or "") != "open":
            return False, f"position {pid} is not open"

        slot_id = int(position.get("slot_id", -1))
        slot = self.slots.get(slot_id)
        if slot is None:
            return False, f"slot {slot_id} not found"

        live = self._find_live_exit_for_position(position)
        if live is None:
            return False, f"position {pid} has no live exit order"
        local_id, order = live

        old_txid = str(order.txid or "").strip()
        if not old_txid:
            return False, "position exit has no txid"

        old_price = float(order.price)
        now = _now()
        try:
            cancelled = self._cancel_order(old_txid)
        except Exception:
            cancelled = False
        if not cancelled:
            return False, "failed to cancel current exit"

        market_txid: str | None = None
        try:
            market_txid = self._place_market_order(
                side=str(order.side),
                volume=float(order.volume),
                userref=(int(slot_id) * 1_000_000 + 970_000 + int(local_id)),
            )
        except Exception:
            market_txid = None

        if not market_txid:
            restore_txid: str | None = None
            try:
                restore_txid = self._place_order(
                    side=str(order.side),
                    volume=float(order.volume),
                    price=float(old_price),
                    userref=(int(slot_id) * 1_000_000 + 971_000 + int(local_id)),
                )
            except Exception:
                restore_txid = None
            self._position_by_exit_txid.pop(old_txid, None)
            if restore_txid:
                restore_norm = str(restore_txid).strip()
                self._patch_live_exit_order(
                    slot_id=int(slot_id),
                    local_id=int(local_id),
                    price=float(old_price),
                    txid=restore_norm,
                    placed_at=float(now),
                )
                self._position_by_exit_local[(int(slot_id), int(local_id))] = int(pid)
                if restore_norm:
                    self._position_by_exit_txid[restore_norm] = int(pid)
                    self._position_ledger.bind_exit_txid(int(pid), restore_norm)
                self._persist_position_ledger_row(int(pid))
                return False, "market close failed; restored previous exit order"

            self._patch_live_exit_order(
                slot_id=int(slot_id),
                local_id=int(local_id),
                price=float(old_price),
                txid="",
                placed_at=float(now),
            )
            self.pause(
                f"self-heal write-off failed: slot={slot_id} local={local_id} market close placement failed"
            )
            return False, "market close failed and restore failed; bot paused"

        market_txid_norm = str(market_txid).strip()
        volume = max(0.0, float(order.volume))
        if volume <= 0.0:
            return False, "invalid position volume"

        fill_price = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        if fill_price <= 0.0:
            fill_price = max(
                0.0,
                float(position.get("current_exit_price", 0.0) or 0.0),
                float(position.get("entry_price", 0.0) or 0.0),
                float(old_price),
            )
        if fill_price <= 0.0:
            return False, "market price unavailable"

        fill_fee = max(0.0, fill_price * volume * (max(0.0, float(self.taker_fee_pct)) / 100.0))
        fill_cost = fill_price * volume

        try:
            self._apply_event(
                int(slot_id),
                sm.FillEvent(
                    order_local_id=int(local_id),
                    txid=market_txid_norm,
                    side=str(order.side),
                    price=float(fill_price),
                    volume=float(volume),
                    fee=float(fill_fee),
                    timestamp=float(now),
                ),
                "self_heal_write_off",
                {
                    "position_id": int(pid),
                    "txid": market_txid_norm,
                    "fill_price": float(fill_price),
                    "fill_fee": float(fill_fee),
                    "reason": str(operator_reason or "manual"),
                },
            )
        except Exception as e:
            logger.exception("self-heal write-off transition failed position=%s: %s", pid, e)
            self.pause(f"self-heal write-off transition failed for position {pid}")
            return False, f"market close placed but local transition failed: {e}"

        slot_after = self.slots.get(int(slot_id))
        cycle_row = None
        if slot_after is not None:
            cycle_row = next(
                (
                    c
                    for c in reversed(slot_after.state.completed_cycles)
                    if str(c.trade_id) == str(order.trade_id)
                    and int(c.cycle) == int(order.cycle)
                ),
                None,
            )
        if cycle_row is not None:
            net_profit = float(cycle_row.net_profit)
        else:
            entry_price = float(position.get("entry_price", 0.0) or 0.0)
            entry_fee = float(position.get("entry_fee", 0.0) or 0.0)
            gross = (fill_price - entry_price) * volume if order.side == "sell" else (entry_price - fill_price) * volume
            net_profit = gross - (entry_fee + float(fill_fee))

        try:
            self._position_ledger.close_position(
                int(pid),
                {
                    "exit_price": float(fill_price),
                    "exit_cost": float(fill_cost),
                    "exit_fee": float(fill_fee),
                    "exit_time": float(now),
                    "exit_regime": str(self._regime_label(self._current_regime_id())),
                    "net_profit": float(net_profit),
                    "close_reason": "write_off",
                    "reason": str(operator_reason or "manual"),
                },
            )
            self._persist_position_ledger_row(int(pid))
            self._persist_position_journal_tail(int(pid), count=1)
        except Exception as e:
            logger.warning(
                "self-heal write-off ledger close failed slot=%s local=%s position=%s: %s",
                int(slot_id),
                int(local_id),
                int(pid),
                e,
            )
            return False, f"market close executed but ledger close failed: {e}"

        self._cycle_slot_mode[(int(slot_id), str(order.trade_id), int(order.cycle))] = str(
            position.get("slot_mode") or "legacy"
        )
        self._self_heal_operator_journal(
            position_id=int(pid),
            event_type="operator_action",
            details={
                "action": "close_market_write_off",
                "operator_reason": str(operator_reason or "manual"),
                "txid": market_txid_norm,
                "fill_price": float(fill_price),
                "fill_fee": float(fill_fee),
                "net_profit": float(net_profit),
            },
            now_ts=now,
        )

        self._self_heal_hold_until_by_position.pop(int(pid), None)
        self._unbind_position_for_exit(int(slot_id), int(local_id), str(old_txid))
        self.seen_fill_txids.add(market_txid_norm)
        return True, f"position {pid}: closed at market @ ${fill_price:.6f}"

    def _run_self_healing_reprice(self, now_ts: float | None = None) -> None:
        now = float(now_ts if now_ts is not None else _now())
        if not self._position_ledger_enabled():
            return
        if not self._flag_value("SUBSIDY_ENABLED"):
            return

        open_positions = self._position_ledger.get_open_positions()
        summary: dict[str, Any] = {
            "timestamp": float(now),
            "checked": 0,
            "repriced": 0,
            "tighten": 0,
            "subsidy": 0,
            "skipped": {},
        }

        def _skip(reason: str) -> None:
            key = str(reason or "unknown")
            skipped = summary["skipped"]
            skipped[key] = int(skipped.get(key, 0)) + 1

        min_subsidy_rank = self._self_heal_auto_reprice_min_rank()
        cooldown_sec = max(0.0, float(getattr(config, "SUBSIDY_REPRICE_INTERVAL_SEC", 3600)))
        decimals = max(0, int(self.constraints.get("price_decimals", 6)))
        price_scale = float(10 ** decimals) if decimals >= 0 else 1.0

        for position in open_positions:
            summary["checked"] = int(summary["checked"]) + 1
            try:
                slot_id = int(position.get("slot_id", -1))
                position_id = int(position.get("position_id", 0))
            except (TypeError, ValueError):
                _skip("invalid_position")
                continue
            if slot_id < 0 or position_id <= 0:
                _skip("invalid_position")
                continue
            slot = self.slots.get(slot_id)
            if slot is None:
                _skip("slot_missing")
                continue

            live = self._find_live_exit_for_position(position)
            if live is None:
                _skip("live_exit_missing")
                continue
            local_id, order = live
            del local_id  # local_id is represented in `order`; keep intent explicit.

            market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
            if market <= 0:
                _skip("market_unavailable")
                continue

            exit_px = float(position.get("current_exit_price", 0.0) or 0.0)
            age = max(0.0, now - float(position.get("entry_time", now) or now))
            distance = abs(exit_px - market) / market * 100.0 if market > 0 else 0.0
            band = self._age_band_for_effective_age(self._effective_age_seconds(age, distance))
            band_rank = self._self_heal_band_rank(band)

            # Stale-band tighten: one-time, no subsidy debit.
            if band == "stale":
                if not self._has_position_reprice_reason(position_id, "tighten"):
                    target_pct = float(position.get("target_profit_pct", self.profit_pct) or self.profit_pct)
                    tighten_price, tighten_target_pct = self._self_heal_tighten_target_price(
                        position=position,
                        slot=slot,
                        side=order.side,
                        market=market,
                    )
                    if tighten_price > 0 and target_pct > (tighten_target_pct + 0.1):
                        old_price = float(position.get("current_exit_price", order.price) or order.price)
                        tick = 1.0 / price_scale if price_scale > 0 else 0.0
                        if (
                            (order.side == "sell" and old_price - tighten_price >= tick)
                            or (order.side == "buy" and tighten_price - old_price >= tick)
                        ):
                            ok, reason = self._execute_self_heal_reprice(
                                position_id=position_id,
                                slot_id=slot_id,
                                order=order,
                                new_price=float(tighten_price),
                                reason="tighten",
                                subsidy_consumed=0.0,
                                now_ts=now,
                            )
                            if ok:
                                summary["repriced"] = int(summary["repriced"]) + 1
                                summary["tighten"] = int(summary["tighten"]) + 1
                                continue
                            _skip(reason)
                        else:
                            _skip("no_fillable_delta")
                    else:
                        _skip("tighten_not_needed")
                else:
                    _skip("tighten_done")

            if band_rank < min_subsidy_rank:
                _skip("band_not_eligible")
                continue

            needed, fillable = self._subsidy_needed_for_position(
                position,
                slot=slot,
                side=order.side,
                market=market,
                volume_override=float(order.volume),
            )
            if needed <= 1e-12 or fillable <= 0.0:
                _skip("no_fillable_delta")
                continue

            last_subsidy_ts = self._last_position_reprice_ts(position_id, reason="subsidy")
            if last_subsidy_ts > 0 and cooldown_sec > 0 and (now - last_subsidy_ts) < cooldown_sec:
                _skip("cooldown")
                continue

            subsidy_balance = max(0.0, float(self._position_ledger.get_subsidy_balance(slot_id)))
            if subsidy_balance <= 1e-12:
                _skip("insufficient_balance")
                continue

            current_exit = float(position.get("current_exit_price", order.price) or order.price)
            volume = max(0.0, float(order.volume))
            if volume <= 0.0:
                _skip("invalid_volume")
                continue

            affordable = min(needed, subsidy_balance)
            delta_px = affordable / volume
            if delta_px <= 0:
                _skip("insufficient_balance")
                continue

            if order.side == "sell":
                raw_target = max(fillable, current_exit - delta_px)
                if affordable + 1e-12 < needed:
                    raw_target = max(fillable, ceil(raw_target * price_scale) / price_scale)
            else:
                raw_target = min(fillable, current_exit + delta_px)
                if affordable + 1e-12 < needed:
                    raw_target = min(fillable, floor(raw_target * price_scale) / price_scale)

            target_price = float(round(raw_target, decimals))
            if order.side == "sell":
                target_price = max(fillable, target_price)
            else:
                target_price = min(fillable, target_price)

            subsidy_consumed = max(0.0, abs(current_exit - target_price) * volume)
            if subsidy_consumed <= 1e-12:
                _skip("no_fillable_delta")
                continue
            if subsidy_consumed > subsidy_balance + 1e-9:
                _skip("insufficient_balance")
                continue

            ok, reason = self._execute_self_heal_reprice(
                position_id=position_id,
                slot_id=slot_id,
                order=order,
                new_price=target_price,
                reason="subsidy",
                subsidy_consumed=subsidy_consumed,
                now_ts=now,
            )
            if ok:
                summary["repriced"] = int(summary["repriced"]) + 1
                summary["subsidy"] = int(summary["subsidy"]) + 1
            else:
                _skip(reason)

        if int(summary["repriced"]) > 0:
            self._self_heal_reprice_total += int(summary["repriced"])
            self._self_heal_reprice_last_at = float(now)
        self._self_heal_reprice_last_summary = summary

    def _churner_enabled(self) -> bool:
        return self._flag_value("CHURNER_ENABLED") and self._position_ledger_enabled()

    def _ensure_churner_state(self, slot_id: int) -> ChurnerRuntimeState:
        sid = int(slot_id)
        state = self._churner_by_slot.get(sid)
        if state is None:
            base_usd = max(
                0.0,
                float(getattr(config, "CHURNER_ORDER_SIZE_USD", getattr(config, "ORDER_SIZE_USD", 0.0))),
            )
            state = ChurnerRuntimeState(order_size_usd=base_usd)
            self._churner_by_slot[sid] = state
        return state

    def _reconcile_churner_state(self) -> None:
        valid_slots = {int(sid) for sid in self.slots.keys()}
        for sid in list(self._churner_by_slot.keys()):
            if int(sid) in valid_slots:
                continue
            state = self._churner_by_slot.pop(int(sid), None)
            if state is not None:
                self._churner_release_reserve(state)

        max_reserve = max(0.0, float(getattr(config, "CHURNER_RESERVE_USD", 0.0)))
        allocated = sum(max(0.0, float(state.reserve_allocated_usd)) for state in self._churner_by_slot.values())
        self._churner_reserve_available_usd = max(0.0, min(max_reserve, max_reserve - allocated))

    def _churner_release_reserve(self, state: ChurnerRuntimeState) -> None:
        if float(state.reserve_allocated_usd) <= 0.0:
            return
        max_reserve = max(0.0, float(getattr(config, "CHURNER_RESERVE_USD", 0.0)))
        self._churner_reserve_available_usd = min(
            max_reserve,
            float(self._churner_reserve_available_usd) + float(state.reserve_allocated_usd),
        )
        state.reserve_allocated_usd = 0.0

    def _churner_reset_state(
        self,
        state: ChurnerRuntimeState,
        *,
        now_ts: float,
        reason: str = "",
        keep_compound: bool = True,
        keep_active: bool = False,
    ) -> None:
        self._churner_release_reserve(state)
        compound = float(state.compound_usd) if keep_compound else 0.0
        base_usd = max(
            0.0,
            float(getattr(config, "CHURNER_ORDER_SIZE_USD", getattr(config, "ORDER_SIZE_USD", 0.0))),
        )
        state.active = bool(keep_active)
        state.stage = "idle"
        state.parent_position_id = 0
        state.parent_trade_id = ""
        state.cycle_id = 0
        state.order_size_usd = max(base_usd, base_usd + compound)
        state.compound_usd = max(0.0, compound)
        state.entry_side = ""
        state.entry_txid = ""
        state.entry_price = 0.0
        state.entry_volume = 0.0
        state.entry_placed_at = 0.0
        state.entry_fill_price = 0.0
        state.entry_fill_fee = 0.0
        state.entry_fill_time = 0.0
        state.exit_txid = ""
        state.exit_price = 0.0
        state.exit_placed_at = 0.0
        state.churner_position_id = 0
        state.last_error = str(reason or "")
        state.last_state_change_at = float(now_ts)

    @staticmethod
    def _churner_entry_side_for_trade(trade_id: str) -> str:
        return "buy" if str(trade_id or "").strip().upper() == "B" else "sell"

    def _churner_entry_target_price(self, *, side: str, market: float) -> float:
        if market <= 0:
            return 0.0
        pct = max(0.0, float(getattr(config, "CHURNER_ENTRY_PCT", 0.15))) / 100.0
        if str(side).lower() == "buy":
            raw = market * (1.0 - pct)
        else:
            raw = market * (1.0 + pct)
        return round(raw, max(0, int(self.constraints.get("price_decimals", 6))))

    def _churner_exit_target_price(self, *, entry_side: str, entry_fill_price: float, market: float) -> float:
        if entry_fill_price <= 0:
            return 0.0
        profit_pct = max(0.0, float(getattr(config, "CHURNER_PROFIT_PCT", 0.0))) / 100.0
        entry_pct = max(0.0, float(getattr(config, "CHURNER_ENTRY_PCT", 0.15))) / 100.0
        if str(entry_side).lower() == "buy":
            raw = entry_fill_price * (1.0 + profit_pct)
            if market > 0:
                raw = max(raw, market * (1.0 + entry_pct))
        else:
            raw = entry_fill_price * (1.0 - profit_pct)
            if market > 0:
                raw = min(raw, market * (1.0 - entry_pct))
        return round(raw, max(0, int(self.constraints.get("price_decimals", 6))))

    def _churner_candidate_parent_position(
        self,
        slot_id: int,
        *,
        now_ts: float,
    ) -> tuple[dict[str, Any], sm.OrderState, float, str] | None:
        slot = self.slots.get(int(slot_id))
        if slot is None:
            return None
        market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        if market <= 0:
            return None

        best: tuple[dict[str, Any], sm.OrderState, float, str] | None = None
        best_key: tuple[float, float, int] | None = None
        aging_rank = self._self_heal_band_rank("aging")
        for pos in self._position_ledger.get_open_positions(slot_id=int(slot_id)):
            if str(pos.get("slot_mode") or "") == "churner":
                continue
            live = self._find_live_exit_for_position(pos)
            if live is None:
                continue
            _local_id, order = live
            age = max(0.0, float(now_ts) - float(pos.get("entry_time", now_ts) or now_ts))
            distance = abs(float(pos.get("current_exit_price", order.price) or order.price) - market) / market * 100.0
            effective_age = self._effective_age_seconds(age, distance)
            band = self._age_band_for_effective_age(effective_age)
            if self._self_heal_band_rank(band) < aging_rank:
                continue
            key = (float(effective_age), float(age), int(pos.get("position_id", 0) or 0))
            if best_key is None or key > best_key:
                best_key = key
                best = (pos, order, effective_age, band)
        return best

    def _churner_active_slot_count(self) -> int:
        return sum(1 for state in self._churner_by_slot.values() if bool(state.active))

    def _churner_mts_gate_values(self) -> tuple[float, float]:
        gate = max(0.0, min(1.0, float(getattr(config, "MTS_CHURNER_GATE", 0.3))))
        mts_value = 0.0
        if self._flag_value("MTS_ENABLED") and bool(getattr(self._manifold_score, "enabled", False)):
            mts_value = max(0.0, min(1.0, float(getattr(self._manifold_score, "mts", 0.0) or 0.0)))
        return float(mts_value), float(gate)

    def _churner_spawn_parent_candidate(
        self,
        *,
        slot_id: int,
        now_ts: float,
        position_id: int | None = None,
    ) -> tuple[tuple[dict[str, Any], sm.OrderState, float, str] | None, str]:
        sid = int(slot_id)
        pid = int(position_id or 0)
        if pid <= 0:
            candidate = self._churner_candidate_parent_position(sid, now_ts=float(now_ts))
            if candidate is None:
                return None, "no_candidate"
            return candidate, ""

        slot = self.slots.get(sid)
        if slot is None:
            return None, "slot_missing"
        market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        if market <= 0:
            return None, "market_unavailable"

        aging_rank = self._self_heal_band_rank("aging")
        for pos in self._position_ledger.get_open_positions(slot_id=sid):
            if int(pos.get("position_id", 0) or 0) != pid:
                continue
            if str(pos.get("slot_mode") or "") == "churner":
                return None, "position_is_churner"
            live = self._find_live_exit_for_position(pos)
            if live is None:
                return None, "parent_exit_missing"
            _local_id, order = live
            age = max(0.0, float(now_ts) - float(pos.get("entry_time", now_ts) or now_ts))
            distance = abs(float(pos.get("current_exit_price", order.price) or order.price) - market) / market * 100.0
            effective_age = self._effective_age_seconds(age, distance)
            band = self._age_band_for_effective_age(effective_age)
            if self._self_heal_band_rank(band) < aging_rank:
                return None, "position_not_aging"
            return (pos, order, effective_age, band), ""

        return None, "position_not_found"

    def _churner_status_payload(self, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else _now())
        self._reconcile_churner_state()
        enabled = bool(self._churner_enabled())
        mts_value, mts_gate = self._churner_mts_gate_values()
        max_active = max(1, int(getattr(config, "CHURNER_MAX_ACTIVE", 5)))
        active_slots = int(self._churner_active_slot_count())
        subsidy_balance = 0.0
        subsidy_needed = 0.0
        if self._position_ledger_enabled():
            totals = self._position_ledger.get_subsidy_totals()
            subsidy_balance = float(totals.get("balance", 0.0) or 0.0)
            try:
                self_heal = self._self_healing_status_payload(now)
                subsidy_needed = float((self_heal.get("subsidy") or {}).get("pending_needed", 0.0) or 0.0)
            except Exception:
                subsidy_needed = 0.0

        states: list[dict[str, Any]] = []
        for sid in sorted(self.slots.keys()):
            state = self._churner_by_slot.get(int(sid))
            if state is None:
                states.append(
                    {
                        "slot_id": int(sid),
                        "active": False,
                        "stage": "idle",
                        "parent_position_id": 0,
                        "parent_trade_id": "",
                        "cycle_id": 0,
                        "entry_txid": "",
                        "exit_txid": "",
                        "last_error": "",
                        "last_state_change_at": 0.0,
                    }
                )
                continue
            states.append(
                {
                    "slot_id": int(sid),
                    "active": bool(state.active),
                    "stage": str(state.stage or "idle"),
                    "parent_position_id": int(state.parent_position_id or 0),
                    "parent_trade_id": str(state.parent_trade_id or ""),
                    "cycle_id": int(state.cycle_id or 0),
                    "entry_txid": str(state.entry_txid or ""),
                    "exit_txid": str(state.exit_txid or ""),
                    "last_error": str(state.last_error or ""),
                    "last_state_change_at": float(state.last_state_change_at or 0.0),
                }
            )

        return {
            "enabled": enabled,
            "active_slots": active_slots,
            "max_active": int(max_active),
            "reserve_available_usd": float(self._churner_reserve_available_usd),
            "reserve_config_usd": float(getattr(config, "CHURNER_RESERVE_USD", 0.0)),
            "cycles_today": int(self._churner_cycles_today),
            "profit_today": float(self._churner_profit_today),
            "cycles_total": int(self._churner_cycles_total),
            "profit_total": float(self._churner_profit_total),
            "mts": float(mts_value),
            "mts_gate": float(mts_gate),
            "subsidy_balance": float(subsidy_balance),
            "subsidy_needed": float(subsidy_needed),
            "states": states,
        }

    def _churner_candidates_payload(self, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else _now())
        self._reconcile_churner_state()
        rows: list[dict[str, Any]] = []
        if not self._position_ledger_enabled():
            return {
                "enabled": False,
                "count": 0,
                "candidates": rows,
            }

        for sid in sorted(self.slots.keys()):
            candidate = self._churner_candidate_parent_position(int(sid), now_ts=now)
            if candidate is None:
                continue
            parent, order, effective_age, band = candidate
            slot = self.slots.get(int(sid))
            if slot is None:
                continue
            market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
            if market <= 0:
                continue
            exit_px = float(parent.get("current_exit_price", order.price) or order.price)
            distance_pct = abs(exit_px - market) / market * 100.0
            subsidy_needed, fillable_price = self._subsidy_needed_for_position(
                parent,
                slot=slot,
                side=order.side,
                market=market,
                volume_override=float(order.volume),
            )
            rows.append(
                {
                    "slot_id": int(sid),
                    "position_id": int(parent.get("position_id", 0) or 0),
                    "trade_id": str(parent.get("trade_id") or order.trade_id),
                    "cycle": int(parent.get("cycle", 0) or 0),
                    "side": str(order.side or ""),
                    "current_exit_price": float(exit_px),
                    "market_price": float(market),
                    "distance_pct": float(distance_pct),
                    "effective_age_sec": float(effective_age),
                    "age_band": str(band),
                    "subsidy_needed": float(max(0.0, subsidy_needed)),
                    "fillable_price": float(fillable_price),
                    "active": bool(getattr(self._churner_by_slot.get(int(sid)), "active", False)),
                }
            )
        return {
            "enabled": bool(self._churner_enabled()),
            "count": int(len(rows)),
            "candidates": rows,
        }

    def _churner_spawn(
        self,
        *,
        slot_id: int,
        position_id: int | None = None,
        now_ts: float | None = None,
    ) -> tuple[bool, str]:
        now = float(now_ts if now_ts is not None else _now())
        sid = int(slot_id)
        if sid not in self.slots:
            return False, "slot_missing"
        if not self._churner_enabled():
            return False, "churner_disabled"
        self._reconcile_churner_state()
        state = self._ensure_churner_state(sid)
        if bool(state.active):
            return False, "slot_already_active"

        max_active = max(1, int(getattr(config, "CHURNER_MAX_ACTIVE", 5)))
        if int(self._churner_active_slot_count()) >= max_active:
            return False, f"max_active_reached ({max_active})"

        mts_value, mts_gate = self._churner_mts_gate_values()
        if self._flag_value("MTS_ENABLED") and mts_value + 1e-12 < mts_gate:
            return False, f"mts_below_gate ({mts_value:.3f} < {mts_gate:.3f})"

        regime_name, _conf, _bias, _ready, _source = self._policy_hmm_signal()
        if str(regime_name or "").strip().upper() != "RANGING":
            return False, "regime_not_ranging"

        capacity = self._compute_capacity_health(now)
        headroom = int(capacity.get("open_order_headroom") or 0)
        min_headroom = max(0, int(getattr(config, "CHURNER_MIN_HEADROOM", 0)))
        if headroom < min_headroom:
            return False, "headroom_low"

        candidate, reason = self._churner_spawn_parent_candidate(
            slot_id=sid,
            now_ts=now,
            position_id=position_id,
        )
        if candidate is None:
            return False, str(reason or "no_candidate")
        parent, parent_order, _effective_age, _band = candidate

        state.active = True
        state.stage = "idle"
        state.parent_position_id = int(parent.get("position_id", 0) or 0)
        state.parent_trade_id = str(parent.get("trade_id") or parent_order.trade_id)
        state.last_error = ""
        state.last_state_change_at = float(now)
        return True, f"spawned churner on slot {sid}"

    def _churner_kill(
        self,
        *,
        slot_id: int,
        now_ts: float | None = None,
    ) -> tuple[bool, str]:
        now = float(now_ts if now_ts is not None else _now())
        sid = int(slot_id)
        if sid not in self.slots:
            return False, "slot_missing"
        state = self._churner_by_slot.get(sid)
        if state is None:
            return True, f"slot {sid} already idle"

        txids = [str(state.entry_txid or "").strip(), str(state.exit_txid or "").strip()]
        for txid in txids:
            if not txid:
                continue
            try:
                self._cancel_order(txid)
            except Exception:
                pass
        if str(state.stage) == "exit_open":
            self._churner_close_position_cancelled(
                slot_id=int(sid),
                state=state,
                now_ts=now,
                reason="killed",
            )
        self._churner_reset_state(state, now_ts=now, reason="killed")
        return True, f"killed churner on slot {sid}"

    def _churner_update_runtime_config(
        self,
        *,
        reserve_usd: float | None = None,
    ) -> tuple[bool, str]:
        if reserve_usd is None:
            return False, "reserve_usd required"
        try:
            reserve = max(0.0, float(reserve_usd))
        except (TypeError, ValueError):
            return False, "invalid reserve_usd"
        setattr(config, "CHURNER_RESERVE_USD", reserve)
        self._reconcile_churner_state()
        return True, f"churner reserve set to {reserve:.4f}"

    def _churner_gate_check(
        self,
        *,
        slot_id: int,
        state: ChurnerRuntimeState,
        parent: dict[str, Any],
        parent_order: sm.OrderState,
        now_ts: float,
    ) -> tuple[bool, str, float, float, float]:
        sid = int(slot_id)
        slot = self.slots.get(sid)
        if slot is None:
            return False, "slot_missing", 0.0, 0.0, 0.0
        regime_name, _confidence, _bias, _ready, _source = self._policy_hmm_signal()
        if str(regime_name or "").strip().upper() != "RANGING":
            return False, "regime_not_ranging", 0.0, 0.0, 0.0

        capacity = self._compute_capacity_health(now_ts)
        headroom = int(capacity.get("open_order_headroom") or 0)
        min_headroom = max(0, int(getattr(config, "CHURNER_MIN_HEADROOM", 0)))
        if headroom < min_headroom:
            return False, "headroom_low", 0.0, 0.0, 0.0

        market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        if market <= 0:
            return False, "market_unavailable", 0.0, 0.0, 0.0

        entry_side = self._churner_entry_side_for_trade(str(parent.get("trade_id") or parent_order.trade_id))
        entry_price = self._churner_entry_target_price(side=entry_side, market=market)
        if entry_price <= 0:
            return False, "invalid_entry_price", 0.0, 0.0, 0.0

        base_order_size_usd = max(
            0.0,
            float(getattr(config, "CHURNER_ORDER_SIZE_USD", getattr(config, "ORDER_SIZE_USD", 0.0))),
        )
        order_size_usd = max(base_order_size_usd, base_order_size_usd + float(state.compound_usd))
        cfg = self._engine_cfg(slot)
        volume = sm.compute_order_volume(float(entry_price), cfg, float(order_size_usd))
        if volume is None:
            return False, "below_min_size", 0.0, 0.0, 0.0
        volume = float(volume)
        required_usd = max(0.0, float(volume) * float(entry_price))

        free_usd, free_doge = self._available_free_balances(prefer_fresh=False)
        has_capital = (entry_side == "buy" and free_usd >= required_usd - 1e-12) or (
            entry_side == "sell" and free_doge >= volume - 1e-12
        )
        if has_capital:
            return True, "ok", float(entry_price), float(volume), float(required_usd)

        # Reserve backstop: one-order-size cap per slot.
        max_slot_reserve = max(base_order_size_usd, 0.0)
        already_alloc = max(0.0, float(state.reserve_allocated_usd))
        if already_alloc > max_slot_reserve + 1e-12:
            already_alloc = max_slot_reserve
        alloc_needed = min(max_slot_reserve - already_alloc, required_usd)
        if alloc_needed <= 1e-12:
            return False, "capital_unavailable", 0.0, 0.0, 0.0
        if self._churner_reserve_available_usd + 1e-12 < alloc_needed:
            return False, "reserve_exhausted", 0.0, 0.0, 0.0
        state.reserve_allocated_usd = already_alloc + alloc_needed
        self._churner_reserve_available_usd = max(0.0, self._churner_reserve_available_usd - alloc_needed)
        return True, "ok_reserve", float(entry_price), float(volume), float(required_usd)

    def _churner_open_position_on_entry_fill(
        self,
        *,
        slot_id: int,
        state: ChurnerRuntimeState,
        fill_price: float,
        fill_volume: float,
        fill_fee: float,
        fill_cost: float,
        fill_ts: float,
        exit_price_hint: float,
    ) -> int:
        if not self._position_ledger_enabled():
            return 0
        try:
            pid = self._position_ledger.open_position(
                slot_id=int(slot_id),
                trade_id=str(state.parent_trade_id or "B"),
                slot_mode="churner",
                cycle=max(1, int(state.cycle_id or 1)),
                entry_data={
                    "entry_price": float(fill_price),
                    "entry_cost": float(fill_cost if fill_cost > 0 else fill_price * fill_volume),
                    "entry_fee": float(fill_fee),
                    "entry_volume": float(fill_volume),
                    "entry_time": float(fill_ts),
                    "entry_regime": str(self._regime_label(self._current_regime_id())),
                    "entry_volatility": 0.0,
                },
                exit_data={
                    "current_exit_price": float(exit_price_hint),
                    "original_exit_price": float(exit_price_hint),
                    "target_profit_pct": float(getattr(config, "CHURNER_PROFIT_PCT", 0.0)),
                    "exit_txid": "",
                },
            )
            self._position_ledger.journal_event(
                int(pid),
                "created",
                {
                    "entry_price": float(fill_price),
                    "exit_price": float(exit_price_hint),
                    "regime": str(self._regime_label(self._current_regime_id())),
                    "slot_mode": "churner",
                },
                timestamp=float(fill_ts),
            )
            self._persist_position_ledger_row(int(pid))
            self._persist_position_journal_tail(int(pid), count=1)
            return int(pid)
        except Exception as e:
            logger.warning("churner open position failed slot=%s cycle=%s: %s", int(slot_id), int(state.cycle_id), e)
            return 0

    def _churner_close_position_cancelled(
        self,
        *,
        slot_id: int,
        state: ChurnerRuntimeState,
        now_ts: float,
        reason: str,
    ) -> None:
        pid = int(state.churner_position_id or 0)
        if pid <= 0:
            return
        try:
            exit_price = float(state.exit_price if state.exit_price > 0 else state.entry_fill_price)
            if exit_price <= 0:
                slot = self.slots.get(int(slot_id))
                exit_price = float(slot.state.market_price if slot and slot.state.market_price > 0 else self.last_price)
            self._position_ledger.close_position(
                pid,
                {
                    "exit_price": float(exit_price or 0.0),
                    "exit_cost": 0.0,
                    "exit_fee": 0.0,
                    "exit_time": float(now_ts),
                    "exit_regime": str(self._regime_label(self._current_regime_id())),
                    "net_profit": 0.0,
                    "close_reason": "cancelled",
                    "reason": str(reason or "cancelled"),
                    "age_seconds": max(0.0, float(now_ts) - float(state.entry_fill_time or now_ts)),
                },
            )
            self._persist_position_ledger_row(pid)
            self._persist_position_journal_tail(pid, count=1)
        except Exception as e:
            logger.warning("churner cancelled close failed slot=%s pid=%s: %s", int(slot_id), pid, e)

    def _churner_route_profit(
        self,
        *,
        slot_id: int,
        state: ChurnerRuntimeState,
        net_profit: float,
        now_ts: float,
    ) -> None:
        gain = max(0.0, float(net_profit))
        if gain <= 0.0:
            return
        parent_id = int(state.parent_position_id or 0)
        if parent_id <= 0:
            state.compound_usd += gain
            return

        parent = self._position_ledger.get_position(parent_id)
        slot = self.slots.get(int(slot_id))
        if not isinstance(parent, dict) or slot is None or str(parent.get("status") or "") != "open":
            state.compound_usd += gain
            return

        live = self._find_live_exit_for_position(parent)
        needed = 0.0
        if live is not None:
            _local_id, parent_order = live
            market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
            needed, _fillable = self._subsidy_needed_for_position(
                parent,
                slot=slot,
                side=parent_order.side,
                market=market,
                volume_override=float(parent_order.volume),
            )
        balance = max(0.0, float(self._position_ledger.get_subsidy_balance(int(slot_id))))
        if needed > 1e-12 and balance + 1e-12 < needed:
            try:
                self._position_ledger.journal_event(
                    parent_id,
                    "churner_profit",
                    {
                        "net_profit": float(gain),
                        "churner_cycle_id": int(state.cycle_id or 0),
                    },
                    timestamp=float(now_ts),
                )
                self._persist_position_journal_tail(parent_id, count=1)
            except Exception as e:
                logger.warning(
                    "churner profit journal failed slot=%s parent=%s cycle=%s: %s",
                    int(slot_id),
                    int(parent_id),
                    int(state.cycle_id or 0),
                    e,
                )
            return

        state.compound_usd += gain

    def _churner_on_entry_fill(
        self,
        *,
        slot_id: int,
        txid: str,
        fill_price: float,
        fill_volume: float,
        fill_fee: float,
        fill_cost: float,
        fill_ts: float,
    ) -> None:
        state = self._churner_by_slot.get(int(slot_id))
        if state is None:
            return
        txid_norm = str(txid or "").strip()
        if str(state.stage) != "entry_open" or txid_norm != str(state.entry_txid or "").strip():
            return
        slot = self.slots.get(int(slot_id))
        if slot is None:
            self._churner_reset_state(state, now_ts=float(fill_ts), reason="slot_missing")
            return

        state.entry_fill_price = float(fill_price)
        state.entry_fill_fee = max(0.0, float(fill_fee))
        state.entry_fill_time = float(fill_ts)
        state.entry_volume = max(0.0, float(fill_volume))
        market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        exit_price = self._churner_exit_target_price(
            entry_side=str(state.entry_side),
            entry_fill_price=float(fill_price),
            market=float(market),
        )
        if exit_price <= 0:
            self._churner_reset_state(
                state,
                now_ts=float(fill_ts),
                reason="invalid_exit_price",
                keep_active=True,
            )
            return

        pid = self._churner_open_position_on_entry_fill(
            slot_id=int(slot_id),
            state=state,
            fill_price=float(fill_price),
            fill_volume=float(fill_volume),
            fill_fee=float(fill_fee),
            fill_cost=float(fill_cost),
            fill_ts=float(fill_ts),
            exit_price_hint=float(exit_price),
        )
        state.churner_position_id = int(pid)
        exit_side = "sell" if str(state.entry_side) == "buy" else "buy"
        txid_new: str | None = None
        try:
            txid_new = self._place_order(
                side=exit_side,
                volume=float(fill_volume),
                price=float(exit_price),
                userref=(int(slot_id) * 1_000_000 + 960_000 + int(state.cycle_id or 0)),
            )
        except Exception:
            txid_new = None
        if not txid_new:
            self._churner_close_position_cancelled(
                slot_id=int(slot_id),
                state=state,
                now_ts=float(fill_ts),
                reason="exit_place_failed",
            )
            self._churner_reset_state(
                state,
                now_ts=float(fill_ts),
                reason="exit_place_failed",
                keep_active=True,
            )
            return

        state.exit_txid = str(txid_new)
        state.exit_price = float(exit_price)
        state.exit_placed_at = float(fill_ts)
        state.stage = "exit_open"
        state.last_state_change_at = float(fill_ts)
        state.entry_txid = ""
        self.ledger.commit_order(exit_side, float(exit_price), float(fill_volume))
        if pid > 0:
            self._position_ledger.bind_exit_txid(int(pid), str(txid_new))
            self._persist_position_ledger_row(int(pid))

    def _churner_on_exit_fill(
        self,
        *,
        slot_id: int,
        txid: str,
        fill_price: float,
        fill_volume: float,
        fill_fee: float,
        fill_cost: float,
        fill_ts: float,
    ) -> None:
        state = self._churner_by_slot.get(int(slot_id))
        if state is None:
            return
        txid_norm = str(txid or "").strip()
        if str(state.stage) != "exit_open" or txid_norm != str(state.exit_txid or "").strip():
            return

        vol = max(0.0, float(fill_volume if fill_volume > 0 else state.entry_volume))
        if vol <= 0:
            vol = max(0.0, float(state.entry_volume))
        if str(state.entry_side) == "buy":
            gross = (float(fill_price) - float(state.entry_fill_price)) * vol
        else:
            gross = (float(state.entry_fill_price) - float(fill_price)) * vol
        net_profit = float(gross) - (max(0.0, float(state.entry_fill_fee)) + max(0.0, float(fill_fee)))

        pid = int(state.churner_position_id or 0)
        if pid > 0:
            try:
                self._position_ledger.close_position(
                    pid,
                    {
                        "exit_price": float(fill_price),
                        "exit_cost": float(fill_cost if fill_cost > 0 else fill_price * vol),
                        "exit_fee": max(0.0, float(fill_fee)),
                        "exit_time": float(fill_ts),
                        "exit_regime": str(self._regime_label(self._current_regime_id())),
                        "net_profit": float(net_profit),
                        "close_reason": "filled",
                    },
                )
                row = self._position_ledger.get_position(pid)
                if isinstance(row, dict):
                    self._cycle_slot_mode[
                        (int(slot_id), str(row.get("trade_id") or state.parent_trade_id), int(row.get("cycle", 0) or 0))
                    ] = "churner"
                self._persist_position_ledger_row(pid)
                self._persist_position_journal_tail(pid, count=1)
            except Exception as e:
                logger.warning("churner close position failed slot=%s pid=%s: %s", int(slot_id), pid, e)

        self._churner_route_profit(
            slot_id=int(slot_id),
            state=state,
            net_profit=float(net_profit),
            now_ts=float(fill_ts),
        )
        self._churner_cycles_total += 1
        self._churner_profit_total += float(net_profit)
        self._churner_cycles_today += 1
        self._churner_profit_today += float(net_profit)
        self._churner_reset_state(state, now_ts=float(fill_ts), reason="", keep_active=True)

    def _churner_on_order_canceled(
        self,
        *,
        slot_id: int,
        kind: str,
        txid: str,
        now_ts: float,
    ) -> None:
        state = self._churner_by_slot.get(int(slot_id))
        if state is None:
            return
        txid_norm = str(txid or "").strip()
        if kind == "churner_entry" and txid_norm == str(state.entry_txid or "").strip():
            self._churner_reset_state(
                state,
                now_ts=float(now_ts),
                reason="entry_canceled",
                keep_active=bool(state.active),
            )
            return
        if kind == "churner_exit" and txid_norm == str(state.exit_txid or "").strip():
            self._churner_close_position_cancelled(
                slot_id=int(slot_id),
                state=state,
                now_ts=float(now_ts),
                reason="exit_canceled",
            )
            self._churner_reset_state(
                state,
                now_ts=float(now_ts),
                reason="exit_canceled",
                keep_active=bool(state.active),
            )

    def _churner_timeout_tick(self, *, slot_id: int, state: ChurnerRuntimeState, now_ts: float) -> None:
        now = float(now_ts)
        if str(state.stage) == "entry_open" and state.entry_placed_at > 0:
            timeout_sec = max(60.0, float(getattr(config, "CHURNER_TIMEOUT_SEC", 300)))
            if now - float(state.entry_placed_at) >= timeout_sec:
                txid = str(state.entry_txid or "").strip()
                if txid:
                    try:
                        ok = self._cancel_order(txid)
                    except Exception:
                        ok = False
                    if not ok:
                        state.last_error = "entry_timeout_cancel_failed"
                        return
                self._churner_reset_state(state, now_ts=now, reason="entry_timeout", keep_active=True)
            return

        if str(state.stage) == "exit_open" and state.exit_placed_at > 0:
            timeout_sec = max(
                float(getattr(config, "CHURNER_TIMEOUT_SEC", 300)),
                float(getattr(config, "CHURNER_EXIT_TIMEOUT_SEC", 600)),
            )
            if now - float(state.exit_placed_at) >= timeout_sec:
                txid = str(state.exit_txid or "").strip()
                if txid:
                    try:
                        ok = self._cancel_order(txid)
                    except Exception:
                        ok = False
                    if not ok:
                        state.last_error = "exit_timeout_cancel_failed"
                        return
                self._churner_close_position_cancelled(
                    slot_id=int(slot_id),
                    state=state,
                    now_ts=now,
                    reason="exit_timeout",
                )
                self._churner_reset_state(state, now_ts=now, reason="exit_timeout", keep_active=True)

    def _run_churner_engine(self, now_ts: float | None = None) -> None:
        now = float(now_ts if now_ts is not None else _now())
        if not self._churner_enabled():
            for sid, state in list(self._churner_by_slot.items()):
                txids = [str(state.entry_txid or "").strip(), str(state.exit_txid or "").strip()]
                for txid in txids:
                    if not txid:
                        continue
                    try:
                        self._cancel_order(txid)
                    except Exception:
                        pass
                if str(state.stage) == "exit_open":
                    self._churner_close_position_cancelled(
                        slot_id=int(sid),
                        state=state,
                        now_ts=now,
                        reason="churner_disabled",
                    )
                self._churner_reset_state(state, now_ts=now, reason="churner_disabled")
            return
        self._reconcile_churner_state()
        day_key = self._utc_day_key(now)
        if day_key != str(self._churner_day_key or ""):
            self._churner_day_key = day_key
            self._churner_cycles_today = 0
            self._churner_profit_today = 0.0

        max_reserve = max(0.0, float(getattr(config, "CHURNER_RESERVE_USD", 0.0)))
        if self._churner_reserve_available_usd > max_reserve:
            self._churner_reserve_available_usd = max_reserve

        for sid in sorted(self.slots.keys()):
            state = self._ensure_churner_state(int(sid))
            if not bool(state.active):
                continue

            stage = str(state.stage or "idle")
            txids = [str(state.entry_txid or "").strip(), str(state.exit_txid or "").strip()]
            parent_id = int(state.parent_position_id or 0)
            parent: dict[str, Any] | None = None
            if parent_id > 0:
                parent_row = self._position_ledger.get_position(parent_id)
                if isinstance(parent_row, dict) and str(parent_row.get("status") or "") == "open":
                    parent = parent_row

            if stage in {"entry_open", "exit_open"} and parent is None:
                for txid in txids:
                    if not txid:
                        continue
                    try:
                        self._cancel_order(txid)
                    except Exception:
                        pass
                if stage == "exit_open":
                    self._churner_close_position_cancelled(
                        slot_id=int(sid),
                        state=state,
                        now_ts=now,
                        reason="parent_closed",
                    )
                self._churner_reset_state(state, now_ts=now, reason="parent_closed", keep_active=True)
                continue

            capacity = self._compute_capacity_health(now)
            headroom = int(capacity.get("open_order_headroom") or 0)
            min_headroom = max(0, int(getattr(config, "CHURNER_MIN_HEADROOM", 0)))
            if headroom < min_headroom:
                for txid in txids:
                    if not txid:
                        continue
                    try:
                        self._cancel_order(txid)
                    except Exception:
                        pass
                if stage == "exit_open":
                    self._churner_close_position_cancelled(
                        slot_id=int(sid),
                        state=state,
                        now_ts=now,
                        reason="headroom_low",
                    )
                self._churner_reset_state(state, now_ts=now, reason="headroom_low", keep_active=True)
                continue

            regime_name, _conf, _bias, _ready, _source = self._policy_hmm_signal()
            if str(regime_name or "").strip().upper() != "RANGING":
                for txid in txids:
                    if not txid:
                        continue
                    try:
                        self._cancel_order(txid)
                    except Exception:
                        pass
                if stage == "exit_open":
                    self._churner_close_position_cancelled(
                        slot_id=int(sid),
                        state=state,
                        now_ts=now,
                        reason="regime_shift",
                    )
                self._churner_reset_state(state, now_ts=now, reason="regime_shift", keep_active=True)
                continue

            if stage in {"entry_open", "exit_open"}:
                self._churner_timeout_tick(slot_id=int(sid), state=state, now_ts=now)
                continue

            parent_order: sm.OrderState | None = None
            if parent is not None:
                live = self._find_live_exit_for_position(parent)
                if live is not None:
                    _local_id, parent_order = live

            if parent is None or parent_order is None:
                candidate = self._churner_candidate_parent_position(int(sid), now_ts=now)
                if candidate is None:
                    state.parent_position_id = 0
                    state.parent_trade_id = ""
                    state.stage = "idle"
                    state.last_error = "no_candidate"
                    state.last_state_change_at = float(now)
                    continue
                parent, parent_order, _effective_age, _band = candidate
                state.parent_position_id = int(parent.get("position_id", 0) or 0)
                state.parent_trade_id = str(parent.get("trade_id") or parent_order.trade_id)

            ok, gate_reason, entry_price, volume, _required_usd = self._churner_gate_check(
                slot_id=int(sid),
                state=state,
                parent=parent,
                parent_order=parent_order,
                now_ts=now,
            )
            if not ok:
                state.last_error = str(gate_reason)
                continue

            state.cycle_id = max(1, int(self._churner_next_cycle_id))
            self._churner_next_cycle_id += 1
            state.order_size_usd = max(
                float(entry_price) * float(volume),
                max(
                    0.0,
                    float(getattr(config, "CHURNER_ORDER_SIZE_USD", getattr(config, "ORDER_SIZE_USD", 0.0))),
                )
                + max(0.0, float(state.compound_usd)),
            )
            state.entry_side = self._churner_entry_side_for_trade(state.parent_trade_id)
            reserved = self._try_reserve_loop_funds(
                side=str(state.entry_side),
                volume=float(volume),
                price=float(entry_price),
            )
            txid_new: str | None = None
            try:
                txid_new = self._place_order(
                    side=str(state.entry_side),
                    volume=float(volume),
                    price=float(entry_price),
                    userref=(int(sid) * 1_000_000 + 960_000 + int(state.cycle_id)),
                )
            except Exception:
                txid_new = None
            if not txid_new:
                if reserved:
                    self._release_loop_reservation(
                        side=str(state.entry_side),
                        volume=float(volume),
                        price=float(entry_price),
                )
                state.last_error = "entry_place_failed"
                self._churner_release_reserve(state)
                continue

            state.stage = "entry_open"
            state.entry_txid = str(txid_new)
            state.entry_price = float(entry_price)
            state.entry_volume = float(volume)
            state.entry_placed_at = float(now)
            state.exit_txid = ""
            state.exit_price = 0.0
            state.exit_placed_at = 0.0
            state.churner_position_id = 0
            state.last_error = ""
            state.last_state_change_at = float(now)
            self.ledger.commit_order(str(state.entry_side), float(entry_price), float(volume))

    def _self_healing_status_payload(self, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else _now())
        if not self._position_ledger_enabled():
            return {"enabled": False}

        open_positions = self._position_ledger.get_open_positions()
        totals = self._position_ledger.get_subsidy_totals()

        bands = {"fresh": 0, "aging": 0, "stale": 0, "stuck": 0, "write_off": 0}
        slot_subsidy: list[dict[str, Any]] = []
        pending_needed = 0.0
        pending_positions = 0
        for sid in sorted(self.slots.keys()):
            slot_totals = self._position_ledger.get_subsidy_totals(slot_id=sid)
            slot_subsidy.append(
                {
                    "slot_id": int(sid),
                    "balance": float(slot_totals.get("balance", 0.0)),
                    "earned": float(slot_totals.get("earned", 0.0)),
                    "consumed": float(slot_totals.get("consumed", 0.0)),
                }
            )

        for pos in open_positions:
            sid = int(pos.get("slot_id", -1))
            slot = self.slots.get(sid)
            market = (
                float(slot.state.market_price if slot and slot.state.market_price > 0 else self.last_price)
                if self.last_price > 0 or slot is not None
                else 0.0
            )
            if market <= 0:
                continue
            exit_px = float(pos.get("current_exit_price", 0.0) or 0.0)
            entry_px = float(pos.get("entry_price", 0.0) or 0.0)
            vol = float(pos.get("entry_volume", 0.0) or 0.0)
            age = max(0.0, now - float(pos.get("entry_time", now) or now))
            distance = abs(exit_px - market) / market * 100.0 if market > 0 else 0.0
            effective_age = self._effective_age_seconds(age, distance)
            band = self._age_band_for_effective_age(effective_age)
            if band in bands:
                bands[band] += 1

            entry_pct = max(0.0, float(self.entry_pct)) / 100.0
            fee_floor = max(0.0, float(config.ROUND_TRIP_FEE_PCT)) / 100.0
            trade_id = str(pos.get("trade_id") or "")
            needed = 0.0
            if vol > 0 and entry_px > 0:
                if trade_id == "B":
                    fillable = max(market * (1.0 + entry_pct), entry_px * (1.0 + fee_floor))
                    if exit_px > fillable:
                        needed = (exit_px - fillable) * vol
                elif trade_id == "A":
                    fillable = min(market * (1.0 - entry_pct), entry_px * (1.0 - fee_floor))
                    if exit_px < fillable:
                        needed = (fillable - exit_px) * vol
            if needed > 1e-12:
                pending_positions += 1
                pending_needed += max(0.0, needed)

        cleanup_queue, hidden_by_hold = self._self_heal_cleanup_queue_rows(now)

        active_churners = sum(1 for state in self._churner_by_slot.values() if bool(state.active))
        churner_paused_reason = ""
        if not self._churner_enabled():
            churner_paused_reason = "disabled"
        elif active_churners <= 0:
            regime_name, _confidence, _bias, _ready, _source = self._policy_hmm_signal()
            if str(regime_name or "").strip().upper() != "RANGING":
                churner_paused_reason = "regime_not_ranging"
            elif (
                int(bands.get("aging", 0))
                + int(bands.get("stale", 0))
                + int(bands.get("stuck", 0))
                + int(bands.get("write_off", 0))
            ) <= 0:
                churner_paused_reason = "no_aging_positions"
            else:
                churner_paused_reason = "idle"

        eta_hours: float | None
        if pending_needed <= 1e-12:
            eta_hours = 0.0
        else:
            day_dt = datetime.fromtimestamp(now, timezone.utc)
            day_start = datetime(day_dt.year, day_dt.month, day_dt.day, tzinfo=timezone.utc).timestamp()
            elapsed_hours = max(1e-6, (now - day_start) / 3600.0)
            churner_hourly_rate = max(0.0, float(self._churner_profit_today)) / elapsed_hours
            eta_hours = (pending_needed / churner_hourly_rate) if churner_hourly_rate > 1e-9 else None

        total_open = max(0, len(open_positions))
        age_heatmap_rows = []
        for band_name in ("fresh", "aging", "stale", "stuck", "write_off"):
            count = int(bands.get(band_name, 0))
            pct = (float(count) / float(total_open) * 100.0) if total_open > 0 else 0.0
            age_heatmap_rows.append({"band": band_name, "count": count, "pct": float(pct)})

        subsidy_balance = float(totals.get("balance", 0.0))
        subsidy_earned = float(totals.get("earned", 0.0))
        subsidy_consumed = float(totals.get("consumed", 0.0))
        return {
            "enabled": True,
            "open_positions": len(open_positions),
            "journal_entries_local": len(self._position_ledger.get_journal()),
            "subsidy": {
                "balance": subsidy_balance,
                "pool_usd": subsidy_balance,
                "earned": subsidy_earned,
                "lifetime_earned": subsidy_earned,
                "consumed": subsidy_consumed,
                "lifetime_spent": subsidy_consumed,
                "pending_needed": float(pending_needed),
                "pending_needed_usd": float(pending_needed),
                "pending_positions": int(pending_positions),
                "eta_hours": None if eta_hours is None else float(eta_hours),
                "eta_source": "churner_profit_today",
                "by_slot": slot_subsidy,
            },
            "age_bands": bands,
            "age_heatmap": {
                "total_open": int(total_open),
                "bands": age_heatmap_rows,
            },
            "repricing": {
                "enabled": self._flag_value("SUBSIDY_ENABLED"),
                "auto_band": str(getattr(config, "SUBSIDY_AUTO_REPRICE_BAND", "stuck")),
                "lifetime_repriced": int(self._self_heal_reprice_total),
                "last_reprice_at": self._self_heal_reprice_last_at or None,
                "last_summary": dict(self._self_heal_reprice_last_summary or {}),
            },
            "churner": {
                "enabled": self._churner_enabled(),
                "active_slots": int(active_churners),
                "reserve_available_usd": float(self._churner_reserve_available_usd),
                "cycles_today": int(self._churner_cycles_today),
                "profit_today": float(self._churner_profit_today),
                "cycles_total": int(self._churner_cycles_total),
                "profit_total": float(self._churner_profit_total),
                "paused_reason": str(churner_paused_reason),
            },
            "cleanup_queue": cleanup_queue,
            "cleanup_queue_summary": {
                "count": int(len(cleanup_queue)),
                "hidden_by_hold": int(hidden_by_hold),
            },
            "migration": {
                "done": bool(self._position_ledger_migration_done),
                "last_at": self._position_ledger_migration_last_at or None,
                "last_created": int(self._position_ledger_migration_last_created),
                "last_scanned": int(self._position_ledger_migration_last_scanned),
            },
        }

    def _collect_throughput_cycles(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for slot in self.slots.values():
            for c in slot.state.completed_cycles:
                cycle_mode = self._cycle_slot_mode.get((int(slot.slot_id), str(c.trade_id), int(c.cycle)), "legacy")
                regime_at_entry = c.regime_at_entry
                if str(cycle_mode or "legacy") == "churner":
                    # Churner cycles are bucketed as ranging-only so they do not
                    # perturb sticky-slot regime-side stats.
                    regime_at_entry = 1
                rows.append(
                    {
                        "entry_time": float(c.entry_time or 0.0),
                        "exit_time": float(c.exit_time or 0.0),
                        "trade_id": str(c.trade_id or ""),
                        "net_profit": float(c.net_profit),
                        "volume": float(c.volume or 0.0),
                        "regime_at_entry": regime_at_entry,
                    }
                )
        return rows

    def _collect_open_exits(self, now_ts: float | None = None) -> list[dict[str, Any]]:
        now = float(now_ts if now_ts is not None else _now())
        rows: list[dict[str, Any]] = []
        include_recoveries = self._recovery_orders_enabled()
        for slot in self.slots.values():
            for o in slot.state.orders:
                if o.role != "exit":
                    continue
                entry_filled_at = float(o.entry_filled_at or 0.0)
                if entry_filled_at <= 0.0:
                    continue
                rows.append(
                    {
                        "regime_at_entry": o.regime_at_entry,
                        "trade_id": str(o.trade_id or ""),
                        "age_sec": max(0.0, now - entry_filled_at),
                        "volume": float(o.volume or 0.0),
                    }
                )
            if include_recoveries:
                for r in slot.state.recovery_orders:
                    entry_filled_at = float(r.entry_filled_at or 0.0)
                    if entry_filled_at <= 0.0:
                        continue
                    rows.append(
                        {
                            "regime_at_entry": r.regime_at_entry,
                            "trade_id": str(r.trade_id or ""),
                            "age_sec": max(0.0, now - entry_filled_at),
                            "volume": float(r.volume or 0.0),
                        }
                    )
        return rows

    def _update_throughput(self) -> None:
        if self._throughput is None:
            return
        completed = self._collect_throughput_cycles()
        open_exits = self._collect_open_exits()
        _, free_doge = self._available_free_balances(prefer_fresh=False)
        self._throughput.update(
            completed,
            open_exits=open_exits,
            regime_label=self._regime_label(self._current_regime_id()),
            free_doge=float(free_doge),
        )

    @staticmethod
    def _manifold_active_throughput_multiplier(throughput_payload: dict[str, Any]) -> float:
        def _bucket_mult(name: str) -> float:
            try:
                return float((throughput_payload.get(name) or {}).get("multiplier", 1.0) or 1.0)
            except Exception:
                return 1.0

        regime = str(throughput_payload.get("active_regime", "ranging") or "ranging").strip().lower()
        if regime in {"bearish", "ranging", "bullish"}:
            mult = (_bucket_mult(f"{regime}_A") + _bucket_mult(f"{regime}_B")) * 0.5
        else:
            mult = _bucket_mult("aggregate")
        return max(0.0, min(2.0, float(mult)))

    def _update_manifold_score(self, now: float | None = None) -> None:
        now_ts = float(now if now is not None else _now())
        if not self._flag_value("MTS_ENABLED"):
            self._manifold_score = bayesian_engine.ManifoldScore(enabled=False)
            self._manifold_history.clear()
            return

        belief = self._belief_state
        throughput_payload = self._throughput.status_payload() if self._throughput is not None else {}
        throughput_mult = 1.0
        age_pressure = 1.0
        if isinstance(throughput_payload, dict):
            throughput_mult = self._manifold_active_throughput_multiplier(throughput_payload)
            try:
                age_pressure = float(throughput_payload.get("age_pressure", 1.0) or 1.0)
            except Exception:
                age_pressure = 1.0
        age_pressure = max(0.0, min(1.0, age_pressure))
        try:
            slot_vintage = self._slot_vintage_metrics_locked(now_ts)
            stuck_capital_pct = float(slot_vintage.get("stuck_capital_pct", 0.0) or 0.0)
        except Exception:
            stuck_capital_pct = 0.0

        try:
            self._manifold_score = bayesian_engine.compute_manifold_score(
                posterior_1m=getattr(belief, "posterior_1m", [0.0, 1.0, 0.0]),
                posterior_15m=getattr(belief, "posterior_15m", [0.0, 1.0, 0.0]),
                posterior_1h=getattr(belief, "posterior_1h", [0.0, 1.0, 0.0]),
                p_switch_1m=float(getattr(belief, "p_switch_1m", 0.0) or 0.0),
                p_switch_15m=float(getattr(belief, "p_switch_15m", 0.0) or 0.0),
                p_switch_1h=float(getattr(belief, "p_switch_1h", 0.0) or 0.0),
                bocpd_change_prob=float(getattr(self._bocpd_state, "change_prob", 0.0) or 0.0),
                bocpd_run_length=float(getattr(self._bocpd_state, "run_length_mode", 0.0) or 0.0),
                throughput_multiplier=throughput_mult,
                age_pressure=age_pressure,
                stuck_capital_pct=stuck_capital_pct,
                entropy_consensus=float(getattr(belief, "entropy_consensus", 0.0) or 0.0),
                direction_score=float(getattr(belief, "direction_score", 0.0) or 0.0),
                clarity_weights=list(getattr(config, "MTS_CLARITY_WEIGHTS", [0.2, 0.5, 0.3])),
                stability_switch_weights=list(
                    getattr(config, "MTS_STABILITY_SWITCH_WEIGHTS", [0.2, 0.5, 0.3])
                ),
                coherence_weights=list(getattr(config, "MTS_COHERENCE_WEIGHTS", [0.5, 0.25, 0.25])),
                enabled=True,
                kernel_enabled=self._flag_value("MTS_KERNEL_ENABLED"),
                kernel_samples=len(self._manifold_history),
                kernel_score=None,
                kernel_min_samples=max(1, int(getattr(config, "MTS_KERNEL_MIN_SAMPLES", 200))),
                kernel_alpha_max=max(0.0, min(1.0, float(getattr(config, "MTS_KERNEL_ALPHA_MAX", 0.5)))),
            )
        except Exception as exc:
            logger.debug("manifold score update failed: %s", exc)
            self._manifold_score = bayesian_engine.ManifoldScore(enabled=False)
            return

        if not bool(getattr(self._manifold_score, "enabled", False)):
            self._manifold_history.clear()
            return

        components = getattr(self._manifold_score, "components", None)
        self._manifold_history.append(
            (
                float(now_ts),
                float(getattr(self._manifold_score, "mts", 0.0) or 0.0),
                float(getattr(components, "regime_clarity", 0.0) if components is not None else 0.0),
                float(getattr(components, "regime_stability", 0.0) if components is not None else 0.0),
                float(getattr(components, "throughput_efficiency", 0.0) if components is not None else 0.0),
                float(getattr(components, "signal_coherence", 0.0) if components is not None else 0.0),
            )
        )

    def _hmm_runtime_config(self, *, min_train_samples: int | None = None) -> dict[str, Any]:
        resolved_min_samples = max(
            50,
            int(
                getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)
                if min_train_samples is None
                else min_train_samples
            ),
        )
        return {
            "HMM_N_STATES": max(2, int(getattr(config, "HMM_N_STATES", 3))),
            "HMM_N_ITER": max(10, int(getattr(config, "HMM_N_ITER", 100))),
            "HMM_COVARIANCE_TYPE": str(getattr(config, "HMM_COVARIANCE_TYPE", "diag") or "diag"),
            "HMM_INFERENCE_WINDOW": max(5, int(getattr(config, "HMM_INFERENCE_WINDOW", 50))),
            "HMM_CONFIDENCE_THRESHOLD": max(
                0.0, float(getattr(config, "HMM_CONFIDENCE_THRESHOLD", 0.15))
            ),
            "HMM_RETRAIN_INTERVAL_SEC": max(
                300.0, float(getattr(config, "HMM_RETRAIN_INTERVAL_SEC", 86400.0))
            ),
            "HMM_MIN_TRAIN_SAMPLES": resolved_min_samples,
            "HMM_BIAS_GAIN": max(0.0, float(getattr(config, "HMM_BIAS_GAIN", 1.0))),
            "HMM_BLEND_WITH_TREND": max(
                0.0, min(1.0, float(getattr(config, "HMM_BLEND_WITH_TREND", 0.5)))
            ),
            "ENRICHED_FEATURES_ENABLED": self._flag_value("ENRICHED_FEATURES_ENABLED"),
        }

    def _refresh_hmm_state_from_detector(
        self,
        *,
        secondary: bool = False,
        tertiary: bool = False,
    ) -> None:
        detector = (
            self._hmm_detector_tertiary
            if tertiary
            else self._hmm_detector_secondary
            if secondary
            else self._hmm_detector
        )
        if tertiary:
            if not isinstance(self._hmm_state_tertiary, dict):
                self._hmm_state_tertiary = self._hmm_default_state(
                    enabled=self._flag_value("HMM_ENABLED")
                    and self._flag_value("HMM_TERTIARY_ENABLED"),
                    interval_min=max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60))),
                )
            state = self._hmm_state_tertiary
            enabled_flag = self._flag_value("HMM_ENABLED") and bool(
                self._flag_value("HMM_TERTIARY_ENABLED")
            )
            interval_min = max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)))
        elif secondary:
            if not isinstance(self._hmm_state_secondary, dict):
                self._hmm_state_secondary = self._hmm_default_state(
                    enabled=self._flag_value("HMM_ENABLED")
                    and self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"),
                    interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
                )
            state = self._hmm_state_secondary
            enabled_flag = self._flag_value("HMM_ENABLED") and bool(
                self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED")
            )
            interval_min = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        else:
            if not isinstance(self._hmm_state, dict):
                self._hmm_state = self._hmm_default_state(
                    enabled=self._flag_value("HMM_ENABLED"),
                    interval_min=max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
                )
            state = self._hmm_state
            enabled_flag = self._flag_value("HMM_ENABLED")
            interval_min = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))

        if not detector:
            state["enabled"] = enabled_flag
            state["available"] = False
            state["trained"] = False
            state["interval_min"] = interval_min
            state["blend_factor"] = max(
                0.0, min(1.0, float(getattr(config, "HMM_BLEND_WITH_TREND", 0.5)))
            )
            return

        st = getattr(detector, "state", None)
        probs = [0.0, 1.0, 0.0]
        if st is not None:
            raw_probs = list(getattr(st, "probabilities", []) or [])
            if len(raw_probs) >= 3:
                try:
                    probs = [float(raw_probs[0]), float(raw_probs[1]), float(raw_probs[2])]
                except (TypeError, ValueError):
                    probs = [0.0, 1.0, 0.0]

        regime_id = 1
        if st is not None:
            try:
                regime_id = int(getattr(st, "regime", 1))
            except (TypeError, ValueError):
                regime_id = 1
        regime_name = "RANGING"
        try:
            if self._hmm_module and hasattr(self._hmm_module, "Regime"):
                regime_name = str(self._hmm_module.Regime(regime_id).name)
            else:
                regime_name = {0: "BEARISH", 1: "RANGING", 2: "BULLISH"}.get(regime_id, "RANGING")
        except Exception:
            regime_name = "RANGING"

        state.update({
            "enabled": enabled_flag,
            "available": True,
            "trained": bool(getattr(detector, "_trained", False)),
            "interval_min": interval_min,
            "regime": regime_name,
            "regime_id": regime_id,
            "confidence": float(getattr(st, "confidence", 0.0) if st is not None else 0.0),
            "bias_signal": float(getattr(st, "bias_signal", 0.0) if st is not None else 0.0),
            "probabilities": {
                "bearish": probs[0],
                "ranging": probs[1],
                "bullish": probs[2],
            },
            "observation_count": int(getattr(st, "observation_count", 0) if st is not None else 0),
            "blend_factor": max(
                0.0, min(1.0, float(getattr(config, "HMM_BLEND_WITH_TREND", 0.5)))
            ),
            "last_update_ts": float(getattr(st, "last_update_ts", 0.0) if st is not None else 0.0),
            "last_train_ts": float(getattr(detector, "_last_train_ts", 0.0) or 0.0),
        })

    def _init_hmm_runtime(self) -> None:
        primary_interval = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
        secondary_interval = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        tertiary_interval = max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)))
        hmm_enabled = self._flag_value("HMM_ENABLED")
        multi_enabled = self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED")
        tertiary_enabled = self._flag_value("HMM_TERTIARY_ENABLED")

        self._hmm_state = self._hmm_default_state(enabled=hmm_enabled, interval_min=primary_interval)
        self._hmm_state_secondary = self._hmm_default_state(
            enabled=bool(hmm_enabled and multi_enabled),
            interval_min=secondary_interval,
        )
        self._hmm_state_tertiary = self._hmm_default_state(
            enabled=bool(hmm_enabled and tertiary_enabled),
            interval_min=tertiary_interval,
        )
        self._hmm_consensus = dict(self._hmm_state)
        self._hmm_consensus.update({
            "agreement": "primary_only",
            "source_mode": self._hmm_source_mode(),
            "multi_timeframe": bool(multi_enabled),
        })
        self._hmm_detector = None
        self._hmm_detector_secondary = None
        self._hmm_detector_tertiary = None
        self._hmm_module = None
        self._hmm_numpy = None

        if not hmm_enabled:
            self._hmm_consensus = self._compute_hmm_consensus()
            self._update_hmm_tertiary_transition(_now())
            return

        try:
            import numpy as np  # type: ignore
            import hmm_regime_detector as hmm_mod  # type: ignore

            self._hmm_numpy = np
            self._hmm_module = hmm_mod
            self._hmm_detector = hmm_mod.RegimeDetector(
                config=self._hmm_runtime_config(
                    min_train_samples=max(1, int(getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500))),
                )
            )
            self._hmm_state["available"] = True
            self._hmm_state["error"] = ""
            self._refresh_hmm_state_from_detector()
            if multi_enabled:
                try:
                    self._hmm_detector_secondary = hmm_mod.RegimeDetector(
                        config=self._hmm_runtime_config(
                            min_train_samples=max(
                                1,
                                int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)),
                            ),
                        )
                    )
                    self._hmm_state_secondary["available"] = True
                    self._hmm_state_secondary["error"] = ""
                    self._refresh_hmm_state_from_detector(secondary=True)
                except Exception as e:
                    self._hmm_state_secondary["available"] = False
                    self._hmm_state_secondary["trained"] = False
                    self._hmm_state_secondary["error"] = str(e)
                    logger.warning(
                        "Secondary HMM runtime unavailable, continuing with primary HMM only: %s",
                        e,
                    )
            if tertiary_enabled:
                try:
                    self._hmm_detector_tertiary = hmm_mod.RegimeDetector(
                        config=self._hmm_runtime_config(
                            min_train_samples=max(
                                1,
                                int(getattr(config, "HMM_TERTIARY_MIN_TRAIN_SAMPLES", 150)),
                            ),
                        )
                    )
                    self._hmm_state_tertiary["available"] = True
                    self._hmm_state_tertiary["error"] = ""
                    self._refresh_hmm_state_from_detector(tertiary=True)
                except Exception as e:
                    self._hmm_state_tertiary["available"] = False
                    self._hmm_state_tertiary["trained"] = False
                    self._hmm_state_tertiary["error"] = str(e)
                    logger.warning(
                        "Tertiary HMM runtime unavailable, continuing without 1h detector: %s",
                        e,
                    )
            self._push_private_features_to_hmm_detectors()
            logger.info("HMM runtime initialized (advisory mode enabled)")
        except Exception as e:
            self._hmm_state["available"] = False
            self._hmm_state["trained"] = False
            self._hmm_state["error"] = str(e)
            self._hmm_state_secondary["available"] = False
            self._hmm_state_secondary["trained"] = False
            self._hmm_state_secondary["error"] = str(e)
            self._hmm_state_tertiary["available"] = False
            self._hmm_state_tertiary["trained"] = False
            self._hmm_state_tertiary["error"] = str(e)
            logger.warning("HMM runtime unavailable, continuing with trend-only logic: %s", e)
        self._hmm_consensus = self._compute_hmm_consensus()
        self._update_hmm_tertiary_transition(_now())

    def _snapshot_hmm_state(self) -> dict[str, Any]:
        if not self._hmm_module:
            return {}
        out: dict[str, Any] = {}
        try:
            if self._hmm_detector and hasattr(self._hmm_module, "serialize_for_snapshot"):
                snap = self._hmm_module.serialize_for_snapshot(self._hmm_detector)
                if isinstance(snap, dict):
                    out.update(dict(snap))
            if self._hmm_detector_secondary and hasattr(self._hmm_module, "serialize_for_snapshot"):
                sec_snap = self._hmm_module.serialize_for_snapshot(self._hmm_detector_secondary)
                if isinstance(sec_snap, dict):
                    out["_hmm_secondary_regime_state"] = dict(
                        sec_snap.get("_hmm_regime_state", {}) or {}
                    )
                    out["_hmm_secondary_last_train_ts"] = float(
                        sec_snap.get("_hmm_last_train_ts", 0.0) or 0.0
                    )
                    out["_hmm_secondary_trained"] = bool(
                        sec_snap.get("_hmm_trained", False)
                    )
            if self._hmm_detector_tertiary and hasattr(self._hmm_module, "serialize_for_snapshot"):
                ter_snap = self._hmm_module.serialize_for_snapshot(self._hmm_detector_tertiary)
                if isinstance(ter_snap, dict):
                    out["_hmm_tertiary_regime_state"] = dict(
                        ter_snap.get("_hmm_regime_state", {}) or {}
                    )
                    out["_hmm_tertiary_last_train_ts"] = float(
                        ter_snap.get("_hmm_last_train_ts", 0.0) or 0.0
                    )
                    out["_hmm_tertiary_trained"] = bool(
                        ter_snap.get("_hmm_trained", False)
                    )
        except Exception as e:
            logger.warning("HMM snapshot serialization failed: %s", e)
        return out

    def _restore_hmm_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            return
        if not self._hmm_module:
            return
        try:
            if self._hmm_detector and "_hmm_regime_state" in snapshot and hasattr(self._hmm_module, "restore_from_snapshot"):
                self._hmm_module.restore_from_snapshot(self._hmm_detector, snapshot)
            if (
                self._hmm_detector_secondary
                and "_hmm_secondary_regime_state" in snapshot
                and hasattr(self._hmm_module, "restore_from_snapshot")
            ):
                sec_snap = {
                    "_hmm_regime_state": snapshot.get("_hmm_secondary_regime_state", {}),
                    "_hmm_last_train_ts": snapshot.get("_hmm_secondary_last_train_ts", 0.0),
                    "_hmm_trained": snapshot.get("_hmm_secondary_trained", False),
                }
                self._hmm_module.restore_from_snapshot(self._hmm_detector_secondary, sec_snap)
            if (
                self._hmm_detector_tertiary
                and "_hmm_tertiary_regime_state" in snapshot
                and hasattr(self._hmm_module, "restore_from_snapshot")
            ):
                ter_snap = {
                    "_hmm_regime_state": snapshot.get("_hmm_tertiary_regime_state", {}),
                    "_hmm_last_train_ts": snapshot.get("_hmm_tertiary_last_train_ts", 0.0),
                    "_hmm_trained": snapshot.get("_hmm_tertiary_trained", False),
                }
                self._hmm_module.restore_from_snapshot(self._hmm_detector_tertiary, ter_snap)
        except Exception as e:
            logger.warning("HMM snapshot restore failed: %s", e)
        finally:
            self._refresh_hmm_state_from_detector()
            self._refresh_hmm_state_from_detector(secondary=True)
            self._refresh_hmm_state_from_detector(tertiary=True)
            self._update_hmm_tertiary_transition(_now())

    def _train_hmm(self, *, now: float | None = None, reason: str = "scheduled") -> bool:
        if not self._hmm_detector or self._hmm_numpy is None:
            return False
        self._push_private_features_to_hmm_detectors()

        now_ts = float(now if now is not None else _now())
        retry_sec = max(60.0, float(getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0)))
        is_trained = bool(getattr(self._hmm_detector, "_trained", False))
        if (not is_trained) and (now_ts - self._hmm_last_train_attempt_ts) < retry_sec and reason != "startup":
            return False

        self._hmm_last_train_attempt_ts = now_ts
        interval_min = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
        target_candles = max(1, int(getattr(config, "HMM_TRAINING_CANDLES", 4000)))
        min_train_samples = max(1, int(getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)))
        closes, volumes = self._fetch_training_candles(
            count=target_candles,
            interval_min=interval_min,
        )
        self._update_hmm_training_depth(
            current_candles=min(len(closes), len(volumes)),
            secondary=False,
            target_candles=target_candles,
            min_train_samples=min_train_samples,
            interval_min=interval_min,
            now=now_ts,
        )
        if not closes or not volumes:
            self._hmm_state["error"] = "no_training_candles"
            self._refresh_hmm_state_from_detector()
            return False

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            ok = bool(self._hmm_detector.train(closes_arr, volumes_arr))
        except Exception as e:
            logger.warning("HMM train failed (%s): %s", reason, e)
            self._hmm_state["error"] = f"train_failed:{e}"
            self._refresh_hmm_state_from_detector()
            return False

        if ok:
            self._hmm_state["error"] = ""
            logger.info("HMM trained (%s) with %d candles", reason, len(closes))
        else:
            self._hmm_state["error"] = "train_skipped_or_failed"
        self._refresh_hmm_state_from_detector()
        return ok

    def _train_hmm_secondary(self, *, now: float | None = None, reason: str = "scheduled") -> bool:
        if not self._hmm_detector_secondary or self._hmm_numpy is None:
            return False
        self._push_private_features_to_hmm_detectors()

        now_ts = float(now if now is not None else _now())
        retry_sec = max(
            60.0,
            float(
                getattr(
                    config,
                    "HMM_SECONDARY_SYNC_INTERVAL_SEC",
                    getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                )
            ),
        )
        is_trained = bool(getattr(self._hmm_detector_secondary, "_trained", False))
        if (
            (not is_trained)
            and (now_ts - self._hmm_last_train_attempt_ts_secondary) < retry_sec
            and reason != "startup"
        ):
            return False

        self._hmm_last_train_attempt_ts_secondary = now_ts
        interval_min = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        target_candles = max(1, int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 1440)))
        min_train_samples = max(1, int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)))
        closes, volumes = self._fetch_training_candles(
            count=target_candles,
            interval_min=interval_min,
        )
        self._update_hmm_training_depth(
            current_candles=min(len(closes), len(volumes)),
            secondary=True,
            target_candles=target_candles,
            min_train_samples=min_train_samples,
            interval_min=interval_min,
            now=now_ts,
        )
        if not closes or not volumes:
            self._hmm_state_secondary["error"] = "no_training_candles"
            self._refresh_hmm_state_from_detector(secondary=True)
            return False

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            ok = bool(self._hmm_detector_secondary.train(closes_arr, volumes_arr))
        except Exception as e:
            logger.warning("Secondary HMM train failed (%s): %s", reason, e)
            self._hmm_state_secondary["error"] = f"train_failed:{e}"
            self._refresh_hmm_state_from_detector(secondary=True)
            return False

        if ok:
            self._hmm_state_secondary["error"] = ""
            logger.info("Secondary HMM trained (%s) with %d candles", reason, len(closes))
        else:
            self._hmm_state_secondary["error"] = "train_skipped_or_failed"
        self._refresh_hmm_state_from_detector(secondary=True)
        return ok

    def _update_hmm_secondary(self, now: float) -> None:
        if not self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"):
            return
        if not self._hmm_detector_secondary or self._hmm_numpy is None:
            self._refresh_hmm_state_from_detector(secondary=True)
            return
        self._push_private_features_to_hmm_detectors()

        trained = bool(getattr(self._hmm_detector_secondary, "_trained", False))
        if not trained:
            self._train_hmm_secondary(now=now, reason="startup")
            trained = bool(getattr(self._hmm_detector_secondary, "_trained", False))
        else:
            try:
                if bool(self._hmm_detector_secondary.needs_retrain()):
                    self._train_hmm_secondary(now=now, reason="periodic")
            except Exception as e:
                logger.debug("Secondary HMM retrain check failed: %s", e)

        if not trained:
            self._refresh_hmm_state_from_detector(secondary=True)
            return

        interval_min = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        closes, volumes = self._fetch_recent_candles(
            count=int(getattr(config, "HMM_SECONDARY_RECENT_CANDLES", 50)),
            interval_min=interval_min,
        )
        if not closes or not volumes:
            self._refresh_hmm_state_from_detector(secondary=True)
            return

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            self._hmm_detector_secondary.update(closes_arr, volumes_arr)
            self._hmm_state_secondary["error"] = ""
        except Exception as e:
            logger.warning("Secondary HMM inference failed: %s", e)
            self._hmm_state_secondary["error"] = f"inference_failed:{e}"
        finally:
            self._refresh_hmm_state_from_detector(secondary=True)

    def _train_hmm_tertiary(self, *, now: float | None = None, reason: str = "scheduled") -> bool:
        if not self._flag_value("HMM_TERTIARY_ENABLED"):
            return False
        if not self._hmm_detector_tertiary or self._hmm_numpy is None:
            return False
        self._push_private_features_to_hmm_detectors()

        now_ts = float(now if now is not None else _now())
        retry_sec = max(
            60.0,
            float(
                getattr(
                    config,
                    "HMM_TERTIARY_SYNC_INTERVAL_SEC",
                    getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                )
            ),
        )
        is_trained = bool(getattr(self._hmm_detector_tertiary, "_trained", False))
        if (
            (not is_trained)
            and (now_ts - self._hmm_last_train_attempt_ts_tertiary) < retry_sec
            and reason != "startup"
        ):
            return False

        self._hmm_last_train_attempt_ts_tertiary = now_ts
        interval_min = max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)))
        target_candles = max(1, int(getattr(config, "HMM_TERTIARY_TRAINING_CANDLES", 500)))
        min_train_samples = max(1, int(getattr(config, "HMM_TERTIARY_MIN_TRAIN_SAMPLES", 150)))
        closes, volumes = self._fetch_training_candles(
            count=target_candles,
            interval_min=interval_min,
        )

        bootstrap_used = False
        if min(len(closes), len(volumes)) < min_train_samples:
            boot_closes, boot_volumes = self._fetch_bootstrap_tertiary_candles(
                target_candles=target_candles,
            )
            if min(len(boot_closes), len(boot_volumes)) > min(len(closes), len(volumes)):
                closes, volumes = boot_closes, boot_volumes
                bootstrap_used = True

        self._update_hmm_training_depth(
            current_candles=min(len(closes), len(volumes)),
            tertiary=True,
            target_candles=target_candles,
            min_train_samples=min_train_samples,
            interval_min=interval_min,
            now=now_ts,
        )
        if not closes or not volumes:
            self._hmm_state_tertiary["error"] = "no_training_candles"
            self._refresh_hmm_state_from_detector(tertiary=True)
            return False

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            ok = bool(self._hmm_detector_tertiary.train(closes_arr, volumes_arr))
        except Exception as e:
            logger.warning("Tertiary HMM train failed (%s): %s", reason, e)
            self._hmm_state_tertiary["error"] = f"train_failed:{e}"
            self._refresh_hmm_state_from_detector(tertiary=True)
            return False

        if ok:
            self._hmm_state_tertiary["error"] = ""
            src = "bootstrap_15m" if bootstrap_used else "native_1h"
            logger.info("Tertiary HMM trained (%s, %s) with %d candles", reason, src, len(closes))
        else:
            self._hmm_state_tertiary["error"] = "train_skipped_or_failed"
        self._refresh_hmm_state_from_detector(tertiary=True)
        self._update_hmm_tertiary_transition(now_ts)
        return ok

    def _update_hmm_tertiary(self, now: float) -> None:
        if not self._flag_value("HMM_TERTIARY_ENABLED"):
            self._update_hmm_tertiary_transition(now)
            return
        if not self._hmm_detector_tertiary or self._hmm_numpy is None:
            self._refresh_hmm_state_from_detector(tertiary=True)
            self._update_hmm_tertiary_transition(now)
            return
        self._push_private_features_to_hmm_detectors()

        trained = bool(getattr(self._hmm_detector_tertiary, "_trained", False))
        if not trained:
            self._train_hmm_tertiary(now=now, reason="startup")
            trained = bool(getattr(self._hmm_detector_tertiary, "_trained", False))
        else:
            try:
                if bool(self._hmm_detector_tertiary.needs_retrain()):
                    self._train_hmm_tertiary(now=now, reason="periodic")
            except Exception as e:
                logger.debug("Tertiary HMM retrain check failed: %s", e)

        if not trained:
            self._refresh_hmm_state_from_detector(tertiary=True)
            self._update_hmm_tertiary_transition(now)
            return

        interval_min = max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)))
        closes, volumes = self._fetch_recent_candles(
            count=int(getattr(config, "HMM_TERTIARY_RECENT_CANDLES", 30)),
            interval_min=interval_min,
        )
        if not closes or not volumes:
            self._refresh_hmm_state_from_detector(tertiary=True)
            self._update_hmm_tertiary_transition(now)
            return

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            self._hmm_detector_tertiary.update(closes_arr, volumes_arr)
            self._hmm_state_tertiary["error"] = ""
        except Exception as e:
            logger.warning("Tertiary HMM inference failed: %s", e)
            self._hmm_state_tertiary["error"] = f"inference_failed:{e}"
        finally:
            self._refresh_hmm_state_from_detector(tertiary=True)
            self._update_hmm_tertiary_transition(now)

    @staticmethod
    def _normalize_consensus_weights(w1_raw: Any, w15_raw: Any) -> tuple[float, float]:
        try:
            w1 = float(w1_raw)
        except (TypeError, ValueError):
            w1 = 0.0
        try:
            w15 = float(w15_raw)
        except (TypeError, ValueError):
            w15 = 0.0
        if not isfinite(w1):
            w1 = 0.0
        if not isfinite(w15):
            w15 = 0.0
        w1 = max(0.0, w1)
        w15 = max(0.0, w15)
        total = w1 + w15
        if total <= 1e-9:
            return 0.3, 0.7
        return w1 / total, w15 / total

    def _compute_hmm_consensus(self) -> dict[str, Any]:
        primary = dict(self._hmm_state or self._hmm_default_state())
        secondary = dict(
            self._hmm_state_secondary
            or self._hmm_default_state(
                enabled=self._flag_value("HMM_ENABLED")
                and self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"),
                interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
            )
        )
        multi_enabled = self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED")
        source_mode = self._hmm_source_mode()
        primary_ready = bool(primary.get("available")) and bool(primary.get("trained"))
        primary_probs = self._hmm_prob_triplet(primary)
        secondary_probs = self._hmm_prob_triplet(secondary)

        if not primary_ready:
            return {
                "enabled": self._flag_value("HMM_ENABLED"),
                "available": bool(primary.get("available")),
                "trained": False,
                "interval_min": int(primary.get("interval_min", getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
                "regime": "RANGING",
                "regime_id": 1,
                "confidence": 0.0,
                "bias_signal": 0.0,
                "effective_regime": "RANGING",
                "effective_confidence": 0.0,
                "effective_bias": 0.0,
                "agreement": "primary_untrained",
                "source_mode": source_mode,
                "multi_timeframe": bool(multi_enabled),
                "primary": primary,
                "secondary": secondary,
                "consensus_probabilities": {
                    "bearish": 0.0,
                    "ranging": 1.0,
                    "bullish": 0.0,
                },
                "last_update_ts": float(primary.get("last_update_ts", 0.0) or 0.0),
                "last_train_ts": float(primary.get("last_train_ts", 0.0) or 0.0),
                "blend_factor": float(primary.get("blend_factor", getattr(config, "HMM_BLEND_WITH_TREND", 0.5))),
                "error": str(primary.get("error", "")),
            }

        if not multi_enabled:
            out = dict(primary)
            out.update({
                "agreement": "primary_only",
                "source_mode": source_mode,
                "multi_timeframe": False,
                "primary": primary,
                "secondary": secondary,
                "consensus_probabilities": {
                    "bearish": primary_probs[0],
                    "ranging": primary_probs[1],
                    "bullish": primary_probs[2],
                },
                "effective_regime": str(out.get("regime", "RANGING") or "RANGING"),
                "effective_confidence": float(out.get("confidence", 0.0) or 0.0),
                "effective_bias": float(out.get("bias_signal", 0.0) or 0.0),
            })
            return out

        if not bool(secondary.get("available")) or not bool(secondary.get("trained")):
            out = dict(primary)
            out.update({
                "agreement": "primary_only",
                "source_mode": source_mode,
                "multi_timeframe": True,
                "primary": primary,
                "secondary": secondary,
                "consensus_probabilities": {
                    "bearish": primary_probs[0],
                    "ranging": primary_probs[1],
                    "bullish": primary_probs[2],
                },
                "effective_regime": str(out.get("regime", "RANGING") or "RANGING"),
                "effective_confidence": float(out.get("confidence", 0.0) or 0.0),
                "effective_bias": float(out.get("bias_signal", 0.0) or 0.0),
            })
            return out

        regime_1m = str(primary.get("regime", "RANGING") or "RANGING").upper()
        regime_15m = str(secondary.get("regime", "RANGING") or "RANGING").upper()
        valid_regimes = {"BULLISH", "BEARISH", "RANGING"}
        if regime_1m not in valid_regimes:
            regime_1m = "RANGING"
        if regime_15m not in valid_regimes:
            regime_15m = "RANGING"

        conf_1m = max(0.0, min(1.0, float(primary.get("confidence", 0.0) or 0.0)))
        conf_15m = max(0.0, min(1.0, float(secondary.get("confidence", 0.0) or 0.0)))
        bias_1m = float(primary.get("bias_signal", 0.0) or 0.0)
        bias_15m = float(secondary.get("bias_signal", 0.0) or 0.0)
        dampen = max(0.0, min(1.0, float(getattr(config, "CONSENSUS_DAMPEN_FACTOR", 0.5))))
        w1, w15 = self._normalize_consensus_weights(
            getattr(config, "CONSENSUS_1M_WEIGHT", 0.3),
            getattr(config, "CONSENSUS_15M_WEIGHT", 0.7),
        )
        consensus_probs = [
            w1 * primary_probs[i] + w15 * secondary_probs[i]
            for i in range(3)
        ]

        agreement = "conflict"
        if regime_1m == regime_15m:
            effective_confidence = max(conf_1m, conf_15m)
            agreement = "full"
        elif regime_15m == "RANGING":
            effective_confidence = 0.0
            agreement = "15m_neutral"
        elif regime_1m == "RANGING":
            effective_confidence = conf_15m * dampen
            agreement = "1m_cooling"
        else:
            effective_confidence = 0.0
            agreement = "conflict"

        if agreement == "full":
            effective_bias = w1 * bias_1m + w15 * bias_15m
        elif agreement == "1m_cooling":
            effective_bias = bias_15m * dampen
        else:
            effective_bias = 0.0

        effective_confidence = max(0.0, min(1.0, float(effective_confidence)))
        effective_bias = max(-1.0, min(1.0, float(effective_bias)))
        tier1_conf = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER1_CONFIDENCE", 0.20))))
        if effective_confidence < tier1_conf:
            effective_regime = "RANGING"
        elif agreement == "full":
            effective_regime = regime_1m
        elif agreement == "1m_cooling":
            effective_regime = regime_15m
        else:
            effective_regime = "RANGING"

        return {
            "enabled": self._flag_value("HMM_ENABLED"),
            "available": bool(primary.get("available")) and bool(secondary.get("available")),
            "trained": bool(primary.get("trained")) and bool(secondary.get("trained")),
            "interval_min": int(primary.get("interval_min", getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
            "regime": effective_regime,
            "regime_id": {"BEARISH": 0, "RANGING": 1, "BULLISH": 2}.get(effective_regime, 1),
            "confidence": effective_confidence,
            "bias_signal": effective_bias,
            "effective_regime": effective_regime,
            "effective_confidence": effective_confidence,
            "effective_bias": effective_bias,
            "agreement": agreement,
            "weights": {"w1m": w1, "w15m": w15},
            "source_mode": source_mode,
            "multi_timeframe": True,
            "primary": primary,
            "secondary": secondary,
            "consensus_probabilities": {
                "bearish": consensus_probs[0],
                "ranging": consensus_probs[1],
                "bullish": consensus_probs[2],
            },
            "last_update_ts": max(
                float(primary.get("last_update_ts", 0.0) or 0.0),
                float(secondary.get("last_update_ts", 0.0) or 0.0),
            ),
            "last_train_ts": max(
                float(primary.get("last_train_ts", 0.0) or 0.0),
                float(secondary.get("last_train_ts", 0.0) or 0.0),
            ),
            "blend_factor": float(primary.get("blend_factor", getattr(config, "HMM_BLEND_WITH_TREND", 0.5))),
            "error": "",
        }

    def _update_hmm(self, now: float) -> None:
        multi_enabled = self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED")
        tertiary_enabled = self._flag_value("HMM_TERTIARY_ENABLED")
        self._push_private_features_to_hmm_detectors()

        if not self._flag_value("HMM_ENABLED"):
            if tertiary_enabled:
                self._update_hmm_tertiary(now)
            else:
                self._update_hmm_tertiary_transition(now)
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)
            return
        if not self._hmm_detector or self._hmm_numpy is None:
            if multi_enabled:
                self._update_hmm_secondary(now)
            if tertiary_enabled:
                self._update_hmm_tertiary(now)
            else:
                self._update_hmm_tertiary_transition(now)
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)
            return

        trained = bool(getattr(self._hmm_detector, "_trained", False))
        if not trained:
            self._train_hmm(now=now, reason="startup")
            trained = bool(getattr(self._hmm_detector, "_trained", False))
        else:
            try:
                if bool(self._hmm_detector.needs_retrain()):
                    self._train_hmm(now=now, reason="periodic")
            except Exception as e:
                logger.debug("HMM retrain check failed: %s", e)

        if not trained:
            self._refresh_hmm_state_from_detector()
            if multi_enabled:
                self._update_hmm_secondary(now)
            if tertiary_enabled:
                self._update_hmm_tertiary(now)
            else:
                self._update_hmm_tertiary_transition(now)
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)
            return

        interval_min = max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
        closes, volumes = self._fetch_recent_candles(
            count=int(getattr(config, "HMM_RECENT_CANDLES", 100)),
            interval_min=interval_min,
        )
        if not closes or not volumes:
            self._refresh_hmm_state_from_detector()
            if multi_enabled:
                self._update_hmm_secondary(now)
            if tertiary_enabled:
                self._update_hmm_tertiary(now)
            else:
                self._update_hmm_tertiary_transition(now)
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)
            return

        try:
            closes_arr = self._hmm_numpy.asarray(closes, dtype=float)
            volumes_arr = self._hmm_numpy.asarray(volumes, dtype=float)
            self._hmm_detector.update(closes_arr, volumes_arr)
            self._hmm_state["error"] = ""
        except Exception as e:
            logger.warning("HMM inference failed: %s", e)
            self._hmm_state["error"] = f"inference_failed:{e}"
        finally:
            self._refresh_hmm_state_from_detector()
            if multi_enabled:
                self._update_hmm_secondary(now)
            if tertiary_enabled:
                self._update_hmm_tertiary(now)
            else:
                self._update_hmm_tertiary_transition(now)
            self._hmm_consensus = self._compute_hmm_consensus()
            self._record_regime_history_sample(now)

    def _hmm_status_payload(self) -> dict[str, Any]:
        primary = dict(
            self._hmm_state
            or self._hmm_default_state(
                enabled=self._flag_value("HMM_ENABLED"),
                interval_min=max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
            )
        )
        secondary = dict(
            self._hmm_state_secondary
            or self._hmm_default_state(
                enabled=self._flag_value("HMM_ENABLED")
                and self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"),
                interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
            )
        )
        tertiary = dict(
            self._hmm_state_tertiary
            or self._hmm_default_state(
                enabled=self._flag_value("HMM_ENABLED")
                and self._flag_value("HMM_TERTIARY_ENABLED"),
                interval_min=max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60))),
            )
        )
        training_depth_primary = dict(
            self._hmm_training_depth
            or self._hmm_training_depth_default(state_key="primary")
        )
        training_depth_secondary = dict(
            self._hmm_training_depth_secondary
            or self._hmm_training_depth_default(state_key="secondary")
        )
        training_depth_tertiary = dict(
            self._hmm_training_depth_tertiary
            or self._hmm_training_depth_default(state_key="tertiary")
        )
        consensus = dict(self._hmm_consensus or self._compute_hmm_consensus())
        source_mode = self._hmm_source_mode()
        source = dict(self._policy_hmm_source() or primary)
        source_interval = int(source.get("interval_min", primary.get("interval_min", 1)) or 1)
        secondary_interval = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        tertiary_interval = max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)))
        if source_interval == tertiary_interval and self._flag_value("HMM_TERTIARY_ENABLED"):
            training_depth = training_depth_tertiary
        elif source_interval == secondary_interval and self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"):
            training_depth = training_depth_secondary
        else:
            training_depth = training_depth_primary
        raw_probs = source.get("probabilities", primary.get("probabilities"))
        probs = (
            dict(raw_probs)
            if isinstance(raw_probs, dict)
            else {"bearish": 0.0, "ranging": 1.0, "bullish": 0.0}
        )
        confidence_raw = max(0.0, min(1.0, float(source.get("confidence", 0.0) or 0.0)))
        confidence_modifier, confidence_modifier_source = self._hmm_confidence_modifier_for_source(source)
        confidence_effective = max(0.0, min(1.0, confidence_raw * confidence_modifier))
        out = {
            "enabled": bool(source.get("enabled", False)),
            "available": bool(source.get("available", False)),
            "trained": bool(source.get("trained", False)),
            "interval_min": int(source.get("interval_min", primary.get("interval_min", 1))),
            "regime": str(source.get("regime", "RANGING")),
            "regime_id": int(source.get("regime_id", 1)),
            "confidence": confidence_raw,
            "confidence_raw": confidence_raw,
            "confidence_effective": confidence_effective,
            "confidence_modifier": float(confidence_modifier),
            "confidence_modifier_source": str(confidence_modifier_source),
            "bias_signal": float(source.get("bias_signal", 0.0)),
            "probabilities": probs,
            "observation_count": int(source.get("observation_count", 0)),
            "blend_factor": float(primary.get("blend_factor", getattr(config, "HMM_BLEND_WITH_TREND", 0.5))),
            "last_update_ts": float(source.get("last_update_ts", 0.0)),
            "last_train_ts": float(source.get("last_train_ts", 0.0)),
            "error": str(source.get("error", "")),
            "source_mode": source_mode,
            "multi_timeframe": self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"),
            "agreement": str(consensus.get("agreement", "primary_only")),
            "primary": primary,
            "secondary": secondary,
            "tertiary": tertiary,
            "consensus": consensus,
            "tertiary_transition": dict(self._hmm_tertiary_transition or {}),
            "training_depth": training_depth,
            "training_depth_primary": training_depth_primary,
            "training_depth_secondary": training_depth_secondary,
            "training_depth_tertiary": training_depth_tertiary,
            "regime_history_30m": list(self._regime_history_30m),
        }
        if bool(getattr(config, "BELIEF_STATE_IN_STATUS", True)):
            out["belief_state"] = self._belief_state.to_status_dict()
        return out

    def _manual_regime_override(self) -> tuple[str | None, float]:
        raw = str(getattr(config, "REGIME_MANUAL_OVERRIDE", "") or "").strip().upper()
        if raw in {"BULLISH", "BEARISH"}:
            conf = max(0.0, min(1.0, float(getattr(config, "REGIME_MANUAL_CONFIDENCE", 0.75))))
            return raw, conf
        return None, 0.0

    @staticmethod
    def _tier_direction(
        tier: int,
        regime: str,
        bias: float,
        suppressed_side: str | None = None,
    ) -> str:
        use_tier = max(0, min(2, int(tier)))
        if use_tier <= 0:
            return "symmetric"
        if use_tier >= 2:
            if suppressed_side == "A":
                return "long_bias"
            if suppressed_side == "B":
                return "short_bias"
        reg = str(regime or "RANGING").upper()
        if reg == "BULLISH" or float(bias) > 0.0:
            return "long_bias"
        if reg == "BEARISH" or float(bias) < 0.0:
            return "short_bias"
        return "symmetric"

    @staticmethod
    def _classify_ai_regime_agreement(
        ai_tier: int,
        ai_direction: str,
        mechanical_tier: int,
        mechanical_direction: str,
    ) -> str:
        ai_t = max(0, min(2, int(ai_tier)))
        mech_t = max(0, min(2, int(mechanical_tier)))
        ai_dir = str(ai_direction or "symmetric").strip().lower()
        mech_dir = str(mechanical_direction or "symmetric").strip().lower()
        if ai_t == mech_t and ai_dir == mech_dir:
            return "agree"
        if ai_t > mech_t:
            return "ai_upgrade"
        if ai_t < mech_t:
            return "ai_downgrade"
        return "ai_flip"

    def _ai_regime_history_limit(self) -> int:
        return max(1, int(getattr(config, "AI_REGIME_HISTORY_SIZE", 12)))

    @staticmethod
    def _hmm_prob_triplet(source: dict[str, Any]) -> list[float]:
        raw = source.get("probabilities", {})
        if isinstance(raw, dict):
            try:
                return [
                    float(raw.get("bearish", 0.0) or 0.0),
                    float(raw.get("ranging", 1.0) or 0.0),
                    float(raw.get("bullish", 0.0) or 0.0),
                ]
            except (TypeError, ValueError):
                return [0.0, 1.0, 0.0]
        if isinstance(raw, (list, tuple)) and len(raw) >= 3:
            try:
                return [float(raw[0]), float(raw[1]), float(raw[2])]
            except (TypeError, ValueError):
                return [0.0, 1.0, 0.0]
        return [0.0, 1.0, 0.0]

    @staticmethod
    def _resample_candles_from_lower_interval(
        rows: list[dict[str, float | int | None]],
        *,
        group_size: int,
        base_interval_sec: float,
    ) -> list[dict[str, float | int | None]]:
        if group_size <= 1 or not rows:
            return []

        step_sec = max(1.0, float(base_interval_sec))
        out: list[dict[str, float | int | None]] = []
        block: list[dict[str, float | int | None]] = []
        prev_ts = None

        for row in sorted(rows, key=lambda r: float(r.get("time", 0.0) or 0.0)):
            try:
                ts = float(row.get("time", 0.0) or 0.0)
                o = float(row.get("open", 0.0) or 0.0)
                h = float(row.get("high", 0.0) or 0.0)
                l = float(row.get("low", 0.0) or 0.0)
                c = float(row.get("close", 0.0) or 0.0)
                v = float(row.get("volume", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if ts <= 0 or min(o, h, l, c) <= 0 or v < 0:
                continue

            contiguous = (
                prev_ts is not None
                and abs(ts - (float(prev_ts) + step_sec)) <= 1.0
            )
            if block and not contiguous:
                block = []
            block.append(row)
            prev_ts = ts

            if len(block) < group_size:
                continue

            try:
                b_open = float(block[0].get("open", 0.0) or 0.0)
                b_close = float(block[-1].get("close", 0.0) or 0.0)
                b_high = max(float(x.get("high", 0.0) or 0.0) for x in block)
                b_low = min(float(x.get("low", 0.0) or 0.0) for x in block)
                b_volume = sum(float(x.get("volume", 0.0) or 0.0) for x in block)
            except Exception:
                block = []
                continue

            if min(b_open, b_high, b_low, b_close) > 0 and b_volume >= 0:
                out.append(
                    {
                        "time": float(block[0].get("time", 0.0) or 0.0),
                        "open": b_open,
                        "high": b_high,
                        "low": b_low,
                        "close": b_close,
                        "volume": b_volume,
                        "trade_count": None,
                    }
                )
            block = []

        return out

    def _fetch_bootstrap_tertiary_candles(self, *, target_candles: int) -> tuple[list[float], list[float]]:
        tertiary_interval = max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)))
        secondary_interval = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        if tertiary_interval <= secondary_interval:
            return [], []
        if tertiary_interval % secondary_interval != 0:
            return [], []

        group_size = max(1, tertiary_interval // secondary_interval)
        base_rows_target = max(1, int(target_candles)) * group_size
        rows = self._load_recent_ohlcv_rows(
            count=base_rows_target,
            interval_min=secondary_interval,
        )
        if not rows:
            return [], []

        resampled = self._resample_candles_from_lower_interval(
            rows,
            group_size=group_size,
            base_interval_sec=float(secondary_interval * 60),
        )
        if len(resampled) > int(target_candles):
            resampled = resampled[-int(target_candles):]
        return self._extract_close_volume(resampled)

    def _update_hmm_tertiary_transition(self, now: float) -> None:
        state = dict(self._hmm_state_tertiary or {})
        regime = str(state.get("regime", "RANGING") or "RANGING").upper()
        if regime not in {"BEARISH", "RANGING", "BULLISH"}:
            regime = "RANGING"
        confidence = max(0.0, min(1.0, float(state.get("confidence", 0.0) or 0.0)))
        is_ready = bool(state.get("available")) and bool(state.get("trained"))

        transition = dict(self._hmm_tertiary_transition or {})
        from_regime = str(transition.get("from_regime", regime) or regime).upper()
        to_regime = str(transition.get("to_regime", regime) or regime).upper()
        changed_at = float(transition.get("changed_at", 0.0) or 0.0)
        confirmation_count = int(transition.get("confirmation_count", 0) or 0)

        if not is_ready:
            self._hmm_tertiary_transition = {
                "from_regime": regime,
                "to_regime": regime,
                "transition_age_sec": 0.0,
                "confidence": confidence,
                "confirmed": False,
                "confirmation_count": 0,
                "changed_at": 0.0,
            }
            return

        if changed_at <= 0.0:
            changed_at = float(state.get("last_update_ts", 0.0) or now)
            from_regime = regime
            to_regime = regime
            confirmation_count = 1

        if regime != to_regime:
            from_regime = to_regime
            to_regime = regime
            changed_at = float(state.get("last_update_ts", 0.0) or now)
            confirmation_count = 1
        else:
            interval_sec = max(
                60.0,
                float(max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60))) * 60),
            )
            age_sec = max(0.0, float(now) - changed_at)
            confirmation_count = max(1, int(age_sec // interval_sec) + 1)

        transition_age_sec = max(0.0, float(now) - changed_at)
        confirm_needed = max(1, int(getattr(config, "ACCUM_CONFIRMATION_CANDLES", 2)))
        confirmed = bool(from_regime != to_regime and confirmation_count >= confirm_needed)
        self._hmm_tertiary_transition = {
            "from_regime": from_regime,
            "to_regime": to_regime,
            "transition_age_sec": transition_age_sec,
            "confidence": confidence,
            "confirmed": confirmed,
            "confirmation_count": int(confirmation_count),
            "changed_at": changed_at,
        }

    def _fill_rate_1h(self, now: float) -> int:
        cutoff = float(now) - 3600.0
        count = 0
        for slot in self.slots.values():
            for cyc in slot.state.completed_cycles:
                try:
                    exit_time = float(getattr(cyc, "exit_time", 0.0) or 0.0)
                except (TypeError, ValueError):
                    exit_time = 0.0
                if exit_time >= cutoff:
                    count += 1
        return int(count)

    def _build_ai_regime_context(self, now: float) -> dict[str, Any]:
        primary = dict(self._hmm_state or self._hmm_default_state())
        secondary = dict(
            self._hmm_state_secondary
            or self._hmm_default_state(
                enabled=self._flag_value("HMM_ENABLED")
                and self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"),
                interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
            )
        )
        tertiary = dict(
            self._hmm_state_tertiary
            or self._hmm_default_state(
                enabled=self._flag_value("HMM_ENABLED")
                and self._flag_value("HMM_TERTIARY_ENABLED"),
                interval_min=max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60))),
            )
        )
        consensus = dict(self._hmm_consensus or self._compute_hmm_consensus())
        source = dict(self._policy_hmm_source() or primary)
        source_interval = int(source.get("interval_min", primary.get("interval_min", 1)) or 1)
        secondary_interval = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
        tertiary_interval = max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)))
        if source_interval == tertiary_interval and self._flag_value("HMM_TERTIARY_ENABLED"):
            depth = dict(self._hmm_training_depth_tertiary or self._hmm_training_depth_default(state_key="tertiary"))
        elif source_interval == secondary_interval and self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED"):
            depth = dict(self._hmm_training_depth_secondary or self._hmm_training_depth_default(state_key="secondary"))
        else:
            depth = dict(self._hmm_training_depth or self._hmm_training_depth_default(state_key="primary"))
        confidence_modifier, _mod_source = self._hmm_confidence_modifier_for_source(source)

        transmat = None
        transmat_1h = None
        try:
            if self._hmm_detector is not None:
                transmat = getattr(self._hmm_detector, "transmat", None)
        except Exception:
            transmat = None
        try:
            if self._hmm_detector_tertiary is not None:
                transmat_1h = getattr(self._hmm_detector_tertiary, "transmat", None)
        except Exception:
            transmat_1h = None

        capacity = self._compute_capacity_health(now)
        safe_cap = max(1, int(capacity.get("open_orders_safe_cap") or 1))
        headroom_count = int(capacity.get("open_order_headroom") or 0)
        headroom_pct = max(0.0, min(100.0, (float(headroom_count) / float(safe_cap)) * 100.0))

        trend_score = float(self._trend_score)
        dead_zone = abs(float(getattr(config, "TREND_DEAD_ZONE", 0.001)))
        if trend_score > dead_zone:
            directional_trend = "bullish"
        elif trend_score < -dead_zone:
            directional_trend = "bearish"
        else:
            directional_trend = "neutral"

        if self._recovery_orders_enabled():
            recovery_order_count = sum(len(slot.state.recovery_orders) for slot in self.slots.values())
        else:
            recovery_order_count = 0
        throughput_payload = self._throughput.status_payload() if self._throughput is not None else {}
        bull_edge = 0.0
        bear_edge = 0.0
        range_edge = 0.0
        throughput_active_regime = "ranging"
        throughput_multiplier = 1.0
        throughput_age_pressure = 1.0
        throughput_median_fill_sec = 0.0
        throughput_sufficient_data_regimes: list[str] = []
        if isinstance(throughput_payload, dict):
            def _bucket_mult(name: str) -> float:
                try:
                    return float((throughput_payload.get(name) or {}).get("multiplier", 1.0) or 1.0)
                except Exception:
                    return 1.0

            bull_edge = ((_bucket_mult("bullish_A") + _bucket_mult("bullish_B")) * 0.5) - 1.0
            bear_edge = ((_bucket_mult("bearish_A") + _bucket_mult("bearish_B")) * 0.5) - 1.0
            range_edge = ((_bucket_mult("ranging_A") + _bucket_mult("ranging_B")) * 0.5) - 1.0
            throughput_active_regime = str(throughput_payload.get("active_regime", "ranging") or "ranging").strip().lower()
            if throughput_active_regime not in {"bearish", "ranging", "bullish"}:
                throughput_active_regime = "ranging"
            throughput_multiplier = self._manifold_active_throughput_multiplier(throughput_payload)
            try:
                throughput_age_pressure = float(throughput_payload.get("age_pressure", 1.0) or 1.0)
            except Exception:
                throughput_age_pressure = 1.0
            throughput_age_pressure = max(0.0, min(1.0, throughput_age_pressure))
            median_rows: list[float] = []
            for bucket_name in (
                f"{throughput_active_regime}_A",
                f"{throughput_active_regime}_B",
                "aggregate",
            ):
                row = throughput_payload.get(bucket_name) or {}
                try:
                    value = float(row.get("median_fill_sec", 0.0) or 0.0)
                except Exception:
                    value = 0.0
                if value > 0.0:
                    median_rows.append(value)
            if median_rows:
                throughput_median_fill_sec = float(median(median_rows))
            for bucket_name in (
                "bearish_A",
                "bearish_B",
                "ranging_A",
                "ranging_B",
                "bullish_A",
                "bullish_B",
            ):
                row = throughput_payload.get(bucket_name) or {}
                if bool(row.get("sufficient_data", False)):
                    throughput_sufficient_data_regimes.append(bucket_name)

        free_usd, free_doge = self._available_free_balances(prefer_fresh=False)
        scoreboard = self._compute_doge_bias_scoreboard() or {}
        idle_usd = max(0.0, float(scoreboard.get("idle_usd", 0.0) or 0.0))
        idle_usd_pct = max(0.0, min(100.0, float(scoreboard.get("idle_usd_pct", 0.0) or 0.0)))
        util_ratio = max(0.0, min(1.0, 1.0 - (idle_usd_pct / 100.0)))

        ai_opinion = dict(self._ai_regime_opinion or {})
        accum_signal = str(ai_opinion.get("accumulation_signal", "hold") or "hold").strip().lower()
        if accum_signal not in {"accumulate_doge", "hold", "accumulate_usd"}:
            accum_signal = "hold"
        accum_conviction = max(0, min(100, int(ai_opinion.get("accumulation_conviction", 0) or 0)))
        accum_status = self._accumulation_status_payload(now=now)
        accum_state = str(accum_status.get("state", "IDLE") or "IDLE").strip().upper() or "IDLE"
        accum_active = bool(accum_status.get("active", False))
        accum_budget_used = max(0.0, float(accum_status.get("spent_usd", 0.0) or 0.0))
        accum_budget_remaining = max(0.0, float(accum_status.get("budget_remaining_usd", 0.0) or 0.0))
        accum_cooldown_remaining = max(0, int(accum_status.get("cooldown_remaining_sec", 0) or 0))

        manifold_components = getattr(self._manifold_score, "components", None)
        manifold_history = list(self._manifold_history)
        manifold_trend = "stable"
        history_sparkline = [float(row[1]) for row in manifold_history]
        if history_sparkline:
            try:
                manifold_trend = str(
                    bayesian_engine.ev_trend(
                        history_sparkline,
                        window=min(4, len(history_sparkline)),
                    )
                )
            except Exception:
                manifold_trend = "stable"
        if manifold_trend not in {"rising", "falling", "stable"}:
            manifold_trend = "stable"
        mts_30m_ago: float | None = None
        if manifold_history:
            cutoff = float(now) - max(60.0, float(self._regime_history_window_sec))
            mts_30m_ago = float(manifold_history[0][1])
            for row in manifold_history:
                row_ts = float(row[0])
                row_mts = float(row[1])
                if row_ts <= cutoff:
                    mts_30m_ago = row_mts
                    continue
                break

        self_heal_payload = self._self_healing_status_payload(now)
        age_bands = {"fresh": 0, "aging": 0, "stale": 0, "stuck": 0, "write_off": 0}
        subsidy_balance = 0.0
        subsidy_needed = 0.0
        churner_enabled = bool(self._churner_enabled())
        churner_active_slots = int(self._churner_active_slot_count())
        churner_reserve = float(self._churner_reserve_available_usd)
        if isinstance(self_heal_payload, dict):
            raw_bands = self_heal_payload.get("age_bands")
            if isinstance(raw_bands, dict):
                for key in age_bands.keys():
                    age_bands[key] = max(0, int(raw_bands.get(key, 0) or 0))
            subsidy = self_heal_payload.get("subsidy")
            if isinstance(subsidy, dict):
                subsidy_balance = max(0.0, float(subsidy.get("balance", 0.0) or 0.0))
                subsidy_needed = max(0.0, float(subsidy.get("pending_needed", 0.0) or 0.0))
            churner = self_heal_payload.get("churner")
            if isinstance(churner, dict):
                churner_enabled = bool(churner.get("enabled", churner_enabled))
                churner_active_slots = max(0, int(churner.get("active_slots", churner_active_slots) or churner_active_slots))
                churner_reserve = max(0.0, float(churner.get("reserve_available_usd", churner_reserve) or churner_reserve))

        total_open_positions = 0
        avg_distance_pct = 0.0
        distance_samples: list[float] = []
        if self._position_ledger_enabled():
            open_positions = self._position_ledger.get_open_positions()
            total_open_positions = int(len(open_positions))
            for pos in open_positions:
                try:
                    sid = int(pos.get("slot_id", -1))
                except (TypeError, ValueError):
                    continue
                slot = self.slots.get(sid)
                if slot is None:
                    continue
                live = self._find_live_exit_for_position(pos)
                if live is None:
                    continue
                _local_id, order = live
                market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
                if market <= 0.0:
                    continue
                exit_px = float(pos.get("current_exit_price", order.price) or order.price)
                distance_samples.append(abs(exit_px - market) / market * 100.0)
        if distance_samples:
            avg_distance_pct = float(sum(distance_samples) / len(distance_samples))

        trade_beliefs_payload = self._trade_beliefs_status_payload()
        negative_ev_count = max(0, int(trade_beliefs_payload.get("exits_with_negative_ev", 0) or 0))
        slot_vintage = self._slot_vintage_metrics_locked(now)

        return {
            "hmm_primary": {
                "regime": str(primary.get("regime", "RANGING")),
                "confidence": float(primary.get("confidence", 0.0) or 0.0),
                "bias_signal": float(primary.get("bias_signal", 0.0) or 0.0),
                "probabilities": self._hmm_prob_triplet(primary),
            },
            "hmm_secondary": {
                "regime": str(secondary.get("regime", "RANGING")),
                "confidence": float(secondary.get("confidence", 0.0) or 0.0),
                "bias_signal": float(secondary.get("bias_signal", 0.0) or 0.0),
                "probabilities": self._hmm_prob_triplet(secondary),
            },
            "hmm_tertiary": {
                "regime": str(tertiary.get("regime", "RANGING")),
                "confidence": float(tertiary.get("confidence", 0.0) or 0.0),
                "bias_signal": float(tertiary.get("bias_signal", 0.0) or 0.0),
                "probabilities": self._hmm_prob_triplet(tertiary),
                "transition": dict(self._hmm_tertiary_transition or {}),
            },
            "hmm_consensus": {
                "agreement": str(consensus.get("agreement", "primary_only")),
                "effective_regime": str(consensus.get("effective_regime", consensus.get("regime", "RANGING"))),
                "effective_confidence": float(consensus.get("effective_confidence", consensus.get("confidence", 0.0)) or 0.0),
                "effective_bias": float(consensus.get("effective_bias", consensus.get("bias_signal", 0.0)) or 0.0),
            },
            "bocpd": {
                "enabled": bool(self._bocpd is not None),
                "change_prob": float(self._bocpd_state.change_prob),
                "run_length_mode": int(self._bocpd_state.run_length_mode),
                "run_length_mode_prob": float(self._bocpd_state.run_length_mode_prob),
                "alert_active": bool(self._bocpd_state.alert_active),
            },
            "consensus_1h_weight": float(getattr(config, "CONSENSUS_1H_WEIGHT", 0.30)),
            "transition_matrix_1m": transmat,
            "transition_matrix_1h": transmat_1h,
            "training_quality": str(depth.get("quality_tier", "shallow")),
            "training_quality_1h": str(
                (self._hmm_training_depth_tertiary or {}).get("quality_tier", "shallow")
            ),
            "confidence_modifier": float(confidence_modifier),
            "regime_history_30m": list(self._regime_history_30m),
            "mechanical_tier": {
                "current": int(self._regime_mechanical_tier),
                "direction": str(self._regime_mechanical_direction),
                "since": int(float(self._regime_mechanical_since or 0.0)),
            },
            "operational": {
                "directional_trend": directional_trend,
                "trend_detected_at": int(float(self._trend_last_update_ts or 0.0)),
                "fill_rate_1h": int(self._fill_rate_1h(now)),
                "recovery_order_count": int(recovery_order_count),
                "capacity_headroom": float(headroom_pct),
                "capacity_band": str(capacity.get("status_band") or "normal"),
                # Backward-compatible key names; values now proxy throughput bias.
                "kelly_edge_bullish": float(bull_edge),
                "kelly_edge_bearish": float(bear_edge),
                "kelly_edge_ranging": float(range_edge),
            },
            "capital": {
                "free_usd": float(free_usd),
                "idle_usd": float(idle_usd),
                "idle_usd_pct": float(idle_usd_pct),
                "free_doge": float(free_doge),
                "util_ratio": float(util_ratio),
            },
            "accumulation": {
                "enabled": self._flag_value("ACCUM_ENABLED"),
                "state": str(accum_state),
                "active": bool(accum_active),
                "signal": str(accum_signal),
                "conviction": int(accum_conviction),
                "budget_used_usd": float(accum_budget_used),
                "budget_remaining_usd": float(accum_budget_remaining),
                "cooldown_remaining_sec": int(accum_cooldown_remaining),
            },
            "manifold": {
                "mts": float(max(0.0, min(1.0, float(getattr(self._manifold_score, "mts", 0.0) or 0.0)))),
                "band": str(getattr(self._manifold_score, "band", "disabled") or "disabled"),
                "components": {
                    "clarity": float(getattr(manifold_components, "regime_clarity", 0.0) if manifold_components else 0.0),
                    "stability": float(
                        getattr(manifold_components, "regime_stability", 0.0) if manifold_components else 0.0
                    ),
                    "throughput": float(
                        getattr(manifold_components, "throughput_efficiency", 0.0) if manifold_components else 0.0
                    ),
                    "coherence": float(
                        getattr(manifold_components, "signal_coherence", 0.0) if manifold_components else 0.0
                    ),
                },
                "trend": str(manifold_trend),
                "mts_30m_ago": None if mts_30m_ago is None else float(mts_30m_ago),
            },
            "positions": {
                "total_open": int(total_open_positions),
                "age_bands": age_bands,
                "stuck_capital_pct": float(max(0.0, float(slot_vintage.get("stuck_capital_pct", 0.0) or 0.0))),
                "avg_distance_pct": float(max(0.0, avg_distance_pct)),
                "negative_ev_count": int(negative_ev_count),
            },
            "throughput": {
                "active_regime": str(throughput_active_regime),
                "multiplier": float(max(0.0, min(2.0, throughput_multiplier))),
                "age_pressure": float(max(0.0, min(1.0, throughput_age_pressure))),
                "median_fill_sec": float(max(0.0, throughput_median_fill_sec)),
                "sufficient_data_regimes": throughput_sufficient_data_regimes,
            },
            "churner": {
                "enabled": bool(churner_enabled),
                "active_slots": int(churner_active_slots),
                "reserve_usd": float(max(0.0, churner_reserve)),
                "subsidy_balance": float(max(0.0, subsidy_balance)),
                "subsidy_needed": float(max(0.0, subsidy_needed)),
            },
        }

    def _accumulation_signal_conviction(self) -> tuple[str, int]:
        opinion = dict(self._ai_regime_opinion or {})
        signal = str(opinion.get("accumulation_signal", "hold") or "hold").strip().lower()
        if signal not in {"accumulate_doge", "hold", "accumulate_usd"}:
            signal = "hold"
        conviction = max(0, min(100, int(opinion.get("accumulation_conviction", 0) or 0)))
        return signal, conviction

    @staticmethod
    def _normalize_regime_label(value: Any, fallback: str = "RANGING") -> str:
        label = str(value or fallback).strip().upper()
        if label not in {"BEARISH", "RANGING", "BULLISH"}:
            return str(fallback).strip().upper()
        return label

    def _accumulation_trigger_label(self, from_regime: str, to_regime: str) -> str:
        from_norm = self._normalize_regime_label(from_regime, "RANGING").lower()
        to_norm = self._normalize_regime_label(to_regime, "RANGING").lower()
        return f"1h_{from_norm}_to_{to_norm}"

    def _accumulation_idle_budget(self) -> tuple[float, float]:
        free_usd, _free_doge = self._available_free_balances(prefer_fresh=False)
        scoreboard = self._compute_doge_bias_scoreboard() or {}
        idle_usd_raw = scoreboard.get("idle_usd", free_usd)
        try:
            idle_usd = max(0.0, float(idle_usd_raw))
        except (TypeError, ValueError):
            idle_usd = max(0.0, float(free_usd))
        reserve_usd = max(0.0, float(getattr(config, "ACCUM_RESERVE_USD", 50.0)))
        max_budget_usd = max(0.0, float(getattr(config, "ACCUM_MAX_BUDGET_USD", 50.0)))
        budget_usd = min(max_budget_usd, max(0.0, idle_usd - reserve_usd))
        return idle_usd, budget_usd

    def _clear_accumulation_live_state(self) -> None:
        self._accum_state = "IDLE"
        self._accum_direction = None
        self._accum_trigger_from_regime = "RANGING"
        self._accum_trigger_to_regime = "RANGING"
        self._accum_start_ts = 0.0
        self._accum_start_price = 0.0
        self._accum_spent_usd = 0.0
        self._accum_acquired_doge = 0.0
        self._accum_n_buys = 0
        self._accum_last_buy_ts = 0.0
        self._accum_budget_usd = 0.0
        self._accum_armed_at = 0.0
        self._accum_hold_streak = 0
        self._accum_manual_stop_requested = False

    def _arm_accumulation(
        self,
        now: float,
        *,
        from_regime: str,
        to_regime: str,
        budget_usd: float,
        idle_usd: float,
    ) -> None:
        self._accum_state = "ARMED"
        self._accum_direction = "doge"
        self._accum_trigger_from_regime = self._normalize_regime_label(from_regime, "RANGING")
        self._accum_trigger_to_regime = self._normalize_regime_label(to_regime, "RANGING")
        self._accum_armed_at = float(now)
        self._accum_start_ts = 0.0
        self._accum_start_price = 0.0
        self._accum_spent_usd = 0.0
        self._accum_acquired_doge = 0.0
        self._accum_n_buys = 0
        self._accum_last_buy_ts = 0.0
        self._accum_hold_streak = 0
        self._accum_manual_stop_requested = False
        self._accum_budget_usd = max(0.0, float(budget_usd))
        logger.info(
            "accumulation armed: trigger=%s budget=$%.4f idle_usd=$%.4f",
            self._accumulation_trigger_label(self._accum_trigger_from_regime, self._accum_trigger_to_regime),
            float(self._accum_budget_usd),
            float(idle_usd),
        )

    def _activate_accumulation(self, now: float) -> bool:
        if self.last_price <= 0:
            logger.debug("accumulation activation deferred: price unavailable")
            return False
        self._accum_state = "ACTIVE"
        self._accum_direction = "doge"
        self._accum_start_ts = float(now)
        self._accum_start_price = float(self.last_price)
        self._accum_hold_streak = 0
        if self._accum_budget_usd <= 0.0:
            _idle_usd, budget_now = self._accumulation_idle_budget()
            self._accum_budget_usd = max(0.0, float(budget_now))
        logger.info(
            "accumulation active: trigger=%s budget=$%.4f start_price=$%.6f",
            self._accumulation_trigger_label(self._accum_trigger_from_regime, self._accum_trigger_to_regime),
            float(self._accum_budget_usd),
            float(self._accum_start_price),
        )
        return True

    def _finalize_accumulation(
        self,
        *,
        now: float,
        terminal_state: str,
        reason: str,
        ai_signal: str,
        ai_conviction: int,
    ) -> None:
        terminal = str(terminal_state or "STOPPED").strip().upper()
        if terminal not in {"COMPLETED", "STOPPED"}:
            terminal = "STOPPED"
        spent_usd = max(0.0, float(self._accum_spent_usd))
        acquired_doge = max(0.0, float(self._accum_acquired_doge))
        start_ts = float(self._accum_start_ts or self._accum_armed_at or now)
        elapsed_sec = max(0.0, float(now) - start_ts)
        avg_price = (spent_usd / acquired_doge) if acquired_doge > 0 else None
        current_price = float(self.last_price or 0.0)
        current_drawdown_pct = None
        if self._accum_start_price > 0 and current_price > 0:
            current_drawdown_pct = ((current_price - float(self._accum_start_price)) / float(self._accum_start_price)) * 100.0

        summary = {
            "state": terminal,
            "reason": str(reason),
            "direction": str(self._accum_direction or "doge"),
            "trigger_from_regime": str(self._accum_trigger_from_regime),
            "trigger_to_regime": str(self._accum_trigger_to_regime),
            "trigger": self._accumulation_trigger_label(
                self._accum_trigger_from_regime,
                self._accum_trigger_to_regime,
            ),
            "start_ts": float(start_ts),
            "end_ts": float(now),
            "elapsed_sec": float(elapsed_sec),
            "start_price": (float(self._accum_start_price) if self._accum_start_price > 0 else None),
            "end_price": (float(current_price) if current_price > 0 else None),
            "budget_usd": float(max(0.0, float(self._accum_budget_usd))),
            "spent_usd": float(spent_usd),
            "acquired_doge": float(acquired_doge),
            "avg_price": (float(avg_price) if avg_price is not None else None),
            "n_buys": int(max(0, int(self._accum_n_buys))),
            "ai_signal": str(ai_signal),
            "ai_conviction": int(max(0, min(100, int(ai_conviction)))),
            "current_drawdown_pct": (
                float(current_drawdown_pct) if current_drawdown_pct is not None else None
            ),
        }
        self._accum_last_session_summary = summary
        self._accum_last_session_end_ts = float(now)
        self._accum_manual_stop_requested = False
        cooldown_sec = max(0.0, float(getattr(config, "ACCUM_COOLDOWN_SEC", 3600.0)))
        self._accum_cooldown_remaining_sec = int(cooldown_sec)
        self._accum_state = terminal
        logger.info(
            "accumulation %s: reason=%s spent=$%.4f acquired=%.8f buys=%d",
            terminal.lower(),
            str(reason),
            float(spent_usd),
            float(acquired_doge),
            int(self._accum_n_buys),
        )

    def _execute_accum_buy(self, *, usd_notional: float, now: float) -> tuple[bool, str, float, float]:
        price = float(self.last_price or 0.0)
        target_usd = max(0.0, float(usd_notional))
        if price <= 0.0:
            return False, "price_unavailable", 0.0, 0.0
        if target_usd <= 0.0:
            return False, "non_positive_notional", 0.0, 0.0

        vol_decimals = max(0, int(self.constraints.get("volume_decimals", 0)))
        raw_volume = target_usd / price
        if vol_decimals <= 0:
            volume = float(floor(raw_volume))
        else:
            scale = float(10 ** vol_decimals)
            volume = floor(raw_volume * scale) / scale
        if volume <= 0.0:
            return False, "rounded_volume_zero", 0.0, 0.0

        min_volume = max(0.0, float(self.constraints.get("min_volume", 0.0)))
        min_cost = max(0.0, float(self.constraints.get("min_cost_usd", 0.0)))
        estimated_cost = volume * price
        if volume + 1e-12 < min_volume:
            return False, "below_min_volume", 0.0, 0.0
        if min_cost > 0.0 and estimated_cost + 1e-12 < min_cost:
            return False, "below_min_cost", 0.0, 0.0

        if not self._try_reserve_loop_funds(side="buy", volume=volume, price=price):
            return False, "insufficient_usd", 0.0, 0.0
        if not self._consume_private_budget(1, "accumulation_market_buy"):
            self._release_loop_reservation(side="buy", volume=volume, price=price)
            return False, "api_budget_exhausted", 0.0, 0.0

        userref = int((int(now * 1000) % 900_000_000) + 100_000_000)
        try:
            txid = kraken_client.place_order(
                side="buy",
                volume=volume,
                price=price,
                pair=self.pair,
                ordertype="market",
                post_only=False,
                userref=userref,
            )
            if not txid:
                self._release_loop_reservation(side="buy", volume=volume, price=price)
                return False, "empty_txid", 0.0, 0.0
            self.ledger.commit_order("buy", price, volume)
            return True, str(txid), float(estimated_cost), float(volume)
        except Exception as e:
            self._release_loop_reservation(side="buy", volume=volume, price=price)
            return False, str(e), 0.0, 0.0

    def stop_accumulation(self) -> tuple[bool, str]:
        state = str(self._accum_state or "IDLE").strip().upper()
        if state not in {"ARMED", "ACTIVE"}:
            return False, "no active accumulation session"
        signal, conviction = self._accumulation_signal_conviction()
        self._accum_manual_stop_requested = True
        self._finalize_accumulation(
            now=_now(),
            terminal_state="STOPPED",
            reason="manual_stop",
            ai_signal=signal,
            ai_conviction=conviction,
        )
        return True, "accumulation stopped"

    def _update_accumulation(self, now: float) -> None:
        now_ts = float(now)
        cooldown_sec = max(0.0, float(getattr(config, "ACCUM_COOLDOWN_SEC", 3600.0)))
        if self._accum_last_session_end_ts > 0.0 and cooldown_sec > 0.0:
            self._accum_cooldown_remaining_sec = int(
                max(0.0, cooldown_sec - max(0.0, now_ts - float(self._accum_last_session_end_ts)))
            )
        else:
            self._accum_cooldown_remaining_sec = 0

        state = str(self._accum_state or "IDLE").strip().upper()
        if state not in {"IDLE", "ARMED", "ACTIVE", "COMPLETED", "STOPPED"}:
            state = "IDLE"
            self._accum_state = state

        if state in {"COMPLETED", "STOPPED"}:
            self._clear_accumulation_live_state()
            state = "IDLE"

        signal, conviction = self._accumulation_signal_conviction()
        min_conviction = max(0, min(100, int(getattr(config, "ACCUM_MIN_CONVICTION", 60))))
        idle_usd, budget_now = self._accumulation_idle_budget()
        capacity_band = str(self._compute_capacity_health(now_ts).get("status_band") or "normal").strip().lower()

        transition = dict(self._hmm_tertiary_transition or {})
        from_regime = self._normalize_regime_label(transition.get("from_regime", "RANGING"), "RANGING")
        to_regime = self._normalize_regime_label(transition.get("to_regime", "RANGING"), "RANGING")
        confirmed = bool(transition.get("confirmed", False))
        tertiary_state = dict(self._hmm_state_tertiary or {})
        current_regime = self._normalize_regime_label(tertiary_state.get("regime", to_regime), to_regime)

        if not self._flag_value("ACCUM_ENABLED"):
            if state in {"ARMED", "ACTIVE"}:
                self._finalize_accumulation(
                    now=now_ts,
                    terminal_state="STOPPED",
                    reason="accum_disabled",
                    ai_signal=signal,
                    ai_conviction=conviction,
                )
            return

        if state == "IDLE":
            if self._accum_cooldown_remaining_sec > 0:
                return
            if capacity_band == "stop":
                return
            if not (confirmed and from_regime != to_regime):
                return
            if budget_now <= 0.0:
                return
            self._arm_accumulation(
                now_ts,
                from_regime=from_regime,
                to_regime=to_regime,
                budget_usd=budget_now,
                idle_usd=idle_usd,
            )
            state = "ARMED"

        if state == "ARMED":
            if self._accum_manual_stop_requested:
                self._finalize_accumulation(
                    now=now_ts,
                    terminal_state="STOPPED",
                    reason="manual_stop",
                    ai_signal=signal,
                    ai_conviction=conviction,
                )
                return
            if (
                not confirmed
                or from_regime != self._accum_trigger_from_regime
                or to_regime != self._accum_trigger_to_regime
            ):
                logger.info("accumulation disarmed: transition no longer confirmed")
                self._clear_accumulation_live_state()
                return
            if budget_now <= 0.0:
                logger.info("accumulation disarmed: no idle budget above reserve")
                self._clear_accumulation_live_state()
                return
            if self._accum_budget_usd <= 0.0:
                self._accum_budget_usd = budget_now
            else:
                self._accum_budget_usd = min(float(self._accum_budget_usd), float(budget_now))

            if signal == "hold":
                self._accum_hold_streak = int(self._accum_hold_streak) + 1
            else:
                self._accum_hold_streak = 0
            if self._accum_hold_streak >= 3:
                logger.info("accumulation disarmed: AI hold streak reached %d", int(self._accum_hold_streak))
                self._clear_accumulation_live_state()
                return
            if signal != "accumulate_doge" or conviction < min_conviction:
                return
            if capacity_band == "stop":
                return
            if not self._activate_accumulation(now_ts):
                return
            state = "ACTIVE"

        if state != "ACTIVE":
            return

        if self._accum_manual_stop_requested:
            self._finalize_accumulation(
                now=now_ts,
                terminal_state="STOPPED",
                reason="manual_stop",
                ai_signal=signal,
                ai_conviction=conviction,
            )
            return
        if capacity_band == "stop":
            self._finalize_accumulation(
                now=now_ts,
                terminal_state="STOPPED",
                reason="capacity_stop",
                ai_signal=signal,
                ai_conviction=conviction,
            )
            return
        if current_regime == self._accum_trigger_from_regime:
            self._finalize_accumulation(
                now=now_ts,
                terminal_state="STOPPED",
                reason="transition_revert",
                ai_signal=signal,
                ai_conviction=conviction,
            )
            return

        if signal == "hold":
            self._accum_hold_streak = int(self._accum_hold_streak) + 1
        else:
            self._accum_hold_streak = 0
        if self._accum_hold_streak >= 2:
            self._finalize_accumulation(
                now=now_ts,
                terminal_state="STOPPED",
                reason="ai_hold_streak",
                ai_signal=signal,
                ai_conviction=conviction,
            )
            return

        max_drawdown_pct = max(0.0, float(getattr(config, "ACCUM_MAX_DRAWDOWN_PCT", 3.0)))
        if self._accum_start_price > 0 and self.last_price > 0:
            drawdown_pct = ((float(self._accum_start_price) - float(self.last_price)) / float(self._accum_start_price)) * 100.0
            if drawdown_pct > max_drawdown_pct:
                self._finalize_accumulation(
                    now=now_ts,
                    terminal_state="STOPPED",
                    reason="drawdown_breach",
                    ai_signal=signal,
                    ai_conviction=conviction,
                )
                return

        if self._accum_budget_usd <= 0.0:
            self._accum_budget_usd = float(budget_now)
        elif budget_now > 0.0:
            self._accum_budget_usd = min(float(self._accum_budget_usd), float(budget_now))
        self._accum_budget_usd = max(float(self._accum_spent_usd), float(self._accum_budget_usd))

        budget_remaining = max(0.0, float(self._accum_budget_usd) - float(self._accum_spent_usd))
        if budget_remaining <= 1e-9:
            self._finalize_accumulation(
                now=now_ts,
                terminal_state="COMPLETED",
                reason="budget_exhausted",
                ai_signal=signal,
                ai_conviction=conviction,
            )
            return

        interval_sec = max(1.0, float(getattr(config, "ACCUM_INTERVAL_SEC", 120.0)))
        if self._accum_last_buy_ts > 0.0 and (now_ts - float(self._accum_last_buy_ts)) < interval_sec:
            return

        chunk_usd_cfg = max(0.0, float(getattr(config, "ACCUM_CHUNK_USD", 2.0)))
        chunk_usd = min(chunk_usd_cfg, budget_remaining)
        if chunk_usd <= 0.0:
            self._finalize_accumulation(
                now=now_ts,
                terminal_state="COMPLETED",
                reason="budget_exhausted",
                ai_signal=signal,
                ai_conviction=conviction,
            )
            return

        ok, msg, spent_usd, acquired_doge = self._execute_accum_buy(usd_notional=chunk_usd, now=now_ts)
        if not ok:
            if msg in {"api_budget_exhausted", "price_unavailable"}:
                return
            self._finalize_accumulation(
                now=now_ts,
                terminal_state="STOPPED",
                reason=f"order_failed:{str(msg)[:64]}",
                ai_signal=signal,
                ai_conviction=conviction,
            )
            return

        self._accum_spent_usd = float(self._accum_spent_usd) + float(spent_usd)
        self._accum_acquired_doge = float(self._accum_acquired_doge) + float(acquired_doge)
        self._accum_n_buys = int(self._accum_n_buys) + 1
        self._accum_last_buy_ts = float(now_ts)

        logger.info(
            "accumulation buy: spent=$%.4f acquired=%.8f total_spent=$%.4f/%.4f buys=%d",
            float(spent_usd),
            float(acquired_doge),
            float(self._accum_spent_usd),
            float(self._accum_budget_usd),
            int(self._accum_n_buys),
        )

        if float(self._accum_spent_usd) + 1e-9 >= float(self._accum_budget_usd):
            self._finalize_accumulation(
                now=now_ts,
                terminal_state="COMPLETED",
                reason="budget_exhausted",
                ai_signal=signal,
                ai_conviction=conviction,
            )

    def _accumulation_status_payload(self, now: float | None = None) -> dict[str, Any]:
        now_ts = float(now if now is not None else _now())
        cooldown_sec = max(0.0, float(getattr(config, "ACCUM_COOLDOWN_SEC", 3600.0)))
        if self._accum_last_session_end_ts > 0.0 and cooldown_sec > 0.0:
            cooldown_remaining = max(0.0, cooldown_sec - max(0.0, now_ts - float(self._accum_last_session_end_ts)))
        else:
            cooldown_remaining = 0.0
        self._accum_cooldown_remaining_sec = int(cooldown_remaining)

        state = str(self._accum_state or "IDLE").strip().upper()
        if state not in {"IDLE", "ARMED", "ACTIVE", "COMPLETED", "STOPPED"}:
            state = "IDLE"
        direction = str(self._accum_direction or "").strip().lower()
        direction = direction if direction in {"doge", "usd"} else None

        spent_usd = max(0.0, float(self._accum_spent_usd))
        acquired_doge = max(0.0, float(self._accum_acquired_doge))
        budget_usd = max(0.0, float(self._accum_budget_usd))
        budget_remaining_usd = max(0.0, budget_usd - spent_usd)
        avg_price = (spent_usd / acquired_doge) if acquired_doge > 0.0 else None
        start_ref = float(self._accum_start_ts if self._accum_start_ts > 0.0 else self._accum_armed_at)
        elapsed_sec = max(0.0, now_ts - start_ref) if start_ref > 0.0 and state in {"ARMED", "ACTIVE"} else 0.0

        current_drawdown_pct = None
        if self._accum_start_price > 0.0 and self.last_price > 0.0:
            current_drawdown_pct = ((float(self.last_price) - float(self._accum_start_price)) / float(self._accum_start_price)) * 100.0

        ai_signal, ai_conviction = self._accumulation_signal_conviction()
        trigger_from = self._normalize_regime_label(self._accum_trigger_from_regime, "RANGING")
        trigger_to = self._normalize_regime_label(self._accum_trigger_to_regime, "RANGING")

        return {
            "enabled": self._flag_value("ACCUM_ENABLED"),
            "state": str(state),
            "active": bool(state == "ACTIVE"),
            "direction": direction,
            "budget_usd": float(budget_usd),
            "spent_usd": float(spent_usd),
            "budget_remaining_usd": float(budget_remaining_usd),
            "acquired_doge": float(acquired_doge),
            "avg_price": (float(avg_price) if avg_price is not None else None),
            "n_buys": int(max(0, int(self._accum_n_buys))),
            "start_ts": (float(self._accum_start_ts) if self._accum_start_ts > 0.0 else None),
            "elapsed_sec": float(elapsed_sec),
            "start_price": (float(self._accum_start_price) if self._accum_start_price > 0.0 else None),
            "current_drawdown_pct": (
                float(current_drawdown_pct) if current_drawdown_pct is not None else None
            ),
            "max_drawdown_pct": float(max(0.0, float(getattr(config, "ACCUM_MAX_DRAWDOWN_PCT", 3.0)))),
            "trigger": self._accumulation_trigger_label(trigger_from, trigger_to),
            "trigger_from_regime": str(trigger_from),
            "trigger_to_regime": str(trigger_to),
            "ai_accumulation_signal": str(ai_signal),
            "ai_accumulation_conviction": int(ai_conviction),
            "armed_at": (float(self._accum_armed_at) if self._accum_armed_at > 0.0 else None),
            "last_buy_ts": (float(self._accum_last_buy_ts) if self._accum_last_buy_ts > 0.0 else None),
            "cooldown_remaining_sec": int(self._accum_cooldown_remaining_sec),
            "manual_stop_requested": bool(self._accum_manual_stop_requested),
            "last_session_end_ts": (
                float(self._accum_last_session_end_ts) if self._accum_last_session_end_ts > 0.0 else None
            ),
            "last_session_summary": (
                dict(self._accum_last_session_summary) if isinstance(self._accum_last_session_summary, dict) else None
            ),
        }

    def _clear_ai_override(self) -> None:
        self._ai_override_tier = None
        self._ai_override_direction = None
        self._ai_override_until = None
        self._ai_override_applied_at = None
        self._ai_override_source_conviction = None

    def _mark_ai_history_latest(self, action: str) -> None:
        if not self._ai_regime_history:
            return
        tail = dict(self._ai_regime_history[-1] or {})
        tail["action"] = str(action)
        self._ai_regime_history[-1] = tail

    def apply_ai_regime_override(self, ttl_sec: int | None = None) -> tuple[bool, str]:
        if not self._flag_value("AI_REGIME_ADVISOR_ENABLED"):
            return False, "ai regime advisor disabled"

        opinion = dict(self._ai_regime_opinion or {})
        if not opinion:
            return False, "no ai opinion available"
        if str(opinion.get("error", "") or "").strip():
            return False, "ai opinion unavailable"

        agreement = str(opinion.get("agreement", "unknown") or "unknown").strip().lower()
        if agreement not in {"ai_upgrade", "ai_downgrade", "ai_flip"}:
            return False, "ai opinion already agrees with mechanical"

        try:
            recommended_tier = int(opinion.get("recommended_tier", 0) or 0)
        except (TypeError, ValueError):
            recommended_tier = 0
        recommended_tier = max(0, min(2, recommended_tier))
        recommended_direction = str(opinion.get("recommended_direction", "symmetric") or "symmetric").strip().lower()
        if recommended_direction not in {"symmetric", "long_bias", "short_bias"}:
            recommended_direction = "symmetric"

        try:
            conviction = int(opinion.get("conviction", 0) or 0)
        except (TypeError, ValueError):
            conviction = 0
        conviction = max(0, min(100, conviction))
        min_conviction = max(0, min(100, int(getattr(config, "AI_OVERRIDE_MIN_CONVICTION", 50))))
        if conviction < min_conviction:
            return False, f"conviction {conviction} below minimum {min_conviction}"

        mechanical_tier = max(0, min(2, int(self._regime_mechanical_tier)))
        low = max(0, mechanical_tier - 1)
        high = min(2, mechanical_tier + 1)
        applied_tier = max(low, min(high, recommended_tier))
        applied_direction = recommended_direction if applied_tier > 0 else "symmetric"

        capacity_band = str(self._compute_capacity_health(_now()).get("status_band") or "normal").strip().lower()
        if capacity_band == "stop" and applied_tier > mechanical_tier:
            return False, "capacity stop gate blocks upgrade override"

        default_ttl = max(1, int(getattr(config, "AI_OVERRIDE_TTL_SEC", 1800)))
        max_ttl = max(1, int(getattr(config, "AI_OVERRIDE_MAX_TTL_SEC", 3600)))
        if ttl_sec is None:
            use_ttl = default_ttl
        else:
            try:
                use_ttl = int(ttl_sec)
            except (TypeError, ValueError):
                use_ttl = default_ttl
        min_ttl = max(1, int(getattr(config, "AI_OVERRIDE_MIN_TTL_SEC", 300)))
        use_ttl = max(min_ttl, min(use_ttl, max_ttl))

        now_ts = _now()
        self._ai_override_tier = int(applied_tier)
        self._ai_override_direction = str(applied_direction)
        self._ai_override_applied_at = float(now_ts)
        self._ai_override_until = float(now_ts + use_ttl)
        self._ai_override_source_conviction = int(conviction)
        self._ai_regime_dismissed = False
        self._mark_ai_history_latest("applied")
        logger.info(
            "AI regime advisor: override applied tier=%d direction=%s ttl=%ds conviction=%d",
            int(applied_tier),
            str(applied_direction),
            int(use_ttl),
            int(conviction),
        )
        return True, (
            f"AI override applied: Tier {int(applied_tier)} {str(applied_direction)} "
            f"for {int(use_ttl)}s"
        )

    def revert_ai_regime_override(self) -> tuple[bool, str]:
        payload = self._ai_override_payload()
        was_active = bool(payload.get("active"))
        if (
            self._ai_override_tier is None
            and self._ai_override_direction is None
            and self._ai_override_until is None
        ):
            return False, "no ai override active"
        self._clear_ai_override()
        self._mark_ai_history_latest("reverted")
        logger.info("AI regime advisor: override cancelled by operator")
        if was_active:
            return True, "override cancelled; reverted to mechanical"
        return True, "override state cleared"

    def dismiss_ai_regime_opinion(self) -> tuple[bool, str]:
        if not self._flag_value("AI_REGIME_ADVISOR_ENABLED"):
            return False, "ai regime advisor disabled"
        opinion = dict(self._ai_regime_opinion or {})
        if not opinion:
            return False, "no ai opinion available"
        agreement = str(opinion.get("agreement", "unknown") or "unknown").strip().lower()
        if agreement not in {"ai_upgrade", "ai_downgrade", "ai_flip"}:
            return False, "nothing to dismiss (no active disagreement)"
        self._ai_regime_dismissed = True
        self._mark_ai_history_latest("dismissed")
        return True, "ai disagreement dismissed"

    def _ai_override_payload(self, now: float | None = None) -> dict[str, Any]:
        now_ts = float(now if now is not None else _now())
        expires = float(self._ai_override_until or 0.0)
        active = bool(self._ai_override_tier is not None and self._ai_override_direction and expires > now_ts)
        remaining = max(0.0, expires - now_ts) if active else None
        return {
            "active": bool(active),
            "tier": (int(self._ai_override_tier) if self._ai_override_tier is not None else None),
            "direction": (str(self._ai_override_direction) if self._ai_override_direction else None),
            "applied_at": (float(self._ai_override_applied_at) if self._ai_override_applied_at else None),
            "expires_at": (float(expires) if active else None),
            "remaining_sec": (int(remaining) if remaining is not None else None),
            "source_conviction": (
                int(self._ai_override_source_conviction)
                if self._ai_override_source_conviction is not None
                else None
            ),
        }

    def _ai_regime_worker(self, context: dict[str, Any], trigger: str, requested_at: float) -> None:
        try:
            opinion = ai_advisor.get_regime_opinion(context)
            pending = {
                "opinion": dict(opinion or {}),
                "trigger": str(trigger),
                "requested_at": float(requested_at),
                "completed_at": float(_now()),
                "mechanical_at_request": dict(context.get("mechanical_tier", {})),
                "consensus_at_request": str((context.get("hmm_consensus") or {}).get("agreement", "")),
            }
            self._ai_regime_pending_result = pending
        except Exception as e:
            self._ai_regime_pending_result = {
                "opinion": {
                    "recommended_tier": 0,
                    "recommended_direction": "symmetric",
                    "conviction": 0,
                    "accumulation_signal": "hold",
                    "accumulation_conviction": 0,
                    "rationale": "",
                    "watch_for": "",
                    "suggested_ttl_minutes": 0,
                    "panelist": "",
                    "provider": "",
                    "model": "",
                    "error": str(e),
                },
                "trigger": str(trigger),
                "requested_at": float(requested_at),
                "completed_at": float(_now()),
                "mechanical_at_request": dict(context.get("mechanical_tier", {})),
                "consensus_at_request": str((context.get("hmm_consensus") or {}).get("agreement", "")),
            }
        finally:
            self._ai_regime_thread_alive = False

    def _start_ai_regime_run(self, now: float, trigger: str) -> None:
        if not self._flag_value("AI_REGIME_ADVISOR_ENABLED"):
            return
        if self._ai_regime_thread_alive:
            return
        context = self._build_ai_regime_context(now)
        self._ai_regime_last_run_ts = float(now)
        self._ai_regime_last_trigger_reason = str(trigger)
        self._ai_regime_last_mechanical_tier = int(self._regime_mechanical_tier)
        self._ai_regime_last_mechanical_direction = str(self._regime_mechanical_direction)
        self._ai_regime_last_consensus_agreement = str((self._hmm_consensus or {}).get("agreement", "primary_only"))
        self._ai_regime_thread_alive = True
        thread = threading.Thread(
            target=self._ai_regime_worker,
            args=(context, str(trigger), float(now)),
            daemon=True,
            name="ai-regime-advisor",
        )
        try:
            thread.start()
        except Exception:
            self._ai_regime_thread_alive = False
            raise

    def _process_ai_regime_pending_result(self, now: float) -> None:
        pending = self._ai_regime_pending_result
        if not isinstance(pending, dict):
            return
        self._ai_regime_pending_result = None

        raw_opinion = pending.get("opinion", {})
        opinion = dict(raw_opinion) if isinstance(raw_opinion, dict) else {}
        recommended_tier = max(0, min(2, int(opinion.get("recommended_tier", 0) or 0)))
        recommended_direction = str(opinion.get("recommended_direction", "symmetric") or "symmetric").strip().lower()
        if recommended_direction not in {"symmetric", "long_bias", "short_bias"}:
            recommended_direction = "symmetric"
        conviction = max(0, min(100, int(opinion.get("conviction", 0) or 0)))
        accumulation_signal = str(opinion.get("accumulation_signal", "hold") or "hold").strip().lower()
        if accumulation_signal not in {"accumulate_doge", "hold", "accumulate_usd"}:
            accumulation_signal = "hold"
        accumulation_conviction = max(0, min(100, int(opinion.get("accumulation_conviction", 0) or 0)))
        rationale = str(opinion.get("rationale", "") or "")[:500]
        watch_for = str(opinion.get("watch_for", "") or "")[:200]
        suggested_ttl_minutes = max(0, min(60, int(opinion.get("suggested_ttl_minutes", 0) or 0)))
        panelist = str(opinion.get("panelist", "") or "")
        provider = str(opinion.get("provider", "") or "")
        model = str(opinion.get("model", "") or "")
        error = str(opinion.get("error", "") or "")

        mechanical_ref = pending.get("mechanical_at_request", {})
        if not isinstance(mechanical_ref, dict):
            mechanical_ref = {}
        mechanical_tier = max(0, min(2, int(mechanical_ref.get("current", self._regime_mechanical_tier) or 0)))
        mechanical_direction = str(
            mechanical_ref.get("direction", self._regime_mechanical_direction) or "symmetric"
        ).strip().lower()
        if mechanical_direction not in {"symmetric", "long_bias", "short_bias"}:
            mechanical_direction = "symmetric"

        if error:
            agreement = "error"
        else:
            agreement = self._classify_ai_regime_agreement(
                recommended_tier,
                recommended_direction,
                mechanical_tier,
                mechanical_direction,
            )

        self._ai_regime_opinion = {
            "recommended_tier": int(recommended_tier),
            "recommended_direction": str(recommended_direction),
            "conviction": int(conviction),
            "accumulation_signal": str(accumulation_signal),
            "accumulation_conviction": int(accumulation_conviction),
            "rationale": rationale,
            "watch_for": watch_for,
            "suggested_ttl_minutes": int(suggested_ttl_minutes),
            "panelist": panelist,
            "provider": provider,
            "model": model,
            "error": error,
            "agreement": agreement,
            "trigger": str(pending.get("trigger", "")),
            "ts": float(pending.get("completed_at", now) or now),
            "requested_at": float(pending.get("requested_at", now) or now),
            "mechanical_tier": int(mechanical_tier),
            "mechanical_direction": str(mechanical_direction),
        }
        self._ai_regime_dismissed = False

        action = "none"
        if agreement in {"ai_upgrade", "ai_downgrade", "ai_flip"} and not error:
            action = "pending"
        self._ai_regime_history.append(
            {
                "ts": float(pending.get("completed_at", now) or now),
                "mechanical_tier": int(mechanical_tier),
                "mechanical_direction": str(mechanical_direction),
                "ai_tier": int(recommended_tier),
                "ai_direction": str(recommended_direction),
                "conviction": int(conviction),
                "accumulation_signal": str(accumulation_signal),
                "accumulation_conviction": int(accumulation_conviction),
                "agreement": str(agreement),
                "action": action,
            }
        )
        while len(self._ai_regime_history) > self._ai_regime_history_limit():
            self._ai_regime_history.popleft()

        if error:
            logger.info("AI regime advisor: %s", error)
            return
        if agreement == "agree":
            logger.info(
                "AI regime advisor: agrees with mechanical Tier %d %s (conviction %d)",
                mechanical_tier,
                mechanical_direction,
                conviction,
            )
        else:
            logger.info(
                "AI regime advisor: Tier %d %s (conviction %d) -- mechanical Tier %d %s",
                recommended_tier,
                recommended_direction,
                conviction,
                mechanical_tier,
                mechanical_direction,
            )

    def _maybe_schedule_ai_regime(self, now: float) -> None:
        if not self._flag_value("AI_REGIME_ADVISOR_ENABLED"):
            return
        if self._ai_regime_thread_alive:
            return

        self._process_ai_regime_pending_result(now)

        last_run = float(self._ai_regime_last_run_ts or 0.0)
        elapsed = now - last_run if last_run > 0.0 else float("inf")
        debounce_sec = max(1.0, float(getattr(config, "AI_REGIME_DEBOUNCE_SEC", 60.0)))
        interval_sec = max(1.0, float(getattr(config, "AI_REGIME_INTERVAL_SEC", 300.0)))
        if elapsed < debounce_sec:
            return

        periodic_due = elapsed >= interval_sec
        agreement_now = str((self._hmm_consensus or {}).get("agreement", "primary_only"))
        mech_changed = (
            int(self._regime_mechanical_tier) != int(self._ai_regime_last_mechanical_tier)
            or str(self._regime_mechanical_direction) != str(self._ai_regime_last_mechanical_direction)
        )
        consensus_changed = agreement_now != str(self._ai_regime_last_consensus_agreement)
        event_due = bool(mech_changed or consensus_changed)
        if not (periodic_due or event_due):
            return

        if periodic_due:
            trigger = "periodic"
        elif mech_changed and consensus_changed:
            trigger = "mechanical_and_consensus_change"
        elif mech_changed:
            trigger = "mechanical_tier_change"
        else:
            trigger = "consensus_mode_change"
        self._start_ai_regime_run(now, trigger)

    def _regime_grace_elapsed(self, now: float) -> bool:
        if int(self._regime_tier) != 2:
            return False
        grace_sec = max(0.0, float(getattr(config, "REGIME_SUPPRESSION_GRACE_SEC", 0.0)))
        if grace_sec <= 0.0:
            return True
        started_at = float(self._regime_tier2_grace_start or self._regime_tier_entered_at or now)
        return (float(now) - started_at) >= grace_sec

    def _update_regime_tier(self, now: float) -> None:
        base_interval_sec = max(1.0, float(getattr(config, "REGIME_EVAL_INTERVAL_SEC", 300.0)))
        self._build_belief_state(now)
        self._update_bocpd_state(now)
        interval_sec = self._effective_regime_eval_interval(base_interval_sec)
        if self._regime_last_eval_ts > 0 and (now - self._regime_last_eval_ts) < interval_sec:
            return
        self._regime_last_eval_ts = now

        actuation_enabled = self._flag_value("REGIME_DIRECTIONAL_ENABLED")
        shadow_enabled = self._flag_value("REGIME_SHADOW_ENABLED")
        enabled = bool(actuation_enabled or shadow_enabled)

        # Backward-compatible bootstrap for tests/snapshots that only seed
        # effective regime fields.
        if self._regime_mechanical_tier_entered_at <= 0.0 and self._regime_tier_entered_at > 0.0:
            self._regime_mechanical_tier = max(0, min(2, int(self._regime_tier)))
            self._regime_mechanical_tier_entered_at = float(self._regime_tier_entered_at)
            if self._regime_mechanical_since <= 0.0:
                self._regime_mechanical_since = float(self._regime_tier_entered_at)
            reg = str((self._regime_shadow_state or {}).get("regime", "RANGING"))
            reg_bias = float((self._regime_shadow_state or {}).get("bias_signal", 0.0) or 0.0)
            self._regime_mechanical_direction = self._tier_direction(
                int(self._regime_mechanical_tier),
                reg,
                reg_bias,
                self._regime_side_suppressed,
            )
        if self._regime_mechanical_tier2_last_downgrade_at <= 0.0 and self._regime_tier2_last_downgrade_at > 0.0:
            self._regime_mechanical_tier2_last_downgrade_at = float(self._regime_tier2_last_downgrade_at)

        if enabled:
            _, _, _, _, pre_source = self._policy_hmm_signal()
            last_hmm_update_ts = float(pre_source.get("last_update_ts", 0.0) or 0.0)
            if (now - last_hmm_update_ts) >= interval_sec:
                self._update_hmm(now)
                self._build_belief_state(now)
                self._update_bocpd_state(now)

        regime, confidence_raw, bias, hmm_ready, policy_source = self._policy_hmm_signal()
        use_entropy_confidence = self._flag_value("BOCPD_ENABLED") or self._flag_value("KNOB_MODE_ENABLED")
        if use_entropy_confidence and bool(self._belief_state.enabled):
            confidence_raw = float(self._belief_state.confidence_score)
        confidence_modifier, confidence_modifier_source = self._hmm_confidence_modifier_for_source(
            policy_source
        )
        confidence_effective = max(
            0.0,
            min(1.0, float(confidence_raw) * float(confidence_modifier)),
        )
        reason = "disabled"

        override, override_conf = self._manual_regime_override()
        if override is not None:
            regime = override
            confidence_raw = override_conf
            confidence_effective = override_conf
            confidence_modifier = 1.0
            confidence_modifier_source = "manual_override"
            bias = 1.0 if override == "BULLISH" else -1.0
            hmm_ready = True
            reason = "manual_override"

        current_effective_tier = max(0, min(2, int(self._regime_tier)))
        current_mechanical_tier = max(0, min(2, int(self._regime_mechanical_tier)))
        mechanical_target_tier = 0
        target_tier = 0
        suppressed_side: str | None = None
        directional_ok_tier1 = False
        directional_ok_tier2 = False
        effective_regime = str(regime)
        effective_bias = float(bias)
        effective_confidence = float(confidence_effective)
        effective_direction = "symmetric"
        mechanical_reason = str(reason)

        if enabled and hmm_ready:
            tier1_conf = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER1_CONFIDENCE", 0.20))))
            tier2_conf = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER2_CONFIDENCE", 0.50))))
            tier1_bias_floor = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER1_BIAS_FLOOR", 0.10))))
            tier2_bias_floor = max(0.0, min(1.0, float(getattr(config, "REGIME_TIER2_BIAS_FLOOR", 0.25))))
            hysteresis = max(0.0, min(1.0, float(getattr(config, "REGIME_HYSTERESIS", 0.05))))
            min_dwell_sec = max(0.0, float(getattr(config, "REGIME_MIN_DWELL_SEC", 300.0)))
            entered_at = float(self._regime_mechanical_tier_entered_at)
            dwell_elapsed = max(0.0, now - entered_at) if entered_at > 0 else min_dwell_sec
            abs_bias = abs(float(bias))
            directional = regime in ("BULLISH", "BEARISH")
            directional_ok_tier1 = bool(directional and abs_bias >= tier1_bias_floor)
            directional_ok_tier2 = bool(directional and abs_bias >= tier2_bias_floor)

            if confidence_effective >= tier2_conf:
                mechanical_target_tier = 2
            elif confidence_effective >= tier1_conf:
                mechanical_target_tier = 1
            else:
                mechanical_target_tier = 0

            if mechanical_target_tier == 2 and not directional_ok_tier2:
                mechanical_target_tier = 1 if directional_ok_tier1 else 0
            elif mechanical_target_tier == 1 and not directional_ok_tier1:
                mechanical_target_tier = 0

            # Hysteresis on downgrades only — but never override the
            # directional gate.  If the downgrade was caused by missing
            # directional evidence (RANGING or weak bias), hysteresis
            # must not re-promote back to the gated tier.
            if mechanical_target_tier < current_mechanical_tier:
                # Only apply hysteresis if the current tier's directional
                # gate is still satisfied.
                gate_ok_for_current = (
                    (current_mechanical_tier == 1 and directional_ok_tier1) or
                    (current_mechanical_tier == 2 and directional_ok_tier2) or
                    current_mechanical_tier == 0
                )
                if gate_ok_for_current:
                    threshold = [0.0, tier1_conf, tier2_conf][current_mechanical_tier]
                    if confidence_effective > (threshold - hysteresis):
                        mechanical_target_tier = current_mechanical_tier

            # Minimum dwell between transitions.
            if mechanical_target_tier != current_mechanical_tier and dwell_elapsed < min_dwell_sec:
                mechanical_target_tier = current_mechanical_tier

            # Tier 2 re-entry cooldown: prevent rapid 2->0->2 oscillation.
            if mechanical_target_tier == 2 and current_mechanical_tier < 2:
                cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
                if cooldown_sec > 0 and self._regime_mechanical_tier2_last_downgrade_at > 0:
                    since_downgrade = now - self._regime_mechanical_tier2_last_downgrade_at
                    if since_downgrade < cooldown_sec:
                        mechanical_target_tier = 1 if directional_ok_tier1 else 0

            mechanical_reason = "hmm_eval"
        elif enabled:
            mechanical_reason = "hmm_not_ready"

        if int(mechanical_target_tier) != current_mechanical_tier:
            if current_mechanical_tier == 2 and int(mechanical_target_tier) < 2:
                self._regime_mechanical_tier2_last_downgrade_at = float(now)
            elif int(mechanical_target_tier) == 2:
                self._regime_mechanical_tier2_last_downgrade_at = 0.0
            self._regime_mechanical_tier = int(mechanical_target_tier)
            self._regime_mechanical_since = float(now)
            self._regime_mechanical_tier_entered_at = float(now)
        elif self._regime_mechanical_since <= 0.0:
            self._regime_mechanical_since = float(now)
        self._regime_mechanical_direction = self._tier_direction(
            int(mechanical_target_tier),
            str(regime),
            float(bias),
        )

        target_tier = int(mechanical_target_tier)
        reason = str(mechanical_reason)
        effective_direction = str(self._regime_mechanical_direction)

        # AI override lifecycle (manual apply/revert endpoint is added in P2).
        if override is None:
            ttl_max = max(1.0, float(getattr(config, "AI_OVERRIDE_MAX_TTL_SEC", 3600)))
            applied_at = float(self._ai_override_applied_at or 0.0)
            if applied_at > 0 and self._ai_override_until is not None:
                capped_until = min(float(self._ai_override_until), applied_at + ttl_max)
                self._ai_override_until = float(capped_until)

            expires_at = float(self._ai_override_until or 0.0)
            if expires_at > 0.0 and expires_at <= float(now):
                logger.info("AI regime advisor: override expired, reverting to mechanical")
                self._clear_ai_override()
                expires_at = 0.0

            if (
                self._ai_override_tier is not None
                and self._ai_override_direction in {"symmetric", "long_bias", "short_bias"}
                and expires_at > float(now)
            ):
                source_conv = int(self._ai_override_source_conviction or 0)
                min_conv = max(0, min(100, int(getattr(config, "AI_OVERRIDE_MIN_CONVICTION", 50))))
                if source_conv >= min_conv:
                    requested_tier = max(0, min(2, int(self._ai_override_tier)))
                    requested_direction = str(self._ai_override_direction)
                    low = max(0, int(mechanical_target_tier) - 1)
                    high = min(2, int(mechanical_target_tier) + 1)
                    applied_tier = max(low, min(high, requested_tier))
                    applied_direction = requested_direction
                    if applied_tier == 0:
                        applied_direction = "symmetric"

                    capacity_blocked = False
                    capacity_band = str(self._compute_capacity_health(now).get("status_band") or "normal")
                    if capacity_band == "stop" and applied_tier > int(mechanical_target_tier):
                        applied_tier = int(mechanical_target_tier)
                        applied_direction = str(self._regime_mechanical_direction)
                        capacity_blocked = True

                    target_tier = int(applied_tier)
                    effective_direction = str(applied_direction)
                    if (
                        capacity_blocked
                        and int(target_tier) == int(mechanical_target_tier)
                        and str(effective_direction) == str(self._regime_mechanical_direction)
                    ):
                        reason = "ai_override_capacity_blocked"
                    else:
                        reason = "ai_override"
                    effective_confidence = max(effective_confidence, max(0.0, min(1.0, source_conv / 100.0)))
                    if effective_direction == "long_bias":
                        effective_regime = "BULLISH"
                        effective_bias = max(0.25, abs(float(bias)))
                    elif effective_direction == "short_bias":
                        effective_regime = "BEARISH"
                        effective_bias = -max(0.25, abs(float(bias)))
                    else:
                        effective_regime = "RANGING"
                        effective_bias = 0.0
                else:
                    reason = "ai_override_rejected_conviction"

        current_tier = int(current_effective_tier)
        current_tier = max(0, min(2, current_tier))

        changed = target_tier != current_tier
        prev_entered_at = float(self._regime_tier_entered_at)
        prev_dwell_sec = max(0.0, now - prev_entered_at) if prev_entered_at > 0 else 0.0
        if changed:
            self._regime_tier = int(target_tier)
            self._regime_tier_entered_at = float(now)
            if int(target_tier) == 2:
                self._regime_tier2_grace_start = float(now)
                self._regime_tier2_last_downgrade_at = 0.0
                self._regime_cooldown_suppressed_side = None
            else:
                self._regime_tier2_grace_start = 0.0

        # Tier downgrade: clear regime ownership so balance-driven repair can restore both sides.
        # During cooldown, defer clearing to avoid rapid suppression churn.
        if changed and current_tier == 2 and int(target_tier) < 2:
            self._regime_tier2_last_downgrade_at = float(now)
            self._regime_cooldown_suppressed_side = (
                self._regime_side_suppressed if self._regime_side_suppressed in ("A", "B") else None
            )
            cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
            if cooldown_sec <= 0:
                for sid in sorted(self.slots.keys()):
                    st = self.slots[sid].state
                    if str(getattr(st, "mode_source", "none")) == "regime":
                        self.slots[sid].state = replace(st, mode_source="none")
                        logger.info(
                            "slot %s: cleared regime suppression (tier %d -> %d)",
                            sid,
                            int(current_tier),
                            int(target_tier),
                        )
                self._regime_cooldown_suppressed_side = None
            else:
                logger.info(
                    "tier %d -> %d: deferring regime clear for %.0fs cooldown",
                    int(current_tier),
                    int(target_tier),
                    cooldown_sec,
                )

        if int(target_tier) == 2:
            if effective_direction == "long_bias":
                suppressed_side = "A"
            elif effective_direction == "short_bias":
                suppressed_side = "B"
            elif str(effective_regime) in ("BULLISH", "BEARISH"):
                suppressed_side = "A" if float(effective_bias) > 0 else "B"
        self._regime_side_suppressed = suppressed_side

        if changed:
            self._regime_tier_history.append({
                "time": float(now),
                "from_tier": int(current_tier),
                "to_tier": int(target_tier),
                "regime": str(effective_regime),
                "confidence": round(float(effective_confidence), 3),
                "confidence_raw": round(float(confidence_raw), 3),
                "confidence_modifier": round(float(confidence_modifier), 3),
                "bias": round(float(effective_bias), 3),
                "mechanical_tier": int(mechanical_target_tier),
                "mechanical_direction": str(self._regime_mechanical_direction),
                "reason": str(reason),
            })
            if len(self._regime_tier_history) > 20:
                self._regime_tier_history = self._regime_tier_history[-20:]

        if changed:
            tier_labels = {0: "symmetric", 1: "biased", 2: "directional"}
            supabase_store.save_regime_tier_transition({
                "time": float(now),
                "pair": str(self.pair),
                "from_tier": int(current_tier),
                "to_tier": int(target_tier),
                "from_label": str(tier_labels.get(int(current_tier), "symmetric")),
                "to_label": str(tier_labels.get(int(target_tier), "symmetric")),
                "dwell_sec": float(prev_dwell_sec),
                "regime": str(effective_regime),
                "confidence": float(effective_confidence),
                "bias_signal": float(effective_bias),
                "abs_bias": abs(float(effective_bias)),
                "suppressed_side": suppressed_side,
                "favored_side": ("B" if suppressed_side == "A" else "A" if suppressed_side == "B" else None),
                "reason": str(reason),
                "shadow_enabled": bool(shadow_enabled),
                "actuation_enabled": bool(actuation_enabled),
                "hmm_ready": bool(hmm_ready),
            })

        if changed:
            logger.info(
                "[REGIME][shadow] tier %d -> %d regime=%s conf=%.3f bias=%.3f suppressed=%s",
                current_tier,
                int(target_tier),
                effective_regime,
                effective_confidence,
                effective_bias,
                suppressed_side or "-",
            )

        self._regime_shadow_state = {
            "enabled": enabled,
            "shadow_enabled": shadow_enabled,
            "actuation_enabled": actuation_enabled,
            "tier": int(target_tier),
            "regime": str(effective_regime),
            "confidence": float(effective_confidence),
            "confidence_raw": float(confidence_raw),
            "confidence_effective": float(confidence_effective),
            "confidence_modifier": float(confidence_modifier),
            "confidence_modifier_source": str(confidence_modifier_source),
            "bias_signal": float(effective_bias),
            "abs_bias": abs(float(effective_bias)),
            "suppressed_side": suppressed_side,
            "favored_side": ("B" if suppressed_side == "A" else "A" if suppressed_side == "B" else None),
            "directional_ok_tier1": bool(directional_ok_tier1),
            "directional_ok_tier2": bool(directional_ok_tier2),
            "hmm_ready": bool(hmm_ready),
            "last_eval_ts": float(now),
            "reason": reason,
            "mechanical_tier": int(mechanical_target_tier),
            "mechanical_direction": str(self._regime_mechanical_direction),
            "override_active": bool(reason == "ai_override"),
        }
        if self._flag_value("KNOB_MODE_ENABLED"):
            try:
                cap_band = str(self._compute_capacity_health(now).get("status_band") or "normal")
            except Exception:
                cap_band = "normal"
            try:
                vol_score = float(self._estimate_volatility_score())
            except Exception:
                vol_score = 1.0
            self._action_knobs = bayesian_engine.compute_action_knobs(
                belief_state=self._belief_state,
                volatility_score=float(vol_score),
                congestion_score=float((self._micro_features or {}).get("congestion_ratio", 0.0)),
                capacity_band=cap_band,
                cfg=self._belief_knob_cfg(),
                enabled=True,
            )
        else:
            self._action_knobs = bayesian_engine.ActionKnobs(enabled=False)
        self._update_throughput()

    def _apply_tier2_suppression(self, now: float) -> None:
        if self._flag_value("KNOB_MODE_ENABLED"):
            return
        if not self._flag_value("REGIME_DIRECTIONAL_ENABLED"):
            return
        if int(self._regime_tier) != 2:
            return
        if not self._regime_grace_elapsed(now):
            return
        suppressed = self._regime_side_suppressed
        if suppressed not in ("A", "B"):
            return
        suppressed_side = "sell" if suppressed == "A" else "buy"

        # Regime can flip while still in Tier 2; release old-side regime ownership.
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            if str(getattr(st, "mode_source", "none")) != "regime":
                continue
            if suppressed == "A" and st.short_only:
                self.slots[sid].state = replace(st, mode_source="none")
            elif suppressed == "B" and st.long_only:
                self.slots[sid].state = replace(st, mode_source="none")

        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state

            if suppressed == "A" and st.long_only and str(getattr(st, "mode_source", "none")) == "regime":
                continue
            if suppressed == "B" and st.short_only and str(getattr(st, "mode_source", "none")) == "regime":
                continue

            if sm.derive_phase(st) != "S0":
                continue

            target_order = next(
                (
                    o
                    for o in st.orders
                    if o.role == "entry" and o.side == suppressed_side
                ),
                None,
            )

            # Preserve favored one-sided slots by tagging regime ownership.
            if target_order is None:
                if suppressed == "A" and st.long_only and not st.short_only and str(getattr(st, "mode_source", "none")) != "regime":
                    self.slots[sid].state = replace(st, mode_source="regime")
                elif suppressed == "B" and st.short_only and not st.long_only and str(getattr(st, "mode_source", "none")) != "regime":
                    self.slots[sid].state = replace(st, mode_source="regime")
                continue

            if target_order.txid:
                try:
                    if not self._cancel_order(target_order.txid):
                        logger.warning("slot %s tier2 cancel %s failed", sid, target_order.txid)
                        continue
                except Exception as e:
                    logger.warning("slot %s tier2 cancel %s failed: %s", sid, target_order.txid, e)
                    continue

            new_st = sm.remove_order(st, target_order.local_id)
            if suppressed == "A":
                new_st = replace(new_st, long_only=True, short_only=False, mode_source="regime")
            else:
                new_st = replace(new_st, short_only=True, long_only=False, mode_source="regime")
            self.slots[sid].state = new_st
            logger.info(
                "slot %s: tier2 suppressed %s entry (regime=%s, conf=%.3f)",
                sid,
                suppressed,
                self._hmm_consensus.get("regime", ""),
                float(self._hmm_consensus.get("confidence", 0.0)),
            )

    def _clear_expired_regime_cooldown(self, now: float) -> None:
        if int(self._regime_tier) == 2:
            self._regime_tier2_last_downgrade_at = 0.0
            self._regime_cooldown_suppressed_side = None
            return
        if self._regime_tier2_last_downgrade_at <= 0:
            return
        cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
        if cooldown_sec <= 0:
            return
        elapsed = now - self._regime_tier2_last_downgrade_at
        if elapsed < cooldown_sec:
            return
        cleared = 0
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            if str(getattr(st, "mode_source", "none")) == "regime":
                self.slots[sid].state = replace(st, mode_source="none")
                cleared += 1
        if cleared:
            logger.info(
                "cooldown expired (%.0fs): cleared regime ownership on %d slots",
                elapsed,
                cleared,
            )
        self._regime_tier2_last_downgrade_at = 0.0
        self._regime_cooldown_suppressed_side = None

    def _regime_status_payload(self, now: float | None = None) -> dict[str, Any]:
        now_ts = float(now if now is not None else _now())
        state = dict(self._regime_shadow_state or {})
        regime_default, confidence_default, bias_default, _, _ = self._policy_hmm_signal()
        tier = int(state.get("tier", self._regime_tier))
        tier = max(0, min(2, tier))
        suppressed = state.get("suppressed_side", self._regime_side_suppressed)
        suppressed = suppressed if suppressed in ("A", "B") else None
        if suppressed == "A":
            favored = "B"
        elif suppressed == "B":
            favored = "A"
        else:
            favored = None
        tier_label = {0: "symmetric", 1: "biased", 2: "directional"}[tier]
        dwell_sec = max(0.0, now_ts - float(self._regime_tier_entered_at or now_ts))
        grace_sec = max(0.0, float(getattr(config, "REGIME_SUPPRESSION_GRACE_SEC", 0.0)))
        grace_start = float(self._regime_tier2_grace_start or self._regime_tier_entered_at or now_ts)
        grace_remaining = max(0.0, grace_sec - max(0.0, now_ts - grace_start)) if tier == 2 else 0.0
        cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
        cooldown_remaining = 0.0
        cooldown_side = None
        if (
            cooldown_sec > 0
            and int(self._regime_tier) < 2
            and self._regime_tier2_last_downgrade_at > 0
        ):
            elapsed = max(0.0, now_ts - self._regime_tier2_last_downgrade_at)
            if elapsed < cooldown_sec:
                cooldown_remaining = cooldown_sec - elapsed
                if self._regime_cooldown_suppressed_side in ("A", "B"):
                    cooldown_side = self._regime_cooldown_suppressed_side
        return {
            "enabled": bool(state.get("enabled", False)),
            "shadow_enabled": bool(state.get("shadow_enabled", False)),
            "actuation_enabled": bool(state.get("actuation_enabled", False)),
            "tier": tier,
            "tier_label": tier_label,
            "suppressed_side": suppressed,
            "favored_side": favored,
            "regime": str(state.get("regime", regime_default)),
            "confidence": float(state.get("confidence", confidence_default)),
            "confidence_raw": float(state.get("confidence_raw", confidence_default)),
            "confidence_effective": float(state.get("confidence_effective", confidence_default)),
            "confidence_modifier": float(state.get("confidence_modifier", 1.0)),
            "confidence_modifier_source": str(state.get("confidence_modifier_source", "none")),
            "bias_signal": float(state.get("bias_signal", bias_default)),
            "abs_bias": float(state.get("abs_bias", abs(float(bias_default)))),
            "directional_ok_tier1": bool(state.get("directional_ok_tier1", False)),
            "directional_ok_tier2": bool(state.get("directional_ok_tier2", False)),
            "hmm_ready": bool(state.get("hmm_ready", False)),
            "dwell_sec": float(dwell_sec),
            "hysteresis_buffer": float(getattr(config, "REGIME_HYSTERESIS", 0.05)),
            "grace_remaining_sec": float(grace_remaining),
            "cooldown_remaining_sec": float(cooldown_remaining),
            "cooldown_suppressed_side": cooldown_side,
            "regime_suppressed_slots": sum(
                1
                for slot in self.slots.values()
                if str(getattr(slot.state, "mode_source", "none")) == "regime"
            ),
            "tier_history": list(self._regime_tier_history[-20:]),
            "last_eval_ts": float(state.get("last_eval_ts", self._regime_last_eval_ts)),
            "reason": str(state.get("reason", "")),
        }

    def _ai_regime_status_payload(self, now: float | None = None) -> dict[str, Any]:
        now_ts = float(now if now is not None else _now())
        enabled = self._flag_value("AI_REGIME_ADVISOR_ENABLED")
        last_run_ts = float(self._ai_regime_last_run_ts or 0.0)
        last_run_age = (now_ts - last_run_ts) if last_run_ts > 0.0 else None
        interval_sec = max(1.0, float(getattr(config, "AI_REGIME_INTERVAL_SEC", 300.0)))
        if not enabled:
            next_run_in = None
        elif last_run_ts <= 0.0:
            next_run_in = 0
        else:
            next_run_in = int(max(0.0, interval_sec - max(0.0, now_ts - last_run_ts)))

        opinion = dict(self._ai_regime_opinion or {})
        recommended_tier = max(0, min(2, int(opinion.get("recommended_tier", 0) or 0)))
        recommended_direction = str(opinion.get("recommended_direction", "symmetric") or "symmetric").strip().lower()
        if recommended_direction not in {"symmetric", "long_bias", "short_bias"}:
            recommended_direction = "symmetric"
        agreement = str(opinion.get("agreement", "unknown") or "unknown")
        conviction = max(0, min(100, int(opinion.get("conviction", 0) or 0)))
        accumulation_signal = str(opinion.get("accumulation_signal", "hold") or "hold").strip().lower()
        if accumulation_signal not in {"accumulate_doge", "hold", "accumulate_usd"}:
            accumulation_signal = "hold"
        accumulation_conviction = max(0, min(100, int(opinion.get("accumulation_conviction", 0) or 0)))

        opinion_payload = {
            "recommended_tier": int(recommended_tier),
            "recommended_direction": str(recommended_direction),
            "conviction": int(conviction),
            "accumulation_signal": str(accumulation_signal),
            "accumulation_conviction": int(accumulation_conviction),
            "rationale": str(opinion.get("rationale", "") or ""),
            "watch_for": str(opinion.get("watch_for", "") or ""),
            "suggested_ttl_minutes": int(opinion.get("suggested_ttl_minutes", 0) or 0),
            "panelist": str(opinion.get("panelist", "") or ""),
            "provider": str(opinion.get("provider", "") or ""),
            "model": str(opinion.get("model", "") or ""),
            "agreement": agreement,
            "error": str(opinion.get("error", "") or ""),
            "trigger": str(opinion.get("trigger", "") or ""),
            "ts": float(opinion.get("ts", 0.0) or 0.0),
            "mechanical_tier": int(opinion.get("mechanical_tier", self._regime_mechanical_tier) or 0),
            "mechanical_direction": str(
                opinion.get("mechanical_direction", self._regime_mechanical_direction) or "symmetric"
            ),
        }

        default_ttl = int(max(1, int(getattr(config, "AI_OVERRIDE_TTL_SEC", 1800))))
        max_ttl = int(max(1, int(getattr(config, "AI_OVERRIDE_MAX_TTL_SEC", 3600))))
        ai_ttl_min = int(opinion_payload.get("suggested_ttl_minutes", 0) or 0)
        if ai_ttl_min > 0:
            min_ttl = max(1, int(getattr(config, "AI_OVERRIDE_MIN_TTL_SEC", 300)))
            suggested_ttl_sec = max(min_ttl, min(max_ttl, ai_ttl_min * 60))
        else:
            suggested_ttl_sec = default_ttl

        return {
            "enabled": enabled,
            "thread_alive": bool(self._ai_regime_thread_alive),
            "dismissed": bool(self._ai_regime_dismissed),
            "default_ttl_sec": default_ttl,
            "max_ttl_sec": max_ttl,
            "suggested_ttl_sec": int(suggested_ttl_sec),
            "min_conviction": int(max(0, min(100, int(getattr(config, "AI_OVERRIDE_MIN_CONVICTION", 50))))),
            "last_run_ts": (last_run_ts if last_run_ts > 0.0 else None),
            "last_run_age_sec": (float(last_run_age) if last_run_age is not None else None),
            "next_run_in_sec": next_run_in,
            "opinion": opinion_payload,
            "override": self._ai_override_payload(now_ts),
            "history": list(self._ai_regime_history),
        }

    def _normalize_kraken_ohlcv_rows(
        self,
        rows: list,
        *,
        interval_min: int,
        now_ts: float | None = None,
    ) -> list[dict[str, float | int | None]]:
        """
        Parse Kraken OHLC rows into normalized candle dicts sorted by time.
        """
        interval_sec = max(60, int(interval_min) * 60)
        now_ref = float(now_ts if now_ts is not None else _now())
        out: dict[float, dict[str, float | int | None]] = {}

        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            try:
                ts = float(row[0])
                o = float(row[1])
                h = float(row[2])
                l = float(row[3])
                c = float(row[4])
                v = float(row[6])
                tc = int(float(row[7])) if len(row) > 7 else None
            except (TypeError, ValueError):
                continue

            if ts <= 0 or min(o, h, l, c) <= 0 or v < 0:
                continue

            # Skip the still-forming bar.
            if (ts + interval_sec) > now_ref + 1e-9:
                continue

            out[ts] = {
                "time": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "trade_count": tc,
            }

        return [out[k] for k in sorted(out.keys())]

    @staticmethod
    def _extract_close_volume(
        candles: list[dict[str, float | int | None]],
    ) -> tuple[list[float], list[float]]:
        closes: list[float] = []
        volumes: list[float] = []
        for row in candles:
            try:
                c = float(row.get("close", 0.0))
                v = float(row.get("volume", 0.0))
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            closes.append(c)
            volumes.append(max(0.0, v))
        return closes, volumes

    def _sync_ohlcv_candles_for_interval(
        self,
        now: float,
        *,
        interval_min: int,
        sync_interval_sec: float,
        state_key: str,
    ) -> None:
        """
        Pull recent Kraken OHLCV for one interval and queue upserts to Supabase.
        """
        if not bool(getattr(config, "HMM_OHLCV_ENABLED", True)):
            return

        interval = max(1, int(interval_min))
        sync_interval = max(30.0, float(sync_interval_sec))
        if state_key == "secondary":
            last_sync_ts = float(self._ohlcv_secondary_last_sync_ts)
            since_cursor = int(self._ohlcv_secondary_since_cursor) if self._ohlcv_secondary_since_cursor else None
        elif state_key == "tertiary":
            last_sync_ts = float(self._ohlcv_tertiary_last_sync_ts)
            since_cursor = int(self._ohlcv_tertiary_since_cursor) if self._ohlcv_tertiary_since_cursor else None
        else:
            last_sync_ts = float(self._ohlcv_last_sync_ts)
            since_cursor = int(self._ohlcv_since_cursor) if self._ohlcv_since_cursor else None

        if last_sync_ts > 0 and (now - last_sync_ts) < sync_interval:
            return

        if state_key == "secondary":
            self._ohlcv_secondary_last_sync_ts = now
        elif state_key == "tertiary":
            self._ohlcv_tertiary_last_sync_ts = now
        else:
            self._ohlcv_last_sync_ts = now

        try:
            rows, last_cursor = kraken_client.get_ohlc_page(
                pair=self.pair,
                interval=interval,
                since=since_cursor,
            )
            candles = self._normalize_kraken_ohlcv_rows(
                rows,
                interval_min=interval,
                now_ts=now,
            )
            if candles:
                supabase_store.queue_ohlcv_candles(
                    candles,
                    pair=self.pair,
                    interval_min=interval,
                )
                self._hmm_readiness_cache.pop(state_key, None)
                self._hmm_readiness_last_ts.pop(state_key, None)
                if state_key == "secondary":
                    self._ohlcv_secondary_last_rows_queued = len(candles)
                    self._ohlcv_secondary_last_candle_ts = float(candles[-1]["time"])
                elif state_key == "tertiary":
                    self._ohlcv_tertiary_last_rows_queued = len(candles)
                    self._ohlcv_tertiary_last_candle_ts = float(candles[-1]["time"])
                else:
                    self._ohlcv_last_rows_queued = len(candles)
                    self._ohlcv_last_candle_ts = float(candles[-1]["time"])
            else:
                if state_key == "secondary":
                    self._ohlcv_secondary_last_rows_queued = 0
                elif state_key == "tertiary":
                    self._ohlcv_tertiary_last_rows_queued = 0
                else:
                    self._ohlcv_last_rows_queued = 0

            if last_cursor is not None:
                try:
                    next_cursor = int(last_cursor)
                    if state_key == "secondary":
                        self._ohlcv_secondary_since_cursor = next_cursor
                    elif state_key == "tertiary":
                        self._ohlcv_tertiary_since_cursor = next_cursor
                    else:
                        self._ohlcv_since_cursor = next_cursor
                except (TypeError, ValueError):
                    pass
            elif candles:
                if state_key == "secondary":
                    self._ohlcv_secondary_since_cursor = int(float(candles[-1]["time"]))
                elif state_key == "tertiary":
                    self._ohlcv_tertiary_since_cursor = int(float(candles[-1]["time"]))
                else:
                    self._ohlcv_since_cursor = int(float(candles[-1]["time"]))
        except Exception as e:
            logger.warning(
                "OHLCV sync failed (%s interval=%dm): %s",
                state_key,
                interval,
                e,
            )

    def _sync_ohlcv_candles(self, now: float | None = None) -> None:
        """
        Pull recent Kraken OHLCV and queue upserts to Supabase for active intervals.
        """
        now_ts = float(now if now is not None else _now())
        self._sync_ohlcv_candles_for_interval(
            now_ts,
            interval_min=max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5))),
            sync_interval_sec=max(30.0, float(getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0))),
            state_key="primary",
        )

        secondary_collect_enabled = self._flag_value("HMM_SECONDARY_OHLCV_ENABLED") or bool(
            self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED")
        )
        if secondary_collect_enabled:
            self._sync_ohlcv_candles_for_interval(
                now_ts,
                interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
                sync_interval_sec=max(
                    30.0,
                    float(
                        getattr(
                            config,
                            "HMM_SECONDARY_SYNC_INTERVAL_SEC",
                            getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                        )
                    ),
                ),
                state_key="secondary",
            )

        if self._flag_value("HMM_TERTIARY_ENABLED"):
            self._sync_ohlcv_candles_for_interval(
                now_ts,
                interval_min=max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60))),
                sync_interval_sec=max(
                    30.0,
                    float(
                        getattr(
                            config,
                            "HMM_TERTIARY_SYNC_INTERVAL_SEC",
                            getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                        )
                    ),
                ),
                state_key="tertiary",
            )


    def backfill_ohlcv_history(
        self,
        target_candles: int | None = None,
        max_pages: int | None = None,
        *,
        interval_min: int | None = None,
        state_key: str = "primary",
    ) -> tuple[bool, str]:
        """
        Best-effort historical OHLCV backfill for faster HMM warm-up.
        Queues rows into Supabase writer; ingestion is asynchronous.
        """
        if not bool(getattr(config, "HMM_OHLCV_ENABLED", True)):
            msg = "ohlcv pipeline disabled"
            if state_key == "secondary":
                self._hmm_backfill_last_message_secondary = msg
            elif state_key == "tertiary":
                self._hmm_backfill_last_message_tertiary = msg
            else:
                self._hmm_backfill_last_message = msg
            return False, msg

        default_target = (
            int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720))
            if state_key == "secondary"
            else int(getattr(config, "HMM_TERTIARY_TRAINING_CANDLES", 500))
            if state_key == "tertiary"
            else int(getattr(config, "HMM_TRAINING_CANDLES", 720))
        )
        target = max(1, int(target_candles if target_candles is not None else default_target))
        pages = max(1, int(max_pages if max_pages is not None else getattr(config, "HMM_OHLCV_BACKFILL_MAX_PAGES", 40)))
        if interval_min is None:
            interval = max(
                1,
                int(
                    getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)
                    if state_key == "secondary"
                    else getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)
                    if state_key == "tertiary"
                    else getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5)
                ),
            )
        else:
            interval = max(1, int(interval_min))
        existing = supabase_store.load_ohlcv_candles(
            limit=target,
            pair=self.pair,
            interval_min=interval,
        )
        existing_ts: set[float] = set()
        for row in existing:
            try:
                existing_ts.add(float(row.get("time")))
            except Exception:
                continue
        existing_count = len(existing_ts)
        if existing_count >= target:
            if state_key == "secondary":
                self._hmm_backfill_last_at_secondary = _now()
                self._hmm_backfill_last_rows_secondary = 0
                self._hmm_backfill_last_message_secondary = f"already_ready:{existing_count}/{target}"
            elif state_key == "tertiary":
                self._hmm_backfill_last_at_tertiary = _now()
                self._hmm_backfill_last_rows_tertiary = 0
                self._hmm_backfill_last_message_tertiary = f"already_ready:{existing_count}/{target}"
            else:
                self._hmm_backfill_last_at = _now()
                self._hmm_backfill_last_rows = 0
                self._hmm_backfill_last_message = f"already_ready:{existing_count}/{target}"
            return True, f"OHLCV already sufficient: {existing_count}/{target}"

        stall_limit = max(1, int(getattr(config, "HMM_BACKFILL_MAX_STALLS", 3)))
        stall_count = (
            self._hmm_backfill_stall_count_secondary
            if state_key == "secondary"
            else self._hmm_backfill_stall_count_tertiary
            if state_key == "tertiary"
            else self._hmm_backfill_stall_count
        )
        if stall_count >= stall_limit:
            msg = f"backfill_circuit_open:stalls={stall_count}/{stall_limit}"
            if state_key == "secondary":
                self._hmm_backfill_last_at_secondary = _now()
                self._hmm_backfill_last_rows_secondary = 0
                self._hmm_backfill_last_message_secondary = msg
            elif state_key == "tertiary":
                self._hmm_backfill_last_at_tertiary = _now()
                self._hmm_backfill_last_rows_tertiary = 0
                self._hmm_backfill_last_message_tertiary = msg
            else:
                self._hmm_backfill_last_at = _now()
                self._hmm_backfill_last_rows = 0
                self._hmm_backfill_last_message = msg
            return False, f"Backfill circuit-breaker open ({stall_count} consecutive stalls)"

        # Kraken OHLC uses an opaque cursor; start without `since` and paginate
        # only with Kraken's returned `last` value.
        cursor = 0

        fetched: dict[float, dict[str, float | int | None]] = {}
        for _ in range(pages):
            try:
                rows, last_cursor = kraken_client.get_ohlc_page(
                    pair=self.pair,
                    interval=interval,
                    since=cursor if cursor > 0 else None,
                )
            except Exception as e:
                if state_key == "secondary":
                    self._hmm_backfill_last_message_secondary = f"fetch_failed:{e}"
                elif state_key == "tertiary":
                    self._hmm_backfill_last_message_tertiary = f"fetch_failed:{e}"
                else:
                    self._hmm_backfill_last_message = f"fetch_failed:{e}"
                break

            parsed = self._normalize_kraken_ohlcv_rows(
                rows,
                interval_min=interval,
                now_ts=_now(),
            )
            for row in parsed:
                fetched[float(row["time"])] = row

            if last_cursor is None:
                break
            try:
                next_cursor = int(last_cursor)
            except (TypeError, ValueError):
                break
            if next_cursor <= cursor:
                break
            cursor = next_cursor

        queued_rows = 0
        new_unique = 0
        if fetched:
            payload = [fetched[k] for k in sorted(fetched.keys())]
            supabase_store.queue_ohlcv_candles(
                payload,
                pair=self.pair,
                interval_min=interval,
            )
            queued_rows = len(payload)
            new_unique = sum(1 for ts in fetched.keys() if ts not in existing_ts)

        if new_unique == 0:
            if state_key == "secondary":
                self._hmm_backfill_stall_count_secondary += 1
            elif state_key == "tertiary":
                self._hmm_backfill_stall_count_tertiary += 1
            else:
                self._hmm_backfill_stall_count += 1
        else:
            if state_key == "secondary":
                self._hmm_backfill_stall_count_secondary = 0
            elif state_key == "tertiary":
                self._hmm_backfill_stall_count_tertiary = 0
            else:
                self._hmm_backfill_stall_count = 0

        if state_key == "secondary":
            self._hmm_backfill_last_at_secondary = _now()
            self._hmm_backfill_last_rows_secondary = int(queued_rows)
            current_stalls = self._hmm_backfill_stall_count_secondary
        elif state_key == "tertiary":
            self._hmm_backfill_last_at_tertiary = _now()
            self._hmm_backfill_last_rows_tertiary = int(queued_rows)
            current_stalls = self._hmm_backfill_stall_count_tertiary
        else:
            self._hmm_backfill_last_at = _now()
            self._hmm_backfill_last_rows = int(queued_rows)
            current_stalls = self._hmm_backfill_stall_count
        est_total = existing_count + new_unique
        backfill_msg = f"queued={queued_rows} new={new_unique} est_total={est_total}/{target}"
        if current_stalls > 0:
            backfill_msg += f" stalls={current_stalls}"
        if state_key == "secondary":
            self._hmm_backfill_last_message_secondary = backfill_msg
        elif state_key == "tertiary":
            self._hmm_backfill_last_message_tertiary = backfill_msg
        else:
            self._hmm_backfill_last_message = backfill_msg
        self._hmm_readiness_cache.pop(state_key, None)
        self._hmm_readiness_last_ts.pop(state_key, None)

        if queued_rows <= 0:
            return False, (
                "OHLCV backfill queued no rows "
                f"({state_key}, interval={interval}m); "
                f"existing={existing_count}/{target}, max_pages={pages}"
            )

        return True, (
            f"OHLCV backfill queued {queued_rows} rows ({state_key}, interval={interval}m) "
            f"({new_unique} new, est {est_total}/{target})"
        )

    def _maybe_backfill_ohlcv_on_startup(self) -> None:
        if not bool(getattr(config, "HMM_OHLCV_BACKFILL_ON_STARTUP", True)):
            return
        now_ts = _now()
        readiness = self._hmm_data_readiness(now_ts)
        if bool(readiness.get("ready_for_target_window", False)):
            logger.info("OHLCV startup backfill skipped (primary already ready)")
        else:
            ok, msg = self.backfill_ohlcv_history(
                target_candles=int(getattr(config, "HMM_TRAINING_CANDLES", 720)),
                max_pages=int(getattr(config, "HMM_OHLCV_BACKFILL_MAX_PAGES", 40)),
                interval_min=max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1))),
                state_key="primary",
            )
            if ok:
                logger.info("OHLCV startup backfill: %s", msg)
            else:
                logger.warning("OHLCV startup backfill: %s", msg)

        secondary_collect_enabled = self._flag_value("HMM_SECONDARY_OHLCV_ENABLED") or bool(
            self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED")
        )
        if secondary_collect_enabled:
            secondary_readiness = self._hmm_data_readiness(
                now_ts,
                interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
                training_target=max(1, int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720))),
                min_samples=max(1, int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200))),
                sync_interval_sec=max(
                    30.0,
                    float(
                        getattr(
                            config,
                            "HMM_SECONDARY_SYNC_INTERVAL_SEC",
                            getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                        )
                    ),
                ),
                state_key="secondary",
            )
            if bool(secondary_readiness.get("ready_for_target_window", False)):
                logger.info("OHLCV startup backfill skipped (secondary already ready)")
            else:
                ok, msg = self.backfill_ohlcv_history(
                    target_candles=int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720)),
                    max_pages=int(getattr(config, "HMM_OHLCV_BACKFILL_MAX_PAGES", 40)),
                    interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
                    state_key="secondary",
                )
                if ok:
                    logger.info("OHLCV startup backfill (secondary): %s", msg)
                else:
                    logger.warning("OHLCV startup backfill (secondary): %s", msg)

        if not self._flag_value("HMM_TERTIARY_ENABLED"):
            return

        tertiary_readiness = self._hmm_data_readiness(
            now_ts,
            interval_min=max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60))),
            training_target=max(1, int(getattr(config, "HMM_TERTIARY_TRAINING_CANDLES", 500))),
            min_samples=max(1, int(getattr(config, "HMM_TERTIARY_MIN_TRAIN_SAMPLES", 150))),
            sync_interval_sec=max(
                30.0,
                float(
                    getattr(
                        config,
                        "HMM_TERTIARY_SYNC_INTERVAL_SEC",
                        getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                    )
                ),
            ),
            state_key="tertiary",
        )
        if bool(tertiary_readiness.get("ready_for_target_window", False)):
            logger.info("OHLCV startup backfill skipped (tertiary already ready)")
            return

        ok, msg = self.backfill_ohlcv_history(
            target_candles=int(getattr(config, "HMM_TERTIARY_TRAINING_CANDLES", 500)),
            max_pages=int(getattr(config, "HMM_OHLCV_BACKFILL_MAX_PAGES", 40)),
            interval_min=max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60))),
            state_key="tertiary",
        )
        if ok:
            logger.info("OHLCV startup backfill (tertiary): %s", msg)
        else:
            logger.warning("OHLCV startup backfill (tertiary): %s", msg)

    def _load_recent_ohlcv_rows(
        self,
        count: int,
        *,
        interval_min: int,
    ) -> list[dict[str, float | int | None]]:
        """
        Load recent OHLCV candles, preferring Supabase and falling back to Kraken.
        """
        limit = max(1, int(count))
        interval = max(1, int(interval_min))
        supa_rows = supabase_store.load_ohlcv_candles(
            limit=limit,
            pair=self.pair,
            interval_min=interval,
        )

        merged: dict[float, dict[str, float | int | None]] = {}
        for row in supa_rows:
            try:
                ts = float(row.get("time"))
            except (TypeError, ValueError):
                continue
            merged[ts] = row

        need_fallback = len(merged) < limit
        if need_fallback:
            try:
                kr_rows = kraken_client.get_ohlc(pair=self.pair, interval=interval)
                parsed = self._normalize_kraken_ohlcv_rows(
                    kr_rows,
                    interval_min=interval,
                    now_ts=_now(),
                )
                for row in parsed:
                    merged[float(row["time"])] = row
            except Exception as e:
                logger.debug("OHLCV fallback fetch failed: %s", e)

        out = [merged[k] for k in sorted(merged.keys())]
        if len(out) > limit:
            out = out[-limit:]
        return out

    def _fetch_training_candles(
        self,
        count: int | None = None,
        *,
        interval_min: int | None = None,
    ) -> tuple[list[float], list[float]]:
        interval = max(
            1,
            int(
                getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5)
                if interval_min is None
                else interval_min
            ),
        )
        target = max(1, int(count if count is not None else getattr(config, "HMM_TRAINING_CANDLES", 720)))
        rows = self._load_recent_ohlcv_rows(target, interval_min=interval)
        return self._extract_close_volume(rows)

    def _fetch_recent_candles(
        self,
        count: int | None = None,
        *,
        interval_min: int | None = None,
    ) -> tuple[list[float], list[float]]:
        interval = max(
            1,
            int(
                getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5)
                if interval_min is None
                else interval_min
            ),
        )
        target = max(1, int(count if count is not None else getattr(config, "HMM_RECENT_CANDLES", 100)))
        rows = self._load_recent_ohlcv_rows(target, interval_min=interval)
        return self._extract_close_volume(rows)

    def _hmm_data_readiness(
        self,
        now: float | None = None,
        *,
        interval_min: int | None = None,
        training_target: int | None = None,
        min_samples: int | None = None,
        sync_interval_sec: float | None = None,
        state_key: str = "primary",
    ) -> dict[str, Any]:
        """
        Runtime readiness summary for HMM training data.
        """
        now_ts = float(now if now is not None else _now())
        ttl = max(5.0, float(getattr(config, "HMM_READINESS_CACHE_SEC", 300.0)))
        key_raw = str(state_key).lower()
        if key_raw == "secondary":
            use_state_key = "secondary"
        elif key_raw == "tertiary":
            use_state_key = "tertiary"
        else:
            use_state_key = "primary"
        if interval_min is None:
            interval = max(
                1,
                int(
                    getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)
                    if use_state_key == "secondary"
                    else getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)
                    if use_state_key == "tertiary"
                    else getattr(config, "HMM_OHLCV_INTERVAL_MIN", 5)
                ),
            )
        else:
            interval = max(1, int(interval_min))
        if training_target is None:
            target = max(
                1,
                int(
                    getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720)
                    if use_state_key == "secondary"
                    else getattr(config, "HMM_TERTIARY_TRAINING_CANDLES", 500)
                    if use_state_key == "tertiary"
                    else getattr(config, "HMM_TRAINING_CANDLES", 720)
                ),
            )
        else:
            target = max(1, int(training_target))
        if min_samples is None:
            min_train = max(
                1,
                int(
                    getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200)
                    if use_state_key == "secondary"
                    else getattr(config, "HMM_TERTIARY_MIN_TRAIN_SAMPLES", 150)
                    if use_state_key == "tertiary"
                    else getattr(config, "HMM_MIN_TRAIN_SAMPLES", 500)
                ),
            )
        else:
            min_train = max(1, int(min_samples))
        if sync_interval_sec is None:
            sync_interval = (
                float(getattr(config, "HMM_SECONDARY_SYNC_INTERVAL_SEC", 300.0))
                if use_state_key == "secondary"
                else float(getattr(config, "HMM_TERTIARY_SYNC_INTERVAL_SEC", 3600.0))
                if use_state_key == "tertiary"
                else float(getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0))
            )
        else:
            sync_interval = float(sync_interval_sec)

        cached = self._hmm_readiness_cache.get(use_state_key)
        last_cache_ts = float(self._hmm_readiness_last_ts.get(use_state_key, 0.0))
        if cached and (now_ts - last_cache_ts) < ttl:
            cached_interval = int(cached.get("interval_min", 0) or 0)
            cached_target = int(cached.get("training_target", 0) or 0)
            cached_min = int(cached.get("min_train_samples", 0) or 0)
            if cached_interval == interval and cached_target == target and cached_min == min_train:
                return dict(cached)

        try:
            secondary_collect_enabled = self._flag_value("HMM_SECONDARY_OHLCV_ENABLED") or bool(
                self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED")
            )
            tertiary_collect_enabled = self._flag_value("HMM_TERTIARY_ENABLED")
            enabled = bool(getattr(config, "HMM_OHLCV_ENABLED", True))
            if use_state_key == "secondary":
                enabled = bool(enabled and secondary_collect_enabled)
            elif use_state_key == "tertiary":
                enabled = bool(enabled and tertiary_collect_enabled)
            rows = supabase_store.load_ohlcv_candles(
                limit=target,
                pair=self.pair,
                interval_min=interval,
            )
            source = "supabase" if rows else "none"

            closes, volumes = self._extract_close_volume(rows)
            sample_count = min(len(closes), len(volumes))
            span_sec = 0.0
            last_candle_ts = None
            if rows:
                try:
                    first_ts = float(rows[0].get("time", 0.0))
                    last_candle_ts = float(rows[-1].get("time", 0.0))
                    span_sec = max(0.0, last_candle_ts - first_ts)
                except (TypeError, ValueError):
                    last_candle_ts = None
                    span_sec = 0.0

            freshness_sec = (now_ts - last_candle_ts) if last_candle_ts is not None else None
            interval_sec = interval * 60.0
            # Keep short-interval feeds honest (e.g., 1m should not tolerate 15m stale data).
            freshness_limit_sec = max(180.0, interval_sec * 3.0)
            freshness_ok = bool(freshness_sec is not None and freshness_sec <= freshness_limit_sec)
            volume_nonzero_count = sum(1 for v in volumes if v > 0)
            volume_coverage_pct = (volume_nonzero_count / sample_count * 100.0) if sample_count else 0.0
            coverage_pct = sample_count / target * 100.0 if target > 0 else 0.0

            gaps: list[str] = []
            if not enabled:
                gaps.append("pipeline_disabled")
            if sample_count < min_train:
                gaps.append(f"insufficient_samples:{sample_count}/{min_train}")
            if sample_count < target:
                gaps.append(f"below_target_window:{sample_count}/{target}")
            if not freshness_ok:
                gaps.append("stale_candles")
            if volume_coverage_pct < 95.0:
                gaps.append(f"low_volume_coverage:{volume_coverage_pct:.1f}%")
            if source != "supabase":
                gaps.append("no_supabase_ohlcv")

            if use_state_key == "secondary":
                last_sync_ts = float(self._ohlcv_secondary_last_sync_ts)
                last_sync_rows_queued = int(self._ohlcv_secondary_last_rows_queued)
                sync_cursor = self._ohlcv_secondary_since_cursor
                backfill_last_at = float(self._hmm_backfill_last_at_secondary)
                backfill_last_rows = int(self._hmm_backfill_last_rows_secondary)
                backfill_last_message = str(self._hmm_backfill_last_message_secondary or "")
            elif use_state_key == "tertiary":
                last_sync_ts = float(self._ohlcv_tertiary_last_sync_ts)
                last_sync_rows_queued = int(self._ohlcv_tertiary_last_rows_queued)
                sync_cursor = self._ohlcv_tertiary_since_cursor
                backfill_last_at = float(self._hmm_backfill_last_at_tertiary)
                backfill_last_rows = int(self._hmm_backfill_last_rows_tertiary)
                backfill_last_message = str(self._hmm_backfill_last_message_tertiary or "")
            else:
                last_sync_ts = float(self._ohlcv_last_sync_ts)
                last_sync_rows_queued = int(self._ohlcv_last_rows_queued)
                sync_cursor = self._ohlcv_since_cursor
                backfill_last_at = float(self._hmm_backfill_last_at)
                backfill_last_rows = int(self._hmm_backfill_last_rows)
                backfill_last_message = str(self._hmm_backfill_last_message or "")

            out = {
                "enabled": bool(enabled),
                "state_key": use_state_key,
                "source": source,
                "interval_min": interval,
                "training_target": target,
                "min_train_samples": min_train,
                "samples": sample_count,
                "coverage_pct": round(coverage_pct, 2),
                "span_hours": round(span_sec / 3600.0, 2),
                "last_candle_ts": last_candle_ts,
                "freshness_sec": freshness_sec,
                "freshness_limit_sec": freshness_limit_sec,
                "freshness_ok": freshness_ok,
                "volume_coverage_pct": round(volume_coverage_pct, 2),
                "ready_for_min_train": bool(enabled and sample_count >= min_train and freshness_ok),
                "ready_for_target_window": bool(enabled and sample_count >= target and freshness_ok),
                "gaps": gaps,
                "sync_interval_sec": float(sync_interval),
                "last_sync_ts": last_sync_ts,
                "last_sync_rows_queued": last_sync_rows_queued,
                "sync_cursor": sync_cursor,
                "backfill_last_at": backfill_last_at,
                "backfill_last_rows": backfill_last_rows,
                "backfill_last_message": backfill_last_message,
            }
        except Exception as e:
            out = {
                "enabled": bool(getattr(config, "HMM_OHLCV_ENABLED", True)),
                "state_key": use_state_key,
                "error": str(e),
                "ready_for_min_train": False,
                "ready_for_target_window": False,
                "gaps": ["readiness_check_failed"],
                "backfill_last_at": (
                    float(self._hmm_backfill_last_at_secondary)
                    if use_state_key == "secondary"
                    else float(self._hmm_backfill_last_at_tertiary)
                    if use_state_key == "tertiary"
                    else float(self._hmm_backfill_last_at)
                ),
                "backfill_last_rows": (
                    int(self._hmm_backfill_last_rows_secondary)
                    if use_state_key == "secondary"
                    else int(self._hmm_backfill_last_rows_tertiary)
                    if use_state_key == "tertiary"
                    else int(self._hmm_backfill_last_rows)
                ),
                "backfill_last_message": (
                    str(self._hmm_backfill_last_message_secondary or "")
                    if use_state_key == "secondary"
                    else str(self._hmm_backfill_last_message_tertiary or "")
                    if use_state_key == "tertiary"
                    else str(self._hmm_backfill_last_message or "")
                ),
            }

        self._hmm_readiness_cache[use_state_key] = dict(out)
        self._hmm_readiness_last_ts[use_state_key] = now_ts
        return out

    def _price_age_sec(self) -> float:
        if self.last_price_ts <= 0:
            return 1e9
        return max(0.0, _now() - self.last_price_ts)

    def _volatility_profit_pct(self) -> float:
        # Volatility-aware runtime target: user's profit_pct is the base,
        # volatility applies a bounded multiplier (same pattern as entry_pct + HMM).
        base = self.profit_pct
        if base <= 0:
            base = float(config.VOLATILITY_PROFIT_FLOOR)

        samples = [p for _, p in self.price_history[-180:]]
        if len(samples) < 12:
            return base

        ranges = []
        for i in range(1, len(samples)):
            prev = samples[i - 1]
            cur = samples[i]
            if prev > 0:
                ranges.append(abs(cur - prev) / prev * 100.0)
        if not ranges:
            return base

        vol_suggested = median(ranges) * 2.0 * float(config.VOLATILITY_PROFIT_FACTOR)

        # Express as multiplier on user's base, clamped to configured bounds.
        raw_mult = vol_suggested / base if base > 0 else 1.0
        mult = max(float(config.VOLATILITY_PROFIT_MULT_FLOOR),
                   min(float(config.VOLATILITY_PROFIT_MULT_CEILING), raw_mult))
        target = base * mult

        # Absolute ceiling still applies.
        target = min(float(config.VOLATILITY_PROFIT_CEILING), target)

        # Never below fee floor.
        fee_floor = self.maker_fee_pct * 2.0 + 0.1
        target = max(target, fee_floor)
        return round(target, 4)

    def _utc_day_key(self, ts: float | None = None) -> str:
        dt = datetime.fromtimestamp(ts if ts is not None else _now(), timezone.utc)
        return dt.strftime("%Y-%m-%d")

    def _compute_daily_realized_loss_utc(self, now_ts: float | None = None) -> float:
        now_ts = float(now_ts if now_ts is not None else _now())
        now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
        day_start_dt = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc)
        day_start = day_start_dt.timestamp()
        day_end = day_start + 86400.0

        loss_total = 0.0
        for slot in self.slots.values():
            for cycle in slot.state.completed_cycles:
                exit_ts = float(getattr(cycle, "exit_time", 0.0) or 0.0)
                if exit_ts <= 0.0 or exit_ts < day_start or exit_ts >= day_end:
                    continue
                net = float(getattr(cycle, "net_profit", 0.0) or 0.0)
                if net < 0.0:
                    loss_total += -net
        return loss_total

    def _update_daily_loss_lock(self, now_ts: float | None = None) -> float:
        now_ts = float(now_ts if now_ts is not None else _now())
        utc_day = self._utc_day_key(now_ts)

        # Auto-clear at UTC rollover, but require manual operator resume.
        if self._daily_loss_lock_active and self._daily_loss_lock_utc_day and self._daily_loss_lock_utc_day != utc_day:
            self._daily_loss_lock_active = False
            self._daily_loss_lock_utc_day = ""
            if self.mode == "PAUSED" and str(self.pause_reason).startswith("daily loss limit hit"):
                self.pause_reason = "daily loss lock cleared at UTC rollover; manual resume required"

        daily_loss = self._compute_daily_realized_loss_utc(now_ts)
        self._daily_realized_loss_utc = float(daily_loss)

        limit = max(0.0, float(config.DAILY_LOSS_LIMIT))

        # If limit is disabled (0) or raised above current loss, clear any
        # existing lock on the same day so resume() is not blocked.
        if self._daily_loss_lock_active and (limit <= 0.0 or daily_loss + 1e-12 < limit):
            logger.info(
                "daily loss lock cleared: loss $%.4f < limit $%.4f (or limit disabled)",
                daily_loss, limit,
            )
            self._daily_loss_lock_active = False
            self._daily_loss_lock_utc_day = ""

        if limit <= 0.0:
            return daily_loss

        if daily_loss + 1e-12 < limit:
            return daily_loss

        if not self._daily_loss_lock_active or self._daily_loss_lock_utc_day != utc_day:
            self._daily_loss_lock_active = True
            self._daily_loss_lock_utc_day = utc_day
            reason = f"daily loss limit hit: ${daily_loss:.4f} >= ${limit:.4f} (UTC {utc_day})"
            self.pause(reason)
        return daily_loss

    # ------------------ Startup/Reconcile ------------------

    def _reconcile_open_orders(self) -> dict:
        try:
            open_orders = self._get_open_orders()
        except Exception as e:
            logger.warning("Open-order reconciliation failed: %s", e)
            return {}

        known = 0
        dropped = 0
        for sid, slot in self.slots.items():
            st = slot.state
            kept = []
            for o in st.orders:
                if not o.txid:
                    # Unbound pending order from old crash, drop and let bootstrap rebuild.
                    dropped += 1
                    continue
                if o.txid in open_orders:
                    kept.append(o)
                    known += 1
                else:
                    # Keep it for one loop so closed status can be picked by QueryOrders.
                    kept.append(o)
            self.slots[sid].state = replace(st, orders=tuple(kept))

        logger.info("Reconciliation: %d tracked open orders (dropped %d unbound)", known, dropped)
        return open_orders

    def _replay_missed_fills(self, open_orders: dict) -> None:
        """
        Exactly-once restart replay:
        - look for tracked txids no longer open
        - aggregate Kraken trades history by ordertxid
        - emit synthetic fill events once
        """
        tracked: dict[str, tuple[int, str, int, str, str, int]] = {}
        for sid, slot in self.slots.items():
            for o in slot.state.orders:
                if o.txid:
                    tracked[o.txid] = (sid, "order", o.local_id, o.side, o.trade_id, o.cycle)
            for r in slot.state.recovery_orders:
                if r.txid:
                    tracked[r.txid] = (sid, "recovery", r.recovery_id, r.side, r.trade_id, r.cycle)

        candidates = [
            txid for txid in tracked.keys()
            if txid not in open_orders and txid not in self.seen_fill_txids
        ]
        if not candidates:
            return

        # 7-day replay window is enough for crash/redeploy recovery.
        start_ts = _now() - 7 * 86400
        try:
            history = self._get_trades_history(start=start_ts)
        except Exception as e:
            logger.warning("TradesHistory replay failed: %s", e)
            return
        if not history:
            return

        grouped: dict[str, list[dict]] = {}
        for row in history.values():
            order_txid = row.get("ordertxid", "")
            if order_txid not in tracked:
                continue
            pair_name = str(row.get("pair", "")).upper()
            if pair_name and self.pair not in pair_name and self.pair.replace("USD", "/USD") not in pair_name:
                continue
            grouped.setdefault(order_txid, []).append(row)

        replays = 0
        for txid, rows in grouped.items():
            if txid in self.seen_fill_txids:
                continue
            sid, kind, local_id, side, trade_id, cycle = tracked[txid]

            total_vol = 0.0
            total_cost = 0.0
            total_fee = 0.0
            last_time = 0.0
            for r in rows:
                try:
                    vol = float(r.get("vol", 0.0))
                    fee = float(r.get("fee", 0.0))
                    cost = float(r.get("cost", 0.0))
                    t = float(r.get("time", 0.0))
                except (TypeError, ValueError):
                    continue
                total_vol += vol
                total_cost += cost
                total_fee += fee
                if t > last_time:
                    last_time = t
            if total_vol <= 0:
                continue
            avg_price = total_cost / total_vol if total_cost > 0 else 0.0
            if avg_price <= 0:
                continue

            self.seen_fill_txids.add(txid)
            if kind == "order":
                supabase_store.save_fill(
                    {
                        "time": last_time or _now(),
                        "side": side,
                        "price": avg_price,
                        "volume": total_vol,
                        "profit": 0.0,
                        "fees": total_fee,
                    },
                    pair=self.pair,
                    trade_id=trade_id,
                    cycle=cycle,
                )
                ev = sm.FillEvent(
                    order_local_id=local_id,
                    txid=txid,
                    side=side,
                    price=avg_price,
                    volume=total_vol,
                    fee=total_fee,
                    timestamp=last_time or _now(),
                )
                self._apply_event(sid, ev, "fill_replay", {"txid": txid, "price": avg_price, "volume": total_vol})
            else:
                ev = sm.RecoveryFillEvent(
                    recovery_id=local_id,
                    txid=txid,
                    side=side,
                    price=avg_price,
                    volume=total_vol,
                    fee=total_fee,
                    timestamp=last_time or _now(),
                )
                self._apply_event(sid, ev, "recovery_fill_replay", {"txid": txid, "price": avg_price, "volume": total_vol})
            replays += 1

        if replays:
            logger.info("Replayed %d missed fills from trade history", replays)

    def _ensure_slot_bootstrapped(self, slot_id: int) -> None:
        slot = self.slots[slot_id]
        if slot.state.orders:
            return

        balance = self._safe_balance()
        if balance is None:
            logger.warning(
                "slot %s bootstrap deferred: balance unavailable (live+cache unavailable)",
                slot_id,
            )
            return
        usd = self.ledger.available_usd if self.ledger._synced else _usd_balance(balance)
        doge = self.ledger.available_doge if self.ledger._synced else _doge_balance(balance)
        market = self.last_price

        min_vol, min_cost = self._minimum_bootstrap_requirements(market)

        cfg = self._engine_cfg(slot)

        now_ts = _now()
        suppressed = None
        cooldown_suppressed = (
            self._regime_cooldown_suppressed_side
            if self._regime_cooldown_suppressed_side in ("A", "B")
            else None
        )
        if self._flag_value("REGIME_DIRECTIONAL_ENABLED"):
            if int(self._regime_tier) == 2 and self._regime_grace_elapsed(now_ts):
                if self._regime_side_suppressed in ("A", "B"):
                    suppressed = self._regime_side_suppressed
            elif (
                self._regime_tier2_last_downgrade_at > 0
                and cooldown_suppressed in ("A", "B")
            ):
                cooldown_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
                elapsed = now_ts - self._regime_tier2_last_downgrade_at
                if elapsed < cooldown_sec:
                    suppressed = cooldown_suppressed

        if suppressed == "A":
            if usd >= min_cost:
                st = replace(slot.state, long_only=True, short_only=False, mode_source="regime")
                st, action = sm.add_entry_order(
                    st,
                    cfg,
                    side="buy",
                    trade_id="B",
                    cycle=st.cycle_b,
                    order_size_usd=self._slot_order_size_usd(slot, trade_id="B"),
                    reason="bootstrap_regime_long_only",
                )
                self.slots[slot_id].state = st
                if action:
                    self._execute_actions(slot_id, [action], "bootstrap_regime")
                else:
                    logger.info("slot %s bootstrap_regime waiting: buy entry below minimum", slot_id)
            else:
                logger.info(
                    "slot %s bootstrap waiting: regime suppresses A, insufficient USD for B",
                    slot_id,
                )
            return

        if suppressed == "B":
            if doge >= min_vol:
                st = replace(slot.state, short_only=True, long_only=False, mode_source="regime")
                st, action = sm.add_entry_order(
                    st,
                    cfg,
                    side="sell",
                    trade_id="A",
                    cycle=st.cycle_a,
                    order_size_usd=self._slot_order_size_usd(slot),
                    reason="bootstrap_regime_short_only",
                )
                self.slots[slot_id].state = st
                if action:
                    self._execute_actions(slot_id, [action], "bootstrap_regime")
                else:
                    logger.info("slot %s bootstrap_regime waiting: sell entry below minimum", slot_id)
            else:
                logger.info(
                    "slot %s bootstrap waiting: regime suppresses B, insufficient DOGE for A",
                    slot_id,
                )
            return

        # Normal bootstrap: both sides available.
        if doge >= min_vol and usd >= min_cost:
            st = slot.state
            actions: list[sm.Action] = []
            st, a1 = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=self._slot_order_size_usd(slot), reason="bootstrap_A")
            if a1:
                actions.append(a1)
            st, a2 = sm.add_entry_order(st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=self._slot_order_size_usd(slot, trade_id="B"), reason="bootstrap_B")
            if a2:
                actions.append(a2)
            self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")
            if actions:
                self._execute_actions(slot_id, actions, "bootstrap")
            else:
                target_usd = self._slot_order_size_usd(slot)
                min_vol = float(self.constraints.get("min_volume", 13.0))
                min_cost = float(self.constraints.get("min_cost_usd", 0.0))
                required_usd = max(min_cost, min_vol * market)
                logger.info(
                    "slot %s bootstrap waiting: target $%.4f < required $%.4f "
                    "(ORDER_SIZE_USD=$%.4f, total_profit=$%.4f, min_vol=%.1f, "
                    "min_cost=$%.4f, market=$%.6f)",
                    slot_id, target_usd, required_usd,
                    float(config.ORDER_SIZE_USD), slot.state.total_profit,
                    min_vol, min_cost, market,
                )
            return

        # Symmetric auto-reseed.
        if usd < min_cost and doge >= 2 * min_vol:
            st = replace(slot.state, short_only=True, long_only=False, mode_source="balance")
            target_usd = market * (2 * min_vol)
            st, a = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=target_usd, reason="reseed_usd")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "bootstrap_reseed_usd")
            else:
                logger.info("slot %s reseed_usd waiting: computed order below minimum", slot_id)
            return

        if doge < min_vol and usd >= 2 * min_cost:
            st = replace(slot.state, long_only=True, short_only=False, mode_source="balance")
            target_usd = market * (2 * min_vol)
            st, a = sm.add_entry_order(st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=target_usd, reason="reseed_doge")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "bootstrap_reseed_doge")
            else:
                logger.info("slot %s reseed_doge waiting: computed order below minimum", slot_id)
            return

        # Graceful degradation fallback: place whichever side can run.
        if doge >= min_vol:
            st = replace(slot.state, short_only=True, long_only=False, mode_source="balance")
            st, a = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=market * min_vol, reason="fallback_short_only")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "fallback_short_only")
            else:
                logger.info("slot %s fallback_short_only waiting: computed order below minimum", slot_id)
            return

        if usd >= min_cost:
            st = replace(slot.state, long_only=True, short_only=False, mode_source="balance")
            st, a = sm.add_entry_order(st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=market * min_vol, reason="fallback_long_only")
            self.slots[slot_id].state = st
            if a:
                self._execute_actions(slot_id, [a], "fallback_long_only")
            else:
                logger.info("slot %s fallback_long_only waiting: computed order below minimum", slot_id)
            return

        logger.warning(
            "slot %s bootstrap blocked: usd=%.8f doge=%.8f min_cost=%.8f min_vol=%.8f market=%.8f keys=%s",
            slot_id,
            usd,
            doge,
            min_cost,
            min_vol,
            market,
            sorted(balance.keys()),
        )
        self.pause(f"slot {slot_id} cannot bootstrap: insufficient USD and DOGE")

    def _auto_repair_degraded_slot(self, slot_id: int) -> None:
        if self.mode != "RUNNING":
            return

        slot = self.slots[slot_id]
        st = slot.state
        if not (st.long_only or st.short_only):
            return

        if str(getattr(st, "mode_source", "none")) == "regime":
            # Check if the regime still requires suppression.
            # If suppression has lapsed (tier dropped, cooldown expired),
            # clear mode_source and fall through to attempt normal repair.
            still_suppressed = False
            if self._flag_value("REGIME_DIRECTIONAL_ENABLED"):
                now_ts = _now()
                if int(self._regime_tier) == 2 and self._regime_grace_elapsed(now_ts):
                    if self._regime_side_suppressed in ("A", "B"):
                        still_suppressed = True
                elif self._regime_tier2_last_downgrade_at > 0:
                    cd_side = (
                        self._regime_cooldown_suppressed_side
                        if self._regime_cooldown_suppressed_side in ("A", "B")
                        else None
                    )
                    if cd_side:
                        cd_sec = max(0.0, float(getattr(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0)))
                        if now_ts - self._regime_tier2_last_downgrade_at < cd_sec:
                            still_suppressed = True
            if still_suppressed:
                return

        phase = sm.derive_phase(st)
        entries = [o for o in st.orders if o.role == "entry"]
        exits = [o for o in st.orders if o.role == "exit"]

        market = st.market_price or self.last_price
        if market <= 0:
            return
        min_vol, min_cost = self._minimum_bootstrap_requirements(market)

        balance = self._safe_balance()
        if balance is None:
            logger.warning(
                "slot %s auto-repair deferred: balance unavailable (live+cache unavailable)",
                slot_id,
            )
            return
        usd = self.ledger.available_usd if self.ledger._synced else _usd_balance(balance)
        doge = self.ledger.available_doge if self.ledger._synced else _doge_balance(balance)
        cfg = self._engine_cfg(slot)

        repaired_state = st
        actions: list[sm.PlaceOrderAction] = []

        def _queue_entry(
            side: str,
            trade_id: str,
            cycle: int,
            reason: str,
        ) -> None:
            nonlocal repaired_state
            repaired_state, action = sm.add_entry_order(
                repaired_state,
                cfg,
                side=side,
                trade_id=trade_id,
                cycle=cycle,
                order_size_usd=self._slot_order_size_usd(
                    slot,
                    trade_id=trade_id if trade_id in ("A", "B") else None,
                ),
                reason=reason,
            )
            if action:
                actions.append(action)

        if phase == "S0":
            has_buy_entry = any(o.side == "buy" for o in entries)
            has_sell_entry = any(o.side == "sell" for o in entries)
            if st.long_only and has_buy_entry and not has_sell_entry and doge >= min_vol:
                _queue_entry("sell", "A", st.cycle_a, "auto_repair_s0_sell")
            elif st.short_only and has_sell_entry and not has_buy_entry and usd >= min_cost:
                _queue_entry("buy", "B", st.cycle_b, "auto_repair_s0_buy")
        elif phase == "S1a":
            has_buy_exit = any(o.side == "buy" for o in exits)
            has_buy_entry = any(o.side == "buy" for o in entries)
            if st.short_only and has_buy_exit and not has_buy_entry and usd >= min_cost:
                _queue_entry("buy", "B", st.cycle_b, "auto_repair_s1a_buy")
        elif phase == "S1b":
            has_sell_exit = any(o.side == "sell" for o in exits)
            has_sell_entry = any(o.side == "sell" for o in entries)
            if st.long_only and has_sell_exit and not has_sell_entry and doge >= min_vol:
                _queue_entry("sell", "A", st.cycle_a, "auto_repair_s1b_sell")

        if not actions:
            return

        self.slots[slot_id].state = repaired_state
        self._execute_actions(slot_id, list(actions), "auto_repair")

        post = self.slots[slot_id].state
        if any(sm.find_order(post, action.local_id) is not None for action in actions):
            self.slots[slot_id].state = replace(post, long_only=False, short_only=False, mode_source="none")
            self._validate_slot(slot_id)
            logger.info("slot %s auto-repaired degraded %s state", slot_id, phase)

    # ------------------ Exchange IO ------------------

    def _safe_balance(self) -> dict | None:
        def _fresh_cached_balance(now_ts: float) -> dict | None:
            if not self._last_balance_snapshot:
                return None
            # Cached balance can safely bridge brief API-budget starvation.
            max_age = max(60.0, float(config.POLL_INTERVAL_SECONDS) * 3.0)
            if now_ts - self._last_balance_ts > max_age:
                return None
            return dict(self._last_balance_snapshot)

        if self._loop_balance_cache is not None:
            return dict(self._loop_balance_cache)
        now_ts = _now()
        if not self._consume_private_budget(1, "get_balance"):
            return _fresh_cached_balance(now_ts)
        try:
            bal = kraken_client.get_balance()
            self._last_balance_snapshot = dict(bal)
            self._last_balance_ts = now_ts
            if self.enforce_loop_budget:
                self._loop_balance_cache = dict(bal)
            return bal
        except Exception as e:
            logger.warning("Balance query failed: %s", e)
            return _fresh_cached_balance(now_ts)

    def _seed_loop_available_from_balance(self, balance: dict | None) -> bool:
        if self._loop_available_usd is not None and self._loop_available_doge is not None:
            return True
        if self.ledger._synced:
            self._loop_available_usd = self.ledger.available_usd
            self._loop_available_doge = self.ledger.available_doge
            return True
        if balance is None:
            return False
        self._loop_available_usd = _usd_balance(balance)
        self._loop_available_doge = _doge_balance(balance)
        return True

    def _required_notional(self, side: str, volume: float, price: float) -> float:
        if side == "buy":
            return max(0.0, float(volume) * float(price))
        if side == "sell":
            return max(0.0, float(volume))
        return 0.0

    def _try_reserve_loop_funds(self, *, side: str, volume: float, price: float) -> bool:
        if self._loop_available_usd is None or self._loop_available_doge is None:
            if not self._seed_loop_available_from_balance(self._loop_balance_cache):
                if not self._seed_loop_available_from_balance(self._safe_balance()):
                    return False

        req = self._required_notional(side, volume, price)
        if side == "buy":
            if (self._loop_available_usd or 0.0) + 1e-12 < req:
                return False
            self._loop_available_usd = (self._loop_available_usd or 0.0) - req
            return True
        if side == "sell":
            if (self._loop_available_doge or 0.0) + 1e-12 < req:
                return False
            self._loop_available_doge = (self._loop_available_doge or 0.0) - req
            return True
        return True

    def _release_loop_reservation(self, *, side: str, volume: float, price: float) -> None:
        req = self._required_notional(side, volume, price)
        if side == "buy":
            self._loop_available_usd = (self._loop_available_usd or 0.0) + req
        elif side == "sell":
            self._loop_available_doge = (self._loop_available_doge or 0.0) + req

    def _apply_event(self, slot_id: int, event: sm.Event, event_type: str, details: dict) -> None:
        slot = self.slots[slot_id]
        cfg = self._engine_cfg(slot)
        old_phase = sm.derive_phase(slot.state)
        order_sizes = {
            "A": self._slot_order_size_usd(slot, trade_id="A"),
            "B": self._slot_order_size_usd(slot, trade_id="B"),
        }
        order_size = self._slot_order_size_usd(slot)

        new_state, actions = sm.transition(
            slot.state,
            event,
            cfg,
            order_size_usd=order_size,
            order_sizes=order_sizes,
        )
        self.slots[slot_id].state = new_state
        new_phase = sm.derive_phase(new_state)

        self._log_event(
            slot_id=slot_id,
            from_state=old_phase,
            to_state=new_phase,
            event_type=event_type,
            details=details,
        )

        self._execute_actions(slot_id, actions, event_type)
        # Normalize degraded single-sided modes before strict invariant checks.
        self._normalize_slot_mode(slot_id)
        self._validate_slot(slot_id)

    def _execute_actions(self, slot_id: int, actions: list[sm.Action], source: str) -> None:
        if not actions:
            return

        slot = self.slots[slot_id]
        def _mark_entry_fallback_for_insufficient_funds(action: sm.PlaceOrderAction) -> None:
            # Graceful degradation: if an entry cannot be funded now,
            # switch to the side that can keep running.
            if action.role != "entry":
                return
            if action.side == "sell":
                slot.state = replace(slot.state, long_only=True, short_only=False, mode_source="balance")
            elif action.side == "buy":
                slot.state = replace(slot.state, short_only=True, long_only=False, mode_source="balance")

        # Pre-compute order capacity for gating new entries.
        _internal_order_count = self._internal_open_order_count()
        _pair_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
        _safe_ratio = min(1.0, max(0.1, float(config.OPEN_ORDER_SAFETY_RATIO)))
        _order_cap = max(1, int(_pair_limit * _safe_ratio))

        for action in actions:
            if isinstance(action, sm.PlaceOrderAction):
                if action.role == "exit" and action.reason == "entry_fill_exit":
                    slot.state = sm.apply_order_regime_at_entry(
                        slot.state,
                        action.local_id,
                        self._current_regime_id(),
                    )
                # Pause/HALT blocks new entry placement; exits still allowed to reduce state risk.
                if self.mode in ("PAUSED", "HALTED") and action.role == "entry":
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    continue
                if self._price_age_sec() > config.STALE_PRICE_MAX_AGE_SEC:
                    self.pause("stale price data > 60s")
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    continue

                # Capacity gate: block new entry orders when at open-order cap.
                # Exit orders are always allowed (they reduce exposure).
                if action.role == "entry" and _internal_order_count >= _order_cap:
                    logger.warning(
                        "slot %s entry blocked: at order capacity (%d/%d)",
                        slot_id, _internal_order_count, _order_cap,
                    )
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    _mark_entry_fallback_for_insufficient_funds(action)
                    self._normalize_slot_mode(slot_id)
                    continue

                if action.role == "entry" and self.entry_adds_per_loop_used >= self.entry_adds_per_loop_cap:
                    # Bootstrap and auto-repair entries bypass the scheduler cap so
                    # both sides of a slot are placed atomically.
                    if source not in ("bootstrap", "bootstrap_regime", "auto_repair"):
                        self._defer_entry_due_scheduler(slot_id, action, source)
                        continue

                # Retarget B-entry volume at execution time so follow-up entries
                # use fresh balance-aware sizing after the triggering exit event.
                if action.role == "entry" and action.side == "buy" and action.trade_id == "B":
                    cfg = self._engine_cfg(slot)
                    target_usd = self._slot_order_size_usd(slot, trade_id="B")
                    refreshed_vol = sm.compute_order_volume(float(action.price), cfg, float(target_usd))
                    if refreshed_vol is None:
                        logger.info(
                            "slot %s B-entry deferred: target $%.4f below exchange minimum at px=%.6f",
                            slot_id,
                            float(target_usd),
                            float(action.price),
                        )
                        slot.state = sm.remove_order(slot.state, action.local_id)
                        _mark_entry_fallback_for_insufficient_funds(action)
                        self._normalize_slot_mode(slot_id)
                        continue
                    refreshed_vol = float(refreshed_vol)
                    if abs(refreshed_vol - float(action.volume)) > 1e-12:
                        action = replace(action, volume=refreshed_vol)
                        patched_orders: list[sm.OrderState] = []
                        for o in slot.state.orders:
                            if o.local_id == action.local_id:
                                patched_orders.append(replace(o, volume=refreshed_vol))
                            else:
                                patched_orders.append(o)
                        slot.state = replace(slot.state, orders=tuple(patched_orders))

                reserved_locally = self._try_reserve_loop_funds(
                    side=action.side,
                    volume=action.volume,
                    price=action.price,
                )
                if not reserved_locally:
                    logger.warning(
                        "slot %s local-funds check blocked %s %s [%s.%s] vol=%.8f px=%.8f (avail usd=%.8f doge=%.8f)",
                        slot_id,
                        action.role,
                        action.side,
                        action.trade_id,
                        action.cycle,
                        action.volume,
                        action.price,
                        self._loop_available_usd or 0.0,
                        self._loop_available_doge or 0.0,
                    )
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    _mark_entry_fallback_for_insufficient_funds(action)
                    self._normalize_slot_mode(slot_id)
                    continue

                try:
                    txid = self._place_order(
                        side=action.side,
                        volume=action.volume,
                        price=action.price,
                        userref=(slot_id * 1_000_000 + action.local_id),
                    )
                    if not txid:
                        self._release_loop_reservation(
                            side=action.side,
                            volume=action.volume,
                            price=action.price,
                        )
                        slot.state = sm.remove_order(slot.state, action.local_id)
                        _mark_entry_fallback_for_insufficient_funds(action)
                        self._normalize_slot_mode(slot_id)
                        continue
                    slot.state = sm.apply_order_txid(slot.state, action.local_id, txid)
                    if action.role == "exit":
                        self._bind_position_txid_for_exit(
                            int(slot_id),
                            int(action.local_id),
                            str(txid),
                        )
                    state_order = sm.find_order(slot.state, action.local_id)
                    if state_order and abs(float(state_order.volume) - float(action.volume)) > 1e-8:
                        logger.error(
                            "[REBAL] VOLUME DRIFT slot=%s local_id=%s state_vol=%.10f action_vol=%.10f",
                            slot_id,
                            action.local_id,
                            state_order.volume,
                            action.volume,
                        )
                    self.ledger.commit_order(action.side, action.price, action.volume)
                    if action.role == "entry":
                        self.entry_adds_per_loop_used += 1
                        if action.side == "buy" and action.trade_id == "B":
                            dust_bump = self._dust_bump_usd(slot, trade_id="B")
                            if dust_bump > 0.0:
                                self._dust_last_absorbed_usd += dust_bump
                                logger.info(
                                    "DUST ABSORBED: $%.4f into slot %d B-entry (lifetime: $%.4f)",
                                    dust_bump,
                                    slot_id,
                                    self._dust_last_absorbed_usd,
                                )
                    _internal_order_count += 1
                except Exception as e:
                    self._release_loop_reservation(
                        side=action.side,
                        volume=action.volume,
                        price=action.price,
                    )
                    logger.warning("slot %s place failed %s.%s: %s", slot_id, action.trade_id, action.cycle, e)
                    slot.state = sm.remove_order(slot.state, action.local_id)
                    # Graceful degradation: if an entry fails due insufficient funds,
                    # switch slot mode to whichever side can keep running.
                    if "insufficient funds" in str(e).lower():
                        _mark_entry_fallback_for_insufficient_funds(action)
                    self._normalize_slot_mode(slot_id)
                    self.consecutive_api_errors += 1
                    if self.consecutive_api_errors >= config.MAX_CONSECUTIVE_ERRORS:
                        self.pause(f"{self.consecutive_api_errors} consecutive API errors")

            elif isinstance(action, sm.CancelOrderAction):
                if action.txid:
                    try:
                        self._cancel_order(action.txid)
                    except Exception as e:
                        logger.warning("cancel failed %s: %s", action.txid, e)

            elif isinstance(action, sm.OrphanOrderAction):
                # Orphan keeps order live on Kraken as lottery ticket.
                pass

            elif isinstance(action, sm.BookCycleAction):
                text = (
                    f"<b>{self.pair_display} {action.trade_id}.{action.cycle}</b> "
                    f"net ${action.net_profit:.4f} "
                    f"(gross ${action.gross_profit:.4f}, fees ${action.fees:.4f}, settled ${action.settled_usd:.4f})"
                    f"{' [recovery]' if action.from_recovery else ''}"
                )
                notifier._send_message(text)
                self._record_exit_outcome(slot_id, action)

        self._normalize_slot_mode(slot_id)

    def _record_exit_outcome(self, slot_id: int, action: sm.BookCycleAction) -> None:
        slot = self.slots.get(slot_id)
        if slot is None:
            return

        cycle_record = next(
            (
                c
                for c in reversed(slot.state.completed_cycles)
                if c.trade_id == action.trade_id and int(c.cycle) == int(action.cycle)
            ),
            None,
        )
        if cycle_record is None:
            logger.debug(
                "exit_outcomes: cycle record missing for slot=%s trade=%s cycle=%s",
                slot_id,
                action.trade_id,
                action.cycle,
            )
            return

        belief_snapshot = self._apply_cycle_belief_snapshot(
            slot_id=int(slot_id),
            cycle_record=cycle_record,
            now_ts=_now(),
        )

        regime_name, regime_confidence, regime_bias, _, _ = self._policy_hmm_signal()

        against_trend = False
        if regime_name == "BULLISH":
            against_trend = action.trade_id == "A"
        elif regime_name == "BEARISH":
            against_trend = action.trade_id == "B"

        entry_time = float(cycle_record.entry_time or 0.0)
        exit_time = float(cycle_record.exit_time or 0.0)
        if entry_time > 0 and exit_time > 0:
            total_age_sec = max(0.0, exit_time - entry_time)
        else:
            total_age_sec = 0.0

        row = {
            "time": float(exit_time if exit_time > 0 else _now()),
            "pair": str(self.pair),
            "trade": str(action.trade_id),
            "cycle": int(action.cycle),
            "resolution": "recovery" if bool(action.from_recovery) else "normal",
            "from_recovery": bool(action.from_recovery),
            "entry_time": entry_time if entry_time > 0 else None,
            "exit_time": exit_time if exit_time > 0 else None,
            "total_age_sec": float(total_age_sec),
            "entry_price": float(cycle_record.entry_price),
            "exit_price": float(cycle_record.exit_price),
            "volume": float(cycle_record.volume),
            "gross_profit_usd": float(action.gross_profit),
            "fees_usd": float(action.fees),
            "net_profit_usd": float(action.net_profit),
            "regime_at_entry": cycle_record.regime_at_entry,
            "regime_confidence": float(regime_confidence),
            "regime_bias_signal": float(regime_bias),
            "against_trend": bool(against_trend),
            "regime_tier": int(self._regime_tier),
            "posterior_1m": list(
                belief_snapshot.get("posterior_1m")
                or getattr(cycle_record, "posterior_1m", None)
                or [0.0, 1.0, 0.0]
            ),
            "posterior_15m": list(
                belief_snapshot.get("posterior_15m")
                or getattr(cycle_record, "posterior_15m", None)
                or [0.0, 1.0, 0.0]
            ),
            "posterior_1h": list(
                belief_snapshot.get("posterior_1h")
                or getattr(cycle_record, "posterior_1h", None)
                or [0.0, 1.0, 0.0]
            ),
            "entropy_at_entry": float(
                belief_snapshot.get("entropy_at_entry", getattr(cycle_record, "entropy_at_entry", 0.0) or 0.0)
            ),
            "p_switch_at_entry": float(
                belief_snapshot.get("p_switch_at_entry", getattr(cycle_record, "p_switch_at_entry", 0.0) or 0.0)
            ),
            "posterior_at_exit_1m": list(
                belief_snapshot.get("posterior_at_exit_1m")
                or getattr(cycle_record, "posterior_at_exit_1m", None)
                or [0.0, 1.0, 0.0]
            ),
            "posterior_at_exit_15m": list(
                belief_snapshot.get("posterior_at_exit_15m")
                or getattr(cycle_record, "posterior_at_exit_15m", None)
                or [0.0, 1.0, 0.0]
            ),
            "posterior_at_exit_1h": list(
                belief_snapshot.get("posterior_at_exit_1h")
                or getattr(cycle_record, "posterior_at_exit_1h", None)
                or [0.0, 1.0, 0.0]
            ),
            "entropy_at_exit": float(
                belief_snapshot.get("entropy_at_exit", getattr(cycle_record, "entropy_at_exit", 0.0) or 0.0)
            ),
            "p_switch_at_exit": float(
                belief_snapshot.get("p_switch_at_exit", getattr(cycle_record, "p_switch_at_exit", 0.0) or 0.0)
            ),
        }
        supabase_store.save_exit_outcome(row)

    def _poll_order_status(self) -> None:
        # Query active + recovery txids once per loop.
        def _to_float(value: Any) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        def _first_positive(row: dict, *keys: str) -> float:
            for k in keys:
                v = _to_float(row.get(k))
                if v > 0:
                    return v
            return 0.0

        tx_map: dict[str, tuple[int, str, int]] = {}
        for sid, slot in self.slots.items():
            for o in slot.state.orders:
                if o.txid:
                    tx_map[o.txid] = (sid, "order", o.local_id)
            for r in slot.state.recovery_orders:
                if r.txid:
                    tx_map[r.txid] = (sid, "recovery", r.recovery_id)
        for sid, state in self._churner_by_slot.items():
            entry_txid = str(state.entry_txid or "").strip()
            if entry_txid:
                tx_map[entry_txid] = (int(sid), "churner_entry", 0)
            exit_txid = str(state.exit_txid or "").strip()
            if exit_txid:
                tx_map[exit_txid] = (int(sid), "churner_exit", 0)

        if not tx_map:
            return

        try:
            info = self._query_orders_batched(list(tx_map.keys()))
            if not info:
                return
            self.consecutive_api_errors = 0
        except Exception as e:
            self.consecutive_api_errors += 1
            logger.warning("query_orders failed (%d): %s", self.consecutive_api_errors, e)
            if self.consecutive_api_errors >= config.MAX_CONSECUTIVE_ERRORS:
                self.pause(f"{self.consecutive_api_errors} consecutive API errors")
            return

        for txid, row in info.items():
            status = row.get("status", "")
            if txid not in tx_map:
                continue
            sid, kind, local_id = tx_map[txid]

            if status == "closed":
                self._partial_open_seen_txids.discard(txid)
                if txid in self.seen_fill_txids:
                    continue

                volume = _first_positive(row, "vol_exec", "vol")
                # Kraken can report limit price as 0 for closed orders; prefer executed/avg prices.
                price = _first_positive(row, "price_exec", "avg_price", "price")
                if price <= 0 and volume > 0:
                    cost = _to_float(row.get("cost"))
                    if cost > 0:
                        price = cost / volume
                fill_cost = _to_float(row.get("cost"))
                if fill_cost <= 0 and price > 0 and volume > 0:
                    fill_cost = price * volume
                fee = _to_float(row.get("fee"))
                if volume <= 0 or price <= 0:
                    logger.warning(
                        "closed order %s missing fill details (status=%s price=%s avg=%s exec=%s vol_exec=%s vol=%s)",
                        txid,
                        status,
                        row.get("price"),
                        row.get("avg_price"),
                        row.get("price_exec"),
                        row.get("vol_exec"),
                        row.get("vol"),
                    )
                    continue

                if kind == "order":
                    o = sm.find_order(self.slots[sid].state, local_id)
                    if not o:
                        logger.warning("closed order %s not found in slot %s local_id=%s", txid, sid, local_id)
                        continue
                    position_id_for_exit = None
                    if o.role == "exit":
                        position_id_for_exit = self._find_position_for_exit(sid, local_id, txid=txid)
                    closed_ts = _now()
                    if o.placed_at > 0:
                        self._record_fill_duration(closed_ts - o.placed_at, closed_ts)
                    supabase_store.save_fill(
                        {
                            "time": _now(),
                            "side": o.side,
                            "price": price,
                            "volume": volume,
                            "profit": 0.0,
                            "fees": fee,
                        },
                        pair=self.pair,
                        trade_id=o.trade_id,
                        cycle=o.cycle,
                    )
                    ev = sm.FillEvent(
                        order_local_id=local_id,
                        txid=txid,
                        side=o.side,
                        price=price,
                        volume=volume,
                        fee=fee,
                        timestamp=closed_ts,
                    )
                    self._record_fill_event(str(o.trade_id), closed_ts)
                    self._apply_event(sid, ev, "fill", {"txid": txid, "price": price, "volume": volume})
                    if o.role == "entry":
                        slot_after = self.slots.get(int(sid))
                        mapped_exit_price = 0.0
                        if slot_after is not None:
                            mapped_exit = next(
                                (
                                    candidate
                                    for candidate in slot_after.state.orders
                                    if candidate.role == "exit"
                                    and str(candidate.trade_id) == str(o.trade_id)
                                    and int(candidate.cycle) == int(o.cycle)
                                ),
                                None,
                            )
                            if mapped_exit is not None:
                                mapped_exit_price = float(mapped_exit.price or 0.0)
                        self._stamp_belief_entry_metadata(
                            slot_id=int(sid),
                            trade_id=str(o.trade_id),
                            cycle=int(o.cycle),
                            entry_price=float(price),
                            exit_price=float(mapped_exit_price),
                            entry_ts=float(closed_ts),
                        )
                        self._record_position_open_for_entry_fill(
                            slot_id=int(sid),
                            entry_order=o,
                            fill_price=float(price),
                            fill_volume=float(volume),
                            fill_fee=float(fee),
                            fill_cost=float(fill_cost),
                            fill_timestamp=float(closed_ts),
                        )
                    elif o.role == "exit" and position_id_for_exit is not None:
                        self._record_position_close_for_exit_fill(
                            slot_id=int(sid),
                            exit_order=o,
                            fill_price=float(price),
                            fill_fee=float(fee),
                            fill_cost=float(fill_cost),
                            fill_timestamp=float(closed_ts),
                            txid=str(txid),
                        )
                    self.seen_fill_txids.add(txid)
                elif kind == "recovery":
                    r = next((x for x in self.slots[sid].state.recovery_orders if x.recovery_id == local_id), None)
                    if not r:
                        logger.warning("closed recovery %s not found in slot %s recovery_id=%s", txid, sid, local_id)
                        continue
                    ev = sm.RecoveryFillEvent(
                        recovery_id=local_id,
                        txid=txid,
                        side=r.side,
                        price=price,
                        volume=volume,
                        fee=fee,
                        timestamp=_now(),
                    )
                    self._record_fill_event(str(r.trade_id), _now())
                    self._apply_event(sid, ev, "recovery_fill", {"txid": txid, "price": price, "volume": volume})
                    self.seen_fill_txids.add(txid)
                elif kind == "churner_entry":
                    self._churner_on_entry_fill(
                        slot_id=int(sid),
                        txid=str(txid),
                        fill_price=float(price),
                        fill_volume=float(volume),
                        fill_fee=float(fee),
                        fill_cost=float(fill_cost),
                        fill_ts=float(_now()),
                    )
                    self.seen_fill_txids.add(txid)
                elif kind == "churner_exit":
                    self._churner_on_exit_fill(
                        slot_id=int(sid),
                        txid=str(txid),
                        fill_price=float(price),
                        fill_volume=float(volume),
                        fill_fee=float(fee),
                        fill_cost=float(fill_cost),
                        fill_ts=float(_now()),
                    )
                    self.seen_fill_txids.add(txid)

            elif status == "open":
                vol_exec = _to_float(row.get("vol_exec"))
                vol = _to_float(row.get("vol"))
                if vol_exec > 0 and vol > 0 and vol_exec < vol and txid not in self._partial_open_seen_txids:
                    self._record_partial_fill_open(_now())
                    self._partial_open_seen_txids.add(txid)

            elif status in ("canceled", "expired"):
                self._partial_open_seen_txids.discard(txid)
                vol_exec = _to_float(row.get("vol_exec"))
                vol = _to_float(row.get("vol"))
                if vol_exec > 0:
                    self._record_partial_fill_cancel(_now())
                    logger.warning(
                        "PHANTOM_POSITION_CANARY txid=%s kind=%s slot=%s local=%s status=%s vol_exec=%.8f vol=%.8f",
                        txid,
                        kind,
                        sid,
                        local_id,
                        status,
                        vol_exec,
                        vol,
                    )
                if kind == "order":
                    st = self.slots[sid].state
                    if sm.find_order(st, local_id):
                        self.slots[sid].state = sm.remove_order(st, local_id)
                elif kind == "recovery":
                    st = self.slots[sid].state
                    self.slots[sid].state = sm.remove_recovery(st, local_id)
                elif kind in {"churner_entry", "churner_exit"}:
                    self._churner_on_order_canceled(
                        slot_id=int(sid),
                        kind=str(kind),
                        txid=str(txid),
                        now_ts=float(_now()),
                    )

    # ------------------ Invariants ------------------

    def _validate_slot(self, slot_id: int) -> None:
        st = self.slots[slot_id].state
        violations = sm.check_invariants(st)
        if violations:
            # Hotfix: if order size is intentionally below Kraken minimum, slot may
            # legally sit in an empty/incomplete S0 waiting state. Do not hard halt.
            if self._is_min_size_wait_state(slot_id, violations):
                logger.info(
                    "Slot %s in min-size wait state; skipping invariant halt (%s)",
                    slot_id,
                    violations[0],
                )
                return
            if self._is_bootstrap_pending_state(slot_id, violations):
                logger.info(
                    "Slot %s bootstrap pending; skipping invariant halt (%s)",
                    slot_id,
                    violations[0],
                )
                return
            self.halt(f"slot {slot_id} invariant violation: {violations[0]}")
            logger.error("Slot %s invariant violations: %s", slot_id, violations)

    def _is_min_size_wait_state(self, slot_id: int, violations: list[str]) -> bool:
        if not violations:
            return False
        if any(v != "S0 must be exactly A sell entry + B buy entry" for v in violations):
            return False

        slot = self.slots[slot_id]
        st = slot.state
        if sm.derive_phase(st) != "S0":
            return False
        if any(o.role == "exit" for o in st.orders):
            return False

        target_usd = self._slot_order_size_usd(slot)
        market = st.market_price or self.last_price
        if market <= 0:
            return False

        min_vol = float(self.constraints.get("min_volume", 13.0))
        min_cost = float(self.constraints.get("min_cost_usd", 0.0))
        required_usd = max(min_cost, min_vol * market)
        return target_usd < required_usd

    def _is_bootstrap_pending_state(self, slot_id: int, violations: list[str]) -> bool:
        if not violations:
            return False
        allowed = {
            "S0 must be exactly A sell entry + B buy entry",
            "S0 long_only must be exactly one buy entry",
            "S0 short_only must be exactly one sell entry",
        }
        if any(v not in allowed for v in violations):
            return False
        st = self.slots[slot_id].state
        if sm.derive_phase(st) != "S0":
            return False
        entries = [o for o in st.orders if o.role == "entry"]
        exits = [o for o in st.orders if o.role == "exit"]
        if exits or len(entries) > 1:
            return False
        # Recoverable startup/placement gap: allow empty/one-entry S0 briefly.
        if not entries:
            return True
        if st.long_only and entries[0].side != "buy":
            return False
        if st.short_only and entries[0].side != "sell":
            return False
        return True

    def _normalize_slot_mode(self, slot_id: int) -> None:
        st = self.slots[slot_id].state
        entries = [o for o in st.orders if o.role == "entry"]
        exits = [o for o in st.orders if o.role == "exit"]
        one_sided_source = "regime" if str(getattr(st, "mode_source", "none")) == "regime" else "balance"
        if not entries and not exits:
            # Prevent stale snapshot flags from causing false S0 single-sided halts.
            self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")
            return
        if exits:
            # Degraded S1 states are legal when only one exit side survives
            # (e.g., loop API budget skipped replacing the missing entry).
            if not entries and len(exits) == 1:
                exit_side = exits[0].side
                if exit_side == "sell":
                    self.slots[slot_id].state = replace(
                        st, long_only=True, short_only=False, mode_source=one_sided_source
                    )
                elif exit_side == "buy":
                    self.slots[slot_id].state = replace(
                        st, long_only=False, short_only=True, mode_source=one_sided_source
                    )
            elif len(exits) == 2:
                self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")
            elif len(exits) == 1 and len(entries) == 1 and entries[0].side == exits[0].side:
                # Normal S1 shape (exit + same-side entry) should not keep degraded flags.
                self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")
            return
        buy_entries = [o for o in entries if o.side == "buy"]
        sell_entries = [o for o in entries if o.side == "sell"]
        if len(buy_entries) == 1 and len(sell_entries) == 0:
            self.slots[slot_id].state = replace(st, long_only=True, short_only=False, mode_source=one_sided_source)
        elif len(sell_entries) == 1 and len(buy_entries) == 0:
            self.slots[slot_id].state = replace(st, long_only=False, short_only=True, mode_source=one_sided_source)
        elif len(sell_entries) == 1 and len(buy_entries) == 1:
            self.slots[slot_id].state = replace(st, long_only=False, short_only=False, mode_source="none")

    # ------------------ Commands ------------------

    def add_slot(self) -> tuple[bool, str]:
        if self.mode == "HALTED":
            return False, "bot halted"
        sid = self.next_slot_id
        self.next_slot_id += 1
        alias = self._allocate_slot_alias()
        st = sm.PairState(
            market_price=self.last_price,
            now=_now(),
            profit_pct_runtime=self.profit_pct,
        )
        self.slots[sid] = SlotRuntime(slot_id=sid, state=st, alias=alias)
        self._ensure_slot_bootstrapped(sid)
        self._save_snapshot()
        return True, f"slot {sid} ({alias}) added"

    def add_layer(self, source: str | None = None) -> tuple[bool, str]:
        if self._layer_action_in_flight:
            return False, "layer action already in progress"
        self._layer_action_in_flight = True
        try:
            src = str(source or config.CAPITAL_LAYER_DEFAULT_SOURCE).strip().upper()
            if src not in {"AUTO", "DOGE", "USD"}:
                return False, f"invalid layer funding source: {src}"

            max_target_layers = max(1, int(getattr(config, "CAPITAL_LAYER_MAX_TARGET_LAYERS", 20)))
            current_target_layers = max(0, int(self.target_layers))
            if current_target_layers >= max_target_layers:
                return False, f"layer add rejected: target limit {max_target_layers} reached"

            step_doge_eq = self._capital_layer_step_doge_eq()
            if step_doge_eq <= 0:
                return False, "layer add rejected: invalid layer step"

            price = self._layer_mark_price()
            if price <= 0:
                return False, "layer add rejected: market price unavailable"

            free_usd, free_doge = self._available_free_balances(prefer_fresh=True)
            required_usd = step_doge_eq * price
            available_doge_eq = free_doge + (free_usd / price)

            ok = False
            if src == "DOGE":
                ok = free_doge + 1e-12 >= step_doge_eq
            elif src == "USD":
                ok = free_usd + 1e-12 >= required_usd
            else:
                ok = available_doge_eq + 1e-12 >= step_doge_eq

            if not ok:
                if src == "DOGE":
                    return False, f"layer add rejected: need {step_doge_eq:.0f} DOGE, available {free_doge:.4f} DOGE"
                if src == "USD":
                    return False, f"layer add rejected: need ${required_usd:.4f}, available ${free_usd:.4f}"
                return (
                    False,
                    f"layer add rejected: need {step_doge_eq:.0f} DOGE-eq, available {available_doge_eq:.4f} DOGE-eq",
                )

            self.target_layers = current_target_layers + 1
            self.layer_last_add_event = {
                "timestamp": _now(),
                "source": src,
                "price_at_commit": float(price),
                "usd_equiv_at_commit": float(required_usd),
            }
            self._recompute_effective_layers(mark_price=price)
            self._loop_effective_layers = None
            self._save_snapshot()
            return (
                True,
                f"layer added: target={self.target_layers} (+{config.CAPITAL_LAYER_DOGE_PER_ORDER:.0f} DOGE/order), "
                f"commit step {step_doge_eq:.0f} DOGE-eq @ ${price:.4f}",
            )
        finally:
            self._layer_action_in_flight = False

    def remove_layer(self) -> tuple[bool, str]:
        if self._layer_action_in_flight:
            return False, "layer action already in progress"
        self._layer_action_in_flight = True
        try:
            if int(self.target_layers) <= 0:
                return False, "layer remove rejected: target already zero"
            self.target_layers = int(self.target_layers) - 1
            self._recompute_effective_layers()
            self._loop_effective_layers = None
            self._save_snapshot()
            target_doge_per_order = float(self.target_layers) * max(0.0, float(config.CAPITAL_LAYER_DOGE_PER_ORDER))
            return True, f"layer removed: target={self.target_layers} (+{target_doge_per_order:.3f} DOGE/order)"
        finally:
            self._layer_action_in_flight = False

    def set_entry_pct(self, value: float) -> tuple[bool, str]:
        if value < 0.05:
            return False, "entry_pct must be >= 0.05"
        self.entry_pct = float(value)
        self._save_snapshot()
        return True, f"entry_pct set to {self.entry_pct:.3f}%"

    def set_profit_pct(self, value: float) -> tuple[bool, str]:
        fee_floor = self.maker_fee_pct * 2.0 + 0.1
        if value < fee_floor:
            return False, f"profit_pct must be >= {fee_floor:.3f}%"
        self.profit_pct = float(value)
        self._save_snapshot()
        return True, f"profit_pct set to {self.profit_pct:.3f}%"

    def soft_close(self, slot_id: int, recovery_id: int) -> tuple[bool, str]:
        if not self._recovery_orders_enabled():
            return False, _recovery_disabled_message("soft_close")
        if self._flag_value("STICKY_MODE_ENABLED"):
            return False, "soft_close disabled in sticky mode; use release_slot"
        slot = self.slots.get(slot_id)
        if not slot:
            return False, f"unknown slot {slot_id}"

        rec = next((r for r in slot.state.recovery_orders if r.recovery_id == recovery_id), None)
        if not rec:
            return False, f"unknown recovery id {recovery_id}"

        # Soft close = cancel old orphan and re-place nearer to market.
        if rec.txid:
            try:
                self._cancel_order(rec.txid)
            except Exception as e:
                logger.warning("soft close cancel failed %s: %s", rec.txid, e)

        side = rec.side
        if side == "sell":
            new_price = round(self.last_price * (1 + self.entry_pct / 100.0), self.constraints["price_decimals"])
        else:
            new_price = round(self.last_price * (1 - self.entry_pct / 100.0), self.constraints["price_decimals"])

        try:
            txid = self._place_order(
                side=side,
                volume=rec.volume,
                price=new_price,
                userref=(slot_id * 1_000_000 + 900_000 + recovery_id),
            )
            if not txid:
                return False, "soft-close skipped: API loop budget exceeded"
        except Exception as e:
            return False, f"soft-close placement failed: {e}"

        patched = []
        for r in slot.state.recovery_orders:
            if r.recovery_id == recovery_id:
                patched.append(replace(r, price=new_price, txid=txid, reason="soft_close"))
            else:
                patched.append(r)
        slot.state = replace(slot.state, recovery_orders=tuple(patched))
        self._save_snapshot()
        return True, f"soft-close repriced recovery {recovery_id}"

    def soft_close_next(self) -> tuple[bool, str]:
        if not self._recovery_orders_enabled():
            return False, _recovery_disabled_message("soft_close_next")
        if self._flag_value("STICKY_MODE_ENABLED"):
            return False, "soft_close_next disabled in sticky mode; use release_slot"
        oldest: tuple[int, sm.RecoveryOrder] | None = None
        for sid, slot in self.slots.items():
            for r in slot.state.recovery_orders:
                if oldest is None or r.orphaned_at < oldest[1].orphaned_at:
                    oldest = (sid, r)
        if not oldest:
            return False, "no recovery orders"
        return self.soft_close(oldest[0], oldest[1].recovery_id)

    def remove_slot(self, slot_id: int) -> tuple[bool, str]:
        """Remove a slot entirely, cancelling all its open orders on Kraken."""
        slot = self.slots.get(slot_id)
        if not slot:
            return False, f"unknown slot {slot_id}"

        cancelled = 0
        failed = 0

        # Cancel all active orders for this slot.
        for o in slot.state.orders:
            if o.txid:
                try:
                    ok = self._cancel_order(o.txid)
                    if ok:
                        cancelled += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.warning("remove_slot: cancel order %s failed: %s", o.txid, e)
                    failed += 1

        # Cancel all recovery orders for this slot.
        for r in slot.state.recovery_orders:
            if r.txid:
                try:
                    ok = self._cancel_order(r.txid)
                    if ok:
                        cancelled += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.warning("remove_slot: cancel recovery %s failed: %s", r.txid, e)
                    failed += 1

        churner = self._churner_by_slot.get(int(slot_id))
        if churner is not None:
            for txid in (str(churner.entry_txid or "").strip(), str(churner.exit_txid or "").strip()):
                if not txid:
                    continue
                try:
                    ok = self._cancel_order(txid)
                    if ok:
                        cancelled += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.warning("remove_slot: cancel churner %s failed: %s", txid, e)
                    failed += 1

        if failed > 0:
            return False, f"slot {slot_id}: {failed} cancel failures, not removed (retry)"

        self._release_slot_alias(slot.alias)
        if churner is not None:
            self._churner_release_reserve(churner)
            self._churner_by_slot.pop(int(slot_id), None)
        del self.slots[slot_id]
        self._save_snapshot()

        msg = f"slot {slot_id} removed, cancelled {cancelled} orders"
        logger.info("remove_slot: %s", msg)
        return True, msg

    def remove_slots(self, count: int = 1) -> tuple[bool, str]:
        """Remove N slots from the top (highest slot IDs first)."""
        if count < 1:
            return False, "count must be >= 1"
        if count > len(self.slots):
            return False, f"only {len(self.slots)} slots exist"

        removed = []
        for sid in sorted(self.slots.keys(), reverse=True)[:count]:
            ok, msg = self.remove_slot(sid)
            if not ok:
                return False, f"stopped after removing {len(removed)}: {msg}"
            removed.append(sid)

        return True, f"removed {len(removed)} slots: {removed}"

    def _auto_soft_close_if_capacity_pressure(self) -> None:
        """Soft-close farthest recovery orders when capacity utilization is high.

        Triggered each main-loop cycle.  Processes a small batch (default 2)
        per cycle to stay within rate limits while steadily draining orphans.
        """
        if not self._recovery_orders_enabled():
            return
        cap_threshold = float(config.AUTO_SOFT_CLOSE_CAPACITY_PCT)
        batch_size = max(1, min(int(config.AUTO_SOFT_CLOSE_BATCH), 5))

        # Use Kraken-reported count if available, else internal count.
        if self._kraken_open_orders_current is not None:
            current = int(self._kraken_open_orders_current)
        else:
            current = self._internal_open_order_count()

        pair_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
        utilization_pct = current / pair_limit * 100.0

        if utilization_pct < cap_threshold:
            return

        # Collect all recoveries with distance from market, sorted farthest first.
        if self.last_price <= 0:
            return
        candidates: list[tuple[float, int, sm.RecoveryOrder]] = []
        for sid in self.slots:
            for r in self.slots[sid].state.recovery_orders:
                dist = abs(r.price - self.last_price) / self.last_price * 100.0
                candidates.append((dist, sid, r))

        if not candidates:
            return

        candidates.sort(key=lambda t: t[0], reverse=True)
        batch = candidates[:batch_size]

        repriced = 0
        for _dist, sid, rec in batch:
            slot = self.slots[sid]

            # Cancel old order.
            if rec.txid:
                try:
                    ok = self._cancel_order(rec.txid)
                    if not ok:
                        continue
                except Exception:
                    continue

            # Place near market.
            if rec.side == "sell":
                new_price = round(self.last_price * (1 + self.entry_pct / 100.0), self.constraints["price_decimals"])
            else:
                new_price = round(self.last_price * (1 - self.entry_pct / 100.0), self.constraints["price_decimals"])

            try:
                txid = self._place_order(
                    side=rec.side,
                    volume=rec.volume,
                    price=new_price,
                    userref=(sid * 1_000_000 + 900_000 + rec.recovery_id),
                )
            except Exception:
                txid = None

            if not txid:
                # Clear txid so poller won't silently drop on next cycle.
                slot.state = replace(slot.state, recovery_orders=tuple(
                    replace(x, txid="", reason="auto_close_place_failed") if x.recovery_id == rec.recovery_id else x
                    for x in slot.state.recovery_orders
                ))
                continue

            slot.state = replace(slot.state, recovery_orders=tuple(
                replace(x, price=new_price, txid=txid, reason="auto_soft_close") if x.recovery_id == rec.recovery_id else x
                for x in slot.state.recovery_orders
            ))
            repriced += 1

        if repriced > 0:
            self._auto_soft_close_total += repriced
            self._auto_soft_close_last_at = _now()
            logger.info(
                "auto_soft_close: repriced %d/%d farthest recoveries (capacity %.0f%% >= %.0f%% threshold, lifetime %d)",
                repriced, len(batch), utilization_pct, cap_threshold, self._auto_soft_close_total,
            )
            notifier._send_message(
                f"<b>Auto soft-close</b>\nCapacity {utilization_pct:.0f}% — "
                f"repriced {repriced} farthest recoveries near market"
            )

    def _auto_drain_recovery_backlog(self) -> None:
        """Force-close a small number of recoveries to reduce persistent backlog.

        Priority is deterministic: furthest-from-market first, then oldest.
        """
        if not self._recovery_orders_enabled():
            return
        if not bool(config.AUTO_RECOVERY_DRAIN_ENABLED):
            return
        if self.last_price <= 0:
            return

        max_per_loop = max(1, min(int(config.AUTO_RECOVERY_DRAIN_MAX_PER_LOOP), 5))
        if max_per_loop <= 0:
            return

        total_recoveries = sum(len(slot.state.recovery_orders) for slot in self.slots.values())
        if total_recoveries <= 0:
            return

        slot_count = max(1, len(self.slots))
        target_total = max(0, int(config.MAX_RECOVERY_SLOTS)) * slot_count
        excess = max(0, total_recoveries - target_total)

        if self._kraken_open_orders_current is not None:
            open_current = int(self._kraken_open_orders_current)
        else:
            open_current = self._internal_open_order_count()
        pair_limit = max(1, int(config.KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT))
        utilization_pct = open_current / pair_limit * 100.0
        pressure_threshold = float(config.AUTO_RECOVERY_DRAIN_CAPACITY_PCT)
        pressure = utilization_pct >= pressure_threshold

        if excess <= 0 and not pressure:
            return

        drain_target = min(max_per_loop, excess if excess > 0 else total_recoveries)
        if drain_target <= 0:
            return

        candidates: list[tuple[float, float, int, int, sm.RecoveryOrder]] = []
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            for rec in st.recovery_orders:
                dist = abs(float(rec.price) - float(self.last_price)) / float(self.last_price)
                candidates.append((-dist, float(rec.orphaned_at), int(rec.recovery_id), sid, rec))
        if not candidates:
            return
        candidates.sort()

        drained = 0
        now_ts = _now()
        for _neg_dist, _orphaned_at, _rid, sid, rec in candidates:
            if drained >= drain_target:
                break
            slot = self.slots.get(sid)
            if not slot:
                continue
            live = next((r for r in slot.state.recovery_orders if r.recovery_id == rec.recovery_id), None)
            if not live:
                continue

            if live.txid:
                try:
                    ok = self._cancel_order(live.txid)
                    if not ok:
                        continue
                except Exception as e:
                    logger.warning("auto_drain: cancel recovery %s failed: %s", live.txid, e)
                    continue

            fill_price = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
            if fill_price <= 0:
                fill_price = float(live.price if live.price > 0 else live.entry_price)
            if fill_price <= 0:
                continue
            fill_fee = max(0.0, fill_price * float(live.volume) * (float(self.maker_fee_pct) / 100.0))
            ev = sm.RecoveryFillEvent(
                recovery_id=int(live.recovery_id),
                txid=str(live.txid or ""),
                side=live.side,
                price=fill_price,
                volume=float(live.volume),
                fee=fill_fee,
                timestamp=now_ts,
            )
            self._apply_event(
                sid,
                ev,
                "recovery_auto_drain",
                {
                    "recovery_id": int(live.recovery_id),
                    "fill_price": fill_price,
                    "fill_fee": fill_fee,
                    "reason": "auto_recovery_drain",
                },
            )
            drained += 1

        if drained > 0:
            self._auto_recovery_drain_total += drained
            self._auto_recovery_drain_last_at = now_ts
            logger.info(
                "auto_recovery_drain: drained %d recoveries (excess=%d pressure=%s util=%.1f%% target_total=%d lifetime=%d)",
                drained,
                excess,
                pressure,
                utilization_pct,
                target_total,
                self._auto_recovery_drain_total,
            )

    def cancel_stale_recoveries(self, min_distance_pct: float = 3.0, max_batch: int = 8) -> tuple[bool, str]:
        """Bulk soft-close recovery orders farther than min_distance_pct from market.

        Reprices them to within entry_pct of market so they fill quickly and
        book P&L through the normal recovery-fill path.  Processes up to
        max_batch per call to stay within Kraken rate limits (2 API calls each:
        cancel old + place new).  Call repeatedly until remaining == 0.
        """
        if not self._recovery_orders_enabled():
            return False, _recovery_disabled_message("cancel_stale_recoveries")
        if self._flag_value("STICKY_MODE_ENABLED"):
            return False, "cancel_stale_recoveries disabled in sticky mode; use release_slot"
        if self.last_price <= 0:
            return False, "no market price"

        max_batch = max(1, min(max_batch, 20))

        # Collect all stale recoveries across slots.
        stale: list[tuple[int, sm.RecoveryOrder]] = []
        for sid in sorted(self.slots.keys()):
            for r in self.slots[sid].state.recovery_orders:
                distance_pct = abs(r.price - self.last_price) / self.last_price * 100.0
                if distance_pct >= min_distance_pct:
                    stale.append((sid, r))

        if not stale:
            return True, "no stale recoveries"

        batch = stale[:max_batch]
        remaining = len(stale) - len(batch)

        repriced = 0
        failed = 0

        # Bypass per-loop budget — this is a user-initiated bulk operation.
        saved_enforce = self.enforce_loop_budget
        self.enforce_loop_budget = False
        try:
            for sid, rec in batch:
                slot = self.slots[sid]

                # Cancel old order on Kraken.
                # _cancel_order returns False on failure (not just exception).
                if rec.txid:
                    try:
                        ok = self._cancel_order(rec.txid)
                        if not ok:
                            logger.warning("cancel_stale: cancel %s returned False", rec.txid)
                            failed += 1
                            continue
                    except Exception as e:
                        logger.warning("cancel_stale: cancel %s failed: %s", rec.txid, e)
                        failed += 1
                        continue

                # Place new order near market.
                if rec.side == "sell":
                    new_price = round(self.last_price * (1 + self.entry_pct / 100.0), self.constraints["price_decimals"])
                else:
                    new_price = round(self.last_price * (1 - self.entry_pct / 100.0), self.constraints["price_decimals"])

                try:
                    txid = self._place_order(
                        side=rec.side,
                        volume=rec.volume,
                        price=new_price,
                        userref=(sid * 1_000_000 + 900_000 + rec.recovery_id),
                    )
                except Exception as e:
                    logger.warning("cancel_stale: place failed after cancel: %s", e)
                    txid = None

                if not txid:
                    # Cancel succeeded but place failed — clear txid so the
                    # poller doesn't see a "cancelled" order and silently drop
                    # the recovery.  It stays in state for retry next call.
                    slot.state = replace(slot.state, recovery_orders=tuple(
                        replace(x, txid="", reason="place_failed") if x.recovery_id == rec.recovery_id else x
                        for x in slot.state.recovery_orders
                    ))
                    failed += 1
                    continue

                # Update recovery in-place with new price/txid.
                slot.state = replace(slot.state, recovery_orders=tuple(
                    replace(x, price=new_price, txid=txid, reason="soft_close") if x.recovery_id == rec.recovery_id else x
                    for x in slot.state.recovery_orders
                ))
                repriced += 1
        finally:
            self.enforce_loop_budget = saved_enforce

        if repriced > 0 or failed > 0:
            self._save_snapshot()
        msg = f"repriced {repriced} stale recoveries to within {self.entry_pct:.1f}% of market"
        if failed:
            msg += f", {failed} failures"
        if remaining > 0:
            msg += f", {remaining} remaining (call again)"
        return True, msg

    def _trend_strength_proxy_adx(self, period: int = 14) -> float:
        """
        Lightweight trend-strength proxy mapped to ADX-like 0-100 scale.

        Uses close-only directionality (net move / total path) over recent
        samples. This is intentionally conservative for release gating until
        full OHLC ADX is introduced.
        """
        p = max(2, int(period))
        closes = [float(px) for _, px in self.price_history[-(p + 1):] if float(px) > 0]
        if len(closes) < p + 1:
            return 0.0

        path = 0.0
        for i in range(1, len(closes)):
            path += abs(closes[i] - closes[i - 1])
        if path <= 1e-12:
            return 0.0
        net = abs(closes[-1] - closes[0])
        strength = (net / path) * 100.0
        return max(0.0, min(100.0, strength))

    def _slot_unrealized_profit(self, st: sm.PairState) -> float:
        market = float(st.market_price if st.market_price > 0 else self.last_price)
        if market <= 0:
            return 0.0

        total = 0.0
        for o in st.orders:
            if o.role != "exit":
                continue
            if o.entry_price <= 0 or o.volume <= 0:
                continue
            if o.side == "buy":
                total += (o.entry_price - market) * o.volume
            else:
                total += (market - o.entry_price) * o.volume
        for r in st.recovery_orders:
            if r.entry_price <= 0 or r.volume <= 0:
                continue
            if r.side == "buy":
                total += (r.entry_price - market) * r.volume
            else:
                total += (market - r.entry_price) * r.volume
        return total

    def _total_unrealized_profit_locked(self) -> float:
        return sum(self._slot_unrealized_profit(slot.state) for slot in self.slots.values())

    def _balance_recon_locked(self) -> dict | None:
        total_profit = sum(slot.state.total_profit for slot in self.slots.values())
        total_unrealized = self._total_unrealized_profit_locked()
        return self._compute_balance_recon(total_profit, total_unrealized)

    def _update_release_recon_gate_locked(self) -> tuple[bool, str]:
        if not bool(config.RELEASE_RECON_HARD_GATE_ENABLED):
            self._release_recon_blocked = False
            self._release_recon_blocked_reason = ""
            return True, "release recon hard-gate disabled"

        recon = self._balance_recon_locked()
        if not isinstance(recon, dict):
            # No baseline / no balance -> don't hard-block operator action.
            self._release_recon_blocked = False
            self._release_recon_blocked_reason = ""
            return True, "release recon unavailable"

        status = str(recon.get("status") or "")
        drift_pct = float(recon.get("drift_pct", 0.0) or 0.0)
        threshold = float(recon.get("threshold_pct", float(config.BALANCE_RECON_DRIFT_PCT)) or 0.0)
        if status == "DRIFT" and abs(drift_pct) > threshold + 1e-12:
            self._release_recon_blocked = True
            self._release_recon_blocked_reason = (
                f"release blocked by balance recon drift: {drift_pct:+.4f}% > {threshold:.4f}%"
            )
            return False, self._release_recon_blocked_reason

        self._release_recon_blocked = False
        self._release_recon_blocked_reason = ""
        return True, "release recon gate clear"

    def _release_gate_flags(
        self,
        slot: SlotRuntime,
        order: sm.OrderState,
        *,
        now_ts: float,
    ) -> dict[str, float | bool]:
        market = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        if market <= 0:
            market = float(order.price if order.price > 0 else 0.0)
        age_sec = max(0.0, now_ts - float(order.entry_filled_at or order.placed_at or now_ts))
        distance_pct = abs(float(order.price) - market) / market * 100.0 if market > 0 else 0.0
        regime_strength = self._trend_strength_proxy_adx(period=14)

        age_ok = age_sec >= float(config.RELEASE_MIN_AGE_SEC)
        distance_ok = distance_pct >= float(config.RELEASE_MIN_DISTANCE_PCT)
        regime_ok = regime_strength >= float(config.RELEASE_ADX_THRESHOLD)
        return {
            "age_sec": age_sec,
            "distance_pct": distance_pct,
            "regime_strength": regime_strength,
            "age_ok": age_ok,
            "distance_ok": distance_ok,
            "regime_ok": regime_ok,
        }

    def _pick_release_exit(
        self,
        slot: SlotRuntime,
        *,
        local_id: int | None = None,
        trade_id: str | None = None,
    ) -> sm.OrderState | None:
        exits = [o for o in slot.state.orders if o.role == "exit"]
        if not exits:
            return None
        if local_id is not None:
            return next((o for o in exits if int(o.local_id) == int(local_id)), None)
        if trade_id in ("A", "B"):
            candidates = [o for o in exits if o.trade_id == trade_id]
            if candidates:
                candidates.sort(key=lambda o: float(o.entry_filled_at or o.placed_at or 0.0))
                return candidates[0]
        exits.sort(key=lambda o: float(o.entry_filled_at or o.placed_at or 0.0))
        return exits[0]

    def _release_exit_locked(
        self,
        slot_id: int,
        order: sm.OrderState,
        *,
        reason: str,
        now_ts: float,
    ) -> tuple[bool, str]:
        slot = self.slots.get(slot_id)
        if not slot:
            return False, f"unknown slot {slot_id}"

        live = next((o for o in slot.state.orders if o.local_id == order.local_id and o.role == "exit"), None)
        if not live:
            return False, f"slot {slot_id} exit {order.local_id} no longer active"

        if live.txid:
            try:
                ok = self._cancel_order(live.txid)
                if not ok:
                    return False, f"release cancel failed for {live.txid}"
            except Exception as e:
                return False, f"release cancel failed for {live.txid}: {e}"

        fill_price = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
        if fill_price <= 0:
            fill_price = float(live.price if live.price > 0 else live.entry_price)
        if fill_price <= 0:
            return False, "release failed: no valid mark price"

        fill_fee = max(0.0, fill_price * float(live.volume) * (float(self.maker_fee_pct) / 100.0))
        ev = sm.FillEvent(
            order_local_id=int(live.local_id),
            txid=str(live.txid or ""),
            side=live.side,
            price=fill_price,
            volume=float(live.volume),
            fee=fill_fee,
            timestamp=now_ts,
        )
        self._apply_event(
            slot_id,
            ev,
            "sticky_release",
            {
                "order_local_id": int(live.local_id),
                "trade_id": live.trade_id,
                "fill_price": fill_price,
                "fill_fee": fill_fee,
                "reason": reason,
            },
        )
        self._sticky_release_total += 1
        self._sticky_release_last_at = now_ts
        gate_ok, gate_msg = self._update_release_recon_gate_locked()
        if not gate_ok:
            return True, f"released exit {live.local_id} on slot {slot_id}; {gate_msg}"
        return True, f"released exit {live.local_id} on slot {slot_id} @ ${fill_price:.6f}"

    def _slot_vintage_metrics_locked(self, now_ts: float | None = None) -> dict[str, float | int]:
        now_ts = float(now_ts if now_ts is not None else _now())
        buckets = {
            "fresh_0_1h": 0,
            "aging_1_6h": 0,
            "stale_6_24h": 0,
            "old_1_7d": 0,
            "ancient_7d_plus": 0,
        }
        oldest_age = 0.0
        stuck_capital_usd = 0.0
        release_eligible = 0
        period = 14

        for sid in sorted(self.slots.keys()):
            slot = self.slots[sid]
            for o in slot.state.orders:
                if o.role != "exit":
                    continue
                age_sec = max(0.0, now_ts - float(o.entry_filled_at or o.placed_at or now_ts))
                if age_sec < 3600:
                    buckets["fresh_0_1h"] += 1
                elif age_sec < 6 * 3600:
                    buckets["aging_1_6h"] += 1
                elif age_sec < 24 * 3600:
                    buckets["stale_6_24h"] += 1
                elif age_sec < 7 * 86400:
                    buckets["old_1_7d"] += 1
                else:
                    buckets["ancient_7d_plus"] += 1
                oldest_age = max(oldest_age, age_sec)
                mark = float(slot.state.market_price if slot.state.market_price > 0 else self.last_price)
                if mark > 0:
                    stuck_capital_usd += abs(float(o.volume)) * mark
                flags = self._release_gate_flags(slot, o, now_ts=now_ts)
                if bool(flags.get("age_ok")) and bool(flags.get("distance_ok")) and bool(flags.get("regime_ok")):
                    release_eligible += 1

        bal = self._last_balance_snapshot
        mark = float(self.last_price)
        portfolio_usd = 0.0
        if bal and mark > 0:
            portfolio_usd = _usd_balance(bal) + _doge_balance(bal) * mark
        stuck_capital_pct = (stuck_capital_usd / portfolio_usd * 100.0) if portfolio_usd > 0 else 0.0

        sizes = [self._slot_order_size_usd(self.slots[sid]) for sid in sorted(self.slots.keys())]
        min_size = min(sizes) if sizes else 0.0
        max_size = max(sizes) if sizes else 0.0
        med_size = float(median(sizes)) if sizes else 0.0

        out: dict[str, float | int] = {
            **buckets,
            "oldest_exit_age_sec": float(oldest_age),
            "min_slot_size_usd": float(min_size),
            "median_slot_size_usd": float(med_size),
            "max_slot_size_usd": float(max_size),
            "stuck_capital_usd": float(stuck_capital_usd),
            "stuck_capital_pct": float(stuck_capital_pct),
            "vintage_release_eligible": int(release_eligible),
            "regime_strength_adx_proxy": float(self._trend_strength_proxy_adx(period=period)),
        }
        return out

    def release_slot(
        self,
        slot_id: int,
        local_id: int | None = None,
        trade_id: str | None = None,
    ) -> tuple[bool, str]:
        slot = self.slots.get(int(slot_id))
        if not slot:
            return False, f"unknown slot {slot_id}"

        gate_ok, gate_msg = self._update_release_recon_gate_locked()
        if not gate_ok:
            return False, gate_msg

        order = self._pick_release_exit(slot, local_id=local_id, trade_id=trade_id)
        if not order:
            return False, f"slot {slot_id}: no matching active exit"

        now_ts = _now()
        flags = self._release_gate_flags(slot, order, now_ts=now_ts)
        if not (bool(flags["age_ok"]) and bool(flags["distance_ok"]) and bool(flags["regime_ok"])):
            return (
                False,
                "release blocked by gates: "
                f"age={flags['age_sec']:.0f}s ({'ok' if flags['age_ok'] else 'no'}) "
                f"distance={flags['distance_pct']:.2f}% ({'ok' if flags['distance_ok'] else 'no'}) "
                f"regime={flags['regime_strength']:.2f} ({'ok' if flags['regime_ok'] else 'no'})",
            )
        return self._release_exit_locked(int(slot_id), order, reason="manual_release", now_ts=now_ts)

    def release_oldest_eligible(self, slot_id: int) -> tuple[bool, str]:
        slot = self.slots.get(int(slot_id))
        if not slot:
            return False, f"unknown slot {slot_id}"

        gate_ok, gate_msg = self._update_release_recon_gate_locked()
        if not gate_ok:
            return False, gate_msg

        now_ts = _now()
        exits = [o for o in slot.state.orders if o.role == "exit"]
        exits.sort(key=lambda o: float(o.entry_filled_at or o.placed_at or now_ts))
        for order in exits:
            flags = self._release_gate_flags(slot, order, now_ts=now_ts)
            if bool(flags["age_ok"]) and bool(flags["distance_ok"]) and bool(flags["regime_ok"]):
                return self._release_exit_locked(
                    int(slot_id),
                    order,
                    reason="manual_release_oldest_eligible",
                    now_ts=now_ts,
                )

        return False, f"slot {slot_id}: no release-eligible exits (age/distance/regime)"

    def _auto_release_sticky_slots(self) -> None:
        if not self._flag_value("STICKY_MODE_ENABLED"):
            return
        if not self._flag_value("RELEASE_AUTO_ENABLED"):
            return
        gate_ok, _gate_msg = self._update_release_recon_gate_locked()
        if not gate_ok:
            return

        vintage = self._slot_vintage_metrics_locked(_now())
        stuck_pct = float(vintage.get("stuck_capital_pct", 0.0) or 0.0)
        tier1_threshold = float(config.RELEASE_MAX_STUCK_PCT)
        tier2_threshold = float(config.RELEASE_PANIC_STUCK_PCT)
        if stuck_pct <= tier1_threshold:
            return

        now_ts = _now()
        tier2 = stuck_pct > tier2_threshold
        batch_max = max(1, min(int(config.AUTO_RECOVERY_DRAIN_MAX_PER_LOOP), 5))
        target_pct = float(config.RELEASE_RECOVERY_TARGET_PCT)
        panic_age = float(config.RELEASE_PANIC_MIN_AGE_SEC)

        candidates: list[tuple[float, int, sm.OrderState]] = []
        for sid in sorted(self.slots.keys()):
            slot = self.slots[sid]
            for o in slot.state.orders:
                if o.role != "exit":
                    continue
                flags = self._release_gate_flags(slot, o, now_ts=now_ts)
                age_sec = float(flags["age_sec"])
                if tier2:
                    if age_sec >= panic_age:
                        candidates.append((-age_sec, sid, o))
                else:
                    if bool(flags["age_ok"]) and bool(flags["distance_ok"]) and bool(flags["regime_ok"]):
                        candidates.append((-age_sec, sid, o))
        if not candidates:
            return
        candidates.sort()

        released = 0
        for _neg_age, sid, order in candidates:
            if released >= batch_max:
                break
            ok, _msg = self._release_exit_locked(
                sid,
                order,
                reason=("auto_release_tier2" if tier2 else "auto_release_tier1"),
                now_ts=now_ts,
            )
            if not ok:
                continue
            released += 1
            if self._release_recon_blocked:
                break
            if tier2:
                stuck_pct = float(self._slot_vintage_metrics_locked(now_ts).get("stuck_capital_pct", 0.0) or 0.0)
                if stuck_pct <= target_pct:
                    break

        if released > 0:
            logger.info(
                "auto_release: released %d exits (%s trigger, stuck_capital_pct=%.2f%%)",
                released,
                "tier2" if tier2 else "tier1",
                float(vintage.get("stuck_capital_pct", 0.0) or 0.0),
            )

    def reconcile_drift(self) -> tuple[bool, str]:
        """Cancel Kraken-only orders not tracked internally (drift orders).

        Fetches open orders from Kraken, compares against all known txids
        in slots (orders + recovery_orders), and cancels any pair-matching
        orders that we don't recognize.
        """
        try:
            open_orders = kraken_client.get_open_orders()
        except Exception as e:
            return False, f"failed to fetch open orders: {e}"

        # Build set of all internally tracked txids.
        known_txids: set[str] = set()
        for slot in self.slots.values():
            for o in slot.state.orders:
                if o.txid:
                    known_txids.add(o.txid)
            for r in slot.state.recovery_orders:
                if r.txid:
                    known_txids.add(r.txid)

        # Find pair-matching orders on Kraken that we don't track.
        unknown_txids: list[str] = []
        for txid, row in open_orders.items():
            if not isinstance(row, dict):
                continue
            if not self._order_matches_runtime_pair(row):
                continue
            if txid not in known_txids:
                unknown_txids.append(txid)

        if not unknown_txids:
            self._update_release_recon_gate_locked()
            return True, f"no drift: {len(open_orders)} kraken orders, {len(known_txids)} tracked"

        cancelled = 0
        failed = 0
        for txid in unknown_txids:
            try:
                kraken_client.cancel_order(txid)
                cancelled += 1
            except Exception as e:
                logger.warning("reconcile_drift: cancel %s failed: %s", txid, e)
                failed += 1

        msg = f"cancelled {cancelled}/{len(unknown_txids)} drift orders"
        if failed:
            msg += f", {failed} failures"
        self._update_release_recon_gate_locked()
        return True, msg

    def _pnl_audit_summary(self, tolerance: float = 1e-8) -> dict[str, Any]:
        """Recompute realized P&L from completed cycles."""
        total_profit_state = 0.0
        total_profit_cycles = 0.0
        total_loss_state = 0.0
        total_loss_cycles = 0.0
        total_trips_state = 0
        total_trips_cycles = 0
        bad_slots: list[str] = []

        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            cycle_profit = sum(c.net_profit for c in st.completed_cycles)
            cycle_loss = sum(-c.net_profit for c in st.completed_cycles if c.net_profit < 0)
            cycle_trips = len(st.completed_cycles)

            profit_drift = st.total_profit - cycle_profit
            loss_drift = st.today_realized_loss - cycle_loss
            trips_drift = st.total_round_trips - cycle_trips
            if abs(profit_drift) > tolerance or abs(loss_drift) > tolerance or trips_drift != 0:
                bad_slots.append(f"{sid}(pnl={profit_drift:+.6f},loss={loss_drift:+.6f},trips={trips_drift:+d})")

            total_profit_state += st.total_profit
            total_profit_cycles += cycle_profit
            total_loss_state += st.today_realized_loss
            total_loss_cycles += cycle_loss
            total_trips_state += st.total_round_trips
            total_trips_cycles += cycle_trips

        profit_drift = total_profit_state - total_profit_cycles
        loss_drift = total_loss_state - total_loss_cycles
        trips_drift = total_trips_state - total_trips_cycles
        ok = abs(profit_drift) <= tolerance and abs(loss_drift) <= tolerance and trips_drift == 0 and not bad_slots
        preview = bad_slots[:6]
        more = max(0, len(bad_slots) - len(preview))

        return {
            "ok": ok,
            "tolerance": tolerance,
            "slot_count": len(self.slots),
            "slot_mismatch_count": len(bad_slots),
            "slot_mismatches_preview": preview,
            "slot_mismatches_more": more,
            "profit_drift": profit_drift,
            "loss_drift": loss_drift,
            "trips_drift": trips_drift,
            "total_round_trips_state": total_trips_state,
            "total_round_trips_cycles": total_trips_cycles,
            "total_profit_state": total_profit_state,
            "total_profit_cycles": total_profit_cycles,
            "total_loss_state": total_loss_state,
            "total_loss_cycles": total_loss_cycles,
        }

    def _format_pnl_audit_message(self, summary: dict[str, Any]) -> str:
        if bool(summary.get("ok")):
            return (
                "pnl audit OK: "
                f"slots={int(summary.get('slot_count', 0))} "
                f"trips={int(summary.get('total_round_trips_state', 0))} "
                f"profit_drift={float(summary.get('profit_drift', 0.0)):+.8f} "
                f"loss_drift={float(summary.get('loss_drift', 0.0)):+.8f}"
            )

        preview = list(summary.get("slot_mismatches_preview", []))
        details = ", ".join(str(x) for x in preview)
        more = int(summary.get("slot_mismatches_more", 0))
        if more > 0:
            details += f", +{more} more"
        return (
            "pnl audit mismatch: "
            f"profit_drift={float(summary.get('profit_drift', 0.0)):+.8f} "
            f"loss_drift={float(summary.get('loss_drift', 0.0)):+.8f} "
            f"trips_drift={int(summary.get('trips_drift', 0)):+d}; "
            f"slots={details or 'none'}"
        )

    def audit_pnl(self, tolerance: float = 1e-8) -> tuple[bool, str]:
        """Recompute realized P&L from completed cycles and report drift."""
        summary = self._pnl_audit_summary(tolerance=tolerance)
        return bool(summary.get("ok")), self._format_pnl_audit_message(summary)

    def status_text(self) -> str:
        lines = [
            f"mode: {self.mode}",
            f"pair: {self.pair_display}",
            f"price: ${self.last_price:.6f}",
            f"price_age: {self._price_age_sec():.1f}s",
            f"entry_pct: {self.entry_pct:.3f}%",
            f"profit_pct: {self.profit_pct:.3f}%",
            f"slots: {len(self.slots)}",
        ]
        for sid in sorted(self.slots.keys()):
            st = self.slots[sid].state
            lines.append(
                f"slot {sid}: {sm.derive_phase(st)} A.{st.cycle_a} B.{st.cycle_b} "
                f"orders={len(st.orders)} orphans={len(st.recovery_orders)} pnl=${st.total_profit:.4f}"
            )
        return "\n".join(lines)

    # ------------------ Loop ------------------

    def run_loop_once(self) -> None:
        with self.lock:
            if self.mode == "HALTED":
                self._save_snapshot()
                return

            self._refresh_price(strict=False)
            if self._price_age_sec() > config.STALE_PRICE_MAX_AGE_SEC:
                self.pause("stale price data > 60s")
            loop_now = _now()
            self._sync_ohlcv_candles(loop_now)
            self._update_daily_loss_lock(loop_now)
            self._update_rebalancer(loop_now)
            self._update_micro_features(loop_now)
            self._build_belief_state(loop_now)
            self._maybe_retrain_survival_model(loop_now)
            self._update_regime_tier(loop_now)
            self._update_manifold_score(loop_now)
            self._maybe_schedule_ai_regime(loop_now)
            self._update_accumulation(loop_now)
            self._apply_tier2_suppression(loop_now)
            self._clear_expired_regime_cooldown(loop_now)
            # Prioritize older deferred entries each loop, while avoiding stale placements
            # that the upcoming price tick is likely to refresh anyway.
            self._drain_pending_entry_orders("entry_scheduler_pre_tick", skip_stale=True)

            if self._recon_baseline is None and self.last_price > 0 and self._last_balance_snapshot:
                bal = self._last_balance_snapshot
                self._recon_baseline = {
                    "usd": _usd_balance(bal), "doge": _doge_balance(bal), "ts": _now(),
                }
                logger.info("Balance recon baseline captured: $%.2f + %.1f DOGE",
                            self._recon_baseline["usd"], self._recon_baseline["doge"])

            if self._should_poll_flows(loop_now):
                self._poll_external_flows(loop_now)

            runtime_profit = self._volatility_profit_pct()

            # Tick slots with latest price and timer.
            for sid in sorted(self.slots.keys()):
                st = self.slots[sid].state
                self.slots[sid].state = replace(st, profit_pct_runtime=runtime_profit)

                ev_price = sm.PriceTick(price=self.last_price, timestamp=_now())
                self._apply_event(sid, ev_price, "price_tick", {"price": self.last_price})

                ev_timer = sm.TimerTick(timestamp=_now())
                self._apply_event(sid, ev_timer, "timer_tick", {})

                # If a slot drained its active orders, bootstrap it again.
                self._ensure_slot_bootstrapped(sid)
                # When a slot is in one-sided fallback, try to restore normal mode
                # as soon as balances and API budget allow.
                self._auto_repair_degraded_slot(sid)

            # After all slot transitions/actions, use remaining entry quota.
            self._drain_pending_entry_orders("entry_scheduler_post_tick", skip_stale=False)

            self._poll_order_status()
            self._update_micro_features(_now())
            self._update_trade_beliefs(_now())
            self._run_self_healing_reprice(loop_now)
            self._run_churner_engine(loop_now)
            self._update_daily_loss_lock(_now())
            # Refresh pair open-order telemetry (Kraken source of truth) when budget allows.
            self._refresh_open_order_telemetry()
            # Force-drain a small number of recoveries when backlog/pressure is high.
            self._auto_drain_recovery_backlog()
            # Auto-soft-close farthest recoveries when nearing order capacity.
            self._auto_soft_close_if_capacity_pressure()
            # Keep release hard-gate status fresh and run sticky auto-release tiers.
            self._update_release_recon_gate_locked()
            self._auto_release_sticky_slots()

            # Pressure notice for orphan growth.
            total_orphans = sum(len(s.state.recovery_orders) for s in self.slots.values())
            if total_orphans and total_orphans % int(config.ORPHAN_PRESSURE_WARN_AT) == 0:
                notifier._send_message(f"<b>Orphan pressure</b>\n{total_orphans} recovery orders on book")

            self._update_doge_eq_snapshot(loop_now)
            if self._should_flush_equity_ts(loop_now):
                self._flush_equity_ts(loop_now)

            self._save_snapshot()

    # ------------------ Telegram ------------------

    def poll_telegram(self) -> None:
        callbacks, commands = notifier.poll_updates()

        for cb in callbacks:
            data = cb.get("data", "")
            if data.startswith("sc:"):
                # soft-close callback: sc:<slot>:<recovery>
                try:
                    _, s, r = data.split(":", 2)
                    ok, msg = self.soft_close(int(s), int(r))
                except Exception as e:
                    ok, msg = False, f"bad soft-close callback: {e}"
                notifier.answer_callback(cb.get("callback_id", ""), msg)

        for cmd in commands:
            text = (cmd.get("text") or "").strip()
            parts = text.split()
            head = parts[0].lower() if parts else ""

            ok = True
            msg = ""

            if head == "/pause":
                self.pause("paused by operator")
                msg = "paused"
            elif head == "/resume":
                ok, msg = self.resume()
            elif head == "/add_slot":
                ok, msg = self.add_slot()
            elif head == "/status":
                msg = self.status_text()
            elif head == "/help":
                msg = (
                    "Commands:\n"
                    "/pause\n/resume\n/add_slot\n/status\n/help\n"
                    "/remove_slot [slot_id]\n/remove_slots [N]\n"
                    "/soft_close [slot_id recovery_id]\n"
                    "/cancel_stale [min_distance_pct]\n"
                    "/reconcile_drift\n"
                    "/audit_pnl\n"
                    "/backfill_ohlcv [target_candles] [max_pages] [interval_min]\n"
                    "/set_entry_pct <value>\n"
                    "/set_profit_pct <value>"
                )
            elif head == "/set_entry_pct":
                if len(parts) < 2:
                    ok, msg = False, "usage: /set_entry_pct <value>"
                else:
                    try:
                        ok, msg = self.set_entry_pct(float(parts[1]))
                    except ValueError:
                        ok, msg = False, "invalid value"
            elif head == "/set_profit_pct":
                if len(parts) < 2:
                    ok, msg = False, "usage: /set_profit_pct <value>"
                else:
                    try:
                        ok, msg = self.set_profit_pct(float(parts[1]))
                    except ValueError:
                        ok, msg = False, "invalid value"
            elif head == "/soft_close":
                if len(parts) == 3:
                    try:
                        ok, msg = self.soft_close(int(parts[1]), int(parts[2]))
                    except ValueError:
                        ok, msg = False, "usage: /soft_close <slot_id> <recovery_id>"
                else:
                    # Interactive list via inline buttons.
                    rows = []
                    for sid in sorted(self.slots.keys()):
                        for r in self.slots[sid].state.recovery_orders[:12]:
                            rows.append([{"text": f"slot {sid} / #{r.recovery_id} {r.side} {r.trade_id}.{r.cycle}", "callback_data": f"sc:{sid}:{r.recovery_id}"}])
                    if not rows:
                        ok, msg = False, "no recovery orders"
                    else:
                        notifier.send_with_buttons("Select recovery to soft-close:", rows)
                        ok, msg = True, "sent picker"
            elif head == "/cancel_stale":
                dist = 3.0
                if len(parts) >= 2:
                    try:
                        dist = float(parts[1])
                    except ValueError:
                        pass
                ok, msg = self.cancel_stale_recoveries(dist)
            elif head == "/remove_slot":
                if len(parts) >= 2:
                    try:
                        ok, msg = self.remove_slot(int(parts[1]))
                    except ValueError:
                        ok, msg = False, "usage: /remove_slot [slot_id]"
                else:
                    # Remove highest-numbered slot by default.
                    if not self.slots:
                        ok, msg = False, "no slots"
                    else:
                        ok, msg = self.remove_slot(max(self.slots.keys()))
            elif head == "/remove_slots":
                count = 1
                if len(parts) >= 2:
                    try:
                        count = int(parts[1])
                    except ValueError:
                        pass
                ok, msg = self.remove_slots(count)
            elif head == "/reconcile_drift":
                ok, msg = self.reconcile_drift()
            elif head == "/audit_pnl":
                ok, msg = self.audit_pnl()
            elif head == "/backfill_ohlcv":
                target = None
                pages = None
                interval_min = None
                if len(parts) >= 2:
                    try:
                        target = int(parts[1])
                    except ValueError:
                        ok, msg = False, "usage: /backfill_ohlcv [target_candles] [max_pages] [interval_min]"
                if ok and len(parts) >= 3:
                    try:
                        pages = int(parts[2])
                    except ValueError:
                        ok, msg = False, "usage: /backfill_ohlcv [target_candles] [max_pages] [interval_min]"
                if ok and len(parts) >= 4:
                    try:
                        interval_min = int(parts[3])
                    except ValueError:
                        ok, msg = False, "usage: /backfill_ohlcv [target_candles] [max_pages] [interval_min]"
                if ok:
                    interval = (
                        max(1, int(interval_min))
                        if interval_min is not None
                        else max(1, int(getattr(config, "HMM_OHLCV_INTERVAL_MIN", 1)))
                    )
                    secondary_interval = max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15)))
                    tertiary_interval = max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60)))
                    if interval == tertiary_interval:
                        state_key = "tertiary"
                    elif interval == secondary_interval:
                        state_key = "secondary"
                    else:
                        state_key = "primary"
                    if state_key == "secondary":
                        self._hmm_backfill_stall_count_secondary = 0
                    elif state_key == "tertiary":
                        self._hmm_backfill_stall_count_tertiary = 0
                    else:
                        self._hmm_backfill_stall_count = 0
                    ok, msg = self.backfill_ohlcv_history(
                        target_candles=target,
                        max_pages=pages,
                        interval_min=interval,
                        state_key=state_key,
                    )
            else:
                ok, msg = False, "unknown command"

            notifier._send_message(("OK: " if ok else "ERR: ") + msg)

    # ------------------ DOGE bias scoreboard ------------------

    def _update_doge_eq_snapshot(self, now: float) -> None:
        if now - self._doge_eq_last_snapshot_ts < self._doge_eq_snapshot_interval:
            return
        bal = self._last_balance_snapshot
        price = self.last_price
        if not bal or price <= 0:
            return
        usd = _usd_balance(bal)
        doge = _doge_balance(bal)
        doge_eq = doge + usd / price
        self._doge_eq_snapshots.append((now, doge_eq))
        self._doge_eq_last_snapshot_ts = now
        if not self._equity_ts_enabled:
            return

        total_profit = sum(slot.state.total_profit for slot in self.slots.values())
        total_unrealized = self._total_unrealized_profit_locked()
        self._equity_ts_records.append({
            "ts": float(now),
            "doge_eq": float(doge_eq),
            "usd": float(usd),
            "doge": float(doge),
            "price": float(price),
            "bot_pnl_usd": float(total_profit + total_unrealized),
            "flows_cumulative_doge_eq": float(self._flow_total_deposits_doge_eq + self._flow_total_withdrawals_doge_eq),
        })
        self._trim_equity_ts_records(now)
        self._equity_ts_dirty = True

    def _should_flush_equity_ts(self, now: float) -> bool:
        if not self._equity_ts_enabled:
            return False
        if not self._equity_ts_dirty:
            return False
        return (now - self._equity_ts_last_flush_ts) >= self._equity_ts_flush_interval

    def _flush_equity_ts(self, now: float) -> None:
        if not self._equity_ts_enabled:
            return
        self._trim_equity_ts_records(now)
        payload = {
            "version": 1,
            "cursor": float(self._equity_ts_records[-1]["ts"]) if self._equity_ts_records else float(now),
            "snapshots": list(self._equity_ts_records),
        }
        try:
            supabase_store.save_state(payload, pair="__equity_ts__")
            self._save_local_equity_ts(payload)
            self._equity_ts_last_flush_ts = float(now)
            self._equity_ts_last_flush_ok = True
            self._equity_ts_last_flush_error = ""
            self._equity_ts_dirty = False
        except Exception as e:
            self._equity_ts_last_flush_ts = float(now)
            self._equity_ts_last_flush_ok = False
            self._equity_ts_last_flush_error = str(e)
            logger.warning("equity_ts flush failed: %s", e)

    def _extract_b_side_gaps(self) -> list[dict]:
        """Extract gaps between consecutive B-side cycles for opportunity PnL and re-entry lag."""
        gaps: list[dict] = []
        price = self.last_price
        now = _now()
        for slot in self.slots.values():
            b_cycles = [c for c in slot.state.completed_cycles if c.trade_id == "B"]
            b_cycles.sort(key=lambda c: c.cycle)
            for i in range(len(b_cycles) - 1):
                prev, nxt = b_cycles[i], b_cycles[i + 1]
                if prev.exit_time <= 0 or nxt.entry_time <= 0:
                    continue
                lag_sec = nxt.entry_time - prev.exit_time
                gap_start_price = prev.exit_price
                gap_end_price = nxt.entry_price
                if gap_start_price <= 0:
                    continue
                price_distance_pct = (gap_end_price - gap_start_price) / gap_start_price * 100.0
                opportunity_usd = (gap_end_price - gap_start_price) * prev.volume
                gaps.append({
                    "slot_id": slot.slot_id,
                    "lag_sec": lag_sec,
                    "opportunity_usd": opportunity_usd,
                    "price_distance_pct": price_distance_pct,
                    "volume": prev.volume,
                    "gap_start_price": gap_start_price,
                    "gap_end_price": gap_end_price,
                    "open": False,
                })
            # Detect open gap: last B-cycle exited but no subsequent B entry has filled.
            # A resting (unfilled) B-entry still leaves capital in USD, so we measure
            # the gap from the last exit fill to now regardless of pending orders.
            if b_cycles and b_cycles[-1].exit_time > 0 and price > 0:
                last = b_cycles[-1]
                lag_sec = now - last.exit_time
                gap_start_price = last.exit_price
                if gap_start_price > 0:
                    price_distance_pct = (price - gap_start_price) / gap_start_price * 100.0
                    opportunity_usd = (price - gap_start_price) * last.volume
                    gaps.append({
                        "slot_id": slot.slot_id,
                        "lag_sec": lag_sec,
                        "opportunity_usd": opportunity_usd,
                        "price_distance_pct": price_distance_pct,
                        "volume": last.volume,
                        "gap_start_price": gap_start_price,
                        "gap_end_price": price,
                        "open": True,
                    })
        return gaps

    def _compute_doge_bias_scoreboard(self) -> dict | None:
        bal = self._last_balance_snapshot
        price = self.last_price
        if not bal or price <= 0:
            return None
        now = _now()

        # --- Metric 1: DOGE-Equivalent Equity ---
        current_doge_eq = _doge_balance(bal) + _usd_balance(bal) / price
        doge_eq_change_1h = None
        doge_eq_change_24h = None
        sparkline = [v for _, v in self._doge_eq_snapshots]

        for target_ago, attr_name in [(3600, "1h"), (86400, "24h")]:
            target_ts = now - target_ago
            best_snap = None
            best_dist = float("inf")
            for ts, val in self._doge_eq_snapshots:
                dist = abs(ts - target_ts)
                if dist < best_dist and dist < 600:  # 10 min tolerance
                    best_dist = dist
                    best_snap = val
            if best_snap is not None:
                delta = current_doge_eq - best_snap
                if attr_name == "1h":
                    doge_eq_change_1h = delta
                else:
                    doge_eq_change_24h = delta

        # --- Metric 2: Idle USD Above Runway ---
        observed_usd = _usd_balance(bal)
        usd_committed_buy_orders = 0.0
        usd_next_entries_estimate = 0.0
        for slot in self.slots.values():
            usd_next_entries_estimate += self._slot_order_size_usd(slot)
            for o in slot.state.orders:
                if o.txid and o.side == "buy":
                    usd_committed_buy_orders += o.volume * o.price
            for r in slot.state.recovery_orders:
                if r.txid and r.side == "buy":
                    usd_committed_buy_orders += r.volume * r.price
        usd_runway_floor = usd_committed_buy_orders + (usd_next_entries_estimate * 1.5)
        idle_usd = max(0.0, observed_usd - usd_runway_floor)
        idle_usd_pct = (idle_usd / observed_usd * 100.0) if observed_usd > 0 else 0.0

        # --- Metrics 3 & 4: Opportunity PnL + Re-entry Lag ---
        gaps = self._extract_b_side_gaps()
        closed_gaps = [g for g in gaps if not g["open"]]
        open_gaps = [g for g in gaps if g["open"]]

        # Metric 3: Opportunity PnL
        total_opportunity_pnl_usd = sum(g["opportunity_usd"] for g in closed_gaps)
        total_opportunity_pnl_doge = total_opportunity_pnl_usd / price if price > 0 else 0.0
        open_gap_opportunity_usd = sum(g["opportunity_usd"] for g in open_gaps) if open_gaps else None
        gap_count = len(closed_gaps)
        avg_opportunity_per_gap_usd = (total_opportunity_pnl_usd / gap_count) if gap_count > 0 else None
        worst_missed_usd = max((g["opportunity_usd"] for g in closed_gaps), default=None)

        # Metric 4: Re-entry Lag
        closed_lags = [g["lag_sec"] for g in closed_gaps]
        median_reentry_lag_sec = float(median(closed_lags)) if closed_lags else None
        avg_reentry_lag_sec = (sum(closed_lags) / len(closed_lags)) if closed_lags else None
        max_reentry_lag_sec = max(closed_lags, default=None)
        current_open_lag_sec = max((g["lag_sec"] for g in open_gaps), default=None)
        current_open_lag_price_pct = max(
            (g["price_distance_pct"] for g in open_gaps), default=None
        )
        lag_count = len(closed_lags)
        closed_price_dists = [g["price_distance_pct"] for g in closed_gaps]
        median_price_distance_pct = float(median(closed_price_dists)) if closed_price_dists else None

        return {
            "doge_eq": current_doge_eq,
            "doge_eq_change_1h": doge_eq_change_1h,
            "doge_eq_change_24h": doge_eq_change_24h,
            "doge_eq_sparkline": sparkline,
            "idle_usd": idle_usd,
            "idle_usd_pct": idle_usd_pct,
            "usd_runway_floor": usd_runway_floor,
            "observed_usd": observed_usd,
            "total_opportunity_pnl_usd": total_opportunity_pnl_usd,
            "total_opportunity_pnl_doge": total_opportunity_pnl_doge,
            "open_gap_opportunity_usd": open_gap_opportunity_usd,
            "gap_count": gap_count,
            "avg_opportunity_per_gap_usd": avg_opportunity_per_gap_usd,
            "worst_missed_usd": worst_missed_usd,
            "median_reentry_lag_sec": median_reentry_lag_sec,
            "avg_reentry_lag_sec": avg_reentry_lag_sec,
            "max_reentry_lag_sec": max_reentry_lag_sec,
            "current_open_lag_sec": current_open_lag_sec,
            "current_open_lag_price_pct": current_open_lag_price_pct,
            "lag_count": lag_count,
            "median_price_distance_pct": median_price_distance_pct,
        }

    def _compute_dynamic_idle_target(self, now: float) -> float:
        base_target = max(0.0, min(1.0, float(config.REBALANCE_TARGET_IDLE_PCT)))
        floor_target = max(0.0, min(1.0, float(config.TREND_IDLE_FLOOR)))
        ceil_target = max(0.0, min(1.0, float(config.TREND_IDLE_CEILING)))
        if floor_target > ceil_target:
            floor_target, ceil_target = ceil_target, floor_target
        sensitivity = max(0.0, float(config.TREND_IDLE_SENSITIVITY))
        dead_zone = max(0.0, float(config.TREND_DEAD_ZONE))
        hold_sec = max(0.0, float(config.TREND_HYSTERESIS_SEC))
        min_samples = max(1, int(config.TREND_MIN_SAMPLES))
        fast_halflife = max(1.0, float(config.TREND_FAST_HALFLIFE))
        slow_halflife = max(fast_halflife, float(config.TREND_SLOW_HALFLIFE))
        smooth_halflife = max(1.0, float(config.TREND_HYSTERESIS_SMOOTH_HALFLIFE))
        price = float(self.last_price)
        if price <= 0:
            self._trend_score = 0.0
            self._trend_dynamic_target = base_target
            self._trend_smoothed_target = base_target
            self._trend_target_locked_until = 0.0
            return base_target

        last_update_ts = float(self._trend_last_update_ts)
        interval_sec = max(1.0, float(config.REBALANCE_INTERVAL_SEC))
        dt = max(1.0, (now - last_update_ts) if last_update_ts > 0 else interval_sec)

        # Long update gaps can leave stale EMA state; restart from current price.
        if last_update_ts > 0 and (now - last_update_ts) > (slow_halflife * 2.0):
            self._trend_fast_ema = price
            self._trend_slow_ema = price

        has_persisted_ema = self._trend_fast_ema > 0 and self._trend_slow_ema > 0
        if not has_persisted_ema:
            if len(self.price_history) < min_samples:
                self._trend_fast_ema = price
                self._trend_slow_ema = price
                self._trend_score = 0.0
                self._trend_dynamic_target = base_target
                self._trend_smoothed_target = base_target
                self._trend_target_locked_until = 0.0
                self._trend_last_update_ts = now
                return base_target
            self._trend_fast_ema = price
            self._trend_slow_ema = price

        fast_alpha = 1.0 - exp(-dt / fast_halflife)
        slow_alpha = 1.0 - exp(-dt / slow_halflife)
        self._trend_fast_ema = fast_alpha * price + (1.0 - fast_alpha) * float(self._trend_fast_ema)
        self._trend_slow_ema = slow_alpha * price + (1.0 - slow_alpha) * float(self._trend_slow_ema)

        slow = float(self._trend_slow_ema)
        if slow <= 0:
            self._trend_score = 0.0
        else:
            self._trend_score = (float(self._trend_fast_ema) - slow) / slow

        signal_for_target = float(self._trend_score)
        hmm_enabled = self._flag_value("HMM_ENABLED")
        _, _, policy_bias, hmm_ready, _ = self._policy_hmm_signal()
        if hmm_enabled and hmm_ready:
            blend_factor = max(
                0.0,
                min(1.0, float(self._hmm_state.get("blend_factor", getattr(config, "HMM_BLEND_WITH_TREND", 0.5)))),
            )
            hmm_bias = float(policy_bias)
            signal_for_target = blend_factor * float(self._trend_score) + (1.0 - blend_factor) * hmm_bias
            self._hmm_state["blend_factor"] = blend_factor
        self._hmm_state["blended_signal"] = float(signal_for_target)

        # Dead zone first: collapse to base target and skip hold/smoothing stages.
        if abs(float(signal_for_target)) < dead_zone:
            self._trend_dynamic_target = base_target
            self._trend_smoothed_target = base_target
            self._trend_target_locked_until = 0.0
            self._trend_last_update_ts = now
            return base_target

        raw_target = base_target - sensitivity * float(signal_for_target)
        clamped_target = max(floor_target, min(ceil_target, raw_target))

        # Hold second: freeze output and smoothing state.
        if now < float(self._trend_target_locked_until):
            self._trend_last_update_ts = now
            return max(0.0, min(1.0, float(self._trend_dynamic_target)))

        # Smooth third: only when hold is not active.
        prev_output = max(0.0, min(1.0, float(self._trend_dynamic_target)))
        prev_smoothed = float(self._trend_smoothed_target)
        if not isfinite(prev_smoothed):
            prev_smoothed = prev_output
        target_alpha = 1.0 - exp(-dt / smooth_halflife)
        smoothed_target = target_alpha * clamped_target + (1.0 - target_alpha) * prev_smoothed
        smoothed_target = max(floor_target, min(ceil_target, smoothed_target))
        self._trend_smoothed_target = smoothed_target
        self._trend_dynamic_target = smoothed_target

        if abs(smoothed_target - prev_output) > 0.02 + 1e-12:
            self._trend_target_locked_until = now + hold_sec
        else:
            self._trend_target_locked_until = 0.0

        self._trend_last_update_ts = now
        return max(0.0, min(1.0, smoothed_target))

    def _update_rebalancer(self, now: float) -> None:
        if not self._flag_value("REBALANCE_ENABLED"):
            self._rebalancer_current_skew = 0.0
            return

        interval_sec = max(1.0, float(config.REBALANCE_INTERVAL_SEC))
        last_ts = float(self._rebalancer_last_update_ts)
        if last_ts > 0 and (now - last_ts) < interval_sec:
            return

        self._update_hmm(now)
        target = self._compute_dynamic_idle_target(now)

        capacity = self._compute_capacity_health(now)
        band = str(capacity.get("status_band") or "normal")
        self._rebalancer_last_capacity_band = band
        if band in ("caution", "stop"):
            self._rebalancer_current_skew = 0.0
            self._rebalancer_last_update_ts = now
            logger.info("[REBAL] paused: capacity band=%s", band)
            return

        scoreboard = self._compute_doge_bias_scoreboard()
        if not scoreboard:
            self._rebalancer_current_skew = 0.0
            self._rebalancer_last_update_ts = now
            return

        idle_ratio = max(0.0, min(1.0, float(scoreboard.get("idle_usd_pct", 0.0)) / 100.0))
        raw_error = idle_ratio - target

        dt = max(1.0, (now - last_ts) if last_ts > 0 else interval_sec)
        halflife = max(1.0, float(config.REBALANCE_EMA_HALFLIFE))
        alpha = 1.0 - exp(-dt / halflife)

        prev_error = float(self._rebalancer_smoothed_error)
        smoothed_error = alpha * raw_error + (1.0 - alpha) * prev_error
        raw_velocity = (smoothed_error - prev_error) / dt
        prev_velocity = float(self._rebalancer_smoothed_velocity)
        smoothed_velocity = alpha * raw_velocity + (1.0 - alpha) * prev_velocity

        max_skew = max(0.0, float(config.REBALANCE_MAX_SKEW))
        if now < float(self._rebalancer_damped_until):
            max_skew *= 0.5

        neutral_band = max(0.0, float(config.REBALANCE_NEUTRAL_BAND))
        if abs(smoothed_error) < neutral_band:
            raw_skew = 0.0
        else:
            raw_skew = float(config.REBALANCE_KP) * smoothed_error + float(config.REBALANCE_KD) * smoothed_velocity
            raw_skew = max(-max_skew, min(max_skew, raw_skew))

        current_skew = float(self._rebalancer_current_skew)
        max_step = max(0.0, float(config.REBALANCE_MAX_SKEW_STEP))
        delta = raw_skew - current_skew
        if abs(delta) > max_step:
            new_skew = current_skew + (max_step if delta > 0 else -max_step)
        else:
            new_skew = raw_skew
        new_skew = max(-max_skew, min(max_skew, new_skew))

        # 1h sign-flip tracking for oscillation damping.
        if abs(current_skew) > 1e-12 and abs(new_skew) > 1e-12 and current_skew * new_skew < 0:
            self._rebalancer_sign_flip_history.append(now)
        cutoff = now - 3600.0
        while self._rebalancer_sign_flip_history and self._rebalancer_sign_flip_history[0] < cutoff:
            self._rebalancer_sign_flip_history.popleft()
        sign_flips_1h = len(self._rebalancer_sign_flip_history)
        if sign_flips_1h >= 3 and now >= float(self._rebalancer_damped_until):
            self._rebalancer_damped_until = now + 3600.0
            logger.warning(
                "[REBAL] WARNING: oscillation detected (%d flips/hr), auto-damping active",
                sign_flips_1h,
            )

        self._rebalancer_idle_ratio = idle_ratio
        self._rebalancer_last_raw_error = raw_error
        self._rebalancer_smoothed_error = smoothed_error
        self._rebalancer_smoothed_velocity = smoothed_velocity
        self._rebalancer_current_skew = new_skew
        self._rebalancer_last_update_ts = now

        logger.info(
            "[REBAL] idle=%.3f target=%.3f err=%+.4f vel=%+.6f skew=%+.4f band=%s flips1h=%d",
            idle_ratio,
            target,
            smoothed_error,
            smoothed_velocity,
            new_skew,
            band,
            sign_flips_1h,
        )

    # ------------------ Balance intelligence ------------------

    @staticmethod
    def _flow_asset_kind(asset: str) -> str | None:
        symbol = str(asset or "").strip().upper()
        if symbol in {"XXDG", "XDG", "DOGE"}:
            return "doge"
        if symbol in {"ZUSD", "USD"}:
            return "usd"
        return None

    def _last_external_flow(self) -> ExternalFlow | None:
        if not self._external_flows:
            return None
        return self._external_flows[-1]

    def _should_poll_flows(self, now: float) -> bool:
        if not bool(getattr(config, "FLOW_DETECTION_ENABLED", True)):
            return False
        if not self._flow_detection_active:
            return False
        if self.last_price <= 0:
            return False
        if self._recon_baseline is None:
            return False
        return (now - self._flow_last_poll_ts) >= self._flow_poll_interval

    def _apply_flow_baseline_adjustment(self, flow: ExternalFlow, now: float) -> None:
        if not isinstance(self._recon_baseline, dict):
            return
        kind = self._flow_asset_kind(flow.asset)
        old_usd = float(self._recon_baseline.get("usd", 0.0) or 0.0)
        old_doge = float(self._recon_baseline.get("doge", 0.0) or 0.0)
        new_usd = old_usd
        new_doge = old_doge

        # Preserve baseline semantics by adjusting the field that matches the flow asset.
        if kind == "usd":
            new_usd = old_usd + float(flow.amount)
        elif kind == "doge":
            new_doge = old_doge + float(flow.amount)
        else:
            new_doge = old_doge + float(flow.doge_eq)

        self._recon_baseline["usd"] = float(new_usd)
        self._recon_baseline["doge"] = float(new_doge)
        self._baseline_adjustments.append({
            "ts": float(now),
            "ledger_id": str(flow.ledger_id),
            "flow_type": str(flow.flow_type),
            "asset": str(flow.asset),
            "amount": float(flow.amount),
            "doge_eq_adjustment": float(flow.doge_eq),
            "baseline_before": {"usd": float(old_usd), "doge": float(old_doge)},
            "baseline_after": {"usd": float(new_usd), "doge": float(new_doge)},
            "price": float(self.last_price),
        })
        self._trim_flow_buffers()

    def _poll_external_flows(self, now: float) -> None:
        if not bool(getattr(config, "FLOW_DETECTION_ENABLED", True)):
            return
        if not self._flow_detection_active:
            return
        if not self._consume_private_budget(1, "get_ledgers"):
            return

        if self._flow_ledger_cursor <= 0.0:
            baseline_ts = 0.0
            if isinstance(self._recon_baseline, dict):
                baseline_ts = float(self._recon_baseline.get("ts", 0.0) or 0.0)
            self._flow_ledger_cursor = max(baseline_ts, now - self._flow_poll_interval)

        try:
            result = kraken_client.get_ledgers(
                type_="all",
                start=self._flow_ledger_cursor if self._flow_ledger_cursor > 0 else None,
            )
            ledger_rows = result.get("ledger", {}) if isinstance(result, dict) else {}
            if not isinstance(ledger_rows, dict):
                ledger_rows = {}

            max_seen_ts = float(self._flow_ledger_cursor)
            discovered: list[ExternalFlow] = []
            for ledger_id, row in ledger_rows.items():
                if not isinstance(row, dict):
                    continue
                lid = str(ledger_id or "").strip()
                if not lid or lid in self._flow_seen_ids:
                    continue

                try:
                    flow_ts = float(row.get("time") or 0.0)
                except (TypeError, ValueError):
                    flow_ts = 0.0
                if flow_ts > max_seen_ts:
                    max_seen_ts = flow_ts

                flow_type = str(row.get("type") or "").strip().lower()
                if flow_type not in {"deposit", "withdrawal"}:
                    continue
                asset = str(row.get("asset") or "").strip().upper()
                kind = self._flow_asset_kind(asset)
                if kind is None:
                    continue

                try:
                    raw_amount = float(row.get("amount") or 0.0)
                except (TypeError, ValueError):
                    continue
                signed_amount = abs(raw_amount) if flow_type == "deposit" else -abs(raw_amount)
                if abs(signed_amount) <= 1e-12:
                    continue

                doge_eq = signed_amount if kind == "doge" else (signed_amount / self.last_price)
                try:
                    fee = float(row.get("fee") or 0.0)
                except (TypeError, ValueError):
                    fee = 0.0
                discovered.append(ExternalFlow(
                    ledger_id=lid,
                    flow_type=flow_type,
                    asset=asset,
                    amount=float(signed_amount),
                    fee=float(fee),
                    timestamp=float(flow_ts if flow_ts > 0 else now),
                    doge_eq=float(doge_eq),
                    price_at_detect=float(self.last_price),
                ))

            discovered.sort(key=lambda f: (float(f.timestamp), str(f.ledger_id)))
            auto_adjust = bool(getattr(config, "FLOW_BASELINE_AUTO_ADJUST", True))
            for flow in discovered:
                self._external_flows.append(flow)
                self._flow_seen_ids.add(flow.ledger_id)
                self._flow_total_count += 1
                if flow.doge_eq >= 0.0:
                    self._flow_total_deposits_doge_eq += float(flow.doge_eq)
                else:
                    self._flow_total_withdrawals_doge_eq += float(flow.doge_eq)
                if auto_adjust:
                    self._apply_flow_baseline_adjustment(flow, now)

            self._trim_flow_buffers()
            if max_seen_ts > self._flow_ledger_cursor:
                self._flow_ledger_cursor = max_seen_ts + 1.0
            self._flow_last_poll_ts = float(now)
            self._flow_last_ok = True
            self._flow_last_error = ""
        except Exception as e:
            msg = str(e)
            self._flow_last_poll_ts = float(now)
            self._flow_last_ok = False
            self._flow_last_error = msg
            if "permission" in msg.lower():
                self._flow_detection_active = False
                self._flow_disabled_reason = "Kraken API key lacks ledger-query permission"
                logger.warning("Flow polling disabled: %s", self._flow_disabled_reason)
            else:
                logger.warning("Flow polling failed: %s", msg)

    def _equity_delta_from_series(self, series: list[tuple[float, float]], now: float, lookback_sec: float) -> float | None:
        if len(series) < 2:
            return None
        latest_ts, latest_val = series[-1]
        target = now - lookback_sec
        ref_val: float | None = None
        for ts, val in reversed(series):
            if ts <= target:
                ref_val = val
                break
        if ref_val is None:
            ref_val = series[0][1]
        return float(latest_val - ref_val)

    def _equity_history_status_payload(self, now: float) -> dict:
        if not self._equity_ts_enabled:
            return {"enabled": False}

        in_mem = list(self._doge_eq_snapshots)
        persisted_pairs = [
            (float(row.get("ts", 0.0) or 0.0), float(row.get("doge_eq", 0.0) or 0.0))
            for row in self._equity_ts_records
            if isinstance(row, dict)
        ]
        persisted_pairs = [row for row in persisted_pairs if row[0] > 0]
        persisted_pairs.sort(key=lambda x: x[0])

        spark_24h = [float(v) for _, v in in_mem]
        spark_7d = [float(v) for _, v in persisted_pairs[::self._equity_ts_sparkline_7d_step]]
        if persisted_pairs and (not spark_7d or spark_7d[-1] != persisted_pairs[-1][1]):
            spark_7d.append(float(persisted_pairs[-1][1]))

        change_series = persisted_pairs if persisted_pairs else in_mem
        oldest_ts = float(persisted_pairs[0][0]) if persisted_pairs else None
        newest_ts = float(persisted_pairs[-1][0]) if persisted_pairs else None
        span_hours = None
        if oldest_ts is not None and newest_ts is not None and newest_ts >= oldest_ts:
            span_hours = (newest_ts - oldest_ts) / 3600.0

        flush_age = None
        if self._equity_ts_last_flush_ts > 0:
            flush_age = max(0.0, now - self._equity_ts_last_flush_ts)

        return {
            "enabled": True,
            "snapshots_in_memory": len(in_mem),
            "snapshots_persisted": len(persisted_pairs),
            "oldest_persisted_ts": oldest_ts,
            "newest_persisted_ts": newest_ts,
            "span_hours": span_hours,
            "flush_age_sec": flush_age,
            "flush_interval_sec": float(self._equity_ts_flush_interval),
            "flush_ok": bool(self._equity_ts_last_flush_ok),
            "flush_error": str(self._equity_ts_last_flush_error or ""),
            "sparkline_24h": spark_24h,
            "sparkline_7d": spark_7d,
            "doge_eq_change_1h": self._equity_delta_from_series(change_series, now, 3600.0),
            "doge_eq_change_24h": self._equity_delta_from_series(change_series, now, 86400.0),
            "doge_eq_change_7d": self._equity_delta_from_series(change_series, now, 7.0 * 86400.0),
        }

    def _external_flows_status_payload(self, now: float) -> dict:
        if not bool(getattr(config, "FLOW_DETECTION_ENABLED", True)):
            return {"enabled": False}
        deposits = max(0.0, float(self._flow_total_deposits_doge_eq))
        withdrawals = abs(min(0.0, float(self._flow_total_withdrawals_doge_eq)))
        net = deposits - withdrawals
        poll_age = None
        if self._flow_last_poll_ts > 0:
            poll_age = max(0.0, now - self._flow_last_poll_ts)

        recent: list[dict] = []
        for flow in self._external_flows[-self._flow_recent_status_limit:]:
            recent.append({
                "ledger_id": str(flow.ledger_id),
                "type": str(flow.flow_type),
                "asset": str(flow.asset),
                "amount": float(abs(flow.amount)),
                "doge_eq": float(flow.doge_eq),
                "fee": float(flow.fee),
                "ts": float(flow.timestamp),
                "baseline_adjusted": bool(getattr(config, "FLOW_BASELINE_AUTO_ADJUST", True)),
            })
        recent.reverse()

        return {
            "enabled": True,
            "active": bool(self._flow_detection_active),
            "poll_interval_sec": float(self._flow_poll_interval),
            "last_poll_ts": (float(self._flow_last_poll_ts) if self._flow_last_poll_ts > 0 else None),
            "last_poll_age_sec": poll_age,
            "flow_poll_ok": bool(self._flow_last_ok and self._flow_detection_active),
            "disabled_reason": str(self._flow_disabled_reason or ""),
            "last_error": str(self._flow_last_error or ""),
            "total_deposits_doge_eq": deposits,
            "total_withdrawals_doge_eq": withdrawals,
            "net_flows_doge_eq": net,
            "flow_count": int(self._flow_total_count),
            "recent_flows": recent,
        }

    # ------------------ Balance reconciliation ------------------

    def _compute_balance_recon(self, total_profit: float, total_unrealized: float) -> dict | None:
        if self._recon_baseline is None:
            return None
        price = self.last_price
        if price <= 0:
            return {"status": "NO_PRICE"}
        bal = self._last_balance_snapshot
        if not bal:
            return {"status": "NO_BALANCE"}

        baseline = self._recon_baseline
        baseline_doge_eq = baseline["doge"] + baseline["usd"] / price
        current_usd = _usd_balance(bal)
        current_doge = _doge_balance(bal)
        current_doge_eq = current_doge + current_usd / price

        account_growth = current_doge_eq - baseline_doge_eq
        bot_pnl_doge = (total_profit + total_unrealized) / price if price > 0 else 0.0
        drift = account_growth - bot_pnl_doge
        drift_pct = (drift / baseline_doge_eq * 100.0) if baseline_doge_eq > 0 else 0.0
        threshold = float(config.BALANCE_RECON_DRIFT_PCT)
        status = "OK" if abs(drift_pct) <= threshold else "DRIFT"
        net_flows_doge_eq = float(self._flow_total_deposits_doge_eq + self._flow_total_withdrawals_doge_eq)
        auto_adjust = bool(getattr(config, "FLOW_BASELINE_AUTO_ADJUST", True))
        adjusted_drift = drift if auto_adjust else (drift - net_flows_doge_eq)
        adjusted_drift_pct = (adjusted_drift / baseline_doge_eq * 100.0) if baseline_doge_eq > 0 else 0.0
        adjusted_status = "OK" if abs(adjusted_drift_pct) <= threshold else "DRIFT"
        now = _now()
        poll_age = None
        if self._flow_last_poll_ts > 0:
            poll_age = max(0.0, now - self._flow_last_poll_ts)
        last_flow = self._last_external_flow()

        return {
            "status": status,
            "baseline_doge_eq": baseline_doge_eq,
            "current_doge_eq": current_doge_eq,
            "account_growth_doge": account_growth,
            "bot_pnl_doge": bot_pnl_doge,
            "drift_doge": drift,
            "drift_pct": drift_pct,
            "threshold_pct": threshold,
            "baseline_ts": baseline["ts"],
            "price": price,
            "simulated": config.DRY_RUN,
            "external_flows_doge_eq": net_flows_doge_eq,
            "external_flow_count": int(self._flow_total_count),
            "adjusted_drift_doge": adjusted_drift,
            "adjusted_drift_pct": adjusted_drift_pct,
            "adjusted_status": adjusted_status,
            "baseline_adjustments_count": len(self._baseline_adjustments),
            "last_flow_ts": (float(last_flow.timestamp) if last_flow else None),
            "last_flow_type": (str(last_flow.flow_type) if last_flow else None),
            "last_flow_amount": (float(abs(last_flow.amount)) if last_flow else None),
            "last_flow_asset": (str(last_flow.asset) if last_flow else None),
            "flow_poll_age_sec": poll_age,
            "flow_poll_ok": bool(self._flow_last_ok and self._flow_detection_active),
        }

    # ------------------ API status ------------------

    def status_payload(self) -> dict:
        def _unrealized_pnl(exit_side: str, entry_price: float, market_price: float, volume: float) -> float:
            if entry_price <= 0 or market_price <= 0 or volume <= 0:
                return 0.0
            # buy exit closes a short (profit as market falls), sell exit closes a long.
            if exit_side == "buy":
                return (entry_price - market_price) * volume
            return (market_price - entry_price) * volume

        with self.lock:
            now = _now()
            self._update_release_recon_gate_locked()
            self._trim_rolling_telemetry(now)
            self._update_doge_eq_snapshot(now)
            slots = []
            total_unrealized_profit = 0.0
            total_active_orders = 0
            committed_usd_internal = 0.0
            committed_doge_internal = 0.0
            for sid in sorted(self.slots.keys()):
                st = self.slots[sid].state
                phase = sm.derive_phase(st)
                slot_unrealized_profit = 0.0
                open_orders = []
                total_active_orders += len(st.orders)
                for o in st.orders:
                    if o.role == "exit":
                        slot_unrealized_profit += _unrealized_pnl(
                            exit_side=o.side,
                            entry_price=o.entry_price,
                            market_price=st.market_price,
                            volume=o.volume,
                        )
                    if o.txid:
                        if o.side == "buy":
                            committed_usd_internal += o.volume * o.price
                        elif o.side == "sell":
                            committed_doge_internal += o.volume
                    open_orders.append({
                        "local_id": o.local_id,
                        "side": o.side,
                        "role": o.role,
                        "trade_id": o.trade_id,
                        "cycle": o.cycle,
                        "volume": o.volume,
                        "price": o.price,
                        "txid": o.txid,
                    })
                recs = []
                for r in st.recovery_orders:
                    dist = abs(r.price - st.market_price) / st.market_price * 100.0 if st.market_price > 0 else 0.0
                    slot_unrealized_profit += _unrealized_pnl(
                        exit_side=r.side,
                        entry_price=r.entry_price,
                        market_price=st.market_price,
                        volume=r.volume,
                    )
                    if r.txid:
                        if r.side == "buy":
                            committed_usd_internal += r.volume * r.price
                        elif r.side == "sell":
                            committed_doge_internal += r.volume
                    recs.append({
                        "recovery_id": r.recovery_id,
                        "trade_id": r.trade_id,
                        "cycle": r.cycle,
                        "side": r.side,
                        "price": r.price,
                        "volume": r.volume,
                        "txid": r.txid,
                        "reason": r.reason,
                        "age_sec": max(0.0, now - r.orphaned_at),
                        "distance_pct": dist,
                    })
                cycles = list(st.completed_cycles[-20:])
                belief_badges = [
                    belief.to_badge_dict()
                    for belief in self._trade_beliefs.values()
                    if int(belief.slot_id) == int(sid)
                ]
                slot_realized_doge = st.total_profit / st.market_price if st.market_price > 0 else 0.0
                slot_unrealized_doge = slot_unrealized_profit / st.market_price if st.market_price > 0 else 0.0
                slots.append({
                    "slot_id": sid,
                    "slot_alias": self._slot_label(self.slots[sid]),
                    "slot_label": self._slot_label(self.slots[sid]),
                    "phase": phase,
                    "long_only": st.long_only,
                    "short_only": st.short_only,
                    "mode_source": str(getattr(st, "mode_source", "none") or "none"),
                    "s2_entered_at": st.s2_entered_at,
                    "market_price": st.market_price,
                    "cycle_a": st.cycle_a,
                    "cycle_b": st.cycle_b,
                    "total_profit": st.total_profit,
                    "total_settled_usd": float(getattr(st, "total_settled_usd", st.total_profit)),
                    "total_profit_doge": slot_realized_doge,
                    "unrealized_profit": slot_unrealized_profit,
                    "unrealized_profit_doge": slot_unrealized_doge,
                    "today_realized_loss": st.today_realized_loss,
                    "total_round_trips": st.total_round_trips,
                    "order_size_usd": self._slot_order_size_usd(self.slots[sid]),
                    "profit_pct_runtime": st.profit_pct_runtime,
                    "belief_badges": belief_badges,
                    "open_orders": open_orders,
                    "recovery_orders": recs,
                    "recent_cycles": [c.__dict__ for c in reversed(cycles)],
                })
                total_unrealized_profit += slot_unrealized_profit

            total_profit = sum(s.state.total_profit for s in self.slots.values())
            total_settled_usd = sum(float(getattr(s.state, "total_settled_usd", s.state.total_profit)) for s in self.slots.values())
            total_loss = sum(s.state.today_realized_loss for s in self.slots.values())
            total_round_trips = sum(s.state.total_round_trips for s in self.slots.values())
            total_orphans = sum(len(s.state.recovery_orders) for s in self.slots.values())
            daily_realized_loss_utc = self._compute_daily_realized_loss_utc(now)
            self._daily_realized_loss_utc = float(daily_realized_loss_utc)
            daily_loss_lock_active = bool(self._daily_loss_lock_active)
            daily_loss_lock_day = str(self._daily_loss_lock_utc_day or "")
            capacity = self._compute_capacity_health(now)
            pending_entries = len(self._pending_entry_orders())
            try:
                private_api = kraken_client.rate_limit_telemetry()
            except Exception:
                private_api = {}
            internal_open_orders_current = int(capacity.get("open_orders_internal") or 0)
            last_balance = dict(self._last_balance_snapshot) if self._last_balance_snapshot else {}
            observed_usd_balance = _usd_balance(last_balance) if last_balance else None
            observed_doge_balance = _doge_balance(last_balance) if last_balance else None
            balance_age_sec = (now - self._last_balance_ts) if last_balance else None
            kraken_open_orders_current = capacity.get("open_orders_kraken")
            open_orders_current = int(capacity.get("open_orders_current") or 0)
            open_orders_source = str(capacity.get("open_orders_source") or "internal_fallback")
            open_order_headroom = int(capacity.get("open_order_headroom") or 0)
            partial_fill_open_events_1d = int(capacity.get("partial_fill_open_events_1d") or 0)
            partial_fill_cancel_events_1d = int(capacity.get("partial_fill_cancel_events_1d") or 0)
            status_band = str(capacity.get("status_band") or "normal")

            drift_persistent = self._open_order_drift_is_persistent(
                now=now,
                internal_open_orders_current=internal_open_orders_current,
                kraken_open_orders_current=kraken_open_orders_current,
            )

            blocked_risk_hint: list[str] = []
            if open_orders_source == "internal_fallback":
                blocked_risk_hint.append("kraken_open_orders_unavailable")
            if drift_persistent:
                blocked_risk_hint.append("open_order_drift_persistent")
            if open_order_headroom < 10:
                blocked_risk_hint.append("near_open_order_cap")
            elif open_order_headroom < 20:
                blocked_risk_hint.append("open_order_caution")
            if partial_fill_open_events_1d > 0:
                blocked_risk_hint.append("partial_fill_open_pressure")
            if partial_fill_cancel_events_1d > 0:
                blocked_risk_hint.append("partial_fill_cancel_detected")

            cutoff_flips = now - 3600.0
            while self._rebalancer_sign_flip_history and self._rebalancer_sign_flip_history[0] < cutoff_flips:
                self._rebalancer_sign_flip_history.popleft()

            top_phase = slots[0]["phase"] if slots else "S0"
            pnl_ref_price = self.last_price if self.last_price > 0 else (slots[0]["market_price"] if slots else 0.0)
            total_profit_doge = total_profit / pnl_ref_price if pnl_ref_price > 0 else 0.0
            total_unrealized_doge = total_unrealized_profit / pnl_ref_price if pnl_ref_price > 0 else 0.0
            pnl_audit = self._pnl_audit_summary()
            layer_metrics = self._current_layer_metrics(mark_price=pnl_ref_price)
            orders_at_funded_size = self._count_orders_at_funded_size()
            slot_vintage = self._slot_vintage_metrics_locked(now)
            hmm_data_pipeline = self._hmm_data_readiness(now)
            secondary_collect_enabled = self._flag_value("HMM_SECONDARY_OHLCV_ENABLED") or bool(
                self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED")
            )
            if secondary_collect_enabled:
                hmm_data_pipeline_secondary = self._hmm_data_readiness(
                    now,
                    interval_min=max(1, int(getattr(config, "HMM_SECONDARY_INTERVAL_MIN", 15))),
                    training_target=max(1, int(getattr(config, "HMM_SECONDARY_TRAINING_CANDLES", 720))),
                    min_samples=max(1, int(getattr(config, "HMM_SECONDARY_MIN_TRAIN_SAMPLES", 200))),
                    sync_interval_sec=max(
                        30.0,
                        float(
                            getattr(
                                config,
                                "HMM_SECONDARY_SYNC_INTERVAL_SEC",
                                getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                            )
                        ),
                    ),
                    state_key="secondary",
                )
            else:
                hmm_data_pipeline_secondary = {
                    "enabled": False,
                    "state_key": "secondary",
                    "ready_for_min_train": False,
                    "ready_for_target_window": False,
                    "gaps": ["pipeline_disabled"],
                }
            if self._flag_value("HMM_TERTIARY_ENABLED"):
                hmm_data_pipeline_tertiary = self._hmm_data_readiness(
                    now,
                    interval_min=max(1, int(getattr(config, "HMM_TERTIARY_INTERVAL_MIN", 60))),
                    training_target=max(1, int(getattr(config, "HMM_TERTIARY_TRAINING_CANDLES", 500))),
                    min_samples=max(1, int(getattr(config, "HMM_TERTIARY_MIN_TRAIN_SAMPLES", 150))),
                    sync_interval_sec=max(
                        30.0,
                        float(
                            getattr(
                                config,
                                "HMM_TERTIARY_SYNC_INTERVAL_SEC",
                                getattr(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0),
                            )
                        ),
                    ),
                    state_key="tertiary",
                )
            else:
                hmm_data_pipeline_tertiary = {
                    "enabled": False,
                    "state_key": "tertiary",
                    "ready_for_min_train": False,
                    "ready_for_target_window": False,
                    "gaps": ["pipeline_disabled"],
                }
            hmm_regime = self._hmm_status_payload()
            hmm_consensus = dict(self._hmm_consensus or self._compute_hmm_consensus())
            hmm_consensus["source_mode"] = self._hmm_source_mode()
            hmm_consensus["multi_timeframe"] = self._flag_value("HMM_MULTI_TIMEFRAME_ENABLED")
            regime_directional = self._regime_status_payload(now)
            ai_regime_advisor = self._ai_regime_status_payload(now)
            oldest_exit_age_sec = float(slot_vintage.get("oldest_exit_age_sec", 0.0) or 0.0)
            stuck_capital_pct = float(slot_vintage.get("stuck_capital_pct", 0.0) or 0.0)
            slot_vintage["vintage_warn"] = oldest_exit_age_sec >= 3.0 * 86400.0
            slot_vintage["vintage_critical"] = stuck_capital_pct > float(config.RELEASE_MAX_STUCK_PCT)
            slot_vintage["release_recon_gate_blocked"] = bool(self._release_recon_blocked)
            slot_vintage["release_recon_gate_reason"] = str(self._release_recon_blocked_reason or "")
            # Prefer live computation if loop data is still available,
            # otherwise use cached value from last loop.
            _live_dust = self._compute_dust_dividend()
            dust_dividend_usd = float(_live_dust if _live_dust > 0 else self._dust_last_dividend_usd)
            buy_ready_slots = sum(1 for slot in self.slots.values() if self._slot_wants_buy_entry(slot))
            dust_available_usd: float | None = None
            if self._loop_available_usd is not None:
                dust_available_usd = float(self._loop_available_usd)
            elif self.ledger._synced:
                dust_available_usd = float(self.ledger.available_usd)
            elif self._last_balance_snapshot:
                dust_available_usd = float(_usd_balance(self._last_balance_snapshot))
            belief_state_payload = self._belief_state.to_status_dict()
            if not bool(getattr(config, "BELIEF_STATE_IN_STATUS", True)):
                belief_state_payload = {"enabled": False}
            bocpd_payload = {
                "enabled": bool(self._bocpd is not None),
                **self._bocpd_state.to_status_dict(),
            }
            if self._survival_model is not None:
                survival_payload = self._survival_model.status_payload(
                    enabled=self._flag_value("SURVIVAL_MODEL_ENABLED")
                )
                survival_payload["last_retrain_ts"] = float(self._survival_last_retrain_ts)
            else:
                survival_payload = {
                    "enabled": False,
                    "model_tier": "kaplan_meier",
                    "n_observations": 0,
                    "n_censored": 0,
                    "last_retrain_ts": float(self._survival_last_retrain_ts),
                    "strata_counts": {},
                    "synthetic_observations": 0,
                    "cox_coefficients": {},
                }
            trade_beliefs_payload = self._trade_beliefs_status_payload()
            action_knobs_payload = self._action_knobs.to_status_dict()
            if not self._flag_value("KNOB_MODE_ENABLED"):
                action_knobs_payload = bayesian_engine.ActionKnobs(enabled=False).to_status_dict()
            manifold_payload = self._manifold_score.to_status_dict()
            manifold_history = list(self._manifold_history)
            history_sparkline: list[float] = [round(float(row[1]), 6) for row in manifold_history]
            trend = "stable"
            if history_sparkline:
                try:
                    trend = str(
                        bayesian_engine.ev_trend(
                            history_sparkline,
                            window=min(4, len(history_sparkline)),
                        )
                    )
                except Exception:
                    trend = "stable"
            if trend not in {"rising", "falling", "stable"}:
                trend = "stable"
            mts_30m_ago: float | None = None
            if manifold_history:
                cutoff = float(now) - max(60.0, float(self._regime_history_window_sec))
                mts_30m_ago = float(manifold_history[0][1])
                for row in manifold_history:
                    row_ts = float(row[0])
                    row_mts = float(row[1])
                    if row_ts <= cutoff:
                        mts_30m_ago = row_mts
                        continue
                    break
                mts_30m_ago = round(float(mts_30m_ago), 6)
            manifold_payload["history_sparkline"] = history_sparkline
            manifold_payload["trend"] = trend
            manifold_payload["mts_30m_ago"] = mts_30m_ago

            return {
                "mode": self.mode,
                "pause_reason": self.pause_reason,
                "pair": self.pair_display,
                "entry_pct": self.entry_pct,
                "profit_pct": self.profit_pct,
                "price": self.last_price,
                "price_age_sec": self._price_age_sec(),
                "top_phase": top_phase,
                "slot_count": len(self.slots),
                "total_profit": total_profit,
                "total_settled_usd": total_settled_usd,
                "total_profit_doge": total_profit_doge,
                "total_unrealized_profit": total_unrealized_profit,
                "total_unrealized_doge": total_unrealized_doge,
                "today_realized_loss": total_loss,
                "daily_loss_limit": float(config.DAILY_LOSS_LIMIT),
                "daily_realized_loss_utc": float(daily_realized_loss_utc),
                "daily_loss_lock_active": daily_loss_lock_active,
                "daily_loss_lock_utc_day": daily_loss_lock_day,
                "total_round_trips": total_round_trips,
                "total_orphans": total_orphans,
                "recovery_orders_enabled": self._recovery_orders_enabled(),
                "pnl_audit": {
                    "ok": bool(pnl_audit.get("ok")),
                    "message": self._format_pnl_audit_message(pnl_audit),
                    "tolerance": float(pnl_audit.get("tolerance", 0.0)),
                    "profit_drift": float(pnl_audit.get("profit_drift", 0.0)),
                    "loss_drift": float(pnl_audit.get("loss_drift", 0.0)),
                    "trips_drift": int(pnl_audit.get("trips_drift", 0)),
                    "slot_mismatch_count": int(pnl_audit.get("slot_mismatch_count", 0)),
                    "slot_mismatches_preview": list(pnl_audit.get("slot_mismatches_preview", [])),
                    "slot_mismatches_more": int(pnl_audit.get("slot_mismatches_more", 0)),
                },
                "pnl_reference_price": pnl_ref_price,
                "s2_orphan_after_sec": float(config.S2_ORPHAN_AFTER_SEC),
                "stale_price_max_age_sec": float(config.STALE_PRICE_MAX_AGE_SEC),
                "reentry_base_cooldown_sec": float(config.REENTRY_BASE_COOLDOWN_SEC),
                "capacity_fill_health": {
                    "open_orders_current": open_orders_current,
                    "open_orders_source": open_orders_source,
                    "open_orders_internal": int(capacity.get("open_orders_internal") or internal_open_orders_current),
                    "open_orders_kraken": kraken_open_orders_current,
                    "open_orders_drift": capacity.get("open_orders_drift"),
                    "open_order_limit_configured": int(capacity.get("open_order_limit_configured") or 0),
                    "open_orders_safe_cap": int(capacity.get("open_orders_safe_cap") or 0),
                    "open_order_headroom": open_order_headroom,
                    "open_order_utilization_pct": float(capacity.get("open_order_utilization_pct") or 0.0),
                    "orders_per_slot_estimate": capacity.get("orders_per_slot_estimate"),
                    "estimated_slots_remaining": int(capacity.get("estimated_slots_remaining") or 0),
                    "partial_fill_open_events_1d": partial_fill_open_events_1d,
                    "partial_fill_cancel_events_1d": partial_fill_cancel_events_1d,
                    "median_fill_seconds_1d": capacity.get("median_fill_seconds_1d"),
                    "p95_fill_seconds_1d": capacity.get("p95_fill_seconds_1d"),
                    "status_band": status_band,
                    "blocked_risk_hint": blocked_risk_hint,
                    "auto_soft_close_total": self._auto_soft_close_total,
                    "auto_soft_close_last_at": self._auto_soft_close_last_at or None,
                    "auto_soft_close_threshold_pct": float(config.AUTO_SOFT_CLOSE_CAPACITY_PCT),
                    "auto_recovery_drain_total": self._auto_recovery_drain_total,
                    "auto_recovery_drain_last_at": self._auto_recovery_drain_last_at or None,
                    "auto_recovery_drain_threshold_pct": float(config.AUTO_RECOVERY_DRAIN_CAPACITY_PCT),
                    "private_api_metronome": {
                        "enabled": bool(private_api.get("enabled", False)),
                        "wave_calls": int(private_api.get("wave_calls", 0) or 0),
                        "wave_seconds": float(private_api.get("wave_seconds", 0.0) or 0.0),
                        "wave_calls_used": int(private_api.get("wave_calls_used", 0) or 0),
                        "wave_window_remaining_sec": float(private_api.get("wave_window_remaining_sec", 0.0) or 0.0),
                        "wait_events": int(private_api.get("wait_events", 0) or 0),
                        "wait_total_sec": float(private_api.get("wait_total_sec", 0.0) or 0.0),
                        "last_wait_sec": float(private_api.get("last_wait_sec", 0.0) or 0.0),
                        "calls_last_60s": int(private_api.get("calls_last_60s", 0) or 0),
                        "effective_calls_per_sec": float(private_api.get("effective_calls_per_sec", 0.0) or 0.0),
                        "budget_available": float(private_api.get("budget_available", 0.0) or 0.0),
                        "consecutive_rate_errors": int(private_api.get("consecutive_rate_errors", 0) or 0),
                    },
                    "entry_scheduler": {
                        "cap_per_loop": int(self.entry_adds_per_loop_cap),
                        "used_this_loop": int(self.entry_adds_per_loop_used),
                        "pending_entries": int(pending_entries),
                        "deferred_total": int(self._entry_adds_deferred_total),
                        "drained_total": int(self._entry_adds_drained_total),
                        "last_deferred_at": self._entry_adds_last_deferred_at or None,
                        "last_drained_at": self._entry_adds_last_drained_at or None,
                    },
                },
                "balance_health": {
                    "usd_observed": observed_usd_balance,
                    "doge_observed": observed_doge_balance,
                    "balance_age_sec": balance_age_sec,
                    "usd_committed_internal": committed_usd_internal,
                    "doge_committed_internal": committed_doge_internal,
                    "loop_available_usd": self._loop_available_usd,
                    "loop_available_doge": self._loop_available_doge,
                    "ledger": self.ledger.snapshot(),
                },
                "balance_recon": self._compute_balance_recon(total_profit, total_unrealized_profit),
                "external_flows": self._external_flows_status_payload(now),
                "equity_history": self._equity_history_status_payload(now),
                "sticky_mode": {
                    "enabled": self._flag_value("STICKY_MODE_ENABLED"),
                    "target_slots": int(config.STICKY_TARGET_SLOTS),
                    "max_target_slots": int(config.STICKY_MAX_TARGET_SLOTS),
                    "compounding_mode": str(getattr(config, "STICKY_COMPOUNDING_MODE", "legacy_profit")),
                    "auto_release_enabled": self._flag_value("RELEASE_AUTO_ENABLED"),
                },
                "self_healing": self._self_healing_status_payload(now),
                "slot_vintage": slot_vintage,
                "hmm_data_pipeline": hmm_data_pipeline,
                "hmm_data_pipeline_secondary": hmm_data_pipeline_secondary,
                "hmm_data_pipeline_tertiary": hmm_data_pipeline_tertiary,
                "hmm_regime": hmm_regime,
                "hmm_consensus": hmm_consensus,
                "belief_state": belief_state_payload,
                "bocpd": bocpd_payload,
                "survival_model": survival_payload,
                "trade_beliefs": trade_beliefs_payload,
                "action_knobs": action_knobs_payload,
                "manifold_score": manifold_payload,
                "ops_panel": self._ops_panel_status_payload(),
                "regime_history_30m": list(self._regime_history_30m),
                "throughput_sizer": (
                    self._throughput.status_payload() if self._throughput is not None else {"enabled": False}
                ),
                "dust_sweep": {
                    "enabled": bool(self._dust_sweep_enabled),
                    "current_dividend_usd": dust_dividend_usd,
                    "lifetime_absorbed_usd": float(self._dust_last_absorbed_usd),
                    "available_usd": dust_available_usd,
                },
                "b_side_sizing": {
                    "base_usd": float(_bsb) if (_bsb := getattr(self, "_loop_b_side_base", None)) is not None else None,
                    "slot_count": len(self.slots),
                    "buy_ready_slots": int(buy_ready_slots),
                    "quote_first_enabled": bool(getattr(config, "QUOTE_FIRST_ALLOCATION", False)),
                    "carry_usd": float(self._quote_first_carry_usd),
                    "committed_buy_quote_usd": float((self._loop_quote_first_meta or {}).get("committed_buy_quote_usd", 0.0)),
                    "deployable_usd": float((self._loop_quote_first_meta or {}).get("deployable_usd", 0.0)),
                    "allocation_pool_usd": float((self._loop_quote_first_meta or {}).get("allocation_pool_usd", 0.0)),
                    "allocated_usd": float((self._loop_quote_first_meta or {}).get("allocated_usd", 0.0)),
                    "unallocated_spendable_usd": float((self._loop_quote_first_meta or {}).get("unallocated_spendable_usd", 0.0)),
                },
                "regime_directional": regime_directional,
                "ai_regime_advisor": ai_regime_advisor,
                "accumulation": self._accumulation_status_payload(now),
                "release_health": {
                    "sticky_release_total": int(self._sticky_release_total),
                    "sticky_release_last_at": self._sticky_release_last_at or None,
                    "recon_hard_gate_enabled": bool(config.RELEASE_RECON_HARD_GATE_ENABLED),
                    "recon_hard_gate_blocked": bool(self._release_recon_blocked),
                    "recon_hard_gate_reason": str(self._release_recon_blocked_reason or ""),
                },
                "doge_bias_scoreboard": self._compute_doge_bias_scoreboard(),
                "rebalancer": {
                    "enabled": self._flag_value("REBALANCE_ENABLED"),
                    "idle_ratio": float(self._rebalancer_idle_ratio),
                    "target": float(max(0.0, min(1.0, self._trend_dynamic_target))),
                    "base_target": float(max(0.0, min(1.0, float(config.REBALANCE_TARGET_IDLE_PCT)))),
                    "error": float(self._rebalancer_last_raw_error),
                    "smoothed_error": float(self._rebalancer_smoothed_error),
                    "velocity": float(self._rebalancer_smoothed_velocity),
                    "skew": float(self._rebalancer_current_skew),
                    "skew_direction": (
                        "buy_doge"
                        if self._rebalancer_current_skew > 1e-12
                        else "sell_doge"
                        if self._rebalancer_current_skew < -1e-12
                        else "neutral"
                    ),
                    "size_mult_a": (
                        min(
                            float(config.REBALANCE_MAX_SIZE_MULT),
                            1.0 + abs(float(self._rebalancer_current_skew)) * float(config.REBALANCE_SIZE_SENSITIVITY),
                        )
                        if self._rebalancer_current_skew < 0
                        else 1.0
                    ),
                    "size_mult_b": (
                        min(
                            float(config.REBALANCE_MAX_SIZE_MULT),
                            1.0 + abs(float(self._rebalancer_current_skew)) * float(config.REBALANCE_SIZE_SENSITIVITY),
                        )
                        if self._rebalancer_current_skew > 0
                        else 1.0
                    ),
                    "damped": bool(now < float(self._rebalancer_damped_until)),
                    "sign_flips_1h": len(self._rebalancer_sign_flip_history),
                    "capacity_band": self._rebalancer_last_capacity_band,
                },
                "trend": {
                    "score": float(self._trend_score),
                    "score_display": (
                        f"+{(float(self._trend_score) * 100.0):.2f}%"
                        if float(self._trend_score) > 0
                        else f"{(float(self._trend_score) * 100.0):.2f}%"
                    ),
                    "fast_ema": float(self._trend_fast_ema),
                    "slow_ema": float(self._trend_slow_ema),
                    "dynamic_idle_target": float(max(0.0, min(1.0, self._trend_dynamic_target))),
                    "hysteresis_active": bool(now < float(self._trend_target_locked_until)),
                    "hysteresis_expires_in_sec": int(
                        max(0.0, float(self._trend_target_locked_until) - now)
                    ),
                },
                "capital_layers": {
                    "target_layers": int(layer_metrics.get("target_layers", 0) or 0),
                    "effective_layers": int(layer_metrics.get("effective_layers", 0) or 0),
                    "max_target_layers": max(1, int(getattr(config, "CAPITAL_LAYER_MAX_TARGET_LAYERS", 20))),
                    "doge_per_order_per_layer": float(layer_metrics.get("doge_per_order_per_layer", 0.0) or 0.0),
                    "layer_order_budget": int(layer_metrics.get("layer_order_budget", 0) or 0),
                    "layer_step_doge_eq": float(layer_metrics.get("layer_step_doge_eq", 0.0) or 0.0),
                    "add_layer_usd_equiv_now": layer_metrics.get("add_layer_usd_equiv_now"),
                    "funding_source_default": str(config.CAPITAL_LAYER_DEFAULT_SOURCE),
                    "active_sell_orders": int(layer_metrics.get("active_sell_orders", 0) or 0),
                    "active_buy_orders": int(layer_metrics.get("active_buy_orders", 0) or 0),
                    "orders_at_funded_size": int(orders_at_funded_size),
                    "open_orders_total": int(layer_metrics.get("open_orders_total", 0) or 0),
                    "gap_layers": int(layer_metrics.get("gap_layers", 0) or 0),
                    "gap_doge_now": float(layer_metrics.get("gap_doge_now", 0.0) or 0.0),
                    "gap_usd_now": float(layer_metrics.get("gap_usd_now", 0.0) or 0.0),
                    "last_add_layer_event": self.layer_last_add_event,
                },
                "slots": slots,
            }


_RUNTIME: BotRuntime | None = None


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        logger.info("HTTP %s - %s", self.address_string(), fmt % args)

    def _send_json(self, data: dict, code: int = 200) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid request body") from exc
        if not isinstance(body, dict):
            raise ValueError("invalid request body")
        return body

    def do_GET(self) -> None:  # noqa: N802
        global _RUNTIME
        if self.path == "/" or self.path.startswith("/?"):
            body = dashboard.DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/factory" or self.path.startswith("/factory?"):
            import factory_viz

            body = factory_viz.FACTORY_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/api/status") or self.path.startswith("/api/swarm/status"):
            if _RUNTIME is None:
                self._send_json({"error": "runtime not ready"}, 503)
                return
            self._send_json(_RUNTIME.status_payload())
            return

        if self.path.startswith("/api/ops/toggles"):
            if _RUNTIME is None:
                self._send_json({"error": "runtime not ready"}, 503)
                return
            with _RUNTIME.lock:
                self._send_json(_RUNTIME._ops_toggles_payload())
            return

        if self.path.startswith("/api/churner/status"):
            if _RUNTIME is None:
                self._send_json({"error": "runtime not ready"}, 503)
                return
            with _RUNTIME.lock:
                self._send_json(_RUNTIME._churner_status_payload())
            return

        if self.path.startswith("/api/churner/candidates"):
            if _RUNTIME is None:
                self._send_json({"error": "runtime not ready"}, 503)
                return
            with _RUNTIME.lock:
                self._send_json(_RUNTIME._churner_candidates_payload())
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        global _RUNTIME
        try:
            if self.path.startswith("/api/ops/"):
                if _RUNTIME is None:
                    self._send_json({"ok": False, "message": "runtime not ready"}, 503)
                    return
                try:
                    body = self._read_json()
                except Exception:
                    self._send_json({"ok": False, "message": "invalid request body"}, 400)
                    return

                def _parse_bool_value(raw: Any) -> tuple[bool, bool]:
                    if isinstance(raw, bool):
                        return bool(raw), True
                    if isinstance(raw, (int, float)):
                        if float(raw) in (0.0, 1.0):
                            return bool(int(raw)), True
                        return False, False
                    text = str(raw or "").strip().lower()
                    if text in {"1", "true", "yes", "on"}:
                        return True, True
                    if text in {"0", "false", "no", "off"}:
                        return False, True
                    return False, False

                with _RUNTIME.lock:
                    if self.path.startswith("/api/ops/toggle"):
                        key = str(body.get("key", "")).strip().upper()
                        if not key:
                            self._send_json({"ok": False, "message": "toggle key required"}, 400)
                            return
                        value, ok_bool = _parse_bool_value(body.get("value", None))
                        if not ok_bool:
                            self._send_json({"ok": False, "message": "invalid toggle value (expected bool)"}, 400)
                            return
                        ok, msg = _RUNTIME._set_runtime_override(key, value)
                        if ok:
                            _RUNTIME._save_snapshot()
                        self._send_json(
                            {
                                "ok": bool(ok),
                                "message": str(msg),
                                "ops_panel": _RUNTIME._ops_panel_status_payload(),
                            },
                            200 if ok else 400,
                        )
                        return

                    if self.path.startswith("/api/ops/reset-all"):
                        cleared = int(_RUNTIME._clear_all_runtime_overrides())
                        _RUNTIME._save_snapshot()
                        self._send_json(
                            {
                                "ok": True,
                                "message": f"cleared {cleared} overrides",
                                "cleared": cleared,
                                "ops_panel": _RUNTIME._ops_panel_status_payload(),
                            },
                            200,
                        )
                        return

                    if self.path.startswith("/api/ops/reset"):
                        key = str(body.get("key", "")).strip().upper()
                        if not key:
                            self._send_json({"ok": False, "message": "toggle key required"}, 400)
                            return
                        ok, msg = _RUNTIME._clear_runtime_override(key)
                        if ok:
                            _RUNTIME._save_snapshot()
                        self._send_json(
                            {
                                "ok": bool(ok),
                                "message": str(msg),
                                "ops_panel": _RUNTIME._ops_panel_status_payload(),
                            },
                            200 if ok else 400,
                        )
                        return

                self._send_json({"ok": False, "message": "not found"}, 404)
                return

            if self.path.startswith("/api/churner/"):
                if _RUNTIME is None:
                    self._send_json({"ok": False, "message": "runtime not ready"}, 503)
                    return
                try:
                    body = self._read_json()
                except Exception:
                    self._send_json({"ok": False, "message": "invalid request body"}, 400)
                    return

                with _RUNTIME.lock:
                    if self.path.startswith("/api/churner/spawn"):
                        try:
                            slot_id = int(body.get("slot_id", -1))
                        except (TypeError, ValueError):
                            self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
                            return
                        if slot_id < 0:
                            self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
                            return
                        position_id_raw = body.get("position_id", None)
                        position_id: int | None = None
                        if position_id_raw not in (None, ""):
                            try:
                                position_id = int(position_id_raw)
                            except (TypeError, ValueError):
                                self._send_json({"ok": False, "message": "invalid position_id"}, 400)
                                return
                            if position_id <= 0:
                                self._send_json({"ok": False, "message": "invalid position_id"}, 400)
                                return
                        ok, msg = _RUNTIME._churner_spawn(slot_id=slot_id, position_id=position_id)
                        if ok:
                            _RUNTIME._save_snapshot()
                        self._send_json(
                            {
                                "ok": bool(ok),
                                "message": str(msg),
                                "churner": _RUNTIME._churner_status_payload(),
                            },
                            200 if ok else 400,
                        )
                        return

                    if self.path.startswith("/api/churner/kill"):
                        try:
                            slot_id = int(body.get("slot_id", -1))
                        except (TypeError, ValueError):
                            self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
                            return
                        if slot_id < 0:
                            self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
                            return
                        ok, msg = _RUNTIME._churner_kill(slot_id=slot_id)
                        if ok:
                            _RUNTIME._save_snapshot()
                        self._send_json(
                            {
                                "ok": bool(ok),
                                "message": str(msg),
                                "churner": _RUNTIME._churner_status_payload(),
                            },
                            200 if ok else 400,
                        )
                        return

                    if self.path.startswith("/api/churner/config"):
                        if "reserve_usd" not in body:
                            self._send_json({"ok": False, "message": "reserve_usd required"}, 400)
                            return
                        try:
                            reserve_usd = float(body.get("reserve_usd", 0.0))
                        except (TypeError, ValueError):
                            self._send_json({"ok": False, "message": "invalid reserve_usd"}, 400)
                            return
                        if not isfinite(reserve_usd):
                            self._send_json({"ok": False, "message": "invalid reserve_usd"}, 400)
                            return
                        ok, msg = _RUNTIME._churner_update_runtime_config(reserve_usd=reserve_usd)
                        if ok:
                            _RUNTIME._save_snapshot()
                        self._send_json(
                            {
                                "ok": bool(ok),
                                "message": str(msg),
                                "churner": _RUNTIME._churner_status_payload(),
                            },
                            200 if ok else 400,
                        )
                        return

                self._send_json({"ok": False, "message": "not found"}, 404)
                return

            if not self.path.startswith("/api/action"):
                self._send_json({"ok": False, "message": "not found"}, 404)
                return
            if _RUNTIME is None:
                self._send_json({"ok": False, "message": "runtime not ready"}, 503)
                return

            try:
                body = self._read_json()
            except Exception:
                self._send_json({"ok": False, "message": "invalid request body"}, 400)
                return

            action = (body.get("action") or "").strip()
            parsed: dict[str, float | int | str] = {}
            flag_reader = getattr(_RUNTIME, "_flag_value", None)
            if callable(flag_reader):
                sticky_mode_enabled = bool(flag_reader("STICKY_MODE_ENABLED"))
            else:
                sticky_mode_enabled = bool(getattr(config, "STICKY_MODE_ENABLED", False))
            recovery_orders_enabled = _recovery_orders_enabled_flag()
            if sticky_mode_enabled and action in ("soft_close", "soft_close_next", "cancel_stale_recoveries"):
                self._send_json(
                    {"ok": False, "message": f"{action} disabled in sticky mode; use release_slot"},
                    400,
                )
                return
            if not recovery_orders_enabled and action in ("soft_close", "soft_close_next", "cancel_stale_recoveries"):
                self._send_json({"ok": False, "message": _recovery_disabled_message(action)}, 400)
                return

            if action in ("set_entry_pct", "set_profit_pct"):
                try:
                    parsed["value"] = float(body.get("value", 0))
                    if not isfinite(parsed["value"]):
                        raise ValueError("non-finite value")
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid numeric value"}, 400)
                    return
            elif action == "soft_close":
                try:
                    parsed["slot_id"] = int(body.get("slot_id", 0))
                    parsed["recovery_id"] = int(body.get("recovery_id", 0))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid slot/recovery id"}, 400)
                    return
            elif action == "release_slot":
                try:
                    parsed["slot_id"] = int(body.get("slot_id", 0))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
                    return
                local_id_raw = body.get("local_id", body.get("exit_local_id"))
                if local_id_raw not in (None, ""):
                    try:
                        parsed["local_id"] = int(local_id_raw)
                    except (TypeError, ValueError):
                        self._send_json({"ok": False, "message": "invalid local_id"}, 400)
                        return
                trade_id_raw = body.get("trade_id", "")
                trade_id = str(trade_id_raw).strip().upper()
                if trade_id:
                    if trade_id not in {"A", "B"}:
                        self._send_json({"ok": False, "message": "invalid trade_id (expected A or B)"}, 400)
                        return
                    parsed["trade_id"] = trade_id
            elif action == "release_oldest_eligible":
                try:
                    parsed["slot_id"] = int(body.get("slot_id", 0))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
                    return
            elif action == "cancel_stale_recoveries":
                try:
                    parsed["min_distance_pct"] = float(body.get("min_distance_pct", 3.0))
                except (TypeError, ValueError):
                    parsed["min_distance_pct"] = 3.0
                try:
                    parsed["max_batch"] = int(body.get("max_batch", 8))
                except (TypeError, ValueError):
                    parsed["max_batch"] = 8
            elif action == "remove_slot":
                try:
                    parsed["slot_id"] = int(body.get("slot_id", -1))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid slot_id"}, 400)
                    return
            elif action == "remove_slots":
                try:
                    parsed["count"] = int(body.get("count", 1))
                except (TypeError, ValueError):
                    parsed["count"] = 1
            elif action == "add_layer":
                source = str(body.get("source", config.CAPITAL_LAYER_DEFAULT_SOURCE)).strip().upper()
                if source not in {"AUTO", "DOGE", "USD"}:
                    self._send_json({"ok": False, "message": "invalid layer source"}, 400)
                    return
                parsed["source"] = source
            elif action == "remove_layer":
                pass
            elif action == "ai_regime_override":
                ttl_raw = body.get("ttl_sec", None)
                if ttl_raw in (None, ""):
                    parsed["ttl_sec"] = int(getattr(config, "AI_OVERRIDE_TTL_SEC", 1800))
                else:
                    try:
                        parsed["ttl_sec"] = int(ttl_raw)
                    except (TypeError, ValueError):
                        self._send_json({"ok": False, "message": "invalid ttl_sec"}, 400)
                        return
            elif action in ("self_heal_reprice_breakeven", "self_heal_close_market", "self_heal_keep_holding"):
                try:
                    parsed["position_id"] = int(body.get("position_id", 0))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "message": "invalid position_id"}, 400)
                    return
                if int(parsed.get("position_id", 0)) <= 0:
                    self._send_json({"ok": False, "message": "invalid position_id"}, 400)
                    return
                reason = str(body.get("reason", body.get("operator_reason", "")) or "").strip()
                if reason:
                    parsed["reason"] = reason[:160]
                if action == "self_heal_keep_holding":
                    hold_raw = body.get("hold_sec", None)
                    if hold_raw not in (None, ""):
                        try:
                            parsed["hold_sec"] = float(hold_raw)
                        except (TypeError, ValueError):
                            self._send_json({"ok": False, "message": "invalid hold_sec"}, 400)
                            return
            elif action in ("ai_regime_revert", "ai_regime_dismiss", "accum_stop"):
                pass
            elif action in ("pause", "resume", "add_slot", "soft_close_next", "reconcile_drift", "audit_pnl"):
                pass
            else:
                self._send_json({"ok": False, "message": f"unknown action: {action}"}, 400)
                return

            with _RUNTIME.lock:
                ok = True
                msg = "ok"
                if action == "pause":
                    _RUNTIME.pause("paused from dashboard")
                    msg = "paused"
                elif action == "resume":
                    ok, msg = _RUNTIME.resume()
                elif action == "add_slot":
                    ok, msg = _RUNTIME.add_slot()
                elif action == "add_layer":
                    ok, msg = _RUNTIME.add_layer(str(parsed.get("source", config.CAPITAL_LAYER_DEFAULT_SOURCE)))
                elif action == "remove_layer":
                    ok, msg = _RUNTIME.remove_layer()
                elif action == "set_entry_pct":
                    ok, msg = _RUNTIME.set_entry_pct(float(parsed["value"]))
                elif action == "set_profit_pct":
                    ok, msg = _RUNTIME.set_profit_pct(float(parsed["value"]))
                elif action == "soft_close":
                    ok, msg = _RUNTIME.soft_close(int(parsed["slot_id"]), int(parsed["recovery_id"]))
                elif action == "release_slot":
                    local_id = int(parsed["local_id"]) if "local_id" in parsed else None
                    trade_id = str(parsed["trade_id"]) if "trade_id" in parsed else None
                    ok, msg = _RUNTIME.release_slot(int(parsed["slot_id"]), local_id=local_id, trade_id=trade_id)
                elif action == "release_oldest_eligible":
                    ok, msg = _RUNTIME.release_oldest_eligible(int(parsed["slot_id"]))
                elif action == "soft_close_next":
                    ok, msg = _RUNTIME.soft_close_next()
                elif action == "cancel_stale_recoveries":
                    ok, msg = _RUNTIME.cancel_stale_recoveries(
                        float(parsed.get("min_distance_pct", 3.0)),
                        int(parsed.get("max_batch", 8)),
                    )
                elif action == "remove_slot":
                    ok, msg = _RUNTIME.remove_slot(int(parsed["slot_id"]))
                elif action == "remove_slots":
                    ok, msg = _RUNTIME.remove_slots(int(parsed.get("count", 1)))
                elif action == "reconcile_drift":
                    ok, msg = _RUNTIME.reconcile_drift()
                elif action == "audit_pnl":
                    ok, msg = _RUNTIME.audit_pnl()
                elif action == "ai_regime_override":
                    ok, msg = _RUNTIME.apply_ai_regime_override(int(parsed.get("ttl_sec", 0)))
                elif action == "ai_regime_revert":
                    ok, msg = _RUNTIME.revert_ai_regime_override()
                elif action == "ai_regime_dismiss":
                    ok, msg = _RUNTIME.dismiss_ai_regime_opinion()
                elif action == "accum_stop":
                    ok, msg = _RUNTIME.stop_accumulation()
                elif action == "self_heal_reprice_breakeven":
                    ok, msg = _RUNTIME.self_heal_reprice_breakeven(
                        int(parsed.get("position_id", 0)),
                        operator_reason=str(parsed.get("reason", "")),
                    )
                elif action == "self_heal_close_market":
                    ok, msg = _RUNTIME.self_heal_close_at_market(
                        int(parsed.get("position_id", 0)),
                        operator_reason=str(parsed.get("reason", "")),
                    )
                elif action == "self_heal_keep_holding":
                    hold_sec_raw = parsed.get("hold_sec", None)
                    hold_sec = float(hold_sec_raw) if hold_sec_raw is not None else None
                    ok, msg = _RUNTIME.self_heal_keep_holding(
                        int(parsed.get("position_id", 0)),
                        operator_reason=str(parsed.get("reason", "")),
                        hold_sec=hold_sec,
                    )
                _RUNTIME._save_snapshot()

            self._send_json({"ok": bool(ok), "message": str(msg)}, 200 if ok else 400)
        except Exception:
            logger.exception("Unhandled exception in API POST")
            self._send_json({"ok": False, "message": "internal server error"}, 500)


def start_http_server() -> ThreadingHTTPServer | None:
    if config.HEALTH_PORT <= 0:
        return None
    server = ThreadingHTTPServer(("0.0.0.0", int(config.HEALTH_PORT)), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dashboard-server")
    thread.start()
    logger.info("Dashboard server started on :%s", config.HEALTH_PORT)
    return server


def run() -> None:
    global _RUNTIME
    setup_logging()

    rt = BotRuntime()
    _RUNTIME = rt

    def _handle_signal(signum, _frame):
        logger.info("Signal %s received", signum)
        rt.shutdown(f"signal {signum}")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    server = None
    try:
        # Start health/dashboard server before heavy startup initialization so
        # platform health checks can succeed while we warm up.
        server = start_http_server()
        rt.initialize()

        poll = max(5, int(config.POLL_INTERVAL_SECONDS))
        logger.info("Entering main loop (every %ss)", poll)

        while rt.running:
            loop_start = _now()
            try:
                with rt.lock:
                    rt.begin_loop()
                    rt.run_loop_once()
                    rt.poll_telegram()
                    rt.end_loop()
            except Exception as e:
                logger.exception("Main loop error: %s", e)
                rt.consecutive_api_errors += 1
                with rt.lock:
                    rt.end_loop()
                if rt.consecutive_api_errors >= config.MAX_CONSECUTIVE_ERRORS:
                    rt.pause(f"loop errors: {rt.consecutive_api_errors}")

            elapsed = _now() - loop_start
            sleep_for = max(0.2, poll - elapsed)
            time.sleep(sleep_for)

    finally:
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
        if _RUNTIME is not None:
            _RUNTIME.shutdown("process exit")


if __name__ == "__main__":
    run()
