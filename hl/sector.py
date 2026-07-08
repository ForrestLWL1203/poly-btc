"""Market-sector helpers for copyability decisions.

Hyperliquid copy targets can be good at crypto while bleeding on transparent
builder stock/index perps, or vice versa. The scanner therefore records a
per-wallet sector policy that the observer can enforce per fill.
"""

from __future__ import annotations

import json
import math
from typing import Mapping

from . import config

SECTORS = ("crypto", "stock")


def classify_coin(coin: str | None) -> str:
    text = str(coin or "").strip()
    return "stock" if text.lower().startswith("xyz:") else "crypto"


def filter_fills(fills: list[dict], sector: str) -> list[dict]:
    return [x for x in fills or [] if classify_coin(x.get("coin")) == sector]


def parse_json_obj(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def policy_allows_coin(policy, coin: str | None, default: bool = True) -> bool:
    policy = parse_json_obj(policy)
    if not policy:
        return bool(default)
    sector = classify_coin(coin)
    item = policy.get(sector)
    if not isinstance(item, dict) or "allow" not in item:
        return bool(default)
    return bool(item.get("allow"))


def _num(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        out = float(v)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except (TypeError, ValueError):
        return default


def _int(v, default: int = 0) -> int:
    return int(_num(v, default))


def _min_closed_for_days(days: int) -> int:
    if int(days) <= 7:
        return int(getattr(config, "SECTOR_COPY_MIN_CLOSED_7D", config.COPY_BT_MIN_CLOSED_7D))
    if int(days) <= 14:
        return int(getattr(config, "SECTOR_COPY_MIN_CLOSED_14D", config.COPY_BT_MIN_CLOSED_14D))
    return int(getattr(config, "SECTOR_COPY_MIN_CLOSED_30D", config.COPY_BT_MIN_CLOSED))


def _window_result(windows: Mapping, days: int) -> dict:
    return dict(windows.get(days) or windows.get(str(days)) or {})


def _compact_result(result: Mapping) -> dict:
    keys = (
        "copy_net_pnl", "closed_n", "wins", "liquidations", "fee_drag",
        "target_open_events", "opened_n", "open_fill_rate", "capacity_open_fit",
        "target_adds", "followed_adds", "missed_adds",
    )
    return {k: result.get(k) for k in keys if k in result}


def compact_sector_results(sector_results: Mapping) -> dict:
    out = {}
    for sector in SECTORS:
        windows = sector_results.get(sector) or {}
        out[sector] = {str(days): _compact_result(result) for days, result in windows.items() if result}
    return out


def evaluate_sector_policy(sector_results: Mapping, min_net: float | None = None) -> dict:
    min_net = float(config.COPY_BT_MIN_NET_PNL if min_net is None else min_net)
    policy = {}
    allowed = []
    for sector in SECTORS:
        windows = sector_results.get(sector) or {}
        enough = {}
        pnl = {}
        closed = {}
        for days in (30, 14, 7):
            result = _window_result(windows, days)
            closed[days] = _int(result.get("closed_n"))
            pnl[days] = _num(result.get("copy_net_pnl"))
            enough[days] = closed[days] >= _min_closed_for_days(days)

        enough_days = [days for days in (30, 14, 7) if enough[days]]
        if not enough_days:
            item = {
                "allow": False,
                "status": "thin_evidence",
                "reason": "板块copy样本不足",
                "closed": {str(k): closed[k] for k in (30, 14, 7)},
                "pnl": {str(k): pnl[k] for k in (30, 14, 7)},
            }
        elif any(enough[days] and pnl[days] <= min_net for days in (14, 7)):
            item = {
                "allow": False,
                "status": "recent_loss",
                "reason": "板块近期copy亏损",
                "closed": {str(k): closed[k] for k in (30, 14, 7)},
                "pnl": {str(k): pnl[k] for k in (30, 14, 7)},
            }
        elif enough[30] and pnl[30] <= min_net:
            item = {
                "allow": False,
                "status": "primary_loss",
                "reason": "板块30天copy亏损",
                "closed": {str(k): closed[k] for k in (30, 14, 7)},
                "pnl": {str(k): pnl[k] for k in (30, 14, 7)},
            }
        elif (enough[14] and pnl[14] > min_net) or (enough[30] and pnl[30] > min_net):
            item = {
                "allow": True,
                "status": "allowed",
                "reason": "板块copy回测盈利",
                "closed": {str(k): closed[k] for k in (30, 14, 7)},
                "pnl": {str(k): pnl[k] for k in (30, 14, 7)},
            }
            allowed.append(sector)
        else:
            item = {
                "allow": False,
                "status": "thin_evidence",
                "reason": "板块copy正收益证据不足",
                "closed": {str(k): closed[k] for k in (30, 14, 7)},
                "pnl": {str(k): pnl[k] for k in (30, 14, 7)},
            }
        policy[sector] = item
    policy["allowed"] = allowed
    return policy


def _aggregate_window(copy_json: Mapping, allowed: set[str], days: int) -> dict | None:
    total = {
        "copy_net_pnl": 0.0,
        "closed_n": 0,
        "wins": 0,
        "target_open_events": 0,
        "opened_n": 0,
        "liquidations": 0,
        "fee_drag": 0.0,
    }
    seen = False
    for sector in allowed:
        result = _window_result(copy_json.get(sector) or {}, days)
        if not result:
            continue
        seen = True
        total["copy_net_pnl"] += _num(result.get("copy_net_pnl"))
        total["closed_n"] += _int(result.get("closed_n"))
        total["wins"] += _int(result.get("wins"))
        total["target_open_events"] += _int(result.get("target_open_events"))
        total["opened_n"] += _int(result.get("opened_n"))
        total["liquidations"] += _int(result.get("liquidations"))
        total["fee_drag"] += _num(result.get("fee_drag"))
    if not seen:
        return None
    target_open = total["target_open_events"]
    total["open_fill_rate"] = (total["opened_n"] / target_open) if target_open else None
    return total


def apply_allowed_sector_copy_metrics(metrics: Mapping) -> dict:
    policy = parse_json_obj(metrics.get("sector_policy_json"))
    copy_json = parse_json_obj(metrics.get("sector_copy_json"))
    allowed = {
        sector for sector in SECTORS
        if isinstance(policy.get(sector), dict) and policy[sector].get("allow")
    }
    if not allowed or not copy_json:
        return dict(metrics)

    out = dict(metrics)
    primary = _aggregate_window(copy_json, allowed, 30)
    if primary:
        out["copy_bt_net_pnl"] = primary["copy_net_pnl"]
        out["copy_bt_closed_n"] = primary["closed_n"]
        out["copy_bt_win_rate"] = (primary["wins"] / primary["closed_n"]) if primary["closed_n"] else 0.0
        out["copy_bt_open_fill_rate"] = primary["open_fill_rate"]
        out["copy_bt_liquidations"] = primary["liquidations"]
        out["copy_bt_fee_drag"] = primary["fee_drag"]
    for days, net_key, n_key in (
        (14, "copy_bt_14d_net_pnl", "copy_bt_14d_closed_n"),
        (7, "copy_bt_7d_net_pnl", "copy_bt_7d_closed_n"),
    ):
        agg = _aggregate_window(copy_json, allowed, days)
        if agg:
            out[net_key] = agg["copy_net_pnl"]
            out[n_key] = agg["closed_n"]
    out["allowed_sectors"] = sorted(allowed)
    return out
