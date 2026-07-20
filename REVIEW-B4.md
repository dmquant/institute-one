# R-B4 独立审查：白板相似度门、多样性选题与类目权重

## 结论

**FAIL**

A5 的条件认领/失败释放/单事务开板/COMMIT 后隔离，以及 A2 的 MCP `inserted`
原子判定均未发生回归；指定的 28 个测试全部通过。但配置端点允许
`skip_window_days > augment_window_days`，门查询却只回看 augment 窗口，实测会把配置上
应当 `skip` 的同主题判成 `pass`。此外，24 小时 verdict cache 没有绑定 embedding model
或配置版本，换模型及调阈值后仍会沿用旧 `skip`，与本实现声明继承的 A8
“当前模型过滤、换模型隐藏旧投影”语义不一致。前者是确定性功能错误，合入前必须修复。

## 审查范围

- 通读 `git diff -- app/institute/whiteboard.py app/api/whiteboard.py`。
- 通读 `migrations/0011_whiteboard_similarity.sql`、
  `tests/test_whiteboard_similarity.py`、`PATCH-NOTES-B4.md`。
- 边界对照：`REVIEW-A5.md`、`PATCH-NOTES-A2.md`、`REVIEW-A2.md`、
  `app/mcp.py`、`tests/test_whiteboard.py`、`tests/test_mcp.py`、`app/db.py`、
  `app/bus.py`、`app/institute/vectors.py`、`app/institute/prompts.py`。
- 其他代理的在途改动不计入 B4 结论；但会在验证结果中如实注明其干扰。

## A5 / A2 边界保全

### A5：PASS

- **条件认领保持不变**：`app/institute/whiteboard.py:475-479` 仍执行
  `pending → used` 的条件 UPDATE 并检查 rowcount；丢失竞争后继续下一候选，不会开板。
- **板行与首卡仍是一个事务**：`app/institute/whiteboard.py:425-435` 只使用事务给出的
  `conn.execute()`；任一 INSERT 或 COMMIT 失败都会回滚，不会留下无首卡板。
- **COMMIT 后普通异常不再逃逸**：emit 在 `app/institute/whiteboard.py:439-442`
  兜底；板向量写入由 `:198-220` 全部兜底；最终读取由 `:447-455` 兜底并返回已提交字段。
- **失败释放保持不变**：只有 `_open_board()` 在提交前/提交时抛出才进入
  `app/institute/whiteboard.py:487-495`，并以 `id + status='used'` 释放回 pending。
  释放 UPDATE 自身失败仍由 kickoff 外层 `:499-501` 吞掉，这是 REVIEW-A5 已记录的既有
  NIT，不是 B4 回归。
- **板向量失败不会释放 topic**：向量写入明确位于事务 COMMIT 后
  (`app/institute/whiteboard.py:436-445`)，且 `_store_board_vector()` 自身 never-raise；
  失败只让该板暂时不参与未来门判定。
- `tests/test_whiteboard.py` 的 7 个用例全部通过，包括真实板/首卡 INSERT 故障回滚、
  topic 释放，以及 COMMIT 后读取失败仍保持 `used` 且不重复开板。

### A2：PASS

- `add_topic()` 只在末尾增加可选 `category=None`：
  `app/institute/whiteboard.py:84-87`。`app/mcp.py:385` 的
  `add_topic(topic, question, source="mcp")` 仍完全兼容。
- `app/institute/whiteboard.py:91-97` 仍以同一条 `INSERT OR IGNORE` 的 rowcount 构造
  `inserted=bool(n)`；`app/db.py:181-186` 证实 `execute()` 返回 cursor rowcount。
- `app/mcp.py:386-396` 仅依据该键判断 `added/duplicate` 和是否发事件，hash alias 与并发
  路径没有退回 MCP 侧预查。
- `tests/test_mcp.py` 的 7 个用例全部通过且 **0 skip**；真实新增、hash alias、两个并发
  同参调用“一真一假/一行/一个事件”均实际执行。

## 相似度门状态机

### 正确部分

- 候选 SQL 的三值逻辑正确：`app/institute/whiteboard.py:321-325` 只排除
  `state='skip' AND checked_at > cutoff`；NULL/从未评估、过期 skip 均会进入。
- TTL 使用 UTC 秒级 ISO 字符串比较；`bus.now_iso()` 与 `_iso_ago()` 当前输出格式相同，
  因而字典序等于时间序。
- `_cosine()` (`app/institute/whiteboard.py:189-195`) 的点积与范数正确，任一零向量返回
  `0.0`，不会除零；额外正交/同向/双侧零向量探针全部通过。
- 新评估时 `vectors.embed() is None` 会在 `app/institute/whiteboard.py:245-247`
  直接 `pass`，不写 verdict cache；持续不可用时 `_store_board_vector()` 在 `:210-213`
  也不入库，开板保持降级放行。
- 默认阈值下：14 天内且 `cosine >= 0.85` skip；30 天内且未触发 skip、
  `cosine >= 0.65` augment；其余 pass，边界比较均使用 `>=`。

### [高 / Must-fix] 配置允许反转窗口，但查询会漏掉 skip 窗口内的板

- `app/api/whiteboard.py:27-33` 只分别校验两个窗口 `>=1`，允许
  `skip_window_days > augment_window_days`。
- `_similarity_gate()` 却先以 `augment_window_days` 截断全部历史行
  (`app/institute/whiteboard.py:249-254`)，之后才计算更远的 skip cutoff (`:255`)。
  因此 augment 窗口外、skip 窗口内的板根本不会进入比较。
- 临时库实测：已有一块 **20 天前、cosine=1.0** 的板，配置
  `skip_window_days=30, augment_window_days=14`；结果为
  `{"verdict": "pass", "prior": null}`，按配置应为 `skip`。

修复可二选一：在 API 与域函数共同强制 `skip_window_days <= augment_window_days`；或查询
回看两窗口的最大值，并只让 augment 判定使用 augment cutoff。后者更忠实于两个独立配置项。
应补 API 422 或门结果回归测试。

### [中] verdict cache 未绑定模型或配置版本

- migration 只缓存 `state/checked_at/similar_board_id`
  (`migrations/0011_whiteboard_similarity.sql:55-58`)，没有 model/config revision。
- 新鲜 cache 在 `app/institute/whiteboard.py:239-243` 直接返回；新鲜 skip 更早在
  `:321-324` 被 SQL 排除，根本不会走 `v.model = vectors._model()` 的 `:249-253`。
- `set_similarity_config()` (`app/institute/whiteboard.py:135-146`) 更新配置时也不失效缓存。

结果是：切换 `INSTITUTE_EMBED_MODEL` 后，旧模型产生的 fresh skip 最多继续阻塞 24 小时；
调高 skip threshold/缩短窗口也不会立即释放已缓存 topic。应为 cache 增加 model 与配置
fingerprint/revision，或在模型/配置变化时原子失效 pending topic 的 verdict。现有
`test_config_change_takes_effect_in_gate` (`tests/test_whiteboard_similarity.py:367-378`)
是在首次评估前改配置，没有覆盖“已有 cache 后改配置”。

### [低 / 硬规则] cutoff helper 直接调用 `datetime.now()`

`app/institute/whiteboard.py:170-174` 违反 `CLAUDE.md:47` 的
“Never datetime.now() raw”。虽然当前格式与 `bus.now_iso()` 一致且不直接落库，仍应从
项目时间 helper 取得当前 UTC，再做 timedelta，避免时钟源分叉并保持可测试性。

### [低 / 测试真实性] “逐字节一致”断言并非严格全尾部比较

`tests/test_whiteboard_similarity.py:239-242` 只做 `startswith(anchor)` +
`endswith(expected_tail)` + persona 次数；在 anchor 与 persona 之间插入任意额外字节仍会
通过，故不能严格证明“锚点行以下逐字节一致”。代码审阅表明普通板实际仍把相同的
`context_blocks` 传给 builder，当前行为等价；但测试应切掉第一段时间锚点后直接比较完整
剩余字节。

## 多样性与轮换

- effective 公式与报告一致：
  `score × category_weight − diversity_penalty × recent_same_category_count`
  (`app/institute/whiteboard.py:314,336-340`)。
- SQL 先按原有 `score DESC, created_at ASC` 排序，Python stable sort 在默认权重、零惩罚
  时保持原顺序；默认退化测试通过。
- `app/institute/whiteboard.py:297-308` 精确读取最近 N 块板，只有行数达到 N 且类别全同
  才触发；有其他类别时提升第一个替代候选，无替代时允许连击继续。三条轮换测试均通过。
- `LIMIT 10` 在 raw-score 预选后才计算 effective，意味着权重/惩罚不能把原始第 11 名提升
  进窗口；这是明确的有界候选折中，当前规模下可接受，但应保留为已知限制。

## BUILD_ON_PRIOR_BLOCK 与 prompts 边界

- 新块只定义在 `app/institute/whiteboard.py:62-68`，并于 `:617-638` 作为额外
  `context_blocks` 注入；既有白板 `task_text` 未改。
- prior 内容来自 `prior_board_id` 指向板的 topic/work_date，以及最近 3 张
  `status='completed'` 且非空的卡片 summary；每条截断 400 字符
  (`app/institute/whiteboard.py:570-590`)。测试实际验证 prior topic 与 summary 都进入 prompt。
- 当前共享工作树的 `git diff -- app/institute/prompts.py` **不是零 diff**：B3 在该独占分区
  增加了 `memory_block`。按第二轮分区记录这不归 B4，且默认 `None` 不改变白板输出；
  因此不计 B4 问题，但 PATCH-NOTES 中“git diff 可证 prompts.py 零改动”不能按整个共享
  工作树字面成立。

## Migration 0011

- 纯增量：一个 `INSERT OR IGNORE`、两个 `CREATE TABLE IF NOT EXISTS`、六个
  `ALTER TABLE ... ADD COLUMN`；没有改旧 migration、删列或重写表。
- `admin_state` 使用 `INSERT OR IGNORE`，不会覆盖 operator 已有配置；重复应用时，
  CREATE/seed 自身安全，ADD COLUMN 由当前 `db.migrate()` 的重复列恢复逻辑跳过。
- `topic_pool` 四列与 `whiteboard_boards` 两列均可 NULL、无破坏性默认值；旧显式列 INSERT
  与旧读取行为保持兼容。新测试每例从空库执行完整 migration 链，均成功。
- `topic_category_weights.weight >= 0`，缺行由代码解释为 1.0；板向量按 board 主键一板一条，
  删除板时级联删除。

## 硬规则汇总

- 调度入口不 raise：PASS，`kickoff()` 外层仍吞普通异常
  (`app/institute/whiteboard.py:467-501`)。
- 条件认领：PASS。
- 存储时间戳与 work date：持久字段均使用 `bus.now_iso()` / `work_date()`；cutoff helper
  存在上列 raw `datetime.now()` 违规。
- migration 只增：PASS。
- 既有 prompt 文本：B4 范围 PASS；共享工作树中的 B3 diff 已单独说明。

## 验证

- `.venv/bin/python -m pytest tests/test_whiteboard_similarity.py tests/test_whiteboard.py tests/test_mcp.py -q`
  → **28 passed in 1.16s**（0 failed，0 skipped）。
- `.venv/bin/python -m compileall app -q`
  → 最终复跑退出码 0、无输出。首次运行曾被 B4 范围外、并行编辑中的
  `app/institute/scorecard.py:81` SyntaxError 干扰；该代理更新后同一命令已通过。
- 补充隔离验证：
  `.venv/bin/python -m py_compile app/institute/whiteboard.py app/api/whiteboard.py`
  → 退出码 0。
- scoped `git diff --check` → 无输出；cosine 同向/正交/零向量探针通过。
- 按要求未运行全量测试。

## 重新审查门槛

1. 修复或拒绝反转窗口配置，并加入 20 天板 / 30 天 skip / 14 天 augment 回归测试。
2. 让 verdict cache 绑定当前 embedding model 与配置版本，至少补换模型、改阈值后的
   fresh-skip 用例。
3. 改用项目时间 helper，并收紧“锚点以下逐字节一致”的测试断言。
