# REVIEW-B5 — Phase 1b fetcher ladder + 研究数据注入独立审查

## 结论：FAIL

方言映射、API 兼容、惰性注入、增量 migration 和定向测试的正常路径均成立；但 confidence gate 会把 `NaN`/`Infinity` 当作合法价格，daily ladder 也会把“有日期但全部不可信”的首源结果当成成功而不降级。这两点直接破坏本卡的核心契约“confidence-gated refuse-to-write + FMP→Stooq→Sina ladder”，应在合入前修复。另有 `adj_factor` 未纳入 unchanged 判定、`.env` 过渡说明不成立等集成问题。

## 审查范围与可归因性

- 已全文审阅 `app/institute/market_fetchers.py`、`app/institute/market_data.py`、`app/api/market_data.py`、`app/institute/workflows.py`、`migrations/0014_shared_data.sql`、`tests/test_market_fetchers.py`、`tests/test_market_data.py`、`PATCH-NOTES-B5.md`。
- 已执行指定 Git 检查。`app/institute/market_data.py` 与 `app/api/market_data.py` 仍是未跟踪文件，因此 `git diff -- <file>` 无法分离 A7 基线与 B5 增量；`app/institute/workflows.py` 的 HEAD diff 还含其他代理的 analyst-key 归一化改动，已按要求忽略。
- `workflows/research.json` 在当前工作树并非零字节变化：有一处 `analyst`→`analyst_id`（`1 insertion, 1 deletion`），但其所有 prompt 字符串逐字未变；该变化不归因于 B5。

## 问题分级

### [高 / must-fix] 非有限数可绕过 confidence gate

- 位置：`app/institute/market_fetchers.py:194-199, 403-442`。
- `_f()` 接受 Python 可解析的 `NaN`/`Infinity`；后续 `v <= 0`、OHLC 比较和涨幅比较对 `NaN` 均为 false，因此 `check_bar()`/`check_quote()` 返回空问题列表。只读探针实测 `nan_bar_problems == []`、`nan_quote_problems == []`。
- 后果：首根 `Infinity` bar 可进入 PIT；`NaN` 可能在 SQLite 绑定时变成 `NULL` 并从“拒写统计”退化为异常；无 `prev_close` 的非有限 quote 还能走到 API 序列化并产生 500。
- 同一区域的 `check_quote()` 也未校验返回 payload 中已有的 open/high/low/volume 是否有限、为正及 OHLC 一致，和文件头声明的 quote sanity 契约不一致。
- 建议：在 `_f()` 或 gate 中统一要求 `math.isfinite()`；quote 对存在的 OHLC/volume 应执行与 bar 一致的结构校验，并补 `NaN`、`±Infinity` 回归测试。

### [中 / must-fix] daily ladder 在 confidence 之前错误认定首源成功

- 位置：`app/institute/market_fetchers.py:485-501`。
- `fetch_daily_bars()` 只要求首源结果存在 truthy `bar_date` 就立即返回；缺 OHLC、非有限 OHLC、结构矛盾或全体超阈值的结果均不会继续尝试下一源，随后才在 `refresh_security()` 被拒绝。
- 只读 MockTransport 探针中，FMP 返回一根日期合法但 OHLC 全为 `NaN` 的 bar，Stooq 同时有正常 bar；实际结果仍选中 `source=fmp, finite=False`。
- 这不会保证落脏数据（修复上一个问题后会拒写），但会让本应可用的 Stooq/Sina 降级路径失效。建议仅在首源至少含一根通过基本 gate 的 bar 时结束 ladder；全体不可信时继续下一源。

### [中] unchanged 判定不是完整写入 payload 的逐字段相同

- 位置：`app/institute/market_fetchers.py:506-510, 549-557`；A7 字段定义见 `app/institute/market_data.py:358-375`。
- OHLC 与 `volume` 已比较；`adj_factor` 未比较，且三个 parser 均未产出它。实测已知 `adj_factor=2.0`、传入 `adj_factor=1.0` 时 `_same_bar()` 仍返回 true。`source` 也被忽略。
- `tests/test_market_fetchers.py:328-341` 的重复 refresh 测试是真实的顺序重放测试，但 correction 只改 `close`，没有覆盖 volume-only 或 adj-factor-only 修正，不能证明“逐字段相同”。
- 应先明确 fetcher 写入的是 raw bar（固定 `adj_factor=1.0`）还是要消费供应商调整数据，再用规范化后的完整事实 payload 比较；至少补 volume-only 与 adj-factor 边界测试。

### [中] PATCH-NOTES 所述 `.env` 过渡兼容不成立

- 位置：`app/institute/market_fetchers.py:76-94`、`app/config.py:18-20`、`PATCH-NOTES-B5.md:25-31`。
- 在 Settings 尚无 B5 字段时，fallback 只读真实进程 `os.environ`。Pydantic 的 `env_file=".env"` 会忽略未知字段，但不会把它们写回 `os.environ`，所以此阶段把 `INSTITUTE_FETCH_PROXY`/FMP key 写进仓库 `.env` 不会生效。
- 当前 `app/config.py` 尚无建议的 5 个字段，`app/institute/scheduler.py` 也尚未挂 market job；必须应用 PATCH-NOTES 后才完整集成。
- 加字段后还需调整 `test_settings_bridge_reads_env`：fixture 已缓存 Settings，测试中途 monkeypatch bool env 时，`settings.market_fetch_enabled=True` 会优先于新的进程 env，当前断言将失效；若真实 `.env` 已有 key/proxy，首个“应为 None”断言也不再隔离。

### [低] 顺序去重成立，但并发 refresh 仍可堆相同版本

- 位置：`app/institute/market_fetchers.py:533-558`、`app/institute/market_data.py:99-105, 379-407`。
- 顺序重复 refresh 会先读到最新值并跳过，现有测试确实验证了这一点。
- 但 scheduler 与手工 POST 并发时，两次调用可同时读到旧快照，再各用不同微秒 `as_known_at` 追加同一 payload；没有 per-security lock/事务来保证“不堆版本”。
- 微秒墙钟使顺序碰撞概率很低，但不是绝对唯一保证；若同一微秒出现不同 payload，A7 的 `TransitionConflict` 路径仍可能触发。因而“绝不触发/绝不堆积”只能成立于当前顺序测试场景。

### [低] `shared_data` 是同日最新投影，不是完整审计轨迹

- 位置：`migrations/0014_shared_data.sql:5-13`、`app/institute/market_fetchers.py:782-790`。
- `(topic, work_date)` 覆盖式 upsert 作为投影缓存是合理的，也没有被当成价格真相消费；价格真相仍只来自 PIT 表。
- 但同日重渲染会覆盖早先文本，因此 migration/PATCH-NOTES 中“审计研究看到了什么”的措辞过强；真正的逐运行审计是 `workflow_runs.variables["DATA_BUNDLE"]`。

## 方言映射逐格核验

| canonical | FMP | Stooq | Sina | 代码/单测结论 |
|---|---|---|---|---|
| `600519.SH` | `600519.SS` | `None` | `sh600519` | 通过；`.SH`→FMP `.SS` 已覆盖 |
| `BRK.B.US` | `BRK-B` | `brk-b.us` | `gb_brk$b` | 通过；share-class 三种方言均覆盖 |
| `0700.HK` | `0700.HK`（4 位） | `0700.hk`（4 位） | `hk00700`（5 位） | 通过；补位规则与测试一致 |
| `005930.KS` / 非 canonical | `None` | `None` | `None` | 通过；`_available_sources()` 出列，quote 返回 `None`、bars 返回 `[]` |

对应实现为 `app/institute/market_fetchers.py:103-176, 447-456`，单测为 `tests/test_market_fetchers.py:99-142`。

## 十项逐条裁决

1. **文件与 diff：有保留。** 新文件均已全文读；两份 A7/B5 共用文件因未跟踪而无法用 Git 分离增量，workflows HEAD diff 含其他代理改动。
2. **方言映射：通过。** 三个指定 ticker、HK 4/5 位补零及无映射出列路径均与代码和单测一致。
3. **confidence 拒写：不通过。** 有限正常值下拒写发生在 `upsert_bar()` 前且 `stats.rejected` 每行 +1；恰好 50% 使用 `>=`、会拒绝，但 bar 单测注释所称“恰好 50%”实际不是精确边界；`NaN/Infinity` 漏检为阻断项。UTC `today+1` 可覆盖 UTC+8 交易所在 UTC 16:00 后出现“明日”本地日期，但对 US 和 UTC 日内大部分时间偏宽松，会容忍真正的下一日脏日期。
4. **ingest / A7：不通过。** volume 已比较、adj_factor 未比较；顺序重复 refresh 不堆版本，微秒时钟降低但不消灭同键冲突，并发场景仍可堆相同版本。
5. **workflow 注入：通过（带 Git 归因保留）。** B5 注入块位于 `app/institute/workflows.py:203-218`，只在 prompt 引用且未显式传值时求值并回写 variables；缺数据空串、显式值优先和不引用不计算均有测试。当前文件的其他 diff 不属于该注入点；research prompt 文本未改。
6. **API：通过。** prefix 改写后 8 个旧 path template/13 个旧 method URL 保持 `/api/market/*`，新增 3 个 method；生产 OpenAPI 可见这些路由。仓库 `rg` 未发现 SPA/插件消费者。未知证券 404、现有证券但所有源失败/不可信 quote 502；路径输入经参数化查询，domain/body 校验沿用 A7。
7. **httpx / proxy：部分通过。** 生产 fetcher 只有 `_client()` 一个 AsyncClient 创建点且明确 `trust_env=False`，显式 proxy 会覆盖直连；`.env` 过渡读取和未来 settings-cache 测试问题见上。
8. **migration 0014：通过（措辞 nit）。** 仅 `CREATE TABLE/INDEX IF NOT EXISTS`，迁移账本按文件名逐项记账，编号空洞安全；同 topic+work_date 覆盖符合投影缓存语义，应用内也只有 market_fetchers 读写它。
9. **硬规则：通过。** B5 migration 只增；持久化 created/updated 使用 `bus.now_iso()`，A7 的微秒 `as_known_at` 例外沿用既定 PIT 契约；未发现新增模型调用；research prompt 字符串逐字未改。
10. **指定验证：通过。** `compileall app -q` exit 0；定向 pytest 为 `49 passed, 1 skipped in 1.51s`，未运行全量。

## PATCH-NOTES-B5 裁决

- **config 5 字段：方向正确但必须先落地。** 名称与调用点匹配；interval 禁用值应写 `0`/负数，不能把空字符串传给 int 字段。需同步修正 settings bridge 测试与上文 `.env` 过渡说明。
- **scheduler `gated=False`：同意。** ROADMAP 明确 `maintenance-exempt`，该 job 不提交模型调用，并已有 `market_fetch_enabled` kill switch；应保持 `@metered("market-refresh")` 默认 ungated。落地时顺带更新 scheduler 当前“Only the janitor stays ungated”注释，避免文档自相矛盾。
- **无 FMP key 仍运行：同意。** Stooq/Sina 无 key，继续运行比照搬旧的“no keys disabled”更符合三层 ladder；总开关负责显式停用。
- **prompt 卡建议：同意裸 `${DATA_BUNDLE}`。** 当前 research workflow 没有该变量，所以注入能力尚未实际用于研究；后续 prompt 卡仍是完成 ROADMAP “replace web-search with grounded numbers”的必要步骤。
- **shared_data：同意作为可覆盖投影缓存。** 不应将其描述成完整 per-run 审计；per-run 真值在 workflow variables。
