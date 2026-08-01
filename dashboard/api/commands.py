"""Dashboard command-channel endpoints and process-control commands."""

import json
import sqlite3

from hyper.ops import procman
from hyper.util import now_iso
from .common import q1


ALLOWED_COMMANDS = {"pause", "resume", "close_position", "close_all", "wallet_toggle",
                    "wallet_exit_request", "wallet_star",
                    "observer_start", "observer_stop", "rescan", "scan_stop",
                    "patch_params", "reload_params"}
PROCESS_COMMANDS = {"observer_start", "observer_stop", "rescan", "scan_stop"}


def validate_command_payload(ctype, payload):
    """Validate command payloads before they can enter the command table."""
    payload = payload or {}
    if ctype in {"wallet_toggle", "wallet_star"}:
        expected_flag = "enabled" if ctype == "wallet_toggle" else "starred"
        if set(payload) != {"address", expected_flag}:
            raise ValueError(f"{ctype} requires address and {expected_flag}")
        address = payload.get("address")
        if not isinstance(address, str) or not address.strip() or len(address) > 128:
            raise ValueError("invalid wallet address")
        if not isinstance(payload.get(expected_flag), bool):
            raise ValueError(f"{expected_flag} must be boolean")
    elif ctype == "wallet_exit_request":
        if set(payload) != {"address"}:
            raise ValueError("wallet_exit_request requires address")
        address = payload.get("address")
        if not isinstance(address, str) or not address.strip() or len(address) > 128:
            raise ValueError("invalid wallet address")
    return payload


def rw_connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    return db


def insert_command(db_path, ctype, payload, idem):
    db = rw_connect(db_path)
    try:
        if idem:
            row = db.execute("SELECT id,status FROM commands WHERE idempotency_key=?", (idem,)).fetchone()
            if row:
                return row["id"], row["status"]
        cur = db.execute(
            "INSERT INTO commands (type,payload_json,idempotency_key,owner,status,created_at) "
            "VALUES (?,?,?,?,'pending',?)",
            (ctype, json.dumps(payload or {}), idem, "dashboard", now_iso()))
        db.commit()
        return cur.lastrowid, "pending"
    finally:
        db.close()


def _resolve_command(db_path, cmd_id, status, result):
    try:
        db = rw_connect(db_path)
        db.execute("UPDATE commands SET status=?,done_at=?,result_json=? WHERE id=?",
                   (status, now_iso(), json.dumps(result or {}), cmd_id))
        db.commit()
        db.close()
    except sqlite3.Error:
        pass


def exec_process_command(db_path, ctype, payload=None):
    """Run a process-lifecycle command inline and record the result in commands."""
    cmd_id, _ = insert_command(db_path, ctype, payload, None)
    try:
        if ctype == "observer_start":
            res = procman.start_observer(db_path)
        elif ctype == "observer_stop":
            res = procman.stop_observer(db_path)
        elif ctype == "scan_stop":
            res = procman.stop_scan(db_path)
        else:
            procman.start_scan(db_path)
            return cmd_id, "pending"
        _resolve_command(db_path, cmd_id, "done", res)
        return cmd_id, "done"
    except Exception as e:  # noqa: BLE001
        _resolve_command(db_path, cmd_id, "error", {"error": str(e)})
        return cmd_id, "error"


def ep_command(db, cmd_id):
    r = q1(db, "SELECT id,type,status,result_json,error,created_at,acked_at,done_at "
               "FROM commands WHERE id=?", (cmd_id,))
    if not r:
        return {"commandId": cmd_id, "status": "not_found"}
    return {"commandId": r["id"], "type": r["type"], "status": r["status"],
            "result": json.loads(r["result_json"]) if r["result_json"] else None,
            "error": r["error"], "createdAt": r["created_at"],
            "ackedAt": r["acked_at"], "doneAt": r["done_at"]}
