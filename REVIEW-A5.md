# R5 独立审查：A5 / Phase 0 Hardening Small bundle

## 结论

**FAIL**

定向编译与 7 个测试全部通过，条件认领、板行/首卡事务和 daily 状态枚举本身正确；但 `_open_board()` 在事务提交后仍有一次未兜底的数据库读取，读取失败会被 `kickoff()` 误判为“开板失败”并把 topic 释放回 `pending`，留下“已存在 active board + 可再次认领 topic”的重复开板风险。

## 审查范围

- 已审 tracked diff：`app/institute/whiteboard.py`、`app/institute/daily.py`、`tests/test_whiteboard.py`。
- 已通读新文件：`tests/test_daily.py`、`PATCH-NOTES-A5.md`。
- 对照读取：`app/db.py`、`app/institute/theses.py`、`app/institute/scheduler.py`、`app/router/executor.py`、`migrations/0001_init.sql`、`tests/conftest.py`。
- 检查了 A1 当前 `app/router/executor.py` diff：A1 只加入字节感知的 `truncate_output()`，没有修改 `compact_error()`。
- 其他在途代理文件未纳入结论。

## 逐项核验

- **条件认领：PASS** — `app/institute/whiteboard.py:177-181` 使用 `id + status='pending'` 的条件 UPDATE，并检查 rowcount 后才开板。
- **会话创建最终失败：PASS** — `_create_board_session()` 的主路径和 fallback 若都失败，异常会到达 `kickoff()` 的开板异常分支并尝试释放 topic。
- **板 INSERT 失败：PASS** — `app/institute/whiteboard.py:145-155` 位于 `db.transaction()` 中，异常先由 `app/db.py:94-104` 回滚，再由 `kickoff()` 尝试释放 topic。
- **首卡 INSERT 失败：PASS** — 与板 INSERT 同一事务，失败会同时撤销板行，不会留下“无首卡板”。
- **释放 UPDATE 自身失败：NIT** — `app/institute/whiteboard.py:185-191` 的 `db.execute()` 若因持续 DB 锁直接 raise，会落到外层 `app/institute/whiteboard.py:195-197`，调度任务仍返回 `None`，但 topic 保持 `used`，且当前没有重试/回收机制。
- **释放并发精度：PASS（按当前写路径）** — 同一行处于 `used` 时第二次 kickoff 无法通过 `pending→used` 认领；当前仓库也没有其他 `used→pending` 恢复器，因此 `WHERE id=? AND status='used'` 不会在现有并发路径下覆盖另一次合法认领。它没有 claim token，若未来加入 stale-claim 恢复器则必须升级条件。
- **事务惯用法：PASS** — 与 `app/institute/theses.py:232-265,410-450` 一致，事务内只使用 yielded `conn.execute()`；没有调用会再次争抢 `_write_lock` 的 `db.execute()` / `db.insert()`，也没有在事务内 `bus.emit()`。
- **emit 时序：PASS** — `whiteboard.board_opened` 在 COMMIT 后发出，`app/institute/whiteboard.py:158-161` 会吞掉普通 emit 异常，不会仅因遥测失败释放 topic。
- **提交后整体边界：FAIL** — `app/institute/whiteboard.py:162` 的最终 `db.query_one()` 仍可在 COMMIT 后 raise，并触发 topic 回滚，详见 M1。
- **kickoff 不 raise：PASS** — 普通 `Exception`（包括 topic 释放本身失败）最终都由 `app/institute/whiteboard.py:195-197` 记录并转为 `None`；`@metered` 约定仍成立。与仓库现有惯例一致，任务取消用的 `asyncio.CancelledError` 不在该保证内。
- **进程终止窗口：PASS-WITH-NIT** — 认领提交后、板事务提交前若进程被杀，topic 会永久停在 `used`。这是本 small bundle 没有迁移/claim lease 前可接受的已知残余，但应进入后续恢复设计，见 N2。
- **daily 状态枚举：PASS** — `migrations/0001_init.sql:76-88` 只允许 `running/completed/failed/cancelled`；`NOT IN ('failed','cancelled')` 精确阻塞 `running/completed`，并放行 `failed/cancelled`。
- **daily 漏改扫描：PASS** — 全仓对 `workflow_runs`、`WORK_DATE`、`_ran_today` 和旧 `status != 'failed'` 的定向搜索未发现第二个“当天已跑”守卫。
- **compact_error 分析：PASS** — 当前 `app/router/executor.py:99-106` 确有负切片/末行前置问题；实测长末行时 200 字符输出以末行开头且完全丢失首行。A1 没有覆盖它。
- **硬规则：PASS** — 条件认领检查 rowcount；调度入口吞普通异常；新增时间戳仍走 `bus.now_iso()`；A5 分区及 prompt/workflow 文件状态未见 prompts 改动。

## Must-fix

### M1. 事务提交后的最终查询失败会错误释放已消费 topic

- `app/institute/whiteboard.py:145-155` 已经提交板行和首卡行。
- `app/institute/whiteboard.py:158-161` 正确兜底 emit，但随后 `app/institute/whiteboard.py:162` 仍执行一次未兜底的 `db.query_one()`。
- 若该读取抛出普通异常，控制流进入 `app/institute/whiteboard.py:184-191`，把 topic 从 `used` 改回 `pending`；数据库中却已经存在 active board。下一次 kickoff 可对同一 topic 再建一块板。

应保证 COMMIT 后没有普通异常再被解释为“开板未落库”：至少兜底最终读取并返回 `{"id": board_id}`，或让 `_open_board()` 显式区分 pre-commit failure 与 post-commit ancillary failure。应补测试覆盖“事务已提交、最终读取失败时 topic 仍为 used 且 board 仅一块”。

## Nice-to-have

### N1. 新 whiteboard 测试验证了 topic 释放，但没有模拟真实 INSERT/事务失败

`tests/test_whiteboard.py:57-77` 直接把整个 `_open_board` 替换为 `boom()`。它不只是验证异常被吞：对 `pending` 的断言及随后再次 kickoff 确实验证了释放和可重领；但它没有执行板 INSERT、首卡 INSERT、`db.transaction()` 回滚或 post-commit emit 分支。建议让真实 `_open_board()` 运行，只在指定的板/首卡语句处注入失败，并断言无 board/card 残留。

### N2. DB 持续不可写和进程硬杀仍会留下永久 `used`

`app/institute/whiteboard.py:185-197` 对释放失败没有持久化重试；进程在认领后被杀也没有机会执行释放。当前按 small bundle 边界不升级为 must-fix，但后续宜采用 claim lease/claim token + 启动恢复，或把 topic claim 与板/首卡落库纳入同一原子事务。

### N3. 板事务失败会留下孤立 session/workspace

session 在 `app/institute/whiteboard.py:139` 先于板事务创建。板或首卡 INSERT 失败时，topic 会释放且板行会回滚，但 session 行和 workspace 不会清理；重复失败会累积孤儿资源。

### N4. daily 测试没有显式覆盖 `running`

`tests/test_daily.py:25-52` 覆盖 completed/cancelled/failed，SQL 与 CHECK 约束足以证明 running 会阻塞，但增加一个直接 `_ran_today()` 的四状态参数化测试会更精确且更快。

## `compact_error` 补丁应用建议

**直接用。**

- A5 对当前 bug 的分析正确；建议的 3/5 head + 2/5 tail 实现在现有默认 `cap=1000` 和所附测试 cap 下保持总长度、首部、尾部及截断标记。
- A1 新增的是 `app/router/executor.py:79-96` 的 `truncate_output()`，`compact_error()` 本体仍是原实现，因此补丁仍需要。
- `PATCH-NOTES-A5.md` 中的行号因 A1 在函数前插入约 20 行而已过时；按函数上下文应用即可，并应同时落地所附测试。

## 验证

- `.venv/bin/python -m compileall app -q`：退出码 0，无输出。
- `.venv/bin/python -m pytest tests/test_whiteboard.py tests/test_daily.py -q`：`7 passed in 0.26s`。
- `git diff --check -- app/institute/whiteboard.py app/institute/daily.py tests/test_whiteboard.py`：退出码 0。
- 按要求未运行全量测试。
