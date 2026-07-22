# REVIEW-B3 — Analyst memory 独立审查

- 审查代理：R-B3
- 范围：仅 B3 独占分区及 `PATCH-NOTES-B3.md` 指定的集成点
- 方式：只读审查；除本报告外未修改仓库文件
- 结论：**FAIL**

## 结论摘要

1. **`prompts.py` 通过字节级核验。** `git diff` 只有 `memory_block` 关键字参数、说明文字和条件追加；`CITATION_MANDATE`、`FILE_DELIVERABLE`、日期锚点、persona 组装及其余既有函数源码均未变化。固定时间锚点后，当前默认值/`None`/空串/纯空白共 12 组调用与 HEAD 版本逐字节一致。
2. **`region=False` 的既有文件模式行为没有回归。** 原子写、全文件 ownership/frontmatter、全文件 hash-ledger、skip-if-unchanged、file-mode doctor 的判定顺序保持不变；`mode DEFAULT 'file'` 使老行仍落入旧路径。
3. **但五规则作为 VaultWriter 的整体契约已被 region 模式削弱，不能合并。** region 路径可丢失 `managed: institute`、把区内边缘空白编辑误判为 clean、改写区外 CRLF 字节，并会在同一天第二次冲突时直接覆盖已有且可能被人工编辑的冲突副本。
4. **Analyst memory 存在确定性漏数窗口。** 新版 memory 的游标使用“模型完成后”的 `created_at`；模型执行期间到达的材料，以及同秒到达的材料，会被下一轮严格 `>` 查询永久跳过。实测第二轮返回 `no new material`。

因此，对用户要求的两个明确问题：

- 五规则有没有任何一条被削弱？**有。region 模式削弱了 ownership、never-clobber、精确 skip 判定和 doctor；只有 atomic 明确保持。file 模式本身未回归。**
- prompts 有没有任何一个旧常量变化？**没有。既有 prompt 常量及日期/persona 组装零字节变化。**

## 阻断问题

### B3-H1 — memory 游标会永久漏掉 compact 进行期间及同秒到达的材料

严重级别：**HIGH / 阻断**

证据：

- `app/institute/memory.py:210-212` 用上一版 `created_at` 作为三个来源共同的 `since`。
- 三个来源分别在 `app/institute/memory.py:112-116`、`:152-156`、`:171-175` 使用严格 `created_at/finished_at > since`。
- 新版游标却直到模型调用完成后才在 `app/institute/memory.py:238-247` 生成并写入。
- `app/bus.py:28-29` 的时间戳只有秒精度；`tests/test_memory.py:28-30` 也明确用“未来 10 秒”规避相等时间戳。

这会产生两个漏数窗口：

1. 材料收集完成后、模型返回前到达的事件，其时间戳早于新 memory 的 `created_at`，本轮没收集、下轮又不满足 `> since`。
2. memory 插入后同一秒到达的事件与游标相等，也不满足严格 `>`。

独立探针实测：

```text
INFLIGHT_CURSOR 1 {'analyst_id': 'macro-analyst', 'skipped': 'no new material'}
```

探针在 `executor.submit` 内插入第二张 card，等待到下一秒后让模型返回；v1 成功落库，但第二次 compact 判定无新材料。

修复要求：在收集前捕获稳定 cutoff，并只消费 `(prev_cutoff, cutoff]`；新版保存该 cutoff，而不是模型完成时间。更稳妥的是按三个来源分别保存单调 id/cursor。秒级时间戳和严格 `>` 不能单独承担游标语义。

### B3-H2 — region 冲突副本同日复用，会覆盖人工编辑

严重级别：**HIGH / 阻断**

`app/vault/writer.py:325-334` 以日期生成唯一固定副本名，随后直接 `_atomic_write`，写前没有核对该副本自己的 ledger/disk hash，也没有选择新的不冲突名称。

确定性场景：

1. 人工无标记文件触发 `foo (institute update 2026-07-20).md`；
2. 人工编辑该冲突副本；
3. 同日再次 compact；
4. 第二次写入同一路径，人工编辑被覆盖。

独立探针实测：

```text
SIBLING_REUSE True False
```

前一个 `True` 表示第二次返回相同副本路径，后一个 `False` 表示人工加入副本的文本已消失。这直接违反 hard rule “human-edited note is NEVER overwritten”。

注：旧 file-mode 在 `app/vault/writer.py:242-250` 已有同名历史缺口；B3 没有改变旧路径，但把同一缺口复制到了新 region 路径，因而新功能本身不满足五规则。修复应让冲突副本也走 ledger 检查，或使用保证不存在的递增/唯一文件名；绝不能直接复用日期名。

### B3-H3 — region 模式不能保证 ownership 标记存活

严重级别：**HIGH / 阻断**

- fresh/upgrade/conflict 文件通过 `compose()` 写入 `managed: institute`，见 `app/vault/writer.py:167-188`、`:295-302`。
- 已存在 region 的更新只替换 marker 之间内容，见 `app/vault/writer.py:306-310`；frontmatter 被视为区外人工区。
- 因此人工删除 `managed: institute` 后，后续机构更新仍原位成功，而且不会恢复 ownership。

独立探针实测：

```text
OWNERSHIP_REMOVAL True False
```

即更新仍在原文件完成，但最终文件不再包含 `managed: institute`。这与模块首部 hard rule “every note carries YAML frontmatter with managed: institute” 正面冲突。

修复要求：明确把最小 ownership 元数据排除在人工可删除区域之外；更新和 doctor 都应校验它。可以保留其他人工 frontmatter key，但 `managed: institute` 必须强制存在，否则应走冲突/修复策略。

### B3-H4 — region hash 和替换并非字节安全，区内/区外人工改动均可能被无声改写

严重级别：**HIGH / 阻断**

有两个独立问题：

1. `app/vault/writer.py:111-118` 对磁盘 region 做 `.strip()`，`:272-273` 对新 body 也做 `.strip()`。因此 region 首尾的空格、空行等人工编辑不进入 hash；`:306-310` 会把它误判为未编辑并原位覆盖。doctor 同样复用该 stripped hash。
2. `app/vault/writer.py:282` 的默认文本读取会做通用换行转换，`:121-130` 又对整个文件 `splitlines()` 后用 `"\n".join(...)` 重建。即使区外内容逻辑相同，CRLF、其他换行符及部分末尾空行也会被归一化，和 `:308` 的“outside survives byte-for-byte”声明不符。

独立探针实测：

```text
WHITESPACE_EDIT True False
CRLF_PRESERVED False
```

前一行表示区内仅添加边缘空白后仍原位更新，空白被抹掉；后一行表示区外原有 CRLF 在 region 更新后消失。

修复要求：ledger hash 必须覆盖 marker 之间的原始字节/精确文本，不得 `.strip()`；替换时应按 bytes 读取并定位 ASCII marker，或用 `newline=""` 保留原换行后按 offset 切片拼接，只改 begin/end 之间的内容，保留区外换行和尾部字节。

## 其他问题

### B3-M1 — marker 解析器没有执行其声明的保守校验

严重级别：**MEDIUM**

`app/vault/writer.py:94-108` 声称“end before begin”及 malformed 文件走无 region 的保守路径，但实现会忽略 begin 之前的 end，并在后面找到任一 pair 后接受；嵌套和多对 marker 也只取第一对。

独立探针结果：

```text
PREMATURE_END_ACCEPTED (2, 4) owned
NESTED_ACCEPTED (0, 4) 'owned\n%% institute:begin %%\nhuman'
MULTIPLE_ACCEPTED (0, 2) 'owned'
```

应只接受“恰好一个 begin、恰好一个 end、begin < end、无嵌套/额外 marker”的结构；其余全部走 conflict。现有 7 个 region 测试没有覆盖这些形态。

### B3-M2 — doctor 的 region 分支可因非 UTF-8 文件直接中断

严重级别：**MEDIUM**

`app/vault/writer.py:355-358` 只捕获 `OSError`，未捕获 writer 读路径已经考虑的 `UnicodeDecodeError`。独立探针得到：

```text
DOCTOR_NON_UTF8 UnicodeDecodeError
```

删除行为核对：

- 文件在扫描前已删除：`:348-349` 正确计入 `missing`；实测 `missing=1`。
- 文件在 `exists()` 后、`read_text()` 前被删除：`OSError` 被捕获，最终计入 `drifted` 而不是 `missing`，但不会 raise。

应至少捕获 `(OSError, UnicodeDecodeError)`；如要保证分类精确，捕获后再判断一次是否存在。

### B3-M3 — 每来源 LIMIT 会和推进游标组合成永久丢弃

严重级别：**MEDIUM**

三来源 SQL 都是只读 `SELECT`，并且数量/单项字符上限确实生效：

- 日报：7 项 × 2000 字符；
- 白板：10 项 × 800 字符；
- 信箱：10 项 × 800 字符。

但查询按时间倒序取最新 N 项，compact 成功后游标直接推进到新版时间。若一个周期内超过上限，较老但尚未消费的记录不会在下一轮补取。上限保证了 prompt 有界，却不能宣称“自上一版以来的产出”都被压缩。应分页消费，或把 cursor 只推进到本轮实际消费的最老/边界记录。

### B3-M4 — PATCH-NOTES 的 whiteboard 精确补丁已经过时

严重级别：**MEDIUM / 集成前必须修订**

`PATCH-NOTES-B3.md:80-99` 假设 whiteboard 当前参数仍是：

```text
context_blocks=[context] if context else None
```

实际 `app/institute/whiteboard.py:617-639` 已构造 `context_blocks` 列表，并可能在 `:619-623` 插入 BUILD-ON prior block；当前调用是 `context_blocks=context_blocks or None`。若按 PATCH-NOTES 的“精确代码”替换，会丢掉该 prior block。

正确集成应保留当前 `context_blocks=context_blocks or None`，只追加：

```text
memory_block=await memory.memory_block(analyst.id)
```

其余三个调用点与当前代码匹配：

- `app/institute/analyst_daily.py:189`
- `app/institute/mailbox.py:167-169`
- `app/institute/workflows.py:240-244`

建议的 `from . import memory` 不形成循环依赖。

### B3-N1 — migration 没有约束 mode 枚举

严重级别：**NIT**

`migrations/0010_analyst_memory.sql:25` 的 `mode TEXT NOT NULL DEFAULT 'file'` 能保证老库已有行升级后为 `file`，所以老行为不变；但它没有 `CHECK (mode IN ('file','region'))`，与“mode 列只有 file/region”这一契约不完全一致。

`analyst_memory` 已有：

- `id` 主键；
- 关键字段 `NOT NULL`；
- `UNIQUE(analyst_id, version)` 并发兜底；
- latest 索引。

未约束之处：`version > 0`、`supersedes` 自引用完整性、`work_date` 格式。前两项不是当前阻断点，但 mode 枚举至少应在后续重建表 migration 中补齐（SQLite 不能直接给既有列追加 CHECK）。

### B3-N2 — “8000 字符硬上限”实际返回 8001 个 code point

严重级别：**NIT**

`app/institute/memory.py:84-89` 使用 `md[:8000] + "…"`，因此超限时结果正文为 8001 个 Python 字符。Python `str` 切片按 Unicode code point，不会截断 UTF-8 编码字节，**UTF-8 边界安全**；但可能切开 grapheme cluster，且“硬 8000 字符”存在 off-by-one。

另外，`MEMORY_MAX_CHARS=6000` 只是 prompt 指令，`compact_one` 没有在入库前强制截断模型输出。若 6000 是存储硬约束，当前实现未满足；若只是目标长度，注释应明确。

## prompts.py 字节级核验

`git diff -- app/institute/prompts.py` 只有一个 hunk：

- 新增 keyword-only `memory_block: str | None = None`；
- 扩展函数 docstring；
- persona 后按非空条件追加 `memory_block.strip()`。

没有改动：

- `CITATION_MANDATE`
- `FILE_DELIVERABLE`
- `now_sgt` / `work_date` / `date_anchor`
- `persona_block`
- `previous_steps_block`
- `substitute_variables`
- `extract_summary`

独立脚本从 `git show HEAD:app/institute/prompts.py` 读取基线，逐节点比较上述 9 段源码，并固定 `date_anchor` 后比较旧函数和新函数：

```text
PROMPT_BYTE_CHECK=PASS; unchanged_nodes=9; default_empty_cases=12
```

`tests/test_memory.py:182-187` 只验证“省略参数 == 空串”，没有与 HEAD snapshot 做逐字节比较，也没覆盖 `None`/纯空白/output_file 组合；因此现有测试对 hard rule 4 的保护不足，但本次独立核验结论是 **PASS**。仓库不存在 `tests/test_prompts.py` 或其他 prompt 专用测试文件。

## writer.py 五规则逐条结论

1. **Atomic：PASS。** `_atomic_write` 未改，所有 fresh/replace/upgrade/conflict region 写都通过同目录 tmp + `os.replace`。
2. **Ownership：FAIL（region）。** fresh 文件有 frontmatter，但后续 region 更新允许 `managed: institute` 被删除且不恢复。file 模式不变。
3. **Hash-ledger / never-clobber：FAIL（region）。** 区内边缘空白不进入 hash；同日冲突副本被直接复用覆盖；CRLF 区外字节会被重写。file 模式原判定链未改。
4. **Skip-if-unchanged：PARTIAL/FAIL（region 精确性）。** 三方一致时确实 no-op，但 stripped hash 会把区内空白编辑误判为 unchanged。file 模式不变。
5. **Doctor：PARTIAL/FAIL（region）。** 能忽略区外普通注释并识别正文/缺 marker 漂移；但同样忽略边缘空白、接受 malformed 多 marker，且非 UTF-8 会 raise。file 模式不变。

## region 判定链核对

实现顺序与报告的主链基本一致：

1. **skip**：ledger hash、目标 body hash、disk region hash 三者一致时直接返回，`app/vault/writer.py:288-293`。
2. **新文件**：磁盘不存在且不是 unreadable，写 fresh + `mode='region'`，`:295-304`。
3. **region hash 匹配**：disk region hash 等于 ledger hash，原位替换 region，`:306-312`。
4. **全文件 hash 匹配升级**：仅 marker-less 且 disk 全文件 hash 等于旧 ledger 时原位升级，`:314-320`。
5. **其余冲突**：写日期命名 sibling，`:322-337`。

判定方向是保守的；unreadable 文件不会被当新文件覆盖。问题不在主分支顺序，而在 hash 的 `.strip()` 语义、marker 结构校验、区外重建方式及冲突副本自身没有二次保护。

## memory.py 其余核验

- **并发版本认领：PASS（仅保证只落一版）。** `UNIQUE(analyst_id, version)` + `INSERT OR IGNORE` + `db.execute()` rowcount 检查正确；并发测试确实只有一个 v1。注意两个并发调用都在认领前调用模型，会双烧配额，只是 loser 输出被丢弃。
- **三来源：PASS with caveat。** collectors 的 DB 操作全是只读 SELECT；日报还按设计读取 workspace 文件并回退到 task output。数量和字符上限有效，但存在 B3-M3 的 overflow 丢弃。
- **executor：PASS。** `app/institute/memory.py:226-229` 使用 `executor.submit(settings.default_hand, ...)`；测试环境 default hand 是 echo。
- **版本分配：PASS。** `(prev.version + 1)` 的竞态由唯一键兜底，rowcount=0 的 loser 不 emit。
- **UTF-8：PASS with nit。** Python 字符串切片不会切坏 UTF-8 byte sequence；见 B3-N2 的 8001 字符问题。
- **compact_all：PASS。** 串行遍历，跳过 ops，单个失败不断链。

## migration 0010 核验

- 老 `vault_index` 行在 `ALTER ... DEFAULT 'file'` 后得到 `mode='file'`。
- non-region `_upsert(..., mode='file')` 会继续把旧路径写成 file 模式。
- migration runner 按文件事务执行，并能处理历史“列已加、ledger 未记”的重复列恢复场景。
- `analyst_memory` 的唯一键足以承担当前版本认领。
- 约束缺口见 B3-N1。

## exporter 核验

- `app/vault/exporter.py:395-402` 已把 `memory.compacted` 绑定到 `_on_memory`。
- handler 主体在 `:364-390` 捕获异常，不会把导出失败传播给 emitter；bus 自身也会兜底。
- 目标路径为 `Analysts/<slug analyst_id>/memory.md`。
- `app/vault/exporter.py:383-386` 明确传入 `region=True`。
- 测试覆盖人工区外注释在正常 LF/单 marker 文件中的存活；未覆盖实际 `register()` 后由 bus 触发，只是直接调用 handler。

## PATCH-NOTES 集成核验

- **调度建议正确。** 23:30 SGT 晚于 23:00 daily；compact 会提交模型调用，`gated=True` 与 scheduler 的 maintenance 语义一致；cron 自带 `max_instances=1`。当前代码尚未出现 `memory_compact_time`，说明补丁确实尚未应用。
- **analyst_daily / mailbox / workflows 三处注入代码正确。**
- **whiteboard 补丁必须按 B3-M4 更新后再应用。**
- `_handoff` 是非分析师手工 prompt，不注入 memory 的判断合理。

## 验证结果

按要求未跑全量：

```text
.venv/bin/python -m compileall app -q
PASS

.venv/bin/python -m pytest tests/test_memory.py tests/test_vault.py -q
19 passed in 0.57s
```

仓库没有 prompt 专用测试文件，故实际运行的是 `test_memory.py` + `test_vault.py`。当前 `test_memory.py` 有 7 个测试，不是背景中所称的 8 个；`test_vault.py` 共 12 个，其中 7 个为本轮 region 新增测试。

`git diff --check` 对 B3 文件无输出。所有现有测试通过，但它们没有覆盖本报告复现出的阻断边界。

## 合并门槛

在改为 PASS 前至少需要：

1. 修复 memory cutoff/cursor 漏数，并增加“模型运行期间到达”“同秒到达”“超过 LIMIT 后续补取”测试。
2. region 使用精确内容 hash 和原字符串切片，保留区外原始换行/字节。
3. 对 marker 做唯一、顺序、无嵌套校验；malformed 一律 conflict。
4. 强制保留/校验 `managed: institute`。
5. 冲突副本存在时也执行 never-clobber，禁止同日覆盖。
6. doctor 捕获解码错误，并为 whitespace/malformed/non-UTF8/删除竞态补测试。
7. 修订 PATCH-NOTES 的 whiteboard 注入片段。
