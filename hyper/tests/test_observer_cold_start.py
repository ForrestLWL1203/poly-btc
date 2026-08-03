import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hyper.cli import observe as hl_observe
from hyper import storage


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

            with patch.object(sys, "argv", ["hyper.cli.observe", "--db", db_path, "observe"]), \
                    patch.object(hl_observe.observer, "Observer", FakeObserver):
                code = hl_observe.main()

            self.assertEqual(code, 0)
            self.assertEqual(seen["addrs"], [])
            self.assertTrue(seen["ran"])

    def test_live_idle_diagnostic_does_not_count_paper_positions(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "hl.db")
            db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g-live','published',1,1,1,'2026-01-01','2026-01-02')"
            )
            db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,opened_at) "
                "VALUES ('0xpaper','BTC','long','open','2026-01-01')"
            )
            db.execute(
                "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
                "VALUES (1,'live','live_running','session-live','2026-01-02') "
                "ON CONFLICT(id) DO UPDATE SET selected_mode='live',state='live_running',"
                "active_session_id='session-live'"
            )
            db.commit()
            db.close()

            class FakeObserver:
                def __init__(self, *_args, **_kwargs):
                    pass

                async def run(self):
                    pass

            with patch.object(sys, "argv", ["hyper.cli.observe", "--db", db_path, "observe"]), \
                    patch.object(hl_observe.observer, "Observer", FakeObserver), \
                    patch("builtins.print") as output:
                code = hl_observe.main()

            self.assertEqual(code, 0)
            output.assert_any_call("selection g-live has zero enabled Core wallets; observer is running idle.")


if __name__ == "__main__":
    unittest.main()
