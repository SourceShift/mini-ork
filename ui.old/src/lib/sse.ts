/**
 * Tiny SSE consumer hook. EventSource auto-reconnects on its own, but we
 * still wrap it so callers get a clean React API + invalidation hooks.
 */

import { useEffect } from "react";

import { withWorkspaceParam } from "./api";

export function useEventStream(
  path: string,
  onEvent: (name: string, data: unknown) => void,
  enabled = true,
) {
  useEffect(() => {
    if (!enabled) return;
    // EventSource can't send headers, so workspace rides a query param.
    const es = new EventSource(withWorkspaceParam(path));
    const handle = (name: string) => (ev: MessageEvent) => {
      try {
        onEvent(name, JSON.parse(ev.data));
      } catch {
        onEvent(name, ev.data);
      }
    };
    es.addEventListener("hello", handle("hello"));
    es.addEventListener("event", handle("event"));
    es.onerror = () => {
      // EventSource will reconnect automatically; surface as silent.
      // Consumers can detect "stuck" state via stale data + react-query's
      // dataUpdatedAt timestamp.
    };
    return () => es.close();
  }, [path, enabled, onEvent]);
}
