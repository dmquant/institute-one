# PATCH-NOTES-LOOP-P2 — operator 路由毒行：失败也占 propose-once 名额

日期：2026-07-20 ｜ 范围：`app/institute/operator.py` + `tests/test_operator.py` ｜ 无 migration

## 问题

`route_actions()` 的候选查询用 `NOT EXISTS (disposition WHERE proposed_by=?)` 做
propose-once 守卫，但两条失败路径（router 任务结束时 `status != 'completed'`；
路由单个 action 时在途抛异常）只 `errors += 1` 后 `continue`，**不写任何
disposition 行**。守卫因此永远不会触发：同一条高优先级毒行每个 tick 都被重选，
无限烧模型配额并占满 cap（原审查指向 ~725-728 / 753-755，工作区演进后为
task-status 分支与 per-action except 分支两处）。

## 修法

新增 `_record_route_failure()`（`ROUTE_ERROR_FLAG = "route_error"`），两条失败路径
各补一次调用：写入一条占位 shadow disposition 占掉本 loop 的 propose-once 名额。

占位行的安全性质（铁律逐条核对）：

- `shadow=1` 恒定（铁律 1 不变，无任何 `shadow=0` 写路径）；
- `disposition='unparsed'` + `confidence=NULL`：approve 端点的 LIVE 楼层门对
  NULL confidence 永远 409，`'unparsed'` 也不在可提炼词表——占位行是遥测，
  永远不可消费、不可成 recipe；
- flags 走既有 `disposition_flags()` 再追加 `route_error`：pinned 领地
  （kind/disposition 级）在失败路径同样保留 `human_pinned`（铁律 2 不被洗掉）；
- 与 0022 部分唯一索引的并发收敛语义一致：占位 INSERT 输掉 propose-once 竞态
  → IntegrityError → log + 收敛（不是错误）；其他异常 log + 吞（失败路径必须有界，
  第二次故障不许弄沉整个 tick）。此时该 action 下个 tick 会被重选一次——
  这是显式选择的降级路径（DB 故障时宁可重试也不丢守卫），不是无限循环的回归。
- action 行本身仍然一字节不动（铁律 1）：只有 disposition 表多一行。

选择"占位 disposition"而非"per-action 尝试计数+退避"：前者复用 0022 索引这一
现成的 DB 仲裁原语，零 schema 变更；后者需要新列/新表（本任务禁止自建 migration）。
每个 action 的模型尝试上限自然成为 2（fast_loop + deep_loop 各一次），有界。

## 回归测试（前任执行者留下的 TDD 红灯，本次转绿）

- `test_router_failed_task_writes_placeholder_and_is_not_reselected`
- `test_router_inflight_exception_writes_placeholder_and_is_not_reselected`
- `test_router_failure_placeholder_keeps_pinned_marker`
- `test_router_failure_placeholder_is_never_consumable`

核心断言：两次 `route_actions()` 后模型只被调 1 次（counter）；占位行
approve→409、promote-recipe→409；action 保持 open 供人工处置。

## 测试结果

```
.venv/bin/python -m pytest tests/test_operator.py -q
62 passed in 6.28s
```

（修前基线：4 failed, 58 passed —— 正是上述 4 个测试的红灯。）
