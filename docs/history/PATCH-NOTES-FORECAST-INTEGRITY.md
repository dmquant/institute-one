# PATCH — 预测账本诚信修复（对抗审计 Top3）

对应审计 findings 1/2/4（结算证据链）、5/6（extract exactly-once）、3/8（诚信硬边界）。

**改动文件**：`migrations/0033_forecast_evidence.sql`（新建）、`app/institute/forecasts.py`、`app/api/forecasts.py`、`app/institute/forecast_extract.py`、`app/institute/paper_book.py`、`app/config.py`（仅删 cap 开关一行）、`tests/test_forecasts.py`、`tests/test_forecast_extract.py`、`tests/test_paper_book.py`。scheduler / market_data / frontend / plugin 未动。

---

## Fix 1 结算证据链（findings 1/2/4）

### 1a. 变更型结算禁止调用者指定时点

- `settle_forecast()` 的 `as_of` 参数**整体移除**（签名只剩 `note`）；API `SettleBody` 同步删除该字段——`POST /{id}/settle` 带 `as_of` 现在是 422（`extra="forbid"`）。
- knowledge cutoff 由**系统固定**：`_knowledge_cutoff()` = 微秒级 UTC now（0006 PIT 版本键同形，`bus.now_iso()` 是秒级、同秒版本会在字符串比较上打平，故不用）。持久化到 settlement 行新列 `knowledge_as_of`。
- 历史 replay 改为**只读 preview**：领域层 `preview_settlement(forecast_id, as_of=None)` + API `GET /api/forecasts/{id}/settlement-preview?as_of=…`。不落库、不改状态、不发事件，对 open/settled/invalid 任意状态可用（响应带 `preview: true` 与 `expired` 标志）；把 settlement 行里记录的 `knowledge_as_of` 喂回去即可从不可变版本库复算出当时的 verdict。

### 1b. PIT 快照一致性（四次读一个时点）

- `get_bars_pit` / `get_marks_pit` 均支持 `as_of` 显式传参（先确认过签名）。结算的四条腿现在**全部显式**：entry 两腿 `as_of = made_at`（反前视契约不变，冻结在预测者知识）；exit 两腿 `as_of = knowledge_as_of`（同一个值传给 security 与 benchmark）——两次 exit 读之间落进来的 correction 不再可能把一次结算劈成两个知识快照。
- 计算核心抽成纯函数 `_evaluate_settlement(fc, rule, knowledge_as_of)`，settle 与 preview 共用，保证两者语义严格一致。

### 1c. benchmark 窗口对齐（fail closed）

- 旧逻辑：benchmark entry/exit 取 `<= made_date / expires_date` 的最后一条 mark——security 因迟发回退到更早 entry bar 时，两个窗口静默错位。
- 新逻辑 `_aligned_benchmark_return()`：benchmark 的 entry/exit mark 必须**精确落在 security 实际使用的 bar_date 上**（entry 腿按 made_at 知识、exit 腿按 cutoff 知识各查一天）。对不齐 → `verdict='invalid'`，note 带 `window misaligned` 与具体日期；绝不取"最近一天"代理。

### 1d. 证据列（0033）

`forecast_settlements` 新增 9 列：`knowledge_as_of` + 四条腿的版本标识——`entry_bar_date/entry_as_known_at`、`exit_bar_date/exit_as_known_at`、`bench_entry_date/bench_entry_as_known_at`、`bench_exit_date/bench_exit_as_known_at`。版本标识 =（日期, `as_known_at`），正是 0006 的不可变版本键（security_id/freq、benchmark_id 由 forecast 行/rule 提供），足以唯一定位每条腿用过的那一个版本行。invalid 结算记录已解析的腿，其余留 NULL；0033 之前的行全 NULL（诚实：当时没记录）。

## Fix 2 extract exactly-once（findings 5/6）

- **claim 后 create 前崩溃窗口**：`forecast_extraction_items.forecast_id` 改为**认领时写入**的预生成确定性 id（`sha256(extraction_id|security_id)[:12]`），`create_forecast()` 增加 trusted kwarg `forecast_id=` 用该 id 建行。replay 语义变为"INSERT OR IGNORE 式"：item 在而 forecasts 行不在 → 用同一 id 安全重试 create（forecasts 主键仲裁并发赢家：撞主键的 ForecastError 复查存在性后按已创建计）；item 在且行在 → 直接计入。旧的"NULL 即跳过（in doubt）"只剩给 0033 前遗留行的 fail-closed 分支，操作员手术路径不再需要。
- **source_ref 绑定内容 hash**：claim 行持久化 `text_sha256`（对传入 text 的 UTF-8 字节整体取 sha256）。同 ref 同内容 replay = duplicate（原语义）；同 ref **不同内容** = readable `ValueError`（"already claimed for different content"），pending / complete 状态一视同仁，绝不静默续跑成混合抽取。遗留行 hash 为 NULL 时不拒（无从校验），finalize 时 `COALESCE` 补写。
- **冻结 made_at**：claim 行持久化 `made_at`（显式参数或认领时 now，经 `_norm_ts` 规范化）。所有候选——包括崩溃后 resume 补建的——一律用冻结值；resume 传入不同 made_at 会被无视（回归测试锁死）。
- **finalize 从 item 表汇总**：`forecast_ids` / `n_forecasts` 改为 `items JOIN forecasts` 聚合（含并发处理者与崩溃前孤儿的成果），不再信任调用者本地 `created` 列表；返回值与 `forecast.extracted` 事件同源。

## Fix 3 诚信硬边界（findings 3/8）

- **made_at ±24h 门**：新领域入口 `create_forecast_public()`（仅 `POST /api/forecasts` 走）——made_at 距 now 超过 24h（过去或未来）且未声明 `backfill: true` → 400（信息里指明 backfill 路径）。声明后行上持久化 `origin='backfill'`（0033 给 `forecasts` 加 `origin TEXT NOT NULL DEFAULT 'standard'`，闭集在领域层校验——additive 迁移加不了可扩 CHECK，沿用 0006 open-set 方针）。内部可信路径（extractor、测试直调 `create_forecast`）不过门，extractor 的知识时间由 Fix 2 的冻结 made_at 管。
- **绩效统计口径排除**：rg 全仓确认——后端**没有** forecast 命中率 SQL（`scorecard.py` 是 hand QA，与 forecast verdict 无关；PATCH-NOTES-PLUGIN-ALIGN 也记载"后端没有 forecast stats 端点"）。两个命中率消费方都是客户端聚合 `GET /api/forecasts` 默认列表：SPA Dashboard `loadForecastHitRate`（status=settled 全量翻页）与 Obsidian 插件 dashboard（近 5 条）。因此排除点落在 **`list_forecasts` 默认口径**：`origin` 参数缺省 = `origin <> 'backfill'`（绩效视图，两个消费方自动获得排除，前端/插件零改动）；`origin=backfill|standard` 精确过滤、`origin=all` 完整问责视图。Vault `Book/forecasts.md` 保持完整账本（rows are truth 的投影），backfill 行标注"来源：backfill（回填记录，不计入绩效统计）"。
- **paper book 同口径排除**：回填单若进簿会拿到历史冻结入场价——正是账本要防的前视。opener 候选查询加 `f.origin <> 'backfill'`；手动 `open_forecast_position` 对 backfill 行 400 拒绝。
- **删除 INSTITUTE_PAPER_BOOK_ENFORCE_CAPS**：`config.py` 删字段（仅此一行）；`paper_book._insert_position` 的 cap WHERE 无条件生效、`opener_tick` 删开关读取与 `cap_enforced` 摘要键（无消费方，全仓 rg 确认）；`open_forecast_position` 文档同步。**可关的风险上限不是上限**——治理入口只剩 admin_state 的 `max_positions` 本身。

## Schema 变更（migrations/0033_forecast_evidence.sql，编号 0033）

| 表 | 新列 |
|---|---|
| `forecasts` | `origin TEXT NOT NULL DEFAULT 'standard'`（闭集 standard/backfill，领域校验） |
| `forecast_settlements` | `knowledge_as_of` + 8 列证据（entry/exit × security/benchmark 的 date + as_known_at） |
| `forecast_extractions` | `text_sha256`、`made_at`（冻结知识时间） |

无 BEGIN/COMMIT/ATTACH/VACUUM/PRAGMA；`test_db_migrate.py` 全绿（含语句拆分与 executescript 等价性、崩溃恢复守卫）。

## API 变更（破坏性，刻意）

- `POST /{id}/settle`：body 只收 `note`；带 `as_of` → 422。
- `GET /api/forecasts`：默认口径排除 backfill（新 `origin` 查询参数暴露完整视图）。
- `POST /api/forecasts`：超窗 made_at 无 backfill → 400；新 `backfill` 字段。
- 新增 `GET /api/forecasts/{id}/settlement-preview?as_of=`。

## 测试

命令：`.venv/bin/python -m pytest tests/test_forecasts.py tests/test_forecast_extract.py tests/test_paper_book.py tests/test_db_migrate.py -q` → **80 passed**（23 + 18 + 20 + 19）；`compileall app` 干净。

- `test_forecasts.py` 20→23：新增证据链持久化+回放复现（correction 后 recorded cutoff 仍复现原 verdict、preview 零写入）、benchmark 对齐四场景（entry 错位/exit 错位/迟发回退日必须对齐/对齐后可结算且证据日期相等）、made_at 门+origin 全链路（±24h 两侧、origin 过滤、未知 origin 拒绝、vault 标注）；改写 4 个既有测试（as_of replay → preview、`_window_return` 4 元组、API roundtrip 走 backfill 声明并断言默认口径排除、settle as_of → 422）。
- `test_forecast_extract.py` 15→18：改写 crash-after-create（孤儿现在计入、零 in doubt、item id == forecasts id）；新增 crash-before-create 同 id 重试、content hash 绑定（同文 duplicate/异文 readable 拒绝）、冻结 made_at 统辖 resume。
- `test_paper_book.py` 19→20：两个"开关可关"测试改写为"不可关"（Settings 无字段、env 死旋钮、opener 与 API 双路径 cap 恒硬、admin cap 上调仍是合法治理路径）；新增 backfill 永不进簿（opener 预滤 + 手动拒绝 + 同标的 standard 单不受影响）。
- 溢出敏感的下游抽查（并行 agent 在跑，未跑全量）：`test_portfolios.py` 12 passed、`test_mcp_roundtrip.py + test_api_routes.py + test_exporter_handlers.py + test_restart_recovery.py` 34 passed。

## 遗留 / 口径备注

- 0033 前的 settlement 行证据列全 NULL（当时确实没记录）、extraction 遗留 NULL-item 仍走 fail-closed in-doubt 分支——历史不粉饰。
- backfill 行仍可结算（问责记录要有结果），只是不进任何绩效口径（默认列表、SPA/插件命中率、paper book）。
- ~~前端/插件在边界外未动~~（R2 P2-1 已改 Forecasts 台账页，见下）。

---

# R2 轮闭合（对抗审核员 GPT 5.6 Sol Max，REQUEST_CHANGES → 全部修复）

每条 finding 均先用 /tmp 临时脚本对修复前代码确认攻击成立（`/tmp/repro_p1_1.py`、`/tmp/repro_p1_2.py`、`/tmp/repro_p1_3.py`，共用 `/tmp/repro_r2_common.py`；三条 P1 全部打印 `ATTACK CONFIRMED`），修复后复跑全部不再复现。

## P1-1 backdated PIT 修订可改写重放 → 腿级 pin

**攻击确认**：settle 得 hit/+10%（knowledge_as_of=T）；随后 `upsert_bar` 落一条 `as_known_at` 回填在原版本与 T 之间的修订（9.0）——PIT 扫描按 `MAX(as_known_at) <= T` 现在选中它，`preview(as_of=T)` 变 miss/−10%。

**修法**：preview 分两条证据路径，响应新字段 `evidence_source` 声明用了哪条：

| 路径 | 触发 | 取数 | 语义 |
|---|---|---|---|
| **pinned** | `as_of` == 该 forecast settlement 行的 `knowledge_as_of` | `_evaluate_pinned()`：按行上持久化的四腿 (date, as_known_at) 版本标识**精确取版本行**（`_pinned_bar`/`_pinned_mark`，0006 版本键等值查询） | **复现这次结算**。后落库的任何修订（含回填 as_known_at）都动不了它；未 pin 的腿（invalid 结算）或版本行已级联删除 → 复现 fail-closed invalid |
| **pit** | 其他任意 `as_of`；或 0033 前遗留 settlement（knowledge_as_of/证据列全 NULL）——遗留行的**回退路径** | PIT 扫描（`MAX(as_known_at) <= as_of`） | 回答"**版本库现在**认为 T 时点我们知道什么"——回填修订流**本来就允许**改写这个答案（这正是 caller-supplied as_known_at 的设计用途）。审计一次结算用 pinned，探索反事实时点用 pit |

`_conclude()` 抽出共享的 measured→verdict 尾段，pinned/pit 两个 evaluator 语义严格同源。

**测试**：`test_pinned_replay_immune_to_backdated_revisions`（security+benchmark 四腿回填攻击下 pinned 不动、pit 口径按文档变化、遗留 NULL 行回退 pit、preview 全程零写入）；既有 `test_settlement_persists_evidence_chain_and_replays` 追加 `evidence_source` 断言。

## P1-2 并发 extractor 可把缺失 forecast 的 item 封成 complete → 条件封口

**攻击确认**：A 认领 item#2 后崩溃（forecast 未建）；B 并发 resume，对 item#2 的读是陈旧读 → 认领 INSERT 输掉 → 跳过 → B 的 finalize 无条件封 complete。claim 行 complete 而 item#2 无 forecast，此后 replay 全部撞 duplicate——候选永久丢失。

**修法**：finalize 改**条件领取**（pending→complete 是一次 conditional claim）：聚合 SELECT 与封口 UPDATE 同在一个 `db.transaction()`；UPDATE 的 WHERE 要求 `status='pending'` **且不存在** "forecast_id 非 NULL 但 forecasts 行缺失" 的 item（遗留 NULL item 不阻塞，照旧记 detail）。封不上时：若已被并发方封掉 → 返回 processed（事件由封口赢家独发，顺带消掉旧的并发双发窗口）；否则返回新状态 `status='pending'` + problems 列出 in-flight 候选——来源保持可重放，永不 entomb。

**测试**：`test_seal_is_conditional_on_every_item_reaching_terminal_state`（复刻攻击交错：B 返回 pending 而非封死、claim 行保持 pending、replay 补建缺失候选后封 complete、`forecast.extracted` 恰好一次）。

## P1-3 48-bit forecast ID 碰撞误判并发成功 → 归属校验 + fail loud

**攻击确认**：把确定性 id 钉到一条无关人工 forecast 上，process_source 把它当"并发重试成功"计入 `created`，item 行反向把外来 forecast 误归属到本次 extraction（污染 paper_book 归因链）。

**修法**：`_owns_forecast(row, sid, frozen_made_at)` —— 确定性 id 下已存在的 forecast 只有当 `made_at == claim 行冻结值` 且 `security_id ∈ {sid, NULL(删标的后)}` 才算本槽位的并发成功。三个入口全部接上：① create 撞主键后回读校验，不属己 → 先 DELETE 本 item 行（防误归属）再抛 readable `ValueError("deterministic forecast id collision …")`；② resume-count 路径同样校验，污染 item 释放 + fail loud；③ 跨 extraction 碰撞在 item 认领 INSERT 处撞 `idx_extraction_items_forecast` 部分唯一索引 → 捕 IntegrityError 转同款 readable 错误（原来是裸 500）。真碰撞概率约 2^-48/对，fail loud 由操作员裁决，不自动化解。

**测试**：`test_id_collision_with_unrelated_forecast_fails_loud`（三个入口逐一：外来行零改动、item 零残留、来源保持 pending 不封口；末尾正向断言合法并发赢家仍通过 `_owns_forecast`）。

## P2-1 backfill 默认隐藏波及台账页与 MCP → 台账口径显式 origin=all

- `app/mcp.py` `forecasts_list`：默认 `origin='all'`（完整台账），schema 增加 `origin` 枚举参数（standard/backfill/all）供精确过滤；工具描述写明与 HTTP 默认绩效口径的差别。
- `frontend/src/pages/Forecasts.tsx`：台账卡改走页内 `listAllForecasts()`（`GET /api/forecasts?origin=all`，复用 Dashboard 页的 AUTH_TOKEN_KEY 原生 fetch 模式——`api.ts` 属于并行改动不越界）；Dashboard 命中率继续用 `api.listForecasts` 默认口径（排除 backfill），不动。
- **测试**：后端 `test_mcp_list_shows_the_complete_ledger_including_backfill`（MCP 默认含 backfill、origin 过滤、HTTP 默认仍排除、`origin=all` 即台账页请求的契约）。**前端无对应测试面**：现有 vitest 只覆盖 `api.ts`/`useSSE`（无组件测试依赖，devDeps 无 @testing-library），页面改动以 `npm run build`（tsc 严格类型检查）+ 既有 16 个 vitest 用例通过为自动化验证；**手工验证步骤**：`POST /api/forecasts` 带 `made_at`(>24h 前)+`backfill:true` 建一条回填 → 打开 SPA「预测与账本」页应看到该行（状态筛选各档均可见）→ Dashboard「预测命中率」样本数不含它 → MCP `forecasts_list` 默认返回含它、`{"origin":"standard"}` 不含。

## R2 验证

`.venv/bin/python -m pytest tests/test_forecasts.py tests/test_forecast_extract.py tests/test_paper_book.py tests/test_portfolios.py tests/test_api_routes.py -q` → **83 passed**（forecasts 25 + extract 20 + paper_book 20 + portfolios 12 + api_routes 6）；另抽查 `test_mcp.py + test_mcp_roundtrip.py + test_db_migrate.py` 36 passed；`compileall app` 干净；`cd frontend && npm run build` 通过 + `npm run test`（vitest）16 passed。0033 迁移本轮未增列（P1-1 复用第一轮已持久化的证据列）。不 commit。
