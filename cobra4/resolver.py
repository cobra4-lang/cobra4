"""Name resolution and lvalue validation.

M2 expands beyond M1's flat scope to:

- Track nested function / class / block scopes.
- Emit warnings for undefined names (severity ``warning`` — cobra4 stays
  dynamic-friendly; ``c4 check`` can be configured to treat them as errors).
- Detect shadowing of outer-scope names by inner declarations.

Errors are reserved for things that prevent code generation:
invalid lvalues, malformed declarations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cobra4 import ast_nodes as N


# ---------- Diagnostics ----------


@dataclass
class Diagnostic:
    severity: str  # "error" | "warning"
    message: str
    loc: Optional[N.Loc] = None
    code: str = ""

    def __str__(self) -> str:
        prefix = self.severity.upper()
        loc = f"{self.loc}" if self.loc else "?:?"
        code = f"[{self.code}] " if self.code else ""
        return f"{prefix} {code}at {loc}: {self.message}"


@dataclass
class ResolveResult:
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    def ok(self) -> bool:
        return not self.errors


# ---------- Builtins ----------


_PY_BUILTINS: frozenset[str] = frozenset(
    {
        "print", "len", "range", "list", "dict", "set", "tuple", "str", "int",
        "float", "bool", "bytes", "type", "isinstance", "open", "abs", "min",
        "max", "sum", "sorted", "enumerate", "zip", "map", "filter", "any",
        "all", "True", "False", "None", "Exception", "ValueError", "TypeError",
        "KeyError", "IndexError", "RuntimeError", "input", "repr", "object",
        "frozenset", "bytearray", "complex", "id", "hash", "iter", "next",
        "reversed", "round", "divmod", "pow", "hex", "oct", "bin", "ord",
        "chr", "ascii", "callable", "issubclass", "vars", "dir", "getattr",
        "setattr", "hasattr", "delattr", "globals", "locals",
    }
)

_C4_BUILTINS: frozenset[str] = frozenset(
    {
        # smart functions / runtime
        "read", "save", "log", "smart", "every", "on_event", "parallel_for",
        "secret", "queue", "fetch", "http", "inventory", "run", "host",
        "event", "self", "cls",
        # M3 cloud / fleet / deploy
        "Host", "fan_out", "env_from",
        "aws", "gcp", "azure", "k8s", "fly",
        # M4 daemon
        "serve_forever",
    }
)

_BUILTINS = _PY_BUILTINS | _C4_BUILTINS


# ---------- Scope ----------


@dataclass
class _Scope:
    parent: Optional["_Scope"] = None
    kind: str = "module"  # "module" | "function" | "class" | "block"
    names: dict[str, N.Loc | None] = field(default_factory=dict)

    def define(self, name: str, loc: Optional[N.Loc] = None) -> None:
        self.names[name] = loc

    def lookup(self, name: str) -> bool:
        if name in self.names:
            return True
        if self.parent is not None:
            return self.parent.lookup(name)
        return False

    def is_local(self, name: str) -> bool:
        return name in self.names


# ---------- Resolver ----------


class Resolver:
    def __init__(
        self,
        *,
        warn_undefined: bool = True,
        warn_shadowing: bool = True,
        extra_builtins: tuple[str, ...] = (),
    ) -> None:
        self.result = ResolveResult()
        self.module_scope = _Scope(kind="module")
        # Seed with builtins so undefined-check skips them.
        for n in _BUILTINS:
            self.module_scope.define(n)
        # Plugin-provided names (e.g. `sql_run` when `lang use sql` is on).
        for n in extra_builtins:
            self.module_scope.define(n)
        self.scope: _Scope = self.module_scope
        self.warn_undefined = warn_undefined
        self.warn_shadowing = warn_shadowing

    # ---------- API ----------

    def resolve_module(self, module: N.Module) -> ResolveResult:
        # Two-pass for top level: collect declarations, then visit.
        for s in module.body:
            self._collect_top_decls(s)
        for s in module.body:
            self._visit_stmt(s)
        return self.result

    # ---------- helpers ----------

    def _enter(self, kind: str) -> _Scope:
        new = _Scope(parent=self.scope, kind=kind)
        self.scope = new
        return new

    def _leave(self) -> None:
        assert self.scope.parent is not None, "cannot leave module scope"
        self.scope = self.scope.parent

    def _err(self, msg: str, loc: Optional[N.Loc], code: str = "E001") -> None:
        self.result.diagnostics.append(Diagnostic("error", msg, loc, code))

    def _warn(self, msg: str, loc: Optional[N.Loc], code: str = "W001") -> None:
        self.result.diagnostics.append(Diagnostic("warning", msg, loc, code))

    def _define(self, name: str, loc: Optional[N.Loc]) -> None:
        if (
            self.warn_shadowing
            and self.scope.parent is not None
            and self.scope.parent.lookup(name)
            and name not in _BUILTINS
            and not self.scope.is_local(name)
        ):
            self._warn(f"name '{name}' shadows an outer-scope binding", loc, "W002")
        self.scope.define(name, loc)

    def _collect_top_decls(self, s: N.Stmt) -> None:
        if isinstance(s, N.FnDecl):
            self.module_scope.define(s.name, s.loc)
        elif isinstance(s, N.ClassDecl):
            self.module_scope.define(s.name, s.loc)
        elif isinstance(s, N.Use):
            self.module_scope.define(s.alias or _last_segment(s.target), s.loc)
        elif isinstance(s, N.Assign):
            for t in s.targets:
                if isinstance(t, N.Name):
                    self.module_scope.define(t.name, t.loc)

    # ---------- lvalues ----------

    def _validate_lvalue(self, target: N.Expr) -> None:
        if isinstance(target, N.Name):
            return
        if isinstance(target, (N.Attr, N.Index)):
            return
        if isinstance(target, N.Tuple):
            for it in target.items:
                self._validate_lvalue(it)
            return
        self._err("invalid assignment target", target.loc, "E002")

    def _bind_lvalue(self, target: N.Expr) -> None:
        if isinstance(target, N.Name):
            self._define(target.name, target.loc)
        elif isinstance(target, N.Tuple):
            for it in target.items:
                self._bind_lvalue(it)

    # ---------- statements ----------

    def _visit_stmts(self, body: list[N.Stmt]) -> None:
        for s in body:
            self._visit_stmt(s)

    def _visit_stmt(self, s: N.Stmt) -> None:
        if isinstance(s, N.Assign):
            for t in s.targets:
                self._validate_lvalue(t)
            self._visit_expr(s.value)
            for t in s.targets:
                self._bind_lvalue(t)
        elif isinstance(s, N.AugAssign):
            if s.target is not None:
                self._validate_lvalue(s.target)
            self._visit_expr(s.value)
        elif isinstance(s, N.ExprStmt):
            self._visit_expr(s.value)
        elif isinstance(s, N.Return):
            self._visit_expr(s.value)
        elif isinstance(s, N.Raise):
            self._visit_expr(s.value)
        elif isinstance(s, (N.Break, N.Continue, N.Pass)):
            pass
        elif isinstance(s, N.If):
            self._visit_expr(s.cond)
            self._visit_stmts(s.body)
            for cond, body in s.elifs:
                self._visit_expr(cond)
                self._visit_stmts(body)
            self._visit_stmts(s.orelse)
        elif isinstance(s, N.While):
            self._visit_expr(s.cond)
            self._visit_stmts(s.body)
        elif isinstance(s, N.For):
            self._visit_expr(s.iterable)
            self._define(s.var, s.loc)
            self._visit_stmts(s.body)
        elif isinstance(s, N.Each):
            self._visit_expr(s.iterable)
            self._define(s.var, s.loc)
            self._visit_stmts(s.body)
        elif isinstance(s, N.Every):
            self._visit_stmts(s.body)
        elif isinstance(s, N.OnEvent):
            self._visit_expr(s.source)
            self._define("event", s.loc)
            self._visit_stmts(s.body)
        elif isinstance(s, N.Match):
            self._visit_expr(s.subject)
            for c in s.cases:
                self._enter("block")
                self._bind_pattern(c.pattern)
                if c.guard is not None:
                    self._visit_expr(c.guard)
                self._visit_stmts(c.body)
                self._leave()
        elif isinstance(s, N.Try):
            self._visit_stmts(s.body)
            for c in s.catches:
                self._visit_expr(c.exc)
                if c.name:
                    self._define(c.name, c.loc)
                self._visit_stmts(c.body)
            self._visit_stmts(s.finally_body)
        elif isinstance(s, N.Serve):
            self._visit_expr(s.handler)
        elif isinstance(s, N.Deploy):
            self._visit_expr(s.handler)
            self._visit_expr(s.target)
            self._visit_stmts(s.body)
        elif isinstance(s, N.FnDecl):
            self._define(s.name, s.loc)
            self._enter("function")
            for p in s.params:
                self._define(p.name, p.loc)
                if p.default is not None:
                    self._visit_expr(p.default)
            if s.body is not None:
                self._visit_expr(s.body)
            if s.block is not None:
                self._visit_stmts(s.block)
            self._leave()
        elif isinstance(s, N.ClassDecl):
            self._define(s.name, s.loc)
            self._enter("class")
            self._define("self", s.loc)
            self._define("cls", s.loc)
            self._visit_stmts(s.body)
            self._leave()
        elif isinstance(s, N.Use):
            self._define(s.alias or _last_segment(s.target), s.loc)

    def _bind_pattern(self, p: Optional[N.Pattern]) -> None:
        if p is None:
            return
        if isinstance(p, N.PatName):
            self._define(p.name, p.loc)
        elif isinstance(p, N.PatCall):
            for it in p.items:
                self._bind_pattern(it)
        elif isinstance(p, N.PatList):
            for it in p.items:
                self._bind_pattern(it)
        elif isinstance(p, N.PatDict):
            for _k, v in p.entries:
                self._bind_pattern(v)
            if p.rest_name:
                self._define(p.rest_name, p.loc)
        elif isinstance(p, N.PatRest):
            self._define(p.name, p.loc)
        elif isinstance(p, N.PatOr):
            for alt in p.alternatives:
                self._bind_pattern(alt)
        elif isinstance(p, N.PatTuple):
            for it in p.items:
                self._bind_pattern(it)

    # ---------- expressions ----------

    def _visit_expr(self, e: Optional[N.Expr]) -> None:
        if e is None:
            return
        if isinstance(e, N.Name):
            if self.warn_undefined and not self.scope.lookup(e.name):
                self._warn(f"undefined name '{e.name}'", e.loc, "W003")
            return
        if isinstance(e, (N.Num, N.Str, N.Bool, N.NoneLit)):
            return
        if isinstance(e, N.UnaryOp):
            self._visit_expr(e.operand)
        elif isinstance(e, N.BinOp):
            self._visit_expr(e.left)
            self._visit_expr(e.right)
        elif isinstance(e, N.BoolOp):
            for op in e.operands:
                self._visit_expr(op)
        elif isinstance(e, N.Compare):
            self._visit_expr(e.left)
            for c in e.comparators:
                self._visit_expr(c)
        elif isinstance(e, N.Nullish):
            for op in e.operands:
                self._visit_expr(op)
        elif isinstance(e, N.Ternary):
            self._visit_expr(e.cond)
            self._visit_expr(e.if_true)
            self._visit_expr(e.if_false)
        elif isinstance(e, (N.Attr, N.SafeAttr)):
            self._visit_expr(e.target)
        elif isinstance(e, N.Index):
            self._visit_expr(e.target)
            self._visit_expr(e.key)
        elif isinstance(e, N.Call):
            self._visit_expr(e.func)
            for a in e.args:
                self._visit_expr(a.value)
        elif isinstance(e, (N.List, N.Tuple, N.Set)):
            for it in e.items:
                self._visit_expr(it)
        elif isinstance(e, N.Dict):
            for k, v in e.entries:
                self._visit_expr(k)
                self._visit_expr(v)
        elif isinstance(e, N.Lambda):
            self._enter("function")
            for p in e.params:
                self._define(p.name, p.loc)
            if e.body is not None:
                self._visit_expr(e.body)
            if e.block is not None:
                self._visit_stmts(e.block)
            self._leave()
        elif isinstance(e, N.EachExpr):
            self._visit_expr(e.iterable)
            self._enter("block")
            self._define(e.var, e.loc)
            self._visit_stmts(e.body)
            self._leave()


# ---------- Public API ----------


def resolve(
    module: N.Module,
    *,
    warn_undefined: bool = True,
    warn_shadowing: bool = True,
    extra_builtins: tuple[str, ...] = (),
) -> ResolveResult:
    return Resolver(
        warn_undefined=warn_undefined,
        warn_shadowing=warn_shadowing,
        extra_builtins=extra_builtins,
    ).resolve_module(module)


def _last_segment(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]
