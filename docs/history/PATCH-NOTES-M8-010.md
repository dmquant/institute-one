# PATCH-NOTES-M8-010 — Projects product surface 收尾

## 动手前现状核对

卡片描述和 `ROADMAP.md` 已落后于代码，四条验收标准中三条已经完成：

- **archive/unlink operations API 已完成**：`POST /api/projects/{id}/archive`、`POST /api/projects/{id}/unarchive`、`DELETE /api/projects/{id}/links/{kind}/{ref_id}` 均已存在；domain 层归档使用状态条件更新，解绑同时幂等清理显式链接与 `research_queue.project_id`。
- **SPA project page 已完成**：`Projects.tsx` 已有列表、创建、详情、状态、归档操作，并分组展示 research/board/thread/tree 四类链接；`App.tsx` 已有导航和列表/详情路由。
- **project digest 已完成**：JSON `GET /api/projects/{id}/digest` 与 Markdown `GET /api/projects/{id}/digest.md` 均已存在并有测试。
- **唯一缺口**：MCP `research_queue_add` 的 schema 不接受 `project_id`，调用 `research.enqueue()` 时也未透传。

## 本次改动

- `app/mcp.py`
  - 为既有写工具 `research_queue_add` 增加可选字符串参数 `project_id`。
  - 将参数透传给 `research.enqueue(..., project_id=...)`，成功新建时回显实际 `project_id`。
  - 将未知/已归档项目的 domain `ValueError` 映射为 MCP `-32602` validation error。
  - `WRITE_TOOLS` 保持原有三个工具，没有新增第四个写工具。
- `tests/test_mcp.py`
  - 新增 2 个测试：schema/三写工具边界及成功挂项目；已归档项目拒绝且不落队列行。

`topic_pool_add` 未增加 `project_id`：S4-P2-17 和 M8-010 的验收对象明确是 `research_queue_add`，且 `topic_pool` 没有项目归属列；扩展它需要另行定义“topic 还是生成后的 board 属于项目”的产品语义。

## 验证

- 修改前：`.venv/bin/python -m pytest tests/test_projects.py tests/test_mcp.py -q` → `28 passed`
- 修改后：同一命令 → `30 passed`
- 未修改前端文件，因此未触发 TypeScript 验证要求。

## 遗留风险

- `research.enqueue()` 的既有契约不会在 dedup 命中时给旧队列项补挂项目；MCP 保持这一语义。调用方若必须归档已有研究，应使用 project link API，而不是依赖重复 enqueue。
- `roadmap/backlog.json` 与 `ROADMAP.md` 的陈旧描述按本任务边界未修改。
