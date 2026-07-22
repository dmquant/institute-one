# PATCH-NOTES-LOOP-P1 — executor 锁顺序：hand 锁先于全局信号量（防跨 hand 饿死）

实现 `roadmap/loop-fix-backlog.md` 工作包 **P1**（高优）。接手 round-1 被叫停执行者在
`app/router/executor.py` / `tests/test_executor.py` 留下的半成品：`git diff` 审阅后判定方向正确、
实现完整，选择在其基础上验证补强（TDD 反证 + 测试健壮性修补 + 次级语义对齐），而非清理重写。

## 1. 问题

`app/router/executor.py` `_execute()`（改前约 274 行）：

```python
async with _sem(), _hand_lock(hand.name):   # 旧序：先全局槽，后 hand 锁
```

先占全局并发槽（`max_concurrent`，默认 3）再等 per-hand 互斥锁。hand A 一个长任务运行中
（占 1 槽 + A 锁），再来 2 个 A 任务各占 1 个全局槽干等 A 锁 → 3 个槽全被 A 系任务持有，
其中 2 个**什么都没在运行**。此时空闲 hand B 的新 submit/spawn 在信号量上排队，直到 A 的
积压排干——队头阻塞、跨 hand 饿死。全局槽本应只度量"正在运行的任务数"。

## 2. 修法（锁顺序前后）

```python
# 前（旧序，饿死源）
async with _sem(), _hand_lock(hand.name):
# 后（LOOP-P1）
async with _hand_lock(hand.name), _sem():
```

- 等 hand 锁的任务不再持有任何全局资源；拿到 hand 锁后才竞争全局槽 → 信号量只被
  "即将运行/正在运行"的任务持有，空闲 hand 永不被别的 hand 的积压挡住。
- **无 ABBA**：全仓只有 `_execute` 这一处同时获取两把锁（`_sem()` 仅 executor.py 一个调用点；
  `_hand_lock` 的另一获取处 `tests/test_ask_priority._busy` 只拿 hand 锁），顺序全局一致。
  信号量持有者已持有自己的 hand 锁、不再等任何锁，无循环等待。
- **cancel 协议不变**：queued 行仍先条件宣占翻终态（`UPDATE … WHERE id=? AND status='queued'`
  查 rowcount）再 `cancel()` 唤醒；停在任一锁 await 点的任务被唤醒后行已终态、无事可持久化
  （`tests/test_ask_priority.py` 的 cancel-wake 用例覆盖，28 通过）。
- **次级语义对齐**：`hand_busy()` 在新序下多了一个"持 hand 锁、等全局槽"的窗口返回 True——
  语义更准（该 hand 已认领下一个任务），`prepare_ask` 的空闲手改路因此更保守；docstring 已同步。
- 限流重试递归（`_execute` 尾部）发生在 `async with` 块退出之后，两把锁均已释放，无重入死锁。

## 3. 回归测试（TDD）

`tests/test_executor.py::test_backlog_on_busy_hand_does_not_starve_idle_hand`：

- `BlockingHand`("slowhand") 用事件占住 hand 锁模拟长 CLI 运行；再 spawn 2 个 slowhand 任务
  停在锁 await；此时 `submit("echo")` 必须在 4s 超时内完成（旧序下 3 槽被 slowhand 系占满，
  echo 卡死在 `semaphore.acquire`）。收尾释放事件排干积压，断言 3 个 slowhand 任务最终 completed。
- 对半成品的修补：原 `assert get_settings().max_concurrent == 3` 改为
  `monkeypatch.setattr(get_settings(), "max_concurrent", 3)` 钉死场景宽度——conftest 每测清空
  `executor._global_sem`，本测第一次 `_sem()` 用钉住值建信号量；避免操作者 `.env` 漂移误伤测试。
- **先失败后通过的证据**：临时把锁序还原为旧序单跑该测试 →
  `FAILED tests/test_executor.py::test_backlog_on_busy_hand_does_not_starve_idle_hand`
  （`1 failed in 4.21s`，TimeoutError：`submit` 栈停在 `_sem()` 的 `semaphore.acquire`）；
  恢复新序 → 通过。测试非恒真、确实暴露旧序问题。

## 4. 测试证据（实际输出行）

- `.venv/bin/python -m pytest tests/test_executor.py -q` → `13 passed in 1.25s`
- `.venv/bin/python -m pytest tests/test_executor.py tests/test_ask_priority.py -q` → `28 passed in 2.99s`
- `.venv/bin/python -m pytest tests/test_executor_shutdown.py tests/test_restart_recovery.py -q` → `14 passed in 4.72s`
- 全量第一次：`1 failed, 1050 passed, 2 skipped in 128.87s` —— 唯一失败
  `tests/test_research_tree.py::test_concurrent_settles_emit_exactly_one_snapshot`
  （"bound to a different event loop"）出自 research_tree.py owner agent 的在飞改动
  `_announce_lock`（该 agent 随后在 conftest 补了每测重绑）；与 executor 无关。
  单独跑 `tests/test_research_tree.py -q` → `39 passed in 3.39s`。
- 全量重跑：`.venv/bin/python -m pytest tests -q` → **`1051 passed, 2 skipped in 107.68s`** 全绿。
- `.venv/bin/python -m compileall app -q` → exit 0。

## 5. 边界与遗留

- 分区纪律：只改 `app/router/executor.py`、`tests/test_executor.py`、本文件。未动
  chain/factcheck/operator/research_tree/scheduler、prompts.py、workflows、migrations；
  未动 `roadmap/backlog.json` / `loop-fix-backlog.md` 勾选 / `loop-fix-state.json`
  （orchestrator 统一记账防并发写冲突）。未 git commit/push/restore。
- 同一未提交 diff 里 executor.py / test_executor.py 还含 NORTHSTAR-R1 轮的 per-hand
  queue-depth cap（`overcommitted`、migration 0028）工作，已有 `PATCH-NOTES-NORTHSTAR-R1.md`
  记档，非本包内容，保留未动。
- P1 附带项（research_tree `NODES_PER_TICK=3` 与 2 只 research hand 的搭配评估）按
  orchestrator 分工归 research_tree owner agent（state notes："C=research_tree.py(P5+P1附带…)")。
  P1 修后等 hand 锁不占全局槽，该项的风险已降为"注释说明即可"级。
