"""Per-wallet metrics, eligibility gates, and the v3 quality score.

v3 philosophy: GATES are minimal binary ELIGIBILITY (can we follow this wallet at all?); QUALITY is
a single continuous SCORE; the watchlist is the top-N by score — no scattered hardcoded quality
thresholds. The score is built on the DAILY PnL series (consistency), not just window totals, so it
separates a steady grinder from a one-lucky-day wallet and from a chronic loss-holder (扛单/浮亏).
"""
import statistics

from . import config
from .util import f

DAY_MS = 86400_000


def max_drawdown(curve: list) -> float:
    peak, mdd = -1e30, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _daily(eps: list, lookback_days: float) -> dict:
    """Bucket episodes by calendar day → daily pnl/count series + derived consistency metrics."""
    by_day: dict = {}
    for e in eps:
        rec = by_day.setdefault(e["close_ms"] // DAY_MS, {"pnl": 0.0, "n": 0})
        rec["pnl"] += e["net_pnl"]
        rec["n"] += 1
    pnls = [d["pnl"] for d in by_day.values()]
    counts = [d["n"] for d in by_day.values()]
    D = len(pnls)
    greens = [p for p in pnls if p > 0]
    return {
        "active_days": D,
        "activity_ratio": (D / lookback_days) if lookback_days else 0.0,
        "median_eps": statistics.median(counts) if counts else 0.0,
        "max_eps": max(counts) if counts else 0,
        "pos_day_ratio": (sum(1 for p in pnls if p > 0) / D) if D else 0.0,   # fraction of GREEN days
        "profit_conc": (max(greens) / sum(greens)) if greens else 0.0,        # best day's share of gross profit
    }


def _hold_skew(eps: list) -> float:
    """median hold of LOSING episodes / median hold of WINNING episodes. >1 ⇒ holds losers longer
    than winners (disposition effect / 扛单 — the chronic-unrealized-loss behaviour)."""
    losers = [e["hold_s"] for e in eps if e["net_pnl"] < 0]
    winners = [e["hold_s"] for e in eps if e["net_pnl"] > 0]
    if not winners:
        return 3.0                             # only losers ever held -> worst (display-only metric)
    if not losers:
        return 0.0                             # never holds losers -> ideal
    return statistics.median(losers) / max(statistics.median(winners), 1.0)


def compute_metrics(fills: list, eps: list, now_ms: int, lookback_days: float):
    """Aggregate perp fills + reconstructed episodes into one metrics dict (or None). All metrics
    here are account-value-independent; roi_equity/dd are added by the caller (it has acct_value)."""
    if not fills or not eps:
        return None
    taker_notl = sum(f(x["px"]) * f(x["sz"]) for x in fills if x.get("crossed"))
    tot_notl = sum(f(x["px"]) * f(x["sz"]) for x in fills)
    window_days = max((fills[-1]["time"] - fills[0]["time"]) / DAY_MS, 1e-9)
    holds = sorted(e["hold_s"] for e in eps)
    coins: dict = {}
    for e in eps:
        coins[e["coin"]] = coins.get(e["coin"], 0) + 1
    cum, curve = 0.0, []
    for e in sorted(eps, key=lambda e: e["close_ms"]):
        cum += e["net_pnl"]
        curve.append(cum)
    total_notl = sum(e["max_notl"] for e in eps)
    # market type by traded-NOTIONAL split: crypto perp (plain name) vs transparent builder (xyz:* stock/
    # commodity). crypto_frac=1 pure crypto, 0 pure stock. Lets the watchlist/UI tag a wallet's battlefield.
    crypto_notl = sum(e["max_notl"] for e in eps if ":" not in e["coin"])
    crypto_frac = (crypto_notl / total_notl) if total_notl else 1.0
    m = {
        "crypto_frac": crypto_frac,
        "market_type": ("crypto" if crypto_frac >= 0.7 else "stock" if crypto_frac <= 0.3 else "mixed"),
        "n_fills": len(fills), "n_trades": len(eps), "window_days": window_days,
        "trades_per_day": len(eps) / window_days,
        "taker_frac_notl": (taker_notl / tot_notl) if tot_notl else 0.0,
        "median_hold_s": holds[len(holds) // 2],
        "win_rate": sum(1 for e in eps if e["net_pnl"] > 0) / len(eps),
        "net_pnl": cum, "gross_pnl": sum(e["net_pnl"] + e["fee"] for e in eps),
        "roi_notional": (cum / total_notl) if total_notl else 0.0, "total_notl": total_notl,
        "total_fee": sum(e["fee"] for e in eps), "n_coins": len(coins),
        "top_coin": max(coins.items(), key=lambda kv: kv[1])[0],
        "long_frac": sum(1 for e in eps if e["side"] == "long") / len(eps),
        "max_drawdown": max_drawdown(curve), "avg_notional": total_notl / len(eps),
        "last_fill_ms": fills[-1]["time"], "hold_skew": _hold_skew(eps),
        # GRID/DCA signature: distinct scale-in ORDERS per round-trip. A directional swing trader adds
        # 0–few times; a grid/ladder trader stuffs one episode with dozens (e.g. 73 on SKHX). median_eps
        # (round-trips/day) can't see this — it all rolls into one episode. max = worst single episode.
        "max_adds_per_ep": max((e.get("n_adds", 0) for e in eps), default=0),
        "median_adds_per_ep": sorted(e.get("n_adds", 0) for e in eps)[len(eps) // 2],
        # LOSS DISCIPLINE: the single worst losing round-trip ($, <=0). Caller divides by acct_value
        # -> worst_loss_pct. Small = cuts losses promptly (followable even at 50% win); large = holds
        # one loser to disaster (扛单到爆) — the thing to gate, distinct from cumulative max_drawdown.
        "worst_loss": min((e["net_pnl"] for e in eps if e["net_pnl"] < 0), default=0.0),
        # TAKE-PROFIT SIGNATURE: median favorable price move on WINNING round-trips (|close-open|/open).
        # This is the target's own thesis horizon — a tight-scalp wallet ~1.5-2%, a trend wallet much
        # wider. The copy-side stop sets our cut at a MULTIPLE of this in the adverse direction.
        "tp_move_pct": statistics.median([abs(e["close_px"] - e["open_px"]) / e["open_px"]
                                          for e in eps if e["net_pnl"] > 0 and e.get("open_px")] or [0.0]),
    }
    m.update(_daily(eps, lookback_days))
    return m


def gates(m: dict, now_ms: int, p) -> tuple:
    """ELIGIBILITY — can we follow this wallet at all? Minimal binary checks; everything about HOW
    GOOD it is lives in score(). `p` carries the (few, interpretable) gate thresholds."""
    if m["perp_frac"] < p.min_perp:
        return False, "spot_dominant"                          # not copyable enough
    if (now_ms - m["last_fill_ms"]) / DAY_MS > p.inactive_days:
        return False, "inactive"                               # stopped trading / rotated away
    if m["net_pnl"] <= 0:
        return False, "not_profitable"                         # net realized loss over the window
    if m["median_eps"] > p.max_daily_eps:
        return False, "bot_frequency"                          # mid-freq OK; HFT/MM excluded
    # HFT switch: sub-minute-hold scalpers (e.g. 2s round-trips) are PROFITABLE but UNcopyable at our
    # ~seconds REST latency (the trade is closed before we detect it). Excluded while we lack sub-second
    # execution; flip EXCLUDE_HFT off once a high-freq feed (HyperRPC WS) makes them copyable.
    if getattr(p, "exclude_hft", True) and m.get("median_hold_s") is not None \
            and m["median_hold_s"] < getattr(p, "hft_min_hold_min", 3.0) * 60:
        return False, "hft_uncopyable"
    if m["activity_ratio"] < p.min_activity:                   # MINIMAL floor (~3 active days) — only
        return False, "irregular"                              # rejects one-shot noise; genuine low-freq
    #                                                            traders pass and are ranked down by the
    #                                                            evidence-shrink in score(), not killed here
    if (m.get("max_adds_per_ep") or 0) > p.grid_max_adds:      # grid/DCA: one round-trip stuffed with
        return False, "grid_dca"                               # dozens of laddered scale-ins — our
    #                                                            capped-add model can't replicate it (we
    #                                                            get only the worst few entries) -> exclude
    if (m.get("worst_loss_pct") or 0) < -p.max_single_loss:    # one round-trip lost > this % of equity
        return False, "blowup_loss"                            # = 扛单到爆 / no stop-discipline; not the
    #                                                            cut-losses-small wallet we want to copy
    return True, "ok"


def score(m: dict) -> float:
    """v3 continuous quality. SCORE = Quality × Survival × Health.
      Quality = (evidence-shrunk, capped) risk-adjusted return × frequency-scaled day-consistency
      Health  = current-snapshot open-underwater depth
    Shape constants live in config (interpretable, UI-tunable — not arbitrary cutoffs)."""
    dd_eq = m["max_drawdown"] / (m["acct_value"] + 1.0)
    # EVIDENCE-aware risk-adjusted return: (1) shrink roi toward 0 by sample size — roi×n/(n+K) — so a
    # lucky few-trade +100% doesn't read as edge; (2) CAP the ratio so a tiny low-sample drawdown can't
    # produce an unbounded score. This buries low-evidence wallets below the follow line (still observed,
    # promoted as round-trips accumulate) WITHOUT a hard activity gate that would kill low-freq traders.
    n = m.get("n_trades") or 0
    roi_eff = max(0.0, m["roi_equity"]) * n / (n + config.SCORE_SHRINK_K)
    rar = min(config.SCORE_RAR_CAP, roi_eff / (dd_eq + 0.05))  # risk-adjusted return (strength)
    D = m["active_days"]
    w = D / (D + config.SCORE_K)                               # confidence in the daily series
    pos = max(m["pos_day_ratio"], 1e-6)
    consistency = pos ** (w * config.SCORE_GAMMA)             # high-freq must be green MOST days; low-freq lenient
    quality = rar * consistency

    # survival = a SMALL cross-scan persistence bonus only. times_active (how many of OUR scans this
    # wallet has stayed eligible) is a mild DURABILITY REWARD, not a penalty: brand-new-to-us = 0.9,
    # proven-across-10-scans = 1.0 — so a strong recent performer isn't crushed for being newly found.
    # NO self-liquidation penalty: a blow-up's damage is ALREADY in net_pnl / roi_equity (RAR) /
    # max_drawdown / hold_skew (double-counting it here was redundant), it's rare (74% of actives have
    # zero), and on isolated per-trade copy a target's account blow-up doesn't transfer to us anyway.
    # liq_count/liq_worst_pct stay in the profile as a human-readable flag, out of the score.
    survival = 0.9 + 0.1 * min(m.get("times_active", 1), 10) / 10

    # NO FreqFit factor: frequency is purely a GATE concern (inactive at the low end, >30 round-trips/
    # day = bot at the high end). Inside that allowed band we want low-freq swing-holders and mid-freq
    # scalpers EQUALLY — discounting low-freq here would fight our own "copy good traders of any
    # cadence" thesis. Quality = returns × consistency × risk, NOT how often they trade.

    # Health = current loss-DEPTH only. Two factors were removed because their lens was wrong:
    #  • hold_skew (loser-hold / winner-hold TIME): 扛单 risk is about how DEEP a loss you sit on, not
    #    how long — cutting winners fast is GOOD discipline, and holding a few-% dip until it recovers
    #    is fine. Depth is already captured by RAR (roi/max_drawdown, realized) + open_underwater below.
    #  • profit_conc (one day's share of gross profit): a big day on top of otherwise-green days is a
    #    GREAT wallet; the bad pattern (one big day, bleeding the rest) is just a LOW pos_day_ratio,
    #    already crushed by `consistency`. profit_conc punished both alike — it mis-fired.
    # Both stay in the profile as display-only metrics, out of the score.
    uw = abs(min(0.0, m.get("open_underwater") or 0.0))        # current worst open underwater (fraction)
    health = 1.0 - _clip((uw - config.UW_TOL) / max(config.UW_REF - config.UW_TOL, 1e-6), 0.0, 1.0)

    # 扛单 SOFT demote — see config.DISP_*. A realized win rate above DISP_WR_FREE is usually
    # MANUFACTURED by deferring losses (the bag-hold signature closed-trade metrics can't see, since
    # the loss never becomes a closed round-trip); hold_skew above DISP_SKEW_FREE catches the "holds
    # losers far longer than winners" variant. We penalize the EXCESS of each, amplified by current
    # open-underwater depth (a wallet sitting on no bag right now gets the benefit of the doubt). Soft:
    # demotes toward/below the follow line but never zeroes a profitable wallet. 0 disables.
    wr_excess = max(0.0, (m.get("win_rate") or 0.0) - config.DISP_WR_FREE)
    skew_excess = max(0.0, (m.get("hold_skew") or 0.0) - config.DISP_SKEW_FREE)
    disp = (10.0 * wr_excess + skew_excess) * (1.0 + 4.0 * uw)
    disp_penalty = 1.0 / (1.0 + config.DISP_PENALTY_K * disp)

    return quality * survival * health * disp_penalty
