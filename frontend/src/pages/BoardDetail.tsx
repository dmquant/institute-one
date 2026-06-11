import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getBoard, listAnalysts, readWorkspaceFile, stopBoard } from "../api";
import { useSSE } from "../useSSE";
import {
  Empty,
  ErrorNote,
  FileView,
  Loading,
  PageHead,
  StatusBadge,
  ago,
  fmtTime,
  useLoad,
} from "../ui";

export default function BoardDetail() {
  const { boardId = "" } = useParams();
  const { lastEvent } = useSSE({ types: ["whiteboard", "task"], max: 1 });
  const board = useLoad(() => getBoard(boardId), [boardId, lastEvent?.id ?? 0], 15000);
  const analysts = useLoad(listAnalysts, []);
  const [viewing, setViewing] = useState<{ path: string; text: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const analystName = (id: string) => {
    const a = (analysts.data ?? []).find((x) => x.id === id);
    return a ? `${a.emoji} ${a.name}` : id;
  };

  const openFile = async (path: string) => {
    const sessionId = board.data?.session_id;
    if (!sessionId) {
      setErr("该白板没有会话，无法读取文件");
      return;
    }
    setErr(null);
    try {
      setViewing({ path, text: await readWorkspaceFile(sessionId, path) });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const b = board.data;

  return (
    <>
      <PageHead zh={b ? `白板 · ${b.topic}` : "白板"} en={`board ${boardId}`}>
        {b?.status === "active" && (
          <button className="danger" onClick={() => stopBoard(boardId).then(board.reload)}>
            停止白板
          </button>
        )}
        <Link to="/whiteboard">
          <button className="ghost">返回列表</button>
        </Link>
      </PageHead>

      <ErrorNote error={board.error} />
      <ErrorNote error={err} />
      {board.loading && !b && <Loading />}

      {b && (
        <>
          <div className="card">
            <dl className="kv">
              <dt>状态</dt>
              <dd>
                <StatusBadge status={b.status} />
              </dd>
              <dt>总问题</dt>
              <dd>{b.question || "—"}</dd>
              <dt>卡片进度</dt>
              <dd>
                {b.cards.length}/{b.max_cards}
              </dd>
              <dt>工作日</dt>
              <dd>{b.work_date}</dd>
              <dt>会话</dt>
              <dd>{b.session_id ? <Link to={`/sessions/${b.session_id}`}>{b.session_id}</Link> : "—"}</dd>
              <dt>创建</dt>
              <dd>{fmtTime(b.created_at)}</dd>
            </dl>
          </div>

          <div className="card">
            <h2>
              卡片接力<span className="en">cards timeline</span>
            </h2>
            {b.cards.map((c) => (
              <div className={`wb-card ${c.status}`} key={c.id}>
                <div className="head">
                  <span className="mono faint">#{c.idx}</span>
                  <strong>{analystName(c.analyst_id)}</strong>
                  <StatusBadge status={c.status} />
                  {c.task_id && (
                    <Link className="mono" style={{ fontSize: 12 }} to={`/tasks?id=${c.task_id}`}>
                      task {c.task_id}
                    </Link>
                  )}
                  {c.output_file && (
                    <button className="small ghost" onClick={() => openFile(c.output_file!)}>
                      查看全文 {c.output_file}
                    </button>
                  )}
                  <span className="faint" style={{ marginLeft: "auto" }}>
                    {ago(c.finished_at ?? c.created_at)}
                  </span>
                </div>
                {c.question && <div className="q">问题：{c.question}</div>}
                {c.summary && <div className="summary">{c.summary}</div>}
              </div>
            ))}
            {b.cards.length === 0 && <Empty text="还没有卡片" />}
          </div>

          {viewing && (
            <div className="card">
              <h2>
                {viewing.path}
                <span className="en">card file</span>
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
