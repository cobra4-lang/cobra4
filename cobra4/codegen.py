"""Codegen: cobra4 AST → Python source (string).

The output is deliberately deterministic and reasonably formatted so
generated files can be diffed and read like hand-written Python.

Sugar lowered inline:

- ``each x in xs in parallel(...) { body }``:
    *expression* form  → ``parallel_for(xs, lambda x: <body>, **opts)``
    *statement* form    → same call, used as expression statement
- ``each x in xs { body }`` (sequential):
    *expression* form → list-comprehension or inline list build
    *statement* form  → ordinary ``for`` loop
- ``a?.b``  → ``safe_attr(a, "b")``
- ``a ?? b`` → ``(a if a is not None else b)``
- ``"hi {x}"`` → ``f"hi {x}"`` (only for non-raw strings carrying ``{}``)
- ``every Ns { body }`` → ``every(N, lambda: <body>)``
- ``on event from src { body }`` → ``on_event(src, lambda event: <body>)``
- ``serve fn on :port`` → ``_c4_serve(fn, port)``
- ``deploy fn to target { body }`` → ``_c4_deploy(fn, target, lambda: <body>)``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cobra4 import ast_nodes as N
from cobra4.source_map import SourceMap


# Names of runtime helpers injected at module top.
_RUNTIME_IMPORT = (
    "from cobra4.runtime import (\n"
    "    safe_attr as _c4_safe_attr,\n"
    "    default as _c4_default,\n"
    "    every as _c4_every,\n"
    "    on_event as _c4_on_event,\n"
    "    parallel_for as _c4_parallel_for,\n"
    "    read, save, log,\n"
    "    smart,\n"
    "    Host, inventory, run, fan_out,\n"
    "    secret,\n"
    "    deploy as _c4_deploy,\n"
    "    env_from,\n"
    "    aws, gcp, azure, k8s, fly,\n"
    "    queue, serve_forever,\n"
    "    Result, Ok, Err,\n"
    "    _c4_try_propagate, _C4Propagate,\n"
    ")\n"
    "from cobra4.runtime.core import serve_handler as _c4_serve\n"
    "from cobra4.runtime.concurrency import async_parallel_for as _c4_async_parallel_for\n"
)


# Tracks whether we're currently inside an `async fn` body. Affects how
# `each ... in parallel` is emitted (sync fan-out vs `await asyncio.gather`).
_ASYNC_DEPTH = 0


def _in_async() -> bool:
    return _ASYNC_DEPTH > 0


def _enter_async() -> None:
    global _ASYNC_DEPTH
    _ASYNC_DEPTH += 1


def _leave_async() -> None:
    global _ASYNC_DEPTH
    _ASYNC_DEPTH -= 1


@dataclass
class CodegenResult:
    code: str
    source_map: SourceMap


@dataclass
class _Emitter:
    indent_level: int = 0
    lines: list[str] = field(default_factory=list)
    source_map: SourceMap = field(default_factory=SourceMap)
    cobra4_path: str = ""

    def write(self, s: str, c4_line: int = 0, c4_col: int = 0) -> None:
        prefix = "    " * self.indent_level
        for line in s.split("\n"):
            self.lines.append(prefix + line if line else "")
            py_line = len(self.lines)
            if c4_line:
                self.source_map.record(py_line, c4_line, len(prefix), c4_col)

    def blank(self) -> None:
        self.lines.append("")

    def text(self) -> str:
        return "\n".join(self.lines) + ("\n" if self.lines and self.lines[-1] else "")


def generate(module: N.Module, cobra4_path: str = "") -> CodegenResult:
    em = _Emitter(cobra4_path=cobra4_path)
    em.source_map.cobra4_path = cobra4_path
    # Header
    em.lines.append(f"# generated from {cobra4_path or '<input>'} by cobra4")
    em.lines.append("# DO NOT EDIT — re-run `c4 build` instead.")
    em.lines.append("")
    em.lines.extend(_RUNTIME_IMPORT.rstrip().split("\n"))
    em.blank()
    for stmt in module.body:
        _emit_stmt(em, stmt)
    return CodegenResult(code=em.text(), source_map=em.source_map)


# ---------- Statement emission ----------


def _emit_stmt(em: _Emitter, s: N.Stmt) -> None:
    line = s.loc.line if s.loc else 0
    if isinstance(s, N.ExprStmt):
        # `each x in xs { ... }` at statement level: the parser wraps
        # it in an ExprStmt because cobra4 grammar only has `each_expr`.
        # Compiling the lambda form here loses side-effect statements
        # in the body (assignments can't run inside a Python lambda's
        # expression context). Recover by emitting a real `for` loop —
        # the result list isn't used anyway in statement context.
        if isinstance(s.value, N.EachExpr) and not s.value.parallel:
            ee = s.value
            em.write(f"for {ee.var} in {_expr(ee.iterable)}:", line)
            if ee.where is not None:
                em.indent_level += 1
                em.write(f"if not ({_expr(ee.where)}):")
                em.indent_level += 1
                em.write("continue")
                em.indent_level -= 2
            _emit_block(em, ee.body)
        else:
            em.write(_expr(s.value), line)
    elif isinstance(s, N.Assign):
        targets = ", ".join(_expr(t) for t in s.targets)
        em.write(f"{targets} = {_expr(s.value)}", line)
    elif isinstance(s, N.AugAssign):
        em.write(f"{_expr(s.target)} {s.op} {_expr(s.value)}", line)
    elif isinstance(s, N.Return):
        if s.value is None:
            em.write("return", line)
        else:
            em.write(f"return {_expr(s.value)}", line)
    elif isinstance(s, N.Raise):
        if s.value is None:
            em.write("raise", line)
        else:
            em.write(f"raise {_expr(s.value)}", line)
    elif isinstance(s, N.Break):
        em.write("break", line)
    elif isinstance(s, N.Continue):
        em.write("continue", line)
    elif isinstance(s, N.Pass):
        em.write("pass", line)
    elif isinstance(s, N.If):
        em.write(f"if {_expr(s.cond)}:", line)
        _emit_block(em, s.body)
        for cond, body in s.elifs:
            em.write(f"elif {_expr(cond)}:", cond.loc.line if cond.loc else line)
            _emit_block(em, body)
        if s.orelse:
            em.write("else:", line)
            _emit_block(em, s.orelse)
    elif isinstance(s, N.While):
        em.write(f"while {_expr(s.cond)}:", line)
        _emit_block(em, s.body)
    elif isinstance(s, N.For):
        em.write(f"for {s.var} in {_expr(s.iterable)}:", line)
        if s.where is not None:
            em.indent_level += 1
            em.write(f"if not ({_expr(s.where)}):")
            em.indent_level += 1
            em.write("continue")
            em.indent_level -= 2
        _emit_block(em, s.body)
    elif isinstance(s, N.Each):
        _emit_each_stmt(em, s, line)
    elif isinstance(s, N.Every):
        _emit_every(em, s, line)
    elif isinstance(s, N.OnEvent):
        _emit_on_event(em, s, line)
    elif isinstance(s, N.Match):
        _emit_match(em, s, line)
    elif isinstance(s, N.Try):
        em.write("try:", line)
        _emit_block(em, s.body)
        for c in s.catches:
            head = f"except {_expr(c.exc)}"
            if c.name:
                head += f" as {c.name}"
            em.write(head + ":", c.exc.loc.line if c.exc and c.exc.loc else line)
            _emit_block(em, c.body)
        if s.finally_body:
            em.write("finally:", line)
            _emit_block(em, s.finally_body)
    elif isinstance(s, N.Serve):
        em.write(f"_c4_serve({_expr(s.handler)}, {s.port})", line)
    elif isinstance(s, N.Deploy):
        body_lambda = _block_to_lambda(s.body)
        em.write(
            f"_c4_deploy({_expr(s.handler)}, {_expr(s.target)}, {body_lambda})",
            line,
        )
    elif isinstance(s, N.FnDecl):
        _emit_fn_decl(em, s, line)
    elif isinstance(s, N.ClassDecl):
        _emit_class(em, s, line)
    elif isinstance(s, N.DataClassDecl):
        _emit_data_class(em, s, line)
    elif isinstance(s, N.DataSumDecl):
        _emit_data_sum(em, s, line)
    elif isinstance(s, N.WorkflowDecl):
        _emit_workflow(em, s, line)
    elif isinstance(s, N.ResourceDecl):
        _emit_resource(em, s, line)
    elif isinstance(s, N.Use):
        _emit_use(em, s, line)
    else:
        em.write(f"# unsupported stmt: {type(s).__name__}", line)


def _emit_block(em: _Emitter, body: list[N.Stmt]) -> None:
    if not body:
        em.indent_level += 1
        em.write("pass")
        em.indent_level -= 1
        return
    em.indent_level += 1
    for s in body:
        _emit_stmt(em, s)
    em.indent_level -= 1


def _emit_each_stmt(em: _Emitter, s: N.Each, line: int) -> None:
    if s.parallel:
        opts = _args_to_kw(s.parallel_args)
        body_fn = _block_to_lambda(s.body, params=[s.var])
        if s.where is not None:
            iterable = f"[{s.var} for {s.var} in {_expr(s.iterable)} if {_expr(s.where)}]"
        else:
            iterable = _expr(s.iterable)
        if _in_async():
            em.write(f"await _c4_async_parallel_for({iterable}, {body_fn}{opts})", line)
        else:
            em.write(f"_c4_parallel_for({iterable}, {body_fn}{opts})", line)
    else:
        em.write(f"for {s.var} in {_expr(s.iterable)}:", line)
        if s.where is not None:
            em.indent_level += 1
            em.write(f"if not ({_expr(s.where)}):")
            em.indent_level += 1
            em.write("continue")
            em.indent_level -= 2
        _emit_block(em, s.body)


def _emit_every(em: _Emitter, s: N.Every, line: int) -> None:
    body_fn = _block_to_lambda(s.body)
    em.write(f"_c4_every({s.seconds}, {body_fn})", line)


def _emit_on_event(em: _Emitter, s: N.OnEvent, line: int) -> None:
    body_fn = _block_to_lambda(s.body, params=["event"])
    em.write(f"_c4_on_event({_expr(s.source)}, {body_fn})", line)


def _emit_match(em: _Emitter, s: N.Match, line: int) -> None:
    em.write(f"match {_expr(s.subject)}:", line)
    em.indent_level += 1
    for c in s.cases:
        guard = f" if {_expr(c.guard)}" if c.guard is not None else ""
        em.write(f"case {_pattern(c.pattern)}{guard}:")
        _emit_block(em, c.body)
    em.indent_level -= 1


def _emit_fn_decl(em: _Emitter, s: N.FnDecl, line: int) -> None:
    for d in s.decorators:
        em.write(f"@{_decorator(d)}", d.loc.line if d.loc else line)
    params = _params_str(s.params)
    ret = ""
    if s.return_type is not None:
        ret = f" -> {_type(s.return_type)}"
    kw = "async def" if s.is_async else "def"
    em.write(f"{kw} {s.name}({params}){ret}:", line)

    if s.is_async:
        _enter_async()
    try:
        _emit_fn_decl_body(em, s, line)
    finally:
        if s.is_async:
            _leave_async()


def _emit_fn_decl_body(em: _Emitter, s: N.FnDecl, line: int) -> None:
    # If the body uses postfix `?`, wrap in try/except so an early
    # `Err` propagation surfaces as the function's return value.
    needs_propagate_wrap = _contains_try_propagate(s.block) or (
        s.body is not None and _expr_contains_try_propagate(s.body)
    )

    if s.block is not None:
        if needs_propagate_wrap:
            em.indent_level += 1
            em.write("try:")
            em.indent_level += 1
            for inner in s.block:
                _emit_stmt(em, inner)
            em.indent_level -= 1
            em.write("except _C4Propagate as __c4_p:")
            em.indent_level += 1
            em.write("return __c4_p.err")
            em.indent_level -= 1
            em.indent_level -= 1
        else:
            _emit_block(em, s.block)
    elif s.body is not None:
        em.indent_level += 1
        if needs_propagate_wrap:
            em.write("try:")
            em.indent_level += 1
            em.write(f"return {_expr(s.body)}")
            em.indent_level -= 1
            em.write("except _C4Propagate as __c4_p:")
            em.indent_level += 1
            em.write("return __c4_p.err")
            em.indent_level -= 1
        else:
            em.write(f"return {_expr(s.body)}")
        em.indent_level -= 1
    else:
        em.indent_level += 1
        em.write("pass")
        em.indent_level -= 1


def _expr_contains_try_propagate(e: Optional[N.Expr]) -> bool:
    if e is None:
        return False
    if isinstance(e, N.TryPropagate):
        return True
    for f in getattr(e, "__dataclass_fields__", {}):
        v = getattr(e, f, None)
        if isinstance(v, N.Expr):
            if _expr_contains_try_propagate(v):
                return True
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, N.Expr) and _expr_contains_try_propagate(x):
                    return True
                elif isinstance(x, N.Stmt) and _stmt_contains_try_propagate(x):
                    return True
                elif isinstance(x, tuple):
                    for y in x:
                        if isinstance(y, N.Expr) and _expr_contains_try_propagate(y):
                            return True
    return False


def _stmt_contains_try_propagate(s: Optional[N.Stmt]) -> bool:
    if s is None:
        return False
    for f in getattr(s, "__dataclass_fields__", {}):
        v = getattr(s, f, None)
        if isinstance(v, N.Expr):
            if _expr_contains_try_propagate(v):
                return True
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, N.Stmt) and _stmt_contains_try_propagate(x):
                    return True
                elif isinstance(x, N.Expr) and _expr_contains_try_propagate(x):
                    return True
                elif isinstance(x, tuple):
                    for y in x:
                        if isinstance(y, N.Expr) and _expr_contains_try_propagate(y):
                            return True
                        elif isinstance(y, list):
                            if _contains_try_propagate(y):
                                return True
    return False


def _contains_try_propagate(stmts: Optional[list]) -> bool:
    if stmts is None:
        return False
    return any(_stmt_contains_try_propagate(s) for s in stmts)


def _emit_class(em: _Emitter, s: N.ClassDecl, line: int) -> None:
    sup = ""
    if s.supers:
        sup = "(" + ", ".join(_type(t) for t in s.supers) + ")"
    em.write(f"class {s.name}{sup}:", line)
    if not s.body:
        em.indent_level += 1
        em.write("pass")
        em.indent_level -= 1
        return
    em.indent_level += 1
    for item in s.body:
        _emit_stmt(em, item)
    em.indent_level -= 1


def _emit_data_class(em: _Emitter, s: N.DataClassDecl, line: int) -> None:
    em.write("import dataclasses as _c4_dc", line)
    em.write("@_c4_dc.dataclass", line)
    em.write(f"class {s.name}:", line)
    if not s.fields:
        em.indent_level += 1
        em.write("pass")
        em.indent_level -= 1
        return
    em.indent_level += 1
    # Required (no default) must come before defaulted in @dataclass.
    fields_sorted = sorted(s.fields, key=lambda f: f.default is not None)
    for f in fields_sorted:
        _emit_data_field_line(em, f)
    em.indent_level -= 1


def _emit_data_sum(em: _Emitter, s: N.DataSumDecl, line: int) -> None:
    em.write("import dataclasses as _c4_dc", line)
    # Base class — empty, marker for the sum type.
    em.write(f"class {s.name}:", line)
    em.indent_level += 1
    em.write("pass")
    em.indent_level -= 1
    # Each variant is a dataclass subclass of the base.
    for v in s.variants:
        em.write("@_c4_dc.dataclass", line)
        em.write(f"class {v.name}({s.name}):", line)
        if not v.fields:
            em.indent_level += 1
            em.write("pass")
            em.indent_level -= 1
            continue
        em.indent_level += 1
        fields_sorted = sorted(v.fields, key=lambda f: f.default is not None)
        for f in fields_sorted:
            _emit_data_field_line(em, f)
        em.indent_level -= 1


_WORKFLOW_TASK_OPTS = {"retries", "timeout", "on_failure"}


def _emit_workflow(em: _Emitter, s: N.WorkflowDecl, line: int) -> None:
    em.write("import cobra4.runtime.workflow as _c4_wf_mod", line)
    wf_var = f"__c4_wf_{s.name}"
    em.write(f"{wf_var} = _c4_wf_mod.Workflow({s.name!r})", line)

    declared: set[str] = set()
    for t in s.tasks:
        if not isinstance(t.call, N.Call):
            em.write(
                f"# task '{t.var}' must be `task <call>(...)` — got "
                f"{type(t.call).__name__}",
                t.loc.line if t.loc else line,
            )
            continue

        # Split call args into "task options" (retries / timeout / ...)
        # and "real call args" (everything else).
        opts: dict[str, str] = {}
        call_args: list[N.Arg] = []
        for a in t.call.args:
            if a.name in _WORKFLOW_TASK_OPTS and not a.star and not a.dstar:
                opts[a.name] = _expr(a.value)
            else:
                call_args.append(a)

        # Determine deps: any argument expression that references a
        # previously declared task variable creates an edge.
        deps: list[str] = []
        for a in call_args:
            for name in _expr_names(a.value):
                if name in declared and name not in deps:
                    deps.append(name)

        # Build the lambda body:
        #   - Replace each dep `Name(d)` with the lambda's arg of the same name.
        #   - Render the call with original positional structure.
        rebuilt_call = N.Call(func=t.call.func, args=call_args, loc=t.call.loc)
        lambda_params = ", ".join(deps)
        body_str = _expr(rebuilt_call)
        wrapper = f"lambda {lambda_params}: {body_str}"

        opts_kw = "".join(f", {k}={v}" for k, v in opts.items())
        deps_tuple = "(" + "".join(f"{d!r}, " for d in deps) + ")"
        em.write(
            f"{wf_var}.add({t.var!r}, {wrapper}, deps={deps_tuple}{opts_kw})",
            t.loc.line if t.loc else line,
        )
        declared.add(t.var)

    # Run the workflow and bind every task name to its result so the
    # rest of the module (after the block) can use them like normal vars.
    em.write(f"{wf_var}__results = {wf_var}.run()", line)
    for name in declared:
        em.write(f"{name} = {wf_var}__results[{name!r}]", line)


def _emit_resource(em: _Emitter, s: N.ResourceDecl, line: int) -> None:
    em.write("import cobra4.runtime.infra as _c4_infra", line)
    # Build a lambda that returns the fields dict — captures any outer
    # references (including previously-declared resources) so deps wire
    # up naturally.
    body_parts = ", ".join(f"{k!r}: {_expr(v)}" for k, v in s.fields)
    fields_fn = f"lambda: {{{body_parts}}}"
    em.write(
        f"{s.name} = _c4_infra.declare_resource("
        f"{s.name!r}, {s.adapter_path!r}, {fields_fn})",
        line,
    )


def _expr_names(e: Optional[N.Expr]) -> list[str]:
    """Collect all bare ``Name`` references reachable from an expression.
    Used by the workflow codegen to compute task dependencies."""
    found: list[str] = []

    def visit(node):
        if isinstance(node, N.Name):
            found.append(node.name)
            return
        for f in getattr(node, "__dataclass_fields__", {}):
            v = getattr(node, f, None)
            if hasattr(v, "__dataclass_fields__"):
                visit(v)
            elif isinstance(v, list):
                for x in v:
                    if hasattr(x, "__dataclass_fields__"):
                        visit(x)

    visit(e)
    return found


def _emit_data_field_line(em: _Emitter, f: N.DataField) -> None:
    ann = _type(f.type_ref) if f.type_ref else "object"
    if f.default is not None:
        em.write(f"{f.name}: {ann} = {_expr(f.default)}")
    else:
        em.write(f"{f.name}: {ann}")


def _emit_use(em: _Emitter, s: N.Use, line: int) -> None:
    if s.is_string:
        # Path-based import: convert "./foo/bar" → relative path runtime call
        path_repr = repr(s.target)
        alias = s.alias or _safe_alias_from_path(s.target)
        em.write(f"{alias} = __import__('importlib').import_module({path_repr})", line)
    else:
        if s.alias:
            em.write(f"import {s.target} as {s.alias}", line)
        else:
            em.write(f"import {s.target}", line)


def _safe_alias_from_path(p: str) -> str:
    base = p.rsplit("/", 1)[-1].split(".", 1)[0]
    return "".join(c if c.isalnum() or c == "_" else "_" for c in base) or "_mod"


# ---------- Expression emission ----------


def _expr(e: Optional[N.Expr]) -> str:
    if e is None:
        return "None"
    if isinstance(e, N.Name):
        return e.name
    if isinstance(e, N.Num):
        if e.is_int:
            return str(int(e.value))
        return str(e.value)
    if isinstance(e, N.Str):
        return _emit_str(e)
    if isinstance(e, N.Bool):
        return "True" if e.value else "False"
    if isinstance(e, N.NoneLit):
        return "None"
    if isinstance(e, N.List):
        return "[" + ", ".join(_expr(x) for x in e.items) + "]"
    if isinstance(e, N.Tuple):
        if len(e.items) == 0:
            return "()"
        if len(e.items) == 1:
            return f"({_expr(e.items[0])},)"
        return "(" + ", ".join(_expr(x) for x in e.items) + ")"
    if isinstance(e, N.Dict):
        parts = []
        for k, v in e.entries:
            if k is None:
                parts.append(f"**{_expr(v)}")
            else:
                parts.append(f"{_expr(k)}: {_expr(v)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(e, N.Set):
        return "{" + ", ".join(_expr(x) for x in e.items) + "}"
    if isinstance(e, N.UnaryOp):
        op = e.op
        if op == "not":
            return f"(not {_expr(e.operand)})"
        return f"({op}{_expr(e.operand)})"
    if isinstance(e, N.BinOp):
        return f"({_expr(e.left)} {e.op} {_expr(e.right)})"
    if isinstance(e, N.BoolOp):
        sep = f" {e.op} "
        return "(" + sep.join(_expr(x) for x in e.operands) + ")"
    if isinstance(e, N.Compare):
        out = _expr(e.left)
        for op, c in zip(e.ops, e.comparators):
            out += f" {op} {_expr(c)}"
        return f"({out})"
    if isinstance(e, N.Nullish):
        # right-associative chain: a ?? b ?? c → default(default(a,b),c)? No,
        # we want: pick the first non-None. Implement left-fold via default.
        out = _expr(e.operands[0])
        for nxt in e.operands[1:]:
            out = f"_c4_default({out}, {_expr(nxt)})"
        return out
    if isinstance(e, N.Ternary):
        return f"({_expr(e.if_true)} if {_expr(e.cond)} else {_expr(e.if_false)})"
    if isinstance(e, N.Attr):
        return f"{_expr(e.target)}.{e.name}"
    if isinstance(e, N.SafeAttr):
        return f"_c4_safe_attr({_expr(e.target)}, {e.name!r})"
    if isinstance(e, N.TryPropagate):
        return f"_c4_try_propagate({_expr(e.target)})"
    if isinstance(e, N.Await):
        return f"(await {_expr(e.target)})"
    if isinstance(e, N.Index):
        return f"{_expr(e.target)}[{_subscript(e.key)}]"
    if isinstance(e, N.Call):
        return f"{_expr(e.func)}({_args(e.args)})"
    if isinstance(e, N.Lambda):
        return _lambda_to_str(e)
    if isinstance(e, N.EachExpr):
        return _each_expr_to_str(e)
    return f"<unsupported:{type(e).__name__}>"


def _subscript(e: Optional[N.Expr]) -> str:
    """Render an index key, handling Slice specially (no Python-side wrapping)."""
    if isinstance(e, N.Slice):
        a = _expr(e.start) if e.start is not None else ""
        b = _expr(e.stop) if e.stop is not None else ""
        if e.step is not None:
            return f"{a}:{b}:{_expr(e.step)}"
        return f"{a}:{b}"
    return _expr(e)


def _emit_str(e: N.Str) -> str:
    """Emit a Python string literal whose runtime value matches ``e.value``.

    For raw cobra4 strings (``r"..."``), the body is taken verbatim from
    the source. We emit a NON-raw Python string with proper escapes so
    the runtime bytes match — emitting `r"..."` would re-interpret
    backslashes wrongly (Python raw doesn't unescape `\\\\` to `\\`).
    """
    if e.is_raw:
        return repr(e.value)
    if e.has_interp:
        # Convert cobra4 "{x}" to Python f"{x}"
        return "f" + repr(e.value)
    return repr(e.value)


def _args(args: list[N.Arg]) -> str:
    parts = []
    for a in args:
        if a.dstar:
            parts.append(f"**{_expr(a.value)}")
        elif a.star:
            parts.append(f"*{_expr(a.value)}")
        elif a.name:
            parts.append(f"{a.name}={_expr(a.value)}")
        else:
            parts.append(_expr(a.value))
    return ", ".join(parts)


def _args_to_kw(args: list[N.Arg]) -> str:
    """Emit a leading ``", "`` then args (only kwargs / positionals)."""
    if not args:
        return ""
    return ", " + _args(args)


def _params_str(params: list[N.Param]) -> str:
    parts = []
    for p in params:
        if p.dstar:
            parts.append(f"**{p.name}")
            continue
        if p.star:
            parts.append(f"*{p.name}")
            continue
        s = p.name
        if p.type_ref is not None:
            s += f": {_type(p.type_ref)}"
        if p.default is not None:
            s += f"={_expr(p.default)}"
        parts.append(s)
    return ", ".join(parts)


def _type(t: Optional[N.TypeRef]) -> str:
    if t is None:
        return "object"
    if t.optional:
        return f"{t.name} | None"
    if t.args:
        return f"{t.name}[{', '.join(_type(a) for a in t.args)}]"
    return t.name


def _decorator(d: N.Decorator) -> str:
    base = d.name
    if d.has_call:
        return f"{base}({_args(d.args)})"
    return base


def _lambda_to_str(e: N.Lambda) -> str:
    params = _params_str(e.params)
    if e.body is not None:
        return f"(lambda {params}: {_expr(e.body)})"
    # Block lambda → emit a synthetic helper: `(lambda *_a, **_k: __c4_block(...))`.
    # In M1 we accept the limitation and lower to a Python lambda only if the
    # block is a single-return statement. Otherwise, fall back to def-then-name.
    if e.block and len(e.block) == 1 and isinstance(e.block[0], N.Return):
        ret = e.block[0]
        return f"(lambda {params}: {_expr(ret.value)})"
    # Generic block lambda: use a nested def via walrus is not viable in
    # expression position; we emit a comprehension-friendly inline form using
    # a tuple of side-effect statements only when none returns. M1 supports
    # the common case (single expression / single return) only.
    return f"(lambda {params}: _c4_unsupported_block_lambda())"


def _each_expr_to_str(e: N.EachExpr) -> str:
    body = e.body
    where_clause = f" if {_expr(e.where)}" if e.where is not None else ""
    if e.parallel:
        body_fn = _block_to_lambda(body, params=[e.var])
        opts = _args_to_kw(e.parallel_args)
        if e.where is not None:
            iterable = f"[{e.var} for {e.var} in {_expr(e.iterable)}{where_clause}]"
        else:
            iterable = _expr(e.iterable)
        if _in_async():
            return f"(await _c4_async_parallel_for({iterable}, {body_fn}{opts}))"
        return f"_c4_parallel_for({iterable}, {body_fn}{opts})"
    # sequential each as expression: build list-comprehension when body is a
    # single expression; else a helper.
    if len(body) == 1 and isinstance(body[0], N.ExprStmt):
        return f"[{_expr(body[0].value)} for {e.var} in {_expr(e.iterable)}{where_clause}]"
    if len(body) == 1 and isinstance(body[0], N.Return):
        return f"[{_expr(body[0].value)} for {e.var} in {_expr(e.iterable)}{where_clause}]"
    # Fallback: parallel_for with workers=1 to preserve order using a runtime
    # helper that supports this.
    body_fn = _block_to_lambda(body, params=[e.var])
    if e.where is not None:
        iterable = f"[{e.var} for {e.var} in {_expr(e.iterable)}{where_clause}]"
    else:
        iterable = _expr(e.iterable)
    return f"_c4_parallel_for({iterable}, {body_fn}, workers=1)"


def _block_to_lambda(body: list[N.Stmt], params: Optional[list[str]] = None) -> str:
    """Render a small block as a Python lambda where possible.

    M1 accepts blocks of:
      - single ``return expr``
      - single ``ExprStmt`` (returns the expression's value)
    Otherwise falls back to ``(lambda ...: _c4_run_stmts(...))`` which is
    not actually wired — the cases above cover all examples in M1.

    Special case: inside an ``async fn``, Python lambdas cannot contain
    ``await``. If the user wrote ``each ... in parallel { await coro }``
    we strip the redundant ``await`` — ``_c4_async_parallel_for`` awaits
    the returned coroutine itself.
    """
    p = ", ".join(params or [])
    if not body:
        return f"(lambda {p}: None)"
    if len(body) == 1 and isinstance(body[0], N.ExprStmt):
        return f"(lambda {p}: {_expr(_strip_await_in_async(body[0].value))})"
    if len(body) == 1 and isinstance(body[0], N.Return):
        return f"(lambda {p}: {_expr(_strip_await_in_async(body[0].value))})"
    # Fallback: a tuple of expressions (sequential). Best effort.
    exprs = []
    for st in body:
        if isinstance(st, N.ExprStmt):
            exprs.append(_expr(st.value))
        elif isinstance(st, N.Return):
            exprs.append(_expr(st.value))
        else:
            exprs.append("None")
    return f"(lambda {p}: ({', '.join(exprs)})[-1])"


def _strip_await_in_async(e: Optional[N.Expr]) -> Optional[N.Expr]:
    """If ``e`` is ``Await(inner)`` and we're inside an async fn, return
    ``inner`` — the redundant ``await`` would put a ``await`` keyword
    inside a sync ``lambda`` (a SyntaxError) and the calling helper
    (`_c4_async_parallel_for`) awaits the coroutine itself."""
    if _in_async() and isinstance(e, N.Await):
        return e.target
    return e


def _pattern(p: Optional[N.Pattern]) -> str:
    if p is None:
        return "_"
    if isinstance(p, N.PatLiteral):
        return _expr(p.value)
    if isinstance(p, N.PatName):
        return p.name
    if isinstance(p, N.PatWildcard):
        return "_"
    if isinstance(p, N.PatCall):
        inner = ", ".join(_pattern(x) for x in p.items)
        return f"{p.name}({inner})"
    if isinstance(p, N.PatList):
        return "[" + ", ".join(_pattern(x) for x in p.items) + "]"
    if isinstance(p, N.PatDict):
        parts = [f"{_expr(k)}: {_pattern(v)}" for k, v in p.entries]
        if p.rest_name:
            parts.append(f"**{p.rest_name}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(p, N.PatRest):
        prefix = "**" if p.is_double_star else "*"
        return f"{prefix}{p.name}"
    if isinstance(p, N.PatOr):
        return " | ".join(_pattern(x) for x in p.alternatives)
    if isinstance(p, N.PatTuple):
        return "(" + ", ".join(_pattern(x) for x in p.items) + ")"
    return "_"
