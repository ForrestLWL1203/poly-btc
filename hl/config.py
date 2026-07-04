"""Shared constants — endpoints, hard limits, sim parameters. No logic here."""

# Hyperliquid endpoints
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
UA = {"User-Agent": "hl-copytrade/0.3", "Accept": "application/json", "Content-Type": "application/json"}

# numeric
FLAT = 1e-6                 # |position| below this (coin units) counts as flat
MIN_POST_INTERVAL = 1.1     # global REST pace (s/POST). HL /info budget = 1200 WEIGHT/min/IP, and
#                             our heavy calls (userFillsByTime, frontendOpenOrders) cost weight 20
#                             each (+1 per 20 results) — so the real ceiling is ~60 weight-20/min,
#                             NOT a request count. 1.2s = 50/min ≈ 1000 weight/min: safely under
#                             1200, leaving headroom for the 8s-trickle scanner (~150 weight/min)
#                             on the same IP. (l2Book/clearinghouseState are only weight 2.)
SCAN_IDLE_INTERVAL = 1.2    # scan REST pace when NO copy-trading is running — full speed (the observer
#                             isn't competing for the IP's weight budget). Adaptive: the scan uses the
#                             slow --scan-interval only while the observer is live; idle → this. ~15min sweep.
#                             The scanner overrides this to --scan-interval in its own process.

# HL WS hard limits (per IP, official): the binding one is unique users.
MAX_WS_USERS = 10           # max unique users across user-specific subscriptions (WS only)

# Copy engine: SIGNAL via REST poll (per-wallet userFills — REST has no 10-user cap, so we can
# watch the whole watchlist); PRICING via WS bbo (per-COIN top-of-book — NOT subject to the
# 10-user cap, only the 1000-sub cap, and we touch only a few dozen coins). Targets are low-freq
# long-hold, so a few-seconds poll latency is fine; we execute against the live book at detection.
MIN_FOLLOW_SCORE = 0.88     # follow watchlist wallets with score >= this. v5 (2026-06-30): score is now
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
AUTO_SCAN_EVERY_H = 24.0   # dashboard auto-scan cadence: spawn a silent full scan this long after the last one
WATCHLIST_RELOAD_S = 300   # re-read the watchlist table this often (track rolling discovery)
POLL_OVERLAP_MS = 12000    # re-fetch this far behind each wallet's in-memory cursor (tid-dedup absorbs
#                            it) so a fill landing between poll rounds isn't missed. This is the ONLY
#                            look-back — the observer is forward-only, it never catches up on history.
#                            (Widened from 5s so a slower round can't slip a fill past the boundary.)
POLL_CONCURRENCY = 10      # signal-poll fan-out: fetch this many wallets' fills concurrently. The global
#                            pacer still spaces the SPAWN of each POST, but the network round-trips overlap
#                            instead of running serially → a round's wall-time ≈ (N × pace), not (N × (pace+RTT)).
ORDER_POLL_S = 60          # frontendOpenOrders (target limit-order INTENTIONS — display/analysis, NOT the copy
#                            hot path) polled at most this often. Was ~continuous (5s) and cost 1 weight-20 call
#                            PER wallet, stealing ~half the REST budget from the fill signal → doubled copy LAG.
LIVE_FILLS_RETENTION_DAYS = 7  # prune live_fills older than this (tid-dedup only needs the overlap
#                                window; the rest is audit) — keeps the only unbounded table bounded

# Copy account & sizing (UI-tunable). Real-account paper model: a simulated wallet with an initial
# balance. Each copy commits isolated margin out of CURRENT AVAILABLE balance, sized by VOLATILITY
# TARGETING (below) — never a fixed $ amount, always a fraction of available. notional = margin *
# leverage; isolated liquidation (loss = margin). No stop-loss in v1.
INITIAL_BALANCE = 10000.0   # simulated wallet starting equity ($)
ADD_FRAC = 0.5              # each follow-on ADD commits this fraction of the position's FIRST-OPEN margin
#                             (NOT the tier margin% again — so BTC 3% first + 3×(3%·0.5) = 7.5% max, not 12%).
#                             One knob, auto-scales per tier off each position's own first entry.
# max follow-on ADDS per position — PER σ-TIER (a volatile coin shouldn't pile into a huge position via
# repeated averaging). Each add = first-open margin × ADD_FRAC. UI-tunable per tier.
STABLE_MAX_ADDS = 3         # BTC/majors calm → OK to keep averaging in
MID_MAX_ADDS    = 2         # ETH/SOL/HYPE
HIGH_MAX_ADDS   = 1         # volatile/meme/stock → at most one add (don't build size on a wild coin; 0 = never add)

# v8 SIZING (2026-06-30). Three VOLATILITY TIERS (by daily σ = high-low range, see volatility.py); each
# tier has its own margin% + leverage cap; WITHIN a tier, leverage scales continuously with σ. σ classifies
# AND fine-tunes — no coin lists. Anchored to AVAILABLE (self-throttles as positions fill). Tier by σ:
#   stable  σ ≤ STABLE_SIGMA_MAX        (BTC + anything calmer incl low-σ stocks like GOLD) → big
#   mid     STABLE_SIGMA_MAX < σ < HIGH_SIGMA_MIN  (ETH/SOL/HYPE/majors)
#   high    σ ≥ HIGH_SIGMA_MIN          (ZEC/memes/wild) → small
#   margin   = EQUITY × <tier>_MARGIN_PCT   (v10 2026-07-02: equity, NOT shrinking available — so a wallet's
#              copy is the same size regardless of open order; free cash only gates it as a hard backstop)
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
STABLE_MARGIN_PCT = 0.06    # FIRST-OPEN margin = this × EQUITY, for STABLE-tier coins (BTC). Sized for the
#                             TYPICAL position (first-open ONLY) — adds are the MINORITY case, so do NOT budget
#                             for the rare full-built (that would leave every non-add position too small to
#                             matter). 6% × 20x ≈ 120% notional (meaningful standalone); ~16 concurrent before the
#                             cash gate taps in; 4.2% max loss/position @70% stop. (v6: line 88 → ~24 targets.)
MID_MARGIN_PCT    = 0.05    # ...MID (ETH/SOL): 5% × 10x = 50% notional
HIGH_MARGIN_PCT   = 0.04    # ...HIGH (meme): 4% × 5x = 20% notional
STABLE_LEV_CAP = 20.0       # leverage ceiling for STABLE-tier coins
MID_LEV_CAP    = 10.0       # ...for MID-tier coins
HIGH_LEV_CAP   = 5.0        # ...for HIGH-VOL-tier coins
# PER-TIER minimum order notional: skip a copy whose FINAL notional (after the master-notl cap) is below
# its tier's floor — a too-small position isn't worth the fee/latency drag (esp. on calm coins where the
# whole edge is a fraction of a %). Per-tier only — the old flat dust floor (MIN_COPY_NOTIONAL) was removed. UI-tunable ($).
STABLE_MIN_NOTIONAL = 5000.0   # BTC/majors: below $5k it's not worth opening
MID_MIN_NOTIONAL    = 3000.0   # mid-vol coins
HIGH_MIN_NOTIONAL   = 800.0    # volatile/meme/stock: smaller floor (higher σ, smaller sizes are normal)
#                             (STOCK_FORCE_HIGH_TIER rolled back 2026-07-01 — stocks tier by their own σ;
#                             their over-leverage risk is handled by the master-leverage cap, not tier-forcing.)
REDUCE_STEP_FRAC = 0.10       # REDUCE STEPPING: an algo master dribbles a huge position out in 100s of tiny
#                             orders → mirroring each is noise + fees. Only mirror a reduce once the master's
#                             cumulative unwind since our last reduce reaches this fraction of his position
#                             (10% → at most ~10 partial reduces/position). Smaller unwinds accumulate; the next
#                             reduce cuts the whole accumulated ratio (self-correcting proportional mirror), and
#                             a FULL close always executes (exact flat). If he dumps it in 2 big fills, we follow both.
STOCK_MAX_LEV = 10.0        # HARD leverage ceiling for stock/builder perps (xyz:*), regardless of σ-tier or
#                           master lev. Stocks GAP (earnings/news) and their calm realized σ (e.g. TSLA 4%)
#                           badly understates tail risk — mean-daily-range σ let TSLA into the STABLE tier at
#                           20x, and one 10% day ate our profit. No σ statistic reliably catches stock gaps →
#                           cap by instrument class. (2026-07-02, after the TSLA 20x blow-up.)
MIN_LEV = 1.0               # leverage floor — ultra-volatile coin → ~spot (isolated 1x ≈ unliquidatable)
#                           (per-coin cap now lives entirely in the σ-tiered STABLE/MID/HIGH_COIN_CAP_PCT below;
#                           the old flat COIN_MARGIN_CAP_PCT was removed 2026-07-02 — the tiered caps fully cover it)

# ═══ 加仓策略引擎(独立)═══ B 逆向加仓可选:老"硬cap"(分档次数 + ADD_FRAC) 或 新"智能动态"
ADD_STRATEGY = "smart"       # "smart" | "hardcap"  —— B 逆向加仓的模式(A 正向加仓固定用 hardcap)
# 智能模式三闸:①波动闸 x = ADD_GAP_K×σ(目标加仓相对我们上次加仓价 逆向移动 ≥ x 才跟;数据标定 0.15 利润最大)
#              ②每跟一次 x ×ADD_GAP_SHRINK_G(逐步收紧,加仓次数自然收口)③单币预算封顶(下面三档)+ 硬顶
ADD_GAP_K = 0.15            # 波动闸 σ 系数(逐币:x = k×该币σ)
ADD_GAP_SHRINK_G = 1.5     # 收缩因子(每加一次门槛×此)
ADD_MAX_HARD = 8           # 智能模式硬顶(兜底;通常单币预算先触顶)
FOLLOW_POS_ADD = False     # A 正向加仓:目标"顺势加仓"(价格朝其有利方向、拉高成本)时是否跟。默认关=不追盈利加仓;
#                            开=也按比例镜像跟(共用硬顶+三档预算)。B 逆向(摊低)始终按上面的波动闸走。
# 智能模式加仓额 = (目标本次加仓额 ÷ 目标首仓额) × 我们首仓保证金,封顶到该币剩余"单币预算"。
# 三档单币"灾难闸":同一币+同向所有仓位保证金合计 ≤ 占账户%。不是"单笔税"(单笔大小由 EQUITY×MARGIN_PCT 定),
# 而是封住"N 个钱包碰巧全压同一币同向 → 一次波动最多吃掉账户的百分之几"。实测极少堆币(最集中仅~9%),故设宽
# (2026-07-02: 20/12/6 → 40/30/20),日常不触发,只拦真·极端堆仓;高波动币仍比 BTC 更严。
STABLE_COIN_CAP_PCT = 0.40
MID_COIN_CAP_PCT    = 0.30
HIGH_COIN_CAP_PCT   = 0.20
MAX_DEPLOY_PCT = 0.80       # PORTFOLIO deployment cap: stop opening NEW positions once total committed margin
#                           reaches this fraction of equity. Equity-based sizing (每笔=权益×档位%) has no
#                           self-throttle (~20 fixed-size opens = 100% full), so it saturated fast. This keeps
#                           a (1-this)=20% dry-powder reserve for ADDS (逆势摊低仍要吃保证金) + new signals +
#                           risk buffer. Adds MAY dip into the reserve (they're higher-value than a fresh open).
MIN_OPEN_MARGIN_PCT = 0.005 # skip a new copy if its formula margin (= MAX_MARGIN_PCT·scale·available) is below this
#                             fraction of equity: once free balance is too low to fund a MEANINGFUL
#                             position, just skip the signal (don't open dust). Existing positions stay
#                             managed/exited. High-conviction signals (bigger rf) still open later than
#                             low-conviction ones, which is intended. UI-tunable.
# (the flat post-cap dust floor MIN_COPY_NOTIONAL was replaced by the per-tier STABLE/MID/HIGH_MIN_NOTIONAL
#  above — a $4-probe master position now falls under its tier's min and is skipped.)
MAX_LEV = 20.0              # hard leverage cap (BTC + anything calmer pin here); also a stale-σ backstop

# Per-coin volatility (regime-aware) for the sizing above. A coin calm-then-erupting must NOT keep its
# old low σ and get over-levered into a blow-up — so we use TWO horizons and take the MAX (de-risk fast
# when vol rises, re-risk slowly when it falls). Refreshed periodically into the coin_vol TABLE off the
# signal hot path; sizing just reads the row. σ_used = max(σ_fast, σ_slow), both daily realized vol.
VOL_FAST_DAYS = 7           # recent window — catches a fresh volatility regime within ~a day
VOL_SLOW_DAYS = 30          # long baseline — stable; the floor we hold until calm is sustained
VOL_MIN_SAMPLES = 5         # need this many daily candles, else fall back
VOL_REFRESH_S = 43200       # re-fetch each tracked coin's σ at most this often (12h). σ is built from CLOSED
#                             daily candles (today's forming candle is dropped) → it can only STEP when a day
#                             closes, so refreshing more than a couple times a day is pure wasted REST budget.
#                             (A newly-seen coin still gets its σ fetched immediately via _ensure_vol.)
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
EXEC_MAKER_MIRROR = True      # maker book rests at the passive book side on the target's maker fills (saves the
#                              spread vs crossing) — only fires when our_maker=True, so the taker book (always
#                              our_maker=False) is unaffected; assumes our rest fills (optimistic; 戳破 = v2).

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
HARVEST_WEEK_VLM_MAX = 100_000_000.0 # 7d VOLUME ceiling (v9: 30M→100M). Absolute volume is a CRUDE churner cut —
#                                     the turnover gate (vlm/equity) does it precisely at profile, so a big LEGIT
#                                     account (deep pockets, low turnover) must not be pre-excluded here. Cheap
#                                     stage-1 noise-cut only; churner judgment deferred to PORTFOLIO_MAX_TURNOVER.
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
# v6 (2026-07-02): the THREE roots are 胜率 / 活跃度 / ROI (user). 活跃度 promoted from evidence-multiplier
# to a CORE term; 逐日稳定性 dropped. NO 反噬/worst-loss guard — 小赚大亏 already shows as low/neg ROI
# (net≤0 gated; low ROI → low ROI term). We copy ISOLATED + our own stop, so their single big loss doesn't
# transfer. Only guards ROI can't see remain: 刷胜率 (fake win by holding losers) + a mild current-deep-bag.
SCORE_W_WIN  = 0.35    # 胜率权重
SCORE_W_ACT  = 0.30    # 活跃度权重(成交数 + 活跃天数,升为核心项) —— W_* 之和自动归一
SCORE_W_ROI  = 0.35    # ROI 权重(收敛后;ROI 本身就把"小赚大亏"量化为低分)
SCORE_STRETCH = 1.15   # 线性拉伸:最强真实钱包 ≈ 100,平滑下滑(便于设跟单线)。调大→top 更贴近 100
ROI_NOTL_FLOOR    = 1000.0 # 名义额下限(仅用于把 max_drawdown 归一成 dd_eq;防除零/噪音)
SCORE_DD_AVERSION = 3.0   # roi_adj = max(0,roi)/(1 + 此×回撤dd_eq):回撤越大有效edge越低(回撤按名义额归一)
SCORE_ROI_SCALE   = 0.35  # roiS = 1 − exp(−roi_adj/此):综合ROI 分布~0.05–1.5,此值让有效区拉得开(0.3→0.58,0.5→0.76,1.0→0.94)
# ROI 支柱口径 = HL 官方 return-on-capital(净利/本金,已按出入金调整、含杠杆资本效率),取代旧的 net/名义
# (net/名义 ≡ 真实收益率 ÷ 杠杆,把杠杆红利除没了,系统性埋没大体量 BTC 波段客)。
# copy 只跟【最近表现】→ 只用近期两窗口(周+月),全期(all_roi)权重=0 不计入(新号/小本金复利虚高、与"跟最近"无关):
ROI_W_WEEK = 0.40         # 近期(7d)权重 —— 最近状态(copy 关注点)
ROI_W_MON  = 0.60         # 月度(30d)权重 —— 主锚(窗口固定、噪音适中)
ROI_W_ALL  = 0.00         # 全期 = 0:不看长期战绩(对跟单无意义,且会爆表带飞)
ROI_CLIP_LO = -0.5        # 各窗口 ROI 先 clip 到 [此, 上]:压离群 + 防单窗口幸运带飞
ROI_CLIP_HI = 1.0         # +100% 单窗口封顶:>100% 一律视为"优秀",避免单个月/周暴涨独撑排名(需周+月都好)
SCORE_EV_TRADES = 20      # 活跃度:达此回合数 = 满分
SCORE_EV_DAYS   = 10      # 活跃度:达此活跃天数 = 满分
# 反噬/双胞胎守卫 —— 最惨单笔 ÷ 净利润 = |worst_loss_pct|/roi_equity。抓"n笔小赚+1笔大亏吞掉所有收益"的高胜率欺骗手;
# 用户的良性例(5赢@5%+1亏@7.5% → 7.5/17.5=0.43)在 FREE 内、不罚。
SCORE_FRAG_FREE = 0.5     # 最惨单笔 ≤ 净利润此比例 → 不罚
SCORE_FRAG_SPAN = 1.0     # 超出 FREE 后再涨此幅 → 守卫降到下限(frag≥1.5≈被压到底)
# 深度抗单/爆仓守卫 —— 按【深度】不按持仓时间(用户:小幅逆向抗回盈利很正常、且我们有自己的止损):
# 深度 = 单仓最惨浮亏 open_underwater(真实扛单深度,不用 open_loss_frac:大账户会把总浮亏稀释成"看着没事",
# 即"无限保证金熬过来"的假象)。BAG_REF 6%(用户:≤7% 还能接受),所以 −7% 几乎不痛、−9% 中扣、−29% 砍到底。
SCORE_BAG_REF  = 0.10     # 当前单仓浮亏达账户此比例才开始轻扣(软化:10%起;isolated+自有止损让它只是小信号)
SCORE_BAG_SPAN = 0.20     # 浮亏超出 BAG_REF 后再涨此幅 → g_deep 降到 DEEP_FLOOR
SCORE_DEEP_FLOOR = 0.75   # 当前深亏守卫下限(最多扣 25%)
SCORE_GUARD_FLOOR = 0.25  # 刷胜率守卫下限(最差也保留 25%,靠分数线压在线下,而非硬杀)
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
# v7 PORTFOLIO copyability gates (from HL portfolio: net-of-fees, deposit-adjusted; only when pf data present).
PORTFOLIO_MAX_TURNOVER = 80.0      # 换手率上限 = 周成交量/权益. >this = HFT bot (unreplicable at our latency +
#                                  fee-drag we can't outrun). Full-pop dist: p75=39x (trend), p90=126x (bots).
PORTFOLIO_MIN_EDGE_BPS = 20.0     # 边际下限 = 30d 净利/成交量 ×1e4. <this ≈ <2× our ~9bp round-trip taker cost →
#                                  no margin left after our slippage/latency. Month window (30d) = less noisy than 7d.
# --- v9 strict-gate additions: every wallet that survives to the watchlist must be genuinely copyable ---
MIN_PAYOFF = 1.0        # 盈亏比下限 avg_win/avg_loss. <this = 大亏小赚(平均亏 > 平均赢)—— 我们跟会放大那笔大亏、
#                        剪掉小赢。低胜率真趋势客 payoff 天然 >1(否则不盈利)故不受影响;抓的是高胜率倒挂盘。
WINDFALL_CONC    = 0.80  # 单日利润集中度上限:单日 >= 此比例的毛利 且 胜率 < WINDFALL_WIN_MAX = 靠一笔偶然大赚撑着
WINDFALL_WIN_MAX = 0.60  # (亏损尚未覆盖,ROI 此刻还正)→ reject。真·高胜率的集中不算(它靠稳定胜率不靠一把)。
GATE_REQUIRE_WEEK_EDGE_POS = True  # 近一周 edge 转负(且有真实成交量)→ reject:月度光环掩盖近期反转,当下在亏。
MAX_CONCURRENT_POS = 15  # 峰值同时持仓数上限. 我们权益均额开仓 + 部署上限 → 只能同时装 ~5-8 个仓;目标同时开 >此 数量,
#                          我们只能随机抓其中一小片(拿不到它靠全组合对冲的净正),结构上跟不了 → reject too_many_concurrent。
#                          全池 p90=8、断层在 12-17 之间;15 卡在断层,切掉极端组合客(如 0xc9c781 峰值20),不误伤 10-11 的慢波段好钱包。
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

# COPY-SIDE STOP — our isolated-account tail guard: cut a copy when its unrealized loss reaches a fixed
# fraction of ITS OWN MARGIN. v10 (2026-07-01): MARGIN-BASED — replaces the old σ-multiple stop.
# WHY the change: a price-distance stop (σ× or flat-%) is leverage-BLIND — the same adverse price % costs 5×
# more margin at 5x than at 1x — so the σ-stop fired inside normal intraday noise on leveraged positions and
# cut positions the master rode back to profit (verified 2026-07-01: 6 σ-stops = −$682, 4 of which the master
# recovered to profit; the tight stop was net-negative even counting the 2 it correctly protected). And
# drawdown DEPTH doesn't separate "recovers" from "bags" (SILVER bagged at 0.5σ, XLM recovered at 0.77σ) —
# that is a wallet-SELECTION signal, not a stop signal. So the stop is now a pure catastrophe backstop in
# MARGIN terms: cut at STOP_MARGIN_PCT of margin. Leverage-aware (adverse price move = STOP_MARGIN_PCT ÷ lev),
# coin-agnostic, always BEFORE liquidation (liq = 100% of margin). COPY_STOP_ENABLE = master toggle (UI).
COPY_STOP_ENABLE = True
STOP_MARGIN_PCT  = 0.70     # cut when unrealized loss ≥ this fraction of the position's margin (0.70 = bail
#                             at 70% of the way to liquidation). Leverage-aware adverse price: 5x → ~14%,
#                             3x → ~23%, 7x → ~10%. UI-tunable follow param. Disable → ride to liquidation.

# paper-copy simulation
LATENCIES = [0.5, 2.0, 5.0]  # (legacy) latency bands — schema columns; REST signal has one
TAKER_FEE = 0.00045          # detection latency, so all three resolve to the same live-book price
MAKER_FEE = 0.00015          # 1.5bp — maker-shadow account fills passively (resting limit), pays the maker rate
MAKER_THROUGH_WINDOW_MS = 20000  # v2 戳破: rolling window over which we track a coin's price extreme to decide
#                                 whether the price traded THROUGH our resting maker price (else we didn't fill)
SHADOW_MAKER_ENABLED = True  # gate the parallel maker-shadow book (turn on once the taker refactor is verified)
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)
BOOK_HIST_S = max(LATENCIES) + 3  # (legacy) bbo history depth — REST mode prices off current bbo only

DEFAULT_DB = "data/hl.db"
