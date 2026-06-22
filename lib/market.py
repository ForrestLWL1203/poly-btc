from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

UTC = dt.timezone.utc
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


@dataclass(frozen=True)
class MarketSeries:
    symbol: str
    slug_prefix: str
    slug_step: int = 300

    @classmethod
    def from_symbol(cls, symbol: str) -> "MarketSeries":
        upper = symbol.upper()
        if upper not in {"BTC", "ETH"}:
            raise ValueError(f"unsupported symbol: {symbol}")
        return cls(symbol=upper, slug_prefix=f"{upper.lower()}-updown-5m")

    def epoch_to_slug(self, epoch: int) -> str:
        return f"{self.slug_prefix}-{int(epoch)}"


@dataclass(frozen=True)
class MarketWindow:
    symbol: str
    slug: str
    condition_id: str
    question: str
    up_token: str
    down_token: str
    start_time: dt.datetime
    end_time: dt.datetime

    @property
    def start_epoch(self) -> int:
        return int(self.start_time.timestamp())

    @property
    def end_epoch(self) -> int:
        return int(self.end_time.timestamp())


def current_epoch_start(now: dt.datetime | None = None, step: int = 300) -> int:
    value = now or dt.datetime.now(UTC)
    return int(value.timestamp()) // step * step


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_tokens(raw: Any) -> list[str]:
    if isinstance(raw, str):
        value = json.loads(raw)
    else:
        value = raw
    return [str(item) for item in value or []]


def build_window(raw: dict[str, Any], series: MarketSeries) -> MarketWindow | None:
    tokens = parse_tokens(raw.get("clobTokenIds", []))
    if len(tokens) < 2:
        return None
    end_time = parse_datetime(raw.get("endDate"))
    if end_time is None:
        return None
    start_time = parse_datetime(raw.get("eventStartTime")) or end_time - dt.timedelta(seconds=series.slug_step)
    return MarketWindow(
        symbol=series.symbol,
        slug=str(raw.get("slug") or ""),
        condition_id=str(raw.get("conditionId") or ""),
        question=str(raw.get("question") or ""),
        up_token=tokens[0],
        down_token=tokens[1],
        start_time=start_time,
        end_time=end_time,
    )


def fetch_market_by_slug(slug: str, timeout: float = 10.0) -> dict[str, Any] | None:
    url = GAMMA_MARKETS_URL + "?" + urllib.parse.urlencode({"slug": slug})
    req = urllib.request.Request(url, headers={"User-Agent": "poly-monitor/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("slug") == slug:
                return item
    return None


def find_current_or_next_window(series: MarketSeries, *, now: dt.datetime | None = None, scan: int = 4) -> MarketWindow | None:
    base = current_epoch_start(now, series.slug_step)
    now_dt = now or dt.datetime.now(UTC)
    for offset in range(scan):
        slug = series.epoch_to_slug(base + offset * series.slug_step)
        raw = fetch_market_by_slug(slug)
        if raw is None or raw.get("closed"):
            continue
        window = build_window(raw, series)
        if window is not None and window.end_time > now_dt:
            return window
    return None
