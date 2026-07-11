import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hl import selection, storage
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

    def test_legacy_fallback_only_before_first_publication(self):
        db = self._db()
        db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0xlegacy',.8,'now')")
        db.commit()

        addrs, _ = load_targets(db, 10, 0.7)

        self.assertEqual(addrs, ["0xlegacy"])
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
                 deploy=.7, cost=.10, latency=.05):
        return selection.PortfolioMetrics(
            net_lcb=net, stress_net_lcb=net if stress is None else stress,
            liquidations=liqs, actionable_open_rate=actionable, capacity_fit=capacity,
            max_drawdown=dd, peak_deploy_pct=deploy, cost_drag_ratio=cost,
            poll_latency_degradation=latency,
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


if __name__ == "__main__":
    unittest.main()
