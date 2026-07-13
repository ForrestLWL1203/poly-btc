import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hl import api_wallets, params, storage


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
            "FROM episode e JOIN followed f",
            "FROM copy_position cp JOIN followed f",
            "FROM episode e JOIN page_followed f",
            "FROM copy_position cp JOIN page_followed f",
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
    def _publish_selection(self, db, cores=()):
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at) "
            "VALUES ('g-current','published',1,1,1,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
        )
        for i, addr in enumerate(cores):
            db.execute(
                "INSERT INTO follow_selection "
                "(generation,addr,role,enabled,reason,utility,selected_at) "
                "VALUES ('g-current',?,'core',1,'portfolio_positive_net_contribution',?,'2026-01-01T01:00:00Z')",
                (addr, 1000 - i),
            )

    def test_wallets_uses_joined_aggregates_for_forward_stats(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO watchlist (rank,addr,score,market_type,win_rate,top_coin,n_trades,updated_at) "
                "VALUES (1,'0xaaa',0.9,'crypto',0.75,'BTC',10,'now')"
            )
            db.execute(
                "INSERT INTO profile (addr,status,score,active_days,sector_policy_json) "
                "VALUES ('0xaaa','active',0.9,5,?)",
                (json.dumps({
                    "allowed": ["crypto"],
                    "crypto": {"allow": True, "status": "allowed", "reason": "板块copy回测盈利"},
                    "stock": {"allow": False, "status": "recent_loss", "reason": "板块近期copy亏损"},
                }),),
            )
            db.execute("INSERT INTO leaderboard (addr,week_roi,mon_roi) VALUES ('0xaaa',0.1,0.2)")
            db.execute(
                "INSERT INTO episode (addr,coin,side,open_ms,seq,close_ms) "
                "VALUES ('0xaaa','BTC','long',1,0,9999999999999)"
            )
            db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,realized_pnl,opened_at,closed_at) "
                "VALUES ('0xaaa','BTC','long','closed',12,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            self._publish_selection(db, ["0xaaa"])
            db.execute(
                "INSERT INTO auto_tune_state(key,value,updated_at) VALUES "
                "('effective_portfolio_replay',?,'now')",
                (json.dumps({
                    "generation": "g-current", "status": "ok", "coreCount": 1,
                    "netPnl30": 4321.0, "replayedAt": "now",
                }),),
            )
            db.commit()

            with patch.object(api_wallets, "_score_breakdown", side_effect=AssertionError("detail-only")):
                res = api_wallets.ep_wallets(GuardedDb(db), {"tab": ["followed"]})

        wallet = res["wallets"][0]
        self.assertEqual(wallet["followCount"], 1)
        self.assertEqual(wallet["closed7d"], 1)
        self.assertEqual(wallet["forwardNetPnl"], 12)
        self.assertNotIn("scoreBreakdown", wallet)
        self.assertNotIn("rawScore", wallet)
        self.assertNotIn("profileGeneration", wallet)
        self.assertNotIn("evidenceHeld", wallet)
        self.assertEqual(res["portfolioReplay"]["netPnl30"], 4321.0)

    def test_wallets_falls_back_to_copy_7d_closed_when_episode_rows_are_missing(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO watchlist (rank,addr,score,market_type,win_rate,top_coin,updated_at) "
                "VALUES (1,'0xaaa',0.9,'crypto',0.75,'BTC','now')"
            )
            db.execute(
                "INSERT INTO profile (addr,status,score,copy_bt_7d_closed_n,copy_bt_7d_net_pnl) "
                "VALUES ('0xaaa','active',0.9,8,123)"
            )
            db.execute("INSERT INTO leaderboard (addr,week_roi,mon_roi) VALUES ('0xaaa',0.1,0.2)")
            self._publish_selection(db, ["0xaaa"])
            db.commit()

            res = api_wallets.ep_wallets(GuardedDb(db), {"tab": ["followed"]})

        self.assertEqual(res["wallets"][0]["closed7d"], 8)

    def test_followed_wallet_marks_recent_first_follow_as_new(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO watchlist (rank,addr,score,market_type,win_rate,top_coin,updated_at) "
                "VALUES (1,'0xaaa',0.9,'crypto',0.75,'BTC','now')"
            )
            db.execute("INSERT INTO profile (addr,status,score) VALUES ('0xaaa','active',0.9)")
            db.execute("INSERT INTO leaderboard (addr,week_roi,mon_roi) VALUES ('0xaaa',0.1,0.2)")
            db.execute(
                "INSERT INTO follow_history (addr,first_followed_at,last_followed_at,last_followed_score) "
                "VALUES ('0xaaa','2026-01-03T00:00:00Z','2026-01-03T00:00:00Z',0.9)"
            )
            self._publish_selection(db, ["0xaaa"])
            db.commit()

            with patch.object(api_wallets.time, "time", return_value=1767430800):
                recent = api_wallets.ep_wallets(GuardedDb(db), {"tab": ["followed"]})["wallets"][0]
            with patch.object(api_wallets.time, "time", return_value=1767484801):
                stale = api_wallets.ep_wallets(GuardedDb(db), {"tab": ["followed"]})["wallets"][0]

        self.assertTrue(recent["isNew"])
        self.assertNotIn("firstFollowedAt", recent)
        self.assertFalse(stale["isNew"])

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
            self._publish_selection(db)
            db.commit()

            res = api_wallets.ep_wallets(GuardedDb(db), {"tab": ["dropped"]})

        self.assertEqual(res["wallets"][0]["dropReason"], "转亏")
        self.assertNotIn("scoreBreakdown", res["wallets"][0])

    def test_dropped_wallet_uses_first_batch_after_last_followed(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO follow_history (addr,last_followed_at,last_followed_score) "
                "VALUES ('0xaaa','2026-01-01T00:00:00Z',0.8)"
            )
            db.execute(
                "INSERT INTO profile (addr,status,reason,score,market_type,win_rate,top_coin,last_refreshed) "
                "VALUES ('0xaaa','rejected','not_profitable',0.4,'crypto',0.5,'BTC','2026-01-02T00:00:00Z')"
            )
            db.execute("INSERT INTO leaderboard (addr,week_roi,mon_roi) VALUES ('0xaaa',0.1,0.2)")
            db.execute(
                "INSERT INTO pipeline_audit (stamp,source,stage,addr,status,reason,created_at) "
                "VALUES ('2026-01-03T00:00:00Z','scan','profile','0xaaa','rejected','not_profitable',"
                "'2026-01-03T01:23:00Z')"
            )
            db.execute(
                "INSERT INTO pipeline_audit (stamp,source,stage,addr,status,reason,created_at) "
                "VALUES ('2026-01-04T00:00:00Z','scan_post_tune','profile','0xaaa','rejected','not_profitable',"
                "'2026-01-04T01:23:00Z')"
            )
            self._publish_selection(db)
            db.commit()

            res = api_wallets.ep_wallets(GuardedDb(db), {"tab": ["dropped"]})

        self.assertEqual(res["wallets"][0]["lastFollowedAt"], 1767225600)
        self.assertEqual(res["wallets"][0]["dropAt"], 1767398400)
        self.assertEqual(res["wallets"][0]["dropSource"], "scan")
        self.assertEqual(res["wallets"][0]["dropStage"], "profile")
        self.assertEqual(res["wallets"][0]["dropDecidedAt"], 1767403380)

    def test_dropped_wallet_uses_follow_score_not_raw_profile_score(self):
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
                "VALUES ('0xaaa','active','ok',0.9,'crypto',0.5,'BTC')"
            )
            db.execute(
                "INSERT INTO watchlist (rank,addr,score,market_type,win_rate,top_coin,updated_at) "
                "VALUES (1,'0xaaa',0.6,'crypto',0.5,'BTC','now')"
            )
            self._publish_selection(db)
            db.commit()

            res = api_wallets.ep_wallets(GuardedDb(db), {"tab": ["dropped"]})

        self.assertEqual(res["total"], 1)
        self.assertEqual(res["wallets"][0]["dropReason"], "退出Core")
        self.assertEqual(res["wallets"][0]["score"], 60.0)
        self.assertNotIn("rawScore", res["wallets"][0])

    def test_dropped_wallets_are_paginated_in_sql(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            for i, addr in enumerate(("0xaaa", "0xbbb", "0xccc")):
                db.execute(
                    "INSERT INTO follow_history (addr,last_followed_at,last_followed_score) VALUES (?,?,0.8)",
                    (addr, f"2026-01-0{i + 1}T00:00:00Z"),
                )
                db.execute(
                    "INSERT INTO profile (addr,status,reason,score,market_type,win_rate,top_coin) "
                    "VALUES (?,'rejected','not_profitable',0.4,'crypto',0.5,'BTC')",
                    (addr,),
                )
            self._publish_selection(db)
            db.commit()

            res = api_wallets.ep_wallets(GuardedDb(db), {"tab": ["dropped"], "page": ["1"], "size": ["2"]})

        self.assertEqual(res["total"], 3)
        self.assertEqual(res["page"], 1)
        self.assertEqual(res["size"], 2)
        self.assertEqual([w["address"] for w in res["wallets"]], ["0xaaa"])

    def test_wallets_observing_tab_falls_back_to_followed_without_legacy_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO watchlist (rank,addr,score,market_type,win_rate,top_coin,updated_at) "
                "VALUES (1,'0xaaa',0.9,'crypto',0.75,'BTC','now')"
            )
            self._publish_selection(db, ["0xaaa"])
            db.commit()

            res = api_wallets.ep_wallets(GuardedDb(db), {"tab": ["observing"]})

        self.assertEqual(res["tab"], "followed")
        self.assertEqual(res["followed"], 1)
        self.assertNotIn("observing", res)
        self.assertEqual(res["wallets"][0]["followPos"], 1)

    def test_published_selection_serves_core_and_challenger_roles(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g1','published',1,1,1,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            for addr, role, utility in (("0xaaa", "core", 0.12), ("0xbbb", "challenger", 0.07)):
                db.execute(
                    "INSERT INTO follow_selection "
                    "(generation,addr,role,enabled,reason,utility,data_status,evidence_status,selected_at) "
                    "VALUES ('g1',?,?,1,?,?,'valid','qualified','2026-01-01T01:00:00Z')",
                    (addr, role, "core_entry" if role == "core" else "challenger_evidence", utility),
                )
                db.execute(
                    "INSERT INTO profile "
                    "(addr,status,score,data_status,evidence_status,profile_generation,evaluated_at,"
                    "last_copyable_open_ms,open_events_7d,actionable_open_events_7d,actionable_open_rate,capacity_fit,"
                    "copy_bt_net_pnl,copy_bt_closed_n,copy_bt_7d_closed_n,oos_net_pnl,oos_max_drawdown,oos_cvar95) "
                    "VALUES (?,'active',0.8,'valid','qualified','g1','2026-01-01T00:30:00Z',"
                    "1767225600000,6,5,0.8,0.9,42,9,4,42,0.03,-5)",
                    (addr,),
                )
                db.execute(
                    "INSERT INTO watchlist (rank,addr,score,market_type,updated_at) VALUES (1,?,0.8,'crypto','now')",
                    (addr,),
                )
            db.commit()

            core = api_wallets.ep_wallets(db, {"tab": ["followed"]})
            challenger = api_wallets.ep_wallets(db, {"tab": ["challenger"]})
            challenger_detail = api_wallets.ep_wallet_detail(db, "0xbbb")

        self.assertTrue(core["selectionMode"])
        self.assertEqual(core["selectionGeneration"], "g1")
        # Activity is the target wallet's actual opens; copied/actionable opens remain a separate field.
        self.assertEqual(core["wallets"][0]["openEvents7d"], 6)
        self.assertEqual(core["wallets"][0]["copyBacktestNetPnl"], 42)
        self.assertEqual(core["wallets"][0]["copyBacktestClosedN"], 9)
        self.assertNotIn("role", core["wallets"][0])
        self.assertNotIn("selectionMarginalUtility", challenger["wallets"][0])
        self.assertEqual(challenger["wallets"][0]["selectionReasonText"], "近7日有效Copy仅4笔（门槛5笔）")
        self.assertEqual(challenger_detail["selectionReasonText"], "近7日有效Copy仅4笔（门槛5笔）")

    def test_published_selection_scores_do_not_follow_in_progress_profile_mutations(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g1','published',1,1,1,'2026-01-01','2026-01-02')"
            )
            db.executemany(
                "INSERT INTO follow_selection "
                "(generation,addr,role,enabled,utility,follow_score,selection_rank,selected_at) "
                "VALUES ('g1',?,?,?,?,?,?,'2026-01-02')",
                [
                    ("0xcore2", "core", 1, 20, .72, 2),
                    ("0xcore1", "core", 1, 30, .68, 1),
                    ("0xchallenger", "challenger", 1, .81, .81, 3),
                ],
            )
            for addr in ("0xcore1", "0xcore2", "0xchallenger"):
                db.execute(
                    "INSERT INTO profile(addr,status,score,profile_generation) "
                    "VALUES (?,'rejected',0,'g-in-progress')", (addr,),
                )
            db.commit()

            core = api_wallets.ep_wallets(db, {"tab": ["followed"]})
            challenger = api_wallets.ep_wallets(db, {"tab": ["challenger"]})

        self.assertEqual([row["address"] for row in core["wallets"]], ["0xcore1", "0xcore2"])
        self.assertEqual([row["score"] for row in core["wallets"]], [68.0, 72.0])
        self.assertEqual(challenger["wallets"][0]["score"], 81.0)

    def test_selected_list_displays_only_observer_allowed_sector_replay(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO scan_generation(generation,status,complete,publishable,is_current,started_at) "
                "VALUES('g1','published',1,1,1,'now')"
            )
            policy = json.dumps({"allowed": ["stock"], "stock": {"allow": True}, "crypto": {"allow": False}})
            sectors = json.dumps({
                "stock": {"30": {"copy_net_pnl": 307, "closed_n": 11, "wins": 6},
                          "14": {"copy_net_pnl": 293, "closed_n": 9, "wins": 5},
                          "7": {"copy_net_pnl": 171, "closed_n": 6, "wins": 3}},
                "crypto": {"30": {"copy_net_pnl": -525, "closed_n": 2, "wins": 0}},
            })
            db.execute(
                "INSERT INTO follow_selection(generation,addr,role,enabled,reason,"
                "replay_copy_bt_net_pnl,replay_copy_bt_closed_n,replay_copy_bt_14d_net_pnl,"
                "replay_copy_bt_14d_closed_n,replay_copy_bt_7d_net_pnl,replay_copy_bt_7d_closed_n,"
                "replay_sector_copy_json,replay_params_hash,replayed_at,selected_at) "
                "VALUES('g1','0xaaa','core',1,'above_follow_line',999,99,888,88,777,77,"
                "?,'current123','2026-01-02T00:00:00Z','now')",
                (sectors,),
            )
            db.execute(
                "INSERT INTO profile(addr,status,score,copy_bt_net_pnl,copy_bt_closed_n,"
                "copy_bt_14d_net_pnl,copy_bt_14d_closed_n,copy_bt_7d_net_pnl,copy_bt_7d_closed_n,"
                "sector_policy_json,sector_copy_json) VALUES"
                "('0xaaa','active',0.8,-219,13,290,9,168,6,?,?)",
                (policy, sectors),
            )
            db.execute("INSERT INTO watchlist(rank,addr,score,updated_at) VALUES(1,'0xaaa',0.8,'now')")
            db.commit()

            wallet = api_wallets.ep_wallets(db, {"tab": ["followed"]})["wallets"][0]

        self.assertEqual(wallet["copyBacktestNetPnl"], 307)
        self.assertEqual(wallet["copyBacktestClosedN"], 11)
        self.assertEqual(wallet["copyBacktest7dNetPnl"], 171)
        self.assertNotIn("copyBacktest14dNetPnl", wallet)
        self.assertNotIn("copyReplayParamsHash", wallet)
        self.assertEqual(wallet["selectionReasonText"], "达到跟单线")

    def test_published_zero_core_selection_does_not_fall_back_to_score_line(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO watchlist (rank,addr,score,market_type,updated_at) "
                "VALUES (1,'0xlegacy',0.99,'crypto','now')"
            )
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g-empty','published',1,1,1,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            db.execute(
                "INSERT INTO follow_selection "
                "(generation,addr,role,enabled,reason,selected_at) "
                "VALUES ('g-empty','0xchallenger','challenger',1,'shadow','2026-01-01T01:00:00Z')"
            )
            db.commit()

            res = api_wallets.ep_wallets(db, {"tab": ["followed"]})

        self.assertTrue(res["selectionMode"])
        self.assertEqual(res["total"], 0)
        self.assertEqual(res["wallets"], [])

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

            res = api_wallets.ep_wallet_detail(WalletDetailGuardedDb(db), "0xaaa")

        self.assertEqual(res["closedN"], 1)
        record = res["records"][0]
        self.assertEqual(record["pnl"], 12)
        self.assertEqual(record["openedAt"], "2026-01-01T00:00:00Z")
        for key in ("entry", "exit", "masterEntry", "leverage", "margin", "notional", "addCount", "closedAt"):
            self.assertNotIn(key, record)

    def test_wallet_detail_score_uses_final_watchlist_score(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute(
                "INSERT INTO watchlist (rank,addr,score,market_type,win_rate,top_coin,updated_at) "
                "VALUES (1,'0xaaa',0.89,'crypto',0.28,'BTC','now')"
            )
            db.execute(
                "INSERT INTO profile (addr,status,score,win_rate,n_trades,market_type) "
                "VALUES ('0xaaa','active',0.65,0.28,29,'crypto')"
            )
            db.commit()

            res = api_wallets.ep_wallet_detail(WalletDetailGuardedDb(db), "0xaaa")

        self.assertEqual(res["score"], 89.0)
        self.assertEqual(res["scoreBreakdown"]["rawScore"], 65.0)


if __name__ == "__main__":
    unittest.main()
