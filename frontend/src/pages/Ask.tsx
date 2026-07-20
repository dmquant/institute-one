import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AskDoneTask,
  AskStreamFrame,
  askStream,
  askSync,
  listAnalysts,
  listHands,
} from "../api";
import { ErrorNote, PageHead, StatusBadge, useLoad } from "../ui";

// One transcript line: stdout renders normally, stderr in red, status greyed.
interface Line {
  kind: "stdout" | "stderr" | "status";
  text: string;
}

export default function Ask() {
  const analysts = useLoad(listAnalysts, []);
  const hands = useLoad(listHands, []);

  const [prompt, setPrompt] = useState("");
  const [analystId, setAnalystId] = useState("");
  const [hand, setHand] = useState("");
  const [streaming, setStreaming] = useState(true); // fallback switch: off = 同步 /api/ask
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [doneTask, setDoneTask] = useState<AskDoneTask | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const outRef = useRef<HTMLDivElement | null>(null);

  // follow the tail while output grows
  useEffect(() => {
    const el = outRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  useEffect(() => () => abortRef.current?.abort(), []); // page unmount stops reading

  const run = async () => {
    if (!prompt.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setLines([]);
    setDoneTask(null);
    const body = {
      prompt: prompt.trim(),
      analyst_id: analystId || null,
      hand: hand || null,
    };
    try {
      if (streaming) {
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        const onFrame = (f: AskStreamFrame) => {
          if (f.type === "done") {
            setDoneTask(f.task);
            return;
          }
          setLines((prev) => {
            // merge consecutive stdout chunks into the last line-block to keep
            // the DOM small on chatty hands (one element per chunk gets slow)
            const last = prev[prev.length - 1];
            if (last && last.kind === f.type && f.type === "stdout") {
              const next = prev.slice(0, -1);
              next.push({ kind: last.kind, text: last.text + f.text });
              return next;
            }
            return [...prev, { kind: f.type, text: f.text }];
          });
        };
        // askStream rejects if the stream ends without a done frame, so an
        // incomplete response surfaces in the catch below instead of silently
        // clearing busy with no result card
        const done = await askStream(body, onFrame, ctrl.signal);
        if (done.status === "failed" && done.error) setErr(done.error);
      } else {
        setLines([{ kind: "status", text: "同步模式：等待任务完成…" }]);
        const task = await askSync(body);
        setLines(task.output ? [{ kind: "stdout", text: task.output }] : []);
        setDoneTask({
          id: task.id,
          status: task.status,
          hand: task.hand,
          exit_code: task.exit_code,
          error: task.error,
          output: task.output,
        });
        if (task.error) setErr(task.error);
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setErr(e instanceof Error ? e.message : String(e));
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
    }
  };

  const stopReading = () => abortRef.current?.abort();

  return (
    <>
      <PageHead zh="即问" en="Ask" />

      <div className="card">
        <div className="form-row">
          <label className="field grow">
            <span className="lbl">提示词 Prompt</span>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="一次性提问；选择分析师则以其人设作答"
              rows={4}
            />
          </label>
        </div>
        <div className="form-row">
          <label className="field">
            <span className="lbl">分析师（可选）</span>
            <select value={analystId} onChange={(e) => setAnalystId(e.target.value)}>
              <option value="">— 不指定 —</option>
              {(analysts.data ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.emoji} {a.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="lbl">执行手（可选）</span>
            <select value={hand} onChange={(e) => setHand(e.target.value)}>
              <option value="">— 默认 —</option>
              {(hands.data ?? []).map((h) => (
                <option key={h.name} value={h.name} disabled={!h.available}>
                  {h.name}
                  {!h.available ? "（不可用）" : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="field" style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 10 }}>
            <input
              type="checkbox"
              style={{ width: "auto" }}
              checked={streaming}
              onChange={(e) => setStreaming(e.target.checked)}
            />
            <span className="lbl" style={{ margin: 0 }}>
              流式输出（关闭则用同步 /api/ask）
            </span>
          </label>
          <button onClick={run} disabled={busy || !prompt.trim()}>
            {busy ? "运行中…" : "提问"}
          </button>
          {busy && streaming && (
            <button className="ghost" onClick={stopReading} title="只断开读取；任务在后台继续跑，结果在任务页可查">
              停止读取
            </button>
          )}
        </div>
      </div>

      <ErrorNote error={err} />

      {(lines.length > 0 || doneTask || busy) && (
        <div className="card">
          <h2>
            输出<span className="en">output {busy ? "· streaming" : ""}</span>
          </h2>
          <div className="ask-out" ref={outRef}>
            {lines.map((l, i) => (
              <span key={i} className={`ask-line ${l.kind}`}>
                {l.text}
              </span>
            ))}
            {lines.length === 0 && busy && <span className="ask-line status">等待首帧输出…</span>}
          </div>
          {doneTask && (
            <div className="form-row" style={{ marginTop: 10, alignItems: "center" }}>
              <StatusBadge status={doneTask.status} />
              {doneTask.hand && <span className="mono dim">{doneTask.hand}</span>}
              {doneTask.exit_code !== null && <span className="faint mono">exit {doneTask.exit_code}</span>}
              {doneTask.id && (
                <Link to={`/tasks?id=${doneTask.id}`} className="mono">
                  查看任务 {doneTask.id} →
                </Link>
              )}
            </div>
          )}
        </div>
      )}
    </>
  );
}
