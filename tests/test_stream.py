"""Tests for `cobra4.runtime.stream` — async iterables + windowing."""

from __future__ import annotations

import asyncio
import time

import pytest

from cobra4.runtime.stream import Stream, from_async, from_iter


# ---------- sources ----------


def test_from_iter_collects_all_items() -> None:
    out = asyncio.run(from_iter([1, 2, 3, 4]).collect())
    assert out == [1, 2, 3, 4]


def test_from_iter_empty() -> None:
    assert asyncio.run(from_iter([]).collect()) == []


def test_from_async_works_with_user_async_generator() -> None:
    async def gen():
        for i in range(3):
            yield i * 10
    out = asyncio.run(from_async(gen()).collect())
    assert out == [0, 10, 20]


# ---------- map / filter / take ----------


def test_map_applies_sync_callable() -> None:
    out = asyncio.run(from_iter([1, 2, 3]).map(lambda x: x * 10).collect())
    assert out == [10, 20, 30]


def test_map_applies_async_callable() -> None:
    async def doubler(x):
        return x * 2
    out = asyncio.run(from_iter([1, 2, 3]).map(doubler).collect())
    assert out == [2, 4, 6]


def test_filter_drops_falsy() -> None:
    out = asyncio.run(from_iter([1, 2, 3, 4, 5]).filter(lambda x: x % 2 == 0).collect())
    assert out == [2, 4]


def test_take_limits_to_n() -> None:
    out = asyncio.run(from_iter([1, 2, 3, 4, 5]).take(3).collect())
    assert out == [1, 2, 3]


def test_take_more_than_available_returns_all() -> None:
    out = asyncio.run(from_iter([1, 2]).take(10).collect())
    assert out == [1, 2]


# ---------- windowing ----------


def test_window_size_groups_into_buckets() -> None:
    out = asyncio.run(from_iter([1, 2, 3, 4, 5, 6, 7]).window(size=3).collect())
    assert out == [[1, 2, 3], [4, 5, 6], [7]]


def test_window_size_exact_multiple_no_partial_window() -> None:
    out = asyncio.run(from_iter([1, 2, 3, 4]).window(size=2).collect())
    assert out == [[1, 2], [3, 4]]


def test_window_size_one_yields_singletons() -> None:
    out = asyncio.run(from_iter([1, 2, 3]).window(size=1).collect())
    assert out == [[1], [2], [3]]


def test_window_tumbling_groups_by_time() -> None:
    """Source yields immediately; tumbling=0 emits one window per item.
    Smoke check that the operator wires up correctly."""
    async def gen():
        for x in range(4):
            yield x
            await asyncio.sleep(0.001)

    out = asyncio.run(from_async(gen()).window(tumbling=0.0).collect())
    # With near-zero delays, each item is its own window
    assert sum(len(w) for w in out) == 4
    assert all(len(w) >= 1 for w in out)


def test_window_without_args_raises() -> None:
    with pytest.raises(ValueError):
        from_iter([1, 2, 3]).window()


def test_window_size_with_partial_final_window() -> None:
    out = asyncio.run(from_iter(range(5)).window(size=10).collect())
    assert out == [[0, 1, 2, 3, 4]]


# ---------- chaining ----------


def test_chain_filter_map_window_collect() -> None:
    """Realistic data-pipeline shape: filter, transform, group, collect."""
    out = asyncio.run(
        from_iter(range(10))
        .filter(lambda x: x % 2 == 0)        # 0, 2, 4, 6, 8
        .map(lambda x: x * x)                # 0, 4, 16, 36, 64
        .window(size=2)
        .collect()
    )
    assert out == [[0, 4], [16, 36], [64]]


def test_for_each_invokes_sink_for_every_item() -> None:
    seen: list[int] = []
    asyncio.run(from_iter([1, 2, 3]).for_each(lambda x: seen.append(x)))
    assert seen == [1, 2, 3]


def test_for_each_handles_async_sink() -> None:
    seen: list[int] = []
    async def sink(x):
        await asyncio.sleep(0)
        seen.append(x * 10)
    asyncio.run(from_iter([1, 2, 3]).for_each(sink))
    assert seen == [10, 20, 30]
