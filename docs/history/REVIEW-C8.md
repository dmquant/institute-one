# REVIEW-C8 — hand weights 接线与文档同步

## 结论：FAIL

四个 hand 决策点的接线语义正确，定向测试与编译检查均通过；但本次交付还包含文档同步，而新增/勾选的文档中有两处可复现的完成度误报，故不能按 C8 整体通过。

## 问题分级

### M1（中）Curl-back digests 被写成已接入 CLI prompt，实际只有端点

- `CLAUDE.md:31` 把 digests 描述为 “Step-0 context blocks for CLI hands”。
- `ROADMAP.md:118` 已勾选，并明确称 CLI hands 会在 prompt 中执行 Step-0 `curl`。
- 实际端点及 8KB/占位降级已实现于 `app/api/digests.py:31-52`、`app/institute/digests.py:32-46`；但 `app/hands/`、`workflows/`、`catalog/` 以及实际 prompt 组装中均没有这些 URL 或 curl block。相关字符串只存在于 digest 模块自己的说明文字。

因此“端点基础已完成”成立，“CLI prompt 已消费”不成立。应把 ROADMAP 保持未完成/拆项，或将文字改为仅说明端点已就绪。

### M2（中）“memory 注入 every analyst prompt”与代码不符

- 误报位置：`CLAUDE.md:29`、`ROADMAP.md:116`。
- 已接入的四个自治域调用点：
  - `app/institute/analyst_daily.py:328-332`
  - `app/institute/whiteboard.py:693-699`
  - `app/institute/mailbox.py:168-172`
  - `app/institute/workflows.py:251-257`
- 仍调用 `build_analyst_prompt()` 但未传 `memory_block` 的路径：
  - `app/api/tasks.py:125-129`
  - `app/api/ask_stream.py:114-118`
  - `app/api/sessions.py:71-76`
  - `app/mcp.py:464-469`

四个计划内自治循环已接入，但不能写成“每一个 analyst prompt”。应收窄文案，或另行补齐其余入口后再保留现表述。

### L1（低）Phase 1a 勾选包含无法核验的完成条件

`ROADMAP.md:102` 勾选了 whiteboard similarity 整项，其中仍写有“约 50 个 known pairs 的一次性分布 sanity check”。仓库中有阈值矩阵、缓存、降级、diversity 和 rotation 测试，但没有该约 50 对样本的脚本、数据或报告。其余功能事实成立；该子条件应补证据或从完成定义中移除。

### L2（低）README 对 research 显式 hand 的说法过宽

`README.md:146` 写“explicit analyst hands always win”。实际 hard rule 10 要求 research 忽略 `analyst.hand`；`app/institute/workflows.py:183-195` 中 research 只允许显式 `step.hand` 优先，普通 workflow 才使用 `analyst_hand`。建议改成“显式 analyst hand 在 daily/whiteboard/mailbox 优先；research 的显式 step hand 优先”。

### L3（低）OFF 测试锁定的是 hand 选择，不是完整字节输出

`tests/test_weights_wiring.py:88-120` 确实覆盖 daily/whiteboard/mailbox/research 四个 scope 的 OFF 选择结果；静态 diff 也证明 C8 没改 prompt 字符串。但这些测试没有比较完整 prompt、任务行或输出字节，因此文件头 `:3-5` 的“byte-for-byte”说法比实际断言更强。该缺口不影响本次静态语义结论。

## 四点接线逐项

| Scope | OFF / 显式优先 | live pool 与 scope | 结论 |
|---|---|---|---|
| daily | `analyst.hand` 在 `app/institute/analyst_daily.py:264-265` 直接返回；OFF 继续原 rotation | `ROTATION_HANDS` 先经 `is_available`（`:266-271`），传 `"daily"` | PASS |
| whiteboard | 先取 `analyst.hand or default_hand`，仅 `enabled and not analyst.hand` 才加权（`app/institute/whiteboard.py:700-708`） | 正权重 scope 行先经 `is_available`，传 `"whiteboard"` | PASS |
| mailbox | 与 whiteboard 同型（`app/institute/mailbox.py:173-181`） | 正权重 scope 行先经 `is_available`，传 `"mailbox"` | PASS |
| research | OFF 保持原 round-robin；显式 `step.hand` 优先，按 hard rule 10 仍忽略 `analyst.hand` | `live` 直接由 `research_hand_names ∩ available` 构造（`app/institute/workflows.py:183-194`），传 `"research"`，返回的 fallback chain 不变 | PASS |

补充核验：

- `enable_hand_weights` 默认 `False`：`app/config.py:46-48`。
- research 链外高权重 hand 的回归测试：`tests/test_weights_wiring.py:190-208`。
- 四 scope 显式优先测试：`tests/test_weights_wiring.py:213-236`（research 使用显式 step hand，符合 rule 10）。
- whiteboard/mailbox 的候选集按本测试文件约定为“本 scope 正权重行 ∩ available”；该约定已被 `:151-187` 锁定。

## 文档事实抽查

### CLAUDE.md

| 抽查项 | 代码事实 | 结果 |
|---|---|---|
| roster mtime cache（`:66,74`） | `app/institute/analysts.py:38-52` 使用 `st_mtime_ns`，CRUD 仍 `reload()` | PASS |
| analyst_daily guard key（`:78`） | `app/institute/analyst_daily.py:66-112` 使用 `analyst_daily:<date>:<analyst_id>` 并合并 legacy blob | PASS |
| workflow key 规范化（`:43`） | `app/institute/workflows.py:39-62` canonical key 优先、legacy alias 折叠、未知 id 告警 | PASS |
| maintenance 语义（`:53`） | `app/api/meta.py:94-102` 有 POST；`app/institute/scheduler.py:118-178` 按是否提交模型调用分 gated/ungated | PASS |
| migration 纪律（`:42`） | `tests/test_db_migrate.py:23-32` 强制禁事务控制/ATTACH/VACUUM；`app/db.py` 逐语句单事务执行 | PASS |
| hand-weight Gotcha（`:79`） | 默认 off、boot 预热、GET/PUT 自愈、research 限链均与代码一致 | PASS |
| memory / digest 新模块 Map（`:29,31`） | 分别存在“every prompt”和“Step-0 已消费”的过度陈述 | **FAIL（M1/M2）** |

### ROADMAP.md

| Phase | 抽查项 | 结果 |
|---|---|---|
| 0 | research orphan recovery（`:78`）— `research.recover_orphans()` 且 lifespan 调用 | PASS |
| 0 | graceful shutdown（`:80`）— `main._drain_background()` 覆盖七组注册表并在关 DB 前执行 | PASS |
| 1a | sqlite-vec + bge-m3（`:100`）— 1024 维、runtime vec0、0007 metadata、archive indexing、FTS 降级、POST `/api/search` 均存在 | PASS |
| 1a | similarity + diversity（`:102`）— 主功能存在，但约 50 known-pairs sanity 无可核验产物 | PARTIAL（L1） |
| 2 | analyst memory（`:116`）— 表、23:30 gated job、managed region、四域注入存在；“every prompt”不成立 | PARTIAL（M2） |
| 2 | curl-back digests（`:118`）— 四端点存在，但 prompt Step-0 消费未接入 | **FAIL（M1）** |
| 2（额外核对） | hand weights + scorecard（`:119`）— 本次四点接线、00:05 前一日 scorecard、API/triage 数据均存在 | PASS |

### README.md

- `README.md:143` 的 maintenance 路径与 JSON body 与 `app/api/meta.py:94-102` 一致；`tests/test_maintenance.py:14-41` 做了真实 ASGI round-trip，PASS。
- gated/ungated 列表与 scheduler 代码一致。
- hand weights 段除 L2 的 research 显式 hand 措辞外，其余事实一致。

## conftest 逐行核对

`tests/conftest.py` 的累计 diff 仅：

1. 新增 `analyst_daily_mod`、`archive_mod` 两个导入；
2. teardown 新增 `analyst_daily._background`、`research._bg_tasks`、`archive._bg_tasks` 三组；
3. 新增“与 main 七组 registry 保持同步”的注释。

其集合与 `app/main.py:60-71` 一致；没有改变数据库、hand 或 fixture 的业务语义。PASS。

## 硬规则

- Prompt 逐字：C8 weighted-pick hunk 均位于既有 prompt 组装之后；未改四个域文件中的既有 prompt 字符串。累计 diff 中的 memory/similarity/data-bundle prompt 变化来自并行分区，不属于 C8 weighted 行。PASS。
- Config：C8 只新增 `enable_hand_weights: bool = False`（以及两行说明）；`app/config.py` 中其余新增字段来自并行分区。PASS。

## 验证

- `python -m compileall app -q`：PASS。
- `pytest tests/test_weights_wiring.py tests/test_maintenance.py -q`：**19 passed in 0.89s**（15 + 4）。
