"""
kraken_client.py -- Kraken REST API wrapper using only the standard library.

Handles:
  - Public endpoints (ticker data -- no auth needed)
  - Private endpoints (placing/cancelling orders -- HMAC-SHA512 signed)
  - Rate-limit awareness (tracks call counter)
  - Dry-run simulation (returns fake responses when DRY_RUN is True)

KRAKEN API SIGNING (how it works):
  1. Generate a nonce (monotonically increasing number -- we use millisecond timestamp)
  2. Build the POST body: "nonce=<nonce>&<other params>"
  3. Compute: SHA256(nonce + POST body)  ->  gives a 32-byte hash
  4. Compute: HMAC-SHA512(url_path + sha256_hash, key=base64_decode(api_secret))
  5. Base64-encode the HMAC result -> this goes in the "API-Sign" header
"""

import time
import hmac
import hashlib
import base64
import json
import logging
import threading
import urllib.request
import urllib.parse
import urllib.error

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kraken API base URLs
# ---------------------------------------------------------------------------
BASE_URL = "https://api.kraken.com"

# Public endpoints (no auth)
TICKER_PATH = "/0/public/Ticker"
ASSET_PAIRS_PATH = "/0/public/AssetPairs"

# Private endpoints (auth required)
ADD_ORDER_PATH = "/0/private/AddOrder"
CANCEL_ORDER_PATH = "/0/private/CancelOrder"
CANCEL_ALL_PATH = "/0/private/CancelAll"
OPEN_ORDERS_PATH = "/0/private/OpenOrders"
QUERY_ORDERS_PATH = "/0/private/QueryOrders"
BALANCE_PATH = "/0/private/Balance"
TRADE_BALANCE_PATH = "/0/private/TradeBalance"
TRADES_HISTORY_PATH = "/0/private/TradesHistory"

# ---------------------------------------------------------------------------
# Rate-limit tracking (thread-safe with circuit breaker)
# ---------------------------------------------------------------------------
# Kraken standard tier: counter starts at 15, each call adds 1,
# counter decays by 1 per second.  If counter hits 0, you get rate-limited.
# We track this locally to avoid hitting the limit.


class _RateLimiter:
    """Thread-safe rate limiter with exponential backoff circuit breaker."""

    def __init__(self, max_budget: int = 15, decay_rate: float = 1.0):
        self._lock = threading.Lock()
        self._max_budget = max_budget
        self._decay_rate = decay_rate
        self._budget = float(max_budget)
        self._last_decay = time.time()
        self._consecutive_errors = 0
        self._circuit_open_until = 0.0  # timestamp; 0 = closed

    def _decay(self):
        """Replenish budget based on elapsed time. Must hold _lock."""
        now = time.time()
        elapsed = now - self._last_decay
        if elapsed > 0:
            self._budget = min(self._max_budget,
                               self._budget + elapsed * self._decay_rate)
            self._last_decay = now

    def consume(self, units: int = 1):
        """Block until budget is available, then deduct units."""
        while True:
            with self._lock:
                # Check circuit breaker
                now = time.time()
                if self._circuit_open_until > now:
                    wait = self._circuit_open_until - now
                    logger.warning("Circuit breaker open, waiting %.1fs", wait)
                else:
                    wait = 0.0

                if wait <= 0:
                    self._decay()
                    if self._budget >= units:
                        self._budget -= units
                        return
                    # Not enough budget -- compute wait time
                    wait = (units - self._budget) / self._decay_rate
                    logger.warning("Rate limit low (%.1f/%d), sleeping %.1fs",
                                   self._budget, self._max_budget, wait)

            # Sleep outside the lock so other threads aren't blocked
            time.sleep(min(wait, 5.0))

    def report_rate_error(self):
        """Called after a Kraken rate-limit or lockout error."""
        with self._lock:
            self._consecutive_errors += 1
            # Exponential backoff: 5s, 10s, 20s, 40s... capped at 60s
            backoff = min(60.0, 5.0 * (2 ** (self._consecutive_errors - 1)))
            self._circuit_open_until = time.time() + backoff
            # Slash budget to prevent immediate retry storm
            self._budget = 0.0
            logger.warning("Rate limit error #%d, circuit open for %.0fs",
                           self._consecutive_errors, backoff)

    def report_success(self):
        """Called after a successful API call."""
        with self._lock:
            self._consecutive_errors = 0
            self._circuit_open_until = 0.0

    def budget_available(self) -> float:
        """Non-blocking check of current available budget."""
        with self._lock:
            self._decay()
            return self._budget


_rate_limiter = _RateLimiter()


def _consume_call():
    """Consume one rate-limit unit (delegates to thread-safe limiter)."""
    _rate_limiter.consume(1)


def budget_available() -> float:
    """Return the current available rate-limit budget (non-blocking)."""
    return _rate_limiter.budget_available()


# ---------------------------------------------------------------------------
# Nonce generation
# ---------------------------------------------------------------------------
# Kraken requires a monotonically increasing nonce for each private API call.
# Using millisecond timestamps works well.  We also keep a floor to guarantee
# monotonicity even if the clock drifts.

_last_nonce = 0
_nonce_lock = threading.Lock()


def _make_nonce() -> str:
    """Generate a nonce that is always greater than the previous one (thread-safe)."""
    global _last_nonce
    with _nonce_lock:
        nonce = int(time.time() * 1000)
        if nonce <= _last_nonce:
            nonce = _last_nonce + 1
        _last_nonce = nonce
        return str(nonce)


# ---------------------------------------------------------------------------
# API signature
# ---------------------------------------------------------------------------

def _sign(url_path: str, data: dict, secret: str) -> str:
    """
    Create the API-Sign header value for a Kraken private endpoint.

    Steps:
      1. Encode the POST data as a URL query string
      2. SHA-256 hash of (nonce + encoded_data)
      3. HMAC-SHA-512 of (url_path_bytes + sha256_hash) using decoded secret
      4. Base64-encode the HMAC digest

    Returns the base64-encoded signature string.
    """
    # Step 1: encode POST body
    encoded = urllib.parse.urlencode(data)

    # Step 2: SHA-256(nonce + POST body)
    nonce_str = str(data["nonce"])
    sha256_hash = hashlib.sha256((nonce_str + encoded).encode("utf-8")).digest()

    # Step 3: HMAC-SHA-512(path + sha256, key=base64_decode(secret))
    secret_bytes = base64.b64decode(secret)
    message = url_path.encode("utf-8") + sha256_hash
    mac = hmac.new(secret_bytes, message, hashlib.sha512)

    # Step 4: base64-encode
    return base64.b64encode(mac.digest()).decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _request(url: str, data: bytes = None, headers: dict = None, timeout: int = 15) -> dict:
    """
    Make an HTTP request and return the parsed JSON response.
    Uses urllib only -- no external dependencies.

    Args:
        url:     Full URL to request.
        data:    POST body as bytes (None for GET).
        headers: Dict of extra headers.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON as a dict.

    Raises:
        Exception on HTTP errors or invalid JSON.
    """
    headers = headers or {}
    headers.setdefault("User-Agent", "DOGEGridBot/1.0")

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error("HTTP %d from %s: %s", e.code, url, body[:500])
        raise
    except urllib.error.URLError as e:
        logger.error("URL error for %s: %s", url, e.reason)
        raise
    except Exception as e:
        logger.error("Request failed for %s: %s", url, e)
        raise


def _public_request(path: str, params: dict = None) -> dict:
    """
    Call a Kraken public endpoint (GET with query params, no auth).
    Returns the 'result' dict or raises on error.
    """
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    resp = _request(url)

    # Kraken wraps responses in {"error": [...], "result": {...}}
    errors = resp.get("error", [])
    if errors:
        raise Exception(f"Kraken API error: {errors}")

    return resp.get("result", {})


def _private_request(path: str, params: dict = None) -> dict:
    """
    Call a Kraken private endpoint (POST with HMAC-SHA512 signature).
    Automatically adds nonce and handles signing.
    Returns the 'result' dict or raises on error.
    """
    if not config.KRAKEN_API_KEY or not config.KRAKEN_API_SECRET:
        raise Exception("Kraken API credentials not configured")

    _consume_call()

    params = params or {}
    params["nonce"] = _make_nonce()

    # Sign the request
    signature = _sign(path, params, config.KRAKEN_API_SECRET)

    # Build headers
    headers = {
        "API-Key": config.KRAKEN_API_KEY,
        "API-Sign": signature,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Encode POST body
    body = urllib.parse.urlencode(params).encode("utf-8")

    url = BASE_URL + path
    resp = _request(url, data=body, headers=headers)

    errors = resp.get("error", [])
    if errors:
        error_str = str(errors)
        if "EAPI:Rate limit" in error_str or "Temporary lockout" in error_str:
            _rate_limiter.report_rate_error()
        raise Exception(f"Kraken API error: {errors}")

    _rate_limiter.report_success()
    return resp.get("result", {})


# ===========================================================================
# PUBLIC API METHODS
# ===========================================================================

def get_ticker(pair: str = None) -> dict:
    """
    Fetch the current ticker data for a trading pair.

    Returns a dict with keys like:
      'a' = ask [price, whole lot volume, lot volume]
      'b' = bid [price, whole lot volume, lot volume]
      'c' = last trade [price, lot volume]
      'v' = volume [today, last 24h]
      'p' = volume weighted average price [today, last 24h]
      'h' = high [today, last 24h]
      'l' = low [today, last 24h]

    Example:
      ticker = get_ticker()
      price = float(ticker['c'][0])  # last trade price
    """
    pair = pair or config.PAIR
    result = _public_request(TICKER_PATH, {"pair": pair})

    # Kraken returns {pair_name: {ticker_data}} -- extract the inner dict
    # The key might be the "altname" variant, so just grab the first value
    if result:
        return next(iter(result.values()))
    return {}


def get_price(pair: str = None) -> float:
    """
    Get the current mid-price (average of best bid and best ask).
    This is more accurate than last-trade price for order placement.
    """
    ticker = get_ticker(pair)
    bid = float(ticker["b"][0])
    ask = float(ticker["a"][0])
    return (bid + ask) / 2.0


def get_spread(pair: str = None) -> tuple:
    """
    Get the current bid, ask, and spread.
    Returns (bid, ask, spread_pct).
    """
    ticker = get_ticker(pair)
    bid = float(ticker["b"][0])
    ask = float(ticker["a"][0])
    spread_pct = ((ask - bid) / bid) * 100.0
    return bid, ask, spread_pct


def get_ohlc(pair: str = None, interval: int = 60) -> list:
    """
    Get OHLC (candle) data.  interval is in minutes.
    Common values: 1, 5, 15, 30, 60, 240, 1440.

    Returns a list of [time, open, high, low, close, vwap, volume, count].
    """
    pair = pair or config.PAIR
    result = _public_request("/0/public/OHLC", {"pair": pair, "interval": interval})
    # Result has the pair data and a "last" key
    for key, value in result.items():
        if key != "last" and isinstance(value, list):
            return value
    return []


def get_asset_pairs(pair: str = None) -> dict:
    """Fetch asset pair info (useful for verifying ticker names and decimal precision)."""
    params = {}
    if pair:
        params["pair"] = pair
    return _public_request(ASSET_PAIRS_PATH, params)


# ===========================================================================
# BATCH PUBLIC API METHODS (for multi-pair swarm)
# ===========================================================================

def get_tickers(pairs: list) -> dict:
    """
    Fetch ticker data for multiple pairs in one call.
    Public endpoint -- no rate limit cost.

    Chunks into groups of 30 for URL length safety.
    Returns raw {kraken_response_key: ticker_data} merged across chunks.
    """
    if not pairs:
        return {}

    result = {}
    # Chunk into groups of 30
    for i in range(0, len(pairs), 30):
        chunk = pairs[i:i + 30]
        pair_str = ",".join(chunk)
        try:
            chunk_result = _public_request(TICKER_PATH, {"pair": pair_str})
            result.update(chunk_result)
        except Exception as e:
            logger.error("Batch ticker failed for chunk %d: %s", i // 30, e)
    return result


def get_prices(pairs: list) -> dict:
    """
    Get mid-prices for multiple pairs in one batch call.
    Returns {input_pair_name: mid_price} for all pairs that returned data.

    Handles Kraken's key aliasing (e.g. input "XDGUSD" -> response "XXDGZUSD")
    by checking substring containment in both directions.
    """
    if not pairs:
        return {}

    raw = get_tickers(pairs)
    if not raw:
        return {}

    # Build reverse lookup: map response keys back to input pair names
    prices = {}
    for input_pair in pairs:
        inp = input_pair.upper()
        matched = False
        for resp_key, ticker in raw.items():
            rk = resp_key.upper()
            # Check substring match both directions
            if inp in rk or rk in inp or inp == rk:
                try:
                    bid = float(ticker["b"][0])
                    ask = float(ticker["a"][0])
                    prices[input_pair] = (bid + ask) / 2.0
                except (KeyError, IndexError, ValueError, TypeError):
                    logger.warning("Bad ticker data for %s: %s", input_pair, ticker)
                matched = True
                break
        if not matched:
            logger.debug("No ticker match for %s in response keys: %s",
                         input_pair, list(raw.keys()))

    return prices


# ===========================================================================
# PRIVATE API METHODS (require auth)
# ===========================================================================

def get_balance() -> dict:
    """
    Get account balances.
    Returns dict like {"ZUSD": "120.0000", "XXDG": "0.0000", ...}
    """
    if config.DRY_RUN:
        logger.debug("[DRY RUN] Returning simulated balance")
        return {"ZUSD": str(config.STARTING_CAPITAL), "XXDG": "0.00000000"}
    return _private_request(BALANCE_PATH)


def get_open_orders() -> dict:
    """
    Get all open orders.
    Returns dict of {txid: order_info, ...}
    """
    if config.DRY_RUN:
        logger.debug("[DRY RUN] Returning empty open orders")
        return {}
    result = _private_request(OPEN_ORDERS_PATH)
    return result.get("open", {})


def place_order(side: str, volume: float, price: float, pair: str = None,
                ordertype: str = "limit") -> str:
    """
    Place an order on Kraken.

    Args:
        side:      "buy" or "sell"
        volume:    Amount of asset to buy/sell
        price:     Limit price (ignored for market orders)
        pair:      Trading pair (defaults to config.PAIR)
        ordertype: "limit" (default) or "market"

    Returns:
        Transaction ID (txid) of the placed order, or a simulated ID in dry run.
    """
    pair = pair or config.PAIR

    if config.DRY_RUN:
        # Generate a fake txid for dry-run tracking
        fake_txid = f"DRY-{side[0].upper()}-{int(time.time() * 1000)}"
        label = "market" if ordertype == "market" else "limit"
        logger.info(
            "[DRY RUN] Would place %s %s %s %s @ $%s ($%.4f)",
            pair, label, side, volume, price, volume * price,
        )
        return fake_txid

    params = {
        "pair": pair,
        "type": side,          # "buy" or "sell"
        "ordertype": ordertype,
        "volume": f"{volume:.8f}",
        # "oflags": "post",    # Uncomment to force post-only (maker) orders
    }
    if ordertype == "limit":
        params["price"] = f"{price:.8f}"

    result = _private_request(ADD_ORDER_PATH, params)

    # Result contains {"descr": {...}, "txid": ["OXXXXX-XXXXX-XXXXXX"]}
    txids = result.get("txid", [])
    if txids:
        txid = txids[0]
        logger.info("Placed %s %s %s @ $%s -> %s", pair, side, volume, price, txid)
        return txid
    else:
        raise Exception(f"Order placed but no txid returned: {result}")


def cancel_order(txid: str) -> bool:
    """
    Cancel a single open order by transaction ID.
    Returns True if cancelled, False otherwise.
    """
    if config.DRY_RUN:
        logger.info("[DRY RUN] Would cancel order %s", txid)
        return True

    try:
        result = _private_request(CANCEL_ORDER_PATH, {"txid": txid})
        count = result.get("count", 0)
        if count > 0:
            logger.info("Cancelled order %s", txid)
            return True
        else:
            logger.warning("Cancel returned count=0 for %s", txid)
            return False
    except Exception as e:
        logger.error("Failed to cancel %s: %s", txid, e)
        return False


def cancel_all_orders() -> int:
    """
    Cancel ALL open orders.  Used during shutdown and grid resets.
    Returns the number of orders cancelled.
    """
    if config.DRY_RUN:
        logger.info("[DRY RUN] Would cancel all open orders")
        return 0

    try:
        result = _private_request(CANCEL_ALL_PATH)
        count = result.get("count", 0)
        logger.info("Cancelled %d open orders", count)
        return count
    except Exception as e:
        logger.error("Failed to cancel all orders: %s", e)
        return 0


def query_orders(txids: list) -> dict:
    """
    Query the status of specific orders by their txids.
    Returns dict of {txid: order_info, ...}.

    order_info includes:
      'status': 'open', 'closed', 'canceled', 'expired'
      'vol_exec': volume executed
      'cost': total cost
      'fee': fees paid
    """
    if config.DRY_RUN:
        return {}

    if not txids:
        return {}

    # Kraken accepts comma-separated txids
    params = {"txid": ",".join(txids)}
    return _private_request(QUERY_ORDERS_PATH, params)


def query_orders_batched(txids: list, batch_size: int = 50) -> dict:
    """
    Query order status for many txids, batching into groups of batch_size.
    Each batch costs 1 private API call.
    Returns merged dict of {txid: order_info}.
    """
    if config.DRY_RUN:
        return {}
    if not txids:
        return {}

    result = {}
    for i in range(0, len(txids), batch_size):
        chunk = txids[i:i + batch_size]
        try:
            chunk_result = query_orders(chunk)
            result.update(chunk_result)
        except Exception as e:
            logger.error("Batch query_orders failed for chunk %d: %s", i // batch_size, e)
    return result


def get_trades_history(start: float = None) -> dict:
    """
    Get recent trade history.
    Args:
        start: Unix timestamp to start from (optional).
    Returns dict of {txid: trade_info, ...}.
    """
    if config.DRY_RUN:
        return {}

    params = {}
    if start:
        params["start"] = str(int(start))
    result = _private_request(TRADES_HISTORY_PATH, params)
    return result.get("trades", {})
