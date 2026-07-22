# PATCH-NOTES-LOOP-P11（g/h）— revival 扫描加界 + mailbox sweep 重驱上限

来源：`roadmap/loop-fix-backlog.md` P11 低危修补 B 中归属 scheduler/mailbox 的两小项
（g/h 合一份；a-f 属 chain/paper_book，归并行卡）。

## P11g — rate-limit-revival 候选扫描加 LIMIT

原位 `app/institute/scheduler.py`（原 334-339 行）：`SELECT * FROM tasks WHERE
status='rate_limited' AND 未带宣占标记 ORDER BY …` **无 LIMIT**——rate_limited
积压大时一次 tick 把全表候选（含完整 prompt 列）拉进内存，尽管每火最多只复活 3 个。

修法：新增模块常量 `RATE_LIMIT_REVIVAL_SCAN_LIMIT = 50`（与
`RATE_LIMIT_REVIVAL_LIMIT = 3`、`JANITOR_DELETE_LIMIT` 同风格），查询尾接
`LIMIT ?` 绑定该常量。50 对 3 留了足够余量：可跳过行（hand 冷却中 / lineage 有
在飞重试）都是暂时态，不会把窗口后的可复活行饿死；被宣占的行（error 带标记）
自动退出扫描窗口，后续 tick 持续推进。宣占/回滚/幂等语义零改动（仍是条件
UPDATE 查 rowcount）。

回归测试（追加到 `tests/test_rate_limit_revival.py`，2 个）：

- `test_revival_candidate_scan_carries_limit`：spy 包裹 `db.query` 捕获真实 SQL，
  断言 rate_limited 扫描语句带 `LIMIT` 且参数绑定 `RATE_LIMIT_REVIVAL_SCAN_LIMIT`，
  并断言常量 ≥ 每火复活上限（TDD 红位：旧 SQL 无 LIMIT）。
- `test_revival_scan_limit_bounds_fetch_without_starvation`：把扫描界压到 1，
  两行积压下第一火只复活 1 个（证明取数被界住而非只被复活上限截断），第二火
  复活余下那个（证明窗口推进、无饿死）。

## P11h — mailbox sweep 单次重驱上限

原位 `app/institute/mailbox.py` `sweep()`（原 227-239 行）：重启后对每个
kind='dispatch' 且 status='pending' 的孤儿行**全部**当场 `_spawn_bg(_run_dispatch)`
——积压大时一次 tick 涌出等量后台协程挤兑 executor（全局 3 槽 + per-hand 互斥）。

修法：新增模块常量 `SWEEP_REDRIVE_LIMIT = 20`；sweep 循环计数**实际 spawn 数**，
到 20 即 break 并 log 剩余行数。被跳过的行（`_inflight` 中 / task 仍在 executor
驱动）不消耗名额；没轮到的行保持 pending 原状，天然由下个 tick（
`mailbox_sweep_seconds` 间隔）继续消化——无状态丢失，无需新状态列。

回归测试（追加到 `tests/test_mailbox.py`，1 个）：

- `test_sweep_redrive_is_capped_per_tick`：直插 1 线程 + 3 条 `_inflight` 占位行
  + 25 条孤儿 dispatch，monkeypatch `_spawn_bg` 捕获而不真跑；断言单次 sweep 恰好
  spawn `SWEEP_REDRIVE_LIMIT == 20` 个（跳过行不占名额），且全部 28 行仍是
  pending（无丢失）。TDD 红位：旧代码会 spawn 25 个。
- 既有 `test_sweep_is_noop_when_clean` 不变仍绿（干净时零 spawn 语义未动）。

## 硬边界自查

条件宣占语义未动；时间戳仍走 `bus.now_iso()`；不改 prompts/workflows；无新依赖；
无迁移；未重启服务器。

## 验证（与 P9 同一轮）

- 模块 7 文件：**40 passed**（含 revival 原有 4 测试、mailbox 原有 2 测试全绿）。
- 全量：`.venv/bin/python -m pytest tests -q` → **5 failed, 1033 passed, 2 skipped**；
  5 失败均为并行卡在 `tests/test_operator.py` / `tests/test_chain.py` 的在飞半成品，
  与本卡零交集；剔除两文件复跑 **913 passed, 2 skipped** 全绿。
- `.venv/bin/python -m compileall app -q` → 通过。

## 改动清单

- `app/institute/scheduler.py`：`RATE_LIMIT_REVIVAL_SCAN_LIMIT` + 扫描 `LIMIT ?`。
- `app/institute/mailbox.py`：`SWEEP_REDRIVE_LIMIT` + sweep 重驱计数封顶。
- `tests/test_rate_limit_revival.py` 追加 2 测试；`tests/test_mailbox.py` 追加 1 测试。
- `roadmap/backlog.json` 未动（orchestrator 统一补卡）。

## R3 闭合（合入前复核 P1/P2：固定头部窗口饿死 + sweep 扫描仍无界）

R3 复核判定首版 LIMIT/封顶不彻底，两处都按「下推 SQL + 有界窗口 + admin_state
持久 keyset cursor 轮转」重做（无新迁移，各一行 admin_state；损坏/缺失游标
fail-open 回头部扫描，与 get_maintenance 同姿态）：

**P1（revival，scheduler.py）**——旧版 `LIMIT 50` 永远取排序最旧的 50 行，Python
跳过行（hand=NULL / hand 冷却 / lineage 有 live retry）不宣占、不离开头部，尤其
hand=NULL 是持久形态：50 条这种行即可让第 51 条 eligible 永不被扫描。修三点：
1. hand=NULL 回退 `requested_hand` 做冷却判定（与 respawn_from_row 实际复活用的
   hand 一致），不再让其占窗；
2. live-lineage 的 NOT EXISTS 下推进候选 SQL（`COALESCE(t.lineage_root, t.id)`），
   此类行不再进入窗口；宣占 UPDATE 里的竞态守卫原样保留；
3. 候选扫描改持久 keyset 游标（`admin_state['rate_limit_revival_cursor']`，
   `(COALESCE(finished_at,created_at), id)` 行值比较）：满窗推进到窗尾、短窗
   （到达表尾）回卷、复活到 3 上限提前 break 时推进到最后已处理行——每个有界
   窗口最终都被访问，无永久饿死。
   新测试：`test_hand_null_row_revives_via_requested_hand`、
   `test_revival_rotates_past_permanently_skipped_head`（50 条 hand 与
   requested_hand 双 NULL 的毒头行 + 第 51 条 eligible，两火后断言第 51 条被宣占
   复活、毒行零宣占；TDD 先红后绿）。

**P2（mailbox sweep，mailbox.py）**——旧版只封 spawn 数，SQL 仍一次读全部
pending dispatch 并逐行 `executor.get_task()`，单火 O(N)。修：
1. 「task 仍活着」判定下推 SQL（`NOT EXISTS (... t.id = m.task_id AND t.status IN
   ('queued','running'))`，与旧 `task is not None and status not in TERMINAL`
   逐分支等价：task_id 空/task 缺失/task 终态→候选，活 task→排除），循环内
   get_task 探测删除；
2. 新增 `SWEEP_SCAN_LIMIT = 100`：单火最多读 100 行候选；
3. 持久 keyset 游标（`admin_state['mailbox_sweep_cursor']`，整数 id keyset）同
   revival 轮转语义，_inflight 头部行不再能饿死其后孤儿。
   新测试：`test_sweep_scan_and_checks_are_bounded_per_tick`（110 条活 task 积压，
   spy 断言单火候选取数 ≤ SWEEP_SCAN_LIMIT、get_task 调用数 ≤ 上限、零 spawn）、
   `test_sweep_rotates_past_inflight_head`（105 条 _inflight 头行 + 1 孤儿：第一火
   零 spawn 证明窗口有界，第二火恰好 1 spawn 证明轮转可达；TDD 先红后绿）。

**验证**：五文件指定套件 → **37 passed**（含首版 8 个回归测试不回退）；
compileall 通过。全量 `.venv/bin/python -m pytest tests -q` →
**32 failed, 1067 passed, 2 skipped**，失败全在并行卡在飞文件
（test_roadmap 30 个：`RoadmapError: unknown type 'fix'`，backlog.json 被
orchestrator 补卡引入未注册 type；test_operator 3 个：P10a sweep 半成品；
test_api_routes/test_factcheck 等零星同源）；剔除这六个他人文件复跑：
**3 failed, 872 passed**（3 个仍是 test_operator 同源半成品，该文件属并行卡）。
上一轮全量里我方五文件相关的 0 失败。

## R4 闭合（合入前复核 P1：mailbox dispatch 无 DB 宣占 / task_id 错配）

R4 复核指出：`_run_dispatch` 在 `executor.submit` 前没有任何 DB 侧宣占（
`_inflight` 只是进程内集合，不是正确性机制），且 task_id 在终态宣占之前**无条件**
UPDATE——输掉 `status='done'` 条件宣占的晚到 worker 仍会覆盖 task_id，造成
dispatch.task_id 与真正落库的 reply 出自不同 worker（崩溃重入即可触发）。

修法（迁移 `migrations/0040_mailbox_dispatch_lease.sql`，additive：
`mailbox_messages` 加 `lease_id`/`leased_at`，0034 fact_cards lease 惯用法）：

1. **提交前条件宣占**：`_run_dispatch` 生成随机 lease，`UPDATE … SET lease_id,
   leased_at WHERE id=? AND kind='dispatch' AND status='pending' AND (无租约或
   租约过期)` 查 rowcount——只有 claim winner 才能调 `executor.submit`；
   `_inflight` 降级为纯进程内去重优化。
2. **task_id 只由 lease owner 写**：`UPDATE … SET task_id=? WHERE id=? AND
   lease_id=?`；绑定失败（租约被回收）直接丢弃晚到结果，不再触碰终态。
3. **终态携带租约**：done/failed 两个翻转都追加 `AND lease_id=?`（保留原
   `AND status='pending'`），晚到 worker 全部写操作变 no-op。
4. **sweep 只回收过期租约**：候选 SQL 增加 `(lease_id IS NULL OR leased_at IS
   NULL OR leased_at < 过期线)`；`DISPATCH_LEASE_TTL_S = 45*60`，盖过 executor
   default_timeout_s(1800s)+30s 缓冲，慢而活着的 dispatch 不会被从脚下重驱。
   时间基准 `datetime.fromisoformat(bus.now_iso())`（规则 7）。

新测试（先红后绿；红态即审核员剧本）：
- `test_late_dispatch_worker_cannot_overwrite_task_id_or_reply`：worker1 停在
  submit 内 →「重启」清 _inflight → 租约老化 → worker2 完整跑完 → 放行 worker1
  ——断言 task_id 与 reply 同出 worker2、仅 1 条 reply（旧代码 task_id 被 worker1
  覆盖 → 红）。
- `test_sweep_reclaims_only_stale_leases`：新鲜租约 sweep 不重驱（旧代码立刻
  重驱 → 红），老化后恰好重驱 1 次。

## R4 闭合（P1 revival 部分见 PATCH-NOTES-LOOP-P9.md 的 R4 小节；本卡两文件
共用六文件验证：`pytest tests/test_scheduler_backup.py tests/test_rate_limit_revival.py
tests/test_mailbox.py tests/test_maintenance.py tests/test_cron_metrics.py
tests/test_db_migrate.py -q` → **61 passed**，compileall 通过。）
