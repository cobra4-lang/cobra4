# Cobra4 Studio

`c4 studio` starts a local browser-based editor for Cobra4. `c4 idle`
is kept as a compatibility alias.

```bash
c4 studio
c4 studio --no-browser
c4 studio --port 8765
```

By default `c4 studio` asks the operating system for a free random local
port. Pass `--port` only when a fixed port is needed.

Studio uses the same compiler path as `c4 build` and `c4 run`. Its
Python tab shows the generated Python for the current source, and the
graph tab visualizes imports, functions, IO calls, HTTP handlers,
schedules, events, workflow tasks, resources, and deploy targets found in
the Cobra4 AST.

The editor also provides Cobra4 syntax highlighting, LSP-powered
completions, live lint diagnostics, signature help, hover metadata,
formatting, an outline view, and clickable diagnostics that jump back to
the relevant source line.

The generated Python view is connected to Cobra4 through the compiler
source map. Clicking a Cobra4 line highlights the generated Python lines
that came from it; clicking a mapped Python line jumps back to the Cobra4
source.

The command palette opens from the Commands button or `Ctrl+Shift+P`
(`Cmd+Shift+P` on macOS), and runs common actions such as Run, Check,
Format, Save, theme switching, project search, terminal focus, and
snippet creation.

The sidebar shows the project file tree rooted at the directory where
Studio was launched. Directories are collapsible, and the open/closed tree
state is preserved while the tree auto-refreshes. The tree toolbar can
create files and folders, rename, duplicate, and delete the selected
entry. The snippet library contains built-in Cobra4 building blocks plus
project-custom snippets stored in `cobra4.snippets.json`. Selecting a
snippet and pressing Insert places it at the current editor line.

The project search field scans text files under the project root, skips
heavy generated folders such as `.git`, `.venv`, `node_modules`, `build`,
and `dist`, and opens matches directly at the matching source line.

The project tree refreshes automatically while Studio is open and when
the browser window regains focus. File rows use type-specific icons for
Cobra4 files, directories, Python files, Markdown, JSON, config files,
and generic files. The top bar includes a light/dark theme toggle and a
Settings modal, stored locally in the browser. Settings cover editor font
size, tab size, autosave, tree refresh interval, and run timeout.

The sidebar and editor/result split are resizable. Studio saves those
panel sizes in the browser so the layout comes back the same way next
time.

Snippet rows include an Inspect action that opens a modal with a blurred
background, larger scrollable fields, and Insert, Save, and Delete
actions at the bottom. Saving a built-in snippet creates a custom copy.

The Terminal tab runs non-interactive shell commands in the project root,
which is useful for commands such as `git status`, `git push`, `c4 test`,
and `c4 check --strict src/main.c4`.

Files are opened and saved relative to the directory where `c4 studio` was
started, unless an absolute path is supplied.
