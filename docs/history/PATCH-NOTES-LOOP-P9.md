# PATCH-NOTES-LOOP-P9 — janitor 备份一致性（VACUUM INTO 快照）

来源：`roadmap/loop-fix-backlog.md` P9（中优）。原位 `app/institute/scheduler.py`
janitor 第 7 步（原 485-491 行）：`PRAGMA wal_checkpoint(TRUNCATE)` 后用
`shutil.copy2` 直拷在写的库文件——拷贝进行中若发生自动 checkpoint（WAL 回写主文件），
备份文件就是撕裂的半新半旧字节，且当天不再重试（`target.exists()` 已为真）。

## 修法

`app/institute/scheduler.py`：

1. **一致性快照**：备份逻辑抽成 `_nightly_backup()`，用 SQLite 运行时 SQL
   `VACUUM INTO ?`（经 `db.execute` 参数绑定）替代 checkpoint+copy2。快照走 SQLite
   自己的事务机制读一致视图，并发 checkpoint/写入不再能损坏它。原
   `wal_checkpoint(TRUNCATE)` 只为让 copy2 拷到全量数据而存在，随之删除。
   **纪律注**：`VACUUM INTO` 是**运行时执行**的 SQL，不落任何 `migrations/` 文件；
   migrations 内禁 VACUUM 的规则（`tests/test_db_migrate.py`）不受影响，本卡零迁移。
2. **崩溃安全落名**：快照先写 `institute-<date>.db.tmp`，成功后 `Path.replace`
   原子改名为 `institute-<date>.db`；失败/崩溃残留的 tmp 在下次进窗时先
   `unlink(missing_ok=True)` 清掉（`VACUUM INTO` 拒绝写已存在文件），保证
   「once per date」守卫 `target.exists()` 只认完整备份。
3. **失败隔离**：janitor 第 7 步改为 `try: await _nightly_backup() except Exception:
   log.exception(...)`。备份失败只留日志，janitor 其余步骤不受影响，其
   cron_metrics 行保持 ok=1（不再把整个 janitor 火次标失败）。

窗口语义不变：03:00–05:00 SGT、每日期一次、时间判断仍走 `now_sgt()`/`work_date()`
（无裸 `datetime.now()`）。janitor 步骤 1–6 零改动。

## 回归测试（新文件 `tests/test_scheduler_backup.py`，5 个）

- `test_backup_is_valid_consistent_snapshot`：窗内 janitor 后备份存在、
  `PRAGMA integrity_check` = ok、`admin_state` 行数与在线库一致、无 tmp 残留。
- `test_backup_skipped_outside_window`：窗外零文件。
- `test_backup_written_once_per_date`：已有当日备份不被覆盖。
- `test_backup_recovers_from_crashed_tmp`：预置崩溃残留 tmp，下次进窗仍落有效备份
  且 tmp 被清（TDD 红位之一：旧代码不清 tmp）。
- `test_backup_failure_never_poisons_janitor`：备份炸掉时 metrics-prune 步骤照跑、
  janitor 自身 cron_metrics 行 ok=1 且 error 为空（TDD 红位之二：旧代码无隔离）。

测试对 `backups_dir` 先清后用（同一 INSTITUTE_HOME 跨测试存活），窗口经
monkeypatch `scheduler.now_sgt` 注入，不依赖真实时钟。

## 验证

- 模块：`pytest tests/test_scheduler_backup.py tests/test_rate_limit_revival.py
  tests/test_mailbox.py tests/test_maintenance.py tests/test_cron_metrics.py
  tests/test_events_retention.py tests/test_restart_recovery.py -q` →
  **40 passed**（含 janitor 邻居断言：job 数 24、gate 表、prune、booked 计数器、
  stuck-run 过期全未被破坏）。
- 全量：`.venv/bin/python -m pytest tests -q` → **5 failed, 1033 passed, 2 skipped**；
  5 个失败全在 `tests/test_operator.py`（4）与 `tests/test_chain.py`（1），是并行
  P2/P4 卡的在飞半成品（placeholder disposition / poison persistence 尚未实现），
  与本卡零交集。剔除这两文件复跑：**913 passed, 2 skipped** 全绿。
- `.venv/bin/python -m compileall app -q` → 通过。

## 改动清单

- `app/institute/scheduler.py`：新增 `_nightly_backup()`；janitor 第 7 步改为隔离调用。
- `tests/test_scheduler_backup.py`：新建（5 测试）。
- 无迁移、无新依赖、不动 prompts/workflows；`roadmap/backlog.json` 留待 orchestrator 统一补卡。

## R4 闭合（合入前复核 P1：revival marker 先于 retry 持久化，硬崩溃永久除名）

R4 复核指出（本节记在 P9 文件因同属 scheduler.py；P11 文件互见）：revival 先把
永久 marker `[rate-limit-revival:claimed]` 写进 source task.error，再调
`executor.respawn_from_row`。普通 Exception 有补偿清 marker，但 SIGKILL/
BaseException 不会——候选 SQL 永久排除带 marker 的行，该 lineage 从此死掉，无
lease 无 stale reclaim（R3 的 cursor 轮转帮不上：行在进窗前就被过滤）。
**永久 marker 不能兼任崩溃安全 claim。**

修法（迁移 `migrations/0039_revival_lease.sql`，additive：`tasks` 加
`revival_lease_id`/`revival_leased_at`/`revival_attempts INTEGER NOT NULL
DEFAULT 0`；沿用 0034 fact_cards lease 惯用法，条件宣占查 rowcount）：

1. **宣占改租约**：respawn 前的条件 UPDATE 只写随机 lease + `revival_attempts+1`
   （条件含：无 marker、attempts < 上限、无租约或租约过期、lineage 无 live 行），
   不再写永久 marker。
2. **marker 后置**：`_mark_revival_consumed()` 在 retry 世代真实 durable 之后才写
   marker，且携带 `AND revival_lease_id=?`——被回收租约的晚到写是 no-op。
   IntegrityError（unique index 判定 lineage 已有 live retry）同样视为已消费、
   补 marker（与旧语义一致）。
3. **崩溃恢复**：respawn 前硬崩 → 只留 lease，过
   `RATE_LIMIT_REVIVAL_LEASE_TTL_S = 15min` 后候选 SQL 自动回收重试；retry 已
   durable 但 marker 未写时硬崩 → 重启后 `recover_orphans` 把孤儿 retry 翻终态、
   source 无 marker、租约过期后再宣占（at-least-once，永不静默丢失）。
4. **attempt 上限**：`RATE_LIMIT_REVIVAL_MAX_ATTEMPTS = 5`，软失败释放租约但
   attempts 已递增，毒源 5 次后永久停牌，不再每 tick 烧一次宣占。

新测试（先红后绿，红态即审核员剧本「marker 残留 + 第二轮 respawn=0」）：
- `test_hard_crash_before_retry_insert_leaves_source_claimable`（BaseException
  模拟 SIGKILL：断言崩溃后无 durable marker，租约老化后下一火成功复活）
- `test_hard_crash_after_retry_insert_recovers_after_restart`（retry durable 后
  崩溃：recover_orphans + 租约老化 → 再宣占 → lineage 两个世代且有 completed）
- `test_revival_attempts_are_capped`（软失败循环恰好 5 次后停牌，故障消除也不
  再重试）

验证：六文件套件（+tests/test_db_migrate.py）→ **61 passed**；compileall 通过。
既有 revival 语义测试（cooldown 跳过、live-lineage 幂等、每火上限 3、R3 轮转/
NULL-hand 回退）全部不回退。

## R3 闭合（合入前复核 P3：janitor 裸 UTC 时钟）

R3 复核指出 janitor 顶部仍是 `datetime.now(timezone.utc)` 生成各步 cutoff，违反
硬规则 7（统一时间源）。修：改为 `datetime.fromisoformat(bus.now_iso())`（格式与
入库字符串一致，秒精度 +00:00，比较语义不变），SGT 日期继续走 `now_sgt()`/
`work_date()`；`timezone` import 随之移除（已无使用点）。回归测试
`tests/test_maintenance.py::test_scheduler_source_never_calls_raw_datetime_now`
（源码级断言 scheduler.py 不含 `datetime.now(` 调用，TDD 先红后绿）。
验证：五文件指定套件 `pytest tests/test_scheduler_backup.py
tests/test_rate_limit_revival.py tests/test_mailbox.py tests/test_maintenance.py
tests/test_cron_metrics.py -q` → **37 passed**；compileall 通过。
