# PATCH-NOTES-A1 — 分区外集成需求

给主代理：A1 分区（Phase 0 hardening 五项 + R1 复审修复）本身无需改 `app/config.py`。
唯一的分区外需求是给 **A4 分区的 `app/institute/scheduler.py`** 提一个可选的公共访问器（见下）。

## 1. scheduler 停机窗口：现状方案（已在 A1 分区内实现）

R1-M1 指出 APScheduler `shutdown(wait=False)`（3.11.x）只 cancel 内部
`AsyncIOExecutor._pending_futures` 并**立即 clear 集合**，不 await——job 若正处于
两个 await 之间，可能在 `db.close()` 后继续写库，甚至再 spawn 新 executor 任务。

在不改 `scheduler.py`（A4 分区）的前提下，`app/main.py` 侧的处理是：

- `_scheduler_inflight()`（app/main.py）：在 `sched.shutdown()` **之前**从
  `scheduler._executors[*]._pending_futures` 快照在途 job task（私有 API，
  `try/except` 防内部结构漂移，漂移时退化为空集并 log）。
- lifespan finally 顺序：快照 → `sched.shutdown()`（不再触发新 job）→
  `_drain_background(extra=快照)`（cancel + await，含异常消费）→ `db.close()`。
- `_drain_background` 做**两轮清扫**：第 1 轮 cancel 期间被 job/任务临终 spawn 的
  新登记任务，第 2 轮会再次收集并 cancel。

**残余窗口（接受并记录）**：若 APScheduler 升级导致私有结构漂移，快照退化为空——
此时 job 仍会被 `shutdown()` cancel，但不被 await；它在 cancel 注入前的同步段里
对 DB 的访问仍可能与 `db.close()` 竞态（aiosqlite 在关闭后调用会抛异常，由
scheduler 的 `metered()` 兜底吞掉，不会崩进程，但该次写入丢失）。同理，连续
spawn 两代以上任务的极端 job 可能漏过两轮清扫。以上概率都极低（本仓库 job 都是
短 tick 型），等 A4 落地公共访问器后可完全消除私有 API 依赖。

## 2. 给 A4 的建议改动（scheduler.py，可选但推荐）

**目标文件**：`app/institute/scheduler.py`
**原因**：把 `app/main.py::_scheduler_inflight()` 对 APScheduler 私有内部
（`_executors` / `_pending_futures`）的探测替换为 scheduler 模块自己的公共接口，
消除升级漂移风险。

```python
# app/institute/scheduler.py — 追加在 shutdown() 附近

def inflight_jobs() -> set[asyncio.Task]:
    """Snapshot in-flight job tasks. Call BEFORE shutdown() (it clears them)."""
    tasks: set[asyncio.Task] = set()
    if _scheduler is None:
        return tasks
    for ex in _scheduler._executors.values():
        for f in getattr(ex, "_pending_futures", ()):
            if isinstance(f, asyncio.Task) and not f.done():
                tasks.add(f)
    return tasks
```

落地后把 `app/main.py::_scheduler_inflight()` 的函数体替换为
`from .institute import scheduler as sched; return sched.inflight_jobs()`
（或直接删掉该函数改为调用点内联）。私有探测集中到 scheduler 模块内，与
APScheduler 版本演进同处一地。

## 3. 其他说明

- retry 端点（`app/api/tasks.py`）的执行策略推导规则：`source='research'` →
  限链 `settings.research_hand_names`（CLAUDE.md 规则 10）；其余 source →
  registry 默认 fallback。tasks 表未持久化 fallback 设置；若未来出现
  `fallback=False` 的生产调用方，应在 tasks 行上持久化策略而不是扩展推导
  （代码注释里已写明）。
- migration 0005 采用"旧行 work_date 保持 NULL、永不计入日上限"口径（R1-M3），
  部署当日 research 日上限从重启后重新起算，可能单日超跑至多一个完整
  `research_daily_cap`——一次性、可接受。
