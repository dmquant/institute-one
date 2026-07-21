# Loop 指令书 — 循环体系有界自治修复(2026-07-20)

## 北极星目标

**institute-one 的全部后台循环达到「有界自治」标准:任何单行数据、单次失败都不能造成
(1) 无限模型配额消耗,(2) 队列饿死/队头阻塞,(3) 事件循环(进程)阻塞,(4) 状态或事件静默丢失。**

完成判据(全部满足才算达成):
1. `roadmap/loop-fix-backlog.md` 12 个工作包全部勾选完成;
2. `.venv/bin/python -m pytest tests -q` 全绿,`.venv/bin/python -m compileall app -q` 通过;
3. 每个高/中优先级修复都有对应回归测试(毒行 N 次后停、锁顺序不饿死、竞态守卫内嵌 UPDATE);
4. CLAUDE.md 硬规则收录「失败重试必须带 attempts/lease 上界」惯用法;
5. 每包有 `PATCH-NOTES-LOOP-<包号>.md`,roadmap/backlog.json 有对应卡。

## 硬边界(每轮开始先核对)

- **时间**:读 `roadmap/loop-fix-state.json` 的 `started_at`;当前 UTC 时间超过 started_at + 10 小时
  → 立即写最终报告并停止循环(ScheduleWakeup stop:true),未完包留在 backlog 里如实报告。
- **并发**:每轮启动的 subagent 总数 ≤ 50(Fable 5 max + codex GPT-5.6 fast 合计)。真实并行度受
  Workflow 上限 min(16, 核数-2) 约束,超出部分排队完成——这是正常的,不要为凑并发拆散任务。
- **进度**:连续 2 轮没有新勾选任何包 → 停止并报告卡点。全部勾完 → 执行 P12 收尾后停止。
- **Git**:绝不 commit、绝不 push、绝不还原工作区已有改动(工作区是有意脏的)。改动只落盘 + 写
  PATCH-NOTES。绝不重启服务器。
- **Migration**:只增不改;每轮最多一个包新建 migration,编号 = 写入时目录里的最大号 + 1(当前已到
  0034)。文件内禁 BEGIN/COMMIT/ATTACH/VACUUM/PRAGMA。
- **范围**:只做 backlog 里列出的修复,不顺手重构、不改 prompt 字符串、不动 rate_limits/VaultWriter
  等 CLAUDE.md 第 5 条列举的禁区。

## 每轮流程

1. **读状态**:`roadmap/loop-fix-state.json` + `roadmap/loop-fix-backlog.md`,核对上面三个停止条件。
2. **选包**:从未完成的包里按优先级(P1→P12)选出本轮可并行的包——遵守 backlog 底部的「冲突分组」
   (同一文件的包不得同轮),migration 包每轮最多一个。
3. **扇出**(用 Workflow 编排,每包一条 pipeline):
   - 实现者:Fable 5(model 'fable', effort 'max'),按包内描述 TDD 实现——先写会失败的回归测试,
     再改代码,跑该模块相关测试(如 `pytest tests/test_operator.py -q`)。只许改自己包声明的文件。
   - 对抗校验者:Fable 5(effort 'max'),独立读 diff,专职证伪:找条件宣占漏洞、新引入的竞态、
     违反硬边界之处;发现问题回给实现者修(最多 2 轮往返)。
   - codex 二诊:通过 agentType 'codex:codex-rescue'(GPT-5.6, fast reasoning)对该包 diff 做只读
     独立审查;其 CONFIRMED 级发现由实现者修复。审后核对 git status,codex 是只读的、不应有意外改动。
4. **集成验证**(所有包合流后,单个整合 agent):`.venv/bin/python -m pytest tests -q` 全量 +
   `compileall`。失败 → 定位到包、回给该包实现者修,本轮内修不好则撤销该包本轮改动(只撤它声明的
   文件中本轮新增的部分)并在 state 里记卡点,不勾选。
5. **记账**:勾选完成的包;写各包 PATCH-NOTES-LOOP-*.md;更新 state(round+1、completed、
   no_progress_rounds、notes);在 roadmap/backlog.json 给完成的包补卡(P12 也会兜底核对)。
6. **续约**:ScheduleWakeup(delaySeconds=60, prompt 原样传回本 /loop 输入)进入下一轮;满足停止
   条件则 stop:true 并输出最终报告(完成/未完清单、测试结果、剩余风险)。

## 报告要求

每轮结束在回复里用 3-6 句话说明:本轮完成了哪些包、测试状态、下一轮计划;最终轮给完整收尾报告。
