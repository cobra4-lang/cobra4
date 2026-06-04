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
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cobra4 import ast_nodes as N
from cobra4.parser import parse, ParseError
from cobra4.lowering import lower
from cobra4.codegen import generate
from cobra4.plugins import preprocess
from cobra4.source_map import SourceMap


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


@dataclass
class CompiledTestFile:
    path: Path
    code: object
    source_map: SourceMap
    source_lines: list[str]
    test_names: list[str]


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


def _compile_test_file(path: Path) -> CompiledTestFile:
    """Compile a test file and discover its test function names."""
    src = path.read_text(encoding="utf-8")
    pre = preprocess(src)
    module = parse(pre.source, source_path=str(path))
    test_names = sorted(
        s.name
        for s in module.body
        if isinstance(s, N.FnDecl)
        and s.name.startswith("test_")
        and not s.name.startswith("test__")
    )
    result = generate(lower(module), cobra4_path=str(path))
    py_src = result.code
    if pre.plugins:
        plugin_imports = "\n".join(
            f"from {p.runtime_module} import *  # plugin: {p.name}"
            for p in pre.plugins
            if p.runtime_module
        )
        if plugin_imports:
            py_src = plugin_imports + "\n" + py_src

    code = compile(py_src, str(path), "exec")
    return CompiledTestFile(
        path=path,
        code=code,
        source_map=result.source_map,
        source_lines=src.splitlines(),
        test_names=test_names,
    )


def _fresh_namespace(path: Path) -> dict[str, Any]:
    return {
        "__file__": str(path),
        "__name__": f"_c4test_{path.stem}",
        "__package__": None,
        "__cached__": None,
    }


def _exec_compiled(compiled: CompiledTestFile) -> dict[str, Any]:
    ns = _fresh_namespace(compiled.path)
    exec(compiled.code, ns)
    return ns


_FRAME_RE = re.compile(
    r'^(?P<prefix>\s*File ")(?P<file>[^"]+)(", line )(?P<line>\d+)(?P<rest>.*)$'
)


def _format_traceback(exc: BaseException, compiled: CompiledTestFile) -> str:
    raw = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).splitlines()
    out: list[str] = []
    i = 0
    test_path = str(compiled.path)

    def is_frame_header(line: str) -> bool:
        return line.startswith("  File ")

    def skip_frame_body(idx: int) -> int:
        idx += 1
        while (
            idx < len(raw)
            and not is_frame_header(raw[idx])
            and raw[idx].startswith(" ")
        ):
            idx += 1
        return idx

    while i < len(raw):
        line = raw[i]
        m = _FRAME_RE.match(line)
        if m:
            filename = m.group("file")
            if filename == test_path:
                py_line = int(m.group("line"))
                c4_line, c4_col = compiled.source_map.lookup_position(py_line, 0)
                if c4_line:
                    pos = f"{c4_line}" if c4_col == 0 else f"{c4_line}:{c4_col}"
                    out.append(
                        f'{m.group("prefix")}{test_path}", line {pos}{m.group("rest")}'
                    )
                    if 0 < c4_line <= len(compiled.source_lines):
                        out.append("    " + compiled.source_lines[c4_line - 1].strip())
                    i = skip_frame_body(i)
                    continue
            if filename.endswith("/cobra4/test_runner.py") or filename.endswith(
                "/cobra4/stdlib/test.c4"
            ):
                i = skip_frame_body(i)
                continue
        out.append(line)
        i += 1

    return "\n".join(out)


def _failure_result(
    path: Path,
    name: str,
    start: float,
    exc: BaseException,
    compiled: Optional[CompiledTestFile] = None,
    *,
    prefix: str = "",
) -> TestResult:
    error_message = f"{prefix}{type(exc).__name__}: {exc}"
    trace = (
        _format_traceback(exc, compiled)
        if compiled is not None
        else traceback.format_exc()
    )
    return TestResult(
        file=str(path),
        name=name,
        passed=False,
        duration_ms=(time.monotonic() - start) * 1000,
        error_message=error_message,
        error_trace=trace,
    )


def _run_single_test(compiled: CompiledTestFile, name: str) -> TestResult:
    start = time.monotonic()
    try:
        ns = _exec_compiled(compiled)
        setup = ns.get("setup")
        teardown = ns.get("teardown")
        fn = ns[name]
    except BaseException as e:  # noqa: BLE001
        return _failure_result(
            compiled.path, name, start, e, compiled, prefix="import "
        )

    failure: Optional[TestResult] = None
    try:
        if callable(setup):
            setup()
        fn()
    except BaseException as e:  # noqa: BLE001
        failure = _failure_result(compiled.path, name, start, e, compiled)
    finally:
        if callable(teardown):
            try:
                teardown()
            except BaseException as e:  # noqa: BLE001
                teardown_failure = _failure_result(
                    compiled.path,
                    name,
                    start,
                    e,
                    compiled,
                    prefix="teardown ",
                )
                if failure is None:
                    failure = teardown_failure
                else:
                    failure.error_trace = (
                        (failure.error_trace or "")
                        + "\n\nDuring teardown:\n"
                        + (teardown_failure.error_trace or "")
                    )

    if failure is not None:
        return failure
    return TestResult(
        file=str(compiled.path),
        name=name,
        passed=True,
        duration_ms=(time.monotonic() - start) * 1000,
    )


def run_file(path: Path) -> list[TestResult]:
    """Compile and run all test_* functions in a file."""
    try:
        compiled = _compile_test_file(path)
    except (ParseError, SyntaxError, ValueError) as e:
        return [
            TestResult(
                file=str(path),
                name="<compile>",
                passed=False,
                duration_ms=0.0,
                error_message=str(e),
            )
        ]
    except BaseException as e:  # noqa: BLE001 — module-level exec failure
        return [
            TestResult(
                file=str(path),
                name="<import>",
                passed=False,
                duration_ms=0.0,
                error_message=str(e),
                error_trace=traceback.format_exc(),
            )
        ]

    return [_run_single_test(compiled, name) for name in compiled.test_names]


def run(
    paths: list[str], *, verbose: bool = False, junit_xml: Optional[str] = None
) -> TestRunSummary:
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
                sys.stdout.write(
                    f"  [{marker}] {rel}::{r.name} ({r.duration_ms:.1f}ms)\n"
                )
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

    ts = ET.Element(
        "testsuite",
        attrib={
            "name": "cobra4",
            "tests": str(summary.total),
            "failures": str(summary.failed),
        },
    )
    for r in summary.results:
        case = ET.SubElement(
            ts,
            "testcase",
            attrib={
                "classname": r.file,
                "name": r.name,
                "time": f"{r.duration_ms / 1000:.3f}",
            },
        )
        if not r.passed:
            f = ET.SubElement(
                case, "failure", attrib={"message": r.error_message or ""}
            )
            f.text = r.error_trace or ""
    ET.ElementTree(ts).write(path, encoding="utf-8", xml_declaration=True)
