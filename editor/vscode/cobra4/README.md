# cobra4 for VS Code

Language support for [cobra4](https://github.com/cobra4-lang/cobra4): a
high-level cloud-native language transpiled to Python.

## Features

- **Syntax highlighting** for `.c4` files (keywords, plugins, decorators,
  operators, smart-dispatch builtins).
- **Language server** powered by `c4 lsp`:
  - inline diagnostics from the parser, resolver, and type checker
  - go-to-definition, find-references, document outline
  - hover with inferred types and function signatures
  - autocomplete for keywords, builtins, and in-scope identifiers
  - canonical formatter (preserves `lang use` and plugin blocks)
- **Snippets** for `fn`, `each ... in parallel`, `match`, `every`,
  `serve`, `deploy aws.lambda` / `gcp.run` / `k8s`, and more.
- **Commands** wired to the cobra4 CLI:
  - `cobra4: Run File` (Ctrl/Cmd+F5)
  - `cobra4: Build to Python`
  - `cobra4: Format File`
  - `cobra4: Check (lint + types)`
  - `cobra4: Run Tests` (Ctrl/Cmd+Shift+T)
  - `cobra4: Serve (daemon mode)`
  - `cobra4: Restart Language Server`
- **Format on save** (opt-in via `cobra4.format.onSave`).

## Requirements

- Install the cobra4 CLI:

  ```bash
  pip install cobra4
  # or, from a checkout:
  pip install -e /path/to/cobra4
  ```

  Make sure `c4` is on your `PATH`. Otherwise set
  `cobra4.executable` in VS Code settings to its absolute path.

- Optional extras enable richer features:
  ```bash
  pip install cobra4[aws,data,ssh,yaml]
  ```

## Settings

| Setting | Default | What it does |
|---|---|---|
| `cobra4.executable` | `c4` | Path to the cobra4 CLI. |
| `cobra4.lsp.enabled` | `true` | Run `c4 lsp` for diagnostics + format. |
| `cobra4.format.onSave` | `false` | Run `c4 fmt` on save. |
| `cobra4.check.strict` | `false` | Pass `--strict` to `c4 check`. |

## Quick start

1. Open a folder containing `.c4` files.
2. Open any `.c4` file — diagnostics light up automatically.
3. Press Ctrl/Cmd+F5 to run the active file.

## Snippets cheat sheet

| Trigger | Expands to |
|---|---|
| `fn`         | block-bodied function |
| `fn=`        | inline function |
| `each` / `parallel` | parallel fan-out comprehension |
| `eachwhere`  | comprehension with `where` filter |
| `match`      | full match block |
| `every`      | cron-style scheduler block |
| `serve`      | HTTP handler + `serve ... on :port` |
| `deploylambda` | `deploy ... to aws.lambda(...)` |
| `useStd`     | `use cobra4.stdlib.<name>` |
| `testfn`     | `fn test_*()` recognized by `c4 test` |
| `fleet`      | inventory + parallel SSH |
| `smart`      | `@smart fn ...` decorator |

## Troubleshooting

- **"Could not start cobra4 LSP"** → set `cobra4.executable` to your
  cobra4 CLI path and run `cobra4: Restart Language Server`.
- **Diagnostics not refreshing** → save the file, or run
  `cobra4: Restart Language Server`.
- **Format on save loops** → disable any other formatter for cobra4
  files in your settings.

## License

MIT.
