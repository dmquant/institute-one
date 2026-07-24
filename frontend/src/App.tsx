import { lazy, Suspense, useRef } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { getMeta } from "./api";
import { useSSE } from "./useSSE";
import { Loading, useLoad } from "./ui";
import Dashboard from "./pages/Dashboard";

// Route-level code splitting: the Dashboard landing page stays in the main
// bundle; every other page is lazy-loaded behind the <Suspense> boundary
// that wraps <Routes> below.
const Tasks = lazy(() => import("./pages/Tasks"));
const Ask = lazy(() => import("./pages/Ask"));
const Workflows = lazy(() => import("./pages/Workflows"));
const RunDetail = lazy(() => import("./pages/RunDetail"));
const Whiteboard = lazy(() => import("./pages/Whiteboard"));
const BoardDetail = lazy(() => import("./pages/BoardDetail"));
const Mailbox = lazy(() => import("./pages/Mailbox"));
const ThreadDetail = lazy(() => import("./pages/ThreadDetail"));
const Research = lazy(() => import("./pages/Research"));
const Trees = lazy(() => import("./pages/Trees"));
const Projects = lazy(() => import("./pages/Projects"));
const Forecasts = lazy(() => import("./pages/Forecasts"));
const MultiAgent = lazy(() => import("./pages/MultiAgent"));
const Sessions = lazy(() => import("./pages/Sessions"));
const Analysts = lazy(() => import("./pages/Analysts"));
const Hands = lazy(() => import("./pages/Hands"));
const CronHealth = lazy(() => import("./pages/CronHealth"));
const Operator = lazy(() => import("./pages/Operator"));
const Insights = lazy(() => import("./pages/Insights"));
const Settings = lazy(() => import("./pages/Settings"));

const NAV: { to: string; zh: string; en: string }[] = [
  { to: "/", zh: "总览", en: "Dashboard" },
  { to: "/tasks", zh: "任务", en: "Tasks" },
  { to: "/ask", zh: "即问", en: "Ask" },
  { to: "/workflows", zh: "工作流", en: "Workflows" },
  { to: "/whiteboard", zh: "白板", en: "Whiteboard" },
  { to: "/mailbox", zh: "信箱", en: "Mailbox" },
  { to: "/research", zh: "深度研究", en: "Research" },
  { to: "/trees", zh: "研究树", en: "Trees" },
  { to: "/projects", zh: "项目", en: "Projects" },
  { to: "/forecasts", zh: "预测账本", en: "Forecasts" },
  { to: "/multi-agent", zh: "多智能体", en: "Multi-agent" },
  { to: "/sessions", zh: "会话", en: "Sessions" },
  { to: "/analysts", zh: "分析师", en: "Analysts" },
  { to: "/hands", zh: "执行手", en: "Hands" },
  { to: "/cron", zh: "定时任务", en: "Cron" },
  { to: "/operator", zh: "运维", en: "Operator" },
  { to: "/insights", zh: "洞察", en: "Insights" },
  { to: "/settings", zh: "设置", en: "Settings" },
];

const META_POLL_MS = 30_000;
// Event-driven meta refreshes are throttled: on a busy bus, keying the fetch
// off every event id would refetch GET /api/meta (and re-render) nonstop.
const META_EVENT_MIN_GAP_MS = 5_000;

/** Header strip. Owns the SSE subscription + meta fetch so bus events only
 * re-render the topbar — not the whole <Routes> tree below it. */
function Topbar() {
  // mount time ≈ the initial fetch, so the first events don't double-fetch
  const lastFetchAt = useRef(Date.now());
  // refresh the header meta on a slow poll + on bus events (throttled)
  const meta = useLoad(async () => {
    const m = await getMeta();
    lastFetchAt.current = Date.now();
    return m;
  }, [], META_POLL_MS);
  const { connected } = useSSE({
    max: 1,
    onEvent: () => {
      const now = Date.now();
      if (now - lastFetchAt.current < META_EVENT_MIN_GAP_MS) return;
      lastFetchAt.current = now;
      meta.reload();
    },
  });

  const running = meta.data?.queue.by_status["running"] ?? 0;
  const queued = meta.data?.queue.by_status["queued"] ?? 0;
  const availableHands = meta.data?.hands.filter((h) => h.available).length ?? 0;

  return (
    <header className="topbar">
      <span>
        <span className={`dot ${connected ? "on" : "off"}`} />
        {connected ? "事件流已连接" : "事件流断开"}
      </span>
      {meta.data && (
        <>
          <span className="stat">
            工作日 <b>{meta.data.work_date}</b>
          </span>
          <span className="stat">
            运行中 <b>{running}</b> · 排队 <b>{queued}</b>
          </span>
          <span className="stat">
            可用执行手 <b>{availableHands}/{meta.data.hands.length}</b>
          </span>
        </>
      )}
      <span className="spacer" />
      {meta.data && (
        <span className="mono faint">
          v{meta.data.version} · {meta.data.timezone}
        </span>
      )}
    </header>
  );
}

export default function App() {
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="zh">研究所</div>
          <div className="en">institute-one</div>
        </div>
        <nav className="nav">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"}>
              <span className="zh">{n.zh}</span>{" "}
              <span className="en">{n.en}</span>
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="main">
        <Topbar />
        <main className="content">
          <Suspense fallback={<Loading />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/tasks" element={<Tasks />} />
              <Route path="/ask" element={<Ask />} />
              <Route path="/workflows" element={<Workflows />} />
              <Route path="/workflows/runs/:runId" element={<RunDetail />} />
              <Route path="/whiteboard" element={<Whiteboard />} />
              <Route path="/whiteboard/:boardId" element={<BoardDetail />} />
              <Route path="/mailbox" element={<Mailbox />} />
              <Route path="/mailbox/:threadId" element={<ThreadDetail />} />
              <Route path="/research" element={<Research />} />
              <Route path="/research/:itemId" element={<Research />} />
              <Route path="/trees" element={<Trees />} />
              <Route path="/trees/:treeId" element={<Trees />} />
              <Route path="/projects" element={<Projects />} />
              <Route path="/projects/:projectId" element={<Projects />} />
              <Route path="/forecasts" element={<Forecasts />} />
              <Route path="/multi-agent" element={<MultiAgent />} />
              <Route path="/sessions" element={<Sessions />} />
              <Route path="/sessions/:sessionId" element={<Sessions />} />
              <Route path="/analysts" element={<Analysts />} />
              <Route path="/hands" element={<Hands />} />
              <Route path="/cron" element={<CronHealth />} />
              <Route path="/operator" element={<Operator />} />
              <Route path="/insights" element={<Insights />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </div>
  );
}
