import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from hyper import storage
from hyper.execution.observer import Observer


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

    def _position(self, addr="0xabc", status="open", pnl=0.0, was_liq=0):
        return self.db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,realized_pnl,was_liq,opened_at) "
            "VALUES (?,'BTC','long',?,?,?,'2026-07-01T00:00:00Z')",
            (addr, status, pnl, was_liq),
        ).lastrowid

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
