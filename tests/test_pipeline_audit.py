import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hl import api_discovery, params, pipeline_audit, scanner, storage


def _profile_row(addr, status, score, **overrides):
    cols = storage.PROFILE_COLS.split(",")
    row = {c: None for c in cols}
    row.update(
        addr=addr,
        status=status,
        reason="ok" if status == "active" else "copy_bt_loss",
        score=score,
        n_fills=20,
        n_trades=10,
        window_days=14,
        trades_per_day=0.7,
        taker_frac_notl=0.5,
        median_hold_s=3600,
        win_rate=0.7,
        net_pnl=1000,
        roi_equity=0.1,
        total_notl=10_000,
        acct_value=10_000,
        perp_frac=1,
        max_drawdown=0,
        age_days=14,
        top_coin="BTC",
        market_type="crypto",
        times_active=1,
        first_added="2026-07-05T00:00:00Z",
        last_refreshed="2026-07-05T00:00:00Z",
        last_fill_ms=1,
        copy_bt_net_pnl=900,
        copy_bt_14d_net_pnl=500,
        copy_bt_7d_net_pnl=200,
        copy_bt_closed_n=12,
        copy_bt_14d_closed_n=8,
        copy_bt_7d_closed_n=4,
        copy_bt_open_fill_rate=0.9,
        copy_bt_liquidations=0,
        copy_bt_fee_drag=30,
    )
    row.update(overrides)
    return [row.get(c) for c in cols]


class PipelineAuditTests(unittest.TestCase):
    def _db(self):
        td = tempfile.TemporaryDirectory()
        db = storage.connect(str(Path(td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        self.addCleanup(td.cleanup)
        return db

    def _insert_profiles(self, db):
        cols = storage.PROFILE_COLS.split(",")
        db.executemany(
            f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
            [
                _profile_row("0xaaa", "active", 0.82, reason="ok"),
                _profile_row("0xbbb", "rejected", 0.0, reason="copy_bt_loss", copy_bt_14d_net_pnl=-120),
            ],
        )
        db.commit()

    def _dict_rows(self, cur):
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def test_profile_snapshot_records_status_reason_and_copy_backtest(self):
        db = self._db()
        self._insert_profiles(db)

        pipeline_audit.record_profile_snapshot(db, "2026-07-07T00:00:00Z", "scan", ["0xaaa", "0xbbb"])

        rows = self._dict_rows(db.execute(
            "SELECT stage,addr,status,reason,raw_score,payload_json FROM pipeline_audit "
            "WHERE stamp=? AND source=? ORDER BY addr",
            ("2026-07-07T00:00:00Z", "scan"),
        ))
        self.assertEqual(len(rows), 2)
        active = rows[0]
        rejected = rows[1]
        self.assertEqual(active["stage"], "profile")
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["reason"], "ok")
        self.assertAlmostEqual(active["raw_score"], 0.82)
        active_payload = json.loads(active["payload_json"])
        self.assertEqual(active_payload["copyBt"]["14dNetPnl"], 500)
        self.assertEqual(active_payload["copyBt"]["7dClosedN"], 4)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reason"], "copy_bt_loss")
        self.assertEqual(json.loads(rejected["payload_json"])["copyBt"]["14dNetPnl"], -120)

    def test_refresh_watchlist_records_follow_line_and_watchlist_snapshots(self):
        db = self._db()
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
            "line": 0.735,
            "count": 2,
            "selected": {"n": 2, "score": 1200},
        }):
            scanner.refresh_watchlist(db, "2026-07-07T00:00:00Z", source="scan")

        stages = self._dict_rows(db.execute(
            "SELECT stage,status,reason,addr,rank,payload_json FROM pipeline_audit "
            "WHERE stamp='2026-07-07T00:00:00Z' ORDER BY stage,rank,addr"
        ))
        follow = [r for r in stages if r["stage"] == "follow_line"]
        watched = [r for r in stages if r["stage"] == "watchlist"]
        self.assertEqual(len(follow), 1)
        self.assertEqual(follow[0]["status"], "ok")
        self.assertEqual(follow[0]["reason"], "portfolio_topn")
        self.assertEqual(json.loads(follow[0]["payload_json"])["count"], 2)
        self.assertEqual([r["status"] for r in watched], ["followed", "followed", "below_line"])
        self.assertEqual([r["rank"] for r in watched], [1, 2, 3])

    def test_pipeline_audit_endpoint_returns_recent_events_with_payload(self):
        db = self._db()
        self._insert_profiles(db)
        pipeline_audit.record_profile_snapshot(db, "2026-07-07T00:00:00Z", "scan", ["0xaaa"])
        db.commit()

        res = api_discovery.ep_pipeline_audit(db, {"limit": ["5"]})

        self.assertEqual(res["total"], 1)
        event = res["events"][0]
        self.assertEqual(event["stage"], "profile")
        self.assertEqual(event["addr"], "0xaaa")
        self.assertEqual(event["status"], "active")
        self.assertEqual(event["payload"]["copyBt"]["14dNetPnl"], 500)

    def test_pipeline_summary_endpoint_compacts_latest_scan_decisions(self):
        db = self._db()
        self._insert_profiles(db)
        pipeline_audit.record_profile_snapshot(db, "2026-07-07T00:00:00Z", "scan", ["0xaaa", "0xbbb"])
        pipeline_audit.record_follow_line_choice(db, "2026-07-07T00:00:00Z", "scan", {
            "status": "ok",
            "reason": "portfolio_topn",
            "line": 0.735,
            "count": 2,
            "target_n": 16,
        })
        pipeline_audit.record_auto_tune_result(db, "2026-07-07T00:00:00Z", "scan", {
            "status": "ok",
            "applied": True,
            "applied_sizing": True,
            "applied_add": False,
            "followed_n": 2,
            "selected_mult": 1.2,
            "params": {"MID_MARGIN_PCT": 0.04},
            "add_params": {"ADD_GAP_K": 0.08},
            "candidates": [{"score": 1}],
            "add_candidates": [],
        })
        db.commit()

        res = api_discovery.ep_pipeline_summary(db, {})

        self.assertEqual(res["stamp"], "2026-07-07T00:00:00Z")
        self.assertEqual(res["source"], "scan")
        self.assertEqual(res["profile"]["total"], 2)
        self.assertEqual(res["profile"]["active"], 1)
        self.assertEqual(res["profile"]["rejected"], 1)
        self.assertEqual(res["profile"]["reasonCounts"][0]["reason"], "copy_bt_loss")
        self.assertEqual(res["followLine"]["reason"], "portfolio_topn")
        self.assertEqual(res["followLine"]["score"], 73.5)
        self.assertEqual(res["followLine"]["count"], 2)
        self.assertTrue(res["autoTune"]["applied"])
        self.assertTrue(res["autoTune"]["appliedSizing"])
        self.assertFalse(res["autoTune"]["appliedAdd"])

    def test_pipeline_summary_prefers_latest_complete_decision_over_profile_only_backfill(self):
        db = self._db()
        self._insert_profiles(db)
        pipeline_audit.record_profile_snapshot(db, "2026-07-07T00:00:00Z", "scan", ["0xaaa"])
        pipeline_audit.record_follow_line_choice(db, "2026-07-07T00:00:00Z", "scan", {
            "status": "ok",
            "reason": "portfolio_topn",
            "line": 0.735,
        })
        pipeline_audit.record_profile_snapshot(db, "2026-07-08T00:00:00Z", "manual_backfill", ["0xbbb"])
        db.commit()

        res = api_discovery.ep_pipeline_summary(db, {})

        self.assertEqual(res["stamp"], "2026-07-07T00:00:00Z")
        self.assertEqual(res["source"], "scan")
        self.assertEqual(res["followLine"]["reason"], "portfolio_topn")


if __name__ == "__main__":
    unittest.main()
