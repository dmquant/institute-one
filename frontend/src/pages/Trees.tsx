import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { createTree, getTree, listTrees, stopTree } from "../api";
import type { TreeNode } from "../api";
import { useSSE } from "../useSSE";
import {
  Empty,
  ErrorNote,
  Loading,
  PageHead,
  StatusBadge,
  ago,
  fmtTime,
  useLoad,
} from "../ui";

// BFS explore trees (Phase 7). One component, two routes: /trees renders the
// list + create form, /trees/:treeId the layered node view. Live refresh rides
// the existing wake-up contract: tree.* SSE events bump lastEvent, useLoad
// re-GETs (PATCH-NOTES-D4: full render on GET, SSE is only a trigger).
export default function Trees() {
  const { treeId } = useParams();
  return treeId ? <TreeDetail treeId={treeId} /> : <TreeList />;
}

function TreeList() {
  const navigate = useNavigate();
  const { lastEvent } = useSSE({ types: ["tree."], max: 1 });
  const trees = useLoad(() => listTrees(undefined, 50), [lastEvent?.id ?? 0], 20000);

  const [topic, setTopic] = useState("");
  const [maxDepth, setMaxDepth] = useState(2);
  const [maxNodes, setMaxNodes] = useState(12);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const submit = async () => {
    if (!topic.trim()) return;
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const r = await createTree(topic.trim(), maxDepth, maxNodes);
      if ("refused" in r) {
        setNote(`已拒绝：今日建树额度已用完（${r.booked_today}/${r.cap}），明天再来`);
      } else {
        setTopic("");
        navigate(`/trees/${r.id}`);
      }
      trees.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHead zh="研究树" en="Research Trees" />
      {note && <div className="ok-note">{note}</div>}
      <ErrorNote error={err} />

      <div className="card">
        <h2>
          新建探索树<span className="en">create explore tree</span>
        </h2>
        <div className="form-row">
          <label className="field grow">
            <span className="lbl">根主题 Root topic</span>
            <input
              style={{ width: "100%" }}
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="例如：固态电池产业链"
            />
          </label>
          <label className="field">
            <span className="lbl">最大深度 (0-4)</span>
            <input
              type="number"
              min={0}
              max={4}
              style={{ width: 80 }}
              value={maxDepth}
              onChange={(e) => setMaxDepth(Math.max(0, Math.min(4, Number(e.target.value) || 0)))}
            />
          </label>
          <label className="field">
            <span className="lbl">最大节点 (1-50)</span>
            <input
              type="number"
              min={1}
              max={50}
              style={{ width: 80 }}
              value={maxNodes}
              onChange={(e) => setMaxNodes(Math.max(1, Math.min(50, Number(e.target.value) || 12)))}
            />
          </label>
          <button onClick={submit} disabled={busy || !topic.trim()}>
            创建
          </button>
        </div>
        <p className="dim" style={{ fontSize: 12.5, margin: "6px 0 0" }}>
          根节点入队后由 5 分钟一档的 tick 逐层探索（BFS），受每日建树上限与单树并发上限约束。
        </p>
      </div>

      <div className="card">
        <h2>
          树列表<span className="en">trees</span>
        </h2>
        <ErrorNote error={trees.error} />
        {trees.loading && !trees.data && <Loading />}
        <table className="data">
          <thead>
            <tr>
              <th>根主题</th>
              <th>状态</th>
              <th>节点</th>
              <th>深度/上限</th>
              <th>创建</th>
              <th>完成</th>
            </tr>
          </thead>
          <tbody>
            {(trees.data ?? []).map((t) => (
              <tr key={t.id}>
                <td>
                  <Link to={`/trees/${t.id}`}>{t.root_topic}</Link>
                </td>
                <td>
                  <StatusBadge status={t.status} />
                </td>
                <td className="mono">
                  {t.nodes_completed ?? 0}/{t.nodes_total ?? 0}
                </td>
                <td className="dim mono">
                  d≤{t.max_depth} · n≤{t.max_nodes}
                </td>
                <td className="dim nowrap">{ago(t.created_at)}</td>
                <td className="dim nowrap">{t.finished_at ? ago(t.finished_at) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {trees.data?.length === 0 && <Empty text="还没有探索树" />}
      </div>
    </>
  );
}

function TreeDetail({ treeId }: { treeId: string }) {
  const { lastEvent } = useSSE({ types: ["tree."], max: 1 });
  const tree = useLoad(() => getTree(treeId), [treeId, lastEvent?.id ?? 0], 15000);
  const [err, setErr] = useState<string | null>(null);

  const t = tree.data;

  // depth -> nodes, preserving the backend's BFS order (depth, created_at)
  const layers = useMemo(() => {
    const m = new Map<number, TreeNode[]>();
    for (const n of t?.nodes ?? []) {
      const arr = m.get(n.depth);
      if (arr) arr.push(n);
      else m.set(n.depth, [n]);
    }
    return Array.from(m.entries()).sort((a, b) => a[0] - b[0]);
  }, [t?.nodes]);

  const byId = useMemo(() => {
    const m = new Map<string, TreeNode>();
    for (const n of t?.nodes ?? []) m.set(n.id, n);
    return m;
  }, [t?.nodes]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const n of t?.nodes ?? []) c[n.status] = (c[n.status] ?? 0) + 1;
    return c;
  }, [t?.nodes]);

  const stop = async () => {
    setErr(null);
    try {
      await stopTree(treeId);
      tree.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>
      <PageHead zh={t ? `研究树 · ${t.root_topic}` : "研究树"} en={`tree ${treeId}`}>
        {t && (t.status === "pending" || t.status === "exploring") && (
          <button className="danger" onClick={stop}>
            停止探索
          </button>
        )}
        <Link to="/trees">
          <button className="ghost">返回列表</button>
        </Link>
      </PageHead>

      <ErrorNote error={tree.error} />
      <ErrorNote error={err} />
      {tree.loading && !t && <Loading />}

      {t && (
        <>
          <div className="card">
            <dl className="kv">
              <dt>状态</dt>
              <dd>
                <StatusBadge status={t.status} />
              </dd>
              <dt>节点统计</dt>
              <dd>
                {["completed", "running", "pending", "failed", "pruned"]
                  .filter((s) => counts[s])
                  .map((s) => `${s} ${counts[s]}`)
                  .join(" · ") || "无节点"}
              </dd>
              <dt>探索上限</dt>
              <dd className="mono">
                深度 ≤ {t.max_depth} · 节点 ≤ {t.max_nodes}
              </dd>
              <dt>创建</dt>
              <dd>{fmtTime(t.created_at)}</dd>
              <dt>完成</dt>
              <dd>{t.finished_at ? fmtTime(t.finished_at) : "—"}</dd>
            </dl>
            {(t.status === "pending" || t.status === "exploring") && (
              <p className="dim" style={{ fontSize: 12.5, margin: "10px 0 0" }}>
                停止后：pending 节点即刻剪枝，运行中的节点自然收尾（结果保留、不再产出子节点）。
              </p>
            )}
          </div>

          {layers.map(([depth, nodes]) => (
            <div className="card" key={depth}>
              <h2>
                第 {depth} 层{depth === 0 ? "（根）" : ""}
                <span className="en">depth {depth} · {nodes.length} nodes</span>
              </h2>
              {nodes.map((n) => {
                const parent = n.parent_id ? byId.get(n.parent_id) : null;
                return (
                  <div className={`wb-card ${n.status}`} key={n.id}>
                    <div className="head">
                      <strong>{n.topic}</strong>
                      <StatusBadge status={n.status} />
                      {n.task_id && (
                        <Link className="mono" style={{ fontSize: 12 }} to={`/tasks?id=${n.task_id}`}>
                          task {n.task_id}
                        </Link>
                      )}
                      <span className="faint" style={{ marginLeft: "auto" }}>
                        {ago(n.finished_at ?? n.created_at)}
                      </span>
                    </div>
                    {parent && <div className="q">↳ 来自：{parent.topic}</div>}
                    {n.question && <div className="q">问题：{n.question}</div>}
                    {n.summary && (
                      <details>
                        <summary className="faint" style={{ cursor: "pointer", fontSize: 12 }}>
                          结论摘要（点击展开）
                        </summary>
                        <div className="summary">{n.summary}</div>
                      </details>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
          {t.nodes.length === 0 && <Empty text="树里还没有节点" />}
        </>
      )}
    </>
  );
}
