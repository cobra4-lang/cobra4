<img src="docs/assets/logo-128.png" align="right" width="128" alt="cobra4 logo">

# cobra4

**A high-level, cloud-native language transpiled to Python.**

cobra4 promotes patterns common in cloud automation, data pipelines, and
distributed jobs to first-class language constructs. *One line of cobra4
often replaces a small Python program.*

[![CI](https://github.com/cobra4-lang/cobra4/actions/workflows/ci.yml/badge.svg)](https://github.com/cobra4-lang/cobra4/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/cobra4.svg?label=PyPI)](https://pypi.org/project/cobra4/)
[![Open VSX](https://img.shields.io/open-vsx/v/cobra4-lang/cobra4?label=Open%20VSX)](https://open-vsx.org/extension/cobra4-lang/cobra4)
[![Docs](https://img.shields.io/badge/docs-cobra4--lang.github.io-1f6feb)](https://cobra4-lang.github.io/cobra4/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

```bash
pip install cobra4              # CLI: c4 run | build | check | fmt | repl | lsp | serve
```

VS Code extension: install from the
[Open VSX registry](https://open-vsx.org/extension/cobra4-lang/cobra4)
(Cursor, VSCodium, code-server) or from
[`editor/vscode/cobra4`](editor/vscode/cobra4) for a local build. The
official VS Code Marketplace listing is *coming soon*.

---

## Three lines that show what cobra4 *is*

### 1. ETL across formats — `read`/`save` are smart-dispatched

```cobra4
# csv → filter → json. No imports, no boilerplate.
rows   = read("./users.csv")
adults = each r in rows where int(r["age"]) >= 18 { r }
save(adults, "./adults.json")
```

`read` and `save` route on URI scheme + extension + MIME — `s3://`,
`https://`, `parquet`, `jsonl`, anything a library has registered. No
wrapper code per source.

### 2. Webhook server, with auth and pattern-matched routing

```cobra4
fn handler(req) {
    if req?.headers?.authorization != "Bearer secret" {
        return (401, {}, {"error": "nope"})
    }
    match (req.method, req.path) {
        case ("GET",  "/health")     { return {"ok": true} }
        case ("POST", "/users")      { return create_user(req.json()) }
        case _                       { return (404, {}, {}) }
    }
}

serve handler on :8080
```

`serve` boots a real `ThreadingHTTPServer`, JSON-encodes return values,
inferred content types, supports `(status, headers, body)` tuples.

### 3. Scheduled jobs + parallel fan-out, no orchestrator

```cobra4
urls = read("./targets.txt")

every 5 minutes {
    results = each url in urls in parallel(workers=10) { fetch(url) }
    save(results, "s3://bucket/snapshots/{now()}.jsonl")
}
```

`every`, `each ... in parallel`, smart `save` to S3 — built in. Run with
`c4 serve job.c4` and you have a daemon.

---

## The mantra

1. **Readability first** — no esoteric operators (no `|>`), English keywords.
2. **One line = one program** — cloud/distributed patterns are syntax.
3. **General-purpose** — anything Python does, cobra4 does.
4. **Extensible on two axes** — *libraries* extend the runtime,
   *language plugins* (`lang use sql`) extend the parser/AST. Both first-class.

## Quick start

```bash
pip install cobra4

c4 run   examples/03_etl.c4              # transpile + execute
c4 build examples/03_etl.c4 -o etl.py    # transpile only
c4 fmt   examples/03_etl.c4              # canonical format
c4 check examples/03_etl.c4              # parse + types + dispatch overlap
c4 repl                                   # interactive
c4 serve daemon.c4                        # event loop / scheduler / HTTP
c4 test                                   # discover tests/test_*.c4
```

Optional extras: `pip install cobra4[aws,data,ssh,yaml,otel,dev]`.

## Smart dispatch — the heart

Built-in and stdlib functions are **open dispatchers**. Their behavior
depends on argument type, URI scheme, file extension, and MIME — and any
library can extend them at boot or at runtime:

```python
# Python side (in a library):
from cobra4.runtime.io import read
import yaml
read.register(yaml.safe_load, type=str, scheme="file", ext="yml", name="local-yaml")
```

User cobra4 code can opt in with `@smart`:

```cobra4
@smart
fn process(target) { return target }

process.register(scheme="s3", fn=fn(t) { ... })
process.register(type=DataFrame, fn=fn(df) { ... })
```

Specificity wins. Ties at the same priority raise `AmbiguousDispatch`
on the first call — no silent fallbacks. Set `COBRA4_TRACE_DISPATCH=1`
to log every resolution.

## What's shipped

cobra4 is **alpha but real**: 174 tests pass, every example runs end-to-end,
the runtime hardens itself against the obvious foot-guns (atomic `save`,
`shell=False` by default for fleet, paramiko `RejectPolicy`, HTTP bound
to localhost by default, …).

| Surface              | Status |
|----------------------|--------|
| Compiler (Lark + AST + codegen + source-map)                              | ✅ |
| Smart dispatch (`SmartFn`, `@smart`, type/scheme/ext/MIME/predicate)      | ✅ |
| `read`/`save`: csv, json, yaml/yml, jsonl, txt, md, parquet × file/http/s3 | ✅ |
| `each ... in parallel`, `every`, `on event from`, `serve`                | ✅ |
| `match`/`case` with OR-patterns, guards, list/dict/tuple destructure     | ✅ |
| Resolver + gradual type checker + dispatch overlap analysis (`c4 check`) | ✅ |
| Daemon mode (`c4 serve`): scheduler + event poller + ThreadingHTTPServer | ✅ |
| Cloud primitives: `fleet`, `secrets` (env/file/vault/aws-sm/gcp-sm), `deploy` (lambda, gcp.run, k8s, fly) | ✅ |
| Language plugins: `sql`, `regex`, `yaml` (and a public `LanguagePlugin` API) | ✅ |
| Stdlib written in cobra4 itself (`http`, `json`, `fs`, `data`, `time`, `strings`, `cli`, `test`) with mtime-cached import hook | ✅ |
| LSP (`c4 lsp`): diagnostics, hover, go-to-def, references, completion, format | ✅ |
| Tooling: REPL with completion + history, formatter, `c4 doc` markdown, `c4 deps`, `c4 plugin` | ✅ |
| VS Code extension (`editor/vscode/cobra4`) — packaged `.vsix`            | ✅ |
| Marketplace publish + PyPI publish                                        | 🚧 |

## Operational env vars

| Var | Effect |
|---|---|
| `COBRA4_TRACE_DISPATCH=1` | Log every `SmartFn` resolution to stderr. |
| `COBRA4_HTTP_BIND=0.0.0.0` | Override daemon HTTP bind (default `127.0.0.1`). |
| `COBRA4_SSH_HOST_KEY_POLICY=auto` | Use paramiko `AutoAddPolicy` (default `RejectPolicy`). |
| `COBRA4_DEPLOY_DRY_RUN=0` | Actually invoke deploy adapters (default dry-run). |
| `COBRA4_LOG_FORMAT=json` | Switch `log()` from `key=value` to JSON-line. |
| `COBRA4_OTEL_EXPORT=1` | Forward log records to OTel (requires `cobra4[otel]`). |
| `COBRA4_SECRETS_BACKEND` | `env` \| `file` \| `vault` \| `aws-sm` \| `gcp-sm`. |
| `COBRA4_QUEUE_BACKEND` | `memory` \| `file` \| `sqs` \| `redis`. |
| `COBRA4_SQL_URL` | Default SQLAlchemy URL for the `sql` plugin. |

## Project layout

```
cobra4/
  cli.py             # CLI: run, build, fmt, check, repl, lsp, serve, test, …
  grammar.lark       # LALR(1) grammar
  lexer.py           # Lark wrapper + bracket-aware postlex
  parser.py          # Tree → AST transformer
  ast_nodes.py       # AST dataclasses
  resolver.py        # Scope check + lvalue validation
  typecheck.py       # Gradual type checker (advisory)
  dispatch_analysis.py  # Smart-dispatch overlap detector
  lowering.py        # Surface AST → core AST
  codegen.py         # Core AST → Python source
  source_map.py      # Line:col → line:col mapping
  import_hook.py     # `.c4` import + mtime-keyed bytecode cache
  runtime/           # smart, io, concurrency, fleet, secrets, deploy, http, queues, schedule, observe
  stdlib/            # http.c4, json.c4, fs.c4, data.c4, time.c4, strings.c4, cli.c4, test.c4
  plugins/           # builtin: sql, regex, yaml (+ LanguagePlugin API)
  tools/             # repl, fmt, lsp
examples/            # 10 end-to-end programs
tests/               # 174 passing
editor/vscode/       # VS Code extension (TextMate + LSP client)
```

## AI assistants

If you let an LLM write cobra4 code, point it at
[`AI_HELPER.md`](AI_HELPER.md) — a structured spec of what the language
does and does *not* support, designed to be consumed by an assistant
without prose narrative.

## License

MIT.
