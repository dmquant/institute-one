import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Analyst, AnalystDailyStatus } from "../api";
import { flushMicrotasks, jsonResponse } from "../test-helpers";
import Analysts from "./Analysts";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

const analysts: Analyst[] = [
  {
    id: "macro-lin",
    name: "林宏",
    name_en: "Lin Hong",
    category: "macro",
    emoji: "📈",
    focus: "全球宏观与流动性",
    persona: "资深宏观经济学家，擅长...",
    hand: "codex",
    model: null,
  },
];

const analystRoles = {
  roles: ["macro", "quant", "ops"],
  in_use: ["macro"],
};

const dailyStatus: AnalystDailyStatus = {
  date: new Date().toISOString().slice(0, 10),
  analysts: { "macro-lin": "pending" },
  session_id: "run-abc123",
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

    if (url.pathname === "/api/analysts" && method === "GET") {
      return jsonResponse(empty ? [] : analysts);
    }
    if (url.pathname === "/api/analysts/roles") {
      return jsonResponse(analystRoles);
    }
    if (url.pathname === "/api/analysts/daily/status") {
      return jsonResponse(dailyStatus);
    }
    if (
      url.pathname.startsWith("/api/analysts/") &&
      method === "POST" &&
      !url.pathname.endsWith("/daily/run")
    ) {
      // update/create analyst
      return jsonResponse({
        ...analysts[0],
        name: (body as { name: string })?.name ?? analysts[0].name,
      });
    }
    if (
      url.pathname.startsWith("/api/analysts/") &&
      method === "DELETE"
    ) {
      return jsonResponse({ deleted: analysts[0].id });
    }
    if (
      url.pathname === "/api/analysts/daily/run-now" ||
      url.pathname.includes("/daily/run")
    ) {
      return jsonResponse({ started: "run-id-test" });
    }
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderAnalysts(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter initialEntries={["/analysts"]}>
        <Analysts />
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
  return Array.from(page.querySelectorAll("button")).find((b) => b.textContent === text) ?? null;
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

describe("Analysts smoke", () => {
  it("renders analyst cards with role badges, shows running daily button and notification after run", async () => {
    installBackend();
    const page = await renderAnalysts();

    // card renders name, focus, role badge — zh/en names live in sibling divs
    // (.names .zh / .names .en), so assert the elements instead of a page-wide
    // concatenated "林宏 Lin Hong · macro-lin" string
    const card = page.querySelector(".analyst-card");
    expect(card).not.toBeNull();
    expect(card!.querySelector(".names .zh")?.textContent).toBe("林宏");
    expect(card!.querySelector(".names .en")?.textContent).toBe("Lin Hong · macro-lin");
    expect(page.textContent).toContain("📈");
    expect(page.textContent).toContain("全球宏观与流动性");
    expect(page.textContent).toContain("macro");

    // "运行日报" button present; the mocked pending daily state adds no
    // ✓/✗ suffix (only completed/failed do) — exact-match lookup proves it
    const dailyBtn = buttonByText(page, "运行日报");
    expect(dailyBtn).not.toBeNull();
    expect(dailyBtn!.textContent).toBe("运行日报");

    // click run -> POST /api/analysts/{id}/daily/run
    const runBtn = buttonByText(page, "运行日报");
    expect(runBtn).not.toBeNull();
    await click(runBtn!);

    // runAnalystDaily POSTs without a body — the mock records body: null
    expect(seen).toContainEqual({
      path: "/api/analysts/macro-lin/daily/run",
      method: "POST",
      body: null,
    });

    // OK note appears with analyst name
    expect(page.textContent).toContain("已启动");
    expect(page.textContent).toContain("林宏");
  });

  it("survives empty data: no analysts available", async () => {
    installBackend(true);
    const page = await renderAnalysts();

    expect(page.textContent).toContain("还没有分析师");
    expect(buttonByText(page, "新增分析师")).not.toBeNull();
  });
});
