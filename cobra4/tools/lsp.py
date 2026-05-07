"""Minimal LSP server for cobra4.

Implements just enough of the protocol to give VS Code / Neovim / Helix
useful feedback: parse-time errors as diagnostics, document formatting,
and hover with type info from the type checker.

Speaks JSON-RPC over stdio (no extra deps). Capabilities advertised:

- ``textDocument/didOpen`` / ``didChange`` / ``didSave`` → diagnostics.
- ``textDocument/formatting`` → canonical format from AST.
- ``textDocument/hover`` → variable inferred type, if known.

Run with ``c4 lsp`` (added to the CLI). To wire it up in VS Code,
install the official "Generic LSP Client" extension (or build a tiny
extension yourself) and point it at ``c4 lsp``.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from cobra4.parser import parse, ParseError
from cobra4.resolver import resolve
from cobra4.typecheck import TypeChecker
from cobra4.tools.fmt import format_module


# ---------- IO ----------


@dataclass
class _Reader:
    stream: Any

    def read_message(self) -> Optional[dict]:
        # Read headers until empty line.
        headers = {}
        while True:
            line = self.stream.readline()
            if not line:
                return None
            line = line.decode("utf-8") if isinstance(line, bytes) else line
            line = line.rstrip("\r\n")
            if not line:
                break
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        body = self.stream.read(length) if hasattr(self.stream, "read") else self.stream.buffer.read(length)
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return json.loads(body)


@dataclass
class _Writer:
    stream: Any
    lock: threading.Lock = field(default_factory=threading.Lock)

    def write(self, msg: dict) -> None:
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self.lock:
            target = self.stream.buffer if hasattr(self.stream, "buffer") else self.stream
            target.write(header)
            target.write(body)
            target.flush()


# ---------- Server ----------


class _Server:
    def __init__(self) -> None:
        self.docs: dict[str, str] = {}
        self.shutdown_requested = False

    # ----- handlers -----

    def handle(self, msg: dict, w: _Writer) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        msg_id = msg.get("id")

        if method == "initialize":
            w.write({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "capabilities": {
                        "textDocumentSync": 1,
                        "documentFormattingProvider": True,
                        "hoverProvider": True,
                        "definitionProvider": True,
                        "referencesProvider": True,
                        "documentSymbolProvider": True,
                        "completionProvider": {"triggerCharacters": [".", " "]},
                    },
                    "serverInfo": {"name": "cobra4-lsp", "version": "0.2.0"},
                },
            })
            return
        if method == "initialized":
            return
        if method == "shutdown":
            self.shutdown_requested = True
            w.write({"jsonrpc": "2.0", "id": msg_id, "result": None})
            return
        if method == "exit":
            sys.exit(0 if self.shutdown_requested else 1)

        if method == "textDocument/didOpen":
            uri = params["textDocument"]["uri"]
            self.docs[uri] = params["textDocument"]["text"]
            self._publish_diagnostics(uri, w)
            return
        if method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            # Full sync: last contentChanges entry is the new text.
            changes = params["contentChanges"]
            if changes:
                self.docs[uri] = changes[-1]["text"]
            self._publish_diagnostics(uri, w)
            return
        if method == "textDocument/didSave":
            uri = params["textDocument"]["uri"]
            self._publish_diagnostics(uri, w)
            return
        if method == "textDocument/formatting":
            uri = params["textDocument"]["uri"]
            text = self.docs.get(uri, "")
            try:
                module = parse(text, source_path=uri)
                formatted = format_module(module)
                edits = [{
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 999_999, "character": 0},
                    },
                    "newText": formatted,
                }]
            except ParseError:
                edits = []
            w.write({"jsonrpc": "2.0", "id": msg_id, "result": edits})
            return
        if method == "textDocument/hover":
            uri = params["textDocument"]["uri"]
            text = self.docs.get(uri, "")
            line = params["position"]["line"]
            col = params["position"]["character"]
            info = self._hover_info(text, line, col)
            if info:
                w.write({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"contents": {"kind": "markdown", "value": info}},
                })
            else:
                w.write({"jsonrpc": "2.0", "id": msg_id, "result": None})
            return
        if method == "textDocument/definition":
            uri = params["textDocument"]["uri"]
            text = self.docs.get(uri, "")
            loc = self._definition(text, uri, params["position"]["line"], params["position"]["character"])
            w.write({"jsonrpc": "2.0", "id": msg_id, "result": loc})
            return
        if method == "textDocument/references":
            uri = params["textDocument"]["uri"]
            text = self.docs.get(uri, "")
            locs = self._references(text, uri, params["position"]["line"], params["position"]["character"])
            w.write({"jsonrpc": "2.0", "id": msg_id, "result": locs})
            return
        if method == "textDocument/documentSymbol":
            uri = params["textDocument"]["uri"]
            text = self.docs.get(uri, "")
            symbols = self._document_symbols(text)
            w.write({"jsonrpc": "2.0", "id": msg_id, "result": symbols})
            return
        if method == "textDocument/completion":
            uri = params["textDocument"]["uri"]
            text = self.docs.get(uri, "")
            items = self._completions(text, params["position"]["line"], params["position"]["character"])
            w.write({"jsonrpc": "2.0", "id": msg_id, "result": {"items": items, "isIncomplete": False}})
            return

        # Unhandled but expects response
        if msg_id is not None:
            w.write({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })

    # ----- diagnostics -----

    def _publish_diagnostics(self, uri: str, w: _Writer) -> None:
        text = self.docs.get(uri, "")
        diags: list[dict] = []
        try:
            module = parse(text, source_path=uri)
        except ParseError as e:
            diags.append({
                "range": _range(e.line - 1, e.column - 1, e.line - 1, e.column),
                "severity": 1,  # error
                "source": "cobra4",
                "message": e.message,
            })
            module = None
        if module is not None:
            rr = resolve(module)
            for d in rr.diagnostics:
                if d.loc is None:
                    continue
                diags.append({
                    "range": _range(d.loc.line - 1, d.loc.column - 1, d.loc.line - 1, d.loc.column + 5),
                    "severity": 1 if d.severity == "error" else 2,
                    "source": "cobra4",
                    "code": d.code,
                    "message": d.message,
                })
            for d in TypeChecker().check(module):
                if d.loc is None:
                    continue
                diags.append({
                    "range": _range(d.loc.line - 1, d.loc.column - 1, d.loc.line - 1, d.loc.column + 5),
                    "severity": 2,  # warning
                    "source": "cobra4-types",
                    "code": d.code,
                    "message": d.message,
                })
        w.write({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": diags},
        })

    def _word_at(self, text: str, line: int, col: int) -> Optional[str]:
        lines = text.splitlines()
        if line >= len(lines):
            return None
        src = lines[line]
        start = col
        while start > 0 and (src[start - 1].isalnum() or src[start - 1] == "_"):
            start -= 1
        end = col
        while end < len(src) and (src[end].isalnum() or src[end] == "_"):
            end += 1
        return src[start:end] or None

    def _definition(self, text: str, uri: str, line: int, col: int):
        from cobra4 import ast_nodes as N

        name = self._word_at(text, line, col)
        if not name:
            return None
        try:
            module = parse(text, source_path=uri)
        except ParseError:
            return None
        loc = self._find_decl(module.body, name)
        if loc is None:
            return None
        return {
            "uri": uri,
            "range": _range(loc.line - 1, loc.column - 1, loc.line - 1, loc.column - 1 + len(name)),
        }

    def _find_decl(self, body, name):
        from cobra4 import ast_nodes as N

        for s in body:
            if isinstance(s, N.FnDecl) and s.name == name:
                return s.loc
            if isinstance(s, N.ClassDecl) and s.name == name:
                return s.loc
            if isinstance(s, N.Assign):
                for t in s.targets:
                    if isinstance(t, N.Name) and t.name == name:
                        return t.loc
            # recurse into compound bodies
            if isinstance(s, N.If):
                hit = self._find_decl(s.body, name) or self._find_decl(s.orelse, name)
                if hit:
                    return hit
            if isinstance(s, (N.While, N.For, N.Each, N.Every, N.OnEvent)):
                hit = self._find_decl(s.body, name)
                if hit:
                    return hit
            if isinstance(s, N.FnDecl) and s.block is not None:
                hit = self._find_decl(s.block, name)
                if hit:
                    return hit
            if isinstance(s, N.ClassDecl):
                hit = self._find_decl(s.body, name)
                if hit:
                    return hit
        return None

    def _references(self, text: str, uri: str, line: int, col: int):
        """All Name occurrences matching the identifier under the cursor."""
        from cobra4 import ast_nodes as N

        name = self._word_at(text, line, col)
        if not name:
            return []
        try:
            module = parse(text, source_path=uri)
        except ParseError:
            return []
        locs: list = []

        def visit(node):
            if isinstance(node, N.Name) and node.name == name and node.loc:
                locs.append(node.loc)
            for f in getattr(node, "__dataclass_fields__", {}):
                v = getattr(node, f)
                if isinstance(v, list):
                    for x in v:
                        if hasattr(x, "__dataclass_fields__"):
                            visit(x)
                        elif isinstance(x, tuple):
                            for y in x:
                                if hasattr(y, "__dataclass_fields__"):
                                    visit(y)
                elif hasattr(v, "__dataclass_fields__"):
                    visit(v)

        visit(module)
        return [
            {"uri": uri, "range": _range(loc.line - 1, loc.column - 1, loc.line - 1, loc.column - 1 + len(name))}
            for loc in locs
        ]

    def _document_symbols(self, text: str):
        """Outline: top-level fn / class / data-class declarations."""
        from cobra4 import ast_nodes as N

        try:
            module = parse(text, source_path="<sym>")
        except ParseError:
            return []
        symbols = []
        for s in module.body:
            if isinstance(s, N.FnDecl) and s.loc:
                symbols.append({
                    "name": s.name,
                    "kind": 12,  # Function
                    "range": _range(s.loc.line - 1, 0, s.loc.line - 1, 100),
                    "selectionRange": _range(s.loc.line - 1, 0, s.loc.line - 1, 100),
                })
            elif isinstance(s, N.ClassDecl) and s.loc:
                children = []
                for inner in s.body:
                    if isinstance(inner, N.FnDecl) and inner.loc:
                        children.append({
                            "name": inner.name,
                            "kind": 6,  # Method
                            "range": _range(inner.loc.line - 1, 0, inner.loc.line - 1, 100),
                            "selectionRange": _range(inner.loc.line - 1, 0, inner.loc.line - 1, 100),
                        })
                symbols.append({
                    "name": s.name,
                    "kind": 5,  # Class
                    "range": _range(s.loc.line - 1, 0, s.loc.line - 1, 100),
                    "selectionRange": _range(s.loc.line - 1, 0, s.loc.line - 1, 100),
                    "children": children,
                })
        return symbols

    def _completions(self, text: str, line: int, col: int):
        """Best-effort completion: function/class/var names + cobra4 keywords + builtins."""
        from cobra4 import ast_nodes as N
        from cobra4.resolver import _PY_BUILTINS, _C4_BUILTINS

        keywords = [
            "if", "elif", "else", "while", "for", "each", "in", "and", "or", "not",
            "True", "False", "None", "fn", "class", "data", "return", "raise",
            "break", "continue", "pass", "match", "case", "try", "catch", "finally",
            "use", "as", "where", "every", "on", "from", "to", "with", "parallel",
            "serve", "deploy", "lang",
        ]
        items = [{"label": kw, "kind": 14} for kw in keywords]  # 14 = Keyword
        for b in (_PY_BUILTINS | _C4_BUILTINS):
            items.append({"label": b, "kind": 3})  # Function

        try:
            module = parse(text, source_path="<comp>")
            for s in module.body:
                if isinstance(s, N.FnDecl):
                    items.append({"label": s.name, "kind": 3})  # Function
                elif isinstance(s, N.ClassDecl):
                    items.append({"label": s.name, "kind": 7})  # Class
                elif isinstance(s, N.Assign):
                    for t in s.targets:
                        if isinstance(t, N.Name):
                            items.append({"label": t.name, "kind": 6})  # Variable
        except ParseError:
            pass
        return items

    def _hover_info(self, text: str, line: int, col: int) -> Optional[str]:
        try:
            module = parse(text, source_path="<hover>")
        except ParseError:
            return None
        # Find the token at the cursor position.
        lines = text.splitlines()
        if line >= len(lines):
            return None
        src_line = lines[line]
        # naive identifier extraction
        start = col
        while start > 0 and (src_line[start - 1].isalnum() or src_line[start - 1] == "_"):
            start -= 1
        end = col
        while end < len(src_line) and (src_line[end].isalnum() or src_line[end] == "_"):
            end += 1
        name = src_line[start:end]
        if not name:
            return None
        tc = TypeChecker()
        tc.check(module)
        t = tc.var_types.get(name)
        sig = tc.fn_sigs.get(name)
        out = [f"**{name}**"]
        if sig is not None:
            params = ", ".join(f"{n}: {t}" for n, t in sig.params)
            out.append(f"`fn {name}({params}) -> {sig.return_type}`")
        elif t is not None:
            out.append(f"`{name}: {t}`")
        else:
            return None
        return "\n\n".join(out)


def _range(start_line: int, start_col: int, end_line: int, end_col: int) -> dict:
    return {
        "start": {"line": max(0, start_line), "character": max(0, start_col)},
        "end": {"line": max(0, end_line), "character": max(0, end_col)},
    }


def run() -> int:
    """Entry point: serve LSP on stdio until exit notification."""
    r = _Reader(sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin)
    w = _Writer(sys.stdout)
    server = _Server()
    while True:
        msg = r.read_message()
        if msg is None:
            return 0
        try:
            server.handle(msg, w)
        except SystemExit:
            raise
        except BaseException as e:  # noqa: BLE001
            sys.stderr.write(f"lsp error: {e}\n")
