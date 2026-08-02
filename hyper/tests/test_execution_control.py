import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account import Account

from hyper import storage
from hyper.util import now_iso
from hyper.execution import control
from hyper.execution.command_worker import execute_control_command
from hyper.execution.credentials import (
    ENVELOPE_ALGORITHM, credential_aad, generate_wrap_keypair, public_wrap_key_payload,
)
from hyper.execution.live_preflight import activate_live_session, run_live_preflight, unlock_live_canary


ACCOUNT = "0x" + "2" * 40


class FakeInfo:
    def __init__(self, agent, *, total="8000"):
        self.agent = agent.lower()
        self.total = total

    def user_role(self, address):
        if str(address).lower() == self.agent:
            return {"role": "agent", "data": {"user": ACCOUNT.lower()}}
        return {"role": "user"}

    def query_user_abstraction_state(self, _account):
        return "unifiedAccount"

    def spot_user_state(self, _account):
        return {"balances": [{"coin": "USDC", "total": self.total, "hold": "0"}]}

    def user_state(self, _account, dex=""):
        return {"assetPositions": [], "marginSummary": {"accountValue": "0"}}

    def open_orders(self, _account, dex=""):
        return []

    def frontend_open_orders(self, _account, dex=""):
        return []

    def extra_agents(self, _account):
        return [{
            "name": "copy-agent",
            "address": self.agent,
            "validUntil": 4_102_444_800_000,
        }]

    def perp_dexs(self):
        return [{"name": ""}, {"name": "xyz"}]

    def meta(self, dex=""):
        return {"universe": [{"name": "xyz:XYZ100" if dex else "BTC", "szDecimals": 5, "maxLeverage": 20}]}


def envelope(public_path, wallet):
    from cryptography.hazmat.primitives import serialization

    public = serialization.load_pem_public_key(Path(public_path).read_bytes())
    aes, iv = os.urandom(32), os.urandom(12)
    ciphertext = AESGCM(aes).encrypt(
        iv, wallet.key.hex().encode(), credential_aad("mainnet", ACCOUNT, wallet.address),
    )
    wrapped = public.encrypt(
        aes, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return {
        "version": 1, "algorithm": ENVELOPE_ALGORITHM,
        "wrapKeyId": public_wrap_key_payload(public_path)["wrapKeyId"],
        "wrappedKey": base64.b64encode(wrapped).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


class ExecutionControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = Path(self.temp.name) / "private.pem"
        self.public = Path(self.temp.name) / "public.pem"
        generate_wrap_keypair(self.private, self.public)
        self.wallet = Account.create()
        self.db = storage.connect(":memory:", storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        control.store_encrypted_credential(
            self.db, network="mainnet", account_address=ACCOUNT, agent_address=self.wallet.address,
            envelope=envelope(self.public, self.wallet),
        )
        self.db.commit()
        self.bundle = {
            "revision": "strategy-one", "status": "active", "selectionGeneration": "generation-one",
            "paramsHash": "abc", "params": {"MARGIN_EQUITY_PCT": 0.8},
            "targets": [{"addr": "0xsource", "seedCoins": ["BTC"], "sectorPolicy": {"allowed": ["crypto"]}}],
        }

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_live_preflight_creates_6400_sizing_base_and_full_live_session(self):
        with patch("hyper.execution.live_preflight.strategy_revision.load_active", return_value=self.bundle), \
                patch("hyper.execution.live_preflight.strategy_revision.resolved_targets", return_value=self.bundle["targets"]):
            result = run_live_preflight(
                self.db,
                private_wrap_key_path=str(self.private),
                websocket_probe=lambda: True,
                info_factory=lambda *_args, **_kwargs: FakeInfo(self.wallet.address),
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["equity"], 8000.0)
        self.assertEqual(result["sizingEquity"], 6400.0)
        with patch("hyper.execution.live_preflight.strategy_revision.load_active", return_value=self.bundle):
            session = activate_live_session(self.db, result["preflightId"], "启动实盘")
        self.assertFalse(session["canary"])
        self.assertIsNone(session["canaryMarginCap"])
        self.assertEqual(session["sizingEquity"], 6400.0)
        self.assertEqual(control.execution_status(self.db)["state"], "live_running")

    def test_credential_verification_syncs_authoritative_agent_expiry(self):
        with patch(
            "hyper.execution.command_worker.create_public_info_client",
            return_value=FakeInfo(self.wallet.address),
        ):
            result = execute_control_command(
                self.db,
                "credential_verify",
                {"network": "mainnet"},
                private_key_path=str(self.private),
            )

        self.assertEqual(result["validUntil"], "2100-01-01T00:00:00Z")
        self.assertEqual(result["accountPreview"]["equity"], 8000.0)
        status = control.execution_status(self.db)
        credential = status["credentials"]["mainnet"]
        self.assertEqual(credential["status"], "verified")
        self.assertEqual(credential["validUntil"], "2100-01-01T00:00:00Z")
        self.assertEqual(status["accountPreview"]["available"], 8000.0)
        self.assertEqual(status["accountPreview"]["positionCount"], 0)
        self.assertIsNone(self.db.execute("SELECT * FROM live_copy_account").fetchone())
        self.assertIsNone(self.db.execute("SELECT * FROM execution_session").fetchone())

    def test_legacy_canary_unlock_requires_only_clean_flat_reconcile(self):
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,sizing_anchor,"
            "margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
            "VALUES ('live-canary','live','mainnet','live_canary',?,?,'r',8000,.8,6400,1,80,?,?)",
            (ACCOUNT, self.wallet.address.lower(), stamp, stamp),
        )
        control.ensure_execution_control(self.db)
        self.db.execute(
            "UPDATE execution_control SET selected_mode='live',state='live_canary',"
            "active_session_id='live-canary' WHERE id=1"
        )
        self.db.execute(
            "INSERT INTO execution_reconcile_checkpoint "
            "(session_id,status,position_count,open_order_count,unknown_positions,unknown_orders,created_at) "
            "VALUES ('live-canary','ok',0,0,0,0,?)", (now_iso(),),
        )

        result = unlock_live_canary(self.db, "解除 Canary")

        self.assertFalse(result["canary"])
        self.assertEqual(control.execution_status(self.db)["state"], "live_running")

    def test_no_funds_fails_closed(self):
        with patch("hyper.execution.live_preflight.strategy_revision.load_active", return_value=self.bundle), \
                patch("hyper.execution.live_preflight.strategy_revision.resolved_targets", return_value=self.bundle["targets"]):
            result = run_live_preflight(
                self.db,
                private_wrap_key_path=str(self.private),
                websocket_probe=lambda: True,
                info_factory=lambda *_args, **_kwargs: FakeInfo(self.wallet.address, total="0"),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "NO_AVAILABLE_COLLATERAL")
        self.assertEqual(control.execution_status(self.db)["state"], "no_funds")

    def test_200_equity_activates_full_live_without_one_percent_cap(self):
        with patch("hyper.execution.live_preflight.strategy_revision.load_active", return_value=self.bundle), \
                patch("hyper.execution.live_preflight.strategy_revision.resolved_targets", return_value=self.bundle["targets"]):
            result = run_live_preflight(
                self.db,
                private_wrap_key_path=str(self.private),
                websocket_probe=lambda: True,
                info_factory=lambda *_args, **_kwargs: FakeInfo(self.wallet.address, total="200"),
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["sizingEquity"], 160.0)
        with patch("hyper.execution.live_preflight.strategy_revision.load_active", return_value=self.bundle):
            session = activate_live_session(self.db, result["preflightId"], "启动实盘")
        self.assertFalse(session["canary"])
        self.assertIsNone(session["canaryMarginCap"])
        self.assertEqual(session["state"], "live_running")

    def test_switch_to_paper_rejects_live_projection(self):
        control.ensure_execution_control(self.db)
        self.db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,sizing_anchor,"
            "margin_equity_pct,sizing_equity,started_at,updated_at) "
            "VALUES ('live-x','live','mainnet','paused',?,?, 'r',8000,.8,6400,'t','t')",
            (ACCOUNT, self.wallet.address.lower()),
        )
        self.db.execute(
            "UPDATE execution_control SET selected_mode='live',state='paused',active_session_id='live-x' WHERE id=1"
        )
        self.db.execute(
            "INSERT INTO execution_position_projection "
            "(session_id,dex,coin,signed_size,observed_at) VALUES ('live-x','','BTC',.1,'t')"
        )
        with self.assertRaisesRegex(ValueError, "live_exposure_prevents_paper_switch"):
            control.set_selected_mode(self.db, "paper")

    def test_mode_switch_requires_observer_to_be_stopped(self):
        control.mark_credential_verified(
            self.db, "mainnet", valid_until="2100-01-01T00:00:00Z",
        )
        self.db.execute(
            "INSERT INTO process_status (name,state) VALUES ('observer','running') "
            "ON CONFLICT(name) DO UPDATE SET state=excluded.state"
        )

        with self.assertRaisesRegex(ValueError, "observer_must_be_stopped"):
            control.set_selected_mode(self.db, "live")

        self.db.execute("UPDATE process_status SET state='stopped' WHERE name='observer'")
        result = control.set_selected_mode(self.db, "live")
        self.assertEqual(result["selectedMode"], "live")

        self.db.execute("UPDATE process_status SET state='running' WHERE name='observer'")
        with self.assertRaisesRegex(ValueError, "observer_must_be_stopped"):
            control.delete_credential(self.db, "mainnet")
        self.assertIsNotNone(control.credential_row(self.db, "mainnet"))
        self.assertEqual(control.execution_status(self.db)["selectedMode"], "live")


if __name__ == "__main__":
    unittest.main()
