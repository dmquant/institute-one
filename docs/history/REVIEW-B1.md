# REVIEW-B1 — 第二轮独立审查 R-B1

审查日期：2026-07-20  
审查范围：仅 B1 指定分区；仓库中其他代理的未提交改动按要求忽略。  
结论：**FAIL**

当前正常路径和 43 个定向测试均通过，但 `db.migrate()` 的 COMMIT 失败不在回滚保护内，违反“迁移失败整文件回滚”的核心保证；另有 sweep 租约上界不足和 ADD COLUMN 恢复守卫过宽的问题。前者是本轮阻断项。

## 一、问题分级（附行号）

### B1-H1 / HIGH / 阻断：COMMIT 失败不会回滚，连接会残留活动事务

- 位置：`app/db.py:141-164`，尤其 `COMMIT` 位于 `try/except BaseException` 之外的 `app/db.py:164`。
- 语句执行和 ledger INSERT 在 `try` 内，但最终 `await c.execute("COMMIT")` 不在。真实的 `SQLITE_BUSY`、I/O/磁盘错误、延迟外键约束失败，或提交 await 的取消，都可能从这里抛出。
- 此时不会执行 `ROLLBACK`，也不会输出 `app/db.py:157-162` 的修复提示。连接仍可看到未提交的 schema 与 ledger；再次调用 `migrate()` 会在 `BEGIN` 处报 `cannot start a transaction within a transaction`。
- 隔离内存库注入一次 COMMIT 失败的实测结果：

```text
first_error=OperationalError:synthetic commit failure
in_transaction_after_error=True
visible_ledger_rows=1
retry_error=OperationalError:cannot start a transaction within a transaction
ledger_rows_after_manual_rollback=0
```

- `PATCH-NOTES-B1.md:42-43` 声称“迁移失败会整文件回滚”，目前对 COMMIT 阶段不成立。
- 建议：把 COMMIT 放进同一个 `try`，任何 BaseException 都尝试回滚；增加“COMMIT 抛错后 `in_transaction=False`、schema 与 ledger 均不存在、可直接重试”的回归测试。还应考虑 `db.init()` 在 migrate 失败时关闭并清空 `_conn`，避免同进程重试直接返回半初始化连接（`app/db.py:27-39`）。

### B1-M1 / MAJOR：3 小时 sweep 租约小于受支持配置的最坏执行上界

- 位置：`app/institute/analyst_daily.py:37-40,148-178,307-349`。
- `_pick_hand()` 会把所有分析师分配给“当前可用”的 hand（`app/institute/analyst_daily.py:213-220`）。当仅一个 CLI hand 可用时，9 位工作分析师全部落到同一 hand；executor 又对同一 hand 串行加锁（`app/router/executor.py:180-193`）。
- 默认超时为 1800 秒（`app/config.py:30-35`），executor 外层保险上界为 `timeout_s + 30`。最坏上界约为 `9 × 1830 = 16470 秒`，即 4 小时 34 分，明显大于 3 小时租约。
- 租约不续期，也没有 per-analyst running claim/fencing。旧 sweep 合法运行超过 3 小时后，第二个 `run_all()` 会接管锁，并重新启动尚未完成（包括仍在运行）的分析师，造成重复模型调用和双倍配额。
- CAS 只能防止旧 owner 误删新锁，不能阻止旧 owner 在接管后继续工作。`PATCH-NOTES-B1.md:74-77` 所称“run_all 互斥后已无路径触发双跑”因此不完整。
- 建议：优先增加租约续期/心跳并配合 fencing，或增加 per-analyst running claim；最低限度也应把租约设为严格高于可证明的最坏上界，并测试“旧 owner 仍活着时不可接管”。

### B1-M2 / MAJOR：ADD COLUMN 恢复守卫只比列名，会静默接受不兼容 schema

- 位置：`app/db.py:101-124`。
- `_skip_add_column()` 只确认同名列存在，不核对类型、NOT NULL、默认值、CHECK 或引用约束。实测已有 `mode INTEGER` 时，面对期望 `mode TEXT NOT NULL DEFAULT 'file' CHECK (...)` 的迁移仍返回 `True`，随后 ledger 会被记为已应用。
- 标识符正则的开引号和闭引号也是相互独立的可选字符；例如缺失闭引号的 `ALTER TABLE "probe ADD COLUMN a TEXT;` 也会匹配，并在 `a` 已存在时被静默跳过，而不是暴露 SQL 错误。
- 表名大小写本身不会误指向另一张表（SQLite 标识符大小写不敏感）；列名大小写差异反而会 fail closed，因为 Python 集合比较区分大小写。但“同名、定义不兼容”以及“不平衡引号”会被误吞。
- 建议：将恢复路径限制到明确的历史迁移/表/列白名单，并用 `PRAGMA table_xinfo` 校验预期定义；无法证明等价时必须失败，不应补记 ledger。至少应使用成对引号的严格解析并补不兼容定义、引号错误测试。

### B1-L1 / LOW：迁移“等价”测试只比较 sqlite_master

- 位置：`tests/test_db_migrate.py:35-53`。
- `MIGRATIONS` 确实动态包含并实际执行了当前全部 0001–0014（14 个文件），不是抽样测试。
- 但断言仅比较 `sqlite_master(type, name, sql)`，不比较 DML 种子数据、PRAGMA 副作用、外键检查，也没有在该对照测试中复现 aiosqlite `isolation_level=None` 的逐文件 BEGIN/COMMIT。
- 本次额外用 `foreign_keys=ON` 对 14 个文件做了旧 executescript 路径与显式逐文件事务路径的增强对照：schema 相同、业务表数据相同、两边 `foreign_key_check` 均为 0。因此当前迁移链没有发现差异；问题是未来回归覆盖面偏窄。

### B1-L2 / LOW：metrics 写失败 never-raise 没有仓库内回归测试

- 实现正确地在 `app/institute/scheduler.py:61-68` 捕获普通 Exception 并只记录日志；成功、任务失败、maintenance skip 三态测试均存在。
- `tests/test_cron_metrics.py` 的 6 个测试没有 monkeypatch `db.execute` 令 cron_metrics INSERT 失败，因此 `PATCH-NOTES-B1.md:64-66` 的关键降级承诺没有自动化保护。
- 建议增加一例：业务 job 成功/失败各自遇到 metric INSERT 异常时，wrapper 都不向 APScheduler 抛出。

### B1-L3 / LOW：未来时间戳会被视为超长 live，取消释放缺少正式回归测试

- 位置：`app/institute/analyst_daily.py:164-173,348-349`。
- 代码解析 UTC aware datetime 后做真实时间差，不依赖 ISO 字符串排序；正常 `bus.now_iso()` 形状正确（`app/bus.py:28-29`）。
- 但 `now - claimed_at < lease` 对未来时间戳也为真。若系统时钟曾大幅快进后回拨，或 claim 值语义损坏但仍可解析，该行会一直阻塞到未来时间再加 3 小时。建议要求 `0 <= age < lease`，否则按异常 claim 处理并告警。
- 仓库测试覆盖正常完成释放和 CAS 释放，但没有直接取消 `run_all()` 的用例。本次独立探针分别调用一次、连续两次 `Task.cancel()`，claim 行均为 0，确认当前 Python 3.13 取消语义下 finally 的 await 确实执行；仍建议把该探针固化为测试。

## 二、六项逐条结论

### 1. analyst_daily sweep claim 与 `_today_session`：FAIL

- `INSERT ... ON CONFLICT DO NOTHING` 使用 rowcount 决胜（`app/institute/analyst_daily.py:157-162`），符合条件认领硬规则。
- 过期接管 `UPDATE ... WHERE value = stale_value`、释放 `DELETE ... WHERE value = token` 都是完整 value CAS（`app/institute/analyst_daily.py:174-184`）；两个普通接管者只有一个 rowcount 为 1，迟到 owner 不会误删新 claim。仅剩 48-bit owner token 同秒碰撞的理论 ABA，概率极低。
- `_today_session` 的 per-loop lock 覆盖 SELECT + create（`app/institute/analyst_daily.py:110-129`），单进程部署下成立。
- 单次及预先连续两次取消的独立实测都释放 claim；`CancelledError` 不会被 `_safe` 的 `except Exception` 吞掉，能进入 `finally`。
- maintenance 门控位于 metered wrapper，paused 时在调用 `run_all()` 前返回（`app/institute/scheduler.py:81-86,129-132`），不会拿 sweep 锁。
- live 租约窗口内 whole-sweep run-now 返回 skip；per-analyst force endpoint和删行逃生通道已在 `PATCH-NOTES-B1.md:20-28,81-82` 写明。
- 但 3 小时不是执行上界，见 B1-M1；因此互斥保证不完整。

### 2. db.migrate 原子化与恢复：FAIL

- 当前 14 个迁移文件均被等价测试真实读取执行；增强对照也确认 schema、种子数据、FK 检查一致。
- `PRAGMA foreign_keys=ON` 在 migrate 前、显式事务外执行（`app/db.py:33-38`），时机正确；现存迁移没有修改该 PRAGMA。
- 0007 不创建 sqlite-vec 虚表，vec0 确实留给 runtime；但 0001 仍有 FTS5 `archive_fts` 虚表（`migrations/0001_init.sql:194-196`）。独立回滚探针确认 0001 在显式事务中回滚后 FTS 及 shadow objects 均消失，当前 FTS5 路径可事务化。
- 语句执行阶段失败会回滚 schema 与 ledger，现有测试有效（`tests/test_db_migrate.py:115-144`）。
- COMMIT 阶段未受保护，见 B1-H1；恢复守卫过宽，见 B1-M2。

### 3. scheduler.inflight_jobs 与 main 薄封装：PASS

- 私有 APScheduler 探测只在 `app/institute/scheduler.py:273-293`；`app/main.py:28-42` 是带最终降级保护的薄封装。
- 已安装 APScheduler 3.11.3；其 AsyncIOExecutor 对 async job 使用 `loop.create_task()` 并存入 `_pending_futures`，当前 `isinstance(f, asyncio.Task)` 过滤与真实实现吻合。
- accessor 会过滤 done/non-Task，并在私有结构漂移时返回空集合；定向 shutdown 测试通过。
- 降级为空集合意味着升级漂移时只能“取消但不等待”scheduler job，可能重新暴露 db.close 竞态；这是已文档化的降级损失，不是当前版本缺陷。

### 4. cron_metrics、metered、janitor 与 health：PASS-WITH-NITS

- `migrations/0008_cron_metrics.sql` 字段、CHECK、两个索引与三态写入一致。
- metered 的成功、失败、maintenance skip 均写一行；普通业务异常最终被最外层捕获，调度 job 不向上 raise（`app/institute/scheduler.py:75-104`）。
- skip 保存为 `ok=1, skipped=1`，health 聚合先按 skipped 排除，因此 `ok_rate = ok / (ok + failed)`、平均耗时也排除 skip（`app/api/meta.py:52-89`），口径正确。
- janitor 按固定宽度 UTC ISO 清理 30 天前记录（`app/institute/scheduler.py:212-216`）。端点自身不加时间 WHERE，依赖 janitor 已运行；重启后首次 janitor 前可能短暂包含超窗数据，属于轻微可观测性滞后。
- metric 写失败实现符合 never-raise，但缺测试，见 B1-L2。

### 5. truncate_output 极小 cap：PASS

- `app/router/executor.py:82-101` 在 marker 放不下时退化为 UTF-8 安全的纯头部切片。
- `cap=0/1/2/3` 已在 `tests/test_executor_output.py:30-46` 的真实循环中执行；ASCII 与被切开的 CJK 均覆盖。
- 所有非负 cap 均满足 `len(output.encode("utf-8")) <= cap`；未超 cap 的输入原样返回。

### 6. tests/conftest.py 特批 teardown：PASS

- diff 仅新增 analyst_daily/archive 两个 import、同步说明注释，以及 analyst_daily/research/archive 三组注册表并集（`tests/conftest.py:40-45,82-96`）；没有改变 setup、fixture 作用域或业务测试状态。
- 顺序安全：先汇总所有 task，统一 cancel，再 `gather(return_exceptions=True)`，最后 `db.close()`，允许取消清理路径在连接仍开放时落库。

## 三、migrate 原子化风险清单

1. **当前迁移链重放：已通过。** 0001–0014 共 14 个文件均实际运行；schema/data 等价，FK 检查为 0。
2. **现存隐式提交语句：未发现。** 纪律测试随本次 43 项一起通过，当前 0009–0014 未被误伤。
3. **虚表：当前可回滚。** 0007 vec0 不在迁移；0001 FTS5 在迁移且本机 SQLite 回滚成功。未来其他 virtual table module 仍需逐模块确认事务能力。
4. **外键 PRAGMA：当前时机正确。** 未来迁移若加入 `PRAGMA foreign_keys`、`defer_foreign_keys`、`journal_mode` 等，现有 forbidden-head 测试不会拦截，且事务内语义可能与 executescript 路径不同。
5. **提交失败：阻断风险。** COMMIT 不在保护区，见 B1-H1。
6. **旧库恢复：schema 漂移风险。** 仅按列名跳过会把不兼容定义补记为已迁移，见 B1-M2。
7. **测试覆盖：当前结果可信，未来 DML 覆盖不足。** sqlite_master 等价测试不检查数据/PRAGMA，见 B1-L1。
8. **初始化重试：残余风险。** 任意 migrate 异常后 `_conn` 仍非空；若进程不退出而重试 init，会绕过 migrate。建议失败时 close + `_conn=None`。

## 四、迁移纪律与硬规则核对

- 迁移纪律测试已在指定 pytest 命令中执行并通过；当前全部 14 个迁移无 `BEGIN/COMMIT/ROLLBACK/END/ATTACH/VACUUM` 首语句。
- 调度普通异常 never-raise：通过；`CancelledError` 作为 shutdown 控制流有意不吞。
- sweep 条件认领与接管均检查 rowcount：通过。
- 时间戳：`bus.now_iso()` 为 UTC、秒精度、带 offset；claim 用 datetime 解析比较，cron 清理使用同形 ISO 字符串序。未来时间戳例外见 B1-L3。
- B1 指定分区没有修改 prompts、rate_limits、`get_cli_env` 或 VaultWriter。仓库总体的 `app/institute/prompts.py`、`app/vault/writer.py` 当前确有其他代理未提交改动，已按指示不归因、不审查。

## 五、验证结果

```text
.venv/bin/python -m compileall app -q
PASS（exit 0）

.venv/bin/python -m pytest \
  tests/test_db_migrate.py tests/test_cron_metrics.py \
  tests/test_analyst_daily.py tests/test_executor_output.py \
  tests/test_executor_shutdown.py -q
43 passed in 1.57s
```

未运行全量测试，符合审查指令。
