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
from .copy_data import is_copyable_coin
from .economics import (
    OPEN_LOSS_RATIO_LIMIT,
    open_loss_ratio_within_limit,
    replay_result_profitability,
)
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


def _window_equity(windows: Mapping, days: int) -> float:
    """Return floating account equity at one continuous replay-window boundary."""
    result = _window_result(windows, days)
    for key in ("window_start_equity", "initial_margin_equity"):
        value = _num(result.get(key))
        if value > 0:
            return value
    for fallback_days in (30, 14, 7):
        fallback = _window_result(windows, fallback_days)
        for key in ("window_start_equity", "initial_margin_equity"):
            value = _num(fallback.get(key))
            if value > 0:
                return value
    return max(
        1.0,
        float(getattr(config, "INITIAL_BALANCE", 10_000.0)),
    )


def _sector_economic_gate(windows: Mapping) -> dict:
    """Admit only positive, Challenger-grade sectors into wallet-level aggregation.

    Sector isolation prevents a profitable Crypto side from masking a losing Stock side. It must not repeat
    wallet-level source/Copy quality gates per sector: the aggregate of safe sectors owns that proof.
    Requiring every side to be a standalone Core was the main sector-level false-negative cliff.
    """
    policy = load_copy_policy()
    results = {days: _window_result(windows, days) for days in (30, 14, 7)}
    closed = {days: _int(results[days].get("closed_n")) for days in results}
    economics = {
        days: replay_result_profitability(
            results[days], start_equity=_window_equity(windows, days),
        )
        for days in results
    }
    pnl = {days: _num(economics[days].get("qualificationPnl")) for days in results}
    closed_pnl = {days: _num(economics[days].get("closedPnl")) for days in results}
    wins = {
        days: max(0, min(closed[days], _int(results[days].get("wins"))))
        for days in results
    }
    win_rate = {
        days: wins[days] / closed[days] if closed[days] else 0.0
        for days in results
    }
    equities = {days: _window_equity(windows, days) for days in (30, 14, 7)}
    return30 = pnl[30] / equities[30]
    return14 = pnl[14] / equities[14]
    return7 = pnl[7] / equities[7]
    primary = results[30]
    evidence_days = _int(primary.get("evidence_days"))
    if evidence_days <= 0:
        evidence_days = len({
            int(position.get("closed_at") or 0) // 86_400_000
            for position in primary.get("positions") or ()
            if int(position.get("closed_at") or 0) > 0
        })
    challenger_watch = bool(return30 > 0.0 and closed[30] > 0)
    base = {
        "closed": {str(days): closed[days] for days in (30, 14, 7)},
        "pnl": {str(days): pnl[days] for days in (30, 14, 7)},
        "closedPnl": {str(days): closed_pnl[days] for days in (30, 14, 7)},
        "openProfitReference": {
            str(days): _num(economics[days].get("openProfitReference"))
            for days in (30, 14, 7)
        },
        "openLoss": {
            str(days): _num(economics[days].get("openLoss"))
            for days in (30, 14, 7)
        },
        "openLossRatio": {
            str(days): economics[days].get("openLossRatio")
            for days in (30, 14, 7)
        },
        "profitabilityBasis": economics[30].get("basis"),
        "returns": {"30": return30, "14": return14, "7": return7},
        "winRate": {str(days): win_rate[days] for days in (30, 14, 7)},
        "evidenceDays": evidence_days,
        "simulatedPathRisk": {
            "liquidations30d": _int(primary.get("liquidations")),
            "liquidations7d": _int(results[7].get("liquidations")),
            "maxLiquidationLossPct30d": _num(
                primary.get("max_liquidation_loss_pct")
            ),
            "maxLiquidationLoss30d": _num(primary.get("max_liquidation_loss")),
            "maxLiquidationLossCoin30d": primary.get(
                "max_liquidation_loss_coin"
            ),
            "intratradeMaxDrawdown": _num(primary.get("intratrade_max_drawdown")),
            "deepBagEvents": _int(primary.get("deep_bag_event_n")),
            "failedDeepBagEvents": _int(primary.get("failed_deep_bag_n")),
            "deepBagRecoveryRate": _num(primary.get("deep_bag_recovery_rate"), 1.0),
        },
        "qualificationEquity": equities[30],
        "windowStartEquity": {str(days): equities[days] for days in (30, 14, 7)},
    }

    # Path drawdown and proxy liquidations are produced with our configured maximum leverage because source
    # fills do not disclose their historical margin/leverage. Drawdown stays diagnostic; proxy liquidations
    # remain a sizing/tuning input. Neither suppresses a profitable sector before tuning can repair sizing.
    hard_checks = (
        (
            primary.get("valuation_status") is not None
            and str(primary.get("valuation_status") or "").strip().lower() != "complete",
            "sector_valuation_pending",
            "板块持仓末端估值不完整",
        ),
    )
    for failed, status, reason in hard_checks:
        if failed:
            return {
                **base, "allow": False, "status": status, "reason": reason, "watch": False,
                "hardRisk": False,
            }
    if return30 <= 0.0:
        return {
            **base,
            "allow": False,
            "status": "sector_not_profitable",
            "reason": "板块30天严格Copy净收益不为正",
            "watch": False,
        }
    if not open_loss_ratio_within_limit(economics[30]):
        return {
            **base,
            "allow": False,
            "status": "sector_open_loss_over_50pct",
            "reason": (
                "板块当前浮亏超过30日已平净利润的"
                f"{OPEN_LOSS_RATIO_LIMIT * 100:.0f}%"
            ),
            "watch": False,
        }
    if closed[30] < 2:
        return {
            **base,
            "allow": False,
            "status": "sector_sample_watch",
            "reason": "板块完整已平回合少于2个，先保留观察证据",
            "watch": challenger_watch,
        }

    return {
        **base,
        "allow": True,
        "status": "allowed",
        "reason": "板块严格Copy扣除执行成本后净盈利；路径风险交由最终参数与爆仓≤3规则处理",
        "watch": False,
    }


def _compact_result(result: Mapping) -> dict:
    keys = (
        "copy_net_pnl", "closed_net_pnl", "unrealized_pnl", "valuation_status",
        "valuation_coverage", "closed_n", "wins", "liquidations", "fee_drag",
        "gross_profit", "gross_loss", "profit_factor", "payoff_ratio",
        "top3_profit_share", "body_after_top3_n", "body_after_top3_wins",
        "body_after_top3_win_rate", "body_after_top3_net_pnl",
        "max_liquidation_loss_pct", "max_liquidation_loss",
        "max_liquidation_loss_coin", "max_liquidation_loss_closed_at",
        "target_open_events", "raw_target_open_events", "small_open_excluded_n",
        "effective_target_open_events", "opened_n", "raw_open_capture_rate",
        "effective_open_follow_rate", "open_execution_audit",
        "open_fill_rate", "capacity_open_fit",
        "target_adds", "followed_adds", "missed_adds", "missed_add_rate",
        "path_completion_rate", "behavior_replication_rate", "behavior_replication_v2",
        "add_metrics_version", "add_outcome_counts", "raw_add_order_follow_rate",
        "noise_merged_adds", "blocked_adds", "actionable_add_orders",
        "actionable_add_capture_rate", "true_blocked_add_rate", "add_episode_count",
        "entry_gap_sigma_weighted", "entry_gap_sigma_p90", "entry_gap_pct_weighted",
        "entry_gap_pct_p90", "entry_gap_sigma_samples", "entry_gap_pct_samples",
        "entry_gap_weight", "entry_gap_sigma_weighted_sum", "entry_gap_pct_weighted_sum",
        "entry_alignment", "add_execution", "add_fidelity", "add_fidelity_applied",
        "effective_add_fidelity",
        "path_risk_status", "intratrade_max_drawdown", "max_underwater_hours",
        "loss_over_5_time_ratio", "deep_bag_event_n", "failed_deep_bag_n",
        "deep_bag_recovery_rate", "max_deep_bag_hours", "current_open_loss_frac",
        "current_bag_hours",
        "wallet_forward_loss_blocks",
        "initial_margin_equity", "window_start_equity",
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
    # The retired dollar knob duplicated the dynamic return contract and made admission depend on account
    # scale. Keep the argument only for old offline callers; production always uses the zero-profit boundary.
    min_net = float(0.0 if min_net is None else min_net)
    # Kept in the signature for old replay callers. Current-generation sector weakness is immediate and
    # never inherits a live permission or grace period from the previous policy.
    previous_policy = parse_json_obj(previous_policy)
    structural_policy = parse_json_obj(structural_policy)
    policy = {}
    allowed = []
    evidence_watch = []
    for sector in SECTORS:
        windows = sector_results.get(sector) or {}
        economic = _sector_economic_gate(windows)
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
        if structural and (not structural.get("allow") or structural.get("watch")):
            item = {
                **item_base,
                "allow": False,
                "status": str(structural.get("status") or "structural_unqualified"),
                "reason": str(structural.get("reason") or "板块结构不可复制"),
                "structural": structural,
            }
            if sector in allowed:
                allowed.remove(sector)
        elif structural:
            item["structural"] = structural
        # Weak/thin sectors can remain observation evidence, but never live permissions. Wallet scoring
        # consumes ``allowed`` first, so a strong side cannot aggregate the wallet's weak side.
        if not item.get("allow") and item.get("watch") and (not structural or structural.get("allow")):
            evidence_watch.append(sector)
        policy[sector] = item
    # Sample density is a wallet-level proof. A genuine Mix wallet may split its complete Episodes across
    # Crypto and Stock, so positive structurally clean sides can share the aggregate seven-close evidence.
    aggregate_sectors = []
    evidence_days = set()
    fallback_evidence_days = 0
    aggregate_closed = 0
    for sector in SECTORS:
        item = policy.get(sector) or {}
        structural = item.get("structural")
        structural = structural if isinstance(structural, dict) else {}
        closed_n = _int((item.get("closed") or {}).get("30"))
        positive = _num((item.get("returns") or {}).get("30")) > 0.0
        structurally_clean = (
            (not structural or structural.get("allow"))
            and not structural.get("watch")
            and not item.get("hardRisk")
        )
        if not (
            closed_n >= 2
            and positive
            and structurally_clean
            and (item.get("allow") or item.get("status") == "sector_sample_watch")
        ):
            continue
        aggregate_sectors.append(sector)
        aggregate_closed += closed_n
        fallback_evidence_days = max(
            fallback_evidence_days, _int(item.get("evidenceDays")),
        )
        primary = _window_result(sector_results.get(sector) or {}, 30)
        evidence_days.update(
            int(position.get("closed_at") or 0) // 86_400_000
            for position in primary.get("positions") or ()
            if int(position.get("closed_at") or 0) > 0
        )
    aggregate_evidence_days = len(evidence_days) or fallback_evidence_days
    aggregate_sample_ok = bool(
        aggregate_closed >= load_copy_policy().min_closed_30d
        and aggregate_evidence_days >= 5
    )
    if aggregate_sample_ok:
        for sector in aggregate_sectors:
            item = policy[sector]
            if item.get("status") != "sector_sample_watch":
                continue
            item.update({
                "allow": True,
                "watch": False,
                "status": "allowed_by_wallet_aggregate_evidence",
                "reason": "板块自身扣成本后盈利；样本密度由钱包安全板块合并证明",
                "aggregateEvidence": {
                    "closed": aggregate_closed,
                    "days": aggregate_evidence_days,
                },
            })
            if sector not in allowed:
                allowed.append(sector)
    policy["allowed"] = allowed
    policy["watch"] = [sector for sector in evidence_watch if sector not in allowed]
    if structural_policy.get("source"):
        policy["specializationSource"] = structural_policy.get("source")
    return policy


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
        out["copy_bt_closed_net_pnl"] = primary.get(
            "closed_net_pnl",
            _num(primary.get("copy_net_pnl")) - _num(primary.get("unrealized_pnl")),
        )
        out["copy_bt_closed_n"] = primary["closed_n"]
        closed_n = _int(primary.get("closed_n"))
        out["copy_bt_wins"] = _int(primary.get("wins"))
        out["copy_bt_win_rate"] = out["copy_bt_wins"] / closed_n if closed_n else 0.0
        out["copy_bt_position_win_rate"] = out["copy_bt_win_rate"]
        target_open = _int(primary.get("target_open_events"))
        out["copy_bt_open_fill_rate"] = primary.get("open_fill_rate")
        if out["copy_bt_open_fill_rate"] is None and target_open:
            out["copy_bt_open_fill_rate"] = _int(primary.get("opened_n")) / target_open
        for key in (
            "raw_target_open_events", "small_open_excluded_n",
            "effective_target_open_events", "opened_n", "raw_open_capture_rate",
            "effective_open_follow_rate", "open_execution_audit",
        ):
            if key in primary:
                out[f"copy_bt_{key}"] = primary[key]
        out["copy_bt_liquidations"] = _int(primary.get("liquidations"))
        out["copy_bt_max_liquidation_loss_pct"] = _num(
            primary.get("max_liquidation_loss_pct")
        )
        out["copy_bt_max_liquidation_loss"] = _num(
            primary.get("max_liquidation_loss")
        )
        out["copy_bt_max_liquidation_loss_coin"] = primary.get(
            "max_liquidation_loss_coin"
        )
        out["copy_bt_max_liquidation_loss_closed_at"] = primary.get(
            "max_liquidation_loss_closed_at"
        )
        out["copy_bt_fee_drag"] = _num(primary.get("fee_drag"))
        for key in (
            "gross_profit", "gross_loss", "profit_factor", "payoff_ratio",
            "top3_profit_share", "body_after_top3_n", "body_after_top3_wins",
            "body_after_top3_win_rate", "body_after_top3_net_pnl",
        ):
            if key in primary:
                out[f"copy_bt_{key}"] = primary[key]
        out["copy_bt_unrealized_pnl"] = _num(primary.get("unrealized_pnl"))
        out["copy_bt_valuation_status"] = primary.get("valuation_status") or "complete"
        for key in (
            "add_metrics_version", "add_outcome_counts",
            "raw_add_order_follow_rate", "noise_merged_adds", "blocked_adds",
            "actionable_add_capture_rate", "entry_gap_pct_weighted", "entry_gap_pct_p90",
            "entry_gap_sigma_weighted", "entry_gap_sigma_p90", "entry_alignment",
            "add_execution", "add_fidelity", "add_fidelity_applied",
            "behavior_replication_v2", "behavior_replication_rate",
            "initial_margin_equity", "window_start_equity",
            "path_risk_status", "intratrade_max_drawdown", "max_underwater_hours",
            "loss_over_5_time_ratio", "deep_bag_event_n", "failed_deep_bag_n",
            "deep_bag_recovery_rate", "max_deep_bag_hours", "current_open_loss_frac",
            "current_bag_hours",
            "wallet_forward_loss_blocks",
        ):
            if key in primary:
                # Keep the transient legacy alias for in-process callers and the prefixed field for durable
                # Profile storage. Reloaded qualification must retain the same sector-scoped denominator.
                if key == "initial_margin_equity":
                    out["initial_margin_equity"] = primary[key]
                    out["copy_bt_initial_margin_equity"] = primary[key]
                else:
                    out[f"copy_bt_{key}"] = primary[key]
        for source, target in (
            ("path_risk_status", "copy_path_risk_status"),
            ("intratrade_max_drawdown", "copy_intratrade_max_drawdown"),
            ("max_underwater_hours", "copy_max_underwater_hours"),
            ("loss_over_5_time_ratio", "copy_loss_over_5_time_ratio"),
            ("deep_bag_event_n", "copy_deep_bag_event_n"),
            ("failed_deep_bag_n", "copy_failed_deep_bag_n"),
            ("deep_bag_recovery_rate", "copy_deep_bag_recovery_rate"),
            ("max_deep_bag_hours", "copy_max_deep_bag_hours"),
            ("current_open_loss_frac", "copy_current_open_loss_frac"),
            ("current_bag_hours", "copy_current_bag_hours"),
        ):
            if source in primary:
                out[target] = primary[source]
    for days, net_key, n_key in (
        (14, "copy_bt_14d_net_pnl", "copy_bt_14d_closed_n"),
        (7, "copy_bt_7d_net_pnl", "copy_bt_7d_closed_n"),
    ):
        agg = _evidence_window(copy_json, evidence_sectors, days)
        if agg:
            out[net_key] = agg["copy_net_pnl"]
            out[f"copy_bt_{days}d_closed_net_pnl"] = agg.get(
                "closed_net_pnl",
                _num(agg.get("copy_net_pnl")) - _num(agg.get("unrealized_pnl")),
            )
            out[n_key] = agg["closed_n"]
            out[f"copy_bt_{days}d_wins"] = _int(agg.get("wins"))
            out[f"copy_bt_{days}d_win_rate"] = (
                out[f"copy_bt_{days}d_wins"] / _int(agg.get("closed_n"))
                if _int(agg.get("closed_n")) else 0.0
            )
            out[f"copy_bt_{days}d_position_win_rate"] = out[f"copy_bt_{days}d_win_rate"]
            out[f"copy_bt_{days}d_unrealized_pnl"] = _num(agg.get("unrealized_pnl"))
            out[f"copy_bt_{days}d_window_start_equity"] = (
                agg.get("window_start_equity")
                if agg.get("window_start_equity") is not None
                else agg.get("initial_margin_equity")
            )
            for key in ("liquidations",):
                out[f"copy_bt_{days}d_{key}"] = agg.get(key)
    out["allowed_sectors"] = sorted(allowed)
    out["evidence_sectors"] = sorted(evidence_sectors)
    return out
