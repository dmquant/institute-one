import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Analyst, MultiAgentCompletedRun, MultiAgentOutput } from "../api";
import { flushMicrotasks, jsonResponse } from "../test-helpers";
import MultiAgent from "./MultiAgent";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

const analysts: Analyst[] = [
  {
    id: "macro",
    name: "宏观分析师",
    name_en: "Macro Analyst",
    category: "macro",
    emoji: "📈",
    focus: "全球宏观与流动性",
    persona: "资深宏观经济学家...",
    hand: "codex",
    model: null,
  },
  {
    id: "quant",
    name: "量化分析师",
    name_en: "Quant Analyst",
    category: "quant",
    emoji: "🔬",
    focus: "统计套利与多因子",
    persona: "量化研究员，擅长...",
    hand: "claude",
    model: null,
  },
];

function completedRun(outputs: MultiAgentOutput[]): MultiAgentCompletedRun {
  return {
    run_id: "run-abc123",
    mode: "all",
    ok: true,
    output: "",
    outputs: outputs,
  };
}

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
    if (
      url.pathname === "/api/multi-agent/run" &&
      method === "POST"
    ) {
      const payload = body as { agents: string[]; prompt: string; mode: string };
      return jsonResponse(
        completedRun([
          {
            agent: payload.agents[0],
            hand: analysts.find((a) => a.id === payload.agents[0])?.hand ?? "",
            status: "completed",
            output: `${payload.agents[0]} 的输出`,
            error: null,
            task_id: "t-run-1",
          },
        ]),
      );
    }
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderMultiAgent(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter initialEntries={["/multi-agent"]}>
        <MultiAgent />
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

function textareaInput(textarea: HTMLTextAreaElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  act(() => {
    setter?.call(textarea, value);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
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

describe("Multi-agent smoke", () => {
  it("renders analyst list, allows selection + prompt entry, then runs comparison", async () => {
    installBackend();
    const page = await renderMultiAgent();

    // both analyst buttons present
    expect(page.textContent).toContain("📈 宏观分析师");
    expect(page.textContent).toContain("🔬 量化分析师");

    // run button disabled until two selections made (min count in page is 2)
    expect(buttonByText(page, "运行中...")).toBeNull();
    const runBtn = Array.from(page.querySelectorAll("button")).find((b) =>
      b.textContent?.startsWith("运行（"),
    );
    expect(runBtn).not.toBeNull();
    expect((runBtn as HTMLButtonElement).disabled).toBe(true);

    // toggle both analysts
    await click(Array.from(page.querySelectorAll("button")).find((b) => b.textContent?.includes("宏观"))!);
    await click(Array.from(page.querySelectorAll("button")).find((b) => b.textContent?.includes("量化"))!);

    const textarea = page.querySelector("textarea") as HTMLTextAreaElement;
    textareaInput(textarea, "测试多智能体对比功能");

    // run button enabled
    expect((runBtn as HTMLButtonElement).disabled).toBe(false);
    await click(runBtn!);

    // POST request sent with selected agents, prompt, mode
    expect(seen).toContainEqual({
      path: "/api/multi-agent/run",
      method: "POST",
      body: { agents: ["macro", "quant"], prompt: "测试多智能体对比功能", mode: "all" },
    });

    // result panel shows the completed run: status badge, run id and the
    // mock's single output card
    expect(page.querySelector(".badge.st-completed")?.textContent).toBe("完成");
    expect(page.textContent).toContain("run: run-abc123");
    expect(page.textContent).toContain("macro 的输出");
    // both analysts stayed selected after the run — the run button reads 2
    const runBtnAfter = Array.from(page.querySelectorAll("button")).find((b) =>
      b.textContent?.startsWith("运行（"),
    );
    expect(runBtnAfter?.textContent).toBe("运行（2 个）");
  });

  it("survives empty data: no analysts available", async () => {
    installBackend(true);
    const page = await renderMultiAgent();

    expect(page.textContent).toContain("没有分析师");
    // run button stays disabled
    expect(page.textContent).toContain("运行（0 个）");
  });
});
