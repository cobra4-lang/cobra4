"""Codegen snapshot tests — make sure the emitted Python is valid and stable."""

from __future__ import annotations

import ast as pyast
import textwrap

from cobra4.parser import parse
from cobra4.lowering import lower
from cobra4.codegen import generate


def transpile(src: str) -> str:
    return generate(lower(parse(src)), cobra4_path="<test>").code


def test_emitted_python_parses() -> None:
    """The codegen output must always be syntactically valid Python."""
    src = textwrap.dedent("""
        x = 1
        if x > 0 { log("positive") } else { log("zero") }
        for i in [1, 2, 3] { print(i) }
        each i in [1, 2, 3] in parallel(workers=2) { i * 2 }
    """).strip() + "\n"
    out = transpile(src)
    pyast.parse(out)  # raises if invalid


def test_safe_attr_lowered() -> None:
    out = transpile("y = a?.b\n")
    assert "_c4_safe_attr" in out
    assert "'b'" in out


def test_default_lowered() -> None:
    out = transpile("y = a ?? 1\n")
    assert "_c4_default" in out


def test_each_parallel_lowered() -> None:
    out = transpile("r = each i in xs in parallel(workers=4) { f(i) }\n")
    assert "_c4_parallel_for(xs" in out
    assert "workers=4" in out


def test_each_sequential_expr_to_listcomp() -> None:
    out = transpile("r = each i in xs { f(i) }\n")
    assert "for i in xs" in out


def test_every_lowered() -> None:
    out = transpile("every 5 seconds { tick() }\n")
    assert "_c4_every(5" in out


def test_on_event_lowered() -> None:
    out = transpile('on event from queue("x") { handle(event) }\n')
    assert "_c4_on_event(" in out
    assert "lambda event" in out


def test_fstring_emitted() -> None:
    out = transpile('y = "hello {name}"\n')
    assert "f'hello {name}'" in out


def test_deterministic_output() -> None:
    src = "x = 1\ny = x + 2\n"
    a = transpile(src)
    b = transpile(src)
    assert a == b
