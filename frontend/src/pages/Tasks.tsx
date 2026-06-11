import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Task, cancelTask, getTask, listTasks } from "../api";
import { useSSE } from "../useSSE";
import { Empty, ErrorNote, Loading, PageHead, StatusBadge, fmtTime, useLoad } from "../ui";

const STATUSES = ["", "queued", "running", "completed", "failed", "rate_limited", "cancelled", "expired"];
const SOURCES = ["", "api", "workflow", "whiteboard", "mailbox", "research", "daily", "obsidian", "mcp", "test"];

export default function Tasks() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const hand = params.get("hand") ?? "";
  const source = params.get("source") ?? "";
  const selectedId = params.get("id");

  const { lastEvent } = useSSE({ types: ["task"], max: 1 });
  const rows = useLoad(
    () => listTasks({ status: status || undefined, hand: hand || undefined, source: source || undefined, limit: 200 }),
    [status, hand, source, lastEvent?.id ?? 0],
  );

  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const hands = Array.from(new Set((rows.data ?? []).map((t) => t.hand ?? t.requested_hand))).sort();

  return (
    <>
      <PageHead zh="任务" en="Tasks" />

      <div className="filter-bar">
        <select value={status} onChange={(e) => setFilter("status", e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s === "" ? "全部状态" : s}
            </option>
          ))}
        </select>
        <select value={source} onChange={(e) => setFilter("source", e.target.value)}>
          {SOURCES.map((s) => (
            <option key={s} value={s}>
              {s === "" ? "全部来源" : s}
            </option>
          ))}
        </select>
        <select value={hand} onChange={(e) => setFilter("hand", e.target.value)}>
          <option value="">全部执行手</option>
          {hands.map((h) => (
            <option key={h} value={h}>
              {h}
            </option>
          ))}
        </select>
        <button className="ghost" onClick={rows.reload}>
          刷新
        </button>
        <span className="faint">{rows.data ? `${rows.data.length} 条` : ""}</span>
      </div>

      <div className="card">
        <ErrorNote error={rows.error} />
        {rows.loading && !rows.data && <Loading />}
        <table className="data">
          <thead>
            <tr>
              <th>ID</th>
              <th>状态</th>
              <th>执行手</th>
              <th>来源</th>
              <th>错误</th>
              <th>创建时间</th>
              <th>结束时间</th>
            </tr>
          </thead>
          <tbody>
            {(rows.data ?? []).map((t) => (
              <tr key={t.id} className="clickable" onClick={() => setFilter("id", t.id)}>
                <td className="mono">{t.id}</td>
                <td>
                  <StatusBadge status={t.status} />
                </td>
                <td className="mono">
                  {t.hand ?? t.requested_hand}
                  {t.hand && t.hand !== t.requested_hand && (
                    <span className="faint"> (请求 {t.requested_hand})</span>
                  )}
                </td>
                <td className="dim">{t.source}</td>
                <td className="dim ellipsis">{t.error ?? ""}</td>
                <td className="dim mono nowrap">{fmtTime(t.created_at)}</td>
                <td className="dim mono nowrap">{fmtTime(t.finished_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.data?.length === 0 && <Empty text="没有符合条件的任务" />}
      </div>

      {selectedId && <TaskDrawer taskId={selectedId} onClose={() => setFilter("id", "")} />}
    </>
  );
}

function TaskDrawer({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const { lastEvent } = useSSE({ types: ["task"], max: 1 });

  useEffect(() => {
    getTask(taskId)
      .then((t) => {
        setTask(t);
        setError(null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [taskId, lastEvent?.id]);

  const doCancel = async () => {
    setCancelling(true);
    try {
      await cancelTask(taskId);
      setTask(await getTask(taskId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCancelling(false);
    }
  };

  const cancellable = task && (task.status === "queued" || task.status === "running");

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer">
        <div className="drawer-head">
          <h2 className="mono">任务 {taskId}</h2>
          <div className="page-actions">
            {cancellable && (
              <button className="danger" onClick={doCancel} disabled={cancelling}>
                取消任务
              </button>
            )}
            <button className="ghost" onClick={onClose}>
              关闭
            </button>
          </div>
        </div>
        <ErrorNote error={error} />
        {!task && !error && <Loading />}
        {task && (
          <>
            <dl className="kv">
              <dt>状态</dt>
              <dd>
                <StatusBadge status={task.status} />
              </dd>
              <dt>执行手</dt>
              <dd>
                {task.hand ?? "—"} (请求: {task.requested_hand}
                {task.tried && task.tried.length > 0 ? `; 尝试: ${task.tried.join(" → ")}` : ""})
              </dd>
              <dt>模型</dt>
              <dd>{task.model ?? "—"}</dd>
              <dt>来源</dt>
              <dd>{task.source}</dd>
              <dt>退出码</dt>
              <dd>{task.exit_code ?? "—"}</dd>
              <dt>会话</dt>
              <dd>{task.session_id ? <Link to={`/sessions/${task.session_id}`}>{task.session_id}</Link> : "—"}</dd>
              <dt>工作流运行</dt>
              <dd>
                {task.parent_run_id ? (
                  <Link to={`/workflows/runs/${task.parent_run_id}`}>{task.parent_run_id}</Link>
                ) : (
                  "—"
                )}
              </dd>
              <dt>工作目录</dt>
              <dd>{task.workspace_dir || "—"}</dd>
              <dt>产物</dt>
              <dd>{task.artifacts && task.artifacts.length > 0 ? task.artifacts.join(", ") : "—"}</dd>
            </dl>

            <h2 style={{ marginTop: 18 }}>提示词 Prompt</h2>
            <pre>{task.prompt}</pre>

            {task.error && (
              <>
                <h2>错误 Error</h2>
                <pre style={{ borderColor: "rgba(240,86,106,.4)" }}>{task.error}</pre>
              </>
            )}

            <h2>输出 Output</h2>
            <pre>{task.output || "（空）"}</pre>
          </>
        )}
      </div>
    </>
  );
}
