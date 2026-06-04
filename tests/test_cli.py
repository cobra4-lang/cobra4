"""CLI smoke tests."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "cobra4.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_version():
    p = _run_cli("--version")
    assert p.returncode == 0
    assert "cobra4" in p.stdout


def test_cli_build_to_stdout():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "x.c4"
        src.write_text("x = 1\n", encoding="utf-8")
        p = _run_cli("build", str(src), cwd=d)
        assert p.returncode == 0
        assert "x = 1" in p.stdout


def test_cli_check_ok():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "x.c4"
        src.write_text("x = 1\nfn f() { return x }\n", encoding="utf-8")
        p = _run_cli("check", str(src), cwd=d)
        assert p.returncode == 0


def test_cli_check_parse_error():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "x.c4"
        src.write_text("fn f(\n", encoding="utf-8")  # truncated
        p = _run_cli("check", str(src), cwd=d)
        assert p.returncode != 0
        assert "error" in p.stderr.lower()


def test_cli_run_simple():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "x.c4"
        src.write_text('print("hello cobra4")\n', encoding="utf-8")
        p = _run_cli("run", str(src), cwd=d)
        assert p.returncode == 0
        assert "hello cobra4" in p.stdout


def test_cli_run_resets_and_forwards_argv():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "x.c4"
        src.write_text(
            "use sys\n"
            "print(sys.argv[0].endswith(\"x.c4\"))\n"
            "print(\"|\".join(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        p = _run_cli("run", str(src), "--", "--name", "ada", "pos", cwd=d)
        assert p.returncode == 0
        assert p.stdout.splitlines() == ["True", "--name|ada|pos"]


def test_cli_fmt_validates():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "x.c4"
        src.write_text("x = 1   \n", encoding="utf-8")  # trailing spaces
        p = _run_cli("fmt", str(src), cwd=d)
        assert p.returncode == 0
        assert "x = 1\n" in p.stdout
