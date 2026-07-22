# REVIEW-A7 — M4-001 行情数据 PIT 存储独立审查

## 结论：FAIL

三条 backlog 验收的基本结构和正常路径均已实现，定向测试也全部通过；但 PIT 版本行并非真正不可变：相同 `as_known_at` 的不同数据会被 `DO UPDATE` 静默覆写，而且时间规整会丢弃亚秒精度，使本来不同的知识时点碰撞到同一版本键。该行为直接违背 migration 与域模块写明的“correction never overwrites / history is never overwritten”契约，属于合入前必须修复的数据审计风险。

## 审查范围与验证

- 全文审阅：`migrations/0006_market_data.sql`、`app/institute/market_data.py`、`app/api/market_data.py`、`tests/test_market_data.py`、`PATCH-NOTES-A7.md`。
- 一致性参照：`app/db.py`、`app/bus.py`、`app/main.py`、`app/api/theses.py`、`migrations/0004_securities.sql`、`roadmap/backlog.json`、`tests/conftest.py`。
- `.venv/bin/python -m compileall app -q`：通过。
- `.venv/bin/python -m pytest tests/test_market_data.py -q`：`15 passed in 0.52s`。
- 未运行全量测试，符合审查要求。
- `git status --short --untracked-files=all` 显示 A7 分区的五个交付文件均为未跟踪文件；工作树还有多项其他代理的在途改动，已按要求忽略。状态中未见 prompt 文件改动。`app/main.py` 当前有与关停排空/恢复相关的并行改动，但没有挂载 market router。

## PIT 语义逐点结论

### 1. `get_bars_pit` 相关子查询：通过

- `app/institute/market_data.py:388-395` 按 `(security_id, freq, bar_date)` 相关，选择 `MAX(v.as_known_at)`，同一交易日多版本的分组键正确。
- 有 `as_of` 时使用 `v.as_known_at <= ?`，所以恰好等于版本 `as_known_at` 的边界会包含该版本；`tests/test_market_data.py:139-141` 覆盖了等号边界。
- `as_of=None` 时不追加截止条件，得到每个 `bar_date` 的最新版本；`tests/test_market_data.py:142-145` 覆盖该分支。
- 当截止时点早于全部版本时，标量子查询返回 `NULL`，外层等式不命中，因此该 bar 被正确省略。
- `start`/`end` 只限制外层交易日，相关子查询仍在该日全部版本中选版本，语义正确；SQL 占位符与参数追加顺序一致。
- `get_marks_pit` 在 `app/institute/market_data.py:495-513` 使用同一正确模式。

### 2. 时间戳字符串排序前提：部分通过，但存在 must-fix

- `_norm_ts`（`app/institute/market_data.py:73-85`）覆盖 bar/mark 的 `valid_time`、`as_known_at` 写入口及 PIT 的 `as_of` 读入口。
- 实测非 UTC 偏移 `2026-07-01T16:00:00+08:00` 与 `Z` 后缀都会规整为 `2026-07-01T08:00:00+00:00`；裸日期规整为当天 `00:00:00+00:00`。因此这些格式进入应用层后可按字符串正确排序。
- 无时区的 datetime 会被当作 UTC；行为稳定，但调用方需要知道该约定。
- 问题是 `isoformat(timespec="seconds")` 会丢弃显式时间戳的亚秒部分；详见 must-fix #2。
- schema 本身不约束 `as_known_at` 的格式，直接 SQL 可以绕过规整；当前暴露的 bar/mark 域写入口没有绕过。`corporate_actions` 本卡只有表、没有域写入口，后续实现必须复用同一规整策略。

### 3. 相同版本键的 upsert 幂等：不通过

- `app/institute/market_data.py:346-349` 和 `:472-474` 在完整版本键冲突时刷新事实字段。
- `tests/test_market_data.py:164-174` 甚至把“相同 knowledge timestamp、不同 close 值时改写旧行”固化成预期。
- 这不是幂等重放：相同输入的重复执行才是幂等；相同版本键但不同事实应被视为冲突。当前实现会永久丢失第一次写入内容，且 `created_at` 不变，事后无法看出发生过覆写。
- migration 的 `migrations/0006_market_data.sql:14-18` 与域模块的 `app/institute/market_data.py:8-16,307-311` 明确承诺修正追加新行、历史不覆写，现有“field refresh”说明不能抵消该契约冲突。

### 4. 外键：通过

- `app/db.py:28-33` 在连接初始化时执行 `PRAGMA foreign_keys=ON`。
- 域层 `_check_security`（`app/institute/market_data.py:123-126`）先给出可读的 400；若并发删除穿过预检，数据库外键仍是最终防线。
- 测试并非只靠应用校验：`tests/test_market_data.py:211-219` 的 benchmark raw INSERT 验证真实 FK 拒绝，`:197-200` 与 `:313-315` 的级联删除也验证 FK 已启用。

## must-fix

### 1. 版本行必须不可变

位置：

- `app/institute/market_data.py:346-349`（price bar）
- `app/institute/market_data.py:472-474`（benchmark mark）
- `tests/test_market_data.py:164-174`（当前测试固化了错误语义）

要求：

- 完整版本键冲突时，不得更新已有事实字段。
- 相同键、相同 payload 可以作为真正的幂等重放返回已有行。
- 相同键、不同 payload 应返回明确冲突（建议域层 `TransitionConflict`/HTTP 409），提示调用方用更晚且真实的 `as_known_at` 追加修正版。
- benchmark mark 与 bar 必须采用同一策略；测试应同时覆盖两者。

### 2. 不得把不同亚秒知识时点折叠为同一版本

位置：

- `app/institute/market_data.py:73-85`，尤其 `:85`
- `app/bus.py:28-29`

实测 `2026-07-01T08:00:00.987654+00:00` 被规整为 `2026-07-01T08:00:00+00:00`。两个亚秒不同的显式修订会碰撞；省略 `as_known_at` 时，`bus.now_iso()` 也只有秒精度，同一秒内对同一自然键的两次写入同样碰撞。结合 must-fix #1 的 `DO UPDATE`，当前结果是无提示的数据覆写。

应保留足以区分修订的精度，并确保所有应用写入和 `as_of` 使用同一种规范化 UTC 表示。至少增加“同秒不同亚秒仍为两个版本”的回归测试。

## nice-to-have

- `app/institute/market_data.py:332-333,458-459` 用 `value or default`，导致调用方显式传空字符串时被当作“未传”并静默改用 bar date/当前时间。建议只在值为 `None` 时默认，空字符串继续交给 `_norm_ts` 报 400。
- 增加非 UTC 偏移、`Z`、裸日期、亚秒和 naive datetime 的端到端回归测试，避免字符串排序前提以后退化。
- 主代理挂载 router 后补一个基于 `create_app()`/路由表的 smoke test；当前裸 app 测试只证明 router 自身可用，不证明生产 app 已暴露 `/api/market/*`。

## 域模块与 API 核验

- 域模块数据库访问均通过 `db.query` / `db.query_one` / `db.execute`，未直接操作连接。
- `close_suspension` 的更新条件包含 `end_date IS NULL`，并检查 `db.execute` 返回的 rowcount（`app/institute/market_data.py:242-247`），条件认领正确。
- 日期域校验使用严格正则再调用 `date.fromisoformat`（`app/institute/market_data.py:62-70`）；`2026-1-1` 会被拒绝。schema 的固定宽度 GLOB 同样会拒绝该格式，测试用 `2026-10-1` 覆盖了同类边界。
- `freq` 在域层由 `FREQS={"1d"}` 守住（`app/institute/market_data.py:34-38,317-318,380`）；schema 保持开放集合符合增量 migration 约束。
- API `_call` 的 400/409 映射与 `theses.py` 一致；所有写 body 均配置 `extra="forbid"`，拼写错误走 422。
- 无鉴权写接口与 `app/main.py:3-5` 的 loopback、单用户、无鉴权设计一致，不列为问题。

## migration 与验收逐条对照

- migration 只包含 `CREATE TABLE/INDEX IF NOT EXISTS`，为增量新增；没有修改旧表或 prompt。
- 共创建 6 张要求内的表。`price_bars`、`benchmark_marks`、`corporate_actions` 均有非空 `valid_time/as_known_at`；日历、停牌区间和 benchmark identity 表不是事实版本表。
- `price_bars.security_id` 等外键为 `TEXT`，与 `migrations/0004_securities.sql:27-29` 的 `securities.id TEXT PRIMARY KEY` 匹配。
- `benchmarks`/`benchmark_marks` 与 `securities` 完全分离，满足验收 (b)。
- `trading_calendar.is_open` 可表达休市，`security_suspensions` 可表达闭区间及 `end_date NULL` 的在停状态，组合状态接口可区分休市/停牌，满足验收 (c)。
- 验收 (a) 的表结构、正常 PIT 查询和边界查询通过，但不可变版本契约被 must-fix #1/#2 破坏，因此整体不能判定通过。

## PATCH-NOTES-A7 可用性：可用

- 当前 `app/main.py:102-117` 的 import 块中，在 `mailbox` 与 `meta` 之间加入 `market_data as api_market_data`，与现有别名和排序风格兼容。
- 当前 `app/main.py:120-126` 的 `include_router` 元组可直接加入 `api_market_data.router`；放在 `api_theses.router` 后不会造成路由冲突。
- 当前 `app/main.py` 的并行改动只涉及恢复与 shutdown drain，未改变上述挂载位置，补丁无需调整。
- 补丁落地前，运行中的主应用不会暴露 `/api/market/*`；补丁本身可交由主代理应用。现有 A7 测试不会验证该挂载，建议按 nice-to-have 增加 smoke test。
