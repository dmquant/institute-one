# R1 独立审查：A1 / Phase 0 Hardening

## 结论

**FAIL**

定向编译与 15 个测试全部通过，但存在 3 个 must-fix：停机 drain 未覆盖全部既有后台任务、retry 丢失执行 fallback 策略并可违反 research hand 硬规则、0005 对旧行的回填不符合本次“旧 `NULL` 不计入今日 cap”的验收口径。

## 审查范围

- 已审 tracked diff：`app/main.py`、`app/router/executor.py`、`app/institute/research.py`、`app/api/tasks.py`、`tests/test_research.py`。
- 从 `git status --short` 识别并通读的 A1 新文件：`migrations/0005_research_hardening.sql`、`tests/test_executor_output.py`、`tests/test_executor_shutdown.py`、`tests/test_tasks_retry.py`。
- 状态中其余修改/未跟踪文件按要求视为其他在途代理工作，未纳入结论。

## 五项逐项核验

- **a. PASS** — `app/main.py:64-67` 严格在 `executor.recover_orphans()` 后调用 `research.recover_orphans()`；`app/institute/research.py:75-77` 只把 `status='running'` 改回 `pending` 并清空 `started_at`，且启动阶段早于 scheduler 启动和 lifespan 放行，不存在本进程新认领竞态。
- **b. FAIL** — `app/main.py:38-45` 会 cancel 并以 15 秒上限等待列出的 executor/workflow/whiteboard/mailbox 任务，`db.close()` 顺序也在其后；`app/hands/base.py:197-204` 确认取消会 `SIGKILL` 整个进程组，但 drain 漏掉既有后台任务，详见 M1。
- **c. PASS-WITH-NIT** — `app/router/executor.py:82-96` 按 UTF-8 字节截断，`errors="ignore"` 不会留下半个码点，并追加 `…[truncated]`；极小 cap 会突破字节上限，详见 N1。
- **d. PASS（核心语义）/FAIL（执行策略）** — `app/api/tasks.py:54-66` 查询旧行后调用 `spawn()` 创建新 id，旧行不变，且精确要求 `status == 'failed'`，所以 running/completed 均返回 409；但未保留 fallback 策略，可违反 research 只能走 codex+agy 的硬规则，详见 M2。
- **e. FAIL（按验收口径）** — 0005 是新编号增量文件、运行时查询 `work_date = ?` 可安全忽略 `NULL`，新写入也使用 `prompts.work_date()`；但 `migrations/0005_research_hardening.sql:20-22` 主动回填全部旧 `NULL`，使同一 SGT 日的旧行计入今日 cap，详见 M3。

## Must-fix

### M1. 停机 drain 未覆盖全部后台任务，仍可能与 `db.close()` 竞态

- `app/main.py:36-45` 只收集四组任务，遗漏了仓库原本就存在的 `app/institute/analyst_daily.py:36,250-260` 的 `_background` 注册表。
- `app/api/research.py:47-51` 的 `asyncio.shield(research.tick())` 还会创建未登记的独立 Task；客户端断开后它可继续运行。
- `app/institute/scheduler.py:223-227` 使用 APScheduler `shutdown(wait=False)`；当前安装版本只 cancel 内部 future，不能 await。`app/main.py:83-87` 随后只等待自己的快照，任务若正处于两个 executor 调用之间，可能在快照后继续访问 DB，甚至再创建未被取消的新 executor 任务。
- `asyncio.wait()` 的 done 集合也被丢弃；取消清理若异常，停机路径不会同步记录该异常。

影响：P1 所要求的“取消并等待全部后台任务，再关 DB”没有成立，仍可能出现关闭连接后的写入、状态残留，极端窗口下也不能排除新 CLI 任务漏过本次取消。

### M2. retry 丢失原任务执行策略，research 重试可跑到禁止的 hand

- `app/api/tasks.py:59-65` 调用 `executor.spawn()` 时未传 `fallback` / `fallback_chain`。
- `app/router/executor.py:157-161` 因而走默认 registry fallback；`app/hands/registry.py:25-29` 中 codex 默认首先 fallback 到 claude，而不是 research 专用的 agy。
- 原 research 路径在 `app/institute/workflows.py:153-156,194-201` 明确传入 `settings.research_hand_names`；CLAUDE.md 硬规则要求 research 始终限制在 codex+agy。

影响：重试一个 `source='research'` 的失败任务可能静默改用 claude/gemini，违反执行隔离和配额策略；原本 `fallback=False` 的任务也会被重试成允许 fallback。应持久化原执行策略，或至少按 `source` 重新应用 research policy。

### M3. 0005 回填与“旧 NULL 不计入今日 cap”的验收口径冲突

- `migrations/0005_research_hardening.sql:20-22` 把所有旧行的 `work_date` 从 `completed_at` 回填。
- 若部署当天已有旧格式完成记录，它会被转换为今日 SGT 日期并进入 `app/institute/research.py:101-104` 的 cap 计数；旧行不再是应被等值比较自然排除的 `NULL`。
- 现有测试只验证新写入和显式 work_date，没有覆盖“旧行保持 NULL 且不计入今日”的迁移语义。

按本次明确验收口径，应移除该回填，或先明确修改口径并补一条真实迁移测试。

## Nice-to-have

- **N1 字节 cap 极小值**：`app/router/executor.py:94-96` 在 `cap_bytes < len(TRUNCATION_MARKER.encode())` 时仍返回完整 marker，例如 cap=1 会得到约 15 字节。可校验最小配置，或定义极小 cap 下的 marker 降级策略。
- **N2 retry 并发幂等**：`app/api/tasks.py:54-65` 的“读 failed → spawn”没有原子幂等保护；两个并发请求会各自创建并执行一个新任务。若产品允许多次人工重试，应明确记录；否则需 retry lineage / 唯一约束或条件认领。
- **N3 测试缺口**：`tests/test_tasks_retry.py` 直接调用 handler，未走实际 HTTP 路由，且只实测 completed 拒绝、未实测 running；shutdown 测试未覆盖 analyst-daily、shielded research、scheduler 任务及 timeout-alive 分支；0005 未做旧库迁移测试。

## CLAUDE.md 硬规则核验

- 条件认领：**PASS**；research 的 claim/完成更新均保留状态条件，恢复 UPDATE 也限定 `status='running'`。retry 不修改旧状态，但存在 N2 的重复执行窗口。
- 调度任务不 raise：**PASS**；`research.tick()` 继续兜底异常，新 recovery 是启动钩子而非调度任务。
- 时间戳 / 工作日：**PASS**；新存储时间仍走 `bus.now_iso()`，今日口径走 `prompts.work_date()`。
- prompts 字符串：**PASS**；本分区未修改 `prompts.py` 或 workflow prompt。
- migration 纪律：**PASS**；新增 0005，未修改旧 migration；M3 是数据语义问题。
- research hand 隔离：**FAIL**；retry 路径存在 M2。

## 验证摘要

- `.venv/bin/python -m compileall app -q`：退出码 0，无输出。
- `.venv/bin/python -m pytest tests/test_research.py tests/test_tasks_retry.py tests/test_executor_output.py tests/test_executor_shutdown.py -q`：`15 passed in 0.55s`。
- 按要求未运行全量 pytest。
