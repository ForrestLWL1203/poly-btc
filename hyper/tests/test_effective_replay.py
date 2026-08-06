import json
import inspect
import sqlite3
import unittest

from hyper.discovery import scanner
from hyper.selection import effective_replay


class EffectiveReplayTests(unittest.TestCase):
    def test_final_selection_replay_excludes_watch_only_sectors(self):
        source = inspect.getsource(scanner._build_forced_prefix_selection)
        marker = "The final published estimate must describe the immutable execution policy"
        final_block = source[source.index(marker):]
        self.assertIn("include_watch=False", final_block)

    def test_active_context_uses_immutable_target_policy(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE active_strategy_revision (id INTEGER,revision TEXT)")
        db.execute(
            "CREATE TABLE strategy_revision (revision TEXT,selection_generation TEXT,parent_revision TEXT,"
            "source TEXT,status TEXT,params_json TEXT,params_hash TEXT,targets_json TEXT,"
            "validation_json TEXT,reason TEXT,created_at TEXT,activated_at TEXT,superseded_at TEXT)"
        )
        db.execute(
            "CREATE TABLE scan_generation (id INTEGER PRIMARY KEY,generation TEXT,status TEXT,complete INTEGER,is_current INTEGER)"
        )
        db.execute(
            "CREATE TABLE follow_selection (generation TEXT,addr TEXT,role TEXT,enabled INTEGER,selection_rank INTEGER)"
        )
        db.execute("CREATE TABLE target_controls (addr TEXT,enabled INTEGER,intent TEXT,pinned INTEGER,pinned_at TEXT)")
        target = {
            "addr": "0xabc", "sectorPolicy": {
                "allowed": ["crypto"], "crypto": {"allow": True},
            },
        }
        db.execute("INSERT INTO scan_generation VALUES (1,'g1','published',1,1)")
        db.execute("INSERT INTO follow_selection VALUES ('g1','0xabc','core',1,1)")
        db.execute("INSERT INTO active_strategy_revision VALUES (1,'r1')")
        db.execute(
            "INSERT INTO strategy_revision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "r1", "g1", None, "scanner", "active", json.dumps({"MID_LEV_CAP": 10}),
                "h", json.dumps([target]), "{}", None, "now", "now", None,
            ),
        )

        follow, addrs, policies = effective_replay.active_execution_context(db, "g1")

        self.assertEqual(addrs, ["0xabc"])
        self.assertEqual(policies["0xabc"]["allowed"], ["crypto"])
        self.assertEqual(follow["MID_LEV_CAP"], 10)

    def test_strict_payload_excludes_open_profit_from_roi(self):
        base = {
            "window_start_equity": 100.0, "window_end_equity": 180.0,
            "copy_net_pnl": 80.0, "closed_net_pnl": 50.0, "unrealized_pnl": 30.0,
            "actionable_open_rate": 1.0, "execution_capacity_fit": 1.0,
            "price_path_coverage": 1.0, "maintenance_margin_coverage": 1.0,
        }
        payload = effective_replay._strict_payload({30: base, 7: base}, 4, {})

        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["dynamicReturn30d"], .5)
        self.assertEqual(payload["markedNetPnl30d"], 80.0)
        self.assertEqual(payload["netPnl30d"], 50.0)
        self.assertEqual(payload["validationSource"], "active_revision_allowed_strict")


if __name__ == "__main__":
    unittest.main()
