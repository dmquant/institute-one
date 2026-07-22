# REVIEW-C1 — Phase 3 Fact-check v2 独立审查

## 结论

**FAIL**

核心数据模型、三态复用闸、SGT `work_date`、事件契约和 MCP 读工具方向基本正确，定向测试也全部通过；但验证前没有持久化条件认领，每日 cap 只统计成功判定且存在并发越界，无法兑现“防双验证/限制 quota”的核心保证。判定解析还会把引文、代码块和否定句中的 token 当成最终 verdict，而现有 echo 测试恰好用提示词本身充当答案，掩盖了这一问题。

当前工作树也尚未挂载 router/hooks/scheduler；PATCH-NOTES 的 vault 路径与 ROADMAP 不一致，且 source callout、Step-0 disputes 注入、Obsidian 写作命令均未交付。因此 Phase 3 全节不能验收。

## 审查范围与验证

- 已审：`migrations/0015_fact_check.sql`、`app/institute/factcheck.py`、`app/api/factcheck.py`、`app/mcp.py` 的三个 fact-check 只读工具增量、`tests/test_factcheck.py`、`PATCH-NOTES-C1.md`。
- 仅为契约核对而读取：`ROADMAP.md`、`CLAUDE.md`、`app/main.py`、`app/institute/scheduler.py`、`app/vault/exporter.py`、`app/institute/mailbox.py`、`app/institute/operator.py`、`app/bus.py`。
- `compileall app -q`：**PASS**。
- `.venv/bin/python -m pytest tests/test_factcheck.py tests/test_mcp.py -q`：**42 passed in 1.88s**（35 个 fact-check 参数化用例 + 7 个 MCP 用例）。
- 按要求未跑全量测试。

## ROADMAP Phase 3 逐项验收

| ROADMAP 项 | 结论 | 核对结果 |
|---|---|---|
| Claim extraction | **PARTIAL** | durable queue、白板/研究 hooks、≤3 cap、枚举落库、来源级幂等均已实现；但实际使用 `settings.default_hand`，未兑现 opencode/cheap-hand；echo 用例把预期 JSON 塞回源文本，不是生产形态的模型输出。 |
| Tier-1 reuse gate | **PASS-WITH-NITS** | VERIFIED→`reused`、DISPUTED→`self_contradicted`、无向量→`fresh`（重新验证而非直接复用）方向正确；跨类别候选、按新 claim 类别取阈值、`expires_at` 冻结均有明确实现。默认表内部一致，但没有分布校准，且向量中断期间生成的事实没有后续补向量路径。 |
| Verification | **FAIL** | UNVERIFIABLE/DISPUTED 顺序及 UNVERIFIED 防误配的窄案例正确；但 verdict 可从引文/代码块/否定句误取，且没有模型调用前的持久条件认领；每日 cap 可并发越界，也不计失败调用。验证 hand 亦未保证具备 websearch。 |
| Disputed-claim surfacing | **FAIL** | mailbox 调用签名和 C4 bus 事件契约对齐；但 digest 尚未挂载，PATCH 路径错误，source dossier warning callout、Step-0 disputes 注入均缺失，且 surfacing 失败没有 durable retry。 |
| Claim-check-before-write | **FAIL / PARTIAL** | API/domain 函数存在且无 DB 写入；但 router 当前未挂载，Obsidian plugin command 缺失。查询会混入 UNVERIFIABLE/过期事实，向量路径出错时也不会回退到已算出的关键词命中。 |
| MCP fact tools | **PASS** | `fact_cards_list`、`fact_cards_get`、`claim_check` 使用现有 JSON-RPC envelope/schema/错误映射；当前写工具仍只有 `research_queue_add`、`topic_pool_add`、`institute_ask` 三个。 |

## 必修问题

### P1-1 — 每日 cap 不是模型调用 cap，且并发可突破

位置：`app/institute/factcheck.py:476-519, 522-564`；测试：`tests/test_factcheck.py:481-513`。

`verify_pending()` 用当日 `verified_facts` 成功行数计算预算，然后在 `_verify_card()` 里先调用 `executor.submit()`，模型完成后才执行 `UPDATE ... WHERE status='pending'`。这只能防双写，不能防双验证，也没有在调用前原子占用每日额度。

实测反例：

- `DEFAULT_DAILY_CAP=1`，并发两次 `verify_pending()`、两张 pending 卡，最终生成 **2** 条 `verified_facts`。
- `DEFAULT_DAILY_CAP=1`，同一张卡的模型连续失败两次，产生 **2** 次模型调用、0 条判定行，卡仍为 pending；继续 tick 可无限烧 quota。

必须在调用前做持久条件认领并原子占用 SGT work-date 的 attempt slot；失败尝试是否退还额度需要显式裁决。当前进程内 `_verifying` 集合不能替代数据库仲裁。

### P1-2 — verdict 解析会从非结论文本误判，echo 测试是伪 oracle

位置：`app/institute/factcheck.py:435-459`；测试：`tests/test_factcheck.py:179-212, 420-478`。

正确部分：

- 大小写变体由 `re.IGNORECASE` 支持；
- `VERDICT: UNVERIFIED` 返回 `None`；
- 全局级联确实让 UNVERIFIABLE 优先于 DISPUTED。

错误边界的实测结果：

- `> VERDICT: DISPUTED` 后跟真实 `VERDICT: VERIFIED` → **DISPUTED**；
- fenced code block 内 `VERDICT: DISPUTED` 后跟真实 `VERDICT: VERIFIED` → **DISPUTED**；
- `The claim is NOT VERIFIED.` → **VERIFIED**；
- 多个 VERDICT 行会按全局 token 优先级选值，完全不报告输出歧义。

`test_parse_verdict_on_mirrored_verify_prompt_is_verified` 明确把提示词里的 `VERDICT: VERIFIED|...` 当作模型结论；DISPUTED/UNVERIFIABLE 用例则把 verdict 注入 claim，再依赖 echo 回显。它们没有测试任何生产形态的 verifier 输出，反而锁定了错误行为。

应先提取允许位置上的规范 verdict 行（排除 blockquote/code fence/示例），对多个互相冲突的行采取明确的保守策略，并增加独立于 prompt/claim 的输出 fixture。

### P1-3 — ROADMAP 的写作时检查与争议投影未完整交付

位置：`ROADMAP.md:129-134`；`PATCH-NOTES-C1.md:80-141`；当前挂载点 `app/main.py:116-124, 152-184`、`app/institute/scheduler.py:154-175, 278-288`、`app/vault/exporter.py:393-402`。

- 当前树没有 factcheck `register()`、API router、scheduler job 或 exporter handler，生产管线尚不工作；测试通过手工 include router 避开了这一事实。
- ROADMAP 要求 `Inbox/Disputed Claims.md`，PATCH 却写 `FactCheck/争议论断.md`（`PATCH-NOTES-C1.md:130-133`）。
- ROADMAP 要求 source dossier 的 `> [!warning]` managed-region callout 和分析师 Step-0 disputed block；分区与 PATCH 均无实现。
- ROADMAP 要求 Obsidian plugin command；仓库中没有 claim-check plugin 消费方。

以上不是 nit；在补齐前只能把相应 ROADMAP 项标为 partial。

## 其他问题

### P2-1 — claim_check 候选语义与降级承诺不一致

位置：`app/institute/factcheck.py:837-909`；`app/api/factcheck.py:16-30`；`app/mcp.py:380-390`。

- 两条候选 SQL 都未限制 `vf.verdict IN ('VERIFIED','DISPUTED')`，实际会返回 **UNVERIFIABLE**，与 API/MCP 文档不符。
- 两条候选 SQL 都不处理 `expires_at`；过期的短 TTL 财务事实会永久以当前 verdict 命中，且响应不标记 stale。
- 关键词命中已在向量遍历前算好，但任一损坏向量/查询异常都会返回 `{"mode":"error","hits":[]}`，不会降级到关键词结果。实测损坏 BLOB 即复现。
- `ClaimCheckBody.text` 没有长度上限，整段输入会直接送 embedding/tokenizer；`k` 虽由 domain 钳制，但 API schema 未表达 1..20。

### P2-2 — 抽取/核查 hand 不满足 ROADMAP 的能力约束

位置：`app/institute/factcheck.py:382-388, 522-529`。

抽取和核查都走 `settings.default_hand`。这不保证抽取使用 cheap/opencode，也不保证核查使用可联网的 claude/gemini；若默认 hand 是 ollama/echo/其他无 websearch hand，系统仍会把结果写成事实判定。executor 唯一路径规则本身是满足的，但 hand 能力路由未满足 Phase 3 语义。

### P2-3 — extraction parser 与 echo 测试仍有错取边界

位置：`app/institute/factcheck.py:228-283`；`tests/test_factcheck.py:1-13, 137-176, 272-304`。

fenced JSON 优先于模板裸 `[]`，这一点正确；但解析器会取输出中“最后一个有效 fence”，无法区分模型答案与被引用/回显的源文 JSON。没有 fence 时又取第一个可解码对象，echo prompt 中的模板裸 `[]` 会吞掉后续真实裸数组。

现有 E2E 用例把 fenced claims 直接写进待抽取源文，再让 echo 回显，证明的是“源文可被 parser 当答案”，不是模型按 Filter-A/B 生成了结构化结果。应补 parser-only 的真实裸/fenced 输出、模板回显+后置答案、源文含无关 JSON fence 等对抗用例。

### P2-4 — surfacing 在终态提交后失败不会重试

位置：`app/institute/factcheck.py:546-575, 583-623`。

判定行/`self_contradicted` 状态先成为终态，之后 mailbox/event 全部 catch-and-log。任一瞬时失败都不会被后续 tick 再投递；rows 虽是 truth，但当前没有 outbox/sweep 来补 mailbox、vault 或 operator action。

### P2-5 — extraction 终态更新未遵守条件转换硬规则

位置：`app/institute/factcheck.py:778-802`。

queue 的 pending→running 使用了 rowcount 条件认领；但 running→done/failed 的 UPDATE 没有 `AND status='running'`。若 stale recovery/人工重置与旧 worker 交错，旧 worker 可覆盖新状态。所有状态转换都应带预期源状态并检查 rowcount。

## 可接受项与 nits

- `REUSE_POLICY_DEFAULTS` 与 0015 seed 完全一致：numerical/financial `0.92/7d`、event/policy `0.88/30d`、other `0.90/14d`。作为首版默认值方向合理，但没有真实 bge-m3 相似度分布校准；admin_state 数值也没有 0..1/非负 TTL 校验。
- 无向量时 `_reuse_state()` 返回 `fresh`，新卡进入 pending 后重新验证，而不是直接复用：**方向正确**。
- DISPUTED 合格近邻优先于 VERIFIED，`related_fact_id` 指向原判定：**正确**。
- 跨类别候选 + 使用新 claim 类别阈值是明确、可辩护的抗标签抖动策略；TTL 冻结在原 `verified_facts.expires_at`：**正确**。
- `content_hash=hash(source_kind, source_ref, claim)` 会让同一 claim 的不同来源形成两张卡。对来源追溯、分析师反馈和 source dossier 投影而言合理；向量正常时第二张卡应由 reuse gate 避免重复核查。
- `mailbox.create_thread(subject, analyst_id, body)` 参数顺序与 `mailbox.py:42` 一致。
- `factcheck.disputed` 与 `operator.FACTCHECK_DISPUTED_EVENT` 完全一致；event `ref_id` 是 card id，payload 的 `claim`/`analyst_id` 也是 C4 消费方使用的键。
- hooks 均 catch 异常、只入 durable queue，不在 bus handler 内调用模型；模型调用只出现在 `extract_claims()`/`_verify_card()` 等 tick/显式路径。
- 时间口径：存储时间使用 `bus.now_iso()`，daily cap 使用 SGT `work_date()`；符合硬规则。
- 0015 是纯新增迁移，无事务控制/PRAGMA/VACUUM，CHECK/FK/index 与现有 B1 迁移纪律兼容。由于尚未上生产，P1-1 所需 schema 调整应在首次应用前完成。
- prompt 均集中在新常量，调用处未改写字符串；未发现绕开 executor 的模型调用。

## PATCH-NOTES-C1 四项集成核对

1. **main.py：代码形态正确，但尚未应用。** `factcheck.register()` 放在 bus handler 注册区、scheduler 启动前合理；API router 放在 SPA fallback 前合理。应用后必须改用 `create_app()` 做 route 存在性测试，不能继续由测试自行 include。
2. **scheduler.py：代码形态正确，但须与 config 同时应用。** `@metered("factcheck-tick", gated=True)` 和 30 分钟 interval 符合维护门控；`every()` 已正确处理非正间隔。
3. **config.py：字段定义形态正确。** `factcheck_tick_minutes=30` 合理；`factcheck_daily_cap=10` 的字段名/环境变量映射正确，但当前实现并不能保证“verification tasks per SGT work date”，须先修 P1-1。
4. **exporter.py：不可原样应用。** rows-as-truth、全量重投影、never-raise 风格正确；但输出路径违背 ROADMAP，且只实现 digest，不包含 source callout/Step-0 链路。应先改路径并明确其余两项的集成方案与测试。

## 复审门槛

至少完成以下事项后再改判：

1. 持久化 verification claim/attempt cap，新增并发 cap、失败 attempt、重启恢复测试。
2. 重写 verdict 规范行提取并加入引文、代码块、冲突多行、否定句、大小写用例；移除 echo prompt 作为 verdict oracle。
3. 修复 claim_check verdict/expiry 语义和向量异常→关键词降级。
4. 明确联网 verification hand 与 cheap extraction hand。
5. 修正并实际挂载四项 PATCH；补齐 ROADMAP 的 digest 路径、source warning、Step-0 disputes 和 Obsidian command。
