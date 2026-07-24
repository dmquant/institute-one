import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Meta, OperatorTriage, Task } from "../api";
import { FakeStream, flushMicrotasks, jsonResponse, streamResponse } from "../test-helpers";
import Dashboard from "./Dashboard";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const meta: Meta = {
  version: "0.1.0",
  timezone: "Asia/Singapore",
  work_date: "2026-07-22",
  hands: [
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
  ],
  vault_configured: true,
  queue: { by_status: { completed: 12, failed: 1 }, running_now: 2 },
  limits: { max_concurrent: 3, default_timeout_s: 1800, output_cap_bytes: 262144 },
};

const triage: OperatorTriage = {
  maintenance: { paused: true, drain_depth: 0, queue: { by_status: {}, running_now: 0 } },
  feature_switches: {},
  feature_switches_version: 0,
  hand_weights: { configured: 0, by_scope: {} },
  cron: { window_days: 7, jobs: 24, failing: [] },
  vault: { ledger_total: 0, conflicts: 0 },
  actions: { by_status: { open: 3 }, open_by_kind: {}, open: 3 },
};

const task = {
  id: "t-abc123",
  status: "completed",
  hand: "codex",
  requested_hand: "codex",
  source: "whiteboard",
  created_at: new Date().toISOString(),
} as unknown as Task;

let root: Root | null = null;
let holder: HTMLDivElement | null = null;

function installBackend(): void {
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://test");
    if (url.pathname === "/api/events/stream") {
      return streamResponse(new FakeStream(init?.signal)); // stays open: live feed idles
    }
    if (url.pathname === "/api/events") return jsonResponse([]); // short page = log tail
    if (url.pathname === "/api/meta") return jsonResponse(meta);
    if (url.pathname === "/api/tasks") return jsonResponse([task]);
    if (url.pathname === "/api/admin/state") {
      return jsonResponse({ maintenance: JSON.stringify({ paused: true }) });
    }
    if (url.pathname === "/api/operator/triage") return jsonResponse(triage);
    if (url.pathname === "/api/forecasts/stats") {
      return jsonResponse({ hits: 0, misses: 0, partial: 0, settled: 0 });
    }
    if (url.pathname === "/api/vectors/health") {
      return jsonResponse({
        enabled: false,
        reason: "vectors_disabled",
        extension_available: false,
        ollama_reachable: null,
        model_available: null,
        current_model: "bge-m3",
        chunk_counts: {},
      });
    }
    throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url.pathname}`);
  });
}

async function renderDashboard(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
  });
  await act(async () => {
    await flushMicrotasks();
  });
  return holder;
}

beforeEach(() => {
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

describe("Dashboard smoke", () => {
  it("renders queue stats, hands, recent tasks, operator signals and the maintenance banner", async () => {
    installBackend();
    const page = await renderDashboard();

    // maintenance banner comes from admin_state JSON, not from a silent default
    expect(page.textContent).toContain("维护模式已开启");

    // queue: running_now + by_status boxes from /api/meta
    expect(page.textContent).toContain("本进程在跑");
    expect(page.textContent).toContain("12");

    // hands chip and recent-task row
    expect(page.textContent).toContain("codex");
    expect(page.textContent).toContain("t-abc123");

    // operator signals: open actions count links to /operator
    expect(page.textContent).toContain("待处置事项");
    expect(page.querySelector('a[href="/operator"]')?.textContent).toContain("3");

    // vector health degrades loudly instead of pretending to be healthy
    expect(page.textContent).toContain("向量检索");
    expect(page.textContent).toContain("未启用");
  });
});
