# REVIEW-B8 — Phase 2 curl-back digests + streaming ask

## 结论

**FAIL**

后端主体路径可运行，定向编译与 10 个测试均通过；但 streaming ask 仍有两个上线前必须修复的问题：断开/慢客户端会让无界 Queue 持续积压全部 chunk，以及它并不真正兼容现有 `/api/ask` 的 `analyst_id` 语义。另需注意，ROADMAP 原项还包含 SPA + Obsidian 插件增量渲染，本分区未交付，整张卡暂不能标记完成。

## 逐项核验

- **ROADMAP · curl-back digest 路由：PASS** — 四个要求的 URL 均存在，正文由 `PlainTextResponse` 直接返回 Markdown，无 JSON envelope（`app/api/digests.py:22-52`）。
- **ROADMAP · streaming ask 后端：FAIL** — NDJSON 桥接、终帧和持久化任务路径成立，但存在无界缓冲与 ask 请求契约缺口（见 MF-1、MF-2）。
- **ROADMAP · streaming ask 完整交付：FAIL/集成门槛** — 原要求还写明 “SPA + plugin render incrementally”（`ROADMAP.md:123`）；B8 分区及 `PATCH-NOTES-B8.md:52-58` 未记录这部分的实现或交接，主代理需确认由其他卡承接后才能勾选整项。
- **Digest 只读性：PASS** — 仅有两段 `SELECT` 查询（`app/institute/digests.py:86-101,127-131`），无 `INSERT/UPDATE/DELETE`，占位 renderer 不访问数据库。
- **SQL 注入：PASS** — `analyst_id` 通过 `WHERE analyst_id = ?` 和参数元组传入（`app/institute/digests.py:127-131`），未拼接进 SQL。
- **8KB 与 UTF-8 边界：PASS（有 NH-4）** — 默认截断先为 marker 预留字节，再以 `errors="ignore"` 丢弃被切开的末尾码点，结果是合法 UTF-8 且不超过 8192 bytes（`app/institute/digests.py:39-46`）。
- **Analyst memory 缺表降级：PASS** — `sqlite3.OperationalError` 被捕获并返回稳定 Markdown 占位（`app/institute/digests.py:126-137`）；该捕获也会吞掉非“缺表/缺列”的 OperationalError，但不构成本轮阻断。
- **days 钳制：PASS** — `min(max(days, 1), 90)` 正确覆盖上下界（`app/institute/digests.py:82-85`）；非整数仍由 FastAPI 标准校验返回 422 JSON，故文件头“永远 200”不应按字面理解。
- **Markdown 响应：PASS** — digest content-type 为 `text/markdown; charset=utf-8`，正文直接输出；测试也验证首字符不是 JSON 容器（`app/api/digests.py:24-28`，`tests/test_digests.py:44-49`）。
- **Stream NDJSON：PASS** — 每帧均由 `json.dumps(..., ensure_ascii=False) + "\n"` 生成，文本内换行会被 JSON 转义；content-type 为 `application/x-ndjson`（`app/api/ask_stream.py:98-115`）。
- **Stream 任务来源：PASS** — done payload 使用 `executor.submit()` 返回的 `Task`；executor 各终态均通过 `get_task()` 从 `tasks` 表重新装载后返回，不是另造的临时结果（`app/api/ask_stream.py:81-105`，`app/router/executor.py:173-247`）。
- **done.output ≤8KB：PASS** — DB 中 output 已先受 executor 的 `settings.output_cap_bytes` 限制，终帧再调用同一字节安全函数压到 8192 bytes（`app/api/ask_stream.py:53-61`，`app/router/executor.py:207-247`）。
- **断开不取消：PASS（有 MF-1、NH-2）** — 生成器关闭不会调用 `submit_task.cancel()`；内部 `_execute` task 还由 `executor._running` 强引用，done callback 会读取异常，未见实际 GC 或 “exception was never retrieved” 风险（`app/api/ask_stream.py:78-100`）。
- **未知 hand：PASS-WITH-NIT** — 当前会沿 executor 的既有路径产出 HTTP 200 + `done.task.status="rate_limited"`，不会 500；但未知名称是无效输入而非限流，建议流开始前返回 422（见 NH-1）。
- **10 个测试的独立性：PASS** — autouse fixture 每个测试重建 DB、registry、锁与运行任务集合；测试自身无顺序依赖，定向运行 10/10 通过。
- **断开测试真实性：部分 PASS** — 测试直接取得 `StreamingResponse.body_iterator`，读取首帧后 `agen.aclose()`（`tests/test_digests.py:288-305`）；它真实触发生成器关闭，但没有经过 ASGI `http.disconnect`/网络传输层（见 NH-2）。
- **硬规则：PASS** — B8 应用代码无直接写库 SQL；模型调用只走 `executor.submit`；B8 未新增 migration；阈值使用 aware UTC ISO、展示日期使用 SGT `work_date()` 且不写入时间戳；现有 prompts 未改。

## Must-fix

1. **MF-1 · 无界 Queue 可在慢客户端或断开后无上限吃内存**（`app/api/ask_stream.py:69-76,87-90`）。`asyncio.Queue()` 没有 `maxsize`；客户端断开后 generator 不再 drain，但 `on_chunk` 会一直把后续输出放入 Queue，直到 hand 完成。executor 的 output cap 只在执行完成后截断落库值（`app/router/executor.py:207`），不能限制运行期 chunk；而 CLI pump 本身还会累计 stdout/stderr，因此这里会额外保留一份输出。应使用有界缓冲并捕获 `QueueFull`（丢弃并计数/发状态帧），generator 关闭后停止入队，同时保证 done 信号不会因满队列丢失。
2. **MF-2 · “Same body as POST /api/ask” 的声明不成立，缺少 `analyst_id` 语义**（`app/api/ask_stream.py:3-4,46-50,67-85`）。现有 `/api/ask` 接受 `analyst_id`、校验分析师并组装 persona prompt（`app/api/tasks.py:110-132`）；stream body 会静默忽略额外的 `analyst_id`，直接提交裸 prompt。插件现有 ask 正是发送该字段，未来切换到流式接口会丢失分析师人格（以及后续 memory 注入）。应复用同步 ask 的请求模型/共享 prompt-preparation helper，未知 analyst 保持 404。
3. **MF-3 · ROADMAP 完成状态的外部分区门槛**（`ROADMAP.md:123`）。后端 endpoint 不能单独代表原条目完成；SPA 与插件的 NDJSON 增量消费必须由主代理确认已由其他分区承接。此项不是要求 B8 越权修改其他代理文件。

## Nice-to-have

1. **NH-1 · 未知 hand 应是请求错误**（`app/api/ask_stream.py:67-85`，`tests/test_digests.py:255-263`）。建议在返回 `StreamingResponse` 前区分“名称不存在”和“已知 hand 暂不可用”：前者 422，后者才保留流内 `rate_limited` done 帧，避免污染任务审计并误导调用方。
2. **NH-2 · 补一个 ASGI 层断开测试**（`tests/test_digests.py:282-305`）。现有 `agen.aclose()` 是有效单元测试，但未验证 Starlette 收到 `http.disconnect` 后的实际取消边界；修 MF-1 时可同时验证断开后 Queue 不再增长且任务仍完成。
3. **NH-3 · 测试 DDL 应贴近 B3 migration**（`tests/test_digests.py:86-99`）。测试替身缺少 migration 的 `id TEXT PRIMARY KEY`、`created_at TEXT NOT NULL`，把 `work_date/compact_md` 放宽为 nullable，并将 `supersedes` 从 `TEXT` 改成 `INTEGER`；这不影响当前 SELECT，但削弱跨卡集成验证。
4. **NH-4 · 两个带 analyst_id 的占位结果没有经过 8KB clamp**（`app/institute/digests.py:134-149`）。正常 roster id 很短，不影响生产常规路径；若坚持“每个 digest 均 ≤8KB”的模块契约，应对占位也调用 `clamp_md()` 或限制 analyst id 长度。
5. **NH-5 · recent reports 的 JSON 损坏会违背“永不 500”文档承诺**（`app/institute/digests.py:65-70`，`app/api/digests.py:10-13`）。`variables/results` 的 `json.loads` 未降级；可捕获 `ValueError/TypeError`，或收窄文档承诺为“后期表缺失不 500”。

## B3 列名一致性

- **查询列名完全一致**：B8 使用 `analyst_id`、`version`、`work_date`、`compact_md`；`migrations/0010_analyst_memory.sql:7-17` 均以同名定义。
- **排序语义一致**：B8 按 `version DESC` 取最新，migration 有 `UNIQUE(analyst_id, version)` 和 `(analyst_id, version DESC)` 索引。
- **B3 的附加列不冲突**：migration 另有 `id`、`created_at`，以及 `supersedes TEXT`（指向前版 row id）；B8 不写表也不读取这些列。
- **精确差异只在测试替身**：见 NH-3；生产查询与 B3 migration 无列名/类型依赖冲突。

## main.py 挂载补丁

- **补丁本身：PASS** — `ask_stream as api_ask_stream` 与 `digests as api_digests` 的导入语法、别名及现有 import 风格正确；把两个 `.router` 加入既有 tuple 可直接应用。
- **路由冲突：无** — `/api/ask/stream` 不会与精确路径 `/api/ask` 冲突，`/api/institute/*` 当前未占用，SPA catch-all 注册在 API 之后。
- **集成状态：尚未挂载** — 当前 `app/main.py:147-173` 不含这两个 router；B1/主代理应用补丁前，真实应用中端点不可访问。按分区约定这不是 B8 越权缺陷，但属于合并门槛。

## 验证

- `.venv/bin/python -m compileall app -q`：**PASS**（exit 0；实际使用仓库绝对路径执行）。
- `.venv/bin/python -m pytest tests/test_digests.py -q`：**PASS** — `10 passed in 0.68s`。
- 未运行全量测试；除新建本报告外未修改任何仓库文件。
