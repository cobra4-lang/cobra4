"""Parser tests — exercise every grammatical construct in the M1 subset."""

from __future__ import annotations

import pytest

from cobra4 import ast_nodes as N
from cobra4.parser import parse, ParseError


def kinds(src: str) -> list[str]:
    return [type(s).__name__ for s in parse(src).body]


# ---------- simple statements ----------


def test_assign_int():
    [s] = parse("x = 1\n").body
    assert isinstance(s, N.Assign)
    assert isinstance(s.targets[0], N.Name) and s.targets[0].name == "x"
    assert isinstance(s.value, N.Num) and s.value.value == 1.0


def test_aug_assign():
    [s] = parse("x += 5\n").body
    assert isinstance(s, N.AugAssign) and s.op == "+="


def test_attribute_assign():
    [s] = parse("a.b = 1\n").body
    assert isinstance(s, N.Assign)
    assert isinstance(s.targets[0], N.Attr)


def test_index_assign():
    [s] = parse("a[0] = 1\n").body
    assert isinstance(s, N.Assign)
    assert isinstance(s.targets[0], N.Index)


# ---------- expressions ----------


def test_arithmetic_precedence():
    [s] = parse("y = 1 + 2 * 3\n").body
    # 1 + (2 * 3)
    assert isinstance(s.value, N.BinOp) and s.value.op == "+"
    assert isinstance(s.value.right, N.BinOp) and s.value.right.op == "*"


def test_unary_minus():
    [s] = parse("y = -x\n").body
    assert isinstance(s.value, N.UnaryOp) and s.value.op == "-"


def test_safe_attr_and_default():
    [s] = parse("y = a?.b ?? 0\n").body
    assert isinstance(s.value, N.Nullish)
    assert isinstance(s.value.operands[0], N.SafeAttr)


def test_comparison_chain():
    [s] = parse("y = 1 < 2 < 3\n").body
    assert isinstance(s.value, N.Compare)
    assert s.value.ops == ["<", "<"]


def test_bool_op():
    [s] = parse("y = a and b or c\n").body
    # Top-level is `or` of [(a and b), c]
    assert isinstance(s.value, N.BoolOp) and s.value.op == "or"


def test_dict_literal():
    [s] = parse("d = {a: 1, b: 2}\n").body
    assert isinstance(s.value, N.Dict)
    assert len(s.value.entries) == 2


def test_list_literal():
    [s] = parse("d = [1, 2, 3]\n").body
    assert isinstance(s.value, N.List) and len(s.value.items) == 3


def test_tuple_literal():
    [s] = parse("d = (1, 2, 3)\n").body
    assert isinstance(s.value, N.Tuple) and len(s.value.items) == 3


def test_set_literal():
    [s] = parse("d = {1, 2, 3}\n").body
    assert isinstance(s.value, N.Set) and len(s.value.items) == 3


def test_string_with_interpolation():
    [s] = parse('y = "hello {name}"\n').body
    assert isinstance(s.value, N.Str) and s.value.has_interp


def test_lambda_inline():
    [s] = parse("f = fn(x) = x + 1\n").body
    assert isinstance(s.value, N.Lambda) and s.value.body is not None


def test_lambda_block():
    [s] = parse("f = fn(x) { return x + 1 }\n").body
    assert isinstance(s.value, N.Lambda) and s.value.block is not None


def test_call_with_kwargs_and_splats():
    [s] = parse("y = f(1, *xs, k=2, **kw)\n").body
    args = s.value.args
    assert args[0].star is False and args[0].dstar is False
    assert args[1].star is True
    assert args[2].name == "k"
    assert args[3].dstar is True


# ---------- compound ----------


def test_if_elif_else():
    [s] = parse("if a { x } elif b { y } else { z }\n").body
    assert isinstance(s, N.If) and len(s.elifs) == 1 and s.orelse


def test_while_loop():
    [s] = parse("while a { do() }\n").body
    assert isinstance(s, N.While)


def test_for_loop():
    [s] = parse("for i in xs { do(i) }\n").body
    assert isinstance(s, N.For) and s.var == "i"


def test_each_expr_parallel():
    [s] = parse("r = each i in xs in parallel(workers=4) { f(i) }\n").body
    assert isinstance(s, N.Assign)
    assert isinstance(s.value, N.EachExpr) and s.value.parallel


def test_each_expr_sequential():
    [s] = parse("r = each i in xs { f(i) }\n").body
    assert isinstance(s.value, N.EachExpr) and not s.value.parallel


def test_match():
    [s] = parse("match x { case 1 { foo() } case _ { bar() } }\n").body
    assert isinstance(s, N.Match) and len(s.cases) == 2


def test_try_catch_finally():
    [s] = parse("try { foo() } catch Err as e { bar() } finally { baz() }\n").body
    assert isinstance(s, N.Try)
    assert s.catches and s.catches[0].name == "e"
    assert s.finally_body


def test_every():
    [s] = parse("every 5 minutes { tick() }\n").body
    assert isinstance(s, N.Every) and s.seconds == 300.0


def test_on_event():
    [s] = parse('on event from queue("orders") { handle(event) }\n').body
    assert isinstance(s, N.OnEvent)


def test_serve():
    [s] = parse("serve handler on :8080\n").body
    assert isinstance(s, N.Serve) and s.port == 8080


def test_deploy_with_block():
    [s] = parse('deploy api to aws.lambda(region="x") { env from ".env" }\n').body
    assert isinstance(s, N.Deploy)


# ---------- declarations ----------


def test_fn_decl_block():
    [s] = parse("fn add(a, b) { return a + b }\n").body
    assert isinstance(s, N.FnDecl) and s.block is not None


def test_fn_decl_inline():
    [s] = parse("fn add(a, b) = a + b\n").body
    assert isinstance(s, N.FnDecl) and s.body is not None


def test_fn_with_decorators():
    [s] = parse("@smart\nfn process(t) { return t }\n").body
    assert isinstance(s, N.FnDecl) and len(s.decorators) == 1
    assert s.decorators[0].name == "smart"


def test_fn_with_type_annotations():
    [s] = parse("fn greet(name: str) -> str = name\n").body
    assert s.params[0].type_ref.name == "str"
    assert s.return_type.name == "str"


def test_class_decl():
    [s] = parse("class Foo(Bar) { fn x(self) { return 1 } }\n").body
    assert isinstance(s, N.ClassDecl) and s.supers


def test_use_dotted():
    [s] = parse("use a.b.c\n").body
    assert isinstance(s, N.Use) and s.target == "a.b.c"


def test_use_string():
    [s] = parse('use "./mylib"\n').body
    assert isinstance(s, N.Use) and s.is_string and s.target == "./mylib"


def test_use_alias():
    [s] = parse("use json as j\n").body
    assert s.alias == "j"


# ---------- error messages ----------


def test_parse_error_has_location():
    with pytest.raises(ParseError) as ei:
        parse("fn foo(\n")
    assert ei.value.line >= 1


def test_parse_error_renders_snippet():
    try:
        parse("fn foo()\n")
    except ParseError as e:
        text = str(e)
        assert "error:" in text
        assert "fn foo()" in text or "expected" in text
