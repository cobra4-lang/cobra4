"""Local browser-based Studio for cobra4.

The app intentionally avoids frontend build tooling. `c4 studio` starts a
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
import shutil
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
from cobra4.plugins.loader import preserve_plugin_constructs
from cobra4.resolver import resolve
from cobra4.tools.fmt import _expr as fmt_expr
from cobra4.tools.fmt import format_module
from cobra4.tools.lsp import _Server as LspServer
from cobra4.typecheck import check as typecheck

SAMPLE_SOURCE = """\
# Cobra4 Studio scratch file

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

DEFAULT_SNIPPETS: list[dict[str, str]] = [
    {
        "id": "etl-read-transform-save",
        "title": "ETL read -> transform -> save",
        "category": "Data",
        "description": "Read rows from one format, transform them, and save another format.",
        "code": """\
rows = read("./data/input.csv")

out = []
for r in rows {
    out.append({"name": r["name"].upper(), "age": int(r["age"])})
}

save(out, "./out/output.json")
log("etl done", input=len(rows), output=len(out))
""",
    },
    {
        "id": "http-handler",
        "title": "HTTP handler",
        "category": "Service",
        "description": "Pattern-match method/path pairs and register a local HTTP handler.",
        "code": """\
fn handler(req) {
    match (req.method, req.path) {
        case ("GET", "/health") { return {"ok": True} }
        case _ { return (404, {"error": "not found"}) }
    }
}

serve handler on :8080
""",
    },
    {
        "id": "parallel-healthcheck",
        "title": "Parallel fan-out",
        "category": "Concurrency",
        "description": "Run work across many items and collect the results.",
        "code": """\
urls = ["https://www.python.org", "https://www.google.com"]

fn check(url) {
    try {
        body = read(url)
        return {"url": url, "ok": True, "size": len(body)}
    } catch Exception as e {
        return {"url": url, "ok": False, "error": str(e)}
    }
}

results = each url in urls in parallel(workers=8) { check(url) }
log("checked", total=len(results))
""",
    },
    {
        "id": "scheduled-job",
        "title": "Scheduled job",
        "category": "Daemon",
        "description": "Register work that runs under `c4 serve`.",
        "code": """\
state = {"ticks": 0}

every 5 seconds {
    state["ticks"] = state["ticks"] + 1
    log("tick", n=state["ticks"])
}
""",
    },
    {
        "id": "cobra4-test",
        "title": "Cobra4 test",
        "category": "Testing",
        "description": "Use the Cobra4 stdlib test helpers in tests/test_*.c4.",
        "code": """\
use cobra4.stdlib.test as t

fn test_example() {
    t.assert_eq(2 + 2, 4)
}
""",
    },
    {
        "id": "result-flow",
        "title": "Result flow",
        "category": "Errors",
        "description": "Return Ok/Err values and propagate failures with `?`.",
        "code": """\
fn parse_age(row) {
    try {
        return Ok(int(row["age"]))
    } catch Exception as e {
        return Err(str(e))
    }
}

age = parse_age({"age": "42"})?
log("age", value=age)
""",
    },
]

SKIPPED_PROJECT_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "build",
    "dist",
    "site",
    ".mypy_cache",
    ".ruff_cache",
}


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
    source_map: dict[str, Any] = field(default_factory=dict)
    graph: dict[str, Any] = field(default_factory=dict)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)
    diagnostics: list[IdleDiagnostic] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "python": self.python,
            "sourceMap": self.source_map,
            "graph": self.graph,
            "symbols": self.symbols,
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
            symbols=build_symbols(module),
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
        source_map=_source_map_payload(result.source_map),
        graph=build_graph(module),
        symbols=build_symbols(module),
        metrics=_metrics(source, code),
        diagnostics=diagnostics,
    )


def complete_source(source: str, line: int, column: int) -> dict[str, Any]:
    """Return LSP-powered completion items for the editor."""

    items = LspServer()._completions(source, line, column)
    return {"items": _dedupe_completion_items(items)}


def signature_source(source: str, line: int, column: int) -> dict[str, Any]:
    """Return LSP-powered signature help for the editor."""

    return {"signature": LspServer()._signature_help(source, line, column)}


def hover_source(source: str, line: int, column: int) -> dict[str, Any]:
    """Return hover markdown for the identifier at the cursor."""

    return {"contents": LspServer()._hover_info(source, line, column)}


def format_source(source: str, *, source_path: str = "<idle>") -> dict[str, Any]:
    """Format Cobra4 source using the same plugin-aware path as `c4 fmt`."""

    directives = _leading_lang_directives(source)
    try:
        sentinel_body, restorers, _plugins = preserve_plugin_constructs(source)
        module = parse(sentinel_body, source_path=source_path)
    except (ParseError, ValueError) as e:
        return {"ok": False, "source": source, "diagnostics": [_error_payload(e)]}

    formatted = format_module(module)
    for sentinel, original in restorers:
        formatted = formatted.replace(sentinel + "()", original)
        formatted = formatted.replace(sentinel, original)
    if directives:
        formatted = "\n".join(directives) + "\n\n" + formatted
    return {"ok": True, "source": formatted, "diagnostics": []}


def project_tree(cwd: str, *, max_entries: int = 800) -> dict[str, Any]:
    """Return a bounded file tree for the current project root."""

    root = Path(cwd).resolve()
    count = 0

    def rel(path: Path) -> str:
        try:
            return "." if path == root else path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()

    def walk(path: Path) -> dict[str, Any]:
        nonlocal count
        count += 1
        node = {
            "name": path.name or path.as_posix(),
            "path": rel(path),
            "kind": "dir" if path.is_dir() else "file",
        }
        if count >= max_entries:
            node["truncated"] = True
            return node
        if path.is_dir():
            children: list[dict[str, Any]] = []
            try:
                entries = [p for p in path.iterdir() if not _skip_project_path(p)]
            except OSError:
                entries = []
            entries.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
            for child in entries:
                if count >= max_entries:
                    children.append(
                        {
                            "name": "...",
                            "path": rel(path),
                            "kind": "more",
                            "truncated": True,
                        }
                    )
                    break
                children.append(walk(child))
            node["children"] = children
        return node

    return {"ok": True, "root": str(root), "tree": walk(root)}


def search_project(
    cwd: str,
    query: str,
    *,
    max_results: int = 80,
    max_file_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Search text files under the project root with conservative bounds."""

    root = Path(cwd).resolve()
    needle = query.strip()
    if not needle:
        return {"ok": True, "query": needle, "results": [], "truncated": False}
    lowered = needle.lower()
    results: list[dict[str, Any]] = []

    def rel(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()

    for path in _iter_project_files(root):
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(lines, start=1):
            if lowered not in line.lower():
                continue
            results.append(
                {
                    "path": rel(path),
                    "line": line_no,
                    "preview": line.strip()[:240],
                }
            )
            if len(results) >= max_results:
                return {
                    "ok": True,
                    "query": needle,
                    "results": results,
                    "truncated": True,
                }

    return {"ok": True, "query": needle, "results": results, "truncated": False}


def file_action(cwd: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Create, rename, delete, and duplicate project files or folders."""

    action = str(payload.get("action") or "").strip()
    root = Path(cwd).resolve()
    path = str(payload.get("path") or "").strip()
    if not action:
        return {"ok": False, "error": "missing action"}
    if not path and action not in {"new_file", "new_dir"}:
        return {"ok": False, "error": "missing path"}

    try:
        target = _resolve_user_path(path or ".", str(root))
        if action in {"new_file", "new_dir"}:
            target = _resolve_user_path(str(payload.get("path") or ""), str(root))
            if target.exists():
                return {"ok": False, "error": "target already exists"}
            if action == "new_file":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(payload.get("content") or ""), encoding="utf-8")
            else:
                target.mkdir(parents=True, exist_ok=False)
            return {"ok": True, "path": _project_rel(target, root)}

        if target == root:
            return {"ok": False, "error": "cannot modify project root"}
        if not target.exists():
            return {"ok": False, "error": "path does not exist"}

        if action == "delete":
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return {"ok": True, "path": _project_rel(target, root)}

        if action == "rename":
            new_path = str(payload.get("newPath") or "").strip()
            if not new_path:
                return {"ok": False, "error": "missing new path"}
            destination = _resolve_user_path(new_path, str(root))
            if destination.exists():
                return {"ok": False, "error": "target already exists"}
            destination.parent.mkdir(parents=True, exist_ok=True)
            target.rename(destination)
            return {"ok": True, "path": _project_rel(destination, root)}

        if action == "duplicate":
            new_path = str(payload.get("newPath") or "").strip()
            destination = (
                _resolve_user_path(new_path, str(root))
                if new_path
                else _duplicate_path(target)
            )
            if destination.exists():
                return {"ok": False, "error": "target already exists"}
            if target.is_dir():
                shutil.copytree(target, destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, destination)
            return {"ok": True, "path": _project_rel(destination, root)}
    except OSError as e:
        return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"unknown action: {action}"}


def _skip_project_path(path: Path) -> bool:
    return path.name in SKIPPED_PROJECT_NAMES or path.name.endswith(".egg-info")


def _project_rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _duplicate_path(path: Path) -> Path:
    stem = path.name if path.is_dir() else path.stem
    suffix = "" if path.is_dir() else path.suffix
    parent = path.parent
    candidate = parent / f"{stem} copy{suffix}"
    index = 2
    while candidate.exists():
        candidate = parent / f"{stem} copy {index}{suffix}"
        index += 1
    return candidate


def _iter_project_files(root: Path) -> list[Path]:
    pending = [root]
    files: list[Path] = []
    while pending:
        current = pending.pop()
        try:
            entries = [p for p in current.iterdir() if not _skip_project_path(p)]
        except OSError:
            continue
        entries.sort(key=lambda p: (not p.is_dir(), p.name.lower()), reverse=True)
        for entry in entries:
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                files.append(entry)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def load_snippets(cwd: str) -> dict[str, Any]:
    """Load built-in snippets plus project-custom snippets."""

    custom_path = _snippets_path(cwd)
    custom: list[dict[str, str]] = []
    if custom_path.exists():
        try:
            raw = json.loads(custom_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                custom = [_normalize_snippet(item, custom=True) for item in raw]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            custom = []
    return {
        "ok": True,
        "path": str(custom_path),
        "snippets": [*_builtin_snippets(), *custom],
    }


def save_custom_snippets(cwd: str, snippets: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist project-custom snippets under .cobra4/idle_snippets.json."""

    custom = [_normalize_snippet(item, custom=True) for item in snippets]
    target = _snippets_path(cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(custom, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "path": str(target),
        "snippets": [*_builtin_snippets(), *custom],
    }


def run_terminal_command(
    command: str,
    *,
    cwd: str,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run a non-interactive shell command in the project root."""

    command = command.strip()
    if not command:
        return {"ok": False, "returncode": 2, "stdout": "", "stderr": "empty command"}
    env = os.environ.copy()
    project_root = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=Path(cwd).resolve(),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": e.stdout or "",
            "stderr": f"Timed out after {timeout:g}s",
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


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


def build_symbols(module: N.Module) -> list[dict[str, Any]]:
    """Return a compact outline for top-level declarations."""

    symbols: list[dict[str, Any]] = []
    for stmt in module.body:
        line = stmt.loc.line if stmt.loc else 0
        if isinstance(stmt, N.FnDecl):
            symbols.append(
                {
                    "name": stmt.name,
                    "kind": "function",
                    "line": line,
                    "detail": f"fn {stmt.name}",
                }
            )
        elif isinstance(stmt, N.ClassDecl):
            children = [
                {
                    "name": inner.name,
                    "kind": "method",
                    "line": inner.loc.line if inner.loc else 0,
                    "detail": f"fn {inner.name}",
                }
                for inner in stmt.body
                if isinstance(inner, N.FnDecl)
            ]
            symbols.append(
                {
                    "name": stmt.name,
                    "kind": "class",
                    "line": line,
                    "detail": f"class {stmt.name}",
                    "children": children,
                }
            )
        elif isinstance(stmt, N.DataClassDecl):
            symbols.append(
                {
                    "name": stmt.name,
                    "kind": "data",
                    "line": line,
                    "detail": f"data class {stmt.name}",
                }
            )
        elif isinstance(stmt, N.DataSumDecl):
            symbols.append(
                {
                    "name": stmt.name,
                    "kind": "data",
                    "line": line,
                    "detail": f"data {stmt.name}",
                    "children": [
                        {
                            "name": variant.name,
                            "kind": "variant",
                            "line": variant.loc.line if variant.loc else line,
                            "detail": "variant",
                        }
                        for variant in stmt.variants
                    ],
                }
            )
        elif isinstance(stmt, N.WorkflowDecl):
            symbols.append(
                {
                    "name": stmt.name,
                    "kind": "workflow",
                    "line": line,
                    "detail": f"workflow {stmt.name}",
                }
            )
        elif isinstance(stmt, N.ResourceDecl):
            symbols.append(
                {
                    "name": stmt.name,
                    "kind": "resource",
                    "line": line,
                    "detail": stmt.adapter_path,
                }
            )
    return symbols


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


def _source_map_payload(source_map: Any) -> dict[str, Any]:
    mappings: list[dict[str, int]] = []
    for py_line in sorted(getattr(source_map, "lines", {})):
        segments = source_map.lines.get(py_line) or []
        if not segments:
            continue
        c4_line = segments[0].c4_line
        if c4_line:
            mappings.append({"python": py_line, "cobra4": c4_line})
    return {"path": getattr(source_map, "cobra4_path", ""), "mappings": mappings}


def _dedupe_completion_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        label = item.get("label")
        if not label or label in seen:
            continue
        seen.add(label)
        out.append(
            {
                "label": label,
                "kind": item.get("kind", 1),
                "detail": item.get("detail") or _completion_kind_name(item.get("kind")),
                "insertText": item.get("insertText") or label,
            }
        )
    return sorted(out, key=lambda item: item["label"].lower())


def _completion_kind_name(kind: Any) -> str:
    names = {
        2: "method",
        3: "function",
        5: "field",
        6: "variable",
        7: "class",
        14: "keyword",
    }
    return names.get(kind, "symbol")


def _leading_lang_directives(source: str) -> list[str]:
    directives: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        if stripped.startswith("lang use "):
            directives.append(stripped)
            continue
        break
    return directives


def _builtin_snippets() -> list[dict[str, Any]]:
    return [{**snippet, "custom": False} for snippet in DEFAULT_SNIPPETS]


def _snippets_path(cwd: str) -> Path:
    return Path(cwd).resolve() / "cobra4.snippets.json"


def _normalize_snippet(item: dict[str, Any], *, custom: bool) -> dict[str, Any]:
    title = str(item.get("title") or "Custom snippet").strip()
    code = str(item.get("code") or "").rstrip() + "\n"
    if not code.strip():
        raise ValueError("snippet code cannot be empty")
    snippet_id = str(item.get("id") or _slug(title)).strip()
    return {
        "id": snippet_id or "custom-snippet",
        "title": title,
        "category": str(item.get("category") or "Custom").strip() or "Custom",
        "description": str(item.get("description") or "").strip(),
        "code": code,
        "custom": custom,
    }


def _slug(value: str) -> str:
    chars = []
    prev_dash = False
    for ch in value.lower():
        if ch.isalnum():
            chars.append(ch)
            prev_dash = False
        elif not prev_dash:
            chars.append("-")
            prev_dash = True
    return "".join(chars).strip("-")


def _error_payload(error: ParseError | ValueError) -> dict[str, Any]:
    if isinstance(error, ParseError):
        return {
            "severity": "error",
            "message": str(error),
            "line": error.line,
            "column": error.column,
        }
    return {"severity": "error", "message": str(error), "line": None, "column": None}


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
        if parsed.path == "/api/tree":
            self._send_json(project_tree(self.idle_server.cwd))
            return
        if parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            self._send_json(search_project(self.idle_server.cwd, query))
            return
        if parsed.path == "/api/snippets":
            self._send_json(load_snippets(self.idle_server.cwd))
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
        if parsed.path == "/api/complete":
            payload = self._read_json()
            self._send_json(
                complete_source(
                    payload.get("source", ""),
                    int(payload.get("line") or 0),
                    int(payload.get("column") or 0),
                )
            )
            return
        if parsed.path == "/api/signature":
            payload = self._read_json()
            self._send_json(
                signature_source(
                    payload.get("source", ""),
                    int(payload.get("line") or 0),
                    int(payload.get("column") or 0),
                )
            )
            return
        if parsed.path == "/api/hover":
            payload = self._read_json()
            self._send_json(
                hover_source(
                    payload.get("source", ""),
                    int(payload.get("line") or 0),
                    int(payload.get("column") or 0),
                )
            )
            return
        if parsed.path == "/api/format":
            payload = self._read_json()
            self._send_json(
                format_source(
                    payload.get("source", ""),
                    source_path=payload.get("path") or "idle_scratch.c4",
                )
            )
            return
        if parsed.path == "/api/save":
            payload = self._read_json()
            self._handle_save(payload)
            return
        if parsed.path == "/api/file-action":
            payload = self._read_json()
            result = file_action(self.idle_server.cwd, payload)
            self._send_json(result, status=200 if result.get("ok") else 400)
            return
        if parsed.path == "/api/snippets":
            payload = self._read_json()
            try:
                self._send_json(
                    save_custom_snippets(
                        self.idle_server.cwd,
                        list(payload.get("snippets") or []),
                    )
                )
            except (OSError, ValueError, TypeError) as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        if parsed.path == "/api/terminal":
            payload = self._read_json()
            self._send_json(
                run_terminal_command(
                    payload.get("command", ""),
                    cwd=self.idle_server.cwd,
                    timeout=float(payload.get("timeout") or 120),
                )
            )
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
    port: int = 0,
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
    print(f"Cobra4 Studio running at {url}")
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
    parser = argparse.ArgumentParser(prog="c4 studio", description="Cobra4 Studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
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
<title>Cobra4 Studio</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f7f8fb;
  --surface: #ffffff;
  --surface-2: #eef3f6;
  --panel-bg: #f8fafb;
  --editor-bg: #fbfcfd;
  --editor-ink: #101820;
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
  --hover-bg: #e9f2f1;
  --completion-active: #d9edeb;
  --signature-bg: #fffdf7;
  --signature-line: #d3b675;
  --signature-ink: #3b2b00;
  --signature-strong: #003737;
  --signature-code-bg: rgba(0, 55, 55, .08);
  --graph-bg: #f9fbfc;
  --terminal-bg: #0e1116;
  --terminal-panel: #151a21;
  --terminal-line: #2a333d;
  --terminal-input-line: #34424c;
  --terminal-ink: #e7eef2;
  --file-icon-bg: #f8fbfd;
  --file-icon-line: #9fb0b8;
  --file-icon-c4: #00796b;
  --file-icon-py: #3a67a3;
  --file-icon-md: #8a4b00;
  --file-icon-json: #7b5aa6;
  --file-icon-config: #607d8b;
  --sidebar-width: 286px;
  --editor-width: 50%;
  --editor-font-size: 14px;
  --source-line-bg: rgba(0, 106, 106, .12);
  --source-line-border: rgba(0, 106, 106, .45);
  --syntax-keyword: #005f73;
  --syntax-string: #8a4b00;
  --syntax-comment: #6b7b85;
  --syntax-number: #7b5aa6;
  --syntax-builtin: #00796b;
  font-family: Inter, Roboto, "Segoe UI", system-ui, sans-serif;
}}
body[data-theme="dark"] {{
  color-scheme: dark;
  --bg: #111417;
  --surface: #181d21;
  --surface-2: #22292f;
  --panel-bg: #1d2328;
  --editor-bg: #101418;
  --editor-ink: #e7eef2;
  --ink: #e7eef2;
  --muted: #a9b7bf;
  --line: #303940;
  --primary: #4db6ac;
  --primary-ink: #062220;
  --accent: #e1a95f;
  --error: #ffb4ab;
  --ok: #86d39d;
  --code-bg: #0b0f12;
  --code-ink: #e7eef2;
  --hover-bg: #203533;
  --completion-active: #254743;
  --signature-bg: #282319;
  --signature-line: #8f7240;
  --signature-ink: #f6dfb0;
  --signature-strong: #9ee0d9;
  --signature-code-bg: rgba(158, 224, 217, .12);
  --graph-bg: #14191d;
  --terminal-bg: #080c0f;
  --terminal-panel: #11181d;
  --terminal-line: #28323a;
  --terminal-input-line: #3a4852;
  --terminal-ink: #e7eef2;
  --file-icon-bg: #20282e;
  --file-icon-line: #6f818b;
  --file-icon-c4: #4db6ac;
  --file-icon-py: #7fa7d6;
  --file-icon-md: #e1a95f;
  --file-icon-json: #b79ad8;
  --file-icon-config: #9fb0b8;
  --source-line-bg: rgba(77, 182, 172, .14);
  --source-line-border: rgba(77, 182, 172, .5);
  --syntax-keyword: #75d3cb;
  --syntax-string: #e1a95f;
  --syntax-comment: #81919a;
  --syntax-number: #b79ad8;
  --syntax-builtin: #86d39d;
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
  grid-template-rows: auto minmax(0, 1fr);
  height: 100vh;
  min-height: 0;
}}
.bar {{
  display: grid;
  grid-template-columns: auto minmax(180px, 1fr) auto;
  align-items: center;
  gap: 16px;
  min-height: 64px;
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
  justify-content: flex-end;
  flex-wrap: wrap;
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
.workspace {{
  display: grid;
  grid-template-columns: var(--sidebar-width) 6px minmax(0, 1fr);
  min-height: 0;
  overflow: hidden;
}}
.sidebar {{
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: minmax(160px, .92fr) minmax(230px, 1.08fr);
  background: var(--surface);
  border-right: 1px solid var(--line);
  overflow: hidden;
}}
.sidePanel {{
  min-height: 0;
  display: grid;
  grid-template-rows: 40px auto minmax(0, 1fr);
  border-bottom: 1px solid var(--line);
}}
.projectPanel {{
  grid-template-rows: 40px auto auto auto minmax(0, 1fr);
}}
.sideHead {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 0 10px;
  border-bottom: 1px solid var(--line);
}}
.sideHead strong {{
  font-size: 13px;
}}
.miniBtn {{
  height: 28px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--surface-2);
  color: var(--ink);
  padding: 0 8px;
  font-size: 12px;
  cursor: pointer;
}}
.miniBtn:hover {{ border-color: #9fb3bd; }}
.btn:disabled, .miniBtn:disabled, .iconBtn:disabled {{
  opacity: .48;
  cursor: not-allowed;
}}
.fileToolbar {{
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 5px;
  padding: 7px 8px;
  border-bottom: 1px solid var(--line);
}}
.fileToolbar .miniBtn {{
  min-width: 0;
  padding: 0 4px;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.projectRoot {{
  padding: 8px 10px;
  color: var(--muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border-bottom: 1px solid var(--line);
}}
.projectSearch {{
  display: grid;
  gap: 6px;
  padding: 8px;
  border-bottom: 1px solid var(--line);
}}
.projectSearch input {{
  width: 100%;
  height: 32px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--editor-bg);
  color: var(--ink);
  padding: 0 9px;
  font-size: 12px;
}}
.searchResults {{
  display: none;
  max-height: 180px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--surface);
}}
.searchResults.active {{
  display: block;
}}
.searchItem {{
  display: grid;
  gap: 2px;
  padding: 7px 8px;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
  font-size: 12px;
}}
.searchItem:last-child {{
  border-bottom: 0;
}}
.searchItem:hover {{
  background: var(--hover-bg);
}}
.searchItem strong {{
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}}
.searchItem span {{
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
}}
.fileTree, .snippetList {{
  min-height: 0;
  overflow: auto;
  padding: 6px;
}}
.treeItem {{
  display: grid;
  grid-template-columns: 16px 18px minmax(0, 1fr);
  gap: 7px;
  align-items: center;
  min-height: 28px;
  padding: 4px 6px;
  border-radius: 5px;
  cursor: pointer;
  font-size: 12px;
}}
.treeItem:hover, .snippetItem:hover, .snippetItem.active {{
  background: var(--hover-bg);
}}
.treeItem.selected {{
  background: var(--completion-active);
}}
.treeName {{
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.treeToggle {{
  position: relative;
  width: 16px;
  height: 20px;
  border: 0;
  padding: 0;
  background: transparent;
  cursor: pointer;
}}
.treeToggle::before {{
  content: "";
  position: absolute;
  left: 4px;
  top: 6px;
  width: 6px;
  height: 6px;
  border-right: 1.5px solid var(--muted);
  border-bottom: 1.5px solid var(--muted);
  transform: rotate(-45deg);
}}
.treeItem.open .treeToggle::before {{
  transform: rotate(45deg);
  top: 4px;
}}
.treeToggle.empty {{
  cursor: default;
}}
.treeToggle.empty::before {{
  display: none;
}}
.fileIcon {{
  position: relative;
  display: inline-block;
  width: 15px;
  height: 16px;
}}
.fileIcon.dir {{
  width: 16px;
  height: 12px;
  border: 1px solid #b7892d;
  border-radius: 2px;
  background: #d9a441;
}}
.fileIcon.dir::before {{
  content: "";
  position: absolute;
  left: 1px;
  top: -4px;
  width: 8px;
  height: 5px;
  border: 1px solid #b7892d;
  border-bottom: 0;
  border-radius: 2px 2px 0 0;
  background: #e4b653;
}}
.fileIcon.file, .fileIcon.c4, .fileIcon.py, .fileIcon.md, .fileIcon.json, .fileIcon.config {{
  border: 1px solid var(--file-icon-line);
  border-radius: 2px;
  background: var(--file-icon-bg);
}}
.fileIcon.file::after, .fileIcon.c4::after, .fileIcon.py::after, .fileIcon.md::after, .fileIcon.json::after, .fileIcon.config::after {{
  content: "";
  position: absolute;
  right: -1px;
  top: -1px;
  width: 5px;
  height: 5px;
  border-left: 1px solid var(--file-icon-line);
  border-bottom: 1px solid var(--file-icon-line);
  background: var(--surface);
}}
.fileIcon.c4::before, .fileIcon.py::before, .fileIcon.md::before, .fileIcon.json::before, .fileIcon.config::before {{
  position: absolute;
  left: 2px;
  bottom: 1px;
  font-size: 6px;
  font-weight: 700;
  line-height: 1;
}}
.fileIcon.c4 {{ border-color: var(--file-icon-c4); }}
.fileIcon.c4::before {{ content: "C4"; color: var(--file-icon-c4); }}
.fileIcon.py {{ border-color: var(--file-icon-py); }}
.fileIcon.py::before {{ content: "PY"; color: var(--file-icon-py); }}
.fileIcon.md {{ border-color: var(--file-icon-md); }}
.fileIcon.md::before {{ content: "MD"; color: var(--file-icon-md); }}
.fileIcon.json {{ border-color: var(--file-icon-json); }}
.fileIcon.json::before {{ content: "{{}}"; color: var(--file-icon-json); }}
.fileIcon.config {{ border-color: var(--file-icon-config); }}
.fileIcon.config::before {{ content: "*"; color: var(--file-icon-config); }}
.snippetPanel {{
  grid-template-rows: 40px minmax(140px, .95fr) minmax(180px, 1fr);
  overflow: hidden;
}}
.snippetItem {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) 30px;
  gap: 8px;
  align-items: center;
  min-height: 58px;
  padding: 7px 6px 7px 9px;
  border-radius: 5px;
  cursor: pointer;
  font-size: 12px;
}}
.snippetSummary {{
  min-width: 0;
  display: grid;
  gap: 2px;
}}
.snippetSummary strong {{
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}}
.snippetSummary small {{
  color: var(--primary);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0;
  text-transform: uppercase;
}}
.snippetSummary span {{
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
}}
.iconBtn {{
  display: inline-grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--surface-2);
  color: var(--ink);
  cursor: pointer;
}}
.iconBtn:hover {{
  border-color: #9fb3bd;
}}
.iconEye {{
  position: relative;
  width: 16px;
  height: 10px;
  border: 1.5px solid currentColor;
  border-radius: 50%;
}}
.iconEye::after {{
  content: "";
  position: absolute;
  left: 50%;
  top: 50%;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentColor;
  transform: translate(-50%, -50%);
}}
.snippetEditor {{
  min-height: 0;
  display: grid;
  grid-template-rows: auto auto auto 1fr auto;
  gap: 7px;
  padding: 8px;
  border-top: 1px solid var(--line);
  overflow: auto;
}}
.snippetEditor input, .snippetEditor textarea {{
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--editor-bg);
  color: var(--ink);
  padding: 7px 8px;
  font-size: 12px;
}}
.snippetEditor textarea {{
  min-height: 58px;
  max-height: 180px;
  overflow: auto;
  resize: vertical;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  line-height: 1.35;
}}
.snippetActions {{
  display: grid;
  grid-template-columns: 1fr 1fr auto auto;
  gap: 6px;
}}
.shell {{
  display: grid;
  grid-template-columns: minmax(280px, var(--editor-width)) 6px minmax(280px, 1fr);
  min-height: 0;
  overflow: hidden;
}}
.resizer {{
  min-width: 6px;
  min-height: 6px;
  background: var(--surface);
  cursor: col-resize;
  border-left: 1px solid var(--line);
  border-right: 1px solid var(--line);
}}
.resizer:hover, .resizer.dragging {{
  background: var(--hover-bg);
}}
.editorPane, .resultPane {{
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: 44px minmax(0, 1fr);
  overflow: hidden;
}}
.editorPane {{ border-right: 1px solid var(--line); position: relative; }}
.editorPane {{ border-right: 0; }}
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
.sourceWrap {{
  position: relative;
  min-height: 0;
  overflow: hidden;
  background: var(--editor-bg);
}}
#sourceHighlight {{
  position: absolute;
  inset: 0;
  z-index: 0;
  margin: 0;
  padding: 18px;
  overflow: hidden;
  background: transparent;
  color: var(--editor-ink);
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: var(--editor-font-size);
  line-height: 1.55;
  tab-size: var(--editor-tab-size, 4);
  white-space: pre;
  pointer-events: none;
}}
.sourceLine {{
  display: block;
  min-height: 1.55em;
  border-left: 3px solid transparent;
  padding-left: 0;
}}
.sourceLine.mappedLine {{
  background: var(--source-line-bg);
  border-left-color: var(--source-line-border);
}}
.tok-keyword {{ color: var(--syntax-keyword); font-weight: 650; }}
.tok-string {{ color: var(--syntax-string); }}
.tok-comment {{ color: var(--syntax-comment); font-style: italic; }}
.tok-number {{ color: var(--syntax-number); }}
.tok-builtin {{ color: var(--syntax-builtin); font-weight: 650; }}
#source {{
  position: relative;
  z-index: 1;
  width: 100%;
  height: 100%;
  min-height: 0;
  resize: none;
  border: 0;
  outline: 0;
  overflow: auto;
  padding: 18px;
  background: transparent;
  color: transparent;
  caret-color: var(--editor-ink);
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: var(--editor-font-size);
  line-height: 1.55;
  tab-size: var(--editor-tab-size, 4);
}}
#source::selection {{
  background: rgba(0, 106, 106, .24);
  color: transparent;
}}
#source.hasErrors {{ box-shadow: inset 3px 0 0 var(--error); }}
.completionBox {{
  position: absolute;
  z-index: 20;
  display: none;
  width: min(340px, calc(100% - 36px));
  max-height: 260px;
  overflow: auto;
  background: var(--surface);
  border: 1px solid #b8c8d0;
  border-radius: 6px;
  box-shadow: 0 12px 32px rgba(16, 32, 39, .18);
}}
.completionItem {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  padding: 8px 10px;
  cursor: pointer;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
}}
.completionItem.active {{
  background: var(--completion-active);
}}
.completionItem span:first-child {{
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.completionItem span:last-child {{
  color: var(--muted);
  font-family: Inter, Roboto, "Segoe UI", system-ui, sans-serif;
  font-size: 11px;
}}
.signatureBox {{
  position: absolute;
  left: 18px;
  right: 18px;
  bottom: 14px;
  z-index: 15;
  display: none;
  padding: 10px 12px;
  background: var(--signature-bg);
  border: 1px solid var(--signature-line);
  border-radius: 6px;
  box-shadow: 0 8px 22px rgba(16, 32, 39, .12);
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  color: var(--signature-ink);
}}
.signatureBox strong {{ color: var(--signature-strong); }}
.signatureBox code {{
  background: var(--signature-code-bg);
  border-radius: 4px;
  padding: 1px 4px;
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
.view.active {{ display: grid; }}
pre {{
  margin: 0;
  width: 100%;
  height: 100%;
  min-height: 0;
  padding: 18px;
  overflow: auto;
  background: var(--code-bg);
  color: var(--code-ink);
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
  line-height: 1.55;
}}
.pyLine {{
  display: block;
  min-height: 1.55em;
  border-left: 3px solid transparent;
  padding-left: 8px;
  cursor: default;
}}
.pyLine[data-c4-line] {{
  cursor: pointer;
}}
.pyLine.mappedLine {{
  background: var(--source-line-bg);
  border-left-color: var(--source-line-border);
}}
#output {{
  background: var(--terminal-bg);
}}
#problems {{
  padding: 14px;
}}
.problem {{
  border-left: 4px solid var(--line);
  padding: 10px 12px;
  margin-bottom: 10px;
  background: var(--panel-bg);
  border-radius: 6px;
  white-space: pre-wrap;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
}}
.problem.error {{ border-left-color: var(--error); }}
.problem.warning {{ border-left-color: var(--accent); }}
.problem[data-line] {{ cursor: pointer; }}
.symbolList {{
  padding: 14px;
}}
.symbol {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  padding: 9px 10px;
  margin-bottom: 6px;
  background: var(--panel-bg);
  border: 1px solid var(--line);
  border-radius: 6px;
  cursor: pointer;
}}
.symbol.child {{ margin-left: 18px; }}
.symbol strong {{
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.symbol span {{
  color: var(--muted);
  font-size: 11px;
}}
.terminalWrap {{
  min-height: 100%;
  display: grid;
  grid-template-rows: 1fr auto;
  background: var(--terminal-bg);
}}
#terminalOutput {{
  min-height: 0;
}}
.terminalForm {{
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  padding: 9px;
  background: var(--terminal-panel);
  border-top: 1px solid var(--terminal-line);
  color: var(--terminal-ink);
}}
.terminalForm input {{
  min-width: 0;
  height: 34px;
  border: 1px solid var(--terminal-input-line);
  border-radius: 5px;
  background: var(--terminal-bg);
  color: var(--terminal-ink);
  padding: 0 10px;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
}}
.graphWrap {{
  height: 100%;
  min-height: 420px;
  background: var(--graph-bg);
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
.modal[hidden] {{
  display: none;
}}
.modal {{
  position: fixed;
  inset: 0;
  z-index: 80;
  display: grid;
  place-items: center;
  padding: 24px;
}}
.modalBackdrop {{
  position: absolute;
  inset: 0;
  background: rgba(9, 15, 18, .42);
  backdrop-filter: blur(10px);
}}
.modalDialog {{
  position: relative;
  z-index: 1;
  width: min(880px, 100%);
  max-height: min(760px, calc(100vh - 48px));
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  overflow: hidden;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 24px 80px rgba(8, 17, 22, .28);
}}
.modalHead, .modalActions {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
}}
.modalHead h2 {{
  margin: 0;
  font-size: 15px;
}}
.modalHead span {{
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-size: 12px;
}}
.modalBody {{
  min-height: 0;
  overflow: auto;
  display: grid;
  grid-template-columns: minmax(180px, .42fr) minmax(280px, 1fr);
  gap: 12px;
  padding: 14px;
}}
.modalFields {{
  min-height: 0;
  display: grid;
  gap: 8px;
  align-content: start;
}}
.modalFields input, .modalFields textarea {{
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--editor-bg);
  color: var(--ink);
  padding: 8px 9px;
  font-size: 13px;
}}
.modalFields textarea {{
  min-height: 160px;
  resize: vertical;
  overflow: auto;
  line-height: 1.4;
}}
.modalCode {{
  min-height: 0;
  display: grid;
  grid-template-rows: minmax(320px, 1fr);
}}
.modalCode textarea {{
  width: 100%;
  min-height: 0;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--code-bg);
  color: var(--code-ink);
  padding: 12px;
  resize: none;
  overflow: auto;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
  line-height: 1.5;
  tab-size: 4;
}}
.modalActions {{
  justify-content: flex-end;
  border-top: 1px solid var(--line);
  border-bottom: 0;
}}
.commandDialog {{
  width: min(680px, 100%);
  grid-template-rows: auto auto minmax(0, 1fr);
}}
.commandSearch {{
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
}}
.commandSearch input {{
  width: 100%;
  height: 42px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--editor-bg);
  color: var(--ink);
  padding: 0 12px;
  font-size: 14px;
}}
.commandList {{
  min-height: 0;
  max-height: min(520px, 62vh);
  overflow: auto;
  padding: 8px;
}}
.commandItem {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  min-height: 46px;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
}}
.commandItem:hover, .commandItem.active {{
  background: var(--hover-bg);
}}
.commandItem strong {{
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}}
.commandItem span {{
  color: var(--muted);
  font-size: 12px;
}}
.settingsDialog {{
  width: min(620px, 100%);
}}
.settingsBody {{
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  padding: 14px;
  overflow: auto;
}}
.settingField {{
  display: grid;
  gap: 5px;
}}
.settingField label {{
  color: var(--muted);
  font-size: 12px;
}}
.settingField input, .settingField select {{
  width: 100%;
  height: 36px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--editor-bg);
  color: var(--ink);
  padding: 0 9px;
}}
.settingCheck {{
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 36px;
  color: var(--ink);
  font-size: 13px;
}}
@media (max-width: 860px) {{
  body {{ overflow: auto; }}
  .app {{ min-height: 100vh; height: auto; }}
  .bar {{
    height: auto;
    grid-template-columns: 1fr;
    align-items: stretch;
    padding: 10px;
  }}
  .pathbar {{ grid-template-columns: 1fr; }}
  .actions {{ flex-wrap: wrap; }}
  .workspace {{ grid-template-columns: 1fr; grid-template-rows: minmax(320px, 40vh) minmax(620px, 1fr); }}
  .workspace > .resizer, .shell > .resizer {{ display: none; }}
  .sidebar {{ grid-template-rows: minmax(130px, 1fr) minmax(170px, 1fr); border-right: 0; border-bottom: 1px solid var(--line); }}
  .shell {{ grid-template-columns: 1fr; grid-template-rows: 50vh 50vh; }}
  .editorPane {{ border-right: 0; border-bottom: 1px solid var(--line); }}
  .modal {{ padding: 12px; }}
  .modalDialog {{ max-height: calc(100vh - 24px); }}
  .modalBody {{ grid-template-columns: 1fr; }}
  .modalCode {{ grid-template-rows: minmax(260px, 1fr); }}
  .settingsBody {{ grid-template-columns: 1fr; }}
}}
@media (max-height: 740px) and (min-width: 861px) {{
  .bar {{ min-height: 56px; }}
  .brand img {{ width: 32px; height: 32px; }}
  .sidebar {{ grid-template-rows: minmax(130px, .85fr) minmax(190px, 1.15fr); }}
  .snippetItem {{ min-height: 50px; }}
  .snippetEditor textarea {{ min-height: 44px; }}
}}
</style>
</head>
<body>
<div class="app">
  <header class="bar">
    <div class="brand">
      <img src="/assets/logo-128.png" alt="cobra4 logo">
      <div><strong>Cobra4 Studio</strong><span>v{__version__}</span></div>
    </div>
    <div class="pathbar">
      <input id="path" value="idle_scratch.c4" aria-label="file path">
      <button class="btn" id="openBtn">Open</button>
      <button class="btn" id="saveBtn">Save</button>
    </div>
    <div class="actions">
      <button class="btn tonal" id="checkBtn">Check</button>
      <button class="btn" id="formatBtn">Format</button>
      <button class="btn primary" id="runBtn">Run</button>
      <button class="btn" id="newBtn">New</button>
      <button class="btn" id="commandBtn">Commands</button>
      <button class="btn" id="settingsBtn">Settings</button>
      <button class="btn" id="themeBtn">Theme</button>
    </div>
  </header>
  <main class="workspace">
    <aside class="sidebar">
      <section class="sidePanel projectPanel">
        <div class="sideHead">
          <strong>Progetto</strong>
          <button class="miniBtn" id="refreshTreeBtn">Refresh</button>
        </div>
        <div id="projectRoot" class="projectRoot"></div>
        <div class="fileToolbar">
          <button class="miniBtn" id="newFileBtn" title="New file">File</button>
          <button class="miniBtn" id="newDirBtn" title="New folder">Dir</button>
          <button class="miniBtn" id="renameFileBtn" title="Rename selected">Rename</button>
          <button class="miniBtn" id="duplicateFileBtn" title="Duplicate selected">Copy</button>
          <button class="miniBtn" id="deleteFileBtn" title="Delete selected">Del</button>
        </div>
        <div class="projectSearch">
          <input id="projectSearchInput" autocomplete="off" placeholder="Search project">
          <div id="projectSearchResults" class="searchResults"></div>
        </div>
        <div id="fileTree" class="fileTree"></div>
      </section>
      <section class="sidePanel snippetPanel">
        <div class="sideHead">
          <strong>Snippet</strong>
          <button class="miniBtn" id="newSnippetBtn">New</button>
        </div>
        <div id="snippetList" class="snippetList"></div>
        <div class="snippetEditor">
          <input id="snippetTitle" placeholder="Title">
          <input id="snippetCategory" placeholder="Category">
          <textarea id="snippetDescription" placeholder="Description"></textarea>
          <textarea id="snippetCode" placeholder="Cobra4 code"></textarea>
          <div class="snippetActions">
            <button class="miniBtn" id="insertSnippetBtn">Insert</button>
            <button class="miniBtn" id="saveSnippetBtn">Save</button>
            <button class="miniBtn" id="deleteSnippetBtn">Del</button>
            <button class="iconBtn" id="inspectSnippetBtn" title="Inspect" aria-label="Inspect snippet">
              <span class="fa fa-eye iconEye" aria-hidden="true"></span>
            </button>
          </div>
        </div>
      </section>
    </aside>
    <div class="resizer" id="sidebarResizer" aria-hidden="true"></div>
    <section class="shell">
      <section class="editorPane">
        <div class="paneHead">
          <h1>Source</h1>
          <div class="stats">
            <span id="c4Loc">C4 0</span>
            <span id="pyLoc">Python 0</span>
            <span id="savedLoc">Saved 0</span>
            <span id="lintStatus">OK</span>
            <span id="cursorPos">1:1</span>
          </div>
        </div>
        <div class="sourceWrap">
          <pre id="sourceHighlight" aria-hidden="true"></pre>
          <textarea id="source" spellcheck="false"></textarea>
        </div>
        <div id="completionBox" class="completionBox"></div>
        <div id="signatureBox" class="signatureBox"></div>
      </section>
      <div class="resizer" id="editorResizer" aria-hidden="true"></div>
      <section class="resultPane">
        <nav class="tabs" aria-label="result tabs">
          <button class="tab active" data-tab="outputView">Output</button>
          <button class="tab" data-tab="pythonView">Python</button>
          <button class="tab" data-tab="graphView">Grafica</button>
          <button class="tab" data-tab="symbolView">Simboli</button>
          <button class="tab" data-tab="problemView">Problemi</button>
          <button class="tab" data-tab="terminalView">Terminale</button>
        </nav>
        <section id="outputView" class="view active"><pre id="output"></pre></section>
        <section id="pythonView" class="view"><pre id="python"></pre></section>
        <section id="graphView" class="view"><div class="graphWrap" id="graph"></div></section>
        <section id="symbolView" class="view"><div id="symbols" class="symbolList"></div></section>
        <section id="problemView" class="view"><div id="problems"></div></section>
        <section id="terminalView" class="view">
          <div class="terminalWrap">
            <pre id="terminalOutput"></pre>
            <form id="terminalForm" class="terminalForm">
              <span>$</span>
              <input id="terminalInput" autocomplete="off" placeholder="git status">
              <button class="miniBtn" type="submit">Run</button>
            </form>
          </div>
        </section>
      </section>
    </section>
  </main>
</div>
<div class="modal" id="settingsModal" hidden>
  <div class="modalBackdrop" id="settingsModalBackdrop"></div>
  <section class="modalDialog settingsDialog" role="dialog" aria-modal="true" aria-labelledby="settingsTitle">
    <header class="modalHead">
      <div>
        <h2 id="settingsTitle">Settings</h2>
        <span>Make Studio fit the way you work</span>
      </div>
      <button class="iconBtn" id="closeSettingsBtn" title="Close" aria-label="Close settings">x</button>
    </header>
    <div class="settingsBody">
      <div class="settingField">
        <label for="settingTheme">Theme</label>
        <select id="settingTheme">
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </div>
      <div class="settingField">
        <label for="settingFontSize">Editor font size</label>
        <input id="settingFontSize" type="number" min="11" max="22" step="1">
      </div>
      <div class="settingField">
        <label for="settingTabSize">Tab size</label>
        <input id="settingTabSize" type="number" min="2" max="8" step="1">
      </div>
      <div class="settingField">
        <label for="settingTreeRefresh">Tree refresh seconds</label>
        <input id="settingTreeRefresh" type="number" min="2" max="60" step="1">
      </div>
      <div class="settingField">
        <label for="settingRunTimeout">Run timeout seconds</label>
        <input id="settingRunTimeout" type="number" min="1" max="300" step="1">
      </div>
      <div class="settingField">
        <label>Autosave</label>
        <label class="settingCheck"><input id="settingAutoSave" type="checkbox"> Save after edits</label>
      </div>
    </div>
    <footer class="modalActions">
      <button class="btn" id="resetSettingsBtn">Reset</button>
      <button class="btn primary" id="saveSettingsBtn">Save</button>
    </footer>
  </section>
</div>
<div class="modal" id="snippetModal" hidden>
  <div class="modalBackdrop" id="snippetModalBackdrop"></div>
  <section class="modalDialog" role="dialog" aria-modal="true" aria-labelledby="snippetModalTitle">
    <header class="modalHead">
      <div>
        <h2 id="snippetModalTitle">Snippet</h2>
        <span id="snippetModalMeta"></span>
      </div>
      <button class="iconBtn" id="closeSnippetModalBtn" title="Close" aria-label="Close snippet inspector">x</button>
    </header>
    <div class="modalBody">
      <div class="modalFields">
        <input id="modalSnippetTitle" placeholder="Title">
        <input id="modalSnippetCategory" placeholder="Category">
        <textarea id="modalSnippetDescription" placeholder="Description"></textarea>
      </div>
      <div class="modalCode">
        <textarea id="modalSnippetCode" spellcheck="false" placeholder="Cobra4 code"></textarea>
      </div>
    </div>
    <footer class="modalActions">
      <button class="btn" id="modalInsertSnippetBtn">Insert</button>
      <button class="btn primary" id="modalSaveSnippetBtn">Save</button>
      <button class="btn" id="modalDeleteSnippetBtn">Delete</button>
    </footer>
  </section>
</div>
<div class="modal" id="commandPalette" hidden>
  <div class="modalBackdrop" id="commandPaletteBackdrop"></div>
  <section class="modalDialog commandDialog" role="dialog" aria-modal="true" aria-labelledby="commandPaletteTitle">
    <header class="modalHead">
      <div>
        <h2 id="commandPaletteTitle">Commands</h2>
        <span>Run Studio actions without leaving the keyboard</span>
      </div>
      <button class="iconBtn" id="closeCommandPaletteBtn" title="Close" aria-label="Close command palette">x</button>
    </header>
    <div class="commandSearch">
      <input id="commandInput" autocomplete="off" placeholder="Type a command">
    </div>
    <div id="commandList" class="commandList"></div>
  </section>
</div>
<script>
const source = document.getElementById("source");
const sourceHighlight = document.getElementById("sourceHighlight");
const pathInput = document.getElementById("path");
const output = document.getElementById("output");
const python = document.getElementById("python");
const problems = document.getElementById("problems");
const symbols = document.getElementById("symbols");
const projectRoot = document.getElementById("projectRoot");
const fileTree = document.getElementById("fileTree");
const projectSearchInput = document.getElementById("projectSearchInput");
const projectSearchResults = document.getElementById("projectSearchResults");
const snippetList = document.getElementById("snippetList");
const snippetTitle = document.getElementById("snippetTitle");
const snippetCategory = document.getElementById("snippetCategory");
const snippetDescription = document.getElementById("snippetDescription");
const snippetCode = document.getElementById("snippetCode");
const snippetModal = document.getElementById("snippetModal");
const snippetModalTitle = document.getElementById("snippetModalTitle");
const snippetModalMeta = document.getElementById("snippetModalMeta");
const modalSnippetTitle = document.getElementById("modalSnippetTitle");
const modalSnippetCategory = document.getElementById("modalSnippetCategory");
const modalSnippetDescription = document.getElementById("modalSnippetDescription");
const modalSnippetCode = document.getElementById("modalSnippetCode");
const commandPalette = document.getElementById("commandPalette");
const commandInput = document.getElementById("commandInput");
const commandList = document.getElementById("commandList");
const settingsModal = document.getElementById("settingsModal");
const settingTheme = document.getElementById("settingTheme");
const settingFontSize = document.getElementById("settingFontSize");
const settingTabSize = document.getElementById("settingTabSize");
const settingTreeRefresh = document.getElementById("settingTreeRefresh");
const settingRunTimeout = document.getElementById("settingRunTimeout");
const settingAutoSave = document.getElementById("settingAutoSave");
const terminalOutput = document.getElementById("terminalOutput");
const terminalInput = document.getElementById("terminalInput");
const completionBox = document.getElementById("completionBox");
const signatureBox = document.getElementById("signatureBox");
const statusEls = {{
  c4: document.getElementById("c4Loc"),
  py: document.getElementById("pyLoc"),
  saved: document.getElementById("savedLoc"),
  lint: document.getElementById("lintStatus"),
  cursor: document.getElementById("cursorPos")
}};
let compileTimer = null;
let lastGraph = {{nodes: [], edges: []}};
let lastSymbols = [];
let completionState = {{items: [], selected: 0, prefix: ""}};
let allSnippets = [];
let selectedSnippetId = null;
let lastTreeSignature = "";
let treeRefreshTimer = null;
let openTreePaths = new Set(["."]);
let selectedTreePath = ".";
let treeNodeKinds = new Map();
let projectSearchTimer = null;
let commandState = {{items: [], selected: 0}};
let lastSourceMap = {{mappings: []}};
let lastPythonCode = "";
let activeC4Line = null;
let activePythonLines = new Set();
let autoSaveTimer = null;
let studioSettings = {{
  fontSize: 14,
  tabSize: 4,
  treeRefresh: 5,
  runTimeout: 10,
  autoSave: false
}};

async function api(path, payload) {{
  const response = await fetch(path, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify(payload)
  }});
  return response.json();
}}

async function getJson(path) {{
  const response = await fetch(path);
  return response.json();
}}

function applyTheme(theme) {{
  const chosen = theme === "dark" ? "dark" : "light";
  document.body.dataset.theme = chosen;
  localStorage.setItem("cobra4-idle-theme", chosen);
  document.getElementById("themeBtn").textContent = chosen === "dark" ? "Light" : "Dark";
}}

function initTheme() {{
  const saved = localStorage.getItem("cobra4-idle-theme");
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
}}

function toggleTheme() {{
  applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark");
}}

function loadStudioSettings() {{
  try {{
    const saved = JSON.parse(localStorage.getItem("cobra4-studio-settings") || "{{}}");
    studioSettings = {{
      ...studioSettings,
      ...saved,
      fontSize: Number(saved.fontSize || studioSettings.fontSize),
      tabSize: Number(saved.tabSize || studioSettings.tabSize),
      treeRefresh: Number(saved.treeRefresh || studioSettings.treeRefresh),
      runTimeout: Number(saved.runTimeout || studioSettings.runTimeout),
      autoSave: !!saved.autoSave
    }};
  }} catch {{
    studioSettings = {{fontSize: 14, tabSize: 4, treeRefresh: 5, runTimeout: 10, autoSave: false}};
  }}
  applyStudioSettings();
}}

function applyStudioSettings() {{
  document.documentElement.style.setProperty("--editor-font-size", `${{studioSettings.fontSize}}px`);
  document.documentElement.style.setProperty("--editor-tab-size", String(studioSettings.tabSize));
  source.style.tabSize = String(studioSettings.tabSize);
  sourceHighlight.style.tabSize = String(studioSettings.tabSize);
  startTreeAutoRefresh();
  renderSourceHighlight();
}}

function persistStudioSettings() {{
  localStorage.setItem("cobra4-studio-settings", JSON.stringify(studioSettings));
}}

function openSettingsModal() {{
  settingTheme.value = document.body.dataset.theme === "dark" ? "dark" : "light";
  settingFontSize.value = studioSettings.fontSize;
  settingTabSize.value = studioSettings.tabSize;
  settingTreeRefresh.value = studioSettings.treeRefresh;
  settingRunTimeout.value = studioSettings.runTimeout;
  settingAutoSave.checked = !!studioSettings.autoSave;
  settingsModal.hidden = false;
  settingFontSize.focus();
}}

function closeSettingsModal() {{
  settingsModal.hidden = true;
  source.focus();
}}

function saveSettingsFromForm() {{
  studioSettings = {{
    fontSize: clampNumber(settingFontSize.value, 11, 22, 14),
    tabSize: clampNumber(settingTabSize.value, 2, 8, 4),
    treeRefresh: clampNumber(settingTreeRefresh.value, 2, 60, 5),
    runTimeout: clampNumber(settingRunTimeout.value, 1, 300, 10),
    autoSave: settingAutoSave.checked
  }};
  applyTheme(settingTheme.value);
  applyStudioSettings();
  persistStudioSettings();
  closeSettingsModal();
}}

function resetSettings() {{
  studioSettings = {{fontSize: 14, tabSize: 4, treeRefresh: 5, runTimeout: 10, autoSave: false}};
  applyTheme("light");
  applyStudioSettings();
  persistStudioSettings();
  openSettingsModal();
}}

function clampNumber(value, min, max, fallback) {{
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}}

function loadLayoutState() {{
  try {{
    const saved = JSON.parse(localStorage.getItem("cobra4-studio-layout") || "{{}}");
    if (saved.sidebarWidth) document.documentElement.style.setProperty("--sidebar-width", `${{saved.sidebarWidth}}px`);
    if (saved.editorWidth) document.documentElement.style.setProperty("--editor-width", `${{saved.editorWidth}}px`);
  }} catch {{}}
}}

function saveLayoutState(values) {{
  let current = {{}};
  try {{
    current = JSON.parse(localStorage.getItem("cobra4-studio-layout") || "{{}}");
  }} catch {{}}
  localStorage.setItem("cobra4-studio-layout", JSON.stringify({{...current, ...values}}));
}}

function initResizers() {{
  initSidebarResizer();
  initEditorResizer();
}}

function initSidebarResizer() {{
  const handle = document.getElementById("sidebarResizer");
  handle.addEventListener("mousedown", event => {{
    event.preventDefault();
    handle.classList.add("dragging");
    const startX = event.clientX;
    const startWidth = document.querySelector(".sidebar").getBoundingClientRect().width;
    const onMove = moveEvent => {{
      const width = Math.round(clampNumber(startWidth + moveEvent.clientX - startX, 220, 560, startWidth));
      document.documentElement.style.setProperty("--sidebar-width", `${{width}}px`);
      saveLayoutState({{sidebarWidth: width}});
    }};
    const onUp = () => {{
      handle.classList.remove("dragging");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }};
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }});
}}

function initEditorResizer() {{
  const handle = document.getElementById("editorResizer");
  const shell = document.querySelector(".shell");
  handle.addEventListener("mousedown", event => {{
    event.preventDefault();
    handle.classList.add("dragging");
    const onMove = moveEvent => {{
      const rect = shell.getBoundingClientRect();
      const width = Math.round(clampNumber(moveEvent.clientX - rect.left, 260, rect.width - 260, rect.width / 2));
      document.documentElement.style.setProperty("--editor-width", `${{width}}px`);
      saveLayoutState({{editorWidth: width}});
    }};
    const onUp = () => {{
      handle.classList.remove("dragging");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }};
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }});
}}

function loadTreeState() {{
  try {{
    const saved = JSON.parse(localStorage.getItem("cobra4-idle-open-tree") || "[]");
    openTreePaths = new Set([".", ...saved.filter(Boolean)]);
  }} catch {{
    openTreePaths = new Set(["."]);
  }}
}}

function saveTreeState() {{
  localStorage.setItem("cobra4-idle-open-tree", JSON.stringify([...openTreePaths]));
}}

async function loadProjectTree(force=false) {{
  const result = await getJson("/api/tree");
  if (!result.ok) return;
  const signature = JSON.stringify(result.tree);
  if (!force && signature === lastTreeSignature) return;
  lastTreeSignature = signature;
  projectRoot.textContent = result.root || "";
  renderFileTree(result.tree);
}}

function renderFileTree(root) {{
  fileTree.innerHTML = "";
  treeNodeKinds = new Map();
  const addNode = (node, depth=0) => {{
    if (!node) return;
    const isDir = node.kind === "dir";
    const nodePath = node.path || ".";
    const isOpen = !isDir || openTreePaths.has(nodePath);
    treeNodeKinds.set(nodePath, node.kind || "file");
    const row = document.createElement("div");
    row.className = `treeItem ${{isDir ? "dirItem" : ""}} ${{isOpen ? "open" : ""}} ${{nodePath === selectedTreePath ? "selected" : ""}}`;
    row.style.paddingLeft = `${{6 + depth * 12}}px`;
    row.dataset.path = nodePath;
    row.innerHTML = `<button class="treeToggle ${{isDir ? "" : "empty"}}" aria-label="${{isOpen ? "Collapse" : "Expand"}}" aria-expanded="${{isOpen}}"></button><span class="fileIcon ${{fileIconClass(node)}}" aria-hidden="true"></span><span class="treeName">${{escapeHtml(node.name || "")}}</span>`;
    if (isDir) {{
      row.addEventListener("click", () => {{
        selectedTreePath = nodePath;
        if (openTreePaths.has(nodePath)) {{
          if (nodePath !== ".") openTreePaths.delete(nodePath);
        }} else {{
          openTreePaths.add(nodePath);
        }}
        saveTreeState();
        renderFileTree(root);
      }});
    }} else if (node.kind === "file") {{
      row.addEventListener("click", () => {{
        selectedTreePath = nodePath;
        pathInput.value = node.path;
        openPath();
        renderFileTree(root);
      }});
    }}
    fileTree.appendChild(row);
    if (isOpen) {{
      (node.children || []).forEach(child => addNode(child, depth + 1));
    }}
  }};
  addNode(root);
}}

function fileIconClass(node) {{
  if (node.kind === "dir") return "dir";
  if (node.kind !== "file") return "file";
  const name = String(node.name || "").toLowerCase();
  if (name.endsWith(".c4")) return "c4";
  if (name.endsWith(".py")) return "py";
  if (name.endsWith(".md")) return "md";
  if (name.endsWith(".json") || name.endsWith(".jsonl")) return "json";
  if (name.endsWith(".toml") || name.endsWith(".yml") || name.endsWith(".yaml") || name.startsWith(".")) return "config";
  return "file";
}}

function startTreeAutoRefresh() {{
  clearInterval(treeRefreshTimer);
  const delay = Math.max(2, Number(studioSettings.treeRefresh || 5)) * 1000;
  treeRefreshTimer = setInterval(() => loadProjectTree(false), delay);
}}

function scheduleProjectSearch() {{
  clearTimeout(projectSearchTimer);
  projectSearchTimer = setTimeout(runProjectSearch, 180);
}}

async function runProjectSearch() {{
  const query = projectSearchInput.value.trim();
  if (!query) {{
    projectSearchResults.classList.remove("active");
    projectSearchResults.innerHTML = "";
    return;
  }}
  const result = await getJson("/api/search?q=" + encodeURIComponent(query));
  renderProjectSearchResults(result);
}}

function renderProjectSearchResults(result) {{
  const items = result.results || [];
  projectSearchResults.innerHTML = "";
  projectSearchResults.classList.add("active");
  if (!items.length) {{
    const empty = document.createElement("div");
    empty.className = "searchItem";
    empty.innerHTML = "<strong>No results</strong><span></span>";
    projectSearchResults.appendChild(empty);
    return;
  }}
  for (const item of items) {{
    const row = document.createElement("div");
    row.className = "searchItem";
    row.innerHTML = `<strong>${{escapeHtml(item.path)}}:${{item.line || 1}}</strong><span>${{escapeHtml(item.preview || "")}}</span>`;
    row.addEventListener("click", async () => {{
      pathInput.value = item.path;
      const opened = await openPath();
      if (opened && opened.ok) goToLine(Number(item.line || 1), 1);
    }});
    projectSearchResults.appendChild(row);
  }}
}}

function selectedTreeBasePath() {{
  const path = selectedTreePath || ".";
  const kind = treeNodeKinds.get(path);
  if (kind === "file") return dirname(path);
  return path;
}}

function dirname(path) {{
  const value = String(path || ".");
  const idx = value.lastIndexOf("/");
  if (idx < 0) return ".";
  return value.slice(0, idx) || ".";
}}

function basename(path) {{
  const value = String(path || "");
  const idx = value.lastIndexOf("/");
  return idx < 0 ? value : value.slice(idx + 1);
}}

function joinPath(base, name) {{
  if (!base || base === ".") return name;
  return `${{base.replace(/\\/$/, "")}}/${{name}}`;
}}

function defaultDuplicatePath(path) {{
  const name = basename(path);
  const base = dirname(path);
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const suffix = dot > 0 ? name.slice(dot) : "";
  return joinPath(base, `${{stem}} copy${{suffix}}`);
}}

async function runFileAction(action) {{
  const current = selectedTreePath || ".";
  let payload = {{action, path: current}};
  if (action === "new_file") {{
    const path = prompt("New file path", joinPath(selectedTreeBasePath(), "new.c4"));
    if (!path) return;
    payload = {{action, path}};
  }} else if (action === "new_dir") {{
    const path = prompt("New folder path", joinPath(selectedTreeBasePath(), "folder"));
    if (!path) return;
    payload = {{action, path}};
  }} else if (action === "rename") {{
    if (current === ".") return;
    const path = prompt("Rename to", current);
    if (!path || path === current) return;
    payload = {{action, path: current, newPath: path}};
  }} else if (action === "duplicate") {{
    if (current === ".") return;
    const path = prompt("Duplicate to", defaultDuplicatePath(current));
    if (!path) return;
    payload = {{action, path: current, newPath: path}};
  }} else if (action === "delete") {{
    if (current === ".") return;
    if (!confirm(`Delete ${{current}}?`)) return;
  }}
  const result = await api("/api/file-action", payload);
  if (!result.ok) {{
    output.textContent = result.error || "file action failed";
    setTab("outputView");
    return;
  }}
  if (result.path) selectedTreePath = result.path;
  await loadProjectTree(true);
  if (["new_file", "rename", "duplicate"].includes(action) && result.path && treeNodeKinds.get(result.path) !== "dir") {{
    pathInput.value = result.path;
    openPath();
  }}
}}

async function loadSnippets() {{
  const result = await getJson("/api/snippets");
  if (!result.ok) return;
  allSnippets = result.snippets || [];
  selectedSnippetId = selectedSnippetId || (allSnippets[0] && allSnippets[0].id);
  renderSnippetList();
  selectSnippet(selectedSnippetId);
}}

function renderSnippetList() {{
  snippetList.innerHTML = "";
  for (const item of allSnippets) {{
    const row = document.createElement("div");
    row.className = item.id === selectedSnippetId ? "snippetItem active" : "snippetItem";
    row.dataset.id = item.id;
    const label = item.custom ? "custom" : item.category || "built-in";
    const description = item.description || item.code || "";
    row.innerHTML = `<div class="snippetSummary"><small>${{escapeHtml(label)}}</small><strong>${{escapeHtml(item.title || "")}}</strong><span>${{escapeHtml(description)}}</span></div><button class="iconBtn snippetInspectBtn" title="Inspect" aria-label="Inspect snippet"><span class="fa fa-eye iconEye" aria-hidden="true"></span></button>`;
    row.addEventListener("click", () => selectSnippet(item.id));
    row.querySelector(".snippetInspectBtn").addEventListener("click", event => {{
      event.stopPropagation();
      openSnippetModal(item.id);
    }});
    snippetList.appendChild(row);
  }}
}}

function selectedSnippet() {{
  return allSnippets.find(snippet => snippet.id === selectedSnippetId);
}}

function snippetFields(target="sidebar") {{
  if (target === "modal") {{
    return {{
      title: modalSnippetTitle,
      category: modalSnippetCategory,
      description: modalSnippetDescription,
      code: modalSnippetCode
    }};
  }}
  return {{
    title: snippetTitle,
    category: snippetCategory,
    description: snippetDescription,
    code: snippetCode
  }};
}}

function setSnippetFields(item, target="sidebar") {{
  const fields = snippetFields(target);
  fields.title.value = item ? (item.title || "") : "";
  fields.category.value = item ? (item.category || "") : "";
  fields.description.value = item ? (item.description || "") : "";
  fields.code.value = item ? (item.code || "") : "";
}}

function selectSnippet(id) {{
  const item = allSnippets.find(snippet => snippet.id === id) || allSnippets[0];
  if (!item) return;
  selectedSnippetId = item.id;
  setSnippetFields(item, "sidebar");
  renderSnippetList();
  syncSnippetActions();
}}

function currentSnippetFromEditor(target="sidebar") {{
  const fields = snippetFields(target);
  const existing = selectedSnippet();
  const keepId = existing && existing.custom ? existing.id : `custom-${{Date.now()}}`;
  return {{
    id: keepId,
    title: fields.title.value || "Custom snippet",
    category: fields.category.value || "Custom",
    description: fields.description.value || "",
    code: fields.code.value || "",
    custom: true
  }};
}}

function syncSnippetActions() {{
  const existing = selectedSnippet();
  const canDelete = !!(existing && existing.custom);
  document.getElementById("deleteSnippetBtn").disabled = !canDelete;
  document.getElementById("modalDeleteSnippetBtn").disabled = !canDelete;
}}

function openSnippetModal(id=selectedSnippetId) {{
  if (!id && selectedSnippetId === null) {{
    const draft = currentSnippetFromEditor("sidebar");
    setSnippetFields(draft, "modal");
    snippetModalTitle.textContent = draft.title || "Snippet";
    snippetModalMeta.textContent = "Unsaved custom";
    snippetModal.hidden = false;
    modalSnippetCode.focus();
    syncSnippetActions();
    return;
  }}
  selectSnippet(id);
  const item = selectedSnippet();
  if (!item) return;
  setSnippetFields(item, "modal");
  snippetModalTitle.textContent = item.title || "Snippet";
  snippetModalMeta.textContent = `${{item.custom ? "Custom" : "Built-in"}} - ${{item.category || "Snippet"}}`;
  snippetModal.hidden = false;
  modalSnippetCode.focus();
  syncSnippetActions();
}}

function closeSnippetModal() {{
  snippetModal.hidden = true;
  source.focus();
}}

async function saveSnippet(target="sidebar") {{
  const next = currentSnippetFromEditor(target);
  const custom = allSnippets.filter(item => item.custom && item.id !== next.id);
  custom.push(next);
  const result = await api("/api/snippets", {{snippets: custom}});
  if (!result.ok) {{
    output.textContent = result.error || "snippet save failed";
    setTab("outputView");
    return;
  }}
  allSnippets = result.snippets || [];
  selectedSnippetId = next.id;
  renderSnippetList();
  selectSnippet(next.id);
  if (target === "modal") openSnippetModal(next.id);
}}

async function deleteSnippet() {{
  const existing = selectedSnippet();
  if (!existing || !existing.custom) return;
  const custom = allSnippets.filter(item => item.custom && item.id !== existing.id);
  const result = await api("/api/snippets", {{snippets: custom}});
  if (!result.ok) return;
  allSnippets = result.snippets || [];
  selectedSnippetId = allSnippets[0] && allSnippets[0].id;
  renderSnippetList();
  selectSnippet(selectedSnippetId);
  closeSnippetModal();
}}

function newSnippet() {{
  selectedSnippetId = null;
  const draft = {{
    title: "Custom snippet",
    category: "Custom",
    description: "",
    code: "log(\\"hello\\")\\n"
  }};
  setSnippetFields(draft, "sidebar");
  setSnippetFields(draft, "modal");
  renderSnippetList();
  syncSnippetActions();
}}

function insertSnippetAtCursor(code=null) {{
  const selectedCode = code === null ? snippetCode.value : code;
  const codeText = selectedCode || "";
  if (!codeText.trim()) return;
  const cursor = source.selectionStart;
  const lineStart = source.value.lastIndexOf("\\n", Math.max(0, cursor - 1)) + 1;
  const text = codeText.replace(/\\s+$/g, "") + "\\n";
  source.setRangeText(text, lineStart, lineStart, "end");
  source.focus();
  renderSourceHighlight();
  scheduleCompile();
  scheduleAutoSave();
  updateCursorStatus();
}}

function newScratch() {{
  pathInput.value = "idle_scratch.c4";
  source.value = "";
  activeC4Line = null;
  activePythonLines = new Set();
  renderSourceHighlight();
  hideCompletions();
  signatureBox.style.display = "none";
  scheduleCompile();
  source.focus();
}}

function commandItems() {{
  return [
    {{title: "Run current file", hint: "Ctrl+Enter", run: runNow}},
    {{title: "Check and lint", hint: "Compile diagnostics", run: checkNow}},
    {{title: "Format source", hint: "Ctrl+Shift+F", run: formatNow}},
    {{title: "Save file", hint: "Ctrl+S", run: savePath}},
    {{title: "New scratch file", hint: "Clear editor", run: newScratch}},
    {{title: "Show generated Python", hint: "Open Python tab", run: () => setTab("pythonView")}},
    {{title: "Show graph", hint: "Open Grafica tab", run: () => setTab("graphView")}},
    {{title: "Open terminal", hint: "Run project commands", run: () => {{ setTab("terminalView"); terminalInput.focus(); }}}},
    {{title: "Search project", hint: "Focus sidebar search", run: () => projectSearchInput.focus()}},
    {{title: "New snippet", hint: "Create a custom bolt", run: () => {{ newSnippet(); openSnippetModal(null); }}}},
    {{title: "Inspect selected snippet", hint: "Open snippet modal", run: () => openSnippetModal(selectedSnippetId)}},
    {{title: "Open settings", hint: "Font, autosave, run timeout", run: openSettingsModal}},
    {{title: "Toggle theme", hint: "Light or dark", run: toggleTheme}}
  ];
}}

function openCommandPalette() {{
  commandPalette.hidden = false;
  commandInput.value = "";
  renderCommandPalette();
  commandInput.focus();
}}

function closeCommandPalette() {{
  commandPalette.hidden = true;
  source.focus();
}}

function renderCommandPalette() {{
  const query = commandInput.value.trim().toLowerCase();
  const items = commandItems().filter(item => {{
    const haystack = `${{item.title}} ${{item.hint}}`.toLowerCase();
    return !query || haystack.includes(query);
  }});
  commandState = {{
    items,
    selected: Math.min(commandState.selected || 0, Math.max(0, items.length - 1))
  }};
  commandList.innerHTML = "";
  if (!items.length) {{
    const empty = document.createElement("div");
    empty.className = "commandItem";
    empty.innerHTML = "<strong>No commands</strong><span></span>";
    commandList.appendChild(empty);
    return;
  }}
  items.forEach((item, index) => {{
    const row = document.createElement("div");
    row.className = index === commandState.selected ? "commandItem active" : "commandItem";
    row.innerHTML = `<strong>${{escapeHtml(item.title)}}</strong><span>${{escapeHtml(item.hint)}}</span>`;
    row.addEventListener("click", () => runSelectedCommand(index));
    commandList.appendChild(row);
  }});
}}

function moveCommandSelection(delta) {{
  const count = commandState.items.length;
  if (!count) return;
  commandState.selected = (commandState.selected + delta + count) % count;
  renderCommandPalette();
}}

function runSelectedCommand(index=commandState.selected) {{
  const item = commandState.items[index];
  if (!item) return;
  closeCommandPalette();
  item.run();
}}

function setTab(id) {{
  document.querySelectorAll(".tab").forEach(btn => {{
    btn.classList.toggle("active", btn.dataset.tab === id);
  }});
  document.querySelectorAll(".view").forEach(view => {{
    view.classList.toggle("active", view.id === id);
  }});
  if (id === "graphView") renderGraph(lastGraph);
  if (id === "symbolView") renderSymbols(lastSymbols);
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
  renderPython(result.python || "", result.sourceMap || {{mappings: []}});
  lastGraph = result.graph || {{nodes: [], edges: []}};
  lastSymbols = result.symbols || [];
  const metrics = result.metrics || {{}};
  statusEls.c4.textContent = `C4 ${{metrics.cobra4Loc || 0}}`;
  statusEls.py.textContent = `Python ${{metrics.pythonLoc || 0}}`;
  statusEls.saved.textContent = `Saved ${{metrics.savedLoc || 0}}`;
  const diagnostics = result.diagnostics || [];
  const errors = diagnostics.filter(item => item.severity === "error").length;
  const warnings = diagnostics.filter(item => item.severity === "warning").length;
  statusEls.lint.textContent = errors ? `${{errors}} error` : warnings ? `${{warnings}} warning` : "OK";
  statusEls.lint.className = errors ? "status err" : "status ok";
  source.classList.toggle("hasErrors", errors > 0);
  renderProblems(result.diagnostics || []);
  renderGraph(lastGraph);
  renderSymbols(lastSymbols);
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
    if (item.line) {{
      node.dataset.line = item.line;
      node.dataset.column = item.column || 1;
      node.addEventListener("click", () => goToLine(Number(node.dataset.line), Number(node.dataset.column)));
    }}
    problems.appendChild(node);
  }}
}}

function renderSymbols(items) {{
  symbols.innerHTML = "";
  if (!items.length) {{
    const node = document.createElement("div");
    node.className = "problem";
    node.textContent = "OK";
    symbols.appendChild(node);
    return;
  }}
  const addSymbol = (item, child=false) => {{
    const node = document.createElement("div");
    node.className = child ? "symbol child" : "symbol";
    node.dataset.line = item.line || 1;
    node.innerHTML = `<strong>${{escapeHtml(item.name || "")}}</strong><span>${{escapeHtml(item.kind || "")}} ${{item.line || ""}}</span>`;
    node.addEventListener("click", () => goToLine(Number(node.dataset.line), 1));
    symbols.appendChild(node);
    (item.children || []).forEach(childItem => addSymbol(childItem, true));
  }};
  items.forEach(item => addSymbol(item));
}}

function renderSourceHighlight() {{
  const lines = source.value.split("\\n");
  sourceHighlight.innerHTML = lines.map((line, index) => {{
    const lineNo = index + 1;
    const cls = lineNo === activeC4Line ? "sourceLine mappedLine" : "sourceLine";
    return `<span class="${{cls}}" data-line="${{lineNo}}">${{highlightCobra4(line) || " "}}</span>`;
  }}).join("");
  syncSourceHighlightScroll();
}}

function highlightCobra4(line) {{
  const commentIndex = findCommentIndex(line);
  const code = commentIndex >= 0 ? line.slice(0, commentIndex) : line;
  const comment = commentIndex >= 0 ? line.slice(commentIndex) : "";
  const escaped = highlightCobra4Code(code);
  return escaped + (comment ? `<span class="tok-comment">${{escapeHtml(comment)}}</span>` : "");
}}

function highlightCobra4Code(code) {{
  const keywordPattern = /^(data|class|fn|return|if|elif|else|for|in|while|match|case|try|catch|finally|raise|every|seconds?|on|event|from|serve|deploy|to|resource|use|as|async|await|parallel|where|break|continue|pass)$/;
  const builtinPattern = /^(read|save|log|Ok|Err|Result|len|int|str|float|bool|list|dict)$/;
  const tokenPattern = /"(?:\\\\.|[^"\\\\])*"|'(?:\\\\.|[^'\\\\])*'|\\b\\d+(?:\\.\\d+)?\\b|\\b[A-Za-z_][A-Za-z0-9_]*\\b/g;
  let out = "";
  let last = 0;
  for (const match of code.matchAll(tokenPattern)) {{
    const token = match[0];
    out += escapeHtml(code.slice(last, match.index));
    if (token.startsWith('"') || token.startsWith("'")) {{
      out += `<span class="tok-string">${{escapeHtml(token)}}</span>`;
    }} else if (/^\\d/.test(token)) {{
      out += `<span class="tok-number">${{escapeHtml(token)}}</span>`;
    }} else if (keywordPattern.test(token)) {{
      out += `<span class="tok-keyword">${{escapeHtml(token)}}</span>`;
    }} else if (builtinPattern.test(token)) {{
      out += `<span class="tok-builtin">${{escapeHtml(token)}}</span>`;
    }} else {{
      out += escapeHtml(token);
    }}
    last = match.index + token.length;
  }}
  return out + escapeHtml(code.slice(last));
}}

function findCommentIndex(line) {{
  let quote = null;
  for (let i = 0; i < line.length; i++) {{
    const ch = line[i];
    if (quote) {{
      if (ch === "\\\\") i++;
      else if (ch === quote) quote = null;
      continue;
    }}
    if (ch === '"' || ch === "'") {{
      quote = ch;
      continue;
    }}
    if (ch === "#") return i;
    if (ch === "/" && line[i + 1] === "/") return i;
  }}
  return -1;
}}

function syncSourceHighlightScroll() {{
  sourceHighlight.scrollTop = source.scrollTop;
  sourceHighlight.scrollLeft = source.scrollLeft;
}}

function renderPython(code, sourceMap) {{
  lastPythonCode = String(code || "");
  lastSourceMap = sourceMap || {{mappings: []}};
  const byPython = new Map((lastSourceMap.mappings || []).map(item => [item.python, item.cobra4]));
  const lines = lastPythonCode.split("\\n");
  python.innerHTML = lines.map((line, index) => {{
    const pyLine = index + 1;
    const c4Line = byPython.get(pyLine);
    const mapped = activePythonLines.has(pyLine) ? " mappedLine" : "";
    const attr = c4Line ? ` data-c4-line="${{c4Line}}" data-py-line="${{pyLine}}" title="Cobra4 line ${{c4Line}}"` : ` data-py-line="${{pyLine}}"`;
    return `<span class="pyLine${{mapped}}"${{attr}}>${{escapeHtml(line) || " "}}</span>`;
  }}).join("");
}}

function highlightMappedLinesFromC4(line) {{
  activeC4Line = line || null;
  activePythonLines = new Set(
    (lastSourceMap.mappings || [])
      .filter(item => item.cobra4 === activeC4Line)
      .map(item => item.python)
  );
  renderSourceHighlight();
  renderPython(lastPythonCode, lastSourceMap);
}}

function highlightMappedLinesFromPython(pyLine) {{
  const item = (lastSourceMap.mappings || []).find(mapping => mapping.python === pyLine);
  if (!item) return;
  highlightMappedLinesFromC4(item.cobra4);
  goToLine(item.cobra4, 1);
}}

function escapeHtml(value) {{
  return String(value || "").replace(/[&<>"]/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}}[ch]));
}}

function goToLine(line, column=1) {{
  const lines = source.value.split("\\n");
  let pos = 0;
  for (let i = 0; i < Math.max(0, line - 1) && i < lines.length; i++) pos += lines[i].length + 1;
  pos += Math.max(0, column - 1);
  source.focus();
  source.setSelectionRange(pos, pos);
  updateCursorStatus();
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

function cursorPosition() {{
  const pos = source.selectionStart;
  const before = source.value.slice(0, pos).split("\\n");
  return {{line: before.length - 1, column: before[before.length - 1].length}};
}}

function updateCursorStatus() {{
  const pos = cursorPosition();
  statusEls.cursor.textContent = `${{pos.line + 1}}:${{pos.column + 1}}`;
}}

function currentPrefix() {{
  let start = source.selectionStart;
  while (start > 0 && /[A-Za-z0-9_]/.test(source.value[start - 1])) start--;
  return source.value.slice(start, source.selectionStart);
}}

async function requestCompletions(force=false) {{
  if (source.selectionStart !== source.selectionEnd) return hideCompletions();
  const prefix = currentPrefix();
  const prev = source.value[source.selectionStart - prefix.length - 1] || "";
  if (!force && prefix.length < 1 && prev !== ".") return hideCompletions();
  const pos = cursorPosition();
  const result = await api("/api/complete", {{
    source: source.value,
    line: pos.line,
    column: pos.column
  }});
  const allItems = result.items || [];
  const items = prev === "."
    ? allItems
    : allItems.filter(item => item.label.toLowerCase().startsWith(prefix.toLowerCase()));
  completionState = {{items: items.slice(0, 60), selected: 0, prefix}};
  renderCompletions();
}}

function renderCompletions() {{
  const items = completionState.items;
  completionBox.innerHTML = "";
  if (!items.length) return hideCompletions();
  const caret = caretCoordinates();
  completionBox.style.left = `${{caret.left}}px`;
  completionBox.style.top = `${{caret.top + 22}}px`;
  completionBox.style.display = "block";
  items.forEach((item, index) => {{
    const node = document.createElement("div");
    node.className = index === completionState.selected ? "completionItem active" : "completionItem";
    node.innerHTML = `<span>${{escapeHtml(item.label)}}</span><span>${{escapeHtml(item.detail || "")}}</span>`;
    node.addEventListener("mousedown", event => {{
      event.preventDefault();
      completionState.selected = index;
      applyCompletion();
    }});
    completionBox.appendChild(node);
  }});
}}

function hideCompletions() {{
  completionBox.style.display = "none";
  completionBox.innerHTML = "";
}}

function applyCompletion() {{
  const item = completionState.items[completionState.selected];
  if (!item) return;
  const prefix = completionState.prefix || "";
  const start = source.selectionStart - prefix.length;
  const end = source.selectionEnd;
  source.setRangeText(item.insertText || item.label, start, end, "end");
  hideCompletions();
  source.focus();
  renderSourceHighlight();
  updateCursorStatus();
  scheduleCompile();
  scheduleAutoSave();
}}

function moveCompletion(delta) {{
  const count = completionState.items.length;
  if (!count) return;
  completionState.selected = (completionState.selected + delta + count) % count;
  renderCompletions();
}}

function caretCoordinates() {{
  const rect = source.getBoundingClientRect();
  const paneRect = source.parentElement.getBoundingClientRect();
  const text = source.value.slice(0, source.selectionStart);
  const lines = text.split("\\n");
  const lineHeight = 21.7;
  const charWidth = 8.4;
  const top = rect.top - paneRect.top + 18 + (lines.length - 1) * lineHeight - source.scrollTop;
  const left = rect.left - paneRect.left + 18 + lines[lines.length - 1].length * charWidth - source.scrollLeft;
  return {{
    left: Math.max(18, Math.min(left, paneRect.width - 360)),
    top: Math.max(48, top)
  }};
}}

async function requestSignature() {{
  const pos = cursorPosition();
  const result = await api("/api/signature", {{
    source: source.value,
    line: pos.line,
    column: pos.column
  }});
  renderSignature(result.signature);
}}

async function requestHover() {{
  const pos = cursorPosition();
  const result = await api("/api/hover", {{
    source: source.value,
    line: pos.line,
    column: pos.column
  }});
  renderHover(result.contents);
}}

function renderSignature(signature) {{
  if (!signature || !signature.signatures || !signature.signatures.length) {{
    signatureBox.style.display = "none";
    signatureBox.innerHTML = "";
    return;
  }}
  const sig = signature.signatures[signature.activeSignature || 0];
  const active = signature.activeParameter || 0;
  const params = sig.parameters || [];
  let label = escapeHtml(sig.label || "");
  if (params[active] && params[active].label) {{
    const needle = escapeHtml(params[active].label);
    label = label.replace(needle, `<strong>${{needle}}</strong>`);
  }}
  signatureBox.innerHTML = label;
  signatureBox.style.display = "block";
}}

function renderHover(contents) {{
  if (!contents) return;
  let html = escapeHtml(contents);
  html = html.replace(/\\*\\*(.*?)\\*\\*/g, "<strong>$1</strong>");
  html = html.replace(/`([^`]*)`/g, "<code>$1</code>");
  signatureBox.innerHTML = html.replace(/\\n/g, "<br>");
  signatureBox.style.display = "block";
}}

function hideSignatureSoon() {{
  setTimeout(() => {{
    if (!source.matches(":focus")) signatureBox.style.display = "none";
  }}, 120);
}}

async function runNow() {{
  setTab("outputView");
  output.textContent = "Running...";
  const result = await api("/api/run", {{
    source: source.value,
    path: pathInput.value,
    timeout: studioSettings.runTimeout || 10
  }});
  updateCompile(result);
  const chunks = [];
  chunks.push(`exit ${{result.returncode}}`);
  if (result.stdout) chunks.push("\\n[stdout]\\n" + result.stdout);
  if (result.stderr) chunks.push("\\n[stderr]\\n" + result.stderr);
  output.textContent = chunks.join("\\n");
}}

async function runTerminalCommand(command) {{
  command = (command || "").trim();
  if (!command) return;
  setTab("terminalView");
  terminalOutput.textContent += `$ ${{command}}\\n`;
  terminalInput.value = "";
  const result = await api("/api/terminal", {{command, timeout: 120}});
  if (result.stdout) terminalOutput.textContent += result.stdout;
  if (result.stderr) terminalOutput.textContent += result.stderr;
  terminalOutput.textContent += `\\n[exit ${{result.returncode}}]\\n\\n`;
  terminalOutput.scrollTop = terminalOutput.scrollHeight;
}}

async function checkNow() {{
  const result = await compileNow();
  setTab(result.ok ? "pythonView" : "problemView");
}}

async function formatNow() {{
  const result = await api("/api/format", {{
    source: source.value,
    path: pathInput.value
  }});
  if (!result.ok) {{
    renderProblems(result.diagnostics || []);
    setTab("problemView");
    return;
  }}
  source.value = result.source || source.value;
  renderSourceHighlight();
  hideCompletions();
  signatureBox.style.display = "none";
  await compileNow();
  source.focus();
}}

async function openPath() {{
  const response = await fetch("/api/file?path=" + encodeURIComponent(pathInput.value));
  const result = await response.json();
  if (!result.ok) {{
    output.textContent = result.error || "open failed";
    setTab("outputView");
    return result;
  }}
  pathInput.value = result.path;
  source.value = result.source;
  activeC4Line = null;
  activePythonLines = new Set();
  renderSourceHighlight();
  scheduleCompile();
  return result;
}}

function scheduleAutoSave() {{
  if (!studioSettings.autoSave) return;
  clearTimeout(autoSaveTimer);
  autoSaveTimer = setTimeout(() => savePath({{silent: true}}), 900);
}}

async function savePath(options={{}}) {{
  const result = await api("/api/save", {{
    path: pathInput.value,
    source: source.value
  }});
  if (!options.silent) {{
    output.textContent = result.ok ? `saved ${{result.path}}` : (result.error || "save failed");
  }}
  if (result.path) pathInput.value = result.path;
  if (!options.silent) setTab("outputView");
  loadProjectTree(true);
  return result;
}}

document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => setTab(btn.dataset.tab)));
source.addEventListener("input", event => {{
  scheduleCompile();
  renderSourceHighlight();
  scheduleAutoSave();
  updateCursorStatus();
  const text = event.data || "";
  if (text === "(" || text === ",") requestSignature();
  if (text === "." || /[A-Za-z_]/.test(text)) requestCompletions(false);
  if (text === ")" || text === "\\n") signatureBox.style.display = "none";
}});
source.addEventListener("click", () => {{
  updateCursorStatus();
  const pos = cursorPosition();
  highlightMappedLinesFromC4(pos.line + 1);
  hideCompletions();
  requestHover();
}});
source.addEventListener("keyup", event => {{
  updateCursorStatus();
  if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) requestHover();
  if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Escape"].includes(event.key)) return;
  if (event.key === "Escape") {{
    hideCompletions();
    signatureBox.style.display = "none";
  }}
}});
source.addEventListener("keydown", event => {{
  if (completionBox.style.display === "block") {{
    if (event.key === "ArrowDown") {{
      event.preventDefault();
      moveCompletion(1);
      return;
    }}
    if (event.key === "ArrowUp") {{
      event.preventDefault();
      moveCompletion(-1);
      return;
    }}
    if (event.key === "Enter" || event.key === "Tab") {{
      event.preventDefault();
      applyCompletion();
      return;
    }}
  }}
  if (event.key === "Tab") {{
    event.preventDefault();
    source.setRangeText("    ", source.selectionStart, source.selectionEnd, "end");
    renderSourceHighlight();
    scheduleCompile();
    scheduleAutoSave();
    updateCursorStatus();
    return;
  }}
  if (event.ctrlKey || event.metaKey) {{
    if (event.key === " ") {{
      event.preventDefault();
      requestCompletions(true);
    }} else if (event.key === "Enter") {{
      event.preventDefault();
      runNow();
    }} else if (event.key.toLowerCase() === "s") {{
      event.preventDefault();
      savePath();
    }} else if (event.shiftKey && event.key.toLowerCase() === "f") {{
      event.preventDefault();
      formatNow();
    }}
  }}
}});
source.addEventListener("blur", hideSignatureSoon);
source.addEventListener("scroll", syncSourceHighlightScroll);
python.addEventListener("click", event => {{
  const row = event.target.closest && event.target.closest(".pyLine[data-c4-line]");
  if (!row) return;
  highlightMappedLinesFromPython(Number(row.dataset.pyLine || 0));
}});
document.getElementById("runBtn").addEventListener("click", runNow);
document.getElementById("checkBtn").addEventListener("click", checkNow);
document.getElementById("formatBtn").addEventListener("click", formatNow);
document.getElementById("openBtn").addEventListener("click", openPath);
document.getElementById("saveBtn").addEventListener("click", savePath);
document.getElementById("refreshTreeBtn").addEventListener("click", () => loadProjectTree(true));
document.getElementById("themeBtn").addEventListener("click", toggleTheme);
document.getElementById("commandBtn").addEventListener("click", openCommandPalette);
document.getElementById("settingsBtn").addEventListener("click", openSettingsModal);
document.getElementById("newFileBtn").addEventListener("click", () => runFileAction("new_file"));
document.getElementById("newDirBtn").addEventListener("click", () => runFileAction("new_dir"));
document.getElementById("renameFileBtn").addEventListener("click", () => runFileAction("rename"));
document.getElementById("duplicateFileBtn").addEventListener("click", () => runFileAction("duplicate"));
document.getElementById("deleteFileBtn").addEventListener("click", () => runFileAction("delete"));
projectSearchInput.addEventListener("input", scheduleProjectSearch);
projectSearchInput.addEventListener("keydown", event => {{
  if (event.key === "Escape") {{
    projectSearchInput.value = "";
    projectSearchResults.classList.remove("active");
    projectSearchResults.innerHTML = "";
  }}
}});
document.getElementById("newSnippetBtn").addEventListener("click", newSnippet);
document.getElementById("insertSnippetBtn").addEventListener("click", () => insertSnippetAtCursor());
document.getElementById("saveSnippetBtn").addEventListener("click", () => saveSnippet("sidebar"));
document.getElementById("deleteSnippetBtn").addEventListener("click", deleteSnippet);
document.getElementById("inspectSnippetBtn").addEventListener("click", () => openSnippetModal(selectedSnippetId));
document.getElementById("closeSnippetModalBtn").addEventListener("click", closeSnippetModal);
document.getElementById("snippetModalBackdrop").addEventListener("click", closeSnippetModal);
document.getElementById("modalInsertSnippetBtn").addEventListener("click", () => insertSnippetAtCursor(modalSnippetCode.value));
document.getElementById("modalSaveSnippetBtn").addEventListener("click", () => saveSnippet("modal"));
document.getElementById("modalDeleteSnippetBtn").addEventListener("click", deleteSnippet);
document.getElementById("closeCommandPaletteBtn").addEventListener("click", closeCommandPalette);
document.getElementById("commandPaletteBackdrop").addEventListener("click", closeCommandPalette);
document.getElementById("closeSettingsBtn").addEventListener("click", closeSettingsModal);
document.getElementById("settingsModalBackdrop").addEventListener("click", closeSettingsModal);
document.getElementById("saveSettingsBtn").addEventListener("click", saveSettingsFromForm);
document.getElementById("resetSettingsBtn").addEventListener("click", resetSettings);
commandInput.addEventListener("input", () => {{
  commandState.selected = 0;
  renderCommandPalette();
}});
commandInput.addEventListener("keydown", event => {{
  if (event.key === "ArrowDown") {{
    event.preventDefault();
    moveCommandSelection(1);
  }} else if (event.key === "ArrowUp") {{
    event.preventDefault();
    moveCommandSelection(-1);
  }} else if (event.key === "Enter") {{
    event.preventDefault();
    runSelectedCommand();
  }} else if (event.key === "Escape") {{
    event.preventDefault();
    closeCommandPalette();
  }}
}});
document.getElementById("terminalForm").addEventListener("submit", event => {{
  event.preventDefault();
  runTerminalCommand(terminalInput.value);
}});
document.addEventListener("keydown", event => {{
  if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "p") {{
    event.preventDefault();
    openCommandPalette();
    return;
  }}
  if (!commandPalette.hidden && event.key === "Escape") {{
    closeCommandPalette();
    return;
  }}
  if (!settingsModal.hidden && event.key === "Escape") {{
    closeSettingsModal();
    return;
  }}
  if (!snippetModal.hidden && event.key === "Escape") closeSnippetModal();
}});
document.getElementById("newBtn").addEventListener("click", newScratch);

fetch("/api/sample").then(r => r.json()).then(sample => {{
  source.value = sample.source;
  pathInput.value = sample.path;
  renderSourceHighlight();
  updateCursorStatus();
  compileNow();
}});
initTheme();
loadStudioSettings();
loadLayoutState();
initResizers();
loadTreeState();
loadProjectTree(true);
startTreeAutoRefresh();
window.addEventListener("focus", () => loadProjectTree(false));
document.addEventListener("visibilitychange", () => {{
  if (!document.hidden) loadProjectTree(false);
}});
loadSnippets();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
