import { useMemo, useState } from "react";
import {
  Analyst,
  AnalystInput,
  createAnalyst,
  deleteAnalyst,
  getAnalystDailyStatus,
  getAnalystRoles,
  listAnalysts,
  runAllAnalystDailies,
  runAnalystDaily,
  updateAnalyst,
} from "../api";
import { Empty, ErrorNote, Loading, PageHead, useLoad } from "../ui";

const HANDS = ["claude", "codex", "gemini", "agy", "opencode", "ollama"];
const CUSTOM_ROLE = "__custom__";

interface FormState {
  id: string;
  name: string;
  name_en: string;
  category: string;
  emoji: string;
  focus: string;
  persona: string;
  hand: string;
  model: string;
}

const EMPTY_FORM: FormState = {
  id: "",
  name: "",
  name_en: "",
  category: "",
  emoji: "🧑‍💼",
  focus: "",
  persona: "",
  hand: "",
  model: "",
};

export default function Analysts() {
  const analysts = useLoad(listAnalysts, []);
  const roles = useLoad(getAnalystRoles, []);
  const dailyStatus = useLoad(() => getAnalystDailyStatus(), []);

  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null); // analyst id when editing
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [customRole, setCustomRole] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // known roles + roles already in use (dedup, keep order)
  const roleOptions = useMemo(
    () => Array.from(new Set([...(roles.data?.roles ?? []), ...(roles.data?.in_use ?? [])])),
    [roles.data]
  );

  const set =
    (k: keyof FormState) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
      setForm((f) => ({ ...f, [k]: e.target.value }));

  const startCreate = () => {
    setForm(EMPTY_FORM);
    setEditing(null);
    setCustomRole(false);
    setErr(null);
    setNote(null);
    setOpen(true);
  };

  const startEdit = (a: Analyst) => {
    setForm({
      id: a.id,
      name: a.name,
      name_en: a.name_en,
      category: a.category,
      emoji: a.emoji,
      focus: a.focus,
      persona: a.persona,
      hand: a.hand ?? "",
      model: a.model ?? "",
    });
    setEditing(a.id);
    setCustomRole(false);
    setErr(null);
    setNote(null);
    setOpen(true);
    window.scrollTo({ top: 0 });
  };

  const cancelForm = () => {
    setOpen(false);
    setEditing(null);
    setForm(EMPTY_FORM);
    setCustomRole(false);
  };

  const canSubmit =
    !!form.id.trim() &&
    !!form.name.trim() &&
    !!form.name_en.trim() &&
    !!form.category.trim() &&
    !!form.emoji.trim() &&
    !!form.focus.trim() &&
    !!form.persona.trim();

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setErr(null);
    setNote(null);
    const body: AnalystInput = {
      id: form.id.trim(),
      name: form.name.trim(),
      name_en: form.name_en.trim(),
      category: form.category.trim(),
      emoji: form.emoji.trim(),
      focus: form.focus.trim(),
      persona: form.persona.trim(),
      hand: form.hand || null,
      model: form.model.trim() || null,
    };
    try {
      if (editing) {
        const updated = await updateAnalyst(editing, body);
        setNote(`已更新分析师 ${updated.name}（${updated.id}）`);
      } else {
        const created = await createAnalyst(body);
        setNote(`已新增分析师 ${created.name}（${created.id}）`);
      }
      cancelForm();
      analysts.reload();
      roles.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (a: Analyst) => {
    if (!window.confirm(`确认删除分析师「${a.name}（${a.id}）」？此操作不可恢复。`)) return;
    setErr(null);
    setNote(null);
    try {
      await deleteAnalyst(a.id);
      setNote(`已删除分析师 ${a.name}（${a.id}）`);
      if (editing === a.id) cancelForm();
      analysts.reload();
      roles.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const runDaily = async (a: Analyst) => {
    setErr(null);
    setNote(null);
    try {
      await runAnalystDaily(a.id);
      setNote(`已启动 ${a.name} 的观察日报（后台运行，完成后跟进项自动进入白板与信箱）`);
      setTimeout(() => dailyStatus.reload(), 1500);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const runAllDailies = async () => {
    setErr(null);
    setNote(null);
    try {
      await runAllAnalystDailies();
      setNote("已启动全员观察日报（后台运行；已完成的分析师会自动跳过）");
      setTimeout(() => dailyStatus.reload(), 1500);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const dailyState = (id: string) => dailyStatus.data?.analysts?.[id];

  const list = analysts.data ?? [];

  return (
    <>
      <PageHead zh="分析师" en="Analysts">
        <button className="ghost" onClick={runAllDailies}>
          运行全员日报
        </button>
        <button onClick={startCreate}>新增分析师</button>
      </PageHead>
      {note && <div className="ok-note">{note}</div>}
      <ErrorNote error={err} />
      <ErrorNote error={analysts.error} />

      {open && (
        <div className="card">
          <h2>
            {editing ? "编辑分析师" : "新增分析师"}
            <span className="en">{editing ? `edit · ${editing}` : "new analyst"}</span>
          </h2>
          <div className="form-row">
            <label className="field">
              <span className="lbl">ID（slug）</span>
              <input
                style={{ width: 160 }}
                value={form.id}
                onChange={set("id")}
                disabled={editing !== null}
                placeholder="如 macro-lin"
              />
            </label>
            <label className="field">
              <span className="lbl">中文名 Name</span>
              <input style={{ width: 140 }} value={form.name} onChange={set("name")} placeholder="如 林宏" />
            </label>
            <label className="field">
              <span className="lbl">英文名 Name (EN)</span>
              <input style={{ width: 160 }} value={form.name_en} onChange={set("name_en")} placeholder="如 Lin Hong" />
            </label>
            <label className="field">
              <span className="lbl">Emoji</span>
              <input style={{ width: 70 }} value={form.emoji} onChange={set("emoji")} />
            </label>
            <label className="field">
              <span className="lbl">角色 Role</span>
              <select
                value={customRole ? CUSTOM_ROLE : form.category}
                onChange={(e) => {
                  if (e.target.value === CUSTOM_ROLE) {
                    setCustomRole(true);
                    setForm((f) => ({ ...f, category: "" }));
                  } else {
                    setCustomRole(false);
                    setForm((f) => ({ ...f, category: e.target.value }));
                  }
                }}
              >
                <option value="">（选择角色）</option>
                {roleOptions.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
                <option value={CUSTOM_ROLE}>自定义…</option>
              </select>
            </label>
            {customRole && (
              <label className="field">
                <span className="lbl">自定义角色 custom</span>
                <input
                  style={{ width: 140 }}
                  value={form.category}
                  onChange={set("category")}
                  placeholder="如 quant"
                />
              </label>
            )}
            <label className="field">
              <span className="lbl">执行手 Hand（可选）</span>
              <select value={form.hand} onChange={set("hand")}>
                <option value="">（默认）</option>
                {HANDS.map((h) => (
                  <option key={h} value={h}>
                    {h}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="lbl">模型 Model（可选）</span>
              <input style={{ width: 160 }} value={form.model} onChange={set("model")} placeholder="如 opus" />
            </label>
          </div>
          <label className="field">
            <span className="lbl">覆盖方向 Focus</span>
            <input
              style={{ width: "100%" }}
              value={form.focus}
              onChange={set("focus")}
              placeholder="一句话覆盖范围，如：全球宏观与流动性"
            />
          </label>
          <label className="field">
            <span className="lbl">人设 Persona（注入提示词的人设段落）</span>
            <textarea rows={4} value={form.persona} onChange={set("persona")} />
          </label>
          <div className="form-row">
            <button onClick={submit} disabled={busy || !canSubmit}>
              {editing ? "保存修改" : "创建"}
            </button>
            <button className="ghost" onClick={cancelForm} disabled={busy}>
              取消
            </button>
          </div>
        </div>
      )}

      {analysts.loading && !analysts.data && <Loading />}
      {analysts.data?.length === 0 && <Empty text="还没有分析师" />}

      <div className="grid cols-3">
        {list.map((a) => (
          <div className="card analyst-card" key={a.id}>
            <div className="head">
              <span className="emoji">{a.emoji}</span>
              <div className="names">
                <div className="zh">{a.name}</div>
                <div className="en">
                  {a.name_en} · {a.id}
                </div>
              </div>
              <span className="badge role">{a.category}</span>
            </div>
            <div className="focus">{a.focus}</div>
            <details className="persona">
              <summary>人设 persona</summary>
              <div className="persona-text">{a.persona}</div>
            </details>
            {(a.hand || a.model) && (
              <div className="prefs mono">
                {a.hand && <span>执行手 {a.hand}</span>}
                {a.model && <span>模型 {a.model}</span>}
              </div>
            )}
            <div className="actions">
              {a.category !== "ops" && (
                <button className="small" onClick={() => runDaily(a)}>
                  运行日报
                  {dailyState(a.id) === "completed" && " ✓"}
                  {dailyState(a.id) === "failed" && " ✗"}
                </button>
              )}
              <button className="small ghost" onClick={() => startEdit(a)}>
                编辑
              </button>
              <button className="small danger" onClick={() => remove(a)}>
                删除
              </button>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
