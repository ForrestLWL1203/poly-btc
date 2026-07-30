import tempfile
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
