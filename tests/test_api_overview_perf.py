import sqlite3
import tempfile
import unittest
from importlib import import_module, util
from pathlib import Path
from unittest.mock import patch

from hl import api, api_overview, storage


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
            with patch("hl.api._fast_bundle", return_value={"ok": True}) as build:
                first = api._shared_fast_bundle(db)
                second = api._shared_fast_bundle(db)

        self.assertEqual(first, second)
        build.assert_called_once()

    def test_overview_endpoints_are_split_from_api_module(self):
        self.assertIsNotNone(util.find_spec("hl.api_overview"))
        api_overview = import_module("hl.api_overview")

        self.assertTrue(callable(api_overview.ep_shadow))
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
        self.assertEqual(overview["unrealizedPnl"], 94.0)
        self.assertEqual(overview["availableBalance"], 9870.0)
        self.assertEqual(overview["risk"]["gross"], 1300.0)
        self.assertEqual(overview["risk"]["net"], -300.0)

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
