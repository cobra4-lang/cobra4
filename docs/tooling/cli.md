# CLI reference

```bash
c4 run    FILE                # transpile + execute
c4 build  FILE -o OUT.py      # transpile only
c4 fmt    FILE [-w]           # canonical re-format from AST (-w writes)
c4 check  FILE [--strict]     # lint + types + dispatch overlap (no exec)
c4 doctor [--json] [--online] # local install + project health checks
c4 repl                       # multi-line REPL with completion + history
c4 studio                     # Cobra4 Studio with generated Python + graph
c4 idle                       # compatibility alias for c4 studio
c4 lsp                        # language server on stdio
c4 serve  FILE                # daemon: every / on event from / serve
c4 test                       # discover & run tests/test_*.c4
c4 doc    FILE [--html]       # extract docstrings + signatures
c4 deps   add|list|remove|install [--venv]
c4 plugin list|add NAME
```

## Studio

```bash
c4 studio [--host HOST] [--port PORT] [--no-browser] [--verbose]
```

`c4 studio` starts Cobra4 Studio in the current directory. If `--port` is
omitted, Cobra4 asks the operating system for a free random local port to
avoid conflicts with other development servers. Use `c4 idle` only as a
compatibility alias.

## Doctor

```bash
c4 doctor
c4 doctor --json
c4 doctor --online
```

`c4 doctor` checks the local Python version, cobra4 version, CLI
availability, compiler smoke path, stdlib import hook, nearby
`cobra4.toml`, declared project dependencies, configured language
plugins, and optional extras. Missing optional extras are informational;
compiler, stdlib, or invalid project/plugin checks fail the command.

`--json` prints machine-readable output for CI or bug reports. `--online`
also queries PyPI and warns when the local version is behind the latest
published release.

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
