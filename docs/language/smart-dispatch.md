# Smart dispatch — the heart of cobra4

A `SmartFn` is a function with an **ordered chain of handlers**. Each
handler declares what it matches on — type, URI scheme, file extension,
MIME, predicate. The dispatcher picks the **most specific** match. Ties
at the same priority raise `AmbiguousDispatch` (no silent fallback).

## Built-in smart functions

| `SmartFn`                    | Dispatches on               | Example                            |
|------------------------------|-----------------------------|------------------------------------|
| `read(target)`               | URI scheme + ext + MIME     | `read("./x.json")`, `read("s3://b/k.csv")` |
| `save(value, target)`        | target URI + ext            | `save(rows, "out.parquet")`        |
| `run(cmd, host=h)`           | host (local vs remote)      | `run("uptime", host=h)`            |

## Marking a user function smart

```cobra4
@smart
fn process(target) { return target }   # default fallback

# Add handlers
process.register(fn=fn(x) = ... , type=str, scheme="s3")
process.register(fn=fn(df) = ... , type=DataFrame)
```

## Extending a built-in

```cobra4
use yaml as _yaml

read.register(
    fn=fn(target) = _yaml.safe_load(open(target).read()),
    type=str, scheme="file", ext="yml",
)
```

## Tracing dispatch

Set `COBRA4_TRACE_DISPATCH=1` to log every resolution to stderr.

## Caching

If no handler in a SmartFn uses `when=`, resolutions are cached on
`(type, scheme, ext, mime)`. As soon as **any** handler uses a
predicate, the cache is disabled for the whole SmartFn — the predicate
could depend on values, so a cache hit could be a wrong answer.

## Source

Implementation: [`cobra4/runtime/smart.py`](https://github.com/cobra4-lang/cobra4/blob/main/cobra4/runtime/smart.py).
