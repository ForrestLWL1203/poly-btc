import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest

from hyper import storage
from hyper.selection import wallet_risk


class WalletRiskTest(unittest.TestCase):
    def test_low_confirms_medium_after_72_hours_and_health_recovers(self):
        low = wallet_risk.advance(
            assessed_at="2026-01-01T00:00:00Z",
            reason="copy_profit_factor_below_1_25",
        )
        self.assertEqual(wallet_risk.LOW, low.level)
        pending = wallet_risk.advance(
            previous_level=low.level,
            previous_count=low.confirmation_count,
            previous_reasons=low.reasons,
            previous_first_confirmed_at=low.first_confirmed_at,
            assessed_at="2026-01-03T23:59:59Z",
            reason="latest_7d_inactive",
        )
        self.assertEqual(wallet_risk.LOW, pending.level)
        medium = wallet_risk.advance(
            previous_level=low.level,
            previous_count=low.confirmation_count,
            previous_reasons=low.reasons,
            previous_first_confirmed_at=low.first_confirmed_at,
            assessed_at="2026-01-04T00:00:00Z",
            reason="latest_7d_inactive",
        )
        self.assertEqual(wallet_risk.MEDIUM, medium.level)
        self.assertTrue(medium.entry_allowed)
        recovered = wallet_risk.advance(
            previous_level=medium.level,
            previous_count=medium.confirmation_count,
            previous_reasons=medium.reasons,
            previous_first_confirmed_at=medium.first_confirmed_at,
            assessed_at="2026-01-05T00:00:00Z",
            reason=None,
        )
        self.assertEqual(wallet_risk.NORMAL, recovered.level)

    def test_severe_loss_is_immediate_medium_and_high_is_durable(self):
        medium = wallet_risk.advance(
            assessed_at="2026-01-01T00:00:00Z",
            reason="copy_30d_closed_pnl_not_positive",
        )
        self.assertEqual(wallet_risk.MEDIUM, medium.level)
        high = wallet_risk.advance(
            assessed_at="2026-01-02T00:00:00Z",
            reason="copy_single_liquidation_loss_over_8pct",
        )
        persisted = wallet_risk.advance(
            previous_level=high.level,
            previous_count=high.confirmation_count,
            previous_reasons=high.reasons,
            previous_first_confirmed_at=high.first_confirmed_at,
            assessed_at="2026-02-01T00:00:00Z",
            reason=None,
        )
        self.assertEqual(wallet_risk.HIGH, persisted.level)
        self.assertFalse(persisted.entry_allowed)

    def test_unavailable_recovers_but_deferred_does_not_advance(self):
        unavailable = wallet_risk.advance(
            assessed_at="2026-01-01T00:00:00Z",
            reason="source_zero_equity_no_positions",
        )
        self.assertEqual(wallet_risk.UNAVAILABLE, unavailable.level)
        recovered = wallet_risk.advance(
            previous_level=unavailable.level,
            previous_count=unavailable.confirmation_count,
            previous_reasons=unavailable.reasons,
            previous_first_confirmed_at=unavailable.first_confirmed_at,
            assessed_at="2026-01-02T00:00:00Z",
            reason=None,
        )
        self.assertEqual(wallet_risk.NORMAL, recovered.level)
        deferred = wallet_risk.advance(
            previous_level=wallet_risk.LOW,
            previous_count=1,
            previous_reasons=("latest_7d_inactive",),
            previous_first_confirmed_at="2026-01-01T00:00:00Z",
            assessed_at="2026-01-10T00:00:00Z",
            reason="copy_path_incomplete",
            deferred=True,
        )
        self.assertEqual(wallet_risk.LOW, deferred.level)
        self.assertEqual(1, deferred.confirmation_count)
        self.assertEqual("data_incomplete", deferred.block_reason)

    def test_actual_catastrophe_requires_opening_equity(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(
                str(Path(td) / "test.db"),
                storage.DISCOVERY_SCHEMA,
                storage.OBSERVE_SCHEMA,
            )
            db.executemany(
                "INSERT INTO copy_position "
                "(addr,coin,status,realized_pnl,was_liq,opening_account_equity,closed_at) "
                "VALUES ('0xabc',?,?,?,?,?,?)",
                [
                    ("BTC", "liquidated", -900.0, 1, None, "2026-07-29T00:00:00Z"),
                    ("ETH", "liquidated", -900.0, 1, 10_000.0, "2026-07-29T00:00:00Z"),
                ],
            )
            evidence = wallet_risk.actual_copy_evidence(db, "0xabc")
            self.assertEqual(1, len(evidence["catastrophicPositionIds"]))
            db.close()

    def test_actual_copy_accumulates_repeated_losses_like_current_core_three(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(
                str(Path(td) / "test.db"),
                storage.DISCOVERY_SCHEMA,
                storage.OBSERVE_SCHEMA,
            )
            db.executemany(
                "INSERT INTO copy_position "
                "(addr,coin,status,realized_pnl,unrealized_pnl,was_liq,"
                "opening_account_equity,opened_at,closed_at) "
                "VALUES ('0xcore3',?,?,?,?,?,?,?,?)",
                [
                    (
                        "xyz:MU", "liquidated", -257.36531661368434, 0.0, 1,
                        9752.091812184486, "2026-07-30T13:35:00Z",
                        "2026-07-30T14:25:00Z",
                    ),
                    (
                        "xyz:AMD", "liquidated", -251.60749524501858, 0.0, 1,
                        9750.321705330909, "2026-07-30T14:30:00Z",
                        "2026-07-30T15:10:00Z",
                    ),
                    (
                        "xyz:AMD", "closed", -25.034201642017543, 0.0, 0,
                        11690.026809727655, "2026-07-30T16:00:00Z",
                        "2026-07-30T17:00:00Z",
                    ),
                    (
                        "xyz:AMD", "tail_closed", 39.19492591441556, 0.0, 0,
                        11523.895501961417, "2026-07-30T18:00:00Z",
                        "2026-07-30T19:00:00Z",
                    ),
                    (
                        "xyz:BRENTOIL", "open", 0.0, -23.432738485902878, 0,
                        11588.130939802752, "2026-07-30T20:00:00Z", None,
                    ),
                ],
            )
            evidence = wallet_risk.actual_copy_evidence(
                db, "0xcore3",
                now=datetime(2026, 7, 31, tzinfo=timezone.utc),
            )
            self.assertEqual(4, evidence["closedN30d"])
            self.assertAlmostEqual(-518.2448260722078, evidence["conservativePnl30d"])
            self.assertAlmostEqual(
                0.0531419142, evidence["cumulativeLossPct30d"], places=7,
            )
            reason = wallet_risk.actual_copy_reason(evidence)
            self.assertEqual(
                "actual_copy_30d_conservative_pnl_not_positive", reason,
            )
            assessment = wallet_risk.advance(
                assessed_at="2026-07-31T08:00:00Z", reason=reason,
            )
            self.assertEqual(wallet_risk.MEDIUM, assessment.level)
            self.assertTrue(assessment.entry_allowed)
            db.close()

    def test_cumulative_high_recovers_after_subsequent_profit(self):
        reason = wallet_risk.actual_copy_reason({
            "closedN30d": 4,
            "closedPnl30d": -850.0,
            "conservativePnl30d": -850.0,
            "cumulativeLossPct30d": 0.085,
            "catastrophicPositionIds": [],
            "openUnrealized": 0.0,
        })
        self.assertEqual("actual_copy_cumulative_loss_over_8pct", reason)
        high = wallet_risk.advance(
            assessed_at="2026-07-31T00:00:00Z", reason=reason,
        )
        self.assertEqual(wallet_risk.HIGH, high.level)

        medium_reason = wallet_risk.actual_copy_reason({
            "closedN30d": 6,
            "closedPnl30d": -500.0,
            "conservativePnl30d": -500.0,
            "cumulativeLossPct30d": 0.05,
            "catastrophicPositionIds": [],
            "openUnrealized": 0.0,
        })
        lowered = wallet_risk.advance(
            previous_level=high.level,
            previous_count=high.confirmation_count,
            previous_reasons=high.reasons,
            previous_first_confirmed_at=high.first_confirmed_at,
            assessed_at="2026-08-02T00:00:00Z",
            reason=medium_reason,
        )
        self.assertEqual(wallet_risk.MEDIUM, lowered.level)

        recovered = wallet_risk.advance(
            previous_level=lowered.level,
            previous_count=lowered.confirmation_count,
            previous_reasons=lowered.reasons,
            previous_first_confirmed_at=lowered.first_confirmed_at,
            assessed_at="2026-08-04T00:00:00Z",
            reason=None,
        )
        self.assertEqual(wallet_risk.NORMAL, recovered.level)

    def test_risk_owned_execution_control_is_released_on_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(
                str(Path(td) / "test.db"),
                storage.DISCOVERY_SCHEMA,
                storage.OBSERVE_SCHEMA,
            )
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g1','published',1,1,1,'now','now')"
            )
            db.execute(
                "INSERT INTO follow_selection "
                "(generation,addr,role,enabled,selected_at) "
                "VALUES ('g1','0xrecover','core',1,'now')"
            )
            high = wallet_risk.advance(
                assessed_at="2026-07-31T00:00:00Z",
                reason="actual_copy_cumulative_loss_over_8pct",
            )
            wallet_risk.sync_execution_control(db, "0xrecover", high)
            self.assertEqual(
                (0, "requalify", "high_risk_override"),
                tuple(db.execute(
                    "SELECT enabled,intent,intent_resolution FROM target_controls "
                    "WHERE addr='0xrecover'"
                ).fetchone()),
            )
            recovered = wallet_risk.advance(
                previous_level=high.level,
                previous_count=high.confirmation_count,
                previous_reasons=high.reasons,
                previous_first_confirmed_at=high.first_confirmed_at,
                assessed_at="2026-08-04T00:00:00Z",
                reason=None,
            )
            wallet_risk.sync_execution_control(db, "0xrecover", recovered)
            self.assertEqual(
                (1, "active", "cumulative_risk_recovered"),
                tuple(db.execute(
                    "SELECT enabled,intent,intent_resolution FROM target_controls "
                    "WHERE addr='0xrecover'"
                ).fetchone()),
            )
            db.execute(
                "UPDATE target_controls SET enabled=0,intent='requalify',"
                "intent_resolution='operator_disabled' WHERE addr='0xrecover'"
            )
            wallet_risk.sync_execution_control(db, "0xrecover", recovered)
            self.assertEqual(
                (0, "requalify", "operator_disabled"),
                tuple(db.execute(
                    "SELECT enabled,intent,intent_resolution FROM target_controls "
                    "WHERE addr='0xrecover'"
                ).fetchone()),
            )
            db.close()
