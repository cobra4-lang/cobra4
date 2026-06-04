from __future__ import annotations

from pathlib import Path

from cobra4 import cli
from cobra4 import idle as idle_module
from cobra4.idle import (
    _html,
    build_graph,
    complete_source,
    format_source,
    hover_source,
    inspect_source,
    load_snippets,
    project_tree,
    signature_source,
    run_terminal_command,
    run_source,
    save_custom_snippets,
)
from cobra4.parser import parse


def test_idle_inspect_source_returns_python_metrics_and_graph() -> None:
    source = """\
fn handler(req) {
    return {"ok": True}
}

items = read("./in.json")
save(items, "./out.json")
serve handler on :8080
"""

    result = inspect_source(source, source_path="app.c4")

    assert result.ok
    assert "_c4_serve(handler, 8080)" in result.python
    assert result.metrics["cobra4Loc"] > 0
    assert result.metrics["pythonLoc"] >= result.metrics["cobra4Loc"]
    kinds = {node["kind"] for node in result.graph["nodes"]}
    assert {"function", "io-read", "io-save", "http"} <= kinds
    assert result.symbols[0]["name"] == "handler"


def test_idle_graph_includes_schedules_events_and_resources() -> None:
    module = parse("""\
resource bucket = aws.s3 { name: "demo" }
every 5 seconds { log("tick") }
on event from queue("jobs") { log("job") }
""")

    graph = build_graph(module)

    kinds = {node["kind"] for node in graph["nodes"]}
    assert {"resource", "schedule", "event", "log"} <= kinds


def test_idle_editor_completions_include_builtins_scope_and_members() -> None:
    member_source = """\
fn greet(user) {
    return user.
}
"""
    scope_source = """\
fn greet(user) {
    return user
}

message = gr
"""

    member_items = complete_source(member_source, 1, len("    return user."))["items"]
    top_level_items = complete_source(scope_source, 4, len("message = gr"))["items"]

    assert any(item["label"] == "upper" for item in member_items)
    assert any(item["label"] == "greet" for item in top_level_items)
    assert any(item["label"] == "save" for item in top_level_items)


def test_idle_editor_signature_hover_and_formatting() -> None:
    valid_source = """\
fn greet(name: str) {
return "hi {name}"
}

greet("ada")
"""
    incomplete_source = """\
fn greet(name: str) {
    return "hi {name}"
}

greet(
"""

    formatted = format_source(valid_source)
    signature = signature_source(incomplete_source, 4, len("greet("))["signature"]
    hover = hover_source(valid_source, 4, 2)["contents"]

    assert formatted["ok"]
    assert '    return "hi {name}"' in formatted["source"]
    assert signature["signatures"][0]["label"].startswith("greet(")
    assert "**greet**" in hover


def test_idle_project_tree_skips_heavy_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c4").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    result = project_tree(str(tmp_path))

    assert result["ok"]
    names = {child["name"] for child in result["tree"]["children"]}
    assert "src" in names
    assert ".git" not in names


def test_idle_custom_snippets_roundtrip(tmp_path: Path) -> None:
    saved = save_custom_snippets(
        str(tmp_path),
        [
            {
                "title": "My bolt",
                "category": "Custom",
                "description": "Reusable custom code.",
                "code": 'log("bolt")',
            }
        ],
    )
    loaded = load_snippets(str(tmp_path))

    assert saved["ok"]
    custom = [item for item in loaded["snippets"] if item.get("custom")]
    assert custom[0]["title"] == "My bolt"
    assert custom[0]["code"].endswith("\n")


def test_idle_terminal_command_runs_in_project_root(tmp_path: Path) -> None:
    result = run_terminal_command("pwd", cwd=str(tmp_path), timeout=10)

    assert result["ok"], result["stderr"]
    assert result["stdout"].strip() == str(tmp_path)


def test_idle_ui_includes_theme_icons_and_tree_auto_refresh() -> None:
    html = _html()

    assert 'id="themeBtn"' in html
    assert 'body[data-theme="dark"]' in html
    assert "fileIconClass" in html
    assert "startTreeAutoRefresh" in html
    assert "openTreePaths" in html
    assert 'id="snippetModal"' in html
    assert 'title="Inspect"' in html
    assert "modalInsertSnippetBtn" in html


def test_idle_run_source_executes_from_requested_cwd(tmp_path: Path) -> None:
    source = 'save(["ok"], "./idle_run.json")\nprint("done")\n'

    result = run_source(
        source,
        source_path="scratch.c4",
        cwd=str(tmp_path),
        timeout=10,
    )

    assert result["ok"], result["stderr"]
    assert result["stdout"].strip() == "done"
    assert (tmp_path / "idle_run.json").exists()


def test_idle_run_source_preserves_runtime_failure_status(tmp_path: Path) -> None:
    result = run_source(
        'raise Exception("boom")\n',
        source_path="scratch.c4",
        cwd=str(tmp_path),
        timeout=10,
    )

    assert not result["ok"]
    assert result["returncode"] == 1
    assert "boom" in result["stderr"]


def test_cli_idle_subcommand_wires_options(monkeypatch) -> None:
    seen = {}

    def fake_cmd_idle(args):
        seen.update(
            host=args.host,
            port=args.port,
            no_browser=args.no_browser,
            verbose=args.verbose,
        )
        return 0

    monkeypatch.setattr(cli, "cmd_idle", fake_cmd_idle)

    assert cli.main(["idle", "--no-browser"]) == 0
    assert seen == {
        "host": "127.0.0.1",
        "port": 0,
        "no_browser": True,
        "verbose": False,
    }

    seen.clear()
    assert cli.main(["idle", "--host", "0.0.0.0", "--port", "0", "--no-browser"]) == 0
    assert seen == {
        "host": "0.0.0.0",
        "port": 0,
        "no_browser": True,
        "verbose": False,
    }


def test_idle_module_main_defaults_to_random_port(monkeypatch) -> None:
    seen = {}

    def fake_serve(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(idle_module, "serve", fake_serve)

    assert idle_module.main(["--no-browser"]) == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 0
    assert seen["open_browser"] is False
