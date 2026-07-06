import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hl import scanner, scanner_lifecycle, storage


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


def _leaderboard_row(addr, account=20_000, week_pnl=2_000, week_vlm=1_000_000, mon_pnl=4_000, all_pnl=10_000):
    return {
        "ethAddress": addr,
        "displayName": None,
        "accountValue": account,
        "windowPerformances": [
            ("day", {"pnl": 100, "roi": 0.01, "vlm": 100_000}),
            ("week", {"pnl": week_pnl, "roi": week_pnl / account, "vlm": week_vlm}),
            ("month", {"pnl": mon_pnl, "roi": mon_pnl / account, "vlm": week_vlm * 2}),
            ("allTime", {"pnl": all_pnl, "roi": all_pnl / account, "vlm": week_vlm * 4}),
        ],
    }


class ScannerWatchlistTests(unittest.TestCase):
    def test_harvest_clears_stale_candidate_flags_before_current_leaderboard(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.execute(
                "INSERT INTO leaderboard (addr,is_candidate,fetched_at,mon_roi,week_roi) VALUES "
                "('0xstale',1,'old',0.9,0.8)"
            )
            db.commit()
            p = SimpleNamespace(
                min_acct=10_000,
                week_vlm_min=500_000,
                week_vlm_max=100_000_000,
                pnl_vol_min=0.001,
                pnl_vol_max=0.08,
            )

            with patch.object(scanner.rest, "get_leaderboard", return_value=[
                _leaderboard_row("0xfresh"),
            ]):
                n = scanner.harvest(db, p)

            self.assertEqual(n, 1)
            rows = dict(db.execute("SELECT addr,is_candidate FROM leaderboard").fetchall())
            self.assertEqual(rows["0xstale"], 0)
            self.assertEqual(rows["0xfresh"], 1)

    def test_prune_discovery_cache_removes_disappeared_non_active_profiles(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xgone", "rejected", 0.0),
                    _profile_row("0xcand", "rejected", 0.0),
                    _profile_row("0xactive", "active", 0.9),
                ],
            )
            db.executemany(
                "INSERT INTO leaderboard (addr,is_candidate,fetched_at,mon_roi,week_roi) VALUES (?,?,?,?,?)",
                [
                    ("0xgone", 0, "2026-07-01T00:00:00Z", 0.1, 0.1),
                    ("0xcand", 1, "2026-07-02T00:00:00Z", 0.2, 0.2),
                    ("0xactive", 0, "2026-07-01T00:00:00Z", 0.3, 0.3),
                ],
            )
            db.executemany(
                "INSERT INTO candidate_fills (addr,tid,time,fill_json) VALUES (?,?,?,?)",
                [
                    ("0xgone", 1, 1, "{}"),
                    ("0xcand", 2, 1, "{}"),
                    ("0xactive", 3, 1, "{}"),
                ],
            )
            db.executemany(
                "INSERT INTO episode (addr,coin,side,open_ms,seq,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("0xgone", "BTC", "long", 1, 0, 2, 1, 1, 0, 100, 2, 100, 101),
                    ("0xactive", "BTC", "long", 1, 0, 2, 1, 1, 0, 100, 2, 100, 101),
                ],
            )
            db.commit()

            counts = scanner_lifecycle.prune_discovery_cache(db)

            self.assertEqual(counts["profiles"], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile WHERE addr='0xgone'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM episode WHERE addr='0xgone'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidate_fills WHERE addr='0xgone'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile WHERE addr='0xcand'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile WHERE addr='0xactive'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leaderboard WHERE addr='0xgone'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leaderboard WHERE addr='0xactive'").fetchone()[0], 1)

    def test_incremental_scan_workset_rechecks_current_top_ranked_rejected_tail(self):
        cand = ["0xactive", "0xold_good", "0xnew", "0xold_tail"]
        active = ["0xactive"]
        profiled = {"0xactive", "0xold_good", "0xold_tail"}

        workset, mode = scanner_lifecycle.profile_workset(
            cand,
            active,
            profiled,
            full_scan=False,
            limit=100,
            daily_recheck_top=2,
        )

        self.assertEqual(workset, ["0xactive", "0xnew", "0xold_good"])
        self.assertIn("1 active + 1 new + 1 top-recheck", mode)
        self.assertIn("top-recheck", mode)

    def test_incremental_scan_workset_keeps_off_list_actives_and_dedupes_recheck(self):
        cand = ["0xactive", "0xold_good", "0xnew"]
        active = ["0xactive", "0xoff"]
        profiled = {"0xactive", "0xold_good", "0xoff"}

        workset, _mode = scanner_lifecycle.profile_workset(
            cand,
            active,
            profiled,
            full_scan=False,
            limit=100,
            daily_recheck_top=3,
        )

        self.assertEqual(workset, ["0xactive", "0xnew", "0xold_good", "0xoff"])

    def test_full_scan_workset_uses_all_candidates_plus_off_list_actives(self):
        cand = ["0xa", "0xb"]
        active = ["0xb", "0xoff"]

        workset, mode = scanner_lifecycle.profile_workset(
            cand,
            active,
            profiled={"0xa", "0xb", "0xoff"},
            full_scan=True,
            limit=100,
            daily_recheck_top=0,
        )

        self.assertEqual(workset, ["0xa", "0xb", "0xoff"])
        self.assertIn("FULL", mode)

    def test_copy_backtest_gate_rejects_copy_loss_with_enough_sample(self):
        m = {}
        ok, reason = scanner._apply_copy_bt_gate(
            m,
            {"copy_net_pnl": -1.0, "copy_win_rate": 0.4, "closed_n": 8,
             "opened_n": 10, "target_open_events": 12, "liquidations": 1, "fee_drag": 3.5},
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "copy_backtest_loss")
        self.assertEqual(m["copy_bt_closed_n"], 8)
        self.assertEqual(m["copy_bt_liquidations"], 1)
        self.assertAlmostEqual(m["copy_bt_open_fill_rate"], 10 / 12)

    def test_copy_backtest_gate_records_but_allows_thin_sample_loss(self):
        m = {}
        ok, reason = scanner._apply_copy_bt_gate(
            m,
            {"copy_net_pnl": -10.0, "copy_win_rate": 0.0, "closed_n": 3,
             "opened_n": 3, "target_open_events": 3, "liquidations": 0, "fee_drag": 1.0},
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(m["copy_bt_net_pnl"], -10.0)
        self.assertEqual(m["copy_bt_closed_n"], 3)

    def test_copy_backtest_gate_allows_positive_copy_result(self):
        ok, reason = scanner._apply_copy_bt_gate(
            {},
            {"copy_net_pnl": 12.0, "copy_win_rate": 0.6, "closed_n": 7,
             "opened_n": 8, "target_open_events": 8, "liquidations": 0, "fee_drag": 2.0},
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_copy_backtest_gate_rejects_recent_window_loss_with_enough_sample(self):
        m = {}
        ok, reason = scanner._apply_copy_bt_gate(
            m,
            {
                30: {"copy_net_pnl": 100.0, "copy_win_rate": 0.6, "closed_n": 8,
                     "opened_n": 8, "target_open_events": 8, "liquidations": 0, "fee_drag": 5.0},
                14: {"copy_net_pnl": -12.0, "copy_win_rate": 0.25, "closed_n": 4,
                     "opened_n": 4, "target_open_events": 4, "liquidations": 0, "fee_drag": 2.0},
                7: {"copy_net_pnl": 3.0, "copy_win_rate": 0.5, "closed_n": 2,
                    "opened_n": 2, "target_open_events": 2, "liquidations": 0, "fee_drag": 1.0},
            },
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_days=30,
                            copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "copy_backtest_loss_14d")
        self.assertEqual(m["copy_bt_net_pnl"], 100.0)
        self.assertEqual(m["copy_bt_closed_n"], 8)
        self.assertEqual(m["copy_bt_14d_net_pnl"], -12.0)
        self.assertEqual(m["copy_bt_14d_closed_n"], 4)

    def test_copy_backtest_gate_allows_primary_loss_when_recent_windows_recover(self):
        m = {"net_pnl": 50.0, "roi_total": 0.05, "net_30d": 200.0, "net_life": 500.0}
        ok, reason = scanner._apply_copy_bt_gate(
            m,
            {
                30: {"copy_net_pnl": -80.0, "copy_win_rate": 0.45, "closed_n": 12,
                     "opened_n": 12, "target_open_events": 12, "liquidations": 0, "fee_drag": 6.0},
                14: {"copy_net_pnl": 35.0, "copy_win_rate": 0.6, "closed_n": 4,
                     "opened_n": 4, "target_open_events": 4, "liquidations": 0, "fee_drag": 2.0},
                7: {"copy_net_pnl": 8.0, "copy_win_rate": 0.5, "closed_n": 2,
                    "opened_n": 2, "target_open_events": 2, "liquidations": 0, "fee_drag": 1.0},
            },
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_days=30,
                            copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(m["copy_bt_net_pnl"], -80.0)
        self.assertEqual(m["copy_bt_14d_net_pnl"], 35.0)
        self.assertEqual(m["copy_bt_7d_net_pnl"], 8.0)

    def test_copy_backtest_gate_keeps_primary_loss_when_target_perp_is_not_profitable(self):
        ok, reason = scanner._apply_copy_bt_gate(
            {"net_pnl": -1.0, "roi_total": -0.01, "net_30d": 200.0, "net_life": 500.0},
            {
                30: {"copy_net_pnl": -80.0, "copy_win_rate": 0.45, "closed_n": 12,
                     "opened_n": 12, "target_open_events": 12, "liquidations": 0, "fee_drag": 6.0},
                14: {"copy_net_pnl": 35.0, "copy_win_rate": 0.6, "closed_n": 4,
                     "opened_n": 4, "target_open_events": 4, "liquidations": 0, "fee_drag": 2.0},
                7: {"copy_net_pnl": 8.0, "copy_win_rate": 0.5, "closed_n": 2,
                    "opened_n": 2, "target_open_events": 2, "liquidations": 0, "fee_drag": 1.0},
            },
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_days=30,
                            copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "copy_backtest_loss")

    def test_copy_backtest_gate_keeps_primary_loss_when_recent_recovery_is_thin(self):
        ok, reason = scanner._apply_copy_bt_gate(
            {"net_pnl": 50.0, "roi_total": 0.05, "net_30d": 200.0, "net_life": 500.0},
            {
                30: {"copy_net_pnl": -80.0, "copy_win_rate": 0.45, "closed_n": 12,
                     "opened_n": 12, "target_open_events": 12, "liquidations": 0, "fee_drag": 6.0},
                14: {"copy_net_pnl": 35.0, "copy_win_rate": 0.6, "closed_n": 4,
                     "opened_n": 4, "target_open_events": 4, "liquidations": 0, "fee_drag": 2.0},
                7: {"copy_net_pnl": 8.0, "copy_win_rate": 0.5, "closed_n": 1,
                    "opened_n": 1, "target_open_events": 1, "liquidations": 0, "fee_drag": 1.0},
            },
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_days=30,
                            copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "copy_backtest_loss")

    def test_copy_backtest_gate_records_but_allows_thin_recent_window_loss(self):
        m = {}
        ok, reason = scanner._apply_copy_bt_gate(
            m,
            {
                30: {"copy_net_pnl": 100.0, "copy_win_rate": 0.6, "closed_n": 8,
                     "opened_n": 8, "target_open_events": 8, "liquidations": 0, "fee_drag": 5.0},
                7: {"copy_net_pnl": -3.0, "copy_win_rate": 0.0, "closed_n": 1,
                    "opened_n": 1, "target_open_events": 1, "liquidations": 0, "fee_drag": 1.0},
            },
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_days=30,
                            copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(m["copy_bt_7d_net_pnl"], -3.0)
        self.assertEqual(m["copy_bt_7d_closed_n"], 1)

    def test_regate_rejects_profile_when_copy_backtest_loses(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            cols = storage.PROFILE_COLS.split(",")
            now_ms = int(time.time() * 1000)
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                _profile_row(
                    "0xaaa",
                    "active",
                    0.91,
                    n_trades=10,
                    n_fills=20,
                    active_days=8,
                    activity_ratio=0.8,
                    median_eps=1,
                    median_hold_s=3600,
                    win_rate=0.7,
                    net_pnl=100,
                    roi_equity=0.1,
                    roi_total=0.1,
                    net_30d=100,
                    net_life=500,
                    pf_equity=10_000,
                    pf_mon_pnl=800,
                    pf_mon_vlm=20_000,
                    pf_week_pnl=100,
                    pf_week_vlm=5_000,
                    pf_turnover=1,
                    payoff_ratio=1.2,
                    avg_notional=1_000,
                    last_fill_ms=now_ms,
                ),
            )
            db.commit()
            p = SimpleNamespace(
                min_perp=0.3,
                evidence_min_days=5,
                evidence_min_trades=7,
                max_daily_eps=10,
                exclude_hft=True,
                hft_min_hold_min=3.0,
                grid_max_adds=5,
                max_single_adds=20,
                max_fills_per_ep=50,
                max_concurrent_pos=15,
                inactive_days=7,
                min_activity=0.1,
                portfolio_max_turnover=80,
                portfolio_min_edge_bps=10,
                windfall_conc=0.8,
                windfall_win_max=0.6,
                min_active_score=0.0,
                copy_bt_gate_enable=True,
                copy_bt_days=30,
                copy_bt_min_closed=7,
                copy_bt_min_net_pnl=0.0,
            )

            with patch.object(scanner, "_copy_bt_result", return_value={
                "copy_net_pnl": -25.0,
                "copy_win_rate": 0.3,
                "closed_n": 9,
                "opened_n": 9,
                "target_open_events": 9,
                "liquidations": 0,
                "fee_drag": 4.0,
            }):
                scanner.regate(db, p)

            row = db.execute(
                "SELECT status,reason,copy_bt_net_pnl,copy_bt_closed_n FROM profile WHERE addr='0xaaa'"
            ).fetchone()
            self.assertEqual(tuple(row), ("retired", "copy_backtest_loss", -25.0, 9))

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
