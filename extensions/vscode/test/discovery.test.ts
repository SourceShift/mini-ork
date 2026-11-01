// Run with: node --test test/
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as net from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';

import {
  findFreePort,
  isInitializedHome,
  isPortOpen,
  readFileToken,
} from '../src/backend/discovery.ts';

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'mo-ext-test-'));
}

test('findFreePort returns a bindable port', async () => {
  const port = await findFreePort('127.0.0.1');
  assert.ok(port > 0 && port < 65536);
  // The port was released, so it must be bindable again.
  await new Promise<void>((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => server.close(() => resolve()));
  });
});

test('isPortOpen is false for a closed port', async () => {
  const port = await findFreePort('127.0.0.1');
  assert.equal(await isPortOpen('127.0.0.1', port, 300), false);
});

test('isPortOpen is true while a server is listening', async () => {
  const port = await findFreePort('127.0.0.1');
  const server = net.createServer();
  await new Promise<void>((resolve) => server.listen(port, '127.0.0.1', resolve));
  try {
    assert.equal(await isPortOpen('127.0.0.1', port, 800), true);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test('isInitializedHome requires a state.db file', () => {
  const dir = tempDir();
  assert.equal(isInitializedHome(dir), false);
  fs.writeFileSync(path.join(dir, 'state.db'), '');
  assert.equal(isInitializedHome(dir), true);
});

test('readFileToken skips comments and blank lines', () => {
  const dir = tempDir();
  fs.writeFileSync(
    path.join(dir, 'auth-tokens.txt'),
    ['# operator tokens', '', '  ', 'abc123  amir', 'def456  ops-bot'].join('\n'),
  );
  assert.equal(readFileToken(dir), 'abc123');
});

test('readFileToken is undefined when the file is absent (fail-closed)', () => {
  assert.equal(readFileToken(tempDir()), undefined);
});

test('readFileToken is undefined for a comment-only file', () => {
  const dir = tempDir();
  fs.writeFileSync(path.join(dir, 'auth-tokens.txt'), '# nothing here\n');
  assert.equal(readFileToken(dir), undefined);
});
