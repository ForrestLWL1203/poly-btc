-- BTC 5min wallet-profiling collector — schema.
-- Goal: find consistently profitable two-sided (pair-arb) wallets.
-- Centerpiece is pair-cost, not spot momentum. Three tables:
--   windows        one row per 5min window, finalised at settlement
--   wallet_window  per (window, wallet) aggregate — the unbiased discovery substrate
--   trades         per-fill detail WITH book context, kept only for two-sided wallets

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS windows (
    slug          TEXT PRIMARY KEY,
    condition_id  TEXT NOT NULL,
    up_token      TEXT NOT NULL,
    down_token    TEXT NOT NULL,
    start_epoch   INTEGER NOT NULL,
    end_epoch     INTEGER NOT NULL,
    open_price    REAL,
    close_price   REAL,
    winning_side  TEXT,            -- 'UP' | 'DOWN' (close>open => UP)
    settled       INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    settled_at    TEXT
);

-- Computed ourselves from the unbiased market trade feed — never from any
-- per-wallet positions endpoint. One row per wallet per window we observed.
CREATE TABLE IF NOT EXISTS wallet_window (
    window_slug   TEXT NOT NULL,
    wallet        TEXT NOT NULL,
    name          TEXT,
    n_trades      INTEGER NOT NULL DEFAULT 0,
    up_buys       INTEGER NOT NULL DEFAULT 0,
    down_buys     INTEGER NOT NULL DEFAULT 0,
    up_sells      INTEGER NOT NULL DEFAULT 0,
    down_sells    INTEGER NOT NULL DEFAULT 0,
    -- cash legs (USDC)
    up_buy_usdc   REAL NOT NULL DEFAULT 0,
    down_buy_usdc REAL NOT NULL DEFAULT 0,
    up_sell_usdc  REAL NOT NULL DEFAULT 0,
    down_sell_usdc REAL NOT NULL DEFAULT 0,
    -- net shares per outcome (BUY - SELL); negative => off-book acquisition
    up_shares     REAL NOT NULL DEFAULT 0,
    down_shares   REAL NOT NULL DEFAULT 0,
    -- pair-arb core: matched pair count and its mean total cost vs $1
    pair_shares   REAL,            -- min(up_shares, down_shares) — matched pairs
    pair_cost     REAL,            -- mean (up_vwap + down_vwap) over matched pairs
    dual          INTEGER NOT NULL DEFAULT 0,   -- bought BOTH sides
    -- settlement-derived
    realized_pnl  REAL,
    incomplete    INTEGER NOT NULL DEFAULT 0,   -- net shares went negative => PnL untrustworthy
    first_ts      INTEGER,
    last_ts       INTEGER,
    PRIMARY KEY (window_slug, wallet),
    FOREIGN KEY (window_slug) REFERENCES windows(slug)
);

CREATE INDEX IF NOT EXISTS idx_ww_wallet ON wallet_window(wallet);
CREATE INDEX IF NOT EXISTS idx_ww_dual ON wallet_window(dual, realized_pnl);

-- Per-fill detail, persisted only for two-sided wallets (the targets), with the
-- book reconstructed as of the fill's exchange_ts. Spot columns are an optional
-- secondary signal (only meaningful if a wallet profits without pair_cost<$1).
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_key       TEXT UNIQUE,
    window_slug    TEXT NOT NULL,
    condition_id   TEXT NOT NULL,
    wallet         TEXT NOT NULL,
    name           TEXT,
    side           TEXT,           -- BUY | SELL
    outcome        TEXT,           -- Up | Down
    token          TEXT,
    price          REAL,
    size           REAL,
    usdc           REAL,
    exchange_ts    INTEGER,
    observed_at    TEXT,
    window_age_sec        REAL,
    window_remaining_sec  REAL,
    -- top-of-book @ exchange_ts (the wallet's execution context: maker/taker, spread)
    up_bid         REAL,
    up_ask         REAL,
    down_bid       REAL,
    down_ask       REAL,
    book_lag_sec   REAL,
    -- optional secondary spot context @ exchange_ts
    spot           REAL,
    spot_age_sec   REAL,
    ret_1s_bps     REAL,
    ret_3s_bps     REAL,
    ret_5s_bps     REAL,
    ret_10s_bps    REAL,
    FOREIGN KEY (window_slug) REFERENCES windows(slug)
);

CREATE INDEX IF NOT EXISTS idx_trades_wallet_window ON trades(wallet, window_slug);
