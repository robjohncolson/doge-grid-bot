"""
pair_scanner.py -- Scans Kraken for tradeable pairs, ranks by composite score,
and auto-configures trading parameters.

Used by the swarm dashboard to browse/add pairs.
All API calls are public (no rate limit cost).

Public API:
    scan_all_pairs()           -> dict of pair_name -> PairInfo
    scan_all_usd_pairs()       -> alias for scan_all_pairs() (backward compat)
    get_ranked_pairs(min_vol)  -> sorted list of PairInfo (by composite score)
    auto_configure(info)       -> config.PairConfig
    select_top_pairs(scored)   -> diversity-capped top-N list
"""

import math
import time
import logging

import config
import kraken_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------

class PairInfo:
    """Metadata for a single Kraken trading pair."""
    __slots__ = (
        "pair", "altname", "base", "quote", "price",
        "pair_decimals", "lot_decimals", "ordermin", "costmin",
        "fee_maker", "volatility_pct", "spread_pct", "volume_24h", "status",
        "score", "recovery_mode",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot, 0))

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_pair_cache: dict = {}      # pair_name -> PairInfo
_cache_time: float = 0.0    # timestamp of last scan
CACHE_TTL = 3600             # re-scan hourly


# ---------------------------------------------------------------------------
# Quote currency classification
# ---------------------------------------------------------------------------

def _build_quote_sets():
    """Parse SWARM_SAFE_QUOTES / SWARM_ACCEPTABLE_QUOTES into Kraken-style sets.

    Kraken prefixes some currencies with Z (ZUSD, ZJPY, ZEUR, ZGBP).
    We expand each user-facing symbol to include the Z-prefixed variant.
    """
    safe = set()
    for q in config.SWARM_SAFE_QUOTES.split(","):
        q = q.strip().upper()
        if q:
            safe.add(q)
            safe.add("Z" + q)
    acceptable = set()
    for q in config.SWARM_ACCEPTABLE_QUOTES.split(","):
        q = q.strip().upper()
        if q:
            acceptable.add(q)
            acceptable.add("Z" + q)
    return safe, acceptable

SAFE_QUOTES, ACCEPTABLE_QUOTES = _build_quote_sets()
ALL_QUOTES = SAFE_QUOTES | ACCEPTABLE_QUOTES


def _recovery_mode_for_quote(quote: str) -> str:
    """Return 'lottery' for safe quotes, 'liquidate' for acceptable quotes."""
    if quote in SAFE_QUOTES:
        return "lottery"
    return "liquidate"


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def compute_score(pi: "PairInfo") -> float:
    """Composite grid-bot-friendliness score (0-100).

    Components:
        Volume (30%):  log-scaled 24h USD volume
        Spread (30%):  how much room between spread and min profit target
        Range  (40%):  volatility sweet spot 0.5%-3.0%
    """
    # Volume score: log-scaled, 0-100
    vol_score = min(100, math.log10(max(pi.volume_24h, 1)) / 7 * 100)

    # Spread efficiency: profit_target / spread.  Higher = better.
    min_profit = 2 * pi.fee_maker + 0.10
    if pi.spread_pct < min_profit:
        spread_eff = min(100, (min_profit / max(pi.spread_pct, 0.01)) * 25)
    else:
        spread_eff = 0

    # Volatility fit: sweet spot 0.5%-3.0%.  Penalize outside range.
    v = pi.volatility_pct
    if 0.5 <= v <= 3.0:
        range_score = 100
    elif v < 0.5:
        range_score = max(0, v / 0.5 * 100)
    else:
        range_score = max(0, 100 - (v - 3.0) * 10)

    return round(vol_score * 0.30 + spread_eff * 0.30 + range_score * 0.40, 1)


# ---------------------------------------------------------------------------
# Diversity cap
# ---------------------------------------------------------------------------

def select_top_pairs(scored: list, n: int = 50,
                     max_per_base: int = None) -> list:
    """Apply diversity cap and return top N pairs by score."""
    if max_per_base is None:
        max_per_base = config.SWARM_MAX_PER_BASE
    scored.sort(key=lambda p: p.score, reverse=True)
    base_counts = {}
    result = []
    for pi in scored:
        base = pi.base
        if base_counts.get(base, 0) >= max_per_base:
            continue
        base_counts[base] = base_counts.get(base, 0) + 1
        result.append(pi)
        if len(result) >= n:
            break
    return result


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan_all_pairs() -> dict:
    """
    Fetch all eligible pairs from Kraken, enrich with ticker data.

    Scans USD, JPY, EUR, GBP, USDT, USDC, DOGE (configurable via env vars).
    Each pair is tagged with a composite score and recovery_mode.

    Results cached for 1 hour.
    Returns dict of pair_name -> PairInfo.
    """
    global _pair_cache, _cache_time

    if _pair_cache and (time.time() - _cache_time) < CACHE_TTL:
        return _pair_cache

    logger.info("Scanning all eligible pairs from Kraken...")

    # Step 1: Get all asset pair info
    try:
        all_pairs = kraken_client.get_asset_pairs()
    except Exception as e:
        logger.error("Failed to fetch asset pairs: %s", e)
        return _pair_cache  # return stale cache if available

    # Step 2: Filter to eligible quote currencies, online pairs
    eligible = {}
    for pair_name, info in all_pairs.items():
        quote = info.get("quote", "")
        status = info.get("status", "online")
        if quote not in ALL_QUOTES:
            continue
        if status != "online":
            continue
        # Skip darkpool pairs (contain ".d")
        if ".d" in pair_name:
            continue

        # Extract fee schedule (first tier maker fee)
        fees = info.get("fees_maker", info.get("fees", []))
        fee_maker = 0.26  # default
        if fees and isinstance(fees, list) and len(fees) > 0:
            fee_maker = float(fees[0][1]) if isinstance(fees[0], list) else 0.26

        # Build base display name
        base = info.get("base", "")
        altname = info.get("altname", pair_name)

        pi = PairInfo(
            pair=pair_name,
            altname=altname,
            base=base,
            quote=quote,
            price=0.0,
            pair_decimals=int(info.get("pair_decimals", 6)),
            lot_decimals=int(info.get("lot_decimals", 8)),
            ordermin=float(info.get("ordermin", 0)),
            costmin=float(info.get("costmin", 0.5)),
            fee_maker=fee_maker,
            volatility_pct=0.0,
            spread_pct=0.0,
            volume_24h=0.0,
            status=status,
            score=0.0,
            recovery_mode=_recovery_mode_for_quote(quote),
        )
        eligible[pair_name] = pi

    logger.info("Found %d eligible online pairs", len(eligible))

    # Step 3: Get ticker data in batches of 30
    pair_names = list(eligible.keys())
    for i in range(0, len(pair_names), 30):
        chunk = pair_names[i:i + 30]
        try:
            tickers = kraken_client.get_tickers(chunk)
        except Exception as e:
            logger.warning("Ticker batch %d failed: %s", i // 30, e)
            continue

        for input_pair in chunk:
            inp = input_pair.upper()
            for resp_key, ticker in tickers.items():
                rk = resp_key.upper()
                if inp in rk or rk in inp or inp == rk:
                    pi = eligible[input_pair]
                    try:
                        bid = float(ticker["b"][0])
                        ask = float(ticker["a"][0])
                        pi.price = (bid + ask) / 2.0
                        pi.spread_pct = ((ask - bid) / bid * 100) if bid > 0 else 0
                        high_24h = float(ticker["h"][1])
                        low_24h = float(ticker["l"][1])
                        vwap_24h = float(ticker["p"][1])
                        if vwap_24h > 0:
                            pi.volatility_pct = (high_24h - low_24h) / vwap_24h * 100
                        vol_24h = float(ticker["v"][1])
                        pi.volume_24h = vol_24h * pi.price  # approximate USD volume
                    except (KeyError, IndexError, ValueError, TypeError):
                        pass
                    break

    # Step 4: Compute composite scores
    for pi in eligible.values():
        pi.score = compute_score(pi)

    _pair_cache = eligible
    _cache_time = time.time()
    logger.info("Scan complete: %d pairs with ticker data", len(eligible))

    # Persist to Supabase (best-effort, never fails the scanner)
    try:
        import supabase_store
        pairs_rows = [
            {**pi.to_dict(), "scanned_at": _cache_time}
            for pi in eligible.values()
        ]
        supabase_store.save_pairs(pairs_rows)
    except Exception as e:
        logger.debug("Supabase pairs save skipped: %s", e)

    return eligible


# Backward-compat alias
scan_all_usd_pairs = scan_all_pairs


def get_ranked_pairs(min_volume_usd: float = 1000) -> list:
    """
    Return cached pairs filtered by min volume, sorted by composite score.
    Returns list of PairInfo objects.
    """
    pairs = scan_all_pairs()
    ranked = [
        pi for pi in pairs.values()
        if pi.volume_24h >= min_volume_usd and pi.price > 0
    ]
    ranked.sort(key=lambda p: p.score, reverse=True)
    return ranked


def get_display_name(pair_name: str, altname: str = "",
                     quote: str = "") -> str:
    """Generate a human-readable display name like 'DOGE/USD' or 'ETH/EUR'."""
    name = altname or pair_name
    # Try common quote suffixes (longest first to avoid partial matches)
    for suffix, display_q in (
        ("ZUSD", "USD"), ("ZJPY", "JPY"), ("ZEUR", "EUR"), ("ZGBP", "GBP"),
        ("USDT", "USDT"), ("USDC", "USDC"), ("DOGE", "DOGE"),
        ("USD", "USD"), ("JPY", "JPY"), ("EUR", "EUR"), ("GBP", "GBP"),
    ):
        if name.upper().endswith(suffix):
            base = name[:-len(suffix)]
            # Strip leading X/Z if present (Kraken convention)
            if len(base) > 3 and base[0] in ("X", "Z"):
                base = base[1:]
            return f"{base}/{display_q}"
    return name


def auto_configure(info: PairInfo) -> "config.PairConfig":
    """
    Generate trading parameters for a pair based on its market data.

    Volatility buckets:
        < 3%  -> entry 0.10%  (low vol, tight)
        3-8%  -> entry 0.20%  (medium)
        8-15% -> entry 0.35%  (high)
        > 15% -> entry 0.50%  (very high)

    profit_pct = max(entry_pct, 2 * fee_maker + 0.10)
    """
    vol = info.volatility_pct

    if vol < 3:
        entry_floor = 0.10
    elif vol < 8:
        entry_floor = 0.20
    elif vol < 15:
        entry_floor = 0.35
    else:
        entry_floor = 0.50

    # Entry must exceed half the spread + buffer
    entry_pct = max(info.spread_pct / 2 + 0.05, entry_floor)
    entry_pct = round(entry_pct, 2)

    # Profit must clear round-trip fees with DOGE-like margin
    actual_fee = max(info.fee_maker, config.MAKER_FEE_PCT)
    profit_pct = max(entry_pct, 2 * actual_fee + 0.50)
    profit_pct = round(profit_pct, 2)

    refresh_pct = round(entry_pct * 2, 2)

    display = get_display_name(info.pair, info.altname, info.quote)

    # Determine min volume from Kraken metadata
    min_volume = info.ordermin if info.ordermin > 0 else 1.0

    # Build filter strings for order matching
    base_clean = info.base
    for prefix in ("X", "Z"):
        if len(base_clean) > 3 and base_clean.startswith(prefix):
            base_clean = base_clean[1:]

    recovery_mode = getattr(info, "recovery_mode", "lottery") or "lottery"

    return config.PairConfig(
        pair=info.pair,
        display=display,
        entry_pct=entry_pct,
        profit_pct=profit_pct,
        refresh_pct=refresh_pct,
        order_size_usd=5.0,
        daily_loss_limit=1.0,
        stop_floor=0.0,  # per-pair stop floor disabled for swarm
        min_volume=min_volume,
        price_decimals=info.pair_decimals,
        volume_decimals=info.lot_decimals,
        filter_strings=[base_clean, info.base],
        recovery_mode=recovery_mode,
        capital_budget_usd=10.0,  # conservative default, overridden by _add_pair()
    )
