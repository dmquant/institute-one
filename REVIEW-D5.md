# REVIEW-D5 — Phase 7 Research projects / Bilingual twins 独立审查

## 结论：FAIL

定向验证全部通过（46 passed，compileall exit 0），主路径实现也大体清楚；但有两个必须修复的正确性问题：

1. 项目归档与 `link()` / `research.enqueue(project_id=...)` 之间存在可复现的 TOCTOU，项目已变成 `archived` 后仍能落新关联；
2. maintenance 行损坏或 JSON 形状错误时，`scheduler.get_maintenance()` 返回 `False`，双语 handler 会继续启动新模型调用，违背配额场景应保守暂停的要求。

此外，当前 checkout 已有 0020 树表却仍允许任意 tree ref、全文 twin 事件会膨胀事件表且“大于 executor cap 仍全文”的契约不成立、项目名可注入 Markdown 结构；PATCH-NOTES 的中英文同 stem 与现有 Vault 内容读取 API 也有事实偏差。

## 逐项裁决（每项一行）

- projects ref 表名：**PASS** — research/board/thread 分别正确指向 `research_queue`、`whiteboard_boards`、`mailbox_threads`。
- tree ref 取舍：**FAIL（中）** — `migrations/0020_research_tree.sql` 已先于 0021 提供 `research_trees`，当前集成态继续不校验会制造悬空链接。
- link 幂等：**PASS** — `UNIQUE(project_id,kind,ref_id)` + `INSERT OR IGNORE` + rowcount 的顺序重放语义正确。
- archive 条件认领：**PARTIAL / FAIL（高）** — UPDATE 本身有 `status='active'` 条件，但 active 预读与后续 link/enqueue INSERT 不原子，冻结不成立。
- archived 后顺序拒绝：**PASS** — `tests/test_projects.py:132-134,214-220` 覆盖 link 与 enqueue；缺并发交错回归。
- research 双轨合并：**PASS** — 当前 `UNION` 的去重键是五个投影列的整行；两轨都读取同一 `research_queue` 行，当前效果等同按 queue id 去重。
- digest 8KB：**PASS** — 复用 byte-aware `clamp_md()`，测试覆盖 CJK 超长正文。
- digest 注入面：**FAIL（中）** — project name 未折单行或转义，换行、链接、图片、HTML 等 Markdown 结构可直接进入标题。
- bilingual 开关：**PASS** — 无行、JSON 坏行均为关，且真实测试覆盖；只有 JSON `true` 才开启。
- bilingual maintenance：**FAIL（高）** — 正常 paused=true 覆盖，但坏行被 scheduler 降级为“未暂停”，实测会 spawn。
- `_bg_tasks`：**PASS** — 强引用集合 + `done_callback(discard)` 会自清；PATCH-NOTES 的 main/conftest drain 收编位置正确。
- twin payload：**FAIL（中）** — events 无 payload 上限/清理；默认单条虽受 executor 200KB cap 间接约束，但累计增长，且“全文永不截断”在超过 cap 时不成立。
- TRANSLATE_PROMPT：**PASS-WITH-NIT** — 新常量、唯一 executor 路径正确；待译正文没有闭合的不可信边界，仍有低概率指令注入面。
- research.py 最小扩展：**PASS-WITH-HIGH-ISSUE** — 可归因改动仅 kwarg/规范化/active 校验/INSERT 列/FK 文案；dedup、cooldown、claim、cap 未引入 project_id，但 active 校验存在归档竞态。
- migration 0021：**PASS** — 纯 additive、无 B1 禁用语句；`project_id` 可空且 `ON DELETE SET NULL` 保留研究历史合理。
- 硬规则：**PARTIAL** — executor、`bus.now_iso()`、handler never-raise、既有 prompt 不改均通过；项目冻结原子性与 maintenance 保守门控未通过。
- projects API：**PASS（待集成）** — 裸 router 契约和测试正确；当前 `app/main.py` 尚未挂载，PATCH-NOTES 的两处 import/include 指示正确。
- exporter / locale API 规格：**PARTIAL / FAIL（中）** — handler 骨架、`:en` artifact id、writer frontmatter 方向正确；日期同 stem 与现有读取 API 的描述不成立。

## 问题分级

### H1（高 / must-fix）归档冻结存在 TOCTOU，归档后仍可新增 link 和 enqueue

- 位置：
  - `app/institute/projects.py:93-103`：archive 条件 UPDATE；
  - `app/institute/projects.py:106-136`：先 `_require_active()`，后独立 INSERT；
  - `app/institute/research.py:145-150,198-204`：先查 active，经过 dedup/cooldown 后才 INSERT。
- 两条写路径都把“active 判定”和“新增行”放在不同语句中；`db._write_lock` 只包单次写，无法锁住中间窗口。
- 确定性交错探针在 active 查询后暂停调用，先执行 archive，再恢复调用，结果为：
  - `link_after_archive=True status=archived`
  - enqueue 返回非空 `project_id`，同时项目状态已是 `archived`
- 现有测试只覆盖 archive 完成后再发起调用，没有覆盖“active 预读 → archive → INSERT”。
- 修复要求：让新增写与 active 条件在同一原子边界线性化。可用 `INSERT ... SELECT ... FROM projects WHERE id=? AND status='active'` 并按 rowcount 区分 archived/duplicate，或用 `db.transaction()` 把 active 校验和 INSERT 包在同一写锁事务；link/enqueue 各补一条 barrier 并发测试。

### H2（高 / must-fix）损坏的 maintenance 状态会 fail-open 并烧配额

- `app/institute/scheduler.py:33-40` 对缺行返回 `False` 合理，但对坏 JSON、非 object JSON 等所有异常也返回 `False`，注释明确写着 “corrupt state means not paused”。
- `app/institute/bilingual.py:201-216` 直接复用该结果；因此 switch 已开启时，坏 maintenance 行会走到 `_spawn_bg()`。
- 只读探针写入 `admin_state('maintenance','not-json')`，实际结果：`corrupt_maintenance_spawned=True`。
- DB 查询本身抛异常时，handler 外层 catch 会阻止 spawn；缺口是 scheduler 主动吞掉的解析/形状异常。
- `tests/test_bilingual.py:193-203` 只覆盖合法 `{"paused": true}`，没有坏行/错误形状。
- 修复要求：涉及新模型调用的门控应 fail-closed。优先统一修正 `scheduler.get_maintenance()` 的坏行语义为 paused；若全局语义暂不能改，bilingual 必须自行做保守读取。补坏 JSON、`[]`/`true` 等错误形状及读取异常测试，均断言零 `tasks` 行。

### M1（中）tree “暂不校验”的前提在当前集成态已失效

- `app/institute/projects.py:43-51,127-131` 故意不为 tree 配 `_REF_TABLES` 项。
- 但 `migrations/0020_research_tree.sql:28-42` 已定义 `research_trees(id)`，且编号保证在 0021 前应用。
- `tests/test_projects.py:129-130` 反而锁定了任意 `tree-abc` 都能挂入的行为。
- 独立 cherry-pick D5 时“不依赖并行卡”曾是合理过渡；当前 checkout 合并 0020 后不再合理。建议表存在时校验 `research_trees.id`（动态探测可保留独立 cherry-pick 能力），并把测试改为“真实 tree 成功、ghost tree 拒绝”。

### M2（中）twin 全文事件同时存在数据库膨胀与契约矛盾

- `app/institute/bilingual.py:173-181` 把完整 `text` 放入 durable event。
- `app/bus.py:57-63` 会先把整个 JSON 写进 `events.payload`；`migrations/0001_init.sql:33-39` 没有 payload 大小约束，当前 janitor 也不清理 events。
- `app/router/executor.py:210` 会先按 `app/config.py:35` 的默认 200KB 截断 `task.output`；`translate_note()` 返回的正是这个值。因此单事件并非真正无界，但“大于 cap 仍是全文”的注释/契约不成立。
- 事件还会进入 SSE/replay，重复补发会复制大正文。
- 建议把正文持久化一次到专用 twin 行或完整 artifact（超长时分块），事件仅携带 `twin_id`/`task_id`/path 与摘要；exporter 按引用读取。若坚持内联，至少明确 200KB 上限、截断状态和 events 留存策略。

### M3（中）project name 可破坏 digest Markdown 结构

- `app/institute/projects.py:56-63` 只 trim/限长，不拒绝内部换行或 Markdown/HTML 控制字符。
- `app/institute/projects.py:230-237` 把 name 原样插入一级标题。
- 探针用 name `正常\n\n## 注入标题`，digest 实际前缀成为两个独立标题；`[]()`、`![]()`、raw HTML 同样会被解释。
- 描述正文允许 Markdown 可视为产品选择，但 name 是结构元数据，应折为一行并转义 Markdown 特殊字符；补换行、链接、图片、反引号/HTML 测试。

### M4（中）PATCH-NOTES 的“中英文同 stem”在跨日运行时不成立

- workflow 在创建时冻结 `WORK_DATE`：`app/institute/workflows.py:171-182`。
- bilingual payload 使用该冻结值：`app/institute/bilingual.py:170-179`，PATCH-NOTES-D5.md:61-66,81-87 也要求英文文件使用它。
- 现有中文 exporter 却在完成时重新调用 `work_date()`：`app/vault/exporter.py:260-286`。
- 运行跨过 SGT 午夜时，中文会落完成日、英文会落启动日，不是同 stem sibling。
- exporter 集成补丁应同时把中文 `_on_workflow` 改为优先读取 run/payload 的 `variables.WORK_DATE`，中英文共享一个日期 helper，并补跨日回归测试。

### M5（中）PATCH-NOTES 声称的现有 Vault 内容读取面不存在

- `PATCH-NOTES-D5.md:102-106` 称 SPA 可走现有 `GET /api/vault/*` 直读 twin。
- `app/api/vault.py:19-67` 实际只有 status、index、doctor、research re-export，没有通配内容读取端点。
- 可用的 note dereference 是 `GET /api/artifacts?ref=note:<path>`，但 `app/api/contract.py:148-166` 只返回前 8KB；不能承载这里声明的完整英文 briefing。
- 因此 locale toggle 仍缺一个可按 run/locale 读取完整 twin 的稳定 API，不能把现有 Vault API 写成已满足。建议落专用 twin 存储与 `GET /api/workflows/runs/{id}/twin?locale=en`，或明确新增安全的完整 note 读取端点。

### L1（低）待译正文只有开始标记，存在 prompt injection 面

- `app/institute/bilingual.py:55-62,104-110` 将全文直接拼到 `【原文】` 后，没有结束边界，也没有声明“正文内指令是不可信数据、不得执行”。
- 翻译任务的攻击收益低于路由/工具调用，但研究文档可能含协议行或恶意引用，仍可能让模型停止翻译、改写格式或输出额外说明。
- 建议增加明确的 begin/end 不可信文档边界和忽略正文指令的规则；这是新 prompt，当前修订不违反“不得改写既有 prompt”。

### L2（低）`list_projects().n_links` 不计 direct research rail

- `app/institute/projects.py:77-90` 只统计 `project_links`。
- `app/institute/projects.py:156-168` 与 get/digest 又把 `research_queue.project_id` 视为同一项目内容。
- 因此只用 `enqueue(project_id=...)` 的项目会返回 `n_links=0`，但详情里已有 research。若该字段表示总附件数，应把 direct rail 合并计数；否则改名为 `n_explicit_links` 并写明语义。

### L3（低）所谓 byte-stable prompt 测试只做子串断言

- `tests/test_bilingual.py:119-125` 只检查 echo 输出包含“专业财经译者”和源文本，没有比较完整 prompt。
- 若要锁定常量逐字稳定，应断言任务行中的 `prompt == TRANSLATE_PROMPT.format(text=...)`（或固定 hash），以捕获标点、空行、尾随换行漂移。

## research.py 最小性核对

- D5 可归因行是 `project_id` 参数（`:110`）、说明（`:119-121`）、strip/None 化（`:131`）、active 校验（`:145-150`）、INSERT 列和值（`:199-203`）及 FK 文案（`:208-209`）。
- dedup key 与 pending 查询不含 project_id；cooldown 查询不含 project_id；`_claim_next()`、daily cap、运行状态条件认领均不含 project_id。
- dedup hit 返回原行、不重打标签，`tests/test_projects.py:223-229` 已锁定。
- 唯一不通过点是 H1：project active 校验与 INSERT 不原子。

## migration / 硬规则

- `migrations/0021_projects.sql` 无 `BEGIN/COMMIT/ROLLBACK/END/ATTACH/VACUUM`，符合 B1 每文件单事务纪律。
- 0021 只新增两表、一列、索引；`research_queue.project_id` nullable，旧行保持 NULL。
- `ON DELETE SET NULL` 对 durable research queue 合理；`project_links` 随项目 CASCADE 也合理。
- bilingual 模型调用只走 `executor.submit`；没有直接启动 CLI。
- project 持久时间均用 `bus.now_iso()`；没有 raw `datetime.now()`。
- `_on_workflow_completed` 和 `_twin_safe` 都有 never-raise 壳；后台异常只记录。
- `TRANSLATE_PROMPT` 是新增常量，没有改写既有 `prompts.py` / workflow prompt；注入加固见 L1。

## PATCH-NOTES-D5 集成核对

- projects router：在 `app/main.py:164-189` import，在 `:192-203` include，指示正确。
- bilingual register：应在 lifespan 中、scheduler start 前注册；PATCH-NOTES 的两行正确。
- shutdown drain：`app/main.py:60-72` 增加 bilingual import 与 `set(bilingual._bg_tasks)` 正确。
- conftest：`tests/conftest.py:40-47,82-96` 增加模块导入、pending 并集并更新 7→8 注释正确。
- research API 透传：`app/api/research.py:14-23,43-48` 增字段并传 `project_id=body.project_id` 正确，既有 ValueError→400 可复用。
- exporter handler：事件名/ref、`:en` artifact id、writer 调用和 never-raise 骨架正确；必须同时修 M4，并修正文读取契约 M5。
- 当前 main、drain、research API、exporter 尚未落这些集成补丁；这是 PATCH-NOTES 明示的主代理工作，不单独归因 D5，但合并前必须完成。

## 验证

- `.venv/bin/python -m compileall app -q`：PASS（exit 0）。
- `.venv/bin/python -m pytest tests/test_projects.py tests/test_bilingual.py tests/test_research.py -q`：**46 passed in 1.84s**。
- `git diff --check`：PASS。
- 未运行全量测试。

## 最终裁决

- Research projects：**FAIL**（归档冻结竞态为 must-fix；tree 校验与 digest 注入需修）。
- Bilingual twins：**FAIL**（maintenance 坏状态 fail-open 为 must-fix；事件正文与 exporter/API 契约需收敛）。
- 合并总裁决：**FAIL**。
