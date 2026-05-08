"""Tests for `async fn` / `await` and async-aware `each ... in parallel`."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

from cobra4.parser import parse
from cobra4 import ast_nodes as N
from cobra4.codegen import generate
from cobra4.runtime.concurrency import async_parallel_for


# ---------- parsing ----------


def test_parse_async_fn_sets_is_async() -> None:
    m = parse("async fn f() { return 1 }\n")
    assert isinstance(m.body[0], N.FnDecl)
    assert m.body[0].is_async is True


def test_parse_plain_fn_is_not_async() -> None:
    m = parse("fn f() { return 1 }\n")
    assert m.body[0].is_async is False


def test_parse_await_creates_await_node() -> None:
    m = parse("async fn f(c) { x = await c\n return x\n }\n")
    fn = m.body[0]
    assert isinstance(fn, N.FnDecl)
    assert fn.is_async
    assign = fn.block[0]
    assert isinstance(assign, N.Assign)
    assert isinstance(assign.value, N.Await)


# ---------- codegen ----------


def test_codegen_emits_async_def() -> None:
    out = generate(parse("async fn f() { return 1 }\n")).code
    assert "async def f" in out


def test_codegen_emits_plain_def_for_sync_fn() -> None:
    out = generate(parse("fn f() { return 1 }\n")).code
    assert "def f" in out and "async def" not in out.split("def f", 1)[0] + "def f"


def test_codegen_each_parallel_uses_async_helper_inside_async() -> None:
    src = (
        "async fn main(xs) {\n"
        "    return each x in xs in parallel(workers=3) { x }\n"
        "}\n"
    )
    out = generate(parse(src)).code
    assert "_c4_async_parallel_for" in out
    assert "await _c4_async_parallel_for" in out


def test_codegen_each_parallel_uses_sync_helper_outside_async() -> None:
    src = "fn main(xs) { return each x in xs in parallel { x } }\n"
    out = generate(parse(src)).code
    fn_body = out.split("def main", 1)[1]
    assert "_c4_async_parallel_for" not in fn_body
    assert "_c4_parallel_for" in fn_body


def test_codegen_strips_redundant_await_in_async_parallel_lambda() -> None:
    """Inside `each ... in parallel { await coro }` the `await` is
    redundant (the helper awaits) and would put `await` inside a sync
    lambda — SyntaxError. Codegen must strip it."""
    src = (
        "async fn main(xs) {\n"
        "    return each x in xs in parallel { await fetch_one(x) }\n"
        "}\n"
    )
    out = generate(parse(src)).code
    # Find the lambda body and confirm no `await` inside it.
    lam = out.split("lambda x: ", 1)[1].split(")", 1)[0]
    assert "await" not in lam, f"await leaked into lambda: {lam!r}"


# ---------- runtime ----------


def test_async_parallel_for_returns_results_in_order() -> None:
    async def coro_fn(x):
        await asyncio.sleep(0.001 * (5 - x))  # later items finish faster
        return x * 10

    out = asyncio.run(async_parallel_for([0, 1, 2, 3, 4], coro_fn, workers=4))
    assert out == [0, 10, 20, 30, 40]


def test_async_parallel_for_workers_caps_concurrency() -> None:
    """With workers=1, total time ≈ N * delay (serialized).
    With workers=N, total time ≈ delay (parallel).
    Use a 50ms delay × 4 items, allow generous margin."""
    DELAY = 0.05
    N_ITEMS = 4

    async def slow(x):
        await asyncio.sleep(DELAY)
        return x

    t0 = time.perf_counter()
    asyncio.run(async_parallel_for(range(N_ITEMS), slow, workers=1))
    serial = time.perf_counter() - t0

    t0 = time.perf_counter()
    asyncio.run(async_parallel_for(range(N_ITEMS), slow, workers=N_ITEMS))
    parallel = time.perf_counter() - t0

    assert serial > parallel * 1.5, f"serial {serial:.3f}s, parallel {parallel:.3f}s"


def test_async_parallel_for_handles_sync_callable() -> None:
    """If `fn` is sync (not a coroutine), `async_parallel_for` should
    still work — useful when mixing sync and async helpers."""
    def squared(x):
        return x * x
    out = asyncio.run(async_parallel_for([1, 2, 3], squared, workers=2))
    assert out == [1, 4, 9]


# ---------- end-to-end ----------


def _run_c4(tmp_path: Path, src: str) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_e2e_async_main(tmp_path: Path) -> None:
    src = (
        "use asyncio\n"
        "async fn fetch_one(url) {\n"
        "    await asyncio.sleep(0.001)\n"
        "    return \"ok-{url}\"\n"
        "}\n"
        "async fn main() {\n"
        "    r = await fetch_one(\"a\")\n"
        "    log(\"got\", r=r)\n"
        "}\n"
        "asyncio.run(main())\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "ok-a" in stderr


def test_e2e_async_each_in_parallel(tmp_path: Path) -> None:
    src = (
        "use asyncio\n"
        "async fn fetch(u) {\n"
        "    await asyncio.sleep(0.001)\n"
        "    return u\n"
        "}\n"
        "async fn main() {\n"
        "    rs = each x in [1, 2, 3, 4, 5] in parallel(workers=3) { await fetch(x) }\n"
        "    log(\"all\", n=len(rs), sum=sum(rs))\n"
        "}\n"
        "asyncio.run(main())\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "n=5" in stderr
    assert "sum=15" in stderr
