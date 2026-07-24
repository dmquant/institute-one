# PATCH-NOTES-VAULT-PROJECTION — Phase 4 "Vault projection" 两个剩余缺口

ROADMAP.md:142（◔ Vault projection）括号注明的两个未完成项：
Dataview typed-relation display、footer coverage across every new artifact kind。

## 缺口 1 — Dataview inline typed relations（勘误 + 测试钉死，无生产代码改动）

**现状勘误**：chain 实体笔记（`Chain/<entity>.md`）不是由 `app/vault/exporter.py`
投影，而是 `app/institute/chain.py::_render_note / export_entity_note`（经
`chain.node_updated` 总线事件触发）。且 inline typed relation 渲染在提交基线
（commit a232d71）里**已经实现**：每条出边渲染为
`{relation}:: [[dst_slug]]`（如 `customer_of:: [[台积电]]`），入边保留人类可读的
`- [[src]] —relation→ 本实体`；Dataview 查询走出边字段即可
（`FROM "Chain" WHERE supplier_of`，`_meta/Dashboards.md` 的 starter query 已用它）。
既有测试 `tests/test_chain.py::test_entity_note_region_projection_and_human_notes_survive`
断言过单个 role。ROADMAP 括号里的 "Dataview typed-relation display … incomplete"
对这半句而言是**过时描述**。

**role → field 名转换规则（本轮裁决）：恒等映射（identity），不做任何转换。**
依据（按任务要求查过现有 role 值的实际形态）：

- 生产 DB（`~/.institute-one/institute.db`）`chain_edges` 现有 **0 行**——尚无
  真实 role 值；
- 系统内唯一的 role 形态来源是 `chain.RELATION_VOCABULARY`
  （`supplier_of / customer_of / competitor_of / subsidiary_of / produces`），
  全部为 snake_case ASCII，无空格无中文，本身就是合法 Dataview field 名，
  转换是恒等的；
- Dataview 实际允许含空格/中文的 inline field 名（空格名会被规范化出 dash-case
  的查询键），真正会破坏结构的是换行、`::`、region marker 文本等
  （REVIEW-C2 S5 的开放集缺口）——这类值该在 domain 边界（`add_edge` 校验）拒绝，
  而非投影时转义。

为把该裁决固化，新增测试
`test_vault_projection.py::test_vocabulary_roles_are_legal_dataview_field_names_verbatim`
（词表每项必须匹配 `[a-z][a-z0-9_]*`：以后往词表加入含空格/中文的 role 会先在此
失败，逼出显式转换规则或边界拒绝），以及
`test_entity_note_renders_every_vocabulary_role_as_dataview_field`
（全词表逐 role 断言 `role:: [[slug]]` 出现在实体笔记，入边箭头形式也钉死）。

## 缺口 2 — `## Entities` footer 补齐到全部 artifact 种类（本轮主要改动）

**盘点**（`app/vault/exporter.py` 全部 write_note 投影点，改动前）：

| 投影点 | 笔记 | footer |
|---|---|---|
| `_export_research` | `Research/<topic>/<date> 深度报告.md` | ✅ 已有 |
| `_on_workflow` | `Briefing|Daily/<date> ….md` | ✅ 已有 |
| `export_board` | `Whiteboard/<date> <topic>.md` | ✅ 已有 |
| `_on_analyst_daily` | `Analysts/<id>/<date> 日报.md` | ✅ 已有 |
| `_on_memory` | `Analysts/<id>/memory.md`（region） | ✅ 已有 |
| `_on_committee` | `Committee/<date> 委员会裁决.md` | ✅ 已有（本工作区 M8-012 并行卡带入） |
| `_on_factcheck_disputed` | `Inbox/Disputed Claims.md` | ❌ → **本轮加入** |
| `_on_paper_book` | `Book/journal/<date>.md` | ❌ → **本轮加入** |
| `_on_research_tree_completed` | `Research/<topic>/tree.md` | ❌ → **本轮加入** |
| `_on_twin_ready` | `…/<date> …_en.md`（英文孪生） | ❌ → **本轮加入**（英文正文可命中实体别名如 CATL） |

四处注入全部沿用现有模式：`from ..institute.chain import entity_footer`
（lazy import）+ 空串不追加，注入点在 `write_note` 之前、handler 的
`try/except` 之内（footer 只读 DB；即便它抛异常也被现有"handler 永不炸 bus"
外壳吞掉）。幂等性由结构保证：这四个 handler 每次导出都从 rows/tasks **重新
渲染正文**（从不回读旧笔记），footer 是对新正文重算的，重复导出不会堆叠
（测试逐一断言 re-fire 后 `## Entities` 恰出现一次）；内容不变时 writer 的
skip-if-unchanged（规则 d）继续生效。

## 文件清单

- `app/vault/exporter.py` — 四个 handler 各 +4/5 行 footer 注入（additive，唯一生产代码改动）。
- `tests/test_vault_projection.py`（新，6 测试）— 合成 bus events → 笔记内容断言，
  沿用 `test_exporter_handlers.py` 的模式（`clean_vault` fixture、`_event` 构造、
  真实 emit shape 的 payload）。
- `tests/test_exporter_handlers.py` — 同一套 footer 覆盖在 handler 自己的测试文件里
  也各钉一个用例（factcheck/paper-book 走真实持仓行/研究树/twin 四个
  `*_gets_entity_footer_without_bloat`），并把 degrade 清单从 8 个 handler 补齐到
  全部 10 个（`_on_research_tree_completed`、`_on_twin_ready` 此前不在空 payload
  吞咽断言里）。
- `PATCH-NOTES-VAULT-PROJECTION.md`（本文件）。

边界遵守：未触碰 `app/institute/`、`app/api/`、`migrations/`、`frontend/`、
`obsidian-plugin/`；ROADMAP.md 仅改 142 行一条；未 commit。

## 验证

- `.venv/bin/python -m pytest tests/test_vault.py tests/test_exporter_handlers.py
  tests/test_vault_projection.py -q` → **43 passed**（三个 vault 测试文件全绿）。
- `.venv/bin/python -m pytest tests -q --ignore=tests/test_similarity_calibration.py`
  → 最终轮 **1078 passed, 1 skipped**。（此前一轮曾见 `tests/test_factcheck.py`
  2 例 `*_vector_scan_is_bounded_newest_first` 红——并行 agent 当时正在改写
  factcheck.py（P10cde 向量扫描 LIMIT 卡）的半成品，settle 后复跑即绿，
  与 vault 改动无关。）
- `.venv/bin/python -m compileall app -q` → 通过。

## 遗留（不在本轮边界内，建议开卡）

1. **role 开放集的边界校验（REVIEW-C2 S5）**：`add_edge` 只要求 relation 非空，
   含换行/`::`/region-marker 文本的 role 依然能存库并破坏 Dataview/受管区结构。
   修复点在 `app/institute/chain.py`（domain 边界拒绝或白名单语法
   `[A-Za-z0-9_\-]+`），本轮边界禁改该文件；词表恒等映射的前提已被新测试钉死。
2. **历史回填通道未覆盖新种类**：`chain.REPROJECT_KINDS`（`POST /api/chain/reproject`）
   只含 research/briefing/daily/whiteboard/analyst-daily/memory。新加 footer 的
   factcheck / paper-book-journal / research_tree / committee 种类的**存量**笔记
   不会被回填（增量导出即有 footer；twin 笔记 artifact_kind 为 briefing/daily，
   已天然在回填范围内）。扩 tuple 在 `app/institute/chain.py`，同属禁区。
3. **`Book/forecasts.md`（forecast-history）无 footer**：投影点在
   `app/institute/forecasts.py::export_vault_history`（禁区文件），正文含标的/
   论点名，可命中实体。
4. ~~ROADMAP.md:142 建议改写~~ **已执行**：该条已翻 ☑，附 2026-07-20 注记
   （typed relations 勘误 + footer 补齐 + REPROJECT_KINDS 遗留指回本文件）。
