# PATCH-NOTES-C5 — Phase 7（委员会 workflow + 多代理原语）分区外改动清单

（v2：按 REVIEW-C5 修订 —— M1 请求级总超时 / M2 幂等周认领 / M3 weekday 防御性注册；含次级项 json_set、异常边界、裁决措辞。）

C5 交付物（已落盘，独占分区内）：

- `workflows/committee.json` — 5 步周委员会辩论。裁决步骤的多数判定已按 REVIEW-C5 P2 消歧：**任意两名及以上辩手明确投同一方向即形成多数；含糊表态不得代为补票但计入三人分母；没有方向取得至少两票才写「未达成裁决」**。其余 prompt 逐字未动。
- `app/institute/workflows.py` — 两个独立新增段（不碰 `_drive` 主循环与 prompt 常量）：
  - `${WEEK_DISPUTES}` 惰性变量（`week_disputes_variable()` + `_drive` 里与 `${DATA_BUNDLE}` 并列的注入块）。持久化改为 **`json_set` 单键写入**（REVIEW-C5 P2：整 blob 覆盖会丢并发写者落的键；status='running' 守卫只保证终态不可变，不是并发保护）——`DATA_BUNDLE` 的持久化也同步改成 json_set。
  - `run_committee_once()`（REVIEW-C5 M2）：**每 ISO 周至多一跑**的持久化原子认领，见 §1.2。
- `app/institute/multi_agent.py` — 原语拆成两半（REVIEW-C5 M1 的域层支撑）：`spawn_fan_out(agents, prompt, *, hand, timeout_s)` 逐 agent `executor.spawn`（persona 三明治、校验先于任何 spawn），立即返回 task ids；`wait_fan_out(task_ids, *, timeout_s)` 用 `asyncio.wait`（**超时不取消任务**，driver 异常留在行状态里，不向上传播成 500）；`fan_out` = 两半组合（无预算等待）。`join(tasks, mode)` 四模式不变。
- `app/api/multi_agent.py` — `POST /api/multi-agent/run` 加请求级总墙钟预算（契约见 §3）。
- `tests/test_committee.py`（18 测试）+ `tests/test_multi_agent.py`（11 测试）。

## 1. 主代理需要做的事（C5 无权修改的文件）

### 1.1 main.py 挂载 multi_agent router（两行）

`create_app()` 的 `from .api import (...)` 里加 `multi_agent as api_multi_agent,`，`include_router` 元组里加 `api_multi_agent.router,`。测试用裸 FastAPI app 包 router，不依赖挂载，先合并不炸。

### 1.2 scheduler 加 committee job + config 字段

`app/config.py`（Scheduler 段，紧跟 memory_compact_time）：

```python
committee_time: str = "20:00"   # 每周委员会（仅周五触发；"" 禁用）
```

> **裁决点（REVIEW-C5 P2）**：本卡与 README recipe 用周五 20:00 SGT，`ROADMAP.md:165` 写的是 22:00 SGT on committee days。请主代理先裁决产品源再固化默认值，两处只留一个真相（若定 22:00，只改上面这个默认值即可，其余不动）。

`app/institute/scheduler.py` job（**必须调 `run_committee_once`，不要直接 `run_workflow`** —— 幂等认领在域层）：

```python
@metered("committee", gated=True)
async def _committee_job() -> None:
    from . import workflows
    await workflows.run_committee_once(source="scheduler")
```

幂等语义（REVIEW-C5 M2，已在域层落地并有测试）：`run_committee_once` 以 `admin_state` 行 `committee:<ISO周>`（如 `committee:2026-W30`）做 `INSERT ... ON CONFLICT DO NOTHING` 原子认领——scheduler misfire/coalesce 重放、重启、手动重复触发全部收敛为每周一跑。重试语义：该周 run 终态为 failed/cancelled（或认领后 1 小时仍没记上 run_id——kickoff 崩了）时，经 CAS UPDATE 接管认领重跑一次；running/completed 的 run 关死本周。逃生门：`POST /api/workflows/committee/run`（通用端点）绕过守卫，等同 daily 的语义。

### 1.3 cron helper 扩展 day_of_week（REVIEW-C5 M3 —— 完整 diff）

现有 `cron()` helper 只支持每日 HH:MM。**不要在 helper 外裸写 `split(":")` + `CronTrigger`**（v1 建议稿的错误：`committee_time=""` 或非法格式会让 `scheduler.start()` 抛异常、应用 lifespan 启动失败）。请按下面的 diff 扩 helper——空串禁用与解析失败 log.error 禁用两条防御原样覆盖新参数（非法 `day_of_week` 会在 `CronTrigger(...)` 构造时抛 `ValueError`，落进同一个 except）：

```diff
-    def cron(job: Callable, name: str, hhmm: str) -> None:
+    def cron(job: Callable, name: str, hhmm: str, day_of_week: str | None = None) -> None:
         hhmm = (hhmm or "").strip()
         if not hhmm:
             log.info("job %s disabled (empty time)", name)
             return
         try:
             h, m = hhmm.split(":")
-            trigger = CronTrigger(hour=int(h), minute=int(m), timezone=settings.timezone)
+            trigger = CronTrigger(day_of_week=day_of_week, hour=int(h), minute=int(m),
+                                  timezone=settings.timezone)
         except (ValueError, TypeError):
             log.error("job %s: cannot parse time %r; disabled", name, hhmm)
             return
         sched.add_job(job, trigger, id=name, max_instances=1, coalesce=True, misfire_grace_time=3600)
```

（`CronTrigger(day_of_week=None, ...)` 与不传等价——现有 9 个每日 job 行为不变。）注册一行：

```python
cron(_committee_job, "committee", settings.committee_time, day_of_week="fri")
```

### 1.4 vault export（可选，后续卡）

`workflow.completed` 且 `workflow_id == "committee"` 时导出《委员会裁决.md》到 vault——按 CLAUDE.md Recipes 的 exporter 模式，另开卡做。

## 2. `${WEEK_DISPUTES}` 口径（给后续 prompt/审计卡）

- **数据源**：`whiteboard_boards`（status='completed'，`updated_at >= UTC now−7d`）+ 每板最高 idx 的 completed 卡 summary（收尾卡）。`stopped`/`failed`/`active` 板不入选；卡全失败的板列出但标注「（无收尾摘要）」。
- **格式**：`- 「{topic}」（{work_date} 研讨）：{收尾摘要单行化}`，倒序（最新在前），最多 50 板，UTF-8 ≤3072 字节截断加 `…`。
- **降级**：无板/查询异常 → 空串；01-agenda prompt 自带「材料为空」分支。
- **审计**：实际注入文本在 `workflow_runs.variables["WEEK_DISPUTES"]`；写入是 `json_set` 单键（并发写者落的其他键不会被覆盖，有回归测试）。

## 3. API 契约（给前端）

`POST /api/multi-agent/run` —— 同步等待但有总墙钟预算（REVIEW-C5 M1）：

```jsonc
// request
{
  "agents": ["macro-analyst", "equity-analyst"],  // 必填，1–5 个 catalog 里的 analyst id
  "prompt": "一句话表态……",                        // 必填，非空白
  "mode": "all",              // 可选，all|first_success|majority_vote|best_effort，默认 all
  "hand": null,               // 可选，覆盖所有 agent 的 hand；null=各自 analyst.hand 或 default_hand
  "timeout_s": null,          // 可选，单 task 执行超时；null=1800；须在 (0, 3600]
  "wait_s": 900               // 可选，请求总墙钟预算（秒）；默认 900，须在 (0, 1800]
}
// 200：预算内全部任务到达终态 —— join 结果 + agent 标注
{
  "mode": "all", "ok": true,
  "output": null,             // 见 §4；majority_vote 获胜时为文本
  "votes": 2,                 // 仅 majority_vote 模式存在
  "outputs": [                // 与请求 agents 同序
    {"task_id": "…", "agent": "macro-analyst", "status": "completed", "output": "…", "error": null}
  ]
}
// 202：预算先耗尽 —— 任务**不取消**，照常跑完入库（与 ask/stream 断开语义一致）
{
  "detail": "wait budget of 900s elapsed; tasks keep running",
  "mode": "all",
  "agents": ["macro-analyst", "equity-analyst"],
  "task_ids": ["ab12…", "cd34…"]   // agents 同序；事后 GET /api/tasks/{id} 轮询，终态后可自行按 §4 语义合并
}
// 400：agents 空/>5/含未知 analyst（detail 列出）/ prompt 空白 / mode 未知
//      / timeout_s ∉ (0,3600] / wait_s ∉ (0,1800]
// 422：JSON 形状/类型错误（FastAPI pydantic 层，先于业务校验）
```

每次调用产生 N 行 `tasks`（source='multi_agent'），无 session、无整组取消——这是原语不是 workflow；如需断线恢复/SSE/取消的完整分组语义，另开卡做持久化 group/run。真实并发受 executor 全局信号量（3）与 per-hand 互斥约束：同 hand 串行、跨 hand 真并行，前端选 agent 分散 hand 收益最大。

## 4. join 四模式语义（domain 契约）

| mode | ok 条件 | output | 备注 |
|---|---|---|---|
| `all` | 全部 task completed | `null`（看 outputs） | 一票否决 |
| `first_success` | ≥1 completed | fan-out 顺序第一个成功者的输出 | "first"=提交顺序非完成时刻 |
| `majority_vote` | 某 ballot 严格过半（> len(tasks)/2，失败任务算分母） | 获胜文本 | strip 后完全一致才同票；仅适用受约束输出（单词裁决/规范 JSON/枚举）；平票必然不过半 → ok=false；附 `votes`=最高票数 |
| `best_effort` | ≥1 completed | `null` | 从不因个别失败拒收 |

异常边界（REVIEW-C5 P2）：hand/模型失败永不外抛（executor 归一成行状态，join 按 not-completed 计）；spawn 层基础设施异常（task 行插不进等）从 `spawn_fan_out` 传播，此时已 spawn 的 agent 照常跑完（无回滚）；wait 层 driver 异常被吸收并 log，行状态是唯一真相。

## 5. 其他分区外事项

- 无新 migration（周认领复用 `admin_state`，0011/B1 同款 idiom）、无新必需 config（committee_time 见 §1.2 裁决点）。
- `tasks.source` 无 CHECK 约束，'multi_agent' 直接可写。
- REVIEW-C5 P2「文件链是 prompt 约定非引擎契约」未在本卡处理：两个修法（引擎判失败 / 2–4 步 prompt 补降级说明）都超出本卡授权（`_drive` 主循环与既有 prompt 措辞），建议主代理开 prompt 卡跟进。
- 测试基线：本卡两文件 18 + 11 = 29 测试全绿；`pytest tests/test_committee.py tests/test_multi_agent.py tests/test_workflows.py` 36 passed；全量见交付报告。
