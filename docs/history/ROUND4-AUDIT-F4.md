# ROUND4-AUDIT-F4 — 第四轮轮级终审（跨分区交互 / 全局不变量 / I4 集成层 / 未送审分区抽查）

审查日期：2026-07-20
审查代理：F4（fable，深度审查，只读；与 S4 并行——全量 764/10 复核归 S4）
审查范围：D1/D2/D3/D6/D7 抽查、调度终版 20 job、bus 事件面第四轮增量、drain 终版、迁移链 0001-0022、硬规则终扫、I4 三处适配差异、I4 遗留裁决点。

## 结论：**PASS-WITH-NITS**

无 must-fix。5 个 nit（全部 LOW / 记录级），1 个裁决点已裁（修测试，下轮落地即可，当前 skip 无生产风险）。

本轮定向实测：13 个第四轮相关测试套件 **213 passed / 1 skipped**（唯一 skip 即裁决点探针）；迁移链两路真实空库实测通过；生产库 22/22 已应用、`/health` ok。

---

## 一、未送审分区抽查（5/5 通过）

### D2 — forecast_extraction_items 状态机（对照 REVIEW-C3 M2 原始要求）：PASS

- `0019` 落地 `status ('pending','complete')` 且**老行默认 complete**（防旧行被 resume 重抽，0019:17-20），`forecast_extraction_items` 以 `PRIMARY KEY (extraction_id, security_id)` 做候选级仲裁 + `forecast_id` 部分唯一索引（一 forecast 恰一候选槽）。
- **in-doubt 保守跳过判定为正确**：claim 落库后、`forecast_id` 回填前的崩溃，系统无法区分「create 已提交」与「create 未发生」（forecast 存在但无指针 vs 根本不存在，无可靠反查键），fails-closed（宁可漏、绝不重）是与 REVIEW-C3「不接受把未知当已知」精神一致的取舍；`detail` 报告 + 手术式操作员通道（查 forecasts → DELETE 单 item → flip pending → replay）在代码 docstring 与 0019 注释双处文档化（forecast_extract.py:532-540）。
- `ForecastError` 候选**释放 claim**（refusal ≠ doubt，:603-611）——确定性拒绝可在后续 resume 重评，不会积累假 doubt。
- 两个崩溃窗口都有真实故障注入测试（test_forecast_extract.py:369-471：候选边界崩溃 resume 恰补缺失、create 后回填前崩溃不复制），`forecast.extracted` 每 source 恰一次有断言。
- M1 同名仲裁（canonical 锚定抑制 siblings、无锚拒绝+计数）、M4 三层否定（紧邻/8 字符 advisory 窗+CJK lookbehind/问号否答跨 split）、M5 归因（最后非 ops 步骤，fails-closed 到 NULL）代码逐一复核与 PATCH 声明一致；`2026年内` 双 pattern（数字年 cap=3 拒绝 + 静态 `年内` 前置数字拒绝）都封死，`未来2周` 最短胜。

### D3 — reproject_footers 人工编辑语义：PASS

- `state='conflict'` 行**直接计数返回、永不触盘**（chain.py:1293-1294）；region 模式三重验证（恰一对标记 + ownership + region sha256 与台账一致）任一不符 → `conflicts` 不写（:1298-1305）；full 模式文件 sha 不符 → `conflicts` 不写（:1312-1313）。与 writer 的 never-clobber/hash-ledger 语义完全同构——即使走到 `write_note`，人工编辑也只会落 conflict-sibling，`_reproject_one` 把非原路径返回同样计为 `conflicts`（:1330）。
- 重算 footer 相同 → skip（重复清扫免费）；cap 只计 reprojected（写量守卫，:1366）；单条异常隔离不断清扫（:1370-1372）。

### D6 — MCP 写工具守卫：PASS

- 35 工具注册；`WRITE_TOOLS` frozenset 恰 3（mcp.py:41）。**双向**守卫成立（test_mcp_roundtrip.py:163-177）：①写集合恰等三个已知名字；②其余全部工具必须出现在 SMOKE 读表——新变异工具无法以「未列名读工具」身份混入。
- 读面零写探针：13 张可变域表 before/after 计数相等（:277-298）；8KB 输出钳制 + 空库全读面冒烟在位。

### D7 — 插件 fetch/requestUrl 取舍：PASS

- 全插件以 Obsidian `requestUrl` 为主（CLAUDE.md 规则）；`askstream.ts` 是**唯一**有意 fetch 例外，取舍成立：requestUrl 缓冲整响应、结构上无法消费 NDJSON 流；CORS 强制环境下 fetch 对无 CORS 头后端抛 TypeError → **自动回落**同步 requestUrl 路径（askstream.ts:230-238），404/405/501（旧后端）同路回落；中途断流**不重放**（任务已在服务端提交，:286-290 防双跑）。CORS 实测结论与代码注释一致、边界完备。

### D1 — _fold_line 三层封堵（F3 P2-1 探针固化）：PASS

- ①源头：`open_action` title 折叠、**ref 含控制字符直接 ValueError**（operator.py:159-173）；②入口：feeds 的 `_payload_str` 折叠一切 payload 提取（:215-222）；③插值：`build_router_prompt` 对 title/ref 再折一次（防 pre-hygiene 旧行/旁路 writer）+ `_quote_detail` 行引用化 + 协议行冒号拆断双保险（:436-460）。
- approve 门 **live floor 复核**（api/operator.py:250-258）：存储 flags 降级为 proposal-time 缓存，消费时对 live floor 重查，missing confidence 永不过门（F3 P3-1 闭环）；approve 事务原子（action 认领 + disposition flag 同落同滚）。
- `0022` 部分唯一索引 + `route_actions` IntegrityError 收敛为 feeds 同款惯用法（operator.py:561-567）；20-job 反射全集断言落地（F3 P3-3 闭环）。

## 二、调度终版 20 job：PASS

- 7 cron + 13 interval = 20，`test_maintenance.py:89-119` 反射全集断言与 scheduler 注册**逐一对得上**：gated 14 / ungated 6，无缺无余。
- **research-tree-tick gated=True 正确**：`tick()` → `_claim_next_node` → `_run_node` → `executor.submit` 发起新模型调用，必须尊重维护暂停；PATCH-NOTES-D4 §5 的挂载指示被 I4 忠实执行（含 gated 清单更新）。
- 全负载配额增量估算：树 tick 每 5min ≤3 节点（`NODES_PER_TICK=3`）；日增量上限 = `daily_tree_cap=3` 树 × `max_nodes=12` = **≤36 次节点模型调用/日**（预算仲裁内嵌在子插入语句，count-guarded，并发不可联合超支）；bilingual 默认 OFF（admin_state 缺行=OFF，烧配额路径 fail-closed）零增量。峰值并发仍被 executor 全局信号量 3 + 单手互斥钳制——F3「配额被 max_concurrent=3 钳制」结论在 20 job 终版继续成立。

## 三、bus 事件面第四轮增量：PASS

- `tree.node_completed` / `tree.completed`（research_tree.py:601/629/554）：payload **无 analyst_id 且无消费方期待**——树探索走 `research_hand_names` 轮询，无分析师归属；消费方 exporter 只用 ref_id 重投影，前端 SSE 是类型无关游标。`tree.completed` 由 `announced_at` 条件 UPDATE 单发仲裁（终态+排空才命中），exporter 投影不可能收到进行中快照。
- `bilingual.twin_ready`：引用式 payload（全文只存 `tasks.output`，task_id 解引用）；消费方 `_on_twin_ready` 消费 workflow_id/task_id/work_date/run_id 全部对齐（exporter.py:616-645）。
- `paper_book.closed` 的 **analyst_id 归因链闭合**：发射侧经 0019 `items→extraction` 反查（paper_book.py:282-309），消费侧 memory.py:208-217 按 payload analyst_id 过滤——REVIEW-C3 M5 的回流要求端到端成立。
- **exporter 9 handler 全不 raise**：9 个 handler + helper 共 11 处 `except Exception + log.exception` 兜底，register 断言测试锁定 9 对注册（test_exporter_handlers.py:286-302），空 payload/悬空 ref 的 degrade 面单独有测试。

## 四、drain 终版：PASS

全仓 `asyncio.create_task` 11 处逐一核销：

| 位点 | 归属 |
|---|---|
| executor.py:303 | `executor._running` |
| workflows.py:195 | `workflows._driving` |
| whiteboard.py:82 / mailbox.py:35 / research.py:66 / archive.py:42 / **bilingual.py:84** | 各自 `_bg_tasks`（bilingual 第四轮收编 ✅） |
| analyst_daily.py:420/426 | `analyst_daily._background` |
| analyst_daily.py:379（heartbeat） | run_all 内部结构化子任务：finally 先 `stop.set()` 再 await，`wait_for(stop.wait)` 即时返回——drain cancel run_all 时不悬挂 |

`main._drain_background` 8 注册表 + scheduler inflight extra 快照 + 两轮清扫，与 create_task 全集一一对应。**research_tree 确认零游离**：模块自身无 create_task，节点任务全走 `executor.submit`（`_running` 注册），tick 本体由 scheduler 驱动（inflight extra 覆盖）。

## 五、迁移链 0001-0022：PASS（两路实测）

- **路 1（/tmp 空库全链）**：22/22 applied、`integrity_check` ok、0019/0020/0021/0022 新对象探针全在（forecast_extraction_items / research_trees+nodes / projects / uq_action_dispositions_loop_once）。
- **路 2（0018 停点增量）**：先 18/18（模拟生产旧 checkout），预置一对 pre-0022 重复 loop disposition 行，再全链升级 → 22/22、**0022 的 belt-and-braces DELETE 实测保最早行**（kept ids [1]）、`integrity_check` ok、`foreign_key_check` 零违例。
- **B1 纪律 22 文件全扫**：BEGIN/COMMIT/ROLLBACK/ATTACH/VACUUM 零违例（注释剥离后正则词边界扫描）。
- **生产核对**：`~/.institute-one/institute.db` 22/22 已应用（0019-0022 在列），`/health` ok（launchd 实例存活）。
- 旧文件未动：0001-0018 mtime 全部早于第四轮起点 10:33（0017=08:25、0018=07:34），0022 注释明确承认 0018 生产不可变。

## 六、硬规则终扫：PASS

- **规则 4（prompts 逐字）**：`prompts.py` 相对 HEAD 仅 memory_block 插槽增量（B3 轮已审），既有 prompt 字符串零字节改动；`workflows/*.json` 仅 `analyst→analyst_id` 键名规范化（A4/F2 已知），prompt 文本逐字未动；第四轮新增 EXPLORE_PROMPT / TRANSLATE_PROMPT 为**新常量**非改写，且结构性回显免疫（协议 token 不在行首 + 插值材料中和）。
- **规则 5（勿动久经考验）**：`rate_limit.py` / `registry.py` / `writer.py` mtime 均早于第四轮（04:12/06:50/06:54），未被触碰；五规则管辖一切第四轮新 export（tree/twin/journal 全走 `write_note`）。
- **规则 1/2/3/7**：D4/D5 模型调用全走 executor.submit；状态迁移全条件认领（本报告各节已逐一列举）；20 job 全 @metered 永不 raise；时间戳 bus.now_iso()/work_date() 合规（reproject 的 SGT 窗、tree 的 finished_at 抽查无 raw now）。
- **跨轮事务边界不变量全在**：A5 whiteboard COMMIT 异常边界、A7 PIT 版本不可变、B3 region 字节级指纹、B6 entry PIT 冻结（paper_book 复用同源 helper）、D4 单事务完成路径（父终态先行+子批同事务+stop 单事务+announced_at 仲裁，research_tree.py:574-638/728-761/528-563 复核）。
- maintenance key/形状一致（`'maintenance'` + `{"paused": bool}`）；scheduler fail-open 与 bilingual fail-closed 的**读语义双轨是有意分歧**（skip 只延迟无配额工作 vs gate fail-open 烧配额），REVIEW-D5 H2 记录在案——见 NIT-5 提示。

## 七、I4 三处适配差异：全部合理

1. **exporter handler 全表 7→9**：D6 写测试时 7 个是当时事实；D3（twin_ready 属 D5 交付、由 D3/I4 挂到 exporter）与 D4（tree.completed）落地后，I4 把注册断言表扩到 9 对（test_exporter_handlers.py:296-299）是正确同步，非语义变更。
2. **建树 422 归类**：`POST /api/research/tree` 带 required `root_topic` → 路由枚举测试的 `_empty_body_would_422` 机制**自动**将其纳入 422 验证面（test_api_routes.py:108-124），无需表条目；枚举守卫另 pin 了 `/api/research/tree` 前缀防 router 脱挂（:272）。归类与 PATCH-NOTES-D4 §3 的契约（越界/多余字段→422）一致。
3. **_run_work_date 同源修正**：exporter zh 侧从 run 冻结 `variables.WORK_DATE` 取 stem（exporter.py:256-274，fallback 今天）；bilingual twin payload 同源同 fallback（bilingual.py:228-235）——跨 SGT 午夜的 run，zh 导出与 `_en` twin 文件名 stem 保证一致（REVIEW-D5 M4 的修复正确且两侧闭合）。

## 八、裁决点：test_restart_recovery.py:241 D4 探针 skip

**裁决：修测试（S4 收尾或下轮首卡落地）；当前接受 skip，无生产风险。**

- 现状实测复现：`4 passed, 1 skipped`。盲插失败机制确认——`research_trees.status` CHECK 无 `'running'`（树状态枚举本就不含它）；`research_tree_nodes.tree_id` FK（db.py:38 `PRAGMA foreign_keys=ON`）拒绝无树的 probe 行 → `seeded=0` → 诚实 skip。
- **为什么修而不是删**：该探针的独特价值是走**真实 lifespan**（`async with _lifespan()`），验证 main.py 确实挂载了 `research_tree.recover_orphans()`（main.py:117-118，恰是 I4 本轮的两行挂载）。D4 分区的恢复测试（test_research_tree.py:423/448）只证函数语义、不证挂载——挂载回归正是 restart-recovery 套件的存在意义。D4 已落地、schema 已知，防御性盲猜可以退役。
- **修法**（~15 行）：显式种子替代泛型猜测——种一棵 `status='exploring'` 树 + running 节点（断言 lifespan 后 running→pending），再种一棵 `status='stopped'` 树 + running 节点（断言 running→pruned，两种恢复语义都锁死）；删掉 schema 猜测循环与 `seeded=0` skip 分支。

## 九、问题分级

无 HIGH / MEDIUM。

- **NIT-1 (LOW)** `app/institute/forecast_extract.py:571-581`：并发 resume 同一 pending source 时，后来者会把先行者 in-flight（已 claim 未回填）的 item 误报「in doubt」进 problems/detail，且最后完成者的 complete UPDATE 覆盖 bookkeeping 数字。**无重复 forecast 风险**（item 主键仲裁），仅 detail 噪声；单进程 bus 串行分发下几乎不可达（需 API 手动重放撞上事件触发）。可留档不修。
- **NIT-2 (LOW)** `app/institute/forecast_extract.py:367-372`：`_find_securities` 每句重建同名分组字典，O(句数×名表)；名表大时的纯性能项，功能正确。
- **NIT-3 (记录)** `app/vault/exporter.py:603-607`：tree 投影 artifact_id 按 root_topic slug 锚定——同 root_topic 多棵树**后完成者覆盖**同一 `tree.md`。PATCH-NOTES-D4 §6 已声明为设计取舍并给出一树一档改法；确认为已知边界，非缺陷。
- **NIT-4 (待办)** `tests/test_restart_recovery.py:204-251`：探针 skip，裁决见第八节（修法已给）。
- **NIT-5 (提示)** maintenance 读语义双轨（scheduler.get_maintenance fail-open / bilingual._maintenance_paused fail-closed）：都以同 key 同形状为准、分歧有意且各自正确；**后续新增烧配额 gate 应复用 bilingual 的保守读法**，勿抄 scheduler 版。

## 十、收尾建议

1. **修 restart-recovery 探针**（NIT-4 修法）——S4 核销时顺手或第五轮首卡。
2. unshadow 前置四项（D1 已收拢：注入三层/live floor/0022/反射断言）可正式解锁 operator 非影子化评审。
3. 已立卡的后续项继续排期：PATCH-NOTES-C1 五项（digest 路径/callout/Step-0 prompt 接线/插件命令/durable retry）、memory 注入 ad-hoc asks（CLAUDE.md:29 已如实标注）、weights 接入 research 轮询。
4. janitor 的 30 天清理可顺带回收 `research_tree_booked:<date>` 与 factcheck_attempts 计数行（D4/C1 同款备忘，几字节/天，不急）。
5. F2 遗留的「前端 KNOWN_EVENT_TYPES 落后」已被 C7b 类型无关游标架构**结构性消解**（全仓 rg 零命中），可从跟踪清单销项。

## 十一、验证记录

```text
定向套件（13 个第四轮相关文件）
.venv/bin/pytest tests/test_forecast_extract.py tests/test_paper_book.py
  tests/test_research_tree.py tests/test_projects.py tests/test_bilingual.py
  tests/test_operator.py tests/test_chain.py tests/test_mcp_roundtrip.py
  tests/test_api_routes.py tests/test_exporter_handlers.py
  tests/test_restart_recovery.py tests/test_maintenance.py tests/test_cron_metrics.py
→ 213 passed, 1 skipped（skip = 裁决点探针）

迁移两路实测（/tmp 临时库，一次性脚本）
路1 空库全链    → 22 applied, integrity ok, 0019-0022 对象探针全过
路2 0018 停点   → 18 → 22, 0022 去重保最早, integrity ok, FK 零违例
B1 纪律 22 文件 → BEGIN/COMMIT/ROLLBACK/ATTACH/VACUUM 零违例

生产核对
sqlite3 ~/.institute-one/institute.db → schema_migrations 22 行（尾 4 = 0019-0022）
curl /health → {"ok":true}（launchd 实例）

未运行全量套件（S4 分工负责 764/10 复核）。
```
