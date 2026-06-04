"""Regression tests for Tier 2.2 type-checker upgrades.

Covers:
- Generic-arg compatibility (list[int] vs list[str], dict[str, int] vs dict[str, str]).
- Element-type inference for list/dict/set/tuple literals.
- Optional narrowing on `is None` / `is not None` in `if` branches.
"""

from __future__ import annotations

from cobra4.parser import parse
from cobra4.typecheck import TypeChecker, _join, INT_T, FLOAT_T, STR_T, ANY_T, NONE_T


def _check(src: str):
    module = parse(src, source_path="<t>")
    tc = TypeChecker()
    diags = tc.check(module)
    return tc, diags


# ---------- Element-type inference ----------


def test_list_literal_homogeneous_inferred_as_typed() -> None:
    tc, _ = _check("xs = [1, 2, 3]\n")
    assert tc.var_types["xs"].name == "list"
    assert tc.var_types["xs"].args == (INT_T,)


def test_list_literal_mixed_int_float_joins_to_float() -> None:
    tc, _ = _check("xs = [1, 2.5, 3]\n")
    assert tc.var_types["xs"].args == (FLOAT_T,)


def test_list_literal_heterogeneous_collapses_to_any() -> None:
    tc, _ = _check('xs = [1, "hi"]\n')
    assert tc.var_types["xs"].args == (ANY_T,)


def test_dict_literal_homogeneous_inferred() -> None:
    tc, _ = _check('d = {"a": 1, "b": 2}\n')
    assert tc.var_types["d"].name == "dict"
    assert tc.var_types["d"].args == (STR_T, INT_T)


def test_dict_with_spread_falls_back_to_opaque() -> None:
    src = 'extra = {"a": 1}\nd = {"x": 2, **extra}\n'
    tc, _ = _check(src)
    assert tc.var_types["d"].name == "dict"
    assert tc.var_types["d"].args == ()  # opaque


def test_tuple_literal_keeps_positional_types() -> None:
    tc, _ = _check('t = (1, "hi", True)\n')
    assert tc.var_types["t"].name == "tuple"
    assert len(tc.var_types["t"].args) == 3
    assert tc.var_types["t"].args[0].name == "int"
    assert tc.var_types["t"].args[1].name == "str"


def test_set_literal_homogeneous_inferred() -> None:
    tc, _ = _check("s = {1, 2, 3}\n")
    assert tc.var_types["s"].name == "set"
    assert tc.var_types["s"].args == (INT_T,)


# ---------- Generic compat warnings ----------


def test_generic_param_default_mismatch_warns() -> None:
    src = 'fn handle(xs: list[int] = ["a", "b"]) -> int = 0\n'
    _, diags = _check(src)
    assert any(d.code == "T003" and "list[str]" in d.message for d in diags), diags


def test_generic_param_default_match_silent() -> None:
    src = "fn handle(xs: list[int] = [1, 2, 3]) -> int = 0\n"
    _, diags = _check(src)
    assert not any(d.code in ("T002", "T003", "T006") for d in diags), diags


def test_generic_param_call_argument_warns() -> None:
    src = "fn handle(xs: list[int]) -> int = 0\n" 'ys = ["a", "b"]\n' "handle(ys)\n"
    _, diags = _check(src)
    assert any(d.code == "T005" and "list[str]" in d.message for d in diags), diags


def test_generic_param_call_argument_match_silent() -> None:
    src = "fn handle(xs: list[int]) -> int = 0\n" "ys = [1, 2, 3]\n" "handle(ys)\n"
    _, diags = _check(src)
    assert not any(d.code == "T005" for d in diags), diags


# ---------- Optional narrowing ----------


def test_join_helper() -> None:
    assert _join(INT_T, INT_T).name == "int"
    assert _join(INT_T, FLOAT_T).name == "float"
    assert _join(INT_T, STR_T).name == "Any"


def test_narrow_facts_is_not_none_drops_optional_in_then_branch() -> None:
    src = (
        "fn use_str(s: str) -> int = 0\n"
        "fn handler(name: str?) {\n"
        "    if name is not None {\n"
        "        use_str(name)\n"
        "    }\n"
        "}\n"
    )
    _, diags = _check(src)
    # Inside the then branch, `name` should be `str` not `str?`, so no warning.
    assert not any(d.code == "T005" for d in diags), diags


def test_narrow_facts_is_none_narrows_else_branch() -> None:
    src = (
        "fn use_str(s: str) -> int = 0\n"
        "fn handler(name: str?) {\n"
        "    if name is None {\n"
        "        return\n"
        "    } else {\n"
        "        use_str(name)\n"
        "    }\n"
        "}\n"
    )
    _, diags = _check(src)
    assert not any(d.code == "T005" for d in diags), diags


def test_narrow_facts_else_branch_of_is_not_none_keeps_none() -> None:
    """In the else-branch of `if x is not None`, x is None — and passing
    None to a non-optional parameter should still warn."""
    src = (
        "fn use_str(s: str) -> int = 0\n"
        "fn handler(name: str?) {\n"
        "    if name is not None {\n"
        "        return\n"
        "    } else {\n"
        "        use_str(name)\n"
        "    }\n"
        "}\n"
    )
    _, diags = _check(src)
    assert any(d.code == "T005" for d in diags), diags
