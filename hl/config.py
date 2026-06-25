"""Shared constants — endpoints, hard limits, sim parameters. No logic here."""

# Hyperliquid endpoints
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
UA = {"User-Agent": "hl-copytrade/0.3", "Accept": "application/json", "Content-Type": "application/json"}

# numeric
FLAT = 1e-6                 # |position| below this (coin units) counts as flat
MIN_POST_INTERVAL = 0.8     # global REST pacing between POSTs. HL's /info limit is weight-based
#                             (~1200 weight/min) and fill queries weigh ~20 => ~60 req/min; 0.8s
#                             (75/min) stays under it. The 429 backoff self-regulates any overshoot,
#                             and the thread-safe pacer means more workers fill RTT, not raise rate.

# HL WS hard limits (per IP, official): the binding one is unique users.
MAX_WS_USERS = 10           # max unique users across user-specific subscriptions (WS only)

# Copy engine: SIGNAL via REST poll (per-wallet userFills — REST has no 10-user cap, so we can
# watch the whole watchlist); PRICING via WS bbo (per-COIN top-of-book — NOT subject to the
# 10-user cap, only the 1000-sub cap, and we touch only a few dozen coins). Targets are low-freq
# long-hold, so a few-seconds poll latency is fine; we execute against the live book at detection.
MAX_TARGETS = 60            # max watchlist wallets the signal poller covers (bounds REST load)
WATCHLIST_RELOAD_S = 300   # re-read the watchlist table this often (track rolling discovery)
POLL_OVERLAP_MS = 5000     # re-fetch this far behind each wallet's cursor (tid-dedup absorbs it)
MAX_BACKFILL_S = 3600      # never look back further than this on a poll (forward-only, bounds stale cursors)
LIVE_FILLS_RETENTION_DAYS = 7  # prune live_fills older than this (tid-dedup only needs ~MAX_BACKFILL_S;
#                                the rest is audit) — keeps the only unbounded-on-disk table bounded

# Copy account & sizing (UI-tunable). Real-account paper model: a simulated wallet with an initial
# balance; each copy commits MARGIN_PCT of CURRENT AVAILABLE balance as isolated margin, at the
# master's leverage capped to MAX_LEV. notional = margin * leverage; liquidation when price crosses
# the isolated liq level (loss = margin). No stop-loss in v1 (the 2% margin is the per-trade max loss).
INITIAL_BALANCE = 10000.0   # simulated wallet starting equity ($)
MARGIN_PCT = 0.02           # margin per copy (open AND each follow-on add) = fraction of available
MAX_LEV = 10.0              # cap on the master's leverage we mirror
MAX_ADDS = 3                # follow the master's scale-ins up to this many adds/position (avg down)

# Copy-strategy knobs (UI-tunable; no hardcoded magic). None = disabled.
# Chase guard: on a fast spike the master eats the book with size and our taker fill lands worse.
# If our entry price is more than this % worse than the master's, SKIP that open (don't chase).
# Applies to taker opens only (maker rests passively; exits are never blocked — always follow out).
MAX_ENTRY_CHASE_PCT = None    # e.g. 0.5 => skip a taker open whose entry is >0.5% worse than master

# paper-copy simulation
LATENCIES = [0.5, 2.0, 5.0]  # (legacy) latency bands — schema columns; REST signal has one
TAKER_FEE = 0.00045          # detection latency, so all three resolve to the same live-book price
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)
BOOK_HIST_S = max(LATENCIES) + 3  # (legacy) bbo history depth — REST mode prices off current bbo only

DEFAULT_DB = "data/hl.db"
