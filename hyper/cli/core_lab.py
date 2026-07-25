#!/usr/bin/env python3
"""Read-only cache audit for the source-Top40 and fills-only rough-Copy funnel.

The source database is opened with ``mode=ro`` and ``PRAGMA query_only``. The command reconstructs source
Episode quality and runs at most 40 K-line-free wallet replays under the active parameter surface. It never
migrates a table, writes a profile, changes parameters, publishes a generation, or starts a process.

Legacy generations may have cached Portfolio decisions without the new official Perp 30-day return. Those
wallets are useful for an explicitly labelled economic preview, but can never be reported as publishable Core
evidence. A complete new generation must collect and persist the missing official return first.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import sqlite3
import time
from types import SimpleNamespace

from hyper import config, params, storage
from hyper.copy.copy_data import load_copyable_fills
from hyper.copy.fills import build_episodes
from hyper.discovery import scanner
from hyper.selection import follow_score, state as selection
from hyper.util import f


DAY_MS = 86_400_000


def _iso_ms(value: str | None) -> int:
    if not value:
        return int(time.time() * 1000)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)


def _columns(db, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def _active_follow_surface(db) -> dict:
    row = db.execute(
        "SELECT sr.params_json FROM strategy_revision sr "
        "JOIN active_strategy_revision ar ON ar.revision=sr.revision WHERE ar.id=1"
    ).fetchone()
    follow = json.loads(row[0]) if row and row[0] else params.load_follow(db)
    follow.update(params.load_category(db, "scanner"))
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
    return follow


def _latest_published_generation(db) -> dict:
    generation = selection.latest_published_generation(db)
    if not generation:
        raise RuntimeError("no_published_generation")
    row = db.execute(
        "SELECT generation,started_at,published_at FROM scan_generation WHERE generation=?",
        (generation,),
    ).fetchone()
    if not row:
        raise RuntimeError("published_generation_row_missing")
    return {"generation": row[0], "startedAt": row[1], "publishedAt": row[2]}


def _generation_profiles(db, generation: str) -> list[dict]:
    available = _columns(db, "profile")
    columns = [
        column for column in storage.PROFILE_COLS.split(",")
        if column in available
    ]
    if "addr" not in columns:
        raise RuntimeError("profile_addr_column_missing")
    if "profile_generation" in available:
        rows = db.execute(
            f"SELECT {','.join(columns)} FROM profile WHERE profile_generation=?",
            (generation,),
        ).fetchall()
    else:
        rows = db.execute(f"SELECT {','.join(columns)} FROM profile").fetchall()
    return [
        {**{column: None for column in storage.PROFILE_COLS.split(",")},
         **dict(zip(columns, row))}
        for row in rows
    ]


def _generation_official_evidence(db, generation: dict) -> dict[str, dict]:
    """Recover the newest immutable official evidence for each profiled wallet."""
    rows = db.execute(
        "SELECT lower(addr),status,reason,payload_json FROM pipeline_audit "
        "WHERE source='scan' AND stage='perp_prefilter' AND addr IS NOT NULL "
        "AND created_at>=? AND created_at<=? ORDER BY id DESC",
        (generation["startedAt"], generation["publishedAt"]),
    ).fetchall()
    out = {}
    for addr, status, reason, payload_json in rows:
        addr = str(addr or "").lower()
        if not addr or addr in out:
            continue
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        official = dict((payload.get("windows") or {}).get("officialPerp30d") or {})
        out[addr] = {
            "status": str(status or ""),
            "reason": str(reason or ""),
            "return30d": official.get("return"),
            "evidenceSufficient": official.get("evidenceSufficient"),
        }
    return out


def _profile_official_evidence(row: dict, fallback: dict | None) -> dict:
    fallback = dict(fallback or {})
    status = row.get("official_perp_status") or fallback.get("status") or ""
    reason = row.get("official_perp_reason") or fallback.get("reason") or ""
    return30 = row.get("official_perp_return_30d")
    if return30 is None:
        return30 = fallback.get("return30d")
    return {
        "status": str(status),
        "reason": str(reason),
        "return30d": float(return30) if return30 is not None else None,
    }


def _structure_namespace(db) -> SimpleNamespace:
    p = SimpleNamespace(
        days=37,
        min_perp=0.60,
        max_daily_eps=30,
        exclude_hft=True,
        hft_min_hold_min=3.0,
        grid_max_adds=3,
        max_single_adds=config.MAX_SINGLE_ADDS_PER_EP,
        max_fills_per_ep=50,
        max_concurrent_pos=config.MAX_CONCURRENT_POS,
        copy_bt_gate_enable=True,
        copy_bt_days=config.COPY_BT_DAYS,
        copy_bt_min_closed=config.COPY_BT_MIN_CLOSED,
        copy_bt_min_net_pnl=config.COPY_BT_MIN_NET_PNL,
    )
    return params.apply_scanner_params(db, p)


def _market_evidence(db) -> tuple[dict, dict, dict]:
    columns = _columns(db, "coin_vol")
    rows = db.execute(
        "SELECT coin,sigma,day_ntl_vlm,oi_notional,max_leverage"
        + (",mark_px" if "mark_px" in columns else "")
        + " FROM coin_vol"
    ).fetchall()
    sigmas, context, marks = {}, {}, {}
    for row in rows:
        coin = str(row[0])
        if row[1] is not None:
            sigmas[coin] = float(row[1])
        context[coin] = {
            "day_ntl_vlm": row[2],
            "oi_notional": row[3],
            "max_leverage": row[4],
        }
        if len(row) > 5 and f(row[5]) > 0.0:
            marks[coin] = f(row[5])
            context[coin]["mark_px"] = f(row[5])
    return sigmas, context, marks


def _source_record(db, row: dict, official: dict, now_ms: int, p, follow: dict) -> dict:
    addr = str(row.get("addr") or "").lower()
    start_ms = int(now_ms) - 37 * DAY_MS
    fills = load_copyable_fills(db, [addr], start_ms)
    structure = scanner._current_sector_structure_policy(fills, int(now_ms), p)
    episodes, _open = build_episodes(fills)
    source = scanner._source_quality_surface(fills, episodes, structure, int(now_ms))
    current_official = bool(
        official.get("status") == "passed"
        and official.get("return30d") is not None
        and f(official.get("return30d")) >= f(follow.get(
            "OFFICIAL_PERP_MIN_RETURN_30D", config.OFFICIAL_PERP_MIN_RETURN_30D,
        ))
    )
    legacy_preview = bool(
        official.get("status") == "passed" and official.get("return30d") is None
    )
    augmented = {
        **row,
        **source,
        "addr": addr,
        "official_perp_status": "passed" if current_official or legacy_preview else official.get("status"),
        "official_perp_reason": official.get("reason") or "official_perp_evidence_missing",
        # A missing legacy return is assigned the admission boundary only for preview ordering. It remains
        # explicitly non-publishable in the report and cannot become production Core.
        "official_perp_return_30d": (
            official.get("return30d")
            if official.get("return30d") is not None
            else config.OFFICIAL_PERP_MIN_RETURN_30D
        ),
        "sector_policy_json": json.dumps(structure, sort_keys=True),
        "data_status": row.get("data_status") or "valid",
        "copy_bt_data_status": row.get("data_status") or "valid",
        "score_as_of_ms": int(now_ms),
    }
    qualification = follow_score.evaluate_source_quality(
        augmented, policy_values=follow, as_of_ms=int(now_ms),
    )
    structural_ok = bool(structure.get("allowed"))
    first_failure = qualification.get("firstFailure")
    if not first_failure and not structural_ok:
        first_failure = "source_structure_unqualified"
    eligible = bool(
        (current_official or legacy_preview)
        and qualification.get("eligible")
        and structural_ok
        and fills
    )
    source_score = follow_score.compute_source_quality_score(
        augmented, policy_values=follow, as_of_ms=int(now_ms),
    )[0]
    return {
        "addr": addr,
        "metrics": augmented,
        "sourceScore": source_score,
        "sourceEligible": eligible,
        "sourceFirstFailure": (
            None if eligible else first_failure or "official_perp_not_qualified"
        ),
        "officialCurrent": current_official,
        "officialLegacyPreview": legacy_preview,
        "official": official,
        "allowedSectors": list(structure.get("allowed") or ()),
        "fillCount": len(fills),
    }


def _rough_record(db, item: dict, *, label: str, generation: str, now_ms: int,
                  follow: dict, sigmas: dict, market_ctx: dict, marks: dict) -> dict:
    replay = scanner._effective_follow_replay(
        db,
        item["metrics"],
        int(now_ms),
        generation_id=generation,
        follow=follow,
        valuation_marks=marks,
        sigmas=sigmas,
        market_ctx=market_ctx,
        strict_path=False,
        qualification_stage="rough",
    )
    qualification = dict(replay.get("qualification") or {})
    detail = dict(replay.get("scoreDetail") or {})
    returns = dict(qualification.get("returns") or {})
    pnl = dict(qualification.get("netPnl") or {})
    source = dict(qualification.get("sourceQuality") or {})
    return {
        "wallet": label,
        "sourceRank": item["sourceRank"],
        "score": round(f(replay.get("score")) * 100, 2),
        "economicPreviewEligible": bool(qualification.get("coreEligible")),
        "publishableEvidenceEligible": bool(
            qualification.get("coreEligible") and item.get("officialCurrent")
        ),
        "firstFailure": qualification.get("firstFailure"),
        "officialPerp30dReturnPct": (
            round(f(item["official"].get("return30d")) * 100, 3)
            if item["official"].get("return30d") is not None else None
        ),
        "officialEvidencePending": not bool(item.get("officialCurrent")),
        "dynamicReturn30dPct": round(f(returns.get("30")) * 100, 3),
        "dynamicReturn7dPct": round(f(returns.get("7")) * 100, 3),
        "netPnl30d": round(f(pnl.get("30")), 2),
        "netPnl7d": round(f(pnl.get("7")), 2),
        "copyWinRatePct": (
            round(f(qualification.get("copyWinRate")) * 100, 2)
            if qualification.get("copyWinRate") is not None else None
        ),
        "openFillRatePct": (
            round(f(qualification.get("openFillRate")) * 100, 2)
            if qualification.get("openFillRate") is not None else None
        ),
        "closedEpisodes30d": qualification.get("closedN"),
        "sourceEpisodes30d": source.get("episodeN30d"),
        "sourceWinRatePct": (
            round(f(source.get("winRate30d")) * 100, 2)
            if source.get("winRate30d") is not None else None
        ),
        "sourceTop3ProfitSharePct": (
            round(f(source.get("top3ProfitShare")) * 100, 2)
            if source.get("top3ProfitShare") is not None else None
        ),
        "sourceBodyWinRatePct": (
            round(f(source.get("bodyAfterTop3WinRate")) * 100, 2)
            if source.get("bodyAfterTop3WinRate") is not None else None
        ),
        "sourceBodyNetPnl": source.get("bodyAfterTop3NetPnl"),
        "allowedSectors": item.get("allowedSectors") or [],
        "scoreComponents": detail.get("components") or {},
    }


def build_report(db, *, as_of: str | None = None, max_rough: int = 40,
                 progress=None) -> dict:
    generation = _latest_published_generation(db)
    now_ms = _iso_ms(as_of or generation.get("publishedAt"))
    follow = _active_follow_surface(db)
    profiles = _generation_profiles(db, generation["generation"])
    official_events = _generation_official_evidence(db, generation)
    p = _structure_namespace(db)
    official_counts = Counter()
    source_failures = Counter()
    source_rows = []
    total = len(profiles)
    for index, row in enumerate(profiles, 1):
        addr = str(row.get("addr") or "").lower()
        official = _profile_official_evidence(row, official_events.get(addr))
        if official.get("status") == "passed" and official.get("return30d") is not None:
            official_counts["current_return_present"] += 1
        elif official.get("status") == "passed":
            official_counts["legacy_return_missing"] += 1
        elif official.get("status") == "deferred_data_error":
            official_counts["deferred"] += 1
        else:
            official_counts["rejected_or_missing"] += 1
        if official.get("status") != "passed":
            continue
        item = _source_record(db, row, official, now_ms, p, follow)
        if item["sourceEligible"]:
            source_rows.append(item)
        else:
            source_failures[item["sourceFirstFailure"]] += 1
        if progress and (index % 10 == 0 or index == total):
            progress("source", index, total)

    source_rows.sort(key=lambda item: (-f(item["sourceScore"]), item["addr"]))
    source_limit = max(0, min(
        int(max_rough),
        int(follow.get("SOURCE_QUALITY_MAX_N", config.SOURCE_QUALITY_MAX_N)),
    ))
    source_pool = source_rows[:source_limit]
    for index, item in enumerate(source_pool, 1):
        item["sourceRank"] = index
        item["label"] = f"wallet_{index:03d}"

    sigmas, market_ctx, marks = _market_evidence(db)
    rough = []
    rough_failures = Counter()
    for index, item in enumerate(source_pool, 1):
        result = _rough_record(
            db,
            item,
            label=item["label"],
            generation=generation["generation"],
            now_ms=now_ms,
            follow=follow,
            sigmas=sigmas,
            market_ctx=market_ctx,
            marks=marks,
        )
        rough.append(result)
        if not result["economicPreviewEligible"]:
            rough_failures[result.get("firstFailure") or "rough_copy_unqualified"] += 1
        if progress:
            progress("rough", index, len(source_pool))

    rough.sort(key=lambda item: (-f(item["score"]), item["sourceRank"]))
    economic = [item for item in rough if item["economicPreviewEligible"]]
    publishable = [item for item in rough if item["publishableEvidenceEligible"]]
    return {
        "status": "complete",
        "readOnly": True,
        "generationPresent": True,
        "replayAt": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
        "profiledCandidateCount": len(profiles),
        "officialEvidenceCounts": dict(sorted(official_counts.items())),
        "sourceQualifiedBeforeCap": len(source_rows),
        "sourceFailureCounts": dict(sorted(source_failures.items())),
        "sourceTop40Count": len(source_pool),
        "excludedBySourceCap": max(0, len(source_rows) - len(source_pool)),
        "roughReplayedCount": len(rough),
        "roughEconomicEligibleCount": len(economic),
        "roughPublishableEvidenceEligibleCount": len(publishable),
        "roughFailureCounts": dict(sorted(rough_failures.items())),
        "roughScoreTop16": [item["wallet"] for item in economic[:16]],
        "officialPerpRescanRequired": any(
            item.get("officialEvidencePending") for item in rough
        ),
        "strictReplayExecuted": False,
        "strictReplayReason": "offline_stage_stops_after_fills_only_rough_copy",
        "wallets": rough,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only source-Top40 and rough-Copy cache audit")
    parser.add_argument("--db", required=True)
    parser.add_argument("--as-of", help="optional ISO replay endpoint; defaults to published generation time")
    parser.add_argument("--max-rough", type=int, default=40)
    parser.add_argument("--progress", action="store_true", help="emit address-free stage progress to stderr")
    args = parser.parse_args()

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")

    def emit(stage, done, total):
        if args.progress:
            import sys
            print(f"audit_progress {stage} {done}/{total}", file=sys.stderr, flush=True)

    try:
        report = build_report(
            db,
            as_of=args.as_of,
            max_rough=max(0, int(args.max_rough)),
            progress=emit,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
