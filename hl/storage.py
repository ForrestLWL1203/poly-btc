"""Single source of truth for the SQLite schema. All persistent data lives here as
structured tables (never raw JSON dumps) so the schema can be extended over time
(add columns/tables for the execution leg later without touching call sites).

One db file (data/hl.db), layered by concern:
  discovery   : leaderboard (raw HL firehose)  ->  profile (full per-wallet analysis,
                all statuses)  ->  watchlist (OUR curated tiny leaderboard, ranked,
                UI-facing, rebuilt each scan)
  control     : target_controls (operator settings: enabled/pinned/note — survive scans)
  diagnostics : scan_runs (one row per scan: counts + duration, for ops/UI history)
  execution   : live_fills, copy_account, copy_position and copy_action
"""
import sqlite3
import re
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
    fetched_at    TEXT,
    generation    TEXT
);
CREATE INDEX IF NOT EXISTS idx_leaderboard_candidate_mon_roi ON leaderboard(is_candidate, mon_roi DESC, addr);
CREATE INDEX IF NOT EXISTS idx_leaderboard_candidate_week_roi ON leaderboard(is_candidate, week_roi DESC, addr);
CREATE INDEX IF NOT EXISTS idx_leaderboard_candidate_mon_pnl ON leaderboard(is_candidate, mon_pnl DESC, addr);

-- Atomic discovery generations.  Network results are first written to leaderboard_staging, validated,
-- profiled and selected; only a complete generation becomes current.  Keeping every generation row makes
-- incomplete/failed scans auditable without exposing a half-built selection to the Observer.
CREATE TABLE IF NOT EXISTS scan_generation (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    generation                    TEXT NOT NULL UNIQUE,
    source                        TEXT,
    status                        TEXT NOT NULL DEFAULT 'staging',
    complete                      INTEGER NOT NULL DEFAULT 0,
    publishable                   INTEGER NOT NULL DEFAULT 0,
    is_current                    INTEGER NOT NULL DEFAULT 0,
    started_at                    TEXT NOT NULL,
    leaderboard_fetched_at        TEXT,
    ready_at                      TEXT,
    published_at                  TEXT,
    failed_at                     TEXT,
    previous_published_generation TEXT,
    leaderboard_rows              INTEGER DEFAULT 0,
    leaderboard_unique_rows       INTEGER DEFAULT 0,
    leaderboard_complete_rows     INTEGER DEFAULT 0,
    leaderboard_completeness      REAL DEFAULT 0,
    leaderboard_valid             INTEGER DEFAULT 0,
    profile_total                 INTEGER DEFAULT 0,
    profile_valid                 INTEGER DEFAULT 0,
    profile_deferred              INTEGER DEFAULT 0,
    profile_rejected              INTEGER DEFAULT 0,
    profile_complete              INTEGER DEFAULT 0,
    workset_mode                  TEXT,
    fill_mode                     TEXT,
    full_refresh_shard            INTEGER,
    workset_n                     INTEGER DEFAULT 0,
    deferred_n                    INTEGER DEFAULT 0,
    metrics_json                  TEXT,
    error                         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_generation_current
    ON scan_generation(is_current) WHERE is_current=1;
CREATE INDEX IF NOT EXISTS idx_scan_generation_status_published
    ON scan_generation(status, published_at DESC, id DESC);

-- Immutable market inputs used by one scanner generation.  The Observer intentionally continues to use
-- the latest ``coin_vol`` cache; qualification, portfolio formation and tuning must instead read these
-- frozen rows so a long scan cannot mix volatility/liquidity regimes.
CREATE TABLE IF NOT EXISTS generation_market_manifest (
    generation       TEXT PRIMARY KEY,
    asof_ms          INTEGER NOT NULL,
    context_hash     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'building',
    snapshot_hash    TEXT,
    created_at       TEXT NOT NULL,
    sealed_at        TEXT
);
CREATE TABLE IF NOT EXISTS generation_market_snapshot (
    generation       TEXT NOT NULL,
    coin             TEXT NOT NULL,
    asof_ms          INTEGER NOT NULL,
    sigma            REAL NOT NULL,
    sigma_fast       REAL,
    sigma_slow       REAL,
    sigma_n          INTEGER NOT NULL DEFAULT 0,
    sigma_source     TEXT NOT NULL,
    day_ntl_vlm      REAL,
    open_interest    REAL,
    mark_px          REAL,
    oi_notional      REAL,
    max_leverage     REAL,
    context_at       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (generation, coin)
);
CREATE INDEX IF NOT EXISTS idx_generation_market_snapshot_generation
    ON generation_market_snapshot(generation, coin);

CREATE TABLE IF NOT EXISTS leaderboard_staging (
    generation    TEXT NOT NULL,
    addr          TEXT NOT NULL,
    display_name  TEXT,
    account_value REAL,
    day_pnl REAL,  day_roi REAL,  day_vlm REAL,
    week_pnl REAL, week_roi REAL, week_vlm REAL,
    mon_pnl REAL,  mon_roi REAL,  mon_vlm REAL,
    all_pnl REAL,  all_roi REAL,  all_vlm REAL,
    daily_turnover REAL,
    is_candidate  INTEGER DEFAULT 0,
    fetched_at    TEXT,
    PRIMARY KEY (generation, addr)
);
CREATE INDEX IF NOT EXISTS idx_leaderboard_staging_generation_candidate
    ON leaderboard_staging(generation, is_candidate, mon_roi DESC, addr);

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
    payoff_ratio     REAL DEFAULT 0,      -- 平均盈利回合 / 平均亏损回合（无亏封顶 999）
    win_pt           REAL DEFAULT 0,      -- 赢单每笔中位名义收益% (审计指标; 不再作为 raw score 乘法降分)
    max_concurrent   INTEGER DEFAULT 0,   -- 峰值同时持仓数 (>阈值 = 组合客,我们装不下 → too_many_concurrent)
    net_pnl          REAL,
    roi_equity       REAL,
    total_notl       REAL,
    acct_value       REAL,
    perp_frac        REAL,
    top_coin         TEXT,
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
    open_underwater  REAL DEFAULT 0,      -- v3: worst material current open position underwater (fraction, <=0)
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
    pf_week_pnl      REAL,                -- v7 portfolio (NET of fees, deposit-adjusted): 7d account PnL
    pf_week_vlm      REAL,                -- v7: 7d traded volume ($)
    pf_mon_pnl       REAL,                -- v7: 30d account PnL (net)
    pf_mon_vlm       REAL,                -- v7: 30d traded volume ($)
    pf_equity        REAL,                -- v7: current account value (portfolio, combined perp+spot+vault)
    pf_turnover      REAL,                -- v7: 7d vlm / equity — frequency proxy (trend traders <~50x, bots >>100x)
    copy_bt_net_pnl  REAL,                -- copy replay net PnL under current observer rules (fees included)
    copy_bt_win_rate REAL,                -- copy replay closed-position win rate
    copy_bt_closed_n INTEGER DEFAULT 0,   -- copy replay closed positions
    copy_bt_open_fill_rate REAL,          -- copied opens / target open events
    copy_bt_liquidations INTEGER DEFAULT 0,
    copy_bt_fee_drag REAL DEFAULT 0,
    copy_bt_unrealized_pnl REAL DEFAULT 0,
    copy_bt_valuation_status TEXT DEFAULT 'complete',
    copy_bt_14d_net_pnl REAL,             -- recent copy replay net PnL (14d confirmation)
    copy_bt_14d_unrealized_pnl REAL DEFAULT 0,
    copy_bt_14d_closed_n INTEGER DEFAULT 0,
    copy_bt_7d_net_pnl REAL,              -- short-term copy replay net PnL (7d confirmation)
    copy_bt_7d_unrealized_pnl REAL DEFAULT 0,
    copy_bt_7d_closed_n INTEGER DEFAULT 0,
    sector_copy_json TEXT,                -- per-sector copy replay summaries (crypto/stock windows)
    sector_policy_json TEXT,              -- per-sector allow/deny policy consumed by observer
    profile_generation TEXT,              -- last complete generation that evaluated this profile
    evaluated_at TEXT,
    data_status TEXT DEFAULT 'valid',     -- valid / deferred_data_error; business rejection belongs to status/reason
    evidence_status TEXT,                 -- qualified / thin / missing / invalid
    last_copyable_open_ms INTEGER,
    open_events_7d INTEGER DEFAULT 0,
    open_events_30d INTEGER DEFAULT 0,
    actionable_open_events_7d INTEGER DEFAULT 0,
    actionable_open_events_30d INTEGER DEFAULT 0,
    open_days_30d INTEGER DEFAULT 0,
    open_probability_48h REAL,
    open_position_count INTEGER DEFAULT 0,
    material_open_count INTEGER DEFAULT 0,
    raw_quality_score REAL,
    copy_expected_return REAL,
    copy_return_lcb REAL,
    copy_return_volatility REAL,
    copy_positive_probability REAL,
    copy_evidence_days INTEGER DEFAULT 0,
    copy_recent_return_14d REAL,
    copy_recent_return_7d REAL,
    copy_risk_score REAL,
    execution_score REAL,
    selection_marginal_utility REAL,
    model_coverage REAL,
    oos_net_pnl REAL,
    oos_max_drawdown REAL,
    oos_cvar95 REAL,
    actionable_open_rate REAL,
    capacity_fit REAL,
    first_added      TEXT,
    last_refreshed   TEXT,
    times_seen       INTEGER DEFAULT 0,
    times_active     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS episode (
    addr TEXT, coin TEXT, side TEXT, open_ms INTEGER, seq INTEGER DEFAULT 0, close_ms INTEGER,
    hold_s REAL, net_pnl REAL, fee REAL, max_notl REAL, n_fills INTEGER,
    open_px REAL, close_px REAL, open_complete INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (addr, coin, open_ms, seq)
);
CREATE INDEX IF NOT EXISTS idx_ep_addr ON episode(addr);
CREATE INDEX IF NOT EXISTS idx_ep_addr_close ON episode(addr, close_ms);
CREATE INDEX IF NOT EXISTS idx_prof_status ON profile(status);
CREATE INDEX IF NOT EXISTS idx_prof_status_score_addr ON profile(status, score DESC, addr);
CREATE INDEX IF NOT EXISTS idx_prof_status_reason ON profile(status, reason);

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
    sector_copy_json TEXT,
    sector_policy_json TEXT,
    generation      TEXT,
    profile_generation TEXT,
    evaluated_at    TEXT,
    data_status     TEXT DEFAULT 'valid',
    evidence_status TEXT,
    times_active   INTEGER,
    first_added    TEXT,
    last_fill_ms   INTEGER,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_watchlist_score_rank ON watchlist(score, rank);
CREATE INDEX IF NOT EXISTS idx_watchlist_rank ON watchlist(rank);

-- Durable wallet identity/lifecycle.  Heavy profile/fill history may be pruned, but registry rows are not.
CREATE TABLE IF NOT EXISTS wallet_registry (
    addr                       TEXT PRIMARY KEY,
    state                      TEXT NOT NULL DEFAULT 'qualified',
    current_role               TEXT,
    first_seen_at              TEXT NOT NULL,
    last_seen_at               TEXT NOT NULL,
    first_qualified_at         TEXT,
    last_qualified_at          TEXT,
    first_core_at              TEXT,
    last_core_at               TEXT,
    last_rejected_at           TEXT,
    last_reject_reason         TEXT,
    cooldown_until             TEXT,
    data_error_count           INTEGER NOT NULL DEFAULT 0,
    consecutive_qualified      INTEGER NOT NULL DEFAULT 0,
    consecutive_bad            INTEGER NOT NULL DEFAULT 0,
    core_entries               INTEGER NOT NULL DEFAULT 0,
    core_exits                 INTEGER NOT NULL DEFAULT 0,
    recovery_count             INTEGER NOT NULL DEFAULT 0,
    last_valid_generation      TEXT,
    last_evaluated_generation  TEXT,
    last_actionable_open_ms    INTEGER,
    updated_at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_registry_state_role
    ON wallet_registry(state, current_role, last_seen_at DESC, addr);
CREATE INDEX IF NOT EXISTS idx_wallet_registry_last_evaluated
    ON wallet_registry(last_evaluated_generation, addr);

-- Explicit generation-scoped Observer target set.  Roles are core/challenger/exit_only; only enabled core
-- rows from the current published generation may originate new positions.
CREATE TABLE IF NOT EXISTS follow_selection (
    generation      TEXT NOT NULL,
    addr            TEXT NOT NULL,
    role            TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    reason          TEXT,
    utility         REAL,
    follow_score    REAL,             -- immutable final copy-follow score at selection publication
    selection_rank  INTEGER,          -- Core contribution order; Challenger score order
    data_status     TEXT,
    evidence_status TEXT,
    model_version   TEXT,
    policy_version  TEXT,
    acct_value      REAL,
    sector_policy_json TEXT,
    replay_copy_bt_net_pnl        REAL,
    replay_copy_bt_win_rate       REAL,
    replay_copy_bt_closed_n       INTEGER,
    replay_copy_bt_open_fill_rate REAL,
    replay_copy_bt_liquidations   INTEGER,
    replay_copy_bt_fee_drag       REAL,
    replay_copy_bt_unrealized_pnl REAL,
    replay_copy_bt_valuation_status TEXT,
    replay_copy_bt_14d_net_pnl    REAL,
    replay_copy_bt_14d_unrealized_pnl REAL,
    replay_copy_bt_14d_closed_n   INTEGER,
    replay_copy_bt_7d_net_pnl     REAL,
    replay_copy_bt_7d_unrealized_pnl REAL,
    replay_copy_bt_7d_closed_n    INTEGER,
    replay_sector_copy_json       TEXT,
    replay_params_hash            TEXT,
    replayed_at                   TEXT,
    selected_at     TEXT NOT NULL,
    PRIMARY KEY (generation, addr)
);
CREATE INDEX IF NOT EXISTS idx_follow_selection_generation_role
    ON follow_selection(generation, role, enabled, addr);
CREATE INDEX IF NOT EXISTS idx_follow_selection_addr_generation
    ON follow_selection(addr, generation);

-- Immutable execution bundles.  A revision binds one published Core generation to the exact
-- engine-unit follow parameters and target execution context used by Observer.  Activation is a
-- singleton pointer update in the same writer transaction that publishes/scales the strategy.
CREATE TABLE IF NOT EXISTS strategy_revision (
    revision             TEXT PRIMARY KEY,
    selection_generation TEXT NOT NULL,
    parent_revision      TEXT,
    source               TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'staged',
    params_json          TEXT NOT NULL,
    params_hash          TEXT NOT NULL,
    targets_json         TEXT NOT NULL,
    validation_json      TEXT,
    reason               TEXT,
    created_at           TEXT NOT NULL,
    activated_at         TEXT,
    superseded_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_strategy_revision_generation
    ON strategy_revision(selection_generation, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_revision_status
    ON strategy_revision(status, activated_at DESC);
CREATE TABLE IF NOT EXISTS active_strategy_revision (
    id         INTEGER PRIMARY KEY CHECK (id=1),
    revision   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Explicit Core-membership history. A wallet that leaves Core keeps its last membership timestamp so the
-- Dashboard can explain recent drops; a later return updates the current generation without losing first-seen history.
CREATE TABLE IF NOT EXISTS follow_history (
    addr                TEXT PRIMARY KEY,
    first_followed_at   TEXT,
    last_followed_at    TEXT,
    last_followed_score REAL,
    first_followed_generation TEXT,
    last_followed_generation  TEXT
);
CREATE INDEX IF NOT EXISTS idx_follow_history_last_followed ON follow_history(last_followed_at DESC, addr);

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
    profiled    INTEGER,
    probed_new  INTEGER,
    added       INTEGER,
    retired     INTEGER,
    kept        INTEGER,
    rejected    INTEGER,
    n_active    INTEGER,
    full        INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    complete    INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_scan_runs_finished ON scan_runs(finished_at DESC);

-- Decision audit for the generation-bound discovery and selection pipeline.
-- One scan/regate stamp can produce:
--   profile           rows per profiled wallet (status/reason/raw score/copy-BT summary)
--   selection         rows per published Core/Challenger/exit-only wallet
--   selection_summary one atomic membership summary
--   tuner_finalize    one synchronous formation/replay summary
CREATE TABLE IF NOT EXISTS pipeline_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stamp         TEXT,
    source        TEXT,
    stage         TEXT,
    addr          TEXT,
    rank          INTEGER,
    status        TEXT,
    reason        TEXT,
    raw_score     REAL,
    follow_score  REAL,
    payload_json  TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipeline_audit_stamp_stage ON pipeline_audit(stamp DESC, stage, rank);
CREATE INDEX IF NOT EXISTS idx_pipeline_audit_addr ON pipeline_audit(addr, stamp DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_audit_stamp_stage_id ON pipeline_audit(stamp DESC, stage, id DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_audit_stamp_source_stage_id ON pipeline_audit(stamp DESC, source, stage, id DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_audit_stage_id ON pipeline_audit(stage, id DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_audit_addr_id ON pipeline_audit(addr, id DESC);
"""

PROFILE_COLS = (
    "addr,status,reason,score,n_fills,n_trades,window_days,trades_per_day,taker_frac_notl,"
    "median_hold_s,win_rate,payoff_ratio,win_pt,max_concurrent,net_pnl,roi_equity,total_notl,acct_value,perp_frac,"
    "top_coin,max_drawdown,avg_notional,age_days,"
    "last_fill_ms,lev_proxy,margin_type,cur_leverage,liq_count,liq_worst_pct,"
    "active_days,activity_ratio,median_eps,pos_day_ratio,profit_conc,hold_skew,open_underwater,"
    "max_adds_per_ep,median_adds_per_ep,worst_loss_pct,market_type,crypto_frac,tp_move_pct,"
    "roi_total,open_unrealized,open_loss_frac,open_win_frac,bag_count,max_bag_days,max_win_days,hedge_ratio,loss_pain,"
    "net_7d,net_14d,net_30d,net_life,"
    "pf_week_pnl,pf_week_vlm,pf_mon_pnl,pf_mon_vlm,pf_equity,pf_turnover,"
    "copy_bt_net_pnl,copy_bt_win_rate,copy_bt_closed_n,copy_bt_open_fill_rate,copy_bt_liquidations,copy_bt_fee_drag,"
    "copy_bt_unrealized_pnl,copy_bt_valuation_status,copy_bt_14d_net_pnl,copy_bt_14d_unrealized_pnl,copy_bt_14d_closed_n,"
    "copy_bt_7d_net_pnl,copy_bt_7d_unrealized_pnl,copy_bt_7d_closed_n,"
    "sector_copy_json,sector_policy_json,"
    "profile_generation,evaluated_at,data_status,evidence_status,last_copyable_open_ms,"
    "open_events_7d,open_events_30d,actionable_open_events_7d,actionable_open_events_30d,"
    "open_days_30d,open_probability_48h,open_position_count,material_open_count,"
    "raw_quality_score,copy_expected_return,copy_return_lcb,copy_return_volatility,"
    "copy_positive_probability,copy_evidence_days,copy_recent_return_14d,copy_recent_return_7d,"
    "copy_risk_score,execution_score,"
    "selection_marginal_utility,model_coverage,oos_net_pnl,oos_max_drawdown,oos_cvar95,"
    "actionable_open_rate,capacity_fit,"
    "first_added,last_refreshed,times_seen,times_active"
)

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
    day_ntl_vlm REAL,             -- 24h notional volume from metaAndAssetCtxs; low-liquidity gate
    open_interest REAL,           -- base open interest from metaAndAssetCtxs
    mark_px    REAL,              -- mark used to value OI
    oi_notional REAL,             -- open_interest * mark_px; low-liquidity gate
    max_leverage REAL,            -- first-tier market max leverage; maintenance rate = 0.5/max_leverage
    margin_meta_updated_at TEXT,
    market_ctx_updated_at TEXT,
    updated_at TEXT
);

-- Our paper strategy account: ONE row. initial_balance is the allocation/sizing anchor; balance is
-- realized strategy equity (starts at initial_balance, += closed PnL). New-copy margin compounds above
-- the anchor and shrinks on a bounded curve below it; real equity/available still enforce hard caps.
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
    entry_px REAL, size REAL, rem_size REAL,       -- our fill px, cumulative followed size, remaining
    peak_size REAL,                                -- historical peak live size; tail exits use rem/peak
    liq_px REAL,                                   -- isolated liquidation price (loss = margin)
    realized_pnl REAL DEFAULT 0,                   -- accumulated realized PnL on this position
    add_count INTEGER DEFAULT 0,                   -- follow-on adds taken (capped at MAX_ADDS)
    mae_pct REAL DEFAULT 0, was_liq INTEGER DEFAULT 0, num_actions INTEGER DEFAULT 0,
    opened_at TEXT, closed_at TEXT,
    strategy_revision_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_cp_status ON copy_position(status);
CREATE INDEX IF NOT EXISTS idx_cp_addr ON copy_position(addr);
CREATE INDEX IF NOT EXISTS idx_cp_status_opened ON copy_position(status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_cp_closed_closed_at ON copy_position(closed_at DESC) WHERE status!='open';
CREATE INDEX IF NOT EXISTS idx_cp_addr_status_opened ON copy_position(addr, status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_cp_coin_status_opened ON copy_position(coin, status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_cp_side_status_opened ON copy_position(side, status, opened_at DESC);

-- Operator manual loss exits create a short wallet+coin cooldown. Profitable full exits and all partial
-- exits keep normal follow eligibility; partial exits retain their live episode for later adds/reduces.
CREATE TABLE IF NOT EXISTS manual_close_cooldown (
    addr       TEXT NOT NULL,
    coin       TEXT NOT NULL,
    pos_id     INTEGER,
    reason     TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (addr, coin)
);
CREATE INDEX IF NOT EXISTS idx_manual_close_cooldown_expires ON manual_close_cooldown(expires_at);

-- One row per master action on a tracked position (open / add / reduce / close), with
-- full detail + OUR mirrored fill at the primary 2s latency. UI "timeline / drill-down".
CREATE TABLE IF NOT EXISTS copy_action (
    act_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pos_id INTEGER, addr TEXT, coin TEXT, ts INTEGER, recv_ms INTEGER,
    action         TEXT,                 -- open / add / reduce / close
    master_oid     INTEGER,              -- master's order id; retained for signal/action audit
    master_px REAL, master_sz_delta REAL, master_pos_after REAL,
    our_qty_delta REAL, our_px REAL, realized_pnl REAL, slippage_bps REAL,
    strategy_revision_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_ca_oid ON copy_action(master_oid);
CREATE INDEX IF NOT EXISTS idx_ca_pos ON copy_action(pos_id);
CREATE INDEX IF NOT EXISTS idx_ca_pos_act ON copy_action(pos_id, action, act_id);  -- per-pos action filter + ordered detail
CREATE INDEX IF NOT EXISTS idx_ca_pos_action_ts ON copy_action(pos_id, action, ts, act_id);

-- ===== Dashboard layer (control plane) =====
-- The dashboard NEVER writes business tables directly. All writes go here as commands consumed by
-- Observer/Scanner (single-writer invariant). Read side: process_status / scan_progress / params.

-- Command channel: the ONLY way the dashboard mutates trading state. Observer/Scanner poll this,
-- execute, and flip status. owner+TTL lets a consumer self-heal a stuck flag if the issuer dies.
CREATE TABLE IF NOT EXISTS commands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT,                 -- pause|resume|close_position|close_all|wallet_toggle|rescan|scan_stop|patch_params
    payload_json    TEXT,
    idempotency_key TEXT UNIQUE,          -- client-supplied dedup key (optional)
    owner           TEXT,                 -- issuing dashboard instance
    status          TEXT DEFAULT 'pending', -- pending|acked|done|failed
    result_json     TEXT,
    error           TEXT,
    created_at      TEXT, acked_at TEXT, done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmd_status ON commands(status);
CREATE INDEX IF NOT EXISTS idx_cmd_status_type_id ON commands(status, type, id);

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
    stage              TEXT,              -- scan_leaderboard|fetch_history|score_filter|rebuild_watchlist|auto_tune|persist
    candidates_scanned INTEGER DEFAULT 0,
    candidates_total   INTEGER DEFAULT 0,
    eta_sec            INTEGER,
    manual             INTEGER DEFAULT 0,  -- 1 = dashboard-triggered (lock UI); 0 = scheduled background scan
    updated_at         TEXT
);

-- Per-candidate executable contract cache (rolling PROFILE_FETCH_DAYS window). The source endpoint returns
-- all user fills, but only current standard Crypto perps and transparent xyz stock/index/commodity contracts
-- may enter this table. Spot, outcome/settlement and private builder markets are discarded before persistence.
-- fill_cache_state.coverage_end_ms is the source cursor, so filtering every row does not cause refetch loops.
CREATE TABLE IF NOT EXISTS candidate_fills (
    addr      TEXT NOT NULL,
    tid       INTEGER NOT NULL,   -- HL trade id (unique per fill) — dedup key
    time      INTEGER NOT NULL,   -- fill time (ms)
    fill_json TEXT NOT NULL,
    PRIMARY KEY (addr, tid)
);
CREATE INDEX IF NOT EXISTS idx_candidate_fills_addr_time ON candidate_fills(addr, time);

-- Shared, bounded market path cache for copy-replay liquidation validation. Candles are keyed by market,
-- not wallet, so every candidate and portfolio replay reuses the same observations. Retention is enforced
-- by hl.price_path after refresh: 15m keeps 39 days (37d replay + boundary buffer), 1m keeps 4 days.
CREATE TABLE IF NOT EXISTS coin_price_candle (
    coin       TEXT NOT NULL,
    interval   TEXT NOT NULL,
    open_time  INTEGER NOT NULL,
    close_time INTEGER NOT NULL,
    open_px    REAL NOT NULL,
    high_px    REAL NOT NULL,
    low_px     REAL NOT NULL,
    close_px   REAL NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (coin, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_coin_price_candle_expiry
    ON coin_price_candle(interval, close_time);
CREATE INDEX IF NOT EXISTS idx_coin_price_candle_coin_range
    ON coin_price_candle(coin, interval, open_time);
CREATE TABLE IF NOT EXISTS coin_price_path_state (
    coin          TEXT NOT NULL,
    interval      TEXT NOT NULL,
    status        TEXT NOT NULL,
    error_count   INTEGER NOT NULL DEFAULT 0,
    last_attempt  INTEGER NOT NULL,
    retry_after   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (coin, interval)
);

-- Per-wallet cache coverage.  This deliberately separates a full PROFILE workset from a full
-- historical FILL refetch: migrations can refresh every wallet while only backfilling wallets whose
-- copy replay actually needs the additional warm-up context.
CREATE TABLE IF NOT EXISTS fill_cache_state (
    addr              TEXT PRIMARY KEY,
    coverage_start_ms INTEGER,
    coverage_end_ms   INTEGER,
    updated_at        TEXT
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

-- Auto-tuner durable state and run audit (active proposal, rollback and effective replay snapshots).
CREATE TABLE IF NOT EXISTS auto_tune_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS auto_tune_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT,
    stamp         TEXT,
    generation    TEXT,
    mode          TEXT DEFAULT 'shadow',
    status        TEXT,
    selected_mult REAL,
    applied       INTEGER DEFAULT 0,
    eligible_to_apply INTEGER DEFAULT 0,
    followed_n    INTEGER DEFAULT 0,
    baseline_json TEXT,
    proposal_json TEXT,
    validation_json TEXT,
    result_json   TEXT,
    applied_at    TEXT,
    rollback_at   TEXT,
    rollback_reason TEXT,
    created_at    TEXT
);

-- ===== AI risk radar (Observer-owned business state) =====
-- Dashboard routes are read-only for these tables.  Start/stop and credential changes travel through
-- `commands`, then the Observer validates and persists them, preserving the single-writer boundary.
CREATE TABLE IF NOT EXISTS market_risk_snapshot (
    snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    assessed_for_ms   INTEGER NOT NULL UNIQUE,
    features_json     TEXT NOT NULL,
    coverage_json     TEXT NOT NULL,
    input_hash        TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_risk_snapshot_created
    ON market_risk_snapshot(created_at DESC);

CREATE TABLE IF NOT EXISTS market_risk_assessment (
    assessment_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id         INTEGER,
    assessed_for_ms     INTEGER NOT NULL,
    model               TEXT,
    prompt_version      TEXT,
    status              TEXT NOT NULL,
    raw_bullish_score   REAL,
    bullish_score       REAL,
    bearish_score       REAL,
    confidence          REAL,
    regime              TEXT,
    risky_direction     TEXT,
    block_side          TEXT,
    confirmation_mode   TEXT,
    active_block        INTEGER NOT NULL DEFAULT 0,
    previous_assessment_id INTEGER,
    valid_until_ms      INTEGER,
    reason              TEXT,
    evidence_json       TEXT,
    invalidation_json   TEXT,
    response_json       TEXT,
    latency_ms          INTEGER,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    estimated_cost       REAL,
    cost_currency        TEXT,
    error               TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES market_risk_snapshot(snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_market_risk_assessment_time
    ON market_risk_assessment(assessed_for_ms DESC, assessment_id DESC);

CREATE TABLE IF NOT EXISTS market_risk_state (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    mode                  TEXT NOT NULL DEFAULT 'off',
    status                TEXT NOT NULL DEFAULT 'stopped',
    current_assessment_id INTEGER,
    block_side            TEXT,
    risk_score            REAL,
    confirmation_mode     TEXT,
    valid_until_ms        INTEGER,
    connection_status     TEXT NOT NULL DEFAULT 'not_configured',
    last_assessed_at      TEXT,
    last_error            TEXT,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_risk_intent (
    intent_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pos_id              INTEGER NOT NULL UNIQUE,
    addr                TEXT NOT NULL,
    coin                TEXT NOT NULL,
    side                TEXT NOT NULL,
    source_oid          INTEGER,
    assessment_id       INTEGER,
    risk_score          REAL,
    would_block         INTEGER NOT NULL DEFAULT 0,
    confirmation_mode   TEXT,
    decision_reason     TEXT,
    opened_at           TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',
    checkpoint_pnl      REAL,
    realized_pnl        REAL,
    fee                 REAL,
    net_pnl             REAL,
    outcome             TEXT,
    resolved_at         TEXT,
    FOREIGN KEY(pos_id) REFERENCES copy_position(pos_id),
    FOREIGN KEY(assessment_id) REFERENCES market_risk_assessment(assessment_id)
);
CREATE INDEX IF NOT EXISTS idx_market_risk_intent_opened
    ON market_risk_intent(opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_risk_intent_shadow
    ON market_risk_intent(would_block, status, opened_at DESC);

-- V2 action-level counterfactual.  The normal Paper position remains the baseline book.  This episode
-- holds only the AI-filtered exposure: a blocked first open leaves shadow_qty=0, while a later allowed
-- add can create a delayed entry without catching up the exposure that was previously rejected.
CREATE TABLE IF NOT EXISTS market_risk_episode (
    episode_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pos_id              INTEGER NOT NULL UNIQUE,
    addr                TEXT NOT NULL,
    coin                TEXT NOT NULL,
    side                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',
    entry_blocked       INTEGER NOT NULL DEFAULT 0,
    delayed_entry       INTEGER NOT NULL DEFAULT 0,
    blocked_entries     INTEGER NOT NULL DEFAULT 0,
    allowed_entries     INTEGER NOT NULL DEFAULT 0,
    shadow_qty          REAL NOT NULL DEFAULT 0,
    shadow_entry_px     REAL,
    shadow_realized_pnl REAL NOT NULL DEFAULT 0,
    shadow_fee          REAL NOT NULL DEFAULT 0,
    baseline_net_pnl    REAL,
    shadow_net_pnl      REAL,
    net_benefit         REAL,
    outcome             TEXT,
    opened_at           TEXT NOT NULL,
    resolved_at         TEXT,
    FOREIGN KEY(pos_id) REFERENCES copy_position(pos_id)
);
CREATE INDEX IF NOT EXISTS idx_market_risk_episode_status
    ON market_risk_episode(status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_risk_episode_benefit
    ON market_risk_episode(status, net_benefit);

-- One immutable entry-time decision per copied exposure action.  Multiple fills from the same master
-- order reuse the first decision_group verdict, so exchange slicing cannot repeatedly query or flip AI.
-- Exit actions have decision='mandatory_exit': AI is never allowed to prevent risk reduction.
CREATE TABLE IF NOT EXISTS market_risk_action (
    risk_action_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id          INTEGER NOT NULL,
    pos_id              INTEGER NOT NULL,
    copy_act_id         INTEGER UNIQUE,
    decision_group      TEXT,
    source_oid          INTEGER,
    action              TEXT NOT NULL,
    side                TEXT NOT NULL,
    assessment_id       INTEGER,
    risk_score          REAL,
    would_block         INTEGER NOT NULL DEFAULT 0,
    confirmation_mode   TEXT,
    decision            TEXT NOT NULL,
    decision_reason     TEXT,
    baseline_qty_delta  REAL NOT NULL DEFAULT 0,
    baseline_px         REAL,
    shadow_qty_delta    REAL NOT NULL DEFAULT 0,
    shadow_px           REAL,
    shadow_realized_pnl REAL NOT NULL DEFAULT 0,
    close_fraction      REAL,
    created_at          TEXT NOT NULL,
    FOREIGN KEY(episode_id) REFERENCES market_risk_episode(episode_id),
    FOREIGN KEY(pos_id) REFERENCES copy_position(pos_id),
    FOREIGN KEY(copy_act_id) REFERENCES copy_action(act_id),
    FOREIGN KEY(assessment_id) REFERENCES market_risk_assessment(assessment_id)
);
CREATE INDEX IF NOT EXISTS idx_market_risk_action_episode
    ON market_risk_action(episode_id, risk_action_id);
CREATE INDEX IF NOT EXISTS idx_market_risk_action_decision_group
    ON market_risk_action(pos_id, decision_group, risk_action_id);

-- Envelope-encrypted provider credentials.  No plaintext is persisted; the private wrapping key lives
-- outside SQLite and is readable only by the Observer service account.
CREATE TABLE IF NOT EXISTS provider_credential (
    provider          TEXT PRIMARY KEY,
    envelope_version  INTEGER NOT NULL,
    key_id            TEXT NOT NULL,
    wrapped_key       TEXT NOT NULL,
    nonce             TEXT NOT NULL,
    ciphertext        TEXT NOT NULL,
    status            TEXT NOT NULL,
    last_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_validated_at TEXT
);

CREATE TABLE IF NOT EXISTS provider_balance_snapshot (
    balance_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    provider          TEXT NOT NULL,
    checked_at        TEXT NOT NULL,
    currency          TEXT,
    total_balance     REAL,
    granted_balance   REAL,
    topped_up_balance REAL,
    is_available      INTEGER,
    estimated_days    REAL,
    estimated_requests INTEGER,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_balance_latest
    ON provider_balance_snapshot(provider, checked_at DESC);
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
    # Take-profit signature (non-destructive on existing DBs).
    "ALTER TABLE profile ADD COLUMN tp_move_pct REAL DEFAULT 0",
    "ALTER TABLE watchlist ADD COLUMN tp_move_pct REAL DEFAULT 0",
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
    # v7 portfolio net-of-fees metrics (authoritative account-level perf; leaderboard is gross + lagging).
    "ALTER TABLE profile ADD COLUMN pf_week_pnl REAL",
    "ALTER TABLE profile ADD COLUMN pf_week_vlm REAL",
    "ALTER TABLE profile ADD COLUMN pf_mon_pnl REAL",
    "ALTER TABLE profile ADD COLUMN pf_mon_vlm REAL",
    "ALTER TABLE profile ADD COLUMN pf_equity REAL",
    "ALTER TABLE profile ADD COLUMN pf_turnover REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_net_pnl REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_win_rate REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_closed_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_open_fill_rate REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_liquidations INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_fee_drag REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_14d_net_pnl REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_14d_closed_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_7d_net_pnl REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_7d_closed_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN sector_copy_json TEXT",
    "ALTER TABLE profile ADD COLUMN sector_policy_json TEXT",
    "ALTER TABLE watchlist ADD COLUMN sector_copy_json TEXT",
    "ALTER TABLE watchlist ADD COLUMN sector_policy_json TEXT",
    # 盈亏比与并发/单笔盈利幅度审计字段。
    "ALTER TABLE profile ADD COLUMN payoff_ratio REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN max_concurrent INTEGER DEFAULT 0",  # 峰值同时持仓 → too_many_concurrent 闸
    "ALTER TABLE profile ADD COLUMN win_pt REAL DEFAULT 0",             # 赢单每笔中位收益% (审计指标)
    "ALTER TABLE scan_runs ADD COLUMN profiled INTEGER",
    "ALTER TABLE scan_runs ADD COLUMN full INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN failed INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN complete INTEGER DEFAULT 1",
    "ALTER TABLE follow_history ADD COLUMN first_followed_at TEXT",
    "ALTER TABLE follow_history ADD COLUMN first_followed_generation TEXT",
    "ALTER TABLE follow_history ADD COLUMN last_followed_generation TEXT",
    "ALTER TABLE coin_vol ADD COLUMN day_ntl_vlm REAL",
    "ALTER TABLE coin_vol ADD COLUMN open_interest REAL",
    "ALTER TABLE coin_vol ADD COLUMN mark_px REAL",
    "ALTER TABLE coin_vol ADD COLUMN oi_notional REAL",
    "ALTER TABLE coin_vol ADD COLUMN market_ctx_updated_at TEXT",
    "ALTER TABLE coin_vol ADD COLUMN max_leverage REAL",
    "ALTER TABLE coin_vol ADD COLUMN margin_meta_updated_at TEXT",
    # Generation/freshness/evidence and actionable-open flow.
    "ALTER TABLE leaderboard ADD COLUMN generation TEXT",
    "ALTER TABLE profile ADD COLUMN profile_generation TEXT",
    "ALTER TABLE profile ADD COLUMN evaluated_at TEXT",
    "ALTER TABLE profile ADD COLUMN data_status TEXT DEFAULT 'valid'",
    "ALTER TABLE profile ADD COLUMN evidence_status TEXT",
    "ALTER TABLE profile ADD COLUMN last_copyable_open_ms INTEGER",
    "ALTER TABLE profile ADD COLUMN open_events_7d INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN open_events_30d INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN actionable_open_events_7d INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN actionable_open_events_30d INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN open_days_30d INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN open_probability_48h REAL",
    "ALTER TABLE profile ADD COLUMN open_position_count INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN material_open_count INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN raw_quality_score REAL",
    "ALTER TABLE profile ADD COLUMN copy_expected_return REAL",
    "ALTER TABLE profile ADD COLUMN copy_return_lcb REAL",
    "ALTER TABLE profile ADD COLUMN copy_return_volatility REAL",
    "ALTER TABLE profile ADD COLUMN copy_positive_probability REAL",
    "ALTER TABLE profile ADD COLUMN copy_evidence_days INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_recent_return_14d REAL",
    "ALTER TABLE profile ADD COLUMN copy_recent_return_7d REAL",
    "ALTER TABLE profile ADD COLUMN copy_risk_score REAL",
    "ALTER TABLE profile ADD COLUMN execution_score REAL",
    "ALTER TABLE profile ADD COLUMN selection_marginal_utility REAL",
    "ALTER TABLE profile ADD COLUMN model_coverage REAL",
    "ALTER TABLE profile ADD COLUMN oos_net_pnl REAL",
    "ALTER TABLE profile ADD COLUMN oos_max_drawdown REAL",
    "ALTER TABLE profile ADD COLUMN oos_cvar95 REAL",
    "ALTER TABLE profile ADD COLUMN actionable_open_rate REAL",
    "ALTER TABLE profile ADD COLUMN capacity_fit REAL",
    "ALTER TABLE watchlist ADD COLUMN generation TEXT",
    "ALTER TABLE watchlist ADD COLUMN profile_generation TEXT",
    "ALTER TABLE watchlist ADD COLUMN evaluated_at TEXT",
    "ALTER TABLE watchlist ADD COLUMN data_status TEXT DEFAULT 'valid'",
    "ALTER TABLE watchlist ADD COLUMN evidence_status TEXT",
    # Auto-tune proposal lifecycle; legacy rows remain readable.
    "ALTER TABLE auto_tune_runs ADD COLUMN generation TEXT",
    "ALTER TABLE auto_tune_runs ADD COLUMN mode TEXT DEFAULT 'shadow'",
    "ALTER TABLE auto_tune_runs ADD COLUMN status TEXT",
    "ALTER TABLE auto_tune_runs ADD COLUMN eligible_to_apply INTEGER DEFAULT 0",
    "ALTER TABLE auto_tune_runs ADD COLUMN proposal_json TEXT",
    "ALTER TABLE auto_tune_runs ADD COLUMN validation_json TEXT",
    "ALTER TABLE auto_tune_runs ADD COLUMN applied_at TEXT",
    "ALTER TABLE auto_tune_runs ADD COLUMN rollback_at TEXT",
    "ALTER TABLE auto_tune_runs ADD COLUMN rollback_reason TEXT",
    # Display-only replay under the currently effective strategy parameters.  The scan-time profile
    # evidence remains immutable, so this refresh cannot feed back into Core membership.
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_win_rate REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_closed_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_open_fill_rate REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_liquidations INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_fee_drag REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_14d_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_14d_closed_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_7d_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_7d_closed_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_sector_copy_json TEXT",
    "ALTER TABLE follow_selection ADD COLUMN replay_params_hash TEXT",
    "ALTER TABLE follow_selection ADD COLUMN replayed_at TEXT",
    "ALTER TABLE follow_selection ADD COLUMN follow_score REAL",
    "ALTER TABLE follow_selection ADD COLUMN selection_rank INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN acct_value REAL",
    "ALTER TABLE follow_selection ADD COLUMN sector_policy_json TEXT",
    "ALTER TABLE episode ADD COLUMN open_complete INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE copy_position ADD COLUMN strategy_revision_id TEXT",
    "ALTER TABLE copy_action ADD COLUMN strategy_revision_id TEXT",
    "ALTER TABLE copy_position ADD COLUMN peak_size REAL",
    # AI risk radar development migrations (safe no-ops on a fresh schema).
    "ALTER TABLE market_risk_intent ADD COLUMN source_oid INTEGER",
    "ALTER TABLE market_risk_assessment ADD COLUMN estimated_cost REAL",
    "ALTER TABLE market_risk_assessment ADD COLUMN cost_currency TEXT",
    # Canonical Copy economic PnL = closed net + terminal open-position mark-to-market.
    "ALTER TABLE profile ADD COLUMN copy_bt_unrealized_pnl REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_valuation_status TEXT DEFAULT 'complete'",
    "ALTER TABLE profile ADD COLUMN copy_bt_14d_unrealized_pnl REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_7d_unrealized_pnl REAL DEFAULT 0",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_unrealized_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_valuation_status TEXT",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_14d_unrealized_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_7d_unrealized_pnl REAL",
)


def connect(path: str, *schemas: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)  # used across the scanner's
    db.execute("PRAGMA journal_mode=WAL")                            # worker threads (writes are
    db.execute("PRAGMA busy_timeout=30000")                          # serialized by a lock)
    for s in schemas:
        db.executescript(s)
    # Dashboard, Observer and a maintenance CLI can start at the same moment after deploy.  Serialize
    # schema inspection + ALTERs so two fresh processes cannot both decide a column is missing and race.
    db.execute("BEGIN IMMEDIATE")
    try:
        _apply_migrations(db)
        _retire_maker_shadow(db)
        _retire_obsolete_selection_state(db)
        _migrate_episode_seq(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return db


_ADD_COLUMN_RE = re.compile(
    r"^ALTER TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)


def _apply_migrations(db: sqlite3.Connection) -> None:
    """Apply only missing column migrations without using exceptions as normal control flow.

    Connections are opened frequently by CLI/tests. The old implementation retried every historical ALTER
    and swallowed every OperationalError, which was noisy and could hide a malformed migration or I/O error.
    """
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    columns = {}
    for stmt in _MIGRATIONS:
        match = _ADD_COLUMN_RE.match(stmt)
        if not match:
            db.execute(stmt)
            continue
        table, column = match.groups()
        if table not in tables:
            continue
        if table not in columns:
            columns[table] = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in columns[table]:
            continue
        db.execute(stmt)
        columns[table].add(column)
    if "auto_tune_runs" in tables:
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_auto_tune_runs_generation "
            "ON auto_tune_runs(generation, created_at DESC, id DESC)"
        )


def _retire_maker_shadow(db: sqlite3.Connection) -> None:
    """Remove the retired Maker/Taker experiment from both fresh and existing databases."""
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    for table in ("shadow_action", "shadow_position", "shadow_order", "shadow_account", "target_orders"):
        if table in tables:
            db.execute(f"DROP TABLE {table}")
    if "params" in tables:
        db.execute("DELETE FROM params WHERE key='EXEC_MAKER_MIRROR'")
    if "copy_action" in tables:
        copy_action_columns = {row[1] for row in db.execute("PRAGMA table_info(copy_action)").fetchall()}
        if "maker" in copy_action_columns:
            db.execute("ALTER TABLE copy_action DROP COLUMN maker")


def _retire_obsolete_selection_state(db: sqlite3.Connection) -> None:
    """Remove state and write-only profile columns retired by the current selection model."""
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "wallet_registry" in tables:
        columns = {row[1] for row in db.execute("PRAGMA table_info(wallet_registry)").fetchall()}
        for column in (
            "core_nomination_streak",
            "core_omission_streak",
            "core_nomination_started_at",
            "core_omission_started_at",
            "last_core_signal_generation",
        ):
            if column in columns:
                db.execute(f"ALTER TABLE wallet_registry DROP COLUMN {column}")
    if "profile" in tables:
        columns = {row[1] for row in db.execute("PRAGMA table_info(profile)").fetchall()}
        for column in (
            "avg_win", "avg_loss", "roi_notional", "gross_pnl", "total_fee", "n_coins",
            "long_frac", "life_trades", "pf_max_dd", "pf_edge_bps", "open_events_14d",
            "actionable_open_events_14d", "open_days_7d", "open_days_14d",
            "avg_open_interval_h", "median_open_interval_h", "open_probability_24h",
        ):
            if column in columns:
                db.execute(f"ALTER TABLE profile DROP COLUMN {column}")
    if "params" in tables:
        db.execute(
            "DELETE FROM params WHERE key IN ('MIN_FOLLOW_SCORE','COPY_STOP_ENABLE','STOP_MARGIN_PCT')"
        )
    if "auto_tune_state" in tables:
        db.execute(
            "DELETE FROM auto_tune_state WHERE key IN "
            "('margin_base','margin_last_auto','tune_base','tune_last_auto',"
            "'add_base','add_last_auto','follow_line_last_choice','async_tuner_lease')"
        )


def _migrate_episode_seq(db: sqlite3.Connection) -> None:
    cols = db.execute("PRAGMA table_info(episode)").fetchall()
    if not cols:
        return
    pk_cols = [r[1] for r in sorted((r for r in cols if r[5]), key=lambda r: r[5])]
    if pk_cols == ["addr", "coin", "open_ms", "seq"]:
        return

    names = {r[1] for r in cols}
    db.execute("DROP TABLE IF EXISTS episode_migrate_old")
    db.execute("ALTER TABLE episode RENAME TO episode_migrate_old")
    db.executescript(
        """
        CREATE TABLE episode (
            addr TEXT, coin TEXT, side TEXT, open_ms INTEGER, seq INTEGER DEFAULT 0, close_ms INTEGER,
            hold_s REAL, net_pnl REAL, fee REAL, max_notl REAL, n_fills INTEGER,
            open_px REAL, close_px REAL, open_complete INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (addr, coin, open_ms, seq)
        );
        """
    )
    seq_expr = "COALESCE(seq, 0)" if "seq" in names else "0"
    complete_expr = "COALESCE(open_complete, 1)" if "open_complete" in names else "1"
    db.execute(
        "INSERT OR IGNORE INTO episode "
        "(addr,coin,side,open_ms,seq,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px,open_complete) "
        f"SELECT addr,coin,side,open_ms,{seq_expr},close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px,{complete_expr} "
        "FROM episode_migrate_old"
    )
    db.execute("DROP TABLE episode_migrate_old")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ep_addr ON episode(addr)")
