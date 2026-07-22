# REVIEW-A4 — Phase 0 maintenance 门控 + workflow key 归一化

## 结论

**PASS-WITH-NITS**

A4 的两项主目标均成立：

- 8 个调度任务中 7 个受 maintenance 门控，仅 janitor 不门控；门控在 job 进入业务函数前短路。
- workflow step 在落库前归一化到 `analyst_id`；旧 `analyst` 输入仍兼容，未知 id 会 warning，运行时仍按原行为回退到 `chief-strategist`。

未发现会卡死白板/信箱、误拦操作员手动触发、修改 prompt、引入 migration 或破坏时间戳约定的问题。发现 2 个低风险边界问题：maintenance API 的 bool 校验是宽松 coercion；双 key 冲突时 legacy `analyst` 反而覆盖 canonical `analyst_id`。

审查仅覆盖 A4 分区。`app/api/tasks.py`（A1）、`app/api/roadmap.py`（A6）及其他在途代理改动均未纳入结论。

## 问题分级

### N1 · 低：maintenance API 不会拒绝所有非 bool JSON 值

- 位置：`app/api/meta.py:45-46`
- 测试缺口：`tests/test_maintenance.py:39-41`

`MaintenanceBody.paused` 使用普通 Pydantic `bool`，会把 `1`、`0`、`"true"`、`"false"` 接受并转换成布尔值，而不是按接口声明只接受 JSON boolean。实测：

```text
1       -> True
0       -> False
"true"  -> True
"false" -> False
None / [] / {} -> 422
```

现有测试注释写“body must carry a bool”，但只覆盖了字段缺失，没有覆盖字符串和数字。建议改用 `StrictBool`（或严格模型配置），并补 422 用例。当前 coercion 会写入正确的布尔 JSON，不影响正常 SPA 调用，因此定为低风险。

### N2 · 低：双 key 冲突时 alias 优先于 canonical key

- 位置：`app/institute/workflows.py:49-50`
- 测试缺口：`tests/test_workflows.py:46-62`

当前归一化顺序是：

```python
legacy = step.pop("analyst", None)
aid = str(legacy or step.get("analyst_id") or "").strip()
```

因此同时提供两键且值不同时，旧 `analyst` 会覆盖 canonical `analyst_id`；这也与运行时 `app/institute/workflows.py:210-213` 的“canonical first”顺序不一致。例如：

```text
{"analyst": "legacy-id", "analyst_id": "canonical-id"}
→ {"analyst_id": "legacy-id"}
```

仓库当前三份 workflow 定义没有双 key，主路径不受影响。建议让 `analyst_id` 优先，并补一条冲突用例；也可在两值不同时 warning。

## 8 个调度任务门控决策表

| Job | gated | 是否会开启/推进模型工作 | 核验结论 |
|---|---:|---|---|
| `briefing` | 是 | 启动晨会 workflow | 正确；A4 新补 |
| `daily-report` | 是 | 启动日报 workflow | 正确；A4 新补 |
| `analyst-dailies` | 是 | 批量提交分析师日报 | 正确；原已门控 |
| `whiteboard-kickoff` | 是 | 认领 topic、建立新 board/首张 pending card | 正确；属于新开工，原已门控 |
| `whiteboard-tick` | 是 | 认领下一张 card；card 完成后可能提交 handoff | 正确；A4 新补 |
| `mailbox-sweep` | 是 | 重驱 orphan dispatch，随后提交模型调用 | 正确；A4 新补 |
| `research-tick` | 是 | 认领 pending research 并运行 workflow | 正确；原已门控 |
| `janitor` | 否 | 只做 DB 状态清理、过期、adhoc 清理、DB 备份 | 正确；无 `executor.submit`，不烧模型配额 |

实际装饰器元数据核验结果为 7 个 `gated=True`、`janitor=False`，与 A4 声称一致。janitor 代码位于 `app/institute/scheduler.py:125-185`，没有模型调用；保持运行还能在暂停期清理卡死 workflow、陈旧 topic、旧 adhoc workspace 并做备份，独留理由成立。

## maintenance 门控机制

- `app/institute/scheduler.py:33-40` 从 `admin_state.key='maintenance'` 读取 `{"paused": ...}`。
- key 不存在时明确返回 `False`，所以新库默认不暂停。
- `app/institute/scheduler.py:57-62` 在计时和业务函数调用之前先读取状态；paused 时立即 `return`，不会先认领队列、创建 workflow 或启动模型任务。
- wrapper 的普通异常由 `app/institute/scheduler.py:65-66` 吞掉并记录，调度任务不会把异常抛回 APScheduler。

边界语义符合 A4 的裁决：这是“阻止新的 scheduler job 开工”，不是 executor 全局急停。切换前已经越过门控的 job，以及已经创建的 card/dispatch/workflow 后台协程，会自然跑完；操作员手动触发也不会被拦截。

## 白板暂停与恢复

结论：**不会因门控 `whiteboard-tick` 卡死；board 级状态持久，恢复后沿原 board 继续。**

- board/card 均持久化在数据库；`tick()` 只查询 `status='active'` board（`app/institute/whiteboard.py:217-223`）。
- 暂停时 wrapper 不进入 `tick()`，因此 pending card 不会被认领，也不会丢失。
- 已启动 card 是 `_bg_tasks` 中的普通 asyncio task，会继续完成；card executor 使用全局默认超时，handoff 另有 `HANDOFF_TIMEOUT_S=300`（`app/institute/whiteboard.py:38,305-341,417-430`）。
- handoff 生成的下一张 card 以 `pending` 落库（`app/institute/whiteboard.py:438-450`）；暂停数天不会被 janitor 清掉。
- 恢复后，`tick()` 会认领原 board 的 pending card；若期间发生进程重启，遗留 `running` card 会在首次恢复 tick 时标记 failed，再做 handoff（`app/institute/whiteboard.py:230-248`），不会永久占住 board。
- 最后一张 card 或 handoff stop 后，恢复时下一次 tick 会 finalize（`app/institute/whiteboard.py:250-262`）。

“原地续推”应理解为沿同一 board、已有 cards 和 session 继续；如果暂停期间进程重启，正在运行的那一张 card 会按既有 orphan 规则记 failed，而不是原 turn 重试。

另一个已接受边界：如果 maintenance 在 card 已运行后才打开，该 card 完成后的 handoff 仍可能再消耗一次模型调用。这属于 A4 明示的“在途协程自然 drain”，不是本次 gate 能提供的瞬时硬停。

## 信箱暂停与恢复

结论：**不会卡死；orphan dispatch 持久保留，恢复后 sweep 重驱。**

- dispatch 先以 `status='pending'` 落库，再启动 `_run_dispatch`（`app/institute/mailbox.py:115-125`）。
- 已启动 `_run_dispatch` 是普通后台 task，会自然完成（`app/institute/mailbox.py:128-209`）。
- 暂停时 gated sweep 不运行，orphan pending row 保留。
- 恢复后 sweep 会跳过本进程仍在途的 id；若旧 `task_id` 已终态或不存在，则重新启动同一 dispatch（`app/institute/mailbox.py:214-229`）。
- 没有按年龄删除 pending dispatch 的 janitor 逻辑，因此长暂停不会丢信箱工作。

## API 核验

- `POST /api/admin/maintenance` 位于 `app/api/meta.py:49-53`，写入后回读并返回 `{"paused": bool}`。
- `scheduler.set_maintenance()` 写入 `admin_state` 的 JSON 为 `{"paused": true|false}`；`get_maintenance()` 读取同一 key 和同一字段，形状一致。
- `GET /api/admin/state` 仍返回既有的 `{key: raw_json_string}` 结构；测试已验证其中 `maintenance` 可解析为 `{"paused": true}`。
- 新库缺 key 时 `get_maintenance()` 返回 `False`。
- 唯一问题是 N1：普通 `bool` 会宽松接收字符串/0/1。

## workflow key 归一化

- `reconcile_from_disk()` 在落库前调用 `_normalize_steps()`（`app/institute/workflows.py:62-84`）。
- 只含 legacy `analyst` 的 step 会删除旧 key 并写成 `analyst_id`。
- 只含 `analyst_id` 的 step 保持 canonical 形状。
- 未知 analyst id 使用 `log.warning`，消息含 workflow id、step id 和未知 id（`app/institute/workflows.py:53-57`），满足“大声警告且 boot 不 raise”。
- 未知 id 仍保留在定义中；运行时再次 warning 后回退到 `chief-strategist`（`app/institute/workflows.py:210-220`），没有顺手改成 raise。
- 双 key 冲突存在 N2 的优先级不一致。
- 测试验证了 warning 中的 workflow id 和 analyst id；实现虽含 step id，但测试没有显式断言 step id，建议顺手补强。

## prompts、migration、时间戳与手动触发

- 对 HEAD 与当前 `workflows/{briefing,daily,research}.json` 的每个 step `prompt` 做了 JSON 解码后逐字符串比较，三者均 `prompts_equal=True`；diff 只改了 7 个 key 名。
- A4 分区没有 migration。仓库中其他在途 migration 不属于 A4。
- A4 没新增时间戳格式；workflow reconcile 继续使用 `bus.now_iso()`（UTC、秒精度、带时区），maintenance 的 `admin_state` schema 本身没有时间戳列。
- maintenance 只存在于 scheduler wrapper 和 admin API，没有下沉到 executor/domain 层。因此以下显式操作员入口在 paused 时仍可触发，符合“手动动作保留”：
  - `POST /api/workflows/daily/briefing/run-now`
  - `POST /api/workflows/daily/daily/run-now`
  - `POST /api/workflows/{workflow_id}/run`
  - `POST /api/analysts/daily/run-now`
  - `POST /api/analysts/{analyst_id}/daily/run`
  - 手动 whiteboard/research `tick`/`kickoff` API 同样不经 scheduler wrapper

## 验证结果

```text
.venv/bin/python -m compileall app -q
PASS

.venv/bin/python -m pytest tests/test_maintenance.py tests/test_workflows.py -q
11 passed in 0.68s

git diff --check -- <A4 tracked files>
PASS
```

仓库没有 `tests/test_scheduler.py`，因此按实际存在文件运行了 `tests/test_maintenance.py` 与 `tests/test_workflows.py`，未跑全量测试。

## PATCH-NOTES-A4.md

4 条分区外跟进均与当前代码一致：README maintenance 语义、CLAUDE workflow key 文档、SPA toggle、前端 `WorkflowStep.analyst?` 类型收紧。它们不阻塞 A4 后端主目标。
