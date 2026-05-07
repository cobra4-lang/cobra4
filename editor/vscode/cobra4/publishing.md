# How to build, test, and publish the cobra4 VS Code extension

## 0. Prerequisites

```bash
# Node 18+ is required by vscode-languageclient.
node --version

# Install vsce (Visual Studio Code Extension manager).
npm install -g @vscode/vsce

# Inside this directory:
cd editor/vscode/cobra4
npm install
```

## 1. Try it locally without packaging

The fastest dev loop:

```bash
# 1. Open this folder in VS Code:
code editor/vscode/cobra4

# 2. Press F5 (or Run → Start Debugging).
#    A second VS Code window opens with the extension activated.

# 3. In that window, open any .c4 file from the project to see
#    highlighting, diagnostics, hover, etc.
```

Alternative — symlink it as an installed extension:

```bash
# macOS / Linux:
ln -s "$PWD/editor/vscode/cobra4" ~/.vscode/extensions/cobra4-lang.cobra4-0.1.0

# Windows (PowerShell as admin):
New-Item -ItemType SymbolicLink \
  -Path "$env:USERPROFILE\.vscode\extensions\cobra4-lang.cobra4-0.1.0" \
  -Target "$PWD\editor\vscode\cobra4"
```

Reload VS Code (`Ctrl/Cmd+Shift+P → Developer: Reload Window`) and the
extension is live.

## 2. Build a `.vsix` package

```bash
cd editor/vscode/cobra4
vsce package
```

You get `cobra4-0.1.0.vsix`. To install it manually:

```bash
code --install-extension cobra4-0.1.0.vsix
```

To uninstall the manual install:

```bash
code --uninstall-extension cobra4-lang.cobra4
```

## 3. Publish to the Marketplace

You need a publisher account on the Visual Studio Marketplace.

### One-time setup

1. Create a publisher account at
   <https://marketplace.visualstudio.com/manage> (sign in with an MSA
   tied to an Azure DevOps org).
2. Create a Personal Access Token (PAT) at
   <https://dev.azure.com/{your-org}/_usersSettings/tokens>:
   - Organization: **All accessible organizations**
   - Scopes: **Marketplace → Manage**
3. Update the `"publisher"` field in `package.json` to your publisher
   id (currently `cobra4-lang`; replace with what you registered).

### Login + publish

```bash
vsce login your-publisher-id
# Paste the PAT when prompted.

vsce publish              # patches the version automatically? No: use:
vsce publish minor        # bumps minor (e.g. 0.1.0 → 0.2.0)
# or specify exactly:
vsce publish 0.2.0
```

The Marketplace processes the upload in a couple of minutes. Verify at
<https://marketplace.visualstudio.com/items?itemName=your-publisher-id.cobra4>.

### Subsequent updates

```bash
# bump CHANGELOG.md
# bump version in package.json
vsce publish patch        # x.y.z → x.y.(z+1)
```

## 4. Open VSX (cursor / VSCodium)

If you want users on forks (Cursor, VSCodium, code-server) to install
without GitHub Workspaces, also publish to <https://open-vsx.org>:

```bash
npm install -g ovsx
ovsx publish cobra4-0.1.0.vsix --pat YOUR_OPEN_VSX_TOKEN
```

You need an Open VSX account (Eclipse Foundation) to mint the token.

## 5. Verifying the LSP wiring

Once installed:

1. Open a `.c4` file with a deliberate typo (`fn foo(`) — diagnostics
   should appear within a second.
2. Hover a defined function — signature + return type appears.
3. F12 (Go to Definition) on a function name jumps to its `fn` line.
4. `Ctrl/Cmd+Shift+O` shows the document outline.
5. Right-click → cobra4: Run File runs the active file in the
   integrated terminal.

If diagnostics never show, check the **cobra4** output channel
(`View → Output → cobra4`) for the LSP startup message.

## 6. Repository hygiene before publishing

- Add a 128×128 `icon.png` next to `package.json`. The Marketplace
  shows it in search results. (Currently the manifest references
  `icon.png` but expects you to drop one in.)
- Confirm the `repository.url` in `package.json` matches the public
  repo where users find issues.
- Run `vsce ls` to inspect what gets bundled — anything excluded by
  `.vscodeignore` won't ship.

## 7. CI publishing (optional)

A minimal GitHub Action that publishes on tag:

```yaml
name: Publish VS Code extension
on:
  push:
    tags: ['vscode-v*']
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 20 }
      - run: npm install -g @vscode/vsce
      - run: npm install
        working-directory: editor/vscode/cobra4
      - run: vsce publish --pat ${{ secrets.VSCE_PAT }}
        working-directory: editor/vscode/cobra4
```

Trigger with `git tag vscode-v0.2.0 && git push --tags`.
