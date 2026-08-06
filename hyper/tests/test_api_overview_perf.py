import sqlite3
import tempfile
import time
import unittest
from importlib import import_module, util
from pathlib import Path
from unittest.mock import patch

from dashboard import api
from dashboard.api import overview as api_overview
from hyper import storage


class GuardedDb:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized == "SELECT realized_pnl FROM copy_position WHERE status!='open'":
            raise AssertionError("overview must aggregate closed PnL in SQL, not fetch every row")
        if normalized.startswith("SELECT side,rem_size,size,entry_px,mark_px,unrealized_pnl,margin,notional FROM copy_position"):
            raise AssertionError("overview must aggregate open risk in SQL, not fetch every row")
        return self.db.execute(sql, args)


class CountingDb:
    def __init__(self, db):
        self.db = db
        self.gross_sum_queries = 0

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized == "SELECT COALESCE(SUM(ABS(our_qty_delta*our_px)),0) g FROM copy_action":
            self.gross_sum_queries += 1
        return self.db.execute(sql, args)


class InsightsGuardedDb:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized.endswith("FROM copy_position cp LEFT JOIN watchlist w ON w.addr=cp.addr GROUP BY cp.addr"):
            raise AssertionError("insights should limit wallet groups in SQL, not sort every wallet in Python")
        if normalized.endswith("FROM copy_position cp GROUP BY cp.coin"):
            raise AssertionError("insights should limit coin groups in SQL, not sort every coin in Python")
        return self.db.execute(sql, args)


class EquityGuardedDb:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized == "SELECT ts,equity FROM account_stats ORDER BY ts":
            raise AssertionError("equity endpoint should sample in SQL, not fetch the whole series")
        if normalized == "SELECT ts,equity FROM account_stats WHERE ts>=? ORDER BY ts":
            raise AssertionError("equity endpoint should sample ranged series in SQL, not fetch every point")
        return self.db.execute(sql, args)


class ApiOverviewPerfTests(unittest.TestCase):
    def test_sse_clients_share_one_fast_bundle_per_tick(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            api._fast_bundle_cache.clear()
            with patch("dashboard.api._fast_bundle", return_value={"ok": True}) as build:
                first = api._shared_fast_bundle(db)
                second = api._shared_fast_bundle(db)

        self.assertEqual(first, second)
        build.assert_called_once()

    def test_overview_endpoints_are_split_from_api_module(self):
        self.assertIsNotNone(util.find_spec("dashboard.api.overview"))
        api_overview = import_module("dashboard.api.overview")

        self.assertTrue(callable(api_overview.ep_overview))
        self.assertTrue(callable(api_overview.ep_equity))
        self.assertTrue(callable(api_overview.ep_insights))

    def test_overview_aggregates_closed_win_rate_in_sql(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO copy_account (id,initial_balance,balance,updated_at) VALUES (1,10000,10020,'now')"
            )
            db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,realized_pnl,opened_at,closed_at) "
                "VALUES ('0x1','BTC','long','closed',50,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,realized_pnl,opened_at,closed_at) "
                "VALUES ('0x2','ETH','short','closed',-30,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            db.commit()

            overview = api_overview.ep_overview(GuardedDb(db))

        self.assertEqual(overview["winRatePct"], 50.0)

    def test_overview_exposes_latest_storage_guard_alert(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO storage_guard_run "
                "(checked_at,severity,reasons_json,disk_total_bytes,disk_used_bytes,disk_free_bytes,"
                "disk_used_pct,db_main_bytes,db_wal_bytes,db_growth_24h_bytes,db_page_bytes,"
                "db_freelist_bytes,pipeline_audit_rows,staging_generation_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "2026-08-01T00:00:00Z", "warning", '["db_growth_24h_warning"]',
                    1000, 710, 290, 71.0, 200, 30, 120, 200, 20, 4, 2,
                ),
            )
            db.commit()

            overview = api_overview.ep_overview(db)

        guard = overview["system"]["storageGuard"]
        self.assertEqual(guard["status"], "warning")
        self.assertEqual(guard["reasons"], ["db_growth_24h_warning"])
        self.assertEqual(guard["diskUsedPct"], 71.0)

    def test_overview_aggregates_open_risk_in_sql(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO copy_account (id,initial_balance,balance,updated_at) VALUES (1,10000,10000,'now')"
            )
            db.execute(
                "INSERT INTO copy_position "
                "(addr,coin,side,status,entry_px,mark_px,margin,notional,size,rem_size,unrealized_pnl,opened_at) "
                "VALUES ('0x1','BTC','long','open',100,110,100,1000,10,5,NULL,'2026-01-01T00:00:00Z')"
            )
            db.execute(
                "INSERT INTO copy_position "
                "(addr,coin,side,status,entry_px,mark_px,margin,notional,size,rem_size,unrealized_pnl,opened_at) "
                "VALUES ('0x2','ETH','short','open',200,190,80,800,4,4,44,'2026-01-01T00:00:00Z')"
            )
            db.commit()

            overview = api_overview.ep_overview(GuardedDb(db))

        self.assertEqual(overview["openCount"], 2)
        self.assertEqual(overview["closedCount"], 0)
        self.assertEqual(overview["unrealizedPnl"], 94.0)
        self.assertEqual(overview["availableBalance"], 9870.0)
        self.assertEqual(overview["risk"]["gross"], 1310.0)
        self.assertEqual(overview["risk"]["net"], -210.0)

    def test_overview_exposes_closed_position_count_for_navigation(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO copy_account (id,initial_balance,balance,updated_at) VALUES (1,10000,10000,'now')"
            )
            db.executemany(
                "INSERT INTO copy_position (addr,coin,side,status,realized_pnl,opened_at,closed_at) "
                "VALUES (?,?,?,'closed',?,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')",
                [("0x1", "BTC", "long", 50), ("0x2", "ETH", "short", -30)],
            )
            db.commit()

            overview = api_overview.ep_overview(db)

        self.assertEqual(overview["closedCount"], 2)

    def test_verified_live_preview_drives_display_without_initializing_live_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO execution_control (id,selected_mode,state,updated_at) "
                "VALUES (1,'live','live_ready','2026-08-02T00:00:00Z')"
            )
            db.execute(
                "INSERT INTO execution_credential "
                "(network,account_address,agent_address,envelope_json,wrap_key_id,status,created_at,updated_at) "
                "VALUES ('mainnet','0xaccount','0xagent','{}','key','verified',"
                "'2026-08-02T00:00:00Z','2026-08-02T00:00:00Z')"
            )
            db.execute(
                "INSERT INTO execution_account_preview "
                "(network,account_address,equity,available,margin_used,unrealized_pnl,"
                "position_count,open_order_count,observed_at) "
                "VALUES ('mainnet','0xaccount',200,198,2,0,0,0,'2026-08-02T00:01:00Z')"
            )
            db.commit()

            overview = api_overview.ep_overview(db)
            equity = api_overview.ep_equity(db, "all")

            self.assertEqual(overview["system"]["mode"], "live")
            self.assertEqual(overview["equity"], 200.0)
            self.assertEqual(overview["availableBalance"], 198.0)
            self.assertEqual(overview["lastUpdate"], "2026-08-02T00:01:00Z")
            self.assertEqual(equity["points"], [{"t": "2026-08-02T00:01:00Z", "equity": 200.0}])
            self.assertIsNone(db.execute("SELECT * FROM live_copy_account").fetchone())
            self.assertIsNone(db.execute("SELECT * FROM execution_session").fetchone())

    def test_live_overview_excludes_deposits_from_roi_and_today_return(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
                "VALUES (1,'live','live_running','live-1','2026-08-02T00:00:00Z')"
            )
            db.execute(
                "INSERT INTO live_copy_account (id,initial_balance,balance,available,updated_at) "
                "VALUES (1,169.4,2169.2,2169.2,'2026-08-02T00:01:00Z')"
            )
            db.execute(
                "INSERT INTO execution_account_snapshot "
                "(session_id,equity,available,margin_used,unrealized_pnl,observed_at) "
                "VALUES ('live-1',169.4,169.4,0,0,?)",
                (api_overview._iso_ago(25 * 3600),),
            )
            db.commit()

            overview = api_overview.ep_overview(db)

        self.assertEqual(overview["equity"], 2169.2)
        self.assertEqual(overview["realizedPnl"], 0.0)
        self.assertEqual(overview["roiPct"], 0.0)
        self.assertEqual(overview["todayPct"], 0.0)

    def test_live_overview_returns_use_only_confirmed_copy_trade_pnl(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
                "VALUES (1,'live','live_running','live-1','2026-08-02T00:00:00Z')"
            )
            db.execute(
                "INSERT INTO live_copy_account (id,initial_balance,balance,available,updated_at) "
                "VALUES (1,169.4,2183,2160,'2026-08-02T00:01:00Z')"
            )
            db.execute(
                "INSERT INTO execution_order_intent "
                "(cloid,session_id,strategy_revision,action_seq,action,coin,side,reduce_only,"
                "requested_size,requested_limit_px,state,created_at,updated_at) "
                "VALUES ('0xcopy','live-1','rev-1',1,'close','BTC','sell',1,1,100,'filled','now','now')"
            )
            db.execute(
                "INSERT INTO execution_fill "
                "(network,tid,session_id,cloid,coin,side,size,px,fee,closed_pnl,fill_time_ms,created_at) "
                "VALUES ('mainnet','copy-fill','live-1','0xcopy','BTC','sell',1,100,1,12,?,'now')",
                (int(time.time() * 1000),),
            )
            # This account fill is not tied to a durable copy intent and must not affect strategy returns.
            db.execute(
                "INSERT INTO execution_fill "
                "(network,tid,session_id,cloid,coin,side,size,px,fee,closed_pnl,fill_time_ms,created_at) "
                "VALUES ('mainnet','manual-fill','live-1','0xmanual','ETH','sell',1,100,2,100,?,'now')",
                (int(time.time() * 1000),),
            )
            db.execute(
                "INSERT INTO live_copy_position "
                "(addr,coin,side,status,size,rem_size,entry_px,mark_px,margin,notional,"
                "unrealized_pnl,realized_pnl,opened_at) "
                "VALUES ('0xsource','ETH','long','open',1,1,100,103,10,100,3,0,'now')"
            )
            db.commit()

            overview = api_overview.ep_overview(db)

        expected_pnl = 12 - 1 + 3
        self.assertEqual(overview["realizedPnl"], 11.0)
        self.assertEqual(overview["unrealizedPnl"], 3.0)
        self.assertAlmostEqual(overview["roiPct"], expected_pnl / (2183 - expected_pnl) * 100)
        self.assertAlmostEqual(overview["todayPct"], expected_pnl / (2183 - expected_pnl) * 100)
        self.assertEqual(overview["fees"]["cumulative"], 1.0)

    def test_overview_exposes_current_scan_stage(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT OR REPLACE INTO scan_progress (id,state,stage,updated_at) "
                "VALUES (1,'scanning','selection_search','now')"
            )
            db.commit()

            overview = api_overview.ep_overview(db)

        self.assertEqual(overview["system"]["scannerStage"], "selection_search")

    def test_overview_reuses_gross_traded_until_copy_actions_change(self):
        if hasattr(api_overview, "_GROSS_TRADED_CACHE"):
            api_overview._GROSS_TRADED_CACHE.clear()
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO copy_account (id,initial_balance,balance,updated_at) VALUES (1,10000,10000,'now')"
            )
            db.execute(
                "INSERT INTO copy_action (pos_id,addr,coin,ts,action,our_qty_delta,our_px) "
                "VALUES (1,'0x1','BTC',1,'open',2,100)"
            )
            db.commit()
            counting = CountingDb(db)

            api_overview.ep_overview(counting)
            api_overview.ep_overview(counting)
            db.execute(
                "INSERT INTO copy_action (pos_id,addr,coin,ts,action,our_qty_delta,our_px) "
                "VALUES (1,'0x1','BTC',2,'close',-2,110)"
            )
            db.commit()
            api_overview.ep_overview(counting)

        self.assertEqual(counting.gross_sum_queries, 2)

    def test_insights_limits_grouped_rows_in_sql(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            for i in range(10):
                addr = f"0x{i:03d}"
                coin = f"C{i}"
                pnl = (i - 5) * 10
                db.execute(
                    "INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (?,?,0.8,'now')",
                    (i + 1, addr),
                )
                db.execute(
                    "INSERT INTO copy_position (addr,coin,side,status,realized_pnl,opened_at,closed_at) "
                    "VALUES (?,?,'long','closed',?,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')",
                    (addr, coin, pnl),
                )
            db.commit()

            insights = api_overview.ep_insights(InsightsGuardedDb(db))

        self.assertEqual([x["netPnl"] for x in insights["walletContrib"]], [40, 30, 20, 10, 0, -30, -40, -50])
        self.assertEqual([x["netPnl"] for x in insights["coinPnl"]], [40, 30, 20, 10, 0, -30, -40, -50])

    def test_equity_curve_samples_large_series_in_sql(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.executemany(
                "INSERT INTO account_stats (ts,balance,unrealized_pnl,equity) VALUES (?,?,0,?)",
                [(f"2026-01-01T00:{i:04d}:00Z", 10000 + i, 10000 + i) for i in range(1000)],
            )
            db.commit()

            res = api_overview.ep_equity(EquityGuardedDb(db), "all")

        points = res["points"]
        self.assertEqual(res["range"], "all")
        self.assertEqual(len(points), 251)
        self.assertEqual(points[0]["equity"], 10000)
        self.assertEqual(points[1]["equity"], 10004)
        self.assertEqual(points[-1]["equity"], 10999)


if __name__ == "__main__":
    unittest.main()
