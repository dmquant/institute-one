# ROUND1-AUDIT-F1 — 轮级终审（跨分区交互面 / 全局不变量）

- 审计代理：F1（深度代码审查视角，只读）；与 S1（must-fix 逐项核销 + 全链验证）并行、分工互补。
- 审计对象：第一轮 8 个实现代理（A1–A8）+ 主代理集成的全部未提交改动（29 个修改文件 + 6 个新文件，+1996/−118）。
- 方法：通读 `app/` 全部本轮触碰模块与其交互对端；`git diff` 逐行核硬规则面；/tmp 临时 INSTITUTE_HOME 空库/升级库两路迁移实测；echo-hand 并发探针实测；compileall + 定向 pytest（46+29 passed）。未跑全量 pytest（S1 负责）。

## 总结论：**PASS-WITH-NITS**

跨分区交互面没有发现新的 must-fix 级问题。七个审查重点全部通过或带 nit 通过；发现若干值得第二轮处理的组合缝隙（最重要的一条是既有代码的并发双跑缝隙，非本轮回归，但本轮 A3 的守卫加固并未覆盖它）。

---

## 逐重点结论

### 1. 停机 drain 全局图 — PASS

`rg asyncio.create_task` 全仓 8 处调用点逐一核对，全部落在 `main._drain_background` 的注册表并集里：

| create_task 调用点 | 注册表 | drain 覆盖 |
|---|---|---|
| `app/router/executor.py:269,295`（submit/spawn） | `executor._running` | ✅ |
| `app/institute/workflows.py:139` | `workflows._driving` | ✅ |
| `app/institute/whiteboard.py:47` | `whiteboard._bg_tasks` | ✅ |
| `app/institute/mailbox.py:34` | `mailbox._bg_tasks` | ✅ |
| `app/institute/analyst_daily.py:256,262` | `analyst_daily._background` | ✅ |
| `app/institute/research.py:43`（shielded_tick） | `research._bg_tasks` | ✅ |
| `app/institute/archive.py:42`（vector embed） | `archive._bg_tasks` | ✅ |
| APScheduler job 任务 | `extra`（shutdown 前快照，main.py:137） | ✅ |

组合面确认：
- 被 await 的子任务（如 `_drive` 里 await 的 `executor.submit` 内部 `ensure_future`）本身也在 `_running` 注册，同一轮被直接 cancel，不依赖 await 链传播——设计正确。
- 两轮清扫覆盖"round 1 快照后、cancel 生效前"新 spawn 的一代任务（如 `_run_card` 尾部 `_handoff` 的 submit）；cancel 注入后协程在下一个 await 点终止、不再继续 spawn，>2 代连环在现有代码里无实例。
- `research.shielded_tick` 的"shield 挡请求取消、注册表交 drain 取消"双语义（app/api/research.py:47-53）自洽；被 cancel 后 `_claim_lock` 经 async with 正常释放，残留的 running 行由 boot 时 `research.recover_orphans()` 闭环。
- 残余窗口（已在 PATCH-NOTES-A1 文档化、可接受）：`_scheduler_inflight` 依赖 APScheduler 私有 `_executors/_pending_futures`，版本漂移时退化为空集。

### 2. 迁移链 0001→0007 完整性 — PASS（附一个第二轮加固项）

实测两路（/tmp 临时 INSTITUTE_HOME，未触生产库）：

- **空库全链**：0001→0007 顺序应用干净；`integrity_check` ok、`foreign_key_check` 0 违规；`research_log.work_date` 列 + 索引、0006 六表、`vector_chunks` 全部就位；PIT 三个 UNIQUE 版本索引齐。
- **升级路径**（模拟生产：0001–0004 已应用 + 1 条 legacy research_log 行）：db.init 只补 0005–0007；legacy 行 `work_date IS NULL`，实测**不计入**当日 cap（0005 验收口径成立）。
- 相互引用自洽：0006 的三处 `REFERENCES securities(id)` 指向 0004；0007 `model TEXT NOT NULL DEFAULT ''` 建表即含列（非 ALTER），与 vectors.py 的 (path, sha, model) 幂等键一致；`vec_search` 虚表刻意不进迁移（migrate 的 executescript 无扩展），由 `vectors.ensure_ready` 运行时建——注释与实现一致。
- **实测复现的风险**（第二轮加固，与 S1 记录的"executescript+记账非原子"同根）：0005 含全仓**第一条非幂等迁移语句**（`ALTER TABLE ... ADD COLUMN`）。模拟"executescript 已提交、记账行未写入"的崩溃窗口后重启：`duplicate column name: work_date`，**db.init 抛异常、服务无法启动**，需手工补 schema_migrations 行。窗口极窄（单机 + WAL + 逐文件记账），且有每日备份兜底，故不升 must-fix；但今后每条 ALTER 都会放大此风险。

### 3. 事件面一致性 — PASS-WITH-NITS

全仓 `bus.emit` 与全部消费方（`vault/exporter.py` 的 4 个 `on()` 前缀、`frontend/src/useSSE.ts`、`app/api/events.py` replay）交叉核对：

- exporter 依赖的 4 个事件 `research.completed` / `workflow.completed` / `whiteboard.board_completed` / `analyst_daily.completed` 的 payload 与时序本轮**零变化**（diff 均未触碰对应 emit）。
- `whiteboard.board_opened` 时序变化（A5）：从"两条裸 INSERT 之后"变为"事务 COMMIT 之后"——消费方（前端 Whiteboard 页 SSE 刷新）收到事件时 board+card 必然已落库，属加强不属破坏；emit 在事务外执行，无 `db._write_lock` 死锁。
- `research.queued` payload 变化（A2）：MCP 路径从自发 `{topic, priority, source}` 统一为域函数的 `{topic}`。消费方核查：前端 Research 页只用事件 id 触发 reload、exporter 不监听 research.queued——无消费方受损，仅事件表观测字段变窄（可接受）。
- roadmap 新事件（`decision.opened/resolved`、`checklist.checked/renamed`、`card.moved` 等）走 `_record_event` 双写（roadmap_events 表 + bus 镜像，roadmap.py:139-145），resolved 后事件携带新值（REVIEW-A6 P1-2 修复确认在 roadmap.py:1084-1092，claim 失败先抛 MoveConflict 不发事件）。
- **Nit（N3）**：`frontend/src/useSSE.ts:7-28` 的 `KNOWN_EVENT_TYPES` 落后于事件面——`roadmap.*`、`research.followups`、`analyst_daily.*`、`thesis.*`、`market_thesis_import.completed` 均不在监听清单（SSE named event 无 wildcard），Dashboard 事件流看不到这些事件；注释"Derived from every bus.emit() call"已失真。功能性消费方无破坏，属观测缺口。

### 4. CLAUDE.md 硬规则全局扫 — PASS

- **规则 4（prompts 逐字不动）**：`app/institute/prompts.py` 零 diff；`workflows/*.json` diff 逐行核对——仅步骤键名 `"analyst"` → `"analyst_id"`（A4 归一化），全部 prompt 字符串逐字未动。
- **迁移纪律**：migrations 0001–0004 零 diff；0005–0007 纯新增。
- **规则 5（勿动久经考验的）**：`app/vault/` 零 diff（five rules 完好）；`app/hands/` 仅 `api_hands.py` 两类改动——base_url 可配置 + `trust_env=False`（main 集成接 cliproxy 的授权改动，预期内）；`rate_limit.py`/`registry.py`/`get_cli_env()` 零 diff。三个 API hand 的 URL 拼接与默认值语义核对一致（openai 默认 base 含 `/v1`、anthropic/gemini 路径补全正确，与 .env 的 `http://127.0.0.1:8317/v1` 兼容）。
- **规则 10（research 限链）**：见重点 5。规则 3（gated）：见重点 6。规则 7（时间戳）：本轮新增时间全部走 `bus.now_iso()`/`work_date()`；market_data 的微秒 `_now_known_iso` 是文档化的版本键专用时钟，与全仓秒级约定共存且注释说明充分。

### 5. retry 端点 × research 限链 — PASS

`app/api/tasks.py:54-72 _retry_policy` 与 `app/institute/workflows.py:174-184 _workflow_hand_policy` 同读 `settings.research_hand_names`，无第二事实源，配置变更（如 .env 现网 `codex,openai-api`）两处同步生效：

- 原始执行：requested = 步骤显式 `hand` 或链内轮转；`_fallback_candidates` 把 requested 放链首。
- retry：存量 requested 仍在链内则保留（含原 model），掉链则取链头且 model 置 None（家族边界规则与 executor 一致）。
- 唯一理论漂移：research 步骤若带**链外显式 hand**，原始执行会先试它（candidates 含 requested），retry 则直接替换为链头——比原始执行更严格，方向符合规则 10；且当前 `workflows/*.json` 无任何步骤级 `hand` 键，无实例。
- **Nit（N4）**：workflow 步骤任务（含 research）retry 成功后，新任务 `parent_run_id` 指向已终结的 run——产物写回原 session workspace 但不驱动 run/queue 状态推进。作为单任务复跑语义是对的，但 docstring（tasks.py:77-83）未告知操作员"救不活整条 research 链"，有误用空间。

### 6. per-analyst 守卫 × maintenance 门控 — PASS（附一个既有缝隙实测确认）

- 门控语义全局一致：`gated=True` 只约束 scheduler 作业；全部手动 API（`POST /api/analysts/daily/run-now`、`/api/research/tick`、workflow run-now）不检查 maintenance——"暂停自动开工、操作员手动可越权"是自洽的设计，scheduler.py:74-81 注释亦明确。
- paused 期间手动 `run_all`：守卫正常——完成者逐个 UPSERT `analyst_daily:<date>:<analyst_id>`（analyst_daily.py:81-86），resume 后 19:00 cron 的 run_all 读聚合 record、completed 全部跳过，不重复烧配额；paused 中途失败的分析师 resume 后正常补跑。`spawn_all` 任务在 `_background` 注册表内，停机 drain 覆盖。
- A3 M1（GLOB 注入）修复确认：`_get_record` 用 `substr(key,1,?) = ?` 字面量前缀（analyst_daily.py:69-72），外部日期元字符不再跨日污染。
- **实测确认的既有缝隙（F1-1，非本轮回归）**：守卫只防"完成后重复"，不防"进行中并发"。echo-hand 探针：两个并发 `run_all`（cron 与手动重叠、或连点两次 run-now 即可触发）→ 9 分析师 × 2 = **18 个 executor 任务、18 个 daily session**（设计不变量是一天 1 个共享 session）。两个根因都在既有代码：`run_one` 无 running 态条件认领（analyst_daily.py:176-193）；`_today_session` 的 SELECT-then-INSERT 竞态（analyst_daily.py:89-99）在单次 run_all 的 gather 并发下就会每分析师各建一个 session。功能不崩（vault export 按 payload session_id 各读各的），但双倍配额 + "共享日报 session"不变量失效。列第二轮首位建议。

### 7. 其他跨分区发现

- **F1-2（nit）**：`app/mcp.py:386-392` 注释仍写 "Until PATCH-NOTES-A2.md lands the 'inserted' key in add_topic()..."——`whiteboard.add_topic` 已返回 `inserted`（whiteboard.py:64）且 mcp 正依赖它发 `topic_pool.added`，注释与现实相反，误导后来者。
- **F1-3（nit）**：`whiteboard._open_board`（whiteboard.py:139-161）先建 session 再开 board 事务；board 事务失败时 kickoff 释放 topic claim，但已创建的 session 行/workspace 残留为孤儿（janitor 不清 sessions）。低频、无功能影响。
- **F1-4（nit）**：`tests/conftest.py:81-89` teardown 只收 4 组注册表，缺 `analyst_daily._background`、`research._bg_tasks`、`archive._bg_tasks`——与 drain 全局图不同步，残留任务可能跨测试泄漏（当前测试恰好都 await 完成，未爆）。
- **F1-5（第二轮）**：PATCH-NOTES-A1 §2 提议的 `scheduler.inflight_jobs()` 公共访问器未落地，`main._scheduler_inflight` 仍探测 APScheduler 私有结构。
- market_data（A7）跨分区面复核：无 bus 事件（文档化决策，exporter/SSE 无耦合）；`_require_replay_match` 的 float/dict 相等性经 SQLite REAL 与 json 往返无损，成立；域层 FREQS 门 + schema 开放 CHECK 的组合与"additive-only 迁移"约束自洽。
- vectors（A8）跨分区面复核：`_enabled()` 防御式 getattr 与 config.py:56-57 已落地字段吻合；快照钩子 fire-and-forget + drain 纳管 + `flush_vector_indexing` 测试钩子闭环；`archive.search_hybrid` 降级路径不阻塞 FTS。

## 问题分级汇总

**Must-fix（本轮阻断）**：无。

**Nice-to-have / 第二轮**：

| 编号 | 级别 | 位置 | 问题 |
|---|---|---|---|
| F1-1 | 第二轮·高 | `app/institute/analyst_daily.py:89-99,176-193` | 并发 run_all/run_one 双跑（双倍配额）+ 单次 sweep 每分析师各建 session，破坏"一天一共享 session"不变量（实测 18 任务/18 session）。修法：`run_one` 加 running 条件认领；`_today_session` 加锁或改 INSERT-first 幂等 |
| F1-6 | 第二轮·高 | `app/db.py:50-61` + `migrations/0005` | migrate 的 executescript 与记账非原子，0005 的 ALTER 非幂等——崩溃窗口重放实测卡启动（duplicate column）。修法：迁移文件级事务 + 记账同事务，或 ALTER 前守卫 |
| F1-5 | 第二轮·中 | `app/main.py:28-52` / `app/institute/scheduler.py` | 落地 `inflight_jobs()` 公共访问器，消除 APScheduler 私有 API 依赖 |
| N3 | 第二轮·中 | `frontend/src/useSSE.ts:7-28` | KNOWN_EVENT_TYPES 补齐本轮新事件（roadmap.*、research.followups、analyst_daily.*），或改造为 wildcard 方案 |
| N4 | nice-to-have | `app/api/tasks.py:77-83` | retry docstring 说明"workflow 步骤任务复跑不驱动已终结的 run" |
| F1-2 | nice-to-have | `app/mcp.py:386-392` | 删除已失真的 "Until PATCH-NOTES-A2 lands..." 注释 |
| F1-3 | nice-to-have | `app/institute/whiteboard.py:139-161` | board 事务失败时孤儿 session 清理（或 janitor 收编） |
| F1-4 | nice-to-have | `tests/conftest.py:81-89` | teardown 补齐 analyst_daily/research/archive 三组注册表 |

## 给第二轮的建议清单（按优先级）

1. **analyst_daily 并发防护**（F1-1）：running 条件认领 + `_today_session` 竞态修复——这是唯一实测能双倍烧配额的缝隙，且触发条件日常（cron 与手动重叠）。
2. **迁移执行器原子化**（F1-6）：per-file `BEGIN…executescript…INSERT 记账…COMMIT`（executescript 隐式提交语义需绕开，可改为逐句执行）；0005 之后 ALTER 会越来越多，早修早安全。
3. **scheduler 公共 inflight 访问器**（F1-5）+ **前端事件清单同步**（N3）：一小一大两处"全局图与局部实现漂移"的收口。
4. 事件面治理：给"新增 bus 事件必须同步 useSSE 清单/文档"立一条 checklist 规则（roadmap 卡片验收项），防止第三轮再漂。
5. conftest teardown 与 drain 注册表保持同构（F1-4），可抽一个共享的 `all_background_registries()` 帮助函数供 main 与 conftest 复用。

## 验证摘要

- `.venv/bin/python -m compileall app -q`：exit 0。
- 定向 pytest：test_research / test_analyst_daily / test_workflows / test_whiteboard / test_tasks_retry / test_executor_shutdown / test_executor_output = **46 passed**；test_maintenance / test_mcp / test_market_data = **29 passed**。全量套件由 S1 验证（191 passed / 8 skipped）。
- /tmp 迁移实测：空库全链 ✅、升级路径 ✅、崩溃窗口重放复现 ✅（均用临时 INSTITUTE_HOME，未触 `~/.institute-one`）。
- echo-hand 并发探针：F1-1 实测复现（探针文件已清理，不留仓库）。
