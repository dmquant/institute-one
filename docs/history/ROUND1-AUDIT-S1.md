# 第一轮升级改造轮级终审 S1

> 角色：验证核销视角；当前代码只读，唯一新增文件为本报告。  
> 审计时间：2026-07-20 04:53–05:10（UTC+8）  
> 结论：**S1 范围通过，但需完成列明的收尾补丁。** 8 份审查共 14 条 must-fix，当前代码核销为 **14 FIXED / 0 PARTIALLY / 0 NOT-FIXED**；A4 没有 must-fix。全量 Python 验证为 **191 passed / 8 skipped / 0 failed**，两个 TypeScript 构建均退出 0；运行库完整性为 `ok`。

## 1. Must-fix 逐项核销

审查报告中的旧行号均未直接沿用；下列位置是当前工作树按函数和语义重新定位后的行号。

| 审查 | 条目 | 状态 | 当前代码证据 | 回归证据与核销结论 |
|---|---|---:|---|---|
| REVIEW-A1 | M1 停机 drain 漏后台任务、shield task、scheduler job，且丢弃 done 异常 | **FIXED** | `app/main.py:28-52` 在 shutdown 前快照 APScheduler 在途 task；`:55-107` 两轮收集、cancel、有限等待并消费异常，注册表覆盖 executor/workflows/whiteboard/mailbox/analyst_daily/research/archive；`:135-142` 顺序为快照→scheduler shutdown→drain→DB close。`app/institute/research.py:30-46` 将 shielded tick 登记到 `_bg_tasks`。 | `tests/test_executor_shutdown.py:115-205` 覆盖 analyst_daily/research、shield 注册、异常消费、第二轮清扫及 scheduler 快照；`tests/test_vectors.py:273-294` 另覆盖 archive embedding task。原报告列出的四个缺口均已闭合。 |
| REVIEW-A1 | M2 retry 丢失 research 限链执行策略 | **FIXED** | `app/api/tasks.py:54-72` 按 `source == "research"` 重建 `research_hand_names` 限链，存量 hand 不在链内时改用链首；`:75-107` 仅允许 failed、创建新行并保留 source/session/run/workspace/timeout。 | `tests/test_tasks_retry.py:94-115` 实测 research retry 只尝试测试配置的 research hand；`:48-66` 覆盖 completed/running 409。未持久化历史 `fallback=False` 仍是已记录的后续建模项，但已满足审查给出的“至少按 source 重建 research policy”修复路径。 |
| REVIEW-A1 | M3 0005 回填旧 `NULL`，错误挤占当日 cap | **FIXED** | `migrations/0005_research_hardening.sql:10-21` 明确不回填，只加列和索引；`app/institute/research.py:117-123` 只用 `work_date = ?` 计数；`:197-201` 新完成记录显式写 SGT `work_date()`。 | `tests/test_research.py:96-115` 实测 completed_at 为当天但 `work_date IS NULL` 的旧行不占 cap；`:118-134` 固定 SGT 工作日口径。 |
| REVIEW-A2 | M1 MCP 等值预查不等价于域 content hash | **FIXED** | `app/institute/whiteboard.py:54-64` 在域函数内取得 `INSERT OR IGNORE` rowcount，并返回 `inserted`；`app/mcp.py:378-396` 仅据该权威信号决定 added/duplicate 与事件。 | `tests/test_mcp.py:114-130` 覆盖 `("机器人产业链","")` / `("机器人","产业链")` hash 别名，不误报新增、不发伪事件。 |
| REVIEW-A2 | M2 预查与 INSERT 非原子，并发双重误报/双事件 | **FIXED** | 同上：rowcount 在 `db.execute` 的单条原子 INSERT 内产生，不再存在 MCP 侧 SELECT→INSERT 窗口。 | `tests/test_mcp.py:148-186` 覆盖真实新增恰一次，以及两个并发 MCP 请求恰一真一假、库中一行、事件一次。 |
| REVIEW-A3 | M1 外部日期直接进入 GLOB，跨日期污染 | **FIXED** | `app/institute/analyst_daily.py:54-77` 改为 `substr(key, 1, ?) = ?` 的字面量前缀比较，legacy 精确 key 仍先合并。 | `tests/test_analyst_daily.py:119-144` 覆盖近似日期以及 `?`、`*`、`[]`、`%` 等探针，均不会跨日匹配。 |
| REVIEW-A4 | 无 must-fix（原结论 PASS-WITH-NITS） | **N/A** | 两个 nit 已额外修复：`app/api/meta.py:45-53` 使用 `StrictBool`；`app/institute/workflows.py:44-60` 让 canonical `analyst_id` 优先于 legacy alias。 | 本报告统计不把 A4 的两个 nit 计入 14 条 must-fix。 |
| REVIEW-A5 | M1 COMMIT 后 query 失败误释放已消费 topic | **FIXED** | `app/institute/whiteboard.py:139-175` 明确以事务 COMMIT 为边界，COMMIT 后 emit/read 均兜底，读取失败用已知字段返回；`:178-208` 只对 `_open_board` 真正抛出的 pre-commit 失败释放 `used` claim。 | `tests/test_whiteboard.py:128-160` 注入 post-commit read 失败，断言 board 仅一块、topic 保持 used、第二次 kickoff 不会重复开板；`:83-125` 另覆盖真实 board/card INSERT 失败回滚。 |
| REVIEW-A6 | P1-1 export→空库 import 丢 `blocked_reason` | **FIXED** | `app/institute/roadmap.py:190-211` 校验 seed 字段；`:240-248` 新卡 INSERT 吃回 `blocked_reason`；`:267-274` 与 status 一致采用“本地优先、force 才覆盖”；`:316-360` 对称导出。 | `tests/test_roadmap.py:743-801` 覆盖 blocker 导出、同库 no-op、空库重建、claim 门仍阻塞，以及 JSON bytes 相等。 |
| REVIEW-A6 | P1-2 resolved decision 可无事件改写 | **FIXED** | `app/institute/roadmap.py:1047-1065` 对 resolved 行的任何非空 PATCH 直接拒绝；`:1079-1101` resolve 和 open 状态编辑都带 `WHERE status='open'` 条件并检查 rowcount。 | `tests/test_roadmap.py:464-480` 覆盖二次 resolve 及 decision/title/options 的 resolved 后改写，原值保持不变。 |
| REVIEW-A6 | P1-3 checklist 改名破坏 deterministic id/seed merge | **FIXED** | `app/institute/roadmap.py:711-758` 改名时同一 UPDATE 重算 `_det_id(card, kind, text)`，返回新 id 并发 rename event。 | `tests/test_roadmap.py:636-692` 验证新 id、不变量及 rename→原 seed import 后旧文本正常重建、改名行保留。 |
| REVIEW-A7 | M1 PIT 版本行会被同键不同 payload 覆写 | **FIXED** | `app/institute/market_data.py:158-170` 定义逐字段重放校验；`:379-408` bar 使用 `DO NOTHING`，冲突后仅接受精确重放；`:516-539` benchmark mark 使用同一策略。 | `tests/test_market_data.py:164-196` 同时覆盖 bar/mark：精确重放 no-op，同键不同事实抛 `TransitionConflict`，原行不变。 |
| REVIEW-A7 | M2 亚秒知识时点被折叠 | **FIXED** | `app/institute/market_data.py:79-105` 统一为 UTC、固定微秒精度，默认版本键时钟也为微秒；`:369-373`、`:505-508` 写入口统一使用。 | `tests/test_market_data.py:199-249` 覆盖同秒不同亚秒、缺省时钟连续修订、偏移/Z/裸日期/naive/亚秒输入及空串拒绝。 |
| REVIEW-A8 | 阻断 1：SHA 只存不校验，漏建/陈旧/空文件/并发乱序 | **FIXED** | `app/institute/archive.py:95-104` 未变化文件仍安排幂等回填；`:123-132` 统一登记任务。`app/institute/vectors.py:190-265` 以 `(path, sha, model)` 幂等、空文件清旧投影、事务内复核当前 SHA；`:273-317` 查询 JOIN 当前 archive SHA + 当前 model，并按 path 折叠。 | `tests/test_vectors.py:136-222` 分别覆盖首次降级后回填、刷新失败隐藏旧向量并恢复、空文件清理、慢旧任务不得覆盖新快照。 |
| REVIEW-A8 | 阻断 2：archive embedding task 未纳入 shutdown drain | **FIXED** | `app/institute/archive.py:26-54` 有强引用注册表和完成回收；`app/main.py:70-81` 将 `archive._bg_tasks` 纳入统一 drain；`:135-142` drain 早于 DB close。 | `tests/test_vectors.py:273-294` 实测在途 embedding task 被 drain cancel 且注册表清空。 |

**统计：14 FIXED / 0 PARTIALLY / 0 NOT-FIXED。**

## 2. PATCH-NOTES 应用核销

| 补丁记录 | 状态 | 当前证据 / 待办 |
|---|---:|---|
| PATCH-NOTES-A1 | **按预期部分未应用** | A1 的 retry 与 0005 语义已在当前代码中；建议的 `scheduler.inflight_jobs()` 公共访问器**未应用**：`app/institute/scheduler.py:233-238` 只有 `shutdown()`，私有 `_executors/_pending_futures` 探测仍在 `app/main.py:28-52`。与补丁记录“留给下一轮”一致。 |
| PATCH-NOTES-A2 | **APPLIED** | `app/institute/whiteboard.py:54-64` 已返回 `inserted: bool(n)`；`app/mcp.py:393-396` 已消费该键。原来两个 gated MCP 测试当前未 skip，并随全量套件通过。 |
| PATCH-NOTES-A3 | **NOT APPLIED（符合预期）** | `CLAUDE.md:56` 仍写手工 roster 修改需 restart/reload；`:64` 仍称 `lru_cache`；`:68` 仍称单 blob key。`ROADMAP.md:82,88` 两项仍未勾选。backlog 中没有对应 Phase 0 卡，按记录可忽略。 |
| PATCH-NOTES-A4 | **NOT APPLIED** | `README.md:143` 仍只写手改 maintenance key/kickoff skip；`CLAUDE.md:33` 仍写 `analyst\|analyst_id`；`frontend/src` 无 maintenance 调用/开关；`frontend/src/api.ts:131-136` 仍保留 `WorkflowStep.analyst?`。A4 后端两个 review nit 已修，但这些跨分区收尾尚未做。 |
| PATCH-NOTES-A5 | **APPLIED** | `app/router/executor.py:99-111` 已采用头 3/5 + 尾 2/5 的首尾截断；`tests/test_executor_output.py:38-53` 已落地首行、末行、短文本、单长行测试。retry 端点也已由 A1 实现。 |
| PATCH-NOTES-A7 | **APPLIED** | `app/main.py:157-180` 已 import 并挂载 `api_market_data.router`。但 `tests/test_market_data.py:11,60-62` 仍有“尚未挂载”的过时注释，且测试仍用裸 FastAPI app；建议补生产 `create_app()` 路由 smoke test。 |
| PATCH-NOTES-A8 | **PARTIAL** | 已应用 `archive._bg_tasks` drain（`app/main.py:70-81`）。**未应用** pyproject 依赖、Settings 字段和 Obsidian 消费者适配；见下方精确清单。 |

### PATCH-NOTES-A8 精确待应用清单

1. `pyproject.toml:6-14` 的 `[project].dependencies` 追加 `"sqlite-vec>=0.1.9",`。建议正确可用：当前 venv 实装并验证的版本正是 `0.1.9`。
2. `app/config.py:46-53` 的 Ollama 设置附近追加：
   - `enable_vectors: bool = False`
   - `embed_model: str = "bge-m3"`
   
   `app/config.py:18-19` 已配置 `env_prefix="INSTITUTE_"`，因此会自然映射 `INSTITUTE_ENABLE_VECTORS` / `INSTITUTE_EMBED_MODEL`；默认 False 不改变现状。`app/institute/vectors.py:51,68-75` 的 1024 维和防御式读取与建议一致。
3. 适配 Obsidian API 响应：后端 `app/api/archive.py:15-22` 已从数组改为 `{mode, results}`，但 `obsidian-plugin/src/api.ts:497-500` 仍声明/返回 `Promise<ArchiveHit[]>`，`src/main.ts:474-482` 直接把对象当数组。应给响应建 `{mode, results}` 类型，并在 `archiveSearch()` 内返回 `.results`（这样 `main.ts` 可不改），或同步改消费者。当前 TypeScript 构建通过不能发现该运行时形状错误。
4. 同步补丁文字：FTS 行现在由 `app/institute/archive.py:170-177` 增加 `source="fts"`，并非“内容完全不变”。

## 3. 全量验证

所有命令均在仓库当前工作树执行；未重启 8100 服务。第一次以相对路径并行启动时受中文路径下终端 cwd 漂移影响，命令未实际开始；以下结果均为随后使用绝对路径的有效重跑。

### Python 编译

```text
$ .venv/bin/python -m compileall app -q
exit 0
（无输出）
```

### Python 全量测试

```text
$ .venv/bin/python -m pytest tests -q
........................................................................ [ 36%]
.......ssssssss......................................................... [ 72%]
.......................................................                  [100%]
191 passed, 8 skipped in 6.10s
```

用 `-rs` 复核后，8 个 skip 全部来自 `tests/test_market_thesis_import.py`，行号为 240、272、353、379、396、408、443、457，原因完全相同：

```text
market-thesis-data/bundle.json not present
191 passed, 8 skipped in 5.89s
```

定义处为 `tests/test_market_thesis_import.py:185-191`：该目录是 intentionally untracked 的只读输入；因此属于预期环境缺失，不是代码失败。

### Frontend

```text
$ cd frontend && npm run build
> tsc && vite build
✓ 49 modules transformed.
✓ built in 498ms
exit 0
```

### Obsidian plugin

```text
$ cd obsidian-plugin && npm run build
> tsc -noEmit -skipLibCheck && node esbuild.config.mjs production
exit 0
```

两个 npm 命令都额外打印了同一个环境级提示 `Unknown env config "devdir"`，但无 TypeScript/esbuild/Vite 错误。`git diff --exit-code -- roadmap/backlog.json` 为 0，确认 frontend 的 `roadmap.ts` 输入没有变化；`obsidian-plugin/main.js` 与 `frontend/dist` 也没有新增 tracked diff。

## 4. 运行库健康与老库升级路径

检查方式为 `sqlite3 -readonly ~/.institute-one/institute.db`，同时设置 `PRAGMA query_only=ON`；没有写库、没有重启服务。

```text
PRAGMA integrity_check;
ok

schema_migrations:
0001_init.sql|2026-07-19 18:40:01
0002_roadmap.sql|2026-07-19 18:40:01
0003_theses.sql|2026-07-19 18:40:01
0004_securities.sql|2026-07-19 18:40:01

research_log_columns:
id,topic,run_id,summary,completed_at

0006/0007 新表探测:
（无结果）
```

仓库文件清单为 `0001`–`0007`，因此账本与文件集合的差集精确为 **0005、0006、0007**。`research_log` 尚无 `work_date`，7 张 0006/0007 新表也不存在，确认运行库确实仍是“老库”，而不是迁移做了一半但漏记账。

升级机制：

- `app/db.py:22-33` 在连接初始化后、其他业务启动前调用 `migrate()`。
- `app/db.py:50-61` 读取 `schema_migrations.name`，按文件名排序遍历，只执行未记账文件，并在脚本成功后写入文件名。
- 0005 依赖 0001 的 `research_log`，只做 add-column + index，且不回填旧行。
- 0006 依赖已应用的 0004 `securities`，所有对象均为 `CREATE ... IF NOT EXISTS`。
- 0007 也只做 `CREATE TABLE/INDEX IF NOT EXISTS`，vec0 虚表在运行时加载扩展后创建，不在 migration 中。

**结论：在正常、不中断的一次重启中，升级顺序和依赖关系安全，0005→0006→0007 会各执行一次。**

**非阻断但应登记的 crash-consistency caveat：** `app/db.py:59-61` 的 `executescript(sql)` 与随后写 `schema_migrations` 没有包在一个显式事务中。若进程恰在 schema 已变更、账本尚未写入时被强杀，尤其 0005 的 `ALTER TABLE` 不是可重入语句，下次启动可能因重复加列失败。建议第二轮把“脚本 + 账本”做成可恢复/原子迁移流程，或至少在重启操作中避免中途 kill。

## 5. 测试基线一致性

### 当前端点

当前实际结果与最终基线完全一致：**191 passed / 8 skipped / 0 failed**，且 skip 原因全部已核实。

### 历史序列

用户给出的序列为：

```text
93 → 109 → 113 → 128 → 131 → 143 → 147 → 160 → 163 → 171 → 184 → 191
```

但当前 `implementation-notes.md` 并没有完整记录这条序列：

- 明确写入：93（`:20`）、131（`:53`）、143（`:55`）、147（`:56`）、160（`:59`）、163（`:62`）、184（`:65`）、191（`:67`）。
- 109、113、128、171 在当前文件中未出现。
- 文件另外明确记录了 **182 passed**（`:64`，A8 初版），而该点不在上述序列中。

因此核销结论是：**最终端点一致；中间历史序列只可判为“与最终结果不冲突”，不能判为已被当前日志完整、可重放地证明。** 工作树没有为每个时间点保留提交/快照，无法从当前状态重跑旧基线。建议收尾时把真实时间线补成单一序列，至少纳入已记录的 182，并说明 109/113/128/171 的来源。

## 6. 第二轮遗留与 nice-to-have 汇总

### 优先收尾

1. **A8 可用性与消费者兼容**：应用 pyproject/config 两项；修 Obsidian `{mode, results}` 响应适配。未加 config 前向量功能在正式 Settings 中恒为关闭；未改插件时档案搜索会在运行时把对象当数组。
2. **A3/A4 文档与 UI**：更新 `CLAUDE.md` 的 roster、daily key、workflow key；更新 `ROADMAP.md` 两个完成项；更新 README maintenance 语义；增加 SPA maintenance toggle；移除前端 legacy `WorkflowStep.analyst?`。
3. **A1 公共 scheduler 访问器**：把 APScheduler 私有探测集中到 `scheduler.inflight_jobs()`；同时考虑上述 migration crash-consistency 加固。

### 各审查遗留汇总

| 来源 | 已在返工中顺手解决 | 仍建议进入第二轮 |
|---|---|---|
| A1 | running HTTP 409、research 限链、各后台注册表、异常消费、两轮清扫、旧 NULL 测试均已补 | `truncate_output` 在极小 cap 小于 marker 时仍会超 cap（`executor.py:79-96`）；retry fallback 策略/lineage 未持久化，跨进程幂等也无约束；scheduler 公共访问器；timeout 后仍 alive 分支的更强验证。 |
| A2 | hash 别名、并发单赢家、事件次数测试已补 | cooldown 拒绝仍不含旧字段 `id/status/duplicate`（`app/mcp.py:364-366`）；`research.queued` payload 仍只有 topic（`research.py:81`）；`app/mcp.py:390-392` “补丁尚未落地”注释已经过时。 |
| A3 | must-fix 字面量前缀已补 | roster 文件瞬时缺失时仍不会使用热缓存（`analysts.py:43-52`）；坏 JSON/并发读写等边界可继续扩充；文档同步未做。 |
| A4 | review N1 StrictBool、N2 canonical key 优先均已修 | PATCH-NOTES-A4 的 README/CLAUDE/SPA/type 四项全部未应用。 |
| A5 | 真实事务注入测试、四状态 daily 参数化均已补 | 硬杀或持续 DB 不可写仍可能留下永久 `used` topic；板事务失败可能遗留 session/workspace；后续宜引入 claim lease/recovery 或更大事务边界。 |
| A6 | 三个 P1 must-fix、空白 acceptance、relation 规范化、真实并发测试均已修 | 原审查列出的 P2/P3 已无剩余阻断；可另行决定 seed checklist `checked` 状态是否需要可逆导出（当前文档明确为已知损失）。 |
| A7 | 空串默认问题、时间格式测试已修，main mount 已应用 | 补基于 `app.main.create_app()` 的生产挂载 smoke test；清理测试文件中的“尚未挂载”过时注释。 |
| A8 | current SHA/model、回填、空文件、并发乱序、path 折叠、负缓存、k=1/分支测试及 drain 均已修 | `mode` 仍不能区分“健康零命中”和“向量降级”；跨路径同内容不复用 embedding；旧 model 投影只隐藏不清理；外部分区三项收尾见上。 |

## 7. S1 终审判定

- **Must-fix 门：通过（14/14 FIXED）。**
- **全量验证门：通过（191 passed / 8 expected skipped / 0 failed；两端构建 exit 0）。**
- **运行库门：通过（integrity `ok`；0005/0006/0007 待正常重启顺序应用；本次未重启、未写库）。**
- **交付收尾门：尚有明确待办**，首要是 A8 依赖/config/Obsidian 适配，其次是 A3/A4 文档与 UI，再次是 scheduler 公共访问器与迁移 crash-consistency。
