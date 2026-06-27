"""Seed a realistic mock hl.db for eyeballing the dashboard UI end-to-end (no live engine needed).
Usage (from repo root):  python3 web/dev/seed_mock.py data/hl_mock.db
- scores on the NATIVE 0–3 scale (so the 0–100 display normalization shows a real spread)
- equity curve with a midday drawdown then recovery
- open positions: long/short, crypto+stock, one near liquidation; rejects covering all funnel buckets
"""
import math, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from hl import storage, params
from hl.util import now_iso, now_ms

DB = sys.argv[1] if len(sys.argv) > 1 else "data/hl_mock.db"
db = storage.connect(DB, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
params.seed_params(db)
for t in ("account_stats", "copy_position", "copy_action", "coin_vol", "watchlist",
          "target_controls", "profile", "leaderboard", "scan_runs"):
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
for rank, (addr, name, score, roi, wr, coin, mt, worst, madds, acct, en) in enumerate(W, 1):
    db.execute("INSERT INTO leaderboard (addr,display_name,account_value,mon_roi,is_candidate,fetched_at) "
               "VALUES (?,?,?,?,1,?)", (addr, name, acct, roi, now_iso()))
    db.execute("INSERT INTO profile (addr,status,reason,score,n_trades,win_rate,roi_equity,acct_value,"
               "top_coin,market_type,worst_loss_pct,median_adds_per_ep) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (addr, "active", "ok", score, 40, wr, roi, acct, coin, mt, worst, madds))
    db.execute("INSERT INTO watchlist (rank,addr,display_name,score,roi_equity,win_rate,top_coin,"
               "market_type,acct_value,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (rank, addr, name, score, roi, wr, coin, mt, acct, now_iso()))
    db.execute("INSERT INTO target_controls (addr,enabled,updated_at) VALUES (?,?,?)", (addr, en, now_iso()))

for i in range(40):
    db.execute("INSERT INTO leaderboard (addr,display_name,account_value,mon_roi,is_candidate,fetched_at) "
               "VALUES (?,?,?,?,1,?)", (f"0xcand{i:036x}", f"c{i}", 12000, 0.1, now_iso()))
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
for addr, coin, side, entry, lev, margin, notl, mark, liq in O:
    size = notl / entry
    sgn = 1 if side == "long" else -1
    upnl = size * (mark - entry) * sgn
    db.execute("INSERT INTO copy_position (addr,coin,side,status,entry_px,leverage,margin,notional,"
               "size,rem_size,liq_px,mark_px,unrealized_pnl,open_lag_sec,opened_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (addr, coin, side, "open", entry, lev, margin, notl, size, size, liq, mark, upnl,
                round(0.8 + (lev % 3) * 0.4, 1), ago(3600 * (2 + lev % 4))))

C = [
    (W[0][0], "BTC",  "long",  980.0, 26000), (W[0][0], "ETH", "short", 210.0, 8000),
    (W[1][0], "ETH",  "long",  -180.0, 5400), (W[1][0], "SOL", "long",  430.0, 14000),
    (W[2][0], "SOL",  "short", -95.0, 3600),  (W[2][0], "BTC", "long",  620.0, 30000),
    (W[3][0], "AAPL", "long",  150.0, 20000), (W[4][0], "DOGE","long", -260.0, 4200),
]
for addr, coin, side, pnl, dur in C:
    db.execute("INSERT INTO copy_position (addr,coin,side,status,entry_px,realized_pnl,opened_at,closed_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (addr, coin, side, "closed", 100, pnl, ago(dur + 1200), ago(1200)))

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

db.commit(); db.close()
print("seeded mock:", DB)
