"""Read-only parity check between official and QuickNode collection sources.

The report deliberately contains only aggregate counts. Core addresses and the
QuickNode endpoint remain process-private and are never printed.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

from hyper import config, storage
from hyper.market.collection_runtime import read_quicknode_endpoint
from hyper.selection.state import published_core_addrs


class VerificationError(RuntimeError):
    """Sanitized parity failure safe to print in an operational log."""


def _error_code(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{int(exc.code)}"
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return "unavailable"
    if isinstance(exc, (UnicodeDecodeError, json.JSONDecodeError)):
        return "invalid_json"
    return "request_failed"


def _post(url: str, body: dict, *, timeout: float = 30.0):
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers=config.UA,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - only fixed error codes leave this boundary
        raise VerificationError(_error_code(exc)) from None
    if not isinstance(payload, (dict, list)) or (
        isinstance(payload, dict) and payload.get("error")
    ):
        raise VerificationError("invalid_response")
    return payload


class _Clients:
    def __init__(self, quicknode_endpoint: str):
        self.quicknode_endpoint = quicknode_endpoint
        self._official_last = 0.0
        self._quicknode_last = 0.0

    @staticmethod
    def _pace(last: float, interval: float) -> float:
        wait = max(0.0, interval - (time.monotonic() - last))
        if wait:
            time.sleep(wait)
        return time.monotonic()

    def official(self, body: dict):
        weight = 2 if str(body.get("type") or "") in {
            "clearinghouseState", "spotClearinghouseState", "allMids",
        } else 20
        self._official_last = self._pace(self._official_last, 1.25 if weight == 20 else 0.15)
        return _post(config.INFO_URL, body)

    def quicknode(self, body: dict):
        self._quicknode_last = self._pace(self._quicknode_last, 0.1)
        return _post(self.quicknode_endpoint, body)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _portfolio_compare(official, quicknode) -> tuple[bool, int, int]:
    """Compare stable portfolio history while excluding each live tail point.

    Hyperliquid appends a mark-to-market point generated at request time to
    every history. Two sequential requests can therefore be correct while
    their final timestamp/value differs. Period membership, non-history fields
    (including volume), history length, and every earlier point remain strict.
    """
    try:
        official_periods = {str(period): details for period, details in official}
        quicknode_periods = {str(period): details for period, details in quicknode}
    except (TypeError, ValueError):
        raise VerificationError("portfolio_invalid") from None
    if set(official_periods) != set(quicknode_periods):
        return False, 0, 0
    stable_points = 0
    ignored_tails = 0
    for period in sorted(official_periods):
        left, right = official_periods[period], quicknode_periods[period]
        if not isinstance(left, dict) or not isinstance(right, dict):
            raise VerificationError("portfolio_invalid")
        left_other = {key: value for key, value in left.items() if key not in {
            "accountValueHistory", "pnlHistory",
        }}
        right_other = {key: value for key, value in right.items() if key not in {
            "accountValueHistory", "pnlHistory",
        }}
        if _canonical(left_other) != _canonical(right_other):
            return False, stable_points, ignored_tails
        for key in ("accountValueHistory", "pnlHistory"):
            left_history, right_history = left.get(key), right.get(key)
            if not isinstance(left_history, list) or not isinstance(right_history, list):
                raise VerificationError("portfolio_invalid")
            if abs(len(left_history) - len(right_history)) > 1:
                return False, stable_points, ignored_tails
            left_stable = left_history[:-1] if left_history else []
            right_stable = right_history[:-1] if right_history else []
            if _canonical(left_stable) != _canonical(right_stable):
                return False, stable_points, ignored_tails
            stable_points += len(left_stable)
            ignored_tails += int(bool(left_history) or bool(right_history))
    return True, stable_points, ignored_tails


def _meta_signature(payload) -> list:
    universe = payload.get("universe") if isinstance(payload, dict) else None
    if not isinstance(universe, list):
        raise VerificationError("meta_invalid")
    fields = ("name", "szDecimals", "maxLeverage", "onlyIsolated", "isDelisted")
    return [tuple(item.get(field) for field in fields) for item in universe if isinstance(item, dict)]


def _perp_state_signature(payload) -> dict:
    if not isinstance(payload, dict):
        raise VerificationError("clearinghouse_state_invalid")
    positions = {}
    for wrapper in payload.get("assetPositions") or []:
        position = wrapper.get("position") if isinstance(wrapper, dict) else None
        if not isinstance(position, dict) or not position.get("coin"):
            continue
        positions[str(position["coin"])] = {
            key: position.get(key)
            for key in ("szi", "entryPx", "leverage", "liquidationPx", "cumFunding")
        }
    return positions


def _spot_state_signature(payload) -> dict:
    if not isinstance(payload, dict):
        raise VerificationError("spot_state_invalid")
    return {
        str(item.get("coin")): {
            key: item.get(key) for key in ("total", "hold", "entryNtl")
        }
        for item in (payload.get("balances") or [])
        if isinstance(item, dict) and item.get("coin")
    }


_FILL_FIELDS = (
    "coin", "side", "px", "sz", "time", "startPosition", "dir", "closedPnl",
    "fee", "feeToken", "crossed", "hash", "oid",
)


def _fill_map(rows: list) -> dict:
    out = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("tid") is None:
            continue
        out[str(row["tid"])] = tuple(row.get(field) for field in _FILL_FIELDS)
    return out


def _fetch_fills(client, addr: str, start_ms: int, *, max_pages: int = 32) -> list:
    rows, seen, cursor = [], set(), int(start_ms)
    for _ in range(max_pages):
        page = client({
            "type": "userFillsByTime",
            "user": addr,
            "startTime": cursor,
            "aggregateByTime": True,
        })
        if not isinstance(page, list):
            raise VerificationError("fills_invalid")
        page.sort(key=lambda item: int(item.get("time") or 0))
        for item in page:
            tid = str(item.get("tid"))
            if tid not in seen:
                seen.add(tid)
                rows.append(item)
        if len(page) < 2_000:
            return rows
        cursor = int(page[-1].get("time") or cursor) + 1
    raise VerificationError("fills_page_cap")


def run_parity(db_path: str, *, days: int = 37, expected_core_count: int = 5) -> dict:
    endpoint = read_quicknode_endpoint()
    if not endpoint:
        raise VerificationError("quicknode_not_configured")
    db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    try:
        addrs = published_core_addrs(db)
    finally:
        db.close()
    if addrs is None:
        raise VerificationError("published_generation_missing")
    if len(addrs) != int(expected_core_count):
        raise VerificationError("unexpected_core_count")

    clients = _Clients(endpoint)
    report = {
        "ok": True,
        "coreCount": len(addrs),
        "meta": {"matched": False},
        "portfolio": {
            "matchedWallets": 0,
            "checkedWallets": len(addrs),
            "stableHistoryPoints": 0,
            "liveTailPairsIgnored": 0,
        },
        "fills": {
            "matchedWallets": 0,
            "checkedWallets": len(addrs),
            "officialRows": 0,
            "quicknodeRows": 0,
            "missingTids": 0,
            "extraTids": 0,
            "fieldMismatches": 0,
        },
        "perpState": {"matchedWallets": 0, "checkedWallets": len(addrs)},
        "spotState": {"matchedWallets": 0, "checkedWallets": len(addrs)},
    }

    official_meta = clients.official({"type": "meta"})
    quicknode_meta = clients.quicknode({"type": "meta"})
    report["meta"]["matched"] = _meta_signature(official_meta) == _meta_signature(quicknode_meta)

    start_ms = int((time.time() - max(1, int(days)) * 86_400) * 1_000)
    for addr in addrs:
        official_portfolio = clients.official({"type": "portfolio", "user": addr})
        quicknode_portfolio = clients.quicknode({"type": "portfolio", "user": addr})
        portfolio_match, stable_points, ignored_tails = _portfolio_compare(
            official_portfolio, quicknode_portfolio,
        )
        report["portfolio"]["stableHistoryPoints"] += stable_points
        report["portfolio"]["liveTailPairsIgnored"] += ignored_tails
        if portfolio_match:
            report["portfolio"]["matchedWallets"] += 1

        official_fills = _fetch_fills(clients.official, addr, start_ms)
        quicknode_fills = _fetch_fills(clients.quicknode, addr, start_ms)
        official_map, quicknode_map = _fill_map(official_fills), _fill_map(quicknode_fills)
        official_tids, quicknode_tids = set(official_map), set(quicknode_map)
        missing = official_tids - quicknode_tids
        extra = quicknode_tids - official_tids
        mismatches = sum(
            official_map[tid] != quicknode_map[tid]
            for tid in official_tids & quicknode_tids
        )
        report["fills"]["officialRows"] += len(official_map)
        report["fills"]["quicknodeRows"] += len(quicknode_map)
        report["fills"]["missingTids"] += len(missing)
        report["fills"]["extraTids"] += len(extra)
        report["fills"]["fieldMismatches"] += mismatches
        if not missing and not extra and not mismatches:
            report["fills"]["matchedWallets"] += 1

        official_perp = clients.official({"type": "clearinghouseState", "user": addr})
        quicknode_perp = clients.quicknode({"type": "clearinghouseState", "user": addr})
        if _perp_state_signature(official_perp) == _perp_state_signature(quicknode_perp):
            report["perpState"]["matchedWallets"] += 1

        official_spot = clients.official({"type": "spotClearinghouseState", "user": addr})
        quicknode_spot = clients.quicknode({"type": "spotClearinghouseState", "user": addr})
        if _spot_state_signature(official_spot) == _spot_state_signature(quicknode_spot):
            report["spotState"]["matchedWallets"] += 1

    report["ok"] = bool(
        report["meta"]["matched"]
        and report["portfolio"]["matchedWallets"] == len(addrs)
        and report["fills"]["matchedWallets"] == len(addrs)
        and report["perpState"]["matchedWallets"] == len(addrs)
        and report["spotState"]["matchedWallets"] == len(addrs)
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare official and QuickNode collection data")
    parser.add_argument("--db", default=config.DEFAULT_DB)
    parser.add_argument("--days", type=int, default=37)
    parser.add_argument("--expected-core-count", type=int, default=5)
    args = parser.parse_args()
    try:
        report = run_parity(
            args.db,
            days=args.days,
            expected_core_count=args.expected_core_count,
        )
    except VerificationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
