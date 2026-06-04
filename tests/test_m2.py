"""M2 tests: enhanced resolver, gradual type checker, dispatcher analysis."""

from __future__ import annotations

from cobra4.parser import parse
from cobra4.resolver import resolve
from cobra4.typecheck import check as typecheck
from cobra4.dispatch_analysis import analyze as dispatch_analyze

# ---------- Resolver ----------


def test_resolver_no_warnings_for_simple_program():
    rr = resolve(parse("x = 1\nprint(x)\n"))
    assert rr.ok()
    assert not rr.warnings


def test_resolver_warns_on_undefined_name():
    rr = resolve(parse("y = undefined_thing\n"))
    msgs = [str(w) for w in rr.warnings]
    assert any("undefined name" in m and "undefined_thing" in m for m in msgs)


def test_resolver_does_not_warn_on_builtins():
    rr = resolve(parse("print(len([1,2,3]))\n"))
    assert not rr.warnings


def test_resolver_function_scope_isolated():
    src = "fn f() { local = 1 }\ny = local\n"
    rr = resolve(parse(src))
    msgs = [str(w) for w in rr.warnings]
    assert any("undefined name 'local'" in m for m in msgs)


def test_resolver_warns_on_shadowing():
    src = "x = 1\nfn f() { x = 2 }\n"
    rr = resolve(parse(src))
    msgs = [str(w) for w in rr.warnings]
    assert any("shadow" in m for m in msgs)


def test_resolver_invalid_lvalue_is_error():
    rr = resolve(parse("(1 + 2) = 3\n"))
    assert not rr.ok()


# ---------- Type checker ----------


def test_type_check_passes_consistent_program():
    src = "fn add(a: int, b: int) -> int = a + b\nadd(1, 2)\n"
    diags = typecheck(parse(src))
    assert not diags


def test_type_check_warns_on_arg_mismatch():
    src = "fn upper(s: str) -> str = s\nupper(42)\n"
    diags = typecheck(parse(src))
    assert any("declared str, got int" in d.message for d in diags)


def test_type_check_warns_on_return_mismatch():
    src = "fn s() -> str = 1\n"
    diags = typecheck(parse(src))
    assert any("returns int, declared str" in d.message for d in diags)


def test_type_check_int_float_compat():
    src = "fn f(x: float) -> float = x\nf(1)\n"
    diags = typecheck(parse(src))
    assert not diags


def test_type_check_kwarg_mismatch():
    src = "fn f(name: str) = name\nf(name=7)\n"
    diags = typecheck(parse(src))
    assert any("declared str, got int" in d.message for d in diags)


# ---------- Dispatcher analysis ----------


def test_dispatch_analysis_flags_duplicate_keys():
    src = """
read.register(handler1, scheme="file", ext="yml")
read.register(handler2, scheme="file", ext="yml")
"""
    diags = dispatch_analyze(parse(src))
    assert any("AmbiguousDispatch" in d.message for d in diags)


def test_dispatch_analysis_silent_when_keys_differ():
    src = """
read.register(handler1, scheme="file", ext="yml")
read.register(handler2, scheme="file", ext="toml")
"""
    diags = dispatch_analyze(parse(src))
    assert not diags


def test_dispatch_analysis_distinct_priorities_silent():
    src = """
read.register(handler1, scheme="file", ext="yml", priority=1)
read.register(handler2, scheme="file", ext="yml", priority=2)
"""
    diags = dispatch_analyze(parse(src))
    assert not diags
