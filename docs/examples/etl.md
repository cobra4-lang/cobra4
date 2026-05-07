# ETL across formats

Materializes a tiny CSV, reads it back through smart dispatch (`csv → list[dict]`), filters with a `for` loop, and saves to JSON — same `save()` builtin picks the JSON handler from the extension. Demonstrates the smart-dispatch read/save roundtrip.

Source: [`examples/03_etl.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/03_etl.c4)

```cobra4
--8<-- "examples/03_etl.c4"
```

## Run it

```bash
c4 run examples/03_etl.c4
```
