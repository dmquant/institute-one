# Loop 修复待办 — 2026-07-20 循环审查结论

> 由三路并行审查 + 人工复核产出。每个工作包 = 一个 checkbox;loop 完成一包勾一格。
> 同一文件只允许一个包在飞(见指令书)。所有 file:line 已逐行核实。

## 硬约束(每个包都必须遵守)

- migrations 只增不改:新建编号文件,文件内禁 BEGIN/COMMIT/ATTACH/VACUUM/PRAGMA。
- 不改任何 prompt 字符串(prompts.py / workflows/*.json)。
- 状态迁移一律条件宣占(`UPDATE … WHERE id=? AND status=?` 查 rowcount)。
- 测试用 echo hand,跑 `.venv/bin/python -m pytest tests -q` 必须全绿。
- 不 git commit、不 git push、不还原工作区已有改动、不重启服务器。
- 每包完成写一份 `PATCH-NOTES-LOOP-<包号>.md`(仿现有 PATCH-NOTES 惯例)。

## 高优先级

- [x] **P1 executor 锁顺序**(高)
  [app/router/executor.py:274] `async with _sem(), _hand_lock(...)` → 改为先 hand 锁后信号量,
  使等 hand 锁的任务不占全局槽。补回归测试:hand A 忙 + 2 个排队 A 任务时,hand B 的 submit 不被阻塞。
  顺带评估 [app/institute/research_tree.py:110] NODES_PER_TICK=3 与 2 只 research hand 的搭配(可降为
  min(len(research_hand_names), max_concurrent-1) 或注释说明 P1 修后已无害)。

- [x] **P2 operator 路由毒行**(高)
  [app/institute/operator.py:725-728,753-755] 路由任务失败/解析异常时不写 disposition → 同一 action
  无限重选烧配额。修:失败也写占位 disposition(如 disposition='unparsed'、flags 带 route_error)占掉
  propose-once 名额;或 per-action 尝试计数+退避。保持 shadow=1 铁律。测试:失败 action 不被第二次重选。

- [x] **P3 factcheck 毒卡片**(高)
  [app/institute/factcheck.py:739-757,712-721] 验证失败只放回 pending,ORDER BY created_at ASC 使毒卡
  每 tick 重试 3 次、不退还每日配额(cap=10),约 2 小时烧光。修:fact_cards 加 attempts 列(新 migration),
  N 次(建议 3)后终态 failed;取卡排序考虑 attempts。测试:毒卡 N 次后不再被选中。

## 中优先级

- [x] **P4 chain 游标卡死**(中)
  [app/institute/chain.py:1665-1678] 持久化确定性失败 → 游标永久停在同一事件且每小时重付一次抽取
  模型调用。修:admin_state 按事件记失败次数,N 次后跳过该事件推进游标 + 开 operator action 卡。

- [x] **P5 research_tree 终态与事件**(中)
  a) [research_tree.py:537-561] `_maybe_finish_tree` 的翻转 UPDATE 缺 "无 pending/running 节点" 的
  NOT EXISTS 守卫(对齐 `_announce_if_drained` :576-583 的写法),修掉与 retry_node 的竞态(重试节点
  被静默 prune)。b) [research_tree.py:576-598] tree.completed 先置 announced_at 后 emit,崩溃窗口事件
  永久丢失 → 改 outbox 式或 emit 成功后置位。c)(可选)[research_tree.py:632-646] expired/rate_limited
  节点给一次有界自动重试(attempt 计数)。

- [x] **P6 operator 自改进链**(中)
  a) [operator.py:1184-1215] approve_proposal 宣占后 apply 失败 → 永久卡 approved+applied=0。修:对
  status='approved' AND applied=0 允许幂等重放 apply(apply 原语已幂等)。
  b) [operator.py:1112-1119,1301-1314] 陈旧 set_parameter 提案可把 confidence_floor 批降 → apply 时
  校验 new_floor > 当前 floor,否则拒绝。人工门语义不变。

- [x] **P7 fact_extract_queue lease**(中)
  [factcheck.py:1294-1334,1259-1269] claim/终态写无 lease,过期重开后旧 worker 迟到写入覆盖新 claim。
  修:照抄 fact_cards 的 lease 三件套(lease_id 列新 migration,终态写带 AND lease_id=?,回收清 lease)。

- [x] **P8 事件循环阻塞三处**(中)
  a) [operator.py:367-383] sweep_vault_conflicts 全表同步文件读+SHA → 搬进 asyncio.to_thread(顺带
  评估与 writer.doctor() 的重复扫描)。b) [chain.py:1530-1561] _auto_cluster 无界 pending×nodes 同步
  嵌套匹配 → pending 扫描加 LIMIT/分批 + 老化清理。c) [app/institute/paper_book.py:270-279 + 165-172]
  opener_tick 候选无 LIMIT 且 get_bars_pit 全历史扫描 → 候选加 LIMIT(50),PIT 读传 start 下限或加
  "只取最后一根" 专用读。

- [x] **P9 janitor 备份一致性**(中)
  [app/institute/scheduler.py:485-491] checkpoint 后 shutil.copy2 在写库,拷贝中自动 checkpoint 可损坏
  备份 → 改 `VACUUM INTO`(运行时执行,非 migration)或 sqlite backup API,失败不影响 janitor 其余步骤。

## 低优先级(合并为两包)

- [x] **P10 低危修补 A(operator/factcheck)**
  a) [operator.py:400-411] vault 漂移洪水 → sweep 单次开卡上限或聚合卡。b) [operator.py:953-961,
  1029-1033] _latest_observations 按近 N 天过滤陈旧快照。c) [factcheck.py:1053-1066,950-960] outbox
  错误记录 CAS 失配时重读 attempts 再记。d) [factcheck.py:1348-1349] tick 去掉自吞异常,交给 @metered
  记 cron_metrics。e) [factcheck.py:408-414,1428-1435] 向量扫描加 LIMIT/截断。

- [x] **P11 低危修补 B(chain/paper_book/scheduler/mailbox)**
  a) [chain.py:1240-1278] staged 断言加 attempts,N 次转 skipped。b) [chain.py:1568-1571] _auto_promote
  每 tick 上限。c) [chain.py:1066-1107] artifact 读取先钳制(512KB)。d) [paper_book.py:236-243] opened
  事件 ref_id 改用 position id 并在 payload 带 position_id(核对消费方)。e) [paper_book.py:438-456]
  benchmark base 区分"首挂"与"损坏",损坏返回 None + log.error,不静默重挂。f) [paper_book.py:260,293]
  opened_at 循环内取时。g) [scheduler.py:334-339] rate-limit-revival 查询加 LIMIT。h) [app/institute/
  mailbox.py:227-239] sweep 单次重驱加上限(如 20)。

- [x] **P12 收尾(文档+路线图)**
  a) CLAUDE.md 硬规则新增一条:失败重试必须带 attempts/lease 上界(毒行惯用法),引用 outbox/fact_cards
  为模板。b) 本清单各包在 roadmap/backlog.json 补卡(规则 9)。c) 全量验证:pytest 全绿 + compileall。

## 冲突分组(不得并行的包)

- operator.py:P2 / P6 / P8a / P10ab
- factcheck.py:P3 / P7 / P10cde
- chain.py:P4 / P8b / P11abc
- paper_book.py:P8c / P11def
- research_tree.py:P1(附带项)/ P5
