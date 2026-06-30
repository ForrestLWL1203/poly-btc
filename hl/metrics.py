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


def loss_pain(pnls: list) -> float:
    """REALIZED loss-vs-win asymmetry = |worst realized loss| / median realized win. >1 means the worst
    loss dwarfs a typical win (小赚大亏 — the bag-hold-then-割肉 signature). A wallet that NEVER realizes a
    loss over a large sample is the extreme deferrer → assigned PAIN_NOLOSS. Returns 0 when there's not
    enough evidence. Used (gated by loss-RATE in score) to catch wallets that don't cut losses."""
    wins = sorted(p for p in pnls if p > 0)
    med_win = wins[len(wins) // 2] if wins else 0.0
    worst = min((p for p in pnls if p < 0), default=0.0)
    if med_win > 0 and worst < 0:
        return abs(worst) / med_win
    if worst == 0 and len(pnls) >= config.PAIN_MIN_TRADES:      # many trades, never realized a loss
        return config.PAIN_NOLOSS
    return 0.0


def window_nets(eps_full: list, now_ms: int) -> dict:
    """Realized net PnL over rolling windows from FULL-history closed episodes — multi-window stability
    cross-check (7/14/30/lifetime). Cheap (in-memory) once the full fill history is fetched. `net_life`
    is the long-term truth that the 14d scoring window can't see (catches a blow-up older than 14d)."""
    def net(days):
        cut = now_ms - days * DAY_MS
        return sum(e["net_pnl"] for e in eps_full if e.get("close_ms", 0) >= cut)
    return {"net_7d": net(7), "net_14d": net(14), "net_30d": net(30),
            "net_life": sum(e["net_pnl"] for e in eps_full), "life_trades": len(eps_full)}


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
        "loss_pain": loss_pain([e["net_pnl"] for e in eps]),   # |worst loss| / median win (小赚大亏 signal)
    }
    m.update(_daily(eps, lookback_days))
    return m


def gates_structural(m: dict, p) -> tuple:
    """COPYABILITY structure — checks that need only the CLOSED-trade record, no live-position/API data.
    Run BEFORE fetching the open-position snapshot (cheap reject of MM/HFT/grid). A genuine trend trader
    passes all of these. Episode-based checks are skipped when there are no closed trades (n_trades==0,
    e.g. a pure-hold trend trader) — judged on open positions in gates_state instead."""
    if m["perp_frac"] < p.min_perp:
        return False, "spot_dominant"                          # not copyable enough
    if (m.get("n_trades") or 0) > 0:                           # structure from closed round-trips
        if m["median_eps"] > p.max_daily_eps:
            return False, "bot_frequency"                      # mid-freq OK; HFT/MM excluded
        # HFT: sub-minute-hold scalpers are PROFITABLE but UNcopyable at our ~seconds REST latency.
        if getattr(p, "exclude_hft", True) and m.get("median_hold_s") is not None \
                and m["median_hold_s"] < getattr(p, "hft_min_hold_min", 3.0) * 60:
            return False, "hft_uncopyable"
        if (m.get("max_adds_per_ep") or 0) > p.grid_max_adds:  # grid/DCA: one round-trip stuffed with
            return False, "grid_dca"                           # dozens of laddered scale-ins — uncopyable
    return True, "ok"


def gates_state(m: dict, now_ms: int, p) -> tuple:
    """STATE eligibility — uses LIVE positions (open_*) + realized+unrealized performance. This is where
    a held-position trader is treated as ACTIVE (not 'inactive'), profit is judged on realized+unrealized
    (so a 扛单 carrying deep bags reads as not-profitable while a trend trader's winning holds count),
    and a low-frequency wallet is kept when it holds a real WINNING position."""
    has_open = (m.get("bag_count") or 0) > 0 or (m.get("open_win_frac") or 0.0) > 1e-9
    trend = (m.get("open_win_frac") or 0.0) >= config.TREND_OPEN_MIN     # a real winning hold = trend value
    if (now_ms - m["last_fill_ms"]) / DAY_MS > p.inactive_days and not has_open:
        return False, "inactive"                               # no recent fills AND no live position
    if (m.get("roi_total") if m.get("roi_total") is not None else m.get("net_pnl", 0)) <= 0:
        return False, "not_profitable"                         # realized + UNREALIZED net loss (catches 扛单)
    if m["activity_ratio"] < p.min_activity and not trend:     # low-freq noise — but a genuine trend
        return False, "irregular"                              # holder (winning open) is exempt
    if (m.get("worst_loss_pct") or 0) < -p.max_single_loss:    # one CLOSED round-trip lost > this % equity
        return False, "blowup_loss"                            # = 扛到爆并已实现; the cut-losses gate
    # ── DISCIPLINE GATES (2026-06-30): hard floors on the behaviours we used to only SOFT-demote in score
    # (promotes 赌徒-rejection from "ranked low" to "never in the watchlist"). Each 0 = disabled.
    lp_max = getattr(p, "gate_loss_pain_max", config.GATE_LOSS_PAIN_MAX)
    if lp_max and (m.get("loss_pain") or 0) >= lp_max:
        return False, "asym_loss"                              # 小赚大亏: worst loss dwarfs median win
    sk_max = getattr(p, "gate_hold_skew_max", config.GATE_HOLD_SKEW_MAX)
    if sk_max and (m.get("hold_skew") or 0) >= sk_max:
        return False, "holds_losers"                           # 抗单: holds losers far longer than winners
    pc_max = getattr(p, "gate_profit_conc_max", config.GATE_PROFIT_CONC_MAX)
    if pc_max and (m.get("profit_conc") or 0) >= pc_max:
        return False, "one_window"                             # 一把行情: one day = most of the profit
    # LIFETIME / 30d realized net (the full-history datum). Skipped when absent (old profiles) so `regate`
    # is safe BEFORE a re-profile populates net_life — the gate activates once the scan refetches.
    if config.GATE_REQUIRE_LIFETIME_NET and m.get("net_life") is not None and m["net_life"] <= 0:
        return False, "lifetime_loss"                          # 长期净亏 (#47: clean 14d, -123k over 287d)
    if config.GATE_REQUIRE_30D_NET and m.get("net_30d") is not None and m["net_30d"] <= 0:
        return False, "cooling_off"                            # 近30天净亏 — recent edge has decayed
    if (m.get("hedge_ratio") or 0.0) > config.HEDGE_MAX_FRAC:  # perp shorts offset by spot longs of the
        return False, "spot_hedge"                             # same coin = market-neutral hedge, NOT a
    #                                                            directional trade — copying the naked perp
    #                                                            leg loses what their spot leg offsets.
    return True, "ok"


def score(m: dict) -> float:
    """v4 continuous quality. SCORE = Quality × Survival × Discipline.
      Quality    = (evidence-shrunk, capped) risk-adjusted return on REALIZED+UNREALIZED roi
                   × frequency-scaled day-consistency
      Discipline = does the wallet CUT losses? penalizes currently-carried losing bags (depth×count×
                   duration) + historical forced liquidations. (Replaces the old win-rate proxy.)
    Shape constants live in config (interpretable, UI-tunable — not arbitrary cutoffs)."""
    # risk denominator = realized drawdown PLUS unrealized-win-at-risk. Unrealized gains (open_win_frac)
    # are return NOT yet locked — they can reverse — so a wallet riding a huge open winner (e.g. +215% of
    # account on 0 closed trades) is UNPROVEN, not elite. Counting that as risk keeps genuine trend
    # traders included (their roi_total still counts) but ranks them BEHIND wallets that actually REALIZED
    # the same return, and stops a single unrealized pump from topping the board.
    dd_eq = m["max_drawdown"] / (m["acct_value"] + 1.0) + config.UNREAL_RISK_W * (m.get("open_win_frac") or 0.0)
    # EVIDENCE-aware risk-adjusted return on REALIZED+UNREALIZED roi (roi_total): a trend trader's
    # winning HOLDS count toward return, a 扛单's losing holds drag it down. n_eff counts live positions
    # as evidence too, so a low-frequency holder isn't shrunk to nothing for having few CLOSED trades.
    roi = m.get("roi_total")
    if roi is None:
        roi = m.get("roi_equity", 0.0)
    n_eff = (m.get("n_trades") or 0) + (m.get("bag_count") or 0) \
        + (1 if (m.get("open_win_frac") or 0.0) > 1e-9 else 0)
    roi_eff = max(0.0, roi) * n_eff / (n_eff + config.SCORE_SHRINK_K)
    rar = min(config.SCORE_RAR_CAP, roi_eff / (dd_eq + 0.05))  # risk-adjusted return (strength)
    D = m["active_days"]
    w = D / (D + config.SCORE_K)                               # confidence in the daily series
    pos = max(m["pos_day_ratio"], 1e-6)
    consistency = pos ** (w * config.SCORE_GAMMA)             # high-freq must be green MOST days; low-freq lenient
    quality = rar * consistency

    # survival = a SMALL cross-scan persistence bonus (brand-new=0.9, proven-across-10-scans=1.0).
    survival = 0.9 + 0.1 * min(m.get("times_active", 1), 10) / 10

    # LOSS-DISCIPLINE penalty — measures NOT cutting losses DIRECTLY, never via win rate (a clean
    # fast-cutter with a 95% win rate and no open loss is untouched). Two evidences:
    #   • bag = how bad the CURRENTLY carried LOSING positions are — total depth (open_loss_frac) amplified
    #     by how MANY (bag_count) and how LONG (max_bag_days). One shallow brief dip ≈ 0; several deep bags
    #     held for days = unambiguous 扛单 (depth×breadth×duration, not single-snapshot noise).
    #   • liq = historical FORCED liquidations (the system closed them = demonstrably didn't stop loss).
    # WINNING long holds are NOT here (open_loss_frac counts only negative unrealized) — a trend trader
    # holding deep green is rewarded via roi_total above, not punished here.
    bag_depth = abs(min(0.0, m.get("open_loss_frac") or 0.0))
    bag = bag_depth * (1.0 + 0.5 * max(0, (m.get("bag_count") or 0) - 1)) \
        * (1.0 + min((m.get("max_bag_days") or 0.0) / 3.0, 1.0))
    # REALIZED-asymmetry term (小赚大亏 / 不及时止损 — the twins, #17, RESOLV). Measured by the TAIL
    # directly: how much |worst realized loss| exceeds TAIL_FREE× the median win. v5: NO win-rate gate
    # (the old `defer` zeroed this for win<85%, letting a 60%-win 4×-tail churner pass) — loss_pain bites
    # at ANY win rate. A clean fast-cutter with small symmetric losses has loss_pain≤TAIL_FREE → asym=0.
    asym = max(0.0, (m.get("loss_pain") or 0.0) - config.TAIL_FREE)
    # HOLD-SKEW term (扛单 by duration): holds losers longer than winners. Only EXTREME skew penalized —
    # moderate skew on SMALL losses is benign (and the dangerous combo is already caught by loss_pain).
    skew = max(0.0, (m.get("hold_skew") or 0.0) - config.HOLD_SKEW_FREE)
    disc = (5.0 * bag + 1.0 * (m.get("liq_count") or 0)
            + config.ASYM_W * asym + config.HOLD_SKEW_W * skew)
    discipline = 1.0 / (1.0 + config.DISP_PENALTY_K * disc)

    return quality * survival * discipline
