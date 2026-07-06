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

    def test_patch_min_follow_score_stores_native_score(self):
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

            self.assertEqual(updated, {"MIN_FOLLOW_SCORE": 77})
            db = sqlite3.connect(path)
            stored = db.execute("SELECT value FROM params WHERE key='MIN_FOLLOW_SCORE'").fetchone()[0]
            db.close()
            self.assertEqual(stored, "0.77")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
