import sqlite3
import tempfile
import unittest
from pathlib import Path

from hl import api, params, storage


class GuardedDb:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        forbidden = (
            "(SELECT COUNT(*) FROM episode e WHERE e.addr=w.addr",
            "(SELECT COUNT(*) FROM copy_position cp WHERE cp.addr=w.addr",
            "(SELECT COALESCE(SUM(realized_pnl),0) FROM copy_position cp WHERE cp.addr=w.addr",
            "w.n_trades",
            "pr.active_days",
            "p.roi_equity",
            "p.net_pnl",
            "p.avg_notional",
            "p.roi_total",
        )
        if any(fragment in normalized for fragment in forbidden):
            raise AssertionError("wallets endpoint should aggregate per-wallet stats before joining watchlist")
        return self.db.execute(sql, args)


class WalletDetailGuardedDb:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        forbidden = (
            "WHERE addr=? AND status!='open'",
            "WHERE addr=? AND status='open'",
            "cp.entry_px",
            "cp.mark_px",
            "cp.leverage",
            "cp.margin",
            "cp.notional",
            "cp.master_open_px",
            "cp.add_count",
            "SELECT our_px FROM copy_action",
        )
        if any(fragment in normalized for fragment in forbidden):
            raise AssertionError("wallet detail should aggregate once and defer row detail to position detail")
        return self.db.execute(sql, args)


class ApiWalletsPerfTests(unittest.TestCase):
    def test_wallets_uses_joined_aggregates_for_forward_stats(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO watchlist (rank,addr,score,market_type,win_rate,top_coin,n_trades,updated_at) "
                "VALUES (1,'0xaaa',0.9,'crypto',0.75,'BTC',10,'now')"
            )
            db.execute("INSERT INTO profile (addr,status,score,active_days) VALUES ('0xaaa','active',0.9,5)")
            db.execute("INSERT INTO leaderboard (addr,week_roi,mon_roi) VALUES ('0xaaa',0.1,0.2)")
            db.execute(
                "INSERT INTO episode (addr,coin,side,open_ms,seq,close_ms) "
                "VALUES ('0xaaa','BTC','long',1,0,9999999999999)"
            )
            db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,realized_pnl,opened_at,closed_at) "
                "VALUES ('0xaaa','BTC','long','closed',12,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            db.commit()

            res = api.ep_wallets(GuardedDb(db), {"tab": ["followed"]})

        wallet = res["wallets"][0]
        self.assertEqual(wallet["followCount"], 1)
        self.assertEqual(wallet["closedN"], 1)
        self.assertEqual(wallet["closed7d"], 1)
        self.assertEqual(wallet["forwardNetPnl"], 12)
        self.assertNotIn("evidenceHeld", wallet)

    def test_dropped_wallets_omit_unused_profile_columns(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO follow_history (addr,last_followed_at,last_followed_score) "
                "VALUES ('0xaaa','2026-01-01T00:00:00Z',0.8)"
            )
            db.execute(
                "INSERT INTO profile (addr,status,reason,score,market_type,win_rate,top_coin) "
                "VALUES ('0xaaa','rejected','not_profitable',0.4,'crypto',0.5,'BTC')"
            )
            db.execute("INSERT INTO leaderboard (addr,week_roi,mon_roi) VALUES ('0xaaa',0.1,0.2)")
            db.commit()

            res = api.ep_wallets(GuardedDb(db), {"tab": ["dropped"]})

        self.assertEqual(res["wallets"][0]["dropReason"], "转亏")

    def test_wallets_observing_tab_falls_back_to_followed_without_legacy_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO watchlist (rank,addr,score,market_type,win_rate,top_coin,updated_at) "
                "VALUES (1,'0xaaa',0.9,'crypto',0.75,'BTC','now')"
            )
            db.commit()

            res = api.ep_wallets(GuardedDb(db), {"tab": ["observing"]})

        self.assertEqual(res["tab"], "followed")
        self.assertEqual(res["followed"], 1)
        self.assertNotIn("observing", res)
        self.assertEqual(res["wallets"][0]["followPos"], 1)

    def test_wallet_detail_records_are_lean_and_lazy_load_position_detail(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO profile (addr,status,score,win_rate,n_trades,market_type) "
                "VALUES ('0xaaa','active',0.9,0.7,10,'crypto')"
            )
            db.execute(
                "INSERT INTO copy_position "
                "(addr,coin,side,status,realized_pnl,unrealized_pnl,entry_px,mark_px,leverage,margin,notional,"
                "master_open_px,add_count,opened_at,closed_at) "
                "VALUES ('0xaaa','BTC','long','closed',12,0,100,101,5,20,100,99,1,"
                "'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            db.commit()

            res = api.ep_wallet_detail(WalletDetailGuardedDb(db), "0xaaa")

        self.assertEqual(res["closedN"], 1)
        record = res["records"][0]
        self.assertEqual(record["pnl"], 12)
        self.assertEqual(record["openedAt"], "2026-01-01T00:00:00Z")
        for key in ("entry", "exit", "masterEntry", "leverage", "margin", "notional", "addCount", "closedAt"):
            self.assertNotIn(key, record)


if __name__ == "__main__":
    unittest.main()
