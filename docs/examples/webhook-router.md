# Webhook router

HTTP server with bearer auth, pattern-matched routes (`match (method, path)`), and SQLite via the `sql` language plugin. The most ambitious end-to-end example — exercises pretty much every cobra4 feature in one file.

Source: [`examples/10_webhook_router.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/10_webhook_router.c4)

```cobra4
--8<-- "examples/10_webhook_router.c4"
```

## Run it

```bash
c4 run examples/10_webhook_router.c4
```
