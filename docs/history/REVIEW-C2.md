# REVIEW-C2 — Phase 4 链图前三项独立审查

**结论：FAIL**

审查范围严格限定为 `migrations/0016_chain_graph.sql`、`app/institute/chain.py`、`app/api/chain.py`、`tests/test_chain.py`、`PATCH-NOTES-C2.md`；只为核对事件、VaultWriter 与挂载点只读查看了相关现有文件。

指定验证结果：

- `.venv/bin/python -m compileall app -q`：通过。
- `.venv/bin/python -m pytest tests/test_chain.py -q`：`24 passed in 0.83s`。

测试全绿不能支持合入结论：下面 5 个 must-fix 中，4 个已用一次性临时库探针稳定复现，另 1 个是与 ROADMAP 原文直接对照出的功能缺项。

## Must-fix

### C2-M1 — ROADMAP 第二项的 auto-cluster / periodic alias merge 未实现

- ROADMAP 明确要求“Opencode tagger + auto-cluster/merge”以及“periodic merge of aliases”（`ROADMAP.md:141`）。
- 当前只有精确同名 candidate upsert（`app/institute/chain.py:614-645`）、人工单别名追加（`app/institute/chain.py:282-301`）和阈值晋升（`app/institute/chain.py:740-763`）；`tick()` 也只调用 `_auto_promote()`（`app/institute/chain.py:790-837`）。
- 没有聚类、相似实体归并、周期 alias merge，也没有节点合并操作或相关测试。精确字符串去重不能等同 auto-cluster/merge。
- 因而“Phase 4 前三项完成”的认领不成立；至少第二项仍是部分完成。

### C2-M2 — 抽取出的实体晋升后不会回填来源 mention，也不会补写原笔记 footer

- 每个事件先做 backstop，再抽取 candidate（`app/institute/chain.py:819-828`）；此时新实体还不是 node，必然无法命中 backstop。
- 自动晋升发生在整批事件之后（`app/institute/chain.py:833`），晋升路径只创建 node（`app/institute/chain.py:662-693`），没有对刚处理的工件再跑 backstop，也没有触发来源笔记重导出。
- candidate 只保留一个 `first_seen_ref`（`migrations/0016_chain_graph.sql:79`），后续 sightings 只累加计数（`app/institute/chain.py:632-637`），因此晋升时甚至没有完整来源集合可回填。
- 临时库复现：同一 candidate 记录 3 个来源并自动晋升后，其 `chain_mentions` 数量为 **0**。
- PATCH-NOTES 中的 footer 只在工件完成/重导出当下计算；当时未知的实体以后晋升也不会让旧笔记获得 wikilink。这破坏了“mentions 来自 reports/facts”及“Vault 即图浏览器”的核心闭环。

### C2-M3 — `_slug()` 非单射，两个合法节点会争用并相互覆盖同一实体笔记

- DB 只保证原始 `name` 唯一（`migrations/0016_chain_graph.sql:16-25`），但投影路径由替换敌意字符并截断 80 字符的 `_slug()` 生成（`app/institute/chain.py:159-168,875`）。
- 例如 `A/B` 与 `A:B` 是两个不同且合法的唯一 name，却都映射为 `Chain/A-B.md`。region writer 会把同一路径的受管区和 ledger `artifact_id` 改写给后写节点，无法维持“一 node 一 note”。
- 临时库复现：两次 `export_entity_note()` 均返回 `Chain/A-B.md`，第二次后文件正文已变成 `# A:B`。
- `tests/test_chain.py:331-334` 只测了单个 `/` 名称能 slug，没有测试 slug 碰撞或 80 字符截断碰撞。
- 需要持久化且唯一的 slug，或在规范化结果后加入稳定 node-id/hash 后缀；仅靠 `name UNIQUE` 不够。

### C2-M4 — “一个术语只解析到一个 node”的不变量可被公开领域函数破坏

- `_resolves_elsewhere()` 的目标正是保证 name/alias 唯一解析（`app/institute/chain.py:218-230`），但 `create_node()` 只检查“新 aliases 是否已被占用”，没有检查“新 name 是否已是别人的 alias”（`app/institute/chain.py:250-268`）。
- 可达场景包括：旧 candidate 仍 pending 时，另一个 node 新增了同名 alias；随后 API 晋升该 candidate，`create_node()` 会成功创建冲突 name。
- `chain_mentions` 的 UNIQUE 仅按 node 去重，无法阻止同一术语命中两个不同 node（`migrations/0016_chain_graph.sql:57-64`）。
- 临时库复现：先建“宁德时代”并加 alias `CATL`，再建 name=`CATL`；对文本 `CATL` 做 backstop 会新增 **2** 个 node mention。
- 此外 alias 唯一性只靠查询后写入，两个并发 alias/create 写也存在 TOCTOU 窗口；JSON 数组上没有 DB 唯一约束兜底。

### C2-M5 — events 游标的崩溃重放会重复增加 candidate 计数并可能误晋升

- candidate upsert 每次无条件 `mention_count + 1`，没有 `(candidate, artifact_kind, artifact_ref)` sighting 幂等键（`app/institute/chain.py:632-637`）。
- `tick()` 在 backstop、模型调用、candidate 写入全部完成后才单独推进游标（`app/institute/chain.py:819-832`）。
- 普通异常会按已声明的 best-effort 策略推进游标并永久跳过该次抽取；但进程崩溃、取消或 `_set_cursor()` 失败发生在 candidate 写入后、游标写入前时，同一 event 下次会完整重放。mention insert 幂等，candidate 计数不幂等。
- 临时库复现：模拟首次处理在 `_set_cursor()` 前崩溃，恢复后重跑同一 event，单个工件对应 candidate 的 `mention_count` 变为 **2**；重复两次即可触发默认阈值 3 的错误自动晋升。
- 批 10 本身按 event 推进，不会重放已成功推进的前序项，也不会漏掉尚未到达的后序项；问题集中在“当前 event 的工作结果与 cursor 不是一个原子/幂等单元”。
- 建议增加唯一 candidate-sighting 记录并由其聚合计数；这也能一并解决 C2-M2 的来源回填问题。

## Should-fix

### C2-S1 — candidate 状态认领与 node 创建不是一个一致性单元

- `promote_candidate()` 先把 candidate 独立提交为 `promoted`（`app/institute/chain.py:675-680`），随后才查询/创建 node（`app/institute/chain.py:682-693`）。
- 进程在两者之间退出，或 node insert 因非同名竞态的完整性错误失败，都会留下“status=promoted、但没有 node”的永久状态；自动 sweep 不会再重试。
- 条件认领 rowcount 的形式是正确的，但当前副作用并不 restart-safe。应使用同一事务，或引入可恢复的 `promoting` 状态。

### C2-S2 — whiteboard 只抽取 card summary，未读取实际导出正文

- `_artifact_from_event()` 对 board 只查询 `topic/question` 与 `whiteboard_cards.summary`（`app/institute/chain.py:526-539`）。
- 现有 exporter 会优先读取每张 card 的 `output_file` 全文（`app/vault/exporter.py:287-303`）。只出现在正文、未进入 summary 的实体不会产生 mention/candidate，链图与 Vault 实际内容不一致。
- 对 board 应沿用 exporter 的 workspace/output-file 组装语义，summary 仅作降级。

### C2-S3 — hostile 名称的 wikilink 显示部分未转义/拒绝

- `_slug()` 清理了 link target，但 `_wikilink()` 把原名原样放进 `[[slug|原名]]` 的 display 部分（`app/institute/chain.py:159-168,842-845`）。
- 名称含 `|`、`]` 或换行时会生成损坏或可注入额外 Markdown 的 wikilink；临时探针中 `A|B]]` 生成 `[[A-B|A|B]]]]`。
- `entity_footer()` 的 50 链接上限正确（`app/institute/chain.py:929-933`），但 `tests/test_chain.py:331-334` 只覆盖 `/`，未覆盖 wikilink 分隔符和控制字符。
- 更稳妥的修复是在 node/candidate/alias 边界拒绝控制字符及 Obsidian 分隔符，并同时处理 C2-M3 的稳定 slug。

### C2-S4 — parser 不接受中文模型常见的全角竖线

- `_ENTITY_LINE` 接受前导/分隔处多空格及尾随空白，但只接受 ASCII `|`（`app/institute/chain.py:105`）。
- `ENTITY: 台积电 ｜ company`（全角 `｜`）解析为空；当前测试 fixture 未覆盖该变体（`tests/test_chain.py:177-216`）。
- Prompt 虽要求 ASCII 格式，中文模型仍可能输出全角标点；建议分隔符接受 `[|｜]`。

### C2-S5 — relation 是开放集，但原样作为 Dataview key 缺少最小语法约束

- API/domain 只要求 relation 去首尾空白后非空（`app/institute/chain.py:304-321`），投影时直接拼成 `relation:: [[dst]]`（`app/institute/chain.py:861-865`）。
- 对推荐值 `supplier_of` 等格式完全正确；但换行、`::` 或 region marker 等任意非空 relation 会破坏 Dataview/受管区结构。
- “开放集”不等于“任意 Markdown”；建议保留开放词表，同时限制为安全 field-key 语法。

### C2-S6 — PATCH-NOTES 把 memory footer 标成“可选”，与“every exported note”原文不一致

- ROADMAP 要求 footer 注入 every exported note（`ROADMAP.md:142`）。
- `PATCH-NOTES-C2.md:86-95` 把 `_on_memory` 的注入标为可跳过。若主代理选择跳过，该项不满足字面验收标准；应明确纳入，或先修改验收范围并记录裁决。

## 逐项核对

### ROADMAP Phase 4 前三项

- **Tables + INSTR backstop：基本通过。** 四表、约束、复合 SELECT、幂等 mention 都已实现；需修复 C2-M4 的解析唯一性。
- **Opencode tagger + auto-cluster/merge：不通过。** executor 抽取和 candidate promotion 已有，但 auto-cluster/periodic alias merge 缺失（C2-M1），晋升来源也丢失（C2-M2）。
- **Vault projection：不通过。** region writer、Dataview 与 dashboards 已有，但路径碰撞会让节点互相覆盖（C2-M3），旧来源 footer 不回填（C2-M2）。

### Backstop

- **复合 SELECT：通过。** `_MATCH_SQL` 用一个 `UNION` SELECT 扫 name 与 `json_each(aliases)`（`app/institute/chain.py:444-450`）；随后逐命中 `INSERT OR IGNORE`，与 PATCH-NOTES 披露的裁决一致。
- **坏 JSON：通过当前迁移前提。** 新表 `aliases CHECK(json_valid(...))`（`migrations/0016_chain_graph.sql:21`），没有迁移前旧行；因此 `json_each` 不会遇到 malformed JSON。低风险缺口是没有限制 `json_type(aliases)='array'`，合法 object/scalar 仍可进入。
- **≥2 字符：通过其既定边界。** node name 有 DB CHECK，应用也校验；alias 应用校验且 SQL 再过滤 `length(a.value)>=2`。这只能防单字，不能防 `AI` 命中 `PAID` 等子串误报，属于 INSTR 方案固有限制。
- **同一 node 的 name 与 alias 同时命中：通过。** `_match_hits()` 按 node_id 只保留最早命中（`app/institute/chain.py:453-469`），DB UNIQUE 再兜底；但跨 node 术语歧义见 C2-M4。
- **snippet UTF-8 边界：通过。** SQL 只判命中，位置由 Python `str.find()` 取得；`_snippet()` 按 Unicode 字符切 ±60（`app/institute/chain.py:472-475`），不会切出非法 UTF-8。
- **事件名：通过。** `research.completed` 实际 emit 在 `app/institute/research.py:444-446`，`whiteboard.board_completed` 在 `app/institute/whiteboard.py:888-891`，`analyst_daily.completed` 在 `app/institute/analyst_daily.py:349-353`；不存在本路径所需的 `board_finalized`。

### 抽取管道

- **events.id 游标：部分通过。** 单 event 成功后推进、批量上限 10、失败也推进的声明与实现一致；崩溃窗口不幂等见 C2-M5。
- **失败也推进：属于明确 best-effort 裁决。** 它会永久丢失失败事件的模型抽取，且若 live handler 当时也失败，backstop 也会丢；不是实现与文档不一致，但需接受这一数据完整性代价。
- **Prompt 回显安全测试：有效但范围有限。** `test_extract_entities_prompt_carries_no_bare_entity_line` 确实能防模板示例产生 phantom entity；echo roundtrip 确实经过 executor 并落 tasks row，但它靠输入文本自带 `ENTITY:` 行，只验证执行/解析通路，不验证真实模型抽取质量。
- **格式容忍：部分通过。** 多空格和尾随空白可解析；全角竖线不可解析，见 C2-S4。
- **自动晋升并发：基本路径正确。** `UPDATE ... WHERE status='pending'` 保证同一 candidate 只有一个赢家；candidate `name UNIQUE` 排除了两个同名 candidate。same-name node insert 的 IntegrityError fallback 存在（`app/institute/chain.py:685-692`），但现有测试只覆盖“节点预先存在”分支（`tests/test_chain.py:260-274`），没有真正命中竞态 catch。
- **模型调用位置：通过。** `executor.submit()` 只在 `extract_entities()`（`app/institute/chain.py:586-600`），由 `tick()` 调用；`_on_artifact_event()`（`app/institute/chain.py:556-566`）只有文本组装与 backstop，零 executor 调用。

### Vault 投影

- **只经 writer 写盘：通过。** `_render_note()` 只组装数据；`export_entity_note()` 与 `export_dashboards()` 都调用 `writer.write_note(..., region=True)`（`app/institute/chain.py:848-909`），chain.py 没有直接写 Vault 文件。
- **region 参数：通过。** 与现有 `VaultWriter.write_note(region=True)` 契约匹配（`app/vault/writer.py:286-304`），人工区外内容保留测试有效。
- **entity footer：部分通过。** 首次出现排序、alias→node name、空结果及最多 50 条都正确；hostile display 失败见 C2-S3，晋升后历史 footer 不回填见 C2-M2。
- **Dataview：推荐 relation 通过。** `supplier_of:: [[X]]` 的输出格式正确；开放 relation 的安全语法缺口见 C2-S5。
- **一节点一笔记：不通过。** 非单射 slug 见 C2-M3。

### API

- **7 个端点：通过。** 路径与 PATCH-NOTES 清单一致（`app/api/chain.py:49-87`）。
- **graph depth：通过。** domain 钳制到 1..3（`app/institute/chain.py:383-400`），API 测试覆盖 99→3、1/2 跳及 404。
- **confidence：通过。** 转 float 后校验闭区间 `[0,1]`（`app/institute/chain.py:315-321`）；NaN/Infinity 也因比较结果而被拒绝。
- **晋升 404/409：通过常规路径。** unknown id→404、重复/丢失 claim→409 的映射正确（`app/api/chain.py:13-23,72-74`）；竞态错误消息可能引用 claim 前的 stale status，但状态码正确。

### 迁移与硬规则

- **B1/additive：通过。** 0016 只有 CREATE TABLE/INDEX，无 BEGIN/COMMIT/ATTACH/VACUUM；当前序列中位于 0015 与并行的 0017/0018 之间。
- **FK 删除策略：通过。** node 删除级联 edges/mentions，security 删除 SET NULL（`migrations/0016_chain_graph.sql:20,35-45,57-65`），符合“图节点可脱离上市标的继续存在”的语义。
- **模型统一走 executor：通过。**
- **bus handler 不 raise：通过业务异常路径。** 两个 handler 都全包 `Exception` 并记录；取消异常按 asyncio 正常取消语义传播。
- **条件认领：形式通过、崩溃一致性不通过。** status 更新有条件 WHERE/rowcount，但 promote 副作用非原子，见 C2-S1。
- **时间戳：通过。** 新业务行均使用 `bus.now_iso()`。
- **Vault 只由 writer 写：通过。**

## PATCH-NOTES-C2 四个挂载点核对

- **挂载 1 / `main.py` register：可直接应用。** 当前 `vault_exporter.register()` 位于 `app/main.py:120-121`，紧随其后 import/call `chain_graph.register()` 无循环依赖；三个实际事件名均正确。
- **挂载 2 / API router：可直接应用。** 当前 import 块为 `app/main.py:152-171`、router 元组为 `app/main.py:174-181`，建议位置和变量命名均匹配现状。
- **挂载 3 / scheduler：可直接应用但必须同步测试。** `@metered(..., gated=True)` 与现有 job 风格匹配（`app/institute/scheduler.py:107-175`），`every(... minutes=60)` 与 `start()` 注册方式匹配（`app/institute/scheduler.py:269-288`）；`tests/test_maintenance.py:82-95` 当前锁定 gating registry，应把 `_chain_tick_job` 加入 gated 集。
- **挂载 4 / exporter footer：五处代码上下文全部匹配当前文件。** `_export_research` 对应 `app/vault/exporter.py:156-174`，`_on_workflow` 对应 `252-258`，`_on_board` 对应 `291-308`，`_on_analyst_daily` 对应 `326-345`，`_on_memory` 对应 `377-385`；变量作用域和 async `await` 都正确。
- **exporter 语义仍有两项阻断。** 注入只处理“当时已知 node”，无法补写后来晋升实体（C2-M2）；memory 不应在“every exported note”验收下标成可选（C2-S6）。

## 测试覆盖评价

- 24 个测试均为有效回归测试，覆盖了主 happy path、幂等 mention、基础校验、executor echo 通路、API、depth、region 人工批注保留。
- 缺失的关键失败路径：auto-cluster/periodic merge、晋升来源回填、slug 碰撞、name-vs-alias 冲突、cursor 崩溃重放、claim 后创建失败、真实 IntegrityError 竞态 fallback、whiteboard output-file 全文、footer 50 上限与 hostile 分隔符、全角 `｜`、迁移 FK 删除行为。
- 在上述 must-fix 修复并补相应回归测试前，不建议主代理挂载或把 Phase 4 前三卡标为完成。
