"""Streaming primitives for cobra4: async iterables + windowing operators.

Wraps any async source (queue, generator, list) and offers the operators
most useful for cloud-data pipelines:

- :func:`from_async` — wrap an existing ``__aiter__`` source.
- :func:`from_iter` — wrap a sync iterable as a trivial async source.
- :func:`from_queue` — read continuously from a cobra4 ``queue("name")``.
- :class:`Stream` operators: ``map``, ``filter``, ``take``, ``window``,
  and the terminal ``collect`` / ``for_each``.

Use it like a tiny Reactive / Akka Streams:

.. code-block:: cobra4

    use cobra4.runtime.stream as s

    async fn main() {
        batches = await s.from_queue("orders")
            .window(tumbling=5.0)
            .map(fn(b) = analyze(b))
            .collect()
        log("done", n=len(batches))
    }

The intent is "small, composable, async-first" — not Apache Beam.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, AsyncIterator, Callable, Iterable


class Stream:
    """A wrapped async iterator with chainable operators."""

    __slots__ = ("_source",)

    def __init__(self, source: AsyncIterator[Any]) -> None:
        self._source = source

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._source

    # ----- intermediate operators -----

    def map(self, fn: Callable[[Any], Any]) -> "Stream":
        async def gen() -> AsyncIterator[Any]:
            async for x in self._source:
                r = fn(x)
                if inspect.isawaitable(r):
                    r = await r
                yield r

        return Stream(gen())

    def filter(self, pred: Callable[[Any], Any]) -> "Stream":
        async def gen() -> AsyncIterator[Any]:
            async for x in self._source:
                r = pred(x)
                if inspect.isawaitable(r):
                    r = await r
                if r:
                    yield x

        return Stream(gen())

    def take(self, n: int) -> "Stream":
        async def gen() -> AsyncIterator[Any]:
            i = 0
            async for x in self._source:
                if i >= n:
                    break
                yield x
                i += 1

        return Stream(gen())

    def window(
        self,
        *,
        tumbling: float | None = None,
        sliding: float | None = None,
        step: float | None = None,
        size: int | None = None,
    ) -> "Stream":
        """Group items into windows.

        - ``tumbling=5.0`` → non-overlapping 5s buckets.
        - ``sliding=10.0, step=5.0`` → 10s windows that shift every 5s.
        - ``size=100`` → fixed-count windows (emit on every N-th item).

        Each emitted window is a list of items. The last window flushes
        on source exhaustion even if not full.
        """
        if size is not None:
            return self._window_size(size)
        if tumbling is not None:
            return self._window_tumbling(tumbling)
        if sliding is not None and step is not None:
            return self._window_sliding(sliding, step)
        raise ValueError(
            "window(...) requires one of: size=N, tumbling=seconds, or "
            "sliding=seconds + step=seconds"
        )

    def _window_size(self, size: int) -> "Stream":
        async def gen() -> AsyncIterator[list]:
            buf: list = []
            async for x in self._source:
                buf.append(x)
                if len(buf) >= size:
                    yield buf
                    buf = []
            if buf:
                yield buf

        return Stream(gen())

    def _window_tumbling(self, seconds: float) -> "Stream":
        async def gen() -> AsyncIterator[list]:
            buf: list = []
            window_start = time.monotonic()
            async for x in self._source:
                buf.append(x)
                now = time.monotonic()
                if now - window_start >= seconds:
                    yield buf
                    buf = []
                    window_start = now
            if buf:
                yield buf

        return Stream(gen())

    def _window_sliding(self, length: float, step: float) -> "Stream":
        async def gen() -> AsyncIterator[list]:
            # Each item is timestamped; the window holds items in
            # [now - length, now]; emits on every `step` seconds.
            items: list[tuple[float, Any]] = []
            next_emit = time.monotonic() + step
            async for x in self._source:
                t = time.monotonic()
                items.append((t, x))
                # Drop stale
                items = [(ts, v) for (ts, v) in items if t - ts <= length]
                if t >= next_emit:
                    yield [v for _, v in items]
                    next_emit = t + step
            # Final window flush
            if items:
                yield [v for _, v in items]

        return Stream(gen())

    # ----- terminal operators -----

    async def collect(self) -> list:
        out: list = []
        async for x in self._source:
            out.append(x)
        return out

    async def for_each(self, fn: Callable[[Any], Any]) -> None:
        async for x in self._source:
            r = fn(x)
            if inspect.isawaitable(r):
                await r


# ----- sources -----


def from_async(source: AsyncIterator[Any]) -> Stream:
    """Wrap an existing async iterable as a :class:`Stream`."""
    return Stream(source)


def from_iter(source: Iterable[Any]) -> Stream:
    """Wrap a synchronous iterable as a trivial async :class:`Stream`."""

    async def gen() -> AsyncIterator[Any]:
        for x in source:
            yield x

    return Stream(gen())


def from_queue(name: str, *, timeout: float = 0.1) -> Stream:
    """Continuously poll a cobra4 ``queue(name)`` and yield events.

    The stream runs forever — combine with :meth:`Stream.take` or a
    cancel-aware caller to bound it.
    """
    from cobra4.runtime.schedule import queue as _queue

    q = _queue(name)

    async def gen() -> AsyncIterator[Any]:
        while True:
            ev = q.poll(timeout=timeout)
            if ev is None:
                # Yield control to the event loop so other tasks proceed.
                await asyncio.sleep(0)
                continue
            yield ev

    return Stream(gen())


__all__ = ["Stream", "from_async", "from_iter", "from_queue"]
