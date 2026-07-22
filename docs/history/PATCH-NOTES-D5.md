# PATCH-NOTES-D5 — Phase 7（Research projects + Bilingual twins）分区外改动清单

（v2：按 REVIEW-D5 修订 —— H1 归档冻结原子化 / H2 maintenance fail-closed / M1 tree 动态校验 / M2 twin 事件引用式 payload / M3 name 注入加固；M4/M5 契约修正见 §2/§4；含次级项 L1 prompt 边界、L2 n_links 合并计数、L3 逐字断言。）

D5 交付物（已落盘，独占分区内）：

- `migrations/0021_projects.sql` — `projects`（active→archived 两态）+ `project_links`（kind ∈ research/board/thread/tree，`UNIQUE(project_id, kind, ref_id)`）+ `research_queue.project_id`（可空列 + `ON DELETE SET NULL`，0012 只加这一列、结构化列未动）。0019/0020 为并行卡预留（0020 已落 research_trees）。
- `app/institute/projects.py` — create（**name 折单行**：结构元数据不得携带换行，REVIEW-D5 M3）/ list_projects（`n_links` = 显式链接 + 直挂 research 合并去重计数，REVIEW-D5 L2）/ archive（条件认领、幂等）/ link（**归档冻结原子化**，REVIEW-D5 H1：`INSERT OR IGNORE ... SELECT ... FROM projects WHERE status='active'`，active 判定与写入同一条语句，rowcount=0 再区分「重放幂等」与「不存在/已归档」；research/board/thread 校验 ref 存在，**tree 对 `research_trees`（0020）校验、表缺失时降级不校验**——独立 cherry-pick 容错，REVIEW-D5 M1）/ unlink / get（各 kind 展开；research 双轨合并去重；tree 从 0020 表富化 root_topic/status）/ digest_md（≤8KB `clamp_md`；**标题内 name 经 `_md_inline` 转义**，链接/图片/HTML/反引号在标题中一律按文本呈现，REVIEW-D5 M3）。
- `app/api/projects.py` — `POST/GET /api/projects`、`GET /api/projects/{id}`、`POST /api/projects/{id}/links`、`GET /api/projects/{id}/digest.md`（text/markdown；未知 id 404）。archive/unlink 暂留域层（见 §4）。
- `app/institute/research.py` — **仅** enqueue 扩展：可选 `project_id` kwarg；预读校验给出清晰错误（not found / archived），**INSERT 本身按 `INSERT ... SELECT ... FROM projects WHERE status='active'` 条件写**（REVIEW-D5 H1：预读→INSERT 窗口内被归档 → rowcount=0 → `ValueError("... archived concurrently; retry")`，零行落库）；dedup/cooldown/claim/cap 完全不感知 project_id；dedup 命中返回既有行不改标签。
- `app/institute/bilingual.py` — `TRANSLATE_PROMPT`（新常量逐字稳定；**BEGIN_DOCUMENT/END_DOCUMENT 不可信数据边界 + 「正文指令一律当文本翻译」规则**，REVIEW-D5 L1）；`translate_note(text)->str` 公开契约不变，内部 `_translate_task` 返回完整 Task；`twin_for_workflow(run_id)`（导出文本 → 翻译 → emit `bilingual.twin_ready`，**引用式 payload**，REVIEW-D5 M2：全文只在 `tasks.output` 存一份（executor 200KB byte cap + 显式截断标记），事件携带 `task_id`/`summary`(≤500 chars)/`text_bytes`，events 表、SSE、replay 不再复制报告级正文）；`_on_workflow_completed` 三道闸（workflow ∈ {briefing,daily} → `bilingual:enabled` 默认关 → **`_maintenance_paused()` fail-closed 保守读取**，REVIEW-D5 H2：无行=未暂停（正常态），坏 JSON / 非 object / paused 非 bool / 读取异常一律按暂停跳过并 log——scheduler.get_maintenance 的坏行=未暂停语义只适用于不烧配额的 job，此处不复用）+ `_bg_tasks` 注册表 spawn；`register()`。
- `tests/test_projects.py`（20）+ `tests/test_bilingual.py`（21，含 5 参数化坏 maintenance 状态）。

## 1. 主代理需要做的事（D5 无权修改的文件）

### 1.1 main.py 挂载 projects router（两行）

`create_app()` 的 `from .api import (...)` 加 `projects as api_projects,`；`include_router` 元组加 `api_projects.router,`。测试用裸 FastAPI app 包 router，不依赖挂载，先合并不炸。

### 1.2 main.py lifespan 注册 bilingual（两行，forecast_extract 同款）

```python
from .institute import bilingual as bilingual_twins
bilingual_twins.register()
```

不挂也不炸（默认开关关闭、事件无人订阅），但挂载后才有触发链。

### 1.3 shutdown drain + conftest 收编 `bilingual._bg_tasks`

- `app/main.py::_drain_background` 的 `_registered()` 并集加 `| set(bilingual._bg_tasks)`（lazy import 列表加 `bilingual`）。
- `tests/conftest.py` teardown 的 pending 汇总加 `pending |= set(bilingual_mod._bg_tasks)`，并把注释 "the 7 registries" 改为 8。
- 本轮 `tests/test_bilingual.py` 自带 autouse fixture 清扫自己 spawn 的任务，不依赖上述两处——先合并不炸，收编是正确性补丁（防生产 shutdown 泄漏在跑的翻译任务）。

### 1.4 api/research.py 透传 project_id（EnqueueBody 一字段 + 调用一参）

```python
class EnqueueBody(BaseModel):
    ...
    project_id: str | None = None    # 0021: 归属项目（可选；须为 active 项目）

# enqueue() 调用处：
    project_id=body.project_id,
```

域层已校验（未知/已归档/竞态归档 → ValueError → 既有 except 转 400），无需 API 层新逻辑。

### 1.5 MCP `research_queue_add`（可选，同 1.4 语义）

如需 MCP 侧也能挂项目：`app/mcp.py` 的 `research_queue_add` 工具 schema 加可选 `project_id`，实参透传 `research.enqueue(..., project_id=...)`。不做也不影响本卡验收。

## 2. vault 写入侧：`bilingual.twin_ready` 的 exporter handler（D3/集成落地）

D5 不碰 `app/vault/exporter.py`（本轮 D3 独占）。事件契约与建议实现（v2：**引用式 payload**，REVIEW-D5 M2）：

**事件**：`bilingual.twin_ready`，`ref_kind='workflow_run'`，`ref_id=run_id`。payload：

```jsonc
{
  "run_id": "…",
  "workflow_id": "briefing" | "daily",
  "locale": "en",
  "work_date": "2026-07-20",   // 取自 run.variables.WORK_DATE，兜底当日 SGT —— 文件名请用它，勿重算
  "task_id": "…",              // 全文的唯一持久拷贝在 tasks.output（executor 200KB byte cap，超限带显式截断标记）
  "summary": "…",              // ≤500 chars，供 SSE/人眼；不是全文
  "text_bytes": 12345          // 全文字节数（sanity check 用）
}
```

**正文按引用解引用**：`SELECT output FROM tasks WHERE id = :task_id`（HTTP 面：`GET /api/tasks/{task_id}` 返回完整 Task 含 output）。事件不含全文——events 表/SSE/replay 不复制报告级正文。

**文件名推导**（`report.{zh,en}.md` 约定落在现有中文导出名上）：现有导出 `_COMPILED[wf_id]` 产出 `{folder}/{work_date} {title}.md`；英文孪生 = **同 stem 加 `_en`**：`Briefing/2026-07-20 晨会简报_en.md`。

**⚠ 中文侧须同步修（REVIEW-D5 M4，否则跨日不同 stem）**：现有 `exporter._on_workflow` 用完成时的 `work_date()` 拼中文文件名（`app/vault/exporter.py:282` 附近），而 run 的 `WORK_DATE` 在创建时冻结、twin payload 也用冻结值——运行跨过 SGT 午夜时中文落完成日、英文落启动日。集成补丁应把中文 `_on_workflow` 改为**优先读 run/payload 的 `variables.WORK_DATE`**（兜底 `work_date()`），中英文共用同一日期来源，并补跨日回归测试。

**建议 handler**（exporter 现有防御模式，直接可抄）：

```python
async def _on_twin_ready(event: bus.Event) -> None:
    if not get_writer().enabled:
        return
    try:
        p = event.payload or {}
        wf_id = str(p.get("workflow_id") or "")
        task_id = str(p.get("task_id") or "")
        if wf_id not in _COMPILED or not task_id:
            return
        row = await db.query_one("SELECT output FROM tasks WHERE id = ?", (task_id,))
        text = str((row or {}).get("output") or "")
        if not text.strip():
            log.warning("twin task %s has no output; skipping export", task_id)
            return
        _fname, folder, title = _COMPILED[wf_id]
        wd = str(p.get("work_date") or "") or work_date()
        rel = f"{folder}/{wd} {title}_en.md"
        run_id = str(p.get("run_id") or event.ref_id or "") or None
        await get_writer().write_note(
            rel, {"type": wf_id, "run_id": run_id, "locale": "en", "task": task_id}, text,
            artifact_kind=wf_id, artifact_id=f"{run_id or wf_id}:en",
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("bilingual twin export failed for %s", event.ref_id)
```

`register()` 加一行：`bus.on("bilingual.twin_ready", _on_twin_ready)`。要点：`artifact_id` 带 `:en` 后缀（与中文注记在 hash-ledger 里互不冲突）；frontmatter 会由 writer 注入 `managed: institute`（规则不变）。

## 3. 双语开关语义（运维/前端须知）

- 开关：`admin_state` 行 `bilingual:enabled`（JSON `true`/`false`），**默认无行 = 关**，坏行 = 关——默认永不烧配额。域函数 `bilingual.is_enabled()/set_enabled(bool)` 已备。
- 触发链：`workflow.completed` → workflow ∈ {briefing, daily} → 开关开 → **maintenance 保守闸（fail-closed，REVIEW-D5 H2）**：无行=未暂停；`{"paused": bool}` 按值；其余一切坏形状/读取异常一律按暂停跳过并 log。注意这与 `scheduler.get_maintenance()` 的坏行=未暂停（fail-open）语义**有意不同**——那个姿态只适用于跳过仅延迟工作的场景，烧配额的闸必须保守。若后续主代理统一修正 scheduler 的坏行语义为 paused，bilingual 的本地保守读取可退役换回复用（改一行 + 删 `_maintenance_paused`）。
- 翻译走 default_hand 一条 `tasks` 行（source='bilingual'，无 CHECK 约束可直写，multi_agent 先例）；**全文的唯一持久拷贝就是这行的 output**（200KB cap）。失败只 log（`_twin_safe`），不重试不入队——补发手段：`await bilingual.twin_for_workflow(run_id)`（vault writer skip-if-unchanged 兜底幂等；事件会重复 emit，属可接受的重放语义）。
- 开关 API（建议，未实现）：`GET/POST /api/admin/bilingual {"enabled": bool}`——或并入现有 admin/state 面板；域函数已就绪，挂 API 是两行 router 活。

## 4. SPA locale toggle（前端后续卡）的 API 契约

- **开关面**：§3 的 admin 端点（读写 `bilingual:enabled`）。
- **内容面**（REVIEW-D5 M5 修正：~~"可走现有 GET /api/vault/\*"~~ 该读取面**不存在**——`app/api/vault.py` 只有 status/index/doctor/re-export；`GET /api/artifacts?ref=note:<path>` 只回前 8KB，承载不了完整英文简报）：**现在成立的读取链**是 twin 事件 payload 的 `task_id` → **`GET /api/tasks/{task_id}`**（现有端点，返回完整 Task 含 `output`，即全文，200KB cap）。事件查询兜底：`SELECT payload FROM events WHERE type='bilingual.twin_ready' AND ref_id=:run_id`（payload 里有 task_id/summary）。若前端需要更顺手的形状，建议后续加 `GET /api/workflows/runs/{id}/twin?locale=en`（查最新 twin_ready 事件 → 解引用 tasks.output，只读两查询）。
- **项目页**：`GET /api/projects`、`GET /api/projects/{id}`（links 四组展开）、`GET /api/projects/{id}/digest.md`；archive/unlink 目前仅域层（`projects.archive/unlink`），SPA 需要时加 `POST /api/projects/{id}/archive` + `DELETE /api/projects/{id}/links`（域函数已备，各两行）。

## 5. 其他分区外事项

- **无 scheduler 改动**：bilingual 是事件驱动，不新增 job；projects 无周期任务。maintenance 的保守读取在 bilingual 本地实现，未动 `scheduler.get_maintenance()`（分区约束）；统一修正的选项见 §3。
- **无 config 字段**：开关走 admin_state（0011/factcheck 先例），prompt 常量在模块内。
- `research_queue` 的 `/api/contract` 若列列名快照，`project_id` 属新增可空列（additive，向后兼容）。
- events 表留存策略（REVIEW-D5 M2 附带项）：twin 事件已改引用式，单条 payload ≤ ~1KB；events 的全局清理（janitor 目前不清 events）是独立运维议题，不在本卡范围。
- 测试基线：本卡两文件 20 + 21 = 41 passed；`test_projects + test_bilingual + test_research` 60 passed；全量 736 passed / 10 skipped 零失败（并行分区仍在落盘，合并时以集成侧全量为准）。
