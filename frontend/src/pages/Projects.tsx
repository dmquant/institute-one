import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  archiveProject,
  createProject,
  getProject,
  getProjectDigest,
  listProjects,
} from "../api";
import {
  Empty,
  ErrorNote,
  Loading,
  Markdown,
  PageHead,
  StatusBadge,
  ago,
  fmtTime,
  useLoad,
} from "../ui";

// Research projects (Phase 7): named long-running containers grouping
// research runs / boards / threads / trees. No project events exist on the
// bus, so both views poll instead of riding SSE.
export default function Projects() {
  const { projectId } = useParams();
  return projectId ? <ProjectDetail projectId={projectId} /> : <ProjectList />;
}

function ProjectList() {
  const navigate = useNavigate();
  const projects = useLoad(() => listProjects(undefined, 100), [], 30000);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    if (!name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const p = await createProject(name.trim(), description.trim());
      setName("");
      setDescription("");
      navigate(`/projects/${p.id}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHead zh="项目" en="Projects" />
      <ErrorNote error={err} />

      <div className="card">
        <h2>
          新建项目<span className="en">create project</span>
        </h2>
        <div className="form-row">
          <label className="field grow">
            <span className="lbl">名称 Name（唯一）</span>
            <input
              style={{ width: "100%" }}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：AI 算力供应链跟踪"
            />
          </label>
          <label className="field grow">
            <span className="lbl">描述 Description（可选，支持 Markdown）</span>
            <input style={{ width: "100%" }} value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <button onClick={submit} disabled={busy || !name.trim()}>
            创建
          </button>
        </div>
      </div>

      <div className="card">
        <h2>
          项目列表<span className="en">projects</span>
        </h2>
        <ErrorNote error={projects.error} />
        {projects.loading && !projects.data && <Loading />}
        <table className="data">
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>关联数</th>
              <th>创建</th>
            </tr>
          </thead>
          <tbody>
            {(projects.data ?? []).map((p) => (
              <tr key={p.id}>
                <td>
                  <Link to={`/projects/${p.id}`}>{p.name}</Link>
                  {p.description && <div className="faint ellipsis">{p.description}</div>}
                </td>
                <td>
                  <StatusBadge status={p.status} />
                </td>
                <td className="mono">{p.n_links ?? 0}</td>
                <td className="dim nowrap">{ago(p.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {projects.data?.length === 0 && <Empty text="还没有项目" />}
      </div>
    </>
  );
}

function ProjectDetail({ projectId }: { projectId: string }) {
  const project = useLoad(() => getProject(projectId), [projectId], 30000);
  const digest = useLoad(() => getProjectDigest(projectId), [projectId]);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [archiving, setArchiving] = useState(false);

  const p = project.data;

  const archive = async () => {
    setErr(null);
    setNote(null);
    setArchiving(true);
    try {
      await archiveProject(projectId);
      setNote("项目已归档：历史关联保留，不再接受新关联/新入队");
      project.reload();
      digest.reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setNote("项目已是归档状态（幂等）");
        project.reload();
      } else if (e instanceof ApiError && (e.status === 404 || e.status === 405 || e.status === 501)) {
        setErr("归档端点尚未部署（archive 目前只在 domain 层，API 是 PATCH-NOTES-D5 的后续项）");
      } else {
        setErr(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setArchiving(false);
    }
  };

  return (
    <>
      <PageHead zh={p ? `项目 · ${p.name}` : "项目"} en={`project ${projectId}`}>
        {p?.status === "active" && (
          <button className="danger" onClick={archive} disabled={archiving}>
            {archiving ? "归档中…" : "归档项目"}
          </button>
        )}
        <Link to="/projects">
          <button className="ghost">返回列表</button>
        </Link>
      </PageHead>

      <ErrorNote error={project.error} />
      <ErrorNote error={err} />
      {note && <div className="ok-note">{note}</div>}
      {project.loading && !p && <Loading />}

      {p && (
        <>
          <div className="card">
            <dl className="kv">
              <dt>状态</dt>
              <dd>
                <StatusBadge status={p.status} />
              </dd>
              <dt>描述</dt>
              <dd>{p.description || "—"}</dd>
              <dt>创建</dt>
              <dd>{fmtTime(p.created_at)}</dd>
            </dl>
          </div>

          <div className="grid cols-2">
            <div className="card">
              <h2>
                深度研究<span className="en">research · {p.links.research.length}</span>
              </h2>
              {p.links.research.map((l) => (
                <div className="feed-item" key={`research-${l.ref_id}`}>
                  <Link to={`/research/${l.ref_id}`}>{l.topic ?? l.ref_id}</Link>
                  {l.status && <StatusBadge status={l.status} />}
                  <span className="t">{ago(l.created_at)}</span>
                </div>
              ))}
              {p.links.research.length === 0 && <Empty text="未关联研究" />}
            </div>

            <div className="card">
              <h2>
                白板研讨<span className="en">boards · {p.links.board.length}</span>
              </h2>
              {p.links.board.map((l) => (
                <div className="feed-item" key={`board-${l.ref_id}`}>
                  <Link to={`/whiteboard/${l.ref_id}`}>{l.topic ?? l.ref_id}</Link>
                  {l.status && <StatusBadge status={l.status} />}
                  {l.work_date && <span className="dim mono">{l.work_date}</span>}
                  <span className="t">{ago(l.created_at)}</span>
                </div>
              ))}
              {p.links.board.length === 0 && <Empty text="未关联白板" />}
            </div>

            <div className="card">
              <h2>
                邮件线程<span className="en">threads · {p.links.thread.length}</span>
              </h2>
              {p.links.thread.map((l) => (
                <div className="feed-item" key={`thread-${l.ref_id}`}>
                  <Link to={`/mailbox/${l.ref_id}`}>{l.subject ?? l.ref_id}</Link>
                  {l.status && <StatusBadge status={l.status} />}
                  {l.analyst_id && <span className="dim mono">{l.analyst_id}</span>}
                  <span className="t">{ago(l.created_at)}</span>
                </div>
              ))}
              {p.links.thread.length === 0 && <Empty text="未关联线程" />}
            </div>

            <div className="card">
              <h2>
                研究树<span className="en">trees · {p.links.tree.length}</span>
              </h2>
              {p.links.tree.map((l) => (
                <div className="feed-item" key={`tree-${l.ref_id}`}>
                  <Link to={`/trees/${l.ref_id}`}>{l.root_topic ?? l.ref_id}</Link>
                  {l.status && <StatusBadge status={l.status} />}
                  <span className="t">{ago(l.created_at)}</span>
                </div>
              ))}
              {p.links.tree.length === 0 && <Empty text="未关联研究树" />}
            </div>
          </div>

          <div className="card">
            <h2>
              项目摘要<span className="en">digest.md</span>
              <button className="small ghost" style={{ marginLeft: 12 }} onClick={digest.reload}>
                刷新
              </button>
            </h2>
            <ErrorNote error={digest.error} />
            {digest.loading && !digest.data && <Loading />}
            {digest.data && <Markdown text={digest.data} />}
          </div>
        </>
      )}
    </>
  );
}
