import sqlite3
import tempfile
import unittest
from pathlib import Path

from hl import storage
from hl.api_positions import ep_positions
from hl.api_risk import ep_connections, ep_credential_wrap_key, ep_risk_radar, ep_risk_thresholds
from hl.risk_radar import RiskRadar


class RiskApiTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.db = storage.connect(str(Path(self.td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        self.db.row_factory = sqlite3.Row
        RiskRadar(self.db)

    def test_read_models_never_expose_credential_ciphertext(self):
        payload = ep_connections(self.db)
        self.assertEqual(payload["deepseek"]["configured"], False)
        self.assertNotIn("ciphertext", str(payload).lower())
        self.assertTrue(ep_credential_wrap_key(self.db)["ready"])

    def test_position_projection_includes_entry_time_shadow_label(self):
        cur = self.db.execute("INSERT INTO copy_position (addr,coin,side,status,entry_px,leverage,margin,notional,size,"
                              "rem_size,mark_px,unrealized_pnl,opened_at) VALUES ('0xa','BTC','long','open',100,2,50,100,1,1,90,-10,'now')")
        pos_id = cur.lastrowid
        self.db.execute("INSERT INTO market_risk_intent (pos_id,addr,coin,side,risk_score,would_block,confirmation_mode,"
                        "opened_at,status) VALUES (?,?,?,?,80,1,'steady','now','open')", (pos_id, "0xa", "BTC", "long"))
        self.db.commit()
        row = ep_positions(self.db, {"status": ["open"]})["positions"][0]
        self.assertEqual(row["shadowRisk"]["wouldBlock"], True)
        self.assertEqual(row["shadowRisk"]["riskScore"], 80)

    def test_summary_and_threshold_comparison_use_resolved_intents(self):
        self.db.execute("INSERT INTO market_risk_assessment (assessment_id,assessed_for_ms,status,bullish_score,bearish_score,"
                        "block_side,active_block,created_at) VALUES (1,1,'ok',20,80,'long',1,'now')")
        cur = self.db.execute("INSERT INTO copy_position (addr,coin,side,status,entry_px,realized_pnl,opened_at,closed_at) "
                              "VALUES ('0xa','ETH','long','closed',100,-25,'a','b')")
        self.db.execute("INSERT INTO market_risk_intent (pos_id,addr,coin,side,assessment_id,risk_score,would_block,opened_at,"
                        "status,net_pnl,outcome) VALUES (?,?,?,?,1,80,1,'a','resolved',-25,'avoided_loss')",
                        (cur.lastrowid, "0xa", "ETH", "long"))
        self.db.commit()
        self.assertEqual(ep_risk_radar(self.db)["summary"]["hypotheticalNetBenefit"], 25)
        at_75 = next(x for x in ep_risk_thresholds(self.db)["comparison"] if x["threshold"] == 75)
        self.assertEqual(at_75["wouldBlock"], 1)
        self.assertEqual(at_75["avoidedLosses"], 1)


if __name__ == "__main__":
    unittest.main()
