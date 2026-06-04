"""Static analysis of smart-dispatch registrations.

Detects two kinds of likely-runtime issues:

1. **Identical-key collision** — two registrations with the same dispatch
   key (scheme/ext/type/mime/priority). At runtime they tie, raising
   ``AmbiguousDispatch``.
2. **Specificity-overlap collision** — a more general handler and a more
   specific one share the same priority. Even though the more-specific
   wins by specificity score, this only works if the user understood that
   priority alone won't break ties — using equal priorities for handlers
   that overlap usually signals confusion, not intent. We surface it so
   the author can either bump priority or add a discriminator.

Caveats:

- We can only inspect AST-visible registrations. Library-side handlers
  (registered when imported) are not seen by this pass.
- `when=` predicates are opaque to static analysis; we flag them as
  "may overlap" with anything that shares the rest of the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from cobra4 import ast_nodes as N
from cobra4.resolver import Diagnostic

_DISPATCH_KWS = ("scheme", "ext", "type", "mime", "priority", "when")


@dataclass(frozen=True)
class _RegKey:
    scheme: Optional[str]
    ext: Optional[str]
    type_: Optional[str]
    mime: Optional[str]
    priority: int
    has_custom: bool  # True if `when=` was passed


@dataclass
class _Registration:
    fn_name: str  # e.g. "read"
    key: _RegKey
    loc: Optional[N.Loc]


def analyze(module: N.Module) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    regs: list[_Registration] = []
    _collect_regs(module.body, regs)

    groups: dict[str, list[_Registration]] = {}
    for r in regs:
        groups.setdefault(r.fn_name, []).append(r)

    for fn_name, group in groups.items():
        # 1. Identical-key collisions — same priority, same constraints.
        seen: dict[_RegKey, list[_Registration]] = {}
        for r in group:
            seen.setdefault(r.key, []).append(r)
        flagged_pairs: set[tuple[int, int]] = set()
        for key, dups in seen.items():
            if len(dups) > 1:
                locs = ", ".join(str(d.loc) for d in dups if d.loc)
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        f"smart fn '{fn_name}': {len(dups)} registrations share "
                        f"the same dispatch key {key} (locations: {locs}). "
                        f"Runtime AmbiguousDispatch likely.",
                        dups[0].loc,
                        code="D001",
                    )
                )
                for i, a in enumerate(dups):
                    for b in dups[i + 1 :]:
                        flagged_pairs.add(_pair(id(a), id(b)))

        # 2. Specificity overlap — same priority, one strictly subsumes another.
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if _pair(id(a), id(b)) in flagged_pairs:
                    continue
                if a.key.priority != b.key.priority:
                    continue
                rel = _subsume(a.key, b.key)
                if rel is None:
                    continue
                more_general, more_specific = rel
                # If both have `when=`, we can't compare statically.
                if more_general.has_custom and more_specific.has_custom:
                    note = "both use `when=`; runtime order will decide"
                else:
                    note = (
                        "more general handler outranks the specific one "
                        "only by registration order — bump priority on "
                        "the specific handler to make intent explicit"
                    )
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        f"smart fn '{fn_name}': specificity overlap at priority "
                        f"{a.key.priority} between {more_general} and "
                        f"{more_specific}. {note}",
                        a.loc,
                        code="D002",
                    )
                )

        # 3. `when=` predicates without other constraints — fully opaque,
        # will defeat the resolution cache for the whole SmartFn.
        for r in group:
            k = r.key
            if k.has_custom and not (k.scheme or k.ext or k.type_ or k.mime):
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        f"smart fn '{fn_name}': handler uses `when=` with no "
                        f"other constraints. This disables the dispatch cache "
                        f"for ALL handlers on '{fn_name}'.",
                        r.loc,
                        code="D003",
                    )
                )
    return diagnostics


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _subsume(a: _RegKey, b: _RegKey) -> Optional[tuple[_RegKey, _RegKey]]:
    """If one key strictly subsumes the other, return (general, specific).

    Subsumption: every constraint of the *general* key is None or matches
    the corresponding constraint of the *specific* key, AND the specific
    key has at least one extra constraint.
    """

    def is_general_of(g: _RegKey, s: _RegKey) -> bool:
        # For each constraint, g must be None (= "don't care") or equal to s.
        if g.scheme is not None and g.scheme != s.scheme:
            return False
        if g.ext is not None and g.ext != s.ext:
            return False
        if g.type_ is not None and g.type_ != s.type_:
            return False
        if g.mime is not None and g.mime != s.mime:
            return False
        # Must be strictly more general — at least one None where s has Some.
        return _count_constraints(g) < _count_constraints(s)

    if is_general_of(a, b):
        return a, b
    if is_general_of(b, a):
        return b, a
    return None


def _count_constraints(k: _RegKey) -> int:
    return sum(1 for v in (k.scheme, k.ext, k.type_, k.mime) if v is not None) + (
        1 if k.has_custom else 0
    )


def _collect_regs(stmts: list[N.Stmt], out: list[_Registration]) -> None:
    for s in stmts:
        _collect_in_stmt(s, out)


def _collect_in_stmt(s: N.Stmt, out: list[_Registration]) -> None:
    bodies = []
    if isinstance(s, N.If):
        bodies.append(s.body)
        for _, b in s.elifs:
            bodies.append(b)
        bodies.append(s.orelse)
    elif isinstance(s, (N.While, N.For, N.Each, N.Every)):
        bodies.append(s.body)
    elif isinstance(s, N.OnEvent):
        bodies.append(s.body)
    elif isinstance(s, N.Match):
        for c in s.cases:
            bodies.append(c.body)
    elif isinstance(s, N.Try):
        bodies.append(s.body)
        for c in s.catches:
            bodies.append(c.body)
        bodies.append(s.finally_body)
    elif isinstance(s, N.FnDecl) and s.block is not None:
        bodies.append(s.block)
    elif isinstance(s, N.ClassDecl):
        bodies.append(s.body)

    for b in bodies:
        _collect_regs(b, out)

    expr = None
    if isinstance(s, N.ExprStmt):
        expr = s.value
    elif isinstance(s, N.Assign):
        expr = s.value

    reg = _maybe_extract_register(expr)
    if reg is not None:
        out.append(reg)


def _maybe_extract_register(e: Optional[N.Expr]) -> Optional[_Registration]:
    if not isinstance(e, N.Call):
        return None
    func = e.func
    if not isinstance(func, N.Attr):
        return None
    if func.name != "register":
        return None
    if not isinstance(func.target, N.Name):
        return None
    fn_name = func.target.name

    scheme = ext = type_ = mime = None
    priority = 0
    has_custom = False
    for a in e.args:
        if a.name not in _DISPATCH_KWS:
            continue
        if a.name == "when":
            has_custom = True
            continue
        v = _const_value(a.value)
        if a.name == "scheme":
            scheme = v
        elif a.name == "ext":
            ext = v
        elif a.name == "type":
            type_ = v
        elif a.name == "mime":
            mime = v
        elif a.name == "priority" and isinstance(v, int):
            priority = v
    key = _RegKey(
        scheme=scheme,
        ext=ext,
        type_=type_,
        mime=mime,
        priority=priority,
        has_custom=has_custom,
    )
    return _Registration(fn_name=fn_name, key=key, loc=e.loc)


def _const_value(e: Optional[N.Expr]):
    if isinstance(e, N.Str):
        return e.value
    if isinstance(e, N.Num):
        return int(e.value) if e.is_int else e.value
    if isinstance(e, N.Bool):
        return e.value
    if isinstance(e, N.Name):
        return e.name
    return None
