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

The sidebar shows the project file tree rooted at the directory where the
IDLE was launched. The snippet library contains built-in Cobra4 building
blocks plus project-custom snippets stored in `cobra4.snippets.json`.
Selecting a snippet and pressing Insert places it at the current editor
line.

The project tree refreshes automatically while the IDLE is open and when
the browser window regains focus. File rows use type-specific icons for
Cobra4 files, directories, Python files, Markdown, JSON, config files,
and generic files. The top bar includes a light/dark theme toggle, stored
locally in the browser.

The Terminal tab runs non-interactive shell commands in the project root,
which is useful for commands such as `git status`, `git push`, `c4 test`,
and `c4 check --strict src/main.c4`.

Files are opened and saved relative to the directory where `c4 idle` was
started, unless an absolute path is supplied.
