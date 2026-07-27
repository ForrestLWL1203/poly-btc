"""Cross-process serialization for full scans and Challenger refreshes."""
from __future__ import annotations

from contextlib import contextmanager
import os

try:
    import fcntl
except ImportError:  # Windows launcher/local development
    fcntl = None
    import msvcrt


class ScanBusyError(RuntimeError):
    pass


@contextmanager
def acquire(db_path: str, *, blocking: bool = False):
    """Hold the scanner's process lock for one bounded scan operation."""
    run_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "run")
    os.makedirs(run_dir, exist_ok=True)
    lock_path = os.path.join(run_dir, "scanner.lock")
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            if fcntl is not None:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), flags)
            else:
                handle.seek(0)
                handle.write("\0")
                handle.flush()
                handle.seek(0)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
        except (BlockingIOError, OSError) as exc:
            raise ScanBusyError("scanner_run_already_active") from exc
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
