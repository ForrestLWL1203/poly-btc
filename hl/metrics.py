"""Per-wallet metric computation, copyability gates, and ranking score.

ROI is measured on EQUITY (leverage-correct): roi_equity = window net_pnl /
account_value. roi_notional (net/notional) is leverage-free and kept only as a
secondary diagnostic. Strength for a small-capital copier = risk-adjusted equity ROI.
"""
from .util import f


def max_drawdown(curve: list) -> float:
    peak, mdd = -1e30, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def compute_metrics(fills: list, eps: list, now_ms: int):
    """Aggregate perp fills + reconstructed episodes into one metrics dict (or None)."""
    if not fills or not eps:
        return None
    n_fills = len(fills)
    taker_notl = sum(f(x["px"]) * f(x["sz"]) for x in fills if x.get("crossed"))
    tot_notl = sum(f(x["px"]) * f(x["sz"]) for x in fills)
    first_ms, last_ms = fills[0]["time"], fills[-1]["time"]
    window_days = max((last_ms - first_ms) / 86400_000.0, 1e-9)
    holds = sorted(e["hold_s"] for e in eps)
    coins: dict = {}
    for e in eps:
        coins[e["coin"]] = coins.get(e["coin"], 0) + 1
    top_coin = max(coins.items(), key=lambda kv: kv[1])[0]
    cum, curve = 0.0, []
    for e in sorted(eps, key=lambda e: e["close_ms"]):
        cum += e["net_pnl"]
        curve.append(cum)
    total_notl = sum(e["max_notl"] for e in eps)
    return {
        "n_fills": n_fills, "n_trades": len(eps), "window_days": window_days,
        "trades_per_day": len(eps) / window_days,
        "taker_frac_notl": (taker_notl / tot_notl) if tot_notl else 0.0,
        "median_hold_s": holds[len(holds) // 2],
        "win_rate": sum(1 for e in eps if e["net_pnl"] > 0) / len(eps),
        "net_pnl": cum, "gross_pnl": sum(e["net_pnl"] + e["fee"] for e in eps),
        "roi_notional": (cum / total_notl) if total_notl else 0.0, "total_notl": total_notl,
        "total_fee": sum(e["fee"] for e in eps),
        "n_coins": len(coins), "top_coin": top_coin,
        "long_frac": sum(1 for e in eps if e["side"] == "long") / len(eps),
        "max_drawdown": max_drawdown(curve),
        "avg_notional": total_notl / len(eps),
        "last_fill_ms": last_ms,
    }


def gates(m: dict, now_ms: int, p) -> tuple:
    """Copyability gates. `p` carries thresholds (argparse namespace or any obj with
    these attrs). Returns (ok, reason)."""
    if m["perp_frac"] < p.min_perp:
        return False, "spot_dominant"
    if (now_ms - m["last_fill_ms"]) / 86400_000.0 > p.inactive_days:
        return False, "inactive"
    if m["n_trades"] < p.min_trades:
        return False, "too_few_trades"          # raised w/ win gate: high win over few trades = luck
    if m["net_pnl"] <= 0:
        return False, "not_profitable"
    if m["win_rate"] < getattr(p, "min_win", 0.0):
        return False, "win_too_low"             # CONSISTENCY — the primary selector
    if m["roi_equity"] < getattr(p, "min_roi_eq", 0.0):
        return False, "roi_too_low"             # realized 14d strength (not leaderboard unrealized)
    dd_eq = m["max_drawdown"] / (m["acct_value"] + 1.0)
    if dd_eq > getattr(p, "max_dd_eq", 1e9):
        return False, "drawdown_too_high"        # variance cap (equity drawdown)
    if m["trades_per_day"] > p.max_tpd:
        return False, "too_frequent"            # TRUE frequency (episodes/day), not volume turnover
    if m["median_hold_s"] < p.min_hold_h * 3600:
        return False, "hold_too_short"
    # NB: no taker/maker gate (maker limit traders are followable via the poller); no volume-turnover
    # gate (it conflates leverage w/ frequency) — real frequency is episodes/day above. MMs never
    # return to flat -> 0 episodes -> caught by too_few_trades.
    return True, "ok"


def score(m: dict) -> float:
    """Core = consistent profitability + survival. We copy small isolated per-trade with our own
    leverage cap + stop, so we do NOT inherit a target's account-level risk — leverage/margin-type
    are OBSERVED (not scored), and a self-liquidation only mildly flags high variance (their
    account blow-up doesn't transfer to us). The judge is consistent profitability over time."""
    roi = m["roi_equity"]
    dd_eq = m["max_drawdown"] / (m["acct_value"] + 1.0)
    rr = roi / (dd_eq + 0.01)                                  # risk-adjusted return (core)
    consistency = 0.4 + 0.6 * m["win_rate"]                    # per-trade consistency
    age = m.get("age_days") or 0
    survival = (0.4 + 0.3 * min(age, 365) / 365                # longevity (900d profitable = proven)
                + 0.3 * min(m.get("times_active", 1), 10) / 10)  # + persistence across our scans
    # taker/maker is a STYLE (how we copy: market vs mirror-limit), not a quality factor — not scored.
    worst = abs(m.get("liq_worst_pct") or 0.0)                 # worst self-liquidation, % of equity
    liq_factor = 0.6 if worst >= 20 else (0.85 if worst >= 5 else 1.0)  # mild: only catastrophic bites
    return rr * consistency * survival * liq_factor
