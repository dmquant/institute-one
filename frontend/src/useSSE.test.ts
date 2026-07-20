// useSSE state-machine tests. The REAL hook renders through react-dom with a
// scripted fetch fake (in-memory append-only log + FakeStream SSE bodies) —
// no network, no EventSource, no testing-library.
//
// Module isolation: useSSE keeps module-level shared state by design
// (shared.tail / shared.ring / walk), so every test loads a fresh module
// graph via vi.resetModules() + dynamic imports. react and react-dom are
// re-imported per generation too — the hook and the renderer must come from
// the SAME React instance or hooks explode with "invalid hook call".
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BusEvent } from "./api";
import type { SSEState } from "./useSSE";
import { FakeStream, flushMicrotasks, jsonResponse, streamResponse } from "./test-helpers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// ------------------------------------------------- scripted backend state ----
let log: BusEvent[] = []; // the append-only events table
let eventsSince: number[] = []; // ?since= of every /api/events page fetch
let eventsTypes: (string | null)[] = []; // ?types= of every /api/events page fetch
let streamSince: number[] = []; // ?since= of every /api/events/stream connect
let streamTypes: (string | null)[] = []; // ?types= of every stream connect
let streams: FakeStream[] = []; // one per stream connect, in order
let streamFailures = 0; // scripted non-2xx stream handshakes
interface EventPageGate {
  since: number;
  used: boolean;
  started: Promise<void>;
  markStarted: () => void;
  released: Promise<void>;
  release: () => void;
}
let eventPageGate: EventPageGate | null = null;

const ev = (id: number, type = "task.done"): BusEvent => ({
  id,
  type,
  ref_kind: "task",
  ref_id: `t${id}`,
  payload: {},
  created_at: "2026-07-20T00:00:00+08:00",
});

const seed = (n: number, from = 1): void => {
  for (let i = from; i < from + n; i++) log.push(ev(i));
};

function blockEventPage(since: number): EventPageGate {
  let markStarted = (): void => {};
  let release = (): void => {};
  const gate: EventPageGate = {
    since,
    used: false,
    started: new Promise((resolve) => {
      markStarted = () => resolve();
    }),
    markStarted: () => markStarted(),
    released: new Promise((resolve) => {
      release = () => resolve();
    }),
    release: () => release(),
  };
  eventPageGate = gate;
  return gate;
}

function installFetch(): void {
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://test");
    if (url.pathname === "/api/events/stream") {
      streamSince.push(Number(url.searchParams.get("since") ?? "-1"));
      streamTypes.push(url.searchParams.get("types"));
      if (streamFailures > 0) {
        streamFailures--;
        return { ok: false, status: 503, body: null } as unknown as Response;
      }
      const s = new FakeStream(init?.signal ?? null);
      streams.push(s);
      return streamResponse(s);
    }
    if (url.pathname === "/api/events") {
      const since = Number(url.searchParams.get("since") ?? "0");
      const limit = Number(url.searchParams.get("limit") ?? "200");
      eventsSince.push(since);
      const types = url.searchParams.get("types");
      eventsTypes.push(types);
      const prefixes = types?.split(",").filter(Boolean);
      // ASC by id, > since, capped at limit — mirrors the real endpoint
      const page = log
        .filter((e) => e.id > since && (!prefixes || prefixes.some((prefix) => e.type.startsWith(prefix))))
        .slice(0, limit);
      const gate = eventPageGate;
      if (gate && !gate.used && gate.since === since) {
        gate.used = true;
        gate.markStarted();
        await gate.released; // page is a pre-wait snapshot: later rows belong to the dirty follow-up round
      }
      return jsonResponse(page);
    }
    throw new Error(`unexpected fetch: ${url.pathname}`);
  });
}

// ---------------------------------------------------- per-test React world ----
async function loadWorld() {
  vi.resetModules();
  const React = await import("react");
  const dom = await import("react-dom/client");
  const sse = await import("./useSSE");
  return { React, createRoot: dom.createRoot, useSSE: sse.useSSE, act: React.act };
}
type World = Awaited<ReturnType<typeof loadWorld>>;

interface FeedOpts {
  types?: string[];
  max?: number;
  onEvent?: (e: BusEvent) => void;
}

function renderFeed(w: World, opts: FeedOpts = {}) {
  const holder: { state: SSEState | null } = { state: null };
  let currentOpts = opts;
  const Probe = (): null => {
    holder.state = w.useSSE(currentOpts);
    return null;
  };
  const root = w.createRoot(document.createElement("div"));
  w.act(() => {
    root.render(w.React.createElement(Probe));
  });
  return {
    get state(): SSEState {
      return holder.state!;
    },
    unmount: (): void => {
      w.act(() => {
        root.unmount();
      });
    },
    rerender: (nextOpts: FeedOpts): void => {
      currentOpts = nextOpts;
      w.act(() => {
        root.render(w.React.createElement(Probe));
      });
    },
  };
}

/** Settle all in-flight promise chains (bootstrap, catch-up, stream reads). */
const flush = (w: World) =>
  w.act(async () => {
    await flushMicrotasks();
  });

/** Advance fake timers inside act so timer-driven setState stays legal. */
const advance = (w: World, ms: number) =>
  w.act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

beforeEach(() => {
  log = [];
  eventsSince = [];
  eventsTypes = [];
  streamSince = [];
  streamTypes = [];
  streams = [];
  streamFailures = 0;
  eventPageGate = null;
  installFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ------------------------------------------------------------------- tests ----
describe("useSSE cursor state machine", () => {
  it("bootstrap: first pull pages to a short page, seeds history newest-first, history never fires onEvent", async () => {
    seed(5);
    const w = await loadWorld();
    const got: number[] = [];
    const feed = renderFeed(w, { max: 10, onEvent: (e) => got.push(e.id) });
    await flush(w);

    expect(feed.state.connected).toBe(true);
    expect(feed.state.events.map((e) => e.id)).toEqual([5, 4, 3, 2, 1]);
    expect(feed.state.lastEvent?.id).toBe(5);
    expect(got).toEqual([]); // mount history is not "live"
    expect(eventsSince[0]).toBe(0); // the walk started at the log head
    // 5 rows < PAGE ⇒ short page: nothing ever paged past the tail
    expect(Math.max(...eventsSince)).toBe(5);

    // a data frame is only a wake-up: the new event arrives via paging
    seed(1, 6);
    streams[0].push("data: wake\n\n");
    await flush(w);
    expect(got).toEqual([6]);
    expect(feed.state.events.map((e) => e.id)).toEqual([6, 5, 4, 3, 2, 1]);
    expect(feed.state.lastEvent?.id).toBe(6);
    feed.unmount();
  });

  it("pagination: a long log advances the cursor across full pages, in order, no gaps or dupes", async () => {
    seed(2500); // 3 walk pages: 1000 + 1000 + 500(short)
    const w = await loadWorld();
    const got: number[] = [];
    const feed = renderFeed(w, { max: 300, onEvent: (e) => got.push(e.id) });
    await flush(w);

    expect(eventsSince.slice(0, 3)).toEqual([0, 1000, 2000]); // cursor advanced page by page
    expect(feed.state.events).toHaveLength(200); // mount history is ring-capped
    expect(feed.state.events[0].id).toBe(2500);
    expect(feed.state.events[199].id).toBe(2301);
    expect(got).toEqual([]);

    // post-mount burst longer than one page: 1000 + 500(short)
    seed(1500, 2501);
    streams[0].push("data: wake\n\n");
    await flush(w);
    expect(got).toEqual(Array.from({ length: 1500 }, (_, i) => 2501 + i)); // exactly once, ascending
    expect(feed.state.events).toHaveLength(300); // UI window trimmed to max
    expect(feed.state.events[0].id).toBe(4000);
    expect(feed.state.lastEvent?.id).toBe(4000);
    feed.unmount();
  });

  it("reconnect-with-cursor: a dropped stream reconnects at the cursor — no loss, no dupes", async () => {
    vi.useFakeTimers();
    seed(3);
    const w = await loadWorld();
    const got: number[] = [];
    const feed = renderFeed(w, { onEvent: (e) => got.push(e.id) });
    await flush(w);
    expect(feed.state.connected).toBe(true);
    expect(streamSince).toEqual([0]); // first connect raced bootstrap: cursor was still 0

    seed(3, 4); // events land while the connection is dead — no wake-up for these
    streams[0].fail(new Error("connection reset"));
    await flush(w);
    expect(feed.state.connected).toBe(false);
    expect(got).toEqual([]); // blind: nothing delivered yet…

    await advance(w, 3000); // RETRY_MS
    expect(streams).toHaveLength(2);
    expect(streamSince[1]).toBe(3); // reconnect carries the cursor
    expect(feed.state.connected).toBe(true);
    expect(got).toEqual([4, 5, 6]); // …and the on-reconnect catch-up recovered all of it

    streams[1].push("data: wake\n\n"); // a redundant wake-up must not re-apply anything
    await flush(w);
    expect(got).toEqual([4, 5, 6]);
    expect(feed.state.events.map((e) => e.id)).toEqual([6, 5, 4, 3, 2, 1]);
    feed.unmount();
  });

  it("ring eviction: shared history caps at 200, later mounts see the shifted window, the log walk runs once", async () => {
    seed(250);
    const w = await loadWorld();
    const a = renderFeed(w, { max: 500 });
    await flush(w);
    expect(a.state.events).toHaveLength(200); // RING_CAP, not max, bounds mount history
    expect(a.state.events[0].id).toBe(250);
    expect(a.state.events[199].id).toBe(51); // 1..50 evicted during the walk

    const b = renderFeed(w, { max: 100 });
    await flush(w);
    expect(b.state.events).toHaveLength(100); // ring tail slice
    expect(b.state.events[0].id).toBe(250);
    expect(eventsSince.filter((s) => s < 250)).toEqual([0]); // ONE whole-log walk, shared by all mounts

    seed(10, 251);
    streams[0].push("data: wake\n\n"); // wake A only: its applyBatch feeds the shared ring
    await flush(w);
    const c = renderFeed(w, { max: 500 });
    await flush(w);
    expect(c.state.events).toHaveLength(200); // still capped after the push
    expect(c.state.events[0].id).toBe(260);
    expect(c.state.events[199].id).toBe(61); // 51..60 evicted by the new batch
    expect(b.state.events[0].id).toBe(250); // B holds its own cursor: un-woken, unchanged
    a.unmount();
    b.unmount();
    c.unmount();
  });

  it("watchdog: 65s of dead silence aborts the stream and reconnects; heartbeats keep it alive", async () => {
    vi.useFakeTimers();
    seed(3);
    const w = await loadWorld();
    const feed = renderFeed(w);
    await flush(w); // t=0: connected, lastActivity=0
    expect(feed.state.connected).toBe(true);
    expect(streams).toHaveLength(1);

    await advance(w, 30_000); // ticks @15s/@30s: 30s silence < 65s ⇒ alive
    expect(streams).toHaveLength(1);

    streams[0].push(": hb\n\n"); // heartbeat bytes refresh lastActivity (t=30s)
    await flush(w);

    await advance(w, 45_000); // t=75s: only 45s since the heartbeat ⇒ still alive
    expect(streams).toHaveLength(1);
    expect(feed.state.connected).toBe(true);

    await advance(w, 30_000); // t=105s: 75s of silence > 65s ⇒ watchdog aborts
    expect(streams).toHaveLength(1); // aborted, retry timer pending
    expect(feed.state.connected).toBe(false);

    await advance(w, 3000); // RETRY_MS
    expect(streams).toHaveLength(2);
    expect(streamSince[1]).toBe(3); // reconnected at the cursor
    expect(feed.state.connected).toBe(true);
    feed.unmount();
  });

  it("filtered bootstrap: starts empty at the shared tail, sends prefixes, and applies only matching live rows", async () => {
    log.push(ev(1, "task.done"), ev(2, "workflow.started"));
    const w = await loadWorld();
    const got: number[] = [];
    const feed = renderFeed(w, {
      types: ["workflow.", "task.failed"],
      max: 1,
      onEvent: (e) => got.push(e.id),
    });
    await flush(w);

    expect(feed.state.events).toEqual([]);
    expect(feed.state.lastEvent).toBeNull();
    expect(streamTypes).toEqual(["workflow.,task.failed"]);
    expect(eventsTypes).toContain("workflow.,task.failed");

    log.push(ev(3, "task.done"), ev(4, "workflow.completed"), ev(5, "task.failed"));
    streams[0].push("data: wake\n\n");
    await flush(w);

    expect(got).toEqual([4, 5]);
    expect(feed.state.events.map((e) => e.id)).toEqual([5]);
    expect(feed.state.lastEvent?.id).toBe(5);
    feed.unmount();
  });

  it("filtered staleness: reconcile polling finds silent matches and the unfiltered-only watchdog stays off", async () => {
    vi.useFakeTimers();
    log.push(ev(1, "task.done"));
    const w = await loadWorld();
    const got: number[] = [];
    const feed = renderFeed(w, { types: ["workflow."], onEvent: (e) => got.push(e.id) });
    await flush(w);

    log.push(ev(2, "workflow.completed"));
    await advance(w, 15_000); // no stream frame: the reconcile timer is the only wake-up
    expect(got).toEqual([2]);
    expect(feed.state.events.map((e) => e.id)).toEqual([2]);

    await advance(w, 75_000); // filtered silence is legitimate, so no watchdog reconnect
    expect(streams).toHaveLength(1);
    expect(feed.state.connected).toBe(true);
    feed.unmount();
  });

  it("SSE parsing: waits for a complete data-frame boundary and treats heartbeat comments as liveness only", async () => {
    seed(1);
    const w = await loadWorld();
    const got: number[] = [];
    const feed = renderFeed(w, { onEvent: (e) => got.push(e.id) });
    await flush(w);

    log.push(ev(2));
    streams[0].push("event: task.done\nda");
    await flush(w);
    streams[0].push("ta: wake\n");
    await flush(w);
    expect(got).toEqual([]); // data line exists, but the frame has no blank-line terminator yet

    streams[0].push("\n");
    await flush(w);
    expect(got).toEqual([2]);

    log.push(ev(3));
    streams[0].push(": heartbeat\n\n");
    await flush(w);
    expect(got).toEqual([2]); // heartbeat bytes do not claim that event data exists
    feed.unmount();
  });

  it("dirty follow-up: a second wake-up during an in-flight page runs one serial round without loss or duplication", async () => {
    seed(3);
    const w = await loadWorld();
    const got: number[] = [];
    const feed = renderFeed(w, { onEvent: (e) => got.push(e.id) });
    await flush(w);

    const gate = blockEventPage(3);
    log.push(ev(4));
    streams[0].push("data: first\n\n");
    await gate.started; // first catch-up holds a snapshot containing only id=4

    log.push(ev(5));
    streams[0].push("data: second\n\n");
    await flush(w); // second trigger observes applying=true and marks the round dirty
    expect(got).toEqual([]);

    await w.act(async () => {
      gate.release();
      await flushMicrotasks();
    });
    expect(got).toEqual([4, 5]);
    expect(feed.state.events.map((e) => e.id)).toEqual([5, 4, 3, 2, 1]);

    streams[0].push("data: redundant\n\n");
    await flush(w);
    expect(got).toEqual([4, 5]);
    feed.unmount();
  });

  it("stream HTTP failure: renders disconnected history, then retries with the bootstrapped cursor", async () => {
    vi.useFakeTimers();
    streamFailures = 1;
    seed(2);
    const w = await loadWorld();
    const got: number[] = [];
    const feed = renderFeed(w, { onEvent: (e) => got.push(e.id) });
    await flush(w);

    expect(feed.state.connected).toBe(false);
    expect(feed.state.events.map((e) => e.id)).toEqual([2, 1]);
    expect(streamSince).toEqual([0]);

    log.push(ev(3));
    await advance(w, 3000);
    expect(streamSince).toEqual([0, 2]);
    expect(feed.state.connected).toBe(true);
    expect(got).toEqual([3]);
    expect(feed.state.events.map((e) => e.id)).toEqual([3, 2, 1]);
    feed.unmount();
  });

  it("option changes: rebuilding as a filtered feed clears stale unfiltered rows before new matches arrive", async () => {
    log.push(ev(1, "task.done"), ev(2, "workflow.started"));
    const w = await loadWorld();
    const feed = renderFeed(w, { max: 10 });
    await flush(w);
    expect(feed.state.events.map((e) => e.id)).toEqual([2, 1]);

    feed.rerender({ types: ["workflow."], max: 1 });
    await flush(w);
    expect(feed.state.events).toEqual([]);
    expect(feed.state.lastEvent).toBeNull();
    expect(streamTypes[streamTypes.length - 1]).toBe("workflow.");

    log.push(ev(3, "workflow.completed"), ev(4, "task.done"));
    streams[streams.length - 1].push("data: wake\n\n");
    await flush(w);
    expect(feed.state.events.map((e) => e.id)).toEqual([3]);
    expect(feed.state.lastEvent?.id).toBe(3);
    feed.unmount();
  });

  it("cleanup: aborts the active stream and cancels watchdog, reconcile, and retry work", async () => {
    vi.useFakeTimers();
    seed(1);
    const w = await loadWorld();
    const feed = renderFeed(w);
    await flush(w);
    expect(streams).toHaveLength(1);

    feed.unmount();
    await advance(w, 120_000);
    expect(streams).toHaveLength(1);
    expect(streamSince).toEqual([0]);
  });
});
