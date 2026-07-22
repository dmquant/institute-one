# PATCH-NOTES-B1 — 分区外集成需求与语义说明

给主代理：B1 分区（第二轮加固包：analyst_daily 并发防护 / db.migrate 原子化 /
scheduler.inflight_jobs / truncate_output 极小 cap / conftest teardown /
cron_metrics + /api/cron/health）**不需要改 `app/config.py`、pyproject.toml 或 .env**。
以下是有轻微跨分区影响的语义变化与可选后续项。

## 1. 新常量（刻意不进 Settings）

- `analyst_daily.SWEEP_LEASE_S = 30*60` / `SWEEP_HEARTBEAT_S = 5*60`：sweep 认领
  的租约与心跳（REVIEW-B1 M1 后改为心跳续租模型，见 §2）。不是调度参数，故不做
  成配置。
- cron_metrics 保留窗口 30 天：janitor 清理与 `/api/cron/health` 的 `window_days`
  同为 30，硬编码在 `scheduler._janitor` 与 `api/meta.cron_health`。要改窗口时
  两处一起改（或届时再提成 Settings）。

## 2. analyst_daily sweep 认领的操作员语义

- 认领行：`admin_state` key `analyst_daily_sweep:<SGT date>`（**不在**
  `analyst_daily:<date>:*` 命名空间下，不会被 `_get_record`/status 端点误读）。
- 并发 `run_all()`（19:00 cron 与手动 run-now 重叠、连点两次 run-now）：一个赢家，
  输家立即返回 `{"skipped": "sweep already running"}`（POST /api/analysts/daily/run-now
  仍 202，效果是 no-op）。
- **心跳续租**（REVIEW-B1 M1）：赢家 sweep 运行期间每 5 分钟 CAS 刷新一次
  claimed_at，因此**活着的 sweep 永不会被接管，无论跑多久**——最坏执行时长本身
  无上界（单 hand 可用时 per-hand 互斥串行 9 个分析师 ≈ 9×1830s ≈ 4h35m，且随
  roster 增长），静态租约无法覆盖，故弃用"拉长租约"方案。租约 30 分钟只界定
  "硬杀进程（心跳停止、finally 未跑）后多久可被接管" = 6 个未响应心跳周期。
  心跳 CAS 失败（认领被强删/接管）时自动退出不再争抢；迟到的 release 对新 owner
  的认领是 no-op。
- 未来时间戳防护（REVIEW-B1 L3）：claimed_at 在未来（时钟回拨/垃圾值）按 stale
  处理并 WARN，不会把当天卡到未来时刻+租约。
- 逃生通道：sweep 卡住时 per-analyst 强跑端点（`POST /api/analysts/{id}/daily/run`）
  不经过 sweep 认领，照常可用；也可直接删 admin_state 行解锁（心跳会发现丢锁并
  退出）。正常完成（含 cancel/异常路径的 finally）都会释放认领行。
- 释放与接管都是 CAS（比对整个 value 串），超时旧 owner 迟到的 release 不会误删
  新 owner 的认领。

## 3. db.migrate 原子化后的迁移文件纪律（对所有写迁移的分区生效）

- 每个迁移文件现在跑在**单个显式事务**里（逐语句 execute，不再 executescript），
  SQL 与 schema_migrations 记账同 commit——崩溃窗口不复存在。**COMMIT 本身失败
  （SQLITE_BUSY/磁盘满/IO 错）也在保护区内**（REVIEW-B1 H1）：回滚兜底 + 连接退出
  事务态可直接重试；`db.init()` 中 migrate 失败会 close 连接并清空 `_conn`，同进程
  重试不会拿到跳过迁移的半初始化连接。
- 因此迁移脚本内**禁止** `BEGIN`/`COMMIT`/`ROLLBACK`/`END`/`ATTACH`/`VACUUM`；
  `tests/test_db_migrate.py::test_real_migration_files_have_no_transaction_statements`
  对全部 `migrations/*.sql` 强制此纪律（0001–0014 现状全部合规）。**也不要在迁移
  里写 PRAGMA**（forbidden-head 测试暂未拦截，但事务内语义与旧 executescript 路径
  可能不同——需要时先扩纪律测试）。
- 语句切分用 `sqlite3.complete_statement`（字符串/注释里的 `;` 不会截断；结尾
  无 `;` 的最后一条语句也会执行）。多行 `CREATE TRIGGER ... BEGIN ... END;` 也能
  正确整体切分（当前无触发器迁移，属前瞻验证）。
- 恢复路径（针对旧版非原子 migrate 留下的"schema 已变、记账缺失"库）：重放时
  `ALTER TABLE ... ADD COLUMN` 仅当既有列**定义可证等价**（类型/NOT NULL/DEFAULT，
  即 PRAGMA table_info 暴露的部分；标识符成对引号解析、大小写不敏感）才跳过并
  WARN；同名但定义不一致抛 `MigrationRecoveryError`（schema 漂移不是崩溃重放，
  绝不静默补记账）；引号不成对等解析不了的语句原样交给 SQLite 报语法错
  （REVIEW-B1 M2）。其余语句依赖 `IF NOT EXISTS` 幂等。迁移失败会整文件回滚并在
  日志给出可操作提示（手工补记账的 INSERT 语句）。

## 4. /api/cron/health（新端点，挂在 api/meta.py）

响应形状（前端/插件想接的话）：

```json
{"window_days": 30, "jobs": {"briefing": {
  "last_fired_at": "...", "last_status": "ok|failed|skipped",
  "fires": 3, "ok": 1, "failed": 1, "skipped": 1,
  "ok_rate": 0.5, "avg_duration_ms": 200,
  "last_error": {"fired_at": "...", "error": "..."} }}}
```

- `ok_rate`/`avg_duration_ms` 只算真实执行（maintenance skip 不进分母）；
  没执行过则为 null。从未 fire 过的 job 不出现在 `jobs` 里（表是唯一事实源）。
- 没有新 bus 事件（cron_metrics 不发事件），useSSE 清单无需更新。
- SPA/插件暂无消费界面——如果第二轮有前端分区想加 cron 健康页，直接消费此端点。

## 5. 其他说明

- `scheduler.metered()` 现在每次触发写一行 cron_metrics（成功/失败/skip 各一行），
  写失败只 log 不影响 job（"调度任务永不 raise"不变）。`fired_at` 是触发时刻
  （不是完成时刻）。
- `main._scheduler_inflight()` 已改为薄封装调 `scheduler.inflight_jobs()`；
  APScheduler 私有结构探测现在只存在于 scheduler.py 一处（PATCH-NOTES-A1 §2 落地）。
- `truncate_output` 在 `cap_bytes <= len(marker)` 时降级为纯头部字节切片（不再
  超 cap；宁可无 marker 也不输出一个比 cap 还长的 marker）。

## 6. 遗留风险（记录在案，未处理）

- run_one 本身仍无 running 态认领：`spawn_one`（API 侧 force=True）与进行中的
  sweep 并发时同一分析师会双跑。这是"强制重跑"端点的文档化语义（操作员显式
  行为），且 run_all 互斥后已无自动路径能触发；如果未来出现 force=False 的新
  调用方，需要补 per-analyst running 认领（带租约，防 crash 残留卡死当天补跑）。
- `_today_session` 用进程内锁堵竞态（单进程系统足够）。没有加
  `UNIQUE(kind, title)` 数据库约束：老库可能已有重复 daily session 行，加约束
  会让迁移卡在存量数据上；跨进程双开服务器本就是不支持的部署形态。
- sweep 硬杀后的 30 分钟租约窗口内，手动 run-now 会被 skip（日志有 INFO）；
  等租约过期或删 admin_state 行即可。cron 在 19:00 只跑一次，不受影响。
- 病理场景下心跳丢锁后 sweep 不中止：心跳连续 6 个周期失败（DB 持续故障）后被
  接管，旧 sweep 在途的 executor 任务会继续跑完（不易撤销），期间新旧两个 sweep
  可能重叠——per-analyst completed 标记幂等，重叠窗口只影响尚未完成的分析师。
  心跳把这个窗口从"3 小时后必然"压到"仅当 DB 连续故障 30 分钟"。
- ADD COLUMN 恢复守卫不比对 CHECK/REFERENCES/GENERATED（PRAGMA table_info 不暴露
  这些）：同名同类型但 CHECK 不同的漂移仍会被跳过。当前全部 ALTER（0005/0010/
  0011/0012）中仅 0010 带 CHECK；如需更强校验要上 `PRAGMA table_xinfo` + sql 文本
  比对，本轮判定为过度工程。
