"""Single source of truth for the SQLite schema. All persistent data lives here as
structured tables (never raw JSON dumps) so the schema can be extended over time
(add columns/tables for the execution leg later without touching call sites).

One db file (data/hl.db), layered by concern:
  discovery   : leaderboard (raw HL firehose)  ->  profile (full per-wallet analysis,
                all statuses)  ->  watchlist (OUR curated tiny leaderboard, ranked,
                UI-facing, rebuilt each scan)
  control     : target_controls (operator settings: enabled/pinned/note — survive scans)
  diagnostics : scan_runs (one row per scan: counts + duration, for ops/UI history)
  execution   : durable source signals/cursors, independent Paper/Live ledgers, and signed-order audit
"""
import sqlite3
import re
from pathlib import Path

from hyper import config

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

-- Immutable market inputs used by one scanner generation. The resolver snapshots usable ``coin_vol`` values
-- when the generation starts and materialises them here on first use; cold coins are fetched/computed once.
-- Qualification, portfolio formation and tuning read these frozen rows so a long scan cannot mix later cache
-- refreshes into the same generation.
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
    retry_transition_n INTEGER DEFAULT 0, -- same-coin closed Episode -> next Episode transitions
    rapid_same_side_retry_n INTEGER DEFAULT 0,
    rapid_same_side_retry_rate REAL DEFAULT 0,
    loss_retry_transition_n INTEGER DEFAULT 0,
    rapid_loss_retry_n INTEGER DEFAULT 0,
    rapid_loss_retry_rate REAL DEFAULT 0,
    rapid_retry_chain_n INTEGER DEFAULT 0,
    rapid_retry_max_chain_episodes INTEGER DEFAULT 0,
    loss_started_retry_chain_n INTEGER DEFAULT 0,
    loss_started_retry_chain_losing_n INTEGER DEFAULT 0,
    loss_started_retry_chain_lose_rate REAL DEFAULT 0,
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
    copy_bt_closed_net_pnl REAL,           -- exact fee-paid PnL from complete closed Copy Episodes
    copy_bt_win_rate REAL,                -- copy replay closed-position win rate
    copy_bt_closed_n INTEGER DEFAULT 0,   -- copy replay closed positions
    copy_bt_open_fill_rate REAL,          -- copied opens / economically effective target opens
    copy_bt_raw_target_open_n INTEGER DEFAULT 0,
    copy_bt_small_open_excluded_n INTEGER DEFAULT 0,
    copy_bt_effective_target_open_n INTEGER DEFAULT 0,
    copy_bt_opened_n INTEGER DEFAULT 0,
    copy_bt_raw_open_capture_rate REAL,
    copy_bt_open_audit_json TEXT,
    copy_bt_liquidations INTEGER DEFAULT 0,
    copy_bt_max_liquidation_loss_pct REAL,
    copy_bt_max_liquidation_loss REAL,
    copy_bt_max_liquidation_loss_coin TEXT,
    copy_bt_max_liquidation_loss_closed_at INTEGER,
    copy_bt_fee_drag REAL DEFAULT 0,
    copy_bt_unrealized_pnl REAL DEFAULT 0,
    copy_bt_valuation_status TEXT DEFAULT 'complete',
    copy_bt_initial_margin_equity REAL,    -- replay account equity before warm-up
    copy_bt_window_start_equity REAL,     -- actual floating equity at the 30d boundary
    copy_bt_14d_net_pnl REAL,             -- recent copy replay net PnL (14d confirmation)
    copy_bt_14d_closed_net_pnl REAL,
    copy_bt_14d_unrealized_pnl REAL DEFAULT 0,
    copy_bt_14d_closed_n INTEGER DEFAULT 0,
    copy_bt_14d_window_start_equity REAL, -- actual floating equity at the 14d boundary
    copy_bt_7d_net_pnl REAL,              -- short-term copy replay net PnL (7d confirmation)
    copy_bt_7d_closed_net_pnl REAL,
    copy_bt_7d_unrealized_pnl REAL DEFAULT 0,
    copy_bt_7d_closed_n INTEGER DEFAULT 0,
    copy_bt_7d_window_start_equity REAL,  -- actual floating equity at the 7d boundary
    sector_copy_json TEXT,                -- per-sector copy replay summaries (crypto/stock windows)
    sector_policy_json TEXT,              -- per-sector allow/deny policy consumed by observer
    profile_generation TEXT,              -- last complete generation that evaluated this profile
    evaluated_at TEXT,
    data_status TEXT DEFAULT 'valid',     -- valid / deferred_data_error; business rejection belongs to status/reason
    evidence_status TEXT,                 -- qualified / thin / missing / invalid
    official_perp_status TEXT,             -- passed / rejected / deferred_data_error for this generation
    official_perp_reason TEXT,
    official_perp_evidence_json TEXT,
    official_perp_return_30d REAL,
    official_perp_pnl_30d REAL,
    official_perp_pnl_share REAL,
    source_episode_n_30d INTEGER DEFAULT 0,
    source_episode_n_7d INTEGER DEFAULT 0,
    source_win_rate_30d REAL,
    source_win_rate_7d REAL,
    source_net_pnl_30d REAL,
    source_net_pnl_7d REAL,
    source_active_days_30d INTEGER DEFAULT 0,
    source_active_days_7d INTEGER DEFAULT 0,
    source_top3_profit_share REAL,
    source_body_after_top3_n INTEGER DEFAULT 0,
    source_body_after_top3_win_rate REAL,
    source_body_after_top3_net_pnl REAL,
    source_gross_profit_30d REAL,
    source_gross_loss_30d REAL,
    source_profit_factor_30d REAL,
    source_payoff_ratio_30d REAL,
    source_quality_score REAL,
    rough_copy_score REAL,
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
    copy_path_risk_status TEXT DEFAULT 'pending', -- pending/complete/missing/invalid; old rows fail closed for Core
    copy_intratrade_max_drawdown REAL,
    copy_max_underwater_hours REAL,
    copy_loss_over_5_time_ratio REAL,
    copy_deep_bag_event_n INTEGER DEFAULT 0,
    copy_failed_deep_bag_n INTEGER DEFAULT 0,
    copy_deep_bag_recovery_rate REAL,
    copy_max_deep_bag_hours REAL,
    copy_current_open_loss_frac REAL,
    copy_current_bag_hours REAL,
    copy_campaign_max_drawdown REAL,
    copy_campaign_peak_positions INTEGER DEFAULT 0,
    copy_campaign_peak_margin_pct REAL,
    copy_bt_gross_profit REAL,
    copy_bt_gross_loss REAL,
    copy_bt_profit_factor REAL,
    copy_bt_payoff_ratio REAL,
    copy_bt_top3_profit_share REAL,
    copy_bt_body_after_top3_n INTEGER,
    copy_bt_body_after_top3_win_rate REAL,
    copy_bt_body_after_top3_net_pnl REAL,
    first_added      TEXT,
    last_refreshed   TEXT,
    times_seen       INTEGER DEFAULT 0,
    times_active     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS episode (
    addr TEXT, coin TEXT, side TEXT, open_ms INTEGER, seq INTEGER DEFAULT 0, close_ms INTEGER,
    hold_s REAL, net_pnl REAL, fee REAL, max_notl REAL, n_fills INTEGER, n_oids INTEGER,
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
    core_retention_status      TEXT NOT NULL DEFAULT 'healthy',
    core_retention_fail_streak INTEGER NOT NULL DEFAULT 0,
    core_retention_reason      TEXT,
    core_retention_started_generation TEXT,
    last_core_retention_generation TEXT,
    risk_level                 TEXT NOT NULL DEFAULT 'normal',
    risk_reasons_json          TEXT,
    risk_confirmation_count    INTEGER NOT NULL DEFAULT 0,
    risk_first_confirmed_at     TEXT,
    risk_assessed_at           TEXT,
    risk_block_reason          TEXT,
    updated_at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_registry_state_role
    ON wallet_registry(state, current_role, last_seen_at DESC, addr);
CREATE INDEX IF NOT EXISTS idx_wallet_registry_last_evaluated
    ON wallet_registry(last_evaluated_generation, addr);

-- Durable admission vetoes. Unlike rolling fill/profile caches, confirmed major-loss events survive
-- discovery pruning so a wallet cannot quietly re-enter after the offending replay window ages out.
CREATE TABLE IF NOT EXISTS wallet_risk_event (
    addr          TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    event_key     TEXT NOT NULL,
    occurred_at   INTEGER,
    coin          TEXT,
    loss_usd      REAL,
    loss_pct      REAL,
    evidence_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (addr, event_type, event_key)
);
CREATE INDEX IF NOT EXISTS idx_wallet_risk_event_addr_type
    ON wallet_risk_event(addr, event_type, occurred_at DESC);

-- Scanner and Observer assessments are immutable at generation/day level.  The registry above is only the
-- latest projection used by execution and the Dashboard; prepublication rows survive a later scan failure.
CREATE TABLE IF NOT EXISTS wallet_risk_assessment (
    generation          TEXT NOT NULL,
    addr                TEXT NOT NULL,
    source              TEXT NOT NULL,
    risk_level          TEXT NOT NULL,
    reasons_json        TEXT,
    evidence_json       TEXT,
    confirmation_count  INTEGER NOT NULL DEFAULT 0,
    first_confirmed_at  TEXT,
    assessed_at         TEXT NOT NULL,
    complete            INTEGER NOT NULL DEFAULT 1,
    block_reason        TEXT,
    PRIMARY KEY (generation, addr)
);
CREATE INDEX IF NOT EXISTS idx_wallet_risk_assessment_addr_time
    ON wallet_risk_assessment(addr, assessed_at DESC);

-- Fast execution-side safety freezes are deliberately separate from permanent admission vetoes.
-- A pending source liquidation blocks new exposure while clearinghouse confirmation is retried.
CREATE TABLE IF NOT EXISTS execution_wallet_safety (
    addr          TEXT PRIMARY KEY,
    state         TEXT NOT NULL,       -- pending / confirmed / cleared
    event_key     TEXT,
    occurred_at   INTEGER,
    reason        TEXT,
    evidence_json TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_wallet_safety_state
    ON execution_wallet_safety(state, updated_at, addr);

-- Immutable generation-scoped hand-off between deep fills-only analysis and path-complete strict replay.
-- It is intentionally separate from the mutable profile projection and from read-only research tables.
CREATE TABLE IF NOT EXISTS pre_strict_evidence (
    generation                   TEXT NOT NULL,
    addr                         TEXT NOT NULL,
    policy_version               TEXT NOT NULL,
    model_version                TEXT NOT NULL,
    status                       TEXT NOT NULL,
    first_failure                TEXT,
    activity_json                TEXT,
    latest_7d_active             INTEGER NOT NULL DEFAULT 0,
    active_weeks_4               INTEGER NOT NULL DEFAULT 0,
    weekly_open_counts_json      TEXT,
    max_open_gap_days_28d        REAL,
    actionable_open_events_28d   INTEGER NOT NULL DEFAULT 0,
    actionable_open_events_7d    INTEGER NOT NULL DEFAULT 0,
    source_closed_n_30d          INTEGER NOT NULL DEFAULT 0,
    source_win_rate_30d          REAL,
    source_gross_profit_30d      REAL,
    source_gross_loss_30d        REAL,
    source_profit_factor_30d     REAL,
    source_payoff_ratio_30d      REAL,
    source_top3_profit_share     REAL,
    source_body_net_pnl          REAL,
    source_body_win_rate         REAL,
    copy_closed_n_30d            INTEGER NOT NULL DEFAULT 0,
    copy_win_rate_30d            REAL,
    copy_gross_profit_30d        REAL,
    copy_gross_loss_30d          REAL,
    copy_profit_factor_30d       REAL,
    copy_payoff_ratio_30d        REAL,
    copy_top3_profit_share       REAL,
    copy_body_net_pnl            REAL,
    copy_body_win_rate           REAL,
    rough_return_30d             REAL,
    rough_return_14d             REAL,
    rough_return_7d              REAL,
    rough_closed_pnl_30d         REAL,
    rough_closed_pnl_14d         REAL,
    rough_closed_pnl_7d          REAL,
    rough_open_loss_ratio_30d    REAL,
    rough_profit_priority        REAL,
    tier                         TEXT,
    queue_rank                   INTEGER,
    strict_status                TEXT,
    strict_first_failure         TEXT,
    evidence_json                TEXT,
    created_at                   TEXT NOT NULL,
    PRIMARY KEY (generation, addr)
);
CREATE INDEX IF NOT EXISTS idx_pre_strict_evidence_generation_queue
    ON pre_strict_evidence(generation, queue_rank, tier, addr);
CREATE INDEX IF NOT EXISTS idx_pre_strict_evidence_generation_status
    ON pre_strict_evidence(generation, status, first_failure, addr);

-- Compact, resumable shared-prefix replay evidence. Membership addresses are represented only by a stable
-- hash; full trajectories never enter this table. A generation/model/parameter change creates a new key.
CREATE TABLE IF NOT EXISTS formation_prefix_evidence (
    generation       TEXT NOT NULL,
    policy_version   TEXT NOT NULL,
    params_hash      TEXT NOT NULL,
    membership_hash  TEXT NOT NULL,
    member_count     INTEGER NOT NULL,
    evaluation_json  TEXT NOT NULL,
    replay_json      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (generation, policy_version, params_hash, membership_hash)
);
CREATE INDEX IF NOT EXISTS idx_formation_prefix_evidence_generation
    ON formation_prefix_evidence(generation, policy_version, params_hash, member_count);

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
    replay_profit_priority REAL,       -- immutable generation-specific strict-Copy profit priority
    selection_rank  INTEGER,          -- immutable final profit order within Core/Challenger
    data_status     TEXT,
    evidence_status TEXT,
    model_version   TEXT,
    policy_version  TEXT,
    acct_value      REAL,
    sector_policy_json TEXT,
    replay_copy_bt_net_pnl        REAL,
    replay_copy_bt_closed_net_pnl REAL,
    replay_copy_bt_window_start_equity REAL,
    replay_copy_bt_win_rate       REAL,
    replay_copy_bt_closed_n       INTEGER,
    replay_copy_bt_open_fill_rate REAL,
    replay_copy_bt_raw_target_open_n INTEGER,
    replay_copy_bt_small_open_excluded_n INTEGER,
    replay_copy_bt_effective_target_open_n INTEGER,
    replay_copy_bt_opened_n INTEGER,
    replay_copy_bt_raw_open_capture_rate REAL,
    replay_copy_bt_open_audit_json TEXT,
    replay_copy_bt_liquidations   INTEGER,
    replay_copy_bt_fee_drag       REAL,
    replay_copy_bt_unrealized_pnl REAL,
    replay_copy_bt_valuation_status TEXT,
    replay_copy_bt_14d_net_pnl    REAL,
    replay_copy_bt_14d_closed_net_pnl REAL,
    replay_copy_bt_14d_unrealized_pnl REAL,
    replay_copy_bt_14d_closed_n   INTEGER,
    replay_copy_bt_7d_net_pnl     REAL,
    replay_copy_bt_7d_closed_net_pnl REAL,
    replay_copy_bt_7d_window_start_equity REAL,
    replay_copy_bt_7d_unrealized_pnl REAL,
    replay_copy_bt_7d_closed_n    INTEGER,
    replay_sector_copy_json       TEXT,
    replay_params_hash            TEXT,
    replay_score_detail_json      TEXT,
    replayed_at                   TEXT,
    replay_copy_bt_max_liquidation_loss_pct REAL,
    entry_eligible                INTEGER NOT NULL DEFAULT 1,
    retention_status              TEXT NOT NULL DEFAULT 'healthy',
    retention_failure_reason      TEXT,
    retention_failure_streak      INTEGER NOT NULL DEFAULT 0,
    retained_by_hysteresis        INTEGER NOT NULL DEFAULT 0,
    selected_at     TEXT NOT NULL,
    PRIMARY KEY (generation, addr)
);
CREATE INDEX IF NOT EXISTS idx_follow_selection_generation_role
    ON follow_selection(generation, role, enabled, addr);
CREATE INDEX IF NOT EXISTS idx_follow_selection_addr_generation
    ON follow_selection(addr, generation);

-- Live-only execution-policy skips. Historical Copy replay intentionally assumes sufficient liquidity;
-- Observer records the real-time liquidity decisions here so the dashboard can explain any divergence.
CREATE TABLE IF NOT EXISTS live_policy_skip (
    day TEXT NOT NULL,
    addr TEXT NOT NULL,
    coin TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    first_ms INTEGER NOT NULL,
    last_ms INTEGER NOT NULL,
    strategy_revision_id TEXT,
    PRIMARY KEY (day, addr, coin, action, reason)
);
CREATE INDEX IF NOT EXISTS idx_live_policy_skip_addr_last
    ON live_policy_skip(addr, last_ms DESC);

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
    intent      TEXT NOT NULL DEFAULT 'active', -- active / draining / requalify
    intent_requested_at TEXT,
    intent_position_ids_json TEXT,
    intent_resolved_at TEXT,
    intent_resolution TEXT,
    pinned      INTEGER DEFAULT 0,
    pinned_at   TEXT,                -- operator Core lock order; cleared when unstarred
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
    complete    INTEGER DEFAULT 1,
    kind        TEXT DEFAULT 'complete',
    generation  TEXT,
    api_requests INTEGER DEFAULT 0,
    api_weight  INTEGER DEFAULT 0,
    outcome_reason TEXT,
    core_added INTEGER DEFAULT 0,
    core_removed INTEGER DEFAULT 0,
    core_probation INTEGER DEFAULT 0,
    core_recovered INTEGER DEFAULT 0,
    core_confirmed_demotion INTEGER DEFAULT 0,
    core_safety_exit INTEGER DEFAULT 0,
    replacement_blocked INTEGER DEFAULT 0,
    selected_source TEXT DEFAULT 'official',
    effective_source TEXT DEFAULT 'official',
    source_fallback_reason TEXT,
    source_fallback_at TEXT
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
    generation    TEXT,
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

-- Daily bounded-retention and filesystem/database growth audit. The latest result is also projected through
-- process_status('storage_guard') so operators can inspect one compact health record without scanning history.
CREATE TABLE IF NOT EXISTS storage_guard_run (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at                  TEXT NOT NULL,
    severity                    TEXT NOT NULL,
    reasons_json                TEXT NOT NULL,
    disk_total_bytes            INTEGER NOT NULL,
    disk_used_bytes             INTEGER NOT NULL,
    disk_free_bytes             INTEGER NOT NULL,
    disk_used_pct               REAL NOT NULL,
    db_main_bytes               INTEGER NOT NULL,
    db_wal_bytes                INTEGER NOT NULL,
    db_growth_bytes             INTEGER,
    db_growth_24h_bytes         INTEGER,
    db_page_bytes               INTEGER NOT NULL,
    db_freelist_bytes           INTEGER NOT NULL,
    db_active_bytes             INTEGER,
    pipeline_audit_rows         INTEGER NOT NULL,
    staging_generation_count    INTEGER NOT NULL,
    deleted_pipeline_rows       INTEGER NOT NULL DEFAULT 0,
    deleted_staging_rows        INTEGER NOT NULL DEFAULT 0,
    deleted_staging_generations INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_storage_guard_run_checked_at
    ON storage_guard_run(checked_at DESC, id DESC);
"""

PROFILE_COLS = (
    "addr,status,reason,score,n_fills,n_trades,window_days,trades_per_day,taker_frac_notl,"
    "median_hold_s,win_rate,payoff_ratio,win_pt,max_concurrent,net_pnl,roi_equity,total_notl,acct_value,perp_frac,"
    "top_coin,max_drawdown,avg_notional,age_days,"
    "last_fill_ms,lev_proxy,margin_type,cur_leverage,liq_count,liq_worst_pct,"
    "active_days,activity_ratio,median_eps,pos_day_ratio,profit_conc,hold_skew,open_underwater,"
    "max_adds_per_ep,median_adds_per_ep,retry_transition_n,rapid_same_side_retry_n,"
    "rapid_same_side_retry_rate,loss_retry_transition_n,rapid_loss_retry_n,rapid_loss_retry_rate,"
    "rapid_retry_chain_n,rapid_retry_max_chain_episodes,loss_started_retry_chain_n,"
    "loss_started_retry_chain_losing_n,loss_started_retry_chain_lose_rate,"
    "worst_loss_pct,market_type,crypto_frac,tp_move_pct,"
    "roi_total,open_unrealized,open_loss_frac,open_win_frac,bag_count,max_bag_days,max_win_days,hedge_ratio,loss_pain,"
    "net_7d,net_14d,net_30d,net_life,"
    "pf_week_pnl,pf_week_vlm,pf_mon_pnl,pf_mon_vlm,pf_equity,pf_turnover,"
    "copy_bt_net_pnl,copy_bt_closed_net_pnl,copy_bt_win_rate,copy_bt_closed_n,copy_bt_open_fill_rate,"
    "copy_bt_raw_target_open_n,copy_bt_small_open_excluded_n,copy_bt_effective_target_open_n,"
    "copy_bt_opened_n,copy_bt_raw_open_capture_rate,copy_bt_open_audit_json,"
    "copy_bt_liquidations,copy_bt_max_liquidation_loss_pct,copy_bt_max_liquidation_loss,"
    "copy_bt_max_liquidation_loss_coin,copy_bt_max_liquidation_loss_closed_at,copy_bt_fee_drag,"
    "copy_bt_unrealized_pnl,copy_bt_valuation_status,copy_bt_initial_margin_equity,copy_bt_window_start_equity,"
    "copy_bt_14d_net_pnl,copy_bt_14d_closed_net_pnl,copy_bt_14d_unrealized_pnl,copy_bt_14d_closed_n,copy_bt_14d_window_start_equity,"
    "copy_bt_7d_net_pnl,copy_bt_7d_closed_net_pnl,copy_bt_7d_unrealized_pnl,copy_bt_7d_closed_n,copy_bt_7d_window_start_equity,"
    "sector_copy_json,sector_policy_json,"
    "profile_generation,evaluated_at,data_status,evidence_status,"
    "official_perp_status,official_perp_reason,official_perp_evidence_json,"
    "official_perp_return_30d,official_perp_pnl_30d,official_perp_pnl_share,"
    "source_episode_n_30d,source_episode_n_7d,source_win_rate_30d,source_win_rate_7d,"
    "source_net_pnl_30d,source_net_pnl_7d,source_active_days_30d,source_active_days_7d,"
    "source_top3_profit_share,source_body_after_top3_n,source_body_after_top3_win_rate,"
    "source_body_after_top3_net_pnl,source_gross_profit_30d,source_gross_loss_30d,"
    "source_profit_factor_30d,source_payoff_ratio_30d,source_quality_score,rough_copy_score,last_copyable_open_ms,"
    "open_events_7d,open_events_30d,actionable_open_events_7d,actionable_open_events_30d,"
    "open_days_30d,open_probability_48h,open_position_count,material_open_count,"
    "raw_quality_score,copy_evidence_days,execution_score,"
    "selection_marginal_utility,model_coverage,oos_net_pnl,oos_max_drawdown,oos_cvar95,"
    "actionable_open_rate,capacity_fit,"
    "copy_path_risk_status,copy_intratrade_max_drawdown,copy_max_underwater_hours,"
    "copy_loss_over_5_time_ratio,copy_deep_bag_event_n,copy_failed_deep_bag_n,"
    "copy_deep_bag_recovery_rate,copy_max_deep_bag_hours,copy_current_open_loss_frac,copy_current_bag_hours,"
    "copy_bt_gross_profit,copy_bt_gross_loss,copy_bt_profit_factor,copy_bt_payoff_ratio,"
    "copy_bt_top3_profit_share,copy_bt_body_after_top3_n,copy_bt_body_after_top3_win_rate,"
    "copy_bt_body_after_top3_net_pnl,"
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

-- Durable signal inbox for real-money execution.  Receipt, policy decision and
-- order/ledger completion are separate states: a fetched target fill is never
-- considered handled merely because it reached SQLite.  ``payload_json`` is
-- bounded to one normalized Hyperliquid fill and contains no credential data.
CREATE TABLE IF NOT EXISTS execution_signal (
    signal_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    addr            TEXT NOT NULL,
    coin            TEXT NOT NULL,
    tid             INTEGER NOT NULL,
    source_time_ms  INTEGER NOT NULL,
    source_order_id TEXT,
    payload_json    TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    next_attempt_ms INTEGER NOT NULL DEFAULT 0,
    decision_code   TEXT,
    last_error      TEXT,
    received_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,
    UNIQUE(mode, session_id, addr, tid)
);
CREATE INDEX IF NOT EXISTS idx_execution_signal_pending
    ON execution_signal(mode, session_id, state, next_attempt_ms, signal_id);
CREATE INDEX IF NOT EXISTS idx_execution_signal_episode_order
    ON execution_signal(mode, session_id, addr, coin, source_time_ms, signal_id);
CREATE INDEX IF NOT EXISTS idx_execution_signal_status_completed
    ON execution_signal(state, completed_at);

-- Per-session source cursor.  Live restarts resume from the last successfully
-- journalled API window instead of resetting every target to process start.
CREATE TABLE IF NOT EXISTS observer_target_cursor (
    mode         TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    addr         TEXT NOT NULL,
    last_fill_ms INTEGER NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY(mode, session_id, addr)
);

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
    status         TEXT,                 -- open / closed / gap_closed / liquidated / tail_closed
    master_open_ms INTEGER, master_open_px REAL, master_peak_sz REAL,
    master_leverage REAL, master_margin REAL,     -- target's leverage + margin captured AT OPEN
    master_open_notional REAL,                    -- cumulative source opening order used as add-ratio anchor
    leverage REAL, margin REAL, notional REAL,    -- our sizing (margin = 2% of available at open)
    entry_px REAL, size REAL, rem_size REAL,       -- our fill px, cumulative followed size, remaining
    peak_size REAL,                                -- historical peak live size; tail exits use rem/peak
    master_current_sz REAL,                        -- latest observed target size; includes ignored tiny reductions
    smart_tp_armed INTEGER DEFAULT 0,
    smart_tp_stage INTEGER DEFAULT 0,
    smart_tp_peak_pnl REAL DEFAULT 0,
    smart_tp_base_size REAL,
    smart_tp_master_anchor REAL,
    liq_px REAL,                                   -- isolated liquidation price (loss = margin)
    realized_pnl REAL DEFAULT 0,                   -- accumulated realized PnL on this position
    add_count INTEGER DEFAULT 0,                   -- follow-on adds taken (capped at MAX_ADDS)
    mae_pct REAL DEFAULT 0, was_liq INTEGER DEFAULT 0, num_actions INTEGER DEFAULT 0,
    opened_at TEXT, closed_at TEXT,
    strategy_revision_id TEXT,
    opening_account_equity REAL
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

-- Mode-scoped replacement for the legacy cooldown table above.  A Paper loss
-- exit must never suppress a Live entry (or vice versa).
CREATE TABLE IF NOT EXISTS execution_manual_close_cooldown (
    mode       TEXT NOT NULL,
    addr       TEXT NOT NULL,
    coin       TEXT NOT NULL,
    pos_id     INTEGER,
    reason     TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (mode, addr, coin)
);
CREATE INDEX IF NOT EXISTS idx_execution_manual_close_cooldown_expires
    ON execution_manual_close_cooldown(mode, expires_at);

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

-- Live strategy journal.  These tables intentionally mirror the Paper shape
-- so the signal/decision engine can be shared, while every monetary mutation
-- is driven by confirmed exchange fills through the execution audit tables.
-- They never share rows with the Paper ledger.
CREATE TABLE IF NOT EXISTS live_copy_account (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance REAL,
    balance         REAL,
    available       REAL,
    equity_projection_version INTEGER NOT NULL DEFAULT 2,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS live_copy_position (
    pos_id INTEGER PRIMARY KEY AUTOINCREMENT,
    addr TEXT, coin TEXT, side TEXT, status TEXT,
    master_open_ms INTEGER, master_open_px REAL, master_peak_sz REAL,
    master_leverage REAL, master_margin REAL, master_open_notional REAL,
    leverage REAL, margin REAL, notional REAL,
    entry_px REAL, size REAL, rem_size REAL, peak_size REAL,
    master_current_sz REAL,
    smart_tp_armed INTEGER DEFAULT 0, smart_tp_stage INTEGER DEFAULT 0,
    smart_tp_peak_pnl REAL DEFAULT 0, smart_tp_base_size REAL,
    smart_tp_master_anchor REAL, liq_px REAL,
    realized_pnl REAL DEFAULT 0, add_count INTEGER DEFAULT 0,
    mae_pct REAL DEFAULT 0, was_liq INTEGER DEFAULT 0, num_actions INTEGER DEFAULT 0,
    opened_at TEXT, closed_at TEXT, strategy_revision_id TEXT,
    opening_account_equity REAL, mark_px REAL, unrealized_pnl REAL, open_lag_sec REAL
);
CREATE INDEX IF NOT EXISTS idx_live_cp_status ON live_copy_position(status);
CREATE INDEX IF NOT EXISTS idx_live_cp_addr ON live_copy_position(addr);
CREATE INDEX IF NOT EXISTS idx_live_cp_status_opened ON live_copy_position(status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_cp_closed ON live_copy_position(closed_at DESC) WHERE status!='open';
CREATE TABLE IF NOT EXISTS live_copy_action (
    act_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pos_id INTEGER, addr TEXT, coin TEXT, ts INTEGER, recv_ms INTEGER,
    action TEXT, master_oid INTEGER, master_px REAL, master_sz_delta REAL,
    master_pos_after REAL, our_qty_delta REAL, our_px REAL,
    realized_pnl REAL, slippage_bps REAL, strategy_revision_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_ca_oid ON live_copy_action(master_oid);
CREATE INDEX IF NOT EXISTS idx_live_ca_pos ON live_copy_action(pos_id);
CREATE INDEX IF NOT EXISTS idx_live_ca_pos_act ON live_copy_action(pos_id, action, act_id);

-- ===== Dashboard layer (control plane) =====
-- The dashboard NEVER writes business tables directly. All writes go here as commands consumed by
-- Observer/Scanner (single-writer invariant). Read side: process_status / scan_progress / params.

-- QuickNode credential health is mutated only by the protected collection-control worker and scanner.
-- The endpoint itself never enters SQLite; it remains in the protected secret file/systemd credential.
CREATE TABLE IF NOT EXISTS collection_source_control (
    id                         INTEGER PRIMARY KEY CHECK (id = 1),
    quicknode_configured       INTEGER NOT NULL DEFAULT 0,
    quicknode_status           TEXT NOT NULL DEFAULT 'missing',
    quicknode_verified_at      TEXT,
    quicknode_last_success_at  TEXT,
    quicknode_error_code       TEXT,
    quicknode_error_at         TEXT,
    updated_at                 TEXT
);

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
CREATE INDEX IF NOT EXISTS idx_commands_status_created
    ON commands(status, created_at);

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
    selected_source    TEXT DEFAULT 'official',
    effective_source   TEXT DEFAULT 'official',
    source_fallback_reason TEXT,
    source_fallback_at TEXT,
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
    backfill_start_ms INTEGER,
    backfill_cursor_ms INTEGER,
    updated_at        TEXT
);

-- High-confidence collection blacklist. Only durable automated/un-copyable behaviour belongs here;
-- temporary economics, data failures, Heavy-DCA and portfolio-shape decisions remain recoverable.
-- Keeping this separate from ``profile`` lets every future scan subtract the address before Portfolio or
-- fill-history API calls while retaining one compact, auditable decision row.
CREATE TABLE IF NOT EXISTS wallet_scan_blacklist (
    addr          TEXT PRIMARY KEY,
    reason        TEXT NOT NULL,
    evidence_json TEXT,
    generation    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_scan_blacklist_reason_updated
    ON wallet_scan_blacklist(reason, updated_at DESC);

-- UI-tunable strategy parameters. Seeded from code defaults (hyper/params.py); the operator edits via
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

-- ===== Paper / Live execution control and audit =====
-- Dashboard writes requests only through ``commands``. Hyperliquid execution workers own these tables.
CREATE TABLE IF NOT EXISTS execution_control (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    selected_mode      TEXT NOT NULL DEFAULT 'paper',
    state              TEXT NOT NULL DEFAULT 'paper',
    active_session_id  TEXT,
    canary_unlocked    INTEGER NOT NULL DEFAULT 0,
    last_error_code    TEXT,
    last_error_at      TEXT,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_lease (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    owner          TEXT NOT NULL,
    acquired_at    TEXT NOT NULL,
    heartbeat_at   TEXT NOT NULL,
    expires_at_ms  INTEGER NOT NULL
);

-- Ciphertext-only Agent credential records. API readers must never select ``envelope_json``.
CREATE TABLE IF NOT EXISTS execution_credential (
    network          TEXT PRIMARY KEY,
    account_address  TEXT NOT NULL,
    agent_address    TEXT NOT NULL,
    envelope_json    TEXT NOT NULL,
    wrap_key_id      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'encrypted',
    valid_until      TEXT,
    verified_at      TEXT,
    error_code       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Replaceable read-only exchange snapshot populated when a credential is verified/refreshed.
-- It powers pre-session account display without creating a Live session or initializing the Live ledger.
CREATE TABLE IF NOT EXISTS execution_account_preview (
    network          TEXT PRIMARY KEY,
    account_address  TEXT NOT NULL,
    equity           REAL NOT NULL DEFAULT 0,
    available        REAL NOT NULL DEFAULT 0,
    margin_used      REAL NOT NULL DEFAULT 0,
    unrealized_pnl   REAL NOT NULL DEFAULT 0,
    position_count   INTEGER NOT NULL DEFAULT 0,
    open_order_count INTEGER NOT NULL DEFAULT 0,
    equity_projection_version INTEGER NOT NULL DEFAULT 2,
    observed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_preflight (
    preflight_id       TEXT PRIMARY KEY,
    network            TEXT NOT NULL,
    account_address    TEXT NOT NULL,
    agent_address      TEXT NOT NULL,
    strategy_revision  TEXT NOT NULL,
    snapshot_hash      TEXT NOT NULL,
    status             TEXT NOT NULL,
    code               TEXT NOT NULL,
    equity             REAL NOT NULL DEFAULT 0,
    available          REAL NOT NULL DEFAULT 0,
    sizing_equity      REAL NOT NULL DEFAULT 0,
    position_count     INTEGER NOT NULL DEFAULT 0,
    open_order_count   INTEGER NOT NULL DEFAULT 0,
    details_json       TEXT,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    consumed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_execution_preflight_created
    ON execution_preflight(created_at DESC);

CREATE TABLE IF NOT EXISTS execution_session (
    session_id           TEXT PRIMARY KEY,
    mode                 TEXT NOT NULL,
    network              TEXT NOT NULL,
    state                TEXT NOT NULL,
    account_address      TEXT NOT NULL,
    agent_address        TEXT NOT NULL,
    strategy_revision    TEXT NOT NULL,
    preflight_id         TEXT,
    sizing_anchor        REAL NOT NULL,
    margin_equity_pct    REAL NOT NULL,
    sizing_equity        REAL NOT NULL,
    canary               INTEGER NOT NULL DEFAULT 1,
    canary_margin_cap    REAL,
    started_at           TEXT NOT NULL,
    stopped_at           TEXT,
    stop_reason          TEXT,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_session_state
    ON execution_session(state, started_at DESC);

CREATE TABLE IF NOT EXISTS execution_order_intent (
    cloid                TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL,
    strategy_revision    TEXT NOT NULL,
    source_address       TEXT,
    source_fill_id       TEXT,
    source_order_id      TEXT,
    source_time_ms       INTEGER,
    action_seq           INTEGER NOT NULL,
    action               TEXT NOT NULL,
    coin                 TEXT NOT NULL,
    side                 TEXT NOT NULL,
    reduce_only          INTEGER NOT NULL,
    leverage             REAL,
    requested_size       REAL NOT NULL,
    requested_limit_px   REAL NOT NULL,
    state                TEXT NOT NULL,
    oid                  INTEGER,
    filled_size          REAL NOT NULL DEFAULT 0,
    average_px           REAL,
    error_code           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_intent_session_state
    ON execution_order_intent(session_id, state, created_at);
CREATE INDEX IF NOT EXISTS idx_execution_intent_oid
    ON execution_order_intent(oid);

CREATE TABLE IF NOT EXISTS execution_order_attempt (
    attempt_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    cloid             TEXT NOT NULL,
    attempt_no        INTEGER NOT NULL,
    request_json      TEXT NOT NULL,
    response_json     TEXT,
    transport_status  TEXT NOT NULL,
    error_code        TEXT,
    started_at        TEXT NOT NULL,
    completed_at      TEXT,
    UNIQUE(cloid, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_execution_attempt_cloid
    ON execution_order_attempt(cloid, attempt_no);

CREATE TABLE IF NOT EXISTS execution_fill (
    network          TEXT NOT NULL,
    tid              TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    cloid            TEXT,
    oid              INTEGER,
    coin             TEXT NOT NULL,
    side             TEXT,
    size             REAL NOT NULL,
    px               REAL NOT NULL,
    fee              REAL NOT NULL DEFAULT 0,
    closed_pnl       REAL NOT NULL DEFAULT 0,
    fill_time_ms     INTEGER NOT NULL,
    raw_json         TEXT,
    created_at       TEXT NOT NULL,
    PRIMARY KEY(network, tid)
);
CREATE INDEX IF NOT EXISTS idx_execution_fill_session_time
    ON execution_fill(session_id, fill_time_ms);
CREATE INDEX IF NOT EXISTS idx_execution_fill_cloid
    ON execution_fill(cloid);

-- The exchange is authoritative. This is a replaceable projection used for reconciliation and UI only.
CREATE TABLE IF NOT EXISTS execution_position_projection (
    session_id        TEXT NOT NULL,
    dex               TEXT NOT NULL,
    coin              TEXT NOT NULL,
    signed_size       REAL NOT NULL,
    entry_px          REAL,
    position_value    REAL,
    margin_used       REAL,
    leverage_type     TEXT,
    leverage_value    REAL,
    unrealized_pnl    REAL,
    liquidation_px    REAL,
    observed_at       TEXT NOT NULL,
    PRIMARY KEY(session_id, dex, coin)
);

CREATE TABLE IF NOT EXISTS execution_account_snapshot (
    snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    equity         REAL NOT NULL,
    available      REAL NOT NULL,
    margin_used    REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    equity_projection_version INTEGER NOT NULL DEFAULT 2,
    observed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_account_session_time
    ON execution_account_snapshot(session_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_execution_account_observed_at
    ON execution_account_snapshot(observed_at);

CREATE TABLE IF NOT EXISTS execution_reconcile_checkpoint (
    checkpoint_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL,
    status              TEXT NOT NULL,
    exchange_hash       TEXT,
    position_count      INTEGER NOT NULL DEFAULT 0,
    open_order_count    INTEGER NOT NULL DEFAULT 0,
    unknown_positions   INTEGER NOT NULL DEFAULT 0,
    unknown_orders      INTEGER NOT NULL DEFAULT 0,
    details_json        TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_reconcile_session
    ON execution_reconcile_checkpoint(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_reconcile_status_created
    ON execution_reconcile_checkpoint(status, created_at);

"""


# Non-destructive column adds for EXISTING DBs (CREATE IF NOT EXISTS won't add columns to a table that
# already exists). Idempotent: on a fresh DB the column is already in the CREATE → ALTER errors → ignored.
_MIGRATIONS = (
    "ALTER TABLE pipeline_audit ADD COLUMN generation TEXT",
    "ALTER TABLE storage_guard_run ADD COLUMN db_active_bytes INTEGER",
    "ALTER TABLE profile ADD COLUMN market_type TEXT",
    "ALTER TABLE profile ADD COLUMN crypto_frac REAL DEFAULT 1",
    "ALTER TABLE watchlist ADD COLUMN market_type TEXT",
    # Dashboard: per-position realtime fields (Observer persists each heartbeat / at open) so the
    # read-only API can serve mark/upnl/lag without its own live book.
    "ALTER TABLE copy_position ADD COLUMN mark_px REAL",
    "ALTER TABLE copy_position ADD COLUMN unrealized_pnl REAL",
    "ALTER TABLE copy_position ADD COLUMN open_lag_sec REAL",
    "ALTER TABLE live_copy_account ADD COLUMN available REAL",
    # Version 1 stored Unified spot USDC total plus position uPnL even though that total already includes
    # uPnL. Existing rows are normalized once after these additive columns are installed; all new writes
    # explicitly persist version 2.
    "ALTER TABLE live_copy_account ADD COLUMN equity_projection_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE execution_account_preview ADD COLUMN equity_projection_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE execution_account_snapshot ADD COLUMN equity_projection_version INTEGER NOT NULL DEFAULT 1",
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
    "ALTER TABLE profile ADD COLUMN copy_bt_raw_target_open_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_small_open_excluded_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_effective_target_open_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_opened_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_raw_open_capture_rate REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_open_audit_json TEXT",
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
    # Stop/reopen trial-loop evidence. Stored so cache-only regate applies the same structural contract.
    "ALTER TABLE profile ADD COLUMN retry_transition_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN rapid_same_side_retry_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN rapid_same_side_retry_rate REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN loss_retry_transition_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN rapid_loss_retry_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN rapid_loss_retry_rate REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN rapid_retry_chain_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN rapid_retry_max_chain_episodes INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN loss_started_retry_chain_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN loss_started_retry_chain_losing_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN loss_started_retry_chain_lose_rate REAL DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN profiled INTEGER",
    "ALTER TABLE scan_runs ADD COLUMN full INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN failed INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN complete INTEGER DEFAULT 1",
    "ALTER TABLE scan_runs ADD COLUMN kind TEXT DEFAULT 'complete'",
    "ALTER TABLE scan_runs ADD COLUMN generation TEXT",
    "ALTER TABLE scan_runs ADD COLUMN api_requests INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN api_weight INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN outcome_reason TEXT",
    "ALTER TABLE scan_runs ADD COLUMN core_added INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN core_removed INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN core_probation INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN core_recovered INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN core_confirmed_demotion INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN core_safety_exit INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN replacement_blocked INTEGER DEFAULT 0",
    "ALTER TABLE scan_runs ADD COLUMN selected_source TEXT DEFAULT 'official'",
    "ALTER TABLE scan_runs ADD COLUMN effective_source TEXT DEFAULT 'official'",
    "ALTER TABLE scan_runs ADD COLUMN source_fallback_reason TEXT",
    "ALTER TABLE scan_runs ADD COLUMN source_fallback_at TEXT",
    "ALTER TABLE scan_progress ADD COLUMN selected_source TEXT DEFAULT 'official'",
    "ALTER TABLE scan_progress ADD COLUMN effective_source TEXT DEFAULT 'official'",
    "ALTER TABLE scan_progress ADD COLUMN source_fallback_reason TEXT",
    "ALTER TABLE scan_progress ADD COLUMN source_fallback_at TEXT",
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
    "ALTER TABLE profile ADD COLUMN official_perp_status TEXT",
    "ALTER TABLE profile ADD COLUMN official_perp_reason TEXT",
    "ALTER TABLE profile ADD COLUMN official_perp_evidence_json TEXT",
    "ALTER TABLE profile ADD COLUMN official_perp_return_30d REAL",
    "ALTER TABLE profile ADD COLUMN official_perp_pnl_30d REAL",
    "ALTER TABLE profile ADD COLUMN official_perp_pnl_share REAL",
    "ALTER TABLE profile ADD COLUMN source_episode_n_30d INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN source_episode_n_7d INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN source_win_rate_30d REAL",
    "ALTER TABLE profile ADD COLUMN source_win_rate_7d REAL",
    "ALTER TABLE profile ADD COLUMN source_net_pnl_30d REAL",
    "ALTER TABLE profile ADD COLUMN source_net_pnl_7d REAL",
    "ALTER TABLE profile ADD COLUMN source_active_days_30d INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN source_active_days_7d INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN source_top3_profit_share REAL",
    "ALTER TABLE profile ADD COLUMN source_body_after_top3_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN source_body_after_top3_win_rate REAL",
    "ALTER TABLE profile ADD COLUMN source_body_after_top3_net_pnl REAL",
    "ALTER TABLE profile ADD COLUMN source_gross_profit_30d REAL",
    "ALTER TABLE profile ADD COLUMN source_gross_loss_30d REAL",
    "ALTER TABLE profile ADD COLUMN source_profit_factor_30d REAL",
    "ALTER TABLE profile ADD COLUMN source_payoff_ratio_30d REAL",
    "ALTER TABLE profile ADD COLUMN source_quality_score REAL",
    "ALTER TABLE profile ADD COLUMN rough_copy_score REAL",
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
    "ALTER TABLE profile ADD COLUMN copy_path_risk_status TEXT DEFAULT 'pending'",
    "ALTER TABLE profile ADD COLUMN copy_intratrade_max_drawdown REAL",
    "ALTER TABLE profile ADD COLUMN copy_max_underwater_hours REAL",
    "ALTER TABLE profile ADD COLUMN copy_loss_over_5_time_ratio REAL",
    "ALTER TABLE profile ADD COLUMN copy_deep_bag_event_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_failed_deep_bag_n INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_deep_bag_recovery_rate REAL",
    "ALTER TABLE profile ADD COLUMN copy_max_deep_bag_hours REAL",
    "ALTER TABLE profile ADD COLUMN copy_current_open_loss_frac REAL",
    "ALTER TABLE profile ADD COLUMN copy_current_bag_hours REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_gross_profit REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_gross_loss REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_profit_factor REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_payoff_ratio REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_top3_profit_share REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_body_after_top3_n INTEGER",
    "ALTER TABLE profile ADD COLUMN copy_bt_body_after_top3_win_rate REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_body_after_top3_net_pnl REAL",
    "ALTER TABLE profile ADD COLUMN copy_campaign_max_drawdown REAL",
    "ALTER TABLE profile ADD COLUMN copy_campaign_peak_positions INTEGER DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_campaign_peak_margin_pct REAL",
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
    # Immutable final-surface replay evidence published with selection. Older databases acquire these
    # columns additively so rolling readers remain backward compatible.
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_window_start_equity REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_win_rate REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_closed_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_open_fill_rate REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_raw_target_open_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_small_open_excluded_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_effective_target_open_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_opened_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_raw_open_capture_rate REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_open_audit_json TEXT",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_liquidations INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_fee_drag REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_14d_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_14d_closed_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_7d_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_7d_window_start_equity REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_7d_closed_n INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_sector_copy_json TEXT",
    "ALTER TABLE follow_selection ADD COLUMN replay_params_hash TEXT",
    "ALTER TABLE follow_selection ADD COLUMN replay_score_detail_json TEXT",
    "ALTER TABLE follow_selection ADD COLUMN replayed_at TEXT",
    "ALTER TABLE follow_selection ADD COLUMN follow_score REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_profit_priority REAL",
    "ALTER TABLE follow_selection ADD COLUMN selection_rank INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN acct_value REAL",
    "ALTER TABLE follow_selection ADD COLUMN sector_policy_json TEXT",
    "ALTER TABLE episode ADD COLUMN open_complete INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE episode ADD COLUMN n_oids INTEGER",
    "ALTER TABLE copy_position ADD COLUMN strategy_revision_id TEXT",
    "ALTER TABLE copy_action ADD COLUMN strategy_revision_id TEXT",
    "ALTER TABLE copy_position ADD COLUMN peak_size REAL",
    "ALTER TABLE copy_position ADD COLUMN master_current_sz REAL",
    "ALTER TABLE copy_position ADD COLUMN master_open_notional REAL",
    "ALTER TABLE copy_position ADD COLUMN smart_tp_armed INTEGER DEFAULT 0",
    "ALTER TABLE copy_position ADD COLUMN smart_tp_stage INTEGER DEFAULT 0",
    "ALTER TABLE copy_position ADD COLUMN smart_tp_peak_pnl REAL DEFAULT 0",
    "ALTER TABLE copy_position ADD COLUMN smart_tp_base_size REAL",
    "ALTER TABLE copy_position ADD COLUMN smart_tp_master_anchor REAL",
    # Canonical Copy economic PnL = closed net + terminal open-position mark-to-market.
    "ALTER TABLE profile ADD COLUMN copy_bt_unrealized_pnl REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_valuation_status TEXT DEFAULT 'complete'",
    "ALTER TABLE profile ADD COLUMN copy_bt_initial_margin_equity REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_window_start_equity REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_14d_unrealized_pnl REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_14d_window_start_equity REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_7d_unrealized_pnl REAL DEFAULT 0",
    "ALTER TABLE profile ADD COLUMN copy_bt_7d_window_start_equity REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_max_liquidation_loss_pct REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_max_liquidation_loss REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_max_liquidation_loss_coin TEXT",
    "ALTER TABLE profile ADD COLUMN copy_bt_max_liquidation_loss_closed_at INTEGER",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_unrealized_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_valuation_status TEXT",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_14d_unrealized_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_7d_unrealized_pnl REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_closed_net_pnl REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_14d_closed_net_pnl REAL",
    "ALTER TABLE profile ADD COLUMN copy_bt_7d_closed_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_closed_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_14d_closed_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_7d_closed_net_pnl REAL",
    "ALTER TABLE follow_selection ADD COLUMN replay_copy_bt_max_liquidation_loss_pct REAL",
    "ALTER TABLE follow_selection ADD COLUMN entry_eligible INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE follow_selection ADD COLUMN retention_status TEXT NOT NULL DEFAULT 'healthy'",
    "ALTER TABLE follow_selection ADD COLUMN retention_failure_reason TEXT",
    "ALTER TABLE follow_selection ADD COLUMN retention_failure_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE follow_selection ADD COLUMN retained_by_hysteresis INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE wallet_registry ADD COLUMN core_retention_status TEXT NOT NULL DEFAULT 'healthy'",
    "ALTER TABLE wallet_registry ADD COLUMN core_retention_fail_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE wallet_registry ADD COLUMN core_retention_reason TEXT",
    "ALTER TABLE wallet_registry ADD COLUMN core_retention_started_generation TEXT",
    "ALTER TABLE wallet_registry ADD COLUMN last_core_retention_generation TEXT",
    "ALTER TABLE target_controls ADD COLUMN pinned_at TEXT",
    "ALTER TABLE wallet_registry ADD COLUMN risk_level TEXT NOT NULL DEFAULT 'normal'",
    "ALTER TABLE wallet_registry ADD COLUMN risk_reasons_json TEXT",
    "ALTER TABLE wallet_registry ADD COLUMN risk_confirmation_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE wallet_registry ADD COLUMN risk_first_confirmed_at TEXT",
    "ALTER TABLE wallet_registry ADD COLUMN risk_assessed_at TEXT",
    "ALTER TABLE wallet_registry ADD COLUMN risk_block_reason TEXT",
    "ALTER TABLE target_controls ADD COLUMN intent TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE target_controls ADD COLUMN intent_requested_at TEXT",
    "ALTER TABLE target_controls ADD COLUMN intent_position_ids_json TEXT",
    "ALTER TABLE target_controls ADD COLUMN intent_resolved_at TEXT",
    "ALTER TABLE target_controls ADD COLUMN intent_resolution TEXT",
    "ALTER TABLE copy_position ADD COLUMN opening_account_equity REAL",
    "ALTER TABLE fill_cache_state ADD COLUMN backfill_start_ms INTEGER",
    "ALTER TABLE fill_cache_state ADD COLUMN backfill_cursor_ms INTEGER",
)


def connect(path: str, *schemas: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)  # used across the scanner's
    db.execute("PRAGMA journal_mode=WAL")                            # worker threads (writes are
    db.execute("PRAGMA busy_timeout=30000")                          # serialized by a lock)
    db.execute(f"PRAGMA journal_size_limit={int(config.SQLITE_JOURNAL_SIZE_LIMIT_BYTES)}")
    for s in schemas:
        db.executescript(s)
    # Dashboard, Observer and a maintenance CLI can start at the same moment after deploy.  Serialize
    # schema inspection + ALTERs so two fresh processes cannot both decide a column is missing and race.
    db.execute("BEGIN IMMEDIATE")
    try:
        _apply_migrations(db)
        _migrate_unified_equity_projection(db)
        _retire_maker_shadow(db)
        _retire_obsolete_selection_state(db)
        _migrate_episode_seq(db)
        _migrate_target_control_intents(db)
        _migrate_risk_compatibility(db)
        _migrate_execution_cooldowns(db)
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
    if "pipeline_audit" in tables:
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_audit_generation_stage_id "
            "ON pipeline_audit(generation, stage, id DESC)"
        )


def _migrate_unified_equity_projection(db: sqlite3.Connection) -> None:
    """Normalize snapshots written before Unified USDC total was treated as total equity.

    Version 1 added position uPnL to the Unified spot USDC total a second time. The version column makes
    this correction idempotent and lets historical Dashboard curves stay continuous after the runtime fix.
    """
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    for table in ("execution_account_preview", "execution_account_snapshot"):
        if table not in tables:
            continue
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        required = {"equity", "unrealized_pnl", "equity_projection_version"}
        if required.issubset(columns):
            db.execute(
                f"UPDATE {table} SET equity=MAX(0.0,equity-COALESCE(unrealized_pnl,0)),"
                "equity_projection_version=2 WHERE equity_projection_version<2"
            )

    if "live_copy_account" not in tables or "execution_account_snapshot" not in tables:
        return
    account_columns = {
        row[1] for row in db.execute("PRAGMA table_info(live_copy_account)").fetchall()
    }
    if {"balance", "equity_projection_version"}.issubset(account_columns):
        db.execute(
            "UPDATE live_copy_account SET balance=COALESCE(("
            "SELECT equity FROM execution_account_snapshot ORDER BY snapshot_id DESC LIMIT 1"
            "),balance),equity_projection_version=2 WHERE equity_projection_version<2"
        )


def _migrate_target_control_intents(db: sqlite3.Connection) -> None:
    """Map legacy disabled rows to the recoverable requalification state.

    The requested-at guard keeps modern ``draining`` rows from being rewritten on a rolling restart.
    """
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "target_controls" not in tables:
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(target_controls)").fetchall()}
    if {"enabled", "intent", "intent_requested_at"}.issubset(columns):
        db.execute(
            "UPDATE target_controls SET intent='requalify' "
            "WHERE COALESCE(enabled,1)=0 AND COALESCE(intent,'active')='active' "
            "AND intent_requested_at IS NULL"
        )


def _migrate_risk_compatibility(db: sqlite3.Connection) -> None:
    """Project legacy probation rows to advisory low risk with entry permission."""
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "follow_selection" in tables:
        columns = {row[1] for row in db.execute("PRAGMA table_info(follow_selection)").fetchall()}
        if {"entry_eligible", "retention_status"}.issubset(columns):
            db.execute(
                "UPDATE follow_selection SET entry_eligible=1 "
                "WHERE retention_status IN ('probation','medium_risk')"
            )
    if "wallet_registry" in tables:
        columns = {row[1] for row in db.execute("PRAGMA table_info(wallet_registry)").fetchall()}
        if {"risk_level", "core_retention_status"}.issubset(columns):
            db.execute(
                "UPDATE wallet_registry SET risk_level='low' "
                "WHERE risk_level='normal' AND core_retention_status='probation'"
            )
            db.execute(
                "UPDATE wallet_registry SET risk_level='medium' "
                "WHERE risk_level='normal' AND core_retention_status='medium_risk'"
            )


def _migrate_execution_cooldowns(db: sqlite3.Connection) -> None:
    """Copy legacy unscoped cooldowns into every ledger they can belong to.

    Position ids are independently allocated in Paper and Live and can overlap,
    so an ambiguous legacy row is deliberately copied to both modes.  New writes
    are mode-scoped and no longer need this compatibility path.
    """
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    required = {
        "manual_close_cooldown", "execution_manual_close_cooldown",
        "copy_position", "live_copy_position",
    }
    if not required.issubset(tables):
        return
    for mode, position_table in (
        ("paper", "copy_position"), ("live", "live_copy_position"),
    ):
        db.execute(
            "INSERT OR IGNORE INTO execution_manual_close_cooldown "
            "(mode,addr,coin,pos_id,reason,created_at,expires_at) "
            "SELECT ?,c.addr,c.coin,c.pos_id,c.reason,c.created_at,c.expires_at "
            "FROM manual_close_cooldown c WHERE EXISTS ("
            f"SELECT 1 FROM {position_table} p WHERE p.pos_id=c.pos_id "
            "AND lower(p.addr)=lower(c.addr) AND lower(p.coin)=lower(c.coin))",
            (mode,),
        )
    # The legacy table is retired.  Leaving copied rows behind would resurrect a cooldown that the
    # mode-scoped runtime later cleared, because connect() intentionally makes migrations idempotent.
    db.execute("DELETE FROM manual_close_cooldown")


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
            hold_s REAL, net_pnl REAL, fee REAL, max_notl REAL, n_fills INTEGER, n_oids INTEGER,
            open_px REAL, close_px REAL, open_complete INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (addr, coin, open_ms, seq)
        );
        """
    )
    seq_expr = "COALESCE(seq, 0)" if "seq" in names else "0"
    complete_expr = "COALESCE(open_complete, 1)" if "open_complete" in names else "1"
    oids_expr = "n_oids" if "n_oids" in names else "NULL"
    db.execute(
        "INSERT OR IGNORE INTO episode "
        "(addr,coin,side,open_ms,seq,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,n_oids,"
        "open_px,close_px,open_complete) "
        f"SELECT addr,coin,side,open_ms,{seq_expr},close_ms,hold_s,net_pnl,fee,max_notl,n_fills,"
        f"{oids_expr},open_px,close_px,{complete_expr} "
        "FROM episode_migrate_old"
    )
    db.execute("DROP TABLE episode_migrate_old")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ep_addr ON episode(addr)")
