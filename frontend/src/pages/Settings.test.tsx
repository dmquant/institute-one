import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CronHealth, CronJobHealth, Meta, VaultStatus } from "../api";
import { FakeStream, flushMicrotasks, jsonResponse, streamResponse } from "../test-helpers";
import Settings from "./Settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

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
      fallback_chain: ["claude"],
    },
  ],
  vault_configured: true,
  queue: { by_status: { completed: 4 }, running_now: 1 },
  limits: { max_concurrent: 3, default_timeout_s: 1800, output_cap_bytes: 262144 },
};

const emptyMeta: Meta = {
  ...meta,
  hands: [],
  queue: { by_status: {}, running_now: 0 },
};

function cronJob(gated: boolean): CronJobHealth {
  return {
    registered: true,
    gated,
    schedule: "0 8 * * *",
    next_run_time: null,
    last_fired_at: null,
    last_status: null,
    fires: 0,
    ok: 0,
    failed: 0,
    skipped: 0,
    ok_rate: null,
    avg_duration_ms: null,
    last_error: null,
  };
}

const cron: CronHealth = {
  window_days: 7,
  jobs: { daily_briefing: cronJob(true), events_retention: cronJob(false) },
};

const vault: VaultStatus = {
  configured: true,
  vault_dir: "/tmp/vault",
  counts: { clean: 5, conflict: 0 },
  total: 5,
};

let root: Root | null = null;
let holder: HTMLDivElement | null = null;
let seen: RequestSeen[] = [];

function installBackend(opts: { paused: boolean; empty?: boolean }): void {
  const state = { paused: opts.paused };
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://test");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    seen.push({ path: url.pathname, method, body });

    if (url.pathname === "/api/events/stream") {
      return streamResponse(new FakeStream(init?.signal)); // BilingualCard's live feed idles
    }
    if (url.pathname === "/api/events") return jsonResponse([]); // no twin_ready history
    if (url.pathname === "/api/meta") return jsonResponse(opts.empty ? emptyMeta : meta);
    if (url.pathname === "/api/vault/status") {
      return jsonResponse(
        opts.empty ? { configured: false, vault_dir: null, counts: {}, total: 0 } : vault,
      );
    }
    if (url.pathname === "/api/admin/state") {
      return jsonResponse({ maintenance: JSON.stringify({ paused: state.paused }) });
    }
    if (url.pathname === "/api/admin/maintenance" && method === "POST") {
      state.paused = Boolean((body as { paused: boolean }).paused);
      return jsonResponse({ paused: state.paused });
    }
    if (url.pathname === "/api/cron/health") {
      return jsonResponse(opts.empty ? { window_days: 7, jobs: {} } : cron);
    }
    if (url.pathname === "/api/bilingual/preference") return jsonResponse({ locale: "zh" });
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderSettings(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter>
        <Settings />
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

function buttonByText(page: HTMLElement, text: string): HTMLButtonElement | null {
  return (
    Array.from(page.querySelectorAll("button")).find((b) => b.textContent === text) ?? null
  );
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

describe("Settings smoke", () => {
  it("renders maintenance state, hands, gated jobs, vault and system info, then toggles maintenance", async () => {
    installBackend({ paused: true });
    const page = await renderSettings();

    // maintenance card reflects admin_state JSON
    expect(page.textContent).toContain("已暂停");
    // gate lists come from /api/cron/health registry flags
    expect(page.textContent).toContain("daily_briefing");
    expect(page.textContent).toContain("events_retention");
    // hands table: name, fallback chain and availability
    expect(page.textContent).toContain("codex");
    expect(page.textContent).toContain("claude");
    expect(page.textContent).toContain("可用");
    // vault + system info from their endpoints
    expect(page.textContent).toContain("/tmp/vault");
    expect(page.textContent).toContain("v0.1.0");
    expect(page.textContent).toContain("Asia/Singapore");

    // toggle: paused -> resume posts {paused: false} and refreshes admin state
    const resume = buttonByText(page, "恢复运行");
    expect(resume).not.toBeNull();
    await click(resume!);
    expect(seen).toContainEqual({
      path: "/api/admin/maintenance",
      method: "POST",
      body: { paused: false },
    });
    expect(page.textContent).toContain("维护模式已关闭：定时任务恢复");
    expect(page.textContent).toContain("正常运行");
  });

  it("survives empty data: no hands, no tasks, unconfigured vault, no twins", async () => {
    installBackend({ paused: false, empty: true });
    const page = await renderSettings();

    expect(page.textContent).toContain("正常运行");
    expect(page.textContent).toContain("没有注册的执行手");
    expect(page.textContent).toContain("还没有任务");
    expect(page.textContent).toContain("否（未设置 vault_dir）");
    expect(page.textContent).toContain("还没有英文孪生产出");
  });
});
