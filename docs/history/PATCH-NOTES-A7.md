# PATCH-NOTES-A7 — 卡 M4-001（market data PIT store）分区外改动清单

A7 交付物（已落盘，独占分区内）：

- `migrations/0006_market_data.sql` — trading_calendar / security_suspensions / price_bars / benchmarks / benchmark_marks / corporate_actions（0005 编号留给并行卡；`db.migrate()` 按文件名排序、逐文件记账，序号有空洞不影响）
- `app/institute/market_data.py` — 域模块（日历/停牌/PIT bars/基准 marks）
- `app/api/market_data.py` — REST 路由（`/api/market/*`，模块级 `router`，风格同 `app/api/theses.py`）
- `tests/test_market_data.py` — 三条验收全覆盖；API 测试用裸 FastAPI app 挂本 router，不依赖 main.py

## 需要主代理执行的挂载（app/main.py，A7 无权修改）

`create_app()` 里的 `from .api import (...)` 块加一行（按字母序放在 `mailbox` 与 `meta` 之间）：

```python
    from .api import (
        analysts as api_analysts,
        archive as api_archive,
        events as api_events,
        hands as api_hands,
        mailbox as api_mailbox,
        market_data as api_market_data,
        meta as api_meta,
        research as api_research,
        roadmap as api_roadmap,
        sessions as api_sessions,
        tasks as api_tasks,
        theses as api_theses,
        vault as api_vault,
        whiteboard as api_whiteboard,
        workflows as api_workflows,
    )
```

`include_router` 循环的元组里加 `api_market_data.router`（建议放在 `api_theses.router` 之后）：

```python
    for r in (
        api_meta.router, api_tasks.router, api_hands.router, api_events.router,
        api_analysts.router, api_sessions.router, api_workflows.router,
        api_whiteboard.router, api_mailbox.router, api_research.router,
        api_roadmap.router, api_theses.router, api_market_data.router,
        api_archive.router, api_vault.router, api_mcp.router,
    ):
        app.include_router(r)
```

挂载后复测：`.venv/bin/python -m pytest tests/test_market_data.py -q`（本卡测试不经 main.py，挂载前后都应通过；挂载只影响运行中的服务器暴露 `/api/market/*`）。

## 其他分区外事项

- `app/config.py` / `.env`：本卡无需任何新设置。
- `roadmap/backlog.json`：M4-001 状态迁移（inbox → 完成态）由主代理按状态机推进，A7 未动。
- REVIEW-A7 两条 must-fix 已修复（版本行不可变 + 亚秒精度版本键），详见审查修复报告；`tests/test_market_data.py` 现为 18 个测试。
- 后续卡接口约定：**PIT 版本行不可变**——`upsert_bar`/`upsert_benchmark_mark` 对同版本键仅接受逐字段一致的幂等重放（no-op 返回已有行），同键不同数据抛 `TransitionConflict`（HTTP 409），修正必须用更晚的 `as_known_at` 追加新版本。`as_known_at` 省略时默认微秒级 UTC 时钟（`market_data._now_known_iso()`，同秒两次修正不撞键）；回填修正流传显式值，任意 ISO-8601 输入会被规整为微秒精度 `+00:00` 单一形状后存储。`created_at`/`updated_at` 仍用 `bus.now_iso()`。`corporate_actions` 表已建好但域函数（split/dividend ingest）留给 fetcher 卡，届时必须复用 `_norm_ts`/`_now_known_iso` 与同一不可变追加语义。
