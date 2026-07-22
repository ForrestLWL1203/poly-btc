"""Copy-backtest helpers used by scanner/profile gates."""

from __future__ import annotations

import json

from hyper import config, params
from hyper.copy.copy_backtest import run_backtest, slice_backtest_result
from hyper.copy.copy_data import market_evidence_key, normalize_copyable_fills
from hyper.copy.copy_policy import COPY_POLICY_PARAM_KEYS, load_copy_policy
from hyper.copy.sector import SECTORS, compact_sector_results, evaluate_sector_policy, filter_fills


def copy_bt_sigmas(db):
    try:
        return {coin: sigma for coin, sigma in db.execute("SELECT coin,sigma FROM coin_vol WHERE sigma IS NOT NULL")}
    except Exception:  # noqa: BLE001
        return {}


def copy_bt_market_ctx(db):
    try:
        rows = db.execute(
            "SELECT coin,day_ntl_vlm,oi_notional,max_leverage FROM coin_vol "
            "WHERE day_ntl_vlm IS NOT NULL OR oi_notional IS NOT NULL OR max_leverage IS NOT NULL"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    return {r[0]: {"day_ntl_vlm": r[1], "oi_notional": r[2], "max_leverage": r[3]} for r in rows}


def copy_bt_overrides(db):
    try:
        vals = params.load_follow(db)
    except Exception:  # noqa: BLE001
        return {}
    out = dict(vals)
    try:
        scanner_values = params.load_category(db, "scanner")
        out.update({key: scanner_values[key] for key in COPY_POLICY_PARAM_KEYS if key in scanner_values})
    except Exception:  # noqa: BLE001
        pass
    if "SMART_ADD" in vals:
        out["ADD_STRATEGY"] = "smart" if vals["SMART_ADD"] else "hardcap"
    return out


def copy_bt_window_days(p):
    policy = load_copy_policy()
    base = int(getattr(p, "copy_bt_days", policy.windows[0]) or policy.windows[0])
    days = [base] + list(policy.windows[1:])
    out = []
    for day in days:
        try:
            val = int(day)
        except (TypeError, ValueError):
            continue
        if val > 0 and val not in out and val <= base:
            out.append(val)
    return out or [base]


def copy_bt_min_closed_for_days(p, days):
    policy = load_copy_policy()
    explicit = policy.min_closed(int(days))
    if int(days) >= int(getattr(p, "copy_bt_days", policy.windows[0]) or policy.windows[0]):
        return int(getattr(p, "copy_bt_min_closed", explicit) or 0)
    return explicit


def copy_bt_result(addr, fills, now_ms, p, days=None, *, valuation_marks=None,
                   sigmas=None, market_ctx=None):
    days = int(days if days is not None else (getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS))
    start_ms = now_ms - days * 86400_000
    replay_fills = [
        x for x in normalize_copyable_fills(
            fills,
            addr=addr,
            universe=getattr(p, "copyable_universe", None),
        )
        if start_ms <= x.get("time", 0) <= now_ms
    ]
    if not replay_fills:
        return {
            "valid": True,
            "data_status": "valid",
            "evidence_status": "no_fills",
            "has_evidence": False,
            "closed_n": 0,
            "open_n": 0,
            "target_open_events": 0,
            "opened_n": 0,
            "_window_days": days,
            "_window_start_ms": start_ms,
            "_window_end_ms": now_ms,
            "_market_evidence_key": market_evidence_key(replay_fills),
            "_market_generation": getattr(p, "scan_generation", None),
        }
    try:
        result = run_backtest(
            addr,
            replay_fills,
            sigmas=(sigmas if sigmas is not None else getattr(p, "copy_bt_sigmas", None)) or {},
            overrides=getattr(p, "copy_bt_overrides", None) or {},
            market_ctx=(
                market_ctx if market_ctx is not None else getattr(p, "copy_bt_market_ctx", None)
            ) or {},
            price_path=getattr(p, "copy_bt_price_path", None),
            price_path_meta=getattr(p, "copy_bt_price_path_meta", None) or {},
            valuation_marks=(
                valuation_marks
                if valuation_marks is not None
                else getattr(p, "copy_bt_valuation_marks", None)
            ) or {},
            valuation_asof_ms=now_ms,
        )
        # Keep window boundaries on the in-memory replay result so recent-loss
        # checks can compare the latest seven days with a non-overlapping
        # historical baseline. These private fields are not persisted by the
        # compact sector payload.
        result["_window_days"] = days
        result["_window_start_ms"] = start_ms
        result["_window_end_ms"] = now_ms
        result["valid"] = True
        result["data_status"] = "valid"
        result["has_evidence"] = bool(
            int(result.get("target_open_events") or 0)
            or int(result.get("closed_n") or 0)
            or int(result.get("open_n") or 0)
        )
        result["evidence_status"] = "observed" if result["has_evidence"] else "no_open_events"
        result["_market_evidence_key"] = market_evidence_key(replay_fills)
        result["_market_generation"] = getattr(p, "scan_generation", None)
        return result
    except Exception:  # noqa: BLE001 - backtest is a quality aid; never kill discovery on a replay bug
        # The scanner may keep a prior profile on a data/replay failure, but the
        # failed replay must never masquerade as positive/missing evidence.
        return {
            "valid": False,
            "data_status": "replay_error",
            "evidence_status": "invalid",
            "has_evidence": False,
            "closed_n": 0,
            "open_n": 0,
            "target_open_events": 0,
            "opened_n": 0,
            "_window_days": days,
            "_window_start_ms": start_ms,
            "_window_end_ms": now_ms,
            "_market_evidence_key": market_evidence_key(replay_fills),
            "_market_generation": getattr(p, "scan_generation", None),
        }


def copy_bt_results(addr, fills, now_ms, p, *, valuation_marks=None,
                    sigmas=None, market_ctx=None):
    days_list = copy_bt_window_days(p)
    primary_days = max(days_list)
    warmup_days = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
    warm = copy_bt_result(
        addr, fills, now_ms, p, days=primary_days + warmup_days,
        valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
    )
    if warm.get("valid") is False:
        return {days: dict(warm, _window_days=days) for days in days_list}

    out = {}
    for days in days_list:
        direct = copy_bt_result(
            addr, fills, now_ms, p, days=days, valuation_marks=valuation_marks,
            sigmas=sigmas, market_ctx=market_ctx,
        )
        sliced = slice_backtest_result(
            warm,
            now_ms - int(days) * 86_400_000,
            window_days=int(days),
        )
        # Activity/fillability belongs to the requested window. PnL and samples
        # come from the warm replay so boundary-spanning positions are retained.
        for key in (
            "target_open_events", "opened_n", "open_fill_rate", "actionable_open_rate",
            "execution_fill_rate", "capacity_open_fit", "target_adds", "followed_adds",
            "missed_adds", "missed_add_rate", "skip_reasons", "add_metrics_version",
            "add_outcome_counts", "raw_add_order_follow_rate", "noise_merged_adds",
            "blocked_adds", "actionable_add_orders", "actionable_add_capture_rate",
            "true_blocked_add_rate", "add_episode_count", "entry_gap_sigma_weighted",
            "entry_gap_sigma_p90", "entry_gap_pct_weighted", "entry_gap_pct_p90",
            "entry_gap_sigma_samples", "entry_gap_pct_samples", "entry_gap_weight",
            "entry_gap_sigma_weighted_sum", "entry_gap_pct_weighted_sum",
            "entry_alignment", "add_execution", "add_fidelity", "add_fidelity_applied",
            "effective_add_fidelity",
        ):
            if key in direct:
                sliced[key] = direct[key]
        open_rate = float(sliced.get("actionable_open_rate") or 0.0)
        if sliced.get("actionable_open_rate") is None:
            open_rate = 1.0
        path_rate = float(sliced.get("path_completion_rate") or 0.0)
        if sliced.get("path_completion_rate") is None:
            path_rate = 1.0
        add_fidelity = float(sliced.get("effective_add_fidelity") or 0.0)
        if sliced.get("effective_add_fidelity") is None:
            add_fidelity = 1.0
        behavior_v2 = max(0.0, min(1.0, open_rate * path_rate * add_fidelity))
        sliced["behavior_replication_rate"] = behavior_v2
        sliced["behavior_replication_v2"] = behavior_v2
        sliced["valid"] = bool(direct.get("valid", True))
        sliced["data_status"] = direct.get("data_status", "valid")
        sliced["has_evidence"] = bool(
            int(sliced.get("target_open_events") or 0)
            or int(sliced.get("closed_n") or 0)
            or int(sliced.get("open_n") or 0)
        )
        sliced["evidence_status"] = "observed" if sliced["has_evidence"] else "no_open_events"
        out[int(days)] = sliced
    return out


def sector_copy_bt_results(addr, fills, now_ms, p, *, valuation_marks=None,
                           sigmas=None, market_ctx=None):
    return {
        sector: copy_bt_results(
            addr, filter_fills(fills, sector), now_ms, p,
            valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
        )
        for sector in SECTORS
    }


def record_primary_copy_bt(metrics, result):
    if not result:
        return
    opened = int(result.get("opened_n") or 0)
    target_open = int(result.get("target_open_events") or 0)
    metrics.update(
        copy_bt_net_pnl=result.get("copy_net_pnl"),
        copy_bt_unrealized_pnl=result.get("unrealized_pnl"),
        copy_bt_valuation_status=result.get("valuation_status"),
        copy_bt_win_rate=result.get("copy_win_rate"),
        copy_bt_closed_n=int(result.get("closed_n") or 0),
        copy_bt_open_fill_rate=(opened / target_open) if target_open else None,
        copy_bt_liquidations=int(result.get("liquidations") or 0),
        copy_bt_fee_drag=result.get("fee_drag"),
        initial_margin_equity=result.get("initial_margin_equity"),
        copy_bt_data_status=result.get("data_status", "valid"),
        copy_bt_evidence_status=result.get("evidence_status", "observed"),
        copy_path_risk_status=result.get("path_risk_status", "missing"),
        copy_intratrade_max_drawdown=result.get("intratrade_max_drawdown"),
        copy_max_underwater_hours=result.get("max_underwater_hours"),
        copy_loss_over_5_time_ratio=result.get("loss_over_5_time_ratio"),
        copy_deep_bag_event_n=int(result.get("deep_bag_event_n") or 0),
        copy_failed_deep_bag_n=int(result.get("failed_deep_bag_n") or 0),
        copy_deep_bag_recovery_rate=result.get("deep_bag_recovery_rate"),
        copy_max_deep_bag_hours=result.get("max_deep_bag_hours"),
        copy_current_open_loss_frac=result.get("current_open_loss_frac"),
        copy_current_bag_hours=result.get("current_bag_hours"),
        copy_campaign_max_drawdown=result.get("campaign_max_drawdown"),
        copy_campaign_peak_positions=int(result.get("campaign_peak_positions") or 0),
        copy_campaign_peak_margin_pct=result.get("campaign_peak_margin_pct"),
    )
    for key in (
        "profit_factor", "payoff_ratio", "gross_profit", "gross_loss",
        "positive_episode_n", "negative_episode_n", "top1_profit_share",
        "top3_profit_share", "net_after_top1", "net_after_top2", "cost_stress_net_pnl",
        "body_after_top3_n", "body_after_top3_wins", "body_after_top3_losses",
        "body_after_top3_win_rate", "body_after_top3_net_pnl",
        "body_after_top3_gross_profit", "body_after_top3_gross_loss",
        "body_after_top3_profit_factor", "body_after_top3_payoff_ratio",
        "body_after_top3_median_pnl",
        "add_metrics_version", "add_outcome_counts", "raw_add_order_follow_rate",
        "noise_merged_adds", "blocked_adds", "actionable_add_capture_rate",
        "entry_gap_pct_weighted", "entry_gap_pct_p90", "entry_gap_sigma_weighted",
        "entry_gap_sigma_p90", "entry_alignment", "add_execution", "add_fidelity",
        "add_fidelity_applied", "behavior_replication_v2", "behavior_replication_rate",
        "campaign_closed_n", "campaign_wins", "campaign_win_rate", "campaign_net_pnl",
        "campaign_profit_factor", "campaign_net_after_top1", "campaign_net_after_top2",
        "campaign_max_positions", "campaign_peak_positions", "campaign_peak_margin_pct",
    ):
        if key in result:
            metrics[f"copy_bt_{key}"] = result.get(key)


def _result_has_evidence(result):
    if not result:
        return False
    if "has_evidence" in result:
        return bool(result.get("has_evidence"))
    return bool(
        int(result.get("target_open_events") or 0)
        or int(result.get("closed_n") or 0)
        or int(result.get("open_n") or 0)
    )


def _result_is_valid(result):
    return bool(result) and result.get("valid") is not False and result.get("data_status") != "replay_error"


def _window_state(result):
    if not result:
        return "legacy_missing"
    if "copy_net_pnl" in result or "valid" in result:
        rows = [result]
    else:
        rows = [row for row in result.values() if isinstance(row, dict)]
    if any(not _result_is_valid(row) for row in rows):
        return "invalid"
    if not any(_result_has_evidence(row) for row in rows):
        return "no_evidence"
    return "observed"


def record_recent_copy_bt(metrics, days, result):
    if not result:
        return
    if days == 14:
        metrics["copy_bt_14d_net_pnl"] = result.get("copy_net_pnl")
        metrics["copy_bt_14d_unrealized_pnl"] = result.get("unrealized_pnl")
        metrics["copy_bt_14d_closed_n"] = int(result.get("closed_n") or 0)
        metrics["copy_bt_14d_win_rate"] = result.get("copy_win_rate")
    elif days == 7:
        metrics["copy_bt_7d_net_pnl"] = result.get("copy_net_pnl")
        metrics["copy_bt_7d_unrealized_pnl"] = result.get("unrealized_pnl")
        metrics["copy_bt_7d_closed_n"] = int(result.get("closed_n") or 0)
        metrics["copy_bt_7d_win_rate"] = result.get("copy_win_rate")
    prefix = f"copy_bt_{int(days)}d_"
    for key in (
        "profit_factor", "payoff_ratio", "top1_profit_share", "top3_profit_share",
        "net_after_top1", "net_after_top2", "cost_stress_net_pnl",
        "body_after_top3_n", "body_after_top3_wins", "body_after_top3_losses",
        "body_after_top3_win_rate", "body_after_top3_net_pnl",
        "body_after_top3_gross_profit", "body_after_top3_gross_loss",
        "body_after_top3_profit_factor", "body_after_top3_payoff_ratio",
        "body_after_top3_median_pnl",
        "campaign_closed_n", "campaign_wins", "campaign_win_rate", "campaign_net_pnl",
        "campaign_profit_factor", "campaign_net_after_top1", "campaign_net_after_top2",
        "campaign_max_positions", "campaign_peak_positions", "campaign_peak_margin_pct",
    ):
        if key in result:
            metrics[prefix + key] = result.get(key)


def record_copy_bt_windows(metrics, result, p):
    if not result:
        return
    if "copy_net_pnl" in result:
        record_primary_copy_bt(metrics, result)
        return
    by_days = {}
    for days, res in result.items():
        try:
            day = int(days)
        except (TypeError, ValueError):
            continue
        if res:
            by_days[day] = res
    if not by_days:
        return
    primary_days = int(getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS)
    primary = by_days.get(primary_days) or by_days.get(max(by_days))
    record_primary_copy_bt(metrics, primary)
    for days, res in by_days.items():
        record_recent_copy_bt(metrics, days, res)


def copy_bt_target_perp_positive(metrics):
    """Target wallet itself must still be profitable on copyable perp metrics before old copy loss is waived."""
    if (metrics.get("net_pnl") or 0.0) <= 0:
        return False
    roi_total = metrics.get("roi_total")
    if roi_total is None:
        roi_total = metrics.get("roi_equity")
    if roi_total is not None and roi_total <= 0:
        return False
    for key in ("net_30d", "net_life"):
        val = metrics.get(key)
        if val is not None and val <= 0:
            return False
    return True


def copy_bt_recent_recovery_ok(metrics, by_days, primary_days, p, min_net):
    """Allow an old primary-window loss only after every configured recent window has recovered."""
    if not copy_bt_target_perp_positive(metrics):
        return False
    recent_days = []
    for days in getattr(config, "COPY_BT_RECENT_DAYS", (14, 7)):
        try:
            day = int(days)
        except (TypeError, ValueError):
            continue
        if 0 < day < primary_days and day not in recent_days:
            recent_days.append(day)
    if not recent_days:
        return False
    for days in recent_days:
        res = by_days.get(days)
        if not res:
            return False
        if int(res.get("closed_n") or 0) < copy_bt_min_closed_for_days(p, days):
            return False
        net = res.get("copy_net_pnl")
        if net is None or net <= min_net:
            return False
    return True


def apply_copy_bt_gate(metrics, result, p):
    if not result:
        return True, "ok"

    state = _window_state(result)
    if "copy_net_pnl" in result or "valid" in result:
        record_primary_copy_bt(metrics, result)
    if state == "invalid":
        metrics["copy_bt_data_status"] = "replay_error"
        metrics["copy_bt_evidence_status"] = "invalid"
        return True, "copy_backtest_deferred_data_error"
    if state == "no_evidence":
        metrics["copy_bt_data_status"] = "valid"
        metrics["copy_bt_evidence_status"] = "no_evidence"
        return True, "copy_backtest_no_evidence"

    if "copy_net_pnl" in result or "valid" in result:
        days = int(getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS)
        record_primary_copy_bt(metrics, result)
        if not getattr(p, "copy_bt_gate_enable", config.COPY_BT_GATE_ENABLE):
            return True, "ok"
        if int(result.get("closed_n") or 0) < copy_bt_min_closed_for_days(p, days):
            return True, "ok"
        net = result.get("copy_net_pnl")
        min_net = float(getattr(p, "copy_bt_min_net_pnl", config.COPY_BT_MIN_NET_PNL) or 0.0)
        if net is not None and net <= min_net:
            return False, "copy_backtest_loss"
        return True, "ok"

    by_days = {}
    for days, res in result.items():
        try:
            day = int(days)
        except (TypeError, ValueError):
            continue
        if res:
            by_days[day] = res
    if not by_days:
        return True, "ok"

    primary_days = int(getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS)
    primary = by_days.get(primary_days) or by_days.get(max(by_days))
    record_primary_copy_bt(metrics, primary)
    for days, res in by_days.items():
        record_recent_copy_bt(metrics, days, res)

    if not getattr(p, "copy_bt_gate_enable", config.COPY_BT_GATE_ENABLE):
        return True, "ok"
    min_net = float(getattr(p, "copy_bt_min_net_pnl", config.COPY_BT_MIN_NET_PNL) or 0.0)
    recent_recovery_ok = copy_bt_recent_recovery_ok(metrics, by_days, primary_days, p, min_net)
    for days in sorted(by_days, reverse=True):
        res = by_days[days]
        if int(res.get("closed_n") or 0) < copy_bt_min_closed_for_days(p, days):
            continue
        net = res.get("copy_net_pnl")
        if net is not None and net <= min_net:
            if days == primary_days and recent_recovery_ok:
                continue
            return False, "copy_backtest_loss" if days == primary_days else f"copy_backtest_loss_{days}d"
    return True, "ok"


def apply_sector_copy_bt_gate(metrics, result, sector_results, p, previous_policy=None,
                              structural_policy=None):
    """Record global copy replay, then gate followability by profitable sector.

    A wallet can stay active when one sector is copyable even if another sector
    loses. The observer later enforces the resulting sector policy per fill.
    """
    record_copy_bt_windows(metrics, result, p)
    compact = compact_sector_results(sector_results or {}, joint_results=result)
    policy = evaluate_sector_policy(
        sector_results or {},
        min_net=float(getattr(p, "copy_bt_min_net_pnl", config.COPY_BT_MIN_NET_PNL) or 0.0),
        previous_policy=previous_policy,
        structural_policy=structural_policy,
    )
    metrics["sector_copy_json"] = json.dumps(compact, sort_keys=True)
    metrics["sector_policy_json"] = json.dumps(policy, sort_keys=True)
    if not getattr(p, "copy_bt_gate_enable", config.COPY_BT_GATE_ENABLE):
        return True, "ok"
    states = [_window_state(result)] + [
        _window_state((sector_results or {}).get(sector)) for sector in SECTORS
    ]
    if "invalid" in states:
        metrics["copy_bt_data_status"] = "replay_error"
        metrics["copy_bt_evidence_status"] = "invalid"
        return True, "copy_backtest_deferred_data_error"
    if all(state in {"no_evidence", "legacy_missing"} for state in states):
        metrics["copy_bt_data_status"] = "valid"
        metrics["copy_bt_evidence_status"] = "no_evidence"
        return True, "copy_backtest_no_evidence"
    if policy.get("allowed"):
        return True, "ok"
    if any(compact.get(sector) for sector in SECTORS):
        # Economic weakness or thin evidence belongs to Challenger/continuous scoring.  The sector policy
        # remains fail-closed for live opens, but it must not erase an otherwise structurally copyable wallet
        # from the discovery pool before lifecycle/OOS selection can accumulate confirmation.
        statuses = {
            str((policy.get(sector) or {}).get("status") or "")
            for sector in SECTORS
            if isinstance(policy.get(sector), dict)
        }
        metrics["copy_bt_evidence_status"] = (
            "thin"
            if policy.get("watch") or (statuses and statuses.issubset({"thin_evidence", ""}))
            else "economically_disqualified"
        )
        return True, "copy_backtest_challenger_only"
    return apply_copy_bt_gate(metrics, result, p)
