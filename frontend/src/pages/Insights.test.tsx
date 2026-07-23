import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BusEvent, Favorite, ResearchItem, TaskRow } from "../api";
import { flushMicrotasks, jsonResponse } from "../test-helpers";
import Insights from "./Insights";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

const now = Date.now();

const favs: Favorite[] = [
  {
    id: 1,
    ref_kind: "research",
    ref_id: "r-1",
    note: "",
    created_at: new Date(now - 172800 * 1000).toISOString(), // 2 days ago
    title: "测试深度研究",
    status: "completed",
  },
];

// events in last 30 days (yesterday)
const recentEvents: BusEvent[] = [
  {
    id: 1,
    type: "research.completed",
    ref_kind: "research",
    ref_id: "r-x",
    payload: {},
    created_at: new Date(now - 86400 * 1000).toISOString(),
  },
  {
    id: 2,
    type: "task.completed",
    ref_kind: "task",
    ref_id: "t-y",
    payload: {},
    created_at: new Date(now - 86400 * 1000).toISOString(),
  },
];

// terminal tasks with mixed outcomes
const tasks: TaskRow[] = [
  {
    id: "t-a",
    session_id: null,
    hand: "codex",
    requested_hand: "codex",
    model: null,
    status: "completed",
    source: "whiteboard",
    exit_code: 0,
    error: null,
    parent_run_id: null,
    created_at: new Date(now - 172800 * 1000).toISOString(),
    started_at: new Date(now - 172800 * 1000).toISOString(),
    finished_at: new Date(now - 172800 * 1000).toISOString(),
  },
  {
    id: "t-b",
    session_id: null,
    hand: "codex",
    requested_hand: "codex",
    model: null,
    status: "failed",
    source: "whiteboard",
    exit_code: 1,
    error: "test failure",
    parent_run_id: null,
    created_at: new Date(now - 172800 * 1000).toISOString(),
    started_at: new Date(now - 172800 * 1000).toISOString(),
    finished_at: new Date(now - 172800 * 1000).toISOString(),
  },
];

// completed research items from last 30 days
const researchItems: ResearchItem[] = [
  {
    id: "i-1",
    topic: "topic 1",
    priority: 0,
    status: "completed",
    source: "default",
    run_id: "run-1",
    error: null,
    created_at: new Date(now - 86400 * 1000).toISOString(),
    started_at: new Date(now - 86400 * 1000).toISOString(),
    finished_at: new Date(now - 86400 * 1000).toISOString(),
  },
];

let root: Root | null = null;
let holder: HTMLDivElement | null = null;
let seen: RequestSeen[] = [];
let favSet: Set<number> = new Set();

function installBackend(favIds: Set<number>, empty = false): void {
  favSet = favIds;

  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://test");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    seen.push({ path: url.pathname, method, body });

    if (url.pathname === "/api/favorites" && method === "GET") {
      return jsonResponse(empty ? [] : favs);
    }
    if (
      url.pathname.startsWith("/api/favorites/research/") &&
      method === "DELETE"
    ) {
      // removeFavorite deletes by (ref_kind, ref_id) — resolve to the favorite id
      const refId = decodeURIComponent(url.pathname.split("/").pop() ?? "");
      const fav = favs.find((f) => f.ref_kind === "research" && f.ref_id === refId);
      if (fav) favSet.delete(fav.id);
      return jsonResponse({ removed: true });
    }
    if (url.pathname === "/api/events" && method === "GET") {
      // single page for simplicity
      return jsonResponse(empty ? [] : recentEvents);
    }
    if (url.pathname === "/api/tasks") {
      return jsonResponse(empty ? [] : tasks);
    }
    if (url.pathname === "/api/research/queue") {
      return jsonResponse(empty ? [] : researchItems);
    }
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderInsights(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter initialEntries={["/insights"]}>
        <Insights />
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

describe("Insights smoke", () => {
  it("renders favorites list with chart legends, event counts, task success percentages and research trend", async () => {
    installBackend(new Set([1]));
    const page = await renderInsights();

    // fav row renders title, status badge and relative created_at — the mock
    // fav is 2 days old and ago() emits "2天前" (no space); assert inside the
    // row so chart legends elsewhere on the page can't drift the check
    const favRow = Array.from(page.querySelectorAll("tbody tr")).find((tr) =>
      tr.textContent?.includes("测试深度研究"),
    );
    expect(favRow).not.toBeNull();
    expect(favRow!.querySelector(".badge.st-completed")?.textContent).toBe("完成");
    expect(favRow!.textContent).toContain("2天前");
    // events legend & totals
    expect(page.textContent).toContain("research.completed");
    expect(page.textContent).toContain("task.completed");
    // task success bars + codex label
    expect(page.textContent).toContain("codex");
    expect(page.textContent).toContain("50% · 2");
    // research trend total
    expect(page.textContent).toContain("近 30 天完成 1 项");

    // unfavorite action removes row from DOM
    const unfavBtn = Array.from(page.querySelectorAll("button")).find((b) => b.textContent === "取消收藏");
    expect(unfavBtn).not.toBeNull();
    await click(unfavBtn!);
    expect(favSet.size).toBe(0);
    await act(async () => {
      await flushMicrotasks();
    });
    expect(page.textContent).toContain("还没有收藏");
  });

  it("survives empty data: no favorites, no events, no tasks, no research", async () => {
    installBackend(new Set(), true);
    const page = await renderInsights();

    expect(page.textContent).toContain("还没有收藏");
    expect(page.textContent).toContain("近 30 天没有事件");
    expect(page.textContent).toContain("还没有终态任务");
    expect(page.textContent).toContain("近 30 天没有完成的研究");
  });
});
