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

        addrs, seeds = load_targets(db, 10, 0.90)

        self.assertEqual(addrs, ["0xcore"])
        self.assertEqual(seeds, {"0xcore": {"BTC"}})

    def test_published_empty_core_does_not_fall_back(self):
        db = self._db()
        db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0xlegacy',.99,'now')")
        self._published(db)

        addrs, _ = load_targets(db, 10, 0.5)

        self.assertEqual(addrs, [])
        self.assertEqual(selection.published_core_addrs(db), [])

    def test_no_score_fallback_before_first_publication(self):
        db = self._db()
        db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0xlegacy',.8,'now')")
        db.commit()

        addrs, _ = load_targets(db, 10, 0.7)

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

    def test_lifecycle_entry_keep_and_confirmed_soft_exit(self):
        day = 24 * 60 * 60 * 1000
        now = 100 * day
        entry = selection.decide_lifecycle(selection.LifecycleEvidence(
            addr="0xA", now_ms=now, consecutive_complete_good=2,
            last_actionable_open_ms=now - day, oos_closed_n=7,
            positive_probability=.70, challenger_since_ms=now - day,
        ))
        self.assertEqual(entry.role, selection.CORE)

        pending = selection.decide_lifecycle(selection.LifecycleEvidence(
            addr="0xB", now_ms=now, current_role="core", soft_bad=True,
            consecutive_soft_bad=1, last_actionable_open_ms=now,
        ))
        self.assertEqual(pending.role, selection.CORE)

        exited = selection.decide_lifecycle(selection.LifecycleEvidence(
            addr="0xB", now_ms=now, current_role="core", soft_bad=True,
            consecutive_soft_bad=2, last_actionable_open_ms=now, has_open_copy=True,
        ))
        self.assertEqual(exited.role, selection.EXIT_ONLY)

    def test_hard_exits_immediate_and_soft_changes_are_limited(self):
        now = 10 * 24 * 60 * 60 * 1000
        rows = [
            selection.LifecycleEvidence("0xhard", now, current_role="core", hard_exit=True),
            selection.LifecycleEvidence("0xsoft1", now, current_role="core", soft_bad=True,
                                                consecutive_soft_bad=2, last_actionable_open_ms=now),
            selection.LifecycleEvidence("0xsoft2", now, current_role="core", soft_bad=True,
                                                consecutive_soft_bad=2, last_actionable_open_ms=now),
        ]

        decisions = {d.addr: d for d in selection.decide_lifecycles(rows)}

        self.assertEqual(decisions["0xhard"].role, selection.REJECTED)
        self.assertEqual(decisions["0xsoft1"].role, selection.CHALLENGER)
        self.assertEqual(decisions["0xsoft2"].role, selection.CORE)
        self.assertEqual(decisions["0xsoft2"].reason, "soft_change_budget")

    @staticmethod
    def _metrics(net, *, stress=None, liqs=0, actionable=.8, capacity=.9, dd=.10,
                 deploy=.7, cost=.10, latency=None):
        return selection.PortfolioMetrics(
            net_lcb=net, stress_net_lcb=net if stress is None else stress,
            liquidations=liqs, actionable_open_rate=actionable, capacity_fit=capacity,
            max_drawdown=dd, peak_deploy_pct=deploy, cost_drag_ratio=cost,
        )

    def test_marginal_selector_can_choose_one_or_keep_empty(self):
        metrics = {
            (): self._metrics(0, stress=0),
            ("0xbad",): self._metrics(20, cost=.40),
            ("0xgood",): self._metrics(10),
        }
        result = selection.select_marginal_core(
            [], ["0xbad", "0xgood"], lambda addrs: metrics[addrs],
        )
        self.assertEqual(result.selected, ("0xgood",))
        self.assertEqual(result.action, "add")

        empty = selection.select_marginal_core([], [], lambda _: self._metrics(0, stress=0))
        self.assertEqual(empty.selected, ())

    def test_marginal_selector_replacement_requires_all_constraints(self):
        values = {
            ("0xold",): self._metrics(100),
            ("0xnew",): self._metrics(106),
            ("0xrisky",): self._metrics(150, dd=.12),
        }
        result = selection.select_marginal_core(
            ["0xold"], ["0xnew", "0xrisky"], lambda addrs: values[addrs],
            selection.SelectionConstraints(max_targets=1),
        )
        self.assertEqual(result.selected, ("0xnew",))
        self.assertEqual(result.removed, ("0xold",))

    def test_cold_bootstrap_adds_every_positive_marginal_wallet(self):
        values = {
            (): self._metrics(0, stress=0, dd=0, deploy=0, cost=0, latency=0),
            ("0xa",): self._metrics(10, dd=.005),
            ("0xb",): self._metrics(8, dd=.004),
            ("0xbad",): self._metrics(50, cost=.40),
            ("0xa", "0xb"): self._metrics(20, dd=.008),
            ("0xa", "0xbad"): self._metrics(60, cost=.40),
            ("0xb", "0xbad"): self._metrics(58, cost=.40),
            ("0xa", "0xb", "0xbad"): self._metrics(70, cost=.40),
        }

        result = selection.select_bootstrap_core(
            ["0xbad", "0xb", "0xa"], lambda addrs: values[addrs],
        )

        self.assertEqual(result.selected, ("0xa", "0xb"))
        self.assertEqual(result.added, ("0xa", "0xb"))
        self.assertEqual(result.action, "bootstrap")

    def test_ranked_economic_selector_prices_liquidation_through_pnl_and_drawdown(self):
        def economic(net, dd, liqs=0):
            drawdown_dollars = dd * 10_000
            return selection.PortfolioMetrics(
                net, net, liqs, .9, .9, dd, .6, .1,
                net_pnl=net, stress_net_pnl=net, drawdown_dollars=drawdown_dollars,
                risk_adjusted_utility=net - drawdown_dollars,
            )

        values = {
            (): economic(0, 0),
            ("0xprofitable",): economic(3_000, .10, liqs=2),
            ("0xrisky",): economic(2_000, .25, liqs=1),
            ("0xprofitable", "0xrisky"): economic(3_500, .25, liqs=3),
        }
        result = selection.select_ranked_positive_core(
            ["0xprofitable", "0xrisky"], lambda addrs: values[addrs],
        )

        self.assertEqual(result.selected, ("0xprofitable",))
        self.assertEqual(result.metrics.liquidations, 2)
        self.assertEqual(
            selection.portfolio_economic_rejection_reason(
                values[("0xprofitable",)], values[("0xprofitable", "0xrisky")],
                selection.SelectionConstraints(),
            ),
            "portfolio_risk_adjusted_gain_low",
        )

    def test_ranked_selector_can_replace_two_lower_quality_wallets(self):
        def economic(net, dd=.05):
            drawdown_dollars = dd * 10_000
            return selection.PortfolioMetrics(
                net, net, 0, .9, .9, dd, .7, .1,
                net_pnl=net, stress_net_pnl=net, drawdown_dollars=drawdown_dollars,
                risk_adjusted_utility=net - drawdown_dollars,
            )

        values = {
            ("0xlow1", "0xlow2"): economic(100),
            ("0xhigh", "0xlow1"): economic(104),
            ("0xhigh", "0xlow2"): economic(103),
            ("0xhigh",): economic(130),
        }
        result = selection.select_ranked_positive_core(
            ["0xhigh"],
            lambda addrs: values[addrs],
            selection.SelectionConstraints(max_targets=2),
            initial_core=["0xlow1", "0xlow2"],
            score_by_addr={"0xhigh": .90, "0xlow1": .60, "0xlow2": .50},
            individual_net_by_addr={"0xhigh": 30, "0xlow1": 10, "0xlow2": 20},
            max_replace_out=2,
        )

        self.assertEqual(result.selected, ("0xhigh",))
        self.assertEqual(result.removed, ("0xlow1", "0xlow2"))
        self.assertEqual(result.action, "rebalance")

    def test_ranked_selector_does_not_replace_higher_profit_incumbent(self):
        def economic(net):
            return selection.PortfolioMetrics(
                net, net, 0, .9, .9, 0, .7, .1,
                net_pnl=net, stress_net_pnl=net, drawdown_dollars=0,
                risk_adjusted_utility=net,
            )

        values = {("0xold",): economic(100)}
        result = selection.select_ranked_positive_core(
            ["0xnew"],
            lambda addrs: values[addrs],
            selection.SelectionConstraints(max_targets=1),
            initial_core=["0xold"],
            score_by_addr={"0xnew": .90, "0xold": .50},
            individual_net_by_addr={"0xnew": 10, "0xold": 20},
        )

        self.assertEqual(result.selected, ("0xold",))
        self.assertEqual(result.evaluated, 1)

    def test_ranked_quality_upgrade_still_requires_portfolio_gain(self):
        def economic(net):
            return selection.PortfolioMetrics(
                net, net, 0, .9, .9, 0, .7, .1,
                net_pnl=net, stress_net_pnl=net, drawdown_dollars=0,
                risk_adjusted_utility=net,
            )

        values = {
            ("0xold",): economic(100),
            ("0xnew",): economic(80),
        }
        result = selection.select_ranked_positive_core(
            ["0xnew"],
            lambda addrs: values[addrs],
            selection.SelectionConstraints(max_targets=1),
            initial_core=["0xold"],
            score_by_addr={"0xnew": .90, "0xold": .50},
            individual_net_by_addr={"0xnew": 30, "0xold": 20},
        )

        self.assertEqual(result.selected, ("0xold",))
        self.assertEqual(result.removed, ())

    def test_smart_core_search_builds_seed_then_stops_on_zero_marginal(self):
        nets = {
            (): 0,
            ("0xa",): 10, ("0xb",): 9, ("0xc",): 8, ("0xd",): 7,
            ("0xa", "0xb"): 12, ("0xa", "0xc"): 25, ("0xa", "0xd"): 11,
            ("0xb", "0xc"): 13, ("0xb", "0xd"): 10, ("0xc", "0xd"): 9,
            ("0xa", "0xb", "0xc"): 26,
            ("0xa", "0xb", "0xd"): 13,
            ("0xa", "0xc", "0xd"): 30,
            ("0xb", "0xc", "0xd"): 14,
            ("0xa", "0xb", "0xc", "0xd"): 30,
        }

        def evaluate(addrs):
            net = nets[addrs]
            return selection.PortfolioMetrics(
                net, net, 0, .9, .95, .05, .7, .1,
                net_pnl=net, stress_net_pnl=net, drawdown_dollars=500,
                risk_adjusted_utility=net - 500,
            )

        result = selection.search_smart_core(
            ["0xa", "0xb", "0xc", "0xd"], evaluate,
            selection.SelectionConstraints(max_targets=4),
            seed_target=2, beam_width=2, swap_passes=1, max_replace_out=2,
        )

        self.assertEqual(result.selected, ("0xa", "0xc", "0xd"))
        self.assertEqual(result.metrics.net_pnl, 30)
        self.assertEqual(result.search_meta["selectedCount"], 3)
        self.assertEqual(result.search_meta["stopReason"], "no_positive_expansion_marginal")

    def test_smart_core_seed_target_is_not_a_minimum_quota(self):
        def evaluate(addrs):
            net = 10 if addrs == ("0xa",) else 9 if addrs else 0
            return selection.PortfolioMetrics(
                net, net, 0, .9, .95, .05, .7, .1,
                net_pnl=net, stress_net_pnl=net, drawdown_dollars=500,
                risk_adjusted_utility=net - 500,
            )

        result = selection.search_smart_core(
            ["0xa", "0xb"], evaluate,
            selection.SelectionConstraints(max_targets=2),
            seed_target=2, beam_width=1, max_replace_out=1,
        )

        self.assertEqual(result.selected, ("0xa",))
        self.assertEqual(result.search_meta["stopReason"], "no_positive_seed_marginal")

    def test_smart_core_validates_final_sizes_with_effective_params(self):
        neutral = {
            (): 0, ("0xa",): 10, ("0xb",): 9,
            ("0xa", "0xb"): 20,
        }
        effective = {("0xa",): 30, ("0xb",): 15, ("0xa", "0xb"): 25}

        def metrics(net):
            return selection.PortfolioMetrics(
                net, net, 0, .9, .95, .05, .7, .1,
                net_pnl=net, stress_net_pnl=net, drawdown_dollars=500,
                risk_adjusted_utility=net - 500,
            )

        result = selection.search_smart_core(
            ["0xa", "0xb"],
            lambda addrs: metrics(neutral[addrs]),
            selection.SelectionConstraints(max_targets=2),
            seed_target=1,
            beam_width=2,
            max_replace_out=1,
            validation_evaluator=lambda addrs: metrics(effective[addrs]),
        )

        self.assertEqual(result.selected, ("0xa",))
        self.assertEqual(result.search_meta["neutralSelectedCount"], 2)
        self.assertEqual(result.search_meta["selectedCount"], 1)

    def test_smart_core_stops_when_post_seed_gain_is_below_ratio(self):
        nets = {
            (): 0, ("0xa",): 10, ("0xb",): 9, ("0xc",): 8,
            ("0xa", "0xb"): 15, ("0xa", "0xc"): 25, ("0xb", "0xc"): 14,
            ("0xa", "0xb", "0xc"): 30,
        }

        def evaluate(addrs):
            net = nets[addrs]
            return selection.PortfolioMetrics(
                net, net, 0, .9, .95, .05, .7, .1,
                net_pnl=net, stress_net_pnl=net, drawdown_dollars=500,
                risk_adjusted_utility=net - 500,
            )

        result = selection.search_smart_core(
            ["0xa", "0xb", "0xc"], evaluate,
            selection.SelectionConstraints(max_targets=3),
            seed_target=2, beam_width=2, max_replace_out=1,
            min_marginal_gain_ratio=.25,
        )

        self.assertEqual(result.selected, ("0xa", "0xc"))
        self.assertEqual(result.search_meta["stopReason"], "expansion_marginal_gain_below_floor")
        self.assertAlmostEqual(result.search_meta["stoppedMarginal"]["ratio"], .20)

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
