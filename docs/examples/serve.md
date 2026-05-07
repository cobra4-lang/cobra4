# HTTP server

A minimal `serve handler on :PORT` daemon. Run with `c4 serve examples/04_serve.c4` and it boots a `ThreadingHTTPServer` that JSON-encodes return values.

Source: [`examples/04_serve.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/04_serve.c4)

```cobra4
--8<-- "examples/04_serve.c4"
```

## Run it

```bash
c4 run examples/04_serve.c4
```
