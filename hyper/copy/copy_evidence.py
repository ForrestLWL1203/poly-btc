"""Non-overlapping, capital-normalized copy evidence.

One closed copy position contributes ``net_pnl / occupied_margin``.  Evidence is
blocked by close-day so a burst of fills or several highly correlated episodes
on one market day does not masquerade as independent confidence.  Bootstrap
sampling is deterministic for a wallet/generation seed, making scans and audits
reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
import statistics
from typing import Iterable, Mapping


DAY_MS = 86_400_000


def _finite(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


@dataclass(frozen=True)
class CopyEvidence:
    expected_return: float | None
    return_lcb: float | None
    positive_probability: float | None
    return_volatility: float | None
    episode_count: int
    evidence_days: int
    recent_return_14d: float | None
    recent_return_7d: float | None


def _endpoint_pnl(position: Mapping) -> float:
    if position.get("net_pnl") is not None:
        return _finite(position.get("net_pnl"))
    return _finite(position.get("unrealized_pnl"))


def _closed_campaign_records(positions: Iterable[Mapping]) -> list[dict]:
    """Return independent closed Campaign records.

    Campaign identity deliberately matches ``copy_backtest.campaign_structure_metrics``: overlapping
    positions on the same wallet, market board and direction are one economic decision, not several votes.
    """
    grouped: dict[tuple[str, str, str], list[tuple[int, float, Mapping, bool]]] = {}
    for position in positions or ():
        opened = int(_finite(position.get("opened_at")))
        closed = int(_finite(position.get("closed_at")))
        is_open = str(position.get("status") or "open") == "open" or closed <= 0
        end = float("inf") if is_open else float(closed)
        coin = str(position.get("coin") or "")
        key = (
            str(position.get("addr") or "").lower(),
            "stock" if coin.lower().startswith("xyz:") else "crypto",
            str(position.get("side") or "").lower(),
        )
        grouped.setdefault(key, []).append((opened, end, position, is_open))

    out: list[dict] = []
    for rows in grouped.values():
        rows.sort(key=lambda row: (row[0], row[1]))
        current = None
        for opened, end, position, is_open in rows:
            if current is None or opened > current["end"]:
                if current is not None and current["closed"]:
                    out.append({
                        "closed_at": int(current["end"]),
                        "net_pnl": float(current["pnl"]),
                        "fee_drag": float(current["fees"]),
                    })
                current = {
                    "end": end,
                    "closed": not is_open,
                    "pnl": _endpoint_pnl(position),
                    "fees": max(0.0, _finite(position.get("fee_drag"))),
                }
            else:
                current["end"] = max(current["end"], end)
                current["closed"] = bool(current["closed"] and not is_open)
                current["pnl"] += _endpoint_pnl(position)
                current["fees"] += max(0.0, _finite(position.get("fee_drag")))
        if current is not None and current["closed"]:
            out.append({
                "closed_at": int(current["end"]),
                "net_pnl": float(current["pnl"]),
                "fee_drag": float(current["fees"]),
            })
    return sorted(out, key=lambda row: row["closed_at"])


def _closed_campaign_rows(positions: Iterable[Mapping]) -> list[tuple[int, float]]:
    return [
        (int(row["closed_at"]), float(row["net_pnl"]))
        for row in _closed_campaign_records(positions)
    ]


def _equity_at(samples: list[tuple[int, float]], stamp: int) -> float | None:
    """Last marked equity at or before ``stamp`` on one continuous strict replay path."""
    value = None
    for sample_stamp, equity in samples:
        if sample_stamp > stamp:
            break
        value = equity
    return value


def summarize_campaign_stability(
    positions: Iterable[Mapping],
    *,
    now_ms: int,
    fold_days: int = 7,
    fold_count: int = 4,
    min_campaigns: int = 2,
    min_evaluable: int = 4,
    min_profitable: int = 4,
    min_return: float = 0.05,
    initial_equity: float = 10_000.0,
    path_equity_samples: Iterable[Mapping] = (),
    require_cost_stress: bool = True,
    min_net_per_closed_return: float = 0.0,
    max_loss_to_total_profit: float | None = None,
) -> dict:
    """Evaluate fee-paid Copy returns on adjacent, non-overlapping time folds.

    Returns use the continuously marked strict-replay equity path when it is available, so the denominator
    is each fold's actual starting equity and profits compound naturally.  The cache-only fallback carries
    realized Campaign PnL forward on the same capital base. When ``require_cost_stress`` is enabled, the
    four-fold aggregate must remain positive after charging an extra half of its already-paid taker fees.
    """
    fold_days = max(1, int(fold_days))
    fold_count = max(1, int(fold_count))
    min_campaigns = max(0, int(min_campaigns))
    min_evaluable = max(1, int(min_evaluable))
    min_profitable = max(1, int(min_profitable))
    min_return = max(0.0, float(min_return))
    min_net_per_closed_return = max(0.0, float(min_net_per_closed_return))
    max_loss_to_total_profit = (
        None if max_loss_to_total_profit is None
        else max(0.0, float(max_loss_to_total_profit))
    )
    initial_equity = max(1.0, _finite(initial_equity, 10_000.0))
    width = fold_days * DAY_MS
    start = int(now_ms) - fold_count * width
    positions = list(positions or ())
    records = _closed_campaign_records(positions)
    closed_positions = [
        {
            "closed_at": int(_finite(position.get("closed_at"))),
            "net_pnl": _endpoint_pnl(position),
        }
        for position in positions
        if str(position.get("status") or "open") != "open"
        and int(_finite(position.get("closed_at"))) > 0
    ]
    rows = [
        row for row in records
        if start <= int(row["closed_at"]) <= int(now_ms)
    ]
    path_samples = sorted(
        (
            (int(_finite(row.get("time"))), _finite(row.get("equity")))
            for row in path_equity_samples or ()
            if int(_finite(row.get("time"))) >= 0 and _finite(row.get("equity")) > 0.0
        ),
        key=lambda row: row[0],
    )
    realized_before_start = sum(
        float(row["net_pnl"]) for row in records if int(row["closed_at"]) < start
    )
    carried_equity = max(1.0, initial_equity + realized_before_start)
    folds = []
    for index in range(fold_count):
        lo = start + index * width
        hi = lo + width
        values = [
            row for row in rows
            if lo <= int(row["closed_at"]) < hi
            or (index == fold_count - 1 and int(row["closed_at"]) == hi)
        ]
        closed_values = [
            row for row in closed_positions
            if lo <= int(row["closed_at"]) < hi
            or (index == fold_count - 1 and int(row["closed_at"]) == hi)
        ]
        campaign_net = sum(float(row["net_pnl"]) for row in values)
        fee_drag = sum(float(row["fee_drag"]) for row in values)
        path_start_equity = _equity_at(path_samples, lo)
        path_end_equity = _equity_at(path_samples, hi)
        path_complete = bool(path_start_equity is not None and path_end_equity is not None)
        start_equity = path_start_equity if path_complete else carried_equity
        net = (
            float(path_end_equity) - float(path_start_equity)
            if path_complete else campaign_net
        )
        end_equity = max(1.0, float(start_equity) + net)
        fold_return = net / max(1.0, float(start_equity))
        cost_stress_net = net - 0.5 * max(0.0, fee_drag)
        evaluable = len(values) >= min_campaigns
        return_ok = fold_return > 0.0 if min_return <= 0.0 else fold_return >= min_return
        average_closed_net = (
            sum(float(row["net_pnl"]) for row in closed_values) / len(closed_values)
            if closed_values else None
        )
        average_closed_return = (
            average_closed_net / max(1.0, float(start_equity))
            if average_closed_net is not None else None
        )
        density_ok = bool(
            min_net_per_closed_return <= 0.0
            or (
                average_closed_return is not None
                and average_closed_return >= min_net_per_closed_return
            )
        )
        # A material weekly account return plus positive 1.5x-cost stress already proves that the
        # follower earned more than modeled execution costs.  Per-close density is still valuable for
        # ranking thin/high-turnover wallets, but hard-gating every fold on it double-counts the same
        # economics and can reject a strongly profitable diversified portfolio merely because it closed
        # many independent positions.
        qualified = bool(
            evaluable and return_ok
            and (cost_stress_net > 0.0 or not require_cost_stress)
        )
        folds.append({
            "index": index + 1, "startMs": lo, "endMs": hi,
            "campaigns": len(values), "netPnl": net,
            "campaignNetPnl": campaign_net, "feeDrag": fee_drag,
            "costStressNetPnl": cost_stress_net,
            "closedPositionN": len(closed_values),
            "averageClosedNetPnl": average_closed_net,
            "averageClosedNetReturn": average_closed_return,
            "averageClosedNetReturnFloor": min_net_per_closed_return,
            "economicDensityPassed": density_ok,
            "startEquity": float(start_equity), "endEquity": end_equity,
            "return": fold_return, "returnFloor": min_return,
            "equitySource": "marked_path" if path_complete else "realized_fallback",
            "evaluable": evaluable, "profitable": bool(evaluable and net > 0.0),
            "qualified": qualified,
        })
        carried_equity = end_equity
    evaluated = [fold for fold in folds if fold["evaluable"]]
    profitable = [fold for fold in evaluated if fold["profitable"]]
    return_qualified = [
        fold for fold in evaluated
        if (
            float(fold["return"]) > 0.0
            if min_return <= 0.0
            else float(fold["return"]) >= min_return
        )
    ]
    qualified = [fold for fold in evaluated if fold["qualified"]]
    total_net = sum(float(fold["netPnl"]) for fold in evaluated)
    losing = [abs(float(fold["netPnl"])) for fold in evaluated if float(fold["netPnl"]) < 0.0]
    worst_loss = max(losing, default=0.0)
    worst_loss_to_total_profit = (
        worst_loss / total_net if worst_loss > 0.0 and total_net > 0.0
        else (None if worst_loss > 0.0 else 0.0)
    )
    loss_bound_passed = bool(
        not losing
        or (
            max_loss_to_total_profit is not None
            and total_net > 0.0
            and worst_loss_to_total_profit is not None
            and worst_loss_to_total_profit <= max_loss_to_total_profit
        )
    )
    aggregate_cost_stress_net = sum(float(fold["costStressNetPnl"]) for fold in evaluated)
    aggregate_cost_stress_passed = bool(
        not require_cost_stress or aggregate_cost_stress_net > 0.0
    )
    sufficient = len(evaluated) >= min_evaluable
    passed = bool(
        sufficient
        and len(return_qualified) >= min_profitable
        and loss_bound_passed
        and aggregate_cost_stress_passed
    )
    return {
        "version": "nonoverlap-weekly-return-v3", "foldDays": fold_days,
        "folds": folds, "evaluableFolds": len(evaluated),
        "profitableFolds": len(profitable), "qualifiedFolds": len(qualified),
        "returnQualifiedFolds": len(return_qualified),
        "requiredEvaluableFolds": min_evaluable,
        "requiredProfitableFolds": min_profitable,
        "minReturn": min_return,
        "maxLossToTotalProfit": max_loss_to_total_profit,
        "worstLossToTotalProfit": worst_loss_to_total_profit,
        "lossBoundPassed": loss_bound_passed,
        "costStressRequired": bool(require_cost_stress),
        "aggregateCostStressNetPnl": aggregate_cost_stress_net,
        "aggregateCostStressPassed": aggregate_cost_stress_passed,
        "minNetPerClosedReturn": min_net_per_closed_return,
        "economicDensityDiagnosticOnly": True,
        "allEconomicDensityPassed": all(
            bool(fold["economicDensityPassed"]) for fold in evaluated
        ) if evaluated else False,
        "allCostStressPositive": all(
            float(fold["costStressNetPnl"]) > 0.0 for fold in evaluated
        ) if evaluated else False,
        "totalNetPnl": total_net,
        "evidenceSufficient": sufficient, "passed": passed,
    }


def _episode_rows(positions: Iterable[Mapping]) -> list[tuple[int, float]]:
    rows = []
    for position in positions or ():
        margin = _finite(position.get("margin"))
        closed_at = int(_finite(position.get("closed_at")))
        if margin <= 0 or closed_at <= 0:
            continue
        value = _finite(position.get("net_pnl")) / margin
        # A malformed replay row must not dominate the entire model.  Values
        # outside +/-100% of occupied isolated margin are clipped for evidence;
        # proxy liquidation remains a separate final-surface risk metric.
        rows.append((closed_at, max(-1.0, min(1.0, value))))
    return rows


def _blocked(rows: list[tuple[int, float]]) -> list[list[float]]:
    days: dict[int, list[float]] = {}
    for closed_at, value in rows:
        days.setdefault(closed_at // DAY_MS, []).append(value)
    return [days[day] for day in sorted(days)]


def _recent_mean(rows: list[tuple[int, float]], now_ms: int | None, days: int) -> float | None:
    if not rows:
        return None
    end = int(now_ms or max(ts for ts, _ in rows))
    values = [value for ts, value in rows if ts >= end - days * DAY_MS]
    return statistics.fmean(values) if values else None


def summarize_copy_evidence(
    positions: Iterable[Mapping],
    *,
    seed: str = "",
    now_ms: int | None = None,
    prior_episodes: int = 5,
    bootstrap_draws: int = 800,
    lower_quantile: float = 0.05,
) -> CopyEvidence:
    rows = _episode_rows(positions)
    blocks = _blocked(rows)
    n = len(rows)
    if not rows or not blocks:
        return CopyEvidence(None, None, None, None, 0, 0, None, None)

    prior_episodes = max(0, int(prior_episodes))
    shrink_denom = n + prior_episodes
    expected = sum(value for _, value in rows) / max(1, shrink_denom)
    raw_values = [value for _, value in rows]
    volatility = statistics.pstdev(raw_values + [0.0] * prior_episodes) if shrink_denom > 1 else 0.0

    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    # Zero-return prior blocks represent the uncertainty that is absent from a
    # short history.  Without them a one-day winning burst would bootstrap to a
    # 100% positive probability because every resample is the same day.
    prior_blocks = [[0.0] for _ in range(prior_episodes)]
    population = blocks + prior_blocks
    sample_block_n = len(population)
    draws = []
    for _ in range(max(100, int(bootstrap_draws))):
        sampled = [population[rng.randrange(len(population))] for _ in range(sample_block_n)]
        total = sum(sum(block) for block in sampled)
        count = sum(len(block) for block in sampled)
        draws.append(total / max(1, count))
    draws.sort()
    index = min(len(draws) - 1, max(0, int(math.floor(lower_quantile * (len(draws) - 1)))))
    lcb = draws[index]
    positive_probability = sum(1 for value in draws if value > 0.0) / len(draws)
    return CopyEvidence(
        expected_return=expected,
        return_lcb=lcb,
        positive_probability=positive_probability,
        return_volatility=volatility,
        episode_count=n,
        evidence_days=len(blocks),
        recent_return_14d=_recent_mean(rows, now_ms, 14),
        recent_return_7d=_recent_mean(rows, now_ms, 7),
    )
