import { ChildProcess, spawn } from 'child_process';
import * as vscode from 'vscode';

import { readConfig, resolveHome, resolveWorkspaceRoot } from '../config';
import { error, info, warn } from '../log';
import {
  findBinary,
  findFreePort,
  isInitializedHome,
  isPortOpen,
} from './discovery';
import { Health } from './types';

export type BackendState = 'stopped' | 'starting' | 'ready' | 'failed';

export interface BackendStatus {
  state: BackendState;
  baseUrl?: string;
  port?: number;
  /** `attached` means we did not start the process and must not kill it. */
  mode?: 'attached' | 'spawned';
  detail?: string;
}

const MAX_AUTO_RESTARTS = 3;
const RESTART_BACKOFF_MS = [1_000, 4_000, 10_000];

/**
 * Owns the lifetime of the `mini-ork serve` process.
 *
 * This is the same shape the Antigravity extension uses for `agy --hub`: the
 * editor finds a backend, and if there isn't one it starts and supervises it,
 * so the UI never has to explain "the server isn't running" as a dead end.
 *
 * Two rules keep this honest:
 *  - never kill a process we did not spawn (attach mode records `mode`), and
 *  - a health probe, not a successful `spawn()`, is what makes the state
 *    `ready` — a binary that starts and immediately exits still "spawned".
 */
export class BackendManager implements vscode.Disposable {
  private readonly emitter = new vscode.EventEmitter<BackendStatus>();
  readonly onDidChangeState = this.emitter.event;

  private status: BackendStatus = { state: 'stopped' };
  private child?: ChildProcess;
  private healthTimer?: NodeJS.Timeout;
  private restartCount = 0;
  private intentionalStop = false;
  private starting?: Promise<BackendStatus>;

  get current(): BackendStatus {
    return this.status;
  }

  get baseUrl(): string | undefined {
    return this.status.state === 'ready' ? this.status.baseUrl : undefined;
  }

  private setStatus(next: BackendStatus): void {
    this.status = next;
    this.emitter.fire(next);
    if (next.state === 'ready') {
      this.startHealthWatch();
    } else {
      this.stopHealthWatch();
    }
  }

  async start(): Promise<BackendStatus> {
    if (this.status.state === 'ready') {
      return this.status;
    }
    // Collapse concurrent starts: two tree providers activating at once must
    // not race into spawning two servers.
    if (this.starting) {
      return this.starting;
    }
    this.starting = this.doStart().finally(() => {
      this.starting = undefined;
    });
    return this.starting;
  }

  private async doStart(): Promise<BackendStatus> {
    const cfg = readConfig();
    const home = resolveHome();
    this.intentionalStop = false;

    if (!home) {
      return this.fail('no mini-ork project in this workspace — run "mini-ork: Initialize Project"');
    }
    if (!isInitializedHome(home)) {
      return this.fail(`no state.db in ${home} — run "mini-ork: Initialize Project"`);
    }

    const binary = findBinary(cfg.binaryPath, resolveWorkspaceRoot());
    const host = cfg.serverHost;
    const preferred = cfg.serverPort > 0 ? cfg.serverPort : 7090;

    // Attach to a server someone else already started.
    if (cfg.attachToExisting && (await isPortOpen(host, preferred))) {
      const baseUrl = `http://${host}:${preferred}`;
      const health = await this.probeHealth(baseUrl);
      if (health?.ok) {
        info(`attached to existing mini-ork at ${baseUrl} (home=${home})`);
        this.setStatus({ state: 'ready', baseUrl, port: preferred, mode: 'attached' });
        return this.status;
      }
    }

    if (!binary) {
      return this.fail(
        'mini-ork executable not found — set miniOrk.binaryPath or put `mini-ork` on PATH',
      );
    }

    const port = cfg.serverPort > 0 ? cfg.serverPort : await findFreePort(host);
    const baseUrl = `http://${host}:${port}`;
    this.setStatus({ state: 'starting', baseUrl, port, mode: 'spawned', detail: binary });

    const args = ['serve', '--port', String(port), '--host', host, '--home', home];
    info(`spawning: ${binary} ${args.join(' ')}`);

    let child: ChildProcess;
    try {
      child = spawn(binary, args, {
        cwd: resolveWorkspaceRoot() ?? home,
        env: { ...process.env, MINI_ORK_HOME: home },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      return this.fail(`spawn failed: ${err instanceof Error ? err.message : String(err)}`);
    }
    this.child = child;

    const echo = (tag: string) => (buf: Buffer): void => {
      const text = buf.toString('utf8').trimEnd();
      if (text) {
        info(`[${tag}] ${text}`);
      }
    };
    child.stdout?.on('data', echo('serve'));
    child.stderr?.on('data', echo('serve'));

    child.once('error', (err) => {
      this.fail(`backend error: ${err.message}`);
    });
    child.once('exit', (code, signal) => {
      this.child = undefined;
      if (this.intentionalStop) {
        this.setStatus({ state: 'stopped' });
        return;
      }
      warn(`backend exited (code=${code ?? 'null'} signal=${signal ?? 'null'})`);
      this.scheduleRestart();
    });

    const deadline = Date.now() + cfg.startupTimeoutMs;
    while (Date.now() < deadline) {
      if (!this.child) {
        // The exit handler already moved us to failed/retrying.
        return this.status;
      }
      const health = await this.probeHealth(baseUrl);
      if (health?.ok) {
        this.restartCount = 0;
        info(`backend ready at ${baseUrl}`);
        this.setStatus({ state: 'ready', baseUrl, port, mode: 'spawned' });
        return this.status;
      }
      await delay(300);
    }

    return this.fail(
      `backend did not answer /api/v1/health within ${cfg.startupTimeoutMs}ms`,
    );
  }

  private fail(detail: string): BackendStatus {
    error(detail);
    this.setStatus({ state: 'failed', detail, mode: this.status.mode });
    return this.status;
  }

  private scheduleRestart(): void {
    if (this.restartCount >= MAX_AUTO_RESTARTS) {
      this.setStatus({
        state: 'failed',
        detail: `backend exited ${this.restartCount + 1} times — restart it manually`,
      });
      return;
    }
    const backoff = RESTART_BACKOFF_MS[this.restartCount] ?? 10_000;
    this.restartCount += 1;
    this.setStatus({
      state: 'starting',
      detail: `restarting in ${Math.round(backoff / 1000)}s (attempt ${this.restartCount})`,
    });
    setTimeout(() => {
      if (!this.intentionalStop) {
        void this.start();
      }
    }, backoff);
  }

  private async probeHealth(baseUrl: string): Promise<Health | undefined> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2_000);
    try {
      const res = await fetch(`${baseUrl}/api/v1/health`, { signal: controller.signal });
      if (!res.ok) {
        return undefined;
      }
      return (await res.json()) as Health;
    } catch {
      return undefined;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Re-probe an attached server; a gone backend must not look ready forever. */
  private startHealthWatch(): void {
    if (this.healthTimer) {
      return;
    }
    const interval = Math.max(readConfig().refreshIntervalMs, 2_000);
    this.healthTimer = setInterval(async () => {
      const { baseUrl } = this.status;
      if (!baseUrl) {
        return;
      }
      const health = await this.probeHealth(baseUrl);
      if (!health?.ok && this.status.state === 'ready') {
        if (this.status.mode === 'attached') {
          this.fail('attached backend stopped responding');
        }
        // Spawned backends are watched by the child's exit handler instead;
        // a slow health probe must not declare a live process dead.
      }
    }, interval);
  }

  private stopHealthWatch(): void {
    if (this.healthTimer) {
      clearInterval(this.healthTimer);
      this.healthTimer = undefined;
    }
  }

  async stop(): Promise<void> {
    this.intentionalStop = true;
    this.stopHealthWatch();
    const child = this.child;
    this.child = undefined;
    if (child && !child.killed) {
      child.kill('SIGTERM');
      await delay(500);
      if (!child.killed) {
        child.kill('SIGKILL');
      }
    }
    this.setStatus({ state: 'stopped' });
  }

  async restart(): Promise<BackendStatus> {
    this.restartCount = 0;
    await this.stop();
    return this.start();
  }

  dispose(): void {
    this.stopHealthWatch();
    const child = this.child;
    this.child = undefined;
    this.intentionalStop = true;
    if (child && !child.killed) {
      child.kill('SIGTERM');
    }
    this.emitter.dispose();
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
