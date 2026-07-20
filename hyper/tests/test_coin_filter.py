import unittest

from hyper.coin_filter import coin_is_blacklisted, coin_is_blocked, is_korean_stock, parse_coin_blacklist


class CoinFilterTests(unittest.TestCase):
    def test_parse_blacklist_normalizes_case_and_separators(self):
        coins = parse_coin_blacklist(" xyz:shkx, BTC\neth  ,")

        self.assertEqual(coins, {"XYZ:SHKX", "BTC", "ETH"})
        self.assertTrue(coin_is_blacklisted("xyz:SHKX", coins))
        self.assertTrue(coin_is_blacklisted("btc", coins))
        self.assertFalse(coin_is_blacklisted("SOL", coins))

    def test_korean_stock_preset_is_explicit_and_additive(self):
        self.assertTrue(is_korean_stock("xyz:ewy"))
        self.assertTrue(is_korean_stock("XYZ:SKHX"))
        self.assertFalse(is_korean_stock("xyz:MU"))
        self.assertTrue(coin_is_blocked("xyz:ewy", set(), block_korean_stocks=True))
        self.assertFalse(coin_is_blocked("xyz:ewy", set(), block_korean_stocks=False))


if __name__ == "__main__":
    unittest.main()
