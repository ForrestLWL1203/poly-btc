"""Quality-first Core formation.

Each bounded binary prefix node may own an independently tuned sizing surface.  This preserves the intended
16 -> 8 -> 12 search: wallet count and capital sizing are evaluated together instead of measuring every count
with parameters fitted only to the incumbent Core.  A later strict leave-one-out pass may remove a non-tail
member only when its actual presence lowers funded shared-account net economics.
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
        # Copy positions use isolated margin.  A liquidation loses that position's bounded margin and the
        # loss is already debited from net_pnl and reflected in max_drawdown.  Treating the count itself as
        # a veto double-charges the same loss and can let one profitable wallet collapse an otherwise valid
        # quality prefix.  Account ruin is still impossible to admit: the portfolio must stay solvent and
        # profitable in both the normal and stressed replays.
        return (
            self.net_pnl > 0
            and self.stress_net_pnl > 0
            and self.max_drawdown < 1.0
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
    )


def search_quality_prefix(initial_count: int, evaluate: Callable[[int], PrefixEvaluation], *,
                          retention_kwargs: Mapping[str, float] | None = None,
                          tie_tolerance: float = .02,
                          exhaustive_below: int = 0) -> PrefixSearchResult:
    """Evaluate quality prefixes and return the best safe economic state.

    Small pools are cheap enough to search exhaustively.  Larger pools use the original bounded binary
    direction (16 -> 8 -> 12 ...) plus boundary neighbours.  The full-size prefix is only the search anchor,
    never a privileged answer; the final answer is the highest-utility feasible evaluated state.
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
    if initial_count <= max(0, int(exhaustive_below)):
        for count in range(1, initial_count + 1):
            get(count)
        if reference.feasible:
            retained = [
                value.count for value in cache.values()
                if retains_reference(reference, value, **retain_args)
            ]
            boundary = min(retained or [initial_count])
        else:
            boundary = max((value.count for value in cache.values() if value.feasible), default=0)
    elif reference.feasible:
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
    feasible = [value for value in cache.values() if value.feasible]
    if not feasible:
        raise RuntimeError("no_feasible_quality_prefix")
    best_utility = max(value.utility for value in feasible)
    tolerance = max(0.0, float(tie_tolerance))
    near_best = [
        value for value in feasible
        if value.utility >= best_utility - max(1.0, abs(best_utility) * tolerance)
    ]
    selected = min(near_best, key=lambda value: (value.count, -value.utility, -value.net_pnl))
    return PrefixSearchResult(
        selected=selected,
        reference=reference,
        evaluated=tuple(cache[count] for count in sorted(cache)),
        boundary=boundary,
    )
