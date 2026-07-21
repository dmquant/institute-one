# REVIEW-R3-QUEUE — 队列 / 调度 / 自改进簇合入前复核

## 结论

**REQUEST_CHANGES**

定向回归测试全绿，但复核发现 2 个 P1：P6 的“楼层只升”校验存在可复现 TOCTOU，
会覆盖并降低并发人工刚提高的 live floor；P11g 的固定头部 `LIMIT 50` 会被永久或
长时不可处理的旧行占满，使窗口后的可复活任务永远轮不到。另有 5 个 P2 和 1 个
P3。P1/P6、P11g 修复前不建议合入。

## Findings

### [P1] `app/institute/operator.py:1246-1269,1304-1349,1445-1463` — raise-only 校验与实际写入分离，可覆盖并降低并发人工值

**问题说明**

`_check_floor_raise_only()` 在 proposal 宣占及 `set_parameter()` 之前读取 live
floor；真正写入时，`set_parameter()` 又重新读取当前值并允许任意方向写入。若人工
PUT 恰好落在两者之间，proposal 会把人工刚提高的值降回自己的旧值。byte-CAS 只防
“读取后再变化”，不能防“方向校验后、读取前已变化”。

这直接违反 P6b 声明的 proposal 路径“楼层只升”，也会静默覆盖并发人工决策。

**复现思路**

1. live floor 为 `0.7`，创建目标值 `0.75` 的 proposal。
2. 在 `_check_floor_raise_only()` 成功返回后暂停 approve。
3. 人工路径调用 `set_parameter(..., 0.9)`。
4. 恢复 approve。

本次 `/tmp` 定向探针实际得到：

```text
{"proposal_value": 0.75, "human_value_before_apply": 0.9, "final_value": 0.75}
```

**建议修法**

把 raise-only 判定放进实际参数写事务：在持有 `db.transaction()` 写锁后读取 live
值、比较 `new > live`、写 `admin_state` 和 history。人工 API 继续使用
`raise_only=False`；proposal 路径使用 `raise_only=True`。外层预检可以保留用于
快速 409，但不能作为安全仲裁器。补一个精确卡在预检与 apply 之间的并发测试。

### [P1] `app/institute/scheduler.py:339-358` — `LIMIT 50` 固定扫最旧头部，永久跳过行会饿死后续 eligible 任务

**问题说明**

候选查询每次固定取最旧 50 行，然后在 Python 中跳过：

- `hand` 为空的行；
- hand 仍 cooling 的行；
- lineage 仍有 live retry 的行。

这些跳过行不带 claim、不更新时间、也不离开排序头部。尤其 `hand IS NULL` 并非
临时态：executor 在“没有可用 hand”时可直接生成 `rate_limited` 且 `hand=NULL`
的任务。只要前 50 行属于这种形状，第 51 行即使现在完全可复活，也永远不会被扫描。
长 cooldown 或长期 live lineage 也会造成同样的头阻塞。

**复现思路**

插入 50 条更早、每次都会被跳过的 `rate_limited` 行，再插入第 51 条 echo eligible
行，连续触发 revival。此次探针用 50 条持续 cooling 的头部行执行两次，结果为：

```text
{"respawned": [], "eligible_claimed": false}
```

换成 `hand=NULL` 后无需时间假设，饿死是永久的。

**建议修法**

至少：

1. 明确 `hand=NULL` 的恢复语义（通常应回退到 `requested_hand`），不要让其永久占窗；
2. 把 live-lineage `NOT EXISTS` 下推到候选 SQL；
3. 对仍需 Python 判定的 cooldown 使用持久 keyset cursor / round-robin 扫描并在尾部
   wrap，保证每个有界窗口最终被访问。

补“50 条永久不可处理头行 + 第 51 条 eligible”回归测试；现有测试只覆盖所有头行
都会 claim 并离窗的理想情形。

### [P2] `app/institute/operator.py:1311-1349,1432-1465` / `migrations/0026_operator_selfimprove.sql:105-116` — 并发 replay 可追加两条同 proposal 的参数历史

**问题说明**

两个 approve 都可进入 `approved AND applied=0` replay 窗口。两者可先后看到
`parameter_history WHERE proposal_id=?` 为空；第一个写 `0.7 -> 0.75` 后，第二个
重新读取当前 `0.75`，SQLite 对 `SET 0.75 WHERE value=0.75` 仍给成功 rowcount，
于是再追加一条 `0.75 -> 0.75` history。schema 对 `proposal_id` 没有唯一索引，
结尾的 `applied=1` UPDATE 也未检查 rowcount，因此两个请求都会返回成功。

这使 append-only audit 与“per-proposal 幂等”声明不成立，并让该 proposal 对应哪个
rollback history 变得不唯一。

**复现思路**

用 barrier 让两个 approve 都在 apply 前看到 history 不存在，再让 A 完成写入后才
让 B 调 `set_parameter()`。实际探针得到两个成功返回及两条 history：

```text
0.7  -> 0.75
0.75 -> 0.75
```

**建议修法**

在持有同一写事务后先按 `proposal_id` 查 history，已存在则直接复用；同时新增
`UNIQUE(parameter_history.proposal_id) WHERE proposal_id IS NOT NULL` 作为数据库
竞态后盾，并让唯一冲突重读赢家行后收敛。若 API 语义要求“一个赢家、一个 409”，
再增加 per-proposal 串行化；不能仅依赖 apply 结束后的 `applied=1`。

### [P2] `app/institute/operator.py:429-446` — `to_thread` 扫描与 Vault 写入无同步，可为本进程正常写入误开 drift 卡

**问题说明**

搬线程的函数本身不接触 asyncio 对象，局部内存也没有共享写；但它读取的是“先取到
的 ledger rows + 稍后读取的磁盘”。线程运行期间事件循环继续执行 VaultWriter：
文件先 `os.replace`，ledger 随后的 async upsert 尚未完成时，扫描会把“新文件 +
旧 hash”判成 drift，并立即开卡。旧同步扫描不会与同事件循环中的 writer 交错，
这是 P8a 引入的新竞态。

**复现思路**

1. sweep 读完旧 `vault_index` 后暂停 worker；
2. writer 完成文件原子替换，但在 `_upsert` 前暂停；
3. 放行 worker，再完成 ledger upsert。

实际探针最终 ledger 为 `clean`，但 sweep 已报告 `drifted=1` 并留下
`vault:Probe/note.md` open action。

**建议修法**

给“磁盘写 + ledger upsert”和“ledger snapshot + 磁盘分类”共享一个 writer 级 async
锁；sweep 持锁时仍可把纯文件扫描放到 thread。若不愿扩大 writer 临界区，至少在开卡
前用最新 ledger generation/hash 重验，但单次无锁重验仍有 TOCTOU，锁更可靠。

### [P2] `app/institute/mailbox.py:233-253` — 20 只封顶 spawn，候选读取与跳过扫描仍无界

**问题说明**

`SWEEP_REDRIVE_LIMIT` 确实限制了实际 `_spawn_bg` 数量；但 SQL 仍一次性读取全部
pending dispatch，没有 `LIMIT`。若前部大量行属于 `_inflight` 或仍有非终态
`task_id`，sweep 会先把全表物化到内存，并逐行执行 `executor.get_task()`，直到找到
20 个可重驱行或扫完整表。因而单 tick 的内存、DB round-trip 和运行时间仍是 O(N)，
没有实现整体有界。

**复现思路**

插入大批 pending dispatch，并让绝大多数关联 live task；spy `db.query` 的返回行数
和 `executor.get_task` 调用数。spawn 始终不超过 20，但候选取数/检查数可等于全部
积压。

**建议修法**

把 task terminal/missing 条件尽量下推到 SQL，再用稳定 keyset 分页；设置独立且有限
的 `SWEEP_SCAN_LIMIT`，配持久/轮转 cursor 防止被跳过头部饿死。测试同时断言
“spawn <= 20”和“本火读取/检查行数有上限”。

### [P2] `app/institute/operator.py:429-446` — P10 卡上限在头部反复关闭时会永久饿死尾部路径

**问题说明**

正常情况下，前一轮 live 卡 `created=False` 不消耗上限，测试证明了这种理想排干。
但上限没有公平游标，且 `vault_index` 查询没有稳定 `ORDER BY`。如果前 20 个仍漂移
路径的 action 被 done/dismissed（磁盘问题未修），下一轮会为同一批 ref 重开 20 张
并再次耗尽 cap；后面的路径可永久 `deferred`。因此“旧卡不饿死后续”只在旧卡保持
live 的附加假设下成立。

**复现思路**

准备 21 个 drift 路径、cap=20；每轮 sweep 后仅关闭前 20 张卡但不修改磁盘，再触发
sweep。第 21 个路径始终没有 action。

**建议修法**

使用稳定排序和持久 round-robin cursor；或优先选择从未开过 action 的 ref，再处理
已关闭但仍漂移的 ref。新增“头部卡反复关闭、尾部仍须最终被访问”的 liveness 测试。

### [P2] `app/institute/research_tree.py:639-655`（关联 `app/bus.py:72-77`, `app/vault/exporter.py:576-624`）— `announced_at` 只确认 event 落库，不确认唯一副作用消费者成功

**问题说明**

emit-before-mark 已保证 `tree.completed` 事件行至少一次：崩在 event INSERT 后、
marker 前会重发。正常并发也由 `_announce_lock` 收敛。

但 `bus.emit()` 会吞掉 handler 异常，research-tree exporter 自己也捕获异常后返回；
所以 Vault 写入失败时 `bus.emit()` 仍被视为成功，随后 `announced_at` 被置位，sweep
不再重发。事件 at-least-once 成立，Vault 投影的 at-least-once 不成立。重复成功调用
时 writer 是幂等的，但失败没有 durable consumer ack/retry。

**复现思路**

让 `_on_research_tree_completed` 的 `write_note` 首次抛异常：event 行存在且
`announced_at` 非空，后续 `_sweep_trees()` 不再 emit，目标 note 永久缺失。

**建议修法**

把“事件已发布”和“Vault 已投影”分成两个 durable ack。可给 exporter 建消费 cursor /
outbox drain，按 event id 重试并在成功后 ack；不要用 producer 的 `announced_at`
代替 consumer ack。若本卡只承诺 event at-least-once，应同步收窄 PATCH-NOTES 中对
Vault 崩溃收敛的表述并另开修复卡。

### [P3] `app/institute/scheduler.py:426` — 仍有直接 `datetime.now(timezone.utc)`

**问题说明**

目标文件仍直接调用 `datetime.now(timezone.utc)` 生成 janitor 的多个 cutoff。它是
timezone-aware，不会产生当前时区错误，但违反仓库统一时间源规则，也让测试只
monkeypatch `bus.now_iso()` 时出现两套“现在”。该行不是 P9 新增 hunk，但属于本次
明确要求核查的范围。

**复现思路**

全文件搜索 `datetime.now(` 即可定位；将 bus 时钟固定后，janitor cutoff 仍走真实
墙钟。

**建议修法**

改为 `datetime.fromisoformat(bus.now_iso())`，SGT 日期继续使用
`now_sgt()/work_date()`。

## 已确认成立

- **P1 锁序**：生产代码只有 `executor._execute()` 同时取得 hand mutex 与 global
  semaphore，顺序为 hand → semaphore；仓内没有反向生产获取点，未发现 ABBA。
  queued cancel 先翻 DB 再 cancel asyncio task，停在任一 await 都会释放已持有锁；
  rate-limit 递归发生在 `async with` 退出后。
- **P2 占位 disposition**：两条失败路径均调用 `_record_route_failure()`；0022 唯一
  索引及候选 `NOT EXISTS` 会堵死同 loop 重选。NULL confidence 无法通过 approve，
  `unparsed` 不能提炼 recipe，`human_pinned` 得以保留。
- **P5 终态 UPDATE**：`_maybe_finish_tree()` 的翻转语句带 live-node `NOT EXISTS`
  且检查 rowcount，能让 retry 与 finish 由 SQLite 仲裁。event 行的 crash 语义为
  at-least-once，进程内正常并发由 `_announce_lock` 单发；端到端投影限制见 finding。
- **P9 备份**：`VACUUM INTO` 成功返回后才在同目录 `Path.replace`；进程崩在改名前
  只留 tmp，下次先清，崩在改名后 target 是完整 SQLite 快照。未发现撕裂 target
  窗口；`target.exists()` 的 once-per-date 守卫只认最终名。
- **P10b 新鲜度**：cutoff 使用 SGT `work_date()`，ISO date 比较正确；最新快照过期
  后 subject 整体掉队。
- **条件宣占**：本批新增的 P2/P5/revival 关键仲裁均检查 rowcount 或由唯一索引收敛；
  P6 `applied=1` 的未检查结果及其并发后果已列为 finding。
- **P8a worker 内容**：线程函数不碰 asyncio/DB，也不修改共享 Python 容器；问题是
  它与事件循环上的外部磁盘/ledger 写入缺少快照同步。

## 验证记录

- 指定命令：

```text
.venv/bin/python -m pytest tests/test_executor.py tests/test_operator.py \
  tests/test_scheduler_backup.py tests/test_rate_limit_revival.py \
  tests/test_mailbox.py tests/test_research_tree.py -q
133 passed in 15.59s
```

- scoped `git diff --check`：通过。
- 临时并发/饿死探针仅放 `/tmp`，执行后已删除；未修改仓库代码。
- 合并工作树的 aggregate diff 不是单一补丁：上述 P-hunk 均位于声明的生产文件及
  对应测试/notes（P5 另有已披露的 `tests/conftest.py` lock rebind），但同一文件还
  混有 NORTHSTAR/M8/其他卡片的未提交 hunk，不能从当前 aggregate diff 证明每个
  agent 的独立写集。

## 最终判决

**REQUEST_CHANGES**
