# PATCH-NOTES-M8-012 — 持久化多代理组/run 记录 + Committee Vault 导出

来源：`roadmap/backlog.json` 卡片 M8-012（S4-P2-06/07 + ROADMAP Phase 7 Committee 遗留：
multi-agent 无持久 group/run 行、无重连与部分 spawn 恢复；committee 无 Committee Vault
导出与输入快照；majority 裁决偏 free-text）。

## 改动清单（全部在授权分区内）

- `migrations/0027_multi_agent_groups.sql`（新建）— 两张表：
  - `multi_agent_groups`：命名常设面板（成员分析师 JSON 数组 = fan-out 顺序 + 路由策略
    join mode / 可选 hand override）。删除组不动历史（runs 的 `group_id` ON DELETE SET NULL）。
  - `multi_agent_runs`：每次 fan-out（及每周 committee run）一行——`prompt` 即输入快照、
    `task_ids`（agents 序）、`verdict`（结构化 JSON）、状态机 running → completed/failed
    全部条件认领（硬规则 2）。`workflow_run_id` UNIQUE（NULL 各自独立）是 committee
    桥接的幂等仲裁。编号 0027：0026 被并行卡占用，sorted 应用、缺号无碍。
- `app/institute/multi_agent.py` —
  - **结构化 majority 裁决**：`extract_ballot()`（取输出中最后一行 `VERDICT: <token>`，
    半/全角冒号、大小写不敏感；无此行退回整段 strip 文本 = 旧行为）；`join()` 的
    majority_vote 改用结构化 ballot 计票，结果新增 `ballots` 计票表 + 每个 output 的
    `ballot` 字段。旧「exact-match only」测试语义原样保留。
  - **组 CRUD**：`create_group / list_groups / get_group / update_group / delete_group`
    （名称单行化+UNIQUE、成员 ≤5 且查 roster、无重复、mode 校验）。
  - **持久 run**：`start_run()`（意图行先落库，再逐 agent spawn，每个 task 带
    `parent_run_id`=run id——链接与 task 行同一 INSERT 原子落地；spawn 中途失败把已
    spawn 的 id 记进 failed 行再抛出 = 部分 spawn 有账可查）；`settle_run()`
    （settle-on-read 重连路径：任务全部终态才条件认领写入精简结构化 verdict；
    `task_ids` 丢失时从 tasks 表按 `parent_run_id` 复原；比 agents 少的 'running' 行
    只在超过 `RUN_SPAWN_STALE_S`=600s 且已 spawn 任务全终态时判 failed，避免和
    spawn 中的写手竞态）；`get_run_record / list_run_records / run_outputs`。
  - **committee 桥**：`ensure_committee_group()`（从 reconciled 定义 upsert 系统组
    'committee'，成员=步骤分析师首现顺序）；`open_committee_run()`（kickoff 幂等开
    记录，INSERT OR IGNORE，绝不抛）；`finalize_committee_run()`（先 upsert 兜住手动
    escape-hatch 路径，再条件认领写：输入快照=run 冻结的 `${WEEK_DISPUTES}`、步骤
    task ids、结构化 step-map verdict、completed→completed / failed/cancelled→failed）。
- `app/vault/exporter.py` — 新 bus handler `_on_committee`，注册在 `workflow.` 前缀并
  自过滤：`workflow.started` 开 committee 记录；terminal 事件先 settle 记录（rows are
  truth，vault 关闭也照做），仅 `workflow.completed` 再写
  `Committee/<WORK_DATE> 委员会裁决.md`——裁决正文（工作区 `委员会裁决.md`，缺失退
  step 摘要）+「## 输入快照（当周白板研讨摘要）」+ 档案 footer + chain entity footer；
  写入全走 VaultWriter（`managed: institute`、hash ledger，五条安全规则不变）。
- `app/api/multi_agent.py` — 原 `POST /run` 契约不变（200/202/400/422 原样），响应
  增量携带 `run_id`；新增：`POST/GET /groups`、`GET/PUT/DELETE /groups/{id}`、
  `POST /groups/{id}/run`（用组内存的路由策略跑面板）、`GET /runs`（history，
  group_id/status 过滤）、`GET /runs/{run_id}`（重连读：settle-on-read + 从 tasks 行
  回读全文 outputs）。
- 测试：`tests/test_multi_agent.py` +15、`tests/test_committee.py` +5、
  `tests/test_exporter_handlers.py` +5（另更新 degrade 清单与 register 布线断言至 10 项）。
  **共 25 个新用例**，三文件合计 64 全绿。

## 刻意不改的

- `app/institute/scheduler.py` **零改动**：committee 记录的开/结不走 scheduler——
  `workflows._create_run` 本来就 emit `workflow.started`，exporter 的 `workflow.` handler
  在 lifespan 注册后对 scheduler 与手动 API 两条触发路径一视同仁；比在
  `_committee_job` 里接线覆盖面更全（手动 run 也有 'running' 期记录）。
  （工作区里该文件当前的 factcheck-outbox diff 是并行卡的改动，与本卡无关。）
- `workflows/committee.json` 与全部 prompt 逐字未动（硬规则 4）；`app/institute/workflows.py`
  未动（卡片 expected_files 提到它，但本次授权边界排除——见「遗留风险」）。
- `roadmap/backlog.json`、`CHANGELOG.md`、`app/main.py`、frontend 均未动。

## 验收标准逐条对照

1. **multi-agent groups/runs are durable rows supporting reconnect** ✅
   0027 两表 + `start_run` 意图行先行 + `parent_run_id` 原子链接；202 响应带 `run_id`，
   `GET /api/multi-agent/runs/{id}` settle-on-read（测试：`test_api_202_reconnect_settles_on_read`、
   `test_settle_run_is_reconnect_safe`、部分 spawn 三连测）。
2. **committee runs export a Committee/ vault note with an input snapshot** ✅
   `_on_committee` → `Committee/<WORK_DATE> 委员会裁决.md`，含「## 输入快照（当周白板
   研讨摘要）」=run 冻结 `${WEEK_DISPUTES}`；记录侧 `multi_agent_runs.prompt` 同快照
   （测试：`test_committee_completed_exports_note_with_input_snapshot` 等 5 个 handler 用例 +
   `test_committee_run_lands_durable_record_with_input_snapshot`）。
3. **majority verdicts are structured, not free-text** ✅
   `VERDICT:` 行提取 + `ballots` 计票表 + 每 output `ballot`，并以精简结构化 JSON 持久到
   `multi_agent_runs.verdict`（全文只存 tasks 行，verdict 只存 ref/status/ballot）
   （测试：`test_join_majority_vote_structured_ballots` 等 3 个 + verdict 持久断言）。

## 验证

- `.venv/bin/python -m pytest tests/test_multi_agent.py tests/test_committee.py tests/test_exporter_handlers.py -q` → **64 passed**
- `.venv/bin/python -m pytest tests/test_db_migrate.py -q` → 19 passed（0027 过原子迁移/重放/无事务语句约束）
- `.venv/bin/python -m pytest tests/test_api_routes.py tests/test_workflows.py -q` → 15 passed（全路由冒烟吃下新端点）
- `.venv/bin/python -m compileall app -q` → 通过

## 遗留风险 / 后续卡建议

1. **workflow 输出文件仍非引擎契约**（S4-P2-06 的第三小项）：`_drive` 主循环与
   `app/institute/workflows.py` 在本卡授权之外，2–4 步 prompt 的文件链降级说明照旧
   （REVIEW-C5 P2 原样遗留）——建议单独开 prompt/engine 卡。
2. **janitor 直接改 workflow_runs 状态不发 bus 事件**：被 janitor 判死的 committee run
   其记录停在 'running'，直到任一次 `GET /runs/{id}`（settle-on-read 会走
   `finalize_committee_run` 补账）。列表页只读不 settle，属已记录的惰性语义。
3. **VERDICT 行约定尚未进任何生产 prompt**：majority 结构化收敛需要 prompt 尾行
   `VERDICT: <方向>` 配合；改 prompt 超出本卡（硬规则 4），建议随 prompt 卡落地。
4. **committee 组成员漂移**：`ensure_committee_group` 每次 committee 事件按当前
   workflow 定义 upsert agents；操作员若手工改该组成员会在下次 committee run 被覆写
   （name/description 不会被覆写）。系统组语义已在迁移头注释与 docstring 声明。
5. `GET /runs` 列表为廉价投影不做 settle：刚跑完但没人读过单条的 run 在列表里短暂显示
   'running'，读一次单条即收敛。
