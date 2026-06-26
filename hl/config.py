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
RISK_K = 4.0                # liquidation buffer in daily-σ; also margin = RF·RISK_K·available
RF_MIN = 0.005              # min per-position risk fraction (low-conviction / unknown target bet)
RF_MAX = 0.02               # max per-position risk fraction (bounds a target's all-in)
MIN_LEV = 1.0               # leverage floor — ultra-volatile coin → ~spot (isolated 1x ≈ unliquidatable)
MAX_LEV = 30.0              # leverage BACKSTOP only (σ<0.83%/day to bind); guards a bad/stale σ estimate

# Per-coin volatility (regime-aware) for the sizing above. A coin calm-then-erupting must NOT keep its
# old low σ and get over-levered into a blow-up — so we use TWO horizons and take the MAX (de-risk fast
# when vol rises, re-risk slowly when it falls). Refreshed periodically into the coin_vol TABLE off the
# signal hot path; sizing just reads the row. σ_used = max(σ_fast, σ_slow), both daily realized vol.
VOL_FAST_DAYS = 7           # recent window — catches a fresh volatility regime within ~a day
VOL_SLOW_DAYS = 30          # long baseline — stable; the floor we hold until calm is sustained
VOL_MIN_SAMPLES = 5         # need this many daily candles, else fall back
VOL_REFRESH_S = 3600        # re-fetch each tracked coin's σ at most this often (1h) — vol drifts slowly
VOL_FALLBACK_SIGMA = 0.10   # σ when candles unavailable (new/illiquid coin) → low lev, small notional

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

# paper-copy simulation
LATENCIES = [0.5, 2.0, 5.0]  # (legacy) latency bands — schema columns; REST signal has one
TAKER_FEE = 0.00045          # detection latency, so all three resolve to the same live-book price
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)
BOOK_HIST_S = max(LATENCIES) + 3  # (legacy) bbo history depth — REST mode prices off current bbo only

DEFAULT_DB = "data/hl.db"
