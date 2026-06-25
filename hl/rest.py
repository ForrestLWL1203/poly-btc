"""Hyperliquid REST client — leaderboard + per-wallet fills. Throttled & retrying.

Pure data access: no episode/metric logic (that lives in fills.py / metrics.py).
"""
import json
import time
import urllib.error
import urllib.request

from . import config

_last_post = [0.0]


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
        wait = config.MIN_POST_INTERVAL - (time.time() - _last_post[0])
        if wait > 0:
            time.sleep(wait)
        _last_post[0] = time.time()
        try:
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


def user_fills_by_time(addr: str, start_ms: int):
    return post({"type": "userFillsByTime", "user": addr, "startTime": start_ms})


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


def account_birth_ms(addr: str):
    """First-ever fill time (address age)."""
    page = user_fills_by_time(addr, 0)
    if isinstance(page, list) and page:
        return min(x["time"] for x in page)
    return None


def clearinghouse_state(addr: str):
    """Current account state — open positions with leverage {type isolated/cross, value} and
    marginSummary (accountValue, totalNtlPos). Snapshot only (flat wallet -> no positions)."""
    return post_soft({"type": "clearinghouseState", "user": addr})


def perp_universe() -> set:
    """Valid perp coin names (for the standard dex). Used to guard bbo subscriptions —
    subscribing bbo for an unknown coin name closes the WS connection."""
    m = post_soft({"type": "meta"})
    if isinstance(m, dict):
        return {u.get("name") for u in m.get("universe", []) if u.get("name")}
    return set()
