import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Session,
  getSession,
  listSessions,
  listWorkspaceFiles,
  readWorkspaceFile,
} from "../api";
import {
  Empty,
  ErrorNote,
  FileView,
  Loading,
  PageHead,
  StatusBadge,
  ago,
  fmtBytes,
  fmtTime,
  useLoad,
} from "../ui";

const KINDS = ["", "chat", "workflow", "whiteboard"];

export default function Sessions() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const [kind, setKind] = useState("");
  const sessions = useLoad(() => listSessions(kind || undefined, 200), [kind], 30000);

  return (
    <>
      <PageHead zh="会话" en="Sessions" />

      <div className="filter-bar">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k === "" ? "全部类型" : k}
            </option>
          ))}
        </select>
        <button className="ghost" onClick={sessions.reload}>
          刷新
        </button>
        <span className="faint">{sessions.data ? `${sessions.data.length} 个会话` : ""}</span>
      </div>

      <div className="split">
        <div className="card">
          <h2>
            会话列表<span className="en">sessions</span>
          </h2>
          <ErrorNote error={sessions.error} />
          {sessions.loading && !sessions.data && <Loading />}
          <table className="data">
            <thead>
              <tr>
                <th>标题</th>
                <th>类型</th>
                <th>更新</th>
              </tr>
            </thead>
            <tbody>
              {(sessions.data ?? []).map((s) => (
                <tr
                  key={s.id}
                  className="clickable"
                  onClick={() => navigate(`/sessions/${s.id}`)}
                  style={s.id === sessionId ? { background: "var(--accent-soft)" } : undefined}
                >
                  <td>
                    {s.title || s.id}
                    <div className="faint mono">{s.id}</div>
                  </td>
                  <td>
                    <StatusBadge status={s.kind} />
                  </td>
                  <td className="dim nowrap" title={fmtTime(s.updated_at)}>
                    {ago(s.updated_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {sessions.data?.length === 0 && <Empty text="还没有会话" />}
        </div>

        {sessionId ? (
          <SessionWorkspace sessionId={sessionId} />
        ) : (
          <div className="card">
            <Empty text="从左侧选择一个会话，浏览其工作区文件" />
          </div>
        )}
      </div>
    </>
  );
}

function SessionWorkspace({ sessionId }: { sessionId: string }) {
  const session = useLoad(() => getSession(sessionId), [sessionId]);
  const files = useLoad(() => listWorkspaceFiles(sessionId), [sessionId], 20000);

  const [selected, setSelected] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [fileErr, setFileErr] = useState<string | null>(null);

  // reset the viewer when switching sessions
  useEffect(() => {
    setSelected(null);
    setText(null);
    setFileErr(null);
  }, [sessionId]);

  const open = (path: string) => {
    setSelected(path);
    setText(null);
    setFileErr(null);
    readWorkspaceFile(sessionId, path)
      .then(setText)
      .catch((e: unknown) => setFileErr(e instanceof Error ? e.message : String(e)));
  };

  const s = session.data as Session | null;

  return (
    <div>
      <div className="card">
        <h2>
          {s ? s.title || s.id : "会话"}
          <span className="en">session {sessionId}</span>
        </h2>
        <ErrorNote error={session.error} />
        {s && (
          <dl className="kv">
            <dt>类型</dt>
            <dd>{s.kind}</dd>
            <dt>分析师</dt>
            <dd>{s.analyst_id ?? "—"}</dd>
            <dt>工作区目录</dt>
            <dd>{s.workspace_dir || "—"}</dd>
            <dt>创建</dt>
            <dd>{fmtTime(s.created_at)}</dd>
            <dt>更新</dt>
            <dd>{fmtTime(s.updated_at)}</dd>
          </dl>
        )}
      </div>

      <div className="card">
        <h2>
          工作区文件<span className="en">workspace files · {files.data?.length ?? 0}</span>
          <button className="small ghost" style={{ marginLeft: 12 }} onClick={files.reload}>
            刷新
          </button>
        </h2>
        <ErrorNote error={files.error} />
        {files.loading && !files.data && <Loading />}
        {files.data && files.data.length === 0 && <Empty text="工作区为空" />}
        {files.data && files.data.length > 0 && (
          <div className="file-grid">
            <div className="file-list">
              {files.data.map((f) => (
                <button
                  key={f.path}
                  className={f.path === selected ? "sel" : ""}
                  title={`${fmtBytes(f.size)} · ${fmtTime(f.mtime)}`}
                  onClick={() => open(f.path)}
                >
                  {f.path}
                  <span className="faint"> · {fmtBytes(f.size)}</span>
                </button>
              ))}
            </div>
            <div>
              {!selected && <Empty text="选择左侧文件查看内容" />}
              {selected && (
                <>
                  <div className="dim mono" style={{ marginBottom: 8 }}>
                    {selected}
                  </div>
                  <ErrorNote error={fileErr} />
                  {text === null && !fileErr && <Loading />}
                  {text !== null && <FileView path={selected} text={text} />}
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
