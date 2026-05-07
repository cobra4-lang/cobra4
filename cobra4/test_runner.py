"""Native cobra4 test runner — `c4 test`.

Discovers ``tests/test_*.c4`` (plus a few standard alternates like
``test/`` and ``tests/`` at the project root) and runs every top-level
function whose name starts with ``test_`` as an isolated test case.

A test passes if the function returns without raising. Failures are
surfaced with the exception message and source location of the failing
``assert_*`` call (when raised by ``cobra4.stdlib.test``).

Output mimics pytest at-a-glance: ``.`` per pass, ``F`` per fail, plus
a summary block. JUnit XML output via ``--junit-xml=PATH`` for CI.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cobra4.parser import parse, ParseError
from cobra4.lowering import lower
from cobra4.codegen import generate
from cobra4.plugins import preprocess


@dataclass
class TestResult:
    file: str
    name: str
    passed: bool
    duration_ms: float
    error_message: Optional[str] = None
    error_trace: Optional[str] = None


@dataclass
class TestRunSummary:
    results: list[TestResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def total(self) -> int:
        return len(self.results)


def discover(roots: list[str]) -> list[Path]:
    """Find all ``test_*.c4`` files under each root."""
    seen: set[Path] = set()
    for r in roots:
        root = Path(r)
        if root.is_file() and root.suffix == ".c4" and root.stem.startswith("test_"):
            seen.add(root.resolve())
            continue
        if not root.is_dir():
            continue
        for p in root.rglob("test_*.c4"):
            if "__pycache__" in p.parts:
                continue
            seen.add(p.resolve())
    return sorted(seen)


def _compile_test_file(path: Path) -> tuple[dict, list[str]]:
    """Compile a test file and return (namespace_after_exec, list_of_test_names)."""
    src = path.read_text(encoding="utf-8")
    pre = preprocess(src)
    module = parse(pre.source, source_path=str(path))
    py_src = generate(lower(module), cobra4_path=str(path)).code
    if pre.plugins:
        plugin_imports = "\n".join(
            f"from {p.runtime_module} import *  # plugin: {p.name}"
            for p in pre.plugins if p.runtime_module
        )
        if plugin_imports:
            py_src = plugin_imports + "\n" + py_src

    ns: dict[str, Any] = {"__file__": str(path), "__name__": f"_c4test_{path.stem}"}
    code = compile(py_src, str(path), "exec")
    exec(code, ns)

    # Collect callable names starting with `test_`.
    test_names = sorted(
        n for n, v in ns.items()
        if n.startswith("test_") and callable(v) and not n.startswith("test__")
    )
    return ns, test_names


def run_file(path: Path) -> list[TestResult]:
    """Compile and run all test_* functions in a file."""
    results: list[TestResult] = []
    try:
        ns, names = _compile_test_file(path)
    except (ParseError, SyntaxError) as e:
        return [TestResult(
            file=str(path),
            name="<compile>",
            passed=False,
            duration_ms=0.0,
            error_message=str(e),
        )]
    except BaseException as e:  # noqa: BLE001 — module-level exec failure
        return [TestResult(
            file=str(path),
            name="<import>",
            passed=False,
            duration_ms=0.0,
            error_message=str(e),
            error_trace=traceback.format_exc(),
        )]

    # Optional setup/teardown hooks.
    setup = ns.get("setup")
    teardown = ns.get("teardown")

    for name in names:
        fn = ns[name]
        start = time.monotonic()
        try:
            if callable(setup):
                setup()
            fn()
            results.append(TestResult(
                file=str(path), name=name, passed=True,
                duration_ms=(time.monotonic() - start) * 1000,
            ))
        except BaseException as e:  # noqa: BLE001
            results.append(TestResult(
                file=str(path), name=name, passed=False,
                duration_ms=(time.monotonic() - start) * 1000,
                error_message=f"{type(e).__name__}: {e}",
                error_trace=traceback.format_exc(),
            ))
        finally:
            if callable(teardown):
                try:
                    teardown()
                except BaseException:  # noqa: BLE001
                    pass
    return results


def run(paths: list[str], *, verbose: bool = False, junit_xml: Optional[str] = None) -> TestRunSummary:
    """Run all tests under ``paths``. Print results, optionally write JUnit XML."""
    files = discover(paths or ["tests", "test"])
    if not files:
        sys.stdout.write("no tests found\n")
        return TestRunSummary()

    summary = TestRunSummary()
    start = time.monotonic()

    for path in files:
        rel = os.path.relpath(path)
        results = run_file(path)
        summary.results.extend(results)
        if verbose:
            for r in results:
                marker = "PASS" if r.passed else "FAIL"
                sys.stdout.write(f"  [{marker}] {rel}::{r.name} ({r.duration_ms:.1f}ms)\n")
                if not r.passed and r.error_message:
                    sys.stdout.write(f"      {r.error_message}\n")
        else:
            line = ""
            for r in results:
                line += "." if r.passed else "F"
            sys.stdout.write(f"{rel} {line}\n")

    elapsed = time.monotonic() - start
    sys.stdout.write("\n")
    if summary.failed:
        sys.stdout.write("=" * 60 + "\n")
        for r in summary.results:
            if not r.passed:
                sys.stdout.write(f"FAIL {os.path.relpath(r.file)}::{r.name}\n")
                sys.stdout.write(f"  {r.error_message}\n")
                if r.error_trace:
                    for tline in r.error_trace.splitlines()[-5:]:
                        sys.stdout.write(f"  {tline}\n")
                sys.stdout.write("\n")
    sys.stdout.write(
        f"{summary.passed} passed, {summary.failed} failed "
        f"in {elapsed:.2f}s ({summary.total} total)\n"
    )

    if junit_xml:
        _write_junit_xml(summary, junit_xml)

    return summary


def _write_junit_xml(summary: TestRunSummary, path: str) -> None:
    import xml.etree.ElementTree as ET

    ts = ET.Element("testsuite", attrib={
        "name": "cobra4",
        "tests": str(summary.total),
        "failures": str(summary.failed),
    })
    for r in summary.results:
        case = ET.SubElement(ts, "testcase", attrib={
            "classname": r.file,
            "name": r.name,
            "time": f"{r.duration_ms / 1000:.3f}",
        })
        if not r.passed:
            f = ET.SubElement(case, "failure", attrib={"message": r.error_message or ""})
            f.text = r.error_trace or ""
    ET.ElementTree(ts).write(path, encoding="utf-8", xml_declaration=True)
