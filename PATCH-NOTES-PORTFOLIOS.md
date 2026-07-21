# PATCH-NOTES-PORTFOLIOS — Portfolios L1–L3 + Sunday proposer 分区外集成需求

本分区（ROADMAP Phase 5 "Portfolios L1–L3 + Sunday proposer" ☐ 项：
`migrations/0032_portfolios.sql`、`app/institute/portfolios.py`、
`app/api/portfolios.py`、`tests/test_portfolios.py`）已自包含落地并测试通过
（12 个测试全绿；`compileall` / `tests/test_db_migrate.py` /
`tests/test_paper_book.py` / `tests/test_forecasts.py` 均通过）。

集成前功能已可用：手动调用 `portfolios.sunday_proposer_job()` 即可产出提案，
域函数/裁决/估值均可直接使用；只是不会每周日自动跑、API 也还没挂到主 app 上。
以下两处改动落在其他代理/主代理的分区，按精确行给出。

## 1. 调度挂载（`app/institute/scheduler.py`，被并行 agent 占用，勿由本分区改）

job 定义追加到 `_paper_mtm_job` 之后（与 paper-opener / paper-mtm 并列。
**不加 `gated=True`**：proposer 是纯 DB 读写——从 forecasts + PIT 行情生成提案，
零模型调用，属于 ungated 类，maintenance 暂停期间照常运行，与 paper 系 job 同族）：

```python
@metered("portfolio-proposer")
async def _portfolio_proposer_job() -> None:
    from . import portfolios
    await portfolios.sunday_proposer_job()
```

`start()` 里追加挂载（放在 `cron(_paper_mtm_job, "paper-mtm", "00:00")` 之后；
字面量时间的先例是 hand-scorecard "00:05" / paper-mtm "00:00"，周几语法先例是
committee 的 `day_of_week="fri"`）：

```python
    cron(_portfolio_proposer_job, "portfolio-proposer", "22:00", day_of_week="sun")   # ROADMAP: Sun 22:00 proposer
```

说明：`sunday_proposer_job()` 天然幂等——每 (portfolio, work_date) 至多一份提案
（UNIQUE + `ON CONFLICT DO NOTHING`，INSERT 即仲裁），同日重触发只计 skip；
单个分析师失败不断链（never-raise 由 `@metered` 兜底，域内也逐分析师吞异常）。
不需要 `app/config.py` 改动（未新增配置项）。

## 2. API 挂载（`app/main.py`，主代理集成）

import 块（`from .api import (...)` 内，按字母序插在
`operator as api_operator,` 之后、`paper_book as api_paper_book,` 之前）：

```python
        portfolios as api_portfolios,
```

`include_router` 元组里（插在 `api_favorites.router` 之后、`api_mcp.router` 之前，
或任意位置——路由前缀互不冲突）：

```python
        api_portfolios.router,
```

路由前缀 `/api/portfolios`；`/api/portfolios/proposals*` 字面路径在模块内先于
`/{portfolio_id}` 注册，挂载顺序无需额外处理。

## 3. 分层语义（TIER_SPECS，代码常量，`app/institute/portfolios.py`）

每个非 ops 分析师三个虚拟组合（幂等创建，UNIQUE(analyst_id, tier)），初始现金
1,000,000（`DEFAULT_INITIAL_CASH`，同时是 NAV 分母与权重基数）。一条 forecast
只落一层——按 conviction 取其能通过下限的最高层；无 conviction 一律进 L3：

| 层 | 语义 | conviction 下限 | 仓位上限 | 单仓权重 |
|---|---|---|---|---|
| L1 | 高确信集中（best ideas） | ≥ 0.70 | 5 | 20% |
| L2 | 分散（中等确信） | ≥ 0.40 | 15 | 6% |
| L3 | 观察仓（其余全部 call，含无 conviction） | 无 | 30 | 2% |

## 4. 提案生成逻辑（周日 22:00 SGT，零模型调用）

候选 = 分析师**本人**的 open、未到期、long/short、有标的的 forecasts，归属走
0019 溯源链（forecast → extraction_items → extractions.analyst_id）；无归属的
forecast 不给任何人（fails closed，同 memory.py 姿态）。每组合一份提案：

- **平仓项**：持仓对应 forecast 已 settled / invalid / 到期 → 拟平仓
  （reason = `forecast_settled|forecast_invalid|forecast_expired`）；
- **开仓项**：该层候选中尚无该标的持仓者，按 made_at 顺序补到层上限
  （平仓项计入释放的 slot——裁决应用时先平后开，算术自洽）；
  当下无可用 PIT 价格的候选跳过并计数（下周日自然重试）；
- 空提案不落行（无操作员噪音）；`changes` 为 JSON 清单，`rationale` 为中文摘要。
- 生命周期：pending → approved / rejected（操作员裁决）；新提案日生成前，
  **所有更早 work_date 的 pending 统一翻 expired**（新一期 supersede 旧 pending，
  已裁决历史永不触碰）。

**裁决**（`decide_proposal`，API `POST /api/portfolios/proposals/{id}/decide`）：
pending→decided 是条件声明（rowcount 检查，丢声明 = 409），与仓位应用同一事务提交，
不可能双重应用。approve 逐条 best-effort 应用，每条在消费时重查活状态
（operator approve 门先例）：forecast 已 resolve / 标的重复持仓 / 超层上限 /
无可用价 / 现金不足 → 只跳过该条并把 outcome 记进 `applied` JSON。

**定价**：开/平两腿都取裁决当日"最新已知"的可用复权收盘
（`paper_book._latest_mark` → `market_data.get_bars_pit`，B6 正有限白名单）——
刻意不同于 paper book 的 made_at 冻结入场：paper book 度量 call 质量，
组合度量组合管理（成交发生在裁决时点）。记账对多空对称：
开仓 `cost = weight × initial_cash`，`cash -= cost`；平仓
`realized = signed_return × cost`，`cash += cost + realized`。

**估值**（`GET /api/portfolios/{id}/valuation`，即时计算不落表）：
`total = cash + Σ 可定价持仓 cost×(1+signed_return)`；无法定价的持仓
（标的被删/无可用 bar）**不计入且计 n_unpriced**（H3 姿态：未知不当零说）。
已知局限：标的被删的持仓无法诚实平仓（平仓需要价格，拒绝猜价），slot 挂起
直到有数据或操作员手工处理——与 paper book 手动平仓拒绝 unpriceable 同姿态。

事件：`portfolio.proposed`（ref=proposal id）、`portfolio.proposal_decided`
（payload 含 decision + outcome 计数）。vault 导出本轮未做（如需，另起
exporter handler 分区）。

## 5. Roadmap 卡片（`roadmap/backlog.json` 不在本分区文件清单内）

请主控补一张卡：Phase 5 "Portfolios L1–L3 + Sunday proposer"（本 PATCH-NOTES
即证据；ROADMAP.md 第 151 行的 ☐ 项可改 ☑ 并去掉 "skipped this build" 备注——
ROADMAP.md 同样不在本分区清单内，未动）。

## 6. 验收提示

- `.venv/bin/python -m pytest tests/test_portfolios.py -q` → 12 passed。
- 迁移编号 0032（0026–0031 已被并行分区占用）；纯增量，无
  BEGIN/COMMIT/ATTACH/VACUUM/PRAGMA（`test_db_migrate.py` 全绿验证过）。
- 集成后冒烟：`curl -s localhost:8100/api/portfolios` 应返回空表或组合清单；
  手动触发一轮 `python -c` 调 `sunday_proposer_job()` 或等周日 22:00 后查
  `/api/portfolios/proposals?status=pending`。
- scheduler 挂载后 `job_registry()` 会自动收录 `portfolio-proposer`
  （@metered 反射），cron 健康页无需额外接线；如需关停，feature switch
  约定键为 `job:portfolio-proposer`。
