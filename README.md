<img src="docs/assets/logo-128.png" align="right" width="128" alt="cobra4 logo">

# cobra4

A high-level, cloud-native language transpiled to Python.

cobra4 promotes patterns common in cloud automation, data pipelines, and
distributed jobs to first-class language constructs. A single line of
cobra4 often replaces a small Python program.

```cobra4
# read auto-detects format & source
users  = read("./data/users.csv")
config = read("https://api.example.com/config.json")
adults = read("s3://acme/people.parquet")

# parallel fan-out, no boilerplate
results = each url in urls in parallel(workers=10) { fetch(url) }

# scheduling as a block
every 5 minutes {
    ingest()
}

# pattern matching
match resp.status {
    case 200 { handle(resp.json) }
    case _   { log.warn("unexpected", code=resp.status) }
}
```

## Mantra

1. **Readability first** — no esoteric operators (no `|>`), English keywords.
2. **One line = one program** — cloud/distributed patterns are syntax.
3. **General purpose** — anything Python does, cobra4 does.
4. **Extensible** — *libraries* (extend the runtime) and *language plugins*
   (extend the parser/AST) are distinct, both first-class.

## Quick start

```bash
pip install -e .[aws,data,dev]

c4 run   examples/03_etl.c4              # transpile + execute
c4 build examples/03_etl.c4 -o etl.py    # transpile only
c4 fmt   examples/03_etl.c4              # canonical format
c4 check examples/03_etl.c4              # parse + scope check
c4 repl                                   # interactive
```

## The cobra4 mentality: smart functions

Built-in and stdlib functions are **open dispatchers**. Their behavior
depends on argument type, URI scheme, file extension, and MIME content
type — and any library can extend them at boot or at runtime:

```python
# Python side (in a library)
from cobra4.runtime.io import read
import yaml
read.register(yaml.safe_load, type=str, scheme="file", ext="yml", name="local-yaml")
```

User cobra4 code can opt-in for the same behavior with the `@smart` decorator:

```cobra4
@smart
fn process(target) { return target }
process.register(scheme="s3", fn=fn(t) { ... })
process.register(type=DataFrame, fn=fn(df) { ... })
```

Specificity wins. Ties at the same priority raise `AmbiguousDispatch`
on the first call — no silent fallbacks. See [`cobra4/runtime/smart.py`](cobra4/runtime/smart.py).

## What's shipped

### M1 — Core MVP (✅)

End-to-end working pipeline with a small but real surface:

- **Syntax**: brace-based blocks, English keywords, `?.` and `??` only.
- **Statements**: `if`/`elif`/`else`, `while`, `for`, `each ... (in parallel)?`,
  `every`, `on event from`, `match`/`case`, `try`/`catch`/`finally`,
  `serve`, `deploy`, `use`, `fn`, `class`.
- **Expressions**: full Python-like precedence; `?.` safe-nav; `??` default;
  string interpolation `"hello {name}"`; lambdas `fn(x) = ...` / `fn(x) { ... }`.
- **Smart dispatch core** (`runtime/smart.py`): `SmartFn`, `@smart`,
  `.register(...)`, type/scheme/ext/MIME/predicate matching, ambiguity
  detection, resolution caching.
- **`read` / `save`**: smart-dispatch IO. Stdlib handlers for `csv`, `json`,
  `jsonl`, `txt`, `md`, `parquet` × `./` `file://` `https://` `s3://`.
- **Concurrency**: `each ... in parallel { ... }` → thread/process pool fan-out.
- **CLI**: `c4 run | build | fmt | check | repl`. `serve` / `doc` / `deps` /
  `plugin` are stubbed (planned milestones).
- **Source map**: line-to-line cobra4↔Python; tracebacks point back to source.
- **Test suite**: ~80 tests covering lexer, parser, codegen, runtime,
  dispatcher, CLI, and end-to-end execution of every example.

### M2 — Tipi & lint (✅)

- **Resolver** with nested function/class/block scopes; warnings for
  undefined names and outer-scope shadowing.
- **Gradual type checker** ([typecheck.py](cobra4/typecheck.py)):
  honors annotations, infers literal/binop types, warns on call-site /
  return-type / default mismatches. Stays advisory — code still runs.
- **Dispatch static analysis** ([dispatch_analysis.py](cobra4/dispatch_analysis.py)):
  flags two `read.register(...)` (or any SmartFn) calls with overlapping
  dispatch keys at the same priority — runtime AmbiguousDispatch likely.
- `c4 check` wires all three; flags `--strict / --no-types / --no-shadowing`.

### M3 — Cloud primitives (✅)

- **`fleet`** ([runtime/fleet.py](cobra4/runtime/fleet.py)): TOML inventory
  (groups, glob patterns), `Host` dataclass, `run(cmd, host=...)` dispatching
  to local subprocess or system `ssh`, `fan_out` parallel helper.
- **`secrets`** ([runtime/secrets.py](cobra4/runtime/secrets.py)): pluggable
  backends — `env`, `file`, `vault` (hvac), `aws-sm` (boto3), `gcp-sm`.
  Selection via `COBRA4_SECRETS_BACKEND` or `[secrets]` in `cobra4.toml`.
- **`deploy`** ([runtime/deploy.py](cobra4/runtime/deploy.py)): adapter
  builder pattern (`aws.lambda(region="x")`), `register_adapter()`, dry-run
  by default (set `COBRA4_DEPLOY_DRY_RUN=0` to live), `env_from(".env")`.

### M4 — Daemon & event loop (✅)

- **`c4 serve FILE`**: imports the module to register `every` / `on event` /
  `serve` callbacks, then runs the scheduler + event poller + HTTP servers
  in dedicated threads until SIGINT.
- **HTTP**: `serve handler on :8080` boots `ThreadingHTTPServer`, request
  → `Request(method, path, params, headers, body)`, JSON-encode return value.
- **Queues**: built-in `InMemoryQueue` with `.put` / `.poll`; the
  `EventSource` protocol (`poll(timeout)`) lets users plug SQS/Rabbit/Kafka.

### M5 — Language plugins (✅)

- **Plugin contract** ([plugins/api.py](cobra4/plugins/api.py)):
  `LanguagePlugin(name, transform_source, runtime_module, builtins, description)`.
- **Loader** ([plugins/loader.py](cobra4/plugins/loader.py)) scans
  `lang use NAME` directives at the top of a file, auto-imports
  `cobra4.plugins.builtin.<name>` or `cobra4_lang_<name>`, and pre-processes
  the source before the main parser.
- **Reference plugin** ([plugins/builtin/sql.py](cobra4/plugins/builtin/sql.py)):
  rewrites `sql { SELECT ... }` blocks to `sql_run("...")` runtime calls.
- `c4 plugin list` prints registered plugins.

### Post-M5 expansions (✅)

**Wave 1 — syntax fills**:
- Slice indexing: `xs[a:b]`, `xs[:b]`, `xs[a:]`, `xs[a:b:c]`, `xs[:]`.
- `where` filter on `each` and `for` (comprehension-style).
- OR-pattern (`case 1 | 2 | 3`) and guard (`case x if cond`) in `match`.

**Wave 2 — tooling (M6)**:
- Multi-line REPL ([tools/repl.py](cobra4/tools/repl.py)) with bracket-aware continuation.
- Canonical formatter ([tools/fmt.py](cobra4/tools/fmt.py)) — re-emits cobra4 from the AST, idempotent.
- LSP server ([tools/lsp.py](cobra4/tools/lsp.py)) on stdio: diagnostics from
  parser+resolver+typecheck, formatting, hover with type info. Run with `c4 lsp`.

**Wave 3 — more language plugins**:
- `lang use regex` ([plugins/builtin/regex.py](cobra4/plugins/builtin/regex.py)) — `p = re"[a-z]+"i` literals.
- `lang use yaml` ([plugins/builtin/yaml.py](cobra4/plugins/builtin/yaml.py)) — inline YAML literals via `yaml"""..."""`.

**Wave 4 — cloud hardening**:
- Paramiko SSH backend with key-agent + sftp, fallback to system `ssh`. Install via `cobra4[ssh]`.
- AWS Lambda packaging: deterministic zip with vendored cobra4 runtime, idempotent create-or-update.
- OpenTelemetry log export: set `COBRA4_OTEL_EXPORT=1` after `pip install cobra4[otel]`.

**Wave 5 — stdlib + packaging (M7)**:
- `c4 deps add | list | remove | install` — manages `[deps]` in `cobra4.toml`, calls pip for install.
- `c4 doc FILE` — extracts docstrings + signatures to markdown.
- **Stdlib written in cobra4 itself**: [cobra4/stdlib/http.c4](cobra4/stdlib/http.c4),
  [cobra4/stdlib/json.c4](cobra4/stdlib/json.c4), [cobra4/stdlib/fs.c4](cobra4/stdlib/fs.c4).
  Loaded via a custom Python import hook with `__pycache__/*.cobra4.pyc` mtime-based caching —
  re-import is a single file read, not a re-transpile.

### Hardening pass (post-review)

A code-review pass identified 14 issues; the ones with runtime impact were
fixed and locked in with regression tests in [test_review_fixes.py](tests/test_review_fixes.py):

- **SmartFn cache bypassed when any handler uses `when=`** — the (type,
  scheme, ext, mime) cache key is unsafe in the presence of value-dependent
  predicates. The dispatcher now detects this and re-resolves on every call;
  cache stays active when no handler uses `when=`.
- **`?.` resolves dict keys** — `req?.params?.name` works whether `params`
  is an object (`getattr`) or a dict/Mapping (`.get`). Missing attribute
  returns `None` rather than raising, so `??` composes cleanly.
- **`c4 fmt` is plugin-aware** — `lang use NAME` directives and
  plugin-specific blocks (`sql {...}`, `re"..."`, `yaml"""..."""`) are
  preserved verbatim through formatting via the new
  `LanguagePlugin.preserve_for_format` hook.
- **`c4 check` knows plugin builtins** — the resolver now accepts
  `extra_builtins` from the active plugins, so `sql_run` etc. don't get
  flagged as undefined.
- **`dispatch_analysis` flags semantic overlaps** — not just identical
  keys: also a generic handler subsuming a specific one at the same priority
  (D002), and `when=` predicates with no other constraints (D003, since
  they invalidate the cache for the whole SmartFn).
- **`fleet.run` is `shell=False` by default** — explicit opt-in with
  `shell=True` for shell features. Argv form (list) is the recommended
  path. Removes injection surface.
- **paramiko uses `RejectPolicy` by default** — unknown host keys are
  rejected. Override per-host with `host_key_policy="auto"` in the
  inventory `extra` map, or globally via `COBRA4_SSH_HOST_KEY_POLICY=auto`.
- **`save()` is atomic** — writes to a temp file in the target's directory,
  fsyncs, then `os.replace()`. Crash mid-write leaves either old file or
  new file, never a half-written one.
- **HTTP server**: binds `127.0.0.1` by default (override with
  `COBRA4_HTTP_BIND=0.0.0.0`); response content-type is inferred from
  return type (dict/list → JSON, str → text/plain, bytes → octet-stream);
  handlers can return `(status, headers, body)` tuples; `Request` exposes
  `.json()` and `.text()` body decoders.
- **Stdlib import hook caches** — `__pycache__/<name>.cobra4.pyc` keyed
  on source mtime+size. Edit `.c4`, next import re-transpiles; otherwise
  loads from cache.
- **Dispatch tracing** — set `COBRA4_TRACE_DISPATCH=1` to get a one-line
  log of every smart-fn resolution. Makes the "smart" routing observable.

### Operational env vars

| Var | What it does |
|---|---|
| `COBRA4_TRACE_DISPATCH=1` | Log every `SmartFn` resolution to stderr. |
| `COBRA4_HTTP_BIND=0.0.0.0` | Override the daemon HTTP bind address (default `127.0.0.1`). |
| `COBRA4_SSH_HOST_KEY_POLICY=auto` | Use paramiko `AutoAddPolicy` (default is `RejectPolicy`). |
| `COBRA4_DEPLOY_DRY_RUN=0` | Actually invoke deploy adapters (default is dry-run). |
| `COBRA4_LOG_FORMAT=json` | Switch `log()` from key=value to JSON-line. |
| `COBRA4_OTEL_EXPORT=1` | Forward log records to OTel (requires `cobra4[otel]`). |
| `COBRA4_SECRETS_BACKEND=env\|file\|vault\|aws-sm\|gcp-sm` | Pick a secrets backend. |
| `COBRA4_SECRETS_DIR=…` | Override the file-backend root (default `~/.cobra4/secrets/`). |
| `COBRA4_LAMBDA_ROLE=arn:…` | IAM role ARN for AWS Lambda deploys. |
| `COBRA4_SQL_URL=postgresql://…` | Default SQLAlchemy URL for the `sql` plugin. |
| `COBRA4_QUEUE_BACKEND=memory\|file\|sqs\|redis` | Default backend for `queue("name")`. |
| `COBRA4_FILE_QUEUE_DIR=…` | Where the FileQueue stores events. |
| `COBRA4_REDIS_URL=redis://…` | Connection URL for the Redis queue backend. |

### "Make it real" pass (✅)

Removes every stub and aspirational feature. After this pass, every
claim in this README has working code and regression tests.

- **Pattern matching completed**: `*rest` in lists, `**rest` in dicts,
  `(a, b)` tuple destructure. Already shipped: OR-patterns, guards.
- **Local `.c4` modules**: `use mymodule` resolves `mymodule.c4` from
  `sys.path` via [import_hook.py](cobra4/import_hook.py), with
  mtime-keyed bytecode cache.
- **Parser error recovery**: `parse_collect_errors()` reports multiple
  diagnostics in one shot.
- **Cloud adapters made real**: `gcp.run` builds Docker + `gcloud`,
  `k8s` generates manifests + `kubectl apply`, `fly` calls `flyctl`,
  `aws.lambda` packages with vendored runtime + boto3 create/update.
- **SQL plugin executes**: `configure(url)` or `COBRA4_SQL_URL` enables
  real SQLAlchemy execution with `:name` parameters.
- **Queue backends real**: `InMemoryQueue`, `FileQueue` (durable),
  `SQSQueue`, `RedisQueue`.
- **Test framework**: `c4 test` discovers/runs `tests/test_*.c4` with
  pytest-style output, optional JUnit XML.
- **LSP completed**: go-to-definition, find-references,
  document-symbols, completion (in addition to existing diagnostics + format + hover).
- **Source map column-precise**: tracebacks point to `line:col`.
- **REPL**: tab completion + history (`~/.cobra4/history`).
- **`c4 run --watch`**, **`c4 deps install --venv`**,
  **`c4 plugin add NAME`**, **`c4 doc --html`**.
- **Stdlib expanded**: `http` (Session, retries, auth), `fs` (walk,
  copy, copytree, move), `data` (group_by, aggregate, join, sort_by),
  `time` (parse/fmt durations), `strings` (slugify, camel_to_snake),
  `cli` (App builder), `test` (assertion DSL).

**End-to-end examples that exercise everything**:
- [09_log_analyzer.c4](examples/09_log_analyzer.c4) — log parsing →
  per-status & per-path aggregations → JSON report.
- [10_webhook_router.c4](examples/10_webhook_router.c4) — HTTP server
  with bearer auth, pattern-matched routing, real SQLite via the `sql`
  plugin.

**176 tests, all green. Every example runs end-to-end.**

See `~/.claude/plans/voglio-realizzare-cobra4-un-curried-lark.md` for the
full roadmap.

## Project layout

```
cobra4/
  cli.py             CLI: run, build, fmt, check, repl
  grammar.lark       LALR(1) grammar
  lexer.py           Lark wrapper + bracket-aware postlex
  parser.py          Tree → AST transformer
  ast_nodes.py       AST dataclasses
  resolver.py        Scope check, lvalue validation
  lowering.py        Surface AST → core AST (M1: identity)
  codegen.py         Core AST → Python source
  source_map.py      Line-to-line mapping
  runtime/
    smart.py         SmartFn / @smart / open dispatch (the heart)
    io.py            read / save with stdlib handlers
    concurrency.py   parallel_for
    observe.py       structured log
    core.py          ?., ??, every/on_event/serve/deploy registries
examples/            01-05 end-to-end programs
tests/               lexer, parser, codegen, runtime, CLI, examples
```

## License

MIT.
