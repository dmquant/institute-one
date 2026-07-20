import { useEffect, useRef, useState } from "react";
import { listEvents } from "./api";
import type { BusEvent } from "./api";

// Live events reach the UI through exactly ONE path: paging
// GET /api/events?since= — the durable, ordered log (emit() INSERTs into the
// events table BEFORE any fan-out, and nothing ever deletes from it). The SSE
// stream is used only as a low-latency wake-up signal, never as a data source.
//
// Why stream frames must NOT be applied as data (R-C7 / C7-H1): the stream
// endpoint replays at most 500 rows, registers its live queue only AFTER the
// replay snapshot (events emitted in between reach neither replay nor queue),
// and a full live queue silently drops frames. Applying a frame with id=X
// while events in (cursor, X) were dropped would advance the cursor past the
// gap forever — the lost events could never be recovered.
//
// Timing guarantee — post-mount events are delivered exactly once, in id
// order, under any interleaving:
//  - catchUp() is the only thing that advances the cursor. It pages
//    `id > cursor` in ASC order until a short page, so a round ends exactly
//    at the DB tail of that instant, and the cursor only moves over events
//    actually delivered — it can never skip a gap the stream dropped.
//  - Rounds are strictly serial: `applying` rejects re-entry and a trigger
//    firing mid-round sets `dirty`, scheduling exactly one follow-up round.
//    Both flags flip in synchronous sections only, so no wake-up can slip
//    between the checks. Monotonic cursor + serial rounds ⇒ no duplicates,
//    no reordering.
//  - Every stream loss mode (replay/subscribe handshake race, >500 backlog
//    truncation, live-queue overflow, dead connection) therefore at worst
//    DELAYS a wake-up — never the data. The on-reconnect catch-up, the
//    reconcile timer and the stall watchdog bound that delay.
//
// fetch + hand-rolled SSE parsing (not EventSource): the server emits *named*
// events (event: <type>), which EventSource only exposes via per-name
// listeners — useless as a type-agnostic wake-up signal (ROUND2-AUDIT-F2).

export interface SSEState {
  events: BusEvent[];
  connected: boolean;
  lastEvent: BusEvent | null;
}

const PAGE = 1000; // catch-up page size (localhost SQLite)
const RECONCILE_MS = 15_000; // poll fallback: bounds staleness if the stream drops frames
const STALL_MS = 65_000; // unfiltered stream sends ≥1 line per 25s (event or heartbeat); 65s ⇒ dead
const RETRY_MS = 3_000;
const RING_CAP = 200; // shared history window; callers with max > RING_CAP get RING_CAP rows at mount

// Cross-instance shared state (the log is append-only, ids are global).
// `tail`: highest id ANY instance has seen — a safe fast-forward hint (an
// undershoot only means fetching a few extra rows). `ring`: the most recent
// CONTIGUOUS unfiltered events; every unfiltered instance appends to it, so a
// mounting feed gets its history without re-walking the log. Contiguity
// holds because each appended batch is a complete id range (cursor, last]
// and ringPush keeps only the part above the ring's current tail.
const shared = { tail: 0, ring: [] as BusEvent[] };

const ringTail = () => (shared.ring.length ? shared.ring[shared.ring.length - 1].id : 0);

function ringPush(batch: BusEvent[]) {
  const add = batch.filter((e) => e.id > ringTail());
  if (add.length === 0) return;
  shared.ring = shared.ring.concat(add);
  if (shared.ring.length > RING_CAP) shared.ring = shared.ring.slice(shared.ring.length - RING_CAP);
}

// One whole-log walk per SPA load, shared by all instances (12+ hooks mount
// across the app; without this every mount would page the full log). Walks
// unfiltered from the shared tail to the current end, seeding tail + ring.
let walk: Promise<void> | null = null;

function ensureTail(): Promise<void> {
  walk ??= (async () => {
    for (;;) {
      const batch = await listEvents(shared.tail, undefined, PAGE);
      if (batch.length > 0) {
        shared.tail = Math.max(shared.tail, batch[batch.length - 1].id);
        ringPush(batch);
      }
      if (batch.length < PAGE) return; // short page ⇒ reached the log tail
    }
  })().catch((e: unknown) => {
    walk = null; // failed (backend down?): let the next trigger retry
    throw e;
  });
  return walk;
}

/**
 * Live event feed backed by the durable cursor endpoint, with the SSE stream
 * as a push trigger and auto-reconnect. Post-mount events are delivered
 * exactly once and in id order (timing notes above). Unfiltered hooks seed
 * `events` with the most recent history at mount; filtered hooks start empty
 * at the log tail (every current filtered caller is a max=1 reload trigger).
 * `types` are prefixes, filtered server-side (LIKE / startswith).
 */
export function useSSE(opts: { types?: string[]; max?: number; onEvent?: (e: BusEvent) => void } = {}): SSEState {
  const { types, max = 200 } = opts;
  const [events, setEvents] = useState<BusEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<BusEvent | null>(null);
  const onEventRef = useRef(opts.onEvent);
  onEventRef.current = opts.onEvent;
  const typesKey = types ? types.join(",") : "";

  useEffect(() => {
    // Cursor state is effect-local: a types/max change or StrictMode's dev
    // double-mount rebuilds the feed (stale filtered rows must not linger).
    setEvents([]);
    let cursor = 0; // highest event id already applied to this instance
    let bootstrapped = false;
    let closed = false;
    let retryTimer: number | undefined;
    let ctrl: AbortController | null = null;
    let applying = false; // a catch-up round is in flight
    let dirty = false; // a trigger arrived mid-round: run one more round
    let streamLive = false; // current stream fetch is open and being read
    let lastActivity = 0; // Date.now() of the last stream bytes (data OR heartbeat)

    // Mount: land the cursor on the log tail and (unfiltered) render recent
    // history. History does not fire onEvent — the live contract starts at
    // mount; pages already fetch their initial state via useLoad.
    const bootstrap = async () => {
      await ensureTail();
      if (closed) return;
      if (typesKey) {
        // shared.tail may exceed the newest MATCHING event — the feed just
        // starts empty and lastEvent stays null until a matching emit
        cursor = shared.tail;
      } else {
        // resume from the ring tail, NOT shared.tail: a filtered instance
        // may have pushed shared.tail past the ring, and skipping there
        // would punch a permanent hole into this feed's history
        cursor = ringTail();
        const hist = shared.ring.slice(-max);
        if (hist.length > 0) {
          setLastEvent(hist[hist.length - 1]);
          setEvents(hist.slice().reverse()); // newest first
        }
      }
      bootstrapped = true;
    };

    const applyBatch = (batch: BusEvent[]) => {
      // batch is ASC by id (SQL ORDER BY); the filter preserves that order
      const fresh = batch.filter((e) => e.id > cursor);
      if (fresh.length === 0) return;
      cursor = fresh[fresh.length - 1].id;
      shared.tail = Math.max(shared.tail, cursor);
      if (!typesKey) ringPush(fresh);
      setLastEvent(fresh[fresh.length - 1]);
      setEvents((prev) => {
        const next = [...fresh].reverse().concat(prev); // newest first
        return next.length > max ? next.slice(0, max) : next;
      });
      for (const e of fresh) onEventRef.current?.(e);
    };

    const catchUp = async () => {
      if (applying) {
        dirty = true;
        return;
      }
      applying = true;
      try {
        do {
          dirty = false;
          if (!bootstrapped) {
            await bootstrap(); // a trigger during it sets dirty ⇒ one more round
            if (closed) return;
            continue;
          }
          for (;;) {
            const batch = await listEvents(cursor, typesKey || undefined, PAGE);
            if (closed) return;
            applyBatch(batch);
            if (batch.length < PAGE) break; // short page ⇒ caught up to the DB tail
          }
        } while (dirty);
      } catch {
        // network hiccup: the next trigger (frame, reconnect, timer) retries;
        // the cursor did not move, so nothing was skipped
      } finally {
        applying = false;
      }
    };

    const connect = async () => {
      if (closed) return;
      ctrl = new AbortController();
      // ?since= only trims the server-side replay; frames are never consumed
      // as data, so the replay cap / handshake race cannot lose anything here.
      const params = new URLSearchParams({ since: String(cursor) });
      if (typesKey) params.set("types", typesKey);
      try {
        const res = await fetch(`/api/events/stream?${params.toString()}`, {
          signal: ctrl.signal,
          headers: { Accept: "text/event-stream" },
        });
        if (!res.ok || !res.body) throw new Error(`stream http ${res.status}`);
        streamLive = true;
        lastActivity = Date.now();
        setConnected(true);
        // (re)connect ⇒ we may have been blind for a while: reconcile now
        void catchUp();

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        let sawData = false;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          lastActivity = Date.now();
          buf += decoder.decode(value, { stream: true });
          let nl: number;
          while ((nl = buf.indexOf("\n")) !== -1) {
            let line = buf.slice(0, nl);
            buf = buf.slice(nl + 1);
            if (line.endsWith("\r")) line = line.slice(0, -1);
            if (line === "") {
              // frame boundary: a data-bearing frame means new events exist —
              // wake the catch-up loop (the frame body itself is discarded)
              if (sawData) {
                sawData = false;
                void catchUp();
              }
              continue;
            }
            if (line.startsWith(":")) continue; // heartbeat comment — liveness only
            if (line.startsWith("data:")) sawData = true;
            // id:/event: lines carry nothing the catch-up loop needs
          }
        }
        throw new Error("stream ended"); // server closed: fall through to retry
      } catch {
        if (closed) return;
        streamLive = false;
        setConnected(false);
        retryTimer = window.setTimeout(() => void connect(), RETRY_MS);
      }
    };

    // One timer, two jobs:
    //  - reconcile poll: even when the stream misses frames and then goes
    //    quiet (race window, queue overflow), staleness stays ≤ RECONCILE_MS;
    //  - stall watchdog: a half-open TCP (e.g. laptop sleep/wake) never
    //    errors the read — abort it so the retry path takes over. UNFILTERED
    //    streams only: the server's 25s heartbeat timer resets on every bus
    //    event, so a filtered stream can legitimately stay silent for long
    //    stretches while non-matching events flow; silence there proves
    //    nothing (its freshness is covered by the reconcile poll anyway).
    const interval = window.setInterval(() => {
      void catchUp();
      if (!typesKey && streamLive && Date.now() - lastActivity > STALL_MS) {
        ctrl?.abort(); // read() rejects ⇒ reconnect in RETRY_MS
      }
    }, RECONCILE_MS);
    void catchUp(); // bootstrap: seed history, land the cursor on the tail
    void connect();
    return () => {
      closed = true;
      window.clearInterval(interval);
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      ctrl?.abort();
    };
  }, [typesKey, max]);

  return { events, connected, lastEvent };
}
