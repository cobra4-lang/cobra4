"""M5 tests: plugin loader and reference SQL plugin."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr

import pytest

from cobra4.plugins import preprocess, register_plugin
from cobra4.plugins.api import LanguagePlugin


def test_lang_use_strips_directive():
    src = "lang use sql\nx = 1\n"
    res = preprocess(src)
    assert "lang use" not in res.source
    assert "x = 1" in res.source
    assert any(p.name == "sql" for p in res.plugins)


def test_unknown_plugin_raises():
    with pytest.raises(ValueError, match="unknown language plugin"):
        preprocess("lang use definitely_not_a_real_plugin_name\n")


def test_register_and_load_custom_plugin():
    register_plugin(
        LanguagePlugin(
            name="upper",
            transform_source=lambda s: s.upper(),
            description="testing",
        )
    )
    res = preprocess("lang use upper\nx = 1\n")
    assert "X = 1" in res.source


def test_sql_plugin_rewrites_block():
    src = '''lang use sql
rows = sql {
    SELECT id, name FROM users WHERE age > 18
}
'''
    res = preprocess(src)
    assert "sql_run(" in res.source
    assert "SELECT id, name FROM users" in res.source
    assert "{" not in res.source.split("sql_run(")[1].split(")")[0]


def test_sql_plugin_runtime_logs():
    """sql_run logs but doesn't connect anywhere by default."""
    from cobra4.plugins.builtin import sql as sql_plugin
    from cobra4.runtime import observe

    buf = io.StringIO()
    observe.set_stream(buf)
    try:
        result = sql_plugin.sql_run("SELECT 1")
        assert result == []
        assert "sql.run" in buf.getvalue()
    finally:
        observe.set_stream(sys.stderr)
