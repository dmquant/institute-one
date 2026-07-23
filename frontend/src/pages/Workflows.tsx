import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Workflow,
  listRuns,
  listWorkflows,
  runBriefingNow,
  runDailyNow,
  runWorkflow,
} from "../api";
import { useSSE } from "../useSSE";
import { Empty, ErrorNote, Loading, PageHead, StatusBadge, ago, fmtTime, useLoad } from "../ui";

export default function Workflows() {
  const navigate = useNavigate();
  const { lastEvent } = useSSE({ types: ["workflow"], max: 1 });
  const wfs = useLoad(listWorkflows, []);
  const runs = useLoad(() => listRuns({ limit: 30 }), [lastEvent?.id ?? 0], 20000);
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const daily = async (fn: () => Promise<{ run_id: string | null; skipped: boolean }>, name: string) => {
    setErr(null);
    setNote(`${name} 执行中…`);
    try {
      const r = await fn();
      setNote(r.skipped ? `${name}：今天已生成，跳过` : `${name}：运行完成 ${r.run_id}`);
      runs.reload();
    } catch (e) {
      setNote(null);
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>
      <PageHead zh="工作流" en="Workflows">
        <button onClick={() => daily(runBriefingNow, "晨间简报")}>立即生成简报</button>
        <button onClick={() => daily(runDailyNow, "每日报告")}>立即生成日报</button>
      </PageHead>
      {note && <div className="ok-note">{note}</div>}
      <ErrorNote error={err} />

      <div className="grid cols-2">
        <div>
          <ErrorNote error={wfs.error} />
          {wfs.loading && !wfs.data && <Loading />}
          {(wfs.data ?? []).map((wf) => (
            <WorkflowCard key={wf.id} wf={wf} onStarted={(runId) => navigate(`/workflows/runs/${runId}`)} />
          ))}
          {wfs.data?.length === 0 && <Empty text="没有已注册的工作流" />}
        </div>

        <div className="card">
          <h2>
            最近运行<span className="en">recent runs</span>
          </h2>
          <ErrorNote error={runs.error} />
          <table className="data">
            <thead>
              <tr>
                <th>运行</th>
                <th>工作流</th>
                <th>状态</th>
                <th>步骤</th>
                <th>开始</th>
              </tr>
            </thead>
            <tbody>
              {(runs.data ?? []).map((r) => (
                <tr key={r.id}>
                  <td className="mono">
                    <Link to={`/workflows/runs/${r.id}`}>{r.id}</Link>
                  </td>
                  <td>{r.workflow_id}</td>
                  <td>
                    <StatusBadge status={r.status} />
                  </td>
                  <td className="mono">{r.current_step}</td>
                  <td className="dim nowrap" title={fmtTime(r.started_at)}>
                    {ago(r.started_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {runs.data?.length === 0 && <Empty text="还没有运行记录" />}
        </div>
      </div>
    </>
  );
}

// Server-side lazy variables (app/institute/workflows.py _drive): computed
// at run time when a step prompt references them and no explicit value was
// passed. Rendering an input for them — and thereby submitting an empty
// string — would count as an explicit value and suppress that lazy
// computation, so they stay out of the form.
const LAZY_VARIABLES = new Set(["DATA_BUNDLE", "WEEK_DISPUTES"]);

function WorkflowCard({ wf, onStarted }: { wf: Workflow; onStarted: (runId: string) => void }) {
  const [vars, setVars] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      // an empty input means "not provided": sending "" would count as an
      // explicit value on the run and suppress server-side defaults/lazy vars
      const filled = Object.fromEntries(Object.entries(vars).filter(([, value]) => value !== ""));
      const r = await runWorkflow(wf.id, Object.keys(filled).length > 0 ? filled : undefined);
      onStarted(r.run_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const visibleVariables = wf.variables.filter((v) => !LAZY_VARIABLES.has(v));

  return (
    <div className="card">
      <h2>
        {wf.name}
        <span className="en">{wf.id}</span>
      </h2>
      {wf.description && <p className="dim" style={{ marginTop: 0 }}>{wf.description}</p>}
      <div className="dim" style={{ fontSize: 12, marginBottom: 8 }}>
        {wf.steps.length} 个步骤：{wf.steps.map((s) => s.title ?? s.id).join(" → ")}
      </div>
      {visibleVariables.length > 0 && (
        <div className="form-row" style={{ marginBottom: 10 }}>
          {visibleVariables.map((v) => (
            <label className="field grow" key={v}>
              <span className="lbl mono">{v}</span>
              <input
                style={{ width: "100%" }}
                value={vars[v] ?? ""}
                placeholder={v === "WORK_DATE" ? "默认今天" : ""}
                onChange={(e) => setVars((prev) => ({ ...prev, [v]: e.target.value }))}
              />
            </label>
          ))}
        </div>
      )}
      <ErrorNote error={err} />
      <button onClick={run} disabled={busy}>
        {busy ? "启动中…" : "立即运行"}
      </button>
    </div>
  );
}
