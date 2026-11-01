import * as crypto from 'crypto';
import * as vscode from 'vscode';

import { ActionDispatcher, PendingConfirm, SpecAction } from '../actions/dispatcher';
import { MiniOrkApi } from '../backend/api';
import { BackendStatus, BackendManager } from '../backend/manager';
import { CliLine } from '../cli/runner';
import { info, warn } from '../log';

/** One open page tab. A run opens one tab per run id; a page opens one per key. */
export interface PageTab {
  key: string;
  tab?: string;
  args?: Record<string, string>;
  label: string;
}

interface PageDoc {
  ok?: boolean;
  key?: string;
  title?: string;
  sub?: string;
  label?: string;
  tabs?: { key: string; label: string; n?: unknown }[];
  tab?: string;
  args?: Record<string, string>;
  chips?: unknown[];
  actions?: unknown[];
  sections?: unknown[];
  errors?: Record<string, string>;
  error?: string;
  [k: string]: unknown;
}

/**
 * The mini-ork IDE panel.
 *
 * It is a **renderer, not an app**: the backend's `/ide/page` already emits the
 * design's whole vocabulary (`mini_ork/ide_pages/spec.py`), and the panel's job
 * is to show that JSON in the design's visual language and execute the `do`
 * actions it carries. Three consequences worth keeping:
 *
 * - The webview never makes a request. Its origin (`vscode-webview://…`) is not
 *   in the server's CORS allow-list, so every read is proxied here and the
 *   bearer token stays out of the rendered page.
 * - Writes are the page spec's own argv, run through `ActionDispatcher` — the
 *   IDE gains a feature when the engine gains one, with no change here.
 * - A stale page is worse than a slow one: the shell poll keeps the header
 *   fresh every few seconds while the body only changes when a page is opened,
 *   a tab is switched, or a CLI action ran.
 */
export class MiniOrkPanel implements vscode.Disposable {
  private static current: MiniOrkPanel | undefined;
  private readonly disposables: vscode.Disposable[] = [];

  static show(
    extensionUri: vscode.Uri,
    api: MiniOrkApi,
    manager: BackendManager,
    opts: {
      binary: () => string | undefined;
      home: () => string | undefined;
      cwd: () => string;
      intervalMs: number;
    },
  ): MiniOrkPanel {
    const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
    if (MiniOrkPanel.current) {
      MiniOrkPanel.current.panel.reveal(column);
      MiniOrkPanel.current.instanceApi = api;
      void MiniOrkPanel.current.reload();
      return MiniOrkPanel.current;
    }
    const panel = vscode.window.createWebviewPanel(
      'miniOrkIde',
      'mini-ork',
      column,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
      },
    );
    MiniOrkPanel.current = new MiniOrkPanel(panel, extensionUri, api, manager, opts);
    return MiniOrkPanel.current;
  }

  private instanceApi: MiniOrkApi;
  private disposed = false;

  private tabs: PageTab[] = [];
  private active = 0;
  private page: PageDoc | null = null;
  /**
   * Bumped whenever `page` changes. The webview gets a structured clone, so it
   * cannot compare object identity — this is the stamp it compares instead, and
   * it is what keeps the shell poll from rebuilding a page that did not change.
   */
  private pageRev = 0;
  private header: unknown = null;
  private counts: Record<string, number> = {};
  private loading = false;
  private readonly dispatcher: ActionDispatcher;
  private readonly pending = new Map<string, PendingConfirm>();
  private pollTimer?: NodeJS.Timeout;
  /** Timer for a page that asked to be re-fetched (`page.refresh`, seconds). */
  private pageTimer?: NodeJS.Timeout;
  /**
   * Increments on every fetch. A response whose token is no longer current is
   * dropped, so switching pages quickly can never let the slower, older page
   * land on top of the newer one.
   */
  private fetchSeq = 0;
  private backendStatus: BackendStatus;

  private constructor(
    private readonly panel: vscode.WebviewPanel,
    extensionUri: vscode.Uri,
    api: MiniOrkApi,
    private readonly manager: BackendManager,
    opts: {
      binary: () => string | undefined;
      home: () => string | undefined;
      cwd: () => string;
      intervalMs: number;
    },
  ) {
    this.instanceApi = api;
    this.backendStatus = manager.current;
    this.dispatcher = new ActionDispatcher(
      {
        openPage: (req) => this.openPage(req),
        openRun: (runId, title, tab) => this.openRun(runId, title, tab),
        openPath: (abs, reveal) => this.openPath(abs, reveal),
        toast: (text, sub, tone) => this.toast(text, sub, tone),
        log: (entry) => this.log(entry),
        confirm: (req) => this.confirm(req),
        applySet: (args) => this.applySet(args),
        reload: () => this.reload(),
      },
      { binary: opts.binary, home: opts.home, cwd: opts.cwd },
    );

    this.panel.webview.html = this.render(extensionUri);
    this.panel.webview.onDidReceiveMessage(
      (msg: { type?: string; payload?: unknown }) => {
        void this.handleMessage(msg);
      },
      undefined,
      this.disposables,
    );
    this.panel.onDidDispose(() => this.dispose(), undefined, this.disposables);

    // The shell poll: header + counts only. The body page is fetched on demand.
    this.pollTimer = setInterval(() => void this.pollShell(), Math.max(2_000, opts.intervalMs));
    void this.openPage({ key: 'orch' });
  }

  // ── messages ─────────────────────────────────────────────────────────
  private async handleMessage(msg: { type?: string; payload?: unknown }): Promise<void> {
    const p = (msg.payload ?? {}) as Record<string, unknown>;
    switch (msg.type) {
      case 'ready':
        await this.pushState();
        await this.pollShell();
        break;
      case 'openPage':
        await this.openPage({
          key: String(p.key ?? ''),
          tab: p.tab ? String(p.tab) : undefined,
          args: (p.args as Record<string, string> | undefined) ?? undefined,
          keepTab: p.keepTab === true,
        });
        break;
      case 'closeTab':
        this.closeTab(Number(p.index ?? -1));
        break;
      case 'activateTab':
        await this.activate(Number(p.index ?? -1));
        break;
      case 'act':
        await this.dispatcher.dispatch(p.do as SpecAction);
        break;
      case 'confirmAct': {
        const id = String(p.id ?? '');
        const req = this.pending.get(id);
        this.pending.delete(id);
        if (!req) {
          break;
        }
        try {
          await req.run();
        } catch (err) {
          this.toast('Action failed', err instanceof Error ? err.message : String(err), 'err');
        }
        break;
      }
      case 'refresh':
        await this.reload();
        break;
      case 'search':
        await this.pushSearch();
        break;
      case 'backend': {
        const status = await this.manager.start();
        this.updateBackend(status);
        break;
      }
      default:
        warn(`ide: unknown message type ${String(msg.type)}`);
    }
  }

  // ── tabs + pages ─────────────────────────────────────────────────────
  private tabId(t: PageTab): string {
    const args = t.args ? JSON.stringify(sorted(t.args)) : '';
    return `${t.key}::${t.tab ?? ''}::${args}`;
  }

  async openPage(req: {
    key: string;
    tab?: string;
    args?: Record<string, string>;
    keepTab?: boolean;
  }): Promise<void> {
    if (!req.key) {
      await this.reload();
      return;
    }
    const label = labelFor(req.key, req.args);
    const wanted: PageTab = { key: req.key, tab: req.tab, args: req.args, label };

    if (req.keepTab && this.tabs[this.active]) {
      // Switching a sub-tab inside the page that is already open: replace it in
      // place so the tab strip does not grow one entry per sub-tab.
      this.tabs[this.active] = wanted;
    } else {
      const existing = this.tabs.findIndex((t) => this.tabId(t) === this.tabId(wanted));
      if (existing >= 0) {
        this.tabs[existing].label = label;
        this.active = existing;
      } else {
        this.tabs.push(wanted);
        this.active = this.tabs.length - 1;
      }
    }
    await this.fetchActive();
  }

  async openRun(runId: string, title?: string, tab?: string): Promise<void> {
    await this.openPage({
      key: 'run',
      tab: tab || 'outcome',
      args: { run: runId, ...(title ? { title } : {}) },
    });
  }

  private async activate(index: number): Promise<void> {
    if (index < 0 || index >= this.tabs.length) {
      return;
    }
    this.active = index;
    await this.fetchActive();
  }

  private closeTab(index: number): void {
    if (index < 0 || index >= this.tabs.length) {
      return;
    }
    this.tabs.splice(index, 1);
    if (!this.tabs.length) {
      this.active = 0;
      this.page = null;
      this.pageRev += 1;
      void this.openPage({ key: 'orch' });
      return;
    }
    this.active = Math.min(this.active, this.tabs.length - 1);
    void this.fetchActive();
  }

  private async fetchActive(): Promise<void> {
    const tab = this.tabs[this.active];
    if (!tab) {
      this.page = null;
      this.pageRev += 1;
      this.clearPageTimer();
      await this.pushState();
      return;
    }
    const seq = ++this.fetchSeq;
    this.loading = true;
    await this.pushState();
    const args = tab.args
      ? Object.entries(tab.args).map(([k, v]) => `${k}=${v}`)
      : [];
    let doc: PageDoc;
    try {
      doc = (await this.instanceApi.page(tab.key, {
        tab: tab.tab,
        args,
        spec: '2',
      })) as PageDoc;
    } catch (err) {
      doc = {
        ok: false,
        key: tab.key,
        error: err instanceof Error ? err.message : String(err),
      };
    }
    if (seq !== this.fetchSeq) {
      // A newer fetch started while this one was in flight — its answer wins.
      return;
    }
    this.page = doc;
    this.pageRev += 1;
    if (doc && typeof doc.label === 'string' && doc.label) {
      // The engine knows the page's own name better than our table does.
      const t = this.tabs[this.active];
      if (t) {
        t.label = labelFor(tab.key, tab.args, doc.title ?? doc.label);
      }
    }
    this.armPageTimer(doc);
    this.loading = false;
    await this.pushState();
  }

  /**
   * A page may ask to be re-fetched (contract v2.1 `refresh`, in seconds) —
   * a run in flight, a lane burning budget. The timer belongs to the page, so
   * switching tabs cancels it rather than leaving a stale poll running.
   */
  private armPageTimer(doc: PageDoc | null): void {
    this.clearPageTimer();
    const seconds = Number(doc?.refresh ?? 0);
    if (!Number.isFinite(seconds) || seconds < 2) {
      return;
    }
    this.pageTimer = setTimeout(() => {
      this.pageTimer = undefined;
      void this.refreshInPlace();
    }, seconds * 1_000);
  }

  /** Re-fetch the open page without the loading flicker the first open shows. */
  private async refreshInPlace(): Promise<void> {
    const seq = this.fetchSeq;
    const tab = this.tabs[this.active];
    if (!tab) {
      return;
    }
    const args = tab.args
      ? Object.entries(tab.args).map(([k, v]) => `${k}=${v}`)
      : [];
    try {
      const doc = (await this.instanceApi.page(tab.key, {
        tab: tab.tab,
        args,
        spec: '2',
      })) as PageDoc;
      if (seq !== this.fetchSeq) {
        return;
      }
      this.page = doc;
    this.pageRev += 1;
      this.armPageTimer(doc);
      await this.pushState();
    } catch {
      // A refresh that fails leaves the last good page on screen and tries
      // again on the same cadence — that is what the badge is for.
      this.armPageTimer(this.page);
    }
  }

  private clearPageTimer(): void {
    if (this.pageTimer) {
      clearTimeout(this.pageTimer);
      this.pageTimer = undefined;
    }
  }

  async reload(): Promise<void> {
    await this.fetchActive();
    await this.pollShell();
  }

  async applySet(args: Record<string, string>): Promise<void> {
    const tab = this.tabs[this.active];
    if (!tab) {
      return;
    }
    tab.args = { ...(tab.args ?? {}), ...args };
    await this.fetchActive();
  }

  // ── host polling ─────────────────────────────────────────────────────
  private async pollShell(): Promise<void> {
    if (this.disposed) {
      return;
    }
    try {
      const shell = (await this.instanceApi.boardShell()) as {
        header?: unknown;
        counts?: Record<string, number>;
      };
      this.header = shell?.header ?? this.header;
      this.counts = shell?.counts ?? this.counts;
    } catch (err) {
      // A backend that is down is a state, not an error to shout about: the
      // header keeps its last good values and the pill says "offline".
      this.counts = {};
      if (this.backendStatus.state === 'ready') {
        warn(`ide shell poll: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
    this.backendStatus = this.manager.current;
    await this.pushState();
  }

  private async pushSearch(): Promise<void> {
    try {
      const res = (await this.instanceApi.runs()) as { runs?: unknown[] };
      this.post('runs', { runs: res?.runs ?? [] });
    } catch {
      this.post('runs', { runs: [] });
    }
  }

  private async pushState(): Promise<void> {
    if (this.disposed) {
      return;
    }
    this.post('state', {
      header: this.header,
      counts: this.counts,
      backend: this.backendStatus,
      tabs: this.tabs,
      active: this.active,
      page: this.page,
      pageRev: this.pageRev,
      loading: this.loading,
    });
  }

  private post(type: string, payload: unknown): void {
    void this.panel.webview.postMessage({ type, payload });
  }

  // ── host services used by the dispatcher ─────────────────────────────
  private async openPath(abs: string, reveal: boolean): Promise<void> {
    try {
      const uri = vscode.Uri.file(abs);
      if (reveal) {
        await vscode.commands.executeCommand('revealFileInOS', uri);
      } else {
        const doc = await vscode.workspace.openTextDocument(uri);
        await vscode.window.showTextDocument(doc, { preview: true });
      }
    } catch (err) {
      this.toast('Could not open', `${abs} — ${err instanceof Error ? err.message : String(err)}`, 'err');
    }
  }

  private toast(text: string, sub?: string, tone?: 'ok' | 'err'): void {
    this.post('toast', { text, sub, tone });
  }

  private log(entry: CliLine): void {
    this.post('log', { entries: [entry], open: entry.kind === 'cmd' });
  }

  private confirm(req: Omit<PendingConfirm, 'id'>): string {
    const id = `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    this.pending.set(id, { ...req, id });
    // Only the newest request is on screen; an abandoned one is dropped so a
    // stale approval can never run an action the user has stopped looking at.
    for (const key of [...this.pending.keys()]) {
      if (key !== id) {
        this.pending.delete(key);
      }
    }
    this.post('confirm', { ...req, id, run: undefined });
    return id;
  }

  /** Forward a backend state change to the open panel, if there is one. */
  static notifyBackend(status: BackendStatus): void {
    MiniOrkPanel.current?.updateBackend(status);
  }

  updateBackend(status: BackendStatus): void {
    this.backendStatus = status;
    void this.pushState();
    if (status.state === 'ready') {
      void this.reload();
    }
  }

  /** Run a spec action from outside the webview (a keybinding or a command). */
  async runAction(spec: SpecAction): Promise<void> {
    await this.dispatcher.dispatch(spec);
  }

  // ── markup ───────────────────────────────────────────────────────────
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
    // extension only. The renderer builds every node with createElement and
    // textContent, so no backend string is ever parsed as HTML.
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
<div id="root">
  <div class="topbar">
    <div class="brand">
      <span class="glyph">✦</span>
      <b id="project">mini-ork</b>
      <span class="git" id="branch"></span>
      <span class="git" id="worktrees"></span>
    </div>
    <div class="searchbox" id="search" title="Search runs and pages">⌕ Search…
      <kbd>⌘K</kbd>
    </div>
    <div class="spacer"></div>
    <div class="topright">
      <span class="chip yellow" id="needs" style="display:none"></span>
      <span class="mono" id="cost"></span>
      <span id="sched"></span>
      <span class="flag" id="cn"></span>
      <span id="nodecount"></span>
      <span class="flag" id="backend">connecting…</span>
      <span class="flag" id="logbtn" title="Command log">≡ log</span>
      <span class="flag" id="refresh" title="Refresh (⌘R)">⟳</span>
    </div>
  </div>
  <div class="body">
    <div class="rail" id="rail"></div>
    <div class="main">
      <div class="tabstrip" id="tabstrip"></div>
      <div class="viewport" id="viewport" aria-live="polite">
        <div class="loading">Loading mini-ork…</div>
      </div>
      <div class="drawer" id="drawer">
        <div class="dh">
          <span>mini-ork output</span>
          <span class="flag" id="drawerClear" style="cursor:pointer">clear</span>
          <span class="x" id="drawerClose">×</span>
        </div>
        <div class="dl" id="logbody"></div>
      </div>
    </div>
  </div>
  <button class="btn ghost" id="toggleRail" title="Toggle the page rail"
    style="position:fixed;left:4px;bottom:4px;opacity:.35;z-index:5">◧</button>
</div>
<div class="toast" id="toast"></div>
<div id="modals"></div>
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
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
    }
    this.clearPageTimer();
    this.panel.dispose();
    while (this.disposables.length) {
      this.disposables.pop()?.dispose();
    }
    info('ide panel disposed');
  }
}

function sorted(o: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const k of Object.keys(o).sort()) {
    out[k] = o[k];
  }
  return out;
}

const LABELS: Record<string, string> = {
  orch: 'Dispatch',
  inbox: 'Inbox',
  kanban: 'Board',
  runs: 'Runs',
  changes: 'Changes',
  verify: 'Verify & safety',
  recipes: 'Recipes & epics',
  autos: 'Automations',
  lanes: 'Lanes & cost',
  learn: 'Learning & memory',
  context: 'Context',
  nodes: 'Nodes',
  dags: 'DAGs',
  setup: 'Setup & health',
  features: 'Features',
  run: 'Run',
  task: 'Task',
  thread: 'Threads',
};

/** The tab strip label: a run shows its title, a page its name. */
function labelFor(key: string, args?: Record<string, string>, title?: string): string {
  if (key === 'run') {
    const t = args?.title || title;
    return t ? truncate(t, 32) : `Run ${args?.run ? args.run.slice(-8) : ''}`.trim();
  }
  if (key === 'task') {
    return `Task ${args?.uid ?? ''}`.trim();
  }
  return LABELS[key] ?? key;
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
