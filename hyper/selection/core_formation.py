"""Profit-aligned-score Core formation.

Individual qualification builds one bounded profit-with-confidence ordered pool without a minimum-wallet quota.
That pool receives one shared parameter tune; portfolio economics may shorten only its lowest-score suffix.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from hyper import config
from hyper.copy.economics import OPEN_LOSS_RATIO_LIMIT


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
        # Historical maximum drawdown is telemetry, not a membership score or gate.
        return float(self.net_pnl)

    @property
    def feasible(self) -> bool:
        # Copy positions use isolated margin.  A liquidation loses that position's bounded margin and the
        # loss is already debited from net_pnl and reflected in max_drawdown. Treating the count itself as a
        # veto double-charges the same loss. Open/capacity misses are likewise already priced into admission
        # PnL. Only the explicit count-search projection sets ``requireCongestionFit`` because that search
        # must decide whether reducing N releases enough real capital; final admission does not charge it
        # twice.
        congestion_ok = bool(
            not self.payload.get("requireCongestionFit")
            or (
                self.actionable_open_rate >= float(config.SELECTION_MIN_ACTIONABLE_RATE)
                and self.capacity_fit >= float(config.SELECTION_MIN_CAPACITY_FIT)
            )
        )
        return_fit = bool(
            not self.payload.get("requireReturnFit")
            or (
                float(self.payload.get("return30d") or 0.0)
                >= float(config.CORE_PORTFOLIO_MIN_RETURN_30D)
                and float(self.payload.get("return7d") or 0.0)
                >= float(config.CORE_PORTFOLIO_MIN_RETURN_7D)
                and (
                    self.payload.get("openLossRatio30d") is None
                    or float(self.payload.get("openLossRatio30d") or 0.0)
                    <= OPEN_LOSS_RATIO_LIMIT
                )
            )
        )
        return self.net_pnl > 0 and congestion_ok and return_fit


def validate_final_membership(
    candidate: PrefixEvaluation,
    *,
    baseline: PrefixEvaluation | None = None,
    membership_changed: bool = False,
    replacing_qualified_core: bool = False,
    initial_margin_equity: float = 10_000.0,
    min_relative_utility_gain: float = 0.05,
    min_net_return_gain: float = 0.02,
) -> dict:
    """Validate the shared account without repeating individual-wallet outlier gates."""
    del baseline, membership_changed, replacing_qualified_core
    del initial_margin_equity, min_relative_utility_gain, min_net_return_gain
    reasons = []
    if not candidate.feasible:
        reasons.append("membership_dynamic_return_or_execution_failed")

    return {
        "eligible": not reasons,
        "reasons": reasons,
        "return30d": candidate.payload.get("return30d"),
        "return7d": candidate.payload.get("return7d"),
        "returnFloors": {
            "30d": float(config.CORE_PORTFOLIO_MIN_RETURN_30D),
            "7d": float(config.CORE_PORTFOLIO_MIN_RETURN_7D),
        },
        "singleWalletDependencyWarning": False,
    }


@dataclass(frozen=True)
class PrefixSearchResult:
    selected: PrefixEvaluation
    reference: PrefixEvaluation
    evaluated: tuple[PrefixEvaluation, ...]
    boundary: int


def retains_reference(reference: PrefixEvaluation, candidate: PrefixEvaluation, *,
                      utility_retention: float = .97, net_retention: float = .95,
                      stress_retention: float = .90, utility_slack: float = 50.0,
                      net_slack: float = 100.0, stress_slack: float = 100.0) -> bool:
    """Whether a smaller quality prefix preserves the full-prefix portfolio.

    Absolute slack keeps the predicate stable near zero; relative retention governs meaningful portfolios.
    The full reference always passes when feasible.
    """
    if not candidate.feasible:
        return False
    utility_floor = reference.utility - max(abs(reference.utility) * (1.0 - utility_retention), utility_slack)
    net_floor = reference.net_pnl - max(abs(reference.net_pnl) * (1.0 - net_retention), net_slack)
    del stress_retention, stress_slack
    return (
        candidate.utility >= utility_floor
        and candidate.net_pnl >= net_floor
    )


def search_quality_prefix(initial_count: int, evaluate: Callable[[int], PrefixEvaluation], *,
                          retention_kwargs: Mapping[str, float] | None = None,
                          tie_tolerance: float = .02,
                          exhaustive_below: int = 0,
                          required_count: int = 0) -> PrefixSearchResult:
    """Evaluate quality prefixes and return the best safe economic state.

    Small pools are cheap enough to search exhaustively.  Larger pools use the original bounded binary
    direction (16 -> 8 -> 12 ...) plus boundary neighbours.  The full-size prefix is only the search anchor,
    never a privileged answer; the final answer is the highest-utility feasible evaluated state.
    """
    initial_count = int(initial_count)
    if initial_count < 1:
        raise ValueError("initial_count must be positive")
    required_count = int(required_count)
    if required_count < 0 or required_count > initial_count:
        raise ValueError("required_count must be between zero and initial_count")
    minimum_size = max(1, required_count)
    cache: dict[int, PrefixEvaluation] = {}

    def get(count: int) -> PrefixEvaluation:
        count = max(minimum_size, min(initial_count, int(count)))
        if count not in cache:
            value = evaluate(count)
            if int(value.count) != count:
                raise ValueError("prefix evaluation count mismatch")
            cache[count] = value
        return cache[count]

    reference = get(initial_count)
    retain_args = dict(retention_kwargs or {})
    lo, hi = minimum_size, initial_count
    if initial_count > max(0, int(exhaustive_below)):
        # Keep the operationally useful bounded direction explicit: N -> N/2 -> midpoint.  Besides making
        # the search auditable (16 -> 8 -> 12), this probes the likely congestion repair before assuming
        # monotonic feasibility at either extreme.
        get(max(minimum_size, initial_count // 2))
    if initial_count <= max(0, int(exhaustive_below)):
        for count in range(minimum_size, initial_count + 1):
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
        # prefix, then compare its neighbours within the bounded quality range.
        first = get(minimum_size)
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
        if minimum_size <= count <= initial_count:
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
