# R5 factcheck / chain 合入前可靠性复核

日期：2026-07-21
范围：R4 对 `factcheck` 原子预订、复验 verdict 换代，以及 `chain` graph-generation / rotation 的最新修复与交互
结论：**REQUEST_CHANGES**

发现分布：**P0 0 / P1 4 / P2 0 / P3 0，共 4 项**。

R4 的两个局部机制本身成立：

- `_book_verification()` 确实在一个 `db.transaction()` 内完成 daily slot 条件自增、card `attempts+1/verify_task_id` 绑定和 born-queued `tasks` 插入；预算或 lease 条件失败通过异常回滚全部三条写入。
- `chain:graph_generation` 覆盖了当前仓库全部三个 production `chain_nodes` 写点，且都与节点写入处于同一事务；rotation 的损坏状态、世代变化和最终 merge 前复核也基本闭合。

但 task/card 的恢复协议、reset generation 的读侧与 outbox 侧，以及 alias 成本上限仍有四个可导致重复模型调用、错误事实复用、错误争议投递或错误图合并的 P1。

## [P1] app/institute/factcheck.py:1588-1624, app/router/executor.py:553-562 — stale recovery 完全不读 verify_task_id，已完成任务会被丢弃并重复验证

### 问题

`verify_task_id` 只在 `_book_verification()` 写入，`_recover_stale_running()` 恢复 card 时没有查询它对应的 task。恢复逻辑只按 `verify_started_at` 把所有 stale `verifying` card 无条件改回 `pending`，随后按 card attempts 决定是否耗尽。

启动顺序中，`executor.recover_orphans()` 会先把 `queued/running` task 标成 `failed`，然后 scheduler 才启动；所以当前单进程 boot 不会和 factcheck 同时驱动同一 queued task。问题在于：

- task 已经 `completed`、进程却在 card settle 前崩溃时，boot sweep 不会动该 terminal task；
- 60 分钟后 card sweep 仍把 card 重开，下一轮再预订一个 task，已有完整输出不会被解析；
- 若这是第 3 次 attempt，card 会直接被 `_settle_exhausted_card()` 写成 `UNVERIFIABLE`，即使绑定 task 的输出已经是可用的 VERIFIED/DISPUTED；
- cancel、queued/running orphan 等 terminal task 状态也不驱动 card 的即时收敛，card 只能等待按时间 stale，最长还要叠加 tick 周期。

因此 durable task 目前只是“attempt 有一行”，还不是 card 可恢复状态机的一部分；R4 所称“已有 durable task 可恢复或明确结算”只对 task 行成立，对 card verdict 不成立。

### 复现

在临时库中：

1. claim card 并调用 `_book_verification()`，得到 `task1`；
2. 模拟 executor 已完成：把 `task1` 写成 `completed`，保存合法 VERIFIED 输出；
3. 模拟 card settle 前崩溃，把 card lease 调旧；
4. 依次运行 `executor.recover_orphans()` 和 `_recover_stale_running()`；
5. 再 claim/book 一次。

实测：

```text
boot_orphaned_completed_task=0
card_after_stale_recovery={status: pending, attempts: 1, verify_task_id: task1}
second_booking={task2, ok}
daily_slots=2
tasks=[task1: completed, task2: queued]
```

第一次已有完整 terminal output，系统仍安排第二次模型调用。

### 建议

增加 factcheck 自己的 task-aware recovery，并在 boot 的 `executor.recover_orphans()` 之后、scheduler 启动之前立即运行：

- `completed`：从现有 task output 走只做解析/settle 的路径，不再 `_execute()`；
- `failed/rate_limited/cancelled/expired/overcommitted`：按该已预订 attempt 释放或耗尽；
- `queued/running`：boot sweep 后应已变 terminal；非 boot 场景若仍有 live owner，不得重开 card；
- task 缺失或 card/task 绑定损坏：显式 quarantine/失败结算，不要静默再调用模型。

恢复和正常 settle 都应以 `card.id + lease_id + verify_task_id` 为条件，避免旧 task 的结果落到新一代 lease。若未来允许恢复方驱动 queued task，还需让 executor 返回“本调用是否赢得 queued→running claim”；当前 `_execute()` 的 claim loser 只返回 live row，不能被误当成 task failure 后释放 card。

## [P1] app/institute/factcheck.py:441-448, 1715-1782 — reset 后到新 verdict 落账前，旧 VERIFIED/DISPUTED 仍是 active reuse 候选

### 问题

R4 的 UPSERT 只保证“新 settle 成功后”card status 与单行 `verified_facts` 一致。operator reset 把 terminal card 改回 `pending` 后，旧 verdict 行仍保留原来的 `VERIFIED`/`DISPUTED`，而三个读面都只过滤 `vf.verdict` 与 expiry，不核对 card 当前状态：

- `_reuse_state()` 的向量复用门；
- `_verdict_rows()` 的 keyword claim-check；
- `claim_check()` 的向量分支。

因此 card 在 `pending` 或 `verifying` 的整个复验窗口内，旧结论仍可产生 `reused` / `self_contradicted`，也仍可作为 writing-time claim-check 命中。若 budget 耗尽、hand 持续失败或恢复延迟，这个窗口可以长期存在。R4 修复了最终 `unverifiable` settle 后的残留，却没有关闭 reset 本身到 settle 之间的同代性缺口。

当前没有正式 reset API，但 R4 的实现注释和回归测试明确把 operator reset 作为受支持场景；直接 UPDATE 是现有测试采用的实际协议。

### 复现

临时库中创建 `status=verified`、active verdict `VERIFIED`、未过期且有向量的 card，然后只把 card reset 为 `pending`：

```text
before_reset:       reused(fact=r5stalefact1)
pending_reuse:      reused(fact=r5stalefact1)
pending_claim_check VERIFIED hit, similarity=1.0
verifying_reuse:    reused(fact=r5stalefact1)
```

旧 VERIFIED 在两种非 terminal 状态下均继续生效。

### 建议

短期先让所有 actionable-fact 查询 fail closed，只接受状态与 verdict 成对一致的行：

```text
(c.status='verified' AND vf.verdict='VERIFIED')
OR
(c.status='disputed' AND vf.verdict='DISPUTED')
```

长期应提供正式、事务化的 reset/reverify 操作：递增 `verification_generation`，让旧 active verdict 在同一事务中失活/归档，再把 card 放回 pending。新 settle 必须携带 generation 条件，不能只依赖一行原地覆盖。

## [P1] app/institute/factcheck.py:1047-1081, 1122-1195, 1419-1451 — verdict 原地换代但 dispute outbox 按 card 永久幂等，会投递旧代或吞掉新代

### 问题

R4 UPSERT 保留既有 `verified_facts.id`，但 dispute outbox 的幂等键仍是：

```text
dispute_id = "disputed:<card_id>"
UNIQUE(dispute_id, recipient_id)
```

它没有 verification generation，drain 也不检查 card 当前 status、active verdict 或 generation。这产生两个对称故障：

1. 旧 DISPUTED 代的 outbox 仍 pending 时，card 被 reset 后复验为 VERIFIED/UNVERIFIABLE，旧 outbox 仍会投递 DISPUTED 事件/邮件；payload 的 `related_fact_id` 此时指向一行已经被 UPSERT 成相反 verdict 的 mutable row。
2. 旧 outbox 已 delivered 时，card 后续再次复验为 DISPUTED，`INSERT OR IGNORE` 返回旧 outbox id；即时 drain 和后台 drain 都不会产生该新一代的事件/邮件，新证据也不会写入 outbox payload。

所以“回读 live fact id”只保证新 payload 创建时拿到当前行号，不能让这个可变行号成为跨代的正确 provenance，也不能保证每一代 dispute 都有对应 durable intent。

### 复现

故障注入先建立旧 DISPUTED card/fact 和 pending event outbox，再 reset 并调用 `_settle_exhausted_card()`：

```text
current card=unverifiable
current fact id=r5outboxf1 verdict=UNVERIFIABLE
drain events=1
emitted payload kind=disputed
emitted payload related_fact_id=r5outboxf1
emitted evidence="old dispute evidence"
```

即当前 active row 已是 UNVERIFIABLE，仍投递旧 DISPUTED。随后模拟新一代 dispute 入队：

```text
reenqueue_returned_same_outbox=true
second_drain.events=0
event_outbox_rows=1 (status=delivered, payload 仍是旧证据)
```

### 建议

把 verdict 与 outbox 都纳入显式 generation：

- verdict 历史使用 immutable row id，另有 active generation；不要让历史事件引用会被原地改义的 fact id；
- outbox 唯一键至少包含 `(fact_card_id, verification_generation, intent, recipient_id)`；
- reset 在同一事务中 supersede/cancel 旧代 pending outbox；
- drain 在 claim 前核对 outbox generation 仍是 card 当前 disputed generation，不匹配则终态标记 `superseded`，绝不投递；
- 同代重试仍沿用原 outbox id，以保留现有 at-least-once/idempotent 语义。

## [P1] app/institute/chain.py:125, 393-470, 1613-1645, 1687-1698 — 512 alias cap 截断正确性集合，可把本应 ambiguous 的 candidate 错合并

### 问题

`term_offset` 已经把单节点 term 扫描拆到多 tick，使每 tick 比较成本有界；但 `_cluster_terms()` 又把 alias 数组硬截为前 512 个。production 的 `create_node()` 和 `merge_aliases()` 均不限制 alias 总数，因此第 513 个 alias 是合法、可由正式 API 写入的 graph surface，并被 `_is_known_entity`、`_term_taken_txn` 和普通 mention matching 视为真实 alias。

auto-cluster 却永久看不到它。若第 513 个 alias 是 candidate 对节点 A 的第二个匹配，而节点 B 在前 512 范围内有一个 containment 匹配，正确决策应是 ambiguous；实际只看到 B，随后把 candidate 条件宣占为 `merged_into=B`。事后的 `merge_aliases(B, candidate)` 会发现该 exact alias 已属于 A，捕获 `ChainError` 后只跳过 alias，不回滚已经写错的 candidate merge 和 mention backfill。

这不是单纯“成本有界”，而是对决定 0 / 1 / ≥2 matches 的正确性集合做截断。

### 复现

全部使用 production 写入口：

1. `create_node(A, aliases=[512 个无关 alias])`；
2. 创建 candidate `目标公司股份有限公司`；
3. `merge_aliases(A, candidate)`，使它成为第 513 个 alias；
4. 创建节点 B，name=`目标公司股份`，与 candidate 构成 containment match；
5. 运行 `_auto_cluster()`。

实测：

```text
stored_alias_count=513
cluster_alias_cap=512
clustered=1
hidden_exact_node=A
visible_containment_node=B
candidate.status=merged
candidate.merged_into=B
wrong_merge=true
```

graph generation 正确递增并重扫，但重扫后的 correctness set 仍被 cap 截断，所以 generation 机制无法修复此错误。

### 建议

不要在 matcher 内截断合法 graph surface。既然已有 `term_offset + total comparison budget`，应让全部稳定排序后的 aliases 跨 tick 扫完；每 tick 成本仍然有界。

若必须保留 512 的产品上限，则必须：

- 在 `create_node()`、`merge_aliases()` 和任何导入路径统一拒绝超过上限的写入；
- 对已有超限节点迁移/修复；
- 修复前 matcher 遇到超限节点必须保守地拒绝 auto-merge，不能把未扫描尾部当作“不匹配”。

## 已核查但未形成 finding

- `migrations/0038_fact_cards_verify_task.sql` 是 additive 单列迁移，无事务禁用语句；定向 migration 测试通过。
- `_book_verification()` 的 daily counter、card bump/binding 和 task insert 确实共用一个 SQLite 事务；`_BookingRefused` 由 `db.transaction()` 的 `BaseException` rollback 覆盖，未发现半提交窗口。
- `_run_verification_task()` 进入 executor 自有 `_execute()`：包含 queued→running 条件宣占、requested hand 的 fallback resolution、per-hand lock、global semaphore、terminal task settle；`_running` 注册发生在首次 await 前，正常 cancel/shutdown 可见。
- app boot 先同步执行 `executor.recover_orphans()`，再启动 scheduler；当前单进程顺序下没有 boot recovery 与 factcheck 同时驱动同一 queued task。finding 1 是两者没有按 `verify_task_id` 协同结算 card，而不是当前启动顺序存在双 driver。
- repository-wide production Python 写点只有 `create_node()` INSERT、`merge_aliases()` aliases UPDATE、`promote_candidate()` 新节点 INSERT；三处都在节点 mutation 同一事务中 bump generation。当前没有 production node delete/name rename 路径。
- corrupt rotation state 会删除 key 并在同一 tick 使用 default state；generation 变化会清空 `node_cursor/matches/node_id/term_offset`；稳定 generation 内 normalized/deduped/sorted term 顺序与 offset 一致。
- 最终 merge 在 `_merge_candidate_into_node()` 的同一事务中先复核 generation，再条件宣占 pending candidate；未发现“检查后、宣占前”可提交 stale evidence 的应用内窗口。
- `_save_cluster_rotation()` 的 generation read 与 advisory state save 不是同一事务，但 mutation 夹入时，下 tick 的 generation mismatch 会在任何 evidence 使用前删除 state；最终 merge 还有事务内复核，因此该处未形成 correctness finding。

## 验证

以用户给定的权威全量基线 `1113 passed / 1 skipped`、`compileall OK` 为基线。本次另外执行：

```text
.venv/bin/python -m pytest \
  tests/test_factcheck.py tests/test_chain.py tests/test_executor.py \
  tests/test_restart_recovery.py tests/test_db_migrate.py -q

227 passed in 22.53s
```

```text
.venv/bin/python -m compileall app -q
OK
```

四组故障注入均使用临时 `INSTITUTE_HOME`，未写仓库代码、测试、迁移或运行数据。

## 判决

**REQUEST_CHANGES**

P1 共 4 项：已完成 verification task 会被丢弃并重复调用；reset 窗口旧 verdict 仍可复用；跨代 outbox 会错投/漏投；第 513 个 alias 可造成错误图合并。按约定不能合入。

## 2026-07-21 闭环附录

以上判决是修补前的时间点审计记录。四项 P1 现已全部闭合：task-aware verification recovery、reset generation 隔离、generation-bound dispute outbox，以及跨 tick 全量 alias 扫描均已落地并有故障注入回归。逐项映射与验证证据见 `PATCH-NOTES-NORTHSTAR-R5-CLOSURE.md`。

当前工作区判决：**ACCEPT**（代码就绪；正式 roadmap 卡仍留在 `review` 等待 operator 验收）。
