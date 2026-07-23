import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hyper import params, storage
from hyper.execution.observer import Observer
from hyper.selection import strategy_revision


class StrategyRevisionTests(unittest.TestCase):
    def _db(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = storage.connect(
            str(Path(td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )
        db.row_factory = sqlite3.Row
        params.seed_params(db)
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at,"
            "leaderboard_valid,profile_complete) "
            "VALUES ('g1','published',1,1,1,'2026-01-01','2026-01-02',1,1)"
        )
        db.execute(
            "INSERT INTO watchlist (rank,addr,score,acct_value,sector_policy_json,updated_at) "
            "VALUES (1,'0xaaa',.9,12345,'{\"allowed\":[\"crypto\"],\"crypto\":{\"allow\":true}}','now')"
        )
        db.execute(
            "INSERT INTO follow_selection "
            "(generation,addr,role,enabled,utility,acct_value,sector_policy_json,selected_at) "
            "VALUES ('g1','0xaaa','core',1,9,12345,"
            "'{\"allowed\":[\"crypto\"],\"crypto\":{\"allow\":true}}','now')"
        )
        db.execute("INSERT INTO episode (addr,coin,open_ms,seq) VALUES ('0xaaa','BTC',1,0)")
        db.commit()
        return db

    def test_revision_freezes_params_and_target_context(self):
        db = self._db()
        created = strategy_revision.create_revision(db, "g1", source="test")
        db.commit()

        db.execute("UPDATE params SET value='9' WHERE key='STABLE_MARGIN_PCT'")
        db.execute("UPDATE watchlist SET acct_value=999 WHERE addr='0xaaa'")
        db.commit()
        active = strategy_revision.load_active(db)

        self.assertEqual(active["revision"], created["revision"])
        self.assertNotEqual(active["params"]["STABLE_MARGIN_PCT"], .09)
        self.assertEqual(active["targets"][0]["acctValue"], 12345)
        self.assertEqual(active["targets"][0]["seedCoins"], ["BTC"])
        self.assertIn("COPY_POLICY_VERSION", active["params"])
        self.assertNotIn("CORE_MIN_COPY_RETURN_30D", active["params"])
        self.assertEqual(active["params"]["CORE_INTRATRADE_DD_MAX"], .12)
        self.assertEqual(active["params"]["COPY_DEEP_BAG_EVENT_MIN_HOURS"], 4.0)
        self.assertEqual(active["params"]["CORE_DEEP_BAG_MIN_RECOVERY_RATE"], .50)
        self.assertNotIn("WALLET_HWM_EXIT_DD_PCT", active["params"])
        self.assertEqual(active["params"]["WALLET_STOCK_SIDE_CAP_PCT"], .10)

    def test_revision_legally_snapshots_zero_core_targets(self):
        db = self._db()
        db.execute(
            "UPDATE follow_selection SET role='exit_only',enabled=0 WHERE generation='g1'"
        )

        created = strategy_revision.create_revision(db, "g1", source="zero_core")
        db.commit()
        active = strategy_revision.load_active(db)

        self.assertEqual(created["targetCount"], 0)
        self.assertEqual(active["targets"], [])
        self.assertEqual(strategy_revision.resolved_targets(db, active), [])

    def test_activation_compare_and_swap_rejects_stale_parent(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        second = strategy_revision.create_revision(
            db,
            "g1",
            source="manual",
            parent_revision=first["revision"],
            expected_active_revision=first["revision"],
            enqueue_reload=False,
        )
        db.commit()
        with self.assertRaisesRegex(RuntimeError, "strategy_revision_changed"):
            strategy_revision.create_revision(
                db,
                "g1",
                source="stale_tuner",
                parent_revision=first["revision"],
                expected_active_revision=first["revision"],
                enqueue_reload=False,
            )
        db.rollback()
        self.assertEqual(strategy_revision.active_revision_id(db), second["revision"])

    def test_observer_manual_param_command_creates_child_and_loads_bundle(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        db.commit()
        db.execute("UPDATE params SET value='7' WHERE key='STABLE_MARGIN_PCT'")
        db.commit()
        observer = Observer(db, [], {})

        result = asyncio.run(observer._dispatch_command("reload_params", {
            "by": "dashboard_params",
            "createStrategyRevision": True,
            "reason": "operator_follow_params_changed",
        }))

        active = strategy_revision.load_active(db)
        self.assertEqual(active["parentRevision"], first["revision"])
        self.assertEqual(active["params"]["STABLE_MARGIN_PCT"], .07)
        self.assertEqual(observer.strategy_revision_id, active["revision"])
        self.assertEqual(observer.addrs, ["0xaaa"])
        self.assertEqual(result["revision"], active["revision"])

    def test_operator_disable_is_live_overlay_not_snapshot_mutation(self):
        db = self._db()
        strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        db.execute("INSERT INTO target_controls (addr,enabled) VALUES ('0xaaa',0)")
        db.commit()

        active = strategy_revision.load_active(db)
        self.assertEqual(len(active["targets"]), 1)
        self.assertEqual(strategy_revision.resolved_targets(db, active), [])

    def test_wallet_star_command_persists_original_order_timestamp(self):
        db = self._db()
        observer = Observer(db, [], {})

        first = observer._cmd_wallet_star("0xAAA", True)
        second = observer._cmd_wallet_star("0xaaa", True)
        row = db.execute(
            "SELECT pinned,pinned_at FROM target_controls WHERE addr='0xaaa'"
        ).fetchone()

        self.assertTrue(first["starred"])
        self.assertTrue(second["starred"])
        self.assertEqual(row["pinned"], 1)
        self.assertEqual(row["pinned_at"], first["starredAt"])

        observer._cmd_wallet_star("0xaaa", False)
        row = db.execute(
            "SELECT pinned,pinned_at FROM target_controls WHERE addr='0xaaa'"
        ).fetchone()
        self.assertEqual(row["pinned"], 0)
        self.assertIsNone(row["pinned_at"])


if __name__ == "__main__":
    unittest.main()
