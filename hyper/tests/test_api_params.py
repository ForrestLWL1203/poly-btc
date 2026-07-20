import os
import sqlite3
import tempfile
import unittest
from importlib import import_module, util

from dashboard.api import params as api_params


class ApiParamsTests(unittest.TestCase):
    def test_params_endpoints_are_split_from_api_module(self):
        self.assertIsNotNone(util.find_spec("dashboard.api.params"))
        api_params = import_module("dashboard.api.params")

        self.assertTrue(callable(api_params.ep_params))
        self.assertTrue(callable(api_params.patch_params))
        self.assertTrue(callable(api_params.reset_params))

    def test_retired_min_follow_score_cannot_be_patched(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE params ("
                "key TEXT PRIMARY KEY,value TEXT,category TEXT,level TEXT,type TEXT,"
                "effect TEXT,default_value TEXT,updated_at TEXT)"
            )
            db.execute(
                "INSERT INTO params (key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES ('MIN_FOLLOW_SCORE','0.7','follow','green','float','immediate','0.7',NULL)"
            )
            db.commit()
            db.close()

            updated = api_params.patch_params(path, "follow", {"MIN_FOLLOW_SCORE": 77})

            self.assertEqual(updated, {})
            db = sqlite3.connect(path)
            stored = db.execute("SELECT value FROM params WHERE key='MIN_FOLLOW_SCORE'").fetchone()[0]
            db.close()
            self.assertEqual(stored, "0.7")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_patch_coin_blacklist_normalizes_list(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE params ("
                "key TEXT PRIMARY KEY,value TEXT,category TEXT,level TEXT,type TEXT,"
                "effect TEXT,default_value TEXT,updated_at TEXT)"
            )
            db.execute(
                "INSERT INTO params (key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES ('COIN_BLACKLIST','','follow','green','text','immediate','',NULL)"
            )
            db.commit()
            db.close()

            updated = api_params.patch_params(path, "follow", {"COIN_BLACKLIST": " xyz:shkx, btc\nETH "})

            self.assertEqual(updated, {"COIN_BLACKLIST": " xyz:shkx, btc\nETH "})
            db = sqlite3.connect(path)
            stored = db.execute("SELECT value FROM params WHERE key='COIN_BLACKLIST'").fetchone()[0]
            db.close()
            self.assertEqual(stored, "BTC, ETH, XYZ:SHKX")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_margin_equity_pct_api_enforces_manual_range(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE params ("
                "key TEXT PRIMARY KEY,value TEXT,category TEXT,level TEXT,type TEXT,"
                "effect TEXT,default_value TEXT,updated_at TEXT)"
            )
            db.execute(
                "INSERT INTO params (key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES ('MARGIN_EQUITY_PCT','100','follow','yellow','pct','immediate','100',NULL)"
            )
            db.commit()
            db.close()

            self.assertEqual(
                api_params.patch_params(path, "follow", {"MARGIN_EQUITY_PCT": 50}),
                {"MARGIN_EQUITY_PCT": 50},
            )
            with self.assertRaisesRegex(ValueError, "between 10 and 100"):
                api_params.patch_params(path, "follow", {"MARGIN_EQUITY_PCT": 5})
            db = sqlite3.connect(path)
            self.assertEqual(db.execute(
                "SELECT value FROM params WHERE key='MARGIN_EQUITY_PCT'"
            ).fetchone()[0], "50")
            db.close()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_follow_api_rejects_tier_margin_that_cannot_fit_four_adds(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE params ("
                "key TEXT PRIMARY KEY,value TEXT,category TEXT,level TEXT,type TEXT,"
                "effect TEXT,default_value TEXT,updated_at TEXT)"
            )
            rows = [
                ("MARGIN_EQUITY_PCT", "100"), ("MIN_OPEN_MARGIN_PCT", "0.5"),
                ("STABLE_MARGIN_PCT", "8.5"), ("STABLE_COIN_CAP_PCT", "40"),
                ("MID_MARGIN_PCT", "5.2"), ("MID_COIN_CAP_PCT", "22"),
                ("HIGH_MARGIN_PCT", "3.625"), ("HIGH_COIN_CAP_PCT", "15"),
            ]
            db.executemany(
                "INSERT INTO params (key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES (?,?,'follow','yellow','pct','immediate',?,NULL)",
                [(key, value, value) for key, value in rows],
            )
            db.commit()
            db.close()

            self.assertEqual(
                api_params.patch_params(path, "follow", {"STABLE_MARGIN_PCT": 8.5}),
                {"STABLE_MARGIN_PCT": 8.5},
            )
            with self.assertRaisesRegex(ValueError, "容纳不了4次加仓"):
                api_params.patch_params(path, "follow", {"HIGH_MARGIN_PCT": 3.7})
            db = sqlite3.connect(path)
            self.assertEqual(db.execute(
                "SELECT value FROM params WHERE key='HIGH_MARGIN_PCT'"
            ).fetchone()[0], "3.625")
            db.close()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_tail_close_api_rejects_inverted_thresholds_atomically(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE params ("
                "key TEXT PRIMARY KEY,value TEXT,category TEXT,level TEXT,type TEXT,"
                "effect TEXT,default_value TEXT,updated_at TEXT)"
            )
            db.executemany(
                "INSERT INTO params (key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES (?,?,'follow','yellow','pct','immediate',?,NULL)",
                [
                    ("TAIL_CLOSE_HARD_REMAIN_PCT", "20", "20"),
                    ("TAIL_CLOSE_RISK_REMAIN_PCT", "35", "35"),
                    ("TAIL_CLOSE_PROFIT_GIVEBACK_PCT", "50", "50"),
                ],
            )
            db.commit()
            db.close()

            with self.assertRaisesRegex(ValueError, "must not exceed"):
                api_params.patch_params(path, "follow", {
                    "TAIL_CLOSE_HARD_REMAIN_PCT": 40,
                    "TAIL_CLOSE_RISK_REMAIN_PCT": 30,
                })

            db = sqlite3.connect(path)
            stored = dict(db.execute(
                "SELECT key,value FROM params WHERE key LIKE 'TAIL_CLOSE_%'"
            ).fetchall())
            db.close()
            self.assertEqual(stored["TAIL_CLOSE_HARD_REMAIN_PCT"], "20")
            self.assertEqual(stored["TAIL_CLOSE_RISK_REMAIN_PCT"], "35")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_tail_close_api_allows_disabling_with_stale_child_thresholds(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE params ("
                "key TEXT PRIMARY KEY,value TEXT,category TEXT,level TEXT,type TEXT,"
                "effect TEXT,default_value TEXT,updated_at TEXT)"
            )
            db.executemany(
                "INSERT INTO params (key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES (?,?, 'follow',? ,?,'immediate',?,NULL)",
                [
                    ("TAIL_CLOSE_ENABLE", "true", "green", "bool", "true"),
                    ("TAIL_CLOSE_HARD_REMAIN_PCT", "20", "yellow", "pct", "20"),
                    ("TAIL_CLOSE_RISK_REMAIN_PCT", "35", "yellow", "pct", "35"),
                ],
            )
            db.commit()
            db.close()

            result = api_params.patch_params(path, "follow", {
                "TAIL_CLOSE_ENABLE": False,
                "TAIL_CLOSE_HARD_REMAIN_PCT": 40,
                "TAIL_CLOSE_RISK_REMAIN_PCT": 30,
            })

            self.assertFalse(result["TAIL_CLOSE_ENABLE"])
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_ep_params_can_include_score_distribution(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            db.execute(
                "CREATE TABLE params ("
                "key TEXT PRIMARY KEY,value TEXT,category TEXT,level TEXT,type TEXT,"
                "effect TEXT,default_value TEXT,updated_at TEXT)"
            )
            db.execute("CREATE TABLE watchlist (addr TEXT PRIMARY KEY,score REAL)")
            db.executemany(
                "INSERT INTO watchlist (addr,score) VALUES (?,?)",
                [("0x1", 0.723), ("0x2", 0.681)],
            )
            db.commit()

            out = api_params.ep_params(db, include_score_dist=True)

            self.assertEqual(out["scoreDist"], {"scores": [72.3, 68.1], "total": 2})
            db.close()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
