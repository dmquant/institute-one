# PATCH-NOTES-E3 — 第五轮 prompt-change 专卡（${DATA_BUNDLE} 真注入 + digest Step-0 + ad-hoc ask memory）

> 本卡是 CLAUDE.md 硬规则 4 意义上的 **deliberate prompt-change card**——生产 prompt（`workflows/research.json`）的首次授权变更。
> 分区内落地：`workflows/research.json`、`app/api/tasks.py`、`app/api/sessions.py`、`app/mcp.py`（institute_ask）、
> `tests/test_workflows.py`（+2 例）、`tests/test_ask_memory.py`（新，7 例）。
> 验证：`compileall` exit 0；定向 16 例全绿；全量 **783 passed / 10 skipped 零失败**（进场基线 764/10，+9 为本卡，其余为并行卡）；
> 生产只读冒烟 `curl http://127.0.0.1:8100/api/institute/recent-reports.md?days=7` 返回稳定 markdown。未触碰 launchctl / git 写 / prompts.py。

## 1. ${DATA_BUNDLE} 真注入（ROUND4-AUDIT-S4 §4.2 Phase 1b 缺口）

机制自 B5 起就绪（`workflows.py:354-372` 惰性计算 + 持久化回 `workflow_runs.variables`），但生产 prompt 从未引用变量。
本卡按 PATCH-NOTES-B5 §3 的建议措辞接线，**只动 `01-company` 与 `03-financials` 的以下几处**：

- `03-financials`：任务正文前插入 `【本地行情数据】\n${DATA_BUNDLE}\n` 段（卡指令的字面措辞）；
  并把结尾「请使用联网搜索（如当前 CLI 支持）核实。」改为
  「**优先使用上方已注入的本地行情数据；数据缺失的部分再联网搜索核实。**」（即 ROADMAP 的 "replacing please web-search with grounded numbers"）。
- `01-company`：同位置插入**裸 `${DATA_BUNDLE}`**（不套标题——B5 §3 明示 bundle 渲染自带「【行情数据注入】…生成于 <日期>」头部，
  再套标题会双重标题；03 的「【本地行情数据】」是卡指令点名要的段头，空数据时段落只剩标题行，B5 判定可接受）。
  01 的联网搜索句（措辞为「获取并核实最新资料」）**不改**——卡指令只点名 03 的那句。
- `variables` 数组补登 `"DATA_BUNDLE"`（声明用途；引擎按 prompt 内容惰性计算，与该数组无关）。
- 插入位置在「研究基准日：…禁止偏题。」首行**之后**而非 prompt 最顶：首行是全部 7 步统一的研究对象锚定开场，
  数据块置于锚定之后、任务描述之前，prompt 仍以任务锚定开头（判断依据；B5 原文「开头」不排斥此解）。

空数据语义（既有机制，未改）：topic 无匹配证券/无 bar 时变量渲染空串，prompt 无痕降级；每次 run 实际注入的文本
持久化在 `workflow_runs.variables["DATA_BUNDLE"]`，`GET /api/data/{topic}/latest` 可预览。

### 1a. research.json 完整 diff（透明度要求；`\n` 为 JSON 源内转义形式）

```diff
   "variables": [
     "TOPIC",
     "WORK_DATE",
-    "ANALYST_CATALOG"
+    "ANALYST_CATALOG",
+    "DATA_BUNDLE"
   ],
```

`01-company`（插入 Step-0 段 + 裸变量段；其余逐字未动）：

```diff
-"prompt": "研究基准日：${WORK_DATE}。本任务唯一研究对象是 ${TOPIC}，禁止偏题。\n\n请完成「公司与业务概况」研究。…
+"prompt": "研究基准日：${WORK_DATE}。本任务唯一研究对象是 ${TOPIC}，禁止偏题。\n\n【研究前置】开始研究前，先执行 curl -s 'http://127.0.0.1:8100/api/institute/recent-reports.md?days=7' 了解研究所近 7 天已完成的工作，避免重复劳动；若命令失败则忽略，直接开始。\n\n${DATA_BUNDLE}\n\n请完成「公司与业务概况」研究。…
```

`02-industry` / `04-drivers-risks` / `05-thesis`（仅插入 Step-0 段；其余逐字未动）：

```diff
-"prompt": "研究基准日：${WORK_DATE}。本任务唯一研究对象是 ${TOPIC}，禁止偏题。\n\n<各步原任务正文>…
+"prompt": "研究基准日：${WORK_DATE}。本任务唯一研究对象是 ${TOPIC}，禁止偏题。\n\n【研究前置】开始研究前，先执行 curl -s 'http://127.0.0.1:8100/api/institute/recent-reports.md?days=7' 了解研究所近 7 天已完成的工作，避免重复劳动；若命令失败则忽略，直接开始。\n\n<各步原任务正文>…
```

`03-financials`（插入 Step-0 段 + 带标题数据段 + 改写搜索句；其余逐字未动）：

```diff
-"prompt": "研究基准日：${WORK_DATE}。本任务唯一研究对象是 ${TOPIC}，禁止偏题。\n\n请完成「财务与估值」研究：…
+"prompt": "研究基准日：${WORK_DATE}。本任务唯一研究对象是 ${TOPIC}，禁止偏题。\n\n【研究前置】开始研究前，先执行 curl -s 'http://127.0.0.1:8100/api/institute/recent-reports.md?days=7' 了解研究所近 7 天已完成的工作，避免重复劳动；若命令失败则忽略，直接开始。\n\n【本地行情数据】\n${DATA_BUNDLE}\n\n请完成「财务与估值」研究：…
 …
-…数据必须标注期间与来源，禁止编造数字；请使用联网搜索（如当前 CLI 支持）核实。把完整成果写入工作目录下的 03_财务估值.md。"
+…数据必须标注期间与来源，禁止编造数字；优先使用上方已注入的本地行情数据；数据缺失的部分再联网搜索核实。把完整成果写入工作目录下的 03_财务估值.md。"
```

`06-report` / `07-followups`：**逐字未动**。
（git diff 相对 HEAD 会额外显示 07 步 `"analyst"` → `"analyst_id"` 一处——那是先前轮次未提交的键归一化，非本卡改动。）

## 2. digest Step-0 块（ROUND4-AUDIT-S4 §4.2 Phase 2 缺口）

措辞定稿（5 个分析步逐字相同，便于审计与将来整体替换）：

```
【研究前置】开始研究前，先执行 curl -s 'http://127.0.0.1:8100/api/institute/recent-reports.md?days=7' 了解研究所近 7 天已完成的工作，避免重复劳动；若命令失败则忽略，直接开始。
```

措辞考量：`【研究前置】`段头沿用仓库 prompt 的`【…】`标记风格（时间锚点/引用规范/交付规范）；URL 带单引号防 zsh 把 `?` 当
glob；末句给出显式失败降级（digest 端点本身永远 200，但 CLI 沙箱可能禁 curl）；`127.0.0.1:8100` 硬编码与
`app/institute/digests.py` docstring 的既定约定一致（8100 由 launchd 管理，是固定生产端口）。

**加给哪些步（判断依据，卡指令要求写明）**：

- **判断口径一：hand 类型。** `_workflow_hand_policy`（`workflows.py:315-334`）对 `workflow_id == "research"` 完全忽略
  `analyst.hand`，在 `settings.research_hand_names`（生产默认 `codex,agy`）内轮转；`research.json` 没有任何步骤显式声明
  `hand` 键。因此 **7 步全部落在 CLI 手上，不存在 api 型手的步骤**——「显式 api hand 则不加」的排除条款无一命中。
- **判断口径二：步骤性质。** 只加给 `01-company`–`05-thesis` 五个**分析步**（做研究判断，需要知道研究所近期已研究过什么来避免
  重复劳动）。`06-report` 是汇编步（prompt 明令「不得新增事实」，唯一输入是工作目录的 01–05 文件，curl 外部上下文与其任务矛盾）；
  `07-followups` 是跟进编排步（输入为 06 报告，输出受严格 JSON 结构约束，且 recent-reports digest 不含白板议题，
  对「避免重复跟进项」帮助有限、噪音风险高）。**06/07 不加。**

briefing/daily/committee 等其他工作流不在本卡授权范围，未动。

## 3. ad-hoc ask 的 memory 注入（PATCH-NOTES-B3 §2e 三处落地）

模式与四个工作流注入点逐字一致（`memory_block=await memory.memory_block(analyst.id)`；空记忆返回 `""`，
`build_analyst_prompt` 对空块是严格 no-op——无记忆分析师的 prompt 与改动前逐字节相同）。

### 3a. `app/api/tasks.py`（POST /api/ask）

```diff
 from ..config import get_settings
+from ..institute import memory
 from ..institute.analysts import get_analyst
 …
-        prompt = build_analyst_prompt(analyst, body.prompt)
+        prompt = build_analyst_prompt(
+            analyst, body.prompt,
+            memory_block=await memory.memory_block(analyst.id),
+        )
```

### 3b. `app/api/sessions.py`（POST /api/sessions/{id}/messages）

```diff
-from ..institute import sessions
+from ..institute import memory, sessions
 …
-            prompt = build_analyst_prompt(analyst, body.content)
+            prompt = build_analyst_prompt(
+                analyst, body.content,
+                memory_block=await memory.memory_block(analyst.id),
+            )
```

### 3c. `app/mcp.py`（institute_ask 工具）

```diff
 async def _t_institute_ask(args: dict) -> Any:
-    from .institute.analysts import get_analyst  # lazy: domain modules
+    from .institute import memory  # lazy: domain modules
+    from .institute.analysts import get_analyst
     from .institute.prompts import build_analyst_prompt
 …
-        prompt = build_analyst_prompt(analyst, prompt)
+        prompt = build_analyst_prompt(
+            analyst, prompt,
+            memory_block=await memory.memory_block(analyst.id),
+        )
```

### 3d. ask_stream 镜像验证结论（卡指令要求「验证之」）

**未自动生效。** `app/api/ask_stream.py::_prepare` 仍是 `/api/ask` 预处理的独立逐行镜像——B8 提议的共享 `prepare_ask`
helper 从未被抽取（PATCH-NOTES-B8「建议主代理集成」段至今未落地），因此流式端点**不会**随本卡获得 memory 注入。
`ask_stream.py` 不在本卡分区，未动。两点缓解：
① `tests/test_digests.py::test_ask_stream_analyst_id_parity_with_sync_ask` 用的是无记忆分析师（空块 no-op），
sync/stream 的 prompt 仍逐字节一致，全量通过（783 例含它）；
② 遗留与 S4-P1-07/S4-P1-08 合并处理——主代理抽 `prepare_ask`（改为 async）时把 memory 注入放进 helper，流式即自动同步。
在此之前 **stream ask 有意保持无记忆**，`tests/test_ask_memory.py` 模块 docstring 已写明该边界。

## 4. 测试（本卡 +9 例；全量 783 passed / 10 skipped 零失败）

`tests/test_workflows.py`（+2）：

- `test_research_definition_carries_step0_and_data_bundle` — reconcile 后逐步骤断言：Step-0 行逐字节出现在 01–05、
  不在 06/07；`${DATA_BUNDLE}` 只在 01（裸）与 03（带「【本地行情数据】」标题）；03 的搜索句已替换、旧句不存在；
  variables 数组为四元组。
- `test_research_run_renders_bundle_and_step0_end_to_end` — seed 茅台（securities + 5 根 PIT bar）后 echo 跑**生产
  research 工作流全 7 步**：run completed；惰性计算的 bundle 持久化在 `run.variables["DATA_BUNDLE"]`（含 600519.SH）；
  对每步 tasks 行的实际 prompt 断言变量零残留，且按「## 任务」节切分后（echo 回显会把前序 prompt 带进后续步骤的
  前序块，故对任务节断言）：Step-0 行在 01–05、不在 06/07；bundle 正文（600519.SH/最新日线）恰好到达 01/03 两步，
  其余步骤不含。

`tests/test_ask_memory.py`（新，7 例）：

- `/api/ask`：有记忆分析师 → prompt 含「## 常备记忆（第 1 版 · …）」与记忆正文，且顺序为 persona < memory < ## 任务
  （三明治位置）；无记忆分析师 → prompt 无「常备记忆」；无 analyst_id → prompt 保持裸文不包装（记忆行在库也不咨询）。
- session chat：同上两向（经 POST /api/sessions/{id}/messages，断言 task.prompt）。
- MCP institute_ask：JSON-RPC 往返，output 与持久化 tasks 行的 prompt 均含记忆块；无记忆分析师干净。

## 5. 回滚说明（生产效果回退时恢复旧 prompt）

代码三处（tasks/sessions/mcp）回滚 = 撤掉 §3a-3c 的 diff（把 `build_analyst_prompt` 调用还原为单参形式并移除
`memory` import）。`research.json` 的**精确反向操作**（9 处查找替换，字符串按 JSON 源内转义形式；注：03 的「【本地行情数据】」段头已按 ROUND5-AUDIT-F5 NIT-F5-1 改为裸变量——bundle 自带头部，空数据无痕——回滚时该处按裸变量形态查找）：

1. variables 数组：`"ANALYST_CATALOG",\n    "DATA_BUNDLE"` → `"ANALYST_CATALOG"`。
2. 01/02/03/04/05 五步各删一段（5 处）：
   `\n\n【研究前置】开始研究前，先执行 curl -s 'http://127.0.0.1:8100/api/institute/recent-reports.md?days=7' 了解研究所近 7 天已完成的工作，避免重复劳动；若命令失败则忽略，直接开始。` → 空串。
3. 01 步：`禁止偏题。\n\n${DATA_BUNDLE}\n\n请完成「公司与业务概况」` → `禁止偏题。\n\n请完成「公司与业务概况」`。
4. 03 步：`禁止偏题。\n\n【本地行情数据】\n${DATA_BUNDLE}\n\n请完成「财务与估值」` → `禁止偏题。\n\n请完成「财务与估值」`。
5. 03 步：`优先使用上方已注入的本地行情数据；数据缺失的部分再联网搜索核实。` → `请使用联网搜索（如当前 CLI 支持）核实。`。

回滚后 `reconcile_from_disk()`（重启或手动触发）即恢复 DB 内定义；`tests/test_workflows.py` 新增 2 例需一并删除
（它们锁定新措辞）；`tests/test_ask_memory.py` 与代码三处同进退。注意**不要**用 `git checkout HEAD -- workflows/research.json`
整文件回滚——那会连带撤销 07 步先前轮次的 `analyst_id` 键归一化（非本卡工作；虽然 reconcile 对 legacy key 有容忍，
但不应无谓回退）。

## 6. 分区外遗留（主代理）

- `roadmap/backlog.json`：Phase 1b「research data injection」与 Phase 2「digest→Step-0」「ad-hoc ask memory」的状态
  推进（B5 §5 同款约定：状态由主代理管）。
- ask_stream 的 memory 注入：随 B8 `prepare_ask` 抽取（需改 async）一并落地，见 §3d。
- `06-report`/`07-followups` 及其他工作流（briefing/daily/committee）是否也要 Step-0 / 数据注入：本卡按最小授权
  未动，若要扩面须另开 prompt-change 卡。
