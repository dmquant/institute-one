# PATCH-NOTES-B5 — Phase 1b（fetcher ladder + ${DATA_BUNDLE}）分区外改动清单

B5 交付物（已落盘，独占分区内）：

- `app/institute/market_fetchers.py`（新）— FMP→Stooq→Sina 数据源梯子、symbol 方言映射、confidence-gated 拒写 ingest（复用 A7 PIT 语义）、`refresh_security`/`refresh_all`、topic→securities 解析、`build_data_bundle`（≤4KB）
- `migrations/0014_shared_data.sql`（新）— `shared_data(topic, work_date)` upsert 表（0008–0013 留给并行卡，序号有空洞不影响 `db.migrate()`）
- `app/api/market_data.py` — router prefix 从 `/api/market` 改为 `/api`（现有 13 条路由的路径逐条补上 `/market` 段，**最终 URL 全部不变**）；新增 `GET /api/quote/{ticker}`、`GET /api/data/{topic}/latest`、`POST /api/market/refresh/{security_id}`。main.py 的挂载调用无需任何改动（同一个模块级 `router`）。
- `app/institute/workflows.py` — `_drive` 里唯一新增点：当某 step prompt 含 `${DATA_BUNDLE}` 且调用方未显式传值时，惰性调用 `market_fetchers.data_bundle_variable(variables)` 并把算出的值持久化回 `workflow_runs.variables`（可审计"prompt 实际看到了什么"）。数据缺失/异常渲染为空串，prompt 无痕降级，永不 raise。
- `tests/test_market_fetchers.py`（新，30 测试）+ `tests/test_market_data.py`（+1 shared_data schema 测试）。网络全 mock（httpx.MockTransport）；真实 Sina 冒烟测试默认 skip（`INSTITUTE_NET_TESTS=1` 打开）。

## 1. 需要主代理加的 config（app/config.py，B5 无权修改）

在 `Settings` 里加（建议放 "Vector search" 块之后）：

```python
    # Market data fetchers (Phase 1b). The ladder is FMP -> Stooq -> Sina;
    # Stooq/Sina are keyless, so fetching works with no key at all.
    fmp_api_key: str | None = None          # INSTITUTE_FMP_API_KEY
    fetch_proxy: str | None = None          # INSTITUTE_FETCH_PROXY, e.g. http://127.0.0.1:7897 (mihomo)
    market_fetch_enabled: bool = True       # INSTITUTE_MARKET_FETCH_ENABLED — kill switch for the hourly job
    market_refresh_minutes: int = 60        # hourly per ROADMAP; 禁用写 0 或负数（int 字段不能设 ""）
    market_refresh_limit: int = 20          # securities per sweep (stalest first)
```

**过渡期兼容（修正版，REVIEW-B5）**：`market_fetchers.py` 的 settings bridge 先 `getattr(settings, ...)` 再回退 `os.environ`。注意回退层读的是**进程环境变量**——pydantic 的 `env_file=".env"` 不会把未知字段写回 `os.environ`，所以 config.py 加字段**之前**，写在仓库 `.env` 里的 `INSTITUTE_FETCH_PROXY`/`INSTITUTE_FMP_API_KEY` 不生效，只有真实导出的环境变量生效；字段落地后 `.env` 才是常规入口。`tests/test_market_fetchers.py` 的 bridge/开关测试已写成两态兼容（字段存在时 monkeypatch settings，缺失时 monkeypatch env），config 落地前后都绿。

**代理说明**：所有 fetcher HTTP 都走 `httpx.AsyncClient(trust_env=False)`（本机全局 SOCKS 代理绝不能被继承）。`INSTITUTE_FETCH_PROXY` 有值时该 client 显式走它。这台机器上若直连 Stooq/Sina/FMP 不通，`.env` 加：

```
INSTITUTE_FETCH_PROXY=http://127.0.0.1:7897
```

注意 Sina（境内源）走代理可能反而变慢/被拒——建议先直连试 `curl -s 'https://hq.sinajs.cn/list=sh600519' -H 'Referer: https://finance.sina.com.cn'`，不通再开代理。

## 2. 需要主代理挂的 scheduler 任务（app/institute/scheduler.py，B1 分区）

job 定义（放在 `_janitor` 附近）：

```python
@metered("market-refresh")
async def _market_refresh_job() -> None:
    from . import market_fetchers
    await market_fetchers.refresh_all(limit=get_settings().market_refresh_limit)
```

`start()` 里注册：

```python
    every(_market_refresh_job, "market-refresh", minutes=settings.market_refresh_minutes)
```

**gated=False（maintenance-exempt）是有意的**：ROADMAP Phase 1b 明确 "hourly scheduler job (maintenance-exempt)"，且现有 gating 语义是"gated=True 仅用于提交新模型调用的 job"——行情抓取不花任何模型 quota，与 janitor 同类。落地时请顺带更新 scheduler.py 里 "Only the janitor stays ungated" 那行注释（改为 janitor + market-refresh），避免文档自相矛盾。
**no-op 语义**：`refresh_all` 在 `INSTITUTE_MARKET_FETCH_ENABLED=false` 时直接返回 `{"enabled": False}`；ROADMAP 原文 "job disabled when no keys" 是 FMP-only 时代的表述——现在 Stooq/Sina 免 key，无 key 依然可抓美股/A股/港股，所以改为总开关 + 每标的无可用源自动跳过（GLOBAL_CONTEXT 市场如 005930.KS 三个源都无方言，天然跳过；`listing_status != 'active'` 也不选）。每轮按 `MAX(price_bars.as_known_at)` 最旧优先选 `limit` 只，逐只 try/except 不互相影响。

## 3. ${DATA_BUNDLE} 的后续 prompt 卡建议（本卡不改 prompt 措辞）

`workflows/research.json` 现有 prompt 一字未动。变量能力已就位：任何 step prompt 里出现 `${DATA_BUNDLE}` 即自动注入（基于 `${TOPIC}` 匹配 securities：canonical id / 裸六位A股代码 / 中英文名 / security_aliases；渲染最新已知日线 + 近30天区间摘要 + 近5日收盘 + 基准对比 CSI300/HSI/SPX + 停牌标注，UTF-8 ≤4KB）。建议后续 prompt 卡：

- 在 `03-financials`（财务与估值）prompt 的开头加一段：`【本地行情数据】\n${DATA_BUNDLE}\n`，并把「请使用联网搜索…核实」改为「优先使用上方已注入的本地行情数据；缺失部分再联网搜索」——即 ROADMAP 说的 "replacing please web-search with grounded numbers"。`01-company` 可同样受益。
- 空数据时变量渲染为空串，段落只剩标题行也可接受；若想更干净，prompt 卡可以把标题并入变量本身（本卡的渲染已自带「【行情数据注入】…」头部，因此建议 prompt 里**只写 `${DATA_BUNDLE}` 裸变量**，别再套标题）。
- bundle 生成同时 upsert 进 `shared_data(topic, work_date)`，`GET /api/data/{topic}/latest` 可预览研究运行将注入什么。

## 4. shared_data 取舍说明（为什么建在 0014、为什么不是 PIT 表）

ROADMAP 1b 提 "`(topic, work_date)` upsert into `shared_data`"，0006 没建它。决定：**建，且按 upsert-in-place 语义**（`UNIQUE(topic, work_date)`），因为它存的是渲染后的投影文本——行情的 point-in-time 真相已经在 `price_bars`/`benchmark_marks`（不可变版本行）里，投影缓存无需再版本化；同日重渲染刷新该行即可。它是"该 topic 当日最新一次渲染"的缓存（`GET /api/data/{topic}/latest` 的后端）；**逐次运行的精确审计不在这里**——每个研究 run 实际注入的文本持久化在 `workflow_runs.variables["DATA_BUNDLE"]`（REVIEW-B5 措辞修正：同日重渲染会覆盖 shared_data 早先文本）。fetch 日志没有单独建表——refuse-to-write 拒写按要求走 log warning，`refresh_*` 的 stats 通过 API 返回值与 `market.refreshed` bus 事件可观测，避免一张一次性表。

## 5. 其他分区外事项

- `app/main.py`：**无需改动**（A7 时 router 已挂载；本卡只在同一 router 上加路由）。
- `pyproject.toml`：无新依赖（mock 用 httpx 自带的 MockTransport，没引 respx）。
- `roadmap/backlog.json`：Phase 1b 两个条目（fetcher ladder / research data injection）状态推进由主代理做。
- 后续 Phase 5（paper book）可直接复用：`fetch_quote`（MTM 现价）、`refresh_all`（收盘后批量）、`MARKET_BENCHMARKS` 常量（基准 id 约定 CSI300/HSI/SPX——注意基准 marks 本卡只读不抓，基准数据的 fetcher 是后续卡）。
- 已知边界（REVIEW-B5 低级项，本卡不修）：并发 refresh（scheduler 与手工 POST 同时打同一标的）可能各读旧快照后追加两条同 payload、不同微秒 `as_known_at` 的版本——顺序场景已有去重测试；若要绝对保证，后续卡加 per-security claim/lock。
