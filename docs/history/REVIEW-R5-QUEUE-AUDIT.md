# R5 队列/租约可靠性合入前复核

日期：2026-07-21
范围：R4 最新修复及其交互（rate-limit revival / mailbox dispatch / parameter-history 0037 / operator vault sweep）
结论：**REQUEST_CHANGES**

发现分布：**P0 0 / P1 6 / P2 4 / P3 1，共 11 项**。

当前权威基线按委托方给定为 `1113 passed / 1 skipped`、`compileall OK`。本轮另跑定向回归 `112 passed`；绿测不覆盖下列 marker 两侧崩溃窗、终态与 reply 的非原子窗口及 stale reclaim 无上界路径。

## [P1] app/institute/scheduler.py:373-396, 465-483；app/router/executor.py:423-435, 553-562；migrations/0039_revival_lease.sql:9-21 — marker 落地后 queued retry 并非可恢复工作，重启会永久断掉自动 revival

### 问题

`respawn_from_row()` 通过 `spawn()` 插入 queued task 并仅在当前进程创建内存 `asyncio.Task`；scheduler 随即把永久 marker 写入 source。queued 行虽然“durable”，却没有持久 consumer：boot 的 `recover_orphans()` 不会重驱它，而是把所有 queued/running task 改成 failed。

因此在“retry 行已插入、source marker 已提交、retry 尚未完成”后硬崩溃：

1. boot 把 retry 改成 `failed: orphaned by restart`；
2. source 因永久 marker 永远不再进入 revival；
3. 没有任何自动路径重驱该 lineage。手工 retry 不能替代所宣称的崩溃安全。

### 复现

故障注入让 `respawn_from_row` 只持久插入 queued child、返回后让真实 marker 落地，再模拟 boot recovery：

```text
before restart:
  source = rate_limited, error="quota\n[rate-limit-revival:claimed]"
  child  = queued
after recover_orphans + next revival tick:
  source = rate_limited, marker unchanged
  child  = failed, error="orphaned by restart"
  new retry rows = 0
```

### 建议

把“一次 source 对应哪个 retry”变成持久协议，而不是 marker + 内存 driver：

- 在同一事务中条件 claim source、插入/绑定唯一的 `revival_task_id`（或独立 revival outbox）；
- 当前进程与 boot 都按该 durable id 条件宣占并驱动同一 queued generation；
- boot 不得先把这类 prepared/queued retry 无条件改成 failed；
- 只有 durable consumer 明确接管或终态结算后，才把 source 置为已消费。

## [P1] app/institute/scheduler.py:421-423, 448-483；migrations/0028_task_overcommitted.sql:64-67 — retry 已完成但 marker 未落地时会再次调用模型，live-only lineage 唯一索引挡不住

### 问题

marker 与 retry task 不在同一事务。`spawn()` 返回后，child 可在 marker 的 DB await 期间运行并终态；若此时崩溃，source 只留下会过期的 lease。stale reclaim 不记录“这次 lease 已经创建并完成了哪个 child”，于是再次 respawn。

`uq_tasks_lineage_active` 只约束 `queued/running`；第一个 child 一旦 terminal 就退出 partial index，不能阻止第二个 generation。故该窗口会重复真实模型调用，而不只是多一行审计记录。

### 复现

故障注入在 child completed 后、source marker 前模拟硬崩溃，再老化 lease：

```text
after crash:    completed generations=1, marker=false
after reclaim:  completed generations=2, revival_attempts=2
```

两个 generation 均实际走过 echo executor；现有 `test_hard_crash_after_retry_insert_recovers_after_restart` 只把 crash-window child 留在 queued 后由 boot 置 failed，未覆盖 child 已终态的另一侧窗口。

### 建议

除上一项的 durable source→retry 绑定外，增加跨终态仍成立的 source-generation 唯一约束（例如 child 上 `revived_from_task_id UNIQUE`），reclaim 时先按绑定 id 对账，不得只查询 live lineage。仅靠 active partial index 无法证明“该 source 尚未生成过 retry”。

## [P1] app/institute/scheduler.py:465-483；app/router/executor.py:414-435 — born-terminal `overcommitted` child 仍永久消费 source，临时队列压力会零模型调用丢任务

### 问题

`executor.spawn()` 在 hand queue 超 cap 时返回一个 terminal `overcommitted` task id，并不创建执行 coroutine。`respawn_from_row()` 把这个正常返回当成功，scheduler 不检查 child 状态就写永久 marker。

结果是临时队列压力永久排除 source；child 不是 `failed`，现有手工 retry API 也只接受 failed task，自动 revival 又只扫 rate_limited source，lineage 无后续恢复入口。

### 复现

插入 9 个 echo queued task（默认 cap 8），再触发一个 cooled source：

```text
child.status = "overcommitted"
child.error  = "hand 'echo' already has 9 queued tasks (cap 8)"
source marker = true
model calls = 0
```

### 建议

revival 不应把 born-terminal admission rejection 当作成功 generation。让 respawn 返回/查询 child 状态；遇到 `overcommitted` 时在本 lease 下释放或延后 source（仍计 bounded attempt 并记录原因），不要写永久 consumed marker。更稳妥的是由 durable revival outbox 等待容量后再驱动已绑定 generation。

## [P1] app/institute/mailbox.py:225-268 — dispatch 先变 done、后插 reply/事件，普通 DB 失败即可静默丢回复

### 问题

lease winner 先执行 `pending → done`，之后才分别：

1. INSERT reply；
2. UPDATE thread timestamp；
3. INSERT `mailbox.reply` event。

这些不是同一事务。reply INSERT 抛普通 `Exception` 时，外层补偿只执行 `UPDATE ... SET failed WHERE status='pending'`，但 row 已是 done，补偿必然 no-op。sweep 也只扫描 pending，因此数据库永久保留“done + task_id”，却没有 reply 和事件。

lease 条件只保护了终态 flip；reply 行既无 `dispatch_id` 唯一关联，也没有与终态同事务，不能满足“终态/reply 一起由 lease owner 落地”。

### 复现

让 fake submit 返回 completed task，并只在 reply INSERT 注入 `sqlite3.OperationalError`：

```text
dispatch.status = "done"
dispatch.task_id = "r5-task-done"
reply rows = 0
mailbox.reply events = 0
```

无需 SIGKILL 即可复现。

### 建议

在一个 `db.transaction()` 中完成 lease-conditioned `pending → done`、带 `dispatch_id` 的 reply INSERT（加唯一约束）和 thread timestamp；事务失败则 dispatch 仍 pending/可重试。事件至少通过 durable outbox 与该事务一起落地，再由幂等 drainer 发出，避免 reply 已提交但事件永久缺失。

## [P1] app/institute/mailbox.py:38-43, 221-227, 319-324；app/router/executor.py:283-296；app/config.py:43 — 45 分钟 TTL 不覆盖合法 queue wait，活 worker 会被当 stale 并重复提交模型

### 问题

lease 在调用 `executor.submit()` 前开始，但 `task_id` 要等 submit **终态返回后**才绑定。executor 的 `timeout_s + 30` 只包住拿到 hand mutex/global semaphore 后的 `hand.execute()`；等待同 hand mutex 的时间没有上界。`default_timeout_s` 还是可配置值，并不保证小于 45 分钟。

因此正常 backlog 下，活 worker 可等待超过 45 分钟；其 dispatch 此时 `task_id IS NULL`，sweep 的 live-task 子查询无从识别，第二进程会 reclaim 并启动第二次模型调用。lease fencing只能丢弃旧 worker 的晚到写，无法撤销已花掉的两次调用。

### 复现

让 worker 1 停在 submit 内，老化 lease，并用独立进程语义（独立 `_inflight`）运行 worker 2：

```text
submit calls = 2
dispatch.task_id = worker-2 task
reply = worker-2 reply
worker-1 result fenced out
```

这也正是现有 late-worker 测试的交错；该测试只断言最终 task_id/reply 一致，没有断言模型只提交一次。

### 建议

不要用固定 TTL 猜测 executor 的 queue+run 总时长。优先在 claim 事务中创建并绑定 durable task/outbox id，再由单一 consumer 驱动；至少应在等待和执行期间按 lease id 心跳续租，并让 sweep 对已绑定 live task fail closed。TTL 必须基于可证明的最大 deadline，而不是当前默认配置值。

## [P1] app/institute/mailbox.py:171-175, 306-350；migrations/0040_mailbox_dispatch_lease.sql:9-16 — dispatch 无 attempts 上界，反复崩溃可无限 stale-reclaim、无限烧配额

### 问题

0040 只有 `lease_id/leased_at`，没有 attempts。每次 stale claim 不增加持久计数，也没有达到 N 次后的 terminal transition。若进程反复在模型提交期间硬崩溃，row 永远保持 pending，TTL 后无限重投。

普通 `Exception` 会把 row 置 failed，但 SIGKILL、掉电、`BaseException` 正是 lease 要处理的路径；这些路径目前没有 hard rule 11 要求的上界。

### 复现

让 submit 每次抛硬崩溃替身，逐次老化 lease，连续驱动 7 次：

```text
submit calls = 7
dispatch.status = "pending"
lease remains stale/reclaimable
mailbox_messages has no attempts column
```

继续循环没有协议上限。

### 建议

新增 `dispatch_attempts INTEGER NOT NULL DEFAULT 0`，在 lease claim 同一 UPDATE 中原子递增；每次 terminal write 带 lease id；达到上限后条件转成 failed/quarantined 并保留最后错误。若一次 attempt 对应 durable task，则同时持久绑定 task id，避免“是否已提交模型”只能由超时猜测。

## [P2] app/router/executor.py:145-165, 423-435；app/institute/scheduler.py:472-479 — task 行已提交但 `task.queued` 事件失败时，当前进程留下无人驱动的 live child

### 问题

`_create_row()` 先 autocommit tasks INSERT，再调用 `bus.emit("task.queued")`；`spawn()` 只有 emit 返回后才 `create_task(_execute(...))`。emit/create_task 之间失败时，scheduler 的普通 Exception 分支断言“Nothing durable was created”并释放 source lease，实际却已有 queued child。

后续 tick 的 live-lineage `NOT EXISTS` 会排除 source，而当前进程没有 task poller，child 永远 queued；只能等一次进程重启把 child 改 failed 后才恢复。

### 复现

仅让 `task.queued` emit 抛一次：

```text
after failure: child.status="queued", source.lease=NULL, source marker=false
executor._running = {}
after next revival tick: 状态完全不变
```

### 建议

让 task row 创建、source 绑定与 durable dispatch/outbox 成为一个协议；当前进程必须能重新认领已存在的 queued child。短期至少在 Exception 分支查询本 lease/source 是否已经产生 live child：存在则驱动/结算它，不能释放 lease 后假装没有 durable footprint。

## [P2] app/institute/scheduler.py:465-471 — 任意 `sqlite3.IntegrityError` 都被误当 live-lineage race，可在零 retry 时写永久 marker

### 问题

catch 覆盖整个 `respawn_from_row()`，却不验证 `uq_tasks_lineage_active` 是否真的有 winner。task id 碰撞、其他 constraint/schema 异常同样是 `sqlite3.IntegrityError`；当前代码直接 `_mark_revival_consumed()`。

### 复现

让 respawn 抛一个无关 CHECK IntegrityError：

```text
retry rows = 0
source.revival_attempts = 1
source error gains permanent [rate-limit-revival:claimed]
```

### 建议

捕获后查询该 lineage 的实际 queued/running winner；只有 winner 存在且字段与预期 retry policy 对得上才消费 source。否则按普通失败路径释放本 lease、保留原 error 并受 attempts 上界约束。

## [P2] app/institute/mailbox.py:162-165, 347-350 — `_inflight` 会否决已 stale 的 durable lease，挂住的本进程 coroutine 可无限阻止恢复

### 问题

注释称 `_inflight` 只作优化，但 sweep 在 SQL 已证明 lease stale/no lease 后，仍因进程内 set 成员直接跳过。若 coroutine 活着但永久挂住，id 会一直留在 `_inflight`，数据库 TTL 永远无法在该进程触发 recovery；这已影响持久 liveness，不只是减少重复 spawn。

### 复现

构造 pending + 2000 年 stale lease，并把 id 放进 `_inflight`，连续三次 sweep：

```text
spawned = 0
dispatch remains pending with stale lease
```

重启会因 set 清空而恢复，但一个长期运行的挂起进程不会自行收敛。

### 建议

fresh lease 已由 SQL 过滤，无需 `_inflight` 再覆盖 stale 判定。让活 worker 心跳续租；一旦 DB 判 stale，就以 lease CAS 为唯一仲裁。`_inflight` 可保留在 `_run_dispatch` 的同进程瞬时去重入口，但不能阻断 stale recovery。

## [P2] app/institute/operator.py:1457-1468, 1583-1643, 1740-1765 — parameter history 的幂等收敛不会补齐 effect，commit 后崩溃可永久丢测量审计

### 问题

admin_state + parameter_history 在事务中正确原子提交，但 `_open_effect()` 在事务后执行。若在两者之间崩溃：

- `set_parameter()` 重放在事务内看到 prior 后直接 return；
- `approve_proposal()` 重放更早就看到 history，完全不再调用 `set_parameter()`；
- IntegrityError loser 也直接返回 winner。

因此 unique index/历史行收敛正确，但“每次 change freezes effect baseline”的伴随审计不会自愈，proposal 仍可被置 `applied=1`。

### 复现

让 `_open_effect` 在 parameter commit 后模拟硬崩溃，再以同 proposal 重放：

```text
parameter_history rows = 1
replay returns same history_id
operator_effects rows for proposal = 0
```

### 建议

把 effect/outbox 与 parameter_history 同事务预建，并保存应用时刻/基线所需的 durable 数据；重放必须对齐 history 与 effect 两个不变量后才能把 proposal 置 applied。仅在 prior/winner 分支晚补当前时刻 baseline 会改变测量口径，最多只能作为带明确 late/backfill 标记的降级方案。

## [P3] app/institute/operator.py:365-375, 515-533 — writer TOCTOU 仍可开假卡，但确认不改 vault/domain 状态且可恢复

### 问题

已记档窗口仍存在：fresh ledger 重读、磁盘分类、`open_action()` 间没有 writer generation/锁。writer 在 `os.replace` 后暂停超过 120 秒，sweep 可按旧 ledger 对新文件开出假 `vault_conflict` 卡。

### 复现

沿 R4 探针顺序：ledger=H1，磁盘 replace=H2，暂停超过 grace，sweep 开卡，随后 writer upsert ledger=H2。最终 vault_index 与文件一致，只多一张 live action。

### 风险确认与建议

该路径不写 vault 文件、不修改权威 domain row，也不删除/覆盖 event；`open_action` 仅持久化一张按 ref 幂等、可 dismiss 的 operator action。因此确认是可恢复假卡，不是状态/事件静默丢失，维持 P3。根治仍是 writer 级临界区或 ledger generation。

## 已核查通过

### A. revival lease / cursor

- 0039 的 pre-respawn claim 是随机 lease + lease-conditioned marker；在 **尚无任何 retry footprint** 的硬崩溃路径上，15 分钟后可 stale reclaim。
- `revival_attempts` 在 claim 时原子递增，最多 5 次；soft failure 只按本 lease 清理 lease，不覆盖 source 原始 error。
- 在有限、可排空候选集下，`(finished_at-or-created_at, id)` keyset、cap-break cursor 与 tail wrap 仍能越过 cooling/unresolvable head；本轮没有发现由新 lease filter 单独造成的有限 backlog 永久漏扫。严重问题在 claim 后的 source↔retry 协议，而不是 keyset 比较式。

### B. mailbox lease 基本仲裁

- 模型提交前的 DB claim 对同一 message 是单赢家；task_id、done、failed 写均带 lease id，旧 worker 晚到不能覆盖 winner 的 task_id/终态。
- SQL 对 fresh lease / stale lease / `lease_id IS NULL` / `leased_at IS NULL` 四种物理形态的筛选一致；进程重启后 fresh lease 等 TTL，stale/无 lease 可进入重驱。
- 上述基本 CAS 正确，但 TTL 不覆盖真实执行生命周期、reply 不与终态同事务、attempt 无上界、`_inflight` 可覆盖 stale 判定，故整体协议仍不合格。

### C. 0037 / operator R4 修复

- 0037 的 DELETE 只匹配 `proposal_id IS NOT NULL`、`old_value = new_value` 且存在更早同 proposal 行。`old_value/new_value` 是默认 BINARY collation 的 TEXT：`"0.75"` vs `"0.750"`、`{"a":1}` vs `{"a": 1}` 均不相等；SQL `NULL = NULL` 为 NULL，也不会被删。
- 显式事务探针放入“真实 transition → no-op → 第二个真实 transition”：CREATE UNIQUE INDEX fail loud，ROLLBACK 后三行（包括先删的 no-op）全部恢复。`db.migrate()` 的 per-file `BEGIN/ROLLBACK` 因而满足整体回滚，不会留下部分清理状态。
- partial unique index、`set_parameter()` 事务内 prior lookup、byte-CAS 与 IntegrityError 后重读 winner 对“参数值 + parameter_history 恰一行”一致；伴随 effect 不收敛的问题已单列 P2。
- future mtime 使用 `-300s <= age < 120s` 有界窗口；一年未来 mtime 立即按 anomaly 处理，不再永久延期。
- poison path 已 per-candidate 隔离，cursor 在 finally 持久化；坏 ref 只增加 `errors`，后续正常候选不会被永久饿死。

## 验证

定向回归：

```text
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  tests/test_rate_limit_revival.py tests/test_mailbox.py \
  tests/test_operator.py tests/test_db_migrate.py \
  -q -p no:cacheprovider

112 passed in 13.24s
```

另做隔离故障注入：

- revival：marker 前/后崩溃、queued event 失败、overcommit、无关 IntegrityError；
- mailbox：reply INSERT 失败、活 worker lease 过期、七轮硬崩溃重投、stale + `_inflight`；
- operator：parameter commit 后 effect 崩溃；
- 0037：精确文本比较与整文件 rollback。

探针均使用临时 home/内存 SQLite 并清理，没有写仓库数据或代码。

## 判决

**REQUEST_CHANGES**

P1 共 6 项：revival 在 marker 两侧分别存在“永久断链/重复调用”，overcommit 会零调用消费 source；mailbox 会静默丢 reply/event、合法长执行可被重复提交、且 stale reclaim 无 attempt 上界。按约定不可合入。

## 2026-07-21 闭环附录

以上判决是修补前的时间点审计记录。本报告的 6 项 P1、4 项 P2、1 项 P3 现已全部闭合：revival 使用 reciprocal durable binding 与同 task-id recovery；mailbox 使用原子 booking/settlement、task-aware deadline 和持久化 attempt ceiling；durable event 可无重复 fan-out；parameter effect 与 history 原子提交；VaultWriter 与 operator 共享协调锁。逐项映射与验证证据见 `PATCH-NOTES-NORTHSTAR-R5-CLOSURE.md`。

当前工作区判决：**ACCEPT**（代码就绪；正式 roadmap 卡仍留在 `review` 等待 operator 验收）。
