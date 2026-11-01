import * as vscode from 'vscode';

import { MiniOrkApi } from '../backend/api';
import { BackendManager } from '../backend/manager';
import { findBinary } from '../backend/discovery';
import { resolveHome, resolveWorkspaceRoot, readConfig } from '../config';
import { info, report, warn } from '../log';
import { MiniOrkPanel } from '../panel/panel';
import { StatusBar } from '../statusBar';
import { RemotePty } from '../terminal/ptyTerminal';
import { LearningsTreeProvider } from '../views/learningsTree';
import { RecipesTreeProvider } from '../views/recipesTree';
import { RunsTreeProvider } from '../views/runsTree';

export interface CommandDeps {
  api: MiniOrkApi;
  manager: BackendManager;
  runsTree: RunsTreeProvider;
  recipesTree: RecipesTreeProvider;
  learningsTree: LearningsTreeProvider;
  status: StatusBar;
  extensionUri: vscode.Uri;
  refreshAll: () => void;
}

export function registerCommands(
  context: vscode.ExtensionContext,
  deps: CommandDeps,
): void {
  const { api, manager } = deps;
  const reg = (id: string, fn: (...args: never[]) => unknown): void => {
    context.subscriptions.push(
      vscode.commands.registerCommand(id, fn as (...args: unknown[]) => unknown),
    );
  };

  const requireReady = async (): Promise<boolean> => {
    if (manager.current.state === 'ready') {
      return true;
    }
    const status = await manager.start();
    if (status.state !== 'ready') {
      void vscode.window.showErrorMessage(
        `mini-ork backend unavailable: ${status.detail ?? 'unknown error'}`,
      );
      return false;
    }
    return true;
  };

  reg('miniOrk.init', async () => {
    const home = resolveHome();
    const root = resolveWorkspaceRoot();
    if (!root) {
      void vscode.window.showErrorMessage('open a folder before initializing mini-ork');
      return;
    }
    const cfg = readConfig();
    const binary = findBinary(cfg.binaryPath, root);
    if (!binary) {
      void vscode.window.showErrorMessage(
        'mini-ork executable not found — set miniOrk.binaryPath',
      );
      return;
    }
    const target = home ?? `${root}/.mini-ork`;
    const terminal = vscode.window.createTerminal({
      name: 'mini-ork init',
      cwd: root,
    });
    terminal.show();
    terminal.sendText(`${binary} init`, true);
    info(`initializing mini-ork at ${target}`);
  });

  reg('miniOrk.pickHome', async () => {
    const picked = await vscode.window.showOpenDialog({
      canSelectFiles: false,
      canSelectFolders: true,
      canSelectMany: false,
      openLabel: 'Use as mini-ork home',
      title: 'Select a .mini-ork home directory',
    });
    const dir = picked?.[0];
    if (!dir) {
      return;
    }
    await vscode.workspace
      .getConfiguration('miniOrk')
      .update('home', dir.fsPath, vscode.ConfigurationTarget.Workspace);
    deps.refreshAll();
  });

  reg('miniOrk.startServer', async () => {
    await manager.start();
    deps.refreshAll();
  });

  reg('miniOrk.stopServer', async () => {
    await manager.stop();
  });

  reg('miniOrk.restartServer', async () => {
    await manager.restart();
    deps.refreshAll();
  });

  reg('miniOrk.showOutput', () => {
    void vscode.commands.executeCommand('workbench.action.output.toggleOutput');
  });

  reg('miniOrk.openSettings', () => {
    void vscode.commands.executeCommand('workbench.action.openSettings', 'miniOrk');
  });

  reg('miniOrk.refreshAll', () => deps.refreshAll());

  // The IDE is a renderer over the backend's page spec: opening it needs the
  // binary + home so its buttons can run the argv the pages carry.
  const showIde = (): MiniOrkPanel =>
    MiniOrkPanel.show(deps.extensionUri, api, manager, {
      binary: () => findBinary(readConfig().binaryPath, resolveWorkspaceRoot()),
      home: () => resolveHome(),
      cwd: () => resolveWorkspaceRoot() ?? process.cwd(),
      intervalMs: readConfig().refreshIntervalMs,
    });

  const openDashboard = (): void => {
    showIde();
  };
  reg('miniOrk.openDashboard', openDashboard);

  reg('miniOrk.openOrchestrator', async () => {
    await showIde().openPage({ key: 'orch', tab: 'dispatch' });
  });

  /** `miniOrk.openPage { key, tab?, args?, run? }` — the IDE's one entry point. */
  reg('miniOrk.openPage', async (arg?: unknown) => {
    const panel = showIde();
    const req = (arg ?? {}) as {
      key?: string;
      tab?: string;
      args?: Record<string, string>;
      run?: string;
      title?: string;
    };
    if (req.run) {
      await panel.openRun(req.run, req.title, req.tab);
      return;
    }
    await panel.openPage({
      key: req.key ?? 'orch',
      tab: req.tab,
      args: req.args,
    });
  });

  reg('miniOrk.launchRun', async (arg?: unknown) => {
    if (!(await requireReady())) {
      return;
    }
    const presetRecipe = extractRecipe(arg);
    const recipe = presetRecipe ?? (await pickRecipe(api));
    if (!recipe) {
      return;
    }
    const kickoff = extractKickoffOverride(arg) ?? (await pickKickoff());
    if (kickoff === undefined) {
      return;
    }
    try {
      const result = await api.launch({ recipe, kickoffMarkdown: kickoff });
      if (!result.ok) {
        report(result.error ?? 'launch failed');
        return;
      }
      info(`launched ${result.run_id ?? '(unknown id)'} with recipe ${recipe}`);
      void vscode.window.showInformationMessage(
        `mini-ork run started: ${result.run_id ?? '(see server log)'}`,
      );
      deps.refreshAll();
    } catch (err) {
      report(`launch failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  });

  reg('miniOrk.launchRunSelection', async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.selection.isEmpty) {
      void vscode.window.showInformationMessage('select some text first');
      return;
    }
    const selection = editor.document.getText(editor.selection);
    await vscode.commands.executeCommand('miniOrk.launchRun', {
      kickoffOverride: [
        `# Kickoff from selection`,
        '',
        `Source: \`${vscode.workspace.asRelativePath(editor.document.uri)}\``,
        '',
        selection,
      ].join('\n'),
    });
  });

  reg('miniOrk.steerRun', async (node?: unknown) => {
    if (!(await requireReady())) {
      return;
    }
    const runId = await resolveRunId(api, node);
    if (!runId) {
      return;
    }
    try {
      await api.steer(runId);
      void vscode.window.showInformationMessage(`steered ${runId}`);
      deps.refreshAll();
    } catch (err) {
      report(`steer failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  });

  reg('miniOrk.pauseCost', async (node?: unknown) => {
    if (!(await requireReady())) {
      return;
    }
    const runId = await resolveRunId(api, node);
    if (!runId) {
      return;
    }
    try {
      await api.pauseCost(runId);
      void vscode.window.showInformationMessage(`paused ${runId}`);
      deps.refreshAll();
    } catch (err) {
      report(`pause failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  });

  reg('miniOrk.resumeCost', async (node?: unknown) => {
    if (!(await requireReady())) {
      return;
    }
    const runId = await resolveRunId(api, node);
    if (!runId) {
      return;
    }
    try {
      await api.resumeCost(runId);
      void vscode.window.showInformationMessage(`resumed ${runId}`);
      deps.refreshAll();
    } catch (err) {
      report(`resume failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  });

  reg('miniOrk.recoverRun', async (node?: unknown) => {
    if (!(await requireReady())) {
      return;
    }
    const runId = await resolveRunId(api, node);
    if (!runId) {
      return;
    }
    try {
      const view = await api.recovery(runId);
      await showJson(`recovery-${runId}.json`, view);
    } catch (err) {
      report(`recovery failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  });

  reg('miniOrk.openTrajectory', async (node?: unknown) => {
    if (!(await requireReady())) {
      return;
    }
    const runId = extractRunId(node) ?? (await resolveRunId(api, node));
    if (!runId) {
      return;
    }
    try {
      await showIde().openRun(runId, undefined, 'dag');
    } catch (err) {
      report(`trajectory failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  });

  reg('miniOrk.openArtifacts', async (node?: unknown) => {
    if (!(await requireReady())) {
      return;
    }
    const runId = extractRunId(node) ?? (await resolveRunId(api, node));
    if (!runId) {
      return;
    }
    try {
      const artifacts = await api.artifacts(runId);
      await showJson(`artifacts-${runId}.json`, artifacts);
    } catch (err) {
      report(`artifacts failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  });

  reg('miniOrk.attachTerminal', async (node?: unknown) => {
    const runId = extractRunId(node) ?? (await resolveRunId(api, node));
    if (!runId || !(await requireReady())) {
      return;
    }
    const terminal = vscode.window.createTerminal({
      name: `mini-ork ${runId}`,
      pty: new RemotePty(api, runId),
    });
    terminal.show();
  });

  reg('miniOrk.copyRunId', async (node?: unknown) => {
    const runId = extractRunId(node) ?? (await resolveRunId(api, node));
    if (!runId) {
      return;
    }
    await vscode.env.clipboard.writeText(runId);
    void vscode.window.setStatusBarMessage(`copied ${runId}`, 2_000);
  });

}

// --- helpers ---------------------------------------------------------------

function extractRecipe(arg: unknown): string | undefined {
  if (arg && typeof arg === 'object' && 'recipe' in arg) {
    const recipe = (arg as { recipe?: unknown }).recipe;
    if (typeof recipe === 'string' && recipe) {
      return recipe;
    }
  }
  return undefined;
}

function extractKickoffOverride(arg: unknown): string | undefined {
  if (arg && typeof arg === 'object' && 'kickoffOverride' in arg) {
    const k = (arg as { kickoffOverride?: unknown }).kickoffOverride;
    if (typeof k === 'string' && k.trim()) {
      return k;
    }
  }
  return undefined;
}

function extractRunId(node: unknown): string | undefined {
  if (!node || typeof node !== 'object') {
    return undefined;
  }
  const maybeRun = node as { runId?: unknown; run?: { id?: unknown } };
  if (typeof maybeRun.runId === 'string') {
    return maybeRun.runId;
  }
  if (maybeRun.run && typeof maybeRun.run.id === 'string') {
    return maybeRun.run.id;
  }
  return undefined;
}

async function pickRecipe(api: MiniOrkApi): Promise<string | undefined> {
  try {
    const options = await api.recipes();
    const items = (options.recipes ?? []).map((r) => ({
      label: r.recipe,
      description: r.task_class,
      detail: r.description?.split('\n')[0],
    }));
    if (!items.length) {
      warn('no recipes available from the backend');
      return undefined;
    }
    const picked = await vscode.window.showQuickPick(items, {
      title: 'Recipe',
      placeHolder: 'Pick the recipe to run',
      matchOnDescription: true,
      matchOnDetail: true,
    });
    return picked?.label;
  } catch (err) {
    report(`could not list recipes: ${err instanceof Error ? err.message : String(err)}`);
    return undefined;
  }
}

/**
 * Resolve the kickoff body. Returns `undefined` when the user cancels, which
 * the caller must distinguish from an empty string — an empty kickoff is a
 * valid (if unusual) request.
 */
async function pickKickoff(): Promise<string | undefined> {
  const editor = vscode.window.activeTextEditor;
  const options: Array<vscode.QuickPickItem & { source: string }> = [];

  if (editor && !editor.selection.isEmpty) {
    options.push({
      label: '$(selection) Current selection',
      description: `${editor.selection.end.line - editor.selection.start.line + 1} lines`,
      source: 'selection',
    });
  }
  if (editor) {
    options.push({
      label: '$(file) Active file',
      description: vscode.workspace.asRelativePath(editor.document.uri),
      source: 'file',
    });
  }
  options.push({ label: '$(edit) Type a kickoff…', source: 'inline' });
  options.push({ label: '$(folder-opened) Pick a kickoff file…', source: 'path' });

  const picked = await vscode.window.showQuickPick(options, {
    title: 'Kickoff',
    placeHolder: 'What should the run start from?',
  });
  if (!picked) {
    return undefined;
  }

  switch (picked.source) {
    case 'selection':
      return editor?.document.getText(editor.selection);
    case 'file':
      return editor ? `# From ${vscode.workspace.asRelativePath(editor.document.uri)}\n\n${editor.document.getText()}` : undefined;
    case 'inline': {
      const text = await vscode.window.showInputBox({
        title: 'Kickoff markdown',
        placeHolder: 'Describe the task for the agent',
        prompt: 'Shift+Enter is unavailable here — one line, or use a file instead.',
      });
      return text ? text : undefined;
    }
    case 'path': {
      const files = await vscode.window.showOpenDialog({
        canSelectMany: false,
        openLabel: 'Use as kickoff',
        filters: { Markdown: ['md'], Text: ['txt'] },
      });
      const file = files?.[0];
      if (!file) {
        return undefined;
      }
      const bytes = await vscode.workspace.fs.readFile(file);
      return Buffer.from(bytes).toString('utf8');
    }
    default:
      return undefined;
  }
}

async function resolveRunId(api: MiniOrkApi, node: unknown): Promise<string | undefined> {
  const direct = extractRunId(node);
  if (direct) {
    return direct;
  }
  try {
    const res = await api.runs();
    const items = (res.runs ?? []).map((run) => ({
      label: run.title || run.id,
      description: `${run.state}${run.step ? ` · ${run.step}` : ''}`,
      detail: run.id,
      id: run.id,
    }));
    if (!items.length) {
      void vscode.window.showInformationMessage('no runs to choose from');
      return undefined;
    }
    const picked = await vscode.window.showQuickPick(items, {
      title: 'Pick a run',
      matchOnDescription: true,
      matchOnDetail: true,
    });
    return picked?.id;
  } catch (err) {
    report(`could not list runs: ${err instanceof Error ? err.message : String(err)}`);
    return undefined;
  }
}

/** Show a payload as a read-only untitled document rather than a modal. */
async function showJson(name: string, payload: unknown): Promise<void> {
  const doc = await vscode.workspace.openTextDocument({
    language: 'json',
    content: JSON.stringify(payload, null, 2),
  });
  await vscode.window.showTextDocument(doc, { preview: true });
  info(`opened ${name}`);
}

