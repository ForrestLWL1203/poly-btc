"""Per-wallet metrics, eligibility gates, and the v3 quality score.

v3 philosophy: GATES are minimal binary ELIGIBILITY (can we follow this wallet at all?); QUALITY is
a single continuous SCORE; the watchlist is the top-N by score — no scattered hardcoded quality
thresholds. The score is built on the DAILY PnL series (consistency), not just window totals, so it
separates a steady grinder from a one-lucky-day wallet and from a chronic loss-holder (扛单/浮亏).
"""
import math
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
        if (m.get("median_adds_per_ep") or 0) > p.grid_max_adds:  # grid/DCA: TYPICALLY laddered scale-ins.
            return False, "grid_dca"                              # MEDIAN not MAX — one heavy DCA in the window
        #                                                           ≠ a grid bot (would kill +$65k wallets whose
        #                                                           median adds is 0). A real grid dominates most eps.
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
    # NOTE (v5 2026-06-30): the loss_pain/hold_skew/profit_conc/blow-up HARD gates were REMOVED — they
    # double-counted and FOUGHT the composite score (a score-63 wallet vetoed while score-20 ones stayed).
    # Those behaviours are now SMOOTH factors inside score(): 反噬→g_frag, 深度抗单/爆仓→g_deep. The only
    # remaining hard floors are COPYABILITY (above) + NET>0 (below) — unambiguous, score-independent.
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
    """v5 SMOOTH BLENDED QUALITY ∈ [0,1] (display = ×100). The follow-quality is an ADDITIVE weighted
    blend of the user's roots — 胜率 / 风险调整ROI / 逐日稳定性 — then a gentle 活跃度(样本) multiplier and
    two SMOOTH risk guards (反噬/twins, 深度抗单/爆仓). No hard vetoes here (those that remain in gates are
    pure copyability + net>0); the temp loss_pain/hold_skew/profit_conc gates are folded in as guards.
    Smooth by construction → no 90→20 cliff; a single flaw discounts, never zeroes."""
    g = lambda k, d=0.0: (m.get(k) if m.get(k) is not None else d)

    # ── core positives, each ∈ [0,1] ──
    win = _clip(g("win_rate"), 0.0, 1.0)                                   # 胜率(根本)
    roi = g("roi_total", g("roi_equity"))                                  # realized + unrealized return
    dd_eq = g("max_drawdown") / (g("acct_value") + 1.0) + config.UNREAL_RISK_W * g("open_win_frac")
    roi_adj = max(0.0, roi) / (1.0 + config.SCORE_DD_AVERSION * dd_eq)     # 回撤惩罚后的有效收益
    roi_s = 1.0 - math.exp(-roi_adj / config.SCORE_ROI_SCALE)             # 平滑饱和 [0,1)
    stab = _clip(g("pos_day_ratio"), 0.0, 1.0)                           # 逐日为正比例(稳定性)
    wsum = config.SCORE_W_WIN + config.SCORE_W_ROI + config.SCORE_W_STAB or 1.0   # relative weights (UI-safe)
    core = (config.SCORE_W_WIN * win + config.SCORE_W_ROI * roi_s + config.SCORE_W_STAB * stab) / wsum

    # ── evidence/活跃度: gentle confidence multiplier (sample size → trust), floored so低频好钱包不被碾压 ──
    n_eff = g("n_trades") + g("bag_count")
    samp = 0.6 * min(1.0, n_eff / config.SCORE_EV_TRADES) + 0.4 * min(1.0, g("active_days") / config.SCORE_EV_DAYS)
    ev = config.SCORE_EV_FLOOR + (1.0 - config.SCORE_EV_FLOOR) * samp

    # ── 反噬/双胞胎守卫: |最惨单笔| ÷ 净利润 (= |worst_loss_pct|/roi_equity). 高胜率也救不了"一笔亏吞掉所有利润". ──
    roi_eq = g("roi_equity")
    wl = abs(g("worst_loss_pct"))                                         # worst single round-trip loss / acct (≥0)
    frag = (wl / roi_eq) if roi_eq > 1e-6 else 0.0                        # net≤0 已被 gates 挡;此处记 0
    g_frag = _clip(1.0 - max(0.0, frag - config.SCORE_FRAG_FREE) / config.SCORE_FRAG_SPAN,
                   config.SCORE_GUARD_FLOOR, 1.0)

    # ── 深度抗单/爆仓守卫: 按深度,不按持仓时间. 用 open_underwater(单仓最惨浮亏 = 真实扛单深度),
    #    不用 open_loss_frac(总浮亏÷账户,会被大账户稀释 → 深扛单单仓看着像没事=“无限保证金熬过来”的假象). ──
    bag = max(abs(min(0.0, g("open_underwater"))), abs(min(0.0, g("open_loss_frac"))))   # 单仓深度优先, 账户总额兜底
    deep = max(bag / config.SCORE_BAG_REF, wl / config.SCORE_BLOW_REF)
    g_deep = _clip(1.0 - max(0.0, deep - 1.0) * config.SCORE_DEEP_SLOPE, config.SCORE_GUARD_FLOOR, 1.0)

    # ── 刷胜率守卫: 高胜率 + 几乎从不兑现亏损 = 靠扛单藏亏刷假胜率(双胞胎本质). 真会止损的高胜率钱包(wl≥LOSS_REF)不罚. ──
    manuf = (_clip((win - config.SCORE_MANUF_WIN_FLOOR) / (1.0 - config.SCORE_MANUF_WIN_FLOOR), 0.0, 1.0)
             * _clip(1.0 - wl / config.SCORE_MANUF_LOSS_REF, 0.0, 1.0))
    g_manuf = _clip(1.0 - manuf * config.SCORE_MANUF_PEN, config.SCORE_GUARD_FLOOR, 1.0)

    survival = 0.9 + 0.1 * min(g("times_active", 1), 10) / 10             # cross-scan persistence (tiny)
    return _clip(core * ev * g_frag * g_deep * g_manuf * survival, 0.0, 1.0)
