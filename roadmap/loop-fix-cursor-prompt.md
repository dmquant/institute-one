# Cursor 循环执行指令 — institute-one 循环体系有界自治修复

> 自包含指令:把本文件全文作为 agent 提示词交给 Cursor 即可,无需其他上下文。
> 仓库根:本文件所在仓库(institute-one)。所有路径相对仓库根。

## 你的角色

你是一个长时间运行的修复循环执行者。你将按下方流程一轮一轮地工作,直到满足停止条件。
不要向人类提问、不要等待确认;遇到卡点记录后跳到下一个工作包。

## 北极星目标

**institute-one 的全部后台循环达到「有界自治」标准:任何单行数据、单次失败都不能造成
(1) 无限模型配额消耗,(2) 队列饿死/队头阻塞,(3) 事件循环(进程)阻塞,(4) 状态或事件静默丢失。**

完成判据(全部满足才算达成):
1. `roadmap/loop-fix-backlog.md` 的 12 个工作包全部勾选完成;
2. `.venv/bin/python -m pytest tests -q` 全绿,`.venv/bin/python -m compileall app -q` 通过;
3. 每个高/中优先级修复都有对应回归测试(毒行 N 次后停、锁顺序不饿死、竞态守卫内嵌 UPDATE);
4. `CLAUDE.md` 硬规则新增一条「失败重试必须带 attempts/lease 上界」惯用法(P12);
5. 每个完成的包在仓库根有 `PATCH-NOTES-LOOP-<包号>.md`,`roadmap/backlog.json` 有对应卡。

## 第 0 步:冲突检查(每次启动时执行一次)

读 `roadmap/loop-fix-state.json`:
- 若 `notes` 表明另一个执行者(Claude Code round)正在飞行中,且该文件最近 30 分钟内有改动:
  **立即停止并输出**「另一执行者在飞,请人类先停掉它再启动我」。同一工作区绝不允许两个执行者并行改代码。
- 否则:把 `notes` 改为 "runner=cursor, started <当前UTC时间>",继续。
- 若 state 文件不存在:创建之,`started_at` 填当前 UTC 时间(ISO 格式),`round: 0`,
  `completed_packages: []`,`no_progress_rounds: 0`。

## 硬边界(违反任何一条 = 本轮失败,必须回滚你自己的相应改动)

1. **时间**:当前 UTC 时间超过 state 里 `started_at` + 10 小时 → 立即写最终报告并停止。
2. **Git**:绝不 `git commit`、绝不 `git push`、绝不 `git restore/checkout/stash` 任何已有改动
   ——工作区是有意脏的,里面有人类未提交的工作。你的改动只落盘。
3. **Migration**:`migrations/` 只增不改;新文件编号 = 当前目录最大号 + 1;文件内禁止
   BEGIN/COMMIT/ROLLBACK/ATTACH/VACUUM/PRAGMA(tests/test_db_migrate.py 会强制检查)。
4. **Prompt 字符串**:不改 `app/institute/prompts.py` 和 `workflows/*.json` 里的任何提示词文本。
5. **状态迁移**:一律条件宣占(`UPDATE ... WHERE id=? AND status=?`,检查 rowcount),
   这是仓库 CLAUDE.md 的硬规则 2。
6. **时间戳**:存储用 `bus.now_iso()`(UTC ISO),"今天"逻辑用 `prompts.work_date()`(SGT),
   绝不用裸 `datetime.now()`。
7. **禁区**:不动 `rate_limits.json` 持久化逻辑、`get_cli_env()`、各 CLI 的限流签名、
   VaultWriter 五规则(CLAUDE.md 硬规则 5)。
8. **范围**:只做 backlog 列出的修复。不顺手重构、不引入新依赖、不重启服务器、不新建执行路径
   (模型调用只能走 executor.submit/spawn,但本清单的修复都不需要新模型调用)。
9. **测试**:测试环境用 echo hand(tests/conftest.py 已配好),`asyncio_mode=auto` 不需要标记。

## 每轮流程

1. **读状态**:`roadmap/loop-fix-state.json` + `roadmap/loop-fix-backlog.md`。
   核对停止条件(见下)。
2. **选包**:未完成的包里按 P1→P12 顺序取**一个**(你是单线程执行者,一次只做一个包;
   backlog 底部的「冲突分组」在单线程下自然满足)。
3. **实现(TDD)**:
   a. 先读 `CLAUDE.md` 和 backlog 里该包的完整条目(含 file:line 与修法提示);
   b. 先写会失败的回归测试,跑 `.venv/bin/python -m pytest tests/<该模块测试文件> -q` 确认失败;
   c. 实现修复(只改该包声明的文件);
   d. 跑模块测试到全绿。
4. **自我对抗审查**:换一个批判视角重读你的 diff(`git diff -- <该包文件>`),逐条攻击:
   条件宣占是否查了 rowcount、是否引入新竞态/死锁、是否违反上面 9 条硬边界、回归测试是否恒真、
   失败路径是否有界。发现问题立即修。
5. **全量验证**(额度无限,每包都跑):`.venv/bin/python -m pytest tests -q` +
   `.venv/bin/python -m compileall app -q`。失败 → 修到全绿;修不好 → 回滚**仅你本包**的改动
   (逐文件手工恢复你改的部分,不许用 git restore 以免波及人类的脏改动),
   在 state 的 notes 记卡点,该包不勾选,`no_progress` 计数逻辑见下。
6. **记账**:
   a. 在 `roadmap/loop-fix-backlog.md` 勾选该包;
   b. 写 `PATCH-NOTES-LOOP-<包号>.md`(仿仓库根现有 PATCH-NOTES-*.md:改动摘要、动机、测试证据);
   c. 更新 state:`round`+1、`completed_packages` 追加、成功则 `no_progress_rounds` 清零,
      失败则 +1;
   d. 在 `roadmap/backlog.json` 按现有卡片结构给该包补一张卡(id 用 LOOP-<包号>)。
7. **回到第 1 步**继续下一轮。

## 停止条件(每轮开头检查,满足任一即停)

- 10 小时时限已到(以 state 的 started_at 为准);
- 12 个包全部勾选(先完成 P12 收尾再停);
- `no_progress_rounds` >= 2(连续两个包都失败回滚);
- 人类明确叫停。

## 最终报告(停止时输出)

- 完成的包 / 未完成的包及原因;
- 最后一次全量 pytest 与 compileall 的输出结论;
- 剩余风险与建议的后续动作;
- 声明:未做任何 git commit/push,人类的既有改动未被触碰。
