"""Tests for the post-M5 waves: syntax extensions, tooling, plugins, stdlib."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cobra4.parser import parse
from cobra4.lowering import lower
from cobra4.codegen import generate
from cobra4.tools.fmt import format_module
from cobra4.tools.repl import _is_incomplete


# ---------- Wave 1: syntax ----------


def transpile(src: str) -> str:
    return generate(lower(parse(src)), cobra4_path="<test>").code


def test_slice_basic():
    out = transpile("y = xs[1:5]\n")
    assert "xs[1:5]" in out


def test_slice_open_left():
    out = transpile("y = xs[:10]\n")
    assert "xs[:10]" in out


def test_slice_open_right():
    out = transpile("y = xs[2:]\n")
    assert "xs[2:]" in out


def test_slice_step():
    out = transpile("y = xs[0:10:2]\n")
    assert "xs[0:10:2]" in out


def test_each_where_filter():
    out = transpile("r = each x in xs where x > 0 { x * 2 }\n")
    assert "for x in xs if (x > 0)" in out


def test_for_where_filter():
    out = transpile("for x in xs where x > 0 { print(x) }\n")
    assert "if not ((x > 0))" in out
    assert "continue" in out


def test_match_or_pattern():
    out = transpile("match v { case 1 | 2 | 3 { foo() } case _ { bar() } }\n")
    assert "case 1 | 2 | 3:" in out


def test_match_guard():
    out = transpile("match v { case x if x > 100 { foo() } }\n")
    assert "case x if (x > 100):" in out


# ---------- Wave 2: tooling ----------


def test_repl_incomplete_detector():
    """Multi-line REPL keeps reading until braces are balanced."""
    from cobra4.parser import ParseError
    err = ParseError(message="...", line=1, column=1)
    assert _is_incomplete(err, "fn foo() {\n")
    assert not _is_incomplete(err, "fn foo() {\n}\n")
    assert _is_incomplete(err, "x = [1, 2,\n")
    assert not _is_incomplete(err, "x = [1, 2]\n")


def test_formatter_idempotent():
    """Running the formatter twice produces identical output."""
    src = textwrap_dedent("""
        fn double(x: int) -> int = x * 2

        for i in [1, 2, 3] where i > 1 {
            print(i)
        }
    """).strip() + "\n"
    once = format_module(parse(src))
    twice = format_module(parse(once))
    assert once == twice


def test_formatter_emits_braces():
    """Output uses { } not Python indentation."""
    out = format_module(parse("if x > 0 { foo() } else { bar() }\n"))
    assert "{" in out and "}" in out
    assert "if " in out and "else {" in out


# ---------- Wave 3: plugins ----------


def test_regex_plugin_rewrites_literal():
    from cobra4.plugins.builtin import regex as regex_plugin
    src = '''lang use regex
p = re"[a-z]+"i
'''
    from cobra4.plugins import preprocess
    res = preprocess(src)
    assert "re_compile(" in res.source
    assert "re.IGNORECASE" in res.source


def test_regex_plugin_runtime_compiles():
    from cobra4.plugins.builtin.regex import re_compile
    p = re_compile("[a-z]+", 0)
    assert p.match("hello")


def test_yaml_plugin_rewrites_triple_string():
    src = '''lang use yaml
config = yaml"""
key: value
"""
'''
    from cobra4.plugins import preprocess
    res = preprocess(src)
    assert "yaml_load(" in res.source


# ---------- Wave 5: deps + doc + stdlib ----------


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args, cwd=None):
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


def test_deps_add_list_remove():
    with tempfile.TemporaryDirectory() as d:
        p = _run_cli("deps", "add", "requests", "--version", "2.31.0", cwd=d)
        assert p.returncode == 0
        p = _run_cli("deps", "list", cwd=d)
        assert "requests" in p.stdout and "2.31.0" in p.stdout
        p = _run_cli("deps", "remove", "requests", cwd=d)
        assert p.returncode == 0


def test_deps_install_venv_uses_project_python(tmp_path, monkeypatch):
    from cobra4.cli import cmd_deps

    (tmp_path / "cobra4.toml").write_text('[deps]\nsix = "1.17.0"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_run(cmd, check=False):
        calls.append(("run", cmd, check))
        scripts = "Scripts" if os.name == "nt" else "bin"
        py_name = "python.exe" if os.name == "nt" else "python"
        py_path = tmp_path / ".venv-test" / scripts / py_name
        py_path.parent.mkdir(parents=True, exist_ok=True)
        py_path.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    def fake_call(cmd):
        calls.append(("call", cmd, None))
        return 0

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "call", fake_call)

    rc = cmd_deps(SimpleNamespace(action="install", name=None, version=None, venv=".venv-test"))

    assert rc == 0
    pip_call = calls[-1][1]
    assert pip_call[1:4] == ["-m", "pip", "install"]
    assert pip_call[-1] == "six==1.17.0"


def test_doc_extracts_signatures():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "lib.c4"
        src.write_text(
            'fn add(a: int, b: int) -> int {\n'
            '    "Sum of two integers."\n'
            '    return a + b\n'
            '}\n',
            encoding="utf-8",
        )
        p = _run_cli("doc", str(src), cwd=d)
        assert p.returncode == 0
        assert "## `fn add(a: int, b: int) -> int`" in p.stdout
        assert "Sum of two integers." in p.stdout


def test_stdlib_imports_via_hook():
    """The custom finder lets `cobra4.stdlib.X` resolve to cobra4/stdlib/X.c4."""
    import cobra4.stdlib  # installs the finder
    import cobra4.stdlib.json as cj
    import cobra4.stdlib.http as ch

    assert cj.dumps({"x": 1}) == '{"x": 1}'
    assert ch.ok(200) is True
    assert ch.ok(500) is False


def test_stdlib_list_modules():
    import cobra4.stdlib as stdlib
    mods = stdlib.list_modules()
    assert "http" in mods
    assert "json" in mods
    assert "fs" in mods


# ---------- Wave 5: examples that use everything together ----------


def test_example_08_stdlib_dogfood_runs():
    from cobra4.codegen import generate
    from cobra4.lowering import lower
    from cobra4.parser import parse

    src_path = PROJECT_ROOT / "examples" / "08_stdlib_dogfood.c4"
    src = src_path.read_text(encoding="utf-8")
    code = generate(lower(parse(src, source_path=str(src_path))), cobra4_path=str(src_path)).code
    with tempfile.TemporaryDirectory() as d:
        # Need the examples/ dir to exist relative to cwd for the example
        # to find files via fs.list_dir("examples", "*.c4").
        examples_dir = Path(d) / "examples"
        examples_dir.mkdir()
        for sf in (PROJECT_ROOT / "examples").glob("*.c4"):
            (examples_dir / sf.name).write_text(sf.read_text(encoding="utf-8"), encoding="utf-8")
        py = Path(d) / "out.py"
        py.write_text(code, encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, str(py)],
            cwd=d,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0


def textwrap_dedent(s: str) -> str:
    import textwrap
    return textwrap.dedent(s)
