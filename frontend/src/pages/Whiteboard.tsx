import { useState } from "react";
import { Link } from "react-router-dom";
import {
  addTopic,
  createBoard,
  expireTopic,
  listBoards,
  listTopics,
  whiteboardKickoff,
  whiteboardTick,
} from "../api";
import { useSSE } from "../useSSE";
import { Empty, ErrorNote, Loading, PageHead, StatusBadge, ago, useLoad } from "../ui";

export default function Whiteboard() {
  const { lastEvent } = useSSE({ types: ["whiteboard", "topic_pool"], max: 1 });
  const boards = useLoad(() => listBoards(undefined, 50), [lastEvent?.id ?? 0], 20000);
  const topics = useLoad(() => listTopics("pending"), [lastEvent?.id ?? 0], 30000);

  const [topic, setTopic] = useState("");
  const [question, setQuestion] = useState("");
  const [maxCards, setMaxCards] = useState(5);
  const [asBoard, setAsBoard] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const submit = async () => {
    if (!topic.trim()) return;
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      if (asBoard) {
        const b = await createBoard(topic.trim(), question.trim(), maxCards);
        setNote(`已直接开板：${b.id}`);
        boards.reload();
      } else {
        await addTopic(topic.trim(), question.trim());
        setNote("已加入主题池");
        topics.reload();
      }
      setTopic("");
      setQuestion("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: () => Promise<unknown>, label: string) => {
    setErr(null);
    try {
      await fn();
      setNote(`${label} 完成`);
      boards.reload();
      topics.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>
      <PageHead zh="白板" en="Whiteboard">
        <button className="ghost" onClick={() => act(whiteboardKickoff, "开板（kickoff）")}>
          从主题池开板
        </button>
        <button className="ghost" onClick={() => act(whiteboardTick, "推进（tick）")}>
          手动推进
        </button>
      </PageHead>
      {note && <div className="ok-note">{note}</div>}
      <ErrorNote error={err} />

      <div className="card">
        <h2>
          新增主题<span className="en">add topic</span>
        </h2>
        <div className="form-row">
          <label className="field grow">
            <span className="lbl">主题 Topic</span>
            <input style={{ width: "100%" }} value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="例如：AI 算力供需" />
          </label>
          <label className="field grow">
            <span className="lbl">问题 Question（可选）</span>
            <input style={{ width: "100%" }} value={question} onChange={(e) => setQuestion(e.target.value)} />
          </label>
          {asBoard && (
            <label className="field">
              <span className="lbl">最大卡片数</span>
              <input
                type="number"
                min={1}
                max={12}
                style={{ width: 80 }}
                value={maxCards}
                onChange={(e) => setMaxCards(Math.max(1, Math.min(12, Number(e.target.value) || 5)))}
              />
            </label>
          )}
          <label className="field">
            <span className="lbl">直接开板</span>
            <input type="checkbox" checked={asBoard} onChange={(e) => setAsBoard(e.target.checked)} />
          </label>
          <button onClick={submit} disabled={busy || !topic.trim()}>
            {asBoard ? "创建白板" : "加入主题池"}
          </button>
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>
            主题池<span className="en">topic pool (pending)</span>
          </h2>
          <ErrorNote error={topics.error} />
          <table className="data">
            <thead>
              <tr>
                <th>主题</th>
                <th>来源</th>
                <th>分数</th>
                <th>加入</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {(topics.data ?? []).map((t) => (
                <tr key={t.id}>
                  <td>
                    {t.topic}
                    {t.question && <div className="faint">{t.question}</div>}
                  </td>
                  <td className="dim">{t.source}</td>
                  <td className="mono">{t.score.toFixed(1)}</td>
                  <td className="dim nowrap">{ago(t.created_at)}</td>
                  <td>
                    <button className="small danger" onClick={() => expireTopic(t.id).then(topics.reload)}>
                      作废
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {topics.data?.length === 0 && <Empty text="主题池为空" />}
        </div>

        <div className="card">
          <h2>
            白板列表<span className="en">boards</span>
          </h2>
          <ErrorNote error={boards.error} />
          {boards.loading && !boards.data && <Loading />}
          <table className="data">
            <thead>
              <tr>
                <th>主题</th>
                <th>状态</th>
                <th>卡片</th>
                <th>日期</th>
                <th>更新</th>
              </tr>
            </thead>
            <tbody>
              {(boards.data ?? []).map((b) => (
                <tr key={b.id}>
                  <td>
                    <Link to={`/whiteboard/${b.id}`}>{b.topic}</Link>
                    {b.question && <div className="faint">{b.question}</div>}
                  </td>
                  <td>
                    <StatusBadge status={b.status} />
                  </td>
                  <td className="mono">
                    {b.n_cards ?? 0}/{b.max_cards}
                  </td>
                  <td className="dim mono">{b.work_date}</td>
                  <td className="dim nowrap">{ago(b.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {boards.data?.length === 0 && <Empty text="还没有白板" />}
        </div>
      </div>
    </>
  );
}
