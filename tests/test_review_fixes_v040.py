"""Regression tests for the code-review fixes in 0.5.0.

These cover the specific bugs the review flagged: codegen fallback that
called an undefined helper, non-atomic infra state writes, and the SQL
plugin's braces-in-strings bug.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from cobra4 import ast_nodes as N
from cobra4.codegen import CodegenError, _lambda_to_str
from cobra4.parser import parse
from cobra4.plugins.builtin.sql import _find_sql_blocks, _transform as _sql_transform
from cobra4.runtime import infra as infra_mod


# ---------- codegen: dead helper now raises a clean compile error ----------


def test_multi_stmt_block_lambda_raises_codegen_error() -> None:
    """A block lambda with multiple statements that aren't a single
    Return used to silently emit a call to an undefined
    `_c4_unsupported_block_lambda` helper, crashing at runtime. Should
    now raise CodegenError at compile time."""
    lam = N.Lambda(
        params=[N.Param(name="x")],
        block=[
            N.Assign(targets=[N.Name(name="y")], value=N.Num(value=1, is_int=True)),
            N.Return(value=N.Name(name="y")),
        ],
    )
    with pytest.raises(CodegenError, match="multiple statements"):
        _lambda_to_str(lam)


def test_single_return_block_lambda_still_works() -> None:
    """Single-Return block lambdas are still supported (common case)."""
    lam = N.Lambda(
        params=[N.Param(name="x")],
        block=[N.Return(value=N.Name(name="x"))],
    )
    out = _lambda_to_str(lam)
    assert out == "(lambda x: x)"


def test_expression_body_lambda_works() -> None:
    """Inline lambdas (`fn(x) = x * 2`) are the common case."""
    lam = N.Lambda(
        params=[N.Param(name="x")],
        body=N.Name(name="x"),
    )
    assert "lambda x: x" in _lambda_to_str(lam)


# ---------- infra: atomic state write ----------


def test_save_state_is_atomic(tmp_path: Path) -> None:
    """The state file should be updated via temp + replace so a crash
    can't leave a half-written JSON. Verified by checking that no
    `.tmp` file is left after a successful write."""
    state_path = tmp_path / "state.json"
    infra_mod.save_state({"hello": {"k": "v"}}, state_path)
    assert state_path.exists()
    assert json.loads(state_path.read_text()) == {"hello": {"k": "v"}}
    # No leftover temp file
    assert not (tmp_path / "state.json.tmp").exists()


def test_save_state_overwrites_atomically(tmp_path: Path) -> None:
    """Repeated writes converge on the latest state."""
    state_path = tmp_path / "state.json"
    infra_mod.save_state({"a": {"v": 1}}, state_path)
    infra_mod.save_state({"a": {"v": 2}, "b": {}}, state_path)
    data = json.loads(state_path.read_text())
    assert data["a"]["v"] == 2
    assert "b" in data


def test_save_state_concurrent_does_not_corrupt(tmp_path: Path) -> None:
    """Two threads writing the same state file end up with one
    well-formed file. The contents may be either thread's writeout,
    but JSON must always parse — never half-written."""
    state_path = tmp_path / "state.json"

    def writer(tag: str) -> None:
        for _ in range(50):
            infra_mod.save_state({tag: {"v": tag}}, state_path)

    t1 = threading.Thread(target=writer, args=("a",))
    t2 = threading.Thread(target=writer, args=("b",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Whatever the result, the file must parse cleanly.
    data = json.loads(state_path.read_text())
    assert data  # non-empty


# ---------- SQL plugin: braces inside string literals ----------


def test_sql_plugin_handles_braces_inside_string_literal() -> None:
    """The old regex-only scanner stopped at the first `{` inside a SQL
    string. Now the brace-aware scanner skips string contents."""
    src = 'rows = sql { SELECT * FROM t WHERE meta = \'{"k": "v"}\' }\n'
    out = _sql_transform(src)
    # Should have transformed to sql_run(...) with the full body
    assert "sql_run(" in out
    # `sql { ... }` should be entirely replaced, no fragments left
    assert "sql {" not in out
    assert "{\"k\"" in out or "{\\\"k\\\"" in out  # body content preserved


def test_sql_plugin_handles_dollar_brace_format_placeholders() -> None:
    """`{name}` interpolation-style strings inside SQL also shouldn't
    break the scanner."""
    src = 'q = sql { SELECT * FROM users WHERE id = \'{user_id}\' }\n'
    out = _sql_transform(src)
    assert "sql_run(" in out
    assert "sql {" not in out


def test_sql_plugin_handles_multiple_blocks_with_braces() -> None:
    src = (
        'a = sql { SELECT 1 WHERE x = \'{foo}\' }\n'
        'b = sql { SELECT 2 }\n'
    )
    out = _sql_transform(src)
    sql_run_count = out.count("sql_run(")
    assert sql_run_count == 2


def test_find_sql_blocks_returns_correct_spans() -> None:
    src = 'pre  sql { SELECT 1 }  post\n'
    blocks = list(_find_sql_blocks(src))
    assert len(blocks) == 1
    start, end, body = blocks[0]
    assert src[start:end] == "sql { SELECT 1 }"
    assert body.strip() == "SELECT 1"


def test_find_sql_blocks_skips_unmatched_brace_gracefully() -> None:
    """If a `sql {` has no matching `}`, don't loop forever and don't
    emit garbage — leave the source alone so the main parser reports
    the syntax error."""
    src = 'sql { SELECT 1\n'  # no closing brace
    blocks = list(_find_sql_blocks(src))
    assert blocks == []
    out = _sql_transform(src)
    # Unmatched `sql {` passes through unchanged
    assert out == src
