import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  ApiError,
  OperatorAction,
  OperatorActionKind,
  OperatorActionStatus,
  OperatorParameter,
  OperatorParameters,
  OperatorProposal,
  OperatorTriage,
  approveDisposition,
  approveOperatorProposal,
  getOperatorParameters,
  getOperatorTriage,
  listOperatorActions,
  listOperatorProposals,
  patchOperatorAction,
  putOperatorParameter,
  putFeatureSwitches,
  rejectOperatorProposal,
} from "../api";
import { Empty, ErrorNote, Loading, PageHead, ago, fmtTime, useLoad } from "../ui";

// GET /api/operator/* answers 404/501 while the operator router is unmounted
// (the SPA GET catch-all is mapped to 404 by api.ts req()) — degrade to the
// 运维面未启用 notice instead of an error, mirroring the paper-book pattern.
function operatorDisabled(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 404 || e.status === 501);
}

const DISABLED_TEXT = "运维面未启用（/api/operator 尚未部署）";

const KIND_META: Record<OperatorActionKind, { zh: string; color: string }> = {
  vault_conflict: { zh: "知识库冲突", color: "var(--amber)" },
  disputed_fact: { zh: "事实争议", color: "var(--blue)" },
  scorecard_anomaly: { zh: "记分卡异常", color: "var(--red)" },
  failed_run: { zh: "运行失败", color: "var(--red)" },
  cron_failure: { zh: "定时任务故障", color: "var(--amber)" },
  other: { zh: "其他", color: "var(--grey)" },
};

const COLUMNS: { status: OperatorActionStatus; zh: string }[] = [
  { status: "open", zh: "待处理" },
  { status: "in_progress", zh: "处理中" },
  { status: "done", zh: "已完成" },
  { status: "dismissed", zh: "已忽略" },
];

// Mirror of the backend conditional-claim map (_ALLOWED_FROM in
// app/api/operator.py): done/dismissed are terminal.
const TRANSITIONS: Record<OperatorActionStatus, { to: OperatorActionStatus; zh: string; danger?: boolean }[]> = {
  open: [
    { to: "in_progress", zh: "认领" },
    { to: "done", zh: "完成" },
    { to: "dismissed", zh: "忽略", danger: true },
  ],
  in_progress: [
    { to: "done", zh: "完成" },
    { to: "open", zh: "释放" },
    { to: "dismissed", zh: "忽略", danger: true },
  ],
  done: [],
  dismissed: [],
};

export default function Operator() {
  const triage = useLoad<OperatorTriage | "disabled">(
    () =>
      getOperatorTriage().catch((e: unknown) => {
        if (operatorDisabled(e)) return "disabled" as const;
        throw e;
      }),
    [],
    30000,
  );
  const actions = useLoad<OperatorAction[] | "disabled">(
    () =>
      listOperatorActions().then(
        (r) => r.actions,
        (e: unknown) => {
          if (operatorDisabled(e)) return "disabled" as const;
          throw e;
        },
      ),
    [],
    30000,
  );
  const proposals = useLoad<OperatorProposal[] | "disabled">(
    () =>
      listOperatorProposals().then(
        (r) => r.proposals,
        (e: unknown) => {
          if (operatorDisabled(e)) return "disabled" as const;
          throw e;
        },
      ),
    [],
    30000,
  );
  const parameters = useLoad<OperatorParameters | "disabled">(
    () =>
      getOperatorParameters().then(
        (r) => r.parameters,
        (e: unknown) => {
          if (operatorDisabled(e)) return "disabled" as const;
          throw e;
        },
      ),
    [],
    30000,
  );

  const reloadAll = () => {
    triage.reload();
    actions.reload();
    proposals.reload();
    parameters.reload();
  };

  return (
    <>
      <PageHead zh="运维" en="Operator · actions kanban / triage">
        <button className="ghost" onClick={reloadAll}>
          刷新
        </button>
      </PageHead>

      <div className="grid cols-2">
        <TriageCard triage={triage.data} loading={triage.loading} error={triage.error} />
        <FeatureSwitchesCard
          triage={triage.data}
          loading={triage.loading}
          onSaved={triage.reload}
        />
      </div>

      <div className="grid cols-2">
        <ProposalInboxCard
          proposals={proposals.data}
          loading={proposals.loading}
          error={proposals.error}
          onChanged={reloadAll}
        />
        <ParametersCard
          parameters={parameters.data}
          loading={parameters.loading}
          error={parameters.error}
          onChanged={reloadAll}
        />
      </div>

      <KanbanCard
        actions={actions.data}
        loading={actions.loading}
        error={actions.error}
        onChanged={reloadAll}
      />
    </>
  );
}

// ---- self-improvement proposals (explicit human approve/reject gate) --------

const PROPOSAL_KIND_ZH: Record<string, string> = {
  promote_recipe: "晋升处置配方",
  retire_recipe: "退役处置配方",
  set_parameter: "调整白名单参数",
};

const PROPOSAL_STATUS_ZH: Record<string, string> = {
  proposed: "待决策",
  approved: "已批准",
  rejected: "已拒绝",
};

function ProposalInboxCard({
  proposals,
  loading,
  error,
  onChanged,
}: {
  proposals: OperatorProposal[] | "disabled" | null;
  loading: boolean;
  error: string | null;
  onChanged: () => void;
}) {
  if (proposals === "disabled") {
    return (
      <div className="card">
        <h2>
          改进提案<span className="en">self-improvement proposals</span>
        </h2>
        <Empty text={DISABLED_TEXT} />
      </div>
    );
  }

  const ordered = [...(proposals ?? [])].sort(
    (a, b) => Number(b.status === "proposed") - Number(a.status === "proposed") || b.id - a.id,
  );
  const pending = ordered.filter((p) => p.status === "proposed").length;

  return (
    <div className="card">
      <h2>
        改进提案<span className="en">human decision inbox · {pending} pending</span>
      </h2>
      <ErrorNote error={error} />
      {loading && !proposals && <Loading />}
      {proposals && ordered.length === 0 && <Empty text="暂无改进提案" />}
      {proposals && ordered.map((proposal) => (
        <ProposalRow key={proposal.id} proposal={proposal} onChanged={onChanged} />
      ))}
      <p className="faint" style={{ fontSize: 11.5, margin: "10px 0 0" }}>
        批准会立即应用提案中的白名单参数或配方变更；拒绝不会应用任何变更。两者都是条件认领，状态已被他处决定时会返回
        409 并自动刷新。
      </p>
    </div>
  );
}

function ProposalRow({ proposal: p, onChanged }: { proposal: OperatorProposal; onChanged: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const decide = async (decision: "approve" | "reject") => {
    const verb = decision === "approve" ? "批准并立即应用" : "拒绝且不应用";
    if (!window.confirm(`确认${verb}提案 #${p.id}「${p.title}」？`)) return;
    setBusy(decision);
    setError(null);
    try {
      if (decision === "approve") await approveOperatorProposal(p.id, note);
      else await rejectOperatorProposal(p.id, note);
      setNote("");
      onChanged();
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (e instanceof ApiError && e.status === 409) {
        setError(`状态冲突：${message}（已刷新最新状态）`);
        onChanged();
      } else {
        setError(message);
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="op-card" style={{ borderLeftColor: p.status === "proposed" ? "var(--amber)" : undefined }}>
      <div className="head">
        <span className="badge" title={p.kind}>{PROPOSAL_KIND_ZH[p.kind] ?? p.kind}</span>
        <span
          className="badge"
          style={p.status === "approved" ? { color: "var(--green)" } : p.status === "rejected" ? { color: "var(--red)" } : { color: "var(--amber)" }}
          title={p.status}
        >
          {PROPOSAL_STATUS_ZH[p.status] ?? p.status}
        </span>
        <span className="faint mono" style={{ marginLeft: "auto", fontSize: 11 }} title={fmtTime(p.created_at)}>
          #{p.id} · {ago(p.created_at)}
        </span>
      </div>
      <div className="title">{p.title}</div>
      {p.rationale && <div className="dim" style={{ marginTop: 5 }}>{p.rationale}</div>}
      <details className="detail">
        <summary>参数与来源</summary>
        <pre>{JSON.stringify({ params: p.params, observation_id: p.observation_id, recipe_id: p.recipe_id }, null, 2)}</pre>
      </details>
      {p.status !== "proposed" && (
        <div className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>
          {p.decided_at ? fmtTime(p.decided_at) : "已决策"} · {p.applied === 1 ? "已应用" : "未应用"}
          {p.decided_note ? ` · ${p.decided_note}` : ""}
        </div>
      )}
      {error && <div className="error-note" style={{ fontSize: 11.5, padding: "5px 8px" }}>⚠ {error}</div>}
      {p.status === "proposed" && (
        <div className="actions">
          <input
            aria-label={`提案 #${p.id} 决策备注`}
            className="grow"
            maxLength={1000}
            placeholder="决策备注（可选）"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            disabled={busy !== null}
          />
          <button
            aria-label={`批准并应用提案 #${p.id}`}
            disabled={busy !== null}
            onClick={() => void decide("approve")}
          >
            {busy === "approve" ? "应用中…" : "批准并应用"}
          </button>
          <button
            aria-label={`拒绝提案 #${p.id}`}
            className="danger"
            disabled={busy !== null}
            onClick={() => void decide("reject")}
          >
            {busy === "reject" ? "拒绝中…" : "拒绝"}
          </button>
        </div>
      )}
    </div>
  );
}

// ---- whitelisted parameters (direct human edit, backend byte-CAS) -----------

function jsonValue(value: unknown): string {
  const encoded = JSON.stringify(value);
  return encoded === undefined ? "" : encoded;
}

function ParametersCard({
  parameters,
  loading,
  error,
  onChanged,
}: {
  parameters: OperatorParameters | "disabled" | null;
  loading: boolean;
  error: string | null;
  onChanged: () => void;
}) {
  if (parameters === "disabled") {
    return (
      <div className="card">
        <h2>
          可调参数<span className="en">whitelisted parameters</span>
        </h2>
        <Empty text={DISABLED_TEXT} />
      </div>
    );
  }
  const entries = Object.entries(parameters ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="card">
      <h2>
        可调参数<span className="en">whitelisted · CAS protected</span>
      </h2>
      <ErrorNote error={error} />
      {loading && !parameters && <Loading />}
      {parameters && entries.length === 0 && <Empty text="当前没有白名单参数" />}
      {parameters && entries.map(([key, parameter]) => (
        <ParameterRow key={key} name={key} parameter={parameter} onChanged={onChanged} />
      ))}
      <p className="faint" style={{ fontSize: 11.5, margin: "10px 0 0" }}>
        这里只能修改后端白名单中的参数。值按 JSON 解析（数字可直接填写）；每次写入都会记录参数历史和效果基线。并发修改会被
        CAS 拒绝并重新加载，不会静默覆盖。
      </p>
    </div>
  );
}

function ParameterRow({
  name,
  parameter,
  onChanged,
}: {
  name: string;
  parameter: OperatorParameter;
  onChanged: () => void;
}) {
  const effective = parameter.set ? parameter.stored : parameter.default;
  const [draft, setDraft] = useState(() => jsonValue(effective));
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // Polls may refresh the server value; preserve a local edit until it is
  // either saved or explicitly discarded by a 409 conflict.
  useEffect(() => {
    if (!dirtyRef.current) setDraft(jsonValue(parameter.set ? parameter.stored : parameter.default));
  }, [parameter]);

  const edit = (value: string) => {
    setDraft(value);
    setDirty(true);
    dirtyRef.current = true;
    setError(null);
    setNote(null);
  };

  const save = async () => {
    let value: unknown;
    try {
      value = JSON.parse(draft);
    } catch {
      setError("请输入合法 JSON 值，例如 0.8、true 或 \"文本\"");
      return;
    }
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await putOperatorParameter(name, value);
      dirtyRef.current = false;
      setDirty(false);
      setDraft(jsonValue(value));
      setNote("已保存并记录效果基线");
      onChanged();
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (e instanceof ApiError && e.status === 409) {
        dirtyRef.current = false;
        setDirty(false);
        setError(`参数已被他处修改：${message}（已重新加载，请按最新值重做）`);
        onChanged();
      } else {
        setError(message);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="op-card" style={{ borderLeftColor: "var(--blue)" }}>
      <div className="mono" style={{ fontWeight: 600, wordBreak: "break-all" }}>{name}</div>
      <div className="faint" style={{ fontSize: 11.5, margin: "4px 0 7px" }}>
        当前 <span className="mono">{jsonValue(effective)}</span>
        {parameter.set ? "（显式设置）" : `（默认值，尚未设置）`}
      </div>
      {note && <div className="ok-note" style={{ fontSize: 11.5, padding: "5px 8px" }}>{note}</div>}
      {error && <div className="error-note" style={{ fontSize: 11.5, padding: "5px 8px" }}>⚠ {error}</div>}
      <div className="form-row" style={{ alignItems: "center" }}>
        <input
          aria-label={`参数 ${name}`}
          className="grow mono"
          value={draft}
          onChange={(e) => edit(e.target.value)}
          disabled={busy}
        />
        <button
          aria-label={`保存参数 ${name}`}
          disabled={busy || !dirty}
          onClick={() => void save()}
        >
          {busy ? "保存中…" : dirty ? "保存" : "已同步"}
        </button>
      </div>
    </div>
  );
}

// ---- triage panel (GET /api/operator/triage) ---------------------------------

function TriageCard({
  triage,
  loading,
  error,
}: {
  triage: OperatorTriage | "disabled" | null;
  loading: boolean;
  error: string | null;
}) {
  if (triage === "disabled") {
    return (
      <div className="card">
        <h2>
          分诊面板<span className="en">triage</span>
        </h2>
        <Empty text={DISABLED_TEXT} />
      </div>
    );
  }
  const openByKind = Object.entries(triage?.actions.open_by_kind ?? {}).sort(([, a], [, b]) => b - a);
  return (
    <div className="card">
      <h2>
        分诊面板<span className="en">triage</span>
      </h2>
      <ErrorNote error={error} />
      {loading && !triage && <Loading />}
      {triage && (
        <>
          <div className="stat-row">
            <div className="stat-box">
              <div className="n" style={{ color: triage.maintenance.paused ? "var(--amber)" : "var(--green)" }}>
                {triage.maintenance.paused ? "暂停" : "运行"}
              </div>
              <div className="l">维护模式</div>
            </div>
            <div className="stat-box">
              <div className="n">{triage.maintenance.drain_depth}</div>
              <div className="l">排空深度（排队+运行）</div>
            </div>
            <div className="stat-box">
              <div className="n" style={triage.cron.failing.length > 0 ? { color: "var(--red)" } : undefined}>
                {triage.cron.failing.length}
                <span className="faint" style={{ fontSize: 13 }}>/{triage.cron.jobs}</span>
              </div>
              <div className="l">失败定时任务</div>
            </div>
            <div className="stat-box">
              <div className="n" style={triage.vault.conflicts > 0 ? { color: "var(--red)" } : undefined}>
                {triage.vault.conflicts}
              </div>
              <div className="l">知识库冲突 / {triage.vault.ledger_total} 索引</div>
            </div>
            <div className="stat-box">
              <div className="n">{triage.actions.open}</div>
              <div className="l">待处理行动</div>
            </div>
          </div>
          {triage.cron.failing.length > 0 && (
            <p className="dim" style={{ fontSize: 12.5, margin: "10px 0 0" }}>
              失败任务：<span className="mono" style={{ color: "var(--red)" }}>{triage.cron.failing.join(" · ")}</span>
              {"　"}
              <Link to="/cron" className="faint">
                查看定时任务健康 →
              </Link>
            </p>
          )}
          {openByKind.length > 0 && (
            <p className="dim" style={{ fontSize: 12.5, margin: "10px 0 0" }}>
              待处理分布：
              {openByKind.map(([kind, n]) => (
                <span key={kind} className="mono" style={{ marginRight: 10 }}>
                  {KIND_META[kind as OperatorActionKind]?.zh ?? kind} <b>{n}</b>
                </span>
              ))}
            </p>
          )}
          <p className="faint" style={{ fontSize: 11.5, margin: "8px 0 0" }}>
            权重配置 {triage.hand_weights.configured} 条 · cron 统计窗口 {triage.cron.window_days} 天
          </p>
        </>
      )}
    </div>
  );
}

// ---- feature switches (PUT /api/operator/feature-switches, CAS) --------------

function FeatureSwitchesCard({
  triage,
  loading,
  onSaved,
}: {
  triage: OperatorTriage | "disabled" | null;
  loading: boolean;
  onSaved: () => void;
}) {
  const [switches, setSwitches] = useState<Record<string, boolean> | null>(null);
  // CAS base: the version the current edit started from (frozen while dirty)
  const [baseVersion, setBaseVersion] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // follow the server state on every poll until the user starts editing
  useEffect(() => {
    if (triage && triage !== "disabled" && !dirty) {
      setSwitches(triage.feature_switches);
      setBaseVersion(triage.feature_switches_version ?? 0);
    }
  }, [triage, dirty]);

  if (triage === "disabled") {
    return (
      <div className="card">
        <h2>
          功能开关<span className="en">feature switches</span>
        </h2>
        <Empty text={DISABLED_TEXT} />
      </div>
    );
  }

  const edit = (fn: (s: Record<string, boolean>) => Record<string, boolean>) => {
    setSwitches((s) => fn(s ?? {}));
    setDirty(true);
    setNote(null);
  };

  const addKey = () => {
    const k = newKey.trim();
    if (!k) return;
    edit((s) => ({ ...s, [k]: true }));
    setNewKey("");
  };

  const save = async () => {
    if (!switches) return;
    setSaving(true);
    setErr(null);
    setNote(null);
    try {
      const r = await putFeatureSwitches(switches, baseVersion);
      setBaseVersion(r.version);
      setDirty(false);
      setNote("已保存");
      onSaved();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // CAS lost: someone saved after we loaded — drop the stale edit,
        // resync to the server state, and ask the operator to redo it
        setDirty(false);
        setErr("开关已被他处修改（版本过期）：已重新加载最新状态，请重做修改后再保存");
        onSaved();
      } else {
        setErr(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setSaving(false);
    }
  };

  const entries = Object.entries(switches ?? {}).sort(([a], [b]) => a.localeCompare(b));

  return (
    <div className="card">
      <h2>
        功能开关<span className="en">feature switches</span>
      </h2>
      {note && <div className="ok-note">{note}</div>}
      <ErrorNote error={err} />
      {loading && !triage && <Loading />}
      {switches !== null && (
        <>
          {entries.length === 0 && <Empty text="还没有开关（下方新增）" />}
          {entries.map(([key, on]) => (
            <div key={key} className="form-row" style={{ alignItems: "center", marginBottom: 6 }}>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={on}
                  onChange={(e) => edit((s) => ({ ...s, [key]: e.target.checked }))}
                />
                <span className="mono">{key}</span>
                <span className={on ? "" : "faint"} style={on ? { color: "var(--green)" } : undefined}>
                  {on ? "开" : "关"}
                </span>
              </label>
              <button
                className="small ghost"
                title="从开关集中移除（保存后生效）"
                onClick={() =>
                  edit((s) => {
                    const next = { ...s };
                    delete next[key];
                    return next;
                  })
                }
              >
                ✕
              </button>
            </div>
          ))}
          <div className="form-row" style={{ marginTop: 10 }}>
            <input
              placeholder="新开关名，如 shadow_router"
              value={newKey}
              onChange={(e) => setNewKey(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addKey()}
            />
            <button className="ghost" onClick={addKey} disabled={!newKey.trim()}>
              新增
            </button>
            <button onClick={save} disabled={saving || !dirty}>
              {saving ? "保存中…" : dirty ? "保存（全量替换）" : "已同步"}
            </button>
          </div>
          <p className="faint" style={{ fontSize: 11.5, margin: "10px 0 0" }}>
            <span className="mono">job:&lt;任务名&gt;</span> 形式的开关由调度器消费：关=该定时任务跳过（cron
            页可见 skipped），缺省=启用。其余开关名暂为标注。保存为全量替换（此处删除即整体移除）、带版本校验（CAS）：他处先保存会得到
            409，需刷新后重做。
          </p>
        </>
      )}
    </div>
  );
}

// ---- actions kanban (GET/PATCH /api/operator/actions, approve gate) ----------

function KanbanCard({
  actions,
  loading,
  error,
  onChanged,
}: {
  actions: OperatorAction[] | "disabled" | null;
  loading: boolean;
  error: string | null;
  onChanged: () => void;
}) {
  // per-card error + busy so a 409 shows up on the card that raced, not page-top
  const [cardErr, setCardErr] = useState<Record<number, string>>({});
  const [busyAction, setBusyAction] = useState<number | null>(null);

  if (actions === "disabled") {
    return (
      <div className="card">
        <h2>
          行动看板<span className="en">actions kanban</span>
        </h2>
        <Empty text={DISABLED_TEXT} />
      </div>
    );
  }

  const run = async (actionId: number, fn: () => Promise<unknown>) => {
    setBusyAction(actionId);
    setCardErr((m) => {
      const next = { ...m };
      delete next[actionId];
      return next;
    });
    try {
      await fn();
      onChanged();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setCardErr((m) => ({ ...m, [actionId]: msg }));
      // a 409 means the card state moved under us (claim lost / already
      // disposed / floor refusal) — refresh the board to show the truth
      if (e instanceof ApiError && e.status === 409) onChanged();
    } finally {
      setBusyAction(null);
    }
  };

  const transition = (a: OperatorAction, to: OperatorActionStatus) =>
    run(a.id, () => patchOperatorAction(a.id, to));
  const approve = (a: OperatorAction, dispositionId: number) =>
    run(a.id, () => approveDisposition(dispositionId));

  const byStatus = new Map<OperatorActionStatus, OperatorAction[]>();
  for (const a of actions ?? []) {
    const list = byStatus.get(a.status) ?? [];
    list.push(a);
    byStatus.set(a.status, list);
  }

  return (
    <div className="card">
      <h2>
        行动看板<span className="en">actions kanban · shadow 建议仅记录，批准才落账</span>
      </h2>
      <ErrorNote error={error} />
      {loading && !actions && <Loading />}
      {actions && (
        <div className="kanban">
          {COLUMNS.map((col) => {
            const items = byStatus.get(col.status) ?? [];
            return (
              <div key={col.status} className="kanban-col">
                <h3>
                  {col.zh}
                  <span className="en mono">{col.status}</span>
                  <span className="n">{items.length}</span>
                </h3>
                {items.length === 0 && <div className="empty" style={{ padding: "10px 0" }}>无</div>}
                {items.map((a) => (
                  <ActionCard
                    key={a.id}
                    action={a}
                    busy={busyAction === a.id}
                    error={cardErr[a.id] ?? null}
                    onTransition={(to) => transition(a, to)}
                    onApprove={(dId) => approve(a, dId)}
                  />
                ))}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ActionCard({
  action: a,
  busy,
  error,
  onTransition,
  onApprove,
}: {
  action: OperatorAction;
  busy: boolean;
  error: string | null;
  onTransition: (to: OperatorActionStatus) => void;
  onApprove: (dispositionId: number) => void;
}) {
  const kind = KIND_META[a.kind] ?? KIND_META.other;
  const live = a.status === "open" || a.status === "in_progress";
  const prioClass = a.priority >= 3 ? "p3" : a.priority === 2 ? "p2" : "";

  return (
    <div className={`op-card ${prioClass}`}>
      <div className="head">
        <span
          className="badge"
          style={{ color: kind.color, borderColor: kind.color, background: "transparent" }}
          title={a.kind}
        >
          {kind.zh}
        </span>
        <span
          className="mono"
          style={a.priority >= 3 ? { color: "var(--red)" } : a.priority === 2 ? { color: "var(--amber)" } : undefined}
          title="priority（越大越紧急）"
        >
          P{a.priority}
        </span>
        <span className="faint mono" style={{ marginLeft: "auto", fontSize: 11 }} title={fmtTime(a.created_at)}>
          #{a.id} · {ago(a.created_at)}
        </span>
      </div>
      <div className="title">{a.title}</div>
      {a.ref && (
        <div className="faint mono" style={{ fontSize: 11, wordBreak: "break-all" }}>
          {a.ref}
        </div>
      )}
      {a.detail && (
        <details className="detail">
          <summary>详情</summary>
          <pre>{a.detail}</pre>
        </details>
      )}

      {a.dispositions.map((d) => {
        const flags = d.flags ? d.flags.split(",").filter(Boolean) : [];
        const low = flags.includes("low_confidence");
        const pinned = flags.includes("human_pinned");
        const approved = flags.includes("approved");
        return (
          <div
            key={d.id}
            className={`op-disp ${low ? "low" : ""}`}
            title={d.shadow === 1 ? "shadow 建议：仅记录，绝不自动执行；批准后仍需人工落实" : undefined}
          >
            <span className="faint mono">{d.proposed_by}</span>
            <b className="mono">{d.disposition}</b>
            <span className="mono" title="提案时点置信度（批准时按实时门槛复核）">
              {d.confidence === null ? "置信度缺失" : d.confidence.toFixed(2)}
            </span>
            {low && (
              <span className="faint" title="low_confidence：提案时低于门槛（门槛实时可调，批准时以实时值为准）">
                低置信
              </span>
            )}
            {pinned && (
              <span title="human_pinned：提示词/排程领域，永不自动执行——批准仅记账，改动仍须人工">
                📌
              </span>
            )}
            {approved && <span style={{ color: "var(--green)" }}>✓ 已批准</span>}
            {live && !approved && (
              <button className="small" style={{ marginLeft: "auto" }} disabled={busy} onClick={() => onApprove(d.id)}>
                批准
              </button>
            )}
          </div>
        );
      })}

      {a.resolution && (
        <div className="dim" style={{ fontSize: 11.5, marginTop: 6, wordBreak: "break-word" }}>
          处置：{a.resolution}
        </div>
      )}

      {error && (
        <div className="error-note" style={{ fontSize: 11.5, padding: "5px 8px" }}>
          ⚠ {error}
        </div>
      )}

      {TRANSITIONS[a.status].length > 0 && (
        <div className="actions">
          {TRANSITIONS[a.status].map((t) => (
            <button
              key={t.to}
              className={`small ${t.danger ? "danger" : "ghost"}`}
              disabled={busy}
              onClick={() => onTransition(t.to)}
            >
              {busy ? "…" : t.zh}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
