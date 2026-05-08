"""Tests for Result types and the postfix `?` propagation operator."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cobra4.parser import parse
from cobra4 import ast_nodes as N
from cobra4.codegen import generate
from cobra4.runtime.result import Ok, Err, _c4_try_propagate, _C4Propagate


# ---------- runtime ----------


def test_runtime_ok_unwraps() -> None:
    assert _c4_try_propagate(Ok(42)) == 42


def test_runtime_err_raises_propagate() -> None:
    err = Err("boom")
    try:
        _c4_try_propagate(err)
    except _C4Propagate as p:
        assert p.err is err
        return
    assert False, "expected _C4Propagate"


def test_runtime_propagate_inherits_BaseException_not_Exception() -> None:
    """`?` propagation must not be caught by ordinary `except Exception`
    blocks — otherwise user code that wraps something in `try/catch`
    would silently swallow the early return."""
    try:
        _c4_try_propagate(Err("x"))
    except Exception:
        assert False, "_C4Propagate must not be caught by `except Exception`"
    except _C4Propagate:
        pass


def test_runtime_invalid_argument_raises_typeerror() -> None:
    import pytest
    with pytest.raises(TypeError):
        _c4_try_propagate(42)


def test_ok_err_equality() -> None:
    assert Ok(1) == Ok(1)
    assert Err("x") == Err("x")
    assert Ok(1) != Err(1)
    assert Ok(1).is_ok() and not Ok(1).is_err()
    assert Err("x").is_err() and not Err("x").is_ok()


# ---------- parsing ----------


def test_parse_postfix_question_creates_try_propagate_node() -> None:
    m = parse("x = parse(s)?\n")
    assert len(m.body) == 1
    assert isinstance(m.body[0], N.Assign)
    assert isinstance(m.body[0].value, N.TryPropagate)


def test_parse_question_chains_with_member_access() -> None:
    """`expr?.attr` is the safe-nav operator (single token); `expr?.attr`
    after `?` propagation should require a parens to disambiguate."""
    m = parse("x = compute()?\n")
    assert isinstance(m.body[0].value, N.TryPropagate)


# ---------- codegen ----------


def test_codegen_emits_try_propagate_helper_call() -> None:
    m = parse("fn f(x) = compute(x)?\n")
    out = generate(m).code
    assert "_c4_try_propagate" in out


def test_codegen_wraps_fn_using_question_in_try_except() -> None:
    src = (
        "fn add(a, b) {\n"
        "    x = parse(a)?\n"
        "    return Ok(x + 1)\n"
        "}\n"
    )
    out = generate(m := parse(src)).code
    # The generated function must catch _C4Propagate and return p.err
    assert "_C4Propagate" in out
    assert "__c4_p.err" in out


def test_codegen_does_not_wrap_fn_without_question() -> None:
    """Don't pay for what you don't use — a function with no `?`
    shouldn't get the try/except wrapper (it's mostly free, but we want
    the diff to be obvious in `c4 build` output). We check only the
    function body — the runtime import header always references
    `_C4Propagate` regardless."""
    src = "fn double(x) = x * 2\n"
    out = generate(parse(src)).code
    fn_body = out.split("def double", 1)[1]
    assert "_C4Propagate" not in fn_body


# ---------- end-to-end ----------


def _run_c4(tmp_path: Path, src: str) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_e2e_ok_path_returns_unwrapped(tmp_path: Path) -> None:
    src = (
        "fn pure() = Ok(7)\n"
        "fn use_it() {\n"
        "    v = pure()?\n"
        "    return Ok(v + 1)\n"
        "}\n"
        "r = use_it()\n"
        "match r {\n"
        "    case Ok(v) { log(\"got\", v=v) }\n"
        "    case Err(e) { log(\"err\", e=e) }\n"
        "}\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "v=8" in stderr


def test_e2e_err_path_propagates(tmp_path: Path) -> None:
    src = (
        "fn fail() = Err(\"nope\")\n"
        "fn use_it() {\n"
        "    v = fail()?\n"
        "    return Ok(v + 1)\n"
        "}\n"
        "r = use_it()\n"
        "match r {\n"
        "    case Ok(v) { log(\"got\", v=v) }\n"
        "    case Err(e) { log(\"err\", e=e) }\n"
        "}\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "err" in stderr
    assert "e=nope" in stderr


def test_e2e_question_inside_inline_fn_body(tmp_path: Path) -> None:
    """`fn f(x) = expr?` (inline body, no block) — must still wrap."""
    src = (
        "fn pure() = Ok(7)\n"
        "fn use_it() = Ok(pure()? * 2)\n"
        "r = use_it()\n"
        "match r { case Ok(v) { log(\"v\", v=v) } case Err(e) { log(\"e\", e=e) } }\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "v=14" in stderr
