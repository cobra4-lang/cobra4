# Lexicon & syntax

cobra4 is **brace-based** with English keywords. The grammar is LALR(1)
and lives in [`cobra4/grammar.lark`](https://github.com/cobra4-lang/cobra4/blob/main/cobra4/grammar.lark).

## Identifiers

```ebnf
NAME: [A-Za-z_][A-Za-z0-9_]*
```

## Reserved keywords

```
if elif else while for each in not and or is True False None
fn class return raise break continue pass
match case try catch finally
use as where every on event from to parallel
serve deploy lang
```

## Comments

```cobra4
# only `#` opens a comment.
# `//` is always integer division — never a comment.
```

## Numbers

```cobra4
42         # int
3.14       # float
1.5e-3     # scientific
```

Hex / oct / binary literals (`0x1F`, `0b101`) and underscore separators
(`1_000_000`) are **not** supported. Use `int("0x1F", 16)` if needed.

## Strings

All non-raw strings are interpolated:

```cobra4
"hello {name}"               # interpolation, no `f` prefix needed
"escape {{braces}}"          # `{{` and `}}` are literal braces
r"C:\Users\path"             # raw — no escapes
"""
multi
line
"""
```

There is **no** explicit format spec like `"{x:.2f}"`. Use
`format(x, ".2f")` or `"{round(x, 2)}"`.

## Operators

| Category   | Operators                                                          |
|------------|--------------------------------------------------------------------|
| Arithmetic | `+`, `-`, `*`, `/`, `//`, `%`, `**`                                |
| Bitwise    | `&`, `\|`, `^`, `~`, `<<`, `>>`                                    |
| Comparison | `==`, `!=`, `<`, `>`, `<=`, `>=`, `is`, `is not`, `in`, `not in`   |
| Bool       | `and`, `or`, `not`                                                 |
| Special    | `?.` (safe-nav), `??` (null-default)                               |
| Assign     | `=`, `+=`, `-=`, `*=`, `/=`, `//=`, `%=`, `**=`, `&=`, `\|=`, `^=`, `<<=`, `>>=` |

There is **no** walrus (`:=`), **no** pipe (`|>`), and **no** matrix
multiply (`@` is decorator-only).

## Safe-nav and default

```cobra4
name = req?.params?.name ?? "anon"
```

`?.` returns `None` if the target is `None`, **and** uses `.get()` when
the target is a dict instead of `getattr` — so `req?.params?.name`
works whether `params` is an object or a `dict`.
