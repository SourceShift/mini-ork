import * as vscode from 'vscode';

import { MiniOrkApi } from '../backend/api';
import { RecipeOption } from '../backend/types';
import { warn } from '../log';
import { Poller } from './common';

export interface RecipeNode {
  kind: 'recipe' | 'empty';
  recipe?: RecipeOption;
}

/** The dispatch catalogue: every recipe the backend will accept a run for. */
export class RecipesTreeProvider
  implements vscode.TreeDataProvider<RecipeNode>, vscode.Disposable
{
  private readonly emitter = new vscode.EventEmitter<RecipeNode | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;

  private items: RecipeNode[] = [];
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
      const opts = await this.api.recipes();
      const recipes = (opts.recipes ?? [])
        .slice()
        .sort((a, b) => a.recipe.localeCompare(b.recipe));
      this.items = recipes.length
        ? recipes.map((recipe) => ({ kind: 'recipe' as const, recipe }))
        : [{ kind: 'empty' as const }];
      this.emitter.fire(undefined);
    } catch (err) {
      warn(`recipes refresh failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  getChildren(element?: RecipeNode): RecipeNode[] {
    return element ? [] : this.items;
  }

  getTreeItem(node: RecipeNode): vscode.TreeItem {
    if (node.kind === 'empty') {
      const item = new vscode.TreeItem('No recipes found', vscode.TreeItemCollapsibleState.None);
      item.iconPath = new vscode.ThemeIcon('warning');
      return item;
    }
    const recipe = node.recipe as RecipeOption;
    const item = new vscode.TreeItem(recipe.recipe, vscode.TreeItemCollapsibleState.None);
    item.id = recipe.recipe;
    item.contextValue = 'miniOrk.recipe';
    item.description = recipe.task_class;
    item.iconPath = new vscode.ThemeIcon('play-circle');
    item.tooltip = new vscode.MarkdownString(
      `**${recipe.recipe}**\n\n\`task_class: ${recipe.task_class}\`\n\n${firstLine(recipe.description)}`,
    );
    item.command = {
      command: 'miniOrk.launchRun',
      title: 'Launch Run',
      arguments: [{ recipe: recipe.recipe }],
    };
    return item;
  }

  dispose(): void {
    this.poller.dispose();
    this.emitter.dispose();
  }
}

function firstLine(text?: string): string {
  if (!text) {
    return '';
  }
  const line = text.split('\n').find((l) => l.trim());
  return line ? line.trim().slice(0, 400) : '';
}
