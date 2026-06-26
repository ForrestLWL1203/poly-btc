"""Hyperliquid REST client — leaderboard + per-wallet fills. Throttled & retrying.

Pure data access: no episode/metric logic (that lives in fills.py / metrics.py).
"""
import json
import threading
import time
import urllib.error
import urllib.request

from . import config

_last_post = [0.0]
_pace_lock = threading.Lock()   # serialize POST spacing across worker threads (the network call
#                                 itself runs OUTSIDE the lock, so RTTs overlap = real concurrency)


def _get(url: str, retries: int = 3):
    err = None
    for attempt in range(retries):
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
    for attempt in range(retries):
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


# -- higher-level reads -------------------------------------------------------
def get_leaderboard() -> list:
    data = _get(config.LEADERBOARD_URL)
    return data["leaderboardRows"] if isinstance(data, dict) else data


def user_fills_by_time(addr: str, start_ms: int, aggregate: bool = True):
    """Fills since start_ms. aggregate=True asks HL to COMBINE an order's partial fills (slices) into
    one row per trade — ~100x fewer rows (a sliced wallet: 1852 raw -> 19 aggregated) with all the
    fields we profile on (startPosition/closedPnl/dir/crossed/sz/fee). Trade-level granularity is
    exactly what episode reconstruction wants; we never needed the raw slices to profile a wallet."""
    return post({"type": "userFillsByTime", "user": addr, "startTime": start_ms,
                 "aggregateByTime": aggregate})


def user_fills_latest(addr: str, aggregate: bool = True):
    """Most recent ~2000 fills (1 call) — for the cheap pre-screen. Aggregated so the 2000-cap
    covers many more trades (recency check isn't fooled by one heavily-sliced order)."""
    return post({"type": "userFills", "user": addr, "aggregateByTime": aggregate})


def fetch_window(addr: str, start_ms: int, max_pages: int, sleep: float = 0.0):
    """All fills for addr since start_ms, paginated forward. Caps at max_pages
    (order-slicing can explode fill counts). Returns (fills, hit_cap)."""
    out, seen, cur = [], set(), start_ms
    for _ in range(max_pages):
        page = user_fills_by_time(addr, cur)
        if not isinstance(page, list) or not page:
            return out, False
        page.sort(key=lambda x: x["time"])
        for x in page:
            if x.get("tid") not in seen:
                seen.add(x.get("tid"))
                out.append(x)
        if len(page) < 2000:
            return out, False
        cur = page[-1]["time"] + 1
        if sleep:
            time.sleep(sleep)
    return out, True



def clearinghouse_state(addr: str, dex: str = None):
    """Current account state — open positions with leverage {type isolated/cross, value} and
    marginSummary (accountValue, totalNtlPos). Snapshot only (flat wallet -> no positions).
    Pass dex (e.g. 'xyz') for a builder/stock perp dex — the standard call only returns standard-
    perp positions; builder-dex positions need their dex named explicitly."""
    body = {"type": "clearinghouseState", "user": addr}
    if dex:
        body["dex"] = dex
    return post_soft(body)


def candle_snapshot(coin: str, interval: str = "1d", days: int = 30):
    """OHLC candles for coin over the last `days` (for realized-volatility sizing). Returns a list of
    {t,T,s,i,o,c,h,l,v,n} or None. Cheap (weight 2); callers cache + refresh off the signal hot path."""
    now = int(time.time() * 1000)
    return post_soft({"type": "candleSnapshot",
                      "req": {"coin": coin, "interval": interval,
                              "startTime": now - days * 86400_000, "endTime": now}})


def asset_volumes(dex: str = None) -> dict:
    """{coin: 24h notional volume} from metaAndAssetCtxs (optionally a builder dex). Used to pick the
    most-traded coins to pre-warm σ for — the names match candleSnapshot + fill coin names exactly."""
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
            try:
                out[name] = float((c or {}).get("dayNtlVlm") or 0)
            except (TypeError, ValueError):
                out[name] = 0.0
    return out


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
    """Copyable builder-perp names (fully-qualified, e.g. 'xyz:AAPL') for the given transparent dexes."""
    out: set = set()
    for dex in dexes:
        m = post_soft({"type": "meta", "dex": dex})
        if isinstance(m, dict):
            out |= {u.get("name") for u in m.get("universe", []) if u.get("name")}
    return out


def copyable_universe(builder_dexes=BUILDER_DEXES) -> set:
    """Everything we can copy: standard crypto perps ∪ transparent builder perps (stocks/commodities).
    Spot is still excluded upstream (is_spot). HARD-FAILS if the crypto set is empty: a partial
    (stock-only) universe would silently drop every crypto trade and corrupt the whole sweep — far
    worse than aborting. (A failed builder fetch just degrades to crypto-only, which is safe.)"""
    crypto = perp_universe()
    if not crypto:
        raise RuntimeError("perp_universe() empty after retries — refusing a crypto-less universe")
    return crypto | builder_universe(builder_dexes)


def book_top(coin: str):
    """Best (bid, ask) for ANY coin incl builder/stock perps, via REST l2Book — works where WS bbo
    doesn't (builder dexes). Returns (bid, ask) or None."""
    b = post_soft({"type": "l2Book", "coin": coin})
    lv = b.get("levels") if isinstance(b, dict) else None
    if lv and len(lv) == 2 and lv[0] and lv[1]:
        from .util import f as _f
        return _f(lv[0][0]["px"]), _f(lv[1][0]["px"])
    return None
