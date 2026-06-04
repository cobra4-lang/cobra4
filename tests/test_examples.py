"""End-to-end smoke tests for ``examples/*.c4``.

These tests transpile each example, run it via ``runpy``, and check that
it completes without error. They write to/read from temporary files
configured per-example.
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from cobra4.codegen import generate
from cobra4.lowering import lower
from cobra4.parser import parse


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = PROJECT_ROOT / "examples"


def _transpile(c4_path: Path) -> str:
    src = c4_path.read_text(encoding="utf-8")
    return generate(lower(parse(src, source_path=str(c4_path))), cobra4_path=str(c4_path)).code


def _run_example(c4_path: Path, cwd: Path) -> int:
    """Transpile and execute under a fresh subprocess with cwd=cwd."""
    code = _transpile(c4_path)
    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix=f"{c4_path.stem}_",
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=cwd,
    ) as tmp:
        tmp.write(code)
        py_path = tmp.name
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, py_path],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.returncode
    finally:
        os.unlink(py_path)


def test_example_01_wordcount():
    with tempfile.TemporaryDirectory() as d:
        # Provide a README.md the example can read.
        (Path(d) / "README.md").write_text("hello world hello world cobra4 rocks\n", encoding="utf-8")
        assert _run_example(EXAMPLES / "01_wordcount.c4", Path(d)) == 0
        assert (Path(d) / "out_wordcount.json").exists()


def test_example_03_etl():
    with tempfile.TemporaryDirectory() as d:
        assert _run_example(EXAMPLES / "03_etl.c4", Path(d)) == 0
        assert (Path(d) / "_etl_output.json").exists()


def test_example_04_serve():
    with tempfile.TemporaryDirectory() as d:
        assert _run_example(EXAMPLES / "04_serve.c4", Path(d)) == 0


def test_example_05_schedule():
    with tempfile.TemporaryDirectory() as d:
        assert _run_example(EXAMPLES / "05_schedule.c4", Path(d)) == 0


@pytest.mark.skipif(
    os.environ.get("CI") == "true" or os.environ.get("COBRA4_OFFLINE") == "1",
    reason="healthcheck example needs network; skip in CI/offline",
)
def test_example_02_healthcheck_offline_only():
    # Run only when network is available locally; skip in CI.
    with tempfile.TemporaryDirectory() as d:
        # Don't fail on non-zero exit: the example exits 0 even if some
        # hosts are unreachable (it logs warnings).
        assert _run_example(EXAMPLES / "02_healthcheck.c4", Path(d)) == 0


def test_example_09_log_analyzer_runs_via_cli():
    """The CLI must not leak `c4 run ...` arguments into user argparse."""
    with tempfile.TemporaryDirectory() as d:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "cobra4.cli", "run", str(EXAMPLES / "09_log_analyzer.c4")],
            cwd=d,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert (Path(d) / "_log_report.json").exists()
