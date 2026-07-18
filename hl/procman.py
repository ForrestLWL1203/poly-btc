"""Self-contained process manager for the dashboard control plane.

The dashboard SPAWNS and manages the observer (copy engine) and one-shot scan processes DIRECTLY,
as DETACHED children (setsid → their own session, so they SURVIVE a dashboard restart), each tracked
by a pidfile under <db_dir>/run/. This removes the need for a separate always-on supervisor daemon
(the old hl-scan-trigger): the dashboard is the only thing you run, and its buttons start/stop the
observer and trigger scans INDEPENDENTLY —

  点采集   → start_scan()      (spawns a one-shot `hl_discover.py scan`; self-reports scan_progress)
  启动跟单 → start_observer()  (spawns `hl_observe.py observe`, detached)
  停止跟单 → stop_observer()   (SIGTERM the observer's process group, immediately)

There is intentionally NO in-process auto-scan in the dashboard. A brand-new local launcher install
starts only the dashboard; the first full scan is an explicit operator action. On VPS installs, daily
background scans belong to systemd's hl-scan.timer. The dashboard's small background ticker only
reconciles process status/pidfiles.

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


# ── domain API (called by the dashboard) — SYSTEMD on the VPS, DETACHED-SPAWN locally ────────
# On the production VPS the dashboard runs as root and drives real systemd units (hl-observe
# Restart=always + boot-start, hl-scan.timer daily) — OS-supervised, no in-process SPOF. On a box
# with NO systemd (macOS dev, a local launcher deploy) the SAME buttons fall back to the detached-
# spawn helpers above (pidfile-tracked under data/run/, session-leader so a dashboard restart never
# double-spawns or orphan-kills). _use_systemd() picks the backend once; everything else is transparent.
OBSERVER_UNIT = "hl-observe.service"
SCAN_UNIT = "hl-scan.service"


def _systemctl(*args, timeout=15):
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001 — systemd absent (dev box) → treat as unknown, never raise
        return None


_SYSTEMD = None
def _use_systemd():
    """True iff this host is systemd-managed (cached). Gates the supervisor backend for observer/scan."""
    global _SYSTEMD
    if _SYSTEMD is None:
        r = _systemctl("is-system-running", timeout=5)     # any answer (even 'degraded') ⇒ systemd present
        _SYSTEMD = r is not None and bool((r.stdout or "").strip())
    return _SYSTEMD


def _observe_argv(db_path):
    return [PYTHON, os.path.join(REPO, "hl_observe.py"), "--db", db_path, "observe"]


def _scan_argv(db_path):
    return [PYTHON, os.path.join(REPO, "hl_discover.py"), "--db", db_path, "scan", "--days", "14"]


def _repair_scan_state(db_path):
    """Let the scanner maintenance CLI restore published watchlist/progress state after a hard stop."""
    try:
        r = subprocess.run(
            [PYTHON, os.path.join(REPO, "hl_discover.py"), "--db", db_path, "repair-watchlist"],
            cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — caller reports a bounded process-control failure
        return False


def _cancel_rescan_commands(db_path):
    """Retire the interrupted request so a later incremental click cannot inherit an old full flag."""
    try:
        c = _db(db_path)
        cur = c.execute(
            "UPDATE commands SET status='failed',done_at=?,error=?,result_json=? "
            "WHERE type='rescan' AND status IN ('pending','acked')",
            (_now_iso(), "cancelled_by_operator", json.dumps({"cancelled": True, "retry": True})),
        )
        c.commit()
        count = max(0, int(cur.rowcount or 0))
        c.close()
        return count
    except sqlite3.Error:
        return 0


def _unit_state(unit):
    r = _systemctl("is-active", unit)
    return (r.stdout.strip() if r else "") or "unknown"


def observer_running(db_path):
    if _use_systemd():
        return _unit_state(OBSERVER_UNIT) == "active"
    return is_running(db_path, OBSERVER)


def start_observer(db_path):
    """'启动跟单' → systemd (VPS) or a detached child (local). Idempotent; supervised by OS / pidfile."""
    if _use_systemd():
        _systemctl("start", "--no-block", OBSERVER_UNIT)
    else:
        _spawn(db_path, OBSERVER, _observe_argv(db_path))
    running = observer_running(db_path)
    pid = None if _use_systemd() else _read_pid(db_path, OBSERVER)
    _set_proc_status(db_path, "observer", "running" if running else "stopped", pid)
    return {"running": running, "pid": pid, "started": running}


def stop_observer(db_path):
    """'停止' → systemd stops it / SIGTERM the local child's group. Stays stopped until started again."""
    if _use_systemd():
        _systemctl("stop", OBSERVER_UNIT)
    else:
        _stop(db_path, OBSERVER)
    _set_proc_status(db_path, "observer", "stopped", None)   # killed process can't log its own down-state
    return {"running": False, "stopped": True}


def scan_running(db_path):
    if _use_systemd():
        st = _unit_state(SCAN_UNIT)              # oneshot: 'activating'/'active' while the sweep runs
        if st in ("active", "activating"):
            return True
    elif is_running(db_path, SCAN):
        return True
    return _scan_progress_scanning(db_path)


def start_scan(db_path, full=False):
    """'重新扫描' → kick a scan now (systemd oneshot on the VPS, detached child locally). scanner.scan
    reads the 'full' flag from the queued rescan command + marks manual=1 itself."""
    if scan_running(db_path):
        return {"scanning": True, "started": False, "reason": "already_scanning"}
    if _use_systemd():
        _systemctl("start", "--no-block", SCAN_UNIT)
        pid = None
    else:
        pid, _ = _spawn(db_path, SCAN, _scan_argv(db_path))
    return {"scanning": True, "started": True, "pid": pid}


def stop_scan(db_path):
    """Emergency-stop the current scan without changing the last published generation.

    systemd kills the whole service cgroup on the VPS; the local backend kills the detached process
    group.  The maintenance CLI then restores read state from the immutable published generation.
    """
    was_running = scan_running(db_path)
    if _use_systemd():
        r = _systemctl("stop", SCAN_UNIT, timeout=45)
        if r is None or r.returncode != 0 or _unit_state(SCAN_UNIT) in ("active", "activating"):
            raise RuntimeError("scan_stop_failed")
    else:
        _stop(db_path, SCAN)
    cancelled = _cancel_rescan_commands(db_path)
    if _scan_progress_scanning(db_path) and not _repair_scan_state(db_path):
        raise RuntimeError("scan_state_repair_failed")
    return {"scanning": False, "stopped": True, "wasRunning": was_running,
            "cancelledCommands": cancelled}


def reconcile(db_path):
    """Sync the observer's process_status marker to systemd's real state + clean any legacy naked pidfile.
    The live observer writes its own heartbeat; this only corrects the marker after a systemd-side stop/crash
    the process couldn't log itself. (Supervision + auto-resume are systemd's job now, not ours.)"""
    if not observer_running(db_path):
        pid = _read_pid(db_path, OBSERVER)
        if pid:
            _clear_pid(db_path, OBSERVER)        # one-time cleanup of the pre-systemd naked pidfile


def start_auto_scan_ticker(db_path, interval=60.0, stop_event=None):
    """Kept for API compatibility (dashboard boot calls it). Light status-sync only.

    Do not start scans here. Local launcher deployments must remain idle until the operator triggers
    the first full collection from the dashboard.
    """
    import threading

    def loop():
        while stop_event is None or not stop_event.is_set():
            try:
                reconcile(db_path)
            except Exception:  # noqa: BLE001 — a ticker error must never kill the thread
                pass
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="status-sync")
    t.start()
    return t
