# REVIEW-C5 — Phase 7 委员会与多代理原语独立审查

审查日期：2026-07-20  
审查范围：`workflows/committee.json`、`app/institute/workflows.py` 的 `${WEEK_DISPUTES}` 增量、`app/institute/multi_agent.py`、`app/api/multi_agent.py`、`tests/test_committee.py`、`tests/test_multi_agent.py`、`PATCH-NOTES-C5.md`  
结论：**FAIL**

## 判定摘要

委员会五步 prompt、analyst roster、标准 prompt 三明治、近七天白板投影、四种 join 的正常终态语义和唯一模型执行路径基本成立，指定的编译与 28 条定向测试也全部通过。但合入/挂载前仍有三个必须修复的运行语义问题：

1. 同步 multi-agent API 没有总墙钟上限；五个同 hand 的默认请求在没有外部排队和 fallback 的理想情况下也可持续约 152.5 分钟，实际排队时间无上界。
2. PATCH-NOTES 的 committee scheduler job 无持久化按周/按工作日条件认领；`max_instances=1` 不能提供幂等，多次触发会重复创建五步工作流并重复消耗模型额度。
3. PATCH-NOTES 的 weekday 注册片段只在合法非空时间下可用，却错误声称沿用了空串/解析失败禁用语义；照抄后 `committee_time=""` 或格式错误会让 scheduler/app 启动失败。

此外，`${WEEK_DISPUTES}` 的 `status='running'` 只防止终态后写入，不是 variables 的 CAS：当前单 driver 下没有现行冲突写者，但若并发写同一 run，会发生整份 JSON 丢失更新。

## 一、委员会 prompt 质量

### 通过项

- 五个 analyst id 均存在于 `catalog/analysts.json`：`chief-strategist`、`macro-analyst`、`equity-analyst`、`policy-analyst`、`ops-editor`，对应 `catalog/analysts.json:4-12,15-23,26-34,37-45,103-111`。
- 五步均声明固定 `output_file` 与 1800 秒 timeout：`workflows/committee.json:8-45`。
- `${WEEK_DISPUTES}` 只出现在议程步骤，后四步没有重复注入：`workflows/committee.json:5,11`。
- 实际运行时每步都经 `build_analyst_prompt()`，三明治顺序是时间锚 → persona/memory → 前序摘要 → task → `CITATION_MANDATE` → 文件交付：`app/institute/workflows.py:300-325`、`app/institute/prompts.py:39-90`。因此五步即使没有在 JSON 中逐字重复引用规范，也都获得统一的来源、时间点、未经核实和禁止编造要求。
- 步骤衔接与引擎能力匹配：引擎把各步放进同一个 session workspace，并把前序 `## 核心结论` 摘要注入 `previous_steps_block`；JSON 同时要求后续步骤读取前序完整文件，避免只靠 800 字摘要：`app/institute/workflows.py:263,292-340`、`app/institute/prompts.py:51-63`、`workflows/committee.json:19,27,35,43`。
- 辩论 prompt 对立场、置信度、证伪条件、事实/判断分离、dissent 保留均有明确要求；编辑被禁止新增事实或替辩手补票，整体产品质量良好。

### P2 — 裁决的“严格多数”措辞存在逻辑歧义

位置：`workflows/committee.json:43`

“若无法形成多数（三人各持一端或有人表态含糊）”有两个问题：

- 正反二元命题下，“三人各持一端”不是可判定状态；
- 一人含糊、另外两人明确同边时，仍已有 2/3 多数，现文却可能让编辑一律写“未达成裁决”。

建议明确为：“任意两名及以上辩手明确投同一方向即形成多数；含糊表态不得补票，但仍计入三人分母；没有方向取得至少两票时才写未达成裁决。”

### P2 — 文件链是 prompt 约定，不是引擎强制契约

位置：`app/institute/workflows.py:329-340`、`workflows/committee.json:19,27,35,43`

`executor.Task.status == "completed"` 但模型未写声明文件时，引擎不会把该步判失败，而是从 task 文本提取摘要后继续。最终编辑步骤知道要标注缺失，但第 2–4 步只要求“完整阅读文件、不要只依赖摘要”，没有说明文件缺失时如何降级。

建议二选一：

- 在工作流引擎层把“声明了 output_file 但 completed 后文件不存在”判为步骤失败；或
- 仅对 committee 的第 2–4 步补上“文件缺失时明确标注，并使用前序摘要继续，不得臆造”的降级说明。

## 二、`${WEEK_DISPUTES}` 核验

### 时间、筛选与截断：PASS

- 白板完成时以 `bus.now_iso()` 写入 UTC 秒级 `updated_at`：`app/institute/whiteboard.py:859-863`；cutoff 使用同样的 UTC `+00:00` 秒级 ISO 格式，SQLite 文本比较在当前规范化写路径下成立：`app/bus.py:28-29`、`app/institute/workflows.py:140-153`。
- 边界为 `updated_at >= now_utc - 7 days`，仅选 `b.status='completed'`，按更新时间倒序，SQL 先限制 50 板：`app/institute/workflows.py:141-154`。
- 每板取最高 idx 且已完成、摘要非空的卡；没有合格摘要时保留白板并输出“无收尾摘要”：`app/institute/workflows.py:145-159`。
- `_truncate_utf8()` 预留三字节省略号后，以 `errors="ignore"` 丢弃被切断的尾部码点，结果不超过 3072 UTF-8 bytes：`app/institute/workflows.py:122-127,159`。
- 空结果自然返回空串，查询/渲染异常被捕获后也返回空串：`app/institute/workflows.py:155-162`。

### P2 — `status='running'` 不是 variables 的并发写保护

位置：`app/institute/workflows.py:264-290`

代码先把整份 `variables` JSON 读入本地 dict，再用：

`UPDATE workflow_runs SET variables=? WHERE id=? AND status='running'`

整份替换。这个 WHERE 只能保证取消/结束后不再落库；它不比较旧 variables，也不合并数据库中的并发新值。若另一个 driver/未来的运行中编辑器在计算期间写入键，后到的 C5 更新会用旧快照覆盖它。

当前仓库中 variables 的生产写点只有同一 `_drive` 内顺序执行的 `DATA_BUNDLE` 与 `WEEK_DISPUTES`，所以正常单 driver 路径不会触发丢失更新；PATCH-NOTES 把它描述成“条件守卫”可以，但不能把它理解成并发安全。建议用原子 `json_set` 更新单键，或对旧 variables 做 CAS 并在冲突后重新读取合并。应补一个并发写入不丢其他键的回归测试。

### 测试缺口

现有测试覆盖近期/过期/非完成板、最高 idx 摘要、空数据、3 KiB cap、显式值优先、端到端注入和未引用不计算；未覆盖 50 板上限、倒序、查询异常降级、恰好七天边界及上述并发丢失更新。

## 三、fan_out / join 语义

### 正常终态语义：PASS

- 每个 analyst 都经 persona 三明治后调用一次 `executor.submit()`，没有模型旁路：`app/institute/multi_agent.py:30-70`。
- `asyncio.gather` 保留输入顺序，因此返回 task 与 agents 同序：`app/institute/multi_agent.py:61-70`。
- `all`：只要一个 task 不是 `completed` 就 `ok=false`；失败详情仍在 outputs：`app/institute/multi_agent.py:98-106`。
- `first_success`：取 fan-out 输入顺序中第一个 `completed`，不是墙钟最先完成者；文档已明确，而且它仍会等待全部 agent：`app/institute/multi_agent.py:79-81,102-110`。
- `majority_vote`：仅对 completed task 的 strip 后完整文本计票，但门槛用全部 tasks 作分母，`votes * 2 > len(tasks)` 正确实现严格过半，失败任务会压低 quorum：`app/institute/multi_agent.py:111-124`。文档也正确披露自由文本几乎无法逐字一致的限制。
- `best_effort`：至少一个 completed 即 `ok=true`，所有可用和失败投影均留在 outputs：`app/institute/multi_agent.py:89-94,125-128`。
- `timeout_s` 原样传给每次 `executor.submit`：`app/institute/multi_agent.py:61-68`。

### P2 — gather 不处理 submit 层异常，部分 fan-out 会变成 500

位置：`app/institute/multi_agent.py:61-70`、`app/router/executor.py:149-205,250-281`

通常的模型/hand 崩溃由 executor 捕获并归一成 `Task(status="failed")`，所以 `all` 会正常返回 `ok=false`。但 `gather` 没有 `return_exceptions=True`；DB、workspace、registry 或取消等 submit 层异常会直接传播，使 API 在 join 前返回 500。Python `gather` 在首个异常后也不会自动取消其他 awaitable，因此兄弟任务可能继续执行，调用方却拿不到它们的 task id。

额外只读探针复现为：`fan_out_exception RuntimeError submit-layer boom`，同时兄弟提交随后仍完成。不能只把 `return_exceptions=True` 打开，因为 `join` 只接受 `Task`；应由每-agent wrapper 明确映射基础设施异常，或在持久化 group/run 层记录部分提交并允许恢复。至少应把该边界写进契约并补测试。

## 四、API

### MUST-FIX-1 / P1 — 同步 API 没有端到端超时

位置：`app/api/multi_agent.py:33-62`、`app/router/executor.py:180-193`

API 直接等待 `fan_out()` 全部结束，没有总 `asyncio.timeout`，也没有持久化 multi-agent run id。executor 的 `timeout_s` 只在取得全局 semaphore 和 per-hand lock 后包住单次 `hand.execute()`；排队时间不计入。

默认 `timeout_s=1800` 时：

- 五个 agent 落到同一 hand，执行被 per-hand lock 串行；不计外部排队和 fallback，最坏约 `5 × (1800+30) = 9150s`，即 152.5 分钟；
- 即使分散 hand，也受全局并发 3 限制，五个任务至少可能分两波，约 61 分钟；
- 已有任务占住 semaphore/hand 时，队列等待没有上限；
- `first_success` 也不会提前返回，因为 join 发生在 gather 全部完成之后；
- API 还接受任意大的正 `timeout_s`，进一步放大问题。

建议优先改为异步 group/run：提交后返回 202 + durable run id，提供查询/SSE 与取消；无需把它伪装成 session，但必须有一条可恢复的分组记录。如果暂时保留同步接口，至少要给 `timeout_s` 设置合理上限、增加明确总墙钟预算，并设计好总超时后的 task 状态收敛；直接取消正在等 semaphore 的 `_execute` 目前可能留下 queued row，不能只外包一层 `wait_for`。

### 校验与无 session 设计

- 1–5 agent cap、未知 analyst、空白 prompt、未知 mode、非正 timeout 都是明确 400，测试覆盖正常：`app/api/multi_agent.py:37-55`。
- Pydantic schema/type错误在进入 handler 前仍是 FastAPI 422，不是模块 docstring 所称的“所有 request-shape 问题都是 400”：`app/api/multi_agent.py:1-6,25-30`。探针提交 `{}` 得到 422。建议修正文档，或安装统一 validation handler；无需强行把标准 422 改成 400。
- `timeout_s` 没有上限，prompt 也没有长度上限；至少 timeout 应进入请求模型的区间约束。
- 作为短时、一次性 primitive，无 session 可以接受：每个 task 自带审计行，响应也返回 task id。但结合长同步等待，它没有 group id、断线恢复和整组取消能力，因而不适合当前 30 分钟默认值的 HTTP 暴露。

## 五、PATCH-NOTES-C5 核对

### Router 挂载建议：可用，但当前尚未落地

`app/main.py:152-184` 当前没有导入或 include `api.multi_agent.router`，所以生产 app 中该 endpoint 仍是 404；测试使用裸 FastAPI 手工挂载，不能证明生产接线。PATCH-NOTES 所述“import 一行 + include_router 一行”符合现有结构，落地后可用。

### MUST-FIX-2 / P1 — committee job 没有 durable 条件认领

位置：`PATCH-NOTES-C5.md:25-43`、`app/institute/workflows.py:167-194`

建议的 `_committee_job()` 每次都直接 `run_workflow("committee")`。该函数创建新 session/run 后立即把 driver 放到后台并返回；`max_instances=1` 只约束短暂的 scheduler wrapper，不覆盖五步 workflow，更不防手工重复触发、多进程或同日重入。每次触发都会新建一套五步模型任务。

这不满足 ROADMAP 的 “idempotent advance”（`ROADMAP.md:165`），也不满足状态循环的条件认领硬规则。挂载前应增加以 committee work date/week 为键的持久化 claim（单条 `INSERT ... ON CONFLICT DO NOTHING` 或等价 CAS），并明确失败/取消后的重试与 stale claim 处理；仅先 SELECT 再 INSERT 也不是原子认领。

### MUST-FIX-3 / P1 — weekday 片段没有继承 helper 的禁用语义

位置：`PATCH-NOTES-C5.md:34-43`、`app/institute/scheduler.py:256-267`

现有 `cron(job, name, hhmm)` 确实只构造每日的 hour/minute trigger，不支持 weekday。`CronTrigger(day_of_week="fri", ...)` 与 `sched.add_job(..., trigger_object, ...)` 本身在合法时间下可用，`gated=True` 也正确。

但建议片段在 try/except 外直接执行 `settings.committee_time.split(":")` 和 `CronTrigger(...)`：

- 空串会在解包时抛 `ValueError`；
- 非法格式或越界 hour/minute 也会抛异常；
- 异常从 `start()` 逸出，导致应用 lifespan 启动失败。

因此 PATCH-NOTES 第 43 行“沿用空串禁用/解析失败禁用语义”不成立。建议把 helper 扩成 `cron(..., day_of_week: str | None = None)`，在原有 strip/try 内给 `CronTrigger` 追加 weekday；或新建 `weekly()` helper，但必须复用同一套空值与错误处理。

### P2 — 调度时间来源冲突

`PATCH-NOTES-C5.md:22,34-40` 与 README recipe（`README.md:206`）选择周五 20:00 SGT；ROADMAP 当前写的是 22:00 SGT on committee days（`ROADMAP.md:165`）。应由主代理先裁决产品源，再固化默认值；不能一边落 20:00，一边把 ROADMAP 保留为 22:00。

### NIT — 测试数量写反

实际收集为 `tests/test_committee.py` 11 条、`tests/test_multi_agent.py` 10 条；`PATCH-NOTES-C5.md:9` 写成 10 + 11。总数 21 正确。

## 六、硬规则与 Git 归因

- 模型调用只走 `executor.submit()`：PASS。
- C5 新增持久化时间：没有旁路写入；白板完成时间沿用 `bus.now_iso()`，七天 cutoff 是只读查询边界，不是持久化时间：PASS。
- `workflows.py` 相对 HEAD 的聚合 diff 还包含 A4 的 analyst key 归一化、B5 的 `${DATA_BUNDLE}`、B3 memory 注入和 C8 hand weights；这些不归因 C5。可归因的 C5 变更只有 datetime import、三个 cap、`_truncate_utf8()`、`week_disputes_variable()` 和 `_drive` 的 WEEK_DISPUTES 注入块，未改其余 step 循环或 prompt 常量。
- C5 注入块的取消守卫存在，但 variables 并发 CAS 不成立，见上文 P2。
- scheduler 条件认领未满足，见 MUST-FIX-2。

## 七、验证记录

- `.venv/bin/python -m compileall app -q`：PASS（exit 0）。
- `.venv/bin/python -m pytest tests/test_committee.py tests/test_multi_agent.py tests/test_workflows.py -q`：**28 passed in 1.11s**。
- 未运行全量测试，符合审查指令。
- 额外只读探针：
  - 缺失请求字段得到 HTTP 422；
  - submit 层异常从 `fan_out()` 传播，同时兄弟 awaitable 继续完成。
