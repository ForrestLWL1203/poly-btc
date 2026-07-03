"""Self-contained process manager for the dashboard control plane.

The dashboard SPAWNS and manages the observer (copy engine) and one-shot scan processes DIRECTLY,
as DETACHED children (setsid → their own session, so they SURVIVE a dashboard restart), each tracked
by a pidfile under <db_dir>/run/. This removes the need for a separate always-on supervisor daemon
(the old hl-scan-trigger): the dashboard is the only thing you run, and its buttons start/stop the
observer and trigger scans INDEPENDENTLY —

  点采集   → start_scan()      (spawns a one-shot `hl_discover.py scan`; self-reports scan_progress)
  启动跟单 → start_observer()  (spawns `hl_observe.py observe`, detached)
  停止跟单 → stop_observer()   (SIGTERM the observer's process group, immediately)

A 24h auto-scan ticker (start_auto_scan_ticker) lives in the dashboard's serve(). Because children are
detached + pidfile-tracked, a dashboard restart re-attaches to a still-running observer (never a double
spawn, never an orphan kill). Money-critical isolation: killing/restarting the dashboard NEVER stops
live copying.

Design notes:
- start_new_session=True → child is a session leader; os.killpg(pgid) stops the whole tree.
- liveness = os.kill(pid,0) + a /proc cmdline needle (guards against PID reuse; the needle check is
  skipped where /proc is absent, e.g. macOS dev).
- DB writes go through short-lived rw connections and are best-effort (wrapped) — process control must
  never wedge on a busy DB. Scans self-report scan_progress; the observer self-reports process_status
  heartbeats. procman only writes the states a killed process can't write itself (observer→stopped).
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root (…/hl/..)
PYTHON = sys.executable                                              # the venv python running us

OBSERVER, SCAN = "observer", "scan"
_NEEDLE = {OBSERVER: "hl_observe.py", SCAN: "hl_discover.py"}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── pidfile / liveness ──────────────────────────────────────────────────────
def _run_dir(db_path):
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", "run")
    os.makedirs(d, exist_ok=True)
    return d


def _pidfile(db_path, name):
    return os.path.join(_run_dir(db_path), f"{name}.pid")


def _logfile(db_path, name):
    return os.path.join(_run_dir(db_path), f"{name}.log")


def _read_pid(db_path, name):
    try:
        with open(_pidfile(db_path, name)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _clear_pid(db_path, name):
    try:
        os.remove(_pidfile(db_path, name))
    except OSError:
        pass


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        return None            # not Linux / no such proc → caller skips the needle guard


def _reap(pid):
    """Best-effort reap of a dead child (prevents zombies while the dashboard is its parent). Ignores
    ECHILD — after a dashboard restart the process reparents to init, which reaps it (it's not ours)."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _alive(pid, needle=None):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # exists (owned by another uid)
    try:                       # a zombie (exited, reap-pending) is effectively dead (Linux /proc)
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[-1].split()[0] == "Z":
                return False
    except OSError:
        pass                   # no /proc (macOS dev) or a read race — fall through
    if needle:
        cl = _cmdline(pid)
        if cl is not None and needle not in cl:
            return False       # PID reused by an unrelated process
    return True


def is_running(db_path, name):
    return _alive(_read_pid(db_path, name), _NEEDLE.get(name))


def _spawn(db_path, name, argv):
    """Start a detached child (own session), record its pid. Idempotent: no-op if already alive."""
    if is_running(db_path, name):
        return _read_pid(db_path, name), False
    logf = open(_logfile(db_path, name), "ab", buffering=0)
    p = subprocess.Popen(argv, cwd=REPO, stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    with open(_pidfile(db_path, name), "w") as f:
        f.write(str(p.pid))
    return p.pid, True


def _stop(db_path, name, grace=8.0):
    pid = _read_pid(db_path, name)
    if not _alive(pid, _NEEDLE.get(name)):
        _clear_pid(db_path, name)
        return False
    def _sig(s):
        try:
            os.killpg(os.getpgid(pid), s)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, s)
            except OSError:
                pass
    _sig(signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        _reap(pid)                          # reap if it became our zombie, so _alive turns False promptly
        if not _alive(pid):
            break
        time.sleep(0.2)
    if _alive(pid):
        _sig(signal.SIGKILL)
        time.sleep(0.3)
        _reap(pid)
    _clear_pid(db_path, name)
    return True


# ── small best-effort DB writes ─────────────────────────────────────────────
def _db(db_path):
    c = sqlite3.connect(db_path, timeout=10)
    c.execute("PRAGMA busy_timeout=10000")
    return c


def _set_proc_status(db_path, name, state, pid):
    try:
        c = _db(db_path)
        c.execute("INSERT INTO process_status (name,state,pid,heartbeat_at,detail_json) "
                  "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,"
                  "pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,detail_json=excluded.detail_json",
                  (name, state, pid, _now_iso(), json.dumps({"by": "dashboard"})))
        c.commit()
        c.close()
    except sqlite3.Error:
        pass


def _scan_progress_scanning(db_path):
    try:
        c = _db(db_path)
        r = c.execute("SELECT state FROM scan_progress WHERE id=1").fetchone()
        c.close()
        return bool(r and r[0] == "scanning")
    except sqlite3.Error:
        return False


def hours_since_last_scan(db_path):
    """Hours since the last COMPLETED scan (scan_runs.finished_at). 1e9 if never scanned (→ scan now).
    On any read error return 0 so the auto-ticker does NOT spuriously fire."""
    try:
        c = _db(db_path)
        r = c.execute("SELECT MAX(finished_at) FROM scan_runs").fetchone()
        c.close()
        if not r or not r[0]:
            return 1e9
        last = datetime.strptime(r[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    except (sqlite3.Error, ValueError):
        return 0.0


# ── domain API (called by the dashboard) — SYSTEMD is the supervisor ─────────
# The dashboard buttons drive real systemd units (the dashboard runs as root → can systemctl directly).
# Restart=always + boot-start (hl-observe) and the daily hl-scan.timer are OS-supervised — no in-process
# want-marker/ticker to be a single point of failure. The naked-spawn helpers above are legacy/unused.
OBSERVER_UNIT = "hl-observe.service"
SCAN_UNIT = "hl-scan.service"


def _systemctl(*args, timeout=15):
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001 — systemd absent (dev box) → treat as unknown, never raise
        return None


def _unit_state(unit):
    r = _systemctl("is-active", unit)
    return (r.stdout.strip() if r else "") or "unknown"


def observer_running(db_path):
    return _unit_state(OBSERVER_UNIT) == "active"


def start_observer(db_path):
    """'启动跟单' → systemd starts the observer service; Restart=always + boot-start then supervise it."""
    _systemctl("start", "--no-block", OBSERVER_UNIT)
    running = observer_running(db_path)
    _set_proc_status(db_path, "observer", "running" if running else "stopped", None)
    return {"running": running, "pid": None, "started": running}


def stop_observer(db_path):
    """'停止' → systemd stops it. It stays stopped until started again (enabled = boot-start, not auto-resume
    of a deliberate stop)."""
    _systemctl("stop", OBSERVER_UNIT)
    _set_proc_status(db_path, "observer", "stopped", None)   # killed process can't log its own down-state
    return {"running": False, "stopped": True}


def scan_running(db_path):
    st = _unit_state(SCAN_UNIT)                  # oneshot: 'activating'/'active' while the sweep runs
    return st in ("active", "activating") or _scan_progress_scanning(db_path)


def start_scan(db_path, full=False):
    """'重新扫描' → kick the oneshot scan service now (the daily auto-scan is the systemd timer
    hl-scan.timer). scanner.scan still marks manual=1 from the queued 'rescan' command."""
    if scan_running(db_path):
        return {"scanning": True, "started": False, "reason": "already_scanning"}
    _systemctl("start", "--no-block", SCAN_UNIT)
    return {"scanning": True, "started": True, "pid": None}


def reconcile(db_path):
    """Sync the observer's process_status marker to systemd's real state + clean any legacy naked pidfile.
    The live observer writes its own heartbeat; this only corrects the marker after a systemd-side stop/crash
    the process couldn't log itself. (Supervision + auto-resume are systemd's job now, not ours.)"""
    if not observer_running(db_path):
        pid = _read_pid(db_path, OBSERVER)
        if pid:
            _clear_pid(db_path, OBSERVER)        # one-time cleanup of the pre-systemd naked pidfile


def auto_scan_tick(db_path):
    return   # the daily auto-scan is the systemd timer hl-scan.timer now, not an in-process tick


def start_auto_scan_ticker(db_path, interval=60.0):
    """Kept for API compatibility (dashboard boot calls it). Light status-sync only — real supervision and
    the daily scan belong to systemd (hl-observe Restart=always, hl-scan.timer)."""
    import threading

    def loop():
        while True:
            try:
                reconcile(db_path)
            except Exception:  # noqa: BLE001 — a ticker error must never kill the thread
                pass
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="status-sync")
    t.start()
    return t
