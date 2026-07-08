import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hl import config, params, storage


class ScannerSettingsParamTests(unittest.TestCase):
    def test_scanner_settings_expose_only_operator_knobs(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)

            scanner_keys = [p["key"] for p in params.get_all(db)["scanner"]]

            self.assertEqual(
                scanner_keys,
                [
                    "HARVEST_MIN_ACCT",
                    "HARVEST_WEEK_VLM_MIN",
                    "HARVEST_WEEK_VLM_MAX",
                    "EXCLUDE_HFT",
                    "inactive_days",
                ],
            )
            self.assertFalse(any(k.startswith("SCORE_") for k in scanner_keys))

    def test_db_score_rows_do_not_override_code_score_weights(self):
        original = {
            "SCORE_W_WIN": config.SCORE_W_WIN,
            "SCORE_W_ACT": config.SCORE_W_ACT,
            "SCORE_W_ROI": config.SCORE_W_ROI,
            "SCORE_STRETCH": config.SCORE_STRETCH,
            "SCORE_THICK_REF": config.SCORE_THICK_REF,
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
