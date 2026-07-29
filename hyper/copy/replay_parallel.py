"""Bounded CPU parallelism for pure replay work.

Replay workers never receive a live SQLite connection.  The caller prepares immutable fills/market
surfaces in the parent process, workers calculate pure results, and the parent remains the only database
writer.  A one-core host stays entirely in-process; larger hosts scale up automatically to the configured
ceiling.
"""
from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
from collections.abc import Callable, Iterable
from concurrent.futures.process import BrokenProcessPool

from hyper import config


def available_cpu_count() -> int:
    """Return the CPU quota visible to this process, including Linux affinity limits."""
    try:
        affinity = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        affinity = None
    detected = len(affinity) if affinity else (os.cpu_count() or 1)
    return max(1, int(detected or 1))


def effective_worker_count(task_count: int, max_workers: int | None = None) -> int:
    ceiling = int(
        getattr(config, "REPLAY_PROCESS_MAX_WORKERS", 4)
        if max_workers is None else max_workers
    )
    return max(1, min(max(1, int(task_count or 1)), available_cpu_count(), max(1, ceiling)))


def map_ordered(
    fn: Callable,
    items: Iterable,
    *,
    initializer: Callable | None = None,
    initargs: tuple = (),
    max_workers: int | None = None,
) -> list:
    """Map pure work in stable input order, falling back safely when process startup is unavailable."""
    rows = list(items)
    if not rows:
        return []
    workers = effective_worker_count(len(rows), max_workers=max_workers)
    if workers <= 1:
        if initializer is not None:
            initializer(*initargs)
        return [fn(item) for item in rows]
    try:
        # Scanner owns a small adaptive-pacing thread.  ``spawn`` avoids forking a multi-threaded process
        # while still scaling replay on Linux/macOS and remaining deterministic in tests.
        context = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=workers,
            mp_context=context,
            initializer=initializer,
            initargs=initargs,
        ) as executor:
            return list(executor.map(fn, rows, chunksize=1))
    except (OSError, RuntimeError, BrokenProcessPool):
        # Pure replay has no side effects, so a process-creation failure can be retried serially without
        # duplicating writes or changing selection semantics.
        if initializer is not None:
            initializer(*initargs)
        return [fn(item) for item in rows]
