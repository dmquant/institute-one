# ROUND4-AUDIT-S4 — 第四轮验证核销与 15 小时工程终盘

> 角色：S4，核销、全量验证、生产实况与工程完成度盘点。  
> 审计时间：2026-07-20 12:10（UTC+8）  
> 写入边界：除本报告外未手工修改源码、配置或生产数据；生产检查仅使用 GET、MCP `tools/list`、`launchctl print` 与 `sqlite3 -readonly`。  
> 口径：`☑`=当前代码和关键验收均落地；`◔`=主干已落但产品面、验收或可靠性仍缺；`☐`=尚未形成可用能力。

## 0. 终审结论

- **第四轮指定核销项：15/15 FIXED。** R-D4 3 项、R-D5 2 项、D1 4 项、D2 4 项、D3 2 项均有当前代码和回归测试证据。
- **全量质量门：通过。** `compileall`、`764 passed / 10 skipped`、两个前端 build、全部 shell 脚本语法检查均退出 0。
- **生产主路径：通过，带一个可观测性缺口。** `/health` 正常，MCP 35 tools，launchd `running`，生产迁移 22/22；`/api/cron/health` 当前只列出 13 个已经产生 metric 的任务，而代码注册表是 20 个，接口不能证明尚未触发的 7 个日/周任务已注册。
- **15 小时工程不是整张 ROADMAP 全完成。** 58 项实际为 **35 ☑ / 13 ◔ / 10 ☐**；严格完成率 60.3%，半权计入部分完成为 71.6%。排除明确无语料的 legacy migration 后为 72.8%。
- **ROADMAP 状态严重滞后。** 文档当前为 19 ☑ / 2 ◔ / 37 ☐，有 **29/58 项**与代码现实不一致；不能继续把该文件的勾选值直接当完成度。
- **四轮双审 must-fix：75 项，74 FIXED / 1 PARTIALLY。** 唯一未完全闭合的是 B1 `ADD COLUMN` 崩溃恢复守卫仍不比较 `CHECK/REFERENCES`；严格全修率 **98.7%**，核销覆盖率 100%。
- **发布判断：第四轮 GO；全路线图仍是 GO-WITH-BACKLOG。** 本轮修复和当前生产服务可放行，但交互式取消、认证隔离、若干 Phase 6/7 产品面、前端自动化测试及历史迁移等不能宣称完成。

---

## 1. 第四轮逐项核销

### 1.1 R-D4（REVIEW-D4 FAIL 后返工）

这里的单个 “M” 按 `PATCH-NOTES-D4.md:10-16` 的返工记录指 **M2 停止事件失真**；原审查 M1（非确定重放仅 exact-topic 幂等）明确未修，已列入本报告遗留清单。

| 项目 | 状态 | 当前代码证据 | 回归证据与结论 |
|---|---|---|---|
| H1 子节点不得在父结论落库前被认领 | **FIXED** | `app/institute/research_tree.py:385-424` 的候选查询与条件 UPDATE 都要求父节点 `completed`；`:617-627` 在同一个写事务内先把父节点写成 completed，再通过 `_insert_children(conn, ...)` 写全部子节点；丢失 running claim 时整批结果不落库。 | `tests/test_research_tree.py:270-317` 验证父链结论和父未完成不可认领；`:319-351` 验证丢 claim 丢弃整批。事务可见性与防御守卫均闭合。 |
| H2 stop 与子插入竞态不得留下永久 pending | **FIXED** | `research_tree.py:428-498` 的子插入在完成事务内并带树状态守卫；`:642-669` sweep 清理终态树历史搁浅行；`:728-760` 将 tree→stopped 与 pending→pruned 放进同一事务。 | `tests/test_research_tree.py:496-517,544-657` 覆盖历史搁浅、mid-flight stop、已提交子、pending 根和幂等 stop。 |
| M2 `tree.completed` 必须是排空后的单发终态快照 | **FIXED** | `migrations/0020_research_tree.sql:35-45` 增加 `announced_at`；`research_tree.py:528-560` 仅在树终态且无 pending/running 时条件认领并发事件；`:566-571` 统一 settle。 | `tests/test_research_tree.py:544-623` 验证 running 自然收尾后才宣布，SSE/Vault 不再收到半成品快照。 |

集成也已落地：`app/main.py:117-118,188-213` 恢复孤儿并挂 router，`scheduler.py:166,351` 注册 5 分钟 gated job，`app/vault/exporter.py:567-611,659` 注册终态投影。

### 1.2 R-D5（REVIEW-D5 FAIL 后返工）

| 项目 | 状态 | 当前代码证据 | 回归证据与结论 |
|---|---|---|---|
| H1 归档后不得竞态新增 link/enqueue | **FIXED** | `app/institute/projects.py:122-170` 用 `INSERT OR IGNORE ... SELECT ... FROM projects WHERE status='active'` 单语句仲裁 link；`app/institute/research.py:196-214` 对 project enqueue 使用相同条件 INSERT。 | `tests/test_projects.py:171-218` 两条 barrier 竞态测试分别锁住 link/archive 与 enqueue/archive。 |
| H2 损坏 maintenance 状态不得 fail-open 烧翻译配额 | **FIXED** | `app/institute/bilingual.py:114-137` 仅“缺行”视为正常未暂停；坏 JSON、非 object、非 bool 和读取异常全部 fail-closed 为 paused。 | `tests/test_bilingual.py:213-277` 覆盖正常暂停、5 组坏形状、读取异常和保守读取单测。 |

附带中低项已落：tree 引用动态校验（`projects.py:135-153`）、twin 引用式事件（`bilingual.py:142-157`）、name 折行/Markdown 转义、冻结 WORK_DATE 共用、prompt 文档边界、`n_links` 双轨合并。M5 所需专用 twin 读取 API/SPA toggle 仍未交付，列入遗留。

### 1.3 D1 对 F3 P2/P3 的核销

| F3 项目 | 状态 | 当前代码证据 | 回归证据与结论 |
|---|---|---|---|
| P2-1 title/ref 协议注入 | **FIXED** | `app/institute/operator.py:138-141` `_fold_line()` 清控制字符并折单行；`:436-460` detail 引用化，title/ref 折行后再插值。 | `tests/test_operator.py:264-329` 覆盖 detail、title、factcheck claim 与 ref 控制字符攻击。 |
| P3-1 approve 必须按 live floor 复核 | **FIXED** | `app/api/operator.py:234-258` 在消费时读取 live floor，missing/below floor 一律 409；存量 flag 只作提案时 telemetry。 | `tests/test_operator.py:557-592` 覆盖门槛上调追拦与下调解锁旧 flag。 |
| P3-2 消费门边界测试 | **FIXED** | 同上。 | `tests/test_operator.py:534-555` 覆盖 0.69 拒、0.70 过、missing 拒。 |
| P3-3 gated/ungated 注册表必须全集锁定 | **FIXED** | `tests/test_maintenance.py:83-119` 反射全部 `@metered` 函数，对 14 gated + 6 ungated 完整决策表做精确集合断言，并锁总数 20。 | 新 job 漏分类或 gate 翻转都会直接失败。 |

D1 还顺手关闭 F3 NIT-3：`migrations/0022_action_dispositions_unique.sql` 为 route proposal 增数据库唯一兜底，`tests/test_operator.py:594-621` 锁定并发收敛。

### 1.4 D2 对 S3 C3 四项 NOT-FIXED 的核销

| S3 项目 | 状态 | 当前代码证据 | 回归证据与结论 |
|---|---|---|---|
| C3-M1 跨市场同名消歧 | **FIXED** | `app/institute/forecast_extract.py:324-394` 按 casefold name 分组；canonical/bare ticker 可锚定唯一 listing，无强证据时同名组 fail-closed 并记录 `ambiguous_names`。 | `tests/test_forecast_extract.py:262-299` 覆盖同名拒绝与强证据锚定。 |
| C3-M2 extraction 崩溃一致性 | **FIXED** | `forecast_extract.py:549-626` source claim 使用 pending→complete 状态机；每个 security 由 `forecast_extraction_items` 主键认领并回填 forecast_id，重放只补缺项；迁移在 `migrations/0019_paper_book_hardening.sql`。 | `tests/test_forecast_extract.py:369-443` 覆盖候选间崩溃恢复与 create 内崩溃不复制。极窄的 create 成功/回填前窗口按 in-doubt 保守停住，需人工确认，不再自动复制。 |
| C3-M4 否定与 horizon 反例 | **FIXED** | `forecast_extract.py:211-271` 增邻接/建议式/问答否定并对多个 horizon 取最短可信值；`:399-429` 处理 splitter 吃掉问号后否定的情况。 | `tests/test_forecast_extract.py:209-259` 覆盖原审反例。 |
| C3-M5 outcome attribution 回流 memory | **FIXED** | `forecast_extract.py:475-509` 取 workflow 最后非 ops analyst；`paper_book.py:280-310` closed event 带 analyst_id；`memory.py:207-280` 以第四游标来源采集归属结果。 | `tests/test_forecast_extract.py:476-511`、`tests/test_paper_book.py:380-443` 覆盖归因与无归因分支。 |

### 1.5 D3 对 S3 两项 PARTIALLY 的核销

| S3 项目 | 状态 | 当前代码证据 | 回归证据与结论 |
|---|---|---|---|
| C1-P1-3 交付链末端 | **FIXED** | `app/institute/digests.py:149-199` 生成 analyst disputes 真正文并在缺表时降级；`app/vault/exporter.py:315-372,481-537` 把 disputed callout 扩至白板并按 dispute 重投影；`obsidian-plugin/src/main.ts:115-119,260-280` 和 `api.ts:289-302,768-776` 提供 claim-check 命令/API。 | `tests/test_digests.py:239-313` 覆盖空、缺表、排序/归属和 8KB cap；exporter 合成事件测试随全量通过；插件 build 通过。 |
| C2-M2 历史 footer 回填 | **FIXED** | `app/institute/chain.py:1333-1376` 遍历受管 `vault_index`，重算 footer，限 cap、幂等，人工编辑只报 conflicts；`app/api/chain.py:97-102` 提供触发端点。 | `tests/test_chain.py:755-863` 覆盖 file/region、区外人工注释、人工冲突、kind/cap 和 API 校验。 |

### 1.6 第四轮核销统计

| 来源 | 指定项 | FIXED | PARTIALLY | NOT-FIXED |
|---|---:|---:|---:|---:|
| R-D4 | 3 | 3 | 0 | 0 |
| R-D5 | 2 | 2 | 0 | 0 |
| D1 ← F3 | 4 | 4 | 0 | 0 |
| D2 ← S3 | 4 | 4 | 0 | 0 |
| D3 ← S3 | 2 | 2 | 0 | 0 |
| **合计** | **15** | **15** | **0** | **0** |

---

## 2. 全量验证

| 验证 | 命令 | 结果 |
|---|---|---|
| Python 编译 | `.venv/bin/python -m compileall app -q` | **PASS，exit 0** |
| 全量测试 | `.venv/bin/python -m pytest tests -q -rs` | **PASS：764 passed, 10 skipped in 38.52s** |
| SPA | `cd frontend && npm run build` | **PASS，TypeScript + Vite exit 0** |
| Obsidian plugin | `cd obsidian-plugin && npm run build` | **PASS，tsc + esbuild exit 0** |
| shell 语法 | `for f in scripts/*.sh; do bash -n "$f"; done` | **PASS，全部脚本 exit 0** |

### 2.1 10 个 skip 的实质核验

| 数量 | 原因 | 审计判断 |
|---:|---|---|
| 8 | `tests/test_market_thesis_import.py:240-458` 的 8 个 `@requires_bundle`；本仓缺 `market-thesis-data/bundle.json` | 只有 2 个测试真正读取 real bundle；其余 6 个使用自造 `subset` fixture，却被同一 marker 一并跳过，属于**过宽 skip 债**。 |
| 1 | `tests/test_market_fetchers.py:743-747` real-network smoke，需 `INSTITUTE_NET_TESTS=1` | 合理的默认离线 skip。 |
| 1 | `tests/test_restart_recovery.py:207-241` D4 restart probe 无法 seed running tree 后调用旧文案 skip | D4 已落地但 probe 仍不稳定，是**真实覆盖缺口**；应改成确定性 fixture，不应长期 skip。 |

因此“764/10 全绿”成立，但 10 个 skip 不能全部当成预期外部依赖：至少 7 个（6 个过宽 marker + 1 个 D4 probe）应清理。

---

## 3. 生产实况（只读）

审计快照时间：2026-07-20 12:10 SGT。

| 检查 | 实况 | 判定 |
|---|---|---|
| `GET /health` | `ok=true`，`version=0.1.0`，返回 SGT 时间 | **PASS** |
| `GET /api/contract` | `version=1`，`status_source=code_constants`；schema cross-check 包含 `research_queue/tasks/whiteboard_boards/workflow_runs` | **PASS**；契约面仍只覆盖 4 张表。 |
| `GET /api/cron/health` | 30 天窗口内 **13** 个有 metric 的 job；13 个均无失败记录 | **PASS-WITH-GAP**：不是注册表总数接口。 |
| MCP `tools/list` | **35** tools；其中写工具仍恰为 `institute_ask/topic_pool_add/research_queue_add` 3 个 | **PASS** |
| `launchctl print gui/501/com.institute-one.server` | `state=running`，`pid=88160`，`runs=2`，working directory/uvicorn 参数正确 | **PASS** |
| `sqlite3 -readonly` | `COUNT=22`，`MIN=0001_init.sql`，`MAX=0022_action_dispositions_unique.sql` | **PASS** |

### 3.1 cron 的“13 vs 20”解释与缺口

- 代码注册表由 `tests/test_maintenance.py:83-119` 反射证明是 **20（14 gated / 6 ungated；7 cron + 13 interval）**。
- `/api/cron/health` 的实现 `app/api/meta.py:45-91` 只 `GROUP BY cron_metrics`，**从未执行过的 job 不会出现在响应里**。
- 当前已有 metric 的 13 项包括新 `research-tree-tick`；尚未出现的 7 项是 `briefing`、`daily-report`、`analyst-dailies`、`memory-compact`、`hand-scorecard`、`committee`、`paper-mtm`，均为安装/重启后尚未到点的日/周 cron。
- 所以这不是“只注册了 13 个 job”，但也是实际可观测性缺口：生产 GET 不能直接回答“是否注册 20 个、各自 gate/next_run 是什么”。建议让健康端点合并 scheduler registry 与 metric 聚合。

---

## 4. ROADMAP Phase 0–8 完成度

### 4.1 总体对账

| 口径 | ☑ | ◔ | ☐ | 说明 |
|---|---:|---:|---:|---|
| `ROADMAP.md` 当前标记 | 19 | 2 | 37 | 文件原样统计 |
| 当前代码实际 | **35** | **13** | **10** | 本审逐项核对 |
| 差异 | +16 | +11 | -27 | 29 项发生状态迁移，其中 18 项 `☐→☑`、9 项 `☐→◔`、2 项 `☑→◔` |

- 严格完成率：`35 / 58 = 60.3%`。
- 半权完成率：`(35 + 13×0.5) / 58 = 71.6%`。
- 排除 Phase 8 legacy migration（明确无旧语料）：`41.5 / 57 = 72.8%`。

### 4.2 逐 Phase 项级盘点（每 Phase 至少两处代码抽查）

| Phase | 文档标记 `☑/◔/☐` | 实际 `☑/◔/☐` | 项级实际判定 | 两项以上抽查证据与结论 |
|---|---:|---:|---|---|
| 0 Foundation | 10/0/4 | **11/1/2** | ☑ orphan recovery、shutdown、MCP bypass、identity、daily cap、maintenance、workflow dedup、roster cache、output cap、launchd、small fixes；◔ test gaps；☐ interactive timeout/cancel、auth/session isolation | `app/main.py:60-81,115-118` 覆盖 drain/recovery；`app/cli.py:589-670` 与 `scripts/install-service.sh` 证明 launchd 已交付。`app/main.py` 无认证 middleware，interactive ask 仍无可取消任务协议。 |
| 1a Retrieval | 3/0/0 | **2/1/0** | ☑ local embeddings、FTS fallback；◔ whiteboard similarity gate（功能已落，ROADMAP 要求的 50+ pair sanity 未交） | `app/institute/vectors.py:88-116,279-317`；`whiteboard.py:267-331,533-548`。门逻辑存在，但缺大样本校准/验收。 |
| 1b Data injection | 2/0/0 | **1/1/0** | ☑ FMP→Stooq→Sina ladder；◔ research data injection | `market_fetchers.py:483-590,611-680` 实现 ladder/refresh；`workflows.py:354-371` 只有 prompt 含 `${DATA_BUNDLE}` 才计算，但 `workflows/research.json:16-44` 的 5 个生产 prompt 均无该变量，故实际研究不会注入。 |
| 2 Memory & weighting | 3/2/2 | **3/2/2** | ☑ weighting、output memory lifecycle、cron；◔ 四域 memory 注入、digest endpoint→Step-0；☐ ad-hoc ask memory、user prompt override | `workflows.py:327-332,409-414` 接权重和 memory；`app/api/tasks.py:121-130` 的 ad-hoc ask 仅 `build_analyst_prompt`，没有 memory_block。该 Phase 的文档标记是少数准确区。 |
| 3 Fact-checking | 0/0/6 | **6/0/0** | 六项主能力均 ☑：抽取、核验路由、verified ledger、写作前检查、dispute/反馈、复用/standing knowledge 与 Vault callout | `factcheck.py:394-459,599-739,1006-1066`；`digests.py:149-199` 与 `exporter.py:315-372,481-537`。原来的 C1 交付末端已由 D3/D7 闭合。 |
| 4 Knowledge chain | 0/0/4 | **2/1/1** | ☑ entity/alias/edge 与自动聚类；◔ Vault projection/backlinks/footer/relations；☐ Obsidian Properties | `chain.py:675-730,899-1045` 覆盖候选与自动聚类；`:1100-1134,1333-1376` 覆盖实体投影和历史 footer。typed relation/所有新产物覆盖仍不完整，Properties 未落。 |
| 5 Forecasting | 0/0/4 | **3/0/1** | ☑ forecast extraction、paper book/outcome、SPA+MCP；☐ optional portfolios | `forecast_extract.py:512-635`；`paper_book.py:280-311`；`frontend/src/pages/Forecasts.tsx` 与 MCP forecast/book 工具存在。未发现 Portfolio 域模型/API。 |
| 6 Self-improvement | 0/0/4 | **1/2/1** | ☑ action router shadow；◔ actions kanban、triage page；☐ recipes/observations/proposals/effect | `operator.py:510-578` 和 `api/operator.py:222-292` 落 shadow+人工审批；`api/operator.py:117-196` 有后端 switches/triage，但注释明确 switches 未执行，`frontend/src/App.tsx:23-113` 无 operator route。`0018_operator_actions.sql:68-79` 只有 recipes schema placeholder。 |
| 7 Scaling | 1/0/7 | **2/4/2** | ☑ multi-agent primitive、Agy hand；◔ committee、projects、BFS tree、bilingual；☐ more hands、favorites/signals | `workflows.py:209-291` 委员会周幂等与 `multi_agent.py` 已落；`research_tree.py`、`projects.py`、`bilingual.py` 后端主干已落。`frontend/src/App.tsx` 无 tree/projects/bilingual 页面，Committee Vault/持久 group、tree viewer/retry/score、twin toggle/read API 均缺。 |
| 8 Hardening | 0/0/6 | **4/1/1** | ☑ launchd、CLI、contract、MCP expansion；◔ test coverage；☐ legacy migration（无语料跳过） | `app/cli.py:589-670`；`app/api/contract.py`；生产 MCP 35 tools。全量 764 很强，但前端 SSE 无自动测试，另有 7 个可消除 skip；legacy 无输入不应伪造完成。 |

### 4.3 未做项清单

明确跳过：Phase 8 **Legacy migration**，当前没有旧 Vault/frontmatter/event/admin_state 语料，保持 `☐/N/A` 合理。

其余 9 个 `☐`：

1. Phase 0：interactive asks 的 timeout/cancel/可恢复任务协议。
2. Phase 0：auth/session isolation。
3. Phase 2：ad-hoc analyst asks 的 memory injection。
4. Phase 2：用户提供 prompt override。
5. Phase 4：Obsidian Properties。
6. Phase 5：optional portfolios。
7. Phase 6：recipes → observations → proposals → measured effect 自改进链。
8. Phase 7：more hands。
9. Phase 7：favorites / signals。

13 个 `◔` 也不能按完成宣称：Phase 0 test gaps；Phase 1a similarity acceptance；Phase 1b DATA_BUNDLE 真注入；Phase 2 memory/digest 两项；Phase 4 Vault projection；Phase 6 kanban/triage；Phase 7 committee/projects/BFS/bilingual；Phase 8 test coverage。

---

## 5. `roadmap/backlog.json` 16 卡状态

结构统计：**16 cards = 15 done + 1 review**；无 inbox/ready/claimed/blocked 卡。

| 卡 | 状态 | 标题 |
|---|---|---|
| M0-001 | done | Research hand config and constrained fallback |
| M0-002 | done | Test research workflow uses configured hands only |
| M1-000 | done | Document and validate market-thesis-data import contract |
| M1-001 | done | Add thesis and market import schema migration |
| M1-002 | done | Implement thesis domain module and API |
| M1-003 | **review** | Import market-thesis-data bundle |
| M2-001 | done | Add security master schema |
| M3-001 | done | Extend research queue for thesis-aware tasks |
| M4-001 | done | Add market calendar, bars, and benchmark schema |
| M5-001 | done | Add forecast and settlement schema |
| M7-001 | done | Add roadmap schema and seed import |
| M7-003 | done | Build Kanban board UI |
| M7-005 | done | Add coding session tracking |
| M7-006 | done | Add global coding process and release gate views |
| M7-007 | done | Generate agent prompts from roadmap cards |
| M7-008 | done | Roadmap decisions, claim, export, and checklist/dependency CRUD |

M1-003 留在 review 是诚实状态：实现与 synthetic/subset 路径已存在，但真实 `market-thesis-data/bundle.json` 不在仓内，无法完成真实 dry-run/apply 验收。它不是“legacy migration”，也不应伪改为 done；需要补语料或明确取消该卡。

---

## 6. 四轮双审遗留立卡总清单

以下把 PATCH-NOTES 各卡的“后续/遗留/边界”和 REVIEW/F1-F3 的 nice-to-have 按语义去重。已由 D1-D7 明确关闭的旧项不重复伪列为遗留；来源标签用于追溯。

### P0 — 仍影响严格正确性或发布证明

| ID | 来源 | 当前遗留 | 状态/建议 |
|---|---|---|---|
| S4-P0-01 | R2 B1 / S2 | `_skip_add_column()` 只比较 type/NOT NULL/DEFAULT，不比较 `CHECK/REFERENCES`（`app/db.py:186-198`） | **PARTIAL MUST-FIX**。对崩溃后部分应用的含约束列，仍可能误补 ledger；需解析 `sqlite_master` 或直接拒绝无法证明的约束声明。 |
| S4-P0-02 | S4 skip audit / D6 | 6 个 subset import 测试被过宽 real-bundle marker 跳过；D4 restart recovery probe 仍可 skip | 拆 marker；用确定性 running tree fixture 替代探测式 skip。 |
| S4-P0-03 | F3/S4 cron | `/api/cron/health` 只列已有 metrics，不能证明 20 job 注册、gate 与 next run | 合并 scheduler registry，返回 registered/gated/schedule/last metric；前端不再硬编码。 |
| S4-P0-04 | ROADMAP / backlog | ROADMAP 29/58 状态失真；M1-003 缺真实 bundle | 更新 ROADMAP；为 bundle 补语料、外部路径契约或明确取消。 |

### P1 — 已有主干、应继续收敛

| ID | 来源 | 当前遗留 | 状态/建议 |
|---|---|---|---|
| S4-P1-01 | A1/F1 | retry 未持久化原 fallback/lineage，跨进程重试无幂等；极小 output cap 与 marker 的精确上限仍需锁定 | 设计 retry lineage/idempotency key；补边界测试。 |
| S4-P1-02 | A1/F1, B1 | shutdown 仍探测 APScheduler 私有 in-flight 结构；cron metric INSERT 失败/cancellation/版本漂移测试不足 | 暴露公共 `scheduler.inflight_jobs()`，加版本与故障注入测试。 |
| S4-P1-03 | A5/F1 | whiteboard board 事务失败可留下 orphan session/workspace；硬杀后 used claim 缺 lease/recovery | 建 claim lease/reaper，或扩大 session+board 原子边界。 |
| S4-P1-04 | A8/S1 | vector `mode` 不能区分健康零命中与降级；跨路径同内容不复用，旧 model 投影只隐藏不清理 | 增 degradation reason/health；内容寻址复用和旧模型 GC。 |
| S4-P1-05 | A2/A3/A4/A7/S1 | `app/mcp.py:807-811` 仍有“A2 补丁未落”过时注释；部分 README/CLAUDE/test mount 说明需复核 | 做一次只改事实的文档/注释清扫，并补 production `create_app()` route smoke。 |
| S4-P1-06 | B2/F2 | `hand-scorecard` 仍硬编码 00:05；权重预热/历史纠错投影可加强 | 增 `scorecard_time` 配置与 lifespan 集成测试。 |
| S4-P1-07 | B8/F2/S3 | sync ask 与 stream `_prepare` 仍为镜像实现；unknown hand/真实断线协议测试不足 | 抽共享 prepare helper；补断线、慢消费者和 unknown hand 合约测试。 |
| S4-P1-08 | B3/C8 | memory 只注入四个编排入口；ad-hoc tasks/ask_stream/sessions/MCP 未接；并发 compact 可重复烧模型 | 先裁决统一入口，再实现 memory 注入与 compact claim；保留可观测失败。 |
| S4-P1-09 | B5/S3 | `${DATA_BUNDLE}` 机制存在但生产 research prompt 未引用；refresh race、缓存一致性、benchmark fetch/降级不完整 | 先在获准的 prompt 版本加入变量，再做 benchmark/race 测试。 |
| S4-P1-10 | B6/S3 | forecast scheduled seeding、批量 settlement、历史 export、risk/cap 严格执行未完整 | 拆为 seeding、settlement reconciliation、risk enforcement 三卡。 |

### P2 — Phase 3–8 产品与可靠性补全

| ID | 来源 | 当前遗留 | 状态/建议 |
|---|---|---|---|
| S4-P2-01 | C1/S3 | factcheck disputed handler 仍 best-effort，无 durable outbox；extract/verify hand、daily hook、阈值校准、历史向量 backfill与 parser 对抗集可加强 | outbox+幂等重试；独立手配置；扩生产形态 parser oracle。 |
| S4-P2-02 | C2/S3 | hostile wikilink/display（`\|`、换行、`]]`、HTML）、全角 `｜`、relation grammar 与 live event durable replay 未完整 | 先定义编码/拒绝契约，再补关系展示和 replay。 |
| S4-P2-03 | C3/S3 | settlement/账本历史 reconciliation、benchmark base 缺失、stopword/简称、历史 MTM/补价重算、forecast Vault 导出未完整 | 建可重复 reconciliation 命令和历史投影；补严格 API/config 类型。 |
| S4-P2-04 | C3/S3 | `_usable_price/_adj_close` 跨模块口径重复；forecast extraction prompt 未注入 standing memory | 提升公共价格 helper 并做共享契约测试；单独裁决抽取 prompt 的 memory。 |
| S4-P2-05 | C4/F3 | feature switches 只存储/展示且 PUT 非 CAS；operator SPA/人工确认流未交；Vault 私有 helper 耦合、human auth 边界和 shadow 退出策略未定 | 在 unshadow 前完成 enforcement+CAS、前端、人类身份边界与审计。 |
| S4-P2-06 | C5/S3 | multi-agent 无持久 group/run；committee 无 Committee Vault/输入快照；workflow 输出文件不是引擎契约；majority verdict 仍偏 free-text | 统一 group/run/file/verdict 持久协议，支持重连和部分 spawn 恢复。 |
| S4-P2-07 | C5/S3 | API 422/400、prompt 长度、50-board、七天边界、查询降级契约未统一；ROADMAP committee 时间描述需同步 | 建 API contract tests 并统一文档。 |
| S4-P2-08 | C6/S3 | launchd 已安装但未做长时间 install/restart/stop/uninstall soak、日志轮转和失败保留 plist 验证 | 运行独立 soak；不要在本次只读审计中伪造结果。 |
| S4-P2-09 | C6/S3 | doctor 缺稳定 machine-readable schema/version、真实损坏 SQLite、cron stale/missing 与更多 running 域测试；未知 CLI auth 仍只能 unknown | 增版本化输出和 fixture；为 hand provider 建 probe 协议。 |
| S4-P2-10 | C7/S3 | `useSSE` 无 bootstrap/分页/重连/ring eviction/watchdog 自动测试；事件分组 stale 条件、未来 `ago()` 与错误呈现仍可收敛 | 引入前端 test runner 后锁游标状态机和 UI 边界。 |
| S4-P2-11 | C8/S3 | 缺 50+ capability/event 配对 sanity、完整 prompt/task 快照；部分“所有 prompt”式文案仍需持续防回归 | 加生成式清单测试与 OFF 快照，或收窄 byte-for-byte 承诺。 |
| S4-P2-12 | C6/S3 | FastAPI lifespan、Pydantic v2、timezone-aware datetime、httpx transport 弃用债 | 分批消除 warning，不与功能补丁混改。 |

### P2 — D4/D5 新能力边界

| ID | 来源 | 当前遗留 | 状态/建议 |
|---|---|---|---|
| S4-P2-13 | REVIEW/PATCH-D4 M1 | child replay 只对 BINARY exact-topic 幂等；非确定重跑或人工重驱可生成语义重复分支 | 存 normalized child key，或正式把 exact-topic 语义写进 API/运维契约。 |
| S4-P2-14 | PATCH-D4 | BFS 节点失败无自动 retry，`score` 预留未写，无 ranking、SPA viewer、weighted research-hand 分支 | 拆 retry policy、scoring/ranking、viewer 三卡；不要靠人工改 completed 父。 |
| S4-P2-15 | PATCH-D4 | 每日 booked counter 不清理；daily-cap 仍 200 refused 而非 429；同 root tree Vault 后完成覆盖 | janitor 清 30 天前 counter；HTTP 语义与一树一档路径作为产品决策。 |
| S4-P2-16 | REVIEW/PATCH-D5 | Projects 无 SPA，archive/unlink 无 API；当前只是附件容器，非目标/里程碑/证据链/周报的长期项目 | 先补操作 API/UI，再扩项目语义。 |
| S4-P2-17 | PATCH-D5 | MCP `research_queue_add` 无可选 `project_id`；项目读工具存在但写侧不能挂项目 | schema 增 optional project_id 并透传，保持 3 写工具边界不变。 |
| S4-P2-18 | REVIEW/PATCH-D5 M5 | bilingual 无 admin API/SPA locale toggle/按 run+locale 完整读取端点；失败不重试、事件可重放重复 | 建 twin 索引或稳定读取 API、开关 UI、补发/幂等策略。 |
| S4-P2-19 | PATCH-D5 | events 表无全局 retention；twin 已改引用式但长期事件增长问题仍在 | 在 janitor 增按年龄/保留策略，先定义审计留存需求。 |

### P3 — ROADMAP 仍未开始的产品项

| ID | 来源 | 当前遗留 |
|---|---|---|
| S4-P3-01 | ROADMAP Phase 0 | interactive ask cancellation/timeout；auth/session isolation |
| S4-P3-02 | ROADMAP Phase 2 | user prompt override |
| S4-P3-03 | ROADMAP Phase 4/5 | Obsidian Properties；optional portfolios |
| S4-P3-04 | ROADMAP Phase 6/7 | recipes self-improvement；more hands；favorites/signals |

---

## 7. 交付质量指标与终版计分卡

### 7.1 规模增长

| 指标 | 起点 | 终点 | 增量 | 增长 |
|---|---:|---:|---:|---:|
| pytest passed | 93 | **764** | +671 | **8.22×，+721.5%** |
| migrations | 0001 | **0022** | +21 files | **22 files / 22 生产已应用** |
| MCP tools | 15 | **35** | +20 | **2.33×，+133.3%** |
| scheduler jobs | 8 | **20** | +12 | **2.50×，+150%** |
| backlog done | — | **15/16** | 1 review | **93.8% done** |

调度终态是 14 gated / 6 ungated；生产健康接口的 13 是“已有 metric 数”，不能替代 20 注册数。

### 7.2 四轮 must-fix 核销率

计数采用两位轮级审查代理明确标为 must-fix/阻断门的**唯一项**；D2/D3 是第三轮原项的后续核销，不重复计数。F3 明说“无新 must-fix”，其 P2/P3 虽已由 D1 修复，未膨胀计数。并行 F4 若后续新增 must-fix，应在其报告后追加，不预占本表。

| 轮次 | 来源 | 唯一 must-fix/阻断门 | 当前 FIXED | 当前 PARTIALLY |
|---|---|---:|---:|---:|
| Round 1 | S1（F1 无新 must-fix） | 14 | 14 | 0 |
| Round 2 | S2 25 + F2-H1 1 | 26 | 25 | 1 |
| Round 3 | S3 30（F3 无新 must-fix） | 30 | 30 | 0 |
| Round 4 | R-D4 3 + R-D5 2 | 5 | 5 | 0 |
| **合计** |  | **75** | **74** | **1** |

- 核销覆盖率：`75 / 75 = 100%`。
- 严格完全关闭率：`74 / 75 = 98.7%`。
- 未完全关闭：S4-P0-01，B1 ADD COLUMN 恢复对 `CHECK/REFERENCES` 的证明缺口。

### 7.3 终版计分卡

| 维度 | 结果 | 终审判断 |
|---|---|---|
| 第四轮指定整改 | 15/15 FIXED | **A / PASS** |
| 全量测试与构建 | 764/10，compile/build/shell 全绿 | **A- / PASS**；扣分来自 7 个可消除 skip |
| 生产运行 | health、35 MCP、launchd running、22 migrations | **A- / PASS-WITH-GAP**；cron GET 不展示未触发注册项 |
| 四轮正确性债 | 74 FIXED + 1 PARTIAL / 75 | **A-**；仍有一个迁移恢复证明缺口 |
| ROADMAP 实际完成 | 35 ☑ / 13 ◔ / 10 ☐；半权 71.6% | **B- / GO-WITH-BACKLOG** |
| 控制面/文档可信度 | ROADMAP 29/58 状态不符；backlog 15/16 | **C+**；backlog 较可信，ROADMAP 需立即同步 |
| 交付增量 | tests 8.22×、migrations 22、MCP 35、jobs 20 | **A** |

**总评：第四轮修复闭环和生产升级主路径可放行；15 小时工程完成的是一个经过显著硬化、可运行的后端主干，而不是 ROADMAP Phase 0–8 的全部产品终态。下一步应先清 S4-P0 四项，再从 13 个 `◔` 中优先补 DATA_BUNDLE 真注入、operator/tree/projects/bilingual 产品面与前端状态机测试。**
