"""Shared constants — endpoints, hard limits, sim parameters. No logic here."""

# Hyperliquid endpoints
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
UA = {"User-Agent": "hl-copytrade/0.3", "Accept": "application/json", "Content-Type": "application/json"}

# numeric
FLAT = 1e-6                 # |position| below this (coin units) counts as flat
MIN_POST_INTERVAL = 1.2     # global REST pace (s/POST). HL /info budget = 1200 WEIGHT/min/IP, and
#                             our heavy calls (userFillsByTime, frontendOpenOrders) cost weight 20
#                             each (+1 per 20 results) — so the real ceiling is ~60 weight-20/min,
#                             NOT a request count. 1.2s = 50/min ≈ 1000 weight/min: safely under
#                             1200, leaving headroom for the 8s-trickle scanner (~150 weight/min)
#                             on the same IP. (l2Book/clearinghouseState are only weight 2.)
#                             The scanner overrides this to --scan-interval in its own process.

# HL WS hard limits (per IP, official): the binding one is unique users.
MAX_WS_USERS = 10           # max unique users across user-specific subscriptions (WS only)

# Copy engine: SIGNAL via REST poll (per-wallet userFills — REST has no 10-user cap, so we can
# watch the whole watchlist); PRICING via WS bbo (per-COIN top-of-book — NOT subject to the
# 10-user cap, only the 1000-sub cap, and we touch only a few dozen coins). Targets are low-freq
# long-hold, so a few-seconds poll latency is fine; we execute against the live book at detection.
MIN_FOLLOW_SCORE = 0.52     # follow watchlist wallets with score >= this. v5 (2026-06-30): score is now
#                             native [0,1] (display ×100); 0.55 = display 55 → ~36 followable on the current
#                             smooth distribution (top≈77), comfortably above the 20+ floor. The smooth blend
#                             makes this a real quality cut (not a cliff). UI-tunable (0–100 ruler).
#                             v5 (2026-06-29): 1.2→0.85 — recalibrated for the new harvest box + de-bugged
#                             score; 0.85 yields ~30 CLEAN wallets (0 小赚大亏/扛单, win median 87%)

MAX_TARGETS = 40            # hard cap on followed wallets (bounds REST load even if many clear the score)
FOLLOW_MIN_TRADES = 8       # follow-set evidence floor: a wallet must have ≥ this many closed trades in the
FOLLOW_MIN_ACTIVE_DAYS = 4  # 30d profile AND ≥ this many active days to be COPIED — independent of score.
#                             A 100%-win-on-3-trades wallet scores low (evidence multiplier) but still clears
#                             the line; this floor keeps it OUT of the follow set until it has real history.
#                             It stays on the watchlist (observed) — promoted automatically once it qualifies.
OBSERVER_UNIT = "hl-observe"  # systemd unit the scan-trigger supervisor starts/stops on dashboard command
WATCHLIST_RELOAD_S = 300   # re-read the watchlist table this often (track rolling discovery)
POLL_OVERLAP_MS = 5000     # re-fetch this far behind each wallet's in-memory cursor (tid-dedup absorbs
#                            it) so a fill landing between poll rounds isn't missed. This is the ONLY
#                            look-back — the observer is forward-only, it never catches up on history.
LIVE_FILLS_RETENTION_DAYS = 7  # prune live_fills older than this (tid-dedup only needs the overlap
#                                window; the rest is audit) — keeps the only unbounded table bounded

# Copy account & sizing (UI-tunable). Real-account paper model: a simulated wallet with an initial
# balance. Each copy commits isolated margin out of CURRENT AVAILABLE balance, sized by VOLATILITY
# TARGETING (below) — never a fixed $ amount, always a fraction of available. notional = margin *
# leverage; isolated liquidation (loss = margin). No stop-loss in v1.
INITIAL_BALANCE = 10000.0   # simulated wallet starting equity ($)
ADD_MARGIN_PCT = 0.01       # margin on each follow-on ADD (scale-in) = fraction of available
MAX_ADDS = 2                # follow the master's scale-ins up to this many adds/position (each ADD_MARGIN_PCT)

# v8 SIZING (2026-06-30). Three VOLATILITY TIERS (by daily σ = high-low range, see volatility.py); each
# tier has its own margin% + leverage cap; WITHIN a tier, leverage scales continuously with σ. σ classifies
# AND fine-tunes — no coin lists. Anchored to AVAILABLE (self-throttles as positions fill). Tier by σ:
#   stable  σ ≤ STABLE_SIGMA_MAX        (BTC + anything calmer incl low-σ stocks like GOLD) → big
#   mid     STABLE_SIGMA_MAX < σ < HIGH_SIGMA_MIN  (ETH/SOL/HYPE/majors)
#   high    σ ≥ HIGH_SIGMA_MIN          (ZEC/memes/wild) → small
#   margin   = available × <tier>_MARGIN_PCT
#   leverage = floor(clip( RISK_BUDGET / σ , MIN_LEV , <tier>_LEV_CAP ))   ← v9 (2026-06-30)
#              RISK_BUDGET = the margin loss a 1σ adverse move should cost (so lev·σ ≈ RISK_BUDGET). This
#              REPLACES the old hardcoded `STABLE_LEV_CAP×STABLE_SIGMA_MAX` (= 20×4% = 80%) anchor — same
#              shape, but the knob now MEANS something and ties directly to the σ-stop: a 1×σ stop costs
#              exactly RISK_BUDGET of margin (constant across coins). Absolute-vol targeting (σ rises →
#              lev drops), NOT relative-to-BTC. Tier cap is the hard ceiling (binds only for very-low-σ
#              coins). So RISK_BUDGET=60%: BTC(σ3.9%)→15x, ETH→10x(cap), HYPE→6x, ZEC→4x.
#   notional = margin × leverage. (Capped at the master's notional — moot at our size, kept as safety.)
RISK_BUDGET = 0.60          # v9: margin loss target on a 1σ move; lev = RISK_BUDGET/σ. = single σ-stop loss.
STABLE_SIGMA_MAX = 0.05     # σ ≤ this → STABLE tier. 4%→5% (2026-07-01) so BTC (σ≈4.2%, our benchmark) lands
#                             in STABLE, not MID. STABLE coins now trade at the FULL STABLE_LEV_CAP (not
#                             σ-throttled) — see _sizing_for. (user: "BTC 作为基准就该 20x")
HIGH_SIGMA_MIN   = 0.10     # σ ≥ this → HIGH-VOL tier; between the two → MID tier
STABLE_MARGIN_PCT = 0.10    # per-trade margin = this × available, for STABLE-tier coins
MID_MARGIN_PCT    = 0.08    # ...for MID-tier coins
HIGH_MARGIN_PCT   = 0.06    # ...for HIGH-VOL-tier coins (kept meaningful so memes aren't double-crushed)
STABLE_LEV_CAP = 20.0       # leverage ceiling for STABLE-tier coins
MID_LEV_CAP    = 10.0       # ...for MID-tier coins
HIGH_LEV_CAP   = 5.0        # ...for HIGH-VOL-tier coins
#                             (STOCK_FORCE_HIGH_TIER rolled back 2026-07-01 — stocks tier by their own σ;
#                             their over-leverage risk is handled by the master-leverage cap, not tier-forcing.)
MIN_LEV = 1.0               # leverage floor — ultra-volatile coin → ~spot (isolated 1x ≈ unliquidatable)
COIN_MARGIN_CAP_PCT = 0.20  # per-COIN cap: total margin across all our open positions on ONE coin ≤ this
#                             fraction of the account (stops N wallets piling into the same coin/direction)
MIN_OPEN_MARGIN_PCT = 0.005 # skip a new copy if its formula margin (= MAX_MARGIN_PCT·scale·available) is below this
#                             fraction of equity: once free balance is too low to fund a MEANINGFUL
#                             position, just skip the signal (don't open dust). Existing positions stay
#                             managed/exited. High-conviction signals (bigger rf) still open later than
#                             low-conviction ones, which is intended. UI-tunable.
MAX_LEV = 20.0              # hard leverage cap (BTC + anything calmer pin here); also a stale-σ backstop

# Per-coin volatility (regime-aware) for the sizing above. A coin calm-then-erupting must NOT keep its
# old low σ and get over-levered into a blow-up — so we use TWO horizons and take the MAX (de-risk fast
# when vol rises, re-risk slowly when it falls). Refreshed periodically into the coin_vol TABLE off the
# signal hot path; sizing just reads the row. σ_used = max(σ_fast, σ_slow), both daily realized vol.
VOL_FAST_DAYS = 7           # recent window — catches a fresh volatility regime within ~a day
VOL_SLOW_DAYS = 30          # long baseline — stable; the floor we hold until calm is sustained
VOL_MIN_SAMPLES = 5         # need this many daily candles, else fall back
VOL_REFRESH_S = 3600        # re-fetch each tracked coin's σ at most this often (1h) — vol drifts slowly
VOL_FALLBACK_SIGMA = 0.10   # σ when candles unavailable (new/illiquid coin) → low lev, small notional
VOL_PREWARM_TOP = 30        # at startup, warm σ for the top-N by 24h volume in crypto + EACH builder dex

# PERIODIC orphan reconcile: forward-only polling normally catches a master's close in real time, but a
# missed fill (poll gap / aggregation quirk / blip) would leave us dumb-holding a position the master
# already exited. Re-run the startup reconcile this often so an orphan is closed within minutes.
RECONCILE_INTERVAL_S = 300  # 5 min
#                             (the liquid coins our targets most likely trade) → no first-open latency,
#                             warm restart. The long tail is still lazy-fetched on first fill.

# Copy-strategy knobs (UI-tunable; no hardcoded magic). None = disabled.
# Chase guard: on a fast spike the master eats the book with size and our taker fill lands worse.
# If our entry price is more than this % worse than the master's, SKIP that open (don't chase).
# Applies to taker opens only (maker rests passively; exits are never blocked — always follow out).
MAX_ENTRY_CHASE_PCT = None    # e.g. 0.5 => skip a taker open whose entry is >0.5% worse than master

# Execution model (paper fidelity). We ALWAYS price off the CURRENT book at detection (never the
# master's fill price — that's only a fallback when the book isn't ready). The only question is which
# SIDE: a copy reacts seconds LATE (forward-only REST poll), so we can't retroactively have rested at
# the master's maker price — to actually hold the position the master is in, we cross the spread (taker
# catch-up). Pricing a late maker fill at the passive side silently assumes an instant, never-missed
# rest = optimistic paper PnL. Default OFF = honest taker catch-up for ALL fills. Flip ON only once we
# proactively mirror a target's resting order we saw AHEAD of its fill (target_orders) — then a maker
# fill is legitimately reproducible. Until that exists, leave OFF so paper PnL doesn't flatter live.
EXEC_MAKER_MIRROR = False     # True = price master-maker fills at the passive book side (assumes our rest fills)

# Stage-1 leaderboard prefilter (UI-tunable). The leaderboard carries each wallet's 24h/7d/30d/allTime
# perf in ONE bulk fetch, so we pre-bias on what it CAN reliably say — multi-window profitability +
# return magnitude + 7d activity — BEFORE any per-wallet API profiling. What it CANNOT say (true week-
# to-week stability, copyability, loss-discipline) is the PROFILE stage's job (pos_day_ratio, grid gate,
# worst_loss gate). Key lessons baked in: (1) bots/grids are INVISIBLE here (volume/turnover/efficiency
# don't separate them from directional — proven), so don't try; profile catches them. (2) ACTIVITY uses
# the 7d window, NOT 24h — a 24h floor kills the holders we want (low 24h volume mid-hold) and biases to
# high-churn bots. (3) RETURN uses 30d magnitude + 7d magnitude TOGETHER: 30d alone can be one big early
# day then dormant; requiring the 7d to ALSO be earning blocks that, while the 30d requirement stops a
# single-week fluke. We copy by %/leverage so low-ROI wallets give us low returns (small capital).
# STAGE-1 leaderboard BOX (v5, 2026-06-29). Gate ONLY on what the leaderboard can HONESTLY say —
# real capital + genuine recent VOLUME + internal consistency. ROI/PnL MAGNITUDE is NOT a gate:
# leaderboard ROI is contaminated (deposits/withdrawals/spot/airdrop), empirically the top-ROI wallets
# are $0-volume HODLers/ghosts. The one field that can't be faked by holding is VOLUME. Profit
# JUDGMENT is deferred to the profile (real fills). Thresholds calibrated against 20 followed anchors +
# a clean-strength cohort (see memory hl-copytrade.md): strong wallets sit at $0.5–30M wk vol, pnl/vol
# 0.2–4%; ghosts pnl/vol >>8%; MMs vol >$100M & pnl/vol <0.1%.
HARVEST_MIN_ACCT = 10000.0          # real-capital floor (5k→10k; <10k mostly noise, but our proven
#                                     small-account %-traders sit at ~$11-20k so don't raise further)
HARVEST_WEEK_VLM_MIN = 500_000.0    # 7d VOLUME floor — genuinely trading this week (strong density is
#                                     thin below $1M, but $0.5-1M still holds real talent → floor $0.5M)
HARVEST_WEEK_VLM_MAX = 30_000_000.0 # 7d VOLUME ceiling — above ~$30M = market-maker/HFT-bot (billion-$
#                                     /wk, razor pnl/vol); 90% of strong wallets sit under $15M
HARVEST_PNL_VOL_MIN = 0.001         # 7d pnl/volume FLOOR (0.1%) — below = razor-thin MM, not directional
HARVEST_PNL_VOL_MAX = 0.08          # 7d pnl/volume CEILING (8%) — above = profit too big for the volume
#                                     = NOT from trading (deposit/spot/airdrop ghost); real traders 0.2-4%
# RETIRED (leaderboard ROI contaminated; daily turnover doesn't separate MMs from our high-churn keeps):
HARVEST_MON_ROI_MIN = 0.0           # was 0.15 — return magnitude is now a SCORE input, not a gate
HARVEST_MON_ROI_MAX = 1e9           # was 3.0
HARVEST_WEEK_ROI_MIN = 0.0          # was 0.02
HARVEST_MAX_TURNOVER = 1e9          # was 10.0 — volume ceiling + pnl/vol band handle MMs instead

# v3 score shape (interpretable, UI-tunable — NOT arbitrary quality cutoffs). The watchlist is
# top-N by SCORE = Quality(RAR × day-consistency) × Survival × Health(current-underwater depth).
SCORE_K = 5.0          # daily-stats confidence: w = active_days/(active_days+K). Low-freq → lean overall ROI
SCORE_GAMMA = 2.0      # day-consistency strictness: consistency = pos_day_ratio^(w·GAMMA). Higher = stricter
UW_TOL = 0.02          # ignore current open underwater below this (fresh/small dips fine)
UW_REF = 0.10          # open-underwater treated as fully dangerous (Health snap → 0 here). Decoupled
#                        from MAX_LEV (the copy cap) on purpose — this is a scoring-shape param.
# EVIDENCE handling (paired with the now-soft activity gate). Relaxing `irregular` admits genuine
# low-freq swing/trend traders, but a 3-trade +100% wallet must NOT rank like a proven one. So the
# score discounts thin evidence AT THE SOURCE instead of via a hard gate: shrink roi toward 0 by
# sample size, and cap the risk-adjusted ratio so no wallet rides one lucky low-drawdown streak to an
# unbounded score. Low-evidence wallets then sit BELOW the follow line (observed by the scanner, not
# yet copied) and climb as round-trips accumulate across re-scans — graduation with no tier machinery.
SCORE_SHRINK_K = 10.0  # roi trusted as roi×n/(n+K) for n closed round-trips: a wallet needs ~K trades
#                        for its return to be half-believed (n=10→×0.5, n=3→×0.23, n=100→×0.91)
SCORE_RAR_CAP = 3.0    # ceiling on risk-adjusted return (roi_eff/(dd+0.05)) — tiny observed drawdown at
#                        low sample is not real safety, so one extreme ratio can't dominate the score

# ══ SCORE v5 (2026-06-30) — SMOOTH BLENDED QUALITY (replaces the multiplicative RAR×consistency×discipline
# that produced a 90→20 cliff). User principles: the roots are 胜率 / 风险调整ROI / 逐日稳定性 / 活跃度(样本);
# the temp hard gates (loss_pain/hold_skew/profit_conc) are FOLDED IN as smooth factors, not vetoes:
#   score01 = (W_WIN·win + W_ROI·roiS + W_STAB·stab) × evidence × g_frag × g_deep × survival      ∈ [0,1]
#   display = round(score01 × 100).  Native scale is now [0,1] (was [0,3]); score100 = ×100.
# Smooth because the core is an ADDITIVE weighted blend of [0,1] factors (no capped ratio, no power law),
# and the guards/evidence are gentle multipliers with floors (a single flaw discounts, never zeroes).
SCORE_W_WIN  = 0.40    # 胜率权重(用户:胜率是根本)。win = win_rate ∈ [0,1]
SCORE_W_ROI  = 0.35    # 风险调整收益权重
SCORE_W_STAB = 0.25    # 逐日为正比例(pos_day_ratio)权重 —— W_* 之和 = 1
SCORE_DD_AVERSION = 3.0   # roi_adj = max(0,roi)/(1 + 此×回撤dd_eq):回撤越大有效收益越低
SCORE_ROI_SCALE   = 0.25  # roiS = 1 − exp(−roi_adj/此):平滑饱和(25%调整收益→0.63,50%→0.86),无悬崖
SCORE_EV_TRADES = 20      # 达此回合数=证据充分
SCORE_EV_DAYS   = 10      # 达此活跃天数=证据充分
SCORE_EV_FLOOR  = 0.6     # 证据乘子下限:样本再少也保留 60% core(不碾压低频好钱包)
# 反噬/双胞胎守卫 —— 最惨单笔 ÷ 净利润 = |worst_loss_pct|/roi_equity。抓"n笔小赚+1笔大亏吞掉所有收益"的高胜率欺骗手;
# 用户的良性例(5赢@5%+1亏@7.5% → 7.5/17.5=0.43)在 FREE 内、不罚。
SCORE_FRAG_FREE = 0.5     # 最惨单笔 ≤ 净利润此比例 → 不罚
SCORE_FRAG_SPAN = 1.0     # 超出 FREE 后再涨此幅 → 守卫降到下限(frag≥1.5≈被压到底)
# 深度抗单/爆仓守卫 —— 按【深度】不按持仓时间(用户:小幅逆向抗回盈利很正常、且我们有自己的止损):
# 深度 = 单仓最惨浮亏 open_underwater(真实扛单深度,不用 open_loss_frac:大账户会把总浮亏稀释成"看着没事",
# 即"无限保证金熬过来"的假象)。BAG_REF 6%(用户:≤7% 还能接受),所以 −7% 几乎不痛、−9% 中扣、−29% 砍到底。
SCORE_BAG_REF  = 0.06     # 单仓浮亏达账户此比例 → 深度=1(开始扣)
SCORE_BLOW_REF = 0.15     # 历史最惨单笔亏达账户此比例 → 深度=1
SCORE_DEEP_SLOPE = 0.5    # 深度每超出 1,守卫线性下降斜率
SCORE_GUARD_FLOOR = 0.25  # 守卫乘子下限(最差也保留 25%,靠分数线把它压在线下,而非硬杀)
# 刷胜率守卫(双胞胎本质)—— 高胜率 + 几乎从不兑现亏损 = 靠扛单把亏的藏成浮亏、刷出假胜率。
# 只在【胜率≥WIN_FLOOR 且 最惨实现亏损趋近0】时触发;真会止损(最惨实现亏损≥LOSS_REF)的高胜率钱包不受影响。
SCORE_MANUF_WIN_FLOOR = 0.95   # 胜率超过此才疑似(95% 以下完全不罚)
SCORE_MANUF_LOSS_REF  = 0.03   # 最惨实现亏损 ≥ 此(真在止损)→ 不罚;趋近 0(从不兑现亏损)→ 满罚
SCORE_MANUF_PEN       = 0.5    # 满罚强度(评分 ×(1−此))

# LOSS-DISCIPLINE demote ("扛单降权"). Measures NOT cutting losses DIRECTLY — never via win rate. The
# score multiplies by 1/(1+K·disc), where disc = 5×(current losing-bag burden: depth×count×duration) +
# 1×(historical forced liquidations). A clean fast-cutter (no open loss, never liquidated) is untouched
# however high its win rate; a wallet sitting on several deep bags for days, or that's been force-closed,
# is demoted. SOFT: sinks the worst toward/below the follow line, never zeroes a profitable wallet. 0 =
# off. Tunable via dashboard (apply_scanner_params pushes it onto config so scan + regate both honor it).
DISP_PENALTY_K = 0.6   # demote strength (0 = disabled; higher = harsher). score *= 1/(1+K·disc)
# REALIZED-asymmetry sub-term of disc — catches "小赚大亏 / 不及时止损" (the twins, #17, RESOLV) by the
# tail directly: |worst realized loss| vs the median win. v5 (2026-06-29): the OLD win-rate gate
# (defer = 1-loss_rate/LOSS_RATE_REF) is REMOVED — it zeroed this penalty for any wallet with win<85%,
# so a 60%-win churner with a 4× tail loss sailed through. loss_pain now bites at ANY win rate; a clean
# fast-cutter (small symmetric losses) has loss_pain≤TAIL_FREE → still untouched.
TAIL_FREE     = 1.5    # worst loss up to this × median win is fine; beyond = asymmetric (小赚大亏)
ASYM_W        = 1.5    # weight of the asymmetry term inside disc (0.8→1.5: 小赚大亏 sinks below the line)
LOSS_RATE_REF = 0.15   # (retired — the asym win-rate gate is gone; kept only so stale refs don't break)
PAIN_MIN_TRADES = 15   # ≥ this many closed trades with ZERO realized losses = extreme deferrer
PAIN_NOLOSS   = 4.0    # loss_pain assigned to a never-realized-a-loss wallet over a large sample
# HOLD-SKEW sub-term of disc — 扛单 by DURATION: median losing-hold / median winning-hold. >1 = holds
# losers longer than winners (disposition effect). Only EXTREME skew is penalized (the dangerous combo is
# high skew WITH a big tail loss, already caught by loss_pain); moderate skew on small losses is benign.
HOLD_SKEW_FREE = 3.0   # skew up to 3× is tolerated (holding small losers a bit longer ≠ blow-up risk)
HOLD_SKEW_W    = 0.5   # weight of the (hold_skew - FREE) term inside disc

# ── DISCIPLINE GATES (2026-06-30) — promote the SOFT score sub-terms above to HARD watchlist-entry
# gates, so a 赌徒 never enters the watchlist at all (not merely ranked low). These use metrics ALREADY
# stored on the profile (loss_pain / hold_skew / profit_conc) → `regate` applies them instantly with no
# re-fetch. Plus a LIFETIME-net check (the one new datum, from the full-history fetch) that catches a
# wallet whose blow-up is OLDER than the 14d scoring window (e.g. #47: clean 14d, but -123k over 287d).
# All UI-tunable (params.py → apply_scanner_params overlays onto the scan/regate namespace).
GATE_LOSS_PAIN_MAX   = 1.0   # reject if |worst realized loss| / median win ≥ this (要求小亏大赚:worst<median win). 0 = off.
GATE_HOLD_SKEW_MAX   = 1.5   # reject if median losing-hold / winning-hold ≥ this (抗单). 0 = off.
GATE_PROFIT_CONC_MAX = 0.8   # reject if one day ≥ this share of gross profit (一把行情/未经验证). 0 = off.
GATE_REQUIRE_LIFETIME_NET = True   # reject if full-history realized net ≤ 0 (长期净亏). Skipped if the
#                                    net_life field is absent (old profiles) so regate is safe pre-rescan.
GATE_REQUIRE_30D_NET      = True   # reject if 30d realized net ≤ 0 (近一月在走下坡). Same absent-skip.
# How far back the profiler pulls fills (paginated, sorted, capped at max_pages*2000). We target
# RECENTLY-ACTIVE + RECENTLY-STABLE wallets only, and we run our OWN stop-loss + isolated margin, so a
# target's ancient blow-up doesn't transfer to us — fetching old history is wasted time. 30d exactly
# covers the 14d scoring slice + the 7/14/30d multi-window nets (net_life ≡ net over this 30d window).
PROFILE_FETCH_DAYS = 30

# INCREMENTAL scan (2026-07-01): the daily re-scan fetches only the fills SINCE our per-candidate cursor
# (max stored fill time) and merges them onto the stored PROFILE_FETCH_DAYS window — instead of re-pulling
# the whole 30d for every candidate every day (re-fetching 29 unchanged days = wasted API/time). Fills are
# cached in candidate_fills. A NEW candidate (no cache) still does one full-window fetch; a delta that hits
# the page cap falls back to a full fetch (self-heal). A periodic FULL re-sync (every FULL_RESYNC_DAYS)
# re-fetches everyone's window to heal any gap from a transient error (fills are append-only, so a gap can
# only be MISSING fills — a full re-fetch re-adds them). The live open-position snapshot is unaffected
# (still one cheap clearinghouse call per surviving candidate — that's current state, not history).
INCREMENTAL_SCAN = True     # False = always full-fetch (the old stateless behaviour)
FULL_RESYNC_DAYS = 7        # force a full-window re-fetch for all candidates at least this often (self-heal)

# TREND-trader inclusion: a winning OPEN position worth ≥ this fraction of the wallet's account = a real
# trend hold, so the wallet is kept even if low-frequency (exempt from the `irregular` activity floor).
TREND_OPEN_MIN = 0.05

# Unrealized gains are return NOT yet locked (can reverse) → count this fraction of a wallet's winning
# open position as RISK in the score denominator, so an unproven unrealized pump can't top the board
# over wallets that actually realized the same return. Trend traders stay included, just ranked behind.
UNREAL_RISK_W = 0.5

# SPOT-HEDGE exclusion: if more than this fraction of a wallet's perp-short notional is offset by a spot
# long of the same token, it's hedging spot (market-neutral), not trading directionally — reject. Its
# perp 'profit' is cancelled by spot, so copying the naked perp leg is a loss for us.
HEDGE_MAX_FRAC = 0.5

# COPY-SIDE STOP — our isolated-account tail guard: cut a copy when price runs against entry by more than
# the coin's own daily volatility. v9 (2026-06-30): σ-ADAPTIVE — the cut distance = STOP_SIGMA_MULT × σ
# (σ = daily high-low range), NOT a flat % anymore. So BTC(σ~4%) cuts at ~4% adverse, ZEC(σ~15%) at ~15%:
# never noise-stopped (a 1σ adverse move from a mid-range entry is already a tail event), always fires
# BEFORE liquidation (since lev = RISK_BUDGET/σ ⇒ liq at 1/lev = σ/RISK_BUDGET > σ for RISK_BUDGET<1).
# By construction a 1×σ stop realizes exactly RISK_BUDGET of the position's margin — uniform across coins.
# We can't bag-hold like the master ($40k cross + patience) so we cap the tail and free the capital.
# COPY_STOP_ENABLE = the master toggle (default ON; UI-tunable). COPY_STOP_PCT is the legacy flat-% stop,
# retained only as a fallback when a coin's σ is unavailable.
COPY_STOP_ENABLE = True
STOP_SIGMA_MULT  = 1.0      # cut at this × σ adverse move (1.0 = a full daily high-low range against us).
#                             Landed at 1.0 (2026-07-01) after 0.8→1.2→1.0: the safe band is M ∈ (0.8, 1.25)
#                             — must be >0.8 to ride out normal reversion spikes (XLM's 6.87% shakeout that
#                             the master held to profit) AND <1.25 so BTC's stop (M×4%) fires BEFORE its 20x
#                             liquidation (5%). 1.0 sits in the middle: BTC stops early at ~4% (<5% liq),
#                             alts (low lev, far liq) get a full daily-range of room. NOT the leverage anchor
#                             (that's RISK_BUDGET) — this is purely the stop distance. UI-tunable follow param.
COPY_STOP_PCT    = 0.18     # LEGACY flat-% fallback (used only if σ unavailable); σ-stop is primary now

# paper-copy simulation
LATENCIES = [0.5, 2.0, 5.0]  # (legacy) latency bands — schema columns; REST signal has one
TAKER_FEE = 0.00045          # detection latency, so all three resolve to the same live-book price
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)
BOOK_HIST_S = max(LATENCIES) + 3  # (legacy) bbo history depth — REST mode prices off current bbo only

DEFAULT_DB = "data/hl.db"
