import sqlite3
import tempfile
import unittest
from pathlib import Path

from hl import storage
from hl.fills import build_episodes
from hl.scanner import _episode_rows


class EpisodeConsistencyTests(unittest.TestCase):
    def test_storage_migrates_episode_primary_key_to_sequence(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "old.db"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE episode (
                    addr TEXT, coin TEXT, side TEXT, open_ms INTEGER, close_ms INTEGER,
                    hold_s REAL, net_pnl REAL, fee REAL, max_notl REAL, n_fills INTEGER,
                    open_px REAL, close_px REAL,
                    PRIMARY KEY (addr, coin, open_ms)
                );
                INSERT INTO episode VALUES ('0xabc','BTC','long',1000,2000,1,5,0.1,100,1,100,101);
                """
            )
            db.commit()
            db.close()

            migrated = storage.connect(str(path), storage.DISCOVERY_SCHEMA)
            cols = [r[1] for r in migrated.execute("PRAGMA table_info(episode)").fetchall()]
            self.assertIn("seq", cols)
            self.assertIn("open_complete", cols)
            migrated.execute(
                "INSERT INTO episode (addr,coin,side,open_ms,seq,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("0xabc", "BTC", "short", 1000, 1, 3000, 1, -2, 0.1, 100, 1, 101, 100),
            )
            migrated.commit()
            n = migrated.execute(
                "SELECT COUNT(*) FROM episode WHERE addr='0xabc' AND coin='BTC' AND open_ms=1000"
            ).fetchone()[0]
            self.assertEqual(n, 2)

    def test_episode_rows_preserve_duplicate_open_ms(self):
        eps = [
            {
                "coin": "BTC",
                "side": "long",
                "open_ms": 1000,
                "close_ms": 1100,
                "hold_s": 0.1,
                "net_pnl": 1.0,
                "fee": 0.1,
                "max_notl": 100.0,
                "n_fills": 1,
                "open_px": 100.0,
                "close_px": 101.0,
            },
            {
                "coin": "BTC",
                "side": "short",
                "open_ms": 1000,
                "close_ms": 1200,
                "hold_s": 0.2,
                "net_pnl": -1.0,
                "fee": 0.1,
                "max_notl": 100.0,
                "n_fills": 1,
                "open_px": 101.0,
                "close_px": 100.0,
            },
        ]

        rows = _episode_rows("0xabc", eps)

        self.assertEqual(len(rows), 2)
        self.assertEqual([r[4] for r in rows], [0, 1])

    def test_left_censored_episode_is_marked_incomplete(self):
        episodes, _ = build_episodes([
            {"coin": "HYPE", "time": 1000, "px": "10", "sz": "1", "side": "B",
             "startPosition": "1", "oid": 1, "fee": "0", "closedPnl": "0"},
            {"coin": "HYPE", "time": 2000, "px": "11", "sz": "2", "side": "A",
             "startPosition": "2", "oid": 2, "fee": "0", "closedPnl": "2"},
        ])

        self.assertEqual(len(episodes), 1)
        self.assertFalse(episodes[0]["open_complete"])
        self.assertEqual(_episode_rows("0xabc", episodes)[0][-1], 0)


if __name__ == "__main__":
    unittest.main()
