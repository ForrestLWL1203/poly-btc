import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hl import params
from hl.copy_backtest_cli import position_rows, run_wallet


class CopyBacktestCliTests(unittest.TestCase):
    def test_run_wallet_reads_cached_fills_and_coin_vol(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bt.db"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE candidate_fills (
                    addr TEXT NOT NULL,
                    tid INTEGER NOT NULL,
                    time INTEGER NOT NULL,
                    fill_json TEXT NOT NULL,
                    PRIMARY KEY (addr, tid)
                );
                CREATE TABLE coin_vol (
                    coin TEXT PRIMARY KEY,
                    sigma REAL,
                    sigma_fast REAL,
                    sigma_slow REAL,
                    n INTEGER,
                    updated_at TEXT
                );
                """
            )
            db.execute("INSERT INTO coin_vol (coin,sigma) VALUES ('ZEC',0.10)")
            fills = [
                {"time": 1, "tid": 1, "coin": "ZEC", "side": "A", "sz": "100", "startPosition": "0", "px": "100", "oid": 1, "crossed": True},
                {"time": 2, "tid": 2, "coin": "ZEC", "side": "A", "sz": "100", "startPosition": "-100", "px": "100.5", "oid": 2, "crossed": True},
                {"time": 3, "tid": 3, "coin": "ZEC", "side": "B", "sz": "200", "startPosition": "-200", "px": "101", "oid": 3, "crossed": True},
            ]
            for x in fills:
                db.execute("INSERT INTO candidate_fills VALUES (?,?,?,?)", ("0xabc", x["tid"], x["time"], json.dumps(x)))
            db.commit()

            result = run_wallet(db, "0xabc", start_ms=0)

            self.assertEqual(result["closed_n"], 1)
            self.assertEqual(result["missed_adds"], 1)
            self.assertEqual(result["sigmas"]["ZEC"], 0.10)

    def test_run_wallet_uses_db_follow_sizing_params(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bt.db"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE candidate_fills (
                    addr TEXT NOT NULL,
                    tid INTEGER NOT NULL,
                    time INTEGER NOT NULL,
                    fill_json TEXT NOT NULL,
                    PRIMARY KEY (addr, tid)
                );
                CREATE TABLE coin_vol (
                    coin TEXT PRIMARY KEY,
                    sigma REAL,
                    sigma_fast REAL,
                    sigma_slow REAL,
                    n INTEGER,
                    updated_at TEXT
                );
                CREATE TABLE params (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    category TEXT,
                    level TEXT,
                    type TEXT,
                    effect TEXT,
                    default_value TEXT,
                    updated_at TEXT
                );
                """
            )
            params.seed_params(db)
            db.execute("UPDATE params SET value='1.5' WHERE key='STABLE_MARGIN_PCT'")
            db.execute("UPDATE params SET value='25' WHERE key='STABLE_LEV_CAP'")
            db.execute("UPDATE params SET value='2500' WHERE key='STABLE_MIN_NOTIONAL'")
            db.execute("INSERT INTO coin_vol (coin,sigma) VALUES ('BTC',0.04)")
            fills = [
                {"time": 1, "tid": 1, "coin": "BTC", "side": "B", "sz": "10000", "startPosition": "0", "px": "100", "oid": 1, "crossed": True},
                {"time": 2, "tid": 2, "coin": "BTC", "side": "A", "sz": "10000", "startPosition": "10000", "px": "101", "oid": 2, "crossed": True},
            ]
            for x in fills:
                db.execute("INSERT INTO candidate_fills VALUES (?,?,?,?)", ("0xabc", x["tid"], x["time"], json.dumps(x)))
            db.commit()

            result = run_wallet(db, "0xabc", start_ms=0)

            self.assertEqual(result["closed_n"], 1)
            self.assertAlmostEqual(result["positions"][0]["margin"], 150.0)
            self.assertEqual(result["positions"][0]["leverage"], 25.0)

    def test_position_rows_sort_by_add_dependency(self):
        rows = [{
            "addr": "0xabc",
            "rank": 1,
            "positions": [
                {"coin": "BTC", "side": "long", "net_pnl": 1, "add_dependency": 0.1, "target_adds": 1, "missed_adds": 0, "followed_adds": 1, "fee_drag": 1},
                {"coin": "ZEC", "side": "short", "net_pnl": -5, "add_dependency": 10.0, "target_adds": 4, "missed_adds": 4, "followed_adds": 0, "fee_drag": 2},
            ],
        }]

        out = position_rows(rows, limit=1)

        self.assertEqual(out[0]["coin"], "ZEC")
        self.assertEqual(out[0]["addr"], "0xabc")


if __name__ == "__main__":
    unittest.main()
