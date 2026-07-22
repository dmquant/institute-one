import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OperatorParameter, OperatorProposal, OperatorTriage } from "../api";
import { flushMicrotasks, jsonResponse } from "../test-helpers";
import Operator from "./Operator";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

const triage: OperatorTriage = {
  maintenance: { paused: true, drain_depth: 0, queue: { by_status: {}, running_now: 0 } },
  feature_switches: {},
  feature_switches_version: 0,
  hand_weights: { configured: 0, by_scope: {} },
  cron: { window_days: 7, jobs: 24, failing: [] },
  vault: { ledger_total: 0, conflicts: 0 },
  actions: { by_status: {}, open_by_kind: {}, open: 0 },
};

const proposal = (id: number, title: string): OperatorProposal => ({
  id,
  kind: "set_parameter",
  title,
  rationale: "连续观测显示路由质量需要提高门槛",
  params: { key: "operator:confidence_floor", value: 0.8 },
  dedupe_ref: "set_parameter:operator:confidence_floor",
  observation_id: 31,
  recipe_id: null,
  action_id: 90 + id,
  status: "proposed",
  decided_at: null,
  decided_note: null,
  applied: 0,
  created_at: "2026-07-21T08:00:00+08:00",
});

function errorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    statusText: status === 409 ? "Conflict" : "Error",
    headers: { get: (h: string) => (h.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => ({ detail }),
  } as unknown as Response;
}

let root: Root | null = null;
let holder: HTMLDivElement | null = null;
let seen: RequestSeen[] = [];
let proposalGets = 0;
let parameterGets = 0;

function installBackend(conflicts = false): void {
  const proposals = [proposal(7, "提高置信度门槛"), proposal(8, "复核备用提案")];
  const parameter: OperatorParameter = { stored: null, default: 0.7, set: false };

  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://test");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    seen.push({ path: url.pathname, method, body });

    if (url.pathname === "/api/operator/triage") return jsonResponse(triage);
    if (url.pathname === "/api/operator/actions") return jsonResponse({ actions: [], count: 0 });
    if (url.pathname === "/api/operator/proposals" && method === "GET") {
      proposalGets++;
      return jsonResponse({ proposals: proposals.map((p) => ({ ...p })), count: proposals.length });
    }
    if (url.pathname === "/api/operator/parameters" && method === "GET") {
      parameterGets++;
      return jsonResponse({ parameters: { "operator:confidence_floor": { ...parameter } } });
    }

    const decision = url.pathname.match(/^\/api\/operator\/proposals\/(\d+)\/(approve|reject)$/);
    if (decision && method === "POST") {
      if (conflicts) return errorResponse(409, `proposal ${decision[1]} is already decided`);
      const p = proposals.find((item) => item.id === Number(decision[1]));
      if (!p) return errorResponse(404, "unknown proposal");
      p.status = decision[2] === "approve" ? "approved" : "rejected";
      p.applied = decision[2] === "approve" ? 1 : 0;
      p.decided_at = "2026-07-21T09:00:00+08:00";
      p.decided_note = (body as { note?: string }).note ?? "";
      return jsonResponse({ ...p });
    }

    const parameterPrefix = "/api/operator/parameters/";
    if (url.pathname.startsWith(parameterPrefix) && method === "PUT") {
      if (conflicts) return errorResponse(409, "parameter changed concurrently");
      parameter.stored = (body as { value: unknown }).value;
      parameter.set = true;
      return jsonResponse({
        id: 1,
        key: decodeURIComponent(url.pathname.slice(parameterPrefix.length)),
        old_value: null,
        new_value: JSON.stringify(parameter.stored),
        changed_by: "api",
        proposal_id: null,
        rollback_of: null,
        rolled_back_at: null,
        created_at: "2026-07-21T09:00:00+08:00",
      });
    }
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderOperator(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);
  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter>
        <Operator />
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
  proposalGets = 0;
  parameterGets = 0;
  window.localStorage.clear();
  vi.stubGlobal("confirm", vi.fn(() => true));
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

describe("Operator self-improvement controls", () => {
  it("lists proposal provenance and parameters, then drives both human decisions and a parameter PUT", async () => {
    installBackend();
    const page = await renderOperator();

    expect(page.textContent).toContain("提高置信度门槛");
    expect(page.textContent).toContain("operator:confidence_floor");
    expect(page.textContent).toContain("observation_id");

    const approve = page.querySelector('[aria-label="批准并应用提案 #7"]');
    const reject = page.querySelector('[aria-label="拒绝提案 #8"]');
    expect(approve).not.toBeNull();
    expect(reject).not.toBeNull();
    await click(approve!);
    await click(reject!);

    expect(seen).toContainEqual({
      path: "/api/operator/proposals/7/approve",
      method: "POST",
      body: { note: "" },
    });
    expect(seen).toContainEqual({
      path: "/api/operator/proposals/8/reject",
      method: "POST",
      body: { note: "" },
    });
    expect(window.confirm).toHaveBeenCalledTimes(2);

    const parameterInput = page.querySelector(
      '[aria-label="参数 operator:confidence_floor"]',
    ) as HTMLInputElement;
    input(parameterInput, "0.8");
    const save = page.querySelector('[aria-label="保存参数 operator:confidence_floor"]');
    expect(save).not.toBeNull();
    expect((save as HTMLButtonElement).disabled).toBe(false);
    await click(save!);

    expect(seen).toContainEqual({
      path: "/api/operator/parameters/operator%3Aconfidence_floor",
      method: "PUT",
      body: { value: 0.8 },
    });
    expect(page.textContent).toContain("已保存并记录效果基线");
  });

  it("surfaces proposal/parameter 409 conflicts and refreshes their server truth", async () => {
    installBackend(true);
    const page = await renderOperator();

    const proposalGetsBefore = proposalGets;
    await click(page.querySelector('[aria-label="批准并应用提案 #7"]')!);
    expect(page.textContent).toContain("状态冲突");
    expect(page.textContent).toContain("已刷新最新状态");
    expect(proposalGets).toBeGreaterThan(proposalGetsBefore);

    const parameterInput = page.querySelector(
      '[aria-label="参数 operator:confidence_floor"]',
    ) as HTMLInputElement;
    input(parameterInput, "0.8");
    const parameterGetsBefore = parameterGets;
    await click(page.querySelector('[aria-label="保存参数 operator:confidence_floor"]')!);
    expect(page.textContent).toContain("参数已被他处修改");
    expect(page.textContent).toContain("已重新加载");
    expect(parameterGets).toBeGreaterThan(parameterGetsBefore);
    expect(parameterInput.value).toBe("0.7");
  });
});
