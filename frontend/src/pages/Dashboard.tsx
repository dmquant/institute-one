import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  clearHandCooldown,
  getAdminState,
  getForecastStats,
  getMeta,
  getOperatorTriage,
  getVectorHealth,
  isMaintenancePaused,
  listTasks,
  setMaintenance,
} from "../api";
import type { HandStatus } from "../api";
import { EventFeed } from "../events";
import { useSSE } from "../useSSE";
import {
  Empty,
  ErrorNote,
  Loading,
  PageHead,
  StatusBadge,
  ago,
  countdown,
  useLoad,
  useNow,
} from "../ui";

const STATUS_ORDER = [
  "running",
  "queued",
  "completed",
  "failed",
  "overcommitted",
  "rate_limited",
  "cancelled",
  "expired",
];

const VECTOR_REASON_ZH: Record<string, string> = {
  healthy: "健康",
  vectors_disabled: "向量功能未启用",
  vec_ext_missing: "sqlite-vec 不可用",
  ollama_unreachable: "Ollama 不可达",
  model_missing: "嵌入模型缺失",
  vector_error: "向量服务异常",
};

// Event-driven meta/task refreshes are throttled: on a busy bus, keying the
// fetches off every event id would refetch (and re-render) nonstop — same
// pattern as the App.tsx Topbar.
const META_EVENT_MIN_GAP_MS = 5_000;

export default function Dashboard() {
  // mount time ≈ the initial fetch, so the first events don't double-fetch
  const lastFetchAt = useRef(Date.now());
  const meta = useLoad(async () => {
    const m = await getMeta();
    lastFetchAt.current = Date.now();
    return m;
  }, [], 15000);
  const todays = useLoad(async () => {
    const rows = await listTasks({ limit: 30 });
    lastFetchAt.current = Date.now();
    return rows;
  }, [], 30000);
  const { events, connected } = useSSE({
    max: 60,
    onEvent: () => {
      const now = Date.now();
      if (now - lastFetchAt.current < META_EVENT_MIN_GAP_MS) return;
      lastFetchAt.current = now;
      meta.reload();
      todays.reload();
    },
  });
  const admin = useLoad(getAdminState, [], 30000);
  const triage = useLoad(getOperatorTriage, [], 30000);
  const forecastHitRate = useLoad(getForecastStats, [], 300000);
  const vectors = useLoad(getVectorHealth, [], 30000);
  const [resumeErr, setResumeErr] = useState<string | null>(null);
  const [resuming, setResuming] = useState(false);

  const byStatus = meta.data?.queue.by_status ?? {};
  const statuses = [
    ...STATUS_ORDER.filter((s) => s in byStatus),
    ...Object.keys(byStatus).filter((s) => !STATUS_ORDER.includes(s)),
  ];

  const maintenancePaused = isMaintenancePaused(admin.data);
  const decisiveForecasts = (forecastHitRate.data?.hits ?? 0) + (forecastHitRate.data?.misses ?? 0);
  const vectorHealthy = vectors.data?.enabled === true && vectors.data.reason === "healthy";
  const vectorLabel = !vectors.data
    ? "—"
    : !vectors.data.enabled
      ? "未启用"
      : vectorHealthy
        ? "健康"
        : "降级";
  const vectorChunks = vectors.data
    ? (vectors.data.chunk_counts[vectors.data.current_model] ?? 0)
    : null;

  const resume = async () => {
    setResumeErr(null);
    setResuming(true);
    try {
      await setMaintenance(false);
      admin.reload();
    } catch (e) {
      setResumeErr(e instanceof Error ? e.message : String(e));
    } finally {
      setResuming(false);
    }
  };

  return (
    <>
      <PageHead zh="总览" en="Dashboard" />

      {/* a failed read must not silently pass as "未暂停" (banner gone) */}
      {admin.error && <ErrorNote error={`维护状态读取失败：${admin.error}`} />}
      <ErrorNote error={resumeErr} />
      {maintenancePaused && (
        <div className="error-note">
          维护模式已开启：定时任务（简报/日报/白板/信箱/研究/记忆压缩）暂停发起新模型调用，在途任务继续。
          <button
            className="small ghost"
            style={{ marginLeft: 10 }}
            onClick={resume}
            disabled={resuming}
          >
            {resuming ? "恢复中…" : "恢复运行"}
          </button>
        </div>
      )}

      <div className="card">
        <h2>
          操作信号<span className="en">operator signals</span>
        </h2>
        <ErrorNote error={triage.error ? `运维待办读取失败：${triage.error}` : null} />
        <ErrorNote error={forecastHitRate.error ? `预测命中率读取失败：${forecastHitRate.error}` : null} />
        <ErrorNote error={vectors.error ? `向量健康读取失败：${vectors.error}` : null} />
        <div className="stat-row">
          <Link
            className="stat-box"
            style={{ color: "inherit", display: "block", textDecoration: "none" }}
            title="打开运维操作台"
            to="/operator"
          >
            <div className="n">
              {triage.loading && !triage.data ? "…" : (triage.data?.actions.open ?? "—")}
            </div>
            <div className="l">待处置事项 · open</div>
          </Link>
          <Link
            className="stat-box"
            style={{ color: "inherit", display: "block", textDecoration: "none" }}
            title="命中率 = hit / (hit + miss)，partial 不计入分母"
            to="/forecasts"
          >
            <div className="n">
              {forecastHitRate.loading && !forecastHitRate.data
                ? "…"
                : decisiveForecasts > 0 && forecastHitRate.data
                  ? `${Math.round((forecastHitRate.data.hits / decisiveForecasts) * 100)}%`
                  : "—"}
            </div>
            <div className="l">
              预测命中率
              <span className="faint" style={{ display: "block" }}>
                {forecastHitRate.data
                  ? `${forecastHitRate.data.hits}/${decisiveForecasts} hit/miss${
                      forecastHitRate.data.partial ? ` · partial ${forecastHitRate.data.partial}` : ""
                    }`
                  : "hit / (hit + miss)"}
              </span>
            </div>
          </Link>
          <div
            className="stat-box"
            title={vectors.data ? (VECTOR_REASON_ZH[vectors.data.reason] ?? vectors.data.reason) : undefined}
          >
            <div
              className="n"
              style={{
                color: vectors.data
                  ? vectorHealthy
                    ? "var(--green)"
                    : vectors.data.enabled
                      ? "var(--amber)"
                      : "var(--text-dim)"
                  : undefined,
              }}
            >
              {vectors.loading && !vectors.data ? "…" : vectorLabel}
            </div>
            <div className="l">
              向量检索
              <span className="faint mono" style={{ display: "block" }}>
                {vectors.data
                  ? `${VECTOR_REASON_ZH[vectors.data.reason] ?? vectors.data.reason} · ${vectors.data.current_model} · ${
                      vectorChunks ?? 0
                    } 块`
                  : "GET /api/vectors/health"}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>
          队列概况<span className="en">queue</span>
        </h2>
        <ErrorNote error={meta.error} />
        {meta.loading && !meta.data ? (
          <Loading />
        ) : (
          meta.data && (
            <>
              <div className="stat-row">
                <div className="stat-box">
                  <div className="n">{meta.data.queue.running_now}</div>
                  <div className="l">本进程在跑</div>
                </div>
                {statuses.map((s) => (
                  <div className="stat-box" key={s}>
                    <div className="n">{byStatus[s]}</div>
                    <div className="l">
                      <StatusBadge status={s} />
                    </div>
                  </div>
                ))}
              </div>
              {statuses.length === 0 && <Empty text="还没有任务" />}
            </>
          )
        )}
      </div>

      <div className="card">
        <h2>
          执行手状态<span className="en">hands</span>
        </h2>
        <ErrorNote error={meta.error} />
        {meta.loading && !meta.data && <Loading />}
        <div className="stat-row">
          {(meta.data?.hands ?? []).map((h) => (
            <div className="chip" key={h.name} title={h.cooldown_reason ?? h.type}>
              <span className={`dot ${h.available ? "on" : "off"}`} />
              <span className="name">{h.name}</span>
              {!h.installed && <span className="faint">未安装</span>}
              {h.degraded && <span className="hand-cooldown">降级</span>}
              <HandCooldown
                hand={h}
                onClear={() => clearHandCooldown(h.name).then(meta.reload)}
              />
            </div>
          ))}
        </div>
        {meta.data?.hands.length === 0 && !meta.loading && <Empty text="没有可用的执行手信息" />}
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>
            实时事件<span className="en">live events {connected ? "· live" : "· reconnecting"}</span>
          </h2>
          <EventFeed events={events} />
        </div>

        <div className="card">
          <h2>
            最近任务<span className="en">recent tasks</span>
          </h2>
          <ErrorNote error={todays.error} />
          {todays.loading && !todays.data && <Loading />}
          {todays.data && todays.data.length > 0 && (
            <table className="data">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>状态</th>
                  <th>执行手</th>
                  <th>来源</th>
                  <th>创建</th>
                </tr>
              </thead>
              <tbody>
                {todays.data.slice(0, 15).map((t) => (
                  <tr key={t.id}>
                    <td className="mono">
                      <Link to={`/tasks?id=${t.id}`}>{t.id}</Link>
                    </td>
                    <td>
                      <StatusBadge status={t.status} />
                    </td>
                    <td className="mono">{t.hand ?? t.requested_hand}</td>
                    <td className="dim">{t.source}</td>
                    <td className="dim nowrap">{ago(t.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {todays.data?.length === 0 && !todays.loading && <Empty text="今天还没有任务" />}
        </div>
      </div>
    </>
  );
}

/** Cooldown badge + clear button for one hand. Owns the 1s ticker so the page
 * doesn't re-render every second just to tick the countdown text. */
function HandCooldown({ hand, onClear }: { hand: HandStatus; onClear: () => void }) {
  const now = useNow(1000);
  if (hand.cooldown_until === null || hand.cooldown_until <= now / 1000) return null;
  return (
    <>
      <span className="hand-cooldown">冷却 {countdown(hand.cooldown_until, now)}</span>
      <button className="small ghost" onClick={onClear}>
        解除
      </button>
    </>
  );
}
