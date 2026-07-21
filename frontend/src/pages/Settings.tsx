import { useState } from "react";
import { Link } from "react-router-dom";
import {
  clearHandCooldown,
  getAdminState,
  getAuthToken,
  getCronHealth,
  getLocalePreference,
  getMeta,
  getTask,
  getVaultStatus,
  isBilingualEnabled,
  isMaintenancePaused,
  listEvents,
  setAuthToken,
  setLocalePreference,
  setMaintenance,
  vaultDoctor,
} from "../api";
import type { LocalePreference, TwinReadyPayload } from "../api";
import { useSSE } from "../useSSE";
import {
  Empty,
  ErrorNote,
  Loading,
  Markdown,
  PageHead,
  StatusBadge,
  ago,
  countdown,
  fmtBytes,
  useLoad,
  useNow,
} from "../ui";

export default function Settings() {
  const now = useNow(1000);
  const meta = useLoad(getMeta, [], 10000);
  const vault = useLoad(getVaultStatus, [], 30000);
  const admin = useLoad(getAdminState, [], 15000);
  const cron = useLoad(getCronHealth, [], 60000);

  // gate lists come from /api/cron/health's registry fields (gated flag set
  // in app/institute/scheduler.py) — no hardcoded job names here
  const cronJobs = Object.entries(cron.data?.jobs ?? {});
  const gatedJobs = cronJobs.filter(([, j]) => j.gated === true).map(([name]) => name);
  const ungatedJobs = cronJobs.filter(([, j]) => j.gated === false).map(([name]) => name);

  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);

  const paused = isMaintenancePaused(admin.data);

  const toggleMaintenance = async () => {
    setErr(null);
    setNote(null);
    setSwitching(true);
    try {
      const r = await setMaintenance(!paused);
      setNote(r.paused ? "维护模式已开启：受控定时任务将跳过" : "维护模式已关闭：定时任务恢复");
      admin.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSwitching(false);
    }
  };

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

      <AuthTokenCard />

      <div className="card">
        <h2>
          维护模式<span className="en">maintenance</span>
        </h2>
        <ErrorNote error={admin.error} />
        <div className="form-row" style={{ alignItems: "center" }}>
          <span>
            当前状态：
            {paused ? (
              <b style={{ color: "var(--amber)" }}>已暂停</b>
            ) : (
              <b style={{ color: "var(--green)" }}>正常运行</b>
            )}
          </span>
          <button className={paused ? undefined : "danger"} onClick={toggleMaintenance} disabled={switching || admin.loading}>
            {switching ? "切换中…" : paused ? "恢复运行" : "暂停定时任务"}
          </button>
          <Link to="/cron" className="faint">
            查看定时任务健康 →
          </Link>
        </div>
        <p className="dim" style={{ fontSize: 12.5, margin: "10px 0 0" }}>
          暂停期间以下受控任务跳过（不再发起新模型调用），在途任务照常收尾：
          {" "}
          <span className="mono">{gatedJobs.length > 0 ? gatedJobs.join(" · ") : "…"}</span>
          ；不耗模型额度的任务（
          <span className="mono">{ungatedJobs.length > 0 ? ungatedJobs.join(" · ") : "…"}</span>
          ）不受影响。
        </p>
      </div>

      <BilingualCard enabled={isBilingualEnabled(admin.data)} />

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

function AuthTokenCard() {
  const [token, setToken] = useState(getAuthToken);

  const save = () => {
    setAuthToken(token);
    window.location.reload();
  };

  return (
    <div className="card">
      <h2>
        访问令牌<span className="en">API bearer token</span>
      </h2>
      <div className="form-row" style={{ alignItems: "end" }}>
        <label className="field grow">
          <span className="lbl">INSTITUTE_TOKEN（未启用后端鉴权时留空）</span>
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            style={{ width: "100%" }}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Bearer token"
          />
        </label>
        <button onClick={save}>保存并重新连接</button>
      </div>
      <p className="dim" style={{ fontSize: 12.5, margin: "10px 0 0" }}>
        令牌保存在此浏览器的 <span className="mono">localStorage</span>，后续 API 请求会自动附加{" "}
        <span className="mono">Authorization: Bearer …</span>。清空后保存即可移除。
      </p>
    </div>
  );
}

/** Bilingual twins (Phase 7): read-only switch state + twin_ready feed.
 * The enabled switch has no write endpoint, while the independent locale
 * preference is read/write through /api/bilingual/preference.
 * Full twin text is BY REFERENCE: payload.task_id -> GET /api/tasks/{id}. */
function BilingualCard({ enabled }: { enabled: boolean }) {
  const { lastEvent } = useSSE({ types: ["bilingual."], max: 1 });
  const twins = useLoad(() => listEvents(0, "bilingual.twin_ready", 200), [lastEvent?.id ?? 0]);
  const locale = useLoad(getLocalePreference, []);
  const [viewing, setViewing] = useState<{ taskId: string; text: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [savingLocale, setSavingLocale] = useState(false);

  const chooseLocale = async (next: LocalePreference) => {
    setErr(null);
    setSavingLocale(true);
    try {
      await setLocalePreference(next);
      locale.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingLocale(false);
    }
  };

  const openTwin = async (taskId: string) => {
    setErr(null);
    try {
      const t = await getTask(taskId);
      setViewing({ taskId, text: t.output || "（任务输出为空）" });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const recent = (twins.data ?? []).slice(-10).reverse(); // newest first

  return (
    <div className="card">
      <h2>
        双语孪生<span className="en">bilingual twins</span>
      </h2>
      <ErrorNote error={err} />
      <ErrorNote error={locale.error} />
      <div className="form-row" style={{ alignItems: "center", marginBottom: 10 }}>
        <span>默认内容语言：</span>
        <button
          className={locale.data?.locale === "zh" ? undefined : "ghost"}
          disabled={savingLocale || locale.loading || locale.data?.locale === "zh"}
          onClick={() => chooseLocale("zh")}
        >
          中文 zh
        </button>
        <button
          className={locale.data?.locale === "en" ? undefined : "ghost"}
          disabled={savingLocale || locale.loading || locale.data?.locale === "en"}
          onClick={() => chooseLocale("en")}
        >
          English en
        </button>
        {locale.loading && !locale.data && <span className="faint">读取中…</span>}
      </div>
      <div className="form-row" style={{ alignItems: "center" }}>
        <span>
          当前状态：
          {enabled ? (
            <b style={{ color: "var(--green)" }}>已开启</b>
          ) : (
            <b style={{ color: "var(--amber)" }}>已关闭（默认）</b>
          )}
        </span>
        <span className="faint">此开关暂为只读展示（后端尚无写端点）</span>
      </div>
      <p className="dim" style={{ fontSize: 12.5, margin: "10px 0 0" }}>
        开启后，briefing / daily 工作流完成时自动把编译报告翻译成英文孪生（走默认执行手，
        消耗模型额度；维护暂停期间跳过）。开关存于 <span className="mono">admin_state</span> 的{" "}
        <span className="mono">bilingual:enabled</span> 行，开启方法（服务器上执行，默认 DB 路径
        <span className="mono"> ~/.institute-one/institute.db</span>，写{" "}
        <span className="mono">'false'</span> 即关闭）：
      </p>
      <pre className="file-pre" style={{ marginTop: 8 }}>
        {`sqlite3 ~/.institute-one/institute.db "INSERT INTO admin_state (key, value) VALUES ('bilingual:enabled','true') ON CONFLICT(key) DO UPDATE SET value=excluded.value;"`}
      </pre>

      <h2 style={{ marginTop: 16 }}>
        英文孪生产出<span className="en">twin_ready events</span>
      </h2>
      <ErrorNote error={twins.error} />
      {twins.loading && !twins.data && <Loading />}
      <div className="feed">
        {recent.map((e) => {
          const p = e.payload as TwinReadyPayload;
          return (
            <div className="feed-item" key={e.id}>
              <span className="type">{p.workflow_id ?? "?"}</span>
              <span className="ref mono">
                {p.work_date ?? "—"} · run {p.run_id ?? e.ref_id} ·{" "}
                {typeof p.text_bytes === "number" ? fmtBytes(p.text_bytes) : "—"}
              </span>
              {p.task_id && (
                <button className="small ghost" onClick={() => openTwin(p.task_id!)}>
                  查看全文
                </button>
              )}
              <span className="t">{ago(e.created_at)}</span>
              {p.summary && <div className="faint" style={{ flexBasis: "100%", fontSize: 12 }}>{p.summary}</div>}
            </div>
          );
        })}
        {recent.length === 0 && <Empty text="还没有英文孪生产出" />}
      </div>

      {viewing && (
        <>
          <h2 style={{ marginTop: 16 }}>
            孪生全文<span className="en">task {viewing.taskId}</span>
            <button className="small ghost" style={{ marginLeft: 12 }} onClick={() => setViewing(null)}>
              关闭
            </button>
          </h2>
          <Markdown text={viewing.text} />
        </>
      )}
    </div>
  );
}
