import { useEffect, useRef, useState } from "react";
import type { BusEvent } from "./api";

// The backend emits named SSE events (event: <type>), so we must register a
// listener per known type — EventSource has no wildcard. Derived from every
// bus.emit() call in the backend.
export const KNOWN_EVENT_TYPES = [
  "task.queued",
  "task.running",
  "task.completed",
  "task.failed",
  "task.rate_limited",
  "task.cancelled",
  "task.expired",
  "workflow.started",
  "workflow.completed",
  "workflow.failed",
  "workflow.cancelled",
  "whiteboard.board_opened",
  "whiteboard.card_completed",
  "whiteboard.board_completed",
  "mailbox.reply",
  "research.queued",
  "research.completed",
  "topic_pool.added",
  "archive.snapshot",
  "vault.conflict",
];

export interface SSEState {
  events: BusEvent[];
  connected: boolean;
  lastEvent: BusEvent | null;
}

/**
 * Live event stream from GET /api/events/stream with auto-reconnect.
 * Reconnects resume from the last seen cursor (?since=) and duplicate ids
 * are dropped, so the replay-then-live server behaviour is safe.
 */
export function useSSE(opts: { types?: string[]; max?: number; onEvent?: (e: BusEvent) => void } = {}): SSEState {
  const { types, max = 200 } = opts;
  const [events, setEvents] = useState<BusEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<BusEvent | null>(null);
  const lastIdRef = useRef(0);
  const onEventRef = useRef(opts.onEvent);
  onEventRef.current = opts.onEvent;
  const typesKey = types ? types.join(",") : "";

  useEffect(() => {
    let es: EventSource | null = null;
    let retryTimer: number | undefined;
    let closed = false;

    const handle = (raw: MessageEvent) => {
      let e: BusEvent;
      try {
        e = JSON.parse(raw.data) as BusEvent;
      } catch {
        return;
      }
      if (e.id <= lastIdRef.current) return; // replay overlap after reconnect
      lastIdRef.current = e.id;
      setLastEvent(e);
      setEvents((prev) => {
        const next = [e, ...prev];
        return next.length > max ? next.slice(0, max) : next;
      });
      onEventRef.current?.(e);
    };

    const connect = () => {
      if (closed) return;
      const params = new URLSearchParams({ since: String(lastIdRef.current) });
      if (typesKey) params.set("types", typesKey);
      es = new EventSource(`/api/events/stream?${params.toString()}`);
      const listenTypes = typesKey ? typesKey.split(",") : KNOWN_EVENT_TYPES;
      for (const t of listenTypes) es.addEventListener(t, handle);
      es.onmessage = handle; // unnamed events, just in case
      es.onopen = () => setConnected(true);
      es.onerror = () => {
        setConnected(false);
        es?.close();
        retryTimer = window.setTimeout(connect, 3000);
      };
    };

    connect();
    return () => {
      closed = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      es?.close();
    };
  }, [typesKey, max]);

  return { events, connected, lastEvent };
}
