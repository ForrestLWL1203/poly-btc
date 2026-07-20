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
            seen_args = []
            real_mkstemp = tempfile.mkstemp

            def tracked_mkstemp(*args, **kwargs):
                result = real_mkstemp(*args, **kwargs)
                if kwargs.get("prefix") == "hyper-shadow-":
                    created.append(result[1])
                return result

            def fake_scan(db, _args):
                seen_args.append(_args)
                stamp = "2026-07-20T00:00:00Z"
                raw_addr = "0x1111111111111111111111111111111111111111"
                db.execute(
                    "INSERT INTO scan_generation(generation,source,status,started_at,published_at,complete,"
                    "is_current,metrics_json) VALUES ('shadow-g','scan','published',?,?,1,1,?)",
                    (stamp, stamp, json.dumps({
                        "officialRoiPassed": 0,
                        "perpPrefilterPassed": 0,
                        "selectionSearch": {
                            "selected": [raw_addr],
                            raw_addr: {"detail": f"candidate={raw_addr}"},
                        },
                    })),
                )
                db.commit()

            args = SimpleNamespace(full_scan=True, no_harvest=False)
            with patch.object(shadow_scan.tempfile, "mkstemp", side_effect=tracked_mkstemp), \
                 patch.object(shadow_scan.scanner, "scan", side_effect=fake_scan):
                report = shadow_scan.run(
                    source_path, report_path, args,
                    param_overrides={
                        "HARVEST_WEEK_ROI_MIN": 15,
                        "HARVEST_MONTH_ROI_MIN": 45,
                        "HARVEST_ALL_ROI_MIN": 50,
                        "HARVEST_WEEK_PNL_MIN": 2000,
                        "HARVEST_MONTH_PNL_MIN": 8000,
                        "HARVEST_ALL_PNL_MIN": 0,
                    },
                )

            self.assertEqual(Path(source_path).read_bytes(), before)
            self.assertTrue(report["sourceUnchanged"])
            self.assertEqual(report["scanParameters"]["HARVEST_WEEK_ROI_MIN"], 0.15)
            self.assertEqual(report["scanParameters"]["HARVEST_MONTH_ROI_MIN"], 0.45)
            self.assertEqual(report["scanParameters"]["HARVEST_ALL_ROI_MIN"], 0.50)
            self.assertEqual(report["scanParameters"]["HARVEST_WEEK_PNL_MIN"], 2000)
            self.assertEqual(report["scanParameters"]["HARVEST_MONTH_PNL_MIN"], 8000)
            self.assertEqual(report["scanParameters"]["HARVEST_ALL_PNL_MIN"], 0)
            self.assertEqual(seen_args[0].week_roi_min, 0.15)
            self.assertEqual(seen_args[0].month_roi_min, 0.45)
            self.assertEqual(seen_args[0].all_roi_min, 0.50)
            self.assertTrue(Path(report_path).exists())
            self.assertEqual(os.stat(report_path).st_mode & 0o777, 0o600)
            self.assertTrue(created)
            self.assertTrue(all(not Path(path).exists() for path in created))
            report_text = Path(report_path).read_text(encoding="utf-8")
            self.assertNotIn("0x1111111111111111111111111111111111111111", report_text)
            self.assertIn(shadow_scan._mask("0x1111111111111111111111111111111111111111"), report_text)


if __name__ == "__main__":
    unittest.main()
