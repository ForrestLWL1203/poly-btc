"""Anonymous, repeatable analysis over the private profit-research database."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import zlib

from hyper.util import f

from . import profit_distribution


RETURN_TIERS = (
    ("50_10", 0.50, 0.10),
    ("40_7_5", 0.40, 0.075),
    ("30_5", 0.30, 0.05),
)
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)


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


def _wallet_view(wallet, status, reason, record):
    windows = ((record.get("rough") or {}).get("windows") or {})
    source = record.get("source") or {}
    current = record.get("current") or {}
    activity = record.get("activity") or {}

    def value(days, key):
        return (windows.get(str(days)) or {}).get(key)

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
        "copyLiquidations30": value(30, "liquidations"),
        "copyOpenLossRatio30": value(30, "openLossRatio"),
        "actionableOpenRate30": value(30, "actionableOpenRate"),
        "pathCompletionRate30": value(30, "pathCompletionRate"),
        "sourceEpisodes30": source.get("source_episode_n_30d"),
        "sourceEpisodes7": source.get("source_episode_n_7d"),
        "sourceWinRate30": source.get("source_win_rate_30d"),
        "sourceWinRate7": source.get("source_win_rate_7d"),
        "sourceTop3ProfitShare": source.get("source_top3_profit_share"),
        "sourceBodyAfterTop3Pnl": source.get("source_body_after_top3_net_pnl"),
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
    for wallet, status, reason, record_json in db.execute(
        "SELECT wallet,status,reason,record_json FROM profit_research_wallet_cache "
        "WHERE run_key=? ORDER BY wallet",
        (run_key,),
    ):
        rows.append(_wallet_view(
            str(wallet), str(status), reason, json.loads(record_json),
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
        "topRoughCandidates": ranked[:64],
        "referenceWallet": next(
            (row for row in rows if row["wallet"] == reference_wallet), None,
        ) if reference_wallet else None,
    }
    profit_distribution._atomic_json(report_path, report)
    return report
