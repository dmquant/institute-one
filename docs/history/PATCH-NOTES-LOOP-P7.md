# PATCH-NOTES-LOOP-P7 — fact_extract_queue 抽取 worker lease

来源：`roadmap/loop-fix-backlog.md` P7（中）。**判定：真缺口**——R2 的 lease
只覆盖 `fact_cards`（0034）与 dispute outbox event 行；live 库
`fact_extract_queue` 无 lease 列，claim/终态写全是 status-only 条件：stale
sweep 重开 + 新 worker 重认领后行重新是 'running'，老 worker 迟到的
done/failed 写照样命中 `WHERE status='running'`，覆盖新 claim。

## 修法（照抄 fact_cards 的 lease 三件套）

- `migrations/0036_fact_extract_queue_lease.sql`（新建，additive）：
  `ALTER TABLE fact_extract_queue ADD COLUMN lease_id TEXT;`
- **claim 带 lease**：`_drain_extractions` 认领时生成随机 lease
  （`SET status='running', started_at=?, lease_id=? WHERE id=? AND
  status='pending'` 查 rowcount）。
- **终态写带 lease**：done 与三处 failed 写（源文本缺失 / 抽取任务失败 /
  异常，统一进 `_fail_extract_row()` helper）全部
  `WHERE id=? AND status='running' AND lease_id=?`，落终态同时清 lease。
- **stale 回收清 lease**：`_recover_stale_running()` 重开 stale running 行时
  `lease_id=NULL`——老 worker 的 lease 即刻失效，其迟到写自然丢失。

## 回归测试（tests/test_factcheck.py，先红后绿）

- `test_extract_queue_stale_worker_late_write_loses`：A 在飞行中被重开、B 以
  新 lease 重认领，A 的迟到 failed 写 0 行——行仍归 B（status running、
  lease worker-B、error 为空）。修复前该写会落地（红灯确认）。
- `test_extract_queue_claim_writes_lease_and_terminal_write_clears_it`：
  happy path，done 后 lease 清空。
- `test_extract_queue_stale_sweep_clears_lease`：stale 回收后 pending +
  lease NULL。

## 自我对抗审查

- 终态不可复活：done/failed 只有操作员路径可重开（本模块无自动路径）。
- 输掉 lease 的 worker 其 extract_claims 已产出的 fact_cards 行按
  content_hash 幂等，新 worker 重抽为 per-claim no-op——与既有语义一致。
- 全部迁移条件宣占查 rowcount；无新执行路径、无新依赖。
