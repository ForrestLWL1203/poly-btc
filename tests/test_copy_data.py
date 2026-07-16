import json
import sqlite3
import unittest

from hl.copy_data import load_copyable_fills, normalize_copyable_fills


class CopyDataTests(unittest.TestCase):
    def test_normalizer_excludes_spot_and_opaque_builder_and_orders_time_addr_tid(self):
        rows = [
            {"time": 2, "tid": 9, "user": "0xB", "coin": "BTC"},
            {"time": 1, "tid": 3, "user": "0xB", "coin": "foo:PRIVATE"},
            {"time": 1, "tid": 2, "user": "0xB", "coin": "ETH/USDC"},
            {"time": 1, "tid": 6, "user": "0xB", "coin": "#4830", "dir": "Settlement", "px": "0"},
            {"time": 1, "tid": 4, "user": "0xB", "coin": "XYZ:MU"},
            {"time": 1, "tid": 5, "user": "0xA", "coin": "ETH"},
        ]

        result = normalize_copyable_fills(rows)

        self.assertEqual(
            [(row["time"], row["user"], row["tid"], row["coin"]) for row in result],
            [(1, "0xa", 5, "ETH"), (1, "0xb", 4, "xyz:MU"), (2, "0xb", 9, "BTC")],
        )

    def test_db_loader_uses_wallet_policy_fail_closed(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE candidate_fills(addr TEXT, tid INTEGER, time INTEGER, fill_json TEXT)")
        for tid, coin in enumerate(("BTC", "xyz:MU", "dex:SECRET"), 1):
            fill = {"time": tid, "tid": tid, "coin": coin}
            db.execute("INSERT INTO candidate_fills VALUES ('0xabc',?,?,?)", (tid, tid, json.dumps(fill)))

        result = load_copyable_fills(
            db,
            ["0xabc"],
            0,
            policies={"0xabc": {"crypto": {"allow": True}, "stock": {"allow": False}}},
            policy_default=False,
        )

        self.assertEqual([row["coin"] for row in result], ["BTC"])


if __name__ == "__main__":
    unittest.main()
