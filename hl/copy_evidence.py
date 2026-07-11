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


@dataclass(frozen=True)
class PortfolioPnlEvidence:
    net_pnl: float
    pnl_lcb: float
    positive_probability: float
    evidence_days: int


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
        # liquidation remains a separate hard risk signal.
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


def summarize_portfolio_pnl(
    positions: Iterable[Mapping],
    *,
    seed: str = "",
    prior_days: int = 3,
    bootstrap_draws: int = 800,
    lower_quantile: float = 0.05,
) -> PortfolioPnlEvidence:
    """Daily-block bootstrap for shared-account portfolio PnL.

    This keeps dollar PnL where it belongs: portfolio capital allocation.  It
    is deliberately separate from per-wallet ranking, which uses normalized
    return.  Correlated closes on the same day remain one resampling block.
    """
    days: dict[int, float] = {}
    net = 0.0
    for position in positions or ():
        closed_at = int(_finite(position.get("closed_at")))
        if closed_at <= 0:
            continue
        pnl = _finite(position.get("net_pnl"))
        net += pnl
        days[closed_at // DAY_MS] = days.get(closed_at // DAY_MS, 0.0) + pnl
    observed = [days[day] for day in sorted(days)]
    if not observed:
        return PortfolioPnlEvidence(0.0, 0.0, 0.0, 0)
    population = observed + [0.0] * max(0, int(prior_days))
    digest = hashlib.sha256(("portfolio:" + str(seed)).encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    draws = []
    for _ in range(max(100, int(bootstrap_draws))):
        draws.append(sum(population[rng.randrange(len(population))] for _ in range(len(population))))
    draws.sort()
    index = min(len(draws) - 1, max(0, int(math.floor(lower_quantile * (len(draws) - 1)))))
    return PortfolioPnlEvidence(
        net_pnl=net,
        pnl_lcb=draws[index],
        positive_probability=sum(1 for value in draws if value > 0.0) / len(draws),
        evidence_days=len(observed),
    )
