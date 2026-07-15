import asyncio
import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hl import storage
from hl.credentials import CredentialStore, decrypt_envelope, public_wrap_key
from hl.risk_radar import RiskRadar, candle_features, orderbook_features, validate_model_output


class RiskRadarTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.db = storage.connect(
            str(Path(self.td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA
        )
        self.db.row_factory = sqlite3.Row
        self.radar = RiskRadar(self.db)

    def _envelope(self, secret):
        wrap = public_wrap_key(self.db)
        public = serialization.load_der_public_key(base64.b64decode(wrap["spki"]))
        dek = AESGCM.generate_key(bit_length=256)
        nonce = b"123456789012"
        ciphertext = AESGCM(dek).encrypt(nonce, secret.encode(), None)
        wrapped = public.encrypt(
            dek, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        )
        return {"envelopeVersion": 1, "keyId": wrap["keyId"],
                "wrappedKey": base64.b64encode(wrapped).decode(),
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ciphertext).decode()}

    def test_browser_compatible_envelope_round_trip_persists_no_plaintext(self):
        envelope = self._envelope("sk-sensitive-test")
        self.assertEqual(decrypt_envelope(self.db, envelope), "sk-sensitive-test")
        CredentialStore(self.db).save_envelope("deepseek", envelope)
        row = self.db.execute("SELECT * FROM provider_credential").fetchone()
        self.assertNotIn("sk-sensitive-test", " ".join(str(v) for v in row))
        self.assertEqual(CredentialStore(self.db).secret("deepseek"), "sk-sensitive-test")

    def test_invalid_replacement_keeps_previous_credential(self):
        store = CredentialStore(self.db)
        store.save_envelope("deepseek", self._envelope("sk-working"))
        with patch("hl.risk_radar.DeepSeekClient.balance", side_effect=RuntimeError("auth failed")):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.radar.install_credential(self._envelope("sk-invalid")))
        self.assertEqual(store.secret("deepseek"), "sk-working")

    def test_two_consecutive_strong_assessments_confirm_shadow_block(self):
        features = {"asOfMs": 1, "markets": {
            coin: {interval: {"available": True, "emaStack": 1, "macdHistogramPct": .01, "rsi14": 62}
                   for interval in ("15m", "1h", "4h")} for coin in ("BTC", "ETH")
        }, "context": {}, "local": {"bullishScore": 90, "evidenceGroups": ["trend"]}}
        self.radar.mode = "shadow"
        self.radar._set_state(mode="shadow", status="running")
        self.radar.credentials.secret = lambda _provider: "dummy"
        self.radar.build_features = lambda: (features, {"present": 6, "expected": 6, "ratio": 1})

        def assess(_client, _features):
            return ({"bullish_score": 82, "confidence": 100, "regime": "up", "reason": "trend",
                     "evidence": ["trend"], "invalidating_conditions": ["reversal"]},
                    {"choices": []}, 10, {"prompt_tokens": 1000, "completion_tokens": 100})

        with patch("hl.risk_radar.DeepSeekClient.assess", assess), patch("hl.risk_radar.now_ms", return_value=1_800_000):
            asyncio.run(self.radar.assess_once())
        with patch("hl.risk_radar.DeepSeekClient.assess", assess), patch("hl.risk_radar.now_ms", return_value=2_700_000):
            asyncio.run(self.radar.assess_once())

        rows = self.db.execute(
            "SELECT active_block,block_side,confirmation_mode FROM market_risk_assessment ORDER BY assessment_id"
        ).fetchall()
        self.assertEqual(tuple(rows[0]), (0, "short", None))
        self.assertEqual(tuple(rows[1]), (1, "short", "steady"))

    def test_intent_decision_is_immutable_and_resolves_once(self):
        self.radar.mode = "shadow"
        future = 9_999_999_999_999
        self.db.execute("INSERT INTO market_risk_assessment (assessment_id,assessed_for_ms,status,bullish_score,"
                        "bearish_score,block_side,active_block,created_at) VALUES (1,1,'ok',80,20,'short',1,'now')")
        self.radar._set_state(mode="shadow", status="running", current_assessment_id=1,
                              block_side="short", risk_score=80, confirmation_mode="steady", valid_until_ms=future)
        cur = self.db.execute("INSERT INTO copy_position (addr,coin,side,status,entry_px,size,rem_size,realized_pnl,opened_at) "
                              "VALUES ('0xa','BTC','short','open',100,1,1,0,'now')")
        pos_id = cur.lastrowid
        self.db.commit()
        self.radar.record_intent(pos_id, "0xa", "BTC", "short")
        self.radar._set_state(block_side=None, risk_score=50, confirmation_mode=None)
        self.radar.record_intent(pos_id, "0xa", "BTC", "short")
        row = self.db.execute("SELECT would_block,risk_score FROM market_risk_intent WHERE pos_id=?", (pos_id,)).fetchone()
        self.assertEqual(tuple(row), (1, 80))
        self.db.execute("UPDATE copy_position SET status='closed',realized_pnl=-12,closed_at='later' WHERE pos_id=?", (pos_id,))
        self.db.commit()
        self.assertTrue(self.radar.resolve_intent(pos_id))
        self.assertTrue(self.radar.resolve_intent(pos_id))
        row = self.db.execute("SELECT status,outcome,net_pnl FROM market_risk_intent WHERE pos_id=?", (pos_id,)).fetchone()
        self.assertEqual(tuple(row), ("resolved", "avoided_loss", -12))

    def test_indicator_features_cover_macd_bollinger_rsi_and_atr(self):
        rows = []
        for i in range(80):
            close = 100 + i * .4
            rows.append({"o": close - .1, "h": close + 1, "l": close - 1, "c": close, "v": 1000 + i})
        features = candle_features(rows)
        self.assertTrue(features["available"])
        for key in ("emaStack", "macdPct", "macdHistogramPct", "rsi14", "atrPct", "bollingerZ", "volumeZ"):
            self.assertIn(key, features)

    def test_orderbook_features_include_depth_imbalance_spread_and_microprice(self):
        book = {"levels": [[{"px": "99.9", "sz": "4"}, {"px": "99.8", "sz": "2"}],
                           [{"px": "100.1", "sz": "1"}, {"px": "100.2", "sz": "2"}]]}
        features = orderbook_features(book)
        self.assertTrue(features["available"])
        self.assertGreater(features["imbalance10bps"], 0)
        for key in ("spreadBps", "bidDepth10bps", "askDepth10bps", "micropriceOffsetBps"):
            self.assertIn(key, features)

    def test_invalid_model_output_fails_closed_to_no_assessment(self):
        with self.assertRaises(ValueError):
            validate_model_output({"bullish_score": 150, "confidence": "high"})


if __name__ == "__main__":
    unittest.main()
