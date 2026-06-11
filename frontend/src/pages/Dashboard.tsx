import { Link } from "react-router-dom";
import { clearHandCooldown, getMeta, listTasks } from "../api";
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

const STATUS_ORDER = ["running", "queued", "completed", "failed", "rate_limited", "cancelled", "expired"];

export default function Dashboard() {
  const now = useNow(1000);
  const { events, connected, lastEvent } = useSSE({ max: 60 });
  const meta = useLoad(getMeta, [lastEvent?.id ?? 0], 15000);
  const todays = useLoad(() => listTasks({ limit: 30 }), [lastEvent?.id ?? 0], 30000);

  const byStatus = meta.data?.queue.by_status ?? {};
  const statuses = [
    ...STATUS_ORDER.filter((s) => s in byStatus),
    ...Object.keys(byStatus).filter((s) => !STATUS_ORDER.includes(s)),
  ];

  return (
    <>
      <PageHead zh="总览" en="Dashboard" />

      <div className="card">
        <h2>
          队列概况<span className="en">queue</span>
        </h2>
        <ErrorNote error={meta.error} />
        <div className="stat-row">
          <div className="stat-box">
            <div className="n">{meta.data?.queue.running_now ?? "—"}</div>
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
          {statuses.length === 0 && !meta.loading && <Empty text="还没有任务" />}
        </div>
      </div>

      <div className="card">
        <h2>
          执行手状态<span className="en">hands</span>
        </h2>
        <div className="stat-row">
          {(meta.data?.hands ?? []).map((h) => {
            const cooling = h.cooldown_until !== null && h.cooldown_until > now / 1000;
            return (
              <div className="chip" key={h.name} title={h.cooldown_reason ?? h.type}>
                <span className={`dot ${h.available ? "on" : "off"}`} />
                <span className="name">{h.name}</span>
                {!h.installed && <span className="faint">未安装</span>}
                {h.degraded && <span className="hand-cooldown">降级</span>}
                {cooling && h.cooldown_until !== null && (
                  <span className="hand-cooldown">冷却 {countdown(h.cooldown_until, now)}</span>
                )}
                {cooling && (
                  <button
                    className="small ghost"
                    onClick={() => clearHandCooldown(h.name).then(meta.reload)}
                  >
                    解除
                  </button>
                )}
              </div>
            );
          })}
          {meta.loading && !meta.data && <Loading />}
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>
            实时事件<span className="en">live events {connected ? "· live" : "· reconnecting"}</span>
          </h2>
          <div className="feed">
            {events.map((e) => (
              <div className="feed-item" key={e.id}>
                <span className="type">{e.type}</span>
                <span className="ref">
                  {e.ref_kind}:{e.ref_id}
                </span>
                <span className="t">{ago(e.created_at)}</span>
              </div>
            ))}
            {events.length === 0 && <Empty text="等待事件中…" />}
          </div>
        </div>

        <div className="card">
          <h2>
            最近任务<span className="en">recent tasks</span>
          </h2>
          <ErrorNote error={todays.error} />
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
              {(todays.data ?? []).slice(0, 15).map((t) => (
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
          {todays.data?.length === 0 && <Empty text="今天还没有任务" />}
        </div>
      </div>
    </>
  );
}
