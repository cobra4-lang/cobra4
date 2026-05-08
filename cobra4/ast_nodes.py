"""AST node definitions for cobra4.

Every node carries an optional ``loc`` (line, column) so error messages and
source maps can point back to the source. The transformer in
``cobra4.parser`` populates these.

Nodes are kept small and orthogonal — sugar like ``each ... in parallel``
and ``?.`` is preserved here and lowered in ``cobra4.lowering``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------- Position ----------


@dataclass(frozen=True)
class Loc:
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.line}:{self.column}"


# ---------- Base ----------


@dataclass
class Node:
    loc: Optional[Loc] = field(default=None, kw_only=True)


# ---------- Module / top level ----------


@dataclass
class Module(Node):
    body: list["Stmt"] = field(default_factory=list)


# ---------- Expressions ----------


@dataclass
class Expr(Node):
    pass


@dataclass
class Name(Expr):
    name: str = ""


@dataclass
class Num(Expr):
    value: float = 0.0
    is_int: bool = True


@dataclass
class Str(Expr):
    value: str = ""
    is_raw: bool = False
    has_interp: bool = False  # contains `{...}` interpolations


@dataclass
class Bool(Expr):
    value: bool = False


@dataclass
class NoneLit(Expr):
    pass


@dataclass
class List(Expr):
    items: list[Expr] = field(default_factory=list)


@dataclass
class Tuple(Expr):
    items: list[Expr] = field(default_factory=list)


@dataclass
class Dict(Expr):
    # Each entry is (key, value) where key=None means **spread.
    entries: list[tuple[Optional[Expr], Expr]] = field(default_factory=list)


@dataclass
class Set(Expr):
    items: list[Expr] = field(default_factory=list)


@dataclass
class UnaryOp(Expr):
    op: str = ""
    operand: Optional[Expr] = None


@dataclass
class BinOp(Expr):
    op: str = ""
    left: Optional[Expr] = None
    right: Optional[Expr] = None


@dataclass
class BoolOp(Expr):
    op: str = ""  # "and" / "or"
    operands: list[Expr] = field(default_factory=list)


@dataclass
class Compare(Expr):
    left: Optional[Expr] = None
    ops: list[str] = field(default_factory=list)
    comparators: list[Expr] = field(default_factory=list)


@dataclass
class Nullish(Expr):
    """``a ?? b ?? c`` — left-to-right defaulting."""

    operands: list[Expr] = field(default_factory=list)


@dataclass
class Ternary(Expr):
    cond: Optional[Expr] = None
    if_true: Optional[Expr] = None
    if_false: Optional[Expr] = None


@dataclass
class Attr(Expr):
    target: Optional[Expr] = None
    name: str = ""


@dataclass
class SafeAttr(Expr):
    """``target?.name`` — None-safe attribute access."""

    target: Optional[Expr] = None
    name: str = ""


@dataclass
class Await(Expr):
    """``await EXPR`` — only legal inside an ``async fn``."""

    target: Optional[Expr] = None


@dataclass
class TryPropagate(Expr):
    """``expr?`` — postfix Result propagation operator.

    If the expression evaluates to ``Ok(v)``, the whole expression
    evaluates to ``v``. If it evaluates to ``Err(e)``, the enclosing
    function early-returns with that ``Err``. See
    :mod:`cobra4.runtime.result` for the runtime contract.
    """

    target: Optional[Expr] = None


@dataclass
class Index(Expr):
    target: Optional[Expr] = None
    key: Optional[Expr] = None


@dataclass
class Slice(Expr):
    """A slice expression: ``a:b:c``. Any field may be ``None``."""

    start: Optional[Expr] = None
    stop: Optional[Expr] = None
    step: Optional[Expr] = None


@dataclass
class Arg(Node):
    """Positional or keyword argument in a call.

    - ``name=None``: positional
    - ``name="x"``: keyword
    - ``star=True``: ``*expr`` splat
    - ``dstar=True``: ``**expr`` splat
    """

    value: Optional[Expr] = None
    name: Optional[str] = None
    star: bool = False
    dstar: bool = False


@dataclass
class Call(Expr):
    func: Optional[Expr] = None
    args: list[Arg] = field(default_factory=list)


@dataclass
class Lambda(Expr):
    params: list["Param"] = field(default_factory=list)
    return_type: Optional["TypeRef"] = None
    body: Optional[Expr] = None         # for `=` form
    block: Optional[list["Stmt"]] = None  # for `{ ... }` form


@dataclass
class EachExpr(Expr):
    var: str = ""
    iterable: Optional[Expr] = None
    parallel: bool = False
    parallel_args: list[Arg] = field(default_factory=list)
    where: Optional["Expr"] = None
    body: list["Stmt"] = field(default_factory=list)


# ---------- Statements ----------


@dataclass
class Stmt(Node):
    pass


@dataclass
class ExprStmt(Stmt):
    value: Optional[Expr] = None


@dataclass
class Assign(Stmt):
    targets: list[Expr] = field(default_factory=list)  # validated lvalues
    value: Optional[Expr] = None


@dataclass
class AugAssign(Stmt):
    target: Optional[Expr] = None
    op: str = ""  # the AUG_OP, e.g. "+="
    value: Optional[Expr] = None


@dataclass
class Return(Stmt):
    value: Optional[Expr] = None


@dataclass
class Raise(Stmt):
    value: Optional[Expr] = None


@dataclass
class Break(Stmt):
    pass


@dataclass
class Continue(Stmt):
    pass


@dataclass
class Pass(Stmt):
    pass


@dataclass
class If(Stmt):
    cond: Optional[Expr] = None
    body: list[Stmt] = field(default_factory=list)
    elifs: list[tuple[Expr, list[Stmt]]] = field(default_factory=list)
    orelse: list[Stmt] = field(default_factory=list)


@dataclass
class While(Stmt):
    cond: Optional[Expr] = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class For(Stmt):
    var: str = ""
    iterable: Optional[Expr] = None
    where: Optional[Expr] = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Each(Stmt):
    var: str = ""
    iterable: Optional[Expr] = None
    parallel: bool = False
    parallel_args: list[Arg] = field(default_factory=list)
    where: Optional[Expr] = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Every(Stmt):
    seconds: float = 0.0
    body: list[Stmt] = field(default_factory=list)


@dataclass
class OnEvent(Stmt):
    source: Optional[Expr] = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Match(Stmt):
    subject: Optional[Expr] = None
    cases: list["Case"] = field(default_factory=list)


@dataclass
class Case(Node):
    pattern: Optional["Pattern"] = None
    guard: Optional[Expr] = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Pattern(Node):
    pass


@dataclass
class PatLiteral(Pattern):
    value: Optional[Expr] = None  # Num, Str, Bool, NoneLit


@dataclass
class PatName(Pattern):
    name: str = ""


@dataclass
class PatWildcard(Pattern):
    pass


@dataclass
class PatCall(Pattern):
    name: str = ""
    items: list[Pattern] = field(default_factory=list)


@dataclass
class PatList(Pattern):
    items: list[Pattern] = field(default_factory=list)


@dataclass
class PatRest(Pattern):
    """Rest pattern: ``*name`` inside list patterns, ``**name`` inside dicts."""

    name: str = ""
    is_double_star: bool = False


@dataclass
class PatDict(Pattern):
    """Dict pattern: ``{"key": pattern, **rest}``.

    ``entries`` is a list of (key_expr, value_pattern). Keys are
    constant expressions (Str / Num).
    """

    entries: list[tuple[Optional["Expr"], Pattern]] = field(default_factory=list)
    rest_name: Optional[str] = None  # captured by ``**rest`` if present


@dataclass
class PatOr(Pattern):
    """Pattern alternation: ``case 1 | 2 | 3 { ... }``."""

    alternatives: list[Pattern] = field(default_factory=list)


@dataclass
class PatTuple(Pattern):
    """Tuple pattern: ``case (a, b) { ... }`` (also matches sequences in Python)."""

    items: list[Pattern] = field(default_factory=list)


@dataclass
class Try(Stmt):
    body: list[Stmt] = field(default_factory=list)
    catches: list["Catch"] = field(default_factory=list)
    finally_body: list[Stmt] = field(default_factory=list)


@dataclass
class Catch(Node):
    exc: Optional[Expr] = None
    name: Optional[str] = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Serve(Stmt):
    handler: Optional[Expr] = None
    port: int = 0


@dataclass
class Deploy(Stmt):
    handler: Optional[Expr] = None
    target: Optional[Expr] = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Use(Stmt):
    target: str = ""             # dotted name or string path
    is_string: bool = False      # True if `use "path"`
    alias: Optional[str] = None


@dataclass
class FnDecl(Stmt):
    name: str = ""
    params: list["Param"] = field(default_factory=list)
    return_type: Optional["TypeRef"] = None
    body: Optional[Expr] = None         # `=` form
    block: Optional[list[Stmt]] = None  # `{ ... }` form
    decorators: list["Decorator"] = field(default_factory=list)
    is_async: bool = False              # `async fn name() { ... }`


@dataclass
class Decorator(Node):
    name: str = ""           # dotted, e.g. "smart" or "obs.trace"
    args: list[Arg] = field(default_factory=list)
    has_call: bool = False


@dataclass
class Param(Node):
    name: str = ""
    type_ref: Optional["TypeRef"] = None
    default: Optional[Expr] = None
    star: bool = False    # *args
    dstar: bool = False   # **kwargs


@dataclass
class TypeRef(Node):
    name: str = ""
    args: list["TypeRef"] = field(default_factory=list)
    optional: bool = False


@dataclass
class ClassDecl(Stmt):
    name: str = ""
    supers: list[TypeRef] = field(default_factory=list)
    body: list[Stmt] = field(default_factory=list)


# ---------- Data classes & sum types ----------


@dataclass
class DataField(Node):
    name: str = ""
    type_ref: Optional[TypeRef] = None
    default: Optional[Expr] = None


@dataclass
class DataClassDecl(Stmt):
    """``data class Point(x: int, y: int = 0)``.

    Codegen produces a stdlib ``@dataclass`` with the listed fields.
    """
    name: str = ""
    fields: list[DataField] = field(default_factory=list)


@dataclass
class DataVariant(Node):
    name: str = ""
    fields: list[DataField] = field(default_factory=list)


@dataclass
class DataSumDecl(Stmt):
    """``data Event { OrderPlaced(id: str) ... }``.

    Sum type — base ``Event`` is empty, each variant is a dataclass
    subclass. Pattern matching (``match e { case OrderPlaced(id) ... }``)
    binds positional fields by declaration order.
    """
    name: str = ""
    variants: list[DataVariant] = field(default_factory=list)
