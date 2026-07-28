"""Anonymous, repeatable analysis over the private profit-research database."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import zlib

from hyper.copy.copy_backtest import profit_structure_metrics
from hyper.copy.copy_policy import load_copy_policy
from hyper.util import f

from . import profit_distribution


RETURN_TIERS = (
    ("50_10", 0.50, 0.10),
    ("40_7_5", 0.40, 0.075),
    ("30_5", 0.30, 0.05),
    ("25_5", 0.25, 0.05),
    ("20_5", 0.20, 0.05),
)
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)

# These are analysis probes, not production policy.  They quantify which cheap official fields or progressively
# deeper fill-derived fields can reduce work without discarding the operational high-return cohorts.  Missing
# evidence is counted fail-open because the production collector must defer/collect it rather than silently
# reject a potentially strong wallet.
GATE_SWEEPS = (
    {
        "stage": "leaderboard",
        "feature": "leaderboardMonthRoi",
        "direction": "minimum",
        "thresholds": (0.0, 0.02, 0.05, 0.10, 0.20),
    },
    {
        "stage": "leaderboard",
        "feature": "leaderboardWeekRoi",
        "direction": "minimum",
        "thresholds": (0.0, 0.01, 0.025, 0.05, 0.10),
    },
    {
        "stage": "portfolio",
        "feature": "officialPerpWeekVolume",
        "direction": "minimum",
        "thresholds": (250_000, 300_000, 500_000, 1_000_000, 5_000_000),
    },
    {
        "stage": "portfolio",
        "feature": "accountValue",
        "direction": "minimum",
        "thresholds": (0, 20_000, 50_000, 100_000, 250_000),
    },
    {
        "stage": "fills_structure",
        "feature": "medianHoldHours",
        "direction": "minimum",
        "thresholds": (1, 2, 4, 12),
    },
    {
        "stage": "fills_profile",
        "feature": "sourceEpisodes30",
        "direction": "minimum",
        "thresholds": (1, 4, 8, 12, 20),
    },
    {
        "stage": "fills_profile",
        "feature": "sourceEpisodes7",
        "direction": "minimum",
        "thresholds": (1, 2, 3, 5, 10),
    },
    {
        "stage": "fills_profile",
        "feature": "sourceWinRate30",
        "direction": "minimum",
        "thresholds": (0.30, 0.50, 0.60, 0.70, 0.80),
    },
    {
        "stage": "fills_only_copy",
        "feature": "copyWinRate30",
        "direction": "minimum",
        "thresholds": (0.30, 0.40, 0.50, 0.60, 0.70, 0.80),
    },
    {
        "stage": "activity",
        "feature": "activeWeeks4",
        "direction": "minimum",
        "thresholds": (3, 4),
    },
    {
        "stage": "activity",
        "feature": "actionableOpenEvents28",
        "direction": "minimum",
        "thresholds": (4, 8, 12, 20),
    },
    {
        "stage": "activity",
        "feature": "actionableOpenEvents7",
        "direction": "minimum",
        "thresholds": (1, 2, 3, 5),
    },
)


def _quantile(values, q):
    rows = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not rows:
        return None
    index = (len(rows) - 1) * float(q)
    lo, hi = int(math.floor(index)), int(math.ceil(index))
    if lo == hi:
        return rows[lo]
    return rows[lo] + (rows[hi] - rows[lo]) * (index - lo)


def _return_quantiles(rows):
    return {
        f"{days}d": {
            str(q): _quantile([row[f"return{days}"] for row in rows], q)
            for q in QUANTILES
        }
        for days in (30, 14, 7)
    }


def _max_structure(record, key):
    structure = record.get("structure") or {}
    values = [
        (structure.get(sector) or {}).get(key)
        for sector in ("crypto", "stock")
        if (structure.get(sector) or {}).get(key) is not None
    ]
    return max((f(value) for value in values), default=None)


def _wallet_view(wallet, status, reason, record, artifact=None):
    windows = ((record.get("rough") or {}).get("windows") or {})
    source = record.get("source") or {}
    current = record.get("current") or {}
    activity = record.get("activity") or {}
    artifact = artifact or {}
    source_artifact = artifact.get("sourceEpisodeQuality") or {}
    replay_artifact = artifact.get("roughCopyResults") or {}
    replay30 = replay_artifact.get("30") or replay_artifact.get(30) or {}
    closed_positions30 = list(replay30.get("positions") or ())
    closed_structure30 = (
        profit_structure_metrics(
            closed_positions30,
            total_net=sum(f(position.get("net_pnl")) for position in closed_positions30),
        )
        if closed_positions30 else {}
    )

    def value(days, key):
        return (windows.get(str(days)) or {}).get(key)

    def source_value(key):
        value = source.get(key)
        return source_artifact.get(key) if value is None else value

    def copy_structure_value(window_key, structure_key):
        window_value = value(30, window_key)
        if window_value is not None:
            return window_value
        return closed_structure30.get(structure_key)

    return30 = value(30, "qualificationReturn")
    return14 = value(14, "qualificationReturn")
    return7 = value(7, "qualificationReturn")
    priority = (
        0.70 * f(return30) + 0.30 * f(return7)
        if return30 is not None and return7 is not None else None
    )
    return {
        "wallet": wallet,
        "status": status,
        "reason": reason,
        "return30": return30,
        "return14": return14,
        "return7": return7,
        "profitPriority": priority,
        "closedEpisodes30": value(30, "closedEpisodes"),
        "closedEpisodes14": value(14, "closedEpisodes"),
        "closedEpisodes7": value(7, "closedEpisodes"),
        "copyWins30": value(30, "wins"),
        "copyWinRate30": (
            f(value(30, "wins")) / int(value(30, "closedEpisodes") or 0)
            if int(value(30, "closedEpisodes") or 0) > 0 else None
        ),
        "copyLiquidations30": value(30, "liquidations"),
        "copyOpenLossRatio30": value(30, "openLossRatio"),
        "copyTop3ProfitShare": copy_structure_value(
            "closedTop3ProfitShare", "top3_profit_share",
        ),
        "copyBodyAfterTop3N": copy_structure_value(
            "closedBodyAfterTop3N", "body_after_top3_n",
        ),
        "copyBodyAfterTop3WinRate": copy_structure_value(
            "closedBodyAfterTop3WinRate", "body_after_top3_win_rate",
        ),
        "copyBodyAfterTop3Pnl": copy_structure_value(
            "closedBodyAfterTop3Pnl", "body_after_top3_net_pnl",
        ),
        "actionableOpenRate30": value(30, "actionableOpenRate"),
        "pathCompletionRate30": value(30, "pathCompletionRate"),
        "sourceEpisodes30": source_value("source_episode_n_30d"),
        "sourceEpisodes7": source_value("source_episode_n_7d"),
        "sourceWinRate30": source_value("source_win_rate_30d"),
        "sourceWinRate7": source_value("source_win_rate_7d"),
        "sourceTop3ProfitShare": source_value("source_top3_profit_share"),
        "sourceBodyAfterTop3N": source_value("source_body_after_top3_n"),
        "sourceBodyAfterTop3WinRate": source_value("source_body_after_top3_win_rate"),
        "sourceBodyAfterTop3Pnl": source_value("source_body_after_top3_net_pnl"),
        "medianHoldHours": (
            f(source.get("medianHoldSeconds")) / 3600.0
            if source.get("medianHoldSeconds") is not None else None
        ),
        "medianEpisodesPerActiveDay": source.get("medianEpisodesPerActiveDay"),
        "takerNotionalFraction": source.get("takerNotionalFraction"),
        "leaderboardWeekVolume": record.get("leaderboardWeekVolume"),
        "officialPerpWeekVolume": record.get("officialPerpWeekVolume"),
        "leaderboardWeekRoi": record.get("leaderboardWeekRoi"),
        "leaderboardMonthRoi": record.get("leaderboardMonthRoi"),
        "accountValue": current.get("accountValue", record.get("accountValue")),
        "currentOpenLossFraction": current.get("openLossFraction"),
        "spotHedgeRatio": current.get("spotHedgeRatio"),
        "maxAdds": _max_structure(record, "maxAdds"),
        "medianAdds": _max_structure(record, "medianAdds"),
        "maxConcurrent": _max_structure(record, "maxConcurrent"),
        "activityAudited": bool(activity),
        "operationalActivity": activity.get("operational"),
        "activeWeeks4": activity.get("activeWeeks4"),
        "weeklyOpenCounts": activity.get("weeklyOpenCountsOldestFirst"),
        "actionableOpenEvents28": activity.get("actionableOpenEvents28d"),
        "actionableOpenEvents7": activity.get("actionableOpenEvents7d"),
        "maxOpenGapDays28": activity.get("maxOpenGapDays28d"),
        "activityReason": activity.get("reason"),
    }


def _tier_pass(row, floor30, floor7):
    return (
        row["return30"] is not None
        and row["return7"] is not None
        and f(row["return30"]) >= floor30
        and f(row["return7"]) >= floor7
    )


def _bucket(value, cuts):
    if value is None:
        return "missing"
    number = f(value)
    for ceiling, label in cuts:
        if ceiling is None or number < ceiling:
            return label
    return cuts[-1][1]


FEATURE_BUCKETS = {
    "leaderboardMonthRoi": (
        (0.0, "<0%"), (0.05, "0–5%"), (0.10, "5–10%"),
        (0.25, "10–25%"), (0.50, "25–50%"), (None, "≥50%"),
    ),
    "officialPerpWeekVolume": (
        (250_000, "<$250k"), (500_000, "$250–500k"), (1_000_000, "$500k–1m"),
        (5_000_000, "$1–5m"), (None, "≥$5m"),
    ),
    "accountValue": (
        (20_000, "<$20k"), (50_000, "$20–50k"), (100_000, "$50–100k"),
        (250_000, "$100–250k"), (None, "≥$250k"),
    ),
    "sourceEpisodes30": (
        (1, "0"), (3, "1–2"), (8, "3–7"), (20, "8–19"), (50, "20–49"), (None, "≥50"),
    ),
    "sourceEpisodes7": (
        (1, "0"), (2, "1"), (5, "2–4"), (10, "5–9"), (None, "≥10"),
    ),
    "medianHoldHours": (
        (1, "<1h"), (4, "1–4h"), (12, "4–12h"), (48, "12–48h"), (None, "≥48h"),
    ),
    "activeWeeks4": (
        (1, "0/4"), (2, "1/4"), (3, "2/4"), (4, "3/4"), (None, "4/4"),
    ),
}


def _bucket_analysis(rows):
    tier_totals = {
        name: sum(_tier_pass(row, floor30, floor7) for row in rows)
        for name, floor30, floor7 in RETURN_TIERS
    }
    output = {}
    for feature, cuts in FEATURE_BUCKETS.items():
        groups = {}
        for row in rows:
            label = _bucket(row.get(feature), cuts)
            group = groups.setdefault(label, [])
            group.append(row)
        output[feature] = []
        for label, group in groups.items():
            tier_counts = {
                name: sum(_tier_pass(row, floor30, floor7) for row in group)
                for name, floor30, floor7 in RETURN_TIERS
            }
            output[feature].append({
                "bucket": label,
                "wallets": len(group),
                "activityAudited": sum(bool(row["activityAudited"]) for row in group),
                "operational": sum(bool(row["operationalActivity"]) for row in group),
                "tierCounts": tier_counts,
                "tierRecall": {
                    name: (
                        tier_counts[name] / tier_totals[name]
                        if tier_totals[name] else None
                    )
                    for name, _floor30, _floor7 in RETURN_TIERS
                },
            })
    return output


def _sample_depth_grid(rows):
    output = []
    tier_totals = {
        name: sum(_tier_pass(row, floor30, floor7) for row in rows)
        for name, floor30, floor7 in RETURN_TIERS
    }
    for minimum30 in (1, 2, 4, 8, 12, 20):
        for minimum7 in (1, 2, 3, 5, 10):
            selected = [
                row for row in rows
                if int(row.get("sourceEpisodes30") or 0) >= minimum30
                and int(row.get("sourceEpisodes7") or 0) >= minimum7
            ]
            tier_counts = {
                name: sum(_tier_pass(row, floor30, floor7) for row in selected)
                for name, floor30, floor7 in RETURN_TIERS
            }
            output.append({
                "minimumSourceEpisodes30": minimum30,
                "minimumSourceEpisodes7": minimum7,
                "wallets": len(selected),
                "tierCounts": tier_counts,
                "tierRecall": {
                    name: (
                        tier_counts[name] / tier_totals[name]
                        if tier_totals[name] else None
                    )
                    for name, _floor30, _floor7 in RETURN_TIERS
                },
            })
    return output


def _gate_sensitivity(rows):
    """Measure retention and operational high-return recall for possible pre-strict gates."""
    tier_targets = {
        name: [
            row for row in rows
            if row["operationalActivity"] is True
            and _tier_pass(row, floor30, floor7)
        ]
        for name, floor30, floor7 in RETURN_TIERS
    }
    output = []
    for sweep in GATE_SWEEPS:
        feature = str(sweep["feature"])
        direction = str(sweep["direction"])
        for threshold in sweep["thresholds"]:
            known = [row for row in rows if row.get(feature) is not None]
            missing = [row for row in rows if row.get(feature) is None]
            if direction == "minimum":
                passed = [
                    row for row in known
                    if f(row.get(feature)) >= f(threshold)
                ]
            else:
                passed = [
                    row for row in known
                    if f(row.get(feature)) <= f(threshold)
                ]
            passed_ids = {row["wallet"] for row in passed}
            tier_pass = {
                name: sum(row["wallet"] in passed_ids for row in targets)
                for name, targets in tier_targets.items()
            }
            output.append({
                "stage": sweep["stage"],
                "feature": feature,
                "direction": direction,
                "threshold": threshold,
                "roughPopulation": len(rows),
                "knownEvidence": len(known),
                "missingEvidence": len(missing),
                "knownPass": len(passed),
                "knownFail": len(known) - len(passed),
                "failOpenCandidates": len(passed) + len(missing),
                "operationalPass": sum(
                    row["operationalActivity"] is True for row in passed
                ),
                "tierOperationalTotals": {
                    name: len(targets) for name, targets in tier_targets.items()
                },
                "tierOperationalPass": tier_pass,
                "tierOperationalRecall": {
                    name: (
                        tier_pass[name] / len(targets)
                        if targets else None
                    )
                    for name, targets in tier_targets.items()
                },
            })
    return output


def _repeatability_check(row, policy, *, copy_body_guard):
    """Evaluate closed-Episode repeatability without any return or official-ROI gate."""
    source_n = int(row.get("sourceEpisodes30") or 0)
    source_win = row.get("sourceWinRate30")
    standard_lane = source_n >= policy.source_min_episodes_30d
    low_frequency_lane = (
        policy.source_low_freq_min_episodes_30d
        <= source_n
        <= policy.source_low_freq_max_episodes_30d
    )
    source_floor = (
        policy.source_min_episode_win_rate
        if standard_lane else policy.source_low_freq_min_episode_win_rate
    )
    source_concentrated = (
        row.get("sourceTop3ProfitShare") is not None
        and f(row.get("sourceTop3ProfitShare")) >= policy.source_top3_concentration_trigger
    )
    copy_n = int(row.get("closedEpisodes30") or 0)
    copy_win = row.get("copyWinRate30")
    copy_concentrated = (
        row.get("copyTop3ProfitShare") is not None
        and f(row.get("copyTop3ProfitShare")) >= policy.source_top3_concentration_trigger
    )
    checks = {
        "sourceSampleLane": standard_lane or low_frequency_lane,
        "sourceWinRate": (
            source_win is not None and f(source_win) >= source_floor
        ),
        "sourceConcentratedBody": (
            not source_concentrated
            or (
                int(row.get("sourceBodyAfterTop3N") or 0) > 0
                and row.get("sourceBodyAfterTop3WinRate") is not None
                and f(row.get("sourceBodyAfterTop3WinRate")) >= policy.source_body_min_win_rate
                and row.get("sourceBodyAfterTop3Pnl") is not None
                and f(row.get("sourceBodyAfterTop3Pnl")) >= 0.0
            )
        ),
        "copyClosedSample": copy_n >= policy.rough_min_closed_30d,
        "copyWinRate": copy_win is not None and f(copy_win) >= policy.rough_min_win_rate,
        "copyConcentratedBody": (
            not copy_body_guard
            or not copy_concentrated
            or (
                int(row.get("copyBodyAfterTop3N") or 0) > 0
                and row.get("copyBodyAfterTop3WinRate") is not None
                and f(row.get("copyBodyAfterTop3WinRate")) >= policy.rough_min_win_rate
                and row.get("copyBodyAfterTop3Pnl") is not None
                and f(row.get("copyBodyAfterTop3Pnl")) >= 0.0
            )
        ),
    }
    failures = (
        ("source_sample_lane_missing", "sourceSampleLane"),
        ("source_win_rate_below_floor", "sourceWinRate"),
        ("source_top3_dependent_body_weak", "sourceConcentratedBody"),
        ("copy_closed_sample_below_floor", "copyClosedSample"),
        ("copy_win_rate_below_floor", "copyWinRate"),
        ("copy_top3_dependent_body_weak", "copyConcentratedBody"),
    )
    first_failure = next(
        (reason for reason, key in failures if not checks[key]), None,
    )
    return {
        "passed": first_failure is None,
        "firstFailure": first_failure,
        "checks": checks,
        "sourceLane": (
            "standard" if standard_lane
            else "strong_low_frequency" if low_frequency_lane
            else None
        ),
        "sourceWinFloor": source_floor,
        "copyWinFloor": policy.rough_min_win_rate,
        "sourceConcentrationTriggered": source_concentrated,
        "copyConcentrationTriggered": copy_concentrated,
    }


def _repeatability_analysis(rows, policy):
    tier_targets = {
        name: [
            row for row in rows
            if _tier_pass(row, floor30, floor7)
        ]
        for name, floor30, floor7 in RETURN_TIERS
    }
    scenarios = (
        ("current_profile_repeatability", False),
        ("closed_copy_body_lottery_guard", True),
    )
    output = {}
    for name, copy_body_guard in scenarios:
        decisions = {
            row["wallet"]: _repeatability_check(
                row, policy, copy_body_guard=copy_body_guard,
            )
            for row in rows
        }
        passed_ids = {
            wallet for wallet, decision in decisions.items()
            if decision["passed"]
        }
        failures = Counter(
            decision["firstFailure"] or "passed"
            for decision in decisions.values()
        )
        tier_pass = {
            tier: sum(row["wallet"] in passed_ids for row in targets)
            for tier, targets in tier_targets.items()
        }
        output[name] = {
            "copyClosedBodyGuard": copy_body_guard,
            "population": len(rows),
            "passed": len(passed_ids),
            "firstFailureCounts": dict(failures.most_common()),
            "tierOperationalTotals": {
                tier: len(targets) for tier, targets in tier_targets.items()
            },
            "tierOperationalPass": tier_pass,
            "tierOperationalRecall": {
                tier: (
                    tier_pass[tier] / len(targets)
                    if targets else None
                )
                for tier, targets in tier_targets.items()
            },
        }
    return output


def analyze(
    research_db_path: str,
    report_path: str,
    *,
    run_key: str | None = None,
    reference_wallet: str | None = None,
) -> dict:
    db = sqlite3.connect(
        f"file:{Path(research_db_path).resolve().as_posix()}?mode=ro", uri=True,
    )
    db.execute("PRAGMA query_only=ON")
    if run_key is None:
        latest = db.execute(
            "SELECT run_key FROM profit_research_run_cache ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if not latest:
            raise ValueError("profit_research_run_missing")
        run_key = str(latest[0])
    run = db.execute(
        "SELECT model_version,started_at,context_blob FROM profit_research_run_cache "
        "WHERE run_key=?",
        (run_key,),
    ).fetchone()
    if not run:
        raise ValueError("profit_research_run_missing")
    context = json.loads(zlib.decompress(run[2]))
    rows = []
    for wallet, status, reason, record_json, artifact_blob in db.execute(
        "SELECT wallet,status,reason,record_json,artifact_blob "
        "FROM profit_research_wallet_cache "
        "WHERE run_key=? ORDER BY wallet",
        (run_key,),
    ):
        artifact = (
            json.loads(zlib.decompress(artifact_blob))
            if artifact_blob is not None else None
        )
        rows.append(_wallet_view(
            str(wallet), str(status), reason, json.loads(record_json), artifact,
        ))
    db.close()

    rough = [row for row in rows if row["return30"] is not None and row["return7"] is not None]
    operational = [row for row in rough if row["operationalActivity"] is True]
    audited = [row for row in rough if row["activityAudited"]]
    ranked = sorted(
        rough,
        key=lambda row: (
            -f(row["profitPriority"]), -f(row["return30"]),
            -f(row["return7"]), row["wallet"],
        ),
    )

    tiers = {}
    for name, floor30, floor7 in RETURN_TIERS:
        passed = [row for row in ranked if _tier_pass(row, floor30, floor7)]
        active = [row for row in passed if row["operationalActivity"] is True]
        sparse = [row for row in passed if row["activityAudited"] and row["operationalActivity"] is not True]
        tiers[name] = {
            "return30Floor": floor30,
            "return7Floor": floor7,
            "roughPassed": len(passed),
            "operationalPassed": len(active),
            "continuous4WeeksPassed": sum(
                int(row.get("activeWeeks4") or 0) >= 4 for row in active
            ),
            "activitySparse": len(sparse),
            "activityNotYetAudited": sum(not row["activityAudited"] for row in passed),
            "operationalWallets": [row for row in active[:64]],
            "sparseWallets": [row for row in sparse[:64]],
        }

    minimum_volume = f(context.get("minimumPerpWeekVolume"))
    leaderboard = context.get("leaderboard") or []
    expected = sum(
        f(dict(dict(item.get("windowPerformances") or ()).get("week") or {}).get("vlm"))
        >= minimum_volume
        for item in leaderboard
    ) if leaderboard and minimum_volume else None
    status_counts = Counter(row["status"] for row in rows)
    reason_counts = Counter(str(row["reason"] or "unknown") for row in rows)
    policy = load_copy_policy(context.get("surface") or {})
    report = {
        "status": "complete" if expected is not None and len(rows) >= expected else "partial",
        "anonymous": True,
        "readOnlySource": True,
        "runKey": run_key,
        "modelVersion": run[0],
        "runStartedAt": run[1],
        "analyzedAt": datetime.now(timezone.utc).isoformat(),
        "walletRows": len(rows),
        "expectedWalletRows": expected,
        "coverage": len(rows) / expected if expected else None,
        "statusCounts": dict(status_counts),
        "reasonCounts": dict(reason_counts.most_common()),
        "roughRows": len(rough),
        "activityAuditedRows": len(audited),
        "operationalRows": len(operational),
        "returnQuantiles": {
            "allRough": _return_quantiles(rough),
            "activityAudited": _return_quantiles(audited),
            "operational": _return_quantiles(operational),
        },
        "returnTiers": tiers,
        "featureBuckets": _bucket_analysis(rough),
        "sampleDepthGrid": _sample_depth_grid(rough),
        "gateSensitivity": _gate_sensitivity(rough),
        "repeatabilityAnalysis": _repeatability_analysis(operational, policy),
        "topRoughCandidates": ranked[:64],
        "referenceWallet": next(
            (row for row in rows if row["wallet"] == reference_wallet), None,
        ) if reference_wallet else None,
    }
    profit_distribution._atomic_json(report_path, report)
    return report
