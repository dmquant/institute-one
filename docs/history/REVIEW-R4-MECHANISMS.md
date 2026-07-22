# R4 并发/可靠性机制合入前复核

日期：2026-07-21
范围：factcheck attempt 预记、五个持久游标、chain 预算轮转、operator raise-only CAS / 0037、Vault 冲突开卡
结论：**REQUEST_CHANGES**

发现分布：P0 0 / P1 6 / P2 4 / P3 2，共 12 项。以下 P1 均可导致静默错误合并、任务永久丢失、重复模型调用、错误事实继续复用或不可逆审计记录丢失，合入前应修复。

## [P1] app/institute/chain.py:1688-1735 — parked rotation 没有绑定图世代，删节点后 promotion 可复用 rowid 并被漏扫，最终错误合并

### 问题

`node_cursor` 只保存最后扫过的 SQLite `rowid`，`matches` 则跨 tick 直接作为唯一性证据继续使用。它们没有绑定 chain graph 的 generation/version。恢复时查询仅使用 `WHERE rowid > ?`，扫描结束后只要持久状态里恰有一个 match，就直接调用 `_merge_candidate_into_node`。

`chain_nodes` 的 `rowid` 不是 `AUTOINCREMENT`。删除当前最大 rowid 后，新 promotion 的插入可复用同一个 rowid；若它恰好发生在 parked cursor 处，新节点不满足 `rowid > cursor`，本轮永远看不到它。已扫描节点的 name/aliases 被修改也有同样问题：旧扫描证据不会失效。

`_merge_candidate_into_node` 的条件宣占只能防止同一 candidate 被重复消费，不能证明此前的“唯一 match”在当前图上仍唯一。因此注释所说“FULL node rotation”只对静态图成立。

### 复现

临时库中：

1. 插入 rowid=1 的 `n1=宁德时代`（匹配 candidate）和 rowid=2 的 `n2=普通企业`。
2. candidate 为 `宁德时代股份有限公司`，设置 `CLUSTER_COMPARE_BUDGET=3`。
3. 首轮停在 `node_cursor=2, matches=["n1"]`。
4. 删除 rowid=2 的 n2，再 promotion 一个精确节点 `n3=宁德时代股份有限公司`；SQLite 将 rowid=2 复用给 n3。
5. 第二轮查询 `rowid > 2` 为空，于是把 candidate 合并进 n1，尽管当前图已有精确匹配 n3。

实际结果：

```text
parked node_cursor=2, matches=["n1"]
deleted_rowid=2
new_promotion_rowid=2
candidate.status="merged", merged_into="n1"
```

另一个探针在首轮扫过 n1 后给 n1 新增匹配 alias，第二轮也会忽略这个已扫节点，并依据后段唯一 match 错误合并。

### 建议修法

为 chain_nodes 的任何插入、删除、name/aliases 变更维护单调 `graph_generation`；rotation state 保存 generation，恢复时不一致就清空该 candidate 的 `node_cursor/matches` 并从头重扫。节点游标也应改用不复用的单调键，但仅替换 rowid 仍不能解决 alias/name 变更。最终 merge 前必须确认扫描证据仍属于当前 generation。

`matches >= 2` 的短路本身在图世代稳定时是正确的，因为决策只区分 0、1、至少 2；缺陷是世代不稳定，而不是上限 2。

## [P2] app/institute/chain.py:1700-1729 — budget 在单节点 aliases 中耗尽时不保存 term 进度，可永久活锁并饿死整个 rotation

### 问题

一个 node 的 terms 从头排序后逐项比较。若 budget 在 node 中途耗尽，代码故意不推进 `node_cursor`，但也没有保存 `term_cursor`。下一 tick 会从这个 node 的第一个 term 重新开始。只要该 node 的未命中 terms 数量大于每 tick budget，就永远无法完成该 node；同一 candidate 一直占住 rotation，后续节点和 candidate 全部饿死。

### 复现

构造一个含 30 个不匹配 aliases 的 node，把 `CLUSTER_COMPARE_BUDGET` 设为 4，连续执行四轮 `_auto_cluster()`。四轮持久状态完全相同，`node_cursor` 始终为 0，candidate 始终 pending。

默认 budget 较大只提高触发阈值，不改变算法上的活锁：aliases 是无 schema 上限的合法 JSON 数组。

### 建议修法

rotation state 同时持久化当前 node 的稳定标识和 term offset（或稳定 term key）；下一 tick 从未比较项继续。term 集变化时结合上一项的 graph generation 使整个 candidate 扫描失效重来。另应对单节点 aliases 数设置合理上限，避免合法但异常的数据垄断 sweep。

## [P2] app/institute/chain.py:1593-1610, 1661-1666 — cursor 只校验外壳，不校验字段类型；损坏状态不会 fail-open，而会永久崩溃或污染 merge 证据

### 问题

`_load_cluster_rotation` 验证顶层是 dict，并强转两个整数，但 `candidate_id` 可为任意 truthy JSON 值，`matches` 也只是 `str()` 后截断。若 `candidate_id` 是对象，下一步把 Python dict 绑定到 SQLite 参数，抛 `sqlite3.ProgrammingError`。异常发生后 cursor 没有删除，后续每轮重复崩溃。

更危险的是语法合法、语义损坏的 `matches/node_cursor` 会被当成已完成扫描证据；伪造一个高 cursor 和一个 unrelated match 可触发错误合并。此时该 cursor 已不是注释所称的纯 advisory 状态。

### 复现

向 `admin_state` 写入：

```json
{"cand_cursor":0,"candidate_id":{"bad":"type"},"node_cursor":0,"matches":[]}
```

连续两次 `_auto_cluster()` 都抛 `ProgrammingError`，admin_state 原值不变，未自愈。

### 建议修法

严格验证完整 schema：`candidate_id` 只能是 null/string，matches 只能是 string list，数值需有合理范围；无效时删除该 key 并从头扫描。恢复已有 match 前还应验证 candidate/node 存在，并通过 graph generation 防止语义陈旧状态被用于 merge。

## [P1] app/institute/factcheck.py:784-799, 915-943, 1522-1545 — prebook 与 durable model task 创建之间仍有崩溃窗，可在零模型调用下耗尽 retries 并终态化

### 问题

`_prebook_card_attempt` 先独立提交 `attempts=attempts+1`，随后 Python 才进入 `_verify_card`，而 durable task 的创建发生在其中的 `executor.submit`。在 prebook 返回后、executor 创建 task 前硬崩溃，card 已计一次 attempt，但根本没有模型任务。stale sweep 会保留该计数；连续三次同位置崩溃会把 card 置为 `unverifiable`。

这关闭了“模型已启动后崩溃却不计数”的旧窗口，却在它前面引入了“模型未启动也计数”的对称窗口，因此不能满足“一次实际 attempt 恰好计一次”。

### 复现

连续三轮只执行 claim → reserve → prebook，然后模拟进程死亡（不调用 `_verify_card`），把 lease 调旧并运行 `_recover_stale_running()`。

实际状态依次为：

```text
pending attempts=1
pending attempts=2
unverifiable attempts=3
```

`tasks WHERE source='factcheck'` 的数量为 0，但生成了 `UNVERIFIABLE` verdict。

正常 success、task failure 和普通 Exception 路径目前都只计一次；budget exhausted 与 prebook lost-lease 对 card 计数为 0。缺陷专属于 prebook 成功、durable task 尚未建立的硬崩溃窗。

### 建议修法

不要简单把计数移回模型返回后，那会复活原来的无限重试问题。应让“建立可恢复的 queued task”与“card attempts+1 / 绑定 verify_task_id”在同一 SQLite 事务中完成，再由 executor 条件宣占 queued task 执行。崩溃后已有 durable task 可恢复或明确结算，不能只凭 card prebook 猜测模型是否启动。

## [P3] app/institute/factcheck.py:730-751, 909-918 — reserve 与 prebook 之间崩溃会无模型调用地永久消耗当日 slot

### 问题

`_reserve_attempt` 和 `_prebook_card_attempt` 是两个独立事务。两者之间崩溃时，每日计数已加一，card attempts 仍为 0，模型也未启动。它不会错误消耗 card retry，但并非完全“无害”：slot 不退款，足够多的启动崩溃可让当日所有验证提前触顶。

该损失被 daily cap 限制，隔日恢复，因此定为 P3。

### 复现

执行 `_claim_card` 与 `_reserve_attempt` 后立即终止进程；stale recovery 只重开 card，不会回滚 `factcheck_attempts:<date>`。`attempts_today()` 增加 1，而 card attempts 与 tasks 数都不变。

### 建议修法

把 daily counter 的条件自增与 card prebook 放进同一事务，任一条件失败则整个事务不消费 slot。再结合上一项，把 durable task 建立纳入同一个可恢复协议。

## [P1] app/institute/factcheck.py:823-849, 1636-1647 — reset 后再次耗尽时 `INSERT OR IGNORE` 保留旧 VERIFIED verdict，状态虽 unverifiable 仍继续进入复用门

### 问题

`_settle_exhausted_card` 先把 card 改为 `unverifiable`，再 `INSERT OR IGNORE` 一个 UNVERIFIABLE verdict。注释明确说 IGNORE 是为了兼容 operator-reset 后已存在的 UNIQUE fact_card_id。

但若旧行是 VERIFIED/DISPUTED，IGNORE 会保留旧 verdict。`_verdict_rows` 与向量复用查询只按 `verified_facts.verdict/expires_at` 过滤，不检查 card 当前 status。因此数据库同时声称 card 为 unverifiable、fact 为 VERIFIED，旧结论仍会参与 claim-check/reuse。

### 复现

1. 建一个 status=verified、verdict=VERIFIED 的 card。
2. 模拟注释支持的 operator reset，将 card 重置为 pending。
3. 让 attempts 达上限并调用 `_settle_exhausted_card`。

实际结果：

```text
card_status="unverifiable"
stored_fact.verdict="VERIFIED"
still_in_reuse_rows=true
```

### 建议修法

定义正式、事务化的 reopen/reset 操作：清除或归档上一代 active verdict、重置 attempts，并增加 generation。settle 时必须保证 card status 与 active verdict 同代一致；短期至少使用 conflict update 把现有 active row 改为 UNVERIFIABLE，而不是 IGNORE。若历史必须保留，应把 verified_facts 改为版本表并另有唯一 active generation，不能依赖一行覆盖所有世代。

## [P1] app/institute/scheduler.py:387-444 — revival marker 在 retry task 创建前持久化；硬崩溃会把 source 永久排除且没有 stale reclaim

### 问题

revival 先把 `[rate-limit-revival:claimed]` 写入 source task.error，随后才调用 `executor.respawn_from_row`。普通 Exception 会尝试清 marker，但进程死亡、SIGKILL 或 BaseException 不会运行补偿。下一轮候选 SQL 明确排除带 marker 的行，因此该 source 永远不会再次 respawn，也没有 lease 时间或 stale sweep。

cursor 的 wrap/keyset 在这里无助于恢复：行在 cursor 查询前就被 marker 永久过滤。

### 复现

让 `respawn_from_row` 在 marker 写入后模拟硬崩溃；恢复后再运行 sweep。实际结果：

```text
marker_after_crash=true
second_sweep_respawns=0
error_unchanged=true
```

### 建议修法

把 source claim 与 retry task 行的插入放入同一个 SQLite 事务，由现有 live-lineage unique index 仲裁；执行器随后消费 durable queued retry。若暂时保留 lease 模式，至少增加随机 lease_id、leased_at、attempt 上限和 stale reclaim，且 terminal write 必须携带 lease。永久 marker 不能兼任崩溃安全的 claim。

## [P1] app/institute/mailbox.py:138-229, 265-309 — `_inflight` 只是进程内集合，跨实例可对同一 pending dispatch 提交两次模型任务并写出错配 task_id

### 问题

`_run_dispatch` 在模型提交前没有数据库状态宣占，message 在整个 `executor.submit` 期间仍是 pending。`_inflight` 只能阻止同一 Python 进程内重入；两个共享 SQLite 的实例会同时读到 pending 并各自提交模型任务。

结尾的 `UPDATE ... status='done' WHERE status='pending'` 只保证最多一个 reply 落库，不能撤销已经发生的另一个模型调用。更糟的是 task_id 在终态宣占前无条件更新，输掉 done claim 的 worker 可覆盖 task_id，造成 dispatch.task_id 与最终 reply 的实际任务不一致。

持久 sweep cursor 因而只是扫描位置，不能作为安全仲裁。

### 复现

两个独立 Python 进程共享临时 DB，同时调用同一 message 的 `_run_dispatch`；fake submit 用 DB barrier 保证两边都进入提交点。实际结果：

```text
submit_calls=2
dispatch.status="done"
dispatch.task_id="fake-worker-B"
reply.body="reply-worker-A"
```

即一个 reply、两个模型提交，且 task_id 与 reply worker 不一致。

### 建议修法

提交前做数据库 conditional claim（pending → running/dispatching），写随机 lease_id/started_at；只有 claim winner 可创建模型任务。task_id 应与 durable task 创建绑定，并只由 lease owner 写。sweep 只重开超过阈值的 stale lease，晚到 worker 的所有写都携带 lease_id。不要把进程内 `_inflight` 当成跨实例正确性机制。

## [P1] migrations/0037_parameter_history_proposal_unique.sql:26-35 — 建索引前的 DELETE 假设所有后续重复都是 no-op，会不可逆删除真实发生的参数变更

### 问题

迁移对每个 proposal_id 无条件保留 `MIN(id)`，删除其余行。注释假设后续重复一定是 no-op echo，但旧 replay 与人工改值可交错：同一 proposal 第二次应用可能确实把当前值从 X 改到目标值 Y。即使该 replay 本身是旧 bug，它仍是已经真实发生、必须留在审计链中的状态转移。

### 复现

迁移前历史：

```text
id=1 proposal=7  0.50 -> 0.75
id=2 proposal=NULL 0.75 -> 0.60   # 人工改值
id=3 proposal=7  0.60 -> 0.75     # replay 真正再次改变 live value
```

执行 0037 后 id=3 被删除，剩余历史最后状态看起来是 0.60，而 live value 实际可为 0.75。

partial unique index 语法在当前 SQLite 可用，DELETE 也确实在建索引前使 live migration 通过；问题正是它为了“通过”而静默丢失数据。

### 建议修法

迁移不得删除真实 audit rows。至少对历史重复逐项验证，只对可证明的严格 no-op 作显式处置；遇到 `old_value != new_value` 的后续重复应中止迁移并要求修复，而不是猜测。更稳妥的是保留所有 parameter_history，把“一次 proposal 只能有一个 canonical application”的唯一性放在独立 application 表；或为旧重复增加 canonical/duplicate lineage 字段后再建约束。由于这是尚未合入的新迁移，应在首次生产应用前修正，否则后续 additive migration 无法恢复已删除内容。

## [P2] app/institute/operator.py:432-438, 499-501 — freshness grace 对未来 mtime 没有下界，时钟漂移可把人工编辑延迟数月/数年而非“下一轮”

### 问题

判断是 `(now_ts - st_mtime) < 120`。未来 mtime 产生负 age，永远满足“小于 120”，直到本机时间追上文件时间再过 120 秒。来自同步工具、错误系统时钟或恢复备份的未来时间戳会让真实 drift 每轮都被 defer。

### 复现

把 drifted note 的 mtime 设为当前时间后一年。探针结果：

```text
deferred_now=true
age_seconds=-31536000
earliest_card_delay_days=365.0014
```

### 建议修法

计算显式 age，只把有界范围（例如 `-MAX_CLOCK_SKEW <= age < grace`）当作 fresh；超过允许未来漂移的 mtime 应记录 clock anomaly 并按非 fresh 处理，不能无限延期。

## [P2] app/institute/operator.py:165-181, 484-523, app/vault/writer.py:266-271 — 单个 poison path 的开卡异常会在保存 cursor 前终止整个 sweep，尾部候选永久饿死

### 问题

VaultWriter 的 `_resolve` 拒绝绝对路径和 `..`，但允许文件名中的 newline/control character。sweep 之后把 path 拼进 action ref；`open_action` 会拒绝 control character。异常由包住整个 sweep 的外层 try 捕获，此时 510 行的 cursor upsert 尚未执行。下一轮仍从同一个 poison path 开始，再次失败；`SWEEP_MAX_ATTEMPTS=100` 和 round-robin cursor 都无法推进，后续正常候选永久不开卡。

### 复现

通过正式 `write_note` 写入 `Reports/a\nbad.md` 与 `Reports/z-good.md`，人工修改并把 mtime 调旧。连续两轮 sweep 都返回：

```text
{"error":"control characters in action ref"}
cursor=null
actions=[]
good_starved=true
```

### 建议修法

VaultWriter 在入口统一拒绝 control/separator characters，保证 ledger path 可安全成为 ref；同时 sweep 必须按 candidate 隔离异常，并在 `finally`/事务化进度记录中推进到已尝试项。坏行应被 quarantine/记录为单独 operator error，不能阻断整个轮转。

## [P3] app/institute/operator.py:449-454, 491-508, app/vault/writer.py:329-330 — “fresh ledger 重读 + 120s grace”仍不是 writer upsert 的同步屏障，长暂停后可开出假冲突卡

### 问题

fresh ledger query、磁盘 hash/mtime 检查和 `open_action` 之间没有共享 generation、锁或条件写。writer 的顺序是先 `os.replace`，再 await ledger `_upsert`。若 writer 在这两步间因 DB write lock、进程调度或暂停超过 120 秒，sweep 会看到旧 ledger + 新磁盘；grace 已过，于是开卡。writer 随后 upsert 为 clean，但已创建的 action 不会自动撤回。

即使不足 120 秒，writer 也可在 491 行重读后、502 行 action insert 前完成 upsert；当前代码依赖 grace 降低概率，而不是消除 TOCTOU。

### 复现

1. ledger 保存旧 hash H1。
2. 模拟 writer 已 atomic replace 为 H2、尚未执行 `_upsert`，并让该窗口超过 120 秒。
3. sweep fresh-read 仍得到 H1，判 drifted 并开卡。
4. writer 完成 H2 ledger upsert；action 仍为 live。

### 建议修法

在一进程架构下，writer 与 sweep 至少应共享覆盖“磁盘替换 + ledger upsert / revalidate + action 决策”的锁。若要求跨实例，给 ledger 写入显式 generation/writing lease：writer 先持久宣告 generation，完成文件替换后结算；sweep 只对稳定 generation 开卡，并用 generation 条件插入。单纯再读一次仍有最后一条指令后的竞态。

## 已核查但未形成 finding 的点

- `set_parameter(..., raise_only=True)` 的方向判断和 byte-CAS 确实引用同一个 `old_raw`：读取在 `app/institute/operator.py:1545-1546`，方向判断在 1567-1577，CAS 在 1578-1587。并发写会使 CAS rowcount=0，未发现 stale floor 被降低的窗口。
- 0037 的 partial unique index 语法兼容当前 SQLite，且在重复行清理后能建立；finding 是清理策略的数据损失，不是 SQL 语法或执行顺序。
- `paper_book:opener_cursor` 缺失/损坏会回到 head；`made_at` 为 NOT NULL，`(made_at,id)` 两段 wrap 分区无 NULL 洞。cursor 是 advisory，position 的 unique/conditional insert 才是仲裁；多实例最多造成重扫或公平性抖动，未证明永久漏项。
- rate-limit revival 的 `(COALESCE(finished_at,created_at),id)` 中 created_at/id 均非空，keyset 与短窗口 wrap 本身成立；严重问题是 marker claim 的崩溃协议。
- mailbox sweep cursor 的 id keyset、cap-break 和 tail wrap 本身成立；严重问题是 cursor 后面的 dispatch 没有 durable claim。
- operator vault cursor 损坏/缺失会从 head 开始，普通多实例 last-writer-wins 主要改变公平顺序；已单列能导致永久饿死的 poison-item 和 future-mtime 路径。
- factcheck 的常规 success、task failure、普通 Exception、budget exhausted、lost lease 路径计数与 R3 声明一致；已单列两个仍未闭合的硬崩溃窗和 reset 后 verdict 不一致。

## 验证

执行定向套件：

```text
.venv/bin/python -m pytest \
  tests/test_chain.py tests/test_factcheck.py tests/test_paper_book.py \
  tests/test_operator.py tests/test_rate_limit_revival.py tests/test_mailbox.py \
  tests/test_scheduler_backup.py tests/test_db_migrate.py -q

317 passed in 35.31s
```

现有回归全绿，但没有覆盖上述 graph mutation/rowid reuse、单节点 term 活锁、prebook-before-task 硬崩溃、revival marker 硬崩溃、跨进程 mailbox claim、future mtime、poison path 和真实重复 history 迁移场景。所有故障注入均在 `/tmp` 临时环境执行并清理，未改仓库数据。

## 判决

**REQUEST_CHANGES**

P1 共 6 项；按约定不能合入。
