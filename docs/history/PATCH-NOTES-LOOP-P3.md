# PATCH-NOTES-LOOP-P3 — factcheck 毒卡片重试上界

来源：`roadmap/loop-fix-backlog.md` P3（高）。**判定：真缺口**——R1/R2 只做了
lease 与 verdict/outbox 加固；live 库 `fact_cards` 无 attempts 列，验证失败仍
无条件放回 pending + `ORDER BY created_at ASC`，一张毒卡每 tick 3 次重试、每日
cap=10 约 2 小时烧光，与既有工作零重叠。

## 修法

- `migrations/0035_fact_cards_attempts.sql`（新建，additive）：
  `ALTER TABLE fact_cards ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;`
- `VERIFY_MAX_ATTEMPTS = 3`（backlog 建议值）。
- **失败原子计数**：`_record_failed_attempt()` 在释放 lease 的同一条件写里
  `attempts=attempts+1`（`WHERE id=? AND status='verifying' AND lease_id=?
  AND attempts+1 < ?` 查 rowcount）；`_verify_card` 的 task 失败路径与
  `verify_pending` 的 crash 路径都走它。**每日预算耗尽的 release 不计数**
  （不是卡的错）——原 `_release_card` 保留给该路径。
- **达上界终态**：计数写 0 行且非丢 claim 时，`_settle_exhausted_card()` 在
  一个事务里条件宣占（带 lease）置 `status='unverifiable'` + 写一条
  UNVERIFIABLE `verified_facts` 行（evidence 注明「验证任务连续失败 N 次」）。
  用 `unverifiable` 而非新状态：0015 的 status CHECK 不可改（老迁移不可动），
  语义等价——拿不到判定、退出轮换、UNVERIFIABLE 不进 reuse gate / claim_check。
  `INSERT OR IGNORE` 兜住已有 verdict 行的卡（UNIQUE fact_card_id）。
- **取卡排序纳入 attempts**：picker 改
  `WHERE status='pending' AND attempts < ? ORDER BY attempts ASC, created_at ASC`
  ——重试过的卡沉到新卡之后，耗尽的卡直接排除。
- **崩溃窗口兜底**：`_recover_stale_running()` 末尾把
  `status='pending' AND attempts>=N` 的遗留卡（计数已写、终态未落时进程死了，
  或运行中调低上限）逐张 `_settle_exhausted_card(lease_id=None)`（条件宣占
  `AND status='pending' AND attempts>=?`），不会卡在「picker 跳过但永远 pending」
  的悬空态。

## 回归测试（tests/test_factcheck.py，先红后绿）

- `test_poison_card_exhausts_after_max_attempts`：毒卡恰 3 次模型调用（非 10），
  终态 unverifiable + verdict 行，再 sweep 零调用。
- `test_poison_card_sinks_behind_fresh_cards`：attempts=1 的老卡让位给新卡。
- `test_exhausted_pending_card_settled_by_recovery_sweep`：崩溃窗口卡被 picker
  排除、被恢复 sweep 落终态。
- `test_budget_exhausted_release_does_not_count_attempt`：预算耗尽的释放
  attempts 保持 0。
- 既有 `test_verify_failed_attempts_burn_budget`（cap=2 < N）语义原样通过。

## 自我对抗审查

- 上界真封顶：picker 的 `attempts < N` 与计数写的 `attempts+1 < N` 双向夹住；
  终态由条件宣占落地、无任何路径把 unverifiable 复活回 pending。
- 全部状态迁移条件宣占查 rowcount；时间一律 bus.now_iso()/work_date()。
- 测试恒真检查：四个新用例在修复前全部 FAILED（TDD 红灯记录见会话）。

---

## R3 P1 闭合 — stale 回收不消耗 attempts，硬崩溃可无限重试

R3 合入前复核 finding：attempts 只在 post-model 失败处理器里自增；进程在
「每日 slot 已预占、模型已启动」之后硬崩溃时无任何 handler 运行，60 分钟后
`_recover_stale_running()` 把卡放回 pending 而 attempts 仍为 0——同一毒卡跨
重启无限重试，永远到不了 VERIFY_MAX_ATTEMPTS；每日 cap 只是单日闸，不构成
卡片级总上界。/tmp 复现脚本对修复前代码实锤：5×[claim→置 stale→sweep] 后
`status=pending attempts=0`。

### 修法（按审核员方向：预记 attempt）

- **`_prebook_card_attempt(card_id, lease_id)`**（新）：每日 slot 预占成功后、
  模型调用前，在当前 lease 下原子预记（`UPDATE fact_cards SET
  attempts=attempts+1 WHERE id=? AND status='verifying' AND lease_id=?` 查
  rowcount）。预记失败（claim 在极窄窗口被夺）= 不调模型、daily slot 照旧
  不退。**关键顺序保持**：预算耗尽的 `_release_card`（不计数）发生在预记
  之前——审核员确认的现状语义未破坏。
- **失败路径不再自增**：`_record_failed_attempt` 改名
  `_release_failed_card`——只按已预记次数 release（`AND attempts < N`）或
  settle 终态，杜绝双计；`_settle_exhausted_card` 的 lease 分支去掉
  `attempts=attempts+1`、加 `AND attempts>=N` 守卫（两分支统一「只 settle
  真耗尽的卡」）。
- **回收保留已花 attempt**：`_recover_stale_running()` 本就不动 attempts
  （预记已落盘），stale verifying → pending 后同一趟的 exhaustion backstop
  把 `attempts>=N` 的卡事务内落 `unverifiable` + UNVERIFIABLE verdict 行。
- attempts 语义微调：现在计「已启动的验证尝试」（成功路径也计 1，终态后无
  影响）而非仅失败次数；0035 迁移文件的注释按「迁移只增不改」规则未动，
  语义以本节 + 代码 docstring 为准。
- fact_cards.attempts 全模块**唯一写点**即预记（rg 复核）；各路径恰计一次：
  成功 1 / task 失败 1 / 软异常 1 / 硬崩溃 1（sweep 保留）/ 预算耗尽 0 /
  预记失败 0。残余窗口：reserve 与 prebook 两条 UPDATE 之间硬崩溃会花 slot
  不计 attempt——窗口内模型从未启动，无真实消耗，属方案固有且可接受。

### 回归测试（先红后绿；红=AttributeError，旧代码无预记机制）

- `test_crash_after_prebook_keeps_attempt_after_sweep`：slot+预记后硬崩溃
  （不跑任何 handler）→ sweep 后卡 pending 且 attempts=1。
- `test_repeated_crashes_terminalize_poison_card`：跨重启反复硬崩溃，恰好
  3 轮后卡不可再认领、终态 unverifiable + verdict 行、verify_pending 零
  模型调用。
- /tmp/repro_r3_p1.py：修复前 `attempts=0 BUG`，修复后
  `status=unverifiable attempts=3 -> bounded`（真实输出见会话）。

### R3 后验证

`.venv/bin/python -m pytest tests/test_factcheck.py tests/test_db_migrate.py
tests/test_cron_metrics.py -q` → **144 passed**（test_factcheck 112 → 114）；
`compileall` 通过。无新迁移（attempts 列沿用 0035）。

---

## R4 闭合 — 2 P1 + 1 P3（合入前复核，全部修复）

三条先以 /tmp/repro_r4.py 对修复前代码实锤（真实输出）：
P1a `status=unverifiable attempts=3 factcheck_tasks=0`（零任务终态化）、
P3 `daily_counter>card_attempts` 且无模型任务（slot 白耗）、
P1b `card=unverifiable stored_fact=VERIFIED in_reuse_candidates=True`
（死卡旧结论仍进复用门）。修复后同脚本三条全 ok。

### [P1] prebook→task 窗口 + [P3] reserve→prebook 窗口 → 原子预订

- **`_book_verification(card, lease)`**（新，取代 `_reserve_attempt` +
  `_prebook_card_attempt`，二者删除）：每日 slot 条件自增、卡片
  `attempts+1` + `verify_task_id` 绑定（带 lease 条件宣占）、以及一条
  born-'queued' 的 durable `tasks` 行（executor `_create_row` 同形，
  source='factcheck'）**在同一 SQLite 事务提交**；任一环失败
  （`_BookingRefused`）整体回滚——预算耗尽/丢 claim 时 slot、attempts、task
  三者一个都不消费（P3 闭合）。返回 (task_id, "ok"/"budget"/"lost")。
  `task.queued` 事件在提交后补发（事务内 emit 会死锁）。
- **attempts ⇔ durable task 不变式**：每消费一次 attempt 必有一条 task 行。
  硬崩溃后 queued 行由既有 boot orphan sweep（executor.recover_orphans）
  明确结算，不再靠 card 侧猜测模型是否启动（P1 闭合）；`verify_task_id`
  （迁移 0038，additive ALTER ADD COLUMN）永远指向该卡最近一次验证任务，
  settle 时保留（溯源）。
- **执行**：`_run_verification_task(task_id)` 把预建行交给 executor 自己的
  claim-and-run 层 `_execute()`（条件宣占 queued→running、hand 锁+全局信号量、
  fallback、终态落账；`_running` 注册照抄 submit 使 cancel/关机 drain 生效）。
  submit/spawn 均不接受预建 id（已核对），forecast 先例只是域内确定性 id、
  不涉 tasks 行——此处**刻意、有记录地**使用 executor 私有 API，后续卡应在
  executor.py 解冻后补公开 `submit_prepared(task_id)` 入口。已知偏差：预订
  路径不做 `_overcommit_depth` 检查（factcheck 串行 await，每进程任一时刻至
  多一条在飞 queued 行，深度上限保护的是无界堆积场景）。
- `verify_pending` 顺序：claim → `_book_verification`（"budget" → 释放不计数
  并停；"lost" → 什么都没消费，跳过）→ 驱动预建 task → settle/释放。
  失败释放（`_release_failed_card`）继续不二次自增。

### [P1] 耗尽 settle 的 INSERT OR IGNORE 保留旧 VERIFIED → UPSERT 换代

- `_settle_exhausted_card` 与 `_verify_card` 的 verdict 写入统一改
  `INSERT ... ON CONFLICT(fact_card_id) DO UPDATE SET verdict/evidence/
  source_urls/work_date/verified_at/expires_at = excluded.*`：active 行原地
  换代（行 id 保留），card status 与 active verdict 永远同代——unverifiable
  的卡不可能再挂着 VERIFIED active fact 进 reuse gate / claim_check。
  `_verify_card` upsert 后回读 live 行 id，outbox/事件的 related_fact_id
  指向真实 active 行（旧裸 INSERT 会在重置卡上撞 UNIQUE 直接把该次尝试
  烧成 crash）。历史版本表（verified_facts 版本化 + active generation）记档
  留后续卡；本轮以"active 行永远与卡同代"堵住数据诚信洞。

### 回归测试（先红后绿）

- `test_crash_after_booking_keeps_attempt_and_task`（P1a：崩溃后 attempt 保留
  且必有 queued task 行绑定）。
- `test_repeated_crashes_terminalize_poison_card`（改写：终态时
  `tasks WHERE source='factcheck'` 恰好 = VERIFY_MAX_ATTEMPTS，零幻影 attempt）。
- `test_booking_atomicity_no_half_consumed_slot`（P3：budget/lost 两种拒绝下
  slot、attempts、verify_task_id、tasks 四者全零；成功预订四者同现）。
- `test_settle_exhausted_overwrites_stale_active_verdict`（P1b：reset+耗尽后
  active 行翻 UNVERIFIABLE、reuse gate 与 claim_check 双面不再命中）。
- `test_reverify_after_reset_updates_active_verdict`（重置卡复验：active 行
  原地换代、emit 的 fact_id 为 live 行 id）。
- `verifier_output` fixture 迁移为拦截 `_run_verification_task`（不再嗅探
  executor.submit 的 prompt），并把 durable 行落终态，与生产行为同形；
  两个 mid-flight 劫持测试同步迁移。

### R4 后验证

`.venv/bin/python -m pytest tests/test_factcheck.py tests/test_db_migrate.py
tests/test_cron_metrics.py -q` → **147 passed**（test_factcheck 114 → 117）；
`compileall` 通过；/tmp/repro_r4.py 修复后 P1a/P3/P1b 全 ok。新迁移
`migrations/0038_fact_cards_verify_task.sql`（additive，无禁用语句）。

---

## R5 闭合 — task 恢复、reset 读窗、dispute 跨代

R5 三组回归先在旧实现上跑红：定向结果为 **6 failed, 2 passed**。已完成
task 被无条件重开、pending/verifying 卡的旧 verdict 继续命中，以及旧 dispute
outbox 错投/新代被吞，均由测试直接复现。

### [P1-1] task-aware verification recovery

- `_recover_stale_running()` 不再批量把 stale `verifying` 卡改回 pending，而是
  按当前 `verify_task_id` 读取 durable task：
  - `completed`：调用共享 `_settle_completed_verification()`，只解析已有 output
    并落账，绝不进入 executor；
  - `failed/rate_limited/cancelled/expired/overcommitted`：按已预订 attempt
    release，达到 `VERIFY_MAX_ATTEMPTS` 则落 UNVERIFIABLE；
  - `queued/running` 且 `_running` 有 live owner：原样保留；无 owner 时条件宣占
    task 为 failed 后再收敛 card；
  - `verify_task_id IS NULL` 是 claim→booking 之间崩溃的显式「尚未预订」状态，
    未消费 attempt/slot，按旧 lease 条件释放；`_claim_card()` 会先清除上一代
    task id，避免 reset 后把旧 output 误认作本代结果；
  - 非空 task id 对应行缺失、缺 lease 或绑定不匹配：通过
    `_quarantine_verification_binding()` 显式落 UNVERIFIABLE，evidence 记录原因，
    不静默创建新代。
- 正常完成与恢复完成共用同一 parse/settle 路径；card terminal settle、失败
  release 和耗尽 settle 均带 `card.id + lease_id + verify_task_id` 条件，旧 task
  output 不能写进新 lease/generation。
- 回归：
  `test_recovery_settles_completed_bound_task_without_model_call`、
  `test_recovery_terminal_task_failure_converges_without_model_call`、
  `test_recovery_does_not_reopen_task_with_live_owner`、
  `test_recovery_missing_bound_task_fails_closed`。

### [P1-2] reset 窗口读侧 fail closed

- `_reuse_state()`、`_verdict_rows()`、`claim_check()` vector SQL 都显式 join
  `fact_cards`，只接受：
  `(status='verified' AND verdict='VERIFIED') OR
  (status='disputed' AND verdict='DISPUTED')`。
- pending/verifying/unverifiable 卡遗留的 mutable verdict 行在新代 settle 前不再
  参与 reuse、keyword claim-check 或 vector claim-check。
- 参数化回归
  `test_reset_window_old_verdict_excluded_from_all_read_surfaces[pending|verifying]`
  同时覆盖三个读面。

### [P1-3] dispute outbox 以 task 为 verification generation

- 新 dispute 幂等键改为
  `<kind>:<fact_card_id>:<verify_task_id>`；同代重试复用同 id，新代生成新 row，
  旧 delivered 行不再吞掉后续 dispute。self-contradicted 没有验证 task，使用
  immutable `extract:<card_id>` generation。
- mailbox/event payload 均携带 `verification_generation`、`verify_task_id` 与
  自包含 `snapshot`（verdict/claim/category/evidence/source_urls）；
  `related_fact_id` 继续保留，但不再作为跨代权威 provenance。
- drain 在 claim 前核对当前 card `status='disputed'`、active fact
  `verdict='DISPUTED'`、card `verify_task_id` 与 payload generation 一致。
  不一致或无法证明 generation 的 legacy row 条件宣占为现有终态 `failed`，
  保留审计行并写 `last_error='superseded-generation'`，绝不 emit。
- 回归：
  `test_old_pending_dispute_outbox_superseded_after_reset`、
  `test_new_dispute_generation_not_blocked_by_old_delivered`、
  `test_same_dispute_generation_reuses_outbox_and_delivers_once`。

### Schema

无需 `0041`：R4 已有且在 booking 时不可变绑定的
`fact_cards.verify_task_id` 足以作为最小 verification generation；本轮无 schema
变更。

### R5 验证

- `.venv/bin/python -m pytest tests/test_factcheck.py -q`：
  **126 passed**。
- `.venv/bin/python -m pytest tests/test_factcheck.py tests/test_db_migrate.py
  tests/test_cron_metrics.py tests/test_restart_recovery.py -q`：
  **163 passed in 14.66s**。
- `.venv/bin/python -m compileall app -q`：`COMPILEALL_OK`。
