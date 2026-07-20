"""Market-sector helpers for copyability decisions.

Hyperliquid copy targets can be good at crypto while bleeding on transparent
builder stock/index perps, or vice versa. The scanner therefore records a
per-wallet sector policy that the observer can enforce per fill.
"""

from __future__ import annotations

import json
import math
from typing import Mapping

from hyper import config
from .copy_backtest import ADD_BLOCKED_OUTCOMES, ADD_METRICS_VERSION, ADD_OUTCOMES
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


def _qualification_equity(windows: Mapping) -> float:
    """Return the immutable replay sizing basis used by this sector."""
    for days in (30, 14, 7):
        value = _num(_window_result(windows, days).get("initial_margin_equity"))
        if value > 0:
            return value
    return max(
        1.0,
        float(getattr(config, "INITIAL_BALANCE", 10_000.0))
        * max(0.0, min(1.0, float(getattr(config, "MARGIN_EQUITY_PCT", 1.0)))),
    )


def _sector_economic_gate(windows: Mapping, *, min_net: float) -> dict:
    """Apply live-sector economics independently from wallet-level aggregation.

    A Mix wallet receives two permissions, not one blended permission. Strong Crypto evidence therefore
    cannot mask weak Stock evidence (or vice versa). Optional fields are checked whenever the canonical
    replay provides them; legacy rows without those fields still have to pass every sample/return floor.
    """
    policy = load_copy_policy()
    results = {days: _window_result(windows, days) for days in (30, 14, 7)}
    closed = {days: _int(results[days].get("closed_n")) for days in results}
    pnl = {days: _num(results[days].get("copy_net_pnl")) for days in results}
    enough = {days: closed[days] >= policy.min_closed(days) for days in results}
    equity = _qualification_equity(windows)
    return30 = pnl[30] / equity
    return14 = pnl[14] / equity
    return7 = pnl[7] / equity
    base = {
        "closed": {str(days): closed[days] for days in (30, 14, 7)},
        "pnl": {str(days): pnl[days] for days in (30, 14, 7)},
        "returns": {"30": return30, "14": return14, "7": return7},
        "qualificationEquity": equity,
    }

    if not all(enough.values()):
        return {
            **base,
            "allow": False,
            "status": "sector_sample_watch",
            "reason": "板块未独立达到30/14/7日实跟样本线",
            "watch": bool(closed[30] > 0 and pnl[30] > min_net),
        }
    if pnl[14] <= min_net:
        return {
            **base,
            "allow": False,
            "status": "sector_recent_weak",
            "reason": "板块14天严格Copy不盈利，已移出实跟权限",
            "watch": bool(pnl[30] > min_net),
        }
    if return30 < policy.core_min_return_30d:
        return {
            **base,
            "allow": False,
            "status": "sector_return_weak",
            "reason": (
                f"板块30天严格Copy收益率{return30 * 100:.1f}%低于"
                f"{policy.core_min_return_30d * 100:.0f}%实跟线"
            ),
            "watch": bool(pnl[30] > min_net),
        }
    if return7 < policy.core_min_return_7d:
        return {
            **base,
            "allow": False,
            "status": "sector_recent_weak",
            "reason": (
                f"板块7天严格Copy收益率{return7 * 100:.1f}%低于"
                f"{policy.core_min_return_7d * 100:.0f}%实跟线，已立即移出实跟权限"
            ),
            "watch": True,
        }

    primary = results[30]
    recent = results[7]
    checks = (
        (
            primary.get("valuation_status") is not None
            and str(primary.get("valuation_status") or "").strip().lower() != "complete",
            "sector_valuation_pending",
            "板块持仓末端估值不完整",
        ),
        (
            primary.get("profit_factor") is not None
            and _num(primary.get("profit_factor")) < policy.min_profit_factor,
            "sector_profit_structure_weak",
            f"板块严格Copy PF低于{policy.min_profit_factor:.2f}",
        ),
        (
            primary.get("net_after_top2") is not None
            and _num(primary.get("net_after_top2")) < equity * policy.min_tail_return_30d,
            "sector_tail_profit_weak",
            "板块30天移除最大两笔盈利后未达到尾部收益线",
        ),
        (
            recent.get("net_after_top1") is not None
            and _num(recent.get("net_after_top1")) <= min_net,
            "sector_recent_tail_weak",
            "板块7天收益依赖单一盈利回合",
        ),
        (
            primary.get("cost_stress_net_pnl") is not None
            and _num(primary.get("cost_stress_net_pnl")) <= min_net,
            "sector_cost_stress_weak",
            "板块1.5倍成本压力后不盈利",
        ),
        (
            primary.get("open_fill_rate") is not None
            and _num(primary.get("open_fill_rate")) < policy.min_actionable_open_rate,
            "sector_execution_weak",
            f"板块开仓跟随率低于{policy.min_actionable_open_rate * 100:.0f}%",
        ),
        (
            primary.get("capacity_open_fit") is not None
            and _num(primary.get("capacity_open_fit")) < policy.min_capacity_fit,
            "sector_capacity_weak",
            f"板块资金容量适配率低于{policy.min_capacity_fit * 100:.0f}%",
        ),
    )
    for failed, status, reason in checks:
        if failed:
            return {
                **base,
                "allow": False,
                "status": status,
                "reason": reason,
                "watch": True,
            }
    return {
        **base,
        "allow": True,
        "status": "allowed",
        "reason": "板块独立达到Core实跟标准",
        "watch": False,
    }


def _compact_result(result: Mapping) -> dict:
    keys = (
        "copy_net_pnl", "closed_net_pnl", "unrealized_pnl", "valuation_status",
        "valuation_coverage", "closed_n", "wins", "liquidations", "fee_drag",
        "target_open_events", "opened_n", "open_fill_rate", "capacity_open_fit",
        "target_adds", "followed_adds", "missed_adds", "missed_add_rate",
        "path_completion_rate", "behavior_replication_rate", "behavior_replication_v2",
        "add_metrics_version", "add_outcome_counts", "raw_add_order_follow_rate",
        "noise_merged_adds", "blocked_adds", "actionable_add_orders",
        "actionable_add_capture_rate", "true_blocked_add_rate", "add_episode_count",
        "entry_gap_sigma_weighted", "entry_gap_sigma_p90", "entry_gap_pct_weighted",
        "entry_gap_pct_p90", "entry_gap_sigma_samples", "entry_gap_pct_samples",
        "entry_gap_weight", "entry_gap_sigma_weighted_sum", "entry_gap_pct_weighted_sum",
        "entry_alignment", "add_execution", "add_fidelity", "add_fidelity_applied",
        "effective_add_fidelity", "gross_profit", "gross_loss", "profit_factor",
        "payoff_ratio", "positive_episode_n", "negative_episode_n", "top_positive_pnls",
        "top1_profit_share", "top3_profit_share", "net_after_top1", "net_after_top2",
        "body_after_top3_n", "body_after_top3_wins", "body_after_top3_losses",
        "body_after_top3_win_rate", "body_after_top3_net_pnl",
        "body_after_top3_gross_profit", "body_after_top3_gross_loss",
        "body_after_top3_profit_factor", "body_after_top3_payoff_ratio",
        "body_after_top3_median_pnl",
        "cost_stress_net_pnl", "initial_margin_equity",
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


def compact_sector_results(sector_results: Mapping, joint_results: Mapping | None = None) -> dict:
    out = {}
    for sector in SECTORS:
        windows = sector_results.get(sector) or {}
        out[sector] = {str(days): _compact_result(result) for days, result in windows.items() if result}
    if joint_results:
        if "copy_net_pnl" in joint_results:
            out["joint"] = {str(config.COPY_BT_DAYS): _compact_result(joint_results)}
        else:
            out["joint"] = {
                str(days): _compact_result(result)
                for days, result in joint_results.items()
                if isinstance(result, Mapping) and result
            }
    return out


def evaluate_sector_policy(
    sector_results: Mapping,
    min_net: float | None = None,
    previous_policy=None,
    structural_policy=None,
) -> dict:
    min_net = float(config.COPY_BT_MIN_NET_PNL if min_net is None else min_net)
    # Kept in the signature for old replay callers. Current-generation sector weakness is immediate and
    # never inherits a live permission or grace period from the previous policy.
    previous_policy = parse_json_obj(previous_policy)
    structural_policy = parse_json_obj(structural_policy)
    policy = {}
    allowed = []
    evidence_watch = []
    structural_watch = []
    for sector in SECTORS:
        windows = sector_results.get(sector) or {}
        economic = _sector_economic_gate(windows, min_net=min_net)
        closed = {days: _int((economic.get("closed") or {}).get(str(days))) for days in (30, 14, 7)}
        pnl = {days: _num((economic.get("pnl") or {}).get(str(days))) for days in (30, 14, 7)}
        recent_assessment = assess_recent_copy_loss(windows, min_net=min_net)
        recent_assessment["streak"] = 0
        item = {**economic, "recent": recent_assessment}
        item_base = {
            "closed": item.get("closed") or {},
            "pnl": item.get("pnl") or {},
            "returns": item.get("returns") or {},
            "qualificationEquity": item.get("qualificationEquity"),
            "recent": recent_assessment,
        }
        if item.get("allow"):
            allowed.append(sector)
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
        elif structural.get("watch") and item.get("allow"):
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
        elif structural.get("watch"):
            # Heavy-DCA pressure validation cannot resurrect a sector that failed current economics.
            item["structural"] = structural
        elif structural:
            item["structural"] = structural
        # Weak/thin sectors can remain observation evidence, but never live permissions. Wallet scoring
        # consumes ``allowed`` first, so a strong side cannot aggregate the wallet's weak side.
        if not item.get("allow") and item.get("watch") and (not structural or structural.get("allow")):
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
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "positive_episode_n": 0,
        "negative_episode_n": 0,
        "cost_stress_net_pnl": 0.0,
        "initial_margin_equity": 0.0,
        "path_completion_weighted": 0.0,
        "entry_gap_weight": 0.0,
        "entry_gap_sigma_weighted_sum": 0.0,
        "entry_gap_pct_weighted_sum": 0.0,
        "add_episode_count": 0,
    }
    add_counts = {key: 0 for key in ADD_OUTCOMES}
    top_positive_pnls = []
    sigma_samples = []
    pct_samples = []
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
        total["gross_profit"] += _num(result.get("gross_profit"))
        total["gross_loss"] += _num(result.get("gross_loss"))
        total["positive_episode_n"] += _int(result.get("positive_episode_n"))
        total["negative_episode_n"] += _int(result.get("negative_episode_n"))
        total["cost_stress_net_pnl"] += _num(result.get("cost_stress_net_pnl"))
        total["initial_margin_equity"] = max(
            total["initial_margin_equity"], _num(result.get("initial_margin_equity"))
        )
        total["path_completion_weighted"] += (
            _num(result.get("path_completion_rate"), 1.0) * max(0, _int(result.get("closed_n")))
        )
        for key in ADD_OUTCOMES:
            add_counts[key] += _int((result.get("add_outcome_counts") or {}).get(key))
        total["add_episode_count"] += _int(result.get("add_episode_count"))
        total["entry_gap_weight"] += _num(result.get("entry_gap_weight"))
        total["entry_gap_sigma_weighted_sum"] += _num(result.get("entry_gap_sigma_weighted_sum"))
        total["entry_gap_pct_weighted_sum"] += _num(result.get("entry_gap_pct_weighted_sum"))
        top_positive_pnls.extend(_num(value) for value in (result.get("top_positive_pnls") or []))
        sigma_samples.extend(_num(value) for value in (result.get("entry_gap_sigma_samples") or []))
        pct_samples.extend(_num(value) for value in (result.get("entry_gap_pct_samples") or []))
        if str(result.get("valuation_status") or "complete") != "complete":
            total["valuation_status"] = "missing_marks"
    if not seen:
        return None
    target_open = total["target_open_events"]
    total["open_fill_rate"] = (total["opened_n"] / target_open) if target_open else None
    total["actionable_open_rate"] = total["open_fill_rate"] if target_open else 1.0
    total_closed = total["closed_n"]
    total["path_completion_rate"] = (
        total.pop("path_completion_weighted") / total_closed if total_closed else 1.0
    )
    gross_profit = total["gross_profit"]
    gross_loss = total["gross_loss"]
    avg_win = gross_profit / total["positive_episode_n"] if total["positive_episode_n"] else 0.0
    avg_loss = gross_loss / total["negative_episode_n"] if total["negative_episode_n"] else 0.0
    top_positive_pnls.sort(reverse=True)
    total.update({
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
        "payoff_ratio": avg_win / avg_loss if avg_loss > 0 else (999.0 if avg_win > 0 else 0.0),
        "top_positive_pnls": top_positive_pnls[:3],
        "top1_profit_share": top_positive_pnls[0] / gross_profit if gross_profit > 0 and top_positive_pnls else 0.0,
        "top3_profit_share": sum(top_positive_pnls[:3]) / gross_profit if gross_profit > 0 else 0.0,
        "net_after_top1": total["copy_net_pnl"] - sum(top_positive_pnls[:1]),
        "net_after_top2": total["copy_net_pnl"] - sum(top_positive_pnls[:2]),
    })
    target_adds = sum(add_counts.values())
    followed = add_counts["followed"]
    noise = add_counts["noise_merged"]
    blocked = sum(add_counts[key] for key in ADD_BLOCKED_OUTCOMES)
    actionable_adds = followed + blocked
    entry_weight = total["entry_gap_weight"]
    weighted_sigma = total["entry_gap_sigma_weighted_sum"] / entry_weight if entry_weight else 0.0
    weighted_pct = total["entry_gap_pct_weighted_sum"] / entry_weight if entry_weight else 0.0

    def percentile(values, quantile):
        rows = sorted(values)
        if not rows:
            return 0.0
        return rows[max(0, min(len(rows) - 1, int(math.ceil(len(rows) * quantile)) - 1))]

    p90_sigma = percentile(sigma_samples, 0.90)
    p90_pct = percentile(pct_samples, 0.90)
    alignment = max(0.0, min(1.0, 1.0 - 0.5 * weighted_sigma - 0.5 * p90_sigma))
    execution = 1.0 - (blocked / actionable_adds if actionable_adds else 0.0)
    fidelity = 0.8 * alignment + 0.2 * execution
    applied = total["add_episode_count"] >= 5
    total.update({
        "add_metrics_version": ADD_METRICS_VERSION,
        "add_outcome_counts": add_counts,
        "target_adds": target_adds,
        "followed_adds": followed,
        "missed_adds": max(0, target_adds - followed),
        "missed_add_rate": (target_adds - followed) / target_adds if target_adds else 0.0,
        "raw_add_order_follow_rate": followed / target_adds if target_adds else 1.0,
        "noise_merged_adds": noise,
        "blocked_adds": blocked,
        "actionable_add_orders": actionable_adds,
        "actionable_add_capture_rate": followed / actionable_adds if actionable_adds else 1.0,
        "true_blocked_add_rate": blocked / actionable_adds if actionable_adds else 0.0,
        "entry_gap_sigma_weighted": weighted_sigma,
        "entry_gap_sigma_p90": p90_sigma,
        "entry_gap_pct_weighted": weighted_pct,
        "entry_gap_pct_p90": p90_pct,
        "entry_gap_sigma_samples": sigma_samples,
        "entry_gap_pct_samples": pct_samples,
        "entry_alignment": alignment,
        "add_execution": execution,
        "add_fidelity": fidelity,
        "add_fidelity_applied": applied,
        "effective_add_fidelity": fidelity if applied else 1.0,
    })
    behavior_v2 = max(
        0.0,
        min(1.0, total["actionable_open_rate"] * total["path_completion_rate"] * total["effective_add_fidelity"]),
    )
    total["behavior_replication_rate"] = behavior_v2
    total["behavior_replication_v2"] = behavior_v2
    return total


def _evidence_window(copy_json: Mapping, evidence_sectors: set[str], days: int) -> dict | None:
    """Return one canonical account replay for the selected sector policy.

    A single-sector wallet can use that sector's exact replay.  A genuine Mix wallet must use the joint
    replay because summing two independently funded $10k accounts inflates PnL, capacity and sample metrics.
    Legacy payloads without ``joint`` leave the caller's already-joint base fields untouched; independently
    funded sector accounts are never summed as a migration fallback.
    """
    if len(evidence_sectors) == 1:
        sector = next(iter(evidence_sectors))
        return _window_result(copy_json.get(sector) or {}, days) or None
    joint = _window_result(copy_json.get("joint") or {}, days)
    return joint or None


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
    primary = _evidence_window(copy_json, evidence_sectors, 30)
    if primary:
        out["copy_bt_net_pnl"] = primary["copy_net_pnl"]
        out["copy_bt_closed_n"] = primary["closed_n"]
        closed_n = _int(primary.get("closed_n"))
        out["copy_bt_win_rate"] = _int(primary.get("wins")) / closed_n if closed_n else 0.0
        target_open = _int(primary.get("target_open_events"))
        out["copy_bt_open_fill_rate"] = primary.get("open_fill_rate")
        if out["copy_bt_open_fill_rate"] is None and target_open:
            out["copy_bt_open_fill_rate"] = _int(primary.get("opened_n")) / target_open
        out["copy_bt_liquidations"] = _int(primary.get("liquidations"))
        out["copy_bt_fee_drag"] = _num(primary.get("fee_drag"))
        out["copy_bt_unrealized_pnl"] = _num(primary.get("unrealized_pnl"))
        out["copy_bt_valuation_status"] = primary.get("valuation_status") or "complete"
        for key in (
            "profit_factor", "payoff_ratio", "gross_profit", "gross_loss",
            "positive_episode_n", "negative_episode_n",
            "top1_profit_share", "top3_profit_share", "net_after_top1", "net_after_top2",
            "body_after_top3_n", "body_after_top3_wins", "body_after_top3_losses",
            "body_after_top3_win_rate", "body_after_top3_net_pnl",
            "body_after_top3_gross_profit", "body_after_top3_gross_loss",
            "body_after_top3_profit_factor", "body_after_top3_payoff_ratio",
            "body_after_top3_median_pnl",
            "cost_stress_net_pnl", "add_metrics_version", "add_outcome_counts",
            "raw_add_order_follow_rate", "noise_merged_adds", "blocked_adds",
            "actionable_add_capture_rate", "entry_gap_pct_weighted", "entry_gap_pct_p90",
            "entry_gap_sigma_weighted", "entry_gap_sigma_p90", "entry_alignment",
            "add_execution", "add_fidelity", "add_fidelity_applied",
            "behavior_replication_v2", "behavior_replication_rate",
        ):
            if key in primary:
                out[f"copy_bt_{key}"] = primary[key]
    for days, net_key, n_key in (
        (14, "copy_bt_14d_net_pnl", "copy_bt_14d_closed_n"),
        (7, "copy_bt_7d_net_pnl", "copy_bt_7d_closed_n"),
    ):
        agg = _evidence_window(copy_json, evidence_sectors, days)
        if agg:
            out[net_key] = agg["copy_net_pnl"]
            out[n_key] = agg["closed_n"]
            out[f"copy_bt_{days}d_win_rate"] = (
                _int(agg.get("wins")) / _int(agg.get("closed_n"))
                if _int(agg.get("closed_n")) else 0.0
            )
            out[f"copy_bt_{days}d_unrealized_pnl"] = _num(agg.get("unrealized_pnl"))
            for key in (
                "profit_factor", "net_after_top1", "net_after_top2",
                "top1_profit_share", "top3_profit_share", "cost_stress_net_pnl",
            ):
                out[f"copy_bt_{days}d_{key}"] = agg.get(key)
    out["allowed_sectors"] = sorted(allowed)
    out["evidence_sectors"] = sorted(evidence_sectors)
    return out
