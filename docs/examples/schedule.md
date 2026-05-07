# Scheduling

`every 1 seconds` registers a callback; the example then drives the registry manually with `run_scheduled_once()` for testability. In production, `c4 serve` runs the scheduler in a real loop.

Source: [`examples/05_schedule.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/05_schedule.c4)

```cobra4
--8<-- "examples/05_schedule.c4"
```

## Run it

```bash
c4 run examples/05_schedule.c4
```
