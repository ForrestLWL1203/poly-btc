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
MIN_FOLLOW_SCORE = 1.2      # follow watchlist wallets with v3 score >= this (quality threshold, UI-tunable)
MAX_TARGETS = 40            # hard cap on followed wallets (bounds REST load even if many clear the score)
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

# VOLATILITY-TARGETED sizing (the core of v2). We do NOT mirror the target's leverage. Instead, per
# coin: leverage = clip(1/(RISK_K·σ), MIN_LEV, MAX_LEV) puts liquidation RISK_K daily-σ away on EVERY
# coin (calm BTC → high lev, wild meme → low lev, SAME staying power); and margin = RF·RISK_K·available
# so notional = margin·lev = RF·available/σ makes a calm coin a meaningful position and a wild one
# appropriately small. Per-position RISK fraction RF (how much a 1σ daily move swings the position, as
# a % of available) is mapped from the target's CONVICTION (their margin / their account), banded
# [RF_MIN, RF_MAX] — a whale's small % is still a real bet (floor), an all-in is bounded (cap). Isolated
# → max loss = margin. Everything anchored to AVAILABLE (account grows → sizes grow; positions open →
# available shrinks → later sizes shrink = self-throttle). σ is regime-aware, see VOL_* + coin_vol table.
RISK_K = 4.0                # MARGIN multiplier only: margin = RF·RISK_K·available (capital committed /
#                             isolated max-loss per position). Leverage is NOT this anymore — see the
#                             two-anchor fat-tail buffer below.
# FAT-TAIL-AWARE leverage (the safety buffer in σ GROWS with the coin's volatility, so a calm coin gets
# high leverage and a wild meme low — fixing "equal σ-buffer ≠ equal liquidation risk" since memes have
# fat tails). Defined by TWO INTUITIVE ANCHORS (UI-tunable, also the dashboard preview inputs):
#   LEV_LOWVOL_X  = target leverage at a BTC-like vol (LEV_SIGMA_LOW)
#   LEV_HIGHVOL_X = target leverage at a meme-like vol (LEV_SIGMA_HIGH)
# from which buffer k(σ)=a+b·σ is back-solved; lev = clip(1/(k·σ), MIN_LEV, MAX_LEV).
LEV_LOWVOL_X = 20.0         # BTC-level (σ≈2.3%/day) target max leverage
LEV_HIGHVOL_X = 2.0         # meme-level (σ≈9%/day) target max leverage (wildest memes ≤ this)
LEV_SIGMA_LOW = 0.023       # reference "calm" daily σ (BTC-like) for the low-vol anchor
LEV_SIGMA_HIGH = 0.09       # reference "wild" daily σ (meme-like) for the high-vol anchor
RF_MIN = 0.005              # min per-position risk fraction (low-conviction / unknown target bet)
RF_MAX = 0.02               # max per-position risk fraction (bounds a target's all-in)
MIN_LEV = 1.0               # leverage floor — ultra-volatile coin → ~spot (isolated 1x ≈ unliquidatable)
MIN_OPEN_MARGIN_PCT = 0.005 # skip a new copy if its formula margin (= rf·RISK_K·available) is below this
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
HARVEST_MIN_ACCT = 5000.0       # real capital (noise guard; we copy by %, not $)
HARVEST_WEEK_VLM_MIN = 100_000.0 # 7d volume floor = genuinely active over the WEEK (not the last 24h —
#                                  that excludes mid-hold position/swing traders, exactly who we want)
HARVEST_MON_ROI_MIN = 0.15      # 30d ROI FLOOR = meaningful return (small capital needs high % returns)
HARVEST_MON_ROI_MAX = 3.0       # anti-lottery CEILING: cut tiny-account high-leverage gamblers
HARVEST_WEEK_ROI_MIN = 0.02     # 7d ROI floor — paired with the 30d floor: the recent week must ALSO be
#                                  earning (blocks "+50% on day 1 then dormant"); the 30d floor stops the
#                                  inverse (one lucky week). Together = "good month AND still on pace".
HARVEST_MAX_TURNOVER = 10.0     # anti-MM: daily turnover (mon_vlm/acct/30) ceiling; >10x/day = market-maker

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

# LOSS-DISCIPLINE demote ("扛单降权"). Measures NOT cutting losses DIRECTLY — never via win rate. The
# score multiplies by 1/(1+K·disc), where disc = 5×(current losing-bag burden: depth×count×duration) +
# 1×(historical forced liquidations). A clean fast-cutter (no open loss, never liquidated) is untouched
# however high its win rate; a wallet sitting on several deep bags for days, or that's been force-closed,
# is demoted. SOFT: sinks the worst toward/below the follow line, never zeroes a profitable wallet. 0 =
# off. Tunable via dashboard (apply_scanner_params pushes it onto config so scan + regate both honor it).
DISP_PENALTY_K = 0.6   # demote strength (0 = disabled; higher = harsher). score *= 1/(1+K·disc)
# REALIZED-asymmetry sub-term of disc — catches "小赚大亏 / 不及时止损" wallets (the twins) WITHOUT
# punishing high win rate per se. Fires only when BOTH: realized-loss RATE is below LOSS_RATE_REF
# (the wallet defers losses) AND |worst realized loss| exceeds TAIL_FREE× the median win.
LOSS_RATE_REF = 0.15   # a wallet realizing losses on ≥15% of trades "cuts normally" → no asym penalty
TAIL_FREE     = 1.5    # worst loss up to this × median win is fine; beyond = asymmetric (小赚大亏)
ASYM_W        = 0.8    # weight of the asymmetry term inside disc
PAIN_MIN_TRADES = 15   # ≥ this many closed trades with ZERO realized losses = extreme deferrer
PAIN_NOLOSS   = 4.0    # loss_pain assigned to a never-realized-a-loss wallet over a large sample

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

# COPY-SIDE STOP — a flat ADVERSE-PRICE cut, our isolated-account tail guard. Cut a copy when price
# runs COPY_STOP_PCT against entry. Calibrated WIDE on purpose (default 18%) from the twins' winner-MAE
# (5m candles): the normal reverting winners — the actual edge — take a median 1.7% / quick heat and
# recover in <1-4h, so an 18% line NEVER touches them. It only governs the deep-bag minority (>10% MAE:
# ~8% of winners), which on an ISOLATED small account is exactly where the damage is — those bags lock
# our margin 12h-to-4-DAYS while risking the 3x liquidation (+33%). The master rides them on $40k cross
# + patience; we can't, so we cap the tail: realize a bounded ~COPY_STOP_PCT×lev margin loss and free
# the capital, instead of bag-holding to liquidation. Paired with the master-leverage cap (which
# already removed the 6x premature-liq bug). UI-tunable (set very high to effectively disable).
COPY_STOP_ENABLE = True
COPY_STOP_PCT    = 0.18     # adverse price move from entry that triggers a cut (≈ this × lev of margin)

# paper-copy simulation
LATENCIES = [0.5, 2.0, 5.0]  # (legacy) latency bands — schema columns; REST signal has one
TAKER_FEE = 0.00045          # detection latency, so all three resolve to the same live-book price
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)
BOOK_HIST_S = max(LATENCIES) + 3  # (legacy) bbo history depth — REST mode prices off current bbo only

DEFAULT_DB = "data/hl.db"
