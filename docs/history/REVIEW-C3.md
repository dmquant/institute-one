# REVIEW-C3 — Phase 5 前两项 + Paper Book API 独立审查

审查日期：2026-07-20  
审查代理：R-C3  
审查边界：只审 C3 指定分区；其余并行代理改动仅在核对接口契约时只读参照。  
结论：**FAIL**

## 一、结论摘要

16 个定向测试与 `compileall` 均通过，正常路径上的 PIT entry、short 收益符号、stop/target/horizon 分支、B6 双结算防护、API 400/404/409 映射、迁移基本纪律都成立。

但当前实现存在会直接污染预测与纸面账本的阻断问题：

1. 真实导入数据必带的 `kind='ticker'` 六位数字别名会绕过 YYYYMM、小数尾和金额单位守卫，测试库没有模拟该生产形态；
2. 到期仓位仍以 `work_date` 取价，会读取到期后的价格，甚至把本应 horizon 平仓的仓位改判成 target/stop；纸面 PnL 与 B6 settlement 可使用两个不同终点；
3. 不可定价仓位把“未知”当成零收益，NAV 会从最后已知估值跳回 entry-flat，并把 `realized_pnl=0` 永久计入业绩；
4. 跨上市同名实体会同时生成多条 forecast；即使句中已有 canonical ticker，低证据名称轨仍会额外加入另一上市地；
5. `forecast_extractions` 的认领与逐条 `create_forecast()` 不原子；崩溃后重放被封死，而文档建议的 DELETE 强制重抽会复制已经成功的部分；
6. opener 的“同标的最多一个 open position”没有数据库仲裁，并发 tick 可开出两仓；
7. 否定与 horizon 上限存在确定反例：`看多？不` / `不建议看多` 仍判 long，`2026年内` 被通用 `年内` 重新命中为 365 天。

此外，ROADMAP Phase 5 第二项原文包含 attribution 回流 analyst memory；C3 明确延期，且当前 forecast provenance 对 daily 多作者报告不足以可靠归因，所以不能宣称“Phase 5 前两项完成”。

## 二、问题分级（附行号）

### C3-H1 / HIGH / must-fix：ticker 别名绕过 YYYYMM、金额单位与小数守卫

- 位置：
  - `app/institute/forecast_extract.py:208-232`
  - `app/institute/forecast_extract.py:253-265`
  - `app/institute/market_thesis_import.py:371-381`
  - `migrations/0004_securities.sql:65-86`
- `_DATE_LIKE_RE`、金额单位和小数尾只作用于 `_BARE_CODE_RE` 轨（`:253-261`）。
- `_load_name_table()` 不读取/保留 alias kind，把所有 alias 统一送入 `_name_hits()`（`:221-232`）。
- `_name_hits()` 对纯数字 alias 只做 ASCII 字母数字边界（`:216-218`），所以：
  - `200012` 可命中“根据200012的研究”；
  - `600519` 可命中“成交额600519万元”；
  - `600519` 可命中“指标600519.5”。
- 这不是敌意数据库才可达。现有 importer 会为每个证券自动插入 `symbol` 的 `kind='ticker'` alias（`market_thesis_import.py:371-381`）；`0004` 也明确把 unsuffixed ticker 定义为 alias 层用途。
- 一次性探针实际输出：

```text
200012 '根据200012的研究我们看多' True
600519 '成交额600519万元' True
600519 '指标600519.5' True
```

- 因此现有测试 `tests/test_forecast_extract.py:134-142` 只证明“没有 ticker alias 的人工 fixture”能拒绝，不能证明生产库能拒绝。
- 修复要求：
  1. alias 加载必须保留 `kind`；
  2. `kind='ticker'` 必须走与 bare-code 相同的完整 guard，或直接从名称轨排除 ticker alias；
  3. 加入带真实 ticker alias 的 YYYYMM、金额单位、小数尾回归测试。

### C3-H2 / HIGH / must-fix：horizon 平仓读取到期后的价格

- 位置：
  - `app/institute/paper_book.py:320-353`
  - 对照 `app/institute/forecasts.py:382-397`
- 当前先用 `expires_at <= now` 判定已到期（`:322`），但无论是否到期，都用 `_latest_mark(..., wd)` 取到 `work_date` 的最新价（`:323`）。
- 随后 stop/target 又排在 horizon 前（`:340-345`）。如果 MTM 在到期后一天或停机恢复后运行，到期后的价格可以：
  - 改写 horizon 的 close price / realized PnL；
  - 把 close reason 从 horizon 改成 target/stop。
- B6 settlement 明确把 exit 截在 `expires_at[:10]`（`forecasts.py:382,395`），所以同一 forecast 的 paper position 与 settlement 会使用不同终点。
- 一次性探针：
  - entry=10；
  - expires_at=2026-06-15；
  - 到期日 close=10.2；
  - 到期后 2026-07-20 close=20；
  - `mark_to_market("2026-07-20")` 实际得到：

```text
paper position: reason=target, close_price=20, realized_pnl=1.0
forecast settlement: verdict=partial, actual_return=0.02
NAV=2.0
```

- 这在正常日调度也并非纯理论：forecast 按精确 UTC 时刻到期，而 MTM 只在每日 00:00 SGT 运行，首次观察到过期通常已经晚于 horizon。
- 修复要求：已到期仓位至少把 mark 的 `end` 截为 `min(wd, expires_at[:10])`；若要求停机恢复后完全还原状态机，还需按日期扫描到 horizon 为止的 bars，取第一个 stop/target 触发点，而不是只看最后一根。
- 必须新增“到期后存在剧烈价格变化但不得影响平仓”的回归测试。

### C3-H3 / HIGH / must-fix：不可定价被记成零收益，污染 NAV

- 位置：
  - `app/institute/paper_book.py:327-339`
  - `app/institute/paper_book.py:357-368`
  - `migrations/0017_paper_book.sql:42-44,59-60`
- 未到期但不可定价时，代码把 unrealized contribution 直接留为 0（entry-flat）。
- 到期仍不可定价时，代码写入：
  - `close_price=NULL`
  - `realized_pnl=0.0`
- 随后 NAV 无条件把所有 closed position 的 `realized_pnl` 相加（`:357-368`）。
- 这不是 fails closed：价格是未知，但系统发明了“收益恰好为 0”的已知结果。它还会让此前已经进入 NAV 的浮盈/浮亏消失。
- 一次性探针实际复现：

```text
最后可用 mark 后 NAV = 1.08
删除 security 后同日重跑 NAV = 1.00
到期后 NAV = 1.00
closed row = {close_price: NULL, realized_pnl: 0.0, close_reason: horizon}
```

- 跳变在首次变成不可定价时就发生；到期写 0 又把该中性结果永久固化。现有测试使用 entry=mark=10，恰好看不出跳变（`tests/test_paper_book.py:256-279`）。
- “释放 position cap”与“把未知业绩当 0”是两件事。可以关闭仓位释放 slot，但必须保留估值不确定性。
- 修复要求：在以下方案中明确选一个并固化口径：
  - 保存并沿用最后一个有效 mark，同时标记 stale/unpriceable；
  - `realized_pnl=NULL`，增加 invalid/unpriced 状态与 NAV completeness 标志；
  - 或令该日 NAV 不产出/标为不完整。
- 不接受继续用 0 作为未知结果。

### C3-M1 / MEDIUM / must-fix：名称歧义会双开 forecast，canonical 也压不住名称轨

- 位置：
  - `migrations/0004_securities.sql:72-76`
  - `app/institute/forecast_extract.py:221-265`
- `0004` 已明确记录跨上市同名实例（如中芯国际 A/H）。`_load_name_table()` 会同时加载两个 `securities.name_zh`；`_find_securities()` 则把所有命中的不同 sid 都加入结果。
- “strongest evidence first”目前只有返回顺序，没有 evidence arbitration。canonical 命中后只 mask canonical 字符串，不会抑制同句中的低证据同名项。
- 一次性探针：

```text
看多中芯国际
→ 688981.SH + 0981.HK

看多中芯国际（688981.SH）
→ 688981.SH + 0981.HK
```

- 第二例尤其违反用户意图：句中已经显式指定 A 股，仍会附带生成 H 股 forecast；后续 paper book 可能把同一句观点计成两笔独立 call。
- 修复要求：
  - 显式 canonical/bare ticker 命中时，不再由同一 mention 的名称轨补充其他 sid；
  - 仅名称/别名命中多个 sid 时应 fail closed 或要求 market/ticker 消歧；
  - 增加 0004 已知跨上市同名回归测试。

### C3-M2 / MEDIUM / must-fix：source claim 崩溃窗口不是安全可恢复的幂等

- 位置：
  - `app/institute/forecast_extract.py:360-396`
  - `migrations/0017_paper_book.sql:9-18`
- `forecast_extractions` claim 先独立提交，之后每个 `forecasts.create_forecast()` 又各自提交，最后才回填 `forecast_ids`。
- 如果进程在第 N 条 forecast 已提交后退出：
  - claim 行仍是 `n_forecasts=0, forecast_ids=[]` 或部分旧 bookkeeping；
  - 普通重放返回 duplicate，剩余 candidates 永久不创建；
  - 无法从 claim 行可靠知道已经创建了哪些 forecast。
- PATCH-NOTES 建议“DELETE claim 行后重抽”，但这会复制已成功的部分。一次性崩溃探针：

```text
崩溃后 claim: n_forecasts=0, forecast_ids=[]
实际 forecasts: 1
普通 replay: duplicate，仍为 1
DELETE claim 后强制重抽: 新建 2，最终 forecasts=3
```

- 所以当前语义是“可能丢失的 at-most-once”，不是 restart-safe exactly-once；文档中的强制重抽逃生通道也不安全。
- 修复要求：为每个候选建立稳定 source/candidate key 并加唯一约束，或把 claim、forecast rows、bookkeeping 放入同一可恢复事务/状态机。至少要有 `processing/completed/failed` 状态与可幂等逐候选重试。

### C3-M3 / MEDIUM / must-fix：同一标的 open-position 不变量没有数据库仲裁

- 位置：
  - `app/institute/paper_book.py:163-205`
  - `migrations/0017_paper_book.sql:63-64`
- candidate 查询中的 `NOT EXISTS` 与后续 INSERT 不在同一事务；`seen_securities` 只保护单次调用。
- schema 只有 `UNIQUE(forecast_id)`，没有 `UNIQUE(security_id) WHERE status='open'`。
- 两个并发 opener 都可能先看到同一标的无仓位；各自对不同 forecast INSERT 成功。
- 用 barrier 让两个真实 `opener_tick()` 同时通过 entry 读取后，实际得到：

```text
tick A opened=1
tick B opened=1
RACE.US open positions=2
```

- APScheduler 的 `max_instances=1` 会降低生产调度触发概率，但不能把应用声明的不变量变成数据库事实；同样的 check-then-insert 也可并发突破总 cap。
- 修复要求：
  - 增加 partial unique index：非 NULL `security_id` 在 `status='open'` 时唯一；
  - opener sweep 使用事务/显式单进程锁重新检查 cap；
  - 冲突读取赢家并计入 summary；
  - 增加 `asyncio.gather` 并发回归测试。

### C3-M4 / MEDIUM / must-fix：否定边界与 horizon 上限存在确定反例

- 位置：
  - `app/institute/forecast_extract.py:88,104-114,125-138,164-195`
- 正确部分：
  - `不看空反而看多` → long；
  - 句内最后一个未被前置否定的方向命中胜出；
  - 37 月、105 周、366 天等超过单位 cap 的直接数字 cue 会被忽略。
- 错误部分：
  - `_NEGATION_RE` 只检查命中前最多 8 字符；`看多？不` → long；
  - sentence splitter 又会把问号后的“不”拆掉，所以完整抽取同样会保留前半句 long；
  - `不建议看多` → long，因为“不”没有紧贴 cue；
  - horizon 的静态 `年内` pattern 会在数字 year pattern 被 cap 拒绝前/后独立命中其后缀，导致 `2026年内` → 365 天；
  - `2026年内，未来2周` 也取错误的 365 天。
- 一次性探针：

```text
不看空反而看多 -> long
看多？不       -> long
不建议看多     -> long
2026年内       -> 365
2026年内，未来2周 -> 365
```

- 现有 horizon 测试只覆盖 `2026年公司将投产`，没有覆盖更常见的 `YYYY年内`。
- 修复要求：
  - 对后置否定/问答式短句增加明确拒绝规则，至少覆盖上述两个反例；
  - 数字年份与通用 `年内` 必须做重叠抑制，不能让被 cap 拒绝的 span 被另一个 pattern 的子串重新接受；
  - 新增对应端到端 candidate 测试，而不只测 helper。

### C3-M5 / MEDIUM / scope must-fix：ROADMAP 的 analyst attribution 尚未实现

- 位置：
  - `ROADMAP.md:149-150`
  - `PATCH-NOTES-C3.md:11,122-127`
- ROADMAP Paper book 项原文包含 “attribution flows into analyst memory”。
- C3 明确把它延期，只提供 `paper_book.closed` 事件，故“Phase 5 前两项完成”的认领不成立。
- 这不只是少一个 handler：
  - forecasts 表没有 source_ref/analyst_id；
  - `forecast_extractions.forecast_ids` 可反查 source，但 daily report 是多作者聚合；
  - fallback thesis `auto-forecast-extract` 也不能表达具体作者。
- 后续仅从 `forecast → thesis/research` 不一定能可靠定位 daily forecast 的作者。若要满足原文，应先明确 attribution 粒度和 provenance 数据模型。

### C3-S1 / MEDIUM / should-fix：close 与 settlement 的安全性通过，崩溃恢复性不完整

- 位置：
  - `app/institute/paper_book.py:220-264,349-353,427-430`
- `_close()` 的 `UPDATE ... WHERE status='open'` + rowcount 正确；B6 settlement 自身也以同事务条件认领和 `UNIQUE(forecast_id)` 防双结算。
- 预结算 probe 有效：forecast 已 settled 时，position 仍可关闭且 settlement 保持一行。
- 但 position close、`paper_book.closed` emit、`_maybe_settle()` 是三个独立提交步骤。进程在 close 后退出，或 emit 抛错时，position 已 closed，而 expired forecast 可能永久保持 open；重跑 MTM 不再扫描 closed position。
- PATCH-NOTES 提到未来批量结算 job，但当前尚无修复闭环。建议增加 reconciliation：扫描“closed position + expired open forecast”并幂等调用 B6 settlement。

### C3-N1 / LOW：跨模块私有价格 helper 应升级为公共契约

- 位置：`app/institute/paper_book.py:112-149`
- 当前调用 `forecasts._usable_price()` / `forecasts._adj_close()` 的行为与 B6 完全一致，PIT correction probe 也有效，当前数值语义通过。
- 但这两个下划线函数现在已经是 forecast settlement 与 paper book 共同依赖的资金口径，不再是单模块实现细节。建议提升为公开 helper（或独立 `price_math` 模块）并保留一组共享契约测试，防止 B6 重构时账本静默漂移。

### C3-N2 / LOW：API 边界是钳制而非拒绝，config 浮点会被截断

- 位置：
  - `app/api/paper_book.py:22-46`
  - `app/institute/paper_book.py:96-109,440-459`
- 已通过：
  - unknown status → 400；
  - position 不存在 → 404；
  - double close → 409；
  - manual close 无可用价 → 400；
  - 非整数 query 由 FastAPI → 422；
  - limit/days 有硬上限，无法造成无界查询。
- 但 `limit<=0` / `days<=0` 会静默改成 1，超上限静默截断；若 API 契约希望严格输入校验，应使用 `Query(ge=1, le=...)`。
- `max_positions()` 文档声称只接受整数，但 JSON 中 `1.9` 会经 `int(raw)` 变成 1。应要求 `n == raw`，与 B6 的正整数校验风格一致。

## 三、正则抽取专项裁决

### 方向

- “最后一个未否定命中胜出”的主实现正确。
- `不看空反而看多` 正确得到 long。
- 仅前置、紧邻式否定不足以覆盖报告常见问答/建议句式；`看多？不`、`不建议看多` 为确定误报，判定不通过。

### 双层停用词

- 精确 alias `沪深300`：
  - `_load_name_table()` 会过滤一次；
  - `_name_hits()` 会再过滤一次；
  - 所以即使 alias 表恶意绑定到某 security，也不会按名称轨解析。该精确场景通过。
- “无论如何拒绝”的表述仍过强：
  - `沪深 300`、`沪深300指数` 等内部空格/扩展变体不在 exact stopword set，可进入名称轨；
  - 更严重的是 ticker alias 完全绕过动态 bare-code guard，见 C3-H1。

### YYYYMM 误伤与 canonical 轨

- `0004` 对 CN_A canonical id 只要求六位数字 + `.SH/.SZ/.BJ`，没有按证券代码段排除 YYYYMM 形态（`migrations/0004_securities.sql:51-59`）。
- `202001` 的公开市场含义是基金代码，不是已核实的 `202001.SZ` 股票；但真实的日期形态证券确实存在，例如深市 B 股 `200012.SZ`（南玻B）。
- 所以误伤不是纯理论：无 ticker alias 时，裸 `200012` 会被 `_DATE_LIKE_RE` 拒绝。
- canonical `200012.SZ` 路径确实不受 `_DATE_LIKE_RE` 影响；名称路径也可命中。这部分 C3 声明成立。
- 但生产 importer 会插入 alias `200012`，反而让裸码重新从名称轨命中，实际破坏了“日期优先拒绝”的设计。应使用真实 `200012.SZ` + ticker alias 更新测试。

### conviction

- 同句谨慎 cue 压过强烈 cue 是明确的保守偏置，作为低成本规则可接受；它会把“谨慎情绪消退，强烈看多”仍判 0.35，但这是语义精度限制，不单独阻断。
- 建议文档继续明确它不是按语序/修饰对象解析。

### horizon

- 单位上限实际为：36 月、12 季、3 年、104 周、365 天；上限内按天换算，超过上限的直接 cue 忽略。
- `2026年内` 的重叠子串 bug 使“上限防年份”契约不成立，见 C3-M4。

### 每源 5 条与同标的首句胜

- `seen` 在追加 candidate 时更新，后续句不会翻转同 security；达到 5 条立即返回。实现符合声明。
- 名称表查询没有 ORDER BY，单句名称命中超过 5 个时，截取哪 5 个依赖数据库返回顺序；在修复歧义解析后该风险会明显降低。

## 四、Paper Book 专项裁决

### Entry PIT 与前视探针

- `_entry_bar()` 使用 `get_bars_pit(security_id, made_at, end=made_at[:10])`，最后一根再做 adjusted close + positive finite whitelist。
- made_at 后同 bar_date 的 10→20 correction 不会改变 entry=10；现有测试是真实 PIT store，不是 mock，结论可信。
- 当前数值语义通过；私有 helper 接口风险见 C3-N1。

### 状态机与 short 符号

- `_signed_return = price/entry - 1`，short 再取负，10→8.5 得 +15%，实现及测试正确。
- 对同一个合法 mark，stop → target → horizon 的判断顺序符合要求。
- 但 expired position 的 mark 时间上界错误，导致 post-horizon stop/target，故状态机总体不通过（C3-H2）。

### NAV

- 正常有价平仓时，本轮不再计 unrealized，新 close row 会立即进入 realized aggregate；同一个 ret 在同次 MTM 中完成未实现→已实现迁移，正常口径连续。
- 已关闭历史仓位累计 realized + 当前 open unrealized 的公式实现与文档一致。
- unpriceable=0 会把未知当平盘并造成 NAV 跳变，整体不通过（C3-H3）。
- `mark_to_market(wd=历史日期)` 使用当前 open/closed 集合和全部累计 realized，没有按 `opened_at/closed_at` 截止到 wd；因此该参数不能安全用于历史回放。若只允许“当天运行/当天重跑”，应显式校验或改名；若支持 backfill，则需按 wd 做时点过滤。

### Benchmark

- 首次可用 CSI300 mark 会写入固定 base，之后按 value/base；最新 mark 不可用时返回 NULL。主路径与测试通过。
- 存储 base 若为 0/坏值，代码会静默以当前 mark 重钉并返回 1.0（`paper_book.py:282-300`），与 docstring“unusable stored base → NULL”不一致；低概率但应统一文档/实现。

### Settlement linkage

- double-settlement safety 通过：预检查只用于省调用，最终单赢家由 B6 条件 UPDATE + 同事务 INSERT + UNIQUE 保证。
- crash liveness 不通过，见 C3-S1。

## 五、API、迁移与硬规则

### API

- 文件实际提供 4 个 route operation：
  - `GET /api/book/positions`
  - `GET /api/book/positions/{position_id}`
  - `POST /api/book/positions/{position_id}/close`
  - `GET /api/book/nav`
- bare-router 测试有效，manual close 无价 400 通过。
- 当前 `app/main.py:152-182` 尚未 import/include paper-book router；在主代理应用 PATCH-NOTES 前，生产 app 不暴露这些 API。

### 迁移 0017

- B1 纪律通过：
  - 只新增一个 migration；
  - 没有 migration 内 BEGIN/COMMIT/ROLLBACK/ATTACH/VACUUM；
  - CREATE 均带 IF NOT EXISTS，seed 使用 INSERT OR IGNORE；
  - 测试 fixture 已在 fresh DB 上实际应用整个 migration chain。
- 唯一约束：
  - `forecast_extractions.source_ref UNIQUE`：顺序重放单赢家通过，但副作用崩溃一致性不通过；
  - `paper_positions.forecast_id UNIQUE`：同一 forecast 终生一仓通过；
  - 缺少 open `security_id` partial unique，见 C3-M3。
- `paper_positions` 没有 schema-level 状态一致性 CHECK（如 closed 必须 closed_at/reason、NULL close_price 时 realized 规则）；当前只有 domain 写入口，属防御性缺口，不单独阻断。

### 硬规则

- 无模型调用：**PASS**。抽取纯 regex/DB，paper book 纯 PIT/DB；无 executor。
- isfinite 白名单：**PASS（正常入口）**。复用 B6 `_usable_price/_adj_close`，return 再检查 finite。
- `bus.now_iso()` / `work_date()`：**PASS**。新增持久时间与“今天”逻辑符合约定；SGT window 的 datetime 只做确定性换算，不读取 raw now。
- 条件认领：
  - position close：**PASS**；
  - B6 settlement：**PASS**；
  - source claim INSERT rowcount：**形式 PASS、崩溃恢复 FAIL**；
  - opener per-security/cap：**FAIL**。
- migrations only-add：**PASS**。

## 六、PATCH-NOTES-C3 挂载核验

### main.py

- API import/include 建议与当前 `create_app()` 结构匹配，可直接应用。
- `forecast_extract.register()` 放在 lifespan、`vault_exporter.register()` 邻近，事件名正确：
  - research 实际 emit `research.completed`，`event.ref_id` 是 queue item id；
  - daily 实际是 `workflow.completed`，payload 确有 `workflow_id`，run id 可由 `event.ref_id` 回退。
- 当前两项都尚未挂载；修复本报告 must-fix 前不建议启用自动抽取/开仓。

### Scheduler gating

- 同意 paper opener 与 MTM 为 `gated=False`：
  - 两者不提交模型任务；
  - maintenance 的现行语义是阻止新模型调用，不是停止本地账本；
  - 停记 NAV/到期状态比继续本地记账风险更高。
- PATCH 中 `@metered("paper-opener")` / `@metered("paper-mtm")` 虽未显式写 `gated=False`，当前 decorator 默认值就是 false，行为正确；显式写出可提高审计可读性。
- 5 分钟 interval 与 00:00 SGT cron 的调用方式匹配现有 scheduler。

### Exporter 与 SGT 日窗

- `_sgt_day_window()` 正确把 SGT 日期换为 UTC `[前一日16:00, 当日16:00)`；可包含 00:00 SGT MTM 刚写出的 close，不存在 UTC 日期前缀漏记。
- handler 由 `paper_book.marked` 触发后整日重渲，rows-as-truth、writer skip/hash/conflict 语义匹配。
- ROADMAP 原文是 append markers，C3 改成按日全量投影。该偏离有充分架构理由，我倾向接受，但应由主代理在 ROADMAP/PATCH 裁决中明确记录。
- 建议 frontmatter 同时传 `"created": wd`；否则回放旧日 event 时，writer 会用执行当天的 `work_date()` 作为 created。

## 七、测试充分性

现有 16 个测试对 happy path 覆盖较好，尤其是：

- made_at 后 correction 不改变 entry；
- long/short stop/target；
- stop/target 对 horizon 的代码顺序；
- 预结算只保留一条 settlement；
- manual close 无价 400；
- 每源封顶、同标的首句胜；
- 精确 generic stopword；
- 顺序 source replay 幂等。

必须新增的回归测试：

1. importer 形态的 `kind='ticker'` alias 对 YYYYMM、金额单位、小数尾不得绕过；
2. `中芯国际` A/H 歧义；显式 `688981.SH` 必须抑制 H 股名称命中；
3. `看多？不`、`不建议看多` 的端到端 candidate；
4. `2026年内`、`2026年内，未来2周`；
5. 到期日后存在剧烈新 bar，paper close 不得使用；
6. 先产生非零 unrealized，再删除 security，验证 NAV 不把未知重置为 0；
7. source claim 在第一条 forecast commit 后崩溃，恢复不得丢失或复制；
8. 两路并发 opener 对同 security/cap 只有一个赢家；
9. close 已提交、settlement 未调用时的 reconciliation；
10. 主 app router、extractor register、scheduler jobs、exporter handler 的集成 smoke test。

## 八、验证记录

指定验证：

```text
.venv/bin/python -m compileall app -q
PASS（exit 0）

.venv/bin/python -m pytest tests/test_forecast_extract.py tests/test_paper_book.py -q
16 passed in 0.72s
```

未运行全量测试。

额外只读/临时库对抗探针已覆盖：

- 方向否定与 horizon 上限；
- ticker alias guard 绕过；
- A/H 同名和 canonical 证据优先级；
- source claim 崩溃及 DELETE 重抽；
- post-horizon 取价；
- unpriceable NAV 跳变；
- 并发同标的开仓。

## 九、最终裁决

- Forecast extraction：**FAIL**。生产 ticker alias 绕过 guard、跨上市歧义、否定/horizon 反例及不可安全恢复的 source claim 均需修复。
- Paper book：**FAIL**。post-horizon 取价与 unpriceable=0 会直接污染 PnL/NAV；同标的并发唯一性也未落到数据库。
- API/migration 基础：**PASS-WITH-ISSUES**。常规状态码、B1 纪律和两项 UNIQUE 成立，但主 app 尚未挂载且缺 open-security 唯一约束。
- ROADMAP 完整性：**FAIL**。analyst attribution 尚未实现，不能把 Phase 5 第二项标为完成。
- 合并总裁决：**FAIL**。
