// Shared test fakes: a push-driven streaming body plus minimal Response
// look-alikes, so tests control fetch timing precisely — no sockets, no
// undici Response dependency (jsdom lacks fetch internals anyway).

export interface ReadResult {
  done: boolean;
  value?: Uint8Array;
}

/** Push-driven stand-in for a fetch body reader (SSE / NDJSON streams). */
export class FakeStream {
  private queue: ReadResult[] = [];
  private waiter: { resolve: (r: ReadResult) => void; reject: (e: unknown) => void } | null = null;
  private error: unknown = null;

  constructor(signal?: AbortSignal | null) {
    signal?.addEventListener("abort", () => {
      this.fail(new DOMException("The operation was aborted.", "AbortError"));
    });
  }

  push(text: string): void {
    this.enqueue({ done: false, value: new TextEncoder().encode(text) });
  }

  /** Server closed the stream: read() resolves { done: true }. */
  close(): void {
    this.enqueue({ done: true });
  }

  /** Break the stream: pending and future read() calls reject. */
  fail(e: unknown): void {
    this.error = e;
    if (this.waiter) {
      const w = this.waiter;
      this.waiter = null;
      w.reject(e);
    }
  }

  private enqueue(r: ReadResult): void {
    if (this.waiter) {
      const w = this.waiter;
      this.waiter = null;
      w.resolve(r);
    } else {
      this.queue.push(r);
    }
  }

  read(): Promise<ReadResult> {
    if (this.error !== null) return Promise.reject(this.error);
    const next = this.queue.shift();
    if (next) return Promise.resolve(next);
    return new Promise((resolve, reject) => {
      this.waiter = { resolve, reject };
    });
  }
}

/** Minimal Response shape for JSON endpoints (api.ts req() checks ok/headers/json). */
export function jsonResponse(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: { get: (h: string) => (h.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => data,
  } as unknown as Response;
}

/** Minimal streaming Response whose body reads from a FakeStream. */
export function streamResponse(stream: FakeStream): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: { get: () => null },
    body: {
      getReader: () => ({
        read: () => stream.read(),
        cancel: async () => {},
      }),
    },
  } as unknown as Response;
}

/** Drain queued microtasks so multi-await chains settle under fake timers. */
export async function flushMicrotasks(rounds = 25): Promise<void> {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}
