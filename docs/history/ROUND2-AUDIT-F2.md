# ROUND2-AUDIT-F2 — 第二轮轮级终审（跨分区交互 / 全局不变量 / 主代理集成层）

- 审计代理：F2（深度代码审查视角，只读；与 S2 并行，S2 负责逐项核销与全量验证）
- 时间：2026-07-20 07:06–07:40 (UTC+8)
- 范围：B1–B8 跨分区交互、全局不变量、主代理 07:05–07:12 集成层
- 验证手段：AST 定位、git diff 对 HEAD、/tmp 等价迁移实测（空库全链 + 0007 停点增量）、compileall、定向 pytest ~200 项、全量 pytest 一次

## 结论：**FAIL（1 项 must-fix，范围小、方向明确；其余为 nits）**

全量测试当前 **2 failed / 385 passed / 9 skipped**。产品代码（8 个分区 + 集成层四类改动本身）未发现功能性缺陷；失败源于主代理集成收尾动作（backlog.json 卡状态推进）与既有测试的种子数据耦合，且更新后未复跑全量。修掉 F2-H1 即可转 PASS-WITH-NITS。

---

## Must-fix

### F2-H1（High）backlog.json 卡状态推进破坏 2 个 roadmap 测试，集成后未复验
- 现象：`tests/test_roadmap.py::test_claim_card_is_a_conditional_claim`（571–573 行）与 `::test_export_roundtrip_is_idempotent_and_rebuilds`（770、805–808 行）稳定失败，报
  `cannot claim a card in 'done'; only inbox/ready cards are claimable`（期望 match "blocked"）。
- 因果链（已实证）：HEAD 里 `M3-001 status = inbox` → 两测试把 **种子活卡 M3-001** 当 blocked/claim fixture（B6/B7 关闭时为绿）→ 主代理 07:12 集成收尾把 backlog.json 的 M3-001/M5-001/M7-006/M7-007 推进为 `done` → `claim_card` 在 blocked 检查之前先命中 done 拦截 → 断言失败。implementation-notes 07:12 记录的「全量 387 passed」是 backlog 更新**之前**的数字。
- 修复建议（二选一，倾向前者）：
  1. 测试改用自建临时卡做 blocked fixture —— 同文件已有先例（543–547 行注释 + `M7-TMPC`/`M7-TMPW`/`M7-TMP2` 模式），把 571/770 两处 `M3-001` 换成新建临时卡（`test_claim` 里 560 行的 `M7-006` owned-用例同样应换，它现在也是 done）；
  2. 或恢复测试假设不成立的卡不动状态（不推荐：backlog 是控制平面数据，推进是正当集成动作）。
- 附带事实：backlog.json 同时被整体重排版（紧凑 JSON → 缩进多行，疑似 export/import roundtrip 写回）。roundtrip 语义测试证明内容等价，但 diff 噪声大，建议 implementation-notes 补记一句归属。

---

## 逐重点结论

### 1. 集成层正确性 — PASS
- **4 处 memory_block 注入**全部合法：AST 证实各注入点都在 async 函数体内 —— `analyst_daily.run_one`(311 起，注入 324–328)、`whiteboard._run_card`(653 起，注入 692–698)、`mailbox._run_dispatch`(128 起，注入 167–171)、`workflows._drive`(187 起，注入 240–246)；`await memory.memory_block(...)` 均为合法 await 位置。
- **函数内 `from . import memory` 无环**：memory.py 只 import analysts/prompts/executor/db/bus，不回指任何宿主模块；sys.modules 缓存下重复 import 零成本。
- **whiteboard 注入未丢 `context_blocks or None`**（695 行原样保留，B3 PATCH-NOTES 警告的表达式仍在）。
- **异常面**：whiteboard/mailbox/workflows 的注入点都在各自 never-raise try 块内；analyst_daily.run_one 的注入不在 try 内但两条调用路径（`run_all._safe`、API spawn 前置校验）均有兜底。
- **refresh_weights_cache 预热时序正确**：main.py 里 `db.init()`(104，迁移含 0009 已应用) → `init_registry`(105) → 预热(109–110) → `executor.recover_orphans`(111)。与 registry 的 None 哨兵/一次告警/GET 自愈机制（REVIEW-B2 M3）闭环。
- **router 挂载无路径冲突**：19 个 router 全景核对 —— `POST /api/ask/stream`(ask_stream) 与 `POST /api/ask`(tasks) 精确路径不重叠且 tasks 的参数化路由都在 `/api/tasks/*` 下；`/api/institute/*` 前缀唯一（digests）；`/api/forecasts` 前缀唯一（market_data 用 `/api/market/*`、`/api/quote/*`、`/api/data/*`）。B8 的 `_prepare` 与 tasks.ask 预处理逐行镜像（analyst 404 对等、hand 优先级一致），mcp/sessions/ask 三处均不注入 memory —— 与"记忆只进日常循环"的口径一致。

### 2. 调度全景 — PASS（含一处口径更正 + 一个 nit）
- **口径更正：现在共 11 个 job**，非 12（8 旧含 janitor + 3 新）：cron×5（briefing 08:30 / daily-report 23:00 / analyst-dailies 19:00 / memory-compact 23:30 / hand-scorecard 00:05）+ interval×6（whiteboard-kickoff 60m / whiteboard-tick 60s / mailbox-sweep 120s / research-tick 30m / market-refresh 60m / janitor 60m）。
- **gated/ungated 决策表**与 A4 轮判据（gated=启动新模型调用）逐项一致：memory-compact gated=True（compact_one 走 executor.submit 花配额）✓；hand-scorecard ungated（终态行 QA，零模型调用）✓；market-refresh ungated（纯抓数）且 `market_fetch_enabled` kill switch 在 `refresh_all` 内部生效（config 注释所述语义成立）✓；scheduler.py 107–116 行的门控注释已同步更新三个新 job。
- **时序依赖合理**：19:00 dailies → 23:30 compact → 00:05 scorecard（结算前一日闭集，REVIEW-B2 M2 语义落地）。边界场景：单手串行时 dailies 最坏 4h35m，23:30 compact 可能拿不到当天日报 —— 但 B3 的单调 id 游标使漏掉的材料**下一轮补取而非丢失**，可接受。
- **market-refresh 与 janitor 同为 60m interval**：写入面无交集（价格表 vs cron_metrics/topic_pool/workflow_runs/工作区清理），APScheduler 各 job max_instances=1 互不干扰。
- Nit F2-N1：hand-scorecard 的 "00:05" 是 scheduler.py:282 硬编码，是唯一没有 config 字段的 cron job（其它均可置空禁用）。建议下轮补 `scorecard_time` 设置项。

### 3. prompt 组装全链 — PASS
- 4 个调用点全部通过 `build_analyst_prompt(memory_block=...)` 参数注入，三明治顺序由该函数唯一实现保证：**anchor → persona → memory → context → task → CITATION_MANDATE → file deliverable**，不存在各点自行拼接的分叉。
- BUILD-ON 块（B4）作为 `context_blocks[0]` 进入 —— 与 memory 共存时顺序为 persona → memory → BUILD-ON → 前序卡片摘要 → task，语义正确（记忆属于"我是谁"，BUILD-ON 属于"本次任务背景"）。
- `${DATA_BUNDLE}`（B5）在 `_drive` 的变量替换层作用于 **step prompt 文本**（workflows.py:227），先于 memory 参数拼接且互不触碰；memory compact_md 里即使出现 `${...}` 字样也不会被替换（substitute_variables 只喂 step prompt）。惰性计算 + 条件持久化（status='running' 守卫）正确。
- memory.compact_one 自身把前一版记忆放 context_blocks（而非 memory_block 参数）—— 压缩任务的语义正确（记忆在这里是待压缩材料）。
- 硬规则 4 复核：prompts.py 对 HEAD 的 diff **仅** memory_block 参数 + docstring 更新 + 6 行插入逻辑；全部既有常量与 f-string 逐字未动。

### 4. 停机 drain 终版全景 — PASS
- 全仓 `create_task|ensure_future` 共 9 处，逐一核销：whiteboard:77→`_bg_tasks`、workflows:139→`_driving`、mailbox:34→`_bg_tasks`、analyst_daily:416/422→`_background`、research:57→`_bg_tasks`（shielded tick）、executor:274/300→`_running`、archive:42→`_bg_tasks`。**B2 scorecard / B5 fetchers / B6 forecasts 零新建游离 task**（全部直调 await，被宿主 job/请求任务覆盖）。
- 两处注册表外任务均有 owner 语义：analyst_daily:375 heartbeat 由 run_all 的 finally（stop event + await）管理，run_all 本身必在 `_background` 或 scheduler inflight 快照内；ask_stream:149 外层 submit 包装是**有意的** fire-and-forget（模块 docstring 声明），内层 `_execute` 在 `_running` 中被 drain 取消后外层链式收尾、done-callback 消费异常。
- `main._drain_background` 7 组注册表 + `extra=scheduler.inflight_jobs()` 快照（shutdown 之前取）+ 两轮清扫 —— 与 conftest teardown 的 7 组完全同集（conftest 83 行注释自证同步）。

### 5. 迁移链 0001–0014 — PASS
- 编号无冲突且各文件头部注明归属与"gaps are fine"约定：0008=B1、0009=B2、0010=B3、0011=B4、0012/0013=B6、0014=B5。
- **空库全链实测**：14 个全部应用，11 张新表齐备，`PRAGMA integrity_check` ok。
- **升级库实测**（模拟 0007 记账停点）：增量恰好应用 0008–0014 七个，topic_pool 拿到 0011 的 category/similarity_* 五列，integrity ok。
- B1 迁移纪律测试（含"迁移文件禁 BEGIN/COMMIT"断言）对全部 14 个文件通过（test_db_migrate.py 21 项绿）。
- 0001–0004（tracked）对 HEAD 零 diff；0005–0007（untracked，第一轮产物）mtime 停在 04:12/04:39，第二轮未触碰。

### 6. 硬规则全局扫 — PASS-WITH-NITS
- prompts.py：见重点 3，仅 B3 的参数改动 ✓。新常量（MEMORY_COMPACT_TASK/MEMORY_BLOCK_TEMPLATE/BUILD_ON_PRIOR_BLOCK）全部住在新模块或标注 NOT-a-modification，旧字符串零改动 ✓。
- **F2-M1（Medium，计划外偏差非缺陷）：workflows/*.json 非零字节** —— 三个 json 的 step 键 `"analyst"` → `"analyst_id"`（prompt 文本逐字未动，diff 中 prompt 行均为上下文行）。功能安全：`_normalize_steps` 折叠 legacy 键、`_drive` 双键容错、CLAUDE.md:33 本就声明 `analyst|analyst_id` 双形态合法（A4 轮 R4 nit N2 引入的规范化配套）。但与本轮"零字节"预期不符，应在 implementation-notes 明确归属并让 S2 核对 prompt 字段字节级等价（本审计已抽查 diff 确认 prompt 行未动）。
- rate_limits 持久化（never-shorten、60s floor）、`get_cli_env`、per-CLI 签名：hands/rate_limit.py、hands/base.py 本轮零修改（git status 无 M）；registry.py 的改动仅新增 weighted-pick 区段，cooldown/breaker/resolve 逻辑逐行未动 ✓。
- VaultWriter 五规则：writer.py 属 B3 分区（R-B3 已审）；test_vault.py 21 项全绿；exporter 新增 memory.md 导出走 `region=True`（rule 4 managed regions），rows-are-truth 方向正确 ✓。

### 7. 事件面 — PASS-WITH-NITS
- 新事件消费方核对：`memory.compacted` → exporter._on_memory（region 模式写 `Analysts/<id>/memory.md`）✓；`scorecard.completed`/`market.refreshed`/`forecast.created`/`forecast.settled` 无后端消费方 —— 纯观测事件（events 表 + SSE），无隐性依赖，OK。
- memory 三个采集游标的上游 payload 契约逐一核对成立：`analyst_daily.completed`(session_id/file/task_id/date)、`whiteboard.card_completed`(analyst_id 于 payload)、mailbox reply 行(author=analyst_id, kind='reply') ✓。
- **F2-M2（Medium，观测缺口）：前端 `useSSE.ts` KNOWN_EVENT_TYPES 进一步落后** —— F1 已提，本轮后端又新增 5 类（memory.compacted、scorecard.completed、market.refreshed、forecast.created、forecast.settled），叠加此前已缺的 analyst_daily.completed/failed/sweep_completed、research.followups，现缺 ≥9 类。EventSource 按 named event 注册 listener，缺失类型**不进前端实时流**（`?since=` 游标回放不受影响）。影响：SPA 事件页对第二轮全部新功能不可见；不破坏后端。建议下轮一并补齐或改为服务端下发类型清单。

---

## 问题分级汇总

| 级别 | 编号 | 位置 | 摘要 |
|---|---|---|---|
| High (must-fix) | F2-H1 | tests/test_roadmap.py:571,770 ↔ roadmap/backlog.json | backlog 卡状态推进(done)破坏 2 个用种子活卡 M3-001 当 fixture 的测试；集成后未复跑全量。全量当前 2 failed |
| Medium | F2-M1 | workflows/*.json | `analyst`→`analyst_id` 键名规范化使"零字节"预期落空；prompt 文本未动、代码双键兼容，无功能风险，需补记归属 |
| Medium | F2-M2 | frontend/src/useSSE.ts:7 | KNOWN_EVENT_TYPES 缺 ≥9 类事件，第二轮新功能在前端实时流全盲 |
| Low/Nit | F2-N1 | app/institute/scheduler.py:282 | hand-scorecard cron "00:05" 硬编码，唯一无 config 开关的 cron job |
| Low/Nit | F2-N2 | app/institute/analyst_daily.py:375 | heartbeat task 不入 drain 注册表（owner finally 管理，正常路径安全）；仅作记录 |
| Low/Nit | F2-N3 | app/api/ask_stream.py:149 | 外层 submit 包装任务注册表外——有意设计（断连不取消），依赖内层 `_running` 链式收尾；仅作记录 |

## 第三轮建议清单

1. **修 F2-H1**（必须）：test_roadmap 两处 blocked fixture 换自建临时卡（M7-TMPC 先例），恢复全量绿；顺手把 test_claim 里 M7-006 的 owned-用例也换掉，消除对种子卡状态的最后一处耦合。
2. **前端事件清单**（F2-M2）：补 KNOWN_EVENT_TYPES 或改造为后端提供类型枚举端点，前端动态注册。
3. **scorecard_time 配置化**（F2-N1）：补 config 字段，保持与其它 cron job 一致的可禁用性。
4. `_prepare`/`tasks.ask` 共享 helper 抽取（PATCH-NOTES-B8 遗留）：两处 lockstep 注释是脆弱契约。
5. registry.pick_weighted_hand 目前零调用方（opt-in 落位但未接线）——按 PATCH-NOTES-B2 规划把 whiteboard/research/daily/mailbox 轮转接上，或立卡跟踪，避免 0009 三表长期空转。
6. F1 遗留的 scheduler 公共访问器建议（PATCH-NOTES-A1）与 CLAUDE.md 过时 Gotcha（PATCH-NOTES-A3）仍未消化，建议第三轮文档收尾一并处理。

## 验证记录

- `.venv/bin/python -m compileall app -q` ✓
- 空库全链迁移 + 0007 停点增量迁移（等价 /tmp 实测于工作区临时目录，已清理）：ledger 14/14、增量 7、integrity ok ✓
- 定向：test_db_migrate/test_cron_metrics/test_memory/test_hand_weights/test_digests = 67 passed ✓
- 定向：test_analyst_daily/test_whiteboard(+similarity)/test_workflows/test_mailbox/test_maintenance/test_executor_shutdown/test_forecasts/test_market_fetchers = 133 passed, 1 skipped ✓
- 定向：test_vault/test_research/test_archive = 绿；test_roadmap = **2 failed**（F2-H1，单跑复现，非隔离问题）
- 全量：`pytest tests -q` = **2 failed / 385 passed / 9 skipped**（15.1s）
