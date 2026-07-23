"""Shared constants — endpoints, hard limits, sim parameters. No logic here."""

# Hyperliquid endpoints
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
UA = {"User-Agent": "hl-copytrade/0.3", "Accept": "application/json", "Content-Type": "application/json"}

# AI risk radar.  It is shadow-only in v1: assessments can annotate an executable first open but can never
# veto Observer execution.  Secrets are installed through the encrypted provider-credential command flow.
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
RISK_RADAR_MODEL = "deepseek-v4-pro"
RISK_RADAR_PROMPT_VERSION = "risk-radar-v1"
RISK_RADAR_INTERVAL_S = 15 * 60
RISK_RADAR_VALID_FOR_S = 20 * 60
RISK_RADAR_REQUEST_TIMEOUT_S = 45
RISK_RADAR_BALANCE_INTERVAL_S = 6 * 60 * 60
RISK_RADAR_RETENTION_DAYS = 2
# 48 hours at the normal 15-minute cadence.  Old unreferenced market judgements no longer affect a live
# decision; assessments referenced by Shadow order evidence remain available for outcome settlement/audit.
RISK_RADAR_MAX_ASSESSMENTS = RISK_RADAR_RETENTION_DAYS * 24 * 4
RISK_RADAR_BLOCK_SCORE = 75
RISK_RADAR_EXTREME_SCORE = 90
# DeepSeek V4-Pro official CNY list price per 1M tokens (2026-07-15).  Runway uses actual usage fields and
# the latest balance; cache-unknown input is deliberately costed at the conservative cache-miss rate.
DEEPSEEK_V4_PRO_INPUT_CACHE_HIT_CNY_PER_M = 0.025
DEEPSEEK_V4_PRO_INPUT_CACHE_MISS_CNY_PER_M = 3.0
DEEPSEEK_V4_PRO_OUTPUT_CNY_PER_M = 6.0

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

# Copy engine: SIGNAL via REST poll (per-wallet userFills — REST has no 10-user cap, so we can
# watch the whole watchlist); PRICING via WS bbo (per-COIN top-of-book — NOT subject to the
# 10-user cap, only the 1000-sub cap, and we touch only a few dozen coins). Targets are low-freq
# long-hold, so a few-seconds poll latency is fine; we execute against the live book at detection.
MAX_TARGETS = 40            # hard cap on followed wallets (bounds REST load even if many clear the score)
# Every complete generation starts from the highest-quality individually Core-ready wallets.  Portfolio
# tuning may remove only the low-quality suffix; it never substitutes a lower-ranked arbitrary subset.
CORE_INITIAL_MAX_N = 10
CORE_TARGET_MIN_N = 8            # service target: form 8-10 Core wallets when hard-risk-qualified supply exists.
CORE_TARGET_MAX_N = 10
CORE_REBALANCE_INTERVAL_DAYS = 7 # normal rank/portfolio reshuffles are weekly; hard risk failures remain immediate.
CORE_PROMOTION_MIN_HOURS = 24
CORE_SOFT_MIN_TENURE_DAYS = 14
CORE_PREFIX_UTILITY_RETENTION = 0.97
CORE_PREFIX_NET_RETENTION = 0.95
CORE_PREFIX_STRESS_RETENTION = 0.90
CORE_PREFIX_TIE_TOLERANCE = 0.02
CORE_PREFIX_ABS_UTILITY_SLACK = 50.0
CORE_PREFIX_ABS_NET_SLACK = 100.0
CORE_PREFIX_ABS_STRESS_SLACK = 100.0
CORE_PREFIX_MAX_DD_WORSEN = 0.01
CORE_PORTFOLIO_MAX_DRAWDOWN = 0.15
FOLLOW_SELECTION_MODE = "auto"       # auto | manual
CORE_MIN_COPY_RETURN_30D = 0.10
CORE_RETENTION_MIN_COPY_RETURN_30D = 0.07
CORE_SOFT_FAIL_CONFIRMATIONS = 2
CORE_MIN_FOLLOW_SCORE = 0.75
COPY_MIN_RAW_PAYOFF_RATIO = 0.60
COPY_STABILITY_FOLD_DAYS = 10
COPY_STABILITY_FOLD_COUNT = 3
COPY_STABILITY_MIN_CAMPAIGNS_PER_FOLD = 2
COPY_STABILITY_MIN_EVALUABLE_FOLDS = 2
COPY_STABILITY_MIN_PROFITABLE_FOLDS = 2
COPY_STABILITY_MAX_LOSS_TO_30D_PROFIT = 0.25
SELECTION_MIN_RELATIVE_GAIN = 0.05
CORE_REPLACEMENT_MIN_NET_RETURN = 0.02
SELECTION_MIN_ACTIONABLE_RATE = 0.70
SELECTION_MIN_CAPACITY_FIT = 0.75  # hard floor after joint tuning; lower means too many fundable opens were skipped
CORE_SEARCH_TIME_BUDGET_SEC = 0    # 0 = no wall-clock cutoff; the finite search graph remains bounded.
# Production multi-start search: fast discovery -> strict finalists -> repeated
# add/remove/swap/pair-add closure -> non-overlapping fold/cost-stress gate.
CORE_SEARCH_VALIDATION_FINALISTS = 12
CORE_SEARCH_STRICT_MOVE_SHORTLIST = 8
CORE_SEARCH_MAX_STRICT_MOVES = MAX_TARGETS
CORE_SEARCH_PAIR_ADD_LIMIT = 4
CORE_SEARCH_ROBUST_FINALISTS = 12
CORE_SEARCH_MAX_OPEN_RATE_DROP = 0.05  # Reject an addition that materially reduces executable opens.
CORE_SEARCH_MAX_CAPACITY_FIT_DROP = 0.05  # Reject material shared-balance contention before the floor.
OBSERVER_UNIT = "hl-observe"  # systemd unit the scan-trigger supervisor starts/stops on dashboard command
WATCHLIST_RELOAD_S = 300   # re-read the watchlist table this often (track rolling discovery)
POLL_OVERLAP_MS = 12000    # re-fetch this far behind each wallet's in-memory cursor (tid-dedup absorbs
#                            it) so a fill landing between poll rounds isn't missed. This is the ONLY
#                            look-back — the observer is forward-only, it never catches up on history.
#                            (Widened from 5s so a slower round can't slip a fill past the boundary.)
POLL_CONCURRENCY = 10      # signal-poll fan-out: fetch this many wallets' fills concurrently. The global
#                            pacer still spaces the SPAWN of each POST, but the network round-trips overlap
#                            instead of running serially → a round's wall-time ≈ (N × pace), not (N × (pace+RTT)).
LIVE_FILLS_RETENTION_DAYS = 7  # prune live_fills older than this (tid-dedup only needs the overlap
ACCOUNT_STATS_RETENTION_DAYS = 365  # keep the dashboard equity curve bounded (5-minute snapshots)
#                                window; the rest is audit) — keeps the only unbounded table bounded

# Copy account & sizing (UI-tunable). Real-account paper model: a simulated wallet with an initial
# balance. Each copy commits isolated margin out of CURRENT AVAILABLE balance, sized by VOLATILITY
# TARGETING (below) — never a fixed $ amount, always a fraction of available. notional = margin *
# leverage; isolated liquidation (loss = margin). No stop-loss in v1.
PAPER_WALLET_INITIAL_BALANCE = 10000.0  # paper wallet balance before strategy allocation
INITIAL_BALANCE = 10000.0   # paper strategy initial allocation / drawdown sizing anchor ($)
# Profits compound at full current strategy equity. Below the allocation anchor,
# position size contracts by this exponent instead of shrinking dollar-for-dollar.
# The multiplier prevents a deeply depleted account from taking recovery-sized risk.
SIZING_DRAWDOWN_EXPONENT = 0.50
SIZING_DRAWDOWN_MAX_MULTIPLIER = 1.50
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
#   stable  BTC always → fixed product tier (real σ still drives smart-add spacing)
#   mid     every non-BTC market while σ < HIGH_SIGMA_MIN
#   high    every non-BTC market with σ ≥ HIGH_SIGMA_MIN → small
#   margin   = SIZING_EQUITY × <tier>_MARGIN_PCT. Profits compound from current realized equity; below the
#              initial strategy allocation a bounded sqrt curve slows shrinkage. Real risk equity still owns
#              coin/deploy caps, and free cash remains the final hard backstop.
#   leverage = the σ-tier's LEV CAP (v10: σ-scaled RISK_BUDGET/σ dropped as redundant with tier cap +
#              master-lev cap + margin/coin/deploy limits + σ-stop). Clipped by MIN/MAX_LEV; the caller
#              further caps to the master's own leverage and the stock cap. σ still selects the tier.
#   notional = margin × leverage. (Capped at the master's notional — moot at our size, kept as safety.)
STABLE_SIGMA_MAX = 0.05     # compatibility/audit only: BTC is always stable-tier; non-BTC never enters stable.
HIGH_SIGMA_MIN   = 0.09     # σ ≥ this → HIGH-VOL tier; between the two → MID tier
STABLE_MARGIN_MIN_PCT = 0.020  # first-open margin lower bound once portfolio deployment gets crowded
MID_MARGIN_MIN_PCT    = 0.020
HIGH_MARGIN_MIN_PCT   = 0.012
STABLE_MARGIN_PCT = 0.035      # first-open margin upper bound when deployment is light (<= DEPLOY_FULL_PCT)
MID_MARGIN_PCT    = 0.030
HIGH_MARGIN_PCT   = 0.020
STABLE_LEV_CAP = 25.0       # leverage ceiling for STABLE-tier coins (operator-tuned: BTC 作为基准 25x)
MID_LEV_CAP    = 10.0       # ...for MID-tier coins
HIGH_LEV_CAP   = 4.0        # ...for HIGH-VOL-tier coins
# PER-TIER minimum order notional: skip a copy whose FINAL notional (after the master-notl cap) is below
# its tier's floor — a too-small position isn't worth the fee/latency drag (esp. on calm coins where the
# whole edge is a fraction of a %). Per-tier only — the old flat dust floor (MIN_COPY_NOTIONAL) was removed. UI-tunable ($).
STABLE_MIN_NOTIONAL = 2500.0   # BTC/majors: below this it's not worth opening
MID_MIN_NOTIONAL    = 1000.0   # mid-vol coins
HIGH_MIN_NOTIONAL   = 250.0    # volatile/meme/stock: smaller floor (higher σ, smaller sizes are normal)
#                             (STOCK_FORCE_HIGH_TIER rolled back 2026-07-01 — stocks tier by their own σ;
#                             their over-leverage risk is handled by the master-leverage cap, not tier-forcing.)
REDUCE_STEP_FRAC = 0.10       # REDUCE STEPPING: an algo master dribbles a huge position out in 100s of tiny
#                             orders → mirroring each is noise + fees. Only mirror a reduce once the master's
#                             cumulative unwind since our last reduce reaches this fraction of his position
#                             (10% → at most ~10 partial reduces/position). Smaller unwinds accumulate; the next
#                             reduce cuts the whole accumulated ratio (self-correcting proportional mirror), and
#                             a FULL close always executes (exact flat). If he dumps it in 2 big fills, we follow both.
DUST_CLOSE_NOTIONAL = 1.0    # after a mirrored reduce, if our leftover position is below this notional, close it
#                             immediately instead of leaving a $0 open-row dust position on the dashboard.
TAIL_CLOSE_ENABLE = True     # protect already-earned episode profit after a mirrored partial reduce.
TAIL_CLOSE_HARD_REMAIN_PCT = 0.20  # profitable tail at/below this share of our peak position exits outright.
TAIL_CLOSE_RISK_REMAIN_PCT = 0.35  # larger tails are eligible when their asset-specific liq risk is material.
TAIL_CLOSE_PROFIT_GIVEBACK_PCT = 0.50  # exit if tail-to-liq loss can erase this share of close-now profit.
# Optional high-water take-profit.  It is deliberately OFF by default: enabling it changes exit ownership
# for both live Paper execution and canonical Copy replay.  Arm is volatility-normalized (no sale at arm),
# then each stage measures drawdown from the remaining position's own high-water and rebases after a cut.
SMART_TP_ENABLE = False
SMART_TP_STABLE_ARM_SIGMA = 0.60
SMART_TP_MID_ARM_SIGMA = 0.50
SMART_TP_HIGH_ARM_SIGMA = 0.40
SMART_TP_GIVEBACK_1_PCT = 0.20
SMART_TP_GIVEBACK_2_PCT = 0.35
SMART_TP_GIVEBACK_3_PCT = 0.50
SMART_TP_CLOSE_1_PCT = 0.20
SMART_TP_CLOSE_2_PCT = 0.25
SMART_TP_CLOSE_3_PCT = 0.25
SMART_TP_TAIL_REMAIN_PCT = 0.30
SMART_TP_TARGET_REDUCE_EXIT_PCT = 0.30  # once only the tail remains, this cumulative target cut exits all.
SMART_TP_MIN_FEE_MULT = 2.0             # current floating profit must cover this multiple of the cut's exit fee.
STOCK_MAX_LEV = 10.0        # HARD leverage ceiling for stock/builder perps (xyz:*), regardless of σ-tier or
#                           master lev. Stocks GAP (earnings/news) and their calm realized σ (e.g. TSLA 4%)
#                           badly understates tail risk — mean-daily-range σ let TSLA into the STABLE tier at
#                           20x, and one 10% day ate our profit. No σ statistic reliably catches stock gaps →
#                           cap by instrument class. (2026-07-02, after the TSLA 20x blow-up.)
COIN_BLACKLIST = ""         # comma/newline separated exact coin ids to never open anew (e.g. XYZ:SHKX).
BLOCK_KOREAN_STOCKS = False # preset: block EWY/KR200/Samsung/SK hynix/Hyundai new opens and adds.
#                           Existing copy positions still reduce/close normally; flips close old side, then skip
#                           the new blacklisted side. Prefer adding from the position row to avoid symbol aliases.
LOW_LIQUIDITY_FILTER_ENABLE = True
MIN_COIN_DAY_NTL_VLM = 5_000_000.0  # crypto perp 24h notional volume floor; blocks thin meme/alt perps
MIN_COIN_OI_NOTIONAL = 2_000_000.0  # crypto perp open-interest notional floor; volume alone can be one-day noise
MIN_LEV = 1.0               # leverage floor — ultra-volatile coin → ~spot (isolated 1x ≈ unliquidatable)
#                           (per-coin cap now lives entirely in the σ-tiered STABLE/MID/HIGH_COIN_CAP_PCT below;
#                           the old flat COIN_MARGIN_CAP_PCT was removed 2026-07-02 — the tiered caps fully cover it)

# ═══ 加仓策略引擎(独立)═══ B 逆向加仓可选:老"硬cap"(分档次数 + ADD_FRAC) 或 新"智能动态"
ADD_STRATEGY = "smart"       # "smart" | "hardcap"  —— B 逆向加仓的模式(A 正向加仓固定用 hardcap)
# 智能模式三闸:①波动闸 x = k×σ(目标加仓相对我们上次加仓价 移动≥x 才跟;逆向/顺势各自有 k)
#              ②每跟一次 x ×ADD_GAP_SHRINK_G(逐步收紧,加仓次数自然收口)③单币预算封顶(下面三档)+ 硬顶
ADD_GAP_K = 0.12            # 逆向摊价波动闸 σ 系数(逐币:x = k×该币σ)
POS_ADD_GAP_K = 0.08        # 顺势加仓波动闸 σ 系数; FOLLOW_POS_ADD 开时也要过此闸,避免小碎单全跟
ADD_GAP_SHRINK_G = 1.2      # 收缩因子(每加一次门槛×此)
ADD_MAX_HARD = 8           # 智能模式硬顶(兜底;通常单币预算先触顶)
SMART_ADD_MIN_CAPACITY = 4 # 首仓必须为至少4次后续加仓保留单币容量；第4次允许用剩余额度部分成交
FOLLOW_POS_ADD = True      # A 正向加仓:目标"顺势加仓"(价格朝其有利方向、拉高成本)时是否跟。开=过 POS_ADD_GAP_K 才跟;
#                            关=完全不追盈利加仓。B 逆向(摊低)始终按 ADD_GAP_K 波动闸走。
# 智能模式加仓额 = min((目标本次加仓额 ÷ 目标首仓额) × 我们首仓保证金, 我们首仓保证金),
# 再封顶到该币剩余"单币预算"。目标一次巨额加仓不能吃掉多个槽；最后不足整笔时填满剩余预算。
# 三档单币"灾难闸":同一币+同向所有仓位保证金合计 ≤ 占账户%。不是"单笔税"(单笔大小由 EQUITY×MARGIN_PCT 定),
# 而是封住"N 个钱包碰巧全压同一币同向 → 一次波动最多吃掉账户的百分之几"。实测极少堆币(最集中仅~9%),故设宽
# (2026-07-02: 20/12/6 → 40/30/20),日常不触发,只拦真·极端堆仓;高波动币仍比 BTC 更严。
STABLE_COIN_CAP_PCT = 0.30
MID_COIN_CAP_PCT    = 0.22
HIGH_COIN_CAP_PCT   = 0.15
MARGIN_EQUITY_PCT = 1.00    # manual sizing base: each new open uses this share of drawdown-adjusted equity.
#                            The remainder is NOT reserved/frozen: real available cash, per-coin caps and
#                            portfolio deployment limits still use full risk equity, so it remains usable by
#                            other wallets, later signals and adds.  This knob is deliberately not auto-tuned.
DEPLOY_FULL_PCT = 0.40      # <= this deployed margin: use each tier's upper-bound margin. Between this and
#                           MAX_DEPLOY_PCT, new-open margin linearly shrinks to the tier lower bound.
MAX_DEPLOY_PCT = 0.80       # PORTFOLIO deployment cap: stop opening NEW positions once total committed margin
#                           reaches this fraction of equity. Equity-based sizing (每笔=权益×档位%) has no
#                           self-throttle (~20 fixed-size opens = 100% full), so it saturated fast. This keeps
#                           a (1-this)=20% dry-powder reserve for ADDS (逆势摊低仍要吃保证金) + new signals +
#                           risk buffer. Adds MAY dip into the reserve (they're higher-value than a fresh open).
WALLET_MARGIN_CAP_PCT = 0.25       # all open exposure copied from one source wallet, across every market/side.
WALLET_SECTOR_SIDE_CAP_PCT = 0.15  # legacy fallback; live cap is selected by board and volatility tier below.
WALLET_CRYPTO_STABLE_SIDE_CAP_PCT = 0.20
WALLET_CRYPTO_MID_SIDE_CAP_PCT = 0.15
WALLET_CRYPTO_HIGH_SIDE_CAP_PCT = 0.10
WALLET_STOCK_SIDE_CAP_PCT = 0.10
WALLET_MAX_OPEN_POSITIONS = 3      # a basket trader cannot occupy the account with many simultaneous symbols.
WALLET_STOCK_SIDE_MAX_POSITIONS = 2
MAX_TOTAL_MARGIN_PCT = 0.85        # unlike MAX_DEPLOY_PCT this also caps ADDS, preserving a hard risk buffer.
# Retired source-wallet breaker constants remain import-compatible for old offline records only. They are not
# included in strategy revisions and neither Observer nor canonical replay reads them.
WALLET_FORWARD_LOSS_FREEZE_PCT = 0.03
WALLET_HWM_FREEZE_DD_PCT = 0.03
WALLET_HWM_REDUCE_DD_PCT = 0.06
WALLET_HWM_EXIT_DD_PCT = 0.10
WALLET_HWM_RELEASE_DD_PCT = 0.02
WALLET_HWM_EXIT_COOLDOWN_DAYS = 7
LIQUIDATION_REENTRY_COOLDOWN_HOURS = 24
REPEAT_LIQUIDATION_FREEZE_DAYS = 7  # second copied liquidation inside 30d freezes the whole source for a week.
MIN_OPEN_MARGIN_PCT = 0.005 # skip a new copy/add if the post-cap margin is below this fraction of margin-calculation equity:
#                             once free balance is too low to fund a MEANINGFUL
#                             position, just skip the signal (don't open dust). Existing positions stay
#                             managed/exited. High-conviction signals (bigger rf) still open later than
#                             low-conviction ones, which is intended. UI-tunable.
# (the flat post-cap dust floor MIN_COPY_NOTIONAL was replaced by the per-tier STABLE/MID/HIGH_MIN_NOTIONAL
#  above — a $4-probe master position now falls under its tier's min and is skipped.)
EXECUTION_QUOTE_MAX_AGE_MS = 5_000  # Paper fills never reuse an older BBO; builder perps refetch l2Book on demand.
OBSERVER_DB_BUSY_TIMEOUT_MS = 1_500  # Retry fills quickly instead of freezing the whole event loop for 30s.
MAX_LEV = 50.0             # v10: raised 20→50 — leverage is now the VISIBLE σ-tier cap; MAX_LEV is only a
#                            far backstop + the ceiling on the master's read leverage (so "never exceed master"
#                            uses the master's REAL lev, not a 20-clipped one that wrongly under-levered us).

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
VOL_FALLBACK_SIGMA = 0.07   # neutral MID-tier σ when a valid market has too little closed-candle history.
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
# Applies to new opens only; exits are never blocked and always follow out.
MAX_ENTRY_CHASE_PCT = None    # e.g. 0.5 => skip a taker open whose entry is >0.5% worse than master

# Execution model (paper fidelity). We price off the current book at detection and always cross the spread.
# A REST-detected copy reacts after the target, so retroactively assuming a resting maker fill would flatter
# Paper results. A real-money maker workflow will be designed separately after Paper is stable.

# Stage-1 leaderboard recall (UI-tunable). The cheap hard surface proves $5k equity, $250k leveraged 7d
# notional activity, positive 7d/30d PnL, and stable capital efficiency in both recent windows: at least
# 10% 7d ROI and 20% 30d ROI. All-time ROI is audit/ranking only. Incumbent roles and open-position owners
# bypass recall and still receive their mandatory retention replay.
# This official ROI gate is discovery-only; scoped Perp evidence and strict-Copy replay decide executable roles.
HARVEST_MIN_ACCT = 5_000.0
HARVEST_WEEK_VLM_MIN = 250_000.0
HARVEST_WEEK_ROI_MIN = 0.10
HARVEST_MONTH_ROI_MIN = 0.20
HARVEST_ALL_ROI_MIN = 0.10
HARVEST_ROI_WINDOWS_MIN_PASS = 2  # compatibility/audit; hard recall explicitly requires week + month.
HARVEST_WEEK_PNL_MIN = 0.0
HARVEST_MONTH_PNL_MIN = 0.0
HARVEST_ALL_PNL_MIN = 0.0
HARVEST_PERP_PNL_SHARE_MIN = 0.60
PERP_PREFILTER_CACHE_TTL_S = 2 * 3600  # interrupted/redeployed scans reuse the same fresh Portfolio evidence
INACTIVE_DAYS = 3.0                 # Core needs a true flat->open signal within 72h; stale wallets remain Challenger.
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
SCORE_STRETCH = 1.227  # 线性拉伸:最强真实钱包 ≈ 100,平滑下滑(便于设跟单线)。改评分公式时由代码重标
ROI_NOTL_FLOOR    = 1000.0 # 名义额下限(仅用于把 max_drawdown 归一成 dd_eq;防除零/噪音)
SCORE_DD_AVERSION = 3.0   # roi_adj = max(0,roi)/(1 + 此×回撤dd_eq):回撤越大有效edge越低(回撤按名义额归一)
SCORE_ROI_SCALE   = 0.35  # roiS = 1 − exp(−roi_adj/此):综合ROI 分布~0.05–1.5,此值让有效区拉得开(0.3→0.58,0.5→0.76,1.0→0.94)
# ROI 支柱口径 = HL 官方 return-on-capital(净利/本金,已按出入金调整、含杠杆资本效率),取代旧的 net/名义
# (net/名义 ≡ 真实收益率 ÷ 杠杆,把杠杆红利除没了,系统性埋没大体量 BTC 波段客)。
# copy 只跟【最近表现】→ 只用近期两窗口(周+月):
ROI_W_WEEK = 0.40         # 近期(7d)权重 —— 最近状态(copy 关注点)
ROI_W_MON  = 0.60         # 月度(30d)权重 —— 主锚(窗口固定、噪音适中)
ROI_CLIP_LO = -0.5        # 各窗口 ROI 先 clip 到 [此, 上]:压离群 + 防单窗口幸运带飞
ROI_CLIP_HI = 1.0         # +100% 单窗口封顶:>100% 一律视为"优秀",避免单个月/周暴涨独撑排名(需周+月都好)
SCORE_EV_TRADES = 20      # 活跃度:达此回合数 = 满分
SCORE_EV_DAYS   = 10      # 活跃度:达此活跃天数 = 满分
# 深度抗单/爆仓守卫 —— 按【显著仓位深度】不按绝对亏损金额(大户金额天然大,对我们无意义):
# open_underwater 由 scanner 先过滤 dust 仓(仓位名义额/账户权益低于下方阈值),再取最深逆向;
# 总账面压力仍看 open_loss_frac。这样不会让几十刀小仓把一个 copy 回测很强的钱包踢出 active。
OPEN_RISK_MIN_POSITION_EQUITY_FRAC = 0.002  # 低于账户 0.2% 的当前仓位只算账面浮亏,不参与"最深逆向"打分
SCORE_BAG_REF  = 0.10     # 当前单仓浮亏达账户此比例才开始轻扣(软化:10%起;isolated+有界仓位让它保持为软信号)
SCORE_BAG_SPAN = 0.20     # 浮亏超出 BAG_REF 后再涨此幅 → g_deep 降到 DEEP_FLOOR
SCORE_DEEP_FLOOR = 0.75   # 当前深亏守卫下限(最多扣 25%)
SCORE_GUARD_FLOOR = 0.25  # 刷胜率守卫下限(最差也保留 25%,靠分数线压在线下,而非硬杀)
# 刷胜率守卫(双胞胎本质)—— 高胜率 + 几乎从不兑现亏损 = 靠扛单把亏的藏成浮亏、刷出假胜率。
# 只在【胜率≥WIN_FLOOR 且 最惨实现亏损趋近0】时触发;真会止损(最惨实现亏损≥LOSS_REF)的高胜率钱包不受影响。
SCORE_MANUF_WIN_FLOOR = 0.95   # 胜率超过此才疑似(95% 以下完全不罚)
SCORE_MANUF_LOSS_REF  = 0.03   # 最惨实现亏损 ≥ 此(真在止损)→ 不罚;趋近 0(从不兑现亏损)→ 满罚
SCORE_MANUF_PEN       = 0.5    # 满罚强度(评分 ×(1−此))

# Large samples with no realized loss are flagged as possible loss deferral for profile diagnostics.
PAIN_MIN_TRADES = 15   # ≥ this many closed trades with ZERO realized losses = extreme deferrer
PAIN_NOLOSS   = 4.0    # loss_pain assigned to a never-realized-a-loss wallet over a large sample

# Retired discipline hard-gates. Kept only so old scan namespaces / stale DB refs don't break.
# Do NOT use these as active vetoes: loss_pain / hold_skew can be high on wallets that are still
# profitable under our actual copy rules. Copyability is now judged by COPY_BT_* replay below.
GATE_LOSS_PAIN_MAX   = 1.0
GATE_HOLD_SKEW_MAX   = 1.5
GATE_PROFIT_CONC_MAX = 0.8
# v7 PORTFOLIO copyability gates (from HL portfolio: net-of-fees, deposit-adjusted; only when pf data present).
PORTFOLIO_MAX_TURNOVER = 80.0      # 换手率上限 = 周成交量/权益. >this = HFT bot (unreplicable at our latency +
#                                  fee-drag we can't outrun). Full-pop dist: p75=39x (trend), p90=126x (bots).
PORTFOLIO_MIN_EDGE_BPS = 10.0     # 边际硬底线 = 30d 净利/成交量 ×1e4. v10: 20→10 = 手续费打平点(<此我们结构性净亏 →
#                                  gate). 10bp 以上的"厚度"不再硬砍,交给 score 的 g_edge 平滑降分(避免误杀好钱包)。
# --- v9 strict-gate additions: every wallet that survives to the watchlist must be genuinely copyable ---
# (MIN_PAYOFF removed v10 — small_win_big_loss hard gate gone; 盈亏比 is now the g_payoff factor in score, ref = SCORE_PAYOFF_REF)
WINDFALL_CONC    = 0.80  # 单日利润集中度上限:单日 >= 此比例的毛利 且 胜率 < WINDFALL_WIN_MAX = 靠一笔偶然大赚撑着
WINDFALL_WIN_MAX = 0.60  # (亏损尚未覆盖,ROI 此刻还正)→ reject。真·高胜率的集中不算(它靠稳定胜率不靠一把)。
# === v10: quality magnitude lives in score() as smooth ranking factors. Qualification is decided by the
# structural/data/evidence/strict-Copy economic gates below; raw profile score must never veto a wallet that
# passes those authoritative checks. ===
# Winning-trade thickness (`win_pt`) is intentionally observational only. Portfolio edge bps is the hard
# thin-edge gate, and copy replay is the authoritative copyability check.
SCORE_PAYOFF_REF  = 1.0   # 盈亏比≥此(1.0)=满分,只罚真·大亏小赚(payoff<1); payoff 和胜率联动,高胜率天生不需高盈亏比
SCORE_PAYOFF_FLOOR= 0.6    # → 轻推,不双重惩罚高胜率盘(0x770493 payoff1.0/胜78% 不再被压)
EVIDENCE_MIN_DAYS   = 5   # 有效性硬闸:14天窗口内活跃天数 < 此 → insufficient_evidence(无战绩无从评判,取消趋势豁免)
EVIDENCE_MIN_TRADES = 7   #                已平回合 < 此 同理. 5天/7回合≈0.5单/天,砍纯持有+小样本尾巴,不误伤好钱包
COPY_BT_GATE_ENABLE = True  # active 准入二次校验: 用历史 fills 按当前 observer 规则回放,目标赚但我们亏 → 不跟
COPY_BT_DAYS = 30           # copy 回测窗口。用 30d 覆盖 14d 评分外的复制不稳定性,但仍是近期窗口
COPY_BT_WARMUP_DAYS = 7     # 每个窗口额外预热7天，恢复窗口开始前已经打开的仓位
COPY_BT_RECENT_DAYS = (14, 7)  # 近期确认窗口: 达到近期最低样本数后,近期 copy 亏损也不进 active
COPY_BT_MIN_CLOSED = 7      # copy资格最低已平样本；不足则不进入Active
COPY_BT_MIN_CLOSED_14D = 5  # 14d 近期窗口最低样本数; 不再只用 30d 门槛线性缩放
COPY_MIN_EXPECTED_MARGIN_RETURN = 0.02  # 回放扣成本后、向零收缩的每episode保证金收益；低于2%=薄利排除
COPY_BT_MIN_CLOSED_7D = 5   # 7d 少于 5 笔太容易被单笔噪声带偏,不作为盈利/亏损硬结论
COPY_BT_MIN_NET_PNL = 0.0   # copy 回测净收益必须 > 此值才可 active; 手续费已扣

# Core repeatability uses independent Campaigns and the non-overlapping folds above.
CORE_COPY_MIN_CAMPAIGNS_30D = 10
# A profitable low-win trend system is not automatically gambling: payoff and outlier evidence still matter.
# Core nevertheless needs enough winning Campaigns, including after the three largest trade-level winners are
# removed, that joining at an arbitrary point is not excessively dependent on catching a rare payoff.
CORE_COPY_MIN_CAMPAIGN_WIN_RATE = 0.45
CORE_COPY_MIN_BODY_WIN_RATE = 0.40
# One isolated 30d replay liquidation is already fully charged to PnL/drawdown and may coexist with a high-
# win, profitable surface. Repetition is path-dependent gambling and is rejected as a hard risk.
CORE_COPY_MAX_LIQUIDATIONS_30D = 1

# Intratrade path risk.  These percentages are normalized to the replay/member-epoch risk base.
COPY_DEEP_BAG_EVENT_PCT = 0.08
COPY_DEEP_BAG_EVENT_MIN_HOURS = 4.0
COPY_DEEP_BAG_LONG_HOURS = 24.0
CORE_INTRATRADE_DD_MAX = 0.12
CORE_INTRATRADE_DD_REJECT = 0.15
CORE_DEEP_BAG_MAX_FAILED = 1
CORE_DEEP_BAG_MIN_RECOVERY_RATE = 0.50

# Daily post-scan portfolio tuner. It moves the sizing surface approved by the operator and the smart-add
# core knobs. Lower bounds, per-coin caps, max deploy cap, and stop settings remain operator-controlled
# risk boundaries.
AUTO_TUNE_MARGIN_ENABLE = True
# The enable flag is the operator master switch; mode controls whether enabled runs only audit or apply.
AUTO_TUNE_MODE = "apply"              # Paper product default: off | shadow | apply
AUTO_TUNE_APPLY_MIN_SHADOW_DAYS = 0    # Paper validates by OOS/holdout/stress;真钱环境改回14
AUTO_TUNE_APPLY_MIN_FORWARD_CLOSED = 0 # Paper cold-start may apply;真钱环境改回100
AUTO_TUNE_MIN_DIRECTION_STREAK = 1     # one complete Paper generation;真钱环境建议2
AUTO_TUNE_MIN_RELATIVE_GAIN = 0.05
AUTO_TUNE_APPLY_COOLDOWN_DAYS = 0  # Paper每次完整generation都可重新寻优；真钱环境再设冷却
AUTO_TUNE_ROLLBACK_RELATIVE_DROP = 0.10
AUTO_TUNE_MASTER_LEVERAGE_MIN_COVERAGE = 0.0  # Paper exploration;真钱环境建议0.80
AUTO_TUNE_PRICE_PATH_MIN_COVERAGE = 0.94      # Paper: current bounded path cache; live-money should use >=.99.

# Canonical profitable Core replay must survive a bounded 15m market path. Fills-only replay remains the
# fast candidate search, but a new selection is not publishable when its final shared-account path is thin.
CORE_PRICE_PATH_MIN_COVERAGE = 0.94
CORE_MAINTENANCE_META_MIN_COVERAGE = 0.95
# The path tuner searches a compact neighbourhood around the profitable fills-only Core. It must not drive
# the entire portfolio to ultra-low leverage merely to reach zero proxy liquidations. Candidate selection
# requires preserved conservative profit and targets a 20% reduction from the effective path baseline.
AUTO_TUNE_MARGIN_FACTORS = (0.85, 1.0, 1.15)
# Cold-start databases do not have a learned margin surface to perturb.  Probe a few absolute points as a
# fraction of each tier's four-add-safe ceiling so the bounded tuner can rediscover materially larger sizing
# (for example 5-7% stable margin) without restoring a large three-dimensional Cartesian grid.
AUTO_TUNE_MARGIN_CEILING_FRACTIONS = (0.50, 0.75, 1.00)
AUTO_TUNE_COORD_MID_LEV_CAPS = (12, 10, 9, 8, 6)
AUTO_TUNE_COORD_STABLE_LEV_CAPS = (35, 30, 25, 20)
AUTO_TUNE_COORD_HIGH_LEV_CAPS = (6, 4, 3)
AUTO_TUNE_LEVERAGE_SHORTLIST = 2  # 每档保留当前/最佳代表值；组合网格最多 2^3=8，而不是 3^3=27
AUTO_TUNE_DEPLOY_FULL_PCTS = (0.40, 0.50, 0.60)
AUTO_TUNE_SIZING_FINALISTS = 5
AUTO_TUNE_MARGIN_COORD_ROUNDS = 2  # bounded closure can combine two profitable tier moves without 3-D grid
# Prefix-count discovery uses a sparse grid; the winning count receives one complete tune. Bound both modes
# so a large fills/path set cannot swap-thrash the production host indefinitely.
AUTO_TUNE_TIME_BUDGET_SEC = 1800
AUTO_TUNE_COARSE_TIME_BUDGET_SEC = 600
AUTO_TUNE_ADD_GAP_KS = (0.04, 0.08, 0.12)
AUTO_TUNE_POS_ADD_GAP_KS = (0.06, 0.09, 0.12)
AUTO_TUNE_ADD_SHRINK_GS = (1.1, 1.3)
AUTO_TUNE_ADD_MAX_HARDS = (6, 8, 10)
AUTO_TUNE_MARGIN_DAYS = (30, 14, 7)
AUTO_TUNE_FINALIST_LIMIT = 6  # 粗网格只让少量Pareto候选进入昂贵的非重叠折叠验证
AUTO_TUNE_ADD_FINALISTS = 3

CORE_PREFIX_EXHAUSTIVE_MAX_N = 8  # small quality pools tune every 1..N prefix; larger pools use binary search
# Backward elimination stops naturally when every remaining wallet has positive conditional economics.
# The cap only bounds a pathological run; it is not a stability quota or a promise to retain weak wallets.
CORE_LOO_MAX_REMOVALS = MAX_TARGETS - 1
CORE_LOO_MIN_NET_GAIN = 1.0
AUTO_TUNE_FILL_CACHE_MAX_BYTES = 64 * 1024 * 1024  # raw fill_json cache guard for 1GB VPS; fallback if exceeded
AUTO_TUNE_MARGIN_MIN_OPEN_FIT = 0.70
AUTO_TUNE_MARGIN_MAX_OPEN_FIT_DROP = 0.08
AUTO_TUNE_MARGIN_CAP_SKIP_FRAC = 0.05
AUTO_TUNE_MARGIN_MIN_FOLLOWED = 1
MAX_CONCURRENT_POS = 15  # 与资金/部署模型及参数默认值一致；共享账户容量仍由严格Copy再次验证。
#                          我们只能随机抓其中一小片(拿不到它靠全组合对冲的净正),结构上跟不了 → reject too_many_concurrent。
#                          全池 p90=8、断层在 12-17 之间;15 卡在断层,切掉极端组合客(如 0xc9c781 峰值20),不误伤 10-11 的慢波段好钱包。
MAX_SINGLE_ADDS_PER_EP = 30  # 仅完整 round-trip 的 scale-in 次数；执行侧智能间距/单币cap/ADD_MAX_HARD
#                              已阻止完整照抄。30+ 的完整回合仍视为不可复制的极端重DCA。
# How far back the profiler pulls fills (paginated, sorted, capped at max_pages*2000). We target
# RECENTLY-ACTIVE + RECENTLY-STABLE wallets only, and we run our OWN stop-loss + isolated margin, so a
# target's ancient blow-up doesn't transfer to us — fetching old history is wasted time. The extra 7d is
# replay warm-up; reported/scored windows remain 30/14/7d.
PROFILE_FETCH_DAYS = COPY_BT_DAYS + COPY_BT_WARMUP_DAYS

# INCREMENTAL scan (2026-07-01): the daily re-scan fetches only the fills SINCE our per-candidate cursor
# (max stored fill time) and merges them onto the stored PROFILE_FETCH_DAYS window — instead of re-pulling
# the whole 30d for every candidate every day (re-fetching 29 unchanged days = wasted API/time). Fills are
# cached in candidate_fills. A NEW/incomplete candidate still does one full-window fetch; a delta that hits
# the page cap falls back to a full fetch. A confirmed complete cache is never periodically re-downloaded:
# daily shard rotation refreshes its delta and re-scores the cached rolling window. The live open-position
# snapshot is unaffected.
INCREMENTAL_SCAN = True     # False = always full-fetch (the old stateless behaviour)
# Daily discovery budget. Core/held/challenger wallets are outside the discovery cap; the
# remaining budget is split new / near-miss / fair exploration and finalized before the wall-clock limit.
DAILY_SCAN_TIME_BUDGET_MIN = 60
CORE_REFRESH_DEADLINE_MIN = 15
SCAN_FINALIZE_RESERVE_MIN = 15
LEADERBOARD_MIN_ROW_RATIO = 0.85
LEADERBOARD_MIN_COMPLETE_RATIO = 0.99

# SPOT-HEDGE exclusion: if more than this fraction of a wallet's perp-short notional is offset by a spot
# long of the same token, it's hedging spot (market-neutral), not trading directionally — reject. Its
# perp 'profit' is cancelled by spot, so copying the naked perp leg is a loss for us.
HEDGE_MAX_FRAC = 0.5

# paper-copy simulation
TAKER_FEE = 0.00045
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)

DEFAULT_DB = "data/hl.db"
