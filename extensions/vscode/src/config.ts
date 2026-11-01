import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';

export interface MiniOrkConfig {
  binaryPath: string;
  home: string;
  autoStart: boolean;
  attachToExisting: boolean;
  serverPort: number;
  serverHost: string;
  startupTimeoutMs: number;
  refreshIntervalMs: number;
  authToken: string;
  releaseChannel: string;
}

export function readConfig(): MiniOrkConfig {
  const c = vscode.workspace.getConfiguration('miniOrk');
  return {
    binaryPath: c.get<string>('binaryPath', '').trim(),
    home: c.get<string>('home', '').trim(),
    autoStart: c.get<boolean>('autoStart', true),
    attachToExisting: c.get<boolean>('attachToExisting', true),
    serverPort: c.get<number>('serverPort', 0),
    serverHost: c.get<string>('serverHost', '127.0.0.1').trim() || '127.0.0.1',
    startupTimeoutMs: c.get<number>('startupTimeoutMs', 30_000),
    refreshIntervalMs: c.get<number>('refreshIntervalMs', 5_000),
    authToken: c.get<string>('authToken', ''),
    releaseChannel: c.get<string>('releaseChannel', 'production'),
  };
}

/**
 * Resolve the `.mini-ork` home.
 *
 * Order: explicit setting, then the first workspace folder's `.mini-ork`, then
 * `MINI_ORK_HOME`. Returning `undefined` is meaningful — it means "this
 * workspace is not a mini-ork project", which the view uses to show the
 * initialize prompt rather than an empty tree.
 */
export function resolveHome(): string | undefined {
  const cfg = readConfig();
  if (cfg.home) {
    return cfg.home.replace(/^~(?=$|\/)/, os.homedir());
  }

  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    const candidate = path.join(folder.uri.fsPath, '.mini-ork');
    if (isDirectory(candidate)) {
      return candidate;
    }
  }

  const envHome = process.env.MINI_ORK_HOME;
  if (envHome && envHome.trim()) {
    return envHome.trim();
  }
  return undefined;
}

function isDirectory(p: string): boolean {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

/** The workspace root a run should be launched against. */
export function resolveWorkspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}
