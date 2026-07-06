import unittest

from hl.follow_score import choose_follow_line, compute_follow_score


class FollowScoreTests(unittest.TestCase):
    def test_falls_back_to_raw_score_without_copy_backtest(self):
        score, detail = compute_follow_score({"score": 0.73})

        self.assertAlmostEqual(score, 0.73)
        self.assertEqual(detail["rawScore"], 0.73)
        self.assertIn("暂无copy回测", detail["reasons"][0])

    def test_copy_stronger_wallet_beats_slightly_higher_raw_wallet(self):
        weak = {
            "score": 0.674,
            "copy_bt_net_pnl": 1300.3,
            "copy_bt_14d_net_pnl": 28.3,
            "copy_bt_7d_net_pnl": 889.6,
            "copy_bt_closed_n": 46,
            "copy_bt_14d_closed_n": 30,
            "copy_bt_7d_closed_n": 8,
            "copy_bt_open_fill_rate": 1.0,
        }
        strong = {
            "score": 0.667,
            "copy_bt_net_pnl": 2270.8,
            "copy_bt_14d_net_pnl": 1395.1,
            "copy_bt_7d_net_pnl": 1446.3,
            "copy_bt_closed_n": 41,
            "copy_bt_14d_closed_n": 22,
            "copy_bt_7d_closed_n": 17,
            "copy_bt_open_fill_rate": 1.0,
        }

        weak_score, weak_detail = compute_follow_score(weak)
        strong_score, strong_detail = compute_follow_score(strong)

        self.assertGreater(strong_score, weak_score)
        self.assertIn("30/14/7天copy均为正", strong_detail["reasons"])
        self.assertLess(strong_detail["rawScore"], weak_detail["rawScore"])

    def test_thin_7d_sample_is_demoted_even_with_big_30d_profit(self):
        thin = {
            "score": 0.642,
            "copy_bt_net_pnl": 6692.9,
            "copy_bt_14d_net_pnl": 6172.7,
            "copy_bt_7d_net_pnl": 407.5,
            "copy_bt_closed_n": 23,
            "copy_bt_14d_closed_n": 10,
            "copy_bt_7d_closed_n": 1,
            "copy_bt_open_fill_rate": 1.0,
        }
        solid = {
            "score": 0.667,
            "copy_bt_net_pnl": 2270.8,
            "copy_bt_14d_net_pnl": 1395.1,
            "copy_bt_7d_net_pnl": 1446.3,
            "copy_bt_closed_n": 41,
            "copy_bt_14d_closed_n": 22,
            "copy_bt_7d_closed_n": 17,
            "copy_bt_open_fill_rate": 1.0,
        }

        thin_score, thin_detail = compute_follow_score(thin)
        solid_score, _ = compute_follow_score(solid)

        self.assertLess(thin_score, solid_score)
        self.assertTrue(any("7天样本偏少" in r for r in thin_detail["reasons"]))

    def test_recent_copy_loss_demotes_score(self):
        score, detail = compute_follow_score({
            "score": 0.72,
            "copy_bt_net_pnl": 1000.0,
            "copy_bt_14d_net_pnl": -120.0,
            "copy_bt_7d_net_pnl": -40.0,
            "copy_bt_closed_n": 30,
            "copy_bt_14d_closed_n": 10,
            "copy_bt_7d_closed_n": 4,
            "copy_bt_open_fill_rate": 1.0,
        })

        self.assertLess(score, 0.72)
        self.assertTrue(any("近期copy亏损" in r for r in detail["reasons"]))

    def test_choose_follow_line_cuts_before_quality_cliff(self):
        ranked = [{"follow_score": s} for s in (0.90, 0.86, 0.83, 0.80, 0.70, 0.68)]

        choice = choose_follow_line(ranked, min_score=0.50, min_n=3, target_n=5, max_n=6, cliff_gap=0.08)

        self.assertEqual(choice["reason"], "quality_cliff")
        self.assertEqual(choice["count"], 4)
        self.assertAlmostEqual(choice["line"], 0.80)

    def test_choose_follow_line_uses_capacity_when_quality_is_flat(self):
        ranked = [{"follow_score": s} for s in (0.90, 0.88, 0.86, 0.84, 0.82, 0.80)]

        choice = choose_follow_line(ranked, min_score=0.50, min_n=3, target_n=5, max_n=6, cliff_gap=0.05)

        self.assertEqual(choice["reason"], "capacity_cap")
        self.assertEqual(choice["count"], 5)
        self.assertAlmostEqual(choice["line"], 0.82)


if __name__ == "__main__":
    unittest.main()
