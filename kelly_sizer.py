"""
kelly_sizer.py — Regime-conditional Kelly criterion position sizer

Integrates with the DOGE State-Machine Bot v1 architecture.
Reads completed cycle data to compute optimal fractional Kelly sizing
per HMM regime, feeding into _slot_order_size_usd().

Design:
  - Pure computation functions (no network side effects)
  - Runtime class (KellySizer) for stateful integration with bot.py
  - Regime-conditional: separate Kelly fractions for bullish/ranging/bearish
  - Fractional Kelly (default 0.25) to reduce variance and ruin risk
  - Minimum sample gating to avoid sizing on noise

Usage in bot.py:
  sizer = KellySizer(cfg)
  sizer.update(completed_cycles, regime_state)
  order_usd = sizer.size_for_slot(slot, base_order_usd, regime_label)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

_REGIME_TEXT_TO_ID = {
    "BEARISH": 0,
    "RANGING": 1,
    "BULLISH": 2,
}


def _normalize_regime_id(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw if raw in (0, 1, 2) else None
    if isinstance(raw, float):
        i = int(raw)
        return i if i in (0, 1, 2) else None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.isdigit():
            i = int(s)
            return i if i in (0, 1, 2) else None
        return _REGIME_TEXT_TO_ID.get(s.upper())
    return None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class KellyConfig:
    """All tunables for Kelly sizing. Set from env/config.py."""

    # Core Kelly
    kelly_fraction: float = 0.25          # quarter-Kelly default (conservative)
    min_samples_total: int = 30           # need this many cycles before Kelly activates
    min_samples_per_regime: int = 15      # per-regime minimum; below this, fall back to aggregate
    lookback_cycles: int = 500            # rolling window of most recent cycles to consider

    # Sizing bounds
    kelly_floor_mult: float = 0.5         # minimum multiplier on base_order_usd (never size below 50%)
    kelly_ceiling_mult: float = 2.0       # maximum multiplier (caps aggressive compounding)
    negative_edge_mult: float = 0.5       # multiplier when Kelly says no edge (shrink, don't stop)

    # Regime mapping — which regime labels map to which bucket
    # Your HMM emits regime_id 0/1/2; these map to labels
    regime_labels: dict = field(default_factory=lambda: {
        0: "bearish",
        1: "ranging",
        2: "bullish",
    })

    # Decay — optional exponential recency weighting
    use_recency_weighting: bool = True
    recency_halflife_cycles: int = 100    # recent cycles count more

    # Logging
    log_kelly_updates: bool = True


# ---------------------------------------------------------------------------
# Pure computation functions
# ---------------------------------------------------------------------------

def compute_kelly_fraction(
    wins: list[float],
    losses: list[float],
    fraction: float = 0.25,
    weights_w: Optional[list[float]] = None,
    weights_l: Optional[list[float]] = None,
) -> KellyResult:
    """
    Compute fractional Kelly from win/loss arrays.

    Args:
        wins:    list of positive profit amounts (USD) from winning cycles
        losses:  list of positive loss amounts (USD, already abs-valued) from losing cycles
        fraction: Kelly fraction to apply (0.25 = quarter-Kelly)
        weights_w: optional per-win recency weights
        weights_l: optional per-loss recency weights

    Returns:
        KellyResult with full diagnostics
    """
    n = len(wins) + len(losses)
    if n == 0:
        return KellyResult(
            f_star=0.0, f_fractional=0.0, multiplier=1.0,
            win_rate=0.0, avg_win=0.0, avg_loss=0.0, payoff_ratio=0.0,
            n_total=0, n_wins=0, n_losses=0,
            edge=0.0, sufficient_data=False, reason="no_data",
        )

    # Weighted or simple means
    if weights_w and len(weights_w) == len(wins):
        w_sum = sum(weights_w)
        avg_win = sum(w * v for w, v in zip(weights_w, wins)) / w_sum if w_sum > 0 else 0.0
        # weighted win count as fraction
        p = sum(weights_w) / (sum(weights_w) + (sum(weights_l) if weights_l else len(losses)))
    else:
        avg_win = sum(wins) / len(wins) if wins else 0.0
        p = len(wins) / n

    if weights_l and len(weights_l) == len(losses):
        l_sum = sum(weights_l)
        avg_loss = sum(w * v for w, v in zip(weights_l, losses)) / l_sum if l_sum > 0 else 0.0
    else:
        avg_loss = sum(losses) / len(losses) if losses else 0.0

    q = 1.0 - p

    # Payoff ratio b = avg_win / avg_loss
    if avg_loss == 0:
        # All wins, no losses — edge is infinite, but cap Kelly at 1.0
        return KellyResult(
            f_star=1.0, f_fractional=fraction, multiplier=1.0 + fraction,
            win_rate=p, avg_win=avg_win, avg_loss=0.0, payoff_ratio=float("inf"),
            n_total=n, n_wins=len(wins), n_losses=len(losses),
            edge=avg_win, sufficient_data=True, reason="all_wins",
        )

    b = avg_win / avg_loss

    # Kelly: f* = (bp - q) / b  =  p - q/b
    f_star = (b * p - q) / b
    edge = b * p - q  # expected value per unit risked

    if f_star <= 0:
        return KellyResult(
            f_star=f_star, f_fractional=0.0, multiplier=1.0,
            win_rate=p, avg_win=avg_win, avg_loss=avg_loss, payoff_ratio=b,
            n_total=n, n_wins=len(wins), n_losses=len(losses),
            edge=edge, sufficient_data=True, reason="no_edge",
        )

    f_frac = f_star * fraction
    multiplier = 1.0 + f_frac  # sizing multiplier on base order

    return KellyResult(
        f_star=f_star,
        f_fractional=f_frac,
        multiplier=multiplier,
        win_rate=p,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=b,
        n_total=n,
        n_wins=len(wins),
        n_losses=len(losses),
        edge=edge,
        sufficient_data=True,
        reason="ok",
    )


@dataclass
class KellyResult:
    """Full diagnostics from a Kelly computation."""
    f_star: float           # raw Kelly fraction
    f_fractional: float     # after applying kelly_fraction multiplier
    multiplier: float       # sizing multiplier (1.0 + f_fractional)
    win_rate: float         # p
    avg_win: float          # W (USD)
    avg_loss: float         # L (USD)
    payoff_ratio: float     # b = W/L
    n_total: int
    n_wins: int
    n_losses: int
    edge: float             # expected value per unit risked (bp - q)
    sufficient_data: bool
    reason: str             # "ok", "no_data", "no_edge", "all_wins", "insufficient_samples"

    @staticmethod
    def _safe_float(value: float, digits: int, *, inf_fallback: float | None = None) -> float | None:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(num):
            if inf_fallback is None:
                return None
            if math.isinf(num):
                num = inf_fallback if num > 0 else -inf_fallback
            else:
                return None
        return round(num, digits)

    def to_dict(self) -> dict:
        return {
            "f_star": self._safe_float(self.f_star, 6) or 0.0,
            "f_fractional": self._safe_float(self.f_fractional, 6) or 0.0,
            "multiplier": self._safe_float(self.multiplier, 4) or 1.0,
            "win_rate": self._safe_float(self.win_rate, 4) or 0.0,
            "avg_win": self._safe_float(self.avg_win, 6) or 0.0,
            "avg_loss": self._safe_float(self.avg_loss, 6) or 0.0,
            "payoff_ratio": self._safe_float(self.payoff_ratio, 4, inf_fallback=1.7976931348623157e308) or 0.0,
            "n_total": self.n_total,
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
            "edge": self._safe_float(self.edge, 6) or 0.0,
            "sufficient_data": self.sufficient_data,
            "reason": self.reason,
        }


def partition_cycles_by_regime(
    cycles: list[dict],
    regime_labels: dict[int, str],
) -> dict[str, list[dict]]:
    """
    Split completed cycles into regime buckets.

    Each cycle dict should have:
      - net_profit: float (positive = win, negative = loss)
      - regime_at_entry: int or str (regime_id when cycle's entry was placed)
      - exit_time: float (epoch, for recency weighting)

    Cycles without regime_at_entry go into "unknown" bucket.
    """
    buckets: dict[str, list[dict]] = {label: [] for label in regime_labels.values()}
    buckets["unknown"] = []
    buckets["aggregate"] = []

    for c in cycles:
        buckets["aggregate"].append(c)
        regime_id = _normalize_regime_id(c.get("regime_at_entry"))
        if regime_id is not None and regime_id in regime_labels:
            buckets[regime_labels[regime_id]].append(c)
        else:
            buckets["unknown"].append(c)

    return buckets


def _split_wins_losses(cycles: list[dict]) -> tuple[list[float], list[float]]:
    """Split cycle list into win amounts and loss amounts (abs-valued)."""
    wins = [c["net_profit"] for c in cycles if c["net_profit"] > 0]
    losses = [abs(c["net_profit"]) for c in cycles if c["net_profit"] <= 0]
    return wins, losses


def _recency_weights(cycles: list[dict], halflife: int) -> tuple[list[float], list[float]]:
    """
    Compute exponential decay weights based on cycle index (most recent = highest weight).
    Returns separate weight lists aligned with wins and losses.
    """
    if not cycles:
        return [], []

    # Sort by exit_time descending (most recent first)
    indexed = sorted(enumerate(cycles), key=lambda x: x[1].get("exit_time", 0), reverse=True)

    decay = math.log(2) / halflife
    weights = {}
    for rank, (orig_idx, _) in enumerate(indexed):
        weights[orig_idx] = math.exp(-decay * rank)

    w_wins, w_losses = [], []
    for i, c in enumerate(cycles):
        if c["net_profit"] > 0:
            w_wins.append(weights[i])
        else:
            w_losses.append(weights[i])

    return w_wins, w_losses


# ---------------------------------------------------------------------------
# Runtime integration class
# ---------------------------------------------------------------------------

class KellySizer:
    """
    Stateful Kelly sizer for bot.py integration.

    Lifecycle:
      1. Construct once at bot startup with KellyConfig
      2. Call update() periodically (e.g. every regime eval interval)
         with the latest completed_cycles and current regime state
      3. Call size_for_slot() in _slot_order_size_usd() to get the
         Kelly-adjusted order size

    State is serializable for snapshot persistence.
    """

    def __init__(self, cfg: KellyConfig | None = None):
        self.cfg = cfg or KellyConfig()

        # Cached results per regime + aggregate
        self._results: dict[str, KellyResult] = {}
        self._last_update_n: int = 0
        self._active_regime: str = "ranging"  # current regime label

    # ------------------------------------------------------------------
    # Core update
    # ------------------------------------------------------------------

    def update(
        self,
        completed_cycles: list[dict],
        regime_label: str | None = None,
    ) -> dict[str, KellyResult]:
        """
        Recompute Kelly fractions from cycle history.

        Args:
            completed_cycles: list of cycle dicts with at minimum:
                - net_profit (float)
                - regime_at_entry (int, optional)
                - exit_time (float epoch, optional)
            regime_label: current regime label (updates _active_regime)

        Returns:
            dict of regime_label -> KellyResult
        """
        if regime_label:
            self._active_regime = regime_label

        cfg = self.cfg

        # Trim to lookback window (most recent N cycles)
        if len(completed_cycles) > cfg.lookback_cycles:
            # Sort by exit_time desc, take most recent
            sorted_cycles = sorted(
                completed_cycles,
                key=lambda c: c.get("exit_time", 0),
                reverse=True,
            )[:cfg.lookback_cycles]
        else:
            sorted_cycles = completed_cycles

        # Minimum total sample gate
        if len(sorted_cycles) < cfg.min_samples_total:
            insufficient = KellyResult(
                f_star=0.0, f_fractional=0.0, multiplier=1.0,
                win_rate=0.0, avg_win=0.0, avg_loss=0.0, payoff_ratio=0.0,
                n_total=len(sorted_cycles), n_wins=0, n_losses=0,
                edge=0.0, sufficient_data=False,
                reason=f"insufficient_samples ({len(sorted_cycles)}/{cfg.min_samples_total})",
            )
            self._results = {"aggregate": insufficient}
            self._last_update_n = len(sorted_cycles)
            return self._results

        # Partition by regime
        buckets = partition_cycles_by_regime(sorted_cycles, cfg.regime_labels)

        results = {}
        for label, cycles in buckets.items():
            if label == "unknown":
                continue  # don't compute Kelly for untagged cycles separately

            min_needed = (
                cfg.min_samples_total if label == "aggregate"
                else cfg.min_samples_per_regime
            )

            if len(cycles) < min_needed:
                results[label] = KellyResult(
                    f_star=0.0, f_fractional=0.0, multiplier=1.0,
                    win_rate=0.0, avg_win=0.0, avg_loss=0.0, payoff_ratio=0.0,
                    n_total=len(cycles), n_wins=0, n_losses=0,
                    edge=0.0, sufficient_data=False,
                    reason=f"insufficient_samples ({len(cycles)}/{min_needed})",
                )
                continue

            wins, losses = _split_wins_losses(cycles)

            if cfg.use_recency_weighting:
                w_wins, w_losses = _recency_weights(cycles, cfg.recency_halflife_cycles)
            else:
                w_wins, w_losses = None, None

            results[label] = compute_kelly_fraction(
                wins, losses,
                fraction=cfg.kelly_fraction,
                weights_w=w_wins,
                weights_l=w_losses,
            )

        self._results = results
        self._last_update_n = len(sorted_cycles)

        if cfg.log_kelly_updates:
            self._log_summary()

        return results

    # ------------------------------------------------------------------
    # Sizing interface (called from _slot_order_size_usd)
    # ------------------------------------------------------------------

    def size_for_slot(
        self,
        base_order_usd: float,
        regime_label: str | None = None,
    ) -> tuple[float, str]:
        """
        Apply Kelly multiplier to base order size.

        Args:
            base_order_usd: the base order size from existing logic
                            (ORDER_SIZE_USD + slot.total_profit + layers)
            regime_label: override regime; defaults to _active_regime

        Returns:
            (adjusted_usd, reason_str)
        """
        cfg = self.cfg
        label = regime_label or self._active_regime

        # Try regime-specific first, fall back to aggregate
        result = self._results.get(label)
        source = label

        if result is None or not result.sufficient_data:
            result = self._results.get("aggregate")
            source = "aggregate"

        if result is None or not result.sufficient_data:
            # Not enough data yet — pass through base size unchanged
            return base_order_usd, "kelly_inactive"

        if result.reason == "no_edge":
            # Kelly says no edge — shrink to floor
            mult = max(cfg.kelly_floor_mult, min(cfg.negative_edge_mult, cfg.kelly_ceiling_mult))
            adjusted = base_order_usd * mult
            return adjusted, f"kelly_no_edge({source},m={mult:.3f})"

        # Apply Kelly multiplier with floor/ceiling clamp
        mult = max(cfg.kelly_floor_mult, min(result.multiplier, cfg.kelly_ceiling_mult))
        adjusted = base_order_usd * mult

        return adjusted, f"kelly_{source}(f={result.f_fractional:.4f},m={mult:.3f})"

    # ------------------------------------------------------------------
    # Telemetry (for dashboard /api/status payload)
    # ------------------------------------------------------------------

    def status_payload(self) -> dict:
        """Return dict for inclusion in /api/status response."""
        payload = {
            "enabled": True,
            "active_regime": self._active_regime,
            "last_update_n": self._last_update_n,
            "kelly_fraction": self.cfg.kelly_fraction,
        }

        for label, result in self._results.items():
            payload[label] = result.to_dict()

        return payload

    # ------------------------------------------------------------------
    # Persistence (for snapshot save/restore)
    # ------------------------------------------------------------------

    def snapshot_state(self) -> dict:
        """Serialize for bot_state snapshot."""
        return {
            "active_regime": self._active_regime,
            "last_update_n": self._last_update_n,
            "results": {k: v.to_dict() for k, v in self._results.items()},
        }

    def restore_state(self, data: dict) -> None:
        """Restore from bot_state snapshot."""
        if not data:
            return
        self._active_regime = data.get("active_regime", "ranging")
        self._last_update_n = data.get("last_update_n", 0)
        # Results will be recomputed on next update(); no need to deserialize

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _log_summary(self) -> None:
        for label, r in self._results.items():
            if r.sufficient_data:
                log.info(
                    "kelly [%s] f*=%.4f f_frac=%.4f mult=%.3f "
                    "win_rate=%.2f%% payoff=%.3f edge=%.4f n=%d (%dW/%dL)",
                    label, r.f_star, r.f_fractional, r.multiplier,
                    r.win_rate * 100, r.payoff_ratio, r.edge,
                    r.n_total, r.n_wins, r.n_losses,
                )
            else:
                log.info("kelly [%s] %s", label, r.reason)


# ---------------------------------------------------------------------------
# Integration example: patching into _slot_order_size_usd
# ---------------------------------------------------------------------------

INTEGRATION_GUIDE = """
# ===== bot.py integration =====

# 1. Import and construct at startup (after config load):

    from kelly_sizer import KellySizer, KellyConfig

    kelly_cfg = KellyConfig(
        kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
        min_samples_total=int(os.getenv("KELLY_MIN_SAMPLES", "30")),
        min_samples_per_regime=int(os.getenv("KELLY_MIN_REGIME_SAMPLES", "15")),
        lookback_cycles=int(os.getenv("KELLY_LOOKBACK", "500")),
        kelly_floor_mult=float(os.getenv("KELLY_FLOOR_MULT", "0.5")),
        kelly_ceiling_mult=float(os.getenv("KELLY_CEILING_MULT", "2.0")),
    )
    self._kelly = KellySizer(kelly_cfg)

# 2. Update in regime eval (alongside _update_regime_tier, every REGIME_EVAL_INTERVAL_SEC):

    # Collect all completed cycles across slots with regime tags
    all_cycles = []
    for slot_id, pair in self._slots.items():
        for c in pair.completed_cycles:
            all_cycles.append({
                "net_profit": c.net_profit,
                "regime_at_entry": c.regime_at_entry,  # see step 4
                "exit_time": c.exit_time,
            })
    regime_label = self._kelly.cfg.regime_labels.get(
        self._regime_tier_state.get("regime_id"), "ranging"
    )
    self._kelly.update(all_cycles, regime_label=regime_label)

# 3. Apply in _slot_order_size_usd (after computing base_with_layers):

    kelly_usd, kelly_reason = self._kelly.size_for_slot(base_with_layers)
    effective = kelly_usd  # replaces base_with_layers going forward
    log.debug("kelly sizing: base=%.2f -> kelly=%.2f (%s)", base_with_layers, kelly_usd, kelly_reason)

# 4. Tag regime at entry time — in the reducer or in bot.py when booking entries:
#    When a FillEvent for an entry creates an exit, stamp the current regime:

    new_exit.regime_at_entry = current_regime_id

#    And carry it through to completed_cycles when the exit fills.

# 5. Snapshot persistence (in save_state / load_state):

    snapshot["kelly_state"] = self._kelly.snapshot_state()
    # On load:
    self._kelly.restore_state(snapshot.get("kelly_state", {}))

# 6. Dashboard telemetry (in status_payload):

    payload["kelly"] = self._kelly.status_payload()

# ===== Config env vars =====
# KELLY_FRACTION=0.25        # quarter-Kelly (very conservative)
# KELLY_MIN_SAMPLES=30       # minimum cycles before activation
# KELLY_MIN_REGIME_SAMPLES=15
# KELLY_LOOKBACK=500
# KELLY_FLOOR_MULT=0.5       # never size below 50% of base
# KELLY_CEILING_MULT=2.0     # never size above 200% of base
"""


# ---------------------------------------------------------------------------
# CLI diagnostic: run against a CSV of cycle data
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    print("Kelly Sizer — diagnostic mode")
    print("=" * 50)

    # Synthetic example data for demonstration
    example_cycles = [
        {"net_profit":  0.012, "regime_at_entry": 2, "exit_time": 1000 + i}
        for i in range(20)
    ] + [
        {"net_profit": -0.008, "regime_at_entry": 2, "exit_time": 1020 + i}
        for i in range(8)
    ] + [
        {"net_profit":  0.010, "regime_at_entry": 1, "exit_time": 1030 + i}
        for i in range(12)
    ] + [
        {"net_profit": -0.009, "regime_at_entry": 1, "exit_time": 1042 + i}
        for i in range(10)
    ] + [
        {"net_profit": -0.011, "regime_at_entry": 0, "exit_time": 1052 + i}
        for i in range(8)
    ] + [
        {"net_profit":  0.007, "regime_at_entry": 0, "exit_time": 1060 + i}
        for i in range(5)
    ]

    cfg = KellyConfig(
        kelly_fraction=0.25,
        min_samples_total=10,
        min_samples_per_regime=10,
    )
    sizer = KellySizer(cfg)
    results = sizer.update(example_cycles, regime_label="bullish")

    print(f"\nTotal cycles: {len(example_cycles)}")
    print(f"Active regime: {sizer._active_regime}\n")

    for label, r in results.items():
        print(f"--- {label} ---")
        print(json.dumps(r.to_dict(), indent=2))
        print()

    # Sizing example
    base_usd = 5.00
    for regime in ["bullish", "ranging", "bearish"]:
        adj, reason = sizer.size_for_slot(base_usd, regime_label=regime)
        print(f"  {regime}: ${base_usd:.2f} -> ${adj:.2f}  ({reason})")
