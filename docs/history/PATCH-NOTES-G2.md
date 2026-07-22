# PATCH-NOTES-G2 — M8-005（统一记忆注入入口 + compact 跨进程认领）分区外收尾清单

G2 交付物（已落盘，独占分区内）：

- `app/institute/memory.py` —
  1. **统一入口** `prompt_with_memory(analyst, task_text, **kwargs) -> str`：`memory_block` + `build_analyst_prompt` 一处组装（kwargs 透传 `context_blocks`/`output_file`；`memory_block` 由入口供给，调用方再传会 TypeError——这正是想要的约束）。**文档化决策（卡 acceptance #1）**：所有分析师 persona prompt（四工作流点 + ad-hoc ask/sessions/MCP 三点，共七点）一律经 `memory.prompt_with_memory` 组装；`prompts.build_analyst_prompt` 退为无记忆的底层原语（memory.py 自身的 compact prompt 仍直用它——压缩任务的记忆走 context_blocks 前置块，不走注入参数）。`prompts.py` 零字节未动。
  2. **compact 跨进程认领（卡 acceptance #3）**：`compact_one` 在**任何模型调用之前**对 `admin_state` 行 `memory_compact:<analyst_id>` 条件认领（`INSERT ... ON CONFLICT DO NOTHING` rowcount 定胜负——照 `analyst_daily._claim_sweep` 先例）；输家直接 skip（`{"skipped": "compact already running"}`），不收集素材不烧模型。硬死进程的 claim 过 `COMPACT_LEASE_S = 45min`（> 默认 30min hand timeout）后被 CAS takeover（future/corrupt claimed_at 视为 stale）；无 heartbeat——单次 compact 是有界的一次模型调用，与 sweep 的无界 roster 循环不同。`finally` CAS DELETE 只释放自己的 token。原 `UNIQUE(analyst_id, version)` INSERT OR IGNORE 保留为最后防线（takeover 后僵尸醒来也写不进双版本）。`bus.now_iso()` 全程沿用。
- `app/institute/analyst_daily.py` / `whiteboard.py` / `workflows.py` / `mailbox.py` — 四个工作流注入点机械替换为 `await memory.prompt_with_memory(...)`（行为逐字节不变，`test_prompt_with_memory_matches_manual_assembly` 锁死等价性）；各自被孤儿化的 `build_analyst_prompt` import 已清理。
- `tests/test_memory.py` — 新增 5 测：入口逐字节等价（5 种 kwargs 形态 × 有/无记忆，`now_sgt` 钉死防分钟翻转）；并发 compact 单烧（`calls == 1`，模型调用计数）；live claim 挡路（零模型调用、不动别人的锁）+ stale takeover；corrupt claim takeover；僵尸双写被 UNIQUE 兜底。原有 10 测不动全绿。

## 1. 主代理需要做的事（G2 无权修改的文件）

三个 ad-hoc 点做与四工作流点相同的机械替换（行为逐字节不变，`tests/test_ask_memory.py` 8 测已锁定注入行为，替换后应原样全绿）。**用代码片段定位，别信行号**（tasks.py 被 G1 并行改动中）。

### 1.1 `app/api/tasks.py`（`prepare_ask` 内）

```python
# 现状
        prompt = build_analyst_prompt(
            analyst, body.prompt,
            memory_block=await memory.memory_block(analyst.id),
        )
# 替换为
        prompt = await memory.prompt_with_memory(analyst, body.prompt)
```

顶部 `from ..institute.prompts import build_analyst_prompt` 若因此孤儿化则删除（`from ..institute import memory` 保留）。`prepare_ask` docstring 里的 "Persona wrap via ``build_analyst_prompt``" 可顺手改为 "via ``memory.prompt_with_memory``"。

### 1.2 `app/api/sessions.py`（`post_message` 内）

```python
# 现状
            prompt = build_analyst_prompt(
                analyst, body.content,
                memory_block=await memory.memory_block(analyst.id),
            )
# 替换为
            prompt = await memory.prompt_with_memory(analyst, body.content)
```

顶部 `from ..institute.prompts import build_analyst_prompt` 孤儿化则删除。

### 1.3 `app/mcp.py`（`_t_institute_ask` 内）

```python
# 现状
        prompt = build_analyst_prompt(
            analyst, prompt,
            memory_block=await memory.memory_block(analyst.id),
        )
# 替换为
        prompt = await memory.prompt_with_memory(analyst, prompt)
```

函数内 lazy import 行 `from .institute.prompts import build_analyst_prompt` 孤儿化则删除（`from .institute import memory` 保留）。

替换后验证：`.venv/bin/python -m pytest tests/test_ask_memory.py tests/test_memory.py -q` 全绿即收尾完成。

## 2. 运维须知（compact claim）

| 场景 | 行为 |
|---|---|
| 两进程同时 compact 同一分析师 | 一胜一 skip（skip 方零模型调用）；配额单烧 |
| compact 进程硬死（finally 未跑） | claim 行 45min 后过期，下一次 compact CAS takeover |
| 想立即解锁 | 删 `admin_state` 行 `memory_compact:<analyst_id>` |
| takeover 后僵尸醒来写版本 | `UNIQUE(analyst_id, version)` 兜底，输出丢弃、素材相对赢家 cursors 重新消费 |

无 heartbeat 的前提：compact 的模型调用超时 ≤ 默认 30min（`executor.submit` 未传 `timeout_s`，用 `settings.default_timeout_s`）。若将来把 default_timeout_s 调到 45min 以上，需同步调大 `memory.COMPACT_LEASE_S` 或补 heartbeat。

## 3. 测试结果

- 定向：`tests/test_memory.py` **15 passed**（10 旧 + 5 新）；受影响域（memory/ask_memory/analyst_daily/whiteboard/mailbox/workflows）**58 passed**。
- 全量：**847 passed / 4 skipped，零失败**（交接基线 825/4；增量含 G2 的 5 测与并行卡测试）。
- `python -m compileall app` 通过。`prompts.py` 零字节未动；8100/launchd 未触碰。
