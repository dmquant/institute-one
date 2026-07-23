import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BookPosition, BookNavPoint, Forecast } from "../api";
import { flushMicrotasks, jsonResponse } from "../test-helpers";
import Forecasts from "./Forecasts";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface RequestSeen {
  path: string;
  method: string;
  body: unknown;
}

const forecast: Forecast = {
  id: "f-abc123",
  thesis_id: "t-one",
  security_id: null,
  claim: "标普 500 在三个月内上涨 5%",
  direction: "long",
  conviction: 70,
  horizon_days: 90,
  settlement_rule: { type: "pct_change", threshold: 5 },
  made_at: "2026-07-15T08:00:00+08:00",
  expires_at: new Date(Date.now() - 3600 * 1000).toISOString(), // expired 1h ago
  status: "open",
  created_at: "2026-07-15T08:00:00+08:00",
  updated_at: "2026-07-15T08:00:00+08:00",
};

const positions: BookPosition[] = [
  {
    id: "p-1",
    security_id: "SPX",
    direction: "long",
    entry_price: 4000,
    realized_pnl: 123.45,
    status: "closed",
    close_reason: "target",
  },
];

const navPoints: BookNavPoint[] = [
  { work_date: "2026-07-20", nav: 1.01, benchmark_nav: 1.0 },
  { work_date: "2026-07-21", nav: 1.02, benchmark_nav: 1.01 },
];

let root: Root | null = null;
let holder: HTMLDivElement | null = null;
let seen: RequestSeen[] = [];

function installBackend(favoritedIds: Set<string>, empty = false): void {
  const favoritedSet = favoritedIds;
  let settled = false; // flips after POST settle so later GETs reflect it

  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://test");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    seen.push({ path: url.pathname, method, body });

    if (url.pathname === "/api/forecasts" && method === "GET") {
      return jsonResponse(empty ? [] : [settled ? { ...forecast, status: "settled" } : forecast]);
    }
    if (url.pathname === "/api/favorites" && method === "GET") {
      return jsonResponse(
        (empty ? [] : ["f-fav1"]).map((id) => ({
          id: 1,
          ref_kind: "forecast",
          ref_id: id,
          note: "",
          created_at: "2026-07-20T08:00:00+08:00",
          title: "测试预测收藏",
          status: "open",
        })),
      );
    }
    if (url.pathname.startsWith("/api/book/positions")) {
      return jsonResponse(empty ? [] : positions);
    }
    if (url.pathname.startsWith("/api/book/nav")) {
      return jsonResponse(empty ? [] : navPoints);
    }
    if (
      url.pathname.match(/^\/api\/favorites\/forecast\/([^/?]+)$/) &&
      method === "DELETE"
    ) {
      const id = decodeURIComponent(url.pathname.split("/").pop() ?? "");
      if (favoritedSet.has(id)) favoritedSet.delete(id);
      return jsonResponse({ removed: true });
    }
    if (
      url.pathname === "/api/favorites" &&
      method === "POST"
    ) {
      const payload = body as { ref_kind: string; ref_id: string; note: string };
      favoritedSet.add(payload.ref_id);
      return jsonResponse({
        id: 2,
        ref_kind: payload.ref_kind,
        ref_id: payload.ref_id,
        note: payload.note,
        created_at: new Date().toISOString(),
        title: "新收藏",
        status: null,
      });
    }
    if (
      url.pathname === `/api/forecasts/${encodeURIComponent(forecast.id)}/settle` &&
      method === "POST"
    ) {
      settled = true;
      return jsonResponse({ ...forecast, status: "settled" });
    }
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

async function renderForecasts(): Promise<HTMLDivElement> {
  holder = document.createElement("div");
  document.body.appendChild(holder);

  root = createRoot(holder);
  act(() => {
    root!.render(
      <MemoryRouter initialEntries={["/forecasts"]}>
        <Forecasts />
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

describe("Forecasts smoke", () => {
  it("renders forecasts list with settle button, book positions and NAV, then settles and toggles favorites", async () => {
    installBackend(new Set(["f-fav1"]));
    const page = await renderForecasts();

    // forecast row renders id + zh direction label (DIRECTION_ZH.long = 看多) —
    // scope to the row element, not a page-wide "看多 long" concatenation
    const forecastRow = page.querySelector("#forecast-f-abc123");
    expect(forecastRow).not.toBeNull();
    expect(forecastRow!.textContent).toContain("f-abc123");
    expect(forecastRow!.textContent).toContain("看多");
    // expired open forecast shows settlement control
    expect(page.textContent).toContain("可结算");
    expect(buttonByText(page, "结算")).not.toBeNull();
    // fav star reflects favoriteIds: the mocked favorites hold f-fav1, so the
    // listed forecast f-abc123 shows the hollow "add favorite" state
    const starBtn = forecastRow!.querySelector('button[aria-label="收藏预测"]');
    expect(starBtn?.textContent).toBe("☆");
    // positions row: realized pnl (toFixed(4)) + zh close_reason label
    const posRow = Array.from(page.querySelectorAll("tbody tr")).find((tr) =>
      tr.textContent?.includes("SPX"),
    );
    expect(posRow).not.toBeNull();
    expect(posRow!.textContent).toContain("123.4500");
    expect(posRow!.textContent).toContain("止盈");

    // settle the expired forecast
    const settleBtn = buttonByText(page, "结算");
    expect(settleBtn).not.toBeNull();
    await click(settleBtn!);
    // settleForecast POSTs without a body — the mock records body: null
    expect(seen).toContainEqual({
      path: "/api/forecasts/f-abc123/settle",
      method: "POST",
      body: null,
    });
    // forecast row now shows the settled status (STATUS_ZH has no "settled"
    // entry, so the badge falls back to the raw status string)
    const settledBadge = page.querySelector("#forecast-f-abc123 .badge.st-settled");
    expect(settledBadge?.textContent).toBe("settled");
  });

  it("survives empty data: no forecasts, no positions, insufficient nav", async () => {
    installBackend(new Set(), true);
    const page = await renderForecasts();

    expect(page.textContent).toContain("还没有预测记录");
    expect(page.textContent).toContain("没有持仓记录");
    expect(page.textContent).toContain("净值数据不足（≥2 个点才能画线）");
  });
});
