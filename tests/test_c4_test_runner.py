"""Regression tests for the native `c4 test` runner."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_c4_test(tmp_path: Path, src: str) -> subprocess.CompletedProcess:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.c4").write_text(textwrap.dedent(src).lstrip(), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "test", "-v"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_c4_test_isolates_module_state_between_tests(tmp_path: Path) -> None:
    proc = _run_c4_test(
        tmp_path,
        """
        use cobra4.stdlib.test as t
        state = []

        fn test_a_mutates() {
            state.append(1)
        }

        fn test_b_expects_clean() {
            t.assert_eq(len(state), 0)
        }
        """,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed, 0 failed" in proc.stdout


def test_c4_test_fails_when_teardown_fails(tmp_path: Path) -> None:
    proc = _run_c4_test(
        tmp_path,
        """
        use cobra4.stdlib.test as t

        fn teardown() {
            t.fail("cleanup failed")
        }

        fn test_ok() {
            t.assert_true(True)
        }
        """,
    )

    assert proc.returncode == 1
    assert "teardown AssertionFailed: cleanup failed" in proc.stdout
    assert "0 passed, 1 failed" in proc.stdout


def test_c4_test_failure_traceback_uses_cobra4_source_lines(tmp_path: Path) -> None:
    proc = _run_c4_test(
        tmp_path,
        """
        use cobra4.stdlib.test as t

        fn test_failure() {
            t.assert_eq(1, 2)
        }
        """,
    )

    assert proc.returncode == 1
    assert 'test_sample.c4", line 4' in proc.stdout
    assert "t.assert_eq(1, 2)" in proc.stdout
    assert "line 27" not in proc.stdout


def test_c4_test_unknown_plugin_is_compile_failure(tmp_path: Path) -> None:
    proc = _run_c4_test(
        tmp_path,
        """
        lang use nope
        fn test_never_runs() = 1
        """,
    )

    assert proc.returncode == 1
    assert "test_sample.c4::<compile>" in proc.stdout
    assert "unknown language plugin 'nope'" in proc.stdout
    assert "Traceback" not in proc.stdout
