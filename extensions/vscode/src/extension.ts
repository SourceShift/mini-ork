import * as vscode from 'vscode';

import { MiniOrkApi } from './backend/api';
import { readFileToken } from './backend/discovery';
import { BackendManager } from './backend/manager';
import { registerCommands } from './commands';
import { readConfig, resolveHome } from './config';
import { disposeLog, info, warn } from './log';
import { StatusBar } from './statusBar';
import { LearningsTreeProvider } from './views/learningsTree';
import { RecipesTreeProvider } from './views/recipesTree';
import { RunsTreeProvider } from './views/runsTree';

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  info('activating mini-ork extension');

  const manager = new BackendManager();
  // Token resolution is lazy and fail-closed: an explicit setting wins, then
  // the first token in the project's auth-tokens.txt, and otherwise writes
  // simply fail with a clear message rather than sending an empty bearer.
  const api = new MiniOrkApi(
    () => manager.baseUrl,
    () => {
      const cfg = readConfig();
      if (cfg.authToken.trim()) {
        return cfg.authToken.trim();
      }
      const home = resolveHome();
      return home ? readFileToken(home) : undefined;
    },
  );

  const isReady = (): boolean => manager.current.state === 'ready';
  const interval = readConfig().refreshIntervalMs;

  const runsTree = new RunsTreeProvider(api, interval, isReady);
  const recipesTree = new RecipesTreeProvider(api, interval, isReady);
  const learningsTree = new LearningsTreeProvider(api, interval, isReady);

  context.subscriptions.push(
    manager,
    runsTree,
    recipesTree,
    learningsTree,
    vscode.window.createTreeView('miniOrk.runs', {
      treeDataProvider: runsTree,
      showCollapseAll: true,
    }),
    vscode.window.createTreeView('miniOrk.recipes', {
      treeDataProvider: recipesTree,
    }),
    vscode.window.createTreeView('miniOrk.learnings', {
      treeDataProvider: learningsTree,
    }),
  );

  const status = new StatusBar();
  context.subscriptions.push(status);

  const refreshAll = (): void => {
    runsTree.kick();
    recipesTree.kick();
    learningsTree.kick();
    void refreshStatusBoard();
  };

  context.subscriptions.push(
    manager.onDidChangeState((next) => {
      status.setBackend(next);
      if (next.state === 'ready') {
        refreshAll();
      } else if (next.state === 'stopped' || next.state === 'failed') {
        // A dead backend has no board to show; clear rather than freeze the
        // last numbers, which would read as "still live".
        status.setBoard(undefined);
      }
    }),
  );

  async function refreshStatusBoard(): Promise<void> {
    if (!isReady()) {
      return;
    }
    try {
      status.setBoard(await api.boardShell());
    } catch (err) {
      warn(`status board refresh failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  registerCommands(context, {
    api,
    manager,
    runsTree,
    recipesTree,
    learningsTree,
    status,
    extensionUri: context.extensionUri,
    refreshAll,
  });

  // Configuration changes can move the home directory or the binary, which
  // invalidates the running backend; restart rather than silently serving the
  // old project.
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (!event.affectsConfiguration('miniOrk')) {
        return;
      }
      const cfg = readConfig();
      if (
        event.affectsConfiguration('miniOrk.home') ||
        event.affectsConfiguration('miniOrk.binaryPath') ||
        event.affectsConfiguration('miniOrk.serverPort')
      ) {
        void manager.restart().then(() => refreshAll());
      } else {
        refreshAll();
      }
      info(`configuration changed (refresh interval ${cfg.refreshIntervalMs}ms)`);
    }),
  );

  const cfg = readConfig();
  if (cfg.autoStart) {
    void manager.start().then(() => refreshAll());
  } else {
    info('autoStart disabled — backend will not be spawned');
  }
}

export function deactivate(): void {
  // Child processes and intervals are torn down by their disposables in
  // `context.subscriptions`; this only releases the log channel.
  disposeLog();
}
