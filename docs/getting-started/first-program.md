# Your first cobra4 program

Save this as `hello.c4`:

```cobra4
fn main() {
    name = "world"
    log("hello", to=name)

    # `each` is a list comprehension when an expression
    squares = each n in [1, 2, 3, 4] { n * n }
    log("squares", values=squares)
}

main()
```

Run it:

```bash
c4 run hello.c4
```

You should see:

```
2026-05-07T17:00 level=info msg=hello to=world
2026-05-07T17:00 level=info msg=squares values=[1, 4, 9, 16]
```

## What `c4 run` actually did

`c4 run hello.c4` is a one-shot pipeline:

1. **Parse** `hello.c4` with the Lark grammar → AST.
2. **Resolve** scopes; warn on undefined names / shadowing.
3. **Type-check** (gradual; advisory).
4. **Codegen** → Python source.
5. **Execute** the generated Python with the cobra4 runtime imported.

To see the Python that gets executed:

```bash
c4 build hello.c4 -o hello.py
cat hello.py
```

You can also open the same project in Cobra4 Studio:

```bash
c4 studio
```

Studio shows the generated Python beside the Cobra4 source and uses the
compiler source map to highlight which generated lines came from each
source line.

## Other useful commands

```bash
c4 studio                # browser Studio: edit, run, inspect generated Python
c4 fmt   hello.c4         # canonical re-format from AST
c4 check hello.c4         # lint + type warnings, no execution
c4 doctor                # local install + project health checks
c4 repl                   # multi-line REPL with completion + history
c4 doc   hello.c4         # extract docstrings to markdown
c4 test                   # discover and run tests/test_*.c4
c4 serve hello.c4         # run as a daemon (every / on event / serve)
c4 lsp                    # language server (used by IDE extensions)
```
