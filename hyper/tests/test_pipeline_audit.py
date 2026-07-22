import json
import tempfile
import unittest
from pathlib import Path
from dashboard.api import discovery as api_discovery
from hyper import params, storage
from hyper.discovery import pipeline_audit, scanner


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
        copy_expected_return=0.04,
        copy_return_lcb=0.01,
        copy_return_volatility=0.08,
        copy_positive_probability=0.82,
        copy_evidence_days=10,
        copy_recent_return_14d=0.03,
        copy_recent_return_7d=0.02,
        copy_risk_score=0.8,
        execution_score=0.9,
        actionable_open_rate=0.9,
        capacity_fit=0.9,
        open_probability_48h=0.75,
        evidence_status="qualified",
        data_status="valid",
        copy_bt_net_pnl=900,
        copy_bt_14d_net_pnl=500,
        copy_bt_7d_net_pnl=200,
        copy_bt_closed_n=12,
        copy_bt_14d_closed_n=8,
        copy_bt_7d_closed_n=5,
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
        self.assertEqual(active_payload["copyBt"]["7dClosedN"], 5)
        self.assertIn("thresholds", active_payload["decisionAudit"])
        self.assertIn("actual", active_payload["decisionAudit"])
        self.assertIn("role", active_payload["followEligibility"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reason"], "copy_bt_loss")
        self.assertEqual(json.loads(rejected["payload_json"])["copyBt"]["14dNetPnl"], -120)

    def test_funnel_audit_separates_failure_categories_and_economic_roles(self):
        db = self._db()
        stamp = "2026-07-07T00:00:00Z"
        events = (
            ("official_roi", "rejected", "week_volume_below_floor", {"count": 7}),
            ("perp_prefilter", "rejected", "month_perp_not_profitable", {}),
            ("profile", "rejected", "grid_dca", {
                "followEligibility": {"role": "rejected"},
                "decisionAudit": {"stage": "structure_filter", "failureCategory": "business_reject"},
            }),
            ("profile", "rejected", "research_copy_positive", {
                "followEligibility": {"role": "research", "status": "research_copy_positive"},
                "decisionAudit": {"stage": "copy_qualification", "failureCategory": "business_reject"},
            }),
            ("profile", "active", "ok", {
                "followEligibility": {"role": "challenger", "status": "challenger_return_watch"},
                "decisionAudit": {"stage": "copy_qualification", "failureCategory": "soft_retention_failure"},
            }),
            ("profile", "active", "ok", {
                "followEligibility": {"role": "core_eligible", "status": "core_eligible"},
                "decisionAudit": {"stage": "personal_core", "failureCategory": "passed"},
            }),
            ("selection", "challenger", "portfolio_not_selected", {}),
        )
        for stage, status, reason, payload in events:
            pipeline_audit._insert_event(
                db, stamp=stamp, source="scan", stage=stage,
                status=status, reason=reason, payload=payload,
            )
        db.commit()

        roles, reasons, categories, structure_passed = api_discovery._latest_funnel_audit(db, stamp)

        self.assertEqual(roles["research"], 1)
        self.assertEqual(roles["challenger"], 1)
        self.assertEqual(roles["core_eligible"], 1)
        self.assertEqual(structure_passed, 3)
        self.assertEqual(reasons["structure"][0]["reason"], "grid_dca")
        self.assertEqual(reasons["challenger"][0]["reason"], "research_copy_positive")
        self.assertEqual(reasons["personalCore"][0]["category"], "soft_retention_failure")
        self.assertIn("business_reject", {row["category"] for row in categories})

    def test_refresh_watchlist_does_not_publish_legacy_score_line_membership(self):
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

        scanner.refresh_watchlist(db, "2026-07-07T00:00:00Z")

        stages = self._dict_rows(db.execute(
            "SELECT stage,status,reason,addr,rank,payload_json FROM pipeline_audit "
            "WHERE stamp='2026-07-07T00:00:00Z' ORDER BY stage,rank,addr"
        ))
        follow = [r for r in stages if r["stage"] == "follow_line"]
        watched = [r for r in stages if r["stage"] == "watchlist"]
        self.assertEqual(follow, [])
        self.assertEqual(watched, [])

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

    def test_pipeline_audit_endpoint_can_return_compact_payload(self):
        db = self._db()
        self._insert_profiles(db)
        db.execute(
            "UPDATE profile SET sector_policy_json=?, sector_copy_json=? WHERE addr='0xaaa'",
            (
                json.dumps({
                    "allowed": ["crypto"],
                    "crypto": {"allow": True, "status": "allowed"},
                    "stock": {"allow": False, "status": "recent_loss"},
                }),
                json.dumps({
                    "crypto": {"14": {"copy_net_pnl": 500, "closed_n": 6}},
                    "stock": {"14": {"copy_net_pnl": -300, "closed_n": 6}},
                }),
            ),
        )
        pipeline_audit.record_profile_snapshot(db, "2026-07-07T00:00:00Z", "scan", ["0xaaa"])
        db.commit()

        res = api_discovery.ep_pipeline_audit(db, {"limit": ["5"], "compact": ["1"]})

        payload = res["events"][0]["payload"]
        self.assertEqual(
            sorted(payload.keys()),
            ["copyBt", "decisionAudit", "followEligibility", "sectorCopy", "sectorPolicy"],
        )
        self.assertEqual(payload["copyBt"]["14dNetPnl"], 500)
        self.assertEqual(payload["sectorPolicy"]["allowed"], ["crypto"])
        self.assertFalse(payload["sectorPolicy"]["stock"]["allow"])
        self.assertNotIn("marketType", payload)
        self.assertNotIn("openState", payload)

    def test_pipeline_audit_compact_payload_strips_nested_detail(self):
        db = self._db()
        heavy = list(range(50))
        full_payload = {
            "marketType": "crypto",
            "copyBt": {
                "30dNetPnl": 900,
                "30dClosedN": 12,
                "14dNetPnl": 500,
                "14dClosedN": 8,
                "7dNetPnl": 200,
                "7dClosedN": 5,
                "openFillRate": 0.9,
                "liquidations": 0,
                "perEpisode": heavy,
            },
            "followEligibility": {
                "eligible": False,
                "status": "low_fill_rate",
                "reasons": ["开仓跟随率低"],
                "debug": {"episodes": heavy},
            },
            "sectorCopy": {
                "crypto": {
                    "14": {"copy_net_pnl": 500, "closed_n": 6, "episodes": heavy},
                },
                "stock": {
                    "14": {"copy_net_pnl": -300, "closed_n": 6, "episodes": heavy},
                },
            },
            "sectorPolicy": {
                "allowed": ["crypto"],
                "crypto": {"allow": True, "status": "allowed", "pnl": {"14": 500}, "closed": {"14": 6},
                           "samples": heavy},
                "stock": {"allow": False, "status": "recent_loss", "pnl": {"14": -300}, "closed": {"14": 6},
                          "samples": heavy},
            },
            "openState": {"bags": heavy},
        }
        db.execute(
            "INSERT INTO pipeline_audit "
            "(stamp,source,stage,addr,status,reason,payload_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "2026-07-07T00:00:00Z",
                "scan",
                "watchlist",
                "0xaaa",
                "below_line",
                "low_fill_rate",
                json.dumps(full_payload),
                "2026-07-07T00:00:01Z",
            ),
        )
        db.commit()

        compact = api_discovery.ep_pipeline_audit(db, {"limit": ["1"], "compact": ["1"]})["events"][0]["payload"]
        full = api_discovery.ep_pipeline_audit(db, {"limit": ["1"]})["events"][0]["payload"]

        self.assertIn("perEpisode", full["copyBt"])
        self.assertNotIn("perEpisode", compact["copyBt"])
        self.assertNotIn("debug", compact["followEligibility"])
        self.assertNotIn("episodes", compact["sectorCopy"]["crypto"]["14"])
        self.assertNotIn("samples", compact["sectorPolicy"]["stock"])
        self.assertNotIn("openState", compact)
        self.assertEqual(compact["sectorPolicy"]["stock"]["status"], "recent_loss")
        self.assertEqual(compact["sectorCopy"]["crypto"]["14"]["copy_net_pnl"], 500)

    def test_pipeline_summary_endpoint_compacts_latest_scan_decisions(self):
        db = self._db()
        self._insert_profiles(db)
        pipeline_audit.record_workset_summary(db, "2026-07-07T00:00:00Z", "scan", {
            "mode": "INCREMENTAL daily-tier",
            "counts": {
                "candidate": 12,
                "profiled_before": 9,
                "active_total": 3,
                "active_candidate": 2,
                "new_candidate": 4,
                "top_recheck": 5,
                "off_list_active": 1,
                "workset": 12,
                "deferred_tail": 0,
            },
            "full_scan": False,
            "limit": 100,
        })
        pipeline_audit.record_profile_snapshot(db, "2026-07-07T00:00:00Z", "scan", ["0xaaa", "0xbbb"])
        pipeline_audit._insert_event(
            db, stamp="2026-07-07T00:00:00Z", source="scan", stage="selection_summary",
            status="ok", reason="published_core",
            payload={"generation": "g1", "action": "add", "core": 2, "challenger": 1},
        )
        pipeline_audit._insert_event(
            db, stamp="2026-07-07T00:00:00Z", source="scan", stage="tuner_finalize",
            status="complete", reason="synchronous_quality_prefix_formation",
            payload={
                "portfolioReplay": {"status": "ok", "netPnl": 1200},
                "selectionReplay": {"status": "ok", "refreshed": 3},
            },
        )
        pipeline_audit.record_prune_summary(db, "2026-07-07T00:00:00Z", "scan", {
            "stale_profiles": 2,
            "profiles": 2,
            "fills": 7,
            "episodes": 3,
            "leaderboard": 5,
        })
        db.commit()

        res = api_discovery.ep_pipeline_summary(db, {})

        self.assertEqual(res["stamp"], "2026-07-07T00:00:00Z")
        self.assertEqual(res["source"], "scan")
        self.assertEqual(res["profile"]["total"], 2)
        self.assertEqual(res["profile"]["active"], 1)
        self.assertEqual(res["profile"]["rejected"], 1)
        self.assertEqual(res["profile"]["reasonCounts"][0]["reason"], "copy_bt_loss")
        self.assertNotIn("followLine", res)
        self.assertEqual(res["selection"]["core"], 2)
        self.assertEqual(res["selection"]["challenger"], 1)
        self.assertEqual(res["autoTune"]["status"], "complete")
        self.assertEqual(res["autoTune"]["portfolioReplay"]["netPnl"], 1200)
        self.assertEqual(res["autoTune"]["selectionReplay"]["refreshed"], 3)
        self.assertEqual(res["workset"]["profiled"], 12)
        self.assertEqual(res["workset"]["new"], 4)
        self.assertEqual(res["workset"]["topRecheck"], 5)
        self.assertEqual(res["workset"]["offListActive"], 1)
        self.assertEqual(res["prune"]["profiles"], 2)
        self.assertEqual(res["prune"]["fills"], 7)

    def test_pipeline_summary_prefers_latest_complete_decision_over_profile_only_backfill(self):
        db = self._db()
        self._insert_profiles(db)
        pipeline_audit.record_profile_snapshot(db, "2026-07-07T00:00:00Z", "scan", ["0xaaa"])
        pipeline_audit._insert_event(
            db, stamp="2026-07-07T00:00:00Z", source="scan", stage="selection_summary",
            status="ok", reason="published_core",
            payload={"generation": "g1", "action": "keep", "core": 1, "challenger": 0},
        )
        pipeline_audit.record_profile_snapshot(db, "2026-07-08T00:00:00Z", "manual_backfill", ["0xbbb"])
        db.commit()

        res = api_discovery.ep_pipeline_summary(db, {})

        self.assertEqual(res["stamp"], "2026-07-07T00:00:00Z")
        self.assertEqual(res["source"], "scan")
        self.assertEqual(res["selection"]["generation"], "g1")

    def test_pipeline_summary_does_not_group_all_audit_rows_to_find_latest_decision(self):
        class GuardedDb:
            def __init__(self, inner):
                self.inner = inner

            def execute(self, sql, args=()):
                normalized = " ".join(sql.split())
                if "GROUP BY stamp,source" in normalized:
                    raise AssertionError("latest pipeline decision should use indexed latest-row lookup")
                return self.inner.execute(sql, args)

        db = self._db()
        self._insert_profiles(db)
        pipeline_audit.record_profile_snapshot(db, "2026-07-07T00:00:00Z", "scan", ["0xaaa"])
        pipeline_audit._insert_event(
            db, stamp="2026-07-07T00:00:00Z", source="scan", stage="selection_summary",
            status="ok", reason="published_core",
            payload={"generation": "g1", "action": "keep", "core": 1, "challenger": 0},
        )
        db.commit()

        res = api_discovery.ep_pipeline_summary(GuardedDb(db), {})

        self.assertEqual(res["stamp"], "2026-07-07T00:00:00Z")
        self.assertEqual(res["source"], "scan")


if __name__ == "__main__":
    unittest.main()
