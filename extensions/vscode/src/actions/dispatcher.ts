import * as path from 'path';
import * as vscode from 'vscode';

import { CliLine, runCli } from '../cli/runner';
import { warn } from '../log';

import { COSTLY_VERBS, DESTRUCTIVE_VERBS, verbOf } from './verbs';

/**
 * One action from the page spec. The shapes are documented in
 * `mini_ork/ide_pages/spec.py`: every button, row, chip and node carries one.
 */
export interface SpecAction {
  cli?: string[];
  page?: string;
  tab?: string;
  args?: Record<string, string>;
  set?: Record<string, string>;
  run?: string;
  title?: string;
  path?: string;
  reveal?: string;
  url?: string;
  terminal?: { cli?: string[]; cwd?: string; home?: boolean };
  thread?: string;
  project?: string;
  project_pick?: boolean;
  pick_files?: { cli?: string[]; prompt?: string; home?: boolean };
  copy?: string;
  done?: string;
  confirm?: string;
  body?: string;
  /** Reason the action exists — the design surfaces it in the confirm sheet. */
  note?: string;
  [key: string]: unknown;
}

/** A pending confirm: the action waits here until the user decides. */
export interface PendingConfirm {
  id: string;
  title: string;
  body?: string;
  cmd?: string;
  danger?: boolean;
  confirmLabel?: string;
  run: () => Promise<void>;
}

/** What the dispatcher needs from the panel; keeps it free of webview details. */
export interface ActionHost {
  openPage(req: { key: string; tab?: string; args?: Record<string, string>; keepTab?: boolean }): Promise<void>;
  openRun(runId: string, title?: string, tab?: string): Promise<void>;
  openPath(abs: string, reveal: boolean): Promise<void>;
  toast(text: string, sub?: string, tone?: 'ok' | 'err'): void;
  log(entry: CliLine): void;
  confirm(req: Omit<PendingConfirm, 'id'>): string;
  /** Merge args into the page that is currently open and re-fetch it. */
  applySet(args: Record<string, string>): Promise<void>;
  /** Re-fetch the page that is currently open (after a CLI touched its state). */
  reload(): Promise<void>;
}

export class ActionDispatcher {
  constructor(
    private readonly host: ActionHost,
    private readonly deps: {
      binary: () => string | undefined;
      home: () => string | undefined;
      cwd: () => string;
    },
  ) {}

  /**
   * Execute one spec action.
   *
   * Returns `'confirm'` when the action was parked behind a confirm sheet —
   * the panel shows it and calls `run(id)` when the user approves.
   */
  async dispatch(spec: SpecAction | null | undefined): Promise<'done' | 'confirm' | 'noop'> {
    if (!spec || typeof spec !== 'object') {
      return 'noop';
    }

    if (spec.copy !== undefined) {
      await vscode.env.clipboard.writeText(spec.copy);
      this.host.toast(spec.done || 'Copied', spec.copy.slice(0, 120), 'ok');
      return 'done';
    }

    if (spec.page !== undefined) {
      await this.host.openPage({ key: spec.page, tab: spec.tab, args: spec.args });
      return 'done';
    }

    if (spec.set !== undefined) {
      await this.host.applySet(spec.set);
      return 'done';
    }

    if (spec.run !== undefined) {
      await this.host.openRun(spec.run, spec.title, spec.tab);
      return 'done';
    }

    if (spec.reveal !== undefined) {
      await this.host.openPath(spec.reveal, true);
      return 'done';
    }

    if (spec.path !== undefined) {
      await this.host.openPath(spec.path, false);
      return 'done';
    }

    if (spec.url !== undefined) {
      await vscode.env.openExternal(vscode.Uri.parse(spec.url));
      return 'done';
    }

    if (spec.thread !== undefined) {
      // A thread is a durable unit of work; the page owns creating it. Keep the
      // draft in the clipboard so nothing the user typed is lost.
      await vscode.env.clipboard.writeText(spec.thread);
      await this.host.openPage({ key: 'thread' });
      this.host.toast('Draft copied', 'Create a thread from the Threads page, then paste it.', 'ok');
      return 'done';
    }

    if (spec.project_pick) {
      const picked = await vscode.window.showOpenDialog({
        canSelectFolders: true,
        canSelectFiles: false,
        canSelectMany: false,
        openLabel: 'Open project',
      });
      if (!picked?.length) {
        return 'noop';
      }
      return this.openFolder(picked[0].fsPath);
    }

    if (spec.project !== undefined) {
      return this.openFolder(spec.project);
    }

    if (spec.terminal !== undefined) {
      this.openTerminal(spec.terminal);
      return 'done';
    }

    if (spec.pick_files !== undefined) {
      return this.pickFiles(spec);
    }

    if (spec.cli !== undefined) {
      return this.runCliAction(spec);
    }

    warn(`unknown action: ${JSON.stringify(spec).slice(0, 200)}`);
    this.host.toast('This action is not supported by the VS Code surface', JSON.stringify(spec).slice(0, 200), 'err');
    return 'noop';
  }

  // ── cli ──────────────────────────────────────────────────────────────
  private async runCliAction(spec: SpecAction): Promise<'done' | 'confirm'> {
    const args = spec.cli ?? [];
    const binary = this.deps.binary();
    if (!binary) {
      this.host.toast('mini-ork binary not found', 'Set miniOrk.binaryPath or install mini-ork.', 'err');
      return 'done';
    }
    const verb = verbOf(args);
    const costly = verb !== undefined && COSTLY_VERBS.has(verb);
    const destructive = verb !== undefined && DESTRUCTIVE_VERBS.has(verb);

    // The spec's own `confirm` is the authority; these two guards only fill a
    // gap when a page forgot one.
    if (!spec.confirm && (costly || destructive)) {
      spec = {
        ...spec,
        confirm: destructive
          ? `Run \`mini-ork ${args.join(' ')}\`?`
          : `Start \`mini-ork ${args.join(' ')}\`?`,
        body: destructive
          ? 'This cannot be undone from here.'
          : 'This starts work that spends money against your daily cap.',
      };
    }

    const execute = async (): Promise<void> => {
      this.host.toast(`Running: mini-ork ${args.slice(0, 3).join(' ')}…`, args.join(' '));
      const result = await runCli({
        binary,
        args,
        home: spec.home === false ? undefined : this.deps.home(),
        cwd: this.deps.cwd(),
        onLine: (line) => this.host.log(line),
      });
      if (result.timedOut) {
        this.host.toast('Command timed out', args.join(' '), 'err');
      } else if (result.code !== 0) {
        this.host.toast(`Command failed (exit ${result.code ?? '?'})`, result.tail || args.join(' '), 'err');
      } else {
        this.host.toast('Done', args.join(' '), 'ok');
      }
    };

    if (spec.confirm) {
      this.host.confirm({
        title: spec.confirm,
        body: spec.body,
        cmd: `mini-ork ${args.join(' ')}`,
        danger: destructive,
        confirmLabel: destructive ? 'Stop it' : 'Run',
        run: async () => {
          await execute();
          await this.host.reload();
        },
      });
      return 'confirm';
    }

    await execute();
    // Re-read the page the user is on: the CLI just changed state it renders.
    await this.host.reload();
    return 'done';
  }

  // ── file picking ─────────────────────────────────────────────────────
  private async pickFiles(spec: SpecAction): Promise<'done' | 'noop'> {
    const binary = this.deps.binary();
    if (!binary) {
      this.host.toast('mini-ork binary not found', undefined, 'err');
      return 'noop';
    }
    const picked = await vscode.window.showOpenDialog({
      canSelectMany: true,
      canSelectFiles: true,
      canSelectFolders: false,
      openLabel: spec.pick_files?.prompt || 'Add files',
    });
    if (!picked?.length) {
      return 'noop';
    }
    const args = [...(spec.pick_files?.cli ?? []), ...picked.map((u) => u.fsPath)];
    await runCli({
      binary,
      args,
      home: spec.pick_files?.home === false ? undefined : this.deps.home(),
      cwd: this.deps.cwd(),
      onLine: (line) => this.host.log(line),
    });
    await this.host.reload();
    return 'done';
  }

  // ── terminal ─────────────────────────────────────────────────────────
  private openTerminal(term: { cli?: string[]; cwd?: string; home?: boolean }): void {
    const binary = this.deps.binary();
    const args = [...(term.cli ?? [])];
    const home = term.home === false ? undefined : this.deps.home();
    if (home) {
      args.push('--home', home);
    }
    const t = vscode.window.createTerminal({
      name: args.length ? `mini-ork ${args[0]}` : 'mini-ork',
      cwd: term.cwd || this.deps.cwd(),
      env: home ? { MINI_ORK_HOME: home } : undefined,
    });
    t.show();
    if (binary && args.length) {
      t.sendText([quote(binary), ...args.map(quote)].join(' '));
    }
  }

  // ── project ──────────────────────────────────────────────────────────
  private async openFolder(abs: string): Promise<'done' | 'noop'> {
    const uri = vscode.Uri.file(path.resolve(abs));
    const pick = await vscode.window.showInformationMessage(
      `Switch this window to ${path.basename(abs)}?`,
      { modal: true },
      'Switch',
    );
    if (pick !== 'Switch') {
      return 'noop';
    }
    await vscode.commands.executeCommand('vscode.openFolder', uri, { forceNewWindow: false });
    return 'done';
  }
}

/** Quote one argv token for `terminal.sendText` (a POSIX shell runs it). */
function quote(token: string): string {
  if (/^[\w@%+=:,./-]+$/.test(token)) {
    return token;
  }
  return `'${token.replace(/'/g, `'\\''`)}'`;
}
