# REVIEW-B7 — M7-006 / M7-007 第二轮独立审查

## 结论

**PASS-WITH-NITS**

两张卡的主路径与 acceptance 均已实现，指定 Python 测试、编译检查和插件构建全部通过。未发现需要判定 FAIL 的功能错误；有两项 Minor 风险和两项建议，主要涉及确定性排序的形式化边界及测试对活 seed 的残余依赖。

## 审查边界

仅审查 B7 声明的增量：

- `app/institute/roadmap.py`：`generate_agent_prompt()`、`process_overview()` 及其直接依赖；
- `app/api/roadmap.py`：`GET /cards/{id}/prompt`、`GET /process`；
- `tests/test_roadmap.py`：M7-006/M7-007 新测试，以及当前 pinned 临时卡写法；
- `obsidian-plugin/src/api.ts`、`obsidian-plugin/src/roadmap.ts` 与构建产物 `obsidian-plugin/main.js`。

`app/institute/roadmap.py` 中 A6 的第一轮 CRUD/decision/session 等增量，以及工作树中其他代理的改动，均未纳入本结论。

## 问题分级

### Minor-1：checklist 排序不是严格全序，确定性保证仍有同序值边界

- 位置：`app/institute/roadmap.py:499-505`，使用 `ORDER BY kind, sort_order` 读取 checklist；prompt 在 `app/institute/roadmap.py:689-693` 按该结果生成。
- 影响：正常 seed 和默认追加路径会生成递增 `sort_order`，当前结果稳定；但 API 允许多个 acceptance item 使用相同 `sort_order`。SQL 对并列项未声明次级排序键，跨重建、查询计划变化或不同 SQLite 版本时，不能形式化保证字节序完全一致。
- 建议：改为 `ORDER BY kind, sort_order, id`（或稳定的 `rowid`），并增加两个相同 `sort_order` acceptance item 的回归测试。
- 判定：非当前主路径阻断项，但这是 M7-007 “deterministic” 声明应补齐的边界。

### Minor-2：新 process 测试仍依赖活 seed 中 `M3-001` 未完成

- 位置：`tests/test_roadmap.py:936-941`。
- 影响：测试通过 `M3-001` 制造 open dependency；当路线图以后把 `M3-001` 标为 `done`，该断言会再次随 seed 漂移而失败，与本轮把既有测试改成 pinned 临时卡的目的相悖。
- 建议：在临时 seed 中再加入一个固定为 `ready`/`inbox` 的卡，例如 `M7-TMPP3`，让 `M7-TMPP2` 依赖它。
- 判定：仅为未来测试稳定性风险，不影响当前实现正确性。

### Nit-1：协议中的“当前 git status 摘要”未进入生成 prompt

- 位置：`roadmap/06-agent-protocol.md:18` 对 agent input 的要求；当前 prompt 组装见 `app/institute/roadmap.py:694-713`。
- 说明：M7-007 acceptance 未要求这一项，因此不判失败。直接读取实时 git status 也会把非持久化环境状态混入确定性 prompt。
- 建议：把确定性 card prompt 定义为“持久化核心”，由会话启动层另附 git status；或只加入静态约束“开始前检查 git status”，不要在该函数中读取工作树。

### Nit-2：API 可用但 prompt 子接口失败时，复制没有本地兜底

- 位置：`obsidian-plugin/src/roadmap.ts:512-527`。
- 说明：完整离线模式会复制本地模板，满足离线降级；但若卡片列表接口可用、prompt 接口因混合版本或瞬时错误失败，面板仍显示本地模板，复制按钮却直接返回。
- 建议：可在 Notice 明示后允许用户复制当前本地预览。当前后端与插件同版本时不影响 acceptance。

## M7-007 acceptance 核验

### 1. Prompt 要素完整

**PASS**

`app/institute/roadmap.py:694-713` 包含：

- card id 与 title；
- phase、type、priority、risk；
- summary/problem（有值时）；
- design links；
- expected files；
- dependencies 及实时状态；
- acceptance criteria；
- verification commands；
- constraints；
- operator 写入的 `agent_prompt` notes（有值时）。

约束常量位于 `app/institute/roadmap.py:658-666`，覆盖：

- 先读 `CLAUDE.md`，遵循单执行路径、conditional claim、`bus.now_iso()`；
- 变更限定在卡片范围；
- migration 只增不改旧文件；
- 行为变化配套测试并运行 verification；
- 不 push、不引入 hosted infrastructure、保留无关用户改动；
- 扩展工作另建 roadmap card。

内容与 `CLAUDE.md`、`roadmap/06-agent-protocol.md` 一致且合理。

### 2. Operator 可复制生成 prompt

**PASS**

- API client 使用 `cardPrompt()` 请求后端生成内容：`obsidian-plugin/src/api.ts:636-642`；
- 卡片详情提供预览及“复制 Agent Prompt”按钮：`obsidian-plugin/src/roadmap.ts:500-510`；
- 使用 `navigator.clipboard.writeText()`：`obsidian-plugin/src/roadmap.ts:528-534`。该方式在 Obsidian Desktop/Electron 中可用，异常也会显示 Notice；
- 完整离线态使用 bundled seed 的本地模板：`obsidian-plugin/src/roadmap.ts:524-526`。

### 3. 同一卡状态下确定性

**PASS-WITH-NIT**

- 模板没有时间戳、随机数、生成 id 或无序 dict 迭代；
- 常量顺序固定；
- design links、expected files、verification 来自有序 JSON array；
- dependencies 查询显式 `ORDER BY d.depends_on_id`：`app/institute/roadmap.py:502-505`；
- acceptance 查询显式按 `kind, sort_order` 排序，但同 `sort_order` 缺少稳定 tie-breaker，见 Minor-1；
- `sort_order`、`updated_at`、evidence、session 等非 prompt 字段不会泄漏进文本；
- 测试验证了连续调用字节相同、时间戳相关更新不影响文本、真实卡片内容变化会改变文本：`tests/test_roadmap.py:821-858`。

### 4. 未知卡

**PASS**

领域函数返回 `None`，API 在 `app/api/roadmap.py:168-174` 映射为 404；HTTP 测试覆盖于 `tests/test_roadmap.py:1049-1058`。

## “依赖状态是否破坏确定性”的解读

当前 prompt 会输出例如 `M7-001 (done)`，依赖卡状态改变后 prompt 也会改变。

我的解读是：**这不应视为确定性违规**。M7-007 的有效输入状态不是单独一行 `roadmap_cards`，而是 card aggregate：

- card 字段；
- acceptance checklist；
- dependency edge；
- dependency target 的当前状态。

`roadmap/06-agent-protocol.md:64-66` 明确要求依赖未完成时 agent 停止或先处理依赖，因此实时状态是影响执行决策的必要输入。依赖状态变化意味着 prompt 输入闭包已经变化；对于同一个完整输入快照，输出仍应相同。

建议把 acceptance/函数注释中的 “same card state” 明确为 “same prompt input state, including dependency statuses”，并补一条测试：依赖状态不变时字节一致，依赖状态改变时只允许对应 dependency 行发生变化。如果产品真正要求“仅 card 自身行不变就字节不变”，则应移除实时状态，但这会降低 prompt 的操作安全性，不建议这样解释。

## M7-006 acceptance 核验

### 1. Roadmap view 暴露 sessions、decisions、release gates

**PASS**

`process_overview()` 返回 `active_sessions`、`open_decisions`、`release_gates`、`blocked_cards` 四个集合；API 路由位于 `app/api/roadmap.py:365-370`。插件“流程”页签分别渲染活动会话、开放决策和 release gates：`obsidian-plugin/src/roadmap.ts:638-740`。

### 2. Release readiness 由 card status 与 evidence 共同计算

**PASS**

- 仅 `status = 'pass'` 的 evidence 进入 `evidence_pass`：`app/institute/roadmap.py:1218-1222`；
- `evidence_ready` 按具有至少一条 pass evidence 的 scoped card 去重计数；
- `ready` 仅在 scope 非空、全部 card 为 done、全部 card 都有 pass evidence 时为真：`app/institute/roadmap.py:1253-1272`；
- fail/info/override 不会被误算为 pass；
- 测试覆盖 pass 计数、fail 不计数，以及全 done 但无 evidence 仍不 ready：`tests/test_roadmap.py:890-927`。

### 3. 阻塞项无需逐卡打开即可见

**PASS**

- `blocked_reason` 非空或存在未完成依赖即进入 `blocked_cards`；
- done card 明确排除；
- 返回 blocker 原因与 `open_dependencies`；
- 插件直接展示两类原因并可跳回对应看板卡：`obsidian-plugin/src/roadmap.ts:742-774`；
- 测试覆盖 operator blocker、已完成依赖不阻塞、done 排除、未完成依赖阻塞。

### SQL/循环效率

**PASS**

`process_overview()` 使用固定 5 次数据库查询：

1. cards；
2. open dependencies；
3. pass evidence card ids；
4. active sessions；
5. open decisions。

没有逐卡查询的 Python N+1。session command 数量使用同一 SQL 内的相关子查询，且 `roadmap_session_commands(session_id, created_at)` 已有索引。Python 端为固定 3 个 release gate 对 cards 的线性扫描，复杂度约为 `O(N)`。

### RELEASE_GATES 覆盖

**PASS**

- Release A：M0、M1、M2、M3；
- Release B：M4、M5、M6；
- Release C：M7。

M0-M7 全覆盖且无重复遗漏。`_phase_token()` 取精确首 token，可避免 M1 错配 M10；当前 backlog phase 格式均符合要求。插件离线常量与后端一致。

## 插件核验

- **requestUrl：PASS。** `obsidian-plugin/src/api.ts:418-459` 的统一 request 方法使用 Obsidian `requestUrl`；对新增源码执行 `rg "fetch\\s*\\("` 无匹配。
- **离线降级：PASS。** 后端不可达时回到 bundled `roadmap/backlog.json`；流程页仍本地计算 gates 与 blocked cards，并明确提示无 sessions/decisions/evidence：`obsidian-plugin/src/roadmap.ts:647-656`。
- **页签切换：PASS。** 看板与流程仅切换容器 display，不销毁筛选、selected id 或 Kanban 数据；从流程项点击卡片会切回看板并选中该卡：`obsidian-plugin/src/roadmap.ts:257-293, 762-774`。
- **现有 Kanban：PASS。** 原筛选、拖动/移动、详情、旧 release gate、导出逻辑仍在 board 分支执行；远端 move 后 `reload()` 会同步刷新 process 聚合。
- **clipboard：PASS。** Obsidian Desktop 支持 `navigator.clipboard.writeText()`，调用发生在按钮路径并有错误 Notice；混合版本兜底见 Nit-2。
- **main.js：PASS。** 构建成功，产物包含 `requestUrl`、prompt/process API 与 clipboard 逻辑；构建前后 `main.js` diff 规模均为 `+319/-10`，未产生额外漂移。
- `src/main.ts` 无需改动：roadmap view 已注册，本轮只在既有 view 内增加子页签。

## 既有测试改动核验

把依赖活 seed 状态的 move/review gate 场景改为自建临时卡是合理修复：

- 每个场景固定初始状态与依赖关系；
- 仍验证原有的拒绝路径、override、conditional claim、session summary、evidence gate 和事件 payload；
- 没有通过删除断言来迁就新 seed 状态，部分场景反而更自包含。

需要修正的唯一残余是新 process 测试仍借用 `M3-001` 作为未完成依赖，见 Minor-2。

## 硬规则核验

- B7 的 prompt/process 领域增量为只读，不新增时间写入；本文件相关写路径仍统一使用 `bus.now_iso()`。
- B7 范围没有 migration 变更。
- B7 没有改写既有 `app/institute/prompts.py` 或 workflow prompt；`_PROMPT_CONSTRAINTS` 是 roadmap 模块的新常量。工作树中 `prompts.py` 的其他代理改动不计入本审查。
- `obsidian-plugin/main.js` 是预期提交的构建产物。
- `git diff --check` 无空白错误；IDE diagnostics 无新增错误。

## 指定验证

全部通过：

```text
.venv/bin/python -m compileall app -q
→ exit 0

.venv/bin/python -m pytest tests/test_roadmap.py -q
→ 24 passed in 1.22s

cd obsidian-plugin && npm run build
→ tsc -noEmit -skipLibCheck && node esbuild.config.mjs production
→ exit 0
```

`npm` 仅提示环境中的旧 `devdir` 配置将在未来 major 版本停止支持，与本轮代码无关。
