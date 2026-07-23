import json
import os
from pathlib import Path
import tempfile
import unittest

from hyper import storage
from hyper.discovery import frozen_audit, pipeline_audit


class FrozenAuditTests(unittest.TestCase):
    def test_report_is_read_only_redacted_and_contains_first_decision(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "source.db")
            report_path = str(Path(td) / "report.json")
            db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,is_current,started_at,published_at) "
                "VALUES ('g1','published',1,1,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            db.execute(
                "INSERT INTO leaderboard_staging "
                "(generation,addr,account_value,week_vlm,week_pnl,week_roi,mon_pnl,mon_roi,is_candidate) "
                "VALUES ('g1','0xaaa',10000,300000,100,0.01,2000,0.20,1)"
            )
            pipeline_audit._insert_event(
                db, stamp="s1", source="scan", stage="perp_prefilter", addr="0xaaa",
                status="passed", reason="perp_prefilter_passed",
            )
            pipeline_audit._insert_event(
                db, stamp="s1", source="scan", stage="profile", addr="0xaaa",
                status="active", reason="ok", payload={
                    "followEligibility": {
                        "eligible": True, "coreEligible": False,
                        "status": "challenger_copy_weekly_evidence_building",
                        "checks": {
                            "strictCopy30dPositive": True,
                            "strictCopyWeeklyPositive": False,
                        },
                    }
                },
            )
            db.commit()
            db.close()
            before = os.stat(db_path).st_mtime_ns

            result = frozen_audit.build(db_path, report_path, generation="g1", stamp="s1")

            self.assertEqual(os.stat(db_path).st_mtime_ns, before)
            self.assertEqual(os.stat(report_path).st_mode & 0o777, 0o600)
            self.assertEqual(result["funnel"]["coarseRecall"], 1)
            self.assertEqual(result["firstDecisionCounts"]["personal_core"], 1)
            raw = Path(report_path).read_text(encoding="utf-8")
            self.assertNotIn("0xaaa", raw)
            self.assertIn("wallet_", raw)
            loaded = json.loads(raw)
            self.assertFalse(loaded["networkUsed"])


if __name__ == "__main__":
    unittest.main()
