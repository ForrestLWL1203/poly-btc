import unittest
from unittest.mock import patch

from hyper import storage
from hyper.market import rest, volatility


class VolatilityMetadataTests(unittest.TestCase):
    def setUp(self):
        self.db = storage.connect(":memory:", storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)

    def tearDown(self):
        self.db.close()

    def test_builder_asset_context_uses_its_dex(self):
        with patch.object(
            rest, "asset_contexts",
            return_value={"PLTR": {"universe_maxLeverage": 10}},
        ) as contexts:
            row = rest.asset_context("xyz:PLTR")

        contexts.assert_called_once_with("xyz")
        self.assertEqual(row["universe_maxLeverage"], 10)

    def test_refresh_persists_builder_max_leverage_and_never_erases_it_with_null(self):
        sample = (0.07, 0.06, 0.05, 32)
        context = {
            "dayNtlVlm": "1000000", "openInterest": "1000", "markPx": "170",
            "universe_maxLeverage": 10,
        }
        with patch.object(volatility, "compute", return_value=sample), \
                patch.object(rest, "asset_context", return_value=context) as asset_context:
            volatility.refresh(self.db, "xyz:PLTR")

        asset_context.assert_called_once_with("xyz:PLTR")
        row = self.db.execute(
            "SELECT sigma,max_leverage,margin_meta_updated_at FROM coin_vol WHERE coin=?",
            ("xyz:PLTR",),
        ).fetchone()
        self.assertAlmostEqual(row[0], 0.07)
        self.assertEqual(row[1], 10)
        self.assertIsNotNone(row[2])

        with patch.object(volatility, "compute", return_value=(0.08, 0.08, 0.05, 32)), \
                patch.object(rest, "asset_context", return_value=None):
            volatility.refresh(self.db, "xyz:PLTR")

        row = self.db.execute(
            "SELECT sigma,max_leverage FROM coin_vol WHERE coin=?", ("xyz:PLTR",),
        ).fetchone()
        self.assertAlmostEqual(row[0], 0.08)
        self.assertEqual(row[1], 10)


if __name__ == "__main__":
    unittest.main()
