# PATCH-NOTES-A2 — 请主代理应用到 app/institute/whiteboard.py（A5 分区）

## 目的

修复 R2 审查的 M1/M2：MCP `topic_pool_add` 需要一个**权威的"本次调用是否真的插入"信号**。
唯一可靠来源是 `add_topic()` 内部 `INSERT OR IGNORE` 的 rowcount（`db.execute` 本来就返回它，
当前被丢弃）。任何 MCP 侧的预查/回读判定都做不到：

- **M1**：域 hash 是无分隔符拼接 `sha256(topic + question)[:16]`，`("机器人产业链", "")` 与
  `("机器人", "产业链")` 哈希相同——等值预查看不到别名行，会把旧行冒充成新行。
- **M2**：预查与 INSERT 之间有调度窗口，并发同参调用稳定双重误报 `added=true` 并重复发事件
  （R2 实测 20/20 组复现）。rowcount 判定天然原子（`db.execute` 持写锁），两个问题同时消除。

## 精确 diff（apply 到 `app/institute/whiteboard.py` 的 `add_topic`，当前位于 54–62 行）

```diff
 async def add_topic(topic: str, question: str = "", source: str = "manual", score: float = 1.0) -> dict[str, Any]:
     content_hash = hashlib.sha256((topic + question).encode("utf-8")).hexdigest()[:16]
-    await db.execute(
+    n = await db.execute(
         "INSERT OR IGNORE INTO topic_pool (topic, question, source, score, status, content_hash, created_at) "
         "VALUES (?,?,?,?, 'pending', ?, ?)",
         (topic, question, source, score, content_hash, bus.now_iso()),
     )
     row = await db.query_one("SELECT * FROM topic_pool WHERE content_hash = ?", (content_hash,))
-    return row or {}
+    return {**(row or {}), "inserted": bool(n)}
```

hash 算法、INSERT 语句、时间戳（`bus.now_iso()`）均不变；纯增量返回一个 `inserted` 键。

## 影响面（已核查全部调用方）

- `app/mcp.py` `_t_topic_pool_add`（A2 已改）：读 `row.get("inserted")` 决定 `added`/事件。
  **补丁应用前**该键缺失 → MCP 保守地一律报 `duplicate: true` 且不发事件（数据写入不受影响，
  行照常插入）；应用后行为完全正确。
- `app/api/whiteboard.py` `POST /api/whiteboard/topics`：直接回传 dict，多一个键，向后兼容。
- `app/institute/research.py` `_apply_followups`：不读返回值，无影响。

## 应用后的验证

`tests/test_mcp.py` 里两个用例（`test_topic_pool_add_reports_genuine_insert`、
`test_topic_pool_add_concurrent_calls_single_added`）当前因探测不到 `inserted` 键而 **skip**；
补丁应用后自动变为实测并应通过。M1 别名用例与精确重复用例不依赖补丁，现已通过。
