"""Gradual type checker for cobra4 (M2).

Goals:

- Honor explicit annotations (``x: int = 1``, ``fn f(a: str) -> int``).
- Infer types of literals and a few obvious expressions.
- Emit *warnings* (not errors) on mismatches — cobra4 is dynamic-friendly,
  the user can still run code with type warnings.
- Keep the implementation small: we don't unify, we don't track flow, we
  just propagate the "known" types one expression at a time.

What this catches:

- ``y: int = "hi"`` (annotation vs literal mismatch)
- ``f("hi")`` when ``f`` is declared as ``fn f(a: int) -> int``
- ``x = 1; x = "hi"`` does NOT warn — cobra4 keeps Python's rebinding semantics.

What this does NOT do (M3+):

- Generics, unions, full type unification.
- Track types across method calls / attribute access on user types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cobra4 import ast_nodes as N
from cobra4.resolver import Diagnostic


# ---------- Type representation ----------


@dataclass(frozen=True)
class C4Type:
    name: str
    args: tuple = ()
    optional: bool = False

    def __str__(self) -> str:
        out = self.name
        if self.args:
            out += "[" + ", ".join(str(a) for a in self.args) + "]"
        if self.optional:
            out += "?"
        return out


ANY_T = C4Type("Any")
INT_T = C4Type("int")
FLOAT_T = C4Type("float")
STR_T = C4Type("str")
BOOL_T = C4Type("bool")
NONE_T = C4Type("None")
LIST_T = C4Type("list")
DICT_T = C4Type("dict")
SET_T = C4Type("set")
TUPLE_T = C4Type("tuple")
CALLABLE_T = C4Type("Callable")


# ---------- Compatibility ----------


def _compat(declared: C4Type, actual: C4Type) -> bool:
    """Is ``actual`` assignable to ``declared``?

    M2 is intentionally loose:
    - ``Any`` is compatible with anything (and vice versa).
    - ``int`` and ``float`` are bidirectionally compatible (Python coerces).
    - same name → ok.
    - ``None`` matches optional types.
    """
    if declared.name == "Any" or actual.name == "Any":
        return True
    if declared.optional and actual.name == "None":
        return True
    if declared.name == actual.name:
        return True
    numeric = {"int", "float"}
    if declared.name in numeric and actual.name in numeric:
        return True
    return False


# ---------- Type checker ----------


@dataclass
class _FnSig:
    params: list[tuple[str, C4Type]] = field(default_factory=list)
    return_type: C4Type = ANY_T


class TypeChecker:
    def __init__(self) -> None:
        self.diagnostics: list[Diagnostic] = []
        self.var_types: dict[str, C4Type] = {}
        self.fn_sigs: dict[str, _FnSig] = {}

    def check(self, module: N.Module) -> list[Diagnostic]:
        # Pass 1: collect function signatures so call sites can be checked
        # regardless of declaration order.
        for s in module.body:
            self._collect_fn(s)
        # Pass 2: walk module
        for s in module.body:
            self._visit_stmt(s)
        return self.diagnostics

    # ---------- collection ----------

    def _collect_fn(self, s: N.Stmt) -> None:
        if isinstance(s, N.FnDecl):
            params = []
            for p in s.params:
                params.append((p.name, _type_ref_to_t(p.type_ref)))
            ret = _type_ref_to_t(s.return_type)
            self.fn_sigs[s.name] = _FnSig(params=params, return_type=ret)

    # ---------- helpers ----------

    def _warn(self, msg: str, loc: Optional[N.Loc], code: str = "T001") -> None:
        self.diagnostics.append(Diagnostic("warning", msg, loc, code))

    def _record_var(self, name: str, t: C4Type) -> None:
        if t.name != "Any":
            self.var_types[name] = t

    # ---------- statements ----------

    def _visit_stmts(self, body: list[N.Stmt]) -> None:
        for s in body:
            self._visit_stmt(s)

    def _visit_stmt(self, s: N.Stmt) -> None:
        if isinstance(s, N.Assign):
            actual = self._infer(s.value)
            for t in s.targets:
                if isinstance(t, N.Name):
                    declared = self.var_types.get(t.name)
                    if declared is not None and not _compat(declared, actual):
                        self._warn(
                            f"type mismatch: '{t.name}' was {declared}, assigned {actual}",
                            s.loc,
                            "T002",
                        )
                    self._record_var(t.name, actual)
        elif isinstance(s, N.AugAssign):
            self._infer(s.value)
        elif isinstance(s, N.ExprStmt):
            self._infer(s.value)
        elif isinstance(s, N.Return):
            self._infer(s.value)
        elif isinstance(s, N.Raise):
            self._infer(s.value)
        elif isinstance(s, N.If):
            self._infer(s.cond)
            self._visit_stmts(s.body)
            for cond, body in s.elifs:
                self._infer(cond)
                self._visit_stmts(body)
            self._visit_stmts(s.orelse)
        elif isinstance(s, N.While):
            self._infer(s.cond)
            self._visit_stmts(s.body)
        elif isinstance(s, (N.For, N.Each)):
            self._infer(s.iterable)
            self._visit_stmts(s.body)
        elif isinstance(s, N.Every):
            self._visit_stmts(s.body)
        elif isinstance(s, N.OnEvent):
            self._infer(s.source)
            self._visit_stmts(s.body)
        elif isinstance(s, N.Match):
            self._infer(s.subject)
            for c in s.cases:
                self._visit_stmts(c.body)
        elif isinstance(s, N.Try):
            self._visit_stmts(s.body)
            for c in s.catches:
                self._infer(c.exc)
                self._visit_stmts(c.body)
            self._visit_stmts(s.finally_body)
        elif isinstance(s, N.Serve):
            self._infer(s.handler)
        elif isinstance(s, N.Deploy):
            self._infer(s.handler)
            self._infer(s.target)
            self._visit_stmts(s.body)
        elif isinstance(s, N.FnDecl):
            saved = dict(self.var_types)
            for p in s.params:
                t = _type_ref_to_t(p.type_ref)
                self._record_var(p.name, t)
                if p.default is not None:
                    actual = self._infer(p.default)
                    if t.name != "Any" and not _compat(t, actual):
                        self._warn(
                            f"default for parameter '{p.name}' is {actual}, declared {t}",
                            p.loc,
                            "T003",
                        )
            if s.body is not None:
                actual = self._infer(s.body)
                ret = _type_ref_to_t(s.return_type)
                if ret.name != "Any" and not _compat(ret, actual):
                    self._warn(
                        f"function '{s.name}' returns {actual}, declared {ret}",
                        s.loc,
                        "T004",
                    )
            if s.block is not None:
                self._visit_stmts(s.block)
            self.var_types = saved
        elif isinstance(s, N.ClassDecl):
            self._visit_stmts(s.body)
        # Use: nothing to check at type level in M2

    # ---------- expression inference ----------

    def _infer(self, e: Optional[N.Expr]) -> C4Type:
        if e is None:
            return NONE_T
        if isinstance(e, N.Num):
            return INT_T if e.is_int else FLOAT_T
        if isinstance(e, N.Str):
            return STR_T
        if isinstance(e, N.Bool):
            return BOOL_T
        if isinstance(e, N.NoneLit):
            return NONE_T
        if isinstance(e, N.List):
            for it in e.items:
                self._infer(it)
            return LIST_T
        if isinstance(e, N.Tuple):
            for it in e.items:
                self._infer(it)
            return TUPLE_T
        if isinstance(e, N.Dict):
            for k, v in e.entries:
                self._infer(k)
                self._infer(v)
            return DICT_T
        if isinstance(e, N.Set):
            for it in e.items:
                self._infer(it)
            return SET_T
        if isinstance(e, N.Name):
            return self.var_types.get(e.name, ANY_T)
        if isinstance(e, N.UnaryOp):
            inner = self._infer(e.operand)
            if e.op in ("+", "-"):
                return inner if inner.name in ("int", "float") else ANY_T
            if e.op == "not":
                return BOOL_T
            return inner
        if isinstance(e, N.BinOp):
            l = self._infer(e.left)
            r = self._infer(e.right)
            if e.op in ("+", "-", "*", "/", "//", "%", "**"):
                if l.name == "str" and r.name == "str":
                    return STR_T
                if l.name == "float" or r.name == "float":
                    return FLOAT_T
                if l.name == "int" and r.name == "int":
                    return INT_T if e.op != "/" else FLOAT_T
            return ANY_T
        if isinstance(e, N.BoolOp):
            for op in e.operands:
                self._infer(op)
            return BOOL_T
        if isinstance(e, N.Compare):
            self._infer(e.left)
            for c in e.comparators:
                self._infer(c)
            return BOOL_T
        if isinstance(e, N.Nullish):
            t = ANY_T
            for op in e.operands:
                t = self._infer(op)
            return t
        if isinstance(e, N.Ternary):
            self._infer(e.cond)
            self._infer(e.if_true)
            self._infer(e.if_false)
            return ANY_T
        if isinstance(e, (N.Attr, N.SafeAttr)):
            self._infer(e.target)
            return ANY_T
        if isinstance(e, N.Index):
            self._infer(e.target)
            self._infer(e.key)
            return ANY_T
        if isinstance(e, N.Call):
            return self._check_call(e)
        if isinstance(e, N.Lambda):
            for p in e.params:
                self._record_var(p.name, _type_ref_to_t(p.type_ref))
            if e.body is not None:
                return self._infer(e.body)
            if e.block:
                # last return statement, best-effort
                for st in reversed(e.block):
                    if isinstance(st, N.Return) and st.value is not None:
                        return self._infer(st.value)
            return ANY_T
        if isinstance(e, N.EachExpr):
            self._infer(e.iterable)
            for s in e.body:
                self._visit_stmt(s)
            return LIST_T
        return ANY_T

    def _check_call(self, e: N.Call) -> C4Type:
        for a in e.args:
            self._infer(a.value)
        if isinstance(e.func, N.Name):
            sig = self.fn_sigs.get(e.func.name)
            if sig is None:
                return ANY_T
            # Check positional argument types against declared params.
            pos = [a for a in e.args if a.name is None and not a.star and not a.dstar]
            for i, arg in enumerate(pos):
                if i >= len(sig.params):
                    break
                pname, ptype = sig.params[i]
                actual = self._infer(arg.value)
                if ptype.name != "Any" and not _compat(ptype, actual):
                    self._warn(
                        f"argument {i + 1} ('{pname}') of '{e.func.name}': "
                        f"declared {ptype}, got {actual}",
                        arg.loc,
                        "T005",
                    )
            for arg in e.args:
                if arg.name is None:
                    continue
                # find param by name
                for pname, ptype in sig.params:
                    if pname == arg.name:
                        actual = self._infer(arg.value)
                        if ptype.name != "Any" and not _compat(ptype, actual):
                            self._warn(
                                f"keyword argument '{arg.name}' of "
                                f"'{e.func.name}': declared {ptype}, got {actual}",
                                arg.loc,
                                "T005",
                            )
                        break
            return sig.return_type
        return ANY_T


def _type_ref_to_t(t: Optional[N.TypeRef]) -> C4Type:
    if t is None:
        return ANY_T
    return C4Type(name=t.name, args=tuple(_type_ref_to_t(a) for a in t.args), optional=t.optional)


def check(module: N.Module) -> list[Diagnostic]:
    return TypeChecker().check(module)
