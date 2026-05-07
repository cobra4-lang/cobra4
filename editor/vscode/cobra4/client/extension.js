// VS Code extension entry point.
// Spawns `c4 lsp` over stdio and registers run/build/fmt/check/test commands.

const path = require('path');
const cp = require('child_process');
const vscode = require('vscode');
const { LanguageClient, TransportKind } = require('vscode-languageclient/node');

let client = null;
let outputChannel = null;

function getCli() {
  return vscode.workspace.getConfiguration('cobra4').get('executable') || 'c4';
}

function getCwd() {
  const folders = vscode.workspace.workspaceFolders;
  if (folders && folders.length > 0) return folders[0].uri.fsPath;
  const editor = vscode.window.activeTextEditor;
  if (editor) return path.dirname(editor.document.uri.fsPath);
  return process.cwd();
}

function startLanguageServer(context) {
  const cli = getCli();
  const cfg = vscode.workspace.getConfiguration('cobra4');
  if (!cfg.get('lsp.enabled')) return;

  const serverOptions = {
    command: cli,
    args: ['lsp'],
    transport: TransportKind.stdio,
    options: { cwd: getCwd() },
  };

  const clientOptions = {
    documentSelector: [{ scheme: 'file', language: 'cobra4' }],
    outputChannel: outputChannel,
    synchronize: {
      configurationSection: 'cobra4',
      fileEvents: vscode.workspace.createFileSystemWatcher('**/*.c4'),
    },
  };

  client = new LanguageClient(
    'cobra4',
    'cobra4 Language Server',
    serverOptions,
    clientOptions,
  );

  client.start().catch((err) => {
    outputChannel.appendLine(`Failed to start cobra4 LSP: ${err.message}`);
    vscode.window
      .showErrorMessage(
        `Could not start cobra4 LSP (${cli} lsp). Set "cobra4.executable" to a valid path or install cobra4.`,
        'Open Settings',
      )
      .then((choice) => {
        if (choice === 'Open Settings') {
          vscode.commands.executeCommand('workbench.action.openSettings', 'cobra4.executable');
        }
      });
  });
}

function stopLanguageServer() {
  if (!client) return Promise.resolve();
  const c = client;
  client = null;
  return c.stop();
}

// ---------- Command helpers ----------

let runTerminal = null;

function runInTerminal(cmd, name = 'cobra4') {
  if (!runTerminal || runTerminal.exitStatus !== undefined) {
    runTerminal = vscode.window.createTerminal({ name, cwd: getCwd() });
  }
  runTerminal.show(true);
  runTerminal.sendText(cmd);
}

function runCli(args, options = {}) {
  return new Promise((resolve, reject) => {
    const cli = getCli();
    const child = cp.spawn(cli, args, { cwd: getCwd(), shell: false });
    let stdout = '', stderr = '';
    child.stdout.on('data', (d) => { stdout += d.toString(); });
    child.stderr.on('data', (d) => { stderr += d.toString(); });
    child.on('error', reject);
    child.on('close', (code) => {
      if (options.verbose) outputChannel.appendLine(stdout + stderr);
      resolve({ code, stdout, stderr });
    });
  });
}

function activeFilePath() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage('No active cobra4 file.');
    return null;
  }
  if (editor.document.languageId !== 'cobra4') {
    vscode.window.showWarningMessage('Current file is not a cobra4 file.');
    return null;
  }
  return editor.document.uri.fsPath;
}

// ---------- Activation ----------

function activate(context) {
  outputChannel = vscode.window.createOutputChannel('cobra4');
  context.subscriptions.push(outputChannel);

  startLanguageServer(context);

  const disposables = [
    vscode.commands.registerCommand('cobra4.run', () => {
      const file = activeFilePath();
      if (file) runInTerminal(`${getCli()} run "${file}"`);
    }),
    vscode.commands.registerCommand('cobra4.build', async () => {
      const file = activeFilePath();
      if (!file) return;
      const out = file.replace(/\.c4$/, '.py');
      runInTerminal(`${getCli()} build "${file}" -o "${out}"`);
    }),
    vscode.commands.registerCommand('cobra4.fmt', async () => {
      const file = activeFilePath();
      if (!file) return;
      const editor = vscode.window.activeTextEditor;
      if (editor.document.isDirty) await editor.document.save();
      const { code, stderr } = await runCli(['fmt', file, '-w'], { verbose: true });
      if (code !== 0) {
        vscode.window.showErrorMessage(`c4 fmt failed: ${stderr.split('\n')[0]}`);
        return;
      }
      // Reload from disk so the editor reflects the formatted contents.
      await vscode.commands.executeCommand('workbench.action.files.revert');
    }),
    vscode.commands.registerCommand('cobra4.check', () => {
      const file = activeFilePath();
      if (!file) return;
      const cfg = vscode.workspace.getConfiguration('cobra4');
      const args = cfg.get('check.strict') ? 'check --strict' : 'check';
      runInTerminal(`${getCli()} ${args} "${file}"`);
    }),
    vscode.commands.registerCommand('cobra4.test', () => {
      runInTerminal(`${getCli()} test`);
    }),
    vscode.commands.registerCommand('cobra4.serve', () => {
      const file = activeFilePath();
      if (file) runInTerminal(`${getCli()} serve "${file}"`);
    }),
    vscode.commands.registerCommand('cobra4.restartLSP', async () => {
      await stopLanguageServer();
      startLanguageServer(context);
      vscode.window.setStatusBarMessage('cobra4 LSP restarted', 2000);
    }),
  ];
  context.subscriptions.push(...disposables);

  // Format-on-save
  context.subscriptions.push(
    vscode.workspace.onWillSaveTextDocument((e) => {
      if (e.document.languageId !== 'cobra4') return;
      const cfg = vscode.workspace.getConfiguration('cobra4');
      if (!cfg.get('format.onSave')) return;
      e.waitUntil(
        runCli(['fmt', e.document.uri.fsPath]).then(({ stdout, code }) => {
          if (code !== 0) return [];
          const fullRange = new vscode.Range(
            e.document.lineAt(0).range.start,
            e.document.lineAt(e.document.lineCount - 1).range.end,
          );
          return [vscode.TextEdit.replace(fullRange, stdout)];
        }),
      );
    }),
  );
}

function deactivate() {
  return stopLanguageServer();
}

module.exports = { activate, deactivate };
