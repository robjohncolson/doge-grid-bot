"""
throughput_sizer.py - Fill-time throughput position sizer.

Optimizes order sizing using regime/side-specific fill-time throughput
instead of win/loss edge. Designed as a drop-in replacement for KellySizer.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_REGIME_TEXT_TO_LABEL = {
    "BEARISH": "bearish",
    "RANGING": "ranging",
    "BULLISH": "bullish",
}
_REGIME_ID_TO_LABEL = {
    0: "bearish",
    1: "ranging",
    2: "bullish",
}
_BUCKET_ORDER = (
    "aggregate",
    "bearish_A",
    "bearish_B",
    "ranging_A",
    "ranging_B",
    "bullish_A",
    "bullish_B",
)


def _now_ts() -> float:
    return float(time.time())


def _clamp(value: float, low: float, high: float) -> float:
    lo = min(low, high)
    hi = max(low, high)
    return max(lo, min(float(value), hi))


def _safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value):
        return float(default)
    return value


def _safe_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _normalize_regime_label(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return _REGIME_ID_TO_LABEL.get(int(raw))
    if isinstance(raw, int):
        return _REGIME_ID_TO_LABEL.get(raw)
    if isinstance(raw, float):
        return _REGIME_ID_TO_LABEL.get(int(raw))
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.lstrip("-").isdigit():
            return _REGIME_ID_TO_LABEL.get(int(text))
        canonical = _REGIME_TEXT_TO_LABEL.get(text.upper())
        if canonical:
            return canonical
        lower = text.lower()
        if lower in {"bearish", "ranging", "bullish"}:
            return lower
    return None


def _normalize_trade_id(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text in {"A", "B"}:
        return text
    return None


def _bucket_key_for(regime_label: str | None, trade_id: str | None) -> str | None:
    if regime_label in {"bearish", "ranging", "bullish"} and trade_id in {"A", "B"}:
        return f"{regime_label}_{trade_id}"
    return None


def _weighted_percentile(observations: list[tuple[float, float]], q: float) -> float:
    if not observations:
        return 0.0
    pct = _clamp(float(q), 0.0, 1.0)
    clean: list[tuple[float, float]] = []
    total_weight = 0.0
    for value, weight in observations:
        v = _safe_float(value, 0.0)
        w = _safe_float(weight, 0.0)
        if v <= 0.0 or w <= 0.0:
            continue
        clean.append((v, w))
        total_weight += w
    if not clean or total_weight <= 0.0:
        return 0.0

    clean.sort(key=lambda row: row[0])
    if pct <= 0.0:
        return float(clean[0][0])
    if pct >= 1.0:
        return float(clean[-1][0])

    threshold = total_weight * pct
    running = 0.0
    for value, weight in clean:
        running += weight
        if running >= threshold:
            return float(value)
    return float(clean[-1][0])


@dataclass
class ThroughputConfig:
    enabled: bool = False
    lookback_cycles: int = 500
    min_samples: int = 20
    min_samples_per_bucket: int = 10
    full_confidence_samples: int = 50
    floor_mult: float = 0.5
    ceiling_mult: float = 2.0
    censored_weight: float = 0.5
    age_pressure_trigger: float = 1.5
    age_pressure_sensitivity: float = 0.5
    age_pressure_floor: float = 0.3
    util_threshold: float = 0.7
    util_sensitivity: float = 0.8
    util_floor: float = 0.4
    recency_halflife: int = 100
    log_updates: bool = True


@dataclass
class BucketStats:
    median_fill_sec: float
    p75_fill_sec: float
    p95_fill_sec: float
    mean_profit_per_sec: float
    n_completed: int
    n_censored: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "median_fill_sec": round(float(self.median_fill_sec), 6),
            "p75_fill_sec": round(float(self.p75_fill_sec), 6),
            "p95_fill_sec": round(float(self.p95_fill_sec), 6),
            "mean_profit_per_sec": round(float(self.mean_profit_per_sec), 12),
            "n_completed": int(self.n_completed),
            "n_censored": int(self.n_censored),
        }


@dataclass
class ThroughputResult:
    throughput_mult: float
    age_pressure: float
    util_penalty: float
    final_mult: float
    bucket_key: str
    reason: str
    sufficient_data: bool

    def to_dict(self) -> dict[str, float | str | bool]:
        return {
            "throughput_mult": round(float(self.throughput_mult), 6),
            "age_pressure": round(float(self.age_pressure), 6),
            "util_penalty": round(float(self.util_penalty), 6),
            "final_mult": round(float(self.final_mult), 6),
            "bucket_key": str(self.bucket_key),
            "reason": str(self.reason),
            "sufficient_data": bool(self.sufficient_data),
        }


class ThroughputSizer:
    def __init__(self, cfg: ThroughputConfig | None = None):
        self.cfg = cfg or ThroughputConfig()
        self._active_regime = "ranging"
        self._last_update_n = 0
        self._age_pressure = 1.0
        self._util_penalty = 1.0
        self._oldest_open_exit_age_sec = 0.0
        self._util_ratio = 0.0
        self._bucket_stats: dict[str, BucketStats] = {}
        self._bucket_multipliers: dict[str, float] = {}
        self._bucket_reasons: dict[str, str] = {}
        self._bucket_sufficient: dict[str, bool] = {}
        self._bucket_n_completed: dict[str, int] = {name: 0 for name in _BUCKET_ORDER}
        self._bucket_n_censored: dict[str, int] = {name: 0 for name in _BUCKET_ORDER}

    def update(
        self,
        completed_cycles: list[dict],
        open_exits: list[dict],
        regime_label: str | None = None,
        free_doge: float = 0.0,
    ) -> dict[str, BucketStats]:
        if regime_label:
            normalized = _normalize_regime_label(regime_label)
            if normalized:
                self._active_regime = normalized

        self._reset_update_state()
        if not self.cfg.enabled:
            return {}

        trimmed = self._trim_cycles(completed_cycles or [])
        self._last_update_n = len(trimmed)

        completed_by_bucket = self._partition_completed(trimmed)
        open_by_bucket, locked_doge = self._partition_open_exits(open_exits or [])

        aggregate_ready = self._compute_bucket_stats(
            bucket_name="aggregate",
            completed_rows=completed_by_bucket.get("aggregate", []),
            censored_rows=open_by_bucket.get("aggregate", []),
            min_needed=max(1, int(self.cfg.min_samples)),
        )
        for bucket_name in _BUCKET_ORDER:
            if bucket_name == "aggregate":
                continue
            self._compute_bucket_stats(
                bucket_name=bucket_name,
                completed_rows=completed_by_bucket.get(bucket_name, []),
                censored_rows=open_by_bucket.get(bucket_name, []),
                min_needed=max(1, int(self.cfg.min_samples_per_bucket)),
            )

        self._compute_multipliers()
        self._compute_age_pressure(open_by_bucket.get("aggregate", []), aggregate_ready)
        self._compute_util_penalty(locked_doge=locked_doge, free_doge=free_doge)

        if self.cfg.log_updates:
            self._log_summary()
        return dict(self._bucket_stats)

    def size_for_slot(
        self,
        base_order_usd: float,
        regime_label: str | None = None,
        trade_id: str | None = None,
    ) -> tuple[float, str]:
        base = max(0.0, float(base_order_usd))
        result = self._result_for(regime_label=regime_label, trade_id=trade_id)

        if result.reason == "tp_disabled":
            return base, "tp_disabled"
        if not result.sufficient_data:
            return base, f"tp_{result.reason}"

        adjusted = base * result.final_mult
        reason = (
            f"tp_{result.bucket_key}"
            f"(t={result.throughput_mult:.3f},age={result.age_pressure:.3f},"
            f"util={result.util_penalty:.3f},m={result.final_mult:.3f})"
        )
        if result.reason != "ok":
            reason = f"{reason}:{result.reason}"
        return adjusted, reason

    def status_payload(self) -> dict:
        payload: dict[str, Any] = {
            "enabled": bool(self.cfg.enabled),
            "active_regime": str(self._active_regime),
            "last_update_n": int(self._last_update_n),
            "age_pressure": round(float(self._age_pressure), 6),
            "util_penalty": round(float(self._util_penalty), 6),
            "oldest_open_exit_age_sec": round(float(self._oldest_open_exit_age_sec), 6),
            "util_ratio": round(float(self._util_ratio), 6),
        }
        for bucket_name in _BUCKET_ORDER:
            row: dict[str, Any] = {
                "multiplier": round(float(self._bucket_multipliers.get(bucket_name, 1.0)), 6),
                "sufficient_data": bool(self._bucket_sufficient.get(bucket_name, False)),
                "reason": str(self._bucket_reasons.get(bucket_name, "insufficient_data")),
                "n_completed": int(self._bucket_n_completed.get(bucket_name, 0)),
                "n_censored": int(self._bucket_n_censored.get(bucket_name, 0)),
            }
            stats = self._bucket_stats.get(bucket_name)
            if stats is not None:
                row.update(stats.to_dict())
            else:
                row.update(
                    {
                        "median_fill_sec": 0.0,
                        "p75_fill_sec": 0.0,
                        "p95_fill_sec": 0.0,
                        "mean_profit_per_sec": 0.0,
                    }
                )
            payload[bucket_name] = row
        return payload

    def snapshot_state(self) -> dict:
        return {
            "active_regime": str(self._active_regime),
            "last_update_n": int(self._last_update_n),
            "age_pressure": float(self._age_pressure),
            "util_penalty": float(self._util_penalty),
            "oldest_open_exit_age_sec": float(self._oldest_open_exit_age_sec),
            "util_ratio": float(self._util_ratio),
            "bucket_stats": {k: v.to_dict() for k, v in self._bucket_stats.items()},
            "bucket_multipliers": {k: float(v) for k, v in self._bucket_multipliers.items()},
            "bucket_reasons": {k: str(v) for k, v in self._bucket_reasons.items()},
            "bucket_sufficient": {k: bool(v) for k, v in self._bucket_sufficient.items()},
            "bucket_n_completed": {k: int(v) for k, v in self._bucket_n_completed.items()},
            "bucket_n_censored": {k: int(v) for k, v in self._bucket_n_censored.items()},
        }

    def restore_state(self, data: dict) -> None:
        if not isinstance(data, dict) or not data:
            return
        restored_regime = _normalize_regime_label(data.get("active_regime"))
        if restored_regime:
            self._active_regime = restored_regime
        self._last_update_n = max(0, _safe_int(data.get("last_update_n"), 0))
        self._age_pressure = _clamp(
            _safe_float(data.get("age_pressure"), 1.0),
            float(self.cfg.age_pressure_floor),
            1.0,
        )
        self._util_penalty = _clamp(
            _safe_float(data.get("util_penalty"), 1.0),
            float(self.cfg.util_floor),
            1.0,
        )
        self._oldest_open_exit_age_sec = max(0.0, _safe_float(data.get("oldest_open_exit_age_sec"), 0.0))
        self._util_ratio = _clamp(_safe_float(data.get("util_ratio"), 0.0), 0.0, 1.0)

        self._bucket_stats = {}
        raw_stats = data.get("bucket_stats", {})
        if isinstance(raw_stats, dict):
            for key, row in raw_stats.items():
                if key not in _BUCKET_ORDER or not isinstance(row, dict):
                    continue
                self._bucket_stats[key] = BucketStats(
                    median_fill_sec=max(0.0, _safe_float(row.get("median_fill_sec"), 0.0)),
                    p75_fill_sec=max(0.0, _safe_float(row.get("p75_fill_sec"), 0.0)),
                    p95_fill_sec=max(0.0, _safe_float(row.get("p95_fill_sec"), 0.0)),
                    mean_profit_per_sec=_safe_float(row.get("mean_profit_per_sec"), 0.0),
                    n_completed=max(0, _safe_int(row.get("n_completed"), 0)),
                    n_censored=max(0, _safe_int(row.get("n_censored"), 0)),
                )

        self._bucket_multipliers = {name: 1.0 for name in _BUCKET_ORDER}
        raw_mult = data.get("bucket_multipliers", {})
        if isinstance(raw_mult, dict):
            for key, value in raw_mult.items():
                if key not in _BUCKET_ORDER:
                    continue
                self._bucket_multipliers[key] = _clamp(
                    _safe_float(value, 1.0),
                    float(self.cfg.floor_mult),
                    float(self.cfg.ceiling_mult),
                )

        self._bucket_reasons = {name: "insufficient_data" for name in _BUCKET_ORDER}
        raw_reasons = data.get("bucket_reasons", {})
        if isinstance(raw_reasons, dict):
            for key, value in raw_reasons.items():
                if key in _BUCKET_ORDER:
                    self._bucket_reasons[key] = str(value)

        self._bucket_sufficient = {name: False for name in _BUCKET_ORDER}
        raw_sufficient = data.get("bucket_sufficient", {})
        if isinstance(raw_sufficient, dict):
            for key, value in raw_sufficient.items():
                if key in _BUCKET_ORDER:
                    self._bucket_sufficient[key] = bool(value)

        self._bucket_n_completed = {name: 0 for name in _BUCKET_ORDER}
        raw_completed = data.get("bucket_n_completed", {})
        if isinstance(raw_completed, dict):
            for key, value in raw_completed.items():
                if key in _BUCKET_ORDER:
                    self._bucket_n_completed[key] = max(0, _safe_int(value, 0))

        self._bucket_n_censored = {name: 0 for name in _BUCKET_ORDER}
        raw_censored = data.get("bucket_n_censored", {})
        if isinstance(raw_censored, dict):
            for key, value in raw_censored.items():
                if key in _BUCKET_ORDER:
                    self._bucket_n_censored[key] = max(0, _safe_int(value, 0))

    def _reset_update_state(self) -> None:
        self._bucket_stats = {}
        self._bucket_multipliers = {name: 1.0 for name in _BUCKET_ORDER}
        self._bucket_reasons = {name: "insufficient_data" for name in _BUCKET_ORDER}
        self._bucket_sufficient = {name: False for name in _BUCKET_ORDER}
        self._bucket_n_completed = {name: 0 for name in _BUCKET_ORDER}
        self._bucket_n_censored = {name: 0 for name in _BUCKET_ORDER}
        self._age_pressure = 1.0
        self._util_penalty = 1.0
        self._oldest_open_exit_age_sec = 0.0
        self._util_ratio = 0.0

    def _trim_cycles(self, completed_cycles: list[dict]) -> list[dict]:
        if not completed_cycles:
            return []
        lookback = max(1, int(self.cfg.lookback_cycles))
        if len(completed_cycles) <= lookback:
            return list(completed_cycles)
        sorted_cycles = sorted(
            completed_cycles,
            key=lambda row: _safe_float(
                (row or {}).get("exit_time"),
                _safe_float((row or {}).get("entry_time"), 0.0),
            ),
            reverse=True,
        )
        return sorted_cycles[:lookback]

    def _partition_completed(self, completed_cycles: list[dict]) -> dict[str, list[dict[str, float]]]:
        buckets: dict[str, list[dict[str, float]]] = {name: [] for name in _BUCKET_ORDER}
        for row in completed_cycles:
            if not isinstance(row, dict):
                continue
            entry_time = _safe_float(row.get("entry_time"), 0.0)
            exit_time = _safe_float(row.get("exit_time"), 0.0)
            duration = exit_time - entry_time
            if duration <= 0.0:
                continue
            rec = {
                "duration": float(duration),
                "net_profit": _safe_float(row.get("net_profit"), 0.0),
                "exit_time": float(exit_time),
            }
            buckets["aggregate"].append(rec)

            regime_label = _normalize_regime_label(row.get("regime_at_entry"))
            trade_id = _normalize_trade_id(row.get("trade_id"))
            bucket_key = _bucket_key_for(regime_label, trade_id)
            if bucket_key is not None:
                buckets[bucket_key].append(rec)
        return buckets

    def _partition_open_exits(self, open_exits: list[dict]) -> tuple[dict[str, list[dict[str, float]]], float]:
        buckets: dict[str, list[dict[str, float]]] = {name: [] for name in _BUCKET_ORDER}
        locked_doge = 0.0
        now_ts = _now_ts()
        for row in open_exits:
            if not isinstance(row, dict):
                continue
            age_sec = _safe_float(row.get("age_sec"), -1.0)
            if age_sec < 0.0:
                age_sec = _safe_float(row.get("age"), -1.0)
            if age_sec < 0.0:
                entry_filled_at = _safe_float(row.get("entry_filled_at"), 0.0)
                if entry_filled_at > 0.0:
                    age_sec = max(0.0, now_ts - entry_filled_at)
            if age_sec <= 0.0:
                continue

            volume = max(0.0, _safe_float(row.get("volume"), 0.0))
            locked_doge += volume
            rec = {"age_sec": float(age_sec), "volume": float(volume)}
            buckets["aggregate"].append(rec)

            regime_label = _normalize_regime_label(row.get("regime_at_entry"))
            trade_id = _normalize_trade_id(row.get("trade_id"))
            bucket_key = _bucket_key_for(regime_label, trade_id)
            if bucket_key is not None:
                buckets[bucket_key].append(rec)
        return buckets, locked_doge

    def _compute_bucket_stats(
        self,
        *,
        bucket_name: str,
        completed_rows: list[dict[str, float]],
        censored_rows: list[dict[str, float]],
        min_needed: int,
    ) -> bool:
        n_completed = len(completed_rows)
        self._bucket_n_completed[bucket_name] = n_completed
        if n_completed < min_needed:
            self._bucket_reasons[bucket_name] = f"insufficient_samples ({n_completed}/{min_needed})"
            self._bucket_sufficient[bucket_name] = False
            return False

        ranked = sorted(completed_rows, key=lambda row: row["exit_time"], reverse=True)
        observations: list[tuple[float, float]] = []
        weighted_profit = 0.0
        weighted_duration = 0.0

        halflife = int(self.cfg.recency_halflife)
        decay = math.log(2.0) / halflife if halflife > 0 else 0.0

        for rank, row in enumerate(ranked):
            duration = max(0.0, _safe_float(row.get("duration"), 0.0))
            if duration <= 0.0:
                continue
            weight = math.exp(-decay * rank) if decay > 0.0 else 1.0
            observations.append((duration, weight))
            weighted_profit += _safe_float(row.get("net_profit"), 0.0) * weight
            weighted_duration += duration * weight

        if not observations or weighted_duration <= 0.0:
            self._bucket_reasons[bucket_name] = "insufficient_data"
            self._bucket_sufficient[bucket_name] = False
            return False

        base_median = _weighted_percentile(observations, 0.5)
        cutoff = base_median * 0.5
        censored_obs: list[tuple[float, float]] = []
        for row in censored_rows:
            age = max(0.0, _safe_float(row.get("age_sec"), 0.0))
            if age > cutoff:
                censored_obs.append((age, max(0.0, _safe_float(self.cfg.censored_weight, 0.5))))

        merged = observations + censored_obs
        median_fill = _weighted_percentile(merged, 0.5)
        p75_fill = _weighted_percentile(merged, 0.75)
        p95_fill = _weighted_percentile(merged, 0.95)

        stats = BucketStats(
            median_fill_sec=max(0.0, median_fill),
            p75_fill_sec=max(0.0, p75_fill),
            p95_fill_sec=max(0.0, p95_fill),
            mean_profit_per_sec=(weighted_profit / weighted_duration) if weighted_duration > 0.0 else 0.0,
            n_completed=n_completed,
            n_censored=len(censored_obs),
        )
        self._bucket_stats[bucket_name] = stats
        self._bucket_n_censored[bucket_name] = int(stats.n_censored)
        self._bucket_sufficient[bucket_name] = True
        self._bucket_reasons[bucket_name] = "ok"
        return True

    def _compute_multipliers(self) -> None:
        aggregate = self._bucket_stats.get("aggregate")
        if aggregate is None or aggregate.median_fill_sec <= 0.0:
            return

        self._bucket_multipliers["aggregate"] = 1.0
        self._bucket_sufficient["aggregate"] = True
        self._bucket_reasons["aggregate"] = "ok"

        for bucket_name in _BUCKET_ORDER:
            if bucket_name == "aggregate":
                continue
            if not self._bucket_sufficient.get(bucket_name, False):
                self._bucket_multipliers[bucket_name] = 1.0
                continue
            bucket = self._bucket_stats.get(bucket_name)
            if bucket is None or bucket.median_fill_sec <= 0.0:
                self._bucket_multipliers[bucket_name] = 1.0
                self._bucket_sufficient[bucket_name] = False
                self._bucket_reasons[bucket_name] = "insufficient_data"
                continue

            raw_mult = aggregate.median_fill_sec / bucket.median_fill_sec
            bounded = _clamp(raw_mult, float(self.cfg.floor_mult), float(self.cfg.ceiling_mult))

            full_conf = max(1, int(self.cfg.full_confidence_samples))
            confidence = min(1.0, float(bucket.n_completed) / float(full_conf))
            blended = 1.0 + confidence * (bounded - 1.0)
            final = _clamp(blended, float(self.cfg.floor_mult), float(self.cfg.ceiling_mult))
            self._bucket_multipliers[bucket_name] = final

    def _compute_age_pressure(self, open_aggregate: list[dict[str, float]], aggregate_ready: bool) -> None:
        oldest = 0.0
        for row in open_aggregate:
            oldest = max(oldest, max(0.0, _safe_float(row.get("age_sec"), 0.0)))
        self._oldest_open_exit_age_sec = oldest
        self._age_pressure = 1.0

        if not aggregate_ready:
            return
        aggregate = self._bucket_stats.get("aggregate")
        if aggregate is None or aggregate.p75_fill_sec <= 0.0:
            return

        trigger = max(0.0, float(self.cfg.age_pressure_trigger))
        threshold = aggregate.p75_fill_sec * trigger
        if threshold <= 0.0 or oldest <= threshold:
            return

        excess_ratio = (oldest - threshold) / threshold
        pressured = 1.0 - excess_ratio * float(self.cfg.age_pressure_sensitivity)
        self._age_pressure = _clamp(pressured, float(self.cfg.age_pressure_floor), 1.0)

    def _compute_util_penalty(self, *, locked_doge: float, free_doge: float) -> None:
        locked = max(0.0, float(locked_doge))
        free = max(0.0, float(free_doge))
        total = locked + free
        self._util_ratio = (locked / total) if total > 0.0 else 0.0
        self._util_penalty = 1.0

        threshold = _clamp(float(self.cfg.util_threshold), 0.0, 1.0)
        if self._util_ratio <= threshold:
            return
        if threshold >= 1.0:
            return

        excess = (self._util_ratio - threshold) / (1.0 - threshold)
        penalized = 1.0 - excess * float(self.cfg.util_sensitivity)
        self._util_penalty = _clamp(penalized, float(self.cfg.util_floor), 1.0)

    def _result_for(self, regime_label: str | None, trade_id: str | None) -> ThroughputResult:
        if not self.cfg.enabled:
            return ThroughputResult(
                throughput_mult=1.0,
                age_pressure=1.0,
                util_penalty=1.0,
                final_mult=1.0,
                bucket_key="aggregate",
                reason="tp_disabled",
                sufficient_data=False,
            )

        aggregate = self._bucket_stats.get("aggregate")
        if aggregate is None or not self._bucket_sufficient.get("aggregate", False):
            return ThroughputResult(
                throughput_mult=1.0,
                age_pressure=1.0,
                util_penalty=1.0,
                final_mult=1.0,
                bucket_key="aggregate",
                reason="insufficient_data",
                sufficient_data=False,
            )

        normalized_regime = _normalize_regime_label(regime_label) if regime_label else self._active_regime
        normalized_trade = _normalize_trade_id(trade_id)
        candidate = _bucket_key_for(normalized_regime, normalized_trade)

        bucket_key = "aggregate"
        throughput_mult = 1.0
        reason = "ok"
        if candidate:
            if self._bucket_sufficient.get(candidate, False):
                bucket_key = candidate
                throughput_mult = _clamp(
                    _safe_float(self._bucket_multipliers.get(candidate), 1.0),
                    float(self.cfg.floor_mult),
                    float(self.cfg.ceiling_mult),
                )
            else:
                reason = "no_bucket"

        final_mult = _clamp(
            throughput_mult * float(self._age_pressure) * float(self._util_penalty),
            float(self.cfg.floor_mult),
            float(self.cfg.ceiling_mult),
        )
        return ThroughputResult(
            throughput_mult=throughput_mult,
            age_pressure=float(self._age_pressure),
            util_penalty=float(self._util_penalty),
            final_mult=final_mult,
            bucket_key=bucket_key,
            reason=reason,
            sufficient_data=True,
        )

    def _log_summary(self) -> None:
        aggregate = self._bucket_stats.get("aggregate")
        if aggregate is None:
            log.info("throughput: insufficient aggregate samples (n=%d)", self._last_update_n)
            return
        log.info(
            "throughput: n=%d median=%.1fs p75=%.1fs age=%.3f util=%.3f util_ratio=%.3f",
            self._last_update_n,
            float(aggregate.median_fill_sec),
            float(aggregate.p75_fill_sec),
            float(self._age_pressure),
            float(self._util_penalty),
            float(self._util_ratio),
        )

