"""Wallet list/detail endpoints for the dashboard API."""

import json
import time

from hyper.copy.copy_policy import load_copy_policy
from hyper.copy.sector import apply_allowed_sector_copy_metrics
from hyper.selection import follow_score
from .common import iso_epoch, q1, qall, recent_roi_pct, score100

NEW_WATCHLIST_WINDOW_SEC = 12 * 3600


def _col(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _json_obj(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _selection_reason_text(row):
    """Translate internal selection states into one operator-facing explanation."""
    reason = str(_col(row, "selection_reason") or "").strip().lower()
    exit_pending = reason.endswith(":exit_pending")
    if exit_pending:
        reason = reason.removesuffix(":exit_pending")
    labels = {
        "portfolio_no_positive_marginal": "组合评估未通过（旧记录）",
        "portfolio_no_profit_improvement": "加入后组合净收益没有提高",
        "portfolio_gain_below_floor": "加入后的收益提升不足",
        "portfolio_marginal_gain_below_floor": "加入后组合收益增幅低于1%",
        "portfolio_recent_stress_loss": "加入后近期组合回放亏损",
        "portfolio_new_liquidation": "加入后组合出现爆仓",
        "portfolio_open_rate_low": "加入后有效开仓率过低",
        "portfolio_capacity_low": "加入后资金容量不足",
        "portfolio_deploy_limit": "加入后超过总部署上限",
        "portfolio_cost_drag_high": "加入后成本占比过高",
        "portfolio_fold_stability_low": "分段回放稳定性不足（少于2段改善）",
        "portfolio_fold_gain_below_floor": "分段回放合计增益不足",
        "portfolio_holdout_not_better": "最近10日留出回放没有改善",
        "portfolio_cost_stress_no_gain": "提高交易成本后组合没有改善",
        "portfolio_cost_stress_liquidation": "成本压力回放新增爆仓",
        "portfolio_fold_constraints_failed": "分段回放未满足资金或覆盖约束",
        "portfolio_robustness_not_improved": "稳健性复核未优于当前组合",
        "portfolio_not_selected": "组合候补：加入后共享账户没有稳健增益",
        "portfolio_negative_incremental_net": "组合候补：移除后共享账户扣费净收益更高",
        "core_eligible": "组合候补：个人资格合格，本轮组合未选中",
        "core_eligible_strong": "组合候补：强证据合格，本轮组合未选中",
        "core_eligible_profit_concentrated": "利润集中：强证据与压力回放已通过",
        "core_eligible_profit_concentrated_body_strong": "利润集中但主体稳定：去前三大盈利后仍持续盈利",
        "challenger_return_watch": "收益观察：达到候选线，尚未达到Core收益线",
        "challenger_sample_watch": "样本观察：未达到30/14/7日 12/5/5 回合或10个30日Campaign",
        "challenger_confidence_watch": "置信观察：LCB或盈利概率尚未达到Core线",
        "challenger_thin_edge_watch": "收益观察：保证金预期收益接近Core经济线",
        "challenger_structural_watch": "结构观察：单次Heavy-DCA压力回放通过，暂不进入Core",
        "challenger_weekly_return_watch": "近期收益不足：7日Copy总收益未达到Core百分比线",
        "challenger_win_rate_watch": "胜率不足：30日Campaign胜率低于60%或Wilson下界低于50%",
        "challenger_execution_watch": "执行不足：有效开仓跟随率低于70%",
        "challenger_capacity_watch": "容量不足：可复制目标篮子低于75%",
        "challenger_profit_structure_watch": "盈利结构不足：严格Copy PF低于1.25",
        "challenger_tail_profit_watch": "尾部不足：移除最大两个Campaign后收益低于3%",
        "challenger_long_deep_bag": "深亏风险：历史8%以上浮亏持续超过24小时",
        "challenger_path_risk_pending": "路径风险待重建：暂不授予Core新开仓权限",
        "challenger_forward_liquidation": "实跟风险：真实跟单已发生爆仓",
        "challenger_open_valuation_pending": "开放仓位估值待确认：暂不进入Core",
        "challenger_profit_concentration": "利润集中：去极值收益尚可，暂留候选观察",
        "challenger_profit_concentration_sample": "利润集中：去前三大盈利后样本不足，暂留观察",
        "copy_profit_concentration_body_weak": "盈利主体不足：去前三大盈利后普通交易多数不赚钱",
        "copy_profit_structure_weak": "盈利结构不足：严格Copy盈亏因子未达标",
        "copy_tail_profit_weak": "盈利结构不足：移除最大两笔盈利后收益过薄",
        "copy_recent_tail_weak": "盈利结构不足：近期收益依赖单一盈利回合",
        "copy_cost_stress_weak": "盈利结构不足：1.5倍成本压力后不盈利",
        "weak_payoff_structure": "盈利结构不足：原始允许板块盈亏比过低",
        "add_metrics_version_mismatch": "回放指标版本不一致，已进入数据隔离",
        "copy_value_below_challenger_floor": "30日Copy总收益低于候选百分比线",
        "copy_recent_value_below_challenger_floor": "7日Copy总收益低于候选百分比线",
        "recent_copy_collapse": "近期严格Copy收益严重恶化",
        "entry_actionable_open_stale": "近期没有可跟随的新开仓",
        "entry_recent_copy_samples_low": "近7日有效Copy样本不足",
        "entry_positive_probability_low": "历史盈利稳定性不足",
        "deferred_data_error": "本轮数据异常，暂不跟随",
        "below_follow_line": "评分未达到跟单线",
        "operator_disabled": "已被手动停用",
        "operator_starred_core": "已由用户星标锁定在跟单列表",
        "operator_starred_disabled": "已星标锁定，但当前被手动停用",
        "exit_only_open_position": "仅管理已有持仓",
        "above_follow_line": "达到跟单线",
        "actionable_open_stale": "近期没有可跟随的新开仓",
        "core_quality_selected": "个人资格与共享账户组合均已通过",
        "core_strong_evidence": "强证据资格与共享账户组合均已通过",
        "challenger_not_nominated": "本轮未进入严格组合候选",
        "core_inactive_72h": "连续72小时没有可跟开仓，已退出跟单",
        "core_replaced_after_confirmation": "连续弱势后被更强候选替换",
        "core_removed_after_confirmation": "连续弱势且移除后组合更优",
        "core_retained_portfolio_value": "近期偏弱，但保留后组合仍更优",
    }
    if reason in labels:
        text = labels[reason]
        return f"{text} · 旧仓退出中" if exit_pending else text
    if reason.startswith("promotion_pending_"):
        progress = reason.removeprefix("promotion_pending_").replace("_of_", "/")
        return f"候选观察中（{progress}轮）"
    if reason.startswith("core_weak_pending_"):
        progress = reason.removeprefix("core_weak_pending_").replace("_of_", "/")
        return f"近期偏弱，等待连续确认（{progress}轮）"
    if reason != "challenger_evidence":
        return "未满足实跟条件" if reason else None

    policy = load_copy_policy()
    closed_7d = int(_col(row, "copy_bt_7d_closed_n") or 0)
    if closed_7d < policy.min_closed_7d:
        return f"近7日有效Copy仅{closed_7d}笔（门槛{policy.min_closed_7d}笔）"
    positive_probability = float(_col(row, "copy_positive_probability") or 0.0)
    if positive_probability < policy.entry_positive_probability:
        return "历史盈利稳定性不足"
    return "实跟条件仍待确认"


def _sector_policy(row):
    policy = _json_obj(_col(row, "sector_policy_json"))
    if not policy:
        return None
    allowed = policy.get("allowed")
    if not isinstance(allowed, list):
        allowed = [
            sector for sector in ("crypto", "stock")
            if isinstance(policy.get(sector), dict) and policy[sector].get("allow")
        ]
        policy = {**policy, "allowed": allowed}
    return policy


def _market_type_from_sector_policy(row):
    """Render the immutable live permission, not the wallet's legacy raw specialty label."""
    policy = _sector_policy(row)
    if policy:
        sectors = list(policy.get("allowed") or policy.get("watch") or ())
        sector_set = {str(value) for value in sectors}
        if {"crypto", "stock"}.issubset(sector_set):
            return "mixed"
        if "crypto" in sector_set:
            return "crypto"
        if "stock" in sector_set:
            return "stock"
    return _col(row, "market_type") or "crypto"


def _score_breakdown(row):
    _score, detail = follow_score.compute_follow_score({
        "score": _col(row, "raw_score", _col(row, "profile_score", _col(row, "score"))),
        "copy_bt_net_pnl": _col(row, "copy_bt_net_pnl"),
        "copy_bt_win_rate": _col(row, "copy_bt_win_rate"),
        "copy_bt_closed_n": _col(row, "copy_bt_closed_n"),
        "copy_bt_open_fill_rate": _col(row, "copy_bt_open_fill_rate"),
        "copy_bt_liquidations": _col(row, "copy_bt_liquidations"),
        "copy_bt_fee_drag": _col(row, "copy_bt_fee_drag"),
        "copy_bt_unrealized_pnl": _col(row, "copy_bt_unrealized_pnl"),
        "copy_bt_valuation_status": _col(row, "copy_bt_valuation_status"),
        "copy_bt_14d_net_pnl": _col(row, "copy_bt_14d_net_pnl"),
        "copy_bt_14d_unrealized_pnl": _col(row, "copy_bt_14d_unrealized_pnl"),
        "copy_bt_14d_closed_n": _col(row, "copy_bt_14d_closed_n"),
        "copy_bt_7d_net_pnl": _col(row, "copy_bt_7d_net_pnl"),
        "copy_bt_7d_unrealized_pnl": _col(row, "copy_bt_7d_unrealized_pnl"),
        "copy_bt_7d_closed_n": _col(row, "copy_bt_7d_closed_n"),
        "copy_expected_return": _col(row, "copy_expected_return"),
        "copy_return_lcb": _col(row, "copy_return_lcb"),
        "copy_return_volatility": _col(row, "copy_return_volatility"),
        "copy_positive_probability": _col(row, "copy_positive_probability"),
        "copy_evidence_days": _col(row, "copy_evidence_days"),
        "copy_recent_return_14d": _col(row, "copy_recent_return_14d"),
        "copy_recent_return_7d": _col(row, "copy_recent_return_7d"),
        "copy_risk_score": _col(row, "copy_risk_score"),
        "execution_score": _col(row, "execution_score"),
        "actionable_open_rate": _col(row, "actionable_open_rate"),
        "capacity_fit": _col(row, "capacity_fit"),
        "open_probability_48h": _col(row, "open_probability_48h"),
        "sector_policy_json": _col(row, "sector_policy_json"),
        "sector_copy_json": _col(row, "sector_copy_json"),
    })
    return {
        "rawScore": score100(detail.get("rawScore")),
        "copyScore": score100(detail.get("copyScore")) if detail.get("copyScore") is not None else None,
        "economicScore": (
            score100(detail.get("economicScore")) if detail.get("economicScore") is not None else None
        ),
        "economicReturnsPct": {
            key: round(float(value) * 100, 2)
            for key, value in (detail.get("economicReturns") or {}).items()
        },
        "confidencePct": round((detail.get("confidence") or 0.0) * 100, 0),
        "copyPnl": detail.get("copyPnl"),
        "copyUnrealizedPnl": {
            "30d": _col(row, "copy_bt_unrealized_pnl"),
            "14d": _col(row, "copy_bt_14d_unrealized_pnl"),
            "7d": _col(row, "copy_bt_7d_unrealized_pnl"),
        },
        "copyValuationStatus": _col(row, "copy_bt_valuation_status"),
        "closedN": detail.get("closedN"),
        "expectedReturnPct": round(detail.get("expectedReturn") * 100, 2) if detail.get("expectedReturn") is not None else None,
        "returnLcbPct": round(detail.get("returnLcb") * 100, 2) if detail.get("returnLcb") is not None else None,
        "positiveProbabilityPct": round(detail.get("positiveProbability") * 100, 1) if detail.get("positiveProbability") is not None else None,
        "evidenceDays": detail.get("evidenceDays"),
        "riskScore": score100(detail.get("riskScore")) if detail.get("riskScore") is not None else None,
        "executionScore": score100(detail.get("executionScore")) if detail.get("executionScore") is not None else None,
        "profitStructure": {
            "profitFactor": detail.get("profitFactor"),
            "payoffRatio": detail.get("payoffRatio"),
            "netAfterTop1": detail.get("netAfterTop1"),
            "netAfterTop2": detail.get("netAfterTop2"),
            "top1ProfitSharePct": (
                round(float(detail.get("top1ProfitShare")) * 100, 1)
                if detail.get("top1ProfitShare") is not None else None
            ),
            "top3ProfitSharePct": (
                round(float(detail.get("top3ProfitShare")) * 100, 1)
                if detail.get("top3ProfitShare") is not None else None
            ),
            "costStressNetPnl": detail.get("costStressNetPnl"),
            "bodyAfterTop3": detail.get("bodyAfterTop3") or {},
        },
        "addMetrics": detail.get("addMetrics") or {},
        "sectorPolicy": _sector_policy(row),
        "reasons": detail.get("reasons") or [],
    }


def _is_new_followed(first_followed_at):
    ts = iso_epoch(first_followed_at)
    return bool(ts and time.time() - ts <= NEW_WATCHLIST_WINDOW_SEC)


def _published_selection_generation(db):
    """Return the explicit selection generation, or None before the migration cut-over.

    A published generation is authoritative even when it deliberately contains zero Core wallets.  The
    existence check therefore lives on ``scan_generation`` rather than ``follow_selection``; otherwise an
    intentionally empty Core set would incorrectly fall back to the legacy score line.
    """
    try:
        row = q1(
            db,
            "SELECT generation FROM scan_generation "
            "WHERE status='published' AND complete=1 AND is_current=1 "
            "ORDER BY id DESC LIMIT 1",
        )
    except Exception:  # noqa: BLE001 - old read-only DBs may predate the migration
        return None
    return _col(row, "generation") if row else None


def _portfolio_replay_summary(db, generation):
    try:
        row = q1(db, "SELECT value FROM auto_tune_state WHERE key='effective_portfolio_replay'")
        payload = _json_obj(_col(row, "value") if row else None)
    except Exception:  # noqa: BLE001 - rolling deploys may predate tuner state
        return None
    if not payload or payload.get("generation") != generation or payload.get("status") != "ok":
        return None
    return payload


def _ep_selected_wallets(db, generation, role, page, size):
    """Serve one role from the immutable selection snapshot.

    The page CTE is intentionally selected first so episode/copy-position aggregates only touch the visible
    rows.  This preserves the endpoint's bounded-query behaviour for large registries.
    """
    total_row = q1(
        db,
        "SELECT COUNT(*) c FROM follow_selection fs "
        "WHERE fs.generation=? AND fs.role=?",
        (generation, role),
    )
    total = (_col(total_row, "c") or 0) if total_row else 0
    cutoff7d = int((time.time() - 7 * 86400) * 1000)
    rows = qall(
        db,
        "WITH page_selected AS ("
        "  SELECT fs.addr,fs.role AS selection_role,fs.reason AS selection_reason,fs.utility,"
        "         fs.selection_rank,COALESCE(tc.pinned,0) AS pinned,tc.pinned_at,"
        "         fs.follow_score AS selection_follow_score,"
        "         CASE WHEN fs.follow_score IS NOT NULL THEN fs.follow_score "
        "              WHEN fs.role!='core' AND fs.utility BETWEEN 0 AND 1 THEN fs.utility "
        "              WHEN sfh.last_followed_generation=fs.generation THEN sfh.last_followed_score "
        "              ELSE NULL END AS legacy_follow_score,"
        "         fs.data_status AS selection_data_status,"
        "         fs.replay_copy_bt_net_pnl,fs.replay_copy_bt_win_rate,fs.replay_copy_bt_closed_n,"
        "         fs.replay_copy_bt_unrealized_pnl,fs.replay_copy_bt_valuation_status,"
        "         fs.replay_copy_bt_7d_net_pnl,"
        "         fs.replay_copy_bt_7d_unrealized_pnl,"
        "         fs.replay_copy_bt_7d_closed_n,fs.replay_sector_copy_json,"
        "         fs.sector_policy_json AS selection_sector_policy_json,"
        "         fs.replayed_at "
        "  FROM follow_selection fs "
        "  LEFT JOIN target_controls tc ON tc.addr=fs.addr "
        "  LEFT JOIN follow_history sfh ON sfh.addr=fs.addr "
        "  WHERE fs.generation=? AND fs.role=? "
        "  ORDER BY CASE WHEN fs.role='core' THEN COALESCE(tc.pinned,0) ELSE 0 END DESC,"
        "      CASE WHEN fs.role='core' AND COALESCE(tc.pinned,0)=1 THEN tc.pinned_at END,"
        "      CASE WHEN fs.role='core' THEN COALESCE(fs.selection_rank,999999) ELSE 0 END,"
        "      CASE WHEN fs.role='core' THEN fs.utility END DESC,"
        "      COALESCE(fs.follow_score,"
        "      CASE WHEN fs.role!='core' AND fs.utility BETWEEN 0 AND 1 THEN fs.utility END,"
        "      CASE WHEN sfh.last_followed_generation=fs.generation THEN sfh.last_followed_score END,-1) DESC,"
        "      fs.addr LIMIT ? OFFSET ?"
        "), ep7 AS ("
        "  SELECT f.addr,COUNT(e.addr) AS closed_7d "
        "  FROM page_selected f LEFT JOIN episode e ON e.addr=f.addr AND e.close_ms>=? GROUP BY f.addr"
        "), ep_all AS ("
        "  SELECT f.addr,COUNT(e.addr) AS episode_total "
        "  FROM page_selected f LEFT JOIN episode e ON e.addr=f.addr GROUP BY f.addr"
        "), copy_stats AS ("
        "  SELECT f.addr,COUNT(cp.pos_id) AS follow_count,"
        "         COALESCE(SUM(CASE WHEN cp.status!='open' THEN cp.realized_pnl ELSE cp.unrealized_pnl END),0) AS fwd_net "
        "  FROM page_selected f LEFT JOIN copy_position cp ON cp.addr=f.addr GROUP BY f.addr"
        ") "
        "SELECT s.addr,s.selection_role,s.selection_reason,s.selection_data_status,s.utility,s.selection_rank,"
        "s.pinned,s.pinned_at,"
        "s.selection_follow_score,s.legacy_follow_score,"
        "w.market_type,w.score,w.top_coin,COALESCE(tc.enabled,1) AS enabled,"
        "fh.first_followed_at,CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_net_pnl ELSE p.copy_bt_net_pnl END AS copy_bt_net_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_unrealized_pnl ELSE p.copy_bt_unrealized_pnl END AS copy_bt_unrealized_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_valuation_status ELSE p.copy_bt_valuation_status END AS copy_bt_valuation_status,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_win_rate ELSE p.copy_bt_win_rate END AS copy_bt_win_rate,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_closed_n ELSE p.copy_bt_closed_n END AS copy_bt_closed_n,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_7d_net_pnl ELSE p.copy_bt_7d_net_pnl END AS copy_bt_7d_net_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_7d_unrealized_pnl ELSE p.copy_bt_7d_unrealized_pnl END AS copy_bt_7d_unrealized_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_7d_closed_n ELSE p.copy_bt_7d_closed_n END AS copy_bt_7d_closed_n,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_sector_copy_json ELSE p.sector_copy_json END AS sector_copy_json,"
        "COALESCE(json_extract(ast.value,'$.sectorPolicy'),"
        "s.selection_sector_policy_json,p.sector_policy_json) AS sector_policy_json,p.data_status,"
        "p.open_events_7d,p.actionable_open_events_7d,"
        "p.copy_positive_probability,"
        "COALESCE(ep7.closed_7d,0) AS closed_7d,COALESCE(ep_all.episode_total,0) AS episode_total,"
        "COALESCE(cs.follow_count,0) AS follow_count,COALESCE(cs.fwd_net,0) AS fwd_net "
        "FROM page_selected s LEFT JOIN watchlist w ON w.addr=s.addr "
        "LEFT JOIN target_controls tc ON tc.addr=s.addr LEFT JOIN profile p ON p.addr=s.addr "
        "LEFT JOIN active_strategy_revision ar ON ar.id=1 "
        "LEFT JOIN strategy_revision sr ON sr.revision=ar.revision "
        "LEFT JOIN json_each(sr.targets_json) ast "
        "ON lower(json_extract(ast.value,'$.addr'))=lower(s.addr) "
        "LEFT JOIN follow_history fh ON fh.addr=s.addr "
        "LEFT JOIN ep7 ON ep7.addr=s.addr LEFT JOIN ep_all ON ep_all.addr=s.addr "
        "LEFT JOIN copy_stats cs ON cs.addr=s.addr "
        "ORDER BY CASE WHEN s.selection_role='core' THEN COALESCE(s.pinned,0) ELSE 0 END DESC,"
        "CASE WHEN s.selection_role='core' AND COALESCE(s.pinned,0)=1 THEN s.pinned_at END,"
        "CASE WHEN s.selection_role='core' THEN COALESCE(s.selection_rank,999999) ELSE 0 END,"
        "CASE WHEN s.selection_role='core' THEN s.utility END DESC,"
        "COALESCE(s.selection_follow_score,s.legacy_follow_score,-1) DESC,s.addr",
        (generation, role, size, page * size, cutoff7d),
    )
    out = []
    for i, r in enumerate(rows):
        display_metrics = apply_allowed_sector_copy_metrics(dict(r))
        published_score = _col(r, "selection_follow_score")
        if published_score is None:
            published_score = _col(r, "legacy_follow_score")
        closed7d = _col(r, "closed_7d") or 0
        if closed7d == 0 and (_col(r, "episode_total") or 0) == 0:
            closed7d = _col(r, "copy_bt_7d_closed_n") or 0
        display_win_rate = (
            _col(display_metrics, "copy_bt_campaign_win_rate")
            if _col(display_metrics, "copy_bt_campaign_closed_n") is not None
            else _col(r, "copy_bt_win_rate")
        )
        out.append({
            "followPos": page * size + i + 1,
            "address": _col(r, "addr"),
            "selectionReasonText": _selection_reason_text(r),
            "marketType": _market_type_from_sector_policy(r),
            "score": score100(published_score) if published_score is not None else None,
            # The list describes the strategy we can actually copy, not the target's raw account win rate.
            # A missing immutable replay/profile value is unknown and must never be rendered as 0%.
            "winRatePct": None if display_win_rate is None else display_win_rate * 100,
            "campaignClosedN": _col(display_metrics, "copy_bt_campaign_closed_n"),
            "mainCoin": _col(r, "top_coin"),
            "followCount": _col(r, "follow_count") or 0,
            "enabled": bool(_col(r, "enabled", True)),
            "starred": bool(_col(r, "pinned", False)),
            "starredAt": _col(r, "pinned_at"),
            "closed7d": closed7d,
            "openEvents7d": (
                _col(r, "open_events_7d")
                if _col(r, "open_events_7d") is not None
                else (_col(r, "actionable_open_events_7d") or 0)
            ),
            "copyBacktestNetPnl": _col(display_metrics, "copy_bt_net_pnl"),
            "copyBacktestUnrealizedPnl": _col(display_metrics, "copy_bt_unrealized_pnl"),
            "copyBacktestValuationStatus": _col(display_metrics, "copy_bt_valuation_status"),
            "copyBacktestClosedN": _col(display_metrics, "copy_bt_closed_n") or 0,
            "copyBacktest7dNetPnl": _col(display_metrics, "copy_bt_7d_net_pnl"),
            "copyBacktest7dUnrealizedPnl": _col(display_metrics, "copy_bt_7d_unrealized_pnl"),
            "copyBacktest7dClosedN": _col(display_metrics, "copy_bt_7d_closed_n") or 0,
            "forwardNetPnl": _col(r, "fwd_net") or 0,
            "isNew": _is_new_followed(_col(r, "first_followed_at")),
            "dataStatus": _col(r, "selection_data_status") or _col(r, "data_status"),
        })
    tab = "followed" if role == "core" else role
    return {
        "selectionMode": True,
        "selectionGeneration": generation,
        "portfolioReplay": _portfolio_replay_summary(db, generation),
        "tab": tab,
        "total": total,
        "followed": total if role == "core" else None,
        "page": page,
        "size": size,
        "wallets": out,
    }


def ep_wallets(db, qs=None):
    qs = qs or {}
    page = max(0, int((qs.get("page", ["0"]))[0]))
    size = min(100, max(1, int((qs.get("size", ["30"]))[0])))

    requested_tab = (qs.get("tab", ["followed"]))[0]
    if requested_tab == "observing":
        requested_tab = "followed"
    selection_generation = _published_selection_generation(db)
    if requested_tab in {"followed", "core", "challenger", "exit_only"}:
        role = "core" if requested_tab in {"followed", "core"} else requested_tab
        if not selection_generation:
            return {"selectionMode": True, "selectionGeneration": None, "tab": requested_tab,
                    "total": 0, "followed": 0 if role == "core" else None,
                    "page": page, "size": size, "wallets": []}
        return _ep_selected_wallets(db, selection_generation, role, page, size)

    if requested_tab == "dropped":
        if not selection_generation:
            return {"selectionMode": True, "selectionGeneration": None, "total": 0,
                    "tab": "dropped", "page": page, "size": size, "wallets": []}
        total_row = q1(db,
            "SELECT COUNT(*) c "
            "FROM follow_history fh JOIN profile p ON p.addr=fh.addr "
            "LEFT JOIN watchlist w ON w.addr=fh.addr "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM follow_selection fs WHERE fs.generation=? AND fs.addr=fh.addr "
            "  AND fs.role='core' AND fs.enabled=1"
            ") AND (fh.last_followed_generation IS NULL OR fh.last_followed_generation<>?"
            ")",
            (selection_generation, selection_generation))
        total = (total_row["c"] if total_row else 0) or 0
        rows = qall(db,
            "WITH drop_events AS ("
            "  SELECT fh0.addr,pa.stamp,pa.source,pa.stage,pa.created_at,"
            "         ROW_NUMBER() OVER (PARTITION BY fh0.addr ORDER BY pa.stamp,pa.id) AS rn "
            "  FROM follow_history fh0 JOIN pipeline_audit pa ON pa.addr=fh0.addr "
            "  WHERE pa.stamp>fh0.last_followed_at AND ("
            "       (pa.stage='profile' AND pa.status IN ('retired','rejected')) "
            "    OR (pa.stage='watchlist' AND pa.status IN ('below_line','disabled')) "
            "    OR (pa.stage='selection' AND pa.status IN ('challenger','exit_only')))"
            ") "
            "SELECT fh.addr,fh.last_followed_at,fh.last_followed_score,"
            "COALESCE(de.stamp,p.last_refreshed,fh.last_followed_at) AS drop_at,"
            "de.source AS drop_source,de.stage AS drop_stage,de.created_at AS drop_decided_at,"
            "COALESCE(w.score,p.score) AS follow_score,p.status,p.reason,"
            "p.market_type,p.win_rate,p.top_coin,w.rank AS rank,"
            "fs.role AS selection_role,fs.reason AS selection_reason,"
            "l.week_roi,l.mon_roi "
            "FROM follow_history fh JOIN profile p ON p.addr=fh.addr "
            "LEFT JOIN watchlist w ON w.addr=fh.addr "
            "LEFT JOIN leaderboard l ON l.addr=fh.addr "
            "LEFT JOIN follow_selection fs ON fs.generation=? AND fs.addr=fh.addr "
            "LEFT JOIN drop_events de ON de.addr=fh.addr AND de.rn=1 "
            "WHERE NOT COALESCE(fs.role='core' AND fs.enabled=1,0) "
            "AND (fh.last_followed_generation IS NULL OR fh.last_followed_generation<>?) "
            "ORDER BY drop_at DESC LIMIT ? OFFSET ?",
            (selection_generation, selection_generation, size, page * size))
        out = [{
            "address": r["addr"], "rank": r["rank"], "marketType": r["market_type"] or "crypto",
            "score": score100(r["follow_score"] or 0.0),
            "lastFollowedScore": score100(r["last_followed_score"] or 0.0),
            "lastFollowedAt": iso_epoch(r["last_followed_at"]),
            "dropAt": iso_epoch(r["drop_at"]),
            "dropSource": r["drop_source"],
            "dropStage": r["drop_stage"],
            "dropDecidedAt": iso_epoch(r["drop_decided_at"]),
            "dropReason": (r["selection_reason"] or "退回挑战池" if r["selection_role"] in {"challenger", "exit_only"}
                else "退出Core" if r["status"] == "active" else {"inactive": "失活", "blowup_loss": "扛单爆亏",
                "spot_hedge": "对冲盘", "not_profitable": "转亏", "irregular": "低频", "grid_dca": "网格",
                "bot_frequency": "高频", "hft_uncopyable": "高频", "spot_dominant": "现货为主"}.get(r["reason"], r["reason"] or "淘汰")),
            "winRatePct": (r["win_rate"] or 0.0) * 100,
            "roiEqPct": recent_roi_pct(r["week_roi"], r["mon_roi"]),
            "mainCoin": r["top_coin"],
        } for r in rows]
        return {"selectionMode": True, "selectionGeneration": selection_generation,
                "total": total, "tab": "dropped",
                "page": page, "size": size, "wallets": out}
    return {"selectionMode": True, "selectionGeneration": selection_generation,
            "tab": requested_tab, "total": 0, "page": page, "size": size, "wallets": []}


def ep_wallet_detail(db, addr, qs=None):
    w = q1(db, "SELECT rank,score FROM watchlist WHERE addr=?", (addr,))
    selection_generation = _published_selection_generation(db)
    pr = q1(db,
            "SELECT p.score,p.win_rate,p.n_trades,p.market_type,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_net_pnl ELSE p.copy_bt_net_pnl END AS copy_bt_net_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_win_rate ELSE p.copy_bt_win_rate END AS copy_bt_win_rate,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_closed_n ELSE p.copy_bt_closed_n END AS copy_bt_closed_n,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_open_fill_rate ELSE p.copy_bt_open_fill_rate END AS copy_bt_open_fill_rate,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_liquidations ELSE p.copy_bt_liquidations END AS copy_bt_liquidations,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_fee_drag ELSE p.copy_bt_fee_drag END AS copy_bt_fee_drag,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_unrealized_pnl ELSE p.copy_bt_unrealized_pnl END AS copy_bt_unrealized_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_valuation_status ELSE p.copy_bt_valuation_status END AS copy_bt_valuation_status,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_14d_net_pnl ELSE p.copy_bt_14d_net_pnl END AS copy_bt_14d_net_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_14d_unrealized_pnl ELSE p.copy_bt_14d_unrealized_pnl END AS copy_bt_14d_unrealized_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_14d_closed_n ELSE p.copy_bt_14d_closed_n END AS copy_bt_14d_closed_n,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_7d_net_pnl ELSE p.copy_bt_7d_net_pnl END AS copy_bt_7d_net_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_7d_unrealized_pnl ELSE p.copy_bt_7d_unrealized_pnl END AS copy_bt_7d_unrealized_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_7d_closed_n ELSE p.copy_bt_7d_closed_n END AS copy_bt_7d_closed_n,"
            "p.copy_expected_return,p.copy_return_lcb,p.copy_return_volatility,"
            "p.copy_positive_probability,p.copy_evidence_days,p.copy_recent_return_14d,"
            "p.copy_recent_return_7d,p.copy_risk_score,p.execution_score,p.actionable_open_rate,"
            "p.capacity_fit,p.open_probability_48h,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_sector_copy_json ELSE p.sector_copy_json END AS sector_copy_json,"
            "p.sector_policy_json,fs.role AS selection_role,fs.reason AS selection_reason,"
            "fs.follow_score AS selection_follow_score,fs.utility AS selection_utility,"
            "fh.last_followed_score,fh.last_followed_generation,"
            "fs.replay_params_hash,fs.replayed_at "
            "FROM profile p LEFT JOIN follow_selection fs ON fs.generation=? AND fs.addr=p.addr "
            "LEFT JOIN follow_history fh ON fh.addr=p.addr "
            "WHERE p.addr=?", (selection_generation, addr))
    agg = q1(db,
             "SELECT COUNT(*) total_n,"
             "SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) closed_n,"
             "SUM(CASE WHEN status!='open' AND realized_pnl>0 THEN 1 ELSE 0 END) wins,"
             "COALESCE(SUM(CASE WHEN status!='open' THEN realized_pnl ELSE 0 END),0) realized,"
             "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_n,"
             "COALESCE(SUM(CASE WHEN status='open' THEN unrealized_pnl ELSE 0 END),0) open_u "
             "FROM copy_position WHERE addr=?", (addr,))
    n = (agg["closed_n"] if agg else 0) or 0
    win_n = (agg["wins"] if agg else 0) or 0
    realized = (agg["realized"] if agg else 0.0) or 0.0
    open_n = (agg["open_n"] if agg else 0) or 0
    open_u = (agg["open_u"] if agg else 0.0) or 0.0
    total_recs = (agg["total_n"] if agg else 0) or 0
    rp = max(0, int((qs.get("recPage", ["0"]))[0])) if qs else 0
    rs = min(50, max(1, int((qs.get("recSize", ["20"]))[0]))) if qs else 20
    recs = qall(db,
        "SELECT cp.pos_id,cp.coin,cp.side,cp.status,cp.realized_pnl,cp.unrealized_pnl,cp.opened_at "
        "FROM copy_position cp WHERE cp.addr=? ORDER BY cp.opened_at DESC LIMIT ? OFFSET ?",
        (addr, rs, rp * rs))
    final_score = _col(pr, "selection_follow_score") if pr else None
    if final_score is None and pr and _col(pr, "selection_role") != "core":
        utility = _col(pr, "selection_utility")
        if utility is not None and 0 <= utility <= 1:
            final_score = utility
    if (final_score is None and pr
            and _col(pr, "last_followed_generation") == selection_generation):
        final_score = _col(pr, "last_followed_score")
    if final_score is None and not _col(pr, "selection_role"):
        final_score = w["score"] if (w and w["score"] is not None) else (pr["score"] if pr else None)
    return {
        "address": addr, "rank": (w["rank"] if w else None),
        "role": (_col(pr, "selection_role") if pr else None),
        "selectionReason": (_col(pr, "selection_reason") if pr else None),
        "selectionReasonText": (_selection_reason_text(pr) if pr else None),
        "marketType": (pr["market_type"] if pr else None),
        "score": score100(final_score) if final_score is not None else None,
        "scoreBreakdown": _score_breakdown(pr) if pr else {},
        "copyReplayParamsHash": (_col(pr, "replay_params_hash") if pr else None),
        "copyReplayedAt": iso_epoch(_col(pr, "replayed_at")) if pr else None,
        "scoredWinRatePct": (pr["win_rate"] * 100) if (pr and pr["win_rate"] is not None) else None,
        "scoredTrades": (pr["n_trades"] if pr else None),
        "forwardWinRatePct": (win_n / n * 100) if n else None,
        "closedN": n, "winN": win_n, "lossN": n - win_n,
        "realizedPnl": realized, "openN": open_n, "openUnrealized": open_u,
        "netPnl": realized + open_u,
        "recordsTotal": total_recs, "recPage": rp, "recSize": rs,
        "records": [{
            "id": r["pos_id"], "coin": r["coin"], "side": r["side"], "status": r["status"],
            "pnl": (r["realized_pnl"] or 0.0) if r["status"] != "open" else (r["unrealized_pnl"] or 0.0),
            "openedAt": r["opened_at"],
        } for r in recs],
    }
