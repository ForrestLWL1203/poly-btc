"""Source-quality, rough-Copy and final strict-Copy ranking.

Qualification answers four independent questions only: is the source wallet consistently good, is its
profit material, can our execution follow it, and is it currently active. The score never grants permission;
it orders wallets which already passed the applicable business contract.
"""
from __future__ import annotations

import math
import time
from typing import Mapping

from hyper import config
from hyper.copy.copy_policy import load_copy_policy
from hyper.copy.sector import apply_allowed_sector_copy_metrics, parse_json_obj


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return default if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _quality_above_floor(value: float, floor: float, span: float) -> float:
    """Give a qualified boundary 60%, then continue monotonically to a transparent cap."""
    return _clamp(0.60 + 0.40 * (float(value) - float(floor)) / max(1e-9, float(span)))


def _replay_window_equity(metrics: Mapping, days: int) -> float:
    key = "copy_bt_window_start_equity" if int(days) == 30 else f"copy_bt_{int(days)}d_window_start_equity"
    for candidate in (
        metrics.get(key),
        metrics.get("copy_bt_initial_margin_equity"),
        metrics.get("initial_margin_equity"),
        getattr(config, "INITIAL_BALANCE", 10_000.0),
    ):
        value = _num(candidate)
        if value > 0.0:
            return value
    return 1.0


def _activity_age_hours(metrics: Mapping, as_of_ms: int) -> float | None:
    last_open = int(_num(metrics.get("last_copyable_open_ms")))
    if last_open <= 0:
        return None
    return max(0.0, (int(as_of_ms) - last_open) / 3_600_000.0)


def _official_return_context(metrics: Mapping, policy) -> dict:
    """Recover the qualifying official window without calling a 7-day return "30-day ROI"."""
    payload = parse_json_obj(metrics.get("official_perp_evidence_json"))
    windows = payload.get("windows") if isinstance(payload.get("windows"), dict) else {}
    evidence = (
        windows.get("officialPerp30d")
        if isinstance(windows.get("officialPerp30d"), dict)
        else {}
    )
    floor = _num(evidence.get("minimumReturn"), policy.official_perp_min_return_30d)
    return {
        "return": _num(metrics.get("official_perp_return_30d")),
        "floor": floor,
        "historyTier": evidence.get("historyTier") or "full_history",
        "windowDays": (
            _num(evidence.get("windowDays"))
            if evidence.get("windowDays") is not None else None
        ),
        "positiveCoverageDays": (
            _num(evidence.get("positiveCoverageDays"))
            if evidence.get("positiveCoverageDays") is not None else None
        ),
    }


def evaluate_source_quality(
    metrics: Mapping,
    *,
    policy_values: Mapping | None = None,
    as_of_ms: int | None = None,
) -> dict:
    """Apply the sole deep-fill source-wallet quality contract."""
    policy = load_copy_policy(policy_values)
    as_of_ms = int(as_of_ms or time.time() * 1000)
    episodes = int(_num(metrics.get("source_episode_n_30d")))
    win_rate = metrics.get("source_win_rate_30d")
    top3_share = metrics.get("source_top3_profit_share")
    body_n = int(_num(metrics.get("source_body_after_top3_n")))
    body_win_rate = metrics.get("source_body_after_top3_win_rate")
    body_net = metrics.get("source_body_after_top3_net_pnl")
    activity_age = _activity_age_hours(metrics, as_of_ms)
    concentration_triggered = bool(
        top3_share is not None
        and _num(top3_share) >= policy.source_top3_concentration_trigger
    )
    checks = {
        "minimumCompleteEpisodes": episodes >= policy.source_min_episodes_30d,
        "sourceWinRate": (
            win_rate is not None
            and _num(win_rate) >= policy.source_min_episode_win_rate
        ),
        "activityWithin72h": activity_age is not None and activity_age <= 72.0,
        "concentratedBodyWinRate": (
            not concentration_triggered
            or (
                body_n > 0 and body_win_rate is not None
                and _num(body_win_rate) >= policy.source_body_min_win_rate
            )
        ),
        "concentratedBodyNonNegative": (
            not concentration_triggered
            or (body_net is not None and _num(body_net) >= 0.0)
        ),
    }
    failures = (
        ("source_episode_evidence_insufficient", "minimumCompleteEpisodes"),
        ("source_win_rate_below_floor", "sourceWinRate"),
        ("source_activity_stale", "activityWithin72h"),
        ("source_concentrated_body_win_rate_low", "concentratedBodyWinRate"),
        ("source_concentrated_body_unprofitable", "concentratedBodyNonNegative"),
    )
    first_failure = next((reason for reason, key in failures if not checks[key]), None)
    return {
        "eligible": first_failure is None,
        "status": "source_quality_passed" if first_failure is None else first_failure,
        "firstFailure": first_failure,
        "checks": checks,
        "episodeN30d": episodes,
        "winRate30d": _num(win_rate) if win_rate is not None else None,
        "activityAgeHours": activity_age,
        "top3ProfitShare": _num(top3_share) if top3_share is not None else None,
        "concentrationTriggered": concentration_triggered,
        "bodyAfterTop3N": body_n,
        "bodyAfterTop3WinRate": _num(body_win_rate) if body_win_rate is not None else None,
        "bodyAfterTop3NetPnl": _num(body_net) if body_net is not None else None,
    }


def compute_source_quality_score(
    metrics: Mapping,
    *,
    policy_values: Mapping | None = None,
    as_of_ms: int | None = None,
) -> tuple[float, dict]:
    """Rank source-qualified wallets before the global Top40 cap."""
    policy = load_copy_policy(policy_values)
    as_of_ms = int(as_of_ms or time.time() * 1000)
    official = _official_return_context(metrics, policy)
    official_return = official["return"]
    win_rate = _num(metrics.get("source_win_rate_30d"))
    episodes = int(_num(metrics.get("source_episode_n_30d")))
    activity_age = _activity_age_hours(metrics, as_of_ms)
    official_score = _quality_above_floor(
        official_return, official["floor"], 0.80,
    )
    win_score = _quality_above_floor(
        win_rate, policy.source_min_episode_win_rate, 0.25,
    )
    sample_score = _quality_above_floor(
        episodes, policy.source_min_episodes_30d, 30,
    )
    recency_score = 0.0 if activity_age is None else _clamp(1.0 - activity_age / 180.0)
    score = 0.25 * official_score + 0.40 * win_score + 0.25 * sample_score + 0.10 * recency_score
    return _clamp(score), {
        "officialPerp30dScore": official_score,
        "officialPerpHistoryTier": official["historyTier"],
        "officialPerpWindowDays": official["windowDays"],
        "sourceWinRateScore": win_score,
        "sourceSampleScore": sample_score,
        "sourceRecencyScore": recency_score,
    }


def evaluate_follow_eligibility(
    metrics: Mapping,
    *,
    stage: str = "rough",
    min_closed30: int | None = None,
    min_closed14: int | None = None,
    min_closed7: int | None = None,
    min_open_fill_rate: float | None = None,
    min_evidence_days: int | None = None,
    margin_equity_pct: float | None = None,
    policy_values: Mapping | None = None,
    as_of_ms: int | None = None,
    follow_score_value: float | None = None,
) -> dict:
    """Classify one rough or final strict Copy result with one ordered failure reason."""
    del min_closed14, min_closed7, min_evidence_days, margin_equity_pct, follow_score_value
    stage = "strict" if str(stage).lower() in {"strict", "final"} else "rough"
    policy = load_copy_policy(policy_values)
    as_of_ms = int(as_of_ms or time.time() * 1000)
    scoped = apply_allowed_sector_copy_metrics(metrics)
    policy_json = parse_json_obj(scoped.get("sector_policy_json"))
    c30 = int(_num(scoped.get("copy_bt_closed_n")))
    copy_win_rate = scoped.get("copy_bt_win_rate")
    pnl30 = _num(scoped.get("copy_bt_net_pnl"))
    pnl7 = _num(scoped.get("copy_bt_7d_net_pnl"))
    equity30 = _replay_window_equity(scoped, 30)
    equity7 = _replay_window_equity(scoped, 7)
    return30 = pnl30 / equity30
    return7 = pnl7 / equity7
    open_rate = scoped.get("actionable_open_rate", scoped.get("copy_bt_open_fill_rate"))
    activity_age = _activity_age_hours(scoped, as_of_ms)
    data_status = str(scoped.get("copy_bt_data_status") or scoped.get("data_status") or "valid").lower()
    evidence_status = str(
        scoped.get("copy_bt_evidence_status") or scoped.get("evidence_status") or ""
    ).lower()
    valuation_status = str(scoped.get("copy_bt_valuation_status") or "complete").lower()
    path_status = str(scoped.get("copy_path_risk_status") or "").lower()
    official_status = str(scoped.get("official_perp_status") or "").lower()
    official_reason = str(scoped.get("official_perp_reason") or "official_perp_evidence_missing")
    source = evaluate_source_quality(scoped, policy_values=policy_values, as_of_ms=as_of_ms)
    minimum_closed = int(
        policy.rough_min_closed_30d if min_closed30 is None else min_closed30
    )
    minimum_open_rate = (
        policy.min_actionable_open_rate
        if min_open_fill_rate is None else float(min_open_fill_rate)
    )
    return_floor30 = (
        policy.core_min_dynamic_copy_return_30d
        if stage == "strict" else policy.rough_min_return_30d
    )
    return_floor7 = (
        policy.core_min_dynamic_copy_return_7d
        if stage == "strict" else policy.rough_min_return_7d
    )
    win_floor = (
        policy.core_min_copy_win_rate if stage == "strict" else policy.rough_min_win_rate
    )
    allowed = set(policy_json.get("allowed") or ())
    watched = set(policy_json.get("watch") or ())
    sector_ready = bool(allowed) if "allowed" in policy_json else True
    checks = {
        "copyDataValid": data_status in {"", "valid", "ok"} and evidence_status != "invalid",
        "officialPerpPassed": official_status == "passed",
        "sourceQualityPassed": bool(source.get("eligible")),
        "minimumClosedEvidence": c30 >= minimum_closed,
        "copy30dReturn": return30 >= return_floor30,
        "copy7dReturn": return7 >= return_floor7,
        "copyWinRate": copy_win_rate is not None and _num(copy_win_rate) >= win_floor,
        "openExecution": open_rate is not None and _num(open_rate) >= minimum_open_rate,
        "activityWithin72h": activity_age is not None and activity_age <= 72.0,
        "valuationComplete": valuation_status == "complete",
        "sectorExecutable": sector_ready,
        "pathComplete": (
            stage != "strict"
            or path_status not in {"", "pending", "missing", "invalid", "replay_error", "incomplete"}
        ),
        "liquidationsWithinLimit": (
            stage != "strict"
            or int(_num(scoped.get("copy_bt_liquidations"))) <= policy.core_max_liquidations_30d
        ),
    }
    if "allowed" in policy_json and not allowed and not watched:
        checks["sectorExecutable"] = False
    failures = (
        ("copy_data_error", "copyDataValid", True),
        (
            official_reason if official_status == "deferred_data_error" else "official_perp_not_qualified",
            "officialPerpPassed", official_status == "deferred_data_error",
        ),
        (source.get("firstFailure") or "source_quality_not_qualified", "sourceQualityPassed", False),
        ("copy_episode_evidence_insufficient", "minimumClosedEvidence", True),
        (f"{stage}_copy_30d_return_below_floor", "copy30dReturn", False),
        (f"{stage}_copy_7d_return_below_floor", "copy7dReturn", False),
        (f"{stage}_copy_win_rate_below_floor", "copyWinRate", False),
        (f"{stage}_copy_open_rate_below_floor", "openExecution", False),
        ("activity_over_72h", "activityWithin72h", False),
        ("copy_valuation_incomplete", "valuationComplete", True),
        ("sector_not_executable", "sectorExecutable", False),
        ("copy_path_incomplete", "pathComplete", True),
        ("strict_copy_liquidations_over_3", "liquidationsWithinLimit", False),
    )
    first_failure = None
    deferred = False
    for reason, key, is_deferred in failures:
        if not checks[key]:
            first_failure, deferred = reason, is_deferred
            break
    stage_eligible = first_failure is None
    research_eligible = checks["copyDataValid"] and (
        pnl30 > 0.0 or deferred or official_status == "deferred_data_error"
    )
    return {
        "eligible": research_eligible,
        "coreEligible": stage_eligible,
        "stageEligible": stage_eligible,
        "stage": stage,
        "status": f"{stage}_copy_qualified" if stage_eligible else first_failure,
        "firstFailure": first_failure,
        "role": "core_eligible" if stage_eligible else "challenger" if research_eligible else "rejected",
        "deferred": bool(deferred),
        "checks": checks,
        "returns": {"30": return30, "7": return7},
        "returnFloors": {"30": return_floor30, "7": return_floor7},
        "windowStartEquity": {"30": equity30, "7": equity7},
        "netPnl": {"30": pnl30, "7": pnl7},
        "closedN": c30,
        "copyWinRate": _num(copy_win_rate) if copy_win_rate is not None else None,
        "copyWinRateFloor": win_floor,
        "openFillRate": _num(open_rate) if open_rate is not None else None,
        "openFillRateFloor": minimum_open_rate,
        "activityAgeHours": activity_age,
        "sourceQuality": source,
        "officialPerpEvidence": {"status": official_status or "missing", "reason": official_reason},
        "simulatedLiquidations": int(_num(scoped.get("copy_bt_liquidations"))),
        "reasons": [] if stage_eligible else [str(first_failure or "copy_not_qualified")],
    }


def compute_follow_score(
    metrics: Mapping,
    *,
    policy_values: Mapping | None = None,
    stage: str | None = None,
) -> tuple[float, dict]:
    """Return the exact 40/30/20/10 monotonic ranking score; never a permission line."""
    scoped = apply_allowed_sector_copy_metrics(metrics)
    policy = load_copy_policy(policy_values)
    c30 = int(_num(scoped.get("copy_bt_closed_n")))
    if c30 <= 0 or scoped.get("copy_bt_net_pnl") is None:
        source_score = scoped.get("source_quality_score")
        if source_score is None:
            source_score = compute_source_quality_score(scoped, policy_values=policy_values)[0]
        return _clamp(_num(source_score)), {
            "sourceOnly": True,
            "sourceQualityScore": _clamp(_num(source_score)),
            "copyScore": None,
            "reasons": ["尚未进入Top40粗略Copy"],
        }
    stage = str(stage or scoped.get("copy_replay_stage") or "rough").lower()
    strict = stage in {"strict", "final"}
    floor30 = (
        policy.core_min_dynamic_copy_return_30d if strict else policy.rough_min_return_30d
    )
    floor7 = policy.core_min_dynamic_copy_return_7d if strict else policy.rough_min_return_7d
    pnl30 = _num(scoped.get("copy_bt_net_pnl"))
    pnl7 = _num(scoped.get("copy_bt_7d_net_pnl"))
    return30 = pnl30 / _replay_window_equity(scoped, 30)
    return7 = pnl7 / _replay_window_equity(scoped, 7)
    official = _official_return_context(scoped, policy)
    official_return = official["return"]
    source_win = _num(scoped.get("source_win_rate_30d"))
    copy_win = _num(scoped.get("copy_bt_win_rate"))
    open_rate = _clamp(_num(
        scoped.get("actionable_open_rate", scoped.get("copy_bt_open_fill_rate"))
    ))
    behavior = scoped.get(
        "copy_bt_behavior_replication_rate",
        scoped.get("copy_bt_add_fidelity"),
    )
    behavior_score = 0.50 if behavior is None else _clamp(_num(behavior))
    as_of_ms = int(_num(scoped.get("score_as_of_ms"), time.time() * 1000))
    activity_age = _activity_age_hours(scoped, as_of_ms)
    activity_score = 0.0 if activity_age is None else _clamp(1.0 - activity_age / 180.0)
    source_opens = int(_num(scoped.get("open_events_30d")))
    components = {
        "officialPerp30d": _quality_above_floor(
            official_return, official["floor"], 0.80,
        ),
        "copy30d": _quality_above_floor(return30, floor30, 0.60),
        "copy7d": _quality_above_floor(return7, floor7, 0.25),
        "sourceWinRate": _quality_above_floor(
            source_win, policy.source_min_episode_win_rate, 0.25,
        ),
        "copyWinRate": _quality_above_floor(
            copy_win,
            policy.core_min_copy_win_rate if strict else policy.rough_min_win_rate,
            0.35,
        ),
        "openFollowRate": _quality_above_floor(
            open_rate, policy.min_actionable_open_rate, 0.30,
        ),
        "behaviorReplication": behavior_score,
        "activityRecency": activity_score,
        "independentOpens": _quality_above_floor(source_opens, 10, 30),
    }
    score = (
        0.10 * components["officialPerp30d"]
        + 0.20 * components["copy30d"]
        + 0.10 * components["copy7d"]
        + 0.20 * components["sourceWinRate"]
        + 0.10 * components["copyWinRate"]
        + 0.15 * components["openFollowRate"]
        + 0.05 * components["behaviorReplication"]
        + 0.05 * components["activityRecency"]
        + 0.05 * components["independentOpens"]
    )
    return _clamp(score), {
        "sourceOnly": False,
        "stage": "strict" if strict else "rough",
        "components": components,
        "economicReturns": {"30d": return30, "7d": return7},
        "officialPerp": official,
        "economicEquities": {
            "30d": _replay_window_equity(scoped, 30),
            "7d": _replay_window_equity(scoped, 7),
        },
        "copyPnl": {"30d": pnl30, "7d": pnl7},
        "closedN": {"30d": c30, "7d": int(_num(scoped.get("copy_bt_7d_closed_n")))},
        "sourceWinRate": source_win,
        "copyWinRate": copy_win,
        "openFillRate": open_rate,
        "activityAgeHours": activity_age,
        "liquidations": int(_num(scoped.get("copy_bt_liquidations"))),
        "feeDrag": scoped.get("copy_bt_fee_drag"),
        "reasons": [
            f"动态Copy 30d {return30 * 100:+.1f}% / 7d {return7 * 100:+.1f}%",
            f"源胜率 {source_win * 100:.1f}% / Copy胜率 {copy_win * 100:.1f}%",
            f"开仓跟随率 {open_rate * 100:.1f}%",
        ],
    }
