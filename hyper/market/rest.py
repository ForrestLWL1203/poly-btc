"""Hyperliquid REST client — leaderboard + per-wallet fills. Throttled & retrying.

Pure data access: no episode/metric logic (that lives in fills.py / metrics.py).
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import json
import threading
import time
import urllib.error
import urllib.request

from hyper import config
from hyper.market.rate_usage import USAGE


_usage_category = contextvars.ContextVar("hyper_rest_usage_category", default=None)
_default_usage_category = ["other"]


@contextmanager
def request_category(category: str):
    token = _usage_category.set(str(category or "other"))
    try:
        yield
    finally:
        _usage_category.reset(token)


def set_default_request_category(category: str) -> None:
    _default_usage_category[0] = str(category or "other")


def _current_usage_category() -> str:
    return str(_usage_category.get() or _default_usage_category[0] or "other")

_last_post = [0.0]
_pace_lock = threading.Lock()   # serialize POST spacing across worker threads (the network call
#                                 itself runs OUTSIDE the lock, so RTTs overlap = real concurrency)
_pace_condition = threading.Condition(_pace_lock)
_WAIT_EPSILON_S = 1e-6
_stats_lock = threading.Lock()
_request_stats = {
    "requests": 0, "retries": 0, "estimated_weight": 0,
    "rate_limited": 0, "budget_wait_s": 0.0,
}
_WEIGHT_ESTIMATE = {
    "userFills": 20, "userFillsByTime": 20, "portfolio": 20,
    "clearinghouseState": 2, "spotClearinghouseState": 2,
    "l2Book": 2, "allMids": 2, "orderStatus": 2, "exchangeStatus": 2,
    # The official schedule classifies all other documented /info calls as weight 20.
    "candleSnapshot": 20, "meta": 20, "metaAndAssetCtxs": 20,
}
_RESULT_WEIGHT_DIVISORS = {
    "userFills": 20, "userFillsByTime": 20, "frontendOpenOrders": 20,
    "candleSnapshot": 60,
}
_budget = {
    "enabled": False,
    "paused": False,
    "weight_per_min": 0.0,
    "burst_weight": 0.0,
    "min_interval": 0.0,
    "tokens": 0.0,
    "updated_at": 0.0,
    "scale": 1.0,
    "successes": 0,
    "cooldown_until": 0.0,
    "mode": "legacy",
    "reason": None,
    "observer_peak_weight": 0.0,
    "metric_started_at": time.monotonic(),
    "metric_updated_at": time.monotonic(),
    "budget_integral": 0.0,
    "accelerated_s": 0.0,
    "paused_s": 0.0,
    "min_weight_per_min": None,
}


def _update_budget_metrics_locked(now: float | None = None) -> None:
    now = float(time.monotonic() if now is None else now)
    elapsed = max(0.0, now - float(_budget["metric_updated_at"]))
    if _budget["enabled"]:
        configured = 0.0 if _budget["paused"] else float(_budget["weight_per_min"])
        _budget["budget_integral"] = float(_budget["budget_integral"]) + configured * elapsed
        if _budget["mode"] == "ws_released" and not _budget["paused"]:
            _budget["accelerated_s"] = float(_budget["accelerated_s"]) + elapsed
        if _budget["paused"]:
            _budget["paused_s"] = float(_budget["paused_s"]) + elapsed
    _budget["metric_updated_at"] = now


def reset_request_stats():
    with _stats_lock:
        _request_stats.update(
            requests=0, retries=0, estimated_weight=0,
            rate_limited=0, budget_wait_s=0.0,
        )
    with _pace_lock:
        now = time.monotonic()
        _budget.update(
            metric_started_at=now,
            metric_updated_at=now,
            budget_integral=0.0,
            accelerated_s=0.0,
            paused_s=0.0,
            min_weight_per_min=(
                0.0 if _budget["paused"] else float(_budget["weight_per_min"])
            ) if _budget["enabled"] else None,
        )


def request_stats():
    with _stats_lock:
        out = dict(_request_stats)
    with _pace_lock:
        now = time.monotonic()
        _update_budget_metrics_locked(now)
        metric_elapsed = max(0.0, now - float(_budget["metric_started_at"]))
        out["budget_enabled"] = bool(_budget["enabled"])
        out["budget_paused"] = bool(_budget["paused"])
        out["budget_scale"] = float(_budget["scale"])
        out["budget_weight_per_min"] = float(_budget["weight_per_min"])
        out["budget_mode"] = str(_budget["mode"])
        out["budget_reason"] = _budget["reason"]
        out["observer_peak_weight"] = float(_budget["observer_peak_weight"])
        out["avg_weight_budget"] = (
            float(_budget["budget_integral"]) / metric_elapsed if metric_elapsed > 0 else 0.0
        )
        out["min_weight_budget"] = float(_budget["min_weight_per_min"] or 0.0)
        out["accelerated_s"] = float(_budget["accelerated_s"])
        out["budget_paused_s"] = float(_budget["paused_s"])
    return out


def configure_post_budget(*, weight_per_min: float | None, burst_weight: float = 20.0,
                          min_interval: float = 0.02, paused: bool = False,
                          mode: str | None = None, reason: str | None = None,
                          observer_peak_weight: float = 0.0) -> None:
    """Switch the process-local POST pacer between weighted scan mode and legacy interval mode.

    Hyperliquid accounts ``/info`` traffic by request weight, so one fixed sleep wastes most of the
    allowance on weight-2 metadata calls while still being too aggressive for weight-20 fill calls.  The
    scanner uses this weighted leaky bucket in both modes: while Observer has work it preserves the
    former weight-20 request allowance, and while Observer is idle it uses the larger scan-only budget.  The
    Observer runs in its own process and retains its independent low-latency request path.
    """
    with _pace_condition:
        now = time.monotonic()
        _update_budget_metrics_locked(now)
        enabled = bool(paused) or (weight_per_min is not None and float(weight_per_min) > 0.0)
        was_enabled = bool(_budget["enabled"])
        if was_enabled:
            old_rate = (
                float(_budget["weight_per_min"]) * max(0.05, float(_budget["scale"])) / 60.0
            )
            elapsed = max(0.0, now - float(_budget["updated_at"]))
            carried_tokens = float(_budget["tokens"]) + elapsed * old_rate
        else:
            carried_tokens = max(0.0, float(burst_weight or 0.0))
        next_burst = max(0.0, float(burst_weight or 0.0))
        next_weight = max(0.0, float(weight_per_min or 0.0))
        effective_budget = 0.0 if paused else next_weight
        current_min = _budget["min_weight_per_min"]
        _budget.update(
            enabled=enabled,
            paused=bool(paused),
            weight_per_min=next_weight,
            burst_weight=next_burst,
            min_interval=max(0.0, float(min_interval or 0.0)),
            # A live budget adjustment must never mint a fresh burst. Preserve
            # the previous balance/debt and only clamp it to the new capacity.
            tokens=min(next_burst, carried_tokens) if enabled else 0.0,
            updated_at=now,
            scale=1.0,
            successes=0,
            cooldown_until=0.0,
            mode=str(mode or ("paused" if paused else "weighted" if enabled else "legacy")),
            reason=(str(reason) if reason else None),
            observer_peak_weight=max(0.0, float(observer_peak_weight or 0.0)),
            min_weight_per_min=(
                effective_budget if current_min is None else min(float(current_min), effective_budget)
            ) if enabled else current_min,
        )
        if not was_enabled:
            _last_post[0] = 0.0
        _pace_condition.notify_all()


def _reserve_post(weight: float) -> float:
    """Reserve one weighted request slot and return the sleep required before sending it."""
    total_wait = 0.0
    required = max(1.0, float(weight))
    with _pace_condition:
        while True:
            now = time.monotonic()
            if _budget["paused"]:
                wait_started = now
                _pace_condition.wait(timeout=5.0)
                total_wait += max(0.0, time.monotonic() - wait_started)
                continue
            if not _budget["enabled"]:
                wait = max(0.0, float(config.MIN_POST_INTERVAL) - (time.time() - _last_post[0]))
                if wait > 0.0:
                    wait_started = now
                    _pace_condition.wait(timeout=wait)
                    total_wait += max(0.0, time.monotonic() - wait_started)
                    continue
                _last_post[0] = time.time()
                break

            rate_per_s = (
                float(_budget["weight_per_min"]) * max(0.05, float(_budget["scale"])) / 60.0
            )
            elapsed = max(0.0, now - float(_budget["updated_at"]))
            tokens = min(
                float(_budget["burst_weight"]),
                float(_budget["tokens"]) + elapsed * rate_per_s,
            )
            token_wait = max(0.0, required - tokens) / max(rate_per_s, 1e-9)
            interval_wait = max(
                0.0,
                float(_budget["min_interval"]) - (time.time() - _last_post[0]),
            )
            wait = max(token_wait, interval_wait, float(_budget["cooldown_until"]) - now)
            # Token arithmetic can leave a sub-microsecond residue that the
            # platform clock (or a deterministic test clock) cannot advance
            # through. Treat it as settled instead of spinning forever.
            if wait > _WAIT_EPSILON_S:
                wait_started = now
                _pace_condition.wait(timeout=wait)
                total_wait += max(0.0, time.monotonic() - wait_started)
                continue
            _budget["tokens"] = tokens - required
            _budget["updated_at"] = now
            _last_post[0] = time.time()
            break
    if total_wait > 0.0:
        with _stats_lock:
            _request_stats["budget_wait_s"] += total_wait
    return total_wait


def _rate_limit_feedback(*, limited: bool) -> None:
    """Back off sharply on 429 and recover conservatively after sustained successful requests."""
    with _pace_lock:
        if not _budget["enabled"]:
            return
        if limited:
            _budget["scale"] = max(0.25, float(_budget["scale"]) * 0.70)
            _budget["successes"] = 0
            _budget["cooldown_until"] = max(
                float(_budget["cooldown_until"]), time.monotonic() + 2.0,
            )
            if _current_usage_category() == "scanner":
                _update_budget_metrics_locked()
                _budget["paused"] = True
                _budget["mode"] = "rate_limit_pause"
                _budget["reason"] = "recent_429_pause"
            return
        _budget["successes"] = int(_budget["successes"]) + 1
        if _budget["successes"] >= 20 and float(_budget["scale"]) < 1.0:
            _budget["scale"] = min(1.0, float(_budget["scale"]) + 0.05)
            _budget["successes"] = 0


def _charge_result_weight(body: dict, result) -> int:
    """Charge response-sized weight after the server reveals the returned row count."""
    divisor = _RESULT_WEIGHT_DIVISORS.get(body.get("type"))
    if divisor is None or not isinstance(result, list):
        return 0
    extra = (len(result) + divisor - 1) // divisor
    if extra <= 0:
        return 0
    with _stats_lock:
        _request_stats["estimated_weight"] += extra
    USAGE.record(category=_current_usage_category(), weight=extra, requests=0)
    with _pace_lock:
        if _budget["enabled"]:
            # A negative balance is deliberate debt.  The next reservation waits for both its own request
            # and the extra response-sized weight consumed by this result.
            _budget["tokens"] = float(_budget["tokens"]) - float(extra)
    return extra


def _get(url: str, retries: int = 3):
    err = None
    for attempt in range(retries):
        with _stats_lock:
            _request_stats["requests"] += 1
            _request_stats["estimated_weight"] += 1
            if attempt:
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
    weight = _WEIGHT_ESTIMATE.get(body.get("type"), 1)
    for attempt in range(retries):
        with _stats_lock:
            _request_stats["requests"] += 1
            _request_stats["estimated_weight"] += weight
            if attempt:
                _request_stats["retries"] += 1
        _reserve_post(weight)
        USAGE.record(category=_current_usage_category(), weight=weight, requests=1)
        try:                                               # ... the request below runs concurrently
            req = urllib.request.Request(config.INFO_URL, data=data, headers=config.UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                result = json.loads(r.read().decode())
                _charge_result_weight(body, result)
                _rate_limit_feedback(limited=False)
                return result
        except urllib.error.HTTPError as exc:
            err = exc
            if exc.code == 429:
                with _stats_lock:
                    _request_stats["rate_limited"] += 1
                _rate_limit_feedback(limited=True)
                USAGE.record(
                    category=_current_usage_category(), weight=0, requests=0,
                    rate_limited=True,
                )
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
    weight = _WEIGHT_ESTIMATE.get(body.get("type"), 1)
    with _stats_lock:
        _request_stats["requests"] += 1
        _request_stats["estimated_weight"] += weight
    USAGE.record(category=_current_usage_category(), weight=weight, requests=1)
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(config.INFO_URL, data=data, headers=config.UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read().decode())
            _charge_result_weight(body, result)
            return result
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            with _stats_lock:
                _request_stats["rate_limited"] += 1
            USAGE.record(
                category=_current_usage_category(), weight=0, requests=0, rate_limited=True,
            )
        return None
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
    {t,T,s,i,o,c,h,l,v,n} or None. Callers cache + refresh this weight-20 request off the signal hot path."""
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


def asset_contexts(dex: str = None, realtime: bool = False) -> dict:
    """{coin: ctx+universe fields} from metaAndAssetCtxs.

    Standard perps use bare names (BTC, VINE). Builder-dex callers pass dex and receive names exactly as
    Hyperliquid returns them; the observer's low-liquidity gate only applies to standard crypto perps.
    Realtime mode bypasses the historical-fill pacer for latency-sensitive official mark polling.
    """
    body = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    m = realtime_post_soft(body) if realtime else post_soft(body)
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
    """Standard crypto perp coin names. Builder/HIP-3 names are classified separately even though both
    groups support the same public WS BBO and activeAssetCtx subscriptions.
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
# like 'xyz:AAPL'). Verified 2026-08-04: these support public WS BBO/activeAssetCtx using the fully-qualified
# coin name; REST l2Book remains an execution-time read only. EXCLUDES vntl (SPACEX/OPENAI/ANTHROPIC
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
    """On-demand best (bid, ask) for any standard or builder perp via REST l2Book."""
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


def realtime_book_snapshot(coin: str, timeout: float = 2.0):
    """Unpaced aggregated L2 snapshot for one latency-sensitive sizing decision."""
    book = realtime_post_soft({"type": "l2Book", "coin": coin}, timeout=timeout)
    return book if isinstance(book, dict) else None


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
