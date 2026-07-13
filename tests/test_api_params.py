import os
import sqlite3
import tempfile
import unittest
from importlib import import_module, util

from hl import api_params


class ApiParamsTests(unittest.TestCase):
    def test_params_endpoints_are_split_from_api_module(self):
        self.assertIsNotNone(util.find_spec("hl.api_params"))
        api_params = import_module("hl.api_params")

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
