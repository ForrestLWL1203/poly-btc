"""Shared constants — endpoints, hard limits, sim parameters. No logic here."""

# Hyperliquid endpoints
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
UA = {"User-Agent": "hl-copytrade/0.3", "Accept": "application/json", "Content-Type": "application/json"}

# numeric
FLAT = 1e-6                 # |position| below this (coin units) counts as flat
MIN_POST_INTERVAL = 0.16    # global REST pacing between POSTs (avoid 429)

# HL WS hard limits (per IP, official): the binding one is unique users.
MAX_WS_USERS = 10           # max unique users across user-specific subscriptions

# paper-copy simulation
LATENCIES = [0.5, 2.0, 5.0]  # copy-latency sensitivity bands (seconds)
TAKER_FEE = 0.00045          # our taker fee per side (~4.5 bps); round-trip ~9 bps
NOTIONAL = 1000.0            # fixed paper notional per copied trade ($)
BOOK_HIST_S = max(LATENCIES) + 3  # seconds of bbo history to retain per coin

DEFAULT_DB = "data/hl.db"
