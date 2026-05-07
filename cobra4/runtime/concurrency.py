"""Parallel fan-out helper used by ``each x in xs in parallel { ... }``.

Uses a thread pool by default — appropriate for IO-bound cobra4 workloads
(HTTP, SSH, S3). CPU-bound workloads can opt into processes via
``each x in xs in parallel(mode="process") { ... }``.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from typing import Any, Callable, Iterable


def parallel_for(
    items: Iterable[Any],
    fn: Callable[[Any], Any],
    *,
    workers: int | None = None,
    mode: str = "thread",
    timeout: float | None = None,
) -> list[Any]:
    """Apply ``fn`` to each item in ``items`` concurrently and collect results.

    Order of results matches order of input (best-effort).

    - ``workers``: pool size. Defaults to ``min(len(items), os.cpu_count() * 5)``
      for thread mode, ``os.cpu_count()`` for process mode.
    - ``mode``: ``"thread"`` (default) or ``"process"``.
    - ``timeout``: per-task timeout in seconds.

    Empty input returns ``[]`` without spinning up a pool.
    """
    items_list = list(items)
    if not items_list:
        return []

    if workers is None:
        cpu = os.cpu_count() or 4
        workers = min(len(items_list), cpu * 5) if mode == "thread" else cpu
    workers = max(1, workers)

    Executor = ProcessPoolExecutor if mode == "process" else ThreadPoolExecutor
    if workers == 1:
        # Skip the pool entirely — preserves stack traces.
        return [fn(x) for x in items_list]

    with Executor(max_workers=workers) as ex:
        futures = [ex.submit(fn, x) for x in items_list]
        return [f.result(timeout=timeout) for f in futures]
