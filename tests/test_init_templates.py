"""Tests for `c4 init` scaffolds.

Each template must be valid cobra4 (`c4 check` clean) and runnable
end-to-end without external services. Templates that can't run cleanly
without network/services must at least pass `c4 check`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cobra4 import templates as _templates


# ---------- Python-side template structure ----------


def test_all_templates_export_common_files() -> None:
    """Every template must include cobra4.toml, .gitignore, README.md
    and a runnable src/main.c4."""
    for name in _templates.TEMPLATES:
        files = _templates.render(name, "test-proj")
        for needed in (".gitignore", "cobra4.toml", "README.md", "src/main.c4"):
            assert needed in files, f"template {name} missing {needed}"


def test_render_unknown_template_raises() -> None:
    with pytest.raises(ValueError, match="unknown template"):
        _templates.render("nonexistent", "x")


def test_template_main_is_non_empty() -> None:
    for name in _templates.TEMPLATES:
        files = _templates.render(name, "test-proj")
        assert files["src/main.c4"].strip(), f"{name}: src/main.c4 empty"


# ---------- CLI-driven init tests ----------


def _c4_init(tmp_path: Path, template: str, name: str = "myproj") -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "init", name, "-t", template],
        capture_output=True, text=True, cwd=tmp_path,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _c4_check(file: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "check", str(file)],
        capture_output=True, text=True, cwd=file.parent,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _c4_run(file: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(file)],
        capture_output=True, text=True, cwd=file.parent.parent, timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.mark.parametrize("template", ["http-service", "etl-pipeline", "agent", "daemon"])
def test_init_creates_files(tmp_path: Path, template: str) -> None:
    code, _, stderr = _c4_init(tmp_path, template)
    assert code == 0, stderr
    assert (tmp_path / "myproj" / "src" / "main.c4").exists()
    assert (tmp_path / "myproj" / "cobra4.toml").exists()


@pytest.mark.parametrize("template", ["http-service", "etl-pipeline", "agent", "daemon"])
def test_init_main_passes_check(tmp_path: Path, template: str) -> None:
    """Every template's main.c4 must be valid cobra4 — `c4 check` exits 0."""
    _c4_init(tmp_path, template)
    main = tmp_path / "myproj" / "src" / "main.c4"
    code, stdout, stderr = _c4_check(main)
    assert code == 0, f"{template} check failed: {stderr or stdout}"


def test_init_etl_template_runs_end_to_end(tmp_path: Path) -> None:
    """The ETL template should run cleanly without network."""
    _c4_init(tmp_path, "etl-pipeline")
    main = tmp_path / "myproj" / "src" / "main.c4"
    code, _, stderr = _c4_run(main)
    assert code == 0, stderr
    assert "etl done" in stderr


def test_init_agent_template_runs_end_to_end_with_mock(tmp_path: Path) -> None:
    """The agent template uses MockProvider and should run offline."""
    _c4_init(tmp_path, "agent")
    main = tmp_path / "myproj" / "src" / "main.c4"
    code, _, stderr = _c4_run(main)
    assert code == 0, stderr
    assert "agent" in stderr


def test_init_refuses_existing_dir_without_force(tmp_path: Path) -> None:
    (tmp_path / "myproj").mkdir()
    code, _, stderr = _c4_init(tmp_path, "http-service")
    assert code != 0
    assert "already exists" in stderr


def test_init_force_allows_existing_dir(tmp_path: Path) -> None:
    (tmp_path / "myproj").mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "init", "myproj",
         "-t", "http-service", "--force"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "myproj" / "src" / "main.c4").exists()


def test_init_list_prints_templates() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "init", "--list"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    out = proc.stdout
    for name in _templates.TEMPLATES:
        assert name in out


# ---------- regression for the each-as-statement fix ----------


def test_each_as_statement_runs_body_assignments(tmp_path: Path) -> None:
    """Bug found while writing templates: `each r in xs { r["k"] = ... }`
    used to compile to a Python lambda that silently discarded the
    assignment. Confirm the for-loop fallback runs the side effects."""
    f = tmp_path / "prog.c4"
    f.write_text(
        'rows = [{"a": 1}, {"a": 2}, {"a": 3}]\n'
        'each r in rows { r["b"] = 99 }\n'
        'log("rows", v=rows)\n'
    )
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    # Each row got its 'b' key set
    assert "'b': 99" in proc.stderr
