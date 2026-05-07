# Dogfooding the stdlib

Imports several `cobra4.stdlib.*` modules — themselves written in cobra4 — and chains them. Confirms the stdlib import hook with mtime cache works end-to-end.

Source: [`examples/08_stdlib_dogfood.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/08_stdlib_dogfood.c4)

```cobra4
--8<-- "examples/08_stdlib_dogfood.c4"
```

## Run it

```bash
c4 run examples/08_stdlib_dogfood.c4
```
