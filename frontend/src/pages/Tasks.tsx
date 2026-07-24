import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { cancelTask, getTask, listTasks, type Task } from "../api";
import { useSSE } from "../useSSE";
import { Empty, ErrorNote, Loading, PageHead, StatusBadge, fmtTime, useLoad } from "../ui";

const KNOWN_STATUSES = [
  "queued",
  "running",
  "completed",
  "failed",
  "overcommitted",
  "rate_limited",
  "cancelled",
  "expired",
];
const STATUS_LABELS: Record<string, string> = {
  queued: "排队",
  running: "运行中",
  completed: "完成",
  failed: "失败",
  overcommitted: "过载拒绝",
  rate_limited: "限流",
  cancelled: "已取消",
  expired: "超时",
};
const SOURCES = ["", "api", "workflow", "whiteboard", "mailbox", "research", "daily", "obsidian", "mcp", "test"];

type TaskWithRouting = Task & {
  fallback_chain?: string[] | null;
  lineage_root?: string | null;
};

export default function Tasks() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const hand = params.get("hand") ?? "";
  const source = params.get("source") ?? "";
  const selectedId = params.get("id");
  const [expandedErrors, setExpandedErrors] = useState<Set<string>>(() => new Set());

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

  const hands = Array.from(
    new Set([...(rows.data ?? []).map((t) => t.hand ?? t.requested_hand), ...(hand ? [hand] : [])]),
  ).sort();
  const unknownStatuses = Array.from(
    new Set([...(rows.data ?? []).map((task) => String(task.status)), status].filter(Boolean)),
  ).filter((value) => !KNOWN_STATUSES.includes(value));
  const statusOptions = ["", ...KNOWN_STATUSES, ...unknownStatuses];

  const toggleError = (taskId: string) => {
    setExpandedErrors((current) => {
      const next = new Set(current);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  };

  return (
    <>
      <PageHead zh="任务" en="Tasks" />

      <div className="filter-bar">
        <span className="faint" style={{ fontSize: 12 }}>
          状态
        </span>
        {statusOptions.map((value) => {
          const active = status === value;
          return (
            <button
              aria-pressed={active}
              className="small ghost"
              key={value || "all"}
              onClick={() => setFilter("status", value)}
              style={
                active
                  ? {
                      background: "var(--accent-soft)",
                      borderColor: "var(--accent)",
                      color: "var(--accent)",
                    }
                  : undefined
              }
              title={value && !(value in STATUS_LABELS) ? `后端返回的未知状态：${value}` : undefined}
            >
              {value ? (STATUS_LABELS[value] ?? value) : "全部"}
            </button>
          );
        })}
      </div>
      <div className="filter-bar">
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
        {rows.data && rows.data.length > 0 && (
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
              {rows.data.map((t) => {
                const errorExpanded = expandedErrors.has(t.id);
                return (
                  <tr key={t.id} className="clickable" onClick={() => setFilter("id", t.id)}>
                    <td className="mono">{t.id}</td>
                    <td>
                      <StatusBadge status={String(t.status)} />
                    </td>
                    <td className="mono">
                      {t.hand ?? t.requested_hand}
                      {t.hand && t.hand !== t.requested_hand && (
                        <span className="faint"> (请求 {t.requested_hand})</span>
                      )}
                    </td>
                    <td className="dim">{t.source}</td>
                    <td className="dim" style={{ maxWidth: 360 }}>
                      {t.error && (
                        <>
                          <div
                            title={errorExpanded ? undefined : t.error}
                            style={{
                              overflow: errorExpanded ? "visible" : "hidden",
                              textOverflow: errorExpanded ? undefined : "ellipsis",
                              whiteSpace: errorExpanded ? "pre-wrap" : "nowrap",
                              wordBreak: errorExpanded ? "break-word" : undefined,
                            }}
                          >
                            {t.error}
                          </div>
                          {t.status === "failed" && (
                            <button
                              aria-expanded={errorExpanded}
                              className="small ghost"
                              onClick={(event) => {
                                event.stopPropagation();
                                toggleError(t.id);
                              }}
                              style={{ marginTop: 4 }}
                            >
                              {errorExpanded ? "收起错误" : "展开错误"}
                            </button>
                          )}
                        </>
                      )}
                    </td>
                    <td className="dim mono nowrap">{fmtTime(t.created_at)}</td>
                    <td className="dim mono nowrap">{fmtTime(t.finished_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {rows.data?.length === 0 && !rows.loading && <Empty text="没有符合条件的任务" />}
      </div>

      {selectedId && <TaskDrawer key={selectedId} taskId={selectedId} onClose={() => setFilter("id", "")} />}
    </>
  );
}

function TaskDrawer({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const [task, setTask] = useState<TaskWithRouting | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const { lastEvent } = useSSE({ types: ["task"], max: 1 });

  useEffect(() => {
    setTask(null);
    setError(null);
  }, [taskId]);

  useEffect(() => {
    let alive = true;
    getTask(taskId)
      .then((t) => {
        if (alive) {
          setTask(t as TaskWithRouting);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [taskId, lastEvent?.id]);

  const doCancel = async () => {
    setCancelling(true);
    try {
      await cancelTask(taskId);
      setTask((await getTask(taskId)) as TaskWithRouting);
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
              <dt>重试链根</dt>
              <dd className="mono">
                {task.lineage_root ? (
                  <Link to={`/tasks?id=${encodeURIComponent(task.lineage_root)}`}>{task.lineage_root}</Link>
                ) : (
                  "—（原始任务）"
                )}
              </dd>
              <dt>回退链</dt>
              <dd className="mono">
                {task.fallback_chain === undefined || task.fallback_chain === null
                  ? "注册表默认策略"
                  : task.fallback_chain.length > 0
                    ? task.fallback_chain.join(" → ")
                    : "无（仅请求执行手）"}
              </dd>
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
