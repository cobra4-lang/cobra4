# Contributing to cobra4

Thanks for considering a contribution. cobra4 is alpha — every bug
report, doc fix, and small PR is genuinely welcome.

## Quick map

```
cobra4/                # the package
  cli.py               # CLI entry point (run, build, fmt, check, repl, lsp, serve, test, …)
  grammar.lark         # LALR(1) grammar
  parser.py            # Tree → AST transformer (+ parse_collect_errors recovery)
  ast_nodes.py         # AST dataclasses
  resolver.py          # Scope check + lvalue validation
  typecheck.py         # Gradual type checker (advisory) + flow narrowing
  dispatch_analysis.py # Smart-dispatch overlap detector
  lowering.py          # Surface AST → core AST
  codegen.py           # Core AST → Python source
  source_map.py        # line:col mapping
  import_hook.py       # `.c4` import + mtime-keyed bytecode cache
  runtime/             # smart, io, concurrency, fleet, secrets, deploy, http, queues, schedule, observe
  stdlib/              # cobra4-written stdlib (.c4 files)
  plugins/             # builtin: sql, regex, yaml (+ LanguagePlugin API)
  tools/               # repl, fmt, lsp
docs/                  # mkdocs source — published to cobra4-lang.github.io
examples/              # 10 end-to-end programs runnable with `c4 run`
tests/                 # pytest — must stay green
editor/vscode/cobra4/  # VS Code extension (TextMate + LSP client)
AI_HELPER.md           # spec for AI assistants writing cobra4 — keep accurate
```

## Setup

```bash
git clone https://github.com/cobra4-lang/cobra4.git
cd cobra4
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,yaml]"
pytest -q                       # must be green
```

For the docs site:

```bash
pip install mkdocs-material
mkdocs serve   # http://127.0.0.1:8000
```

For the VS Code extension:

```bash
cd editor/vscode/cobra4
npm install
# F5 in VS Code launches a dev host with the extension active.
```

## Workflow

1. **Open an issue first** for anything beyond a one-line typo. We'd
   rather agree on the shape of a change before you spend an evening on
   it.
2. **Branch from `main`**: `git checkout -b fix-<short-name>`.
3. **Tests required** for any code change. Bugfixes ship with a
   regression test that fails *without* the fix and passes *with* it.
4. **Run before pushing**:
   ```bash
   pytest -q                     # all tests
   c4 check examples/*.c4        # examples still parse & lint
   mkdocs build --strict         # if you touched docs/
   ```
5. **Commit message**: `<area>: <imperative one-line summary>` — e.g.
   `parser: support trailing comma in case patterns`. Body explains
   *why*, not *what* (the diff already shows what).
6. **Open a PR** against `main`. CI must be green.

## Style

- **Python**: 4-space indent, type hints on public APIs, `black`-clean.
- **cobra4**: `c4 fmt -w` is the source of truth for `.c4` formatting.
- **Tests**: pytest, no class wrappers unless fixtures demand it.
- **Comments**: explain *why*, not *what*. If the code needs a comment
  to be readable, it probably needs renaming first.

## What's especially welcome

- Bug reports with a minimal reproducer (`.c4` snippet + expected vs
  actual).
- New built-in language plugins (see [docs/plugins/authoring.md](docs/plugins/authoring.md)).
- Stdlib expansions written *in cobra4* under [`cobra4/stdlib/`](cobra4/stdlib/).
- Real-world examples in `examples/` — short, complete, exercising
  multiple features end-to-end.
- Docs improvements. Anything in `docs/` ships to GitHub Pages
  automatically on merge to `main`.

## What's discouraged

- "Drive-by" PRs touching dozens of files for cosmetic reasons.
- New language syntax without a discussion issue first — surface area
  changes are commitments.
- Removing `read.register(...)` patterns to "simplify" — smart dispatch
  is the design, not an accident.

## Reporting a security issue

Please do **not** open a public issue. Email the maintainers via the
contact in the GitHub org settings. We'll respond within 7 days.

## License

By contributing you agree your code is released under cobra4's
[MIT license](LICENSE).
