"""Pure copy-trade decision helpers shared by live observer and backtests."""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .sizing import margin_pct_for_deploy
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
    copy_stop_enable: bool
    stop_margin_pct: float


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
    stop_px: float
    room: float
    deploy_room: float
    available: float
    wanted_margin: float
    master_notional: float


def tier_for_sigma(sigma: float, stable_sigma_max: float, high_sigma_min: float) -> str:
    if sigma <= stable_sigma_max:
        return "stable"
    return "high" if sigma >= high_sigma_min else "mid"


def stop_px(entry_px: float, is_buy: bool, leverage: float, copy_stop_enable: bool, stop_margin_pct: float) -> float:
    if not copy_stop_enable or not entry_px or not leverage or not stop_margin_pct:
        return 0.0
    d = stop_margin_pct / leverage
    return entry_px * (1 - d) if is_buy else entry_px * (1 + d)


def reduce_leaves_dust(rem_size: float, reduce_frac: float, px: float,
                       dust_notional: float = config.DUST_CLOSE_NOTIONAL) -> bool:
    if not dust_notional or dust_notional <= 0 or reduce_frac >= 1.0:
        return False
    remaining_size = max(0.0, abs(rem_size) * (1.0 - max(0.0, reduce_frac)))
    return remaining_size * abs(px) <= dust_notional


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
) -> OpenSizingPlan:
    tier = tier_for_sigma(sigma, params.stable_sigma_max, params.high_sigma_min)
    lev = max(params.min_lev, float(int(params.tier_lev_cap[tier])))
    if coin.startswith("xyz:"):
        lev = max(params.min_lev, min(lev, params.stock_max_lev))
    if master_leverage and master_leverage > 0:
        lev = max(params.min_lev, float(int(min(lev, master_leverage))))

    locked = max(0.0, balance - available)
    margin_pct = margin_pct_for_deploy(
        params.tier_margin[tier],
        params.tier_margin_min[tier],
        params.deploy_full_pct,
        params.max_deploy_pct,
        locked,
        balance,
    )
    wanted_margin = max(0.0, balance * margin_pct)
    room = max(0.0, params.tier_coin_cap[tier] * balance - existing_coin_margin)
    deploy_room = max(0.0, available - (1.0 - params.max_deploy_pct) * balance)
    margin = min(wanted_margin, room, deploy_room)
    if margin < params.min_open_margin_pct * balance:
        reason = (
            "coin_full" if room < wanted_margin else
            "no_cash" if available < wanted_margin else
            "deploy_cap" if deploy_room < wanted_margin else
            "margin_too_small"
        )
        return OpenSizingPlan(False, reason, tier, side, margin_pct, margin, 0.0, lev, 0.0, 0.0, 0.0,
                              room, deploy_room, available, wanted_margin, master_notional)

    notional = margin * lev
    if master_notional > 0 and notional > master_notional:
        notional = master_notional
        margin = notional / lev if lev else margin
    if notional < params.tier_min_notional[tier]:
        return OpenSizingPlan(False, "small_notl", tier, side, margin_pct, margin, notional, lev, 0.0, 0.0, 0.0,
                              room, deploy_room, available, wanted_margin, master_notional)

    size = notional / entry_px if entry_px else 0.0
    is_buy = side == "long"
    liq = entry_px * (1 - 1.0 / lev) if is_buy else entry_px * (1 + 1.0 / lev)
    stop = stop_px(entry_px, is_buy, lev, params.copy_stop_enable, params.stop_margin_pct)
    return OpenSizingPlan(True, "", tier, side, margin_pct, margin, notional, lev, size, liq, stop,
                          room, deploy_room, available, wanted_margin, master_notional)


def default_open_sizing_params() -> OpenSizingParams:
    return OpenSizingParams(
        stable_sigma_max=config.STABLE_SIGMA_MAX,
        high_sigma_min=config.HIGH_SIGMA_MIN,
        tier_margin={
            "stable": config.STABLE_MARGIN_PCT,
            "mid": config.MID_MARGIN_PCT,
            "high": config.HIGH_MARGIN_PCT,
        },
        tier_margin_min={
            "stable": config.STABLE_MARGIN_MIN_PCT,
            "mid": config.MID_MARGIN_MIN_PCT,
            "high": config.HIGH_MARGIN_MIN_PCT,
        },
        tier_lev_cap={
            "stable": config.STABLE_LEV_CAP,
            "mid": config.MID_LEV_CAP,
            "high": config.HIGH_LEV_CAP,
        },
        tier_min_notional={
            "stable": config.STABLE_MIN_NOTIONAL,
            "mid": config.MID_MIN_NOTIONAL,
            "high": config.HIGH_MIN_NOTIONAL,
        },
        tier_coin_cap={
            "stable": config.STABLE_COIN_CAP_PCT,
            "mid": config.MID_COIN_CAP_PCT,
            "high": config.HIGH_COIN_CAP_PCT,
        },
        min_lev=config.MIN_LEV,
        stock_max_lev=config.STOCK_MAX_LEV,
        deploy_full_pct=config.DEPLOY_FULL_PCT,
        max_deploy_pct=config.MAX_DEPLOY_PCT,
        min_open_margin_pct=config.MIN_OPEN_MARGIN_PCT,
        copy_stop_enable=config.COPY_STOP_ENABLE,
        stop_margin_pct=config.STOP_MARGIN_PCT,
    )
