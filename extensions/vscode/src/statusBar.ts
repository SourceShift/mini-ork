import * as vscode from 'vscode';

import { BackendStatus } from './backend/manager';
import { BoardShell } from './backend/types';
import { readConfig } from './config';

/**
 * One status item that answers the two questions a user actually has:
 * "is the backend alive" and "does anything need me".
 *
 * It is the cheapest form of the board — the same shell payload the Zed
 * surface polls, reduced to a count and a cost.
 */
export class StatusBar implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;
  private backend: BackendStatus = { state: 'stopped' };
  private board?: BoardShell;

  constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    this.item.command = 'miniOrk.openDashboard';
    this.item.name = 'mini-ork';
    this.render();
    this.item.show();
  }

  setBackend(status: BackendStatus): void {
    this.backend = status;
    this.render();
  }

  setBoard(board: BoardShell | undefined): void {
    this.board = board;
    this.render();
  }

  private render(): void {
    const channel = readConfig().releaseChannel;
    const suffix = channel && channel !== 'production' ? ` (${channel})` : '';

    switch (this.backend.state) {
      case 'ready': {
        const header = this.board?.header;
        const needs = header?.needs_you ?? 0;
        const usd = header?.today_usd;
        const cost = typeof usd === 'number' ? ` · $${usd.toFixed(2)}` : '';
        const attention = needs > 0 ? ` · ${needs} need you` : '';
        this.item.text = `$(pulse) mini-ork${attention}${cost}`;
        this.item.tooltip = this.tooltip(`${this.board?.project ?? 'project'}${suffix}`);
        this.item.backgroundColor =
          needs > 0
            ? new vscode.ThemeColor('statusBarItem.warningBackground')
            : undefined;
        break;
      }
      case 'starting':
        this.item.text = '$(sync~spin) mini-ork starting';
        this.item.tooltip = this.backend.detail ?? 'starting backend';
        this.item.backgroundColor = undefined;
        break;
      case 'failed':
        this.item.text = '$(error) mini-ork backend down';
        this.item.tooltip = this.backend.detail ?? 'backend failed';
        this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
        break;
      default:
        this.item.text = '$(circle-slash) mini-ork stopped';
        this.item.tooltip = 'backend not running — click to start';
        this.item.backgroundColor = undefined;
    }
  }

  private tooltip(project: string): vscode.MarkdownString {
    const md = new vscode.MarkdownString();
    md.appendMarkdown(`**mini-ork** — ${project}\n\n`);
    md.appendMarkdown(`Backend: \`${this.backend.baseUrl ?? 'n/a'}\` (${this.backend.mode ?? 'n/a'})\n\n`);
    const h = this.board?.header;
    if (h) {
      md.appendMarkdown(
        [
          `| needs you | working | failed | done |`,
          `|---|---|---|---|`,
          `| ${h.needs_you ?? 0} | ${h.working ?? 0} | ${h.failed ?? 0} | ${h.done ?? 0} |`,
        ].join('\n'),
      );
      if (typeof h.today_usd === 'number') {
        md.appendMarkdown(
          `\n\nToday: **$${h.today_usd.toFixed(2)}**${h.cap_usd ? ` / $${h.cap_usd.toFixed(0)}` : ''}`,
        );
      }
    }
    md.appendMarkdown('\n\n_Click for the dashboard._');
    return md;
  }

  dispose(): void {
    this.item.dispose();
  }
}
