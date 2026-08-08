import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hyper import params, storage
from hyper.execution.observer import Observer
from hyper.selection import strategy_revision


class StrategyRevisionTests(unittest.TestCase):
    def _db(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = storage.connect(
            str(Path(td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )
        db.row_factory = sqlite3.Row
        params.seed_params(db)
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at,"
            "leaderboard_valid,profile_complete) "
            "VALUES ('g1','published',1,1,1,'2026-01-01','2026-01-02',1,1)"
        )
        db.execute(
            "INSERT INTO watchlist (rank,addr,score,acct_value,sector_policy_json,updated_at) "
            "VALUES (1,'0xaaa',.9,12345,'{\"allowed\":[\"crypto\"],\"crypto\":{\"allow\":true}}','now')"
        )
        db.execute(
            "INSERT INTO follow_selection "
            "(generation,addr,role,enabled,utility,acct_value,sector_policy_json,selected_at) "
            "VALUES ('g1','0xaaa','core',1,9,12345,"
            "'{\"allowed\":[\"crypto\"],\"crypto\":{\"allow\":true}}','now')"
        )
        db.execute("INSERT INTO episode (addr,coin,open_ms,seq) VALUES ('0xaaa','BTC',1,0)")
        db.commit()
        return db

    def test_revision_freezes_params_and_target_context(self):
        db = self._db()
        db.execute("UPDATE params SET value='90' WHERE key='MARGIN_EQUITY_PCT'")
        created = strategy_revision.create_revision(db, "g1", source="test")
        db.commit()

        db.execute("UPDATE params SET value='9' WHERE key='STABLE_MARGIN_PCT'")
        db.execute("UPDATE watchlist SET acct_value=999 WHERE addr='0xaaa'")
        db.commit()
        active = strategy_revision.load_active(db)

        self.assertEqual(active["revision"], created["revision"])
        self.assertNotEqual(active["params"]["STABLE_MARGIN_PCT"], .09)
        self.assertEqual(active["targets"][0]["acctValue"], 12345)
        self.assertEqual(active["targets"][0]["seedCoins"], ["BTC"])
        self.assertTrue(active["targets"][0]["entryEligible"])
        self.assertEqual(active["targets"][0]["retentionStatus"], "healthy")
        self.assertIn("COPY_POLICY_VERSION", active["params"])
        self.assertEqual(active["params"]["CORE_MIN_DYNAMIC_COPY_RETURN_30D"], 0.10)
        self.assertEqual(active["params"]["CORE_MIN_DYNAMIC_COPY_RETURN_7D"], 0.03)
        self.assertEqual(active["params"]["SOURCE_MIN_EPISODE_WIN_RATE"], 0.70)
        self.assertNotIn("ROUGH_COPY_MIN_RETURN_30D", active["params"])
        self.assertNotIn("ROUGH_COPY_MIN_RETURN_7D", active["params"])
        self.assertEqual(active["params"]["COPY_DEEP_BAG_EVENT_MIN_HOURS"], 4.0)
        self.assertEqual(active["params"]["CORE_COPY_MAX_LIQUIDATIONS_30D"], 3)
        self.assertEqual(active["params"]["MARGIN_EQUITY_PCT"], .90)
        self.assertNotIn("MAX_DEPLOY_PCT", active["params"])
        self.assertNotIn("DEPLOY_FULL_PCT", active["params"])
        self.assertNotIn("CORE_INTRATRADE_DD_MAX", active["params"])
        self.assertNotIn("CORE_DEEP_BAG_MIN_RECOVERY_RATE", active["params"])
        self.assertNotIn("WALLET_HWM_EXIT_DD_PCT", active["params"])
        self.assertNotIn("WALLET_STOCK_SIDE_CAP_PCT", active["params"])

    def test_revision_legally_snapshots_zero_core_targets(self):
        db = self._db()
        db.execute(
            "UPDATE follow_selection SET role='exit_only',enabled=0 WHERE generation='g1'"
        )

        created = strategy_revision.create_revision(db, "g1", source="zero_core")
        db.commit()
        active = strategy_revision.load_active(db)

        self.assertEqual(created["targetCount"], 0)
        self.assertEqual(active["targets"], [])
        self.assertEqual(strategy_revision.resolved_targets(db, active), [])

    def test_activation_compare_and_swap_rejects_stale_parent(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        second = strategy_revision.create_revision(
            db,
            "g1",
            source="manual",
            parent_revision=first["revision"],
            expected_active_revision=first["revision"],
            enqueue_reload=False,
        )
        db.commit()
        with self.assertRaisesRegex(RuntimeError, "strategy_revision_changed"):
            strategy_revision.create_revision(
                db,
                "g1",
                source="stale_tuner",
                parent_revision=first["revision"],
                expected_active_revision=first["revision"],
                enqueue_reload=False,
            )
        db.rollback()
        self.assertEqual(strategy_revision.active_revision_id(db), second["revision"])

    def test_activated_revision_automatically_descends_from_current_active(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        second = strategy_revision.create_revision(db, "g1", source="scanner", enqueue_reload=False)
        db.commit()

        self.assertEqual(second["parentRevision"], first["revision"])
        self.assertEqual(
            strategy_revision.load_active(db)["parentRevision"],
            first["revision"],
        )

    def test_observer_manual_param_command_creates_child_and_loads_bundle(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        db.commit()
        db.execute("UPDATE params SET value='7' WHERE key='STABLE_MARGIN_PCT'")
        db.commit()
        observer = Observer(db, [], {})

        result = asyncio.run(observer._dispatch_command("reload_params", {
            "by": "dashboard_params",
            "createStrategyRevision": True,
            "reason": "operator_follow_params_changed",
        }))

        active = strategy_revision.load_active(db)
        self.assertEqual(active["parentRevision"], first["revision"])
        self.assertEqual(active["params"]["STABLE_MARGIN_PCT"], .07)
        self.assertEqual(observer.strategy_revision_id, active["revision"])
        self.assertEqual(observer.addrs, ["0xaaa"])
        self.assertEqual(result["revision"], active["revision"])

    def test_live_param_reload_advances_session_revision_and_margin_budget(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        stamp = "2026-08-02T00:00:00Z"
        db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
            "sizing_anchor,margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
            "VALUES ('live-current','live','mainnet','live_running',?,?,?,200,1,200,0,NULL,?,?)",
            ("0x" + "a" * 40, "0x" + "b" * 40, first["revision"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
            "VALUES (1,'live','live_running','live-current',?) "
            "ON CONFLICT(id) DO UPDATE SET selected_mode='live',state='live_running',"
            "active_session_id='live-current',updated_at=excluded.updated_at",
            (stamp,),
        )
        db.execute("UPDATE params SET value='80' WHERE key='MARGIN_EQUITY_PCT'")
        db.commit()
        observer = Observer(db, [], {})
        observer.live_executor = SimpleNamespace(session={
            "session_id": "live-current",
            "strategy_revision": first["revision"],
            "margin_equity_pct": 1.0,
        })

        result = asyncio.run(observer._dispatch_command("reload_params", {
            "by": "dashboard_params",
            "createStrategyRevision": True,
            "reason": "operator_follow_params_changed",
        }))

        active = strategy_revision.load_active(db)
        session = db.execute(
            "SELECT strategy_revision,margin_equity_pct FROM execution_session "
            "WHERE session_id='live-current'"
        ).fetchone()
        self.assertEqual(session["strategy_revision"], active["revision"])
        self.assertEqual(session["margin_equity_pct"], 0.8)
        self.assertEqual(observer.live_executor.session["strategy_revision"], active["revision"])
        self.assertEqual(observer.live_executor.session["margin_equity_pct"], 0.8)
        self.assertEqual(result["revision"], active["revision"])
        self.assertFalse(observer.paused)
        self.assertEqual(observer.execution_state, "live_running")
        self.assertEqual(db.execute(
            "SELECT state FROM execution_session WHERE session_id='live-current'"
        ).fetchone()[0], "live_running")
        self.assertEqual(db.execute(
            "SELECT state FROM execution_control WHERE id=1"
        ).fetchone()[0], "live_running")

    def test_hot_reload_retries_lock_without_exposing_unbound_revision(self):
        async def run():
            db = self._db()
            first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
            stamp = "2026-08-02T00:00:00Z"
            db.execute(
                "INSERT INTO execution_session "
                "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
                "sizing_anchor,margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
                "VALUES ('live-current','live','mainnet','live_running',?,?,?,200,1,200,0,NULL,?,?)",
                ("0x" + "a" * 40, "0x" + "b" * 40, first["revision"], stamp, stamp),
            )
            db.execute(
                "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
                "VALUES (1,'live','live_running','live-current',?) "
                "ON CONFLICT(id) DO UPDATE SET selected_mode='live',state='live_running',"
                "active_session_id='live-current',updated_at=excluded.updated_at",
                (stamp,),
            )
            db.commit()
            observer = Observer(db, [], {})
            observer.live_executor = SimpleNamespace(session={
                "session_id": "live-current",
                "strategy_revision": first["revision"],
                "margin_equity_pct": 1.0,
            })
            observer._reload_strategy()
            second = strategy_revision.create_revision(db, "g1", source="daily", enqueue_reload=False)
            db.commit()
            calls = 0

            def bind(revision):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise sqlite3.OperationalError("database is locked")
                db.execute(
                    "UPDATE execution_session SET strategy_revision=? WHERE session_id='live-current'",
                    (revision,),
                )
                db.commit()
                observer.live_executor.session["strategy_revision"] = revision

            async def no_wait(_delay):
                self.assertEqual(observer.strategy_revision_id, first["revision"])
                self.assertTrue(observer._strategy_bind_pending)

            with patch.object(observer, "_bind_live_strategy_revision", side_effect=bind), \
                    patch("hyper.execution.observer.asyncio.sleep", side_effect=no_wait):
                await observer._hot_reload_strategy()

            self.assertEqual(calls, 2)
            self.assertEqual(observer.strategy_revision_id, second["revision"])
            self.assertEqual(observer.live_executor.session["strategy_revision"], second["revision"])
            self.assertFalse(observer._strategy_bind_pending)

        asyncio.run(run())

    def test_command_completion_retries_lock_without_redispatch(self):
        async def run():
            db = self._db()
            observer = Observer(db, [], {})
            db.close()
            attempts = []

            def execute(sql, values):
                attempts.append((sql, values))
                if len(attempts) == 1:
                    raise sqlite3.OperationalError("database is locked")

            fake = SimpleNamespace(
                execute=execute,
                commit=lambda: None,
                rollback=lambda: None,
            )
            observer.db = fake

            async def no_wait(_delay):
                return None

            with patch("hyper.execution.observer.asyncio.sleep", side_effect=no_wait):
                await observer._persist_command_completion(
                    7, status="done", result={"reloaded": True},
                )

            self.assertEqual(len(attempts), 2)
            self.assertIn("status='done'", attempts[-1][0])
            self.assertEqual(attempts[-1][1][-1], 7)

        asyncio.run(run())

    def test_daily_core_revision_hot_switch_keeps_live_entries_running(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        stamp = "2026-08-02T00:00:00Z"
        db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
            "sizing_anchor,margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
            "VALUES ('live-current','live','mainnet','live_running',?,?,?,200,1,200,0,NULL,?,?)",
            ("0x" + "a" * 40, "0x" + "b" * 40, first["revision"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
            "VALUES (1,'live','live_running','live-current',?) "
            "ON CONFLICT(id) DO UPDATE SET selected_mode='live',state='live_running',"
            "active_session_id='live-current',updated_at=excluded.updated_at",
            (stamp,),
        )
        daily = strategy_revision.create_revision(
            db, "g1", source="challenger_daily", enqueue_reload=False,
        )
        db.commit()
        observer = Observer(db, [], {})
        observer.live_executor = SimpleNamespace(session={
            "session_id": "live-current",
            "state": "live_running",
            "strategy_revision": first["revision"],
            "margin_equity_pct": 1.0,
        })

        observer._reload_strategy()
        self.assertTrue(observer._bind_live_strategy_revision())

        self.assertEqual(daily["parentRevision"], first["revision"])
        self.assertEqual(observer.strategy_revision_id, daily["revision"])
        self.assertEqual(observer.execution_state, "live_running")
        self.assertFalse(observer.paused)
        self.assertEqual(tuple(db.execute(
            "SELECT state,strategy_revision FROM execution_session WHERE session_id='live-current'"
        ).fetchone()), ("live_running", daily["revision"]))

    def test_lineage_only_reconcile_failure_auto_resumes_after_full_repair(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        legacy = strategy_revision.create_revision(
            db, "g1", source="challenger_daily", parent_revision=None,
            enqueue_reload=False, allow_lineage_repair=True,
        )
        stamp = "2026-08-02T00:00:00Z"
        db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
            "sizing_anchor,margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
            "VALUES ('live-current','live','mainnet','reconcile_required',?,?,?,200,1,200,0,NULL,?,?)",
            ("0x" + "a" * 40, "0x" + "b" * 40, first["revision"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO execution_control "
            "(id,selected_mode,state,active_session_id,last_error_code,last_error_at,updated_at) "
            "VALUES (1,'live','reconcile_required','live-current','STRATEGY_REVISION_MISMATCH',?,?) "
            "ON CONFLICT(id) DO UPDATE SET selected_mode='live',state='reconcile_required',"
            "active_session_id='live-current',last_error_code='STRATEGY_REVISION_MISMATCH',"
            "last_error_at=excluded.last_error_at,updated_at=excluded.updated_at",
            (stamp, stamp),
        )
        db.commit()
        observer = Observer(db, [], {})
        observer.live_executor = SimpleNamespace(session={
            "session_id": "live-current",
            "state": "reconcile_required",
            "strategy_revision": first["revision"],
            "margin_equity_pct": 1.0,
        })

        self.assertTrue(observer._strategy_revision_recovery_requested())
        # Successful venue reconciliation intentionally lands in paused first.
        db.execute("UPDATE execution_session SET state='paused' WHERE session_id='live-current'")
        db.execute(
            "UPDATE execution_control SET state='paused',last_error_code=NULL,last_error_at=NULL WHERE id=1"
        )
        observer.live_executor.session["state"] = "paused"
        db.commit()
        observer._reload_strategy()
        self.assertTrue(observer._bind_live_strategy_revision())
        self.assertNotEqual(observer.strategy_revision_id, legacy["revision"])

        resumed = observer._resume_after_strategy_revision_recovery(
            requested=True,
            reconcile_result={
                "ok": True, "unknownPositions": 0, "unknownOrders": 0, "ambiguousIntents": 0,
            },
            ledger_projection_ok=True,
        )

        self.assertTrue(resumed)
        self.assertFalse(observer.paused)
        self.assertEqual(observer.execution_state, "live_running")
        self.assertEqual(observer.live_executor.session["state"], "live_running")
        self.assertEqual(tuple(db.execute(
            "SELECT state,last_error_code FROM execution_control WHERE id=1"
        ).fetchone()), ("live_running", None))

    def test_lineage_recovery_never_resumes_exchange_ambiguity(self):
        db = self._db()
        revision = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        stamp = "2026-08-02T00:00:00Z"
        db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
            "sizing_anchor,margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
            "VALUES ('live-current','live','mainnet','paused',?,?,?,200,1,200,0,NULL,?,?)",
            ("0x" + "a" * 40, "0x" + "b" * 40, revision["revision"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
            "VALUES (1,'live','paused','live-current',?) "
            "ON CONFLICT(id) DO UPDATE SET selected_mode='live',state='paused',"
            "active_session_id='live-current',updated_at=excluded.updated_at",
            (stamp,),
        )
        db.commit()
        observer = Observer(db, [], {})
        observer.strategy_revision_id = revision["revision"]
        observer.live_executor = SimpleNamespace(session={
            "session_id": "live-current", "state": "paused",
            "strategy_revision": revision["revision"],
        })

        resumed = observer._resume_after_strategy_revision_recovery(
            requested=True,
            reconcile_result={"ok": False, "unknownPositions": 1},
            ledger_projection_ok=True,
        )

        self.assertFalse(resumed)
        self.assertTrue(observer.paused)
        self.assertEqual(db.execute(
            "SELECT state FROM execution_control WHERE id=1"
        ).fetchone()[0], "paused")

    def test_live_session_revision_rejects_lateral_active_revision(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        replacement = strategy_revision.create_revision(
            db, "g1", source="replacement", parent_revision=None, enqueue_reload=False,
            allow_lineage_repair=True,
        )
        stamp = "2026-08-02T00:00:00Z"
        db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
            "sizing_anchor,margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
            "VALUES ('live-current','live','mainnet','live_running',?,?,?,200,1,200,0,NULL,?,?)",
            ("0x" + "a" * 40, "0x" + "b" * 40, first["revision"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
            "VALUES (1,'live','live_running','live-current',?) "
            "ON CONFLICT(id) DO UPDATE SET selected_mode='live',state='live_running',"
            "active_session_id='live-current',updated_at=excluded.updated_at",
            (stamp,),
        )
        db.commit()
        observer = Observer(db, [], {})
        observer.live_executor = SimpleNamespace(session={
            "session_id": "live-current",
            "strategy_revision": first["revision"],
            "margin_equity_pct": 1.0,
        })
        observer._reload_strategy()

        with self.assertRaisesRegex(RuntimeError, "live_strategy_revision_not_descendant"):
            observer._bind_live_strategy_revision()
        session_revision = db.execute(
            "SELECT strategy_revision FROM execution_session WHERE session_id='live-current'"
        ).fetchone()[0]
        self.assertEqual(observer.strategy_revision_id, replacement["revision"])
        self.assertEqual(session_revision, first["revision"])

    def test_live_session_repairs_legacy_parentless_daily_publication(self):
        db = self._db()
        first = strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        legacy = strategy_revision.create_revision(
            db,
            "g1",
            source="challenger_daily",
            parent_revision=None,
            enqueue_reload=False,
            allow_lineage_repair=True,
        )
        stamp = "2026-08-02T00:00:00Z"
        db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
            "sizing_anchor,margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
            "VALUES ('live-current','live','mainnet','live_running',?,?,?,200,1,200,0,NULL,?,?)",
            ("0x" + "a" * 40, "0x" + "b" * 40, first["revision"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
            "VALUES (1,'live','live_running','live-current',?) "
            "ON CONFLICT(id) DO UPDATE SET selected_mode='live',state='live_running',"
            "active_session_id='live-current',updated_at=excluded.updated_at",
            (stamp,),
        )
        db.commit()
        observer = Observer(db, [], {})
        observer.live_executor = SimpleNamespace(session={
            "session_id": "live-current",
            "strategy_revision": first["revision"],
            "margin_equity_pct": 1.0,
        })
        observer._reload_strategy()

        self.assertTrue(observer._bind_live_strategy_revision())

        active = strategy_revision.load_active(db)
        session_revision = db.execute(
            "SELECT strategy_revision FROM execution_session WHERE session_id='live-current'"
        ).fetchone()[0]
        self.assertEqual(active["source"], "strategy_lineage_repair")
        self.assertEqual(active["parentRevision"], first["revision"])
        self.assertEqual(active["paramsHash"], legacy["paramsHash"])
        self.assertEqual(session_revision, active["revision"])
        self.assertEqual(observer.strategy_revision_id, active["revision"])

    def test_operator_disable_is_live_overlay_not_snapshot_mutation(self):
        db = self._db()
        strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        db.execute("INSERT INTO target_controls (addr,enabled) VALUES ('0xaaa',0)")
        db.commit()

        active = strategy_revision.load_active(db)
        self.assertEqual(len(active["targets"]), 1)
        self.assertEqual(strategy_revision.resolved_targets(db, active), [])

    def test_probation_entry_freeze_is_part_of_immutable_target_snapshot(self):
        db = self._db()
        db.execute(
            "UPDATE follow_selection SET entry_eligible=0,retention_status='probation',"
            "retention_failure_reason='strict_copy_7d_conservative_return_below_floor',"
            "retention_failure_streak=1 WHERE generation='g1' AND addr='0xaaa'"
        )
        strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        db.commit()

        target = strategy_revision.load_active(db)["targets"][0]
        self.assertFalse(target["entryEligible"])
        self.assertEqual(target["retentionStatus"], "probation")
        self.assertEqual(target["retentionFailureStreak"], 1)

    def test_legacy_target_snapshot_resolves_current_probation_policy(self):
        db = self._db()
        strategy_revision.create_revision(db, "g1", source="scan", enqueue_reload=False)
        db.execute(
            "UPDATE follow_selection SET entry_eligible=0,retention_status='probation',"
            "retention_failure_streak=1 WHERE generation='g1' AND addr='0xaaa'"
        )
        db.commit()
        legacy = strategy_revision.load_active(db)
        legacy["targets"] = [{
            key: value for key, value in legacy["targets"][0].items()
            if key not in {
                "entryEligible", "retentionStatus",
                "retentionFailureReason", "retentionFailureStreak",
            }
        }]

        target = strategy_revision.resolved_targets(db, legacy)[0]
        self.assertFalse(target["entryEligible"])
        self.assertEqual(target["retentionStatus"], "probation")

    def test_wallet_star_command_persists_original_order_timestamp(self):
        db = self._db()
        observer = Observer(db, [], {})

        first = observer._cmd_wallet_star("0xAAA", True)
        second = observer._cmd_wallet_star("0xaaa", True)
        row = db.execute(
            "SELECT pinned,pinned_at FROM target_controls WHERE addr='0xaaa'"
        ).fetchone()

        self.assertTrue(first["starred"])
        self.assertTrue(second["starred"])
        self.assertEqual(row["pinned"], 1)
        self.assertEqual(row["pinned_at"], first["starredAt"])

        observer._cmd_wallet_star("0xaaa", False)
        row = db.execute(
            "SELECT pinned,pinned_at FROM target_controls WHERE addr='0xaaa'"
        ).fetchone()
        self.assertEqual(row["pinned"], 0)
        self.assertIsNone(row["pinned_at"])


if __name__ == "__main__":
    unittest.main()
