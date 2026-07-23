import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HandStats, HandStatus, HandWeightRow, Scorecard } from "../api";
import { flushMicrotasks, jsonResponse } from "../test-helpers";
import Hands from "./Hands";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

const hands: HandStatus[] = [
  {
    name: "codex",
    type: "cli",
    installed: true,
    available: true,
    degraded: false,
    cooldown_until: null,
    cooldown_reason: null,
    consecutive_failures: 0,
    fallback_chain: [],
  },
];

const weights: HandWeightRow[] = [
  { scope: "research", hand: "codex", weight: 2, updated_at: "2026-07-21T08:00:00+08:00" },
];

const stats: HandStats = {
  hours: 24,
  since: "2026-07-21T00:00:00+08:00",
  by_hand: {
    codex: { tasks_total: 10, tasks_ok: 8, tasks_failed: 1, tasks_rate_limited: 1, avg_duration_ms: 2000 },
  },
  windows: [],
};

const scorecard: Scorecard = {
  date: "2026-07-21",
  counts: { ok: 3, stub: 1, false_complete: 0 },
  by_hand: { codex: { ok: 3, stub: 1, false_complete: 0 } },
  entries: [
    {
      hand: "codex",
      work_date: "2026-07-21",
      task_id: "t-stub1",
      verdict: "stub",
      reason: "输出过短",
      created_at: "2026-07-21T09:00:00+08:00",
    },
  ],
};

let root: Root | null = null;
let holder: HTMLDivElement | null = null;
let seen: RequestSeen[] = [];

function installBackend(empty = false): void {
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://test");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    seen.push({ path: url.pathname, method, body });

    if (url.pathname === "/api/hands" && method === "GET") {
      return jsonResponse(empty ? [] : hands);
    }
    if (url.pathname === "/api/hands/weights" && method === "GET") {
      return jsonResponse(empty ? [] : weights);
    }
    if (url.pathname === "/api/hands/weights" && method === "PUT") {
      const entries = (body as { entries: unknown[] }).entries;
      return jsonResponse({ ok: true, upserted: entries.length, weights: {} });
    }
    if (url.pathname === "/api/hands/stats") {
      return jsonResponse(empty ? { ...stats, by_hand: {} } : stats);
    }
    if (url.pathname === "/api/hands/scorecard") {
      return jsonResponse(
        empty
          ? { date: "2026-07-21", counts: { ok: 0, stub: 0, false_complete: 0 }, by_hand: {}, entries: [] }
          : scorecard,
      );
    }
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderHands(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter>
        <Hands />
      </MemoryRouter>,
    );
  });
  await act(async () => {
    await flushMicrotasks();
  });
  return holder;
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await flushMicrotasks();
  });
}

function input(element: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  act(() => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  seen = [];
  window.localStorage.clear();
});

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  holder?.remove();
  root = null;
  holder = null;
  vi.unstubAllGlobals();
});

describe("Hands smoke", () => {
  it("renders status chips, weights grid, stats bars and scorecard, then saves an edited weight", async () => {
    installBackend();
    const page = await renderHands();

    // status chip + saved weight cell + stats bar + scorecard counts
    expect(page.textContent).toContain("codex");
    const cells = Array.from(page.querySelectorAll<HTMLInputElement>('input[placeholder="—"]'));
    expect(cells.length).toBe(5); // one row × five scopes
    expect(cells.map((c) => c.value)).toContain("2"); // research|codex from GET /weights
    expect(page.textContent).toContain("10 次");
    expect(page.textContent).toContain("合格 ok");
    expect(page.textContent).toContain("问题条目（1）");
    expect(page.textContent).toContain("输出过短");

    // edit the first (default|codex) cell and save -> PUT with only the dirty entry
    input(cells[0], "3");
    const save = Array.from(page.querySelectorAll("button")).find((b) =>
      b.textContent?.startsWith("保存修改"),
    );
    expect(save).not.toBeUndefined();
    expect((save as HTMLButtonElement).disabled).toBe(false);
    await click(save!);

    expect(seen).toContainEqual({
      path: "/api/hands/weights",
      method: "PUT",
      body: { entries: [{ scope: "default", hand: "codex", weight: 3 }], replace: false },
    });
    expect(page.textContent).toContain("已保存 1 项权重");
  });

  it("survives empty data: no hands, no stats, no scorecard entries", async () => {
    installBackend(true);
    const page = await renderHands();

    expect(page.textContent).toContain("没有执行手");
    expect(page.textContent).toContain("窗口内没有任务统计");
    expect(page.textContent).toContain("该日期还没有评分记录");
  });
});
