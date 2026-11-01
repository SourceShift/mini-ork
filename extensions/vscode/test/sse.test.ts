// Run with: node --test test/
// Node 22+ strips types natively, so these run against the sources directly.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { drainSseBuffer, parseSseFrame } from '../src/backend/sse.ts';

test('parses a named event with a JSON payload', () => {
  const evt = parseSseFrame('event: node_update\ndata: {"node_id":"n1"}');
  assert.deepEqual(evt, { event: 'node_update', data: { node_id: 'n1' } });
});

test('defaults the event name to "message"', () => {
  const evt = parseSseFrame('data: {"a":1}');
  assert.equal(evt?.event, 'message');
});

test('joins multi-line data before parsing', () => {
  const evt = parseSseFrame('event: x\ndata: {"a":\ndata: 1}');
  assert.deepEqual(evt?.data, { a: 1 });
});

test('keeps a non-JSON payload as a string instead of dropping it', () => {
  const evt = parseSseFrame('event: log\ndata: plain text line');
  assert.deepEqual(evt, { event: 'log', data: 'plain text line' });
});

test('ignores a frame with no data line (keep-alive comment)', () => {
  assert.equal(parseSseFrame(': keep-alive'), undefined);
});

test('tolerates CRLF line endings', () => {
  const evt = parseSseFrame('event: x\r\ndata: {"ok":true}\r');
  assert.deepEqual(evt?.data, { ok: true });
});

test('drains complete frames and keeps the incomplete tail', () => {
  const { events, rest } = drainSseBuffer('data: {"i":0}\n\ndata: {"i":1}\n\ndata: {"i"');
  assert.equal(events.length, 2);
  assert.deepEqual(events[1].data, { i: 1 });
  assert.equal(rest, 'data: {"i"');
});

test('a buffer with no terminator yields nothing and is preserved', () => {
  const { events, rest } = drainSseBuffer('data: {"i":0}');
  assert.equal(events.length, 0);
  assert.equal(rest, 'data: {"i":0}');
});

test('draining a streamed sequence in chunks matches draining it whole', () => {
  const whole = 'data: {"n":1}\n\ndata: {"n":2}\n\ndata: {"n":3}\n\n';
  const wholeResult = drainSseBuffer(whole);

  let buffer = '';
  const collected: unknown[] = [];
  for (const chunk of ['data: {"n"', ':1}\n\nda', 'ta: {"n":2}\n\ndata: {"n":', '3}\n\n']) {
    buffer += chunk;
    const { events, rest } = drainSseBuffer(buffer);
    buffer = rest;
    collected.push(...events.map((e) => e.data));
  }

  assert.deepEqual(collected, wholeResult.events.map((e) => e.data));
});
