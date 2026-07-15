import tempfile
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hl import params, scanner, scanner_copy_bt, scanner_lifecycle, storage


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
        copy_bt_closed_n=16,
        copy_bt_14d_closed_n=9,
        copy_bt_7d_closed_n=5,
        copy_expected_return=0.04,
        copy_return_lcb=0.01,
        copy_return_volatility=0.08,
        copy_positive_probability=0.82,
        copy_evidence_days=10,
        copy_recent_return_14d=0.03,
        copy_recent_return_7d=0.02,
        copy_risk_score=0.80,
        execution_score=0.90,
        actionable_open_rate=0.90,
        capacity_fit=0.90,
        open_probability_48h=0.75,
        evidence_status="qualified",
        data_status="valid",
    )
    row.update(overrides)
    if "actionable_open_rate" not in overrides and "copy_bt_open_fill_rate" in overrides:
        row["actionable_open_rate"] = overrides["copy_bt_open_fill_rate"]
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
    def test_repair_missing_episode_rows_rebuilds_from_cached_fills(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            fills = [
                {
                    "tid": 1,
                    "time": 1_000,
                    "coin": "BTC",
                    "side": "B",
                    "sz": "1",
                    "startPosition": "0",
                    "px": "100",
                    "closedPnl": "0",
                    "fee": "0.1",
                },
                {
                    "tid": 2,
                    "time": 2_000,
                    "coin": "BTC",
                    "side": "A",
                    "sz": "1",
                    "startPosition": "1",
                    "px": "101",
                    "closedPnl": "1",
                    "fee": "0.1",
                },
            ]
            db.executemany(
                "INSERT INTO candidate_fills (addr,tid,time,fill_json) VALUES (?,?,?,?)",
                [("0xaaa", x["tid"], x["time"], json.dumps(x)) for x in fills],
            )
            db.commit()

            repaired = scanner.repair_missing_episode_rows(db, ["0xaaa"])

            self.assertEqual(repaired, 1)
            row = db.execute("SELECT addr,coin,side,open_ms,close_ms,n_fills FROM episode").fetchone()
            self.assertEqual(tuple(row), ("0xaaa", "BTC", "long", 1_000, 2_000, 2))

    def test_open_snapshot_uses_material_position_for_underwater_risk(self):
        def position(coin, szi, entry_px, mark_px, unrealized_pnl):
            return {
                "position": {
                    "coin": coin,
                    "szi": str(szi),
                    "entryPx": str(entry_px),
                    "positionValue": str(abs(szi) * mark_px),
                    "unrealizedPnl": str(unrealized_pnl),
                    "leverage": {"type": "cross", "value": 3},
                }
            }

        clearinghouse = {
            "marginSummary": {"accountValue": "100000", "totalNtlPos": "105008.4"},
            "assetPositions": [
                position("KAITO", -100000, 1.0, 1.05, -5000.0),
                position("XPL", 10, 1.0, 0.84, -1.6),
            ],
        }

        with patch.object(scanner.rest, "clearinghouse_state", return_value=clearinghouse), \
                patch.object(scanner.rest, "spot_clearinghouse_state", return_value={"balances": []}):
            snap = scanner._open_snapshot(
                "0xwallet",
                {None},
                [{"coin": "KAITO", "open_ms": 1}, {"coin": "XPL", "open_ms": 1}],
                scanner._DAY_MS * 4,
                100000,
            )

        self.assertAlmostEqual(snap["worst_underwater"], -0.05, places=6)
        self.assertEqual(snap["bag_count"], 1)

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

    def test_incremental_workset_breakdown_separates_new_recheck_and_off_list_active(self):
        cand = ["0xactive", "0xold_good", "0xnew", "0xold_tail"]
        active = ["0xactive", "0xoff"]
        profiled = {"0xactive", "0xold_good", "0xold_tail", "0xoff"}

        breakdown = scanner_lifecycle.profile_workset_breakdown(
            cand,
            active,
            profiled,
            full_scan=False,
            limit=100,
            daily_recheck_top=2,
        )

        self.assertEqual(breakdown["workset"], ["0xactive", "0xnew", "0xold_good", "0xoff"])
        self.assertEqual(breakdown["counts"]["active_candidate"], 1)
        self.assertEqual(breakdown["counts"]["new_candidate"], 1)
        self.assertEqual(breakdown["counts"]["top_recheck"], 1)
        self.assertEqual(breakdown["counts"]["off_list_active"], 1)
        self.assertEqual(breakdown["counts"]["workset"], 4)
        self.assertEqual(breakdown["counts"]["deferred_tail"], 1)

    def test_workset_breakdown_counts_only_wallets_inside_limit(self):
        cand = ["0xactive", "0xnew", "0xold_good"]
        active = ["0xactive", "0xoff"]
        profiled = {"0xactive", "0xold_good", "0xoff"}

        breakdown = scanner_lifecycle.profile_workset_breakdown(
            cand,
            active,
            profiled,
            full_scan=False,
            limit=2,
            daily_recheck_top=3,
        )

        self.assertEqual(breakdown["workset"], ["0xactive", "0xnew"])
        self.assertEqual(breakdown["counts"]["active_candidate"], 1)
        self.assertEqual(breakdown["counts"]["new_candidate"], 1)
        self.assertEqual(breakdown["counts"]["top_recheck"], 0)
        self.assertEqual(breakdown["counts"]["off_list_active"], 0)
        self.assertEqual(breakdown["counts"]["workset"], 2)

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
        ok, reason = scanner_copy_bt.apply_copy_bt_gate(
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
        ok, reason = scanner_copy_bt.apply_copy_bt_gate(
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
        ok, reason = scanner_copy_bt.apply_copy_bt_gate(
            {},
            {"copy_net_pnl": 12.0, "copy_win_rate": 0.6, "closed_n": 7,
             "opened_n": 8, "target_open_events": 8, "liquidations": 0, "fee_drag": 2.0},
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_copy_backtest_gate_rejects_recent_window_loss_with_enough_sample(self):
        m = {}
        ok, reason = scanner_copy_bt.apply_copy_bt_gate(
            m,
            {
                30: {"copy_net_pnl": 100.0, "copy_win_rate": 0.6, "closed_n": 8,
                     "opened_n": 8, "target_open_events": 8, "liquidations": 0, "fee_drag": 5.0},
                14: {"copy_net_pnl": -12.0, "copy_win_rate": 0.25, "closed_n": 5,
                     "opened_n": 5, "target_open_events": 5, "liquidations": 0, "fee_drag": 2.0},
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
        self.assertEqual(m["copy_bt_14d_closed_n"], 5)

    def test_copy_backtest_gate_allows_primary_loss_when_recent_windows_recover(self):
        m = {"net_pnl": 50.0, "roi_total": 0.05, "net_30d": 200.0, "net_life": 500.0}
        ok, reason = scanner_copy_bt.apply_copy_bt_gate(
            m,
            {
                30: {"copy_net_pnl": -80.0, "copy_win_rate": 0.45, "closed_n": 12,
                     "opened_n": 12, "target_open_events": 12, "liquidations": 0, "fee_drag": 6.0},
                14: {"copy_net_pnl": 35.0, "copy_win_rate": 0.6, "closed_n": 5,
                     "opened_n": 5, "target_open_events": 5, "liquidations": 0, "fee_drag": 2.0},
                7: {"copy_net_pnl": 8.0, "copy_win_rate": 0.5, "closed_n": 5,
                    "opened_n": 5, "target_open_events": 5, "liquidations": 0, "fee_drag": 1.0},
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
        ok, reason = scanner_copy_bt.apply_copy_bt_gate(
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
        ok, reason = scanner_copy_bt.apply_copy_bt_gate(
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
        ok, reason = scanner_copy_bt.apply_copy_bt_gate(
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

    def test_copy_backtest_gate_does_not_treat_two_7d_closes_as_enough_sample(self):
        m = {}
        ok, reason = scanner_copy_bt.apply_copy_bt_gate(
            m,
            {
                30: {"copy_net_pnl": 100.0, "copy_win_rate": 0.6, "closed_n": 8,
                     "opened_n": 8, "target_open_events": 8, "liquidations": 0, "fee_drag": 5.0},
                7: {"copy_net_pnl": -3.0, "copy_win_rate": 0.0, "closed_n": 2,
                    "opened_n": 2, "target_open_events": 2, "liquidations": 0, "fee_drag": 1.0},
            },
            SimpleNamespace(copy_bt_gate_enable=True, copy_bt_days=30,
                            copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0),
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(m["copy_bt_7d_closed_n"], 2)

    def test_sector_copy_gate_keeps_wallet_when_crypto_copy_wins_and_stock_loses(self):
        m = {"net_pnl": 1000.0, "roi_total": 0.1, "net_30d": 1000.0, "net_life": 3000.0}
        p = SimpleNamespace(copy_bt_gate_enable=True, copy_bt_days=30,
                            copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0)

        ok, reason = scanner_copy_bt.apply_sector_copy_bt_gate(
            m,
            {
                30: {"copy_net_pnl": -600.0, "copy_win_rate": 0.4, "closed_n": 20,
                     "opened_n": 20, "target_open_events": 20, "liquidations": 0, "fee_drag": 15.0},
                14: {"copy_net_pnl": -300.0, "copy_win_rate": 0.4, "closed_n": 12,
                     "opened_n": 12, "target_open_events": 12, "liquidations": 0, "fee_drag": 8.0},
                7: {"copy_net_pnl": -120.0, "copy_win_rate": 0.4, "closed_n": 8,
                    "opened_n": 8, "target_open_events": 8, "liquidations": 0, "fee_drag": 4.0},
            },
            {
                "crypto": {
                    30: {"copy_net_pnl": 1200.0, "copy_win_rate": 0.7, "closed_n": 10, "wins": 7,
                         "opened_n": 10, "target_open_events": 10, "liquidations": 0, "fee_drag": 7.0},
                    14: {"copy_net_pnl": 600.0, "copy_win_rate": 0.67, "closed_n": 6, "wins": 4,
                         "opened_n": 6, "target_open_events": 6, "liquidations": 0, "fee_drag": 4.0},
                    7: {"copy_net_pnl": 240.0, "copy_win_rate": 0.6, "closed_n": 5, "wins": 3,
                        "opened_n": 5, "target_open_events": 5, "liquidations": 0, "fee_drag": 2.0},
                },
                "stock": {
                    30: {"copy_net_pnl": -1800.0, "copy_win_rate": 0.2, "closed_n": 10, "wins": 2,
                         "opened_n": 10, "target_open_events": 10, "liquidations": 0, "fee_drag": 8.0},
                    14: {"copy_net_pnl": -900.0, "copy_win_rate": 0.17, "closed_n": 6, "wins": 1,
                         "opened_n": 6, "target_open_events": 6, "liquidations": 0, "fee_drag": 4.0},
                    7: {"copy_net_pnl": -360.0, "copy_win_rate": 0.2, "closed_n": 5, "wins": 1,
                        "opened_n": 5, "target_open_events": 5, "liquidations": 0, "fee_drag": 2.0},
                },
            },
            p,
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        policy = json.loads(m["sector_policy_json"])
        self.assertEqual(policy["allowed"], ["crypto"])
        self.assertTrue(policy["crypto"]["allow"])
        self.assertFalse(policy["stock"]["allow"])
        self.assertEqual(m["copy_bt_net_pnl"], -600.0)

    def test_sector_copy_gate_keeps_no_profitable_sector_as_challenger_only(self):
        m = {"net_pnl": 100.0, "roi_total": 0.1, "net_30d": 100.0, "net_life": 300.0}
        p = SimpleNamespace(copy_bt_gate_enable=True, copy_bt_days=30,
                            copy_bt_min_closed=7, copy_bt_min_net_pnl=0.0)

        ok, reason = scanner_copy_bt.apply_sector_copy_bt_gate(
            m,
            {
                30: {"copy_net_pnl": -100.0, "closed_n": 14, "opened_n": 14, "target_open_events": 14},
                14: {"copy_net_pnl": -60.0, "closed_n": 8, "opened_n": 8, "target_open_events": 8},
                7: {"copy_net_pnl": -30.0, "closed_n": 6, "opened_n": 6, "target_open_events": 6},
            },
            {
                "crypto": {
                    30: {"copy_net_pnl": -40.0, "closed_n": 7, "opened_n": 7, "target_open_events": 7},
                    14: {"copy_net_pnl": -20.0, "closed_n": 5, "opened_n": 5, "target_open_events": 5},
                },
                "stock": {
                    30: {"copy_net_pnl": -60.0, "closed_n": 7, "opened_n": 7, "target_open_events": 7},
                    14: {"copy_net_pnl": -40.0, "closed_n": 5, "opened_n": 5, "target_open_events": 5},
                },
            },
            p,
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "copy_backtest_challenger_only")
        self.assertEqual(m["copy_bt_evidence_status"], "economically_disqualified")

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
                copy_bt_gate_enable=True,
                copy_bt_days=30,
                copy_bt_min_closed=7,
                copy_bt_min_net_pnl=0.0,
            )

            with patch.object(scanner, "_copy_bt_results", return_value={
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
            self.assertEqual(tuple(row), ("retired", "normalized_evidence_missing", -25.0, 9))

    def test_regate_reactivates_obsolete_low_quality_outcome_when_copy_gates_pass(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g-current','published',1,1,1,'now','now')"
            )
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                _profile_row(
                    "0xaaa", "retired", 0.0,
                    reason="low_quality", profile_generation="g-current",
                ),
            )
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                _profile_row("0xstale", "active", 0.8, profile_generation="g-old"),
            )
            db.commit()
            p = SimpleNamespace()

            with (
                patch.object(scanner.metrics, "gates_structural", return_value=(True, "ok")),
                patch.object(scanner.metrics, "gates_state", return_value=(True, "ok")),
                patch.object(scanner.metrics, "score", return_value=0.581),
                patch.object(scanner, "_copy_bt_cached_fills", return_value=[]),
                patch.object(scanner, "_copy_bt_results", return_value={}),
                patch.object(scanner, "_sector_copy_bt_results", return_value={}),
                patch.object(scanner, "_apply_sector_copy_bt_gate", return_value=(True, "ok")),
                patch.object(scanner, "_copy_profile_evidence"),
                patch.object(scanner, "_profile_copy_qualification", return_value=(True, "ok")),
                patch.object(scanner.pipeline_audit, "record_profile_snapshot"),
                patch.object(scanner, "refresh_watchlist", return_value=1),
            ):
                scanner.regate(db, p, quiet=True)

            row = db.execute(
                "SELECT status,reason,score,raw_quality_score FROM profile WHERE addr='0xaaa'"
            ).fetchone()
            self.assertEqual(row[0], "active")
            self.assertEqual(row[1], "ok")
            self.assertAlmostEqual(row[2], 0.581)
            self.assertAlmostEqual(row[3], 0.581)
            stale = db.execute(
                "SELECT status,reason,score FROM profile WHERE addr='0xstale'"
            ).fetchone()
            self.assertEqual(tuple(stale), ("retired", "stale_generation", 0.0))

    def test_regate_retires_low_copy_fill_rate_profile(self):
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
                copy_bt_gate_enable=True,
                copy_bt_days=30,
                copy_bt_min_closed=7,
                copy_bt_min_net_pnl=0.0,
            )

            with patch.object(scanner, "_copy_bt_results", return_value={
                "copy_net_pnl": 120.0,
                "copy_win_rate": 0.6,
                "closed_n": 9,
                "opened_n": 4,
                "target_open_events": 10,
                "liquidations": 0,
                "fee_drag": 4.0,
            }):
                scanner.regate(db, p)

            row = db.execute(
                "SELECT status,reason,copy_bt_open_fill_rate FROM profile WHERE addr='0xaaa'"
            ).fetchone()
            self.assertEqual(row[0], "retired")
            self.assertEqual(row[1], "normalized_evidence_missing")
            self.assertAlmostEqual(row[2], 0.4)

    def test_regate_retires_thin_recent_copy_sample(self):
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
                copy_bt_gate_enable=True,
                copy_bt_days=30,
                copy_bt_min_closed=7,
                copy_bt_min_net_pnl=0.0,
            )

            with patch.object(scanner, "_copy_bt_results", return_value={
                30: {"copy_net_pnl": 200.0, "copy_win_rate": 0.6, "closed_n": 9,
                     "opened_n": 9, "target_open_events": 9, "liquidations": 0, "fee_drag": 4.0},
                14: {"copy_net_pnl": 50.0, "copy_win_rate": 0.5, "closed_n": 4,
                     "opened_n": 4, "target_open_events": 4, "liquidations": 0, "fee_drag": 2.0},
                7: {"copy_net_pnl": -5.0, "copy_win_rate": 0.0, "closed_n": 1,
                    "opened_n": 1, "target_open_events": 1, "liquidations": 0, "fee_drag": 1.0},
            }):
                scanner.regate(db, p)

            row = db.execute(
                "SELECT status,reason,copy_bt_7d_net_pnl,copy_bt_7d_closed_n FROM profile WHERE addr='0xaaa'"
            ).fetchone()
            self.assertEqual(tuple(row), ("retired", "normalized_evidence_missing", -5.0, 1))

    def test_ensure_watchlist_current_rebuilds_stale_derived_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            now_ms = int(time.time() * 1000)
            quality = dict(
                n_trades=12,
                n_fills=24,
                active_days=8,
                activity_ratio=0.8,
                median_eps=1,
                median_hold_s=3600,
                win_rate=0.7,
                net_pnl=500,
                roi_equity=0.10,
                roi_total=0.10,
                net_30d=500,
                net_life=900,
                pf_equity=10_000,
                pf_mon_pnl=700,
                pf_mon_vlm=20_000,
                pf_week_pnl=250,
                pf_week_vlm=8_000,
                pf_turnover=1,
                payoff_ratio=1.5,
                avg_notional=1_000,
                last_fill_ms=now_ms,
                copy_bt_net_pnl=900,
                copy_bt_14d_net_pnl=500,
                copy_bt_7d_net_pnl=200,
                copy_bt_closed_n=20,
                copy_bt_14d_closed_n=10,
                copy_bt_7d_closed_n=5,
                copy_bt_open_fill_rate=1.0,
            )
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.91, **quality),
                    _profile_row("0xbbb", "active", 0.82, **quality),
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
            self.assertEqual({r[1] for r in rows}, {"0xaaa", "0xbbb"})
            self.assertTrue(all(r[3] == "2026-07-06T00:00:00Z" for r in rows))

    def test_ensure_watchlist_current_repairs_derived_view_without_replaying_stale_copy_bt(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            now_ms = int(time.time() * 1000)
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                _profile_row(
                    "0xstale",
                    "active",
                    0.90,
                    n_trades=12,
                    n_fills=24,
                    active_days=8,
                    activity_ratio=0.8,
                    median_eps=1,
                    median_hold_s=3600,
                    win_rate=0.7,
                    net_pnl=500,
                    roi_equity=0.10,
                    roi_total=0.10,
                    net_30d=500,
                    net_life=900,
                    pf_equity=10_000,
                    pf_mon_pnl=700,
                    pf_mon_vlm=20_000,
                    pf_week_pnl=250,
                    pf_week_vlm=8_000,
                    pf_turnover=1,
                    payoff_ratio=1.5,
                    avg_notional=1_000,
                    last_fill_ms=now_ms,
                    copy_bt_net_pnl=1200,
                    copy_bt_14d_net_pnl=800,
                    copy_bt_7d_net_pnl=300,
                    copy_bt_closed_n=20,
                    copy_bt_14d_closed_n=10,
                    copy_bt_7d_closed_n=5,
                    copy_bt_open_fill_rate=0.95,
                ),
            )
            db.commit()
            losing_windows = {
                30: {"copy_net_pnl": -120.0, "closed_n": 12, "copy_win_rate": 0.25,
                     "opened_n": 12, "target_open_events": 12, "liquidations": 0, "fee_drag": 12.0},
                14: {"copy_net_pnl": -80.0, "closed_n": 8, "copy_win_rate": 0.25,
                     "opened_n": 8, "target_open_events": 8, "liquidations": 0, "fee_drag": 8.0},
                7: {"copy_net_pnl": -30.0, "closed_n": 5, "copy_win_rate": 0.0,
                    "opened_n": 5, "target_open_events": 5, "liquidations": 0, "fee_drag": 4.0},
            }

            with patch.object(scanner, "_copy_bt_cached_fills", return_value=[{"coin": "BTC", "time": 1}]), \
                    patch.object(scanner, "_copy_bt_results", return_value=losing_windows), \
                    patch.object(scanner, "_sector_copy_bt_results", return_value={"crypto": losing_windows, "stock": {}}):
                n = scanner.ensure_watchlist_current(db, "2026-07-06T00:00:00Z")

            self.assertEqual(n, 1)
            stale = db.execute(
                "SELECT status,reason,copy_bt_net_pnl FROM profile WHERE addr='0xstale'"
            ).fetchone()
            self.assertEqual(tuple(stale), ("active", "ok", 1200.0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0], 1)

    def test_refresh_watchlist_denormalizes_sector_policy_for_observer(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                _profile_row(
                    "0xsector",
                    "active",
                    0.80,
                    copy_bt_net_pnl=1000,
                    copy_bt_14d_net_pnl=600,
                    copy_bt_7d_net_pnl=250,
                    copy_bt_closed_n=12,
                    copy_bt_14d_closed_n=6,
                    copy_bt_7d_closed_n=5,
                    sector_policy_json=json.dumps({
                        "crypto": {"allow": True},
                        "stock": {"allow": False},
                        "allowed": ["crypto"],
                    }),
                    sector_copy_json=json.dumps({
                        "crypto": {"30": {"copy_net_pnl": 1000, "closed_n": 12}},
                        "stock": {"30": {"copy_net_pnl": -500, "closed_n": 12}},
                    }),
                ),
            )
            db.commit()

            with patch.object(scanner.auto_tune, "choose_follow_line_by_portfolio", return_value={
                "status": "ok", "reason": "portfolio_topn", "line": 0.60, "count": 1,
            }):
                scanner.refresh_watchlist(db, "2026-07-06T00:00:00Z")

            row = db.execute(
                "SELECT sector_policy_json,sector_copy_json FROM watchlist WHERE addr='0xsector'"
            ).fetchone()
            self.assertIn('"stock"', row[0])
            self.assertIn('"crypto"', row[1])

    def test_ensure_watchlist_current_ignores_rank_order_changes(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.91),
                    _profile_row("0xbbb", "active", 0.82),
                ],
            )
            db.executemany(
                "INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (?,?,?,?)",
                [
                    (1, "0xbbb", 0.95, "old"),
                    (2, "0xaaa", 0.70, "old"),
                ],
            )
            db.commit()

            n = scanner.ensure_watchlist_current(db, "2026-07-06T00:00:00Z")

            self.assertEqual(n, 2)
            rows = db.execute("SELECT rank,addr,score,updated_at FROM watchlist ORDER BY rank").fetchall()
            self.assertEqual([(r[0], r[1], r[2], r[3]) for r in rows],
                             [(1, "0xbbb", 0.95, "old"), (2, "0xaaa", 0.70, "old")])

    def test_refresh_watchlist_ranks_by_copy_follow_score_not_raw_score(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row(
                        "0xweak",
                        "active",
                        0.674,
                        copy_bt_net_pnl=1300.3,
                        copy_bt_14d_net_pnl=28.3,
                        copy_bt_7d_net_pnl=889.6,
                        copy_bt_closed_n=46,
                        copy_bt_14d_closed_n=30,
                        copy_bt_7d_closed_n=8,
                        copy_bt_open_fill_rate=1.0,
                        copy_expected_return=0.01,
                        copy_return_lcb=-0.01,
                        copy_positive_probability=0.71,
                        copy_risk_score=0.55,
                    ),
                    _profile_row(
                        "0xstrong",
                        "active",
                        0.667,
                        copy_bt_net_pnl=2270.8,
                        copy_bt_14d_net_pnl=1395.1,
                        copy_bt_7d_net_pnl=1446.3,
                        copy_bt_closed_n=41,
                        copy_bt_14d_closed_n=22,
                        copy_bt_7d_closed_n=17,
                        copy_bt_open_fill_rate=1.0,
                        copy_expected_return=0.07,
                        copy_return_lcb=0.025,
                        copy_positive_probability=0.90,
                        copy_risk_score=0.85,
                    ),
                ],
            )
            db.commit()

            scanner.refresh_watchlist(db, "2026-07-06T00:00:00Z")

            rows = db.execute("SELECT rank,addr,score FROM watchlist ORDER BY rank").fetchall()
            self.assertEqual([r[1] for r in rows], ["0xstrong", "0xweak"])
            self.assertGreater(rows[0][2], rows[1][2])

    @unittest.skip("retired score-line membership; explicit Core history is covered by selection tests")
    def test_refresh_watchlist_marks_only_newly_followed_wallet_first_time(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xold", "active", 0.90),
                    _profile_row("0xnew", "active", 0.88),
                    _profile_row("0xlegacy", "active", 0.86),
                ],
            )
            db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0xold',0.90,'old')")
            db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (2,'0xlegacy',0.86,'old')")
            db.execute(
                "INSERT INTO follow_history (addr,first_followed_at,last_followed_at,last_followed_score) "
                "VALUES ('0xold','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',0.90)"
            )
            db.execute(
                "INSERT INTO follow_history (addr,first_followed_at,last_followed_at,last_followed_score) "
                "VALUES ('0xlegacy',NULL,'2026-01-01T06:00:00Z',0.86)"
            )
            db.commit()

            with patch.object(scanner.auto_tune, "choose_follow_line_by_portfolio", return_value={
                "status": "ok", "reason": "portfolio_topn", "line": 0.70, "count": 2,
            }):
                scanner.refresh_watchlist(db, "2026-01-02T00:00:00Z")

            rows = {
                r[0]: {"first_followed_at": r[1], "last_followed_at": r[2]}
                for r in db.execute("SELECT addr,first_followed_at,last_followed_at FROM follow_history")
            }
            self.assertEqual(rows["0xold"]["first_followed_at"], "2026-01-01T00:00:00Z")
            self.assertEqual(rows["0xlegacy"]["first_followed_at"], "2026-01-01T06:00:00Z")
            self.assertEqual(rows["0xnew"]["first_followed_at"], "2026-01-02T00:00:00Z")
            self.assertEqual(rows["0xold"]["last_followed_at"], "2026-01-02T00:00:00Z")
            self.assertEqual(rows["0xlegacy"]["last_followed_at"], "2026-01-02T00:00:00Z")
            self.assertEqual(rows["0xnew"]["last_followed_at"], "2026-01-02T00:00:00Z")

    @unittest.skip("retired score-line membership")
    def test_refresh_watchlist_keeps_low_fill_rate_wallet_below_follow_line(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row(
                        "0xlowfill",
                        "active",
                        0.95,
                        copy_bt_net_pnl=1200,
                        copy_bt_14d_net_pnl=600,
                        copy_bt_7d_net_pnl=200,
                        copy_bt_closed_n=12,
                        copy_bt_14d_closed_n=6,
                        copy_bt_7d_closed_n=5,
                        copy_bt_open_fill_rate=0.55,
                    ),
                    _profile_row(
                        "0xcopy",
                        "active",
                        0.70,
                        copy_bt_net_pnl=900,
                        copy_bt_14d_net_pnl=450,
                        copy_bt_7d_net_pnl=200,
                        copy_bt_closed_n=12,
                        copy_bt_14d_closed_n=6,
                        copy_bt_7d_closed_n=5,
                        copy_bt_open_fill_rate=0.9,
                    ),
                ],
            )
            db.commit()

            with patch.object(scanner.config, "AUTO_FOLLOW_MIN_N", 1), \
                    patch.object(scanner.config, "AUTO_FOLLOW_TARGET_N", 2), \
                    patch.object(scanner.config, "AUTO_FOLLOW_MAX_N", 2), \
                    patch.object(scanner.config, "AUTO_FOLLOW_MIN_SCORE", 0.60), \
                    patch.object(scanner.config, "AUTO_FOLLOW_CLIFF_GAP", 1.0):
                scanner.refresh_watchlist(db, "2026-07-06T00:00:00Z")

            line = float(db.execute("SELECT value FROM params WHERE key='MIN_FOLLOW_SCORE'").fetchone()[0])
            rows = {r[0]: r[1] for r in db.execute("SELECT addr,score FROM watchlist").fetchall()}
            self.assertGreaterEqual(rows["0xcopy"], line)
            self.assertLess(rows["0xlowfill"], line)
            audit = db.execute(
                "SELECT reason,payload_json FROM pipeline_audit "
                "WHERE stage='watchlist' AND addr=?",
                ("0xlowfill",),
            ).fetchone()
            self.assertEqual(audit[0], "low_fill_rate")
            payload = json.loads(audit[1])
            self.assertEqual(payload["followEligibility"]["status"], "low_fill_rate")

    @unittest.skip("retired score-line membership")
    def test_refresh_watchlist_auto_moves_follow_line_to_target_rank(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.90),
                    _profile_row("0xbbb", "active", 0.80),
                    _profile_row("0xccc", "active", 0.70),
                ],
            )
            db.commit()

            with patch.object(scanner.config, "AUTO_FOLLOW_MIN_N", 1), \
                    patch.object(scanner.config, "AUTO_FOLLOW_TARGET_N", 2), \
                    patch.object(scanner.config, "AUTO_FOLLOW_MAX_N", 3), \
                    patch.object(scanner.config, "AUTO_FOLLOW_MIN_SCORE", 0.60), \
                    patch.object(scanner.config, "AUTO_FOLLOW_CLIFF_GAP", 1.0):
                scanner.refresh_watchlist(db, "2026-07-06T00:00:00Z")

            line = db.execute("SELECT value FROM params WHERE key='MIN_FOLLOW_SCORE'").fetchone()[0]
            cmd = db.execute("SELECT type,payload_json FROM commands ORDER BY id DESC LIMIT 1").fetchone()
            second_score = db.execute("SELECT score FROM watchlist ORDER BY rank LIMIT 1 OFFSET 1").fetchone()[0]
            self.assertAlmostEqual(float(line), second_score, places=6)
            self.assertEqual(cmd[0], "reload_params")
            payload = json.loads(cmd[1])
            self.assertEqual(payload["by"], "auto_follow_line")
            self.assertEqual(payload["reason"], "capacity_cap")
            self.assertEqual(payload["status"], "heuristic")
            self.assertEqual(payload["count"], 2)

    @unittest.skip("retired score-line membership")
    def test_auto_follow_line_keeps_fractional_boundary_wallet_included(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            boundary = 0.6724038118728887
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.91),
                    _profile_row("0xbbb", "active", boundary),
                    _profile_row("0xccc", "active", 0.61),
                ],
            )
            db.commit()

            with patch.object(scanner.config, "AUTO_FOLLOW_MIN_N", 1), \
                    patch.object(scanner.config, "AUTO_FOLLOW_TARGET_N", 2), \
                    patch.object(scanner.config, "AUTO_FOLLOW_MAX_N", 3), \
                    patch.object(scanner.config, "AUTO_FOLLOW_MIN_SCORE", 0.60), \
                    patch.object(scanner.config, "AUTO_FOLLOW_CLIFF_GAP", 1.0):
                scanner.refresh_watchlist(db, "2026-07-06T00:00:00Z")

            line = float(db.execute("SELECT value FROM params WHERE key='MIN_FOLLOW_SCORE'").fetchone()[0])
            followed = db.execute("SELECT COUNT(*) FROM watchlist WHERE score>=?", (line,)).fetchone()[0]
            boundary_score = db.execute("SELECT score FROM watchlist WHERE addr='0xbbb'").fetchone()[0]
            self.assertLessEqual(line, boundary_score)
            self.assertEqual(followed, 2)

    @unittest.skip("retired score-line membership")
    def test_refresh_watchlist_uses_portfolio_follow_line_choice(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.90),
                    _profile_row("0xbbb", "active", 0.80),
                    _profile_row("0xccc", "active", 0.70),
                ],
            )
            db.commit()

            with patch.object(scanner.auto_tune, "choose_follow_line_by_portfolio", return_value={
                "status": "ok",
                "reason": "portfolio_topn",
                "line": 0.799999999,
                "count": 2,
            }):
                scanner.refresh_watchlist(db, "2026-07-06T00:00:00Z")

            line = db.execute("SELECT value FROM params WHERE key='MIN_FOLLOW_SCORE'").fetchone()[0]
            cmd = db.execute("SELECT payload_json FROM commands ORDER BY id DESC LIMIT 1").fetchone()[0]
            self.assertAlmostEqual(float(line), 0.799999999)
            self.assertIn("portfolio_topn", cmd)
            self.assertIn('"count": 2', cmd)

    @unittest.skip("retired score-line membership")
    def test_refresh_watchlist_uses_explicit_lifecycle_instead_of_score_bonus(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            db.execute("UPDATE params SET value='0.70' WHERE key='MIN_FOLLOW_SCORE'")
            db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0xold',0.72,'old')")
            cols = storage.PROFILE_COLS.split(",")
            ready = dict(
                copy_bt_net_pnl=1000,
                copy_bt_14d_net_pnl=500,
                copy_bt_7d_net_pnl=200,
                copy_bt_closed_n=12,
                copy_bt_14d_closed_n=8,
                copy_bt_7d_closed_n=5,
                copy_bt_open_fill_rate=0.9,
            )
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xnew", "active", 0.90, **ready),
                    _profile_row("0xold", "active", 0.80, **ready),
                    _profile_row("0xtail", "active", 0.70, **ready),
                ],
            )
            db.commit()

            def fake_score(row):
                return {
                    "0xnew": (0.75, {"reasons": ["mock"]}),
                    "0xold": (0.72, {"reasons": ["mock"]}),
                    "0xtail": (0.68, {"reasons": ["mock"]}),
                }[row["addr"]]

            with patch.object(scanner.follow_score, "compute_follow_score", side_effect=fake_score), \
                    patch.object(scanner.auto_tune, "choose_follow_line_by_portfolio", return_value={
                        "status": "ok",
                        "reason": "portfolio_topn",
                        "line": 0.735,
                        "count": 2,
                    }):
                scanner.refresh_watchlist(db, "2026-07-08T00:00:00Z", source="scan")

            line = float(db.execute("SELECT value FROM params WHERE key='MIN_FOLLOW_SCORE'").fetchone()[0])
            rows = db.execute("SELECT addr,score FROM watchlist WHERE score>=? ORDER BY rank", (line,)).fetchall()
            self.assertEqual([r[0] for r in rows], ["0xnew"])
            old_score = db.execute("SELECT score FROM watchlist WHERE addr='0xold'").fetchone()[0]
            self.assertAlmostEqual(old_score, 0.72)
            audit = db.execute(
                "SELECT payload_json FROM pipeline_audit WHERE stage='watchlist' AND addr='0xold'"
            ).fetchone()
            self.assertEqual(json.loads(audit[0])["followDetail"]["stability"]["status"], "previously_followed")

    @unittest.skip("retired score-line membership")
    def test_refresh_watchlist_does_not_stabilize_thin_recent_wallet(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            db.execute("UPDATE params SET value='0.70' WHERE key='MIN_FOLLOW_SCORE'")
            db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0xold',0.72,'old')")
            cols = storage.PROFILE_COLS.split(",")
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                _profile_row(
                    "0xold",
                    "active",
                    0.80,
                    copy_bt_net_pnl=1000,
                    copy_bt_14d_net_pnl=500,
                    copy_bt_7d_net_pnl=200,
                    copy_bt_closed_n=12,
                    copy_bt_14d_closed_n=8,
                    copy_bt_7d_closed_n=2,
                    copy_bt_open_fill_rate=0.9,
                    copy_evidence_days=2,
                ),
            )
            db.commit()

            with patch.object(scanner.follow_score, "compute_follow_score", return_value=(0.72, {"reasons": ["mock"]})), \
                    patch.object(scanner.auto_tune, "choose_follow_line_by_portfolio", return_value={
                        "status": "ok",
                        "reason": "portfolio_topn",
                        "line": 0.70,
                        "count": 1,
                    }):
                scanner.refresh_watchlist(db, "2026-07-08T00:00:00Z", source="scan")

            line = float(db.execute("SELECT value FROM params WHERE key='MIN_FOLLOW_SCORE'").fetchone()[0])
            score = db.execute("SELECT score FROM watchlist WHERE addr='0xold'").fetchone()[0]
            self.assertLess(score, line)
            audit = db.execute(
                "SELECT reason,payload_json FROM pipeline_audit WHERE stage='watchlist' AND addr='0xold'"
            ).fetchone()
            self.assertEqual(audit[0], "thin_independent_evidence")
            self.assertEqual(json.loads(audit[1])["followDetail"]["stability"]["status"], "ineligible")

    @unittest.skip("retired score-line membership")
    def test_post_scan_pipeline_sets_follow_line_before_auto_tune(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.90),
                    _profile_row("0xbbb", "active", 0.80),
                    _profile_row("0xccc", "active", 0.70),
                ],
            )
            db.commit()
            seen = {}

            def fake_tune(db_arg, source, stamp):
                follow = params.load_follow(db_arg)
                seen["source"] = source
                seen["stamp"] = stamp
                seen["line"] = follow["MIN_FOLLOW_SCORE"]
                seen["addrs"] = scanner.auto_tune._load_followed_wallets(db_arg, follow)
                return {
                    "status": "ok",
                    "applied": False,
                    "applied_sizing": False,
                    "applied_add": False,
                    "followed_n": len(seen["addrs"]),
                    "selected_mult": 1.0,
                    "margins": {"STABLE_MARGIN_PCT": 0.04, "MID_MARGIN_PCT": 0.03, "HIGH_MARGIN_PCT": 0.03},
                    "lev_caps": {"STABLE_LEV_CAP": 25, "MID_LEV_CAP": 10, "HIGH_LEV_CAP": 4},
                    "deploy_full_pct": 0.40,
                    "params": {},
                    "add_params": {"ADD_GAP_K": 0.06, "ADD_GAP_SHRINK_G": 1.1, "ADD_MAX_HARD": 6},
                    "candidates": [{"mult": 1.0}],
                    "add_candidates": [{"gap_k": 0.06}],
                }

            with patch.object(scanner.auto_tune, "choose_follow_line_by_portfolio", return_value={
                "status": "ok",
                "reason": "portfolio_topn",
                "line": 0.735,
                "count": 2,
            }), patch.object(scanner.auto_tune, "maybe_tune_margins", side_effect=fake_tune):
                n = scanner.refresh_watchlist_and_auto_tune(db, "2026-07-06T00:00:00Z", source="scan")

            self.assertEqual(n, 3)
            self.assertAlmostEqual(seen["line"], 0.735)
            self.assertEqual(seen["addrs"], ["0xaaa", "0xbbb"])
            self.assertEqual(seen["source"], "scan")
            stages = [r[0] for r in db.execute(
                "SELECT stage FROM pipeline_audit WHERE stamp=? ORDER BY id",
                ("2026-07-06T00:00:00Z",),
            ).fetchall()]
            self.assertIn("follow_line", stages)
            self.assertIn("auto_tune", stages)
            self.assertIn("auto_tune", stages)

    def test_post_scan_pipeline_does_not_regate_stale_profiles_after_auto_tune(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            quality = dict(
                n_trades=12,
                n_fills=24,
                active_days=8,
                activity_ratio=0.8,
                median_eps=1,
                median_hold_s=3600,
                win_rate=0.7,
                net_pnl=500,
                roi_equity=0.10,
                roi_total=0.10,
                net_30d=500,
                net_life=900,
                pf_equity=10_000,
                pf_mon_pnl=700,
                pf_mon_vlm=20_000,
                pf_week_pnl=250,
                pf_week_vlm=8_000,
                pf_turnover=1,
                payoff_ratio=1.5,
                avg_notional=1_000,
                last_fill_ms=int(time.time() * 1000),
            )
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row(
                        "0xstale",
                        "active",
                        0.90,
                        copy_bt_net_pnl=1200,
                        copy_bt_14d_net_pnl=800,
                        copy_bt_7d_net_pnl=300,
                        copy_bt_closed_n=20,
                        copy_bt_14d_closed_n=10,
                        copy_bt_7d_closed_n=5,
                        copy_bt_open_fill_rate=0.95,
                        **quality,
                    ),
                    _profile_row(
                        "0xkeep",
                        "active",
                        0.80,
                        copy_bt_net_pnl=900,
                        copy_bt_14d_net_pnl=500,
                        copy_bt_7d_net_pnl=200,
                        copy_bt_closed_n=20,
                        copy_bt_14d_closed_n=10,
                        copy_bt_7d_closed_n=5,
                        copy_bt_open_fill_rate=0.95,
                        **quality,
                    ),
                ],
            )
            db.commit()

            def fake_tune(_db, source, stamp):
                return {
                    "status": "ok",
                    "applied": True,
                    "applied_sizing": True,
                    "applied_add": False,
                    "followed_n": 2,
                    "selected_mult": 1.0,
                    "margins": {"STABLE_MARGIN_PCT": 0.04, "MID_MARGIN_PCT": 0.03, "HIGH_MARGIN_PCT": 0.03},
                    "lev_caps": {"STABLE_LEV_CAP": 25, "MID_LEV_CAP": 10, "HIGH_LEV_CAP": 4},
                    "deploy_full_pct": 0.40,
                    "params": {},
                    "add_params": {},
                    "candidates": [],
                    "add_candidates": [],
                }

            losing_windows = {
                30: {
                    "copy_net_pnl": -120.0,
                    "closed_n": 12,
                    "copy_win_rate": 0.25,
                    "opened_n": 12,
                    "target_open_events": 12,
                    "liquidations": 0,
                    "fee_drag": 12.0,
                },
                14: {
                    "copy_net_pnl": -80.0,
                    "closed_n": 8,
                    "copy_win_rate": 0.25,
                    "opened_n": 8,
                    "target_open_events": 8,
                    "liquidations": 0,
                    "fee_drag": 8.0,
                },
                7: {
                    "copy_net_pnl": -30.0,
                    "closed_n": 4,
                    "copy_win_rate": 0.0,
                    "opened_n": 4,
                    "target_open_events": 4,
                    "liquidations": 0,
                    "fee_drag": 4.0,
                },
            }

            def fake_copy_results(addr, _fills, _now, _p):
                if addr == "0xstale":
                    return losing_windows
                return {
                    30: {"copy_net_pnl": 900, "copy_win_rate": 0.7, "wins": 14, "closed_n": 20,
                         "opened_n": 20, "target_open_events": 20, "liquidations": 0, "fee_drag": 10},
                    14: {"copy_net_pnl": 500, "copy_win_rate": 0.7, "wins": 7, "closed_n": 10,
                         "opened_n": 10, "target_open_events": 10, "liquidations": 0, "fee_drag": 5},
                    7: {"copy_net_pnl": 200, "copy_win_rate": 0.8, "wins": 4, "closed_n": 5,
                        "opened_n": 5, "target_open_events": 5, "liquidations": 0, "fee_drag": 3},
                }

            def fake_sector_results(addr, _fills, _now, _p):
                if addr == "0xstale":
                    return {"crypto": losing_windows, "stock": {}}
                return {"crypto": fake_copy_results(addr, _fills, _now, _p), "stock": {}}

            with patch.object(scanner.auto_tune, "choose_follow_line_by_portfolio", return_value={
                "status": "ok",
                "reason": "portfolio_topn",
                "line": 0.60,
                "count": 2,
            }), patch.object(scanner.auto_tune, "maybe_tune_margins", side_effect=fake_tune), \
                    patch.object(scanner, "_copy_bt_cached_fills", return_value=[{"coin": "BTC", "time": 1}]) as cached, \
                    patch.object(scanner, "_copy_bt_results", side_effect=fake_copy_results) as replay, \
                    patch.object(scanner, "_sector_copy_bt_results", side_effect=fake_sector_results) as sector_replay:
                n = scanner.refresh_watchlist_and_auto_tune(db, "2026-07-06T00:00:00Z", source="scan")

            self.assertEqual(n, 2)
            cached.assert_not_called()
            replay.assert_not_called()
            sector_replay.assert_not_called()
            stale = db.execute(
                "SELECT status,reason,copy_bt_net_pnl,copy_bt_7d_net_pnl FROM profile WHERE addr='0xstale'"
            ).fetchone()
            self.assertEqual(stale[0], "active")
            self.assertEqual(stale[2], 1200.0)
            self.assertEqual(stale[3], 300.0)
            self.assertIsNotNone(db.execute("SELECT 1 FROM watchlist WHERE addr='0xstale'").fetchone())
            stages = [r[0] for r in db.execute(
                "SELECT DISTINCT stage FROM pipeline_audit WHERE stamp=?",
                ("2026-07-06T00:00:00Z",),
            ).fetchall()]
            self.assertNotIn("profile", stages)
            self.assertIn("auto_tune", stages)

    @unittest.skip("retired top-N score-line selection; tuner now consumes published Core")
    def test_post_scan_pipeline_replays_topn_prefix_before_auto_tune(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.95, copy_bt_net_pnl=1200, copy_bt_14d_net_pnl=800,
                                 copy_bt_7d_net_pnl=300, copy_bt_closed_n=20, copy_bt_14d_closed_n=10,
                                 copy_bt_7d_closed_n=5),
                    _profile_row("0xbbb", "active", 0.90, copy_bt_net_pnl=1000, copy_bt_14d_net_pnl=700,
                                 copy_bt_7d_net_pnl=250, copy_bt_closed_n=20, copy_bt_14d_closed_n=10,
                                 copy_bt_7d_closed_n=5),
                    _profile_row("0xccc", "active", 0.85, copy_bt_net_pnl=900, copy_bt_14d_net_pnl=650,
                                 copy_bt_7d_net_pnl=200, copy_bt_closed_n=20, copy_bt_14d_closed_n=10,
                                 copy_bt_7d_closed_n=5),
                    _profile_row("0xddd", "active", 0.80, copy_bt_net_pnl=800, copy_bt_14d_net_pnl=600,
                                 copy_bt_7d_net_pnl=150, copy_bt_closed_n=20, copy_bt_14d_closed_n=10,
                                 copy_bt_7d_closed_n=5),
                ],
            )
            db.commit()
            pnl_by_n = {
                2: {30: 3000, 14: 2400, 7: 900},
                3: {30: 1800, 14: 1200, 7: 300},
                4: {30: 1700, 14: 1100, 7: 280},
            }
            seen = {}

            def fake_candidate_windows(_db, addrs, _sigmas, _follow, _now_ms, window_fills=None):
                addrs = [(a or "").lower() for a in addrs]
                for fills in (window_fills or {}).values():
                    self.assertTrue(all((f.get("user") or "").lower() in addrs for f in fills))
                return {
                    days: {
                        "copy_net_pnl": pnl,
                        "closed_n": 10,
                        "capacity_open_fit": 0.98,
                        "open_fill_rate": 0.98,
                        "liquidations": 0,
                        "target_open_events": 10,
                        "skip_reasons": {},
                    }
                    for days, pnl in pnl_by_n[len(addrs)].items()
                }

            def fake_tune(db_arg, source, stamp):
                follow = params.load_follow(db_arg)
                seen["source"] = source
                seen["stamp"] = stamp
                seen["line"] = follow["MIN_FOLLOW_SCORE"]
                seen["addrs"] = scanner.auto_tune._load_followed_wallets(db_arg, follow)
                return {
                    "status": "ok",
                    "applied": False,
                    "applied_sizing": False,
                    "applied_add": False,
                    "followed_n": len(seen["addrs"]),
                    "selected_mult": 1.0,
                    "margins": {},
                    "lev_caps": {},
                    "deploy_full_pct": 0.40,
                    "params": {},
                    "add_params": {},
                    "candidates": [],
                    "add_candidates": [],
                }

            fake_fills = {
                30: [{"user": a} for a in ("0xaaa", "0xbbb", "0xccc", "0xddd")],
                14: [{"user": a} for a in ("0xaaa", "0xbbb", "0xccc", "0xddd")],
                7: [{"user": a} for a in ("0xaaa", "0xbbb", "0xccc", "0xddd")],
            }
            with patch.object(scanner.config, "AUTO_FOLLOW_MIN_N", 2), \
                    patch.object(scanner.config, "AUTO_FOLLOW_TARGET_N", 4), \
                    patch.object(scanner.config, "AUTO_FOLLOW_MAX_N", 4), \
                    patch.object(scanner.config, "AUTO_FOLLOW_MIN_SCORE", 0.50), \
                    patch.object(scanner.config, "AUTO_FOLLOW_PORTFOLIO_MIN_ABS_GAIN", 250.0), \
                    patch.object(scanner.config, "AUTO_FOLLOW_PORTFOLIO_MIN_REL_GAIN", 0.05), \
                    patch.object(scanner.auto_tune, "_portfolio_window_fills", return_value=fake_fills), \
                    patch.object(scanner.auto_tune, "_load_sigmas", return_value={}), \
                    patch.object(scanner.auto_tune, "_candidate_windows", side_effect=fake_candidate_windows), \
                    patch.object(scanner.auto_tune, "maybe_tune_margins", side_effect=fake_tune):
                n = scanner.refresh_watchlist_and_auto_tune(db, "2026-07-06T00:00:00Z", source="scan")

            self.assertEqual(n, 4)
            self.assertEqual(seen["addrs"], ["0xaaa", "0xbbb"])
            self.assertEqual(seen["source"], "scan")
            follow = db.execute(
                "SELECT reason,payload_json FROM pipeline_audit WHERE stamp=? AND stage='follow_line'",
                ("2026-07-06T00:00:00Z",),
            ).fetchone()
            payload = json.loads(follow[1])
            self.assertEqual(follow[0], "portfolio_topn")
            self.assertEqual(payload["selected"]["n"], 2)
            self.assertEqual(payload["reference"]["n"], 4)
            self.assertEqual(payload["count"], 2)
            followed = db.execute(
                "SELECT addr FROM watchlist WHERE score>=? ORDER BY rank",
                (seen["line"],),
            ).fetchall()
            self.assertEqual([r[0] for r in followed], ["0xaaa", "0xbbb"])

    def test_post_scan_pipeline_audits_auto_tune_exception(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.90),
                    _profile_row("0xbbb", "active", 0.80),
                ],
            )
            db.commit()

            with patch.object(scanner.auto_tune, "choose_follow_line_by_portfolio", return_value={
                "status": "ok",
                "reason": "portfolio_topn",
                "line": 0.799999999,
                "count": 2,
            }), patch.object(scanner.auto_tune, "maybe_tune_margins", side_effect=RuntimeError("grid blew up")):
                n = scanner.refresh_watchlist_and_auto_tune(db, "2026-07-06T00:00:00Z", source="scan")

            self.assertEqual(n, 2)
            row = db.execute(
                "SELECT status,reason,payload_json FROM pipeline_audit "
                "WHERE stamp=? AND stage='auto_tune'",
                ("2026-07-06T00:00:00Z",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "error")
            self.assertEqual(row[1], "auto_tune_exception")
            payload = json.loads(row[2])
            self.assertIn("grid blew up", payload["error"])

    @unittest.skip("legacy score-line setup; generation-bound Core tuner is covered by auto-tune tests")
    def test_post_scan_pipeline_real_auto_tune_stays_unapplied_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            now_ms = int(time.time() * 1000)
            quality = dict(
                n_trades=12,
                n_fills=24,
                active_days=8,
                activity_ratio=0.8,
                median_eps=1,
                median_hold_s=3600,
                win_rate=0.7,
                net_pnl=500,
                roi_equity=0.10,
                roi_total=0.10,
                net_30d=500,
                net_life=900,
                pf_equity=10_000,
                pf_mon_pnl=700,
                pf_mon_vlm=20_000,
                pf_week_pnl=250,
                pf_week_vlm=8_000,
                pf_turnover=1,
                payoff_ratio=1.5,
                avg_notional=1_000,
                last_fill_ms=now_ms,
            )
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [
                    _profile_row("0xaaa", "active", 0.95, copy_bt_net_pnl=1200, copy_bt_14d_net_pnl=900,
                                 copy_bt_7d_net_pnl=350, copy_bt_closed_n=20, copy_bt_14d_closed_n=10,
                                 copy_bt_7d_closed_n=5, copy_bt_open_fill_rate=1.0, **quality),
                    _profile_row("0xbbb", "active", 0.90, copy_bt_net_pnl=1000, copy_bt_14d_net_pnl=700,
                                 copy_bt_7d_net_pnl=250, copy_bt_closed_n=20, copy_bt_14d_closed_n=10,
                                 copy_bt_7d_closed_n=5, copy_bt_open_fill_rate=1.0, **quality),
                    _profile_row("0xccc", "active", 0.70, copy_bt_net_pnl=400, copy_bt_14d_net_pnl=200,
                                 copy_bt_7d_net_pnl=80, copy_bt_closed_n=20, copy_bt_14d_closed_n=10,
                                 copy_bt_7d_closed_n=5, copy_bt_open_fill_rate=1.0, **quality),
                ],
            )
            db.commit()
            before_follow = params.load_follow(db)
            seen = {"tune_addrs": [], "add_addrs": []}

            def ok_window(pnl):
                return {
                    "copy_net_pnl": pnl,
                    "closed_n": 12,
                    "capacity_open_fit": 0.99,
                    "open_fill_rate": 0.99,
                    "liquidations": 0,
                    "target_open_events": 12,
                    "skip_reasons": {},
                }

            def tune_axes(base):
                return [
                    scanner.auto_tune.build_tune_candidate(
                        base, 1.0, tuple(base[k] for k in scanner.auto_tune.LEV_KEYS), base["DEPLOY_FULL_PCT"]
                    ),
                    scanner.auto_tune.build_tune_candidate(base, 1.2, (30, 12, 5), 0.50),
                ]

            def add_axes(base):
                return [
                    scanner.auto_tune.build_add_candidate(base, base["ADD_GAP_K"], base["ADD_GAP_SHRINK_G"],
                                                          int(base["ADD_MAX_HARD"])),
                    scanner.auto_tune.build_add_candidate(base, 0.06, 1.3, 6),
                ]

            def eval_tune(_db, addrs, _follow, candidate, **_kw):
                seen["tune_addrs"].append(list(addrs))
                out = dict(candidate)
                out["params"] = candidate["params"]
                out["margins"] = {k: candidate["params"][k] for k in scanner.auto_tune.MARGIN_KEYS}
                out["lev_caps"] = {k: candidate["params"][k] for k in scanner.auto_tune.LEV_KEYS}
                out["deploy_full_pct"] = candidate["params"]["DEPLOY_FULL_PCT"]
                pnl = 500 if candidate.get("mult") == 1.2 else 100
                out["windows"] = {days: ok_window(pnl) for days in (30, 14, 7)}
                return out

            def eval_add(_db, addrs, _follow, candidate, **_kw):
                seen["add_addrs"].append(list(addrs))
                out = dict(candidate)
                out["params"] = candidate["params"]
                out["add_params"] = candidate["params"]
                pnl = 600 if candidate.get("gap_k") == 0.06 and candidate.get("max_hard") == 6 else 120
                out["windows"] = {days: ok_window(pnl) for days in (30, 14, 7)}
                return out

            with patch.object(scanner.auto_tune, "choose_follow_line_by_portfolio", return_value={
                "status": "ok",
                "reason": "portfolio_topn",
                "line": 0.76,
                "count": 2,
            }), patch.object(scanner.auto_tune, "tune_candidates_from_axes", side_effect=tune_axes), \
                    patch.object(scanner.auto_tune, "evaluate_tune_candidate", side_effect=eval_tune), \
                    patch.object(scanner.auto_tune, "add_candidates_from_axes", side_effect=add_axes), \
                    patch.object(scanner.auto_tune, "evaluate_add_candidate", side_effect=eval_add), \
                    patch.object(scanner.auto_tune, "_load_sigmas", return_value={}), \
                    patch.object(scanner.auto_tune, "_portfolio_window_fills",
                                 return_value={30: [{}], 14: [{}], 7: [{}]}):
                n = scanner.refresh_watchlist_and_auto_tune(db, "2026-07-06T00:00:00Z", source="scan")

            self.assertEqual(n, 3)
            self.assertTrue(seen["tune_addrs"])
            self.assertTrue(seen["add_addrs"])
            self.assertTrue(all(addrs == ["0xaaa", "0xbbb"] for addrs in seen["tune_addrs"]))
            self.assertTrue(all(addrs == ["0xaaa", "0xbbb"] for addrs in seen["add_addrs"]))

            follow = params.load_follow(db)
            self.assertAlmostEqual(follow["MIN_FOLLOW_SCORE"], 0.76)
            for key in scanner.auto_tune.TUNE_KEYS + scanner.auto_tune.ADD_TUNE_KEYS:
                self.assertEqual(follow[key], before_follow[key])

            commands = db.execute(
                "SELECT owner,type,payload_json FROM commands WHERE type='reload_params' ORDER BY id"
            ).fetchall()
            self.assertEqual([r[0] for r in commands], ["scanner"])
            self.assertEqual(json.loads(commands[0][2])["by"], "auto_follow_line")

            run = db.execute(
                "SELECT applied,followed_n,result_json FROM auto_tune_runs WHERE source='scan'"
            ).fetchone()
            self.assertEqual(run[0], 0)
            self.assertEqual(run[1], 2)
            result = json.loads(run[2])
            self.assertEqual(result["mode"], "apply")
            self.assertTrue(result["shadow"])
            self.assertFalse(result["applied_sizing"])
            self.assertFalse(result["applied_add"])
            self.assertFalse(result["eligible_to_apply"])
            self.assertNotEqual(result["proposal"]["ADD_GAP_K"], before_follow["ADD_GAP_K"])

            audit = db.execute(
                "SELECT status,reason,payload_json FROM pipeline_audit "
                "WHERE stamp=? AND stage='auto_tune'",
                ("2026-07-06T00:00:00Z",),
            ).fetchone()
            self.assertEqual(audit[0], "ok")
            self.assertNotEqual(audit[1], "applied")
            payload = json.loads(audit[2])
            self.assertEqual(payload["followedN"], 2)
            self.assertFalse(payload["appliedSizing"])
            self.assertFalse(payload["appliedAdd"])


if __name__ == "__main__":
    unittest.main()
