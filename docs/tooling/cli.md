# CLI reference

```bash
c4 run    FILE                # transpile + execute
c4 build  FILE -o OUT.py      # transpile only
c4 fmt    FILE [-w]           # canonical re-format from AST (-w writes)
c4 check  FILE [--strict]     # lint + types + dispatch overlap (no exec)
c4 repl                       # multi-line REPL with completion + history
c4 idle                       # browser IDLE with generated Python + graph
c4 lsp                        # language server on stdio
c4 serve  FILE                # daemon: every / on event from / serve
c4 test                       # discover & run tests/test_*.c4
c4 doc    FILE [--html]       # extract docstrings + signatures
c4 deps   add|list|remove|install [--venv]
c4 plugin list|add NAME
```

## `--watch`

`c4 run FILE --watch` reruns on file change.

## Exit codes

| Code | Meaning                                                   |
|------|-----------------------------------------------------------|
| `0`  | Success.                                                  |
| `1`  | Runtime exception in user code, or test failures.         |
| `2`  | Parse / lint errors (`c4 check`, `c4 build`).             |

## Configuration

A project's `cobra4.toml` (top of repo) configures dependencies,
language plugins, secrets backend, and host inventory. See
[Fleet & secrets](../cloud/fleet.md) for the inventory schema.
