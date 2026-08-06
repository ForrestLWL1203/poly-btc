"""Saved deploy targets + the launcher's own SSH keypair.

Targets live in hyper/launcher/data/targets.json — connection metadata ONLY, never a password: the VPS
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
KNOWN_HOSTS = os.path.join(KEYS, "known_hosts")


def _read_pubkey_for(private_key_path):
    pub_path = private_key_path + ".pub"
    if os.path.exists(pub_path):
        with open(pub_path) as f:
            return f.read().strip()
    r = subprocess.run(["ssh-keygen", "-y", "-f", private_key_path],
                       check=True, capture_output=True, text=True)
    return r.stdout.strip()


def keypair(private_key_path=None):
    """Return (private_key_path, public_key_text).

    If a private key path is supplied, reuse it and derive/read its public key. Otherwise generate
    the launcher-managed keypair on first use.
    """
    if private_key_path:
        path = os.path.expanduser(private_key_path)
        if not os.path.exists(path):
            raise FileNotFoundError(f"SSH private key not found: {path}")
        return path, _read_pubkey_for(path)

    os.makedirs(KEYS, exist_ok=True)
    if not os.path.exists(KEY_PATH):
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", KEY_PATH,
                        "-C", "poly-launcher"], check=True)
        os.chmod(KEY_PATH, 0o600)
    return KEY_PATH, _read_pubkey_for(KEY_PATH)


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


_SAVE_FIELDS = ("name", "mode", "host", "user", "ssh_port", "key_path", "host_fingerprint",
                "app_dir", "branch", "port", "domain", "dash_user", "keyInstalled")


def save(t):
    """Upsert a target by id (host+mode for vps, 'local' for local), MERGING onto any existing record
    so a partial update (e.g. just keyInstalled after a deploy) never wipes name/domain/etc. Secrets are
    dropped (only _SAVE_FIELDS persist)."""
    items = load()
    tid = t.get("id") or (f"vps:{t.get('host')}" if t.get("mode") == "vps" else "local")
    existing = next((x for x in items if x.get("id") == tid), {})
    clean = {**existing, "id": tid, "updated": time.strftime("%Y-%m-%d %H:%M")}
    for k in _SAVE_FIELDS:
        if k in t and t[k] not in (None, ""):
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
