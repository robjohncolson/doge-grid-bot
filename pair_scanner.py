"""
pair_scanner.py -- Scans Kraken for all USD-quoted pairs, ranks by volatility,
and auto-configures trading parameters.

Used by the swarm dashboard to browse/add pairs.
All API calls are public (no rate limit cost).

Public API:
    scan_all_usd_pairs()       -> dict of pair_name -> PairInfo
    get_ranked_pairs(min_vol)  -> sorted list of PairInfo
    auto_configure(info)       -> config.PairConfig
"""

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
# Scanner
# ---------------------------------------------------------------------------

def scan_all_usd_pairs() -> dict:
    """
    Fetch all USD-quoted online pairs from Kraken, enrich with ticker data.

    1 private call for AssetPairs (public, no rate limit)
    ~21 public calls for tickers (30 pairs per call, ~632 USD pairs)

    Results cached for 1 hour.
    Returns dict of pair_name -> PairInfo.
    """
    global _pair_cache, _cache_time

    if _pair_cache and (time.time() - _cache_time) < CACHE_TTL:
        return _pair_cache

    logger.info("Scanning all USD pairs from Kraken...")

    # Step 1: Get all asset pair info
    try:
        all_pairs = kraken_client.get_asset_pairs()
    except Exception as e:
        logger.error("Failed to fetch asset pairs: %s", e)
        return _pair_cache  # return stale cache if available

    # Step 2: Filter to USD-quoted, online pairs
    usd_pairs = {}
    for pair_name, info in all_pairs.items():
        quote = info.get("quote", "")
        status = info.get("status", "online")
        # USD quote assets: "ZUSD", "USD"
        if quote not in ("ZUSD", "USD"):
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
        wsname = info.get("wsname", "")

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
        )
        usd_pairs[pair_name] = pi

    logger.info("Found %d USD-quoted online pairs", len(usd_pairs))

    # Step 3: Get ticker data in batches of 30
    pair_names = list(usd_pairs.keys())
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
                    pi = usd_pairs[input_pair]
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
                        pi.volume_24h = vol_24h * pi.price  # USD volume
                    except (KeyError, IndexError, ValueError, TypeError):
                        pass
                    break

    _pair_cache = usd_pairs
    _cache_time = time.time()
    logger.info("Scan complete: %d pairs with ticker data", len(usd_pairs))
    return usd_pairs


def get_ranked_pairs(min_volume_usd: float = 1000) -> list:
    """
    Return cached pairs filtered by min volume, sorted by volatility descending.
    Returns list of PairInfo objects.
    """
    pairs = scan_all_usd_pairs()
    ranked = [
        pi for pi in pairs.values()
        if pi.volume_24h >= min_volume_usd and pi.price > 0
    ]
    ranked.sort(key=lambda p: p.volatility_pct, reverse=True)
    return ranked


def get_display_name(pair_name: str, altname: str = "") -> str:
    """Generate a human-readable display name like 'DOGE/USD'."""
    name = altname or pair_name
    # Strip trailing "USD" and add slash
    for suffix in ("USD", "ZUSD"):
        if name.upper().endswith(suffix):
            base = name[:-len(suffix)]
            # Strip leading X/Z if present (Kraken convention)
            if len(base) > 3 and base[0] in ("X", "Z"):
                base = base[1:]
            return f"{base}/USD"
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

    # Profit must exceed round-trip fees
    profit_pct = max(entry_pct, 2 * info.fee_maker + 0.10)
    profit_pct = round(profit_pct, 2)

    refresh_pct = round(entry_pct * 2, 2)

    display = get_display_name(info.pair, info.altname)

    # Determine min volume from Kraken metadata
    min_volume = info.ordermin if info.ordermin > 0 else 1.0

    # Build filter strings for order matching
    base_clean = info.base
    for prefix in ("X", "Z"):
        if len(base_clean) > 3 and base_clean.startswith(prefix):
            base_clean = base_clean[1:]

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
    )
