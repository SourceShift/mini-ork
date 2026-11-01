import * as vscode from 'vscode';

import { MiniOrkApi } from '../backend/api';
import { PtySession } from '../backend/stream';

/**
 * Bridges the backend's `/api/v1/pty` WebSocket into a real editor terminal.
 *
 * VS Code's terminal UI is driven by a `Pseudoterminal`: we push bytes with
 * `onDidWrite` and receive keystrokes in `handleInput`. That is exactly the
 * shape of the socket, so the run's shell appears as an ordinary terminal tab
 * with no attach binary and no ssh hop.
 */
export class RemotePty implements vscode.Pseudoterminal {
  private readonly writeEmitter = new vscode.EventEmitter<string>();
  readonly onDidWrite = this.writeEmitter.event;
  private readonly closeEmitter = new vscode.EventEmitter<number>();
  readonly onDidClose = this.closeEmitter.event;

  private session?: PtySession;
  private size = { cols: 120, rows: 30 };
  private opened = false;

  constructor(
    private readonly api: MiniOrkApi,
    private readonly runId: string,
  ) {}

  open(dimensions: vscode.TerminalDimensions): void {
    this.opened = true;
    this.size = { cols: dimensions.columns, rows: dimensions.rows };
    this.session = new PtySession(this.api, {
      onData: (chunk) => this.writeEmitter.fire(normalizeNewlines(chunk)),
      onClose: (code, reason) => {
        const note = reason ? ` (${reason})` : '';
        this.writeEmitter.fire(`\r\n\x1b[2m[pty closed: ${code}${note}]\x1b[0m\r\n`);
        this.closeEmitter.fire(code === 1000 || code === 4403 ? 0 : 1);
      },
    });

    const connected = this.session.connect({
      runId: this.runId,
      cols: this.size.cols,
      rows: this.size.rows,
    });
    if (!connected) {
      this.writeEmitter.fire('\r\n\x1b[31mmini-ork backend is not running\x1b[0m\r\n');
      this.closeEmitter.fire(1);
    }
  }

  handleInput(data: string): void {
    this.session?.send(data);
  }

  setDimensions(dimensions: vscode.TerminalDimensions): void {
    this.size = { cols: dimensions.columns, rows: dimensions.rows };
    this.session?.resize(this.size.cols, this.size.rows);
  }

  close(): void {
    this.session?.dispose();
    this.session = undefined;
    if (!this.opened) {
      return;
    }
  }
}

/**
 * A PTY emits `\n` where the terminal expects `\r\n`. Normalising here (rather
 * than trusting the producer) keeps output from stair-stepping down the pane.
 * Already-correct `\r\n` sequences are left untouched.
 */
function normalizeNewlines(chunk: string): string {
  return chunk.replace(/(?<!\r)\n/g, '\r\n');
}
