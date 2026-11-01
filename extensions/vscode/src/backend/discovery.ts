import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as net from 'net';
import * as os from 'os';
import * as path from 'path';

const EXE = process.platform === 'win32' ? 'mini-ork.exe' : 'mini-ork';

/**
 * Locate the `mini-ork` executable.
 *
 * Order matters: an explicit setting wins, then the workspace's own
 * `bin/mini-ork` (so a developer running the checkout gets the checkout, not
 * whatever pip installed months ago), then PATH, then the user-local install
 * the Zed panel pins. The workspace entry is a symlink-free real file check —
 * a stale symlink resolving nowhere must not shadow a working PATH binary.
 */
export function findBinary(explicitPath: string, workspaceRoot?: string): string | undefined {
  if (explicitPath) {
    const expanded = explicitPath.replace(/^~(?=$|\/)/, os.homedir());
    return isExecutableFile(expanded) ? expanded : undefined;
  }

  const candidates: string[] = [];
  if (workspaceRoot) {
    candidates.push(path.join(workspaceRoot, 'bin', EXE));
  }
  candidates.push(path.join(os.homedir(), '.local', 'bin', EXE));

  for (const c of candidates) {
    if (isExecutableFile(c)) {
      return c;
    }
  }

  const fromPath = which(EXE);
  return fromPath;
}

function isExecutableFile(p: string): boolean {
  try {
    const st = fs.statSync(p);
    if (!st.isFile()) {
      return false;
    }
    if (process.platform !== 'win32') {
      fs.accessSync(p, fs.constants.X_OK);
    }
    return true;
  } catch {
    return false;
  }
}

function which(cmd: string): string | undefined {
  const probe = spawnSync(process.platform === 'win32' ? 'where' : 'which', [cmd], {
    encoding: 'utf8',
  });
  if (probe.status !== 0 || !probe.stdout) {
    return undefined;
  }
  const first = probe.stdout.split(/\r?\n/).map((s) => s.trim()).find(Boolean);
  return first && isExecutableFile(first) ? first : undefined;
}

/** True when something accepts TCP connections on host:port. */
export function isPortOpen(host: string, port: number, timeoutMs = 400): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = new net.Socket();
    let settled = false;
    const done = (open: boolean): void => {
      if (settled) {
        return;
      }
      settled = true;
      socket.destroy();
      resolve(open);
    };
    socket.setTimeout(timeoutMs);
    socket.once('connect', () => done(true));
    socket.once('timeout', () => done(false));
    socket.once('error', () => done(false));
    socket.connect(port, host);
  });
}

/** Ask the OS for a free port, then release it for the child to claim. */
export function findFreePort(host: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once('error', reject);
    server.listen(0, host, () => {
      const addr = server.address();
      if (typeof addr === 'object' && addr) {
        const { port } = addr;
        server.close(() => resolve(port));
      } else {
        server.close(() => reject(new Error('could not allocate a port')));
      }
    });
  });
}

/**
 * Whether `home` looks like an initialized mini-ork project.
 *
 * `serve` refuses to boot without `state.db`, so this is the check that turns
 * a confusing crash-on-start into an actionable "run mini-ork init".
 */
export function isInitializedHome(home: string): boolean {
  try {
    return fs.statSync(path.join(home, 'state.db')).isFile();
  } catch {
    return false;
  }
}

/** First non-comment token from `<home>/auth-tokens.txt`, if any. */
export function readFileToken(home: string): string | undefined {
  try {
    const raw = fs.readFileSync(path.join(home, 'auth-tokens.txt'), 'utf8');
    for (const line of raw.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) {
        continue;
      }
      const token = trimmed.split(/\s+/)[0];
      if (token) {
        return token;
      }
    }
  } catch {
    /* fail-closed: no file, no token */
  }
  return undefined;
}
