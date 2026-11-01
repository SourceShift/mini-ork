import * as crypto from 'crypto';
import * as vscode from 'vscode';

import { MiniOrkApi } from '../backend/api';
import { BackendStatus } from '../backend/manager';
import { error, warn } from '../log';

/** Everything the dashboard renders, assembled host-side. */
export interface DashboardSnapshot {
  backend: BackendStatus;
  board?: unknown;
  runs?: unknown;
  learning?: unknown;
  recipes?: unknown;
  fetchedAt: number;
  errors: string[];
}

export type PanelAction =
  | { action: 'launch'; recipe?: string }
  | { action: 'steer'; runId: string }
  | { action: 'pauseCost'; runId: string }
  | { action: 'resumeCost'; runId: string }
  | { action: 'openTrajectory'; runId: string }
  | { action: 'copyRunId'; runId: string };

/**
 * The dashboard webview.
 *
 * The webview never talks to the backend. Its origin is `vscode-webview://…`,
 * which the server's CORS allow-list cannot name, so every HTTP call is
 * proxied through this host-side class over `postMessage`. A side benefit:
 * the bearer token never enters the rendered page.
 */
export class MiniOrkPanel implements vscode.Disposable {
  private static current: MiniOrkPanel | undefined;
  private readonly disposables: vscode.Disposable[] = [];

  static show(
    extensionUri: vscode.Uri,
    api: MiniOrkApi,
    onAction: (action: PanelAction) => void | Promise<void>,
  ): MiniOrkPanel {
    const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
    if (MiniOrkPanel.current) {
      MiniOrkPanel.current.panel.reveal(column);
      MiniOrkPanel.current.instanceApi = api;
      void MiniOrkPanel.current.push();
      return MiniOrkPanel.current;
    }
    const panel = vscode.window.createWebviewPanel(
      'miniOrkDashboard',
      'mini-ork',
      column,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
      },
    );
    MiniOrkPanel.current = new MiniOrkPanel(panel, extensionUri, api, onAction);
    return MiniOrkPanel.current;
  }

  private instanceApi: MiniOrkApi;
  private disposed = false;

  private constructor(
    private readonly panel: vscode.WebviewPanel,
    extensionUri: vscode.Uri,
    api: MiniOrkApi,
    private readonly onAction: (action: PanelAction) => void | Promise<void>,
  ) {
    this.instanceApi = api;
    this.panel.webview.html = this.render(extensionUri);

    this.panel.webview.onDidReceiveMessage(
      (msg: { type?: string; payload?: unknown }) => {
        void this.handleMessage(msg);
      },
      undefined,
      this.disposables,
    );
    this.panel.onDidDispose(() => this.dispose(), undefined, this.disposables);
    void this.push();
  }

  private async handleMessage(msg: { type?: string; payload?: unknown }): Promise<void> {
    switch (msg.type) {
      case 'ready':
      case 'refresh':
        await this.push();
        break;
      case 'action': {
        const action = msg.payload as PanelAction;
        if (action && typeof action.action === 'string') {
          await this.onAction(action);
          await this.push();
        }
        break;
      }
      default:
        warn(`dashboard: unknown message type ${String(msg.type)}`);
    }
  }

  /** Re-read every surface and push one coherent snapshot to the page. */
  async push(): Promise<void> {
    if (this.disposed) {
      return;
    }
    const api = this.instanceApi;
    const errors: string[] = [];
    const settle = async <T>(label: string, p: Promise<T>): Promise<T | undefined> => {
      try {
        return await p;
      } catch (err) {
        errors.push(`${label}: ${err instanceof Error ? err.message : String(err)}`);
        return undefined;
      }
    };

    const [board, runs, learning, recipes] = await Promise.all([
      settle('board', api.boardShell()),
      settle('runs', api.runs()),
      settle('learning', api.learningSummary()),
      settle('recipes', api.recipes()),
    ]);

    const snapshot: DashboardSnapshot = {
      backend: {
        state: api.baseUrl ? 'ready' : 'stopped',
        baseUrl: api.baseUrl,
      },
      board,
      runs,
      learning,
      recipes,
      fetchedAt: Date.now(),
      errors,
    };
    if (errors.length) {
      error(`dashboard partial load: ${errors.join('; ')}`);
    }
    void this.panel.webview.postMessage({ type: 'snapshot', payload: snapshot });
  }

  updateBackend(status: BackendStatus): void {
    void this.panel.webview.postMessage({ type: 'backend', payload: status });
  }

  private render(extensionUri: vscode.Uri): string {
    const webview = this.panel.webview;
    const nonce = crypto.randomBytes(16).toString('base64');
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, 'media', 'main.js'),
    );
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, 'media', 'style.css'),
    );

    // Strict CSP: no remote origins, nonce-gated script, stylesheet from the
    // extension only. The page needs no network access of its own.
    const csp = [
      "default-src 'none'",
      `img-src ${webview.cspSource} data:`,
      `style-src ${webview.cspSource} 'unsafe-inline'`,
      `script-src 'nonce-${nonce}'`,
      `font-src ${webview.cspSource}`,
    ].join('; ');

    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${styleUri}">
<title>mini-ork</title>
</head>
<body>
  <header class="bar">
    <nav class="tabs" role="tablist">
      <button class="tab active" data-tab="board" role="tab">Board</button>
      <button class="tab" data-tab="runs" role="tab">Runs</button>
      <button class="tab" data-tab="learning" role="tab">Learnings</button>
      <button class="tab" data-tab="recipes" role="tab">Recipes</button>
    </nav>
    <div class="bar-right">
      <span id="backend" class="pill">connecting…</span>
      <button id="refresh" class="ghost" title="Refresh">Refresh</button>
    </div>
  </header>
  <main id="view" aria-live="polite">
    <div class="empty">Loading mini-ork…</div>
  </main>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }

  dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    MiniOrkPanel.current = undefined;
    this.panel.dispose();
    while (this.disposables.length) {
      this.disposables.pop()?.dispose();
    }
  }
}
