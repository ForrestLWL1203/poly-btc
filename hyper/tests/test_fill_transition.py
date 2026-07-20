import unittest

from hyper.fill_transition import classify_fill_transition


class FillTransitionTests(unittest.TestCase):
    def test_classifies_open_add_reduce_close_and_flip(self):
        cases = [
            (0.0, 1.0, "open"),
            (1.0, 2.0, "add"),
            (2.0, 1.0, "reduce"),
            (1.0, 0.0, "close"),
            (1.0, -1.0, "flip"),
            (-1.0, 1.0, "flip"),
        ]

        for pos0, pos1, expected in cases:
            with self.subTest(pos0=pos0, pos1=pos1):
                self.assertEqual(classify_fill_transition(pos0, pos1), expected)


if __name__ == "__main__":
    unittest.main()
