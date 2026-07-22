# PATCH — loop-fix P11d/e/f：paper book 低危修补三项

对应 `roadmap/loop-fix-backlog.md` P11 的 paper_book 三子项（d/e/f 合此一份）。只改 `app/institute/paper_book.py` + `tests/test_paper_book.py`。

## P11d opened 事件 ref_id 改 position id（消费方已逐一核对）

- `_insert_position` 返回值从 `"opened"/"lost_race"` 改为 **新 position id / None**（cap 拒绝仍 raise `RiskLimitConflict`，条件 INSERT 即仲裁语义不变）；两个调用点（`opener_tick`、`open_forecast_position`）随之把 pid 交给 `_emit_opened`。
- `_emit_opened` 现在 `bus.emit("paper_book.opened", "paper_position", <position_id>, payload)`：ref_id 从 forecast id 改为 position id——ref_kind 本来就写着 `paper_position`，且 `paper_book.closed` 一直以 position id 为 ref_id，本次是把 opened 对齐掉行内不一致。payload **新增 `position_id`、保留 `forecast_id`** 及原有全部字段。

**消费方核对结论（rg 全仓）**：
1. `bus.on(...)` 订阅面全仓 11 处注册，**无任何 handler 订阅 `paper_book.opened`**（exporter 只订 `paper_book.marked`；memory 只消费 `paper_book.closed`）。
2. `paper_book.closed` 的消费方 `memory._outcome_items` 用 `e.ref_id JOIN paper_positions p ON p.id = e.ref_id`——closed 的 ref_id 语义未动，本次不涉及。
3. SSE 前端 `frontend/src/events.tsx`：类型无关渲染，ref 仅展示为 `ref_kind:ref_id` 字符串——改后展示的是真实 position 行 id，语义更对，不破坏任何逻辑。
4. vault exporter：`_on_paper_book` 只挂 `paper_book.marked`（journal 投影），不读 opened。
5. MCP/插件：rg `opened` 无命中。
6. 既有测试面只有计数断言（`bus.replay(types=["paper_book.opened"]); len == 1`），不读 ref_id——不破坏。

结论：**没有依赖旧 ref_id 的消费方**；即便未来有离线消费者读历史事件，payload 的 `forecast_id` 从未离开，且旧事件行不回写（events 表 append-only），新旧可由 payload 是否带 `position_id` 区分。

- 回归：`test_opened_event_refs_position_id_with_compat_payload`——tick 与手动 open 两个发射点各发一枚，`ref_id` 能在 `paper_positions` 表解出真实行、`payload.position_id == ref_id`、`payload.forecast_id` 兼容保留、并显式断言 `ref_id != forecast_id`（防回退）。

## P11e benchmark base：首挂 ≠ 损坏

- 旧逻辑：`json.loads` 失败或 stored base 不可用 → 与"从未挂过"走同一分支 → **静默按今日水平重挂**——等于悄悄把整条 benchmark_nav 历史归一化基准重置。
- 新逻辑：
  - `admin_state` 行**不存在** = 首挂：挂 base 并返回 1.0（INSERT 改 `ON CONFLICT(key) DO NOTHING`，并发首挂保留先到者，不再覆盖）。
  - 行**存在但损坏/不可用**（坏 JSON、0/负/非有限）= 事故：`log.error`（含损坏值原文与操作指引）+ 返回 None（benchmark_nav 落 NULL，fails closed），**绝不覆盖**存量行。
  - 操作员有意重挂的通道 = DELETE 该 admin_state 行，下次 MTM 重新首挂。
- 回归：`test_benchmark_base_corruption_fails_closed_never_repins`——首挂 1.0；写坏 JSON 后 MTM → NULL + `log.error` 捕获 + 行原样未动；`{"value": 0.0}` 同路径；DELETE 行后下次 MTM 重新挂回 4000。

## P11f opened_at 循环内取时

- `opener_tick` 循环体内每次开仓前 `opened_at = bus.now_iso()`（硬边界要求的 bus 时钟，非裸 datetime.now），传给 `_insert_position` 作 opened_at/updated_at；tick 顶部的 `now` 只再用于候选查询的 expires_at 预过滤。长 sweep 不再把 tick 起点的同一时间戳抹到每一行——journal 的精确 SGT 日窗按 opened_at 匹配，跨 0 点长扫描时行归属日将更真实。
- 回归：`test_opened_at_is_taken_per_iteration`——monkeypatch `bus.now_iso` 每调用递增，两笔开仓落两个**不同** opened_at（修前：同一戳，测试红）。

## 验证（真实输出）

```
tests/test_paper_book.py                          25 passed in 3.32s
tests/test_paper_book.py tests/test_forecasts.py  50 passed in 8.48s
compileall app -q                                  COMPILE_OK
```

TDD 红→绿记录：四个新测试在修复前运行 `4 failed, 1 passed`（`test_opener_entry_single_row_read_keeps_pit_semantics` 是语义守护，修前即绿属预期），修复后全绿。git status（改动落盘证明）：

```
 M app/institute/market_data.py
 M app/institute/paper_book.py
 M tests/test_market_data.py
 M tests/test_paper_book.py
```

（market_data 两项属 P8c-b；本包三项只落 paper_book 两文件。roadmap 勾选留给 orchestrator。）

---

## R3 闭合：首挂冲突 loser 盲返 1.0 → INSERT rowcount 仲裁 + 重读归一化（REQUEST_CHANGES → 已修）

R3 复核指出 P11e 引入的问题：两个 `_benchmark_nav` 调用都可能先读到 base 不存在，一个 `ON CONFLICT DO NOTHING` 赢得首挂，另一个冲突后**仍无条件返回 1.0**——若两次调用对应不同 work date/mark（如 4000 与 5000），loser 的返回值没有按实际固定的 base 归一化，会把错误 benchmark NAV 写进自己的 nav_history 行。

**修法**（rowcount 即仲裁，与开仓 INSERT 同款惯用法）：

- 首挂 INSERT 检查 `db.execute` rowcount：**rowcount=1（真插入者）才返回 1.0**。
- rowcount=0（输掉首挂竞态）→ **重读** admin_state 行，落回共享的校验路径：赢家的 base 可用 → 返回 `value/base`（复现场景 4000/5000=0.8）；重读到的行损坏/不可用 → 沿用 P11e 的 fail-closed（log.error + None，不覆盖）；重读竟然又不存在（行在窗口内被删）→ None fail closed，下次 MTM 重挂。

**回归测试**：`test_benchmark_first_pin_race_loser_normalizes_to_winner`——monkeypatch 让两次调用的首读都看到"无 base"（竞态窗口的确定性化，与 seal 测试同一 stale-read 手法）：wd=2026-01-02/5000 先到赢得首挂返回 1.0，wd=2026-01-01/4000 输掉后必须返回 **0.8**（修前红：盲返 1.0）；最终存储 base=5000；稳态复算与 loser 答案一致。

**R3 验证（真实输出）**：修前两条 R3 复现 `2 failed`（rotation + race_loser 各自在预言的断言处失败）；修后 `tests/test_paper_book.py` 27 passed、三文件联跑 **71 passed in 6.58s**、compileall COMPILE_OK。git status（本轮实际落盘）：`M app/institute/paper_book.py`、`M tests/test_paper_book.py`（market_data 两文件为上轮已过审改动，本轮未触碰）。
