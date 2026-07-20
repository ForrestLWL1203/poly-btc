import tempfile
import unittest
from pathlib import Path

from hyper import config, params, storage
from hyper.ops import paper_reset


class PaperResetTests(unittest.TestCase):
    def open_db(self, td):
        db = storage.connect(
            str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )
        params.seed_params(db)
        return db

    def test_reset_preserves_operator_params_and_credentials_and_recreates_account(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute("UPDATE params SET value='11' WHERE key='CORE_INITIAL_MAX_N'")
            db.execute(
                "INSERT INTO provider_credential "
                "(provider,envelope_version,key_id,wrapped_key,nonce,ciphertext,status,created_at,updated_at) "
                "VALUES ('fixture',1,'kid','wrapped','nonce','cipher','valid','now','now')"
            )
            db.execute("INSERT INTO profile(addr,status) VALUES('0xabc','active')")
            db.execute(
                "INSERT OR REPLACE INTO copy_account(id,initial_balance,balance,updated_at) "
                "VALUES(1,10000,12345,'old')"
            )
            db.commit()

            result = paper_reset.reset(db)

            self.assertEqual(result["params"], "preserved")
            self.assertEqual(db.execute(
                "SELECT value FROM params WHERE key='CORE_INITIAL_MAX_N'"
            ).fetchone()[0], "11")
            self.assertEqual(db.execute(
                "SELECT status FROM provider_credential WHERE provider='fixture'"
            ).fetchone()[0], "valid")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT initial_balance,balance FROM copy_account WHERE id=1"
            ).fetchone(), (10000.0, 10000.0))

    def test_factory_option_restores_code_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute("UPDATE params SET value='11' WHERE key='CORE_INITIAL_MAX_N'")
            db.commit()

            result = paper_reset.reset(db, factory_params=True)

            self.assertEqual(result["params"], "factory")
            self.assertEqual(int(db.execute(
                "SELECT value FROM params WHERE key='CORE_INITIAL_MAX_N'"
            ).fetchone()[0]), int(config.CORE_INITIAL_MAX_N))


if __name__ == "__main__":
    unittest.main()
