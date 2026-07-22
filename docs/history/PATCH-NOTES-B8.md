# PATCH-NOTES-B8 — Curl-back digests + streaming ask 分区外改动清单

B8 交付物（已落盘，独占分区内）：

- `app/institute/digests.py` — 只读域模块：近 N 天报告清单、analyst 记忆最新版、两个后期占位（disputes/operator-actions）的 markdown 渲染；统一 8KB 字节级 clamp（UTF-8 边界 + `> [digest truncated at 8KB]` 标记）。**绝不写库**；`analyst_memory` 表缺失/列不符时捕获 `sqlite3.OperationalError` 降级为 `# no memory yet` 占位（该表归并行的 analyst-memory 卡）。
- `app/api/digests.py` — `GET /api/institute/{recent-reports, analyst-memory/:id, analyst-disputes/:id, operator-actions-digest}.md`，全部 `text/markdown; charset=utf-8`（PlainTextResponse），永远 200 + markdown（未知 analyst / 空表 / 后期表未建 → 稳定占位，绝不把错误页喂进 prompt）。
- `app/api/ask_stream.py` — `POST /api/ask/stream`（NDJSON）：body **就是** `tasks.AskBody`（import 复用，单一请求契约：prompt/analyst_id/hand/model/timeout_s），预处理与 `/api/ask` 逐行对齐（`_prepare()`：persona 包装 `build_analyst_prompt`、未知 analyst 流开始前 404、hand 优先级 body > analyst > default）。`executor.submit` 的 `on_chunk` 桥接到**有界队列**（`_ChunkBridge`，maxsize=1000，满时丢最旧保最新并计数，done 帧前发一条 `{"type":"status","text":"…N chunks dropped"}`）逐行下发 `{"type":"stdout|stderr|status","text":...}`，终帧 `{"type":"done","task":{id,status,hand,exit_code,error,output≤8KB}}`。**断开语义**：客户端断开只杀响应生成器，生成器退出（finally）把桥置 closed——后续 on_chunk 直接短路不再入队、已缓冲项清空；submit 任务照常跑完入库（fire-and-forget，与 /api/ask 的同步等待相反；docstring 已写明）。
- `tests/test_digests.py` — 14 个测试：四端点形状（markdown、非 JSON、占位路径、8KB 截断、days 窗口/钳制）、`analyst_memory` 缺表/空表降级、echo 手 chunk 序列 + done 帧、未知手 done 帧降级、**analyst_id 对等性**（stream 与 sync ask 同 body → 同 persona prompt/同任务字段；未知 analyst 双 404）、**有界队列**（桥单元测试 + 小队列洪峰下丢最旧+status 告知帧）、断开不取消任务且桥关闭停止积压。测试用裸 FastAPI app 挂 router（同 test_market_data.py 做法），不依赖 main.py；对等性测试同时挂 `tasks.router` 与 `ask_stream.router`。

## 需要主代理执行的挂载（app/main.py，B8 无权修改）

`create_app()` 里的 `from .api import (...)` 块加两行（按字母序）：

```python
    from .api import (
        analysts as api_analysts,
        archive as api_archive,
        ask_stream as api_ask_stream,
        digests as api_digests,
        events as api_events,
        hands as api_hands,
        mailbox as api_mailbox,
        market_data as api_market_data,
        meta as api_meta,
        research as api_research,
        roadmap as api_roadmap,
        sessions as api_sessions,
        tasks as api_tasks,
        theses as api_theses,
        vault as api_vault,
        whiteboard as api_whiteboard,
        workflows as api_workflows,
    )
```

`include_router` 循环的元组里加 `api_ask_stream.router` 与 `api_digests.router`（建议紧跟 `api_tasks.router`，ask/stream 与 ask 语义相邻）：

```python
    for r in (
        api_meta.router, api_tasks.router, api_ask_stream.router, api_digests.router,
        api_hands.router, api_events.router,
        api_analysts.router, api_sessions.router, api_workflows.router,
        api_whiteboard.router, api_mailbox.router, api_research.router,
        api_roadmap.router, api_theses.router, api_market_data.router,
        api_archive.router, api_vault.router, api_mcp.router,
    ):
        app.include_router(r)
```

路径冲突检查（已核对）：`/api/institute/*` 前缀无人占用；`/api/ask/stream` 与 tasks.py 的 `/api/ask` 不同路径；两个 router 均为模块级 `router = APIRouter(...)`，符合 CLAUDE.md 的 api 模块约定。挂载后复测：`.venv/bin/python -m pytest tests/test_digests.py -q`（本卡测试不经 main.py，挂载前后都应通过）。

## 建议主代理集成：tasks.py 抽公共 prompt 预处理（可选，非阻断）

REVIEW-B8 MF-2 修复后，`app/api/ask_stream.py::_prepare` 与 `app/api/tasks.py::ask` 开头（tasks.py:121-129）存在刻意的逐行镜像（tasks.py 不在 B8 分区，无法就地抽取）。建议主代理在 tasks.py 落一个小函数并让两处共用，消除双份维护：

```python
def prepare_ask(body: AskBody) -> tuple[str, str]:
    """(hand, prompt) for an ask-shaped request; 404 on unknown analyst."""
    settings = get_settings()
    prompt = body.prompt
    hand = body.hand or settings.default_hand
    if body.analyst_id:
        analyst = get_analyst(body.analyst_id)
        if analyst is None:
            raise HTTPException(404, f"unknown analyst {body.analyst_id}")
        prompt = build_analyst_prompt(analyst, body.prompt)
        hand = body.hand or analyst.hand or settings.default_hand
    return hand, prompt
```

`ask()` 改为 `hand, prompt = prepare_ask(body)`；然后把 `app/api/ask_stream.py` 的 `_prepare` 函数体替换为 `return prepare_ask(body)`（或直接删掉 `_prepare` 改用 import）。在此之前两处逻辑等价，`tests/test_digests.py::test_ask_stream_analyst_id_parity_with_sync_ask` 会在漂移时报警。B3 的 memory 注入若未来进入 `/api/ask`（在 `build_analyst_prompt` 加 `memory_block=`），抽取后流式端点自动同步获得。

## 其他分区外事项

- `app/config.py` / `.env` / migrations：本卡零新增设置、零迁移（占位端点特意不建表——`fact_cards` 归 Phase 3、`operator_actions` 归 Phase 6，届时只换渲染函数体，URL 形状不变）。
- 与 analyst-memory 卡（B3）的接口约定：`analyst_memory_md` 按 ROADMAP 列定义查询 `SELECT version, work_date, compact_md FROM analyst_memory WHERE analyst_id=? ORDER BY version DESC LIMIT 1`；若 B3 最终列名/排序键不同（例如以 `created_at` 排序），只需改 `app/institute/digests.py` 这一处查询；列不符期间端点自动降级占位，不会 500。
- 与 B1（executor.py）的接口消费：只用了 `executor.submit(on_chunk=)`、`executor.truncate_output`、`executor.get_task`、`executor.TERMINAL`（测试）——均为现有公开接口，未改任何 B1 文件。若 B1 重构 `truncate_output` 签名，`app/api/ask_stream.py` 的 done 帧输出截断需跟进。
- `roadmap/backlog.json`：Phase 2「Curl-back digest endpoints」与「Streaming ask」两项的状态推进由主代理按状态机处理，B8 未动。
- ROADMAP.md 的勾选（☐→☑）也留给主代理（ROADMAP.md 不在 B8 分区）。**注意 REVIEW-B8 MF-3**：「Streaming ask」原条目还含 "SPA + plugin render incrementally"——前端/插件增量渲染不在 B8 分区，主代理需确认由其他卡承接后才能勾选整项；后端 NDJSON 契约（帧形状、dropped 告知帧、断开语义）已在 `app/api/ask_stream.py` docstring 固定，前端可直接对接。
- REVIEW-B8 处置记录：MF-1（无界 Queue）→ 有界 `_ChunkBridge` 丢最旧+计数+status 告知帧+断开短路，见上；MF-2（analyst_id 缺失）→ 复用 `AskBody` + `_prepare` 镜像预处理 + 对等性测试，见上；MF-3 属主代理集成门槛（见上一条）。NH-1（未知 hand 422）未采纳：保持与 `/api/ask` 现行为一致（未知手走 executor 的 rate_limited 终态），改契约应两端点同改，归 tasks.py 分区决策；NH-3（测试 DDL 贴近 B3 migration）已顺手对齐 0010 真实列型；NH-4/NH-5 为文档措辞级，占位正文为常量且 analyst_id 由路由段限长，未改。
