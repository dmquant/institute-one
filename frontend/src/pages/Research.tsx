import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  cancelResearchItem,
  enqueueResearch,
  getResearchItem,
  getResearchLog,
  listResearchQueue,
  readWorkspaceFile,
  researchTick,
  type ResearchItem,
  type ResearchItemDetail,
} from "../api";
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

type ResearchAssociations = {
  thesis_id?: string | null;
  security_id?: string | null;
};

type LinkedResearchItem = ResearchItem & ResearchAssociations;
type LinkedResearchItemDetail = ResearchItemDetail & ResearchAssociations;

export default function Research() {
  const { itemId } = useParams();
  const navigate = useNavigate();
  const { lastEvent } = useSSE({ types: ["research", "workflow"], max: 1 });
  const queue = useLoad(
    () => listResearchQueue(undefined, 100).then((items) => items as LinkedResearchItem[]),
    [lastEvent?.id ?? 0],
    20000,
  );
  const log = useLoad(() => getResearchLog(50), [lastEvent?.id ?? 0], 60000);

  const [topic, setTopic] = useState("");
  const [priority, setPriority] = useState(0);
  const [busy, setBusy] = useState(false);
  const [ticking, setTicking] = useState(false);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const enqueue = async () => {
    if (!topic.trim()) return;
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const r = await enqueueResearch(topic.trim(), priority);
      if ("refused" in r) {
        setNote(`已拒绝（冷却中）：上次完成于 ${r.last_completed_at}。提高优先级（>0）可强制入队。`);
      } else if (r.deduped) {
        setNote(`已在队列中（去重）：${r.id}`);
      } else {
        setNote(`已入队：${r.id}`);
        setTopic("");
      }
      queue.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const tick = async () => {
    setTicking(true);
    setErr(null);
    setNote("正在处理队列（可能需要较长时间）…");
    try {
      const r = await researchTick();
      setNote(r.processed ? `已处理：${r.processed}` : "本次没有可处理的条目（达到日上限 / 已有运行中 / 队列为空）");
      queue.reload();
      log.reload();
    } catch (e) {
      setNote(null);
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setTicking(false);
    }
  };

  const cancel = async (id: string) => {
    setCancellingId(id);
    setErr(null);
    setNote(null);
    try {
      await cancelResearchItem(id);
      setNote(`已取消研究条目：${id}`);
      queue.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setCancellingId(null);
    }
  };

  return (
    <>
      <PageHead zh="深度研究" en="Deep Research">
        <button className="ghost" onClick={tick} disabled={ticking}>
          {ticking ? "处理中…" : "手动处理一条 (tick)"}
        </button>
      </PageHead>
      {note && <div className="ok-note">{note}</div>}
      <ErrorNote error={err} />

      <div className="card">
        <h2>
          入队<span className="en">enqueue</span>
        </h2>
        <div className="form-row">
          <label className="field grow">
            <span className="lbl">题目 / 标的 Topic</span>
            <input
              style={{ width: "100%" }}
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="例如：NVDA 或 “国产替代下的半导体设备”"
            />
          </label>
          <label className="field">
            <span className="lbl">优先级 Priority</span>
            <input
              type="number"
              style={{ width: 90 }}
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value) || 0)}
            />
          </label>
          <button onClick={enqueue} disabled={busy || !topic.trim()}>
            {busy ? "提交中…" : "加入队列"}
          </button>
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>
            研究队列<span className="en">queue</span>
          </h2>
          <ErrorNote error={queue.error} />
          {queue.loading && !queue.data && <Loading />}
          {queue.data && queue.data.length > 0 && (
            <table className="data">
              <thead>
                <tr>
                  <th>题目 / 关联</th>
                  <th>状态</th>
                  <th>优先级</th>
                  <th>创建</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {queue.data.map((it) => (
                  <tr key={it.id}>
                    <td>
                      <Link to={`/research/${it.id}`}>{it.topic}</Link>
                      {(it.thesis_id || it.security_id) && (
                        <div
                          className="faint mono"
                          style={{ display: "flex", flexWrap: "wrap", gap: "2px 10px", fontSize: 11, marginTop: 3 }}
                        >
                          {it.thesis_id && <span title="关联论点">论点 {it.thesis_id}</span>}
                          {it.security_id && <span title="关联标的">标的 {it.security_id}</span>}
                        </div>
                      )}
                      {it.error && (
                        <div className="faint ellipsis" title={it.error}>
                          {it.error}
                        </div>
                      )}
                    </td>
                    <td>
                      <StatusBadge status={it.status} />
                    </td>
                    <td className="mono">{it.priority}</td>
                    <td className="dim nowrap">{ago(it.created_at)}</td>
                    <td>
                      {(it.status === "pending" || it.status === "running") && (
                        <button
                          className="small danger"
                          disabled={cancellingId !== null}
                          onClick={() => cancel(it.id)}
                        >
                          {cancellingId === it.id ? "取消中…" : "取消"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {queue.data?.length === 0 && !queue.loading && <Empty text="队列为空" />}
        </div>

        <div className="card">
          <h2>
            研究日志<span className="en">research log</span>
          </h2>
          <ErrorNote error={log.error} />
          {log.loading && !log.data && <Loading />}
          {log.data && log.data.length > 0 && (
            <table className="data">
              <thead>
                <tr>
                  <th>题目</th>
                  <th>摘要</th>
                  <th>完成于</th>
                </tr>
              </thead>
              <tbody>
                {log.data.map((r) => (
                  <tr key={r.id}>
                    <td className="nowrap">{r.topic}</td>
                    <td className="dim ellipsis" title={r.summary ?? ""}>
                      {r.summary ?? ""}
                    </td>
                    <td className="dim nowrap" title={fmtTime(r.completed_at)}>
                      {ago(r.completed_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {log.data?.length === 0 && !log.loading && <Empty text="还没有完成的研究" />}
        </div>
      </div>

      {itemId && <ItemDetail itemId={itemId} onClose={() => navigate("/research")} />}
    </>
  );
}

function ItemDetail({ itemId, onClose }: { itemId: string; onClose: () => void }) {
  const { lastEvent } = useSSE({ types: ["research", "workflow", "task"], max: 1 });
  const item = useLoad(
    () => getResearchItem(itemId).then((result) => result as LinkedResearchItemDetail),
    [itemId, lastEvent?.id ?? 0],
    15000,
  );
  const [report, setReport] = useState<{ path: string; text: string } | null>(null);
  const [reportErr, setReportErr] = useState<string | null>(null);

  const run = item.data?.run ?? null;

  // try to load the final report: last step result with an output_file
  const finalFile = run?.results
    ? [...run.results].reverse().find((s) => s.output_file)?.output_file ?? null
    : null;
  const sessionId = run?.session_id ?? null;

  useEffect(() => {
    setReport(null);
    setReportErr(null);
    if (!finalFile || !sessionId) return;
    readWorkspaceFile(sessionId, finalFile)
      .then((text) => setReport({ path: finalFile, text }))
      .catch((e: unknown) => setReportErr(e instanceof Error ? e.message : String(e)));
  }, [finalFile, sessionId]);

  const it = item.data;

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer">
        <div className="drawer-head">
          <h2>研究详情 {it ? `· ${it.topic}` : ""}</h2>
          <button className="ghost" onClick={onClose}>
            关闭
          </button>
        </div>
        <ErrorNote error={item.error} />
        {item.loading && !it && <Loading />}
        {it && (
          <>
            <dl className="kv">
              <dt>状态</dt>
              <dd>
                <StatusBadge status={it.status} />
              </dd>
              <dt>优先级</dt>
              <dd>{it.priority}</dd>
              <dt>来源</dt>
              <dd>{it.source}</dd>
              {it.thesis_id && (
                <>
                  <dt>关联论点</dt>
                  <dd className="mono">{it.thesis_id}</dd>
                </>
              )}
              {it.security_id && (
                <>
                  <dt>关联标的</dt>
                  <dd className="mono">{it.security_id}</dd>
                </>
              )}
              <dt>运行</dt>
              <dd>
                {it.run_id ? <Link to={`/workflows/runs/${it.run_id}`}>{it.run_id}</Link> : "—"}
              </dd>
              <dt>开始</dt>
              <dd>{fmtTime(it.started_at)}</dd>
              <dt>结束</dt>
              <dd>{fmtTime(it.finished_at)}</dd>
              {it.error && (
                <>
                  <dt>错误</dt>
                  <dd style={{ color: "var(--red)" }}>{it.error}</dd>
                </>
              )}
            </dl>

            {run && (
              <>
                <h2 style={{ marginTop: 18 }}>运行步骤 Steps</h2>
                <div className="steps">
                  {run.results.map((s, i) => (
                    <div className={`step ${s.status}`} key={s.step_id + i}>
                      <div className="bullet">{i + 1}</div>
                      <div className="step-body">
                        <div className="step-title">
                          {s.title} <StatusBadge status={s.status} />
                        </div>
                        {s.summary && <div className="step-summary">{s.summary}</div>}
                      </div>
                    </div>
                  ))}
                  {run.results.length === 0 && <Empty text="还没有步骤结果" />}
                </div>
              </>
            )}

            <h2 style={{ marginTop: 18 }}>最终报告 Final report</h2>
            <ErrorNote error={reportErr} />
            {report ? (
              <FileView path={report.path} text={report.text} />
            ) : (
              !reportErr && (finalFile ? <Loading /> : <Empty text="暂无报告文件" />)
            )}
          </>
        )}
      </div>
    </>
  );
}
