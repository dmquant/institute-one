# ROUND5-AUDIT-F5 — 第五轮（E 轮）fable 侧终审 · F5b 终版

- 日期：2026-07-20（F5b 完稿 ~15:40 SGT）
- 审计代理：F5b（接替 14:08 连接故障的 F5；F5 于 15:17 自行恢复并交付初版，本文件为吸收其结论后的终版）
- 范围：E3 生产 prompt 变更质量（最高优先）、E6 auth/cancel、E1 sqlite_master 证明、E7 recipes 铁律、全局不变量终扫、收官裁决
- 方法：只读深审 + 独立实测（构造刁钻案例跑真代码、全量测试套件独立复跑、生产端点冒烟）

## 结论：**PASS**

- 全量测试独立复跑：**825 passed / 4 skipped，零失败**（67.9s；4 skip = 真网 ×1 + 外部 thesis bundle ×2 + 真实校准台架 ×1，全部合理外部依赖，达 E1 的 ≤3+1 目标口径）。
- 生产（launchd 常驻，pid 存活）：`/health` ok、cron health **20/20 registered 零 failing**、digest 端点正常。
- F5 初版（15:17）结论 PASS-WITH-NITS，3 个 NIT（NIT-F5-1/2/3）已由主代理于 15:20 全部修复落地，**本审计逐项独立复验通过**（见 §1.4）。
- F5b 新发现：**0 个 must-fix**，3 LOW + 2 观察（§8）。五轮工程整体收敛干净，硬规则零违例。

---

## 1. E3 生产 prompt 变更质量（最高优先）——PASS

### 1.1 措辞忠实 PATCH-NOTES-B5 §3 建议 ✅

对照 `PATCH-NOTES-B5.md:55-60` 与 `workflows/research.json` 现状逐字核对：

- 03 步搜索句改写逐字命中 B5 建议与 ROADMAP "replacing please web-search with grounded numbers"：旧句「请使用联网搜索（如当前 CLI 支持）核实。」全文 0 残留，新句「优先使用上方已注入的本地行情数据；数据缺失的部分再联网搜索核实。」恰 1 处（03 步内）。
- `${DATA_BUNDLE}` 恰好出现在 01/03 两步，02/04/05/06/07 零引用；`variables` 数组四元组补登 `DATA_BUNDLE`。
- **01 与 03 现均为裸变量**（NIT-F5-1 修复后）——B5 §3 原文明示 bundle 渲染自带「【行情数据注入】…」头部、建议只写裸变量：修复后的形态比 E3 初版（03 带段头）更严格地忠实 B5，空数据渲染空串即无痕降级，孤立标题与双标题问题一并消失。
- 01 步的联网搜索句（「获取并核实最新资料」措辞）未动——卡指令只点名 03 的那句，最小授权纪律成立。

### 1.2 Step-0 curl 指引对 CLI 手语义正确 ✅

- 5 个分析步（01–05）的 Step-0 段**逐字节相同**（实测 count=5，便于将来整体替换）；06/07 全文不含 `recent-reports`。
- URL 语义逐项验证：`/api/institute/recent-reports.md?days=7` 与 `app/api/digests.py:31` 的真实路由匹配；单引号防 zsh glob；`curl -s` 静默；显式失败降级句（「若命令失败则忽略，直接开始」）——CLI 沙箱禁 curl / 服务器未起 / **auth token 设置后 401**（见 LOW-F5B-2）三种失败态都被该降级句兜住。
- 端点姿态匹配：digests router 永远 200 + markdown（`app/api/digests.py:10-13`），failing curl 不会向 prompt 注入错误页。
- hand 类型判据核实：`_workflow_hand_policy`（`app/institute/workflows.py:322-333`）对 research 完全忽略 analyst.hand、在 `settings.research_hand_names` 内轮转（生产 `.env` = `codex,openai-api`）。注意该链含 **openai-api（api 型手）**——PATCH-NOTES-E3 §2 说「7 步全部落在 CLI 手上」以生产默认 `codex,agy` 立论，与本机 .env 不符；但 Step-0 是 prompt 文本，api 手收到后按文本忽略或复述，无执行语义危害，判据的「步骤性质」口径（分析步 vs 汇编步）独立成立。

### 1.3 06/07 不加的论证 ✅

- `06-report` prompt 明令「不得新增事实」、唯一输入是工作目录 01–05 文件——curl 外部上下文与任务矛盾，论证成立。
- `07-followups` 输出受严格 JSON 结构约束、digest 不含白板议题——增加噪音风险、无重复跟进防护收益，论证成立。
- 实测两步 prompt 零 Step-0 / 零 bundle 引用；`briefing/daily/committee` 工作流 prompt 正文逐字未动（git diff 仅 A4 轮键归一化，见 §6.2）。

### 1.4 回滚 diff 可用性 ✅（NIT-F5-1/2 修复后重验）

按 PATCH-NOTES-E3 §5（更新后：9 处，03 按裸变量形态）顺序 1→5 在现文件上模拟回滚：

- 各查找串命中次数全部恰如预期（1/5/1/1/1；op4 依赖 op2 先删 Step-0 段——§5 顺序执行语义，成立）。
- 回滚结果与 `git show HEAD:workflows/research.json` 的差异**恰为 07 步 `analyst`→`analyst_id` 两行**——正是 §5 警告「不要整文件 git checkout 回滚」要保护的先前轮次键归一化。回滚说明精确可用。
- 测试同进退口径已写明（test_workflows 2 例锁新措辞需删、test_ask_memory 与代码三处同退）。

### 1.5 变量惰性求值触发正确 ✅

`app/institute/workflows.py:354-372`：仅当「某步 prompt 含 `${DATA_BUNDLE}` 且调用方未显式传值」时惰性计算；持久化用 `json_set` 单键写入 + `status='running'` 守卫（并发保全 REVIEW-C5 P2 语义 + 终态行不可变）；异常渲染空串永不 raise。`${WEEK_DISPUTES}` 同款契约未被 E3 扰动。端到端测试（`tests/test_workflows.py:222-257`）用生产 research 定义真跑 7 步：bundle 持久化到 `run.variables`、变量零残留、bundle 恰达 01/03、Step-0 恰达 01–05——**echo 回显防污染的「## 任务」节切分断言**处理了 previous-steps 块回显干扰，测试真实不掺水。

### 1.6 memory 注入三面 + prepare_ask 收敛 ✅

- 三 ad-hoc ask 面（`app/api/tasks.py` prepare_ask、`app/api/sessions.py:74-77`、`app/mcp.py:840-843`）全部注入 `memory_block`；`build_analyst_prompt` 对空块严格 no-op（`app/institute/prompts.py` 唯一 diff 是加 `memory_block` 参数——**prompt 模板字符串零字节变更**，B3 授权范围内）。
- E3 §3d 曾如实报告 ask_stream 镜像未生效；E6 落地 B8 的 `prepare_ask` 共享提取后 `app/api/ask_stream.py:49` 改 import 复用，`ask_stream` 与同步 ask 恢复 lockstep，行为分叉面归零。诚实交接 + 后续闭环的范本。

## 2. E6 auth middleware——PASS（1 LOW）

- **纯 ASGI 正确性**：非 http scope 直通；放行分支 `await self.app(scope, receive, send)` 原样透传（零响应包装，SSE/NDJSON 无缓冲语义保全）；401 用独立 JSONResponse 现场构造。scope 过滤逐项验证 ✅。
- **compare_digest**：`secrets.compare_digest(provided, f"Bearer {token}")` 恒时比较 ✅。实测：正确 token 200 / 错误 401 / 缺失 401 / **latin-1 高位字节 header 抛 TypeError → 500**（LOW-F5B-1：未授权者不会被放行，fail-closed 成立，但异常路径不如显式 401 干净）。
- **豁免面一致性**：`_EXEMPT_PATHS = ("/health",)` 精确匹配 + 非 `/api/` 前缀豁免。`/apifoo` 不误豁免（startswith("/api/")），SPA 壳/assets 豁免与 PATCH-NOTES-E6 §2 行为矩阵一致；`scripts/stop.sh` 纯 pidfile 不打 API，无交互。token 未设时逐请求直通=零变化 ✅；非环回绑定无 token 的一次性 WARNING 在 `install_auth`（配置态告警）✅。
- **挂载**：曾出现双 `install_auth`（主代理与 E6 落盘撞车，本审计 14:15 目击双层 middleware 堆叠实测确认）；S5b 发现后主代理 14:20 去重，现 `user_middleware` 恰 1 层 ✅。
- `tests/test_auth.py` 6 例矩阵完整（零变化/空 token/方法全覆盖/豁免/双向警告断言）。

## 3. E6 cancel 顺序修复——PASS

`app/router/executor.py:347-400` 三段协议逐项验证：

1. **queued 先条件翻库再唤醒**的顺序正确性：翻库（条件 UPDATE `status='queued'`）成功后行已终态，此时再 `atask.cancel()` 唤醒停在锁 await 的 submit——CancelledError 打在 `async with _sem(), _hand_lock()` 处（保护区外），无 `_finish` 需要执行，行不会复活；若不唤醒，任务最终拿锁后 `claimed==0` 分支（`:201-202`）自然退出。**反向顺序（先 cancel 后翻库）正是旧实现 queued-with-live-task 永卡 bug**——CancelledError 在锁 await 处无人落库。两条直接翻库路径补发 `task.cancelled` 事件与 `_finish` 对齐 ✅。
2. running → `_running` 的 atask.cancel()，复用 A1 停机路径已验证的 CancelledError→_finish('cancelled')+进程组击杀机制 ✅。
3. 幽灵 running 行（无活 task）直接翻库兜底 ✅。

**复现测试真实性**：`tests/test_ask_priority.py:195-219` 持真实 hand mutex（`_busy` 拿 executor 私有锁）+ 真 spawn 排队 + API cancel + `asyncio.gather` 验证被唤醒非锁死 + 锁释放后 sleep 复查行不复活——复现的是真锁竞争，非 mock 剧场。running 取消用真 HangingHand（`:222-238`）、终态 409 幂等 + 竞态窗口 409 + 未知 404 契约齐全。API 层 check-then-cancel 的竞态窗口由 `executor.cancel` 返回 False → 409 收敛（`app/api/tasks.py:58-62`）✅。

## 4. E1 sqlite_master 证明——PASS

**构造 11 个刁钻案例实测 `_norm_def`**（引号内逗号/括号空白/字符串字面量大小写/关键词大小写/`+1` vs `1`/尾随注释/引号 vs 裸标识符/块注释分隔/DEFAULT 内分号/FK 空白），全部符合「宽进等价、保守拒绝」预期：注释丢弃、空白仅在双词字符间保留单空格、非引号文本 casefold、**引号 span 逐字保留**（字符串字面量大小写差异判不等、`"mode"` vs `mode` 判不等——保守方向正确，宁可误报 drift 也不误放）。

- `_table_column_defs` 刁钻 CREATE TABLE 解析：引号列名（含内嵌逗号字面量）/GENERATED 列/表级 CHECK/CONSTRAINT FK 跳过，全部正确提取；解析失败返回 None → 调用方按 UNPROVEN 处理，`_UNPROVABLE_CONSTRAINT_RE` 兜底拒绝证明（`app/db.py:379-387`）。fts5 虚表被解析为空声明列而非 None——不可达场景（SQLite 本身拒绝对虚表 ALTER ADD COLUMN，崩溃重放前提不存在），无风险。
- 不等即 `MigrationRecoveryError` 拒绝启动（不静默记账）；unbalanced quotes 落回 SQLite 自身报错 ✅。`tests/test_db_migrate.py` 19 例覆盖双向反例（声明有 CHECK 现列没有 / 现列有 CHECK 声明没有 / CHECK 文本漂移 / FK 目标漂移），S4-P0-01 关闭成立。
- **skip 拆分（S4-P0-02）**：4 skip 全部为条件外部依赖（INSTITUTE_NET_TESTS / INSTITUTE_THESIS_BUNDLE ×2 / INSTITUTE_CALIBRATION_REAL），过宽 marker 已死；D4 restart-recovery 探针改为**域函数构造的确定性 fixture**（`tests/test_restart_recovery.py:196-243`：真 create_tree + 真 claim + stop 竞态中间态 + lifespan 重启断言 running 零残留、live 树 requeue、终态树 prune）——旧反射盲插探针的永 skip 已消除。

## 5. E7 recipes 铁律——PASS（1 LOW）

- **命中零模型调用仍 shadow=1**：`route_actions`（`app/institute/operator.py:679-714`）recipe 命中路径零 `executor.submit`、disposition INSERT 的 shadow 位是 SQL 字面量 `1`（`:701`）；全模块无 `shadow=0` 写路径。测试以 **tasks 行数不变**锁死零模型调用（`tests/test_operator.py:713-769`），并验证命中/未命中双路径、继承 confidence、flags 照算、仍占 propose-once 槽位（0022 索引语义）。
- **promote 只收 approved**：`promote_disposition_to_recipe:450-456` 检查 flags 含 `approved` + 词表校验拒 `unparsed`；`approved` flag 全仓唯一写入点是 approve 端点事务内（`app/api/operator.py:266,286-288`）——人工门延伸到 recipe 知识成立。关键词提不出 fail closed（空关键词 AND 语义=全匹配，拒绝过宽）；幂等靠 0023 部分唯一索引 + IntegrityError 收敛。
- **recipe 继承 confidence 过 approve live floor 门的语义**：链路自洽——被 promote 的 disposition 必已过当时 floor（approve 门拒 missing/below-floor，故 recipe.confidence 非 NULL）；recipe 命中产生的新 disposition 继承该值，approve 时**重新对 LIVE floor 复查**（`app/api/operator.py:250-258`）：floor 上调追溯拦截旧 recipe 建议（409 + 人工 PATCH 通道），下调解锁 flagged 建议。`disposition_flags` 的 low_confidence 是时点缓存不是门——两层语义与 F3 P3-1 裁决一致。retire 条件认领（仅 active，重复 409）为唯一治理开关 ✅。
- 0023 纪律：ADD COLUMN 全部无 CHECK/REFERENCES（与 §4 守卫的可证明恢复路径协调，迁移文件注释写明取舍）；唯一索引 IF NOT EXISTS 幂等 ✅。
- LOW-F5B-3：`_match_recipe` 关键词为 **substring 匹配无词边界**（`:521`，如关键词 `ai` 可命中含 `repair` 的标题）——与 PATCH-NOTES-E7「宁 miss 不误命中」的措辞方向有出入；缓解链完整（全关键词 AND + 同 kind + shadow=1 + 人工门 + retire），后果上限=一条待人工复核的建议。M8-008 扩面时加词边界即可。

## 6. 全局终扫——全绿

1. **prompts.py 零字节**：git diff 唯一变更 = `build_analyst_prompt` 增加 `memory_block` 参数与插入逻辑（B3 授权）；PROMPT 模板字符串/persona/CITATION_MANDATE 零字节变更 ✅（硬规则 4「never paraphrase existing prompt strings」逐字成立）。
2. **workflows/*.json 只有授权变更**：research.json = E3 卡 + NIT-F5-1 修复（均有 PATCH-NOTES 追溯）；briefing/daily/research-07 的 `analyst`→`analyst_id` 为 A4 轮键归一化（F2 已裁决已知）；committee.json 为 C5 新增卡。无未授权 prompt 措辞变更 ✅。
3. **MCP 写面恰 3**：`WRITE_TOOLS = {research_queue_add, topic_pool_add, institute_ask}`（`app/mcp.py:41`）；`tests/test_mcp_roundtrip.py` 双向守卫（写面恰 3 + 每个注册读工具零写断言 + 新工具无烟测表即 fail）✅。
4. **迁移纪律**：0001-0004（tracked）git diff 零变更；0005-0023 编号唯一无空洞冲突；全链无 BEGIN/COMMIT/ATTACH/VACUUM（测试强制）；两路实测等价（`test_split_statements_matches_executescript_result` 含 0023 在内全链过）；生产 23/23 已应用 ✅。
5. **drain vs create_task**：全仓 9 处 `create_task` + submit 的 `ensure_future` 逐一核对——8 处入模块注册表（executor._running / workflows._driving / whiteboard / mailbox / analyst_daily / research / archive / bilingual 的 _bg_tasks）+ analyst_daily 心跳 task 为结构化并发（run_all finally 内 stop+await 收口）；`_drain_background` 两轮清扫 + scheduler inflight 快照前置 + 异常消费 ✅。ask_stream 的外层 submit_task 随内层 _running task 的取消级联结束、done-callback 消费异常，无泄漏。
6. **事务不变量抽查 5/5**：A5 `whiteboard._open_board` COMMIT 异常边界+议题释放路径在位（`whiteboard.py:485-567`）；A7 PIT 版本行不可变（DO NOTHING+rowcount+重放比对，`market_data.py:344-406`）；B3 region 五规则（字节级指纹/ownership 强制/fresh sibling 永不复用，`vault/writer.py:93-108,360-418`）；B6 entry `as_of=made_at` 前视冻结（`forecasts.py:28,355,391`）；D4 树完成路径单事务+事件 post-commit（`research_tree.py:614-620`）✅。
7. **生产状态**：launchd 常驻实例 `/health` ok（uptime 跨 E 轮重启）、cron health **20/20 registered、零 failing job**、digest/recipes 端点冒烟过 ✅。

## 7. F5 初版三 NIT 的核销复验（15:20 主代理修复）

| # | 内容 | 复验 |
|---|---|---|
| NIT-F5-1 | 03 步「【本地行情数据】」段头空数据留痕/非空双标题 | ✅ 03 已改裸 `${DATA_BUNDLE}`（与 01 同形态，B5 §3 原始建议）；test_workflows.py:185-193 断言双步无标题并注明来源 |
| NIT-F5-2 | 回滚说明"8 处"实为 9 处 | ✅ PATCH-NOTES-E3 §5 已改"9 处"并注明 03 按裸变量形态查找；回滚模拟余差恰为 07 键归一化两行（正确保留） |
| NIT-F5-3 | operator.py docstring 含字面 `shadow=0` 干扰机械检索 | ✅ 接受为非问题（说明性文档；可执行写路径零 `shadow=0` 是本审计§5 的语义验证结论，机械 grep 口径不作为门槛） |

## 8. 问题分级（F5b 新发现，全部非阻断）

- **LOW-F5B-1** `app/api/auth.py:76-81` — Authorization header 含非 ASCII 字节（latin-1 解码合法）时 `secrets.compare_digest` 抛 TypeError → 500。fail-closed（绝不放行）但异常路径不如显式 401 干净。修法一行：比较前 `provided.isascii()` 预检或 try/except 归 401。
- **LOW-F5B-2** `PATCH-NOTES-E6.md §2` 连带影响清单 — 列了 SPA/plugin/MCP 需带 header，**漏列 E3 Step-0 curl 与 digest 端点「永远 200」承诺在 token 模式下失效**（研究 prompt 将静默失去 digest 上下文；Step-0 降级句可兜底不炸 prompt）。当前生产 token 未设=零影响；文档补一行 + M8-019 后续卡处理。
- **LOW-F5B-3** `app/institute/operator.py:521` — recipe 关键词 substring 匹配无词边界（见 §5）；缓解链完整，M8-008 加词边界。
- **OBS-1** MCP `institute_ask` 不经 `prepare_ask`（无空闲手偏好/无 body.hand 钉死语义）——E6 授权面只含 ask/ask_stream，行为一致性观察非缺陷；若要统一，后续卡把 MCP 面也接 prepare_ask。
- **OBS-2** PATCH-NOTES-E3 §2 hand 判据引生产默认 `codex,agy` 立论，本机 `.env` 实为 `codex,openai-api`（含 api 型手）——Step-0 对 api 手无害（纯文本），判据第二口径独立成立，仅文档时点性偏差。

## 9. 收官裁决：**巩固收尾，不开第六轮**

现在 ~15:40，deadline 18:00，实际剩余 **~2h20m**（派发任务书的"约 3.5h"按 14:08 派发时点估计，已过去 1.5h）。建议与理由：

1. **一轮完整分区的最小周期 ~1.8h（E 轮实测 12:16→14:05）且假设零返工**——五轮经验里分区首审 FAIL 率约 70%（第一轮 8 分区 6 FAIL、第二轮 6 FAIL），返工是常态。2h20m 内开第六轮 = 大概率在 deadline 上撞出一个半闭环状态，违背「路线图完成或 15h 先到者」的收敛精神。
2. **当前是干净封版点**：825/4 零失败、23 迁移、20/20 job、launchd 常驻自动恢复、五轮审计链完整（10 份轮审 + 24 份分区审查 + 23 份 PATCH-NOTES）、ROADMAP 终版 39☑/12◔/7☐ 对账严格、backlog 36 卡台账健全。M8 高价值卡（003 durable retry / 006 operator 收敛 / 008 recipes 全环 / 014 forecast 强制）全部涉及核心事务或新迁移——最后两小时动这些的边际收益 < 打破封版的风险。
3. **收尾清单有实事**（预算 ~1h，主代理直做，零新面零迁移）：
   - `roadmap/backlog.json`：M8-019 标 done（E6 已交付其全部内容：cancel 协议+hand_busy+INSTITUTE_TOKEN；卡是 E8 预立）。
   - LOW-F5B-1 一行修（isascii 预检）+ LOW-F5B-2 文档一行——可选，10 分钟内完成才做。
   - implementation-notes.md 尾部 digest：Deviations 与 Open Questions 汇总（交接协议要求），点名 M1-003 外部路径契约、stream-ask 无记忆期已闭环、8100 三次静默死亡的 launchd 根治。
   - 全量最终复跑一次 + kickstart 后冒烟五项，封版。
4. 若主代理仍想消化剩余时间，唯一建议的开发动作是 **M8-020（deprecation/stale-comment sweep）单卡**：零迁移、零新 API 面、纯清扫可随时中止，16:45 硬停线，审查走轮内快审。

## 10. 验证记录

- 全量：`.venv/bin/python -m pytest tests -q -rs` → **825 passed / 4 skipped / 0 failed**（67.85s，独立复跑）。
- 实测脚本：`_norm_def` 11 边界案例、`_table_column_defs` 刁钻 DDL、auth middleware 4 态请求矩阵（含非 ASCII header）、E3 回滚 9 处顺序模拟 vs HEAD 比对、双 install_auth 堆叠复现（修复前）。
- 生产：`/health` ok、`/api/cron/health` 20/20 registered 零 failing、`recent-reports.md` digest 正常。
- 静态：git diff 全面核对（prompts.py/workflows/migrations 0001-0004/写面）；MCP WRITE_TOOLS 恰 3；drain 9+1 处全覆盖；事务不变量 5 处抽查。
