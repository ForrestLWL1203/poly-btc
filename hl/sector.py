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
from .copy_data import is_copyable_coin
from .copy_policy import load_copy_policy

SECTORS = ("crypto", "stock")


def classify_coin(coin: str | None) -> str | None:
    text = str(coin or "").strip()
    if not is_copyable_coin(text):
        return None
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
    if classify_coin(coin) not in SECTORS:
        return False
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
    return load_copy_policy().min_closed(int(days))


def _window_result(windows: Mapping, days: int) -> dict:
    return dict(windows.get(days) or windows.get(str(days)) or {})


def _compact_result(result: Mapping) -> dict:
    keys = (
        "copy_net_pnl", "closed_net_pnl", "unrealized_pnl", "valuation_status",
        "valuation_coverage", "closed_n", "wins", "liquidations", "fee_drag",
        "target_open_events", "opened_n", "open_fill_rate", "capacity_open_fit",
        "target_adds", "followed_adds", "missed_adds",
    )
    return {k: result.get(k) for k in keys if k in result}


def _weighted_median(samples: list[tuple[float, float]]) -> float:
    rows = sorted((value, max(0.0, weight)) for value, weight in samples if weight > 0)
    total = sum(weight for _, weight in rows)
    if not rows or total <= 0:
        return 0.0
    midpoint = total / 2.0
    seen = 0.0
    for value, weight in rows:
        seen += weight
        if seen >= midpoint:
            return value
    return rows[-1][0]


def _position_return_samples(result: Mapping, *, closed_before_ms: int | None = None) -> list[tuple[float, float]]:
    samples = []
    for position in result.get("positions") or []:
        margin = _num(position.get("margin"))
        if margin <= 0:
            continue
        closed_at = _int(position.get("closed_at"))
        if closed_before_ms is not None and (closed_at <= 0 or closed_at >= closed_before_ms):
            continue
        samples.append((_num(position.get("net_pnl")) / margin, margin))
    return samples


def _recent_return_samples(result: Mapping) -> list[tuple[float, float]]:
    samples = _position_return_samples(result)
    open_positions = result.get("open_positions") or []
    open_margin = sum(max(0.0, _num(position.get("margin"))) for position in open_positions)
    if open_margin > 0:
        open_net = sum(_num(position.get("net_pnl")) for position in open_positions)
        open_net += _num(result.get("unrealized_pnl"))
        samples.append((open_net / open_margin, open_margin))
    return samples


def _weighted_return(samples: list[tuple[float, float]]) -> tuple[float, float]:
    total_weight = sum(weight for _, weight in samples if weight > 0)
    if total_weight <= 0:
        return 0.0, 0.0
    mean = sum(value * weight for value, weight in samples if weight > 0) / total_weight
    sum_sq = sum(weight * weight for _, weight in samples if weight > 0)
    effective_n = (total_weight * total_weight / sum_sq) if sum_sq > 0 else 0.0
    return mean, effective_n


def assess_recent_copy_loss(
    windows: Mapping,
    *,
    min_net: float = 0.0,
    min_recent_closed: int = 7,
    min_baseline_closed: int = 7,
    z_limit: float = -1.96,
) -> dict:
    """Classify a negative 7d replay against the wallet's own prior behavior.

    Position PnL is normalized by the copy margin committed to that episode, so
    the decision is independent of account dollars and changing sizing params.
    The baseline excludes the latest seven days to avoid comparing overlapping
    7d/14d/30d aggregates.
    """
    recent = _window_result(windows, 7)
    primary = _window_result(windows, 30)
    recent_pnl = _num(recent.get("copy_net_pnl"))
    recent_closed = _int(recent.get("closed_n"))
    liquidations = _int(recent.get("liquidations"))
    latest_close = max((_int(p.get("closed_at")) for p in recent.get("positions") or []), default=0)
    evidence_key = f"{recent_closed}:{latest_close}:{recent_pnl:.8g}"
    base = {
        "classification": "not_negative",
        "hard": False,
        "recentClosed": recent_closed,
        "baselineClosed": 0,
        "evidenceKey": evidence_key,
    }
    if recent_pnl > min_net:
        return base
    if liquidations > 0:
        return {
            **base,
            "classification": "liquidation",
            "hard": True,
            "liquidations": liquidations,
        }
    if recent_closed < min_recent_closed:
        return {
            **base,
            "classification": "insufficient_recent",
            "reason": "近期亏损样本不足，不作硬否决",
        }

    window_end_ms = _int(primary.get("_window_end_ms")) or _int(recent.get("_window_end_ms"))
    cutoff_ms = window_end_ms - 7 * 86400_000 if window_end_ms > 0 else None
    baseline_samples = _position_return_samples(primary, closed_before_ms=cutoff_ms)
    recent_samples = _recent_return_samples(recent)
    base["baselineClosed"] = len(baseline_samples)
    if len(baseline_samples) < min_baseline_closed or len(recent_samples) < min_recent_closed:
        return {
            **base,
            "classification": "insufficient_distribution",
            "hard": True,
            "reason": "近期亏损且缺少足够的非重叠历史分布",
        }

    baseline_center = _weighted_median(baseline_samples)
    deviations = [(abs(value - baseline_center), weight) for value, weight in baseline_samples]
    robust_scale = 1.4826 * _weighted_median(deviations)
    if robust_scale <= 1e-9:
        baseline_mean, _ = _weighted_return(baseline_samples)
        total_weight = sum(weight for _, weight in baseline_samples)
        variance = (
            sum(weight * (value - baseline_mean) ** 2 for value, weight in baseline_samples) / total_weight
            if total_weight > 0 else 0.0
        )
        robust_scale = math.sqrt(max(0.0, variance))
    # Numerical floor is relative to this wallet's own historical edge, not dollars.
    robust_scale = max(robust_scale, abs(baseline_center) * 0.25, 1e-9)
    recent_return, recent_effective_n = _weighted_return(recent_samples)
    standard_error = robust_scale / math.sqrt(max(1.0, recent_effective_n))
    z_score = (recent_return - baseline_center) / standard_error if standard_error > 0 else 0.0
    hard = z_score <= z_limit
    return {
        **base,
        "classification": "significant_loss" if hard else "shallow_loss",
        "hard": hard,
        "recentReturn": round(recent_return, 6),
        "baselineReturn": round(baseline_center, 6),
        "baselineScale": round(robust_scale, 6),
        "zScore": round(z_score, 3),
        "reason": "近期收益显著低于自身历史" if hard else "近期亏损仍在自身历史波动范围",
    }


def compact_sector_results(sector_results: Mapping) -> dict:
    out = {}
    for sector in SECTORS:
        windows = sector_results.get(sector) or {}
        out[sector] = {str(days): _compact_result(result) for days, result in windows.items() if result}
    return out


def evaluate_sector_policy(
    sector_results: Mapping,
    min_net: float | None = None,
    previous_policy=None,
    structural_policy=None,
) -> dict:
    min_net = float(config.COPY_BT_MIN_NET_PNL if min_net is None else min_net)
    previous_policy = parse_json_obj(previous_policy)
    structural_policy = parse_json_obj(structural_policy)
    policy = {}
    allowed = []
    evidence_watch = []
    structural_watch = []
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

        recent_assessment = assess_recent_copy_loss(windows, min_net=min_net)
        previous_item = previous_policy.get(sector) if isinstance(previous_policy.get(sector), dict) else {}
        previous_recent = previous_item.get("recent") if isinstance(previous_item.get("recent"), dict) else {}
        previous_streak = _int(previous_recent.get("streak"))
        same_evidence = (
            bool(recent_assessment.get("evidenceKey"))
            and recent_assessment.get("evidenceKey") == previous_recent.get("evidenceKey")
        )
        if recent_assessment.get("classification") == "significant_loss":
            streak = previous_streak if same_evidence else previous_streak + 1
            recent_assessment["streak"] = max(1, streak)
        else:
            recent_assessment["streak"] = 0

        item_base = {
            "closed": {str(k): closed[k] for k in (30, 14, 7)},
            "pnl": {str(k): pnl[k] for k in (30, 14, 7)},
            "recent": recent_assessment,
        }
        enough_days = [days for days in (30, 14, 7) if enough[days]]
        if not enough_days:
            item = {
                **item_base,
                "allow": False,
                "status": "thin_evidence",
                "reason": "板块copy样本不足",
            }
        elif enough[14] and pnl[14] <= min_net:
            item = {
                **item_base,
                "allow": False,
                "status": "recent_loss",
                "reason": "板块14天copy亏损",
            }
        elif enough[7] and pnl[7] <= min_net and recent_assessment.get("hard"):
            is_liquidation = recent_assessment.get("classification") == "liquidation"
            has_grace = (
                not is_liquidation
                and bool(previous_item.get("allow"))
                and recent_assessment.get("streak", 1) < 2
                and ((enough[14] and pnl[14] > min_net) or (enough[30] and pnl[30] > min_net))
            )
            item = {
                **item_base,
                "allow": bool(has_grace),
                "status": "recent_degradation_watch" if has_grace else "recent_loss",
                "reason": (
                    "板块近期显著恶化，保留一轮复核"
                    if has_grace else
                    ("板块近期copy出现爆仓" if is_liquidation else "板块近期copy显著恶化")
                ),
            }
            if has_grace:
                allowed.append(sector)
        elif enough[7] and pnl[7] <= min_net and (
            (enough[14] and pnl[14] > min_net) or (enough[30] and pnl[30] > min_net)
        ):
            item = {
                **item_base,
                "allow": True,
                "status": "recent_soft_loss",
                "reason": "板块7天浅亏，仍在自身历史波动范围",
            }
            allowed.append(sector)
        elif enough[30] and pnl[30] <= min_net:
            item = {
                **item_base,
                "allow": False,
                "status": "primary_loss",
                "reason": "板块30天copy亏损",
            }
        elif (enough[14] and pnl[14] > min_net) or (enough[30] and pnl[30] > min_net):
            item = {
                **item_base,
                "allow": True,
                "status": "allowed",
                "reason": "板块copy回测盈利",
            }
            allowed.append(sector)
        else:
            item = {
                **item_base,
                "allow": False,
                "status": "thin_evidence",
                "reason": "板块copy正收益证据不足",
            }
        structural = structural_policy.get(sector)
        structural = structural if isinstance(structural, dict) else {}
        if structural and not structural.get("allow"):
            item = {
                **item_base,
                "allow": False,
                "status": str(structural.get("status") or "structural_unqualified"),
                "reason": str(structural.get("reason") or "板块结构不可复制"),
                "structural": structural,
            }
            if sector in allowed:
                allowed.remove(sector)
        elif structural.get("watch"):
            primary = _window_result(windows, 30)
            pressure_ok = bool(
                item.get("allow")
                and _int(primary.get("closed_n")) >= _min_closed_for_days(30)
                and _num(primary.get("copy_net_pnl")) > min_net
                and _int(primary.get("liquidations")) == 0
                and _num(primary.get("open_fill_rate"), 1.0)
                    >= load_copy_policy().min_actionable_open_rate
                and _num(primary.get("capacity_open_fit"), 1.0)
                    >= load_copy_policy().min_capacity_fit
                and all(
                    _int(_window_result(windows, days).get("closed_n")) < _min_closed_for_days(days)
                    or _num(_window_result(windows, days).get("copy_net_pnl")) > min_net
                    for days in (14, 7)
                )
            )
            if pressure_ok:
                item = {
                    **item,
                    "allow": True,
                    "status": "heavy_dca_pressure_passed",
                    "reason": "单次Heavy-DCA已通过实际跟单规则压力回放",
                    "structural": structural,
                    "coreBlocked": False,
                }
                if sector not in allowed:
                    allowed.append(sector)
                structural_watch.append(sector)
            else:
                item = {
                    **item_base,
                    "allow": False,
                    "status": "heavy_dca_pressure_failed",
                    "reason": "单次Heavy-DCA受限回放未通过额外压力验证",
                    "structural": structural,
                }
                if sector in allowed:
                    allowed.remove(sector)
        elif structural:
            item["structural"] = structural
        # A profitable sector with too few closed samples is useful Challenger evidence, not a live-trading
        # permission.  Keep it on a separate watch list so scoring can apply the same percentage economics
        # while Observer remains fail-closed until the normal 30/14/7 sample floors are met.
        if (
            not item.get("allow")
            and item.get("status") == "thin_evidence"
            and closed[30] > 0
            and pnl[30] > min_net
            and (not structural or structural.get("allow"))
        ):
            item["watch"] = True
            item["reason"] = "板块严格Copy盈利但样本不足，进入样本观察"
            evidence_watch.append(sector)
        policy[sector] = item
    # A one-off Heavy-DCA episode is already executed through bounded smart-add spacing, add-count and
    # coin-cap rules in the pressure replay. Passing that exact replay is sufficient structural proof,
    # including for a genuine Mix wallet whose other specialty is independently qualified.
    core_blocked = False
    policy["allowed"] = allowed
    policy["watch"] = [sector for sector in evidence_watch if sector not in allowed]
    policy["structuralWatch"] = structural_watch
    policy["coreBlocked"] = core_blocked
    if structural_policy.get("source"):
        policy["specializationSource"] = structural_policy.get("source")
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
        "unrealized_pnl": 0.0,
        "valuation_status": "complete",
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
        total["unrealized_pnl"] += _num(result.get("unrealized_pnl"))
        if str(result.get("valuation_status") or "complete") != "complete":
            total["valuation_status"] = "missing_marks"
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
    watched = {
        sector for sector in policy.get("watch", ())
        if sector in SECTORS and isinstance(policy.get(sector), dict)
    }
    evidence_sectors = allowed or watched
    if not evidence_sectors or not copy_json:
        return dict(metrics)

    out = dict(metrics)
    primary = _aggregate_window(copy_json, evidence_sectors, 30)
    if primary:
        out["copy_bt_net_pnl"] = primary["copy_net_pnl"]
        out["copy_bt_closed_n"] = primary["closed_n"]
        out["copy_bt_win_rate"] = (primary["wins"] / primary["closed_n"]) if primary["closed_n"] else 0.0
        out["copy_bt_open_fill_rate"] = primary["open_fill_rate"]
        out["copy_bt_liquidations"] = primary["liquidations"]
        out["copy_bt_fee_drag"] = primary["fee_drag"]
        out["copy_bt_unrealized_pnl"] = primary["unrealized_pnl"]
        out["copy_bt_valuation_status"] = primary["valuation_status"]
    for days, net_key, n_key in (
        (14, "copy_bt_14d_net_pnl", "copy_bt_14d_closed_n"),
        (7, "copy_bt_7d_net_pnl", "copy_bt_7d_closed_n"),
    ):
        agg = _aggregate_window(copy_json, evidence_sectors, days)
        if agg:
            out[net_key] = agg["copy_net_pnl"]
            out[n_key] = agg["closed_n"]
            out[f"copy_bt_{days}d_unrealized_pnl"] = agg["unrealized_pnl"]
    out["allowed_sectors"] = sorted(allowed)
    out["evidence_sectors"] = sorted(evidence_sectors)
    return out
