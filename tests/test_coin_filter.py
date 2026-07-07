import unittest

from hl.coin_filter import coin_is_blacklisted, parse_coin_blacklist


class CoinFilterTests(unittest.TestCase):
    def test_parse_blacklist_normalizes_case_and_separators(self):
        coins = parse_coin_blacklist(" xyz:shkx, BTC\neth  ,")

        self.assertEqual(coins, {"XYZ:SHKX", "BTC", "ETH"})
        self.assertTrue(coin_is_blacklisted("xyz:SHKX", coins))
        self.assertTrue(coin_is_blacklisted("btc", coins))
        self.assertFalse(coin_is_blacklisted("SOL", coins))


if __name__ == "__main__":
    unittest.main()
