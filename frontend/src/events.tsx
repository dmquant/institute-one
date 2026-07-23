// Prefix-grouped live event feed.
//
// Events are grouped by their type prefix (the part before the first "."),
// so new backend event families show up automatically — nothing to maintain
// (the old hand-kept event-type list went blind on every new emit,
// ROUND2-AUDIT-F2). A small label map keeps the common types readable;
// unknown types render verbatim with their raw payload behind <details>.
import { useMemo, useState } from "react";
import type { BusEvent } from "./api";
import { Empty, ago } from "./ui";

export function eventGroup(type: string): string {
  const dot = type.indexOf(".");
  return dot === -1 ? type : type.slice(0, dot);
}

/** Pretty labels for the well-known types; anything else renders as-is. */
const EVENT_LABELS: Record<string, string> = {
  "task.queued": "任务入队",
  "task.running": "任务开始",
  "task.completed": "任务完成",
  "task.failed": "任务失败",
  "task.rate_limited": "任务限流",
  "task.cancelled": "任务取消",
  "task.expired": "任务超时",
  "workflow.started": "工作流开始",
  "workflow.completed": "工作流完成",
  "workflow.failed": "工作流失败",
  "workflow.cancelled": "工作流取消",
  "whiteboard.board_opened": "白板开题",
  "whiteboard.card_completed": "白板卡片完成",
  "whiteboard.board_completed": "白板完成",
  "mailbox.reply": "信箱回复",
  "research.queued": "研究入队",
  "research.completed": "研究完成",
  "research.followups": "研究跟进",
  "analyst_daily.completed": "分析师日报完成",
  "analyst_daily.failed": "分析师日报失败",
  "analyst_daily.sweep_completed": "日报扫尾完成",
  "memory.compacted": "记忆压缩",
  "scorecard.completed": "评分卡完成",
  "market.refreshed": "行情刷新",
  "forecast.created": "预测创建",
  "forecast.settled": "预测结算",
  "thesis.created": "论点创建",
  "thesis.updated": "论点更新",
  "thesis.status_changed": "论点状态变更",
  "topic_pool.added": "选题入池",
  "archive.snapshot": "归档快照",
  "vault.conflict": "知识库冲突",
  "market_thesis_import.completed": "行情论点导入完成",
};

const GROUP_LABELS: Record<string, string> = {
  task: "任务",
  workflow: "工作流",
  whiteboard: "白板",
  mailbox: "信箱",
  research: "研究",
  analyst_daily: "日报",
  memory: "记忆",
  scorecard: "评分卡",
  market: "行情",
  forecast: "预测",
  thesis: "论点",
  topic_pool: "选题池",
  archive: "归档",
  vault: "知识库",
  roadmap: "路线图",
  decision: "决策",
  checklist: "清单",
  card: "卡片",
  factcheck: "事实核查",
  chain: "链",
  book: "账本",
  paper_book: "纸面账本",
  market_thesis_import: "行情导入",
};

function payloadPreview(payload: Record<string, unknown>): string {
  const keys = Object.keys(payload);
  if (keys.length === 0) return "";
  const s = JSON.stringify(payload);
  return s.length > 120 ? s.slice(0, 120) + "…" : s;
}

export function EventFeed({ events, emptyText = "等待事件中…" }: { events: BusEvent[]; emptyText?: string }) {
  const [group, setGroup] = useState<string>("");

  const groups = useMemo(() => {
    const counts = new Map<string, number>();
    for (const e of events) {
      const g = eventGroup(e.type);
      counts.set(g, (counts.get(g) ?? 0) + 1);
    }
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [events]);

  const shown = useMemo(
    () => (group ? events.filter((e) => eventGroup(e.type) === group) : events),
    [events, group],
  );

  // JSON.stringify per row is the hot path of every SSE refresh — memoize the
  // previews alongside the filter so a group toggle doesn't re-stringify.
  const previews = useMemo(() => {
    const map = new Map<number, string>();
    for (const e of shown) map.set(e.id, payloadPreview(e.payload));
    return map;
  }, [shown]);

  return (
    <>
      {groups.length > 1 && (
        <div className="feed-groups">
          <button className={`feed-group ${group === "" ? "sel" : ""}`} onClick={() => setGroup("")}>
            全部 {events.length}
          </button>
          {groups.map(([g, n]) => (
            <button
              key={g}
              className={`feed-group ${group === g ? "sel" : ""}`}
              onClick={() => setGroup(group === g ? "" : g)}
              title={g}
            >
              {GROUP_LABELS[g] ?? g} {n}
            </button>
          ))}
        </div>
      )}
      <div className="feed">
        {shown.map((e) => {
          const label = EVENT_LABELS[e.type];
          const preview = previews.get(e.id) ?? "";
          return (
            <div className="feed-item" key={e.id}>
              <span className="type" title={e.type}>
                {label ?? e.type}
              </span>
              <span className="ref">
                {e.ref_kind}
                {e.ref_id ? `:${e.ref_id}` : ""}
              </span>
              {preview && (
                <details className="payload">
                  <summary>{preview}</summary>
                  <pre>{JSON.stringify(e.payload, null, 2)}</pre>
                </details>
              )}
              <span className="t">{ago(e.created_at)}</span>
            </div>
          );
        })}
        {shown.length === 0 && <Empty text={emptyText} />}
      </div>
    </>
  );
}
