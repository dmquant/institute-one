import { useState } from "react";
import { Link } from "react-router-dom";
import {
  clearHandCooldown,
  getAdminState,
  getMeta,
  isMaintenancePaused,
  listTasks,
  setMaintenance,
} from "../api";
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

const STATUS_ORDER = ["running", "queued", "completed", "failed", "rate_limited", "cancelled", "expired"];

export default function Dashboard() {
  const now = useNow(1000);
  const { events, connected, lastEvent } = useSSE({ max: 60 });
  const meta = useLoad(getMeta, [lastEvent?.id ?? 0], 15000);
  const todays = useLoad(() => listTasks({ limit: 30 }), [lastEvent?.id ?? 0], 30000);
  const admin = useLoad(getAdminState, [], 30000);
  const [resumeErr, setResumeErr] = useState<string | null>(null);
  const [resuming, setResuming] = useState(false);

  const byStatus = meta.data?.queue.by_status ?? {};
  const statuses = [
    ...STATUS_ORDER.filter((s) => s in byStatus),
    ...Object.keys(byStatus).filter((s) => !STATUS_ORDER.includes(s)),
  ];

  const maintenancePaused = isMaintenancePaused(admin.data);

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
          <EventFeed events={events} />
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
