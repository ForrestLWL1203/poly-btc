import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hl import scanner, selection, storage
from hl.observer import Observer, load_targets


class SelectionTests(unittest.TestCase):
    def _db(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = storage.connect(str(Path(td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        db.row_factory = sqlite3.Row
        return db

    def _published(self, db, generation="g1"):
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at,leaderboard_valid,profile_complete) "
            "VALUES (?,'published',1,1,1,'2026-01-01','2026-01-02',1,1)",
            (generation,),
        )
        db.commit()

    def test_observer_prefers_enabled_published_core(self):
        db = self._db()
        db.executemany(
            "INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (?,?,?,'now')",
            [(1, "0xlegacy", 0.99), (2, "0xdisabled", 0.01)],
        )
        self._published(db)
        db.executemany(
            "INSERT INTO follow_selection "
            "(generation,addr,role,enabled,utility,selected_at) VALUES ('g1',?,?,?,?, 'now')",
            [("0xcore", "core", 1, 2.0), ("0xdisabled", "core", 1, 3.0),
             ("0xchallenger", "challenger", 1, 9.0)],
        )
        db.execute("INSERT INTO target_controls (addr,enabled) VALUES ('0xdisabled',0)")
        db.execute("INSERT INTO episode (addr,coin,open_ms,seq) VALUES ('0xcore','BTC',1,0)")
        db.commit()

        addrs, seeds = load_targets(db, 10)

        self.assertEqual(addrs, ["0xcore"])
        self.assertEqual(seeds, {"0xcore": {"BTC"}})

    def test_published_empty_core_does_not_fall_back(self):
        db = self._db()
        db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0xlegacy',.99,'now')")
        self._published(db)

        addrs, _ = load_targets(db, 10)

        self.assertEqual(addrs, [])
        self.assertEqual(selection.published_core_addrs(db), [])

    def test_no_score_fallback_before_first_publication(self):
        db = self._db()
        db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0xlegacy',.8,'now')")
        db.commit()

        addrs, _ = load_targets(db, 10)

        self.assertEqual(addrs, [])
        self.assertIsNone(selection.latest_published_generation(db))

    def test_reload_targets_readds_held_wallet_exit_only_with_empty_core(self):
        db = self._db()
        self._published(db)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs.taker.open_ep[("0xheld", "BTC")] = {}

            obs._reload_targets(init=True)

            self.assertEqual(obs.addrs, ["0xheld"])
            self.assertEqual(obs.held_off, {"0xheld"})
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_publish_selection_is_atomic_and_supports_empty_core(self):
        db = self._db()
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,leaderboard_valid,profile_complete) "
            "VALUES ('g-ready','ready',0,1,0,'2026-01-01',1,1)"
        )
        db.commit()

        result = selection.publish_selection(
            db,
            "g-ready",
            [selection.SelectionRow("0xchallenger", "challenger", reason="collecting_evidence")],
            selected_at="2026-01-02T00:00:00Z",
        )

        self.assertEqual(result["selection_count"], 1)
        self.assertEqual(selection.latest_published_generation(db), "g-ready")
        self.assertEqual(selection.published_core_addrs(db), [])

    def test_current_selection_rows_preserve_manual_snapshot_fields(self):
        db = self._db()
        self._published(db)
        db.execute(
            "INSERT INTO follow_selection "
            "(generation,addr,role,enabled,reason,utility,data_status,evidence_status,model_version,policy_version,selected_at) "
            "VALUES ('g1','0xmanual','core',0,'operator_pick',12.5,'valid','qualified','m1','p1','now')"
        )
        db.commit()

        rows = selection.current_selection_rows(db)

        self.assertEqual(rows, [selection.SelectionRow(
            "0xmanual", "core", enabled=False, reason="operator_pick", utility=12.5,
            data_status="valid", evidence_status="qualified", model_version="m1", policy_version="p1",
        )])

    @staticmethod
    def _transition_metrics(net):
        return selection.PortfolioMetrics(
            net, net, 0, .95, .95, .01, .50, .05,
            net_pnl=net, stress_net_pnl=net, drawdown_dollars=10,
            risk_adjusted_utility=net,
        )

    def test_quality_first_transition_removes_only_actual_shared_account_drag(self):
        profiles = [
            {
                "addr": addr, "status": "active", "profile_generation": "g2",
                "data_status": "valid", "follow_qualification": {"coreEligible": True},
            }
            for addr in ("0xa", "0xb")
        ]

        def run(nets):
            return scanner._quality_first_core_transition(
                profiles, generation_id="g2", previous_roles={}, controls={},
                desired_order=("0xa", "0xb"),
                strict_evaluate=lambda addrs: self._transition_metrics(nets[tuple(sorted(addrs))]),
            )

        drag = run({(): 0, ("0xa",): 100, ("0xb",): 80, ("0xa", "0xb"): 90})
        consensus = run({(): 0, ("0xa",): 100, ("0xb",): 90, ("0xa", "0xb"): 180})

        self.assertEqual(drag["selected"], ("0xa",))
        self.assertEqual(drag["reasons"]["0xb"], "portfolio_negative_incremental_net")
        self.assertEqual(set(consensus["selected"]), {"0xa", "0xb"})
        self.assertEqual(consensus["looRemoved"], ())

    def test_quality_first_transition_does_not_keep_drag_to_minimize_drawdown_alone(self):
        profiles = [
            {
                "addr": addr, "status": "active", "profile_generation": "g2",
                "data_status": "valid", "follow_qualification": {"coreEligible": True},
            }
            for addr in ("0xa", "0xb")
        ]
        def portfolio(net, stress, dd):
            return selection.PortfolioMetrics(
                net, stress, 0, .95, .95, dd, .50, .05,
                net_pnl=net, stress_net_pnl=stress, drawdown_dollars=dd * 10_000,
                risk_adjusted_utility=net - dd * 10_000,
            )

        metrics = {
            (): portfolio(0, 0, 0),
            # Even if B diversifies the stress replay, it is not entitled to Core when removing it raises
            # normal net PnL and the remaining portfolio is still stress-profitable and operationally safe.
            ("0xa",): portfolio(5_500, 200, .14),
            ("0xb",): portfolio(1_000, 500, .08),
            ("0xa", "0xb"): portfolio(5_405, 2_582, .11),
        }

        result = scanner._quality_first_core_transition(
            profiles, generation_id="g2", previous_roles={}, controls={},
            desired_order=("0xa", "0xb"),
            strict_evaluate=lambda addrs: metrics[tuple(sorted(addrs))],
        )

        self.assertEqual(result["selected"], ("0xa",))
        self.assertEqual(result["looRemoved"], ("0xb",))
        self.assertEqual(result["reasons"]["0xb"], "portfolio_negative_incremental_net")

    def test_starred_core_ignores_business_gate_and_cannot_be_removed_by_loo(self):
        profiles = [
            {
                "addr": "0xstar", "status": "rejected", "profile_generation": "g2",
                "data_status": "valid",
                "follow_qualification": {"coreEligible": False, "status": "recent_copy_loss"},
            },
            {
                "addr": "0xgood", "status": "active", "profile_generation": "g2",
                "data_status": "valid", "follow_qualification": {"coreEligible": True},
            },
        ]
        metrics = {
            (): 0,
            ("0xgood",): 200,
            ("0xstar",): 20,
            ("0xgood", "0xstar"): 150,
        }

        result = scanner._quality_first_core_transition(
            profiles, generation_id="g2", previous_roles={"0xstar": "core"}, controls={},
            desired_order=("0xstar", "0xgood"), pinned_order=("0xstar",),
            strict_evaluate=lambda addrs: self._transition_metrics(metrics[tuple(sorted(addrs))]),
        )

        self.assertEqual(result["selected"], ("0xstar", "0xgood"))
        self.assertEqual(result["reasons"]["0xstar"], "operator_starred_core")
        self.assertNotIn("0xstar", result["looRemoved"])

    @staticmethod
    def _metrics(net, *, stress=None, liqs=0, actionable=.8, capacity=.9, dd=.10,
                 deploy=.7, cost=.10, latency=None):
        return selection.PortfolioMetrics(
            net_lcb=net, stress_net_lcb=net if stress is None else stress,
            liquidations=liqs, actionable_open_rate=actionable, capacity_fit=capacity,
            max_drawdown=dd, peak_deploy_pct=deploy, cost_drag_ratio=cost,
        )


    def test_portfolio_economics_rejects_material_coverage_drop(self):
        base = selection.PortfolioMetrics(
            100, 100, 0, .92, .95, .03, .70, .05,
            net_pnl=100, stress_net_pnl=100, risk_adjusted_utility=90,
        )
        trial = selection.PortfolioMetrics(
            120, 120, 0, .84, .94, .03, .70, .05,
            net_pnl=120, stress_net_pnl=120, risk_adjusted_utility=110,
        )

        reason = selection.portfolio_economic_rejection_reason(
            base, trial,
            selection.SelectionConstraints(
                min_actionable_open_rate=.70, max_actionable_open_rate_drop=.05,
            ),
        )

        self.assertEqual(reason, "portfolio_open_rate_drop")


    def test_portfolio_metrics_accept_missing_optional_replay_fields(self):
        day = 86_400_000
        result = scanner._portfolio_selection_metrics({
            14: {
                "closed_n": 5,
                "positions": [
                    {"closed_at": day * index, "net_pnl": 20.0, "margin": 100.0}
                    for index in range(1, 6)
                ],
                "open_fill_rate": 0.8,
                "fee_drag": 5.0,
                "copy_gross_pnl": 100.0,
            },
        }, baseline_n=0, selected_n=1)

        self.assertEqual(result.actionable_open_rate, 0.8)
        self.assertEqual(result.capacity_fit, 0.8)
        self.assertEqual(result.max_drawdown, 0.0)
        self.assertEqual(result.cost_drag_ratio, 0.05)


if __name__ == "__main__":
    unittest.main()
