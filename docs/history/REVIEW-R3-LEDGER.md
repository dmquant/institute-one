# R3 合入前复核：attempts / lease / PIT 簇

日期：2026-07-20
范围：P3、P4、P7、P8b、P8c、P10c/d/e、P11b/c/d/e/f，以及 0035/0036 迁移。
结论：发现 2 个 P1、3 个 P2；存在 P1，不能合入。

## Findings

### [P1] stale verifying 回收不消耗卡片 attempts，进程崩溃可无限重试 — `app/institute/factcheck.py:754-768, 1492-1500`

**问题说明**

`_claim_card()` 只写 `status/verify_started_at/lease_id`，卡片的 `attempts` 要等模型任务返回失败或可捕获异常后，才由 `_record_failed_attempt()` 增加。若进程在每日额度已预占、模型已启动或已返回之后硬崩溃，异常处理不会运行；60 分钟后 `_recover_stale_running()` 又把卡片直接放回 `pending`，但不增加 `attempts`。同一毒卡可跨重启反复执行，永远达不到 `VERIFY_MAX_ATTEMPTS=3`。每日 cap 只限制单日消耗，不能形成卡片级总上界。

正常 task-failed/可捕获异常路径的计数与终态事务是正确的；缺口是 catch 不到的进程崩溃窗口，现有测试只人工构造了“pending 且 attempts 已写满”，没有覆盖“verifying 已花额度但 attempts 尚未写”的窗口。

**复现思路**

循环执行五次：`_claim_card()` → 把 `verify_started_at` 置为 stale（模拟 worker 在模型调用后死亡）→ `_recover_stale_running()`。实测五轮后仍为：

`{"status": "pending", "attempts": 0}`

在真实环境中可在 `executor.submit()` 已开始后、`_record_failed_attempt()` 前杀进程；重启并等 stale sweep 后重复，模型调用数可超过 3。

**建议修法**

每日 slot 预占成功后、调用模型前，在当前 lease 下原子预记一次卡片 attempt，并检查 rowcount；失败路径只负责按已预记次数 release/terminalize。stale sweep 必须保留该已花 attempt，若已达上限则事务内落 `unverifiable + UNVERIFIABLE verdict`，否则才放回 `pending`。这样每日额度耗尽发生在预记之前，仍不会误计卡片 attempts。

### [P1] P4 在第 N 次失败后的崩溃会先重跑模型再跳过，调用上界不成立 — `app/institute/chain.py:1811-1817, 1829-1855`

**问题说明**

持久化失败计数只在 extraction 已执行且 persistence 再次失败后读取/增加。第 3 次失败已把 durable counter 写成 3，并可能已开 operator card，但若在 cursor CAS 前崩溃，下一 tick 不会先检查“该事件已耗尽”；它会再次执行 extraction，随后把计数写成 4，才开卡/推进 cursor。反复在该窗口崩溃可造成无上界模型调用，与补丁声明的“总调用恰为 3”不符。

此外 `_note_persist_failure()` 是读后覆盖而非原子增量；在共享 DB 的多进程情形会丢计数。单进程部署降低了此竞态概率，但不能修复上述重启窗口。

**复现思路**

让 `_stage_properties()` 恒定失败。前两轮正常停住；第 3 轮在 card 已创建后令 `_advance_cursor()` 抛异常模拟崩溃；恢复后再跑一轮。实测 extraction 被调用 4 次，operator action 仍只有 1 张，随后 cursor 才推进。

**建议修法**

处理每个事件前先读取其 durable failure state；若 count 已达到上限，直接幂等确保 action 存在并 CAS 推进 cursor，禁止再次 extraction。最好把计数更新改成原子 CAS/事务，并显式记录 `drop_pending`。补 fault-injection 测试覆盖“计数写后崩溃”和“开卡后、cursor 前崩溃”，两者都应保持模型调用数等于 N。

### [P2] `_auto_cluster` 只限制 candidates，pending×nodes 的 nodes 侧仍无界 — `app/institute/chain.py:1603-1627`

**问题说明**

`CLUSTER_SCAN_BATCH=200` 确实限制了 pending candidates，但随后仍 `SELECT` 全部 `chain_nodes`、展开全部 aliases，并在事件循环中执行 `最多 200 × 全部 surface terms` 的同步嵌套匹配。节点由 promotion 持续增长，故每 tick 的 CPU/内存仍随全图规模无界增长；P8b 所称的 pending×terms 工作“已封顶”并不成立。candidate 老化不会减少既有 nodes。

**复现思路**

保留 1 个无法匹配的 pending candidate，逐批插入大量 nodes/aliases；给 `_norm_term` 加计数器或测量 `_auto_cluster()`，比较次数/耗时会随 node 数线性增长，candidate LIMIT 不改变结果。

**建议修法**

为规范化 name/alias 建可索引的 surface 投影，精确匹配走索引；containment 匹配需要总 comparison budget 加持久化游标/中间状态，跨 tick 扫描后再做唯一匹配判定。仅给 nodes 再加无状态 LIMIT 会漏掉歧义节点，不能直接采用。

### [P2] opener 固定重选前 50 条，长期不可定价头部会饿死后续可开仓 forecast — `app/institute/paper_book.py:289-309`

**问题说明**

候选查询每次都从 `(made_at, id)` 最前端取 50 条；`skipped_no_price` 不写 backoff/cursor。只要最老 50 条长期没有 bar，每个 5 分钟 tick 都重选同一批，后面的可定价 forecast 永远不会被考虑。补丁说明称“到期过滤保证不会永久饿死”，但被阻塞的短 horizon forecast 可能早于这些长 horizon blocker 到期，最终直接过期，形成相对旧实现的漏开仓回归。

**复现思路**

把 batch 设为 1：先建一个无 bar、长 horizon 的老 forecast，再建一个有 bar、短 horizon 的新 forecast。重复运行 opener 始终只得到 `skipped_no_price=1`；推进时间越过新 forecast 的 expiry 后，新行从候选集消失且从未开仓。

**建议修法**

在保持每轮最多 50 条的前提下使用持久化 keyset cursor（`made_at,id`）轮转并在尾部 wrap，或给不可定价候选写有界 backoff/`next_attempt_at`；补“永久 blocker 后面的短期可定价行仍在若干 tick 内被考虑”的回归测试。

### [P2] benchmark 首挂冲突 loser 未重读实际 base，会写出不一致 NAV — `app/institute/paper_book.py:462-473`

**问题说明**

两个调用都可先读到 base 不存在；其中一个 `INSERT ... DO NOTHING` 获胜，另一个冲突后仍无条件返回 `1.0`。若两次调用对应不同 work date/mark，loser 的返回值并非按实际已固定 base 归一化，可能把错误 benchmark NAV 写入自己的 `nav_history`。代码注释声称并发首挂安全，但只保护了 base 不被覆盖，没有保护 loser 的计算结果。

**复现思路**

用 barrier 让 `wd=2026-01-01/value=4000` 与 `wd=2026-01-02/value=5000` 两次调用都先读到 row absent，再并发插入。实测两者均返回 `1.0`，最终存储 base 为 5000；第一条正确值应为 `0.8`。

**建议修法**

检查 INSERT rowcount：只有真正插入者返回 `1.0`；冲突 loser 必须重读、校验实际 base，再计算 `value/base`（若新行损坏则沿用 fail-closed）。补不同 work date 的并发首挂测试。

## 已核实通过的重点

- P3 正常 task-failed/可捕获异常路径：失败计数与 release/终态均带 lease 条件；第 3 次在同一事务内写 `unverifiable` 与 verdict。每日预算耗尽走 `_release_card()`，不增加 attempts；picker 的 `attempts ASC, created_at ASC` 可保护 fresh cards 不被毒卡抢占。
- `UNVERIFIABLE` 不进入 reuse gate/claim_check：三个 actionable 查询均只取 `VERIFIED/DISPUTED`；卡片也退出 pending 轮换，不产生 dispute/outbox。
- P7 lease 三件套完整：claim 写随机 lease；done 与源缺失/task 失败/异常三条 failed 路径都带 `status='running' AND lease_id=?`；stale 回收清 lease。60 分钟阈值高于默认模型超时 1800 秒加 executor 30 秒外层余量，合理。
- P8c `get_last_bar_pit()` 与原 `get_bars_pit(...)[-1]` 的 per-bar-date `MAX(as_known_at) <= as_of`、`bar_date <= end` 和 bare-date 边界一致；未发现反前视或边界 bar 漏失。
- P11d 全仓精确搜索未发现 `paper_book.opened` 的业务消费方；SSE 仅通用展示，exporter 只订阅 `paper_book.marked`，memory 只消费 `paper_book.closed`，MCP 无 opened 依赖。payload 保留 `forecast_id` 并新增 `position_id`。
- P10c CAS miss 重读、P10d 顶层异常交给 scheduler `@metered`、P10e 两个向量 Python 扫描的 2000 行截断均已落地。
- P11b promotion 查询 `LIMIT 20`、P11c 二进制读取 `512 KiB` 钳制、P11f 每笔 `bus.now_iso()` 均属实。审查范围未新增裸 `datetime.now()`；`market_data._now_known_iso()` 的微秒 UTC 时钟为既有 PIT version-key 特例，本轮未改。
- 0035/0036 均为单条 additive `ALTER TABLE ... ADD COLUMN`；存量行分别安全回填 `attempts=0` 与 nullable `lease_id`，与 0034 列名无冲突，且无 BEGIN/COMMIT/ROLLBACK/ATTACH/VACUUM/PRAGMA。

## 验证记录

- 定向命令：`.venv/bin/python -m pytest tests/test_chain.py tests/test_factcheck.py tests/test_paper_book.py tests/test_market_data.py tests/test_forecasts.py tests/test_db_migrate.py -q`
- 结果：`264 passed in 27.50s`
- `git diff --check`（审查代码及对应测试）：通过。
- 三个仓库外临时 DB 场景分别复现了 P3 stale attempts 不增长、P4 第 4 次 extraction，以及 benchmark loser 返回错误的 `1.0`；未在仓库留下临时文件。
- 工作区相对 HEAD 还包含其他并行包的大量改动；本报告只判定用户列出的 hunks、对应测试及 0035/0036，不把其他包纳入本轮结论。

## 判决

REQUEST_CHANGES
