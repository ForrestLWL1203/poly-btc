"""Pure copy-trade decision helpers shared by live observer and backtests."""

from __future__ import annotations

from dataclasses import dataclass

from hyper import config
from .sizing import margin_pct_for_deploy, sizing_equity_for_drawdown
from hyper.util import f


@dataclass(frozen=True)
class OpenSizingParams:
    stable_sigma_max: float
    high_sigma_min: float
    tier_margin: dict
    tier_margin_min: dict
    tier_lev_cap: dict
    tier_min_notional: dict
    tier_coin_cap: dict
    min_lev: float
    stock_max_lev: float
    deploy_full_pct: float
    max_deploy_pct: float
    min_open_margin_pct: float
    capital_anchor: float = config.INITIAL_BALANCE
    drawdown_exponent: float = config.SIZING_DRAWDOWN_EXPONENT
    drawdown_max_multiplier: float = config.SIZING_DRAWDOWN_MAX_MULTIPLIER
    margin_equity_pct: float = config.MARGIN_EQUITY_PCT


@dataclass(frozen=True)
class OpenSizingPlan:
    ok: bool
    reason: str
    tier: str
    side: str
    margin_pct: float
    margin: float
    notional: float
    leverage: float
    size: float
    liq_px: float
    room: float
    deploy_room: float
    available: float
    wanted_margin: float
    master_notional: float
    risk_equity: float
    sizing_equity: float
    margin_equity: float


@dataclass(frozen=True)
class ProfitTailDecision:
    close: bool
    reason: str
    remaining_fraction: float
    close_now_profit: float
    loss_to_liquidation: float
    giveback_fraction: float


@dataclass(frozen=True)
class SmartTakeProfitDecision:
    armed: bool
    trigger: bool
    stage: int
    peak_pnl: float
    base_size: float
    current_pnl: float
    favorable_move: float
    giveback_fraction: float
    close_size: float
    remaining_size: float
    exit_fee: float
    reason: str


def smart_add_margin_ceiling(
    *,
    coin_room: float,
    min_add_margin: float,
    reserved_adds: int = config.SMART_ADD_MIN_CAPACITY,
) -> float:
    """Largest first margin that still leaves an executable final reserved add.

    With four reserved adds, open plus the first three full-sized adds consume
    four first-margin units. The fourth add may fill the remaining coin room.
    """
    adds = max(1, int(reserved_adds or 1))
    return max(0.0, (max(0.0, coin_room) - max(0.0, min_add_margin)) / adds)


def smart_add_order_margin(
    *,
    first_margin: float,
    target_ratio: float,
    followed_margin: float,
    coin_room: float,
    risk_available: float,
    wallet_sector_side_room: float | None = None,
    wallet_room: float | None = None,
    total_margin_room: float | None = None,
) -> float:
    """Size one target add order; a single order cannot consume multiple add slots."""
    first = max(0.0, float(first_margin or 0.0))
    followed = max(0.0, float(followed_margin or 0.0))
    group_room = (
        float("inf") if wallet_sector_side_room is None
        else max(0.0, float(wallet_sector_side_room or 0.0))
    )
    source_room = float("inf") if wallet_room is None else max(0.0, float(wallet_room or 0.0))
    portfolio_room = (
        float("inf") if total_margin_room is None
        else max(0.0, float(total_margin_room or 0.0))
    )
    desired_total = min(
        max(0.0, float(target_ratio or 0.0)) * first,
        first,
        followed + max(0.0, float(coin_room or 0.0)),
        followed + max(0.0, float(risk_available or 0.0)),
        followed + group_room,
        followed + source_room,
        followed + portfolio_room,
    )
    return max(0.0, desired_total - followed)


def copy_market_sector(coin: str | None) -> str:
    """Execution-level market board used by concentration controls."""
    return "stock" if str(coin or "").lower().startswith("xyz:") else "crypto"


def effective_position_margin(position: dict) -> float:
    """Remaining isolated margin after any partial closes."""
    margin = max(0.0, f(position.get("margin")))
    size = abs(f(position.get("size")))
    remaining_raw = position.get("rem_size")
    remaining = abs(f(size if remaining_raw is None else remaining_raw))
    return margin * (remaining / size if size > 0.0 else 1.0)


def wallet_sector_side_margin(
    positions, *, addr: str, coin: str, side: str,
) -> float:
    """Aggregate one source wallet's effective margin on one board and direction."""
    wanted_addr = str(addr or "").lower()
    wanted_sector = copy_market_sector(coin)
    wanted_side = str(side or "").lower()
    return sum(
        effective_position_margin(position)
        for position in positions
        if str(position.get("addr") or "").lower() == wanted_addr
        and copy_market_sector(position.get("coin")) == wanted_sector
        and str(position.get("side") or "").lower() == wanted_side
    )


def wallet_margin(positions, *, addr: str) -> float:
    """Aggregate effective margin copied from one source wallet across its whole basket."""
    wanted_addr = str(addr or "").lower()
    return sum(
        effective_position_margin(position)
        for position in positions
        if str(position.get("addr") or "").lower() == wanted_addr
    )


def total_effective_margin(positions) -> float:
    return sum(effective_position_margin(position) for position in positions)


def wallet_sector_side_margin_room(
    *, cap_pct: float, risk_equity: float, existing_margin: float,
) -> float:
    cap = max(0.0, min(1.0, f(cap_pct)))
    return max(0.0, cap * max(0.0, f(risk_equity)) - max(0.0, f(existing_margin)))


def margin_cap_room(*, cap_pct: float, risk_equity: float, existing_margin: float) -> float:
    """Remaining effective-margin room for any equity-relative concentration cap."""
    return wallet_sector_side_margin_room(
        cap_pct=cap_pct, risk_equity=risk_equity, existing_margin=existing_margin,
    )


def tier_for_sigma(sigma: float, stable_sigma_max: float, high_sigma_min: float,
                   coin: str | None = None) -> str:
    # Product policy: BTC always uses the stable tier.  Its real sigma is still collected for smart-add
    # spacing and audit, but it never migrates to mid/high sizing.  Every non-BTC market starts at mid and
    # can only move upward to high; low-vol altcoins/stocks never inherit BTC-sized risk.
    if str(coin or "").upper() == "BTC":
        return "stable"
    return "high" if sigma >= high_sigma_min else "mid"


def isolated_liq_px(entry_px: float, side: str, size: float, margin: float,
                    maintenance_leverage: float | None, leverage: float) -> float:
    """Estimate Hyperliquid isolated liquidation including first-tier maintenance margin."""
    if entry_px <= 0 or size <= 0 or margin <= 0:
        return 0.0
    maint_lev = float(maintenance_leverage or 0.0)
    mmr = .5 / max(1.0, maint_lev) if maint_lev > 0 else 0.0
    margin_per_unit = margin / size
    if side == "long":
        return max(0.0, (entry_px - margin_per_unit) / max(1e-9, 1.0 - mmr))
    return max(0.0, (entry_px + margin_per_unit) / (1.0 + mmr))


def reduce_leaves_dust(rem_size: float, reduce_frac: float, px: float,
                       dust_notional: float = config.DUST_CLOSE_NOTIONAL) -> bool:
    if not dust_notional or dust_notional <= 0 or reduce_frac >= 1.0:
        return False
    remaining_size = max(0.0, abs(rem_size) * (1.0 - max(0.0, reduce_frac)))
    return remaining_size * abs(px) <= dust_notional


def profit_tail_close_decision(
    *,
    rem_size: float,
    peak_size: float,
    reduce_frac: float,
    execution_px: float,
    risk_px: float | None,
    entry_px: float,
    side: str,
    realized_pnl: float,
    liq_px: float,
    fee_rate: float,
    enabled: bool = config.TAIL_CLOSE_ENABLE,
    hard_remain_pct: float = config.TAIL_CLOSE_HARD_REMAIN_PCT,
    risk_remain_pct: float = config.TAIL_CLOSE_RISK_REMAIN_PCT,
    max_profit_giveback_pct: float = config.TAIL_CLOSE_PROFIT_GIVEBACK_PCT,
) -> ProfitTailDecision:
    """Return a scale-free, asset-aware decision for closing a profitable tail.

    The risk branch uses the position's isolated liquidation price, which already embeds that market's
    Hyperliquid maintenance requirement. This remains profit protection rather than a hidden stop-loss:
    an episode that would be net losing if flattened now is left to the normal mirror/stop policy.
    """
    zero = ProfitTailDecision(False, "", 1.0, 0.0, 0.0, 0.0)
    if not enabled or rem_size <= 0 or peak_size <= 0 or execution_px <= 0 or entry_px <= 0:
        return zero
    reduce_frac = max(0.0, min(1.0, float(reduce_frac)))
    if reduce_frac >= 1.0:
        return zero
    remaining_size = abs(rem_size) * (1.0 - reduce_frac)
    remaining_fraction = remaining_size / max(abs(peak_size), abs(rem_size), 1e-12)
    hard = max(0.0, min(1.0, float(hard_remain_pct)))
    risk_limit = max(hard, min(1.0, float(risk_remain_pct)))
    if remaining_fraction > risk_limit:
        return ProfitTailDecision(False, "", remaining_fraction, 0.0, 0.0, 0.0)

    sign = 1.0 if side == "long" else -1.0
    close_now_profit = (
        float(realized_pnl or 0.0)
        + abs(rem_size) * (execution_px - entry_px) * sign
        - abs(rem_size) * execution_px * max(0.0, float(fee_rate or 0.0))
    )
    if close_now_profit <= 0:
        return ProfitTailDecision(False, "", remaining_fraction, close_now_profit, 0.0, 0.0)
    if remaining_fraction <= hard:
        return ProfitTailDecision(True, "hard_profit_tail", remaining_fraction, close_now_profit, 0.0, 0.0)

    mark = float(risk_px or execution_px)
    liq = float(liq_px or 0.0)
    adverse_distance = (max(0.0, mark - liq) if side == "long"
                        else max(0.0, liq - mark))
    loss_to_liquidation = remaining_size * adverse_distance
    giveback_fraction = loss_to_liquidation / close_now_profit if close_now_profit > 0 else 0.0
    close = giveback_fraction >= max(0.0, float(max_profit_giveback_pct))
    return ProfitTailDecision(
        close,
        "liq_risk_profit_tail" if close else "",
        remaining_fraction,
        close_now_profit,
        loss_to_liquidation,
        giveback_fraction,
    )


def smart_take_profit_decision(
    *,
    enabled: bool,
    rem_size: float,
    base_size: float,
    entry_px: float,
    mark_px: float,
    side: str,
    sigma: float,
    tier: str,
    armed: bool,
    stage: int,
    peak_pnl: float,
    arm_sigma: dict,
    giveback_pcts: tuple[float, ...],
    close_pcts: tuple[float, ...],
    tail_remain_pct: float,
    fee_rate: float,
    min_fee_multiple: float,
) -> SmartTakeProfitDecision:
    """Advance one position's volatility-armed high-water take-profit state.

    Arming never sells.  Once armed, each stage watches floating PnL on the *remaining* position,
    cuts a fixed share of the arming-size after the configured giveback, and leaves the caller to
    rebase ``peak_pnl`` after execution.  This helper is pure so Observer and canonical replay cannot
    drift apart.
    """
    rem = max(0.0, abs(float(rem_size or 0.0)))
    entry = float(entry_px or 0.0)
    mark = float(mark_px or 0.0)
    stage_i = max(0, int(stage or 0))
    base = max(0.0, abs(float(base_size or 0.0)))
    peak = max(0.0, float(peak_pnl or 0.0))
    zero = SmartTakeProfitDecision(
        bool(armed), False, stage_i, peak, base, 0.0, 0.0, 0.0, 0.0, rem, 0.0, "",
    )
    if not enabled or rem <= 0 or entry <= 0 or mark <= 0:
        return zero
    sign = 1.0 if side == "long" else -1.0
    favorable_move = (mark - entry) * sign / entry
    current_pnl = rem * (mark - entry) * sign
    if not armed:
        arm_k = max(0.0, float((arm_sigma or {}).get(tier, 0.0) or 0.0))
        if favorable_move + 1e-12 < arm_k * max(0.0, float(sigma or 0.0)):
            return SmartTakeProfitDecision(
                False, False, stage_i, peak, base, current_pnl, favorable_move,
                0.0, 0.0, rem, 0.0, "",
            )
        armed = True
        base = rem
        peak = max(0.0, current_pnl)
    else:
        base = base or rem
        peak = max(peak, current_pnl)

    if stage_i >= min(len(giveback_pcts), len(close_pcts)) or peak <= 0:
        return SmartTakeProfitDecision(
            True, False, stage_i, peak, base, current_pnl, favorable_move,
            0.0, 0.0, rem, 0.0, "armed",
        )
    giveback = max(0.0, (peak - current_pnl) / peak)
    tail_size = base * max(0.0, min(1.0, float(tail_remain_pct or 0.0)))
    close_size = min(
        base * max(0.0, float(close_pcts[stage_i] or 0.0)),
        max(0.0, rem - tail_size),
    )
    exit_fee = close_size * mark * max(0.0, float(fee_rate or 0.0))
    trigger = (
        close_size > 1e-12
        and giveback + 1e-12 >= max(0.0, float(giveback_pcts[stage_i] or 0.0))
        and current_pnl > 0.0
        and current_pnl + 1e-12 >= max(0.0, float(min_fee_multiple or 0.0)) * exit_fee
    )
    return SmartTakeProfitDecision(
        True,
        trigger,
        stage_i,
        peak,
        base,
        current_pnl,
        favorable_move,
        giveback,
        close_size,
        max(0.0, rem - close_size),
        exit_fee,
        f"giveback_stage_{stage_i + 1}" if trigger else "armed",
    )


def extract_master_leverage(fill: dict | None) -> float | None:
    if not isinstance(fill, dict):
        return None
    for key in ("masterLeverage", "master_leverage", "targetLeverage", "target_leverage"):
        lev = f(fill.get(key))
        if lev > 0:
            return lev
    lev_obj = fill.get("leverage")
    if isinstance(lev_obj, dict):
        lev = f(lev_obj.get("value"))
    else:
        lev = f(lev_obj)
    return lev if lev > 0 else None


def plan_open_sizing(
    *,
    coin: str,
    side: str,
    entry_px: float,
    sigma: float,
    balance: float,
    available: float,
    existing_coin_margin: float,
    master_notional: float,
    master_leverage: float | None,
    params: OpenSizingParams,
    maintenance_leverage: float | None = None,
    wallet_sector_side_room: float | None = None,
    wallet_room: float | None = None,
) -> OpenSizingPlan:
    tier = tier_for_sigma(sigma, params.stable_sigma_max, params.high_sigma_min, coin)
    lev = max(params.min_lev, float(int(params.tier_lev_cap[tier])))
    if coin.startswith("xyz:"):
        lev = max(params.min_lev, min(lev, params.stock_max_lev))
    # `maintenance_leverage` comes from the venue's per-market maxLeverage metadata. It determines both
    # the first maintenance tier and the maximum leverage that can actually be opened. Simulating above
    # it creates impossible notionals and false liquidations (for example ETH/XRP under a 35x stable cap).
    if maintenance_leverage and maintenance_leverage > 0:
        lev = max(params.min_lev, min(lev, float(maintenance_leverage)))
    if master_leverage and master_leverage > 0:
        lev = max(params.min_lev, float(int(min(lev, master_leverage))))

    risk_equity = max(0.0, balance)
    risk_available = max(0.0, min(available, risk_equity))
    sizing_equity = sizing_equity_for_drawdown(
        risk_equity,
        params.capital_anchor,
        exponent=params.drawdown_exponent,
        max_multiplier=params.drawdown_max_multiplier,
    )
    margin_equity_pct = max(0.0, min(1.0, float(params.margin_equity_pct)))
    margin_equity = sizing_equity * margin_equity_pct
    locked = max(0.0, risk_equity - risk_available)
    margin_pct = margin_pct_for_deploy(
        params.tier_margin[tier],
        params.tier_margin_min[tier],
        params.deploy_full_pct,
        params.max_deploy_pct,
        locked,
        risk_equity,
    )
    wanted_margin = max(0.0, margin_equity * margin_pct)
    room = max(0.0, params.tier_coin_cap[tier] * risk_equity - existing_coin_margin)
    deploy_room = max(0.0, risk_available - (1.0 - params.max_deploy_pct) * risk_equity)
    min_add_margin = params.min_open_margin_pct * margin_equity
    add_capacity_margin = smart_add_margin_ceiling(
        coin_room=room,
        min_add_margin=min_add_margin,
    )
    group_room = (
        float("inf") if wallet_sector_side_room is None
        else max(0.0, float(wallet_sector_side_room or 0.0))
    )
    source_room = float("inf") if wallet_room is None else max(0.0, float(wallet_room or 0.0))
    margin = min(wanted_margin, room, deploy_room, add_capacity_margin, group_room, source_room)
    group_limited = group_room <= min(
        wanted_margin, room, deploy_room, add_capacity_margin, source_room,
    ) + 1e-12
    wallet_limited = source_room <= min(
        wanted_margin, room, deploy_room, add_capacity_margin, group_room,
    ) + 1e-12
    # The relative dust threshold follows the same manual sizing base.  Otherwise lowering the sizing
    # budget would silently turn valid proportional opens into "margin_too_small" skips.  Fixed per-tier
    # minimum notionals remain real execution/economic floors and are intentionally not scaled.
    if margin < min_add_margin:
        reason = (
            "wallet_sector_side_full" if group_limited else
            "wallet_full" if wallet_limited else
            "coin_full" if min(room, add_capacity_margin) < wanted_margin else
            "no_cash" if risk_available < wanted_margin else
            "deploy_cap" if deploy_room < wanted_margin else
            "margin_too_small"
        )
        return OpenSizingPlan(False, reason, tier, side, margin_pct, margin, 0.0, lev, 0.0, 0.0,
                              room, deploy_room, risk_available, wanted_margin, master_notional,
                              risk_equity, sizing_equity, margin_equity)

    notional = margin * lev
    if master_notional > 0 and notional > master_notional:
        notional = master_notional
        margin = notional / lev if lev else margin
    if notional < params.tier_min_notional[tier]:
        reason = "wallet_sector_side_full" if group_limited else "wallet_full" if wallet_limited else "small_notl"
        return OpenSizingPlan(False, reason, tier, side, margin_pct, margin, notional, lev, 0.0, 0.0,
                              room, deploy_room, risk_available, wanted_margin, master_notional,
                              risk_equity, sizing_equity, margin_equity)

    size = notional / entry_px if entry_px else 0.0
    liq = isolated_liq_px(entry_px, side, size, margin, maintenance_leverage, lev)
    return OpenSizingPlan(True, "", tier, side, margin_pct, margin, notional, lev, size, liq,
                          room, deploy_room, risk_available, wanted_margin, master_notional,
                          risk_equity, sizing_equity, margin_equity)
