"""Application-level memory guard for scanner replay/tuning.

The production host is Linux and exposes exact current RSS/Swap through procfs. Other platforms retain a
conservative best-effort view. Callers defer a resumable generation before decoding/copying a replay surface;
systemd's cgroup limit remains only the last line of defence.
"""
from __future__ import annotations

import os
from pathlib import Path

from hyper import config


class ResourceDeferred(RuntimeError):
    def __init__(self, detail: dict):
        self.detail = dict(detail)
        reasons = ",".join(self.detail.get("reasons") or ("memory_budget",))
        super().__init__(f"resource_deferred:{reasons}")


def _meminfo() -> dict[str, int]:
    values = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        key, separator, rest = line.partition(":")
        if not separator:
            continue
        try:
            values[key] = int(rest.strip().split()[0]) * 1024
        except (IndexError, TypeError, ValueError):
            continue
    return values


def physical_memory_bytes() -> int | None:
    value = _meminfo().get("MemTotal")
    if value:
        return value
    try:
        value = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def available_memory_bytes() -> int | None:
    return _meminfo().get("MemAvailable")


def _proc_status(pid: int) -> tuple[int, int]:
    rss = swap = 0
    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines()
    except OSError:
        return rss, swap
    for line in lines:
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) * 1024
        elif line.startswith("VmSwap:"):
            swap = int(line.split()[1]) * 1024
    return rss, swap


def _parent_pid(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = raw[raw.rfind(")") + 2:].split()
        return int(tail[1])
    except (OSError, IndexError, TypeError, ValueError):
        return None


def process_tree_usage_bytes(root_pid: int | None = None) -> tuple[int, int]:
    root = int(root_pid or os.getpid())
    proc = Path("/proc")
    if not proc.exists():
        return _proc_status(root)
    parents = {}
    try:
        pids = [int(path.name) for path in proc.iterdir() if path.name.isdigit()]
    except OSError:
        return _proc_status(root)
    for pid in pids:
        parents[pid] = _parent_pid(pid)
    selected = {root}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in selected and pid not in selected:
                selected.add(pid)
                changed = True
    rss = swap = 0
    for pid in selected:
        proc_rss, proc_swap = _proc_status(pid)
        rss += proc_rss
        swap += proc_swap
    return rss, swap


def cgroup_memory_metrics() -> dict:
    """Read cgroup-v2 scanner memory telemetry when available.

    ``memory.current`` includes reclaimable file-backed cache.  Large sequential
    SQLite reads can therefore leave it close to ``MemoryHigh`` even after the
    Python replay objects have been released.  Keep the total for observability,
    but expose the anonymous/unreclaimable working set separately so callers do
    not treat an evictable database page cache as an OOM condition.
    """
    try:
        row = next(
            line for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
            if line.startswith("0::")
        )
        relative = row.split("::", 1)[1].lstrip("/")
        root = Path("/sys/fs/cgroup") / relative
        current = int((root / "memory.current").read_text(encoding="utf-8").strip())
        events = {}
        for line in (root / "memory.events").read_text(encoding="utf-8").splitlines():
            key, value = line.split(None, 1)
            events[key] = int(value)
        memory_stat = {}
        for line in (root / "memory.stat").read_text(encoding="utf-8").splitlines():
            key, value = line.split(None, 1)
            memory_stat[key] = int(value)
        anonymous = int(memory_stat.get("anon") or 0)
        file_cache = int(memory_stat.get("file") or 0)
        unreclaimable = sum(int(memory_stat.get(key) or 0) for key in (
            "slab_unreclaimable", "sock", "pagetables", "percpu",
        ))
        return {
            "currentBytes": current,
            "anonymousBytes": anonymous,
            "fileCacheBytes": file_cache,
            "unreclaimableBytes": unreclaimable,
            "workingSetBytes": anonymous + unreclaimable,
            "events": events,
        }
    except (OSError, StopIteration, TypeError, ValueError):
        return {
            "currentBytes": None,
            "anonymousBytes": None,
            "fileCacheBytes": None,
            "unreclaimableBytes": None,
            "workingSetBytes": None,
            "events": {},
        }


def assess_replay_budget(raw_fill_bytes: int = 0) -> dict:
    raw_fill_bytes = max(0, int(raw_fill_bytes or 0))
    estimated_decode = int(
        raw_fill_bytes * float(getattr(config, "SCANNER_REPLAY_DECODE_MULTIPLIER", 6.0))
    )
    available = available_memory_bytes()
    rss, swap = process_tree_usage_bytes()
    min_available = int(config.SCANNER_MIN_AVAILABLE_MEMORY_BYTES)
    max_rss = int(config.SCANNER_MAX_PROCESS_TREE_RSS_BYTES)
    max_swap = int(config.SCANNER_MAX_PROCESS_TREE_SWAP_BYTES)
    cgroup = cgroup_memory_metrics()
    reasons = []
    if available is not None and available < min_available + estimated_decode:
        reasons.append("available_memory")
    if rss + estimated_decode > max_rss:
        reasons.append("process_tree_rss")
    # Do not gate on cgroup ``memory.current``: it includes evictable SQLite
    # file pages and double-counts the surface estimate against that cache.  A
    # process-tree RSS check remains the primary boundary.  The cgroup working
    # set catches anonymous/kernel memory in the unit that is not visible in
    # the current process tree (for example a just-detached helper).
    cgroup_working_set = cgroup.get("workingSetBytes")
    if (
        cgroup_working_set is not None
        and cgroup_working_set + estimated_decode > max_rss
        and cgroup_working_set > rss
    ):
        reasons.append("cgroup_working_set")
    if swap > max_swap:
        reasons.append("process_tree_swap")
    return {
        "status": "resource_deferred" if reasons else "ok",
        "reasons": reasons,
        "rawFillBytes": raw_fill_bytes,
        "estimatedDecodedBytes": estimated_decode,
        "availableMemoryBytes": available,
        "processTreeRssBytes": rss,
        "processTreeSwapBytes": swap,
        "minAvailableBytes": min_available,
        "maxProcessTreeRssBytes": max_rss,
        "maxProcessTreeSwapBytes": max_swap,
        "physicalMemoryBytes": physical_memory_bytes(),
        "cgroupMemoryCurrentBytes": cgroup["currentBytes"],
        "cgroupAnonymousBytes": cgroup.get("anonymousBytes"),
        "cgroupFileCacheBytes": cgroup.get("fileCacheBytes"),
        "cgroupUnreclaimableBytes": cgroup.get("unreclaimableBytes"),
        "cgroupWorkingSetBytes": cgroup_working_set,
        "cgroupMemoryEvents": cgroup["events"],
    }


def require_replay_budget(raw_fill_bytes: int = 0) -> dict:
    detail = assess_replay_budget(raw_fill_bytes)
    if detail["status"] != "ok":
        raise ResourceDeferred(detail)
    return detail
