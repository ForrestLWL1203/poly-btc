import base64
import json
import os
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account import Account

from hyper import storage
from hyper.execution import control
from hyper.execution.credentials import (
    ENVELOPE_ALGORITHM,
    credential_aad,
    decrypt_agent_wallet,
    generate_wrap_keypair,
    public_wrap_key_payload,
    validate_envelope,
)
from hyper.execution.sdk_clients import CredentialError


ACCOUNT = "0x" + "1" * 40


def encrypted_envelope(public_path, network, account, wallet):
    from cryptography.hazmat.primitives import serialization

    public = serialization.load_pem_public_key(Path(public_path).read_bytes())
    aes = os.urandom(32)
    iv = os.urandom(12)
    ciphertext = AESGCM(aes).encrypt(
        iv, wallet.key.hex().encode(), credential_aad(network, account, wallet.address),
    )
    wrapped = public.encrypt(
        aes,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    key = public_wrap_key_payload(public_path)
    return {
        "version": 1,
        "algorithm": ENVELOPE_ALGORITHM,
        "wrapKeyId": key["wrapKeyId"],
        "wrappedKey": base64.b64encode(wrapped).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


class ExecutionCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = Path(self.temp.name) / "private.pem"
        self.public = Path(self.temp.name) / "public.pem"
        generate_wrap_keypair(self.private, self.public)
        self.wallet = Account.create()
        self.envelope = encrypted_envelope(self.public, "testnet", ACCOUNT, self.wallet)

    def tearDown(self):
        self.temp.cleanup()

    def test_browser_compatible_envelope_decrypts_to_expected_agent(self):
        restored = decrypt_agent_wallet(
            self.envelope,
            network="testnet",
            account_address=ACCOUNT,
            agent_address=self.wallet.address,
            private_key_path=self.private,
        )
        self.assertEqual(restored.address, self.wallet.address)
        self.assertNotIn(self.wallet.key.hex(), json.dumps(self.envelope))

    def test_metadata_binding_prevents_network_or_account_swap(self):
        with self.assertRaisesRegex(CredentialError, "credential_decryption_failed"):
            decrypt_agent_wallet(
                self.envelope,
                network="mainnet",
                account_address=ACCOUNT,
                agent_address=self.wallet.address,
                private_key_path=self.private,
            )

    def test_private_wrap_key_requires_strict_permissions(self):
        os.chmod(self.private, 0o644)
        with self.assertRaisesRegex(CredentialError, "permissions_too_open"):
            decrypt_agent_wallet(
                self.envelope,
                network="testnet",
                account_address=ACCOUNT,
                agent_address=self.wallet.address,
                private_key_path=self.private,
            )

    def test_envelope_shape_and_size_are_bounded(self):
        validate_envelope(self.envelope)
        bad = dict(self.envelope, privateKey="no")
        with self.assertRaisesRegex(CredentialError, "invalid_credential_envelope_fields"):
            validate_envelope(bad)

    def test_control_status_never_returns_ciphertext(self):
        db = storage.connect(":memory:", storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        stored = control.store_encrypted_credential(
            db,
            network="testnet",
            account_address=ACCOUNT,
            agent_address=self.wallet.address,
            envelope=self.envelope,
        )
        db.commit()
        status = control.execution_status(db)
        self.assertEqual(stored["status"], "encrypted")
        self.assertEqual(status["credentials"]["testnet"]["agentAddress"], self.wallet.address.lower())
        self.assertNotIn("envelope", json.dumps(status).lower())
        self.assertNotIn("ciphertext", json.dumps(status).lower())
        db.close()

    def test_key_generation_never_overwrites(self):
        with self.assertRaisesRegex(CredentialError, "already_exists"):
            generate_wrap_keypair(self.private, self.public)


if __name__ == "__main__":
    unittest.main()
