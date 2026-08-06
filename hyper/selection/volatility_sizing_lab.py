"""Read-only BTC-anchored volatility sizing research for the published Core.

This module deliberately consumes the canonical portfolio replay and never writes parameters, membership,
generation evidence, or trading state. The experimental sizing surface is enabled only by a private replay
override which ordinary Paper/Live callers never provide.
"""
from __future__ import annotations

import hashlib
import json
import math

from hyper import config, params
from hyper.copy.copy_backtest import prepare_price_path
from hyper.copy.copy_engine import volatility_target_margin_pct
from hyper.market import price_path
from hyper.selection import auto_tune, state as selection
from hyper.util import f


DAY_MS = 86_400_000
POLICY_VERSION = "btc_volatility_notional_lab_v1"


def _active_follow_surface(db) -> dict:
    row = db.execute(
        "SELECT sr.params_json FROM strategy_revision sr "
        "JOIN active_strategy_revision ar ON ar.revision=sr.revision WHERE ar.id=1"
    ).fetchone()
    follow = json.loads(row[0]) if row and row[0] else params.load_follow(db)
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
    return follow


def _generation_context(db) -> tuple[str, int, list[str]]:
    generation = selection.latest_published_generation(db)
    if not generation:
        raise RuntimeError("volatility_lab_requires_published_generation")
    row = db.execute(
        "SELECT COALESCE(gmm.asof_ms,CAST(strftime('%s',sg.published_at) AS INTEGER)*1000,"
        "CAST(strftime('%s',sg.started_at) AS INTEGER)*1000) "
        "FROM scan_generation sg LEFT JOIN generation_market_manifest gmm "
        "ON gmm.generation=sg.generation WHERE sg.generation=?",
        (generation,),
    ).fetchone()
    now_ms = int((row[0] if row else 0) or 0)
    addrs = list(selection.published_core_addrs(db) or ())
    if now_ms <= 0:
        raise RuntimeError("volatility_lab_generation_asof_missing")
    if not addrs:
        raise RuntimeError("volatility_lab_requires_nonempty_core")
    return generation, now_ms, addrs


def _experiment_settings(*, btc_sigma: float, risk_scale: float) -> dict:
    return {
        "policy_version": POLICY_VERSION,
        "btc_margin_pct": 0.05,
        "btc_leverage": 30.0,
        "btc_sigma": float(btc_sigma),
        "risk_scale": float(risk_scale),
        "tail_sigma": 0.20,
        "tail_exponent": 0.50,
        "margin_grid_step": 0.005,
        "min_margin_pct": 0.005,
        "reserved_adds": 2,
    }


def build_candidate(
    follow: dict,
    *,
    name: str,
    btc_sigma: float,
    risk_scale: float,
    mid_leverage: int,
    high_leverage: int,
) -> dict:
    overrides = dict(follow)
    overrides.update({
        "STABLE_MARGIN_PCT": 0.05,
        "STABLE_LEV_CAP": 30,
        "MID_LEV_CAP": int(mid_leverage),
        "HIGH_LEV_CAP": int(high_leverage),
        "_VOLATILITY_NOTIONAL_SIZING": _experiment_settings(
            btc_sigma=btc_sigma, risk_scale=risk_scale,
        ),
    })
    return {
        "name": str(name),
        "kind": "btc_volatility_notional",
        "riskScale": float(risk_scale),
        "midLeverage": int(mid_leverage),
        "highLeverage": int(high_leverage),
        "overrides": overrides,
    }


def initial_candidates(follow: dict, btc_sigma: float) -> list[dict]:
    return [
        build_candidate(
            follow,
            name=f"vol_scale_{int(scale * 100):03d}_mid12_high6",
            btc_sigma=btc_sigma,
            risk_scale=scale,
            mid_leverage=12,
            high_leverage=6,
        )
        for scale in (0.70, 0.85, 1.00)
    ]


def coordinate_candidates(follow: dict, btc_sigma: float, risk_scale: float) -> list[dict]:
    axes = ((10, 6), (15, 6), (12, 5), (12, 8))
    return [
        build_candidate(
            follow,
            name=(
                f"vol_scale_{int(risk_scale * 100):03d}"
                f"_mid{mid_leverage}_high{high_leverage}"
            ),
            btc_sigma=btc_sigma,
            risk_scale=risk_scale,
            mid_leverage=mid_leverage,
            high_leverage=high_leverage,
        )
        for mid_leverage, high_leverage in axes
    ]


def _window_summary(windows: dict) -> dict:
    out = {}
    for days, result in sorted(windows.items(), reverse=True):
        start = max(1.0, f(result.get("window_start_equity")))
        pnl = f(result.get("copy_net_pnl"))
        out[str(days)] = {
            "netPnl": pnl,
            "roi": pnl / start,
            "startEquity": start,
            "endEquity": f(result.get("window_end_equity")),
            "liquidations": int(result.get("liquidations") or 0),
            "maxLiquidationLossPct": f(result.get("max_liquidation_loss_pct")),
            "maxDrawdown": f(result.get("max_drawdown")),
            "openCaptureRate": f(result.get("effective_open_follow_rate")),
            "capacityFit": f(result.get("execution_capacity_fit")),
            "cashCongestionFit": f(result.get("cash_congestion_fit")),
            "deployment": dict(result.get("deployment_distribution") or {}),
            "skipReasons": dict(result.get("skip_reasons") or {}),
            "tierEconomics": dict(result.get("tier_economics") or {}),
            "pricePathCoverage": f(result.get("price_path_coverage")),
            "ambiguousLiquidations": int(result.get("ambiguous_liquidations") or 0),
        }
    return out


def _quick_rank(item: dict) -> tuple:
    windows = item["windows"]
    primary = windows.get("30") or {}
    recent_ok = all(f((windows.get(str(days)) or {}).get("netPnl")) > 0.0 for days in (14, 7))
    catastrophic = f(primary.get("maxLiquidationLossPct")) >= 0.08
    capacity = f(primary.get("capacityFit"))
    return (
        int(recent_ok and not catastrophic and capacity >= 0.70),
        f(primary.get("netPnl")),
        -int(primary.get("liquidations") or 0),
        capacity,
    )


def _strict_shortlist(experimental: list[dict], limit: int = 3) -> list[dict]:
    ordered = sorted(experimental, key=_quick_rank, reverse=True)
    if not ordered:
        return []
    best_pnl = max(1e-9, f((ordered[0]["windows"].get("30") or {}).get("netPnl")))
    near = [
        item for item in ordered
        if f((item["windows"].get("30") or {}).get("netPnl")) >= best_pnl * 0.92
    ] or ordered
    picks = [ordered[0]]
    picks.append(min(near, key=lambda item: (
        int((item["windows"].get("30") or {}).get("liquidations") or 0),
        f((item["windows"].get("30") or {}).get("maxDrawdown")),
        -f((item["windows"].get("30") or {}).get("netPnl")),
    )))
    picks.append(max(near, key=lambda item: (
        f((item["windows"].get("30") or {}).get("capacityFit")),
        f((item["windows"].get("30") or {}).get("netPnl")),
    )))
    out, seen = [], set()
    for item in picks:
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _recommendation(baseline: dict, candidates: list[dict]) -> dict:
    base30 = baseline["windows"]["30"]
    base_roi = f(base30.get("roi"))
    base_liqs = int(base30.get("liquidations") or 0)
    base_dd = f(base30.get("maxDrawdown"))
    accepted = []
    for item in candidates:
        windows = item["windows"]
        primary = windows["30"]
        reasons = []
        if any(f(windows[str(days)].get("netPnl")) <= 0.0 for days in (30, 14, 7)):
            reasons.append("non_positive_recent_window")
        if f(primary.get("maxLiquidationLossPct")) >= 0.08:
            reasons.append("catastrophic_liquidation")
        if int(primary.get("liquidations") or 0) > base_liqs + max(2, math.ceil(base_liqs * 0.25)):
            reasons.append("liquidations_increased_too_much")
        if f(primary.get("maxDrawdown")) > max(base_dd + 0.10, base_dd * 1.25):
            reasons.append("drawdown_increased_too_much")
        if base_roi > 0.0 and f(primary.get("roi")) < base_roi * 0.80:
            reasons.append("thirty_day_roi_collapsed_over_20pct")
        if f(primary.get("pricePathCoverage")) < float(config.CORE_PRICE_PATH_MIN_COVERAGE):
            reasons.append("price_path_incomplete")
        item["accepted"] = not reasons
        item["rejectionReasons"] = reasons
        if not reasons:
            accepted.append(item)
    if not accepted:
        return {
            "status": "keep_current",
            "reason": "no_experimental_surface_passed_offline_risk_and_return_gates",
        }
    best_profit = max(f(item["windows"]["30"].get("netPnl")) for item in accepted)
    near = [
        item for item in accepted
        if f(item["windows"]["30"].get("netPnl")) >= best_profit * 0.92
    ]
    winner = min(near, key=lambda item: (
        int(item["windows"]["30"].get("liquidations") or 0),
        f(item["windows"]["30"].get("maxDrawdown")),
        -f(item["windows"]["30"].get("capacityFit")),
        -f(item["windows"]["30"].get("netPnl")),
    ))
    return {
        "status": "experimental_candidate_available",
        "candidate": winner["name"],
        "roiChangeVsCurrent": (
            f(winner["windows"]["30"].get("roi")) / base_roi - 1.0
            if base_roi > 0.0 else None
        ),
        "liquidationChangeVsCurrent": (
            int(winner["windows"]["30"].get("liquidations") or 0) - base_liqs
        ),
        "stillOfflineOnly": True,
    }


def _coin_surface(
    fills: list[dict], sigmas: dict, market_ctx: dict, candidate: dict,
    follow: dict,
) -> list[dict]:
    settings = candidate["overrides"]["_VOLATILITY_NOTIONAL_SIZING"]
    high_sigma_min = f(follow.get("HIGH_SIGMA_MIN", config.HIGH_SIGMA_MIN))
    tier_caps = {
        "stable": f(follow.get("STABLE_COIN_CAP_PCT", config.STABLE_COIN_CAP_PCT)),
        "mid": f(follow.get("MID_COIN_CAP_PCT", config.MID_COIN_CAP_PCT)),
        "high": f(follow.get("HIGH_COIN_CAP_PCT", config.HIGH_COIN_CAP_PCT)),
    }
    margin_equity_pct = f(follow.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT))
    coins = sorted({str(row.get("coin") or "") for row in fills if row.get("coin")})
    out = []
    from hyper.copy.copy_engine import tier_for_sigma
    for coin in coins:
        sigma = f(sigmas.get(coin)) or f(config.VOL_FALLBACK_SIGMA)
        tier = tier_for_sigma(sigma, high_sigma_min, coin)
        cap = {"stable": 30, "mid": candidate["midLeverage"], "high": candidate["highLeverage"]}[tier]
        venue = f((market_ctx.get(coin) or {}).get("max_leverage"))
        leverage = min(float(cap), venue) if venue > 0.0 else float(cap)
        margin_pct = volatility_target_margin_pct(
            coin=coin, sigma=sigma, leverage=leverage, tier=tier,
            tier_coin_cap=tier_caps, margin_equity_pct=margin_equity_pct,
            settings=settings,
        )
        out.append({
            "coin": coin,
            "sigma": sigma,
            "sigmaVsBtc": sigma / max(1e-12, f(settings.get("btc_sigma"))),
            "tier": tier,
            "venueMaxLeverage": venue or None,
            "leverage": leverage,
            "marginPct": margin_pct,
            "plannedNotionalPctOfSizingEquity": margin_pct * leverage,
        })
    return out


def run_lab(db, *, initial_balance: float | None = None, progress=None) -> dict:
    generation, now_ms, addrs = _generation_context(db)
    follow = _active_follow_surface(db)
    sigmas = auto_tune._load_sigmas(db, generation)
    market_ctx = auto_tune._load_market_ctx(db, generation)
    btc_sigma = f(sigmas.get("BTC"))
    if btc_sigma <= 0.0:
        raise RuntimeError("volatility_lab_btc_sigma_missing")
    window_fills = auto_tune._portfolio_window_fills(db, addrs, now_ms)
    if not window_fills or not any(window_fills.values()):
        raise RuntimeError("volatility_lab_core_fills_unavailable")
    max_days = max(window_fills)
    fills = list(window_fills[max_days])

    def evaluate(candidate: dict, *, strict: bool, path_rows=None, path_meta=None) -> dict:
        windows = auto_tune._candidate_windows(
            db, addrs, sigmas, candidate["overrides"], now_ms,
            window_fills=window_fills, market_ctx=market_ctx,
            path_rows=path_rows, path_meta=path_meta,
            initial_balance=initial_balance, compact=True,
        )
        result = {**candidate, "windowsRaw": windows, "windows": _window_summary(windows)}
        result.pop("overrides", None)
        result["validation"] = "strict_price_path" if strict else "quick_fills_only"
        if progress:
            progress(result["name"], result["validation"])
        return result

    baseline_candidate = {
        "name": "current_active_surface",
        "kind": "current",
        "riskScale": None,
        "midLeverage": int(f(follow.get("MID_LEV_CAP", config.MID_LEV_CAP))),
        "highLeverage": int(f(follow.get("HIGH_LEV_CAP", config.HIGH_LEV_CAP))),
        "overrides": dict(follow),
    }
    quick = [evaluate(baseline_candidate, strict=False)]
    stage_one = []
    for candidate in initial_candidates(follow, btc_sigma):
        raw = evaluate(candidate, strict=False)
        stage_one.append(raw)
        quick.append(raw)
    scale_winner = max(stage_one, key=_quick_rank)
    scale = f(scale_winner.get("riskScale"))
    known = {item["name"] for item in quick}
    for candidate in coordinate_candidates(follow, btc_sigma, scale):
        if candidate["name"] in known:
            continue
        quick.append(evaluate(candidate, strict=False))

    experimental_quick = [item for item in quick if item["kind"] != "current"]
    shortlist = _strict_shortlist(experimental_quick, limit=3)
    path_start = now_ms - (max_days + int(config.COPY_BT_WARMUP_DAYS)) * DAY_MS
    path_rows = prepare_price_path(price_path.load_refined(db, fills, path_start, now_ms))
    path_meta = price_path.coverage(db, fills, path_start, now_ms)
    baseline_strict_raw = evaluate(
        baseline_candidate, strict=True, path_rows=path_rows, path_meta=path_meta,
    )
    baseline_strict = {
        key: value for key, value in baseline_strict_raw.items() if key != "windowsRaw"
    }
    strict = []
    source_candidates = {
        candidate["name"]: candidate
        for candidate in [
            *initial_candidates(follow, btc_sigma),
            *coordinate_candidates(follow, btc_sigma, scale),
        ]
    }
    for item in shortlist:
        candidate = source_candidates[item["name"]]
        strict_item = evaluate(
            candidate, strict=True, path_rows=path_rows, path_meta=path_meta,
        )
        strict_item.pop("windowsRaw", None)
        strict.append(strict_item)
    recommendation = _recommendation(baseline_strict, strict)
    chosen = next(
        (item for item in strict if item["name"] == recommendation.get("candidate")),
        strict[0] if strict else None,
    )
    surface_candidate = source_candidates.get(chosen["name"]) if chosen else None
    report = {
        "status": "complete",
        "readOnly": True,
        "published": False,
        "policyVersion": POLICY_VERSION,
        "generationFingerprint": hashlib.sha256(generation.encode()).hexdigest()[:12],
        "asOfMs": now_ms,
        "coreCount": len(addrs),
        "fillCount": len(fills),
        "initialBalance": float(config.INITIAL_BALANCE if initial_balance is None else initial_balance),
        "btcAnchor": {"marginPct": 0.05, "leverage": 30, "notionalPct": 1.50, "sigma": btc_sigma},
        "search": {
            "riskScales": [0.70, 0.85, 1.00],
            "selectedRiskScale": scale,
            "midLeverages": [10, 12, 15],
            "highLeverages": [5, 6, 8],
            "quickReplayCount": len(quick),
            "strictExperimentalCount": len(strict),
        },
        "pricePath": {
            "rows": len(path_rows),
            "coverage": f(path_meta.get("coverage")),
            "missingCoinCount": len(path_meta.get("missingCoins") or ()),
        },
        "baseline": baseline_strict,
        "strictCandidates": strict,
        "quickCandidates": [
            {key: value for key, value in item.items() if key != "windowsRaw"}
            for item in quick
        ],
        "recommendation": recommendation,
        "coinSurface": (
            _coin_surface(fills, sigmas, market_ctx, surface_candidate, follow)
            if surface_candidate else []
        ),
        "databaseChanges": int(getattr(db, "total_changes", 0)),
    }
    return report
