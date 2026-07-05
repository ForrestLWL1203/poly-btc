import tempfile
import unittest
from pathlib import Path

from hl import scanner, storage


def _profile_row(addr, status, score, **overrides):
    cols = storage.PROFILE_COLS.split(",")
    row = {c: None for c in cols}
    row.update(
        addr=addr,
        status=status,
        reason="ok" if status == "active" else "retired",
        score=score,
        n_fills=10,
        n_trades=5,
        window_days=14,
        trades_per_day=0.5,
        taker_frac_notl=0.5,
        median_hold_s=3600,
        win_rate=0.7,
        net_pnl=100,
        roi_equity=0.1,
        total_notl=1000,
        acct_value=10000,
        perp_frac=1,
        max_drawdown=0,
        age_days=14,
        top_coin="BTC",
        market_type="crypto",
        times_active=1,
        first_added="2026-07-05T00:00:00Z",
        last_refreshed="2026-07-05T00:00:00Z",
        last_fill_ms=1,
    )
    row.update(overrides)
    return [row.get(c) for c in cols]


class ScannerWatchlistTests(unittest.TestCase):
    def test_ensure_watchlist_current_rebuilds_stale_derived_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.91),
                    _profile_row("0xbbb", "active", 0.82),
                    _profile_row("0xold", "retired", 0.0),
                ],
            )
            db.executemany(
                "INSERT INTO leaderboard (addr,display_name,mon_roi) VALUES (?,?,?)",
                [("0xaaa", "alpha", 0.1), ("0xbbb", "beta", 0.2), ("0xold", "old", -0.1)],
            )
            db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0xold',0,'stale')")
            db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (2,'0xaaa',0.91,'stale')")
            db.commit()

            n = scanner.ensure_watchlist_current(db, "2026-07-06T00:00:00Z")

            self.assertEqual(n, 2)
            rows = db.execute("SELECT rank,addr,score,updated_at FROM watchlist ORDER BY rank").fetchall()
            self.assertEqual([(r[0], r[1], r[2], r[3]) for r in rows],
                             [(1, "0xaaa", 0.91, "2026-07-06T00:00:00Z"),
                              (2, "0xbbb", 0.82, "2026-07-06T00:00:00Z")])


if __name__ == "__main__":
    unittest.main()
