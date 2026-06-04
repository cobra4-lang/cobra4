"""In-process workflow runner for cobra4 — DAG of tasks with retries.

The cobra4 codegen turns a ``workflow X { ... }`` block into a series of
``Workflow.add(...)`` calls followed by ``Workflow.run()``. Tasks
declare their dependencies; the runner topologically sorts them and
executes in order. Each task can opt into retries / timeout.

This is the in-process MVP. A future iteration plugs into Temporal /
Argo / a custom worker pool — the public ``Workflow`` interface stays
stable.
"""

from __future__ import annotations

import time
import traceback
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class WorkflowError(Exception):
    """A workflow stopped because a task failed beyond its retry budget,
    or because the DAG has a cycle, or a dep wasn't declared."""


@dataclass
class _TaskNode:
    name: str
    fn: Callable[..., Any]
    deps: tuple[str, ...]
    retries: int = 0
    timeout: Optional[float] = None
    on_failure: Optional[Callable[[Exception], Any]] = None
    result: Any = None
    error: Optional[BaseException] = None
    attempts: int = 0


class Workflow:
    """Container for a DAG of named tasks.

    Usage from generated code:

    .. code-block:: python

        wf = Workflow("daily_etl")
        wf.add("raw", lambda: fetch(), deps=())
        wf.add("clean", lambda raw: clean(raw), deps=("raw",))
        results = wf.run()
        raw = results["raw"]; clean = results["clean"]
    """

    def __init__(self, name: str = "<unnamed>") -> None:
        self.name = name
        self._tasks: dict[str, _TaskNode] = {}
        self._order: list[str] = []  # insertion order

    def add(
        self,
        name: str,
        fn: Callable[..., Any],
        deps: tuple[str, ...] = (),
        *,
        retries: int = 0,
        timeout: Optional[float] = None,
        on_failure: Optional[Callable[[Exception], Any]] = None,
    ) -> "_TaskNode":
        if name in self._tasks:
            raise WorkflowError(f"task '{name}' redeclared in workflow {self.name!r}")
        for d in deps:
            if d not in self._tasks:
                raise WorkflowError(
                    f"task '{name}' depends on undeclared '{d}' "
                    f"(deps must be declared earlier in the workflow body)"
                )
        node = _TaskNode(
            name=name,
            fn=fn,
            deps=tuple(deps),
            retries=retries,
            timeout=timeout,
            on_failure=on_failure,
        )
        self._tasks[name] = node
        self._order.append(name)
        return node

    # ----- topological sort -----

    def _topo(self) -> list[str]:
        in_deg: dict[str, int] = {n: 0 for n in self._tasks}
        rev: dict[str, list[str]] = defaultdict(list)
        for name, node in self._tasks.items():
            for d in node.deps:
                rev[d].append(name)
                in_deg[name] += 1
        order: list[str] = []
        q: deque[str] = deque(n for n, d in in_deg.items() if d == 0)
        # Stable order: prefer the original declaration order.
        q = deque(sorted(q, key=self._order.index))
        while q:
            n = q.popleft()
            order.append(n)
            for m in sorted(rev[n], key=self._order.index):
                in_deg[m] -= 1
                if in_deg[m] == 0:
                    q.append(m)
        if len(order) != len(self._tasks):
            cycle = [n for n, d in in_deg.items() if d > 0]
            raise WorkflowError(
                f"workflow {self.name!r} has a dependency cycle involving: {cycle}"
            )
        return order

    # ----- execution -----

    def run(self) -> dict[str, Any]:
        """Execute all tasks. Returns a ``{name: result}`` dict.

        Raises :class:`WorkflowError` on cycle or final task failure
        (after retries / on_failure exhausted).
        """
        results: dict[str, Any] = {}
        for name in self._topo():
            node = self._tasks[name]
            args = tuple(results[d] for d in node.deps)
            self._run_one(node, args, results)
        return results

    def _run_one(
        self,
        node: _TaskNode,
        args: tuple,
        results: dict[str, Any],
    ) -> None:
        attempts_left = node.retries + 1
        last_exc: Optional[BaseException] = None
        while attempts_left > 0:
            attempts_left -= 1
            node.attempts += 1
            try:
                if node.timeout is not None:
                    node.result = _run_with_timeout(node.fn, args, node.timeout)
                else:
                    node.result = node.fn(*args)
                results[node.name] = node.result
                return
            except BaseException as e:  # noqa: BLE001
                last_exc = e
                node.error = e
                if attempts_left == 0:
                    break
                # Tiny exponential backoff so retries don't hot-loop.
                time.sleep(0.01 * (node.retries - attempts_left + 1))
        # All retries exhausted.
        if node.on_failure is not None:
            try:
                node.result = node.on_failure(last_exc)
                results[node.name] = node.result
                return
            except BaseException as recovery_exc:  # noqa: BLE001
                last_exc = recovery_exc
        raise WorkflowError(
            f"task {node.name!r} in workflow {self.name!r} failed after "
            f"{node.attempts} attempt(s):\n"
            f"{''.join(traceback.format_exception_only(type(last_exc), last_exc))}"
        ) from last_exc

    # ----- introspection -----

    def task_names(self) -> list[str]:
        return list(self._order)

    def deps_of(self, name: str) -> tuple[str, ...]:
        return self._tasks[name].deps


def _run_with_timeout(fn: Callable[..., Any], args: tuple, seconds: float) -> Any:
    """Run ``fn(*args)`` with a soft timeout. Uses signal.SIGALRM where
    available (Unix), falls back to a simple thread-based watchdog."""
    import threading

    result: list[Any] = [None]
    error: list[Optional[BaseException]] = [None]

    def target() -> None:
        try:
            result[0] = fn(*args)
        except BaseException as e:  # noqa: BLE001
            error[0] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise TimeoutError(f"task exceeded {seconds}s timeout")
    if error[0] is not None:
        raise error[0]
    return result[0]


__all__ = ["Workflow", "WorkflowError"]
