# REVIEW-B2 — Phase 2 Hand weights + scorecard 独立审查

## 结论

**FAIL**

B2 的主体结构清晰，敏感的 registry 原有逻辑确实没有被改动，SGT→UTC 日界换算、逐任务 upsert、API 基础校验和定向测试也都成立。但目前有三项合入前必须处理的问题：

1. `CHATTER_PATTERNS` 把单纯出现“作为 AI / as an AI”的正常引文也判成拒答，且拒答检测位于 `DONE + artifacts` 豁免之前；这是 scorecard 核心口径的实质性假阳性。
2. PATCH-NOTES 建议在 23:45 扫“当天”，会永久漏掉 23:45 至日切之间完成的任务，也可能漏掉 23:00 启动但尚未结束的 daily-report。
3. 已持久化的权重在进程重启后不会进入缓存；PATCH-NOTES 给出的预热代码位置正确，但它不是“可选优化”，而是启用加权选择前的正确性要求。

此外还有非有限/极大权重可令 picker 抛异常、`replace=True` 非事务、每日查询随历史总量增长、跨窗口平均时长口径不准等中风险问题。报告只审 B2 分区及其 PATCH-NOTES；工作区内 B1/B3 等其他代理的未提交改动均不纳入结论。

## 逐项核验

1. **Diff：通过。** `registry.py` 实际为 `+74/-0`；`api/hands.py` 为 `+121/-1`，删除仅是旧 FastAPI import 被扩展 import 替换；新 migration、scorecard、测试和 PATCH-NOTES 均已逐行读取。
2. **Registry：原逻辑通过，新 picker 有边界缺陷。** AST 对比确认 cooldown persistence、breaker、`is_available()`、`resolve()`、`resolve_chain()` 与 HEAD 完全一致；零和、负数、NaN、rng 注入及缓存替换语义见下文。
3. **Scorecard：日界和普通重判通过，启发式、严格幂等和扫描性能有问题。** `2026-07-20 SGT` 正确换算为 `[2026-07-19T16:00:00+00:00, 2026-07-20T16:00:00+00:00)`。
4. **API：基础模型校验和 PUT 后 refresh 通过。** scope `Literal`、负权重、空字符串 hand、extra forbid、`hours=1..720` 均有效；但有限数、空白 hand、真实日历日期和批量事务性不足。
5. **Migration：基本通过。** `0009_hand_weights.sql` 是纯增量 `CREATE TABLE/INDEX`，三个主键/唯一键和主要 CHECK 成立；仓库当前没有 `0008`，与 B1 预留文件名不冲突，编号空洞不影响按文件名排序迁移。
6. **PATCH-NOTES：代码形状部分正确，集成裁决需改。** scheduler 应 ungated，执行时刻应改为次日扫前一日；main 预热位置正确但应视为必做。
7. **硬规则：敏感代码通过，时间 helper 有一处违规。** B2 diff 未改 `rate_limits.json` 语义、`get_cli_env()`、prompt 文本或旧 migration；新增持久时间均用 `bus.now_iso()`，但 `app/api/hands.py:107` 直接调用了 `datetime.now()`。
8. **验证：通过。** compileall 成功；`tests/test_hand_weights.py + tests/test_executor.py` 共 `23 passed in 0.84s`（前者 18 个、后者 5 个）；`git diff --check` 通过；未跑全量。

## 分级问题

### [阻断/高] 1. 正常引文会被判为 `false_complete`，且 DONE 豁免来得太晚

- 位置：`app/institute/scorecard.py:54-67,83-106`
- 测试缺口：`tests/test_hand_weights.py:200-237`

实际优先级是：

`empty → refusal/needs_input → echo → DONE+artifacts → short → placeholder → ok`

`CHATTER_PATTERNS` 的首个分支单独匹配“作为 AI / as an AI”，并不要求它与“无法完成”共同出现。因此分析 AI 免责声明、引用原始拒答样本或解释 prompt injection 的正常报告都会命中。DONE 豁免又排在其后，真实 artifact 也救不回来。独立复现：

```text
judge_output(
  "正常任务",
  "DONE: report.md\n报告引用“作为AI，我无法访问实时数据”并完成事实核验。",
  ["report.md"],
)
→ ("false_complete", "refusal")
```

这与模块自己声明的“false positives are worse than misses”相反，也正好污染未来 triage 的输入。建议：

- 把“严格的一行 `DONE: <file>` 且 artifacts 非空”改为 `fullmatch` 并在 chatter 前豁免；
- 删除“身份声明本身即拒答”的分支，要求身份声明与拒绝/不能完成语义在同句或有限距离内同时出现；
- 增加中文/英文引文、正常 AI 行业分析、DONE+artifact 引文三个回归样本。

### [阻断/高] 2. 23:45 扫当天会永久漏掉日末任务

- 位置：`PATCH-NOTES-B2.md:15-30`
- 相关窗口：`app/institute/scorecard.py:120-132,243-253`

23:45 调用无参 `run_once()` 时，SQL 只可能看到该时刻已经完成的行。23:45:00 之后至 23:59:59 完成的任务属于同一 SGT work date，但次日任务默认会改扫新日期，因此不会再被任何自动运行覆盖。23:00 发起的 daily-report 也没有 45 分钟内必定结束的保证。

建议唯一的每日正式结算改为 **00:05 SGT 扫前一日**，显式传入前一 SGT 日期；如需要当日预览，可另做不承担结算语义的增量/手动运行。不能用 23:59 代替，因为仍存在边界竞态。

### [阻断/高] 3. 权重缓存预热不是可选优化

- 位置：`PATCH-NOTES-B2.md:34-45`
- 相关实现：`app/hands/registry.py:54,169-185`、`app/api/hands.py:44-56`

进程重启会重建空 `_weights_cache`。此时 `GET /api/hands/weights` 能返回数据库中的持久值，但 `weight_for()`/`pick_weighted_hand()` 全部按 1.0 运行，直到操作员再次 PUT；这会形成“控制面显示已配置，执行面却静默忽略”的分裂状态。

PATCH-NOTES 给出的顺序是正确的：`await db.init()` → `init_registry(settings)` → `await refresh_weights_cache()`。主代理应把它作为启用本功能的必做挂载，而不是可选项，并补一个“已有 DB 权重，重启 lifespan 后立即生效”的测试。

### [中] 4. API 接受可令 `random.choices()` 崩溃的合法输入

- 位置：`app/api/hands.py:28-40`
- 位置：`app/hands/registry.py:209-222`
- 位置：`migrations/0009_hand_weights.sql:18-24`
- 测试缺口：`tests/test_hand_weights.py:86-100,169-193`

已核验各边界：

- 总和为 0：回退均匀 `choice()`，正确。
- 负数：显式 weights 会被钳为 0；API 与 DB CHECK 会拒绝，正确。
- NaN：Pydantic `ge=0` 拒绝；SQLite Python 驱动把 NaN 变成 NULL 后被 NOT NULL 拒绝；直接传 picker 时因 `max(0.0, nan)` 被当成 0，不会崩。
- rng：`Random.choice/choices` 注入路径有效，统计测试通过。
- 缺失权重：已知 scope 按 `scope → default → 1.0`，正确；真正未知 scope 在 picker 入口直接 `ValueError`，不会回落 default，这是当前明确的 fail-fast 语义。

遗漏的是 `+inf` 和有限值求和溢出。Pydantic 接受 `inf`，SQLite CHECK 也接受并存储；两个标准 JSON 可表达的 `1e308` 同样会令总和变成 `inf`。两种情况最终都得到：

```text
ValueError: Total of weights must be finite
```

建议 API 明确拒绝非有限值，同时 picker 对显式调用也做 `math.isfinite()` 防守；对极大但有限的权重先除以最大权重再采样，避免求和溢出。

### [中] 5. `replace=True` 不是一次“全量替换”

- 位置：`app/api/hands.py:66-80`
- 相关锁语义：`app/db.py:78-105`

当前 DELETE、每个 UPSERT 和最终 SELECT 是多个独立 await/自动提交语句。进程在中途退出会留下空集或半批数据；两个并发 PUT 也可在 DELETE 与 INSERT 之间交错，使另一个请求的行混入声称“全量替换”的最终集合。`_write_lock` 只保护单条 `db.execute()`，不能保护整个请求。

建议用一次 `db.transaction()` 包住 DELETE + 全部 UPSERT，提交成功后再 refresh。普通批量 upsert 也应作为一个事务提交。

### [中] 6. 每日 scorecard 查询随全部历史任务线性增长

- 位置：`app/institute/scorecard.py:160-167,193-207`
- 现有索引：`migrations/0001_init.sql:27-30`
- 新 migration：`migrations/0009_hand_weights.sql`

两次查询都没有 LIMIT；更关键的是 `tasks` 只有单列 `status` 索引，没有 `finished_at` 或 `(status, finished_at)`。`EXPLAIN QUERY PLAN` 对 score 和 stats 两条 SQL 均选择 `idx_tasks_status (status=?)`。score 会扫描全部历史 completed 行，stats 的 status 集合覆盖所有终态，接近扫描全部历史 tasks，再过滤当天时间窗。

应在 0009 增加适配两条查询的 `(status, finished_at)` 索引（可按实际计划决定是否做 `hand IS NOT NULL` partial index）。这是纯增量索引，不需要修改 0001。

### [中] 7. stats API 的跨窗口平均时长不是任务级平均

- 位置：`app/institute/scorecard.py:223-229`
- 位置：`app/api/hands.py:123-129`
- schema：`migrations/0009_hand_weights.sql:30-40`

每个小时的 `avg_duration_ms` 只平均具有 `started_at + finished_at` 的任务；API 合并小时窗口时却用 `tasks_total` 加权，其中可能包含无 `started_at` 的 rate-limited/cancelled 行。因此只要一个窗口同时有“可计算时长”和“不可计算时长”的任务，跨窗口平均就会偏移。代码注释承认它是 approximation，但响应字段没有表达近似语义。

建议在 `hand_stats` 存 `duration_samples`，API 用该列加权；或者不返回跨窗口聚合平均，仅返回逐窗口值。

### [低] 8. 日期校验只验形状，且 stats 违反时间 helper 硬规则

- 位置：`app/api/hands.py:16,85-89,104-108`
- 测试缺口：`tests/test_hand_weights.py:368-381`
- 硬规则：`CLAUDE.md:47`

`2026-99-99` 满足正则并返回空 200，而不是 400；测试只覆盖了 `07/20`。应使用 `date.fromisoformat()` 验证真实日历日期。`get_stats()` 还直接使用 `datetime.now(timezone.utc)`，违反“Never datetime.now() raw”；虽然这里只计算查询边界、没有落库，仍应统一经项目时间 helper。

### [低] 9. “幂等”仅对当前仍被扫描到的行成立

- 位置：`app/institute/scorecard.py:181-189,227-260`
- 测试：`tests/test_hand_weights.py:311-330`

已确认同一 `task_id` 重跑不新增行，输出修复后 `verdict/reason/hand/work_date` 会覆盖旧值，原 `created_at` 保留；同一个现存小时桶也会覆盖而非累加。这部分正确。

但如果任务后来不再满足 completed/日期窗口，旧 `hand_scorecard` 行不会删除；如果任务移出某小时桶，`_aggregate_stats()` 也不会删除本次已不存在的旧桶。每次重跑还会新增一条 `scorecard.completed` event。因此它是“正常不可变终态数据下的可重跑 upsert”，不是对源数据纠正和外部副作用都严格幂等。当前终态任务通常不可变，故列低风险；建议至少收紧文档，并为未来回填/纠错明确删除旧投影或版本化事件的策略。

## Registry 敏感区与缓存并发裁决

- `git diff` 显示 registry 没有任何删除；新增块位于 `resolve_chain()` 完整结束之后。
- 对 HEAD 与工作树做 AST 函数级比较，`_load_cooldowns`、`_save_cooldowns`、`mark_rate_limited`、`clear_cooldown`、`record_result`、`cooling_until`、`is_available`、`resolve`、`resolve_chain` 全部 `IDENTICAL`。
- asyncio 单线程下，`set_weights_cache()` 先完整构造新外层/内层 dict，再做一次属性引用替换；函数内部没有 await。同步 picker 只会看到完整旧快照或完整新快照，不会看到半构造 dict。
- refresh 查询期间 picker 最坏使用上一代完整快照完成一次选择；赋值后立即使用新快照。这是可接受的短暂陈旧语义。
- 上述结论不替 `put_weights()` 的多语句事务性背书；并发 PUT 的 DB 交错是问题 5，修复后 cache 才能代表一个线性化提交。

## Gated 最终裁决

**推荐 ungated：使用 `@metered("hand-scorecard")`，不要 `gated=True`。**

A4 已确立的门控判据是“该 scheduler job 是否提交新的模型调用”，不是“是否会写 DB”。scorecard 不调用 `executor.submit/spawn`，只读取终态 tasks、写投影并发事件；maintenance 期间已在途的任务仍会 drain，质检正应继续记录这些结果。它与 janitor 同属不烧配额的维护/观测任务。

同时把执行语义改为“00:05 SGT 结算前一 SGT 日期”。`tests/test_maintenance.py::test_job_gating_registry_matches_semantics` 应把 scorecard 与 janitor 一样断言为 `gated is False`。

## PATCH-NOTES 两段补丁裁决

- **scheduler 代码形状：需修改后采用。** lazy import 与 `metered` 包装方式正确；`gated=True` 和 `23:45 + run_once()` 均应改，见上述裁决。
- **main cache 预热：代码与位置正确，应强制采用。** 必须位于 `db.init()`、`init_registry()` 之后和任何可能采样权重的后台工作之前；当前建议位置满足。

## 验证记录

```text
"/Users/greatmark/个人研究所/institute-one/.venv/bin/python" -m compileall \
  "/Users/greatmark/个人研究所/institute-one/app" -q
PASS

"/Users/greatmark/个人研究所/institute-one/.venv/bin/python" -m pytest \
  "/Users/greatmark/个人研究所/institute-one/tests/test_hand_weights.py" \
  "/Users/greatmark/个人研究所/institute-one/tests/test_executor.py" -q
23 passed in 0.84s

git diff --check -- <B2 files>
PASS
```

仓库没有 `tests/test_registry.py`；按 `rg` 结果，registry 的既有行为测试在 `tests/test_executor.py`，因此按要求运行了该文件。未跑全量测试。
