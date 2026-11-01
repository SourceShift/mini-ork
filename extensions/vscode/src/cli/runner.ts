import { spawn } from 'child_process';

import { info, warn } from '../log';

/** One streamed line from a `mini-ork` invocation. */
export interface CliLine {
  kind: 'cmd' | 'out' | 'err';
  text: string;
}

export interface CliRunOptions {
  /** Absolute path to the mini-ork binary. */
  binary: string;
  /** argv after the binary name, e.g. `['board', 'retry', 'run-…']`. */
  args: string[];
  /** The project home (`--home`). Undefined runs against the cwd's project. */
  home?: string;
  cwd: string;
  /**
   * Extra environment. `MINI_ORK_HOME` is always set when `home` is given, so a
   * subcommand whose own parser rejects `--home` still reads the right project.
   */
  env?: Record<string, string>;
  /** Hard ceiling; the child is killed when it elapses. */
  timeoutMs?: number;
  onLine: (line: CliLine) => void;
}

export interface CliResult {
  code: number | null;
  timedOut: boolean;
  /** The last few stderr lines, for a toast. */
  tail: string;
}

/**
 * Run one `mini-ork` subcommand and stream its output.
 *
 * The IDE never reimplements a verb: the backend's page spec hands us the exact
 * argv (`{"cli": ["board", "retry", id]}`), so this is the single place a write
 * leaves the extension. Three rules matter here:
 *
 * 1. **No shell.** `spawn` without `shell: true` means a run title containing
 *    `$(…)` is an argument, never a command.
 * 2. **`--home` is appended, not assumed.** A subcommand's own parser is the
 *    authority on whether it accepts `--home`; `home: false` in the spec means
 *    the IDE leaves the flag off and leans on `MINI_ORK_HOME` instead.
 * 3. **A timeout kills the child.** A wedged CLI must not pin the extension
 *    host forever — the caller sees `timedOut: true` and says so.
 */
export function runCli(opts: CliRunOptions): Promise<CliResult> {
  const args = [...opts.args];
  if (opts.home) {
    args.push('--home', opts.home);
  }
  const env: NodeJS.ProcessEnv = { ...process.env, ...(opts.env ?? {}) };
  if (opts.home) {
    env.MINI_ORK_HOME = opts.home;
  }
  // The CLI re-execs itself through the same interpreter; pinning the venv keeps
  // a run started from the IDE on the engine the backend is actually serving.
  env.FORCE_COLOR = '0';
  env.NO_COLOR = '1';

  const printable = `mini-ork ${args.join(' ')}`;
  opts.onLine({ kind: 'cmd', text: `$ ${printable}` });
  info(printable);

  return new Promise<CliResult>((resolve) => {
    const timeoutMs = opts.timeoutMs ?? 180_000;
    let timedOut = false;
    let tail: string[] = [];

    let child;
    try {
      child = spawn(opts.binary, args, {
        cwd: opts.cwd,
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      opts.onLine({ kind: 'err', text: `spawn failed: ${msg}` });
      resolve({ code: null, timedOut: false, tail: msg });
      return;
    }

    const timer = setTimeout(() => {
      timedOut = true;
      warn(`mini-ork ${args[0] ?? ''} exceeded ${timeoutMs}ms — killing`);
      child.kill('SIGTERM');
      // A CLI waiting on a network read can ignore SIGTERM; escalate once.
      setTimeout(() => child.kill('SIGKILL'), 3_000);
    }, timeoutMs);

    const pipe = (stream: NodeJS.ReadableStream | null, kind: 'out' | 'err'): void => {
      if (!stream) {
        return;
      }
      let buf = '';
      stream.setEncoding('utf8');
      stream.on('data', (chunk: string) => {
        buf += chunk;
        const lines = buf.split(/\r?\n/);
        buf = lines.pop() ?? '';
        for (const line of lines) {
          if (!line) {
            continue;
          }
          opts.onLine({ kind, text: line });
          if (kind === 'err') {
            tail.push(line);
            if (tail.length > 8) {
              tail = tail.slice(-8);
            }
          }
        }
      });
      stream.on('end', () => {
        if (buf) {
          opts.onLine({ kind, text: buf });
          if (kind === 'err') {
            tail.push(buf);
          }
        }
      });
    };
    pipe(child.stdout, 'out');
    pipe(child.stderr, 'err');

    child.on('error', (err) => {
      clearTimeout(timer);
      opts.onLine({ kind: 'err', text: err.message });
      resolve({ code: null, timedOut, tail: err.message });
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      const text = timedOut
        ? `timed out after ${timeoutMs}ms`
        : code === 0
          ? 'ok'
          : `exit ${code}`;
      opts.onLine({ kind: 'out', text: `— ${text}` });
      resolve({ code, timedOut, tail: tail.join('\n') });
    });
  });
}
