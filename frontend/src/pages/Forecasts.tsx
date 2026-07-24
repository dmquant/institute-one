import { useMemo, useState } from "react";
import {
  addFavorite,
  ApiError,
  AUTH_TOKEN_KEY,
  BookNavPoint,
  BookPosition,
  Forecast,
  getBookNav,
  listBookPositions,
  listFavorites,
  removeFavorite,
  settleForecast,
} from "../api";
import { Empty, ErrorNote, Loading, PageHead, StatusBadge, ago, fmtTime, useLoad } from "../ui";

const DIRECTION_ZH: Record<string, string> = { long: "看多", short: "看空", neutral: "中性" };
const VERDICT_ZH: Record<string, string> = { hit: "命中", miss: "落空", partial: "部分", invalid: "无效" };
const FC_STATUSES = ["", "open", "settled", "invalid"];

export default function Forecasts() {
  return (
    <>
      <PageHead zh="预测与账本" en="Forecasts · Paper Book" />
      <ForecastsCard />
      <div className="grid cols-2">
        <BookPositionsCard />
        <BookNavCard />
      </div>
    </>
  );
}

// ---- forecasts (GET /api/forecasts) -----------------------------------------

/** The LEDGER view must show every row, backfills included: the API's default
 * scope excludes origin='backfill' (correct for the Dashboard hit-rate, which
 * keeps using listForecasts), so this page passes origin=all explicitly.
 * Raw fetch with the Dashboard's auth pattern — api.listForecasts has no
 * origin parameter and api.ts belongs to another change. */
async function listAllForecasts(status?: string): Promise<Forecast[]> {
  const params = new URLSearchParams({ origin: "all", limit: "100" });
  if (status) params.set("status", status);
  const headers = new Headers();
  const token = window.localStorage.getItem(AUTH_TOKEN_KEY)?.trim() ?? "";
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`/api/forecasts?${params}`, { headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Non-JSON errors keep the HTTP status text.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as Forecast[];
}

function ForecastsCard() {
  const [status, setStatus] = useState("");
  const rows = useLoad(() => listAllForecasts(status || undefined), [status], 30000);
  const favorites = useLoad(() => listFavorites("forecast"), [], 30000);
  const favoriteIds = useMemo(
    () => new Set((favorites.data ?? []).map((favorite) => favorite.ref_id)),
    [favorites.data],
  );
  const [err, setErr] = useState<string | null>(null);
  const [settling, setSettling] = useState<string | null>(null);
  const [favoriteBusy, setFavoriteBusy] = useState<string | null>(null);

  const settle = async (id: string) => {
    setErr(null);
    setSettling(id);
    try {
      await settleForecast(id);
      rows.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSettling(null);
    }
  };

  const toggleFavorite = async (id: string) => {
    setErr(null);
    setFavoriteBusy(id);
    try {
      if (favoriteIds.has(id)) await removeFavorite("forecast", id);
      else await addFavorite("forecast", id);
      favorites.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setFavoriteBusy(null);
    }
  };

  const ruleText = (f: Forecast): string => {
    const r = f.settlement_rule;
    if (typeof r === "string") return r;
    const bench = r.benchmark_id ? ` vs ${r.benchmark_id}` : "";
    return `${r.type}${bench} @ ${r.threshold ?? "?"}`;
  };

  return (
    <div className="card">
      <h2>
        预测台账<span className="en">forecasts</span>
        <select
          style={{ marginLeft: 12, padding: "2px 8px", fontSize: 12 }}
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          {FC_STATUSES.map((s) => (
            <option key={s} value={s}>
              {s === "" ? "全部状态" : s}
            </option>
          ))}
        </select>
      </h2>
      <ErrorNote error={err} />
      <ErrorNote error={rows.error} />
      <ErrorNote error={favorites.error} />
      {rows.loading && !rows.data && <Loading />}
      <table className="data">
        <thead>
          <tr>
            <th>论断</th>
            <th>方向</th>
            <th>标的</th>
            <th>结算规则</th>
            <th>状态</th>
            <th>到期</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {(rows.data ?? []).map((f) => {
            const expiresMs = new Date(f.expires_at).getTime();
            const expired = f.status === "open" && !isNaN(expiresMs) && expiresMs <= Date.now();
            return (
              <tr id={`forecast-${f.id}`} key={f.id}>
                <td title={f.claim}>
                  <div className="ellipsis" style={{ maxWidth: 300 }}>
                    {f.claim}
                  </div>
                  <div className="faint mono" style={{ fontSize: 11 }}>
                    {f.id} · 论点 {f.thesis_id}
                  </div>
                </td>
                <td>{DIRECTION_ZH[f.direction] ?? f.direction}</td>
                <td className="mono">{f.security_id ?? "—"}</td>
                <td className="dim mono" style={{ fontSize: 11.5 }}>
                  {ruleText(f)}
                </td>
                <td>
                  <StatusBadge status={f.status} />
                  {f.settlement?.verdict && (
                    <span
                      className="mono"
                      style={{
                        marginLeft: 6,
                        fontSize: 11.5,
                        color:
                          f.settlement.verdict === "hit"
                            ? "var(--green)"
                            : f.settlement.verdict === "miss"
                              ? "var(--red)"
                              : "var(--amber)",
                      }}
                    >
                      {VERDICT_ZH[f.settlement.verdict] ?? f.settlement.verdict}
                    </span>
                  )}
                </td>
                <td className="dim nowrap" title={fmtTime(f.expires_at)}>
                  {ago(f.expires_at)}
                  {expired && <span className="hand-cooldown"> 可结算</span>}
                </td>
                <td>
                  <button
                    aria-label={favoriteIds.has(f.id) ? "取消收藏" : "收藏预测"}
                    className="small ghost"
                    disabled={favorites.loading || favoriteBusy === f.id}
                    onClick={() => toggleFavorite(f.id)}
                    style={{
                      color: favoriteIds.has(f.id) ? "var(--amber)" : undefined,
                      marginRight: expired ? 6 : 0,
                    }}
                    title={favoriteIds.has(f.id) ? "取消收藏" : "收藏到洞察页"}
                  >
                    {favoriteIds.has(f.id) ? "★" : "☆"}
                  </button>
                  {expired && (
                    <button className="small ghost" onClick={() => settle(f.id)} disabled={settling === f.id}>
                      {settling === f.id ? "结算中…" : "结算"}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.data?.length === 0 && <Empty text="还没有预测记录" />}
    </div>
  );
}

// ---- paper book (C3, landing in parallel — 404/501 degrade to 账本未启用) ----

/** true when the error means the book API isn't deployed (yet). */
function bookDisabled(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 404 || e.status === 501);
}

const CLOSE_REASON_ZH: Record<string, string> = {
  stop: "止损",
  target: "止盈",
  horizon: "到期",
  manual: "手动",
};

function BookPositionsCard() {
  const [status, setStatus] = useState("");
  const rows = useLoad<BookPosition[] | "disabled">(
    () => listBookPositions(status || undefined).catch((e: unknown) => {
      if (bookDisabled(e)) return "disabled" as const;
      throw e;
    }),
    [status],
    60000,
  );

  if (rows.data === "disabled") {
    return (
      <div className="card">
        <h2>
          纸面持仓<span className="en">book positions</span>
        </h2>
        <Empty text="账本未启用（/api/book 尚未部署）" />
      </div>
    );
  }

  const positions = rows.data ?? [];
  return (
    <div className="card">
      <h2>
        纸面持仓<span className="en">book positions</span>
        <select
          style={{ marginLeft: 12, padding: "2px 8px", fontSize: 12 }}
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="">全部</option>
          <option value="open">持仓中</option>
          <option value="closed">已平仓</option>
        </select>
      </h2>
      <ErrorNote error={rows.error} />
      {rows.loading && !rows.data && <Loading />}
      <table className="data">
        <thead>
          <tr>
            <th>标的</th>
            <th>方向</th>
            <th>入场价</th>
            <th>已实现盈亏</th>
            <th>平仓原因</th>
            <th>开仓</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p, i) => (
            <tr key={p.id ?? i}>
              <td className="mono" title={p.forecast_id ? `forecast ${p.forecast_id}` : undefined}>
                {p.security_id ?? "（已删标的）"}
              </td>
              <td>{p.direction === "long" ? "多" : p.direction === "short" ? "空" : (p.direction ?? "—")}</td>
              <td className="mono">{typeof p.entry_price === "number" ? p.entry_price : "—"}</td>
              <td
                className="mono"
                style={
                  typeof p.realized_pnl === "number"
                    ? { color: p.realized_pnl >= 0 ? "var(--green)" : "var(--red)" }
                    : undefined
                }
              >
                {typeof p.realized_pnl === "number" ? p.realized_pnl.toFixed(4) : "—"}
              </td>
              <td className="dim">
                {p.close_reason ? (CLOSE_REASON_ZH[p.close_reason] ?? p.close_reason) : "—"}
              </td>
              <td className="dim nowrap">{p.opened_at ? ago(p.opened_at) : "—"}</td>
              <td>{p.status ? <StatusBadge status={p.status} /> : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {positions.length === 0 && !rows.loading && <Empty text="没有持仓记录" />}
    </div>
  );
}

function BookNavCard() {
  const nav = useLoad<BookNavPoint[] | "disabled">(
    () => getBookNav(90).catch((e: unknown) => {
      if (bookDisabled(e)) return "disabled" as const;
      throw e;
    }),
    [],
    300000,
  );

  if (nav.data === "disabled") {
    return (
      <div className="card">
        <h2>
          净值曲线<span className="en">NAV</span>
        </h2>
        <Empty text="账本未启用（/api/book 尚未部署）" />
      </div>
    );
  }

  const points = (nav.data ?? []).filter(
    (p): p is BookNavPoint & { work_date: string; nav: number } =>
      typeof p.work_date === "string" && typeof p.nav === "number" && Number.isFinite(p.nav),
  );

  return (
    <div className="card">
      <h2>
        净值曲线<span className="en">NAV vs benchmark · 90d</span>
      </h2>
      <ErrorNote error={nav.error} />
      {nav.loading && !nav.data && <Loading />}
      {points.length >= 2 ? <NavChart points={points} /> : !nav.loading && <Empty text="净值数据不足（≥2 个点才能画线）" />}
    </div>
  );
}

/** Plain SVG polylines (NAV + optional benchmark) — no chart library. */
function NavChart({ points }: { points: (BookNavPoint & { work_date: string; nav: number })[] }) {
  const W = 560;
  const H = 180;
  const PAD = 8;
  const benches = points
    .map((p) => p.benchmark_nav)
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const all = [...points.map((p) => p.nav), ...benches];
  const min = Math.min(...all);
  const max = Math.max(...all);
  const span = max - min || 1;
  const x = (i: number) => PAD + (i * (W - 2 * PAD)) / Math.max(1, points.length - 1);
  const y = (v: number) => H - PAD - ((v - min) * (H - 2 * PAD)) / span;
  const navPath = points.map((p, i) => `${x(i).toFixed(1)},${y(p.nav).toFixed(1)}`).join(" ");
  const benchPath = points
    .map((p, i) =>
      typeof p.benchmark_nav === "number" && Number.isFinite(p.benchmark_nav)
        ? `${x(i).toFixed(1)},${y(p.benchmark_nav).toFixed(1)}`
        : null,
    )
    .filter((s): s is string => s !== null)
    .join(" ");
  const last = points[points.length - 1];
  const first = points[0];
  const up = last.nav >= first.nav;

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}>
        {benchPath && (
          <polyline points={benchPath} fill="none" stroke="var(--grey)" strokeWidth={1} strokeDasharray="4 3" />
        )}
        <polyline
          points={navPath}
          fill="none"
          stroke={up ? "var(--green)" : "var(--red)"}
          strokeWidth={1.6}
          strokeLinejoin="round"
        />
      </svg>
      <div className="form-row" style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>
        <span>
          {first.work_date} <b className="mono">{first.nav.toFixed(4)}</b>
        </span>
        {benchPath && <span className="faint">虚线 = 基准</span>}
        <span style={{ marginLeft: "auto" }}>
          {last.work_date}{" "}
          <b className="mono" style={{ color: up ? "var(--green)" : "var(--red)" }}>
            {last.nav.toFixed(4)}
          </b>
        </span>
      </div>
    </div>
  );
}
