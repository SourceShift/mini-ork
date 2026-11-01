import * as vscode from 'vscode';

/**
 * Interval driver shared by the tree views.
 *
 * Polling rather than pushing is deliberate: every view here reads a cheap
 * aggregated endpoint, and a poll that fails (backend restarting) must leave
 * the last good render on screen instead of blanking the tree. The poller
 * stops itself when the caller reports the backend is not ready, so a dead
 * server costs nothing.
 */
export class Poller implements vscode.Disposable {
  private timer?: NodeJS.Timeout;

  constructor(
    private readonly intervalMs: number,
    private readonly tick: () => void | Promise<void>,
    private readonly isReady: () => boolean = () => true,
  ) {}

  start(immediate = true): void {
    this.stop();
    if (this.intervalMs > 0) {
      this.timer = setInterval(() => {
        if (this.isReady()) {
          void this.tick();
        }
      }, this.intervalMs);
    }
    if (immediate && this.isReady()) {
      void this.tick();
    }
  }

  kick(): void {
    if (this.isReady()) {
      void this.tick();
    }
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  dispose(): void {
    this.stop();
  }
}

/** Relative time, coarse on purpose — "3m" beats a timestamp in a tree row. */
export function relativeTime(epochSeconds?: number | null): string {
  if (!epochSeconds) {
    return '';
  }
  const deltaSec = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
  if (deltaSec < 60) {
    return `${deltaSec}s`;
  }
  if (deltaSec < 3600) {
    return `${Math.floor(deltaSec / 60)}m`;
  }
  if (deltaSec < 86_400) {
    return `${Math.floor(deltaSec / 3600)}h`;
  }
  return `${Math.floor(deltaSec / 86_400)}d`;
}

export function money(usd?: number | null): string {
  return typeof usd === 'number' ? `$${usd.toFixed(2)}` : '';
}
