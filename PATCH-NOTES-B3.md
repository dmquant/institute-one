# PATCH-NOTES-B3 — Analyst memory 分区外集成需求

B3 分区（Analyst memory：`migrations/0010_analyst_memory.sql`、`app/institute/memory.py`、
`app/institute/prompts.py`、`app/vault/writer.py`、`app/vault/exporter.py`、
`tests/test_memory.py`、`tests/test_vault.py`）已自包含落地并测试通过。
以下两类改动落在其他代理/主代理的分区，按精确行给出。集成前 memory 功能已可用
（API/手动调用 `memory.compact_one` 即可产出版本行并触发 vault 导出），只是
不会自动定时跑、分析师 prompt 也还看不到记忆块。

## 1. 调度挂载（B1 分区：`app/institute/scheduler.py` + `app/config.py`）

### 1a. `app/config.py` — Scheduler 配置区新增一行

放在 `analyst_daily_time` 之后（保持 cron 时间集中）：

```python
    memory_compact_time: str = "23:30"  # 常备记忆压缩（analyst memory nightly compact）
```

### 1b. `app/institute/scheduler.py` — 新增 job + 挂载

job 定义追加到 `_research_tick_job` 之后（与其他 gated job 并列；
`gated=True` 因为 compact 提交新模型调用，须尊重 maintenance 暂停）：

```python
@metered("memory-compact", gated=True)
async def _memory_compact_job() -> None:
    from . import memory
    await memory.compact_all()
```

`start()` 里追加挂载（放在 `cron(_analyst_dailies_job, …)` 之后）：

```python
    cron(_memory_compact_job, "memory-compact", settings.memory_compact_time)
```

说明：23:30 SGT 在 `daily_time`（23:00 每日日报）之后，当天产出基本收敛后再压缩。
`compact_all()` 自身串行遍历、单个失败不断链、无新材料的分析师直接跳过（零模型调用），
天然幂等，重复触发安全。

## 2. memory_block 注入调用点（各文件归属其他代理或主代理集成）

`prompts.build_analyst_prompt` 已新增可选参数 `memory_block: str | None = None`
（插在 persona 与 context/task 之间；空串/None 时输出与旧版逐字节相同）。
`memory.memory_block(analyst_id)` 是 async 函数，空记忆返回 `""`，因此各调用点
可以无条件注入。每处改动 = 文件头部 import + 组装调用加一个关键字参数。

### 2a. `app/institute/analyst_daily.py`（run_one，现约 189 行）

import 区（`from .prompts import build_analyst_prompt, work_date` 之后）加：

```python
from . import memory
```

组装行由：

```python
    prompt = build_analyst_prompt(analyst, _daily_task(analyst, filename), output_file=filename)
```

改为：

```python
    prompt = build_analyst_prompt(
        analyst, _daily_task(analyst, filename), output_file=filename,
        memory_block=await memory.memory_block(analyst.id),
    )
```

### 2b. `app/institute/whiteboard.py`（_run_card，现约 635 行）

**注意（R-B3 B3-M4 修订）**：whiteboard 已被另一分区改造——`_run_card` 现在自行构造
`context_blocks` 列表并可能前插 BUILD-ON prior block，组装调用是
`context_blocks=context_blocks or None`。**保持该表达式不变**（否则丢 BUILD-ON 块），
只追加 `memory_block` 关键字参数。

import 区（`from .analysts import get_analyst, roster` 之后）加：

```python
from . import memory
```

组装调用由：

```python
        prompt = build_analyst_prompt(
            analyst, task_text,
            context_blocks=context_blocks or None,
            output_file=output_file,
        )
```

改为：

```python
        prompt = build_analyst_prompt(
            analyst, task_text,
            context_blocks=context_blocks or None,
            output_file=output_file,
            memory_block=await memory.memory_block(analyst.id),
        )
```

（`_handoff` 的主持人 prompt 是手搓的非分析师 prompt，**不**注入。）

### 2c. `app/institute/mailbox.py`（_run_dispatch，现约 167 行）

import 区（`from .prompts import build_analyst_prompt` 之后）加：

```python
from . import memory
```

组装调用由：

```python
        prompt = build_analyst_prompt(
            analyst, task_text, context_blocks=[context] if context else None
        )
```

改为：

```python
        prompt = build_analyst_prompt(
            analyst, task_text, context_blocks=[context] if context else None,
            memory_block=await memory.memory_block(analyst.id),
        )
```

### 2d. `app/institute/workflows.py`（_drive_run 步骤组装，现约 225 行）

import 区加（模块顶部已 `from .analysts import get_analyst`，与其并列）：

```python
from . import memory
```

组装调用由：

```python
            full_prompt = build_analyst_prompt(
                analyst, prompt,
                context_blocks=[previous_steps_block(prior)],
                output_file=step.get("output_file"),
            )
```

改为：

```python
            full_prompt = build_analyst_prompt(
                analyst, prompt,
                context_blocks=[previous_steps_block(prior)],
                output_file=step.get("output_file"),
                memory_block=await memory.memory_block(analyst.id),
            )
```

### 2e. 可选低优先（ad-hoc 单发问答，ROADMAP 未点名，可暂缓）

- `app/api/tasks.py` ~128 行、`app/api/sessions.py` ~74 行、`app/mcp.py` ~420 行
  的 `build_analyst_prompt(analyst, …)` 同样可加 `memory_block=await memory.memory_block(analyst.id)`。
  这三处是操作员即时问答，注入与否不影响飞轮闭环；若求一致可一并加上。

### 循环依赖说明

`memory.py` 只 import `analysts` / `prompts` / `executor` / `bus` / `db` / `config`，
不 import `analyst_daily` / `whiteboard` / `mailbox` / `workflows`，因此上述四处
`from . import memory`（模块顶部）不会成环。若集成代理偏保守，也可以按仓库
惯例改为函数内 lazy import（`from . import memory  # lazy: domain peer`）。

## 3. 行为提示（给集成后的验收）

- 注入格式（`memory.MEMORY_BLOCK_TEMPLATE`，逐字稳定）：

  ```
  ## 常备记忆（第 {version} 版 · {work_date}）
  （这是你此前工作的压缩记忆，可作为判断起点；若与最新事实冲突，以最新事实为准，并在下次压缩时修正。）

  {compact_md}
  ```

- 无记忆分析师注入空串 → prompt 与集成前逐字节一致（`tests/test_memory.py::test_prompt_injection_between_persona_and_task` 已断言）。
- compact 产出的 vault 投影：`Analysts/<id>/memory.md`，managed-region 语义
  （`%% institute:begin/end %%` 内为机构区，区外人工批注重写后存活）。
- 事件：每次成功 compact 发 `memory.compacted`（ref=analyst_id，payload 含
  version/work_date/memory_id/task_id），exporter 已挂 handler。
