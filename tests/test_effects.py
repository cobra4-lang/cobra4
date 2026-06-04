"""Tests for the effect / capability system.

Effects are gradual: only functions with an explicit `with [...]`
clause are checked. Unannotated callees skip the check (they're
considered "any effect").
"""

from __future__ import annotations

from cobra4.parser import parse
from cobra4 import ast_nodes as N
from cobra4.typecheck import TypeChecker


def _check(src: str):
    m = parse(src, source_path="<t>")
    tc = TypeChecker()
    diags = tc.check(m)
    return tc, diags


# ---------- parsing ----------


def test_parse_effect_clause_attaches_to_fn() -> None:
    m = parse("fn f() with [http, log] = 1\n")
    fn = m.body[0]
    assert isinstance(fn, N.FnDecl)
    assert fn.effects == ["http", "log"]


def test_parse_empty_effect_clause_means_pure() -> None:
    m = parse("fn pure(x) with [] = x * 2\n")
    fn = m.body[0]
    assert fn.effects == []


def test_parse_no_effect_clause_means_unannotated() -> None:
    m = parse("fn unannotated(x) = x\n")
    fn = m.body[0]
    assert fn.effects is None


def test_effect_clause_combined_with_return_type() -> None:
    m = parse("fn f(x) -> int with [http] = x\n")
    fn = m.body[0]
    assert fn.effects == ["http"]
    assert fn.return_type is not None


def test_effect_clause_combined_with_async() -> None:
    m = parse("async fn f() with [http] { return 1 }\n")
    fn = m.body[0]
    assert fn.is_async
    assert fn.effects == ["http"]


# ---------- typechecker ----------


def test_unannotated_fn_skips_effect_check() -> None:
    """No `with` → no constraint, even if body calls effectful builtins."""
    src = "fn f(x) {\n" '    log("hi")\n' '    fetch("http://x")\n' "}\n"
    _, diags = _check(src)
    assert not any(d.code == "E001" for d in diags)


def test_pure_fn_calling_log_warns() -> None:
    src = "fn f(x) with [] {\n" '    log("this should be flagged")\n' "}\n"
    _, diags = _check(src)
    e001 = [d for d in diags if d.code == "E001"]
    assert len(e001) == 1
    assert "log" in e001[0].message


def test_caller_with_subset_effects_warns_on_callee_extra() -> None:
    src = (
        "fn fetch_user(id) with [http] = id\n"
        "fn caller(id) with [log] {\n"
        "    fetch_user(id)\n"
        "}\n"
    )
    _, diags = _check(src)
    e001 = [d for d in diags if d.code == "E001"]
    assert len(e001) == 1
    assert "fetch_user" in e001[0].message
    assert "http" in e001[0].message


def test_caller_with_superset_effects_silent() -> None:
    src = (
        "fn fetch_user(id) with [http] = id\n"
        "fn caller(id) with [http, log] {\n"
        "    fetch_user(id)\n"
        '    log("got it")\n'
        "}\n"
    )
    _, diags = _check(src)
    assert not any(d.code == "E001" for d in diags), diags


def test_pure_fn_calling_pure_fn_silent() -> None:
    src = (
        "fn double(x) with [] = x * 2\n" "fn quadruple(x) with [] = double(double(x))\n"
    )
    _, diags = _check(src)
    assert not any(d.code == "E001" for d in diags), diags


def test_unannotated_callee_does_not_propagate_effects() -> None:
    """An unannotated `helper` is considered "trust the caller". Even
    if helper internally calls log(), it doesn't taint a pure caller —
    the user opts in to checking by adding `with [...]` to helper."""
    src = (
        "fn helper(x) {\n"
        '    log("side effect")\n'
        "    return x\n"
        "}\n"
        "fn pure_caller(x) with [] = helper(x)\n"
    )
    _, diags = _check(src)
    assert not any(d.code == "E001" for d in diags), diags


def test_typechecker_records_declared_effects_on_signature() -> None:
    src = "fn f(x) with [http, db] = x\n"
    tc, _ = _check(src)
    sig = tc.fn_sigs["f"]
    assert sig.effects == frozenset({"http", "db"})


def test_builtin_effects_recognized_for_log_fetch_secret_run() -> None:
    src = (
        "fn must_be_pure(x) with [] {\n"
        '    log("x")\n'
        '    fetch("http://x")\n'
        '    secret("k")\n'
        '    run("ls")\n'
        "}\n"
    )
    _, diags = _check(src)
    flagged = [d for d in diags if d.code == "E001"]
    # 4 distinct violations (log, http, secret, ssh)
    assert len(flagged) == 4
    msgs = " ".join(d.message for d in flagged)
    assert "log" in msgs and "http" in msgs and "secret" in msgs and "ssh" in msgs


def test_effect_check_is_warning_not_error() -> None:
    """E001 is advisory like the rest of the type checker — we don't
    block compilation. Confirm severity."""
    src = 'fn f() with [] { log("x") }\n'
    _, diags = _check(src)
    e001 = [d for d in diags if d.code == "E001"]
    assert e001
    assert e001[0].severity == "warning"
