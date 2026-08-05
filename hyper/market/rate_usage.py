"""Process-local rolling Hyperliquid API usage telemetry.

The Observer and Scanner are separate processes, so this module deliberately
does not attempt to enforce one cross-process token bucket.  Each process
records its own traffic and publishes a compact rolling snapshot through the
existing ``process_status.detail_json`` heartbeat.  The Scanner then yields to
the Observer's reported peak before changing its own weighted limiter.
"""

from __future__ import annotations

from collections import deque
import threading
import time


class RollingUsageMeter:
    """Keep bounded request/weight events for the last hour."""

    def __init__(self):
        self._events = deque()
        self._lock = threading.Lock()

    def record(
        self,
        *,
        category: str,
        weight: float,
        requests: int = 1,
        rate_limited: bool = False,
        transport: str = "rest",
        at: float | None = None,
    ) -> None:
        stamp = float(time.time() if at is None else at)
        event = (
            stamp,
            str(category or "other"),
            max(0.0, float(weight or 0.0)),
            max(0, int(requests or 0)),
            bool(rate_limited),
            str(transport or "rest"),
        )
        with self._lock:
            self._events.append(event)
            self._prune_locked(stamp)

    def _prune_locked(self, now: float) -> None:
        cutoff = float(now) - 3600.0
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    @staticmethod
    def _bucket_peak(events, now: float, *, window_s: int = 300,
                     exclude_categories: frozenset[str] = frozenset()) -> float:
        """Maximum weight in any rolling-ish one-minute bucket over ``window_s``.

        Ten-second buckets keep the computation cheap while remaining more
        conservative than a simple five-minute average.
        """
        cutoff = float(now) - float(window_s)
        buckets = {}
        for stamp, category, weight, _requests, _limited, transport in events:
            if stamp < cutoff or transport != "rest" or category in exclude_categories:
                continue
            bucket = int(stamp // 10)
            buckets[bucket] = buckets.get(bucket, 0.0) + float(weight)
        if not buckets:
            return 0.0
        keys = sorted(buckets)
        return max(
            sum(buckets.get(candidate, 0.0) for candidate in range(key - 5, key + 1))
            for key in keys
        )

    def snapshot(self, *, at: float | None = None) -> dict:
        now = float(time.time() if at is None else at)
        with self._lock:
            self._prune_locked(now)
            events = list(self._events)
        last_minute = [event for event in events if event[0] >= now - 60.0]
        rest_minute = [event for event in last_minute if event[5] == "rest"]
        by_category = {}
        for _stamp, category, weight, requests, _limited, transport in rest_minute:
            if transport != "rest":
                continue
            row = by_category.setdefault(category, {"requests1m": 0, "weight1m": 0.0})
            row["requests1m"] += int(requests)
            row["weight1m"] += float(weight)
        limited = [event for event in events if event[4]]
        last_limited = max((event[0] for event in limited), default=None)
        return {
            "observedAt": now,
            "requests1m": sum(int(event[3]) for event in rest_minute),
            "weight1m": round(sum(float(event[2]) for event in rest_minute), 3),
            "weightPeak1mOver5m": round(self._bucket_peak(events, now), 3),
            "nonAuditWeightPeak1mOver5m": round(
                self._bucket_peak(
                    events, now, exclude_categories=frozenset({"account_audit"}),
                ),
                3,
            ),
            "rateLimited1h": len(limited),
            "last429At": last_limited,
            "wsPostRequests1m": sum(
                int(event[3]) for event in last_minute if event[5] == "ws"
            ),
            "byCategory": {
                key: {
                    "requests1m": int(value["requests1m"]),
                    "weight1m": round(float(value["weight1m"]), 3),
                }
                for key, value in sorted(by_category.items())
            },
        }

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


USAGE = RollingUsageMeter()
