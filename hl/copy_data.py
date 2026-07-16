"""Canonical copy-replay fill filtering, normalization, and loading.

Every historical-copy consumer must use the same market universe and event
ordering.  Plain symbols are standard perpetuals; only the transparent
``xyz:*`` builder namespace is copyable.  Spot and opaque/private builder
symbols are deliberately excluded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from .fills import is_spot


def canonical_coin(coin) -> str:
    text = str(coin or "").strip()
    if text.lower().startswith("xyz:"):
        return "xyz:" + text.split(":", 1)[1]
    return text


def is_copyable_coin(coin, universe: Iterable[str] | None = None) -> bool:
    coin = canonical_coin(coin)
    # ``#<id>`` rows are binary settlement/outcome markets.  They disappear from allMids after settling
    # and the live Observer universe cannot originate them.  Replaying them as plain perps both fabricates
    # copyability and leaves zero-price Settlement rows as phantom open positions.
    if not coin or is_spot(coin) or coin.startswith("#"):
        return False
    # A colon outside the public xyz namespace identifies an opaque/private
    # builder dex.  It cannot be priced or copied safely by the live engine.
    if ":" in coin and not coin.lower().startswith("xyz:"):
        return False
    if universe:
        allowed = {canonical_coin(item).lower() for item in universe if item}
        if coin.lower() not in allowed:
            return False
    return True


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _sortable_id(value) -> tuple[int, object]:
    if value is None:
        return (2, "")
    try:
        return (0, int(value))
    except (TypeError, ValueError, OverflowError):
        return (1, str(value))


def fill_order_key(fill: Mapping) -> tuple:
    """Canonical deterministic replay order: ``(time, addr, tid)``."""
    return (
        _int(fill.get("time") if fill.get("time") is not None else fill.get("T")),
        str(fill.get("user") or fill.get("addr") or "").lower(),
        _sortable_id(fill.get("tid")),
    )


def normalize_copyable_fill(fill: Mapping | None, *, addr: str | None = None,
                            universe: Iterable[str] | None = None) -> dict | None:
    if not isinstance(fill, Mapping):
        return None
    coin = canonical_coin(fill.get("coin"))
    if not is_copyable_coin(coin, universe=universe):
        return None
    timestamp = _int(fill.get("time") if fill.get("time") is not None else fill.get("T"))
    if timestamp < 0:
        return None
    out = dict(fill)
    out["coin"] = coin
    out["time"] = timestamp
    owner = (addr or out.get("user") or out.get("addr") or "").lower()
    if owner:
        out["user"] = owner
    return out


def normalize_copyable_fills(
    fills: Iterable[Mapping] | None,
    *,
    addr: str | None = None,
    universe: Iterable[str] | None = None,
    policies: Mapping[str, object] | None = None,
    policy_default: bool = True,
) -> list[dict]:
    """Return one canonical, policy-filtered, deterministically sorted list."""
    out = []
    if policies is not None:
        # Local import avoids making the lower-level normalizer depend on the
        # sector policy module during import initialization.
        from .sector import policy_allows_coin
    for raw in fills or []:
        item = normalize_copyable_fill(raw, addr=addr, universe=universe)
        if item is None:
            continue
        if policies is not None:
            owner = (item.get("user") or addr or "").lower()
            if not policy_allows_coin(policies.get(owner), item["coin"], default=policy_default):
                continue
        out.append(item)
    out.sort(key=fill_order_key)
    return out


def market_evidence_key(fills: Iterable[Mapping] | None) -> str:
    """Hash market observations only, independent of copy-strategy parameters."""
    digest = hashlib.sha256()
    for row in normalize_copyable_fills(fills):
        evidence = [
            row.get("time"),
            (row.get("user") or "").lower(),
            row.get("tid"),
            row.get("coin"),
            row.get("side"),
            row.get("sz"),
            row.get("startPosition"),
            row.get("px"),
            row.get("oid"),
        ]
        digest.update(json.dumps(evidence, separators=(",", ":"), default=str).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()[:24]


def load_copyable_fills(
    db,
    addrs: Iterable[str],
    start_ms: int,
    *,
    policies: Mapping[str, object] | None = None,
    universe: Iterable[str] | None = None,
    policy_default: bool = True,
) -> list[dict]:
    """Load cached fills once using the same rules as all replay consumers."""
    owners = sorted({(addr or "").lower() for addr in addrs if addr})
    if not owners:
        return []
    placeholders = ",".join("?" for _ in owners)
    rows = db.execute(
        f"SELECT addr,fill_json FROM candidate_fills "
        f"WHERE lower(addr) IN ({placeholders}) AND time>=? ORDER BY time,lower(addr),tid",
        (*owners, int(start_ms or 0)),
    ).fetchall()
    raw = []
    for row in rows:
        owner = row[0] if not hasattr(row, "keys") else row["addr"]
        payload = row[1] if not hasattr(row, "keys") else row["fill_json"]
        try:
            fill = json.loads(payload)
        except (TypeError, ValueError):
            continue
        item = normalize_copyable_fill(fill, addr=owner, universe=universe)
        if item is not None:
            raw.append(item)
    return normalize_copyable_fills(
        raw,
        universe=universe,
        policies=policies,
        policy_default=policy_default,
    )
