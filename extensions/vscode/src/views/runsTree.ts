import * as vscode from 'vscode';

import { MiniOrkApi } from '../backend/api';
import { RunCard } from '../backend/types';
import { warn } from '../log';
import { money, Poller, relativeTime } from './common';

const GROUPS = [
  { key: 'needs_you', label: 'Needs You', icon: 'account' },
  { key: 'working', label: 'Working', icon: 'sync' },
  { key: 'failed', label: 'Failed', icon: 'error' },
  { key: 'done', label: 'Done', icon: 'pass' },
] as const;

type GroupKey = (typeof GROUPS)[number]['key'];

export interface RunNode {
  kind: 'group' | 'run';
  group?: GroupKey;
  label: string;
  run?: RunCard;
  children?: RunNode[];
}

/**
 * Runs, grouped by the same four states the board header counts.
 *
 * The grouping is the point: a flat list of a thousand runs is unreadable,
 * and "needs_you" is the only group most sessions ever need to open.
 */
export class RunsTreeProvider
  implements vscode.TreeDataProvider<RunNode>, vscode.Disposable
{
  private readonly emitter = new vscode.EventEmitter<RunNode | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;

  private roots: RunNode[] = [];
  private readonly poller: Poller;

  constructor(
    private readonly api: MiniOrkApi,
    intervalMs: number,
    isReady: () => boolean,
  ) {
    this.poller = new Poller(intervalMs, () => this.refresh(), isReady);
  }

  start(): void {
    this.poller.start();
  }

  kick(): void {
    this.poller.kick();
  }

  async refresh(): Promise<void> {
    try {
      const res = await this.api.runs();
      this.roots = groupRuns(res.runs ?? []);
      this.emitter.fire(undefined);
    } catch (err) {
      // Keep the previous render: a transient 503 during a backend restart
      // must not wipe the tree the user is reading.
      warn(`runs refresh failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  getChildren(element?: RunNode): RunNode[] {
    if (!element) {
      return this.roots;
    }
    return element.children ?? [];
  }

  getTreeItem(node: RunNode): vscode.TreeItem {
    if (node.kind === 'group') {
      const item = new vscode.TreeItem(
        node.label,
        vscode.TreeItemCollapsibleState.Expanded,
      );
      const count = node.children?.length ?? 0;
      item.description = String(count);
      item.contextValue = 'miniOrk.group';
      item.iconPath = new vscode.ThemeIcon(
        GROUPS.find((g) => g.key === node.group)?.icon ?? 'folder',
      );
      if (count === 0) {
        item.collapsibleState = vscode.TreeItemCollapsibleState.None;
      }
      return item;
    }

    const run = node.run as RunCard;
    const item = new vscode.TreeItem(run.title || run.id, vscode.TreeItemCollapsibleState.None);
    item.id = run.id;
    item.contextValue = 'miniOrk.run';
    item.description = [
      run.recipe,
      run.step,
      money(run.cost_usd),
      relativeTime(run.started_at),
    ]
      .filter(Boolean)
      .join(' · ');
    item.tooltip = buildTooltip(run);
    item.iconPath = new vscode.ThemeIcon(iconFor(run.state));
    item.command = {
      command: 'miniOrk.openTrajectory',
      title: 'Open Trajectory',
      arguments: [node],
    };
    return item;
  }

  dispose(): void {
    this.poller.dispose();
    this.emitter.dispose();
  }
}

function groupRuns(runs: RunCard[]): RunNode[] {
  const buckets = new Map<GroupKey, RunCard[]>();
  for (const g of GROUPS) {
    buckets.set(g.key, []);
  }
  for (const run of runs) {
    const key = normalizeState(run.state);
    buckets.get(key)?.push(run);
  }

  return GROUPS.map((g) => {
    const children = (buckets.get(g.key) ?? []).map<RunNode>((run) => ({
      kind: 'run',
      label: run.title || run.id,
      run,
    }));
    return {
      kind: 'group' as const,
      group: g.key,
      label: g.label,
      children,
    };
  }).filter((node) => (node.children?.length ?? 0) > 0 || node.group === 'needs_you');
}

function normalizeState(state: string): GroupKey {
  switch (state) {
    case 'needs_you':
    case 'working':
    case 'failed':
    case 'done':
      return state;
    default:
      return 'working';
  }
}

function iconFor(state: string): string {
  switch (state) {
    case 'needs_you':
      return 'account';
    case 'failed':
      return 'error';
    case 'done':
      return 'pass';
    case 'working':
      return 'sync~spin';
    default:
      return 'circle-outline';
  }
}

function buildTooltip(run: RunCard): vscode.MarkdownString {
  const md = new vscode.MarkdownString();
  md.appendMarkdown(`**${run.title || run.id}**\n\n`);
  md.appendMarkdown(`\`${run.id}\`\n\n`);
  const rows: string[] = [];
  if (run.recipe) {
    rows.push(`- recipe: \`${run.recipe}\``);
  }
  if (run.step) {
    rows.push(`- step: \`${run.step}\``);
  }
  if (run.state) {
    rows.push(`- state: \`${run.state}\``);
  }
  if (typeof run.cost_usd === 'number') {
    rows.push(`- cost: ${money(run.cost_usd)}`);
  }
  if (run.verdict) {
    rows.push(`- verdict: \`${run.verdict}\``);
  }
  if (run.error) {
    rows.push(`- error: ${run.error}`);
  }
  md.appendMarkdown(rows.join('\n'));
  return md;
}

export type { RunNode as RunsTreeNode };
