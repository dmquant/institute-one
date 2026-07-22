# R2 独立审查：A2 / Phase 0 MCP hardening

## 结论

**FAIL**

`research_queue_add` 已正确接入当前 `research.enqueue()`，冷却拒绝仍保持 HTTP 200 + JSON-RPC `result` + `content[0].text` JSON 字符串，定向编译与 3 个测试也全部通过；但 `topic_pool_add` 用等值预查推断实际 INSERT 结果并不可靠：既不等价于域函数的 content hash，又在普通并发调用下稳定双重误报 `added=true`、重复发事件。两项均属 must-fix。

## 审查范围

- 已执行 `git diff -- app/mcp.py`，并通读未跟踪新文件 `tests/test_mcp.py`。
- 对照当前工作区逐行读取 `app/institute/research.py:31-64` 的 `enqueue()`、`app/institute/whiteboard.py:54-62` 的 `add_topic()`，并核对 `app/db.py`、`migrations/0001_init.sql`、README 与 MCP JSON-RPC 封装。
- A2 范围仅为 `app/mcp.py` 与 `tests/test_mcp.py`；其余在途代理修改未纳入结论。

## 逐项核验

- **enqueue 三分支：PASS** — `app/institute/research.py:36-42` 先返回 pending/running 去重结果（含 `deduped=True`），`:44-55` 再在近期完成且 `priority <= 0` 时返回 cooldown 拒绝，`:57-64` 最后插入 pending 行并返回完整数据库行；`app/mcp.py:363-370` 对三种结果的判断与当前域函数一致。
- **冷却优先级语义：PASS** — `app/institute/research.py:54` 明确只有 `priority <= 0` 才拒绝，故 `priority > 0` 越过冷却是域函数原有语义；`app/mcp.py:349-353` 的工具说明准确。pending/running 去重仍优先于该 override，也与说明第一句一致。
- **旧结果字段兼容：PASS（成功/去重）/WARN（拒绝）** — 正常新增和去重仍保留旧字段 `id/topic/status/duplicate`；`app/mcp.py:364-366` 的新拒绝结果只有 `queued/refused/topic/last_completed_at`，缺少 `id/status/duplicate`，因此 MCP 协议兼容，但依赖旧业务字段集的自定义客户端不兼容，见 C1。
- **二元组预查等价性：FAIL** — 相同 `(topic, question)` 必然得到相同 hash，但反向不成立；域函数计算的是无分隔符的 `sha256(topic + question)[:16]`，等值预查不能代表 content-hash 去重，见 M1。
- **大小写/空白：PASS-WITH-CAVEAT** — `add_topic()` 本身不做大小写、空白或 Unicode 规整，hash 和 SQL 等值比较都区分这些差异；A2 保留旧 MCP 对两字段 `.strip()` 的行为。真正破坏等价性的是无分隔符拼接边界（以及理论上的 64-bit hash 碰撞），不是新引入的大小写规整。
- **research 事件：PASS-WITH-NIT** — A2 删除了 MCP 侧 emit，`app/institute/research.py:63` 仅在真正入队时发一次 `research.queued`，去重/冷却均不发，没有重复事件；但事件 payload 相比旧 MCP 路径丢失 `priority/source`，见 N1。
- **topic_pool 事件：FAIL** — 顺序调用的精确重复不会发事件，但 hash 别名及并发都会在实际未新增行时执行 `app/mcp.py:394`，返回 `added=true` 并发出伪新增事件，见 M1、M2。
- **JSON-RPC / Claude Code 封装：PASS** — `app/mcp.py:435-449` 仍把工具返回值 JSON 编码到单个 text content block，`:498-506` 将 cooldown 当成功 `result` 返回 HTTP 200；`tests/test_mcp.py:17-30,50-72` 已实测 envelope、无 `error`、text 内 JSON。它不会被通用 MCP 客户端当协议错误误解析。
- **错误路径一致性：PASS** — cooldown 是可预期业务拒绝，使用成功 `result` 合理；参数/未知工具仍走 `-32602`，内部异常仍走 `-32000` JSON-RPC `error`，A2 未改变其他工具约定。
- **tools/list 与写工具数量：PASS** — input schema 未变，只有 description 增补；`app/mcp.py:346-430` 仍恰好注册三个写工具 `research_queue_add`、`topic_pool_add`、`institute_ask`，与 `README.md:122` 和 `README.zh-CN.md:119` 一致。
- **硬规则：PASS** — A2 未改 prompts、未新增/修改 migration；新写入时间戳全部由域函数继续使用 `bus.now_iso()`，冷却阈值也采用同格式的 UTC 秒级 ISO 字符串。

## Must-fix

### M1（高）等值预查不等价于域 content hash，会把旧行冒充成新行

- `app/institute/whiteboard.py:55` 对 `topic + question` 直接拼接后取 hash，没有长度前缀或分隔符。因此 `("机器人产业链", "")` 与 `("机器人", "产业链")` 的 hash 完全相同，这不是概率极低的密码学碰撞。
- `app/mcp.py:388-392` 的二元组等值查询看不到前一行；`:393` 调用 `add_topic()` 后，`INSERT OR IGNORE` 被唯一约束忽略，域函数返回已有行；`:394-395` 却无条件发 `topic_pool.added` 并返回 `added=true`。
- 实测先由域函数写入 `("机器人产业链", "")`，再由 MCP 添加 `("机器人", "产业链")`：MCP 返回 `{"added": true, "duplicate": false, "id": 1, "topic": "机器人"}`，但 id=1 的实际存储内容仍是 `("机器人产业链", "")`。
- 应让 `whiteboard.add_topic()` 返回实际 `INSERT OR IGNORE` 的 rowcount/`added` 标志，并让 MCP 以该原子写结果决定响应和事件；精确等值预查可仅用于兼容旧 MCP hash 行，不能代替 INSERT 结果。

### M2（高）预查与 INSERT 非原子，并发时稳定双重误报和重复事件

- `app/mcp.py:388-395` 在等值 SELECT 与域 INSERT 之间至少有一个调度窗口；`app/db.py:66-83` 只逐语句执行/加写锁，没有覆盖这两个操作的事务或锁。
- 两个并发 MCP 请求可同时读到“不存在”，随后一个 INSERT 成功、另一个 `INSERT OR IGNORE`；两者取得同一行后都返回 `added=true` 并各发一次事件。
- 无故障注入的本地探针连续运行 20 组 `asyncio.gather()`：20/20 组都出现两个 `added=true`，最终每组只有一行却共有 40 个 `topic_pool.added` 事件；该窗口在当前单连接异步实现下并不“极窄”。
- 这会污染 durable event cursor，并让两个调用者都误以为自己创建了行，不宜接受。让域函数暴露实际 INSERT rowcount 可同时修复 M1 与 M2。

## 兼容性与测试补强

- **C1 冷却拒绝字段集**：`app/mcp.py:364-366` 不保留 `id/status/duplicate`。Claude Code 的 MCP transport 不受影响，但旧自定义调用方若统一读取这些键会失败。若要求应用层兼容，可在拒绝结果中同时提供 `id: null`、`status: "refused"`、`duplicate: false`；至少应固定并文档化输出契约。
- **N1 research 事件 payload**：域函数在 `app/institute/research.py:63` 只发 `{"topic": ...}`，而 A2 删除的旧 MCP emit 还包含 `priority/source`。仓库内现有消费者只按事件类型刷新，未发现字段依赖；外部 SSE 消费者仍可能观察到退化。
- **N2 测试缺口**：`tests/test_mcp.py:75-99` 只覆盖完全相同二元组的顺序去重，未覆盖无分隔符 hash 别名、两个并发 MCP 调用、拒绝结果旧字段集及 research 事件次数/payload。

## 验证摘要

- `.venv/bin/python -m compileall app -q`：退出码 0，无输出。
- `.venv/bin/python -m pytest tests/test_mcp.py -q`：`3 passed in 0.34s`。
- `git diff --check -- app/mcp.py`：退出码 0。
- 额外只读探针复现了 M1，并分别以强制交错和 20 组自然并发复现 M2。
- 按要求未运行全量测试。
