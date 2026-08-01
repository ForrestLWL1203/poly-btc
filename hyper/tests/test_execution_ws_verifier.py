import unittest

from hyper.execution.ws_verifier import _contains_oid


class WebsocketVerifierTests(unittest.TestCase):
    def test_nested_order_and_fill_messages_find_oid(self):
        self.assertTrue(_contains_oid({"order": {"oid": 17}}, 17))
        self.assertTrue(_contains_oid({"fills": [{"oid": "18"}]}, 18))
        self.assertFalse(_contains_oid({"order": {"oid": 19}}, 18))


if __name__ == "__main__":
    unittest.main()
