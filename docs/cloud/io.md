# Smart `read` and `save`

```cobra4
data = read("./users.csv")                        # → list[dict]
data = read("https://example.com/config.json")    # → dict (auto from Content-Type)
data = read("s3://bucket/data.parquet")           # → list[dict] (with cobra4[data])

save(data, "./users.parquet")
save(data, "s3://bucket/out.json")
```

## Built-in handlers

| Source                                              | Output                       |
|-----------------------------------------------------|------------------------------|
| `*.csv` (file or HTTP `text/csv`)                   | `list[dict]`                 |
| `*.json` (file or HTTP `application/json`)          | `dict` or `list`             |
| `*.jsonl`                                           | `list[Any]`                  |
| `*.txt`, `*.md`, anything else local                | `str`                        |
| `*.parquet`                                         | `list[dict]` (`cobra4[data]`)|

## Atomicity

`save()` is **atomic**: it writes to a temp file in the target's
directory, fsyncs, then `os.replace()`s into place. A crash mid-write
leaves either the old file or the new file — never a half-written one.

## Adding your own

See [Smart dispatch](../language/smart-dispatch.md#extending-a-built-in).
