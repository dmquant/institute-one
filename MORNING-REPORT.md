# 晨间验收报告 — 北极星过夜优化（2026-07-20 夜 → 07-21 晨）

> 状态：**代码与本机运行闭环完成，待 operator 人工验收**。正式 roadmap 卡仍保留在 `review`，未用实现就绪冒充业务验收。

## TL;DR

北极星本轮可执行范围已收口：

- **R5 最终协议审计闭环**：两份复核共发现 10 P1 / 4 P2 / 1 P3，15 项均已修复并由回归或故障注入测试锁定；当前判决 `ACCEPT`。
- **权威全量**：续审与 live 缺口修复后为 **1159 passed / 2 skipped**；backend `compileall`、frontend 16 项测试与 build、Obsidian plugin build 均通过。
- **live 已对齐**：先确认 `running_now=0`，完成一致性 SQLite 备份，再将运行库从 0034 对齐到 0043 并重启 LaunchAgent。数据库完整性、API contract、队列、24 个 cron 注册及 thesis import-batches 路由均已实查。
- **安全边界**：仅做本地收口提交；不 push、不 restore/rebase，也不替 operator 将正式 roadmap 卡从 `review` 移到 `done`。

## 一、测试轨迹

| 阶段 | 全量 pytest |
|---|---|
| 接手基线 | 893 passed |
| R1 North Star 后 | 979 → 1003 passed |
| loop-fix + R2/R3/R4 闭合后 | 1078 → 1096 → 1113 passed / 1 skipped |
| R5 协议闭环与稳定化后 | 1148 passed / 2 skipped |
| 独立续审与最终提交候选 | **1159 passed / 2 skipped** |

两个 skip 都是显式 opt-in 的真实外部环境测试：

1. 市场数据真网络 smoke（`INSTITUTE_NET_TESTS=1`）；
2. 真实 bge-m3 calibration（`INSTITUTE_CALIBRATION_REAL=1`）。

其他验证：

```text
.venv/bin/python -m compileall app -q        OK
cd frontend && npm run test                  2 files / 16 tests passed
cd frontend && npm run build                 OK
cd obsidian-plugin && npm run build          OK
git diff --check                             OK
```

## 二、loop-fix 十二包（M10 bounded autonomy）

`roadmap/loop-fix-cursor-prompt.md` 的核心不变量已经落实：任何单行或单次失败都不能造成无限配额消耗、队列饿死、事件循环阻塞或状态静默丢失。P1–P12 全部完成，细节见 `PATCH-NOTES-LOOP-*.md` 与 `roadmap/loop-fix-backlog.md`。

- executor：先 hand 锁后全局信号量，避免等待一个 hand 时占死其他 hand 的槽位；
- operator / factcheck / chain：毒行、毒卡和游标失败都有持久化上界；
- research tree / forecast / paper book：终态宣占、事件投递与 cursor 前进可恢复；
- scheduler / mailbox：持久化 lease、task binding、attempt ceiling 与重启恢复；
- vault / janitor：阻塞 I/O 搬离事件循环、一致性备份、写入与冲突扫描临界区对齐。

## 三、R2–R5 对抗复核

| 轮次 | 发现 | 最终状态 |
|---|---:|---|
| R2 | 11 | 全部闭合 |
| R3 | 13（4 P1 / 8 P2 / 1 P3） | 全部闭合 |
| R4 | 12（6 P1 / 4 P2 / 2 P3） | 全部闭合 |
| R5 | 15（10 P1 / 4 P2 / 1 P3） | **全部闭合，ACCEPT** |

R5 的关键协议修复包括：

- factcheck 以 `verify_task_id` 对账已完成任务，reset 隔离旧 verdict，dispute outbox 绑定验证 generation；
- chain 不再把合法 alias 集截断为 512，而是在总比较预算内跨 tick 扫完；
- revival 在同一事务预订 reciprocal source/child binding，重启驱动同一个 durable task；
- mailbox 原子预订 task，reply/message/thread/event 同事务结算，模型提交最多 3 次、结算最多 5 次；
- durable event 可对已插入 event id fan-out，不制造重复行；
- parameter history 与 effect baseline 原子提交；VaultWriter 与 operator 共用协调锁关闭 TOCTOU；
- operator handler 注册以 bus 实际 handler 集为准，消除测试与重启下的 stale boolean；Sina 中文 payload 使用 GB18030 解码。

逐项 finding → 修复 → 测试映射见 `PATCH-NOTES-NORTHSTAR-R5-CLOSURE.md`。原始审计 `REVIEW-R5-FACT-CHAIN.md`、`REVIEW-R5-QUEUE-AUDIT.md` 保留发现现场，并追加了闭环判决。

### 独立续审（2026-07-21 晚）

在 R5 闭环之后又对完整 working tree 做了一轮独立 Claude Code review，并由本地只读 reviewer 复核修复。没有发现 CRITICAL / HIGH；确认的提交前问题全部闭合：

- 丢失 `0028_task_overcommitted.sql` ledger 行时，不再重放破坏性的 `tasks` 重建；当前 schema 已证明 0028 合同后只补幂等索引，任何漂移都失败关闭，保留 0039–0043 的列与数据；
- chain property 跨 `YYYY` / `YYYY-Qn` / `YYYY-MM` / `YYYY-MM-DD` 以结构化期间结束日排序，`2026-07` 不再被字典序误判为早于 `2026-Q2`，未知格式失败关闭；
- maintenance 在重启窗口也成为严格额度门禁：executor/mailbox 仍复用原 durable recovery 分区完成纯对账，但不挂模型 driver；恢复后 scheduler 继续驱动同一 task id；
- prompt override cache 在任何启动恢复前预热，active override 重启后的第一个 prompt 即生效；
- multi-agent 在 API 与 domain 两层拒绝重复/超过 5 个 agents，所有 mutation body 禁止未知字段，spawn 中途失败保留可重连的 `run_id` / `task_ids`；SPA 同时对齐 200 completed 与 202 pending 两种响应；
- Python 3.14 长测试进程中的 Sina mock payload 改用 canonical `gb18030`，消除 `gbk` alias 偶发 codec lookup 失败。
- live 复核发现 `/api/theses/import-batches` 被 path-like thesis catch-all 吞掉；现已把静态路由置前，读取真实 provenance 表并对本机路径、凭据字段、内嵌 token/Bearer 与 URL userinfo 统一脱敏。

## 四、迁移与 live 对齐

本轮迁移为 0030–0043，均保持 additive、单文件单事务规则。R5 新增：

- `0041_factcheck_outbox_lease_bridge.sql`：把曾后加到已应用 0034 的 outbox lease 列移到独立 bridge，恢复历史迁移不可变性；
- `0042_durable_revival_binding.sql`：revival source/child reciprocal binding；
- `0043_mailbox_dispatch_protocol.sql`：mailbox dispatch id、task binding、attempt/reconcile counters、reply event id 与唯一索引。

live 操作证据：

- 重启前 `/api/tasks/queue`：`running_now=0`；
- 迁移前 SQLite 一致性备份：`/private/tmp/institute-one-pre-r5.qHGejK/institute.db`，完整性 `ok`，包含 836 条 task、34 条旧 migration ledger；
- 最终提交重启前备份：`/private/tmp/institute-one-pre-92b06d5.p1qj14/institute.db`，27M，完整性 `ok`，包含 1059 条 task、43 条 migration ledger，最新为 0043；
- 重启后 live `PRAGMA integrity_check`：`ok`；migration ledger 已到 `0043_mailbox_dispatch_protocol.sql`；
- LaunchAgent `com.institute-one.server`：最终 PID 20512，监听 `127.0.0.1:8100`；
- `/health`、`/api/meta`、`/api/tasks/queue`、`/api/contract`、`/api/cron/health`、`/api/theses?flat=true` 与 `/api/theses/import-batches` 均返回成功；后两者在当前空数据状态都返回 `[]`；
- contract 四项 schema cross-check 全为 `ok`；队列最终为 `running_now=0`（957 completed / 90 failed / 9 overcommitted / 3 expired）；
- 24 个 scheduler job 全部注册，最新状态无 failed；维护开关已恢复为 `paused=false`，恢复后复查服务与队列稳定。

## 五、仍需你决定的边界

1. **正式 roadmap 验收**：M9 与 LOOP 卡仍在 `review`。当前完成的是代码、测试和本机运行就绪，不替你做业务接受决定。
2. **More hands**：vane / mflux 的源仓库 `agent-route-node` 不在本机，属于可选扩展；
3. **Legacy data migration**：本机没有旧 researchos corpus，属于有数据时才执行的可选迁移；
4. **Claude hand**：仍遵守现有本机配置保持禁用，默认 hand 仍为 codex，未擅自改动。

## 六、安全声明

本轮只形成本地收口提交；没有 `git push`、`git restore`、rebase 或工作区清理。正式 M9/LOOP 接受仍由 operator 决定。
