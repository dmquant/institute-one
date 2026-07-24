# PATCH-NOTES-A4 — 分区外改动建议（A4 未执行，留给对应分区）

A4 分区内已完成：maintenance 门控补齐（scheduler.py）、`POST /api/admin/maintenance`（api/meta.py）、
workflow step `analyst` → `analyst_id` 归一化（workflows.py + workflows/*.json 仅 key 名）、
tests/test_maintenance.py、tests/test_workflows.py 归一化测试。以下改动落在 A4 禁区，建议由对应分区跟进：

1. **README.md L143**（Operations 一节）："Pause everything new: … kickoff jobs skip" 的语义已扩大：
   现在 briefing / daily-report / whiteboard-tick / mailbox-sweep 也受 maintenance 门控（8 个任务中仅 janitor 不受控）。
   且新增了写 API：`POST /api/admin/maintenance {"paused": true|false}`（不再只能手写 admin_state）。建议同步文案。

2. **CLAUDE.md Map 表 workflows/*.json 行**：`Steps: {id, title, analyst|analyst_id, …}` 建议改为
   `analyst_id`（canonical；`analyst` 仅作 legacy 输入被 reconcile_from_disk() 归一化吸收）。

3. **frontend（禁区）**：ROADMAP P2 条目要求 "maintenance toggle API + SPA switch"。API 已就绪，
   SPA 还差一个开关（建议放 Settings 页，调 `GET /api/admin/state` + `POST /api/admin/maintenance`）。

4. **frontend/src/api.ts**：`WorkflowStep` 类型仍同时声明 `analyst?` 与 `analyst_id?`。归一化后 API
   返回只有 `analyst_id`；`analyst?` 可在下次前端改动时移除（无功能影响，纯类型收紧）。
