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
# balance; each copy commits MARGIN_PCT of CURRENT AVAILABLE balance as isolated margin, at the
# master's leverage capped to MAX_LEV. notional = margin * leverage; liquidation when price crosses
# the isolated liq level (loss = margin). No stop-loss in v1 (the 2% margin is the per-trade max loss).
INITIAL_BALANCE = 10000.0   # simulated wallet starting equity ($)
MARGIN_PCT = 0.02           # margin on the OPEN of a copy = fraction of available balance
ADD_MARGIN_PCT = 0.01       # margin on each follow-on ADD (scale-in) = fraction of available (smaller
#                             than the open so averaging-down doesn't bloat a single position)
MAX_LEV = 10.0              # cap on the master's leverage we mirror
MAX_ADDS = 3                # follow the master's scale-ins up to this many adds/position (avg down)

# Copy-strategy knobs (UI-tunable; no hardcoded magic). None = disabled.
# Chase guard: on a fast spike the master eats the book with size and our taker fill lands worse.
# If our entry price is more than this % worse than the master's, SKIP that open (don't chase).
# Applies to taker opens only (maker rests passively; exits are never blocked — always follow out).
MAX_ENTRY_CHASE_PCT = None    # e.g. 0.5 => skip a taker open whose entry is >0.5% worse than master

# Stage-1 leaderboard prefilter (UI-tunable). The leaderboard gives each wallet's perf across
# 24h/7d/30d/allTime in ONE bulk fetch — so we discriminate on THREE windows BEFORE any per-wallet
# API profiling: 24h volume = active NOW, 7d/30d returns = recent + stable, allTime = lifetime track
# record. ~hundreds survive → profile becomes a small confirmation step, not a multi-thousand sweep.
# (Volume is leveraged notional, so the floors are large.) The expensive REALIZED/risk judgment —
# realized PnL, perp-copyability, hold-skew, self-liquidation, current underwater — stays in the
# per-wallet profile gates()+score(); this stage only buys a concentrated, active, stable seed set.
HARVEST_MIN_ACCT = 5000.0       # real capital (noise guard; we copy by %, not $)
HARVEST_VOL24_MIN = 200_000.0   # 24h volume floor (leveraged notional) = genuinely active today
HARVEST_WEEK_ROI_MIN = 0.10     # 7d ROI floor = meaningful recent return (not "+1%/week")
HARVEST_MON_ROI_MAX = 3.0       # anti-lottery: cut tiny-account high-leverage gamblers (absurd 30d ROI)
HARVEST_MAX_TURNOVER = 10.0     # anti-MM: daily turnover (mon_vlm/acct/30) ceiling; >10x/day = market-maker

# v3 score shape (interpretable, UI-tunable — NOT arbitrary quality cutoffs). The watchlist is
# top-N by SCORE = Quality × Survival × FreqFit × Health; these tune the curves, not hard gates.
SCORE_K = 5.0          # daily-stats confidence: w = active_days/(active_days+K). Low-freq → lean overall ROI
SCORE_GAMMA = 2.0      # day-consistency strictness: consistency = pos_day_ratio^(w·GAMMA). Higher = stricter
FREQ_STAR = 8.0        # frequency sweet-spot (median episodes/day) where FreqFit saturates (capital efficiency)
UW_TOL = 0.02          # ignore current open underwater below this (fresh/small dips fine; vs liq_dist=1/MAX_LEV)
SKEW_SPAN = 2.0        # disposition penalty span: hold_skew (loser/winner hold) from 1→1+SPAN drives Health→floor
CONC_TOL = 0.40        # profit-concentration tolerance: a single day > this share of gross profit gets penalised
HEALTH_FLOOR = 0.20    # min for the disposition/concentration factors (a soft penalty, never a hard zero)

# paper-copy simulation
LATENCIES = [0.5, 2.0, 5.0]  # (legacy) latency bands — schema columns; REST signal has one
TAKER_FEE = 0.00045          # detection latency, so all three resolve to the same live-book price
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)
BOOK_HIST_S = max(LATENCIES) + 3  # (legacy) bbo history depth — REST mode prices off current bbo only

DEFAULT_DB = "data/hl.db"
