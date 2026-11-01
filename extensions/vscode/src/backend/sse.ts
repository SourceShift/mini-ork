/**
 * Minimal server-sent-events frame parser.
 *
 * Extracted from the stream client so it can be tested without a socket: the
 * parsing is where the bugs live (multi-line `data:`, the blank-line
 * terminator, and payloads that are not JSON), not in the transport.
 */
export interface SseEvent {
  event: string;
  data: unknown;
}

/**
 * Parse one frame — the text between blank lines.
 *
 * Returns `undefined` for a frame with no `data:` line, which is what the SSE
 * spec says to ignore (comment-only keep-alives arrive this way).
 */
export function parseSseFrame(frame: string): SseEvent | undefined {
  let event = 'message';
  const dataLines: string[] = [];

  for (const rawLine of frame.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
    if (line.startsWith('event:')) {
      event = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  if (!dataLines.length) {
    return undefined;
  }
  const raw = dataLines.join('\n');
  try {
    return { event, data: JSON.parse(raw) };
  } catch {
    return { event, data: raw };
  }
}

/**
 * Split a growing buffer into complete frames.
 *
 * Returns the parsed frames plus whatever tail is still incomplete, so the
 * caller keeps a single unconsumed remainder instead of re-scanning.
 */
export function drainSseBuffer(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = [];
  let rest = buffer;
  let boundary = rest.indexOf('\n\n');
  while (boundary >= 0) {
    const frame = rest.slice(0, boundary);
    rest = rest.slice(boundary + 2);
    const parsed = parseSseFrame(frame);
    if (parsed) {
      events.push(parsed);
    }
    boundary = rest.indexOf('\n\n');
  }
  return { events, rest };
}
