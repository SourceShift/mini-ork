// Run with: node --test test/
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { isDestructive, needsConfirm, verbOf } from '../src/actions/verbs.ts';

test('a leading verb is found', () => {
  assert.equal(verbOf(['run', 'fix the parser']), 'run');
  assert.equal(verbOf(['repair', 'run-42']), 'repair');
});

test('a noun-first family still yields its verb', () => {
  // The page spec emits `board kill`, `board merge`, `pm remove` — the verb is
  // the second token, and a guard that only read args[0] would miss it.
  assert.equal(verbOf(['board', 'kill', 'run-42']), 'kill');
  assert.equal(verbOf(['board', 'merge', 'wt/x']), 'merge');
  assert.equal(verbOf(['pm', 'remove', 'pkg']), 'remove');
});

test('kill and merge are destructive, so they get a confirm sheet', () => {
  assert.equal(isDestructive(['board', 'kill', 'run-42']), true);
  assert.equal(isDestructive(['board', 'merge', 'wt/x']), true);
  assert.equal(needsConfirm(['board', 'kill', 'run-42']), true);
});

test('spending verbs are confirmed but not destructive', () => {
  assert.equal(needsConfirm(['board', 'retry', 'run-42']), true);
  assert.equal(isDestructive(['board', 'retry', 'run-42']), false);
});

test('race-* sub-commands map onto the same guard', () => {
  assert.equal(verbOf(['race-stop', 'r1']), 'stop');
  assert.equal(isDestructive(['race-stop', 'r1']), true);
  assert.equal(needsConfirm(['race-start', 'r1']), true);
});

test('reading a page is not confirmed', () => {
  assert.equal(needsConfirm(['board', 'show', 'run-42']), false);
  assert.equal(needsConfirm(['verifier', 'list']), false);
});

test('a title that merely contains a verb word is not the verb', () => {
  // One argv token: the whole sentence, so no exact token match.
  assert.equal(needsConfirm(['run', 'stop the flaky retry loop']), true); // `run` leads
  assert.equal(needsConfirm(['threads', 'send', 'stop']), false);
});

test('no args is no verb', () => {
  assert.equal(verbOf([]), undefined);
  assert.equal(needsConfirm(undefined), false);
});
