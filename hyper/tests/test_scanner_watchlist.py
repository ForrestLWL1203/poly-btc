import tempfile
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hyper import params, storage
from hyper.discovery import scanner, scanner_copy_bt, scanner_lifecycle


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
    def test_account_wide_spot_and_turnover_never_enter_sector_recovery(self):
        self.assertNotIn("spot_dominant", scanner._SECTOR_RECOVERABLE_STRUCTURE_REASONS)
        self.assertNotIn("hft_turnover", scanner._SECTOR_RECOVERABLE_STATE_REASONS)
        self.assertNotIn("spot_hedge", scanner._SECTOR_RECOVERABLE_STRUCTURE_REASONS)

    def test_regate_reactivates_complete_cached_snapshot_but_not_incomplete_profile(self):
        self.assertEqual(
            scanner._regate_profile_status(
                "rejected", "normalized_evidence_missing", True,
                complete_cached_snapshot=True,
            ),
            "active",
        )
        self.assertEqual(
            scanner._regate_profile_status(
                "rejected", "grid_dca", True,
                complete_cached_snapshot=False,
            ),
            "rejected",
        )
        self.assertEqual(
            scanner._regate_profile_status(
                "rejected", "no_portfolio", True,
                complete_cached_snapshot=False,
            ),
            "rejected",
        )

    def test_partial_cache_without_coverage_marker_forces_full_window_heal(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            cached = {"tid": 1, "time": 9_000, "coin": "BTC"}
            db.execute(
                "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
                ("0xaaa", 1, 9_000, json.dumps(cached)),
            )
            db.commit()
            fetched = [{"tid": 2, "time": 2_000, "coin": "BTC"}]

            with patch.object(scanner.rest, "fetch_window_progress", return_value=(fetched, False, 2_001)) as fetch:
                raw, hit_cap, new_fills, fetched_full = scanner._fetch_profile_fills(
                    db, "0xaaa", 1_000, SimpleNamespace(max_pages=5), full=False,
                )

            fetch.assert_called_once_with("0xaaa", 1_000, 5)
            self.assertEqual([row["tid"] for row in raw], [2, 1])
            self.assertEqual(raw[0]["user"], "0xaaa")
            self.assertFalse(hit_cap)
            self.assertEqual([row["tid"] for row in new_fills], [2])
            self.assertTrue(fetched_full)
            db.close()

    def test_complete_cache_uses_delta_and_preserves_full_specialization_window(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            cached = {"tid": 1, "time": 9_000, "coin": "BTC"}
            db.execute(
                "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
                ("0xaaa", 1, 9_000, json.dumps(cached)),
            )
            db.execute(
                "INSERT INTO fill_cache_state(addr,coverage_start_ms,coverage_end_ms,updated_at) "
                "VALUES (?,?,?,?)",
                ("0xaaa", 1_000, 9_000, "2026-07-16T00:00:00Z"),
            )
            db.commit()
            delta = [{"tid": 2, "time": 10_000, "coin": "xyz:IBM"}]

            with patch.object(scanner.rest, "fetch_window", return_value=(delta, False)) as fetch:
                raw, hit_cap, new_fills, fetched_full = scanner._fetch_profile_fills(
                    db, "0xaaa", 1_000, SimpleNamespace(max_pages=5), full=False,
                )

            self.assertEqual(fetch.call_args.args[0], "0xaaa")
            self.assertGreaterEqual(fetch.call_args.args[1], 1_000)
            self.assertEqual([row["tid"] for row in raw], [1, 2])
            self.assertFalse(hit_cap)
            self.assertEqual([row["tid"] for row in new_fills], [2])
            self.assertEqual(new_fills[0]["user"], "0xaaa")
            self.assertFalse(fetched_full)
            db.close()

    def test_history_response_is_filtered_before_cache_and_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            legacy = {"tid": 99, "time": 1_500, "coin": "#4830"}
            db.execute(
                "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
                ("0xaaa", 99, 1_500, json.dumps(legacy)),
            )
            db.commit()
            fetched = [
                {"tid": 1, "time": 2_000, "coin": "BTC"},
                {"tid": 2, "time": 2_100, "coin": "xyz:AAPL"},
                {"tid": 3, "time": 2_200, "coin": "#4830"},
                {"tid": 4, "time": 2_300, "coin": "BTC/USDC"},
                {"tid": 5, "time": 2_400, "coin": "vntl:OPENAI"},
                {"tid": 6, "time": 2_500, "coin": "DELISTED"},
            ]
            with patch.object(scanner.rest, "fetch_window_progress", return_value=(fetched, False, 2_501)):
                scoped, hit_cap, new_fills, fetched_full = scanner._fetch_profile_fills(
                    db, "0xaaa", 1_000, SimpleNamespace(max_pages=5), full=True,
                    universe={"BTC", "xyz:AAPL"},
                )

            self.assertEqual([row["coin"] for row in scoped], ["BTC", "xyz:AAPL"])
            self.assertEqual([row["coin"] for row in new_fills], ["BTC", "xyz:AAPL"])
            self.assertFalse(hit_cap)
            self.assertTrue(fetched_full)

            with scanner._db_lock:
                scanner._store_cached_fills(
                    db, "0xaaa", fetched, 1_000, coverage_complete=True,
                    coverage_end=3_000, universe={"BTC", "xyz:AAPL"},
                )
                db.commit()
            coins = [json.loads(row[0])["coin"] for row in db.execute(
                "SELECT fill_json FROM candidate_fills ORDER BY time"
            )]
            # The persistence boundary also heals legacy/outdated rows already on disk.
            self.assertEqual(coins, ["BTC", "xyz:AAPL"])
            self.assertEqual(
                scanner._assert_scoped_fill_cache(db, ["0xaaa"], {"BTC", "xyz:AAPL"})["invalid"],
                0,
            )
            db.close()

    def test_incomplete_history_resumes_from_saved_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            first = [{"tid": 1, "time": 2_000, "coin": "BTC"}]
            with patch.object(
                scanner.rest, "fetch_window_progress", return_value=(first, True, 2_001),
            ):
                _raw, hit_cap, _new, _full = scanner._fetch_profile_fills(
                    db, "0xaaa", 1_000, SimpleNamespace(max_pages=1), full=True, universe={"BTC"},
                )
            self.assertTrue(hit_cap)
            with scanner._db_lock:
                scanner._store_cached_fills(db, "0xaaa", first, 1_000, coverage_complete=False,
                                            coverage_end=2_000, universe={"BTC"})
                db.commit()
            with patch.object(
                scanner.rest, "fetch_window_progress", return_value=([], False, 2_001),
            ) as fetch:
                raw, hit_cap, _new, _full = scanner._fetch_profile_fills(
                    db, "0xaaa", 1_000, SimpleNamespace(max_pages=1), full=True, universe={"BTC"},
                )
            fetch.assert_called_once_with("0xaaa", 2_001, 1)
            self.assertEqual([row["tid"] for row in raw], [1])
            self.assertFalse(hit_cap)
            db.close()

    def test_generation_scope_audit_fails_closed_on_legacy_invalid_cache(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            row = {"tid": 9, "time": 9_000, "coin": "#4830"}
            db.execute(
                "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
                ("0xaaa", 9, 9_000, json.dumps(row)),
            )
            db.commit()

            with self.assertRaisesRegex(RuntimeError, "market_scope_cache_violation"):
                scanner._assert_scoped_fill_cache(db, ["0xaaa"], {"BTC", "xyz:AAPL"})
            db.close()

    def test_source_cursor_advances_when_every_new_fill_is_out_of_scope(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.execute(
                "INSERT INTO fill_cache_state(addr,coverage_start_ms,coverage_end_ms,updated_at) "
                "VALUES ('0xaaa',1000,10000000,'2026-07-16T00:00:00Z')"
            )
            db.commit()
            with patch.object(
                scanner.rest, "fetch_window",
                return_value=([{"tid": 1, "time": 10_000_100, "coin": "#99"}], False),
            ) as fetch:
                scoped, hit_cap, new_fills, fetched_full = scanner._fetch_profile_fills(
                    db, "0xaaa", 1_000, SimpleNamespace(max_pages=5), full=False,
                    universe={"BTC", "xyz:AAPL"},
                )

            self.assertGreater(fetch.call_args.args[1], 1_000)
            self.assertEqual(scoped, [])
            self.assertEqual(new_fills, [])
            self.assertFalse(hit_cap)
            self.assertFalse(fetched_full)
            db.close()

    def test_successful_delta_persistence_advances_complete_source_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.execute(
                "INSERT INTO fill_cache_state(addr,coverage_start_ms,coverage_end_ms,updated_at) "
                "VALUES ('0xaaa',1000,9000,'2026-07-16T00:00:00Z')"
            )
            with scanner._db_lock:
                scanner._store_cached_fills(
                    db, "0xaaa", [], 2_000,
                    coverage_complete=True, coverage_end=12_000, universe={"BTC"},
                )
                db.commit()

            coverage = db.execute(
                "SELECT coverage_start_ms,coverage_end_ms FROM fill_cache_state WHERE addr='0xaaa'"
            ).fetchone()
            self.assertEqual(tuple(coverage), (1_000, 12_000))
            db.close()

    def test_cache_completeness_uses_source_coverage_not_earliest_fill(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.execute(
                "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
                ("0xquiet", 1, 9_000, json.dumps({"tid": 1, "time": 9_000, "coin": "BTC"})),
            )
            db.execute(
                "INSERT INTO fill_cache_state(addr,coverage_start_ms,coverage_end_ms,updated_at) "
                "VALUES ('0xquiet',1000,10000,'2026-07-20T00:00:00Z')"
            )
            db.commit()

            incomplete = scanner._incomplete_fill_cache_addrs(
                db, ["0xquiet", "0xmissing"], desired_start_ms=2_000,
            )

            self.assertEqual(incomplete, ["0xmissing"])
            db.close()

    def test_current_generation_sector_structure_is_independent_of_prior_policy(self):
        p = SimpleNamespace(days=14, max_single_adds=30, grid_max_adds=10)
        fills = [{"coin": "BTC"}, {"coin": "xyz:IBM"}]
        crypto_ep = {"open_complete": True, "n_adds": 0}
        stock_ep = {"open_complete": True, "n_adds": 12}

        with patch.object(scanner, "build_episodes", side_effect=[([crypto_ep], []), ([stock_ep], [])]), \
                patch.object(scanner.metrics, "compute_metrics", side_effect=[
                    {"median_adds_per_ep": 0, "max_adds_per_ep": 0},
                    {"median_adds_per_ep": 12, "max_adds_per_ep": 12},
                ]), \
                patch.object(scanner.metrics, "gates_structural", side_effect=[
                    (True, "ok"), (False, "grid_dca"),
                ]):
            policy = scanner._current_sector_structure_policy(fills, 1_000, p)

        self.assertEqual(policy["source"], "current_generation")
        self.assertEqual(policy["allowed"], ["crypto"])
        self.assertTrue(policy["crypto"]["allow"])
        self.assertFalse(policy["stock"]["allow"])
        self.assertEqual(policy["stock"]["status"], "grid_dca")

    def test_regate_namespace_without_days_uses_fourteen_day_structure_window(self):
        p = SimpleNamespace(max_single_adds=30, grid_max_adds=3)
        fills = [{"coin": "BTC"}]

        with patch.object(scanner, "build_episodes", return_value=([], [])), \
                patch.object(scanner.metrics, "compute_metrics", return_value=None) as compute:
            scanner._current_sector_structure_policy(fills, 1_000, p)

        self.assertEqual(compute.call_args.args[3], 14)

    def test_single_complete_heavy_dca_sector_enters_pressure_watch(self):
        p = SimpleNamespace(days=14, max_single_adds=30, grid_max_adds=3)
        fills = [{"coin": "BTC"}]
        episodes = [
            {"open_complete": True, "n_adds": 1},
            {"open_complete": True, "n_adds": 31},
            {"open_complete": False, "n_adds": 80},
        ]

        with patch.object(scanner, "build_episodes", return_value=(episodes, [])), \
                patch.object(scanner.metrics, "compute_metrics", return_value={
                    "median_adds_per_ep": 1,
                    "max_adds_per_ep": 31,
                }), \
                patch.object(scanner.metrics, "gates_structural", return_value=(False, "heavy_dca")):
            policy = scanner._current_sector_structure_policy(fills, 1_000, p)

        self.assertEqual(policy["allowed"], ["crypto"])
        self.assertTrue(policy["crypto"]["watch"])
        self.assertFalse(policy["crypto"]["coreBlocked"])
        self.assertEqual(policy["crypto"]["heavyEpisodeCount"], 1)

    def test_structural_specialization_snapshot_is_persistable_before_copy_replay(self):
        snapshot = scanner._structural_specialization_snapshot({
            "source": "current_generation",
            "allowed": ["crypto"],
            "crypto": {"allow": True, "status": "structural_ok"},
            "stock": {"allow": False, "status": "grid_dca"},
        })

        self.assertEqual(snapshot["allowed"], ["crypto"])
        self.assertEqual(snapshot["specializationSource"], "current_generation")
        self.assertEqual(snapshot["specializationPhase"], "structural")
        self.assertTrue(snapshot["crypto"]["allow"])
        self.assertFalse(snapshot["stock"]["allow"])


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
                universe={"KAITO", "XPL"},
            )

        self.assertAlmostEqual(snap["worst_underwater"], -0.05, places=6)
        self.assertEqual(snap["bag_count"], 1)

    def test_open_snapshot_ignores_positions_outside_executable_universe(self):
        clearinghouse = {
            "marginSummary": {"accountValue": "10000", "totalNtlPos": "999999"},
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0.1", "entryPx": "60000",
                              "positionValue": "6100", "unrealizedPnl": "100",
                              "leverage": {"type": "isolated", "value": 5}}},
                {"position": {"coin": "#4830", "szi": "1000", "entryPx": "1",
                              "positionValue": "100", "unrealizedPnl": "-9000",
                              "leverage": {"type": "cross", "value": 20}}},
            ],
        }
        with patch.object(scanner.rest, "clearinghouse_state", return_value=clearinghouse):
            snap = scanner._open_snapshot(
                "0xwallet", {None}, [{"coin": "BTC", "open_ms": 1}],
                scanner._DAY_MS, 10_000, universe={"BTC", "xyz:AAPL"},
            )

        self.assertEqual(snap["open_position_count"], 1)
        self.assertEqual(snap["open_unrealized"], 100)
        self.assertAlmostEqual(snap["cur_leverage"], 0.61)

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
                week_roi_min=0.05,
                month_roi_min=0.10,
                all_roi_min=0.10,
                week_pnl_min=1_000,
                month_pnl_min=2_000,
                all_pnl_min=5_000,
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
            recent_ms = int(time.time() * 1000)
            expired_ms = recent_ms - (scanner.config.PROFILE_FETCH_DAYS + 1) * 86_400_000
            db.executemany(
                "INSERT INTO candidate_fills (addr,tid,time,fill_json) VALUES (?,?,?,?)",
                [
                    ("0xgone", 1, recent_ms, "{}"),
                    ("0xcand", 2, recent_ms, "{}"),
                    ("0xactive", 3, recent_ms, "{}"),
                    ("0xcand", 4, expired_ms, "{}"),
                ],
            )
            db.executemany(
                "INSERT INTO fill_cache_state(addr,coverage_start_ms,coverage_end_ms,updated_at) "
                "VALUES (?,?,?,?)",
                [
                    ("0xgone", expired_ms, recent_ms, "2026-07-20T00:00:00Z"),
                    ("0xcand", expired_ms, recent_ms, "2026-07-20T00:00:00Z"),
                    ("0xactive", expired_ms, recent_ms, "2026-07-20T00:00:00Z"),
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
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fill_cache_state WHERE addr='0xgone'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidate_fills WHERE addr='0xcand'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fill_cache_state WHERE addr='0xcand'").fetchone()[0], 1)
            self.assertGreaterEqual(counts["expired_fills"], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile WHERE addr='0xcand'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile WHERE addr='0xactive'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leaderboard WHERE addr='0xgone'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leaderboard WHERE addr='0xactive'").fetchone()[0], 1)

    def test_prune_keeps_current_role_and_open_position_owners(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            cols = storage.PROFILE_COLS.split(",")
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [_profile_row("0xrole", "rejected", 0), _profile_row("0xopen", "rejected", 0),
                 _profile_row("0xgone", "rejected", 0)],
            )
            db.execute(
                "INSERT INTO scan_generation(generation,source,status,started_at,complete,is_current) "
                "VALUES ('g','scan','published','now',1,1)"
            )
            db.execute(
                "INSERT INTO follow_selection(generation,addr,role,enabled,selected_at) "
                "VALUES ('g','0xrole','exit_only',0,'now')"
            )
            db.execute("INSERT INTO copy_position(addr,status) VALUES ('0xopen','open')")
            db.commit()

            scanner_lifecycle.prune_discovery_cache(db)

            remaining = {row[0] for row in db.execute("SELECT addr FROM profile")}
            self.assertEqual(remaining, {"0xrole", "0xopen"})

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
                     "opened_n": 5, "target_open_events": 5, "liquidations": 0, "fee_drag": 2.0,
                     "body_after_top3_n": 2, "body_after_top3_net_pnl": -4.0},
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
        self.assertEqual(m["copy_bt_14d_win_rate"], 0.25)
        self.assertEqual(m["copy_bt_14d_body_after_top3_n"], 2)
        self.assertEqual(m["copy_bt_14d_body_after_top3_net_pnl"], -4.0)

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
                    30: {"copy_net_pnl": 1200.0, "copy_win_rate": 0.73, "closed_n": 15, "wins": 11,
                         "campaign_closed_n": 12, "campaign_wins": 9, "evidence_days": 5,
                         "campaign_net_after_top2": 300.0, "cost_stress_net_pnl": 800.0,
                         "opened_n": 15, "target_open_events": 15, "liquidations": 0, "fee_drag": 7.0},
                    14: {"copy_net_pnl": 600.0, "copy_win_rate": 0.71, "closed_n": 7, "wins": 5,
                         "opened_n": 7, "target_open_events": 7, "liquidations": 0, "fee_drag": 4.0},
                    7: {"copy_net_pnl": 500.0, "copy_win_rate": 0.8, "closed_n": 5, "wins": 4,
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
            self.assertEqual(tuple(row), ("retired", "copy_not_profitable", -25.0, 9))

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
                    pf_equity=10_000, pf_mon_pnl=1_000, pf_mon_vlm=50_000,
                ),
            )
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                _profile_row("0xstale", "active", 0.8, profile_generation="g-old"),
            )
            db.commit()
            scanner.generation_market.Resolver(db, "g-current", 1, set(), {})
            scanner.generation_market.seal(db, "g-current")
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
                "SELECT status,reason,score,raw_quality_score,data_status "
                "FROM profile WHERE addr='0xaaa'"
            ).fetchone()
            self.assertEqual(row[0], "active")
            self.assertEqual(row[1], "ok")
            self.assertAlmostEqual(row[2], 0.581)
            self.assertAlmostEqual(row[3], 0.581)
            self.assertEqual(row[4], "valid")
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
            self.assertEqual(row[0], "active")
            self.assertEqual(row[1], "copy_backtest_deferred_data_error")
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
            self.assertEqual(tuple(row), ("active", "copy_backtest_deferred_data_error", -5.0, 1))

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


if __name__ == "__main__":
    unittest.main()
