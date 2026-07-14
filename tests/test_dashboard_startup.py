import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hl_dashboard
from hl import storage


class DashboardStartupTests(unittest.TestCase):
    def test_locked_migrator_falls_back_to_existing_readable_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "hl.db")
            db = storage.connect(path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.close()
            with patch.object(
                hl_dashboard.storage,
                "connect",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                hl_dashboard._initialize_db(path)

    def test_locked_migrator_rejects_an_incomplete_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "hl.db")
            sqlite3.connect(path).close()
            with patch.object(
                hl_dashboard.storage,
                "connect",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaisesRegex(RuntimeError, "dashboard_schema_incomplete"):
                    hl_dashboard._initialize_db(path)


if __name__ == "__main__":
    unittest.main()
