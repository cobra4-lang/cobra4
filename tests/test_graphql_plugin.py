"""Tests for the GraphQL plugin."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cobra4.plugins.builtin.graphql import _transform
from cobra4.runtime.graphql import parse_document, GraphQLDocument


# ---------- plugin transform ----------


def test_transform_rewrites_graphql_literal() -> None:
    src = 'x = graphql"""type T { id: ID! }"""\n'
    out = _transform(src)
    assert "_c4_graphql_parse" in out
    # Braces must be doubled so cobra4's interpolation doesn't eat them.
    assert "{{" in out and "}}" in out


def test_transform_preserves_non_graphql_code() -> None:
    src = "fn helper() = 1\nx = graphql\"\"\"type T { id: ID! }\"\"\"\nfn other() = 2\n"
    out = _transform(src)
    assert "fn helper() = 1" in out
    assert "fn other() = 2" in out


def test_transform_handles_multiple_blocks() -> None:
    src = 'a = graphql"""type A {}"""\nb = graphql"""type B {}"""\n'
    out = _transform(src)
    assert out.count("_c4_graphql_parse") == 2


# ---------- runtime ----------


def test_parse_document_returns_wrapper() -> None:
    doc = parse_document("type X { id: ID! }")
    assert isinstance(doc, GraphQLDocument)
    assert "type X" in doc.text


def test_parse_document_str_round_trip() -> None:
    sdl = "type X { id: ID! }"
    doc = parse_document(sdl)
    assert str(doc) == sdl


# ---------- end-to-end ----------


def _run_c4(tmp_path: Path, src: str) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_e2e_graphql_block_compiles_and_runs(tmp_path: Path) -> None:
    src = (
        'lang use graphql\n'
        'doc = graphql"""\n'
        'type User { id: ID! name: String! }\n'
        'type Query { user(id: ID!): User }\n'
        '"""\n'
        'log("r", has_user=("type User" in doc.text))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "has_user=True" in stderr
