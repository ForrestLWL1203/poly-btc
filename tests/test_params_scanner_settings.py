import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hl import config, params, storage


class ScannerSettingsParamTests(unittest.TestCase):
    def test_product_defaults_use_requested_volume_box_and_disable_copy_stop(self):
        self.assertEqual(config.HARVEST_WEEK_VLM_MIN, 300_000.0)
        self.assertEqual(config.HARVEST_WEEK_VLM_MAX, 30_000_000.0)
        self.assertFalse(config.COPY_STOP_ENABLE)

        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)

            scanner = params.load_category(db, "scanner")
            follow = params.load_follow(db)
            self.assertEqual(scanner["HARVEST_WEEK_VLM_MIN"], 300_000.0)
            self.assertEqual(scanner["HARVEST_WEEK_VLM_MAX"], 30_000_000.0)
            self.assertFalse(follow["COPY_STOP_ENABLE"])

    def test_scanner_settings_expose_basic_and_folded_advanced_knobs(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)

            scanner_params = params.get_all(db)["scanner"]
            scanner_keys = [p["key"] for p in scanner_params]
            levels = {p["key"]: p["level"] for p in scanner_params}

            self.assertEqual(scanner_keys[:5], [
                "HARVEST_MIN_ACCT",
                "HARVEST_WEEK_VLM_MIN",
                "HARVEST_WEEK_VLM_MAX",
                "EXCLUDE_HFT",
                "inactive_days",
            ])
            self.assertIn("PORTFOLIO_MAX_TURNOVER", scanner_keys)
            self.assertIn("PORTFOLIO_MIN_EDGE_BPS", scanner_keys)
            self.assertIn("MAX_CONCURRENT_POS", scanner_keys)
            self.assertIn("MIN_ACTIVE_SCORE", scanner_keys)
            self.assertIn("EVIDENCE_MIN_DAYS", scanner_keys)
            self.assertIn("EVIDENCE_MIN_TRADES", scanner_keys)
            self.assertEqual(levels["PORTFOLIO_MAX_TURNOVER"], "blue")
            self.assertEqual(levels["EVIDENCE_MIN_TRADES"], "blue")
            self.assertFalse(any(k.startswith("SCORE_") for k in scanner_keys))

    def test_seed_params_refreshes_metadata_without_overwriting_operator_value(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO params (key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("MAX_CONCURRENT_POS", "42", "scanner", "hidden", "int", "rescan", "15", "old"),
            )
            db.commit()

            params.seed_params(db)

            row = db.execute(
                "SELECT value,category,level,type,effect FROM params WHERE key='MAX_CONCURRENT_POS'"
            ).fetchone()
            self.assertEqual(row["value"], "42")
            self.assertEqual(row["category"], "scanner")
            self.assertEqual(row["level"], "blue")
            self.assertEqual(row["type"], "int")
            self.assertEqual(row["effect"], "rescan")

    def test_db_score_rows_do_not_override_code_score_weights(self):
        original = {
            "SCORE_W_WIN": config.SCORE_W_WIN,
            "SCORE_W_ACT": config.SCORE_W_ACT,
            "SCORE_W_ROI": config.SCORE_W_ROI,
            "SCORE_STRETCH": config.SCORE_STRETCH,
        }
        try:
            with tempfile.TemporaryDirectory() as td:
                db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
                params.seed_params(db)
                for key in original:
                    db.execute(
                        "INSERT OR REPLACE INTO params "
                        "(key,value,category,level,type,effect,default_value,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (key, "999", "scanner", "yellow", "float", "rescan", "999", "test"),
                    )
                db.commit()

                ns = SimpleNamespace()
                params.apply_scanner_params(db, ns)

                for key, value in original.items():
                    self.assertEqual(getattr(config, key), value)
        finally:
            for key, value in original.items():
                setattr(config, key, value)


if __name__ == "__main__":
    unittest.main()
