"""Hyperliquid REST client — leaderboard + per-wallet fills. Throttled & retrying.

Pure data access: no episode/metric logic (that lives in fills.py / metrics.py).
"""
import json
import threading
import time
import urllib.error
import urllib.request

from hyper import config

_last_post = [0.0]
_pace_lock = threading.Lock()   # serialize POST spacing across worker threads (the network call
#                                 itself runs OUTSIDE the lock, so RTTs overlap = real concurrency)
_stats_lock = threading.Lock()
_request_stats = {"requests": 0, "retries": 0, "estimated_weight": 0}
_WEIGHT_ESTIMATE = {
    "userFills": 20, "userFillsByTime": 20, "portfolio": 20,
    "clearinghouseState": 2, "spotClearinghouseState": 2,
    "candleSnapshot": 2, "meta": 2, "metaAndAssetCtxs": 2,
}


def reset_request_stats():
    with _stats_lock:
        _request_stats.update(requests=0, retries=0, estimated_weight=0)


def request_stats():
    with _stats_lock:
        return dict(_request_stats)


def _get(url: str, retries: int = 3):
    err = None
    with _stats_lock:
        _request_stats["requests"] += 1
        _request_stats["estimated_weight"] += 1
    for attempt in range(retries):
        if attempt:
            with _stats_lock:
                _request_stats["retries"] += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=config.UA), timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(0.5 * (attempt + 1))
    raise err  # type: ignore[misc]


def post(body: dict, retries: int = 7):
    """POST to the info endpoint, globally paced and with 429-aware backoff."""
    data = json.dumps(body).encode()
    err = None
    with _stats_lock:
        _request_stats["requests"] += 1
        _request_stats["estimated_weight"] += _WEIGHT_ESTIMATE.get(body.get("type"), 1)
    for attempt in range(retries):
        if attempt:
            with _stats_lock:
                _request_stats["retries"] += 1
        with _pace_lock:                                   # only the spacing is serialized ...
            wait = config.MIN_POST_INTERVAL - (time.time() - _last_post[0])
            if wait > 0:
                time.sleep(wait)
            _last_post[0] = time.time()
        try:                                               # ... the request below runs concurrently
            req = urllib.request.Request(config.INFO_URL, data=data, headers=config.UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            err = exc
            time.sleep(min(2.0 ** attempt, 20.0) if exc.code == 429 else 0.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(0.5 * (attempt + 1))
    raise err  # type: ignore[misc]


def post_soft(body: dict):
    """Like post() but returns None on failure instead of raising (for backfill)."""
    try:
        return post(body, retries=4)
    except Exception:  # noqa: BLE001
        return None


def realtime_post_soft(body: dict, timeout: float = 5.0):
    """Low-latency market-data POST for dashboard/risk marks.

    This deliberately does not use the global historical/fill pacer: one allMids call every few seconds is
    cheap, and sharing the fill-signal queue can leave stock marks stale behind dozens of userFills calls."""
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(config.INFO_URL, data=data, headers=config.UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


# -- higher-level reads -------------------------------------------------------
def get_leaderboard() -> list:
    data = _get(config.LEADERBOARD_URL)
    return data["leaderboardRows"] if isinstance(data, dict) else data


def user_fills_by_time(addr: str, start_ms: int, aggregate: bool = True):
    """Fills since start_ms. aggregate=True asks HL to COMBINE an order's partial fills (slices) into
    one row per trade — ~100x fewer rows (a sliced wallet: 1852 raw -> 19 aggregated) with all the
    fields we profile on (startPosition/closedPnl/dir/crossed/sz/fee). Trade-level granularity is
    exactly what episode reconstruction wants; we never needed the raw slices to profile a wallet.

    Hyperliquid does not accept a coin or dex selector on ``userFillsByTime`` (only user, time range and
    aggregation).  Callers must apply the executable Crypto/xyz universe immediately to the response.
    """
    return post({"type": "userFillsByTime", "user": addr, "startTime": start_ms,
                 "aggregateByTime": aggregate})


def portfolio(addr: str):
    """HL portfolio: per-window account-value & PnL time series + volume. This is the AUTHORITATIVE
    account-level performance — NET of fees, deposit-adjusted, and (verified) matches on-chain fills to
    the dollar; the leaderboard is a lagging, gross approximation. Returns the raw list of
    [period, {accountValueHistory, pnlHistory, vlm}] (day/week/month/allTime + perp* variants) or None."""
    return post_soft({"type": "portfolio", "user": addr})


def fetch_window(addr: str, start_ms: int, max_pages: int, sleep: float = 0.0):
    """All fills for addr since start_ms, paginated forward. Caps at max_pages
    (order-slicing can explode fill counts). Returns (fills, hit_cap)."""
    out, hit_cap, _cursor = fetch_window_progress(addr, start_ms, max_pages, sleep=sleep)
    return out, hit_cap


def fetch_window_progress(addr: str, start_ms: int, max_pages: int, sleep: float = 0.0):
    """Forward pagination with an explicit continuation cursor for resumable 37-day bootstrap."""
    out, seen, cur = [], set(), int(start_ms)
    for _ in range(max_pages):
        page = user_fills_by_time(addr, cur)
        if not isinstance(page, list) or not page:
            return out, False, cur
        page.sort(key=lambda x: x["time"])
        for x in page:
            if x.get("tid") not in seen:
                seen.add(x.get("tid"))
                out.append(x)
        if len(page) < 2000:
            return out, False, int(page[-1]["time"]) + 1
        cur = page[-1]["time"] + 1
        if sleep:
            time.sleep(sleep)
    return out, True, cur


def clearinghouse_state(addr: str, dex: str = None):
    """Current account state — open positions with leverage {type isolated/cross, value} and
    marginSummary (accountValue, totalNtlPos). Snapshot only (flat wallet -> no positions).
    Pass dex (e.g. 'xyz') for a builder/stock perp dex — the standard call only returns standard-
    perp positions; builder-dex positions need their dex named explicitly."""
    body = {"type": "clearinghouseState", "user": addr}
    if dex:
        body["dex"] = dex
    return post_soft(body)


def spot_clearinghouse_state(addr: str):
    """Spot token balances (for SPOT-HEDGE detection): {balances:[{coin,total,hold,entryNtl}]}. A wallet
    that shorts a perp while holding the same token in spot is hedging — its perp 'profit' is offset by
    spot, so copying the naked perp leg is a losing trade for us. Snapshot only."""
    return post_soft({"type": "spotClearinghouseState", "user": addr})


def candle_snapshot(coin: str, interval: str = "1d", days: int = 30):
    """OHLC candles for coin over the last `days` (for realized-volatility sizing). Returns a list of
    {t,T,s,i,o,c,h,l,v,n} or None. Cheap (weight 2); callers cache + refresh off the signal hot path."""
    now = int(time.time() * 1000)
    return post_soft({"type": "candleSnapshot",
                      "req": {"coin": coin, "interval": interval,
                      "startTime": now - days * 86400_000, "endTime": now}})


def candle_snapshot_range(coin: str, interval: str, start_ms: int, end_ms: int):
    """Fetch one explicit candle range. Hyperliquid exposes at most the latest 5000 candles."""
    return post_soft({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": int(start_ms), "endTime": int(end_ms),
    }})


def asset_contexts(dex: str = None) -> dict:
    """{coin: ctx+universe fields} from metaAndAssetCtxs.

    Standard perps use bare names (BTC, VINE). Builder-dex callers pass dex and receive names exactly as
    Hyperliquid returns them; the observer's low-liquidity gate only applies to standard crypto perps.
    """
    body = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    m = post_soft(body)
    if not (isinstance(m, list) and len(m) == 2 and isinstance(m[0], dict)):
        return {}
    out = {}
    for u, c in zip(m[0].get("universe", []), m[1]):
        name = u.get("name")
        if name:
            row = dict(c or {})
            row.update({f"universe_{k}": v for k, v in (u or {}).items()})
            out[name] = row
    return out


def asset_context(coin: str):
    if not coin or ":" in coin:
        return None
    return asset_contexts().get(coin)


def perp_universe() -> set:
    """Standard crypto perp coin names. These price via WS bbo (subscribing bbo for a name NOT in
    here — builder/stock coin or junk — closes the WS connection, so this guards bbo subs).
    Retries: an empty result here is load-bearing — callers filter copyable fills by it, so a
    transient empty would silently DROP ALL CRYPTO. Retry hard before giving up."""
    for _ in range(6):
        m = post_soft({"type": "meta"})
        if isinstance(m, dict):
            names = {u.get("name") for u in m.get("universe", []) if u.get("name")}
            if names:
                return names
        time.sleep(0.5)
    return set()


# Transparent real-asset builder dexes we copy (stocks/commodities/indices, fully-qualified names
# like 'xyz:AAPL'). Verified 2026-06-25: these price via REST l2Book {"coin":"xyz:AAPL"} and
# allMids {"dex":"xyz"} (WS bbo does NOT serve builder dexes). EXCLUDES vntl (SPACEX/OPENAI/ANTHROPIC
# = private-company synthetics, no transparent market price) and crypto-duplicate dexes (hyna/para).
BUILDER_DEXES = ("xyz",)


def builder_universe(dexes=BUILDER_DEXES) -> set:
    """Copyable builder-perp names (fully-qualified, e.g. 'xyz:AAPL'). RETRIES — a transient empty fetch
    would drop ALL stock/commodity fills from perp_frac and falsely retire every stock trader as
    'spot_dominant', silently shrinking the follow set."""
    for _ in range(4):
        out: set = set()
        for dex in dexes:
            m = post_soft({"type": "meta", "dex": dex})
            if isinstance(m, dict):
                out |= {u.get("name") for u in m.get("universe", []) if u.get("name")}
        if out:
            return out
        time.sleep(0.5)
    return set()


_UNIVERSE_CACHE = {"u": None, "ts": 0.0}   # process-level cache; the tradeable-coin set barely changes
UNIVERSE_CACHE_S = 86400                    # re-fetch at most once/day (new listings are rare)


def copyable_universe(builder_dexes=BUILDER_DEXES, force=False) -> set:
    """Everything we can copy: standard crypto perps ∪ transparent builder perps (stocks/commodities).
    CACHED for UNIVERSE_CACHE_S (the coin set barely changes — no need to re-fetch it every scan; pass
    force=True to refresh). Spot is still excluded upstream (is_spot). HARD-FAILS if EITHER set is empty:
    a partial universe silently drops one whole class of fills and corrupts the sweep. A crypto-less
    universe would drop every crypto trade; a BUILDER-less one falsely retires every stock/commodity
    trader as spot_dominant (was previously mis-labelled "safe to degrade to crypto-only" — it is NOT).
    Aborting (→ daemon retries the scan) beats writing corrupt profiles, and a bad fetch never overwrites
    a good cache."""
    now = time.time()
    if not force and _UNIVERSE_CACHE["u"] is not None and (now - _UNIVERSE_CACHE["ts"]) < UNIVERSE_CACHE_S:
        return _UNIVERSE_CACHE["u"]
    crypto = perp_universe()
    if not crypto:
        raise RuntimeError("perp_universe() empty after retries — refusing a crypto-less universe")
    builder = builder_universe(builder_dexes)
    if builder_dexes and not builder:
        raise RuntimeError("builder_universe() empty after retries — refusing a builder-less universe "
                           "(would falsely retire every stock/commodity trader as spot_dominant)")
    u = crypto | builder
    _UNIVERSE_CACHE["u"], _UNIVERSE_CACHE["ts"] = u, now
    return u


def book_top(coin: str):
    """Best (bid, ask) for ANY coin incl builder/stock perps, via REST l2Book — works where WS bbo
    doesn't (builder dexes). Returns (bid, ask) or None."""
    b = post_soft({"type": "l2Book", "coin": coin})
    lv = b.get("levels") if isinstance(b, dict) else None
    if lv and len(lv) == 2 and lv[0] and lv[1]:
        from hyper.util import f as _f
        return _f(lv[0][0]["px"]), _f(lv[1][0]["px"])
    return None


def realtime_book_top(coin: str, timeout: float = 5.0):
    """Unpaced best bid/ask for one latency-sensitive execution decision."""
    b = realtime_post_soft({"type": "l2Book", "coin": coin}, timeout=timeout)
    lv = b.get("levels") if isinstance(b, dict) else None
    if lv and len(lv) == 2 and lv[0] and lv[1]:
        from hyper.util import f as _f
        bid, ask = _f(lv[0][0]["px"]), _f(lv[1][0]["px"])
        if bid > 0 and ask >= bid:
            return bid, ask
    return None


def book_snapshot(coin: str):
    """Raw aggregated L2 book for risk/microstructure features (weight 2, sampled at radar cadence)."""
    book = post_soft({"type": "l2Book", "coin": coin})
    return book if isinstance(book, dict) else None


def all_mids(dex: str = None, realtime: bool = False) -> dict:
    """Current mid prices. With dex='xyz', keys are fully-qualified builder coins like 'xyz:MU'."""
    body = {"type": "allMids"}
    if dex:
        body["dex"] = dex
    m = realtime_post_soft(body) if realtime else post_soft(body)
    return m if isinstance(m, dict) else {}
