# Concurrency: `each ... in parallel`

The fastest way to fan out an I/O- or CPU-bound workload:

```cobra4
# Default: thread pool, sensible default worker count
results = each url in urls in parallel { fetch(url) }

# Tuned thread count for I/O
fetched = each url in urls in parallel(workers=50) { fetch(url) }

# Process pool for CPU-bound work
crunched = each i in big_list in parallel(mode="process") { compute(i) }
```

`each ... in parallel` produces a list — the order of results matches
the order of inputs.

## Where the implementation lives

[`cobra4/runtime/concurrency.py`](https://github.com/cobra4-lang/cobra4/blob/main/cobra4/runtime/concurrency.py).

## Async / await

cobra4 does **not yet** have native `async fn` / `await`. For async
work, drop into Python `asyncio` via `use asyncio` and call from
cobra4 — `each ... in parallel` covers most cloud-pattern use cases.
