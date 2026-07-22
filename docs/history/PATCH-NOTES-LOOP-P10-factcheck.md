# PATCH-NOTES-LOOP-P10（factcheck 部分：c/d/e）— 低危修补

来源：`roadmap/loop-fix-backlog.md` P10 c/d/e。逐条与 R1/R2 已做工作核对后：
**c = 部分已覆盖，补残余缺口；d = 真缺口（与 R1 的 outbox 顶层修复是不同位置）；
e = 真缺口**。

## c) outbox 错误记录 CAS 失配重读 —— 部分已覆盖，补残余

- **已覆盖部分**（R2）：event 行 emit 失败的 last_error 已改由
  `_emit_dispute_event_row` 在 lease 释放写里直接落盘，不再依赖会 miss 的
  `_record_outbox_failure` 旧值 CAS。
- **本次补的残余缺口**：
  1. `_record_outbox_failure()`（mailbox 行 / 入队前毒 payload 共用）在 CAS
     失配时**重读一次 live attempts 再记**（行已 delivered/failed 或被 lease
     持有则不记）——并发 drain 抢先记账后，本方失败不再无声蒸发。
  2. `_emit_dispute_event_row` 改为返回
     `emitted/skipped/retry/failed` 四态、post-claim 失败**全部在函数内记账**
     （不再向 drain 循环 re-raise，避免与通用记账双计）；emit 失败在最后一次
     attempt 上直接落 `failed`（不再等下轮 sweep）；drain 结果的
     `retried/failed` 计数器现在如实覆盖 event 行。
- 测试：`test_record_outbox_failure_rereads_on_cas_miss`（陈旧快照记账成功、
  已 delivered 的行不记）；`test_event_outbox_emit_failure_releases_lease_and_stays_pending`
  增断 `retried==1`。

## d) 主 tick 去掉自吞异常 —— 真缺口（区别于 R1 的 factcheck-outbox 修复）

- R1 改的是 `drain_dispute_outbox`（factcheck-outbox job）的顶层 try/except；
  主 `tick()`（factcheck-tick job）当时仍 `except Exception: log` 自吞——
  系统性故障在 cron health 里永远 ok=1。
- 修：`tick()` 去掉顶层 try/except，顶层失败抛给 scheduler 既有的
  `@metered("factcheck-tick", gated=True)` 包装（scheduler.py 未动，只是依赖
  其行为）记 cron_metrics ok=0；per-card / per-row 失败仍在
  `verify_pending`/`_drain_extractions` 内吸收。模块 docstring 的
  「handlers/tick never raise」措辞同步修正。
- 测试：`test_tick_top_level_failure_propagates`、
  `test_tick_failure_lands_in_cron_health`（走真 scheduler._factcheck_tick_job，
  断言 cron_metrics 行 ok=0 + 错误文本）。

## e) 向量扫描加 LIMIT —— 真缺口

- `_reuse_state`（复用闸门）与 `claim_check` 向量腿的候选查询原本全表扫
  `fact_claim_vectors × verified_facts` 并逐行 Python 余弦；关键词腿的
  `_verdict_rows` 早有 LIMIT 2000，两条向量腿没有。
- 修：两处查询统一 `ORDER BY vf.verified_at DESC LIMIT ?`（新常量
  `VECTOR_SCAN_LIMIT = 2000`，与关键词腿同界）——最新判定优先，超窗老事实
  停止参与门控（degrade-open 语义：漏判=多验证一次，绝不错误复用）。
- 测试：`test_reuse_gate_vector_scan_is_bounded_newest_first`、
  `test_claim_check_vector_scan_is_bounded_newest_first`（LIMIT 收到 2 时，
  被两条更新判定挤出窗口的旧事实不再命中；正常窗口内照常命中）。

## 验证（三包 P3/P7/P10cde 合并后）

- `.venv/bin/python -m pytest tests/test_factcheck.py tests/test_db_migrate.py
  tests/test_cron_metrics.py -q` → **142 passed**
  （test_factcheck 100 → 112，+12：P3 ×4、P7 ×3、P10c ×1、P10d ×2、P10e ×2）；
  `.venv/bin/python -m compileall app -q` 通过。
- 新迁移仅 `0035_fact_cards_attempts.sql`、`0036_fact_extract_queue_lease.sql`
  两个文件；0034 未动；文件内无 BEGIN/COMMIT/ATTACH/VACUUM/PRAGMA。
- 边界：仅改 `app/institute/factcheck.py`、`tests/test_factcheck.py` + 两个新
  迁移 + 三份 PATCH-NOTES；scheduler.py 等并行 agent 文件零改动；未 commit；
  `roadmap/loop-fix-backlog.md` 勾选留给 orchestrator。
