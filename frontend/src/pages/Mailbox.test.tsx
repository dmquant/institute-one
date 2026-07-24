import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Analyst, MailThread, MailThreadDetail } from "../api";
import { FakeStream, flushMicrotasks, jsonResponse, streamResponse } from "../test-helpers";
import Mailbox from "./Mailbox";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

const analyst: Analyst = {
  id: "macro",
  name: "宏观",
  name_en: "Macro",
  category: "macro",
  emoji: "🧭",
  focus: "宏观经济",
  persona: "",
  hand: null,
  model: null,
};

const threads: MailThread[] = [
  {
    id: "t-1",
    subject: "美债利率追问",
    analyst_id: "macro",
    status: "open",
    created_at: "2026-07-22T08:00:00+00:00",
    updated_at: "2026-07-22T09:00:00+00:00",
    n_messages: 2,
  },
  {
    id: "t-2",
    subject: "已答复的线程",
    analyst_id: "macro",
    status: "closed",
    created_at: "2026-07-21T08:00:00+00:00",
    updated_at: "2026-07-21T09:00:00+00:00",
    n_messages: 3,
  },
];

// t-1: the last conversational (non-dispatch) message is the operator's —
// the list must flag it 未回复; t-2 ends on the analyst's reply.
const details: Record<string, MailThreadDetail> = {
  "t-1": {
    ...threads[0],
    messages: [
      { id: 1, thread_id: "t-1", author: "operator", kind: "note", body: "请更新看法", task_id: null, status: "", created_at: "2026-07-22T08:00:00+00:00" },
      { id: 2, thread_id: "t-1", author: "macro", kind: "dispatch", body: "", task_id: "task-9", status: "pending", created_at: "2026-07-22T08:01:00+00:00" },
    ],
  },
  "t-2": {
    ...threads[1],
    messages: [
      { id: 3, thread_id: "t-2", author: "operator", kind: "note", body: "问题", task_id: null, status: "", created_at: "2026-07-21T08:00:00+00:00" },
      { id: 4, thread_id: "t-2", author: "macro", kind: "reply", body: "回答", task_id: null, status: "", created_at: "2026-07-21T09:00:00+00:00" },
    ],
  },
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

    if (url.pathname === "/api/events/stream") {
      return streamResponse(new FakeStream(init?.signal)); // stays open: wake-up channel idles
    }
    if (url.pathname === "/api/events") return jsonResponse([]); // short page = log tail
    if (url.pathname === "/api/analysts") return jsonResponse([analyst]);
    if (url.pathname === "/api/mailbox/threads" && method === "GET") {
      return jsonResponse(empty ? [] : threads);
    }
    if (url.pathname === "/api/mailbox/threads" && method === "POST") {
      return jsonResponse({ ...details["t-1"], id: "t-new" });
    }
    const detail = /^\/api\/mailbox\/threads\/(.+)$/.exec(url.pathname);
    if (detail && details[detail[1]]) return jsonResponse(details[detail[1]]);
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderMailbox(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter>
        <Mailbox />
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

function setValue(element: HTMLElement, value: string): void {
  const proto = Object.getPrototypeOf(element) as object;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  const eventType = element instanceof HTMLSelectElement ? "change" : "input";
  act(() => {
    setter?.call(element, value);
    element.dispatchEvent(new Event(eventType, { bubbles: true }));
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

describe("Mailbox smoke", () => {
  it("lists threads with the unanswered badge only where the operator spoke last", async () => {
    installBackend();
    const page = await renderMailbox();

    expect(page.textContent).toContain("美债利率追问");
    expect(page.textContent).toContain("已答复的线程");
    expect(page.textContent).toContain("🧭 宏观"); // analyst id resolved via roster

    // exactly ONE unanswered badge, and it sits in t-1's row
    const badges = page.querySelectorAll('span[title="最后一条对话消息来自操作员"]');
    expect(badges.length).toBe(1);
    expect(badges[0].closest("tr")?.textContent).toContain("美债利率追问");
  });

  it("creates a thread once subject/analyst/body are filled", async () => {
    installBackend();
    const page = await renderMailbox();

    const send = Array.from(page.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("发送并派发"),
    ) as HTMLButtonElement;
    expect(send.disabled).toBe(true); // nothing filled yet

    setValue(page.querySelector("input")!, "新问题");
    setValue(page.querySelectorAll("select")[0] as HTMLSelectElement, "macro");
    setValue(page.querySelector("textarea")!, "请分析");
    expect(send.disabled).toBe(false);
    await click(send);

    expect(seen).toContainEqual({
      path: "/api/mailbox/threads",
      method: "POST",
      body: { subject: "新问题", analyst_id: "macro", body: "请分析" },
    });
  });

  it("survives an empty mailbox", async () => {
    installBackend(true);
    const page = await renderMailbox();
    expect(page.textContent).toContain("还没有线程");
  });
});
