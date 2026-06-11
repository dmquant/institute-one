// Small shared UI helpers — no runtime dependencies.
import { useCallback, useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------- time ----

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso).getTime();
  if (isNaN(d)) return iso;
  const s = Math.max(0, Math.floor((Date.now() - d) / 1000));
  if (s < 60) return `${s}秒前`;
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)}小时前`;
  return `${Math.floor(s / 86400)}天前`;
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** Countdown text for a unix-seconds deadline, e.g. "4分32秒". */
export function countdown(untilUnixSeconds: number, nowMs: number): string {
  const left = Math.max(0, Math.round(untilUnixSeconds - nowMs / 1000));
  if (left <= 0) return "即将解除";
  const m = Math.floor(left / 60);
  const s = left % 60;
  return m > 0 ? `${m}分${s}秒` : `${s}秒`;
}

/** Re-render every `ms` — for live countdowns. Returns current Date.now(). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

// ------------------------------------------------------------ data hook ----

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/** Load data on mount (and when deps change); manual reload(); optional poll. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = [], pollMs?: number): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fnRef
      .current()
      .then((d) => {
        if (alive) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  useEffect(() => {
    if (!pollMs) return;
    const t = window.setInterval(reload, pollMs);
    return () => window.clearInterval(t);
  }, [pollMs, reload]);

  return { data, loading, error, reload };
}

// ------------------------------------------------------------ components ----

const STATUS_ZH: Record<string, string> = {
  queued: "排队",
  running: "运行中",
  completed: "完成",
  failed: "失败",
  rate_limited: "限流",
  cancelled: "已取消",
  expired: "超时",
  pending: "待处理",
  active: "进行中",
  stopped: "已停止",
  open: "开启",
  closed: "已关闭",
  done: "完成",
  used: "已使用",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge st-${status}`} title={status}>
      {STATUS_ZH[status] ?? status}
    </span>
  );
}

export function PageHead({ zh, en, children }: { zh: string; en: string; children?: React.ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{zh}</h1>
        <div className="subtitle">{en}</div>
      </div>
      {children && <div className="page-actions">{children}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="error-note">⚠ {error}</div>;
}

export function Empty({ text = "暂无数据" }: { text?: string }) {
  return <div className="empty">{text}</div>;
}

export function Loading() {
  return <div className="empty">加载中…</div>;
}

// ------------------------------------------------------------- markdown ----

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inlineMd(s: string): string {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

/** Tiny markdown -> HTML helper (headings, lists, code fences, tables, quotes). */
export function mdToHtml(md: string): string {
  const lines = escapeHtml(md.replace(/\r\n/g, "\n")).split("\n");
  const out: string[] = [];
  let inCode = false;
  let inList = false;
  let inTable = false;

  const closeList = () => {
    if (inList) {
      out.push("</ul>");
      inList = false;
    }
  };
  const closeTable = () => {
    if (inTable) {
      out.push("</table>");
      inTable = false;
    }
  };

  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      closeList();
      closeTable();
      out.push(inCode ? "</code></pre>" : "<pre><code>");
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      out.push(line);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeList();
      closeTable();
      const lvl = h[1].length;
      out.push(`<h${lvl}>${inlineMd(h[2])}</h${lvl}>`);
      continue;
    }
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      closeTable();
      if (!inList) {
        out.push("<ul>");
        inList = true;
      }
      out.push(`<li>${inlineMd(line.replace(/^\s*([-*+]|\d+\.)\s+/, ""))}</li>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      closeList();
      if (/^\s*\|[\s:|-]+\|\s*$/.test(line)) continue; // separator row
      if (!inTable) {
        out.push("<table>");
        inTable = true;
      }
      const cells = line.trim().replace(/^\||\|$/g, "").split("|");
      out.push(`<tr>${cells.map((c) => `<td>${inlineMd(c.trim())}</td>`).join("")}</tr>`);
      continue;
    }
    closeList();
    closeTable();
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      out.push("<hr/>");
      continue;
    }
    if (/^\s*&gt;\s?/.test(line)) {
      out.push(`<blockquote>${inlineMd(line.replace(/^\s*&gt;\s?/, ""))}</blockquote>`);
      continue;
    }
    if (line.trim() === "") {
      out.push("");
      continue;
    }
    out.push(`<p>${inlineMd(line)}</p>`);
  }
  if (inCode) out.push("</code></pre>");
  closeList();
  closeTable();
  return out.join("\n");
}

export function Markdown({ text }: { text: string }) {
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: mdToHtml(text) }} />;
}

/** Render a file: markdown for .md, <pre> otherwise. */
export function FileView({ path, text }: { path: string; text: string }) {
  if (path.endsWith(".md")) return <Markdown text={text} />;
  return <pre className="file-pre">{text}</pre>;
}
