// askStream NDJSON consumer tests: split-line buffering, done handling,
// malformed-frame tolerance and the no-done EOF reject. Fetch is faked with
// a FakeStream body so byte boundaries are fully scripted.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, askStream, getHealth } from "./api";
import type { AskStreamFrame } from "./api";
import { FakeStream, jsonResponse, streamResponse } from "./test-helpers";

let stream: FakeStream;

beforeEach(() => {
  stream = new FakeStream();
  vi.stubGlobal("fetch", async (input: RequestInfo | URL): Promise<Response> => {
    const url = new URL(String(input), "http://test");
    if (url.pathname !== "/api/ask/stream") throw new Error(`unexpected fetch: ${url.pathname}`);
    return streamResponse(stream);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const collect = () => {
  const frames: AskStreamFrame[] = [];
  const promise = askStream({ prompt: "hi" }, (f) => frames.push(f));
  return { frames, promise };
};

describe("askStream NDJSON parsing", () => {
  it("parses line-by-line, buffering half lines across chunks", async () => {
    const { frames, promise } = collect();
    // one frame split across three reads + two frames in one read
    stream.push('{"type":"stdout","te');
    stream.push('xt":"hel');
    stream.push('lo"}\n{"type":"stderr","text":"warn"}\n{"type":"status","text":"running"}\n');
    stream.push('{"type":"done","task":{"id":"t1","status":"completed","hand":"codex","exit_code":0,"error":null,"output":"hello"}}\n');
    stream.close();

    const task = await promise;
    expect(frames).toEqual([
      { type: "stdout", text: "hello" },
      { type: "stderr", text: "warn" },
      { type: "status", text: "running" },
      { type: "done", task },
    ]);
    expect(task).toEqual({
      id: "t1",
      status: "completed",
      hand: "codex",
      exit_code: 0,
      error: null,
      output: "hello",
    });
  });

  it("parses a trailing done frame without a final newline", async () => {
    const { promise } = collect();
    stream.push('{"type":"done","task":{"id":"t2","status":"completed","hand":null,"exit_code":0,"error":null,"output":""}}');
    stream.close(); // EOF with the done frame still in the buffer
    const task = await promise;
    expect(task.id).toBe("t2");
  });

  it("done 容错: malformed lines, unknown frame types and a degenerate done.task survive", async () => {
    const { frames, promise } = collect();
    stream.push("not json at all\n"); // malformed — skipped
    stream.push('"just a string"\n'); // valid JSON, not an object — skipped
    stream.push('{"type":"mystery","text":"?"}\n'); // unknown type — surfaced as status
    stream.push('{"type":"stdout"}\n'); // missing text — defaults to ""
    stream.push('{"type":"done","task":{"id":42,"status":7}}\n'); // wrong-typed task fields
    stream.close();

    const task = await promise;
    expect(frames[0].type).toBe("status"); // the unknown-frame surface line
    expect((frames[0] as { text: string }).text).toContain("mystery");
    expect(frames[1]).toEqual({ type: "stdout", text: "" });
    // coerceDoneTask defaulted every wrong-typed field instead of crashing
    expect(task).toEqual({
      id: null,
      status: "failed",
      hand: null,
      exit_code: null,
      error: null,
      output: "",
    });
  });

  it("EOF without a done frame rejects (incomplete response)", async () => {
    const { frames, promise } = collect();
    const rejection = expect(promise).rejects.toThrow(/done 帧前结束/);
    stream.push('{"type":"stdout","text":"partial"}\n');
    stream.close();
    await rejection;
    expect(frames).toEqual([{ type: "stdout", text: "partial" }]);
  });
});

describe("req 15s timeout", () => {
  it("a wedged backend rejects as ApiError(408) instead of spinning forever", async () => {
    vi.useFakeTimers();
    try {
      // fetch that never answers but honors abort like a real one
      vi.stubGlobal(
        "fetch",
        (_input: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(new DOMException("The operation was aborted.", "AbortError")),
            );
          }),
      );
      const pending = getHealth();
      const rejection = expect(pending).rejects.toMatchObject({
        status: 408,
        message: expect.stringContaining("超时"),
      });
      await vi.advanceTimersByTimeAsync(15_000);
      await rejection;
      await expect(pending).rejects.toBeInstanceOf(ApiError);
    } finally {
      vi.useRealTimers();
    }
  });

  it("fast responses are unaffected by the timer", async () => {
    vi.stubGlobal("fetch", async () => jsonResponse({ status: "ok" }));
    await expect(getHealth()).resolves.toEqual({ status: "ok" });
  });
});
