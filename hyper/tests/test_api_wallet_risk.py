import sqlite3
import tempfile
from pathlib import Path
import unittest

from dashboard.api.wallets import ep_wallets
from hyper import storage


class WalletRiskApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = storage.connect(
            str(Path(self.tmp.name) / "test.db"),
            storage.DISCOVERY_SCHEMA,
            storage.OBSERVE_SCHEMA,
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,is_current,started_at,published_at) "
            "VALUES ('g1','published',1,1,'2026-07-30T00:00:00Z','2026-07-30T01:00:00Z')"
        )
        for rank, addr in enumerate(("0xactive", "0xexit"), 1):
            self.db.execute(
                "INSERT INTO follow_selection "
                "(generation,addr,role,enabled,selection_rank,data_status,selected_at) "
                "VALUES ('g1',?,'core',1,?,'valid','2026-07-30T01:00:00Z')",
                (addr, rank),
            )
            self.db.execute(
                "INSERT INTO wallet_registry "
                "(addr,state,first_seen_at,last_seen_at,risk_level,risk_reasons_json,"
                "risk_confirmation_count,risk_first_confirmed_at,risk_assessed_at,updated_at) "
                "VALUES (?,'qualified','2026-01-01T00:00:00Z','2026-07-30T00:00:00Z',"
                "'low','[\"latest_7d_inactive\"]',1,'2026-07-27T00:00:00Z',"
                "'2026-07-30T00:00:00Z','2026-07-30T00:00:00Z')",
                (addr,),
            )
        self.db.execute(
            "INSERT INTO target_controls "
            "(addr,enabled,intent,intent_requested_at,updated_at) "
            "VALUES ('0xactive',1,'active',NULL,'2026-07-30T00:00:00Z')"
        )
        self.db.execute(
            "INSERT INTO target_controls "
            "(addr,enabled,intent,intent_requested_at,updated_at) "
            "VALUES ('0xexit',0,'requalify','2026-07-30T00:30:00Z','2026-07-30T00:30:00Z')"
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_effective_role_projects_requalify_into_challenger(self):
        followed = ep_wallets(self.db, {"tab": ["followed"], "size": ["20"]})
        self.assertEqual(1, followed["total"])
        wallet = followed["wallets"][0]
        self.assertEqual("0xactive", wallet["address"])
        self.assertEqual("low", wallet["riskLevel"])
        self.assertEqual(["latest_7d_inactive"], wallet["riskReasons"])
        self.assertEqual("active", wallet["operatorIntent"])
        self.assertTrue(wallet["entryAllowed"])

        challenger = ep_wallets(self.db, {"tab": ["challenger"], "size": ["20"]})
        self.assertEqual(1, challenger["total"])
        wallet = challenger["wallets"][0]
        self.assertEqual("0xexit", wallet["address"])
        self.assertEqual("challenger", wallet["effectiveRole"])
        self.assertEqual("core", wallet["publishedRole"])
        self.assertEqual("requalify", wallet["operatorIntent"])
        self.assertFalse(wallet["entryAllowed"])
