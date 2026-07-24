import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskRow } from "../api";
import { flushMicrotasks, jsonResponse } from "../test-helpers";
import Tasks from "./Tasks";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

const now = Date.now();
const tasks: TaskRow[] = [
  {
    id: "t-abc123",
    session_id: null,
    hand: "codex",
    requested_hand: "codex",
    model: null,
    status: "completed",
    source: "whiteboard",
    exit_code: 0,
    error: null,
    parent_run_id: null,
    created_at: new Date(now - 3600 * 1000).toISOString(),
    started_at: new Date(now - 3600 * 1000).toISOString(),
    finished_at: new Date(now - 3600 * 1000).toISOString(),
  },
  {
    id: "t-def456",
    session_id: null,
    hand: "claude",
    requested_hand: "claude",
    model: null,
    status: "failed",
    source: "api",
    exit_code: 1,
    error: "test error message",
    parent_run_id: null,
    created_at: new Date(now - 7200 * 1000).toISOString(),
    started_at: new Date(now - 7200 * 1000).toISOString(),
    finished_at: new Date(now - 7200 * 1000).toISOString(),
  },
];

let root: Root | null = null;
let holder: HTMLDivElement | null = null;
let seen: RequestSeen[] = [];

function installBackend(empty = false): void {
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://test");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    seen.push({ path: url.pathname, method, body });

    if (url.pathname === "/api/events/stream") {
      return new Response(null, { status: 200 });
    }
    if (url.pathname === "/api/events") {
      return jsonResponse([]);
    }
    if (url.pathname === "/api/tasks" && method === "GET") {
      return jsonResponse(empty ? [] : tasks);
    }
    if (url.pathname.startsWith("/api/tasks/") && !url.pathname.endsWith("/cancel")) {
      const id = decodeURIComponent(url.pathname.split("/").pop()!);
      const task = tasks.find((t) => t.id === id);
      if (!task) {
        return jsonResponse({ detail: "not found" });
      }
      return jsonResponse(task);
    }
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderTasks(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <Tasks />
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

describe("Tasks smoke", () => {
  it("renders task rows, filter buttons, shows error text and expands errors on click", async () => {
    installBackend();
    const page = await renderTasks();

    // task rows display id/status/hand/source/error — assert INSIDE each row:
    // StatusBadge renders only the zh label (the raw status sits on the badge
    // class and title), so a page-wide "完成 completed" string no longer exists
    const rowOf = (id: string): HTMLTableRowElement => {
      const tr = Array.from(page.querySelectorAll("tbody tr")).find((el) =>
        el.textContent?.includes(id),
      );
      expect(tr).not.toBeNull();
      return tr as HTMLTableRowElement;
    };
    const row1 = rowOf("t-abc123");
    const badge1 = row1.querySelector(".badge.st-completed");
    expect(badge1?.textContent).toBe("完成");
    expect(badge1?.getAttribute("title")).toBe("completed");
    expect(row1.textContent).toContain("codex");
    expect(row1.textContent).toContain("whiteboard");

    const row2 = rowOf("t-def456");
    expect(row2.querySelector(".badge.st-failed")?.textContent).toBe("失败");
    expect(row2.textContent).toContain("claude");
    expect(row2.textContent).toContain("test error message");

    // filter buttons exist for each known status + sources dropdown
    expect(page.textContent).toContain("全部");
    expect(page.textContent).toContain("完成");
    expect(page.textContent).toContain("源");

    // task count shown
    expect(page.textContent).toContain("2 条");

    // clicking a row opens drawer -> GET /api/tasks/{id}
    await click(rowOf("t-abc123"));
    expect(seen.length).toBeGreaterThan(0);
    expect(seen.some((req) => req.path.includes("/api/tasks/t-abc123"))).toBe(true);
  });

  it("survives empty data: no tasks found for filters", async () => {
    installBackend(true);
    const page = await renderTasks();

    expect(page.textContent).toContain("没有符合条件的任务");
  });
});
