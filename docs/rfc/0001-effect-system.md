# RFC 0001 — Effect / capability system

**Status:** Draft. Targets a future cobra4 release.
**Discussion:** [#TBD](https://github.com/cobra4-lang/cobra4/issues)

## Motivation

Today a cobra4 function's signature tells you *what arguments it takes*
and *what it returns*. It does not tell you *what it can do* — read a
file, hit the network, write to a database, mutate global state, sleep
for an hour. For a language whose pitch is "deploy this to a Lambda /
serverless container", that's a missing dimension.

If the type system tracks **effects**, three things become possible:

1. **Sandboxed execution** — host an untrusted `.c4` script and grant
   it `[http]` only. The runtime refuses to load anything that calls
   `read("./local")` or `secret(...)`.
2. **Cost-aware deploy** — adapter sees `with [http, db]` and provisions
   the matching IAM policy / VPC config. No more "deploy succeeded but
   the lambda 403s on prod because IAM is wrong".
3. **Compiler optimization** — a `with []` (pure) function is
   memoizable, parallelizable without locks, etc.

## Proposed surface

```cobra4
fn pure_double(x) -> int with [] = x * 2

fn fetch_user(id: str) -> User with [http]
    = http.get("https://api.example.com/users/{id}")

fn store(u: User) with [db, log]
    { db.insert(u); log("stored", id=u.id) }

fn pipeline(id) with [http, db, log]
    {
        u = fetch_user(id)
        store(u)
    }
```

The `with [...]` clause is **part of the function type**. A call site
is checked against the caller's effect set:

```cobra4
fn handler(req) with [http, db, log] {
    pipeline(req.id)        # OK — caller's effects ⊇ callee's
}

fn pure_helper(x) with [] {
    pipeline(x)             # ERROR — pure context cannot use [http, db, log]
}
```

## Built-in effects

| Effect    | Granted by                                                        |
|-----------|-------------------------------------------------------------------|
| `http`    | `fetch`, `cobra4.stdlib.http`                                     |
| `fs`      | `read("./...")`, `save("./...")`, `cobra4.stdlib.fs`              |
| `db`      | The `sql` plugin's `sql_run`, any registered DB handler           |
| `log`     | `log(...)` calls                                                  |
| `secret`  | `secret(...)` calls                                               |
| `ssh`     | `run(host=...)` against a remote                                  |
| `time`    | `time.sleep`, `every`, `on event from queue(...)`                 |
| `deploy`  | The `deploy` builder family                                       |

## Inference

Most user functions don't need an explicit `with` — the checker
infers from the body (the union of all callees' effects). Annotation
becomes interesting at module boundaries (public API) and for
sandboxing.

## Inference vs annotation conflict

If the user writes `fn f() with [log]` but the body calls
`http.get(...)` (which has `[http]`), the checker reports a
**`E001: missing effect`** error and lists which call introduced
which effect. Not a warning — annotation is a contract.

## Sandboxing

A new top-level `sandbox` keyword:

```cobra4
sandbox [http, log] {
    use my_untrusted_module
    my_untrusted_module.run()
}
```

Inside the block, only `http` and `log` are granted. The cobra4
runtime injects a guard around every effectful builtin that checks
the active effect mask. Violation raises `EffectViolation` at runtime
(belt and braces — the static check should have caught it, but
dynamic plugin code might evade).

## Implementation sketch

1. **Grammar**: extend `fn_decl` with optional `"with" "[" effect_list? "]"`
   after `return_type?`.
2. **AST**: add `effects: list[str]` to `FnDecl`.
3. **Type checker**: extend `_FnSig` to carry effects. New pass:
   for each fn body, compute the union of called fns' effect sets.
   Report mismatch.
4. **Runtime**: `with_effects(*names) {...}` context manager (Python)
   maintained as a thread-local. Sandbox wraps a Python `exec` in this
   manager. Built-in helpers check the mask and raise on violation.
5. **Codegen**: emit an explicit decorator on each fn that records
   declared effects (for runtime sandboxing). No-op when sandboxing
   is disabled.

## Open questions

- **Effect polymorphism** (`fn map(xs, fn) with effects_of(fn)`):
  needed for higher-order functions to be useful. May need a tiny
  effect-variable language.
- **Existing functions with no annotation**: do they default to
  `with [*]` (any effect)? Or are they audit-required?
- **The `each ... in parallel` case**: does the parallel scope
  inherit caller effects? Probably yes, no special rule.

## Estimated effort

~3-4 days for a usable v1 (declared annotations only, no inference,
no sandboxing). Sandboxing + inference doubles that.
