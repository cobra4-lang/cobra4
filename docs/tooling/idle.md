# Cobra4 IDLE

`c4 idle` starts a local browser-based editor for Cobra4.

```bash
c4 idle
c4 idle --port 0 --no-browser
```

The IDLE uses the same compiler path as `c4 build` and `c4 run`. Its
Python tab shows the generated Python for the current source, and the
graph tab visualizes imports, functions, IO calls, HTTP handlers,
schedules, events, workflow tasks, resources, and deploy targets found in
the Cobra4 AST.

The editor also provides LSP-powered completions, live lint diagnostics,
signature help, hover metadata, formatting, an outline view, and clickable
diagnostics that jump back to the relevant source line.

Files are opened and saved relative to the directory where `c4 idle` was
started, unless an absolute path is supplied.
