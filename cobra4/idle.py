"""Local browser-based IDLE for cobra4.

The app intentionally avoids frontend build tooling. `c4 idle` starts a
localhost HTTP server, serves this single-page UI, and exposes small JSON
endpoints backed by the same compiler pipeline as the CLI.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
from functools import lru_cache
import json
import os
import subprocess
import sys
import tempfile
import threading
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cobra4 import __version__
from cobra4 import ast_nodes as N
from cobra4.codegen import generate
from cobra4.dispatch_analysis import analyze as dispatch_analyze
from cobra4.lowering import lower
from cobra4.parser import ParseError, parse
from cobra4.plugins import preprocess
from cobra4.resolver import resolve
from cobra4.tools.fmt import _expr as fmt_expr
from cobra4.typecheck import check as typecheck

SAMPLE_SOURCE = """\
# Cobra4 IDLE scratch file

data class User(id: str, name: str)

users = [
    User(id="ada", name="Ada"),
    User(id="lin", name="Lin"),
]

fn greet(user) {
    return "hello {user.name}"
}

messages = each user in users { greet(user) }
save(messages, "./idle_messages.json")
log("saved", count=len(messages))
"""


@dataclass
class IdleDiagnostic:
    severity: str
    message: str
    line: int | None = None
    column: int | None = None


@dataclass
class IdleCompileResult:
    ok: bool
    python: str = ""
    graph: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, int] = field(default_factory=dict)
    diagnostics: list[IdleDiagnostic] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "python": self.python,
            "graph": self.graph,
            "metrics": self.metrics,
            "diagnostics": [dataclasses.asdict(d) for d in self.diagnostics],
        }


def inspect_source(source: str, *, source_path: str = "<idle>") -> IdleCompileResult:
    """Compile source and return Python, graph metadata, and diagnostics."""

    try:
        pre = preprocess(source)
        module = parse(pre.source, source_path=source_path)
    except ValueError as e:
        return _compile_error(str(e))
    except ParseError as e:
        return _compile_error(
            e.message,
            line=e.line,
            column=e.column,
            detail=str(e),
        )

    resolver_result = resolve(
        module,
        warn_undefined=True,
        warn_shadowing=True,
        extra_builtins=pre.extra_builtins,
    )
    type_diags = typecheck(module)
    dispatch_diags = dispatch_analyze(module)
    diagnostics = [_diagnostic_from_any(d) for d in resolver_result.diagnostics]
    diagnostics.extend(_diagnostic_from_any(d) for d in type_diags)
    diagnostics.extend(_diagnostic_from_any(d) for d in dispatch_diags)
    if any(d.severity == "error" for d in diagnostics):
        return IdleCompileResult(
            ok=False,
            graph=build_graph(module),
            metrics=_metrics(source, ""),
            diagnostics=diagnostics,
        )

    try:
        lowered = lower(module)
        result = generate(lowered, cobra4_path=source_path)
        code = _inject_plugin_imports(result.code, pre.plugins)
    except Exception as e:  # pragma: no cover - defensive UI boundary
        return _compile_error(f"{type(e).__name__}: {e}")

    return IdleCompileResult(
        ok=True,
        python=code,
        graph=build_graph(module),
        metrics=_metrics(source, code),
        diagnostics=diagnostics,
    )


def run_source(
    source: str,
    *,
    source_path: str = "idle_scratch.c4",
    argv: list[str] | None = None,
    cwd: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Run source via the CLI so tracebacks keep cobra4 source mapping."""

    compiled = inspect_source(source, source_path=source_path)
    if not compiled.ok:
        payload = compiled.to_json()
        payload.update(
            {
                "ok": False,
                "returncode": 2,
                "stdout": "",
                "stderr": _diagnostics_text(compiled.diagnostics),
            }
        )
        return payload

    argv = argv or []
    cwd_path = Path(cwd or os.getcwd())
    with tempfile.TemporaryDirectory(prefix="cobra4_idle_") as tmp:
        c4_path = Path(tmp) / Path(source_path).name
        c4_path.write_text(source, encoding="utf-8")
        env = os.environ.copy()
        project_root = Path(__file__).resolve().parent.parent
        env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, "-m", "cobra4.cli", "run", str(c4_path)]
        if argv:
            cmd.extend(["--", *argv])
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            payload = compiled.to_json()
            payload.update(
                {
                    "ok": False,
                    "returncode": 124,
                    "stdout": e.stdout or "",
                    "stderr": f"Timed out after {timeout:g}s",
                }
            )
            return payload
    payload = compiled.to_json()
    payload.update(
        {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    )
    return payload


def build_graph(module: N.Module) -> dict[str, Any]:
    """Build a lightweight visual model of what the program asks systems to do."""

    builder = _GraphBuilder()
    builder.add_node("program", "Program", "program", "entry point", 0)
    for stmt in module.body:
        builder.visit_stmt(stmt, parent="program")
    return {
        "nodes": list(builder.nodes.values()),
        "edges": builder.edges,
    }


class _GraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, str]] = []
        self._counter = 0

    def add_node(
        self,
        node_id: str | None,
        label: str,
        kind: str,
        detail: str = "",
        line: int | None = None,
    ) -> str:
        if node_id is None:
            self._counter += 1
            node_id = f"n{self._counter}"
        if node_id not in self.nodes:
            self.nodes[node_id] = {
                "id": node_id,
                "label": label,
                "kind": kind,
                "detail": detail,
                "line": line or 0,
            }
        return node_id

    def add_edge(self, source: str, target: str, label: str = "") -> None:
        if source != target:
            self.edges.append({"source": source, "target": target, "label": label})

    def visit_stmt(self, stmt: N.Stmt, *, parent: str) -> None:
        line = stmt.loc.line if stmt.loc else 0
        if isinstance(stmt, N.Use):
            label = stmt.alias or stmt.target
            node = self.add_node(None, label, "import", "use", line)
            self.add_edge(parent, node, "imports")
            return
        if isinstance(stmt, N.FnDecl):
            node = self.add_node(f"fn:{stmt.name}", stmt.name, "function", "fn", line)
            self.add_edge(parent, node, "defines")
            for inner in stmt.block or []:
                self.visit_stmt(inner, parent=node)
            if stmt.body is not None:
                self.visit_expr(stmt.body, parent=node)
            return
        if isinstance(stmt, N.ClassDecl):
            node = self.add_node(
                f"class:{stmt.name}", stmt.name, "class", "class", line
            )
            self.add_edge(parent, node, "defines")
            for inner in stmt.body:
                self.visit_stmt(inner, parent=node)
            return
        if isinstance(stmt, N.DataClassDecl):
            node = self.add_node(f"data:{stmt.name}", stmt.name, "data", "data", line)
            self.add_edge(parent, node, "defines")
            return
        if isinstance(stmt, N.DataSumDecl):
            node = self.add_node(f"data:{stmt.name}", stmt.name, "data", "sum", line)
            self.add_edge(parent, node, "defines")
            for variant in stmt.variants:
                v_node = self.add_node(
                    None, variant.name, "data", f"variant of {stmt.name}", line
                )
                self.add_edge(node, v_node, "variant")
            return
        if isinstance(stmt, N.Assign):
            for target in stmt.targets:
                if isinstance(target, N.Name):
                    node = self.add_node(
                        f"value:{target.name}", target.name, "value", "assign", line
                    )
                    self.add_edge(parent, node, "sets")
            self.visit_expr(stmt.value, parent=parent)
            return
        if isinstance(stmt, N.ExprStmt):
            self.visit_expr(stmt.value, parent=parent)
            return
        if isinstance(stmt, N.Return):
            if stmt.value is not None:
                self.visit_expr(stmt.value, parent=parent)
            return
        if isinstance(stmt, N.For):
            node = self.add_node(None, f"for {stmt.var}", "flow", "loop", line)
            self.add_edge(parent, node, "loops")
            self.visit_expr(stmt.iterable, parent=node)
            for inner in stmt.body:
                self.visit_stmt(inner, parent=node)
            return
        if isinstance(stmt, N.Each):
            detail = "parallel" if stmt.parallel else "each"
            node = self.add_node(None, f"each {stmt.var}", "flow", detail, line)
            self.add_edge(parent, node, "maps")
            self.visit_expr(stmt.iterable, parent=node)
            for inner in stmt.body:
                self.visit_stmt(inner, parent=node)
            return
        if isinstance(stmt, N.Every):
            node = self.add_node(
                None, f"every {stmt.seconds:g}s", "schedule", "timer", line
            )
            self.add_edge(parent, node, "schedules")
            for inner in stmt.body:
                self.visit_stmt(inner, parent=node)
            return
        if isinstance(stmt, N.OnEvent):
            node = self.add_node(None, "event", "event", fmt_expr(stmt.source), line)
            self.add_edge(parent, node, "listens")
            for inner in stmt.body:
                self.visit_stmt(inner, parent=node)
            return
        if isinstance(stmt, N.Serve):
            label = f":{stmt.port}"
            detail = fmt_expr(stmt.handler)
            node = self.add_node(None, label, "http", detail, line)
            self.add_edge(parent, node, "serves")
            return
        if isinstance(stmt, N.Deploy):
            node = self.add_node(
                None, fmt_expr(stmt.target), "deploy", fmt_expr(stmt.handler), line
            )
            self.add_edge(parent, node, "deploys")
            for inner in stmt.body:
                self.visit_stmt(inner, parent=node)
            return
        if isinstance(stmt, N.WorkflowDecl):
            node = self.add_node(
                f"workflow:{stmt.name}", stmt.name, "workflow", "workflow", line
            )
            self.add_edge(parent, node, "orchestrates")
            for task in stmt.tasks:
                t_node = self.add_node(
                    None, task.var, "task", fmt_expr(task.call), line
                )
                self.add_edge(node, t_node, "task")
            return
        if isinstance(stmt, N.ResourceDecl):
            node = self.add_node(None, stmt.name, "resource", stmt.adapter_path, line)
            self.add_edge(parent, node, "declares")
            return
        if isinstance(stmt, N.If):
            node = self.add_node(None, "if", "flow", fmt_expr(stmt.cond), line)
            self.add_edge(parent, node, "branches")
            for inner in stmt.body + stmt.orelse:
                self.visit_stmt(inner, parent=node)
            for _cond, body in stmt.elifs:
                for inner in body:
                    self.visit_stmt(inner, parent=node)
            return
        if isinstance(stmt, N.Match):
            node = self.add_node(None, "match", "flow", fmt_expr(stmt.subject), line)
            self.add_edge(parent, node, "branches")
            for case in stmt.cases:
                for inner in case.body:
                    self.visit_stmt(inner, parent=node)
            return
        if isinstance(stmt, N.Try):
            node = self.add_node(None, "try", "flow", "error boundary", line)
            self.add_edge(parent, node, "guards")
            for inner in stmt.body + stmt.finally_body:
                self.visit_stmt(inner, parent=node)
            for catch in stmt.catches:
                for inner in catch.body:
                    self.visit_stmt(inner, parent=node)
            return
        if isinstance(stmt, N.Sandbox):
            node = self.add_node(
                None, "sandbox", "effect", ", ".join(stmt.effects), line
            )
            self.add_edge(parent, node, "limits")
            for inner in stmt.body:
                self.visit_stmt(inner, parent=node)
            return

    def visit_expr(self, expr: N.Expr | None, *, parent: str) -> None:
        if expr is None:
            return
        line = expr.loc.line if expr.loc else 0
        if isinstance(expr, N.Call):
            name = _call_name(expr.func)
            kind = _call_kind(name)
            if kind:
                node = self.add_node(None, name, kind, _call_detail(expr), line)
                self.add_edge(parent, node, "calls")
            self.visit_expr(expr.func, parent=parent)
            for arg in expr.args:
                self.visit_expr(arg.value, parent=parent)
            return
        if isinstance(expr, N.EachExpr):
            detail = "parallel" if expr.parallel else "each"
            node = self.add_node(None, f"each {expr.var}", "flow", detail, line)
            self.add_edge(parent, node, "maps")
            self.visit_expr(expr.iterable, parent=node)
            for inner in expr.body:
                self.visit_stmt(inner, parent=node)
            return
        for child in _iter_expr_children(expr):
            self.visit_expr(child, parent=parent)


def _iter_expr_children(expr: N.Expr) -> list[N.Expr]:
    children: list[N.Expr] = []
    for field_def in dataclasses.fields(expr):
        value = getattr(expr, field_def.name)
        if isinstance(value, N.Expr):
            children.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, N.Expr):
                    children.append(item)
                elif isinstance(item, tuple):
                    children.extend(x for x in item if isinstance(x, N.Expr))
        elif isinstance(value, tuple):
            children.extend(x for x in value if isinstance(x, N.Expr))
    return children


def _call_name(expr: N.Expr | None) -> str:
    if isinstance(expr, N.Name):
        return expr.name
    if isinstance(expr, N.Attr):
        prefix = _call_name(expr.target)
        return f"{prefix}.{expr.name}" if prefix else expr.name
    return ""


def _call_kind(name: str) -> str:
    if name in {"read"}:
        return "io-read"
    if name in {"save"}:
        return "io-save"
    if name == "log" or name.startswith("log."):
        return "log"
    if name in {"secret"}:
        return "secret"
    if name in {"run", "fan_out", "inventory"}:
        return "fleet"
    if name in {"aws", "gcp", "azure", "k8s", "fly"} or name.startswith(
        ("aws.", "gcp.", "azure.", "k8s.", "fly.")
    ):
        return "cloud"
    return ""


def _call_detail(call: N.Call) -> str:
    try:
        return ", ".join(fmt_expr(arg.value) for arg in call.args[:3])
    except Exception:
        return ""


def _inject_plugin_imports(code: str, plugins: list[Any]) -> str:
    plugin_imports = "\n".join(
        f"from {p.runtime_module} import *  # noqa: F401,F403  (plugin: {p.name})"
        for p in plugins
        if p.runtime_module
    )
    if not plugin_imports:
        return code
    return code.replace("# DO NOT EDIT", plugin_imports + "\n# DO NOT EDIT", 1)


def _compile_error(
    message: str,
    *,
    line: int | None = None,
    column: int | None = None,
    detail: str | None = None,
) -> IdleCompileResult:
    return IdleCompileResult(
        ok=False,
        metrics=_metrics("", ""),
        diagnostics=[
            IdleDiagnostic(
                severity="error",
                message=detail or message,
                line=line,
                column=column,
            )
        ],
    )


def _diagnostic_from_any(d: Any) -> IdleDiagnostic:
    loc = getattr(d, "loc", None)
    return IdleDiagnostic(
        severity=getattr(d, "severity", "warning"),
        message=str(d),
        line=getattr(loc, "line", None),
        column=getattr(loc, "column", None),
    )


def _metrics(cobra4_source: str, python_source: str) -> dict[str, int]:
    c4_loc = _effective_loc(cobra4_source)
    py_loc = _effective_loc(python_source)
    return {
        "cobra4Loc": c4_loc,
        "pythonLoc": py_loc,
        "savedLoc": max(py_loc - c4_loc, 0),
    }


def _effective_loc(source: str) -> int:
    return sum(
        1
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _diagnostics_text(diagnostics: list[IdleDiagnostic]) -> str:
    return "\n".join(d.message for d in diagnostics)


class _IdleHandler(BaseHTTPRequestHandler):
    server_version = "Cobra4IDLE/1.0"

    @property
    def idle_server(self) -> "IdleServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.idle_server.verbose:
            super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(_html())
            return
        if parsed.path == "/assets/logo-128.png":
            self._send_bytes(_logo_bytes(), "image/png")
            return
        if parsed.path == "/api/sample":
            self._send_json({"source": SAMPLE_SOURCE, "path": "idle_scratch.c4"})
            return
        if parsed.path == "/api/file":
            params = parse_qs(parsed.query)
            path = params.get("path", [""])[0]
            self._handle_open(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/compile":
            payload = self._read_json()
            result = inspect_source(
                payload.get("source", ""),
                source_path=payload.get("path") or "idle_scratch.c4",
            )
            self._send_json(result.to_json())
            return
        if parsed.path == "/api/run":
            payload = self._read_json()
            result = run_source(
                payload.get("source", ""),
                source_path=payload.get("path") or "idle_scratch.c4",
                argv=payload.get("argv") or [],
                cwd=self.idle_server.cwd,
                timeout=float(payload.get("timeout") or 10),
            )
            self._send_json(result)
            return
        if parsed.path == "/api/save":
            payload = self._read_json()
            self._handle_save(payload)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def _handle_open(self, path: str) -> None:
        try:
            target = _resolve_user_path(path, self.idle_server.cwd)
            source = target.read_text(encoding="utf-8")
        except OSError as e:
            self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        self._send_json({"ok": True, "source": source, "path": str(target)})

    def _handle_save(self, payload: dict[str, Any]) -> None:
        path = payload.get("path") or "idle_scratch.c4"
        source = payload.get("source", "")
        try:
            target = _resolve_user_path(path, self.idle_server.cwd)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
        except OSError as e:
            self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        self._send_json({"ok": True, "path": str(target)})

    def _send_html(self, text: str) -> None:
        self._send_bytes(text.encode("utf-8"), "text/html; charset=utf-8")

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _send_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class IdleServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        cwd: str,
        verbose: bool = False,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.cwd = cwd
        self.verbose = verbose


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    cwd: str | None = None,
    verbose: bool = False,
) -> int:
    server = IdleServer(
        (host, port),
        _IdleHandler,
        cwd=str(Path(cwd or os.getcwd()).resolve()),
        verbose=verbose,
    )
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"Cobra4 IDLE running at {url}")
    print("Press Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="c4 idle", description="cobra4 IDLE")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    return serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        verbose=args.verbose,
    )


def _resolve_user_path(path: str, cwd: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return candidate.resolve()


@lru_cache(maxsize=1)
def _logo_bytes() -> bytes:
    try:
        return (resources.files("cobra4.assets") / "logo-128.png").read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAAklEQVR4AewaftIA"
            "AAHlSURBVO3BQW7DMAwEwPz/06c9BbEMxkKqzQ5sQIMv4H3f7/f7/X6/3+83"
            "AAAAAAAAAAAAAAAAAAAAAADwL0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mS"
            "JMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAA"
            "AAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAA"
            "AAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8v"
            "AAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4"
            "nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4"
            "P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAA"
            "AAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAA"
            "AAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5n"
            "Pp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mS"
            "JMk4nU5nPp8vAAAAAAAAAAAAAAAAAAD4P0mSJMk4nU5nPp8vAAAAAAAAAAAAAAAA"
            "AAD4P0mSJMn4A1dFAAG5dcA3AAAAAElFTkSuQmCC"
        )


def _html() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cobra4 IDLE</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f7f8fb;
  --surface: #ffffff;
  --surface-2: #eef3f6;
  --ink: #172026;
  --muted: #5f6f7a;
  --line: #d8e0e6;
  --primary: #006a6a;
  --primary-ink: #ffffff;
  --accent: #8a4b00;
  --error: #ba1a1a;
  --ok: #126d3a;
  --code-bg: #101418;
  --code-ink: #e7eef2;
  font-family: Inter, Roboto, "Segoe UI", system-ui, sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  height: 100vh;
  overflow: hidden;
}}
button, input, textarea {{ font: inherit; }}
.app {{
  display: grid;
  grid-template-rows: 64px 1fr;
  min-height: 100vh;
}}
.bar {{
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 16px;
  padding: 0 18px;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}}
.brand {{
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 180px;
}}
.brand img {{ width: 38px; height: 38px; }}
.brand strong {{ display: block; font-size: 16px; line-height: 1.1; }}
.brand span {{ color: var(--muted); font-size: 12px; }}
.pathbar {{
  display: grid;
  grid-template-columns: minmax(160px, 1fr) auto auto;
  gap: 8px;
  align-items: center;
}}
.pathbar input {{
  width: 100%;
  height: 38px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0 12px;
  background: var(--surface-2);
  color: var(--ink);
}}
.actions {{
  display: flex;
  gap: 8px;
  align-items: center;
}}
.btn {{
  height: 38px;
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 0 13px;
  background: var(--surface-2);
  color: var(--ink);
  cursor: pointer;
}}
.btn:hover {{ border-color: #9fb3bd; }}
.btn.primary {{
  background: var(--primary);
  color: var(--primary-ink);
}}
.btn.tonal {{
  background: #e2f1ef;
  color: #003737;
}}
.shell {{
  display: grid;
  grid-template-columns: minmax(320px, 1fr) minmax(360px, 1fr);
  min-height: 0;
}}
.editorPane, .resultPane {{
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: 44px 1fr;
}}
.editorPane {{ border-right: 1px solid var(--line); }}
.paneHead {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 0 14px;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}}
.paneHead h1, .paneHead h2 {{
  margin: 0;
  font-size: 14px;
  font-weight: 650;
}}
.stats {{
  display: flex;
  gap: 8px;
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}}
#source {{
  width: 100%;
  height: 100%;
  resize: none;
  border: 0;
  outline: 0;
  padding: 18px;
  background: #fbfcfd;
  color: #101820;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 14px;
  line-height: 1.55;
  tab-size: 4;
}}
.tabs {{
  display: flex;
  height: 44px;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}}
.tab {{
  border: 0;
  border-bottom: 3px solid transparent;
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  padding: 0 16px;
  cursor: pointer;
}}
.tab.active {{
  color: var(--primary);
  border-bottom-color: var(--primary);
  font-weight: 650;
}}
.view {{
  display: none;
  min-height: 0;
  overflow: auto;
  background: var(--surface);
}}
.view.active {{ display: block; }}
pre {{
  margin: 0;
  min-height: 100%;
  padding: 18px;
  overflow: auto;
  background: var(--code-bg);
  color: var(--code-ink);
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
  line-height: 1.55;
}}
#output {{
  background: #0e1116;
}}
#problems {{
  padding: 14px;
}}
.problem {{
  border-left: 4px solid var(--line);
  padding: 10px 12px;
  margin-bottom: 10px;
  background: #f8fafb;
  border-radius: 6px;
  white-space: pre-wrap;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
}}
.problem.error {{ border-left-color: var(--error); }}
.problem.warning {{ border-left-color: var(--accent); }}
.graphWrap {{
  height: 100%;
  min-height: 420px;
  background: #f9fbfc;
}}
svg.graph {{ display: block; width: 100%; height: 100%; min-height: 420px; }}
.edge {{ stroke: #9db0ba; stroke-width: 1.5; marker-end: url(#arrow); }}
.edgeLabel {{ fill: #667781; font-size: 11px; }}
.node rect {{ rx: 6; ry: 6; stroke-width: 1; }}
.node text {{ font-size: 12px; fill: #102027; pointer-events: none; }}
.node .detail {{ fill: #53666f; font-size: 10px; }}
.kind-program rect {{ fill: #e2f1ef; stroke: #77a9a6; }}
.kind-function rect {{ fill: #eef0ff; stroke: #98a0d6; }}
.kind-data rect {{ fill: #fff0d8; stroke: #d39a41; }}
.kind-http rect {{ fill: #e8f5e9; stroke: #74a874; }}
.kind-io-read rect, .kind-io-save rect {{ fill: #e7f0f7; stroke: #77a2bf; }}
.kind-log rect {{ fill: #f6ecf4; stroke: #bf83b5; }}
.kind-flow rect {{ fill: #f2f5f6; stroke: #9fb0b8; }}
.kind-schedule rect, .kind-event rect {{ fill: #fff8e1; stroke: #c5a549; }}
.kind-resource rect, .kind-deploy rect, .kind-cloud rect {{ fill: #e9f6f1; stroke: #5aa382; }}
.kind-secret rect, .kind-effect rect {{ fill: #fdecec; stroke: #ce7777; }}
.kind-fleet rect, .kind-task rect, .kind-workflow rect {{ fill: #edf4ff; stroke: #7fa7d6; }}
.kind-import rect, .kind-class rect, .kind-value rect {{ fill: #f7f8fb; stroke: #c2cdd3; }}
.status {{
  color: var(--muted);
  font-size: 12px;
}}
.status.ok {{ color: var(--ok); }}
.status.err {{ color: var(--error); }}
@media (max-width: 860px) {{
  body {{ overflow: auto; }}
  .app {{ min-height: 100vh; }}
  .bar {{
    height: auto;
    grid-template-columns: 1fr;
    align-items: stretch;
    padding: 10px;
  }}
  .pathbar {{ grid-template-columns: 1fr; }}
  .actions {{ flex-wrap: wrap; }}
  .shell {{ grid-template-columns: 1fr; grid-template-rows: 50vh 50vh; }}
  .editorPane {{ border-right: 0; border-bottom: 1px solid var(--line); }}
}}
</style>
</head>
<body>
<div class="app">
  <header class="bar">
    <div class="brand">
      <img src="/assets/logo-128.png" alt="cobra4 logo">
      <div><strong>Cobra4 IDLE</strong><span>v{__version__}</span></div>
    </div>
    <div class="pathbar">
      <input id="path" value="idle_scratch.c4" aria-label="file path">
      <button class="btn" id="openBtn">Open</button>
      <button class="btn" id="saveBtn">Save</button>
    </div>
    <div class="actions">
      <button class="btn tonal" id="checkBtn">Check</button>
      <button class="btn primary" id="runBtn">Run</button>
      <button class="btn" id="newBtn">New</button>
    </div>
  </header>
  <main class="shell">
    <section class="editorPane">
      <div class="paneHead">
        <h1>Source</h1>
        <div class="stats">
          <span id="c4Loc">C4 0</span>
          <span id="pyLoc">Python 0</span>
          <span id="savedLoc">Saved 0</span>
        </div>
      </div>
      <textarea id="source" spellcheck="false"></textarea>
    </section>
    <section class="resultPane">
      <nav class="tabs" aria-label="result tabs">
        <button class="tab active" data-tab="outputView">Output</button>
        <button class="tab" data-tab="pythonView">Python</button>
        <button class="tab" data-tab="graphView">Grafica</button>
        <button class="tab" data-tab="problemView">Problemi</button>
      </nav>
      <section id="outputView" class="view active"><pre id="output"></pre></section>
      <section id="pythonView" class="view"><pre id="python"></pre></section>
      <section id="graphView" class="view"><div class="graphWrap" id="graph"></div></section>
      <section id="problemView" class="view"><div id="problems"></div></section>
    </section>
  </main>
</div>
<script>
const source = document.getElementById("source");
const pathInput = document.getElementById("path");
const output = document.getElementById("output");
const python = document.getElementById("python");
const problems = document.getElementById("problems");
const statusEls = {{
  c4: document.getElementById("c4Loc"),
  py: document.getElementById("pyLoc"),
  saved: document.getElementById("savedLoc")
}};
let compileTimer = null;
let lastGraph = {{nodes: [], edges: []}};

async function api(path, payload) {{
  const response = await fetch(path, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify(payload)
  }});
  return response.json();
}}

function setTab(id) {{
  document.querySelectorAll(".tab").forEach(btn => {{
    btn.classList.toggle("active", btn.dataset.tab === id);
  }});
  document.querySelectorAll(".view").forEach(view => {{
    view.classList.toggle("active", view.id === id);
  }});
  if (id === "graphView") renderGraph(lastGraph);
}}

function setStatus(ok, text) {{
  output.dataset.ok = ok ? "1" : "0";
  if (!text) return;
  output.textContent = text;
}}

function scheduleCompile() {{
  clearTimeout(compileTimer);
  compileTimer = setTimeout(compileNow, 220);
}}

async function compileNow() {{
  const result = await api("/api/compile", {{
    source: source.value,
    path: pathInput.value
  }});
  updateCompile(result);
  return result;
}}

function updateCompile(result) {{
  python.textContent = result.python || "";
  lastGraph = result.graph || {{nodes: [], edges: []}};
  const metrics = result.metrics || {{}};
  statusEls.c4.textContent = `C4 ${{metrics.cobra4Loc || 0}}`;
  statusEls.py.textContent = `Python ${{metrics.pythonLoc || 0}}`;
  statusEls.saved.textContent = `Saved ${{metrics.savedLoc || 0}}`;
  renderProblems(result.diagnostics || []);
  renderGraph(lastGraph);
}}

function renderProblems(items) {{
  problems.innerHTML = "";
  if (!items.length) {{
    const node = document.createElement("div");
    node.className = "problem";
    node.textContent = "OK";
    problems.appendChild(node);
    return;
  }}
  for (const item of items) {{
    const node = document.createElement("div");
    node.className = `problem ${{item.severity || ""}}`;
    const loc = item.line ? `${{item.line}}${{item.column ? ":" + item.column : ""}} ` : "";
    node.textContent = `${{(item.severity || "info").toUpperCase()}} ${{loc}}${{item.message}}`;
    problems.appendChild(node);
  }}
}}

function renderGraph(graph) {{
  const host = document.getElementById("graph");
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  if (!nodes.length) {{
    host.innerHTML = `<svg class="graph" viewBox="0 0 800 420"><text x="28" y="40" fill="#667781">No graph</text></svg>`;
    return;
  }}
  const cols = Math.max(1, Math.ceil(Math.sqrt(nodes.length)));
  const w = Math.max(820, cols * 220 + 80);
  const rows = Math.ceil(nodes.length / cols);
  const h = Math.max(420, rows * 130 + 80);
  const positions = new Map();
  nodes.forEach((node, index) => {{
    const col = index % cols;
    const row = Math.floor(index / cols);
    positions.set(node.id, {{x: 40 + col * 220, y: 40 + row * 130}});
  }});
  const escape = s => String(s || "").replace(/[&<>"]/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}}[ch]));
  let svg = `<svg class="graph" viewBox="0 0 ${{w}} ${{h}}" role="img">
    <defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#9db0ba"/></marker></defs>`;
  for (const edge of edges) {{
    const a = positions.get(edge.source);
    const b = positions.get(edge.target);
    if (!a || !b) continue;
    const x1 = a.x + 150;
    const y1 = a.y + 32;
    const x2 = b.x;
    const y2 = b.y + 32;
    svg += `<path class="edge" d="M ${{x1}} ${{y1}} C ${{x1 + 40}} ${{y1}}, ${{x2 - 40}} ${{y2}}, ${{x2}} ${{y2}}"/>`;
    if (edge.label) {{
      svg += `<text class="edgeLabel" x="${{(x1 + x2) / 2 - 20}}" y="${{(y1 + y2) / 2 - 8}}">${{escape(edge.label)}}</text>`;
    }}
  }}
  for (const node of nodes) {{
    const pos = positions.get(node.id);
    const cls = `kind-${{String(node.kind || "value").replace(/[^a-z0-9-]/g, "-")}}`;
    svg += `<g class="node ${{cls}}" transform="translate(${{pos.x}},${{pos.y}})">
      <rect width="150" height="64"></rect>
      <text x="12" y="24">${{escape(node.label).slice(0, 18)}}</text>
      <text class="detail" x="12" y="44">${{escape(node.detail || node.kind).slice(0, 24)}}</text>
    </g>`;
  }}
  svg += `</svg>`;
  host.innerHTML = svg;
}}

async function runNow() {{
  setTab("outputView");
  output.textContent = "Running...";
  const result = await api("/api/run", {{
    source: source.value,
    path: pathInput.value,
    timeout: 10
  }});
  updateCompile(result);
  const chunks = [];
  chunks.push(`exit ${{result.returncode}}`);
  if (result.stdout) chunks.push("\\n[stdout]\\n" + result.stdout);
  if (result.stderr) chunks.push("\\n[stderr]\\n" + result.stderr);
  output.textContent = chunks.join("\\n");
}}

async function checkNow() {{
  const result = await compileNow();
  setTab(result.ok ? "pythonView" : "problemView");
}}

async function openPath() {{
  const response = await fetch("/api/file?path=" + encodeURIComponent(pathInput.value));
  const result = await response.json();
  if (!result.ok) {{
    output.textContent = result.error || "open failed";
    setTab("outputView");
    return;
  }}
  pathInput.value = result.path;
  source.value = result.source;
  scheduleCompile();
}}

async function savePath() {{
  const result = await api("/api/save", {{
    path: pathInput.value,
    source: source.value
  }});
  output.textContent = result.ok ? `saved ${{result.path}}` : (result.error || "save failed");
  if (result.path) pathInput.value = result.path;
  setTab("outputView");
}}

document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => setTab(btn.dataset.tab)));
source.addEventListener("input", scheduleCompile);
document.getElementById("runBtn").addEventListener("click", runNow);
document.getElementById("checkBtn").addEventListener("click", checkNow);
document.getElementById("openBtn").addEventListener("click", openPath);
document.getElementById("saveBtn").addEventListener("click", savePath);
document.getElementById("newBtn").addEventListener("click", () => {{
  pathInput.value = "idle_scratch.c4";
  source.value = "";
  scheduleCompile();
  source.focus();
}});

fetch("/api/sample").then(r => r.json()).then(sample => {{
  source.value = sample.source;
  pathInput.value = sample.path;
  compileNow();
}});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
