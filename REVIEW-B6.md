# REVIEW-B6 — M3-001 / M5-001 第二轮独立审查

## 结论：FAIL

M3-001 的顺序执行主路径、双轨设计、旧行 NULL 兼容、上下文注入和正数 cap 播种基本成立；M5-001 的普通价格路径、判定矩阵、条件认领及唯一约束也成立。  
但 M5-001 的核心契约“PIT + fails closed”存在阻断缺陷：

1. entry 价格没有冻结在 `made_at` 的知识快照，而是与 exit 共用结算时的 `as_of`/最新版本，能读到预测作出后才出现的当日收盘或修正版；
2. `_window_return()` 只拒绝非正 entry，不拒绝非正 exit，也没有拒绝 `NaN`/`Infinity`；规则解析同样接受非有限 threshold。

只读探针已实际复现：预测时 entry=10，预测后把同一日修正为 20，exit=11，当前代码以 `-45%` 判 `miss`，而预测时可知口径应为 `+10%`、判 `hit`；exit=0 也被判成 `miss/-100%`，没有 `invalid`。这两项会直接污染预测台账，因此不能合入。

## 审查范围、归因与验证

- 全文审阅：
  - `migrations/0012_research_thesis.sql`
  - `migrations/0013_forecasts.sql`
  - `app/institute/research.py`
  - `app/institute/forecasts.py`
  - `app/api/research.py`
  - `app/api/forecasts.py`
  - `tests/test_research.py`
  - `tests/test_forecasts.py`
  - `PATCH-NOTES-B6.md`
- 参照审阅：`roadmap/backlog.json`、`app/institute/market_data.py`、`app/db.py`、`app/bus.py`、`app/main.py`、`migrations/0001_init.sql`、`0003_theses.sql`、`0004_securities.sql`、`0005_research_hardening.sql`、`0006_market_data.sql`、`tests/conftest.py`、`tests/test_db_migrate.py`、`workflows/research.json`。
- 已执行指定 `git diff -- app/institute/research.py app/api/research.py tests/test_research.py`；B6 两个新域/API文件和两个 migration 为未跟踪文件，普通 `git diff` 不显示其内容，已改为全文审阅。
- 当前工作树有大量其他代理的在途改动，以下结论只归因 B6 独占分区。
- 指定验证：
  - `.venv/bin/python -m compileall app -q`：exit 0。
  - `.venv/bin/python -m pytest tests/test_research.py tests/test_forecasts.py -q`：`27 passed in 1.04s`。
  - 未运行全量测试。

## 问题分级

### [高 / must-fix] entry 使用结算时快照，存在确定性前视偏差

- 位置：`app/institute/forecasts.py:315-340`，关键调用在 `:325`、`:336`。
- security 和 benchmark 都只读取一次 PIT 序列：
  - `get_bars_pit(security_id, as_of, end=expires_date)`
  - `get_marks_pit(benchmark_id, as_of, end=expires_date)`
- `as_of=None` 代表“每个交易日取当前最新版本”；显式 `as_of` 也代表结算回放时点。随后 `_window_return()` 从这批数据中选择 `bar_date/mark_date <= made_date` 的 entry。因此：
  - 预测作出后才发布、但 `bar_date` 恰为 made_date 的日收盘会被当作 entry；
  - 预测作出后对 entry 日的修正版也会回写预测基准；
  - benchmark entry 有同样问题。
- `market_data.get_*_pit()` 本身正确实现了 `as_known_at <= as_of`；错误在调用方没有用 `fc["made_at"]` 读取 entry 快照。
- 只读探针：
  - made_at 当天已知 close=10；
  - 预测后同一 bar_date 修正为 close=20；
  - expires 日 close=11；
  - 实际输出：`late_entry_correction miss -0.44999999999999996`；
  - 正确的决策时点基准应为 10，实际收益约 `+0.10`，方向和 verdict 均被翻转。
- 修复要求：security 与 benchmark 都应把 entry 和 exit 分开读取/冻结。entry 必须以完整 `made_at` 作为 PIT `as_of`；exit 再按结算请求的 `as_of`（或明确规定的结算时点）读取。不能从 exit 快照的数据集反选 entry。
- 缺失回归测试：
  - 同一 entry 日在 `made_at` 前后各有一个版本；
  - made_at 位于交易日收盘前，当日 close 尚未知，应退回前一根已知价；
  - benchmark 同样两种场景。

### [高 / must-fix] 无效 endpoint 没有完全 fails closed

- 位置：`app/institute/forecasts.py:252-277`，尤其 `:274-277`。
- `_window_return()` 当前只检查：
  - `entry_v is None`
  - `exit_v is None`
  - `entry_v <= 0`
- 它没有检查 `exit_v <= 0`，也没有对 entry/exit 使用 `math.isfinite()`。
- 该缺口不是不可达状态：
  - `migrations/0006_market_data.sql:63-75` 明确允许价格为零或负数；
  - `benchmark_marks.value` 在 `migrations/0006_market_data.sql:110-120` 也没有正数 CHECK；
  - `market_data._require_number()` 接受 `Infinity`，比较运算也不能可靠排除 `NaN`。
- 只读探针使用公开域写入口写入 entry=10、exit=0，实际输出 `zero_exit miss -1.0`，而文件头契约要求 unusable input 必须 `invalid` 并写明 note。
- benchmark exit=0/负数也会进入 excess-return 计算，甚至可能把证券错误判成跑赢基准。
- 非有限值还可能产生 `nan` measured：long/short 会落到 `partial`，neutral 会落到 `miss`，同样不是 `invalid`。
- 修复要求：在计算前对两个 endpoint 的原始值、`close * adj_factor` 结果及最终 return 统一要求 finite；对于当前股票/指数结算规则，entry/exit 均须 `> 0`。任何失败都追加明确 problem note。
- 缺失回归测试：security/benchmark 的零、负数、`Infinity`，以及计算结果非有限。

### [高 / must-fix] create 接受非有限 threshold，且所谓 canonical JSON 可写出 `NaN`/`Infinity`

- 位置：`app/institute/forecasts.py:85-118`、`:65-67`、`:280-288`。
- `float("nan") <= 0` 与 `float("inf") <= 0` 都为 false，因此 `parse_settlement_rule()` 会接受它们。
- `_dumps()` 使用默认 `json.dumps()`，会把非有限数写成非标准 JSON token `NaN`/`Infinity`，不符合 canonical JSON 声明。
- 只读探针实测：`nan_threshold_accepted True`。
- threshold 为 NaN 时，long/short 的 `>=`、`<=` 均为 false，最终错误落到 `partial`；neutral 则落到 `miss`。
- 普通校验项是正确的：unknown type、缺 threshold、非数字、零/负 threshold、`price_vs_benchmark` 缺 `benchmark_id`、unknown fields 都会被拒绝；缺口仅在非有限数和布尔值（`True` 会被当作 `1.0`）。
- 修复要求：拒绝 bool，转换后要求 `math.isfinite(threshold) and threshold > 0`，并建议 `_dumps(..., allow_nan=False)` 作为最后防线。

### [中 / must-fix] 结构化 dedup 与 seed 幂等只在顺序调用下成立

- 位置：`app/institute/research.py:113-164`、`:171-225`；schema 见 `migrations/0012_research_thesis.sql:40,44-45`。
- enqueue 是“先 SELECT、后 INSERT”，migration 只有普通索引，没有唯一约束或覆盖整个检查+写入的事务。
- 5 路并发只读探针实测：
  - `concurrent_structured_ids 5`
  - `concurrent_structured_rows 5`
- 因此两个并发 `seed-from-theses` 或同一结构化 enqueue 会生成多个相同 dedup_key 的 pending 行；现有测试 `tests/test_research.py:334-359` 只证明顺序重放幂等。
- topic 旧轨在改动前已有相同竞态；这不应阻止保留旧行为，但新 structured rail 不能据此宣称并发幂等。
- 建议为非 NULL dedup_key 增加只覆盖 `pending/running` 的部分唯一约束，并将冲突映射为读取赢家；或以事务/锁原子化结构化轨的 check+insert。补 5 路 gather 测试。

### [中] `${TOPIC}` 上下文无长度上限，API 可造成 prompt 成倍膨胀

- 位置：
  - `app/api/research.py:14-27`
  - `app/institute/research.py:98-105`
  - `app/institute/research.py:306-331`
- `topic`、`question`、`output_type`、`priority_reason` 都是无界字符串；数据库中的 thesis/security 名称也无应用层截断。
- `_topic_with_context()` 的格式清楚，topic-only 值保持不变；但结构化值会被代入 research workflow 多个步骤中每一次 `${TOPIC}` 出现的位置。超长 question/topic 会在七步 prompt 中反复复制。
- 当前没有字符上限、字节上限或安全截断，也没有对应测试。因此“不会让 prompt 爆炸”目前不能成立。
- 建议在 domain 层设定可解释的字段上限（API 只做前置友好校验），并对从数据库拼入的显示名称做防御性总长度上限。

### [中] seed 的 cap/API 边界会静默改变调用者意图

- 位置：`app/institute/research.py:186-190`、`app/api/research.py:25-27,50-57`。
- `cap = max(1, int(cap))` 会把 0 或负数静默改为 1。只读探针实测 `seed_cap_zero 1 1`，即请求“不入队”反而入队一条。
- API 对 cap 没有正数/最大值约束，`SeedBody` 也未设置 `extra="forbid"`；未知字段会被忽略。
- 正数 cap 的实现语义合理：它限制本次实际新增数，dedup/refused 不占额度，并继续扫描以统计 `matched`。
- 建议非正 cap 返回 400/422，并设置合理上限；如果产品需要 cap=0 表示 dry/no-op，则应原样返回 0 而不是改成 1。

### [低] 双轨文档中的“逐字等价 / ANY pending”与实现互相矛盾

- 位置：`migrations/0012_research_thesis.sql:13-16,26-29`，对应实现 `app/institute/research.py:121-153`。
- 实际 SQL 增加了 `dedup_key IS NULL`，所以查询文本当然不是 byte-identical。
- 对迁移前可达状态而言语义等价：所有旧 queue/log 行的 dedup_key 都是 NULL，旧行仍得到同一结果。
- 在 structured 行存在后则刻意不等价：topic-only 不会匹配同 topic 的 structured pending/completion。这正是“双轨独立”的设计要求。
- migration 注释一处称“queries stay byte-identical”，另一处又称 topic-only 匹配 “ANY pending row with the same topic”，都与实际独立轨实现不符。建议改成“对全部迁移前/NULL 轨行语义等价”。

### [低] 非法 settle `as_of` 没有映射为 forecast API 400

- 位置：`app/institute/forecasts.py:325,336`、`app/api/forecasts.py:13-20,37-41`。
- 非法 `as_of` 由 `market_data.get_*_pit()` 抛出 `MarketDataError`，而 forecast API `_call()` 只捕获 `ForecastError`，生产 app 会落到 500。
- 建议在 forecast domain 入口规整 `as_of` 并映射为 `ForecastError`，或在 API 明确捕获 market-data validation error。

## M3-001 acceptance 逐条裁决

### 1. old topic-only enqueue still works：PASS（带语义限定）

- topic-only 新列默认/写入均为 NULL：`migrations/0012_research_thesis.sql:35-42`、`app/institute/research.py:159-164`。
- 旧 pending dedup、旧 cooldown 对迁移前行语义不变；structured 行与 topic 轨刻意互不影响。
- `recover_orphans()` 的 UPDATE 在 `app/institute/research.py:228-241` 未改。
- `_claim_next()` 的 daily cap 仍只按 `work_date() = ?` 计数，legacy NULL work_date 继续不计入：`app/institute/research.py:257-282`。
- `_claim_next()` 只把 `SELECT` 改为完整行以取得新列；priority 顺序、单 running 限制和条件认领 rowcount 均未改变。
- `tests/test_research.py:315-331` 覆盖 pre-0012 行完整 tick；`:109-147` 覆盖 A1 work_date cap 边界；`:64-87` 覆盖孤儿恢复。

### 2. structured enqueue stores thesis/security/question：PASS

- 新字段写入完整，thesis/security 外键存在性与 anchor 规则有校验：`app/institute/research.py:101-113,157-168`。
- migration 的 6 个 queue 列和 log dedup_key 全部可空；旧行保持 NULL。
- 测试覆盖字段保存、未知 thesis/security、缺 thesis anchor。

### 3. dedup uses thesis, security, normalized question：PARTIAL / 并发不通过

- key 的字段边界使用 `\x1f` 分隔并 SHA-256：`app/institute/research.py:62-75`。
- question 规范化顺序为 NFKC → 空白折叠/trim → casefold；casefold 不引入空白，因此与“NFKC + casefold + 空白折叠”的目标等价。
- 中英混排、全半角 Latin、全角标点、全角空格均会落入预期等价类；中文本身不受 casefold 破坏。
- `None`、空字符串和纯空白在 enqueue 后都成为 `None`，hash 中统一为空字符串；该边界一致。
- 同一 triple 的顺序 pending dedup、completion cooldown 均正确；不同 question/security 分离。
- 完成路径确实把 queue 的 dedup_key 写入 `research_log`：`app/institute/research.py:357-376`。仓库应用代码只有这一处写 research_log，其他直接 INSERT 均为测试。
- priority > 0 在共享判断 `app/institute/research.py:154-155` 绕过两轨 cooldown；两轨一致。pending/running dedup 不被 priority 绕过，两轨也一致。
- 并发 check+insert 竞态使该 acceptance 不能完整通过，见问题分级。

### 4. imported practical.actionCode can seed research candidates：PASS-WITH-ISSUES

- 只扫描 kind=thesis、candidate/active/watch，保留 dormant/retired 不播种：`app/institute/research.py:176-194`。
- 正确读取 `metadata_json.practical.actionCode`，按 priority/id 稳定排序。
- 正数 cap、顺序重放、cooldown refusal 语义正确，测试覆盖三次重放。
- cap 非正数、API 上限与并发幂等存在问题，见问题分级。

## M3 其他核验

- `_topic_with_context()` 注入格式可读，包含 thesis id/name/current_view、可选 security id/name 和 question；topic-only 原样返回。
- 上下文只进入 `${TOPIC}` 的值，`research_log.topic` 仍存 plain topic。
- `research_log.dedup_key` 在完成且 queue 条件更新成功后写入；取消竞态时不会错误落 log。
- 无长度上限，见中等级问题。

## M5-001 acceptance 逐条裁决

### 1. forecast requires thesis, claim, horizon, direction, settlement_rule：PARTIAL

- 普通输入下全部必填字段、thesis/security 存在性、正整数 horizon、direction、conviction 0..1、unknown fields 都有 domain 校验。
- 两种 launch rule 都要求 security_id，符合当前取价能力。
- settlement_rule 的 unknown type、零/负 threshold、缺 benchmark_id、unknown fields 均拒绝。
- canonical dict 的 key 顺序稳定，expires_at 固定由 made_at + horizon_days 计算，持久时间使用 `bus.now_iso()`。
- 非有限 threshold 可绕过并写出非标准 JSON，因此整体只能判 PARTIAL。

### 2. settlement can record hit/miss/partial/invalid：PARTIAL

- 正常有限输入下四种 verdict 均可落库，forecast status 与 invalid/settled 对应。
- 判定矩阵正确：
  - long：`measured >= threshold` 为 hit，`measured <= 0` 为 miss，中间为 partial；
  - short：等价于 `measured <= -threshold` 为 hit，`measured >= 0` 为 miss，中间为 partial；
  - neutral：`abs(measured) <= threshold` 为 hit，否则 miss，没有 partial。
- 边界正确：long/short 恰好等于 threshold 为 hit，恰好 0 为 miss；neutral 恰好等于 threshold 为 hit。
- `close * adj_factor` 用于 security return，正常路径正确。
- entry PIT 前视、无效 endpoint 和 NaN threshold 会产生非 invalid 的错误 verdict，故整体不通过。

### 3. invalid benchmark fails closed：PARTIAL

- benchmark id 不存在、无 marks、没有 entry mark、entry 后无新 mark，都会形成 problem 并写 `invalid` note。
- benchmark 第一条 mark 的 `mark_date` 晚于 made_date 时，`_window_return()` 找不到 entry，正确 invalid，不会仅按日期直接后视取第一条。
- 但 made_date 当日/之前的 mark 若在 made_at 之后才首次得知或修订，当前单次 latest/as_of 查询会把它当 entry；这是知识时点 lookahead。
- benchmark 非正/非有限 exit 也不会 invalid。

## fails-closed 七类缺数/坏数路径逐项

1. **forecast 无 security_id：通过。** `app/institute/forecasts.py:322-323` 追加明确 note。
2. **security 窗口完全无 rows：通过。** `_window_return():263-264` 返回 `no data in window`。
3. **只有 made_date 之后的 security rows，无 entry：通过。** `:265-270` 返回 `no entry value at or before ...`。
4. **有 entry、没有严格更晚的 exit：通过。** `:271-273` 返回 `no value after entry date ...`。
5. **security endpoint 不可用：部分失败。** entry `<=0` 会 invalid；exit `<=0`、任一 endpoint 非有限不会 invalid。
6. **benchmark id 不存在：通过。** `:332-335` 返回 `benchmark ... not found`。
7. **benchmark series 缺失/不完整/不可用：部分失败。** 空 rows、无 entry、无后续值、非正 entry 会 invalid；非正/非有限 exit 不会。

补充：

- `as_of` 早于全部版本时，PIT getter 返回空 rows，能走 invalid。
- 每个已识别的 `problems` 都通过 `"; ".join(problems)` 进入 settlement note；可同时记录 security 与 benchmark 两边问题。
- 当前漏网路径不会生成 problem，因此 note 会写成普通 measured 说明，进一步掩盖数据质量问题。

## 条件认领、事务与唯一约束

- **PASS。**
- 初始 status 检查虽然在事务外，但最终 UPDATE 使用 `WHERE id=? AND status='open'`：`app/institute/forecasts.py:363-372`。
- 代码读取 `cur.rowcount`，输家抛 `TransitionConflict`；符合硬规则。
- status 翻转与 settlement INSERT 位于同一个 `db.transaction()`；异常会整体 rollback。
- `migrations/0013_forecasts.sql:61-62` 的 `UNIQUE(forecast_id)` 为第二道防线。
- `tests/test_forecasts.py:302-314` 使用真实 5 路 `asyncio.gather`，断言一个 dict winner、四个 TransitionConflict loser、最终一行 settlement；测试不是 mock rowcount。
- 同进程的 `db._write_lock` 会串行化事务，但 SQL 条件和数据库 UNIQUE 仍使跨连接/跨请求的单赢家语义成立。

## migration 核验

### 0012

- 6 个 research_queue 新列及 research_log.dedup_key 全部 nullable、无非 NULL default；迁移前旧行自然为 NULL。
- FK 在删除 thesis/security 时 SET NULL，队列/历史保留；dedup_key 不被清空。
- 只新增列和索引，无旧列重写/回填。
- 缺少 structured active-row 唯一约束，导致并发 dedup 问题。

### 0013

- 两张新表和索引均为 additive `CREATE ... IF NOT EXISTS`。
- closed enum CHECK 完整覆盖：
  - direction：long/short/neutral；
  - forecast status：open/settled/invalid；
  - settlement verdict：hit/miss/partial/invalid；
  - horizon_days > 0。
- thesis FK 默认 NO ACTION，保护问责台账；security 删除 SET NULL 后 settle fails closed；settlement 删除策略为随 forecast CASCADE。
- one-settlement-per-forecast 由 UNIQUE index 保证。
- conviction、claim 非空、settlement_rule grammar/canonical JSON 由 domain 校验，migration 注释已明确不锁死开放规则集合；该分层可接受，但 domain 的非有限数缺口必须修复。

### B1 migration 纪律

- 对 `0012_research_thesis.sql`、`0013_forecasts.sql` 搜索 `BEGIN/COMMIT/ATTACH/VACUUM` 无命中；也没有 `ROLLBACK/END` 事务语句。
- 两文件可由 `db.migrate()` 逐语句放进每文件单事务执行。
- 当前两个文件均为新增未跟踪文件，没有改写旧 migration。

## 硬规则与集成项

- **条件认领 rowcount：通过。**
- **migration 只增：通过。**
- **时间：通过。** forecast/research 持久化时间使用 `bus.now_iso()`；research cap/log 使用 `work_date()`，A1 SGT 语义未破坏。
- **A7 不可变 PIT：写侧通过、读语义不通过。** forecasts 只调用 `get_bars_pit/get_marks_pit`，没有写或覆盖 A7 版本；但 entry 传错 `as_of`，见高等级问题。
- **prompt 文本：B6 可归因范围通过，但工作树全局检查非空。**
  - B6 没有编辑 `workflows/` 或 `app/institute/prompts.py`；
  - 当前 `workflows/research.json` 的 aggregate HEAD diff 有一处其他代理的 `analyst`→`analyst_id`，prompt 字符串本身逐字未变；
  - 当前 `app/institute/prompts.py` 也有其他代理的 memory_block 改动；
  - 因而不能声称“当前整个工作树对这两处 git diff 为空”，只能确认 B6 的 `${TOPIC}` 注入不改模板文本。
- **forecast router 尚未生产挂载。**
  - `app/main.py` 当前只挂了 market_data，尚未 import/include `api_forecasts.router`；
  - `PATCH-NOTES-B6.md` 的补丁位置与现有 import/tuple 兼容；
  - 补丁落地前，裸 router 测试虽通过，运行中的主 app 不暴露 `/api/forecasts/*`。这是主代理集成项，不归因 B6 独占分区，但合并前必须完成并建议补 create_app 路由 smoke test。

## 测试充分性

现有 27 个定向测试对正常路径覆盖较好，尤其包括：

- pre-0012 NULL 行、A1 work_date cap、孤儿恢复；
- NFKC/全半角/空白/casefold 顺序 dedup；
- 双轨顺序独立、structured cooldown、priority 绕 cooldown；
- thesis context 注入及 log dedup_key；
- 正数 cap 与顺序播种幂等；
- required forecast fields 与普通 rule 错误；
- long/short/neutral 的正常 hit/partial/miss；
- adjusted close、benchmark excess return；
- 无数据、无 entry、无后续价、未知 benchmark、空 marks、删除 security；
- 5 路 settlement 单赢家和过期门槛。

必须新增的回归测试：

1. entry 日在 made_at 前后两个 PIT 版本，security 与 benchmark 各一组；
2. made_at 在当日 close 发布前，应退回此前最后一根“当时已知”价；
3. security/benchmark 的零、负数、NaN/Infinity endpoint；
4. threshold NaN/Infinity/bool；
5. 5 路 structured enqueue/seed 只生成一个 active row；
6. seed cap=0/负数/超上限与 unknown API fields；
7. 上下文字段长度上限；
8. 非法 settle as_of 映射 400；
9. `create_app()` 确认真正挂载 forecast router。

## 最终裁决

- M3-001：**PASS-WITH-MUST-FIX-ISSUES**（顺序 acceptance 基本成立；并发 dedup、cap 边界、上下文长度需修）。
- M5-001：**FAIL**（PIT entry 前视与 fails-closed 漏网会产生错误 verdict）。
- 合并总裁决：**FAIL**。
