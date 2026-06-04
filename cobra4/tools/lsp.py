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

from cobra4.parser import parse, ParseError, parse_collect_errors
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
        body = (
            self.stream.read(length)
            if hasattr(self.stream, "read")
            else self.stream.buffer.read(length)
        )
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
            target = (
                self.stream.buffer if hasattr(self.stream, "buffer") else self.stream
            )
            target.write(header)
            target.write(body)
            target.flush()


# ---------- Server ----------


class _Server:
    def __init__(self) -> None:
        self.docs: dict[str, str] = {}
        self.shutdown_requested = False
        # Most-recent parse-success TypeChecker results, keyed by URI.
        # Used by signature help when the current buffer is mid-edit.
        self._last_tc: dict[str, TypeChecker] = {}

    # ----- handlers -----

    def handle(self, msg: dict, w: _Writer) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        msg_id = msg.get("id")

        if method == "initialize":
            w.write(
                {
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
                            "signatureHelpProvider": {"triggerCharacters": ["(", ","]},
                        },
                        "serverInfo": {"name": "cobra4-lsp", "version": "0.3.0"},
                    },
                }
            )
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
                edits = [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 999_999, "character": 0},
                        },
                        "newText": formatted,
                    }
                ]
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
                w.write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"contents": {"kind": "markdown", "value": info}},
                    }
                )
            else:
                w.write({"jsonrpc": "2.0", "id": msg_id, "result": None})
            return
        if method == "textDocument/definition":
            uri = params["textDocument"]["uri"]
            text = self.docs.get(uri, "")
            loc = self._definition(
                text, uri, params["position"]["line"], params["position"]["character"]
            )
            w.write({"jsonrpc": "2.0", "id": msg_id, "result": loc})
            return
        if method == "textDocument/references":
            uri = params["textDocument"]["uri"]
            text = self.docs.get(uri, "")
            locs = self._references(
                text, uri, params["position"]["line"], params["position"]["character"]
            )
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
            items = self._completions(
                text, params["position"]["line"], params["position"]["character"]
            )
            w.write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"items": items, "isIncomplete": False},
                }
            )
            return
        if method == "textDocument/signatureHelp":
            uri = params["textDocument"]["uri"]
            text = self.docs.get(uri, "")
            sig = self._signature_help(
                text, params["position"]["line"], params["position"]["character"]
            )
            w.write({"jsonrpc": "2.0", "id": msg_id, "result": sig})
            return

        # Unhandled but expects response
        if msg_id is not None:
            w.write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )

    # ----- diagnostics -----

    def _publish_diagnostics(self, uri: str, w: _Writer) -> None:
        text = self.docs.get(uri, "")
        diags: list[dict] = []
        try:
            module = parse(text, source_path=uri)
        except ParseError as e:
            diags.append(
                {
                    "range": _range(e.line - 1, e.column - 1, e.line - 1, e.column),
                    "severity": 1,  # error
                    "source": "cobra4",
                    "message": e.message,
                }
            )
            module = None
        if module is not None:
            rr = resolve(module)
            # Cache the typechecker so sig-help still works while editing.
            tc_cached = TypeChecker()
            tc_cached.check(module)
            self._last_tc[uri] = tc_cached
            for d in rr.diagnostics:
                if d.loc is None:
                    continue
                diags.append(
                    {
                        "range": _range(
                            d.loc.line - 1,
                            d.loc.column - 1,
                            d.loc.line - 1,
                            d.loc.column + 5,
                        ),
                        "severity": 1 if d.severity == "error" else 2,
                        "source": "cobra4",
                        "code": d.code,
                        "message": d.message,
                    }
                )
            for d in TypeChecker().check(module):
                if d.loc is None:
                    continue
                diags.append(
                    {
                        "range": _range(
                            d.loc.line - 1,
                            d.loc.column - 1,
                            d.loc.line - 1,
                            d.loc.column + 5,
                        ),
                        "severity": 2,  # warning
                        "source": "cobra4-types",
                        "code": d.code,
                        "message": d.message,
                    }
                )
        w.write(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": diags},
            }
        )

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
            "range": _range(
                loc.line - 1, loc.column - 1, loc.line - 1, loc.column - 1 + len(name)
            ),
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
            {
                "uri": uri,
                "range": _range(
                    loc.line - 1,
                    loc.column - 1,
                    loc.line - 1,
                    loc.column - 1 + len(name),
                ),
            }
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
                symbols.append(
                    {
                        "name": s.name,
                        "kind": 12,  # Function
                        "range": _range(s.loc.line - 1, 0, s.loc.line - 1, 100),
                        "selectionRange": _range(
                            s.loc.line - 1, 0, s.loc.line - 1, 100
                        ),
                    }
                )
            elif isinstance(s, N.ClassDecl) and s.loc:
                children = []
                for inner in s.body:
                    if isinstance(inner, N.FnDecl) and inner.loc:
                        children.append(
                            {
                                "name": inner.name,
                                "kind": 6,  # Method
                                "range": _range(
                                    inner.loc.line - 1, 0, inner.loc.line - 1, 100
                                ),
                                "selectionRange": _range(
                                    inner.loc.line - 1, 0, inner.loc.line - 1, 100
                                ),
                            }
                        )
                symbols.append(
                    {
                        "name": s.name,
                        "kind": 5,  # Class
                        "range": _range(s.loc.line - 1, 0, s.loc.line - 1, 100),
                        "selectionRange": _range(
                            s.loc.line - 1, 0, s.loc.line - 1, 100
                        ),
                        "children": children,
                    }
                )
        return symbols

    def _completions(self, text: str, line: int, col: int):
        """Scope-aware completion.

        - After ``.``, propose member completions for known builtins/types.
        - Otherwise, propose: cobra4 keywords + Py/C4 builtins +
          functions/classes/variables visible at the cursor scope
          (params, locals, loop vars, catch bindings).
        """
        from cobra4 import ast_nodes as N
        from cobra4.resolver import _PY_BUILTINS, _C4_BUILTINS

        # ----- 1. Detect member-access context: text at <line, col-1> is `.` -----
        lines = text.splitlines()
        src_line = lines[line] if line < len(lines) else ""
        prefix_char = src_line[col - 1] if 0 < col <= len(src_line) else ""
        if prefix_char == ".":
            return self._member_completions(src_line[: col - 1])

        keywords = [
            "if",
            "elif",
            "else",
            "while",
            "for",
            "each",
            "in",
            "and",
            "or",
            "not",
            "True",
            "False",
            "None",
            "fn",
            "class",
            "return",
            "raise",
            "break",
            "continue",
            "pass",
            "match",
            "case",
            "try",
            "catch",
            "finally",
            "use",
            "as",
            "where",
            "every",
            "on",
            "event",
            "from",
            "to",
            "parallel",
            "serve",
            "deploy",
            "lang",
        ]
        items: list[dict] = [
            {"label": kw, "kind": 14} for kw in keywords
        ]  # 14 = Keyword
        for b in _PY_BUILTINS | _C4_BUILTINS:
            items.append({"label": b, "kind": 3})  # Function (built-in)

        try:
            module = parse(text, source_path="<comp>")
        except ParseError:
            return items

        # Scope-aware: collect names visible at the cursor line.
        cursor_line = line + 1  # cobra4 Loc is 1-based
        seen: set[str] = set()
        for kind, name in self._scope_names(module.body, cursor_line):
            if name in seen:
                continue
            seen.add(name)
            items.append({"label": name, "kind": kind})
        return items

    def _scope_names(self, body, cursor_line: int):
        """Yield ``(LSP CompletionItemKind, name)`` for everything visible
        at ``cursor_line`` in ``body``. Walks nested scopes — parameters,
        for/each loop vars (statement and expression form), catch
        bindings, class methods, match-pattern binds.

        Extent of a compound statement is computed as ``[start, next_sibling_start - 1]``,
        with the very last statement extending to ``+inf``. This is what
        editors do — it correctly handles the cursor sitting on a blank
        line inside a body (after the last statement, before the closing
        brace).
        """
        from cobra4 import ast_nodes as N

        INF = 10**9

        def _line_of(node) -> int:
            return getattr(getattr(node, "loc", None), "line", 0) or 0

        results: list[tuple[int, str]] = []

        def _walk_expr_for_each(node, container_end: int):
            """Recurse into an expression looking for ``EachExpr`` whose body
            covers the cursor — these introduce a loop variable in scope.

            ``container_end`` is the last line of the enclosing statement
            (next sibling start - 1, or end of file). The block of an
            expression-position EachExpr has no closing-brace token in
            the AST, so we use the container's extent as a fallback.
            """
            if node is None:
                return
            if isinstance(node, N.EachExpr):
                start = _line_of(node)
                inner_end = self._last_loc_in_stmts(node.body)
                end = max(inner_end, container_end)
                if start <= cursor_line <= end:
                    results.append((6, node.var))
                    walk_stmts(node.body, in_scope=True, end_line=end)
            for f in getattr(node, "__dataclass_fields__", {}):
                v = getattr(node, f, None)
                if hasattr(v, "__dataclass_fields__"):
                    _walk_expr_for_each(v, container_end)
                elif isinstance(v, list):
                    for x in v:
                        if hasattr(x, "__dataclass_fields__"):
                            _walk_expr_for_each(x, container_end)

        def walk_stmts(stmts, in_scope: bool, end_line: int):
            """Walk ``stmts``; the enclosing block ends at ``end_line``."""
            n = len(stmts)
            for idx, s in enumerate(stmts):
                start = _line_of(s)
                # Extent of this statement: up to next sibling - 1, or
                # the enclosing block end if last sibling.
                if idx + 1 < n:
                    next_start = _line_of(stmts[idx + 1]) or (start + 1)
                    extent_end = next_start - 1
                else:
                    extent_end = end_line

                # ----- Always-exposed names -----
                if isinstance(s, N.FnDecl):
                    results.append((3, s.name))  # callable everywhere in module
                elif isinstance(s, N.ClassDecl):
                    results.append((7, s.name))
                elif isinstance(s, N.Assign) and in_scope:
                    for t in s.targets:
                        if isinstance(t, N.Name):
                            results.append((6, t.name))
                elif isinstance(s, N.AugAssign) and in_scope:
                    if isinstance(s.target, N.Name):
                        results.append((6, s.target.name))

                # ----- Expression-position EachExpr (e.g. inside Assign.value, ExprStmt) -----
                if isinstance(s, N.Assign):
                    _walk_expr_for_each(s.value, extent_end)
                elif isinstance(s, N.ExprStmt):
                    _walk_expr_for_each(s.value, extent_end)
                elif isinstance(s, N.AugAssign):
                    _walk_expr_for_each(s.value, extent_end)

                cursor_inside = start <= cursor_line <= extent_end

                # ----- Recurse into compound stmts -----
                if isinstance(s, N.FnDecl) and s.block is not None and cursor_inside:
                    for p in s.params:
                        results.append((6, p.name))
                    walk_stmts(s.block, in_scope=True, end_line=extent_end)
                elif isinstance(s, N.ClassDecl) and cursor_inside:
                    walk_stmts(s.body, in_scope=True, end_line=extent_end)
                elif isinstance(s, N.For) and cursor_inside:
                    results.append((6, s.var))
                    walk_stmts(s.body, in_scope=in_scope, end_line=extent_end)
                elif isinstance(s, N.Each) and cursor_inside:
                    results.append((6, s.var))
                    walk_stmts(s.body, in_scope=in_scope, end_line=extent_end)
                elif isinstance(s, N.If) and cursor_inside:
                    walk_stmts(s.body, in_scope=in_scope, end_line=extent_end)
                    for _, eb in s.elifs:
                        walk_stmts(eb, in_scope=in_scope, end_line=extent_end)
                    walk_stmts(s.orelse, in_scope=in_scope, end_line=extent_end)
                elif isinstance(s, (N.While, N.Every, N.OnEvent)) and cursor_inside:
                    walk_stmts(
                        getattr(s, "body", []) or [],
                        in_scope=in_scope,
                        end_line=extent_end,
                    )
                elif isinstance(s, N.Try) and cursor_inside:
                    walk_stmts(s.body, in_scope=in_scope, end_line=extent_end)
                    for c in s.catches:
                        # Catch binding visible to its body — extent of a
                        # catch body is just the next catch / finally / end.
                        if c.name:
                            results.append((6, c.name))
                        walk_stmts(c.body, in_scope=in_scope, end_line=extent_end)
                    walk_stmts(s.finally_body, in_scope=in_scope, end_line=extent_end)
                elif isinstance(s, N.Match) and cursor_inside:
                    for c in s.cases:
                        for nm in self._pattern_binds(c.pattern):
                            results.append((6, nm))
                        walk_stmts(c.body, in_scope=in_scope, end_line=extent_end)

        walk_stmts(body, in_scope=True, end_line=INF)
        return results

    def _last_loc_in_stmts(self, stmts) -> int:
        """Largest ``loc.line`` reachable from ``stmts``. Used only to
        decide whether the cursor is inside an expression-position EachExpr."""
        from cobra4 import ast_nodes as N

        mx = 0

        def visit(node):
            nonlocal mx
            if node is None:
                return
            ll = getattr(getattr(node, "loc", None), "line", 0) or 0
            if ll > mx:
                mx = ll
            for f in getattr(node, "__dataclass_fields__", {}):
                v = getattr(node, f, None)
                if hasattr(v, "__dataclass_fields__"):
                    visit(v)
                elif isinstance(v, list):
                    for x in v:
                        if hasattr(x, "__dataclass_fields__"):
                            visit(x)

        for s in stmts:
            visit(s)
        return mx

    def _pattern_binds(self, pat) -> list[str]:
        from cobra4 import ast_nodes as N

        if pat is None:
            return []
        names: list[str] = []
        if isinstance(pat, N.PatName):
            names.append(pat.name)
        elif isinstance(pat, N.PatRest):
            names.append(pat.name)
        elif isinstance(pat, (N.PatList, N.PatTuple)):
            for it in pat.items:
                names.extend(self._pattern_binds(it))
        elif isinstance(pat, N.PatDict):
            for _, v in pat.entries:
                names.extend(self._pattern_binds(v))
            if pat.rest_name:
                names.append(pat.rest_name)
        elif isinstance(pat, N.PatCall):
            for it in pat.items:
                names.extend(self._pattern_binds(it))
        elif isinstance(pat, N.PatOr):
            # All alternatives must bind the same names; use the first.
            if pat.alternatives:
                names.extend(self._pattern_binds(pat.alternatives[0]))
        return names

    # Static "known attributes" map for cobra4 builtins. When the user
    # types `req.`, we don't have a static type system rich enough to
    # resolve every prefix, but for the most common shapes shipped with
    # the runtime we can offer the right set.
    _KNOWN_MEMBERS: dict[str, list[tuple[str, int]]] = {
        # http.Request — see cobra4/runtime/http.py / serve handler API
        "req": [
            ("method", 5),
            ("path", 5),
            ("params", 5),
            ("headers", 5),
            ("body", 5),
            ("json", 2),
            ("text", 2),
        ],
        "request": [
            ("method", 5),
            ("path", 5),
            ("params", 5),
            ("headers", 5),
            ("body", 5),
            ("json", 2),
            ("text", 2),
        ],
        # CommandResult from fleet.run
        "result": [("stdout", 5), ("stderr", 5), ("returncode", 5), ("ok", 5)],
        # log() namespace
        "log": [("info", 2), ("warn", 2), ("error", 2), ("debug", 2)],
        # SmartFn methods
        "read": [("register", 2), ("handlers", 5)],
        "save": [("register", 2), ("handlers", 5)],
        # Host (fleet)
        "host": [("name", 5), ("addr", 5), ("user", 5), ("port", 5), ("extra", 5)],
        "h": [("name", 5), ("addr", 5), ("user", 5), ("port", 5), ("extra", 5)],
    }

    # Per-string-method completion. Keep small and idiomatic.
    _STRING_METHODS = [
        "upper",
        "lower",
        "strip",
        "lstrip",
        "rstrip",
        "split",
        "splitlines",
        "startswith",
        "endswith",
        "replace",
        "find",
        "join",
        "format",
        "encode",
        "decode",
        "isdigit",
        "isalpha",
        "isalnum",
    ]
    _LIST_METHODS = [
        "append",
        "extend",
        "insert",
        "pop",
        "remove",
        "sort",
        "reverse",
        "count",
        "index",
        "clear",
        "copy",
    ]
    _DICT_METHODS = [
        "get",
        "keys",
        "values",
        "items",
        "update",
        "pop",
        "setdefault",
        "clear",
        "copy",
    ]

    def _member_completions(self, prefix: str):
        """Given the source up to (but not including) the trailing dot,
        return a best-effort list of member completions.

        Strategy:
        1. Last identifier in ``prefix`` matches a known runtime shape →
           return that shape's members.
        2. Otherwise: union of common str/list/dict methods (better to
           over-suggest than under-suggest in a duck-typed language).
        """
        # Extract the trailing identifier (or chain ending with NAME).
        i = len(prefix) - 1
        while i >= 0 and (prefix[i].isalnum() or prefix[i] == "_"):
            i -= 1
        last = prefix[i + 1 :]

        items: list[dict] = []
        known = self._KNOWN_MEMBERS.get(last)
        if known:
            for name, kind in known:
                items.append({"label": name, "kind": kind})
            return items

        # Generic fallback: union of common method names. Mark them as
        # "Method" (kind=2) so the IDE shows the right icon.
        seen: set[str] = set()
        for name in self._STRING_METHODS + self._LIST_METHODS + self._DICT_METHODS:
            if name in seen:
                continue
            seen.add(name)
            items.append({"label": name, "kind": 2})
        return items

    def _signature_help(self, text: str, line: int, col: int):
        """Find the enclosing call at the cursor and return its signature.

        Walks backwards from the cursor counting parentheses to find the
        function name being called and the current argument index.
        """
        lines = text.splitlines()
        if line >= len(lines):
            return None
        # Build a flat string up to the cursor.
        flat = "\n".join(lines[:line] + [lines[line][:col]])

        depth = 0
        active_param = 0
        i = len(flat) - 1
        # Skip backwards past the current arg counting commas at depth 0.
        in_string: Optional[str] = None
        while i >= 0:
            c = flat[i]
            if in_string:
                if c == in_string and (i == 0 or flat[i - 1] != "\\"):
                    in_string = None
                i -= 1
                continue
            if c in ('"', "'"):
                in_string = c
                i -= 1
                continue
            if c == ")":
                depth += 1
            elif c == "(":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                active_param += 1
            i -= 1
        if i < 0:
            return None
        # ``i`` now points at the matching `(`. Read the identifier just before it.
        j = i - 1
        while j >= 0 and flat[j] in " \t":
            j -= 1
        end = j + 1
        while j >= 0 and (flat[j].isalnum() or flat[j] == "_"):
            j -= 1
        fn_name = flat[j + 1 : end]
        if not fn_name:
            return None

        # Look up the signature. Strategy:
        # 1. Try parsing the full buffer (works most of the time after a save).
        # 2. Fall back to ``parse_collect_errors`` (parses chunks, recovers).
        # 3. Fall back to the most recent successfully-parsed cache.
        # 4. Fall back to the static built-in signature catalog.
        sig = None
        tc = None
        try:
            module = parse(text, source_path="<sig>")
            tc = TypeChecker()
            tc.check(module)
        except ParseError:
            module, _errs = parse_collect_errors(text, source_path="<sig>")
            if module is not None:
                tc = TypeChecker()
                tc.check(module)
        if tc is not None:
            sig = tc.fn_sigs.get(fn_name)
        if sig is None:
            # Reach into the per-URI cache populated by didChange/didSave.
            for cached in self._last_tc.values():
                if fn_name in cached.fn_sigs:
                    sig = cached.fn_sigs[fn_name]
                    break
        if sig is None:
            stub = self._BUILTIN_SIGS.get(fn_name)
            if stub is None:
                return None
            label, params, doc = stub
        else:
            param_strs = [f"{n}: {t}" for n, t in sig.params]
            label = f"{fn_name}({', '.join(param_strs)}) -> {sig.return_type}"
            params = [{"label": ps} for ps in param_strs]
            doc = None

        info: dict = {
            "signatures": [{"label": label, "parameters": params}],
            "activeSignature": 0,
            "activeParameter": min(active_param, max(len(params) - 1, 0)),
        }
        if doc:
            info["signatures"][0]["documentation"] = {"kind": "markdown", "value": doc}
        return info

    # Tiny built-in signature catalog for the most common cobra4 calls.
    # Each entry: (full label, [{"label": param_str}, ...], optional markdown doc)
    _BUILTIN_SIGS: dict[str, tuple[str, list[dict], Optional[str]]] = {
        "read": (
            "read(target: str | path) -> Any",
            [{"label": "target"}],
            "Smart-dispatched read. Routes by URI scheme + ext + MIME.",
        ),
        "save": (
            "save(value: Any, target: str | path) -> None",
            [{"label": "value"}, {"label": "target"}],
            "Atomic save. Routes by target ext.",
        ),
        "log": (
            "log(msg: str, **kw) -> None",
            [{"label": "msg"}, {"label": "**kw"}],
            "Structured log. Set `COBRA4_LOG_FORMAT=json` for JSON output.",
        ),
        "fetch": (
            "fetch(url: str, method='GET', **kw) -> Response",
            [{"label": "url"}, {"label": "method"}, {"label": "**kw"}],
            None,
        ),
        "secret": (
            "secret(path: str) -> str",
            [{"label": "path"}],
            "Backend selected via `COBRA4_SECRETS_BACKEND`.",
        ),
        "run": (
            "run(cmd: str | list, host: Host=None, shell: bool=False) -> CommandResult",
            [{"label": "cmd"}, {"label": "host"}, {"label": "shell"}],
            "Local subprocess by default; remote SSH when `host=` is given.",
        ),
        "queue": (
            "queue(name: str) -> EventSource",
            [{"label": "name"}],
            "Backend via `COBRA4_QUEUE_BACKEND` (memory/file/sqs/redis).",
        ),
        "inventory": (
            "inventory(group_or_glob: str) -> list[Host]",
            [{"label": "group_or_glob"}],
            None,
        ),
    }

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
        while start > 0 and (
            src_line[start - 1].isalnum() or src_line[start - 1] == "_"
        ):
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
