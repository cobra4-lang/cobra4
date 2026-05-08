# RFC 0003 — Declarative Infrastructure-as-Code

**Status:** Draft.
**Discussion:** [#TBD](https://github.com/cobra4-lang/cobra4/issues)

## Motivation

cobra4's `deploy` block today is **imperative**: it builds a single
artifact and pushes it. Real infrastructure has dozens of resources
(databases, caches, queues, IAM roles, …) and they reference each
other. Terraform, Pulumi, and CDK all chose a declarative model with
a state store, drift detection, and `plan` / `apply` / `destroy`
phases. cobra4 should join that family.

The leverage: cobra4's smart dispatch, `secret`, `deploy` adapter
framework, and language plugins are **the perfect substrate** — half
the work is already there.

## Proposed surface

```cobra4
resource db = aws.rds.postgres {
    instance_class: "db.t3.medium"
    storage_gb: 100
    name: "prod-orders"
}

resource cache = aws.elasticache.redis {
    node_type: "cache.t3.micro"
    num_nodes: 2
}

resource api = aws.lambda {
    handler: my_handler
    memory: 1024
    timeout: 30
    env: {
        DB_URL: db.connection_string,        # auto-dependency edge
        REDIS:  cache.endpoint,
    }
}
```

`resource X = aws.<service> { ... }` declares a desired piece of
infrastructure. Field references like `db.connection_string` create
**implicit dependencies** in the resource graph, just like Terraform's
`aws_db_instance.main.endpoint`.

## Phases

```bash
c4 infra plan      ./prod.c4    # diff vs. state, show actions
c4 infra apply     ./prod.c4    # execute the diff
c4 infra destroy   ./prod.c4    # tear down everything in state
c4 infra import    ./prod.c4 aws.s3 my-bucket  # bring existing into state
```

State backends: local (`./.cobra4/state.json`), S3 with locking via
DynamoDB, Postgres. Pluggable via the same adapter mechanism that
already exists for `deploy`.

## Adapters

A resource type is just an entry in the registry:

```python
from cobra4.runtime.infra import register

@register("aws.rds.postgres")
class PostgresRDS:
    def plan(self, current, desired): ...
    def apply(self, current, desired): ...
    def destroy(self, current): ...
    def read(self, identity): ...
```

Mirrors the `deploy` adapter contract — minimal cognitive load for
plugin authors who already wrote a deploy adapter.

## Why this is different from Terraform / Pulumi

- **Same language for infra and application**: no HCL ↔ Python ↔ TS
  gap. The lambda handler and the lambda definition live next to each
  other.
- **Smart dispatch composes**: `read("s3://bucket/...")` — the bucket
  came from `resource bucket = aws.s3 { ... }`. cobra4 can wire those
  together.
- **No HCL macro hell**: cobra4 has `each`, `if`, real functions. You
  don't need `count = ...` shenanigans to spawn 5 resources — write a
  loop.

## Open questions

- **Dependency cycles**: detect at parse, error.
- **Drift detection**: do we compare on every plan or trust state?
  Default to compare; let the user disable with `c4 infra plan --fast`.
- **Secret refresh**: a resource's secret reference should re-evaluate
  on apply, not be baked into state.
- **Multi-region / multi-cloud**: a `provider` block analogous to
  Terraform's, scoping resources to a config.

## Estimated effort

This is a project, not a feature. Realistic phases:

- Phase 1 (~1 week): grammar + parser + AST + dependency graph;
  in-memory plan/apply for one adapter (e.g. `aws.s3`); local file
  state backend.
- Phase 2 (~1 week): 4-5 more adapters covering common patterns
  (lambda, RDS, IAM, security group, route53).
- Phase 3 (~1 week): drift detection, import, destroy, S3 backend.
- Phase 4 (open-ended): the long tail of cloud resources, terraform
  registry import bridge, multi-cloud.
