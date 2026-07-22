# REVIEW-D4 — Phase 7 BFS research tree 独立审查

## 结论：FAIL

定向 24 例与 `compileall` 均通过，模型手限链、单语句认领/节点预算仲裁、解析器主体和迁移纪律也成立；但核心并发不变量仍有两个可复现的高严重度缺陷：子节点会在父节点结论落库前被另一 tick 认领，且 `stop_tree()` 的非事务化两步写入可留下永远不可再认领的 `stopped + pending` 行。停止事件还会早于 running 节点真正收尾，使 PATCH-NOTES 拟议的 SSE 终止与 Vault 投影永久失真，因此当前不能按原挂载说明集成。

## 逐项结论

1. **research 手硬规则 10：PASS** — `app/institute/research_tree.py:347-352,502-506` 是独立实现：按 `research_hand_names` 进程内轮询，并把同一 tuple 传为 `fallback_chain`；经 `executor._fallback_candidates/resolve_chain` 后首选手轮转、失败候选仍严格局限于该链，不会落到默认手（但没有复用 workflows 的 opt-in weighted 分支）。
2. **BFS / 并发 / max_nodes：FAIL** — claim UPDATE 与 count-guarded INSERT 各自确为 SQLite 原子单语句，且恰好满额边界正确（非 pruned、含根）；但“先插子、后终态父”暴露了可认领窗口，崩溃恢复也会让父子同轮并发，见 H1；精确幂等键只是 `(tree_id,parent_id,topic)`，见 M1。
3. **防御解析：PASS-WITH-NIT** — `_quote_material` 折叠换行并打断大小写/全角冒号 token，ancestry/topic/question 均中和；只取规范行、跳过 fence/blockquote、topic casefold 去重且封顶 3；但结论占位符没有丢弃，见 N1。
4. **树终态 / stop / stall：FAIL** — 正常树只有至少一个 completed 节点才 completed，全 failed/pruned 的 exploring 树为 failed；stall sweep 阈值实际为 0（每 tick 立即扫 terminal-only exploring），正常写序下误翻风险低；stop 重复调用不重发事件，但其并发与“终态后仍变更”语义不成立，见 H2/M2。
5. **每日占坑：PASS-WITH-NIT** — 条件 UPDATE 在建树前占坑且不退还，与 C1 `_reserve_attempt()` 的“尝试即计数”机制一致，并发不会越 cap；不过 DB 建树失败并未消耗模型额度，仍记作 `created_today` 是保守但命名失真的取舍，见 N2；按日期前缀清理 30 天前 counter 的 janitor 建议合理。
6. **API：PASS-WITH-NIT** — 4 个裸 router 端点可用，`max_depth 0..4`、`max_nodes 1..50` 双层校验成立；daily_cap 用 200 延续现有 cooldown/business-refusal union 形状，内部客户端可接受，但通用 HTTP 客户端用 429 + `Retry-After` 会更清晰；`status` 查询值未枚举校验只会返回空列表。
7. **迁移 0020：PASS-WITH-NITS** — 无 BEGIN/COMMIT/ROLLBACK/END/ATTACH/VACUUM/PRAGMA，全部 additive/idempotent；tree FK CASCADE、parent 自引用 FK、claim/tree/唯一索引齐全。`task_id` 只是逻辑链接而非 FK（与旧表惯例一致），且唯一索引使用 BINARY topic，限制见 M1；PATCH-NOTES 把它称为“部分唯一索引”不准确，实际没有 WHERE。
8. **硬规则：PARTIAL** — 唯一模型路径、`bus.now_iso()`、条件 rowcount、tick 捕获业务异常均通过；新 prompt 常量与 PATCH-NOTES 逐字一致，仓内没有上游 `research-worker/prompt.ts/parser.ts`，故无法证明对外部参考实现逐字移植；stop 的多语句状态迁移不具备操作级原子性。
9. **定向验证：PASS** — `.venv/bin/python -m compileall app -q` 退出 0；`.venv/bin/python -m pytest tests/test_research_tree.py -q` 为 `24 passed in 1.09s`；按要求未跑全量。
10. **PATCH-NOTES 挂载：FAIL（当前未挂载且 exporter 方案需先改）** — `main.py` 目前缺 boot recovery 与 API import/include，`scheduler.py` 缺 gated 5 分钟 job，`exporter.py` 缺 handler/register；三处说明与现状位置相符，maintenance 完整集合测试也需同步，但现拟议 exporter 在 stopped 事件上会读取未收尾节点，不能原样应用，见 M2。

## 分级问题

### H1（高）子节点可在父结论落库前被认领，正常重叠 tick 与崩溃恢复都会破坏父链语义

- `app/institute/research_tree.py:418-431` 先逐条提交 pending 子节点；父节点的 `summary/task_id/status='completed'` 直到 `529-533` 才写入。
- `app/institute/research_tree.py:365-386` 的候选/认领条件只检查子节点 pending、树 pending/exploring 和 running 数量，没有要求 `parent_id IS NULL OR parent.status='completed'`。
- 因而另一进程/tick 可在插入后立即认领子节点；其 `_ancestry_block()` 读到的父节点仍是 `running, summary=NULL`。定向复现输出为：`EARLY_CLAIM 过早子节点 {'status': 'running', 'summary': None} ...（无结论）`。
- 崩溃窗口更确定：子已插入、父尚未终态时进程死亡；`recover_orphans()`（`591-599`）把父改回 pending，下一 tick 先认领低层父，再在 `node_concurrency>=2` 时同轮认领已有子，二者并发执行。父重跑若失败，子仍可能已经基于空父链完成；若输出变化，还会改变原树分叉。
- 建议把“校验 running claim + 写父 summary/task_id/completed + 插入/剪枝全部子节点”放进同一个写事务，事件在 commit 后发；claim 再增加 parent-completed 防御条件。这样崩溃只能发生在整批提交前或后，不会暴露半成品层。

### H2（高）`stop_tree()` 的 prune 与 stopped 翻牌不是一个事务，可制造永久 pending 节点

- `app/institute/research_tree.py:615-624` 先单独执行 pending→pruned，随后才单独执行 tree→stopped。
- running 父节点可在两条语句之间通过 `418-428` 插入新 pending 子节点；第二条语句把树置 stopped 后，该节点不会被第一条 prune 追上，也永远不再满足 claim 查询的 `t.status IN ('pending','exploring')`（`368`）。
- 强制该合法交错的定向复现结果为：`STOP_GAP {'node_status': 'pending', 'tree_status': 'stopped'}`。
- `_insert_children()` 对 pruned fallback（`444-451`）也不是 tree-status guarded INSERT；即使先读到 exploring，stop 仍可在检查与写入之间发生，使 stopped 树在终态后新增 pruned 行。
- 建议把树 conditional stop 与 pending prune 放在同一 `db.transaction()`；子插入也必须在同一 SQL/事务内检查树仍 exploring。SQLite 的单写者序列化会使插入批次要么先于 stop 并被 prune，要么后于 stop 并被拒绝。

### M1（中）崩溃重放只对“完全相同且大小写相同的 topic”幂等

- 唯一仲裁器是 `migrations/0020_research_tree.sql:65-66` 的 `(tree_id,parent_id,topic)`；SQLite 默认 BINARY collation。代码的 duplicate 复核同样是精确 `topic = ?`（`app/institute/research_tree.py:432-438`）。
- 同一次响应内 parser 会 casefold 去重，但父节点重新调用模型后，`AI`→`ai`、标点变化或全新提议都能再插一批；现有 `tests/test_research_tree.py:361-380` 只重放完全相同的 canned 输出，不能证明非确定模型重放幂等。
- 这不等于 exact duplicate 会重复：相同 key 会被 `INSERT OR IGNORE` 正确挡住；问题是 PATCH-NOTES 所称“父重跑幂等”范围过宽。H1 的事务化修复可直接消除主要重放窗口；若仍允许人工重驱，应存规范化 child key 或明确 exact-topic 语义。

### M2（中）`tree.completed(stopped)` 不是最终快照，SSE 与拟议 exporter 会永久漏掉后续节点结果

- `stop_tree()` 在 `app/institute/research_tree.py:620-634` 立即写 stopped 并发 `tree.completed`，但其文档又允许 running 节点自然完成；这些节点随后在 `529-543` 改写 completed/summary，并且 `_maybe_finish_tree()` 只更新 exploring（`473-476`），不会再发树事件。
- 定向复现：事件发出时节点仍 `running`，payload 只有 `{status:'stopped', pruned_pending:0}`；释放模型后节点变 `completed, summary='完成'`，`tree.completed` 总数仍为 1。
- `PATCH-NOTES-D4.md:49-54` 要求 viewer 收到该事件即断开，并宣称 stopped payload 具有 `nodes`，实际 stop payload 没有；`99-143` 的 exporter 也只监听这一事件，极可能把 `[进行]` 和空 summary 永久写入 Vault。
- 必须二选一：立即 stop 时把 running 节点也冻结为 terminal/忽略其迟到结果；或引入 stopping/drained 语义，等 running 全部落定后再发唯一最终事件。当前 exporter handler 不应原样挂载。

### N1（低）占位符模仿只在 CHILD 字段丢弃

- `_is_placeholder()`（`app/institute/research_tree.py:143-145`）只在 child topic/question 分支 `183` 调用；`CONCLUSION: <一段结论>` 会在 `171-175` 被接受为真实 summary。
- 若 PATCH-NOTES 的“`<占位符>` 模仿行丢弃”意指所有协议行，应同时过滤 conclusion；否则把文档收窄为 CHILD placeholder。

### N2（低）每日“不退还”机制成立，但 counter/API 字段名把尝试数称为创建数

- `_reserve_tree_slot()`（`237-254`）在 tree/root 事务（`288-301`）之前提交；后者因磁盘、约束或偶发 ID 冲突失败，slot 仍烧掉。
- 这与 factcheck 的无退款实现同形，但 factcheck 失败也已发起模型调用，而此处失败可能尚无 durable tree、无模型花费。若保留保守策略，建议至少把 `created_today`/注释改为 booked/attempted；若 cap 真指成功创建树，则把 counter 与 tree/root 放进同一事务。

## 已确认成立的关键细节

- **SQLite 原子性**：claim 的 running-count 子查询与目标行 UPDATE 是同一 statement；同进程还有 `db._write_lock`，跨进程 SQLite 仍只允许一个 writer，后到 writer 在前者提交后重新执行条件，因此不会共同越过 concurrency。前置候选 SELECT 可陈旧，但 `id/status/count` 条件会重新仲裁。
- **max_nodes 边界**：guard 使用 `< max_nodes`，计数条件是 `status != 'pruned'`；根从创建起即非 pruned，所以 `max_nodes=2` 时只允许再落一个可探索子节点，恰好满额后的提议进入 pruned。
- **树完成规则**：`_maybe_finish_tree()` 无 live 节点后按 completed 计数决定 completed/failed；全 failed/全 pruned 的 exploring 树为 failed，stopped 不会被 sweep 改写。
- **stall sweep**：没有时间阈值；每次 tick 开头立即处理所有 terminal-only exploring 树。依赖“任何未来子节点都已在父终态前提交”这一不变量时不会误翻；对人工修复/外部延迟插入没有宽限期。
- **挂载现状**：`app/main.py:113-115,164-203`、`app/institute/scheduler.py:159-163,333-351`、`app/vault/exporter.py:509-520` 均确认尚无 D4 挂载；这与分区约定一致，不是偷偷遗漏，但 H1/H2/M2 修复前不应集成。
