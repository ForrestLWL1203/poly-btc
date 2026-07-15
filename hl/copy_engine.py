"""Pure copy-trade decision helpers shared by live observer and backtests."""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .sizing import margin_pct_for_deploy, sizing_equity_for_drawdown
from .util import f


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


def tier_for_sigma(sigma: float, stable_sigma_max: float, high_sigma_min: float,
                   coin: str | None = None) -> str:
    # BTC is the only market eligible for the stable tier. Calm ETH, altcoins and stock/builder perps must
    # still start at mid risk; otherwise a low recent range silently grants them BTC-sized leverage.
    if str(coin or "").upper() == "BTC" and sigma <= stable_sigma_max:
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
    margin = min(wanted_margin, room, deploy_room)
    # The relative dust threshold follows the same manual sizing base.  Otherwise lowering the sizing
    # budget would silently turn valid proportional opens into "margin_too_small" skips.  Fixed per-tier
    # minimum notionals remain real execution/economic floors and are intentionally not scaled.
    if margin < params.min_open_margin_pct * margin_equity:
        reason = (
            "coin_full" if room < wanted_margin else
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
        return OpenSizingPlan(False, "small_notl", tier, side, margin_pct, margin, notional, lev, 0.0, 0.0,
                              room, deploy_room, risk_available, wanted_margin, master_notional,
                              risk_equity, sizing_equity, margin_equity)

    size = notional / entry_px if entry_px else 0.0
    liq = isolated_liq_px(entry_px, side, size, margin, maintenance_leverage, lev)
    return OpenSizingPlan(True, "", tier, side, margin_pct, margin, notional, lev, size, liq,
                          room, deploy_room, risk_available, wanted_margin, master_notional,
                          risk_equity, sizing_equity, margin_equity)
