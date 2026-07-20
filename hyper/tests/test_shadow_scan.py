import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from hyper import storage
from hyper.discovery import shadow_scan


class ShadowScanTests(unittest.TestCase):
    def test_source_is_unchanged_report_is_private_and_temp_db_is_removed(self):
        with tempfile.TemporaryDirectory() as td:
            source_path = str(Path(td) / "source.db")
            report_path = str(Path(td) / "report.json")
            source = storage.connect(source_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            source.execute(
                "INSERT INTO process_status(name,state,heartbeat_at) VALUES ('observer','running','now')"
            )
            source.commit()
            source.close()
            before = Path(source_path).read_bytes()
            created = []
            real_mkstemp = tempfile.mkstemp

            def tracked_mkstemp(*args, **kwargs):
                result = real_mkstemp(*args, **kwargs)
                if kwargs.get("prefix") == "hyper-shadow-":
                    created.append(result[1])
                return result

            def fake_scan(db, _args):
                stamp = "2026-07-20T00:00:00Z"
                db.execute(
                    "INSERT INTO scan_generation(generation,source,status,started_at,published_at,complete,"
                    "is_current,metrics_json) VALUES ('shadow-g','scan','published',?,?,1,1,?)",
                    (stamp, stamp, json.dumps({"officialRoiPassed": 0, "perpPrefilterPassed": 0})),
                )
                db.commit()

            args = SimpleNamespace(full_scan=True, no_harvest=False)
            with patch.object(shadow_scan.tempfile, "mkstemp", side_effect=tracked_mkstemp), \
                 patch.object(shadow_scan.scanner, "scan", side_effect=fake_scan):
                report = shadow_scan.run(source_path, report_path, args)

            self.assertEqual(Path(source_path).read_bytes(), before)
            self.assertTrue(report["sourceUnchanged"])
            self.assertTrue(Path(report_path).exists())
            self.assertEqual(os.stat(report_path).st_mode & 0o777, 0o600)
            self.assertTrue(created)
            self.assertTrue(all(not Path(path).exists() for path in created))


if __name__ == "__main__":
    unittest.main()
