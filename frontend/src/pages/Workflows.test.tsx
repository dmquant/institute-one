import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Workflow, WorkflowRun } from "../api";
import { FakeStream, flushMicrotasks, jsonResponse, streamResponse } from "../test-helpers";
import Workflows from "./Workflows";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

const workflows: Workflow[] = [
  {
    id: "briefing",
    name: "晨会简报",
    description: "每天早上的市场简报",
    variables: ["WORK_DATE"],
    steps: [{ id: "s1", title: "汇总" }, { id: "s2", title: "编写" }],
    updated_at: "2026-07-22T08:00:00+00:00",
  },
  {
    id: "committee",
    name: "周度委员会",
    description: "",
    variables: ["WORK_DATE", "DATA_BUNDLE", "WEEK_DISPUTES"],
    steps: [{ id: "s1", title: "评议" }],
    updated_at: "2026-07-22T08:00:00+00:00",
  },
];

const runs: WorkflowRun[] = [
  {
    id: "r-123",
    workflow_id: "briefing",
    session_id: null,
    status: "completed",
    variables: {},
    current_step: 2,
    results: [],
    error: null,
    source: "scheduler",
    started_at: "2026-07-22T00:30:00+00:00",
    finished_at: "2026-07-22T00:40:00+00:00",
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
      return streamResponse(new FakeStream(init?.signal)); // stays open: wake-up channel idles
    }
    if (url.pathname === "/api/events") return jsonResponse([]); // short page = log tail
    if (url.pathname === "/api/workflows" && method === "GET") {
      return jsonResponse(empty ? [] : workflows);
    }
    if (url.pathname === "/api/workflows/runs/recent") {
      return jsonResponse(empty ? [] : runs);
    }
    if (url.pathname === "/api/workflows/briefing/run" && method === "POST") {
      return jsonResponse({ run_id: "r-new" });
    }
    if (url.pathname === "/api/workflows/committee/run" && method === "POST") {
      return jsonResponse({ run_id: "r-new2" });
    }
    if (url.pathname === "/api/workflows/daily/briefing/run-now" && method === "POST") {
      return jsonResponse({ run_id: null, skipped: true });
    }
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderWorkflows(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter>
        <Workflows />
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

describe("Workflows smoke", () => {
  it("renders workflow cards and recent runs, and starts a run", async () => {
    installBackend();
    const page = await renderWorkflows();

    // card: name, step chain, variable input; runs table: id link + status
    expect(page.textContent).toContain("晨会简报");
    expect(page.textContent).toContain("2 个步骤：汇总 → 编写");
    expect(page.querySelector('input[placeholder="默认今天"]')).not.toBeNull();
    expect(page.querySelector('a[href="/workflows/runs/r-123"]')).not.toBeNull();
    expect(page.textContent).toContain("完成"); // StatusBadge for the completed run

    const run = Array.from(page.querySelectorAll("button")).find(
      (b) => b.textContent === "立即运行",
    );
    expect(run).not.toBeUndefined();
    await click(run!);
    // no variables typed -> the body carries an explicit null
    expect(seen).toContainEqual({
      path: "/api/workflows/briefing/run",
      method: "POST",
      body: { variables: null },
    });
  });

  it("surfaces the run-now skip note when today's briefing already exists", async () => {
    installBackend();
    const page = await renderWorkflows();

    const briefing = Array.from(page.querySelectorAll("button")).find(
      (b) => b.textContent === "立即生成简报",
    );
    await click(briefing!);

    expect(seen.map((r) => `${r.method} ${r.path}`)).toContain(
      "POST /api/workflows/daily/briefing/run-now",
    );
    expect(page.textContent).toContain("晨间简报：今天已生成，跳过");
  });

  it("hides lazy variables and treats empty inputs as not provided", async () => {
    installBackend();
    const page = await renderWorkflows();

    // the committee card declares DATA_BUNDLE/WEEK_DISPUTES, which the backend
    // computes lazily — they must not render as inputs (an submitted "" would
    // suppress the lazy computation); WORK_DATE stays visible
    const committeeCard = Array.from(page.querySelectorAll(".card")).find((c) =>
      c.textContent?.includes("周度委员会"),
    );
    expect(committeeCard).not.toBeUndefined();
    const labels = Array.from(committeeCard!.querySelectorAll(".lbl")).map((el) => el.textContent);
    expect(labels).toEqual(["WORK_DATE"]);

    const inputEl = committeeCard!.querySelector("input")!;
    const runBtn = Array.from(committeeCard!.querySelectorAll("button")).find(
      (b) => b.textContent === "立即运行",
    )!;

    // a typed value is submitted as-is
    input(inputEl, "2026-07-23");
    await click(runBtn);
    expect(seen).toContainEqual({
      path: "/api/workflows/committee/run",
      method: "POST",
      body: { variables: { WORK_DATE: "2026-07-23" } },
    });

    // cleared back to "" → not provided: the body carries an explicit null,
    // letting the server apply its own defaults
    input(inputEl, "");
    await click(runBtn);
    expect(seen).toContainEqual({
      path: "/api/workflows/committee/run",
      method: "POST",
      body: { variables: null },
    });
  });

  it("survives empty registries", async () => {
    installBackend(true);
    const page = await renderWorkflows();
    expect(page.textContent).toContain("没有已注册的工作流");
    expect(page.textContent).toContain("还没有运行记录");
  });
});
