import * as vscode from 'vscode';
import WebSocket from 'ws';

import { warn } from '../log';
import { MiniOrkApi } from './api';
import { drainSseBuffer } from './sse';

export interface RunEvent {
  event: string;
  data: unknown;
}

/**
 * Server-sent events for a run's DAG.
 *
 * `mini-ork serve` exposes `/api/v1/stream?task_run=<id>&nodes=a,b` as
 * `text/event-stream`. We read it with the built-in fetch body stream rather
 * than EventSource: Node 18 has no global EventSource, and the fetch stream
 * keeps the bearer-less read path identical to the rest of the client.
 */
export class RunEventStream implements vscode.Disposable {
  private controller?: AbortController;

  constructor(
    private readonly api: MiniOrkApi,
    private readonly onEvent: (evt: RunEvent) => void,
  ) {}

  start(runId: string, nodes?: string[]): void {
    this.stop();
    const base = this.api.baseUrl;
    if (!base) {
      warn('event stream requested while backend is down');
      return;
    }
    const q = new URLSearchParams({ task_run: runId });
    if (nodes && nodes.length) {
      q.set('nodes', nodes.join(','));
    }
    const url = `${base}/api/v1/stream?${q.toString()}`;

    const controller = new AbortController();
    this.controller = controller;
    void this.pump(url, controller.signal);
  }

  private async pump(url: string, signal: AbortSignal): Promise<void> {
    try {
      const res = await fetch(url, { signal, headers: { Accept: 'text/event-stream' } });
      if (!res.ok || !res.body) {
        warn(`event stream refused: HTTP ${res.status}`);
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          return;
        }
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = drainSseBuffer(buffer);
        buffer = rest;
        for (const evt of events) {
          this.onEvent(evt);
        }
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        return;
      }
      warn(`event stream failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  stop(): void {
    this.controller?.abort();
    this.controller = undefined;
  }

  dispose(): void {
    this.stop();
  }
}

/**
 * A WebSocket session against `/api/v1/pty`.
 *
 * The server answers with a `[pty disabled]` notice and close code 4403 when
 * `MO_PTY_ENABLED=1` is unset — that is a configuration state, not an error,
 * so it is surfaced verbatim rather than swallowed.
 */
export class PtySession implements vscode.Disposable {
  private socket?: WebSocket;

  constructor(
    private readonly api: MiniOrkApi,
    private readonly handlers: {
      onData: (chunk: string) => void;
      onClose?: (code: number, reason: string) => void;
    },
  ) {}

  connect(opts: { runId?: string; cmd?: string; cols?: number; rows?: number } = {}): boolean {
    const base = this.api.wsUrl('/api/v1/pty');
    if (!base) {
      return false;
    }
    const q = new URLSearchParams({
      cols: String(opts.cols ?? 120),
      rows: String(opts.rows ?? 30),
    });
    if (opts.runId) {
      q.set('run_id', opts.runId);
    }
    if (opts.cmd) {
      q.set('cmd', opts.cmd);
    }

    const socket = new WebSocket(`${base}?${q.toString()}`);
    this.socket = socket;

    socket.on('message', (raw) => {
      this.handlers.onData(raw.toString());
    });
    socket.on('close', (code, reason) => {
      this.handlers.onClose?.(code, reason.toString());
    });
    socket.on('error', (err) => {
      warn(`pty socket error: ${err.message}`);
    });
    return true;
  }

  send(data: string): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(data);
    }
  }

  resize(cols: number, rows: number): void {
    this.send(JSON.stringify({ type: 'resize', cols, rows }));
  }

  dispose(): void {
    this.socket?.close();
    this.socket = undefined;
  }
}
