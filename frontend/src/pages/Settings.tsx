import { useState } from "react";
import { clearHandCooldown, getMeta, getVaultStatus, vaultDoctor } from "../api";
import {
  Empty,
  ErrorNote,
  Loading,
  PageHead,
  StatusBadge,
  countdown,
  fmtBytes,
  useLoad,
  useNow,
} from "../ui";

export default function Settings() {
  const now = useNow(1000);
  const meta = useLoad(getMeta, [], 10000);
  const vault = useLoad(getVaultStatus, [], 30000);

  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const clearCooldown = async (name: string) => {
    setErr(null);
    setNote(null);
    try {
      await clearHandCooldown(name);
      setNote(`已解除 ${name} 的冷却`);
      meta.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const doctor = async () => {
    setErr(null);
    setNote("正在体检 vault…");
    try {
      const report = await vaultDoctor();
      setNote(`vault doctor 完成：${JSON.stringify(report)}`);
      vault.reload();
    } catch (e) {
      setNote(null);
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const m = meta.data;
  const byStatus = m?.queue.by_status ?? {};

  return (
    <>
      <PageHead zh="设置" en="Settings" />
      {note && <div className="ok-note">{note}</div>}
      <ErrorNote error={err} />

      <div className="card">
        <h2>
          执行手<span className="en">hands</span>
        </h2>
        <ErrorNote error={meta.error} />
        {meta.loading && !m && <Loading />}
        <table className="data">
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>已安装</th>
              <th>可用</th>
              <th>冷却</th>
              <th>连续失败</th>
              <th>回退链</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {(m?.hands ?? []).map((h) => {
              const cooling = h.cooldown_until !== null && h.cooldown_until > now / 1000;
              return (
                <tr key={h.name}>
                  <td className="mono">
                    {h.name}
                    {h.degraded && (
                      <span className="hand-cooldown" style={{ marginLeft: 8 }}>
                        降级
                      </span>
                    )}
                  </td>
                  <td className="dim">{h.type}</td>
                  <td>{h.installed ? "✓" : <span className="faint">未安装</span>}</td>
                  <td>
                    <span className={`dot ${h.available ? "on" : "off"}`} />
                    {h.available ? "可用" : "不可用"}
                  </td>
                  <td>
                    {cooling && h.cooldown_until !== null ? (
                      <span className="hand-cooldown" title={h.cooldown_reason ?? ""}>
                        {countdown(h.cooldown_until, now)}
                        {h.cooldown_reason ? ` · ${h.cooldown_reason}` : ""}
                      </span>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td className="mono">{h.consecutive_failures}</td>
                  <td className="dim mono">{h.fallback_chain.join(" → ") || "—"}</td>
                  <td>
                    {cooling && (
                      <button className="small ghost" onClick={() => clearCooldown(h.name)}>
                        解除冷却
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {m?.hands.length === 0 && <Empty text="没有注册的执行手" />}
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>
            队列统计<span className="en">queue stats</span>
          </h2>
          <div className="stat-row">
            <div className="stat-box">
              <div className="n">{m?.queue.running_now ?? "—"}</div>
              <div className="l">本进程在跑</div>
            </div>
            {Object.entries(byStatus).map(([s, n]) => (
              <div className="stat-box" key={s}>
                <div className="n">{n}</div>
                <div className="l">
                  <StatusBadge status={s} />
                </div>
              </div>
            ))}
            {m && Object.keys(byStatus).length === 0 && <Empty text="还没有任务" />}
          </div>
          {m && (
            <dl className="kv" style={{ marginTop: 12 }}>
              <dt>最大并发</dt>
              <dd>{m.limits.max_concurrent}</dd>
              <dt>默认超时</dt>
              <dd>{m.limits.default_timeout_s} 秒</dd>
              <dt>输出上限</dt>
              <dd>{fmtBytes(m.limits.output_cap_bytes)}</dd>
            </dl>
          )}
        </div>

        <div className="card">
          <h2>
            知识库<span className="en">vault</span>
            {vault.data?.configured && (
              <button className="small ghost" style={{ marginLeft: 12 }} onClick={doctor}>
                体检 doctor
              </button>
            )}
          </h2>
          <ErrorNote error={vault.error} />
          {vault.loading && !vault.data && <Loading />}
          {vault.data && (
            <dl className="kv">
              <dt>已配置</dt>
              <dd>{vault.data.configured ? "是" : "否（未设置 vault_dir）"}</dd>
              <dt>目录</dt>
              <dd>{vault.data.vault_dir ?? "—"}</dd>
              <dt>索引总数</dt>
              <dd>{vault.data.total}</dd>
              {Object.entries(vault.data.counts).map(([state, n]) => (
                <Fragment2 key={state} k={state === "clean" ? "正常 clean" : state === "conflict" ? "冲突 conflict" : state} v={n} />
              ))}
            </dl>
          )}
        </div>
      </div>

      <div className="card">
        <h2>
          系统信息<span className="en">system</span>
        </h2>
        {m && (
          <dl className="kv">
            <dt>版本</dt>
            <dd>v{m.version}</dd>
            <dt>时区</dt>
            <dd>{m.timezone}</dd>
            <dt>工作日</dt>
            <dd>{m.work_date}</dd>
          </dl>
        )}
        {!m && !meta.error && <Loading />}
      </div>
    </>
  );
}

/** dt/dd pair as a keyed fragment helper for dynamic kv lists. */
function Fragment2({ k, v }: { k: string; v: number }) {
  return (
    <>
      <dt>{k}</dt>
      <dd style={k.startsWith("冲突") && v > 0 ? { color: "var(--red)" } : undefined}>{v}</dd>
    </>
  );
}
