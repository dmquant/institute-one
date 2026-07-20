# PATCH-NOTES-C2 — Phase 4 链图（chain graph）分区外接线清单

C2 交付物（已落盘，独占分区内，36 个分区测试全绿；含 REVIEW-C2 五项 must-fix 返工，见文末「REVIEW-C2 返工记录」）：

- `migrations/0016_chain_graph.sql` — `chain_nodes` / `chain_edges` / `chain_mentions` / `chain_candidates` / `chain_candidate_sightings` 五表（0016 从未上过生产，返工直接改本文件而非追加迁移）。规格外的明确加固（都服务任务书里写明的语义）：`chain_nodes.name` UNIQUE（backstop 命中必须唯一解析）；`chain_nodes.slug` UNIQUE（持久化 vault 路径段，插入时分配——`_slug()` 非单射，撞名者带稳定 node-id 后缀）；`chain_mentions` UNIQUE(node_id, artifact_kind, artifact_ref)（任务书要求的"同 node+ref 不重复"落到 DB 层）；`chain_candidates.name` UNIQUE + `chain_candidate_sightings` UNIQUE(candidate_id, artifact_kind, artifact_ref)（mention_count 改为按 DISTINCT 来源工件聚合，崩溃重放不重复计数）；`aliases` CHECK 收紧为 `json_type='array'`。`relation` 是开放集（建议词表 supplier_of/customer_of/competitor_of/subsidiary_of/produces，无 CHECK）；自环边被 CHECK 拒绝。candidate status 增加 `merged`（周期聚类并入），`merged_into` 记录吸收节点。
- `app/institute/chain.py`（新）— INSTR backstop（一条复合 SELECT 对全部 name+aliases 做大小写敏感中文子串匹配）、`extract_entities`（`ENTITY_EXTRACT_PROMPT` 新常量逐字稳定，走 `executor.submit`，hand=`settings.default_hand`）、`promote_candidate`/`reject_candidate`（条件认领；promote 全事务化并回填来源 mentions）、`merge_aliases`（歧义别名拒绝，检查+写入同事务）、`_auto_cluster()`（周期聚类：规范化名一致或 ≥4 字包含关系的 pending candidate 并入现有节点成为别名；零/多匹配保守跳过）、`tick()`（events.id 游标 + 聚类 + 自动晋升，阈值 admin_state `chain:promote_threshold` 默认 3）、vault 投影（`Chain/<slug>.md` region 模式 + Dataview inline 关系 + `_meta/Dashboards.md`，路径用持久 slug）、`entity_footer()`、`register()`。
- `app/api/chain.py`（新）— GET /api/chain/nodes（搜索/分页）、GET /api/chain/nodes/{id}（含 edges+mentions）、GET /api/chain/candidates（status 增加 merged）、POST /api/chain/candidates/{id}/promote、POST /api/chain/nodes/{id}/aliases、POST /api/chain/edges、GET /api/chain/graph?center=&depth=（BFS 邻接 JSON，depth 钳 1..3）。
- `tests/test_chain.py`（新）— 36 个测试（原 24 个 + REVIEW-C2 五项 must-fix 回归 12 个：聚类三态、晋升回填×2、promote 崩溃回滚、slug 碰撞×2、术语唯一解析×2、游标崩溃重放×2）。

分区测试不依赖任何分区外改动（API 测试用裸 FastAPI app 挂 router，与 test_forecasts 同模式）。以下四项挂载由主代理执行。

## 挂载 1：main.py — chain.register()（bus 钩子 + vault 投影）

exporter.py 没有公开的 per-handler 注册入口（`register()` 是一次性整体挂钩），故按任务书预案：chain 自带 `register()`，在 lifespan 里紧跟 `vault_exporter.register()` 之后加两行：

```python
    from .institute import chain as chain_graph
    chain_graph.register()
```

这一个调用同时挂上：三个 backstop 订阅（`research.completed` / `whiteboard.board_completed` / `analyst_daily.completed`——注意实际事件名是 `board_completed`，不是任务书里的 `board_finalized`）+ `chain.node_updated` → 实体笔记/Dashboards 投影。所有 handler 内部全兜底不 raise。

## 挂载 2：main.py — 路由

import 块加 `chain as api_chain,`（按字母序放 `ask_stream` 后、`digests` 前均可），router 元组里加 `api_chain.router,`（建议放 `api_forecasts.router` 旁）。

## 挂载 3：scheduler.py — chain-tick（hourly，gated=True）

job 定义与现有 gated job 同风格（它提交模型调用——每个新工件一次抽取任务——必须尊重 maintenance 暂停；backstop 本身不烧配额但搭同一游标顺路跑）：

```python
@metered("chain-tick", gated=True)
async def _chain_tick_job() -> None:
    from . import chain
    await chain.tick()
```

`start()` 里注册（hourly；如想走配置可在 config.py 加 `chain_tick_minutes: int = 60`，0=禁用，`every()` 已处理非正数）：

```python
    every(_chain_tick_job, "chain-tick", minutes=60)
```

注意 scheduler.py 顶部注释里的门控清单若维护，把 chain-tick 归入 gated 一侧（判据：`tick()` 调 `executor.submit`）。`tests/test_maintenance.py` 若锁 job 清单，同步断言 `gated is True`。

## 挂载 4：vault/exporter.py — `## Entities` wikilink footers（逐笔记尾注）

`chain.entity_footer(text) -> str` 给定笔记正文返回 `## Entities\n[[实体A]] [[实体B]]`（无命中返回空串，按正文首次出现顺序，别名命中链接到实体名，路径敌意字符走 `[[slug|原名]]`）。五个导出点各一处小 diff（lazy import 遵循 exporter 现有模式；`await` 在 async 函数内合法）：

**(a) `_export_research`** — `footer = _workspace_footer(ws)` 块之后、`rel = f"Research/..."` 之前插入：

```python
    from ..institute.chain import entity_footer  # lazy: domain module
    ef = await entity_footer("\n\n".join(parts))
    if ef:
        parts.append(ef)
```

**(b) `_on_workflow`（briefing/daily）** — `if not text.strip():` 块之后、`rel = f"{folder}/..."` 之前插入：

```python
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(text)
        if ef:
            text = f"{text.rstrip()}\n\n{ef}"
```

**(c) `_on_board`** — `rel = f"Whiteboard/..."` 之前插入（parts 已组装完）：

```python
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer("\n\n".join(parts))
        if ef:
            parts.append(ef)
```

**(d) `_on_analyst_daily`** — `rel = f"Analysts/..."` 之前插入：

```python
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(text)
        if ef:
            text = f"{text.rstrip()}\n\n{ef}"
```

**(e) `_on_memory`（可选，region 内尾注）** — `rel = f"Analysts/.../memory.md"` 之前插入：

```python
        from ..institute.chain import entity_footer  # lazy: domain module
        ef = await entity_footer(body)
        if ef:
            body = f"{body}\n\n{ef}"
```

语义说明：footer 追加发生在 write_note 之前，参与内容 hash——实体集变化会触发笔记重写，这是预期（笔记内容确实变了）；skip-if-unchanged 仍然生效。(e) 的 footer 落在 managed region 内（memory 是 region 笔记），人工批注不受影响；如嫌 memory 笔记链接太密可跳过 (e)。`entity_footer` 只读 DB、不写盘、不 raise 业务错（内部无 emit）；空库/无命中零开销一条 SELECT。

## 设计裁决记录（供轮级审查核对）

- **backstop 与"一条 SQL"**：ROADMAP 原文 "one SQL statement over new artifacts"。命中扫描是一条复合 SELECT（name UNION json_each(aliases)，`instr()` 字节级精确匹配，CJK 安全、大小写敏感）；落 mentions 用逐命中 INSERT OR IGNORE（命中数条级），换来每条 mention 带 snippet + 每个新命中节点一个 `chain.node_updated` 事件（驱动 vault 增量投影）。纯单语句 INSERT..SELECT 做不到这两点，属务实取舍（任务书授权"按现有 SQLite 能力务实实现"）。
- **抽取只在 tick，不在 bus handler**：bus.emit 同步 await handler，若 handler 内做模型调用会把 research/board 完成路径挂在抽取上。故 live handler 只跑 backstop（毫秒级），抽取由 hourly tick 经 events.id 游标（admin_state `chain:extract_cursor`）驱动——顺路重跑一遍 backstop（幂等），停机窗口的工件也补得上。
- **游标推进策略**：单工件抽取失败也推进（best-effort enrichment；live handler 已做过 backstop），不让坏工件卡死队列。批量 10/次，积压跨 tick 消化。
- **晋升漏斗**：已知实体（name/alias 精确匹配）不进 candidates（known 计数返回）；name 解析到现有节点（按 name 或 alias，REVIEW-C2 M4）时 promote 走 merge（merged=True）而不是报错；自动晋升用 kind_guess（非法值降级 other），security_id 留人工晋升补；晋升前 `_auto_cluster` 先把近似表面形式并入现有节点（REVIEW-C2 M1）。
- **抽取 hand**：`settings.default_hand`（生产=codex，测试=echo）。没加专用配置——真要换便宜通道时在 config 加 `chain_extract_hand` 一行即可（本轮未加，避免无谓面）。
- **`chain:promote_threshold`**：admin_state JSON 整数，缺省 3，`chain.set_promote_threshold()` 可改（未开 API 端点——操作面留给 Phase 6 operator loop）。

## REVIEW-C2 返工记录（C2b，五项 must-fix 的修法）

- **C2-M1（auto-cluster/periodic merge 缺项）**：`tick()` 在自动晋升前先跑 `_auto_cluster()`——pending candidate 的规范化名（casefold+去空白）与某节点 name/alias 一致，或互为包含且较短方 ≥4 字符（`MIN_CLUSTER_CONTAIN_LEN`，防「电池」吸收「固态电池」类误并）时，条件认领为 `merged`（记 `merged_into`）、来源 sightings 回填 mentions、表面形式并入该节点 aliases（歧义别名跳过、并入仍成立）。零匹配或多匹配一律不动（保守：歧义交给人工/阈值路径）。
- **C2-M2（晋升来源不回填）**：新表 `chain_candidate_sightings` 记录每个 DISTINCT (candidate, 来源工件) 及抽取时的 snippet；`promote_candidate` 与 `_merge_candidate_into_node` 在同一事务内把全部 sightings 转成 `chain_mentions`（INSERT OR IGNORE，与 backstop 的 UNIQUE 契约天然去重）。已知限制：旧来源笔记的 `## Entities` footer 不会自动补链——那需要触发旧工件重导出，挂载点在 exporter（本分区禁区）；晋升 emit 的 `chain.node_updated` 只刷新实体笔记。主代理如要闭环可在挂载 4 之外加"晋升后重导出来源工件"一步。
- **C2-M3（slug 覆盖）**：`chain_nodes.slug` 持久化且 UNIQUE，插入事务内分配：base `_slug(name)` 空闲则用之，被占则截 67 字符再挂 `-<node_id>` 稳定后缀。投影路径、边 wikilink、`entity_footer` 全部改用持久 slug（`node_detail` 联查带出 `src_slug`/`dst_slug`），不再现场重算——"A/B" 与 "A:B" 各得一笔记，80 字符截断碰撞同理。
- **C2-M4（术语歧义）**：`create_node` 的 name/aliases 占用检查移入插入事务（`_term_taken_txn`，与写共持写锁，消除 TOCTOU），name 撞他人 alias 或 name 均拒绝；`merge_aliases` 同事务化；`promote_candidate` 解析 name 时同时查 name 与 alias，命中即并入拥有者（merged=True）而不是创建冲突节点。原 promote 的 IntegrityError-fallback 路径随事务化删除（存在性检查已在写锁内，竞态窗口不复存在）。
- **C2-M5（游标崩溃重计数）**：`record_candidates` 不再无条件 `mention_count+1`——每个来源工件先落 sighting（UNIQUE 幂等），`mention_count` 由 sightings COUNT 重算。崩溃发生在 candidate 写入后、`_set_cursor()` 前时，重放同一 event 是纯 no-op，不再能凑满阈值误晋升。
- **附带（REVIEW-C2 S1）**：promote 的条件认领、节点创建、merged_into、mentions 回填合为一个事务——中途失败全量回滚，candidate 留在 pending 可重试，不再产生"promoted 无 node"的永久孤儿。

## 其他分区外事项

- `roadmap/backlog.json`：Phase 4 前三项对应卡的状态迁移由主代理推进（C2 未动）。
- 生产 8100 未动；0016 纯 CREATE TABLE/INDEX，下次重启 `db.init()` 秒级应用（0015 是否已就位无关——`db.migrate()` 按文件名排序，序号空洞无影响，B2 已有先例）。
- SPA/插件消费 `/api/chain/*` 与 `chain.node_updated` 事件类型（前端 KNOWN_EVENT_TYPES）归 C7 前端整合分区。
- MCP `chain_*` 读工具是 ROADMAP "MCP expansion" 卡（本轮不做）。
- REVIEW-C2 的 should-fix（S2 whiteboard 全文、S3 wikilink display 转义、S4 全角竖线、S5 relation 语法约束、S6 memory footer 措辞）未在本轮返工范围（任务书只列五项 must-fix + S1 顺带），留待后续卡。
