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
# balance; each copy commits isolated margin = a CONVICTION-WEIGHTED fraction of CURRENT AVAILABLE
# balance, at the master's leverage capped to MAX_LEV. The OPEN size mirrors how much of the target's
# OWN account the target put behind the position (master_margin / target_account), floored + capped:
# a whale's "small %" is still a real bet (floor), a target's all-in is bounded (cap). notional =
# margin * leverage; isolated liquidation (loss = margin). No stop-loss in v1.
INITIAL_BALANCE = 10000.0   # simulated wallet starting equity ($)
OPEN_MIN_PCT = 0.02         # FLOOR: every copy opens at >= this fraction of available (a whale's tiny
#                             %-of-their-account is still a deliberate bet — don't open dust under this)
OPEN_MAX_PCT = 0.05         # CAP: conviction-weighted open tops out here (target's all-in ≠ our all-in)
ADD_MARGIN_PCT = 0.01       # margin on each follow-on ADD (scale-in) = fraction of available
MAX_LEV = 30.0             # cap on the master's per-coin leverage we mirror. The target already
#                            vol-adjusts leverage per coin (40x BTC = low vol, 5x a hot alt), so
#                            mirroring IS volatility-aware for free; this is only a backstop against a
#                            stale read / a target over-levering a dangerous coin. Isolated margin (=
#                            the conviction-weighted % of available) caps our per-position downside.
MAX_ADDS = 2                # follow the master's scale-ins up to this many adds/position (each ADD_MARGIN_PCT)

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
# top-N by SCORE = Quality(RAR × day-consistency) × Survival × Health(current-underwater depth).
SCORE_K = 5.0          # daily-stats confidence: w = active_days/(active_days+K). Low-freq → lean overall ROI
SCORE_GAMMA = 2.0      # day-consistency strictness: consistency = pos_day_ratio^(w·GAMMA). Higher = stricter
UW_TOL = 0.02          # ignore current open underwater below this (fresh/small dips fine)
UW_REF = 0.10          # open-underwater treated as fully dangerous (Health snap → 0 here). Decoupled
#                        from MAX_LEV (the copy cap) on purpose — this is a scoring-shape param.

# paper-copy simulation
LATENCIES = [0.5, 2.0, 5.0]  # (legacy) latency bands — schema columns; REST signal has one
TAKER_FEE = 0.00045          # detection latency, so all three resolve to the same live-book price
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)
BOOK_HIST_S = max(LATENCIES) + 3  # (legacy) bbo history depth — REST mode prices off current bbo only

DEFAULT_DB = "data/hl.db"
