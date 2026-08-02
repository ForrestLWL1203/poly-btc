import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hyper import storage
from hyper.execution import control
from hyper.execution.observer import Observer


class ObserverLiveDrainTests(unittest.TestCase):
    def _db(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = storage.connect(
            str(Path(temp.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )
        db.row_factory = sqlite3.Row
        control.ensure_execution_control(db)
        db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
            "sizing_anchor,margin_equity_pct,sizing_equity,started_at,updated_at) "
            "VALUES ('live-drain','live','mainnet','draining',?,?, 'revision',169.4,1,169.4,'t','t')",
            ("0x" + "1" * 40, "0x" + "2" * 40),
        )
        db.execute(
            "UPDATE execution_control SET selected_mode='live',state='draining',"
            "active_session_id='live-drain' WHERE id=1"
        )
        db.execute(
            "INSERT INTO process_status (name,state,pid,heartbeat_at) "
            "VALUES ('observer','paused',123,'t')"
        )
        db.commit()
        return db

    def test_flat_drain_marks_process_stopped_and_wakes_websocket(self):
        async def run():
            db = self._db()
            observer = Observer(db, [], {})
            observer.live_executor = SimpleNamespace(session={"session_id": "live-drain"})
            observer.ws = SimpleNamespace(close=AsyncMock())

            self.assertTrue(observer._finish_live_session_if_drained())
            await asyncio.sleep(0)

            control = db.execute(
                "SELECT selected_mode,state,active_session_id FROM execution_control WHERE id=1"
            ).fetchone()
            session = db.execute(
                "SELECT state,stop_reason FROM execution_session WHERE session_id='live-drain'"
            ).fetchone()
            process = db.execute(
                "SELECT state,pid FROM process_status WHERE name='observer'"
            ).fetchone()
            self.assertEqual(tuple(control), ("live", "live_ready", None))
            self.assertEqual(tuple(session), ("stopped", "drained"))
            self.assertEqual(tuple(process), ("stopped", None))
            self.assertTrue(observer.stop)
            self.assertFalse(observer.paused)
            self.assertFalse(observer.draining)
            observer.ws.close.assert_awaited_once()
            db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
