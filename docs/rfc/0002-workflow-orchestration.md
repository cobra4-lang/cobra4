# RFC 0002 — Workflow orchestration

**Status:** Draft.
**Discussion:** [#TBD](https://github.com/cobra4-lang/cobra4/issues)

## Motivation

cobra4 already has the cloud-native primitives (`every`, `on event`,
`each ... in parallel`, `serve`). What's missing for serious data /
deploy pipelines is **DAG orchestration with retries and dependencies**
— the territory of Airflow, Prefect, Temporal, Dagster.

Today a cobra4 user who needs that builds it by hand: thread pools,
custom retry decorators, manual dependency wiring. A first-class
construct lifts that into the language.

## Proposed surface

```cobra4
workflow daily_etl {
    raw      = task fetch_from_api(retries=3, timeout=60s)
    cleaned  = task clean(raw)              # depends on raw
    enriched = task enrich(cleaned)         # depends on cleaned
                  parallel=10               # internal fan-out
    save(enriched, "s3://my-bucket/out.parquet")
}

# Run once
run_workflow(daily_etl)

# Schedule
every 1 days at "03:00" {
    run_workflow(daily_etl)
}
```

Three new keywords:

| Keyword     | Role                                                    |
|-------------|---------------------------------------------------------|
| `workflow`  | Defines a named DAG block.                              |
| `task`      | Declares a node — its body is the work, the args are deps. |
| `parallel`  | Inside a task, fans out the work over the input list.   |

## Semantics

- Each `task X` produces a value bound to a name in the workflow scope.
- The DAG is built **at workflow construction**: `clean(raw)` makes
  `cleaned` depend on `raw`. The runner topologically sorts and
  executes.
- **Retries** and **timeout** are first-class on every task.
- A failed task with `on_failure` clause fans out to the recovery
  branch; otherwise the workflow surfaces an `Err`.
- Step results are cached so reruns can resume from the failure point.

## Open questions

- **Distributed execution**: in v1 the runner is single-process. v2
  could plug into Temporal / Argo / a custom worker pool.
- **State**: where do step results live during retry? Default in
  `~/.cobra4/workflows/<name>/<run_id>/` (file backend), pluggable.
- **Conditional branches**: `if cond { task ... }` inside a workflow?
  Probably yes, lowering to runtime choice.

## Estimated effort

~2-3 days for the in-process runner, parser, basic retry/timeout/cache.
Distributed execution is a separate project.

## Why this beats Airflow / Prefect for cobra4 users

- **Less boilerplate**: no decorator forest, no
  `with Flow("name") as flow:` ceremony.
- **Same language for the workflow and the tasks**: no Python-DSL gap.
- **Native to the cloud primitives**: `each in parallel`, `secret`,
  `read`/`save` are already smart-dispatched. Tasks compose with them
  naturally.
- **Strong typing**: the task return type flows into the next task's
  input, checked at compile time.
