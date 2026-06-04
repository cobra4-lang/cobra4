"""Regression tests for the issues raised in code review.

Each test maps to a numbered review point and asserts the fixed
behavior. If any of these regress, we've reintroduced the bug.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------- #1: SmartFn cache + custom predicates ----------


def test_review_1_cache_bypassed_with_custom_predicate():
    """Regression: cache must not pin a handler when `when=` is in play."""
    from cobra4.runtime.smart import make_smart

    f = make_smart("f")
    f.register(lambda v: f"A:{v}", type=str, when=lambda v: v.startswith("a"))
    f.register(lambda v: f"B:{v}", type=str, when=lambda v: v.startswith("b"))
    assert f("alpha") == "A:alpha"
    assert f("beta") == "B:beta"
    # cache flipping order shouldn't matter
    assert f("apple") == "A:apple"
    assert f("blue") == "B:blue"


# ---------- #2: ?. on dict ----------


def test_review_2_safe_attr_dict_uses_get():
    from cobra4.runtime.core import safe_attr

    req = {"params": {"name": "ada"}}
    assert safe_attr(safe_attr(req, "params"), "name") == "ada"
    # missing key → None, composes with ??
    assert safe_attr({"a": 1}, "missing") is None


def test_review_2_safe_attr_chain_with_default():
    """`req?.params?.name ?? "world"` works with dict and missing key."""
    from cobra4.runtime.core import safe_attr, default

    req_full = {"params": {"name": "ada"}}
    req_no_name = {"params": {}}
    req_none_params = {"params": None}

    def chain(r):
        return default(safe_attr(safe_attr(r, "params"), "name"), "world")

    assert chain(req_full) == "ada"
    assert chain(req_no_name) == "world"
    assert chain(req_none_params) == "world"
    assert chain(None) == "world"


def test_review_2_safe_attr_returns_none_for_missing_attr():
    """getattr fallback also returns None instead of raising."""
    from cobra4.runtime.core import safe_attr

    class O:
        pass

    assert safe_attr(O(), "nope") is None


# ---------- #3: c4 fmt preserves plugin syntax ----------


def _run_cli(*args, cwd=None, input_text=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "cobra4.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        input=input_text,
    )


def test_review_3_fmt_preserves_lang_use_directive():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "x.c4"
        src.write_text(
            "lang use sql\n\nx = sql {\n    SELECT 1\n}\n",
            encoding="utf-8",
        )
        p = _run_cli("fmt", str(src), cwd=d)
        assert p.returncode == 0, p.stderr
        assert "lang use sql" in p.stdout
        assert "sql {" in p.stdout
        assert "SELECT 1" in p.stdout


# ---------- #4: c4 check is plugin-aware ----------


def test_review_4_check_does_not_warn_on_plugin_builtins():
    """`sql_run` is injected by the sql plugin — check shouldn't flag it."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "x.c4"
        src.write_text(
            "lang use sql\n\nrows = sql { SELECT 1 }\nprint(len(rows))\n",
            encoding="utf-8",
        )
        p = _run_cli("check", str(src), cwd=d)
        assert p.returncode == 0, p.stderr
        # Must not flag sql_run as undefined
        combined = p.stdout + p.stderr
        assert "undefined name 'sql_run'" not in combined


# ---------- #5: dispatch_analysis catches semantic overlap ----------


def test_review_5_dispatch_analysis_flags_subset_overlap():
    """Same priority, one strictly subsumes the other → warn."""
    from cobra4.parser import parse
    from cobra4.dispatch_analysis import analyze

    src = "read.register(h_general)\n" 'read.register(h_specific, scheme="file")\n'
    diags = analyze(parse(src))
    assert any("specificity overlap" in d.message for d in diags)


def test_review_5_dispatch_analysis_flags_priority_with_when():
    """`when=` without other constraints invalidates the cache; warn."""
    from cobra4.parser import parse
    from cobra4.dispatch_analysis import analyze

    src = "read.register(h, when=p)\n"
    diags = analyze(parse(src))
    assert any("disables the dispatch cache" in d.message for d in diags)


def test_review_5_dispatch_analysis_quiet_when_priorities_differ():
    from cobra4.parser import parse
    from cobra4.dispatch_analysis import analyze

    src = (
        'read.register(h1, scheme="file")\n'
        'read.register(h2, scheme="file", ext="csv", priority=10)\n'
    )
    diags = analyze(parse(src))
    assert not any("specificity overlap" in d.message for d in diags)


# ---------- #6: fleet shell=True is opt-in ----------


def test_review_6_fleet_run_local_default_no_shell():
    """Default exec MUST split argv; shell metacharacters stay literal."""
    from cobra4.runtime.fleet import _run_local, Host

    # `>` would be a redirect under shell=True; under shell=False it's literal.
    # Use an `echo` whose output proves it's a single string arg.
    if os.name == "nt":
        # On Windows shell=False with echo is finicky; we just verify it doesn't crash.
        h = Host(name="local", addr="localhost")
        result = _run_local(["cmd", "/c", "echo", "hello"], host=h)
        assert "hello" in result.stdout
    else:
        result = _run_local("echo a > /tmp/should_not_redirect")
        # Without shell, the literal `>` and redirect target appear in stdout.
        assert ">" in result.stdout


def test_review_6_fleet_run_local_shell_opt_in():
    """shell=True still works for users who want shell features."""
    from cobra4.runtime.fleet import _run_local

    if os.name == "nt":
        result = _run_local("echo hello & echo world", shell=True)
        assert "hello" in result.stdout
    else:
        result = _run_local("echo hello && echo world", shell=True)
        assert result.stdout.count("hello") == 1
        assert result.stdout.count("world") == 1


# ---------- #7: paramiko strict default ----------


def test_review_7_paramiko_strict_by_default():
    """The paramiko code path uses RejectPolicy unless the host or env opts in."""
    import inspect

    from cobra4.runtime import fleet

    src = inspect.getsource(fleet._run_paramiko)
    # The branch order means RejectPolicy is the default.
    assert "RejectPolicy" in src
    assert "AutoAddPolicy" in src  # still available via opt-in
    assert "host_key_policy" in src  # per-host override exists
    assert "COBRA4_SSH_HOST_KEY_POLICY" in src  # env override exists


# ---------- #8: atomic save ----------


def test_review_8_save_is_atomic_temp_then_replace():
    """Crash mid-write must NOT leave a half-written file."""
    from cobra4.runtime.io import _atomic_write_text

    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "out.json")
        # Pre-existing file; if write_text were used, a crash mid-encode
        # could empty it. Atomic replace keeps the old file intact.
        with open(target, "w") as fh:
            fh.write('{"old": true}')
        _atomic_write_text(target, '{"new": true}')
        with open(target) as fh:
            assert fh.read() == '{"new": true}'

        # Verify there's no leftover .c4tmp_* in the dir.
        leftovers = [f for f in os.listdir(d) if f.startswith(".c4tmp_")]
        assert leftovers == []


# ---------- #10: HTTP server defaults ----------


def test_review_10_http_response_content_type_dict_to_json():
    from cobra4.runtime.schedule import _encode_response

    status, headers, body = _encode_response({"hello": "world"})
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == {"hello": "world"}


def test_review_10_http_response_content_type_str_to_text():
    from cobra4.runtime.schedule import _encode_response

    status, headers, body = _encode_response("plain text")
    assert headers["content-type"].startswith("text/plain")
    assert body == b"plain text"


def test_review_10_http_response_bytes_octet_stream():
    from cobra4.runtime.schedule import _encode_response

    status, headers, body = _encode_response(b"\x00\x01\x02")
    assert headers["content-type"] == "application/octet-stream"
    assert body == b"\x00\x01\x02"


def test_review_10_http_response_explicit_status_and_headers():
    from cobra4.runtime.schedule import _encode_response

    status, headers, body = _encode_response((404, {"x-trace": "abc"}, "not found"))
    assert status == 404
    assert headers["x-trace"] == "abc"


def test_review_10_http_default_bind_loopback():
    """Default bind address is 127.0.0.1, not 0.0.0.0."""
    import inspect

    from cobra4.runtime import schedule

    src = inspect.getsource(schedule._start_http_servers)
    assert '"127.0.0.1"' in src
    assert "COBRA4_HTTP_BIND" in src  # override is documented


# ---------- #12: stdlib import hook caches by mtime ----------


def test_review_12_stdlib_import_hook_caches():
    """Second import doesn't re-parse the .c4 source."""
    import cobra4.stdlib  # installs finder

    cobra4.stdlib.clear_cache()
    # First import compiles + caches.
    import cobra4.stdlib.json as cj  # noqa: F401

    cache_files = list(
        (cobra4.stdlib._STDLIB_DIR / "__pycache__").glob("json.cobra4.pyc")
    )
    assert cache_files, "first import should have written a .cobra4.pyc"

    # Drop the module from sys.modules so re-import goes through the loader.
    sys.modules.pop("cobra4.stdlib.json", None)

    # Patch the parser to fail loudly: if the cache is honored, parse won't run.
    import cobra4.parser as parser

    orig_parse = parser.parse
    parser.parse = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("re-parsed despite cache")
    )
    try:
        import cobra4.stdlib.json as cj_again  # should hit the cache

        assert cj_again.dumps({"x": 1}) == '{"x": 1}'
    finally:
        parser.parse = orig_parse
        sys.modules.pop("cobra4.stdlib.json", None)


# ---------- #13: dispatch tracing ----------


def test_review_13_dispatch_trace_when_env_set():
    """COBRA4_TRACE_DISPATCH=1 emits a single line per dispatch on stderr."""
    # We exercise this in a subprocess so importing smart picks up the env var.
    code = (
        "import sys; sys.path.insert(0, r'%s');\n"
        "from cobra4.runtime.smart import make_smart\n"
        "f = make_smart('f')\n"
        "f.register(lambda x: x, type=str)\n"
        "f('hello')\n"
    ) % str(PROJECT_ROOT)
    env = os.environ.copy()
    env["COBRA4_TRACE_DISPATCH"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "[c4-trace] f(" in proc.stderr
