import os
import sqlite3
import tempfile
import unittest
from importlib import import_module, util

from hl import api


class ApiCommandTests(unittest.TestCase):
    def _commands_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE commands ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,payload_json TEXT,idempotency_key TEXT,"
            "owner TEXT,status TEXT,created_at TEXT,acked_at TEXT,done_at TEXT,result_json TEXT,error TEXT)"
        )
        db.commit()
        db.close()
        return path

    def test_command_endpoints_are_split_from_api_module(self):
        self.assertIsNotNone(util.find_spec("hl.api_commands"))
        api_commands = import_module("hl.api_commands")

        self.assertIs(api.insert_command, api_commands.insert_command)
        self.assertIs(api.exec_process_command, api_commands.exec_process_command)
        self.assertIs(api.ep_command, api_commands.ep_command)
        self.assertIs(api.ALLOWED_COMMANDS, api_commands.ALLOWED_COMMANDS)

    def test_insert_command_reuses_idempotency_key(self):
        path = self._commands_db()
        try:
            cmd_id, status = api.insert_command(path, "pause", {"a": 1}, "same-key")
            replay_id, replay_status = api.insert_command(path, "pause", {"a": 2}, "same-key")

            self.assertEqual(status, "pending")
            self.assertEqual(replay_status, "pending")
            self.assertEqual(replay_id, cmd_id)
            db = sqlite3.connect(path)
            n = db.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
            db.close()
            self.assertEqual(n, 1)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
