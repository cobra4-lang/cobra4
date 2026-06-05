# Installation

cobra4 needs Python ≥ 3.11.

## With pip

```bash
pip install cobra4
```

This installs two console scripts on your `PATH`: `c4` (short) and
`cobra4` (alias).

```bash
c4 --version
# cobra4 0.6.1
```

## With pipx (isolated, recommended)

```bash
pipx install cobra4
```

`pipx` keeps `c4` separate from your system Python and lets you upgrade
or remove it cleanly.

## Optional extras

cobra4 has optional features behind `pip` extras:

```bash
pip install "cobra4[aws]"     # boto3 → S3 reads, SQSQueue, AWS Lambda deploy
pip install "cobra4[data]"    # pandas + pyarrow → parquet & DataFrame
pip install "cobra4[ssh]"     # paramiko → fleet.run over SSH
pip install "cobra4[yaml]"    # pyyaml → lang use yaml + read .yml
pip install "cobra4[sql]"     # SQLAlchemy → lang use sql execution
pip install "cobra4[llm]"     # anthropic → AnthropicProvider
pip install "cobra4[graphql]" # graphql-core → GraphQL syntax validation
pip install "cobra4[prom]"    # prometheus-client → process metrics backend
pip install "cobra4[redis]"   # redis → RedisQueue
pip install "cobra4[vault]"   # hvac → Vault secrets backend
pip install "cobra4[gcp]"     # google-cloud-secret-manager → GCP secrets backend
pip install "cobra4[otel]"    # OpenTelemetry → log export
pip install "cobra4[dev]"     # pytest + black for development
```

You can combine them: `pip install "cobra4[aws,data,ssh,sql]"`.

## Open Cobra4 Studio

Start Studio from the root of the project you want to work on:

```bash
c4 studio
```

Studio asks the operating system for a free local port by default and
opens a browser IDE with a project tree, editor, generated Python view,
graph view, snippets, terminal, completion, and lint diagnostics. `c4 idle`
is kept as a compatibility alias.

## VS Code extension

The VS Code extension is in the repository at
[`editor/vscode/cobra4`](https://github.com/cobra4-lang/cobra4/tree/main/editor/vscode/cobra4).
A Marketplace release is planned. For now, install the local `.vsix`:

```bash
git clone https://github.com/cobra4-lang/cobra4.git
cd cobra4/editor/vscode/cobra4
npm install
npx vsce package
code --install-extension cobra4-0.1.0.vsix
```

The extension activates on `.c4` files, attaches to `c4 lsp` for
diagnostics / hover / completion / signature help, and registers
*Run* / *Build* / *Format* / *Check* / *Test* commands.

## From source

```bash
git clone https://github.com/cobra4-lang/cobra4.git
cd cobra4
pip install -e ".[dev]"
pytest -q          # run the test suite
```
