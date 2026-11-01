import * as vscode from 'vscode';

import { MiniOrkApi } from '../backend/api';
import { LearningPattern, LearningSummary } from '../backend/types';
import { warn } from '../log';
import { Poller, relativeTime } from './common';

export interface LearningNode {
  kind: 'stat' | 'pattern' | 'empty';
  label: string;
  detail?: string;
  pattern?: LearningPattern;
}

const STAT_ROWS: Array<{ key: keyof LearningSummary; label: string; icon: string }> = [
  { key: 'traces', label: 'Traces', icon: 'graph' },
  { key: 'gradients', label: 'Gradients', icon: 'arrow-up' },
  { key: 'patterns', label: 'Patterns', icon: 'symbol-structure' },
  { key: 'bandit_arms', label: 'Bandit arms', icon: 'target' },
  { key: 'semantic_memories', label: 'Semantic memories', icon: 'database' },
  { key: 'open_circuit_breakers', label: 'Open breakers', icon: 'circle-slash' },
];

/**
 * The learning-loop readout: aggregate counters plus the promoted patterns.
 *
 * This is the "is it actually learning" view — a pattern with a strength score
 * and a resolved date is evidence the loop closed, and its absence is equally
 * informative.
 */
export class LearningsTreeProvider
  implements vscode.TreeDataProvider<LearningNode>, vscode.Disposable
{
  private readonly emitter = new vscode.EventEmitter<LearningNode | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;

  private items: LearningNode[] = [];
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
      const [summary, patterns] = await Promise.all([
        this.api.learningSummary(),
        this.api.patterns().catch(() => [] as LearningPattern[]),
      ]);

      const items: LearningNode[] = STAT_ROWS.filter(
        (row) => typeof summary[row.key] === 'number',
      ).map((row) => ({
        kind: 'stat' as const,
        label: row.label,
        detail: String(summary[row.key]),
        // Reuse the icon slot by encoding it in the label detail path.
      }));

      const top = patterns
        .slice()
        .sort((a, b) => (b.strength_score ?? 0) - (a.strength_score ?? 0))
        .slice(0, 25)
        .map<LearningNode>((pattern) => ({
          kind: 'pattern',
          label: pattern.cluster_label || pattern.pattern_id,
          detail: [
            pattern.status,
            `strength ${Math.round(pattern.strength_score ?? 0)}`,
            relativeTime(pattern.detected_at),
          ]
            .filter(Boolean)
            .join(' · '),
          pattern,
        }));

      this.items = items.length || top.length ? [...items, ...top] : [{ kind: 'empty', label: 'No learning data yet' }];
      this.emitter.fire(undefined);
    } catch (err) {
      warn(`learnings refresh failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  getChildren(element?: LearningNode): LearningNode[] {
    return element ? [] : this.items;
  }

  getTreeItem(node: LearningNode): vscode.TreeItem {
    if (node.kind === 'empty') {
      const item = new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.None);
      item.iconPath = new vscode.ThemeIcon('info');
      return item;
    }
    if (node.kind === 'stat') {
      const item = new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.None);
      item.description = node.detail;
      item.iconPath = new vscode.ThemeIcon('symbol-number');
      return item;
    }
    const pattern = node.pattern as LearningPattern;
    const item = new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.None);
    item.id = pattern.pattern_id;
    item.description = node.detail;
    item.iconPath = new vscode.ThemeIcon(
      pattern.status === 'approved' ? 'verified' : 'circle-outline',
    );
    item.tooltip = new vscode.MarkdownString(
      `**${pattern.pattern_id}**\n\n${pattern.cluster_label}\n\n${
        pattern.suggested_meta_adr ? `_${pattern.suggested_meta_adr}_` : ''
      }`,
    );
    return item;
  }

  dispose(): void {
    this.poller.dispose();
    this.emitter.dispose();
  }
}
