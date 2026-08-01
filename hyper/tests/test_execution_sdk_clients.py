import os
from pathlib import Path
import tempfile
import unittest

from hyper.execution.sdk_clients import CredentialError, load_agent_account


class AgentCredentialTests(unittest.TestCase):
    def setUp(self):
        try:
            from eth_account import Account
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))
        self.account = Account.create()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "agent-key"
        key_hex = self.account.key.hex()
        self.path.write_text(key_hex if key_hex.startswith("0x") else "0x" + key_hex, encoding="utf-8")
        self.path.chmod(0o600)

    def test_protected_key_loads_and_must_match_expected_agent(self):
        loaded = load_agent_account(self.path, expected_agent_address=self.account.address)

        self.assertEqual(loaded.address.lower(), self.account.address.lower())
        with self.assertRaisesRegex(CredentialError, "agent_private_key_address_mismatch"):
            load_agent_account(self.path, expected_agent_address="0x" + "1" * 40)

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_group_or_other_read_permission_is_rejected(self):
        self.path.chmod(0o644)

        with self.assertRaisesRegex(CredentialError, "permissions_too_open"):
            load_agent_account(self.path, expected_agent_address=self.account.address)

    @unittest.skipUnless(os.name == "posix", "symlink contract")
    def test_symlinked_key_is_rejected(self):
        link = Path(self.tempdir.name) / "linked-key"
        link.symlink_to(self.path)

        with self.assertRaisesRegex(CredentialError, "not_regular"):
            load_agent_account(link, expected_agent_address=self.account.address)

    def test_secret_derived_parse_error_is_sanitized(self):
        self.path.write_text("0x" + "z" * 64, encoding="utf-8")

        with self.assertRaisesRegex(CredentialError, "^invalid_agent_private_key$") as caught:
            load_agent_account(self.path, expected_agent_address=self.account.address)
        self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
