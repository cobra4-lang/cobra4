# Development workflow

A Cobra4 project usually starts with a template, then uses the same
compiler path from the CLI, Studio, tests, and CI.

## Start a project

```bash
c4 init my-service --template http-service
cd my-service
c4 doctor
c4 studio
```

Useful templates:

| Template | Use it for |
|---|---|
| `http-service` | API handlers and `serve` workflows. |
| `etl-pipeline` | `read` / transform / `save` data jobs. |
| `daemon` | `every`, `on event from`, scheduler, and queue work. |
| `agent` | LLM agent examples with a mock provider. |

List templates with:

```bash
c4 init --list
```

## Daily loop

```bash
c4 check src/main.c4 --strict
c4 fmt src/main.c4 -w
c4 run src/main.c4
c4 test
```

Use `c4 run FILE --watch` while iterating on one entry point. Use
`c4 studio` when you want the project tree, snippets, terminal, graph,
and generated Python/source-map view in one place.

## Inspect generated Python

```bash
c4 build src/main.c4 -o build/main.py --source-map
```

Studio shows the same generated Python interactively and highlights the
mapping between Cobra4 source lines and generated Python lines.

## Dependencies and plugins

`cobra4.toml` is the project root signal. It can declare runtime
dependencies, language plugins, secrets backend, and host inventory.

```bash
c4 deps add requests --version 2.31.0
c4 deps install --venv
c4 plugin list
c4 doctor
```

`c4 doctor` checks declared dependencies and configured language plugins,
then reports missing optional extras as informational warnings.

## Daemons and services

For long-running files that contain `every`, `on event from`, or `serve`
handlers:

```bash
c4 serve src/main.c4
```

During development you can still smoke-test the same file with:

```bash
c4 run src/main.c4
c4 check src/main.c4 --strict
```

## CI baseline

A minimal CI job should run:

```bash
c4 doctor
c4 check src/main.c4 --strict
c4 test
```

For libraries or deployment scripts, also build the generated Python:

```bash
c4 build src/main.c4 -o build/main.py --source-map
```
