"""Canonical formatter for cobra4 — emits stable cobra4 source from the AST.

Style:
- 4-space indent
- Opening ``{`` on the same line as the keyword/signature (K&R-ish)
- One blank line between top-level declarations
- ``,`` after every list/dict/tuple element (trailing OK)
- Operators surrounded by spaces; no space inside ``()`` ``[]`` ``{}``
- ``log("hi", n=3)`` — no space before ``=`` in keyword args
- ``fn name(a, b) { ... }`` — block bodies use newlines

Output is deterministic; running ``c4 fmt`` twice is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cobra4 import ast_nodes as N


@dataclass
class _F:
    indent: int = 0
    out: list[str] = field(default_factory=list)

    def line(self, s: str = "") -> None:
        prefix = "    " * self.indent
        self.out.append((prefix + s) if s else "")

    def write_inline(self, s: str) -> None:
        if not self.out:
            self.out.append(s)
            return
        self.out[-1] += s

    def text(self) -> str:
        # Strip trailing blank lines; ensure exactly one trailing newline.
        while self.out and not self.out[-1].strip():
            self.out.pop()
        return "\n".join(self.out) + "\n"


def format_module(module: N.Module) -> str:
    f = _F()
    prev_kind: Optional[type] = None
    for s in module.body:
        # Insert blank line before fn/class declarations (after the first).
        if prev_kind is not None and isinstance(s, (N.FnDecl, N.ClassDecl)):
            f.line()
        _stmt(f, s)
        prev_kind = type(s)
    return f.text()


# ---------- Statements ----------


def _stmt(f: _F, s: N.Stmt) -> None:
    if isinstance(s, N.Pass):
        f.line("pass")
    elif isinstance(s, N.Break):
        f.line("break")
    elif isinstance(s, N.Continue):
        f.line("continue")
    elif isinstance(s, N.Return):
        f.line("return" if s.value is None else f"return {_expr(s.value)}")
    elif isinstance(s, N.Raise):
        f.line("raise" if s.value is None else f"raise {_expr(s.value)}")
    elif isinstance(s, N.ExprStmt):
        f.line(_expr(s.value))
    elif isinstance(s, N.Assign):
        targets = ", ".join(_expr(t) for t in s.targets)
        f.line(f"{targets} = {_expr(s.value)}")
    elif isinstance(s, N.AugAssign):
        f.line(f"{_expr(s.target)} {s.op} {_expr(s.value)}")
    elif isinstance(s, N.If):
        _if_stmt(f, s)
    elif isinstance(s, N.While):
        f.line(f"while {_expr(s.cond)} {{")
        _block(f, s.body)
        f.line("}")
    elif isinstance(s, N.For):
        head = f"for {s.var} in {_expr(s.iterable)}"
        if s.where is not None:
            head += f" where {_expr(s.where)}"
        f.line(head + " {")
        _block(f, s.body)
        f.line("}")
    elif isinstance(s, N.Each):
        head = f"each {s.var} in {_expr(s.iterable)}"
        if s.parallel:
            opts = _arg_list(s.parallel_args)
            head += f" in parallel({opts})" if opts else " in parallel"
        if s.where is not None:
            head += f" where {_expr(s.where)}"
        f.line(head + " {")
        _block(f, s.body)
        f.line("}")
    elif isinstance(s, N.Every):
        f.line(f"every {_duration(s.seconds)} {{")
        _block(f, s.body)
        f.line("}")
    elif isinstance(s, N.OnEvent):
        f.line(f"on event from {_expr(s.source)} {{")
        _block(f, s.body)
        f.line("}")
    elif isinstance(s, N.Match):
        f.line(f"match {_expr(s.subject)} {{")
        f.indent += 1
        for c in s.cases:
            head = f"case {_pattern(c.pattern)}"
            if c.guard is not None:
                head += f" if {_expr(c.guard)}"
            f.line(head + " {")
            _block(f, c.body)
            f.line("}")
        f.indent -= 1
        f.line("}")
    elif isinstance(s, N.Try):
        f.line("try {")
        _block(f, s.body)
        f.line("}")
        for c in s.catches:
            head = f"catch {_expr(c.exc)}"
            if c.name:
                head += f" as {c.name}"
            f.line(head + " {")
            _block(f, c.body)
            f.line("}")
        if s.finally_body:
            f.line("finally {")
            _block(f, s.finally_body)
            f.line("}")
    elif isinstance(s, N.Serve):
        f.line(f"serve {_expr(s.handler)} on :{s.port}")
    elif isinstance(s, N.Deploy):
        head = f"deploy {_expr(s.handler)} to {_expr(s.target)}"
        if s.body:
            f.line(head + " {")
            _block(f, s.body)
            f.line("}")
        else:
            f.line(head)
    elif isinstance(s, N.Use):
        target = f'"{s.target}"' if s.is_string else s.target
        f.line(f"use {target}" + (f" as {s.alias}" if s.alias else ""))
    elif isinstance(s, N.FnDecl):
        _fn_decl(f, s)
    elif isinstance(s, N.ClassDecl):
        sup = "(" + ", ".join(_type(t) for t in s.supers) + ")" if s.supers else ""
        f.line(f"class {s.name}{sup} {{")
        _block(f, s.body)
        f.line("}")


def _if_stmt(f: _F, s: N.If) -> None:
    f.line(f"if {_expr(s.cond)} {{")
    _block(f, s.body)
    f.line("}")
    for cond, body in s.elifs:
        f.line(f"elif {_expr(cond)} {{")
        _block(f, body)
        f.line("}")
    if s.orelse:
        f.line("else {")
        _block(f, s.orelse)
        f.line("}")


def _block(f: _F, body: list[N.Stmt]) -> None:
    if not body:
        f.indent += 1
        f.line("pass")
        f.indent -= 1
        return
    f.indent += 1
    for s in body:
        _stmt(f, s)
    f.indent -= 1


def _fn_decl(f: _F, s: N.FnDecl) -> None:
    for d in s.decorators:
        head = f"@{d.name}"
        if d.has_call:
            head += f"({_arg_list(d.args)})"
        f.line(head)
    params = ", ".join(_param(p) for p in s.params)
    ret = f" -> {_type(s.return_type)}" if s.return_type is not None else ""
    head = f"fn {s.name}({params}){ret}"
    if s.body is not None:
        f.line(f"{head} = {_expr(s.body)}")
        return
    f.line(head + " {")
    _block(f, s.block or [])
    f.line("}")


def _param(p: N.Param) -> str:
    s = p.name
    if p.type_ref is not None:
        s += f": {_type(p.type_ref)}"
    if p.default is not None:
        s += f" = {_expr(p.default)}"
    return s


def _type(t: Optional[N.TypeRef]) -> str:
    if t is None:
        return "Any"
    base = t.name + ("?" if t.optional else "")
    if t.args:
        return base + "[" + ", ".join(_type(a) for a in t.args) + "]"
    return base


def _arg_list(args: list[N.Arg]) -> str:
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


def _duration(seconds: float) -> str:
    if seconds % 86400 == 0:
        return f"{int(seconds // 86400)} days"
    if seconds % 3600 == 0:
        return f"{int(seconds // 3600)} hours"
    if seconds % 60 == 0:
        return f"{int(seconds // 60)} minutes"
    return f"{seconds:g} seconds"


# ---------- Expressions ----------


_PRECEDENCE = {
    "or": 1,
    "and": 2,
    "not": 3,
    "compare": 4,
    "??": 5,
    "|": 6,
    "^": 7,
    "&": 8,
    "<<": 9,
    ">>": 9,
    "+": 10,
    "-": 10,
    "*": 11,
    "/": 11,
    "//": 11,
    "%": 11,
    "**": 13,
    "unary": 12,
}


def _expr(e: Optional[N.Expr]) -> str:
    if e is None:
        return "None"
    if isinstance(e, N.Name):
        return e.name
    if isinstance(e, N.Num):
        return str(int(e.value)) if e.is_int else str(e.value)
    if isinstance(e, N.Str):
        return _format_string(e)
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
        if e.op == "not":
            return f"not {_expr(e.operand)}"
        return f"{e.op}{_expr(e.operand)}"
    if isinstance(e, N.BinOp):
        return f"{_expr(e.left)} {e.op} {_expr(e.right)}"
    if isinstance(e, N.BoolOp):
        return f" {e.op} ".join(_expr(x) for x in e.operands)
    if isinstance(e, N.Compare):
        out = _expr(e.left)
        for op, c in zip(e.ops, e.comparators):
            out += f" {op} {_expr(c)}"
        return out
    if isinstance(e, N.Nullish):
        return " ?? ".join(_expr(x) for x in e.operands)
    if isinstance(e, N.Ternary):
        return f"{_expr(e.if_true)} if {_expr(e.cond)} else {_expr(e.if_false)}"
    if isinstance(e, N.Attr):
        return f"{_expr(e.target)}.{e.name}"
    if isinstance(e, N.SafeAttr):
        return f"{_expr(e.target)}?.{e.name}"
    if isinstance(e, N.Index):
        return f"{_expr(e.target)}[{_subscript(e.key)}]"
    if isinstance(e, N.Slice):  # rare top-level use
        return _subscript(e)
    if isinstance(e, N.Call):
        return f"{_expr(e.func)}({_arg_list(e.args)})"
    if isinstance(e, N.Lambda):
        params = ", ".join(_param(p) for p in e.params)
        if e.body is not None:
            return f"fn({params}) = {_expr(e.body)}"
        return f"fn({params}) {{ ... }}"
    if isinstance(e, N.EachExpr):
        head = f"each {e.var} in {_expr(e.iterable)}"
        if e.parallel:
            head += " in parallel"
        if e.where is not None:
            head += f" where {_expr(e.where)}"
        # one-line body if simple
        if len(e.body) == 1 and isinstance(e.body[0], N.ExprStmt):
            return f"{head} {{ {_expr(e.body[0].value)} }}"
        return f"{head} {{ ... }}"
    return f"<{type(e).__name__}>"


def _subscript(e: Optional[N.Expr]) -> str:
    if isinstance(e, N.Slice):
        a = _expr(e.start) if e.start is not None else ""
        b = _expr(e.stop) if e.stop is not None else ""
        if e.step is not None:
            return f"{a}:{b}:{_expr(e.step)}"
        return f"{a}:{b}"
    return _expr(e)


def _format_string(e: N.Str) -> str:
    if e.is_raw:
        return f'r"{e.value}"'
    # cobra4's strings are interpolating by default; escape literal `{`/`}`.
    body = e.value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{body}"'


# ---------- Patterns ----------


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
        return f"{p.name}(" + ", ".join(_pattern(x) for x in p.items) + ")"
    if isinstance(p, N.PatList):
        return "[" + ", ".join(_pattern(x) for x in p.items) + "]"
    if isinstance(p, N.PatOr):
        return " | ".join(_pattern(x) for x in p.alternatives)
    return "_"
