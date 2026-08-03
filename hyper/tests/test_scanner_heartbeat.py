import json
import tempfile
import unittest
from pathlib import Path

from hyper import storage
from hyper.discovery import scanner


class ScannerHeartbeatTests(unittest.TestCase):
    def _database(self, directory):
        path = Path(directory) / "hl.db"
        db = storage.connect(str(path), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        db.execute(
            "INSERT INTO scan_progress "
            "(id,state,stage,candidates_scanned,candidates_total,updated_at) "
            "VALUES (1,'scanning','portfolio_tune_coarse',8,16,'2000-01-01T00:00:00Z')"
        )
        db.execute(
            "INSERT INTO process_status (name,state,pid,heartbeat_at,detail_json) "
            "VALUES ('scanner','scanning',1,'2000-01-01T00:00:00Z','{}')"
        )
        db.commit()
        return db, path

    def test_writer_refreshes_one_existing_status_row_from_scan_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            db, path = self._database(directory)

            self.assertTrue(scanner._write_scanner_heartbeat(path))

            row = db.execute(
                "SELECT state,heartbeat_at,detail_json FROM process_status WHERE name='scanner'"
            ).fetchone()
            self.assertEqual(row[0], "scanning")
            self.assertNotEqual(row[1], "2000-01-01T00:00:00Z")
            self.assertEqual(json.loads(row[2]), {
                "scanned": 8, "stage": "portfolio_tune_coarse", "total": 16,
            })
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM process_status WHERE name='scanner'").fetchone()[0],
                1,
            )
            db.close()

    def test_writer_never_resurrects_an_idle_scanner(self):
        with tempfile.TemporaryDirectory() as directory:
            db, path = self._database(directory)
            db.execute(
                "UPDATE process_status SET state='idle' WHERE name='scanner'"
            )
            db.commit()

            self.assertFalse(scanner._write_scanner_heartbeat(path))

            row = db.execute(
                "SELECT state,heartbeat_at FROM process_status WHERE name='scanner'"
            ).fetchone()
            self.assertEqual(tuple(row), ("idle", "2000-01-01T00:00:00Z"))
            db.close()


if __name__ == "__main__":
    unittest.main()
