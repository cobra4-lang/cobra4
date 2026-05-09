"""Built-in micro-benchmarks for cobra4.

Run with::

    c4 bench               # all benchmarks
    c4 bench parser        # subset
    c4 bench --json out.json
    c4 bench --compare baseline.json

Each benchmark reports:
- ``ops/s``: operations per second
- ``mean_us``: mean latency per op (microseconds)
- ``p50``, ``p95``: latency percentiles

The benchmark runner runs each target for a fixed wall-time budget
(default 1 second) and reports stable percentiles. No external deps —
this is meant to be the pre-CI sanity check, not a Criterion clone.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class BenchResult:
    name: str
    iterations: int = 0
    total_seconds: float = 0.0
    samples: list[float] = field(default_factory=list)  # per-op seconds

    @property
    def ops_per_sec(self) -> float:
        return self.iterations / self.total_seconds if self.total_seconds > 0 else 0.0

    @property
    def mean_us(self) -> float:
        return (self.total_seconds / self.iterations) * 1e6 if self.iterations else 0.0

    @property
    def p50_us(self) -> float:
        return statistics.median(self.samples) * 1e6 if self.samples else 0.0

    @property
    def p95_us(self) -> float:
        if not self.samples:
            return 0.0
        s = sorted(self.samples)
        idx = max(0, int(len(s) * 0.95) - 1)
        return s[idx] * 1e6

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "iterations": self.iterations,
            "ops_per_sec": round(self.ops_per_sec, 1),
            "mean_us": round(self.mean_us, 2),
            "p50_us": round(self.p50_us, 2),
            "p95_us": round(self.p95_us, 2),
        }


def time_budget(name: str, fn: Callable[[], None], *, seconds: float = 1.0) -> BenchResult:
    """Run ``fn`` repeatedly for at most ``seconds``. Records per-op
    timings until budget is spent (capped at 10k iterations to keep
    ``samples`` from blowing up RAM)."""
    fn()  # warm-up
    out = BenchResult(name=name)
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline and out.iterations < 10_000:
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        out.samples.append(dt)
        out.iterations += 1
    out.total_seconds = sum(out.samples)
    return out


# ---------- benchmark targets ----------


def _bench_parser() -> Callable[[], None]:
    from cobra4.parser import parse
    src = (
        "fn fizzbuzz(n) {\n"
        "    out = []\n"
        "    for i in range(1, n + 1) {\n"
        "        if i % 15 == 0 { out.append(\"FizzBuzz\") }\n"
        "        elif i % 3 == 0 { out.append(\"Fizz\") }\n"
        "        elif i % 5 == 0 { out.append(\"Buzz\") }\n"
        "        else { out.append(str(i)) }\n"
        "    }\n"
        "    return out\n"
        "}\n"
    )

    def run():
        parse(src, source_path="<bench>")
    return run


def _bench_codegen() -> Callable[[], None]:
    from cobra4.parser import parse
    from cobra4.codegen import generate
    src = "fn add(a, b) = a + b\nfn main() { return add(1, 2) }\n"
    module = parse(src)

    def run():
        generate(module)
    return run


def _bench_smart_dispatch() -> Callable[[], None]:
    """`read("./x.json")` on a registered handler — fast-path with cache."""
    from cobra4.runtime import read
    from cobra4.runtime.smart import make_smart

    sf = make_smart("bench_target", default=lambda x: x)
    sf.register(fn=lambda x: x, type=str, ext="json", scheme="file", name="json-h")

    def run():
        sf("./x.json")
    return run


def _bench_workflow_startup() -> Callable[[], None]:
    """Build a 50-task DAG and run it through the workflow runner."""
    from cobra4.runtime.workflow import Workflow

    def run():
        wf = Workflow("bench")
        wf.add("a", lambda: 1, deps=())
        prev = "a"
        for i in range(1, 50):
            name = f"t{i}"
            wf.add(name, lambda v: v + 1, deps=(prev,))
            prev = name
        wf.run()
    return run


def _bench_async_parallel() -> Callable[[], None]:
    """asyncio.gather of 100 trivial coroutines via `async_parallel_for`."""
    import asyncio
    from cobra4.runtime.concurrency import async_parallel_for

    async def double(x: int) -> int:
        return x * 2

    def run():
        asyncio.run(async_parallel_for(range(100), double, workers=20))
    return run


_TARGETS: dict[str, Callable[[], Callable[[], None]]] = {
    "parser":         _bench_parser,
    "codegen":        _bench_codegen,
    "smart-dispatch": _bench_smart_dispatch,
    "workflow":       _bench_workflow_startup,
    "async-parallel": _bench_async_parallel,
}


# ---------- runner ----------


def run_benchmarks(
    names: list[str] | None = None,
    *,
    seconds: float = 1.0,
) -> list[BenchResult]:
    targets = list(_TARGETS) if not names else [n for n in names if n in _TARGETS]
    if names:
        unknown = set(names) - set(_TARGETS)
        if unknown:
            print(f"warning: unknown benchmark target(s): {sorted(unknown)}", file=sys.stderr)
            print(f"available: {sorted(_TARGETS)}", file=sys.stderr)
    out: list[BenchResult] = []
    for name in targets:
        fn = _TARGETS[name]()
        out.append(time_budget(name, fn, seconds=seconds))
    return out


# ---------- output formatting ----------


def format_table(results: list[BenchResult]) -> str:
    cols = ["target", "iters", "ops/s", "mean µs", "p50 µs", "p95 µs"]
    rows = [
        [
            r.name, str(r.iterations),
            f"{r.ops_per_sec:,.0f}",
            f"{r.mean_us:.1f}",
            f"{r.p50_us:.1f}",
            f"{r.p95_us:.1f}",
        ]
        for r in results
    ]
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
    sep = "  ".join("-" * w for w in widths)
    header = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    body = "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows)
    return "\n".join([header, sep, body])


def format_compare(current: list[BenchResult], baseline: list[dict]) -> str:
    by_name = {b["name"]: b for b in baseline}
    cols = ["target", "ops/s now", "ops/s base", "Δ%"]
    rows = []
    for r in current:
        b = by_name.get(r.name, {})
        base_ops = float(b.get("ops_per_sec", 0))
        now_ops = r.ops_per_sec
        delta = ((now_ops - base_ops) / base_ops * 100) if base_ops else 0.0
        rows.append([
            r.name,
            f"{now_ops:,.0f}",
            f"{base_ops:,.0f}",
            f"{delta:+.1f}",
        ])
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
    sep = "  ".join("-" * w for w in widths)
    header = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    body = "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows)
    return "\n".join([header, sep, body])


# ---------- CLI handler ----------


def cli_main(targets: list[str], *, seconds: float, json_path: str | None,
             compare_path: str | None) -> int:
    results = run_benchmarks(targets if targets else None, seconds=seconds)

    if compare_path:
        baseline = json.loads(open(compare_path).read())
        print(format_compare(results, baseline))
    else:
        print(format_table(results))

    if json_path:
        with open(json_path, "w") as f:
            json.dump([r.to_dict() for r in results], f, indent=2)
        print(f"\nresults written to {json_path}")
    return 0


__all__ = [
    "BenchResult", "run_benchmarks", "format_table", "format_compare", "cli_main",
]
