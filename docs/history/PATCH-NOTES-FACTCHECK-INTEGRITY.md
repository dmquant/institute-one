# PATCH-NOTES-FACTCHECK-INTEGRITY — 事实核查子系统四项要害修复

来源：事实核查子系统深度对抗审计（评分 5.5/10）findings 1/2/3/4/5/7 + R1 审核
finding 4。四块修复全部落在 `app/institute/factcheck.py`，schema 增量走
`migrations/0034_factcheck_lease.sql`（0033 空号，缺号无碍）。
**R2 轮对抗审核（GPT 5.6 Sol Max，REQUEST_CHANGES）的 3 个 findings 已在本文末
「R2 闭合」章节逐条闭合。**

## Fix 1 — verdict 严格化（findings 1/2/3）

- **冲突 verdict 不再升级为 DISPUTED**：`parse_verdict()` 收集到的 canonical 行
  若含多种不同 verdict（如 VERIFIED+DISPUTED），一律返回 UNVERIFIABLE——自相矛盾
  的回答不再按「保守序」坍缩成 DISPUTED 去打扰分析师。多行相同 verdict 保持现语义
  （一致即返回该值）；**零 canonical 行仍返回 None**（调用方落
  UNVERIFIABLE 并带「核查输出无法解析出判定」证据说明——`len(set(found)) != 1`
  规则只作用于非空 found，空集走原 None 语义，既有测试与证据文案不受损）。
- **无证据的 actionable verdict 拒绝**：`_verify_card()` 中 VERIFIED/DISPUTED 必须
  同时有非空 EVIDENCE 和至少一个 SOURCES URL，否则降级 UNVERIFIABLE（证据字段前缀
  「…降级 UNVERIFIABLE。」+ 原证据留档）。裸 "VERDICT: VERIFIED" 不再铸造可复用
  事实，裸 DISPUTED 不再开 mailbox 线程/事件。
- **引用过滤加固**：fence 按**种类+长度**配对（``` 与 ~~~ 各自配对；闭合线须同字符、
  长度 ≥ 开启线、且除空白外无其他内容——带 info string 的行是 fence 内容），
  ~~~ 再也关不掉 ``` fence；行首 4 空格/tab 的缩进代码块、`<!-- -->` HTML 注释块内
  的 canonical 行一律不算数。

## Fix 2 — 相似度复用护栏（finding 4）

- **一致性闸门** `_consistency_gate(new_claim, old_claim)`：比对数字集合（去千分位
  逗号、%/‰ 计入 token）、日期模式集合（ISO 式、中文年月日、Q/H 季半年）、中英否定
  词计数（不/未/没/没有/无/非… + not/no/never/without/none/n't）。任一不一致 →
  不允许直接 reused/self_contradicted，进入正常验证（保守方向：闸门只会把复用改成
  验证，绝不凭闸门判 dispute）。刻意轻量、不做 NLP——中文数字、同义日期写法不识别
  时的代价只是多一次验证。
- **DISPUTED 邻居不再无条件优先**：`_reuse_state()` 改为取相似度最高的过阈值邻居
  为准（VERIFIED 赢 → reused，DISPUTED 赢 → self_contradicted）；最高分上若
  VERIFIED/DISPUTED 同分冲突，拒绝猜测、进入验证。为给闸门供旧 claim 文本，候选
  查询加 join `fact_cards`；`_reuse_state` 签名新增 claim 参数（两处调用方同步）。

## Fix 3 — 验证 lease token（finding 5）

- `migrations/0034_factcheck_lease.sql`：`fact_cards` 加 `lease_id TEXT`（0015 不动，
  additive-only）。
- `_claim_card()` 认领时写入随机 lease（uuid4 hex）并返回；settle（`_verify_card`
  终态事务）、release（`_release_card`）全部带 `AND lease_id = ?`；
  `_recover_stale_running()` 重开 stale verifying 卡时清空 lease。效果：老 worker 的
  卡被 stale sweep 重开（乃至被新 worker 以新 lease 重新认领）后，其迟到的
  settle/release 因 lease 不匹配自然丢失，不再出现「status 又是 verifying 所以老
  写入照样落地」的窗口。已花掉的当日 attempt 槽位语义不变（不退款）。

## Fix 4 — outbox 吞错 + dispute 事件耐久化（finding 7 + R1 finding 4）

- **顶层异常不再自吞**：`drain_dispute_outbox()` 去掉整体 try/except——retry-limit
  sweep / 批量 SELECT 失败直接抛给调用方；scheduler 侧 `@metered("factcheck-outbox")`
  （主控已完成 metered 化，本卡未碰 scheduler.py）把失败记进 cron_metrics，
  `/api/cron/health` 可见。逐行 per-item 重试捕获原样保留（毒行不打断批次）；
  进程内调用方 `_surface_dispute` 自带 try/except。
- **`factcheck.disputed` 事件改 durable outbox 同事务投递**：复用 0025 的
  `factcheck_dispute_outbox` 表，0034 加 `intent TEXT NOT NULL DEFAULT 'mailbox'
  CHECK (intent IN ('mailbox','event'))` 区分两类意图（存量行回填 'mailbox'）。
  DISPUTED 判定 / self_contradicted 卡的**同一个 dispute 事务**里现在落两行：
  mailbox 意图（有分析师才有，原语义）+ event 意图（恒有；`recipient_id=''` 借
  0025 的 UNIQUE(dispute_id, recipient_id) 达成每 dispute 恰一行的幂等键）。
  - drain 按 intent 分派：mailbox 行走原 `_deliver_dispute_outbox_row`；event 行走
    新 `_emit_dispute_event_row`——attempts 条件自增（CAS）为认领、bus.emit 在事务外
    （emit 自己写 events 表，事务内调用会死锁）、随后标 delivered。emit 与
    delivered 标记之间崩溃 = 下次 drain 补发（**at-least-once**，消费方本就幂等：
    vault exporter 重读行投影）；对比旧的 post-commit 裸 emit（崩溃即永久丢失）。
  - 事件 payload 形状不变（kind/claim/category/analyst_id/source_*/related_fact_id/
    evidence/source_urls/thread_id）；`thread_id` 在入队时即写成确定性线程 id
    `factcheck-<mailbox outbox id>`（无分析师则 None），与 mailbox 投递最终物化的
    线程一致。
  - `_surface_dispute()` 简化为「按序即时 drain 刚入队的 mailbox、event 两行」，
    绝不 raise；drain 结果新增 `events` 计数。

## Schema 变更（migrations/0034_factcheck_lease.sql，新建）

```sql
ALTER TABLE fact_cards ADD COLUMN lease_id TEXT;
ALTER TABLE factcheck_dispute_outbox ADD COLUMN intent TEXT NOT NULL DEFAULT 'mailbox'
  CHECK (intent IN ('mailbox','event'));
```

无 BEGIN/COMMIT/ATTACH/VACUUM/PRAGMA（test_db_migrate 全链约束通过，含崩溃重放
ADD COLUMN 证明路径）。

## 测试（tests/test_factcheck.py：65 → 89 用例，+24）

- Fix 1：parse_verdict 参数表 +11（冲突→UNVERIFIABLE ×2、fence 种类/长度/info-string
  闭合 ×3、缩进代码 ×3、HTML 注释 ×3；原 `VERIFIED+DISPUTED → DISPUTED` 断言按新语义
  改为 UNVERIFIABLE）；证据闸门 +4（三种缺证据形态降级 + 有证据不误伤）。
- Fix 2：`_consistency_gate` 单测、最高分邻居决胜（双向）、同分冲突走验证、
  数字/否定不一致强制验证（+4；原 disputed-wins 用例重写为 highest-similarity 语义）。
- Fix 3：lease 写入/错 lease 不可 release、stale sweep 清 lease、老 worker 迟到
  settle 输给新 lease（e2e，+3）。
- Fix 4：顶层失败传播、metered 失败落 cron_metrics（调 scheduler._factcheck_outbox_job
  验证）、event 毒行按次计失败、崩溃边缘测试扩展为「event 行幸存 + 补发恰一次 +
  幂等不重发」、无分析师卡只落 event 行（+5 及两处既有用例扩展）。
- 验证命令全绿：`.venv/bin/python -m pytest tests/test_factcheck.py
  tests/test_db_migrate.py tests/test_cron_metrics.py -q` → **119 passed**；
  `.venv/bin/python -m compileall app -q` 通过。未跑全量套件（并行 agent 约定）。

## 刻意不改的

- `app/institute/scheduler.py` 零改动（factcheck-outbox 的 metered 化由主控完成，
  本卡只依赖其行为）；0015/0025 迁移文件原样（additive-only 硬规则）。
- prompt 字符串逐字未动（硬规则 4）；`CLAIM_VERIFY_PROMPT` 本就要求 EVIDENCE/SOURCES，
  证据闸门只是在解析侧执行既有承诺。
- API/MCP 读面（cards/outbox/claim_check）契约未变；outbox 行新增 intent 字段属
  增量透出。

## 遗留 / 已知边界

1. **event 投递是 at-least-once**：emit 与 delivered 标记间崩溃/写失败会补发重复
   事件；消费方（vault exporter 重读行、operator feed 开 action）需保持幂等——
   现有实现均满足。
2. **一致性闸门是启发式**：中文数字（「三百万」）、同义日期写法（2025-06 vs
   2025年6月）会被判不一致——代价是多走一次验证（保守方向），不是错误复用。
   R2 后锚点序比对进一步把复用收紧到「标点/空白/大小写之外逐词一致」。
3. **同分冲突判定依赖浮点全等**：仅在向量逐位相同（典型：同一 claim 文本）时触发，
   近似同分仍按最高分决胜；属可接受语义，如需容差可后续加 epsilon。
4. 旧库存量 pending outbox 行（迁移前写入）intent 回填 'mailbox'，其对应 dispute
   的事件早已按旧路径 emit 过或已丢失——不做历史补发。

---

# R2 闭合（对抗审核 GPT 5.6 Sol Max，3 findings → 全部修复）

三条攻击先以 /tmp 临时脚本对修复前代码复现确认成立（P1-1 fenced 伪证据通过闸门、
P1-2 主客体互换 gate=True、P1-3 交错 drain 双发 emitted=2），修复后同脚本全部转阻断。

## [P1-1] fenced/引用区伪 EVIDENCE+URL 过闸门 → 统一 bare-line 过滤

- 把 parse_verdict 的 fence/引用/缩进/HTML 注释过滤抽成 **`_bare_lines()`**，
  `parse_verdict` 与 `_parse_evidence` 共用同一答案面——被引用包裹的
  EVIDENCE/SOURCES 行（及其中 URL）不再进入提取，"裸 VERDICT + fenced 伪证据"
  组合落 UNVERIFIABLE。
- `_EVIDENCE_RE`/`_SOURCES_LINE_RE` 改为**行首锚定**（MULTILINE，容忍 markdown
  粗体，与 VERDICT 行同等待遇）：折进行中的注入标签（如经 `_quote_material`
  压平的 claim 材料）不再触发提取；`_VERDICT_MATERIAL_GUARD` 同步扩为
  VERDICT|EVIDENCE|SOURCES 三标签防注入。
- 测试：`test_quoted_pseudo_proof_fails_actionable_gate`（5 种引用形态参数化，
  e2e 到 verified_facts 行）、`test_parse_evidence_line_anchored_and_quote_filtered`、
  `test_quote_material_flattens_and_defangs`（扩展）。

## [P1-2] 数字/否定未绑定主体 → 锚点序（anchor sequence）比对

- `_consistency_gate` 第四道检查 **`_claim_anchor_seq()`**：按出现顺序提取
  数字/拉丁词/中文单字的有序序列（大小写与千分位归一；空白、标点不是锚点），
  序列不等 → 不复用、走验证。"A 收购 B" vs "B 收购 A"、数字归属互换（"A营收100亿
  B营收50亿" vs "A营收50亿 B营收100亿"）全部拦下；等价于把复用收紧到「标点/空白/
  大小写之外逐词一致」——宁可多验证一次，不错误复用（数字/日期/否定三道检查保留，
  提供独立可解释的拒绝理由）。
- 测试：`test_consistency_gate_anchor_sequence`（主客体互换 CN/EN、数字归属互换、
  标点/空白/大小写/千分位等价保持复用、多余尾词判不同）；
  `test_consistency_gate_numbers_dates_negation` 相应收紧（尾部多"同比增长"现在
  判不同）。

## [P1-3] outbox attempts 不是 lease → drainer lease token

- 0034 增列（additive）：`factcheck_dispute_outbox.lease_id / leased_at`。
- `_emit_dispute_event_row` 认领从 attempts 值 CAS 改为 **drainer lease**
  （`SET lease_id=?, attempts=attempts+1 WHERE … lease_id IS NULL`，复用
  fact_cards 的 lease 模式）；claim→emit→delivered 全窗口互斥，交错 drain 只有
  一个 drainer 能进 emit。batch SELECT 加 `lease_id IS NULL`，drain 顶部加
  stale-lease sweep（`OUTBOX_LEASE_STALE_MINUTES`=10，死 drainer 的行回收重试，
  已花 attempts 不退）。
- **失败路径分叉**：emit 失败（事件没发出去）→ 释放 lease + 记 last_error +
  保持 pending 有界重试；emit 成功但 delivered 标记写失败 → **绝不落 failed**，
  释放 lease 留 pending 补发（at-least-once 已声明、消费方幂等）；标记写异常
  连释放都失败时由 stale sweep 兜底。retry-limit sweep 加 `lease_id IS NULL`
  避免误杀在飞行中的行。
- 测试：`test_event_outbox_interleaved_drains_emit_once`（复现审计交错时序，
  断言恰一次 emit、attempts=1）、`test_event_outbox_delivered_marker_failure_keeps_pending`
  （标记失败留 pending + 补发成功=记录在案的重复事件）、
  `test_event_outbox_emit_failure_releases_lease_and_stays_pending`、
  `test_event_outbox_stale_lease_reopened_fresh_lease_respected`。

## R2 后验证

- `.venv/bin/python -m pytest tests/test_factcheck.py tests/test_db_migrate.py
  tests/test_cron_metrics.py -q` → **130 passed**（test_factcheck 89 → 100，+11）；
  `compileall` 通过。/tmp 复现脚本修复后输出全部 blocked。
- R2 后新增遗留：**event 行 emit 失败的重试计数不进 drain 返回值的
  retried/failed 计数器**（attempts 已在 lease 认领时自增，`_record_outbox_failure`
  的旧值 CAS 会 miss；行内 last_error 已直接写入，重试上限由 attempts+sweep 兜底）
  ——纯观测口径差异，不影响投递语义。
