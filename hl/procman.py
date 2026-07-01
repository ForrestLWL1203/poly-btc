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


# ── domain API (called by the dashboard) ────────────────────────────────────
def observer_running(db_path):
    return is_running(db_path, OBSERVER)


# ── observer desired-state (want-marker) → auto-resume, replacing systemd Restart=always/boot-start ──
def _wantfile(db_path):
    return os.path.join(_run_dir(db_path), f"{OBSERVER}.want")


def _set_want(db_path, want):
    """Persist whether the observer SHOULD be running. reconcile() re-starts it whenever want=1 but the
    process is dead (crash / dashboard restart / VPS reboot); a deliberate stop clears it so we never
    resurrect an observer the operator turned off."""
    if want:
        try:
            with open(_wantfile(db_path), "w") as f:
                f.write("1")
        except OSError:
            pass
    else:
        try:
            os.remove(_wantfile(db_path))
        except OSError:
            pass


def _get_want(db_path):
    return os.path.exists(_wantfile(db_path))


def start_observer(db_path):
    _set_want(db_path, True)         # desired-state = running → reconcile auto-resumes after any death
    argv = [PYTHON, os.path.join(REPO, "hl_observe.py"), "--db", db_path, "observe"]
    pid, started = _spawn(db_path, OBSERVER, argv)
    if started:                     # optimistic 'running' so the UI flips instantly; the observer then
        _set_proc_status(db_path, "observer", "running", pid)   # keeps its own heartbeat (stale→stopped)
    return {"running": True, "pid": pid, "started": started}


def stop_observer(db_path):
    _set_want(db_path, False)        # deliberate stop → do NOT auto-resume
    stopped = _stop(db_path, OBSERVER)
    _set_proc_status(db_path, "observer", "stopped", None)      # a killed observer can't write its own down-state
    return {"running": False, "stopped": stopped}


def scan_running(db_path):
    return is_running(db_path, SCAN) or _scan_progress_scanning(db_path)


def start_scan(db_path, full=False):
    """Spawn a one-shot scan process (guarded to a single concurrent scan). MANUAL vs AUTO is decided by
    scanner.scan itself from whether a pending 'rescan' command exists — the dashboard queues that command
    for a button press (locks the UI) and does NOT for the 24h auto tick (silent). So this just spawns."""
    if scan_running(db_path):
        return {"scanning": True, "started": False, "reason": "already_scanning"}
    argv = [PYTHON, os.path.join(REPO, "hl_discover.py"), "--db", db_path, "scan"]
    if not observer_running(db_path):        # observer off → take the full REST budget, scan FAST (~20-25 min)
        argv += ["--scan-interval", "1.0", "--workers", "4"]   # else use the CLI default (8s, shares budget)
    if full:
        argv.append("--full")
    pid, started = _spawn(db_path, SCAN, argv)
    return {"scanning": True, "started": started, "pid": pid}


def reconcile(db_path):
    """On dashboard boot: drop stale pidfiles (dead process) so is_running is honest, and if the observer
    pidfile is dead, make sure process_status reflects stopped. A LIVE observer is left untouched (re-attach)."""
    for name in (OBSERVER, SCAN):
        pid = _read_pid(db_path, name)
        if pid:
            _reap(pid)                       # reap our dead children (no zombies in the long-lived dashboard)
        if pid and not _alive(pid, _NEEDLE.get(name)):
            _clear_pid(db_path, name)
            if name == OBSERVER:
                _set_proc_status(db_path, "observer", "stopped", None)
    # AUTO-RESUME (replaces systemd Restart=always + boot-start): if the observer is DESIRED running but
    # its process is gone (crash / dashboard restart / VPS reboot), bring it back. Called on dashboard boot
    # AND every ticker cycle (~60s), so a crashed observer self-heals within a cycle. Crash-loop-safe:
    # at most one (re)start per reconcile.
    if _get_want(db_path) and not is_running(db_path, OBSERVER):
        start_observer(db_path)


def auto_scan_tick(db_path):
    """Spawn a SILENT auto-scan when AUTO_SCAN_EVERY_H has elapsed since the last completed scan and no
    scan is currently running. No 'rescan' command is queued → scanner.scan marks it manual=0 (silent)."""
    if scan_running(db_path):
        return
    if hours_since_last_scan(db_path) >= config.AUTO_SCAN_EVERY_H:
        start_scan(db_path)


def start_auto_scan_ticker(db_path, interval=60.0):
    import threading

    def loop():
        while True:
            try:
                reconcile(db_path)          # reap dead children + clear stale pidfiles + flip observer→stopped
                auto_scan_tick(db_path)
            except Exception:  # noqa: BLE001 — a ticker error must never kill the thread
                pass
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="auto-scan-ticker")
    t.start()
    return t
