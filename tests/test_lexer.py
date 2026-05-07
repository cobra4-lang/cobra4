"""Lexer / tokenizer tests."""

from __future__ import annotations

import pytest

from cobra4.lexer import tokenize


def _types(src: str) -> list[str]:
    return [t.type for t in tokenize(src)]


def test_basic_tokens():
    types = _types("x = 1\n")
    assert "NAME" in types
    assert "EQUAL" in types
    assert "NUMBER" in types


def test_string_literal():
    types = _types('s = "hello"\n')
    assert "STRING" in types


def test_triple_quoted_string():
    types = _types('s = """multi\nline"""\n')
    assert "STRING" in types


def test_comment_ignored():
    types = _types("# only a comment\nx = 1\n")
    # Comments are %ignored — no COMMENT token in the output stream.
    assert "COMMENT" not in types


def test_newlines_suppressed_in_parens():
    """Newlines inside (...) and [...] are dropped by the postlex hook."""
    types = _types("foo(\n  a,\n  b,\n)\n")
    # No _NL between LPAR and RPAR
    in_parens = False
    saw_inner_nl = False
    for t in types:
        if t == "LPAR":
            in_parens = True
        elif t == "RPAR":
            in_parens = False
        elif t == "_NL" and in_parens:
            saw_inner_nl = True
    assert not saw_inner_nl


def test_braces_do_not_swallow_newlines():
    """Newlines inside `{ ... }` blocks must be preserved as separators."""
    types = _types("if x { a()\n b()\n}\n")
    assert "_NL" in types
