import * as vscode from 'vscode';

let channel: vscode.OutputChannel | undefined;

export function logChannel(): vscode.OutputChannel {
  if (!channel) {
    channel = vscode.window.createOutputChannel('mini-ork');
  }
  return channel;
}

function stamp(): string {
  return new Date().toISOString().slice(11, 23);
}

export function info(msg: string): void {
  logChannel().appendLine(`[${stamp()}] ${msg}`);
}

export function warn(msg: string): void {
  logChannel().appendLine(`[${stamp()}] WARN ${msg}`);
}

export function error(msg: string): void {
  logChannel().appendLine(`[${stamp()}] ERROR ${msg}`);
  logChannel().show(true);
}

/** Log and surface a one-line failure without stealing focus for trivia. */
export function report(msg: string): void {
  error(msg);
  void vscode.window.showErrorMessage(`mini-ork: ${msg}`);
}

export function disposeLog(): void {
  channel?.dispose();
  channel = undefined;
}
