"""Regression tests for the LSP scope-aware completion + signature help.

These exercise the pure helpers on _Server without spinning up a stdio
loop. Each test is a self-contained cobra4 snippet; the assertions
target the LSP responses the IDE would receive.
"""

from __future__ import annotations

from cobra4.tools.lsp import _Server


def _server() -> _Server:
    return _Server()


def test_completion_includes_keywords_and_builtins() -> None:
    s = _server()
    items = s._completions("x = 1\n", 0, 0)
    labels = {i["label"] for i in items}
    assert {"if", "fn", "match", "each", "True", "None"} <= labels
    assert {"len", "range", "print"} <= labels


def test_completion_drops_removed_keywords() -> None:
    """`with` and `data class` are NOT real cobra4 syntax — they should
    not be suggested as keywords (would mislead users into writing
    syntax that fails to parse)."""
    s = _server()
    items = s._completions("", 0, 0)
    labels = {i["label"] for i in items}
    assert "with" not in labels
    assert "data" not in labels


def test_completion_includes_function_params_inside_body() -> None:
    text = "fn handler(req, ctx) {\n    x = 1\n    \n}\n"
    s = _server()
    # Cursor inside the body, line 2 col 4
    items = s._completions(text, 2, 4)
    labels = {i["label"] for i in items}
    assert "req" in labels
    assert "ctx" in labels
    assert "x" in labels  # local assignment
    assert "handler" in labels  # function itself, callable from body too


def test_completion_includes_for_loop_var() -> None:
    text = "for item in xs {\n    \n}\n"
    s = _server()
    items = s._completions(text, 1, 4)
    labels = {i["label"] for i in items}
    assert "item" in labels


def test_completion_includes_each_loop_var() -> None:
    text = "result = each row in rows {\n    \n}\n"
    s = _server()
    items = s._completions(text, 1, 4)
    labels = {i["label"] for i in items}
    assert "row" in labels


def test_completion_includes_catch_binding() -> None:
    text = "try {\n    f()\n} catch ValueError as e {\n    \n}\n"
    s = _server()
    items = s._completions(text, 3, 4)
    labels = {i["label"] for i in items}
    assert "e" in labels


def test_member_completion_request() -> None:
    s = _server()
    text = "req.\n"
    items = s._completions(text, 0, 4)
    labels = {i["label"] for i in items}
    # Members of cobra4 Request
    assert {"method", "path", "params", "headers", "body", "json"} <= labels


def test_member_completion_command_result() -> None:
    s = _server()
    text = "result.\n"
    items = s._completions(text, 0, 7)
    labels = {i["label"] for i in items}
    assert {"stdout", "stderr", "returncode", "ok"} <= labels


def test_member_completion_unknown_falls_back_to_common_methods() -> None:
    s = _server()
    text = "stuff.\n"
    items = s._completions(text, 0, 6)
    labels = {i["label"] for i in items}
    # No specific shape registered → suggest common str/list/dict methods.
    assert "append" in labels  # list
    assert "upper" in labels   # str
    assert "keys" in labels    # dict


def test_signature_help_user_function() -> None:
    text = "fn greet(name, age) -> str = \"hi {name}\"\n\ngreet(\n"
    s = _server()
    sig = s._signature_help(text, 2, 6)
    assert sig is not None
    assert sig["activeParameter"] == 0
    assert "greet" in sig["signatures"][0]["label"]


def test_signature_help_active_parameter_advances_with_commas() -> None:
    text = "fn greet(name, age) = name\n\ngreet(\"ada\", \n"
    s = _server()
    sig = s._signature_help(text, 2, 13)
    assert sig is not None
    assert sig["activeParameter"] == 1


def test_signature_help_builtin_log() -> None:
    text = "log(\n"
    s = _server()
    sig = s._signature_help(text, 0, 4)
    assert sig is not None
    assert "log(" in sig["signatures"][0]["label"]


def test_signature_help_returns_none_outside_call() -> None:
    text = "x = 1 + 2\n"
    s = _server()
    sig = s._signature_help(text, 0, 5)
    assert sig is None


def test_signature_help_handles_unknown_function_gracefully() -> None:
    text = "totally_undefined(\n"
    s = _server()
    sig = s._signature_help(text, 0, 18)
    assert sig is None
