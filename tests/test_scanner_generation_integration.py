import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hl import params, scanner, storage


def leaderboard_row(addr="0xaaa"):
    return {
        "ethAddress": addr,
        "accountValue": "100000",
        "windowPerformances": [
            ("day", {"pnl": "100", "roi": "0.001", "vlm": "1000000"}),
            ("week", {"pnl": "300000", "roi": "0.10", "vlm": "30000000"}),
            ("month", {"pnl": "500000", "roi": "0.20", "vlm": "90000000"}),
            ("allTime", {"pnl": "900000", "roi": "0.30", "vlm": "180000000"}),
        ],
    }


def scan_args():
    return SimpleNamespace(
        days=14,
        no_harvest=False,
        full_scan=False,
        order="mon_roi",
        limit=300,
        workers=1,
        max_pages=2,
    )


class ScannerGenerationIntegrationTests(unittest.TestCase):
    def open_db(self, td):
        return storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)

    def test_invalid_leaderboard_retains_old_published_selection_and_skips_finalize(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO leaderboard (addr,is_candidate,fetched_at,generation) "
                "VALUES ('0xold',1,'2026-01-01T00:00:00Z','old')"
            )
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at,leaderboard_valid,profile_complete) "
                "VALUES ('old','published',1,1,1,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z',1,1)"
            )
            db.execute(
                "INSERT INTO follow_selection (generation,addr,role,enabled,selected_at) "
                "VALUES ('old','0xold','core',1,'2026-01-01T01:00:00Z')"
            )
            db.commit()

            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[]), \
                    patch.object(scanner, "_launch_async_tuner") as launch, \
                    patch.object(scanner, "_prune_discovery_cache") as prune:
                scanner.scan(db, scan_args())

            current = db.execute(
                "SELECT generation FROM scan_generation WHERE is_current=1 AND status='published'"
            ).fetchone()[0]
            failed = db.execute(
                "SELECT status,complete FROM scan_generation WHERE generation!='old' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(current, "old")
            self.assertEqual(failed, ("failed", 0))
            self.assertEqual(db.execute("SELECT addr FROM leaderboard").fetchone()[0], "0xold")
            launch.assert_not_called()
            prune.assert_not_called()

    def test_complete_scan_publishes_generation_and_explicit_challenger(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)

            def fake_profile(db_, addr, start_ms, now_ms, p, prior, lb, stamp, universe, force_full=False):
                row = {
                    "addr": addr,
                    "status": "active",
                    "reason": "ok",
                    "score": 0.8,
                    "raw_quality_score": 0.8,
                    "profile_generation": p.scan_generation,
                    "evaluated_at": stamp,
                    "last_refreshed": stamp,
                    "data_status": "valid",
                    "evidence_status": "missing",
                    "last_copyable_open_ms": now_ms,
                    "times_seen": 1,
                    "times_active": 1,
                }
                cols = storage.PROFILE_COLS.split(",")
                with scanner._db_lock:
                    db_.execute(
                        f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                        f"VALUES ({','.join('?' for _ in cols)})",
                        [row.get(col) for col in cols],
                    )
                    db_.commit()
                return "active", "ok", row, False

            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row()]), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner, "_launch_async_tuner", return_value={"status": "launched"}) as launch, \
                    patch.object(scanner, "_prune_discovery_cache", return_value={}):
                scanner.scan(db, scan_args())

            current = db.execute(
                "SELECT generation,profile_complete FROM scan_generation "
                "WHERE is_current=1 AND status='published'"
            ).fetchone()
            selection_row = db.execute(
                "SELECT generation,addr,role,data_status,evidence_status FROM follow_selection"
            ).fetchone()
            self.assertEqual(current[1], 1)
            self.assertEqual(selection_row, (current[0], "0xaaa", "challenger", "valid", "missing"))
            self.assertEqual(db.execute("SELECT DISTINCT generation FROM leaderboard").fetchone()[0], current[0])
            launch.assert_called_once()

    def test_cold_paper_bootstrap_can_publish_first_qualified_core(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)

            def fake_profile(db_, addr, start_ms, now_ms, p, prior, lb, stamp, universe, force_full=False):
                row = {
                    "addr": addr, "status": "active", "reason": "ok", "score": 0.9,
                    "raw_quality_score": 0.9, "profile_generation": p.scan_generation,
                    "evaluated_at": stamp, "last_refreshed": stamp, "data_status": "valid",
                    "evidence_status": "qualified", "last_copyable_open_ms": now_ms,
                    "copy_bt_closed_n": 12, "copy_bt_14d_closed_n": 10, "copy_bt_7d_closed_n": 8,
                    "copy_positive_probability": 0.85, "copy_expected_return": 0.05,
                    "copy_return_lcb": 0.015, "copy_return_volatility": 0.08,
                    "copy_evidence_days": 10, "copy_recent_return_14d": 0.04,
                    "copy_recent_return_7d": 0.03, "copy_risk_score": 0.85,
                    "execution_score": 0.95, "open_probability_48h": 0.8,
                    "copy_bt_open_fill_rate": 0.95, "actionable_open_rate": 0.95,
                    "capacity_fit": 0.95, "copy_bt_net_pnl": 800,
                    "copy_bt_14d_net_pnl": 400, "copy_bt_7d_net_pnl": 200,
                    "times_seen": 1, "times_active": 1,
                }
                cols = storage.PROFILE_COLS.split(",")
                with scanner._db_lock:
                    db_.execute(
                        f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                        f"VALUES ({','.join('?' for _ in cols)})",
                        [row.get(col) for col in cols],
                    )
                    db_.commit()
                return "active", "ok", row, False

            metrics = scanner.selection.PortfolioMetrics(
                100.0, 80.0, 0, 0.95, 0.95, 0.05, 0.20, 0.05, 0.0,
            )
            marginal = scanner.selection.MarginalSelectionResult(
                selected=("0xaaa",), baseline=scanner.selection.PortfolioMetrics(
                    0.0, 0.0, 0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0,
                ), metrics=metrics, action="add", added=("0xaaa",), evaluated=1,
            )
            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row()]), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner.auto_tune, "_portfolio_window_fills",
                                 return_value={30: [{}], 14: [{}], 7: [{}]}), \
                    patch.object(scanner.selection, "select_marginal_core", return_value=marginal), \
                    patch.object(scanner, "_launch_async_tuner", return_value={"status": "launched"}), \
                    patch.object(scanner, "_prune_discovery_cache", return_value={}):
                scanner.scan(db, scan_args())

            current = db.execute(
                "SELECT generation FROM scan_generation WHERE is_current=1 AND status='published'"
            ).fetchone()[0]
            row = db.execute(
                "SELECT addr,role FROM follow_selection WHERE generation=?", (current,)
            ).fetchone()
            self.assertEqual(row, ("0xaaa", "core"))

    def test_manual_selection_mode_carries_operator_membership_into_new_generation(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute("UPDATE params SET value='manual' WHERE key='FOLLOW_SELECTION_MODE'")
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at,leaderboard_valid,profile_complete) "
                "VALUES ('manual-old','published',1,1,1,'2026-01-01','2026-01-02',1,1)"
            )
            db.execute(
                "INSERT INTO follow_selection "
                "(generation,addr,role,enabled,reason,utility,data_status,evidence_status,selected_at) "
                "VALUES ('manual-old','0xoperator','core',1,'operator_pick',9.0,'valid','qualified','2026-01-02')"
            )
            db.commit()

            def fake_profile(db_, addr, start_ms, now_ms, p, prior, lb, stamp, universe, force_full=False):
                row = {
                    "addr": addr, "status": "active", "reason": "ok", "score": 0.99,
                    "raw_quality_score": 0.99, "profile_generation": p.scan_generation,
                    "evaluated_at": stamp, "last_refreshed": stamp, "data_status": "valid",
                    "evidence_status": "qualified", "last_copyable_open_ms": now_ms,
                    "times_seen": 1, "times_active": 1,
                }
                cols = storage.PROFILE_COLS.split(",")
                with scanner._db_lock:
                    db_.execute(
                        f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                        f"VALUES ({','.join('?' for _ in cols)})",
                        [row.get(col) for col in cols],
                    )
                    db_.commit()
                return "active", "ok", row, False

            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row("0xauto")]), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner, "_launch_async_tuner", return_value={"status": "launched"}), \
                    patch.object(scanner, "_prune_discovery_cache", return_value={}):
                scanner.scan(db, scan_args())

            current = db.execute(
                "SELECT generation FROM scan_generation WHERE is_current=1 AND status='published'"
            ).fetchone()[0]
            rows = db.execute(
                "SELECT addr,role,reason FROM follow_selection WHERE generation=? ORDER BY addr", (current,)
            ).fetchall()
            summary = db.execute(
                "SELECT reason,payload_json FROM pipeline_audit "
                "WHERE stage='selection_summary' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(rows, [("0xoperator", "core", "operator_pick")])
            self.assertEqual(summary[0], "manual_selection_preserved")
            self.assertIn('"mode": "manual"', summary[1])


if __name__ == "__main__":
    unittest.main()
