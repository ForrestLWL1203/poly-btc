"""Seed a realistic mock hl.db for eyeballing the dashboard UI end-to-end (no live engine needed).
Usage (from repo root):  python3 dashboard/web/dev/seed_mock.py data/hl_mock.db
- scores on the NATIVE 0–3 scale (so the 0–100 display normalization shows a real spread)
- equity curve with a midday drawdown then recovery
- open positions: long/short, crypto+stock, one near liquidation; rejects covering all funnel buckets
"""
import math, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from hyper import storage, params
from hyper.credentials import ensure_instance_keypair
from hyper.util import now_iso, now_ms

DB = sys.argv[1] if len(sys.argv) > 1 else "data/hl_mock.db"
db = storage.connect(DB, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
params.seed_params(db)
for t in ("account_stats", "copy_position", "copy_action", "coin_vol", "watchlist",
          "target_controls", "profile", "leaderboard", "scan_runs", "follow_selection",
          "scan_generation", "wallet_registry", "pipeline_audit", "follow_history",
          "market_risk_action", "market_risk_episode",
          "market_risk_intent", "market_risk_assessment", "market_risk_snapshot",
          "provider_balance_snapshot", "provider_credential"):
    db.execute(f"DELETE FROM {t}")

def ago(s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - s))

base = 10000.0
N = 432
for i in range(N):
    ts = ago((N - i) * 300)
    trend = base + i * 7.5
    dip = -650 * math.exp(-((i - 300) ** 2) / (2 * 40 ** 2))
    eq = trend + dip + (i % 5) * 4
    bal = eq - 240
    db.execute("INSERT INTO account_stats (ts,balance,unrealized_pnl,equity,realized_pnl_cum,roi,"
               "open_n,closed_n,win_rate,locked_margin,available,gross_notional,net_notional,fees_cum) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (ts, bal, 240.0, eq, bal - base, eq / base - 1, 4, 18, 0.61,
                2100.0, bal - 2100, 31000.0, 12500.0, 60 + i * 0.12))

for coin, sig in (("BTC", 0.021), ("ETH", 0.034), ("SOL", 0.061), ("DOGE", 0.078), ("AAPL", 0.018)):
    db.execute("INSERT INTO coin_vol (coin,sigma,sigma_fast,sigma_slow,n,updated_at) VALUES (?,?,?,?,?,?)",
               (coin, sig, sig, sig, 30, now_iso()))

W = [
    ("0x7a1111111111111111111111111111111111aa01", "steady_btc",  2.84, 1.42, 0.71, "BTC",  "crypto", -0.06, 0, 88000, 1),
    ("0x7a2222222222222222222222222222222222aa02", "eth_swing",   2.31, 0.95, 0.64, "ETH",  "crypto", -0.09, 1, 54000, 1),
    ("0x7a3333333333333333333333333333333333aa03", "macro_mix",   1.78, 0.61, 0.58, "SOL",  "mixed",  -0.12, 2, 120000, 1),
    ("0x7a4444444444444444444444444444444444aa04", "tsla_trader", 1.34, 0.40, 0.55, "AAPL", "stock",  -0.10, 1, 41000, 1),
    ("0x7a5555555555555555555555555555555555aa05", "doge_degen",  0.92, 0.33, 0.52, "DOGE", "crypto", -0.18, 3, 23000, 0),
    ("0x7a6666666666666666666666666666666666aa06", "new_lowevd",  0.47, 0.22, 0.50, "BTC",  "crypto", -0.07, 0, 31000, 1),
]
GEN = "mock-2026-07-11"
db.execute(
    "INSERT INTO scan_generation "
    "(generation,source,status,complete,publishable,is_current,started_at,published_at,"
    "leaderboard_rows,leaderboard_valid,profile_total,profile_valid,profile_complete,workset_mode,fill_mode,"
    "workset_n,deferred_n,metrics_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (GEN, "scan", "published", 1, 1, 1, ago(1800), ago(120), 46, 1, 6, 6, 1,
     "priority", "mixed", 46, 12, '{"requests":128,"estimated_weight":612,"coreRefreshSec":420,"coreDeadlineMet":true,"fullRefetch":7,"deltaRefetch":39}')
)
for rank, (addr, name, score, roi, wr, coin, mt, worst, madds, acct, en) in enumerate(W, 1):
    db.execute("INSERT INTO leaderboard (addr,display_name,account_value,mon_roi,is_candidate,fetched_at,generation) "
               "VALUES (?,?,?,?,1,?,?)", (addr, name, acct, roi, now_iso(), GEN))
    db.execute("INSERT INTO profile (addr,status,reason,score,n_trades,win_rate,roi_equity,acct_value,"
               "top_coin,market_type,worst_loss_pct,median_adds_per_ep,profile_generation,evaluated_at,"
               "data_status,evidence_status,last_copyable_open_ms,open_events_7d,actionable_open_events_7d,"
               "actionable_open_rate,capacity_fit,"
               "oos_net_pnl,oos_max_drawdown,oos_cvar95,selection_marginal_utility,"
               "copy_bt_net_pnl,copy_bt_closed_n,copy_bt_14d_net_pnl,copy_bt_14d_closed_n,"
               "copy_bt_7d_net_pnl,copy_bt_7d_closed_n,copy_expected_return,copy_return_lcb,"
               "copy_return_volatility,copy_positive_probability,copy_evidence_days,"
               "copy_recent_return_14d,copy_recent_return_7d,copy_risk_score,execution_score,open_probability_48h) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (addr, "active", "ok", score, 40, wr, roi, acct, coin, mt, worst, madds, GEN, ago(300),
                "valid", "qualified" if rank <= 4 else "thin", now_ms() - rank * 5 * 3600_000,
                19 - rank * 2, 19 - rank * 2,
                max(.62, .92 - rank * .04), max(.75, .96 - rank * .025),
                1450 - rank * 170, .025 + rank * .004, -120 - rank * 18, .18 - rank * .025,
                1800 - rank * 150, 24 - rank, 900 - rank * 70, 12 - rank, 420 - rank * 35, 7 - min(rank, 2),
                .085 - rank * .006, .032 - rank * .003, .09 + rank * .005, .94 - rank * .025,
                18 - rank, .07 - rank * .005, .06 - rank * .004, .91 - rank * .025,
                .93 - rank * .02, .88 - rank * .04))
    db.execute("INSERT INTO watchlist (rank,addr,display_name,score,roi_equity,win_rate,top_coin,"
               "market_type,acct_value,generation,profile_generation,evaluated_at,data_status,evidence_status,updated_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (rank, addr, name, score, roi, wr, coin, mt, acct, GEN, GEN, ago(300), "valid",
                "qualified" if rank <= 4 else "thin", now_iso()))
    db.execute("INSERT INTO target_controls (addr,enabled,updated_at) VALUES (?,?,?)", (addr, en, now_iso()))
    role = "core" if rank <= 3 else "challenger"
    db.execute("INSERT INTO follow_selection "
               "(generation,addr,role,enabled,reason,utility,data_status,evidence_status,model_version,policy_version,selected_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (GEN, addr, role, en, "core_keep" if role == "core" else "challenger_evidence",
                .18 - rank * .025, "valid", "qualified" if rank <= 4 else "thin",
                "selection-vnext-1", "copy-policy-mock", ago(120)))
    if role == "core":
        db.execute("INSERT INTO follow_history (addr,first_followed_at,last_followed_at,last_followed_score) "
                   "VALUES (?,?,?,?)", (addr, ago(7 * 86400), ago(120), score))

for i in range(40):
    db.execute("INSERT INTO leaderboard (addr,display_name,account_value,mon_roi,is_candidate,fetched_at,generation) "
               "VALUES (?,?,?,?,1,?,?)", (f"0xcand{i:036x}", f"c{i}", 12000, 0.1, now_iso(), GEN))
reasons = (["inactive"] * 9 + ["spot_dominant"] * 4 + ["bot_frequency"] * 3 + ["irregular"] * 5 +
           ["grid_dca"] * 8 + ["blowup_loss"] * 6 + ["not_profitable"] * 3 + ["hit_page_cap"] * 2)
for i, reason in enumerate(reasons):
    db.execute("INSERT INTO profile (addr,status,reason,score) VALUES (?,?,?,?)", (f"0xrej{i:037x}", "rejected", reason, 0.0))
for i, sc in enumerate([0.3, 0.55, 0.7, 0.85, 1.05, 1.15, 1.25, 1.55, 1.95, 2.45, 2.7]):
    db.execute("INSERT INTO profile (addr,status,reason,score) VALUES (?,?,?,?)", (f"0xext{i:037x}", "active", "ok", sc))

O = [
    (W[0][0], "BTC",  "long",  64200, 8,  3200, 25600, 66150, 57000),
    (W[1][0], "ETH",  "short", 3420,  5,  1500, 7500,  3355,  3650),
    (W[2][0], "SOL",  "long",  172.0, 10, 1800, 18000, 158.4, 156.0),
    (W[3][0], "AAPL", "long",  214.5, 3,  1200, 3600,  219.8, 150.0),
]
open_ids = []
for addr, coin, side, entry, lev, margin, notl, mark, liq in O:
    size = notl / entry
    sgn = 1 if side == "long" else -1
    upnl = size * (mark - entry) * sgn
    cur = db.execute("INSERT INTO copy_position (addr,coin,side,status,entry_px,leverage,margin,notional,"
               "size,rem_size,liq_px,mark_px,unrealized_pnl,open_lag_sec,opened_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (addr, coin, side, "open", entry, lev, margin, notl, size, size, liq, mark, upnl,
                round(0.8 + (lev % 3) * 0.4, 1), ago(3600 * (2 + lev % 4))))
    open_ids.append(cur.lastrowid)

C = [
    (W[0][0], "BTC",  "long",  980.0, 26000), (W[0][0], "ETH", "short", 210.0, 8000),
    (W[1][0], "ETH",  "long",  -180.0, 5400), (W[1][0], "SOL", "long",  430.0, 14000),
    (W[2][0], "SOL",  "short", -95.0, 3600),  (W[2][0], "BTC", "long",  620.0, 30000),
    (W[3][0], "AAPL", "long",  150.0, 20000), (W[4][0], "DOGE","long", -260.0, 4200),
]
closed_ids = []
for addr, coin, side, pnl, dur in C:
    cur = db.execute("INSERT INTO copy_position (addr,coin,side,status,entry_px,realized_pnl,opened_at,closed_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (addr, coin, side, "closed", 100, pnl, ago(dur + 1200), ago(1200)))
    closed_ids.append(cur.lastrowid)

# AI risk radar: two consecutive bearish assessments confirm a long-entry shadow block.
assessed_prev = (now_ms() // 900_000 - 1) * 900_000
assessed_now = assessed_prev + 900_000
db.execute("INSERT INTO market_risk_assessment (assessed_for_ms,model,prompt_version,status,raw_bullish_score,"
           "bullish_score,bearish_score,confidence,regime,risky_direction,block_side,active_block,valid_until_ms,"
           "reason,evidence_json,invalidation_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (assessed_prev, "deepseek-v4-pro", "risk-radar-v1", "ok", 18, 24, 76, 82, "risk_off", "bearish",
            "long", 0, assessed_prev + 1200_000, "BTC 与 ETH 同步跌破 1h 趋势结构。", '["1h EMA 空头排列"]',
            '["BTC 重回 1h EMA21"]', ago(900)))
prev_aid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
db.execute("INSERT INTO market_risk_assessment (assessed_for_ms,model,prompt_version,status,raw_bullish_score,"
           "bullish_score,bearish_score,confidence,regime,risky_direction,block_side,confirmation_mode,active_block,"
           "previous_assessment_id,valid_until_ms,reason,evidence_json,invalidation_json,latency_ms,created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (assessed_now, "deepseek-v4-pro", "risk-radar-v1", "ok", 12, 19, 81, 86, "risk_off", "bearish", "long",
            "steady", 1, prev_aid, assessed_now + 1200_000, "抛压延续，BTC/ETH 多周期结构仍偏空。",
            '["15m MACD 下行","BTC/ETH 1h EMA 空头排列"]', '["15m CVD 转正并收复 EMA21"]', 1280, ago(30)))
current_aid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
db.execute("INSERT OR REPLACE INTO market_risk_state (id,mode,status,current_assessment_id,block_side,risk_score,"
           "confirmation_mode,valid_until_ms,connection_status,last_assessed_at,updated_at) VALUES (1,'shadow','running',?,?,?,?,?,'connected',?,?)",
           (current_aid, "long", 81, "steady", assessed_now + 1200_000, ago(30), now_iso()))

intent_rows = [
    (open_ids[0], O[0][0], "BTC", "long", 81, 1, "open", None, None),
    (open_ids[2], O[2][0], "SOL", "long", 81, 1, "open", None, None),
    (closed_ids[2], C[2][0], "ETH", "long", 81, 1, "resolved", C[2][3], "avoided_loss"),
    (closed_ids[0], C[0][0], "BTC", "long", 81, 1, "resolved", C[0][3], "missed_profit"),
]
for pos_id, addr, coin, side, risk, block, status, pnl, outcome in intent_rows:
    db.execute("INSERT INTO market_risk_intent (pos_id,addr,coin,side,assessment_id,risk_score,would_block,confirmation_mode,"
               "decision_reason,opened_at,status,realized_pnl,net_pnl,outcome,resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (pos_id, addr, coin, side, current_aid, risk, block, "steady", "confirmed directional conflict",
                ago(7200), status, pnl, pnl, outcome, ago(1200) if status == "resolved" else None))

# V2 action-level AI counterfactuals: blocked opens stay latent; later allowed adds can enter independently.
episode_rows = [
    (open_ids[0], O[0][0], "BTC", "long", "open", 1, 1, 1, 1, .08, 65500, -2.1, None, None, None, None),
    (open_ids[2], O[2][0], "SOL", "long", "open", 1, 0, 1, 0, 0, None, 0, None, None, None, None),
    (closed_ids[2], C[2][0], "ETH", "long", "resolved", 1, 1, 1, 1, 0, None, 25, -180, 25, 205, "improved"),
    (closed_ids[0], C[0][0], "BTC", "long", "resolved", 1, 1, 1, 1, 0, None, 300, 980, 300, -680, "harmed"),
]
for row in episode_rows:
    pos_id, addr, coin, side, status, entry_blocked, delayed, blocked, allowed, qty, entry, shadow_realized, baseline, shadow, benefit, result = row
    eid = db.execute("INSERT INTO market_risk_episode (pos_id,addr,coin,side,status,entry_blocked,delayed_entry,"
                     "blocked_entries,allowed_entries,shadow_qty,shadow_entry_px,shadow_realized_pnl,baseline_net_pnl,"
                     "shadow_net_pnl,net_benefit,outcome,opened_at,resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pos_id, addr, coin, side, status, entry_blocked, delayed, blocked, allowed, qty, entry,
                      shadow_realized, baseline, shadow, benefit, result, ago(7200), ago(1200) if status == "resolved" else None)).lastrowid
    open_px = {"BTC": 64200, "ETH": 3420, "SOL": 172}.get(coin, 100)
    add_px = {"BTC": 65500, "ETH": 3500, "SOL": 180}.get(coin, open_px)
    close_px = {"BTC": 66000, "ETH": 3600, "SOL": 160}.get(coin, open_px)
    db.execute("INSERT INTO market_risk_action (episode_id,pos_id,decision_group,source_oid,action,side,assessment_id,"
               "risk_score,would_block,confirmation_mode,decision,decision_reason,baseline_qty_delta,baseline_px,"
               "shadow_qty_delta,shadow_px,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (eid, pos_id, "open:oid:1", 1, "open", side, current_aid, 81, 1, "steady", "blocked_open",
                "confirmed directional conflict", .2 if side == "long" else -.2, open_px,
                0, None, ago(7200)))
    if allowed:
        decision = "delayed_entry" if delayed else "allowed_add"
        db.execute("INSERT INTO market_risk_action (episode_id,pos_id,decision_group,source_oid,action,side,assessment_id,"
                   "risk_score,would_block,decision,decision_reason,baseline_qty_delta,baseline_px,shadow_qty_delta,"
                   "shadow_px,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (eid, pos_id, "add:oid:2", 2, "add", side, current_aid, 63, 0, decision,
                    "no confirmed conflict", .08 if side == "long" else -.08, add_px,
                    .08 if side == "long" else -.08, add_px, ago(3600)))
    if status == "resolved":
        db.execute("INSERT INTO market_risk_action (episode_id,pos_id,decision_group,source_oid,action,side,would_block,"
                   "decision,decision_reason,baseline_qty_delta,baseline_px,shadow_qty_delta,shadow_px,"
                   "shadow_realized_pnl,close_fraction,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (eid, pos_id, "close:oid:3", 3, "close", side, 0, "mandatory_exit",
                    "risk reduction is never blocked", -.28 if side == "long" else .28, close_px,
                    -.08 if allowed and side == "long" else .08 if allowed else 0, close_px, shadow_realized, 1, ago(1200)))
db.execute("INSERT INTO provider_balance_snapshot (provider,checked_at,currency,total_balance,granted_balance,"
           "topped_up_balance,is_available,estimated_days,estimated_requests) VALUES ('deepseek',?,'CNY',42.8,2.8,40,1,18.4,1765)",
           (now_iso(),))
ensure_instance_keypair(db)

# paper account: realized equity = initial + sum(closed realized) so overview live-derive is realistic
realized_cum = sum(pnl for *_, pnl, _ in C)
db.execute("INSERT OR REPLACE INTO copy_account (id,initial_balance,balance,updated_at) VALUES (1,10000,?,?)",
           (10000 + realized_cum, now_iso()))

for i in range(60):
    db.execute("INSERT INTO copy_action (pos_id,addr,coin,ts,action,our_qty_delta,our_px) "
               "VALUES (?,?,?,?,?,?,?)", (1, W[0][0], "BTC", now_ms(), "open", 0.35, 64000))

for i in range(4):
    db.execute("INSERT INTO scan_runs (started_at,finished_at,duration_s,candidates,probed_new,added,"
               "retired,kept,rejected,n_active) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (ago((i+1)*86400), ago((i+1)*86400 - 1180), 1180, 1240, 60, 2+i % 2, 1+i % 2, 24, 854+i, 26))

for rank, row in enumerate(W, 1):
    role = "core" if rank <= 3 else "challenger"
    db.execute("INSERT INTO pipeline_audit "
               "(stamp,source,stage,addr,rank,status,reason,follow_score,payload_json,created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (ago(120), "scan", "selection", row[0], rank, role,
                "core_keep" if role == "core" else "challenger_evidence", .18-rank*.025,
                '{}', ago(120)))
db.execute("INSERT INTO pipeline_audit "
           "(stamp,source,stage,status,reason,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
           (ago(120), "scan", "selection_summary", "ok", "explicit_core_selection",
            '{"generation":"mock-2026-07-11","action":"keep","core":3,"challenger":3,"exitOnly":0}', ago(120)))
db.execute("INSERT INTO pipeline_audit "
           "(stamp,source,stage,status,reason,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
           (ago(120), "scan", "tuner_finalize", "complete", "synchronous_quality_prefix_formation",
            '{"portfolioReplay":{"status":"ok"},"selectionReplay":{"status":"ok","refreshed":6}}',
            ago(60)))

db.commit(); db.close()
print("seeded mock:", DB)
