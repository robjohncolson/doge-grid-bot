"""
stats_engine.py -- Inferential statistics for the DOGE grid trading bot.

Each grid fill is a structured sample from the market's price process.
This module extracts statistical insights from that data:

  1. Profitability significance test (one-sample t-test)
  2. Fill asymmetry regime detector (binomial test)
  3. Grid exceedance check (OHLC-based hidden risk)
  4. Fill rate regime detection (Poisson z-test)
  5. Random walk goodness-of-fit (chi-squared test)

All math is pure Python (no scipy/numpy). Uses standard approximations
for distribution CDFs (Lanczos gamma, continued-fraction beta/gamma).
"""

import math
import time
import logging

import config

logger = logging.getLogger(__name__)


# ============================================================================
# Pure Python distribution functions
# ============================================================================

def _normal_cdf(x):
    """Standard normal CDF via Abramowitz & Stegun rational approx (~1e-7)."""
    if x < -8.0:
        return 0.0
    if x > 8.0:
        return 1.0
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    p = d * math.exp(-x * x / 2.0) * t * (
        0.319381530 + t * (-0.356563782 + t * (
            1.781477937 + t * (-1.821255978 + t * 1.330274429)))
    )
    return 1.0 - p if x >= 0 else p


def _log_gamma(x):
    """Log-gamma via Lanczos approximation (g=7, 9 coefficients)."""
    if x <= 0:
        return float('inf')
    c = [
        0.99999999999980993, 676.5203681218851, -1259.1392167224028,
        771.32342877765313, -176.61502916214059, 12.507343278686905,
        -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7,
    ]
    x -= 1
    t = c[0]
    for i in range(1, 9):
        t += c[i] / (x + i)
    w = x + 7.5
    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(w) - w + math.log(t)


def _beta_cf(x, a, b, max_iter=200, tol=1e-10):
    """Continued fraction for the regularized incomplete beta (Lentz's method)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        # even step
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        # odd step
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < tol:
            break
    return h


def _reg_inc_beta(x, a, b):
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = _log_gamma(a + b) - _log_gamma(a) - _log_gamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_cf(x, a, b) / a
    else:
        return 1.0 - front * _beta_cf(1.0 - x, b, a) / b


def _gamma_lower(a, x, max_iter=200, tol=1e-10):
    """Lower regularized incomplete gamma P(a, x) via series / CF."""
    if x <= 0:
        return 0.0
    if x < a + 1.0:
        # series expansion
        ap = a
        total = 1.0 / a
        delta = 1.0 / a
        for _ in range(max_iter):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) < tol * abs(total):
                break
        return total * math.exp(-x + a * math.log(x) - _log_gamma(a))
    else:
        # upper CF then subtract
        b_cf = x + 1.0 - a
        d = 1.0 / b_cf if abs(b_cf) > 1e-30 else 1e30
        h = d
        for i in range(1, max_iter + 1):
            an = -i * (i - a)
            b_cf += 2.0
            d = an * d + b_cf
            if abs(d) < 1e-30:
                d = 1e-30
            c = b_cf + an / (h if abs(h) > 1e-30 else 1e-30)
            if abs(c) < 1e-30:
                c = 1e-30
            d = 1.0 / d
            delta = d * c
            h *= delta
            if abs(delta - 1.0) < tol:
                break
        q = math.exp(-x + a * math.log(x) - _log_gamma(a)) * h
        return 1.0 - q


def _t_cdf_two_tail_p(t_val, df):
    """Two-tailed p-value for Student's t-distribution."""
    if df <= 0:
        return 1.0
    x = df / (df + t_val * t_val)
    return _reg_inc_beta(x, df / 2.0, 0.5)


def _chi2_cdf(x, df):
    """CDF of chi-squared distribution: P(X < x | df)."""
    if x <= 0:
        return 0.0
    return _gamma_lower(df / 2.0, x / 2.0)


# t-critical values for 95% CI (two-tailed alpha=0.05 -> each tail 0.025)
_T_CRIT = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980,
}


def _t_critical(df):
    """Lookup t-critical value for 95% CI. Normal approx for large df."""
    if df >= 120:
        return 1.96
    # find closest key
    best = 1.96
    best_dist = float('inf')
    for k, v in _T_CRIT.items():
        if abs(k - df) < best_dist:
            best_dist = abs(k - df)
            best = v
    return best


# ============================================================================
# Verdict color mapping
# ============================================================================

_GREEN = {
    "significant_profit", "symmetric", "contained", "normal", "mean_reverting",
    "well_tuned", "fast",
}
_RED = {
    "significant_loss", "trend_detected", "high_risk", "high_vol",
    "low_vol", "momentum", "high_volatility", "low_volatility", "slow",
}
# everything else -> yellow


def verdict_color(verdict):
    """Map a verdict string to a display color."""
    if verdict in _GREEN:
        return "green"
    if verdict in _RED:
        return "red"
    return "yellow"


# ============================================================================
# Analyzer 1: Profitability significance test
# ============================================================================

def analyze_profitability(recent_fills):
    """
    One-sample t-test on round-trip profits.
    H0: mean profit per round trip = 0.
    """
    profits = [f["profit"] for f in recent_fills if f.get("profit", 0) != 0]
    n = len(profits)

    if n < 3:
        return _result("profitability", "insufficient_data", "none",
                        f"Need >= 3 round trips (have {n})",
                        {"n": n, "min_needed": 3})

    mean = sum(profits) / n
    var = sum((p - mean) ** 2 for p in profits) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0

    if std == 0:
        v = "significant_profit" if mean > 0 else "significant_loss"
        return _result("profitability", v, "high",
                        f"All {n} trips identical: ${mean:.4f}",
                        {"n": n, "mean": round(mean, 6), "std": 0, "p_value": 0})

    se = std / math.sqrt(n)
    t_stat = mean / se
    df = n - 1
    p_value = _t_cdf_two_tail_p(t_stat, df)

    tc = _t_critical(df)
    ci_lo = mean - tc * se
    ci_hi = mean + tc * se

    # estimate min sample for significance
    min_needed = max(n, int(math.ceil((tc * std / abs(mean)) ** 2))) if abs(mean) > 1e-10 else 999

    if p_value < 0.05 and mean > 0:
        verdict = "significant_profit"
        conf = "high"
        summary = f"Profitable: ${mean:.4f}/trip, p={p_value:.3f}, 95% CI [${ci_lo:.4f}, ${ci_hi:.4f}]"
    elif p_value < 0.05 and mean <= 0:
        verdict = "significant_loss"
        conf = "high"
        summary = f"Losing: ${mean:.4f}/trip, p={p_value:.3f}"
    else:
        verdict = "not_significant"
        conf = "low"
        more = max(0, min_needed - n)
        summary = f"Inconclusive: ${mean:.4f}/trip, p={p_value:.2f}, ~{more} more trips needed"

    return _result("profitability", verdict, conf, summary, {
        "n": n, "mean": round(mean, 6), "std": round(std, 6),
        "t_stat": round(t_stat, 3), "p_value": round(p_value, 4),
        "ci_low": round(ci_lo, 6), "ci_high": round(ci_hi, 6),
        "df": df, "min_needed": min_needed,
    })


# ============================================================================
# Analyzer 2: Fill asymmetry (binomial test)
# ============================================================================

def analyze_fill_asymmetry(recent_fills, window_seconds=43200):
    """
    Binomial test on buy vs sell fill counts.
    H0: P(buy fill) = 0.5 (symmetric / range-bound market).
    """
    now = time.time()
    cutoff = now - window_seconds
    buys = sum(1 for f in recent_fills
               if f.get("time", 0) > cutoff and f["side"] == "buy")
    sells = sum(1 for f in recent_fills
                if f.get("time", 0) > cutoff and f["side"] == "sell")
    n = buys + sells

    if n < 5:
        return _result("fill_asymmetry", "insufficient_data", "none",
                        f"Need >= 5 fills in window (have {n})",
                        {"n": n, "buys": buys, "sells": sells})

    p_hat = buys / n
    k = max(buys, sells)

    # exact two-tailed binomial p-value via incomplete beta
    p_tail = 1.0 - _reg_inc_beta(0.5, n - k + 1, float(k))
    p_value = min(1.0, 2 * p_tail)

    # Wilson score 95% CI for proportion
    z = 1.96
    denom = 1 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * n)) / n) / denom
    ci_lo = max(0, center - margin)
    ci_hi = min(1, center + margin)

    if p_value < 0.05:
        verdict = "trend_detected"
        conf = "high" if p_value < 0.01 else "medium"
        if config.STRATEGY_MODE == "pair":
            if buys > sells:
                direction = "Buy entries filling more often -- price trending down"
            else:
                direction = "Sell entries filling more often -- price trending up"
            summary = f"Asymmetric: {buys}B/{sells}S ({p_hat:.0%} buy), p={p_value:.3f} -- {direction}"
        else:
            direction = "buys (price falling)" if buys > sells else "sells (price rising)"
            summary = f"Asymmetric: {buys}B/{sells}S ({p_hat:.0%} buy), p={p_value:.3f} -- skewed toward {direction}"
    else:
        verdict = "symmetric"
        conf = "medium" if n >= 20 else "low"
        summary = f"Symmetric: {buys}B/{sells}S ({p_hat:.0%} buy), p={p_value:.2f}"

    return _result("fill_asymmetry", verdict, conf, summary, {
        "n": n, "buys": buys, "sells": sells,
        "buy_proportion": round(p_hat, 3),
        "p_value": round(p_value, 4),
        "ci_low": round(ci_lo, 3), "ci_high": round(ci_hi, 3),
        "window_hours": window_seconds // 3600,
    })


# ============================================================================
# Analyzer 3: Grid exceedance (OHLC-based hidden risk)
# ============================================================================

def analyze_grid_exceedance(grid_orders, ohlc_data):
    """
    How often did price escape the grid range between polls?
    Uses Kraken OHLC candle highs/lows vs grid boundaries.
    """
    if not ohlc_data or not grid_orders:
        return _result("grid_exceedance", "no_data", "none",
                        "No OHLC or grid data available", {})

    active = [o for o in grid_orders if o.status == "open"] or grid_orders
    buy_prices = [o.price for o in active if o.side == "buy"]
    sell_prices = [o.price for o in active if o.side == "sell"]

    if not buy_prices or not sell_prices:
        return _result("grid_exceedance", "no_data", "none",
                        "Incomplete grid (no buys or sells)", {})

    grid_lo = min(buy_prices)
    grid_hi = max(sell_prices)

    exceedances = 0
    total = 0
    worst = 0.0
    above = 0
    below = 0

    for candle in ohlc_data:
        try:
            high = float(candle[2])
            low = float(candle[3])
        except (IndexError, ValueError, TypeError):
            continue
        total += 1
        hit = False
        if high > grid_hi:
            pct = (high - grid_hi) / grid_hi * 100
            worst = max(worst, pct)
            above += 1
            hit = True
        if low < grid_lo:
            pct = (grid_lo - low) / grid_lo * 100
            worst = max(worst, pct)
            below += 1
            hit = True
        if hit:
            exceedances += 1

    if total == 0:
        return _result("grid_exceedance", "no_data", "none",
                        "No valid candle data", {})

    pct = exceedances / total * 100

    if pct > 20:
        verdict, conf = "high_risk", "high"
        summary = f"HIGH RISK: {exceedances}/{total} candles ({pct:.0f}%) exceeded grid, worst {worst:.1f}%"
    elif pct > 5:
        verdict, conf = "moderate_risk", "medium"
        summary = f"Moderate: {exceedances}/{total} candles ({pct:.0f}%) exceeded grid, worst {worst:.1f}%"
    else:
        verdict = "contained"
        conf = "high" if total >= 20 else "medium"
        summary = f"Contained: {exceedances}/{total} candles ({pct:.0f}%) exceeded grid"

    return _result("grid_exceedance", verdict, conf, summary, {
        "exceedances": exceedances, "total_candles": total,
        "exceedance_pct": round(pct, 1), "worst_breach_pct": round(worst, 2),
        "grid_low": round(grid_lo, 6), "grid_high": round(grid_hi, 6),
        "above": above, "below": below,
    })


# ============================================================================
# Analyzer 3b: Volatility vs targets (pair mode replacement for exceedance)
# ============================================================================

def analyze_volatility_vs_targets(ohlc_data):
    """
    Pair mode: compare OHLC candle ranges to PAIR_ENTRY_PCT and PAIR_PROFIT_PCT.
    Answers: is current volatility right for my entry/exit targets?
    """
    if not ohlc_data:
        return _result("volatility_targets", "no_data", "none",
                        "No OHLC data available", {})

    entry_pct = config.PAIR_ENTRY_PCT
    profit_pct = config.PAIR_PROFIT_PCT
    ranges = []

    for candle in ohlc_data:
        try:
            high = float(candle[2])
            low = float(candle[3])
        except (IndexError, ValueError, TypeError):
            continue
        if low > 0:
            ranges.append((high - low) / low * 100)

    if len(ranges) < 10:
        return _result("volatility_targets", "no_data", "none",
                        f"Need >= 10 candles (have {len(ranges)})", {})

    ranges.sort()
    n = len(ranges)
    median = ranges[n // 2] if n % 2 else (ranges[n // 2 - 1] + ranges[n // 2]) / 2
    mean_range = sum(ranges) / n

    entry_reachable = sum(1 for r in ranges if r >= entry_pct)
    exit_reachable = sum(1 for r in ranges if r >= profit_pct)
    entry_reach_pct = entry_reachable / n * 100
    exit_reach_pct = exit_reachable / n * 100

    if median < entry_pct:
        verdict = "low_volatility"
        conf = "high" if n >= 50 else "medium"
        summary = (f"Low vol: median candle range {median:.3f}% < entry target "
                   f"{entry_pct:.2f}% -- entries won't fill often "
                   f"({entry_reach_pct:.0f}% of candles reach entry)")
    elif median > profit_pct:
        verdict = "high_volatility"
        conf = "high" if n >= 50 else "medium"
        summary = (f"High vol: median candle range {median:.3f}% > profit target "
                   f"{profit_pct:.2f}% -- profits at risk of reversal "
                   f"({exit_reach_pct:.0f}% of candles reach exit)")
    else:
        verdict = "well_tuned"
        conf = "high" if n >= 50 else "medium"
        summary = (f"Well tuned: median range {median:.3f}% fits between "
                   f"entry {entry_pct:.2f}% and profit {profit_pct:.2f}% "
                   f"({entry_reach_pct:.0f}% reach entry, {exit_reach_pct:.0f}% reach exit)")

    return _result("volatility_targets", verdict, conf, summary, {
        "median_range_pct": round(median, 4),
        "mean_range_pct": round(mean_range, 4),
        "entry_pct": entry_pct,
        "profit_pct": profit_pct,
        "entry_reachable_pct": round(entry_reach_pct, 1),
        "exit_reachable_pct": round(exit_reach_pct, 1),
        "total_candles": n,
    })


# ============================================================================
# Analyzer 4: Fill rate regime detection (Poisson z-test)
# ============================================================================

def analyze_fill_rate(recent_fills):
    """
    Compare last-hour fill rate to historical baseline.
    Tests whether the current regime is significantly different.
    """
    now = time.time()
    fills = [f for f in recent_fills if f.get("time", 0) > 0]

    if len(fills) < 5:
        return _result("fill_rate", "insufficient_data", "none",
                        f"Need >= 5 fills for regime detection (have {len(fills)})",
                        {"total_fills": len(fills)})

    hour_ago = now - 3600
    current = [f for f in fills if f["time"] > hour_ago]
    earlier = [f for f in fills if f["time"] <= hour_ago]

    current_count = len(current)

    if not earlier:
        return _result("fill_rate", "insufficient_data", "none",
                        "No baseline history (bot < 1hr old)",
                        {"current_rate": current_count, "baseline_rate": 0})

    earliest = min(f["time"] for f in earlier)
    baseline_hours = max(0.01, (hour_ago - earliest) / 3600)
    baseline_rate = len(earlier) / baseline_hours
    current_rate = float(current_count)  # fills in exactly 1 hour

    if baseline_rate <= 0:
        return _result("fill_rate", "insufficient_data", "none",
                        "Baseline rate is zero", {"current_rate": current_rate})

    # Poisson z-test: (observed - expected) / sqrt(expected)
    z = (current_rate - baseline_rate) / math.sqrt(baseline_rate)
    p_value = 2 * (1 - _normal_cdf(abs(z)))
    sigma = abs(z)

    if sigma > 3:
        if current_rate > baseline_rate:
            verdict = "high_vol"
            summary = (f"REGIME CHANGE: {current_rate:.0f}/hr vs "
                       f"{baseline_rate:.1f}/hr baseline ({sigma:.1f} sigma)")
        else:
            verdict = "low_vol"
            summary = (f"LOW ACTIVITY: {current_rate:.0f}/hr vs "
                       f"{baseline_rate:.1f}/hr baseline ({sigma:.1f} sigma)")
        conf = "high"
    elif sigma > 2:
        verdict = "elevated" if current_rate > baseline_rate else "reduced"
        conf = "medium"
        summary = (f"{'Elevated' if current_rate > baseline_rate else 'Reduced'}: "
                   f"{current_rate:.0f}/hr vs {baseline_rate:.1f}/hr ({sigma:.1f} sigma)")
    else:
        verdict = "normal"
        conf = "medium" if len(fills) >= 20 else "low"
        summary = f"Normal: {current_rate:.0f}/hr vs {baseline_rate:.1f}/hr ({sigma:.1f} sigma)"

    return _result("fill_rate", verdict, conf, summary, {
        "baseline_rate": round(baseline_rate, 2),
        "current_rate": round(current_rate, 2),
        "z_score": round(z, 2), "sigma": round(sigma, 2),
        "p_value": round(p_value, 4),
        "baseline_fills": len(earlier), "current_fills": current_count,
    })


# ============================================================================
# Analyzer 5: Random walk goodness-of-fit (chi-squared)
# ============================================================================

def analyze_random_walk(recent_fills, center_price, spacing_pct, max_levels):
    """
    Chi-squared test: does the fill-level distribution match a random walk?
    Under RW null, P(fill at distance k) ~ 1/k (harmonic distribution).
    Inner-heavy -> mean reversion.  Outer-heavy -> momentum.
    """
    if not recent_fills or center_price <= 0 or spacing_pct <= 0:
        return _result("random_walk", "insufficient_data", "none",
                        "Need fills and an active grid", {"n": 0})

    spacing = center_price * spacing_pct / 100.0
    counts = {}

    for f in recent_fills:
        dist = round(abs(f["price"] - center_price) / spacing) if spacing > 0 else 0
        dist = max(1, min(dist, max_levels))
        counts[dist] = counts.get(dist, 0) + 1

    levels = sorted(counts.keys())
    n = sum(counts.values())

    if len(levels) < 3 or n < 10:
        return _result("random_walk", "insufficient_data", "none",
                        f"Need >= 10 fills across >= 3 levels (have {n} across {len(levels)})",
                        {"n": n, "levels_hit": len(levels)})

    # expected under RW: harmonic distribution P(k) ~ 1/k
    harmonic = sum(1.0 / k for k in levels)
    observed = [counts[k] for k in levels]
    expected = [n * (1.0 / k) / harmonic for k in levels]

    # chi-squared (merge bins if expected < 5)
    obs_m, exp_m = [], []
    o_acc, e_acc = 0, 0.0
    for o, e in zip(observed, expected):
        o_acc += o
        e_acc += e
        if e_acc >= 5:
            obs_m.append(o_acc)
            exp_m.append(e_acc)
            o_acc, e_acc = 0, 0.0
    if o_acc > 0:
        if exp_m:
            obs_m[-1] += o_acc
            exp_m[-1] += e_acc
        else:
            obs_m.append(o_acc)
            exp_m.append(e_acc)

    if len(obs_m) < 2:
        return _result("random_walk", "insufficient_data", "none",
                        "Not enough bins after merging", {"n": n})

    chi2 = sum((o - e) ** 2 / e for o, e in zip(obs_m, exp_m) if e > 0)
    df = len(obs_m) - 1
    p_value = 1.0 - _chi2_cdf(chi2, df)

    # direction: are inner levels over-represented?
    inner_excess = 0.0
    for k, o, e in zip(levels, observed, expected):
        if k <= max(2, max_levels // 3):
            inner_excess += o - e

    if p_value < 0.05:
        if inner_excess > 0:
            verdict = "mean_reverting"
            summary = f"MEAN REVERTING: chi2={chi2:.1f} (df={df}), p={p_value:.3f} -- grid has edge"
        else:
            verdict = "momentum"
            summary = f"MOMENTUM: chi2={chi2:.1f} (df={df}), p={p_value:.3f} -- grid disadvantaged"
        conf = "high" if p_value < 0.01 else "medium"
    else:
        verdict = "random_walk"
        conf = "medium" if n >= 30 else "low"
        summary = f"Random walk: chi2={chi2:.1f} (df={df}), p={p_value:.2f} -- no detectable edge"

    return _result("random_walk", verdict, conf, summary, {
        "chi2": round(chi2, 2), "df": df, "p_value": round(p_value, 4),
        "n": n, "levels_hit": len(levels),
        "inner_excess": round(inner_excess, 1),
        "direction": "inner" if inner_excess > 0 else "outer",
    })


# ============================================================================
# Analyzer 5b: Round trip duration (pair mode replacement for random walk)
# ============================================================================

def analyze_round_trip_duration(recent_fills):
    """
    Pair mode: measure time between entry fill and exit fill for completed
    round trips. Entry = profit==0, exit = profit!=0.
    """
    # Pair round trips: entry (profit=0) followed by exit (profit!=0)
    # Walk fills chronologically, match entries to their next exit
    fills_chrono = sorted(
        [f for f in recent_fills if f.get("time", 0) > 0],
        key=lambda f: f["time"],
    )

    durations = []
    pending_entry = None

    for f in fills_chrono:
        profit = f.get("profit", 0)
        if profit == 0:
            # This is an entry fill -- start a new potential round trip
            pending_entry = f
        elif pending_entry is not None:
            # This is an exit fill -- complete the round trip
            dt = f["time"] - pending_entry["time"]
            if dt > 0:
                durations.append(dt)
            pending_entry = None

    n = len(durations)
    if n < 3:
        return _result("round_trip_duration", "insufficient_data", "none",
                        f"Need >= 3 completed round trips (have {n})",
                        {"n": n, "min_needed": 3})

    durations.sort()
    mean_dur = sum(durations) / n
    median_dur = durations[n // 2] if n % 2 else (durations[n // 2 - 1] + durations[n // 2]) / 2
    min_dur = durations[0]
    max_dur = durations[-1]

    # Convert to minutes for display
    mean_min = mean_dur / 60
    median_min = median_dur / 60
    min_min = min_dur / 60
    max_min = max_dur / 60

    if median_min < 5:
        verdict = "fast"
        conf = "high" if n >= 10 else "medium"
        summary = (f"Fast: median {median_min:.1f} min/trip "
                   f"(mean {mean_min:.1f}, range {min_min:.1f}-{max_min:.1f}) "
                   f"-- high activity, {n} trips")
    elif median_min <= 60:
        verdict = "normal"
        conf = "high" if n >= 10 else "medium"
        summary = (f"Normal: median {median_min:.1f} min/trip "
                   f"(mean {mean_min:.1f}, range {min_min:.1f}-{max_min:.1f}) "
                   f"-- expected pace, {n} trips")
    else:
        verdict = "slow"
        conf = "high" if n >= 10 else "medium"
        summary = (f"Slow: median {median_min:.1f} min/trip "
                   f"(mean {mean_min:.1f}, range {min_min:.1f}-{max_min:.1f}) "
                   f"-- consider tightening targets, {n} trips")

    return _result("round_trip_duration", verdict, conf, summary, {
        "n": n,
        "mean_minutes": round(mean_min, 1),
        "median_minutes": round(median_min, 1),
        "min_minutes": round(min_min, 1),
        "max_minutes": round(max_min, 1),
    })


# ============================================================================
# Overall strategy health
# ============================================================================

def _compute_health(results):
    """Derive an overall verdict from the 5 analyzers."""
    v = {r["name"]: r["verdict"] for r in results.values() if isinstance(r, dict) and "name" in r}

    is_pair = config.STRATEGY_MODE == "pair"

    if is_pair:
        # Pair mode health: use pair-specific analyzer names
        if v.get("round_trip_duration") == "slow":
            return {"verdict": "unfavorable", "color": "red",
                    "summary": "Round trips are slow -- consider tightening entry or profit targets"}
        if v.get("fill_rate") in ("high_vol",) and v.get("fill_asymmetry") == "trend_detected":
            return {"verdict": "dangerous", "color": "red",
                    "summary": "High volatility + trending -- consider pausing"}
        if v.get("volatility_targets") == "high_volatility":
            return {"verdict": "exposed", "color": "red",
                    "summary": "Volatility exceeds profit target -- profits at risk of reversal"}
        if v.get("volatility_targets") == "low_volatility":
            return {"verdict": "unfavorable", "color": "red",
                    "summary": "Volatility below entry distance -- entries won't fill often"}
        if v.get("round_trip_duration") == "fast":
            return {"verdict": "favorable", "color": "green",
                    "summary": "Fast round trips -- pair strategy is performing well"}
        if v.get("volatility_targets") == "well_tuned":
            return {"verdict": "favorable", "color": "green",
                    "summary": "Volatility matches entry/exit targets -- well configured"}
    else:
        # Grid mode health: original logic
        if v.get("random_walk") == "momentum":
            return {"verdict": "unfavorable", "color": "red",
                    "summary": "Momentum market -- grid strategy disadvantaged"}
        if v.get("fill_rate") in ("high_vol",) and v.get("fill_asymmetry") == "trend_detected":
            return {"verdict": "dangerous", "color": "red",
                    "summary": "High volatility + trending -- consider pausing"}
        if v.get("grid_exceedance") == "high_risk":
            return {"verdict": "exposed", "color": "red",
                    "summary": "Price regularly escaping grid range -- widen grid or increase levels"}
        if v.get("random_walk") == "mean_reverting":
            return {"verdict": "favorable", "color": "green",
                    "summary": "Mean-reverting market -- grid strategy has statistical edge"}

    if v.get("profitability") == "significant_profit":
        return {"verdict": "profitable", "color": "green",
                "summary": "Statistically significant profits confirmed"}
    if all(v.get(k) in ("insufficient_data", "no_data") for k in v):
        return {"verdict": "calibrating", "color": "yellow",
                "summary": "Collecting data -- need more fills for analysis"}
    return {"verdict": "neutral", "color": "yellow",
            "summary": "No clear signal yet -- keep running"}


# ============================================================================
# Public API
# ============================================================================

def run_all(state, current_price, ohlc_data=None):
    """
    Run all 5 analyzers and return a dict of results.
    Called periodically from the main loop (every 60s).
    Pair mode swaps analyzers 3 and 5 for pair-specific versions.
    """
    results = {}
    is_pair = config.STRATEGY_MODE == "pair"

    try:
        results["profitability"] = analyze_profitability(state.recent_fills)
    except Exception as e:
        logger.debug("Analyzer profitability error: %s", e)
        results["profitability"] = _result("profitability", "error", "none", str(e), {})

    try:
        results["fill_asymmetry"] = analyze_fill_asymmetry(state.recent_fills)
    except Exception as e:
        logger.debug("Analyzer fill_asymmetry error: %s", e)
        results["fill_asymmetry"] = _result("fill_asymmetry", "error", "none", str(e), {})

    # Analyzer 3: pair mode uses volatility_vs_targets, grid mode uses grid_exceedance
    if is_pair:
        try:
            results["volatility_targets"] = analyze_volatility_vs_targets(ohlc_data)
        except Exception as e:
            logger.debug("Analyzer volatility_targets error: %s", e)
            results["volatility_targets"] = _result("volatility_targets", "error", "none", str(e), {})
    else:
        try:
            results["grid_exceedance"] = analyze_grid_exceedance(
                state.grid_orders, ohlc_data)
        except Exception as e:
            logger.debug("Analyzer grid_exceedance error: %s", e)
            results["grid_exceedance"] = _result("grid_exceedance", "error", "none", str(e), {})

    try:
        results["fill_rate"] = analyze_fill_rate(state.recent_fills)
    except Exception as e:
        logger.debug("Analyzer fill_rate error: %s", e)
        results["fill_rate"] = _result("fill_rate", "error", "none", str(e), {})

    # Analyzer 5: pair mode uses round_trip_duration, grid mode uses random_walk
    if is_pair:
        try:
            results["round_trip_duration"] = analyze_round_trip_duration(state.recent_fills)
        except Exception as e:
            logger.debug("Analyzer round_trip_duration error: %s", e)
            results["round_trip_duration"] = _result("round_trip_duration", "error", "none", str(e), {})
    else:
        try:
            results["random_walk"] = analyze_random_walk(
                state.recent_fills, state.center_price,
                config.GRID_SPACING_PCT, config.GRID_LEVELS)
        except Exception as e:
            logger.debug("Analyzer random_walk error: %s", e)
            results["random_walk"] = _result("random_walk", "error", "none", str(e), {})

    results["overall_health"] = _compute_health(results)

    return results


def format_for_ai(results):
    """Format stats as context for the AI advisor prompt.

    Each analyzer line is prefixed with its verdict in uppercase brackets
    so the AI can quickly parse signal strength.
    """
    if not results:
        return ""

    lines = ["STATISTICAL ANALYSIS (from bot's own fill data):"]
    # Include whichever analyzers are present (mode-dependent)
    for name in ("profitability", "fill_asymmetry", "grid_exceedance",
                 "volatility_targets", "fill_rate", "random_walk",
                 "round_trip_duration"):
        r = results.get(name)
        if r:
            verdict = r.get("verdict", "unknown").upper()
            lines.append(f"- [{verdict}] {r['summary']}")

    health = results.get("overall_health", {})
    if health:
        health_verdict = health.get("verdict", "unknown").upper()
        lines.append(f"OVERALL: [{health_verdict}] {health.get('summary', 'N/A')}")

    return "\n".join(lines)


# ============================================================================
# Helpers
# ============================================================================

def _result(name, verdict, confidence, summary, detail):
    """Build a standardized analyzer result dict."""
    return {
        "name": name,
        "verdict": verdict,
        "color": verdict_color(verdict),
        "confidence": confidence,
        "summary": summary,
        "detail": detail,
    }
