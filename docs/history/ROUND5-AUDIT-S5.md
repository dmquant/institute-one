# ROUND5-AUDIT-S5 — 第五轮终轮核销与 15 小时升级终版计分卡

> 角色：S5b（接替连接故障的前任 S5，独立只读核销；与 F5b 并行）。  
> 审计快照：2026-07-20 14:15:49（UTC+8）。  
> 写入边界：除新建本报告外，未手工修改源码、配置、数据库、launchd 或生产数据；生产检查仅使用 GET、MCP `tools/list`、`launchctl print` 与 SQLite `mode=ro`。按任务要求执行的 compile/test/build 会产生常规缓存/构建产物。  
> 状态口径：`FIXED`=代码、回归和所需生产证据闭合；`PARTIAL`=主功能已落但声明/验收仍不完整；`OPEN`=尚未形成要求的能力。  
> 最终结论：**运行面 GO，终稿面 GO-WITH-FOLLOWUP。** 825/4、双端 build、23 个生产迁移、20/20 调度注册、35 MCP tools、launchd running 均通过；但 S4-P0 严格为 **3 FIXED / 1 PARTIAL**，`ROADMAP.md` 仍不是最终事实源，且 E6 主代理集成有两处重复落行。

## 0. 执行摘要

- **全量质量门全部通过**：`compileall` exit 0；`825 passed / 4 skipped`；SPA 与 Obsidian plugin build exit 0；6 个 shell 脚本 `bash -n` 全过。
- **生产终验全部通过**：`/health` 200/ok；SQLite 23 migrations、integrity `ok`；cron registry **20/20 registered（14 gated / 6 ungated）**；MCP **35 tools**；launchd `state=running`。
- **S4-P0-01/02/03 已闭合**；P0-04 只部分闭合：E3 预写的 Phase 1b 数据注入、Phase 2 Step-0 确已成真，但 ROADMAP 仍有明确状态/说明漂移。
- **E1–E7 交付主功能均可核销**；E8 的 36 卡落板和外部 bundle 契约存在，但“终稿”与最终代码不同步，判 `PARTIAL`。
- **主代理三组直修均有行为证据**，但 auth 集成在 `app/config.py` 和 `app/main.py` 各重复一次；Ollama 两个 HTTP 入口的 `trust_env=False` 干净落地。
- **五轮唯一 must-fix 78 项：77 FIXED / 1 PARTIAL，严格关闭率 98.7%，核销覆盖率 100%。** 唯一未严格闭合项是 P0-04 终稿一致性。

---

## 1. S4-P0 四项闭环确认

| P0 | 结论 | 当前代码/测试/生产证据 | 核销意见 |
|---|---|---|---|
| S4-P0-01 · ADD COLUMN 的 CHECK/REFERENCES 证明 | **FIXED** | `app/db.py:254-302` 从 `sqlite_master.sql` 解析列定义；`:305-388` 先校验 PRAGMA 可见的 type/NOT NULL/DEFAULT，再把完整声明规范化比对，解析或声明不一致即 `MigrationRecoveryError`。`tests/test_db_migrate.py:297-423` 覆盖真实 0010 CHECK 重放、CHECK+REFERENCES 匹配、缺 CHECK、CHECK 文本漂移和不可证明时 fail-closed。 | 原四轮 75 项中唯一 PARTIAL 已转正。这里不是“看见同名列就跳过”，而是有存储 DDL 的第二道证明。 |
| S4-P0-02 · skip 账目清理 | **FIXED** | `tests/test_market_thesis_import.py:198-203,216-331` 只给 2 个真实 bundle 集成例保留 marker，6 个 subset 例改为自包含 fixture；`tests/test_restart_recovery.py:196-243` 用真实域函数构造两种 running tree，零探测式 skip。全量实际只剩 4 skip，逐项见 §1.1。 | 原来的 6 个过宽 marker + 1 个 D4 探针债均消失。 |
| S4-P0-03 · cron registry/健康面 | **FIXED** | `app/institute/scheduler.py:366-395` 合并定义面与 APScheduler live 面；`app/api/meta.py:45-113` 对 registry 与 metrics 做全集 LEFT JOIN；`frontend/src/pages/Settings.tsx:39-43` 与 `CronHealth.tsx:52-65` 消费服务端 gate/schedule/next-run，不再硬编码。生产实测 20 jobs、20 registered、无 unregistered。 | 代码、前端和生产三层均闭环，不再用“有过 metric 的 13 项”冒充注册全集。 |
| S4-P0-04 · ROADMAP 终稿与 M1-003 契约 | **PARTIAL** | `ROADMAP.md` 原样仍为 **37/11/10**；backlog 为 36 卡、`17 done + 1 review + 18 inbox`。E3 的两处预写已由真实代码证明，但 E4/E6/E7 后仍有状态与说明漂移，见 §1.2。`INSTITUTE_THESIS_BUNDLE` 已进入 `CLAUDE.md:54`、backlog 和真实 bundle 测试选择；CLI 本体仍要求位置参数 `bundle`（`market_thesis_import.py:735-749`），可用 `$INSTITUTE_THESIS_BUNDLE` 显式传入，但不会自动取该环境变量。 | 36 卡落板成立，M1-003 保持 review 合理；“终稿已与代码一致”不成立，不能判 4/4。 |

### 1.1 当前 4 个 skip 逐一核对

| # | pytest 位置与原因 | 合理性 | 后续 |
|---:|---|---|---|
| 1 | `tests/test_market_fetchers.py:743`：`INSTITUTE_NET_TESTS=1` 才跑真实 Sina 网络 smoke | **合理**。默认测试必须离线、确定，不应把外网稳定性变成主门。 | 独立网络/供应商 smoke 执行。 |
| 2 | `tests/test_market_thesis_import.py:334`：真实商业 bundle dry-run | **合理**。仓内不应携带商业数据，且已有自包含 subset 覆盖核心导入逻辑。 | 提供 `INSTITUTE_THESIS_BUNDLE` 后跑。 |
| 3 | `tests/test_market_thesis_import.py:545`：真实商业 bundle full apply | **合理**。它验证 55/74/236/1888 的真实全量，不可用伪数据冒充。 | 与 #2 同批验收，M1-003 才能由 review→done。 |
| 4 | `tests/test_similarity_calibration.py:302`：真实 bge-m3 校准，需 Ollama 与 `INSTITUTE_CALIBRATION_REAL=1` | **合理但代表真实验收债仍在**。60 对合成台架锁住结构行为，却不能证明生产 0.85/0.65 是真实模型最优刻度。 | 对应 M8-004，不能因合成校准通过而把 Phase 1a 写成完全校准。 |

结论：**4 个 skip 都是有明确外部依赖的预期 skip；没有剩余的过宽 marker 或探测式假覆盖。**

### 1.2 E8 终稿抽查与 ROADMAP 重算

| 抽查项 | 文件当前标记 | 代码现实 | 判定 |
|---|---:|---|---|
| Phase 1b · Research data injection | ☑ | `workflows/research.json:9,17,31` 声明并在 01/03 生产 prompt 使用 `${DATA_BUNDLE}`；03 的 web-search 句已改成本地数据优先；端到端测试随 825 套件通过。 | **一致；E3 预写成真。** |
| Phase 2 · Curl-back digest→Step-0 | ☑ | `research.json:17-45` 的 01–05 步含同一 recent-reports curl；06/07 未加，符合汇编/编排边界。 | **一致；E3 预写成真。** |
| Phase 0 · Interactive asks | ☐ | `executor.py:53-63` 有 `hand_busy`；`tasks.py:136-184` 对未 pin 的 ask 优先空闲可用 fallback；`:44-63` 与 `executor.py:347-400` 有 200/404/409 单任务取消。 | **不一致，应为 ☑。** |
| Phase 0 · Optional auth | ☐ | bearer middleware、`INSTITUTE_TOKEN`、`start.sh` host 已存在；但非 loopback 且无 token 时只 WARNING，不按 ROADMAP/M8-019 字面“enforces”强制，且主程序集成重复。 | **不一致，应为 ◔，不是 ☑。** |
| Phase 6 · Actions kanban | ◔，说明仍称“SPA 无 route” | `frontend/src/App.tsx:42,121` 已挂 `/operator`；`pages/Operator.tsx:35-57,331-545` 是四列看板、状态变更与人工 approve。 | **不一致，应为 ☑。** |
| Phase 6 · Recipes/observations/proposals/effect | ☐，说明仍称 schema-only | 0023 + `operator.py:431-525,640-726` + `api/operator.py:295-328` 已形成“人工批准→recipe→零模型复用→retire”最小环；observations/proposals/effect measurement 未做。 | **不一致，应为 ◔，不能写 ☑。** |

同状态但说明也已失真的项目：

- Phase 2 Analyst memory 仍可保留 `◔`（`compact_one` 仍在模型调用后才用唯一索引抢 version，两个并发调用会双烧模型），但“ad-hoc ask/sessions/MCP 尚未注入”已经错误；sync/stream 现共享 `prepare_ask`。
- Phase 6 Triage 仍为 `◔`，因为页面虽已落地，但页面自己在 `Operator.tsx:321-323` 明示开关只存储/展示、PUT 非 CAS。
- Phase 7 Projects/BFS/Bilingual 继续 `◔` 合理，但“无 SPA/无 digest/无 viewer”等旧说明部分失真；剩余分别是操作 API/MCP、retry+score、可写 locale 开关+稳定 twin API。
- Phase 8 Test coverage 继续 `◔`（前端状态机没有自动测试）合理，但“764 tests / 7 removable skips”已经错误：当前为 825/4，且 4 个均为外部验收。

因此不改文件、仅按当前代码重算：

| 口径 | ☑ | ◔ | ☐ | 严格完成率 | 半权完成率 |
|---|---:|---:|---:|---:|---:|
| `ROADMAP.md` 原样 | 37 | 11 | 10 | 63.8% | 73.3% |
| **S5 实际重算** | **39** | **12** | **7** | **67.2%** | **77.6%** |
| 排除 5 个明确 optional/无语料项 | 39 | 12 | 2 | 73.6% | 84.9% |

重算迁移只有四类：interactive `☐→☑`、auth `☐→◔`、actions kanban `◔→☑`、recipes `☐→◔`。E3 的两项已预写且确实落地；E7 的真实 bge 校准和完整自改进链均未完成，不能乐观“全转 ☑”。

---

## 2. E1–E8 交付核销（每代理至少两处证据）

| 代理 | 抽查证据（至少 2 项） | 结论 |
|---|---|---|
| **E1** | ① `db.py:254-388` sqlite_master 完整声明证明，`test_db_migrate.py:297-423` 对抗回归；② `test_market_thesis_import.py:216-331` 自包含 subset，真实 marker 只剩 `:334,:545`；③ `test_restart_recovery.py:196-243` 确定性 live/stopped tree 恢复。 | **PASS / FIXED** |
| **E2** | ① `scheduler.py:366-395` registry 的定义面+live 面；② `meta.py:45-113` registry/metrics LEFT JOIN；③ `Settings.tsx:39-43`、`CronHealth.tsx:52-65` 不再硬编码。生产 20/20。 | **PASS / FIXED** |
| **E3** | ① `research.json:9,17-45` 数据包与 Step-0 真接线；② `tasks.py:157-184`、`sessions.py:76`、`mcp.py:842` 注入 memory；③ `ask_stream.py:49,114` 复用共享 async `prepare_ask`，流/非流不再镜像漂移。 | **PASS / FIXED（声明边界外的 compact 双烧仍在 M8-005）** |
| **E4** | ① `App.tsx:42,121` operator nav/route；② `Operator.tsx:35-57,331-545` 四列看板、条件状态流与人工批准；③ `:114-199,202-328` triage + switches UI。 | **PASS；Actions kanban 可转 ☑，Triage 仍 ◔** |
| **E5** | ① `Trees.tsx:17-29,154-284` 列表/详情、tree.* SSE 唤醒、分层节点与 stop；② `Projects.tsx:23-29,122-262` 项目详情、四类链接和 digest；③ `Settings.tsx:287-378` twin_ready 列表与按 task 引用读全文。 | **PASS-WITH-BOUNDARIES**：viewer/page 已落；project archive/unlink、tree retry/score、bilingual 写开关/稳定索引仍未交付。 |
| **E6** | ① `executor.py:53-63,347-400` busy 读面与正确的 queued-first cancel；② `tasks.py:44-63,136-184` 取消契约+空闲手选择；③ `auth.py:58-106` 纯 ASGI bearer；④ `scripts/start.sh:25` 尊重 `INSTITUTE_HOST`。 | **功能 PASS，集成卫生有 finding**：见 §3 的重复字段/重复 middleware；非 loopback 无 token 只告警。 |
| **E7** | ① `test_similarity_calibration.py:49-161,263-344` 60 对三档合成语料、真实分类器矩阵及 opt-in bge 台架；② `0023_recipes_minimal_loop.sql` 加 recipe 字段/唯一索引；③ `operator.py:431-525,681-725` promote/match/零模型复用；④ `api/operator.py:295-328` list/promote/retire。 | **PASS-WITH-DECLARED-BOUNDARY**：合成 sanity 和最小 recipe 环成立；真实 bge 与 observations/proposals/effect 仍未完成。 |
| **E8** | ① `roadmap/backlog.json` 为 36 卡，M8-001/002 done、M1-003 review；② `CLAUDE.md:12-16,42-54` 加 CLI/launchd 与新模块 Map；③ `ROADMAP.md` 已从严重失真推进到 37/11/10，并诚实标出 optional。 | **PARTIAL**：作为落板/文档交付成立；作为“最终事实终稿”不成立，详见 §1.2、§7。 |

---

## 3. 主代理直修三组核对

实现日志 12:59–13:26 的主代理直修按行为归并为三组（覆盖 4 个文件）：

| 直修组 | 当前证据 | 结论 |
|---|---|---|
| ask-stream memory lockstep | E3 的临时 `_prepare` 直修已被 E6 的更简洁共享实现取代：`ask_stream.py:49,114` 直接 import/await `tasks.prepare_ask`；`tasks.py:157-184` 内含 persona、memory、404 与 idle-hand。 | **FIXED，且不再有镜像实现。** |
| auth Settings + app 挂载 | `app/config.py:25-28` 的 `token` 字段连续定义了 **两次**；`app/main.py:162-166` 的 import + `install_auth(app)` 也连续执行 **两次**。Pydantic 最终字段和双 middleware 的功能仍可用，但与 `auth.py:95`“Called once”不符，非 loopback 无 token 还会重复 warning。 | **EFFECTIVE-BUT-DUPLICATED；应做一处小清理并补 create_app 级单挂载测试。** |
| Ollama 全局代理隔离 | `app/hands/ollama_hand.py:44-47` execute 与 `:67-75` health_check 两个 `AsyncClient` 均为 `trust_env=False`。全量 doctor/hand 测试随 825 套件通过。 | **FIXED。** |

这两处重复不是本轮测试失败或生产故障，但它们证明“主代理集成已完全干净”这一说法不成立；应与 P0-04 文档对账一起做终清。

---

## 4. 全量终验输出

| 验证 | 命令 | 实测结果 |
|---|---|---|
| Python compile | `.venv/bin/python -m compileall app -q` | **PASS，exit 0** |
| 全量 pytest | `.venv/bin/python -m pytest tests -q -rs` | **PASS：825 passed, 4 skipped in 67.41s** |
| SPA build | `cd frontend && npm run build` | **PASS**，tsc + Vite；58 modules，JS 279.88 kB / gzip 85.79 kB |
| Obsidian plugin build | `cd obsidian-plugin && npm run build` | **PASS**，tsc noEmit + esbuild |
| shell syntax | `for f in scripts/*.sh; do bash -n "$f"; done` | **PASS：6 scripts** |

两个 npm build 都打印 `Unknown env config "devdir"` warning；不影响本轮 exit 0，属于环境/未来 npm 主版本兼容提醒，不是当前构建失败。

---

## 5. 生产终验（全只读）

| 检查 | 2026-07-20 14:15 快照 | 判定 |
|---|---|---|
| `GET /health` | HTTP 200；`ok=true`；version `0.1.0` | **PASS** |
| 生产 SQLite | `COUNT=23`；`MIN=0001_init.sql`；`MAX=0023_recipes_minimal_loop.sql`；`PRAGMA integrity_check=ok` | **PASS** |
| `GET /api/cron/health` | 20 jobs；**20 registered**；14 gated / 6 ungated；unregistered `[]` | **PASS，S4 的 13-vs-20 缺口已关闭** |
| MCP `tools/list` | HTTP 200；**35 tools** | **PASS** |
| MCP 写面 | `app/mcp.py:41` 仍恰为 `research_queue_add/topic_pool_add/institute_ask` 3 个；全量 guard 通过 | **PASS** |
| `launchctl print gui/501/com.institute-one.server` | `state=running`；program=`.venv/bin/uvicorn`；cwd 正确；runs=4；pid=54174 | **PASS** |

发布判断：当前单机生产主路径可继续运行；本报告发现的是终稿可信度、auth 策略边界和重复集成卫生，不是现网不可用。

---

## 6. 终版计分卡

### 6.1 规模与交付增长

| 指标 | 起点 | 终点 | 增量 | 倍数/增长 |
|---|---:|---:|---:|---:|
| pytest passed | 93 | **825** | +732 | **8.87× / +787.1%** |
| migrations | 4 | **23** | +19 | **5.75× / +475.0%** |
| MCP tools | 15 | **35** | +20 | **2.33× / +133.3%** |
| scheduler jobs | 8 | **20** | +12 | **2.50× / +150.0%** |
| backlog cards | 16 | **36** | +20 | 当前 **17 done / 1 review / 18 inbox** |

### 6.2 路由、页面、命令规模

| 表面 | 当前规模 | 口径 |
|---|---:|---|
| FastAPI/OpenAPI | **144 unique paths / 171 method-path operations** | 90 GET + 81 mutating；`create_app().openapi()` 动态统计 |
| SPA | **20 page components / 24 concrete routes + 1 catch-all / 17 nav entries** | `frontend/src/pages/*.tsx` 与 `App.tsx` |
| Obsidian plugin | **17 commands** | `main.ts` 的 `this.addCommand(...)` |
| Operator CLI | **4 subcommands** | `start / stop / status / doctor`（`app/cli.py:681-684`） |
| shell scripts | **6** | 本轮全部 `bash -n` 通过 |

### 6.3 五轮 must-fix 与核销率

为避免把 carried debt 膨胀计数，给出两种口径：

| 口径 | 总数 | FIXED | PARTIAL | 严格关闭率 | 核销覆盖率 |
|---|---:|---:|---:|---:|---:|
| 轮级核销动作（Round 1–4 的 75 + Round 5 四个 P0 slot） | 79 | 78 | 1 | 98.7% | 100% |
| **唯一问题去重**（P0-01 是原 75 中的 carried debt，只算一次） | **78** | **77** | **1** | **98.7%** | **100%** |

轮次明细：Round 1=14、Round 2=26、Round 3=30、Round 4=5；Round 5 新增唯一项为 P0-02/03/04 三项，并把 Round 2 遗留 P0-01 转正。唯一 PARTIAL 是 P0-04 文档/控制面终稿。

### 6.4 中断接力恢复率

- 第三轮共有 **8 次 resource/context 中断事件**：原 C1 一次，加 C1b/C2/C4/C5/C6/C7/C8 中断潮七次；均由接手代理或主代理收敛。
- 本轮前任 S5 连接故障由 S5b 接替并完成报告，再加 1 次。
- **代理中断事件恢复率：9/9 = 100%（覆盖 8 个独立工作流：C1/C2/C4/C5/C6/C7/C8/S5）。**
- 另有一次用户中断全量构建命令，不计入“代理接力”；随后复跑及本轮独立复跑均全绿。

### 6.5 CLAUDE.md 十条硬规则

**五轮可核证硬规则违例：0。**

- E3 是明确授权、附完整 diff/回滚的 prompt-change card，不构成规则 4 的偷改。
- E7 recipe 命中仍落 shadow disposition、人工批准门不被绕过；模型 miss 仍走 executor。
- 0023 是新增迁移，旧迁移未改；迁移禁事务语句测试随 825 套件通过。
- MCP 写面仍恰 3，research hand confinement、调度 gate 全集、VaultWriter 边界均有现有 guard。
- 本报告发现的重复 auth 集成与陈旧说明属于质量/事实同步缺陷，不属于十条硬规则中的一条违规。

### 6.6 总评

| 维度 | 结果 | 评级 |
|---|---|---|
| S4-P0 闭环 | 3 FIXED / 1 PARTIAL | **B+** |
| E1–E8 功能交付 | E1–E7 主功能通过；E8 终稿 partial | **A-** |
| 全量测试/构建 | 825/4，compile/build/shell 全绿 | **A** |
| 生产运行 | health、23 migrations、20/20 cron、35 MCP、launchd running | **A** |
| ROADMAP 实际完成 | 39 ☑ / 12 ◔ / 7 ☐；半权 77.6% | **B / GO-WITH-BACKLOG** |
| 控制面可信度 | raw backlog 结构正确；ROADMAP/CLAUDE/backlog 多处事实滞后 | **C+** |
| 硬规则与接力 | 0 违例；9/9 中断恢复 | **A** |

---

## 7. backlog 36 卡分布与 M8 20 卡优先级

### 7.1 文件原样分布

- 总数：**36**
- status：**17 done / 1 review / 18 inbox / 0 其他**
- priority：**4 P0 / 16 P1 / 15 P2 / 1 P3**
- M1-003 保持 `review` 是诚实状态：真实商业 bundle 未提供，2 个 full integration skip 未跑。
- M8-001/002 已 `done`；其余 M8 卡仍全写 `inbox`，但 E3–E7 已对其中多卡完成一部分，状态/summary 需重切。

### 7.2 M8 20 卡建议执行顺序（按剩余风险重排）

| 建议序 | 卡 | 文件状态/优先级 | E 轮后真实剩余 |
|---:|---|---|---|
| — | M8-001 | done / P0 | 已完成 sqlite_master CHECK/REFERENCES 证明。 |
| — | M8-002 | done / P1 | 已完成 D4 确定性 restart fixture。 |
| 1 | **M8-019** | inbox / P2（建议升 P0/P1） | idle-hand + cancel + token middleware 已落；剩余：去掉 config/main 重复、裁决并强制 non-loopback auth、给 SPA/plugin/MCP 增 token 配置面、补 create_app 单挂载测试。 |
| 2 | **M8-003** | inbox / P1 high | durable retry lineage/idempotency、queue depth、rate_limited resurrection；仍是执行正确性主债。 |
| 3 | **M8-005** | inbox / P1 | ad-hoc/sessions/MCP/stream memory 已完成；只剩 **模型调用前** conditional claim，避免并发 compact 双烧。 |
| 4 | **M8-006** | inbox / P1 | SPA kanban/triage 已完成；剩 feature-switch enforcement、CAS、human-auth boundary、shadow-exit policy。 |
| 5 | **M8-007** | inbox / P1 | 前端 test runner + useSSE bootstrap/pagination/reconnect/ring/watchdog；是当前测试面的最大结构缺口。 |
| 6 | **M8-004** | inbox / P1 | 合成 60 对台架已完成；只剩真实 Ollama bge-m3 分布与阈值裁决。外部依赖就绪即跑。 |
| 7 | **M8-013** | inbox / P2 | disputed claim durable outbox，避免 verdict 已落而投递丢失。 |
| 8 | **M8-014** | inbox / P2 high | forecast seeding/reconciliation/risk enforcement/Vault export；涉及历史归因与资金口径，优先于一般产品补面。 |
| 9 | **M8-016** | inbox / P2 | whiteboard session/workspace claim lease + reaper，封硬杀泄漏。 |
| 10 | **M8-015** | inbox / P2 | 48h launchd KeepAlive/restart/uninstall/log soak；属于必须用时间换证据的外部验收。 |
| 11 | **M8-018** | inbox / P2 | events retention + 30 日 tree counter janitor；先定审计留存窗口。 |
| 12 | **M8-009** | inbox / P2 | SSE tree viewer 已完成；剩 bounded retry、score/ranking、normalized child key、counter cleanup、429。 |
| 13 | **M8-010** | inbox / P2 | SPA + digest 已完成；剩 archive/unlink API 与 MCP `project_id` 写侧透传。 |
| 14 | **M8-011** | inbox / P2 | 已有 Settings 只读事件/task 引用视图；剩稳定 `(run_id,locale)` API、可写开关、retry/idempotency。 |
| 15 | **M8-012** | inbox / P2 | persistent multi-agent group/run、Committee Vault、输入快照与结构化 verdict。 |
| 16 | **M8-017** | inbox / P2 | vector degradation reason、内容寻址复用、旧模型 GC。 |
| 17 | **M8-008** | inbox / P2 high，依赖 M8-006 | 最小 recipe reuse 已完成；剩 durable observations/proposals、只经 web 人工批准、parameter history/effect measurement。 |
| 18 | **M8-020** | inbox / P3 | shared ask prepare 与 Ollama proxy 已部分完成；剩 warnings、scorecard time、shared price helper、APScheduler 私有结构、事实型文档清扫。 |

---

## 8. E 轮新遗留与给用户的“下一步”

### 8.1 新发现/最终未清

| ID | 优先级 | 遗留 | 证据与处理建议 |
|---|---:|---|---|
| S5-E-01 | **P0 终清** | E6 auth 主程序集成重复 | `config.py:25-28` 两个同名字段；`main.py:162-166` 两次 `install_auth`。各保留一份，并新增 `create_app().user_middleware` 恰一层 auth 的测试。 |
| S5-E-02 | **P0 终清** | ROADMAP 不是终稿事实源 | 文件 37/11/10，实际 39/12/7；至少同步 interactive/auth/actions/recipes 标记及 memory/projects/tree/test 说明。 |
| S5-E-03 | P1 | non-loopback auth 只告警、不强制 | `auth.py:99-106` 只 log warning，和 ROADMAP/M8-019 acceptance 的“host != loopback enforces bearer”不一致；先裁决 fail-start、强制 401 或保持 warning 的正式威胁模型。 |
| S5-E-04 | P1 | 控制面卡片内容滞后 | M8-005/006/008/009/010/019/020 的 summary 仍把 E 轮已完成部分写成未做；应拆剩余验收后更新状态，避免重复开发。 |
| S5-E-05 | P2 | 文档/注释残留旧事实 | `CLAUDE.md:32,40,81` 仍称 ad-hoc memory 未接、recipes schema-only、ask 必然排长队；`0023_recipes_minimal_loop.sql:10-14` 仍称 db guard 只比 type/NOT NULL/DEFAULT。 |
| S5-E-06 | P2 外部门 | 三类不能在本轮伪造的验收 | M1-003 真实 bundle 2 例；真实 bge-m3 校准；48h launchd soak。 |

### 8.2 推荐下一步（按顺序）

1. **先做一个小型“终稿对账补丁”**：去掉两处重复 auth 集成；补单挂载测试；同步 ROADMAP 到 39/12/7；修 CLAUDE/backlog/0023 旧说明。该补丁完成后，S4-P0-04 才可严格转正。
2. **裁决并收口 M8-019 的 auth 威胁模型**：若允许 LAN bind，无 token 应 fail-start 或强制拒绝；同时给 SPA、Obsidian、MCP 客户端提供 token header 配置。不要只靠 warning 宣称“已隔离”。
3. **做两个高价值正确性卡**：M8-003 durable retry/idempotency 与 M8-005 pre-model compact claim。
4. **补最大测试盲区**：M8-007 前端 useSSE 状态机自动化。
5. **外部条件一到即核销**：`INSTITUTE_THESIS_BUNDLE` 两例、真实 bge 60 对、launchd 48h soak；它们应保留真实证据，不用 mock 宣称完成。
6. 随后按 §7.2 推进 durable outbox、forecast reconciliation/risk、whiteboard lease，再做 Phase 7/recipe 全产品化。

**最终发布口径：当前运行版可继续使用；不要对外宣称“ROADMAP Phase 0–8 全完成”或“S4-P0 4/4 全闭合”。准确表述是：生产主干与第五轮功能已通过终验，ROADMAP 实际 39☑/12◔/7☐，尚需一个小型终稿对账补丁及 M8 backlog。**
