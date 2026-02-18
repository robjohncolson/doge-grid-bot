"""
signal_digest.py -- Rule-based Signal Digest diagnostics.

This module is intentionally pure and side-effect free so rules can be tested
independently from bot runtime wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Mapping


_SEVERITY_RANK = {"green": 0, "amber": 1, "red": 2}
_VALID_SEVERITIES = frozenset(_SEVERITY_RANK.keys())

RULE_PRIORITY = {
    "headroom": 1,
    "regime_confidence": 2,
    "boundary_risk": 3,
    "timeframe_agreement": 4,
    "macd_momentum": 5,
    "rsi_zone": 6,
    "ema_trend": 7,
    "ranger_health": 8,
    "exit_distance": 9,
    "age_skew": 10,
    "capital_efficiency": 11,
    "mts_trend": 12,
}

DEFAULT_DIGEST_CONFIG: dict[str, float | int | bool] = {
    "DIGEST_EMA_AMBER_THRESHOLD": 0.003,
    "DIGEST_EMA_RED_THRESHOLD": 0.01,
    "DIGEST_RSI_AMBER_ZONE": 0.2,
    "DIGEST_RSI_RED_ZONE": 0.4,
    "DIGEST_AGE_AMBER_PCT": 30.0,
    "DIGEST_AGE_RED_PCT": 60.0,
    "DIGEST_EXIT_AMBER_PCT": 3.0,
    "DIGEST_EXIT_RED_PCT": 8.0,
    "DIGEST_HEADROOM_AMBER": 50,
    "DIGEST_HEADROOM_RED": 20,
}


@dataclass
class DiagnosticCheck:
    signal: str
    severity: str
    title: str
    detail: str
    value: float
    threshold: str


@dataclass
class MarketInterpretation:
    narrative: str = ""
    key_insight: str = ""
    watch_for: str = ""
    config_assessment: str = "borderline"
    config_suggestion: str = ""
    panelist: str = ""
    ts: float = 0.0


@dataclass
class SignalDigestResult:
    light: str
    top_concern: str
    checks: list[DiagnosticCheck] = field(default_factory=list)


def _to_float(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN
        return None
    return num


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_path(data: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node.get(key)
    return node


def _first(data: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _get_path(data, path)
        if value is not None:
            return value
    return None


def _cfg_float(cfg: Mapping[str, Any], name: str) -> float:
    value = _to_float(cfg.get(name))
    default = _to_float(DEFAULT_DIGEST_CONFIG.get(name))
    return float(value if value is not None else default if default is not None else 0.0)


def _cfg_int(cfg: Mapping[str, Any], name: str) -> int:
    value = _to_int(cfg.get(name))
    default = _to_int(DEFAULT_DIGEST_CONFIG.get(name))
    return int(value if value is not None else default if default is not None else 0)


def _severity(value: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_SEVERITIES else "green"


def _format_pct(value: float, digits: int = 2) -> str:
    return f"{value * 100:.{digits}f}%"


def _format_num(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}"


def _rule_ema_trend(snapshot: Mapping[str, Any], cfg: Mapping[str, Any]) -> DiagnosticCheck:
    amber_th = abs(_cfg_float(cfg, "DIGEST_EMA_AMBER_THRESHOLD"))
    red_th = max(amber_th, abs(_cfg_float(cfg, "DIGEST_EMA_RED_THRESHOLD")))
    trend_score = _to_float(
        _first(
            snapshot,
            ("trend_score",),
            ("trend", "score"),
            ("hmm_observation", "ema_spread_pct"),
            ("ema_spread_pct",),
        )
    )
    if trend_score is None:
        return DiagnosticCheck(
            signal="ema_trend",
            severity="green",
            title="EMA Trend",
            detail="EMA trend unavailable yet. Waiting for fresh indicator samples.",
            value=0.0,
            threshold=f"amber >= {_format_pct(amber_th, 3)}",
        )

    mag = abs(trend_score)
    direction = "bullish" if trend_score > 0 else "bearish" if trend_score < 0 else "neutral"
    if mag < amber_th:
        severity = "green"
        detail = "EMAs converged - price in equilibrium. Ideal for grid cycling."
        threshold = f"< {_format_pct(amber_th, 3)}"
    elif mag < red_th:
        severity = "amber"
        detail = (
            f"EMAs diverging ({_format_pct(mag, 2)}), developing {direction} lean. "
            "Entries on the unfavored side may orphan more."
        )
        threshold = f">= {_format_pct(amber_th, 3)}"
    else:
        severity = "red"
        detail = (
            f"Strong EMA divergence ({_format_pct(mag, 2)}) - trending market. "
            "Grid cycling against trend risks orphan accumulation."
        )
        threshold = f">= {_format_pct(red_th, 2)}"
    return DiagnosticCheck("ema_trend", severity, "EMA Trend", detail, float(trend_score), threshold)


def _rule_rsi_zone(snapshot: Mapping[str, Any], cfg: Mapping[str, Any]) -> DiagnosticCheck:
    amber_zone = abs(_cfg_float(cfg, "DIGEST_RSI_AMBER_ZONE"))
    red_zone = max(amber_zone, abs(_cfg_float(cfg, "DIGEST_RSI_RED_ZONE")))
    rsi_zone = _to_float(
        _first(
            snapshot,
            ("rsi_zone",),
            ("hmm_observation", "rsi_zone"),
            ("hmm_regime", "observation", "rsi_zone"),
        )
    )
    raw_rsi = _to_float(_first(snapshot, ("rsi_raw",), ("hmm_observation", "rsi_raw")))
    if rsi_zone is None:
        return DiagnosticCheck(
            signal="rsi_zone",
            severity="green",
            title="RSI Zone",
            detail="RSI zone unavailable yet. Waiting for fresh indicator samples.",
            value=0.0,
            threshold=f"amber outside +/-{amber_zone:.2f}",
        )
    if raw_rsi is None:
        raw_rsi = 50.0 + (rsi_zone * 50.0)

    mag = abs(rsi_zone)
    side = "overbought" if rsi_zone > 0 else "oversold" if rsi_zone < 0 else "neutral"
    if mag <= amber_zone:
        severity = "green"
        detail = f"RSI neutral ({raw_rsi:.1f}) - no momentum extreme. Grid entries remain balanced."
        threshold = f"|zone| <= {amber_zone:.2f}"
    elif mag <= red_zone:
        severity = "amber"
        detail = (
            f"RSI in {side} territory ({raw_rsi:.1f}) - one side exits may fill faster, "
            "but reversal risk is increasing."
        )
        threshold = f"|zone| > {amber_zone:.2f}"
    else:
        severity = "red"
        detail = (
            f"RSI extreme ({raw_rsi:.1f}) - strongly {side}. "
            "Mean reversion odds are rising and fresh entries are riskier."
        )
        threshold = f"|zone| > {red_zone:.2f}"
    return DiagnosticCheck("rsi_zone", severity, "RSI Zone", detail, float(rsi_zone), threshold)


def _rule_macd_momentum(snapshot: Mapping[str, Any], _cfg: Mapping[str, Any]) -> DiagnosticCheck:
    slope = _to_float(
        _first(
            snapshot,
            ("macd_hist_slope",),
            ("hmm_observation", "macd_hist_slope"),
            ("hmm_regime", "observation", "macd_hist_slope"),
        )
    )
    regime = str(
        _first(
            snapshot,
            ("regime",),
            ("hmm_regime", "regime"),
            ("hmm_consensus", "effective_regime"),
        )
        or ""
    ).strip().upper()
    if slope is None:
        return DiagnosticCheck(
            signal="macd_momentum",
            severity="green",
            title="MACD Momentum",
            detail="MACD slope unavailable yet. Waiting for fresh indicator samples.",
            value=0.0,
            threshold="|slope| < 1e-6",
        )

    if abs(slope) < 1e-6:
        return DiagnosticCheck(
            "macd_momentum",
            "green",
            "MACD Momentum",
            "MACD flat - no momentum acceleration. Stable range conditions.",
            float(slope),
            "|slope| < 1e-6",
        )

    slope_dir = "bullish" if slope > 0 else "bearish"
    if regime == "RANGING":
        return DiagnosticCheck(
            "macd_momentum",
            "amber",
            "MACD Momentum",
            f"MACD turning {slope_dir} while regime is RANGING - watch for regime transition.",
            float(slope),
            "non-zero slope in RANGING",
        )

    expected = ""
    if regime == "BULLISH":
        expected = "bullish"
    elif regime == "BEARISH":
        expected = "bearish"

    if expected and expected == slope_dir:
        return DiagnosticCheck(
            "macd_momentum",
            "green",
            "MACD Momentum",
            f"MACD confirms {regime} regime - momentum and regime are aligned.",
            float(slope),
            "slope aligns with regime",
        )

    if expected:
        return DiagnosticCheck(
            "macd_momentum",
            "red",
            "MACD Momentum",
            f"MACD/regime conflict: momentum is {slope_dir} but regime reads {regime}.",
            float(slope),
            "slope opposes regime",
        )

    return DiagnosticCheck(
        "macd_momentum",
        "amber",
        "MACD Momentum",
        f"MACD is {slope_dir}, but regime label is unavailable.",
        float(slope),
        "regime unavailable",
    )


def _rule_regime_confidence(snapshot: Mapping[str, Any], _cfg: Mapping[str, Any]) -> DiagnosticCheck:
    confidence = _to_float(
        _first(
            snapshot,
            ("regime_confidence",),
            ("hmm_regime", "confidence_effective"),
            ("hmm_consensus", "effective_confidence"),
            ("hmm_regime", "confidence"),
        )
    )
    regime = str(
        _first(
            snapshot,
            ("regime",),
            ("hmm_regime", "regime"),
            ("hmm_consensus", "effective_regime"),
        )
        or "RANGING"
    ).strip().upper()

    if confidence is None:
        return DiagnosticCheck(
            "regime_confidence",
            "amber",
            "Regime Confidence",
            "Regime confidence unavailable. Treat current regime label as provisional.",
            0.0,
            "confidence unavailable",
        )

    if confidence >= 0.80:
        severity = "green"
        detail = f"Regime confidence {_format_pct(confidence, 1)} - HMM strongly believes {regime}."
        threshold = ">= 80%"
    elif confidence >= 0.50:
        severity = "amber"
        detail = f"Regime confidence only {_format_pct(confidence, 1)} - likely transition noise."
        threshold = "50% to <80%"
    else:
        severity = "red"
        detail = (
            f"Regime confidence low ({_format_pct(confidence, 1)}) - regime call is effectively a guess."
        )
        threshold = "< 50%"
    return DiagnosticCheck("regime_confidence", severity, "Regime Confidence", detail, float(confidence), threshold)


def _rule_timeframe_agreement(snapshot: Mapping[str, Any], _cfg: Mapping[str, Any]) -> DiagnosticCheck:
    tf_1m = str(_first(snapshot, ("timeframes", "1m"), ("hmm_regime", "primary", "regime")) or "").strip().upper()
    tf_15m = str(
        _first(snapshot, ("timeframes", "15m"), ("hmm_regime", "secondary", "regime")) or ""
    ).strip().upper()
    tf_1h = str(_first(snapshot, ("timeframes", "1h"), ("hmm_regime", "tertiary", "regime")) or "").strip().upper()
    labels = [label for label in (tf_1m, tf_15m, tf_1h) if label]
    if len(labels) < 3:
        return DiagnosticCheck(
            "timeframe_agreement",
            "amber",
            "Timeframe Agreement",
            "Incomplete timeframe regime data. Treat directional conviction as moderate.",
            float(len(labels)) / 3.0,
            "requires 1m/15m/1h labels",
        )

    uniq = set(labels)
    if len(uniq) == 1:
        detail = f"Full agreement - 1m/15m/1h all read {tf_1m}."
        return DiagnosticCheck("timeframe_agreement", "green", "Timeframe Agreement", detail, 1.0, "all three match")

    if len(uniq) == 3:
        detail = "No timeframe agreement - market likely in transition."
        return DiagnosticCheck("timeframe_agreement", "red", "Timeframe Agreement", detail, 0.0, "all three differ")

    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    majority = max(counts, key=counts.get)
    dissent = next(label for label in labels if label != majority)
    detail = f"2/3 agreement - one timeframe ({dissent}) diverges from {majority}."
    return DiagnosticCheck(
        "timeframe_agreement",
        "amber",
        "Timeframe Agreement",
        detail,
        2.0 / 3.0,
        "2 of 3 match",
    )


def _rule_boundary_risk(snapshot: Mapping[str, Any], _cfg: Mapping[str, Any]) -> DiagnosticCheck:
    risk = str(_first(snapshot, ("boundary_risk",), ("belief_state", "boundary_risk")) or "").strip().lower()
    p_switch = _to_float(_first(snapshot, ("p_switch",), ("belief_state", "p_switch_consensus"))) or 0.0
    if risk not in {"low", "medium", "high"}:
        if p_switch >= 0.25:
            risk = "high"
        elif p_switch >= 0.10:
            risk = "medium"
        else:
            risk = "low"

    if risk == "low":
        severity = "green"
        detail = f"Low switch probability ({_format_pct(p_switch, 1)}) - stable grid conditions."
    elif risk == "medium":
        severity = "amber"
        detail = f"Moderate switch probability ({_format_pct(p_switch, 1)}) - regime boundary nearby."
    else:
        severity = "red"
        detail = f"High switch probability ({_format_pct(p_switch, 1)}) - regime change risk is elevated."
    return DiagnosticCheck("boundary_risk", severity, "Boundary Risk", detail, float(p_switch), f"risk={risk}")


def _rule_age_skew(snapshot: Mapping[str, Any], cfg: Mapping[str, Any]) -> DiagnosticCheck:
    age_bands = _first(snapshot, ("age_bands",), ("self_healing", "age_bands"))
    age_bands = age_bands if isinstance(age_bands, Mapping) else {}
    fresh = _to_int(age_bands.get("fresh")) or 0
    aging = _to_int(age_bands.get("aging")) or 0
    stale = _to_int(age_bands.get("stale")) or 0
    stuck = _to_int(age_bands.get("stuck")) or 0
    write_off = _to_int(age_bands.get("write_off")) or 0
    total = fresh + aging + stale + stuck + write_off
    bad = stuck + write_off
    if total <= 0:
        return DiagnosticCheck(
            "age_skew",
            "green",
            "Position Age",
            "No open positions to age-profile yet.",
            0.0,
            "< warmup",
        )

    bad_pct = (bad / float(total)) * 100.0
    amber_pct = _cfg_float(cfg, "DIGEST_AGE_AMBER_PCT")
    red_pct = max(amber_pct, _cfg_float(cfg, "DIGEST_AGE_RED_PCT"))
    if bad_pct < amber_pct:
        severity = "green"
        detail = "Healthy age distribution - most exits remain within market reach."
    elif bad_pct < red_pct:
        severity = "amber"
        detail = (
            f"{bad} of {total} positions ({bad_pct:.1f}%) are stuck/write-off - meaningful capital lock-up."
        )
    else:
        severity = "red"
        detail = f"{bad} of {total} positions ({bad_pct:.1f}%) are stuck/write-off - majority capital is frozen."
    return DiagnosticCheck("age_skew", severity, "Position Age", detail, bad / float(total), f">= {amber_pct:.1f}%")


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(median(values))


def _rule_exit_distance(snapshot: Mapping[str, Any], cfg: Mapping[str, Any]) -> DiagnosticCheck:
    explicit_median = _to_float(_first(snapshot, ("exit_distance_median_pct",), ("exit_distance", "median_pct")))
    explicit_count = _to_int(_first(snapshot, ("exit_distance_count",), ("exit_distance", "count"))) or 0

    median_pct = explicit_median
    count = explicit_count
    if median_pct is None:
        positions = _first(snapshot, ("trade_beliefs", "positions"), ("positions",))
        by_side: dict[str, list[float]] = {"buy": [], "sell": []}
        if isinstance(positions, list):
            for row in positions:
                if not isinstance(row, Mapping):
                    continue
                dist = _to_float(row.get("distance_from_market_pct"))
                if dist is None:
                    continue
                side = str(row.get("side") or "").strip().lower()
                if side not in by_side:
                    side = "buy" if str(row.get("trade_id") or "").strip().upper() == "B" else "sell"
                if side in by_side:
                    by_side[side].append(abs(dist))
        side_medians = {side: _median(vals) for side, vals in by_side.items() if vals}
        if side_medians:
            worst_side = max(side_medians, key=side_medians.get)
            median_pct = float(side_medians[worst_side])
            count = len(by_side.get(worst_side, []))

    if median_pct is None:
        return DiagnosticCheck(
            "exit_distance",
            "green",
            "Exit Distance",
            "No open exits found for distance analysis.",
            0.0,
            "no open exits",
        )

    amber = _cfg_float(cfg, "DIGEST_EXIT_AMBER_PCT")
    red = max(amber, _cfg_float(cfg, "DIGEST_EXIT_RED_PCT"))
    if median_pct < amber:
        severity = "green"
        detail = f"Exits close to market (median {median_pct:.2f}%) - normal grid operation."
    elif median_pct < red:
        severity = "amber"
        detail = f"Exits moderately distant (median {median_pct:.2f}%) - mean reversion required for fills."
    else:
        severity = "red"
        detail = (
            f"Exits far from market (median {median_pct:.2f}%) - {count} positions need major reversal to fill."
        )
    return DiagnosticCheck("exit_distance", severity, "Exit Distance", detail, float(median_pct), f">= {amber:.1f}%")


def _rule_headroom(snapshot: Mapping[str, Any], cfg: Mapping[str, Any]) -> DiagnosticCheck:
    headroom = _to_int(
        _first(
            snapshot,
            ("headroom",),
            ("capacity_fill_health", "open_order_headroom"),
        )
    )
    amber = _cfg_int(cfg, "DIGEST_HEADROOM_AMBER")
    red = min(amber, _cfg_int(cfg, "DIGEST_HEADROOM_RED"))
    if headroom is None:
        return DiagnosticCheck(
            "headroom",
            "amber",
            "Order Headroom",
            "Order headroom unavailable. Capacity safety cannot be confirmed.",
            0.0,
            "headroom unavailable",
        )
    if headroom >= amber:
        severity = "green"
        detail = f"Plenty of order headroom ({headroom} slots available)."
    elif headroom >= red:
        severity = "amber"
        detail = f"Order headroom narrowing ({headroom} remaining) - approaching exchange cap."
    else:
        severity = "red"
        detail = f"Critical order headroom ({headroom} remaining) - avoid placing new entries."
    return DiagnosticCheck("headroom", severity, "Order Headroom", detail, float(headroom), f"amber < {amber}, red < {red}")


def _rule_ranger_health(snapshot: Mapping[str, Any], _cfg: Mapping[str, Any]) -> DiagnosticCheck:
    rangers = _first(snapshot, ("rangers",), ("ranger",))
    rangers = rangers if isinstance(rangers, Mapping) else {}
    enabled = bool(rangers.get("enabled", False))
    if not enabled:
        return DiagnosticCheck(
            "ranger_health",
            "green",
            "Ranger Health",
            "Rangers disabled - not applicable.",
            0.0,
            "rangers disabled",
        )

    regime_ok = bool(rangers.get("regime_ok", False))
    cycles = _to_int(rangers.get("cycles_today")) or 0
    active = _to_int(rangers.get("active")) or 0
    orphans = _to_int(rangers.get("orphans_today")) or 0
    profit_today = _to_float(rangers.get("profit_today")) or 0.0
    slots = rangers.get("slots")
    last_error = ""
    if isinstance(slots, list):
        for row in slots:
            if isinstance(row, Mapping):
                err = str(row.get("last_error") or "").strip()
                if err:
                    last_error = err
                    break

    if last_error:
        return DiagnosticCheck(
            "ranger_health",
            "red",
            "Ranger Health",
            f"Ranger error detected: {last_error}.",
            float(active),
            "last_error non-empty",
        )
    if orphans > (cycles * 2):
        return DiagnosticCheck(
            "ranger_health",
            "red",
            "Ranger Health",
            f"Ranger orphan rate critical ({orphans} orphans vs {cycles} cycles).",
            float(orphans),
            "orphans > cycles * 2",
        )
    if not regime_ok:
        regime = str(rangers.get("regime") or "unknown").upper()
        return DiagnosticCheck(
            "ranger_health",
            "amber",
            "Ranger Health",
            f"Rangers paused - regime is {regime}, not RANGING.",
            float(active),
            "regime_ok == false",
        )
    if cycles > 0:
        return DiagnosticCheck(
            "ranger_health",
            "green",
            "Ranger Health",
            f"Rangers cycling: {cycles} cycles today, P&L ${profit_today:.4f}, {orphans} orphans.",
            float(cycles),
            "cycles > 0",
        )
    if active > 0:
        return DiagnosticCheck(
            "ranger_health",
            "amber",
            "Ranger Health",
            "Ranger entries are live but no fills yet. Normal during stable ranges.",
            float(active),
            "active entries, zero cycles",
        )
    return DiagnosticCheck(
        "ranger_health",
        "green",
        "Ranger Health",
        "Rangers enabled and idle.",
        0.0,
        "idle",
    )


def _rule_capital_efficiency(snapshot: Mapping[str, Any], _cfg: Mapping[str, Any]) -> DiagnosticCheck:
    util = _to_float(_first(snapshot, ("capital_util_ratio",), ("throughput_sizer", "util_ratio")))
    stuck_cap = _to_float(_first(snapshot, ("slot_vintage", "stuck_capital_pct"), ("stuck_capital_pct",)))
    if util is None:
        return DiagnosticCheck(
            "capital_efficiency",
            "amber",
            "Capital Efficiency",
            "Capital utilization unavailable. Throughput pressure is unknown.",
            0.0,
            "util_ratio unavailable",
        )

    if util < 0.50:
        severity = "green"
        detail = f"Capital utilization {_format_pct(util, 1)} - room to deploy more."
    elif util < 0.70:
        severity = "amber"
        detail = f"Capital utilization {_format_pct(util, 1)} - throughput throttling may begin."
    else:
        severity = "red"
        detail = f"Capital utilization {_format_pct(util, 1)} - entries are likely being throttled."
    if stuck_cap is not None:
        detail += f" Stuck capital {stuck_cap:.1f}%."
    return DiagnosticCheck("capital_efficiency", severity, "Capital Efficiency", detail, float(util), "green <50%, amber <70%")


def _rule_mts_trend(snapshot: Mapping[str, Any], _cfg: Mapping[str, Any]) -> DiagnosticCheck:
    mts = _to_float(_first(snapshot, ("mts",), ("manifold_score", "mts")))
    trend = str(_first(snapshot, ("mts_trend",), ("manifold_score", "trend")) or "stable").strip().lower()
    band = str(_first(snapshot, ("mts_band",), ("manifold_score", "band")) or "").strip().lower()
    if mts is None:
        return DiagnosticCheck(
            "mts_trend",
            "amber",
            "Manifold Trend",
            "Manifold score unavailable. Broad condition favorability is unknown.",
            0.0,
            "mts unavailable",
        )

    if mts < 0.40:
        severity = "red"
        detail = f"Manifold score {_format_num(mts, 3)} ({band or 'low'}) - hostile conditions for grid cycling."
    elif mts >= 0.60 and trend != "falling":
        severity = "green"
        detail = f"Manifold score {_format_num(mts, 3)} ({band or 'favorable'}) - conditions support grid cycling."
    else:
        severity = "amber"
        detail = (
            f"Manifold score {_format_num(mts, 3)} ({band or 'mixed'}), trend {trend} - "
            "entry quality may be degrading."
        )
    return DiagnosticCheck("mts_trend", severity, "Manifold Trend", detail, float(mts), "green >=0.60 and not falling")


_RULES = (
    _rule_ema_trend,
    _rule_rsi_zone,
    _rule_macd_momentum,
    _rule_regime_confidence,
    _rule_timeframe_agreement,
    _rule_boundary_risk,
    _rule_age_skew,
    _rule_exit_distance,
    _rule_headroom,
    _rule_ranger_health,
    _rule_capital_efficiency,
    _rule_mts_trend,
)


def evaluate_rules(
    snapshot: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
) -> list[DiagnosticCheck]:
    config_map = dict(DEFAULT_DIGEST_CONFIG)
    if cfg:
        config_map.update(dict(cfg))
    return [rule(snapshot, config_map) for rule in _RULES]


def sort_checks(checks: list[DiagnosticCheck]) -> list[DiagnosticCheck]:
    return sorted(
        checks,
        key=lambda c: (
            -_SEVERITY_RANK.get(_severity(c.severity), 0),
            RULE_PRIORITY.get(c.signal, 999),
            c.signal,
        ),
    )


def overall_light(checks: list[DiagnosticCheck]) -> str:
    if not checks:
        return "green"
    top = max(checks, key=lambda c: _SEVERITY_RANK.get(_severity(c.severity), 0))
    return _severity(top.severity)


def top_concern(checks: list[DiagnosticCheck]) -> str:
    ranked = sort_checks(checks)
    for check in ranked:
        sev = _severity(check.severity)
        if sev in {"amber", "red"}:
            return check.detail
    return "All diagnostic checks nominal."


def evaluate_signal_digest(
    snapshot: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
) -> SignalDigestResult:
    checks = sort_checks(evaluate_rules(snapshot=snapshot, cfg=cfg))
    return SignalDigestResult(
        light=overall_light(checks),
        top_concern=top_concern(checks),
        checks=checks,
    )


__all__ = [
    "DEFAULT_DIGEST_CONFIG",
    "DiagnosticCheck",
    "MarketInterpretation",
    "RULE_PRIORITY",
    "SignalDigestResult",
    "evaluate_rules",
    "sort_checks",
    "overall_light",
    "top_concern",
    "evaluate_signal_digest",
]
