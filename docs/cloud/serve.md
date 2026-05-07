# Daemon mode: `c4 serve`

`c4 serve FILE` imports `FILE`, registers any `every` / `on event from` /
`serve` callbacks the module declares, then runs the scheduler + event
poller + HTTP server until `SIGINT`.

## HTTP

```cobra4
fn handler(req) {
    name = req?.params?.name ?? "world"
    return {"hello": name}
}

serve handler on :8080
```

The handler receives a `Request` with: `method`, `path`, `params` (query
string, decoded), `headers` (lower-cased), `body` (bytes). Helpers:
`req.json()`, `req.text()`.

Return value conventions:

| Return type                  | Behavior                              |
|------------------------------|---------------------------------------|
| `dict` / `list`              | 200, JSON-encoded                     |
| `str`                        | 200, `text/plain`                     |
| `bytes`                      | 200, `application/octet-stream`       |
| `(status, body)`             | custom status, body type-inferred     |
| `(status, headers, body)`    | full control                          |

Default bind is `127.0.0.1`. Override with `COBRA4_HTTP_BIND=0.0.0.0`.

## Scheduling

```cobra4
every 5 minutes { rotate_logs() }
every 1 hours   { backup() }
```

## Event consumers

```cobra4
on event from queue("orders") {
    log("order", id=event.id)
    process(event)
}
```

The backend of `queue("name")` is selected via `COBRA4_QUEUE_BACKEND`
(`memory` / `file` / `sqs` / `redis`).

## Where to look

[`cobra4/runtime/schedule.py`](https://github.com/cobra4-lang/cobra4/blob/main/cobra4/runtime/schedule.py).
