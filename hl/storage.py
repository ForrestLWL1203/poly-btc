"""Single source of truth for the SQLite schema. All persistent data lives here as
structured tables (never raw JSON dumps) so the schema can be extended over time
(add columns/tables for the execution leg later without touching call sites).

One db file (data/hl.db), layered by concern:
  discovery   : leaderboard (raw HL firehose)  ->  profile (full per-wallet analysis,
                all statuses)  ->  watchlist (OUR curated tiny leaderboard, ranked,
                UI-facing, rebuilt each scan)
  control     : target_controls (operator settings: enabled/pinned/note — survive scans)
  diagnostics : scan_runs (one row per scan: counts + duration, for ops/UI history)
  observation : live_fills (raw behaviour), episodes_live (observed round-trips),
                paper_legs (simulated copy outcomes per latency)
"""
import sqlite3
from pathlib import Path

DISCOVERY_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS leaderboard (
    addr          TEXT PRIMARY KEY,
    display_name  TEXT,
    account_value REAL,
    day_pnl REAL,  day_roi REAL,  day_vlm REAL,
    week_pnl REAL, week_roi REAL, week_vlm REAL,
    mon_pnl REAL,  mon_roi REAL,  mon_vlm REAL,
    all_pnl REAL,  all_roi REAL,  all_vlm REAL,
    daily_turnover REAL,
    is_candidate  INTEGER DEFAULT 0,
    fetched_at    TEXT
);
CREATE TABLE IF NOT EXISTS profile (
    addr             TEXT PRIMARY KEY,
    status           TEXT,
    reason           TEXT,
    score            REAL,
    n_fills          INTEGER,
    n_trades         INTEGER,
    window_days      REAL,
    trades_per_day   REAL,
    taker_frac_notl  REAL,
    median_hold_s    REAL,
    win_rate         REAL,
    net_pnl          REAL,
    roi_equity       REAL,
    roi_notional     REAL,
    total_notl       REAL,
    acct_value       REAL,
    perp_frac        REAL,
    gross_pnl        REAL,
    total_fee        REAL,
    n_coins          INTEGER,
    top_coin         TEXT,
    long_frac        REAL,
    max_drawdown     REAL,
    avg_notional     REAL,
    age_days         REAL,
    last_fill_ms     INTEGER,
    lev_proxy        REAL,                -- avg position notional / equity (historical eff. leverage)
    margin_type      TEXT,                -- isolated / cross / mixed / flat (current snapshot)
    cur_leverage     REAL,                -- current account effective leverage (totalNtlPos/equity)
    liq_count        INTEGER DEFAULT 0,   -- # self-liquidation events in window (liquidatedUser==self)
    liq_worst_pct    REAL DEFAULT 0,      -- worst single self-liquidation loss as % of equity (<=0)
    active_days      INTEGER DEFAULT 0,   -- v3: distinct days with a closed episode in the window
    activity_ratio   REAL DEFAULT 0,      -- v3: active_days / lookback (regularity; gate >=0.5)
    median_eps       REAL DEFAULT 0,      -- v3: median episodes per ACTIVE day (true daily frequency)
    pos_day_ratio    REAL DEFAULT 0,      -- v3: fraction of active days that were net-positive
    profit_conc      REAL DEFAULT 0,      -- v3: best single day's share of gross profit (1 = one-lucky-day)
    hold_skew        REAL DEFAULT 0,      -- v3: median hold(losers)/hold(winners) (>1 = 扛单/disposition)
    open_underwater  REAL DEFAULT 0,      -- v3: worst current open position underwater (fraction, <=0)
    first_added      TEXT,
    last_refreshed   TEXT,
    times_seen       INTEGER DEFAULT 0,
    times_active     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS episode (
    addr TEXT, coin TEXT, side TEXT, open_ms INTEGER, close_ms INTEGER,
    hold_s REAL, net_pnl REAL, fee REAL, max_notl REAL, n_fills INTEGER,
    open_px REAL, close_px REAL,
    PRIMARY KEY (addr, coin, open_ms)
);
CREATE INDEX IF NOT EXISTS idx_ep_addr ON episode(addr);
CREATE INDEX IF NOT EXISTS idx_prof_status ON profile(status);

-- OUR curated tiny leaderboard: current active targets, ranked, denormalized for UI.
-- Derived (rebuilt each scan) from profile+leaderboard; single source of truth = profile.
CREATE TABLE IF NOT EXISTS watchlist (
    rank           INTEGER,
    addr           TEXT PRIMARY KEY,
    display_name   TEXT,
    score          REAL,
    roi_equity     REAL,
    mon_roi        REAL,
    net_pnl        REAL,
    acct_value     REAL,
    n_trades       INTEGER,
    trades_per_day REAL,
    taker_frac     REAL,
    median_hold_s  REAL,
    win_rate       REAL,
    max_drawdown   REAL,
    age_days       REAL,
    top_coin       TEXT,
    perp_frac      REAL,
    lev_proxy      REAL,
    margin_type    TEXT,
    cur_leverage   REAL,
    liq_worst_pct  REAL,
    times_active   INTEGER,
    first_added    TEXT,
    last_fill_ms   INTEGER,
    updated_at     TEXT
);

-- Operator controls, set via UI; persist across scans (NOT wiped on watchlist rebuild).
CREATE TABLE IF NOT EXISTS target_controls (
    addr        TEXT PRIMARY KEY,
    enabled     INTEGER DEFAULT 1,   -- observe/copy this target?
    pinned      INTEGER DEFAULT 0,
    note        TEXT,
    updated_at  TEXT
);

-- One row per scan run, for diagnostics + UI history.
CREATE TABLE IF NOT EXISTS scan_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    duration_s  REAL,
    candidates  INTEGER,
    probed_new  INTEGER,
    added       INTEGER,
    retired     INTEGER,
    kept        INTEGER,
    rejected    INTEGER,
    n_active    INTEGER
);
"""

PROFILE_COLS = (
    "addr,status,reason,score,n_fills,n_trades,window_days,trades_per_day,taker_frac_notl,"
    "median_hold_s,win_rate,net_pnl,roi_equity,roi_notional,total_notl,acct_value,perp_frac,"
    "gross_pnl,total_fee,n_coins,top_coin,long_frac,max_drawdown,avg_notional,age_days,"
    "last_fill_ms,lev_proxy,margin_type,cur_leverage,liq_count,liq_worst_pct,"
    "active_days,activity_ratio,median_eps,pos_day_ratio,profit_conc,hold_skew,open_underwater,"
    "first_added,last_refreshed,times_seen,times_active"
)  # 42 columns

OBSERVE_SCHEMA = """
-- A target's TRADE-level fills (aggregateByTime merges an order's slices into one row). Serves as
-- both the tid-dedup table and the target's trade audit. Only the fields we actually use are kept;
-- recv_ms/fee/is_liq/liq_method/hash were dropped as redundant.
CREATE TABLE IF NOT EXISTS live_fills (
    addr TEXT, tid INTEGER, time_ms INTEGER,
    coin TEXT, side TEXT, dir TEXT, px REAL, sz REAL, closed_pnl REAL, crossed INTEGER,
    PRIMARY KEY (addr, tid)
);
CREATE INDEX IF NOT EXISTS idx_lf_addr ON live_fills(addr, time_ms);

-- Our paper account: ONE row. balance = realized equity (starts at initial_balance, += closed
-- PnL); available = balance - sum(margin of open positions); each new copy locks margin_pct of
-- available. Persisted so the simulated wallet survives restarts.
CREATE TABLE IF NOT EXISTS copy_account (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance REAL,
    balance         REAL,
    updated_at      TEXT
);

-- One row per copied position (our mirror of a master round-trip). UI "trades" list. Persisted on
-- OPEN (status=open) and finalized on CLOSE/LIQUIDATION — never memory-only, survives restarts.
-- Real-account model: isolated margin, leverage = min(master's, MAX_LEV), notional = margin*lev,
-- size = notional/entry; liquidation when price crosses liq_px (loss = margin). No stop-loss (v1).
CREATE TABLE IF NOT EXISTS copy_position (
    pos_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    addr TEXT, coin TEXT, side TEXT,
    status         TEXT,                 -- open / closed / gap_closed / liquidated
    master_open_ms INTEGER, master_open_px REAL, master_peak_sz REAL,
    leverage REAL, margin REAL, notional REAL,    -- our sizing (margin = 2% of available at open)
    entry_px REAL, size REAL, rem_size REAL,       -- our fill px, position size (coin), remaining
    liq_px REAL,                                   -- isolated liquidation price (loss = margin)
    realized_pnl REAL DEFAULT 0,                   -- accumulated realized PnL on this position
    add_count INTEGER DEFAULT 0,                   -- follow-on adds taken (capped at MAX_ADDS)
    mae_pct REAL DEFAULT 0, was_liq INTEGER DEFAULT 0, num_actions INTEGER DEFAULT 0,
    opened_at TEXT, closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cp_status ON copy_position(status);
CREATE INDEX IF NOT EXISTS idx_cp_addr ON copy_position(addr);

-- One row per master action on a tracked position (open / add / reduce / close), with
-- full detail + OUR mirrored fill at the primary 2s latency. UI "timeline / drill-down".
CREATE TABLE IF NOT EXISTS copy_action (
    act_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pos_id INTEGER, addr TEXT, coin TEXT, ts INTEGER, recv_ms INTEGER,
    action         TEXT,                 -- open / add / reduce / close
    maker          INTEGER,              -- 1 = master's fill was a resting-limit (maker) fill
    master_oid     INTEGER,              -- master's order id -> JOIN target_orders for placed px/sz
    master_px REAL, master_sz_delta REAL, master_pos_after REAL,
    our_qty_delta REAL, our_px REAL, realized_pnl REAL, slippage_bps REAL
);
CREATE INDEX IF NOT EXISTS idx_ca_oid ON copy_action(master_oid);
CREATE INDEX IF NOT EXISTS idx_ca_pos ON copy_action(pos_id);

-- Target wallets' RESTING orders (limit ladders + TP/SL triggers), captured by a REST
-- poller of frontendOpenOrders (zero WS-slot cost). Reveals their intentions BEFORE
-- execution → maker-copy candidates + their take-profit/stop levels. One row per (addr,
-- oid); status flips to 'gone' when it leaves the book (filled or cancelled).
CREATE TABLE IF NOT EXISTS target_orders (
    addr        TEXT, oid INTEGER,
    coin        TEXT, side TEXT,
    order_type  TEXT,                 -- Limit / Take Profit Market / Stop Market / ...
    limit_px    REAL, trigger_px REAL, sz REAL,
    reduce_only INTEGER, is_trigger INTEGER,
    status      TEXT,                 -- open / gone
    first_seen  TEXT, last_seen TEXT,
    PRIMARY KEY (addr, oid)
);
CREATE INDEX IF NOT EXISTS idx_to_addr ON target_orders(addr, status);

-- Per-wallet REST cursor for the periodic backfill safety net. WS is treated as a fast-but-
-- lossy stream (it can silently drop messages); every few minutes we REST-reconcile each
-- monitored wallet's fills from cursor - small overlap (dedup by tid) so nothing is missed.
-- Persisted so it survives process restarts too.
CREATE TABLE IF NOT EXISTS wallet_cursor (
    addr         TEXT PRIMARY KEY,
    last_fill_ms INTEGER,
    updated_at   TEXT
);
"""


def connect(path: str, *schemas: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)  # used across the scanner's
    db.execute("PRAGMA journal_mode=WAL")                            # worker threads (writes are
    db.execute("PRAGMA busy_timeout=30000")                          # serialized by a lock)
    for s in schemas:
        db.executescript(s)
    return db
