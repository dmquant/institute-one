import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, MultiAgentRun, listAnalysts, runMultiAgent } from "../api";
import { Empty, ErrorNote, Loading, PageHead, StatusBadge, useLoad } from "../ui";

// POST /api/multi-agent/run is card C5 (in flight). Until it lands the page
// stays usable: 404/501 render the "未启用" notice instead of an error. An
// unmounted router actually answers 405 today — the SPA catch-all is GET-only,
// so a POST to an unrouted path hits FastAPI's method check (R-C7 / C7-M2).
const MODES = [
  { value: "all", label: "全部返回 all" },
  { value: "first_success", label: "首个成功 first_success" },
  { value: "majority_vote", label: "多数表决 majority_vote" },
  { value: "best_effort", label: "尽力而为 best_effort" },
];

export default function MultiAgent() {
  const analysts = useLoad(listAnalysts, []);

  const [selected, setSelected] = useState<string[]>([]);
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState("all");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [run, setRun] = useState<MultiAgentRun | null>(null);

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const go = async () => {
    if (selected.length === 0 || !prompt.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setUnavailable(false);
    setRun(null);
    try {
      setRun(await runMultiAgent(selected, prompt.trim(), mode));
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 405 || e.status === 501)) {
        setUnavailable(true);
      } else {
        setErr(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
    }
  };

  const results = run?.results ?? [];

  return (
    <>
      <PageHead zh="多智能体对比" en="Multi-agent Compare" />

      <div className="card">
        <h2>
          发起对比<span className="en">run</span>
        </h2>
        <label className="field">
          <span className="lbl">选择智能体（多选）</span>
          <div className="stat-row">
            {(analysts.data ?? []).map((a) => (
              <button
                key={a.id}
                className={`small ${selected.includes(a.id) ? "" : "ghost"}`}
                onClick={() => toggle(a.id)}
                type="button"
              >
                {a.emoji} {a.name}
              </button>
            ))}
            {analysts.loading && !analysts.data && <Loading />}
            {analysts.data?.length === 0 && <Empty text="没有分析师" />}
          </div>
        </label>
        <div className="form-row">
          <label className="field grow">
            <span className="lbl">提示词 Prompt</span>
            <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3} placeholder="同一问题发给多个智能体，结果并排对比" />
          </label>
          <label className="field">
            <span className="lbl">汇合模式 Mode</span>
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              {MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          <button onClick={go} disabled={busy || selected.length === 0 || !prompt.trim()}>
            {busy ? "运行中…" : `运行（${selected.length} 个）`}
          </button>
        </div>
      </div>

      <ErrorNote error={err} />
      {unavailable && (
        <div className="card">
          <Empty text="多智能体接口未启用（POST /api/multi-agent/run 尚未部署，卡片 C5 在建）" />
        </div>
      )}
      {busy && (
        <div className="card">
          <Loading />
        </div>
      )}

      {run && (
        <>
          {(run.mode || run.status || run.id) && (
            <div className="form-row" style={{ marginBottom: 12, alignItems: "center" }}>
              {run.status && <StatusBadge status={run.status} />}
              {run.mode && <span className="dim mono">mode: {run.mode}</span>}
              {run.id && <span className="faint mono">{run.id}</span>}
            </div>
          )}
          <div className="grid cols-2">
            {results.map((r, i) => (
              <div className="card" key={r.agent ?? r.analyst_id ?? i}>
                <h2>
                  {r.agent ?? r.analyst_id ?? `结果 ${i + 1}`}
                  <span className="en">{r.hand ?? ""}</span>
                  {r.status && (
                    <span style={{ marginLeft: 10 }}>
                      <StatusBadge status={r.status} />
                    </span>
                  )}
                </h2>
                {r.error && <ErrorNote error={r.error} />}
                <pre style={{ maxHeight: 420, overflowY: "auto" }}>{r.output || "（无输出）"}</pre>
                {r.task_id && (
                  <Link className="mono faint" to={`/tasks?id=${r.task_id}`}>
                    任务 {r.task_id} →
                  </Link>
                )}
              </div>
            ))}
            {results.length === 0 && (
              <div className="card">
                <Empty text="接口返回了空结果" />
              </div>
            )}
          </div>
        </>
      )}
    </>
  );
}
