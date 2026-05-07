# Fleet command

Loads an inventory from `cobra4.toml` (or programmatically), runs a command over each host in parallel, aggregates `CommandResult`s. Demonstrates the `Host` / `inventory` / `run(host=...)` triad.

Source: [`examples/06_fleet.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/06_fleet.c4)

```cobra4
--8<-- "examples/06_fleet.c4"
```

## Run it

```bash
c4 run examples/06_fleet.c4
```
