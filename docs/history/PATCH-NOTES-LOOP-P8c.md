# PATCH — loop-fix P8c：paper book 事件循环阻塞（opener 无界扫描 + PIT 全历史读）

对应 `roadmap/loop-fix-backlog.md` P8c：`opener_tick` 候选查询无 LIMIT + `get_bars_pit` 全历史扫描。原行号 ~270-279 / ~165-172（工作区已漂移，实际以 rg 定位为准）。

**改动文件**：`app/institute/paper_book.py`、`app/institute/market_data.py`（additive 新函数，经 git status 确认改动前该文件干净无他人占用）、`tests/test_paper_book.py`、`tests/test_market_data.py`。

## a) 候选查询加 LIMIT

- 新模块常量 `OPENER_BATCH_LIMIT = 50`；`opener_tick` 候选查询尾部 `ORDER BY f.made_at, f.id LIMIT ?`（参数绑定，运行时读常量——测试可 monkeypatch）。
- 语义：一个 tick 最多考虑 50 个候选，最老 made_at 优先；5 分钟 tick 逐批消化积压，而不是一次把全表扫上事件循环。已开仓/到期/backfill 候选被 WHERE 排除，批次自然向后推进。
- 已知取舍：若积压前 50 名长期不可定价（无 bar），其后候选会等它们到期出队——deterministic 最老优先是有意选择（与原顺序一致），到期过滤器保证不会永久饿死。

## b) PIT 读改单行专用读

- 调用点传 `start` 下限不可行：entry 腿的反前视回退（made 日收盘迟发时回退到**更早**的上一根 bar）没有可静态确定的下限，任何 start 猜测都会把回退截断成 fails-closed 误报。故走清单允许的第二方案：
- `market_data.get_last_bar_pit(security_id, as_of=None, *, freq='1d', end=None)` —— **additive 新函数**，不改 `get_bars_pit` 任何既有签名/语义。定义即 `get_bars_pit(...)[-1] or None`：同一个 per-bar_date `MAX(as_known_at) <= as_of` 相关子查询，外层 `ORDER BY bar_date DESC LIMIT 1`（每个 bar_date 恰一行满足版本等式，DESC LIMIT 1 == ASC 尾行）。反前视回退语义完整保留：新 bar 的所有版本都晚于 as_of 时整根不存在，自动落到更早 bar_date。
- `paper_book._entry_bar` / `_latest_mark` 改用它——这两个 helper 只用 `bars[-1]`，是 opener tick、MTM、manual close、portfolios（`paper_book._latest_mark` 复用方）的共同热路径。fails-closed 姿态不变：最新一根不可用（0/负/非有限）→ None，绝不回退取更旧的价。
- `reconcile` 的逐 bar 历史重放（`_reconcile_open_decision`/`_historical_trigger_mark`）天然需要整窗，不在 P8c 范围，保持 `get_bars_pit`。

## 回归测试（先红后绿，TDD）

| 测试 | 修复前 | 断言 |
|---|---|---|
| `test_opener_candidate_batch_is_bounded` | 红（常量不存在） | limit=2 时 considered==2；候选可定价后连续 tick 2+1 逐批开满 3（有界且不丢） |
| `test_opener_entry_single_row_read_keeps_pit_semantics` | 绿（语义守护，防优化漂移） | 迟发收盘回退到 05-29 上一根；最新 bar 为 0 时 fails closed 不回退 |
| `test_get_last_bar_pit_is_the_full_scan_tail_without_the_scan`（test_market_data.py） | 红（函数不存在） | 5 个组合（最新知识/历史 as_of/反前视回退/end 裁剪/一无所知）逐一与 `get_bars_pit(...)[-1]` 等价；无 bar → None；坏 freq 报 MarketDataError |

## 验证（真实输出）

```
tests/test_paper_book.py .........................  25 passed in 3.32s
tests/test_market_data.py ....................      20 passed in 1.91s
tests/test_paper_book.py tests/test_forecasts.py    50 passed in 8.48s
compileall app -q                                    COMPILE_OK
```

下游抽查：`test_portfolios.py + test_exporter_handlers.py + test_cron_metrics.py` → 42 passed（`_latest_mark` 复用方 portfolios 未受影响）。

---

## R3 闭合：固定头部 LIMIT 造成饿死 → keyset cursor 轮转（REQUEST_CHANGES → 已修）

R3 复核指出：`LIMIT 50` 恒从 `(made_at, id)` 最前端取——最老 50 条若长期不可定价（无 bar），每个 tick 重选同一批，其后可定价的 forecast **永不被考虑**，短 horizon 的会未开仓即过期。复现测试（修前红）：batch=1，先建无 bar 老单再建有 bar 新单，第二个 tick 仍在重选 blocker、新单永不开仓。

**修法**：持久化 keyset cursor 轮转（选 admin_state 方案，零新迁移）：

- 新 admin_state key `paper_book:opener_cursor`，存上一 tick **最后一个被考虑**的候选 `{"made_at", "id"}`；`_opener_cursor()` 读取，缺行/坏 JSON 按 max_positions 惯例降级为"从头开始"（纯调度状态，最坏重走一轮）。
- 候选取批改两段互补 keyset 切片：先取严格在 cursor 之后的 `(made_at > c OR (made_at = c AND id > cid))` LIMIT N；不足则**回绕**取头部 `(made_at < c OR (made_at = c AND id <= cid))` 补足——两段恰好划分全键集，一批绝不重复见同一行。
- tick 末尾把 cursor 前移到 `candidates[considered-1]`（cap 提前 break 时未考虑的剩余排在回绕后第一顺位）；considered==0 不写。cursor 写入是 last-writer-wins 的调度状态（开仓本身仍由条件 INSERT + 唯一索引仲裁，rowcount 语义未动）。
- 语义：永久 blocker 只占自己那一片批次；被跳过的行下一轮转必被重新考虑（有界公平轮询，无饿死）。

**回归测试**：`test_opener_rotates_past_permanently_unpriceable_head`——tick1 只见 blocker（skip）、tick2 必须开出后面的可定价新单（修前在此失败）、tick3 回绕重见 blocker（不永久跳过）、blocker 数据迟到后一轮内被开出。

**R3 验证（真实输出）**：`tests/test_paper_book.py` 27 passed；`tests/test_paper_book.py tests/test_forecasts.py tests/test_db_migrate.py` → **71 passed in 6.58s**；`compileall` COMPILE_OK。本轮只改 `paper_book.py` + `tests/test_paper_book.py`（market_data 两文件维持已过审的 P8c 状态未再动）。
