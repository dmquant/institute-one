# PATCH-NOTES-D4 — Phase 7 BFS research tree / Explore mode（分区外挂载清单）

D4 交付物（已落盘，独占分区内，分区 31 例全绿）：

- `migrations/0020_research_tree.sql` — `research_trees` / `research_tree_nodes`（0019 留给并行 D 卡；符合 B1 迁移纪律：无 BEGIN/COMMIT/PRAGMA，全 IF NOT EXISTS）。`uq_research_tree_children` 唯一索引（无 WHERE，非部分索引）=（tree, parent, topic）子节点插入幂等的兜底；`research_trees.announced_at` 是 `tree.completed` 事件的单发仲裁列（R-D4 新增）；`score REAL` 为**预留列**（0018 recipes 先例：schema 定稿、本轮无代码读写，后续 ranking 卡可直接用）。另种子 `admin_state.research_tree_limits`（删行降级为内置默认 3/2）。
- `app/institute/research_tree.py`（新）— `create_tree` / `tick`（BFS drain）/ `stop_tree` / `recover_orphans` / `parse_explore`（防御行协议解析）/ `get_tree` / `list_trees`。铁律落点：模型调用只走 `executor.submit`（全局信号量，模块自身**无并发池**）；research 手不越界（rule 10：轮询 `settings.research_hand_names`，fallback 链锁定其中）；一切状态迁移都是条件认领（rowcount 仲裁）；tick 永不 raise；时间戳全 `bus.now_iso()`。
- `app/api/research_tree.py`（新）— 4 端点（见下方契约）。与 research 队列 router 共用 `/api/research` 前缀（路径不相交）。
- `tests/test_research_tree.py`（新，31 例）— 行协议 fixture 全链（建树→逐层→剪枝→完成，fixture 逐调用审计 H1 不变量）、真 echo 回显免疫全链、崩溃恢复、stop（含两个 R-D4 竞态复现）、并发 tick 单认领、单树并发上限、每日建树 cap 并发不可突破、防御解析对抗、API 往返。

## R-D4 返工记录（REVIEW-D4 FAIL → 已修）

- **H1 子节点在父结论落库前可被认领**：`_run_node` 的完成路径改为**一个写事务**——先条件终态父节点（`WHERE status='running'`，写入 summary/task_id），再在同事务内 `_insert_children(conn, …)` 插入全部子行（pending 与 pruned 都在内），commit 后才发事件。整层要么原子可见、要么整体消失：中途丢失 running 认领（操作员重置/他进程恢复）即**丢弃整批结果**（不落 summary、不插子），崩溃只能发生在整批提交前（父重跑、无子存在）或提交后（层完整）。另加**认领守卫**（防御纵深）：候选查询与认领 UPDATE 都要求 `parent_id IS NULL OR parent.status='completed'`——即使有人手工制造出父未完成的 pending 子行，也不可能被认领去组出空父链 prompt。fixture 现在对每次 explore 调用审计该不变量（父必须 completed 且 summary 非空），全部用例零违例。
- **H2 stop 竞态遗留永久 pending**：`stop_tree` 的树翻牌（`pending/exploring→stopped`）与 pending 剪枝合并为**一个事务**；子插入本就在完成事务内且逐语句带「树必须 exploring」守卫——SQLite 单写者序列化后只剩两种交错：子批次先提交（stop 事务把它们剪成 pruned）或 stop 先提交（完成事务读到 stopped、一行不插，pruned 兜底行同样被守卫拦下）。认领 UPDATE 也内嵌树状态守卫（候选扫描与认领之间的 stop 立即生效）。兜底：tick 开头的 sweep 把「终态树下搁浅的 pending 行」剪成 pruned（清历史损伤/手工编辑）；`recover_orphans` 对终态树下的 running 孤儿直接剪 pruned（重排成 pending 会按设计永不可认领），活树下照旧重排 pending。
- **M2 停止事件状态失真**：`tree.completed` 改为**排空后的终态快照**——新列 `announced_at` 的条件 UPDATE 是单发仲裁器，仅当树已终态（completed/stopped/failed）**且无 pending/running 节点**时命中；payload 在终态落库后从库行读出（status、逐状态节点计数、finished_at）。stop 时若仍有 running 节点，事件推迟到最后一个自然完成者收尾时发出——SSE viewer 收到即可断开，exporter 投影的永远是收尾完的树，不会再把 `[进行]`/空 summary 写进 Vault。`_maybe_finish_tree` 不再发事件（翻牌与宣布解耦，两步之间崩溃由 sweep 补宣布）。
- **随手修**（同分区低成本）：N1——`CONCLUSION: <一段结论>` 占位符模仿不再被接受为 summary（与 CHILD 同款丢弃）；N2——每日计数行改名 `research_tree_booked:<date>`、拒绝 payload 字段改 `booked_today`（计的是**占坑尝试**而非成活树：建树事务失败坑不退，保守语义按名归位）。
- **未修（记录在案）**：M1 的宽措辞已收窄——子插入幂等是 **exact-topic（BINARY collation）** 语义：崩溃重放同一 canned 输出幂等；非确定模型重放可能提出不同 topic 而新插一批。H1 事务化已把该窗口压缩到「崩溃重跑」一种；人工把 completed 父节点重置回 pending 重驱时请知悉此语义（测试 docstring 同注）。R-D4 对 daily_cap 建议的 429+Retry-After 未采纳（保持 research 队列 200+refused 联合形状的房内先例）；weighted hand 分支（workflows 的 opt-in weights）未复用——rule 10 只要求链内轮询，weights 接线留给后续卡。

## 1. explore prompt（逐字稳定常量，CLAUDE.md 规则 4）

模块常量 `EXPLORE_PROMPT`（调用时前置 `date_anchor()`，factcheck 先例）。回显免疫是**结构性**的：模板没有任何一行以协议 token 开头（`CONCLUSION:` / `CHILD:` 只出现在「」内的行中部），所有插值材料（topic / question / 父链结论）经 `_quote_material` 中和（折叠成单行 + 打断 token 模式）——镜像回显永远解析出零子节点。

```text
你是研究所的探索研究员，正在对一个研究主题做广度优先（BFS）的树状拆解。你负责其中一个节点：先给出本节点的简要研究结论，再提出最值得继续下钻的子问题。

【当前主题】{topic}
【核心问题】{question}
【父节点结论链】
{ancestry}

【任务】
1. 结合父节点结论链的已有认识，针对当前主题与核心问题给出一段简要研究结论（200 字以内，讲清关键事实、你的判断与依据）；
2. 提出最多 {max_children} 个最值得下钻的子问题——只提能实质推进整体研究、且与父节点已覆盖内容不重复的方向；没有值得下钻的方向就一个都不提。

【输出格式】只输出以下行协议，不要任何其他文字：
第一行输出结论行，格式是「CONCLUSION: <一段结论>」（独占一行）；
随后每个子问题独占一行，格式是「CHILD: <子主题> | <子问题>」——子主题不超过 30 字，子问题是一句可直接研究的问题。
```

解析（`parse_explore`，C1 规范行提取先例）：只认行首 `CONCLUSION:` / `CHILD:` 规范行（容忍整行加粗、全角冒号/竖线）；code fence 与 blockquote 内是引用材料一律跳过；`<占位符>` 模仿在 CHILD 与 CONCLUSION 两类行都丢弃（R-D4 N1）；缺 `|` 降级为 topic-only 子节点；topic 大小写不敏感去重、封顶 3 条；结论取**第一条非占位符**规范行（prompt 要求第一行），无规范结论行则折叠全文头部（800 字）做 summary。任何失败模式返回可挽救部分，永不 raise。

## 2. BFS / 剪枝语义

- **认领序**：`ORDER BY depth ASC, created_at ASC`（同层优先），每 tick 至多 `NODES_PER_TICK=3` 个节点。条件认领 UPDATE 是**一条原子语句**内嵌三重守卫：树仍 live（pending/exploring）、单树 running 数 < `node_concurrency`（admin_state 配置）、**父节点已 completed**（H1 防御纵深）——重叠 tick / 多进程都不可能超跑或提早认领子节点。
- **完成事务（H1 事务边界终稿）**：每个节点收尾 = 一个写事务：①条件终态父（`WHERE status='running'`，summary/task_id 落库）→ ②同事务插全部子行（pending 经 count+树状态双守卫的 `INSERT OR IGNORE … SELECT`；depth/预算超限落 pruned，同样带树状态守卫）→ commit → 发 `tree.node_completed` → settle（翻牌+宣布）。丢认领即弃整批；崩溃不暴露半成品层——任何可认领的 pending 子节点，其祖先链必然全部 completed 且 summary 已持久。
- **树生命周期**：`pending →（首个节点认领时）exploring → completed / failed`；`stop_tree` → `stopped`（树翻牌+pending 剪枝一个事务，H2）。completed 需 ≥1 个 completed 节点，全失败/全剪枝落 failed。**事件与翻牌解耦**：`tree.completed` 由 `announced_at` 条件认领单发，仅在「终态 + 排空（无 pending/running）」时命中（M2）。
- **子节点预算**：`max_nodes` 计**非 pruned 行**（含根）；count-guarded INSERT 是预算仲裁器，并发完成不可能联合超预算。
- **剪枝**：depth 超限（child_depth > max_depth）或节点预算耗尽的子问题落 `pruned` 行（born terminal，viewer 可见「没探的方向」）；`stop_tree` 把 pending 剪成 pruned，running 自然完成（结果保留，其子插入被树状态守卫拦下——stop means stop）。子 topic 与父 topic 相同的镜像方向直接丢弃（rule 8 有界递归精神）。
- **崩溃恢复**：boot 时 `recover_orphans()`——终态树下的 running 孤儿剪 pruned，活树下 running→pending（task_id 清空）；tick 开头的 sweep 三件事：剪掉终态树下搁浅的 pending 行（H2 兜底）、补翻「全节点终态但树没翻」的 exploring 树、补宣布「翻了没宣布」的终态树（宣布故意不放 boot：让 `tree.completed` 在 exporter register 之后发出）。
- **每日建树上限**：`research_tree_booked:<SGT date>` admin_state 计数行，条件 UPDATE 在树落库**前**原子占坑（factcheck `_reserve_attempt` 先例，不退还——计的是占坑尝试数，N2）。

## 3. API 契约（SSE viewer 前端归 D7/后续）

- `POST /api/research/tree` `{root_topic, max_depth?≤4(默认2), max_nodes?≤50(默认12)}` → 200 树 JSON；日配额耗尽 → 200 `{"refused":"daily_cap","cap":N,"booked_today":N,"root_topic":…}`（research 队列 cooldown 拒绝形状）；空 topic/超长 → 400；越界/多余字段 → 422。
- `GET /api/research/tree/{id}` → 树行（含 `announced_at`）+ **扁平** `nodes` 数组（BFS 序），节点带 `parent_id` 引用，viewer 客户端重建嵌套。404 未知 id。节点字段：`id, tree_id, parent_id, depth, topic, question, status(pending/running/completed/failed/pruned), task_id, summary, score(预留恒 null), created_at, finished_at`。
- `GET /api/research/trees?status=&limit=` → 列表（含 `nodes_total` / `nodes_completed` 聚合列，列表页进度条即用）。
- `POST /api/research/tree/{id}/stop` → 200 树 JSON（幂等；终态树重复 stop 不再发事件）。404 未知 id。注意返回的是 stop 时刻快照：running 节点仍会自然收尾，最终形态以 `tree.completed` 快照/重新 GET 为准。

**SSE**：复用现有 `GET /api/events/stream?types=tree.`（无新端点）。两个事件，`ref_kind="research_tree"`、`ref_id=tree_id`（viewer 按 ref_id 过滤即可）：

- `tree.node_completed` payload `{tree_id, node_id, depth, topic, status: completed|failed, task_id, children_added, children_pruned, summary?≤300}` — 每个节点到达终态发一次。
- `tree.completed` payload `{tree_id, root_topic, status: completed|failed|stopped, nodes: {status: count}, finished_at}` — **排空后的终态快照，恰好一次**（R-D4 M2）：仅当树终态且无 pending/running 节点时发出，payload 从库行读出。stop 后若有 running 节点自然收尾，事件等最后一个收尾者；viewer 收到即可安全断开，不会漏任何迟到的节点结果。

建议 SPA 路由 `/research/tree/:id`：进页先 `GET /api/research/tree/{id}` 全量渲染，再订阅 SSE 增量刷新（收到 `tree.completed` 后重新 GET 一次终稿并终止订阅）。状态枚举的 canonical import 点：`research_tree.TREE_STATUSES` / `NODE_STATUSES`（/api/contract 若要收编，从这里 import，勿复述）。

## 4. 需要主代理执行的挂载 1：main.py（D4 无权修改）

lifespan 里 `research.recover_orphans()` 之后加：

```python
    from .institute import research_tree as research_tree_mod
    await research_tree_mod.recover_orphans()
```

`create_app()` 的 import 元组加 `research_tree as api_research_tree`，include 元组加 `api_research_tree.router`（与 `api_research.router` 相邻即可，都在 SPA fallback 之前）。

## 5. 需要主代理执行的挂载 2：scheduler.py 5 分钟门控 job（D4 无权修改）

job 定义（与 `_research_tick_job` 同风格；**gated=True**——tick 经 executor.submit 发起新模型调用，必须尊重维护暂停；`tests/test_maintenance.py::test_job_gating_registry_matches_semantics` 的 gated 清单若要更新，把它加进去）：

```python
@metered("research-tree-tick", gated=True)
async def _research_tree_tick_job() -> None:
    from . import research_tree
    await research_tree.tick()
```

`start()` 里挂 interval（间隔按 ROADMAP 原文硬编码 5 分钟；如主代理想开旋钮，config 加 `research_tree_tick_minutes: int = 5` 再引用即可——模块自身不读该字段，零耦合）：

```python
    every(_research_tree_tick_job, "research-tree-tick", minutes=5)
```

`tick()` 自身永不 raise，内部串行做：sweep（剪终态树下搁浅 pending、补翻/补宣布卡住的树）→ 条件认领 ≤3 个 pending 节点（BFS 序 + 单树并发上限 + 父 completed 守卫）→ 并发跑完（全局信号量约束真实并行度）。

## 6. 需要主代理/D3 执行的挂载 3：vault exporter handler（exporter.py 本轮归 D3）

`register()` 加一行：

```python
    bus.on("tree.completed", _on_research_tree_completed)
```

handler 精确代码（rows are truth：事件触发时从两张表全量重投影；`stopped`/`failed` 树同样导出——树的终态就是这份档案；永不 raise）。R-D4 M2 修复后事件即「排空后的终态快照」，触发时**不可能**再有 pending/running 节点——投影不会把 `[进行]`/空结论写进 Vault（badge 表保留这两态只为防手工重放的健壮性）：

```python
# ---- research tree (BFS explore) --------------------------------------------

async def _on_research_tree_completed(event: bus.Event) -> None:
    """tree.completed → Research/<root_topic>/tree.md（节点树 markdown 投影）。"""
    if not get_writer().enabled:
        return
    try:
        tree_id = str(event.ref_id or "")
        tree = await db.query_one("SELECT * FROM research_trees WHERE id = ?", (tree_id,))
        if tree is None:
            return
        nodes = await db.query(
            "SELECT * FROM research_tree_nodes WHERE tree_id = ? ORDER BY depth, created_at, id",
            (tree_id,),
        )
        by_parent: dict[str | None, list[dict]] = {}
        for n in nodes:
            by_parent.setdefault(n["parent_id"], []).append(n)
        badge = {"completed": "[完成]", "failed": "[失败]", "pruned": "[剪枝]",
                 "pending": "[待研]", "running": "[进行]"}
        lines: list[str] = []

        def _walk(parent_id: str | None, indent: int = 0) -> None:
            for n in by_parent.get(parent_id, ()):
                mark = badge.get(n["status"], f"[{n['status']}]")
                q = f"（{n['question']}）" if n["question"] else ""
                lines.append(f"{'    ' * indent}- {mark} L{n['depth']} {n['topic']}{q}")
                if n["summary"]:
                    lines.append(f"{'    ' * indent}    - 结论：{str(n['summary'])[:300]}")
                _walk(n["id"], indent + 1)

        _walk(None)
        header = (
            f"- 状态：{tree['status']}　节点：{len(nodes)}　"
            f"max_depth={tree['max_depth']}　max_nodes={tree['max_nodes']}\n"
            f"- 创建：{tree['created_at']}　结束：{tree['finished_at'] or '—'}"
        )
        body = f"## 研究树概览\n\n{header}\n\n## 节点树\n\n" + "\n".join(lines)
        rel = f"Research/{_slug(tree['root_topic'])}/tree.md"
        await get_writer().write_note(
            rel, {"type": "research_tree", "tree_id": tree_id}, body,
            artifact_kind="research_tree",
            artifact_id=f"research-tree:{_slug(tree['root_topic'])}",
        )
        log.info("vault export: %s", rel)
    except Exception:
        log.exception("research tree export failed for %s", event.ref_id)
```

说明：路径按 ROADMAP 原文固定 `Research/<root_topic>/tree.md` —— 同一 root_topic 的多棵树共享该文件，**后完成者覆盖**（`artifact_id` 按 slug 锚定路径而非 tree_id，避免 hash-ledger 把第二棵树判成 conflict sibling）。若主代理想一树一档，把 rel 与 artifact_id 都改成带 `{tree_id}` 后缀即可，本模块零改动。

## 7. 测试基线

`tests/test_research_tree.py` 31 例全绿（R-D4 返工 +7）；全量套件在 R-D4 验证时刻 **764 passed / 10 skipped，零失败**（并行第四轮分区仍在追加测试，skip +1 亦来自并行分区）。oracle 结构照 R-C1 返工先例：树展开走生产形态行协议 fixture（仅对 EXPLORE_PROMPT 拦截 executor.submit，其余 submit 走真 executor/echo 路径），fixture 对**每次** explore 调用审计 H1 不变量（被探索节点的父必须 completed 且 summary 非空，违例即败）；另保留一个真 echo 全链用例锁定「回显 → 零子节点、节点照常 completed、task 行落审计」。覆盖：BFS 逐层序、**子 prompt 父链承载全部祖先结论（无「（无结论）」空槽）**、**父未完成的 pending 子不可认领**、**丢认领弃整批**、max_depth/max_nodes 剪枝行、全败树落 failed、崩溃恢复（running 重排 + 终态树下孤儿剪 pruned + sweep 补翻补宣布 + 幂等）、子插入 exact-topic 重放幂等（唯一索引仲裁）、stop 三场景（**mid-flight 竞态零搁浅 pending + 事件推迟到排空**、已提交 pending 子同事务剪枝即刻宣布、pending 根剪枝幂等）、终态树下搁浅 pending 的 sweep 兜底、并发 tick 单认领、node_concurrency=1 in-flight 恒 1、每日 cap 并发建树不可突破 + cap=0 全拒、防御解析 7 组（规范行容忍、引用/围栏跳过、CHILD/CONCLUSION 占位符模仿、去重封顶、整 prompt 反射免疫、父链敌意注入免疫、镜像子丢弃）、API 全往返（200/400/404/422/refused）。

## 8. 遗留风险 / 边界

- **挂载前零自动探索**：第 4/5 节落地前，树只能建不会跑（手动驱动可临时 `python -c` 调 `research_tree.tick()`，或等 scheduler 挂载）。`/api/research/tree*` 路由在 main.py include 前不对外可用（测试自行 include router）。
- **节点失败不重试**：explore task 失败节点永久 failed（research_queue 同款语义——操作员可手动把节点行改回 pending，tick 会重新认领；把 **completed** 父重置回 pending 重驱也受 H1 守卫保护：重驱期间其子暂不可认领，重完成后恢复，子插入为 exact-topic 幂等）。全失败树落 `failed`。**勿把带子节点的父改成 failed**——其 pending 子会因父守卫永不可认领、树滞留 exploring（自然运行到不了这个状态；误操作用 `stop_tree` 解围）。
- **echo/回显型手驱动的探索恒零子节点**（结构性免疫的另一面）：树会“一层即完”。生产环境 `INSTITUTE_RESEARCH_HANDS`（默认 codex,agy）为真实手，不受影响；测试环境这正是免疫用例的断言。
- **每日计数行按日累积**在 admin_state（`research_tree_booked:<date>`，一天一行几字节）；janitor 若做 30 天清理可顺手删（非本次范围，factcheck_attempts 同款）。计数语义是「占坑尝试」：建树事务极端失败时坑不退（N2，factcheck 同款保守取舍）。
- **score 列预留未写**；`tree.node_completed` 事件对 `pruned` 行不发（born terminal，批量可见于 GET 树 JSON / `tree.completed` 终态快照的计数）。
- 单树并发上限是**每树**语义（`node_concurrency`）；跨树总并行度由 executor 全局信号量（3）天然封顶，无需另设。
- `research_tree_limits` / 计数行均为 admin_state 行，config.py 零新增（B1 §1 先例）；`roadmap/backlog.json` 状态迁移由主代理推进，D4 未动。
