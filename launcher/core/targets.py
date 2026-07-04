"""Saved deploy targets + the launcher's own SSH keypair.

Targets live in launcher/data/targets.json — connection metadata ONLY, never a password: the VPS
password is used once (first deploy) then discarded; every later op authenticates with the launcher
keypair (data/keys/id_ed25519, mode 600) installed during the ssh_key step. This keeps the store
safe to leave on disk. dash_password is likewise entered at deploy time, not persisted.
"""
import json
import os
import subprocess
import time

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # launcher/
DATA = os.path.join(_HERE, "data")
KEYS = os.path.join(DATA, "keys")
TARGETS_JSON = os.path.join(DATA, "targets.json")
KEY_PATH = os.path.join(KEYS, "id_ed25519")


def keypair():
    """Return (private_key_path, public_key_text), generating the launcher keypair on first use."""
    os.makedirs(KEYS, exist_ok=True)
    if not os.path.exists(KEY_PATH):
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", KEY_PATH,
                        "-C", "poly-launcher"], check=True)
        os.chmod(KEY_PATH, 0o600)
    with open(KEY_PATH + ".pub") as f:
        return KEY_PATH, f.read().strip()


def load():
    try:
        with open(TARGETS_JSON) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _write(items):
    os.makedirs(DATA, exist_ok=True)
    tmp = TARGETS_JSON + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, TARGETS_JSON)


_SAVE_FIELDS = ("name", "mode", "host", "user", "ssh_port", "app_dir", "branch",
                "port", "domain", "dash_user")


def save(t):
    """Upsert a target by id (host+mode for vps, 'local' for local). Strips any secret fields."""
    items = load()
    tid = t.get("id") or (f"vps:{t.get('host')}" if t.get("mode") == "vps" else "local")
    clean = {"id": tid, "updated": time.strftime("%Y-%m-%d %H:%M")}
    for k in _SAVE_FIELDS:
        if k in t:
            clean[k] = t[k]
    items = [x for x in items if x.get("id") != tid]
    items.append(clean)
    _write(items)
    return clean


def get(tid):
    for x in load():
        if x.get("id") == tid:
            return x
    return None


def remove(tid):
    _write([x for x in load() if x.get("id") != tid])
