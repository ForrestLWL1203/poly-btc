import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hl_observe
from hl import storage


class ObserverColdStartTests(unittest.TestCase):
    def test_empty_fresh_database_runs_idle_instead_of_exiting(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "hl.db")
            db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.close()
            seen = {}

            class FakeObserver:
                def __init__(self, _db, addrs, seed, **_kwargs):
                    seen["addrs"] = list(addrs)
                    seen["seed"] = dict(seed)

                async def run(self):
                    seen["ran"] = True

            with patch.object(sys, "argv", ["hl_observe.py", "--db", db_path, "observe"]), \
                    patch.object(hl_observe.observer, "Observer", FakeObserver):
                code = hl_observe.main()

            self.assertEqual(code, 0)
            self.assertEqual(seen["addrs"], [])
            self.assertTrue(seen["ran"])


if __name__ == "__main__":
    unittest.main()
