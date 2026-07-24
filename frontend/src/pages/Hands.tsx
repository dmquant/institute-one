import { useEffect, useMemo, useState } from "react";
import {
  HandStatsAgg,
  WEIGHT_SCOPES,
  WeightScope,
  clearHandCooldown,
  getHandStats,
  getHandWeights,
  getScorecard,
  listHands,
  putHandWeights,
} from "../api";
import { Empty, ErrorNote, Loading, PageHead, countdown, useLoad, useNow } from "../ui";

const SCOPE_ZH: Record<WeightScope, string> = {
  default: "默认",
  whiteboard: "白板",
  research: "研究",
  daily: "日报",
  mailbox: "信箱",
};

const VERDICT_ZH: Record<string, string> = {
  ok: "合格",
  stub: "敷衍",
  false_complete: "假完成",
};

export default function Hands() {
  const now = useNow(1000);
  const hands = useLoad(listHands, [], 15000);

  return (
    <>
      <PageHead zh="执行手" en="Hands" />

      <div className="card">
        <h2>
          状态<span className="en">status</span>
        </h2>
        <ErrorNote error={hands.error} />
        {hands.loading && !hands.data && <Loading />}
        <div className="stat-row">
          {(hands.data ?? []).map((h) => {
            const cooling = h.cooldown_until !== null && h.cooldown_until > now / 1000;
            return (
              <div className="chip" key={h.name} title={h.cooldown_reason ?? h.type}>
                <span className={`dot ${h.available ? "on" : "off"}`} />
                <span className="name">{h.name}</span>
                {!h.installed && <span className="faint">未安装</span>}
                {h.degraded && <span className="hand-cooldown">降级</span>}
                {cooling && h.cooldown_until !== null && (
                  <span className="hand-cooldown">冷却 {countdown(h.cooldown_until, now)}</span>
                )}
                {cooling && (
                  <button className="small ghost" onClick={() => clearHandCooldown(h.name).then(hands.reload)}>
                    解除
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <WeightsCard handNames={(hands.data ?? []).map((h) => h.name)} />

      <div className="grid cols-2">
        <StatsCard />
        <ScorecardCard />
      </div>
    </>
  );
}

// ---- weights: scope × hand grid (GET/PUT /api/hands/weights) ---------------

function WeightsCard({ handNames }: { handNames: string[] }) {
  const weights = useLoad(getHandWeights, []);
  // grid edits: "scope|hand" -> input text (kept as string so partial input survives)
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const saved = useMemo(() => {
    const m: Record<string, number> = {};
    for (const r of weights.data ?? []) m[`${r.scope}|${r.hand}`] = r.weight;
    return m;
  }, [weights.data]);

  // rows = union of registry hands and any hand that already has a weight row
  const rows = useMemo(() => {
    const set = new Set(handNames);
    for (const r of weights.data ?? []) set.add(r.hand);
    return Array.from(set).sort();
  }, [handNames, weights.data]);

  useEffect(() => setEdits({}), [weights.data]); // a reload discards stale edits

  const cellValue = (key: string): string => edits[key] ?? (key in saved ? String(saved[key]) : "");

  const dirty = Object.entries(edits).filter(([k, v]) => {
    const savedText = k in saved ? String(saved[k]) : "";
    return v !== savedText;
  });

  const save = async () => {
    setErr(null);
    setNote(null);
    const entries = [];
    for (const [key, text] of dirty) {
      if (text.trim() === "") continue; // blank = leave untouched (no delete API per-cell)
      const [scope, hand] = key.split("|");
      const weight = Number(text);
      if (!Number.isFinite(weight) || weight < 0) {
        setErr(`权重必须是 ≥0 的数字：${scope}/${hand} = "${text}"`);
        return;
      }
      entries.push({ scope: scope as WeightScope, hand, weight });
    }
    if (entries.length === 0) {
      setNote("没有可保存的修改");
      return;
    }
    setSaving(true);
    try {
      const r = await putHandWeights(entries);
      setNote(`已保存 ${r.upserted} 项权重`);
      weights.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card">
      <h2>
        路由权重<span className="en">weights · scope × hand</span>
        <button className="small" style={{ marginLeft: 12 }} onClick={save} disabled={saving || dirty.length === 0}>
          {saving ? "保存中…" : `保存修改${dirty.length > 0 ? ` (${dirty.length})` : ""}`}
        </button>
        <button className="small ghost" style={{ marginLeft: 6 }} onClick={weights.reload}>
          重置
        </button>
      </h2>
      {note && <div className="ok-note">{note}</div>}
      <ErrorNote error={err} />
      <ErrorNote error={weights.error} />
      {weights.loading && !weights.data && <Loading />}
      {rows.length === 0 && !weights.loading && <Empty text="没有执行手" />}
      {rows.length > 0 && (
        <>
          <table className="data">
            <thead>
              <tr>
                <th>执行手 \ 场景</th>
                {WEIGHT_SCOPES.map((s) => (
                  <th key={s}>
                    {SCOPE_ZH[s]} <span className="faint">{s}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((hand) => (
                <tr key={hand}>
                  <td className="mono">{hand}</td>
                  {WEIGHT_SCOPES.map((scope) => {
                    const key = `${scope}|${hand}`;
                    const changed = key in edits && edits[key] !== (key in saved ? String(saved[key]) : "");
                    return (
                      <td key={scope}>
                        <input
                          style={{
                            width: 72,
                            padding: "3px 7px",
                            fontFamily: "var(--mono)",
                            fontSize: 12,
                            borderColor: changed ? "var(--amber)" : undefined,
                          }}
                          placeholder="—"
                          value={cellValue(key)}
                          onChange={(e) => setEdits((prev) => ({ ...prev, [key]: e.target.value }))}
                        />
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="faint" style={{ fontSize: 12, margin: "8px 0 0" }}>
            空白 = 未设置（该场景用等权兜底）；数字越大被选中概率越高；0 = 不参与该场景。
          </p>
        </>
      )}
    </div>
  );
}

// ---- stats: CSS bar chart (GET /api/hands/stats) ----------------------------

const HOURS_CHOICES = [24, 72, 168];

function StatsCard() {
  const [hours, setHours] = useState(24);
  const stats = useLoad(() => getHandStats(hours), [hours], 60000);

  const entries = Object.entries(stats.data?.by_hand ?? {}).sort(
    (a, b) => b[1].tasks_total - a[1].tasks_total,
  );
  const maxTotal = Math.max(1, ...entries.map(([, v]) => v.tasks_total));

  return (
    <div className="card">
      <h2>
        任务统计<span className="en">stats · {hours}h</span>
        <span style={{ marginLeft: 12 }}>
          {HOURS_CHOICES.map((h) => (
            <button
              key={h}
              className={`small ${h === hours ? "" : "ghost"}`}
              style={{ marginRight: 4 }}
              onClick={() => setHours(h)}
            >
              {h}h
            </button>
          ))}
        </span>
      </h2>
      <ErrorNote error={stats.error} />
      {stats.loading && !stats.data && <Loading />}
      {entries.length === 0 && stats.data && <Empty text="窗口内没有任务统计" />}
      <div className="bars">
        {entries.map(([hand, v]) => (
          <BarRow key={hand} hand={hand} agg={v} maxTotal={maxTotal} />
        ))}
      </div>
    </div>
  );
}

function BarRow({ hand, agg, maxTotal }: { hand: string; agg: HandStatsAgg; maxTotal: number }) {
  const pct = (n: number) => (100 * n) / maxTotal;
  const other = agg.tasks_total - agg.tasks_ok - agg.tasks_failed - agg.tasks_rate_limited;
  return (
    <div className="bar-row">
      <span className="bar-label mono">{hand}</span>
      <span className="bar-track">
        <span className="bar-seg ok" style={{ width: `${pct(agg.tasks_ok)}%` }} title={`成功 ${agg.tasks_ok}`} />
        <span className="bar-seg fail" style={{ width: `${pct(agg.tasks_failed)}%` }} title={`失败 ${agg.tasks_failed}`} />
        <span
          className="bar-seg rate"
          style={{ width: `${pct(agg.tasks_rate_limited)}%` }}
          title={`限流 ${agg.tasks_rate_limited}`}
        />
        {other > 0 && <span className="bar-seg other" style={{ width: `${pct(other)}%` }} title={`其他 ${other}`} />}
      </span>
      <span className="bar-nums mono">
        {agg.tasks_total} 次
        {agg.avg_duration_ms !== null && (
          <span className="faint"> · 平均 {(agg.avg_duration_ms / 1000).toFixed(1)}s</span>
        )}
      </span>
    </div>
  );
}

// ---- scorecard (GET /api/hands/scorecard?date=) ------------------------------

function ScorecardCard() {
  const [date, setDate] = useState(""); // empty = backend default (previous work date)
  const card = useLoad(() => getScorecard(date || undefined), [date], 60000);

  const byHand = Object.entries(card.data?.by_hand ?? {}).sort();
  const counts = card.data?.counts;

  return (
    <div className="card">
      <h2>
        质量评分卡<span className="en">scorecard {card.data ? `· ${card.data.date}` : ""}</span>
        <input
          type="date"
          style={{ marginLeft: 12, padding: "2px 8px", fontSize: 12 }}
          value={date}
          onChange={(e) => setDate(e.target.value)}
        />
      </h2>
      <ErrorNote error={card.error} />
      {card.loading && !card.data && <Loading />}
      {counts && (
        <div className="stat-row" style={{ marginBottom: 10 }}>
          <div className="stat-box">
            <div className="n" style={{ color: "var(--green)" }}>
              {counts.ok}
            </div>
            <div className="l">合格 ok</div>
          </div>
          <div className="stat-box">
            <div className="n" style={{ color: "var(--amber)" }}>
              {counts.stub}
            </div>
            <div className="l">敷衍 stub</div>
          </div>
          <div className="stat-box">
            <div className="n" style={{ color: "var(--red)" }}>
              {counts.false_complete}
            </div>
            <div className="l">假完成</div>
          </div>
        </div>
      )}
      {byHand.length > 0 && (
        <table className="data">
          <thead>
            <tr>
              <th>执行手</th>
              <th>合格</th>
              <th>敷衍</th>
              <th>假完成</th>
            </tr>
          </thead>
          <tbody>
            {byHand.map(([hand, v]) => (
              <tr key={hand}>
                <td className="mono">{hand}</td>
                <td className="mono" style={{ color: "var(--green)" }}>
                  {v.ok}
                </td>
                <td className="mono" style={v.stub > 0 ? { color: "var(--amber)" } : undefined}>
                  {v.stub}
                </td>
                <td className="mono" style={v.false_complete > 0 ? { color: "var(--red)" } : undefined}>
                  {v.false_complete}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {card.data && byHand.length === 0 && <Empty text="该日期还没有评分记录" />}
      {(card.data?.entries.filter((e) => e.verdict !== "ok").length ?? 0) > 0 && (
        <details style={{ marginTop: 10 }}>
          <summary className="faint" style={{ cursor: "pointer", fontSize: 12 }}>
            问题条目（{card.data!.entries.filter((e) => e.verdict !== "ok").length}）
          </summary>
          <table className="data" style={{ marginTop: 6 }}>
            <thead>
              <tr>
                <th>任务</th>
                <th>执行手</th>
                <th>判定</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {card.data!.entries
                .filter((e) => e.verdict !== "ok")
                .map((e) => (
                  <tr key={e.task_id}>
                    <td className="mono">{e.task_id}</td>
                    <td className="mono">{e.hand}</td>
                    <td>{VERDICT_ZH[e.verdict] ?? e.verdict}</td>
                    <td className="dim ellipsis" title={e.reason ?? ""}>
                      {e.reason ?? ""}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}
