from __future__ import annotations

from pathlib import Path

from cobra4 import cli
from cobra4.idle import build_graph, inspect_source, run_source
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


def test_idle_graph_includes_schedules_events_and_resources() -> None:
    module = parse("""\
resource bucket = aws.s3 { name: "demo" }
every 5 seconds { log("tick") }
on event from queue("jobs") { log("job") }
""")

    graph = build_graph(module)

    kinds = {node["kind"] for node in graph["nodes"]}
    assert {"resource", "schedule", "event", "log"} <= kinds


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

    assert cli.main(["idle", "--host", "0.0.0.0", "--port", "0", "--no-browser"]) == 0
    assert seen == {
        "host": "0.0.0.0",
        "port": 0,
        "no_browser": True,
        "verbose": False,
    }
