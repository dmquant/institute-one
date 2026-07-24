# R6 独立审查：A6 / M7-005 收尾 + M7-008

## 结论

**FAIL**

M7-005 的 `as_evidence` 与 move-to-review 门均符合验收；M7-008 的 12 条路由齐全，条件认领、依赖环检测和正常路径也基本正确，定向测试全部通过。但当前实现仍有两个会破坏控制面事实与审计历史的 must-fix：导出快照在空库重建时丢失 `blocked_reason`，使阻塞卡变成可推进；已 resolved 的 decision 可绕过 resolve 条件认领再次改写，且 `decision.resolved` 事件仍保留旧值。此外，checklist 改名会破坏“按文本合并”的 seed import 契约。

## 审查范围与验证

- 通读 `git diff -- app/institute/roadmap.py app/api/roadmap.py tests/test_roadmap.py`；三文件无 staged diff。
- 对照 `roadmap/02-data-model.md`、`roadmap/05-global-coding-process.md`、`roadmap/backlog.json` 中 M7-005/M7-008 acceptance。
- 通读 `migrations/0002_roadmap.sql`、`app/db.py`、`app/bus.py`、`tests/conftest.py`。
- `migrations/0002_roadmap.sql` 无 diff；A6 分区未见 prompt 改动，也没有 `PATCH-NOTES-A6.md`。工作树中的其他代理文件与新 migration 已排除。
- `roadmap_decisions` 已有 `decision/status/created_at/resolved_at`，足以承载 open→resolved 生命周期；本卡确实无需 migration。
- `git diff --check -- app/institute/roadmap.py app/api/roadmap.py tests/test_roadmap.py`：通过。

## M7-005 四点验收

1. **启动 coding session：PASS** — 既有 `POST /cards/{id}/sessions` 和 `create_session()` 正常保留。
2. **完整记录 session 字段：PASS** — actor、goal、planned/touched files、status、summary 均可读写，终态更新使用条件 UPDATE。
3. **command 作为 evidence：PASS** — `app/institute/roadmap.py:909-940`：
   - `exit_code == 0` → `pass`；
   - 非零 → `fail`；
   - `None` → `info`；
   - `artifact_ref` 为 `session_command:<command_id>`；
   - `card_id` 来自 session 查询结果，不接受调用方任意指定。
   API 的 `as_evidence` 已在 `app/api/roadmap.py:137-142,338-346` 贯通；域测试和真实 ASGI 路由测试均覆盖。
4. **move-to-review 门：PASS** — `app/institute/roadmap.py:589-600` 使用 `status != 'cancelled' AND TRIM(summary) != ''`；cancelled session 与空白 summary 都不开门。override 会写 `card.moved`，payload 包含 `override/reason`，测试覆盖审计事件。

## M7-008：12 条新路由逐条结论

1. **POST `/api/roadmap/cards`：PARTIAL**
   - 只允许 `inbox/ready`，正常 ready 要求 acceptance；重复 id 在 card+checklist 同一事务内原子失败。
   - 临时 `asyncio.gather` 实测同 id 并发创建为一成功、一 `RoadmapError`。
   - 但 `acceptance=["   "]` 会创建成功并进入 ready，见 P2-1。
2. **POST `/api/roadmap/cards/{id}/claim`：PASS**
   - UPDATE 带 owner 为空与读到的 status 条件，并检查 rowcount；blocked 拒绝。
   - 临时 `asyncio.gather` 实测一成功、一 `MoveConflict`，确实命中 lost-claim 分支。
3. **POST `/api/roadmap/cards/{id}/checklists`：PASS**
   - 初始 id 使用 `_det_id(card_id, kind, text)`，与 import 同源；重复项拒绝并发出 `checklist.added`。
4. **PATCH `/api/roadmap/checklists/{id}`：FAIL**
   - checked 更新与 `checklist.checked` 事件正确。
   - text 改名不重算 deterministic id，随后 seed import 无法按文本补回原项，见 P1-3。
5. **DELETE `/api/roadmap/checklists/{id}`：PASS（有明确 reconcile 语义）**
   - 删除与 `checklist.removed` 事件正确。
   - 实测删除 seed acceptance 后再次导入原 seed，会以相同 deterministic id 重建且 `checked=0`；这符合“merge by text”，但意味着 seed 项没有删除 tombstone。
6. **POST `/api/roadmap/cards/{id}/dependencies`：PARTIAL**
   - 自依赖与未知卡拒绝；环检测在 `db.transaction()` 写锁内完成。
   - 边语义为 `card_id → depends_on_id`；从新目标沿同方向 BFS 查找源卡，能正确拒绝 A→B→C→A。move-to-done 也查询当前卡的 `depends_on_id`，方向一致。
   - relation 首尾空白会破坏幂等返回，见 P2-2。
7. **DELETE `/api/roadmap/dependencies/{id}`：PASS**
   - 删除、404 与 `dependency.removed` 事件正确；下一次导入仍会按 seed 权威边集 reconcile。
8. **POST `/api/roadmap/decisions`：PASS**
   - 可开 card/board-level decision；`decision.opened` 经 `_record_event()` 真正调用 `bus.emit()`。
9. **GET `/api/roadmap/decisions`：PASS**
   - card/status 过滤和 limit 边界可用，status 有域枚举校验。
10. **GET `/api/roadmap/decisions/{id}`：PASS**
    - options JSON 正确还原，缺失返回 404。
11. **PATCH `/api/roadmap/decisions/{id}`：FAIL**
    - 正常 resolve 使用 `WHERE status='open'` 并检查 rowcount；第二次携带 `status='resolved'` 返回 409，`decision.resolved` 也走 bus。
    - 但不带 status 的 PATCH 可在 resolved 后改写 decision 并绕过事件，见 P1-2。
12. **GET `/api/roadmap/export`：FAIL**
    - 普通字段、顺序、acceptance、dependencies、owner/status 可形成稳定 seed shape。
    - `blocked_reason` 导出后不能导入，空库重建发生语义与字节漂移，见 P1-1。

## Must-fix

### P1-1. export→空库 import 会丢失阻塞状态并改变门禁语义

位置：

- `app/institute/roadmap.py:330-348`，尤其 `:346-347`
- `app/institute/roadmap.py:228-245,251-255`
- `tests/test_roadmap.py:652-697`

`export_backlog()` 明确输出 `blocked_reason`，但 import 的 INSERT/UPDATE 均不读取该字段。临时探针结果：

- 第一次 snapshot：`blocked_reason="external blocker"`；
- 空库 import 后再次 export：字段消失；
- 相同 JSON 序列化参数下两份 bytes 不相等。

这不只是展示字段漂移：claim/move 门依赖 `blocked_reason`（`app/institute/roadmap.py:426-427,575-579`），所以重建后的卡会从“阻塞”变为可认领/可前进。现有测试没有设置 blocker，且 `:697` 比较的是 Python dict，不是逐字节输出，因此没有覆盖该失败。应让 import 校验并持久化 `blocked_reason`，再增加 blocked card 的空库重建与 bytes 回归。

### P1-2. resolved decision 可被无事件改写，审计事实分叉

位置：

- `app/institute/roadmap.py:1032-1060`
- `app/api/roadmap.py:297-302`
- `tests/test_roadmap.py:453-483`

只有携带 `status="resolved"` 的分支使用 `WHERE status='open'`。随后请求 `PATCH {"decision":"B"}` 会进入 `elif sets` 的无条件 UPDATE，即使行已经 resolved 也返回 200。实测先 resolve 为 `A`、再这样 PATCH 后：

- 行仍是 `status=resolved`，但 `decision=B`；
- 唯一 `decision.resolved` 事件仍记录 `decision=A`。

同一无事件分支还允许 title/question/options 的可见修改，违背 data-model 的用户可见变化留事件原则。应明确生命周期：最小修复是 decision 文本只能和 open→resolved 条件认领一起写入；若要支持其他字段编辑，应限制 resolved 后可编辑范围并增加对应审计事件。必须补真实 HTTP 回归，覆盖 resolve 后不带 status 的 decision PATCH。

### P1-3. checklist text PATCH 破坏 deterministic id 与按文本合并

位置：

- `app/institute/roadmap.py:272-276`
- `app/institute/roadmap.py:685-690`
- `app/institute/roadmap.py:698-730`
- `tests/test_roadmap.py:584-600`

import/add 都把 id 定义为 `_det_id(card_id, kind, text)`，但 PATCH text 只改 `text`，保留旧 id。之后导入仍含旧文本的 seed 时，INSERT 使用旧文本对应的旧 id，与已改名行发生主键冲突并被 `INSERT OR IGNORE` 吞掉；结果既没有按文本补回旧项，也留下“id 与当前 text 不匹配”的行。临时探针已复现。

应二选一：不暴露 text 改名，只让 PATCH 负责 checked/sort_order；或在事务内按新文本重新生成 id，并定义旧 id 的客户端失效与事件语义。补“rename→原 seed import”回归。

## 其他问题

### P2-1. 空白 acceptance 可绕过 ready 门

`app/institute/roadmap.py:375-380,397-401` 只验证 list 非空，不 trim/拒绝每个文本。实测 `acceptance=["   "]` 成功创建 ready 卡。应复用 checklist 的非空文本规则，并在去重前规范化文本。

### P2-2. dependency relation 规范化晚于 deterministic id

`app/institute/roadmap.py:765` 用原始 relation 算 id，`:788` 才 `strip()` 入库。先以 `" blocks "` 添加、再以 `"blocks"` 添加时，唯一索引会忽略第二次 INSERT，但 `:796` 按另一个 id 查询，返回 `None`，HTTP 层继而误报 404。应先规范化 relation，再计算 id、写入和查询。

### P3-1. 仓库并发测试不是真并发

`tests/test_roadmap.py:527-560` 的二次 claim 是顺序调用，只走“已拥有”预检，不覆盖 `rowcount == 0` 分支；重复 create 也是顺序调用。实现本身经临时 `asyncio.gather` 探针验证为单赢家，但该保证尚未固化到测试。

### P3-2. 测试数量与声称不符

当前 `tests/test_roadmap.py` 实际收集并运行 **19** 个测试，不是 20 个；A6 diff 是 11→19。HTTP 面测试使用 `create_app()` + `httpx.AsyncClient(ASGITransport)`，确实经过真实 ASGI 路由，不是直调 API 函数，并逐条触达 12 条新路由。

## 硬规则核验

- **条件认领 rowcount：PASS** — claim、move、session finish、decision resolve 均检查 `db.execute()` 返回值。
- **bus handler 不 raise：PASS** — `_record_event()` 调用真实 `bus.emit()`；`app/bus.py:72-77` 捕获 handler 普通异常。
- **时间戳：PASS** — A6 新写入均使用 `bus.now_iso()`。
- **无 prompt / 0002 migration 改动：PASS** — scoped diff 与 `git diff --exit-code -- migrations/0002_roadmap.sql` 均确认。
- **接口面：PASS** — 12 条新路由均存在且挂载后的 ASGI 测试可达。

## 验证结果

- `.venv/bin/python -m compileall app -q`：退出码 0。
- `.venv/bin/python -m pytest tests/test_roadmap.py -q`：`19 passed in 1.61s`；另有 10 条 pytest 临时垃圾目录清理 warning，与 roadmap 断言无关。
- 未运行全量测试。
