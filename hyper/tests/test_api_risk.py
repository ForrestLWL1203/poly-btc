import sqlite3
import tempfile
import unittest
from pathlib import Path

from hyper import storage
from dashboard.api.positions import ep_positions
from dashboard.api.risk import ep_connections, ep_credential_wrap_key, ep_risk_intents, ep_risk_radar, ep_risk_thresholds
from hyper.execution.risk_radar import RiskRadar


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

    def test_assessment_trail_is_server_paginated_and_keeps_current_projection(self):
        for assessment_id in range(1, 6):
            self.db.execute(
                "INSERT INTO market_risk_assessment (assessment_id,assessed_for_ms,status,bullish_score,"
                "bearish_score,active_block,created_at) VALUES (?,?, 'ok',?,?,0,'now')",
                (assessment_id, assessment_id, 50 + assessment_id, 50 - assessment_id),
            )
        self.db.execute("UPDATE market_risk_state SET current_assessment_id=5 WHERE id=1")
        self.db.commit()

        payload = ep_risk_radar(self.db, {"assessmentPage": ["1"], "assessmentSize": ["2"]})

        self.assertEqual([row["id"] for row in payload["assessments"]], [3, 2])
        self.assertEqual(payload["currentAssessment"]["id"], 5)
        self.assertEqual(payload["assessmentPagination"], {
            "page": 1, "size": 2, "total": 5, "totalPages": 3,
            "retentionLimit": 192, "retentionHours": 48,
        })

    def test_v2_summary_and_projection_compare_action_filtered_ledger(self):
        self.db.execute("INSERT INTO market_risk_assessment (assessment_id,assessed_for_ms,status,bullish_score,bearish_score,"
                        "block_side,active_block,created_at) VALUES (2,2,'ok',20,80,'long',1,'now')")
        pos_id = self.db.execute("INSERT INTO copy_position (addr,coin,side,status,entry_px,realized_pnl,opened_at,closed_at) "
                                 "VALUES ('0xb','BTC','long','closed',100,-100,'a','b')").lastrowid
        self.db.execute("INSERT INTO market_risk_intent (pos_id,addr,coin,side,assessment_id,risk_score,would_block,"
                        "opened_at,status,net_pnl,outcome) VALUES (?,?,?,?,2,80,1,'a','resolved',-100,'avoided_loss')",
                        (pos_id, "0xb", "BTC", "long"))
        episode_id = self.db.execute("INSERT INTO market_risk_episode (pos_id,addr,coin,side,status,entry_blocked,"
                                     "delayed_entry,blocked_entries,allowed_entries,baseline_net_pnl,shadow_net_pnl,"
                                     "net_benefit,outcome,opened_at,resolved_at) "
                                     "VALUES (?,?,?,?, 'resolved',1,1,1,1,-100,20,120,'improved','a','b')",
                                     (pos_id, "0xb", "BTC", "long")).lastrowid
        self.db.execute("INSERT INTO market_risk_action (episode_id,pos_id,decision_group,action,side,assessment_id,"
                        "risk_score,would_block,decision,baseline_qty_delta,baseline_px,created_at) "
                        "VALUES (?,?,?,'open','long',2,80,1,'blocked_open',1,100,'a')",
                        (episode_id, pos_id, "open:oid:1"))
        self.db.execute("INSERT INTO market_risk_action (episode_id,pos_id,decision_group,action,side,risk_score,"
                        "would_block,decision,baseline_qty_delta,baseline_px,shadow_qty_delta,shadow_px,created_at) "
                        "VALUES (?,?,?,'add','long',60,0,'delayed_entry',1,90,1,90,'a')",
                        (episode_id, pos_id, "add:oid:2"))
        self.db.execute("INSERT INTO market_risk_action (episode_id,pos_id,decision_group,action,side,would_block,"
                        "decision,baseline_qty_delta,baseline_px,shadow_qty_delta,shadow_px,close_fraction,created_at) "
                        "VALUES (?,?,?,'close','long',0,'mandatory_exit',-2,110,-1,110,1,'b')",
                        (episode_id, pos_id, "close:act:3"))
        self.db.commit()

        summary = ep_risk_radar(self.db)["summary"]
        self.assertEqual(summary["accountingVersion"], 2)
        self.assertEqual(summary["delayedEntries"], 1)
        self.assertEqual(summary["hypotheticalNetBenefit"], 120)
        intent = ep_risk_intents(self.db, {"limit": [10]})["intents"][0]
        self.assertEqual(intent["shadow"]["netBenefit"], 120)
        self.assertEqual([a["decision"] for a in intent["actions"]],
                         ["blocked_open", "delayed_entry", "mandatory_exit"])
        at_90 = next(x for x in ep_risk_thresholds(self.db)["comparison"] if x["threshold"] == 90)
        self.assertEqual(at_90["wouldBlock"], 0)
        self.assertAlmostEqual(at_90["hypotheticalNetBenefit"], 0)

    def test_shadow_intents_are_server_paginated(self):
        for n in range(7):
            pos_id = self.db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,opened_at) VALUES (?,?,?,?,?)",
                (f"0x{n}", "BTC", "long", "open", f"t{n}"),
            ).lastrowid
            self.db.execute(
                "INSERT INTO market_risk_intent (pos_id,addr,coin,side,opened_at,status) "
                "VALUES (?,?,?,?,?,'open')",
                (pos_id, f"0x{n}", "BTC", "long", f"t{n}"),
            )
        self.db.commit()

        payload = ep_risk_intents(self.db, {"page": ["1"], "size": ["5"]})

        self.assertEqual(len(payload["intents"]), 2)
        self.assertEqual(payload["pagination"], {
            "page": 1, "size": 5, "total": 7, "totalPages": 2, "affectedOnly": False,
        })

    def test_shadow_intents_can_hide_pure_pass_through_rows(self):
        for n, blocked in enumerate((0, 1)):
            pos_id = self.db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,opened_at) VALUES (?,?,?,?,?)",
                (f"0xfilter{n}", "ETH", "short", "open", f"t{n}"),
            ).lastrowid
            self.db.execute(
                "INSERT INTO market_risk_intent (pos_id,addr,coin,side,would_block,opened_at,status) "
                "VALUES (?,?,?,?,?,?,'open')",
                (pos_id, f"0xfilter{n}", "ETH", "short", blocked, f"t{n}"),
            )
        self.db.commit()

        payload = ep_risk_intents(self.db, {"affectedOnly": ["1"], "size": ["5"]})

        self.assertEqual([row["wallet"] for row in payload["intents"]], ["0xfilter1"])
        self.assertEqual(payload["pagination"]["total"], 1)
        self.assertTrue(payload["pagination"]["affectedOnly"])


if __name__ == "__main__":
    unittest.main()
