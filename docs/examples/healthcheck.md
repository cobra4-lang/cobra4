# Health check

Pings a list of URLs in parallel and aggregates the OK/FAIL counts. Demonstrates `each ... in parallel(workers=N)`, HTTP fetch, and structured `log()` output.

Source: [`examples/02_healthcheck.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/02_healthcheck.c4)

```cobra4
--8<-- "examples/02_healthcheck.c4"
```

## Run it

```bash
c4 run examples/02_healthcheck.c4
```
