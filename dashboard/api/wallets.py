"""Wallet list/detail endpoints for the dashboard API."""

import json
import time

from hyper.copy.economics import (
    PROFITABILITY_BASIS,
    conservative_profitability,
)
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
        "portfolio_not_selected": "组合候补：未进入本轮评分前缀",
        "portfolio_negative_incremental_net": "组合候补：移除低分尾部后共享账户净收益更高",
        "core_eligible": "组合候补：个人资格合格，本轮组合未选中",
        "official_perp_not_qualified": "官方Perp收益资格未通过",
        "history_under_28d": "官方Perp有资金运行历史不足28天，证据待积累",
        "history_under_7d": "官方Perp有资金运行历史不足7天，证据待积累",
        "boundary_sample_gap": "官方Perp窗口边界样本不足",
        "zero_start_equity": "官方Perp期初权益异常",
        "official_perp_return_below_floor:short_7d": "官方Perp短历史收益低于5%",
        "source_episode_evidence_insufficient": "源钱包30日完整回合少于7个",
        "source_win_rate_below_floor": "源钱包30日胜率低于70%",
        "source_low_frequency_win_rate_below_floor": "强力低频钱包30日胜率低于85%",
        "source_low_frequency_official_return_below_floor": "强力低频钱包官方Perp收益低于30%",
        "source_low_frequency_recent_not_profitable": "强力低频钱包最近7日源净收益未盈利",
        "source_activity_stale": "最近72小时没有真实新开仓",
        "source_concentrated_body_win_rate_low": "大赢家集中且其余交易胜率低于70%",
        "source_concentrated_body_unprofitable": "大赢家集中且其余交易手续费后亏损",
        "source_quality_below_top40": "源质量排序未进入前40",
        "source_30d_closed_pnl_not_positive": "源钱包30日已平净利润不为正",
        "source_7d_closed_pnl_not_positive": "源钱包7日已平净利润不为正",
        "source_open_loss_over_50pct": "源钱包当前浮亏超过30日已平利润的50%",
        "source_30d_conservative_pnl_not_positive": "源钱包扣除当前浮亏后30日不盈利",
        "source_7d_conservative_pnl_not_positive": "源钱包扣除当前浮亏后7日不盈利",
        "copy_episode_evidence_insufficient": "Copy完整已平回合少于7个",
        "rough_copy_30d_not_profitable": "粗略Copy 30日未盈利",
        "rough_copy_7d_not_profitable": "粗略Copy最近7日未盈利",
        "copy_30d_closed_pnl_not_positive": "Copy 30日已平净利润不为正",
        "copy_7d_closed_pnl_not_positive": "Copy 7日已平净利润不为正",
        "copy_open_loss_over_50pct": "Copy当前浮亏超过30日已平利润的50%",
        "rough_copy_30d_conservative_not_profitable": "粗略Copy扣除浮亏后30日未盈利",
        "rough_copy_7d_conservative_not_profitable": "粗略Copy扣除浮亏后7日未盈利",
        "rough_copy_win_rate_below_floor": "粗略Copy胜率低于60%",
        "rough_copy_open_rate_below_floor": "粗略Copy开仓跟随率低于70%",
        "copy_profit_factor_below_1_25": "Copy Profit Factor 低于1.25",
        "source_lottery_profile_rejected": "源钱包收益依赖少数偶发大赢家",
        "copy_lottery_profile_rejected": "Copy收益依赖少数偶发大赢家",
        "no_actionable_open_28d": "最近28日没有可跟的真实开仓",
        "no_actionable_open_7d": "最近7日没有可跟的真实开仓",
        "active_weeks_below_3_of_4": "最近四个7日桶中活跃不足三个",
        "actionable_open_gap_over_10d": "最近28日可跟开仓最大间隔超过10天",
        "strict_copy_30d_return_below_floor": "严格Copy 30日动态收益低于10%",
        "strict_copy_7d_return_below_floor": "严格Copy最近7日动态收益低于3%",
        "strict_copy_30d_conservative_return_below_floor": "严格Copy保守30日收益低于10%",
        "strict_copy_7d_conservative_return_below_floor": "严格Copy保守7日收益低于3%",
        "strict_copy_win_rate_below_floor": "严格Copy胜率低于60%",
        "strict_copy_open_rate_below_floor": "严格Copy开仓跟随率低于70%",
        "activity_over_72h": "最近72小时没有真实新开仓",
        "copy_path_incomplete": "精细价格路径证据尚未完整",
        "strict_copy_liquidations_over_3": "最终参数模拟逐仓爆仓超过3次",
        "copy_single_liquidation_loss_over_5pct": "单次逐仓爆仓损失达到开仓时动态权益的5%",
        "historical_major_liquidation": "历史重大爆仓记录（永久排除候选）",
        "source_account_liquidated_zero": "源钱包本人清算且Perp权益归零",
        "sector_not_executable": "没有数据完整且可执行的Crypto/Stock板块",
        "copy_valuation_incomplete": "开放仓位末端估值待确认",
        "copy_data_error": "严格Copy数据无效，等待重建",
        "deferred_data_error": "本轮数据异常，暂不跟随",
        "operator_disabled": "已被手动停用",
        "exit_only_open_position": "仅管理已有持仓",
        "core_quality_selected": "个人资格与共享账户组合均已通过",
    }
    if reason in labels:
        text = labels[reason]
        return f"{text} · 旧仓退出中" if exit_pending else text
    return "未满足实跟条件" if reason else None


def _copy_economics(metrics, days):
    prefix = "copy_bt" if int(days) == 30 else f"copy_bt_{int(days)}d"
    marked = _col(metrics, f"{prefix}_net_pnl")
    unrealized = _col(metrics, f"{prefix}_unrealized_pnl") or 0.0
    closed = _col(metrics, f"{prefix}_closed_net_pnl")
    if closed is None and marked is not None:
        closed = float(marked) - float(unrealized)
    equity_key = (
        "copy_bt_window_start_equity"
        if int(days) == 30 else f"copy_bt_{int(days)}d_window_start_equity"
    )
    return conservative_profitability(
        closed, unrealized, start_equity=_col(metrics, equity_key),
    )


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
    detail = _json_obj(_col(row, "replay_score_detail_json"))
    if not detail:
        _score, detail = follow_score.compute_follow_score({
            "official_perp_return_30d": _col(row, "official_perp_return_30d"),
            "official_perp_evidence_json": _col(row, "official_perp_evidence_json"),
            "source_win_rate_30d": _col(row, "source_win_rate_30d"),
            "open_events_30d": _col(row, "open_events_30d"),
            "last_copyable_open_ms": _col(row, "last_copyable_open_ms"),
            "copy_bt_net_pnl": _col(row, "copy_bt_net_pnl"),
            "copy_bt_closed_net_pnl": _col(row, "copy_bt_closed_net_pnl"),
            "copy_bt_unrealized_pnl": _col(row, "copy_bt_unrealized_pnl"),
            "copy_bt_window_start_equity": _col(row, "copy_bt_window_start_equity"),
            "copy_bt_win_rate": _col(row, "copy_bt_win_rate"),
            "copy_bt_closed_n": _col(row, "copy_bt_closed_n"),
            "copy_bt_open_fill_rate": _col(row, "copy_bt_open_fill_rate"),
            "copy_bt_7d_net_pnl": _col(row, "copy_bt_7d_net_pnl"),
            "copy_bt_7d_closed_net_pnl": _col(row, "copy_bt_7d_closed_net_pnl"),
            "copy_bt_7d_unrealized_pnl": _col(row, "copy_bt_7d_unrealized_pnl"),
            "copy_bt_7d_window_start_equity": _col(row, "copy_bt_7d_window_start_equity"),
            "actionable_open_rate": _col(
                row, "copy_bt_open_fill_rate", _col(row, "actionable_open_rate")
            ),
            "copy_bt_behavior_replication_rate": _col(
                row, "copy_bt_behavior_replication_rate"
            ),
            "copy_bt_profit_factor": _col(row, "copy_bt_profit_factor"),
            "copy_bt_top3_profit_share": _col(row, "copy_bt_top3_profit_share"),
            "copy_bt_body_after_top3_n": _col(row, "copy_bt_body_after_top3_n"),
            "copy_bt_body_after_top3_net_pnl": _col(
                row, "copy_bt_body_after_top3_net_pnl"
            ),
            "copy_bt_liquidations": _col(row, "copy_bt_liquidations"),
            "copy_bt_max_liquidation_loss_pct": _col(
                row, "copy_bt_max_liquidation_loss_pct"
            ),
            "pre_strict_activity_json": _col(row, "pre_strict_activity_json"),
        })
    return {
        "stage": detail.get("stage"),
        "mode": detail.get("mode"),
        "components": {
            key: score100(value) for key, value in (detail.get("components") or {}).items()
        },
        "profitPriorityPct": (
            round(float(detail["profitPriorityValue"]) * 100, 2)
            if detail.get("profitPriorityValue") is not None else None
        ),
        "profitComponent": score100(detail.get("profitComponent")),
        "reliability": score100(detail.get("reliability")),
        "confidenceMultiplier": detail.get("confidenceMultiplier"),
        "economicReturnsPct": {
            key: round(float(value) * 100, 2)
            for key, value in (detail.get("economicReturns") or {}).items()
        },
        "windowStartEquity": detail.get("economicEquities") or {},
        "copyPnl": detail.get("copyPnl"),
        "closedN": detail.get("closedN"),
        "sourceWinRatePct": (
            round(float(detail["sourceWinRate"]) * 100, 1)
            if detail.get("sourceWinRate") is not None else None
        ),
        "copyWinRatePct": (
            round(float(detail["copyWinRate"]) * 100, 1)
            if detail.get("copyWinRate") is not None else None
        ),
        "openFillRatePct": (
            round(float(detail["openFillRate"]) * 100, 1)
            if detail.get("openFillRate") is not None else None
        ),
        "activityAgeHours": detail.get("activityAgeHours"),
        "liquidations": detail.get("liquidations"),
        "feeDrag": detail.get("feeDrag"),
        "strictQuality": detail.get("strictQuality") or {},
        "preStrict": detail.get("preStrict") or {},
        "profitPriority": detail.get("profitPriority") or {},
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
    cutoff30d = int((time.time() - 30 * 86400) * 1000)
    rows = qall(
        db,
        "WITH page_selected AS ("
        "  SELECT fs.addr,fs.role AS selection_role,fs.reason AS selection_reason,fs.utility,"
        "         fs.selection_rank,fs.replay_profit_priority,fs.model_version,"
        "         COALESCE(tc.pinned,0) AS pinned,tc.pinned_at,"
        "         fs.follow_score AS selection_follow_score,"
        "         CASE WHEN fs.follow_score IS NOT NULL THEN fs.follow_score "
        "              WHEN fs.role!='core' AND fs.utility BETWEEN 0 AND 1 THEN fs.utility "
        "              WHEN sfh.last_followed_generation=fs.generation THEN sfh.last_followed_score "
        "              ELSE NULL END AS legacy_follow_score,"
        "         fs.data_status AS selection_data_status,"
        "         fs.replay_copy_bt_net_pnl,fs.replay_copy_bt_closed_net_pnl,"
        "         fs.replay_copy_bt_window_start_equity,"
        "         fs.replay_copy_bt_win_rate,fs.replay_copy_bt_closed_n,"
        "         fs.replay_copy_bt_open_fill_rate,fs.replay_copy_bt_liquidations,"
        "         fs.replay_copy_bt_raw_target_open_n,fs.replay_copy_bt_small_open_excluded_n,"
        "         fs.replay_copy_bt_effective_target_open_n,fs.replay_copy_bt_opened_n,"
        "         fs.replay_copy_bt_raw_open_capture_rate,fs.replay_copy_bt_open_audit_json,"
        "         fs.replay_copy_bt_fee_drag,"
        "         fs.replay_copy_bt_unrealized_pnl,fs.replay_copy_bt_valuation_status,"
        "         fs.replay_copy_bt_7d_net_pnl,fs.replay_copy_bt_7d_closed_net_pnl,"
        "         fs.replay_copy_bt_7d_window_start_equity,"
        "         fs.replay_copy_bt_7d_unrealized_pnl,"
        "         fs.replay_copy_bt_7d_closed_n,fs.replay_sector_copy_json,"
        "         fs.sector_policy_json AS selection_sector_policy_json,"
        "         fs.replayed_at "
        "  FROM follow_selection fs "
        "  LEFT JOIN target_controls tc ON tc.addr=fs.addr "
        "  LEFT JOIN follow_history sfh ON sfh.addr=fs.addr "
        "  WHERE fs.generation=? AND fs.role=? "
        "  ORDER BY COALESCE(fs.selection_rank,999999),fs.addr LIMIT ? OFFSET ?"
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
        "), live_liquidity AS ("
        "  SELECT f.addr,COALESCE(SUM(l.count),0) AS live_liquidity_skip_n,"
        "         GROUP_CONCAT(DISTINCT l.coin) AS live_liquidity_skip_coins "
        "  FROM page_selected f LEFT JOIN live_policy_skip l "
        "    ON l.addr=f.addr AND l.last_ms>=? "
        "  GROUP BY f.addr"
        ") "
        "SELECT s.addr,s.selection_role,s.selection_reason,s.selection_data_status,s.utility,s.selection_rank,"
        "s.replay_profit_priority,s.model_version,"
        "s.pinned,s.pinned_at,"
        "s.selection_follow_score,s.legacy_follow_score,"
        "w.market_type,w.score,w.top_coin,COALESCE(tc.enabled,1) AS enabled,"
        "fh.first_followed_at,s.replayed_at AS strict_replayed_at,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_net_pnl ELSE p.copy_bt_net_pnl END AS copy_bt_net_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_closed_net_pnl ELSE p.copy_bt_closed_net_pnl END AS copy_bt_closed_net_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_window_start_equity ELSE p.copy_bt_window_start_equity END AS copy_bt_window_start_equity,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_unrealized_pnl ELSE p.copy_bt_unrealized_pnl END AS copy_bt_unrealized_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_valuation_status ELSE p.copy_bt_valuation_status END AS copy_bt_valuation_status,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_win_rate ELSE p.copy_bt_win_rate END AS copy_bt_win_rate,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_closed_n ELSE p.copy_bt_closed_n END AS copy_bt_closed_n,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_open_fill_rate ELSE p.copy_bt_open_fill_rate END AS copy_bt_open_fill_rate,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_raw_target_open_n ELSE p.copy_bt_raw_target_open_n END AS copy_bt_raw_target_open_n,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_small_open_excluded_n ELSE p.copy_bt_small_open_excluded_n END AS copy_bt_small_open_excluded_n,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_effective_target_open_n ELSE p.copy_bt_effective_target_open_n END AS copy_bt_effective_target_open_n,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_opened_n ELSE p.copy_bt_opened_n END AS copy_bt_opened_n,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_raw_open_capture_rate ELSE p.copy_bt_raw_open_capture_rate END AS copy_bt_raw_open_capture_rate,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_open_audit_json ELSE p.copy_bt_open_audit_json END AS copy_bt_open_audit_json,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_liquidations ELSE p.copy_bt_liquidations END AS copy_bt_liquidations,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_fee_drag ELSE p.copy_bt_fee_drag END AS copy_bt_fee_drag,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_7d_net_pnl ELSE p.copy_bt_7d_net_pnl END AS copy_bt_7d_net_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_7d_closed_net_pnl ELSE p.copy_bt_7d_closed_net_pnl END AS copy_bt_7d_closed_net_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_7d_window_start_equity ELSE p.copy_bt_7d_window_start_equity END AS copy_bt_7d_window_start_equity,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_7d_unrealized_pnl ELSE p.copy_bt_7d_unrealized_pnl END AS copy_bt_7d_unrealized_pnl,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_copy_bt_7d_closed_n ELSE p.copy_bt_7d_closed_n END AS copy_bt_7d_closed_n,"
        "CASE WHEN s.replayed_at IS NOT NULL THEN s.replay_sector_copy_json ELSE p.sector_copy_json END AS sector_copy_json,"
        "COALESCE(json_extract(ast.value,'$.sectorPolicy'),"
        "s.selection_sector_policy_json,p.sector_policy_json) AS sector_policy_json,p.data_status,"
        "p.official_perp_status,p.official_perp_reason,p.official_perp_evidence_json,"
        "p.official_perp_return_30d,p.official_perp_pnl_30d,p.official_perp_pnl_share,"
        "p.source_episode_n_30d,p.source_episode_n_7d,p.source_win_rate_30d,p.source_win_rate_7d,"
        "p.source_net_pnl_30d,p.source_net_pnl_7d,p.open_unrealized,p.source_top3_profit_share,"
        "p.source_body_after_top3_n,p.source_body_after_top3_win_rate,"
        "p.source_body_after_top3_net_pnl,p.source_quality_score,p.rough_copy_score,"
        "p.last_copyable_open_ms,p.open_events_30d,p.open_events_7d,p.actionable_open_events_7d,"
        "p.copy_bt_net_pnl AS rough_copy_marked_net_pnl,"
        "p.copy_bt_closed_net_pnl AS rough_copy_closed_net_pnl,"
        "p.copy_bt_unrealized_pnl AS rough_copy_unrealized_pnl,"
        "p.copy_bt_window_start_equity AS rough_copy_start_equity,"
        "p.copy_bt_7d_net_pnl AS rough_copy_7d_marked_net_pnl,"
        "p.copy_bt_7d_closed_net_pnl AS rough_copy_7d_closed_net_pnl,"
        "p.copy_bt_7d_unrealized_pnl AS rough_copy_7d_unrealized_pnl,"
        "p.copy_bt_7d_window_start_equity AS rough_copy_7d_start_equity,"
        "p.copy_bt_win_rate AS rough_copy_win_rate,p.copy_bt_closed_n AS rough_copy_closed_n,"
        "p.copy_bt_open_fill_rate AS rough_copy_open_fill_rate,"
        "COALESCE(ep7.closed_7d,0) AS closed_7d,COALESCE(ep_all.episode_total,0) AS episode_total,"
        "COALESCE(cs.follow_count,0) AS follow_count,COALESCE(cs.fwd_net,0) AS fwd_net,"
        "COALESCE(ll.live_liquidity_skip_n,0) AS live_liquidity_skip_n,"
        "ll.live_liquidity_skip_coins "
        "FROM page_selected s LEFT JOIN watchlist w ON w.addr=s.addr "
        "LEFT JOIN target_controls tc ON tc.addr=s.addr LEFT JOIN profile p ON p.addr=s.addr "
        "LEFT JOIN active_strategy_revision ar ON ar.id=1 "
        "LEFT JOIN strategy_revision sr ON sr.revision=ar.revision "
        "LEFT JOIN json_each(sr.targets_json) ast "
        "ON lower(json_extract(ast.value,'$.addr'))=lower(s.addr) "
        "LEFT JOIN follow_history fh ON fh.addr=s.addr "
        "LEFT JOIN ep7 ON ep7.addr=s.addr LEFT JOIN ep_all ON ep_all.addr=s.addr "
        "LEFT JOIN copy_stats cs ON cs.addr=s.addr "
        "LEFT JOIN live_liquidity ll ON ll.addr=s.addr "
        "ORDER BY COALESCE(s.selection_rank,999999),s.addr",
        (generation, role, size, page * size, cutoff7d, cutoff30d),
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
        display_win_rate = _col(display_metrics, "copy_bt_win_rate")
        equity30 = _col(display_metrics, "copy_bt_window_start_equity")
        if equity30 is None:
            equity30 = _col(display_metrics, "copy_bt_initial_margin_equity")
        equity7 = _col(display_metrics, "copy_bt_7d_window_start_equity")
        marked30 = _col(display_metrics, "copy_bt_net_pnl")
        marked7 = _col(display_metrics, "copy_bt_7d_net_pnl")
        economic30 = _copy_economics(display_metrics, 30)
        economic7 = _copy_economics(display_metrics, 7)
        rough_economic30 = conservative_profitability(
            _col(r, "rough_copy_closed_net_pnl"),
            _col(r, "rough_copy_unrealized_pnl"),
            start_equity=_col(r, "rough_copy_start_equity"),
        )
        rough_economic7 = conservative_profitability(
            _col(r, "rough_copy_7d_closed_net_pnl"),
            _col(r, "rough_copy_7d_unrealized_pnl"),
            start_equity=_col(r, "rough_copy_7d_start_equity"),
        )
        net30 = economic30["qualificationPnl"]
        net7 = economic7["qualificationPnl"]
        official_evidence = _json_obj(_col(r, "official_perp_evidence_json"))
        official_window = (
            (official_evidence.get("windows") or {}).get("officialPerp30d") or {}
        )
        open_audit = _json_obj(_col(display_metrics, "copy_bt_open_audit_json"))
        out.append({
            "followPos": page * size + i + 1,
            "address": _col(r, "addr"),
            "selectionReasonText": _selection_reason_text(r),
            "marketType": _market_type_from_sector_policy(r),
            "score": score100(published_score) if published_score is not None else None,
            "profitPriorityPct": (
                _col(r, "replay_profit_priority") * 100
                if _col(r, "replay_profit_priority") is not None else None
            ),
            "profitRank": _col(r, "selection_rank"),
            "rankingMode": (
                follow_score.FOLLOW_SCORE_MODE
                if _col(r, "replay_profit_priority") is not None else None
            ),
            "profitabilityBasis": (
                PROFITABILITY_BASIS
                if (
                    str(_col(r, "model_version") or "").endswith("v2")
                    or "pre-strict32" in str(_col(r, "model_version") or "")
                )
                else "legacy_marked_pnl"
            ),
            # The list describes the strategy we can actually copy, not the target's raw account win rate.
            # A missing immutable replay/profile value is unknown and must never be rendered as 0%.
            "winRatePct": None if display_win_rate is None else display_win_rate * 100,
            "sourceWinRatePct": (
                _col(r, "source_win_rate_30d") * 100
                if _col(r, "source_win_rate_30d") is not None else None
            ),
            "sourceEpisodeN30d": _col(r, "source_episode_n_30d"),
            "copyReplayStage": (
                "strict" if _col(r, "strict_replayed_at") is not None else "rough"
            ),
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
            "copyBacktestNetPnl": net30,
            "copyBacktestMarkedNetPnl": marked30,
            "copyBacktestClosedNetPnl": economic30["closedPnl"],
            "copyBacktestOpenProfitReference": economic30["openProfitReference"],
            "copyBacktestOpenLoss": economic30["openLoss"],
            "copyBacktestOpenLossRatioPct": (
                economic30["openLossRatio"] * 100
                if economic30["openLossRatio"] is not None else None
            ),
            "copyBacktestStartEquity": equity30,
            "copyBacktestReturnPct": (
                net30 / equity30 * 100 if net30 is not None and equity30 else None
            ),
            "copyBacktestUnrealizedPnl": _col(display_metrics, "copy_bt_unrealized_pnl"),
            "copyBacktestValuationStatus": _col(display_metrics, "copy_bt_valuation_status"),
            "copyBacktestClosedN": _col(display_metrics, "copy_bt_closed_n") or 0,
            "copyBacktest7dNetPnl": net7,
            "copyBacktest7dMarkedNetPnl": marked7,
            "copyBacktest7dClosedNetPnl": economic7["closedPnl"],
            "copyBacktest7dOpenProfitReference": economic7["openProfitReference"],
            "copyBacktest7dOpenLoss": economic7["openLoss"],
            "copyBacktest7dStartEquity": equity7,
            "copyBacktest7dReturnPct": (
                net7 / equity7 * 100 if net7 is not None and equity7 else None
            ),
            "copyBacktest7dUnrealizedPnl": _col(display_metrics, "copy_bt_7d_unrealized_pnl"),
            "copyBacktest7dClosedN": _col(display_metrics, "copy_bt_7d_closed_n") or 0,
            "fee30d": _col(display_metrics, "copy_bt_fee_drag"),
            "openFollowRatePct": (
                _col(display_metrics, "copy_bt_open_fill_rate") * 100
                if _col(display_metrics, "copy_bt_open_fill_rate") is not None else None
            ),
            "rawOpenCaptureRatePct": (
                _col(display_metrics, "copy_bt_raw_open_capture_rate") * 100
                if _col(display_metrics, "copy_bt_raw_open_capture_rate") is not None else None
            ),
            "rawTargetOpenN": _col(display_metrics, "copy_bt_raw_target_open_n"),
            "smallOpenExcludedN": _col(
                display_metrics, "copy_bt_small_open_excluded_n"
            ),
            "effectiveTargetOpenN": _col(
                display_metrics, "copy_bt_effective_target_open_n"
            ),
            "historicalOpenedN": _col(display_metrics, "copy_bt_opened_n"),
            "openExecutionAudit": open_audit,
            "liveLiquiditySkipN30d": _col(r, "live_liquidity_skip_n") or 0,
            "liveLiquiditySkipCoins30d": sorted(filter(
                None, str(_col(r, "live_liquidity_skip_coins") or "").split(","),
            )),
            "liquidations30d": _col(display_metrics, "copy_bt_liquidations"),
            "roughCopyScore": score100(_col(r, "rough_copy_score")),
            "roughCopyNetPnl": rough_economic30["qualificationPnl"],
            "roughCopyMarkedNetPnl": _col(r, "rough_copy_marked_net_pnl"),
            "roughCopyClosedNetPnl": rough_economic30["closedPnl"],
            "roughCopyOpenProfitReference": rough_economic30["openProfitReference"],
            "roughCopyOpenLoss": rough_economic30["openLoss"],
            "roughCopyOpenLossRatioPct": (
                rough_economic30["openLossRatio"] * 100
                if rough_economic30["openLossRatio"] is not None else None
            ),
            "roughCopyReturnPct": (
                rough_economic30["qualificationReturn"] * 100
                if rough_economic30["qualificationReturn"] is not None else None
            ),
            "roughCopy7dNetPnl": rough_economic7["qualificationPnl"],
            "roughCopy7dMarkedNetPnl": _col(r, "rough_copy_7d_marked_net_pnl"),
            "roughCopy7dClosedNetPnl": rough_economic7["closedPnl"],
            "roughCopy7dOpenProfitReference": rough_economic7["openProfitReference"],
            "roughCopy7dOpenLoss": rough_economic7["openLoss"],
            "roughCopy7dReturnPct": (
                rough_economic7["qualificationReturn"] * 100
                if rough_economic7["qualificationReturn"] is not None else None
            ),
            "roughCopyWinRatePct": (
                _col(r, "rough_copy_win_rate") * 100
                if _col(r, "rough_copy_win_rate") is not None else None
            ),
            "roughCopyClosedN": _col(r, "rough_copy_closed_n"),
            "roughCopyOpenRatePct": (
                _col(r, "rough_copy_open_fill_rate") * 100
                if _col(r, "rough_copy_open_fill_rate") is not None else None
            ),
            "officialPerpStatus": _col(r, "official_perp_status"),
            "officialPerpReason": _col(r, "official_perp_reason"),
            "officialPerpReturn30dPct": (
                _col(r, "official_perp_return_30d") * 100
                if _col(r, "official_perp_return_30d") is not None else None
            ),
            "officialPerpReturnPct": (
                _col(r, "official_perp_return_30d") * 100
                if _col(r, "official_perp_return_30d") is not None else None
            ),
            "officialPerpHistoryTier": official_window.get("historyTier"),
            "officialPerpWindowDays": official_window.get("windowDays"),
            "officialPerpEvidence": official_evidence,
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
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_closed_net_pnl ELSE p.copy_bt_closed_net_pnl END AS copy_bt_closed_net_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_window_start_equity ELSE p.copy_bt_window_start_equity END AS copy_bt_window_start_equity,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_win_rate ELSE p.copy_bt_win_rate END AS copy_bt_win_rate,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_closed_n ELSE p.copy_bt_closed_n END AS copy_bt_closed_n,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_open_fill_rate ELSE p.copy_bt_open_fill_rate END AS copy_bt_open_fill_rate,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_raw_target_open_n ELSE p.copy_bt_raw_target_open_n END AS copy_bt_raw_target_open_n,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_small_open_excluded_n ELSE p.copy_bt_small_open_excluded_n END AS copy_bt_small_open_excluded_n,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_effective_target_open_n ELSE p.copy_bt_effective_target_open_n END AS copy_bt_effective_target_open_n,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_opened_n ELSE p.copy_bt_opened_n END AS copy_bt_opened_n,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_raw_open_capture_rate ELSE p.copy_bt_raw_open_capture_rate END AS copy_bt_raw_open_capture_rate,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_open_audit_json ELSE p.copy_bt_open_audit_json END AS copy_bt_open_audit_json,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_liquidations ELSE p.copy_bt_liquidations END AS copy_bt_liquidations,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_fee_drag ELSE p.copy_bt_fee_drag END AS copy_bt_fee_drag,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_unrealized_pnl ELSE p.copy_bt_unrealized_pnl END AS copy_bt_unrealized_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_valuation_status ELSE p.copy_bt_valuation_status END AS copy_bt_valuation_status,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_14d_net_pnl ELSE p.copy_bt_14d_net_pnl END AS copy_bt_14d_net_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_14d_closed_net_pnl ELSE p.copy_bt_14d_closed_net_pnl END AS copy_bt_14d_closed_net_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_14d_unrealized_pnl ELSE p.copy_bt_14d_unrealized_pnl END AS copy_bt_14d_unrealized_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_14d_closed_n ELSE p.copy_bt_14d_closed_n END AS copy_bt_14d_closed_n,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_7d_net_pnl ELSE p.copy_bt_7d_net_pnl END AS copy_bt_7d_net_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_7d_closed_net_pnl ELSE p.copy_bt_7d_closed_net_pnl END AS copy_bt_7d_closed_net_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_7d_window_start_equity ELSE p.copy_bt_7d_window_start_equity END AS copy_bt_7d_window_start_equity,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_7d_unrealized_pnl ELSE p.copy_bt_7d_unrealized_pnl END AS copy_bt_7d_unrealized_pnl,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_copy_bt_7d_closed_n ELSE p.copy_bt_7d_closed_n END AS copy_bt_7d_closed_n,"
            "p.official_perp_return_30d,p.official_perp_status,p.official_perp_reason,"
            "p.official_perp_evidence_json,"
            "p.source_episode_n_30d,p.source_episode_n_7d,p.source_win_rate_30d,"
            "p.source_win_rate_7d,p.source_net_pnl_30d,p.source_net_pnl_7d,p.open_unrealized,"
            "p.source_top3_profit_share,p.source_body_after_top3_n,"
            "p.source_body_after_top3_win_rate,p.source_body_after_top3_net_pnl,"
            "p.source_gross_profit_30d,p.source_gross_loss_30d,"
            "p.source_profit_factor_30d,p.source_payoff_ratio_30d,"
            "p.copy_bt_profit_factor,p.copy_bt_payoff_ratio,p.copy_bt_top3_profit_share,"
            "p.copy_bt_body_after_top3_n,p.copy_bt_body_after_top3_win_rate,"
            "p.copy_bt_body_after_top3_net_pnl,"
            "p.source_quality_score,p.rough_copy_score,p.last_copyable_open_ms,"
            "p.open_events_30d,p.actionable_open_rate,"
            "pse.policy_version AS pre_strict_policy_version,pse.status AS pre_strict_status,"
            "pse.first_failure AS pre_strict_first_failure,pse.tier AS pre_strict_tier,"
            "pse.queue_rank AS pre_strict_queue_rank,pse.activity_json AS pre_strict_activity_json,"
            "pse.strict_status AS pre_strict_strict_status,"
            "pse.strict_first_failure AS pre_strict_strict_first_failure,"
            "CASE WHEN fs.replayed_at IS NOT NULL THEN fs.replay_sector_copy_json ELSE p.sector_copy_json END AS sector_copy_json,"
            "p.sector_policy_json,fs.role AS selection_role,fs.reason AS selection_reason,"
            "fs.follow_score AS selection_follow_score,fs.utility AS selection_utility,"
            "fs.replay_profit_priority,fs.selection_rank,fs.model_version,"
            "fh.last_followed_score,fh.last_followed_generation,"
            "fs.replay_params_hash,fs.replay_score_detail_json,fs.replayed_at "
            "FROM profile p LEFT JOIN follow_selection fs ON fs.generation=? AND fs.addr=p.addr "
            "LEFT JOIN pre_strict_evidence pse ON pse.generation=? AND lower(pse.addr)=lower(p.addr) "
            "LEFT JOIN follow_history fh ON fh.addr=p.addr "
            "WHERE p.addr=?", (selection_generation, selection_generation, addr))
    agg = q1(db,
             "SELECT COUNT(*) total_n,"
             "SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) closed_n,"
             "SUM(CASE WHEN status!='open' AND realized_pnl>0 THEN 1 ELSE 0 END) wins,"
             "COALESCE(SUM(CASE WHEN status!='open' THEN realized_pnl ELSE 0 END),0) realized,"
             "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_n,"
             "COALESCE(SUM(CASE WHEN status='open' THEN unrealized_pnl ELSE 0 END),0) open_u "
             "FROM copy_position WHERE addr=?", (addr,))
    live_liquidity = q1(
        db,
        "SELECT COALESCE(SUM(count),0) AS skip_n,GROUP_CONCAT(DISTINCT coin) AS coins "
        "FROM live_policy_skip WHERE addr=? AND last_ms>=?",
        (addr, int((time.time() - 30 * 86_400) * 1000)),
    )
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
    official_evidence = (
        _json_obj(_col(pr, "official_perp_evidence_json")) if pr else {}
    )
    official_window = (
        (official_evidence.get("windows") or {}).get("officialPerp30d") or {}
    )
    display_metrics = apply_allowed_sector_copy_metrics(dict(pr)) if pr else {}
    copy_economic30 = _copy_economics(display_metrics, 30)
    copy_economic7 = _copy_economics(display_metrics, 7)
    source_economic30 = conservative_profitability(
        _col(pr, "source_net_pnl_30d") if pr else None,
        _col(pr, "open_unrealized") if pr else None,
    )
    source_economic7 = conservative_profitability(
        _col(pr, "source_net_pnl_7d") if pr else None,
        _col(pr, "open_unrealized") if pr else None,
    )
    score_breakdown = _score_breakdown(pr) if pr else {}
    strict_quality = dict(score_breakdown.get("strictQuality") or {})
    copy_quality = {
        "profitFactor": strict_quality.get(
            "profitFactor", _col(pr, "copy_bt_profit_factor") if pr else None
        ),
        "payoffRatio": strict_quality.get(
            "payoffRatio", _col(pr, "copy_bt_payoff_ratio") if pr else None
        ),
        "top3ProfitSharePct": (
            strict_quality.get("top3ProfitShare") * 100
            if strict_quality.get("top3ProfitShare") is not None
            else (
                _col(pr, "copy_bt_top3_profit_share") * 100
                if pr and _col(pr, "copy_bt_top3_profit_share") is not None else None
            )
        ),
        "bodyAfterTop3N": strict_quality.get(
            "bodyAfterTop3N", _col(pr, "copy_bt_body_after_top3_n") if pr else None
        ),
        "bodyAfterTop3WinRatePct": (
            strict_quality.get("bodyAfterTop3WinRate") * 100
            if strict_quality.get("bodyAfterTop3WinRate") is not None
            else (
                _col(pr, "copy_bt_body_after_top3_win_rate") * 100
                if pr and _col(pr, "copy_bt_body_after_top3_win_rate") is not None
                else None
            )
        ),
        "bodyAfterTop3NetPnl": strict_quality.get(
            "bodyAfterTop3NetPnl",
            _col(pr, "copy_bt_body_after_top3_net_pnl") if pr else None,
        ),
    }
    pre_strict_activity = (
        (score_breakdown.get("preStrict") or {}).get("activity")
        or _json_obj(_col(pr, "pre_strict_activity_json") if pr else None)
    )
    return {
        "address": addr, "rank": (w["rank"] if w else None),
        "role": (_col(pr, "selection_role") if pr else None),
        "selectionReason": (_col(pr, "selection_reason") if pr else None),
        "selectionReasonText": (_selection_reason_text(pr) if pr else None),
        "marketType": (pr["market_type"] if pr else None),
        "score": score100(final_score) if final_score is not None else None,
        "profitPriorityPct": (
            _col(pr, "replay_profit_priority") * 100
            if pr and _col(pr, "replay_profit_priority") is not None else None
        ),
        "profitRank": (_col(pr, "selection_rank") if pr else None),
        "rankingMode": (
            follow_score.FOLLOW_SCORE_MODE
            if pr and _col(pr, "replay_profit_priority") is not None else None
        ),
        "profitabilityBasis": (
            PROFITABILITY_BASIS
            if (
                str(_col(pr, "model_version") or "").endswith("v2")
                or "pre-strict32" in str(_col(pr, "model_version") or "")
            )
            else "legacy_marked_pnl"
        ),
        "copyProfitability": {
            "30d": copy_economic30,
            "7d": copy_economic7,
            "markedPnl": {
                "30d": _col(display_metrics, "copy_bt_net_pnl"),
                "7d": _col(display_metrics, "copy_bt_7d_net_pnl"),
            },
        },
        "scoreBreakdown": score_breakdown,
        "copyQuality": copy_quality,
        "preStrict": {
            "policyVersion": (
                (score_breakdown.get("preStrict") or {}).get("policyVersion")
                or (_col(pr, "pre_strict_policy_version") if pr else None)
            ),
            "status": _col(pr, "pre_strict_status") if pr else None,
            "firstFailure": _col(pr, "pre_strict_first_failure") if pr else None,
            "tier": (
                (score_breakdown.get("preStrict") or {}).get("tier")
                or (_col(pr, "pre_strict_tier") if pr else None)
            ),
            "queueRank": (
                (score_breakdown.get("preStrict") or {}).get("queueRank")
                or (_col(pr, "pre_strict_queue_rank") if pr else None)
            ),
            "strictStatus": _col(pr, "pre_strict_strict_status") if pr else None,
            "strictFirstFailure": (
                _col(pr, "pre_strict_strict_first_failure") if pr else None
            ),
            "activity": pre_strict_activity,
        },
        "copyReplayParamsHash": (_col(pr, "replay_params_hash") if pr else None),
        "copyReplayedAt": iso_epoch(_col(pr, "replayed_at")) if pr else None,
        "copyReplayStage": "strict" if pr and _col(pr, "replayed_at") else "rough",
        "copyExecution": {
            "effectiveFollowRatePct": (
                _col(pr, "copy_bt_open_fill_rate") * 100
                if pr and _col(pr, "copy_bt_open_fill_rate") is not None else None
            ),
            "rawCaptureRatePct": (
                _col(pr, "copy_bt_raw_open_capture_rate") * 100
                if pr and _col(pr, "copy_bt_raw_open_capture_rate") is not None else None
            ),
            "rawTargetOpenN": _col(pr, "copy_bt_raw_target_open_n") if pr else None,
            "smallOpenExcludedN": (
                _col(pr, "copy_bt_small_open_excluded_n") if pr else None
            ),
            "effectiveTargetOpenN": (
                _col(pr, "copy_bt_effective_target_open_n") if pr else None
            ),
            "openedN": _col(pr, "copy_bt_opened_n") if pr else None,
            "historicalAudit": (
                _json_obj(_col(pr, "copy_bt_open_audit_json")) if pr else {}
            ),
            "liveLiquiditySkipN30d": (
                _col(live_liquidity, "skip_n", 0) or 0
            ),
            "liveLiquiditySkipCoins30d": sorted(filter(
                None,
                str(_col(live_liquidity, "coins") or "").split(","),
            )),
        },
        "officialPerpReturn30dPct": (
            _col(pr, "official_perp_return_30d") * 100
            if pr and _col(pr, "official_perp_return_30d") is not None else None
        ),
        "officialPerpReturnPct": (
            _col(pr, "official_perp_return_30d") * 100
            if pr and _col(pr, "official_perp_return_30d") is not None else None
        ),
        "officialPerpHistoryTier": official_window.get("historyTier"),
        "officialPerpWindowDays": official_window.get("windowDays"),
        "officialPerpStatus": _col(pr, "official_perp_status") if pr else None,
        "officialPerpReason": _col(pr, "official_perp_reason") if pr else None,
        "officialPerpEvidence": official_evidence,
        "sourceQuality": {
            "score": score100(_col(pr, "source_quality_score")) if pr else None,
            "episodeN30d": _col(pr, "source_episode_n_30d") if pr else None,
            "episodeN7d": _col(pr, "source_episode_n_7d") if pr else None,
            "winRate30dPct": (
                _col(pr, "source_win_rate_30d") * 100
                if pr and _col(pr, "source_win_rate_30d") is not None else None
            ),
            "winRate7dPct": (
                _col(pr, "source_win_rate_7d") * 100
                if pr and _col(pr, "source_win_rate_7d") is not None else None
            ),
            "netPnl30d": _col(pr, "source_net_pnl_30d") if pr else None,
            "netPnl7d": _col(pr, "source_net_pnl_7d") if pr else None,
            "currentUnrealizedPnl": _col(pr, "open_unrealized") if pr else None,
            "economics30d": source_economic30,
            "economics7d": source_economic7,
            "top3ProfitSharePct": (
                _col(pr, "source_top3_profit_share") * 100
                if pr and _col(pr, "source_top3_profit_share") is not None else None
            ),
            "bodyAfterTop3N": _col(pr, "source_body_after_top3_n") if pr else None,
            "bodyAfterTop3WinRatePct": (
                _col(pr, "source_body_after_top3_win_rate") * 100
                if pr and _col(pr, "source_body_after_top3_win_rate") is not None else None
            ),
            "bodyAfterTop3NetPnl": (
                _col(pr, "source_body_after_top3_net_pnl") if pr else None
            ),
            "grossProfit30d": _col(pr, "source_gross_profit_30d") if pr else None,
            "grossLoss30d": _col(pr, "source_gross_loss_30d") if pr else None,
            "profitFactor30d": _col(pr, "source_profit_factor_30d") if pr else None,
            "payoffRatio30d": _col(pr, "source_payoff_ratio_30d") if pr else None,
        },
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
