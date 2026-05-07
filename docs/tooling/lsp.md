# LSP / IDE setup

`c4 lsp` is a JSON-RPC over stdio language server. Capabilities:

| Feature                         | Notes                                                  |
|---------------------------------|--------------------------------------------------------|
| Diagnostics                     | parser + resolver + type checker, on save & change     |
| Hover                           | inferred type / function signature                     |
| Go to definition (F12)          | function / class / top-level variable                  |
| Find references (Shift+F12)     | all matching `Name` occurrences                        |
| Document symbols (Ctrl+Shift+O) | top-level fns + class methods                          |
| Completion (Ctrl+Space, `.`)    | scope-aware: params, locals, loop vars, catch bindings; member completion for known shapes (`req.`, `result.`, `log.`, …) |
| Signature help (after `(`)      | resolves user-defined fns and built-ins                |
| Format (Shift+Alt+F)            | canonical re-format from AST                           |

## VS Code

The repository ships an extension at
[`editor/vscode/cobra4`](https://github.com/cobra4-lang/cobra4/tree/main/editor/vscode/cobra4).

```bash
cd editor/vscode/cobra4
npm install
npx vsce package
code --install-extension cobra4-0.1.0.vsix
```

Settings:

| Setting                  | Default | Effect                                         |
|--------------------------|---------|------------------------------------------------|
| `cobra4.executable`      | `c4`    | Path to the `c4` CLI (use absolute if not on PATH). |
| `cobra4.lsp.enabled`     | `true`  | Run the language server.                       |
| `cobra4.format.onSave`   | `false` | Run `c4 fmt` on save.                          |
| `cobra4.check.strict`    | `false` | Pass `--strict` to `c4 check`.                 |

## Neovim / Helix

Any LSP-aware editor: point it at the command `c4 lsp` for files with
extension `.c4`. Communication is plain LSP over stdio.
