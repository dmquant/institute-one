# PATCH-NOTES-B6 — 卡 M3-001（thesis-aware research queue）+ M5-001（forecast ledger）分区外事项

B6 交付物（已落盘，独占分区内）：

- `migrations/0012_research_thesis.sql` — research_queue 加 thesis_id/security_id/question/output_type/priority_reason/dedup_key（全部可空，旧行不受影响）；research_log 加 dedup_key（结构化冷却轨）
- `migrations/0013_forecasts.sql` — forecasts + forecast_settlements
- `app/institute/research.py` — enqueue 向后兼容扩展（结构化入队）、双轨去重/冷却、`seed_from_theses()`、结构化任务的 `${TOPIC}` 上下文注入（A1 的孤儿恢复与 work_date cap 语义未动）
- `app/institute/forecasts.py`（新）— create/settle（fails closed）/list/get
- `app/api/research.py` — EnqueueBody 增结构化字段 + `POST /api/research/seed-from-theses`
- `app/api/forecasts.py`（新）— `POST/GET /api/forecasts`、`GET /api/forecasts/{id}`、`POST /api/forecasts/{id}/settle`
- `tests/test_research.py`（6→15）、`tests/test_forecasts.py`（新，12 个）

## 1. 需要主代理执行的挂载（app/main.py，B6 无权修改）

`create_app()` 里的 `from .api import (...)` 块加一行（按字母序放在 `events` 与 `hands` 之间）：

```python
        forecasts as api_forecasts,
```

`include_router` 循环的元组里加 `api_forecasts.router`（建议放在 `api_market_data.router` 之后）：

```python
    for r in (
        api_meta.router, api_tasks.router, api_hands.router, api_events.router,
        api_analysts.router, api_sessions.router, api_workflows.router,
        api_whiteboard.router, api_mailbox.router, api_research.router,
        api_roadmap.router, api_theses.router, api_market_data.router,
        api_forecasts.router, api_archive.router, api_vault.router, api_mcp.router,
    ):
        app.include_router(r)
```

挂载后复测：`.venv/bin/python -m pytest tests/test_forecasts.py tests/test_research.py -q`（两份测试都不经 main.py——forecasts API 测试用裸 FastAPI app 挂本 router——挂载前后都应通过；挂载只影响运行中的服务器暴露 `/api/forecasts/*`）。`/api/research/*` 的新端点（seed-from-theses）随已挂载的 research router 自动生效，无需动作。

## 2. prompts.py / workflows json：本卡零增补（说明，非动作）

M3-001 的「研究 prompt 注入 thesis/security 上下文」实现在 research.py 自有的
变量装配层（`_run_item` → `_topic_with_context()`）：结构化任务把上下文拼进
`${TOPIC}` 变量的**值**（`主题【论点上下文】所属论点…；聚焦标的…；核心研究问题：…`），
`workflows/research.json` 的 7 段 prompt 模板逐字未动，`prompts.py`（B3 分区）逐字未动。
topic-only 任务（含全部 0012 前旧行）的 `${TOPIC}` 值 byte-identical 于旧行为。
若未来想让模板显式感知结构化字段（比如单列一个 `${THESIS_CONTEXT}` 变量），那是
workflows json 的增补，归 B3/主代理决策——当前卡不需要。

## 3. scheduler / config：本卡无需任何新设置

- `seed_from_theses()` 只经 API/手动触发，**未挂**调度 job（theses 表本机尚空，
  market-thesis-data 数据集缺失；等 M1-003 import 落库后由主代理决定是否加
  `metered(gated=True)` 周期播种 job）。
- forecast settle 同理只经 API 触发；到期批量结算 job 是后续卡（M5-002+）的事。
- `app/config.py` / `.env`：无新键。

## 4. roadmap/backlog.json

M3-001、M5-001 状态迁移（inbox → 完成态）由主代理按状态机推进，B6 未动。

## 5. 后续卡接口约定

- **research 去重双轨**：`research_queue.dedup_key` / `research_log.dedup_key`
  为 NULL = topic 旧轨（对全部 0012 前可达状态语义等价；结构化行出现后刻意
  互不匹配），非 NULL = 结构化轨
  （`research.structured_dedup_key(thesis_id, security_id, question)`，
  question 规范化 = NFKC + casefold + 空白折叠）。两轨完全独立：同 topic 字符串
  的结构化 pending/完成不会吞掉/冷却 topic-only 请求，反之亦然。直接 INSERT
  research_log 的旁路写入方（目前只有 research.py 自己）必须带上正确的
  dedup_key，否则结构化冷却失效。
- **结构化轨并发仲裁**（R-B6 修复）：`idx_research_queue_dedup_active` 部分唯一
  索引（`dedup_key IS NOT NULL AND status IN ('pending','running')`）让 INSERT
  本身成为仲裁者——并发同三元组 enqueue 的输家撞约束后重读赢家返回 deduped。
  绕过 `research.enqueue()` 直接 INSERT 结构化行的写入方必须自行处理该
  IntegrityError。completed/failed/cancelled 行离开索引，三元组可再研究。
- **结构化字段上限**：question ≤ `research.MAX_QUESTION_LEN`（500）、
  output_type/priority_reason ≤ `MAX_ANNOTATION_LEN`（200），超限显式报错
  （不静默截断——截断会背着调用方改变去重三元组）；`${TOPIC}` 上下文后缀
  硬顶 `_CTX_SUFFIX_CAP`（700 字符，注入名称切片 80）。seed cap ∈ [0,
  `MAX_SEED_CAP`=100]，**cap=0 = 干跑**（只统计 matched 不入队），负数/超限拒绝。
- **forecast settlement**：`settlement_rule` 只认
  `{"type":"absolute_move","threshold":有限>0}` 与
  `{"type":"price_vs_benchmark","threshold":有限>0,"benchmark_id":...}`（create
  校验 isfinite 并规整为 canonical JSON，`allow_nan=False` 兜底）。
  **知识时点语义（R-B6 修复后的终稿）**：entry = made_at 日历日当日或之前最后一
  根、且 **as known at made_at**（PIT `as_of=made_at`，杜绝前视——made_at 之后
  发布/修正的 entry 日数据永不改写基准）；exit = expires_at 日历日当日或之前
  最后一根、按**结算时点知识**（`as_of=None` 最新，显式 `as_of` 回放，事后修正
  的 exit 数据合法计入）。bars 用 `close*adj_factor` 复权，基准用 marks value；
  entry/exit 及计算结果全走**正有限白名单**（0/负/NaN/Inf → invalid）。过期前
  拒绝结算；任何输入缺失/不可用 → verdict='invalid'，status='invalid'，绝不猜。
  状态翻转是 status='open' 条件认领，与 settlement 行同事务提交，
  UNIQUE(forecast_id) 兜底防双结算。benchmark_id 在 create 时**不**要求已存在
  （marks 可后补），settle 时缺失 fails closed。非法 `as_of` 在 domain 层规整并
  映射 400（复用 `market_data._norm_ts`，与 A7 单一时间形状一致）。
- 事件：`forecast.created` / `forecast.settled`（vault 导出 handler 留给后续卡）。

## 6. R-B6 复审修复（REVIEW-B6.md → 全部落地）

1. **[高] entry 前视偏差**：entry/exit 两腿分开读 PIT——entry 腿
   `get_bars_pit/get_marks_pit(…, as_of=fc["made_at"], end=made_date)` 冻结在
   预测时知识，exit 腿按结算 `as_of`；审查者探针场景（made 后修正 entry 日
   10→20）现判 hit +10%，回归测试 4 个（security/benchmark 修正版、close 迟发
   回退前一根、exit 腿合法用当前知识 + as_of 回放）。
2. **[高] fails-closed 漏网**：`_usable_price()` 正有限白名单管 entry+exit 两端
   （0/负/Inf 域内可写、NaN 由 SQLite 绑定为 NULL 域内不可达但单元测试兜底），
   计算结果再验 isfinite；审查者 zero_exit 探针现判 invalid。逐场景测试 6 端点
   组合 + 基准零 exit + 单元级 NaN/溢出。
3. **[高] 非有限 threshold**：`parse_settlement_rule` 拒 bool + isfinite；
   conviction/horizon_days 同步补 isfinite/bool 口径；`_dumps(allow_nan=False)`
   兜底 canonical JSON 声明。
4. **[中] 并发去重**：0012 追加部分唯一索引（见第 5 节），enqueue 的 INSERT 撞
   约束后重读赢家（A2「数据库仲裁」精神）；5 路 gather enqueue / 双路并发 seed
   回归测试。
5. **[中] cap=0 / API 边界**：cap=0 = 干跑不入队、负数与 >100 拒绝；SeedBody
   `extra="forbid"` + `ge=0, le=100`；EnqueueBody 结构化字段 max_length 前置。
6. **[中] 上下文长度**：`_topic_with_context` 名称切片 80 + 后缀硬顶 700（理由
   见 research.py 常量注释：${TOPIC} 在 7 步模板 ~20 处复现，700×20≈14KB 封顶）；
   enqueue 层字段超限显式报错。
7. **[低] 0012 注释矛盾**：改为「对全部迁移前/NULL 轨行语义等价，结构化行出现后
   刻意独立」。
8. **[低] 非法 settle as_of → 400**：domain 入口规整，MarketDataError 不再以
   500 逃逸。
9. 审查提到的 `create_app()` forecast 路由 smoke test：挂载本身在主代理分区
   （第 1 节），建议主代理落补丁时在 tests/test_forecasts.py 或集成测试里加一条
   `create_app()` 路由存在性断言。
