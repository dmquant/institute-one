import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { createThread, listAnalysts, listThreads } from "../api";
import { useSSE } from "../useSSE";
import { Empty, ErrorNote, Loading, PageHead, StatusBadge, ago, useLoad } from "../ui";

export default function Mailbox() {
  const navigate = useNavigate();
  const { lastEvent } = useSSE({ types: ["mailbox"], max: 1 });
  const [statusFilter, setStatusFilter] = useState("");
  const threads = useLoad(
    () => listThreads(statusFilter || undefined, 100),
    [statusFilter, lastEvent?.id ?? 0],
    20000,
  );
  const analysts = useLoad(listAnalysts, []);

  const [subject, setSubject] = useState("");
  const [analystId, setAnalystId] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const analystName = (id: string) => {
    const a = (analysts.data ?? []).find((x) => x.id === id);
    return a ? `${a.emoji} ${a.name}` : id;
  };

  const submit = async () => {
    if (!subject.trim() || !analystId || !body.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const t = await createThread(subject.trim(), analystId, body.trim());
      navigate(`/mailbox/${t.id}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHead zh="信箱" en="Mailbox" />

      <div className="card">
        <h2>
          新建线程<span className="en">new thread</span>
        </h2>
        <ErrorNote error={err} />
        <div className="form-row">
          <label className="field grow">
            <span className="lbl">主题 Subject</span>
            <input style={{ width: "100%" }} value={subject} onChange={(e) => setSubject(e.target.value)} />
          </label>
          <label className="field">
            <span className="lbl">分析师 Analyst</span>
            <select value={analystId} onChange={(e) => setAnalystId(e.target.value)}>
              <option value="">选择分析师…</option>
              {(analysts.data ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.emoji} {a.name} ({a.name_en})
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="field">
          <span className="lbl">正文 Body</span>
          <textarea value={body} onChange={(e) => setBody(e.target.value)} placeholder="给分析师的留言…" />
        </label>
        <button onClick={submit} disabled={busy || !subject.trim() || !analystId || !body.trim()}>
          {busy ? "发送中…" : "发送并派发"}
        </button>
      </div>

      <div className="card">
        <h2>
          线程列表<span className="en">threads</span>
        </h2>
        <div className="filter-bar">
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">全部</option>
            <option value="open">开启</option>
            <option value="closed">已关闭</option>
          </select>
          <button className="ghost" onClick={threads.reload}>
            刷新
          </button>
        </div>
        <ErrorNote error={threads.error} />
        {threads.loading && !threads.data && <Loading />}
        <table className="data">
          <thead>
            <tr>
              <th>主题</th>
              <th>分析师</th>
              <th>状态</th>
              <th>消息数</th>
              <th>更新</th>
            </tr>
          </thead>
          <tbody>
            {(threads.data ?? []).map((t) => (
              <tr key={t.id}>
                <td>
                  <Link to={`/mailbox/${t.id}`}>{t.subject}</Link>
                </td>
                <td>{analystName(t.analyst_id)}</td>
                <td>
                  <StatusBadge status={t.status} />
                </td>
                <td className="mono">{t.n_messages ?? "—"}</td>
                <td className="dim nowrap">{ago(t.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {threads.data?.length === 0 && <Empty text="还没有线程" />}
      </div>
    </>
  );
}
