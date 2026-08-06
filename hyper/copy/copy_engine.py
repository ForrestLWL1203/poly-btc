"""Pure copy-trade decision helpers shared by live observer and backtests."""

from __future__ import annotations

from dataclasses import dataclass
import math

from hyper import config
from .sizing import margin_pct_for_deploy, sizing_equity_for_drawdown
from hyper.util import f


@dataclass(frozen=True)
class OpenSizingParams:
    high_sigma_min: float
    tier_margin: dict
    tier_lev_cap: dict
    tier_coin_cap: dict
    min_lev: float
    min_open_margin_pct: float
    # Required explicitly so a real-money caller can never inherit the standardized Paper $10k anchor.
    capital_anchor: float
    drawdown_exponent: float = config.SIZING_DRAWDOWN_EXPONENT
    drawdown_max_multiplier: float = config.SIZING_DRAWDOWN_MAX_MULTIPLIER
    margin_equity_pct: float = config.MARGIN_EQUITY_PCT
    # Offline research-only sizing surface. Production callers leave this unset. Keeping the experiment
    # behind an explicit immutable input makes it impossible for a lab replay to change ordinary Paper/Live
    # sizing merely because the helper exists.
    volatility_notional_sizing: dict | None = None


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


def quantize_margin_pct(value: float, step: float = 0.005) -> float:
    """Round to the nearest margin grid point, resolving exact ties downward."""
    step = max(1e-12, float(step or 0.005))
    scaled = max(0.0, float(value or 0.0)) / step
    lower = math.floor(scaled)
    fraction = scaled - lower
    units = lower + (1 if fraction > 0.5 + 1e-12 else 0)
    return units * step


def volatility_target_margin_pct(
    *,
    coin: str,
    sigma: float,
    leverage: float,
    tier: str,
    tier_coin_cap: dict,
    margin_equity_pct: float,
    settings: dict,
) -> float:
    """Derive first-open margin from a BTC notional anchor for offline research.

    The target notional shrinks inversely with volatility. Leverage is selected separately and clipped by
    venue metadata before this function runs; margin is then the residual needed to reach that notional.
    The final value is placed on the 0.5 percentage-point grid and capped so one open plus the configured two
    full add units fit below the tier's per-coin cap.
    """
    btc_margin = max(0.0, float(settings.get("btc_margin_pct", 0.05) or 0.0))
    if str(coin or "").upper() == "BTC":
        return btc_margin

    lev = max(1e-12, float(leverage or 0.0))
    btc_leverage = max(1e-12, float(settings.get("btc_leverage", 30.0) or 30.0))
    btc_sigma = max(1e-12, float(settings.get("btc_sigma") or 0.0))
    coin_sigma = max(btc_sigma, float(sigma or btc_sigma))
    risk_scale = max(0.0, float(settings.get("risk_scale", 1.0) or 0.0))
    tail_start = max(btc_sigma, float(settings.get("tail_sigma", 0.20) or 0.20))
    tail_exponent = max(0.0, float(settings.get("tail_exponent", 0.50) or 0.50))
    tail_penalty = 1.0
    if coin_sigma > tail_start:
        tail_penalty = (tail_start / coin_sigma) ** tail_exponent
    target_notional_pct = (
        btc_margin * btc_leverage
        * min(1.0, btc_sigma / coin_sigma)
        * risk_scale
        * tail_penalty
    )
    grid_step = max(1e-12, float(settings.get("margin_grid_step", 0.005) or 0.005))
    margin_pct = quantize_margin_pct(target_notional_pct / lev, grid_step)
    min_margin_pct = max(0.0, float(settings.get("min_margin_pct", grid_step) or 0.0))
    reserved_adds = max(1, int(settings.get("reserved_adds", config.SMART_ADD_MIN_CAPACITY) or 1))
    equity_pct = max(1e-12, min(1.0, float(margin_equity_pct or 0.0)))
    raw_capacity = max(0.0, float(tier_coin_cap.get(tier) or 0.0)) / (
        (reserved_adds + 1) * equity_pct
    )
    # The published surface must stay on-grid. Floor the capacity itself so rounding cannot exceed it.
    capacity_grid = math.floor((raw_capacity + 1e-12) / grid_step) * grid_step
    return min(capacity_grid, max(min_margin_pct, margin_pct))


def smart_add_margin_ceiling(
    *,
    coin_room: float,
    min_add_margin: float,
    reserved_adds: int = config.SMART_ADD_MIN_CAPACITY,
) -> float:
    """Largest first margin that still leaves ``reserved_adds`` full-size add slots.

    Production currently reserves two adds, so the same-coin/direction cap must
    hold three equal first-margin units: initial open plus two later adds. If
    the resulting unit is below the minimum executable margin, the caller's
    normal dust gate rejects the open.
    """
    adds = max(1, int(reserved_adds or 1))
    return max(0.0, max(0.0, coin_room) / (adds + 1))


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


def wallet_sector_side_margin_room(
    *, cap_pct: float, risk_equity: float, existing_margin: float,
) -> float:
    cap = max(0.0, min(1.0, f(cap_pct)))
    return max(0.0, cap * max(0.0, f(risk_equity)) - max(0.0, f(existing_margin)))


def wallet_sector_side_cap_pct(
    coin: str | None,
    tier: str | None,
    *,
    crypto_stable: float = config.WALLET_CRYPTO_STABLE_SIDE_CAP_PCT,
    crypto_mid: float = config.WALLET_CRYPTO_MID_SIDE_CAP_PCT,
    crypto_high: float = config.WALLET_CRYPTO_HIGH_SIDE_CAP_PCT,
    stock: float = config.WALLET_STOCK_SIDE_CAP_PCT,
) -> float:
    """Return the shared source-wallet board/direction cap for one market."""
    if copy_market_sector(coin) == "stock":
        return max(0.0, min(1.0, f(stock)))
    by_tier = {"stable": crypto_stable, "mid": crypto_mid, "high": crypto_high}
    return max(0.0, min(1.0, f(by_tier.get(str(tier or "mid").lower(), crypto_mid))))


def wallet_sector_side_effective_cap_pct(
    positions,
    *,
    addr: str,
    coin: str,
    side: str,
    candidate_tier: str,
    tier_for_coin=None,
    crypto_stable: float = config.WALLET_CRYPTO_STABLE_SIDE_CAP_PCT,
    crypto_mid: float = config.WALLET_CRYPTO_MID_SIDE_CAP_PCT,
    crypto_high: float = config.WALLET_CRYPTO_HIGH_SIDE_CAP_PCT,
    stock: float = config.WALLET_STOCK_SIDE_CAP_PCT,
) -> float:
    """Use the most conservative tier present in a same-wallet/board/direction basket.

    This removes order dependence: opening a high-volatility coin first and a stable coin second may not
    expand the combined basket from 10% to 20%.
    """
    wanted_addr = str(addr or "").lower()
    wanted_sector = copy_market_sector(coin)
    wanted_side = str(side or "").lower()
    caps = [wallet_sector_side_cap_pct(
        coin, candidate_tier, crypto_stable=crypto_stable, crypto_mid=crypto_mid,
        crypto_high=crypto_high, stock=stock,
    )]
    for position in positions or ():
        position_coin = position.get("coin")
        if (
            str(position.get("addr") or "").lower() != wanted_addr
            or copy_market_sector(position_coin) != wanted_sector
            or str(position.get("side") or "").lower() != wanted_side
        ):
            continue
        position_tier = position.get("risk_tier")
        if not position_tier and tier_for_coin is not None:
            position_tier = tier_for_coin(position_coin)
        caps.append(wallet_sector_side_cap_pct(
            position_coin, position_tier or candidate_tier,
            crypto_stable=crypto_stable, crypto_mid=crypto_mid,
            crypto_high=crypto_high, stock=stock,
        ))
    return min(caps)


def wallet_sector_side_position_count(positions, *, addr: str, coin: str, side: str) -> int:
    wanted_addr = str(addr or "").lower()
    wanted_sector = copy_market_sector(coin)
    wanted_side = str(side or "").lower()
    return sum(
        1 for position in positions
        if str(position.get("addr") or "").lower() == wanted_addr
        and copy_market_sector(position.get("coin")) == wanted_sector
        and str(position.get("side") or "").lower() == wanted_side
        and abs(f(position.get("rem_size", position.get("size")))) > 0.0
    )


def margin_cap_room(*, cap_pct: float, risk_equity: float, existing_margin: float) -> float:
    """Remaining effective-margin room for any equity-relative concentration cap."""
    return wallet_sector_side_margin_room(
        cap_pct=cap_pct, risk_equity=risk_equity, existing_margin=existing_margin,
    )


def tier_for_sigma(sigma: float, high_sigma_min: float, coin: str | None = None) -> str:
    # Product policy: BTC always uses the stable tier.  Its real sigma is still collected for smart-add
    # spacing and audit, but it never migrates to mid/high sizing.  Every non-BTC market starts at mid and
    # can only move upward to high; low-vol altcoins/stocks never inherit BTC-sized risk.
    if str(coin or "").upper() == "BTC":
        return "stable"
    return "high" if sigma >= high_sigma_min else "mid"


def isolated_liq_px(entry_px: float, side: str, size: float, margin: float,
                    maintenance_leverage: float | None) -> float:
    """Estimate Hyperliquid isolated liquidation including first-tier maintenance margin."""
    if entry_px <= 0 or size <= 0 or margin <= 0:
        return 0.0
    maint_lev = float(maintenance_leverage or 0.0)
    mmr = .5 / max(1.0, maint_lev) if maint_lev > 0 else 0.0
    margin_per_unit = margin / size
    if side == "long":
        return max(0.0, (entry_px - margin_per_unit) / max(1e-9, 1.0 - mmr))
    return max(0.0, (entry_px + margin_per_unit) / (1.0 + mmr))


def rebase_isolated_position(
    entry_px: float,
    side: str,
    rem_size: float,
    leverage: float,
    maintenance_leverage: float | None,
) -> dict:
    """Return the current isolated basis after one or more partial reductions.

    ``size``/``margin`` historically accumulated every followed add while
    ``rem_size`` alone shrank on reductions.  A later add then mixed the current
    weighted entry with that historical basis, making liquidation drift toward
    the pre-reduction position.  Peak exposure already has its own field, so an
    open position's sizing basis should always describe only the remaining
    exposure.
    """
    entry = max(0.0, float(entry_px or 0.0))
    size = max(0.0, abs(float(rem_size or 0.0)))
    lev = max(0.0, float(leverage or 0.0))
    notional = entry * size
    margin = notional / lev if lev > 0.0 else 0.0
    return {
        "size": size,
        "margin": margin,
        "notional": notional,
        "liq_px": isolated_liq_px(
            entry, side, size, margin, maintenance_leverage,
        ),
    }


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
    tier = tier_for_sigma(sigma, params.high_sigma_min, coin)
    lev = max(params.min_lev, float(int(params.tier_lev_cap[tier])))
    # `maintenance_leverage` comes from the venue's per-market maxLeverage metadata. It determines both
    # the first maintenance tier and the maximum leverage that can actually be opened. Simulating above
    # it creates impossible notionals and false liquidations (for example ETH/XRP under a 35x stable cap).
    if maintenance_leverage and maintenance_leverage > 0:
        lev = max(params.min_lev, min(lev, float(maintenance_leverage)))
    # Copy risk is defined by our versioned tier surface, not by the source-wallet leverage or an older
    # position opened under a previous strategy revision.  LiveExecutor applies this exact value before every
    # exposure increase; Hyperliquid may update the coin's aggregate isolated leverage as part of that action.

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
    # One operator-owned percentage controls both order size and the aggregate fresh-entry budget. It is
    # not a frozen sub-wallet: once new entries stop, existing-position adds may still use real available cash.
    margin_pct = margin_pct_for_deploy(params.tier_margin[tier])
    if params.volatility_notional_sizing:
        margin_pct = volatility_target_margin_pct(
            coin=coin,
            sigma=sigma,
            leverage=lev,
            tier=tier,
            tier_coin_cap=params.tier_coin_cap,
            margin_equity_pct=margin_equity_pct,
            settings=params.volatility_notional_sizing,
        )
    wanted_margin = max(0.0, margin_equity * margin_pct)
    room = max(0.0, params.tier_coin_cap[tier] * risk_equity - existing_coin_margin)
    deploy_room = max(0.0, risk_available - (1.0 - margin_equity_pct) * risk_equity)
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
    # budget would silently turn valid proportional opens into "margin_too_small" skips. Our order scales with
    # our equity down to the venue minimum, so a small funded Live account remains proportional to Paper.
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

    # The source position only supplies direction and timing; its notional neither gates nor sizes our account.
    # Our notional is owned entirely by our equity, margin, leverage and capacity surface.
    notional = margin * lev
    if notional < config.HYPERLIQUID_MIN_PERP_NOTIONAL_USD:
        reason = "wallet_sector_side_full" if group_limited else "wallet_full" if wallet_limited else "small_notl"
        return OpenSizingPlan(False, reason, tier, side, margin_pct, margin, notional, lev, 0.0, 0.0,
                              room, deploy_room, risk_available, wanted_margin, master_notional,
                              risk_equity, sizing_equity, margin_equity)

    size = notional / entry_px if entry_px else 0.0
    liq = isolated_liq_px(entry_px, side, size, margin, maintenance_leverage)
    return OpenSizingPlan(True, "", tier, side, margin_pct, margin, notional, lev, size, liq,
                          room, deploy_room, risk_available, wanted_margin, master_notional,
                          risk_equity, sizing_equity, margin_equity)
