"""Copy-backtest helpers used by scanner/profile gates."""

from __future__ import annotations

import json
import math

from . import config, params
from .copy_backtest import run_backtest
from .sector import SECTORS, compact_sector_results, evaluate_sector_policy, filter_fills


def copy_bt_sigmas(db):
    try:
        return {coin: sigma for coin, sigma in db.execute("SELECT coin,sigma FROM coin_vol WHERE sigma IS NOT NULL")}
    except Exception:  # noqa: BLE001
        return {}


def copy_bt_market_ctx(db):
    try:
        rows = db.execute(
            "SELECT coin,day_ntl_vlm,oi_notional FROM coin_vol "
            "WHERE day_ntl_vlm IS NOT NULL OR oi_notional IS NOT NULL"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    return {r[0]: {"day_ntl_vlm": r[1], "oi_notional": r[2]} for r in rows}


def copy_bt_overrides(db):
    try:
        vals = params.load_follow(db)
    except Exception:  # noqa: BLE001
        return {}
    out = dict(vals)
    if "SMART_ADD" in vals:
        out["ADD_STRATEGY"] = "smart" if vals["SMART_ADD"] else "hardcap"
    return out


def copy_bt_window_days(p):
    base = int(getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS)
    days = [base] + list(getattr(config, "COPY_BT_RECENT_DAYS", (14, 7)))
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
    base_days = int(getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS)
    base_min = int(getattr(p, "copy_bt_min_closed", config.COPY_BT_MIN_CLOSED) or 0)
    if base_days <= 0 or days >= base_days:
        return base_min
    scaled = int(math.ceil(base_min * days / base_days))
    recent_floor = int(getattr(config, f"COPY_BT_MIN_CLOSED_{int(days)}D", 0) or 0)
    return max(1, scaled, recent_floor)


def copy_bt_result(addr, fills, now_ms, p, days=None):
    days = int(days if days is not None else (getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS))
    start_ms = now_ms - days * 86400_000
    replay_fills = [x for x in fills if x.get("time", 0) >= start_ms]
    if not replay_fills:
        return {}
    try:
        result = run_backtest(
            addr,
            replay_fills,
            sigmas=getattr(p, "copy_bt_sigmas", None) or {},
            overrides=getattr(p, "copy_bt_overrides", None) or {},
            market_ctx=getattr(p, "copy_bt_market_ctx", None) or {},
        )
        # Keep window boundaries on the in-memory replay result so recent-loss
        # checks can compare the latest seven days with a non-overlapping
        # historical baseline. These private fields are not persisted by the
        # compact sector payload.
        result["_window_days"] = days
        result["_window_start_ms"] = start_ms
        result["_window_end_ms"] = now_ms
        return result
    except Exception:  # noqa: BLE001 - backtest is a quality aid; never kill discovery on a replay bug
        return {}


def copy_bt_results(addr, fills, now_ms, p):
    return {days: copy_bt_result(addr, fills, now_ms, p, days=days) for days in copy_bt_window_days(p)}


def sector_copy_bt_results(addr, fills, now_ms, p):
    return {
        sector: copy_bt_results(addr, filter_fills(fills, sector), now_ms, p)
        for sector in SECTORS
    }


def record_primary_copy_bt(metrics, result):
    if not result:
        return
    opened = int(result.get("opened_n") or 0)
    target_open = int(result.get("target_open_events") or 0)
    metrics.update(
        copy_bt_net_pnl=result.get("copy_net_pnl"),
        copy_bt_win_rate=result.get("copy_win_rate"),
        copy_bt_closed_n=int(result.get("closed_n") or 0),
        copy_bt_open_fill_rate=(opened / target_open) if target_open else None,
        copy_bt_liquidations=int(result.get("liquidations") or 0),
        copy_bt_fee_drag=result.get("fee_drag"),
    )


def record_recent_copy_bt(metrics, days, result):
    if not result:
        return
    if days == 14:
        metrics["copy_bt_14d_net_pnl"] = result.get("copy_net_pnl")
        metrics["copy_bt_14d_closed_n"] = int(result.get("closed_n") or 0)
    elif days == 7:
        metrics["copy_bt_7d_net_pnl"] = result.get("copy_net_pnl")
        metrics["copy_bt_7d_closed_n"] = int(result.get("closed_n") or 0)


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

    if "copy_net_pnl" in result:
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


def apply_sector_copy_bt_gate(metrics, result, sector_results, p, previous_policy=None):
    """Record global copy replay, then gate followability by profitable sector.

    A wallet can stay active when one sector is copyable even if another sector
    loses. The observer later enforces the resulting sector policy per fill.
    """
    record_copy_bt_windows(metrics, result, p)
    compact = compact_sector_results(sector_results or {})
    policy = evaluate_sector_policy(
        sector_results or {},
        min_net=float(getattr(p, "copy_bt_min_net_pnl", config.COPY_BT_MIN_NET_PNL) or 0.0),
        previous_policy=previous_policy,
    )
    metrics["sector_copy_json"] = json.dumps(compact, sort_keys=True)
    metrics["sector_policy_json"] = json.dumps(policy, sort_keys=True)
    if not getattr(p, "copy_bt_gate_enable", config.COPY_BT_GATE_ENABLE):
        return True, "ok"
    if policy.get("allowed"):
        return True, "ok"
    if any(compact.get(sector) for sector in SECTORS):
        return False, "copy_backtest_no_profitable_sector"
    return apply_copy_bt_gate(metrics, result, p)
