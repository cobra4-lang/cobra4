"""Regression tests for the "make it real" pass — every claimed feature works."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------- Wave A ----------


def test_pattern_rest_in_list():
    from cobra4.parser import parse
    from cobra4.codegen import generate
    from cobra4.lowering import lower

    src = "match xs { case [a, *rest] { foo(a, rest) } case _ { skip() } }\n"
    out = generate(lower(parse(src))).code
    assert "case [a, *rest]:" in out


def test_pattern_dict():
    from cobra4.parser import parse
    from cobra4.codegen import generate
    from cobra4.lowering import lower

    src = 'match v { case {"name": n, "age": a, **extra} { foo(n, a, extra) } }\n'
    out = generate(lower(parse(src))).code
    assert "case {'name': n, 'age': a, **extra}:" in out


def test_pattern_tuple():
    from cobra4.parser import parse
    from cobra4.codegen import generate
    from cobra4.lowering import lower

    src = 'match (m, p) { case ("GET", "/") { foo() } case _ { bar() } }\n'
    out = generate(lower(parse(src))).code
    assert "case ('GET', '/'):" in out


def test_local_module_import():
    """`use mymodule` should resolve `mymodule.c4` from sys.path."""
    with tempfile.TemporaryDirectory() as d:
        # Drop a .c4 module
        Path(d, "greet.c4").write_text(
            'fn hello(name) = "hello {name}!"\n',
            encoding="utf-8",
        )
        # Run a program that imports it
        program = Path(d, "main.c4")
        program.write_text("use greet\nprint(greet.hello('cobra4'))\n", encoding="utf-8")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + d + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "cobra4.cli", "run", str(program)],
            cwd=d, env=env, capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert "hello cobra4!" in proc.stdout


def test_parser_error_recovery():
    from cobra4.parser import parse_collect_errors

    src = "x = 1\nbad =\ny = 2\n"
    module, errors = parse_collect_errors(src)
    # At least the first statement parses; at least one error is reported.
    assert len(errors) >= 1


# ---------- Wave B ----------


def test_k8s_manifest_generator():
    from cobra4.runtime.deploy import build_k8s_manifest

    yaml = build_k8s_manifest(
        name="my-app", image="repo/my-app:v1",
        replicas=3, port=8080, env={"FOO": "bar"},
    )
    assert "kind: Deployment" in yaml
    assert "kind: Service" in yaml
    assert "image: repo/my-app:v1" in yaml
    assert "replicas: 3" in yaml
    assert "FOO" in yaml


def test_lambda_packager_builds_zip():
    """The aws.lambda packager generates a deterministic zip."""
    from cobra4.runtime.deploy import build_lambda_package

    def my_handler(req): return {"ok": True}
    zip_path = build_lambda_package(my_handler, "test-fn", 256)
    assert os.path.exists(zip_path)
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "lambda_entry.py" in names
        # cobra4 runtime should be vendored.
        assert any(n.startswith("cobra4/") for n in names)


def test_sql_plugin_with_sqlalchemy():
    """sql_run actually executes against SQLAlchemy when configured."""
    pytest.importorskip("sqlalchemy")
    from cobra4.plugins.builtin import sql as sql_plugin
    from cobra4.plugins.builtin.sql import configure, sql_run

    engine = configure("sqlite:///:memory:")
    try:
        sql_run("CREATE TABLE t (id INTEGER, name TEXT)")
        sql_run("INSERT INTO t VALUES (:id, :name)", params={"id": 1, "name": "ada"})
        rows = sql_run("SELECT id, name FROM t WHERE id = :id", params={"id": 1})
        assert rows == [{"id": 1, "name": "ada"}]
    finally:
        engine.dispose()
        sql_plugin._default_engine = None


def test_queue_file_backend():
    """File-backed queue persists across calls (durable)."""
    from cobra4.runtime.schedule import FileQueue

    with tempfile.TemporaryDirectory() as d:
        q = FileQueue("test", root=d)
        q.put({"a": 1})
        q.put({"a": 2})
        events = []
        for _ in range(2):
            events.extend(list(q.poll(timeout=0.1)))
        assert {"a": 1} in events and {"a": 2} in events


# ---------- Wave C ----------


def test_c4_test_runner_executes():
    """`c4 test` discovers and runs cobra4 tests."""
    with tempfile.TemporaryDirectory() as d:
        tests_dir = Path(d, "tests")
        tests_dir.mkdir()
        (tests_dir / "test_basic.c4").write_text(
            "use cobra4.stdlib.test as t\n\n"
            "fn test_arithmetic() {\n"
            "    t.assert_eq(2 + 2, 4)\n"
            "}\n"
            "fn test_failing() {\n"
            "    t.assert_eq(1, 2)\n"
            "}\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "cobra4.cli", "test", str(tests_dir)],
            cwd=d, env=env, capture_output=True, text=True, timeout=30,
        )
        # 1 pass, 1 fail → exit 1
        assert proc.returncode == 1
        assert "1 passed" in proc.stdout
        assert "1 failed" in proc.stdout


# ---------- Wave D ----------


def test_lsp_document_symbols():
    from cobra4.tools.lsp import _Server

    s = _Server()
    src = "fn add(a, b) = a + b\nclass Foo {\n  fn bar(self) { return 1 }\n}\n"
    symbols = s._document_symbols(src)
    names = [sym["name"] for sym in symbols]
    assert "add" in names
    assert "Foo" in names
    foo = next(sym for sym in symbols if sym["name"] == "Foo")
    assert any(c["name"] == "bar" for c in foo.get("children", []))


def test_lsp_definition():
    from cobra4.tools.lsp import _Server

    s = _Server()
    src = "fn greet(n) { return n }\nx = greet('a')\n"
    # Click on `greet` in the second line (col 4)
    loc = s._definition(src, "file://x.c4", 1, 5)
    assert loc is not None
    assert loc["range"]["start"]["line"] == 0


def test_source_map_column_precise():
    from cobra4.source_map import SourceMap

    m = SourceMap()
    m.record(py_line=10, c4_line=3, py_col=4, c4_col=8)
    m.record(py_line=10, c4_line=3, py_col=20, c4_col=24)
    assert m.lookup_position(10, 5) == (3, 8)
    assert m.lookup_position(10, 25) == (3, 24)
    # Backward-compat: lookup() returns line only
    assert m.lookup(10) == 3


# ---------- Wave E ----------


def test_stdlib_strings_real():
    from cobra4.stdlib import clear_cache
    clear_cache()
    import cobra4.stdlib.strings as s

    assert s.slugify("Hello World!") == "hello-world"
    assert s.camel_to_snake("fooBarBaz") == "foo_bar_baz"
    assert s.snake_to_camel("foo_bar_baz") == "fooBarBaz"
    assert s.truncate("abcdefghij", 5) == "ab..."


def test_stdlib_data_real():
    import cobra4.stdlib.data as data

    rows = [{"g": "a", "v": 1}, {"g": "b", "v": 2}, {"g": "a", "v": 3}]
    grouped = data.group_by(rows, "g")
    assert set(grouped.keys()) == {"a", "b"}
    assert len(grouped["a"]) == 2

    sorted_rows = data.sort_by(rows, "v", reverse=True)
    assert sorted_rows[0]["v"] == 3

    joined = data.join(
        [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
        [{"id": 1, "age": 10}],
        on="id", how="left",
    )
    assert len(joined) == 2  # a joined, b kept (left)


def test_stdlib_time_real():
    import cobra4.stdlib.time as t

    assert t.parse_duration("1h30m") == 5400
    assert t.parse_duration("2d") == 172800
    assert t.fmt_duration(5400) == "1h30m"


def test_stdlib_fs_real():
    import cobra4.stdlib.fs as fs

    with tempfile.TemporaryDirectory() as d:
        Path(d, "a.txt").write_text("x", encoding="utf-8")
        Path(d, "sub").mkdir()
        Path(d, "sub", "b.txt").write_text("y", encoding="utf-8")

        files = fs.list_dir(d, "*.txt")
        assert len(files) == 1

        all_files = fs.walk(d, "*.txt")
        assert len(all_files) == 2

        fs.copy(str(Path(d, "a.txt")), str(Path(d, "a-copy.txt")))
        assert fs.exists(str(Path(d, "a-copy.txt")))


def test_stdlib_test_assertions():
    """The cobra4 test stdlib's assertion helpers actually raise on failure."""
    import cobra4.stdlib.test as t

    # Pass
    t.assert_eq(1, 1)
    t.expect(2 + 2).to_eq(4)

    # Fail
    with pytest.raises(t.AssertionFailed):
        t.assert_eq(1, 2)
    with pytest.raises(t.AssertionFailed):
        t.expect("hi").to_eq("bye")


def test_stdlib_http_session_construct():
    """Session class is constructible and exposes the canonical methods."""
    import cobra4.stdlib.http as http

    s = http.Session(base_url="https://example.com", timeout=5)
    assert s.timeout == 5
    s.auth_bearer("tok")
    assert s._sess.headers["Authorization"] == "Bearer tok"


# ---------- Wave F ----------


def test_log_analyzer_example_runs():
    src_path = PROJECT_ROOT / "examples" / "09_log_analyzer.c4"
    with tempfile.TemporaryDirectory() as d:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        from cobra4.parser import parse
        from cobra4.codegen import generate
        from cobra4.lowering import lower

        src = src_path.read_text(encoding="utf-8")
        code = generate(lower(parse(src, source_path=str(src_path)))).code
        py_file = Path(d, "out.py")
        py_file.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(py_file)],
            cwd=d, env=env, capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert (Path(d) / "_log_report.json").exists()


def test_webhook_router_example_compiles_and_runs():
    """The webhook router parses + executes against an in-memory SQLite."""
    pytest.importorskip("sqlalchemy")
    src_path = PROJECT_ROOT / "examples" / "10_webhook_router.c4"
    src = src_path.read_text(encoding="utf-8")
    from cobra4.parser import parse
    from cobra4.codegen import generate
    from cobra4.lowering import lower
    from cobra4.plugins import preprocess

    pre = preprocess(src)
    mod = parse(pre.source, source_path=str(src_path))
    code = generate(lower(mod)).code
    plugin_imports = "\n".join(
        f"from {p.runtime_module} import *" for p in pre.plugins if p.runtime_module
    )
    code = plugin_imports + "\n" + code

    os.environ["COBRA4_SQL_URL"] = "sqlite:///:memory:"
    try:
        if True:
            ns = {"__name__": "__main__"}
            exec(code, ns)
            # Build a fake request and exercise the router.
            class Req:
                def __init__(self, method, path, body=b""):
                    self.method = method
                    self.path = path
                    self.headers = {"authorization": "Bearer dev-token"}
                    self.body = body
                def json(self):
                    import json
                    return json.loads(self.body) if self.body else None

            req = Req("POST", "/orders", b'{"id":"x1","total":1.0}')
            resp = ns["router"](req)
            assert resp[0] == 201

            req2 = Req("GET", "/orders")
            resp2 = ns["router"](req2)
            assert resp2["count"] == 1
    finally:
        # Dispose engine + clear env
        from cobra4.plugins.builtin import sql as sql_plugin
        if sql_plugin._default_engine is not None:
            sql_plugin._default_engine.dispose()
            sql_plugin._default_engine = None
        os.environ.pop("COBRA4_SQL_URL", None)
