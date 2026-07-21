import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { MailMessage, closeThread, getThread, listAnalysts, replyThread } from "../api";
import { useSSE } from "../useSSE";
import { Empty, ErrorNote, Loading, PageHead, StatusBadge, fmtTime, useLoad } from "../ui";

export default function ThreadDetail() {
  const { threadId = "" } = useParams();
  const { lastEvent } = useSSE({ types: ["mailbox", "task"], max: 1 });
  const thread = useLoad(() => getThread(threadId), [threadId, lastEvent?.id ?? 0], 8000);
  const analysts = useLoad(listAnalysts, []);

  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const analystName = (id: string) => {
    const a = (analysts.data ?? []).find((x) => x.id === id);
    return a ? `${a.emoji} ${a.name}` : id;
  };

  const send = async () => {
    if (!body.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await replyThread(threadId, body.trim());
      setBody("");
      thread.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const t = thread.data;
  const dispatches = t?.messages.filter((m) => m.kind === "dispatch") ?? [];
  const pendingDispatches = dispatches.filter((m) => m.status === "pending").length;
  const latestDispatch = dispatches[dispatches.length - 1];

  return (
    <>
      <PageHead zh={t ? `线程 · ${t.subject}` : "线程"} en={`thread ${threadId}`}>
        {t?.status === "open" && (
          <button className="ghost" onClick={() => closeThread(threadId).then(thread.reload)}>
            关闭线程
          </button>
        )}
        <Link to="/mailbox">
          <button className="ghost">返回信箱</button>
        </Link>
      </PageHead>

      <ErrorNote error={thread.error} />
      {thread.loading && !t && <Loading />}

      {t && (
        <>
          <div className="card">
            <dl className="kv">
              <dt>分析师</dt>
              <dd>{analystName(t.analyst_id)}</dd>
              <dt>状态</dt>
              <dd>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <StatusBadge status={t.status} />
                  {pendingDispatches > 0 && (
                    <span
                      className="badge"
                      style={{
                        color: "var(--amber)",
                        borderColor: "rgba(232, 179, 57, 0.55)",
                        background: "rgba(232, 179, 57, 0.13)",
                      }}
                    >
                      派发中{pendingDispatches > 1 ? ` ${pendingDispatches}` : ""}
                    </span>
                  )}
                  {latestDispatch?.status === "failed" && (
                    <span className="badge st-failed">最新派发失败</span>
                  )}
                </span>
              </dd>
              <dt>创建</dt>
              <dd>{fmtTime(t.created_at)}</dd>
            </dl>
          </div>

          <div className="card">
            <h2>
              对话<span className="en">messages</span>
            </h2>
            <div className="chat">
              {t.messages.map((m) => (
                <Message key={m.id} m={m} analystName={analystName} />
              ))}
              {t.messages.length === 0 && <Empty text="暂无消息" />}
            </div>
          </div>

          <div className="card">
            <h2>
              回复<span className="en">reply</span>
            </h2>
            <ErrorNote error={err} />
            <textarea
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder={t.status === "closed" ? "回复将重新打开该线程…" : "输入回复…"}
            />
            <div style={{ marginTop: 8 }}>
              <button onClick={send} disabled={busy || !body.trim()}>
                {busy ? "发送中…" : "发送回复"}
              </button>
            </div>
          </div>
        </>
      )}
    </>
  );
}

function Message({ m, analystName }: { m: MailMessage; analystName: (id: string) => string }) {
  if (m.kind === "dispatch") {
    const attentionStyle =
      m.status === "pending"
        ? {
            color: "var(--amber)",
            borderColor: "rgba(232, 179, 57, 0.6)",
            background: "rgba(232, 179, 57, 0.1)",
          }
        : m.status === "failed"
          ? {
              color: "#ff8b9a",
              borderColor: "rgba(240, 86, 106, 0.65)",
              background: "rgba(240, 86, 106, 0.1)",
            }
          : undefined;

    return (
      <div className="msg dispatch" style={attentionStyle}>
        <span style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
          <strong>
            {m.status === "pending" ? "正在派发" : m.status === "failed" ? "派发失败" : "派发完成"}
          </strong>
          <span>{analystName(m.author)}</span>
          <StatusBadge status={m.status} />
          {m.task_id && (
            <Link className="mono" to={`/tasks?id=${m.task_id}`}>
              task {m.task_id}
            </Link>
          )}
          <span className="faint">{fmtTime(m.created_at)}</span>
        </span>
      </div>
    );
  }
  const mine = m.author === "operator";
  return (
    <div className={`msg ${mine ? "operator" : "analyst"}`}>
      <div className="meta">
        {mine ? "操作员" : analystName(m.author)} · {fmtTime(m.created_at)}
      </div>
      <div className="body">{m.body}</div>
    </div>
  );
}
