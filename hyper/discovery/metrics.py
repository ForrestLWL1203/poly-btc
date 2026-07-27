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


def source_episode_quality(eps_full: list, now_ms: int) -> dict:
    """Build the source-wallet 30d/7d quality record from complete fee-paid Episodes.

    The seven warm-up days only reconstruct positions already open at the 30-day boundary. Such boundary
    episodes remain useful for open-state reconstruction but are excluded from source sample and win-rate
    evidence unless ``open_complete`` proves the actual opening fill is present.
    """
    complete = [
        episode for episode in eps_full
        if episode.get("open_complete", True)
        and int(episode.get("close_ms") or 0) > 0
    ]

    def window(days: int) -> tuple[list, dict]:
        cutoff = int(now_ms) - int(days) * DAY_MS
        rows = [episode for episode in complete if int(episode.get("close_ms") or 0) >= cutoff]
        wins = sum(1 for episode in rows if float(episode.get("net_pnl") or 0.0) > 0.0)
        return rows, {
            f"source_episode_n_{days}d": len(rows),
            f"source_win_rate_{days}d": wins / len(rows) if rows else None,
            f"source_net_pnl_{days}d": sum(float(row.get("net_pnl") or 0.0) for row in rows),
            f"source_active_days_{days}d": len({
                int(row.get("close_ms") or 0) // DAY_MS for row in rows
            }),
        }

    rows30, result30 = window(30)
    _rows7, result7 = window(7)
    profitable = sorted(
        (row for row in rows30 if float(row.get("net_pnl") or 0.0) > 0.0),
        key=lambda row: float(row.get("net_pnl") or 0.0),
        reverse=True,
    )
    top3_ids = {id(row) for row in profitable[:3]}
    gross_profit = sum(float(row.get("net_pnl") or 0.0) for row in profitable)
    top3_profit = sum(float(row.get("net_pnl") or 0.0) for row in profitable[:3])
    body = [row for row in rows30 if id(row) not in top3_ids]
    body_wins = sum(1 for row in body if float(row.get("net_pnl") or 0.0) > 0.0)
    return {
        **result30,
        **result7,
        "source_top3_profit_share": (
            top3_profit / gross_profit if gross_profit > 0.0 else None
        ),
        "source_body_after_top3_n": len(body),
        "source_body_after_top3_win_rate": body_wins / len(body) if body else None,
        "source_body_after_top3_net_pnl": sum(
            float(row.get("net_pnl") or 0.0) for row in body
        ),
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
        # Execution structure is order-based. Hyperliquid may split one large maker/taker order into dozens of
        # fills, while Observer and replay deliberately consume that OID once. Raw fill density remains audit
        # telemetry; only distinct source OIDs may trip the systematic algo-execution gate.
        "max_fills_ep": max((e.get("n_fills", 0) for e in eps), default=0),
        "p90_fills_ep": sorted(e.get("n_fills", 0) for e in eps)[min(len(eps) - 1, int(len(eps) * 0.9))] if eps else 0,
        "max_orders_ep": max((e.get("n_oids", 0) for e in eps), default=0),
        "p90_orders_ep": sorted(e.get("n_oids", 0) for e in eps)[min(len(eps) - 1, int(len(eps) * 0.9))] if eps else 0,
        "heavy_orders_episode_n": sum(1 for e in eps if e.get("n_oids", 0) > 50),
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
        # ALGO-EXECUTION: count distinct source orders, never exchange fill fragments. One large order can be
        # matched in dozens of slices because the wallet is large or the book is thin; our OID state machine
        # mirrors it at most once, so fill count is not structural uncopyability.
        orders_limit = getattr(
            p, "max_orders_per_ep", getattr(p, "max_fills_per_ep", 50),
        )
        heavy_orders_n = int(m.get("heavy_orders_episode_n") or 0)
        systematic_n = max(2, int(math.ceil((m.get("n_trades") or 0) * 0.10)))
        if ((m.get("n_trades") or 0) >= 10 and heavy_orders_n >= systematic_n
                and (m.get("p90_orders_ep") or 0) > orders_limit):
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
