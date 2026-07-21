import { useState } from "react";
import { Link } from "react-router-dom";
import {
  listEvents,
  listFavorites,
  listResearchQueue,
  listTasks,
  removeFavorite,
} from "../api";
import type { BusEvent, Favorite, FavoriteKind, ResearchItem, TaskRow } from "../api";
import { Empty, ErrorNote, Loading, PageHead, StatusBadge, ago, useLoad } from "../ui";


const DAYS = 30;
const EVENT_PAGE_SIZE = 2000;
const CHART_COLORS = [
  "var(--accent)",
  "var(--green)",
  "var(--amber)",
  "var(--red)",
  "#9b7bf7",
  "var(--grey)",
];

const KIND_LABELS: Record<FavoriteKind, string> = {
  research: "深度研究",
  whiteboard: "白板",
  daily: "日报",
  briefing: "简报",
  thesis: "论点",
  forecast: "预测",
  chain_entity: "产业链实体",
  research_tree: "研究树",
};

const TERMINAL_TASKS = new Set([
  "completed",
  "failed",
  "rate_limited",
  "cancelled",
  "expired",
  "overcommitted",
]);


function localDayKey(value: string | Date): string {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}


function recentDayKeys(): string[] {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  start.setDate(start.getDate() - (DAYS - 1));
  return Array.from({ length: DAYS }, (_, i) => {
    const date = new Date(start);
    date.setDate(start.getDate() + i);
    return localDayKey(date);
  });
}


async function loadRecentEvents(): Promise<BusEvent[]> {
  const firstDay = recentDayKeys()[0];
  const recent: BusEvent[] = [];
  let since = 0;
  for (;;) {
    const page = await listEvents(since, undefined, EVENT_PAGE_SIZE);
    for (const event of page) {
      if (localDayKey(event.created_at) >= firstDay) recent.push(event);
    }
    if (page.length < EVENT_PAGE_SIZE) return recent;
    const next = page[page.length - 1].id;
    if (next <= since) return recent;
    since = next;
  }
}


function favoriteHref(favorite: Favorite): string | null {
  const id = encodeURIComponent(favorite.ref_id);
  switch (favorite.ref_kind) {
    case "research":
      return `/research/${id}`;
    case "whiteboard":
      return `/whiteboard/${id}`;
    case "daily":
    case "briefing":
      return `/workflows/runs/${id}`;
    case "research_tree":
      return `/trees/${id}`;
    case "forecast":
      return `/forecasts#forecast-${id}`;
    default:
      return null;
  }
}


export default function Insights() {
  const favorites = useLoad(listFavorites, [], 30000);
  const events = useLoad(loadRecentEvents);
  const tasks = useLoad(() => listTasks({ limit: 500 }), [], 60000);
  const research = useLoad(() => listResearchQueue(undefined, 500), [], 60000);
  const [removing, setRemoving] = useState<number | null>(null);
  const [removed, setRemoved] = useState<Set<number>>(() => new Set());
  const [removeError, setRemoveError] = useState<string | null>(null);

  const visibleFavorites = (favorites.data ?? []).filter((favorite) => !removed.has(favorite.id));

  const unfavorite = async (favorite: Favorite) => {
    setRemoving(favorite.id);
    setRemoveError(null);
    try {
      await removeFavorite(favorite.ref_kind, favorite.ref_id);
      setRemoved((current) => new Set(current).add(favorite.id));
      favorites.reload();
    } catch (error) {
      setRemoveError(error instanceof Error ? error.message : String(error));
    } finally {
      setRemoving(null);
    }
  };

  return (
    <>
      <PageHead zh="收藏与洞察" en="Favorites · Insights" />

      <div className="card">
        <h2>
          收藏清单<span className="en">favorites</span>
        </h2>
        <ErrorNote error={removeError} />
        <ErrorNote error={favorites.error} />
        {favorites.loading && !favorites.data && <Loading />}
        <table className="data">
          <thead>
            <tr>
              <th>类型</th>
              <th>标题</th>
              <th>状态</th>
              <th>备注</th>
              <th>收藏于</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {visibleFavorites.map((favorite) => {
              const href = favoriteHref(favorite);
              return (
                <tr key={favorite.id}>
                  <td className="mono nowrap">{KIND_LABELS[favorite.ref_kind]}</td>
                  <td title={favorite.title}>
                    {href ? <Link to={href}>{favorite.title}</Link> : favorite.title}
                    <div className="faint mono" style={{ fontSize: 11 }}>
                      {favorite.ref_id}
                      {!href && " · 当前 SPA 暂无详情页"}
                    </div>
                  </td>
                  <td>{favorite.status ? <StatusBadge status={favorite.status} /> : "—"}</td>
                  <td className="dim">{favorite.note || "—"}</td>
                  <td className="dim nowrap">{ago(favorite.created_at)}</td>
                  <td>
                    <button
                      className="small danger"
                      disabled={removing === favorite.id}
                      onClick={() => unfavorite(favorite)}
                    >
                      {removing === favorite.id ? "取消中…" : "取消收藏"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {favorites.data && visibleFavorites.length === 0 && <Empty text="还没有收藏" />}
      </div>

      <div className="card">
        <h2>
          事件活动<span className="en">events by type · last 30 days</span>
        </h2>
        <ErrorNote error={events.error} />
        {events.loading && !events.data && <Loading />}
        {events.data && <EventStackChart events={events.data} />}
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>
            执行手成功率<span className="en">terminal tasks · latest 500</span>
          </h2>
          <ErrorNote error={tasks.error} />
          {tasks.loading && !tasks.data && <Loading />}
          {tasks.data && <TaskSuccessChart tasks={tasks.data} />}
        </div>

        <div className="card">
          <h2>
            研究完成趋势<span className="en">completed research · last 30 days</span>
          </h2>
          <ErrorNote error={research.error} />
          {research.loading && !research.data && <Loading />}
          {research.data && <ResearchTrendChart items={research.data} />}
        </div>
      </div>
    </>
  );
}


function EventStackChart({ events }: { events: BusEvent[] }) {
  const days = recentDayKeys();
  const daySet = new Set(days);
  const buckets = new Map<string, Record<string, number>>();
  const totals = new Map<string, number>();
  for (const day of days) buckets.set(day, {});
  for (const event of events) {
    const day = localDayKey(event.created_at);
    if (!daySet.has(day)) continue;
    const family = event.type || "other";
    const bucket = buckets.get(day);
    if (!bucket) continue;
    bucket[family] = (bucket[family] ?? 0) + 1;
    totals.set(family, (totals.get(family) ?? 0) + 1);
  }
  const top = [...totals.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([family]) => family);
  const topSet = new Set(top);
  const hasOther = [...totals.keys()].some((family) => !topSet.has(family));
  const series = hasOther ? [...top, "other"] : top;
  const values = days.map((day) => {
    const source = buckets.get(day) ?? {};
    const row: Record<string, number> = Object.fromEntries(top.map((family) => [family, source[family] ?? 0]));
    if (hasOther) {
      row.other = Object.entries(source)
        .filter(([family]) => !topSet.has(family))
        .reduce((sum, [, count]) => sum + count, 0);
    }
    return row;
  });
  const dailyTotals = values.map((row) => Object.values(row).reduce((sum, count) => sum + count, 0));
  const max = Math.max(0, ...dailyTotals);
  if (max === 0) return <Empty text="近 30 天没有事件" />;

  const width = 900;
  const height = 245;
  const left = 36;
  const right = 8;
  const topPad = 8;
  const bottom = 30;
  const plotHeight = height - topPad - bottom;
  const step = (width - left - right) / days.length;
  const barWidth = Math.max(3, step - 4);

  return (
    <>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ display: "block", width: "100%", height: "auto" }}>
        <line
          x1={left}
          x2={width - right}
          y1={topPad + plotHeight}
          y2={topPad + plotHeight}
          stroke="var(--border)"
        />
        <text x={left - 6} y={topPad + 8} fill="var(--text-faint)" fontSize="10" textAnchor="end">
          {max}
        </text>
        <text x={left - 6} y={topPad + plotHeight} fill="var(--text-faint)" fontSize="10" textAnchor="end">
          0
        </text>
        {days.map((day, dayIndex) => {
          let stacked = 0;
          const x = left + dayIndex * step + (step - barWidth) / 2;
          return (
            <g key={day}>
              {series.map((family, seriesIndex) => {
                const count = values[dayIndex][family] ?? 0;
                const segmentHeight = (count / max) * plotHeight;
                const y = topPad + plotHeight - ((stacked + count) / max) * plotHeight;
                stacked += count;
                return count > 0 ? (
                  <rect
                    key={family}
                    x={x}
                    y={y}
                    width={barWidth}
                    height={segmentHeight}
                    fill={CHART_COLORS[seriesIndex]}
                    rx={1}
                  >
                    <title>{`${day} · ${family}: ${count}`}</title>
                  </rect>
                ) : null;
              })}
              {(dayIndex % 5 === 0 || dayIndex === days.length - 1) && (
                <text
                  x={x + barWidth / 2}
                  y={height - 8}
                  fill="var(--text-faint)"
                  fontSize="10"
                  textAnchor="middle"
                >
                  {day.slice(5)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="form-row" style={{ marginTop: 6, fontSize: 12 }}>
        {series.map((family, i) => (
          <span className="dim" key={family}>
            <span
              style={{
                background: CHART_COLORS[i],
                borderRadius: 2,
                display: "inline-block",
                height: 9,
                marginRight: 5,
                width: 9,
              }}
            />
            {family}
          </span>
        ))}
        <span className="faint" style={{ marginLeft: "auto" }}>
          共 {events.length} 条
        </span>
      </div>
    </>
  );
}


function TaskSuccessChart({ tasks }: { tasks: TaskRow[] }) {
  const grouped = new Map<string, { total: number; completed: number }>();
  for (const task of tasks) {
    if (!TERMINAL_TASKS.has(task.status)) continue;
    const hand = task.hand || task.requested_hand || "unknown";
    const row = grouped.get(hand) ?? { total: 0, completed: 0 };
    row.total += 1;
    if (task.status === "completed") row.completed += 1;
    grouped.set(hand, row);
  }
  const rows = [...grouped.entries()].sort((a, b) => b[1].total - a[1].total);
  if (rows.length === 0) return <Empty text="还没有终态任务" />;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {rows.map(([hand, counts]) => {
        const rate = counts.completed / counts.total;
        const color = rate >= 0.8 ? "var(--green)" : rate >= 0.5 ? "var(--amber)" : "var(--red)";
        return (
          <div
            key={hand}
            style={{
              alignItems: "center",
              display: "grid",
              gap: 8,
              gridTemplateColumns: "92px minmax(90px, 1fr) 72px",
            }}
          >
            <span className="mono ellipsis" title={hand}>
              {hand}
            </span>
            <span
              style={{
                background: "var(--panel-2)",
                border: "1px solid var(--border)",
                borderRadius: 999,
                height: 12,
                overflow: "hidden",
              }}
              title={`${counts.completed}/${counts.total} completed`}
            >
              <span
                style={{
                  background: color,
                  display: "block",
                  height: "100%",
                  width: `${rate * 100}%`,
                }}
              />
            </span>
            <span className="mono dim" style={{ textAlign: "right" }}>
              {(rate * 100).toFixed(0)}% · {counts.total}
            </span>
          </div>
        );
      })}
    </div>
  );
}


function ResearchTrendChart({ items }: { items: ResearchItem[] }) {
  const days = recentDayKeys();
  const daySet = new Set(days);
  const counts = Object.fromEntries(days.map((day) => [day, 0])) as Record<string, number>;
  for (const item of items) {
    if (item.status !== "completed" || !item.finished_at) continue;
    const day = localDayKey(item.finished_at);
    if (daySet.has(day)) counts[day] += 1;
  }
  const values = days.map((day) => counts[day]);
  const total = values.reduce((sum, count) => sum + count, 0);
  if (total === 0) return <Empty text="近 30 天没有完成的研究" />;

  const width = 560;
  const height = 210;
  const left = 28;
  const right = 8;
  const top = 8;
  const bottom = 30;
  const plotHeight = height - top - bottom;
  const step = (width - left - right) / days.length;
  const barWidth = Math.max(3, step - 3);
  const max = Math.max(...values, 1);
  const points = values
    .map((count, i) => {
      const x = left + i * step + step / 2;
      const y = top + plotHeight - (count / max) * plotHeight;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  return (
    <>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ display: "block", width: "100%", height: "auto" }}>
        <line
          x1={left}
          x2={width - right}
          y1={top + plotHeight}
          y2={top + plotHeight}
          stroke="var(--border)"
        />
        {values.map((count, i) => {
          const x = left + i * step + (step - barWidth) / 2;
          const barHeight = (count / max) * plotHeight;
          return (
            <g key={days[i]}>
              <rect
                x={x}
                y={top + plotHeight - barHeight}
                width={barWidth}
                height={barHeight}
                fill="var(--accent)"
                opacity={0.3}
                rx={1}
              >
                <title>{`${days[i]} · 完成 ${count}`}</title>
              </rect>
              {(i % 5 === 0 || i === days.length - 1) && (
                <text
                  x={x + barWidth / 2}
                  y={height - 8}
                  fill="var(--text-faint)"
                  fontSize="10"
                  textAnchor="middle"
                >
                  {days[i].slice(5)}
                </text>
              )}
            </g>
          );
        })}
        <polyline
          points={points}
          fill="none"
          stroke="var(--accent)"
          strokeLinejoin="round"
          strokeWidth={1.5}
        />
      </svg>
      <div className="faint" style={{ fontSize: 12, textAlign: "right" }}>
        近 30 天完成 {total} 项 · 数据取最近 500 条研究
      </div>
    </>
  );
}
