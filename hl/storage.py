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
    max_adds_per_ep    INTEGER DEFAULT 0, -- GRID signature: most scale-in ORDERS in a single round-trip
    median_adds_per_ep INTEGER DEFAULT 0, -- typical scale-ins/round-trip (swing 0-few, grid dozens)
    worst_loss_pct   REAL DEFAULT 0,      -- loss discipline: worst single round-trip loss / acct (<=0)
    market_type      TEXT,                -- crypto / stock / mixed (by traded-notional crypto vs xyz: split)
    crypto_frac      REAL DEFAULT 1,      -- share of traded notional on crypto perps (1=pure crypto, 0=pure stock)
    tp_move_pct      REAL DEFAULT 0,      -- take-profit signature: median favorable price move on wins (copy-stop base)
    roi_total        REAL DEFAULT 0,      -- v4: (realized net_pnl + current unrealized) / acct — the real performance
    open_unrealized  REAL DEFAULT 0,      -- v4: total current unrealized PnL across live positions ($, signed)
    open_loss_frac   REAL DEFAULT 0,      -- v4: total UNDERWATER unrealized / acct (<=0; 扛单 bag burden)
    open_win_frac    REAL DEFAULT 0,      -- v4: total WINNING unrealized / acct (>=0; trend-trader value)
    bag_count        INTEGER DEFAULT 0,   -- v4: # of currently-underwater positions
    max_bag_days     REAL DEFAULT 0,      -- v4: longest-held underwater position (days)
    max_win_days     REAL DEFAULT 0,      -- v4: longest-held winning position (days)
    hedge_ratio      REAL DEFAULT 0,      -- v4: frac of perp-short notional offset by spot long (spot-hedge)
    loss_pain        REAL DEFAULT 0,      -- v4: |worst realized loss| / median win (小赚大亏 / no-stop signal)
    net_7d           REAL,                -- v6: realized net over last 7d (full-history slice; multi-window)
    net_14d          REAL,                -- v6: realized net over last 14d
    net_30d          REAL,                -- v6: realized net over last 30d (gate: >0 = not cooling off)
    net_life         REAL,                -- v6: realized net over FULL history (gate: >0 = long-term profitable)
    life_trades      INTEGER DEFAULT 0,   -- v6: total closed round-trips in full history (evidence depth)
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
    market_type    TEXT,                 -- crypto / stock / mixed (denormalized from profile)
    tp_move_pct    REAL DEFAULT 0,       -- take-profit signature (median favorable move on wins); copy-stop base
    roi_total      REAL DEFAULT 0,       -- realized+unrealized roi (denormalized for the UI)
    open_loss_frac REAL DEFAULT 0,       -- current 扛单 bag burden (denormalized)
    open_win_frac  REAL DEFAULT 0,       -- current trend value / 浮赢 (denormalized)
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

-- Follow-status history: last time each wallet was AT/ABOVE the follow line. Updated each scan/regate
-- for the currently-followed set; a wallet that drops below the line keeps its old timestamp, so the UI
-- can show "was followed, recently dropped". A recovered wallet climbing back re-stamps and leaves the list.
CREATE TABLE IF NOT EXISTS follow_history (
    addr                TEXT PRIMARY KEY,
    last_followed_at    TEXT,
    last_followed_score REAL
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
    "max_adds_per_ep,median_adds_per_ep,worst_loss_pct,market_type,crypto_frac,tp_move_pct,"
    "roi_total,open_unrealized,open_loss_frac,open_win_frac,bag_count,max_bag_days,max_win_days,hedge_ratio,loss_pain,"
    "net_7d,net_14d,net_30d,net_life,life_trades,"
    "first_added,last_refreshed,times_seen,times_active"
)  # 62 columns

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

-- Per-coin realized volatility for risk-targeted sizing (one row/coin), refreshed periodically off
-- the signal hot path. sigma = max(sigma_fast, sigma_slow) — regime-aware (de-risk fast, re-risk
-- slow). The sizing code reads `sigma`; fast/slow/n are kept for inspection + tuning. n=0 + null
-- fast/slow means the fallback σ was used (candles unavailable).
CREATE TABLE IF NOT EXISTS coin_vol (
    coin       TEXT PRIMARY KEY,
    sigma      REAL,              -- used for sizing = max(fast, slow), daily realized vol
    sigma_fast REAL, sigma_slow REAL,
    n          INTEGER,           -- daily candles used
    updated_at TEXT
);

-- Our paper account: ONE row. balance = realized equity (starts at initial_balance, += closed
-- PnL); available = balance - sum(margin of open positions); each new copy locks margin_pct of
-- available. Persisted so the simulated wallet survives restarts.
CREATE TABLE IF NOT EXISTS copy_account (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance REAL,
    balance         REAL,
    updated_at      TEXT
);

-- Periodic account snapshot (one row per heartbeat) — the DASHBOARD time-series. Everything the
-- overview cards/charts need, pre-computed so the UI just reads rows (equity curve, ROI, win rate,
-- hedge ratio = net/gross, fee drag). Append-only; prune old rows later if it grows.
CREATE TABLE IF NOT EXISTS account_stats (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT,
    balance          REAL,    -- realized equity (= copy_account.balance)
    unrealized_pnl   REAL,    -- mark-to-market of open positions
    equity           REAL,    -- balance + unrealized
    realized_pnl_cum REAL,    -- balance - initial_balance
    roi              REAL,    -- equity / initial_balance - 1
    open_n           INTEGER,
    closed_n         INTEGER,
    win_rate         REAL,    -- fraction of closed positions with realized_pnl > 0
    locked_margin    REAL,    -- margin tied up in open positions
    available        REAL,    -- balance - locked_margin
    gross_notional   REAL,    -- sum of |notional| of open positions
    net_notional     REAL,    -- long_notional - short_notional (hedge/direction)
    fees_cum         REAL     -- cumulative est. taker fees across all copy actions
);
CREATE INDEX IF NOT EXISTS idx_stats_ts ON account_stats(ts);

-- One row per copied position (our mirror of a master round-trip). UI "trades" list. Persisted on
-- OPEN (status=open) and finalized on CLOSE/LIQUIDATION — never memory-only, survives restarts.
-- Real-account model: isolated margin, leverage = min(master's, MAX_LEV), notional = margin*lev,
-- size = notional/entry; liquidation when price crosses liq_px (loss = margin). No stop-loss (v1).
CREATE TABLE IF NOT EXISTS copy_position (
    pos_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    addr TEXT, coin TEXT, side TEXT,
    status         TEXT,                 -- open / closed / gap_closed / liquidated
    master_open_ms INTEGER, master_open_px REAL, master_peak_sz REAL,
    master_leverage REAL, master_margin REAL,     -- target's leverage + margin captured AT OPEN
    leverage REAL, margin REAL, notional REAL,    -- our sizing (margin = 2% of available at open)
    entry_px REAL, size REAL, rem_size REAL,       -- our fill px, position size (coin), remaining
    liq_px REAL,                                   -- isolated liquidation price (loss = margin)
    stop_px REAL,                                  -- copy-side stop price (target-TP-relative); cut before liq
    realized_pnl REAL DEFAULT 0,                   -- accumulated realized PnL on this position
    add_count INTEGER DEFAULT 0,                   -- follow-on adds taken (capped at MAX_ADDS)
    mae_pct REAL DEFAULT 0, was_liq INTEGER DEFAULT 0, was_stopped INTEGER DEFAULT 0, num_actions INTEGER DEFAULT 0,
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

-- ===== Dashboard layer (control plane) =====
-- The dashboard NEVER writes business tables directly. All writes go here as commands consumed by
-- Observer/Scanner (single-writer invariant). Read side: process_status / scan_progress / params.

-- Command channel: the ONLY way the dashboard mutates trading state. Observer/Scanner poll this,
-- execute, and flip status. owner+TTL lets a consumer self-heal a stuck flag if the issuer dies.
CREATE TABLE IF NOT EXISTS commands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT,                 -- pause|resume|close_position|close_all|wallet_toggle|rescan|patch_params
    payload_json    TEXT,
    idempotency_key TEXT UNIQUE,          -- client-supplied dedup key (optional)
    owner           TEXT,                 -- issuing dashboard instance
    status          TEXT DEFAULT 'pending', -- pending|acked|done|failed
    result_json     TEXT,
    error           TEXT,
    created_at      TEXT, acked_at TEXT, done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmd_status ON commands(status);

-- Liveness + state machine for the two background processes. Each upserts its own row per heartbeat;
-- a stale heartbeat_at (vs now) signals a dead process for self-heal / UI "stale" badge.
CREATE TABLE IF NOT EXISTS process_status (
    name          TEXT PRIMARY KEY,       -- 'observer' | 'scanner'
    state         TEXT,                   -- observer: running|pausing|paused|resuming ; scanner: idle|scanning
    pid           INTEGER,
    heartbeat_at  TEXT,
    detail_json   TEXT
);

-- Live progress of a full rescan (single row, id=1). Scanner updates per stage; the UI full-screen
-- mask reads it. elapsed/progressPct are derived by the API from started_at + scanned/total.
CREATE TABLE IF NOT EXISTS scan_progress (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    state              TEXT,              -- idle|scanning
    started_at         TEXT,
    stage              TEXT,              -- scan_leaderboard|fetch_history|score_filter|rebuild_watchlist|persist
    candidates_scanned INTEGER DEFAULT 0,
    candidates_total   INTEGER DEFAULT 0,
    eta_sec            INTEGER,
    manual             INTEGER DEFAULT 0,  -- 1 = dashboard-triggered (lock UI); 0 = 24h auto (silent bg)
    updated_at         TEXT
);

-- UI-tunable strategy parameters. Seeded from code defaults (hl/params.py); the operator edits via
-- the dashboard; Observer/Scanner read their category at run time (replacing config constants / CLI
-- args). value is stored as TEXT and parsed by `type`. category: scanner(rescan) | follow(immediate).
CREATE TABLE IF NOT EXISTS params (
    key           TEXT PRIMARY KEY,
    value         TEXT,                   -- parsed per `type`; NULL allowed for nullable
    category      TEXT,                   -- scanner | follow
    level         TEXT,                   -- green|yellow|blue|black
    type          TEXT,                   -- usd|pct|x|int|float|nullable|bool|display
    effect        TEXT,                   -- rescan | immediate
    default_value TEXT,
    updated_at    TEXT
);
"""


# Non-destructive column adds for EXISTING DBs (CREATE IF NOT EXISTS won't add columns to a table that
# already exists). Idempotent: on a fresh DB the column is already in the CREATE → ALTER errors → ignored.
_MIGRATIONS = (
    "ALTER TABLE profile ADD COLUMN market_type TEXT",
    "ALTER TABLE profile ADD COLUMN crypto_frac REAL DEFAULT 1",
    "ALTER TABLE watchlist ADD COLUMN market_type TEXT",
    # Dashboard: per-position realtime fields (Observer persists each heartbeat / at open) so the
    # read-only API can serve mark/upnl/lag without its own live book.
    "ALTER TABLE copy_position ADD COLUMN mark_px REAL",
    "ALTER TABLE copy_position ADD COLUMN unrealized_pnl REAL",
    "ALTER TABLE copy_position ADD COLUMN open_lag_sec REAL",
    # Dashboard: denormalized onto watchlist by the scanner rebuild (API COALESCEs with profile until
    # the next scan repopulates these).
    "ALTER TABLE watchlist ADD COLUMN worst_single_loss_pct REAL",
    "ALTER TABLE watchlist ADD COLUMN grid REAL",
    # 扛单 copy-side stop + take-profit signature (non-destructive on existing DBs).
    "ALTER TABLE profile ADD COLUMN tp_move_pct REAL DEFAULT 0",
    "ALTER TABLE watchlist ADD COLUMN tp_move_pct REAL DEFAULT 0",
    "ALTER TABLE copy_position ADD COLUMN stop_px REAL",
    "ALTER TABLE copy_position ADD COLUMN was_stopped INTEGER DEFAULT 0",
    # v4 open-position character (realized+unrealized perf, trend value, 扛单 bag burden).
    "ALTER TABLE profile ADD COLUMN roi_total REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN open_unrealized REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN open_loss_frac REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN open_win_frac REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN bag_count INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN max_bag_days REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN max_win_days REAL DEFAULT 0",
    "ALTER TABLE watchlist ADD COLUMN roi_total REAL DEFAULT 0",
    "ALTER TABLE watchlist ADD COLUMN open_loss_frac REAL DEFAULT 0",
    "ALTER TABLE watchlist ADD COLUMN open_win_frac REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN hedge_ratio REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN loss_pain REAL DEFAULT 0",
    # v6 multi-window / lifetime realized nets (full-history slice; discipline gates net_30d>0 & net_life>0).
    "ALTER TABLE profile ADD COLUMN net_7d REAL",
    "ALTER TABLE profile ADD COLUMN net_14d REAL",
    "ALTER TABLE profile ADD COLUMN net_30d REAL",
    "ALTER TABLE profile ADD COLUMN net_life REAL",
    "ALTER TABLE profile ADD COLUMN life_trades INTEGER DEFAULT 0",
)


def connect(path: str, *schemas: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)  # used across the scanner's
    db.execute("PRAGMA journal_mode=WAL")                            # worker threads (writes are
    db.execute("PRAGMA busy_timeout=30000")                          # serialized by a lock)
    for s in schemas:
        db.executescript(s)
    for stmt in _MIGRATIONS:
        try:
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass                          # column already exists (fresh DB or prior run) — fine
    db.commit()
    return db
