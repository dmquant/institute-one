import { getCronHealth } from "../api";
import { Empty, ErrorNote, Loading, PageHead, ago, fmtTime, useLoad } from "../ui";

function fmtRate(rate: number | null): string {
  if (rate === null) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

function fmtDuration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export default function CronHealth() {
  const health = useLoad(getCronHealth, [], 30000);

  const jobs = Object.entries(health.data?.jobs ?? {}).sort(([a], [b]) => a.localeCompare(b));

  return (
    <>
      <PageHead zh="定时任务" en={`Cron Health · ${health.data?.window_days ?? 30}d window`}>
        <button className="ghost" onClick={health.reload}>
          刷新
        </button>
      </PageHead>

      <div className="card">
        <ErrorNote error={health.error} />
        {health.loading && !health.data && <Loading />}
        <table className="data">
          <thead>
            <tr>
              <th>任务</th>
              <th>受控</th>
              <th>调度</th>
              <th>下次触发</th>
              <th>最近触发</th>
              <th>最近状态</th>
              <th>触发数</th>
              <th>成功率</th>
              <th>平均时长</th>
              <th>维护跳过</th>
              <th>最近错误</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map(([name, j]) => (
              <tr key={name}>
                <td className="mono">
                  {name}
                  {!j.registered && (
                    <span className="hand-cooldown" style={{ marginLeft: 8 }} title="不在调度器的注册表里（服务未跑、配置禁用或已改名）">
                      未注册
                    </span>
                  )}
                </td>
                <td>
                  {j.gated === true && <span title="维护模式暂停时跳过（会发起新模型调用）">受控</span>}
                  {j.gated === false && <span className="faint">—</span>}
                  {j.gated === null && <span className="faint">?</span>}
                </td>
                <td className="dim mono">{j.schedule ?? "—"}</td>
                <td className="dim nowrap" title={fmtTime(j.next_run_time)}>
                  {j.next_run_time ? fmtTime(j.next_run_time) : "—"}
                </td>
                <td className="dim nowrap" title={fmtTime(j.last_fired_at)}>
                  {j.last_fired_at ? ago(j.last_fired_at) : <span className="faint">从未</span>}
                </td>
                <td>
                  {j.last_status === "ok" && <span style={{ color: "var(--green)" }}>✓ ok</span>}
                  {j.last_status === "failed" && <span style={{ color: "var(--red)" }}>✗ failed</span>}
                  {j.last_status === "skipped" && (
                    <span className="hand-cooldown" title="维护模式跳过">
                      跳过
                    </span>
                  )}
                  {j.last_status === null && <span className="faint">—</span>}
                </td>
                <td className="mono">
                  {j.fires}
                  <span className="faint">
                    {" "}
                    ({j.ok}✓/{j.failed}✗)
                  </span>
                </td>
                <td className="mono" style={j.ok_rate !== null && j.ok_rate < 0.9 ? { color: "var(--amber)" } : undefined}>
                  {fmtRate(j.ok_rate)}
                </td>
                <td className="mono">{fmtDuration(j.avg_duration_ms)}</td>
                <td className="mono">{j.skipped > 0 ? <span className="hand-cooldown">{j.skipped}</span> : "0"}</td>
                <td className="dim ellipsis" title={j.last_error?.error ?? ""}>
                  {j.last_error ? (
                    <>
                      <span className="faint">{ago(j.last_error.fired_at)}</span> {j.last_error.error ?? ""}
                    </>
                  ) : (
                    <span className="faint">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {health.data && jobs.length === 0 && <Empty text="没有已注册的定时任务，也没有历史指标" />}
      </div>
    </>
  );
}
