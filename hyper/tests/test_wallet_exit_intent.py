import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from hyper import storage
from hyper.execution.observer import Book, Observer


class WalletExitIntentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = storage.connect(
            str(Path(self.tmp.name) / "test.db"),
            storage.DISCOVERY_SCHEMA,
            storage.OBSERVE_SCHEMA,
        )
        self.observer = Observer(self.db, [], {})
        self.reload = mock.patch.object(self.observer, "_reload_strategy")
        self.reload.start()

    def tearDown(self):
        self.reload.stop()
        self.db.close()
        self.tmp.cleanup()

    def _position(self, addr="0xabc", status="open", pnl=0.0, was_liq=0, table="copy_position"):
        return self.db.execute(
            f"INSERT INTO {table} "
            "(addr,coin,side,status,realized_pnl,was_liq,entry_px,size,rem_size,leverage,margin,notional,"
            "master_open_px,master_peak_sz,opened_at) "
            "VALUES (?,'BTC','long',?,?,?,100,1,1,5,20,100,100,1,'2026-07-01T00:00:00Z')",
            (addr, status, pnl, was_liq),
        ).lastrowid

    def _use_live_ledger(self):
        self.observer.execution_mode = "live"
        self.observer.taker = Book(
            "live", "live_copy_position", "live_copy_action", "live_copy_account",
        )

    def _publish_core(self, addr="0xabc"):
        self.db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at,"
            "leaderboard_valid,profile_complete) "
            "VALUES ('g1','published',1,1,1,'2026-07-01T00:00:00Z',"
            "'2026-07-01T01:00:00Z',1,1)"
        )
        self.db.execute(
            "INSERT INTO follow_selection "
            "(generation,addr,role,enabled,selected_at) "
            "VALUES ('g1',?,'core',1,'2026-07-01T01:00:00Z')",
            (addr,),
        )
        self.db.commit()

    def test_flat_request_immediately_releases_to_requalify(self):
        result = self.observer._cmd_wallet_exit_request("0xAbC")
        self.assertEqual("requalify", result["intent"])
        row = self.db.execute(
            "SELECT enabled,intent,intent_position_ids_json FROM target_controls WHERE addr='0xabc'"
        ).fetchone()
        self.assertEqual((0, "requalify", "[]"), row)

    def test_open_request_captures_all_positions_and_survives_restart(self):
        first = self._position()
        second = self._position()
        result = self.observer._cmd_wallet_exit_request("0xabc")
        self.assertEqual("draining", result["intent"])
        self.assertEqual([first, second], result["capturedPositionIds"])
        row = self.db.execute(
            "SELECT intent_position_ids_json FROM target_controls WHERE addr='0xabc'"
        ).fetchone()
        self.assertEqual([first, second], json.loads(row[0]))
        pending = self.observer._resolve_all_draining_intents()
        self.assertEqual([], pending)

    def test_live_exit_ignores_paper_position_and_paper_reloads_it_exit_only(self):
        paper_position = self._position()
        self._use_live_ledger()

        result = self.observer._cmd_wallet_exit_request("0xabc")

        self.assertEqual("live", result["executionMode"])
        self.assertEqual("requalify", result["intent"])
        self.assertEqual([], result["capturedPositionIds"])
        self.assertEqual(
            "open",
            self.db.execute(
                "SELECT status FROM copy_position WHERE pos_id=?", (paper_position,),
            ).fetchone()[0],
        )

        paper_observer = Observer(self.db, [], {})
        async def reload_paper_positions():
            paper_observer._reload_open()

        asyncio.run(reload_paper_positions())
        paper_observer._reload_targets(target_snapshot=[])
        self.assertIn("0xabc", paper_observer.held_off)
        self.assertIn("0xabc", paper_observer.addrs)

    def test_live_draining_capture_and_resolution_use_live_ledger_only(self):
        paper_position = self._position(pnl=50)
        live_position = self._position(table="live_copy_position")
        self._use_live_ledger()

        result = self.observer._cmd_wallet_exit_request("0xabc")
        self.assertEqual("draining", result["intent"])
        self.assertEqual([live_position], result["capturedPositionIds"])
        self.db.execute(
            "UPDATE live_copy_position SET status='closed',realized_pnl=-5 WHERE pos_id=?",
            (live_position,),
        )

        resolved = self.observer._resolve_draining_intent(
            "0xabc", reload_strategy=False,
        )
        self.assertEqual("requalify", resolved["intent"])
        self.assertEqual(-5.0, resolved["capturedNetPnl"])
        self.assertEqual(
            "open",
            self.db.execute(
                "SELECT status FROM copy_position WHERE pos_id=?", (paper_position,),
            ).fetchone()[0],
        )

    def test_operator_can_cancel_unresolved_draining_exit(self):
        self._publish_core()
        pos_id = self._position()
        self.observer._cmd_wallet_exit_request("0xabc")

        result = self.observer._cmd_wallet_exit_cancel("0xAbC")

        self.assertEqual(
            {
                "address": "0xabc", "intent": "active", "enabled": True,
                "resolution": "operator_cancelled_exit",
            },
            result,
        )
        self.assertEqual(
            (1, "active", None, "operator_cancelled_exit"),
            self.db.execute(
                "SELECT enabled,intent,intent_position_ids_json,intent_resolution "
                "FROM target_controls WHERE addr='0xabc'"
            ).fetchone(),
        )
        self.assertEqual(
            "open",
            self.db.execute(
                "SELECT status FROM copy_position WHERE pos_id=?", (pos_id,),
            ).fetchone()[0],
        )
        self.assertIsNone(self.observer._resolve_draining_intent("0xabc"))

    def test_cancel_requires_current_unresolved_core_draining(self):
        self._publish_core()
        with self.assertRaisesRegex(ValueError, "wallet_exit_not_draining"):
            self.observer._cmd_wallet_exit_cancel("0xabc")

        self._position()
        self.observer._cmd_wallet_exit_request("0xabc")
        self.db.execute(
            "INSERT INTO wallet_registry "
            "(addr,state,first_seen_at,last_seen_at,risk_level,updated_at) "
            "VALUES ('0xabc','qualified','2026-01-01T00:00:00Z',"
            "'2026-01-01T00:00:00Z','high','2026-01-01T00:00:00Z')"
        )
        with self.assertRaisesRegex(ValueError, "blocked by durable risk state"):
            self.observer._cmd_wallet_exit_cancel("0xabc")
        self.assertEqual(
            (0, "draining"),
            self.db.execute(
                "SELECT enabled,intent FROM target_controls WHERE addr='0xabc'"
            ).fetchone(),
        )

    def test_profitable_captured_cohort_recovers(self):
        first = self._position()
        second = self._position()
        self.observer._cmd_wallet_exit_request("0xabc")
        self.db.execute(
            "UPDATE copy_position SET status='closed',realized_pnl=15 WHERE pos_id=?",
            (first,),
        )
        self.assertEqual(
            "draining",
            self.observer._resolve_draining_intent(
                "0xabc", reload_strategy=False,
            )["intent"],
        )
        self.db.execute(
            "UPDATE copy_position SET status='closed',realized_pnl=-5 WHERE pos_id=?",
            (second,),
        )
        result = self.observer._resolve_draining_intent(
            "0xabc", reload_strategy=False,
        )
        self.assertEqual("active", result["intent"])
        self.assertEqual(10.0, result["capturedNetPnl"])
        self.assertEqual(
            (1, "active", "captured_cohort_profitable_recovered"),
            self.db.execute(
                "SELECT enabled,intent,intent_resolution FROM target_controls WHERE addr='0xabc'"
            ).fetchone(),
        )

    def test_loss_or_any_liquidation_moves_to_requalify(self):
        first = self._position()
        second = self._position()
        self.observer._cmd_wallet_exit_request("0xabc")
        self.db.execute(
            "UPDATE copy_position SET status='closed',realized_pnl=100 WHERE pos_id=?",
            (first,),
        )
        self.db.execute(
            "UPDATE copy_position SET status='liquidated',realized_pnl=-1,was_liq=1 WHERE pos_id=?",
            (second,),
        )
        result = self.observer._resolve_draining_intent(
            "0xabc", reload_strategy=False,
        )
        self.assertEqual("requalify", result["intent"])
        self.assertEqual("captured_cohort_liquidated", result["resolution"])

    def test_high_risk_overrides_profitable_cohort(self):
        pos_id = self._position()
        self.observer._cmd_wallet_exit_request("0xabc")
        self.db.execute(
            "UPDATE copy_position SET status='closed',realized_pnl=100 WHERE pos_id=?",
            (pos_id,),
        )
        self.db.execute(
            "INSERT INTO wallet_registry "
            "(addr,state,first_seen_at,last_seen_at,risk_level,updated_at) "
            "VALUES ('0xabc','qualified','2026-01-01T00:00:00Z',"
            "'2026-01-01T00:00:00Z','high','2026-01-01T00:00:00Z')"
        )
        result = self.observer._resolve_draining_intent(
            "0xabc", reload_strategy=False,
        )
        self.assertEqual("requalify", result["intent"])
        self.assertEqual("captured_cohort_high_risk", result["resolution"])


class WalletExitMigrationTest(unittest.TestCase):
    def test_legacy_disabled_control_migrates_to_requalify(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "legacy.db")
            legacy = sqlite3.connect(path)
            legacy.execute(
                "CREATE TABLE target_controls "
                "(addr TEXT PRIMARY KEY,enabled INTEGER,pinned INTEGER,note TEXT,updated_at TEXT)"
            )
            legacy.execute(
                "INSERT INTO target_controls VALUES ('0xabc',0,0,NULL,'2026-01-01T00:00:00Z')"
            )
            legacy.commit()
            legacy.close()
            db = storage.connect(
                path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
            )
            self.assertEqual(
                (0, "requalify"),
                db.execute(
                    "SELECT enabled,intent FROM target_controls WHERE addr='0xabc'"
                ).fetchone(),
            )
            db.close()
