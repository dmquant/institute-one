# 第三轮升级终审核销（S3）

- 审计时间：2026-07-20（SGT）
- 审计视角：当前工作树的代码证据、可重复验证与生产升级预检；不以 PATCH-NOTES 的自述代替核销
- 写入边界：除本报告外未手工编辑源码/配置；按任务要求执行的两个 build 生成正常构建产物。生产库仅用 `sqlite3 -readonly` 读取，迁移演练只在 `/tmp` 副本上进行
- 总结论：纳入核销的 30 项阻断/明确要求钉死项中，**24 FIXED、2 PARTIALLY、4 NOT-FIXED**。四个 NOT-FIXED 都在 C3；两个 PARTIALLY 分别是 C1 端到端交付缺口和 C2 历史 footer 回填缺口。

## 1. must-fix / 阻断项逐项核销

### C1 — 事实核查全链

| ID | 状态 | 当前代码证据 | 核销结论 |
|---|---|---|---|
| C1-P1-1 日上限须原子化、失败也消耗预算 | **FIXED** | `app/institute/factcheck.py:555-576` 用 `UPDATE admin_state ... CAST(value AS INTEGER) < ?` 的 `rowcount` 仲裁；`app/institute/factcheck.py:579-631` 先占 attempt 再执行 hand，失败不退还；`migrations/0015_fact_check.sql:53-56` 记录 admin_state 条件更新为预算仲裁；`tests/test_factcheck.py:611-690` 覆盖并发、失败烧预算、stale 恢复与并发结算 | 上限判定已从非原子的 count-then-run 改为数据库条件更新；本项闭合。 |
| C1-P1-2 verdict 不得被证据引文中的 “FALSE/UNVERIFIED” 污染 | **FIXED** | `app/institute/factcheck.py:464-523` 仅接收行首独立 canonical verdict、跳过 fence/blockquote，多个 verdict 取保守序；`:487-494` 先展平并破坏 claim 内伪协议行；`tests/test_factcheck.py:245-304` 覆盖 canonical、引文/fence、冲突多行与 echo | 解析协议与测试均已钉死。 |
| C1-P1-3 交付链须真正挂载，不得只落模块 | **PARTIALLY** | 已挂：`app/main.py:120-133,164-203`；`app/institute/scheduler.py:165-174,344-345`；`app/vault/exporter.py:172-184,434-484,518`；`app/api/factcheck.py:24-31`。未挂：`app/institute/digests.py:140-149` 的 `analyst_disputes_md()` 仍是旧占位正文；`obsidian-plugin/src` 无 `claim_check`/`claim-check`/`claim check` 命中；研究笔记已有 callout，但白板来源档案链仍未实现 | 核心 runtime、router、scheduler 与研究 callout 已接通，但 REVIEW 要求的 Step-0 摘要消费、插件命令和更广来源档案仍缺，不能判全修。 |

### C2 — Chain 图谱

| ID | 状态 | 当前代码证据 | 核销结论 |
|---|---|---|---|
| C2-M1 “自动聚类/合并” 不得由人工 promote 冒充 | **FIXED** | `app/institute/chain.py:895-940` 实现 normalized-equal/保守 containment 自动聚类；`app/institute/chain.py:1004-1006,1041-1042` 纳入 tick；`tests/test_chain.py:500-553` 覆盖等值、长名与歧义保守跳过 | 自动聚类已成为周期链路的一部分。 |
| C2-M2 旧笔记 footer 历史回填 | **PARTIALLY** | `app/institute/chain.py:750-818` promote 时回填全部 sightings 为 mention，并发 `chain.node_updated`；`tests/test_chain.py:558-590` 覆盖数据回填；`app/institute/chain.py:1125-1134` 只重写实体 note，exporter 没有重写来源 note 的 handler | 数据层 mentions 已补齐，但已经导出的旧来源 note 不会自动重投影 footer；新写入链路正常，历史 Vault 仍需 backfill/re-export。 |
| C2-M3 slug 映射不得非单射导致覆盖 | **FIXED** | `migrations/0016_chain_graph.sql:15-18,21-32` 持久化并唯一约束 slug；`app/institute/chain.py:241-252,318-322,801-805` 在事务内分配稳定唯一 slug；`app/institute/chain.py:1054-1058` wikilink 使用持久 slug；`tests/test_chain.py:619-655` 覆盖字符与截断碰撞 | 冲突不再覆盖同一个 note。 |
| C2-M4 name/alias 必须唯一解析 | **FIXED** | `app/institute/chain.py:138-159` 定义并发安全的 term 冲突 SQL；`app/institute/chain.py:290-323` 创建 node 在一个事务内校验全部 name/alias；`app/institute/chain.py:569-623` merge aliases 同样仲裁；`tests/test_chain.py:660-691` 覆盖 name 占用他人 alias 与 promote 合并 | 跨 node 的名称解析歧义已拒绝。 |
| C2-M5 cursor crash replay 不得重复计数 | **FIXED** | `migrations/0016_chain_graph.sql:102-118` sighting 唯一键为 `(candidate_id, artifact_kind, artifact_ref)`；`app/institute/chain.py:650-687` `INSERT OR IGNORE` 后按真实 insert 数增量；`app/institute/chain.py:993-1042` cursor 重放仍走幂等写；`tests/test_chain.py:696-735` 覆盖崩溃重放与直接幂等 | “写候选后、推 cursor 前”崩溃不会重复加 mention_count。 |

### C3 — 预测抽取与 paper book

| ID | 状态 | 当前代码证据 | 核销结论 |
|---|---|---|---|
| C3-H1 `kind='ticker'` 数字 alias 不得绕过日期/金额/小数守卫 | **FIXED** | `app/institute/forecast_extract.py:228-250` 名称 rail 拒绝纯数字 alias；`:271-279` 只允许其走带完整 guard 的 ticker rail；`tests/test_forecast_extract.py:120-166` 用生产形态 ticker alias 覆盖 YYYYMM、金额、小数尾与 digit-run | 真实 importer 形态的 numeric ticker alias bypass 已封住。 |
| C3-H2 MTM 到期结算不得取 horizon 之后价格 | **FIXED** | `app/institute/paper_book.py:178-185` 定义 `min(work_date, expiry date)` 窗口；`:383-389` 取 mark 时强制使用该窗口；`tests/test_paper_book.py:256-292` 覆盖到期后剧烈价格变化和晚手工平仓 | 结算不再看见 horizon 之后的未来价格。 |
| C3-H3 无有效价格不得按 0 结算并扭曲 PnL | **FIXED** | `app/institute/paper_book.py:390-407` 将不可定价保持为 unknown/NULL，并累计 unpriced；`:425-436` 排除 NULL realized 且显式计数；`tests/test_paper_book.py:295-339` 覆盖 unknown-not-zero 与 journal gap | 不可定价与真实 0 已分开。 |
| C3-M1 中文简称跨市场同名须消歧 | **NOT-FIXED** | `app/institute/forecast_extract.py:238-250` 将每个 name/alias 直接展开为 `(name, sid)`；`:253-284` 命中后逐 sid 全部加入，未见“同名多 security 拒绝/消歧”仲裁 | canonical ticker 已修不等于 name 唯一；A/H 或多市场同名仍可能同时开仓。 |
| C3-M2 source claim、forecast rows 与 bookkeeping 必须 crash-consistent | **NOT-FIXED** | `app/institute/forecast_extract.py:355-385` 先独立提交 source claim；`:387-414` 再逐条创建 forecast，最后单独回填 forecast_ids。`migrations/0017_paper_book.sql:9-18` 仍明示 claim 后崩溃会留下 claimed-but-empty，建议人工 DELETE 重抽 | 崩溃可留下部分 forecasts 且普通 replay 被 duplicate claim 封死；DELETE 后重抽又会复制已成功部分，未形成可恢复幂等状态机。 |
| C3-M3 并发 opener 不得重复开仓 | **FIXED** | `app/institute/paper_book.py:190-210` 用条件 INSERT + partial unique index/IntegrityError 仲裁；`migrations/0017_paper_book.sql:45-72` 固化 one-open-position-per-security；`tests/test_paper_book.py:342-375` 覆盖 security/forecast/cap 与双 tick | 数据库已经成为唯一仲裁者。 |
| C3-M4 否定与 horizon 边界须保守解析 | **NOT-FIXED** | `app/institute/forecast_extract.py:109-138,169-200` 仍是命中前 8 字符否定和按位置最早 horizon regex。实测：`看多？不 -> long`、`不建议看多 -> long`、`2026年内，未来2周 -> 365` | REVIEW 给出的核心反例仍可复现。 |
| C3-M5 paper outcome attribution 须回流 analyst memory | **NOT-FIXED** | `migrations/0017_paper_book.sql:19-28` 的 extraction provenance 没有 analyst_id；`app/institute/paper_book.py:287-292` 的 closed event 也没有 analyst；`app/institute/memory.py:131-202` 只收 daily/card/mail 三类材料，没有 paper outcome 消费 | 多作者 daily 产物无法可靠归因，账本结果也未进入 analyst memory；ROADMAP Phase 5 attribution 尚未交付。 |

### C4 — operator loop

| ID | 状态 | 当前代码证据 | 核销结论 |
|---|---|---|---|
| C4-MF1 confidence floor 必须成为普通 approve 的消费门 | **FIXED** | `app/institute/operator.py:449-473,507-524` 动态读取 floor 并写 `low_confidence` flag；`app/api/operator.py:239-248` 对该 flag 在任何状态写入前返回 409；`tests/test_operator.py:249-257` 覆盖 flag | 低置信度建议可留作 shadow telemetry，但不能经普通 approve 结案。0.69/0.70/缺失边界测试仍建议补。 |
| C4-MF2 approve 两笔状态变更必须原子 | **FIXED** | `app/api/operator.py:259-278` 在同一 `db.transaction()` 内条件更新 action，并更新 disposition flags；action claim 的 `rowcount==0` 抛错会回滚 | 原先“action 已 done、disposition 未 approved”的半状态风险已移除；仍建议加第二笔写失败 fault-injection 测试。 |
| C4-MAJOR detail 不能伪造 router 输出协议 | **FIXED** | `app/institute/operator.py:404-426` 将 detail 每行引用为 `> `，并二次破坏残留的行首 `DISPOSITION:`/`CONFIDENCE:` 协议形态；`tests/test_operator.py:259-278` 覆盖恶意 detail | 回显 prompt 时，detail 中的协议行不再被 line-anchored parser 当成模型结论。 |

### C5 — 委员会与多代理原语

| ID | 状态 | 当前代码证据 | 核销结论 |
|---|---|---|---|
| C5-MF1 同步 multi-agent API 要有总墙钟预算且不得取消后台任务 | **FIXED** | `app/institute/multi_agent.py:30-106` 拆成 spawn/wait，`asyncio.wait` 超时不 cancel；`app/api/multi_agent.py:46-95` 限制 `wait_s`，超时返回 202+task_ids；`tests/test_multi_agent.py:70-107,210-244` 覆盖 | 单个超时不再串行放大，总预算到点后任务继续。 |
| C5-MF2 committee 周任务必须幂等且允许失败接管 | **FIXED** | `app/institute/workflows.py:209-291` 以 ISO week `admin_state` INSERT 仲裁，允许 failed/cancelled 或 1h kickoff 崩溃重开；`tests/test_committee.py:235-270` 覆盖重放、并发和失败重开 | 同周多次触发不再重复跑。 |
| C5-MF3 cron helper 必须支持 `day_of_week`，片段不得炸启动 | **FIXED** | `app/institute/scheduler.py:310-322` helper 接收并条件传递 `day_of_week`，同时保留空串/坏时间禁用；`:338` committee 用 `day_of_week="fri"` | scheduler 可正常注册 weekday 任务；目前缺独立 scheduler 注册回归测试，已列入第四轮覆盖债。 |

### C6 — 平台化

| ID | 状态 | 当前代码证据 | 核销结论 |
|---|---|---|---|
| C6-H1 `doctor` 默认必须只读 | **FIXED** | `app/cli.py:295-353,384-419` 用 SQLite URI `mode=ro` 读取 DB/ledger，Vault 侧只读文件并算 hash；`tests/test_cli_doctor.py:536-558` 对完整 doctor 前后做 byte-for-byte 树快照 | 默认 doctor 不写生产 DB/Vault。 |
| C6-H2 artifact path 校验必须防 symlink escape | **FIXED** | `app/api/contract.py:128-144` 对目标与 root `resolve()` 后再 `is_relative_to(root)`；`tests/test_contract.py:179-204` 覆盖文件和目录 symlink 越界 403 | 词法前缀绕过与 symlink 越界均被拒绝。 |
| C6-M1 hands auth 不得只看二进制存在 | **FIXED** | `app/cli.py:50-61,173-216` 为可验证 CLI 配置无 prompt 登录探针，不能验证的明确返回 unknown；`tests/test_cli_doctor.py:153-180` 覆盖 logged-in/logged-out/unknown | 已从“which 即健康”升级为真实 auth probe；未知 CLI 仍只能返回 unknown。 |
| C6-M2 `_run_async` 不得在已有 event loop 中 `asyncio.run` | **FIXED** | `app/cli.py:64-78` 无 loop 时 `asyncio.run`，有 loop 时转专用线程；`tests/test_cli_doctor.py:309-324` 覆盖 doctor 在 loop 内调用 | notebook/嵌套 loop 场景不再直接崩溃。 |
| C6-M3 cooldown JSON 损坏不得拖垮 doctor | **FIXED** | `app/cli.py:525-564` 对顶层、逐项类型与异常分别降级成 Check；`tests/test_cli_doctor.py:438-470` 覆盖字符串、标量、Infinity 与混合好坏项 | doctor 可继续输出其余检查结果。 |

### C7 — SPA

| ID | 状态 | 当前代码证据 | 核销结论 |
|---|---|---|---|
| C7-H1（任务说明称 M1）SSE 重连窗口不得丢事件 | **FIXED** | `frontend/src/useSSE.ts:5-31` 明示 SSE 只作唤醒、数据统一走 durable `/api/events?since=`；`:72-86` 共享尾游标遍历；`:123-186` bootstrap + 唯一单调 cursor/串行 catch-up；`:188-258` 重连即对账、15s reconcile 与 65s watchdog | 架构满足“单调游标 + 可补历史”：连接间窗口由 since-cursor 回补，不再依赖 `/api/events/stream` 的环形 replay。frontend build 已通过。 |

C7 的其余非阻断项也已改善：`frontend/src/api.ts:114-152` 的 NDJSON frame/done/EOF 解析、`frontend/src/api.ts:54-73` 的 HTML 2xx 降级、Dashboard/Ask/MultiAgent/Hands 的错误态均已落地；遗留细节见第四轮待办。

### C8 — hand weights 与文档

| ID | 状态 | 当前代码证据 | 核销结论 |
|---|---|---|---|
| C8-M1 文档不得宣称 curl-back digest 已被 CLI Step-0 prompt 消费 | **FIXED** | `CLAUDE.md:31` 明写 prompt-side `curl` 尚未接线；`ROADMAP.md:118` 已改为 ◔，区分“端点已交付”和“Step-0 prompt 未接入” | 完成度口径已校正，不再把稳定端点冒充 prompt 消费。 |
| C8-M2 文档不得宣称 memory 已注入“所有 analyst prompt” | **FIXED** | `CLAUDE.md:29` 限定四个 workflow prompt assembly 点并注明 ad-hoc asks 未接；`ROADMAP.md:116` 已改为 ◔ 并列出四域/未接入口 | 完成度口径与当前代码一致。 |

### 核销统计

| 状态 | 数量 | 涉及项 |
|---|---:|---|
| FIXED | 24 | C1×2、C2×4、C3×4、C4×3、C5×3、C6×5、C7×1、C8×2 |
| PARTIALLY | 2 | C1-P1-3、C2-M2 |
| NOT-FIXED | 4 | C3-M1、C3-M2、C3-M4、C3-M5 |
| 合计 | 30 | 当前代码快照 |

## 2. I3 集成应用核销

### 挂载逐项

| 核销组 | 期望 | 当前代码证据 | 结果 |
|---|---|---|---|
| config 3 字段 | `committee_time`、`factcheck_tick_minutes`、`factcheck_daily_cap` | `app/config.py:89,98,102` | **3/3 APPLIED** |
| scheduler 8 jobs | factcheck、chain、committee、operator fast/deep/vault、paper opener/MTM | job bodies：`app/institute/scheduler.py:165-217`；注册：`app/institute/scheduler.py:338-349` | **8/8 APPLIED** |
| scheduler gate | model job gated；非 model job ungated | `factcheck/chain/committee/operator-fast/operator-deep=True`；`vault-sweep/paper-opener/paper-mtm=False`，见 `app/institute/scheduler.py:165-217` | **8/8 一致** |
| cron weekday | helper 与 committee 注册支持 `day_of_week` | `app/institute/scheduler.py:310-322,338` | **APPLIED** |
| main.py 4 register | `factcheck`、`chain_graph`、`forecast_extract`、`operator_loop` | `app/main.py:120-133` | **4/4 APPLIED** |
| main.py 5 router | factcheck、chain、paper_book、operator、multi_agent | `app/main.py:164-180,192-203`；OpenAPI smoke 验证 `/api/meta/claim_check_before_write`、`/api/chain/nodes`、`/api/book/positions`、`/api/operator/actions`、`/api/multi-agent/run` 均存在 | **5/5 APPLIED** |
| exporter C1 组 | disputed factcheck callout | `app/vault/exporter.py:172-184,434-484,518` | **APPLIED** |
| exporter C2 组 | research/workflow/board/analyst_daily/memory 五类 entity footer | `app/vault/exporter.py:186-189,278-281,331-334,369-372,416-419` | **5/5 APPLIED** |
| exporter C3 组 | `paper_book.marked` → 日度 journal（正文投影开/平仓） | `app/vault/exporter.py:489-506,519` | **APPLIED** |

补充结构核对：当前 scheduler 共 19 个 `@metered` job，wrapper 均带可检查的 `gated` 属性；新增 8 个与 job body 是否提交模型调用一致。`create_app().openapi()` 可正常生成，避免了“文件存在但 router 未挂”的假阳性。

### 五个适配差异合理性

仓库根没有独立的 `I3` 报告文件，`implementation-notes.md` 截至 09:34 也没有 I3 条目；因此下面按五份 PATCH-NOTES、执行日志裁决与当前代码可复现的五项差异核对。

| # | 适配差异 | 评价 | 依据/后续 |
|---:|---|---|---|
| 1 | `factcheck_daily_cap` 在 Settings 中用 `None`，而不是直接写 `10` | **合理** | `app/institute/factcheck.py:197-203` 将缺失/None/非法值归一为模块默认 10，同时保留测试 monkeypatch 与环境覆盖能力。 |
| 2 | 未加入 PATCH-NOTES-C1 建议的 `factcheck_extract_hand` / `factcheck_verify_hand` 两个 Settings 字段 | **短期兼容、最终不完整** | `app/institute/factcheck.py:206-218` 防御性回退到 `default_hand`，所以不破坏运行；但便宜抽取手/联网核查手无法配置，保留为第四轮卡。 |
| 3 | chain tick 固定 `minutes=60`，没有再引入可选 interval 配置 | **合理** | PATCH-NOTES-C2 明确该配置为可选；固定周期与 ROADMAP/hourly 语义一致，避免无必要扩张 Settings。 |
| 4 | paper opener/MTM 固定为 `00:05` / `00:00`，没有再引入可选 schedule 配置 | **合理** | PATCH-NOTES-C3 将配置化标为 optional；当前时序先 MTM、后 opener，语义明确。 |
| 5 | committee 采用 Friday 20:00，而 ROADMAP 文案曾建议 22:00 | **合理但应同步文档** | `workflows/committee.json`、`app/config.py:89` 与 `implementation-notes.md:111` 的主代理裁决一致；22:00 是建议时间，不是契约。ROADMAP 应避免留下冲突默认值。 |

## 3. 全量验证

所有命令均在当前工作树执行；测试未改代码。

| 验证 | 命令 | 结果 |
|---|---|---|
| Python 编译 | `.venv/bin/python -m compileall -q app tests` | **PASS**，exit 0 |
| 全量测试与 skip 理由 | `.venv/bin/python -m pytest tests -q -rs` | **PASS：634 passed / 9 skipped / 110 warnings，96.75s** |
| frontend | `npm run build`（`frontend/`） | **PASS**，Vite 279 modules，产物成功生成 |
| Obsidian plugin | `npm run build`（`obsidian-plugin/`） | **PASS**，tsc + esbuild 成功 |
| shell 语法 | 对 `scripts/*.sh` 逐个执行 `bash -n` | **PASS：6/6** |

9 个 skip 均是环境型、理由明确：

- 4 × `tests/test_cards.py:150`：离线测试环境不可用真实模型
- 4 × `tests/test_executor.py:171`：离线测试环境不可用 opencode
- 1 × `tests/test_vectors.py:21`：本机未安装 `sqlite-vec`

这些 skip 不是断言失败，但意味着真实 hand、opencode 与 sqlite-vec 的生产 smoke 尚不由本轮自动测试覆盖。110 条 warning 主要来自 FastAPI `on_event`、Pydantic V1 validator、datetime 与 httpx app shortcut 的弃用提示，应进入技术债清理。

## 4. 生产升级预检（只读）

### 生产库现状

只读查询对象：`~/.institute-one/institute.db`

- `schema_migrations` 列为 `name`、`applied_at`，不是 `version`
- 已应用：`0001_init.sql` 至 `0014_shared_data.sql`
- 当前停止点：**0014**
- 待应用：**0015_fact_check.sql、0016_chain_graph.sql、0017_paper_book.sql、0018_operator_actions.sql**

### B1 migration discipline

对 `migrations/0015_fact_check.sql`、`0016_chain_graph.sql`、`0017_paper_book.sql`、`0018_operator_actions.sql` 剥离 SQL 注释后检查可执行 statement：

- `BEGIN`：0
- `COMMIT`：0
- `ATTACH`：0
- `VACUUM`：0
- `PRAGMA`：0

四文件均只提供由 runner 管理的 SQL statement，符合 B1 “迁移文件不自管事务/连接级状态”纪律。
原始文本仅有一处字面命中：`0016_chain_graph.sql:8` 的注释写着 “no BEGIN/COMMIT/ATTACH/VACUUM”；它不是可执行 SQL。

### 副本演练

| 演练 | 结果 |
|---|---|
| 空白 `/tmp` DB 运行当前 migration runner | **PASS**：18/18；表计数 fact_cards=0、chain_nodes=0、forecast_extractions=0、action_dispositions=0 |
| 复制生产 DB 到 `/tmp` 后增量运行 | **PASS**：14 → 18；四个目标表均创建，原 18 个 events 与 0 个 analyst_memory 记录保留 |
| 对增量副本再次运行 | **PASS**：仍为 18 条，无重复迁移 |
| 真生产 DB | **未写入**：复查仍为 14 条 |

### 升级安全结论

**有条件 GO**：四个迁移通过语句纪律、空库、生产副本增量与幂等复跑。执行真实升级前仍应：

1. 先备份 DB 与 Vault，并记录校验值；
2. 明确停止所有可能持有 SQLite 写连接的服务；
3. 跑 migration runner，核对 0015–0018 四条记录与四张新表；
4. 启动服务后跑 `/health`、OpenAPI 新路由、scheduler 注册与最小写入 smoke；
5. 本批四个迁移没有 `ALTER ... ADD COLUMN`，不会触发 B1 的旧式部分迁移恢复分支；该分支尚未比较 `CHECK`/`REFERENCES`，应作为独立技术债补强。

本结论是“迁移本身可升级”，不是“已经在生产执行升级”。

## 5. 8100 现状

- `curl --max-time 5 http://127.0.0.1:8100/health`：**连接失败，HTTP 000，curl exit 7**
- `lsof -nP -iTCP:8100 -sTCP:LISTEN`：**无监听者**
- `launchctl print gui/502/com.institute-one`：label 存在，但当前无运行进程，`last exit code = 1`

因此“旧代码正在 8100 正常运行”的前提与审计时现场不符：当前应标记 **SERVICE DOWN / NO LISTENER**。由于没有 listener，新端点是否返回预期 404 也无法现场验证；本报告没有越过只读边界擅自启动服务。

## 6. 第四轮完整待办

### P0 — 先闭合当前风险

- [ ] **C3-M1：跨市场同名消歧。** 建立 `name_zh -> [security_id]` 索引；多候选时要求 ticker/market 证据，否则拒绝提取。
- [ ] **C3-M2：source claim crash consistency。** 将 extraction claim、逐候选 forecast 创建和 forecast_ids bookkeeping 放入可恢复状态机/稳定 candidate key；禁止以 DELETE claim 作为会复制部分成功项的恢复方案，并补 fault-injection 测试。
- [ ] **C3-M4：否定与 horizon 解析。** 覆盖 REVIEW 反例（`看多？不`、`不建议看多`、`2026年内，未来2周`），改为分句/候选排序而非首个 regex 命中。
- [ ] **C3-M5：paper outcome → analyst memory。** 先为 forecast/extraction 建可靠 analyst provenance（尤其多作者 daily），再将 closed/settled 结果幂等回流对应 analyst memory。
- [ ] **8100 服务恢复。** 查 `last exit code=1` 的 stderr/launchd 环境，恢复后验证 `/health`、旧/新路由与 scheduler；不要把当前状态写成“旧代码正常运行”。
- [ ] **B1 历史恢复护栏。** 对旧式 partial-apply 的 `ADD COLUMN` 恢复分支完整比较 `CHECK`/`REFERENCES`，或在无法证明等价时 fail-closed；补 schema-drift 探针与人工恢复说明。

### P1 — REVIEW / PATCH-NOTES 遗留

#### C1

- [ ] 让 `analyst_disputes_md()` 读取真实 fact_cards，不再返回旧占位正文；把 disputed block 真正注入 analyst 的 Step-0，冲突未处理时不得生成“干净摘要”。
- [ ] 给 Obsidian plugin 增加 claim-check-before-write 命令，并显示 `possible_match`/人工确认路径。
- [ ] 将 source dossier / `sources: [...]` callout 从研究笔记扩展到白板及要求的其他输出；对已有产物提供回填。
- [ ] 将 disputed queue/digest 事件从 best-effort handler 升级为 durable retry/outbox；补 handler 失败重试与幂等测试。
- [ ] 增加 `factcheck_extract_hand` / `factcheck_verify_hand` Settings，使便宜抽取与联网核查可独立路由。
- [ ] 增加 daily source hook、低置信阈值校准、claim card 历史向量 backfill；验证 `daily cap=0/None/非法值/跨日` 边界。
- [ ] 给 extraction parser 增加模板回显、源文自带 JSON fence、裸数组后置答案等生产形态对抗用例，避免 echo 再充当伪 oracle。

#### C2

- [ ] 对已有 Research/Whiteboard/Workflow/Analyst/Memory note 做 footer re-export/backfill，不只补数据库 mentions。
- [ ] S2：完整接通 board 产物的实体 backstop/footer，而非只覆盖当前简化字段。
- [ ] S3：对含 `\|`、换行、`]]`、HTML 等 hostile display 的 wikilink/footer 做安全编码。
- [ ] S4：支持全角 `｜` 或明确拒绝并给出可见错误。
- [ ] S5：升级 relation grammar/展示，不只保留极简 edge label。
- [ ] S6：同步 schema/API 文档，清理已过期的“可选/未挂载”表述。
- [ ] ASCII 短实体匹配增加词边界（避免 `AI` 命中 `PAID`），并给 live handler 的永久丢事件风险增加 durable replay。

#### C3

- [ ] 解决上列四个 NOT-FIXED。
- [ ] 增加 settlement 与账本历史 reconciliation、严格 schema/API 校验、benchmark base 缺失防护。
- [ ] 扩展 stopword/简称变体与 daily scan 覆盖；补历史 MTM/补价后重算和 forecast Vault 导出。
- [ ] 另将 standing `memory_block` 注入 forecast extraction 的 analyst prompt；此项与“预测结果回流 memory”的 C3-M5 是两个方向。
- [ ] 将跨模块 `_usable_price` / `_adj_close` 提升为公共资金口径 helper，并用共享契约测试防止 settlement 与 paper book 漂移。
- [ ] API 对越界值明确拒绝而非静默钳制；config 浮点不得经 `int()` 无提示截断。

#### C4

- [ ] 为 confidence floor 加 0.69/0.70/缺失/非法配置测试；为 approve 加第二笔 disposition UPDATE 失败的 rollback 测试。
- [ ] 给 disposition 增加业务唯一约束，防同 action/type 重复卡；把 `human_pinned` 行为钉死。
- [ ] feature switch 改 CAS/版本化写，并让各子系统真实执行开关，而非只存值/展示。
- [ ] 移除 operator 对 Vault 私有 helper 的耦合；评估本机 HTTP approve/reject 的真实 human auth 边界。
- [ ] 完成 operator kanban/triage SPA 与端到端人工确认流程。

#### C5

- [ ] 将“输出文件链”升级为引擎契约：下游消费声明、缺失文件判失败、路径/哈希记录与恢复测试。
- [ ] 为 multi-agent 建持久 group/run 记录、可重连状态 API 和部分 spawn 失败恢复；当前仅返回散列 task_ids。
- [ ] 将 committee 输出导出至 `Committee/` Vault，并对 weekly 输入做可追溯快照。
- [ ] 解决 free-text join 的 exact-equality 局限，给 `majority_vote` 增加结构化 verdict。
- [ ] 明确 FastAPI request-shape 走 422 还是统一 400，并为 prompt 长度、50-board 上限、恰好七天边界与查询降级补约束/测试。
- [ ] 将 ROADMAP 的 22:00 与实际 Friday 20:00 统一；修正 PATCH-NOTES 中过时的测试数量。

#### C6

- [ ] 在真实 launchd 环境做 install/start/restart/stop/uninstall soak；核验日志轮转、stale cron、orphan PID 与失败保留 plist。
- [ ] 给未知 hand CLI 建可扩展 auth probe 协议；当前只能 `unknown`，不能证明已登录。
- [ ] 对 doctor 输出建立稳定 machine-readable schema 与版本，清理 asyncio/thread 生命周期边界。
- [ ] 补真实损坏 SQLite 的 integrity-failure、cron 缺报/陈旧与更多 running 域的 doctor 测试。

#### C7

- [ ] `Forecasts.tsx` 对未知 settlement verdict 不应静默隐藏；显示原值或 unknown badge。
- [ ] 修复事件分组“过滤后只有一组仍按多组显示”的 stale condition。
- [ ] 统一 Ask/MultiAgent source-load 与 Hands cooldown-clear 的错误呈现；修复未来时间 `ago()`。
- [ ] 补 useSSE 单调游标的前端自动测试：bootstrap、并发调用点、分页、重连窗口、ring eviction、watchdog。
- [ ] 让 Obsidian plugin 的 ask 使用 streaming 路径；更新 PATCH-NOTES 中“4 routes”等过时数量。

#### C8

- [ ] 扩大 50+ capability/event 配对 sanity 测试，覆盖权重缓存 invalidation 与所有 hand 选择路径。
- [ ] 清理 `app/hands/registry.py:223` 仍写“wiring call sites is follow-up”的过时注释。
- [ ] README 明示“显式 analyst hand 优先于 weighted pick”，并避免再出现“所有 prompt 已注入 memory”的泛化表述。
- [ ] 为 tasks/ask_stream/sessions/MCP 等 ad-hoc analyst 入口裁决并实现 memory 注入，或持续保持文档的 partial 口径。
- [ ] 若保留“OFF byte-for-byte”声明，增加完整 prompt/task 快照；否则把声明收窄为“hand 选择语义不变”。

### P2 — B 轮与 ROADMAP 剩余

- [ ] **B1 scheduler/migration：** 补 cron metric INSERT 自身失败、正式 cancellation 与 APScheduler 私有接口漂移测试；历史 ADD COLUMN 恢复见 P0。
- [ ] **B2 scorecard/weights：** 补 lifespan 权重预热集成测试、严格历史纠错/回填投影与 triage SPA；四个运行时权重点已由 C8 接通。
- [ ] **B3 memory：** ad-hoc memory 创建入口、并发 compact 避免重复模型调用、注入失败可观测性。
- [ ] **B5 market data：** DATA_BUNDLE 真正注入相关 prompt、refresh race/缓存一致性、benchmark fetch 与失败降级。
- [ ] **B6 forecasts：** scheduled seeding、批量 settlement、历史 export、paper risk/cap 的严格执行。
- [ ] **B8 streaming/digests：** public prepare、unknown hand 协议、真实断线测试、placeholder digest 数据、插件流式消费。
- [ ] **跨轮输入边界：** `research.seed_from_theses()` 对 bool/字符串/小数 cap 采用 strict integer 拒绝，避免当前 `int(cap)` 静默转换。
- [ ] **Phase 7 BFS tree：** 不只保存 task_ids；实现依赖图、状态聚合、失败传播、重试与可视化。
- [ ] **Phase 7 research projects：** 从一次性 workflow 升级为可持续项目，包含目标、里程碑、证据链和周报。
- [ ] **Phase 7 bilingual：** 中英双语模板、实体/引用一致性与语言切换测试。
- [ ] **Phase 7 more hands：** 在认证、能力声明、权重、冷却、scorecard 与 fallback 全链完成后再扩 hand。
- [ ] **Phase 7 committee 完整化：** 持久化 group、Committee Vault、结构化共识及文件链契约。
- [ ] **Phase 8 test coverage：** 新模块分支/并发/故障注入、frontend SSE、真实 CLI/launchd 集成；明确外部依赖 skip 的 CI 策略。
- [ ] **Phase 8 MCP 扩展：** 只读/写入工具边界、contract/version、审计与权限；补 chain/forecast/operator/committee 工具。
- [ ] **Phase 8 legacy migration：** 旧 Vault/frontmatter/event/admin_state 数据迁移、dry-run、可逆回滚、校验报告。
- [ ] **弃用债：** FastAPI lifespan、Pydantic v2、timezone-aware datetime、httpx transport，逐项消除当前 110 warnings。

## 7. 最终判定

- 第三轮不是“全绿收官”：主路径与集成大体闭合，但 C3 仍有 4 个明确未修的正确性/一致性问题。
- 当前代码质量门通过：634/9、两个前端构建、compileall、6 个 shell syntax 全过。
- 迁移 0015–0018 在只读检查与副本演练上可升级，结论为有条件 GO；真实生产尚未执行。
- 8100 当前没有 listener，应先恢复并查 exit 1，再做生产升级后的 runtime smoke。
