"""Quality-prefix Core formation.

The candidate order is fixed by individual wallet quality.  Portfolio search is therefore a one-dimensional
choice of prefix length, not an arbitrary subset problem.  We tune the full initial prefix first, then use a
monotone retention predicate and binary search to find the smallest prefix that preserves its economics.
Neighbour checks protect the final choice from a slightly non-monotone replay surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping


@dataclass(frozen=True)
class PrefixEvaluation:
    count: int
    net_pnl: float
    stress_net_pnl: float
    max_drawdown: float
    actionable_open_rate: float
    capacity_fit: float
    liquidations: int
    params: Mapping[str, float]
    payload: Mapping[str, object]

    @property
    def utility(self) -> float:
        # max_drawdown is an account-equity fraction.  The caller supplies the account anchor in payload.
        anchor = float(self.payload.get("initialBalance") or 10_000.0)
        return float(self.net_pnl) - float(self.max_drawdown) * anchor

    @property
    def feasible(self) -> bool:
        return (
            self.net_pnl > 0
            and self.stress_net_pnl > 0
            and self.liquidations <= 0
            and self.actionable_open_rate >= 0.70
            and self.capacity_fit >= 0.85
        )


@dataclass(frozen=True)
class PrefixSearchResult:
    selected: PrefixEvaluation
    reference: PrefixEvaluation
    evaluated: tuple[PrefixEvaluation, ...]
    boundary: int


def retains_reference(reference: PrefixEvaluation, candidate: PrefixEvaluation, *,
                      utility_retention: float = .97, net_retention: float = .95,
                      stress_retention: float = .90, utility_slack: float = 50.0,
                      net_slack: float = 100.0, stress_slack: float = 100.0,
                      max_dd_worsen: float = .01) -> bool:
    """Whether a smaller quality prefix preserves the full-prefix portfolio.

    Absolute slack keeps the predicate stable near zero; relative retention governs meaningful portfolios.
    The full reference always passes when feasible.
    """
    if not candidate.feasible:
        return False
    utility_floor = reference.utility - max(abs(reference.utility) * (1.0 - utility_retention), utility_slack)
    net_floor = reference.net_pnl - max(abs(reference.net_pnl) * (1.0 - net_retention), net_slack)
    stress_floor = max(
        0.0,
        reference.stress_net_pnl
        - max(abs(reference.stress_net_pnl) * (1.0 - stress_retention), stress_slack),
    )
    return (
        candidate.utility >= utility_floor
        and candidate.net_pnl >= net_floor
        and candidate.stress_net_pnl >= stress_floor
        and candidate.max_drawdown <= reference.max_drawdown + max_dd_worsen
        and candidate.liquidations <= reference.liquidations
    )


def search_quality_prefix(initial_count: int, evaluate: Callable[[int], PrefixEvaluation], *,
                          retention_kwargs: Mapping[str, float] | None = None,
                          tie_tolerance: float = .02) -> PrefixSearchResult:
    """Tune O(log N) quality prefixes and return the best retained state.

    Search finds the smallest prefix that preserves the fully tuned initial portfolio.  It then evaluates
    immediate neighbours and chooses the best risk-adjusted utility, preferring fewer wallets only when the
    utilities are within ``tie_tolerance``.
    """
    initial_count = int(initial_count)
    if initial_count < 1:
        raise ValueError("initial_count must be positive")
    cache: dict[int, PrefixEvaluation] = {}

    def get(count: int) -> PrefixEvaluation:
        count = max(1, min(initial_count, int(count)))
        if count not in cache:
            value = evaluate(count)
            if int(value.count) != count:
                raise ValueError("prefix evaluation count mismatch")
            cache[count] = value
        return cache[count]

    reference = get(initial_count)
    retain_args = dict(retention_kwargs or {})
    lo, hi = 1, initial_count
    if reference.feasible:
        # Find the smallest prefix that preserves the fully funded initial portfolio.
        while lo < hi:
            mid = (lo + hi) // 2
            if retains_reference(reference, get(mid), **retain_args):
                hi = mid
            else:
                lo = mid + 1
        boundary = lo
    else:
        # Capital contention can make the full high-quality set infeasible.  Feasibility is monotone in
        # the useful direction: removing a low-quality suffix releases capacity.  Find the largest feasible
        # prefix, then compare its neighbours.  This is the same 16 -> 8 -> 12 search direction.
        first = get(1)
        if not first.feasible:
            raise RuntimeError("no_feasible_quality_prefix")
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if get(mid).feasible:
                lo = mid
            else:
                hi = mid - 1
        boundary = lo
    for count in {boundary - 1, boundary, boundary + 1, initial_count}:
        if 1 <= count <= initial_count:
            get(count)
    retained = (
        [
            value for value in cache.values()
            if value.count == initial_count or retains_reference(reference, value, **retain_args)
        ]
        if reference.feasible else [value for value in cache.values() if value.feasible]
    )
    best_utility = max(value.utility for value in retained)
    tolerance = max(0.0, float(tie_tolerance))
    near_best = [
        value for value in retained
        if value.utility >= best_utility - max(1.0, abs(best_utility) * tolerance)
    ]
    selected = (
        min(near_best, key=lambda value: (value.count, -value.utility))
        if reference.feasible
        else max(near_best, key=lambda value: (value.count, value.utility))
    )
    return PrefixSearchResult(
        selected=selected,
        reference=reference,
        evaluated=tuple(cache[count] for count in sorted(cache)),
        boundary=boundary,
    )
