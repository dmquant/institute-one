# PATCH-NOTES-LOOP-P10 — 低危修补：vault 漂移洪水上限（a）+ 陈旧观察快照过滤（b）

日期：2026-07-20 ｜ 范围：`app/institute/operator.py` + `tests/test_operator.py` ｜ 无 migration

## P10a：vault 漂移洪水 → sweep 单次开卡上限

### 问题

`sweep_vault_conflicts()` 每个 conflict/drift 路径开一张卡，无上限。人工一次性
重排/批量改动几百篇笔记后，下一次 sweep 一口气开几百张 `vault_conflict` 卡，
淹没 kanban，还给 router 喂进成百上千的待分诊行（原指向 ~400-411）。

### 修法

模块常量 `SWEEP_MAX_NEW_ACTIONS = 20`：单次 sweep 至多**新建** 20 张卡，
超出部分计入返回值新键 `deferred`（并 log.warning 一条聚合行）。要点：

- 上限只数 `created=True` 的新建；早前 sweep 留下的存活卡在 `open_action` 里
  幂等收敛（`created=False`），**不消耗**上限——洪水随后续 sweep 逐批排干，
  不会被自己的旧卡饿死（测试锁定：3 张漂移、cap=2 → 第一轮 opened=2/deferred=1，
  第二轮 opened=1/deferred=0，第三轮 0/0 稳态）；
- 返回形状向后兼容：`{"doctor", "opened"}` 原样保留，新增 `deferred`
  （scheduler 只 await 不消费具体键；既有测试不受影响）。

选"上限"而不是"聚合卡"：聚合卡需要新的 ref 语义与人工处置流程（一张卡对应
N 个路径的关闭条件说不清），上限 + deferred 计数在 cron_metrics/日志里同样可见，
且行为可预测。

## P10b：`_latest_observations` 过滤陈旧快照

### 问题

`_latest_observations(kind)` 取每个 subject 的最新快照**不看多旧**。某个 subject
一旦不再被 observe 覆盖（recipe 局面变化、observe 长期停跑），它冻结的最后
一张快照会永远喂给 `generate_proposals`：reject 会释放 dedupe_ref → 下一轮
sweep 又从同一张陈年快照提出同一个提案，循环往复（原指向 ~953-961, 1029-1033）。

### 修法

模块常量 `OBSERVATION_MAX_AGE_DAYS = 7`：`_latest_observations` 在 SQL 里加
`work_date >= (今日 SGT 工作日 - 7 天)`——**最新快照本身过期就整个 subject 掉队**
（不是退回次新的：次新只会更旧）。三个消费点（action_recurrence /
recipe_performance / router_quality）自动全部套上新鲜度门。时间语义：
`prompts.work_date()`（SGT 工作日）做日期减法，与 work_date 列同刻度，
无裸 `datetime.now()`。

7 天的取值理由：observe 是（拟）日更 sweep，快照描述 trailing 7 天窗口；
最新快照落后超过一周说明链条本身停摆——该修链条，而不是让上月的事实
继续开提案。

## 回归测试

- `test_sweep_flood_is_capped_per_run`（P10a）：见上文三轮断言；同时断言
  `doctor` 计数仍权威（cap 只限卡，不改计数）。
- `test_stale_observations_do_not_feed_proposals`（P10b）：直插一张 30 天前的
  烂 recipe_performance 快照（hits=9 / adoption=0，满足 retire 阈值）→
  `generate_proposals` count=0、零提案；**同样内容**换成今天的 work_date →
  正常提出 retire_recipe。metrics 恒定，只变日期——测的就是新鲜度门本身。

## 测试结果

```
.venv/bin/python -m pytest tests/test_operator.py -q
67 passed in 24.44s
```

（两个新测试先行红灯确认：flood 测试 AttributeError（常量不存在）、
stale 测试 `assert 1 == 0`。）

## R3 闭合（2026-07-20 复核 P2：卡上限在头部反复关闭时永久饿死尾部）

复核指出 P10a 的公平性漏洞：上限无游标、vault_index 查询无稳定 ORDER BY——
若前 N 张漂移卡被人工 done/dismiss（磁盘未修），ref 释放后下一轮 sweep 为
**同一批头部路径**重开卡再耗尽 cap，尾部路径永久 deferred。

修法（稳定排序 + 持久 round-robin 游标，复核建议的首选项）：

- ledger 查询加 `ORDER BY path`（path 是主键，序稳定）；
- `SWEEP_CURSOR_KEY = "operator:vault_sweep_cursor"`（admin_state，JSON 字符串）
  存本轮**最后一个被尝试**的路径；下轮把按 path 排序的候选列表旋转到游标
  之后开始（`rel > cursor` 二分语义，找不到则回绕到头）；
- 游标推进覆盖所有尝试结果（开卡成功 / 幂等收敛 / 重验跳过 / 宽限推迟），
  这正是公平性本体：头部无论以何种方式被消费，下轮都从它后面继续；
- 游标写入用普通 upsert（有意非 CAS）：sweep 由 scheduler max_instances=1
  串行化，并发手动触发最多轻微移动公平起点，无正确性影响（注释说明）。

回归测试：`test_sweep_cap_does_not_starve_the_tail`（liveness：3 条漂移、
cap=1、每轮人工 dismiss 当轮卡且不修磁盘，3 轮后断言三条路径全部至少被
开过一次卡；修前实测 carded 恒为 {vault:Reports/s0.md}——头部每轮重开、
尾部永不被访问）。

## R4 闭合（2026-07-21 复核 P2：poison path 在游标保存前弄沉整轮 sweep）

R4 指出另一种尾部饿死：`VaultWriter._resolve` 允许文件名带 newline/control
char，sweep 把该 path 拼进 action ref 时 `open_action` 的注入守卫抛
ValueError——异常逃到包住**整轮** sweep 的外层 try，此时游标 upsert 尚未
执行：下一轮从同一个 poison path 重新开始再失败，`SWEEP_MAX_ATTEMPTS` 和
round-robin 游标都无法推进，poison 之后的所有路径永久饿死。

修正（operator 侧异常隔离，两层）：

- **per-candidate try/except**：每个候选的"重验 + 宽限 + 开卡"独立包裹，
  坏行 `log.exception`（%r 转义控制字符）+ `errors` 计数 + 跳过，绝不中断
  轮转；返回值新增 `"errors"` 键；
- **游标在 finally 里落盘**：即使后续步骤爆炸，下一轮也从"已尝试到的位置"
  之后继续——重复失败的行至多每轮浪费一个 attempt，永远钉不死轮转。

根治在禁区外：`writer.py` 入口统一拒绝 control char（与 P8a 记档的
writer 级锁/doctor(detail=True) 合并为同一张后续卡），本轮不动 writer.py。

回归测试：`test_sweep_poison_path_does_not_abort_the_round`（真实通过
writer 写入 `Reports/a\nbad.md` 与 `Reports/z-good.md`、双双做旧致漂移；
断言整轮不报 error、errors==1、good 照常开卡、重复轮次收敛不卡死；修前
红灯：ValueError 逃逸、整轮返回 {"error": ...}、good 永不开卡）。
