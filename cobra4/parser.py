"""Parser glue: lark parse tree → cobra4 AST.

The transformer maps each grammar rule to an AST node. Several rules
have multiple alternatives (e.g. ``dict_or_set``); each alternative gets
its own method via the ``-> name`` annotations in the grammar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from lark import Token, Transformer, v_args
from lark.exceptions import UnexpectedInput

from cobra4 import ast_nodes as N
from cobra4.lexer import get_parser


# ---------- Errors ----------


@dataclass
class ParseError(Exception):
    message: str
    line: int
    column: int
    source_path: Optional[str] = None
    snippet: Optional[str] = None
    hint: Optional[str] = None

    def __str__(self) -> str:
        loc = f"{self.source_path or '<input>'}:{self.line}:{self.column}"
        out = [f"error: {self.message}", f"  --> {loc}"]
        if self.snippet:
            out.append(f"   |")
            out.append(f"{self.line:>4} | {self.snippet}")
            out.append(f"   | {' ' * (self.column - 1)}^")
        if self.hint:
            out.append(f"help: {self.hint}")
        return "\n".join(out)


# ---------- Transformer ----------


def _loc(meta) -> Optional[N.Loc]:
    if meta is None or getattr(meta, "empty", True):
        return None
    return N.Loc(line=meta.line, column=meta.column)


def _tok_loc(tok: Token) -> N.Loc:
    return N.Loc(line=tok.line or 0, column=tok.column or 0)


@v_args(meta=True)
class _Transformer(Transformer):
    # ---------- Top level ----------

    def start(self, meta, children):
        body = [c for c in children if isinstance(c, N.Stmt)]
        return N.Module(body=body, loc=_loc(meta))

    # ---------- Simple statements ----------

    def assign_or_expr(self, meta, children):
        head = children[0]
        if len(children) == 1:
            return N.ExprStmt(value=head, loc=_loc(meta))
        tail = children[1]
        if isinstance(tail, _AssignTail):
            if tail.kind == "eq":
                return N.Assign(targets=[head], value=tail.value, loc=_loc(meta))
            return N.AugAssign(target=head, op=tail.op, value=tail.value, loc=_loc(meta))
        return head  # shouldn't happen

    def assign_tail_eq(self, meta, children):
        return _AssignTail(kind="eq", op="=", value=children[0])

    def assign_tail_aug(self, meta, children):
        op_tok, val = children
        return _AssignTail(kind="aug", op=str(op_tok), value=val)

    def return_stmt(self, meta, children):
        return N.Return(value=(children[0] if children else None), loc=_loc(meta))

    def raise_stmt(self, meta, children):
        return N.Raise(value=(children[0] if children else None), loc=_loc(meta))

    def break_stmt(self, meta, children):
        return N.Break(loc=_loc(meta))

    def continue_stmt(self, meta, children):
        return N.Continue(loc=_loc(meta))

    def pass_stmt(self, meta, children):
        return N.Pass(loc=_loc(meta))

    # ---------- Compound ----------

    def if_stmt(self, meta, children):
        # children: [cond, body_block, (cond, body_block)*, ?else_block]
        cond = children[0]
        body = children[1].statements
        elifs: list[tuple[N.Expr, list[N.Stmt]]] = []
        orelse: list[N.Stmt] = []
        i = 2
        while i + 1 < len(children) and isinstance(children[i], N.Expr):
            elifs.append((children[i], children[i + 1].statements))
            i += 2
        if i < len(children):
            orelse = children[i].statements
        return N.If(cond=cond, body=body, elifs=elifs, orelse=orelse, loc=_loc(meta))

    def while_stmt(self, meta, children):
        cond, body = children
        return N.While(cond=cond, body=body.statements, loc=_loc(meta))

    def for_stmt(self, meta, children):
        name_tok = children[0]
        expr = children[1]
        where: Optional[N.Expr] = None
        idx = 2
        if idx < len(children) and isinstance(children[idx], _Where):
            where = children[idx].cond
            idx += 1
        body = children[idx]
        return N.For(
            var=str(name_tok), iterable=expr, where=where,
            body=body.statements, loc=_loc(meta),
        )

    def each_where(self, meta, children):
        return _Where(cond=children[0])

    def each_stmt(self, meta, children):
        # NAME, expr, [parallel?], block
        name_tok = children[0]
        expr = children[1]
        parallel = False
        parallel_args: list[N.Arg] = []
        idx = 2
        if idx < len(children) and isinstance(children[idx], _Parallel):
            parallel = True
            parallel_args = children[idx].args
            idx += 1
        body = children[idx].statements
        return N.Each(
            var=str(name_tok),
            iterable=expr,
            parallel=parallel,
            parallel_args=parallel_args,
            body=body,
            loc=_loc(meta),
        )

    def each_parallel(self, meta, children):
        if not children:
            return _Parallel(args=[])
        # children[0] is parallel_args result (list[Arg]) or empty list
        return _Parallel(args=children[0] if children else [])

    def parallel_args(self, meta, children):
        if not children:
            return []
        return children[0]  # args list

    def each_expr(self, meta, children):
        name_tok = children[0]
        expr = children[1]
        parallel = False
        parallel_args: list[N.Arg] = []
        where: Optional[N.Expr] = None
        idx = 2
        if idx < len(children) and isinstance(children[idx], _Parallel):
            parallel = True
            parallel_args = children[idx].args
            idx += 1
        if idx < len(children) and isinstance(children[idx], _Where):
            where = children[idx].cond
            idx += 1
        body = children[idx].statements
        return N.EachExpr(
            var=str(name_tok),
            iterable=expr,
            parallel=parallel,
            parallel_args=parallel_args,
            where=where,
            body=body,
            loc=_loc(meta),
        )

    def every_stmt(self, meta, children):
        seconds = children[0]
        body = children[1]
        return N.Every(seconds=seconds, body=body.statements, loc=_loc(meta))

    def on_event_stmt(self, meta, children):
        source, body = children
        return N.OnEvent(source=source, body=body.statements, loc=_loc(meta))

    def match_stmt(self, meta, children):
        subject = children[0]
        cases = [c for c in children[1:] if isinstance(c, N.Case)]
        return N.Match(subject=subject, cases=cases, loc=_loc(meta))

    def case_clause(self, meta, children):
        pat = children[0]
        guard: Optional[N.Expr] = None
        body_idx = 1
        if len(children) > 2:
            guard = children[1]
            body_idx = 2
        body = children[body_idx]
        return N.Case(pattern=pat, guard=guard, body=body.statements, loc=_loc(meta))

    def case_guard(self, meta, children):
        return children[0]

    def pattern_or(self, meta, children):
        if len(children) == 1:
            return children[0]
        return N.PatOr(alternatives=list(children), loc=_loc(meta))

    def try_stmt(self, meta, children):
        body = children[0].statements
        catches: list[N.Catch] = []
        finally_body: list[N.Stmt] = []
        for c in children[1:]:
            if isinstance(c, N.Catch):
                catches.append(c)
            elif isinstance(c, _Finally):
                finally_body = c.body
        return N.Try(body=body, catches=catches, finally_body=finally_body, loc=_loc(meta))

    def catch_clause(self, meta, children):
        exc = children[0]
        name = None
        body_idx = 1
        if isinstance(children[1], Token) and children[1].type == "NAME":
            name = str(children[1])
            body_idx = 2
        body = children[body_idx].statements
        return N.Catch(exc=exc, name=name, body=body, loc=_loc(meta))

    def finally_clause(self, meta, children):
        return _Finally(body=children[0].statements)

    def serve_stmt(self, meta, children):
        handler, port = children
        return N.Serve(handler=handler, port=port, loc=_loc(meta))

    def deploy_stmt(self, meta, children):
        handler, target = children[0], children[1]
        body: list[N.Stmt] = []
        if len(children) > 2:
            body = children[2].statements
        return N.Deploy(handler=handler, target=target, body=body, loc=_loc(meta))

    def port_lit(self, meta, children):
        return int(children[0])

    # Duration → seconds (float)
    def duration_lit(self, meta, children):
        n_tok, mult = children
        return float(n_tok) * mult

    def dur_sec(self, meta, _):
        return 1.0

    def dur_min(self, meta, _):
        return 60.0

    def dur_hour(self, meta, _):
        return 3600.0

    def dur_day(self, meta, _):
        return 86400.0

    # ---------- Use ----------

    def use_stmt(self, meta, children):
        target_node = children[0]
        alias = None
        if len(children) > 1:
            alias = str(children[1])
        return N.Use(
            target=target_node.name,
            is_string=target_node.is_string,
            alias=alias,
            loc=_loc(meta),
        )

    def use_dotted(self, meta, children):
        return _UseTarget(name=".".join(str(c) for c in children), is_string=False)

    def use_string(self, meta, children):
        s = _strip_string(str(children[0]))
        return _UseTarget(name=s.value, is_string=True)

    # ---------- fn / class ----------

    def fn_decl(self, meta, children):
        decorators: list[N.Decorator] = []
        idx = 0
        while idx < len(children) and isinstance(children[idx], N.Decorator):
            decorators.append(children[idx])
            idx += 1
        # Optional `async` token before the function NAME.
        is_async = False
        if (
            idx < len(children)
            and hasattr(children[idx], "type")
            and children[idx].type == "ASYNC_KW"
        ):
            is_async = True
            idx += 1
        name_tok = children[idx]; idx += 1
        params: list[N.Param] = []
        return_type: Optional[N.TypeRef] = None
        body: Optional[N.Expr] = None
        block: Optional[list[N.Stmt]] = None
        # The remaining children: optional params (list), optional return_type, fn_body
        for c in children[idx:-1]:
            if isinstance(c, list) and (not c or isinstance(c[0], N.Param)):
                params = c
            elif isinstance(c, N.TypeRef):
                return_type = c
        fn_body = children[-1]
        if isinstance(fn_body, _FnBodyBlock):
            block = fn_body.statements
        else:
            body = fn_body.expr  # _FnBodyExpr
        return N.FnDecl(
            name=str(name_tok),
            params=params,
            return_type=return_type,
            body=body,
            block=block,
            decorators=decorators,
            is_async=is_async,
            loc=_loc(meta),
        )

    def fn_block_body(self, meta, children):
        return _FnBodyBlock(statements=children[0].statements)

    def fn_expr_body(self, meta, children):
        return _FnBodyExpr(expr=children[0])

    def decorator(self, meta, children):
        # children: dotted_name, optional args
        dotted = children[0]
        args: list[N.Arg] = []
        has_call = False
        if len(children) > 1:
            has_call = True
            if children[1] is not None:
                args = children[1]
        return N.Decorator(name=dotted, args=args, has_call=has_call, loc=_loc(meta))

    def dotted_name(self, meta, children):
        return ".".join(str(c) for c in children)

    def params(self, meta, children):
        return list(children)

    def param_normal(self, meta, children):
        name_tok = children[0]
        type_ref: Optional[N.TypeRef] = None
        default: Optional[N.Expr] = None
        for c in children[1:]:
            if isinstance(c, N.TypeRef):
                type_ref = c
            elif isinstance(c, N.Expr):
                default = c
        return N.Param(name=str(name_tok), type_ref=type_ref, default=default, loc=_loc(meta))

    def param_star(self, meta, children):
        return N.Param(name=str(children[0]), star=True, loc=_loc(meta))

    def param_dstar(self, meta, children):
        return N.Param(name=str(children[0]), dstar=True, loc=_loc(meta))

    def return_type(self, meta, children):
        return children[0]

    def type(self, meta, children):
        name = str(children[0])
        args = [c for c in children[1:] if isinstance(c, N.TypeRef)]
        return N.TypeRef(name=name, args=args, loc=_loc(meta))

    def optional_type(self, meta, children):
        return N.TypeRef(name=str(children[0]), optional=True, loc=_loc(meta))

    def class_decl(self, meta, children):
        name_tok = children[0]
        supers: list[N.TypeRef] = []
        body_items: list[N.Stmt] = []
        for c in children[1:]:
            if isinstance(c, _ClassSuper):
                supers = c.types
            elif isinstance(c, N.Stmt):
                body_items.append(c)
        return N.ClassDecl(name=str(name_tok), supers=supers, body=body_items, loc=_loc(meta))

    def class_super(self, meta, children):
        return _ClassSuper(types=list(children))

    def class_body_item(self, meta, children):
        return children[0]

    # ---------- data class / data sum ----------

    def data_field(self, meta, children):
        name = str(children[0])
        tref: Optional[N.TypeRef] = None
        default: Optional[N.Expr] = None
        for c in children[1:]:
            if isinstance(c, N.TypeRef):
                tref = c
            elif isinstance(c, N.Expr):
                default = c
        return N.DataField(name=name, type_ref=tref, default=default, loc=_loc(meta))

    def data_fields(self, meta, children):
        return list(children)

    def data_class_decl(self, meta, children):
        name = str(children[0])
        fields: list[N.DataField] = []
        for c in children[1:]:
            if isinstance(c, list):
                fields = c
        return N.DataClassDecl(name=name, fields=fields, loc=_loc(meta))

    def data_variant(self, meta, children):
        name = str(children[0])
        fields: list[N.DataField] = []
        for c in children[1:]:
            if isinstance(c, list):
                fields = c
        return N.DataVariant(name=name, fields=fields, loc=_loc(meta))

    def data_sum_decl(self, meta, children):
        name = str(children[0])
        variants: list[N.DataVariant] = [c for c in children[1:] if isinstance(c, N.DataVariant)]
        return N.DataSumDecl(name=name, variants=variants, loc=_loc(meta))

    # ---------- Patterns ----------

    def pat_num(self, meta, children):
        n = float(children[0])
        is_int = n.is_integer() and "." not in str(children[0])
        return N.PatLiteral(value=N.Num(value=n, is_int=is_int, loc=_loc(meta)), loc=_loc(meta))

    def pat_str(self, meta, children):
        s = _strip_string(str(children[0]))
        return N.PatLiteral(value=N.Str(value=s.value, is_raw=s.is_raw, loc=_loc(meta)), loc=_loc(meta))

    def pat_true(self, meta, _):
        return N.PatLiteral(value=N.Bool(value=True, loc=_loc(meta)), loc=_loc(meta))

    def pat_false(self, meta, _):
        return N.PatLiteral(value=N.Bool(value=False, loc=_loc(meta)), loc=_loc(meta))

    def pat_none(self, meta, _):
        return N.PatLiteral(value=N.NoneLit(loc=_loc(meta)), loc=_loc(meta))

    def pat_wildcard(self, meta, _):
        return N.PatWildcard(loc=_loc(meta))

    def pat_name(self, meta, children):
        return N.PatName(name=str(children[0]), loc=_loc(meta))

    def pat_call(self, meta, children):
        return N.PatCall(name=str(children[0]), items=list(children[1:]), loc=_loc(meta))

    def pat_list(self, meta, children):
        return N.PatList(items=list(children), loc=_loc(meta))

    def pat_dict(self, meta, children):
        entries: list[tuple] = []
        rest_name = None
        for c in children:
            if isinstance(c, N.PatRest) and c.is_double_star:
                rest_name = c.name
            elif isinstance(c, tuple):
                entries.append(c)
        return N.PatDict(entries=entries, rest_name=rest_name, loc=_loc(meta))

    def pat_item_plain(self, meta, children):
        return children[0]

    def pat_item_rest(self, meta, children):
        return N.PatRest(name=str(children[0]), is_double_star=False, loc=_loc(meta))

    def pat_dict_kv(self, meta, children):
        s = _strip_string(str(children[0]))
        return (N.Str(value=s.value, loc=_loc(meta)), children[1])

    def pat_dict_kv_num(self, meta, children):
        n = float(children[0])
        is_int = "." not in str(children[0])
        return (N.Num(value=n, is_int=is_int, loc=_loc(meta)), children[1])

    def pat_dict_rest(self, meta, children):
        return N.PatRest(name=str(children[0]), is_double_star=True, loc=_loc(meta))

    def pat_tuple(self, meta, children):
        return N.PatTuple(items=list(children), loc=_loc(meta))

    # ---------- Block ----------

    def block(self, meta, children):
        stmts = [c for c in children if isinstance(c, N.Stmt)]
        return _Block(statements=stmts)

    # ---------- Expressions ----------

    def ternary(self, meta, children):
        if len(children) == 1:
            return children[0]
        if_true, cond, if_false = children
        return N.Ternary(cond=cond, if_true=if_true, if_false=if_false, loc=_loc(meta))

    def or_expr(self, meta, children):
        if len(children) == 1:
            return children[0]
        return N.BoolOp(op="or", operands=list(children), loc=_loc(meta))

    def and_expr(self, meta, children):
        if len(children) == 1:
            return children[0]
        return N.BoolOp(op="and", operands=list(children), loc=_loc(meta))

    def not_op(self, meta, children):
        return N.UnaryOp(op="not", operand=children[0], loc=_loc(meta))

    def comparison(self, meta, children):
        if len(children) == 1:
            return children[0]
        # children: expr, op, expr, op, expr, ...
        left = children[0]
        ops: list[str] = []
        comparators: list[N.Expr] = []
        i = 1
        while i < len(children):
            ops.append(children[i])
            comparators.append(children[i + 1])
            i += 2
        return N.Compare(left=left, ops=ops, comparators=comparators, loc=_loc(meta))

    # comparison operators return a string
    def eq(self, meta, _): return "=="
    def ne(self, meta, _): return "!="
    def lt(self, meta, _): return "<"
    def gt(self, meta, _): return ">"
    def le(self, meta, _): return "<="
    def ge(self, meta, _): return ">="
    def is_op(self, meta, _): return "is"
    def is_not_op(self, meta, _): return "is not"
    def in_op(self, meta, _): return "in"
    def not_in_op(self, meta, _): return "not in"

    def nullish(self, meta, children):
        if len(children) == 1:
            return children[0]
        return N.Nullish(operands=list(children), loc=_loc(meta))

    def bit_or(self, meta, children):
        return _left_assoc(children, "|", _loc(meta))

    def bit_xor(self, meta, children):
        return _left_assoc(children, "^", _loc(meta))

    def bit_and(self, meta, children):
        return _left_assoc(children, "&", _loc(meta))

    def shift(self, meta, children):
        # children: expr, OP_TOK, expr, OP_TOK, expr, ...
        if len(children) == 1:
            return children[0]
        node = children[0]
        i = 1
        while i < len(children):
            op = str(children[i])
            node = N.BinOp(op=op, left=node, right=children[i + 1], loc=_loc(meta))
            i += 2
        return node

    def addsub(self, meta, children):
        return _left_assoc_op(children, _loc(meta))

    def muldiv(self, meta, children):
        return _left_assoc_op(children, _loc(meta))

    def power(self, meta, children):
        if len(children) == 1:
            return children[0]
        # children: [left, POWER_token, right]
        return N.BinOp(op="**", left=children[0], right=children[-1], loc=_loc(meta))

    def addsub_unary(self, meta, children):
        op_tok, val = children
        return N.UnaryOp(op=str(op_tok), operand=val, loc=_loc(meta))

    def invert(self, meta, children):
        return N.UnaryOp(op="~", operand=children[0], loc=_loc(meta))

    def call_expr(self, meta, children):
        func = children[0]
        args = children[1] if len(children) > 1 else []
        return N.Call(func=func, args=args, loc=_loc(meta))

    def index_expr(self, meta, children):
        target, key = children
        return N.Index(target=target, key=key, loc=_loc(meta))

    # ---------- subscript variants ----------

    def sub_index(self, meta, children):
        return children[0]  # the inner expression — passed through to index_expr

    def sub_slice3(self, meta, children):
        a, b, c = children
        return N.Slice(start=a, stop=b, step=c, loc=_loc(meta))

    def sub_slice2(self, meta, children):
        a, b = children
        return N.Slice(start=a, stop=b, loc=_loc(meta))

    def sub_slice_l(self, meta, children):
        return N.Slice(start=children[0], loc=_loc(meta))

    def sub_slice_r(self, meta, children):
        return N.Slice(stop=children[0], loc=_loc(meta))

    def sub_slice_full(self, meta, children):
        return N.Slice(loc=_loc(meta))

    def attr_expr(self, meta, children):
        target = children[0]
        name = str(children[1])
        return N.Attr(target=target, name=name, loc=_loc(meta))

    def safe_attr_expr(self, meta, children):
        target = children[0]
        name = str(children[1])
        return N.SafeAttr(target=target, name=name, loc=_loc(meta))

    def try_propagate_expr(self, meta, children):
        return N.TryPropagate(target=children[0], loc=_loc(meta))

    def await_expr(self, meta, children):
        return N.Await(target=children[0], loc=_loc(meta))

    def args(self, meta, children):
        return list(children)

    def kwarg(self, meta, children):
        name = str(children[0])
        value = children[1]
        return N.Arg(value=value, name=name, loc=_loc(meta))

    def star_arg(self, meta, children):
        return N.Arg(value=children[0], star=True, loc=_loc(meta))

    def dstar_arg(self, meta, children):
        return N.Arg(value=children[0], dstar=True, loc=_loc(meta))

    def pos_arg(self, meta, children):
        return N.Arg(value=children[0], loc=_loc(meta))

    # ---------- Atoms ----------

    def name_ref(self, meta, children):
        return N.Name(name=str(children[0]), loc=_loc(meta))

    def number_lit(self, meta, children):
        raw = str(children[0])
        is_int = "." not in raw and "e" not in raw and "E" not in raw
        return N.Num(value=float(raw), is_int=is_int, loc=_loc(meta))

    def string_lit(self, meta, children):
        s = _strip_string(str(children[0]))
        has_interp = (not s.is_raw) and _has_interpolation(s.value)
        return N.Str(value=s.value, is_raw=s.is_raw, has_interp=has_interp, loc=_loc(meta))

    def true_lit(self, meta, _):
        return N.Bool(value=True, loc=_loc(meta))

    def false_lit(self, meta, _):
        return N.Bool(value=False, loc=_loc(meta))

    def none_lit(self, meta, _):
        return N.NoneLit(loc=_loc(meta))

    # paren_or_tuple
    def paren_or_tuple(self, meta, children):
        if not children:
            return N.Tuple(items=[], loc=_loc(meta))
        inner: _ParenInner = children[0]
        if inner.is_tuple:
            return N.Tuple(items=inner.items, loc=_loc(meta))
        return inner.items[0]

    def paren_inner(self, meta, children):
        items = [c for c in children if isinstance(c, N.Expr)]
        # Detect trailing comma: lark drops the comma token, so we must
        # inspect the raw children to know if it's a single-element tuple.
        has_trailing_comma = False
        # If lark gave us only Expr children, we cannot detect commas.
        # But: a single-element tuple `(x,)` is parsed via this rule
        # producing one Expr child. To distinguish from `(x)`, check
        # number of expressions vs. number of commas in the meta line text
        # (heuristic). We mark `is_tuple = (len > 1)` for now; trailing
        # comma support for single-element tuples requires special-casing.
        is_tuple = len(items) > 1 or has_trailing_comma
        return _ParenInner(items=items, is_tuple=is_tuple)

    def list_lit(self, meta, children):
        if not children:
            return N.List(items=[], loc=_loc(meta))
        inner = children[0]
        return N.List(items=inner, loc=_loc(meta))

    def list_inner(self, meta, children):
        return [c for c in children if isinstance(c, N.Expr)]

    # dict_or_set
    def dict_or_set(self, meta, children):
        if not children:
            return N.Dict(entries=[], loc=_loc(meta))
        inner = children[0]
        if isinstance(inner, _DictInner):
            return N.Dict(entries=inner.entries, loc=_loc(meta))
        return N.Set(items=inner, loc=_loc(meta))

    def dict_inner(self, meta, children):
        entries = [c for c in children if isinstance(c, _DictEntry)]
        return _DictInner(entries=[(e.key, e.value) for e in entries])

    def set_inner(self, meta, children):
        return [c for c in children if isinstance(c, N.Expr)]

    def set_inner_one(self, meta, children):
        return [children[0]]

    def dict_kv(self, meta, children):
        k, v = children
        return _DictEntry(key=k, value=v)

    def dict_spread(self, meta, children):
        return _DictEntry(key=None, value=children[0])

    def lambda_expr(self, meta, children):
        params: list[N.Param] = []
        return_type: Optional[N.TypeRef] = None
        body: Optional[N.Expr] = None
        block: Optional[list[N.Stmt]] = None
        for c in children[:-1]:
            if isinstance(c, list) and (not c or isinstance(c[0], N.Param)):
                params = c
            elif isinstance(c, N.TypeRef):
                return_type = c
        last = children[-1]
        if isinstance(last, _FnBodyBlock):
            block = last.statements
        else:
            body = last.expr
        return N.Lambda(
            params=params, return_type=return_type, body=body, block=block, loc=_loc(meta)
        )

    def lambda_block_body(self, meta, children):
        return _FnBodyBlock(statements=children[0].statements)

    def lambda_expr_body(self, meta, children):
        return _FnBodyExpr(expr=children[0])


# ---------- helper containers (transient) ----------


@dataclass
class _AssignTail:
    kind: str  # "eq" or "aug"
    op: str
    value: N.Expr


@dataclass
class _Block:
    statements: list[N.Stmt]


@dataclass
class _Parallel:
    args: list[N.Arg]


@dataclass
class _Where:
    cond: N.Expr


@dataclass
class _FnBodyBlock:
    statements: list[N.Stmt]


@dataclass
class _FnBodyExpr:
    expr: N.Expr


@dataclass
class _Finally:
    body: list[N.Stmt]


@dataclass
class _ClassSuper:
    types: list[N.TypeRef]


@dataclass
class _UseTarget:
    name: str
    is_string: bool


@dataclass
class _ParenInner:
    items: list[N.Expr]
    is_tuple: bool


@dataclass
class _DictEntry:
    key: Optional[N.Expr]
    value: N.Expr


@dataclass
class _DictInner:
    entries: list[tuple]


# ---------- helpers ----------


def _left_assoc(children: list, op: str, loc: Optional[N.Loc]) -> N.Expr:
    if len(children) == 1:
        return children[0]
    node = children[0]
    for nxt in children[1:]:
        node = N.BinOp(op=op, left=node, right=nxt, loc=loc)
    return node


def _left_assoc_op(children: list, loc: Optional[N.Loc]) -> N.Expr:
    """For grammar rules where operator tokens are interleaved with operands."""
    if len(children) == 1:
        return children[0]
    node = children[0]
    i = 1
    while i < len(children):
        op = str(children[i])
        node = N.BinOp(op=op, left=node, right=children[i + 1], loc=loc)
        i += 2
    return node


@dataclass
class _StringValue:
    value: str
    is_raw: bool


def _strip_string(raw: str) -> _StringValue:
    """Decode a STRING terminal into its string value and raw flag."""
    is_raw = False
    if raw.startswith("r") or raw.startswith("R"):
        is_raw = True
        raw = raw[1:]
    if raw.startswith('"""') and raw.endswith('"""'):
        body = raw[3:-3]
    elif raw.startswith("'''") and raw.endswith("'''"):
        body = raw[3:-3]
    else:
        body = raw[1:-1]
    if not is_raw:
        body = (
            body.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\r", "\r")
            .replace("\\\\", "\\")
            .replace("\\\"", "\"")
            .replace("\\'", "'")
        )
    return _StringValue(value=body, is_raw=is_raw)


def _has_interpolation(s: str) -> bool:
    """Detect ``{...}`` interpolation markers in a (non-raw) string body.

    Handles ``{{`` / ``}}`` as escapes (literal braces), like Python f-strings.
    """
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "{":
            if i + 1 < n and s[i + 1] == "{":
                i += 2
                continue
            # find matching `}` at same nesting level
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                j += 1
            if depth == 0 and j > i + 2:
                return True
            return False
        i += 1
    return False


# ---------- Public API ----------


def parse(source: str, source_path: Optional[str] = None) -> N.Module:
    parser = get_parser()
    try:
        tree = parser.parse(source)
    except UnexpectedInput as e:
        line = getattr(e, "line", 0) or 0
        col = getattr(e, "column", 0) or 0
        snippet_line = source.splitlines()[line - 1] if 0 < line <= len(source.splitlines()) else ""
        msg = _format_lark_error(e)
        hint = _hint_for_error(e, snippet_line)
        raise ParseError(
            message=msg,
            line=line,
            column=col,
            source_path=source_path,
            snippet=snippet_line,
            hint=hint,
        ) from None
    return _Transformer().transform(tree)


def parse_collect_errors(
    source: str, source_path: Optional[str] = None, *, max_errors: int = 10
) -> tuple[Optional[N.Module], list[ParseError]]:
    """Parse with error recovery — synchronizes on newlines.

    Returns ``(module_or_None, errors)``. If at least one error is recovered,
    the module covers the parts that DID parse, with ``Pass`` placeholders
    where lines were skipped.

    Useful for tooling (LSP, ``c4 check``) where the user wants every
    diagnostic in one shot, not one-by-one.

    Strategy:
      1. Try a clean parse first; success → ``(module, [])``.
      2. On failure, slice the source into top-level statements (split by
         leading-column whitespace heuristics + newlines), parse each
         independently, collect errors, splice back.
    """
    try:
        return parse(source, source_path), []
    except ParseError as e:
        first = e

    # Recovery: parse line-by-line "chunks" between blank lines / top-level boundaries.
    chunks = _split_top_level(source)
    body: list[N.Stmt] = []
    errors: list[ParseError] = [first]
    for chunk_text, line_offset in chunks:
        if not chunk_text.strip():
            continue
        try:
            sub = parse(chunk_text, source_path)
            for s in sub.body:
                if s.loc is not None:
                    s.loc = N.Loc(line=s.loc.line + line_offset, column=s.loc.column)
                body.append(s)
        except ParseError as e:
            if e is not first:
                errors.append(
                    ParseError(
                        message=e.message,
                        line=e.line + line_offset,
                        column=e.column,
                        source_path=source_path,
                        snippet=e.snippet,
                        hint=e.hint,
                    )
                )
            if len(errors) >= max_errors:
                break
    return N.Module(body=body), errors


def _split_top_level(source: str) -> list[tuple[str, int]]:
    """Slice source into top-level statement chunks.

    A chunk is a contiguous run of non-blank lines starting at column 0
    (no leading whitespace). Multi-line constructs (``{ ... }``) are
    kept together by tracking brace depth.
    """
    lines = source.splitlines(keepends=True)
    chunks: list[tuple[str, int]] = []
    cur: list[str] = []
    cur_start = 0
    depth = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not cur and not stripped:
            continue
        if not cur and (line[0:1] in (" ", "\t")) and depth == 0:
            # Skip indented orphan line.
            continue
        if not cur:
            cur_start = i
        cur.append(line)
        # Naive depth tracking — strings/comments are NOT excluded; good enough
        # for recovery heuristic. The per-chunk parse will reject malformed.
        for c in line:
            if c in "({[":
                depth += 1
            elif c in ")}]":
                depth -= 1
        if depth <= 0:
            depth = 0
            # Close the chunk on a blank line at top-level OR when next line
            # starts at column 0 with a control keyword.
            if i + 1 == len(lines) or not lines[i + 1].strip():
                chunks.append(("".join(cur), cur_start))
                cur = []
    if cur:
        chunks.append(("".join(cur), cur_start))
    return chunks


def _format_lark_error(e: UnexpectedInput) -> str:
    expected = getattr(e, "expected", None)
    got_token = getattr(e, "token", None)
    got = f"'{got_token}'" if got_token is not None else "input"
    if expected:
        return f"unexpected {got}; expected one of {sorted(expected)[:6]}"
    return f"unexpected {got}"


def _hint_for_error(e: UnexpectedInput, snippet: str) -> Optional[str]:
    expected = getattr(e, "expected", None) or set()
    if "LBRACE" in expected or "block" in str(expected).lower():
        return "blocks are delimited with `{ ... }` (e.g. `fn greet() { ... }`)"
    if snippet.strip().startswith("import "):
        return "use `use module` (or `use module as alias`) instead of Python `import`"
    return None
