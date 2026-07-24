"""Per-wallet metrics, eligibility gates, and the v3 quality score.

v3 philosophy: GATES are minimal binary ELIGIBILITY (can we follow this wallet at all?); QUALITY is
a single continuous SCORE; the watchlist is the top-N by score — no scattered hardcoded quality
thresholds. The score is built on the DAILY PnL series (consistency), not just window totals, so it
separates a steady grinder from a one-lucky-day wallet and from a chronic loss-holder (扛单/浮亏).
"""
import math
import statistics

from hyper import config
from hyper.util import f

DAY_MS = 86400_000


def max_drawdown(curve: list) -> float:
    peak, mdd = -1e30, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _peak_concurrent(eps: list) -> int:
    """Peak number of SIMULTANEOUSLY-open positions (sweep line over each episode's [open, close]).
    A wallet that habitually holds 15-20 positions at once can't be copied on our equity-based sizing +
    deploy cap (we fit ~5-8) — we'd take a random subset, missing the balanced book that nets it positive.
    At-instant ties: process closes (-1) before opens (+1) so a same-ms close→reopen isn't double-counted."""
    evts = sorted([(e["open_ms"], 1) for e in eps if e.get("open_ms") and e.get("close_ms")]
                  + [(e["close_ms"], -1) for e in eps if e.get("open_ms") and e.get("close_ms")],
                  key=lambda x: (x[0], x[1]))
    cur = peak = 0
    for _, d in evts:
        cur += d
        peak = max(peak, cur)
    return peak


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
            "net_life": sum(e["net_pnl"] for e in eps_full)}


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
    # 盈亏比 payoff = 平均赢单 / 平均亏单。低胜率但 payoff 高 = 小亏大赢的真趋势客(可跟);高胜率但 payoff<1 =
    # 大亏小赚假胜率(一笔亏吃掉多笔赢)。从不兑现亏损 → 无亏 → 封顶 999(该情形由 score 的刷胜率守卫另管)。
    _wins = [e["net_pnl"] for e in eps if e["net_pnl"] > 0]
    _losses = [-e["net_pnl"] for e in eps if e["net_pnl"] < 0]
    _avg_win = (sum(_wins) / len(_wins)) if _wins else 0.0
    _avg_loss = (sum(_losses) / len(_losses)) if _losses else 0.0
    _payoff = min(999.0, _avg_win / _avg_loss) if _avg_loss > 0 else 999.0
    # 赢单每笔中位名义收益% = 典型赢单吃到几个点的价格波动(杠杆无关、和手续费同口径)。
    # 保留为审计指标；薄边际风险由 portfolio edge bps 和 copy replay 判断。
    _win_pt_list = sorted(e["net_pnl"] / e["max_notl"] * 100 for e in eps if e["net_pnl"] > 0 and e.get("max_notl"))
    _win_pt = _win_pt_list[len(_win_pt_list) // 2] if _win_pt_list else 0.0
    _max_concurrent = _peak_concurrent(eps)   # 峰值同时持仓数 → "开仓太多我们装不下" 的可复制性闸
    complete_eps = [e for e in eps if e.get("open_complete", True)]
    m = {
        "crypto_frac": crypto_frac,
        "market_type": ("crypto" if crypto_frac >= 0.7 else "stock" if crypto_frac <= 0.3 else "mixed"),
        "n_fills": len(fills), "n_trades": len(eps), "window_days": window_days,
        "trades_per_day": len(eps) / window_days,
        "taker_frac_notl": (taker_notl / tot_notl) if tot_notl else 0.0,
        "median_hold_s": holds[len(holds) // 2],
        "win_rate": sum(1 for e in eps if e["net_pnl"] > 0) / len(eps),
        "payoff_ratio": _payoff,
        "win_pt": _win_pt, "max_concurrent": _max_concurrent,
        "net_pnl": cum, "total_notl": total_notl,
        "top_coin": max(coins.items(), key=lambda kv: kv[1])[0],
        "max_drawdown": max_drawdown(curve), "avg_notional": total_notl / len(eps),
        "last_fill_ms": fills[-1]["time"], "hold_skew": _hold_skew(eps),
        # GRID/DCA signature: distinct scale-in ORDERS per round-trip. A directional swing trader adds
        # 0–few times; a grid/ladder trader stuffs one episode with dozens (e.g. 73 on SKHX). median_eps
        # (round-trips/day) can't see this — it all rolls into one episode. max = worst single episode.
        "max_adds_per_ep": max((e.get("n_adds", 0) for e in complete_eps), default=0),
        "median_adds_per_ep": statistics.median(
            [e.get("n_adds", 0) for e in complete_eps]
        ) if complete_eps else 0,
        "complete_episode_n": len(complete_eps),
        "grid_episode_n": sum(1 for e in complete_eps if e.get("n_adds", 0) > 3),
        # ALGO-SLICER signature: use the p90 of per-episode fills, NOT the average (hidden by light episodes,
        # e.g. avg 34 but a 294-fill round-trip) and NOT the max (a single outlier — a swing trader forced to
        # slice ONE illiquid-stock fill shows max 114 but p90 15; killing it is a false positive). p90 fires
        # only on SYSTEMATIC slicing: ≥10% of round-trips are fill-heavy → a real algo, not a one-off.
        "max_fills_ep": max((e.get("n_fills", 0) for e in eps), default=0),
        "p90_fills_ep": sorted(e.get("n_fills", 0) for e in eps)[min(len(eps) - 1, int(len(eps) * 0.9))] if eps else 0,
        "heavy_fills_episode_n": sum(1 for e in eps if e.get("n_fills", 0) > 50),
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
    # Sample depth and trading frequency are continuous evidence.  They decide
    # Challenger/Core confidence later; they are not structural uncopyability.
    if (m.get("n_trades") or 0) > 0:                           # structure from closed round-trips
        if m["median_eps"] > p.max_daily_eps:
            return False, "bot_frequency"                      # mid-freq OK; HFT/MM excluded
        # HFT: sub-minute-hold scalpers are PROFITABLE but UNcopyable at our ~seconds REST latency.
        if getattr(p, "exclude_hft", True) and m.get("median_hold_s") is not None \
                and m["median_hold_s"] < getattr(p, "hft_min_hold_min", 3.0) * 60:
            return False, "hft_uncopyable"
        complete_n = int(m.get("complete_episode_n") or 0)
        grid_n = int(m.get("grid_episode_n") or 0)
        if complete_n >= 5 and grid_n * 2 > complete_n:  # habitual means a strict majority, not one small sample.
            return False, "grid_dca"
        # Heavy one-off DCA is also uncopyable: median catches habitual grid bots, but a single 20-100+
        # scale-in winner can be exactly where the target's edge lives and where our copy path diverges.
        if (m.get("max_adds_per_ep") or 0) > getattr(p, "max_single_adds", config.MAX_SINGLE_ADDS_PER_EP):
            return False, "heavy_dca"
        # ALGO-EXECUTION / 拆单: works each round-trip via dozens–hundreds of sliced fills we can't mirror
        # (noise + fees). Gate on the p90 per-episode fill count — SYSTEMATIC slicing. NOT the average (a slicer
        # hides when its heavy round-trips are diluted by light ones: avg 34 but one round-trip = 294 fills), and
        # NOT the max (a swing trader forced to slice ONE illiquid-stock fill has max 114 but p90 15 — killing it
        # is a false positive, empirically confirmed). p90 > threshold ⇒ ≥10% of round-trips are fill-heavy = algo.
        fills_limit = getattr(p, "max_fills_per_ep", 50)
        heavy_fills_n = int(m.get("heavy_fills_episode_n") or 0)
        systematic_n = max(2, int(math.ceil((m.get("n_trades") or 0) * 0.10)))
        if ((m.get("n_trades") or 0) >= 10 and heavy_fills_n >= systematic_n
                and (m.get("p90_fills_ep") or 0) > fills_limit):
            return False, "hft_uncopyable"
        # v9 装不下: 峰值同时持仓 > cap. Our equity-均额 sizing + deploy cap fits ~5-8 concurrent; a wallet that
        # habitually holds 15-20 at once (portfolio/basket trader) can only be copied as a RANDOM subset — we
        # miss the cross-position hedging that nets it positive, so our slice bleeds (empirically: 0xc9c781).
        if (m.get("max_concurrent") or 0) > getattr(p, "max_concurrent_pos", config.MAX_CONCURRENT_POS):
            return False, "too_many_concurrent"
    return True, "ok"


def gates_state(m: dict, now_ms: int, p) -> tuple:
    """Hard state gates only.

    Profit sign, recency, activity, sample depth, win rate and thin-but-real
    edge are deliberately absent.  They remain visible in raw/Copy scores and
    lifecycle evidence, preventing several overlapping hard cuts from erasing
    a recoverable Challenger before OOS replay can judge it.
    """
    if (m.get("hedge_ratio") or 0.0) > config.HEDGE_MAX_FRAC:  # perp shorts offset by spot longs of the
        return False, "spot_hedge"                             # same coin = market-neutral hedge, NOT a
    #                                                            directional trade — copying the naked perp
    #                                                            leg loses what their spot leg offsets.
    # Hyperliquid's portfolio endpoint is account-wide and cannot be filtered to our executable dex/market
    # scope.  Account-wide PnL, turnover and drawdown therefore have no place in qualification.  We only need
    # a real equity denominator here; HFT/fees/edge are judged from scoped episodes and canonical Copy replay.
    if not (m.get("acct_value") or m.get("pf_equity")):
        return False, "account_equity_unavailable"
    return True, "ok"


def score(m: dict) -> float:
    """Executable-scope quality in ``[0,1]`` (display = ×100).

    The score is intentionally derived from retained Crypto/stock-contract
    history and strict Copy results.  Leaderboard and account-wide portfolio
    returns are discovery metadata only and can never raise this score.

    ADDITIVE core of the user's THREE roots — 胜率 / 活跃度 / ROI —
    × only the guards ROI can't see (刷胜率 fake-win + a mild current-deep-bag), × a linear STRETCH so the
    best real wallet lands near 100 with a smooth decline (easier follow-line setting). NO 反噬/worst-loss
    guard: 小赚大亏 already surfaces as low/negative ROI (net≤0 → gated; low ROI → low ROI term). We copy
    ISOLATED with our OWN stop, so a single big loss THEY took doesn't transfer — don't double-punish it."""
    g = lambda k, d=0.0: (m.get(k) if m.get(k) is not None else d)

    # ── core positives, each ∈ [0,1] ──
    win = _clip(g("copy_bt_win_rate", g("win_rate")), 0.0, 1.0)
    # Use the exact simulated capital basis and fee-paid endpoint PnL.  The 7d/30d pair rewards recent
    # strength without ever importing spot, outcome or a disabled specialist sector.
    _initial_equity = g(
        "copy_bt_initial_margin_equity",
        g("initial_margin_equity", float(getattr(config, "INITIAL_BALANCE", 10_000.0))),
    )
    _equity30 = g("copy_bt_window_start_equity", _initial_equity)
    _equity7 = g("copy_bt_7d_window_start_equity", _initial_equity)
    if _equity30 and _equity30 > 0 and _equity7 and _equity7 > 0 and m.get("copy_bt_net_pnl") is not None:
        _rp = [
            (config.ROI_W_WEEK,
             g("copy_bt_7d_net_pnl") / _equity7),
            (config.ROI_W_MON,
             g("copy_bt_net_pnl") / _equity30),
        ]
    else:
        # Structural-only defensive path: these values were already built from the scoped fill set.
        acct = g("acct_value")
        _rp = (
            [(config.ROI_W_WEEK, g("net_7d") / acct), (config.ROI_W_MON, g("net_30d") / acct)]
            if acct > 0 else []
        )
    _rw = sum(w for w, v in _rp if v is not None)
    roi = (sum(w * _clip(v, config.ROI_CLIP_LO, config.ROI_CLIP_HI) for w, v in _rp if v is not None) / _rw
           if _rw else 0.0)
    # Historical maximum drawdown is reconstructed at our leverage ceiling because source margin/leverage
    # is unavailable. Keep it as telemetry; it must not lower even the legacy discovery score.
    roi_s = 1.0 - math.exp(-max(0.0, roi) / config.SCORE_ROI_SCALE)        # 平滑饱和 [0,1)
    # Open-position evidence uses all real positions, never only losing bags (which created a reverse
    # incentive where carrying more losses raised the quality score).
    open_n = g("open_position_count")
    actionable_opens = g("actionable_open_events_30d", g("open_events_30d", g("n_trades")))
    open_days = g("open_days_30d", g("active_days"))
    act = (0.5 * min(1.0, (actionable_opens + open_n) / config.SCORE_EV_TRADES)
           + 0.5 * min(1.0, open_days / config.SCORE_EV_DAYS))
    wsum = config.SCORE_W_WIN + config.SCORE_W_ACT + config.SCORE_W_ROI or 1.0   # relative weights (UI-safe)
    core = (config.SCORE_W_WIN * win + config.SCORE_W_ACT * act + config.SCORE_W_ROI * roi_s) / wsum

    # ── guards ROI can't capture (NO 反噬/worst-loss dock — ROI handles 小赚大亏) ──
    wl = abs(g("worst_loss_pct"))     # worst realized single loss / acct — used only by the 刷胜率 guard below
    # 刷胜率: ≥WIN_FLOOR 胜率 且 几乎从不兑现亏损 = 靠扛浮亏藏亏刷假胜率(ROI 看不出,因为亏损没兑现)。
    # 这会让跟单组合承接未入账的尾部风险，故其胜率对我们是误导 → 罚。会及时兑现亏损的高胜率钱包(wl≥LOSS_REF)不受影响。
    manuf = (_clip((win - config.SCORE_MANUF_WIN_FLOOR) / (1.0 - config.SCORE_MANUF_WIN_FLOOR), 0.0, 1.0)
             * _clip(1.0 - wl / config.SCORE_MANUF_LOSS_REF, 0.0, 1.0))
    g_manuf = _clip(1.0 - manuf * config.SCORE_MANUF_PEN, config.SCORE_GUARD_FLOOR, 1.0)
    # 当前深亏轻推(此刻正在扛的单仓最惨浮亏);地板高——isolated + 有界仓位让它是软信号,不是重罚
    bag = abs(min(0.0, g("open_underwater")))
    g_deep = _clip(1.0 - max(0.0, bag - config.SCORE_BAG_REF) / config.SCORE_BAG_SPAN, config.SCORE_DEEP_FLOOR, 1.0)

    # ── v10 QUALITY-MAGNITUDE factors (folded INTO score, NOT末尾 hard gates — those double-count & over-cut) ──
    # `win_pt` remains an audit metric only. Thin-edge risk is handled by fee/slippage-paid strict Copy,
    # official four-week 5% folds plus strict-Copy magnitude/stability and cost stress; a second raw
    # notional-edge penalty would double-count.
    # 盈亏比: 只罚真·大亏小赚(payoff<1); payoff≥REF(1.0) 满分 → 不误伤 payoff 1.0 的高胜率盘. 高地板(0.6)=轻推.
    #   edge 厚度不单列因子(和 ROI 支柱重复);严格Copy和成本压力已判断净收益,ROI支柱负责奖励回报.
    g_payoff = _clip(g("payoff_ratio", 1.0) / config.SCORE_PAYOFF_REF, config.SCORE_PAYOFF_FLOOR, 1.0)

    # linear STRETCH → best real wallet ≈ 100, smooth decline (stable/absolute, not max-relative)
    return _clip(core * g_manuf * g_deep * g_payoff * config.SCORE_STRETCH, 0.0, 1.0)
