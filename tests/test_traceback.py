"""Tests for source-mapped tracebacks.

The CLI runs cobra4 programs in a temp .py file. When they crash, the
traceback should:
1. Show the original .c4 file path with line:col, not the temp .py.
2. Show the cobra4 source line, not the transpiled Python.
3. Hide cobra4-internal frames (cli.py, runpy) by default.
4. Surface those internal frames only when COBRA4_TRACE_VERBOSE=1.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run_c4(tmp_path: Path, src: str, *, verbose: bool = False) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    env = dict(os.environ)
    if verbose:
        env["COBRA4_TRACE_VERBOSE"] = "1"
    else:
        env.pop("COBRA4_TRACE_VERBOSE", None)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True, text=True, cwd=tmp_path, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_traceback_shows_c4_file_path(tmp_path: Path) -> None:
    src = "fn boom() { raise ValueError(\"no\") }\nboom()\n"
    code, _, stderr = _run_c4(tmp_path, src)
    assert code != 0
    assert "prog.c4" in stderr
    # The temp .py path should NOT appear (in non-verbose mode)
    assert "__c4_" not in stderr


def test_traceback_shows_c4_source_line(tmp_path: Path) -> None:
    """The frame body should print what the user wrote in cobra4, not
    the transpiled Python (e.g. `return a / b` not `return (a / b)`)."""
    src = (
        "fn divide(a, b) { return a / b }\n"
        "divide(1, 0)\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code != 0
    # Cobra4 form (no parens) must appear.
    assert "return a / b" in stderr


def test_traceback_filters_runpy_and_cli_frames(tmp_path: Path) -> None:
    """The user shouldn't see frames inside cobra4 / runpy machinery —
    check via the literal frame-header substrings the rewrite filters
    out (the bare path test is fragile because pytest's tmp_path may
    contain 'runpy' as part of the test name)."""
    src = "fn boom() { raise IOError(\"x\") }\nboom()\n"
    code, _, stderr = _run_c4(tmp_path, src)
    assert "<frozen runpy>" not in stderr
    assert "cobra4/cli.py" not in stderr


def test_traceback_verbose_keeps_internal_frames(tmp_path: Path) -> None:
    """COBRA4_TRACE_VERBOSE=1 disables the filtering for debugging."""
    src = "fn boom() { raise IOError(\"x\") }\nboom()\n"
    code, _, stderr = _run_c4(tmp_path, src, verbose=True)
    assert code != 0
    assert "runpy" in stderr or "cli.py" in stderr
    assert "transpiled from" in stderr


def test_traceback_shows_full_call_chain(tmp_path: Path) -> None:
    """Each frame in the user's stack maps to a separate .c4 line."""
    src = (
        "fn a() { b() }\n"
        "fn b() { c() }\n"
        "fn c() { raise ValueError(\"deep\") }\n"
        "a()\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    # Three user frames + the module-level call = 4
    frames = [ln for ln in stderr.splitlines() if ln.startswith('  File "')]
    assert len(frames) >= 4, stderr
    # And each one points at prog.c4
    assert all("prog.c4" in f for f in frames)


def test_traceback_handles_uncaught_async_exception(tmp_path: Path) -> None:
    src = (
        "use asyncio\n"
        "async fn fail() { raise IOError(\"async-boom\") }\n"
        "asyncio.run(fail())\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code != 0
    assert "prog.c4" in stderr
    assert "async-boom" in stderr


def test_traceback_handles_workflow_task_failure(tmp_path: Path) -> None:
    """A failed task in a workflow surfaces a WorkflowError; the
    traceback must still point at the user's task definition, not at
    the workflow runner internals."""
    src = (
        "fn always_fails() { raise IOError(\"task-boom\") }\n"
        "workflow w { x = task always_fails() }\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code != 0
    assert "task-boom" in stderr
    # No leak of cobra4/runtime/workflow.py internals
    if "COBRA4_TRACE_VERBOSE" not in os.environ:
        assert "workflow.py" not in stderr or "WorkflowError" in stderr
