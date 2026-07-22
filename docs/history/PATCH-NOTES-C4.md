# PATCH-NOTES-C4 — Operator loop & triage（ROADMAP Phase 6 前两项 + triage API）分区外挂载清单

C4 交付物（已落盘，独占分区内，全量 411 passed / 9 skipped，基线 387+24）：

- `migrations/0018_operator_actions.sql` — `operator_actions`（kanban）/ `action_dispositions`（shadow 建议，含 `flags` 列）/ `recipes`（Phase 6 L 项占位，本轮无代码读写）。0015–0017 编号留给并行 C1–C3；文件符合 B1 迁移纪律（无 BEGIN/COMMIT/PRAGMA，全 IF NOT EXISTS）。`uq_operator_actions_live_ref` 部分唯一索引 = 进料幂等的兜底（同一非空 ref 至多一条 open/in_progress）。
- `app/institute/operator.py`（新）— feeds + `sweep_vault_conflicts()` + shadow router + `resolve_action`/`dismiss_action`。**三条铁律写在模块 docstring 并被测试锁死**：①shadow mode（本轮 disposition 一律 shadow=1、只记录不执行，模块内不存在写 shadow=0 的路径）；②prompt/schedule 变更类连建议都标 `human_pinned`（kind 级：`scorecard_anomaly`/`cron_failure`；disposition 级：`adjust_prompt`/`adjust_schedule`）；③建议只能经 web UI 人工批准端点转化，绝不经 vault frontmatter 或 MCP。
- `app/api/operator.py`（新）— kanban 列表（含内联 dispositions）、PATCH 条件认领状态机、triage 聚合、feature switches PUT、**人工批准端点** `POST /api/operator/dispositions/{id}/approve`（批准=记账：条件认领 action → done + disposition 加 `approved` flag，不执行任何系统变更）。
- `tests/test_operator.py`（新）— 24 个测试（幂等、阈值、shadow 断言、human_pinned、低置信、triage 形状、approve 流、防双处置）。

## 需要主代理执行的挂载 1：main.py lifespan 注册 feeds（C4 无权改 main.py）

`vault_exporter.register()` 旁边（`sched.start()` 之前）加两行：

```python
    from .institute import operator as operator_loop
    operator_loop.register()
```

`register()` 幂等（重复调用 no-op），handler 永不 raise（bus 层还有一道兜底）。

## 需要主代理执行的挂载 2：main.py create_app 挂路由

import 列表加 `operator as api_operator`，include_router 元组加 `api_operator.router`。

## 需要主代理执行的挂载 3：scheduler.py 三个 job（C4 无权改 scheduler.py）

```python
@metered("operator-fast-route", gated=True)
async def _operator_fast_route_job() -> None:
    from . import operator
    await operator.route_actions(cap=5, proposed_by="fast_loop")  # 便宜 hand = settings.default_hand

@metered("operator-deep-route", gated=True)
async def _operator_deep_route_job() -> None:
    from . import operator
    # 强 hand 的旋钮：hand="claude"（或主代理属意的强 hand）；不传则同 default_hand
    await operator.route_actions(cap=10, proposed_by="deep_loop")

@metered("operator-vault-sweep")
async def _operator_vault_sweep_job() -> None:
    from . import operator
    await operator.sweep_vault_conflicts()
```

`start()` 里注册：

```python
    every(_operator_fast_route_job, "operator-fast-route", minutes=15)
    every(_operator_deep_route_job, "operator-deep-route", minutes=60)
    every(_operator_vault_sweep_job, "operator-vault-sweep", minutes=60)
```

裁决理由：

- **fast/deep 两个 route job 必须 gated=True**——它们经 `executor.submit` 烧模型配额（A4 轮门控判据：是否提交新模型调用）。若 `tests/test_maintenance.py::test_job_gating_registry_matches_semantics` 的清单要更新，把这两个加进 gated 集合。
- **vault sweep ungated**——与 janitor/hand-scorecard 同类：读盘+写行，零配额；maintenance 期间恰恰应该继续记录冲突。
- 每 loop 对同一 action 只建议一次（SQL 里 NOT EXISTS 按 proposed_by 过滤），所以 15 分钟 tick 对停滞 kanban 不会重复烧配额；处理量由 cap 控制。
- route_actions/sweep 自身永不 raise（scorecard.run_once 同款兜底），@metered 是第二道带子。

## C1 对齐说明（factcheck 事件名）

C1（fact-check）在建，其 PATCH-NOTES 尚未落盘。C4 按约定订阅 `factcheck.disputed`（常量 `operator.FACTCHECK_DISPUTED_EVENT`，payload 按不可信输入处理：空/畸形 payload 也能开出 action 且不炸）。若 C1 终稿事件名不同，改这一个常量即可；事件从未发生时 handler 永不运行，零成本。

## 设计说明与偏差（记录在案）

- **`action_dispositions.flags` 列是对预分配 schema 的最小扩展**：`low_confidence` / `human_pinned` / `approved` 需要可查询的落点（原 shorthand 未给标记留位置）。逗号连接的 marker 集，migration 头注释有枚举。
- **进料幂等键 = 非空 `ref` 上的活跃行唯一**（open/in_progress）；ref 语法 `task:<id>` / `workflow:<run>` / `fact:<id>` / `scorecard:<date>` / `vault:<path>`。已关闭的 ref 复发会开新 action（历史留痕不复用）。空 ref（人工 other 类）不去重。
- **task.failed 进料跳过 `source='operator-router'` 的任务**——否则 hand 故障时路由器自己的失败分类任务会无限繁殖 action。
- **missing 状态的 vault 笔记不开 action**：行是真相、笔记可重建，删除是人的特权不是冲突（sweep 返回的 doctor counts 里仍可见）。
- `sweep_vault_conflicts()` 调 `writer.doctor()` 拿权威计数，但 doctor 只返回计数不返回路径，所以 per-path 判定镜像了 doctor 的分类逻辑（复用 writer 的私有 helper，未复制代码）。**后续卡建议**：给 doctor 加 `detail=True` 返回逐行状态，删掉这面镜子。
- feature switches 本轮只是「存储+展示」（admin_state key `feature_switches`，PUT 全量替换）；没有任何子系统读它。接线是后续 Phase 6 卡。
- `config.py`/`.env` 零新增（阈值走模块常量，B1 §1 先例）：`FALSE_COMPLETE_RATE_THRESHOLD=0.2`、`SCORECARD_MIN_SCANNED=5`、`CONFIDENCE_FLOOR=0.7`、`ROUTER_TIMEOUT_S=300`。
- SPA kanban/triage 页面不在本轮（端点形状已就绪，`GET /actions` 内联 dispositions 就是为了让 UI 能拿到 disposition id 驱动 approve 按钮）。
- `roadmap/backlog.json` 状态迁移由主代理推进，C4 未动。

## 遗留风险

- 本轮 `route_actions` 未接任何调度（挂载前零自动路由）；挂载后 deep loop 的强 hand 选择是主代理的旋钮，默认与 fast 同 hand。
- disputed_fact 进料依赖 C1 的事件真的发出来；在那之前 kanban 的该列恒空（可用 `open_action("disputed_fact", ...)` 手工补）。
- 批准后的执行仍是人工步骤（铁律使然）：approve 只记账。等 shadow 模式退出的那轮，`shadow=0` 的写入路径必须新开并配套测试（现在测试锁死了「无 shadow=0 行」）。
- bus handler 一经 register 进程内不可注销（bus 无 off()）；测试进程里其余测试文件 emit 的 task.failed 会在各自的临时 DB 里落 operator_actions 行——无副作用（全量 411 绿已验证），但审计 events 表时能看到。
