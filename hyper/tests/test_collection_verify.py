import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hyper.cli import collection_verify


class _ParityClients:
    def __init__(self, _endpoint):
        pass

    @staticmethod
    def _response(body):
        kind = body["type"]
        if kind == "meta":
            return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}
        if kind == "portfolio":
            return [["day", {"accountValueHistory": [[1, "100"]], "pnlHistory": []}]]
        if kind == "userFillsByTime":
            return [{
                "tid": 1, "time": 1, "coin": "BTC", "side": "B", "px": "100",
                "sz": "1", "startPosition": "0", "dir": "Open Long",
                "closedPnl": "0", "fee": "0.01", "feeToken": "USDC",
                "crossed": True, "hash": "0xhash", "oid": 2,
            }]
        if kind == "clearinghouseState":
            return {"assetPositions": [{"position": {"coin": "BTC", "szi": "1"}}]}
        if kind == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "total": "100", "hold": "0"}]}
        raise AssertionError(kind)

    def official(self, body):
        return self._response(body)

    def quicknode(self, body):
        return self._response(body)


class CollectionParityTests(unittest.TestCase):
    def test_aggregate_report_never_contains_endpoint_or_core_addresses(self):
        addresses = [f"0x{index:040x}" for index in range(1, 6)]
        endpoint = "https://secret-token.quiknode.pro/private/info"
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(collection_verify, "read_quicknode_endpoint", return_value=endpoint), \
                patch.object(collection_verify, "published_core_addrs", return_value=addresses), \
                patch.object(collection_verify, "_Clients", _ParityClients):
            report = collection_verify.run_parity(str(Path(temp) / "hl.db"))

        encoded = json.dumps(report, sort_keys=True)
        self.assertTrue(report["ok"])
        self.assertNotIn("quiknode.pro", encoded)
        for address in addresses:
            self.assertNotIn(address, encoded)


if __name__ == "__main__":
    unittest.main()
