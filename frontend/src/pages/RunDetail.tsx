import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { cancelRun, getRun, readWorkspaceFile } from "../api";
import { useSSE } from "../useSSE";
import {
  Empty,
  ErrorNote,
  FileView,
  Loading,
  PageHead,
  StatusBadge,
  fmtTime,
  useLoad,
} from "../ui";

export default function RunDetail() {
  const { runId = "" } = useParams();
  const { lastEvent } = useSSE({ types: ["workflow", "task"], max: 1 });
  const run = useLoad(() => getRun(runId), [runId, lastEvent?.id ?? 0], 10000);
  const [viewing, setViewing] = useState<{ path: string; text: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const openFile = async (path: string) => {
    const sessionId = run.data?.session_id;
    if (!sessionId) return;
    setErr(null);
    try {
      setViewing({ path, text: await readWorkspaceFile(sessionId, path) });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const r = run.data;

  return (
    <>
      <PageHead zh={`工作流运行 ${runId}`} en="Workflow run">
        {r?.status === "running" && (
          <button className="danger" onClick={() => cancelRun(runId).then(run.reload)}>
            取消运行
          </button>
        )}
        <Link to="/workflows">
          <button className="ghost">返回列表</button>
        </Link>
      </PageHead>

      <ErrorNote error={run.error} />
      <ErrorNote error={err} />
      {run.loading && !r && <Loading />}

      {r && (
        <>
          <div className="card">
            <dl className="kv">
              <dt>工作流</dt>
              <dd>{r.workflow_id}</dd>
              <dt>状态</dt>
              <dd>
                <StatusBadge status={r.status} />
              </dd>
              <dt>变量</dt>
              <dd>
                {Object.entries(r.variables)
                  .map(([k, v]) => `${k}=${v}`)
                  .join("  ") || "—"}
              </dd>
              <dt>会话</dt>
              <dd>
                {r.session_id ? (
                  <Link to={`/sessions/${r.session_id}`}>{r.session_id}（查看工作区文件）</Link>
                ) : (
                  "—"
                )}
              </dd>
              <dt>来源</dt>
              <dd>{r.source}</dd>
              <dt>开始</dt>
              <dd>{fmtTime(r.started_at)}</dd>
              <dt>结束</dt>
              <dd>{fmtTime(r.finished_at)}</dd>
              {r.error && (
                <>
                  <dt>错误</dt>
                  <dd style={{ color: "var(--red)" }}>{r.error}</dd>
                </>
              )}
            </dl>
          </div>

          <div className="card">
            <h2>
              步骤进度<span className="en">steps · {r.results.length} done / 当前第 {r.current_step} 步</span>
            </h2>
            <div className="steps">
              {r.results.map((s, i) => (
                <div className={`step ${s.status}`} key={s.step_id + i}>
                  <div className="bullet">{i + 1}</div>
                  <div className="step-body">
                    <div className="step-title">
                      {s.title}
                      <StatusBadge status={s.status} />
                      <Link className="mono" to={`/tasks?id=${s.task_id}`} style={{ fontSize: 12 }}>
                        task {s.task_id}
                      </Link>
                      {s.output_file && (
                        <button className="small ghost" onClick={() => openFile(s.output_file!)}>
                          查看 {s.output_file}
                        </button>
                      )}
                    </div>
                    {s.summary && <div className="step-summary">{s.summary}</div>}
                  </div>
                </div>
              ))}
              {r.status === "running" && (
                <div className="step running">
                  <div className="bullet">{r.results.length + 1}</div>
                  <div className="step-body">
                    <div className="step-title">
                      运行中… <StatusBadge status="running" />
                    </div>
                  </div>
                </div>
              )}
              {r.results.length === 0 && r.status !== "running" && <Empty text="没有步骤结果" />}
            </div>
          </div>

          {viewing && (
            <div className="card">
              <h2>
                {viewing.path}
                <span className="en">workspace file</span>
                <button className="small ghost" style={{ marginLeft: 12 }} onClick={() => setViewing(null)}>
                  关闭
                </button>
              </h2>
              <FileView path={viewing.path} text={viewing.text} />
            </div>
          )}
        </>
      )}
    </>
  );
}
