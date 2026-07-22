# PATCH-NOTES-LOOP-P5 — research_tree 终态翻转守卫与 tree.completed 崩溃安全

来源：loop-fix 工作包 P5（research_tree 终态与事件，中优）。改动全部落在
`app/institute/research_tree.py` + `tests/test_research_tree.py`（另有
`tests/conftest.py` 两行事件循环 rebind，见下）。无新 migration，无新依赖，
无新执行路径。

## P5a — `_maybe_finish_tree` 翻转 UPDATE 加 NOT EXISTS 守卫

**竞态形态**：`_maybe_finish_tree` 原先只用两次 advisory 读（活节点数、完成数）
决定翻转，翻转 UPDATE 本身仅有 `status='exploring'` 条件。`retry_node` 若落在
读与写之间（failed→pending，此时树还是 exploring，其 reopen 分支
`WHERE status IN ('completed','failed')` no-op），翻转照样命中——树被翻成终态，
刚重试的 pending 节点搁浅在终态树下，下个 tick 的 `_sweep_trees` 把它**静默
prune**：操作员显式请求的重试被丢弃且无事件可查。

**修法**：给翻转 UPDATE 加上与 `_announce_if_drained` 同款的
`NOT EXISTS (… status IN ('pending','running'))` 守卫，让数据库语句本身成为
仲裁者。SQLite 单写者下只剩两种交错：翻转先提交（retry 事务随后按既有路径
reopen 终态树）、或 retry 先提交（守卫看到 pending 行，翻转输，树留在
exploring，节点走正常 BFS drain）。`final`（completed/failed）取值不会因守卫
而过期：无活节点时只有 retry_node 能改节点状态，而它必然制造一个阻断翻转的
pending 行。

**回归测试**：`test_finish_flip_blocked_by_concurrent_retry_race` —— 用
`db.query_one` 包装器把 `retry_node` 精确注入 advisory 读与翻转之间（带
`state["injected"]` 断言防匹配器漂移空转），断言翻转输、树保持 exploring、
重试节点最终被真实探索完成而非被 prune、恰好一个 `tree.completed`。修前红
（旧代码翻转返回 True），修后绿。

## P5b — tree.completed 改「emit 成功后再置位」（无新表的 outbox 姿态）

**崩溃窗口**：原顺序是先条件宣占 `announced_at` 再 `bus.emit`。崩在两者之间
时标记已 durable、事件行从未写入，而 sweep 只找 `announced_at IS NULL` 的树
——快照事件**永久丢失**，vault 导出永不发生。

**修法评估**：factcheck dispute outbox（`factcheck_dispute_outbox` 表 +
每分钟 drain job）是同事务意图行 + 异步投递 + delivered 标记的完整 outbox；
但该表列结构（dispute_id/fact_card_id/recipient_id）是 factcheck 专用，复用
即滥用，新表又需要 migration（本包禁止）。关键观察：research_tree **已经有**
outbox 的两个组成部分——`announced_at IS NULL` 就是未投递意图状态，tick sweep
就是重试 drainer，唯一的 bug 是置位顺序。故采用工作包指定的最小改法：

- 资格判定改为一次带全部条件（终态 + 未宣告 + 已排空）的 SELECT；
- `bus.emit` 先落事件（durable）；
- **之后**才条件宣占 `announced_at`（条件原样保留，rowcount 检查）。

**语义（R3 收窄后的准确承诺）**：本卡承诺的 at-least-once 只覆盖
**`tree.completed` 事件行落库**——崩在 emit 前 → 无任何 durable 变化，sweep
重试；崩在 emit 后置位前 → 事件行已在，`announced_at` 仍 NULL，下次 sweep
重发一次（与 factcheck event-outbox 宣告的姿态一致，R2 P1-3）；重复投递对
消费者安全（vault exporter 从 DB 行重读投影且 `write_note`
skip-if-unchanged，SSE 断线消费者本就走 events 游标）。

**本卡不承诺 Vault 投影 at-least-once（R3 P2 确认的已知遗留）**：
`announced_at` 只确认事件行 durable，不确认消费者副作用成功——`bus.emit()`
吞掉 handler 异常、research-tree exporter 自身也捕获异常后返回，所以
`write_note` 首次失败时 emit 仍视为成功、置位照常发生、sweep 不再重发，
目标 note 会缺失。恢复手段：vault 写幂等（rows are truth、note 是投影），
任何时候手工/后续 reproject 均可安全补投；根治需要 exporter 侧按 event id
的消费 cursor/outbox drain（动 exporter.py/bus.py，超出本卡文件边界，见
「遗留/后续卡」）。

正常运行的单发保障来自新增的模块级 `_announce_lock`
（单进程应用内串行化 announce 段；跨重启的真相仍完全由数据库状态承载）。
retry_node 若在窗口内 reopen 世代，置位 UPDATE no-op（记 warning），新世代
排空后自然重发。锁跨 emit 持有无死锁风险：`tree.completed` 唯一 handler
（vault exporter）只读行 + 写笔记，不回调本模块；所有 announce 调用点都在
`db.transaction()` 之外。

**附带的 conftest 改动**：`tests/conftest.py` 在既有「rebind module-level
primitives to the current event loop」区块加两行（import + 每测重建
`_announce_lock`），照搬 `research_mod._claim_lock` 先例——pytest-asyncio 一测
一循环，不 rebind 会报 "bound to a different event loop"（首轮测试实际撞到）。

**回归测试**：
- `test_announce_crash_before_emit_never_loses_the_event` —— monkeypatch
  `bus.emit` 首次对 `tree.completed` 抛异常模拟崩溃，断言崩后 `announced_at`
  仍 NULL、零事件，下个 tick sweep 重发恰好一个事件并置位。修前红（旧代码
  崩后 `announced_at` 已置位，事件永失），修后绿。
- `test_concurrent_settles_emit_exactly_one_snapshot` —— 6 个并发
  `_settle_tree` 只发一个快照（emit-先行不得双发的 regression guard）。

## P5c —（可选项）expired/rate_limited 有界自动重试：判断为不做

- `research_tree_nodes` 无 attempts 列，本包禁新 migration → 跨 tick 的持久
  attempt 计数无处安放；无持久计数的 requeue 就是无界重试，恰是「烧配额」禁区。
- rate_limited 在 executor 内部已有一次链内自动重试（`_execute` 的
  next-hand retry）；submit 返回 rate_limited 意味着整条 confined chain 都在
  冷却，同 tick 立即重试必然再撞冷却墙。
- 手动 `retry_node`（带 `tree.node_retried` 事件与 409 冲突语义）已覆盖恢复
  路径。若未来要做，应由 orchestrator 分配 migration 编号加 attempts 列，
  上界硬编码（建议 1）。

## 附带项（P1 关联）— NODES_PER_TICK=3 评估

工作区中该常量已带 LOOP-P1 注释（说明第 3 个节点在 2 只 research hand 上
doubled-up 之所以安全，是因为 executor 先取 hand mutex 再取全局信号量，排队
节点不占全局槽）。本轮核对了 `app/router/executor.py` 当前代码
（`async with _hand_lock(hand.name), _sem()`）与注释一致，**未改数值**——
降为 `min(len(research_hand_names), max_concurrent-1)` 会与另一 agent 正在修
的 executor 耦合，P1 修好后 3 无害且能吃满测试/多 hand 配置的并行度。

## 遗留/后续卡（orchestrator 据此开卡，本卡不动 backlog.json）

- **Vault 投影 at-least-once 缺口（R3 P2）**：`tree.completed` 事件行已
  at-least-once，但消费方 `write_note` 失败不重试（`bus.emit` 吞 handler 异常
  + exporter 自捕获），需要 exporter 侧按 event id 的消费 cursor/outbox drain
  （ack 后前进）才能把 Vault 投影升级为 at-least-once；vault 写幂等，落地前
  可随时手工 reproject 补投。涉及 `app/vault/exporter.py`（或 `app/bus.py`），
  超出本卡边界。
- expired/rate_limited 节点的有界自动重试（见 P5c）：若做，需 migration 加
  attempts 列（编号由 orchestrator 分配），上界硬编码。

## 验证

- 模块：`.venv/bin/python -m pytest tests/test_research_tree.py -q` →
  `39 passed in 3.39s`（修前定向红灯：`2 failed, 1 passed, 36 deselected`）。
- 全量：`.venv/bin/python -m pytest tests -q` →
  `1051 passed, 2 skipped in 105.40s (0:01:45)`。
- `.venv/bin/python -m compileall app -q` → 通过。
- R3 收窄轮（纯记档：本文 + research_tree.py 两处 docstring 对齐，无逻辑
  改动）：`39 passed in 3.88s`，compileall 通过。

不动 `roadmap/backlog.json`（orchestrator 统一补卡）。
